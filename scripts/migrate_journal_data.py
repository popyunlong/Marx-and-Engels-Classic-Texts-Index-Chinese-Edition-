from __future__ import annotations

"""Copy legacy journal tables into the data-disk journal database and verify."""

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import journal_alerts as journals
from journal_storage import JOURNAL_DB_PATH, ensure_journal_storage, journal_storage_status


def _counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    conn = sqlite3.connect(path)
    try:
        tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'journal_%'"
            )
        }
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in journals._LEGACY_JOURNAL_TABLES
            if table in tables
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="perform the idempotent copy")
    args = parser.parse_args()
    ensure_journal_storage()
    legacy = Path(journals.LEGACY_DB_PATH)
    before = _counts(legacy)
    print({"storage": journal_storage_status(), "legacy": str(legacy), "legacy_counts": before})
    if not args.execute:
        print("dry run only; rerun with --execute")
        return 0
    journals.init_journal_alerts_db()
    after = _counts(Path(JOURNAL_DB_PATH))
    failures = {
        table: {"legacy": count, "target": after.get(table, 0)}
        for table, count in before.items()
        if after.get(table, 0) < count
    }
    if failures:
        print({"ok": False, "count_mismatches": failures}, file=sys.stderr)
        return 2
    with journals._connect() as conn:
        enabled_en = int(conn.execute(
            "SELECT COUNT(*) FROM journal_sources WHERE language='en' AND is_enabled=1"
        ).fetchone()[0])
        enabled_zh = int(conn.execute(
            "SELECT COUNT(*) FROM journal_sources WHERE lower(language) LIKE 'zh%' AND is_enabled=1"
        ).fetchone()[0])
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    ok = enabled_en == 45 and enabled_zh == 0 and quick_check == "ok"
    print({
        "ok": ok,
        "target": str(JOURNAL_DB_PATH),
        "target_counts": after,
        "enabled_english_sources": enabled_en,
        "enabled_chinese_sources": enabled_zh,
        "quick_check": quick_check,
    })
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
