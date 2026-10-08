"""Prepare source-bound drafts; image review is required before publication."""
import collections
import hashlib
import json
import re
import unicodedata
from pathlib import Path

FURNITURE={'header','footer','number','page_number','header_image','footer_image','seal'}
HEADINGS={'doc_title','paragraph_title','title','heading'}


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return h.hexdigest()


def clean_title(value):
    value=re.sub(r'\$[^$]*\$','',value)
    value=re.sub(r'<[^>]*>','',value)
    return re.sub(r'[#*_]|\s+','',unicodedata.normalize('NFKC',value)).strip('.…·')


def key(value):
    return re.sub(r'[^\w]','',clean_title(value))


def read_pages(pdf):
    import pymupdf as fitz
    pdf=Path(pdf);paths=list(pdf.parent.glob(pdf.name+'*.json'))
    with fitz.open(pdf) as doc:
        if len(paths)>1:raise ValueError('ambiguous JSON companions')
        if paths:
            payload=json.loads(paths[0].read_text('utf-8-sig'))
            if len(payload)!=len(doc):raise ValueError('physical page count mismatch')
            rows=[]
            for i,result in enumerate(payload):
                blocks=result.get('prunedResult',result).get('parsing_res_list')
                if not isinstance(blocks,list):raise ValueError('structured blocks missing')
                rows.append(dict(page=i+1,blocks=[dict(kind=b['block_label'],text=b.get('block_content',''),
                    bbox=b.get('block_bbox'),order=b.get('block_order',j)) for j,b in enumerate(blocks)]))
            evidence={'paddle_source':dict(schema=1,json_sha256=sha(paths[0]),file=paths[0].name,coordinates_verified=False)}
        else:
            md=pdf.with_name(pdf.name+'_by_PaddleOCR-VL-1.6.md')
            if not md.exists():raise ValueError('missing structured source and Markdown')
            rows=[]
            for i,p in enumerate(doc):
                blocks=[]
                for b in p.get_text('blocks',sort=True):
                    if b[6]!=0:continue
                    text=b[4].strip();kind='text'
                    if b[1]>p.rect.height*.88 and re.search(r'爱欲与文明|译文经典|^\s*\d+\s*$',text):
                        kind='footer'
                    blocks.append(dict(kind=kind,text=text,bbox=list(b[:4]),order=len(blocks)))
                rows.append(dict(page=i+1,blocks=blocks))
            evidence={'source_evidence':dict(schema=1,kind='pdf_text_with_markdown',pdf_sha256=sha(pdf),
                markdown_sha256=sha(md),file=md.name,extractor='PyMuPDF',physical_page_boundaries=True)}
        return rows,evidence,doc.get_toc()


def label_candidates(row):
    labels=[]
    for b in row['blocks']:
        if b['kind'] in FURNITURE or b['kind']=='aside_text':
            labels.extend(int(x) for x in re.findall(r'(?<!\d)\d{1,3}(?!\d)',b['text']))
    return labels


def parse_contents(rows, pages):
    entries=[];pending=''
    for physical in pages:
        row=rows[physical-1]
        for block in row['blocks']:
            if block['kind'] in FURNITURE or block['kind'] in {'footnote','vision_footnote','image'}:continue
            for raw in block['text'].splitlines():
                raw=re.sub(r'\$[^$]*\$','',raw).strip(' #\t')
                if not raw or key(raw) in {'目录','目次'} or re.fullmatch(r'[IVXivx\d]+',raw):continue
                match=re.match(r'^(.*?)(?:[.…·．]{2,}\s*|[（(/_\-]\s*|\s+)(\d{1,3})[）)]?\s*$',raw)
                if not match:match=re.match(r'^([^\d]+?)(\d{1,3})\s*$',raw)
                if match:
                    title=clean_title(pending+match[1]);pending=''
                    if title:entries.append(dict(title=title,label=int(match[2]),toc_pdf_page=physical))
                elif re.match(r'^(第[一二三四五六七八九十]+[部篇卷]|[上中下]编|[奠基开拓发展挑战].*篇)',raw):
                    pending=''
                elif re.fullmatch(r'[（(]?\d{4}[—~～-]\d{4}[）)]?',raw):
                    pending=''
                else:pending+=raw
    return entries


def prepare(pdf,spec):
    rows,evidence,bookmarks=read_pages(pdf);source=sha(pdf)
    meta=spec['metadata'];segments=spec['segments'];toc_pages=spec['toc_pages']
    if 'source_evidence' in evidence:
        import pymupdf
        with pymupdf.open(pdf) as doc:
            for p in toc_pages:
                lines=[]
                for word in sorted(doc[p-1].get_text('words'),key=lambda w:(w[1],w[0])):
                    line=next((x for x in lines if abs(x[0]-word[1])<3),None)
                    if line is None:line=[word[1],[]];lines.append(line)
                    line[1].append(word)
                text='\n'.join(' '.join(w[4] for w in sorted(line[1],key=lambda w:w[0])) for line in lines)
                text=re.sub(r'(?m)^(\d{3})\s+(.+)$',r'\2 ..... \1',text)
                rows[p-1]['blocks']=[dict(kind='content',text=text,bbox=None,order=0)]
    issues=[];review=[]
    for row in rows:
        p=row['page'];observed=label_candidates(row)
        row.update(text='\n'.join(b['text'] for b in row['blocks'] if b['kind'] not in FURNITURE),
            label='',kind='front',segment_id='unnumbered',segment_title='未编号页',checked=False)
        for segment in segments:
            if segment['start']<=p<=segment['end']:
                expected=p-segment['offset']
                row.update(label=str(expected),kind=segment.get('kind','body'),segment_id=segment['id'],
                    segment_title=segment['title'])
                matched=expected in observed
                method='ocr_number_with_sequence' if matched and 'paddle_source' in evidence else 'native_margin' if matched else 'needs_review'
                row['label_evidence']=dict(method=method,observed=observed,offset=segment['offset'],source_pdf_page=p)
                if not matched:review.append(p)
        if p in toc_pages:
            row.update(kind='front',segment_id='contents',segment_title='目录')
        if not row['text'].strip():
            row.update(kind='blank',label='',segment_id='blank',segment_title='原件空白页')
            review.append(p)
    entries=parse_contents(rows,toc_pages)
    normalized={r['page']:key(r['text']) for r in rows}
    toc=[]
    for e in entries:
        title=e['title'];needle=key(title)
        candidates=[r for r in rows if r['page'] not in toc_pages and needle and needle in normalized[r['page']]]
        matching=[r for r in candidates if r['label']==str(e['label'])]
        if len(matching)==1:row=matching[0]
        elif len(candidates)==1:row=candidates[0]
        else:
            main=next(s for s in segments if s['id']=='body')
            p=e['label']+main['offset']
            row=rows[p-1] if 1<=p<=len(rows) else None
            issues.append(dict(kind='toc_title',title=title,label=e['label'],pdf_page=p,
                toc_pdf_page=e['toc_pdf_page'],candidate_pages=[r['page'] for r in candidates]))
        if row is None:continue
        level=1
        if re.match(r'^第.+节',title):level=2
        elif re.match(r'^[一二三四五六七八九十]+[、. ]?',title) and not title.startswith(('一九','一切')):level=3
        toc.append(dict(title=title,pdf_page=row['page'],printed_page=row['label'],level=level,
            kind=row['kind'],sort_order=len(toc),authors=[],date='',provenance_verified=False,
            evidence_method='paper_contents_and_body_text',toc_pdf_page=e['toc_pdf_page']))
    # Named PDF outline entries are only additional candidates, never folio evidence.
    for level,title,p in bookmarks:
        title=title.replace('\x00','').strip()
        if p in toc_pages or not 1<=p<=len(rows) or re.fullmatch(r'\d+|封面|书名|版权|封底|目录',title):continue
        if not any(t['pdf_page']==p and key(t['title'])==key(title) for t in toc):
            row=rows[p-1]
            if key(title) in key(row['text']):
                toc.append(dict(title=clean_title(title),pdf_page=p,printed_page=row['label'],level=level,
                    kind=row['kind'],sort_order=0,authors=[],date='',provenance_verified=False,
                    evidence_method='pdf_outline_and_body_text',toc_pdf_page=None))
    toc=list({(t['pdf_page'],key(t['title'])):t for t in toc}.values())
    toc.sort(key=lambda x:(x['pdf_page'],x['sort_order']))
    for i,t in enumerate(toc):
        t['sort_order']=i
        following=next((n['pdf_page'] for n in toc[i+1:] if n['level']<=t['level'] and n['pdf_page']>t['pdf_page']),len(rows)+1)
        t['end_pdf_page']=following-1
    main=next(s for s in segments if s['id']=='body')
    return dict(book_id=source[:32],source_sha256=source,source_file='pdfs/自动入库/'+source+'.pdf',
        page_count=len(rows),metadata=meta,pages=rows,toc=toc,issues=issues,**evidence,
        audit=dict(release_reviewed=False,source_pdf=Path(pdf).name,folio_review=sorted(set(review)),
            toc_pages=toc_pages,body_range=[main['start'],main['end']],segments=segments))
