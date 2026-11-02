"""One page, one short-lived process; no network or credentials required."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .layout import LAYOUT_SCHEMA_VERSION, folio_candidates, layout_blocks


def render_size(width, height):
    if not all(math.isfinite(v) and v>0 for v in (width,height,width*height)):
        raise ValueError('Invalid PDF page dimensions')
    # Bound both dimensions before allocation, including extremely tall PDF pages.
    # Reserve rounding space for MuPDF's integer bounding rectangle.
    scale=min(4.0,2199.0/width,8191.0/height,math.sqrt(7_970_000/(width*height)))
    if min(width*scale,height*scale)<1:
        raise ValueError('Page geometry is unreadable within the pixel budget; review required')
    return scale


def recognize(path: Path, page: int):
    import cv2
    import fitz
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    with fitz.open(path) as document:
        source=document[page-1]
        scale=render_size(source.rect.width,source.rect.height)
        pixmap=source.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False)
        if pixmap.width>2200 or pixmap.width*pixmap.height>8_000_000:
            raise ValueError('Rendered page exceeded pixel budget')
        # Decode the raw RGB buffer directly; avoid the former PNG encode/decode copy.
        image=np.frombuffer(pixmap.samples,dtype=np.uint8).reshape(pixmap.height,pixmap.width,3)
        image=cv2.cvtColor(image,cv2.COLOR_RGB2BGR)
        gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
        if float(gray.std())<34.0:
            enhanced=cv2.createCLAHE(clipLimit=1.8,tileGridSize=(8,8)).apply(gray)
            image=cv2.cvtColor(enhanced,cv2.COLOR_GRAY2BGR)
        result,_=RapidOCR()(image)
        lines=[str(item[1]).strip() for item in result or [] if isinstance(item,(list,tuple)) and len(item)>1 and str(item[1] or '').strip()]
        return {'page':page,'text':'\n'.join(lines),'layout_schema':LAYOUT_SCHEMA_VERSION,
                'ocr_engine':'rapidocr-layout-v3','ocr_profile':'bounded-page-v1',
                'folio_candidates':folio_candidates(result,image.shape),
                'layout_blocks':layout_blocks(result,image.shape),
                'image_width':pixmap.width,'image_height':pixmap.height}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--page',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    payload=recognize(args.pdf,args.page)
    args.output.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')


if __name__=='__main__':
    main()
