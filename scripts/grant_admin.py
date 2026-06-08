from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from membership import get_user_by_email, init_membership_db, update_user_account


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提升已注册账号为管理员")
    parser.add_argument("--email", required=True, help="已注册用户的邮箱")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    init_membership_db()
    user = get_user_by_email(args.email)
    if user is None:
        print(f"未找到用户：{args.email}", file=sys.stderr)
        return 1

    updated = update_user_account(int(user["id"]), role="admin")
    if updated is None:
        print(f"管理员提升失败：{args.email}", file=sys.stderr)
        return 1

    print(f"已提升为管理员：{updated['email']}")
    print(f"角色：{updated.get('role') or ''}")
    print(f"账号状态：{'active' if updated.get('is_active') else 'inactive'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
