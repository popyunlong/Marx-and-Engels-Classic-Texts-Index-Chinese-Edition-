#!/usr/bin/env python3
"""Fail closed when a corpus candidate loses configured or live content.

The gate is deliberately read-only.  It opens both SQLite databases in URI
read-only mode and never creates, updates, or deletes a database file.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml


SourceKey = tuple[str, int, str]


@dataclass(frozen=True)
class SourceStats:
    pages: int
    toc_entries: int


def _normalise_source_file(value: object) -> str:
    return str(PurePosixPath(str(value).replace("\\", "/")))


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_public_books(path: Path) -> set[str]:
    payload = _read_yaml(path) or {}
    books = payload.get("books", []) if isinstance(payload, dict) else []
    result: set[str] = set()
    for book in books:
        if not isinstance(book, dict) or book.get("available", True) is False:
            continue
        key = str(book.get("key", "")).strip()
        if key:
            result.add(key)
    if not result:
        raise ValueError(f"no public books found in {path}")
    return result


def load_expected_sources(path: Path, public_books: set[str]) -> set[SourceKey]:
    payload = _read_yaml(path) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"manifest root must be a mapping: {path}")

    result: set[SourceKey] = set()
    missing_sections = sorted(public_books - set(map(str, payload)))
    if missing_sections:
        raise ValueError(f"public books missing from manifest: {missing_sections}")

    for book in sorted(public_books):
        entries = payload.get(book) or []
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"manifest has no volumes for public book {book!r}")
        for entry in entries:
            if not isinstance(entry, dict) or "file" not in entry or "volume" not in entry:
                raise ValueError(f"invalid manifest entry for {book!r}: {entry!r}")
            result.add((book, int(entry["volume"]), _normalise_source_file(entry["file"])))
    return result


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _require_tables(conn: sqlite3.Connection, label: str) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('pages','toc_entries')"
        )
    }
    missing = sorted({"pages", "toc_entries"} - tables)
    if missing:
        raise ValueError(f"{label} is missing required tables: {missing}")


def _source_counts(conn: sqlite3.Connection, table: str) -> dict[SourceKey, int]:
    return {
        (str(book), int(volume), _normalise_source_file(source_file)): int(count)
        for book, volume, source_file, count in conn.execute(
            f"SELECT book, volume, source_file, COUNT(*) FROM {table} "
            "GROUP BY book, volume, source_file"
        )
    }


def read_catalog(path: Path, *, quick_check: bool = True) -> tuple[dict[SourceKey, SourceStats], str]:
    with closing(_open_read_only(path)) as conn:
        _require_tables(conn, str(path))
        check_result = "skipped"
        if quick_check:
            rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
            check_result = "; ".join(rows)
            if rows != ["ok"]:
                raise ValueError(f"SQLite quick_check failed for {path}: {check_result}")
        pages = _source_counts(conn, "pages")
        toc = _source_counts(conn, "toc_entries")
    return (
        {
            key: SourceStats(page_count, toc.get(key, 0))
            for key, page_count in pages.items()
        },
        check_result,
    )


def _serialise_keys(keys: Iterable[SourceKey]) -> list[dict[str, object]]:
    return [
        {"book": book, "volume": volume, "source_file": source_file}
        for book, volume, source_file in sorted(keys)
    ]


def evaluate_gate(
    *,
    candidate: Path,
    books: Path,
    manifest: Path,
    baseline: Path | None = None,
    quick_check: bool = True,
) -> dict[str, Any]:
    public_books = load_public_books(books)
    expected = load_expected_sources(manifest, public_books)
    candidate_catalog, candidate_check = read_catalog(candidate, quick_check=quick_check)
    candidate_public = {key for key in candidate_catalog if key[0] in public_books}

    missing_manifest = expected - candidate_public
    unexpected_public = candidate_public - expected
    missing_baseline: set[SourceKey] = set()
    page_regressions: list[dict[str, object]] = []
    toc_regressions: list[dict[str, object]] = []
    baseline_catalog: dict[SourceKey, SourceStats] = {}
    baseline_check: str | None = None

    if baseline is not None:
        baseline_catalog, baseline_check = read_catalog(baseline, quick_check=quick_check)
        missing_baseline = set(baseline_catalog) - set(candidate_catalog)
        for key in sorted(set(baseline_catalog) & set(candidate_catalog)):
            old = baseline_catalog[key]
            new = candidate_catalog[key]
            if new.pages < old.pages:
                page_regressions.append(
                    {
                        **_serialise_keys([key])[0],
                        "baseline": old.pages,
                        "candidate": new.pages,
                    }
                )
            if new.toc_entries < old.toc_entries:
                toc_regressions.append(
                    {
                        **_serialise_keys([key])[0],
                        "baseline": old.toc_entries,
                        "candidate": new.toc_entries,
                    }
                )

    errors: list[str] = []
    if missing_manifest:
        errors.append(f"missing {len(missing_manifest)} configured source files")
    if unexpected_public:
        errors.append(f"found {len(unexpected_public)} unconfigured public source files")
    if missing_baseline:
        errors.append(f"lost {len(missing_baseline)} source files present in baseline")
    if page_regressions:
        errors.append(f"page rows decreased for {len(page_regressions)} source files")
    if toc_regressions:
        errors.append(f"TOC rows decreased for {len(toc_regressions)} source files")

    candidate_books = {key[0] for key in candidate_public}
    candidate_volumes = {(key[0], key[1]) for key in candidate_public}
    result: dict[str, Any] = {
        "ok": not errors,
        "errors": errors,
        "candidate": str(candidate.resolve()),
        "baseline": str(baseline.resolve()) if baseline is not None else None,
        "quick_check": {"candidate": candidate_check, "baseline": baseline_check},
        "counts": {
            "public_books": len(candidate_books),
            "public_book_volumes": len(candidate_volumes),
            "public_source_files": len(candidate_public),
            "all_source_files": len(candidate_catalog),
            "page_rows": sum(item.pages for item in candidate_catalog.values()),
            "toc_rows": sum(item.toc_entries for item in candidate_catalog.values()),
            "configured_books": len(public_books),
            "configured_source_files": len(expected),
            "baseline_source_files": len(baseline_catalog) if baseline is not None else None,
        },
        "missing_manifest": _serialise_keys(missing_manifest),
        "unexpected_public": _serialise_keys(unexpected_public),
        "missing_baseline": _serialise_keys(missing_baseline),
        "page_regressions": page_regressions,
        "toc_regressions": toc_regressions,
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--books", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--skip-quick-check",
        action="store_true",
        help="tests only: skip SQLite PRAGMA quick_check",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = evaluate_gate(
            candidate=args.candidate,
            baseline=args.baseline,
            books=args.books,
            manifest=args.manifest,
            quick_check=not args.skip_quick_check,
        )
    except Exception as exc:
        result = {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"]}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
