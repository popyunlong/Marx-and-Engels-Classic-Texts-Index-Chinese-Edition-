"""New July grandfathering scenarios; real isolated entitlement/billing stores."""
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from _test_env import APPDATA
import app as A
import membership as M
from legacy_membership_fixture import create_manual_subscription
import comprehensive_web as W
import legacy_ai as L
from flask import g

NOW = "2026-11-12T04:00:00+00:00"
PAID = "2026-07-03T15:59:59+00:00"
FLASH = dict(provider="deepseek", model="deepseek-v4-flash", reasoning_effort="off")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "DB_PATH", tmp_path / "membership.sqlite3")
    monkeypatch.setattr(M, "utc_now", lambda: datetime.fromisoformat(NOW))
    M.init_membership_db()
    with M._connect() as c:
        c.execute("""INSERT INTO plans(code,name,price_cents,currency,interval_months,model_policy,
            parallel_group,weekly_token_limit,daily_deepseek_web_limit,tier_rank)
            SELECT 'quarter','旧季卡',2400,'CNY',3,model_policy,'legacy_membership',490000,20,20
            FROM plans WHERE code='monthly'""")
        c.commit()


def member(plan="yearly", paid=PAID, linked=True, status="paid", user=None,
           starts="2026-07-03T15:59:59+00:00", expires="2027-05-01T00:00:00+00:00"):
    user = user or M.create_user(email=secrets.token_hex(8)+"@promise.test", display_name="旧会员", password_hash="unused",
                               email_verified_at=PAID)
    order = M.create_pending_order(user_id=user["id"], plan_code=plan, allow_out_of_sale=True)
    with M._connect() as c:
        c.execute("UPDATE orders SET status=?,paid_at=?,payment_provider='zpay',payment_reference='fixture-paid' WHERE order_no=?",
                  (status, paid, order["order_no"]))
        sid = c.execute("""INSERT INTO subscriptions(user_id,plan_code,status,source,starts_at,expires_at,
            created_at,updated_at,paid_order_no) VALUES(?,?,'active','zpay_notify',?,?,?,?,?)""",
            (user["id"],plan,starts,expires,paid,paid,order["order_no"] if linked else "")).lastrowid
        c.commit()
    return user, sid, order["order_no"]


def reserve(user, *, tokens=100, completion=100, tool=None, now=NOW, request_id=None, selection=None):
    return M.reserve_ai_budget(user_id=user["id"], **(selection or FLASH), feature="comprehensive-chat",
        estimated_prompt_tokens=tokens, max_completion_tokens=completion, tool=tool,
        occurred_at=now, request_id=request_id)


def settle(user, r, prompt=100, completion=20, success=True):
    return M.record_ai_provider_call(request_id=r["request_id"], reservation_id=r["id"], user_id=user["id"],
        feature="comprehensive-chat", **FLASH, prompt_tokens=prompt, completion_tokens=completion,
        cached_prompt_tokens=0, reasoning_tokens=0, success=success, occurred_at=NOW)


def exhaust_wallet(user):
    M.get_ai_entitlements(user["id"])
    with M._connect() as c:
        c.execute("UPDATE ai_wallets SET spent_micros=released_micros WHERE user_id=?", (user["id"],))
        c.commit()


@pytest.mark.parametrize("paid,allowed", [(PAID,True),("2026-07-03T16:00:00+00:00",False),
    ("2026-07-04T00:00:00+08:00",False),("2026-08-14T00:00:00+00:00",False)])
def test_july_payment_boundary_not_august(paid, allowed):
    u,_,_=member(paid=paid)
    assert M.get_legacy_ai_quota(u["id"])["eligible"] is allowed
    assert M.MEMBERSHIP_REFORM_CUTOFF == "2026-08-14T16:00:00+00:00"


@pytest.mark.parametrize("plan,limit", [("monthly",280000),("quarter",490000),("yearly",770000)])
def test_actual_token_promise_after_money_exhaustion(plan,limit):
    u,_,_=member(plan)
    exhaust_wallet(u)
    r=reserve(u)
    assert r["funding_source"] == "legacy_promise"
    settle(u,r)
    settle(u,r)
    q=M.get_legacy_ai_quota(u["id"])
    assert (q["used"],q["reserved"],q["remaining"]) == (120,0,limit-120)
    rights=M.get_ai_entitlements(u["id"])
    assert rights["wallets"] == [] and rights["remaining_micros"] == 0
    assert rights["models"] == {"deepseek-v4-flash":["off"]}


@pytest.mark.parametrize("status", ["pending","refunded"])
def test_unpaid_or_refunded_order_does_not_protect(status):
    u,_,_=member(status=status)
    assert not M.get_legacy_ai_quota(u["id"])["eligible"]


def test_exact_historical_link_and_ambiguous_match():
    u,sid,order=member(linked=False)
    assert M.get_legacy_ai_quota(u["id"])["eligible"]
    M.init_membership_db()
    with M._connect() as c:
        assert c.execute("SELECT order_no FROM legacy_ai_order_links WHERE subscription_id=?",(sid,)).fetchone()[0] == order
    v,sid2,_=member(linked=False)
    member(user=v,linked=False)
    assert not M.get_legacy_ai_quota(v["id"])["eligible"]
    assert sid2 in M.get_legacy_ai_quota(v["id"])["pending_review_ids"]


def test_gift_chain_and_later_purchase_stays_separate():
    u,sid,_=member(expires="2026-10-01T00:00:00+00:00")
    with M._connect() as c:
        c.execute("""INSERT INTO subscriptions(user_id,plan_code,status,source,starts_at,expires_at,created_at,updated_at)
            VALUES(?,'yearly','active','manual-bulk','2026-10-01T00:00:00+00:00','2026-12-01T00:00:00+00:00',?,?)""",(u["id"],NOW,NOW))
        c.commit()
    assert M.get_legacy_ai_quota(u["id"])["eligible"]
    member(user=u, paid="2026-07-05T00:00:00+00:00",starts="2026-12-01T00:00:00+00:00")
    assert not M.get_legacy_ai_quota(u["id"],at_text="2026-12-02T00:00:00+00:00")["eligible"]


def test_highest_active_legacy_limit_not_sum_and_expiration():
    u,_,_=member("monthly")
    member(user=u,plan="yearly",expires="2026-11-13T00:00:00+00:00")
    assert M.get_legacy_ai_quota(u["id"])["limit"] == 770000
    assert M.get_legacy_ai_quota(u["id"],at_text="2026-11-13T00:00:00+00:00")["limit"] == 280000


def test_concurrent_actual_tokens_and_repeat_reservation():
    u,_,_=member("monthly")
    def attempt(_):
        try: return reserve(u,tokens=0,completion=200000)
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(attempt,range(2)))
    won=[r for r in outcomes if r]
    assert len(won)==1
    assert reserve(u,tokens=0,completion=200000,request_id=won[0]["request_id"])["id"]==won[0]["id"]
    M.release_ai_reservation(won[0]["id"])
    assert M.get_legacy_ai_quota(u["id"])["remaining"]==280000


def test_failed_model_consumption_and_next_week():
    u,_,_=member()
    r=reserve(u)
    settle(u,r,prompt=110,completion=40,success=False)
    assert M.get_legacy_ai_quota(u["id"])["used"]==150
    assert M.get_legacy_ai_quota(u["id"],at_text="2026-11-15T16:00:00+00:00")["used"]==0
    r=reserve(u)
    settle(u,r,prompt=0,completion=0,success=False)
    assert M.get_legacy_ai_quota(u["id"])["reserved"]==0


def test_gift_web_independent_of_exhausted_week_and_wallet():
    u,_,_=member("monthly")
    r=reserve(u,tokens=0,completion=280000)
    settle(u,r,prompt=0,completion=280000)
    for n in range(20):
        r=reserve(u,tool="search")
        assert r["funding_source"]=="legacy_promise"
        args=dict(request_id=r["request_id"],user_id=u["id"],tool="search",reservation_id=r["id"],billable=True,occurred_at=NOW)
        M.record_ai_tool_call(**args)
        M.record_ai_tool_call(**args)
    q=M.get_deepseek_web_quota(u["id"])
    assert (q["used"],q["remaining"],q["gift_remaining"])==(20,0,0)
    assert M.get_legacy_ai_quota(u["id"])["used"]==280000
    with pytest.raises(ValueError): reserve(u,tool="fetch")
    assert M.get_deepseek_web_quota(u["id"],at_text="2026-11-12T16:00:00+00:00")["gift_remaining"]==20


def test_web_last_gift_concurrent_failure_release():
    u,_,_=member()
    reservations=[reserve(u,tool="fetch") for _ in range(19)]
    def attempt(_):
        try: return reserve(u,tool="fetch")
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        won=[r for r in pool.map(attempt,range(2)) if r]
    assert len(won)==1
    r=won[0]
    M.record_ai_tool_call(request_id=r["request_id"],user_id=u["id"],tool="fetch",reservation_id=r["id"],billable=False,occurred_at=NOW)
    assert M.get_deepseek_web_quota(u["id"])["gift_remaining"]==1


def test_mixed_new_plan_fallback_and_pro_authorization():
    u,_,_=member("monthly")
    with pytest.raises(ValueError):
        M.authorize_ai_selection(user_id=u["id"],feature="research",provider="deepseek",model="deepseek-v4-pro",reasoning_effort="high")
    create_manual_subscription(user_email=u["email"],plan_code="support_plus")
    r=reserve(u,tokens=0,completion=280000)
    settle(u,r,prompt=0,completion=280000)
    assert M.get_legacy_ai_quota(u["id"])["remaining"]==0
    r=reserve(u)
    assert "funding_source" not in r
    selection=M.authorize_ai_selection(user_id=u["id"],feature="research",provider="deepseek",model="deepseek-v4-pro",reasoning_effort="high")
    assert selection["model"]=="deepseek-v4-pro"
    assert M.get_ai_entitlements(u["id"])["defaults"]["research"]["model"]=="deepseek-v4-flash"
    assert M.get_deepseek_web_quota(u["id"])["limit"]==40


def test_new_wallet_pays_after_twenty_gifts_without_weekly_charge():
    u,_,_=member()
    create_manual_subscription(user_email=u["email"],plan_code="support_plus")
    for _ in range(20):
        r=reserve(u,tool="search")
        M.record_ai_tool_call(request_id=r["request_id"],user_id=u["id"],tool="search",reservation_id=r["id"],billable=True,occurred_at=NOW)
    r=reserve(u,tool="fetch")
    assert "funding_source" not in r
    M.record_ai_tool_call(request_id=r["request_id"],user_id=u["id"],tool="fetch",reservation_id=r["id"],billable=True,occurred_at=NOW)
    assert M.get_ai_entitlements(u["id"])["spent_micros"]==5000
    assert M.get_legacy_ai_quota(u["id"])["used"]==0
    metrics=M.get_ai_cost_metrics()
    assert metrics["tool_cost_micros"]==105000
    assert metrics["total_cost_micros"]==105000


def test_comprehensive_preflight_and_ui_ignore_empty_historical_wallet():
    u,_,_=member()
    exhaust_wallet(u)
    assert M.affordable_completion_tokens(user_id=u["id"],selection=FLASH,messages=[{"role":"user","content":"问题"}],requested=32768)==32768
    with A.app.test_request_context('/api/ai/comprehensive-chat'):
        g.current_user=u
        payload=A._ai_entitlements_payload(u)
        assert payload["has_remaining"] and not payload["wallets"]
        assert A._ai_token_quota_payload(u)["unit"]=="actual_tokens"
        assert A._require_ai_quota_or_raise()["source"]=="legacy_promise"
        assert W.web_allowed(A) and W.web_quota(A)["allowed"]
    with pytest.raises(ValueError,match="本周"):
        M.affordable_completion_tokens(user_id=u["id"],selection=FLASH,
            messages=[{"role":"user","content":"长"*1300000}],requested=32768)


def test_cutover_inherits_only_linked_raw_model_usage():
    u,sid,order=member(paid="2026-07-04T00:00:00+00:00")
    r=reserve(u)
    settle(u,r,prompt=1000,completion=200)
    with M._connect() as c:
        c.execute("UPDATE orders SET paid_at=? WHERE order_no=?",(PAID,order))
        c.commit()
    q=M.get_legacy_ai_quota(u["id"])
    assert q["eligible"] and q["used"]==1200
    # A pre-cutover reservation continues settling to its original wallet.
    with M._connect() as c:
        assert c.execute("SELECT settled_micros FROM ai_reservation_allocations WHERE reservation_id=?",(r["id"],)).fetchone()[0]>0


def test_old_inflight_reservation_settles_after_qualification_change():
    u,_,order=member(paid="2026-07-04T00:00:00+00:00")
    old=reserve(u,tokens=500,completion=500)
    with M._connect() as c:
        c.execute("UPDATE orders SET paid_at=? WHERE order_no=?",(PAID,order))
        c.commit()
    assert M.get_legacy_ai_quota(u["id"])["reserved"]>0
    new=reserve(u)
    settle(u,old,prompt=300,completion=200)
    settle(u,new,prompt=100,completion=50)
    assert M.get_legacy_ai_quota(u["id"])["used"]==650
    with M._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM ai_reservation_allocations WHERE reservation_id=?",(old["id"],)).fetchone()[0]>0
        assert c.execute("SELECT COUNT(*) FROM ai_reservation_allocations WHERE reservation_id=?",(new["id"],)).fetchone()[0]==0


def test_late_settlement_after_membership_expiry_keeps_site_cost():
    u,sid,_=member()
    r=reserve(u)
    with M._connect() as c:
        c.execute("UPDATE subscriptions SET expires_at=? WHERE id=?",(NOW,sid))
        c.commit()
    assert not M.get_legacy_ai_quota(u["id"])["eligible"]
    record=settle(u,r)
    with M._connect() as c:
        row=c.execute("SELECT status,settled_micros FROM ai_budget_reservations WHERE id=?",(r["id"],)).fetchone()
        assert row["status"]=='settled' and row["settled_micros"]==record["cost_micros"]
        assert c.execute("SELECT COUNT(*) FROM ai_reservation_allocations WHERE reservation_id=?",(r["id"],)).fetchone()[0]==0
