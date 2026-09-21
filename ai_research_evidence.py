"""Deterministic, work-bounded research selection and coverage diagnostics."""
from __future__ import annotations
import re
import time
from types import SimpleNamespace
from ai_evidence import exact_quote


def requested_quotes(question):
    from ai import _inline_quotations
    return list(dict.fromkeys(m.group('quote') for m in _inline_quotations(question)
                             if len(re.sub(r'\W', '', m.group('quote'))) >= 8))[:8]


def body(A, hit):
    base = hit.to_dict()
    if getattr(hit, 'document_id', ''):
        raw, _ = A.corpus.document_text_window(hit, adjacent_pages=0)
    else:
        raw = '\n'.join(p.raw_text for p in getattr(hit, 'pages', [])) or A._plain_hit_context(base)
    return A._clean_ai_source_window(raw, base, preserve_lines=True)[0]


def rank(A, hits, question, keywords, facets, titles, anchors):
    """Reuse topic admission, then keep exact anchors and diversify tied topics."""
    facets = facets or []
    texts = {id(h): body(A, h) for h in hits}
    proxies = [SimpleNamespace(context=texts[id(h)], hit=h) for h in hits]
    admitted = A._rank_explicit_document_candidates(proxies, question, keywords, titles)
    allowed = {id(p.hit) for p in admitted}
    terms = A._explicit_document_topic_terms(question, keywords, titles)
    pending = []
    for position, h in enumerate(hits):
        text = texts[id(h)]
        norm = A.normalize(text)
        exact = sum(bool(exact_quote(q, text)) for q in anchors)
        if id(h) not in allowed and not exact:
            continue
        topic = sum(A.normalize(t) in norm for t in terms)
        covered = {i for i, words in enumerate(facets) if any(A.normalize(w) in norm for w in words if len(A.normalize(w)) >= 2)}
        scope = A.corpus.document_scope_for_offset(h.source_file, h.norm_start)
        chapter = scope.document_id if scope else (h.source_file, h.pages[0].pdf_page)
        pending.append((h, exact, topic, covered, chapter, position))
    selected, seen_facets, seen_chapters = [], set(), set()
    while pending:
        best = max(pending, key=lambda r: (r[1], r[2], len(r[3] - seen_facets),
                                           r[4] not in seen_chapters, -r[5]))
        pending.remove(best)
        selected.append(best[0]); seen_facets.update(best[3]); seen_chapters.add(best[4])
    return selected


def anchor_window(raw, anchors, limit):
    """Keep a requested exact phrase in a sentence-bounded original window."""
    for q in anchors:
        matched = exact_quote(q, raw)
        if not matched:
            continue
        start = raw.find(matched)
        if start < 0:
            continue
        end = start + len(matched)
        left = max(raw.rfind(p, 0, start) for p in ('。', '！', '？')) + 1
        stops = [raw.find(p, end) for p in ('。', '！', '？')]
        right = min((i + 1 for i in stops if i >= 0), default=len(raw))
        value = raw[left:right].strip()
        # A very long source sentence remains one exact fragment, never a
        # fabricated continuation or an ellipsis-joined passage.
        return value if len(value) <= limit else matched
    return ''


def coverage(A, candidates, passages, question, keywords, facets, titles):
    anchors = requested_quotes(question)
    source_texts = [body(A, h) for h in candidates] if anchors else []
    selected = [p['text'] for p in passages]
    quote_states = []
    for q in anchors:
        included = any(exact_quote(q, t) for t in selected)
        recalled = included or any(exact_quote(q, t) for t in source_texts)
        quote_states.append({'text': q, 'status': 'included' if included else 'not_selected' if recalled else 'not_retrieved'})
    terms = A._explicit_document_topic_terms(question, keywords, titles)
    # Only dimensions actually present in the user's question are coverage
    # checks. Model expansion words never manufacture mandatory topics.
    dimensions = [list(dict.fromkeys(t for t in words if t in terms)) for words in (facets or [])]
    dimensions = [d for d in dimensions if d] or [[t] for t in terms]
    combined = A.normalize('\n'.join(selected))
    missing = [d for d in dimensions if not any(A.normalize(t) in combined for t in d)]
    return {'status': 'limited' if missing or any(q['status'] != 'included' for q in quote_states) else 'covered',
            'quotes': quote_states, 'missing_dimensions': missing,
            'basis': 'text_presence_only', 'supplement_attempted': False}


def supplement(A, scopes, report, question, keywords, facets):
    deadline = time.monotonic() + 15
    quotes = [q['text'] for q in report['quotes'] if q['status'] != 'included']
    terms = list(dict.fromkeys(t for d in report['missing_dimensions'] for t in d))
    try:
        hits = A.corpus.locate_associative_in_documents(scopes, quotes=quotes,
            fragments=quotes + terms, keywords=terms or keywords, facets=facets,
            deadline=deadline)
    except Exception as exc:
        A.LOGGER.warning('Bounded research supplement failed: %s', type(exc).__name__)
        return [], False
    return hits, time.monotonic() >= deadline


def warning(report):
    missing = [q['text'] for q in report['quotes'] if q['status'] != 'included']
    dimensions = ['、'.join(d) for d in report['missing_dimensions']]
    parts = []
    if missing:
        parts.append('尚未取得可用于本次回答的原文：' + '；'.join('“'+q+'”' for q in missing))
    if dimensions:
        parts.append('以下方面的证据覆盖仍有限：' + '、'.join(dimensions))
    return '。'.join(parts) + '。正文依据现有材料展开，未补造缺失出处。' if parts else ''


def final_coverage(report, cards):
    result = dict(report)
    quotes = []
    for row in report['quotes']:
        item = dict(row)
        evidence = [e for card in cards for e in card.get('evidence', [])
                    if e.get('kind') == 'quote' and exact_quote(row['text'], e.get('quote', ''))]
        if evidence:
            item['text_verified'] = any(e.get('text_verified') or e.get('location_status') == 'verified' for e in evidence)
            item['location_status'] = 'verified' if any(e.get('location_status') == 'verified' for e in evidence) else 'unresolved'
        quotes.append(item)
    result['quotes'] = quotes
    return result
