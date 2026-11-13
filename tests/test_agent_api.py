"""New API scenarios; run once using the dedicated ledger runner."""
from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, g, request

from agent_api_service import QueryService
from agent_api_store import APIError, Store
from agent_api_web import register_routes
from search import Corpus
from build_index import DB_PATH


@pytest.fixture(scope="module")
def corpus():
    return Corpus()


@pytest.fixture
def service(tmp_path, corpus):
    return QueryService(corpus, Store(tmp_path), lambda: {"文集"}, lambda: "version-one", lambda: "https://example.test")


def test_keys_revocation_rotation_and_secret_storage(tmp_path):
    store = Store(tmp_path)
    first = store.create_key(1, "Codex")
    other = store.create_key(2, "Other")
    original = store.authenticate("Bearer " + first)
    store.create_key(1, "WorkBuddy")
    store.create_key(1, "Harness")
    with pytest.raises(APIError, match="三个"):
        store.create_key(1, "extra")
    replacement = store.create_key(1, "Codex", original["id"])
    with pytest.raises(APIError):
        store.authenticate("Bearer " + first)
    assert store.authenticate("Bearer " + replacement)["user_id"] == 1
    store.revoke(2, store.authenticate("Bearer " + replacement)["id"])
    assert store.authenticate("Bearer " + replacement)
    assert store.authenticate("Bearer " + other)["user_id"] == 2
    assert first.encode() not in store.path.read_bytes()
    assert replacement.encode() not in store.path.read_bytes()


def test_rate_is_atomic_across_keys_and_connections(tmp_path, monkeypatch):
    monkeypatch.setenv("MARX_AGENT_API_PER_MINUTE", "3")
    store = Store(tmp_path)
    keys = [store.authenticate("Bearer " + store.create_key(1, str(i))) for i in range(3)]
    barrier = threading.Barrier(6)
    outcomes = []
    def consume(index):
        barrier.wait()
        try:
            Store(tmp_path).rate(1, consume=True, key_id=keys[index % 3]["id"])
            outcomes.append(200)
        except APIError as exc:
            outcomes.append(exc.status)
    threads = [threading.Thread(target=consume, args=(i,)) for i in range(6)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes) == [200, 200, 200, 429, 429, 429]
    assert store.rate(1)["remaining"] == 0
    assert store.rate(2)["remaining"] == 3


def _lock_child(root, ready, release):
    with Store(root).admission(7):
        ready.set()
        release.wait(10)


def test_cross_process_slots_crash_and_exception_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("MARX_AGENT_API_CONCURRENCY", "1")
    monkeypatch.setenv("MARX_AGENT_API_QUEUE_WAIT_SECONDS", "1")
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_lock_child, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        for uid in (7, 8):
            with pytest.raises(APIError) as error:
                with Store(tmp_path).admission(uid):
                    pytest.fail("global slot bypassed")
            assert error.value.status == 429
    finally:
        process.terminate()
        process.join(10)
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        with store.admission(7):
            raise ValueError("intentional")
    with store.admission(8):
        pass


@pytest.mark.parametrize("scope", [["mylib:1"], ["book:hidden"], ["vol:文集:999"], ["book:文集", "private:2"], "book:文集", [None]])
def test_private_unknown_and_malformed_scope_rejected(service, scope):
    with pytest.raises(APIError) as error:
        service.search(1, {"q": "生产关系", "scope": scope})
    assert error.value.status == 400


def test_exact_passage_metadata_version_and_owner(service):
    result = service.search(1, {"q": "生产关系", "limit": 2})
    assert result["ok"] and result["truncated"] and len(result["results"]) == 2
    first = result["results"][0]
    assert first["book"] == "文集" and first["match_type"] == "exact"
    assert first["verification"] == "normalized_text_match"
    assert first["printed_pages"] and first["pdf_pages"] and first["citations"]
    expanded = service.passage(1, first["evidence_id"])["result"]
    assert "生产关系" in expanded["text"] and len(expanded["text"]) <= 6000
    assert expanded["citations"] == first["citations"]
    assert len(expanded["context_pages"]) <= 3
    with pytest.raises(APIError): service.passage(2, first["evidence_id"])
    service.version = lambda: "version-two"
    with pytest.raises(APIError) as changed: service.passage(1, first["evidence_id"])
    assert changed.value.code == "corpus_changed"
    assert all(row["book"] == "文集" for row in service.sources()["sources"])


def test_cooccurrence_and_near_text_are_candidates(service):
    found = service.search(1, {"q": "生产关系 生产力", "mode": "cooccurrence", "scope": ["vol:文集:1"]})
    assert found["results"]
    assert all(r["volume"] == 1 and r["verification"] == "candidate_requires_review" for r in found["results"])
    query = "国家革命生产关系社会生产力资产阶级无产阶级工人阶级剩余价直劳动异化"
    near = service.search(1, {"q": query, "mode": "near_text", "scope": ["vol:文集:1"]})
    assert near["results"] and near["results"][0]["fuzzy_errors"] == 1
    with pytest.raises(APIError): service.search(1, {"q": query, "mode": "near_text"})


def test_deadline_cancellation_and_nested_search_slot(service, monkeypatch):
    import agent_api_service as module
    import search_capacity
    with pytest.raises(APIError) as cancelled:
        service.search(1, {"q": "生产关系"}, cancelled=lambda: True)
    assert cancelled.value.code == "cancelled"
    ticks = iter([0, 21])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    with pytest.raises(APIError) as timed:
        service.search(1, {"q": "生产关系"})
    assert timed.value.code == "query_timeout"
    assert not getattr(search_capacity._local, "active", False)


def test_routes_membership_csrf_cookie_boundary_and_shared_busy(tmp_path, corpus):
    from werkzeug.exceptions import Forbidden
    app = Flask(__name__, template_folder=str(Path(__file__).parents[1] / "templates"))
    app.secret_key = "fixture"
    app.config["PUBLIC_BASE_URL"] = "https://example.test"
    user = {"id": 11, "is_active": 1, "member": True}
    csrf_exempt = set()
    def csrf():
        if request.form.get("csrf_token") != "fixture-csrf": raise Forbidden()
    @app.before_request
    def browser():
        g.current_user = user
        if request.method == "POST" and request.endpoint not in csrf_exempt: csrf()
    register_routes(app, {"APPDATA_DIR": tmp_path, "CORPUS_INDEX_DB_PATH": Path(DB_PATH), "corpus": corpus,
        "_public_book_keys": lambda: {"文集"}, "current_view_state": lambda: {"data_version": "fixture"},
        "current_app_release": lambda: {"git_sha": "fixture"}, "get_user_by_id": lambda uid: user,
        "_membership_plan_code_for_user": lambda u: "support_pro" if u.get("member") else "",
        "_feature_effective_for_user": lambda feature, u: u.get(feature, True),
        "CSRF_EXEMPT_ENDPOINTS": csrf_exempt, "_require_login_page": lambda: None,
        "_require_csrf": csrf, "_ensure_csrf_token": lambda: "fixture-csrf"})
    store = app.extensions["agent_api"]["store"]
    key = store.create_key(11, "REST")
    headers = {"Authorization": "Bearer " + key}
    client = app.test_client()
    assert client.post("/api/v1/search", json={"q": "生产关系"}).status_code == 401
    assert client.post("/account/agent-api", data={"action": "create", "name": "x"}).status_code == 403
    assert client.get("/account/agent-api").status_code == 302
    assert client.get("/account/agent-api").location.endswith('/api-access')
    response = client.post("/api/v1/search", json={"q": "生产关系"}, headers=headers)
    assert response.status_code == 200 and response.json["results"]
    for field, value in (("member", False), ("agent_api", False), ("viewer", False), ("is_active", 0)):
        old = user.get(field, True)
        user[field] = value
        assert client.get("/api/v1/sources", headers=headers).status_code == 403
        user[field] = old
    with store.admission(11):
        busy = client.get("/api/v1/sources", headers=headers)
        assert busy.status_code == 429 and busy.headers["Retry-After"] == "2"
        assert client.get("/api/v1/limits", headers=headers).status_code == 200
        # The API slot does not consume both existing browser search slots.
        assert corpus.search("生产关系", max_results=1)
    store.revoke(11, store.keys(11)[0]["id"])
    assert client.get("/api/v1/limits", headers=headers).status_code == 401


def test_caddy_route_tracks_target_and_removes_on_legacy_rollback():
    from deploy.agent_api_caddy import patch
    original = "mazhuzuojiansuo.com {\n\treverse_proxy 127.0.0.1:8000\n}\n"
    candidate = patch(original, 18001)
    assert "@agent_api_mcp 127.0.0.1:18001" in candidate
    primary = patch(candidate, 18000)
    assert "18001" not in primary and primary.count("BEGIN marx-agent-api-v1") == 1
    assert patch(primary) == original


def test_contract_and_invalid_input(service):
    from agent_api_contract import TOOLS, openapi
    assert len(TOOLS) == 4
    assert set(openapi()["paths"]) == {"/search", "/sources", "/limits", "/passages/{evidence_id}", "/cancel"}
    for payload in ([], {"q": "a"}, {"q": "甲" * 513}, {"q": "甲乙", "limit": True},
                    {"q": "甲乙", "mode": {}}, {"q": "甲乙", "mode": "cooccurrence"}):
        with pytest.raises(APIError) as error: service.search(1, payload)
        assert error.value.status == 400
