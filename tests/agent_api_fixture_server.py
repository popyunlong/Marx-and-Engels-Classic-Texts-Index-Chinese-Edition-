"""Disposable loopback fixture, synthetic corpus only; never starts production app."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import conftest  # Builds an isolated synthetic database under the task TEMP.
from flask import Flask, g, request
from werkzeug.exceptions import Forbidden
from werkzeug.serving import make_server
from agent_api_web import register_routes
from build_index import DB_PATH
from search import Corpus

output = Path(sys.argv[1])
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
app.secret_key = "synthetic-fixture-only"
app.config["PUBLIC_BASE_URL"] = "http://127.0.0.1"
user = {"id": 17, "is_active": 1}
exempt = set()
def csrf():
    if request.form.get("csrf_token") != "fixture": raise Forbidden()
@app.before_request
def browser():
    g.current_user = user
    if request.method == "POST" and request.endpoint not in exempt: csrf()
register_routes(app, {"APPDATA_DIR": output / "state", "CORPUS_INDEX_DB_PATH": Path(DB_PATH), "corpus": Corpus(),
    "_public_book_keys": lambda: {"文集"}, "current_view_state": lambda: {"data_version": "fixture"},
    "current_app_release": lambda: {"git_sha": "fixture"}, "get_user_by_id": lambda uid: user,
    "_membership_plan_code_for_user": lambda u: "member", "_feature_effective_for_user": lambda *args: True,
    "CSRF_EXEMPT_ENDPOINTS": exempt, "_require_login_page": lambda: None,
    "_require_csrf": csrf, "_ensure_csrf_token": lambda: "fixture"})
store = app.extensions["agent_api"]["store"]
key = store.create_key(17, "protocol fixture")
server = make_server("127.0.0.1", 0, app, threaded=True)
app.config["PUBLIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
(output / "fixture.json").write_text(json.dumps({"port": server.server_port, "key": key}), encoding="utf-8")
server.serve_forever()
