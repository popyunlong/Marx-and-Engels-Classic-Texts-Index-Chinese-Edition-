"""Deterministic, evidence-preserving import of the 2026-10 state documents.

Original PDF/JSON files are never modified. Audit output is not release approval.
"""
from __future__ import annotations
import collections
import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

FURNITURE = {'header','footer','number','page_number','header_image','footer_image'}
TITLES = {'doc_title','paragraph_title','title','heading'}


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''): h.update(chunk)
    return h.hexdigest()


def clean(text):
    text=re.sub(r'\$[^$]*\$', '', text)
    text=re.sub(r'<[^>]+>', '', text)
    return re.sub(r'[#*_]', '', unicodedata.normalize('NFKC',text)).strip()


def key(text):
    return re.sub(r'[^\w]', '', clean(text))


def first_heading(row):
    return next((clean(b['text']) for b in row['blocks'] if b['kind'] in TITLES), '')


def title_author(raw, row=None):
    pieces=re.split(r'[.．…·]{2,}',raw)
    title=pieces[0].strip();author=pieces[-1].strip() if len(pieces)>1 else ''
    if row:
        heading=first_heading(row)
        # A separately printed author is not part of the article title.
        candidates=[clean(b['text']) for b in row['blocks'] if b['kind']=='text'
                    and re.fullmatch(r'[\u4e00-\u9fff\s]{2,12}',clean(b['text']))]
        for name in candidates:
            if title.endswith(name) and key(title[:-len(name)])==key(heading):
                title=title[:-len(name)].strip();author=name;break
        if key(title).startswith(key(heading)) and key(heading):
            remainder=key(title)[len(key(heading)):]
            if re.fullmatch(r'[\u4e00-\u9fff]{2,6}',remainder):
                title=heading;author=remainder
    return title,author


def read_parts(pdf):
    """Page-range names are numeric and absolute, never printed-page labels."""
    pdf=Path(pdf);parts=[]
    whole=pdf.with_name(pdf.name+'_by_PaddleOCR-VL-1.6.json')
    if whole.exists(): parts=[(1,None,whole)]
    split=[]
    for p in pdf.parent.glob('*.json'):
        m=re.fullmatch(re.escape(pdf.stem)+r'_(\d+)-(\d+)\.pdf_by_PaddleOCR-VL-1\.6\.json',p.name)
        if m: split.append((int(m[1]),int(m[2]),p))
    if parts and split: raise ValueError('whole and split JSON both supplied')
    parts=parts or sorted(split)
    if not parts: raise ValueError('JSON missing: '+pdf.name)
    merged=[];manifest=[]
    for start,end,path in parts:
        pages=json.loads(path.read_text('utf-8-sig'))
        if not isinstance(pages,list) or start!=len(merged)+1 or (end is not None and len(pages)!=end-start+1):
            raise ValueError('JSON range gap/overlap/length mismatch: '+path.name)
        manifest.append(dict(file=path.name,sha256=digest(path),start=start,end=start+len(pages)-1))
        for index,page in enumerate(pages):
            result=page.get('prunedResult',page)
            blocks=result.get('parsing_res_list')
            if not isinstance(blocks,list): raise ValueError('structured blocks missing')
            merged.append(dict(page=start+index,source_part=path.name,source_part_page=index+1,
                blocks=[dict(kind=b['block_label'],text=b.get('block_content',''),
                    order=b.get('block_order',i),bbox=b.get('block_bbox')) for i,b in enumerate(blocks)]))
    return merged,manifest


def audit(pdf):
    import fitz
    pdf=Path(pdf);rows,parts=read_parts(pdf)
    with fitz.open(pdf) as doc:
        if len(doc)!=len(rows): raise ValueError('PDF/JSON physical page count mismatch')
    offsets=collections.Counter()
    for row in rows:
        labels=[clean(b['text']) for b in row['blocks'] if b['kind'] in {'number','page_number'}]
        row['observed_labels']=labels
        for label in labels:
            if label.isdigit(): offsets[row['page']-int(label)]+=1
    offset,votes=offsets.most_common(1)[0]
    anchors=[r for r in rows if str(r['page']-offset) in r['observed_labels']]
    start,end=anchors[0]['page'],anchors[-1]['page']
    issues=[];corrections=[]
    for row in rows:
        p=row['page'];expected=str(p-offset)
        row['text']='\n'.join(b['text'] for b in row['blocks'] if b['kind'] not in FURNITURE)
        row['label']='';row['kind']='front' if p<start else 'back';row['segment_id']='unnumbered'
        if start<=p<=end:
            row.update(label=expected,kind='body',segment_id='body')
            observed=expected in row['observed_labels']
            method='ocr_number_with_sequence' if observed else 'needs_review'
            row['label_evidence']=dict(method=method,observed=row['observed_labels'],offset=offset)
            if not observed:
                corrections.append(dict(page=p,expected=expected,observed=row['observed_labels']))
        row['notes']=[b for b in row['blocks'] if b['kind']=='footnote']
    toc_pages=[r['page'] for r in rows[:start-1] if any(key(b['text'])=='目录' for b in r['blocks'])]
    # Include intervening contents pages. Front matter before the first contents
    # heading is excluded; late document-internal contents never become book TOC.
    toc_range=list(range(min(toc_pages),start)) if toc_pages else []
    toc_text='\n'.join('\n'.join(b['text'] for b in rows[p-1]['blocks']
        if b['kind'] not in FURNITURE and key(b['text']) not in {'目录'}) for p in toc_range)
    toc_text=re.sub(r'[（(][^()（）]*年[^()（）]*(?:日|通过|批准|印发)[^()（）]*[)）]','',toc_text)
    toc_text=re.sub(r'^[# \t]*\d{4}年\d{1,2}月\d{1,2}日[ \t]*$', '', toc_text, flags=re.M)
    unpaged=[]
    def attachment(m):
        if re.search(r'[（(]\d+[)）]',m[0]):return m[0]
        unpaged.append(clean(m[0]));return ''
    toc_text=re.sub(r'^\s*附[一二三四]?[：:][^\n]*$',attachment,toc_text,flags=re.M)
    # Notes below a dated article are context, not the next article's title.
    toc_text=re.sub(r'^[ \t]*(?:——中共中央纪律检查委员会[^\n]*|中共十二届二中全会通过)[ \t]*$', '',toc_text,flags=re.M)
    toc=[];previous=0
    for m in re.finditer(r'[（(]\s*(\d{1,4})\s*[)）]',toc_text):
        raw=clean(toc_text[previous:m.start()]);previous=m.end()
        raw=re.sub(r'^\([^)]*(?:年|通过)[^)]*\)\s*','',raw)
        raw=re.sub(r'^[.…·]+[\u4e00-\u9fff]{2,4}\s*\n', '',raw)
        title,author=title_author(raw)
        label=int(m[1]);p=label+offset
        if not start<=p<=end:
            issues.append(dict(kind='toc_outside_pdf',title=title,printed_page=label,pdf_page=p));continue
        row=rows[p-1];title,author=title_author(raw,row);heading=first_heading(row)
        title_key=key(title);heading_key=key(heading)
        exact=bool(title_key) and title_key in heading_key
        if not exact:
            issues.append(dict(kind='toc_title',title=title,heading=heading,pdf_page=p,
                similarity=round(SequenceMatcher(None,title_key,heading_key).ratio(),3)))
        toc.append(dict(title=title,author=author,pdf_page=p,printed_page=str(label),level=1,
            kind='body',sort_order=len(toc),paper_title=raw,title_exact=exact))
    for raw in unpaged:
        title,author=title_author(raw)
        name=re.sub(r'^附[一二三四]?[：:]','',title).strip()
        matches=[r for r in rows[start-1:end] if key(name) in key(first_heading(r))]
        if len(matches)==1:
            row=matches[0]
            toc.append(dict(title=title,author=author,pdf_page=row['page'],printed_page=row['label'],
                level=2,kind='body',sort_order=0,paper_title=raw,title_exact=True,
                evidence_method='paper_attachment_and_body_heading'))
        else:issues.append(dict(kind='unpaged_attachment',title=raw,matches=[r['page'] for r in matches]))
    toc.sort(key=lambda e:e['pdf_page'])
    if not toc: issues.append(dict(kind='no_paper_toc'))
    for i,entry in enumerate(toc):
        entry['sort_order']=i
        entry['end_pdf_page']=toc[i+1]['pdf_page']-1 if i+1<len(toc) else end
        if entry['end_pdf_page']<entry['pdf_page']: issues.append(dict(kind='toc_order',entry=entry))
    return dict(schema=1,pdf=pdf.name,source_sha256=digest(pdf),page_count=len(rows),parts=parts,
        body_start=start,body_end=end,offset=offset,offset_votes=votes,toc_pages=toc_range,
        pages=rows,toc=toc,folio_review=corrections,issues=issues)


def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True);summary=[]
    for pdf in sorted(a.source.glob('*.pdf')):
        report=audit(pdf);target=a.output/(report['source_sha256'][:16]+'.json')
        target.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
        item={k:report[k] for k in ('pdf','source_sha256','page_count','body_start','body_end','offset','toc_pages')}
        item.update(toc_count=len(report['toc']),folio_review=len(report['folio_review']),issues=len(report['issues']),audit=target.name)
        summary.append(item);print(json.dumps(item,ensure_ascii=False),flush=True)
    (a.output/'inventory.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__': main()
