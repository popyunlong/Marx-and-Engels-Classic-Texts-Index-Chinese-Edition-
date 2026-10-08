"""Exact text offsets; no PDF geometry, fuzzy spans or OCR calls."""
import unicodedata


def normalize_with_map(raw):
    text, positions = [], []
    for index, char in enumerate(raw):
        for value in unicodedata.normalize('NFKC', char).casefold():
            if not value.isspace():
                text.append(value)
                positions.append(index)
    return ''.join(text), positions


def locate(raw, terms):
    if isinstance(terms, str):
        terms = [terms]
    normalized, mapping = normalize_with_map(raw)
    spans, missing = [], []
    for term in dict.fromkeys(terms):
        needle, _ = normalize_with_map(str(term))
        if not needle:
            continue
        offset, found = 0, False
        while True:
            start = normalized.find(needle, offset)
            if start < 0:
                break
            end = start + len(needle)
            a, b = mapping[start], mapping[end - 1] + 1
            # Reject a match in part of an expanded Unicode character.
            if normalize_with_map(raw[a:b])[0] == needle:
                spans.append((a, b))
                found = True
            offset = start + 1
        if not found:
            missing.append(str(term))
    merged = []
    for a, b in sorted(set(spans)):
        if merged and a < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    # API offsets use UTF-16, as JavaScript strings do; retain Python offsets too.
    return {'mode': 'text', 'spans': [dict(start=a, end=b, text=raw[a:b],
            utf16_start=len(raw[:a].encode('utf-16-le')) // 2,
            utf16_end=len(raw[:b].encode('utf-16-le')) // 2) for a, b in merged],
            'unmatched': missing, 'hint': '未能可靠匹配，请对照原文核对。' if missing else ''}


def locate_pages(pages, quote):
    """Match a quote across real page boundaries, with page-local raw offsets."""
    combined = '\n'.join(p['text'] for p in pages)
    result = locate(combined, [quote])
    output, base = [], 0
    for page in pages:
        spans = []
        for match in result['spans']:
            a, b = max(0, match['start'] - base), min(len(page['text']), match['end'] - base)
            if a < b:
                spans.append(dict(start=a, end=b, text=page['text'][a:b]))
        if spans:
            output.append(dict(page=page['page'], spans=spans))
        base += len(page['text']) + 1
    return dict(pages=output, unmatched=result['unmatched'], hint=result['hint'])
