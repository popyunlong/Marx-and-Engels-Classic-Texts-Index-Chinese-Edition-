"""Evidence-preserving printed-page mapping, separate from body-text approval."""
from __future__ import annotations

import re
import unicodedata
from .page_tokens import parse_label


def number(text, maximum=2000):
    value = unicodedata.normalize('NFKC', str(text)).strip()
    value = re.sub(r'^[\s\-—–·•]+|[\s\-—–·•]+$', '', value)
    if re.fullmatch(r'[0-9]{1,4}', value) and 0 < int(value) <= maximum:
        return int(value)
    return None



def folio(text, maximum=2000):
    parsed = parse_label(str(text).strip(' -—–·•'))
    if not parsed or parsed[2] > maximum:
        return None
    return parsed[2] if parsed[1] == 'arabic' else parsed[0]


def _folio_value(value):
    parsed = parse_label(value)
    return parsed[1], parsed[2]


def _format_ordinal(system, value):
    if system == 'arabic': return value
    label = ''
    for n, letters in [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),(50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]:
        count, value = divmod(value, n)
        label += letters*count
    return 'pre-' + label.lower()

def ocr_candidates(result, maximum):
    height = result.get('height', 0)
    width = result.get('width', 0)
    found = []
    if not width or not height:
        return found
    for line in result.get('lines', []):
        value = folio(line.get('text'), maximum)
        box = line.get('box', [])
        if len(box) != 4:
            continue
        x = sum(p[0] for p in box) / 4 / width
        y = sum(p[1] for p in box) / 4 / height
        joined_header = False
        if value is None and y < .15:
            text = unicodedata.normalize('NFKC', line.get('text', '')).strip()
            if len(re.findall(r'[\u4e00-\u9fff]', text)) >= 4:
                match = re.match(r'^(\d{1,4})\s+\D', text) or re.search(r'\D\s+(\d{1,4})$', text)
                if match:
                    value = number(match.group(1), maximum)
                    joined_header = value is not None
        if value is None:
            continue
        # Top folios are in outer corners; bottom folios may be centred.
        if joined_header or (y < .22 and (x < .33 or x > .67)) or y > .82:
            found.append({'label': value, 'score': line.get('score', 0), 'box': box,
                          'joined_header': joined_header})
    return found


def map_pages(rows, native_by_page=None, toc_anchors=None):
    native_by_page = native_by_page or {}
    toc_anchors = toc_anchors or {}
    result = []
    maximum = max(200, len(rows) * 2)
    for row in rows:
        pdf_page = row['page']
        evidence = ocr_candidates(row.get('ocr', {}), maximum)
        ocr = {e['label'] for e in evidence if e['score'] >= .90}
        native = {folio(x, maximum) for x in native_by_page.get(pdf_page, [])}
        native.discard(None)
        common = ocr & native
        label, basis = None, 'needs_review'
        if len(common) == 1:
            label, basis = next(iter(common)), 'native_and_ocr'
        elif len(ocr) == 1 and not native:
            label, basis = next(iter(ocr)), 'ocr_margin'
        elif len(native) == 1 and not ocr:
            label, basis = next(iter(native)), 'native_margin'
        issues = []
        if ocr and native and not common:
            issues.append('native_ocr_conflict')
        if pdf_page in toc_anchors:
            expected = folio(toc_anchors[pdf_page], maximum)
            if expected is None: raise ValueError('Invalid verified TOC folio')
            if label is not None and label != expected:
                issues.append('toc_label_conflict')
                label, basis = None, 'needs_review'
            elif not issues:
                label, basis = expected, 'verified_toc_landing'
        result.append({'pdf_page': pdf_page, 'printed_page': label, 'basis': basis,
                       'kind': row['kind'], 'ocr_candidates': evidence,
                       'native_candidates': sorted(native, key=str), 'previous_label': row.get('label'),
                       'issues': issues})
    # A lone I/l at the margin may be the digit 1. Roman labels require a
    # consistent observed Roman neighbour, or an explicitly reviewed TOC anchor.
    roman_unverified = []
    for index, row in enumerate(result):
        parsed = parse_label(row['printed_page'])
        if not parsed or parsed[1] != 'roman' or row['basis'] == 'verified_toc_landing':
            continue
        support = []
        for other in result[max(0,index-8):index+9]:
            anchor = parse_label(other['printed_page'])
            distance = other['pdf_page']-row['pdf_page']
            if anchor and anchor[1]=='roman' and 0 < abs(distance) <= 8 and other['kind']==row['kind'] and anchor[2]-parsed[2]==distance:
                support.append(other['pdf_page'])
        if support:
            row['anchor_pdf_pages'] = support
        else:
            roman_unverified.append(row)
    for row in roman_unverified:
        row.update(printed_page=None,basis='needs_review')
        row['issues'].append('roman_sequence_unverified')
    # Only interpolate between consistent observed anchors. Never extrapolate
    # a book-wide offset over preliminary matter or a numbering reset.
    anchors = [i for i, r in enumerate(result) if r['printed_page'] is not None]
    for left, right in zip(anchors, anchors[1:]):
        system, value = _folio_value(result[left]['printed_page'])
        other_system, other_value = _folio_value(result[right]['printed_page'])
        distance = result[right]['pdf_page'] - result[left]['pdf_page']
        if system != other_system or other_value - value != distance or distance != right-left or distance > 8:
            continue
        kinds = {r['kind'] for r in result[left:right+1]}
        if len(kinds) != 1 or kinds & {'blank', 'cover', 'copyright'}:
            continue
        for index in range(left + 1, right):
            row = result[index]
            if row['issues']:
                continue
            row['printed_page'] = _format_ordinal(system, value + row['pdf_page'] - result[left]['pdf_page'])
            row['basis'] = 'interpolated_between_consistent_anchors'
            row['anchor_pdf_pages'] = [result[left]['pdf_page'], result[right]['pdf_page']]
    for row in result:
        if row['printed_page'] is None and row['kind'] in ('blank', 'cover', 'copyright') and not row['issues']:
            row['basis'] = 'unnumbered_front_or_blank'
    return result
