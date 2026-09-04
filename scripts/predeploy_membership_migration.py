"""8·15 会员库的可回滚部署前迁移。

在新版本进程启动前运行。脚本会用 SQLite backup API 创建 WAL 一致性
快照，然后执行幂等的增量迁移并做 quick_check。任一步失败都以非零码
结束，部署系统必须保持旧进程继续服务。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import membership  # noqa: E402


REQUIRED_TABLES = {
    "ai_price_versions",
    "ai_price_audit",
    "ai_wallets",
    "ai_wallet_ledger",
    "ai_budget_reservations",
    "ai_reservation_allocations",
    "ai_provider_calls",
}
REQUIRED_RESERVATION_COLUMNS = {
    "reserved_micros",
    "reserved_tokens",
    "settled_micros",
    "status",
}
REQUIRED_PLAN_COLUMNS = {
    "tier_rank",
    "model_policy",
    "parallel_group",
    "ai_budget_micros",
    "weekly_token_limit",
    "billing_cycle_days",
    "sale_starts_at",
    "sale_ends_at",
}


def _inspect(db_path: Path) -> dict:
    with sqlite3.connect(db_path, timeout=15) as conn:
        conn.execute("PRAGMA busy_timeout=15000")
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        plan_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(plans)")}
        reservation_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(ai_budget_reservations)")
        }
        plan_codes = {
            str(row[0])
            for row in conn.execute(
                "SELECT code FROM plans WHERE code IN ('support_basic','support_plus','support_pro','support_max','research_pack')"
            )
        }
    missing_tables = sorted(REQUIRED_TABLES - tables)
    missing_columns = sorted(REQUIRED_PLAN_COLUMNS - plan_columns)
    missing_reservation_columns = sorted(REQUIRED_RESERVATION_COLUMNS - reservation_columns)
    missing_plans = sorted(
        {"support_basic", "support_plus", "support_pro", "support_max", "research_pack"} - plan_codes
    )
    return {
        "quick_check": quick_check,
        "missing_tables": missing_tables,
        "missing_plan_columns": missing_columns,
        "missing_reservation_columns": missing_reservation_columns,
        "missing_plans": missing_plans,
        "ok": (
            quick_check == "ok"
            and not missing_tables
            and not missing_columns
            and not missing_reservation_columns
            and not missing_plans
        ),
    }


def _backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"membership-pre-815-{stamp}.sqlite3"
    with sqlite3.connect(db_path, timeout=15) as source, sqlite3.connect(target) as destination:
        source.execute("PRAGMA busy_timeout=15000")
        source.backup(destination)
        if str(destination.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise RuntimeError("迁移前备份未通过 SQLite quick_check")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true", help="只检查，不执行迁移")
    parser.add_argument(
        "--database",
        type=Path,
        default=membership.DB_PATH,
        help="会员 SQLite 路径；部署默认使用运行时会员库。",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=membership.DB_PATH.parent / "migration_backups",
    )
    parser.add_argument("--max-seconds", type=float, default=30.0)
    args = parser.parse_args()

    db_path = args.database.resolve()
    membership.DB_PATH = db_path
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"会员库不存在: {db_path}"}, ensure_ascii=False))
        return 2
    before = _inspect(db_path)
    if args.check_only:
        print(json.dumps({"database": str(db_path), **before}, ensure_ascii=False, indent=2))
        return 0 if before["ok"] else 3

    started = time.monotonic()
    backup_path = _backup(db_path, args.backup_dir)
    membership.init_membership_db()
    wallet_migration = membership.migrate_active_ai_wallets()
    elapsed = time.monotonic() - started
    after = _inspect(db_path)
    result = {
        "database": str(db_path),
        "backup": str(backup_path),
        "elapsed_seconds": round(elapsed, 3),
        "wallet_migration": wallet_migration,
        **after,
    }
    if elapsed > max(1.0, args.max_seconds):
        result["ok"] = False
        result["error"] = f"迁移耗时 {elapsed:.3f}s，超过上限 {args.max_seconds:.3f}s"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
