"""First-pass integration against the actual Flask application's hooks/routes."""
from pathlib import Path
import json

from _test_env import APPDATA  # Isolate stores before importing the real app.
import app as web


def test_real_application_registration_auth_and_existing_agent_entry(monkeypatch):
    user = {"id": 81231, "email": "api-fixture@example.test", "is_active": 1, "role": "user", "membership_plan_code": "support"}
    monkeypatch.setattr(web, "get_user_by_id", lambda uid: user)
    monkeypatch.setattr(web, "_feature_effective_for_user", lambda *args: True)
    monkeypatch.setattr(web, "_public_book_keys", lambda **kw: {"文集"})
    # register_routes holds the original callable; pin only the public book provider.
    extension = web.app.extensions["agent_api"]
    monkeypatch.setattr(extension["service"], "public_books", lambda: {"文集"})
    token = extension["store"].create_key(user["id"], "actual-app-fixture")
    client = web.app.test_client()
    assert client.post("/api/v1/search", json={"q": "生产关系"}).status_code == 401
    response = client.post("/api/v1/search", json={"q": "生产关系", "limit": 1}, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    assert response.json["results"][0]["book"] == "文集"
    assert client.get("/api/citation-agent/jobs/not-a-job").status_code == 401
    with client.session_transaction() as session:
        session["user_id"] = user["id"]
    page = client.get("/account/agent-api", follow_redirects=True)
    assert page.status_code == 200 and "Codex" in page.get_data(as_text=True)
    assert token.encode() not in page.data
    assert "agent_api.search" in web.CSRF_EXEMPT_ENDPOINTS
    assert "agent_api_center" not in web.CSRF_EXEMPT_ENDPOINTS


def test_workbuddy_credentials_and_openapi_artifact_match_contract():
    from agent_api_contract import openapi
    root = Path(__file__).resolve().parents[1]
    assert json.loads((root / "static/agent-api/openapi.json").read_text("utf-8")) == openapi()
    config = json.loads((root / "docs/agent-api/workbuddy/token-schema.json").read_text("utf-8"))
    assert config["fields"][0]["key"] == "API_KEY" and config["fields"][0]["type"] == "password"
    assert config["docUrl"].endswith("/account/agent-api")
