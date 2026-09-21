"""Bounded literal recovery and conservative, local-only semantic admission."""
from __future__ import annotations

import bisect
import math
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein
from search import Page, normalize, _fuzzy_allowed_errors, _FUZZY_SCAN_SEMAPHORE

_VENDOR = Path(__file__).resolve().parent / 'vendor' / 'textual'
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
import jieba

STOP = frozenset('的 了 是 在 与 和 或 对 把 被 将 使 有 为 于 之 也 就 都 而 及 其 一个 一种 一些 这个 那个 什么 如何 为什么 怎样 关于 根据 依据 描述 论述 句子 原文 内容 文章 观点 思想 理论 问题 作用 关系 影响 意义 人的 是人 是一'.split())
RELATION = re.compile('是|不是|成为|决定|创造|产生|影响|作用|关系|如何|为什么|意味着|发展|矛盾|导致|使|把|论述|描述|让|获得|支配|反映|改变|构成')
HAN = re.compile(r'[\u3400-\u9fff]')
_tokenizer = None
_token_lock = threading.Lock()


def tokenizer(corpus):
    global _tokenizer
    if _tokenizer is None:
        with _token_lock:
            if _tokenizer is None:
                from search import TERM_THESAURUS
                instance = jieba.Tokenizer()
                instance.initialize()
                for group in TERM_THESAURUS:
                    for word in group:
                        instance.add_word(normalize(word))
                # Indexed headings only: never open PDFs to initialize a dictionary.
                for entries in getattr(corpus, '_toc_db_entries', {}).values():
                    for entry in entries:
                        title = corpus._clean_work_title(entry.title)
                        if 4 <= len(title) <= 30:
                            instance.add_word(normalize(title))
                _tokenizer = instance
    return _tokenizer


def content_terms(corpus, query):
    return list(dict.fromkeys(word for word in tokenizer(corpus).cut(normalize(query), HMM=False)
                             if len(word) >= 2 and word not in STOP and HAN.search(word)))


def semantic_admission(corpus, query, *, titles=(), complete=True, reliable_count=0):
    result = {'eligible': False, 'reason': ''}
    def reject(reason):
        return dict(result, reason=reason)
    if not complete:
        return reject('search_incomplete')
    if reliable_count:
        return reject('reliable_original_found')
    text = str(query or '').strip()
    chars = ''.join(HAN.findall(text))
    if len(chars) < 5:
        return reject('too_short')
    if re.search(r'https?://|www\.|\S+@\S+|[{};`\\]|\b(?:SELECT|import|function)\b', text, re.I):
        return reject('non_prose')
    if '\ufffd' in text or len(chars) / max(1, len(re.sub(r'\s', '', text))) < .6:
        return reject('unrecognized_input')
    for width in (1, 2):
        if any(chars.count(chars[i:i + width]) * width / len(chars) >= .7
               for i in range(len(chars) - width + 1)):
            return reject('repetitive_input')
    topical = text
    for title in titles:
        topical = topical.replace(title, '')
    terms = content_terms(corpus, topical)
    if len(terms) < (1 if titles else 2):
        return reject('insufficient_content')
    recognized = set()
    for word in terms + [normalize(t) for t in titles]:
        start = 0
        while word and (pos := normalize(text).find(word, start)) >= 0:
            recognized.update(range(pos, pos + len(word)))
            start = pos + len(word)
    if len(recognized) / len(chars) < .6:
        return reject('low_content_coverage')
    if not titles and not RELATION.search(text):
        return reject('unclear_relation')
    return {'eligible': True, 'reason': 'meaningful_query_without_original'}


@dataclass
class TextualResult:
    hits: list = field(default_factory=list)
    complete: bool = True
    reason: str = ''
    windows: int = 0

    @property
    def reliable_count(self):
        return sum(h.textual_evidence['textual_reliable'] for h in self.hits)


def _seeds(query, errors, terms):
    pieces = max(2, errors + 1)
    seeds = [query[i * len(query) // pieces:(i + 1) * len(query) // pieces] for i in range(pieces)]
    seeds.extend(terms)
    for at in (0, max(0, len(query)//2 - 1), max(0, len(query)-3)):
        seeds.append(query[at:at + 3])
    return list(dict.fromkeys(s for s in seeds if len(s) >= 2))[:40]


def _align(query, window, errors, gap, terms):
    pos = window.find(query)
    if pos >= 0:
        return pos, pos + len(query), 'exact', 0
    if errors:
        alignment = fuzz.partial_ratio_alignment(query, window,
            score_cutoff=max(0, 100 * (1 - (errors + .5) / len(query))))
        if alignment:
            # Check neighboring endpoints too; InDel alignment is a locator,
            # not the authority for substitutions or insertion/deletion counts.
            for radius in range(errors + 2):
                for ds in range(-radius, radius + 1):
                    for de in ({-radius, radius} if abs(ds) < radius else range(-radius, radius + 1)):
                        lo, hi = alignment.dest_start + ds, alignment.dest_end + de
                        if lo < 0 or hi > len(window) or hi <= lo or abs(hi-lo-len(query)) > errors:
                            continue
                        distance = Levenshtein.distance(query, window[lo:hi], score_cutoff=errors)
                        if distance <= errors:
                            return lo, hi, 'edited', distance
    if gap and len(terms) >= 2:
        for start in (m.start() for m in re.finditer(re.escape(query[0]), window)):
            end = start
            for char in query[1:]:
                end = window.find(char, end + 1, min(len(window), start + len(query) + gap))
                if end < 0:
                    break
            if end >= 0 and end + 1 - start - len(query) <= gap:
                span = window[start:end + 1]
                cursor, distinct = 0, set()
                for term in terms:
                    at = span.find(term, cursor)
                    if at >= 0:
                        cursor = at + len(term)
                        distinct.add(term)
                if len(distinct) >= 2:
                    return start, end + 1, 'omission', end + 1 - start - len(query)
    return None


def retrieve_textual(corpus, query, *, book_scope=None, documents=(), seconds=3.0, max_windows=2000):
    started = time.monotonic()
    output = TextualResult()
    if not _FUZZY_SCAN_SEMAPHORE.acquire(timeout=0):
        return TextualResult(complete=False, reason='busy')
    try:
        q = normalize(query)
        if len(q) < 2:
            return output
        terms = content_terms(corpus, query)
        query_counts = list(reversed(Counter(q).items()))
        errors = 1 if 5 <= len(q) <= 9 else _fuzzy_allowed_errors(len(q))
        gap = min(12, math.floor(len(q) * .6)) if len(q) >= 5 else 0
        seeds = _seeds(q, errors, terms) if len(q) >= 5 else [q]
        best, boundaries, raw_maps = {}, {}, {}
        clue_count = 0
        last_yield = time.monotonic()
        def expired():
            nonlocal last_yield
            now = time.monotonic()
            if now - started >= seconds or output.windows >= max_windows:
                output.complete, output.reason = False, 'budget_exhausted'
                return True
            # SQLite/template readers repeatedly release the GIL. A short,
            # time-based pause prevents CPU scans from starving those threads.
            if now - last_yield >= .004:
                time.sleep(.001)
                last_yield = time.monotonic()
            return False

        def missing_characters(window):
            missing = 0
            for char, count in query_counts:
                missing += int(char not in window) if count == 1 else max(0, count-window.count(char))
                if missing > errors:
                    break
            return missing

        def bounds(vol, pos, doc):
            if doc:
                return doc.norm_start, doc.norm_end
            if vol.source_file not in boundaries:
                entries = corpus._title_resolution_entries(vol, allow_pdf_fallback=False)
                points = sorted(set(corpus._entry_offset(vol, e) for e in entries))
                boundaries[vol.source_file] = points
            points = boundaries[vol.source_file]
            if not points:
                pi = vol.page_index_at(pos)
                return vol.page_offsets[pi], vol.page_offsets[pi + 1]
            index = bisect.bisect_right(points, pos)
            return (points[index-1] if index else 0, points[index] if index < len(points) else len(vol.norm_full))

        def add(vol, start, end, kind, difference, lo, hi, doc):
            reliable = kind != 'clue'
            score = 100 if kind == 'exact' else max(1, min(99, round(100 * len(q) / (len(q) + difference)))) if reliable else 40
            changes = [op for op in Levenshtein.opcodes(q, vol.norm_full[start:end]) if op.tag != 'equal'] if reliable else []
            coverage = 1 - sum(op.src_end-op.src_start for op in changes) / len(q) if reliable else 0
            key = (vol.source_file, vol.pages[vol.page_index_at(start)].pdf_page)
            quality = (kind != 'exact', -coverage, -score, corpus.book_sort_order(vol.book), vol.volume, start)
            if key in best and (not reliable, best[key][0][1], *quality) >= best[key][0]:
                return
            hit = corpus._make_hit(vol, start, end, 'exact' if kind == 'exact' else 'fuzzy', score, q,
                                   fuzzy_errors=difference if kind == 'edited' else None)
            if doc:
                hit = corpus._apply_document_scope(hit, doc)
            # Clip context to the proven range, including same-page work edges.
            clipped = []
            for page in hit.pages:
                pi = vol.page_index_at(start) if page == hit.pages[0] else vol.pages.index(page)
                p_lo, p_hi = max(lo, vol.page_offsets[pi]), min(hi, vol.page_offsets[pi+1])
                mapping = raw_maps.get(id(page))
                if mapping is None:
                    mapping = corpus._export_page_raw_map(page, None)
                    if len(raw_maps) >= 8:
                        raw_maps.clear()
                    raw_maps[id(page)] = mapping
                if mapping and p_hi > p_lo:
                    a, b = p_lo-vol.page_offsets[pi], p_hi-vol.page_offsets[pi]
                    raw = page.raw_text[mapping[0][a]:mapping[1][b-1]]
                    clipped.append(Page(page.pdf_page, page.printed_page, raw, normalize(raw), page.id))
            if clipped:
                hit.context = corpus._extract_context(clipped, vol.norm_full[start:end])
            hit.textual_evidence = {'textual_reliable': reliable, 'textual_match_kind': kind,
                                    'textual_difference': difference, 'textual_coverage': coverage if reliable else None,
                                    'textual_differences': [{'type':op.tag,'query':q[op.src_start:op.src_end],
                                        'original':vol.norm_full[start:end][op.dest_start:op.dest_end]} for op in changes]}
            section = str(hit.section_title or '')
            penalty = 1 if re.search('目录|索引|序言|序论|前言|出版说明|卷说明|注释|编者', section) or not hit.pages[0].printed_page else 0
            order = (not reliable, penalty, *quality)
            if key not in best or order < best[key][0]:
                best[key] = (order, hit)

        volumes = [(corpus.get_volume_by_source_file(d.source_file), d) for d in documents] if documents else [
            (vol, None) for book in corpus._scoped_book_keys(book_scope) for vol in corpus._scoped_volumes(book, book_scope)]
        for vol, doc in volumes:
            if vol is None or expired():
                break
            lower, upper = (doc.norm_start, doc.norm_end) if doc else (0, len(vol.norm_full))
            nf = vol.norm_full
            seen = set()
            # Search the rarer anchors first, retaining anchors from the whole query.
            # Estimate rarity on a small sample; don't count every common word
            # across an entire volume before scanning that same text again.
            ranked = sorted((nf.count(seed, lower, min(upper,lower+8192)), seed,
                             nf.find(seed, lower, upper)) for seed in seeds)
            for _, seed, pos in ranked:
                if pos < 0 or expired():
                    continue
                while pos >= 0:
                    if expired():
                        break
                    # Reject impossible windows before resolving chapter offsets:
                    # TOC normalization across the full library is expensive.
                    ws, we = max(lower, pos-len(q)-gap-errors), min(upper, pos+len(seed)+len(q)+gap+errors)
                    # Close seed occurrences usually cover the same candidate.
                    key = (ws, we)
                    if key not in seen:
                        seen.add(key)
                        window = nf[ws:we]
                        present = [t for t in terms if t in window] if clue_count < 100 else []
                        missing = missing_characters(window)
                        possible = missing <= errors
                        weak = clue_count < 100 and len(present) >= 2 and sum(len(t) for t in present) >= len(q)*.4
                        if not possible and not weak:
                            pos = nf.find(seed, pos + len(seed), upper)
                            continue
                        lo, hi = bounds(vol, pos, doc)
                        lo, hi = max(lo, lower), min(hi, upper)
                        ws, we = max(ws, lo), min(we, hi)
                        window = nf[ws:we]
                        present = [t for t in terms if t in window]
                        missing = missing_characters(window)
                        if missing <= errors or (gap and missing == 0 and len(present) >= 2):
                            output.windows += 1
                            match = _align(q, window, errors, gap, terms)
                            if match:
                                a,b,kind,diff = match
                                add(vol,ws+a,ws+b,kind,diff,lo,hi,doc)
                        else:
                            match = None
                        if not match and clue_count < 100 and len(present) >= 2 and sum(len(t) for t in present) >= len(q)*.4:
                            a = window.find(present[0])
                            add(vol,ws+a,ws+a+len(present[0]),'clue',0,lo,hi,doc)
                            clue_count += 1
                    pos = nf.find(seed, pos + len(seed), upper)
        output.hits = [item[1] for item in sorted(best.values(), key=lambda item:item[0])][:300]
        return output
    finally:
        _FUZZY_SCAN_SEMAPHORE.release()
