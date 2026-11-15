"""Loopback-only fixture for Plus REST/MCP and browser acceptance."""
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import conftest
from membership_refresh_fixture import A, user
from flask import g, jsonify, redirect, session
from werkzeug.serving import make_server

import urllib.request
import smtplib
def no_external(*args, **kwargs):
    raise RuntimeError("External services disabled in fixture")
urllib.request.urlopen = no_external
smtplib.SMTP = no_external
A.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
users = {name: user(code) for name, code in [
    ("plus", "support_plus_20"), ("plus2", "support_plus_20"), ("pro", "support_pro"),
    ("max", "support_max"), ("max2", "support_max"),
    ("basic", "support_basic"), ("oldplus", "support_plus"), ("legacy", "monthly"),
    ("free", None),
]}
store = A.app.extensions["agent_api"]["store"]
keys = {name: store.create_key(u["id"], "synthetic acceptance") for name, u in users.items()}
second_key = store.create_key(users["plus2"]["id"], "second client")
executed = []
pause_user = [None]
entered, release = threading.Event(), threading.Event()
original_sources = A.app.extensions["agent_api"]["service"].sources
def sources():
    uid = g.agent_api_key["user_id"]
    executed.append(uid)
    if uid == pause_user[0]:
        entered.set()
        if not release.wait(8):
            raise RuntimeError("Fixture execution was not released")
    return original_sources()
A.app.extensions["agent_api"]["service"].sources = sources


@A.app.get("/fixture-login/<name>")
def fixture_login(name):
    session["user_id"] = users[name]["id"]
    return redirect("/account")


@A.app.get("/fixture/status")
def status():
    return jsonify(executed=executed, entered=entered.is_set())


@A.app.post("/fixture/pause/<int:uid>")
def pause(uid):
    pause_user[0] = uid
    entered.clear()
    release.clear()
    return jsonify(ok=True)


@A.app.post("/fixture/release")
def release_execution():
    release.set()
    pause_user[0] = None
    return jsonify(ok=True)


@A.app.post("/fixture/shutdown")
def shutdown():
    release.set()
    threading.Thread(target=server.shutdown, daemon=True).start()
    return jsonify(ok=True)


A.CSRF_EXEMPT_ENDPOINTS.update({"pause", "release_execution", "shutdown"})
if __name__ == "__main__":
    server = make_server("127.0.0.1", 0, A.app, threaded=True)
    base = f"http://127.0.0.1:{server.server_port}"
    A.app.config["PUBLIC_BASE_URL"] = base
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(dict(base=base, keys=keys, second_key=second_key,
            users={name: u["id"] for name, u in users.items()}, state=str(store.root))), encoding="utf-8")
    print("READY=" + base, flush=True)
    server.serve_forever()
    server.server_close()
