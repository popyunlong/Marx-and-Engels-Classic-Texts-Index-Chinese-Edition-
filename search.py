"""
检索核心：在《文集》《全集》中定位引文出处，并补充章节与 PDF 导航信息。

用法（独立调用）：
    from search import Corpus
    corpus = Corpus.load_default()
    hits = corpus.search("请粘贴需要核查的引文")
"""
from __future__ import annotations

import bisect
import os
import re
import sqlite3
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

import fitz
import yaml
from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from book_config import BookConfig, load_book_configs
from build_index import DB_PATH, MANIFEST, VOLUMES, _EXEDIR, _STRIP_RE, _parse_page_token, normalize
from page_label_overrides import apply_page_label_overrides, load_page_label_overrides
from page_labels import citation_pages, page_reference, load_page_evidence, apply_page_evidence


MIN_QUERY_LEN = 2         # 归一化后少于此长度不检索，避免海量误命中
# 近似匹配（OCR 容错兜底，仅在精确零命中时触发）：语义是「容错的精确」——命中片段与
# 查询的真实编辑距离必须 ≤ 容错字数 K，K 按查询长度计算。短查询的偶然碰撞率高且手动
# 校正成本低，故直接不做近似。
MIN_FUZZY_QUERY_LEN = 10  # 归一化后短于此长度不做近似匹配
# 短/中查询（10–59 归一化字符）：维持从严容错——常用字偶然重叠即可碰瓷，且逐字校对成本低。
FUZZY_MAX_ERRORS = 2      # 短/中查询容错字数上限（编辑距离）
FUZZY_ERROR_STEP = 10     # 短/中查询每满 10 个归一化字符容 1 个错字（10-19 字容 1 错，≥20 字容 2 错）
# 长段落引文（≥60 归一化字符，如整段粘贴的经典语录）单独放宽容错。归一化已剥离绝大多数脚注
# 符号（①②③/¹²³/*†‡※/[1]/（1）/〔1〕/裸数字 → 空），故脚注本身通常不破坏精确匹配；真正的
# 破坏源是整段文本里散布的 OCR 错字漏字（含被误识入正文的脚注残字），它们逐处累积成多字编辑
# 差异。2 字硬上限会让「差不多一模一样」的整段引文在每个书库都精确零命中、又被近似兜底以 K=2
# 拒之门外（各书库扫描件的错字位置还各不相同）。故长段落容错随长度线性增长、封顶约 8% 字符差：
# 仍是「高度逐字」的精确指向（无关段落不可能与某条 140+ 字语录 92% 逐字重合，假阳性可忽略），
# 而分散的 OCR 噪声被吸收。真实编辑距离复核（≤K）与 partial_ratio 候选定位的机制均不变。
FUZZY_LONG_QUERY_LEN = 60   # 归一化长度 ≥ 此值按长段落放宽容错
FUZZY_LONG_ERROR_STEP = 12  # 长段落每约 12 个归一化字符容 1 个错字（≈8% 字符差）
FUZZY_LONG_MAX_ERRORS = 24  # 长段落容错字数上限（编辑距离）：防极长查询把近似扫描 cutoff 压得过低
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

# 近似兜底并发闸：零精确命中的查询会逐卷跑 partial_ratio（全语料 CPU 密集扫描），多个并发即可能
# 占满 waitress 线程池（历史「线程饥饿」事故面）。限制同时进行的近似扫描数；短超时拿不到名额就降级
# 为「无结果」而非阻塞等待——只在真有大量并发近似扫描时才降级，正常负载下零影响。可经环境变量调整。
_FUZZY_SCAN_CONCURRENCY = max(1, int(os.environ.get("MARX_FUZZY_CONCURRENCY", "2") or "2"))
_FUZZY_SCAN_ACQUIRE_TIMEOUT = max(0.0, float(os.environ.get("MARX_FUZZY_ACQUIRE_TIMEOUT_SECONDS", "3") or "3"))
_FUZZY_SCAN_SEMAPHORE = threading.BoundedSemaphore(_FUZZY_SCAN_CONCURRENCY)

# 联想检索（AI 提取线索 → 在真实语料中接地定位）相关上限。本模块只做纯 Python 定位，
# 不含任何 AI 调用；这些常量用于把候选规模与扫描成本约束在在线请求可接受的范围内。
# 候选上限：取较高的安全上界（而非真正无限），实际数量已被各召回子方法的内部上限约束在数百内，
# 故等效“全部呈现”，又能防止极端宽泛查询产生病态规模拖垮前端。前端按权重排序并分页（页组）展示。
ASSOC_CANDIDATE_CAP = 300
ASSOC_KEYWORD_WINDOW = 200     # 关键词共现窗口（归一化字符）
ASSOC_RESEARCH_KEYWORD_WINDOW = 320  # 研究意图放宽：更宽共现窗口，容纳跨段论述
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

# ── 领域同义/译名词库（关键词共现召回专用）──────────────────────────────────────
# 马列经典术语的近义词与不同译法。仅用于「关键词共现」把同一概念的多种表述并成一个「概念组」
# （组内 OR：命中任一即算命中该概念），从而让某段只用其中一种表述（如只写「外化」不写「异化」）
# 也能被共现召回。**不参与逐字片段/整句定位**（那两路要求逐字，扩同义会破坏精度）。
# 受控可编辑：每组务求「真同义 / 同一概念的不同译名或写法」，勿把「相关但不同」的概念并进来
# （宁缺毋滥——过度归并会把跑题段落召进候选、拉低精度）。作为种子，域内专家可按需增补。
ASSOC_DOCUMENT_PAGE_CAP = 90


TERM_THESAURUS: tuple[tuple[str, ...], ...] = (
    ("异化", "外化", "自我异化"),
    ("无产阶级", "工人阶级"),
    ("资产阶级", "有产阶级"),
    ("类本质", "类特性"),
    ("私有财产", "私有制"),
    ("拜物教", "商品拜物教"),
    ("生产资料", "生产手段"),
    ("辩证法", "辩证方法"),
    ("上层建筑", "观念上层建筑"),
)
# 归一化后的概念组 + 反查表（变体 → 组号），模块加载期算一次；单元素组无意义（等同不扩）故跳过。
_THESAURUS_GROUPS: list[list[str]] = []
_TERM_TO_GROUP: dict[str, int] = {}
for _grp in TERM_THESAURUS:
    _variants: list[str] = []
    for _v in _grp:
        _n = normalize(_v)
        if _n and _n not in _variants and _n not in _TERM_TO_GROUP:
            _variants.append(_n)
    if len(_variants) >= 2:
        _gid = len(_THESAURUS_GROUPS)
        _THESAURUS_GROUPS.append(_variants)
        for _n in _variants:
            _TERM_TO_GROUP[_n] = _gid

# ── 伪相关反馈（两趟检索）参数 ──────────────────────────────────────────────────
ASSOC_PRF_TOP_HITS = 6           # 从首轮前几条命中里取扩展源
ASSOC_PRF_SHINGLE_LENS = (6, 8)  # 反馈短语切片长度（偏长更具区分度）
ASSOC_PRF_SHINGLE_CAP = 8        # 单条命中最多切出的反馈短语
ASSOC_PRF_FRAG_CAP = 16          # 第二趟片段召回最多实检的反馈短语（控成本）

# 同段多词检索（标准检索的「同段多词」开关）：要求若干关键词全部出现在邻近段落窗口内。
# 纯 Python，复用共现滑窗；三重封顶约束在线请求成本。
COOC_PER_VOL = 30              # 单卷最多取的非重叠共现窗口数
COOC_TOTAL_CAP = 600          # 全语料最多取的共现命中数（超出标 truncated）
COOC_CTX_MAXLEN = 220         # 同段多词上下文片段最大字符数（以最密集关键词簇为中心）

# 导出不应复制首页为快速预览而裁剪的短窗口。工作节点在物化命中时改为
# 按句界扩展；但 OCR 丢失标点时不能让一个“句子”无界增长，因此保留硬上限。
EXPORT_SENTENCE_MAX_CHARS = 1200
EXPORT_PAGE_MAP_CACHE_SIZE = 8
_EXPORT_SENTENCE_END_RE = re.compile(
    r'(?:[。！？!?；;]|(?<!\d)\.(?!\d)|…{2,})+[”’」』》〉】〕）\]"]*'
)
_EXPORT_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t\u3000]*\n+")

def _fuzzy_allowed_errors(q_len: int) -> int:
    """按查询长度计算近似匹配允许的错字数（0 表示不做近似）。

    分两档：短/中查询从严（≤2 错，防常用字偶然重叠碰瓷、且短查询人工校对成本低）；
    长段落引文（≥FUZZY_LONG_QUERY_LEN）放宽——容错随长度线性增长、封顶
    FUZZY_LONG_MAX_ERRORS，以吸收整段文本里散布的 OCR 错字漏字与被误识的脚注残字，
    使「差不多一模一样」的长引文不再因几处错字而在所有书库全部漏检。
    """
    if q_len < MIN_FUZZY_QUERY_LEN:
        return 0
    if q_len < FUZZY_LONG_QUERY_LEN:
        # 取「短查询规则」与「长段落比例规则」的较大者：短查询保持原有从严值不变
        # （10 字仍 K=1、20–30 字仍 K=2），但 36 字以上按同一条 ≈8% 比例放宽。
        # 起因：40 字引文错 3 字（7.5%）在硬顶 K=2 下全库零命中，而同样错法的 60 字引文
        # 却能命中——2026-07-30 实测把 K 提到 3 即精准召回正确出处且只出 1 组、无误召。
        # 这也顺带抹平了 59 字 K=2 / 60 字 K=5 的断崖。
        return max(min(FUZZY_MAX_ERRORS, q_len // FUZZY_ERROR_STEP),
                   min(FUZZY_LONG_MAX_ERRORS, q_len // FUZZY_LONG_ERROR_STEP))
    return min(FUZZY_LONG_MAX_ERRORS, q_len // FUZZY_LONG_ERROR_STEP)


def _count_overlapping(haystack: str, needle: str, start: int, end: int) -> int:
    """统计 [start,end) 内 needle 的【重叠】出现次数（find 步进 +1，与 chapter_hits 同语义）。
    与 str.count 的【非重叠】计数不同：对周期串（如「一一」在「一一一」中）二者会不一致——
    聚合计数若用 str.count 会比钻取列出的命中数少，违反「聚合数=钻取数」不变量。"""
    if not needle:
        return 0
    n = 0
    while True:
        i = haystack.find(needle, start, end)
        if i < 0:
            break
        n += 1
        start = i + 1
    return n


def _substring_edit_distance(needle: str, haystack: str, max_errors: int) -> int | None:
    """needle 与 haystack 任意子串的最小编辑距离（半全局对齐）；超过 max_errors 返回 None。

    haystack 只取候选窗口加少量 padding（几十至几百字符），纯 Python DP 可承受。
    行最小值随行号单调不减，可据此提前终止。
    """
    n, m = len(needle), len(haystack)
    if n == 0:
        return 0
    prev = [0] * (m + 1)  # 空 needle 对任意起点：距离 0（起点空隙免费）
    for i in range(1, n + 1):
        ch = needle[i - 1]
        cur = [i] + [0] * m
        row_min = i
        for j in range(1, m + 1):
            cost = 0 if ch == haystack[j - 1] else 1
            v = prev[j - 1] + cost
            v2 = prev[j] + 1
            if v2 < v:
                v = v2
            v3 = cur[j - 1] + 1
            if v3 < v:
                v = v3
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > max_errors:
            return None
        prev = cur
    best = min(prev)
    return best if best <= max_errors else None


_NUMERIC_TITLE_RE = re.compile(r"^[0-9IVXLCDMivxlcdm\s\-—–\.]+$")
# 扫描车间遗留的「书签即扫描文件名」垃圾（如《全集》二版第40卷的
# "D:\扫书workshop\成果总\5A.tif"）：含盘符路径或以图片扩展名结尾的书签绝非真实篇目，
# 一律不作为可导航目录项（否则整卷目录会被几百条 tif 文件名淹没）。
_SCAN_ARTIFACT_TITLE_RE = re.compile(
    r"(?i)(?:^[a-z]:[\\/])|[\\/].+\.(?:tif|tiff|jpe?g|png|bmp|gif)$|\.(?:tif|tiff|jpe?g|png|bmp|gif)$"
)
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
    id: int | None = None
    page_label_info: dict | None = None


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


@dataclass(frozen=True)
class DocumentScope:
    """A single TOC work and its exact normalized-text range.

    This is deliberately derived from the local corpus rather than from the
    language model.  It is used by the AI research paths to turn an explicitly
    named work into a hard retrieval boundary.
    """

    book: str
    volume: int
    source_file: str
    title: str
    chapter_pdf_page: int
    norm_start: int
    norm_end: int
    authors: tuple[str, ...] = ()
    provenance_verified: bool = False
    # Generic retrieval cites the enclosing work rather than a nested TOC
    # subsection.  Keep that wider scope on a distinct id so an explicitly
    # requested subsection can still retain its original, exact hard boundary.
    citation_root: bool = False

    @property
    def document_id(self) -> str:
        suffix = "-work" if self.citation_root else ""
        return f"{self.source_file}#toc-{self.chapter_pdf_page}-{self.norm_start}{suffix}"

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "book": self.book,
            "volume": self.volume,
            "source_file": self.source_file,
            "work_title": self.title,
            "work_authors": list(self.authors),
            "provenance_verified": self.provenance_verified,
            "chapter_pdf_page": self.chapter_pdf_page,
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
        duplicate_labels: set[str] = set()
        for p in pages:
            parts.append(p.norm_text)
            offsets.append(offsets[-1] + len(p.norm_text))
            if p.printed_page:
                if p.printed_page in printed_to_pdf:
                    duplicate_labels.add(p.printed_page)
                else:
                    printed_to_pdf[p.printed_page] = p.pdf_page
        for label in duplicate_labels:
            printed_to_pdf.pop(label, None)
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
    fuzzy_errors: int | None = None  # 近似匹配时与查询的编辑距离（错字数）
    subject_label: str | None = None  # 命中来自名目索引时，记录索引词条（如「经济领域中的异化·劳动的异化」）
    citations: dict | None = None  # 多格式引文；缺省时回退为 citation 单一格式
    document_id: str = ""
    work_title: str = ""
    work_authors: tuple[str, ...] = ()
    provenance_verified: bool = False
    norm_start: int = 0
    norm_end: int = 0
    chapter_only: bool = False  # 篇章入口线索，没有匹配到请求的正文内容。
    exact_basis: str = "canonical"
    ignored_layout_types: list[str] = field(default_factory=list)
    layout_hit_ref: str = ""
    page_matches: list[dict] = field(default_factory=list)

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
            "page_ids": [p.id for p in self.pages],
            "page_id": self.pages[0].id if self.pages else None,
            "printed_pages": [p.printed_page for p in self.pages],
            "page_refs": [page_reference(p) for p in self.pages],
            "page_location": citation_pages(self.pages)["page"],
            "match_type": self.match_type,
            "score": self.score,
            "fuzzy_errors": self.fuzzy_errors,
            "context": self.context,
            "citation": self.citation,
            "citations": self.citations or {
                "gb2025": self.citation, "gb2015": self.citation,
                "zgshkx": self.citation, "mkszyj": self.citation,
            },
            "section_title": self.section_title,
            "subject_label": self.subject_label,
            "document_id": self.document_id,
            "work_title": self.work_title,
            "work_authors": list(self.work_authors),
            "provenance_verified": self.provenance_verified,
            "chapter_only": self.chapter_only,
            "exact_basis": self.exact_basis if self.match_type == "exact" else None,
            "ignored_layout_types": self.ignored_layout_types,
            "layout_hit_ref": self.layout_hit_ref,
            "page_matches": self.page_matches,
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
    fuzzy_errors: int | None = None  # 近似匹配组内最小错字数

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
            "fuzzy_errors": self.fuzzy_errors,
            "count": total,
            "page_size": page_size,
            "preview_count": preview_count,
            "hits": [hit.to_dict() for hit in self.hits],
        }


# ---------------------------------------------------------------------------
# 引用格式模板（可后台自定义）
# ---------------------------------------------------------------------------
# 多格式引文的默认模板：键与前端「引用格式」下拉一致。后台可对任一格式给出自定义模板覆盖
# 默认值（仅作用于「卷·页」型标准著作；公文/选编/显式 cite 覆盖等特殊体例不套模板）。
# 可用占位符（缺失/拼错会被替换为空串，绝不抛错）：
#   {title}      引文题名（如 马克思恩格斯文集 / 马克思恩格斯全集（第二版））
#   {volume}     卷次数字（如 1）
#   {place}      出版地（如 北京）
#   {publisher}  出版者（如 人民出版社）
#   {year}       出版年（如 2009；未知时为 xxxx）
#   {page}       脚注式页码串（如 第781页 / 第781-784页；印刷页缺失时含「（此为PDF页码，非原书印刷页码）」）
#   {page_range} 紧凑页码（独立编号及混合缺失页含区段和 PDF 说明）
#   {page_note}  页码脚注（印刷页缺失时为「（此为PDF页码，非原书印刷页码）」，否则空串）
# 默认模板务必与 _make_citation / _make_citation_gb 的程序化输出逐字一致（后台「恢复默认」据此）。
DEFAULT_CITATION_TEMPLATES: dict[str, str] = {
    # 2025 版刻意不提供推测性默认值。站长必须依据正式标准录入模板并通过
    # 黄金样例确认；在此之前前台隐藏该选项，避免把 2015 模板冒充 2025。
    "gb2025": "",
    "gb2015": "{title}:第{volume}卷[M].{place}:{publisher},{year}:{page_range}{page_note}.",
    "zgshkx": "《{title}》第{volume}卷，{place}：{publisher}，{year}年，{page}。",
    "mkszyj": "《{title}》第{volume}卷，{place}：{publisher}，{year}年，{page}。",
}


class _CiteSafeDict(dict):
    """format_map 用：未知占位符返回空串而非抛 KeyError，避免后台手滑写错占位符就崩引文。"""

    def __missing__(self, key: str) -> str:
        return ""


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------
class Corpus:
    def __init__(self, db_path: Path = DB_PATH, volumes_cfg_path: Path = VOLUMES) -> None:
        self.volumes_cfg: dict = yaml.safe_load(
            volumes_cfg_path.read_text(encoding="utf-8")
        )
        # 西马增量的出版年按卷从复核表合并；既有 volumes.yaml 保持只读，避免重写其它书库。
        reviewed_path = volumes_cfg_path.parent / "western_marxism_reviewed.yaml"
        if reviewed_path.exists():
            reviewed = yaml.safe_load(reviewed_path.read_text(encoding="utf-8")) or {}
            for row in reviewed.get("records") or []:
                if not isinstance(row, dict) or not row.get("key") or not row.get("year"):
                    continue
                self.volumes_cfg.setdefault(str(row["key"]), {})[int(row.get("volume") or 1)] = int(row["year"])
        # 公文类书库（党代会报告/全会公报）的引文元数据：报告人/真实篇名/全日期
        try:
            self.party_meta: dict = yaml.safe_load(
                (volumes_cfg_path.parent / "party_docs_meta.yaml").read_text(encoding="utf-8")
            ) or {}
        except Exception:
            self.party_meta = {}
        # 引用格式自定义模板覆盖：{format_key: template_str}，仅存后台改过的格式；空＝全用默认。
        # 由 app 层在启动时与保存后注入（set_citation_templates），单进程多线程共享、即时生效。
        self.citation_templates: dict[str, str] = {}
        self.book_configs: list[BookConfig] = load_book_configs()
        self.book_config_by_key: dict[str, BookConfig] = {book.key: book for book in self.book_configs}
        self.books: dict[str, list[Volume]] = {book.key: [] for book in self.book_configs}
        self._manifest_by_file: dict[str, dict] = {}
        self._files_by_book_volume: dict[tuple[str, int], list[str]] = {}
        self._volumes_by_source_file: dict[str, Volume] = {}
        self._toc_cache: dict[str, list[TocEntry]] = {}
        self._toc_db_entries: dict[str, list[TocEntry]] = {}
        self._date_span_cache: dict[tuple[str, int], str] = {}
        self._chapter_level_cache: dict[str, int] = {}
        self._segment_cache: dict[str, list[dict]] = {}
        self._document_scope_cache: dict[str, list[DocumentScope]] = {}
        self._document_scope_by_id: dict[str, DocumentScope] = {}
        self._document_citation_scope_by_id: dict[str, DocumentScope] = {}
        self._segment_lock = threading.Lock()
        self._chaptered_cache: "OrderedDict[tuple, dict]" = OrderedDict()
        self._chaptered_cache_lock = threading.Lock()
        # 读者页码反馈经 PDF 视觉核验后只进入独立内存覆盖层。底层 corpus.sqlite 保持只读；
        # 覆盖文件缺失/损坏或显式关闭时自动回退到底库，不阻断网站启动。
        # 覆盖文件属于当前代码发布包，不能沿用 volumes.yaml 的真实路径：蓝绿候选中的
        # volumes.yaml 可能是指向正式目录的符号链接，否则候选会错误读取旧目录配置。
        self.page_label_overrides = load_page_label_overrides()
        self._load_manifest()
        self._load(db_path)
        self._subject_entries: list[dict] = []
        self._load_subject_index(db_path.parent / "subject_index.sqlite")
        from layout_exact import LayoutIndex
        self.layout_index = LayoutIndex(self)

    @classmethod
    def load_default(cls) -> "Corpus":
        return cls()

    def _load_subject_index(self, path: Path) -> None:
        """加载名目索引主题库（可选）：编辑手工建的「概念→页码」权威映射，供 locate_subject_index 匹配。
        文件缺失或损坏时静默跳过（研究检索退回纯词面召回，不报错）。"""
        try:
            if not Path(path).exists():
                return
            conn = sqlite3.connect(str(path))
            rows = conn.execute(
                "SELECT book, volume, source_file, term, sub, full_label, "
                "norm_term, norm_label, printed_page, pdf_page FROM subject_index"
            ).fetchall()
            conn.close()
        except Exception:  # noqa: BLE001 — 辅助库不可用不应拖垮检索
            return
        self._subject_entries = [
            {"book": r[0], "volume": r[1], "source_file": r[2], "term": r[3], "sub": r[4],
             "full_label": r[5], "norm_term": r[6], "norm_label": r[7],
             "printed_page": r[8], "pdf_page": r[9]}
            for r in rows
        ]

    def _load_manifest(self) -> None:
        if not MANIFEST.exists():
            return
        manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
        # 新增 PDF 清单由专用白名单维护；只补入 manifest 中尚无的 key，不覆盖既有书目。
        source_path = MANIFEST.parent / "western_marxism_sources.yaml"
        if source_path.exists():
            sources = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
            for item in sources.get("books") or []:
                if not isinstance(item, dict) or not item.get("upload"):
                    continue
                target = manifest.setdefault(str(item["key"]), [])
                candidate = {
                    # Newly ingested books keep the user's original filename in
                    # ``file`` as provenance and publish through ``stored_file``.
                    # Registering both the original and stable names made one
                    # physical volume appear twice; the provenance alias then
                    # had no corpus rows, cover, TOC, or AI page context.
                    "file": item.get("stored_file") or item.get("file"),
                    "volume": int(item.get("volume") or 1),
                    "display_title": item.get("display_title") or item.get("key"),
                }
                if not any(entry.get("file") == candidate["file"] and entry.get("volume") == candidate["volume"] for entry in target):
                    target.append(candidate)
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
                "SELECT book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text, id "
                "FROM pages ORDER BY book, volume, source_file, pdf_page"
            ).fetchall()
        else:
            raw_rows = conn.execute(
                "SELECT book, volume, pdf_page, printed_page, raw_text, normalized_text, id "
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
                    page_id,
                )
                for book, volume, pdf_page, printed_page, raw_text, norm_text, page_id in raw_rows
            ]
        self._toc_db_entries = self._load_toc_entries_from_db(conn)
        page_evidence = load_page_evidence(conn)
        conn.close()

        for (book, vol, source_file), grp in groupby(rows, key=lambda r: (r[0], r[1], r[2])):
            pgs = [
                Page(pdf_page=r[3], printed_page=r[4], raw_text=r[5], norm_text=r[6], id=r[7])
                for r in grp
            ]
            apply_page_evidence(source_file, pgs, page_evidence)
            apply_page_label_overrides(source_file, pgs, self.page_label_overrides)
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

    def _scoped_book_keys(self, book_scope: "Collection[str] | None") -> list[str]:
        """按「检索范围」过滤要扫描的书库键（保持 self.books 的原有顺序）。

        ``book_scope is None`` → 全部书库（默认，向后兼容）；给定集合、或 ``{书库键: 允许卷号集合|None}``
        映射时，只保留其中真实存在的书库键（映射按其键判定；卷级过滤另见 _scoped_volumes）。供联想/研究/
        单本检索按范围定向召回时把整个检索预算花在范围内、避免其它作者的强命中霸榜。
        """
        if book_scope is None:
            return list(self.books.keys())
        scope = {str(b) for b in book_scope}  # dict 迭代得键、集合/列表得元素——两者皆归到书库键集合
        return [b for b in self.books if b in scope]

    @staticmethod
    def _scope_cache_key(book_scope: "Collection[str] | None") -> tuple:
        if book_scope is None:
            return ("*",)
        if isinstance(book_scope, dict):
            rows = []
            for book, volumes in book_scope.items():
                rows.append((str(book), None if volumes is None else tuple(sorted(int(v) for v in volumes))))
            return ("map", *sorted(rows))
        return ("books", *sorted(str(book) for book in book_scope))

    def _scoped_volumes(self, book: str, book_scope: "Collection[str] | None" = None) -> list["Volume"]:
        """某书库在「检索范围」内的卷列表（供各扫描函数把逐卷循环限定到范围内的卷）。

        ``book_scope`` 为 ``{书库键: 允许卷号集合 或 None}`` 映射时按卷号过滤（None=该书全部卷）；为 None 或
        纯书库键集合时返回该书全部卷（无卷级约束，向后兼容）。仅对已在范围内的书库调用。
        """
        vols = self.books.get(book, [])
        if isinstance(book_scope, dict):
            allowed = book_scope.get(book)
            if allowed is not None:
                allowed_set = {int(v) for v in allowed}
                return [v for v in vols if v.volume in allowed_set]
        return list(vols)

    def _volume_in_scope(self, vol: "Volume", book_scope: "Collection[str] | None" = None) -> bool:
        """判定单个卷是否落在「检索范围」内（书库 + 可选卷级）。用于按 source_file 拿到卷后的过滤。"""
        if book_scope is None:
            return True
        if vol.book not in {str(b) for b in book_scope}:
            return False
        if isinstance(book_scope, dict):
            allowed = book_scope.get(vol.book)
            if allowed is not None and vol.volume not in {int(v) for v in allowed}:
                return False
        return True

    def get_toc_entries(self, source_file: str) -> list[TocEntry]:
        source_file = self._normalize_source_file(source_file)
        if not source_file:
            return []
        if source_file not in self._toc_cache:
            volume = self.get_volume_by_source_file(source_file)
            from_db = self._toc_db_entries.get(source_file) or []
            self._toc_cache[source_file] = from_db or (self._build_toc_entries(volume) if volume else [])
        return self._toc_cache[source_file]

    # 「本册收录文献的时间跨度」：从目录篇名的日期括注（如「（一九六五年一月一日）」）取
    # 最早/最晚，供卷列表卡片显示（如「1965.1—1965.12」）。零的写法有 〇/○/零 三种混用，
    # 全部认；「附编/附件/附录/附:」是补充材料，其日期不代表该册收录时段，故排除——否则
    # 建国第4册会因一条 1951 年附件显示成「1951.1—1953.12」，实际该册是 1953 年。
    _SPAN_ZERO = "零〇○０0"
    _SPAN_DIGITS = "一二三四五六七八九十" + _SPAN_ZERO
    _SPAN_DATE_RE = re.compile(
        r"[（(]\s*([" + _SPAN_DIGITS + r"]{4})\s*年(?:\s*([" + _SPAN_DIGITS + r"]{1,3})\s*月)?")
    # 年谱类书库的目录条目是「1898年　诞生」「1966年（10月—12月）」这种阿拉伯数字纪年，
    # 没有中文日期括注，上面那条认不出 → 年谱卷列表的时间跨度会全空，而年谱恰恰最需要
    # 显示「这一卷管哪几年」。故补一条阿拉伯数字年（可带月）的模式。
    # 第3组捕获月份区间的末月（「1937年(7月—12月)」→ 7 与 12），供跨度上界使用。
    _SPAN_DATE_ARABIC_RE = re.compile(
        r"\b(1[89]\d{2}|20\d{2})\s*年"
        r"(?:\s*[（(]?\s*(\d{1,2})\s*月(?:\s*[-—–~－至]\s*(\d{1,2})\s*月)?)?")
    _SPAN_APPENDIX_RE = re.compile(r"^(附编|附件|附录|附[:：])")
    _SPAN_CN_NUM = {"零": 0, "〇": 0, "○": 0, "０": 0, "0": 0, "一": 1, "二": 2, "三": 3,
                    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

    @classmethod
    def _span_cn_to_int(cls, text: str) -> int | None:
        s = (text or "").strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        if "十" in s:                     # 十/十一/十二
            head, _, tail = s.partition("十")
            return (cls._SPAN_CN_NUM.get(head, 1) if head else 1) * 10 + \
                   (cls._SPAN_CN_NUM.get(tail, 0) if tail else 0)
        value = 0
        for ch in s:
            if ch not in cls._SPAN_CN_NUM:
                return None
            value = value * 10 + cls._SPAN_CN_NUM[ch]
        return value or None

    def volume_date_span(self, book: str, volume: int) -> str:
        """该卷/册收录文献的时间跨度，形如「1965.1—1965.12」；无法判定时返回空串。"""
        key = (book, int(volume))
        if key in self._date_span_cache:
            return self._date_span_cache[key]
        vol = next((v for v in self.get_volumes(book) if v.volume == volume), None)
        dates: list[tuple[int, int]] = []
        if vol is not None:
            for entry in self.get_toc_entries(vol.source_file):
                title = str(getattr(entry, "title", "") or "").lstrip()
                if not title or self._SPAN_APPENDIX_RE.match(title):
                    continue
                m = self._SPAN_DATE_RE.search(title)
                arabic = m is None
                if arabic:
                    m = self._SPAN_DATE_ARABIC_RE.search(title)
                if not m:
                    continue
                year = int(m.group(1)) if arabic else self._span_cn_to_int(m.group(1))
                # 年谱起于 1893（毛泽东诞生），故下限放到 1800 而非 1900
                if not year or not (1800 <= year <= 2100):
                    continue
                month = (int(m.group(2)) if m.group(2) else None) if arabic \
                    else (self._span_cn_to_int(m.group(2)) if m.group(2) else None)
                # 年谱目录条目常是「1937年(7月—12月)」这种**月份区间**。跨度的下界该取区间首月、
                # 上界该取区间末月；若只取首月，卷末那条「1945年(1月—8月)」会被记成 1945.1，
                # 于是毛年谱第2卷显示成「1937.7—1945.1」——比不显示更糟（日期是错的）。
                end_month = int(m.group(3)) if (arabic and m.lastindex and m.lastindex >= 3
                                                and m.group(3)) else None
                sm = month if month and 1 <= month <= 12 else 0
                em = end_month if end_month and 1 <= end_month <= 12 else sm
                dates.append((year, sm, em))
        span = ""
        if dates:
            fmt = lambda y, mo: f"{y}.{mo}" if mo else str(y)
            lo_y, lo_m, _ = min(dates, key=lambda d: (d[0], d[1]))
            hi_y, _, hi_m = max(dates, key=lambda d: (d[0], d[2]))
            lo, hi = fmt(lo_y, lo_m), fmt(hi_y, hi_m)
            span = lo if lo == hi else f"{lo}—{hi}"
        self._date_span_cache[key] = span
        return span

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

    _WORK_AUTHOR_ALIASES: tuple[tuple[str, str], ...] = (
        ("卡·马克思", "马克思"), ("卡尔·马克思", "马克思"), ("马克思", "马克思"),
        ("弗·恩格斯", "恩格斯"), ("弗里德里希·恩格斯", "恩格斯"), ("恩格斯", "恩格斯"),
        ("弗·伊·列宁", "列宁"), ("列宁", "列宁"), ("斯大林", "斯大林"),
        ("毛泽东", "毛泽东"), ("刘少奇", "刘少奇"), ("周恩来", "周恩来"),
        ("邓小平", "邓小平"), ("江泽民", "江泽民"), ("胡锦涛", "胡锦涛"),
        ("习近平", "习近平"), ("陈独秀", "陈独秀"), ("李大钊", "李大钊"),
    )

    @classmethod
    def _authors_from_toc_title(cls, title: str) -> tuple[str, ...]:
        """Read only explicit responsibility statements at a TOC title's edges.

        Names merely mentioned inside a title (for example ``在马克思墓前的讲话``)
        are intentionally ignored; treating them as authors is the exact attribution
        error this layer is meant to prevent.
        """

        compact = " ".join(str(title or "").split()).strip()
        if not compact:
            return ()
        found: list[str] = []
        for alias, canonical in cls._WORK_AUTHOR_ALIASES:
            # Several legacy TOCs separate the responsibility statement from
            # the work title with a full stop rather than a space.  Preface
            # labels also commonly run the name straight into “第二版序言”.
            leading = bool(re.match(
                rf"^{re.escape(alias)}(?=$|[\s。:：]|(?:第[^\s]{{0,12}})?(?:序言|前言|导言|跋|说明))",
                compact,
            ))
            trailing = re.search(
                rf"(?:[)）\]】]|\s){re.escape(alias)}\s*$", compact
            )
            if (leading or trailing) and canonical not in found:
                found.append(canonical)
        return tuple(found)

    def _verified_work_authors(self, book: str, volume: int, title: str) -> tuple[str, ...]:
        meta = ((self.party_meta.get(book, {}) or {}).get(volume, {}) or {})
        author = str(meta.get("author") or "").strip()
        if author:
            return tuple(part.strip() for part in re.split(r"[、，,和与]", author) if part.strip())
        configured = tuple(self.get_book_config(book).authors or ())
        if configured:
            return configured
        # These collections consist of the named author's writings.  Chronologies
        # are deliberately excluded because their narrative is editorial prose.
        fixed = {
            "列宁全集": ("列宁",), "斯大林全集": ("斯大林",),
            "李大钊全集": ("李大钊",), "陈独秀文集": ("陈独秀",),
            "毛泽东选集": ("毛泽东",), "毛泽东文集": ("毛泽东",),
            "刘少奇选集": ("刘少奇",), "周恩来选集": ("周恩来",),
            "陈云文集": ("陈云",), "邓小平文选": ("邓小平",),
            "江泽民文选": ("江泽民",), "胡锦涛文选": ("胡锦涛",),
            "治国理政": ("习近平",), "习近平经济文选": ("习近平",),
            "习近平著作选读": ("习近平",),
        }.get(book)
        return fixed or self._authors_from_toc_title(title)

    @classmethod
    def _clean_work_title(cls, title: str) -> str:
        cleaned = " ".join(str(title or "").split()).strip().strip("《》")
        for alias, _canonical in cls._WORK_AUTHOR_ALIASES:
            cleaned = re.sub(
                rf"^{re.escape(alias)}(?:\s*[、和与]\s*[^\s。:：]{{2,12}})?[\s。:：]+",
                "",
                cleaned,
            )
            cleaned = re.sub(
                rf"(?P<close>[)）\]】]|\s){re.escape(alias)}\s*$",
                lambda match: match.group("close") if not match.group("close").isspace() else "",
                cleaned,
            )
        return cleaned.strip(" ，,、:：") or " ".join(str(title or "").split()).strip()

    @staticmethod
    def _entry_offset(vol: Volume, entry: TocEntry) -> int:
        pages = vol.pages
        if not pages:
            return 0
        pdf_pages = [page.pdf_page for page in pages]
        page_index = bisect.bisect_left(pdf_pages, int(entry.pdf_page or 1))
        page_index = max(0, min(page_index, len(pages) - 1))
        page_start, page_end = vol.page_offsets[page_index], vol.page_offsets[page_index + 1]
        title_norm = normalize(entry.title)
        if len(title_norm) >= 4:
            exact = vol.norm_full.find(title_norm, page_start, page_end)
            if exact >= 0:
                return exact
            # Long TOC labels often include a date/author suffix absent from the
            # printed heading.  A long leading fragment is still a safe boundary.
            for size in (32, 24, 16, 12):
                if len(title_norm) < size:
                    continue
                exact = vol.norm_full.find(title_norm[:size], page_start, page_end)
                if exact >= 0:
                    return exact
        return page_start

    def _document_scopes(self, vol: Volume) -> list[DocumentScope]:
        cached = self._document_scope_cache.get(vol.source_file)
        if cached is not None:
            return cached
        entries = [entry for entry in self.get_toc_entries(vol.source_file) if entry.kind in {"body", "letter"}]
        entries.sort(key=lambda entry: (entry.pdf_page, entry.sort_order, entry.level))
        scopes: list[DocumentScope] = []
        indexed_scopes: list[tuple[int, DocumentScope]] = []
        for index, entry in enumerate(entries):
            scope = self._document_scope_from_entry(vol, entry, entries, index)
            if scope is None:
                continue
            scopes.append(scope)
            indexed_scopes.append((index, scope))
            self._document_scope_by_id[scope.document_id] = scope

        # A TOC often records both a complete work and its numbered internal
        # sections.  The exact scopes above are needed for explicit title
        # restriction, but generic AI evidence should attribute a quotation to
        # the enclosing work (for example 《工资、价格和利润》), not to
        # “13.争取提高工资……” as though that subsection were an independent
        # publication.  Build a second, wider attribution map without changing
        # the explicit-title scopes.
        exact_by_index = {index: scope for index, scope in indexed_scopes}
        root_by_index = self._citation_root_entry_indices(entries)

        members_by_root: dict[int, list[DocumentScope]] = {}
        for index, scope in indexed_scopes:
            root_index = root_by_index.get(index, index)
            members_by_root.setdefault(root_index, []).append(scope)

        citation_roots: dict[int, DocumentScope] = {}
        for root_index, members in members_by_root.items():
            root_scope = exact_by_index.get(root_index)
            if root_scope is None:
                continue
            citation_title = root_scope.title
            if self._use_whole_book_title_for_citation(vol.book):
                config = self.get_book_config(vol.book)
                citation_title = str(config.citation_title or config.title or vol.book).strip("《》 ")
            citation_scope = DocumentScope(
                book=root_scope.book,
                volume=root_scope.volume,
                source_file=root_scope.source_file,
                title=citation_title,
                chapter_pdf_page=root_scope.chapter_pdf_page,
                norm_start=root_scope.norm_start,
                norm_end=max(member.norm_end for member in members),
                authors=root_scope.authors,
                provenance_verified=root_scope.provenance_verified,
                citation_root=True,
            )
            citation_roots[root_index] = citation_scope
            self._document_scope_by_id[citation_scope.document_id] = citation_scope

        for index, scope in indexed_scopes:
            citation_scope = citation_roots.get(root_by_index.get(index, index))
            if citation_scope is not None:
                self._document_citation_scope_by_id[scope.document_id] = citation_scope
        self._document_scope_cache[vol.source_file] = scopes
        return scopes

    def _use_whole_book_title_for_citation(self, book: str) -> bool:
        """Use a monograph's title instead of its chapter/preface TOC label."""
        config = self.get_book_config(book)
        if bool(config.single_volume) or book == "资本论":
            return True
        title = str(config.citation_title or "").strip()
        collection_suffixes = (
            "全集", "选集", "文集", "文选", "年谱", "选编", "公报", "报告", "著作选读",
        )
        return bool(config.authors) and not title.endswith(collection_suffixes)

    @classmethod
    def _looks_like_subordinate_toc_title(cls, title: str) -> bool:
        """Whether a flat TOC label looks like an internal numbered heading."""
        text = " ".join(str(title or "").split()).strip()
        if not text:
            return False
        if re.match(r"^[\[【（(]?\s*\d{1,3}\s*[.．、:：)）\]-]?", text):
            return True
        if re.match(r"^[一二三四五六七八九十百]+(?:\s*[、.．:：)）]|\s+)", text):
            return True
        return bool(re.match(r"^(?:\[?引言\]?|几点说明|附录(?:一|二|三|四|五|六|七|八|九|十|\d+)?)$", text))

    @staticmethod
    def _looks_like_auxiliary_toc_title(title: str) -> bool:
        """Illustration/facsimile labels embedded inside a work's flat TOC."""
        text = " ".join(str(title or "").split()).strip()
        return bool(re.search(
            r"(?:手稿第\s*\d+\s*页|封面|部分译文|扉页|书影|照片|画像|插图)", text,
        ))

    def _citation_root_entry_indices(self, entries: list[TocEntry]) -> dict[int, int]:
        """Choose enclosing works in one linear TOC pass (then cache per volume)."""
        roots: dict[int, int] = {}
        ancestors: list[int] = []
        last_independent: int | None = None
        for index, entry in enumerate(entries):
            level = int(entry.level or 1)
            while ancestors and int(entries[ancestors[-1]].level or 1) >= level:
                ancestors.pop()

            has_responsibility = bool(self._authors_from_toc_title(entry.title))
            subordinate = self._looks_like_subordinate_toc_title(entry.title)
            auxiliary = self._looks_like_auxiliary_toc_title(entry.title)
            if has_responsibility:
                root = index
            else:
                authored_ancestor = next(
                    (
                        ancestor for ancestor in reversed(ancestors)
                        if self._authors_from_toc_title(entries[ancestor].title)
                    ),
                    None,
                )
                if authored_ancestor is not None:
                    root = authored_ancestor
                elif ancestors:
                    parent = ancestors[-1]
                    root = roots.get(parent, parent)
                elif auxiliary and last_independent is not None:
                    root = roots.get(last_independent, last_independent)
                elif subordinate and last_independent is not None:
                    # Flat legacy TOC: numbered subsection follows its work at
                    # the same nominal level.
                    root = roots.get(last_independent, last_independent)
                else:
                    root = index
            roots[index] = root
            ancestors.append(index)
            if (not subordinate and not auxiliary) or has_responsibility:
                last_independent = index
        return roots

    def _document_scope_from_entry(
        self, vol: Volume, entry: TocEntry, entries: list[TocEntry], index: int,
    ) -> DocumentScope | None:
        start = self._entry_offset(vol, entry)
        end = len(vol.norm_full)
        for later in entries[index + 1:]:
            # Numbered descendants can be mislabeled ``letter``. They remain
            # inside their parent; independently indexed letters still form a
            # boundary even in legacy outlines with inconsistent levels.
            independent_letter = later.kind == "letter" and not self._looks_like_subordinate_toc_title(later.title)
            if independent_letter or int(later.level or 1) <= int(entry.level or 1):
                end = self._entry_offset(vol, later)
                break
        if end <= start:
            return None
        authors = self._verified_work_authors(vol.book, vol.volume, entry.title)
        scope = DocumentScope(
            book=vol.book,
            volume=vol.volume,
            source_file=vol.source_file,
            title=self._clean_work_title(entry.title),
            chapter_pdf_page=int(entry.pdf_page or 1),
            norm_start=start,
            norm_end=end,
            authors=authors,
            provenance_verified=bool(authors),
        )
        self._document_scope_by_id[scope.document_id] = scope
        return scope

    def document_scope_for_page(self, source_file: str, pdf_page: int) -> DocumentScope | None:
        vol = self.get_volume_by_source_file(source_file)
        if not vol or not vol.pages:
            return None
        page = next((p for p in vol.pages if int(p.pdf_page) == int(pdf_page)), None)
        if page is None:
            return None
        page_index = vol.pages.index(page)
        offset = vol.page_offsets[page_index]
        containing = [scope for scope in self._document_scopes(vol) if scope.norm_start <= offset < scope.norm_end]
        return max(containing, key=lambda scope: scope.norm_start) if containing else None

    def document_scope_for_offset(self, source_file: str, norm_offset: int) -> DocumentScope | None:
        vol = self.get_volume_by_source_file(source_file)
        if not vol:
            return None
        containing = [
            scope for scope in self._document_scopes(vol)
            if scope.norm_start <= int(norm_offset) < scope.norm_end
        ]
        return max(containing, key=lambda scope: scope.norm_start) if containing else None

    def citation_document_scope_for_offset(
        self, source_file: str, norm_offset: int,
    ) -> DocumentScope | None:
        """Return the enclosing work name/range used for generic AI citations."""
        exact = self.document_scope_for_offset(source_file, norm_offset)
        if exact is None:
            return None
        return self._document_citation_scope_by_id.get(exact.document_id, exact)

    def document_scope_for_hit(self, hit: Hit) -> DocumentScope | None:
        document_id = str(getattr(hit, "document_id", "") or "")
        if document_id:
            cached = self._document_scope_by_id.get(document_id)
            if cached is not None:
                return cached
        norm_start = getattr(hit, "norm_start", None)
        if norm_start is not None:
            return self.document_scope_for_offset(
                str(getattr(hit, "source_file", "") or ""), int(norm_start),
            )
        pages = getattr(hit, "pages", None) or []
        if not pages:
            return None
        return self.document_scope_for_page(
            str(getattr(hit, "source_file", "") or ""), pages[0].pdf_page,
        )

    def _is_collection_title(self, title: str) -> bool:
        target = normalize(title)
        if not target:
            return False
        for config in self.book_configs:
            aliases = (config.key, config.title, config.short_title, config.citation_title)
            if any(target == normalize(alias) for alias in aliases if alias):
                return True
        return False

    def _title_resolution_entries(self, vol: Volume, *, allow_pdf_fallback: bool = False) -> list[TocEntry]:
        """Return already-indexed TOC data without opening hundreds of PDFs."""

        source_file = self._normalize_source_file(vol.source_file)
        entries = self._toc_db_entries.get(source_file) or self._toc_cache.get(source_file) or []
        if entries or not allow_pdf_fallback:
            return entries
        # A manually selected book/volume is a small, explicit scope.  In that
        # case it is acceptable to build the missing PDF outline lazily; a global
        # title lookup must never parse every PDF on the request path.
        return self.get_toc_entries(source_file)

    @classmethod
    def _work_title_aliases(cls, title: str) -> tuple[str, ...]:
        """Return conservative aliases suitable for an explicit-title check."""

        cleaned = cls._clean_work_title(title)
        aliases = [cleaned]
        # TOCs commonly append a date, ``节选`` note, or a subtitle that users do
        # not type.  Keep only a substantial leading title; never derive aliases
        # from arbitrary words inside the heading.
        for separator in ("——", "—"):
            if separator in cleaned:
                aliases.append(cleaned.split(separator, 1)[0].strip())
        without_note = re.sub(r"\s*[（(][^（）()]{2,40}[）)]\s*$", "", cleaned).strip()
        if without_note != cleaned:
            aliases.append(without_note)
        result: list[str] = []
        for alias in aliases:
            value = " ".join(alias.split()).strip(" ，,、:：")
            if len(normalize(value)) >= 4 and value not in result:
                result.append(value)
        return tuple(result)

    @classmethod
    def _is_excerpt_work_title(cls, title: str) -> bool:
        """Whether a TOC title explicitly describes an excerpt, not a full work."""

        value = normalize(cls._clean_work_title(title))
        return any(marker in value for marker in ("节选", "选段", "摘选", "摘录", "片段", "片断"))


    def _unbracketed_explicit_work_titles(
        self, query: str, book_scope: "Collection[str] | None",
    ) -> list[str]:
        """Recognize titles written without 《》 only in explicit source syntax.

        Merely mentioning words that happen to be a heading must not narrow the
        search.  We therefore require a source-intent verb next to the complete
        TOC-derived alias and, except for ``指定/限定``, a phrase such as ``一文``
        or ``中`` immediately after it.
        """

        del book_scope  # Extraction is syntactic; corpus matching happens below.
        text = " ".join(str(query or "").split())
        if not text:
            return []
        patterns = (
            # Examples: ``根据论新阶段一文`` / ``分析论新阶段中的统一战线``.
            re.compile(
                r"(?:依据|根据|结合|引用|摘引|分析|解读|研读|阅读|讨论)\s*"
                r"(?P<title>[^，。！？；：、《》\n]{2,60}?)"
                r"(?:一文|这篇文章|该文|文中|中|里|的(?:论述|观点|思想|内容))"
            ),
            # ``指定/限定`` already states the user's intent and may end at the title.
            re.compile(
                r"(?:指定|限定)\s*(?P<title>[^，。！？；：、《》\n]{2,60}?)"
                r"(?=$|[，。！？；：])"
            ),
        )
        result: list[str] = []
        for pattern in patterns:
            for match in pattern.finditer(text):
                title = match.group("title").strip(" ，,、:：的")
                if len(normalize(title)) >= 4 and title not in result and not self._is_collection_title(title):
                    result.append(title)
        return result[:4]

    def resolve_document_scopes(
        self, query: str, *, book_scope: "Collection[str] | None" = None,
        allow_title_subject: bool = False,
    ) -> dict:
        """Resolve explicitly named work titles to deterministic corpus ranges.

        Collection titles such as ``《马克思恩格斯文集》`` remain book-level
        hints.  A work title that is explicitly named but absent from the selected
        scope fails closed instead of silently widening to the whole corpus.
        """

        requested: list[str] = []
        for raw in re.findall(r"《([^》\n]{2,100})》", str(query or "")):
            title = " ".join(raw.split()).strip()
            if title and not self._is_collection_title(title) and title not in requested:
                requested.append(title)
        if not requested:
            requested.extend(self._unbracketed_explicit_work_titles(query, book_scope))
        if not requested and allow_title_subject:
            # A known work can itself be the subject: “共产党宣言中描述…”.
            # Validate against real TOC titles so “现实生活中描述…” is not
            # mistaken for a work. Legacy/research callers retain their syntax.
            match = re.match(
                r"^(?:请(?:找出|查找|检索)?\s*)?(?P<title>[^，。！？；：、《》\n]{4,60}?)"
                r"(?:一文中|文中|中|里)(?:的|关于|描述|论述|提到|谈到|讲述|指出|有关|对)",
                str(query or "").strip(),
            )
            if match:
                title = match.group("title").strip()
                if not self._is_collection_title(title):
                    known = self.resolve_document_scopes(f"《{title}》", book_scope=book_scope)
                    if known["status"] == "not_found" and book_scope is not None:
                        known = self.resolve_document_scopes(f"《{title}》")
                    known_titles = [scope.title for scope in known.get("scopes", [])]
                    known_titles.extend(item.get("work_title", "") for item in known.get("candidates", []))
                    if known["status"] in {"resolved", "ambiguous"} and any(
                        normalize(value) == normalize(title) for value in known_titles
                    ):
                        requested.append(title)
        if not requested:
            return {"status": "none", "requested_titles": [], "scopes": [], "candidates": []}

        resolved: list[DocumentScope] = []
        display_candidates: list[dict] = []
        for requested_title in requested[:4]:
            lookup_title = self._clean_work_title(requested_title)
            target = normalize(lookup_title)
            raw_probe = re.sub(r"[\s，,。、:：()（）\[\]【】]", "", lookup_title)[:2]
            matches: list[tuple[int, int, int, DocumentScope]] = []
            for book in self._scoped_book_keys(book_scope):
                for vol in self._scoped_volumes(book, book_scope):
                    entries = [
                        entry for entry in self._title_resolution_entries(
                            vol, allow_pdf_fallback=book_scope is not None,
                        )
                        if entry.kind in {"body", "letter"}
                    ]
                    entries.sort(key=lambda entry: (entry.pdf_page, entry.sort_order, entry.level))
                    for entry_index, entry in enumerate(entries):
                        # The probe is the first two substantive title characters;
                        # TOC punctuation almost never separates them.  This cheap
                        # check avoids running full Unicode normalization over every
                        # heading in the corpus on each question.
                        if raw_probe and raw_probe not in str(entry.title or "") and raw_probe not in "".join(str(entry.title or "").split()):
                            continue
                        candidate_title = self._clean_work_title(entry.title)
                        candidate = normalize(candidate_title)
                        if not candidate or target not in candidate:
                            continue
                        strength = 4 if candidate == target else (3 if candidate.startswith(target) else 2)
                        scope = self._document_scope_from_entry(vol, entry, entries, entry_index)
                        if scope is None:
                            continue
                        matches.append((strength, -abs(len(candidate) - len(target)), -int(scope.chapter_pdf_page), scope))
            if not matches:
                return {
                    "status": "not_found", "requested_titles": requested,
                    "missing_title": requested_title, "scopes": [], "candidates": [],
                }
            best_strength = max(item[0] for item in matches)
            strongest = [item for item in matches if item[0] == best_strength]
            # A work may have both a parent TOC heading (including prefaces)
            # and a nested body heading with the same title. They identify one
            # work in this edition, regardless of the caller's title syntax.
            unique = {}
            for item in strongest:
                scope = item[3]
                unique.setdefault((scope.source_file, normalize(scope.title),
                                   scope.norm_start, scope.norm_end), item)
            strongest = [item for item in unique.values() if not any(
                other[3].source_file == item[3].source_file
                and normalize(other[3].title) == normalize(item[3].title)
                and other[3].norm_start <= item[3].norm_start
                and other[3].norm_end >= item[3].norm_end
                and (other[3].norm_start, other[3].norm_end) != (item[3].norm_start, item[3].norm_end)
                for other in unique.values()
            )]
            # Choose the configured authoritative edition first. Duplicated TOC
            # entries in an unselected edition must not block that decision.
            per_source: dict[str, tuple[int, int, int, DocumentScope]] = {}
            for item in strongest:
                scope = item[3]
                previous = per_source.get(scope.source_file)
                if previous is None or item[:3] > previous[:3]:
                    per_source[scope.source_file] = item
            # With no manually selected collection, prefer an explicitly complete
            # edition over a TOC entry labelled as an excerpt.  Previously the
            # configured collection order could select a nine-page ``节选`` even
            # when the complete work was available in another authoritative
            # collection, starving the subsequent grounded-answer pipeline.  A
            # manual book/volume choice and an explicit request for an excerpt
            # retain the old authoritative-edition ordering exactly.
            prefer_complete = book_scope is None and not self._is_excerpt_work_title(requested_title)
            chosen = min(
                (item[3] for item in per_source.values()),
                key=lambda scope: (
                    int(prefer_complete and self._is_excerpt_work_title(scope.title)),
                    self.book_sort_order(scope.book), scope.volume, scope.chapter_pdf_page,
                ),
            )
            same_source_duplicates = [
                item[3] for item in strongest
                if item[3].source_file == chosen.source_file
                and normalize(item[3].title) == normalize(chosen.title)
            ]
            # Disjoint identically named works within the selected edition are
            # still ambiguous; never combine their ranges or widen user scope.
            if len(same_source_duplicates) > 1:
                candidates = sorted(
                    {scope.document_id: scope.to_dict() for scope in same_source_duplicates}.values(),
                    key=lambda item: (self.book_sort_order(item["book"]), item["volume"], item["chapter_pdf_page"]),
                )
                return {
                    "status": "ambiguous", "requested_titles": requested,
                    "ambiguous_title": requested_title, "scopes": [], "candidates": candidates,
                }
            resolved.append(chosen)
            display_candidates.append(chosen.to_dict())
        return {
            "status": "resolved", "requested_titles": requested,
            "scopes": resolved, "candidates": display_candidates,
        }

    def _apply_document_scope(self, hit: Hit, scope: DocumentScope) -> Hit:
        hit.document_id = scope.document_id
        hit.work_title = scope.title
        hit.work_authors = scope.authors
        hit.provenance_verified = scope.provenance_verified
        return hit

    def enrich_hit_document(self, hit: Hit) -> Hit:
        """Attach work provenance only for AI evidence selected for presentation."""

        if hit.document_id:
            return hit
        scope = self.citation_document_scope_for_offset(hit.source_file, hit.norm_start)
        return self._apply_document_scope(hit, scope) if scope else hit

    def locate_associative_in_documents(
        self,
        scopes: Collection[DocumentScope],
        *,
        quotes: list[str],
        keywords: list[str],
        fragments: list[str] | None = None,
        facets: list[list[str]] | None = None,
        candidate_cap: int = ASSOC_CANDIDATE_CAP,
        clip_context: bool = False,
        deadline: float | None = None,
    ) -> list[Hit]:
        """Associative retrieval constrained to explicit work ranges."""

        best_by_page: dict[tuple[str, int], Hit] = {}

        def add(scope: DocumentScope, vol: Volume, start: int, end: int, score: int, anchor: str) -> None:
            if not (scope.norm_start <= start < end <= scope.norm_end):
                return
            hit = self._apply_document_scope(
                self._make_hit(vol, start, end, "exact", min(100, score), anchor), scope
            )
            if clip_context:
                clipped_pages = []
                for page in hit.pages:
                    page_index = vol.pages.index(page)
                    page_start = vol.page_offsets[page_index]
                    lo = max(0, scope.norm_start - page_start)
                    hi = min(len(page.norm_text), scope.norm_end - page_start)
                    mapping = self._export_page_raw_map(page, None)
                    if mapping is None or hi <= lo:
                        continue
                    raw = page.raw_text[mapping[0][lo]:mapping[1][hi - 1]]
                    clipped_pages.append(Page(page.pdf_page, page.printed_page, raw, normalize(raw), page.id, page.page_label_info))
                hit.context = self._extract_context(clipped_pages, anchor) if clipped_pages else ""
            page = hit.pages[0].pdf_page if hit.pages else scope.chapter_pdf_page
            key = (scope.document_id, int(page))
            previous = best_by_page.get(key)
            if previous is None or hit.score > previous.score:
                best_by_page[key] = hit

        normalized_quotes = [(str(q or ""), normalize(str(q or ""))) for q in (quotes or [])[:ASSOC_MAX_QUOTES]]
        fragment_pool = list(fragments or [])
        for quote, _normalized in normalized_quotes:
            fragment_pool.extend(self._shingle_fragments(quote))
        normalized_fragments: list[str] = []
        for fragment in fragment_pool:
            value = normalize(str(fragment or ""))
            if ASSOC_FRAG_MIN_LEN <= len(value) <= ASSOC_FRAG_MAX_LEN and value not in normalized_fragments:
                normalized_fragments.append(value)
            if len(normalized_fragments) >= ASSOC_FRAG_TOTAL_CAP:
                break
        normalized_keywords = list(dict.fromkeys(
            normalize(str(keyword or "")) for keyword in (keywords or [])
            if len(normalize(str(keyword or ""))) >= MIN_QUERY_LEN
        ))[:ASSOC_MAX_KEYWORDS]
        normalized_facets: list[list[str]] = []
        for facet in facets or []:
            values = list(dict.fromkeys(
                normalize(str(word or "")) for word in facet
                if len(normalize(str(word or ""))) >= MIN_QUERY_LEN
            ))[:ASSOC_MAX_KEYWORDS]
            if values:
                normalized_facets.append(values)

        # Page-level signals are used only inside the already-resolved hard
        # document boundary.  Include exact fragments as a fallback when the
        # expansion has few keywords, but cap the pool so scan cost stays small.
        page_terms = list(dict.fromkeys(
            [*normalized_keywords, *(word for facet in normalized_facets for word in facet),
             *normalized_fragments]
        ))[:ASSOC_FRAG_TOTAL_CAP]

        for scope in scopes:
            if deadline is not None and time.monotonic() >= deadline:
                break
            vol = self.get_volume_by_source_file(scope.source_file)
            if not vol:
                continue
            for raw, value in normalized_quotes:
                if len(value) < MIN_QUERY_LEN:
                    continue
                start = scope.norm_start
                occurrences = 0
                while occurrences < 5:
                    pos = vol.norm_full.find(value, start, scope.norm_end)
                    if pos < 0:
                        break
                    add(scope, vol, pos, pos + len(value), 100, raw)
                    start = pos + 1
                    occurrences += 1
            for fragment in normalized_fragments:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                start = scope.norm_start
                occurrences = 0
                while occurrences < ASSOC_FRAG_PER:
                    pos = vol.norm_full.find(fragment, start, scope.norm_end)
                    if pos < 0:
                        break
                    add(scope, vol, pos, pos + len(fragment), min(97, 56 + 6 * len(fragment)), fragment)
                    start = pos + 1
                    occurrences += 1
            window = self._content_window_in_range(
                vol, scope.norm_start, scope.norm_end, normalized_keywords,
                window=ASSOC_RESEARCH_KEYWORD_WINDOW,
            ) if normalized_keywords else None
            if window:
                start, end, distinct, anchor = window
                add(scope, vol, start, end, 72 + 5 * distinct, anchor)
            for values in normalized_facets:
                window = self._content_window_in_range(
                    vol, scope.norm_start, scope.norm_end, values,
                    window=ASSOC_RESEARCH_KEYWORD_WINDOW,
                ) if values else None
                if window:
                    start, end, distinct, anchor = window
                    add(scope, vol, start, end, 68 + 5 * distinct, anchor)
            # A long named work needs evidence from more than its single global
            # best co-occurrence window.  Add the strongest window on each
            # matching page, then keep a relevance-first, evenly distributed
            # bounded subset.  This is deterministic, corpus-only, and cannot
            # cross ``scope.norm_start/norm_end``.
            if page_terms:
                page_windows = self._content_windows_by_page_in_range(
                    vol, scope.norm_start, scope.norm_end, page_terms,
                    window=ASSOC_RESEARCH_KEYWORD_WINDOW,
                    cap=min(candidate_cap, ASSOC_DOCUMENT_PAGE_CAP),
                    deadline=deadline,
                )
                for start, end, distinct, anchor in page_windows:
                    add(scope, vol, start, end, min(94, 82 + 4 * (distinct - 1)), anchor)
            if not any(key[0] == scope.document_id for key in best_by_page):
                end = min(scope.norm_end, scope.norm_start + 120)
                if end > scope.norm_start:
                    add(scope, vol, scope.norm_start, end, 60, scope.title)
                    for key, hit in best_by_page.items():
                        if key[0] == scope.document_id:
                            hit.chapter_only = True

        results = sorted(
            best_by_page.values(),
            key=lambda hit: (-hit.score, self.book_sort_order(hit.book), hit.volume,
                             hit.pages[0].pdf_page if hit.pages else 0),
        )
        return results[:candidate_cap]

    def document_text_window(self, hit: Hit, *, adjacent_pages: int = 1) -> tuple[str, tuple[int, int]]:
        """Return a hit-centred raw window clipped to the same verified work."""

        scope = self.document_scope_for_hit(hit)
        vol = self.get_volume_by_source_file(hit.source_file)
        if not scope or not vol or not hit.pages:
            return "", (0, 0)
        hit_page = int(hit.pages[0].pdf_page)
        page_index = next((i for i, page in enumerate(vol.pages) if int(page.pdf_page) == hit_page), None)
        if page_index is None:
            return "", (0, 0)
        first = max(0, page_index - max(0, int(adjacent_pages)))
        last = min(len(vol.pages) - 1, page_index + max(0, int(adjacent_pages)))
        start = max(scope.norm_start, vol.page_offsets[first])
        end = min(scope.norm_end, vol.page_offsets[last + 1])
        if end <= start:
            return "", (0, 0)
        start_page = vol.page_index_at(start)
        end_page = vol.page_index_at(end - 1)
        cache: OrderedDict[int, tuple[list[int], list[int]] | None] = OrderedDict()
        pieces: list[str] = []
        focus = (0, 0)
        cursor = 0
        for index in range(start_page, end_page + 1):
            page = vol.pages[index]
            local_start = max(0, start - vol.page_offsets[index])
            local_end = min(len(page.norm_text), end - vol.page_offsets[index])
            mapping = self._export_page_raw_map(page, cache)
            if mapping is None or local_end <= local_start:
                return "", (0, 0)
            raw_start, raw_end = self.document_page_raw_bounds(page, mapping, local_start, local_end)
            piece = page.raw_text[raw_start:raw_end]
            if pieces:
                cursor += 1
            piece_start = cursor
            pieces.append(piece)
            cursor += len(piece)
            if int(page.pdf_page) == hit_page:
                focus = (piece_start, cursor)
        return "\n".join(pieces), focus

    @staticmethod
    def document_page_raw_bounds(page, mapping, start, end):
        """Map a work-bounded page slice without losing its terminal punctuation.

        Normalized search offsets omit punctuation. Whole page edges retain
        their raw margins; an internal work boundary admits only closing
        punctuation, never the next work's opening quotation or letters.
        """
        left = 0 if start == 0 else mapping[0][start]
        right = mapping[1][end - 1]
        if end == len(page.norm_text):
            right = len(page.raw_text)
        else:
            while right < len(page.raw_text) and page.raw_text[right] in '。！？；，、：,.!?;: ”’」』）)\t\r\n':
                right += 1
        return left, right

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
    def search_chaptered(
        self, q: str, book_scope: "Collection[str] | None" = None,
    ) -> dict:
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
        cache_key = (q_norm, self._scope_cache_key(book_scope))
        with self._chaptered_cache_lock:
            cached = self._chaptered_cache.get(cache_key)
            if cached is not None:
                self._chaptered_cache.move_to_end(cache_key)
        if cached is not None:
            return {**cached, "query": q}

        layout_matches, layout_complete, layout_error = self._layout_scan(q_norm, book_scope)
        volumes_out: list[dict] = []
        total = 0
        book_hit_counts: dict[str, int] = {}
        for book in self._scoped_book_keys(book_scope):
            for vol in self._scoped_volumes(book, book_scope):
                nf = vol.norm_full
                supplemental = layout_matches.get(vol.source_file, [])
                if q_norm not in nf and not supplemental:
                    continue
                # 按 chapter_pdf_page 归并：同一篇章若被物理切成多段（get_chapter_for_page
                # 把不相邻的页段映射回同一篇章起始页），合并为一行，避免出现重复的篇章条目，
                # 并保证此处计数与 chapter_hits（按 chapter_pdf_page 取命中）完全一致。
                chapter_map: "OrderedDict[int, dict]" = OrderedDict()
                vol_count = 0
                for seg in self._chapter_segments(vol):
                    # 用重叠计数（与 chapter_hits 的 find 步进 +1 一致），保证聚合数=钻取数。
                    cnt = self._canonical_exact_count(vol, q_norm, seg["norm_start"], seg["norm_end"])
                    cnt += sum(seg["norm_start"] <= m["start"] and m["end"] <= seg["norm_end"] for m in supplemental)
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
            "exact_search_complete": layout_complete,
            "layout_warning": layout_error,
        }
        if not layout_complete:
            return computed
        with self._chaptered_cache_lock:
            self._chaptered_cache[cache_key] = computed
            self._chaptered_cache.move_to_end(cache_key)
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
        positions = [pos for seg in segments for pos in
                     self._canonical_exact_positions(vol, q_norm, seg['norm_start'], seg['norm_end'])]
        supplements, complete, warning = self._layout_scan(q_norm, volumes=[vol])
        extras = {m["start"]: m for m in supplements.get(vol.source_file, [])
                  if any(seg["norm_start"] <= m["start"] and m["end"] <= seg["norm_end"] for seg in segments)}
        positions = list(set(positions) | set(extras))
        out.update(exact_search_complete=complete, layout_warning=warning)
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
            (self._make_layout_hit(vol, extras[i], q) if i in extras else
             self._make_hit(vol, i, i + qlen, "exact", 100, q, occurrence_index=rank)).to_dict()
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
    # 首页检索结果导出：有界计数 + 流式物化
    # ------------------------------------------------------------------
    @staticmethod
    def _export_scope_allows(book: str, volume: int, book_scope: object) -> bool:
        """Return whether one volume is inside a previously validated export scope.

        ``book_scope`` intentionally uses the same compact representation as the
        web layer: ``None`` means all books, a set/list means whole books, and a
        dict maps a book to either ``None`` (all volumes) or a set of volumes.
        Keeping this predicate in the corpus layer lets the worker discard a
        volume before it creates any hit objects.
        """
        if book_scope is None:
            return True
        if book not in {str(value) for value in book_scope}:
            return False
        if isinstance(book_scope, dict):
            allowed = book_scope.get(book)
            if allowed is not None:
                return int(volume) in {int(value) for value in allowed}
        return True

    def count_exact_export(
        self,
        q: str,
        *,
        book_scope: object = None,
        stop_after: int | None = None,
    ) -> int:
        """Count exact (overlapping) hits without materialising contexts.

        ``stop_after`` is the safety fuse used by the export worker.  Once the
        caller's entitlement has certainly been exceeded there is no reason to
        keep scanning or allocate document state.
        """
        aggregate = self.search_chaptered(q, book_scope=book_scope)
        if aggregate.get('exact_search_complete') is False:
            raise RuntimeError('排版补充检索未完成，请稍后重试导出。')
        total = 0
        for row in aggregate.get("volumes") or []:
            if not self._export_scope_allows(
                str(row.get("book") or ""), int(row.get("volume") or 0), book_scope
            ):
                continue
            total += int(row.get("count") or 0)
            if stop_after is not None and total >= int(stop_after):
                return total
        return total

    def iter_exact_export_hits(
        self,
        q: str,
        *,
        book_scope: object = None,
        limit: int | None = None,
    ) -> Iterator[dict]:
        """Yield exact hits in corpus/book/volume/page order, one object at a time."""
        q_norm = normalize(q)
        if len(q_norm) < MIN_QUERY_LEN:
            return
        volumes = [v for book in self.books for v in self.books.get(book, [])
                   if self._export_scope_allows(book, int(v.volume), book_scope)]
        supplements, complete, warning = self._layout_scan(q_norm, volumes=volumes)
        if not complete:
            raise RuntimeError("排版补充检索未完成，请稍后重试导出。")
        emitted = 0
        qlen = len(q_norm)
        for book in self.books:
            for vol in self.books.get(book, []):
                if not self._export_scope_allows(book, int(vol.volume), book_scope):
                    continue
                page_map_cache: OrderedDict[int, tuple[list[int], list[int]] | None] = OrderedDict()
                nf = vol.norm_full
                extras = {m["start"]: m for m in supplements.get(vol.source_file, [])}
                import heapq
                page_ranks: dict[int, int] = {}
                for pos in heapq.merge(self._canonical_exact_positions(vol, q_norm), sorted(extras)):
                    page_index = vol.page_index_at(pos)
                    rank = page_ranks.get(page_index, 0)
                    page_ranks[page_index] = rank + 1
                    yield (self._make_layout_hit(vol, extras[pos], q) if pos in extras else self._make_hit(
                        vol,
                        pos,
                        pos + qlen,
                        "exact",
                        100,
                        q,
                        occurrence_index=rank,
                        export_complete_sentence=True,
                        export_page_map_cache=page_map_cache,
                    )).to_dict()
                    emitted += 1
                    if limit is not None and emitted >= int(limit):
                        return
                    # Export counts deliberately use overlapping occurrences, as
                    # does ``search_chaptered``/``chapter_hits``.
                    start = pos + 1

    @staticmethod
    def _export_cooccurrence_keywords(keywords: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for keyword in keywords or []:
            normalized = normalize(str(keyword or ""))
            if len(normalized) < MIN_QUERY_LEN or normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)
            if len(cleaned) >= ASSOC_MAX_KEYWORDS:
                break
        return cleaned

    @staticmethod
    def _iter_export_cooccurrence_windows(
        text: str,
        keywords: list[str],
        *,
        window: int = ASSOC_KEYWORD_WINDOW,
    ) -> Iterator[tuple[int, int, str]]:
        """Stream minimal non-overlapping co-occurrence windows.

        The online search path intentionally caps per-keyword occurrences.  An
        export may legitimately contain thousands of hits, so this variant uses
        a heap merge of the keyword occurrence streams.  Its memory use is O(k)
        rather than O(number-of-occurrences), even for very common short terms.
        """
        import heapq

        heap: list[tuple[int, int]] = []
        for keyword_id, keyword in enumerate(keywords):
            pos = text.find(keyword)
            if pos < 0:
                return
            heapq.heappush(heap, (pos, keyword_id))
        latest: dict[int, int] = {}
        last_emit_end = -1
        while heap:
            pos, keyword_id = heapq.heappop(heap)
            keyword = keywords[keyword_id]
            latest[keyword_id] = pos
            next_start = pos + len(keyword)
            following = text.find(keyword, next_start)
            if following >= 0:
                heapq.heappush(heap, (following, keyword_id))
            if len(latest) != len(keywords):
                continue
            win_start = min(latest.values())
            win_end = max(latest[kid] + len(keywords[kid]) for kid in latest)
            if win_end - win_start > int(window) or win_start < last_emit_end:
                continue
            anchor_id = max(latest, key=lambda kid: (len(keywords[kid]), -kid))
            yield win_start, win_end, keywords[anchor_id]
            last_emit_end = win_end

    def count_cooccurrence_export(
        self,
        keywords: list[str],
        *,
        book_scope: object = None,
        stop_after: int | None = None,
        window: int = ASSOC_KEYWORD_WINDOW,
    ) -> int:
        cleaned = self._export_cooccurrence_keywords(keywords)
        if len(cleaned) < 2:
            return 0
        total = 0
        for book in self.books:
            for vol in self.books.get(book, []):
                if not self._export_scope_allows(book, int(vol.volume), book_scope):
                    continue
                if any(keyword not in vol.norm_full for keyword in cleaned):
                    continue
                for _window in self._iter_export_cooccurrence_windows(
                    vol.norm_full, cleaned, window=window
                ):
                    total += 1
                    if stop_after is not None and total >= int(stop_after):
                        return total
        return total

    def iter_cooccurrence_export_hits(
        self,
        keywords: list[str],
        *,
        book_scope: object = None,
        limit: int | None = None,
        window: int = ASSOC_KEYWORD_WINDOW,
    ) -> Iterator[dict]:
        cleaned = self._export_cooccurrence_keywords(keywords)
        if len(cleaned) < 2:
            return
        emitted = 0
        for book in self.books:
            for vol in self.books.get(book, []):
                if not self._export_scope_allows(book, int(vol.volume), book_scope):
                    continue
                if any(keyword not in vol.norm_full for keyword in cleaned):
                    continue
                page_map_cache: OrderedDict[int, tuple[list[int], list[int]] | None] = OrderedDict()
                for win_start, win_end, anchor in self._iter_export_cooccurrence_windows(
                    vol.norm_full, cleaned, window=window
                ):
                    yield self._make_hit(
                        vol,
                        win_start,
                        win_end,
                        "exact",
                        100,
                        anchor,
                        highlight_terms=cleaned,
                        export_complete_sentence=True,
                        export_page_map_cache=page_map_cache,
                    ).to_dict()
                    emitted += 1
                    if limit is not None and emitted >= int(limit):
                        return

    # ------------------------------------------------------------------
    # 检索入口
    # ------------------------------------------------------------------
    def search(self, q: str, max_results: int = 5) -> list[Hit]:
        q_norm = normalize(q)
        if len(q_norm) < MIN_QUERY_LEN:
            return []

        exact: list[Hit] = []
        books_with_exact: set[str] = set()
        for book in self.books:
            book_hits, _ = self._exact_in_book(book, q_norm, q, limit=EXACT_HITS_PER_BOOK)
            if book_hits:
                books_with_exact.add(book)
            exact.extend(book_hits)
        if exact:
            # 与 search_grouped 同义：零命中的书库补做近似兜底（见彼处注释）。
            for book in self.books:
                if book in books_with_exact:
                    continue
                partial, _ = self._fuzzy_in_book(book, q_norm, q)
                exact.extend(partial)
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
        book_scope: "Collection[str] | None" = None,
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

        layout_matches, layout_complete, layout_error = self._layout_scan(q_norm, book_scope)
        def finish(result):
            result.update(exact_search_complete=layout_complete, layout_warning=layout_error)
            return result

        hits: list[Hit] = []
        truncated = False
        books_with_exact: set[str] = set()
        exact_limit = max_hits if max_hits is not None else EXACT_HITS_PER_BOOK
        for book in self._scoped_book_keys(book_scope):
            book_hits, book_truncated = self._exact_in_book(
                book, q_norm, q, limit=exact_limit, book_scope=book_scope, layout_matches=layout_matches
            )
            if book_hits:
                books_with_exact.add(book)
            hits.extend(book_hits)
            truncated = truncated or book_truncated
        if hits:
            # 按书库补位的近似兜底：同一段话在各书库扫描件里的 OCR 错字不一致，
            # 查询可能恰好与某书库的文本层逐字一致（连错字都一致），却与另一书库
            # 差一两个字。若因「全库有精确命中」就整体关掉近似，后者会被静默压掉。
            # 故仅对零命中的书库补扫；截断意味着海量命中（将走篇章聚合通道），不补。
            if layout_complete and not truncated and not any(h.exact_basis == 'layout' for h in hits):
                for book in self._scoped_book_keys(book_scope):
                    if book in books_with_exact:
                        continue
                    partial, _ = self._fuzzy_in_book(book, q_norm, q, book_scope=book_scope)
                    hits.extend(partial)
            return finish(self._group_hits(
                q,
                self._dedupe_hits(hits),
                group_limit,
                page_size,
                truncated,
            ))

        if not layout_complete:
            return finish({"query": q, "total_hits": 0, "group_count": 0, "truncated": False, "groups": []})

        if len(q_norm) < MIN_FUZZY_QUERY_LEN:
            return {
                "query": q,
                "total_hits": 0,
                "group_count": 0,
                "truncated": False,
                "groups": [],
            }

        # CPU 密集的全语料近似扫描：经并发闸限制同时进行的扫描数，护住线程池；短超时拿不到名额则
        # 降级为「无结果」（正常负载几乎不触发）。仅包住扫描循环本身，分组/分页等轻活不占名额。
        if not _FUZZY_SCAN_SEMAPHORE.acquire(timeout=_FUZZY_SCAN_ACQUIRE_TIMEOUT):
            return {
                "query": q,
                "total_hits": 0,
                "group_count": 0,
                "truncated": False,
                "groups": [],
            }
        try:
            fuzzy: list[Hit] = []
            fuzzy_limit = max_hits or (group_limit * page_size * 5)
            for book in self._scoped_book_keys(book_scope):
                partial, partial_truncated = self._fuzzy_in_book(
                    book,
                    q_norm,
                    q,
                    limit=max(0, fuzzy_limit - len(fuzzy)),
                    book_scope=book_scope,
                )
                fuzzy.extend(partial)
                truncated = truncated or partial_truncated or len(fuzzy) >= fuzzy_limit
                if len(fuzzy) >= fuzzy_limit:
                    break
        finally:
            _FUZZY_SCAN_SEMAPHORE.release()
        fuzzy.sort(key=lambda h: (-h.score, self.book_sort_order(h.book)))
        return self._group_hits(
            q,
            self._dedupe_hits(fuzzy[:fuzzy_limit]),
            group_limit,
            page_size,
            truncated,
        )

    def search_cooccurrence_grouped(
        self,
        keywords: list[str],
        group_limit: int = DEFAULT_GROUP_LIMIT,
        page_size: int = GROUP_PAGE_SIZE,
        window: int = ASSOC_KEYWORD_WINDOW,
        book_scope: "Collection[str] | None" = None,
    ) -> dict:
        """同段多词检索：返回「全部关键词共现于邻近窗口」的真实命中，结构与 search_grouped 一致。

        语义是「容错的精确」之外的另一种精确——每处命中都保证全部关键词逐字出现在同一
        ~window 字的近邻段落内。产出真实 Hit 后交给 _group_hits，从而复用既有分组/分页/
        摘要/阅读器高亮的全部前后端设施。
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
        query = " ".join(dict.fromkeys(str(k or "").strip() for k in (keywords or []) if str(k or "").strip()))
        if len(kws) < 2:
            return {
                "query": query,
                "total_hits": 0,
                "group_count": 0,
                "truncated": False,
                "groups": [],
            }

        hits: list[Hit] = []
        truncated = False
        for book in self._scoped_book_keys(book_scope):
            for vol in self._scoped_volumes(book, book_scope):
                nf = vol.norm_full
                if any(kn not in nf for kn in kws):
                    continue
                for ws, we, anchor in self._cooccurrence_windows(vol, kws, window=window):
                    hits.append(
                        self._make_hit(vol, ws, we, "exact", 100, anchor, highlight_terms=kws)
                    )
                    if len(hits) >= COOC_TOTAL_CAP:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break

        return self._group_hits(
            query, self._dedupe_hits(hits), group_limit, page_size, truncated
        )

    def _cooccurrence_windows(
        self,
        vol: Volume,
        kws_norm: list[str],
        *,
        window: int = ASSOC_KEYWORD_WINDOW,
        occ_cap: int = ASSOC_KW_OCC_CAP,
        per_vol_cap: int = COOC_PER_VOL,
    ) -> list[tuple[int, int, str]]:
        """枚举某卷内「全部关键词共现于 ≤window 字窗口」的多处非重叠最小窗口。

        与 keyword_cooccurrence 同源（先成员过滤、再收集出现位置、双指针滑窗），但那里每卷
        只取一个最佳窗口；此处要求覆盖**全部** distinct 关键词，并沿正文连续吐出多处非重叠窗口，
        供同段多词检索逐处呈现。
        """
        nf = vol.norm_full
        n_kw = len(kws_norm)
        occ: list[tuple[int, int]] = []  # (pos, kw_id)
        for kid, kn in enumerate(kws_norm):
            start = 0
            cnt = 0
            klen = len(kn)
            while cnt < occ_cap:
                i = nf.find(kn, start)
                if i < 0:
                    break
                occ.append((i, kid))
                start = i + klen
                cnt += 1
        if len(occ) < n_kw:
            return []
        occ.sort()

        results: list[tuple[int, int, str]] = []
        counts: dict[int, int] = {}
        distinct = 0
        left = 0
        last_emit_end = -1
        for right in range(len(occ)):
            pos_r, kid_r = occ[right]
            counts[kid_r] = counts.get(kid_r, 0) + 1
            if counts[kid_r] == 1:
                distinct += 1
            # 收缩左端：剔除冗余出现，得到以 right 结尾的最小覆盖窗口
            while distinct == n_kw and counts[occ[left][1]] > 1:
                counts[occ[left][1]] -= 1
                left += 1
            if distinct < n_kw:
                continue
            win_start = occ[left][0]
            win_end = pos_r + len(kws_norm[kid_r])
            if win_end - win_start > window or win_start < last_emit_end:
                continue
            # 高亮锚点取窗口内最长关键词，确保 context 高亮落在真实命中上
            anchor = max(
                (kws_norm[k] for (p, k) in occ if win_start <= p < win_end),
                key=len,
                default=kws_norm[kid_r],
            )
            results.append((win_start, win_end, anchor))
            last_emit_end = win_end
            if len(results) >= per_vol_cap:
                break
        return results

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
    def _layout_scan(self, q_norm, book_scope=None, volumes=None):
        index = getattr(self, 'layout_index', None)
        if index is None:
            return {}, True, ''
        if volumes is None:
            volumes = [v for book in self._scoped_book_keys(book_scope)
                       for v in self._scoped_volumes(book, book_scope)]
        return index.scan(q_norm, volumes, _FUZZY_SCAN_SEMAPHORE)

    def _canonical_exact_positions(self, vol, q_norm, norm_start=0, norm_end=None):
        start = norm_start
        search_end = len(vol.norm_full) if norm_end is None else norm_end
        while True:
            pos = vol.norm_full.find(q_norm, start, search_end)
            if pos < 0:
                return
            start = pos + 1
            end = pos + len(q_norm)
            first, last = vol.page_index_at(pos), vol.page_index_at(end - 1)
            if first != last:
                if any(vol.pages[i + 1].pdf_page != vol.pages[i].pdf_page + 1 for i in range(first, last)):
                    continue
                if not any(seg['norm_start'] <= pos and end <= seg['norm_end']
                           for seg in self._chapter_segments(vol)):
                    continue
            yield pos

    def _canonical_exact_count(self, vol, q_norm, start, end):
        count = _count_overlapping(vol.norm_full, q_norm, start, end)
        gaps = getattr(vol, '_layout_physical_gaps', None)
        if gaps is None:
            gaps = tuple(vol.page_offsets[i] for i in range(1, len(vol.pages))
                         if vol.pages[i].pdf_page != vol.pages[i - 1].pdf_page + 1)
            vol._layout_physical_gaps = gaps
        invalid = set()
        for gap in gaps:
            if not start < gap < end:
                continue
            lo, hi = max(start, gap - len(q_norm) + 1), min(end, gap + len(q_norm) - 1)
            while True:
                pos = vol.norm_full.find(q_norm, lo, hi)
                if pos < 0 or pos >= gap:
                    break
                invalid.add(pos)
                lo = pos + 1
        return count - len(invalid)

    def _make_layout_hit(self, vol, match, q_raw):
        hit = self._make_hit(vol, match['start'], match['end'], 'exact', 100, q_raw)
        pieces = []
        page_matches = []
        for a, b in match['spans']:
            for pi in range(vol.page_index_at(a), vol.page_index_at(b - 1) + 1):
                page = vol.pages[pi]
                lo, hi = max(a, vol.page_offsets[pi]), min(b, vol.page_offsets[pi + 1])
                mapping = self._export_page_raw_map(page, None)
                if mapping is None:
                    raise ValueError('layout raw mapping invalid')
                starts, ends = mapping
                raw_start = starts[lo - vol.page_offsets[pi]]
                raw_end = ends[hi - vol.page_offsets[pi] - 1]
                # Preserve source punctuation adjacent to an ignored note anchor.
                # Never synthesize punctuation from the user's query.
                if page_matches and page_matches[-1]['pdf_page'] == page.pdf_page:
                    floor = page_matches[-1]['raw_end']
                    while raw_start > floor and not normalize(page.raw_text[raw_start - 1]):
                        raw_start -= 1
                while raw_end < len(page.raw_text) and page.raw_text[raw_end] in '，。；：！？,.!?;:':
                    raw_end += 1
                text = page.raw_text[raw_start:raw_end]
                pieces.append(text)
                page_matches.append({'pdf_page': page.pdf_page, 'raw_start': raw_start,
                                     'raw_end': raw_end, 'text': text})
        hit.context = '[[H]]' + ''.join(pieces) + '[[/H]]'
        hit.exact_basis = 'layout'
        hit.ignored_layout_types = match['types']
        hit.layout_hit_ref = match['ref']
        hit.page_matches = page_matches
        return hit

    def _exact_in_book(self, book: str, q_norm: str, q_raw: str, limit: int | None = 20,
                       book_scope: "Collection[str] | None" = None, layout_matches=None) -> tuple[list[Hit], bool]:
        hits: list[Hit] = []
        truncated = False
        if layout_matches is None:
            layout_matches, _, _ = self._layout_scan(q_norm, book_scope,
                                                   list(self._scoped_volumes(book, book_scope)))
        for vol in self._scoped_volumes(book, book_scope):
            start = 0
            volume_hits = []
            for i in self._canonical_exact_positions(vol, q_norm):
                volume_hits.append(self._make_hit(vol, i, i + len(q_norm), "exact", 100, q_raw))
                start = i + 1
                if limit is not None and len(hits) + len(volume_hits) >= limit:
                    truncated = True
                    break
            volume_hits.extend(self._make_layout_hit(vol, m, q_raw)
                               for m in layout_matches.get(vol.source_file, [])[:limit])
            volume_hits.sort(key=lambda h: (h.norm_start, h.norm_end))
            hits.extend(volume_hits)
            if limit is not None and len(hits) >= limit:
                truncated = True
                hits = hits[:limit]
            if truncated:
                break
        return hits, truncated

    # ------------------------------------------------------------------
    # 模糊匹配
    # ------------------------------------------------------------------
    def _fuzzy_in_book(self, book: str, q_norm: str, q_raw: str, limit: int | None = None,
                       book_scope: "Collection[str] | None" = None) -> tuple[list[Hit], bool]:
        """近似匹配兜底（仅在精确零命中时调用）：容错 OCR 错字，但保持「精确」的指向性。

        partial_ratio 是字符重叠率，常用字偶然重叠就能让无关段落拿到高分（碰瓷命中），
        故它只用作快速候选定位；权威过滤是真实编辑距离 ≤ K——结果集里留下的命中在数学上
        保证与查询至多差 K 个字。
        """
        hits: list[Hit] = []
        truncated = False
        max_errors = _fuzzy_allowed_errors(len(q_norm))
        if max_errors <= 0:
            return hits, truncated
        # partial_ratio 为 InDel 比率：K 个替换 ≈ 2K 个删插、窗口长 ≈ 查询长，
        # 故 K 错字对应分数约 100*(1-K/L)。放宽半个错字防对齐边界抖动漏真命中，
        # 多放进来的候选由编辑距离复核拦截。
        cutoff = max(0.0, 100.0 * (1.0 - (max_errors + 0.5) / len(q_norm)))
        pad = max_errors + 2
        for vol in self._scoped_volumes(book, book_scope):
            if limit is not None and len(hits) >= limit:
                truncated = True
                break
            res = fuzz.partial_ratio_alignment(
                q_norm, vol.norm_full, score_cutoff=cutoff
            )
            if res is None:
                continue
            # 编辑距离复核：先按对齐窗口原样算（C 实现，快路径），失败再用带 padding
            # 的半全局对齐兜住对齐边界偏移。
            span = vol.norm_full[res.dest_start:res.dest_end]
            dist = Levenshtein.distance(q_norm, span, score_cutoff=max_errors)
            if dist > max_errors:
                window = vol.norm_full[max(0, res.dest_start - pad):res.dest_end + pad]
                dist = _substring_edit_distance(q_norm, window, max_errors)
                if dist is None:
                    continue
            hits.append(self._make_hit(
                vol, res.dest_start, res.dest_end, "fuzzy", int(res.score), q_raw,
                fuzzy_errors=dist,
            ))
        return hits, truncated

    # ------------------------------------------------------------------
    # 联想检索的接地定位（纯 Python，无 AI；AI 只在上层提供 quotes/keywords）
    # ------------------------------------------------------------------
    def locate_quote(
        self,
        quote: str,
        *,
        per_book_exact: int = 5,
        allow_fuzzy: bool = True,
        book_scope: "Collection[str] | None" = None,
    ) -> list[Hit]:
        """在真实语料中定位单条候选原文：先精确（每书库取前若干处），无精确再模糊（每卷最佳 span）。

        返回真实 Hit（含 context/citation/section），绝不构造不存在的出处。
        ``allow_fuzzy=False`` 时跳过整句模糊兜底（联想检索用片段召回替代，避免逐卷 partial_ratio 的高开销）。
        ``book_scope`` 限定要扫描的书库（见 _scoped_book_keys）；缺省全部。
        """
        q_raw = (quote or "").strip()
        if len(q_raw) > ASSOC_QUOTE_MAXLEN:
            q_raw = q_raw[:ASSOC_QUOTE_MAXLEN]
        q_norm = normalize(q_raw)
        if len(q_norm) < MIN_QUERY_LEN:
            return []
        scoped_books = self._scoped_book_keys(book_scope)
        hits: list[Hit] = []
        for book in scoped_books:
            book_hits, _ = self._exact_in_book(book, q_norm, q_raw, limit=per_book_exact, book_scope=book_scope)
            hits.extend(book_hits)
        if hits:
            return hits
        if not allow_fuzzy or len(q_norm) < MIN_FUZZY_QUERY_LEN:
            return []
        fuzzy: list[Hit] = []
        for book in scoped_books:
            partial, _ = self._fuzzy_in_book(book, q_norm, q_raw, book_scope=book_scope)
            fuzzy.extend(partial)
        fuzzy.sort(key=lambda h: (-h.score, self.book_sort_order(h.book)))
        return fuzzy

    @staticmethod
    def _cooccurrence_groups(keywords: list[str], expand_synonyms: bool) -> list[list[str]]:
        """把关键词归一化、去重、（可选）按 ``TERM_THESAURUS`` 并成「概念组」；每组为一个概念的归一化
        变体列表（组内 OR）。expand_synonyms=False 时每词自成一组，与旧行为逐字一致。至多
        ``ASSOC_MAX_KEYWORDS`` 组；同一概念的多个同义词只占一组、不重复开组。"""
        groups: list[list[str]] = []
        seen_terms: set[str] = set()
        concept_to_group: dict[int, int] = {}
        for k in keywords or []:
            kn = normalize(str(k or ""))
            if len(kn) < MIN_QUERY_LEN or kn in seen_terms:
                continue
            cid = _TERM_TO_GROUP.get(kn) if expand_synonyms else None
            if cid is not None:
                if cid in concept_to_group:
                    seen_terms.add(kn)
                    continue  # 该概念已由另一同义词开组，本词并入、不新开
                concept_to_group[cid] = len(groups)
                variants = list(_THESAURUS_GROUPS[cid])
                groups.append(variants)
                seen_terms.update(variants)  # 组内全部变体标记已用，避免后续同义词重复开组
            else:
                groups.append([kn])
                seen_terms.add(kn)
            if len(groups) >= ASSOC_MAX_KEYWORDS:
                break
        return groups

    def keyword_cooccurrence(
        self,
        keywords: list[str],
        *,
        window: int = ASSOC_KEYWORD_WINDOW,
        min_distinct: int | None = None,
        occ_cap: int = ASSOC_KW_OCC_CAP,
        book_scope: "Collection[str] | None" = None,
        expand_synonyms: bool = False,
    ) -> list[Hit]:
        """定位“多关键词在近邻窗口内共现”的真实段落。

        先用 M-of-N 子串成员（C 层 ``in``）过滤掉绝大多数卷，仅对存活卷做位置扫描与滑窗，
        从而在数百 MB 内存文本上仍可在线运行（沿用 search_chaptered 的“先成员判断再扫描”模式）。
        每卷取一个最佳窗口（不同概念数最多、跨度最紧），生成真实 Hit。

        ``expand_synonyms=True``：按 ``TERM_THESAURUS`` 把同义/译名并成「概念组」，组内任一表述命中
        即算命中该概念，distinct 与封顶均按**概念数**计——使某段只用一种译法（如「外化」而非「异化」）
        也能被共现召回；缺省 False 时每词自成一组，与旧行为逐字一致。
        """
        groups = self._cooccurrence_groups(keywords, expand_synonyms)
        n_groups = len(groups)
        if n_groups < 2:
            return []
        if min_distinct is None:
            min_distinct = max(2, (n_groups + 1) // 2)  # ceil(0.5 * n)
        min_distinct = min(min_distinct, n_groups)

        hits: list[Hit] = []
        for book in self._scoped_book_keys(book_scope):
            for vol in self._scoped_volumes(book, book_scope):
                nf = vol.norm_full
                # 概念存在性：概念组内任一变体在卷内出现，即算该概念存在
                present = [gi for gi in range(n_groups) if any(v in nf for v in groups[gi])]
                if len(present) < min_distinct:
                    continue
                # 收集各概念前 occ_cap 个出现位置（组内各变体命中统一归到该概念 id）
                occ: list[tuple[int, int, str]] = []  # (pos, concept_id, term)
                for gi in present:
                    cnt = 0
                    for v in groups[gi]:
                        start = 0
                        vlen = len(v)
                        while cnt < occ_cap:
                            i = nf.find(v, start)
                            if i < 0:
                                break
                            occ.append((i, gi, v))
                            start = i + vlen
                            cnt += 1
                        if cnt >= occ_cap:
                            break
                if not occ:
                    continue
                occ.sort()
                # 双指针滑窗：求“不同概念数最多、跨度最紧”的窗口
                counts: dict[int, int] = {}
                distinct = 0
                left = 0
                best: tuple[int, int, int, int] | None = None  # (distinct, -span, start, end)
                for right in range(len(occ)):
                    pos_r, cid_r, term_r = occ[right]
                    counts[cid_r] = counts.get(cid_r, 0) + 1
                    if counts[cid_r] == 1:
                        distinct += 1
                    lo = pos_r - window + 1
                    while occ[left][0] < lo:
                        cid_l = occ[left][1]
                        counts[cid_l] -= 1
                        if counts[cid_l] == 0:
                            distinct -= 1
                        left += 1
                    if distinct >= min_distinct:
                        win_start = occ[left][0]
                        win_end = pos_r + len(term_r)
                        cand = (distinct, -(win_end - win_start), win_start, win_end)
                        if best is None or cand > best:
                            best = cand
                if best is None:
                    continue
                win_distinct, _neg_span, win_start, win_end = best
                # 高亮锚点：窗口内命中的最长变体，确保 context 高亮落在真实命中上
                anchor = ""
                for (pos, cid, term) in occ:
                    if win_start <= pos < win_end and len(term) > len(anchor):
                        anchor = term
                if not anchor:
                    anchor = groups[present[0]][0]
                # 关键词共现是最弱信号（可能巧合），分值封顶低于片段/整句/篇章定向，避免淹没精确命中
                coverage_score = min(88, int(round(92 * win_distinct / n_groups)))
                hits.append(
                    self._make_hit(vol, win_start, win_end, "fuzzy", coverage_score, anchor)
                )
        return hits

    def _fragment_exact_hits(
        self, fn: str, *, max_freq: int, per_fragment: int,
        book_scope: "Collection[str] | None" = None,
    ) -> list[Hit] | None:
        """单趟扫描：统计片段全语料出现数、收集前若干处真实命中；超过 max_freq 视为太常见，弃用。

        把“频次判定”和“取命中”合并为一次扫描，避免重复全语料遍历（联想检索性能关键）。
        ``book_scope`` 限定扫描范围后，频次也只在范围内计——某片段全库高频但范围内稀有时仍具区分度、予以保留。
        """
        hits: list[Hit] = []
        total = 0
        flen = len(fn)
        for book in self._scoped_book_keys(book_scope):
            for vol in self._scoped_volumes(book, book_scope):
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
        book_scope: "Collection[str] | None" = None,
    ) -> list[tuple[Hit, str]]:
        """对“逐字短语片段”做精确检索：保留长度合适且具区分度（出现数 ≤ max_freq）的片段。

        返回 (真实 Hit, 命中片段) 列表；分值按片段长度给出（越长越可信）。按传入顺序处理并
        受 total_cap 限制，故应把可信度更高的模型片段排在前面。``book_scope`` 限定扫描范围。
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
            hits = self._fragment_exact_hits(
                fn, max_freq=max_freq, per_fragment=per_fragment, book_scope=book_scope
            )
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

    @staticmethod
    def _prf_phrases(
        text: str,
        *,
        lengths: tuple[int, ...] = ASSOC_PRF_SHINGLE_LENS,
        cap: int = ASSOC_PRF_SHINGLE_CAP,
    ) -> list[str]:
        """伪相关反馈取词：从一段命中正文里跨全段半重叠采样若干「候选短语」（纯字符串操作，不扫描语料）。
        是否真具区分度／是否存在，统一交给 fragment_search 的频次闸判定（太常见弃用；只在源处出现则无害）。"""
        s = normalize(text)
        out: list[str] = []
        seen: set[str] = set()
        per_len = max(1, cap // max(1, len(lengths)))
        for length in lengths:
            if len(s) < length:
                continue
            n_pos = len(s) - length + 1
            step = max(length, n_pos // per_len)  # 铺开覆盖整段（而非只取开头）
            got = 0
            for i in range(0, n_pos, step):
                frag = s[i:i + length]
                if frag in seen:
                    continue
                seen.add(frag)
                out.append(frag)
                got += 1
                if got >= per_len or len(out) >= cap:
                    break
            if len(out) >= cap:
                break
        return out

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

    def _content_windows_by_page_in_range(
        self,
        vol: Volume,
        range_start: int,
        range_end: int,
        kws_norm: list[str],
        *,
        window: int = ASSOC_RESEARCH_KEYWORD_WINDOW,
        cap: int = ASSOC_DOCUMENT_PAGE_CAP,
        deadline: float | None = None,
    ) -> list[tuple[int, int, int, str]]:
        """Return bounded, page-distributed lexical windows inside one work.

        The global co-occurrence helper deliberately returns one best window;
        that is appropriate for broad search but under-recalls long explicitly
        named works.  Here we scan only the pages intersecting the verified work
        and retain at most one window per page.  Ties at the cap boundary are
        sampled evenly across the work instead of silently favouring its opening
        pages.
        """

        if range_end <= range_start or not kws_norm or cap <= 0 or not vol.pages:
            return []
        first_page = vol.page_index_at(range_start)
        last_page = vol.page_index_at(range_end - 1)
        ranked: list[tuple[int, int, int, int, str]] = []
        for page_index in range(first_page, last_page + 1):
            if deadline is not None and time.monotonic() >= deadline:
                break
            page_start = max(range_start, vol.page_offsets[page_index])
            page_end = min(range_end, vol.page_offsets[page_index + 1])
            if page_end <= page_start:
                continue
            match = self._content_window_in_range(
                vol, page_start, page_end, kws_norm,
                window=window, occ_cap=8,
            )
            if not match:
                continue
            start, end, distinct, anchor = match
            ranked.append((distinct, start, end, page_index, anchor))

        if len(ranked) <= cap:
            selected = ranked
        else:
            # Relevance remains primary.  Only the tied group crossing the cap
            # is thinned, and it is thinned evenly by page position.
            ranked.sort(key=lambda item: (-item[0], item[3], item[1]))
            boundary_strength = ranked[cap - 1][0]
            selected = [item for item in ranked if item[0] > boundary_strength]
            tied = [item for item in ranked if item[0] == boundary_strength]
            slots = cap - len(selected)
            if slots == 1:
                selected.append(tied[len(tied) // 2])
            elif slots > 1:
                indices = {
                    round(index * (len(tied) - 1) / (slots - 1))
                    for index in range(slots)
                }
                selected.extend(tied[index] for index in sorted(indices))

        selected.sort(key=lambda item: (-item[0], item[3], item[1]))
        return [(start, end, distinct, anchor) for distinct, start, end, _page, anchor in selected[:cap]]


    @staticmethod
    def _title_subseq_match(clue: str, title_norm: str) -> bool:
        """篇名容错：clue 的字符按序、紧凑地出现在标题中即算命中（子序列且跨度受限）。

        用于把简称/残名匹配到全称，如“共宣”→“共产党宣言”、“雾月十八”→“路易波拿巴的雾月十八日”。
        clue 已归一化、长度≥2；连续子串命中走调用方的快路，这里只兜“非连续”的情况，并用
        跨度上限(n+4)防止在长标题里凑出无意义的零散子序列。"""
        n = len(clue)
        if n < 2 or n > len(title_norm):
            return False
        first = last = -1
        j = 0
        for idx, ch in enumerate(title_norm):
            if ch == clue[j]:
                if first < 0:
                    first = idx
                last = idx
                j += 1
                if j == n:
                    break
        if j < n:
            return False
        return (last - first + 1) <= n + 4

    def chapter_focused_search(
        self,
        chapter_keywords: list[str],
        content_keywords: list[str],
        *,
        max_chapters: int = ASSOC_CHAPTER_MAX,
        window: int = ASSOC_KEYWORD_WINDOW,
        book_scope: "Collection[str] | None" = None,
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
        for book in self._scoped_book_keys(book_scope):
            for vol in self._scoped_volumes(book, book_scope):
                segs = self._chapter_segments(vol)
                covered_end = -1
                for i, seg in enumerate(segs):
                    if seg["norm_start"] < covered_end:
                        continue  # 已被前一命中篇目的整段范围覆盖
                    title_norm = normalize(seg["title"])
                    if not title_norm:
                        continue
                    hit_ck = next(
                        (k for k in ck if k in title_norm or self._title_subseq_match(k, title_norm)),
                        "",
                    )
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
                        chapter_hit = self._make_hit(vol, ws, we, "exact", 86, hit_ck)
                        chapter_hit.chapter_only = True
                        results.append((chapter_hit, hit_ck))
                    matched += 1
                    if matched >= max_chapters:
                        return results
        return results

    @staticmethod
    def _diversify_by_book(
        hits: list[Hit],
        per_book_cap: int = 4,
        group_key: "Callable[[Hit], str] | None" = None,
    ) -> list[Hit]:
        """多样性排序：保持分数序，但把同一著作（或著作群）超过 per_book_cap 条的命中后置到末尾，
        使首屏在不同著作间铺开。global best 仍在首位，弱命中不会越过强命中——只是同组第 cap+1 条后移。

        ``group_key`` 缺省按 ``h.book`` 计数；传入后按其返回的分组键计数，可把「同一著作的不同版本」
        （如《文集》/《全集》/《全集·二版》同属马恩著作）合并到一个配额里，避免两个版本各占名额、
        把其它作者（列宁/毛泽东等）整体挤出首屏。"""
        counts: dict[str, int] = {}
        primary: list[Hit] = []
        overflow: list[Hit] = []
        for h in hits:
            k = group_key(h) if group_key is not None else h.book
            c = counts.get(k, 0)
            if c < per_book_cap:
                primary.append(h)
                counts[k] = c + 1
            else:
                overflow.append(h)
        return primary + overflow

    def _author_group_key(self, hit: Hit) -> str:
        """著作群分组键：把同一作者的多版本/多书合并为一个多样性配额。

        数据驱动（取自 books.yaml 的 citation_title）：马恩三套版本（《文集》/《全集》/《全集·二版》，
        citation_title 均以「马克思恩格斯」起头）归一到同一群；其它书库各自独立。用于随心问接地的
        按作者铺开，确保两套马恩版本不会同时霸占注入名额。"""
        ct = (self.get_book_config(hit.book).citation_title or "").strip()
        if ct.startswith("马克思恩格斯"):
            return "马克思恩格斯"
        return hit.book

    def _complete_associative_hit_context(
        self,
        hit: Hit,
        page_map_cache: OrderedDict[int, tuple[list[int], list[int]] | None],
    ) -> None:
        """Expand one final associative-search preview to complete sentence boundaries.

        Recall still uses the existing bounded snippets, so ranking cost and semantics stay
        unchanged.  Only the candidates that survive deduplication/ranking are expanded, and
        the existing small LRU shares normalized-to-raw page maps across nearby results.
        """
        if hit.norm_start is None or hit.norm_end is None:
            return
        vol = self.get_volume_by_source_file(hit.source_file)
        if vol is None:
            vol = next(
                (
                    candidate
                    for candidate in self.books.get(hit.book, [])
                    if candidate.volume == hit.volume
                    and candidate.source_file == hit.source_file
                ),
                None,
            )
        if vol is None:
            return
        # Preserve the actual highlighted wording (including OCR punctuation/spacing) instead
        # of highlighting the whole co-occurrence window after sentence expansion.
        highlight_terms = [
            match.group(1)
            for match in re.finditer(r"\[\[H\]\]([\s\S]*?)\[\[/H\]\]", hit.context or "")
            if normalize(match.group(1))
        ]
        context = self._extract_export_sentence_context(
            vol,
            hit.norm_start,
            hit.norm_end,
            highlight_terms=highlight_terms or None,
            page_map_cache=page_map_cache,
        )
        if context:
            hit.context = context

    def locate_associative(
        self,
        *,
        quotes: list[str],
        keywords: list[str],
        fragments: list[str] | None = None,
        chapter_keywords: list[str] | None = None,
        candidate_cap: int = ASSOC_CANDIDATE_CAP,
        intent: str | None = None,
        facets: list[list[str]] | None = None,
        diversify_per_book: int | None = None,
        diversify_by_author: bool = False,
        book_scope: "Collection[str] | None" = None,
        expand_synonyms: bool = True,
        pseudo_feedback: bool = False,
    ) -> list[Hit]:
        """编排：整句定位 + 逐字片段召回 + 关键词共现 + 篇章关键词加权，章节折叠并按权重综合打分。

        召回以片段精确命中为主力（经典语录被改写后仍保留可逐字命中的短语），整句定位负责
        命中规范译文时的高置信，关键词共现作为兜底，篇章关键词命中所属篇章标题再加权。
        返回的全部是真实 Hit；综合权重写回 ``Hit.score``（0-100），供上层聚合与按权重优先排序。

        ``intent=="research"``：额外按 ``facets``（论题各侧面的关键词组）分面共现召回，扩大跨著作
        覆盖面，并在最终结果上做按著作的多样性铺开（_diversify_by_book）；其它意图保持原行为。

        ``diversify_per_book``：任意调用方均可显式限制单一著作在结果里的占比（如随心问接地传 2）。
        分数并列时排序兜底键是 ``book_sort_order`` 升序，而《文集》的 sort_order 全库最小（10），
        会在大量并列分上霸榜；本参数把同一著作超额命中后置，让其它原著在首屏铺开。缺省 None 时
        保持原行为（仅 research 默认铺开），完全向后兼容。
        ``diversify_by_author=True``：配额按「著作群」计（马恩《文集》/《全集》/《全集·二版》三套版本
        合并为一个名额），避免同一文本的两套版本各占名额、把列宁/毛泽东等其它作者整体挤出首屏。
        ``book_scope``：限定本次召回只在给定书库内进行（按著作群定向检索用），使整个检索预算都花在
        范围内，从根上避免「问总书记却检索起马恩」——而非事后过滤丢弃已被别的作者挤掉的范围内命中。
        ``expand_synonyms``（默认 True）：关键词共现按 ``TERM_THESAURUS`` 做同义/译名概念组扩展，提升
        同义召回；``pseudo_feedback``（默认 False，research 意图自动开）：两趟检索，从首轮 top 命中正文
        回灌具区分度短语再做一趟片段召回，让语料自补查询未想到的措辞。二者都只影响「找到哪些真实命中」，
        不改「引文不可伪造」——最终引文仍是真实 Hit。
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
            for h in self.locate_quote(quote, allow_fuzzy=False, book_scope=book_scope):
                _add(h, h.score, ("quote", (quote or "")[:24]))

        # 2) 逐字片段召回：模型片段在前（更可信、优先占用检索预算）+ 候选原文自动切片在后
        frag_pool: list[str] = list(fragments or [])
        for quote in (quotes or [])[:ASSOC_MAX_QUOTES]:
            frag_pool.extend(self._shingle_fragments(quote))
        for h, frag in self.fragment_search(frag_pool, book_scope=book_scope):
            _add(h, h.score, ("frag", frag))

        # 3) 篇章定向：著作/篇章名命中标题 → 在该篇内定位主题词（“[著作] [主题]”型输入的强力路径）
        for h, ck in self.chapter_focused_search(chapter_keywords or [], keywords or [], book_scope=book_scope):
            _add(h, h.score, ("chapter", ck))

        # 4) 关键词共现兜底（研究意图放宽：任意 2 词共现 + 更宽窗口，扩大跨段/跨著作召回；
        #    覆盖度低的命中分值本就低、排在后面，不会顶掉强命中，只是把召回面铺得更广）。
        #    expand_synonyms 时按概念组召回同义/译名（如「异化」也召回只写「外化」的段落）。
        kw_window = ASSOC_RESEARCH_KEYWORD_WINDOW if intent == "research" else ASSOC_KEYWORD_WINDOW
        kw_min = 2 if intent == "research" else None
        for h in self.keyword_cooccurrence(keywords or [], window=kw_window, min_distinct=kw_min, book_scope=book_scope, expand_synonyms=expand_synonyms):
            _add(h, h.score, ("kw", None))

        # 5) 研究分面召回：每个侧面分别共现（同样放宽），扩大跨著作覆盖面（仅 research 传入 facets）
        for fi, fac_kws in enumerate(facets or []):
            for h in self.keyword_cooccurrence(fac_kws, window=kw_window, min_distinct=2, book_scope=book_scope, expand_synonyms=expand_synonyms):
                _add(h, h.score, ("facet", fi))

        # 6) 伪相关反馈（两趟检索，默认仅研究意图开）：从首轮 top 命中正文切「具区分度短语」回灌片段召回，
        #    让语料自己补出查询里没想到的措辞；第二趟经 fragment_search 频次闸自动去噪（太常见弃用）。
        if (pseudo_feedback or intent == "research") and hit_by_ch:
            top_keys = sorted(base_by_ch, key=lambda k: base_by_ch[k], reverse=True)[:ASSOC_PRF_TOP_HITS]
            fb_seen: set[str] = {normalize(f) for f in frag_pool}
            fb_frags: list[str] = []
            for _k in top_keys:
                _h = hit_by_ch.get(_k)
                if _h is None:
                    continue
                _ctx = str(getattr(_h, "context", "") or "").replace("[[H]]", "").replace("[[/H]]", "")
                for _frag in self._prf_phrases(_ctx):
                    if _frag not in fb_seen:
                        fb_seen.add(_frag)
                        fb_frags.append(_frag)
            if fb_frags:
                for h, frag in self.fragment_search(fb_frags, total_cap=ASSOC_PRF_FRAG_CAP, book_scope=book_scope):
                    _add(h, h.score, ("prf", frag))

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
        results = results[:candidate_cap]
        if diversify_per_book is not None:
            gk = self._author_group_key if diversify_by_author else None
            results = self._diversify_by_book(
                results, per_book_cap=diversify_per_book, group_key=gk
            )
        elif intent == "research":
            results = self._diversify_by_book(results)
        # Expand only final associative cards; recall and ranking remain unchanged.
        page_map_cache: OrderedDict[int, tuple[list[int], list[int]] | None] = OrderedDict()
        for hit in results:
            self._complete_associative_hit_context(hit, page_map_cache)
        return results

    def locate_subject_index(
        self, keywords: list[str], *, cap: int = 24,
        book_scope: "Collection[str] | None" = None,
    ) -> list[Hit]:
        """名目索引主题层（P2a）：把查询词与编辑手工建的索引词条匹配 → 取该词条的权威页 → 造真实段落 Hit。

        权威、零幻觉、可解释（Hit.subject_label 记录命中的索引词条，如「经济领域中的异化·劳动的异化」）。
        在编辑受控词表上做归一子串匹配；按「主词命中>子侧面命中、长词优先」排序，按页去重，
        建 Hit 时在该页内定位查询词以给出上下文与高亮。索引库缺失时返回空（研究检索退回纯词面召回）。
        ``book_scope`` 限定书库：范围外的词条整体跳过（名目索引目前仅《文集》，故范围排除《文集》时自然为空）。
        """
        if not self._subject_entries:
            return []
        kws: list[str] = []
        seen_kw: set[str] = set()
        for k in keywords or []:
            kn = normalize(str(k or ""))
            if len(kn) >= MIN_QUERY_LEN and kn not in seen_kw:
                seen_kw.add(kn)
                kws.append(kn)
            if len(kws) >= 12:
                break
        if not kws:
            return []
        scored: list[tuple[tuple[int, int], dict, str]] = []
        for e in self._subject_entries:
            nl, nt = e["norm_label"], e["norm_term"]
            best: tuple[tuple[int, int], str] | None = None
            for kw in kws:
                if kw in nl:
                    pr = 3 if kw == nt else (2 if kw in nt else 1)
                    cand = (pr, len(kw))
                    if best is None or cand > best[0]:
                        best = (cand, kw)
            if best:
                scored.append((best[0], e, best[1]))
        if not scored:
            return []
        scored.sort(key=lambda x: (-x[0][0], -x[0][1]))
        hits: list[Hit] = []
        seen_pages: set[tuple[str, int]] = set()
        per_label: dict[str, int] = {}
        per_term: dict[str, int] = {}
        for _pr, e, kw in scored:
            key = (e["source_file"], int(e["pdf_page"]))
            if key in seen_pages:
                continue
            lab, term = e["full_label"], e["term"]
            # 多样性：每词条≤2页、每主词≤5页，避免单一概念霸屏，让不同主题铺开
            if per_label.get(lab, 0) >= 2 or per_term.get(term, 0) >= 5:
                continue
            vol = self._volumes_by_source_file.get(e["source_file"])
            if vol is None:
                continue
            if not self._volume_in_scope(vol, book_scope):
                continue  # 名目索引词条落在检索范围（书库/卷）之外 → 跳过
            hit = self._subject_hit(vol, int(e["pdf_page"]), kw, lab)
            if hit is not None:
                hits.append(hit)
                seen_pages.add(key)
                per_label[lab] = per_label.get(lab, 0) + 1
                per_term[term] = per_term.get(term, 0) + 1
            if len(hits) >= cap:
                break
        return hits

    def _subject_hit(self, vol: Volume, pdf_page: int, keyword: str, label: str) -> Hit | None:
        pi = next((i for i, p in enumerate(vol.pages) if p.pdf_page == pdf_page), None)
        if pi is None:
            return None
        n = len(vol.page_offsets)
        # 在「该页 → 前一页 → 后一页」范围内找概念锚点并高亮——保证亮标不丢，且引文落到概念实际所在页。
        spans = [(vol.page_offsets[pi], vol.page_offsets[pi + 1] if pi + 1 < n else len(vol.norm_full))]
        if pi - 1 >= 0:
            spans.append((vol.page_offsets[pi - 1], vol.page_offsets[pi]))
        if pi + 2 < n:
            spans.append((vol.page_offsets[pi + 1], vol.page_offsets[pi + 2]))
        # 锚点优先级：查询词 → 词条各层(主词/子侧面) → 词条 2-gram(兜底)
        anchors: list[str] = []
        seen_a: set[str] = set()

        def _add_anchor(s: str) -> None:
            ns = normalize(s)
            if len(ns) >= 2 and ns not in seen_a:
                seen_a.add(ns)
                anchors.append(ns)

        _add_anchor(keyword)
        for part in label.split("·"):
            _add_anchor(part)
        nl = normalize(label.replace("·", ""))
        for i in range(len(nl) - 1):
            _add_anchor(nl[i:i + 2])
        pos, found = -1, ""
        for a in anchors:
            for s, e in spans:
                p = vol.norm_full.find(a, s, e)
                if p >= 0:
                    pos, found = p, a
                    break
            if pos >= 0:
                break
        if pos < 0:
            pos = vol.page_offsets[pi]
            found = vol.norm_full[pos:pos + 6] or "x"
        hit = self._make_hit(vol, pos, pos + max(1, len(found)), "exact", 100, found)
        hit.subject_label = label
        return hit

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
            error_counts = [hit.fuzzy_errors for hit in group_hits if hit.fuzzy_errors is not None]
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
                    fuzzy_errors=min(error_counts) if error_counts else None,
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
        fuzzy_errors: int | None = None,
        highlight_terms: list[str] | None = None,
        export_complete_sentence: bool = False,
        export_page_map_cache: OrderedDict[int, tuple[list[int], list[int]] | None] | None = None,
    ) -> Hit:
        norm_end = max(norm_end, norm_start + 1)
        start_pi = vol.page_index_at(norm_start)
        end_pi = vol.page_index_at(norm_end - 1)
        pages = vol.pages[start_pi:end_pi + 1]

        context = None
        if export_complete_sentence:
            context = self._extract_export_sentence_context(
                vol,
                norm_start,
                norm_end,
                highlight_terms=highlight_terms,
                page_map_cache=export_page_map_cache,
            )
        # 映射失败只降级为旧的有界片段，不得让个别异常 OCR 中断整单导出。
        if context is None and highlight_terms:
            context = self._extract_context_multi(pages, highlight_terms)
        elif context is None:
            # 近似命中：高亮定位必须用语料侧的命中片段（它与页面原文逐字一致），
            # 不能用带错字的用户查询——否则逐字正则必失配，退化为「页首 200 字、无高亮」。
            highlight_src = q_raw
            if match_type == "fuzzy":
                highlight_src = vol.norm_full[norm_start:norm_end]
            context = self._extract_context(pages, highlight_src, occurrence_index)
        citation = self._make_citation(vol.book, vol.volume, pages, source_file=vol.source_file)
        citations = self._make_citations(vol.book, vol.volume, pages, source_file=vol.source_file)
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
            citations=citations,
            section_title=section_title,
            fuzzy_errors=fuzzy_errors,
            norm_start=norm_start,
            norm_end=norm_end,
        )

    @staticmethod
    def _export_page_raw_map(
        page: Page,
        cache: OrderedDict[int, tuple[list[int], list[int]] | None] | None,
    ) -> tuple[list[int], list[int]] | None:
        """Map normalized character positions back to raw-text spans, with a tiny LRU.

        A page map is built only while that page is actively producing export hits.
        Keeping at most eight pages avoids a corpus-sized third representation while
        still making thousands of sequential hits on the same pages inexpensive.
        """
        key = id(page)
        if cache is not None and key in cache:
            cached = cache[key]
            cache.move_to_end(key)
            return cached
        starts: list[int] = []
        ends: list[int] = []
        normalized_parts: list[str] = []
        for raw_index, character in enumerate(page.raw_text):
            normalized = normalize(character)
            if not normalized:
                continue
            normalized_parts.append(normalized)
            starts.extend([raw_index] * len(normalized))
            ends.extend([raw_index + 1] * len(normalized))
        result: tuple[list[int], list[int]] | None
        if "".join(normalized_parts) == page.norm_text and len(starts) == len(page.norm_text):
            result = (starts, ends)
        else:
            # NFKC/OpenCC 极少数组合字可能存在跨字符语境，不猜测映射。
            result = None
        if cache is not None:
            cache[key] = result
            cache.move_to_end(key)
            while len(cache) > EXPORT_PAGE_MAP_CACHE_SIZE:
                cache.popitem(last=False)
        return result

    @staticmethod
    def _export_sentence_bounds(raw: str, match_start: int, match_end: int) -> tuple[int, int, bool, bool]:
        """Return the complete sentence/paragraph range containing a raw match."""
        sentence_left = 0
        for boundary in _EXPORT_SENTENCE_END_RE.finditer(raw, 0, max(0, match_start)):
            sentence_left = max(sentence_left, boundary.end())
        left = sentence_left
        # PDF 文本层常把版式换行抽取成多个空行，因此句号等真实句界
        # 优先于段落界。只在左侧找不到句界时，才用段落界阻止向前无限扩展。
        if sentence_left == 0:
            for boundary in _EXPORT_PARAGRAPH_BREAK_RE.finditer(raw, 0, max(0, match_start)):
                left = max(left, boundary.end())

        sentence_end = _EXPORT_SENTENCE_END_RE.search(raw, max(0, match_end))
        if sentence_end is not None:
            right = sentence_end.end()
        else:
            paragraph_end = _EXPORT_PARAGRAPH_BREAK_RE.search(raw, max(0, match_end))
            right = paragraph_end.start() if paragraph_end is not None else len(raw)

        while left < match_start and raw[left].isspace():
            left += 1
        while right > match_end and raw[right - 1].isspace():
            right -= 1

        original_left, original_right = left, right
        if right - left > EXPORT_SENTENCE_MAX_CHARS:
            match_width = max(1, match_end - match_start)
            spare = max(0, EXPORT_SENTENCE_MAX_CHARS - match_width)
            left = max(left, match_start - spare // 2)
            right = min(right, left + EXPORT_SENTENCE_MAX_CHARS)
            if right < match_end:
                right = match_end
                left = max(original_left, right - EXPORT_SENTENCE_MAX_CHARS)
            if right - left < EXPORT_SENTENCE_MAX_CHARS:
                left = max(original_left, right - EXPORT_SENTENCE_MAX_CHARS)
        return left, right, left > original_left, right < original_right

    def _extract_export_sentence_context(
        self,
        vol: Volume,
        norm_start: int,
        norm_end: int,
        *,
        highlight_terms: list[str] | None = None,
        page_map_cache: OrderedDict[int, tuple[list[int], list[int]] | None] | None = None,
    ) -> str | None:
        """Return complete sentence(s) containing an exact/co-occurrence hit.

        Citation pages remain the actual hit pages.  Only sentence discovery may
        inspect one adjacent page on either side, which also handles sentences split
        by a PDF page break without loading another book or corpus copy.
        """
        norm_start = max(0, min(int(norm_start), len(vol.norm_full) - 1))
        norm_end = max(norm_start + 1, min(int(norm_end), len(vol.norm_full)))
        start_page_index = vol.page_index_at(norm_start)
        end_page_index = vol.page_index_at(norm_end - 1)
        context_first = max(0, start_page_index - 1)
        context_last = min(len(vol.pages) - 1, end_page_index + 1)
        context_pages = vol.pages[context_first:context_last + 1]
        raw_offsets: list[int] = []
        cursor = 0
        for page in context_pages:
            raw_offsets.append(cursor)
            cursor += len(page.raw_text) + 1  # one joining newline
        raw = "\n".join(page.raw_text for page in context_pages)

        start_map = self._export_page_raw_map(vol.pages[start_page_index], page_map_cache)
        end_map = self._export_page_raw_map(vol.pages[end_page_index], page_map_cache)
        if start_map is None or end_map is None:
            return None
        local_norm_start = norm_start - vol.page_offsets[start_page_index]
        local_norm_end = norm_end - vol.page_offsets[end_page_index]
        if not (0 <= local_norm_start < len(start_map[0])):
            return None
        if not (1 <= local_norm_end <= len(end_map[1])):
            return None
        start_raw_offset = raw_offsets[start_page_index - context_first]
        end_raw_offset = raw_offsets[end_page_index - context_first]
        match_start = start_raw_offset + start_map[0][local_norm_start]
        match_end = end_raw_offset + end_map[1][local_norm_end - 1]
        if not (0 <= match_start < match_end <= len(raw)):
            return None

        left, right, clipped_left, clipped_right = self._export_sentence_bounds(
            raw, match_start, match_end,
        )
        highlight_spans: list[tuple[int, int]] = []
        if highlight_terms:
            sentence = raw[left:right]
            for term in highlight_terms:
                keep = [character for character in term if not _STRIP_RE.match(character)]
                if not keep:
                    continue
                pattern = r"\W*".join(re.escape(character) for character in keep)
                highlight_spans.extend(
                    (left + match.start(), left + match.end())
                    for match in re.finditer(pattern, sentence)
                )
        if not highlight_spans:
            highlight_spans = [(match_start, match_end)]
        highlight_spans.sort()
        merged: list[list[int]] = []
        for start, end in highlight_spans:
            start, end = max(left, start), min(right, end)
            if start >= end:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        pieces: list[str] = ["…"] if clipped_left else []
        cursor = left
        for start, end in merged:
            pieces.extend((raw[cursor:start], "[[H]]", raw[start:end], "[[/H]]"))
            cursor = end
        pieces.append(raw[cursor:right])
        if clipped_right:
            pieces.append("…")
        return "".join(pieces).strip()

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

    def _extract_context_multi(self, pages: list[Page], terms: list[str]) -> str:
        """同段多词上下文：在所在页原文里把每个关键词的每处出现都用 [[H]] 标注，
        并以「关键词最密集的一段」为中心截取上下文。

        与 _extract_context 一样用「字之间允许任意非字词字符」的宽松正则匹配（兼容
        OCR 在字间插入的空白/标点），故归一化命中能在原始 raw 文本上重新定位并高亮。
        """
        raw = "\n".join(p.raw_text for p in pages)
        spans: list[tuple[int, int, int]] = []  # (start, end, term_id)
        for tid, term in enumerate(terms):
            keep = [c for c in term if not _STRIP_RE.match(c)]
            if not keep:
                continue
            pattern = r"\W*".join(re.escape(c) for c in keep)
            spans.extend((m.start(), m.end(), tid) for m in re.finditer(pattern, raw))
        if not spans:
            return raw[:200].replace("\n", " ")
        spans.sort()
        # 合并重叠/相接区间，避免嵌套 [[H]]（保留覆盖到的关键词集合，用于定位最佳窗口）
        merged: list[list] = []  # [start, end, set(term_ids)]
        for s, e, tid in spans:
            if merged and s <= merged[-1][1]:
                if e > merged[-1][1]:
                    merged[-1][1] = e
                merged[-1][2].add(tid)
            else:
                merged.append([s, e, {tid}])
        # 选最佳 ~COOC_CTX_MAXLEN 字窗口为中心：先看窗口内**不同关键词数**最多（确保各词都在视野内），
        # 同样多再比命中次数；保证「无产阶级 革命」这类多词的每个词都在截出的上下文里被高亮。
        best_idx = 0
        best_key = (-1, -1)
        for i, (s, _e, _ids) in enumerate(merged):
            ids: set = set()
            cnt = 0
            for (s2, _e2, ids2) in merged:
                if s <= s2 < s + COOC_CTX_MAXLEN:
                    ids |= ids2
                    cnt += 1
            key = (len(ids), cnt)
            if key > best_key:
                best_key = key
                best_idx = i
        anchor_start = merged[best_idx][0]
        a = max(0, anchor_start - CTX_PAD)
        b = min(len(raw), a + COOC_CTX_MAXLEN + CTX_PAD)
        pieces: list[str] = []
        cur = a
        for s, e, _ids in merged:
            if e <= a or s >= b:
                continue
            s = max(s, a)
            e = min(e, b)
            if s < cur:  # 与上一段被裁剪后相接，跳过
                continue
            pieces.append(raw[cur:s])
            pieces.append("[[H]]")
            pieces.append(raw[s:e])
            pieces.append("[[/H]]")
            cur = e
        pieces.append(raw[cur:b])
        return "".join(pieces).replace("\n", " ").strip()

    # 公文类书库（党代会报告 / 全会公报）：每个 PDF 即一篇独立公文，无「卷·页」概念，
    # 引文按「篇名（年份）」出，而非「《书名》第N卷…第N页」。
    _DOC_CITATION_BOOKS = {"历次党代会报告", "历届全会公报", "五年规划"}
    # 《重要文献选编》：分册用 上/中/下，而非「第N卷」
    _XUANBIAN_BOOKS = {"十八大以来重要文献选编", "十九大以来重要文献选编", "二十大以来重要文献选编"}
    _XUANBIAN_VOL_CN = {1: "上", 2: "中", 3: "下"}

    def _citation_year(self, book: str, volume: int, source_file: str | None = None):
        """Resolve a cited volume year without mutating the protected metadata.

        A non-numeric printed volume label proves that the requested volume is
        another part of the same edition.  If volumes.yaml has exactly one
        reviewed year for that edition, reuse it for the missing part rather
        than emitting the placeholder ``xxxx``.
        """
        file_years = self.volumes_cfg.get("file_years") or {}
        if source_file and file_years.get(source_file):
            return file_years[source_file]
        years = self.volumes_cfg.get(book, {}) or {}
        year = years.get(volume, "")
        if year:
            return year
        book_cfg = self.get_book_config(book)
        if not dict(book_cfg.volume_labels).get(volume):
            return ""
        reviewed = {value for key, value in years.items() if str(key).lstrip("-").isdigit() and value}
        return next(iter(reviewed)) if len(reviewed) == 1 else ""

    def _make_citation(self, book: str, volume: int, pages: list[Page], source_file: str | None = None) -> str:
        # 显式权威引文覆盖：party_docs_meta 内某卷给定完整 cite 串则直接采用
        # （用于五年规划等——每部给规范单行本/出处引文）。
        _cite = ((self.party_meta.get(book, {}) or {}).get(volume, {}) or {}).get("cite")
        if _cite:
            return _cite
        if book in self._DOC_CITATION_BOOKS:
            from pathlib import Path as _P
            meta = (self.party_meta.get(book, {}) or {}).get(volume, {}) or {}
            doc_title = meta.get("title") or (_P(source_file).stem if source_file else self.get_book_config(book).citation_title)
            # 全会公报：纸本权威出处用《人民日报》（见报日期＝闭幕次日，头版）
            rb_date = meta.get("rb_date")
            if book == "历届全会公报" and rb_date:
                return f"《{doc_title}》，《人民日报》{rb_date}，第1版。"
            author = meta.get("author")
            date = meta.get("date")
            if not date:
                yr = self.volumes_cfg.get(book, {}).get(volume, "")
                date = f"{yr}年" if yr else ""
            prefix = f"{author}：" if author else ""
            suffix = f"（{date}）" if date else ""
            # 报告若有《人民日报》全文见报日期则附纸本出处
            if rb_date:
                return f"{prefix}《{doc_title}》{suffix}，《人民日报》{rb_date}，第1版。"
            return f"{prefix}《{doc_title}》{suffix}。"
        # 分册年份优先：同一卷分多册、各册年份不同的（如马恩《全集》第 26 卷三册），按 source_file
        # 在 file_years 里单独取年份；未命中再回退到「卷→年」映射。
        file_years = self.volumes_cfg.get("file_years") or {}
        year = self._citation_year(book, volume, source_file)
        book_cfg = self.get_book_config(book)
        publisher = book_cfg.publisher or self.volumes_cfg.get("publisher", "人民出版社")
        place = book_cfg.place or self.volumes_cfg.get("place", "北京")

        page_str = citation_pages(pages)["page"]

        responsibility = ""
        if book_cfg.authors:
            responsibility = "、".join(book_cfg.authors) + "："
        elif book_cfg.editors:
            responsibility = "、".join(book_cfg.editors) + "编："
        elif book_cfg.organizers:
            responsibility = "、".join(book_cfg.organizers) + "整理："
        responsibility_tail: list[str] = []
        if book_cfg.translators:
            responsibility_tail.append("、".join(book_cfg.translators) + "译")
        if book_cfg.authors and book_cfg.editors:
            responsibility_tail.append("、".join(book_cfg.editors) + "编")
        if (book_cfg.authors or book_cfg.editors) and book_cfg.organizers:
            responsibility_tail.append("、".join(book_cfg.organizers) + "整理")
        translated = ("，" + "，".join(responsibility_tail)) if responsibility_tail else ""
        volume_label = dict(book_cfg.volume_labels).get(volume, "")

        if book in self._XUANBIAN_BOOKS:
            editor = ((self.party_meta.get(book, {}) or {}).get(volume, {}) or {}).get("editor")
            prefix = f"{editor}编：" if editor else ""
            title = f"{prefix}《{book_cfg.citation_title}》（{self._XUANBIAN_VOL_CN.get(volume, str(volume))}）"
        elif book_cfg.single_volume or volume in book_cfg.unnumbered_volumes:
            # 单卷本独立著作（各《学习纲要》《概论》），或多卷本中该卷本身无卷次
            # （如《治国理政》卷1 我们用的 2014 无卷次初版）：不冠「第N卷」
            title = f"{responsibility}《{book_cfg.citation_title}》{translated}"
        elif volume_label:
            title = f"{responsibility}《{book_cfg.citation_title}》{volume_label}{translated}"
        else:
            title = f"{responsibility}《{book_cfg.citation_title}》第{volume}{book_cfg.volume_unit}{translated}"
        year_str = f"{year}年" if year else "xxxx年"
        edition_bits = [x for x in (book_cfg.edition_note, book_cfg.source_edition) if x]
        edition_suffix = f"（{'；'.join(edition_bits)}）" if edition_bits else ""
        return f"{title}，{place}：{publisher}，{year_str}{edition_suffix}，{page_str}。"

    # 引文格式标识：与前端「引用格式」下拉一致。
    #   gb2025 = 国标 GB/T 7714—2025（独立可配置模板）
    #   gb2015 = 国标 GB/T 7714—2015（专著 [M]，半角标点）
    #   zgshkx = 《中国社会科学》脚注体例
    #   mkszyj = 《马克思主义研究》脚注体例
    # 两刊脚注当前为同一写法（均带出版地、不加「版」字，与既有 _make_citation 一致），
    # 故 zgshkx/mkszyj 暂同源；保留两个独立键，以便日后任一刊微调而互不影响。
    CITATION_FORMATS = ("gb2025", "gb2015", "zgshkx", "mkszyj")

    def set_citation_templates(self, templates: dict | None) -> None:
        """注入后台自定义的引用格式模板（仅 CITATION_FORMATS 内的键、非空字符串生效）。

        由 app 层在启动时与后台保存后调用；单进程多线程 waitress 下，更新本对象即对所有
        请求线程即时生效（持久化在 app 层的设置项里，重启后再注入）。
        """
        clean: dict[str, str] = {}
        if isinstance(templates, dict):
            for key in self.CITATION_FORMATS:
                tpl = templates.get(key)
                if isinstance(tpl, dict):  # 容忍 {"template": "..."} 形态
                    tpl = tpl.get("template")
                tpl = str(tpl or "").strip()
                if tpl:
                    clean[key] = tpl
        self.citation_templates = clean

    def _citation_parts(self, book: str, volume: int, pages: list[Page], source_file: str | None = None) -> dict[str, str]:
        """标准「卷·页」型著作的引文字段，供自定义模板替换（公文/选编等特殊体例不经此处）。"""
        file_years = self.volumes_cfg.get("file_years") or {}
        year = self._citation_year(book, volume, source_file)
        book_cfg = self.get_book_config(book)
        publisher = book_cfg.publisher or self.volumes_cfg.get("publisher", "人民出版社")
        place = book_cfg.place or self.volumes_cfg.get("place", "北京")
        pagination = citation_pages(pages)
        page, page_range, page_note = (pagination[k] for k in ("page", "page_range", "page_note"))
        return {
            "title": book_cfg.citation_title,
            "authors": "、".join(book_cfg.authors),
            "translators": "、".join(book_cfg.translators),
            "editors": "、".join(book_cfg.editors),
            "organizers": "、".join(book_cfg.organizers),
            "edition_note": book_cfg.edition_note,
            "source_edition": book_cfg.source_edition,
            "volume": dict(book_cfg.volume_labels).get(volume, str(volume)),
            "place": place,
            "publisher": publisher,
            "year": f"{year}" if year else "xxxx",
            "page": page,
            "page_range": page_range,
            "page_note": page_note,
        }

    def _make_citations(self, book: str, volume: int, pages: list[Page], source_file: str | None = None) -> dict[str, str]:
        """产出多格式引文，供前端「引用格式」下拉即时切换。

        默认走 _make_citation（脚注体例）/_make_citation_gb（国标）；后台若对某格式配了自定义
        模板，则该格式按模板渲染——仅限标准「卷·页」型著作（公文/选编/显式 cite 覆盖等特殊体例
        无规范卷页结构，一律沿用默认权威串、不套模板）。模板渲染异常时回退默认，绝不崩引文。
        """
        journal = self._make_citation(book, volume, pages, source_file=source_file)
        gb = self._make_citation_gb(book, volume, pages, source_file=source_file)
        tpls = self.citation_templates or {}
        has_override = bool(((self.party_meta.get(book, {}) or {}).get(volume, {}) or {}).get("cite"))
        # 单卷本 / 无卷次的个别卷也走默认程序化串：自定义模板固定含「第{volume}卷」，套上会错标卷次。
        _bc = self.get_book_config(book)
        # volume_unit != 卷（如两套《重要文献选编》按「册」分册）也走默认串：模板写死了
        # 「第{volume}卷」，套上会把「第17册」错标成「第17卷」。
        special = (book in self._DOC_CITATION_BOOKS or book in self._XUANBIAN_BOOKS
                   or has_override or _bc.single_volume or volume in _bc.unnumbered_volumes
                   or _bc.volume_unit != "卷" or bool(dict(_bc.volume_labels).get(volume))
                   or bool(_bc.editors or _bc.organizers or _bc.edition_note or _bc.source_edition))
        if special or not tpls:
            return {"gb2025": gb, "gb2015": gb, "zgshkx": journal, "mkszyj": journal}
        parts = self._citation_parts(book, volume, pages, source_file=source_file)

        def _render(key: str, default: str) -> str:
            tpl = tpls.get(key)
            if not tpl:
                return default
            try:
                rendered = tpl.format_map(_CiteSafeDict(parts))
                if parts['page_note'] and parts['page_note'] not in rendered:
                    rendered += parts['page_note']
                return rendered
            except Exception:
                return default

        return {
            "gb2025": _render("gb2025", gb),
            "gb2015": _render("gb2015", gb),
            "zgshkx": _render("zgshkx", journal),
            "mkszyj": _render("mkszyj", journal),
        }

    def _make_citation_gb(self, book: str, volume: int, pages: list[Page], source_file: str | None = None) -> str:
        """国标 GB/T 7714—2015 专著著录（[M]，半角标点）。

        经用户确认：马列经典多卷本不冠主要责任者，径以题名起首——
        ``题名:第N卷[M].出版地:出版者,出版年:引文页码.``，例：
        ``马克思恩格斯文集:第1卷[M].北京:人民出版社,2009:123.``
        公文/选编/显式 cite 覆盖等无规范「卷·页」结构的特殊体例，沿用权威注释串（与脚注体例同形）。
        """
        _cite = ((self.party_meta.get(book, {}) or {}).get(volume, {}) or {}).get("cite")
        if _cite:
            return _cite
        if book in self._DOC_CITATION_BOOKS or book in self._XUANBIAN_BOOKS:
            return self._make_citation(book, volume, pages, source_file=source_file)
        file_years = self.volumes_cfg.get("file_years") or {}
        year = self._citation_year(book, volume, source_file)
        book_cfg = self.get_book_config(book)
        publisher = book_cfg.publisher or self.volumes_cfg.get("publisher", "人民出版社")
        place = book_cfg.place or self.volumes_cfg.get("place", "北京")
        pagination = citation_pages(pages)
        page_str, page_note = pagination["page_range"], pagination["page_note"]
        year_str = f"{year}" if year else "xxxx"
        volume_label = dict(book_cfg.volume_labels).get(volume, "")
        if book_cfg.single_volume or volume in book_cfg.unnumbered_volumes:
            vol_seg = ""
        elif volume_label:
            vol_seg = f":{volume_label}"
        else:
            vol_seg = f":第{volume}{book_cfg.volume_unit}"
        if book_cfg.authors:
            author_seg = ",".join(book_cfg.authors)
        elif book_cfg.editors:
            author_seg = ",".join(book_cfg.editors) + ",编"
        else:
            author_seg = ",".join(book_cfg.organizers) + (",整理" if book_cfg.organizers else "")
        author_prefix = f"{author_seg}." if author_seg else ""
        tail_parts: list[str] = []
        if book_cfg.translators:
            tail_parts.append(f"{','.join(book_cfg.translators)},译")
        if book_cfg.authors and book_cfg.editors:
            tail_parts.append(f"{','.join(book_cfg.editors)},编")
        if (book_cfg.authors or book_cfg.editors) and book_cfg.organizers:
            tail_parts.append(f"{','.join(book_cfg.organizers)},整理")
        translator_seg = ("." + ".".join(tail_parts)) if tail_parts else ""
        edition_bits = [x for x in (book_cfg.edition_note, book_cfg.source_edition) if x]
        edition_suffix = f"（{'；'.join(edition_bits)}）" if edition_bits else ""
        return (
            f"{author_prefix}{book_cfg.citation_title}{vol_seg}[M]{translator_seg}."
            f"{place}:{publisher},{year_str}{edition_suffix}:{page_str}{page_note}."
        )

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
        positions: dict[str, set[int]] = {}
        for page in volume.pages:
            token = _parse_page_token(page.printed_page or "")
            if token:
                positions.setdefault(token, set()).add(page.pdf_page)
        for entry in entries:
            token = _parse_page_token(entry.title)
            if token:
                positions.setdefault(token, set()).add(entry.pdf_page)
        for token, candidates in positions.items():
            if len(candidates) == 1:
                page_map.setdefault(token, next(iter(candidates)))
            else:
                page_map.pop(token, None)
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
        # 去掉书签里夹带的双向控制符（U+200E/200F 等），否则路径类书签首尾的 LRM 标记
        # 会绕过扫描垃圾识别，且在界面里显示为不可见乱码。
        text = re.sub(r"[‎‏‪-‮⁦-⁩]", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _is_navigable_title(self, title: str) -> bool:
        title = self._clean_title(title)
        if not title:
            return False
        if _NUMERIC_TITLE_RE.fullmatch(title) is not None:
            return False
        # 扫描文件名/盘符路径类书签不是可导航篇目（见 _SCAN_ARTIFACT_TITLE_RE）。
        if _SCAN_ARTIFACT_TITLE_RE.search(title):
            return False
        return True

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
