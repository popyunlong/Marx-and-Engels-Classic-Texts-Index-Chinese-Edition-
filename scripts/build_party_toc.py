# -*- coding: utf-8 -*-
"""为「历次党代会报告 / 历届全会公报」两个公文类书库写 toc_entries（定向、不动其它书库）。

这两个库每个 PDF 即一篇独立公文（一报告/一公报），无书签、无多级目录，
因此目录就是「每卷一条」：篇名取 source_file 文件名（与 manifest 一致、稳定可读），
pdf_page=1、printed_page='1'、level=1、kind='body'、sort_order=volume。

须在 build_textbook_index（写 pages）之后运行。只 DELETE/INSERT 指定 book 的
toc_entries 行，其余书库不动；完成重算 corpus.sqlite 的 sha256。

用法：python scripts/build_party_toc.py [--book 历次党代会报告 --book 历届全会公报]
"""
from __future__ import annotations
import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import BUILD_DB_PATH  # noqa: E402

HASH_PATH = BUILD_DB_PATH.with_suffix(BUILD_DB_PATH.suffix + ".sha256")
DEFAULT_BOOKS = ["历次党代会报告", "历届全会公报", "五年规划"]


def update_hash() -> None:
    digest = hashlib.sha256()
    with BUILD_DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def build(books: list[str]) -> None:
    conn = sqlite3.connect(str(BUILD_DB_PATH))
    try:
        for book in books:
            rows = conn.execute(
                "SELECT DISTINCT volume, source_file FROM pages WHERE book=? ORDER BY volume",
                (book,),
            ).fetchall()
            if not rows:
                print(f"[{book}] pages 表无该书库行，跳过（先跑 build_textbook_index）。", file=sys.stderr)
                continue
            conn.execute("DELETE FROM toc_entries WHERE book=?", (book,))
            payload = [
                (book, vol, sf, Path(sf).stem, 1, "1", 1, "body", vol)
                for vol, sf in rows
            ]
            conn.executemany(
                "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
                "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
                payload,
            )
            print(f"[{book}] 写入目录 {len(payload)} 条。")
        conn.commit()
    finally:
        conn.close()
    update_hash()
    print("完成，sha256 已刷新。")


def main() -> None:
    ap = argparse.ArgumentParser(description="构建公文类书库的 toc_entries。")
    ap.add_argument("--book", action="append", help="书库 key，可重复；默认两库全建。")
    args = ap.parse_args()
    build(args.book or DEFAULT_BOOKS)


if __name__ == "__main__":
    main()
