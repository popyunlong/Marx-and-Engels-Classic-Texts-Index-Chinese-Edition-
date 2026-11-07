"""REST authentication boundary and session-protected API-key management."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from functools import wraps

from flask import Blueprint, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from agent_api_service import QueryService
from agent_api_store import APIError, Store, setting
from search_capacity import SearchBusy

LOG = logging.getLogger(__name__)


def register_routes(app, web):
    store = Store(os.environ.get("MARX_AGENT_API_STATE_DIR") or web["APPDATA_DIR"])

    def version():
        path = web["CORPUS_INDEX_DB_PATH"]
        stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
        catalog = getattr(web["corpus"], "catalog", None)
        layout = getattr(web["corpus"], "layout_index", None)
        payload = {"database": stamp, "catalog": getattr(catalog, "version", ""),
                   "data": web["current_view_state"]().get("data_version"),
                   "release": web["current_app_release"]().get("git_sha"),
                   "templates": web["corpus"].citation_templates,
                   "layout": str(getattr(layout, "revision", ""))}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    service = QueryService(web["corpus"], store, web["_public_book_keys"], version,
                           lambda: app.config.get("PUBLIC_BASE_URL") or request.url_root)
    app.extensions["agent_api"] = {"store": store, "service": service}
    api = Blueprint("agent_api", __name__, url_prefix="/api/v1")

    def eligible(user):
        return bool(user and user.get("is_active", 1) and web["_membership_plan_code_for_user"](user)
                    and all(web["_feature_effective_for_user"](feature, user) for feature in ("agent_api", "search", "viewer")))

    @api.before_request
    def authenticate():
        g.agent_api_started = time.monotonic()
        if os.environ.get("MARX_AGENT_API_ENABLED", "1") != "1":
            raise APIError("disabled", "查询接口暂时维护中。", 503, 30)
        request.max_content_length = 16 * 1024
        key = store.authenticate(request.headers.get("Authorization"))
        user = web["get_user_by_id"](key["user_id"])
        if not eligible(user):
            raise APIError("membership_required", "此接口需要有效会员及检索、正文和 API 权限。", 403)
        g.agent_api_key = key
        # No cookie fallback and no change to g.current_user's browser identity.

    @api.errorhandler(APIError)
    def api_error(exc):
        response = jsonify(exc.payload())
        response.status_code = exc.status
        if exc.retry_after:
            response.headers["Retry-After"] = str(exc.retry_after)
        if exc.status == 401:
            response.headers["WWW-Authenticate"] = 'Bearer realm="marx-query-api"'
        return response

    @api.errorhandler(SearchBusy)
    def search_busy(exc):
        return api_error(APIError("search_busy", "检索资源暂忙，请稍后重试。", 503, 2))

    @api.errorhandler(HTTPException)
    def http_error(exc):
        return api_error(APIError("invalid_request", "请求无效或超过大小限制。", exc.code or 400))

    @api.errorhandler(Exception)
    def internal_error(exc):
        # Exception strings can contain a query/token: log only the class.
        LOG.error("agent_api exception_type=%s", type(exc).__name__)
        return api_error(APIError("internal_error", "接口暂不可用，请稍后重试。", 503, 2))

    @api.after_request
    def finish(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        LOG.info("agent_api endpoint=%s status=%s elapsed_ms=%d", request.endpoint, response.status_code,
                 int(1000 * (time.monotonic() - getattr(g, "agent_api_started", time.monotonic()))))
        return response

    def admitted(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            key = g.agent_api_key
            with store.admission(key["user_id"]):
                store.rate(key["user_id"], consume=True, key_id=key["id"])
                return fn(*args, **kwargs)
        return wrapped

    @api.get("/sources")
    @admitted
    def sources():
        return jsonify(service.sources())

    @api.post("/search")
    @admitted
    def search():
        return jsonify(service.search(g.agent_api_key["user_id"], request.get_json(silent=True)))

    @api.get("/passages/<evidence_id>")
    @admitted
    def passage(evidence_id):
        if not re.fullmatch("[0-9a-f]{64}", evidence_id):
            raise APIError("invalid_evidence", "证据标识无效。")
        return jsonify(service.passage(g.agent_api_key["user_id"], evidence_id))

    @api.get("/limits")
    def limits():
        return jsonify({"ok": True, "status": "available", "global_concurrency": setting("CONCURRENCY", 1),
                        "user_concurrency": setting("USER_CONCURRENCY", 1), "max_results": 20,
                        "query_budget_seconds": setting("TIMEOUT_SECONDS", 20),
                        "rate": store.rate(g.agent_api_key["user_id"]), "daily_quota": None,
                        "consumes_site_ai_tokens": False})

    app.register_blueprint(api)
    web["CSRF_EXEMPT_ENDPOINTS"].add("agent_api.search")

    @app.route("/account/agent-api", methods=["GET", "POST"])
    def agent_api_center():
        web["_require_login_page"]()
        user = g.current_user
        allowed, new_key, error = eligible(user), "", ""
        if request.method == "POST":
            web["_require_csrf"]()
            try:
                action = request.form.get("action", "")
                if action == "revoke":
                    # Lapsed members must still be able to revoke credentials.
                    store.revoke(user["id"], request.form.get("key_id", ""))
                elif not allowed:
                    raise APIError("membership_required", "当前账号没有 API 使用资格。", 403)
                elif action in {"create", "rotate"}:
                    if action == "rotate" and not request.form.get("key_id"):
                        raise APIError("invalid_key", "请选择要重新生成的 Key。")
                    new_key = store.create_key(user["id"], request.form.get("name"),
                                               request.form.get("key_id") if action == "rotate" else None)
                else:
                    raise APIError("invalid_action", "操作无效。")
            except APIError as exc:
                error = str(exc)
        response = app.make_response(render_template("agent_api.html", allowed=allowed, new_key=new_key,
                                     error=error, keys=store.keys(user["id"]), csrf_token=web["_ensure_csrf_token"](),
                                     api_base=service.base_url().rstrip("/"),
                                     concurrency=setting("CONCURRENCY", 1), per_minute=setting("PER_MINUTE", 30)))
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
