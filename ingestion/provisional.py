"""Build a usable release without misrepresenting pending text as proofread."""
from __future__ import annotations

import hashlib
import re


class NotReady(ValueError):
    pass


def make_package(book, metadata, toc, page_map, corrections=()):
    """Pure transformation; never changes ingestion checked/stage fields."""
    rows = book['page_rows']
    count = book['pages']
    if [r['page'] for r in rows] != list(range(1, count + 1)):
        raise NotReady('原始PDF页序不完整或有重复')
    if not all(metadata.get(k) for k in ('title', 'publisher', 'year')):
        raise NotReady('书名、出版社、出版年份尚未确认')
    if not toc or not all(t.get('title') and 1 <= int(t.get('pdf_page') or 0) <= count for t in toc):
        raise NotReady('目录条目或篇章落点未确认')
    if any(a['pdf_page'] > b['pdf_page'] for a, b in zip(toc, toc[1:])):
        raise NotReady('目录落点逆序')
    mapping = {p['pdf_page']: p for p in page_map}
    if set(mapping) != set(range(1, count + 1)):
        raise NotReady('逐页定位记录不完整')
    patches = {}
    for correction in corrections:
        if not correction.get('image_reviewed') or not correction.get('reason'):
            raise NotReady('修订缺少原图核对记录')
        patches.setdefault(correction['pdf_page'], []).append(correction)
    pages = []
    for row in rows:
        position = mapping[row['page']]
        if position['basis'] == 'needs_review' or position.get('issues'):
            raise NotReady(f"PDF第{row['page']}页的页码定位仍待处理")
        text = row['text']
        provenance = 'primary_recognition'
        if position['basis'] in ('blank_image_and_ocr','reviewed_blank_image_and_ocr'):
            ocr = row.get('ocr', {})
            image_confirmed = ocr.get('ink') == 0 or (position['basis'] == 'reviewed_blank_image_and_ocr' and position.get('evidence_image'))
            if row.get('ocr_status') != 'done' or not image_confirmed or ocr.get('text', '').strip():
                raise NotReady(f"PDF第{row['page']}页缺少真空白证据")
            text, provenance = '', 'confirmed_blank_image_and_ocr'
        # Only explicit failed transcriptions use the already completed OCR.
        # Disagreement on ordinary wording does not cause silent replacement.
        failure = re.search(r"I'm not sure what you're asking|could you please provide more context|作为.*AI.*无法|抱歉.{0,15}无法.{0,10}(识别|提供)", text, re.I)
        if failure or (not text.strip() and row.get('ocr', {}).get('text', '').strip()):
            if row.get('ocr_status') != 'done' or not row.get('ocr', {}).get('text', '').strip():
                raise NotReady(f"PDF第{row['page']}页没有可用转录")
            text = row['ocr']['text']
            provenance = 'free_ocr_fallback'
        applied = []
        for correction in patches.get(row['page'], []):
            before = correction['before']
            if text.count(before) != 1:
                raise NotReady(f"PDF第{row['page']}页修订基线不匹配")
            text = text.replace(before, correction['after'], 1)
            applied.append(correction)
        pages.append({'page': row['page'], 'text': text, 'label': str(position['printed_page']) if position['printed_page'] is not None else '',
                      'kind': position.get('kind', row['kind']), 'checked': row['checked'], 'repairs': row['repairs'],
                      'text_provenance': provenance, 'text_review_pending': not bool(row['checked']),
                      'mapping_basis': position['basis'], 'mapping_evidence': position,
                      'original_text_sha256': hashlib.sha256(row['text'].encode()).hexdigest(), 'release_corrections': applied})
    return {'schema': 1, 'book_id': book['id'], 'source_sha256': book['sha'],
            'source_file': 'pdfs/自动入库/' + book['sha'] + '.pdf', 'page_count': count,
            'metadata': metadata, 'toc': toc, 'pages': pages,
            'release_quality': 'provisional_text', 'quality_note': '新入库文献，识别文本持续校对，请以PDF原文为准。',
            'pending_text_pages': sum(p['text_review_pending'] for p in pages)}
