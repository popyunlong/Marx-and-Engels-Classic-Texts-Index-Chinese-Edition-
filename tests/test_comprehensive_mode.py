"""One-pass acceptance for adaptive conversations, tools and monetary reservations."""
from __future__ import annotations

import contextlib
import json
import secrets
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError

import pytest
from _test_env import APPDATA
import app as A
import ai
import ai_comprehensive as task
import ai_web
import comprehensive_web as routes
import membership as M
from flask import g

FLASH = {"provider": "deepseek", "model": "deepseek-v4-flash", "reasoning_effort": "off"}
PLAN = {"complex": False, "needs_corpus": False, "needs_web": False, "external_facts": False,
        "web_queries": [], "corpus_queries": [], "requirements": [], "sections": ["回答问题"]}


@pytest.mark.parametrize("models,complex_task,expected", [
    ({"deepseek-v4-flash": ["off"]}, True, ("deepseek-v4-flash", "off")),
    ({"deepseek-v4-flash": ["off", "high"]}, True, ("deepseek-v4-flash", "high")),
    ({"deepseek-v4-flash": ["off"], "deepseek-v4-pro": ["high"]}, True, ("deepseek-v4-pro", "high")),
    ({"deepseek-v4-flash": ["off"], "deepseek-v4-pro": ["high"]}, False, ("deepseek-v4-flash", "off")),
])
def test_model_router_intersects_entitlements(models, complex_task, expected):
    actual = task.choose_model(models, complex_task)
    assert (actual["model"], actual["reasoning_effort"]) == expected


def test_long_history_preserves_constraints_headings_and_tail():
    history = task.history_context([
        {"role": "user", "content": "请写文章，保留第三节"},
        {"role": "assistant", "content": "## 第一节\n" + "内容"*60000 + "\n## 第三节\n续写起点"},
    ], "继续", budget=80000)
    combined = str(history)
    assert "第三节" in combined and "续写起点" in combined and "保留第三节" in combined
    assert len(combined) < 81000


def test_full_context_article_keeps_earlier_requirements_and_reference_locators():
    history = task.history_context([
        {"role": "user", "content": "严格只分析指定篇目，不补背景"},
        {"role": "assistant", "content": "正文"*40000,
         "citation_refs": [{"grounding_index": 41, "source_file": "test.pdf", "pdf_pages": [12]}]},
    ], "展开第四十一条", budget=80000)
    text = str(history)
    assert "不补背景" in text and "test.pdf" in text and "41" in text
    assert len(text) < 81000


def test_short_answer_has_no_word_floor_and_completion_is_checked():
    calls, points = [], []
    replies = iter(["简短解释。<section_complete/>", '{"complete":true,"remaining":[]}'])
    def call(messages, limit):
        calls.append(messages)
        return next(replies)
    text, done, status, warning = task.generate("用一句话解释", [], dict(PLAN), [], call,
        deadline=time.monotonic()+10, cancelled=lambda: False,
        checkpoint=lambda *args: points.append(args), verify=lambda text: text)
    assert text == "简短解释。" and status == "complete" and len(calls) == 1
    assert points[-1][2] == "complete"


def test_requirement_check_requests_missing_part_without_rewriting():
    replies = iter(["已有分析。<section_complete/>", '{"complete":false,"remaining":["用户要求的比较"]}',
                    "补充比较。<section_complete/>", '{"complete":true,"remaining":[]}'])
    result = task.generate("分析并比较", [], {**PLAN, "complex": True, "sections": ["分析"]}, [], lambda *_: next(replies),
        deadline=time.monotonic()+10, cancelled=lambda: False, checkpoint=lambda *_: None, verify=lambda t: t)
    assert result[2] == "complete" and result[0].count("已有分析") == 1 and "补充比较" in result[0]


def test_cancel_preserves_completed_checkpoint():
    stopped, points = [False], []
    def checkpoint(*args):
        points.append(args); stopped[0] = True
    result = task.generate("长文", [], {**PLAN, "sections": ["第一部分", "第二部分"]}, [],
        lambda *_: "第一部分完成。<section_complete/>", deadline=time.monotonic()+10,
        cancelled=lambda: stopped[0], checkpoint=checkpoint, verify=lambda t: t)
    assert result[2] == "partial" and result[1] == 1 and points[0][0] == result[0]


@pytest.fixture
def wallet():
    user = M.create_user(email=secrets.token_hex(8)+"@comprehensive.test", display_name="测试", password_hash="unused")
    now = "2026-11-01T00:00:00+00:00"
    expiry = (M.utc_now()+timedelta(days=5)).isoformat(timespec="seconds")
    with M._connect() as conn:
        conn.execute("INSERT INTO ai_wallets(user_id,source_type,source_ref,plan_code,budget_micros,released_micros,starts_at,expires_at,model_policy,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], "test", secrets.token_hex(10), "support_basic", 1_000_000, 1_000_000,
             now, expiry, json.dumps({"models": {"deepseek-v4-flash": ["off"]}}), now, now))
        conn.commit()
    return user


def test_tool_reservation_success_empty_and_idempotent_settlement(wallet):
    stamp = "2026-11-05T00:00:00+00:00"
    with mock.patch.object(M, "utc_now_text", return_value=stamp):
        reservation = M.reserve_ai_budget(user_id=wallet["id"], **FLASH, feature="comprehensive-chat",
            estimated_prompt_tokens=0, max_completion_tokens=0, tool="search")
        args = dict(request_id=reservation["request_id"], user_id=wallet["id"], tool="search",
            reservation_id=reservation["id"], billable=True, occurred_at=stamp)
        first = M.record_ai_tool_call(**args)
        M.record_ai_tool_call(**args)
        empty = M.reserve_ai_budget(user_id=wallet["id"], **FLASH, feature="comprehensive-chat",
            estimated_prompt_tokens=0, max_completion_tokens=0, tool="fetch")
        M.record_ai_tool_call(request_id=empty["request_id"], user_id=wallet["id"], tool="fetch",
            reservation_id=empty["id"], billable=False, occurred_at=stamp)
    with M._connect() as conn:
        balance = conn.execute("SELECT spent_micros,reserved_micros FROM ai_wallets WHERE user_id=?", (wallet["id"],)).fetchone()
        assert tuple(balance) == (5000, 0)
        assert conn.execute("SELECT COUNT(*) FROM ai_provider_calls WHERE request_id=?", (reservation["request_id"],)).fetchone()[0] == 0
    assert first["cost_micros"] == 5000


def test_thinking_mode_allowed_only_with_purchased_right(wallet):
    rights = {"models": {"deepseek-v4-flash": ["off"], "deepseek-v4-pro": ["high"]}}
    with mock.patch.object(M, "get_ai_entitlements", return_value=rights):
        result = M.authorize_ai_selection(user_id=wallet["id"], feature="comprehensive-chat", provider="deepseek", model="deepseek-v4-pro", reasoning_effort="high")
        assert result["model"] == "deepseek-v4-pro"
        with pytest.raises(ValueError):
            M.authorize_ai_selection(user_id=wallet["id"], feature="search-chat", provider="deepseek", model="deepseek-v4-pro", reasoning_effort="high")
    with mock.patch.object(M, "get_ai_entitlements", return_value={"models": {"deepseek-v4-flash": ["off"]}}):
        with pytest.raises(ValueError):
            M.authorize_ai_selection(user_id=wallet["id"], feature="comprehensive-chat", provider="deepseek", model="deepseek-v4-pro", reasoning_effort="high")


def test_durable_request_claim_prevents_repeat_after_process_restart(wallet):
    rid = secrets.token_hex(12)
    assert M.claim_ai_conversation_request(wallet["id"], rid, "digest")
    assert not M.claim_ai_conversation_request(wallet["id"], rid, "digest")
    assert not M.claim_ai_conversation_request(wallet["id"], rid, "different-content")


class HTTPResponse:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, *_): return json.dumps(self.data).encode()


def web_evidence():
    client = SimpleNamespace(_route=lambda _: {"base_url": "https://api.deepseek.com", "api_key": "fixture"})
    return ai_web.WebEvidence(client, user_id=None, selection=FLASH, charge_user=False, deadline=time.monotonic()+20)


def test_search_fetch_cache_attribution_and_billing():
    web = web_evidence()
    result = {"results": [{"title": "背景材料", "url": "https://example.org/paper", "summary": "摘要"}]}
    with mock.patch.object(ai_web.request, "urlopen", side_effect=[HTTPResponse(result), HTTPResponse({"content": "文章正文"})]) as network, \
         mock.patch.object(M, "record_ai_tool_call") as ledger:
        web.search("检索词"); web.search("检索词")
        web.fetch(web.sources[0]); web.fetch(web.sources[0])
    assert network.call_count == 2 and ledger.call_count == 2
    assert all(c.kwargs["billable"] for c in ledger.call_args_list)
    assert "/v1/tools/search" in network.call_args_list[0].args[0].full_url
    assert "ori_question" not in json.loads(network.call_args_list[0].args[0].data)
    text, sources, _, status = web.finish("外部事实[W1]，错误编号[W999]")
    assert sources[0]["purpose"] == "citation" and sources[0]["verification"] == "page_read"
    assert "https://example.org/paper" in text and "未核验" in text and status == "used"
    assert web.finish("站内原文[1]")[1][0]["purpose"] == "background"


@pytest.mark.parametrize("code,attempts", [(401, 1), (402, 1), (403, 1), (429, 2), (504, 2)])
def test_web_failure_visible_and_not_billed(code, attempts):
    web = web_evidence()
    failure = HTTPError("https://api.deepseek.com", code, "fixture", {}, None)
    with mock.patch.object(ai_web.request, "urlopen", side_effect=failure) as network, \
         mock.patch.object(M, "record_ai_tool_call") as ledger, mock.patch.object(ai_web.time, "sleep"):
        web.search("关键词")
    assert network.call_count == attempts and web.warnings
    assert all(not call.kwargs["billable"] for call in ledger.call_args_list)
    assert web.finish("正文")[3] == "unavailable"


def test_web_budget_and_cancel_stop_new_requests():
    web = web_evidence(); web.counts["search"] = 8
    with mock.patch.object(ai_web.request, "urlopen") as network:
        web.search("超预算")
        web.counts["search"] = 0; web.cancelled = lambda: True
        web.search("取消后")
    network.assert_not_called()


@pytest.mark.parametrize("url", ["file:///secret", "https://u:p@example.org", "http://127.0.0.1", "http://localhost", "http://10.0.0.1", "javascript:alert(1)"])
def test_web_rejects_unsafe_urls(url):
    assert not ai_web.safe_url(url)


@contextlib.contextmanager
def route_fixture(plan=PLAN, passages=None, scope_meta=None):
    routes._jobs.clear()
    calls = []
    user = {"id": 900001, "email": "fixture@local", "role": "admin"}
    def complete(messages, **kwargs):
        calls.append(kwargs)
        if "为用户任务" in messages[0]["content"]:
            return json.dumps(plan, ensure_ascii=False)
        if "核对正文" in messages[0]["content"]:
            return '{"complete":true,"remaining":[]}'
        return "按需回答" + "".join(f"[{p['index']}]" for p in (passages or [])) + "。<section_complete/>"
    p = passages or []
    c = [{"grounding_index": row["index"], "citation": "测试出处", "context": row["text"]} for row in p]
    with contextlib.ExitStack() as stack:
        for name in ("_require_content_feature", "_rate_limit_ai_or_abort", "_require_ai", "_record_ai_usage", "_consume_credit_if_paid"):
            stack.enter_context(mock.patch.object(A, name))
        stack.enter_context(mock.patch.object(A, "_require_ai_quota_or_raise", return_value={"weekly_limit": None}))
        stack.enter_context(mock.patch.object(A, "_ai_token_quota_payload", return_value={"unlimited": True}))
        stack.enter_context(mock.patch.object(A, "_ai_entitlements_payload", return_value={"models": {"deepseek-v4-flash": ["off"], "deepseek-v4-pro": ["high"]}}))
        stack.enter_context(mock.patch.object(A, "_resolve_ai_selection_or_abort", side_effect=lambda selection, **_: selection))
        stack.enter_context(mock.patch.object(A, "_public_book_keys", return_value={"测试书"}))
        stack.enter_context(mock.patch.object(A.AI_CLIENT, "chat_complete", side_effect=complete))
        stack.enter_context(mock.patch.object(A.AI_CLIENT, "chat_complete_stream", side_effect=lambda messages, **kwargs: iter([A.AI_CLIENT.chat_complete(messages, **kwargs)])))
        stack.enter_context(mock.patch.object(A.AI_CLIENT, "repair_grounded_answer", side_effect=lambda text, _: {"answer_markdown": text, "issues": []}))
        stack.enter_context(mock.patch.object(A.ai_citations, "enabled", return_value=False))
        grounding = stack.enter_context(mock.patch.object(A, "_build_chat_grounding", return_value=(p, c, [], scope_meta or {})))
        stack.enter_context(mock.patch.object(M, "claim_ai_conversation_request", return_value=True))
        stack.enter_context(mock.patch.object(routes, "web_allowed", return_value=False))
        def post(payload):
            with A.app.test_request_context("/api/ai/comprehensive-chat", method="POST", json=payload):
                g.current_user = user
                response = A.app.view_functions["api_comprehensive_chat"]()
                raw = response.get_data(as_text=True)
                if response.mimetype == "application/json": return json.loads(raw)
                data = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
                return data[-1]
        yield post, calls, grounding


def test_route_short_answer_default_and_repeated_request():
    with route_fixture() as (post, calls, grounding):
        payload = {"question": "简短解释", "request_id": "short-answer-once"}
        first = post(payload); second = post(payload)
    assert first["ok"] and first["completion_status"] == "complete"
    assert first["answer_markdown"] == second["answer_markdown"] and len(calls) == 2
    grounding.assert_not_called()


def test_route_has_no_thirty_citation_cutoff_and_preserves_scope():
    passages = [{"index": i, "text": f"第{i}段原文"} for i in range(1, 46)]
    with route_fixture({**PLAN, "complex": True, "needs_corpus": True}, passages) as (post, calls, grounding):
        result = post({"question": "论证", "scope": ["book:文集:1"], "request_id": "forty-five-sources"})
    assert result["ok"] and len(result["citations"]) == 45 and "[45]" in result["answer_markdown"]
    assert grounding.call_args.kwargs["source_limit"] is None
    assert grounding.call_args.kwargs["raw_scope"] == ["book:文集:1"]
    assert calls[1]["model"] == "deepseek-v4-pro"


def test_route_explicit_scope_without_evidence_stops_generation():
    with route_fixture({**PLAN, "needs_corpus": True}, scope_meta={"manual": True}) as (post, calls, _):
        result = post({"question": "解释指定篇目", "scope": "book:private", "request_id": "missing-document"})
    assert not result["ok"] and len(calls) == 1 and "指定范围" in result["error"]


def test_route_continuation_reuses_plan_and_completed_sections():
    plan = {**PLAN, "complex": True, "sections": ["第一部分", "第二部分"]}
    with route_fixture(plan) as (post, calls, _):
        original = A.AI_CLIENT.chat_complete.side_effect
        failed = [False]
        def intermittent(messages, **kwargs):
            if "本次完成部分：第二部分" in messages[-1]["content"] and not failed[0]:
                failed[0] = True
                raise ai.AIServiceError("暂时不可用")
            return original(messages, **kwargs)
        A.AI_CLIENT.chat_complete.side_effect = intermittent
        first = post({"question": "完整文章", "request_id": "resume-first-request"})
        second = post({"question": "完整文章", "request_id": "resume-second-request", "continue_request_id": "resume-first-request"})
    assert first["completion_status"] == "partial" and second["completion_status"] == "complete"
    assert second["answer_markdown"].startswith(first["answer_markdown"])
    assert len(calls) == 4  # one plan, two successful sections and one completion check


def test_web_off_and_private_scope_never_make_network_calls():
    with A.app.test_request_context("/"), mock.patch.object(routes, "web_allowed", return_value=True), \
         mock.patch.object(ai_web.request, "urlopen") as network:
        g.current_user = {"id": 900001, "role": "admin"}
        assert routes.prepare_web(A, {"web_mode": "off"}, FLASH) is None
        web = routes.prepare_web(A, {"web_mode": "always", "scope": ["book:mylib:42"], "question": "私有正文"}, FLASH)
        assert web.warnings
    network.assert_not_called()
