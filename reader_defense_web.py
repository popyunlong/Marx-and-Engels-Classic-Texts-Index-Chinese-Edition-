"""Flask integration. Challenge completion never grants business permissions."""
from datetime import datetime, timezone
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request

from flask import abort, g, jsonify, redirect, render_template, request, session, url_for

from reader_security import ReaderSecurity, sensitive_path
from search_capacity import SearchBusy


def safe_return(value):
    value = str(value or "/")
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value and not any(ord(c) < 32 for c in value) else "/"


class ReaderDefense:
    def __init__(self, mod):
        self.mod = mod
        self.engine = ReaderSecurity(mod.APPDATA_DIR / "security.sqlite3", mod.app.secret_key)
        self.engine.alert = self.alert
        self.maintenance_lock = threading.Lock()
        self.last_maintenance = 0
        app = mod.app
        app.before_request(self.begin)
        # Sensitive scans need neither a membership query nor a session write.
        app.before_request_funcs[None].remove(self.begin)
        app.before_request_funcs[None].insert(0, self.begin)
        app.after_request(self.finish)
        app.add_url_rule("/reader/verify", "reader_verify", self.verify, methods=["GET", "POST"])
        app.add_url_rule("/api/reader/security/status", "reader_security_status", self.status)
        app.add_url_rule("/api/admin/reader-security", "admin_reader_security", self.admin, methods=["GET", "POST"])
        app.register_error_handler(SearchBusy, self.busy)

    def config(self):
        value = self.mod.get_setting("reader_defense", {})
        return value if isinstance(value, dict) else {}

    def enabled(self):
        return bool(os.environ.get("READER_TURNSTILE_SITE_KEY") and os.environ.get("READER_TURNSTILE_SECRET_KEY")
                    and urllib.parse.urlsplit(self.mod.DEPLOYMENT.public_base_url).hostname)

    def actor(self):
        user = getattr(g, "current_user", None)
        if user:
            return "user:" + str(user["id"])
        if self.mod._request_presented_session_cookie():
            return "session:" + self.mod._visitor_session_key()
        return "ip:" + self.mod._client_ip()

    def event(self, outcome, rule="", status=0):
        if outcome == "attempt":
            if getattr(g, "security_attempt_logged", False):
                return
            g.security_attempt_logged = True
        self.engine.record(actor=self.actor(), endpoint=request.endpoint or "unmatched",
                           outcome=outcome, rule=rule, status=status,
                           release=self.mod.current_app_release()["id"], request_id=g.security_request_id,
                           start=not self.mod.app.testing)

    def begin(self):
        g.security_request_id = secrets.token_hex(16)
        if sensitive_path(request.path):
            g.security_rule = "sensitive_path"
            self.event("attempt", "sensitive_path")
            return jsonify(ok=False, error="Forbidden", request_id=g.security_request_id), 403
        if request.endpoint in self.mod.READER_ENDPOINTS:
            # Actor attribution happens after load_current_user, in guard().
            g.security_protected = True

    def guard(self):
        user = getattr(g, "current_user", None)
        if self.mod._is_admin_user(user) or self.mod._desktop_content_access_enabled():
            return None
        config = self.config()
        rule, retry = self.engine.assess(self.actor(), self.mod._client_ip(), request.endpoint,
                                         str(request.args.get("file") or request.args.get("sid")
                                             or (request.view_args or {}).get("book_key") or (request.view_args or {}).get("slug")
                                             or (request.view_args or {}).get("book_id")
                                             or (request.view_args or {}).get("submission_id")
                                             or (request.view_args or {}).get("article_id") or "")[:300])
        if rule == "allow":
            return None
        self.event("risk", rule)
        if self.engine.mode(config) != "enforce":
            return None
        # Signed-in accounts still have quotas; challenge is anonymous only.
        challenge = rule in {"volume_traversal", "rotating_ip"} and not user
        if challenge and self.engine.verified(self.mod._visitor_session_key(), config.get("epoch", "0")):
            return None
        g.security_rule = rule
        if challenge and self.enabled():
            target = url_for("reader_verify", next=safe_return(request.full_path))
            response = jsonify(ok=False, error="请完成访问验证后重试。", code="reader_verification_required",
                               verify_url=target, request_id=g.security_request_id)
            response.status_code = 403
            response.headers["X-Reader-Verification"] = target
            # Only top-level HTML navigation may redirect to an interactive page.
            if request.headers.get("Sec-Fetch-Dest") == "document" and request.method == "GET":
                response = redirect(target, 303)
            response.headers["Cache-Control"] = "no-store"
            return response
        response = jsonify(ok=False, error="访问过于频繁，请稍后重试。", code="reader_rate_limited",
                           verification="not_enabled" if not self.enabled() else "available")
        response.status_code = 429
        response.headers["Retry-After"] = str(retry)
        response.headers["Cache-Control"] = "no-store"
        return response

    def pool_ban_eligible(self, ip):
        if self.engine.mode(self.config()) != "enforce":
            return False
        with self.engine.lock:
            state = self.engine.states.get("ip:" + ip)
            return bool(state and state.ban_until > self.engine.clock())

    def maintenance(self):
        if self.mod.app.testing:
            return
        now = time.monotonic()
        if now - self.last_maintenance < 120 or not self.maintenance_lock.acquire(blocking=False):
            return
        self.last_maintenance = now
        def run():
            try:
                with self.mod.app.app_context():
                    self.mod._prune_reader_audit_if_due()
                    self.mod._auto_ban_egregious_scrapers_if_due()
            except Exception:
                self.mod.LOGGER.exception("reader security maintenance failed")
            finally:
                self.maintenance_lock.release()
        threading.Thread(target=run, name="reader-security-maintenance", daemon=True).start()

    def finish(self, response):
        response.headers["X-Request-ID"] = getattr(g, "security_request_id", "")
        rule = getattr(g, "security_rule", "")
        protected = getattr(g, "security_protected", False)
        if protected or rule or response.status_code >= 500:
            if protected:
                self.event("attempt")
            if response.status_code >= 500:
                outcome, rule = "server_error", rule or "server_error"
            elif response.status_code in {401, 403, 429} or rule:
                outcome = "denied"
                rule = rule or "permission_or_quota"
            elif 200 <= response.status_code < 300 and not response.is_streamed:
                outcome = "response_ready"
            elif 200 <= response.status_code < 300:
                # HTTP 200 is NOT proof that a client received a whole file.
                outcome = "delivery_started"
            else:
                outcome = "response"
            self.event(outcome, rule, response.status_code)
            if protected and request.method != "HEAD" and 200 <= response.status_code < 300:
                # The iterator completes only after all application bytes were
                # yielded; disconnect/error records partial delivery instead.
                original = response.response
                actor, endpoint = self.actor(), request.endpoint
                release, rid = self.mod.current_app_release()["id"], g.security_request_id
                engine, background = self.engine, not self.mod.app.testing
                def delivered():
                    complete = False
                    try:
                        yield from original
                        complete = True
                    finally:
                        engine.record(actor=actor, endpoint=endpoint, rule="", status=response.status_code,
                                      outcome="delivery_complete" if complete else "delivery_interrupted",
                                      release=release, request_id=rid, start=background)
                        close = getattr(original, "close", None)
                        if close:
                            close()
                response.response = delivered()
        if response.status_code == 429:
            response.headers.setdefault("Retry-After", "60")
        return response

    def validate(self, token, nonce):
        if not self.enabled() or not token or len(token) > 2048 or not nonce:
            return False
        data = urllib.parse.urlencode({"secret": os.environ["READER_TURNSTILE_SECRET_KEY"],
                                      "response": token, "remoteip": self.mod._client_ip()}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data), timeout=5) as result:
                payload = json.loads(result.read(65536))
            stamp = datetime.fromisoformat(str(payload.get("challenge_ts", "")).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            return bool(payload.get("success") is True and -30 <= age <= 300
                        and payload.get("hostname") == urllib.parse.urlsplit(self.mod.DEPLOYMENT.public_base_url).hostname
                        and payload.get("action") == "reader-defense" and payload.get("cdata") == nonce)
        except Exception:
            return False

    def verify(self):
        self.mod._rate_limit_or_abort("reader-verify:" + self.mod._client_ip(), limit=12,
                                     window_seconds=60, message="验证请求过于频繁，请稍后重试。")
        destination = safe_return(request.values.get("next"))
        error = ""
        if request.method == "POST":
            self.mod._require_csrf()
            nonce = session.pop("_reader_challenge_nonce", "")
            if self.validate(str(request.form.get("cf-turnstile-response") or ""), nonce):
                self.engine.issue(self.mod._visitor_session_key(), self.config().get("epoch", "0"))
                return redirect(destination, 303)
            error = "验证未通过或已过期，请重试。" if self.enabled() else "访问验证尚未启用，请稍后重试。"
        session["_reader_challenge_nonce"] = secrets.token_urlsafe(24)
        response = self.mod.app.make_response((render_template("reader_verify.html", enabled=self.enabled(),
            site_key=os.environ.get("READER_TURNSTILE_SITE_KEY", ""), nonce=session["_reader_challenge_nonce"],
            next_path=destination, error=error, csrf_token=self.mod._ensure_csrf_token()),
            400 if error and self.enabled() else 503 if not self.enabled() else 200))
        response.headers["Cache-Control"] = "no-store"
        return response

    def status(self):
        return jsonify(mode=self.engine.mode(self.config()), verification="enabled" if self.enabled() else "not_enabled")

    def admin(self):
        self.mod._require_management_access(True)
        config = self.config()
        if request.method == "POST":
            self.mod._require_management_csrf()
            data = request.get_json(silent=True) or {}
            if data.get("revoke_all"):
                config["epoch"] = secrets.token_hex(16)
            mode = data.get("mode", config.get("mode", "observe"))
            if mode not in {"observe", "enforce"}:
                abort(400)
            if data.get("mode") == "enforce" and (time.time() - self.engine.observed_since < 86400
                                       or data.get("observation_reviewed") is not True):
                return jsonify(ok=False, error="需观察满 24 小时并明确确认误伤审查。"), 409
            config.update(mode=mode, observation_reviewed=(data.get("observation_reviewed") is True
                          if "mode" in data else bool(config.get("observation_reviewed"))))
            self.mod.set_setting("reader_defense", config, updated_by=self.mod._management_actor_label(True))
            self.mod._log_management_action(action="reader_defense.update", target="reader_defense",
                                            result=mode, remote_admin=True)
        return jsonify(ok=True, mode=self.engine.mode(config), requested_mode=config.get("mode", "observe"),
                       verification="enabled" if self.enabled() else "not_enabled", **self.engine.status())

    def alert(self, text):
        target = str(os.environ.get("SECURITY_ALERT_EMAIL") or self.mod.FEEDBACK_ADMIN_EMAIL or "").strip()
        if target:
            self.mod._send_account_email(target, "网站安全事件汇总", text)

    @staticmethod
    def busy(error):
        response = jsonify(ok=False, busy=True, error=error.description)
        response.status_code = 503
        response.headers["Retry-After"] = "3"
        return response
