"""Build toc_entries for configured book collections.

The generic path is intentionally conservative: it primarily records PDF
bookmarks, which are reliable for the Lenin PDFs, and leaves existing
collection-specific scripts available for hand-cleaned Marx/Engels data.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from book_config import book_config_map, load_book_configs  # noqa: E402
from build_index import DB_PATH  # noqa: E402
from search import Corpus  # noqa: E402


HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")
NOISE_TITLES = {"封面", "书名", "版权", "版权页", "目录", "插图目录"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS toc_entries (
            book         TEXT    NOT NULL,
            volume       INTEGER NOT NULL,
            source_file  TEXT    NOT NULL,
            title        TEXT    NOT NULL,
            pdf_page     INTEGER NOT NULL,
            printed_page TEXT,
            level        INTEGER NOT NULL,
            kind         TEXT    NOT NULL,
            sort_order   INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_toc_entries_source_order ON toc_entries(source_file, sort_order)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_toc_entries_book_volume ON toc_entries(book, volume, sort_order)")


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def entry_kind(title: str) -> str:
    if title.endswith("年") and title[:-1].isdigit():
        return "year"
    return "body"


def build_entries_for_volume(corpus: Corpus, volume_obj) -> list[dict[str, Any]]:
    printed_by_pdf = {page.pdf_page: page.printed_page for page in volume_obj.pages}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    page_count = len(volume_obj.pages)
    for entry in corpus.get_toc_entries(volume_obj.source_file):
        title = str(getattr(entry, "title", "") or "").strip()
        pdf_page = int(getattr(entry, "pdf_page", 0) or 0)
        if not title or title in NOISE_TITLES or pdf_page < 1:
            continue
        if page_count and pdf_page > page_count:
            continue
        key = (pdf_page, title)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "title": title,
                "pdf_page": pdf_page,
                "printed_page": getattr(entry, "printed_page", None) or printed_by_pdf.get(pdf_page),
                "level": max(1, min(6, int(getattr(entry, "level", 1) or 1))),
                "kind": str(getattr(entry, "kind", "") or entry_kind(title)),
                "sort_order": len(rows) + 1,
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build toc_entries for configured books.")
    parser.add_argument("--book", action="append", help="Book key to build. Repeat for multiple books.")
    parser.add_argument("--all", action="store_true", help="Build every configured book.")
    parser.add_argument(
        "--replace",
        action="store_true",
        default=True,
        help="Replace toc_entries for selected books. Enabled by default.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configs = book_config_map()
    selected = args.book or (list(configs) if args.all else ["列宁全集"])
    unknown = [book for book in selected if book not in configs]
    if unknown:
        raise SystemExit("未知书库：" + ", ".join(unknown))

    corpus = Corpus.load_default()
    payload: list[tuple[Any, ...]] = []
    summary: list[str] = []
    empty: list[str] = []

    for book in selected:
        volumes = corpus.get_volumes(book)
        if not volumes:
            empty.append(f"{book}: 无卷册")
            continue
        for volume_obj in volumes:
            entries = build_entries_for_volume(corpus, volume_obj)
            if not entries:
                empty.append(f"{book}第 {volume_obj.volume} 卷")
            summary.append(f"{book}第 {volume_obj.volume} 卷：{len(entries)} 项")
            for entry in entries:
                payload.append(
                    (
                        book,
                        volume_obj.volume,
                        volume_obj.source_file,
                        entry["title"],
                        entry["pdf_page"],
                        entry.get("printed_page"),
                        entry["level"],
                        entry["kind"],
                        entry["sort_order"],
                    )
                )

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_schema(conn)
        for book in selected:
            conn.execute("DELETE FROM toc_entries WHERE book = ?", (book,))
        conn.executemany(
            """
            INSERT INTO toc_entries (
                book, volume, source_file, title, pdf_page,
                printed_page, level, kind, sort_order
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        conn.commit()
    finally:
        conn.close()

    update_hash()
    print("\n".join(summary))
    if empty:
        print("以下卷未生成目录：" + "；".join(empty))
    print(f"已写入 {len(payload)} 条目录，书库：{', '.join(selected)}。")


if __name__ == "__main__":
    main()
