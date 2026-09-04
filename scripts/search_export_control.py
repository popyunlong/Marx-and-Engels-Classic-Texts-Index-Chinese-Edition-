from __future__ import annotations

"""Operate the homepage search-export kill switch without restarting Flask."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import search_exports  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="首页引文汇编导出动态开关")
    parser.add_argument("action", choices=("status", "enable", "disable"))
    args = parser.parse_args()
    if args.action == "enable":
        search_exports.set_feature_enabled(True)
    elif args.action == "disable":
        search_exports.set_feature_enabled(False)
    payload = {
        "enabled": search_exports.feature_enabled(),
        "worker_available": search_exports.worker_available(),
        "database": str(search_exports.DB_PATH),
        "artifacts": str(search_exports.ARTIFACT_ROOT),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
