"""One-pass acceptance of the new defense contract; no live services contacted."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from _test_env import APPDATA  # noqa: F401
import app as web
from reader_security import ReaderSecurity, PROTECTED_ENDPOINTS, ban_active, sensitive_path
from reader_defense_web import safe_return


@pytest.fixture
def engine(tmp_path):
    return ReaderSecurity(tmp_path / "security.sqlite3", "unit-test-secret", capacity=8)


@pytest.fixture
def client(monkeypatch, engine):
    monkeypatch.setitem(web.app.config, "TESTING", True)
    monkeypatch.setattr(web.reader_defense, "engine", engine)
    monkeypatch.setattr(web, "_is_monitoring_request", lambda: False)
    web.set_setting("reader_defense", {})
    web.delete_setting("reader_bans")
    web._rate_buckets.clear()
    yield web.app.test_client()
    web.delete_setting("reader_defense")
    web.delete_setting("reader_bans")


def test_legacy_bans_and_new_expiry():
    assert ban_active({"banned_at": "2026-06-08"}, now=9999999999)
    assert ban_active("old format", now=9999999999)
    assert ban_active({"expires_at": "invalid"})
    assert not ban_active({"expires_at": "2026-10-01T00:00:00Z"}, now=1800000000)
    assert ban_active({"expires_at": "2030-01-01T00:00:00Z"}, now=1800000000)


@pytest.mark.parametrize("path", ["/.env", "/backend/.env", "/.git/config", "/service_account.json", "/GC-SERVICE.JSON"])
def test_sensitive_components(path):
    assert sensitive_path(path)


@pytest.mark.parametrize("path", ["/books/environment.pdf", "/book.git", "/.well-known/acme-challenge/token", "/service_account.json.pdf"])
def test_normal_components(path):
    assert not sensitive_path(path)


@pytest.mark.parametrize("ua", ["meta-webindexer/1.1", "Meta-WebIndexer/9.7", "GPTBot/1.2"])
@pytest.mark.parametrize("endpoint,path", [("reader_cover", "/reader/cover?file=never.pdf"), ("page_image", "/page-image?file=never.pdf&page=1"), ("pdf_viewer", "/viewer?file=never.pdf")])
def test_bots_denied_before_any_resource_io(client, monkeypatch, endpoint, path, ua):
    monkeypatch.setitem(web.app.view_functions, endpoint, lambda: pytest.fail("resource handler reached"))
    response = client.get(path, headers={"User-Agent": ua})
    assert response.status_code == 403
    assert response.headers["X-Request-ID"]


def test_ip_ban_cover_and_no_raw_denied_audit(client, monkeypatch):
    web.set_setting("reader_bans", {"ips": {"8.8.8.8": {"banned_at": "2026-09-18"}}})
    monkeypatch.setattr(web, "_record_reader_access_event", lambda **kw: pytest.fail("denied raw audit write"))
    monkeypatch.setitem(web.app.view_functions, "reader_cover", lambda: pytest.fail("resource read"))
    response = client.get("/reader/cover?file=x", headers={"User-Agent": "Mozilla/5.0", "CF-Connecting-IP": "8.8.8.8"})
    assert response.status_code == 403


def test_untrusted_proxy_headers_do_not_change_actor():
    with web.app.test_request_context("/", headers={"CF-Connecting-IP": "8.8.8.8", "X-Forwarded-For": "1.1.1.1"},
                                      environ_base={"REMOTE_ADDR": "9.9.9.9"}):
        assert web._client_ip() == "9.9.9.9"
    with web.app.test_request_context("/", headers={"CF-Connecting-IP": "not-an-ip", "X-Forwarded-For": "spoof, 8.8.4.4"},
                                      environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert web._client_ip() == "8.8.4.4"


def test_observe_requires_day_and_review(engine):
    config = {"mode": "enforce", "observation_reviewed": True}
    assert engine.mode(config) == "observe"
    engine.observed_since -= 86401
    assert engine.mode(config) == "enforce"
    assert engine.mode({"mode": "enforce"}) == "observe"


def test_cross_endpoint_traversal_rotation_and_shared_nat(engine):
    for i in range(120):
        rule, retry = engine.assess("session:one", f"8.8.8.{i % 10}", f"endpoint{i % 2}", f"book{i % 40}")
    assert rule == "volume_traversal" and retry == 60
    # A different authenticated reader at the same exit is independent.
    assert engine.assess("user:two", "8.8.8.1", "viewer", "book1") == ("allow", 0)
    for i in range(160):
        rule, _ = engine.assess("session:rotating", f"8.8.4.{i % 12}", "viewer", f"book{i % 15}")
    assert rule == "rotating_ip"
    for i in range(20):
        engine.assess(str(i), "8.8.8.1", "viewer", "one")
    assert len(engine.states) == 8 and engine.metrics["state_evictions"] > 0


def test_verified_session_expiry_revoke_and_epoch(engine):
    clock = [100000.0]
    engine.clock = lambda: clock[0]
    engine.issue("session-a", "epoch1")
    assert engine.verified("session-a", "epoch1")
    assert not engine.verified("session-b", "epoch1")
    assert not engine.verified("session-a", "epoch2")
    clock[0] += 1801
    assert not engine.verified("session-a", "epoch1")
    engine.issue("session-a", "epoch1")
    engine.revoke("session-a")
    assert not engine.verified("session-a", "epoch1")


def test_journal_aggregates_without_raw_identity_and_counts_overflow(tmp_path):
    engine = ReaderSecurity(tmp_path / "audit.sqlite3", "secret", queue_size=2)
    for rid in ("one", "two", "three"):
        engine.record(actor="ip:8.8.8.8", endpoint="cover", rule="bot", outcome="denied", status=403, request_id=rid, start=False)
    assert engine.metrics["queue_dropped"] == 1
    engine.flush()
    with engine.connect() as conn:
        row = conn.execute("SELECT actor,count,first_request,last_request FROM security_events").fetchone()
        assert row[0] != "ip:8.8.8.8" and row[1:] == (2, "one", "two")
        assert conn.execute("SELECT count FROM security_daily").fetchone()[0] == 2


def test_missing_keys_never_verify_or_hide_disabled(client, monkeypatch):
    monkeypatch.delenv("READER_TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("READER_TURNSTILE_SECRET_KEY", raising=False)
    assert not web.reader_defense.validate("token", "nonce")
    assert client.get("/api/reader/security/status").json["verification"] == "not_enabled"
    response = client.get("/reader/verify")
    assert response.status_code == 503 and "尚未启用" in response.text


def test_siteverify_hostname_action_nonce_and_replay(client, monkeypatch, engine):
    monkeypatch.setenv("READER_TURNSTILE_SITE_KEY", "dedicated-site")
    monkeypatch.setenv("READER_TURNSTILE_SECRET_KEY", "dedicated-secret")
    response = client.get("/reader/verify?next=/reader/cover")
    assert response.status_code == 200
    with client.session_transaction() as sess:
        csrf, nonce = sess["_csrf_token"], sess["_reader_challenge_nonce"]
    payload = {"success": True, "hostname": "example.test", "action": "reader-defense", "cdata": nonce,
               "challenge_ts": datetime.now(timezone.utc).isoformat()}
    class Result:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return json.dumps(payload).encode()
    monkeypatch.setattr("reader_defense_web.urllib.request.urlopen", lambda *a, **kw: Result())
    with web.app.test_request_context("/reader/verify", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        payload["hostname"] = "attacker.example"
        assert not web.reader_defense.validate("token", nonce)
        payload["hostname"], payload["action"] = "example.test", "login"
        assert not web.reader_defense.validate("token", nonce)
        payload["action"] = "reader-defense"
        assert not web.reader_defense.validate("token", "different-session")
    response = client.post("/reader/verify", data={"csrf_token": csrf, "cf-turnstile-response": "token", "next": "/reader/cover"})
    assert response.status_code == 303
    with client.session_transaction() as sess:
        assert engine.verified(sess["_visitor_key"], "0")
    response = client.post("/reader/verify", data={"csrf_token": csrf, "cf-turnstile-response": "token"})
    assert response.status_code == 400


def test_abnormal_api_json_and_observe_normal_flow(client, monkeypatch, engine):
    monkeypatch.setitem(web.app.view_functions, "reader_cover", lambda: (b"image", 200, {"Content-Type": "image/png"}))
    monkeypatch.setattr(engine, "assess", lambda *a: ("volume_traversal", 60))
    headers = {"User-Agent": "Mozilla/5.0"}
    assert client.get("/reader/cover", headers=headers).status_code == 200
    engine.observed_since -= 86401
    web.set_setting("reader_defense", {"mode": "enforce", "observation_reviewed": True})
    monkeypatch.setenv("READER_TURNSTILE_SITE_KEY", "site")
    monkeypatch.setenv("READER_TURNSTILE_SECRET_KEY", "secret")
    response = client.get("/reader/cover", headers=headers)
    assert response.status_code == 403 and response.is_json
    assert response.json["code"] == "reader_verification_required"
    assert response.headers["X-Reader-Verification"].startswith("/reader/verify?")
    monkeypatch.setattr(engine, "assess", lambda *a: ("rate", 60))
    response = client.get("/reader/cover", headers=headers)
    assert response.status_code == 429 and response.headers["Retry-After"] == "60"


def test_non_reader_payment_sse_are_outside_challenge_registry():
    assert PROTECTED_ENDPOINTS <= set(web.app.view_functions)
    assert {"wenku_raw_versioned", "liushi_raw_versioned", "journal_alerts_asset"} <= PROTECTED_ENDPOINTS
    for endpoint in ("zpay_notify", "zpay_return", "api_ai_search_chat", "api_ai_pdf_chat_stream", "api_search_associative"):
        assert endpoint not in PROTECTED_ENDPOINTS
    assert safe_return("//attacker.test/") == "/"
    assert safe_return("/\\attacker.test") == "/"


@pytest.mark.parametrize("ua", ["curl/8.0", "Python-urllib/3.10"])
def test_catalog_navigation_stays_available_for_health_observation(client, monkeypatch, ua):
    # /v2/read is public navigation, used by release and catalogue observers.
    # Actual page images/raw text/downloads retain their resource guards.
    assert "layout_read" not in PROTECTED_ENDPOINTS
    monkeypatch.setitem(web.app.view_functions, "layout_read", lambda: "public catalog")
    monkeypatch.setattr(web, "_record_reader_access_event", lambda **kw: pytest.fail("catalog treated as reader asset"))
    response = client.get("/v2/read", headers={"User-Agent": ua})
    assert response.status_code == 200 and response.text == "public catalog"


def test_busy_response_and_started_sse_error():
    from search_capacity import SearchBusy
    with web.app.test_request_context("/api/search"):
        response = web.reader_defense.busy(SearchBusy())
        assert response.status_code == 503 and response.json["busy"]
        def fail(_): raise SearchBusy()
        stream = "".join(web._sse_run_with_heartbeat(fail, lambda *args: pytest.fail("finalizer should not swallow busy"),
                                                  progress_message="test", heartbeat_interval=0.01))
        assert "event: error" in stream and "<html" not in stream


def test_ban_delta_preserves_manual_and_legacy_records():
    old = {str(i): {"banned_at": "2026-09-18"} for i in range(605)}
    web.set_setting("reader_bans", {"ips": old})
    expected = web._reader_bans()
    web.update_reader_ban_records({"ips": {"new": {"banned_by": "admin"}}}, updated_by="admin")
    web.update_reader_ban_records({"ips": {"new": {"banned_by": "auto"}, "auto": {"expires_at": "2030-01-01"}}},
                                  expected=expected, updated_by="auto")
    result = web._reader_bans()["ips"]
    assert all(result[key] == value for key, value in old.items())
    assert result["new"]["banned_by"] == "admin" and "auto" in result
    web.delete_setting("reader_bans")
