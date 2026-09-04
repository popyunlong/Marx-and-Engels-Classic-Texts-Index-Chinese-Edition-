"""English-only public-PDF publication gate regression tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


from _test_env import APPDATA as _TMP_APPDATA  # noqa: E402
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
import journal_alerts as ja  # noqa: E402
import journal_fulltext as jf  # noqa: E402
from scripts import repair_journal_footnotes as footnote_repair  # noqa: E402


PHILOSOPHY_ADDITIONS = {
    "Radical Philosophy", "Philosophy & Social Criticism", "Constellations",
    "Critical Horizons", "Theory, Culture & Society", "Thesis Eleven",
    "European Journal of Philosophy", "Hegel Bulletin", "Continental Philosophy Review",
    "Inquiry", "Mind", "The Philosophical Review", "The Journal of Philosophy", "Noûs",
    "Philosophy and Phenomenological Research", "Ethics", "Philosophy & Public Affairs",
    "Journal of Political Philosophy", "The Philosophical Quarterly", "Analysis",
    "Australasian Journal of Philosophy", "Philosophical Studies",
    "British Journal for the History of Philosophy", "Journal of the History of Philosophy",
}


class EnglishCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        ja.init_journal_alerts_db()

    def test_catalog_is_exactly_45_active_english_sources(self) -> None:
        catalog = ja.journal_source_catalog()
        names = {source["name"] for source in catalog["en"]}
        self.assertEqual(catalog["zh"], [])
        self.assertEqual(catalog["total"], 45)
        self.assertEqual(len(names), 45)
        self.assertTrue(PHILOSOPHY_ADDITIONS <= names)

    def test_non_english_source_cannot_be_added(self) -> None:
        with self.assertRaises(ValueError):
            ja.add_journal_source(name="中文测试刊", language="zh")

    def test_pricing_catalog_has_only_grouped_english_journals(self) -> None:
        catalog = ja.journal_source_catalog()
        grouped = [source["name"] for group in catalog["en_groups"] for source in group["sources"]]
        self.assertEqual(len(grouped), catalog["total"])
        self.assertEqual(len(set(grouped)), catalog["total"])
        template = (Path(__file__).resolve().parents[1] / "templates" / "pricing.html").read_text(encoding="utf-8")
        self.assertNotIn("中文理论期刊", template)
        self.assertIn("可订阅英文期刊", template)
        self.assertIn("journal_catalog.en_groups", template)


class PdfUrlResolutionTest(unittest.TestCase):
    def test_wiley_uses_pdfdirect_before_html_pdf_route(self) -> None:
        resolver_names = (
            "_candidate_urls_from_unpaywall",
            "_candidate_urls_from_openalex",
            "_candidate_urls_from_core",
            "_candidate_urls_from_doaj",
            "_candidate_urls_from_openaire",
            "_candidate_urls_from_crossref",
            "_candidate_urls_from_semantic_scholar",
        )
        originals = {name: getattr(jf, name) for name in resolver_names}
        for name in resolver_names:
            setattr(jf, name, lambda _doi: [])
        try:
            candidates = jf.resolve_pdf_candidates({
                "doi": "10.1111/papa.70032",
                "pdf_url": "https://onlinelibrary.wiley.com/doi/pdf/10.1111/papa.70032",
            })
        finally:
            for name, resolver in originals.items():
                setattr(jf, name, resolver)
        self.assertEqual(candidates[0], (
            "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1111/papa.70032",
            "wiley-pdfdirect",
        ))


    def test_missing_chunk_marker_is_retried_as_single_paragraph(self) -> None:
        calls: list[list[str]] = []
        original = jf._translate_chunk

        def fake(texts, _src_lang, *, article_id=None):
            calls.append(list(texts))
            if len(texts) == 2:
                return ["甲译", None]
            return ["乙译"]

        jf._translate_chunk = fake
        paragraphs = [
            {"kind": "body", "text": "First complete paragraph."},
            {"kind": "body", "text": "Second complete paragraph."},
        ]
        try:
            done = jf.translate_paragraphs(paragraphs, "en", article_id=99)
        finally:
            jf._translate_chunk = original
        self.assertEqual(done, 2)
        self.assertEqual([item["zh"] for item in paragraphs], ["甲译", "乙译"])
        self.assertEqual(calls[-1], ["Second complete paragraph."])

    def test_paddle_markdown_fallback_preserves_headings_and_body(self) -> None:
        blocks = jf._markdown_ocr_paragraphs(
            "# Introduction\n\nFirst paragraph line one.\nline two.\n\n## Findings\n\nEvidence follows.",
            3,
        )
        self.assertEqual([item["kind"] for item in blocks], ["heading", "body", "heading", "body"])
        self.assertEqual([item.get("level") for item in blocks if item["kind"] == "heading"], [1, 2])
        self.assertTrue(all(item["page"] == 3 for item in blocks))

    def test_invalid_glm_structure_response_keeps_deterministic_labels(self) -> None:
        original = jf._glm_chat
        jf._glm_chat = lambda *args, **kwargs: json.dumps(
            {
                "blocks": [
                    {"id": 0, "kind": "heading|body|footnote|caption|reference", "level": None},
                    {"id": 1, "kind": "body", "level": None},
                ]
            }
        )
        paragraphs = [
            {"page": 1, "kind": "heading", "level": 1, "text": "Introduction"},
            {"page": 1, "kind": "body", "text": "A complete paragraph."},
        ]
        try:
            model = jf.recognize_document_structure(paragraphs, article_id=99)
        finally:
            jf._glm_chat = original
        self.assertEqual([item["kind"] for item in paragraphs], ["heading", "body"])
        self.assertTrue(model.endswith("+deterministic-fallback"))

    def test_glm4v_flash_caps_output_at_provider_limit(self) -> None:
        captured: dict = {}
        original_urlopen = jf.urllib.request.urlopen
        original_record = jf._record_model_usage
        old_key = os.environ.get("ZHIPU_API_KEY")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def fake_urlopen(request, timeout=0):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        jf.urllib.request.urlopen = fake_urlopen
        jf._record_model_usage = lambda *args, **kwargs: None
        os.environ["ZHIPU_API_KEY"] = "site-key"
        try:
            result = jf._glm_chat(
                [{"role": "user", "content": "ocr"}],
                max_tokens=8192,
                feature="test",
                model="glm-4v-flash",
            )
        finally:
            jf.urllib.request.urlopen = original_urlopen
            jf._record_model_usage = original_record
            if old_key is None:
                os.environ.pop("ZHIPU_API_KEY", None)
            else:
                os.environ["ZHIPU_API_KEY"] = old_key
        self.assertEqual(result, "ok")
        self.assertEqual(captured["payload"]["max_tokens"], 1024)

    def test_ocr_similarity_normalizes_layout_whitespace(self) -> None:
        left = "Capital and Class: A Critical Review."
        right = "Capital  and\nClass — A critical review"
        self.assertGreater(jf._ocr_similarity(left, right), 0.95)

    def test_metadata_repairs_keyword_count_with_numbered_translation(self) -> None:
        original_chat, original_chunk = jf._mimo_chat, jf._translate_chunk
        jf._mimo_chat = lambda *args, **kwargs: json.dumps({
            "title_zh": "题名",
            "journal_name_zh": "期刊",
            "authors_zh": ["作者"],
            "abstract_zh": "完整摘要",
            "keywords_zh": ["错误合并"],
            "discipline": "伦理学与政治哲学",
        }, ensure_ascii=False)
        jf._translate_chunk = lambda texts, _src, *, article_id=None: [f"译-{text}" for text in texts]
        try:
            got = jf._translate_article_metadata(
                {"id": 99, "title": "Title", "journal_name": "Journal", "authors": ["Author"]},
                "A complete abstract.", ["democracy", "equality"],
            )
        finally:
            jf._mimo_chat, jf._translate_chunk = original_chat, original_chunk
        self.assertEqual(got["keywords_zh"], ["译-democracy", "译-equality"])

    def test_openalex_extracts_public_pdf_candidate(self) -> None:
        canned = {
            "results": [{
                "id": "https://openalex.org/W1",
                "title": "A study of value form in capital",
                "type": "article",
                "publication_date": "2026-01-15",
                "authorships": [{"author": {"display_name": "Z. San"}}],
                "primary_location": {
                    "source": {"display_name": "Monthly Review"},
                    "landing_page_url": "https://mr.test/w1",
                },
                "best_oa_location": {"pdf_url": "https://mr.test/w1.pdf"},
                "open_access": {"is_oa": True, "oa_status": "gold", "oa_url": "https://mr.test/oa"},
                "biblio": {},
            }]
        }
        original = ja._urlopen_json
        ja._urlopen_json = lambda *args, **kwargs: canned
        try:
            articles = ja._fetch_openalex(
                {"issn": "0027-0520", "language": "en", "name": "Monthly Review"}, 60
            )
        finally:
            ja._urlopen_json = original
        self.assertEqual(articles[0]["pdf_url"], "https://mr.test/w1.pdf")
        self.assertTrue(articles[0]["metadata"]["is_oa"])

    def test_english_discovery_merges_openalex_crossref_and_doaj(self) -> None:
        source = {"issn": "0027-0520", "language": "en", "name": "Monthly Review", "source_type": "openalex"}
        original = (ja._fetch_openalex, ja._fetch_crossref, ja._fetch_doaj)
        ja._fetch_openalex = lambda *_: [{
            "title": "Value and freedom", "doi": "https://doi.org/10.1/example",
            "published_at": "2026-08-16", "authors": ["A. Author"], "metadata": {"openalex_id": "W1"},
        }]
        ja._fetch_crossref = lambda *_: [{
            "title": "Value and freedom", "doi": "10.1/example", "published_at": "2026-08-16",
            "abstract": "A longer publisher abstract.", "metadata": {"crossref_links": [{"url": "https://x.test/a"}]},
        }]
        ja._fetch_doaj = lambda *_: [{
            "title": "Value and freedom", "doi": "10.1/example", "pdf_url": "https://repo.test/a.pdf",
            "metadata": {"doaj_links": [{"url": "https://repo.test/a.pdf"}]},
        }]
        try:
            articles = ja.fetch_source_articles(source, 7)
        finally:
            ja._fetch_openalex, ja._fetch_crossref, ja._fetch_doaj = original
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["abstract"], "A longer publisher abstract.")
        self.assertEqual(articles[0]["pdf_url"], "https://repo.test/a.pdf")
        self.assertEqual(articles[0]["metadata"]["discovery_sources"], ["openalex", "crossref", "doaj"])
        self.assertIn("crossref_links", articles[0]["metadata"])
        self.assertIn("doaj_links", articles[0]["metadata"])

    def test_parallel_discovery_rejects_crossref_paratext_titles(self) -> None:
        for title in ("Book Review", "Abstracts in Chinese September 2026", "A Thinker: A Tribute"):
            self.assertFalse(ja._looks_like_article_title(title))
        self.assertTrue(ja._looks_like_article_title("Age-weighted democracy"))

    def test_alternate_issn_is_tried_when_primary_crossref_issn_is_missing(self) -> None:
        original = ja._urlopen_json
        called: list[str] = []

        def canned(url: str):
            called.append(url)
            if "0022-5053" in url:
                raise RuntimeError("primary not registered")
            return {"message": {"items": []}}

        ja._urlopen_json = canned
        try:
            articles = ja._fetch_crossref(
                {
                    "name": "Journal of the History of Philosophy", "language": "en",
                    "issn": "0022-5053", "config": {"alternate_issn": ["1538-4586"]},
                },
                7,
            )
        finally:
            ja._urlopen_json = original
        self.assertEqual(articles, [])
        self.assertTrue(any("1538-4586" in url for url in called))

    def test_existing_ignored_record_keeps_status_but_gains_new_index_metadata(self) -> None:
        ja.init_journal_alerts_db()
        source = next(source for source in ja.list_journal_sources(limit=160) if source["language"] == "en")
        first, created = ja.upsert_article(
            source,
            {
                "title": "Drivers of <scp>CO2</scp> Reduction", "doi": "10.9999/source-refresh-test",
                "published_at": "2026-08-16", "status": "ignored",
                "metadata": {"openalex_id": "W-refresh"},
            },
            batch_id=771,
        )
        self.assertTrue(created)
        refreshed, created_again = ja.upsert_article(
            source,
            {
                "title": "Drivers of CO2 Reduction", "doi": "10.9999/source-refresh-test",
                "published_at": "2026-08-16", "pdf_url": "https://repo.test/refresh.pdf",
                "metadata": {"crossref_links": [{"url": "https://repo.test/refresh.pdf"}]},
            },
            batch_id=772,
        )
        self.assertFalse(created_again)
        self.assertEqual(refreshed["status"], "ignored")
        self.assertEqual(int(refreshed["batch_id"]), 771)
        self.assertEqual(refreshed["title"], "Drivers of CO2 Reduction")
        self.assertEqual(refreshed["metadata"]["openalex_id"], "W-refresh")
        self.assertIn("crossref_links", refreshed["metadata"])

    def test_fulltext_resolvers_parse_crossref_doaj_openaire_and_semantic_scholar(self) -> None:
        original = jf._http_json

        def canned(url: str, **_kwargs):
            if "crossref" in url:
                return {"message": {"link": [{"URL": "https://publisher.test/a.pdf"}]}}
            if "doaj" in url:
                return {"results": [{"bibjson": {"link": [{"url": "https://doaj.test/a.pdf"}]}}]}
            if "openaire" in url:
                return {"response": {"results": {"result": {"metadata": {"oaf:entity": {"oaf:result": {
                    "fulltext": [{"$": "https://repository.test/a.pdf"}]
                }}}}}}}
            if "semanticscholar" in url:
                return {"url": "https://semanticscholar.test/a", "openAccessPdf": {"url": "https://s2.test/a.pdf"}}
            return {}

        jf._http_json = canned
        try:
            self.assertIn(("https://publisher.test/a.pdf", "crossref"), jf._candidate_urls_from_crossref("10.1/x"))
            self.assertIn(("https://doaj.test/a.pdf", "doaj"), jf._candidate_urls_from_doaj("10.1/x"))
            self.assertIn(("https://repository.test/a.pdf", "openaire-repository"), jf._candidate_urls_from_openaire("10.1/x"))
            self.assertIn(("https://s2.test/a.pdf", "semantic-scholar"), jf._candidate_urls_from_semantic_scholar("10.1/x"))
        finally:
            jf._http_json = original

    def test_openalex_cache_and_core_add_credentialed_oa_fallbacks_without_leaking_keys(self) -> None:
        old_http = jf._http_json
        old_core_limit = jf._core_rate_limit
        old_openalex = os.environ.get("OPENALEX_API_KEY")
        old_core = os.environ.get("CORE_API_KEY")
        os.environ["OPENALEX_API_KEY"] = "openalex-secret"
        os.environ["CORE_API_KEY"] = "core-secret"

        def canned(url: str, **_kwargs):
            if "api.openalex.org" in url:
                return {"id": "https://openalex.org/W123", "locations": []}
            if "api.core.ac.uk" in url:
                return {
                    "results": [{
                        "id": 456,
                        "doi": "https://doi.org/10.1/example",
                        "downloadUrl": "https://repository.test/example.pdf",
                    }]
                }
            raise AssertionError(url)

        jf._http_json = canned
        jf._core_rate_limit = lambda: None
        try:
            openalex = jf._candidate_urls_from_openalex("10.1/example")
            core = jf._candidate_urls_from_core("10.1/example")
            self.assertIn(
                ("https://content.openalex.org/works/W123.pdf", "openalex-cache"),
                openalex,
            )
            self.assertIn(
                ("https://api.core.ac.uk/v3/outputs/456/download", "core-cache"),
                core,
            )
            self.assertFalse(any("secret" in url for url, _kind in [*openalex, *core]))
            self.assertIn(
                "api_key=openalex-secret",
                jf._download_request("https://content.openalex.org/works/W123.pdf").full_url,
            )
            self.assertEqual(
                jf._download_request(
                    "https://api.core.ac.uk/v3/outputs/456/download"
                ).get_header("Authorization"),
                "Bearer core-secret",
            )
        finally:
            jf._http_json = old_http
            jf._core_rate_limit = old_core_limit
            if old_openalex is None:
                os.environ.pop("OPENALEX_API_KEY", None)
            else:
                os.environ["OPENALEX_API_KEY"] = old_openalex
            if old_core is None:
                os.environ.pop("CORE_API_KEY", None)
            else:
                os.environ["CORE_API_KEY"] = old_core

    def test_bad_first_pdf_candidate_does_not_stop_a_complete_repository_copy(self) -> None:
        old_dir = jf.FULLTEXT_DIR
        old_resolve = jf.resolve_pdf_candidates
        old_download = jf._download_pdf
        old_preflight = jf._pdf_candidate_completeness_error
        temp = tempfile.TemporaryDirectory(prefix="pdf-fallback-")
        jf.FULLTEXT_DIR = Path(temp.name)
        jf.resolve_pdf_candidates = lambda _article: [
            ("https://publisher.test/preview.pdf", "publisher"),
            ("https://repository.test/full.pdf", "repository"),
        ]

        def fake_download(url: str, dest: Path):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-test")
            return True, ""

        jf._download_pdf = fake_download
        jf._pdf_candidate_completeness_error = lambda _a, _p, url: "truncated-pdf:2/20" if "preview" in url else ""
        try:
            path, url, host_type, error = jf.fetch_public_pdf({"pages": "1-20"}, 999991)
        finally:
            jf.FULLTEXT_DIR = old_dir
            jf.resolve_pdf_candidates = old_resolve
            jf._download_pdf = old_download
            jf._pdf_candidate_completeness_error = old_preflight
            temp.cleanup()
        self.assertIsNotNone(path)
        self.assertEqual(url, "https://repository.test/full.pdf")
        self.assertEqual(host_type, "repository")
        self.assertEqual(error, "")

    def test_landing_page_extracts_link_jsonld_and_download_anchor(self) -> None:
        original = jf._http_get
        jf._http_get = lambda *_args, **_kwargs: b'''<html><head>
            <link rel="alternate" type="application/pdf" href="/paper.pdf">
            <script type="application/ld+json">{"contentUrl":"/repo/download/2"}</script>
            </head><body><a href="files/final.pdf">PDF</a></body></html>'''
        try:
            urls = jf._resolve_landing_page("https://journal.test/article/1")
        finally:
            jf._http_get = original
        self.assertIn("https://journal.test/paper.pdf", urls)
        self.assertIn("https://journal.test/repo/download/2", urls)
        self.assertIn("https://journal.test/article/files/final.pdf", urls)


_UID = 0


def _publish_complete(pdf_url: str = "https://public.example/article.pdf") -> int:
    global _UID
    _UID += 1
    source = next(source for source in ja.list_journal_sources(limit=160) if source["language"] == "en")
    batch = ja.open_batch(ja.normalize_alert_settings({}))
    row, _ = ja.upsert_article(
        source,
        {
            "title": f"Public article {_UID}",
            "abstract": "A complete abstract.",
            "authors": ["A. Author"],
            "language": "en",
            "url": f"https://public.example/article/{_UID}",
            "pdf_url": pdf_url,
            "published_at": "2026-08-01",
            "status": "ready",
            "metadata": {},
        },
        batch_id=int(batch["id"]),
    )
    now = ja.utc_now_text()
    with ja._connect() as conn:
        conn.execute(
            "UPDATE journal_articles SET public_pdf_verified=1, language='en', status='ready' WHERE id=?",
            (int(row["id"]),),
        )
        conn.execute(
            "INSERT INTO journal_fulltext(article_id,batch_id,status,translated,required_translations,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(row["id"]), int(batch["id"]), "ready", 2, 2, now, now),
        )
        conn.execute(
            "UPDATE journal_digests SET status='published', review_status='approved' WHERE id=?",
            (int(batch["id"]),),
        )
        conn.commit()
    ja.update_article_bilingual_metadata(
        int(row["id"]),
        title_zh=f"公开文章 {_UID}",
        journal_name_zh="每月评论",
        authors_zh=["A·作者"],
        abstract_zh="完整摘要。",
        abstract_en="A complete abstract.",
        discipline="马克思主义思想史与文本研究",
        keywords_en=["critical theory", "capital"],
        keywords_zh=["批判理论", "资本"],
    )
    jf._save_document(
        int(row["id"]),
        {
            "schema_version": 2,
            "article_id": int(row["id"]),
            "paragraphs": [{"kind": "body", "text": "Complete body.", "zh": "完整正文。"}],
        },
    )
    return int(row["id"])


def _mark_as_sample(article_id: int) -> int:
    article = ja.get_public_article(article_id)
    assert article
    batch_id = int(article["batch_id"])
    with ja._connect() as conn:
        conn.execute(
            "UPDATE journal_digests SET status='sample', review_status='pending' WHERE id=?",
            (batch_id,),
        )
        conn.commit()
    return batch_id


class PublicationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        ja.init_journal_alerts_db()

    def test_complete_public_article_is_visible_and_takedown_is_immediate(self) -> None:
        article_id = _publish_complete()
        self.assertIsNotNone(ja.get_public_article(article_id))
        ja.set_article_takedown(article_id, "rights request")
        self.assertIsNone(ja.get_public_article(article_id))
        ja.clear_article_takedown(article_id)
        self.assertIsNotNone(ja.get_public_article(article_id))

    def test_metadata_only_article_is_not_public(self) -> None:
        source = next(source for source in ja.list_journal_sources(limit=160) if source["language"] == "en")
        row, _ = ja.upsert_article(
            source,
            {
                "title": "Metadata only", "abstract": "Not processed.", "authors": ["A"],
                "language": "en", "url": "https://public.example/metadata-only",
                "pdf_url": "https://public.example/metadata-only.pdf", "status": "ready",
            },
        )
        self.assertIsNone(ja.get_public_article(int(row["id"])))


class PdfRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        cls.client = app_module.app.test_client()

    def test_anonymous_formal_issue_redirects_to_sample_membership_prompt(self) -> None:
        article_id = _publish_complete()
        response = self.client.get(f"/journal-alerts/pdf/{article_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/journal-alerts/latest?member_required=1", response.headers["Location"])

    def test_anonymous_can_read_complete_sample_and_open_its_public_pdf(self) -> None:
        article_id = _publish_complete("https://public.example/sample.pdf")
        batch_id = _mark_as_sample(article_id)

        catalog = self.client.get(f"/journal-alerts/latest?d={batch_id}")
        page = catalog.get_data(as_text=True)
        self.assertEqual(catalog.status_code, 200)
        self.assertIn("当前为公开完整样刊", page)
        self.assertIn(f"/journal-alerts/articles/{article_id}", page)

        reader = self.client.get(f"/journal-alerts/articles/{article_id}")
        reader_page = reader.get_data(as_text=True)
        self.assertEqual(reader.status_code, 200)
        self.assertIn("Complete body.", reader_page)
        self.assertIn("完整正文。", reader_page)
        self.assertIn("你正在阅读公开样刊全文", reader_page)

        pdf = self.client.get(f"/journal-alerts/pdf/{article_id}", follow_redirects=False)
        self.assertEqual(pdf.status_code, 302)
        self.assertEqual(pdf.headers["Location"], "https://public.example/sample.pdf")

    def test_anonymous_cannot_select_a_formal_issue_by_id(self) -> None:
        article_id = _publish_complete()
        article = ja.get_public_article(article_id)
        response = self.client.get(
            f"/journal-alerts/latest?d={int(article['batch_id'])}", follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/journal-alerts/latest?member_required=1", response.headers["Location"])

    def test_verified_pdf_redirects_to_public_origin_without_local_mirroring(self) -> None:
        article_id = _publish_complete("https://public.example/original.pdf")
        original = app_module._feature_effective_for_user
        app_module._feature_effective_for_user = lambda *args, **kwargs: True
        try:
            response = self.client.get(f"/journal-alerts/pdf/{article_id}", follow_redirects=False)
        finally:
            app_module._feature_effective_for_user = original
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://public.example/original.pdf")

    def test_reader_renders_heading_pairs_and_original_only_references(self) -> None:
        article_id = _publish_complete()
        jf._save_document(
            article_id,
            {
                "schema_version": 2,
                "article_id": article_id,
                "paragraphs": [
                    {"kind": "heading", "level": 1, "text": "Introduction", "zh": "导论"},
                    {"kind": "body", "text": "Capital is a social relation.", "zh": "资本是一种社会关系。"},
                    {"kind": "reference", "text": "Marx, Karl. Capital.", "zh": ""},
                ],
            },
        )
        original = app_module._feature_effective_for_user
        app_module._feature_effective_for_user = lambda *args, **kwargs: True
        try:
            response = self.client.get(f"/journal-alerts/articles/{article_id}")
        finally:
            app_module._feature_effective_for_user = original
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Introduction", page)
        self.assertIn("导论", page)
        self.assertIn("Capital is a social relation.", page)
        self.assertIn("资本是一种社会关系。", page)
        self.assertEqual(page.count("Marx, Karl. Capital."), 1)
        self.assertIn("GB/T 7714—2015（英文原始）", page)
        self.assertIn("《马克思主义研究》（英文原始）", page)
        self.assertIn("关键词 / KEYWORDS", page)
        self.assertIn("critical theory", page)
        self.assertIn("批判理论", page)
        self.assertNotIn("中文参考译写", page)


class PdfCompletenessAndCleanupTest(unittest.TestCase):
    def test_existing_mixed_paragraph_is_split_without_changing_unaffected_text(self) -> None:
        paragraphs = [
            {"kind": "body", "page": 2, "text": "Main argument. 1 First note. 2 Second note.", "zh": "旧译"},
            {"kind": "body", "page": 3, "text": "Untouched paragraph.", "zh": "不变段落。"},
        ]
        fragments = [
            {"kind": "footnote", "page": 2, "text": "1 First note."},
            {"kind": "footnote", "page": 2, "text": "2 Second note."},
        ]

        repaired, stats = footnote_repair.split_embedded_footnotes(paragraphs, fragments)

        self.assertEqual([item["kind"] for item in repaired], ["body", "footnote", "body"])
        self.assertEqual(repaired[0]["text"], "Main argument.")
        self.assertEqual(repaired[1]["text"], "1 First note. 2 Second note.")
        self.assertEqual(repaired[2], paragraphs[1])
        self.assertEqual(stats["matched_fragments"], 2)
        self.assertEqual(stats["split_paragraphs"], 1)

    def test_layout_footnotes_are_locked_before_model_classification(self) -> None:
        def block(text: str, y1: float, y2: float, size: float = 10.0) -> dict:
            return {
                "bbox": [48.0, y1, 396.0, y2],
                "lines": [{"spans": [{"text": text, "size": size, "flags": 0}]}],
            }

        blocks = [
            block("The main argument continues in ordinary body type.", 120, 160),
            block("14 This note supplies a source for the preceding claim.", 520, 550, 8.0),
            block("It continues on a second line at the bottom of the page.", 552, 590, 8.0),
        ]

        indexes = jf._layout_footnote_indexes(blocks, body_size=10.0, page_height=663.0)

        self.assertEqual(indexes, {1, 2})
        paragraphs = [
            {"kind": "footnote", "text": "14 This note supplies a source.", "_layout_role_locked": True}
        ]
        original = jf._glm_chat
        jf._glm_chat = lambda *args, **kwargs: self.fail("locked footnote should bypass GLM")
        try:
            jf.recognize_document_structure(paragraphs, article_id=1)
        finally:
            jf._glm_chat = original
        self.assertEqual(paragraphs, [{"kind": "footnote", "text": "14 This note supplies a source."}])

    def test_short_page_bottom_label_is_not_a_footnote_continuation(self) -> None:
        blocks = [
            {
                "bbox": [72.0, 760.0, 80.0, 770.0],
                "lines": [{"spans": [{"text": "j", "size": 8.0, "flags": 0}]}],
            }
        ]

        indexes = jf._layout_footnote_indexes(blocks, body_size=11.0, page_height=800.0)

        self.assertEqual(indexes, set())

    def test_two_column_order_keeps_full_width_bands_outside_column_flow(self) -> None:
        def block(text, bbox):
            return {
                "bbox": bbox,
                "lines": [{"spans": [{"text": text, "size": 10, "flags": 0}]}],
            }

        blocks = [
            block("Full width abstract", [40, 100, 550, 180]),
            block("Introduction", [45, 210, 280, 235]),
            block("Left column begins " * 12, [45, 245, 280, 430]),
            block("Right column continues " * 12, [310, 210, 550, 430]),
            block("Full width licence", [40, 500, 550, 530]),
        ]

        self.assertTrue(jf._detect_columns(blocks, 595))
        ordered = [jf._block_text(item) for item in jf._order_two_column_blocks(blocks, 595)]
        self.assertEqual(
            ordered,
            [
                "Full width abstract",
                "Introduction",
                ("Left column begins " * 12).strip(),
                ("Right column continues " * 12).strip(),
                "Full width licence",
            ],
        )

    def test_preview_routes_and_truncated_page_spans_are_rejected(self) -> None:
        article = {"pdf_url": "https://publisher.test/previewpdf/article.xml", "pages": "1-32"}
        self.assertEqual(
            jf.validate_pdf_completeness(article, {"page_count": 2}, article["pdf_url"]),
            "preview-pdf-url",
        )
        article["pdf_url"] = "https://repository.test/article.pdf"
        self.assertEqual(
            jf.validate_pdf_completeness(article, {"page_count": 2}, article["pdf_url"]),
            "truncated-pdf:2/32",
        )
        self.assertEqual(
            jf.validate_pdf_completeness(article, {"page_count": 32}, article["pdf_url"]),
            "",
        )

    def test_short_pdf_without_page_span_requires_stronger_completeness_evidence(self) -> None:
        article = {"pdf_url": "https://repository.test/article.pdf", "pages": ""}
        self.assertEqual(
            jf.validate_pdf_completeness(article, {"page_count": 2}, article["pdf_url"]),
            "unverified-short-pdf:2/3",
        )
        self.assertEqual(
            jf.validate_pdf_completeness(article, {"page_count": 3}, article["pdf_url"]),
            "",
        )
        article["pdf_url"] = "https://publisher.test/files/article-preview.pdf"
        self.assertEqual(
            jf.validate_pdf_completeness(article, {"page_count": 30}, article["pdf_url"]),
            "preview-pdf-url",
        )
        article["pdf_url"] = "https://publisher.test/files/article.pdf?preview=true"
        self.assertEqual(
            jf.validate_pdf_completeness(article, {"page_count": 30}, article["pdf_url"]),
            "preview-pdf-url",
        )

    def test_frontmatter_is_removed_but_notes_references_and_supplements_remain(self) -> None:
        paragraphs = [
            {"kind": "heading", "text": "A Scholarly Article", "page": 1},
            {"kind": "body", "text": "Author Name | ORCID: 0000-0000 | author@example.org", "page": 1},
            {"kind": "heading", "text": "Abstract", "page": 1},
            {"kind": "body", "text": "This article examines a philosophical problem in detail.", "page": 1},
            {"kind": "heading", "text": "Keywords", "page": 1},
            {"kind": "heading", "text": "abolition – revolution – strategy", "page": 1},
            {"kind": "body", "text": "Published with license by Example BV | DOI: 10.1234/example | ISSN 1234-5678", "page": 1},
            {"kind": "heading", "text": "2 Author", "page": 2},
            {"kind": "heading", "level": 1, "text": "Introduction", "page": 2},
            {"kind": "body", "text": "The argument begins with a complete and substantial paragraph of article prose that belongs to the main text.", "page": 2},
            {"kind": "footnote", "text": "1. This note supplies a source and DOI: 10.9999/kept.", "page": 9},
            {"kind": "heading", "level": 1, "text": "Supplementary information", "page": 9},
            {"kind": "body", "text": "Supplementary evidence retained for readers.", "page": 9},
            {"kind": "heading", "level": 1, "text": "References", "page": 10},
            {"kind": "reference", "text": "Marx, Karl. Capital.", "page": 10},
        ]
        normalized = jf.normalize_reflow_content(paragraphs, article={})
        text = "\n".join(item["text"] for item in normalized["paragraphs"])
        self.assertEqual(normalized["keywords_en"], ["abolition", "revolution", "strategy"])
        self.assertTrue(text.startswith("Introduction"))
        self.assertNotIn("ORCID", text)
        self.assertNotIn("Published with license", text)
        self.assertNotIn("Abstract", text)
        self.assertIn("This note supplies a source", text)
        self.assertIn("Supplementary information", text)
        self.assertIn("Marx, Karl. Capital.", text)

    def test_keywords_are_extracted_when_merged_with_received_dates(self) -> None:
        paragraphs = [
            {"kind": "heading", "text": "A Scholarly Article", "page": 1},
            {
                "kind": "body",
                "text": (
                    "Received: 6 May 2026 | Revised: 22 June 2026 | Accepted: 25 June 2026 "
                    "Keywords: Aristotle | Bernard Williams | justice | political realism | Rawls"
                ),
                "page": 1,
            },
            {
                "kind": "body",
                "text": "ABSTRACT This article examines a philosophical problem in detail.",
                "page": 1,
            },
            {"kind": "heading", "level": 1, "text": "1 | Introduction", "page": 1},
            {
                "kind": "body",
                "text": "The complete argument begins here and continues through the article.",
                "page": 1,
            },
            {
                "kind": "body",
                "text": "abstract principles remain ordinary prose after the introduction.",
                "page": 2,
            },
        ]

        normalized = jf.normalize_reflow_content(paragraphs, article={})

        self.assertEqual(
            normalized["keywords_en"],
            ["Aristotle", "Bernard Williams", "justice", "political realism", "Rawls"],
        )
        self.assertEqual(normalized["paragraphs"][0]["text"], "1 | Introduction")
        self.assertIn("abstract principles remain", normalized["paragraphs"][-1]["text"])

    def test_paragraph_fragments_rejoin_after_frontmatter_noise_is_removed(self) -> None:
        paragraphs = [
            {"kind": "heading", "level": 1, "text": "Introduction", "page": 1},
            {"kind": "body", "text": "A complete sentence begins and then", "page": 1},
            {"kind": "body", "text": "© 2026 Publisher. All rights reserved.", "page": 1},
            {"kind": "body", "text": "continues on the next page before it ends.", "page": 2},
        ]

        normalized = jf.normalize_reflow_content(paragraphs, article={})

        self.assertEqual(len(normalized["paragraphs"]), 2)
        self.assertEqual(
            normalized["paragraphs"][1]["text"],
            "A complete sentence begins and then continues on the next page before it ends.",
        )


if __name__ == "__main__":
    unittest.main()
