# -*- coding: utf-8 -*-
"""期刊「单期发布上限 + 顺延（deferred）待办」回归测试（零网络）。

覆盖：① 纯选取逻辑 _select_release_articles（上限、中英搭配、按期刊铺开、顺延=池-选中）；
② apply_release_cap 落库（超额转 deferred/batch_id 置空；后续批次逐步释放、积压最终清零）；
③ collect_batch 一次性涌入大量文章时单期体量被压到上限、其余顺延，且顺延不被源站重复采回；
④ 综述生成器 _review_one_discipline 每类篇数封顶时，超出部分不进正文但在文末引文提示。
"""
from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

import journal_alerts as ja
import journal_review as jr


def _art(i: int, *, lang: str = "zh", journal: str = "某刊") -> dict:
    # 形如线上 NCPSSD 中文文章：占位日期 YYYY-01-01 + metadata.ncpssd_id（据此判为占位、
    # 走「年份粗滤 + 只收新文」窗口分支，与批次 12 场景一致）。id 供纯选取函数按身份去重。
    return {
        "id": i,
        "journal_name": journal,
        "language": lang,
        "title": f"文章{i:03d}",
        "abstract": "摘要",
        "authors": ["作者"],
        "url": f"https://example.org/a/{i}",
        "published_at": f"{ja.utc_now().year}-01-01",
        "status": "ready",  # 显式指定，避免英文触发翻译分支
        "metadata": {"ncpssd_id": f"NC{i:04d}"},
    }


class SelectReleaseArticlesTests(unittest.TestCase):
    """_select_release_articles 纯函数逻辑（不碰 DB/网络）。"""

    def test_no_cap_or_under_cap_selects_all(self) -> None:
        pool = [_art(i) for i in range(30)]
        sel, deferred = ja._select_release_articles(pool, 0)      # 0=不限
        self.assertEqual(len(sel), 30)
        self.assertEqual(deferred, [])
        sel, deferred = ja._select_release_articles(pool, 45)     # 池 < 上限
        self.assertEqual(len(sel), 30)
        self.assertEqual(deferred, [])

    def test_cap_respected_and_deferred_is_complement(self) -> None:
        pool = [_art(i, lang="zh") for i in range(236)] + [_art(1000 + i, lang="en") for i in range(10)]
        sel, deferred = ja._select_release_articles(pool, 45)
        self.assertEqual(len(sel), 45)
        self.assertEqual(len(deferred), 246 - 45)
        sel_ids = {a["url"] for a in sel}
        def_ids = {a["url"] for a in deferred}
        self.assertEqual(sel_ids & def_ids, set(), "选中与顺延不相交")
        self.assertEqual(len(sel_ids | def_ids), 246, "选中∪顺延=全池，无丢弃")

    def test_english_included_for_mix(self) -> None:
        """中英搭配：英文稀缺时应全部纳入本期（不被中文挤掉）。"""
        pool = [_art(i, lang="zh") for i in range(236)] + [_art(1000 + i, lang="en") for i in range(10)]
        sel, _ = ja._select_release_articles(pool, 45)
        en = [a for a in sel if a["language"] == "en"]
        self.assertEqual(len(en), 10, "10 篇英文全部入选（中英搭配）")
        self.assertEqual(len(sel) - len(en), 35, "其余名额给中文")

    def test_english_quota_capped_when_plentiful(self) -> None:
        """英文充足时至多占上限的 _RELEASE_EN_SHARE，避免英文刷屏。"""
        pool = [_art(i, lang="en") for i in range(100)] + [_art(1000 + i, lang="zh") for i in range(100)]
        sel, _ = ja._select_release_articles(pool, 40)
        en = [a for a in sel if a["language"] == "en"]
        self.assertEqual(len(en), round(40 * ja._RELEASE_EN_SHARE))
        self.assertEqual(len(sel), 40)

    def test_roundrobin_spreads_across_journals(self) -> None:
        """按期刊轮转：不会某一家刷屏，均匀铺开。"""
        pool = (
            [_art(i, journal="甲刊") for i in range(20)]
            + [_art(100 + i, journal="乙刊") for i in range(20)]
            + [_art(200 + i, journal="丙刊") for i in range(20)]
        )
        sel = ja._roundrobin_by_journal(pool, 6)
        by_journal: dict[str, int] = {}
        for a in sel:
            by_journal[a["journal_name"]] = by_journal.get(a["journal_name"], 0) + 1
        self.assertEqual(by_journal, {"甲刊": 2, "乙刊": 2, "丙刊": 2}, "6 篇应在三刊间平均铺开")


class ApplyReleaseCapDbTests(unittest.TestCase):
    """apply_release_cap / collect_batch 落库行为（临时 DB，零网络）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="marx-cap-test-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self._old_db, self._old_init = ja.DB_PATH, ja.init_membership_db
        ja.DB_PATH = Path(self._tmp.name) / "membership.sqlite3"
        ja.init_membership_db = lambda: None
        ja.init_journal_alerts_db()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ja.DB_PATH, ja.init_membership_db = self._old_db, self._old_init
        gc.collect()  # 释放悬空 sqlite 连接，Windows 下才能清理临时目录

    def _source_id(self, name: str = "求是") -> dict:
        with ja._connect() as conn:
            row = conn.execute("SELECT * FROM journal_sources WHERE name = ?", (name,)).fetchone()
        return ja._source_row(row)

    def _count(self, **where) -> int:
        clause = " AND ".join(f"{k} = ?" for k in where)
        sql = "SELECT COUNT(*) AS n FROM journal_articles" + (f" WHERE {clause}" if where else "")
        with ja._connect() as conn:
            return int(conn.execute(sql, tuple(where.values())).fetchone()["n"])

    def test_apply_cap_defers_excess_and_drains_over_batches(self) -> None:
        source = self._source_id()
        batch1 = ja.open_batch(ja.normalize_alert_settings({}))
        b1 = int(batch1["id"])
        for i in range(60):
            ja.upsert_article(source, _art(i), None, force_publish=True, batch_id=b1)
        settings = ja.normalize_alert_settings({"weekly_release_cap": 45, "auto_approve_articles": True})

        res = ja.apply_release_cap(b1, settings)
        self.assertEqual(res["selected"], 45)
        self.assertEqual(res["deferred"], 15)
        self.assertEqual(res["backlog_remaining"], 15)
        self.assertEqual(self._count(batch_id=b1, status="ready"), 45, "本期只保留 45 篇在办")
        self.assertEqual(self._count(status="deferred"), 15, "超出的 15 篇顺延")

        # 顺延文章 batch_id 应置空，不挂在任何批次上
        with ja._connect() as conn:
            nulls = conn.execute(
                "SELECT COUNT(*) AS n FROM journal_articles WHERE status = 'deferred' AND batch_id IS NULL"
            ).fetchone()["n"]
        self.assertEqual(int(nulls), 15)

        # 下一期：开新批次（归档上期），无新文，cap 应把 15 篇积压全部释放、清零
        batch2 = ja.open_batch(ja.normalize_alert_settings({}))
        b2 = int(batch2["id"])
        res2 = ja.apply_release_cap(b2, settings)
        self.assertEqual(res2["selected"], 15, "积压少于上限时一次性全部释放")
        self.assertEqual(res2["backlog_remaining"], 0, "积压清零")
        self.assertEqual(self._count(batch_id=b2, status="ready"), 15)
        self.assertEqual(self._count(status="deferred"), 0)

    def test_collect_batch_flood_is_capped_and_backlog_not_recrawled(self) -> None:
        """一次性涌入 120 篇 → 单期封顶 45、其余顺延；且顺延文章不会被源站重复采回。"""
        source = self._source_id()
        flood = [_art(i) for i in range(120)]
        old_list, old_fetch = ja.list_journal_sources, ja.fetch_source_articles
        old_detail = ja.fetch_ncpssd_detail
        ja.list_journal_sources = lambda limit=200: [source]
        ja.fetch_source_articles = lambda src, lookback_days=None: [dict(a, metadata=dict(a["metadata"])) for a in flood]
        ja.fetch_ncpssd_detail = lambda *a, **k: None  # 占位日期文章会触发详情补全，屏蔽网络
        try:
            settings = ja.normalize_alert_settings(
                {"lookback_days": 7, "weekly_release_cap": 45, "auto_approve_articles": True}
            )
            run1 = ja.collect_batch(ai_client=None, settings=settings)
            self.assertEqual(run1["batch_total"], 45, "单期综述面对的体量被压到上限")
            self.assertEqual(self._count(status="deferred"), 75, "其余 75 篇顺延")

            # 下一期再采集：源站仍给同样 120 篇，但已入库者（含 deferred）不应被当新文重复采回
            run2 = ja.collect_batch(ai_client=None, settings=settings)
            self.assertEqual(run2["articles_inserted"], 0, "已入库（含顺延）不被重复采回")
            self.assertEqual(run2["batch_total"], 45, "第二期从积压中再释放一批，仍封顶")
            # 两期共释放 90 篇，剩余积压 30 篇
            self.assertEqual(self._count(status="deferred"), 30)
        finally:
            ja.list_journal_sources, ja.fetch_source_articles = old_list, old_fetch
            ja.fetch_ncpssd_detail = old_detail
            gc.collect()


class ReviewDisciplineCapTests(unittest.TestCase):
    """综述生成器每类篇数封顶：超出部分不进正文，但在小节末尾提示去文末引文。"""

    class _FakeClient:
        class config:
            enabled = True
            model = "fake"

        def __init__(self) -> None:
            self.sent_counts: list[int] = []

        def chat_complete(self, messages, max_tokens=0):
            import json as _json

            user = messages[-1]["content"]
            payload = _json.loads(user[user.index("{"):])
            self.sent_counts.append(len(payload.get("经典问题", [])) + len(payload.get("前沿问题", [])))
            return "### 经典问题\n\n综述正文[1]。\n\n### 前沿问题与前沿研究\n\n本领域本周暂无此类研究。"

    def test_extra_refs_note_pure(self) -> None:
        extra = [{"id": 7}, {"id": 9}]
        note = jr._extra_refs_note(extra, {7: 3, 9: 5})
        self.assertIn("另有 2 篇", note)
        self.assertIn("[3]", note)
        self.assertIn("[5]", note)
        self.assertEqual(jr._extra_refs_note([], {}), "", "无超出则无提示")

    def test_discipline_truncates_input_and_appends_note(self) -> None:
        items = [{"id": i, "title": f"文章{i}", "journal_name": "某刊", "authors": [], "abstract": "x"}
                 for i in range(1, 21)]  # 20 篇全归「经典问题」
        classified = {i: {"discipline": "马克思主义基本原理", "problem_type": "经典问题"} for i in range(1, 21)}
        ref_by_id = {i: i for i in range(1, 21)}
        client = self._FakeClient()
        body = jr._review_one_discipline("马克思主义基本原理", items, classified, client, ref_by_id)
        self.assertEqual(client.sent_counts, [jr._REVIEW_MAX_PER_TYPE], "只送上限篇数给 AI")
        self.assertIn(f"另有 {20 - jr._REVIEW_MAX_PER_TYPE} 篇", body, "超出部分在正文末尾提示")

    def test_citations_cover_all_even_when_capped(self) -> None:
        """全覆盖不变：即便正文封顶，文末引文仍覆盖每一篇（引文块独立于正文）。"""
        articles = [{"id": i, "title": f"文章{i}", "journal_name": "某刊", "authors": [], "abstract": ""}
                    for i in range(1, 41)]
        block = jr._citation_block(articles)
        for n in range(1, 41):
            self.assertIn(f"[{n}]", block)


if __name__ == "__main__":
    unittest.main()
