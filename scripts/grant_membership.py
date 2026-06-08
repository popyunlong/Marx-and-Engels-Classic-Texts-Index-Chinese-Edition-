from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from membership import create_manual_subscription, init_membership_db, list_active_plans


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为指定用户人工开通会员")
    parser.add_argument("email", help="用户邮箱")
    parser.add_argument(
        "--plan",
        default="monthly",
        help="套餐代码，默认 monthly，可选值见 --list-plans",
    )
    parser.add_argument(
        "--note",
        default="",
        help="备注，会记录到订单和订阅中",
    )
    parser.add_argument(
        "--list-plans",
        action="store_true",
        help="只列出当前可用套餐",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    init_membership_db()

    if args.list_plans:
        for plan in list_active_plans():
            print(f"{plan['code']}: {plan['name']} ({plan['price_cents'] / 100:.2f} {plan['currency']})")
        return 0

    result = create_manual_subscription(
        user_email=args.email,
        plan_code=args.plan,
        note=args.note,
    )
    order = result["order"] or {}
    subscription = result["subscription"] or {}
    print(f"用户: {result['user']['email']}")
    print(f"订单: {order.get('order_no', '')}")
    print(f"套餐: {subscription.get('plan_name', '')}")
    print(f"到期: {subscription.get('expires_at', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
