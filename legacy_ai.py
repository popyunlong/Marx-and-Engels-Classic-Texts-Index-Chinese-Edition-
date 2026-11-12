"""Grandfathered July promises, independent of monetary wallets.

All helpers use the caller's connection so quota checks and reservations share
the same SQLite write transaction. No credentials or conversation text live here.
"""
from datetime import datetime, timedelta, timezone

CUTOFF = datetime(2026, 7, 3, 16, tzinfo=timezone.utc)
LIMITS = {"monthly": 280_000, "quarter": 490_000, "quarterly": 490_000, "yearly": 770_000}
BJ = timezone(timedelta(hours=8))


def historical_order_links(conn, user_id=None):
    """Pre-link-column payment callbacks used exactly paid_at as created_at.

    Require an exact timestamp, provider source, user and plan, unique on both
    sides. This deliberately does not use nearest-order/registration heuristics.
    """
    clause, args = ("AND s.user_id=?", (user_id,)) if user_id is not None else ("", ())
    rows = conn.execute(f"""SELECT s.id,s.user_id,o.order_no FROM subscriptions s
        JOIN orders o ON o.user_id=s.user_id AND o.paid_at=s.created_at
        AND (o.plan_code=s.plan_code OR (o.plan_code IN ('quarter','quarterly') AND s.plan_code IN ('quarter','quarterly')))
        WHERE COALESCE(s.paid_order_no,'')='' AND s.source='zpay_notify'
        AND o.payment_provider='zpay' AND o.status='paid' AND o.amount_cents>0 {clause}""", args).fetchall()
    return {r["id"]: r["order_no"] for r in rows
            if sum(x["id"] == r["id"] for x in rows) == 1
            and sum(x["order_no"] == r["order_no"] for x in rows) == 1}


def instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def subscriptions(conn, user_id, now):
    """Only exact order links or explicit, same-tier gift ancestry confer rights."""
    rows = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND plan_code IN ('monthly','quarter','quarterly','yearly')",
        (user_id,))}
    orders = {r["order_no"]: dict(r) for r in conn.execute(
        "SELECT * FROM orders WHERE user_id=?", (user_id,))}
    reviewed = historical_order_links(conn, user_id)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_ai_order_links'").fetchone():
        reviewed.update({r["subscription_id"]: r["order_no"] for r in conn.execute(
            "SELECT * FROM legacy_ai_order_links WHERE user_id=?", (user_id,))})
    def tier(code):
        return "quarterly" if code == "quarter" else code
    def qualifies(row, seen):
        if row["id"] in seen or row["status"] != "active":
            return False
        seen = seen | {row["id"]}
        order_no = row.get("paid_order_no") or reviewed.get(row["id"])
        if order_no:
            order = orders.get(order_no)
            paid = instant(order["paid_at"]) if order else None
            return bool(order and order["status"] == "paid" and order["amount_cents"] > 0
                        and paid and paid < CUTOFF and tier(order["plan_code"]) == tier(row["plan_code"])
                        and order["payment_reference"] != "manual-grant")
        # Only documented gifts may inherit; a later purchase never inherits.
        if row["source"] != "manual-bulk":
            return False
        parent_id = row.get("parent_subscription_id")
        candidates = [r for r in rows.values() if r["id"] != row["id"]
                      and tier(r["plan_code"]) == tier(row["plan_code"])
                      and instant(r["expires_at"]) == instant(row["starts_at"])
                      and instant(r["created_at"]) and instant(row["created_at"])
                      and instant(r["created_at"]) <= instant(row["created_at"])
                      and (not parent_id or r["id"] == parent_id)]
        return len(candidates) == 1 and qualifies(candidates[0], seen)
    at = instant(now)
    active = [r for r in rows.values() if r["status"] == "active" and instant(r["starts_at"])
              and instant(r["expires_at"]) and instant(r["starts_at"]) <= at < instant(r["expires_at"])]
    eligible = [r for r in active if qualifies(r, set())]
    review = [r["id"] for r in active if r not in eligible and not r.get("paid_order_no") and r["id"] not in reviewed]
    return eligible, review


def quota(conn, user_id, now):
    eligible, review = subscriptions(conn, user_id, now)
    at = instant(now).astimezone(BJ)
    start = (at - timedelta(days=at.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    start_text, end_text = (d.astimezone(timezone.utc).isoformat(timespec="seconds") for d in (start, end))
    ids = [r["id"] for r in eligible]
    limit = max((LIMITS[r["plan_code"]] for r in eligible), default=0)
    used = reserved = 0
    if ids:
        placeholders = ",".join("?" for _ in ids)
        # Provider rows are counted once, never per wallet allocation. Pre-cutover
        # usage can be inherited only through its exact protected subscription.
        used = int(conn.execute(f"""
            SELECT COALESCE(SUM(c.prompt_tokens+c.completion_tokens),0) FROM ai_provider_calls c
            JOIN ai_budget_reservations r ON r.id=c.reservation_id
            WHERE c.user_id=? AND r.created_at>=? AND r.created_at<? AND (
              EXISTS(SELECT 1 FROM legacy_ai_reservations l WHERE l.reservation_id=r.id AND l.kind='model')
              OR EXISTS(SELECT 1 FROM ai_reservation_allocations a JOIN ai_wallets w ON w.id=a.wallet_id
                WHERE a.reservation_id=r.id AND w.source_type='legacy_subscription'
                AND CAST(w.source_ref AS INTEGER) IN ({placeholders})))
            """, (user_id, start_text, end_text, *ids)).fetchone()[0])
        reserved = int(conn.execute(f"""
            SELECT COALESCE(SUM(r.reserved_tokens),0) FROM ai_budget_reservations r
            WHERE r.user_id=? AND r.status='reserved' AND r.created_at>=? AND r.created_at<? AND r.web_tool=''
            AND (EXISTS(SELECT 1 FROM legacy_ai_reservations l WHERE l.reservation_id=r.id AND l.kind='model')
              OR EXISTS(SELECT 1 FROM ai_reservation_allocations a JOIN ai_wallets w ON w.id=a.wallet_id
                WHERE a.reservation_id=r.id AND w.source_type='legacy_subscription'
                AND CAST(w.source_ref AS INTEGER) IN ({placeholders})))
            """, (user_id, start_text, end_text, *ids)).fetchone()[0])
    return {"eligible": bool(ids), "cutoff": CUTOFF.isoformat(), "subscription_ids": ids,
            "pending_review_ids": review, "meter": "actual_tokens", "limit": limit,
            "used": used, "reserved": reserved, "remaining": max(0, limit-used-reserved),
            "week_start": start_text, "reset_at": end.isoformat(timespec="seconds"),
            "expires_at": max((r["expires_at"] for r in eligible), default=None),
            "web_gift_limit": 20 if ids else 0}


def paid_wallets(rows, protected):
    ids = set(protected["subscription_ids"])
    return [r for r in rows if not (r["source_type"] == "legacy_subscription"
            and str(r["source_ref"]).isdigit() and int(r["source_ref"]) in ids)]


def supports(model, effort):
    return model == "deepseek-v4-flash" and effort == "off"


def reserve(conn, *, protected, user_id, provider, model, effort, feature, request_id, now,
            tokens, tool, cost):
    expires = (instant(now) + timedelta(minutes=10)).isoformat(timespec="seconds")
    cur = conn.execute("""INSERT INTO ai_budget_reservations
        (request_id,user_id,provider,model,reasoning_effort,feature,reserved_micros,reserved_tokens,
         status,created_at,expires_at,web_tool) VALUES(?,?,?,?,?,?,?,?,'reserved',?,?,?)""",
        (request_id,user_id,provider,model,effort,feature,cost,tokens,now,expires,tool or ""))
    rid = int(cur.lastrowid)
    conn.execute("INSERT INTO legacy_ai_reservations VALUES(?,?,?,?)",
                 (rid, protected["subscription_ids"][0], "web" if tool else "model", protected["week_start"]))
    return {"id": rid, "request_id": request_id, "reserved_micros": cost,
            "reserved_tokens": tokens, "funding_source": "legacy_promise"}


def settle(conn, reservation_id, cost, now):
    if not conn.execute("SELECT 1 FROM legacy_ai_reservations WHERE reservation_id=?", (reservation_id,)).fetchone():
        return False
    conn.execute("UPDATE ai_budget_reservations SET settled_micros=?,status='settled',settled_at=? "
                 "WHERE id=? AND status='reserved'", (cost, now, reservation_id))
    return True


def report(conn, now):
    """Read-only aggregate; safe against the pre-migration production schema."""
    users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM subscriptions "
        "WHERE status='active' AND starts_at<=? AND expires_at>?", (now, now))]
    tiers, protected_users, pending = {}, [], []
    since = (instant(now)-timedelta(days=30)).isoformat(timespec="seconds")
    days_total = 0.0
    for uid in users:
        eligible, review = subscriptions(conn, uid, now)
        pending.extend(review)
        if not eligible:
            continue
        protected_users.append(uid)
        best = max(eligible, key=lambda r: LIMITS[r["plan_code"]])
        code = "quarterly" if best["plan_code"] == "quarter" else best["plan_code"]
        row = tiers.setdefault(code, {"users": 0, "weekly_tokens": LIMITS[code], "remaining_member_days": 0.0})
        days = max(0, (max(instant(r["expires_at"]) for r in eligible)-instant(now)).total_seconds()/86400)
        days_total += days
        row["users"] += 1
        row["remaining_member_days"] += days
    tokens = cost = tool_cost = 0
    if protected_users:
        marks = ','.join('?' for _ in protected_users)
        row = conn.execute(f"SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0),COALESCE(SUM(cost_micros),0) "
            f"FROM ai_provider_calls WHERE user_id IN ({marks}) AND occurred_at>=? AND feature!='mascot'",
            (*protected_users, since)).fetchone()
        tokens, cost = map(int, row)
        tool_cost = int(conn.execute(f"SELECT COALESCE(SUM(cost_micros),0) FROM ai_tool_calls "
            f"WHERE user_id IN ({marks}) AND occurred_at>=?", (*protected_users, since)).fetchone()[0])
    weekly = sum(r["users"]*r["weekly_tokens"] for r in tiers.values())
    return {"as_of": now, "cutoff": CUTOFF.isoformat(), "eligible_users": len(protected_users),
            "by_tier": tiers, "remaining_member_days": round(days_total, 2),
            "pending_review_subscription_ids": pending,
            "recent_30d_actual_tokens": tokens, "recent_30d_model_cost_yuan": cost/1e6,
            "recent_30d_tool_cost_yuan": tool_cost/1e6,
            "recent_cost_scope": "All non-mascot model and web use by this cohort, including separately paid rights",
            "full_use_30d_80_20_peak_yuan": round(weekly*30/7*3.2/1e6+len(protected_users)*3, 2),
            "full_use_30d_all_output_peak_yuan": round(weekly*30/7*8/1e6+len(protected_users)*3, 2),
            "projection_note": "Scenario at Flash input 2/output 8 yuan per million and web 0.005 per call; current cohort held constant for 30 days, not a forecast or total liability."}
