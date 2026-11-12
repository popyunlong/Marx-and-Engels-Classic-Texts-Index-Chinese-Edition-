"""Member web billing and generation failure regressions, isolated stores and no paid calls."""
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from _test_env import APPDATA
import app as A
import membership as M
import ai
import ai_comprehensive as task
import comprehensive_web as W
from flask import g

FLASH = dict(provider="deepseek", model="deepseek-v4-flash", reasoning_effort="off")


def member(plan="support_basic"):
    user = M.create_user(email=secrets.token_hex(8)+"@member-web.test", display_name="fixture", password_hash="unused")
    if plan in {"monthly", "yearly"}:
        # Existing legacy subscriptions remain valid after sales have closed.
        now = M.utc_now_text()
        expiry = (M.utc_now()+timedelta(days=30)).isoformat(timespec="seconds")
        with M._connect() as conn:
            conn.execute("INSERT INTO subscriptions(user_id,plan_code,status,starts_at,expires_at,created_at,updated_at) VALUES(?,?,'active',?,?,?,?)",
                         (user["id"],plan,now,expiry,now,now))
            conn.commit()
    elif plan:
        M.create_manual_subscription(user_email=user["email"], plan_code=plan)
    return user


def reserve(user, tool="search"):
    return M.reserve_ai_budget(user_id=user["id"], **FLASH, feature="comprehensive-chat",
        estimated_prompt_tokens=0, max_completion_tokens=0, tool=tool)


@pytest.mark.parametrize("plan,limit", [(None,0),("support_basic",20),("support_plus",40),
    ("support_pro",80),("support_max",160),("monthly",20),("yearly",20),("research_pack",0)])
def test_membership_daily_quota(plan, limit):
    user = member(plan)
    assert M.get_deepseek_web_quota(user["id"])["limit"] == limit


def test_concurrent_last_slot_and_failed_release():
    user = member()
    with M._connect() as conn:
        conn.execute("UPDATE plans SET daily_deepseek_web_limit=1 WHERE code='support_basic'")
        conn.commit()
    def attempt(_):
        try:
            return reserve(user)
        except ValueError:
            return None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(attempt, range(2)))
        won = [r for r in attempts if r]
        assert len(won) == 1
        assert M.get_deepseek_web_quota(user["id"])["reserved"] == 1
        r = won[0]
        kwargs = dict(request_id=r["request_id"], user_id=user["id"], tool="search", reservation_id=r["id"],
                      billable=False, occurred_at=M.utc_now_text())
        M.record_ai_tool_call(**kwargs)
        assert M.get_deepseek_web_quota(user["id"])["remaining"] == 1
        r = reserve(user, "fetch")
        kwargs.update(request_id=r["request_id"], reservation_id=r["id"], tool="fetch", billable=True)
        M.record_ai_tool_call(**kwargs)
        M.record_ai_tool_call(**kwargs)
        q = M.get_deepseek_web_quota(user["id"])
        assert (q["used"], q["reserved"], q["remaining"]) == (1,0,0)
        tomorrow = (M.utc_now()+timedelta(days=1)).isoformat(timespec="seconds")
        assert M.get_deepseek_web_quota(user["id"], at_text=tomorrow)["remaining"] == 1
    finally:
        with M._connect() as conn:
            conn.execute("UPDATE plans SET daily_deepseek_web_limit=20 WHERE code='support_basic'")
            conn.commit()


def test_expiration_and_multiple_plans_choose_max():
    user = member()
    M.create_manual_subscription(user_email=user["email"], plan_code="support_max")
    assert M.get_deepseek_web_quota(user["id"])["limit"] == 160
    with M._connect() as conn:
        conn.execute("UPDATE subscriptions SET expires_at='2020-01-01T00:00:00+00:00' WHERE user_id=?",(user["id"],))
        conn.commit()
    assert not M.get_deepseek_web_quota(user["id"])["allowed"]


def test_plan_update_and_restart_preserve_operator_value():
    p = M.get_plan("support_plus")
    M.upsert_plan(code=p["code"],name=p["name"],price_cents=p["price_cents"],interval_months=p["interval_months"],daily_deepseek_web_limit=37)
    M.init_membership_db()
    assert M.get_plan("support_plus")["daily_deepseek_web_limit"] == 37
    M.upsert_plan(code=p["code"],name=p["name"],price_cents=p["price_cents"],interval_months=p["interval_months"],daily_deepseek_web_limit=40)


def test_compact_evidence_retains_ids_without_duplicate_text():
    raw = [{"index":7,"text":"正文", "quote_segments":["正文"], "source_segments":[{"page":1}]}]
    result = task.model_evidence(raw)
    assert result[0]["index"] == 7 and result[0]["quote_segments"] == ["正文"]
    assert "source_segments" not in result[0] and "text" not in result[0]
    assert raw[0]["source_segments"]


@pytest.mark.parametrize("error,phase,reason,recoverable", [
    (ValueError("AI 使用额度余额不足"),"writing","wallet_quota",False),
    (ValueError("本周 AI 额度不足"),"writing","weekly_quota",False),
    (ai.AIServiceError("模型连接中断"),"writing","connection_error",True),
    (ValueError("核验失败"),"verifying","verification_error",True),
    (ai.AIServiceError("达到时间预算"),"writing","deadline",True),
    (json.JSONDecodeError("invalid","",0),"checking","structured_output",True)])
def test_failure_reasons_preserve_recovery_policy(error,phase,reason,recoverable):
    result=task.failure_details(error,phase)
    assert result["stop_reason"]==reason and result["recoverable"]==recoverable


def test_member_route_real_authorization_budget_and_recording():
    user=member()
    plan={"complex":False,"needs_corpus":False,"needs_web":False,"requirements":[],"sections":[]}
    def complete(messages, **kwargs):
        return json.dumps(plan)
    def stream(messages, **kwargs):
        kwargs["meta_out"]["finish_reason"]="stop"
        yield "会员回答完成。"
    W._jobs.clear()
    with A.app.test_request_context("/api/ai/comprehensive-chat", method="POST",json={
        "question":"简要回答","request_id":secrets.token_hex(12),"web_mode":"off"}):
        g.current_user=user
        with patch.object(A,"_require_content_feature"), patch.object(A,"_require_ai"), \
             patch.object(A,"_rate_limit_ai_or_abort"), patch.object(A,"_public_book_keys",return_value=set()), \
             patch.object(A.AI_CLIENT,"chat_complete",side_effect=complete), \
             patch.object(A.AI_CLIENT,"chat_complete_stream",side_effect=stream):
            response=A.app.view_functions["api_comprehensive_chat"]()
            events=[json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]
    assert events[-1]["completion_status"] == "complete",events[-1]
    assert events[-1]["deepseek_web_quota"]["limit"] == 20
