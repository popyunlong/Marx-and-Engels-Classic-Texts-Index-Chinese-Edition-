"""
检索核心：在《文集》《全集》中定位引文出处，并补充章节与 PDF 导航信息。

用法（独立调用）：
    from search import Corpus
    corpus = Corpus.load_default()
    hits = corpus.search("请粘贴需要核查的引文")
"""
from __future__ import annotations

import bisect
import re
import sqlite3
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

import fitz
import yaml
from rapidfuzz import fuzz

from book_config import BookConfig, load_book_configs
from build_index import DB_PATH, MANIFEST, VOLUMES, _EXEDIR, _STRIP_RE, _parse_page_token, normalize


MIN_QUERY_LEN = 2         # 归一化后少于此长度不检索，避免海量误命中
MIN_FUZZY_QUERY_LEN = 4   # 过短查询只做精确匹配，避免模糊结果爆炸
FUZZY_THRESHOLD = 85      # 模糊匹配的分数下限
CTX_PAD = 40              # 上下文前后字符数
MAX_TOC_SCAN_PAGES = 40
DEFAULT_GROUP_LIMIT = 30
GROUP_PAGE_SIZE = 10
GROUP_PREVIEW_COUNT = 3
CHAPTER_HITS_PAGE_SIZE = GROUP_PAGE_SIZE  # 海量短词命中时，每篇章按需分页物化的命中数
CHAPTERED_CACHE_MAX = 64  # 聚合结果缓存条数上限（语料在两次部署间静态，可安全缓存）
SHORT_QUERY_CHAPTER_MAX_LEN = 4
# 精确匹配阶段每个书库最多取的命中数。必须逐个书库累计（不能命中第一个书库就返回），
# 否则常见词只要在《文集》里出现，《全集》和《列宁全集》的正文就永远检索不到。
EXACT_HITS_PER_BOOK = 200

# 联想检索（AI 提取线索 → 在真实语料中接地定位）相关上限。本模块只做纯 Python 定位，
# 不含任何 AI 调用；这些常量用于把候选规模与扫描成本约束在在线请求可接受的范围内。
# 候选上限：取较高的安全上界（而非真正无限），实际数量已被各召回子方法的内部上限约束在数百内，
# 故等效“全部呈现”，又能防止极端宽泛查询产生病态规模拖垮前端。前端按权重排序并分页（页组）展示。
ASSOC_CANDIDATE_CAP = 300
ASSOC_KEYWORD_WINDOW = 200     # 关键词共现窗口（归一化字符）
ASSOC_MAX_KEYWORDS = 8         # 参与共现的关键词上限
ASSOC_MAX_QUOTES = 3           # 参与定位的候选原文句子上限
ASSOC_KW_OCC_CAP = 40          # 单卷内单个关键词最多扫描的出现次数（防高频词拖垮）
ASSOC_QUOTE_MAXLEN = 120       # 单条候选原文截断长度（原始字符），约束模糊对齐成本
# 片段（短语级逐字）召回：经典语录即便被改写，仍保留可逐字命中的独特短语，故以片段精确检索补召回。
ASSOC_FRAG_MIN_LEN = 4         # 片段最短长度（归一化字符）
ASSOC_FRAG_MAX_LEN = 16        # 片段最长长度
ASSOC_FRAG_MAX_FREQ = 300      # 片段全语料出现超过此值视为缺乏区分度，弃用
ASSOC_FRAG_PER = 10            # 单个片段最多取的精确命中数
ASSOC_FRAG_TOTAL_CAP = 24      # 单次联想检索最多实际检索的片段数（控成本，模型片段优先）
ASSOC_SHINGLE_CAP = 8          # 单条候选原文最多生成的自动切片数
ASSOC_CHAPTER_MAX = 16         # 篇章定向检索最多命中的篇章数（防泛标题词匹配过多）

_NUMERIC_TITLE_RE = re.compile(r"^[0-9IVXLCDMivxlcdm\s\-—–\.]+$")
_TOC_RANGE_RE = re.compile(
    r"^(?P<title>.+?)"
    r"(?:[·•∙⋯…\.\s]{2,}|[·•∙⋯…\.]+\s*)"
    r"(?P<start>[IVXLCDMivxlcdm\d]+)"
    r"(?:\s*[-—–~～至]+\s*(?P<end>[IVXLCDMivxlcdm\d]+))?"
    r"\s*$"
)
_TOC_INLINE_RANGE_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<start>[IVXLCDMivxlcdm\d]+)\s*[-—–~～至]+\s*(?P<end>[IVXLCDMivxlcdm\d]+)\s*$"
)
_TOC_TRAILING_PAGE_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<start>[IVXLCDMivxlcdm\d]+)\s*$"
)
_TITLE_NOISE = {
    "目录",
    "目 录",
    "插图目录",
    "插图",
    "封面",
    "书名",
    "书名页",
    "版权",
    "版权页",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Page:
    pdf_page: int
    printed_page: str | None
    raw_text: str
    norm_text: str


@dataclass
class TocEntry:
    title: str
    pdf_page: int
    level: int = 1
    source: str = "bookmark"
    printed_page: str | None = None
    kind: str = "body"
    sort_order: int = 0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "pdf_page": self.pdf_page,
            "level": self.level,
            "source": self.source,
            "printed_page": self.printed_page,
            "kind": self.kind,
            "sort_order": self.sort_order,
        }


@dataclass
class Volume:
    book: str
    volume: int
    source_file: str
    display_title: str
    pages: list[Page]
    norm_full: str = ""
    page_offsets: list[int] = field(default_factory=list)
    printed_to_pdf: dict[str, int] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        book: str,
        volume: int,
        source_file: str,
        display_title: str,
        pages: list[Page],
    ) -> "Volume":
        offsets = [0]
        parts: list[str] = []
        printed_to_pdf: dict[str, int] = {}
        for p in pages:
            parts.append(p.norm_text)
            offsets.append(offsets[-1] + len(p.norm_text))
            if p.printed_page and p.printed_page not in printed_to_pdf:
                printed_to_pdf[p.printed_page] = p.pdf_page
        return cls(
            book=book,
            volume=volume,
            source_file=source_file,
            display_title=display_title,
            pages=pages,
            norm_full="".join(parts),
            page_offsets=offsets,
            printed_to_pdf=printed_to_pdf,
        )

    def page_index_at(self, norm_idx: int) -> int:
        """二分查找 norm_idx 落在哪一页。"""
        i = bisect.bisect_right(self.page_offsets, norm_idx) - 1
        return max(0, min(i, len(self.pages) - 1))


@dataclass
class Hit:
    book: str
    volume: int
    source_file: str
    display_title: str
    book_title: str
    book_short_title: str
    citation_title: str
    book_sort_order: int
    pages: list[Page]
    match_type: str         # "exact" | "fuzzy"
    score: int
    context: str
    citation: str
    section_title: str | None

    def to_dict(self) -> dict:
        return {
            "book": self.book,
            "volume": self.volume,
            "source_file": self.source_file,
            "display_title": self.display_title,
            "book_title": self.book_title,
            "book_short_title": self.book_short_title,
            "citation_title": self.citation_title,
            "book_sort_order": self.book_sort_order,
            "pdf_pages": [p.pdf_page for p in self.pages],
            "printed_pages": [p.printed_page for p in self.pages],
            "match_type": self.match_type,
            "score": self.score,
            "context": self.context,
            "citation": self.citation,
            "section_title": self.section_title,
        }


@dataclass
class HitGroup:
    group_id: str
    book: str
    volume: int
    source_file: str
    display_title: str
    book_title: str
    book_short_title: str
    citation_title: str
    book_sort_order: int
    section_title: str | None
    match_type: str
    score: int
    hits: list[Hit]

    def to_dict(self, page_size: int = GROUP_PAGE_SIZE, preview_count: int = GROUP_PREVIEW_COUNT) -> dict:
        total = len(self.hits)
        return {
            "group_id": self.group_id,
            "book": self.book,
            "volume": self.volume,
            "source_file": self.source_file,
            "display_title": self.display_title,
            "book_title": self.book_title,
            "book_short_title": self.book_short_title,
            "citation_title": self.citation_title,
            "book_sort_order": self.book_sort_order,
            "section_title": self.section_title,
            "match_type": self.match_type,
            "score": self.score,
            "count": total,
            "page_size": page_size,
            "preview_count": preview_count,
            "hits": [hit.to_dict() for hit in self.hits],
        }


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------
class Corpus:
    def __init__(self, db_path: Path = DB_PATH, volumes_cfg_path: Path = VOLUMES) -> None:
        self.volumes_cfg: dict = yaml.safe_load(
            volumes_cfg_path.read_text(encoding="utf-8")
        )
        self.book_configs: list[BookConfig] = load_book_configs()
        self.book_config_by_key: dict[str, BookConfig] = {book.key: book for book in self.book_configs}
        self.books: dict[str, list[Volume]] = {book.key: [] for book in self.book_configs}
        self._manifest_by_file: dict[str, dict] = {}
        self._files_by_book_volume: dict[tuple[str, int], list[str]] = {}
        self._volumes_by_source_file: dict[str, Volume] = {}
        self._toc_cache: dict[str, list[TocEntry]] = {}
        self._toc_db_entries: dict[str, list[TocEntry]] = {}
        self._chapter_level_cache: dict[str, int] = {}
        self._segment_cache: dict[str, list[dict]] = {}
        self._segment_lock = threading.Lock()
        self._chaptered_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._chaptered_cache_lock = threading.Lock()
        self._load_manifest()
        self._load(db_path)

    @classmethod
    def load_default(cls) -> "Corpus":
        return cls()

    def _load_manifest(self) -> None:
        if not MANIFEST.exists():
            return
        manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
        for book in self.book_configs:
            for item in manifest.get(book.key) or []:
                vol = item.get("volume")
                if not isinstance(vol, int):
                    continue
                source_file = self._normalize_source_file(item.get("file") or "")
                if not source_file:
                    continue
                meta = {
                    "book": book.key,
                    "volume": vol,
                    "source_file": source_file,
                    # manifest 可显式给出 display_title（用于文件名无意义的书库，如毛泽东文集 1.pdf）；
                    # 缺省回退到 PDF 文件名（如马恩文集/列宁等文件名本身即卷题）。
                    "display_title": (str(item.get("display_title") or "").strip()
                                      or Path(source_file).stem),
                }
                self._manifest_by_file[source_file] = meta
                self._files_by_book_volume.setdefault((book.key, vol), []).append(source_file)

    def _load(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pages)").fetchall()}
        has_source_file = "source_file" in cols
        if has_source_file:
            rows = conn.execute(
                "SELECT book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text "
                "FROM pages ORDER BY book, volume, source_file, pdf_page"
            ).fetchall()
        else:
            raw_rows = conn.execute(
                "SELECT book, volume, pdf_page, printed_page, raw_text, normalized_text "
                "FROM pages ORDER BY book, volume, pdf_page"
            ).fetchall()
            rows = [
                (
                    book,
                    volume,
                    self._infer_source_file(book, volume),
                    pdf_page,
                    printed_page,
                    raw_text,
                    norm_text,
                )
                for book, volume, pdf_page, printed_page, raw_text, norm_text in raw_rows
            ]
        self._toc_db_entries = self._load_toc_entries_from_db(conn)
        conn.close()

        for (book, vol, source_file), grp in groupby(rows, key=lambda r: (r[0], r[1], r[2])):
            pgs = [
                Page(pdf_page=r[3], printed_page=r[4], raw_text=r[5], norm_text=r[6])
                for r in grp
            ]
            if book not in self.books:
                continue
            display_title = self._manifest_by_file.get(source_file, {}).get(
                "display_title",
                Path(source_file).stem if source_file else f"{book}第{vol}卷",
            )
            volume_obj = Volume.build(book, vol, source_file, display_title, pgs)
            self.books[book].append(volume_obj)
            if source_file:
                self._volumes_by_source_file[source_file] = volume_obj

        for book in self.books:
            self.books[book].sort(key=lambda v: (v.volume, v.source_file))

    def _infer_source_file(self, book: str, volume: int) -> str:
        files = self._files_by_book_volume.get((book, volume), [])
        if files:
            return files[0]
        return ""

    def _normalize_source_file(self, source_file: str) -> str:
        return str(Path(source_file).as_posix()) if source_file else ""

    def get_volume_by_source_file(self, source_file: str) -> Volume | None:
        return self._volumes_by_source_file.get(self._normalize_source_file(source_file))

    def get_volumes(self, book: str = "文集") -> list[Volume]:
        return list(self.books.get(book, []))

    def get_book_config(self, book: str) -> BookConfig:
        return self.book_config_by_key.get(book) or BookConfig(
            key=book,
            title=f"《{book}》",
            short_title=f"《{book}》",
            citation_title=book,
            folder=f"pdfs/{book}",
            sort_order=9999,
            publisher=str(self.volumes_cfg.get("publisher") or "人民出版社"),
            place=str(self.volumes_cfg.get("place") or "北京"),
            tag_class="book-other",
        )

    def book_sort_order(self, book: str) -> int:
        return self.get_book_config(book).sort_order

    def book_sort_key(self, book: str, volume: int = 0, pdf_page: int = 0) -> tuple[int, int, int, str]:
        return (self.book_sort_order(book), int(volume or 0), int(pdf_page or 0), book)

    def get_toc_entries(self, source_file: str) -> list[TocEntry]:
        source_file = self._normalize_source_file(source_file)
        if not source_file:
            return []
        if source_file not in self._toc_cache:
            volume = self.get_volume_by_source_file(source_file)
            from_db = self._toc_db_entries.get(source_file) or []
            self._toc_cache[source_file] = from_db or (self._build_toc_entries(volume) if volume else [])
        return self._toc_cache[source_file]

    def _load_toc_entries_from_db(self, conn: sqlite3.Connection) -> dict[str, list[TocEntry]]:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='toc_entries'"
        ).fetchone()
        if table is None:
            return {}

        cols = {row[1] for row in conn.execute("PRAGMA table_info(toc_entries)").fetchall()}
        required = {"source_file", "title", "pdf_page", "level", "kind", "sort_order"}
        if not required.issubset(cols):
            return {}

        has_printed = "printed_page" in cols
        printed_expr = "printed_page" if has_printed else "NULL AS printed_page"
        rows = conn.execute(
            f"SELECT source_file, title, pdf_page, {printed_expr}, level, kind, sort_order "
            "FROM toc_entries ORDER BY source_file, sort_order, pdf_page"
        ).fetchall()

        grouped: dict[str, list[TocEntry]] = {}
        for source_file, title, pdf_page, printed_page, level, kind, sort_order in rows:
            source_file = self._normalize_source_file(str(source_file or ""))
            title = self._clean_title(str(title or ""))
            try:
                page = int(pdf_page)
            except (TypeError, ValueError):
                continue
            if not source_file or not title or page < 1:
                continue
            try:
                entry_level = max(1, int(level or 1))
            except (TypeError, ValueError):
                entry_level = 1
            try:
                order = int(sort_order or 0)
            except (TypeError, ValueError):
                order = 0
            grouped.setdefault(source_file, []).append(
                TocEntry(
                    title=title,
                    pdf_page=page,
                    level=entry_level,
                    source="db",
                    printed_page=str(printed_page) if printed_page else None,
                    kind=str(kind or "body"),
                    sort_order=order,
                )
            )
        return grouped

    def get_section_for_page(self, source_file: str, pdf_page: int) -> str | None:
        entries = self.get_toc_entries(source_file)
        if not entries:
            return None
        candidates = [entry for entry in entries if entry.pdf_page <= pdf_page]
        if not candidates:
            return entries[0].title
        best = max(candidates, key=lambda entry: (entry.pdf_page, entry.level))
        return best.title

    def get_chapter_for_page(self, source_file: str, pdf_page: int) -> TocEntry | None:
        entries = self.get_toc_entries(source_file)
        if not entries:
            return None

        candidates = [entry for entry in entries if entry.pdf_page <= pdf_page]
        if not candidates:
            return entries[0]

        latest = max(candidates, key=lambda entry: (entry.pdf_page, entry.level, entry.sort_order))
        if latest.kind == "letter":
            return latest

        source_file = self._normalize_source_file(source_file)
        chapter_level = self._primary_body_chapter_level(source_file, entries)
        chapter_candidates = [
            entry
            for entry in candidates
            if entry.kind == "body" and entry.level <= chapter_level
        ]
        if chapter_candidates:
            return max(
                chapter_candidates,
                key=lambda entry: (entry.pdf_page, entry.level, entry.sort_order),
            )

        body_candidates = [entry for entry in candidates if entry.kind == "body"]
        if body_candidates:
            return max(body_candidates, key=lambda entry: (entry.pdf_page, entry.level, entry.sort_order))
        return latest

    def _primary_body_chapter_level(self, source_file: str, entries: list[TocEntry]) -> int:
        source_file = self._normalize_source_file(source_file)
        if source_file in self._chapter_level_cache:
            return self._chapter_level_cache[source_file]

        levels = sorted({entry.level for entry in entries if entry.kind == "body"})
        if not levels:
            level = 1
        elif 2 in levels:
            level = 2
        else:
            level = levels[0]
        self._chapter_level_cache[source_file] = level
        return level

    # ------------------------------------------------------------------
    # 篇章分段（按卷缓存，供短词海量命中的完整聚合使用）
    # ------------------------------------------------------------------
    def _chapter_segments(self, vol: Volume) -> list[dict]:
        """返回某一卷按篇章切分的连续区间（含归一化文本偏移），结果按卷缓存。

        归章逻辑与 get_chapter_for_page 完全一致：篇章只可能在目录条目所在页
        发生变化，因此只需在这些边界处求值即可，避免逐页扫描九万余页。
        """
        cached = self._segment_cache.get(vol.source_file)
        if cached is not None:
            return cached
        with self._segment_lock:
            cached = self._segment_cache.get(vol.source_file)
            if cached is not None:
                return cached
            segments = self._build_chapter_segments(vol)
            self._segment_cache[vol.source_file] = segments
            return segments

    def _build_chapter_segments(self, vol: Volume) -> list[dict]:
        pages = vol.pages
        n = len(pages)
        if n == 0:
            return []
        entries = self.get_toc_entries(vol.source_file)
        sorted_pdf = [p.pdf_page for p in pages]
        pdf_to_index = {p.pdf_page: i for i, p in enumerate(pages)}
        boundaries = {0}
        for entry in entries:
            idx = pdf_to_index.get(entry.pdf_page)
            if idx is None:
                j = bisect.bisect_left(sorted_pdf, entry.pdf_page)
                idx = min(max(j, 0), n - 1)
            boundaries.add(idx)
        ordered = sorted(boundaries)
        segments: list[dict] = []
        current: dict | None = None
        for pos, start_idx in enumerate(ordered):
            end_idx = ordered[pos + 1] if pos + 1 < len(ordered) else n
            page = pages[start_idx]
            chapter = self.get_chapter_for_page(vol.source_file, page.pdf_page)
            if chapter is not None:
                title = chapter.title
                chapter_pdf_page = chapter.pdf_page
                kind = chapter.kind
                level = chapter.level
                printed_page = chapter.printed_page or page.printed_page or ""
            else:
                title = "（未识别篇章）"
                chapter_pdf_page = page.pdf_page
                kind = ""
                level = 1
                printed_page = page.printed_page or ""
            key = (title, chapter_pdf_page, kind)
            norm_start = vol.page_offsets[start_idx]
            norm_end = vol.page_offsets[end_idx]
            if current is not None and current["key"] == key:
                current["norm_end"] = norm_end
            else:
                current = {
                    "key": key,
                    "title": title,
                    "chapter_pdf_page": chapter_pdf_page,
                    "kind": kind,
                    "level": level,
                    "printed_page": printed_page,
                    "first_pdf_page": page.pdf_page,
                    "norm_start": norm_start,
                    "norm_end": norm_end,
                }
                segments.append(current)
        return segments

    def warm_chapter_segments(self) -> None:
        """预热所有卷的篇章分段缓存（建议在后台线程中调用，避免首个查询卡顿）。"""
        for volumes in self.books.values():
            for vol in volumes:
                try:
                    self._chapter_segments(vol)
                except Exception:
                    continue

    # ------------------------------------------------------------------
    # 短词海量命中：完整聚合 + 按需物化
    # ------------------------------------------------------------------
    def search_chaptered(self, q: str) -> dict:
        """按卷、篇章完整聚合精确命中数（不物化命中详情）。

        仅做 C 层级子串计数，常见短词的十万级命中也能在 0.1 秒级完成，
        且覆盖全部卷、全部篇章；命中详情交由 chapter_hits 按需分页物化，
        从而既“全部呈现”又不会因一次性物化海量命中而拖垮服务。
        """
        q_norm = normalize(q)
        if len(q_norm) < MIN_QUERY_LEN:
            return {"query": q, "total_hits": 0, "volumes": [], "book_hit_counts": {}}

        # 结果缓存：聚合只取决于（静态语料 + 归一化查询词），可安全复用。
        # 命中缓存后无需重新逐卷计数，消除高并发下 0.1 秒计数被 GIL 串行的尾延迟。
        # 缓存值不会被调用方就地修改（app 层只读取字段并另建新结构），故可共享引用。
        with self._chaptered_cache_lock:
            cached = self._chaptered_cache.get(q_norm)
            if cached is not None:
                self._chaptered_cache.move_to_end(q_norm)
        if cached is not None:
            return {**cached, "query": q}

        volumes_out: list[dict] = []
        total = 0
        book_hit_counts: dict[str, int] = {}
        for book in self.books:
            for vol in self.books.get(book, []):
                nf = vol.norm_full
                if q_norm not in nf:
                    continue
                # 按 chapter_pdf_page 归并：同一篇章若被物理切成多段（get_chapter_for_page
                # 把不相邻的页段映射回同一篇章起始页），合并为一行，避免出现重复的篇章条目，
                # 并保证此处计数与 chapter_hits（按 chapter_pdf_page 取命中）完全一致。
                chapter_map: "OrderedDict[int, dict]" = OrderedDict()
                vol_count = 0
                for seg in self._chapter_segments(vol):
                    cnt = nf.count(q_norm, seg["norm_start"], seg["norm_end"])
                    if not cnt:
                        continue
                    vol_count += cnt
                    row = chapter_map.get(seg["chapter_pdf_page"])
                    if row is None:
                        chapter_map[seg["chapter_pdf_page"]] = {
                            "section_title": seg["title"],
                            "chapter_pdf_page": seg["chapter_pdf_page"],
                            "printed_page": seg["printed_page"],
                            "level": seg["level"],
                            "kind": seg["kind"],
                            "count": cnt,
                        }
                    else:
                        row["count"] += cnt
                if not vol_count:
                    continue
                chapters = list(chapter_map.values())
                chapters.sort(key=lambda c: (c["chapter_pdf_page"], c["level"], c["section_title"]))
                cfg = self.get_book_config(book)
                volumes_out.append({
                    "book": book,
                    "volume": vol.volume,
                    "source_file": vol.source_file,
                    "display_title": vol.display_title,
                    "book_title": cfg.title,
                    "book_short_title": cfg.short_title,
                    "citation_title": cfg.citation_title,
                    "book_sort_order": cfg.sort_order,
                    "count": vol_count,
                    "chapter_count": len(chapters),
                    "chapters": chapters,
                    "first_pdf_page": chapters[0]["chapter_pdf_page"] if chapters else 1,
                })
                total += vol_count
                book_hit_counts[book] = book_hit_counts.get(book, 0) + vol_count
        volumes_out.sort(key=lambda v: (v["book_sort_order"], v["volume"], v["first_pdf_page"]))
        computed: dict = {
            "query": q,
            "total_hits": total,
            "volumes": volumes_out,
            "book_hit_counts": book_hit_counts,
        }
        with self._chaptered_cache_lock:
            self._chaptered_cache[q_norm] = computed
            self._chaptered_cache.move_to_end(q_norm)
            while len(self._chaptered_cache) > CHAPTERED_CACHE_MAX:
                self._chaptered_cache.popitem(last=False)
        return {**computed, "query": q}

    def chapter_hits(
        self,
        source_file: str,
        chapter_pdf_page: int,
        q: str,
        page: int = 1,
        page_size: int = CHAPTER_HITS_PAGE_SIZE,
    ) -> dict:
        """按需物化某一卷某一篇章内的命中详情（分页）。

        只在单卷、单篇章区间内查找命中位置，工作量受限，可安全用于在线请求。
        """
        page_size = max(1, int(page_size or CHAPTER_HITS_PAGE_SIZE))
        out: dict = {"hits": [], "count": 0, "page": 1, "pages": 1, "page_size": page_size}
        q_norm = normalize(q)
        if len(q_norm) < MIN_QUERY_LEN:
            return out
        vol = self.get_volume_by_source_file(source_file)
        if vol is None:
            return out
        try:
            target_page = int(chapter_pdf_page)
        except (TypeError, ValueError):
            return out
        segments = [
            seg for seg in self._chapter_segments(vol)
            if seg["chapter_pdf_page"] == target_page
        ]
        if not segments:
            return out
        nf = vol.norm_full
        qlen = len(q_norm)
        positions: list[int] = []
        for seg in segments:
            start = seg["norm_start"]
            end = seg["norm_end"]
            while True:
                i = nf.find(q_norm, start, end)
                if i < 0:
                    break
                positions.append(i)
                start = i + 1
        count = len(positions)
        if not count:
            return out
        # 归并的多段可能不相邻，统一升序，保证分页顺序与同页命中序号正确。
        positions.sort()
        # 计算每处命中在所在页内的序号，使同页多处命中各自高亮、上下文互不相同。
        page_ranks: list[int] = []
        seen_on_page: dict[int, int] = {}
        for i in positions:
            spi = vol.page_index_at(i)
            rank = seen_on_page.get(spi, 0)
            page_ranks.append(rank)
            seen_on_page[spi] = rank + 1
        pages_total = max(1, (count + page_size - 1) // page_size)
        page = min(max(1, int(page or 1)), pages_total)
        lo = (page - 1) * page_size
        hi = lo + page_size
        hits = [
            self._make_hit(vol, i, i + qlen, "exact", 100, q, occurrence_index=rank).to_dict()
            for i, rank in zip(positions[lo:hi], page_ranks[lo:hi])
        ]
        out.update({
            "hits": hits,
            "count": count,
            "page": page,
            "pages": pages_total,
            "page_size": page_size,
        })
        return out

    # ------------------------------------------------------------------
    # 检索入口
    # ------------------------------------------------------------------
    def search(self, q: str, max_results: int = 5) -> list[Hit]:
        q_norm = normalize(q)
        if len(q_norm) < MIN_QUERY_LEN:
            return []

        exact: list[Hit] = []
        for book in self.books:
            book_hits, _ = self._exact_in_book(book, q_norm, q, limit=EXACT_HITS_PER_BOOK)
            exact.extend(book_hits)
        if exact:
            exact.sort(key=lambda h: (self.book_sort_order(h.book), h.volume))
            return self._dedupe_hits(exact)[:max_results]

        # 无精确命中，模糊兜底（所有书库一起算，按分数排序）
        if len(q_norm) < MIN_FUZZY_QUERY_LEN:
            return []

        fuzzy: list[Hit] = []
        for book in self.books:
            partial, _ = self._fuzzy_in_book(book, q_norm, q)
            fuzzy.extend(partial)
        fuzzy.sort(key=lambda h: (-h.score, self.book_sort_order(h.book)))
        return self._dedupe_hits(fuzzy)[:max_results]

    def search_grouped(
        self,
        q: str,
        group_limit: int = DEFAULT_GROUP_LIMIT,
        group_page: int = 1,
        max_hits: int | None = None,
        page_size: int = GROUP_PAGE_SIZE,
    ) -> dict:
        q_norm = normalize(q)
        if len(q_norm) < MIN_QUERY_LEN:
            return {
                "query": q,
                "total_hits": 0,
                "group_count": 0,
                "truncated": False,
                "groups": [],
            }

        hits: list[Hit] = []
        truncated = False
        exact_limit = max_hits if max_hits is not None else EXACT_HITS_PER_BOOK
        for book in self.books:
            book_hits, book_truncated = self._exact_in_book(
                book, q_norm, q, limit=exact_limit
            )
            hits.extend(book_hits)
            truncated = truncated or book_truncated
        if hits:
            return self._group_hits(
                q,
                self._dedupe_hits(hits),
                group_limit,
                page_size,
                truncated,
            )

        if len(q_norm) < MIN_FUZZY_QUERY_LEN:
            return {
                "query": q,
                "total_hits": 0,
                "group_count": 0,
                "truncated": False,
                "groups": [],
            }

        fuzzy: list[Hit] = []
        fuzzy_limit = max_hits or (group_limit * page_size * 5)
        for book in self.books:
            partial, partial_truncated = self._fuzzy_in_book(
                book,
                q_norm,
                q,
                limit=max(0, fuzzy_limit - len(fuzzy)),
            )
            fuzzy.extend(partial)
            truncated = truncated or partial_truncated or len(fuzzy) >= fuzzy_limit
            if len(fuzzy) >= fuzzy_limit:
                break
        fuzzy.sort(key=lambda h: (-h.score, self.book_sort_order(h.book)))
        return self._group_hits(
            q,
            self._dedupe_hits(fuzzy[:fuzzy_limit]),
            group_limit,
            page_size,
            truncated,
        )

    def _dedupe_hits(self, hits: list[Hit]) -> list[Hit]:
        unique: list[Hit] = []
        seen: set[tuple] = set()
        for hit in hits:
            signature = (
                hit.book,
                hit.volume,
                hit.source_file,
                tuple(page.pdf_page for page in hit.pages),
                tuple(page.printed_page or "" for page in hit.pages),
                hit.section_title or "",
                hit.match_type,
                hit.context,
                hit.citation,
            )
            if signature in seen:
                continue
            seen.add(signature)
            unique.append(hit)
        return unique

    # ------------------------------------------------------------------
    # 精确匹配
    # ------------------------------------------------------------------
    def _exact_in_book(self, book: str, q_norm: str, q_raw: str, limit: int | None = 20) -> tuple[list[Hit], bool]:
        hits: list[Hit] = []
        truncated = False
        for vol in self.books.get(book, []):
            start = 0
            while True:
                i = vol.norm_full.find(q_norm, start)
                if i < 0:
                    break
                hits.append(self._make_hit(vol, i, i + len(q_norm), "exact", 100, q_raw))
                start = i + 1
                if limit is not None and len(hits) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        return hits, truncated

    # ------------------------------------------------------------------
    # 模糊匹配
    # ------------------------------------------------------------------
    def _fuzzy_in_book(self, book: str, q_norm: str, q_raw: str, limit: int | None = None) -> tuple[list[Hit], bool]:
        hits: list[Hit] = []
        truncated = False
        for vol in self.books.get(book, []):
            if limit is not None and len(hits) >= limit:
                truncated = True
                break
            res = fuzz.partial_ratio_alignment(
                q_norm, vol.norm_full, score_cutoff=FUZZY_THRESHOLD
            )
            if res is None:
                continue
            score = int(res.score)
            if score < FUZZY_THRESHOLD:
                continue
            hits.append(self._make_hit(
                vol, res.dest_start, res.dest_end, "fuzzy", score, q_raw
            ))
        return hits, truncated

    # ------------------------------------------------------------------
    # 联想检索的接地定位（纯 Python，无 AI；AI 只在上层提供 quotes/keywords）
    # ------------------------------------------------------------------
    def locate_quote(self, quote: str, *, per_book_exact: int = 5, allow_fuzzy: bool = True) -> list[Hit]:
        """在真实语料中定位单条候选原文：先精确（每书库取前若干处），无精确再模糊（每卷最佳 span）。

        返回真实 Hit（含 context/citation/section），绝不构造不存在的出处。
        ``allow_fuzzy=False`` 时跳过整句模糊兜底（联想检索用片段召回替代，避免逐卷 partial_ratio 的高开销）。
        """
        q_raw = (quote or "").strip()
        if len(q_raw) > ASSOC_QUOTE_MAXLEN:
            q_raw = q_raw[:ASSOC_QUOTE_MAXLEN]
        q_norm = normalize(q_raw)
        if len(q_norm) < MIN_QUERY_LEN:
            return []
        hits: list[Hit] = []
        for book in self.books:
            book_hits, _ = self._exact_in_book(book, q_norm, q_raw, limit=per_book_exact)
            hits.extend(book_hits)
        if hits:
            return hits
        if not allow_fuzzy or len(q_norm) < MIN_FUZZY_QUERY_LEN:
            return []
        fuzzy: list[Hit] = []
        for book in self.books:
            partial, _ = self._fuzzy_in_book(book, q_norm, q_raw)
            fuzzy.extend(partial)
        fuzzy.sort(key=lambda h: (-h.score, self.book_sort_order(h.book)))
        return fuzzy

    def keyword_cooccurrence(
        self,
        keywords: list[str],
        *,
        window: int = ASSOC_KEYWORD_WINDOW,
        min_distinct: int | None = None,
        occ_cap: int = ASSOC_KW_OCC_CAP,
    ) -> list[Hit]:
        """定位“多关键词在近邻窗口内共现”的真实段落。

        先用 M-of-N 子串成员（C 层 ``in``）过滤掉绝大多数卷，仅对存活卷做位置扫描与滑窗，
        从而在数百 MB 内存文本上仍可在线运行（沿用 search_chaptered 的“先成员判断再扫描”模式）。
        每卷取一个最佳窗口（不同关键词数最多、跨度最紧），生成真实 Hit。
        """
        seen: set[str] = set()
        kws: list[str] = []
        for k in keywords or []:
            kn = normalize(str(k or ""))
            if len(kn) < MIN_QUERY_LEN or kn in seen:
                continue
            seen.add(kn)
            kws.append(kn)
            if len(kws) >= ASSOC_MAX_KEYWORDS:
                break
        if len(kws) < 2:
            return []
        if min_distinct is None:
            min_distinct = max(2, (len(kws) + 1) // 2)  # ceil(0.5 * n)
        min_distinct = min(min_distinct, len(kws))

        hits: list[Hit] = []
        for book in self.books:
            for vol in self.books.get(book, []):
                nf = vol.norm_full
                present = [kn for kn in kws if kn in nf]
                if len(present) < min_distinct:
                    continue
                # 收集各关键词前 occ_cap 个出现位置（非重叠）
                occ: list[tuple[int, int, int]] = []  # (pos, kw_id, kw_len)
                for kid, kn in enumerate(present):
                    start = 0
                    cnt = 0
                    klen = len(kn)
                    while cnt < occ_cap:
                        i = nf.find(kn, start)
                        if i < 0:
                            break
                        occ.append((i, kid, klen))
                        start = i + klen
                        cnt += 1
                if not occ:
                    continue
                occ.sort()
                # 双指针滑窗：求“不同关键词数最多、跨度最紧”的窗口
                counts: dict[int, int] = {}
                distinct = 0
                left = 0
                best: tuple[int, int, int, int] | None = None  # (distinct, -span, start, end)
                for right in range(len(occ)):
                    pos_r, kid_r, len_r = occ[right]
                    counts[kid_r] = counts.get(kid_r, 0) + 1
                    if counts[kid_r] == 1:
                        distinct += 1
                    lo = pos_r - window + 1
                    while occ[left][0] < lo:
                        kid_l = occ[left][1]
                        counts[kid_l] -= 1
                        if counts[kid_l] == 0:
                            distinct -= 1
                        left += 1
                    if distinct >= min_distinct:
                        win_start = occ[left][0]
                        win_end = pos_r + len_r
                        cand = (distinct, -(win_end - win_start), win_start, win_end)
                        if best is None or cand > best:
                            best = cand
                if best is None:
                    continue
                win_distinct, _neg_span, win_start, win_end = best
                # 高亮锚点：选窗口内出现的最长关键词，确保 context 高亮落在真实命中上
                in_window = {
                    kid for (pos, kid, klen) in occ if win_start <= pos < win_end
                }
                anchor = max(
                    (present[kid] for kid in in_window),
                    key=len,
                    default=present[0],
                )
                # 关键词共现是最弱信号（可能巧合），分值封顶低于片段/整句/篇章定向，避免淹没精确命中
                coverage_score = min(88, int(round(92 * win_distinct / len(kws))))
                hits.append(
                    self._make_hit(vol, win_start, win_end, "fuzzy", coverage_score, anchor)
                )
        return hits

    def _fragment_exact_hits(self, fn: str, *, max_freq: int, per_fragment: int) -> list[Hit] | None:
        """单趟扫描：统计片段全语料出现数、收集前若干处真实命中；超过 max_freq 视为太常见，弃用。

        把“频次判定”和“取命中”合并为一次扫描，避免重复全语料遍历（联想检索性能关键）。
        """
        hits: list[Hit] = []
        total = 0
        flen = len(fn)
        for book in self.books:
            for vol in self.books.get(book, []):
                nf = vol.norm_full
                start = 0
                while True:
                    i = nf.find(fn, start)
                    if i < 0:
                        break
                    total += 1
                    if total > max_freq:
                        return None  # 太常见、缺区分度，整体弃用（提前结束，开销小）
                    if len(hits) < per_fragment:
                        hits.append(self._make_hit(vol, i, i + flen, "exact", 100, fn))
                    start = i + flen
        return hits

    def fragment_search(
        self,
        fragments: list[str],
        *,
        max_freq: int = ASSOC_FRAG_MAX_FREQ,
        per_fragment: int = ASSOC_FRAG_PER,
        total_cap: int = ASSOC_FRAG_TOTAL_CAP,
    ) -> list[tuple[Hit, str]]:
        """对“逐字短语片段”做精确检索：保留长度合适且具区分度（出现数 ≤ max_freq）的片段。

        返回 (真实 Hit, 命中片段) 列表；分值按片段长度给出（越长越可信）。按传入顺序处理并
        受 total_cap 限制，故应把可信度更高的模型片段排在前面。
        """
        seen: set[str] = set()
        results: list[tuple[Hit, str]] = []
        used = 0
        for frag in fragments or []:
            fn = normalize(str(frag or ""))
            if not (ASSOC_FRAG_MIN_LEN <= len(fn) <= ASSOC_FRAG_MAX_LEN) or fn in seen:
                continue
            seen.add(fn)
            if used >= total_cap:
                break
            used += 1
            hits = self._fragment_exact_hits(fn, max_freq=max_freq, per_fragment=per_fragment)
            if not hits:
                continue
            score = min(97, 56 + 6 * len(fn))  # 4字→80，7字→98→封顶97
            for h in hits:
                h.score = score
                results.append((h, fn))
        return results

    def _shingle_fragments(
        self,
        text: str,
        *,
        lengths: tuple[int, ...] = (5, 6, 8),
        cap: int = ASSOC_SHINGLE_CAP,
    ) -> list[str]:
        """从候选原文（可能是改写句）切出多种长度的候选片段（纯字符串操作，不扫描语料）。

        短片段优先：改写改动较大时短片段更可能逐字保留（“社会关系”“自由发展”），且常见短片段
        在 fragment_search 里会因超频次被廉价剔除；长片段作为补充提升区分度。是否真正存在/是否
        过于常见，统一留给 fragment_search 单趟扫描判定。
        """
        s = normalize(text)
        seen: set[str] = set()
        kept: list[str] = []
        for length in lengths:
            if len(s) < length:
                continue
            for i in range(len(s) - length + 1):
                frag = s[i:i + length]
                if frag in seen:
                    continue
                seen.add(frag)
                kept.append(frag)
                if len(kept) >= cap:
                    break
            if len(kept) >= cap:
                break
        return kept

    def _content_window_in_range(
        self,
        vol: Volume,
        range_start: int,
        range_end: int,
        kws_norm: list[str],
        *,
        window: int = ASSOC_KEYWORD_WINDOW,
        occ_cap: int = ASSOC_KW_OCC_CAP,
    ) -> tuple[int, int, int, str] | None:
        """在指定区间 [range_start, range_end) 内找“内容关键词最密集”的窗口，返回 (start,end,distinct,anchor)。"""
        nf = vol.norm_full
        present = [kn for kn in kws_norm if nf.find(kn, range_start, range_end) != -1]
        if not present:
            return None
        occ: list[tuple[int, int, int]] = []
        for kid, kn in enumerate(present):
            start = range_start
            cnt = 0
            klen = len(kn)
            while cnt < occ_cap:
                i = nf.find(kn, start, range_end)
                if i < 0:
                    break
                occ.append((i, kid, klen))
                start = i + klen
                cnt += 1
        if not occ:
            return None
        occ.sort()
        counts: dict[int, int] = {}
        distinct = 0
        left = 0
        best: tuple[int, int, int, int] | None = None
        for right in range(len(occ)):
            pos_r, kid_r, len_r = occ[right]
            counts[kid_r] = counts.get(kid_r, 0) + 1
            if counts[kid_r] == 1:
                distinct += 1
            lo = pos_r - window + 1
            while occ[left][0] < lo:
                kid_l = occ[left][1]
                counts[kid_l] -= 1
                if counts[kid_l] == 0:
                    distinct -= 1
                left += 1
            win_start = occ[left][0]
            win_end = pos_r + len_r
            cand = (distinct, -(win_end - win_start), win_start, win_end)
            if best is None or cand > best:
                best = cand
        win_distinct, _neg, ws, we = best
        in_window = {kid for (p, kid, kl) in occ if ws <= p < we}
        anchor = max((present[kid] for kid in in_window), key=len, default=present[0])
        return ws, we, win_distinct, anchor

    def chapter_focused_search(
        self,
        chapter_keywords: list[str],
        content_keywords: list[str],
        *,
        max_chapters: int = ASSOC_CHAPTER_MAX,
        window: int = ASSOC_KEYWORD_WINDOW,
    ) -> list[tuple[Hit, str]]:
        """篇章定向检索：先按“著作/篇章名”命中篇章标题，再在该篇（含其子节的整段范围）内定位内容关键词。

        这正是 “[著作名] [主题]” 型输入（如“共产党宣言 资产阶级贡献”“雾月十八日 农民”）的强力路径：
        命中的标题把搜索锚定到正确的篇目，主题词再在篇内找到最相关的段落。返回 (真实 Hit, 命中的标题词)。
        """
        def _norm_unique(items: list[str] | None) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for it in items or []:
                n = normalize(str(it or ""))
                if len(n) >= MIN_QUERY_LEN and n not in seen:
                    seen.add(n)
                    out.append(n)
            return out

        ck = _norm_unique(chapter_keywords)
        if not ck:
            return []
        cw = _norm_unique(content_keywords)
        results: list[tuple[Hit, str]] = []
        matched = 0
        for book in self.books:
            for vol in self.books.get(book, []):
                segs = self._chapter_segments(vol)
                covered_end = -1
                for i, seg in enumerate(segs):
                    if seg["norm_start"] < covered_end:
                        continue  # 已被前一命中篇目的整段范围覆盖
                    title_norm = normalize(seg["title"])
                    if not title_norm:
                        continue
                    hit_ck = next((k for k in ck if k in title_norm), "")
                    if not hit_ck:
                        continue
                    # 把范围扩展到该篇所有子节（直到出现同级或更高级的下一篇目），覆盖整部著作
                    lvl = int(seg.get("level") or 1)
                    range_end = seg["norm_end"]
                    j = i + 1
                    while j < len(segs) and int(segs[j].get("level") or 1) > lvl:
                        range_end = segs[j]["norm_end"]
                        j += 1
                    covered_end = range_end
                    win = self._content_window_in_range(vol, seg["norm_start"], range_end, cw, window=window) if cw else None
                    if win:
                        ws, we, distinct, anchor = win
                        # 命中“著作名”所指篇内、且主题词在此密集 → 强相关，分值高于泛共现
                        base = min(100, 90 + 3 * distinct)
                        results.append((self._make_hit(vol, ws, we, "exact", base, anchor), hit_ck))
                    else:
                        # 标题命中但篇内无内容关键词：以篇章开头作为定位（中等偏高权重）
                        ws = seg["norm_start"]
                        we = min(range_end, ws + 120)
                        results.append((self._make_hit(vol, ws, we, "exact", 86, hit_ck), hit_ck))
                    matched += 1
                    if matched >= max_chapters:
                        return results
        return results

    def locate_associative(
        self,
        *,
        quotes: list[str],
        keywords: list[str],
        fragments: list[str] | None = None,
        chapter_keywords: list[str] | None = None,
        candidate_cap: int = ASSOC_CANDIDATE_CAP,
    ) -> list[Hit]:
        """编排：整句定位 + 逐字片段召回 + 关键词共现 + 篇章关键词加权，章节折叠并按权重综合打分。

        召回以片段精确命中为主力（经典语录被改写后仍保留可逐字命中的短语），整句定位负责
        命中规范译文时的高置信，关键词共现作为兜底，篇章关键词命中所属篇章标题再加权。
        返回的全部是真实 Hit；综合权重写回 ``Hit.score``（0-100），供上层聚合与按权重优先排序。
        """
        def _chapter_key(h: Hit) -> tuple:
            first_page = h.pages[0].pdf_page if h.pages else -1
            return (h.book, h.volume, h.source_file, first_page)

        base_by_ch: dict[tuple, int] = {}
        hit_by_ch: "OrderedDict[tuple, Hit]" = OrderedDict()
        evid_by_ch: dict[tuple, set] = {}

        def _add(h: Hit, base: int, evidence) -> None:
            key = _chapter_key(h)
            if base > base_by_ch.get(key, -1):
                hit_by_ch[key] = h
                base_by_ch[key] = base
            elif key not in base_by_ch:
                hit_by_ch[key] = h
                base_by_ch[key] = base
            evid_by_ch.setdefault(key, set()).add(evidence)

        # 1) 整句定位（命中规范译文时最可信）；整句模糊兜底交给片段召回，避免逐卷 partial_ratio 高开销
        for quote in (quotes or [])[:ASSOC_MAX_QUOTES]:
            for h in self.locate_quote(quote, allow_fuzzy=False):
                _add(h, h.score, ("quote", (quote or "")[:24]))

        # 2) 逐字片段召回：模型片段在前（更可信、优先占用检索预算）+ 候选原文自动切片在后
        frag_pool: list[str] = list(fragments or [])
        for quote in (quotes or [])[:ASSOC_MAX_QUOTES]:
            frag_pool.extend(self._shingle_fragments(quote))
        for h, frag in self.fragment_search(frag_pool):
            _add(h, h.score, ("frag", frag))

        # 3) 篇章定向：著作/篇章名命中标题 → 在该篇内定位主题词（“[著作] [主题]”型输入的强力路径）
        for h, ck in self.chapter_focused_search(chapter_keywords or [], keywords or []):
            _add(h, h.score, ("chapter", ck))

        # 4) 关键词共现兜底
        for h in self.keyword_cooccurrence(keywords or []):
            _add(h, h.score, ("kw", None))

        if not hit_by_ch:
            return []

        # 篇章关键词：命中所属篇章标题者加权（用户描述常隐含“在哪一篇/哪一章”的线索）
        ck_norm = []
        seen_ck: set[str] = set()
        for ck in chapter_keywords or []:
            n = normalize(str(ck or ""))
            if len(n) >= MIN_QUERY_LEN and n not in seen_ck:
                seen_ck.add(n)
                ck_norm.append(n)

        results: list[Hit] = []
        for key, h in hit_by_ch.items():
            base = base_by_ch[key]
            distinct = len(evid_by_ch.get(key, ()))
            chapter_boost = 0
            if ck_norm and h.section_title:
                sect = normalize(h.section_title)
                matched = sum(1 for n in ck_norm if n in sect)
                chapter_boost = min(18, 6 * matched)
            # 综合权重：基础证据 + 多证据加成(每多一条+3) + 篇章标题命中加成，封顶 100
            h.score = min(100, base + 3 * max(0, distinct - 1) + chapter_boost)
            results.append(h)

        results.sort(key=lambda h: (-h.score, self.book_sort_order(h.book), h.volume))
        return results[:candidate_cap]

    def _group_hits(
        self,
        query: str,
        hits: list[Hit],
        group_limit: int,
        page_size: int,
        truncated: bool,
    ) -> dict:
        grouped: OrderedDict[tuple, list[Hit]] = OrderedDict()
        for hit in hits:
            key = (
                hit.book,
                hit.volume,
                hit.source_file,
                hit.section_title or "（未识别篇章）",
                hit.match_type,
            )
            grouped.setdefault(key, []).append(hit)

        groups: list[HitGroup] = []
        for key, group_hits in grouped.items():
            first = group_hits[0]
            group_id = f"{first.book}|{first.volume}|{first.source_file}|{first.section_title or ''}|{first.match_type}"
            groups.append(
                HitGroup(
                    group_id=group_id,
                    book=first.book,
                    volume=first.volume,
                    source_file=first.source_file,
                    display_title=first.display_title,
                    book_title=first.book_title,
                    book_short_title=first.book_short_title,
                    citation_title=first.citation_title,
                    book_sort_order=first.book_sort_order,
                    section_title=first.section_title,
                    match_type=first.match_type,
                    score=max(hit.score for hit in group_hits),
                    hits=group_hits,
                )
            )

        def _front_matter_penalty(group: HitGroup) -> int:
            # 同卷内：有印刷页码的正文命中优先于目录/前置页命中（后者引文只能按 PDF 页码降级）。
            first_page = group.hits[0].pages[0]
            printed = first_page.printed_page
            return 0 if (printed and not str(printed).startswith("pre-")) else 1

        groups.sort(
            key=lambda group: (
                group.book_sort_order,
                group.volume,
                _front_matter_penalty(group),
                group.hits[0].pages[0].pdf_page,
                -len(group.hits),
            )
        )
        limited_groups = groups[:group_limit]
        more_groups = len(groups) > group_limit

        return {
            "query": query,
            "total_hits": len(hits),
            "group_count": len(groups),
            "truncated": truncated or more_groups,
            "groups": [group.to_dict(page_size=page_size) for group in limited_groups],
        }

    # ------------------------------------------------------------------
    # 命中结构与引文
    # ------------------------------------------------------------------
    def _make_hit(
        self,
        vol: Volume,
        norm_start: int,
        norm_end: int,
        match_type: str,
        score: int,
        q_raw: str,
        occurrence_index: int = 0,
    ) -> Hit:
        norm_end = max(norm_end, norm_start + 1)
        start_pi = vol.page_index_at(norm_start)
        end_pi = vol.page_index_at(norm_end - 1)
        pages = vol.pages[start_pi:end_pi + 1]

        context = self._extract_context(pages, q_raw, occurrence_index)
        citation = self._make_citation(vol.book, vol.volume, pages, source_file=vol.source_file)
        section_title = self.get_section_for_page(vol.source_file, pages[0].pdf_page)
        book_cfg = self.get_book_config(vol.book)

        return Hit(
            book=vol.book,
            volume=vol.volume,
            source_file=vol.source_file,
            display_title=vol.display_title,
            book_title=book_cfg.title,
            book_short_title=book_cfg.short_title,
            citation_title=book_cfg.citation_title,
            book_sort_order=book_cfg.sort_order,
            pages=pages,
            match_type=match_type,
            score=score,
            context=context,
            citation=citation,
            section_title=section_title,
        )

    def _extract_context(self, pages: list[Page], q_raw: str, occurrence_index: int = 0) -> str:
        raw = "\n".join(p.raw_text for p in pages)
        # 构造宽松正则：查询字之间允许出现任意非字词字符（空白、标点、换行）
        keep = [c for c in q_raw if not _STRIP_RE.match(c)]
        if not keep:
            return raw[:200].replace("\n", " ")
        pattern = r"\W*".join(re.escape(c) for c in keep)
        # 同一页可能出现多处命中：高亮第 occurrence_index 处（而非永远第一处），
        # 否则同页的多处命中会渲染出完全相同的上下文，看起来像重复结果。
        matches = list(re.finditer(pattern, raw))
        if matches:
            idx = occurrence_index if 0 <= occurrence_index < len(matches) else 0
            s, e = matches[idx].span()
            a = max(0, s - CTX_PAD)
            b = min(len(raw), e + CTX_PAD)
            snippet = raw[a:s] + "[[H]]" + raw[s:e] + "[[/H]]" + raw[e:b]
            return snippet.replace("\n", " ").strip()
        # 兜底：截取开头
        return raw[:200].replace("\n", " ")

    def _make_citation(self, book: str, volume: int, pages: list[Page], source_file: str | None = None) -> str:
        # 分册年份优先：同一卷分多册、各册年份不同的（如马恩《全集》第 26 卷三册），按 source_file
        # 在 file_years 里单独取年份；未命中再回退到「卷→年」映射。
        file_years = self.volumes_cfg.get("file_years") or {}
        year = (file_years.get(source_file) if source_file else None) or self.volumes_cfg.get(book, {}).get(volume, "")
        book_cfg = self.get_book_config(book)
        publisher = book_cfg.publisher or self.volumes_cfg.get("publisher", "人民出版社")
        place = book_cfg.place or self.volumes_cfg.get("place", "北京")

        printed_nums = [
            p.printed_page
            for p in pages
            if p.printed_page and not p.printed_page.startswith("pre-")
        ]
        if printed_nums:
            first, last = printed_nums[0], printed_nums[-1]
            page_str = f"第{first}页" if first == last else f"第{first}-{last}页"
        else:
            # 没识别出印刷页码：用 PDF 物理页号做降级，标明
            pdf_nums = [p.pdf_page for p in pages]
            first, last = pdf_nums[0], pdf_nums[-1]
            if first == last:
                page_str = f"第{first}页（按PDF页码）"
            else:
                page_str = f"第{first}-{last}页（按PDF页码）"

        title = f"《{book_cfg.citation_title}》第{volume}卷"
        year_str = f"{year}年" if year else "xxxx年"
        return f"{title}，{place}：{publisher}，{year_str}，{page_str}。"

    # ------------------------------------------------------------------
    # 目录 / 章节
    # ------------------------------------------------------------------
    def _build_toc_entries(self, volume: Volume | None) -> list[TocEntry]:
        if volume is None or not volume.source_file:
            return []
        pdf_path = (_EXEDIR / volume.source_file).resolve()
        if not pdf_path.exists():
            return []

        with fitz.open(pdf_path) as doc:
            bookmarks = self._extract_bookmark_entries(doc)
            numeric_map = self._build_numeric_page_map(bookmarks, volume)
            meaningful_bookmarks = [
                entry for entry in bookmarks if self._is_navigable_title(entry.title)
            ]

            if self._bookmarks_are_good_enough(meaningful_bookmarks, bookmarks):
                return meaningful_bookmarks

            parsed_entries = self._extract_toc_from_pages(doc, volume, bookmarks, numeric_map)
            if parsed_entries:
                return parsed_entries

            return meaningful_bookmarks

    def _extract_bookmark_entries(self, doc: fitz.Document) -> list[TocEntry]:
        rows = doc.get_toc(simple=False)
        seen: set[tuple[int, int, str]] = set()
        entries: list[TocEntry] = []
        for row in rows:
            if len(row) < 3:
                continue
            level = max(1, int(row[0]))
            title = self._clean_title(str(row[1]))
            pdf_page = self._coerce_pdf_page(row[2])
            if not title or pdf_page is None:
                continue
            key = (level, pdf_page, title)
            if key in seen:
                continue
            seen.add(key)
            entries.append(TocEntry(title=title, pdf_page=pdf_page, level=level, source="bookmark"))
        return entries

    def _build_numeric_page_map(self, entries: list[TocEntry], volume: Volume) -> dict[str, int]:
        page_map = dict(volume.printed_to_pdf)
        for entry in entries:
            token = _parse_page_token(entry.title)
            if token:
                page_map.setdefault(token, entry.pdf_page)
        return page_map

    def _bookmarks_are_good_enough(
        self,
        meaningful_entries: list[TocEntry],
        all_entries: list[TocEntry],
    ) -> bool:
        if len(meaningful_entries) < 10:
            return False
        return len(meaningful_entries) / max(1, len(all_entries)) >= 0.12

    def _extract_toc_from_pages(
        self,
        doc: fitz.Document,
        volume: Volume,
        bookmarks: list[TocEntry],
        page_map: dict[str, int],
    ) -> list[TocEntry]:
        toc_pages = self._find_toc_pages(doc, bookmarks)
        if not toc_pages:
            return []

        parsed: list[TocEntry] = []
        pending_title = ""
        seen: set[tuple[int, str]] = set()
        for pdf_page in toc_pages:
            text = doc[pdf_page - 1].get_text("text").replace("\x00", "")
            for raw_line in text.splitlines():
                line = self._clean_title(raw_line)
                if not line or self._is_toc_noise(line):
                    continue

                title, token = self._parse_toc_line(line)
                if token is None:
                    if self._should_carry_line(line):
                        pending_title = f"{pending_title}{line}" if pending_title else line
                    else:
                        pending_title = ""
                    continue

                if pending_title:
                    title = f"{pending_title}{title}"
                    pending_title = ""

                title = self._clean_title(title)
                if not title or self._is_toc_noise(title):
                    continue

                mapped_pdf_page = self._map_printed_token_to_pdf(token, page_map)
                if mapped_pdf_page is None:
                    continue

                key = (mapped_pdf_page, title)
                if key in seen:
                    continue
                seen.add(key)
                parsed.append(
                    TocEntry(
                        title=title,
                        pdf_page=mapped_pdf_page,
                        level=1,
                        source="parsed_toc",
                    )
                )

        parsed.sort(key=lambda entry: (entry.pdf_page, entry.title))
        return parsed

    def _find_toc_pages(self, doc: fitz.Document, bookmarks: list[TocEntry]) -> list[int]:
        seed_pages = [
            entry.pdf_page
            for entry in bookmarks
            if "目录" in entry.title.replace(" ", "")
        ]
        if not seed_pages:
            seed_pages = list(range(1, min(doc.page_count, MAX_TOC_SCAN_PAGES) + 1))

        visited: set[int] = set()
        for start_page in seed_pages:
            if start_page in visited:
                continue
            visited.add(start_page)
            text = doc[start_page - 1].get_text("text").replace("\x00", "")
            if not self._looks_like_toc_page(text):
                continue
            pages = [start_page]
            for next_page in range(start_page + 1, min(doc.page_count, start_page + 7) + 1):
                next_text = doc[next_page - 1].get_text("text").replace("\x00", "")
                if not self._looks_like_toc_page(next_text, follow_page=True):
                    break
                pages.append(next_page)
            return pages
        return []

    def _looks_like_toc_page(self, text: str, follow_page: bool = False) -> bool:
        lines = [self._clean_title(line) for line in text.splitlines() if self._clean_title(line)]
        if not lines:
            return False
        joined_head = "".join(lines[:4]).replace(" ", "")
        has_catalog_word = "目录" in joined_head
        hit_lines = sum(1 for line in lines if self._parse_toc_line(line)[1] is not None)
        if has_catalog_word and hit_lines >= 3:
            return True
        if follow_page and hit_lines >= 6:
            return True
        return False

    def _parse_toc_line(self, line: str) -> tuple[str, str | None]:
        candidate = self._clean_toc_line(line)
        for pattern in (_TOC_RANGE_RE, _TOC_INLINE_RANGE_RE, _TOC_TRAILING_PAGE_RE):
            match = pattern.match(candidate)
            if not match:
                continue
            title = self._clean_title(match.group("title"))
            token = _parse_page_token(match.group("start"))
            if title and token:
                return title, token
        return candidate, None

    def _clean_toc_line(self, line: str) -> str:
        line = unicodedata.normalize("NFKC", line).replace("\u00a0", " ")
        line = line.replace("—", "-").replace("–", "-")
        line = re.sub(r"\s+", " ", line)
        return line.strip()

    def _map_printed_token_to_pdf(self, token: str, page_map: dict[str, int]) -> int | None:
        if token in page_map:
            return page_map[token]
        if token.startswith("pre-"):
            return page_map.get(token)
        return None

    def _coerce_pdf_page(self, value: object) -> int | None:
        if isinstance(value, int):
            return value if value >= 1 else None
        if isinstance(value, str) and value.isdigit():
            page = int(value)
            return page if page >= 1 else None
        return None

    def _clean_title(self, text: str) -> str:
        text = unicodedata.normalize("NFKC", text).replace("\x00", "")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _is_navigable_title(self, title: str) -> bool:
        title = self._clean_title(title)
        if not title:
            return False
        return _NUMERIC_TITLE_RE.fullmatch(title) is None

    def _is_toc_noise(self, title: str) -> bool:
        compact = title.replace(" ", "")
        if compact in {item.replace(" ", "") for item in _TITLE_NOISE}:
            return True
        return _NUMERIC_TITLE_RE.fullmatch(title) is not None

    def _should_carry_line(self, line: str) -> bool:
        if self._is_toc_noise(line):
            return False
        compact = line.replace(" ", "")
        if compact.startswith(("卡·马克思", "弗·恩格斯", "马克思", "恩格斯")) and len(line) <= 24:
            return False
        if len(line) <= 2:
            return False
        return True


if __name__ == "__main__":
    import sys

    corpus = Corpus.load_default()
    q = " ".join(sys.argv[1:]) or input("请输入引文：")
    results = corpus.search(q)
    if not results:
        print("未找到匹配。")
    for i, h in enumerate(results, 1):
        print(f"\n[{i}] {h.match_type} score={h.score}")
        print(f"  书={h.book}  卷={h.volume}  文件={h.source_file}")
        print(f"  篇目={h.section_title or '（未识别）'}")
        print(
            f"  PDF页={[p.pdf_page for p in h.pages]}  "
            f"印刷页={[p.printed_page for p in h.pages]}"
        )
        print(f"  上下文：{h.context}")
        print(f"  引文：{h.citation}")
