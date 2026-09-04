from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

# 与其余测试一致：导入 ai 前把 APPDATA 硬置到临时目录，避免触碰真实数据目录。
from _test_env import APPDATA as _TMP_APPDATA  # noqa: E402

import ai as ai_module  # noqa: E402


# 截图里智谱 search_std 真实返回的三条内容农场垃圾（字段名同智谱 /web_search 响应）。
_SCREENSHOT_JUNK = [
    {"title": "跟男朋友做爱一点感觉都没有怎么回事_医联媒体", "link": "http://m.example/1", "media": "医联媒体", "publish_date": "2024-10-14", "content": "性生活质量……"},
    {"title": "为什么做爱时我一点感觉也没有? - 我爱康", "link": "http://m.example/2", "media": "我爱康", "publish_date": "2019-09-01", "content": "……"},
    {"title": "姨妈量很少几乎没有是什么原因_医联媒体", "link": "http://m.example/3", "media": "医联媒体", "publish_date": "2022-11-17", "content": "月经量少……"},
]
_GOOD_RESULT = {
    "title": "马克思劳动价值论：活劳动与价值创造",
    "link": "http://edu.example/marx",
    "media": "学术期刊",
    "publish_date": "2021-03-01",
    "content": "马克思在《资本论》中论述活劳动是价值的唯一源泉……",
}


def _client() -> "ai_module.ZAIClient":
    # 入口提纯/出口过滤的方法都不依赖 config（仅用静态词表与 LOGGER），
    # 故绕开 __init__ 直接造裸实例，免去构造完整配置/密钥。
    return object.__new__(ai_module.ZAIClient)


class DistillQueryTermsTests(unittest.TestCase):
    def test_strips_leading_question_shell(self):
        out = ai_module.ZAIClient._distill_query_terms("为什么资本会增殖？")
        self.assertNotIn("为什么", out)
        self.assertIn("资本会增殖", out)

    def test_strips_trailing_what_does_it_mean(self):
        self.assertEqual(
            ai_module.ZAIClient._distill_query_terms("商品拜物教是什么意思？"),
            "商品拜物教",
        )

    def test_strips_content_farm_tail_patterns(self):
        self.assertEqual(
            ai_module.ZAIClient._distill_query_terms("异化劳动是怎么回事"), "异化劳动"
        )
        self.assertEqual(
            ai_module.ZAIClient._distill_query_terms("今天天气怎么样"), "今天天气"
        )

    def test_pure_shell_falls_back_to_original(self):
        # 剥成空/过短则回退原句，绝不返回空串。
        self.assertTrue(ai_module.ZAIClient._distill_query_terms("为什么呢"))

    def test_blank_input(self):
        self.assertEqual(ai_module.ZAIClient._distill_query_terms("  "), "")


class ZhipuSearchQueryTests(unittest.TestCase):
    def setUp(self):
        self.client = _client()

    def test_on_topic_query_not_anchored(self):
        out = self.client.zhipu_search_query("商品拜物教是什么意思？")
        self.assertEqual(out, "商品拜物教")
        self.assertNotIn("马克思主义", out)

    def test_off_topic_colloquial_gets_domain_anchor(self):
        out = self.client.zhipu_search_query("价值是怎么产生的")
        self.assertTrue(out.startswith("马克思主义"))

    def test_reading_context_anchors_without_prefix(self):
        out = self.client.zhipu_search_query(
            "活劳动如何创造价值", page_context={"section_title": "剩余价值"}
        )
        self.assertIn("剩余价值", out)
        self.assertFalse(out.startswith("马克思主义"))

    def test_truncated_to_70_chars(self):
        out = self.client.zhipu_search_query("马克思" + "啊" * 200)
        self.assertLessEqual(len(out), 70)


class LooksLikeWebJunkTests(unittest.TestCase):
    def test_denied_site_name(self):
        # 截图样本①：站点名命中黑名单。
        self.assertTrue(
            ai_module.ZAIClient._looks_like_web_junk(
                {"title": "跟男朋友做爱一点感觉都没有怎么回事", "site": "医联媒体", "link": "http://x", "snippet": ""}
            )
        )

    def test_denied_domain(self):
        self.assertTrue(
            ai_module.ZAIClient._looks_like_web_junk(
                {"title": "姨妈量很少几乎没有是什么原因", "site": "健康号", "link": "http://a.120ask.com/p/1", "snippet": ""}
            )
        )

    def test_medical_signal_without_anchor(self):
        # 截图样本②：站点名抓不到，但有医疗强信号且无马列锚点。
        self.assertTrue(
            ai_module.ZAIClient._looks_like_web_junk(
                {"title": "为什么做爱时一点感觉也没有", "site": "某问答", "link": "http://foo.example", "snippet": "性生活质量下降的原因"}
            )
        )

    def test_marx_anchor_protects_against_false_positive(self):
        # 含医疗词但同时含马列锚点：不应误删（如谈生育与私有制起源）。
        self.assertFalse(
            ai_module.ZAIClient._looks_like_web_junk(
                {"title": "恩格斯论家庭与私有制", "site": "学术网", "link": "http://edu.example", "snippet": "怀孕、生育与家庭形态的历史唯物分析"}
            )
        )

    def test_relevant_source_kept(self):
        self.assertFalse(
            ai_module.ZAIClient._looks_like_web_junk(
                {"title": "马克思论异化劳动", "site": "哲学研究", "link": "http://phil.example", "snippet": "马克思在《1844年经济学哲学手稿》中分析了异化劳动"}
            )
        )


class FilterWebSourcesTests(unittest.TestCase):
    def setUp(self):
        self.client = _client()

    def test_drops_junk_keeps_relevant(self):
        # 还原截图：1 条真实引文相关 + 3 条内容农场垃圾。
        sources = [
            {"title": "马克思的劳动价值论概述", "site": "理论网", "link": "http://t.example", "snippet": "活劳动创造价值"},
            {"title": "跟男朋友做爱一点感觉都没有怎么回事", "site": "医联媒体", "link": "http://1", "snippet": ""},
            {"title": "为什么做爱时我一点感觉也没有", "site": "我爱康", "link": "http://2", "snippet": ""},
            {"title": "姨妈量很少几乎没有是什么原因", "site": "医联媒体", "link": "http://3", "snippet": ""},
        ]
        kept = self.client._filter_web_sources(sources, "活劳动")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], "马克思的劳动价值论概述")

    def test_all_junk_returns_empty(self):
        sources = [
            {"title": "做爱没感觉怎么回事", "site": "寻医问药", "link": "http://1", "snippet": ""},
            {"title": "月经推迟是什么原因", "site": "39健康", "link": "http://2", "snippet": ""},
        ]
        self.assertEqual(self.client._filter_web_sources(sources), [])

    def test_empty_and_none_input(self):
        self.assertEqual(self.client._filter_web_sources([]), [])
        self.assertEqual(self.client._filter_web_sources(None), [])


class EndToEndGroundingTests(unittest.TestCase):
    """端到端（除 HTTP 外全真实代码路径）：只 mock _post_json，回放截图真实垃圾。"""

    def _client_with_recorder(self, search_result):
        client = _client()
        client.config = SimpleNamespace(
            zhipu_search_engine="search_std",
            zhipu_search_count=5,
            zhipu_base_url="http://stub",
            zhipu_api_key="stub",
        )
        sent = {}

        def fake_post_json(endpoint, payload, **kwargs):
            sent["endpoint"] = endpoint
            sent["payload"] = payload
            return {"search_result": search_result}

        client._post_json = fake_post_json  # type: ignore[assignment]
        return client, sent

    def test_real_junk_filtered_only_good_grounded(self):
        client, sent = self._client_with_recorder(_SCREENSHOT_JUNK + [_GOOD_RESULT])
        query = client.zhipu_search_query("活劳动是怎么创造价值的")
        messages, sources, tool_stages = client._zhipu_grounding_or_stages(
            [{"role": "system", "content": "sys"}], query
        )
        # 实际发给智谱的查询词：口语壳被剥、且带上领域锚点。
        self.assertEqual(sent["payload"]["search_query"], query)
        self.assertIn("活劳动", query)
        # 三条垃圾全部剔除，只剩相关结果，并已注入 grounding（不再带检索工具）。
        self.assertEqual([s["title"] for s in sources], [_GOOD_RESULT["title"]])
        self.assertEqual(tool_stages, [None])
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("活劳动", messages[1]["content"])  # grounding 注入了好结果

    def test_all_junk_no_grounding_no_refetch(self):
        client, _ = self._client_with_recorder(list(_SCREENSHOT_JUNK))
        original = [{"role": "system", "content": "sys"}]
        messages, sources, tool_stages = client._zhipu_grounding_or_stages(
            original, client.zhipu_search_query("做爱没感觉怎么回事")
        )
        # 全判垃圾：不注入 grounding、无来源、且不再对话内重搜（tool_stages 为纯对话档）。
        self.assertEqual(sources, [])
        self.assertEqual(tool_stages, [None])
        self.assertEqual(messages, original)


if __name__ == "__main__":
    unittest.main()
