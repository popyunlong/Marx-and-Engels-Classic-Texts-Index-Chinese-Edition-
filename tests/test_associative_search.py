from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import warnings
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

warnings.filterwarnings("ignore", category=ResourceWarning)
# 必须在导入 app 前“硬置”到临时 APPDATA：Windows 上 APPDATA 恒被设置，setdefault 会失效而误用真实库。
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-search-assoc-")
atexit.register(lambda: shutil.rmtree(_TMP_APPDATA, ignore_errors=True))
os.environ["APPDATA"] = _TMP_APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["ZPAY_PID"] = "test-pid"
os.environ["ZPAY_KEY"] = "test-secret"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
import ai as ai_module  # noqa: E402
import search as search_module  # noqa: E402
from membership import (  # noqa: E402
    consume_ai_credit,
    create_manual_subscription,
    create_pending_order,
    create_user,
    get_ai_credit_balance,
    get_ai_credit_balances,
    get_plan,
    grant_ai_credits,
    mark_order_paid,
    get_user_by_email,
    record_ai_usage,
    update_user_account,
    upsert_plan,
)
from werkzeug.security import generate_password_hash  # noqa: E402


REGISTERED_FULL = {"audience": {"registered": {"search": True, "library": True, "ai": True}}}


def _corpus_sample(min_len: int = 16) -> tuple[str, str]:
    """从真实语料中取一段确定存在的原文子串，用作可命中的“候选原文”。"""
    corpus = app_module.corpus
    for book, vols in corpus.books.items():
        for vol in vols:
            nf = vol.norm_full
            if len(nf) > 4000:
                return book, nf[2000:2000 + min_len]
    raise RuntimeError("corpus has no usable volume for sampling")


def _parse_sse_text(raw):
    """从 SSE 文本里取最后一个 data: 负载并解析为 JSON；忽略以 ':' 开头的心跳注释行。失败返回 None。"""
    result = None
    for block in str(raw or "").split("\n\n"):
        data_parts = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
        if not data_parts:
            continue
        try:
            result = json.loads("".join(data_parts))
        except (ValueError, TypeError):
            pass
    return result


def _read_result(resp):
    """读取检索响应：研究综述走 SSE 心跳保活流(text/event-stream)，取最后一个 done 事件的 JSON；
    其余仍是普通 JSON。"""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in ctype:
        return resp.get_json()
    return _parse_sse_text(resp.get_data(as_text=True))


class AssociativeUnitTests(unittest.TestCase):
    """search.py 接地定位方法的单元测试（对真实语料，断言结构性质而非具体页码）。"""

    def test_locate_quote_exact_real_substring(self) -> None:
        book, sample = _corpus_sample()
        hits = app_module.corpus.locate_quote(sample)
        self.assertTrue(hits, "真实子串应当被精确定位到")
        self.assertTrue(all(h.match_type in ("exact", "fuzzy") for h in hits))
        self.assertTrue(all(h.citation.startswith("《") for h in hits))

    def test_locate_quote_nonsense_returns_empty(self) -> None:
        hits = app_module.corpus.locate_quote("zZqx不存在的随机串9w8e7r6t5y")
        self.assertEqual(hits, [])

    def test_keyword_cooccurrence_window_and_score(self) -> None:
        # 从真实窗口里截三个互不相同的两字片段做关键词，保证“近邻共现”可命中。
        _book, sample = _corpus_sample(min_len=60)
        kws = [sample[0:2], sample[20:22], sample[40:42]]
        hits = app_module.corpus.keyword_cooccurrence(kws, window=200)
        self.assertTrue(hits)
        for h in hits:
            self.assertTrue(0 <= h.score <= 100)
            self.assertTrue(h.citation.startswith("《"))

    def test_keyword_cooccurrence_needs_two_keywords(self) -> None:
        self.assertEqual(app_module.corpus.keyword_cooccurrence(["只有一个词"]), [])

    def test_fragment_search_finds_real_fragment(self) -> None:
        _book, sample = _corpus_sample(min_len=12)  # 取自 norm_full，已归一化
        hits = app_module.corpus.fragment_search([sample])
        self.assertTrue(hits, "确实存在的独特片段应被精确检索到")
        for h, frag in hits:
            self.assertEqual(frag, sample)
            self.assertTrue(h.citation.startswith("《"))

    def test_fragment_path_recovers_paraphrase(self) -> None:
        # 整句是“改写”（不在语料），但提供一个真实逐字片段 → 仍能接地定位。
        _book, sample = _corpus_sample(min_len=12)
        res = app_module.corpus.locate_associative(
            quotes=["这是一句不可能逐字命中的随机改写zzqxy"],
            keywords=[],
            fragments=[sample],
        )
        self.assertTrue(res, "片段召回应能在整句对不上时定位到真实段落")
        self.assertTrue(res[0].citation.startswith("《"))

    def test_locate_associative_capped_and_sorted(self) -> None:
        _book, sample = _corpus_sample(min_len=60)
        kws = [sample[0:2], sample[20:22], sample[40:42]]
        res = app_module.corpus.locate_associative(quotes=[sample], keywords=kws)
        self.assertLessEqual(len(res), search_module.ASSOC_CANDIDATE_CAP)
        scores = [h.score for h in res]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(0 <= s <= 100 for s in scores))

    def test_chapter_keyword_boosts_matching_section(self) -> None:
        # 找一个 section_title 非空的真实命中，用其标题里的词做篇章关键词，断言该段权重被抬升。
        _book, sample = _corpus_sample(min_len=14)
        base = app_module.corpus.locate_associative(quotes=[sample], keywords=[], fragments=[sample])
        target = next((h for h in base if h.section_title and len(search_module.normalize(h.section_title)) >= 2), None)
        if target is None:
            self.skipTest("样本段落无可用篇章标题")
        base_score = {(h.book, h.volume, h.source_file, h.pages[0].pdf_page): h.score for h in base}
        ck = search_module.normalize(target.section_title)[:4]
        boosted = app_module.corpus.locate_associative(
            quotes=[sample], keywords=[], fragments=[sample], chapter_keywords=[ck]
        )
        key = (target.book, target.volume, target.source_file, target.pages[0].pdf_page)
        new_score = next((h.score for h in boosted
                          if (h.book, h.volume, h.source_file, h.pages[0].pdf_page) == key), None)
        self.assertIsNotNone(new_score)
        self.assertGreaterEqual(new_score, base_score[key])

    def test_chapter_focused_search_locates_within_named_work(self) -> None:
        # 取一个标题较长的真实篇章，用其标题里的词做“著作/篇章名”，用篇内一个真实词做主题词，
        # 断言篇章定向检索能在该卷该篇内定位到段落。
        corpus = app_module.corpus
        picked = None
        for vols in corpus.books.values():
            for vol in vols:
                for seg in corpus._chapter_segments(vol):
                    title_norm = search_module.normalize(seg["title"])
                    span = vol.norm_full[seg["norm_start"]:seg["norm_end"]]
                    if len(title_norm) >= 5 and len(span) >= 400:
                        picked = (vol, seg, title_norm, span)
                        break
                if picked:
                    break
            if picked:
                break
        if not picked:
            self.skipTest("未找到合适的篇章样本")
        vol, seg, title_norm, span = picked
        title_kw = title_norm[:5]
        content_kw = span[200:204]  # 篇内一个真实四字片段
        hits = corpus.chapter_focused_search([title_kw], [content_kw])
        self.assertTrue(hits, "篇章定向应能在命中标题的篇内定位")
        self.assertTrue(any(h.citation.startswith("《") for h, _ck in hits))

    def test_chapter_focused_empty_without_chapter_keywords(self) -> None:
        self.assertEqual(app_module.corpus.chapter_focused_search([], ["资产阶级", "生产力"]), [])


class JsonAndPlanParsingTests(unittest.TestCase):
    def test_expand_query_uses_zero_temperature(self) -> None:
        # 确定性：结构化线索抽取必须以 temperature=0 调用，避免同一输入“有时有有时无”。
        ai_module._ASSOC_EXPAND_CACHE.clear()
        with mock.patch.object(
            app_module.AI_CLIENT, "chat_complete", return_value='{"keywords":["x"]}'
        ) as cc:
            app_module.AI_CLIENT.expand_associative_query("温度测试输入")
        self.assertTrue(cc.called)
        self.assertEqual(cc.call_args.kwargs.get("temperature"), 0.0)

    def test_expand_query_caches_and_retries(self) -> None:
        # 缓存：同一输入只调一次模型、结果可复现；空响应会重试。
        ai_module._ASSOC_EXPAND_CACHE.clear()
        responses = ["", "{}", '{"keywords":["生产力"]}']  # 前两次空/无用 → 重试到第三次
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", side_effect=responses) as cc:
            first = app_module.AI_CLIENT.expand_associative_query("缓存重试输入")
        self.assertEqual(first.get("keywords"), ["生产力"])
        self.assertEqual(cc.call_count, 3)  # 重试了 3 次
        # 第二次同输入应命中缓存，不再调用模型
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete") as cc2:
            second = app_module.AI_CLIENT.expand_associative_query("缓存重试输入")
        self.assertEqual(second.get("keywords"), ["生产力"])
        cc2.assert_not_called()

    def test_research_review_continues_when_initial_answer_is_incomplete(self) -> None:
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        initial = "## 开篇\n" + ("这是一段尚未收束的研究综述。[1]\n" * 80)
        continuation = "## 小结\n综上，现有材料已经能够支撑这一论题的基本分析。[1]"
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", side_effect=[initial, continuation]) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        self.assertIn(initial.strip(), review)
        self.assertIn(continuation, review)
        self.assertEqual(cc.call_count, 2)
        self.assertEqual(cc.call_args_list[0].kwargs["max_tokens"], ai_module.RESEARCH_REVIEW_MAX_TOKENS)
        self.assertFalse(cc.call_args_list[0].kwargs["allow_reasoning_fallback"])
        self.assertEqual(
            cc.call_args_list[1].kwargs["max_tokens"],
            ai_module.RESEARCH_REVIEW_CONTINUATION_MAX_TOKENS,
        )
        self.assertFalse(cc.call_args_list[1].kwargs["allow_reasoning_fallback"])

    def test_research_review_does_not_continue_when_answer_is_complete(self) -> None:
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        complete = "## 开篇\n" + ("这是一段完整的研究综述。[1]\n" * 80) + "## 小结\n综上，文章完整收束。[1]"
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=complete) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        self.assertEqual(review, complete)
        self.assertEqual(cc.call_count, 1)
        self.assertFalse(cc.call_args.kwargs["allow_reasoning_fallback"])

    def test_research_review_retries_when_reasoning_leaks(self) -> None:
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        leaked = "思考过程：我需要先分析材料，然后再写正文。"
        repaired = "【综述正文开始】\n## 研究综述\n" + ("这是一段正式综述正文。[1]\n" * 80) + "## 小结\n综上，文章自然完成。[1]\n【综述正文结束】"
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", side_effect=[leaked, repaired]) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        self.assertNotIn("思考过程", review)
        self.assertNotIn("【综述正文开始】", review)
        self.assertIn("## 研究综述", review)
        self.assertIn("## 小结", review)
        self.assertEqual(cc.call_count, 2)

    def test_research_review_retries_lower_budget_when_high_budget_is_rejected(self) -> None:
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        complete = "## 研究综述\n" + ("这是一段完整的研究综述。[1]\n" * 80) + "## 小结\n综上，文章自然完成。[1]"
        with mock.patch.object(
            app_module.AI_CLIENT,
            "chat_complete",
            side_effect=[app_module.AIServiceError("max_tokens exceeds limit"), complete],
        ) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        self.assertEqual(review, complete)
        self.assertEqual(cc.call_count, 2)
        self.assertEqual(cc.call_args_list[0].kwargs["max_tokens"], ai_module.RESEARCH_REVIEW_MAX_TOKENS)
        self.assertEqual(cc.call_args_list[1].kwargs["max_tokens"], 12000)

    def test_research_review_passes_per_call_http_timeout(self) -> None:
        # 每次模型调用都按 min(研究专用超时, 剩余总预算) 压一个 HTTP 超时。生成跑在 SSE 心跳保活线程里、
        # 已与 CF ~100s 解耦，故该超时刻意宽于全局 120s（用 RESEARCH_REVIEW_CALL_TIMEOUT_SECONDS）。
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        complete = "## 研究综述\n" + ("这是一段完整的研究综述。[1]\n" * 60) + "## 小结\n综上，文章自然完成。[1]"
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=complete) as cc:
            app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        timeout = cc.call_args.kwargs.get("http_timeout")
        self.assertIsNotNone(timeout)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, ai_module.RESEARCH_REVIEW_CALL_TIMEOUT_SECONDS)

    def test_research_review_stops_extra_rounds_when_time_budget_exhausted(self) -> None:
        # 预算不足以再安全跑一轮（这里把所需余量调到极大模拟「预算将尽」）时，即便首轮综述
        # 未自然收尾，也不再追加续写/重写，而是带着已成文返回——保证非流式链路在 CF 超时前回 JSON。
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        incomplete = "## 研究综述\n" + ("这是一段尚未收尾的正文。[1]\n" * 50)
        with mock.patch.object(ai_module, "RESEARCH_REVIEW_FOLLOWUP_MIN_HEADROOM_SECONDS", 10_000), \
             mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=incomplete) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        self.assertEqual(cc.call_count, 1)  # 既不续写也不重写
        self.assertIn("这是一段尚未收尾的正文。", review)  # 已成文照常返回，未抛错

    def test_research_review_passage_uses_complete_sentence_window(self) -> None:
        raw = (
            ("前置背景说明，暂不涉及核心命中。" * 18)
            + "资本主义生产方式在这里表现为劳动条件同劳动者相分离。"
            + "工人处境因此不是孤立的生活细节，而是同生产关系的总体运动相连。"
            + "这一段继续说明，只有回到完整的原文句群中，论证才不至于被短窗口削弱。"
            + ("后续背景说明，暂不涉及核心命中。" * 18)
        )
        hit = SimpleNamespace(pages=[SimpleNamespace(raw_text=raw)])
        payload = {
            "context": "短窗 [[H]]工人处境[[/H]] 因此不是孤立的生活细节",
            "source_file": "",
            "pdf_pages": [],
        }
        passage = app_module._research_review_passage_text(hit, payload, "工人处境")
        self.assertIn("工人处境", passage)
        self.assertIn("完整的原文句群", passage)
        self.assertGreater(len(passage), 240)
        self.assertLessEqual(len(passage), app_module.RESEARCH_REVIEW_PASSAGE_MAX_CHARS)
        self.assertRegex(passage, r"[。！？；;!?]$")
        self.assertNotIn("[[H]]", passage)

    def test_extract_json_object_variants(self) -> None:
        self.assertEqual(
            ai_module._extract_json_object('{"quotes":["a"],"keywords":["x"]}'),
            {"quotes": ["a"], "keywords": ["x"]},
        )
        self.assertEqual(
            ai_module._extract_json_object("前言\n```json\n{\"keywords\":[\"z\"]}\n```尾"),
            {"keywords": ["z"]},
        )
        self.assertEqual(
            ai_module._extract_json_object('数组：[{"index":0,"confidence":90}]！'),
            [{"index": 0, "confidence": 90}],
        )
        self.assertEqual(ai_module._extract_json_object("毫无 JSON"), {})
        # 字符串内的括号不应破坏平衡扫描
        self.assertEqual(
            ai_module._extract_json_object('{"reason":"含]和}符号","index":3}'),
            {"reason": "含]和}符号", "index": 3},
        )

    def test_parse_assoc_plan(self) -> None:
        quotes, fragments, keywords, chapter_keywords = app_module._parse_assoc_plan(
            {
                "quotes": ["人的本质", "x", 123, ""],
                "fragments": ["社会关系的总和", "的"],
                "keywords": ["生产力", "的", "上层建筑"],
                "chapter_keywords": ["费尔巴哈", "x"],
            }
        )
        self.assertIn("人的本质", quotes)
        self.assertNotIn("x", quotes)  # 归一化后<2字被丢弃
        self.assertIn("社会关系的总和", fragments)
        self.assertNotIn("的", fragments)
        self.assertIn("生产力", keywords)
        self.assertNotIn("的", keywords)
        self.assertIn("费尔巴哈", chapter_keywords)
        self.assertEqual(app_module._parse_assoc_plan("not a dict"), ([], [], [], []))

    def test_apply_assoc_ranking_drops_invalid(self) -> None:
        candidates = ["c0", "c1", "c2"]
        ranking = [
            {"index": 2, "confidence": 80, "reason": "好"},
            {"index": 999, "confidence": 50},   # 越界 → 丢弃
            {"index": 2, "confidence": 70},     # 重复 → 丢弃
            {"index": 0, "confidence": "x"},    # 非法置信度 → 保留条目但 conf=None
            "garbage",                           # 非 dict → 丢弃
        ]
        ordered, rationale = app_module._apply_assoc_ranking(candidates, ranking)
        self.assertEqual(ordered, ["c2", "c0"])
        self.assertEqual(rationale[0], {"confidence": 80, "reason": "好"})
        self.assertEqual(rationale[1]["confidence"], None)


class SseHeartbeatStreamTests(unittest.TestCase):
    """研究综述 SSE 心跳保活：慢活(非流式综述生成)丢后台线程，其间吐心跳喂住 Cloudflare ~100s
    「首字节」计时器，完成后吐 done 事件——故全长综述也不会被砍成 524 HTML。"""

    def _collect(self, slow_fn, finalize_fn):
        # heartbeat_interval 取极小值不影响结果：join(timeout) 在线程瞬时完成时立即返回，不空等满拍。
        return list(app_module._sse_run_with_heartbeat(slow_fn, finalize_fn, heartbeat_interval=0.01))

    def test_emits_keepalive_first_then_done_payload(self) -> None:
        chunks = self._collect(
            lambda: "REVIEW_MD",
            lambda result, error: {"ok": True, "md": result, "err": error},
        )
        self.assertTrue(chunks[0].startswith(":"))             # 首字节是心跳注释，抢在 CF 计时前
        self.assertTrue(chunks[-1].startswith("event: done"))  # 末尾是 done 事件
        payload = _parse_sse_text("".join(chunks))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["md"], "REVIEW_MD")
        self.assertIsNone(payload["err"])

    def test_slow_fn_exception_passed_to_finalize(self) -> None:
        # 生成抛错(如超时)不弄断流：异常转交 finalize 决定兜底，仍吐一个干净的 done 事件。
        seen = {}

        def _boom():
            raise app_module.AIServiceError("boom")

        def _finalize(result, error):
            seen["error"] = error
            return {"ok": True, "fellback": error is not None}

        chunks = self._collect(_boom, _finalize)
        self.assertIsInstance(seen["error"], app_module.AIServiceError)
        self.assertTrue(_parse_sse_text("".join(chunks))["fellback"])

    def test_finalize_failure_yields_error_event(self) -> None:
        def _finalize(result, error):
            raise RuntimeError("finalize broke")

        chunks = self._collect(lambda: "x", _finalize)
        self.assertTrue(chunks[-1].startswith("event: error"))
        self.assertFalse(_parse_sse_text("".join(chunks))["ok"])


class AssociativeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.set_setting("access_policy", REGISTERED_FULL)
        # 每个用例前把研究型每周额度重置为默认（空＝默认 30 等），避免设额度的用例污染其他用例。
        app_module.set_setting(app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY, {})
        app_module.set_setting(app_module.AI_TOKEN_DAILY_SETTING_KEY, {})
        app_module._rate_buckets.clear()
        with sqlite3.connect(app_module.FEEDBACK_DB_PATH) as conn:
            conn.execute("DELETE FROM feedback_messages")
            conn.execute("DELETE FROM feedback_threads")
            conn.commit()
        self.client = app_module.app.test_client()

    def _csrf(self) -> str:
        html = self.client.get("/").get_data(as_text=True)
        m = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(
            r'const csrfToken = "([^"]+)";', html
        )
        return m.group(1) if m else ""

    def _login_member(self, email: str) -> None:
        create_user(
            email=email,
            display_name=email.split("@", 1)[0],
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        create_manual_subscription(user_email=email, plan_code="monthly", note="test")
        token = self._csrf()
        resp = self.client.post(
            "/login",
            data={"csrf_token": token, "email": email, "password": "correct horse battery staple"},
        )
        self.assertEqual(resp.status_code, 302)

    def _post(self, payload: dict, token: str):
        return self.client.post(
            "/api/search/associative", json=payload, headers={"X-CSRF-Token": token}
        )

    def _post_chat(self, payload: dict, token: str):
        return self.client.post(
            "/api/ai/search-chat", json=payload, headers={"X-CSRF-Token": token}
        )

    def _login_plain(self, email: str) -> int:
        """登录一个无会员订阅的普通用户（注册用户），返回 user_id。"""
        create_user(
            email=email, display_name=email.split("@", 1)[0],
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        token = self._csrf()
        resp = self.client.post(
            "/login",
            data={"csrf_token": token, "email": email, "password": "correct horse battery staple"},
        )
        self.assertEqual(resp.status_code, 302)
        return int(get_user_by_email(email)["id"])

    def test_success_is_grounded(self) -> None:
        self._login_member("assoc-ok@example.test")
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}
        candidates = app_module.corpus.locate_associative(
            quotes=plan["quotes"], keywords=plan["keywords"]
        )
        self.assertTrue(candidates, "前置：真实样本应能定位到候选")
        valid_citations = {h.citation for h in candidates}
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(
                 app_module.AI_CLIENT,
                 "rank_associative_candidates",
                 return_value=[{"index": 0, "confidence": 88, "reason": "语义匹配"}],
             ):
            resp = self._post({"gist": "找那一段"}, token)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_mode"], "associative")
        self.assertGreaterEqual(data["count"], 1)
        first = data["results"][0]
        self.assertTrue(first["citation"].startswith("《"))
        self.assertEqual(first["associative_reason"], "语义匹配")
        self.assertEqual(first["associative_confidence"], 88)
        # 接地校验：返回的每条引文都必须来自真实定位到的候选集合
        for hit in data["results"]:
            self.assertIn(hit["citation"], valid_citations)

    def test_rerank_annotates_top_and_drops_fabricated_index(self) -> None:
        # 新语义：AI 重排只“标注”按权重排在前的候选，不丢弃任何已接地候选；伪造越界 index 被忽略。
        self._login_member("assoc-junk@example.test")
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(
                 app_module.AI_CLIENT,
                 "rank_associative_candidates",
                 return_value=[{"index": 99999, "confidence": 99, "reason": "假"},
                               {"index": 0, "confidence": 60, "reason": "真"}],
             ):
            resp = self._post({"gist": "x"}, token)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["count"], 1)              # 所有已接地候选都保留（聚合展示）
        self.assertEqual(data["results"][0]["associative_reason"], "真")   # 权重最高者获标注
        self.assertEqual(data["results"][0]["associative_confidence"], 60)
        self.assertIsNotNone(data["results"][0].get("associative_weight"))
        # 伪造的越界条目不会凭空造出第 2 条标注
        self.assertFalse(any(r.get("associative_reason") == "假" for r in data["results"]))

    def test_rerank_failure_degrades_gracefully(self) -> None:
        self._login_member("assoc-degrade@example.test")
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(
                 app_module.AI_CLIENT,
                 "rank_associative_candidates",
                 side_effect=app_module.AIServiceError("boom"),
             ):
            resp = self._post({"gist": "x"}, token)
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["count"], 1)
        self.assertTrue(data["warnings"])  # 给出降级提示

    def test_expand_failure_returns_502(self) -> None:
        self._login_member("assoc-502@example.test")
        token = self._csrf()
        with mock.patch.object(
            app_module.AI_CLIENT,
            "expand_associative_query",
            side_effect=app_module.AIServiceError("down"),
        ):
            resp = self._post({"gist": "随便"}, token)
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.get_json()["ok"])

    def test_empty_gist_returns_400(self) -> None:
        self._login_member("assoc-empty@example.test")
        token = self._csrf()
        resp = self._post({"gist": "   "}, token)
        self.assertEqual(resp.status_code, 400)

    def test_no_clues_skips_rerank(self) -> None:
        self._login_member("assoc-noclue@example.test")
        token = self._csrf()
        with mock.patch.object(
            app_module.AI_CLIENT, "expand_associative_query", return_value={"quotes": [], "keywords": []}
        ), mock.patch.object(app_module.AI_CLIENT, "rank_associative_candidates") as rank_mock:
            # 纯拉丁乱码：中文语料里零命中（连中文 bigram 兜底也匹配不到），用于验证零命中→跳过 rerank。
            resp = self._post({"gist": "zzqxyvbnmqwlk"}, token)
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 0)
        self.assertIn("message", data)
        rank_mock.assert_not_called()

    def test_fallback_search_when_llm_returns_empty(self) -> None:
        # 鲁棒性：即便 LLM 返回空 plan，也用用户原词兜底检索，绝不“根本搜索不了”。
        self._login_member("assoc-fallback@example.test")
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=16)  # 一段真实存在的原文，作为用户输入
        # 兜底为空 plan → 启发式判 research → 走综述分支；mock 掉长文综述生成，避免真实 AI 调用。
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value={}), \
             mock.patch.object(app_module.AI_CLIENT, "rank_associative_candidates", return_value=[]), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", return_value="综述"):
            resp = self._post({"gist": sample}, token)
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["count"], 1)  # 兜底命中

    def test_research_mode_returns_grounded_review(self) -> None:
        # 研究意图：检索真实命中 → 生成接地综述 + 引文条；综述文本来自 AI，引文条来自真实命中。
        self._login_member("assoc-review@example.test")
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"intent": "research", "quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", return_value="综述正文 [1]") as rev:
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
        data = _read_result(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_mode"], "research_review")
        self.assertEqual(data["review_markdown"], "综述正文 [1]")
        self.assertGreaterEqual(len(data["review_citations"]), 1)
        # 接地：引文条全部来自真实命中（真实出处）
        self.assertTrue(all(c["citation"].startswith("《") for c in data["review_citations"]))
        rev.assert_called_once()

    def test_research_mode_falls_back_when_review_generation_fails(self) -> None:
        # 长文生成失败时也要返回可显示的接地综述，避免前端只看到空白综述区。
        self._login_member("assoc-review-fallback@example.test")
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"intent": "research", "quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(
                 app_module.AI_CLIENT,
                 "generate_research_review",
                 side_effect=app_module.AIServiceError("token limit"),
             ):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
        data = _read_result(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_mode"], "research_review")
        self.assertIn("## 研究综述", data["review_markdown"])
        self.assertIn("[1]", data["review_markdown"])
        self.assertGreaterEqual(len(data["review_citations"]), 1)
        self.assertTrue(data["warnings"])

    def test_research_quota_defaults_match_spec(self) -> None:
        # 后台未配置时返回规格默认值：登录5/月15/季20/年25。
        app_module.set_setting(app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY, {})
        settings = app_module._research_weekly_quota_settings()
        self.assertEqual(settings["registered"], 5)
        self.assertEqual(settings["monthly"], 15)
        self.assertEqual(settings["quarterly"], 20)
        self.assertEqual(settings["yearly"], 25)

    def test_research_blocked_when_quota_exhausted(self) -> None:
        # 额度设为 0 → 研究型检索直接 429，且绝不调用 AI（expand 不触发）。
        self._login_member("assoc-quota-blocked@example.test")
        app_module.set_setting(
            app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY,
            {"registered": 0, "monthly": 0, "quarterly": 0, "yearly": 0},
        )
        token = self._csrf()
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query") as expand_mock, \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review") as review_mock:
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
        self.assertEqual(resp.status_code, 429)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("research_quota", data)
        self.assertEqual(data["research_quota"]["limit"], 0)
        self.assertFalse(data["research_quota"]["allowed"])
        expand_mock.assert_not_called()
        review_mock.assert_not_called()

    def test_research_blocked_after_reaching_limit_via_usage_rows(self) -> None:
        # 本周已有等于上限条数的 research_review 成功记录 → 拦截。
        email = "assoc-quota-rows@example.test"
        self._login_member(email)
        app_module.set_setting(
            app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY,
            {"registered": 2, "monthly": 2, "quarterly": 2, "yearly": 2},
        )
        user = get_user_by_email(email)
        day = app_module._research_quota_week_window()["start_day"]
        for _ in range(2):
            record_ai_usage(
                user_id=int(user["id"]), day=day,
                feature=app_module.RESEARCH_QUOTA_FEATURE, success=True,
            )
        token = self._csrf()
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query") as expand_mock:
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(resp.get_json()["ok"])
        expand_mock.assert_not_called()

    def test_research_success_returns_decremented_quota(self) -> None:
        # 研究型检索成功后回传更新后的额度，剩余减一，且记账走 research_review feature。
        email = "assoc-quota-success@example.test"
        self._login_member(email)
        app_module.set_setting(
            app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY,
            {"registered": 5, "monthly": 5, "quarterly": 5, "yearly": 5},
        )
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"intent": "research", "quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", return_value="综述 [1]"):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_mode"], "research_review")
        self.assertIn("research_quota", data)
        self.assertEqual(data["research_quota"]["limit"], 5)
        self.assertEqual(data["research_quota"]["used"], 1)
        self.assertEqual(data["research_quota"]["remaining"], 4)
        # 记账确实写到 research_review feature（供本周统计）。
        user = get_user_by_email(email)
        week = app_module._research_quota_week_window()
        count = app_module.count_ai_usage_requests(
            user_id=int(user["id"]), start_day=week["start_day"], end_day=week["end_day"],
            feature=app_module.RESEARCH_QUOTA_FEATURE, success_only=True,
        )
        self.assertEqual(count, 1)

    def test_research_uses_pack_credit_when_weekly_free_exhausted(self) -> None:
        # 免费周额设为 0，但用户持有研究资源包次数 → 研究型检索仍放行，成功后扣 1 次研究包。
        email = "assoc-pack-research@example.test"
        self._login_member(email)
        app_module.set_setting(
            app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY,
            {"registered": 0, "monthly": 0, "quarterly": 0, "yearly": 0},
        )
        user = get_user_by_email(email)
        grant_ai_credits(int(user["id"]), research=3, reason="test")
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"intent": "research", "quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", return_value="综述 [1]"):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_mode"], "research_review")
        # 扣 1 次：剩余资源包 2 次
        self.assertEqual(get_ai_credit_balance(int(user["id"]), "research"), 2)
        self.assertEqual(data["research_quota"]["pack_credits"], 2)

    def test_research_blocked_when_no_free_and_no_credit(self) -> None:
        # 免费周额 0 且无资源包 → 仍 429，不消耗 AI。
        email = "assoc-pack-none@example.test"
        self._login_member(email)
        app_module.set_setting(
            app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY,
            {"registered": 0, "monthly": 0, "quarterly": 0, "yearly": 0},
        )
        token = self._csrf()
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query") as expand_mock:
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
        self.assertEqual(resp.status_code, 429)
        expand_mock.assert_not_called()

    def test_chat_uses_pack_credit_when_daily_token_exhausted(self) -> None:
        # 每日 token 限额置 0（恒超额），但持有随心问资源包 → 放行并扣 1 次 chat。
        email = "chat-pack@example.test"
        self._login_member(email)
        user = get_user_by_email(email)
        update_user_account(user_id=int(user["id"]), daily_ai_token_limit_override=0)
        grant_ai_credits(int(user["id"]), chat=2, reason="test")
        token = self._csrf()
        fake = mock.Mock()
        fake.answer_markdown = "回答内容"
        fake.to_dict = mock.Mock(return_value={"ok": True, "answer_markdown": "回答内容", "sources": []})
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat", return_value=fake):
            resp = self._post_chat({"question": "什么是剩余价值？"}, token)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(get_ai_credit_balance(int(user["id"]), "chat"), 1)
        self.assertEqual(data["ai_credits"]["chat"], 1)

    def test_chat_blocked_when_daily_token_exhausted_without_credit(self) -> None:
        # 每日 token 0 且无资源包 → 429，不调用 AI。
        email = "chat-nopack@example.test"
        self._login_member(email)
        user = get_user_by_email(email)
        update_user_account(user_id=int(user["id"]), daily_ai_token_limit_override=0)
        token = self._csrf()
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat") as ans_mock:
            resp = self._post_chat({"question": "什么是剩余价值？"}, token)
        self.assertEqual(resp.status_code, 429)
        ans_mock.assert_not_called()

    def test_effective_ai_limit_buckets(self) -> None:
        # 分档：每日额度＋本周硬上限(=每日×7)；访客 guest、月度会员 monthly；管理员不限。
        app_module.set_setting(app_module.AI_TOKEN_DAILY_SETTING_KEY, {
            "guest": 0, "registered": 4000, "monthly": 55000, "quarterly": 90000, "yearly": 120000,
        })
        member = "tokbucket-member@example.test"
        self._login_member(member)  # monthly
        muid = int(get_user_by_email(member)["id"])
        factor = app_module.AI_TOKEN_WEEKLY_FACTOR
        with app_module.app.test_request_context():
            guest = app_module._effective_ai_limit_info(None)
            self.assertEqual((guest["bucket"], guest["daily_limit"], guest["weekly_limit"]), ("guest", 0, 0))
            mem = app_module._effective_ai_limit_info({"id": muid})
            self.assertEqual(mem["bucket"], "monthly")
            self.assertEqual(mem["daily_limit"], 55000)
            self.assertEqual(mem["weekly_limit"], 55000 * factor)
            admin = app_module._effective_ai_limit_info({"id": muid, "role": "admin"})
            self.assertIsNone(admin["weekly_limit"])  # 管理员不限

    def test_registered_weekly_token_cap_blocks_chat(self) -> None:
        # 弹性额度：硬上限是本周(=每日×7)。本周累计达上限 → 随心问 429，且不调用 AI。
        email = "tok-registered-block@example.test"
        uid = self._login_plain(email)
        app_module.set_setting(app_module.AI_TOKEN_DAILY_SETTING_KEY, {"registered": 2000})
        weekly_cap = 2000 * app_module.AI_TOKEN_WEEKLY_FACTOR  # 14000
        record_ai_usage(
            user_id=uid, day=app_module.china_day_text(), feature="search-chat",
            total_tokens=weekly_cap, success=True,
        )
        token = self._csrf()
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat") as ans_mock:
            resp = self._post_chat({"question": "什么是商品拜物教？"}, token)
        self.assertEqual(resp.status_code, 429)
        ans_mock.assert_not_called()

    def test_daily_overuse_allowed_within_weekly_cap(self) -> None:
        # 弹性核心：单日用量超过「每日额度」但本周累计仍在周上限内 → 仍放行（不被每日卡死）。
        email = "tok-burst@example.test"
        uid = self._login_plain(email)
        app_module.set_setting(app_module.AI_TOKEN_DAILY_SETTING_KEY, {"registered": 2000})
        # 今日已用 5000（远超每日 2000），但 < 周上限 14000 → 仍应放行。
        record_ai_usage(
            user_id=uid, day=app_module.china_day_text(), feature="search-chat",
            total_tokens=5000, success=True,
        )
        token = self._csrf()
        fake = mock.Mock()
        fake.answer_markdown = "回答"
        fake.to_dict = mock.Mock(return_value={"ok": True, "answer_markdown": "回答", "sources": []})
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat", return_value=fake):
            resp = self._post_chat({"question": "什么是剩余价值？"}, token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_registered_user_within_limit_returns_token_quota(self) -> None:
        # 未超额 → 随心问成功并回带 ai_token_quota（limit=本周=每日×7、剩余<上限、未耗尽）。
        email = "tok-registered-ok@example.test"
        self._login_plain(email)
        app_module.set_setting(app_module.AI_TOKEN_DAILY_SETTING_KEY, {"registered": 50000})
        weekly_cap = 50000 * app_module.AI_TOKEN_WEEKLY_FACTOR
        token = self._csrf()
        fake = mock.Mock()
        fake.answer_markdown = "回答"
        fake.to_dict = mock.Mock(return_value={"ok": True, "answer_markdown": "回答", "sources": []})
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat", return_value=fake):
            resp = self._post_chat({"question": "什么是剩余价值？"}, token)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("ai_token_quota", data)
        self.assertFalse(data["ai_token_quota"]["unlimited"])
        self.assertEqual(data["ai_token_quota"]["limit"], weekly_cap)  # 硬上限＝本周
        self.assertEqual(data["ai_token_quota"]["daily_limit"], 50000)  # 每日参考
        self.assertLess(data["ai_token_quota"]["remaining"], weekly_cap)  # 本次已计入用量
        self.assertFalse(data["ai_token_quota"]["exhausted"])

    def test_admin_unlimited_ai_tokens(self) -> None:
        # 管理员不受每日额度限制：即便 registered=0 也能用，且 ai_token_quota.unlimited=True。
        email = "tok-admin@example.test"
        uid = self._login_plain(email)
        app_module.set_setting(app_module.AI_TOKEN_DAILY_SETTING_KEY, {"registered": 0})
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
            conn.commit()
        token = self._csrf()
        fake = mock.Mock()
        fake.answer_markdown = "回答"
        fake.to_dict = mock.Mock(return_value={"ok": True, "answer_markdown": "回答", "sources": []})
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat", return_value=fake):
            resp = self._post_chat({"question": "什么是异化劳动？"}, token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ai_token_quota"]["unlimited"])

    def test_mascot_is_unlimited_and_uses_flash_model(self) -> None:
        # 马克思形象＝无限量基础服务：即便主通道每日额度=0 仍可用，且固定走 deepseek-v4-flash。
        email = "mascot-flash@example.test"
        self._login_plain(email)
        app_module.set_setting(app_module.AI_TOKEN_DAILY_SETTING_KEY, {"registered": 0})
        token = self._csrf()
        with mock.patch.object(
            app_module.AI_CLIENT, "chat_complete", return_value="历史属于勇于思考的人。"
        ) as cc:
            resp = self.client.post(
                "/api/ai/mascot-chat",
                json={"mode": "ask", "user_text": "你好"},
                headers={"X-CSRF-Token": token},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])  # 不受每日额度限制
        self.assertEqual(cc.call_args.kwargs.get("model"), app_module.MASCOT_AI_MODEL)
        self.assertEqual(app_module.MASCOT_AI_MODEL, "deepseek-v4-flash")

    def test_research_citation_highlights_quoted_sentence(self) -> None:
        # 端到端：综述逐字引用了某段原文的一句 → 对应引文方框应高亮该句并标「综述已引用」。
        self._login_member("assoc-hl@example.test")
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"intent": "research", "quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}

        def _fake_review(topic, passages):
            text = passages[0]["text"] if passages else ""
            chunks = app_module._sentence_chunks(text)
            sent = chunks[0] if chunks else text[:30]
            return f"## 研究综述\n马克思指出「{sent}」[1]。\n## 小结\n完。"

        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", side_effect=_fake_review):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
        self.assertTrue(data["ok"])
        cits = data["review_citations"]
        self.assertTrue(any(c.get("review_quoted") for c in cits), "应有引文被标注为综述已引用")
        quoted = next(c for c in cits if c.get("review_quoted"))
        self.assertIn("[[H]]", quoted["context"])  # 亮标落在综述用到的句子上

    def test_desktop_mode_short_circuits_without_ai(self) -> None:
        self._login_member("assoc-desktop@example.test")
        token = self._csrf()
        desktop = replace(app_module.DEPLOYMENT, app_mode="desktop")
        with mock.patch.object(app_module, "DEPLOYMENT", desktop), \
             mock.patch.object(app_module.AI_CLIENT, "expand_associative_query") as expand_mock:
            resp = self._post({"gist": "x"}, token)
        expand_mock.assert_not_called()
        # 桌面模式不应给出成功的联想结果
        self.assertFalse(resp.get_json().get("ok") is True and resp.status_code == 200
                         and resp.get_json().get("display_mode") == "associative")


class AiCreditLedgerTests(unittest.TestCase):
    """资源包次数台账：发放 / 消耗 / 原子性 / credit_pack 支付发次数不开会员。"""

    def _new_user(self, email: str) -> int:
        user = create_user(
            email=email, display_name=email.split("@", 1)[0],
            password_hash=generate_password_hash("x"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        return int(user["id"])

    def test_grant_and_balance(self) -> None:
        uid = self._new_user("credit-grant@example.test")
        self.assertEqual(get_ai_credit_balances(uid), {"research": 0, "chat": 0})
        grant_ai_credits(uid, research=10, chat=20, reason="test")
        self.assertEqual(get_ai_credit_balances(uid), {"research": 10, "chat": 20})
        self.assertEqual(get_ai_credit_balance(uid, "research"), 10)

    def test_consume_decrements_and_floors_at_zero(self) -> None:
        uid = self._new_user("credit-consume@example.test")
        grant_ai_credits(uid, research=2, reason="test")
        self.assertTrue(consume_ai_credit(uid, "research", reason="t"))
        self.assertTrue(consume_ai_credit(uid, "research", reason="t"))
        self.assertFalse(consume_ai_credit(uid, "research", reason="t"))  # 余额 0 不再扣
        self.assertEqual(get_ai_credit_balance(uid, "research"), 0)

    def test_consume_unknown_kind_or_anonymous_is_noop(self) -> None:
        uid = self._new_user("credit-bad@example.test")
        grant_ai_credits(uid, chat=1, reason="test")
        self.assertFalse(consume_ai_credit(uid, "nope", reason="t"))
        self.assertFalse(consume_ai_credit(None, "chat", reason="t"))
        self.assertEqual(get_ai_credit_balance(uid, "chat"), 1)

    def test_credit_pack_payment_grants_credits_not_subscription(self) -> None:
        upsert_plan(
            code="pack_test", name="测试资源包", price_cents=300, interval_months=1,
            kind="credit_pack", research_credits=10, chat_credits=20, sort_order=99,
        )
        uid = self._new_user("credit-pack-pay@example.test")
        order = create_pending_order(user_id=uid, plan_code="pack_test")
        result = mark_order_paid(order_no=order["order_no"], provider="manual", source="manual")
        self.assertIsNone(result["subscription"])  # 不开会员
        self.assertEqual(result["credits"], {"research": 10, "chat": 20})
        self.assertEqual(get_ai_credit_balances(uid), {"research": 10, "chat": 20})

    def test_credit_pack_payment_is_idempotent(self) -> None:
        upsert_plan(
            code="pack_test2", name="测试资源包2", price_cents=300, interval_months=1,
            kind="credit_pack", research_credits=5, chat_credits=0, sort_order=99,
        )
        uid = self._new_user("credit-pack-idem@example.test")
        order = create_pending_order(user_id=uid, plan_code="pack_test2")
        mark_order_paid(order_no=order["order_no"], provider="manual", source="manual")
        # 重复回调（已 paid）不应再发一遍次数
        mark_order_paid(order_no=order["order_no"], provider="manual", source="manual")
        self.assertEqual(get_ai_credit_balance(uid, "research"), 5)

    def test_default_pack_seeded(self) -> None:
        plan = get_plan("pack_basic")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "credit_pack")
        self.assertEqual(plan["research_credits"], 10)
        self.assertEqual(plan["chat_credits"], 20)


class ReviewCitationHighlightTests(unittest.TestCase):
    """研究综述引文「亮标」：标在综述真正逐字引用到的句子上（而非检索词）。"""

    def test_research_review_sources_are_diversified_by_library(self) -> None:
        def hit(book: str, page: int, score: int = 90):
            return SimpleNamespace(
                book=book, source_file=f"{book}-{page}.pdf", pages=[SimpleNamespace(pdf_page=page)], score=score
            )

        candidates = (
            [hit("文集", i) for i in range(1, 11)]
            + [hit("全集", i) for i in range(1, 4)]
            + [hit("列宁全集", i) for i in range(1, 4)]
        )
        selected = app_module._select_research_review_hits(candidates, limit=6)
        books = [h.book for h in selected]
        self.assertEqual(books[:3], ["文集", "全集", "列宁全集"])
        self.assertIn("全集", books)
        self.assertIn("列宁全集", books)

    def test_research_review_sources_do_not_force_weak_libraries(self) -> None:
        def hit(book: str, page: int, score: int):
            return SimpleNamespace(
                book=book, source_file=f"{book}-{page}.pdf", pages=[SimpleNamespace(pdf_page=page)], score=score
            )

        candidates = [
            hit("文集", 1, 100),
            hit("文集", 2, 98),
            hit("全集", 1, 86),
            hit("毛泽东选集", 1, 35),
        ]
        selected = app_module._select_research_review_hits(candidates, limit=3)
        books = [h.book for h in selected]
        self.assertEqual(books, ["文集", "全集", "文集"])
        self.assertNotIn("毛泽东选集", books)

    def test_extract_quotes_longest_first(self) -> None:
        md = '马克思指出「商品是用来交换的劳动产品」，又谈到“价值”。'
        qs = app_module._extract_review_quotes(md)
        self.assertIn("商品是用来交换的劳动产品", qs)
        self.assertEqual(qs[0], "商品是用来交换的劳动产品")  # 长引文优先

    def test_used_span_matches_quoted_sentence_in_passage(self) -> None:
        passage = "在本章里，商品是用来交换的劳动产品，这一判断至关重要。"
        span = app_module._review_used_span_in_passage(
            passage, ["商品是用来交换的劳动产品", "无关的话"]
        )
        self.assertEqual(span, "商品是用来交换的劳动产品")

    def test_quotes_are_grouped_by_review_source_number(self) -> None:
        md = "马克思说「第一条原文」[1]。恩格斯说「第二条原文」[2]。"
        grouped = app_module._review_quotes_by_index(md)
        self.assertEqual(grouped[1], ["第一条原文"])
        self.assertEqual(grouped[2], ["第二条原文"])

    def test_used_span_matches_normalized_punctuation(self) -> None:
        passage = "这里说，商品是用来交换的劳动产品。"
        span = app_module._review_used_span_in_passage(
            passage, ["商品，是用来交换的劳动产品"]
        )
        self.assertEqual(span, "商品是用来交换的劳动产品")

    def test_paraphrased_citation_picks_source_sentence(self) -> None:
        passage = "前文叙述。商品是用来交换的劳动产品，这一点说明交换关系的重要性。后续展开。"
        units = ["综述认为商品首先体现为用于交换的劳动产品，并由此进入社会关系分析。"]
        span = app_module._best_review_cited_sentence_in_passage(passage, units)
        self.assertEqual(span, "商品是用来交换的劳动产品，这一点说明交换关系的重要性。")

    def test_quoted_span_can_be_recovered_from_hit_volume(self) -> None:
        picked = None
        for vols in app_module.corpus.books.values():
            for vol in vols:
                for page in vol.pages:
                    match = re.search(r"[\u4e00-\u9fff]{10,}", page.raw_text or "")
                    if match:
                        picked = (vol, page, match.group(0)[:12])
                        break
                if picked:
                    break
            if picked:
                break
        if not picked:
            self.skipTest("语料中没有可用于回捞测试的中文连续片段")
        vol, page, quote = picked
        hit = SimpleNamespace(source_file=vol.source_file, pages=[page])
        source_text, span, pdf_page = app_module._review_used_span_in_hit_volume(hit, [quote])
        self.assertIn(quote, span)
        self.assertIn(span, source_text)
        self.assertEqual(pdf_page, page.pdf_page)

    def test_multiple_quotes_on_same_page_share_one_evidence_page(self) -> None:
        picked = None
        for vols in app_module.corpus.books.values():
            for vol in vols:
                for page in vol.pages:
                    matches = re.findall(r"[\u4e00-\u9fff]{10,}", page.raw_text or "")
                    if len(matches) >= 2:
                        picked = (vol, page, matches[0][:10], matches[1][:10])
                        break
                if picked:
                    break
            if picked:
                break
        if not picked:
            self.skipTest("语料中没有可用于多证据测试的页面")
        vol, page, q1, q2 = picked
        hit = SimpleNamespace(source_file=vol.source_file, pages=[page])
        base = {
            "book": vol.book, "volume": vol.volume, "source_file": vol.source_file,
            "citation": "测试出处", "pdf_pages": [page.pdf_page], "printed_pages": [page.printed_page],
        }
        evidence, _base, _highlight = app_module._make_review_evidence_items(
            hit, base, "", [q1, q2], [], False, "测试"
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["pdf_page"], page.pdf_page)
        self.assertIn("[[H]]", evidence[0]["context"])

    def test_wenji_backtrace_only_applies_to_marx_engels_quanji(self) -> None:
        fake_volume = SimpleNamespace(book="列宁全集")
        with mock.patch.object(app_module, "_hit_volume", return_value=fake_volume), \
             mock.patch.object(app_module, "_text_match_in_book") as matcher:
            self.assertIsNone(app_module._prefer_wenji_evidence_for_hit(object(), "国家", kind="quote"))
        matcher.assert_not_called()

    def test_used_span_empty_when_passage_not_quoted(self) -> None:
        self.assertEqual(app_module._review_used_span_in_passage("一段无关原文。", ["某引文"]), "")

    def test_context_highlights_used_span(self) -> None:
        passage = "前文叙述。商品是用来交换的劳动产品。后续展开。"
        ctx = app_module._review_citation_context(passage, "商品是用来交换的劳动产品")
        self.assertIn("[[H]]商品是用来交换的劳动产品[[/H]]", ctx)

    def test_context_plain_when_no_span(self) -> None:
        ctx = app_module._review_citation_context("一段真实原文片段。", "")
        self.assertNotIn("[[H]]", ctx)
        self.assertIn("一段真实原文片段", ctx)


if __name__ == "__main__":
    unittest.main()
