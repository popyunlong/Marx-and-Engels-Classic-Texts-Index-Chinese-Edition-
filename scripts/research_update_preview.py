"""Read-only loopback preview of candidate research data; all writes are rejected."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, abort, g
from research_routes import register


def create_app(port=8879):
    root = Path(__file__).resolve().parents[1]
    app = Flask(__name__, template_folder=str(root / "templates"), static_folder=str(root / "static"))
    app.secret_key = "local-read-only-preview"
    @app.before_request
    def read_only():
        from flask import request
        if request.method not in {"GET", "HEAD"}:
            abort(405)
        g.current_user = {"id": "preview"}
    for name, path in {"control": "/legacy-admin", "journal_alerts_latest": "/legacy-sample", "account_journal_alerts": "/subscriptions", "pricing": "/pricing"}.items():
        app.add_url_rule(path, name, lambda: "本地只读预览；此入口上线后连接现有网站。")
    register(app, {"_feature_effective_for_user": lambda name: True, "_require_admin": lambda: None,
                   "_ensure_csrf_token": lambda: "preview", "CSRF_EXEMPT_ENDPOINTS": set(),
                   "journal_alert_public_base_url": lambda dep: f"http://127.0.0.1:{port}", "DEPLOYMENT": None})
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8879, debug=False)
