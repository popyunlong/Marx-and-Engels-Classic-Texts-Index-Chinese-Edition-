"""Plus eligibility and three-tier API acceptance against actual application routes."""
import json
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from membership_refresh_fixture import A, M, user, login
import admin_store
import feature_access as F
import agent_api_web
from agent_api_contract import OUTPUTS, openapi
from agent_api_store import Store, APIError
from member_catalog import PLUS_CODE, public_plan


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch):
    import urllib.request
    import smtplib
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external service")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    A.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    previous = admin_store.get_setting("access_policy", {})
    admin_store.set_setting("access_policy", {})
    yield
    admin_store.set_setting("access_policy", previous)


def auth(key):
    return {"Authorization": "Bearer " + key}


def wait_for(fn):
    end = time.monotonic() + 3
    while not fn():
        if time.monotonic() >= end:
            raise AssertionError("Request did not reach the expected queue state")
        time.sleep(.01)


@pytest.mark.parametrize("code,priority", [
    ("support_basic", "normal"), ("support_plus", "normal"), (PLUS_CODE, "normal"),
    ("support_pro", "high"), ("support_max", "highest"),
    ("monthly", "normal"), ("quarterly", "normal"), ("yearly", "normal"),
])
def test_self_service_and_real_limits_match_current_tier(code, priority):
    if code == "quarterly":
        # This historical product is maintained by admins, not seeded on a fresh DB.
        M.upsert_plan(code="quarterly", name="季度会员", price_cents=2400, interval_months=3, sort_order=15)
    u = user(code)
    store = A.app.extensions["agent_api"]["store"]
    with A.app.test_client() as c:
        headers = login(c, u)
        page = c.post("/api-access", data={"action": "create", "name": "Plus acceptance"}, headers=headers)
        assert page.status_code == 200
        key = re.search(r"marx_[A-Za-z0-9_-]{40,}", page.get_data(as_text=True)).group()
        response = c.get("/api/v1/limits?priority=2", headers={**auth(key), "X-Priority": "2"})
        assert response.status_code == 200, response.get_data(as_text=True)
        result = response.json
        assert result["scheduling"]["priority"] == priority
        assert result["rate"] == {"per_minute": 30, "remaining": 30, "reset_after_seconds": 0}
        assert result["daily_quota"] is None and result["consumes_site_ai_tokens"] is False
        assert result["scheduling"]["queue_capacity"] == 8
        assert result["scheduling"]["queue_wait_seconds"] == 5
        assert result["user_concurrency"] == 1 and len(store.keys(u["id"])) == 1
        matrix = F.plan_feature_access_rows([M.get_plan(code)], F.load_access_policy())
        assert matrix[0]["values"]["agent_api"]


@pytest.mark.parametrize("code", ["support_basic", "support_plus", PLUS_CODE])
def test_existing_plus_key_recovers_without_recreation(code, monkeypatch):
    u = user(code)
    store = A.app.extensions["agent_api"]["store"]
    key = store.create_key(u["id"], "existing key")
    before = store.keys(u["id"])
    current = agent_api_web.api_eligible
    monkeypatch.setattr(agent_api_web, "api_eligible", lambda code: False)
    with A.app.test_client() as c:
        assert c.get("/api/v1/limits", headers=auth(key)).status_code == 403
        monkeypatch.setattr(agent_api_web, "api_eligible", current)
        assert c.get("/api/v1/limits", headers=auth(key)).status_code == 200
    assert store.keys(u["id"]) == before


@pytest.mark.parametrize("reason", ["free", "expired", "disabled", "agent_api", "search", "viewer"])
def test_plus_access_still_requires_active_account_and_permissions(reason):
    u = user(None if reason == "free" else PLUS_CODE)
    store = A.app.extensions["agent_api"]["store"]
    key = store.create_key(u["id"], "boundary")
    if reason == "expired":
        with M._connect() as db:
            db.execute("UPDATE subscriptions SET expires_at=? WHERE user_id=?",
                       ((M.utc_now() - timedelta(seconds=1)).isoformat(), u["id"]))
    elif reason == "disabled":
        with M._connect() as db:
            db.execute("UPDATE users SET is_active=0 WHERE id=?", (u["id"],))
    elif reason in {"agent_api", "search", "viewer"}:
        admin_store.set_setting("access_policy", {"users": {u["email"]: {reason: False}}})
    with A.app.test_client() as c:
        response = c.get("/api/v1/sources", headers=auth(key))
        assert response.status_code == 403
        assert store.rate(u["id"])["remaining"] == 30
        if reason != "disabled":
            headers = login(c, u)
            c.post("/api-access", data={"action": "create", "name": "denied"}, headers=headers)
            assert len(store.keys(u["id"])) == 1
            c.post("/api-access", data={"action": "revoke", "key_id": store.keys(u["id"])[0]["id"]}, headers=headers)
            assert not store.keys(u["id"])


@pytest.mark.parametrize("change,status", [("expiry", 403), ("key", 401), ("policy", 403), ("cancel", 408)])
def test_plus_waiting_request_rechecks_changes_without_charging(change, status, monkeypatch):
    monkeypatch.setenv("MARX_AGENT_API_LIGHT_CONCURRENCY", "1")
    u = user(PLUS_CODE)
    store = A.app.extensions["agent_api"]["store"]
    key = store.create_key(u["id"], "waiting")
    control_key = store.create_key(u["id"], "control")
    request_id = "f" * 32
    results = []
    def run():
        with A.app.test_client() as c:
            results.append(c.get("/api/v1/sources", headers={**auth(key), "X-Query-Request-ID": request_id}))
    thread = threading.Thread(target=run)
    def queued():
        with store.connect() as db:
            row = db.execute("SELECT priority FROM api_waiters WHERE user_id=?", (u["id"],)).fetchone()
            return row is not None and row["priority"] == 0
    try:
        with store._admission_nowait(910001, lane="light"):
            thread.start()
            wait_for(queued)
            if change == "expiry":
                with M._connect() as db:
                    db.execute("UPDATE subscriptions SET expires_at=? WHERE user_id=?",
                               ((M.utc_now() - timedelta(seconds=1)).isoformat(), u["id"]))
            elif change == "key":
                store.revoke(u["id"], store.keys(u["id"])[0]["id"])
            elif change == "policy":
                # A different process changes the DB without invalidating this process's TTL cache.
                with admin_store._connect() as db:
                    db.execute("UPDATE unified_settings SET value_json=? WHERE key='access_policy'",
                               (json.dumps({"users": {u["email"]: {"agent_api": False}}}),))
            else:
                with A.app.test_client() as c:
                    assert c.get("/api/v1/limits", headers=auth(control_key)).status_code == 200
                    assert c.post("/api/v1/cancel", json={"request_id": request_id}, headers=auth(control_key)).status_code == 200
    finally:
        if thread.ident:
            thread.join(8)
    assert not thread.is_alive() and len(results) == 1
    assert results[0].status_code == status, results[0].get_data(as_text=True)
    assert store.rate(u["id"])["remaining"] == 30
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM api_waiters WHERE user_id=?", (u["id"],)).fetchone()


def test_full_plus_queue_does_not_evict_for_max_and_timeout_does_not_charge(tmp_path, monkeypatch):
    monkeypatch.setenv("MARX_AGENT_API_QUEUE_WAIT_SECONDS", "1")
    store = Store(tmp_path)
    with store.connect(True) as db:
        for uid in range(8):
            db.execute("INSERT INTO api_waiters VALUES(?,?,?,?,?,?)",
                       (str(uid), uid, "light", 0, time.time(), time.time() + 10))
    with pytest.raises(APIError) as full:
        with store.admission(90, lane="light", priority=2):
            pytest.fail("Max must not displace an existing waiter")
    assert full.value.status == 429 and full.value.retryable
    with store.connect(True) as db:
        assert db.execute("SELECT COUNT(*) FROM api_waiters").fetchone()[0] == 8
        db.execute("DELETE FROM api_waiters")
    monkeypatch.setenv("MARX_AGENT_API_LIGHT_CONCURRENCY", "1")
    with store._admission_nowait(91, lane="light"):
        with pytest.raises(APIError) as timeout:
            with store.admission(92, lane="light"):
                pytest.fail("No execution slot should be available")
    assert timeout.value.status == 429 and store.rate(92)["remaining"] == 30


def test_contract_artifact_and_membership_copy_are_consistent():
    artifact = json.loads((Path(__file__).resolve().parents[1] / "static/agent-api/openapi.json").read_text("utf-8"))
    assert artifact == openapi()
    schema = OUTPUTS["get_api_limits"]["oneOf"][0]["properties"]["scheduling"]["properties"]["priority"]
    assert schema["enum"] == ["normal", "high", "highest"]
    for code, wording in [(PLUS_CODE, "普通排队"), ("support_pro", "优先排队"), ("support_max", "最高优先排队")]:
        plan = public_plan(M.get_plan(code))
        assert "API" in plan["features"] and wording in plan["features"]
        assert "不含 API" not in plan["features"] and "增加 API" not in plan["description"]
