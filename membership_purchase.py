"""Multi-month order snapshots; no changes to administrator-maintained plan copy."""
from __future__ import annotations

import json
import re
from datetime import timedelta

FEATURE_KEY = "membership_multimonth_enabled"
DISCOUNT_KEY = "membership_graduated_discounts_enabled"
SNAPSHOT_VERSION = 1


def enabled() -> bool:
    from admin_store import get_setting
    return get_setting(FEATURE_KEY, False) is True


def graduated_enabled() -> bool:
    from admin_store import get_setting
    return get_setting(DISCOUNT_KEY, False) is True


def discount_offer() -> str:
    if graduated_enabled():
        return "购买 3 个月享 9 折，4 个月享 8.5 折，5 个月享 8 折，6 个月享 7.5 折"
    return "购买 3–5 个月享 9 折，购买 6 个月享 8 折"


def validate_months(value=1) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-6]", str(value)):
        raise ValueError("购买月数须为 1 至 6 的整数。")
    return int(value)


def eligible(plan: dict) -> bool:
    return (str(plan.get("kind") or "membership") == "membership"
            and str(plan.get("parallel_group") or "") == "new_membership"
            and int(plan.get("billing_cycle_days") or 30) == 30)


def quote(plan: dict, months=1) -> dict:
    months = validate_months(months)
    unit = int(plan["price_cents"])
    percent = ({1: 100, 2: 100, 3: 90, 4: 85, 5: 80, 6: 75}[months]
               if graduated_enabled() else 80 if months == 6 else 90 if months >= 3 else 100)
    original = unit * months
    # All arithmetic is integer cents; half a cent rounds upwards.
    total = (original * percent + 50) // 100
    return {"purchase_version": SNAPSHOT_VERSION, "purchase_months": months,
            "unit_price_cents": unit, "original_amount_cents": original,
            "discount_percent": percent, "amount_cents": total,
            "currency": str(plan.get("currency") or "CNY"), "cycle_days": 30,
            "monthly_budget_micros": unit * 9000 if str(plan["code"]) in
            {"support_basic", "support_plus", "support_pro", "support_max"}
            else max(0, int(plan.get("ai_budget_micros") or 0))}


def snapshot(order) -> dict:
    try:
        value = json.loads(str(order["entitlement_snapshot_json"] or "{}"))
    except (KeyError, IndexError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def is_monthly_order(order) -> bool:
    return snapshot(order).get("purchase_version") == SNAPSHOT_VERSION


def discount_label(order) -> str:
    """Receipts describe the frozen order, independently of the current offer."""
    percent = snapshot(order).get("discount_percent", 100)
    if not isinstance(percent, (int, float)) or isinstance(percent, bool) or not 0 < percent < 100:
        return ""
    return f"{percent / 10:g} 折"


def subscriptions_for_order(conn, order) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT s.*, p.name AS plan_name FROM subscriptions s JOIN plans p ON p.code=s.plan_code "
        "WHERE s.paid_order_no=? AND s.user_id=? ORDER BY s.starts_at,s.id",
        (order["order_no"], order["user_id"])).fetchall()]


def settle_monthly_order(conn, order, *, paid_at, provider, payment_reference, notes, source):
    import membership as m
    info = snapshot(order)
    months = validate_months(info["purchase_months"])
    if months != int(order["purchase_months"]) or int(info["amount_cents"]) != int(order["amount_cents"]):
        raise ValueError("订单金额或购买月数不一致，请联系管理员。")
    paid_dt = m._parse_utc(paid_at)
    rows = conn.execute(
        "SELECT s.*,p.tier_rank FROM subscriptions s JOIN plans p ON p.code=s.plan_code "
        "WHERE s.user_id=? AND s.status='active' AND p.parallel_group='new_membership' "
        "AND s.expires_at>?", (order["user_id"], paid_at)).fetchall()
    # An order can remain pending while another purchase changes the active tier.
    current = [r for r in rows if r["starts_at"] <= paid_at]
    if any(int(r["tier_rank"]) != int(order["tier_rank"]) for r in current):
        raise ValueError("当前会员档位已变化，请联系管理员核对该订单；升档须先补差。")
    starts = max([paid_dt, *(m._parse_utc(r["expires_at"]) for r in rows)])
    conn.execute(
        "UPDATE orders SET status='paid',payment_provider=?,payment_reference=?,notes=?,paid_at=? "
        "WHERE order_no=? AND status='pending'",
        (provider, payment_reference, notes, paid_at, order["order_no"]))
    for _ in range(months):
        expires = starts + timedelta(days=30)
        cur = conn.execute(
            "INSERT INTO subscriptions(user_id,plan_code,status,source,starts_at,expires_at,notes,"
            "created_at,updated_at,paid_order_no) VALUES(?,?,'active',?,?,?,?,?,?,?)",
            (order["user_id"],order["plan_code"],source,starts.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds"),notes,paid_at,paid_at,order["order_no"]))
        if int(info["monthly_budget_micros"]) > 0:
            m._insert_ai_wallet(conn,user_id=int(order["user_id"]),source_type="subscription",
                source_ref=str(cur.lastrowid),plan=order,starts_at=starts,expires_at=expires,
                paid_at=paid_at,budget_micros=int(info["monthly_budget_micros"]))
        starts = expires
    subscriptions = subscriptions_for_order(conn,order)
    updated = dict(conn.execute("SELECT o.*,p.name AS plan_name FROM orders o JOIN plans p ON p.code=o.plan_code "
                               "WHERE o.order_no=?",(order["order_no"],)).fetchone())
    conn.commit()
    m._invalidate_request_membership_cache()
    user = m.get_user_by_id(int(order["user_id"])) or {}
    for subscription in subscriptions:
        m._append_member_export({"ts":paid_at,"event":"membership_paid","order_no":order["order_no"],
            "source":source,"user":user,"subscription":subscription,"order":updated})
    return {"order":updated,"subscription":subscriptions[-1],"subscriptions":subscriptions}


def monthly_budget_for_subscription(conn, subscription):
    """None means a historical order: retain the established reconciliation path."""
    order_no = str(subscription["paid_order_no"] or "")
    if not order_no:
        return None
    order = conn.execute("SELECT * FROM orders WHERE order_no=? AND user_id=? AND status='paid'",
                         (order_no,subscription["user_id"])).fetchone()
    if order is None or not is_monthly_order(order):
        return None
    base = int(snapshot(order)["monthly_budget_micros"])
    upgrades = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM orders WHERE user_id=? "
        "AND target_subscription_id=? AND status='paid' AND purchase_action='upgrade'",
        (subscription["user_id"],subscription["id"])).fetchone()[0]
    return base + int(upgrades) * 9000


def purchase_options(user_id, plans):
    """Read-only page quotes, sharing the same arithmetic as checkout."""
    import membership as m
    current_rank = 0
    if user_id:
        now = m.utc_now_text()
        with m._connect() as conn:
            current_rank = conn.execute("SELECT COALESCE(MAX(p.tier_rank),0) FROM subscriptions s "
                "JOIN plans p ON p.code=s.plan_code WHERE s.user_id=? AND s.status='active' "
                "AND s.starts_at<=? AND s.expires_at>? AND p.parallel_group='new_membership'",
                (user_id,now,now)).fetchone()[0]
    result = {}
    for plan in plans:
        if not eligible(plan):
            continue
        rank = int(plan.get("tier_rank") or 0)
        result[plan["code"]] = {"upgrade": bool(current_rank and rank > current_rank),
            "downgrade": bool(current_rank and rank < current_rank),
            "offer": discount_offer(),
            "quotes": [quote(plan,months) for months in range(1,7)]}
    return result
