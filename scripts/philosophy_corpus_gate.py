#!/usr/bin/env python3
"""Fail closed unless a candidate adds only the pinned philosophy corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

try:
    from .philosophy_catalog import load_volumes
except ImportError:  # pragma: no cover
    from philosophy_catalog import load_volumes


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _quick_check(conn: sqlite3.Connection) -> str:
    result = "; ".join(str(row[0]) for row in conn.execute("PRAGMA quick_check"))
    if result != "ok":
        raise ValueError(f"SQLite quick_check failed: {result}")
    return result


def _feed(digest: Any, values: Iterable[Any]) -> None:
    for value in values:
        data = ("" if value is None else str(value)).encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)


def schema_digest(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    )
    for row in rows:
        _feed(digest, row)
    return digest.hexdigest()


def protected_digest(conn: sqlite3.Connection, table: str, new_books: set[str]) -> str:
    if table == "pages":
        columns = "id, book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text"
        order = "id"
    elif table == "toc_entries":
        columns = "book, volume, source_file, title, pdf_page, printed_page, level, kind, sort_order"
        order = "book, volume, source_file, sort_order, pdf_page, title"
    else:
        raise ValueError(table)
    digest = hashlib.sha256()
    placeholders = ",".join("?" for _ in new_books)
    sql = f"SELECT {columns} FROM {table}"
    params: tuple[str, ...] = tuple(sorted(new_books))
    if params:
        sql += f" WHERE book NOT IN ({placeholders})"
    sql += f" ORDER BY {order}"
    for row in conn.execute(sql, params):
        _feed(digest, row)
    return digest.hexdigest()


def evaluate(baseline: Path, candidate: Path) -> dict[str, Any]:
    specs = load_volumes()
    new_books = {item.book for item in specs}
    expected_pages = sum(item.pages for item in specs)
    failures: list[str] = []
    volume_reports: list[dict[str, Any]] = []
    with closing(_open_read_only(baseline)) as old, closing(_open_read_only(candidate)) as new:
        old_check = _quick_check(old)
        new_check = _quick_check(new)
        old_schema = schema_digest(old)
        new_schema = schema_digest(new)
        if old_schema != new_schema:
            failures.append("schema changed")
        protected: dict[str, dict[str, str]] = {}
        for table in ("pages", "toc_entries"):
            old_digest = protected_digest(old, table, new_books)
            new_digest = protected_digest(new, table, new_books)
            protected[table] = {"baseline": old_digest, "candidate": new_digest}
            if old_digest != new_digest:
                failures.append(f"protected {table} rows changed")

        new_page_total = int(
            new.execute(
                f"SELECT COUNT(*) FROM pages WHERE book IN ({','.join('?' for _ in new_books)})",
                tuple(sorted(new_books)),
            ).fetchone()[0]
        )
        if new_page_total != expected_pages:
            failures.append(f"new page total is {new_page_total}/{expected_pages}")
        for item in specs:
            page_count, nonempty, printed = new.execute(
                "SELECT COUNT(*), SUM(CASE WHEN length(trim(raw_text))>0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN printed_page IS NOT NULL AND trim(printed_page)<>'' THEN 1 ELSE 0 END) "
                "FROM pages WHERE book=? AND volume=? AND source_file=?",
                (item.book, item.volume, item.file),
            ).fetchone()
            toc_count = int(
                new.execute(
                    "SELECT COUNT(*) FROM toc_entries WHERE book=? AND volume=? AND source_file=?",
                    (item.book, item.volume, item.file),
                ).fetchone()[0]
            )
            printed_rows = [
                (int(pdf_page), int(str(printed_page)))
                for pdf_page, printed_page in new.execute(
                    "SELECT pdf_page, printed_page FROM pages "
                    "WHERE book=? AND volume=? AND source_file=? "
                    "AND printed_page IS NOT NULL AND trim(printed_page)<>'' ORDER BY pdf_page",
                    (item.book, item.volume, item.file),
                )
                if str(printed_page).strip().isdigit()
            ]
            declared_resets = set(item.printed_page_resets)
            observed_resets = {
                current[0]
                for previous, current in zip(printed_rows, printed_rows[1:])
                if current[1] < previous[1]
            }
            printed_violations = [
                {
                    "previous_pdf_page": previous[0],
                    "previous_printed_page": previous[1],
                    "pdf_page": current[0],
                    "printed_page": current[1],
                }
                for previous, current in zip(printed_rows, printed_rows[1:])
                if current[1] < previous[1] and current[0] not in declared_resets
            ]
            missing_declared_resets = sorted(declared_resets - observed_resets)
            reasons: list[str] = []
            if int(page_count or 0) != item.pages:
                reasons.append(f"pages={page_count}/{item.pages}")
            if int(nonempty or 0) < 1:
                reasons.append("no-nonempty-text")
            if int(printed or 0) < 1:
                reasons.append("no-printed-page")
            if printed_violations:
                reasons.append(f"printed-page-regressions={len(printed_violations)}")
            if missing_declared_resets:
                reasons.append(f"missing-declared-page-resets={missing_declared_resets}")
            if toc_count < 1:
                reasons.append("no-toc")
            volume_reports.append(
                {
                    "id": item.id,
                    "pages": int(page_count or 0),
                    "nonempty_pages": int(nonempty or 0),
                    "printed_pages": int(printed or 0),
                    "printed_page_monotonic": not printed_violations,
                    "declared_printed_page_resets": sorted(declared_resets),
                    "observed_printed_page_resets": sorted(observed_resets),
                    "printed_page_violations": printed_violations[:20],
                    "toc_entries": toc_count,
                    "passed": not reasons,
                    "reasons": reasons,
                }
            )
            if reasons:
                failures.append(f"{item.id}: " + ", ".join(reasons))
    return {
        "schema": "kant-feuerbach-corpus-gate-v1",
        "baseline": str(baseline.resolve()),
        "candidate": str(candidate.resolve()),
        "quick_check": {"baseline": old_check, "candidate": new_check},
        "schema_sha256": {"baseline": old_schema, "candidate": new_schema},
        "protected_sha256": protected,
        "expected_new_pages": expected_pages,
        "candidate_new_pages": new_page_total,
        "volumes": volume_reports,
        "errors": failures,
        "passed": not failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.baseline, args.candidate)
    except Exception as exc:  # noqa: BLE001 - structured fail-closed CLI
        report = {"passed": False, "errors": [f"{type(exc).__name__}: {exc}"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
