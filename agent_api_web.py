"""REST authentication boundary and session-protected API-key management."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import ExitStack

from flask import Blueprint, g, jsonify, render_template, request, redirect, url_for
from werkzeug.exceptions import HTTPException

from agent_api_service import QueryService
from agent_api_store import APIError, Store, setting, scheduling, QUERY_DEADLINE
from member_catalog import priority as member_priority, api_eligible
from search_capacity import SearchBusy, search_slot

LOG = logging.getLogger(__name__)
HTTP_ERROR_CODES = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                    405: "method_not_allowed", 413: "request_too_large", 429: "rate_limited",
                    503: "service_unavailable", 504: "upstream_timeout"}


def is_api_path(path):
    return path == "/api/v1" or path.startswith("/api/v1/")


def register_routes(app, web):
    from search import _FUZZY_SCAN_SEMAPHORE
    from search_capacity import SEARCH_CONCURRENCY

    if scheduling()["heavy_concurrency"] >= min(SEARCH_CONCURRENCY, _FUZZY_SCAN_SEMAPHORE.limit):
        raise RuntimeError("API heavy concurrency must leave one search and scan slot for the website")
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

    def start_request():
        if is_api_path(request.path):
            g.agent_api_started = time.monotonic()
            g.agent_api_timings = {}
            g.agent_api_executing = False
            request.max_content_length = 16 * 1024

    # Runs before browser hooks, including on unmatched paths and wrong methods.
    app.before_request_funcs.setdefault(None, []).insert(0, start_request)

    def stage(name, started):
        g.agent_api_timings[name] = int(1000 * (time.monotonic() - started))

    def eligible(user):
        return bool(user and user.get("is_active", 1) and api_eligible(web["_membership_plan_code_for_user"](user))
                    and all(web["_feature_effective_for_user"](feature, user) for feature in ("agent_api", "search", "viewer")))

    @api.before_request
    def authenticate():
        started = time.monotonic()
        if os.environ.get("MARX_AGENT_API_ENABLED", "1") != "1":
            raise APIError("disabled", "查询接口暂时维护中。", 503, 30)
        request.max_content_length = 16 * 1024
        key = store.authenticate(request.headers.get("Authorization"))
        user = web["get_user_by_id"](key["user_id"])
        if not eligible(user):
            raise APIError("membership_required", "此接口需要有效会员及检索、正文和 API 权限。", 403)
        g.agent_api_key = key
        g.agent_api_priority = 2 if member_priority(web["_membership_plan_code_for_user"](user)) == 2 else 0
        stage("auth", started)
        # No cookie fallback and no change to g.current_user's browser identity.

    @api.errorhandler(APIError)
    def api_error(exc):
        if getattr(g, "agent_api_executing", False) and exc.status >= 500:
            # The operation may already have run; do not invite duplicate execution.
            exc.retry_after, exc.retryable = 0, False
        g.agent_api_error = exc.code
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
        if exc.response is not None:
            return exc.get_response()  # Keep abort(Response(...)) status and semantic headers.
        status = exc.code or 400
        response = api_error(APIError(HTTP_ERROR_CODES.get(status, "invalid_request"), "请求无效或超过大小限制。", status))
        for name, value in exc.get_headers():
            if name.lower() in {"allow", "www-authenticate", "retry-after"}:
                response.headers[name] = value
        return response

    @api.errorhandler(Exception)
    def internal_error(exc):
        # Exception strings can contain a query/token: log only the class.
        LOG.error("agent_api exception_type=%s", type(exc).__name__)
        return api_error(APIError("internal_error", "接口暂不可用。请提供请求标识联系管理员。", 503))

    @app.after_request
    def finish(response):
        if not is_api_path(request.path):
            return response
        if response.status_code >= 400:
            payload = response.get_json(silent=True) if response.is_json else None
            if not (isinstance(payload, dict) and isinstance(payload.get("error"), dict)
                    and {"code", "message", "retryable", "retry_after_seconds"} <= payload["error"].keys()):
                status = response.status_code
                retry_header = response.headers.get("Retry-After", "")
                retry = int(retry_header) if retry_header.isdigit() and status in {429, 503} else 0
                if getattr(g, "agent_api_executing", False):
                    retry = 0
                    response.headers.pop("Retry-After", None)
                exc = APIError(HTTP_ERROR_CODES.get(status, "internal_error"), "请求未完成，请根据错误代码处理。", status, retry)
                response.set_data(json.dumps(exc.payload(), ensure_ascii=False))
                response.content_type = "application/json"
                g.agent_api_error = exc.code
            if response.status_code == 401:
                response.headers.setdefault("WWW-Authenticate", 'Bearer realm="marx-query-api"')
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        ray = request.headers.get("CF-Ray", "")
        ray = ray if re.fullmatch(r"[0-9a-fA-F]{16,32}-[A-Z]{3}", ray) else ""
        parent = request.headers.get("X-MCP-Request-ID", "")
        parent = parent if re.fullmatch(r"[0-9a-f]{32}", parent) else ""
        LOG.info("agent_api request_id=%s mcp_request_id=%s cf_ray=%s endpoint=%s status=%s reason=%s elapsed_ms=%d stages_ms=%s",
                 getattr(g, "security_request_id", ""), parent, ray, request.endpoint, response.status_code,
                 getattr(g, "agent_api_error", ""),
                 int(1000 * (time.monotonic() - getattr(g, "agent_api_started", time.monotonic()))),
                 json.dumps(getattr(g, "agent_api_timings", {}), sort_keys=True))
        return response

    def execute(fn, *, lane="heavy", mode=None, control=False):
        key = g.agent_api_key
        request_id = request.headers.get("X-Query-Request-ID") or request.headers.get("X-MCP-Request-ID", "")
        if request_id and not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise APIError("invalid_request", "查询请求标识须为 32 位小写十六进制。")
        started = time.monotonic()
        with ExitStack() as stack:
            token = QUERY_DEADLINE.set(started + setting("TIMEOUT_SECONDS", 20))
            stack.callback(QUERY_DEADLINE.reset, token)
            stack.enter_context(store.admission(key["user_id"], lane=lane, near_text=mode == "near_text", control=control,
                                                priority=g.agent_api_priority, cancelled=request.environ.get("waitress.client_disconnected", lambda: False), request_id=request_id))
            from membership import _invalidate_request_membership_cache
            _invalidate_request_membership_cache()
            user = web["get_user_by_id"](key["user_id"])
            if not eligible(user):
                raise APIError("membership_required", "会员或 API 权限已变化。", 403)
            store.authenticate(request.headers.get("Authorization"))
            if lane == "heavy":
                stack.enter_context(search_slot())
            if mode in {"exact", "near_text"}:
                if not _FUZZY_SCAN_SEMAPHORE.acquire(blocking=False):
                    raise SearchBusy()
                stack.callback(_FUZZY_SCAN_SEMAPHORE.release)
            stage("admission", started)
            if not control:
                started = time.monotonic()
                store.rate(key["user_id"], consume=True, key_id=key["id"])
                stage("charge", started)
            g.agent_api_executing = True
            started = time.monotonic()
            try:
                return fn()
            finally:
                stage("execute", started)

    @api.get("/sources")
    def sources():
        return execute(lambda: jsonify(service.sources()), lane="light")

    @api.post("/search")
    def search():
        started = time.monotonic()
        payload = request.get_json(silent=True)
        validated = service.validate_search(payload)
        stage("validate", started)
        return execute(lambda: jsonify(service.search(g.agent_api_key["user_id"], payload, validated=validated)), mode=validated[1])

    @api.get("/passages/<evidence_id>")
    def passage(evidence_id):
        started = time.monotonic()
        if not re.fullmatch("[0-9a-f]{64}", evidence_id):
            raise APIError("invalid_evidence", "证据标识无效。")
        prepared = service.prepare_passage(g.agent_api_key["user_id"], evidence_id)
        stage("validate", started)
        return execute(lambda: jsonify(service.passage(g.agent_api_key["user_id"], evidence_id, prepared=prepared)))

    @api.get("/limits")
    def limits():
        return execute(lambda: jsonify({"ok": True, "status": "available", "global_concurrency": setting("CONCURRENCY", 2),
                        "user_concurrency": setting("USER_CONCURRENCY", 1), "max_results": 20,
                        "query_budget_seconds": setting("TIMEOUT_SECONDS", 20),
                        "rate": store.rate(g.agent_api_key["user_id"]), "daily_quota": None,
                        "consumes_site_ai_tokens": False, "scheduling": scheduling(g.agent_api_priority)}), lane="light", control=True)

    @api.post("/cancel")
    def cancel():
        payload = request.get_json(silent=True) or {}
        request_id = payload.get("request_id", "") if isinstance(payload, dict) else ""
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise APIError("invalid_request", "查询请求标识须为 32 位小写十六进制。")
        def apply():
            store.cancel_waiting(g.agent_api_key["user_id"], request_id)
            return jsonify(ok=True, scope="waiting_only")
        return execute(apply, lane="light", control=True)

    app.register_blueprint(api)
    web["CSRF_EXEMPT_ENDPOINTS"].add("agent_api.search")
    web["CSRF_EXEMPT_ENDPOINTS"].add("agent_api.cancel")

    @app.route("/account/agent-api", methods=["GET", "POST"])
    def agent_api_center():
        if request.method == 'GET':
            return redirect(url_for('api_access'))
        return api_access()

    @app.route('/api-access', methods=['GET', 'POST'])
    def api_access():
        if request.method == 'POST':
            web['_require_login_page']()
        user = getattr(g, 'current_user', None)
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
                                     error=error, keys=store.keys(user["id"]) if user else [],
                                     logged_in=bool(user), layout_v2=True, layout_page='api',
                                     csrf_token=web["_ensure_csrf_token"](),
                                     api_base=service.base_url().rstrip("/"),
                                     concurrency=setting("CONCURRENCY", 2), per_minute=setting("PER_MINUTE", 30)))
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
