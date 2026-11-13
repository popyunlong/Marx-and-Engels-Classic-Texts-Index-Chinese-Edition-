"""New HTTP, serialization and exact-only acceptance scenarios; execute once per ledger."""
import json
from unittest.mock import Mock

import pytest
from flask import g
from werkzeug.exceptions import Forbidden

from fuzzy_access_fixture import A, M, account, login, install_corpus
import ai
import feature_access as F
from chapter_search import chapter_rows
from textual_search import TextualResult


FORBIDDEN = {"context", "textual_differences", "citation", "citations", "printed_pages",
             "pdf_pages", "page_refs", "page_location", "page_id", "page_ids",
             "viewer_url", "source_file", "document_id", "chapter_pdf_page",
             "end_pdf_page", "work_end_pdf_page", "norm_start", "norm_end", "highlight_text"}


def directory_only(value):
    if isinstance(value, dict):
        assert not (value.keys() & FORBIDDEN)
        for item in value.values():
            directory_only(item)
    elif isinstance(value, list):
        for item in value:
            directory_only(item)


@pytest.fixture
def setup(monkeypatch):
    import urllib.request
    import smtplib
    blocked = Mock(side_effect=AssertionError("External service or paid quota was invoked"))
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(smtplib, "SMTP", blocked)
    monkeypatch.setattr(A.AI_CLIENT, "expand_associative_query", blocked)
    monkeypatch.setattr(A.AI_CLIENT, "rank_associative_candidates", blocked)
    monkeypatch.setattr(A, "_require_ai_quota_or_raise", blocked)
    monkeypatch.setattr(A, "_record_ai_usage", blocked)
    corpus, toc = install_corpus(monkeypatch)
    A.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return corpus, toc, blocked


def post(client, headers, **overrides):
    payload = dict(gist="劳动创造社会财福", mode="locate", retrieval_strategy="textual", scope=["vol:文集:1"])
    payload.update(overrides)
    return client.post("/api/search/associative", json=payload, headers=headers)


@pytest.mark.parametrize("kind,status,full", [
    ("guest", 401, False), ("registered", 200, False), ("expired", 200, False),
    ("member", 200, True), ("blocked", 403, False), ("disabled", 401, False),
])
def test_textual_http_identity_and_content_contract(setup, monkeypatch, kind, status, full):
    client = A.app.test_client()
    headers = {}
    if kind != "guest":
        u = account(kind)
        headers = login(client, u)
        if kind == "blocked":
            policy = F.load_access_policy(include_saved=False)
            policy["users"][u["email"]] = {"search": False}
            monkeypatch.setattr(A, "_load_access_policy", lambda: policy)
    else:
        # Guest CSRF is valid: the rejection must be the login boundary.
        with client.session_transaction() as session:
            session["_csrf_token"] = "guest-fuzzy"
        headers = {"X-CSRF-Token": "guest-fuzzy"}
    response = post(client, headers)
    assert response.status_code == status, response.get_data(as_text=True)
    if status == 200:
        data = response.get_json()
        assert data["count"] > 0 and data["effective_strategy"] == "textual"
        assert data["textual_search_complete"]
        assert data["access_level"] == ("full" if full else "summary")
        if full:
            assert data["results"][0]["context"] and data["results"][0]["viewer_available"]
        else:
            directory_only(data)
            assert sum(row["count"] for row in data["results"]) == data["count"]
            assert {row["volume"] for row in data["results"]} == {1}
    assert not setup[2].called


@pytest.mark.parametrize("strategy,status", [("semantic", 403), (None, 403), ("invalid", 400), (["textual"], 400)])
def test_free_account_cannot_select_semantic_or_legacy_strategy(setup, strategy, status):
    client = A.app.test_client()
    headers = login(client, account("registered"))
    response = post(client, headers, retrieval_strategy=strategy)
    assert response.status_code == status
    assert not setup[2].called


def test_research_mode_takes_precedence_over_free_strategy(setup, monkeypatch):
    calls = []
    original = A._require_content_feature
    def gate(feature, **kwargs):
        calls.append(feature)
        if feature == "research":
            raise Forbidden("research boundary")
        return original(feature, **kwargs)
    monkeypatch.setattr(A, "_require_content_feature", gate)
    with A.app.test_request_context("/api/search/associative", method="POST",
                                   json={"mode": " RESEARCH ", "retrieval_strategy": "textual", "gist": "劳动"}):
        g.current_user = account("registered")
        with pytest.raises(Forbidden):
            A._api_search_associative_impl()
    assert calls == ["research"]
    assert not setup[2].called


def test_member_semantic_uses_site_cost_context_not_personal_quota(setup, monkeypatch):
    contexts = []
    def expand(*args, **kwargs):
        contexts.append(dict(ai._AI_CALL_CONTEXT.get()))
        return {"quotes": ["劳动创造社会财富"], "keywords": ["劳动", "财富"]}
    def rank(*args, **kwargs):
        contexts.append(dict(ai._AI_CALL_CONTEXT.get()))
        return []
    monkeypatch.setattr(A, "_require_ai", lambda: None)
    monkeypatch.setattr(A.AI_CLIENT, "expand_associative_query", expand)
    monkeypatch.setattr(A.AI_CLIENT, "rank_associative_candidates", rank)
    usage = Mock()
    monkeypatch.setattr(A, "_record_ai_usage", usage)
    client = A.app.test_client()
    u = account("member")
    before = M.get_ai_entitlements(u["id"])
    response = post(client, login(client, u), retrieval_strategy="semantic", gist="劳动与财富的关系")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["effective_strategy"] == "semantic"
    assert len(contexts) == 2 and all(c["charge_user"] is False for c in contexts)
    assert all(c["feature"] == "associative_internal" for c in contexts)
    assert usage.call_args.kwargs["feature"] == "associative_internal"
    assert M.get_ai_entitlements(u["id"]) == before
    assert not setup[2].called


def test_free_chapter_scope_and_stale_version_do_not_leak_locators(setup):
    corpus = setup[0]
    token = chapter_rows(corpus, corpus.books["文集"][0])[1]["token"]
    client = A.app.test_client()
    headers = login(client, account("registered"))
    response = post(client, headers, scope=[token], scope_version="fuzzy-v1")
    assert response.status_code == 200
    data = response.get_json()
    assert data["count"] == 1 and data["scope"]["id"] == "document"
    assert data["results"][0]["section_title"] == "内部子节"
    directory_only(data)
    response = post(client, headers, scope=[token], scope_version="old-version")
    assert response.status_code == 409
    assert not setup[2].called


def test_ambiguous_document_candidates_are_directory_only(setup, monkeypatch):
    corpus = setup[0]
    monkeypatch.setattr(corpus, "resolve_document_scopes", lambda *a, **k: {
        "status": "ambiguous", "ambiguous_title": "第一作品", "requested_titles": ["第一作品"],
        "scopes": [], "candidates": [{"book": "文集", "volume": 1, "work_title": "第一作品",
                                     "source_file": "secret.pdf", "chapter_pdf_page": 91,
                                     "document_id": "secret.pdf#toc-91"}]})
    client = A.app.test_client()
    response = post(client, login(client, account("registered")))
    assert response.status_code == 200
    data = response.get_json()
    assert data["count"] == 0 and data["scope"]["document_status"] == "ambiguous"
    directory_only(data)


def test_textual_budget_warning_remains_local_and_preserves_completion_state(setup, monkeypatch):
    import textual_search
    monkeypatch.setattr(textual_search, "retrieve_textual", lambda *a, **k: TextualResult(complete=False, reason="budget_exhausted"))
    client = A.app.test_client()
    data = post(client, login(client, account("registered"))).get_json()
    assert not data["textual_search_complete"] and data["warnings"]
    assert data["auto_semantic"]["eligible"] is False
    assert not setup[2].called


def test_explicit_exact_switch_disables_both_fuzzy_scans_but_keeps_shared_default(setup, monkeypatch):
    corpus = setup[0]
    fuzzy = Mock(return_value=([], False))
    monkeypatch.setattr(corpus, "_fuzzy_in_book", fuzzy)
    for query in ("劳动创造社会财富劳动创造社会财富", "劳动创造社会财福劳动创造社会财富"):
        corpus.search_grouped(query, allow_fuzzy=False)
    assert not fuzzy.called
    corpus.search_grouped("劳动创造社会财福劳动创造社会财富")
    assert fuzzy.called


def test_web_exact_never_uses_shared_approximation_for_guest_or_member(setup, monkeypatch):
    corpus = setup[0]
    fuzzy = Mock(side_effect=AssertionError("Hidden fuzzy scan"))
    monkeypatch.setattr(corpus, "_fuzzy_in_book", fuzzy)
    monkeypatch.setattr(A, "_chaptered_search_payload", lambda *a, **k: None)
    for kind in ("guest", "member"):
        client = A.app.test_client()
        if kind == "member":
            headers = login(client, account(kind))
        else:
            with client.session_transaction() as session: session["_csrf_token"] = "exact-guest"
            headers = {"X-CSRF-Token": "exact-guest"}
        response = client.post("/api/search", json={"q": "劳动创造社会财福劳动创造社会财富"}, headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.get_json()["count"] == 0
    assert not fuzzy.called


def test_homepage_uses_new_notes_despite_saved_legacy_prompts(setup, monkeypatch):
    from site_content import update_site_text_overrides
    update_site_text_overrides({"search.fuzzy_no_access": "OLD_PAYWALL", "search.assoc_no_ai": "OLD_AI_PERMISSION"})
    client = A.app.test_client()
    login(client, account("registered"))
    html = client.get("/").get_data(as_text=True)
    assert "const fuzzyAccessEnabled = true;" in html
    assert "const assocAccessEnabled = false;" in html
    assert "const searchViewerEnabled = false;" in html
    assert "OLD_PAYWALL" not in html and "OLD_AI_PERMISSION" not in html
    assert json.dumps("登录后免费使用。", ensure_ascii=True)[1:-1] in html
