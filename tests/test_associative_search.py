from __future__ import annotations

import json
import os
import re
import sqlite3
import unittest
import warnings
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

warnings.filterwarnings("ignore", category=ResourceWarning)
# 必须在导入 app 前“硬置”到临时 APPDATA：Windows 上 APPDATA 恒被设置，setdefault 会失效而误用真实库。
from _test_env import APPDATA as _TMP_APPDATA  # noqa: E402
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
    get_ai_token_usage,
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


def _drain(resp):
    """在调用方的 mock.patch 上下文内立刻读完响应体（含 SSE 心跳保活流）。

    随心问的 SSE 心跳保活（``_sse_run_with_heartbeat``）把慢活（``answer_search_chat``）丢进后台
    worker 线程，由生成器边吐心跳边等其完成。Werkzeug 测试客户端默认惰性消费响应体——若推迟到
    测试里 ``with mock.patch(...)`` 退出后才读流（如 ``_read_result`` 写在 with 块外），worker 届时
    才被驱动，会调到**真实** AI 而非 mock。``_post_chat`` 在返回前（仍在 with 内）即调本函数把流读完，
    确保 mock 生效。生产环境由 waitress 持续驱动生成器、worker 立即启动，本问题仅存在于测试客户端。"""
    resp.get_data()


def _read_result(resp):
    """读取检索响应：研究综述走 SSE 心跳保活流(text/event-stream)，取最后一个 done 事件的 JSON；
    其余仍是普通 JSON。"""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in ctype:
        return resp.get_json()
    return _parse_sse_text(resp.get_data(as_text=True))


class FuzzyToleranceTests(unittest.TestCase):
    """近似匹配容错随查询长度分档的纯函数单元测试（不依赖语料，快速确定）。

    背景：整段粘贴的经典语录在各书库扫描件里往往因散布的 OCR 错字漏字（及被误识入正文的
    脚注残字）而精确零命中，旧策略近似兜底把容错硬封在 2 字，导致「差不多一模一样」的长
    引文在所有书库全部漏检。此处锁定「短/中查询仍从严、长段落按长度放宽」的契约。
    """

    def test_short_tolerance_stays_strict(self) -> None:
        """短查询（<36 字）必须保持从严：常用字偶然重叠即可碰瓷，且逐字校对成本低。"""
        f = search_module._fuzzy_allowed_errors
        self.assertEqual(f(0), 0)
        self.assertEqual(f(9), 0)        # 短于 MIN_FUZZY_QUERY_LEN 不做近似
        self.assertEqual(f(10), 1)       # 10–19 字容 1 错
        self.assertEqual(f(19), 1)
        self.assertEqual(f(20), 2)       # 20–35 字容 2 错（与放宽前一致）
        self.assertEqual(f(35), 2)

    def test_medium_tolerance_scales_like_long_passages(self) -> None:
        """中等长度（36–59 字）与长段落同用 ≈8% 比例容错。

        起因（2026-07-30）：读者手里的引文常与扫描原文有个别字出入（OCR 错字、版本差异、
        凭记忆默写）。旧策略把 20–59 字硬封在 2 错，导致「40 字错 3 字」（7.5%）在全库
        精确+近似双双零命中，而同样错法的 60 字引文却能命中——同一比例的差异，只因长度
        跨过 60 字这条线就被区别对待，且在 59/60 处形成 K=2 → K=5 的断崖。
        线上实测：放宽后「48 字错 3 字」精准召回原书且只出 1 组；语料里不存在的常用词
        拼装句仍零命中（未引入碰瓷）；编辑距离超过 K 的查询照常被拒；耗时仅 +1%。
        """
        f = search_module._fuzzy_allowed_errors
        step = search_module.FUZZY_LONG_ERROR_STEP
        self.assertEqual(f(36), 36 // step)          # 3
        self.assertEqual(f(48), 48 // step)          # 4
        self.assertEqual(f(59), 59 // step)          # 4
        # 抹平断崖：59 与 60 之间不再从 2 跳到 5
        self.assertLessEqual(f(60) - f(59), 1)
        # 但不得低于放宽前的水平（只放宽、不收紧）
        for n in (36, 40, 48, 59):
            self.assertGreaterEqual(f(n), search_module.FUZZY_MAX_ERRORS)

    def test_long_passage_tolerance_scales_with_length(self) -> None:
        f = search_module._fuzzy_allowed_errors
        # 跨过 FUZZY_LONG_QUERY_LEN(60) 后容错开始随长度增长，且严格大于旧的 2 字硬顶
        self.assertEqual(f(60), 60 // search_module.FUZZY_LONG_ERROR_STEP)
        self.assertGreater(f(120), search_module.FUZZY_MAX_ERRORS)
        self.assertGreater(f(200), f(120))            # 越长容错越多
        # 封顶：极长查询不会把近似扫描的 cutoff 压得过低
        self.assertEqual(f(10_000), search_module.FUZZY_LONG_MAX_ERRORS)
        # 单调不减：长度递增，容错不应回落
        vals = [f(n) for n in range(0, 400, 7)]
        self.assertEqual(vals, sorted(vals))

    def test_reported_long_quote_gets_generous_tolerance(self) -> None:
        # 用户报告的《德意志意识形态》长文段：归一化后约 140+ 字，
        # 旧策略只容 2 错（几处 OCR 错字即漏检），新策略应给出足够吸收散布错字的容错。
        passage = (
            "进行革命的阶级，仅就它对抗另一个阶级这一点来说，从一开始就不是作为一个阶级，"
            "而是作为全社会的代表出现的；它俨然以社会全体群众的姿态反对唯一的统治阶级。"
            "它之所以能这样做，是因为它的利益在开始时的确同其余一切非统治阶级的共同利益还有"
            "更多的联系，在当时存在的那些关系的压力下还来不及发展为特殊阶级的特殊利益"
        )
        q_len = len(search_module.normalize(passage))
        self.assertGreaterEqual(q_len, 60, "该长文段归一化后应达到长段落阈值")
        allowed = search_module._fuzzy_allowed_errors(q_len)
        # 旧策略此处只有 2；新策略应显著放宽（≥8），足以吸收「个别错字漏字」而仍高度逐字。
        self.assertGreaterEqual(allowed, 8)
        self.assertLessEqual(allowed, search_module.FUZZY_LONG_MAX_ERRORS)


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
        # book_scope 限定在取样所在书库：两字词在全语料属超高频，不限范围时每卷都要全文扫描，
        # 语料扩容后（2026-08 已达 740MB）足以让 pre-push 门禁跑不完；目标窗口本就在该书内，
        # 限定范围不改变断言语义。
        book, sample = _corpus_sample(min_len=60)
        kws = [sample[0:2], sample[20:22], sample[40:42]]
        hits = app_module.corpus.keyword_cooccurrence(kws, window=200, book_scope=[book])
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

    def test_locate_associative_expands_cross_page_hit_to_complete_sentence(self) -> None:
        page1_raw = "本页前一句已经结束。跨页完整句的前半部分仍在这里，"
        page2_raw = "后半部分包含关键命中片段并在这里结束。下一句不应被带入。"
        pages = [
            search_module.Page(1, "1", page1_raw, search_module.normalize(page1_raw)),
            search_module.Page(2, "2", page2_raw, search_module.normalize(page2_raw)),
        ]
        volume = search_module.Volume.build("测试书", 1, "test.pdf", "测试书", pages)
        corpus = object.__new__(search_module.Corpus)
        corpus.books = {"测试书": [volume]}
        corpus._volumes_by_source_file = {"test.pdf": volume}
        corpus.book_config_by_key = {}
        corpus.volumes_cfg = {"publisher": "测试出版社", "place": "北京"}
        corpus._make_citation = lambda *args, **kwargs: "《测试书》第2页。"
        corpus._make_citations = lambda *args, **kwargs: {}
        corpus.get_section_for_page = lambda *args, **kwargs: None

        direct_hits = corpus.locate_quote("关键命中片段", allow_fuzzy=False)
        hits = corpus.locate_associative(
            quotes=["关键命中片段"],
            keywords=[],
            fragments=[],
        )

        self.assertTrue(direct_hits)
        self.assertNotIn("跨页完整句的前半部分", direct_hits[0].context)
        self.assertTrue(hits)
        context = hits[0].context
        self.assertIn("跨页完整句的前半部分仍在这里", context)
        self.assertIn("后半部分包含[[H]]关键命中片段[[/H]]并在这里结束。", context)
        self.assertNotIn("本页前一句", context)
        self.assertNotIn("下一句不应被带入", context)
        self.assertTrue(context.endswith("。"), context)

    def test_locate_associative_capped_and_sorted(self) -> None:
        # book_scope 同上：避免超高频两字词在全语料上的无界扫描拖垮门禁。
        book, sample = _corpus_sample(min_len=60)
        kws = [sample[0:2], sample[20:22], sample[40:42]]
        res = app_module.corpus.locate_associative(quotes=[sample], keywords=kws, book_scope=[book])
        self.assertLessEqual(len(res), search_module.ASSOC_CANDIDATE_CAP)
        scores = [h.score for h in res]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(0 <= s <= 100 for s in scores))

    def test_diversify_by_book_respects_group_key(self) -> None:
        # 同一「著作群」的多本书应共用一个配额：随心问接地用此把马恩多版本合并，防霸榜。
        Corpus = search_module.Corpus
        hits = (
            [SimpleNamespace(book="文集") for _ in range(4)]
            + [SimpleNamespace(book="全集") for _ in range(2)]
            + [SimpleNamespace(book="列宁全集") for _ in range(2)]
        )
        group = lambda h: "马恩" if h.book in ("文集", "全集") else h.book
        # 不分组（按书）：cap=2 → 文集2 + 全集2 仍占满前 4（两版本各占名额）
        by_book = [h.book for h in Corpus._diversify_by_book(hits, per_book_cap=2)][:4]
        self.assertEqual(by_book, ["文集", "文集", "全集", "全集"])
        # 分组（马恩合一）：cap=2 → 马恩群至多 2，列宁得以进入前 4
        by_group = [h.book for h in Corpus._diversify_by_book(hits, per_book_cap=2, group_key=group)][:4]
        self.assertEqual(by_group, ["文集", "文集", "列宁全集", "列宁全集"])
        # 不丢命中，只是把超额者后置
        self.assertEqual(len(Corpus._diversify_by_book(hits, per_book_cap=2, group_key=group)), len(hits))

    def test_author_group_key_merges_marx_engels_editions(self) -> None:
        # 数据驱动（citation_title 以「马克思恩格斯」起头）：三套马恩版本归一，其它作者各自独立。
        corpus = app_module.corpus
        keyed = {b: corpus._author_group_key(SimpleNamespace(book=b))
                 for b in ("文集", "全集", "全集二版", "列宁全集", "毛泽东选集")}
        self.assertEqual(keyed["文集"], "马克思恩格斯")
        self.assertEqual(keyed["全集"], "马克思恩格斯")
        self.assertEqual(keyed["全集二版"], "马克思恩格斯")
        self.assertEqual(keyed["列宁全集"], "列宁全集")
        self.assertEqual(keyed["毛泽东选集"], "毛泽东选集")

    def test_locate_associative_author_diversify_caps_marx_engels(self) -> None:
        # 端到端：用一个会命中马恩多版本 + 列宁的概念查询，断言按作者铺开后马恩群在前 4 至多占 2。
        kws = ["国家", "阶级", "无产阶级专政", "革命"]
        res = app_module.corpus.locate_associative(
            quotes=[], keywords=kws, fragments=[], chapter_keywords=[],
            diversify_per_book=2, diversify_by_author=True,
        )
        top_books = [corpus_book for corpus_book in (h.book for h in res)][:4]
        marx_editions = {"文集", "全集", "全集二版"}
        self.assertLessEqual(sum(1 for b in top_books if b in marx_editions), 2,
                             f"马恩群在前 4 不应超过 2 条：{top_books}")
        self.assertTrue(set(top_books) - marx_editions, f"应有非马恩著作进入前 4：{top_books}")

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
    def test_research_http_pool_is_isolated_from_interactive_pool(self) -> None:
        interactive = mock.Mock()
        interactive.acquire.return_value = True
        research = mock.Mock()
        research.acquire.return_value = True
        with mock.patch.object(ai_module, "_AI_HTTP_INTERACTIVE_SEMAPHORE", interactive), \
             mock.patch.object(ai_module, "_AI_HTTP_RESEARCH_SEMAPHORE", research):
            with ai_module._ai_http_slot():
                pass
            with ai_module.research_ai_http_context():
                with ai_module._ai_http_slot():
                    pass
        interactive.acquire.assert_called_once()
        interactive.release.assert_called_once()
        research.acquire.assert_called_once()
        research.release.assert_called_once()
        self.assertGreaterEqual(ai_module._AI_HTTP_INTERACTIVE_CONCURRENCY, 10)
        self.assertLessEqual(ai_module._AI_HTTP_RESEARCH_CONCURRENCY, 2)

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
        initial = "## 开篇\n" + ("这是一段尚未收束的研究综述。[1]\n" * 420)
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
        self.assertTrue(cc.call_args_list[1].kwargs["disable_thinking"])

    def test_research_review_does_not_continue_when_answer_is_complete(self) -> None:
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        complete = "## 开篇\n" + ("这是一段完整的研究综述。[1]\n" * 500) + "## 小结\n综上，文章完整收束。[1]"
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=complete) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        self.assertEqual(review, complete)
        self.assertEqual(cc.call_count, 1)
        self.assertFalse(cc.call_args.kwargs["allow_reasoning_fallback"])

    def test_research_review_expands_short_complete_draft_and_replaces_conclusion(self) -> None:
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        short = "## 研究综述\n" + ("这是已有但仍显单薄的分析段落。[1]\n" * 180) + "## 小结\n这是应被替换的旧小结。[1]"
        expansion = (
            "## 补充分析\n"
            + ("这里依据原文补充新的分析层次，避免重复已有论述。[1]\n" * 220)
            + "## 小结\n综上，扩写后的全文已经完整收束。[1]"
        )
        with mock.patch.object(
            app_module.AI_CLIENT, "chat_complete", side_effect=[short, expansion]
        ) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        first_prompt = cc.call_args_list[0].args[0][1]["content"]
        self.assertIn("优先在本轮一次完整写完", first_prompt)
        self.assertIn("达到 5000 个中文汉字以上", first_prompt)
        self.assertEqual(cc.call_count, 2)
        self.assertNotIn("这是应被替换的旧小结", review)
        self.assertEqual(review.count("## 小结"), 1)
        self.assertGreaterEqual(
            app_module.AI_CLIENT._research_review_cjk_chars(review),
            ai_module.RESEARCH_REVIEW_MIN_CJK_CHARS,
        )
        self.assertIn("低于约 5600 字的目标", cc.call_args_list[1].args[0][1]["content"])
        self.assertTrue(cc.call_args_list[1].kwargs["disable_thinking"])

    def test_research_review_retries_when_reasoning_leaks(self) -> None:
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        leaked = "思考过程：我需要先分析材料，然后再写正文。"
        repaired = "【综述正文开始】\n## 研究综述\n" + ("这是一段正式综述正文。[1]\n" * 500) + "## 小结\n综上，文章自然完成。[1]\n【综述正文结束】"
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", side_effect=[leaked, repaired]) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        self.assertNotIn("思考过程", review)
        self.assertNotIn("【综述正文开始】", review)
        self.assertIn("## 研究综述", review)
        self.assertIn("## 小结", review)
        self.assertEqual(cc.call_count, 2)

    def test_research_review_retries_lower_budget_when_high_budget_is_rejected(self) -> None:
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        complete = "## 研究综述\n" + ("这是一段完整的研究综述。[1]\n" * 500) + "## 小结\n综上，文章自然完成。[1]"
        with mock.patch.object(
            app_module.AI_CLIENT,
            "chat_complete",
            side_effect=[app_module.AIServiceError("max_tokens exceeds limit"), complete],
        ) as cc:
            review = app_module.AI_CLIENT.generate_research_review("研究论题", passages)
        self.assertEqual(review, complete)
        self.assertEqual(cc.call_count, 2)
        self.assertEqual(cc.call_args_list[0].kwargs["max_tokens"], ai_module.RESEARCH_REVIEW_MAX_TOKENS)
        self.assertEqual(cc.call_args_list[1].kwargs["max_tokens"], 65536)

    def test_research_review_passes_per_call_http_timeout(self) -> None:
        # 每次模型调用都按 min(研究专用超时, 剩余总预算) 压一个 HTTP 超时。生成跑在 SSE 心跳保活线程里、
        # 已与 CF ~100s 解耦，故该超时刻意宽于全局 120s（用 RESEARCH_REVIEW_CALL_TIMEOUT_SECONDS）。
        passages = [{"index": 1, "citation": "《测试文献》第1页", "text": "生产力与生产关系的材料。"}]
        complete = "## 研究综述\n" + ("这是一段完整的研究综述。[1]\n" * 420) + "## 小结\n综上，文章自然完成。[1]"
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
        # Keep the draft above the 5000-CJK content floor so this test isolates
        # the follow-up time-budget decision rather than the length validator.
        incomplete = "## 研究综述\n" + ("这是一段尚未收尾的正文。[1]\n" * 600)
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

    def test_emits_visible_progress_first_then_done_payload(self) -> None:
        chunks = self._collect(
            lambda cancel_event: "REVIEW_MD",
            lambda result, error: {"ok": True, "md": result, "err": error},
        )
        self.assertTrue(chunks[0].startswith("event: progress"))  # 可见进度也能抢在 CF 计时前
        self.assertTrue(chunks[-1].startswith("event: done"))  # 末尾是 done 事件
        payload = _parse_sse_text("".join(chunks))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["md"], "REVIEW_MD")
        self.assertIsNone(payload["err"])

    def test_slow_fn_exception_passed_to_finalize(self) -> None:
        # 生成抛错(如超时)不弄断流：异常转交 finalize 决定兜底，仍吐一个干净的 done 事件。
        seen = {}

        def _boom(cancel_event):
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

        chunks = self._collect(lambda cancel_event: "x", _finalize)
        self.assertTrue(chunks[-1].startswith("event: error"))
        self.assertFalse(_parse_sse_text("".join(chunks))["ok"])


class AssociativeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.set_setting("access_policy", REGISTERED_FULL)
        # 每个用例前把研究型每周额度重置为默认（空＝默认 30 等），避免设额度的用例污染其他用例。
        app_module.set_setting(app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY, {})
        # 「研究次数限制」总开关默认关；本类多数用例测的正是「按次数拦截」，故在 setUp 里默认打开，
        # 让这些用例的前提成立（单独测「关＝不限次数、纯 token 计量」的用例会自行置 False）。
        app_module.set_setting(app_module.RESEARCH_COUNT_LIMIT_ENABLED_SETTING_KEY, True)
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
        create_manual_subscription(user_email=email, plan_code="support_basic", note="test")
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
        resp = self.client.post(
            "/api/ai/search-chat", json=payload, headers={"X-CSRF-Token": token}
        )
        _drain(resp)
        return resp

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
            app_module.corpus, "locate_associative", return_value=[]
        ), mock.patch.object(
            app_module.AI_CLIENT,
            "expand_associative_query",
            side_effect=app_module.AIServiceError("down"),
        ):
            resp = self._post({"gist": "zzqxyvbnmqwlk", "mode": "locate", "scope": "all"}, token)
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
        verified = {
            "answer_markdown": "综述正文 [1]",
            "status": "verified",
            "issues": [],
            "used_indices": [1],
        }
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", return_value="综述正文 [1]") as rev, \
             mock.patch.object(app_module, "_repair_research_answer", return_value=verified), \
             mock.patch.object(app_module, "_grounded_answer_underuses_evidence", return_value=False):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
            _drain(resp)  # SSE 惰性消费：必须在 mock 作用域内读完，否则综述走真实 AI 而非 mock
        data = _read_result(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_mode"], "research_review")
        self.assertEqual(data["review_markdown"], "综述正文 [1]")
        self.assertGreaterEqual(len(data["review_citations"]), 1)
        # 接地：引文条全部来自真实命中（真实出处非空即可；部分书库引文全名以作者名开头，
        # 如「习近平：《…》」，不能要求一律以「《」起头）
        self.assertTrue(all(str(c["citation"]).strip() for c in data["review_citations"]))
        rev.assert_called_once()

    def test_research_mode_emits_keepalive_before_query_expansion(self) -> None:
        """Cloudflare must receive a byte before any potentially slow preprocessing call."""
        self._login_member("assoc-early-heartbeat@example.test")
        token = self._csrf()
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query") as expand_mock:
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
            first_chunk = next(iter(resp.response))
            self.assertIn("event: progress", first_chunk.decode("utf-8"))
            expand_mock.assert_not_called()
            resp.close()

    def test_research_pipeline_waits_for_a_worker_instead_of_failing_immediately(self) -> None:
        gate = mock.Mock()
        gate.acquire.side_effect = [False, True]
        cancel_event = app_module.threading.Event()
        with mock.patch.object(app_module, "_RESEARCH_PIPELINE_SEMAPHORE", gate), \
             mock.patch.object(app_module, "_RESEARCH_QUEUE_WAIT_SECONDS", 3.0):
            self.assertTrue(app_module._acquire_research_pipeline_slot(cancel_event))
        self.assertEqual(gate.acquire.call_count, 2)

    def test_research_pipeline_queue_stops_waiting_after_disconnect(self) -> None:
        gate = mock.Mock()
        cancel_event = app_module.threading.Event()
        cancel_event.set()
        with mock.patch.object(app_module, "_RESEARCH_PIPELINE_SEMAPHORE", gate):
            self.assertFalse(app_module._acquire_research_pipeline_slot(cancel_event))
        gate.acquire.assert_not_called()

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
            _drain(resp)  # SSE 惰性消费：在 mock 作用域内读完
        data = _read_result(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_mode"], "research_review")
        self.assertEqual(data["verification"]["status"], "evidence_only")
        self.assertIn("## 可核验原文", data["review_markdown"])
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
            _drain(resp)
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
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
            _drain(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(_read_result(resp)["ok"])
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
        verified = {
            "answer_markdown": "综述 [1]",
            "status": "verified",
            "issues": [],
            "used_indices": [1],
        }
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", return_value="综述 [1]"), \
             mock.patch.object(app_module, "_repair_research_answer", return_value=verified), \
             mock.patch.object(app_module, "_grounded_answer_underuses_evidence", return_value=False):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
            _drain(resp)  # SSE 惰性消费：在 mock 作用域内读完
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

    def test_research_count_limit_off_means_unlimited(self) -> None:
        # 总开关关闭（默认）：即便免费周额=0，研究综述也不按次数拦截，改为纯 token 额度计量。
        email = "assoc-count-off@example.test"
        self._login_member(email)
        app_module.set_setting(app_module.RESEARCH_COUNT_LIMIT_ENABLED_SETTING_KEY, False)
        app_module.set_setting(
            app_module.RESEARCH_WEEKLY_QUOTA_SETTING_KEY,
            {"registered": 0, "monthly": 0, "quarterly": 0, "yearly": 0},
        )
        token = self._csrf()
        _book, sample = _corpus_sample(min_len=18)
        plan = {"intent": "research", "quotes": [sample], "keywords": [sample[0:2], sample[8:10]]}
        verified = {
            "answer_markdown": "综述 [1]",
            "status": "verified",
            "issues": [],
            "used_indices": [1],
        }
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", return_value="综述 [1]"), \
             mock.patch.object(app_module, "_repair_research_answer", return_value=verified), \
             mock.patch.object(app_module, "_grounded_answer_underuses_evidence", return_value=False):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
            _drain(resp)  # SSE 惰性消费：在 mock 作用域内读完
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_mode"], "research_review")
        self.assertTrue(data["research_quota"]["allowed"])
        self.assertTrue(data["research_quota"]["unlimited"])
        self.assertIsNone(data["research_quota"]["limit"])

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
        verified = {
            "answer_markdown": "综述 [1]",
            "status": "verified",
            "issues": [],
            "used_indices": [1],
        }
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", return_value="综述 [1]"), \
             mock.patch.object(app_module, "_repair_research_answer", return_value=verified), \
             mock.patch.object(app_module, "_grounded_answer_underuses_evidence", return_value=False):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
            _drain(resp)  # SSE 惰性消费：在 mock 作用域内读完
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
            _drain(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(_read_result(resp)["ok"])
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
        data = _read_result(resp)
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

    def test_ungrounded_chat_keeps_proposed_blockquote_wording(self) -> None:
        """关闭引文检索时，模型常用 Markdown 引用块突出“可以写成”的示例句。

        这些句子是用户要的拟写结果，不是本站已核验引文；应去掉引用样式但保留文字。
        """
        self._login_member("chat-ungrounded-proposal@example.test")
        token = self._csrf()
        generated = (
            "### 建议的过渡句写法\n"
            "如果只补一句，可以写成：\n\n"
            "> 理论只有转化为实践，才能成为改造现实的力量。\n\n"
            "或者更贴合原文风格：\n\n"
            "> 统一的评价尺度，只有在具体工作实践中得到贯彻，才能真正发挥作用。\n\n"
            "这样，两段之间的逻辑就连起来了。"
        )
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=generated):
            resp = self._post_chat(
                {"question": "请写两个过渡句", "grounding": False}, token
            )
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
        self.assertTrue(data["ok"])
        answer = data["answer_markdown"]
        self.assertIn("理论只有转化为实践", answer)
        self.assertIn("统一的评价尺度", answer)
        self.assertIn("两段之间的逻辑就连起来了", answer)
        self.assertFalse(any(line.lstrip().startswith(">") for line in answer.splitlines()))

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
            plan_weekly = int(get_plan("support_basic")["weekly_token_limit"])
            self.assertEqual(mem["daily_limit"], plan_weekly // factor)
            self.assertEqual(mem["weekly_limit"], plan_weekly)
            admin = app_module._effective_ai_limit_info({"id": muid, "role": "admin"})
            self.assertIsNone(admin["weekly_limit"])  # 管理员不限

    def test_registered_weekly_token_cap_blocks_chat(self) -> None:
        # 弹性额度：硬上限是本周(=每日×7)。本周累计达上限 → 随心问 429，且不调用 AI。
        email = "tok-registered-block@example.test"
        uid = self._login_plain(email)
        weekly_cap = app_module.REGISTERED_FREE_AI_WEEKLY_LIMIT
        record_ai_usage(
            user_id=uid, day=app_module.china_day_text(), feature="search-chat",
            total_tokens=weekly_cap, success=True,
        )
        token = self._csrf()
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat") as ans_mock:
            resp = self._post_chat({"question": "什么是商品拜物教？"}, token)
        self.assertEqual(resp.status_code, 429)
        ans_mock.assert_not_called()

    def test_mascot_tokens_do_not_consume_shared_quota(self) -> None:
        # 马克思形象＝「无限量基础服务」：它自身不过额度闸，其 token 也不该吃掉随心问/研究/导学
        # 共用的周额度池。这里记满一整周上限的 mascot 用量，随心问仍须照常放行、徽章已用量为 0。
        email = "tok-mascot-exempt@example.test"
        uid = self._login_plain(email)
        weekly_cap = app_module.REGISTERED_FREE_AI_WEEKLY_LIMIT
        record_ai_usage(
            user_id=uid, day=app_module.china_day_text(), feature="mascot",
            total_tokens=weekly_cap * 3, success=True,
        )
        token = self._csrf()
        fake = mock.Mock()
        fake.answer_markdown = "回答"
        fake.to_dict = mock.Mock(return_value={"ok": True, "answer_markdown": "回答", "sources": []})
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat", return_value=fake):
            resp = self._post_chat({"question": "什么是剩余价值？"}, token)
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
        self.assertTrue(data["ok"])
        self.assertEqual(data["ai_token_quota"]["limit"], weekly_cap)
        # 已用量只含本次随心问自身的记账，吉祥物那三周的量不计入。
        self.assertLess(data["ai_token_quota"]["used"], weekly_cap)
        # 后台用量总览仍看得到吉祥物的真实消耗（只在额度统计里排除，不是不记）。
        self.assertGreaterEqual(
            get_ai_token_usage(day=app_module.china_day_text(), user_id=uid), weekly_cap * 3
        )

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
        self.assertTrue(_read_result(resp)["ok"])

    def test_registered_user_within_limit_returns_token_quota(self) -> None:
        # 未超额 → 随心问成功并回带 ai_token_quota（limit=本周=每日×7、剩余<上限、未耗尽）。
        email = "tok-registered-ok@example.test"
        self._login_plain(email)
        weekly_cap = app_module.REGISTERED_FREE_AI_WEEKLY_LIMIT
        token = self._csrf()
        fake = mock.Mock()
        fake.answer_markdown = "回答"
        fake.to_dict = mock.Mock(return_value={"ok": True, "answer_markdown": "回答", "sources": []})
        with mock.patch.object(app_module.AI_CLIENT, "answer_search_chat", return_value=fake):
            resp = self._post_chat({"question": "什么是剩余价值？"}, token)
        self.assertEqual(resp.status_code, 200)
        data = _read_result(resp)
        self.assertIn("ai_token_quota", data)
        self.assertFalse(data["ai_token_quota"]["unlimited"])
        self.assertEqual(data["ai_token_quota"]["limit"], weekly_cap)  # 硬上限＝本周
        self.assertEqual(
            data["ai_token_quota"]["daily_limit"],
            weekly_cap // app_module.AI_TOKEN_WEEKLY_FACTOR,
        )  # 每日参考
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
        self.assertTrue(_read_result(resp)["ai_token_quota"]["unlimited"])

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

        def _fake_review(topic, passages, should_cancel=None):
            text = passages[0]["text"] if passages else ""
            chunks = app_module._sentence_chunks(text)
            sent = chunks[0] if chunks else text[:30]
            return f"## 研究综述\n马克思指出「{sent}」[1]。\n## 小结\n完。"

        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value=plan), \
             mock.patch.object(app_module.AI_CLIENT, "generate_research_review", side_effect=_fake_review):
            resp = self._post({"gist": "研究论题", "mode": "research"}, token)
            _drain(resp)  # SSE 惰性消费：在 mock 作用域内读完
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
        self.assertEqual(get_ai_credit_balances(uid), {"research": 0, "chat": 0, "reader": 0})
        grant_ai_credits(uid, research=10, chat=20, reason="test")
        self.assertEqual(get_ai_credit_balances(uid), {"research": 10, "chat": 20, "reader": 0})
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
        self.assertEqual(result["credits"], {"research": 10, "chat": 20, "reader": 0})
        self.assertEqual(get_ai_credit_balances(uid), {"research": 10, "chat": 20, "reader": 0})

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

    def test_squeeze_cjk_line_joins_only_between_chinese(self) -> None:
        # PDF 按物理行抽取 → 中文句内多出空格；只合并中文之间的，中英文/数字之间的空格保持原样。
        squeeze = app_module._squeeze_cjk_line_joins
        self.assertEqual(
            squeeze("人的本质不是 单个人所固有的抽象物，在其现实性上，它是 一切社会关系的总和。"),
            "人的本质不是单个人所固有的抽象物，在其现实性上，它是一切社会关系的总和。",
        )
        self.assertEqual(squeeze("马克思 1844 年手稿 中 Grundrisse 一词"), "马克思 1844 年手稿中 Grundrisse 一词")
        self.assertEqual(squeeze(""), "")

    def test_chat_grounding_injects_complete_sentence_passage(self) -> None:
        # 回归：注入模型的原文段必须是「完整句窗口」，而不是语料 ±CTX_PAD 字的半句窗口——
        # 模型只能引它看得见的文字，喂半句就只能引出支离破碎的引文。
        raw = (
            "前段铺垫，尚未进入正题。" * 40
            + "异化劳动使人的类本质变成对人来说是异己的本质。"
            + "工人生产的财富越多，他就越是变成廉价的商品，这一点在原文里说得很完整。"
            + "后段继续申论，仍属同一段落。" * 40
        )
        hit = SimpleNamespace(
            pages=[SimpleNamespace(raw_text=raw, pdf_page=1)],
            source_file="",
            to_dict=lambda: {
                # 语料给的展示窗口：命中词前后各 CTX_PAD 字，两端都硬切在半句上。
                "context": "变成对人来说是异己的本质。[[H]]工人生产的财富越多[[/H]]，他就越是变成廉",
                "citation": "《测试文献》第1卷，第1页。",
                "book": "文集",
                "source_file": "",
                "pdf_pages": [1],
                "printed_pages": ["1"],
            },
        )
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value={}), \
             mock.patch.object(app_module.corpus, "locate_associative", return_value=[hit]), \
             app_module.app.test_request_context():
            passages, citations, _notes, _scope = app_module._build_chat_grounding("异化劳动")

        self.assertEqual(len(passages), 1)
        text = passages[0]["text"]
        self.assertIn("工人生产的财富越多，他就越是变成廉价的商品，这一点在原文里说得很完整。", text)
        self.assertGreater(len(text), 240)          # 远长于旧口径的 ~80 字半句窗口
        self.assertLessEqual(len(text), app_module.CHAT_GROUNDING_CONTEXT_CHARS)
        self.assertRegex(text, r"[。！？；;!?]$")     # 止于句末标点，不断在半句
        self.assertNotIn("[[H]]", text)
        self.assertEqual(citations[0]["grounding_index"], 1)
        self.assertIn("[[H]]", citations[0]["context"])  # 卡片仍带高亮

    def test_chat_grounding_completes_sentence_across_page_boundary(self) -> None:
        # 命中句跨越两页时必须按页序拼接后再切句，不能从第二页页首的半句开始引用。
        hit = SimpleNamespace(
            pages=[
                SimpleNamespace(raw_text="上一句已经结束。这是跨页完整句的前半部分，", pdf_page=10),
                SimpleNamespace(raw_text="也是包含关键命中的后半部分。下一句提供必要语境。", pdf_page=11),
            ],
            source_file="",
        )
        payload = {
            "context": "整句的前半部分，[[H]]也是包含关键命中[[/H]]的后半部分。下一句提供",
            "source_file": "",
            "pdf_pages": [11],
        }
        passage = app_module._chat_grounding_passage_text(hit, payload, "关键命中")
        self.assertIn("这是跨页完整句的前半部分，也是包含关键命中的后半部分。", passage)
        self.assertFalse(passage.startswith("也是包含关键命中"))
        self.assertRegex(passage, r"[。！？!?]$")

    def test_chat_grounding_deduplicates_same_sentence_and_backfills(self) -> None:
        def _hit(raw: str, highlight: str, page: int, citation: str):
            return SimpleNamespace(
                pages=[SimpleNamespace(raw_text=raw, pdf_page=page)],
                source_file=f"test-{page}.pdf",
                to_dict=lambda: {
                    "context": f"前文。[[H]]{highlight}[[/H]]，句子继续到完整句末。后文。",
                    "citation": citation,
                    "book": "文集",
                    "source_file": "",
                    "pdf_pages": [page],
                    "printed_pages": [str(page)],
                },
            )

        duplicate_sentence = "共同命中短语，句子继续到完整句末。"
        hits = [
            _hit("版本甲前文。" + duplicate_sentence + "版本甲后文。", "共同命中短语", 1, "出处甲"),
            _hit("版本乙前文。" + duplicate_sentence + "版本乙后文。", "共同命中短语", 2, "出处乙"),
            _hit("另一个角度。另一条命中短语，形成不同的完整原句。继续分析。", "另一条命中短语", 3, "出处丙"),
        ]
        with mock.patch.object(app_module.AI_CLIENT, "expand_associative_query", return_value={}), \
             mock.patch.object(app_module.corpus, "locate_associative", return_value=hits), \
             app_module.app.test_request_context():
            passages, citations, _notes, _scope = app_module._build_chat_grounding("共同命中短语")

        self.assertEqual(len(passages), 2)
        self.assertEqual([item["index"] for item in passages], [1, 2])
        self.assertEqual([item["grounding_index"] for item in citations], [1, 2])
        self.assertEqual(sum(duplicate_sentence in item["text"] for item in passages), 1)
        self.assertTrue(any("另一条命中短语，形成不同的完整原句。" in item["text"] for item in passages))


class CitationFormatTests(unittest.TestCase):
    """28 个独立格式键、25 刊格式族与后台自定义模板。"""

    def setUp(self) -> None:
        self.corpus = app_module.corpus
        if not self.corpus:
            self.skipTest("corpus unavailable")
        self._saved = dict(self.corpus.citation_templates)

    def tearDown(self) -> None:
        self.corpus.citation_templates = self._saved

    def _std_vol_page(self):
        vol = next((v for v in self.corpus.books.get("文集", []) if v.volume == 1), None)
        if not vol:
            self.skipTest("文集 vol1 unavailable")
        page = next(
            (p for p in vol.pages if p.printed_page and not str(p.printed_page).startswith("pre-")),
            None,
        )
        if not page:
            self.skipTest("no printed page")
        return vol, page

    def test_to_dict_carries_all_registered_formats(self) -> None:
        from citation_styles import CITATION_FORMAT_KEYS
        book, sub = _corpus_sample()
        hits = self.corpus.locate_quote(sub, allow_fuzzy=False)
        self.assertTrue(hits)
        d = hits[0].to_dict()
        self.assertEqual(set(d["citations"]), set(CITATION_FORMAT_KEYS))
        self.assertEqual(len(d["citations"]), 28)
        self.assertTrue(d["citation"].startswith("《"))  # 向后兼容：默认仍是脚注体例

    def test_default_templates_match_procedural(self) -> None:
        vol, page = self._std_vol_page()
        from search import DEFAULT_CITATION_TEMPLATES as D, _CiteSafeDict
        parts = self.corpus._citation_parts("文集", 1, [page], source_file=vol.source_file)
        self.assertEqual(
            D["gb2015"].format_map(_CiteSafeDict(parts)),
            self.corpus._make_citation_gb("文集", 1, [page], source_file=vol.source_file),
        )
        self.assertEqual(
            D["zgshkx"].format_map(_CiteSafeDict(parts)),
            self.corpus._make_citation("文集", 1, [page], source_file=vol.source_file),
        )

    def test_gb_and_journal_shapes(self) -> None:
        vol, page = self._std_vol_page()
        d = self.corpus._make_citations("文集", 1, [page], source_file=vol.source_file)
        self.assertIn("[M]", d["gb2015"])
        self.assertNotIn("《", d["gb2015"])
        self.assertTrue(d["zgshkx"].startswith("《"))
        self.assertEqual(d["zgshkx"], d["mkszyj"])  # 两刊当前同源

    def test_twenty_five_journal_golden_families(self) -> None:
        from citation_styles import CITATION_STYLE_BY_KEY, JOURNAL_STYLE_KEYS
        vol, page = self._std_vol_page()
        pg = str(page.printed_page).upper()
        d = self.corpus._make_citations("文集", 1, [page], source_file=vol.source_file)
        self.assertEqual(len(JOURNAL_STYLE_KEYS), 25)
        self.assertEqual(d["mkszyj"], f"《马克思恩格斯文集》第1卷，北京：人民出版社，2009年，第{pg}页。")
        self.assertEqual(d["qs"], f"《马克思恩格斯文集》（第1卷），北京：人民出版社，2009年，第{pg}页。")
        self.assertEqual(d["gwlldx"], f"《马克思恩格斯文集》第1卷，人民出版社2009年版，第{pg}页。")
        self.assertEqual(d["zgdsyj"], f"《马克思恩格斯文集》第1卷，人民出版社，2009年，第{pg}页。")
        self.assertEqual(d["mkszyyxs"], f"《马克思恩格斯文集》第1卷第{pg}页。")
        self.assertEqual(d["jxyyj"], f"《马克思恩格斯文集》，第1卷，人民出版社，2009年，第{pg}页。")
        self.assertEqual(d["zgtsshzyyj"], f"马克思恩格斯文集：第1卷[M].北京：人民出版社，2009：{pg}.")
        self.assertEqual(d["sxllyjdk"], f"马克思恩格斯文集，第1卷[M].北京：人民出版社，2009:{pg}.")
        self.assertEqual(d["llsy"], f"马克思，恩格斯.马克思恩格斯文集：第1卷[M].北京：人民出版社，2009：{pg}.")
        self.assertEqual(d["jjllyjjgl"], f"马克思，恩格斯，2009：《马克思恩格斯文集》第1卷，北京：人民出版社，第{pg}页。")
        family_golden = {
            "full_footnote": d["mkszyj"],
            "parenthesized_volume_footnote": d["qs"],
            "publisher_year_footnote": d["gwlldx"],
            "separated_footnote": d["zgdsyj"],
            "compact_footnote": d["mkszyyxs"],
            "edition_compact_footnote": f"《马克思恩格斯文集》第1卷第{pg}页。",
            "comma_segmented_footnote": d["jxyyj"],
            "colon_volume_bibliography": d["zgtsshzyyj"],
            "comma_volume_bibliography": d["sxllyjdk"],
            "responsible_bibliography": d["llsy"],
            "author_year": d["jjllyjjgl"],
        }
        for key in JOURNAL_STYLE_KEYS:
            self.assertEqual(d[key], family_golden[CITATION_STYLE_BY_KEY[key].family], key)

    def test_registry_alias_codes_groups_and_approval_counts(self) -> None:
        from citation_styles import (
            CITATION_STYLE_BY_KEY, CITATION_STYLE_ALIASES, JOURNAL_STYLE_KEYS, flat_style_options,
        )
        self.assertEqual(len(JOURNAL_STYLE_KEYS), 25)
        self.assertEqual(CITATION_STYLE_ALIASES["中特研究"], "zgtsshzyyj")
        self.assertEqual(CITATION_STYLE_BY_KEY["zgtsshzyyj"].label, "《中国特色社会主义研究》")
        self.assertEqual(CITATION_STYLE_BY_KEY["zgtsshzyyj"].subject_codes, ("A81", "G641", "D64"))
        self.assertEqual(len(flat_style_options(gb2025_approved=False)), 27)
        self.assertEqual(len(flat_style_options(gb2025_approved=True)), 28)

    def test_missing_publication_fields_are_omitted_not_guessed(self) -> None:
        from citation_styles import DEFAULT_CITATION_TEMPLATES, add_publication_segments
        from search import _CiteSafeDict
        parts = add_publication_segments({
            "title": "测试著作", "volume_segment": "第1卷", "volume_parenthesized": "（第1卷）",
            "volume_colon": "：第1卷", "volume_comma": "，第1卷", "place": "", "publisher": "",
            "year": "xxxx", "edition_suffix": "", "page": "第9页", "page_range": "9", "page_note": "",
            "responsibility_cn": "马克思.", "responsibility_author_year": "马克思，", "responsibility_tail_cn": "",
        })
        footnote = DEFAULT_CITATION_TEMPLATES["mkszyj"].format_map(_CiteSafeDict(parts))
        author_year = DEFAULT_CITATION_TEMPLATES["jjllyjjgl"].format_map(_CiteSafeDict(parts))
        self.assertEqual(footnote, "《测试著作》第1卷，第9页。")
        self.assertEqual(author_year, "马克思：《测试著作》第1卷，第9页。")
        self.assertNotIn("xxxx", footnote + author_year)

    def test_edition_single_volume_and_non_volume_units_are_structured(self) -> None:
        second = next(iter(self.corpus.books.get("全集二版", [])), None)
        if second and second.pages:
            d = self.corpus._make_citations("全集二版", second.volume, [second.pages[0]], source_file=second.source_file)
            self.assertIn(f"《马克思恩格斯全集》第2版第{second.volume}卷", d["ddsjyshzy"])
            self.assertNotIn("（第二版）第2版", d["ddsjyshzy"])

        single_cfg = next((cfg for cfg in self.corpus.book_configs if cfg.single_volume and self.corpus.books.get(cfg.key)), None)
        if single_cfg:
            vol = self.corpus.books[single_cfg.key][0]
            parts = self.corpus._citation_parts(single_cfg.key, vol.volume, [vol.pages[0]], source_file=vol.source_file)
            self.assertEqual(parts["volume_segment"], "")

        unit_cfg = next((cfg for cfg in self.corpus.book_configs if cfg.volume_unit != "卷" and self.corpus.books.get(cfg.key)), None)
        if unit_cfg:
            vol = self.corpus.books[unit_cfg.key][0]
            parts = self.corpus._citation_parts(unit_cfg.key, vol.volume, [vol.pages[0]], source_file=vol.source_file)
            self.assertEqual(parts["volume_segment"], f"第{vol.volume}{unit_cfg.volume_unit}")

    def test_page_ranges_and_missing_printed_page_flow_to_all_families(self) -> None:
        from search import Page
        pages = [
            Page(pdf_page=1, printed_page="123", raw_text="", norm_text=""),
            Page(pdf_page=2, printed_page="124", raw_text="", norm_text=""),
        ]
        d = self.corpus._make_citations("文集", 1, pages, source_file="pdfs/文集/马克思恩格斯文集第1卷.pdf")
        self.assertIn("第123-124页", d["mkszyj"])
        self.assertIn(":123-124.", d["sxllyjdk"])

        disjoint = [
            Page(pdf_page=1, printed_page="123", raw_text="", norm_text=""),
            Page(pdf_page=3, printed_page="126", raw_text="", norm_text=""),
        ]
        d2 = self.corpus._make_citations("文集", 1, disjoint, source_file="pdfs/文集/马克思恩格斯文集第1卷.pdf")
        self.assertIn("第123页；第126页", d2["mkszyj"])
        self.assertIn("123；126", d2["zgtsshzyyj"])

        missing = [Page(pdf_page=9, printed_page=None, raw_text="", norm_text="")]
        d3 = self.corpus._make_citations("文集", 1, missing, source_file="pdfs/文集/马克思恩格斯文集第1卷.pdf")
        self.assertIn("此为PDF页码，非原书印刷页码", d3["mkszyj"])
        self.assertIn("此为PDF页码，非原书印刷页码", d3["jjllyjjgl"])

    def test_custom_template_overrides_single_format(self) -> None:
        vol, page = self._std_vol_page()
        self.corpus.set_citation_templates(
            {"mkszyj": "{title}（第{volume}卷），{publisher}{year}年版，第{page_range}页。"}
        )
        d = self.corpus._make_citations("文集", 1, [page], source_file=vol.source_file)
        self.assertIn("年版", d["mkszyj"])
        self.assertNotIn("年版", d["zgshkx"])  # 中国社科未受影响
        self.assertIn("[M]", d["gb2015"])      # 国标未受影响

    def test_bad_template_falls_back(self) -> None:
        vol, page = self._std_vol_page()
        self.corpus.set_citation_templates({"gb2015": "{title}:{"})  # 未闭合大括号→渲染抛错
        d = self.corpus._make_citations("文集", 1, [page], source_file=vol.source_file)
        self.assertEqual(
            d["gb2015"],
            self.corpus._make_citation_gb("文集", 1, [page], source_file=vol.source_file),
        )

    def test_authoritative_documents_ignore_templates(self) -> None:
        special = None
        for key in ("五年规划", "历次党代会报告", "历届全会公报"):
            vols = self.corpus.books.get(key) or []
            if vols:
                special = (key, vols[0])
                break
        if not special:
            self.skipTest("no special-citation book available")
        key, vol = special
        if not vol.pages:
            self.skipTest("special vol has no pages")
        page = vol.pages[len(vol.pages) // 2]
        self.corpus.set_citation_templates({"gb2015": "X{title}X[M]."})
        d = self.corpus._make_citations(key, vol.volume, [page], source_file=vol.source_file)
        self.assertEqual(
            d["gb2015"],
            self.corpus._make_citation_gb(key, vol.volume, [page], source_file=vol.source_file),
        )
        self.assertFalse(d["gb2015"].startswith("X"))  # 自定义模板未生效

    def test_editor_rows_and_loader_drops_default(self) -> None:
        from citation_styles import CITATION_FORMAT_KEYS
        rows = app_module._citation_formats_editor()
        self.assertEqual([r["key"] for r in rows], list(CITATION_FORMAT_KEYS))
        self.assertTrue(all(r["source_url"] and r["reviewed_at"] and r["family"] for r in rows))
        app_module.set_setting(
            "citation_formats", {"zgshkx": app_module.DEFAULT_CITATION_TEMPLATES["zgshkx"]}
        )
        try:
            self.assertEqual(app_module._load_citation_formats(), {})  # 与默认相同→丢弃
        finally:
            app_module.set_setting("citation_formats", {})


class ScopeRoutingTests(unittest.TestCase):
    """检索范围（著作群语义路由）：book_scope 定向召回 + 语义检测 + 参数解析。

    解决「问总书记却检索起马恩」：AI 判 corpus + 输入标志词 → 著作群范围 → 定向检索（限定+兜底回填）。
    """

    def test_book_scope_restricts_locate_associative(self) -> None:
        # book_scope 限定后，命中只来自范围内书库——从根上避免其它作者的强命中霸榜。
        corpus = app_module.corpus
        xi_books = app_module._scope_books("xi")
        if not xi_books:
            self.skipTest("本地语料缺习近平著作群")
        res = corpus.locate_associative(
            quotes=[], keywords=["中华民族", "伟大复兴", "现代化"], fragments=[], chapter_keywords=[],
            book_scope=xi_books,
        )
        self.assertTrue(res, "习近平著作群内应有相关命中")
        stray = sorted({h.book for h in res} - xi_books)
        self.assertFalse(stray, f"限定范围后不应出现范围外书库：{stray}")

    def test_book_scope_none_searches_all(self) -> None:
        # 不限定（None）时保持全库检索的原有行为（向后兼容）。
        corpus = app_module.corpus
        scoped = corpus.keyword_cooccurrence(["国家", "革命"], book_scope={"列宁全集"})
        allb = corpus.keyword_cooccurrence(["国家", "革命"])
        self.assertTrue(all(h.book == "列宁全集" for h in scoped))
        self.assertGreaterEqual(len(allb), len(scoped))

    def test_scope_books_subset_of_corpus(self) -> None:
        corpus_books = set(app_module.corpus.books)
        for sid in app_module._CORPUS_SCOPE_BY_ID:
            self.assertLessEqual(app_module._scope_books(sid), corpus_books, sid)

    def test_detect_scope_xi_from_hints(self) -> None:
        if not app_module._scope_books("xi"):
            self.skipTest("本地语料缺习近平著作群")
        self.assertEqual(app_module._detect_scope("总书记如何理解中华民族伟大复兴", {}), "xi")
        self.assertEqual(app_module._detect_scope("中国式现代化的本质要求是什么", {}), "xi")

    def test_detect_scope_marx_from_hints(self) -> None:
        if not app_module._scope_books("marx_engels"):
            self.skipTest("本地语料缺马恩著作群")
        self.assertEqual(app_module._detect_scope("剩余价值与劳动异化的关系", {}), "marx_engels")

    def test_detect_scope_ai_corpus_signal(self) -> None:
        # AI 明确点名 corpus（权重更高）也能把弱信号输入路由过去。
        if not app_module._scope_books("xi"):
            self.skipTest("本地语料缺习近平著作群")
        self.assertEqual(app_module._detect_scope("谈谈发展这个问题", {"corpus": ["习近平"]}), "xi")

    def test_detect_scopes_keeps_both_sides_of_comparison(self) -> None:
        # 比较研究不能再选中一方、丢掉另一方；“马恩”简称也应被识别。
        if not (app_module._scope_books("marx_engels") and app_module._scope_books("lenin")):
            self.skipTest("本地语料缺马恩或列宁著作群")
        self.assertEqual(
            app_module._detect_scopes("比较马恩国家观与列宁国家观的异同", {}),
            ["marx_engels", "lenin"],
        )

    def test_detect_scopes_respects_multi_corpus_plan(self) -> None:
        if not (app_module._scope_books("marx_engels") and app_module._scope_books("lenin")):
            self.skipTest("本地语料缺马恩或列宁著作群")
        self.assertEqual(
            app_module._detect_scopes("比较两种国家学说", {"corpus": ["马克思恩格斯", "列宁"]}),
            ["marx_engels", "lenin"],
        )

    def test_chinese_modernization_expands_to_china_corpus_bundle(self) -> None:
        expected = [sid for sid in ("xi", "party_docs", "deng", "mao", "jiang", "hu")
                    if app_module._scope_books(sid)]
        if len(expected) < 2:
            self.skipTest("本地语料缺中国式现代化组合文库")
        detected = app_module._detect_scopes("研究中国式现代化的理论内涵与历史演进", {})
        self.assertEqual(detected, expected)

    def test_detect_scope_ambiguous_returns_none(self) -> None:
        # 无标志词、无 AI 信号 → 不限定（含糊输入不误锁著作群）。
        self.assertIsNone(app_module._detect_scope("请你谈一谈这个看法", {}))

    def test_resolve_manual_scope_hard_restrict(self) -> None:
        if not app_module._scope_books("xi"):
            self.skipTest("本地语料缺习近平著作群")
        books, sid, manual = app_module._resolve_search_scope("xi", "任意输入", {})
        self.assertTrue(manual)
        self.assertEqual(sid, "xi")
        self.assertEqual(books, app_module._scope_books("xi"))

    def test_resolve_all_scope_no_restriction(self) -> None:
        books, sid, manual = app_module._resolve_search_scope("all", "总书记中华民族伟大复兴", {})
        self.assertEqual(books, app_module._public_book_keys())
        self.assertEqual(sid, "all")
        self.assertFalse(manual)

    def test_resolve_auto_detects_and_restricts(self) -> None:
        if not app_module._scope_books("xi"):
            self.skipTest("本地语料缺习近平著作群")
        books, sid, manual = app_module._resolve_search_scope("auto", "总书记如何理解中华民族伟大复兴", {})
        self.assertEqual(sid, "xi")
        self.assertFalse(manual)
        self.assertEqual(books, app_module._scope_books("xi"))

    def test_resolve_auto_no_signal_no_restriction(self) -> None:
        books, sid, manual = app_module._resolve_search_scope("auto", "请你谈一谈这个看法", {})
        self.assertEqual(books, app_module._public_book_keys())
        self.assertEqual(sid, "auto")
        self.assertFalse(manual)

    def test_resolve_auto_comparison_unions_multiple_corpora(self) -> None:
        marx = app_module._scope_books("marx_engels")
        lenin = app_module._scope_books("lenin")
        if not (marx and lenin):
            self.skipTest("本地语料缺马恩或列宁著作群")
        books, sid, manual = app_module._resolve_search_scope(
            "auto", "比较马恩国家观与列宁国家观的异同", {})
        self.assertFalse(manual)
        self.assertEqual(sid, "marx_engels,lenin")
        self.assertEqual(books, marx | lenin)

    def test_scope_options_payload_shape(self) -> None:
        opts = app_module._scope_options_payload()
        ids = [o["id"] for o in opts]
        self.assertEqual(ids[:2], ["auto", "all"])
        self.assertTrue(all("label" in o for o in opts))
        # 至少有一个真实著作群（本地语料非空时）
        self.assertTrue(len(ids) > 2)

    def test_subject_index_respects_scope(self) -> None:
        # 名目索引仅《文集》：限定到习近平著作群时应为空（与词面召回的定向一致）。
        corpus = app_module.corpus
        xi_books = app_module._scope_books("xi")
        if not xi_books or not corpus._subject_entries:
            self.skipTest("本地语料缺习近平著作群或名目索引")
        self.assertEqual(corpus.locate_subject_index(["异化", "劳动"], book_scope=xi_books), [])

    def test_resolve_multi_select_union(self) -> None:
        # 多选著作群 → 书库并集、手动硬限定、结果 id 逗号连接、标签顿号连接。
        xi = app_module._scope_books("xi")
        party = app_module._scope_books("party_docs")
        if not xi or not party:
            self.skipTest("本地语料缺相关著作群")
        books, sid, manual = app_module._resolve_search_scope(["xi", "party_docs"], "任意输入", {})
        self.assertTrue(manual)
        self.assertEqual(books, xi | party)
        self.assertEqual(sid, "xi,party_docs")
        self.assertEqual(app_module._scope_label(sid), "习近平、党和国家文献")

    def test_resolve_multi_with_all_is_unrestricted(self) -> None:
        # 列表里含 all → 明确不限定（不与著作群同时限定，避免歧义）。
        books, sid, manual = app_module._resolve_search_scope(["xi", "all"], "任意", {})
        self.assertEqual(books, app_module._public_book_keys())
        self.assertEqual(sid, "all")
        self.assertFalse(manual)

    def test_resolve_multi_dedup_and_canonical_order(self) -> None:
        # 乱序 + 重复 → 按 CORPUS_SCOPES 定义顺序去重（marx_engels 在 xi 之前）。
        if not (app_module._scope_books("marx_engels") and app_module._scope_books("xi")):
            self.skipTest("本地语料缺相关著作群")
        _books, sid, _m = app_module._resolve_search_scope(["xi", "marx_engels", "xi"], "x", {})
        self.assertEqual(sid, "marx_engels,xi")

    def test_resolve_single_id_still_manual(self) -> None:
        # 单值（字符串）著作群 id 仍按手动硬限定处理（向后兼容旧前端）。
        if not app_module._scope_books("mao"):
            self.skipTest("本地语料缺毛泽东著作群")
        books, sid, manual = app_module._resolve_search_scope("mao", "任意", {})
        self.assertTrue(manual)
        self.assertEqual(sid, "mao")
        self.assertEqual(books, app_module._scope_books("mao"))

    # ---- 指定著作：单本 / 单卷 / 多选并集（D1 卷级 + D-多选 + D2 指定优先）----

    def test_resolve_single_book_hard_restrict(self) -> None:
        if "文集" not in app_module.corpus.books:
            self.skipTest("本地语料缺《文集》")
        books, sid, manual = app_module._resolve_search_scope("book:文集", "任意", {})
        self.assertTrue(manual)
        self.assertEqual(books, {"文集": None})
        self.assertEqual(sid, "book:文集")
        self.assertTrue(app_module._scope_label(sid).startswith("《"))

    def test_resolve_single_volume(self) -> None:
        corpus = app_module.corpus
        if "文集" not in corpus.books or 5 not in {v.volume for v in corpus.get_volumes("文集")}:
            self.skipTest("本地语料缺《文集》第5卷")
        books, sid, manual = app_module._resolve_search_scope("vol:文集:5", "任意", {})
        self.assertTrue(manual)
        self.assertEqual(books, {"文集": {5}})
        self.assertEqual(sid, "vol:文集:5")
        self.assertIn("第 5 卷", app_module._scope_label(sid))

    def test_resolve_whole_book_absorbs_volume(self) -> None:
        # 同本同时给「整套」与「单卷」→ 整套 None 覆盖卷集。
        corpus = app_module.corpus
        vols = {v.volume for v in corpus.get_volumes("文集")}
        if "文集" not in corpus.books or 5 not in vols:
            self.skipTest("本地语料缺《文集》第5卷")
        books, _sid, manual = app_module._resolve_search_scope(
            ["vol:文集:5", "book:文集"], "任意", {})
        self.assertTrue(manual)
        self.assertEqual(books, {"文集": None})

    def test_resolve_cross_book_volume_union(self) -> None:
        corpus = app_module.corpus
        if "文集" not in corpus.books or "全集" not in corpus.books:
            self.skipTest("本地语料缺《文集》或《全集》")
        if 5 not in {v.volume for v in corpus.get_volumes("全集")}:
            self.skipTest("本地语料缺《全集》第5卷")
        books, _sid, manual = app_module._resolve_search_scope(
            ["book:文集", "vol:全集:5"], "任意", {})
        self.assertTrue(manual)
        self.assertEqual(books, {"文集": None, "全集": {5}})

    def test_resolve_specific_book_overrides_group(self) -> None:
        # D2：同时给著作群 id 与单本 token → 只取单本（忽略群）。
        if "文集" not in app_module.corpus.books:
            self.skipTest("本地语料缺《文集》")
        books, sid, _m = app_module._resolve_search_scope(["xi", "book:文集"], "任意", {})
        self.assertEqual(books, {"文集": None})
        self.assertEqual(sid, "book:文集")

    def test_resolve_invalid_volume_dropped(self) -> None:
        # 不存在的卷号被剔除；该本无有效卷 → 回落不限定（None），不至搜出空。
        if "文集" not in app_module.corpus.books:
            self.skipTest("本地语料缺《文集》")
        books, _sid, _m = app_module._resolve_search_scope("vol:文集:999", "任意", {})
        self.assertEqual(books, app_module._public_book_keys())

    def test_scoped_volumes_filters_to_requested_volume(self) -> None:
        corpus = app_module.corpus
        if "文集" not in corpus.books or 5 not in {v.volume for v in corpus.get_volumes("文集")}:
            self.skipTest("本地语料缺《文集》第5卷")
        self.assertEqual({v.volume for v in corpus._scoped_volumes("文集", {"文集": {5}})}, {5})
        # None（整套）→ 返回全部卷（向后兼容）
        self.assertEqual(
            {v.volume for v in corpus._scoped_volumes("文集", {"文集": None})},
            {v.volume for v in corpus.get_volumes("文集")},
        )

    def test_volume_scope_restricts_locate_associative(self) -> None:
        # 卷级 book_scope（dict）落到底层扫描：命中只来自被限定的那一卷。
        corpus = app_module.corpus
        if "文集" not in corpus.books or 5 not in {v.volume for v in corpus.get_volumes("文集")}:
            self.skipTest("本地语料缺《文集》第5卷")
        res = corpus.locate_associative(
            quotes=[], keywords=["生产", "关系", "社会"], fragments=[], chapter_keywords=[],
            book_scope={"文集": {5}},
        )
        stray = sorted({(h.book, h.volume) for h in res
                        if not (h.book == "文集" and h.volume == 5)})
        self.assertFalse(stray, f"限定《文集》第5卷后不应出现范围外命中：{stray}")


class _FakeSseResp:
    """伪装 urllib 流式响应：上下文管理器 + 逐行迭代（SSE data: 行）。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self) -> "_FakeSseResp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def __iter__(self):
        return iter(self._lines)


def _sse_delta_lines(*deltas: dict) -> list[bytes]:
    """把若干 delta dict 编成上游 /chat/completions 的 SSE data: 行（含结尾 [DONE]）。"""
    lines = [
        ("data: " + json.dumps({"choices": [{"delta": d}]}, ensure_ascii=False)).encode("utf-8")
        for d in deltas
    ]
    lines.append(b"data: [DONE]")
    return lines


class ReaderAiStreamAndScopeTests(unittest.TestCase):
    """阅读器 AI 导学两项修复：①思维链只保活不下发（不进答案/历史/额度）②接地问答检索范围 chips。"""

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.set_setting("access_policy", REGISTERED_FULL)
        app_module.set_setting(app_module.AI_TOKEN_DAILY_SETTING_KEY, {})
        app_module._rate_buckets.clear()
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
        create_manual_subscription(user_email=email, plan_code="support_basic", note="test")
        token = self._csrf()
        resp = self.client.post(
            "/login",
            data={"csrf_token": token, "email": email, "password": "correct horse battery staple"},
        )
        self.assertEqual(resp.status_code, 302)

    @staticmethod
    def _any_allowed_source_file() -> str:
        for _book, vols in app_module.corpus.books.items():
            for vol in vols:
                if vol.source_file in app_module.ALLOWED_SOURCE_FILES and vol.pages:
                    return vol.source_file
        raise RuntimeError("corpus has no allowed source file")

    # ---------- ①流式层：思维链不下发 ----------

    def test_stream_drops_reasoning_keeps_content(self) -> None:
        # 推理模型先吐 reasoning_content 再给正文：正文原样流出，思维链绝不混入。
        lines = _sse_delta_lines(
            {"reasoning_content": "好的，用户要求我讲解本页"},
            {"reasoning_content": "……大段自我分析……"},
            {"content": "商品"},
            {"content": "是财富的元素形式。"},
        )
        with mock.patch.object(
            ai_module.urllib_request, "urlopen", return_value=_FakeSseResp(lines)
        ):
            chunks = list(
                app_module.AI_CLIENT.chat_complete_stream(
                    [{"role": "user", "content": "讲讲"}], 200
                )
            )
        self.assertEqual("".join(chunks), "商品是财富的元素形式。")
        self.assertTrue(all("用户要求我" not in c and "自我分析" not in c for c in chunks))

    def test_stream_reasoning_only_retries_with_thinking_disabled(self) -> None:
        # 上游全程没给正文（思考烧光 max_tokens）→ 一个字都没下发，关掉思考重来一次；
        # 读者拿到的是重试得到的正文，思维链绝不当答案下发。
        first = _sse_delta_lines(
            {"reasoning_content": "只有思考"},
            {"reasoning_content": "没有正文"},
        )
        second = _sse_delta_lines({"content": "商品是"}, {"content": "财富的元素形式。"})
        payloads: list[dict] = []

        def _fake_urlopen(req, *args, **kwargs):
            payloads.append(json.loads(req.data.decode("utf-8")))
            return _FakeSseResp(first if len(payloads) == 1 else second)

        # 深思档（pro）：首次开思考、判空后关思考重来。快档 flash 首次就已关思考，测不出这一步。
        with mock.patch.object(ai_module.urllib_request, "urlopen", side_effect=_fake_urlopen), \
                mock.patch.object(app_module.AI_CLIENT, "config",
                                  replace(app_module.AI_CLIENT.config, model="deepseek-v4-pro")):
            chunks = list(
                app_module.AI_CLIENT.chat_complete_stream(
                    [{"role": "user", "content": "讲讲"}], 200
                )
            )
        self.assertEqual("".join(chunks), "商品是财富的元素形式。")
        self.assertTrue(all("只有思考" not in c and "没有正文" not in c for c in chunks))
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0].get("thinking"), {"type": "enabled"})
        self.assertEqual(payloads[1].get("thinking"), {"type": "disabled"})  # 重试关思考

    def test_stream_fast_tier_disables_thinking_upfront(self) -> None:
        # 快档 flash 的流式：首包就关思考，读者不必干等模型「先想两分钟」。
        lines = _sse_delta_lines({"content": "商品"}, {"content": "是财富的元素形式。"})
        payloads: list[dict] = []

        def _fake_urlopen(req, *args, **kwargs):
            payloads.append(json.loads(req.data.decode("utf-8")))
            return _FakeSseResp(lines)

        with mock.patch.object(ai_module.urllib_request, "urlopen", side_effect=_fake_urlopen), \
                mock.patch.object(app_module.AI_CLIENT, "config",
                                  replace(app_module.AI_CLIENT.config, model="deepseek-v4-flash")):
            chunks = list(
                app_module.AI_CLIENT.chat_complete_stream(
                    [{"role": "user", "content": "讲讲"}], 200
                )
            )
        self.assertEqual("".join(chunks), "商品是财富的元素形式。")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].get("thinking"), {"type": "disabled"})

    def test_stream_reasoning_only_twice_raises_instead_of_leaking(self) -> None:
        # 关思考重试后仍只有思维链 → 如实报错，绝不把「用户要求我…」当答案吐给读者。
        lines = _sse_delta_lines({"reasoning_content": "用户要求我讲解本页"})
        with mock.patch.object(
            ai_module.urllib_request, "urlopen", side_effect=lambda *a, **k: _FakeSseResp(list(lines))
        ):
            with self.assertRaises(app_module.AIServiceError):
                list(
                    app_module.AI_CLIENT.chat_complete_stream(
                        [{"role": "user", "content": "讲讲"}], 200
                    )
                )

    def test_stream_reasoning_emits_keepalive_ticks(self) -> None:
        # 思考阶段超过节流间隔 → yield 空串（保活 tick），空串不算正文。
        lines = _sse_delta_lines(
            {"reasoning_content": "思考A"},
            {"reasoning_content": "思考B"},
            {"content": "答"},
        )
        # monotonic 调用序：起点 0.0 → 思考A 时 9.0（≥8 出 tick）→ 思考B 时 9.5（不足再 tick）
        with mock.patch.object(
            ai_module.urllib_request, "urlopen", return_value=_FakeSseResp(lines)
        ), mock.patch.object(ai_module.time, "monotonic", side_effect=[0.0, 9.0, 9.5]):
            chunks = list(
                app_module.AI_CLIENT.chat_complete_stream(
                    [{"role": "user", "content": "讲讲"}], 200
                )
            )
        self.assertEqual(chunks, ["", "答"])

    def test_pdf_chat_instructions_forbid_reasoning_prose(self) -> None:
        # 提示词第 5 条：直接输出讲解正文、不复述任务、不展示思考过程（从源头压缩思维链）。
        text = app_module.AI_CLIENT._pdf_chat_instructions(False, False)
        self.assertIn("5. 直接输出讲解正文", text)
        self.assertIn("思考过程", text)

    def test_pdf_chat_stream_route_translates_ticks_to_sse_comments(self) -> None:
        # 路由层：空串 tick → SSE 注释保活；正文进 delta 与 done，tick 不进答案。
        self._login_member("pdf-stream@example.test")
        token = self._csrf()
        source_file = self._any_allowed_source_file()

        def _fake_stream(messages, max_tokens, provider=None, meta_out=None, web_search_query=None, **kwargs):
            yield ""
            yield "你好"

        with mock.patch.object(
            app_module.AI_CLIENT,
            "prepare_pdf_chat",
            return_value=([{"role": "user", "content": "q"}], 100, [], []),
        ), mock.patch.object(
            app_module.AI_CLIENT, "chat_complete_stream", side_effect=_fake_stream
        ):
            resp = self.client.post(
                "/api/ai/pdf-chat-stream",
                json={"question": "讲讲本页", "source_file": source_file, "page": 1},
                headers={"X-CSRF-Token": token},
            )
            raw = resp.get_data(as_text=True)  # 流式响应需在 patch 上下文内读完
        self.assertEqual(resp.status_code, 200)
        self.assertIn(": keepalive", raw)
        data = _parse_sse_text(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["answer_markdown"], "你好")
        self.assertNotIn('data: {"text": ""}', raw)

    # ---------- ②阅读器接地问答：检索范围 chips ----------

    def test_viewer_renders_scope_chips(self) -> None:
        self._login_member("viewer-scope@example.test")
        source_file = self._any_allowed_source_file()
        resp = self.client.get("/viewer", query_string={"file": source_file, "page": 1})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="aiScopeChips"', html)
        self.assertIn('data-scope="auto"', html)
        self.assertIn('data-scope="all"', html)
        # 与随心问共用同一 localStorage 偏好键；接地请求带用户所选范围
        self.assertIn("marx-ai-scope-v2", html)
        self.assertIn("scope: currentAiScope()", html)
        # 至少渲染出一个真实著作群 chip（本地语料非空时）
        chip_ids = re.findall(r'data-scope="([^"]+)"', html)
        self.assertTrue(len(chip_ids) > 2, chip_ids)


class UngroundedAnswerSanitizerTests(unittest.TestCase):
    def test_flattens_quote_blocks_without_deleting_their_text(self) -> None:
        answer = (
            "### 建议的过渡句写法\n"
            "如果只补一句，可以写成：\n\n"
            "> 理论只有转化为实践，才能成为改造现实的力量。\n\n"
            "或者更贴合原文风格：\n\n"
            ">> 统一的评价尺度，只有在具体工作实践中得到贯彻，才能真正发挥作用。\n\n"
            "这样，两段之间的逻辑就连起来了。"
        )
        cleaned = app_module.AI_CLIENT.sanitize_ungrounded_answer(answer)
        self.assertIn("理论只有转化为实践", cleaned)
        self.assertIn("统一的评价尺度", cleaned)
        self.assertIn("两段之间的逻辑就连起来了", cleaned)
        self.assertFalse(any(line.lstrip().startswith(">") for line in cleaned.splitlines()))

    def test_removes_only_unverified_claim_fragments(self) -> None:
        answer = (
            "马克思在《资本论》中指出：“资本不是物，而是一定的社会关系。”[2] "
            "这一分析仍需结合现实展开。\n"
            "这一判断见第 12 页，但核心分析仍然保留。\n"
            "> [3]"
        )
        cleaned = app_module.AI_CLIENT.sanitize_ungrounded_answer(answer)
        self.assertIn("资本不是物，而是一定的社会关系。", cleaned)
        self.assertIn("这一分析仍需结合现实展开。", cleaned)
        self.assertIn("核心分析仍然保留。", cleaned)
        self.assertNotIn("马克思在《资本论》中指出", cleaned)
        self.assertNotIn("第 12 页", cleaned)
        self.assertNotRegex(cleaned, r"\[\d+\]")
        self.assertNotIn("“", cleaned)
        self.assertNotIn("”", cleaned)

    def test_ungrounded_prompt_forbids_quote_blocks(self) -> None:
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value="普通段落。") as complete:
            app_module.AI_CLIENT.answer_search_chat([], "请改写这句话")
        prompt = complete.call_args.args[0][-1]["content"]
        self.assertIn("不要使用 Markdown `> 引用块`", prompt)
        self.assertIn("示例、改写稿、过渡句或建议文本", prompt)


class GroundedDirectQuoteSanitizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grounding = [{
            "index": 1,
            "citation": "《测试文献》第1页",
            "text": "前一句提供语境。人的本质不是单个人所固有的抽象物，在其现实性上，它是一切社会关系的总和。后一句继续分析。",
        }]

    def test_completes_and_deduplicates_quote_blocks_without_dropping_analysis(self) -> None:
        answer = (
            "### 第一层\n"
            "> 人的本质不是单个人所固有的抽象物[1]\n\n"
            "这里保留第一段解释。\n\n"
            "### 第二层\n"
            "> 人的本质不是单个人所固有的抽象物[1]\n\n"
            "这里保留第二段不同的解释。"
        )
        cleaned = app_module.AI_CLIENT._sanitize_grounded_direct_quotes(answer, self.grounding)
        selected = "人的本质不是单个人所固有的抽象物"
        self.assertEqual(cleaned.count(selected), 1)
        self.assertIn(f"> {selected}[1]", cleaned)
        self.assertIn("这里保留第一段解释。", cleaned)
        self.assertIn("这里保留第二段不同的解释。", cleaned)
        self.assertIn("### 第二层", cleaned)

    def test_completes_inline_quote_and_turns_repeat_into_reference(self) -> None:
        answer = (
            "马克思指出：“人的本质不是单个人所固有的抽象物”[1]，这一判断具有方法论意义。"
            "进一步说，“人的本质不是单个人所固有的抽象物”[1]还要求考察现实关系。"
        )
        cleaned = app_module.AI_CLIENT._sanitize_grounded_direct_quotes(answer, self.grounding)
        selected = "人的本质不是单个人所固有的抽象物"
        self.assertEqual(cleaned.count(selected), 1)
        self.assertIn(f"“{selected}”[1]", cleaned)
        self.assertIn("进一步说，这一论述[1]还要求考察现实关系。", cleaned)
        self.assertIn("这一判断具有方法论意义。", cleaned)

    def test_unmatched_or_uncited_text_is_untouched(self) -> None:
        answer = "普通表述“不是本站原文”。\n\n> 无法核验的引文[9]\n\n分析照常保留。"
        self.assertEqual(
            app_module.AI_CLIENT._sanitize_grounded_direct_quotes(answer, self.grounding),
            answer,
        )


class NonStreamReasoningLeakTests(unittest.TestCase):
    """非流式对话（随心问 /ai 页快速问答、接地问答等）：思维链绝不当正文返回。

    线上事故形态：推理模型把 max_tokens 全烧在思考上（finish_reason=length、content 为空），
    旧实现回退 reasoning_content 当答案 → 读者页面出现「we need answer in Chinese…」的自我分析。
    """

    @staticmethod
    def _resp(content: str = "", reasoning: str = "") -> dict:
        message: dict = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        return {"choices": [{"message": message, "finish_reason": "length" if not content else "stop"}]}

    def test_empty_content_retries_with_thinking_disabled(self) -> None:
        leaked = "we need answer in Chinese, based on provided excerpts, user asks…"
        payloads: list[dict] = []

        def _fake_post(path, payload, **kwargs):
            payloads.append(payload)
            return self._resp("", leaked) if len(payloads) == 1 else self._resp("异化劳动的四重规定……")

        with mock.patch.object(app_module.AI_CLIENT, "_post_json", side_effect=_fake_post):
            answer = app_module.AI_CLIENT.chat_complete(
                [{"role": "user", "content": "谈谈异化劳动"}], max_tokens=6000,
                model="deepseek-v4-pro",  # 深思档：首次开思考，判空后才关思考重试
            )
        self.assertEqual(answer, "异化劳动的四重规定……")
        self.assertNotIn("we need answer", answer)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0].get("thinking"), {"type": "enabled"})
        self.assertEqual(payloads[1].get("thinking"), {"type": "disabled"})

    def test_reasoning_leaked_into_content_retries(self) -> None:
        # 思维链被模型写进 content（另一种形态）：开头即「我们 need answer in Chinese」→ 重试换干净正文。
        leaked = "我们 need answer in Chinese, based on provided excerpts, user asks 谈谈异化劳动。Need structure: four aspects…"
        clean = "### 一、与劳动产品相异化\n马克思在《1844年经济学哲学手稿》中指出……"
        with mock.patch.object(
            app_module.AI_CLIENT, "_post_json", side_effect=[self._resp(leaked), self._resp(clean)]
        ) as post:
            answer = app_module.AI_CLIENT.chat_complete(
                [{"role": "user", "content": "谈谈异化劳动"}], max_tokens=6000,
                model="deepseek-v4-pro",
            )
        self.assertEqual(answer, clean)
        self.assertEqual(post.call_count, 2)

    def test_reasoning_only_twice_raises_instead_of_leaking(self) -> None:
        leaked = "用户要求我用中文回答，需要先梳理四重规定……"
        with mock.patch.object(
            app_module.AI_CLIENT, "_post_json", return_value=self._resp("", leaked)
        ):
            with self.assertRaises(app_module.AIServiceError) as ctx:
                app_module.AI_CLIENT.chat_complete(
                    [{"role": "user", "content": "谈谈异化劳动"}], max_tokens=6000
                )
        self.assertNotIn("用户要求我", str(ctx.exception))

    def test_grounded_answer_uses_relaxed_ceiling_and_falls_back_by_rungs(self) -> None:
        # 接地作答的上限是「思考+正文」合计；已上线版本放宽到 65536，拒绝时逐级回退。
        grounding = [{"index": 1, "citation": "《测试文献》第1页", "text": "异化劳动的材料。"}]
        with mock.patch.object(
            app_module.AI_CLIENT, "chat_complete", return_value="### 正文\n答。"
        ) as cc:
            app_module.AI_CLIENT.answer_search_chat([], "谈谈异化劳动", grounding=grounding)
        self.assertEqual(cc.call_args.kwargs["max_tokens"], ai_module.GROUNDED_ANSWER_MIN_TOKENS)
        self.assertEqual(ai_module.GROUNDED_ANSWER_MIN_TOKENS, 65536)

        with mock.patch.object(
            app_module.AI_CLIENT,
            "chat_complete",
            side_effect=[
                app_module.AIServiceError("max_tokens exceeds limit"),
                app_module.AIServiceError("max_tokens exceeds limit"),
                "### 正文\n答。",
            ],
        ) as cc2:
            app_module.AI_CLIENT.answer_search_chat([], "谈谈异化劳动", grounding=grounding)
        self.assertEqual(
            [c.kwargs["max_tokens"] for c in cc2.call_args_list],
            [65536, ai_module.GROUNDED_ANSWER_MID_TOKENS, ai_module.GROUNDED_ANSWER_FALLBACK_TOKENS],
        )

    def test_fast_tier_disables_thinking_upfront(self) -> None:
        # 快档 flash：首次就关思考——它的思考对质量无增益却常吃光预算（实测线索抽取 16.2s→5.1s、
        # 接地作答 33.4s→15.5s 而正文长度与结构不变），故不必先浪费一次再重试。
        with mock.patch.object(
            app_module.AI_CLIENT, "_post_json", return_value=self._resp("### 结论\n正文。")
        ) as post:
            app_module.AI_CLIENT.chat_complete(
                [{"role": "user", "content": "谈谈异化劳动"}], max_tokens=6000,
                model="deepseek-v4-flash",
            )
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[1].get("thinking"), {"type": "disabled"})

    def test_structured_extraction_forces_no_thinking(self) -> None:
        # 线索抽取/重排是「全有或全无」的 JSON：开思考会把预算烧光、一个字 JSON 都不吐，
        # 故这两步无论走哪个档位都硬性关思考。
        plan = '{"quotes": ["劳动的产品"], "keywords": ["异化"], "fragments": [], "chapter_keywords": []}'
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=plan) as cc:
            app_module.AI_CLIENT.expand_associative_query("异化劳动线索抽取用例")
        self.assertTrue(cc.call_args.kwargs["disable_thinking"])
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value="[]") as cc2:
            app_module.AI_CLIENT.rank_associative_candidates(
                "异化劳动", [{"index": 1, "citation": "《测试》第1页", "context": "材料。"}]
            )
        self.assertTrue(cc2.call_args.kwargs["disable_thinking"])

    def test_normal_answer_costs_no_extra_call(self) -> None:
        # 深思档（pro）正常回答零额外开销：只发一次请求、首次不关思考。
        with mock.patch.object(
            app_module.AI_CLIENT, "_post_json", return_value=self._resp("### 结论\n正文。", "思考若干")
        ) as post:
            answer = app_module.AI_CLIENT.chat_complete(
                [{"role": "user", "content": "谈谈异化劳动"}], max_tokens=6000,
                model="deepseek-v4-pro",
            )
        self.assertEqual(answer, "### 结论\n正文。")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[1].get("thinking"), {"type": "enabled"})


class ResearchTierExpandTests(unittest.TestCase):
    """联想检索的两种缓存档都固定使用站方 Flash 非思考。"""

    PLAN = '{"quotes": ["劳动的产品"], "keywords": ["异化"], "fragments": ["异化劳动"], "chapter_keywords": []}'

    def setUp(self) -> None:
        ai_module._ASSOC_EXPAND_CACHE.clear()

    def tearDown(self) -> None:
        ai_module._ASSOC_EXPAND_CACHE.clear()

    def test_all_associative_tiers_use_site_flash_without_thinking(self) -> None:
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=self.PLAN) as cc:
            app_module.AI_CLIENT.expand_associative_query("研究论题甲", deep=True)
        self.assertEqual(cc.call_args.kwargs["model"], ai_module.ASSOC_EXPAND_DEEP_MODEL)
        self.assertEqual(ai_module.ASSOC_EXPAND_DEEP_MODEL, "deepseek-v4-flash")
        self.assertTrue(cc.call_args.kwargs["disable_thinking"])  # 深档同样关思考
        self.assertEqual(cc.call_args.kwargs["reasoning_effort"], "off")

        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=self.PLAN) as cc2:
            app_module.AI_CLIENT.expand_associative_query("研究论题甲", deep=False)
        self.assertEqual(cc2.call_args.kwargs["model"], ai_module.ASSOC_EXPAND_MODEL)

    def test_cache_is_separated_by_tier(self) -> None:
        # 同一句话在两档下必须各问一次模型：混用缓存会让研究档悄悄吃到快档线索。
        with mock.patch.object(app_module.AI_CLIENT, "chat_complete", return_value=self.PLAN) as cc:
            app_module.AI_CLIENT.expand_associative_query("研究论题乙", deep=False)
            app_module.AI_CLIENT.expand_associative_query("研究论题乙", deep=True)
            app_module.AI_CLIENT.expand_associative_query("研究论题乙", deep=True)  # 深档第二次走缓存
        self.assertEqual(cc.call_count, 2)
        self.assertEqual(
            [c.kwargs["model"] for c in cc.call_args_list],
            [ai_module.ASSOC_EXPAND_MODEL, ai_module.ASSOC_EXPAND_DEEP_MODEL],
        )

    def test_deep_falls_back_to_flash_when_pro_yields_nothing(self) -> None:
        # pro 吐不出可用 JSON → 立刻回落 flash（不再赌第二次 pro，抽取耗时要顶在 CF ~100s 之前）。
        with mock.patch.object(
            app_module.AI_CLIENT, "chat_complete", side_effect=["不是 JSON", self.PLAN]
        ) as cc:
            plan = app_module.AI_CLIENT.expand_associative_query("研究论题丙", deep=True)
        self.assertEqual(plan.get("keywords"), ["异化"])
        self.assertEqual(
            [c.kwargs["model"] for c in cc.call_args_list],
            [ai_module.ASSOC_EXPAND_DEEP_MODEL, ai_module.ASSOC_EXPAND_MODEL],
        )

    def test_deep_model_error_does_not_break_retrieval(self) -> None:
        # pro 通道报错（限流/超时）也不能让整条研究检索失败：吞掉异常继续走 flash。
        with mock.patch.object(
            app_module.AI_CLIENT,
            "chat_complete",
            side_effect=[app_module.AIServiceError("上游 500"), self.PLAN],
        ) as cc:
            plan = app_module.AI_CLIENT.expand_associative_query("研究论题丁", deep=True)
        self.assertEqual(plan.get("fragments"), ["异化劳动"])
        self.assertEqual(cc.call_args.kwargs["model"], ai_module.ASSOC_EXPAND_MODEL)

    def test_all_attempts_failing_returns_empty_not_exception(self) -> None:
        # 全挂也只返回空 plan（调用方据此回退原词兜底），绝不把异常抛给路由层。
        with mock.patch.object(
            app_module.AI_CLIENT, "chat_complete", side_effect=app_module.AIServiceError("全挂")
        ):
            plan = app_module.AI_CLIENT.expand_associative_query("研究论题戊", deep=True)
        self.assertEqual(plan, {})

    def test_route_marks_research_mode_as_deep(self) -> None:
        # 路由层：只有显式 mode=research 才走深档。
        import inspect

        src = inspect.getsource(app_module._api_search_associative_impl)
        self.assertIn(
            'AI_CLIENT.expand_associative_query(retrieval_gist, deep=(mode == "research"))',
            src,
        )


if __name__ == "__main__":
    unittest.main()
