from __future__ import annotations

from types import SimpleNamespace

from flask import Flask, g, request
from jinja2 import DictLoader

import citation_agent_test_web as agent_web


def _test_app(monkeypatch) -> Flask:
    monkeypatch.setattr(agent_web, "_REGISTERED", False)
    monkeypatch.setattr(agent_web.tasks, "list_jobs", lambda _user_id, limit=20: [])

    app = Flask(__name__)
    app.secret_key = "citation-agent-route-test"
    app.jinja_loader = DictLoader({
        "citation_agent_test.html": (
            "access={{ citation_access }}|jobs={{ jobs|length }}|cta={{ access_cta_label }}"
        ),
    })

    @app.before_request
    def inject_user() -> None:
        kind = request.headers.get("X-Test-User", "")
        g.current_user = {
            "member": {"id": 11, "role": "user", "member": True},
            "registered": {"id": 12, "role": "user", "member": False},
            "admin": {"id": 13, "role": "admin", "member": False},
        }.get(kind)

    @app.get("/login")
    def login():
        return "login"

    @app.get("/pricing")
    def pricing():
        return "pricing"

    corpus = SimpleNamespace(books={})
    mylib_corpus = SimpleNamespace(submission_id_from_scope_token=lambda _token: "")
    agent_web.register_routes(app, {
        "APP_NAME": "test",
        "APP_VERSION": "test",
        "corpus": corpus,
        "mylib_corpus": mylib_corpus,
        "_require_admin": lambda: None,
        "_is_admin_user": lambda user: bool(user and user.get("role") == "admin"),
        "_citation_assistant_enabled_for_user": lambda user: bool(
            user and (user.get("member") or user.get("role") == "admin")
        ),
        "_citation_personal_scope_rows": lambda *_args: [],
        "_ensure_csrf_token": lambda: "csrf",
        "_book_scope_tree": lambda: [],
        "_gb2025_template_approved": lambda: False,
        "_citation_corpus_sha256": lambda: "corpus",
        "_citation_template_version": lambda: "template",
    })
    return app


def test_public_preview_and_member_agent_access_are_separated(monkeypatch) -> None:
    app = _test_app(monkeypatch)
    client = app.test_client()

    guest = client.get("/citation-agent")
    assert guest.status_code == 200
    assert b"access=False" in guest.data
    assert "no-store" in guest.headers["Cache-Control"]

    registered = client.get("/citation-agent", headers={"X-Test-User": "registered"})
    assert registered.status_code == 200
    assert b"access=False" in registered.data

    member = client.get("/citation-agent", headers={"X-Test-User": "member"})
    assert member.status_code == 200
    assert b"access=True" in member.data

    assert client.post("/api/citation-agent/jobs").status_code == 401
    assert client.post(
        "/api/citation-agent/jobs", headers={"X-Test-User": "registered"},
    ).status_code == 403
    assert client.post(
        "/api/citation-agent/jobs", headers={"X-Test-User": "member"},
    ).status_code == 400


def test_legacy_admin_page_redirects_to_member_agent(monkeypatch) -> None:
    app = _test_app(monkeypatch)
    response = app.test_client().get(
        "/admin/citation-agent-test", headers={"X-Test-User": "admin"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/citation-agent")


def test_candidate_cutover_blocks_upload_until_current_worker_heartbeat(monkeypatch):
    import json
    import shlex
    from pathlib import Path

    deploy = Path(__file__).resolve().parents[1] / "deploy"
    setting = "CITATION_WORKER_READY_REQUIRED=1"
    assert f"Environment={setting}" in (deploy / "marx-search.service").read_text(encoding="utf-8")
    for transaction in ("promote_release.sh", "rollback_release.sh"):
        source = (deploy / transaction).read_text(encoding="utf-8")
        command = source.split("systemd-run --unit=", 1)[1].split(">/dev/null", 1)[0]
        args = shlex.split(command.replace(chr(92) + chr(10), " "))
        assert "--setenv=" + setting in args

    client = _test_app(monkeypatch).test_client()
    headers = {"X-Test-User": "member"}
    monkeypatch.setenv(*setting.split("=", 1))
    monkeypatch.setattr(agent_web.time, "time", lambda: 2000)
    state = {}

    def read_ready_file(path):
        assert path.as_posix() == "/run/marx-search-citation-agent-test-worker/ready-v2.json"
        if not state:
            raise FileNotFoundError
        return json.dumps(state)

    monkeypatch.setattr(agent_web.Path, "read_text", read_ready_file)
    for protocol, stamp in [(None, None), (1, 1999), (2, 1900)]:
        if protocol is not None:
            state.update(protocol=protocol, time=stamp)
        response = client.post("/api/citation-agent/jobs", headers=headers)
        assert response.status_code == 503
    state.update(protocol=2, time=1999)
    # The request reaches normal upload validation as soon as the new worker is ready.
    assert client.post("/api/citation-agent/jobs", headers=headers).status_code == 400
