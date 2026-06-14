"""期刊文献 PDF 下载 + 新增政经期刊的回归测试。

覆盖：① 5 种政经期刊已进入可订阅目录（数据驱动，套餐页/控制台同源）；
② pdf_url 列在采集/补全链路上的落库与读取；③ NCPSSD/OpenAlex 的 pdf_url 解析；
④ /journal-alerts/pdf/<id> 路由的鉴权(403)、缺失(404)、源站跳转与本地直发分支。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path


warnings.filterwarnings("ignore", category=ResourceWarning)
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-search-journalpdf-")
atexit.register(lambda: shutil.rmtree(_TMP_APPDATA, ignore_errors=True))
os.environ["APPDATA"] = _TMP_APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
import journal_alerts  # noqa: E402

NEW_JOURNALS = ["当代经济研究", "政治经济学评论", "政治经济学季刊", "政治经济学报", "经济纵横"]


class NewJournalsCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        journal_alerts.init_journal_alerts_db()

    def test_five_journals_in_catalog(self) -> None:
        catalog = journal_alerts.journal_source_catalog()
        zh_names = {s["name"] for s in catalog["zh"]}
        for name in NEW_JOURNALS:
            self.assertIn(name, zh_names, f"{name} 应出现在中文期刊目录(套餐页/控制台同源)")

    def test_source_types_resolved(self) -> None:
        by_name = {s["name"]: s for s in journal_alerts.DEFAULT_JOURNAL_SOURCES}
        # 三种正式刊已核到 NCPSSD gch → web_html/ncpssd_journal 自动抓取
        for name in ("当代经济研究", "经济纵横", "政治经济学季刊"):
            self.assertEqual(by_name[name]["source_type"], "web_html")
            self.assertEqual((by_name[name].get("config") or {}).get("parser"), "ncpssd_journal")
        # 政治经济学评论：不在 NCPSSD/OpenAlex → 抓人大官网（玛格泰克平台）
        self.assertEqual(by_name["政治经济学评论"]["source_type"], "web_html")
        self.assertEqual((by_name["政治经济学评论"].get("config") or {}).get("parser"), "magtech_journal")
        # 政治经济学报：集刊、无 ISSN/gch → manual(待补充)
        self.assertEqual(by_name["政治经济学报"]["source_type"], "manual")


class PdfUrlResolutionTest(unittest.TestCase):
    def test_ncpssd_pdf_url(self) -> None:
        f = journal_alerts._ncpssd_pdf_url
        self.assertEqual(f({"pdfurl": "/down/x.pdf"}), "https://www.ncpssd.cn/down/x.pdf")
        self.assertEqual(f({"fileaddress": "https://h/y.pdf"}), "https://h/y.pdf")
        self.assertEqual(
            f({"pdfsize": 187096, "lngid": "DDJJYJ2026003001"}),
            "https://www.ncpssd.cn/Literature/articleinfo?id=DDJJYJ2026003001&type=journalArticle",
        )
        self.assertEqual(f({"pdfsize": 0}), "")
        self.assertEqual(f({}), "")

    def test_openalex_extracts_pdf_url(self) -> None:
        canned = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "A study of value form in capital",
                    "type": "article",
                    "publication_date": "2026-01-15",
                    "authorships": [{"author": {"display_name": "Z. San"}}],
                    "primary_location": {"source": {"display_name": "Monthly Review"},
                                         "landing_page_url": "https://mr.test/w1"},
                    "best_oa_location": {"pdf_url": "https://mr.test/w1.pdf"},
                    "open_access": {"oa_url": "https://mr.test/oa"},
                    "biblio": {},
                }
            ]
        }
        orig = journal_alerts._urlopen_json
        journal_alerts._urlopen_json = lambda *a, **k: canned
        try:
            arts = journal_alerts._fetch_openalex({"issn": "0027-0520", "language": "en", "name": "Monthly Review"}, 60)
        finally:
            journal_alerts._urlopen_json = orig
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["pdf_url"], "https://mr.test/w1.pdf")


_UID = [0]


def _unique_url() -> str:
    _UID[0] += 1
    return f"https://s/x/{_UID[0]}"


class PdfColumnRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        journal_alerts.init_journal_alerts_db()
        self.source = journal_alerts.list_journal_sources(limit=1)[0]

    def _insert(self, *, pdf_url: str, status: str, title: str) -> dict:
        # url 唯一：upsert_article 以 url 为去重键，重复 url 会并到同一行。
        row, _ = journal_alerts.upsert_article(
            self.source,
            {"title": title, "abstract": "x", "authors": ["A"], "url": _unique_url(),
             "pdf_url": pdf_url, "published_at": "2026-01-01", "status": status,
             "metadata": {}},
        )
        return row

    def test_pdf_url_persisted_and_public_filter(self) -> None:
        ready = self._insert(pdf_url="https://s/a.pdf", status="ready", title="Ready one")
        got = journal_alerts.get_public_article(int(ready["id"]))
        self.assertIsNotNone(got)
        self.assertEqual(got["pdf_url"], "https://s/a.pdf")
        # 草稿态(待翻译)不对外
        draft = self._insert(pdf_url="https://s/b.pdf", status="translation_pending", title="Draft two")
        self.assertIsNone(journal_alerts.get_public_article(int(draft["id"])))


class PdfRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        cls.client = app_module.app.test_client()
        journal_alerts.init_journal_alerts_db()
        cls.source = journal_alerts.list_journal_sources(limit=1)[0]

    def _make(self, *, pdf_url: str, status: str = "ready", title: str = "Art") -> int:
        row, _ = journal_alerts.upsert_article(
            self.source,
            {"title": title, "abstract": "x", "authors": ["A"], "url": _unique_url(),
             "pdf_url": pdf_url, "published_at": "2026-01-01", "status": status, "metadata": {}},
        )
        return int(row["id"])

    def test_anonymous_gets_403(self) -> None:
        aid = self._make(pdf_url="https://s/a.pdf", title="gate")
        resp = self.client.get(f"/journal-alerts/pdf/{aid}", follow_redirects=False)
        self.assertEqual(resp.status_code, 403)

    def test_404_when_no_pdf_or_missing(self) -> None:
        orig = app_module._feature_effective_for_user
        app_module._feature_effective_for_user = lambda *a, **k: True
        try:
            no_pdf = self._make(pdf_url="", title="nopdf")
            self.assertEqual(self.client.get(f"/journal-alerts/pdf/{no_pdf}").status_code, 404)
            self.assertEqual(self.client.get("/journal-alerts/pdf/99999").status_code, 404)
        finally:
            app_module._feature_effective_for_user = orig

    def test_ncpssd_landing_redirects(self) -> None:
        landing = "https://www.ncpssd.cn/Literature/articleinfo?id=X&type=journalArticle"
        orig = app_module._feature_effective_for_user
        app_module._feature_effective_for_user = lambda *a, **k: True
        try:
            aid = self._make(pdf_url=landing, title="ncpssd")
            resp = self.client.get(f"/journal-alerts/pdf/{aid}", follow_redirects=False)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers.get("Location"), landing)
        finally:
            app_module._feature_effective_for_user = orig

    def test_mirrored_pdf_served(self) -> None:
        tmp = Path(_TMP_APPDATA) / "served.pdf"
        tmp.write_bytes(b"%PDF-1.4\n%%EOF\n")
        orig_feat = app_module._feature_effective_for_user
        orig_mirror = app_module._mirror_journal_pdf_to_cache
        app_module._feature_effective_for_user = lambda *a, **k: True
        app_module._mirror_journal_pdf_to_cache = lambda article_id, pdf_url: tmp
        try:
            aid = self._make(pdf_url="https://s/real.pdf", title="真·下载")
            resp = self.client.get(f"/journal-alerts/pdf/{aid}", follow_redirects=False)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.mimetype, "application/pdf")
            self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        finally:
            app_module._feature_effective_for_user = orig_feat
            app_module._mirror_journal_pdf_to_cache = orig_mirror

    def test_mirror_failure_redirects_to_source(self) -> None:
        orig_feat = app_module._feature_effective_for_user
        orig_mirror = app_module._mirror_journal_pdf_to_cache
        app_module._feature_effective_for_user = lambda *a, **k: True
        app_module._mirror_journal_pdf_to_cache = lambda article_id, pdf_url: None
        try:
            aid = self._make(pdf_url="https://s/blocked.pdf", title="blocked")
            resp = self.client.get(f"/journal-alerts/pdf/{aid}", follow_redirects=False)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers.get("Location"), "https://s/blocked.pdf")
        finally:
            app_module._feature_effective_for_user = orig_feat
            app_module._mirror_journal_pdf_to_cache = orig_mirror


if __name__ == "__main__":
    unittest.main()
