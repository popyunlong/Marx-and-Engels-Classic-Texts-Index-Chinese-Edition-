"""Recovery/scheduling regressions. Consult the recovery ledger before running."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace
import threading

import pytest
from flask import Flask, abort, jsonify

from agent_api_store import APIError, Store
from agent_api_web import register_routes
from search_capacity import ReentrantSemaphore, search_slot


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    import search_capacity
    import search
    monkeypatch.setenv("MARX_AGENT_API_CONCURRENCY", "2")
    monkeypatch.setenv("MARX_AGENT_API_LIGHT_CONCURRENCY", "2")
    monkeypatch.setenv("MARX_AGENT_API_USER_CONCURRENCY", "1")
    monkeypatch.setenv("MARX_AGENT_API_PER_MINUTE", "30")
    monkeypatch.setenv("MARX_FUZZY_CONCURRENCY", "3")
    monkeypatch.setattr(search_capacity, "SEARCH_CONCURRENCY", 3)
    monkeypatch.setattr(search_capacity, "_slots", threading.BoundedSemaphore(3))
    monkeypatch.setattr(search, "_FUZZY_SCAN_SEMAPHORE", ReentrantSemaphore(3))
    app = Flask(__name__)

    @app.before_request
    def simulated_early_guard():
        from flask import request
        if request.headers.get("X-Fixture-Busy"):
            response = jsonify(ok=False, error="legacy global limit")
            response.status_code = 429
            response.headers["Retry-After"] = "9"
            abort(response)

    corpus = SimpleNamespace(books={"文集": []}, citation_templates={})
    register_routes(app, {
        "APPDATA_DIR": tmp_path, "CORPUS_INDEX_DB_PATH": tmp_path / "absent.sqlite",
        "corpus": corpus, "_public_book_keys": lambda: {"文集"},
        "current_view_state": lambda: {}, "current_app_release": lambda: {},
        "get_user_by_id": lambda uid: {"id": uid, "is_active": 1},
        "_membership_plan_code_for_user": lambda user: "member",
        "_feature_effective_for_user": lambda feature, user: True,
        "CSRF_EXEMPT_ENDPOINTS": set(),
    })
    service = app.extensions["agent_api"]["service"]
    store = app.extensions["agent_api"]["store"]
    monkeypatch.setattr(service, "sources", lambda: {"ok": True, "sources": []})
    keys = {uid: {"Authorization": "Bearer " + store.create_key(uid, "recovery-fixture")}
            for uid in (1, 2, 3, 4)}
    return app, store, service, keys


def test_two_heavy_keep_web_scan_and_light_capacity(boundary, monkeypatch):
    app, store, service, keys = boundary
    import search
    ready = threading.Barrier(3)
    finish = threading.Event()

    def work(uid, payload, *, validated):
        ready.wait(timeout=5)
        assert finish.wait(5)
        # Nested actual retrieval must reuse its pre-charged reservation.
        with search_slot():
            assert search._FUZZY_SCAN_SEMAPHORE.acquire(blocking=False)
            search._FUZZY_SCAN_SEMAPHORE.release()
        return {"ok": True, "results": []}

    monkeypatch.setattr(service, "search", work)
    def request(uid):
        with app.test_client() as client:
            return client.post("/api/v1/search", json={"q": "生产关系"}, headers=keys[uid])

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(request, uid) for uid in (1, 2)]
        try:
            ready.wait(timeout=5)
            with app.test_client() as client:
                third = client.post("/api/v1/search", json={"q": "生产关系"}, headers=keys[3])
                assert third.status_code == 429 and third.json["error"]["retryable"]
                assert store.rate(3)["remaining"] == 30
                assert client.get("/api/v1/sources", headers=keys[3]).status_code == 200
                limits = client.get("/api/v1/limits", headers=keys[1])
                assert limits.status_code == 200 and limits.json["scheduling"]["heavy_concurrency"] == 2
                assert store.rate(1)["remaining"] == 29
            with search_slot():
                assert search._FUZZY_SCAN_SEMAPHORE.acquire(blocking=False)
                search._FUZZY_SCAN_SEMAPHORE.release()
        finally:
            finish.set()
        assert [job.result().status_code for job in jobs] == [200, 200]


def test_validation_and_all_admission_failures_precede_charge(boundary, monkeypatch):
    app, store, service, keys = boundary
    import search_capacity
    import search
    with app.test_client() as client:
        assert client.post("/api/v1/search", json={"q": "x"}, headers=keys[1]).status_code == 400
        assert client.get("/api/v1/passages/bad", headers=keys[1]).status_code == 400
        for pool in (search_capacity._slots, search._FUZZY_SCAN_SEMAPHORE):
            # Own raw scan permits outside its reentrant wrapper to model other threads.
            raw = getattr(pool, "_semaphore", pool)
            with ExitStack() as stack:
                for _ in range(3):
                    assert raw.acquire(blocking=False)
                    stack.callback(raw.release)
                busy = client.post("/api/v1/search", json={"q": "生产关系"}, headers=keys[1])
                assert busy.status_code == 503 and busy.json["error"]["retryable"]
        assert store.rate(1)["remaining"] == 30
        def fail(*args, **kwargs):
            raise APIError("store_busy", "fixture", 503, 2)
        monkeypatch.setattr(service, "search", fail)
        failed = client.post("/api/v1/search", json={"q": "生产关系"}, headers=keys[1])
        assert failed.status_code == 503 and not failed.json["error"]["retryable"]
        assert "Retry-After" not in failed.headers
        assert store.rate(1)["remaining"] == 29


def test_light_control_and_near_text_have_distinct_bounded_slots(boundary):
    _, store, _, _ = boundary
    with store.admission(1, near_text=True):
        with store.admission(2):
            with store.admission(3, lane="light"):
                with store.admission(1, lane="light", control=True):
                    with pytest.raises(APIError):
                        with store.admission(4, lane="light"):
                            pass
                    with pytest.raises(APIError):
                        with store.admission(1, lane="light", control=True):
                            pass
        with pytest.raises(APIError):
            with store.admission(2, near_text=True):
                pass
        with store.admission(2):
            pass
    with store.admission(2, near_text=True):
        pass


def test_early_errors_wrong_methods_and_missing_routes_are_json(boundary):
    app, _, _, keys = boundary
    with app.test_client() as client:
        responses = [client.get("/api/v1/absent", headers=keys[1]),
                     client.delete("/api/v1/search", headers=keys[1]),
                     client.get("/api/v1/sources", headers={**keys[1], "X-Fixture-Busy": "1"}),
                     client.post("/api/v1/search", data="x" * 16385, content_type="application/json", headers=keys[1])]
        assert [response.status_code for response in responses] == [404, 405, 429, 413]
        assert "POST" in responses[1].headers["Allow"]
        assert responses[2].headers["Retry-After"] == "9"
        for response in responses:
            assert isinstance(response.json["error"], dict)
            assert response.headers["Cache-Control"] == "private, no-store"
