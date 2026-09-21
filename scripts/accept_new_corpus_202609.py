# -*- coding: utf-8 -*-
"""离线验收新增语料的检索、引文、目录直达和阅读器源文件映射。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from search import Corpus, Page  # noqa: E402
from scripts.build_new_corpus_202609 import EXPECTED_FILES, EXPECTED_PAGES, NEW_BOOKS  # noqa: E402

_CJK_RUN = re.compile(r"[\u3400-\u9fff]{12,}")


def _manifest() -> list[tuple[str, dict]]:
    data = yaml.safe_load((ROOT / "config" / "manifest.yaml").read_text(encoding="utf-8")) or {}
    return [(book, dict(row)) for book in NEW_BOOKS for row in (data.get(book) or [])]


def _search_sample(
    corpus: Corpus,
    conn: sqlite3.Connection,
    book: str,
    volume: int,
    source_file: str,
) -> tuple[tuple, str, list] | None:
    """Choose a real body-page phrase that demonstrably retrieves this volume.

    Front matter is repeated across many volumes, so taking the first non-empty
    page creates false failures once a series grows beyond the result cap.  Start
    at the first body TOC landing and try bounded CJK phrases until the actual
    full-corpus search returns the intended stable source file.
    """
    body_start = conn.execute(
        "SELECT MIN(pdf_page) FROM toc_entries WHERE book=? AND volume=? AND kind='body'",
        (book, volume),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT pdf_page,printed_page,raw_text,normalized_text FROM pages "
        "WHERE book=? AND volume=? AND pdf_page>=? AND length(normalized_text)>=24 "
        "ORDER BY pdf_page LIMIT 80",
        (book, volume, int(body_start or 1)),
    ).fetchall()
    for row in rows:
        for match in _CJK_RUN.finditer(str(row[2] or "")):
            run = match.group(0)
            offsets = range(0, max(1, len(run) - 15), 8)
            for offset in offsets:
                query = run[offset:offset + 16]
                if len(query) < 12:
                    continue
                hits = corpus.search(query, max_results=200)
                if any(hit.source_file == source_file for hit in hits):
                    return row, query, hits
    return None


def accept(
    db: Path,
    pdf_root: Path | None,
    *,
    corpus: Corpus | None = None,
    structural_only: bool = False,
) -> dict:
    rows = _manifest()
    if corpus is None and not structural_only:
        corpus = Corpus(db_path=db)
    failures: list[str] = []
    samples: list[dict] = []
    with sqlite3.connect(db) as conn:
        page_total = conn.execute(
            f"SELECT COUNT(*) FROM pages WHERE book IN ({','.join('?' for _ in NEW_BOOKS)})", NEW_BOOKS
        ).fetchone()[0]
        for book, item in rows:
            volume = int(item["volume"])
            source_file = str(item["file"])
            if structural_only:
                toc_count = conn.execute(
                    "SELECT COUNT(*) FROM toc_entries WHERE book=? AND volume=?",
                    (book, volume),
                ).fetchone()[0]
                if not toc_count:
                    failures.append(f"目录为空：{book} v{volume}")
                bad_landings = conn.execute(
                    "SELECT COUNT(*) FROM toc_entries WHERE book=? AND volume=? "
                    "AND (pdf_page<1 OR pdf_page>?)",
                    (book, volume, int(item["page_count"])),
                ).fetchone()[0]
                if bad_landings:
                    failures.append(f"目录落点越界：{book} v{volume}")
                if pdf_root is not None and not (pdf_root / Path(*Path(source_file).parts[1:])).is_file():
                    failures.append(f"阅读器PDF缺失：{source_file}")
                continue
            assert corpus is not None
            toc = corpus.get_toc_entries(source_file)
            if not toc:
                failures.append(f"目录为空：{book} v{volume}")
                continue
            if any(not (1 <= int(entry.pdf_page) <= int(item["page_count"])) for entry in toc):
                failures.append(f"目录落点越界：{book} v{volume}")
            sample = _search_sample(corpus, conn, book, volume, source_file)
            if sample is None:
                failures.append(f"正文抽样未检索回本卷：{book} v{volume}")
                continue
            row, query, hits = sample
            page = Page(int(row[0]), str(row[1]) if row[1] is not None else None, str(row[2]), str(row[3]))
            citations = corpus._make_citations(book, volume, [page], source_file=source_file)
            if not all(str(citations.get(fmt) or "").strip() for fmt in Corpus.CITATION_FORMATS):
                failures.append(f"引文缺格式：{book} v{volume}")
            if pdf_root is not None and not (pdf_root / Path(*Path(source_file).parts[1:])).is_file():
                failures.append(f"阅读器PDF缺失：{source_file}")
            samples.append({"book": book, "volume": volume, "query": query,
                            "hit_count": len(hits), "toc_count": len(toc),
                            "citation": citations["zgshkx"]})
    if len(rows) != EXPECTED_FILES or page_total != EXPECTED_PAGES:
        failures.append(f"总量异常：{len(rows)}册/{page_total}页")
    # 年份目录必须是一级，文献标题必须是二级；总目录不得进入可搜索 pages。
    with sqlite3.connect(db) as conn:
        bad_levels = conn.execute(
            "SELECT COUNT(*) FROM toc_entries WHERE book IN (?,?) AND "
            "((kind='year' AND level<>1) OR (kind='body' AND level<>2))",
            ("中共中央文件选集（1921—1949）", "中共中央文件选集（1949—1966）"),
        ).fetchone()[0]
        aux_hits = conn.execute("SELECT COUNT(*) FROM pages WHERE source_file LIKE '%总目录%'").fetchone()[0]
    if bad_levels: failures.append(f"中央文件目录层级异常={bad_levels}")
    if aux_hits: failures.append(f"辅助总目录进入正文索引={aux_hits}")
    return {"ok": not failures, "files": len(rows), "pages": page_total,
            "samples": samples, "failures": failures}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--pdf-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--structural-only", action="store_true",
                        help="只检查数据库结构、目录落点和PDF路径，不加载全文检索对象")
    args = parser.parse_args()
    result = accept(args.db, args.pdf_root, structural_only=args.structural_only)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
