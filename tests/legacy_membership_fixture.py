"""Reconstruct pre-cutover orders without reopening retired production products."""
import json
import secrets
from datetime import timedelta
import membership as M
import membership_purchase


def create_manual_subscription(*, user_email, plan_code, note=""):
    if plan_code not in {"support_basic", "support_plus"}:
        return M.create_manual_subscription(user_email=user_email, plan_code=plan_code, note=note)
    user=M.get_user_by_email(user_email)
    if user is None:
        raise ValueError("用户不存在")
    plan=M.get_plan(plan_code)
    now=M.utc_now_text()
    order_no="historical-fixture-"+secrets.token_hex(12)
    snapshot=membership_purchase.quote(plan,1)
    with M._connect() as c:
        c.execute("INSERT INTO orders(order_no,user_id,plan_code,status,amount_cents,currency,created_at,expires_at,entitlement_snapshot_json) "
                  "VALUES(?,?,?,'pending',?,'CNY',?,?,?)", (order_no,user['id'],plan_code,plan['price_cents'],now,
                  (M.utc_now()+timedelta(hours=1)).isoformat(),json.dumps(snapshot)))
        c.commit()
    result=M.mark_order_paid(order_no=order_no,provider='manual',source='manual',notes=note)
    return {'user':user,'order':result['order'],'subscription':result['subscription']}
