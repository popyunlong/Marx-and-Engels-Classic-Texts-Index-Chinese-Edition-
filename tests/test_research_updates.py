from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
import pytest
from flask import Flask, abort
import research_updates as r
import research_collect as col
from research_routes import register
from scripts import research_domestic_sync as sync


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "DB_PATH", tmp_path / "journal.sqlite3")
    r.init_db()
    monkeypatch.setattr(r, "now_text", lambda: "2026-10-04T12:00:00+00:00")


def article(**kw):
    return {"title": "Research title", "journal": "Example Journal", "origin": "domestic", "authors": ["甲", "乙"],
            "url": "https://example.org/article", "year": "2026", "published_at": "2026-09-29", "issue": "9", **kw}


def issue():
    return r.issue_for("2026-10-03T22:00:00+08:00")


def ready(**kw):
    i = issue()
    out = r.upsert(i["id"], article(**kw))
    r.review(i["id"], [out["entry_id"]], "approved", "admin")
    return i, out


def payload(title="第一篇", metadata=True):
    md = f"# 目录\n状态：待复核\n来源：<https://example.org/toc>\n公众号推送：2026-09-29T10:00:00\n\n## 1. {title}\n\n期刊：教学与研究\n作者：甲、乙\n年份：2026\n期号：9\n页码：原文未提供\n起始页：5\n关键词：原文未提供\n\n### 摘要\n\n原文未提供\n\n引文缺项：仅提供起始页\n"
    meta = {"source": {"url": "https://example.org/toc", "published_at": "2026-09-29T10:00:00"}, "needs_review": True,
            "papers": [{"title": title, "journal": "教学与研究", "authors": ["甲", "乙"], "year": 2026, "issue": "9", "page_start": "5"}]} if metadata else None
    out = {"version": 1, "source_id": "source-one", "markdown": md, "metadata": meta}
    out["content_hash"] = r.digest({"markdown": md, "metadata": meta})
    return out


def test_week_window_is_saturday_22_and_half_open():
    assert r.weekly_window(datetime(2026, 10, 3, 21, 59, tzinfo=r.TZ))[1] == "2026-09-26T22:00:00+08:00"
    start, end = r.weekly_window(datetime(2026, 10, 3, 22, tzinfo=r.TZ))
    assert (start, end) == ("2026-09-26T22:00:00+08:00", "2026-10-03T22:00:00+08:00")
    i = issue()
    assert r.period_section(article(published_at=start), i) == "new"
    assert r.period_section(article(published_at=end), i) == "future"
    assert r.period_section(article(published_at="2026-10-03"), i) == "date_review"
    assert r.period_section(article(published_at="2026-09"), i) == "date_review"
    assert r.period_section(article(published_at="2026-01"), i) == "supplement"
    assert r.period_section(article(published_at="2026-04"), i) == "supplement"
    assert r.period_section(article(published_at="2026-10"), i) == "date_review"
    assert r.period_section(article(published_at="2026-11"), i) == "future"
    assert r.period_section(article(published_at="2025"), i) == "supplement"
    assert r.period_section(article(published_at="2026"), i) == "date_review"


def test_sources_match_existing_45_and_have_publishers():
    assert len(col.sources()) == 45
    assert len({x["id"] for x in col.sources()}) == 45
    assert all(x["toc_url"].startswith("https://") for x in col.sources())


@pytest.mark.parametrize("metadata", [True, False])
def test_domestic_parser_preserves_missing_and_review(metadata):
    rows, warnings = r.parse_import(payload(metadata=metadata))
    assert len(rows) == 1 and rows[0]["authors"] == ["甲", "乙"]
    assert rows[0]["abstract"] == "" and rows[0]["keywords"] == []
    assert rows[0]["pages"] == "" and rows[0]["page_start"] == "5"
    assert warnings


def test_import_hash_and_idempotency_and_draft_only():
    p = payload()
    result = r.import_payload(p, issue()["id"])
    assert result["parsed"] == 1 and result["pending"] == 1
    assert r.import_payload(p, issue()["id"])["replayed"]
    assert len(r.items(issue()["id"])) == 1
    assert not r.issues(True)
    p["markdown"] += "changed"
    with pytest.raises(ValueError, match="哈希"):
        r.import_payload(p)


def test_metadata_conflict_is_visible():
    p = payload()
    p["metadata"]["papers"][0]["title"] = "另一篇"
    rows, _ = r.parse_import(p)
    assert any("不一致" in w for w in rows[0]["warnings"])


def test_doi_dedup_updates_without_repeat_issue():
    i, a = ready(doi="https://doi.org/10.1/ABC", pages="")
    snapshot = r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])
    next_i = r.issue_for("2026-10-10T22:00:00+08:00")
    update = r.upsert(next_i["id"], article(doi="10.1/abc", pages="12-20"))
    assert update["entry_id"] == a["entry_id"]
    assert update["issue_id"] != next_i["id"]
    assert not r.items(next_i["id"])
    assert json.loads(r.get_issue(i["id"])["snapshot"]) == snapshot
    correction = r.get_issue(update["issue_id"])
    assert correction["parent_id"] == i["id"]


def test_publish_rejects_stale_preview_and_pending():
    i = issue()
    a = r.upsert(i["id"], article())
    with pytest.raises(ValueError, match="待复核"):
        r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])
    old = r.preview_hash(i["id"])
    r.review(i["id"], [a["entry_id"]], "approved", "admin")
    with pytest.raises(ValueError, match="预览"):
        r.publish(i["id"], old, "admin", [])


def test_foreign_translation_and_ambiguous_date_gate():
    i = issue()
    a = r.upsert(i["id"], article(origin="foreign", published_at="2026-09", abstract="Original"))
    with pytest.raises(ValueError, match="日期"):
        r.review(i["id"], [a["entry_id"]], "approved", "admin")
    with pytest.raises(ValueError, match="译文"):
        r.review(i["id"], [a["entry_id"]], "approved", "admin", section="new")


def test_metadata_bulletin_has_no_45_item_or_pdf_gate():
    i = issue()
    ids = [r.upsert(i["id"], article(title=f"Title {n}"))["entry_id"] for n in range(70)]
    r.review(i["id"], ids, "approved", "admin")
    snapshot = r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])
    assert len(snapshot["articles"]) == 70


def test_correction_updates_web_without_requeueing_email():
    i, a = ready(doi="10.1/test")
    r.publish(i["id"], r.preview_hash(i["id"]), "admin", [{"email": "a@example.org"}])
    updated = r.upsert(i["id"], article(doi="10.1/test", pages="5-10"))
    r.review(updated["issue_id"], [a["entry_id"]], "approved", "admin")
    r.publish(updated["issue_id"], r.preview_hash(updated["issue_id"]), "admin", [{"email": "b@example.org"}])
    with r.connect() as c:
        rows = c.execute("SELECT * FROM research_mail_schedules").fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]["snapshot"])["revision"] == 1
        assert c.execute("SELECT count(*) FROM research_deliveries").fetchone()[0] == 0
    assert json.loads(r.get_issue(i["id"])["snapshot"])["revision"] == 2


def test_mail_revalidates_membership_and_deduplicates(monkeypatch):
    i, _ = ready()
    recipients = [{"email": "a@example.org"}, {"email": "b@example.org"}]
    r.publish(i["id"], r.preview_hash(i["id"]), "admin", recipients)
    monkeypatch.setattr(r, "now_text", lambda: "2026-10-05T01:00:00+00:00")
    send = Mock()
    result = r.deliver("https://example.org", sender=send, recipients=recipients[:1], smtp=object())
    assert result["sent"] == 1 and result["skipped"] == 0
    r.deliver("https://example.org", sender=send, recipients=recipients, smtp=object())
    assert send.call_count == 1


def test_ambiguous_delivery_is_never_automatically_retried(monkeypatch):
    i, _ = ready()
    recipient = [{"email": "a@example.org"}]
    r.publish(i["id"], r.preview_hash(i["id"]), "admin", recipient)
    monkeypatch.setattr(r, "now_text", lambda: "2026-10-05T01:00:00+00:00")
    send = Mock(side_effect=TimeoutError("lost connection"))
    assert r.deliver("https://example.org", sender=send, recipients=recipient, smtp=object())["uncertain"] == 1
    r.deliver("https://example.org", sender=send, recipients=recipient, smtp=object())
    assert send.call_count == 1


def test_citations_do_not_invent_end_pages_or_export_placeholders():
    i, _ = ready(page_start="5", pages="", page_end="", doi="10.1/xyz")
    a = r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])["articles"][0]
    ris = r.export_citation(a, "ris")
    assert "SP  - 5" in ris and "EP  -" not in ris
    assert "未提供" not in ris and "原文未提供" not in r.export_citation(a, "bib")
    assert "仅起始页" in r.citation(a, editorial=True)


def catalogue_rows(starts=("5", "16", "28")):
    return [r.normalize(article(title=f"Paper {n}", source_id="catalogue-one", ordinal=n,
                                page_start=start)) for n, start in enumerate(starts)]


def test_catalogue_page_inference_preserves_source_and_requires_confirmation():
    rows = catalogue_rows()
    assert r.infer_catalogue_pages(rows) == 2
    assert rows[0]["pagination_inference"]["pages"] == "5-15"
    assert rows[1]["pagination_inference"]["pages"] == "16-27"
    assert rows[0]["page_end"] == rows[0]["pages"] == ""
    assert "pagination_inference" not in rows[-1]
    assert r.infer_catalogue_pages(rows) == 0
    i = issue()
    out = r.upsert(i["id"], rows[0])
    with pytest.raises(ValueError, match="页码推断"):
        r.review(i["id"], [out["entry_id"]], "approved", "admin")
    r.review(i["id"], [out["entry_id"]], "approved", "admin", pagination_decision="accept")
    a = r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])["articles"][0]
    assert a["pages"] == "5-15" and a["page_end"] == "15"
    assert "EP  - 15" in r.export_citation(a, "ris")
    assert "已人工确认" in r.export_citation(a, "bib")
    assert "已确认" in r.citation(a, editorial=True)


@pytest.mark.parametrize("change", ["gap", "missing_start", "article_number", "different_issue", "different_source", "duplicate_start", "duplicate_order", "roman"])
def test_page_inference_cannot_cross_uncertain_neighbours(change):
    rows = catalogue_rows()
    if change == "gap": rows[1]["ordinal"] = 7
    if change == "missing_start": rows[1]["page_start"] = ""
    if change == "article_number": rows[1]["article_number"] = "e16"
    if change == "different_issue": rows[1]["issue"] = "10"
    if change == "different_source": rows[1]["source_id"] = "another"
    if change == "duplicate_start": rows[2]["page_start"] = "16"
    if change == "duplicate_order": rows[2]["ordinal"] = 1
    if change == "roman": rows[1]["page_start"] = "xvi"
    r.infer_catalogue_pages(rows)
    assert "pagination_inference" not in rows[0]


def test_page_inference_preserves_explicit_end_and_can_be_rejected():
    rows = catalogue_rows()
    rows[0].update(page_end="14", pages="5-14")
    assert r.infer_catalogue_pages(rows) == 1
    assert "pagination_inference" not in rows[0]
    i = issue()
    out = r.upsert(i["id"], rows[1])
    r.review(i["id"], [out["entry_id"]], "approved", "admin", pagination_decision="reject")
    a = r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])["articles"][0]
    assert a["page_end"] == "" and "EP  -" not in r.export_citation(a, "ris")


def test_import_infers_pages_and_replay_does_not_duplicate():
    p = payload()
    p["metadata"]["papers"] += [{"title": "第二篇", "journal": "教学与研究", "authors": ["丙"], "year": 2026, "issue": "9", "page_start": "18"}]
    p["content_hash"] = r.digest({"markdown": p["markdown"], "metadata": p["metadata"]})
    result = r.import_payload(p, issue()["id"])
    assert result["inferred_pages"] == 1
    assert r.items(issue()["id"])[0]["article"]["pagination_inference"]["pages"] == "5-17"
    assert r.import_payload(p, issue()["id"])["replayed"]
    assert len(r.items(issue()["id"])) == 2


def test_mixed_abstract_catalogue_has_one_issue_header_and_collapsed_details():
    from bs4 import BeautifulSoup
    from scripts.research_update_preview import create_app
    i = issue()
    r.upsert(i["id"], article(title="仅题录篇", page_start="5", ordinal=0))
    r.upsert(i["id"], article(title="摘要篇", abstract="已有摘要", page_start="18", ordinal=1))
    app = create_app()
    res = app.test_client().get(f"/admin/research-updates/{i['id']}/preview")
    assert res.status_code == 200
    soup = BeautifulSoup(res.data, "html.parser")
    assert len(soup.select(".journal-block")) == len(soup.select(".issue-heading")) == 1
    assert len(soup.select(".catalogue-entry")) == len(soup.select(".article-details:not([open])")) == 2
    assert "仅题录" in soup.select(".entry-labels")[0].get_text()
    assert "含摘要" in soup.select(".entry-labels")[1].get_text()
    assert soup.select_one(".abstract").get_text().endswith("已有摘要")
    text, rich = r.render_email({"id": i["id"], "period_start": i["period_start"], "period_end": i["period_end"],
                                "articles": [{**x["article"], "entry_id": x["entry_id"]} for x in r.items(i["id"])]}, {}, "https://example.org")
    assert rich.count("Example Journal") == 1
    assert "原刊未提供摘要" not in rich and "仅题录" in rich and "已有摘要" in rich
    assert "5 起" in text


def test_crossref_cursor_does_not_truncate_at_50():
    source = col.sources()[0]
    first = [{"DOI": f"10.1/{n}", "title": [f"Paper {n}"], "type": "journal-article", "published-online": {"date-parts": [[2026, 9, 29]]}} for n in range(100)]
    http = Mock()
    http.get.side_effect = [json.dumps({"message": {"items": first, "next-cursor": "next"}}), json.dumps({"message": {"items": first[:2]}})]
    assert len(list(col.indexed_records(source, issue(), "crossref", http))) == 102
    assert "cursor=next" in http.get.call_args[0][0]


def test_failed_source_is_not_zero_new(monkeypatch):
    monkeypatch.setattr(col, "indexed_records", Mock(side_effect=RuntimeError("HTTP 429")))
    monkeypatch.setattr(col, "publisher_records", Mock(side_effect=RuntimeError("HTTP 403")))
    result = col.collect(issue()["id"], source_id=col.sources()[0]["id"], http=Mock())
    assert result[0]["status"] == "failed"
    assert len(result[0]["providers"]) == 3


def test_openalex_model_keywords_not_presented_as_author_keywords():
    a = col.openalex_record({"title": "Test", "keywords": [{"display_name": "predicted"}], "doi": "https://doi.org/10.1/x"}, col.sources()[0])
    assert not a.get("keywords")


@pytest.mark.parametrize("url", ["http://api.crossref.org/works", "https://localhost/", "https://127.0.0.1/", "https://api.crossref.org@evil.test/", "https://api.crossref.org:8000/"])
def test_ssrf_allowlist(url):
    with pytest.raises(ValueError):
        col.validate_url(url)


def test_tokens_revocable_and_not_plaintext():
    token = r.create_token("test")
    assert r.token_valid(token)
    with r.connect(True) as c:
        row = c.execute("SELECT * FROM research_tokens").fetchone()
        assert row["token_hash"] != token
        c.execute("UPDATE research_tokens SET active=0")
    assert not r.token_valid(token)


def test_local_sync_skips_history_and_unchanged(tmp_path):
    root = tmp_path / "export"
    (root / "current").mkdir(parents=True)
    (root / "history").mkdir()
    p = payload()
    for folder in ("current", "history"):
        path = root / folder / "catalogue.md"
        path.write_text(p["markdown"], encoding="utf-8")
        import os, time
        os.utime(path, (time.time() - 60, time.time() - 60))
    state = tmp_path / "state.json"
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"import_id": 1}))
    assert len(sync.sync(root, state, "https://example.org", "token", post=post)) == 1
    assert sync.sync(root, state, "https://example.org", "token", post=post)[0]["unchanged"]
    assert post.call_count == 1


def route_app():
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
    app.secret_key = "test"
    def deny():
        abort(403)
    host = {"_feature_effective_for_user": lambda name: False, "_require_admin": deny,
            "_require_management_csrf": deny, "CSRF_EXEMPT_ENDPOINTS": set()}
    register(app, host)
    return app, host


def test_bearer_import_cannot_access_admin_or_unpublished_entries():
    app, _ = route_app()
    client = app.test_client()
    token = r.create_token("import")
    assert client.post("/api/research-updates/imports", json=payload()).status_code == 401
    result = client.post("/api/research-updates/imports", json=payload(), headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    i = result.json["issues"][0]
    assert client.get(f"/research-updates/{i}").status_code == 404
    assert client.post("/admin/research-updates", data={"action": "publish"}, headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.get(f"/admin/research-updates/{i}/preview").status_code == 403


def test_member_only_published_snapshot_and_export():
    app, host = route_app()
    i, a = ready()
    r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])
    client = app.test_client()
    assert client.get(f"/research-updates/{i['id']}/articles/{a['entry_id']}/citation.ris").status_code == 403
    host["_feature_effective_for_user"] = lambda name: True
    response = client.get(f"/research-updates/{i['id']}/articles/{a['entry_id']}/citation.ris")
    assert response.status_code == 200 and b"TY  - JOUR" in response.data


def test_merge_ocr_identity_and_future_replay():
    i = issue()
    old = r.upsert(i["id"], article(title="OCR typo"))
    new = r.upsert(i["id"], article(title="Corrected title"))
    r.merge_items(i["id"], new["entry_id"], old["entry_id"], "admin")
    later = r.upsert(i["id"], article(title="Corrected title", pages="3-9"))
    assert later["entry_id"] == old["entry_id"]
    assert next(x for x in r.items(i["id"]) if x["entry_id"] == new["entry_id"])["review"] == "excluded"


def test_moving_items_requires_review_in_target_issue():
    i, a = ready()
    target = r.issue_for("2026-10-10T22:00:00+08:00")
    r.move_items(i["id"], [a["entry_id"]], target["id"])
    assert not r.items(i["id"])
    moved = r.items(target["id"])[0]
    assert moved["review"] == "pending" and moved["section"] == "supplement"


def test_original_revision_invalidates_translation():
    i = issue()
    a = article(origin="foreign", title_zh="旧译名", abstract="Old abstract", abstract_zh="旧摘要")
    r.upsert(i["id"], a)
    r.upsert(i["id"], {**a, "abstract": "Revised abstract"})
    saved = r.items(i["id"])[0]["article"]
    assert saved["title_zh"] == "旧译名" and saved["abstract_zh"] == ""


def test_online_first_without_doi_then_doi_with_new_year_keeps_identity():
    i = issue()
    a = r.upsert(i["id"], article(origin="foreign"))
    b = r.upsert(i["id"], article(origin="foreign", doi="10.1/new", year="2027", volume="10"))
    assert a["entry_id"] == b["entry_id"] and len(r.items(i["id"])) == 1


def test_translation_cache_does_not_fill_missing_abstract_or_keywords():
    i = issue()
    r.upsert(i["id"], article(origin="foreign"))
    client = Mock()
    client.chat_complete.return_value = json.dumps({"title_zh":"研究题名", "abstract_zh":"invented", "keywords_zh":["invented"]})
    assert col.translate(i["id"], client) == {"translated": 1, "failed": 0}
    assert col.translate(i["id"], client)["translated"] == 1
    assert client.chat_complete.call_count == 1
    a = r.items(i["id"])[0]["article"]
    assert not a["abstract_zh"] and not a["keywords_zh"]


def test_rate_limit_retry_and_cooldown(monkeypatch):
    monkeypatch.setattr(col, "validate_url", lambda url: None)
    monkeypatch.setattr(col.time, "sleep", lambda delay: None)
    def response(code, body=b"{}"):
        m = Mock(status_code=code, headers={"Retry-After":"0"})
        m.__enter__ = Mock(return_value=m); m.__exit__ = Mock(return_value=False)
        m.iter_content.return_value = [body]
        return m
    http = col.HTTP(retries=1, delay=0)
    http.session.get = Mock(side_effect=[response(429), response(200)])
    assert http.get("https://api.crossref.org/works") == "{}"
    assert http.session.get.call_count == 2
    http.session.get = Mock(return_value=response(429))
    with pytest.raises(RuntimeError, match="429"):
        http.get("https://api.crossref.org/works")
    with pytest.raises(RuntimeError, match="冷却"):
        http.get("https://api.crossref.org/works")
    assert http.session.get.call_count == 2


def test_budget_exhaustion_does_not_retry_as_a_short_rate_limit(monkeypatch):
    monkeypatch.setattr(col, "validate_url", lambda url: None)
    m = Mock(status_code=429, headers={"Retry-After": "43000", "X-RateLimit-Reset": "43000", "X-RateLimit-Remaining": "0"})
    m.__enter__ = Mock(return_value=m); m.__exit__ = Mock(return_value=False)
    m.iter_content.return_value = [b'{"message":"Insufficient budget"}']
    http = col.HTTP(retries=3, delay=0)
    http.session.get = Mock(return_value=m)
    with pytest.raises(RuntimeError, match="预算不足"):
        http.get("https://api.openalex.org/works")
    assert http.session.get.call_count == 1
    with pytest.raises(RuntimeError, match="预算不足"):
        http.get("https://api.openalex.org/works")
    assert http.session.get.call_count == 1


def test_publisher_preserves_online_date_and_excludes_covers():
    a = col.detail_metadata('<meta name="citation_title" content="Paper"><meta name="citation_publication_date" content="2026/04"><meta name="citation_online_date" content="2025/08/12">', "https://www.cambridge.org/article/test")
    assert a["published_online"] == "2025-08-12" and a["published_at"] == "2026-04"
    assert r.period_section(a, issue()) == "supplement"
    assert col.EXCLUDED.search("HGL volume 47 issue 1 Cover and Front matter")
    assert col.EXCLUDED.search("HGL volume 47 issue 1 Cover and Back matter")
    assert col.EXCLUDED.search("Editorial Board")
    assert not col.EXCLUDED.search("The Method of Hegel’s Philosophy of Right")


def test_alternate_issn_empty_success_is_partial_not_total_failure(monkeypatch):
    source = col.sources()[-1]
    monkeypatch.setattr(col, "sources", lambda: [source])
    monkeypatch.setattr(col, "publisher_records", Mock(side_effect=RuntimeError("verification")))
    def indexed(s, i, provider, http):
        if provider == "openalex": raise RuntimeError("budget")
        raise col.PartialSourceError("paper ISSN 404; electronic ISSN checked, zero records")
    monkeypatch.setattr(col, "indexed_records", indexed)
    result = col.collect(issue()["id"], http=Mock())[0]
    assert result["status"] == "partial" and result["providers"]["crossref"]["status"] == "partial"


def test_publisher_rss_and_author_keywords():
    source = {"id":"test", "name":"Example", "toc_url":"https://monthlyreview.org/"}
    http = Mock()
    http.get.side_effect = [
        '<link type="application/rss+xml" href="https://monthlyreview.org/feed/">',
        '<rss><channel><item><title>A meaningful research paper</title><link>https://monthlyreview.org/articles/example</link></item></channel></rss>',
        '<meta name="citation_title" content="A meaningful research paper"><meta name="citation_keywords" content="Labour; Value"><meta name="citation_publication_date" content="2026/09/29">'
    ]
    records, report = col.publisher_records(source, http)
    assert len(records) == 1 and records[0]["keywords"] == ["Labour", "Value"]
    assert not report["errors"]


def test_partial_provider_success_establishes_baseline_for_delayed_records(monkeypatch):
    source = col.sources()[0]
    monkeypatch.setattr(col, "publisher_records", Mock(side_effect=RuntimeError("403")))
    def records(source, issue, provider, http):
        if provider == "openalex": raise RuntimeError("429")
        yield article(origin="foreign", title="Delayed research", published_at="2026-09-01")
    monkeypatch.setattr(col, "indexed_records", records)
    col.collect(issue()["id"], source["id"], http=Mock())
    assert not r.items(issue()["id"])
    col.collect(issue()["id"], source["id"], http=Mock())
    assert r.items(issue()["id"])[0]["section"] == "supplement"


def test_candidate_handoff_is_pending_only_and_idempotent(tmp_path, monkeypatch):
    from scripts.research_candidate import export_draft, import_draft
    i = issue()
    r.import_payload(payload(), i["id"])
    r.upsert(i["id"], article(origin="foreign", title_zh="译文"))
    bundle = export_draft(i["id"])
    monkeypatch.setattr(r, "DB_PATH", tmp_path / "handoff.sqlite3")
    r.init_db()
    out = import_draft(bundle)
    assert import_draft(bundle)["replayed"]
    assert len(r.items(out["issue_id"])) == 2
    assert all(x["review"] == "pending" for x in r.items(out["issue_id"]))
    assert not r.issues(True)
    bundle["payload"]["period_end"] = "altered"
    with pytest.raises(ValueError, match="校验"):
        import_draft(bundle)


def test_unsubscribe_during_batch_is_rechecked(monkeypatch):
    import journal_alerts as ja
    i, _ = ready()
    first = {"email":"a@example.org", "_subscription":{"is_active":1}}
    second = {"email":"b@example.org", "_subscription":{"is_active":1}}
    r.publish(i["id"], r.preview_hash(i["id"]), "admin", [first, second])
    monkeypatch.setattr(ja, "resolve_recipients", Mock(side_effect=[([first, second], True), ([first, second], True), ([first], True)]))
    monkeypatch.setattr(ja, "subscription_is_deliverable", lambda s: bool(s["is_active"]))
    monkeypatch.setattr(r, "now_text", lambda: "2026-10-05T01:00:00+00:00")
    sender = Mock()
    assert r.deliver("https://example.org", sender=sender, smtp=object()) == {"sent":1, "failed":0, "uncertain":0, "skipped":1}
    assert sender.call_count == 1


def test_alternate_issn_is_tried_after_primary_failure():
    source = {**col.sources()[0], "issns":["first", "second"]}
    http = Mock()
    http.get.side_effect = [RuntimeError("HTTP 404"), json.dumps({"message":{"items":[{"title":["Alternate ISSN paper"],"DOI":"10.1/alt","type":"journal-article"}]}})]
    records = col.indexed_records(source, issue(), "crossref", http)
    assert next(records)["doi"] == "10.1/alt"
    with pytest.raises(RuntimeError, match="first"):
        next(records)
    assert "second" in http.get.call_args[0][0]


def test_news_feed_does_not_import_publisher_book_events():
    http = Mock()
    http.get.side_effect = ['<link type="application/rss+xml" href="https://monthlyreview.org/feed/">', '<rss><channel><item><title>New book launch at a festival</title><link>https://monthlyreview.org/new-book-event</link></item></channel></rss>']
    with pytest.raises(RuntimeError, match="未识别"):
        col.publisher_records({"id":"0027-0520", "name":"Monthly Review", "toc_url":"https://monthlyreview.org/"}, http)


def test_release_timer_switch_and_legacy_rollback(tmp_path):
    import os, shutil, subprocess
    bash = os.environ.get("MARX_TEST_BASH") or shutil.which("bash")
    if not bash: pytest.skip("bash unavailable")
    def unix(p):
        return subprocess.check_output([bash,"-c",'cygpath -u "$1"',"--",str(p)],text=True).strip() if os.name == "nt" else str(p)
    root = Path(__file__).resolve().parents[1]
    new = tmp_path / "new"; (new/"scripts").mkdir(parents=True)
    (new/"scripts/research_update_worker.py").write_text("")
    old = tmp_path / "old"; old.mkdir()
    state = tmp_path / "old-state.tsv"
    state.write_bytes(b"marx-search-journal-alerts.timer\tenabled\tactive\nmarx-search-journal-process.timer\tdisabled\tinactive\n")
    script = (root/"deploy/research_units.sh").read_text() + '\nsystemctl() { echo "$*" >> "$4"; }\n'
    # A closure keeps the mocked systemctl output separate from function args.
    script = (root/"deploy/research_units.sh").read_text() + '\nlog="$4"; systemctl() { echo "$*" >> "$log"; }; activate_research_units "$1" "$3"; activate_research_units "$2" "$3"\n'
    log = tmp_path / "calls"
    out = subprocess.run([bash,"--noprofile","--norc","-c",script,"--",unix(new),unix(old),unix(state),unix(log)],capture_output=True,text=True)
    assert out.returncode == 0, out.stderr
    calls=log.read_text()
    assert "enable --now marx-search-research-collect.timer" in calls
    assert "start marx-search-journal-alerts.timer" in calls
    assert "start marx-search-journal-process.timer" not in calls
    rollback=(root/"deploy/rollback_release.sh").read_text()
    loop=rollback[rollback.index('for unit in "${MANAGED_SUPPORT_UNITS[@]}"; do'):rollback.index('exec 9>')]
    fixture='TARGET="$1"; MANAGED_SUPPORT_UNITS=(marx-search-research-collect.timer); OPTIONAL_PRODUCTION_UNITS=();\n'+loop
    out=subprocess.run([bash,"--noprofile","--norc","-c",fixture,"--",unix(old)],capture_output=True,text=True)
    assert out.returncode == 0, out.stderr


def test_index_timestamp_alone_does_not_create_correction():
    i, a = ready(provenance={"crossref":{"indexed":"2026-09-29"}})
    r.publish(i["id"],r.preview_hash(i["id"]),"admin",[])
    update=r.upsert(i["id"],article(provenance={"crossref":{"indexed":"2026-10-04"}}))
    assert update["duplicate"]
    assert len(r.issues()) == 1


def test_publisher_fields_are_not_overwritten_by_secondary_index():
    i=issue()
    r.upsert(i["id"],article(doi="10.1/priority",pages="2-9",field_sources={"pages":[{"provider":"publisher"}]}))
    r.upsert(i["id"],article(doi="10.1/priority",pages="2-5",field_sources={"pages":[{"provider":"openalex"}]}))
    assert r.items(i["id"])[0]["article"]["pages"] == "2-9"


def test_long_email_uses_journal_links_and_escapes_imported_text():
    a=r.normalize(article(title="<script>alert(1)</script>", abstract="甲"*1000))
    snapshot={"id":1,"period_start":"2026-09-26","period_end":"2026-10-03","articles":[{**a,"entry_id":i} for i in range(180)]}
    text,rich=r.render_email(snapshot,{"unsubscribe_token":"test"},"https://example.org")
    assert "?journal=" in rich and "?journal=" in text
    assert "<script>" not in rich
    assert len(rich.encode()) < 80000
    assert "/journal-alerts/unsubscribe/test" in rich


@pytest.mark.parametrize("title", ["Issue Information", "Notes on Contributors", "Front Matter", "Back Cover", "Table of Contents", "Editorial Board", "Advertisement", "Call for Papers: Special Issue", "HGL volume 47 issue 1 Cover and Front matter", "投稿须知"])
def test_nonacademic_rules_auto_exclude_without_deleting_source(title):
    out = r.upsert(issue()["id"], article(title=title, ordinal=4))
    row = r.items(issue()["id"])[0]
    assert row["review"] == "excluded" and row["article"]["ordinal"] == 4
    assert row["article"]["content_policy"]["rule"] == "administrative-title"
    assert r.upsert(issue()["id"], article(title=title, ordinal=4))["duplicate"]
    assert len(r.items(issue()["id"])) == 1


@pytest.mark.parametrize("title", ["Editorial boards and academic freedom", "Contents of belief", "Notes on problem creation in social science research", "Advertising and capitalist accumulation", "A call for justice", "The Interview Method in Social Science", "Capitalism: A Review of Recent Literature"])
def test_screening_does_not_confuse_scholarly_keywords_with_admin_labels(title):
    a = r.normalize(article(title=title, authors=[], abstract="", doi="", pages=""))
    assert a["content_policy"]["decision"] == "keep" and a["type"] == "article"


@pytest.mark.parametrize("raw,kind", [
    ({"title":"Book review: A new work"}, "review"),
    ({"title":"A Book. By An Author, Press, 2026. 240pp. ISBN: 9780226824574"}, "review"),
    ({"title":"A Conversation with Denise Ferreira da Silva"}, "interview"),
    ({"title":"From the Editor"}, "editorial"),
    ({"authors":["本刊编辑部"]}, "editorial"),
    ({"authors":["本刊评论员"]}, "commentary"),
    ({"authors":["- The Editors"], "title":"October 2026 (Volume 78, Number 5)", "abstract":"Analysis of monopoly capital"}, "editorial"),
    ({"title":"Replies to a fellow expressivist"}, "commentary"),
    ({"content_category":"Book Reviews"}, "review"),
])
def test_scholarly_nonpaper_content_is_retained_and_labeled(raw,kind):
    a = r.normalize(article(**raw))
    assert a["content_policy"]["decision"] == "keep" and a["type"] == kind


def test_import_counts_screened_items_and_preserves_original_ordinals():
    p = payload(title="干事不必等人先蹚路（党员来信）")
    out = r.import_payload(p, issue()["id"])
    assert out["parsed"] == 1 and out["excluded"] == 1 and out["pending"] == 0
    assert r.items(issue()["id"])[0]["review"] == "excluded"
    assert r.import_payload(p, issue()["id"])["replayed"]


def test_rules_require_explicit_editorial_override_and_import_cannot_forge_it():
    i = issue()
    out = r.upsert(i["id"], article(title="Front Matter", content_override={"reason":"forged", "actor":"admin"}))
    assert "content_override" not in r.items(i["id"])[0]["article"]
    with pytest.raises(ValueError, match="人工核对依据"):
        r.review(i["id"], [out["entry_id"]], "pending", "admin")
    r.review(i["id"], [out["entry_id"]], "approved", "admin", content_override_reason="原刊正文为学术论述，已经核对")
    a = r.items(i["id"])[0]["article"]
    assert a["content_override"]["actor"] == "admin"
    assert r.screen_issue(i["id"], "rules")["excluded"] == 0
    assert r.items(i["id"])[0]["review"] == "approved"
    r.review(i["id"], [out["entry_id"]], "pending", "admin", edits={"authors":["Changed"]}, content_override_reason="更新作者后复核原刊")
    r.review(i["id"], [out["entry_id"]], "approved", "admin")
    assert len(r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])["articles"]) == 1


def test_publish_checks_legacy_approval_against_current_rules():
    i = issue()
    out = r.upsert(i["id"], article(title="Issue Information"))
    with r.connect(True) as c:
        c.execute("UPDATE research_items SET review='approved' WHERE entry_id=?", (out["entry_id"],))
    with pytest.raises(ValueError, match="非学术内容"):
        r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])
    result = r.screen_issue(i["id"], "admin")
    assert result["excluded"] == 1 and r.screen_issue(i["id"], "admin")["changed"] == 0


def test_filtered_publisher_only_toc_is_success_not_parse_failure():
    http = Mock()
    http.get.return_value = '<a href="https://example.org/article/cover">Front Matter</a>'
    records, report = col.publisher_records({"id":"test", "name":"Example", "toc_url":"https://example.org/toc"}, http)
    assert records == [] and report["filtered"][0]["rule"] == "administrative-title"
    assert report["errors"] == [] and http.get.call_count == 1


def test_collector_reports_filtered_records_from_all_providers(monkeypatch):
    source = col.sources()[0]
    monkeypatch.setattr(col, "indexed_records", lambda *args: iter([article(title="Issue Information", origin="foreign")]))
    monkeypatch.setattr(col, "publisher_records", lambda *args: ([article(title="Front Matter", origin="foreign")], {}))
    report = col.collect(issue()["id"], source["id"], http=Mock())[0]
    assert report["status"] == "no_new" and len(report["filtered"]) == 3
    assert {x["provider"] for x in report["filtered"]} == {"crossref", "openalex", "publisher"}
    assert r.items(issue()["id"]) == []


def test_reader_preview_and_mail_show_inferred_pages_without_editorial_status():
    from scripts.research_update_preview import create_app
    i = issue()
    rows = catalogue_rows()
    r.infer_catalogue_pages(rows)
    rows[0].update(authors=[], type="other", published_at="2026-10", warnings=["OCR待复核"])
    r.upsert(i["id"], rows[0])
    r.upsert(i["id"], article(title="Issue Information"))
    response = create_app().test_client().get(f"/admin/research-updates/{i['id']}/preview")
    text = response.get_data(as_text=True)
    assert "待核对" not in text and "待复核" not in text and "推断" not in text and "Issue Information" not in text
    assert "作者信息未提供" in text and "5-15" in text
    a = r.items(i["id"])[0]["article"]
    assert "待核对" in r.citation(a, editorial=True) and "待核对" not in r.citation(a)
    snapshot = {**i, "articles":[{**a, "entry_id":1}]}
    plain, rich = r.render_email(snapshot, {}, "https://example.org")
    assert all("待核对" not in x and "待复核" not in x and "5-15" in x for x in (plain,rich))
    admin = create_app().test_client().get(f"/admin/research-updates?issue={i['id']}").get_data(as_text=True)
    assert "内容待复核" in admin and "待核对" in admin and "按规则筛选本期条目" in admin



def test_publisher_uses_original_toc_columns_without_keyword_guessing():
    http = Mock()
    http.get.side_effect = [
        '<h4>Book Reviews</h4><h5><a href="https://example.org/article/book">The Fruitfulness of Normative Concepts</a></h5><h4>Articles</h4><h5><a href="https://example.org/article/research">Can Luck Egalitarians Handle Self-Sacrifice?</a></h5>',
        '<meta name="citation_title" content="The Fruitfulness of Normative Concepts">',
        '<meta name="citation_title" content="Can Luck Egalitarians Handle Self-Sacrifice?">',
    ]
    records, _ = col.publisher_records({"id":"test", "name":"Example", "toc_url":"https://example.org/toc"}, http)
    assert records[0]["type"] == "review" and records[1]["type"] == "article"
    assert records[0]["provenance"]["publisher_toc_category"]["url"] == "https://example.org/toc"


def test_rescreen_keeps_excluded_page_anchors_and_never_changes_published_issue():
    i = issue()
    rows = catalogue_rows()
    rows[1]["title"] = "Issue Information"
    for a in rows: r.upsert(i["id"], a)
    r.screen_issue(i["id"], "admin")
    assert len(r.items(i["id"])) == 3
    assert r.infer_issue_pages(i["id"], "admin") == 2
    assert r.items(i["id"])[0]["article"]["pagination_inference"]["pages"] == "5-15"
    for row in r.items(i["id"]):
        if row["review"] != "excluded": r.review(i["id"], [row["entry_id"]], "approved", "admin", pagination_decision="accept")
    r.publish(i["id"], r.preview_hash(i["id"]), "admin", [])
    with pytest.raises(ValueError, match="未发布草稿"):
        r.screen_issue(i["id"], "admin")



def test_batch_journal_review_does_not_silently_restore_excluded_items():
    from bs4 import BeautifulSoup
    from scripts.research_update_preview import create_app
    i = issue()
    keep = r.upsert(i["id"], article(title="A scholarly contribution"))
    excluded = r.upsert(i["id"], article(title="Issue Information"))
    soup = BeautifulSoup(create_app().test_client().get(f"/admin/research-updates?issue={i['id']}").data, "html.parser")
    form = soup.select_one('.admin-journal > form')
    entry_ids = [x['value'] for x in form.select('input[name=entry_id]')]
    assert entry_ids == [str(keep['entry_id'])] and str(excluded['entry_id']) not in entry_ids



def test_crossref_title_keyword_alone_cannot_override_research_type():
    a = col.crossref_record({"title":["The Book Review as a Literary Genre"], "type":"journal-article", "DOI":"10.1/example"}, col.sources()[0])
    assert r.normalize(a)["type"] == "article"



def test_author_translation_preserves_order_and_reuses_unique_name_cache():
    i = issue()
    r.upsert(i["id"], article(origin="foreign", authors=["John Bellamy Foster", "Lisa Herzog"]))
    client = Mock()
    client.chat_complete.return_value = r.dumps({"names":{"0":"约翰·贝拉米·福斯特", "1":"丽莎·赫尔佐格"}})
    assert col.translate_authors(i["id"],client) == {"translated":1,"failed":0}
    a = r.items(i["id"])[0]["article"]
    assert a["authors"] == ["John Bellamy Foster", "Lisa Herzog"]
    assert a["authors_zh"] == ["约翰·贝拉米·福斯特", "丽莎·赫尔佐格"]
    assert client.chat_complete.call_count == 1
    r.upsert(i["id"], article(title="Another contribution", url="https://example.org/article/another", origin="foreign", authors=["Lisa Herzog", "John Bellamy Foster"]))
    assert col.translate_authors(i["id"],client)["translated"] == 1
    assert client.chat_complete.call_count == 1
    assert r.items(i["id"])[1]["article"]["authors_zh"] == ["丽莎·赫尔佐格", "约翰·贝拉米·福斯特"]


@pytest.mark.parametrize("response", [{"names":{"0":"中文名"}}, {"names":{"0":"Foster","1":"Herzog"}}, {"names":{"0":"","1":"丽莎"}}])
def test_invalid_author_translations_never_replace_original_names(response):
    i=issue()
    r.upsert(i["id"],article(origin="foreign",authors=["John Bellamy Foster", "Lisa Herzog"]))
    client=Mock()
    client.chat_complete.return_value=r.dumps(response)
    assert col.translate_authors(i["id"],client)["failed"] == 1
    a=r.items(i["id"])[0]["article"]
    assert a["authors"] == ["John Bellamy Foster", "Lisa Herzog"] and not a["authors_zh"]


def test_author_changes_invalidate_old_translations_in_import_and_admin_review():
    i=issue()
    raw=article(origin="foreign",authors=["John Bellamy Foster"],authors_zh=["约翰·贝拉米·福斯特"])
    result=r.upsert(i["id"],raw)
    r.review(i["id"],[result["entry_id"]],"pending","admin",edits={"authors":["Lisa Herzog"],"authors_zh":["约翰·贝拉米·福斯特"]})
    assert r.items(i["id"])[0]["article"]["authors_zh"] == []
    r.upsert(i["id"],{**raw,"authors":["Lisa Herzog"]})
    rows=r.items(i["id"])
    assert rows[-1]["article"]["authors"] == ["Lisa Herzog"] and rows[-1]["article"]["authors_zh"] == []


def test_author_translation_cannot_overwrite_edit_or_published_snapshot():
    i=issue()
    result=r.upsert(i["id"],article(origin="foreign",authors=["John Bellamy Foster"]))
    client=Mock()
    def answer(*args,**kwargs):
        r.review(i["id"],[result["entry_id"]],"pending","admin",edits={"authors":["Lisa Herzog"]})
        return r.dumps({"names":{"0":"约翰·贝拉米·福斯特"}})
    client.chat_complete.side_effect=answer
    assert col.translate_authors(i["id"],client)["translated"] == 0
    a=r.items(i["id"])[0]["article"]
    assert a["authors"] == ["Lisa Herzog"] and not a["authors_zh"]


def test_bilingual_authors_appear_in_web_mail_search_while_citations_keep_originals():
    from scripts.research_update_preview import create_app
    i=issue()
    result=r.upsert(i["id"],article(origin="foreign",authors=["John Bellamy Foster"],authors_zh=["约翰·贝拉米·福斯特"],title_zh="中文题名"))
    a=r.items(i["id"])[0]["article"]
    a["verified_fields"]=["title","authors","journal","year"]
    web=create_app().test_client().get(f"/admin/research-updates/{i['id']}/preview?q=福斯特").get_data(as_text=True)
    assert "约翰·贝拉米·福斯特" in web and "John Bellamy Foster" in web
    assert 'class="author-original"' in web and 'id="article-' in web
    plain,rich=r.render_email({**i,"articles":[{**a,"entry_id":result['entry_id']}]},{},"https://example.org")
    assert all("约翰·贝拉米·福斯特" in s and "John Bellamy Foster" in s for s in (plain,rich))
    for fmt in ("ris","bib"):
        exported=r.export_citation(a,fmt)
        assert "John Bellamy Foster" in exported and "约翰·贝拉米·福斯特" not in exported


def test_target_journal_translations_cover_catalogue_without_changing_citations():
    from research_journals import chinese_name
    assert len(col.sources()) == 45
    assert all(chinese_name(s['name']) for s in col.sources())
    assert chinese_name('Nous') == chinese_name('Noûs') == '努斯'
    assert chinese_name('Theory, Culture and Society') == '理论、文化与社会'
    a = r.normalize(article(journal='Ethics'))
    assert a['journal'] == 'Ethics' and '伦理学' not in r.citation(a)


def test_issue_fold_headers_group_counts_and_filtered_first_group():
    from bs4 import BeautifulSoup
    from scripts.research_update_preview import create_app
    i=issue()
    for n,(journal,number) in enumerate([('Ethics','1'),('Ethics','1'),('Ethics','2'),('Mind','3')]):
        r.upsert(i['id'],article(title=f'Paper {n}',url=f'https://example.org/article/{n}',origin='foreign',journal=journal,volume='137',issue=number))
    client=create_app().test_client()
    soup=BeautifulSoup(client.get(f"/admin/research-updates/{i['id']}/preview").data,'html.parser')
    groups=soup.select('.journal-issue')
    assert len(groups)==3 and [g.has_attr('open') for g in groups]==[True,False,False]
    assert [len(g.select('.catalogue-entry')) for g in groups]==[2,1,1]
    headers=[g.select_one('summary').get_text(' ',strip=True) for g in groups]
    assert '伦理学' in headers[0] and 'Ethics' in headers[0] and '2026 年' in headers[0] and '第 1 期' in headers[0] and '2 篇' in headers[0]
    assert '第 2 期' in headers[1] and '1 篇' in headers[1]
    option=soup.select_one('option[value="Ethics"]')
    assert '伦理学' in option.get_text()
    filtered=BeautifulSoup(client.get(f"/admin/research-updates/{i['id']}/preview?journal=Mind").data,'html.parser')
    assert len(filtered.select('.journal-issue'))==1 and filtered.select_one('.journal-issue').has_attr('open')


def test_compact_email_preserves_group_counts_and_links_without_hidden_content():
    entries=[{**r.normalize(article(title=f'Paper {n}',journal=journal,origin='foreign',abstract=f'Abstract {n}',issue=number)), 'entry_id':n,'section':section}
             for n,(journal,number,section) in enumerate([('Ethics','1','new'),('Ethics','2','new'),('Mind','3','supplement')])]
    plain,rich=r.render_email({**issue(),'articles':entries},{'unsubscribe_token':'token'},'https://example.org')
    assert '伦理学 · Ethics · 2026 年 · 第 1 期 · 1 篇' in plain
    assert '第 2 期 · 1 篇' in plain and '心灵 · Mind' in plain and '补录与更正' in plain
    assert 'Abstract 0' in rich and 'Abstract 1' not in rich and 'Abstract 2' not in rich
    assert all(f'Paper {n}' in rich for n in range(3))
    assert '?journal=Mind' in plain and '/journal-alerts/unsubscribe/token' in rich
    assert '<details' not in rich and 'display:none' not in rich
