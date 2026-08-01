# -*- coding: utf-8 -*-
"""期刊中文源「国内采集中继」回归测试（零网络）。

覆盖：① 中继新鲜时 fetch_source_articles 直接采用中继文章（不发任何请求），
journal_name/requires_review 以服务器侧来源配置重算；② 可信刊（auto_publish）经中继
仍免人工审核；③ 中继过期回退直抓；④ NCPSSD 详情页被 302 重定向回首页时显式报错
（不再静默空手）；⑤ relay_generated_age_days 年龄计算。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import journal_alerts as ja


def _write_relay(path: Path, *, name: str, fetched_at: str, articles: list[dict], generated_at: str = "") -> None:
    payload = {
        "version": 1,
        "generated_at": generated_at or fetched_at,
        "sources": {name: {"fetched_at": fetched_at, "articles": articles}},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class JournalRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        # ignore_cleanup_errors：journal_alerts 的 `with _connect()` 只提交不关闭连接，
        # Windows 下 sqlite 句柄延迟释放会让目录清理撞 WinError 32（与 test_journal_pdf
        # 的 rmtree(ignore_errors=True) 同一策略——泄漏几 KB 临时文件无害）。
        self._tmp = tempfile.TemporaryDirectory(prefix="marx-relay-test-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.relay_path = Path(self._tmp.name) / "journal_relay.json"
        self._old_path = ja.RELAY_PATH
        ja.RELAY_PATH = self.relay_path
        ja._RELAY_CACHE["mtime"] = None
        ja._RELAY_CACHE["payload"] = None
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ja.RELAY_PATH = self._old_path
        ja._RELAY_CACHE["mtime"] = None
        ja._RELAY_CACHE["payload"] = None

    @staticmethod
    def _article(title: str) -> dict:
        return {
            "journal_name": "本地旧名",
            "language": "zh",
            "title": title,
            "abstract": "摘要",
            "authors": ["某人"],
            "url": "https://www.ncpssd.cn/Literature/articleinfo?id=x",
            "published_at": "2026-06-20",
            "requires_review": True,
            "metadata": {"source": "ncpssd_journal", "ncpssd_id": "x"},
        }

    def test_fresh_relay_is_used_and_review_recomputed(self) -> None:
        src = ja._default_source("马克思主义研究")
        _write_relay(
            self.relay_path,
            name="马克思主义研究",
            fetched_at=ja.utc_now_text(),
            articles=[self._article("论中继"), {"title": "  "}],
        )
        got = ja.fetch_source_articles(src, lookback_days=45)
        self.assertEqual(len(got), 1, "空标题条目应被过滤")
        art = got[0]
        self.assertEqual(art["journal_name"], "马克思主义研究", "journal_name 应以服务器来源为准")
        self.assertTrue(art["metadata"].get("relayed"))
        # 马克思主义研究非可信刊（auto_publish=False）→ 中继文章仍需人工审核
        self.assertTrue(art["requires_review"])

    def test_trusted_source_skips_review_via_relay(self) -> None:
        src = ja._default_source("求是")  # CN_JOURNAL_TRUSTED → auto_publish=True
        _write_relay(
            self.relay_path,
            name="求是",
            fetched_at=ja.utc_now_text(),
            articles=[self._article("论可信刊")],
        )
        got = ja.fetch_source_articles(src, lookback_days=45)
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0]["requires_review"], "可信刊经中继仍应免人工审核")

    def test_stale_relay_falls_back_to_direct_fetch(self) -> None:
        src = ja._default_source("马克思主义研究")
        stale = (ja.utc_now() - timedelta(days=ja.RELAY_MAX_AGE_DAYS + 1)).isoformat(timespec="seconds")
        _write_relay(self.relay_path, name="马克思主义研究", fetched_at=stale, articles=[self._article("旧闻")])
        calls: list[str] = []

        def fake_open(url, user_agent=""):
            calls.append(url)
            raise OSError("network unreachable in test")

        old = ja._urlopen_text_final
        ja._urlopen_text_final = fake_open
        try:
            with self.assertRaises(RuntimeError):
                ja.fetch_source_articles(src, lookback_days=45)
        finally:
            ja._urlopen_text_final = old
        self.assertTrue(calls, "中继过期时应回退到直抓（发出请求）")

    def test_ncpssd_redirect_raises_explicit_error(self) -> None:
        src = ja._default_source("中共党史研究")  # NCPSSD gch 源

        def fake_open(url, user_agent=""):
            return "<html><title>国家哲学社会科学文献中心</title></html>", "https://www.ncpssd.cn/"

        old = ja._urlopen_text_final
        ja._urlopen_text_final = fake_open
        try:
            with self.assertRaises(RuntimeError) as ctx:
                ja.fetch_source_articles(src, lookback_days=45)
        finally:
            ja._urlopen_text_final = old
        msg = str(ctx.exception)
        self.assertIn("重定向", msg)
        self.assertIn("中继", msg, "报错应指向中继排查路径")

    def test_placeholder_window_keeps_new_and_skips_seen(self) -> None:
        """占位日期（NCPSSD 年精度）窗口分支：新文章照收（不被 7 天严格窗口滤掉）；
        库里已有的旧文不随每周采集重浮进新批次。"""
        db_path = Path(self._tmp.name) / "membership.sqlite3"
        old_db, old_init = ja.DB_PATH, ja.init_membership_db
        ja.DB_PATH = db_path
        ja.init_membership_db = lambda: None
        try:
            ja.init_journal_alerts_db()
            with ja._connect() as conn:
                src_row = conn.execute(
                    "SELECT * FROM journal_sources WHERE name = ?", ("求是",)
                ).fetchone()
            source = ja._source_row(src_row)
            year = ja.utc_now().year
            canned = {
                "journal_name": "求是",
                "language": "zh",
                "title": "论新文章窗口",
                "abstract": "已有摘要，避免触发回填网络请求",
                "authors": ["测试"],
                "url": "https://www.ncpssd.cn/Literature/articleinfo?id=QSTEST01",
                "published_at": f"{year}-01-01",   # NCPSSD 占位日期
                "requires_review": False,
                "metadata": {"source": "ncpssd_journal", "ncpssd_id": f"QS{year}01TEST"},
            }
            old_list, old_fetch = ja.list_journal_sources, ja.fetch_source_articles
            ja.list_journal_sources = lambda limit=200: [source]
            ja.fetch_source_articles = lambda src, lookback_days=None: [dict(canned, metadata=dict(canned["metadata"]))]
            try:
                settings = ja.normalize_alert_settings({"lookback_days": 7})
                run1 = ja.collect_batch(ai_client=None, settings=settings)
                self.assertEqual(run1["articles_inserted"], 1, "占位日期的新文章不应被 7 天严格窗口滤掉")
                run2 = ja.collect_batch(ai_client=None, settings=settings)
                self.assertEqual(run2["articles_inserted"], 0)
                with ja._connect() as conn:
                    row = conn.execute(
                        "SELECT batch_id, status FROM journal_articles WHERE title = ?", ("论新文章窗口",)
                    ).fetchone()
                self.assertEqual(int(row["batch_id"]), int(run1["batch_id"]), "旧文不应被重新拉进新批次")
                self.assertEqual(str(row["status"]), "archived", "旧批次归档后旧文应保持归档，不重浮")
            finally:
                ja.list_journal_sources = old_list
                ja.fetch_source_articles = old_fetch
        finally:
            ja.DB_PATH = old_db
            ja.init_membership_db = old_init
            # journal_alerts 的 `with _connect()` 只提交不关闭连接；Windows 下句柄不释放
            # 会让 TemporaryDirectory 清理撞 WinError 32——强制 GC 回收悬空连接。
            import gc

            gc.collect()

    def test_relay_status_fresh_and_missing(self) -> None:
        """控制台状态卡：新鲜中继给出源/篇统计；缺文件时 present=False 且不抛异常。"""
        missing = ja.relay_status()
        self.assertFalse(missing["present"])
        self.assertTrue(missing["expired"])
        _write_relay(
            self.relay_path,
            name="求是",
            fetched_at=ja.utc_now_text(),
            articles=[self._article("状态卡样本")],
        )
        st = ja.relay_status()
        self.assertTrue(st["present"])
        self.assertFalse(st["expired"])
        self.assertFalse(st["stale"])
        self.assertEqual(st["source_count"], 1)
        self.assertEqual(st["article_count"], 1)

    def test_relay_generated_age_days(self) -> None:
        five_days_ago = (ja.utc_now() - timedelta(days=5)).isoformat(timespec="seconds")
        _write_relay(
            self.relay_path,
            name="求是",
            fetched_at=five_days_ago,
            articles=[],
            generated_at=five_days_ago,
        )
        age = ja.relay_generated_age_days()
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 5.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()
