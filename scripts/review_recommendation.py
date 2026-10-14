"""Finalize an operator's original-page review; never publish from this command."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.recommendations import root, sha, write_json
import personal_library as library
import recommendations


def finalize(rid, review, actor):
    row = library.get_book_recommendation(rid)
    if not row or review.get('source_sha256') != row['sha256']:
        raise ValueError('Review source differs from recommendation')
    directory = root() / row['sha256']
    if sha(directory / 'source.pdf') != row['sha256']:
        raise ValueError('Original PDF changed')
    pages = [json.loads((directory / f'page-{i:05}.json').read_text('utf8'))
             for i in range(1, row['page_count']+1)]
    if any(p['page'] != i or p['source_sha256'] != row['sha256'] for i,p in enumerate(pages,1)):
        raise ValueError('Page sequence or source changed')
    meta = review['metadata']
    for field in ('title','authors','publisher','year','place','edition','recommender_name','recommender_email_masked'):
        if not meta.get(field):
            raise ValueError('Missing reviewed bibliography: ' + field)
    toc = review['toc']
    evidence = set(review.get('reviewed_pdf_pages', []))
    if any(not isinstance(p, int) or not 1 <= p <= len(pages) for p in evidence):
        raise ValueError('Review evidence page is outside original PDF')
    samples = review.get('body_samples', [])
    if len(samples) < 3 or len(set(samples)) < 3 or not set(samples) <= evidence:
        raise ValueError('First/middle/last body sample evidence missing')
    if not toc or any(not 1 <= int(t['pdf_page']) <= len(pages) or t['pdf_page'] not in evidence for t in toc):
        raise ValueError('Every TOC target requires original-page review')
    if not set(review.get('bibliography_pages', [])) <= evidence or not review.get('bibliography_pages'):
        raise ValueError('Copyright-page evidence missing')
    if any(int(toc[i]['pdf_page']) > int(toc[i+1]['pdf_page']) for i in range(len(toc)-1)):
        raise ValueError('TOC order is invalid')
    for i,t in enumerate(toc):
        if not str(t.get('title','')).strip():raise ValueError('Empty chapter title')
        t.update(sort_order=i, kind='chapter', level=int(t.get('level',1)), printed_page=str(t.get('printed_page','')))
    meta = dict(meta, book_key='user_rec_'+row['sha256'][:16], citation_title=meta['title'],
                display_title=meta['title'], collection='user_recommended', volume=1, single_volume=True,
                recommendation_id=rid, public_window_days=0, translators=meta.get('translators',[]), editors=[])
    # Unknown printed folios remain empty. Reviews can supply explicit verified mappings.
    for number,label in review.get('printed_pages',{}).items():
        number=int(number)
        if number not in evidence:raise ValueError('Unreviewed printed page')
        pages[number-1]['label']=str(label)
        pages[number-1]['label_evidence']={'method':'image_review','reviewer':str(actor)}
    for number,text in review.get('text_corrections',{}).items():
        number=int(number)
        if number not in evidence:raise ValueError('Unreviewed text correction')
        pages[number-1]['text']=str(text)
    sections={'目录':('front','contents'),'版权页':('front','copyright'),
              '封面':('front','cover'),'原件空白页':('blank','blank')}
    for number,title in review.get('auxiliary_pages',{}).items():
        number=int(number)
        if number not in evidence or title not in sections:
            raise ValueError('Unreviewed auxiliary page')
        kind,segment=sections[title]
        pages[number-1].update(kind=kind,segment_id=segment,segment_title=title,
            label_evidence={'method':'image_review','reviewer':str(actor),
                            'note':'原页类型已核对；未确认的印刷页码保持空白'})
    if any(not pages[p-1]['text'].strip() for p in samples):
        raise ValueError('Body sample text is empty')
    payload = json.dumps(pages, ensure_ascii=False, sort_keys=True).encode()
    package = dict(schema=1,book_id=row['sha256'][:32],source_sha256=row['sha256'],
                   source_file='pdfs/自动入库/'+row['sha256']+'.pdf',page_count=len(pages),metadata=meta,
                   pages=pages,toc=toc,issues=[],quality_note=review.get('quality_note') or '识别文本持续校对，请以 PDF 原文为准。',
                   source_evidence=dict(schema=1,kind='recommendation_pdf',pdf_sha256=row['sha256'],
                                        pages_sha256=hashlib.sha256(payload).hexdigest()),
                   audit=dict(release_reviewed=True,reviewer=str(actor),reviewed_pdf_pages=sorted(evidence),
                              body_samples=samples,bibliography_pages=review['bibliography_pages']))
    from ingestion.paddle_catalog import register
    register(package, [], {}, {})
    target=directory/'reviewed-package.json'
    write_json(target,package)
    recommendations.mark_ready(row['sha256'],sha(target),meta['book_key'],actor)
    return target


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--id',type=int,required=True)
    p.add_argument('--review',type=Path,required=True);p.add_argument('--reviewer',required=True)
    a=p.parse_args()
    print(finalize(a.id,json.loads(a.review.read_text('utf8')),a.reviewer))
