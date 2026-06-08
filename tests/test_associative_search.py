from __future__ import annotations

import atexit
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import warnings
from dataclasses import replace
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
from membership import create_manual_subscription, create_user  # noqa: E402
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


class AssociativeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.set_setting("access_policy", REGISTERED_FULL)
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
            resp = self._post({"gist": "无法提取线索"}, token)
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
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value={}), \
             mock.patch.object(app_module.AI_CLIENT, "rank_associative_candidates", return_value=[]):
            resp = self._post({"gist": sample}, token)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["count"], 1)  # 兜底命中

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


if __name__ == "__main__":
    unittest.main()
