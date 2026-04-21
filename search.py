"""
检索核心：在《文集》《全集》中定位引文出处，组装 GB-like 引文。

用法（独立调用）：
    from search import Corpus
    corpus = Corpus.load_default()
    hits = corpus.search("请粘贴需要核查的引文")
"""
from __future__ import annotations

import bisect
import re
import sqlite3
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from build_index import DB_PATH, VOLUMES, normalize, _STRIP_RE


MIN_QUERY_LEN = 4         # 归一化后少于此长度不检索，避免海量误命中
FUZZY_THRESHOLD = 85      # 模糊匹配的分数下限
CTX_PAD = 40              # 上下文前后字符数


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
class Volume:
    book: str
    volume: int
    pages: list[Page]
    norm_full: str = ""
    page_offsets: list[int] = field(default_factory=list)

    @classmethod
    def build(cls, book: str, volume: int, pages: list[Page]) -> "Volume":
        offsets = [0]
        parts: list[str] = []
        for p in pages:
            parts.append(p.norm_text)
            offsets.append(offsets[-1] + len(p.norm_text))
        return cls(book=book, volume=volume, pages=pages,
                   norm_full="".join(parts), page_offsets=offsets)

    def page_index_at(self, norm_idx: int) -> int:
        """二分查找 norm_idx 落在哪一页。"""
        i = bisect.bisect_right(self.page_offsets, norm_idx) - 1
        return max(0, min(i, len(self.pages) - 1))


@dataclass
class Hit:
    book: str
    volume: int
    pages: list[Page]
    match_type: str         # "exact" | "fuzzy"
    score: int
    context: str
    citation: str

    def to_dict(self) -> dict:
        return {
            "book": self.book,
            "volume": self.volume,
            "pdf_pages": [p.pdf_page for p in self.pages],
            "printed_pages": [p.printed_page for p in self.pages],
            "match_type": self.match_type,
            "score": self.score,
            "context": self.context,
            "citation": self.citation,
        }


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------
class Corpus:
    def __init__(self, db_path: Path = DB_PATH, volumes_cfg_path: Path = VOLUMES) -> None:
        self.volumes_cfg: dict = yaml.safe_load(
            volumes_cfg_path.read_text(encoding="utf-8")
        )
        self.books: dict[str, list[Volume]] = {"文集": [], "全集": []}
        self._load(db_path)

    @classmethod
    def load_default(cls) -> "Corpus":
        return cls()

    def _load(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT book, volume, pdf_page, printed_page, raw_text, normalized_text "
            "FROM pages ORDER BY book, volume, pdf_page"
        ).fetchall()
        conn.close()
        for (book, vol), grp in groupby(rows, key=lambda r: (r[0], r[1])):
            pgs = [Page(pdf_page=r[2], printed_page=r[3],
                        raw_text=r[4], norm_text=r[5]) for r in grp]
            if book in self.books:
                self.books[book].append(Volume.build(book, vol, pgs))
        for book in self.books:
            self.books[book].sort(key=lambda v: v.volume)

    # ------------------------------------------------------------------
    # 检索入口
    # ------------------------------------------------------------------
    def search(self, q: str, max_results: int = 5) -> list[Hit]:
        q_norm = normalize(q)
        if len(q_norm) < MIN_QUERY_LEN:
            return []

        for book in ("文集", "全集"):
            hits = self._exact_in_book(book, q_norm, q)
            if hits:
                return hits[:max_results]

        # 无精确命中，模糊兜底（两套书一起算，按分数排序）
        fuzzy: list[Hit] = []
        for book in ("文集", "全集"):
            fuzzy.extend(self._fuzzy_in_book(book, q_norm, q))
        fuzzy.sort(key=lambda h: (-h.score, h.book != "文集"))
        return fuzzy[:max_results]

    # ------------------------------------------------------------------
    # 精确匹配
    # ------------------------------------------------------------------
    def _exact_in_book(self, book: str, q_norm: str, q_raw: str) -> list[Hit]:
        hits: list[Hit] = []
        for vol in self.books.get(book, []):
            start = 0
            while True:
                i = vol.norm_full.find(q_norm, start)
                if i < 0:
                    break
                hits.append(self._make_hit(vol, i, i + len(q_norm), "exact", 100, q_raw))
                start = i + 1
                if len(hits) >= 20:
                    break
        return hits

    # ------------------------------------------------------------------
    # 模糊匹配
    # ------------------------------------------------------------------
    def _fuzzy_in_book(self, book: str, q_norm: str, q_raw: str) -> list[Hit]:
        hits: list[Hit] = []
        for vol in self.books.get(book, []):
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
        return hits

    # ------------------------------------------------------------------
    # 命中结构与引文
    # ------------------------------------------------------------------
    def _make_hit(self, vol: Volume, norm_start: int, norm_end: int,
                  match_type: str, score: int, q_raw: str) -> Hit:
        norm_end = max(norm_end, norm_start + 1)
        start_pi = vol.page_index_at(norm_start)
        end_pi = vol.page_index_at(norm_end - 1)
        pages = vol.pages[start_pi:end_pi + 1]

        context = self._extract_context(pages, q_raw)
        citation = self._make_citation(vol.book, vol.volume, pages)

        return Hit(
            book=vol.book, volume=vol.volume, pages=pages,
            match_type=match_type, score=score,
            context=context, citation=citation,
        )

    def _extract_context(self, pages: list[Page], q_raw: str) -> str:
        raw = "\n".join(p.raw_text for p in pages)
        # 构造宽松正则：查询字之间允许出现任意非字词字符（空白、标点、换行）
        keep = [c for c in q_raw if not _STRIP_RE.match(c)]
        if not keep:
            return raw[:200].replace("\n", " ")
        pattern = r"\W*".join(re.escape(c) for c in keep)
        m = re.search(pattern, raw)
        if m:
            s, e = m.span()
            a = max(0, s - CTX_PAD)
            b = min(len(raw), e + CTX_PAD)
            snippet = raw[a:s] + "【" + raw[s:e] + "】" + raw[e:b]
            return snippet.replace("\n", " ").strip()
        # 兜底：截取开头
        return raw[:200].replace("\n", " ")

    def _make_citation(self, book: str, volume: int, pages: list[Page]) -> str:
        year = self.volumes_cfg.get(book, {}).get(volume, "")
        publisher = self.volumes_cfg.get("publisher", "人民出版社")
        place = self.volumes_cfg.get("place", "北京")

        printed_nums = [p.printed_page for p in pages
                        if p.printed_page and not p.printed_page.startswith("pre-")]
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

        title = f"《马克思恩格斯{book}》第{volume}卷"
        year_str = f"{year}年" if year else "xxxx年"
        return f"{title}，{place}：{publisher}，{year_str}，{page_str}。"


if __name__ == "__main__":
    import sys
    corpus = Corpus.load_default()
    q = " ".join(sys.argv[1:]) or input("请输入引文：")
    results = corpus.search(q)
    if not results:
        print("未找到匹配。")
    for i, h in enumerate(results, 1):
        print(f"\n[{i}] {h.match_type} score={h.score}")
        print(f"  书={h.book}  卷={h.volume}  PDF页={[p.pdf_page for p in h.pages]}  "
              f"印刷页={[p.printed_page for p in h.pages]}")
        print(f"  上下文：{h.context}")
        print(f"  引文：{h.citation}")
