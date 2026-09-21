"""Pinned offline OCR and evidence comparison; never grants publication approval."""
from __future__ import annotations
import hashlib
import math
import re
import time
from pathlib import Path

PROFILE = 'rapidocr-3.9.2-ppv6-small-det-medium-rec-300dpi-v1'
MODELS = {
    'PP-OCRv6_det_small.onnx': '090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f',
    'PP-OCRv6_rec_medium.onnx': 'eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b',
    'ch_ppocr_mobile_v2.0_cls_mobile.onnx': 'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c',
}


def engine(model_root, threads=4):
    from rapidocr import RapidOCR, ModelType, OCRVersion
    from importlib.metadata import version
    if version('rapidocr') != '3.9.2':
        raise RuntimeError('免费 OCR 运行库版本不符')
    root = Path(model_root)
    for name, expected in MODELS.items():
        if not (root/name).is_file() or hashlib.sha256((root/name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('免费 OCR 模型缺失或校验不符：' + name)
    return RapidOCR(params={
        'Det.ocr_version': OCRVersion.PPOCRV6, 'Det.model_type': ModelType.SMALL,
        'Rec.ocr_version': OCRVersion.PPOCRV6, 'Rec.model_type': ModelType.MEDIUM,
        'Det.model_path': str(root/'PP-OCRv6_det_small.onnx'),
        'Rec.model_path': str(root/'PP-OCRv6_rec_medium.onnx'),
        'Cls.model_path': str(root/'ch_ppocr_mobile_v2.0_cls_mobile.onnx'),
        'Global.max_side_len': 4200, 'Global.text_score': 0.0, 'Global.log_level': 'warning',
        'EngineConfig.onnxruntime.intra_op_num_threads': threads,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1, 'Rec.rec_batch_num': 4,
    })


def render(path, page):
    import fitz
    with fitz.open(path) as pdf:
        if not 1 <= page <= len(pdf):
            raise ValueError('页码越界')
        p = pdf[page-1]
        # Some scan-only PDFs use pixel counts as points. Reading their native
        # pixels preserves all source detail without an artificial 4x enlargement.
        if max(p.rect.width, p.rect.height)*300/72 > 4200:
            infos=p.get_image_info(xrefs=True)
            if len(infos)==1 and not p.get_text().strip() and not p.get_drawings() and not p.rotation:
                info=infos[0];box=info['bbox'];rect=tuple(p.rect)
                if (info.get('xref',0)>0 and not info.get('has-mask')
                    and info['width']<=4200 and info['height']<=8400
                    and all(abs(a-b)<.1 for a,b in zip(box,rect))
                    and abs(info['transform'][1])<.01 and abs(info['transform'][2])<.01
                    and info['transform'][0]>0 and info['transform'][3]>0):
                    from PIL import Image,PngImagePlugin
                    from io import BytesIO
                    original=pdf.extract_image(info['xref'])['image']
                    source=Image.open(BytesIO(original)).convert('RGB')
                    meta=PngImagePlugin.PngInfo();meta.add_text('ingestion_render','native_scan_pixels')
                    output=BytesIO();source.save(output,format='PNG',pnginfo=meta)
                    return output.getvalue()
            raise ValueError('页面过大，需要分区识别，已保留待核对')
        return p.get_pixmap(dpi=300, colorspace=fitz.csRGB, alpha=False).tobytes('png')


def recognize(ocr, png):
    import numpy as np
    from PIL import Image
    from io import BytesIO
    started = time.monotonic()
    source=Image.open(BytesIO(png))
    render_policy=source.info.get('ingestion_render','pdf_300dpi')
    array = np.array(source.convert('RGB'))
    height,width=array.shape[:2]
    if width>4200 or height>8400:raise ValueError('页图超出分区识别范围')
    lines=[];tiles=[]
    # Overlap protects complete text lines at seams. A line belongs to exactly
    # one core interval by its center, without fuzzy text deletion or rewriting.
    cores=[(0,height)] if height<=4200 else [(y,min(height,y+3800)) for y in range(0,height,3800)]
    for first,last in cores:
        top=max(0,first-128);bottom=min(height,last+128)
        result=ocr(array[top:bottom])
        texts=list(result.txts or [])
        scores=[] if result.scores is None else [float(s) for s in result.scores]
        boxes=[] if result.boxes is None else result.boxes.tolist()
        for text,score,box in zip(texts,scores,boxes):
            box=[[x,y+top] for x,y in box]
            center=sum(p[1] for p in box)/4
            if first<=center<last:
                lines.append({'text':text,'score':score,'box':box})
        tiles.append({'top':top,'bottom':bottom,'core_top':first,'core_bottom':last})
    dark=sum(int((array[y:y+256].sum(axis=2,dtype=np.uint16)<645).sum()) for y in range(0,height,256))
    return {'profile': PROFILE, 'model_hashes': MODELS, 'image_sha256': hashlib.sha256(png).hexdigest(),
            'render_policy':render_policy,
            'text': '\n'.join(line['text'] for line in lines), 'lines':lines,'tiles':tiles,
            'width':array.shape[1], 'height':array.shape[0], 'seconds':time.monotonic()-started,
            'ink':dark/(width*height)}


def validate(result):
    if result.get('profile') != PROFILE or result.get('model_hashes') != MODELS:
        raise ValueError('OCR 模型版本或哈希不符')
    if not isinstance(result.get('text'), str) or len(result['text']) > 20000:
        raise ValueError('OCR 文本格式或长度异常')
    if not re.fullmatch('[0-9a-f]{64}', str(result.get('image_sha256',''))):
        raise ValueError('OCR 页图校验值无效')
    if not all(isinstance(result.get(k),int) and 1 <= result[k] <= limit for k,limit in [('width',4200),('height',8400)]):
        raise ValueError('OCR 页图尺寸异常')
    lines = result.get('lines')
    if not isinstance(lines,list) or len(lines)>2000:
        raise ValueError('OCR 行信息异常')
    for line in lines:
        box = line.get('box', [])
        score = line.get('score', -1)
        if not isinstance(line.get('text'),str) or not isinstance(score,(float,int)) or not 0 <= score <= 1:
            raise ValueError('OCR 行内容或置信度异常')
        if len(box)!=4 or any(not isinstance(p,list) or len(p)!=2 for p in box):
            raise ValueError('OCR 定位框异常')
        if any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 or v>max(result['width'],result['height']) for p in box for v in p):
            raise ValueError('OCR 定位坐标异常')
    if result['text'] != '\n'.join(line['text'] for line in lines):
        raise ValueError('OCR 全文和逐行内容不一致')
    for field in ['seconds','ink']:
        if not isinstance(result.get(field),(int,float)) or not math.isfinite(result[field]) or result[field]<0:
            raise ValueError('OCR 统计值异常')
    if result['ink']>1:
        raise ValueError('OCR 图像统计异常')


def compare(primary, result):
    import difflib
    # Whitespace-only comparison: do not normalize punctuation/full-width forms away.
    a,b = re.sub(r'\s+','',primary), re.sub(r'\s+','',result['text'])
    differences = []
    for op,i,j,k,l in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes():
        if op != 'equal':
            differences.append({'operation':op,'primary':a[i:j], 'ocr':b[k:l],
                                'primary_offset':i,'ocr_offset':k})
    flags = []
    for i,line in enumerate(result['lines']):
        value = line['text'].strip()
        if line['score']<.9:
            flags.append({'kind':'low_confidence','line':i})
        if value and not re.search(r'[\w\u4e00-\u9fff]',value):
            flags.append({'kind':'isolated_symbol','line':i})
    if '\ufffd' in b or '□' in b:
        flags.append({'kind':'unreadable_character'})
    if not b:
        flags.append({'kind':'possible_blank'})
    return {'equal':a==b,'differences':differences, 'flags':flags,
            'note':'识别器一致不等于已经核验；标点未作自动补齐或替换'}
