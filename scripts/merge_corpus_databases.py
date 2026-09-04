#!/usr/bin/env python3
"""Create a new corpus by appending one configured collection to a full base.

Both input databases are opened read-only.  The output path must not exist and
must differ from both inputs, which makes accidental replacement impossible.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import yaml


PAGE_COLUMNS = (
    "book",
    "volume",
    "source_file",
    "pdf_page",
    "printed_page",
    "raw_text",
    "normalized_text",
)
TOC_COLUMNS = (
    "book",
    "volume",
    "source_file",
    "title",
    "pdf_page",
    "printed_page",
    "level",
    "kind",
    "sort_order",
)


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _collection_books(path: Path, collection: str) -> list[str]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    keys = [
        str(item["key"])
        for item in payload.get("books", [])
        if isinstance(item, dict) and item.get("collection") == collection and item.get("key")
    ]
    if not keys:
        raise ValueError(f"collection {collection!r} has no books in {path}")
    return keys


def _source_keys(conn: sqlite3.Connection, books: list[str] | None = None) -> set[tuple[str, int, str]]:
    params: tuple[object, ...] = ()
    where = ""
    if books is not None:
        where = " WHERE book IN ({})".format(",".join("?" for _ in books))
        params = tuple(books)
    return {
        (str(book), int(volume), str(source_file))
        for book, volume, source_file in conn.execute(
            "SELECT DISTINCT book, volume, source_file FROM pages" + where,
            params,
        )
    }


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def merge_collection(
    *,
    base: Path,
    overlay: Path,
    output: Path,
    books_config: Path,
    collection: str,
) -> dict[str, object]:
    for source in (base, overlay, books_config):
        if not source.is_file():
            raise FileNotFoundError(source)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    output_parent = output.parent.resolve()
    if not output_parent.is_dir():
        raise FileNotFoundError(output_parent)
    output_resolved = output_parent / output.name
    if output_resolved in {base.resolve(), overlay.resolve()}:
        raise ValueError("output must differ from both input databases")

    selected_books = _collection_books(books_config, collection)
    with closing(_open_read_only(base)) as base_conn, closing(_open_read_only(overlay)) as overlay_conn:
        base_sources = _source_keys(base_conn)
        overlay_sources = _source_keys(overlay_conn, selected_books)
        overlay_books = {key[0] for key in overlay_sources}
        missing_books = sorted(set(selected_books) - overlay_books)
        if missing_books:
            raise ValueError(f"overlay has no page rows for configured books: {missing_books}")
        overlap = sorted(base_sources & overlay_sources)
        if overlap:
            raise ValueError(
                f"base already contains {len(overlap)} selected source files; "
                "refusing an ambiguous merge"
            )
        base_counts = {
            "sources": len(base_sources),
            "pages": _row_count(base_conn, "pages"),
            "toc_entries": _row_count(base_conn, "toc_entries"),
        }
        overlay_counts = {
            "sources": len(overlay_sources),
            "pages": int(
                overlay_conn.execute(
                    "SELECT COUNT(*) FROM pages WHERE book IN ({})".format(
                        ",".join("?" for _ in selected_books)
                    ),
                    selected_books,
                ).fetchone()[0]
            ),
            "toc_entries": int(
                overlay_conn.execute(
                    "SELECT COUNT(*) FROM toc_entries WHERE book IN ({})".format(
                        ",".join("?" for _ in selected_books)
                    ),
                    selected_books,
                ).fetchone()[0]
            ),
        }

    # copy2 creates a new file.  It never opens either source for writing.
    shutil.copy2(base, output)
    with sqlite3.connect(output) as conn:
        conn.execute("ATTACH DATABASE ? AS overlay", (overlay.resolve().as_uri() + "?mode=ro",))
        placeholders = ",".join("?" for _ in selected_books)
        for table, columns in (("pages", PAGE_COLUMNS), ("toc_entries", TOC_COLUMNS)):
            column_sql = ",".join(columns)
            conn.execute(
                f"INSERT INTO main.{table} ({column_sql}) "
                f"SELECT {column_sql} FROM overlay.{table} WHERE book IN ({placeholders})",
                selected_books,
            )
        conn.commit()
        conn.execute("DETACH DATABASE overlay")
        check = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
        if check != ["ok"]:
            raise ValueError(f"merged candidate failed SQLite quick_check: {check}")
        merged_counts = {
            "sources": len(_source_keys(conn)),
            "pages": _row_count(conn, "pages"),
            "toc_entries": _row_count(conn, "toc_entries"),
        }

    expected_counts = {
        name: base_counts[name] + overlay_counts[name]
        for name in ("sources", "pages", "toc_entries")
    }
    if merged_counts != expected_counts:
        raise ValueError(
            f"merged counts differ from exact additive expectation: "
            f"actual={merged_counts}, expected={expected_counts}"
        )
    return {
        "ok": True,
        "base": str(base.resolve()),
        "overlay": str(overlay.resolve()),
        "output": str(output.resolve()),
        "collection": collection,
        "books": selected_books,
        "base_counts": base_counts,
        "overlay_counts": overlay_counts,
        "merged_counts": merged_counts,
        "quick_check": "ok",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--books", type=Path, required=True)
    parser.add_argument("--collection", required=True)
    args = parser.parse_args(argv)
    try:
        result = merge_collection(
            base=args.base,
            overlay=args.overlay,
            output=args.output,
            books_config=args.books,
            collection=args.collection,
        )
    except Exception as exc:
        result = {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"]}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
