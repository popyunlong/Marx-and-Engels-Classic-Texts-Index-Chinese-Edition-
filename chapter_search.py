"""Versioned, explicit chapter ranges for browser search and its export worker.

No global corpus mutation: every request resolves its own immutable selection.
The original offsets are retained for citations, layout evidence and reader links.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from itertools import islice
import time

from build_index import normalize
from search import DocumentScope
from search_capacity import bounded_search


class ChapterScopeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def has_chapters(tokens):
    values = tokens if isinstance(tokens, (list, tuple)) else [tokens]
    return any(isinstance(t, str) and t.startswith("chapter:") for t in values)


def _heading_offset(corpus, vol, entry):
    """Unlike legacy attribution, selection must never fall back to page start."""
    indices = [i for i, page in enumerate(vol.pages) if page.pdf_page == entry.pdf_page]
    if len(indices) != 1:
        return None
    i = indices[0]
    lo, hi = vol.page_offsets[i:i + 2]
    title = normalize(entry.title)
    probes = [title] if len(title) >= 2 else []
    probes += [title[:size] for size in (32, 24, 16, 12) if len(title) > size]
    for probe in probes:
        pos = vol.norm_full.find(probe, lo, hi)
        if pos >= 0 and vol.norm_full.find(probe, pos + 1, hi) < 0:
            return pos
    return None


def chapter_rows(corpus, vol):
    entries = sorted(corpus.get_toc_entries(vol.source_file),
                     key=lambda e: (e.pdf_page, e.sort_order, e.level))
    offsets = [_heading_offset(corpus, vol, entry) for entry in entries]
    rows = []
    for index, entry in enumerate(entries):
        end = len(vol.norm_full)
        for j in range(index + 1, len(entries)):
            later = entries[j]
            independent_letter = later.kind == "letter" and not corpus._looks_like_subordinate_toc_title(later.title)
            if independent_letter or int(later.level or 1) <= int(entry.level or 1):
                end = offsets[j]
                break
        start = offsets[index]
        enabled = (entry.kind in {"body", "letter"} and start is not None
                   and end is not None and start < end)
        scope = None
        if enabled:
            scope = DocumentScope(vol.book, vol.volume, vol.source_file,
                                  corpus._clean_work_title(entry.title), entry.pdf_page, start, end,
                                  authors=entry.authors, provenance_verified=entry.provenance_verified,
                                  date=entry.date, end_pdf_page=entry.end_pdf_page,
                                  evidence_method=entry.evidence_method)
        rows.append({"title": entry.title, "level": max(1, int(entry.level or 1)),
                     "pdf_page": entry.pdf_page, "printed_page": entry.printed_page or "",
                     "token": "chapter:" + scope.document_id if scope else None,
                     "selectable": bool(scope), "reason": "" if scope else "正文起止尚未确认，暂不可选",
                     "scope": scope})
    return rows


@dataclass(frozen=True)
class ChapterSelection:
    tokens: tuple[str, ...]
    version: str
    documents: tuple[DocumentScope, ...]
    book_filter: str = ""

    @property
    def book_scope(self):
        result = {}
        for doc in self.documents:
            result.setdefault(doc.book, set()).add(doc.volume)
        return result

    @property
    def label(self):
        return "、".join(f"{d.book}第{d.volume}卷《{d.title}》" for d in self.documents)

    def snapshot(self):
        result = {"tokens": list(self.tokens), "version": self.version}
        if self.book_filter:
            result['book_filter'] = self.book_filter
        return result

    def for_book(self, book):
        if not book:
            return self
        documents = tuple(d for d in self.documents if d.book == book)
        if not documents:
            raise ChapterScopeError('书库筛选不在所选篇章范围内。')
        return ChapterSelection(self.tokens, self.version, documents, book)


def resolve(corpus, tokens, version, current_version, public_books):
    if not isinstance(tokens, list) or not 1 <= len(tokens) <= 500 or not all(isinstance(t, str) for t in tokens):
        raise ChapterScopeError("检索范围参数无效。")
    if not version or version != current_version:
        raise ChapterScopeError("篇章范围版本已变化，请重新选择篇章。", 409)
    public = set(public_books)
    selected, volumes, row_cache = [], {}, {}
    for token in dict.fromkeys(tokens):
        if token.startswith("chapter:"):
            identity = token[len("chapter:"):]
            source, sep, _ = identity.rpartition("#toc-")
            vol = corpus.get_volume_by_source_file(source) if sep else None
            if vol is None or vol.book not in public:
                raise ChapterScopeError("所选篇章当前不可访问，请重新选择。")
            if source not in row_cache:
                row_cache[source] = {r["token"]: r["scope"] for r in chapter_rows(corpus, vol) if r["selectable"]}
            doc = row_cache[source].get(token)
            if doc is None:
                raise ChapterScopeError("所选篇章不存在或正文边界尚未确认。")
            selected.append(doc)
        elif token.startswith(("book:", "vol:")):
            book, number = token[5:], None
            if token.startswith("vol:"):
                book, _, raw = token[4:].rpartition(":")
                if not raw.isdigit():
                    raise ChapterScopeError("所选卷册无效。")
                number = int(raw)
            if book not in public or book not in corpus.books:
                raise ChapterScopeError("所选文库当前不可访问。")
            found = [v for v in corpus.get_volumes(book) if number is None or v.volume == number]
            if not found:
                raise ChapterScopeError("所选卷册不存在。")
            volumes.update((v.source_file, v) for v in found)
        else:
            raise ChapterScopeError("篇章范围只能与明确的公共著作或卷册组合。")
    for vol in volumes.values():
        if vol.pages and vol.norm_full:
            selected.append(DocumentScope(vol.book, vol.volume, vol.source_file,
                            vol.display_title, vol.pages[0].pdf_page, 0, len(vol.norm_full)))
    unique = { (d.source_file, d.norm_start, d.norm_end): d for d in selected }
    selected = sorted(unique.values(), key=lambda d: (corpus.book_sort_order(d.book), d.volume,
                                                      d.source_file, d.norm_start, -d.norm_end))
    documents = []
    for doc in selected:
        if any(d.source_file == doc.source_file and d.norm_start <= doc.norm_start
               and doc.norm_end <= d.norm_end for d in documents):
            continue
        if any(d.source_file == doc.source_file and doc.norm_start < d.norm_end for d in documents):
            raise ChapterScopeError("目录区间存在重叠歧义，请重新选择。")
        documents.append(doc)
    if not documents:
        raise ChapterScopeError("所选范围没有可检索正文。")
    return ChapterSelection(tuple(sorted(set(tokens))), current_version, tuple(documents))


class ChapterQuery:
    """One bounded source of truth for counts, lazy pages and streamed exports."""
    @bounded_search
    def __init__(self, corpus, selection, query, mode="exact", seconds=20):
        self.corpus, self.selection, self.query, self.mode = corpus, selection, query, mode
        self.normalized = normalize(query)
        self.keywords = corpus._export_cooccurrence_keywords(query.split()) if mode == "cooccurrence" else []
        self.complete, self.warning, self.supplements = True, "", {}
        self.deadline = time.monotonic() + seconds if seconds is not None else None
        if mode == "exact":
            volumes = {d.source_file: corpus.get_volume_by_source_file(d.source_file) for d in selection.documents}
            ranges = {}
            for doc in selection.documents:
                ranges.setdefault(doc.source_file, []).append((doc.norm_start, doc.norm_end))
            self.supplements, self.complete, self.warning = corpus._layout_scan(
                self.normalized, volumes=list(volumes.values()), document_ranges=ranges)

    def _check_budget(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ChapterScopeError('篇章检索达到计算预算，请缩小所选范围。', 504)

    def matches(self, doc):
        self._check_budget()
        corpus = self.corpus
        vol = corpus.get_volume_by_source_file(doc.source_file)
        if self.mode == "cooccurrence":
            if len(self.keywords) < 2:
                return
            # Independent slices: never join disjoint chapters to invent a hit.
            for start, end, anchor in corpus._iter_export_cooccurrence_windows(
                    vol.norm_full[doc.norm_start:doc.norm_end], self.keywords, check=self._check_budget):
                start, end = start + doc.norm_start, end + doc.norm_start
                first, last = vol.page_index_at(start), vol.page_index_at(end - 1)
                if all(vol.pages[i + 1].pdf_page == vol.pages[i].pdf_page + 1 for i in range(first, last)):
                    yield start, end, anchor, None
        else:
            segments = [{"norm_start": doc.norm_start, "norm_end": doc.norm_end}]
            extras = corpus._exact_supplement_map(vol, self.supplements.get(doc.source_file, []), segments)
            for start in corpus._iter_exact_positions(vol, self.normalized, extras, doc.norm_start, doc.norm_end):
                self._check_budget()
                extra = extras.get(start)
                yield start, extra["end"] if extra else start + len(self.normalized), self.query, extra

    def count(self, stop_after=None):
        total = 0
        for doc in self.selection.documents:
            for _ in self.matches(doc):
                total += 1
                if stop_after is not None and total >= stop_after:
                    return total
        return total

    def hit(self, doc, match):
        start, end, anchor, layout = match
        vol = self.corpus.get_volume_by_source_file(doc.source_file)
        hit = (self.corpus._make_layout_hit(vol, layout, self.query) if layout else
               self.corpus._make_hit(vol, start, end, "exact", 100, anchor))
        self.corpus._apply_document_scope(hit, doc)
        if not layout:
            hit.context = self.corpus._extract_export_sentence_context(
                vol, start, end, highlight_terms=self.keywords or None,
                norm_bounds=(doc.norm_start, doc.norm_end)) or ""
            if not hit.context:
                raise ChapterScopeError("正文偏移映射不可用，无法安全生成篇章摘录。")
        return hit.to_dict()

    def iter_hits(self, limit=None):
        if limit is not None and limit <= 0:
            return
        emitted = 0
        for doc in self.selection.documents:
            for match in self.matches(doc):
                yield self.hit(doc, match)
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

    @bounded_search
    def page(self, document_id, page, page_size):
        doc = next((d for d in self.selection.documents if d.document_id == document_id), None)
        if doc is None:
            raise ChapterScopeError("请求的篇章不在本次检索范围内。")
        count = sum(1 for _ in self.matches(doc))
        pages = max(1, (count + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        matches = islice(self.matches(doc), (page - 1) * page_size, page * page_size)
        return {"count": count, "page": page, "pages": pages, "page_size": page_size,
                "hits": [self.hit(doc, match) for match in matches],
                "exact_search_complete": self.complete, "layout_warning": self.warning}

    @bounded_search
    def aggregate(self):
        volumes, book_counts = OrderedDict(), {}
        for doc in self.selection.documents:
            count = sum(1 for _ in self.matches(doc))
            if not count:
                continue
            vol = self.corpus.get_volume_by_source_file(doc.source_file)
            cfg = self.corpus.get_book_config(doc.book)
            row = volumes.setdefault(doc.source_file, {
                "book": doc.book, "volume": doc.volume, "source_file": doc.source_file,
                "display_title": vol.display_title, "book_title": cfg.title,
                "book_short_title": cfg.short_title, "citation_title": cfg.citation_title,
                "book_sort_order": cfg.sort_order, "count": 0, "chapters": []})
            row["chapters"].append({"section_title": doc.title, "chapter_pdf_page": doc.chapter_pdf_page,
                                    "scope_document_id": doc.document_id, "count": count})
            row["count"] += count
            row["chapter_count"] = len(row["chapters"])
            book_counts[doc.book] = book_counts.get(doc.book, 0) + count
        return {"volumes": list(volumes.values()), "book_hit_counts": book_counts,
                "total_hits": sum(book_counts.values()), "exact_search_complete": self.complete,
                "layout_warning": self.warning}
