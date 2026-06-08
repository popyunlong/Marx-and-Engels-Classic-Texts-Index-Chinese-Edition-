from __future__ import annotations

import atexit
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import warnings
from dataclasses import replace
from urllib.parse import parse_qs, urlparse


warnings.filterwarnings("ignore", category=ResourceWarning)
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-search-security-")
atexit.register(lambda: shutil.rmtree(_TMP_APPDATA, ignore_errors=True))
os.environ["APPDATA"] = _TMP_APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["ZPAY_PID"] = "test-pid"
os.environ["ZPAY_KEY"] = "test-secret"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
import journal_alerts  # noqa: E402
from membership import (  # noqa: E402
    create_account_email_token,
    create_manual_subscription,
    create_pending_order,
    create_user,
    get_user_by_email,
    get_order_by_no,
    list_orders_for_user,
    list_subscriptions_for_user,
)
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.set_setting("access_policy", {})
        app_module.delete_setting("reader_bans")
        app_module.delete_setting("journal_alerts_settings")
        app_module._rate_buckets.clear()
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("DELETE FROM reader_access_events")
            conn.commit()
        with sqlite3.connect(app_module.JOURNAL_ALERTS_DB_PATH) as conn:
            conn.execute("DELETE FROM journal_delivery_logs")
            conn.execute("DELETE FROM journal_articles")
            conn.execute("DELETE FROM journal_subscriptions")
            conn.commit()
        with sqlite3.connect(app_module.FEEDBACK_DB_PATH) as conn:
            conn.execute("DELETE FROM feedback_messages")
            conn.execute("DELETE FROM feedback_threads")
            conn.commit()
        self.client = app_module.app.test_client()

    def _csrf_from(self, path: str) -> str:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        if match is None:
            match = re.search(r"const csrfToken = \"([^\"]+)\";", html)
        self.assertIsNotNone(match)
        return match.group(1)

    def _create_active_member(self, email: str, password: str = "correct horse battery staple") -> dict:
        user = create_user(
            email=email,
            display_name=email.split("@", 1)[0],
            password_hash=generate_password_hash(password),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        create_manual_subscription(user_email=email, plan_code="monthly", note="test")
        return user

    def _login(self, email: str, password: str = "correct horse battery staple") -> None:
        token = self._csrf_from("/login")
        response = self.client.post(
            "/login",
            data={"csrf_token": token, "email": email, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def _force_login_user_id(self, user_id: int) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = int(user_id)

    def _create_admin(self, email: str) -> dict:
        user = create_user(
            email=email,
            display_name="admin",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (int(user["id"]),))
            conn.commit()
        return user

    def test_admin_2fa_skipped_when_email_unconfigured(self) -> None:
        # 安全底线：发信邮箱未配置时管理员二次验证必须自动跳过，绝不把管理员锁在门外。
        admin = self._create_admin("admin-2fa-off@example.test")
        self._force_login_user_id(int(admin["id"]))
        self.assertFalse(app_module._admin_2fa_enabled())
        resp = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    def test_admin_2fa_gate_and_email_code(self) -> None:
        admin = self._create_admin("admin-2fa-on@example.test")
        self._force_login_user_id(int(admin["id"]))
        original = app_module._admin_2fa_enabled
        app_module._admin_2fa_enabled = lambda: True
        try:
            # 启用二次因子后，进入后台先被拦到 /admin/2fa。
            gate = self.client.get("/admin", follow_redirects=False)
            self.assertEqual(gate.status_code, 302)
            self.assertIn("/admin/2fa", gate.headers.get("Location", ""))
            # 注入一个已知验证码，带 csrf POST 验证通过后放行进后台。
            token = self._csrf_from("/admin/2fa")
            app_module.create_account_email_token(
                email="admin-2fa-on@example.test", purpose="admin_2fa", code="123456", ttl_minutes=15
            )
            verify = self.client.post(
                "/admin/2fa",
                data={"csrf_token": token, "action": "verify", "code": "123456", "next": "/admin"},
                follow_redirects=False,
            )
            self.assertEqual(verify.status_code, 302)
            self.assertIn("/admin", verify.headers.get("Location", ""))
            # 验证通过后再访问后台不再被拦。
            ok = self.client.get("/admin", follow_redirects=False)
            self.assertEqual(ok.status_code, 200)
        finally:
            app_module._admin_2fa_enabled = original

    def test_sql_injection_user_search_parameterized(self) -> None:
        from membership import list_users

        self._create_active_member("inj-a@example.test")
        self._create_active_member("inj-b@example.test")
        for payload in ["' OR 1=1--", "x'; DROP TABLE users;--", "%' UNION SELECT 1,2,3--", '" OR "1"="1']:
            rows = list_users(search_text=payload, limit=50)
            self.assertIsInstance(rows, list)  # 参数化：不抛 sqlite3 异常、不崩
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("users", tables)  # DROP TABLE 注入未执行
        hit = list_users(search_text="inj-a", limit=50)
        self.assertTrue(any(r["email"] == "inj-a@example.test" for r in hit))  # 正常搜索仍工作

    def test_sql_injection_feedback_body_safe(self) -> None:
        self._create_active_member("inj-fb@example.test")
        self._login("inj-fb@example.test")
        token = self._csrf_from("/")
        resp = self.client.post(
            "/api/feedback/messages",
            data={"csrf_token": token, "body": "boom'); DROP TABLE feedback_messages;-- 注入测试"},
        )
        self.assertLess(resp.status_code, 500)  # 参数化：不应 5xx
        with sqlite3.connect(app_module.FEEDBACK_DB_PATH) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("feedback_messages", tables)  # 表仍在

    def test_login_post_requires_csrf(self) -> None:
        response = self.client.post("/login", data={"email": "nobody@example.test", "password": "bad"})
        self.assertEqual(response.status_code, 403)

    def test_login_post_accepts_csrf(self) -> None:
        token = self._csrf_from("/login")
        response = self.client.post(
            "/login",
            data={"csrf_token": token, "email": "nobody@example.test", "password": "bad"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("邮箱或密码不正确", response.get_data(as_text=True))

    def test_security_headers_and_security_txt(self) -> None:
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertIn("Permissions-Policy", response.headers)
        security = self.client.get("/.well-known/security.txt")
        self.assertIn("Contact:", security.get_data(as_text=True))

    def test_guest_search_returns_summary_without_context(self) -> None:
        token = self._csrf_from("/")
        response = self.client.post(
            "/api/search",
            json={"q": "马克思", "group_page": 1},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["access_level"], "summary")
        raw = response.get_data(as_text=True)
        self.assertNotIn('"context"', raw)
        self.assertNotIn('"citation"', raw)
        self.assertNotIn('"source_file"', raw)

    def test_member_search_book_filter_contract(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"registered": {"search": True, "library": True, "ai": True}}},
        )
        self._create_active_member("book-filter@example.test")
        self._login("book-filter@example.test")
        token = self._csrf_from("/")

        response = self.client.post(
            "/api/search",
            json={"q": "帝国主义", "group_page": 1},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("book_counts", payload)
        self.assertIn("book_filter", payload)
        self.assertEqual(payload["book_filter"], "")
        self.assertGreaterEqual(len(payload["book_counts"]), 2)

        selected = payload["book_counts"][-1]["key"]
        filtered = self.client.post(
            "/api/search",
            json={"q": "帝国主义", "group_page": 1, "book": selected},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(filtered.status_code, 200)
        filtered_payload = filtered.get_json()
        self.assertTrue(filtered_payload["ok"])
        self.assertEqual(filtered_payload["book_filter"], selected)
        self.assertTrue(filtered_payload["book_counts"])
        for result in filtered_payload["results"]:
            if filtered_payload["display_mode"] == "direct":
                self.assertEqual(result["book"], selected)
            else:
                self.assertEqual(result["book"], selected)

    def test_member_search_viewer_url_carries_highlight_text(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"registered": {"search": True, "library": True, "ai": True}}},
        )
        self._create_active_member("highlight-url@example.test")
        self._login("highlight-url@example.test")
        token = self._csrf_from("/")

        query = "\u8bba\u7cae\u98df\u7a0e"
        response = self.client.post(
            "/api/search",
            json={"q": query, "group_page": 1},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        if payload["display_mode"] == "direct":
            hit = payload["results"][0]
        elif payload["display_mode"] == "volume_chaptered":
            # 短词海量命中：命中详情改为按篇章按需拉取，校验该接口的视图链接同样携带高亮文本。
            volume = payload["results"][0]
            chapter = volume["chapters"][0]
            self.assertTrue(chapter.get("hits_lazy"))
            chapter_resp = self.client.post(
                "/api/search/chapter-hits",
                json={
                    "q": query,
                    "source_file": volume["source_file"],
                    "chapter_pdf_page": chapter["chapter_pdf_page"],
                    "page": 1,
                },
                headers={"X-CSRF-Token": token},
            )
            self.assertEqual(chapter_resp.status_code, 200)
            chapter_payload = chapter_resp.get_json()
            self.assertTrue(chapter_payload["ok"])
            hit = chapter_payload["hits"][0]
        else:
            hit = payload["results"][0]["hits"][0]
        parsed = urlparse(hit["viewer_url"])
        params = parse_qs(parsed.query)
        self.assertEqual(params.get("q"), [query])
        self.assertEqual(params.get("h"), [hit["highlight_text"]])

        viewer = self.client.get(hit["viewer_url"])
        self.assertEqual(viewer.status_code, 200)
        self.assertIn("const highlightQueryText =", viewer.get_data(as_text=True))

    def test_zpay_notify_rejects_param_tamper_and_is_idempotent(self) -> None:
        user = create_user(
            email="payer@example.test",
            display_name="Payer",
            password_hash=generate_password_hash("correct horse battery staple"),
        )
        order = create_pending_order(user_id=int(user["id"]), plan_code="monthly")
        money = f"{int(order['amount_cents']) / 100:.2f}"

        tampered = {
            "pid": "test-pid",
            "out_trade_no": order["order_no"],
            "trade_no": "trade-1",
            "type": "alipay",
            "name": "test",
            "money": money,
            "trade_status": "TRADE_SUCCESS",
            "param": f"user:{int(user['id']) + 1}",
        }
        tampered["sign"] = app_module.PAYMENT_CLIENT.sign(tampered)
        tampered["sign_type"] = "MD5"
        self.assertEqual(self.client.get("/payments/zpay/notify", query_string=tampered).get_data(as_text=True), "failure")
        self.assertEqual(get_order_by_no(order["order_no"])["status"], "pending")

        valid = dict(tampered)
        valid["param"] = f"user:{user['id']}"
        valid["sign"] = app_module.PAYMENT_CLIENT.sign(valid)
        self.assertEqual(self.client.get("/payments/zpay/notify", query_string=valid).get_data(as_text=True), "success")
        self.assertEqual(self.client.get("/payments/zpay/notify", query_string=valid).get_data(as_text=True), "success")
        self.assertEqual(get_order_by_no(order["order_no"])["status"], "paid")
        self.assertEqual(len(list_subscriptions_for_user(int(user["id"]))), 1)

    def test_pending_orders_are_reused_and_duplicate_pending_orders_are_expired(self) -> None:
        user = create_user(
            email="pending-order@example.test",
            display_name="Pending Order",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        first = create_pending_order(user_id=int(user["id"]), plan_code="monthly")
        second = create_pending_order(user_id=int(user["id"]), plan_code="monthly")
        self.assertEqual(first["order_no"], second["order_no"])

        yearly = create_pending_order(user_id=int(user["id"]), plan_code="yearly")
        orders = list_orders_for_user(int(user["id"]))
        pending = [order for order in orders if order["status"] == "pending"]
        self.assertEqual({order["plan_code"] for order in pending}, {"monthly", "yearly"})
        self.assertEqual(len(pending), 2)
        self.assertIn(yearly["order_no"], {order["order_no"] for order in pending})

    def test_legacy_account_login_backfills_email_verified_at(self) -> None:
        user = create_user(
            email="legacy-login@example.test",
            display_name="Legacy Login",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="",
        )
        self.assertFalse(user["email_verified_at"])
        self._login("legacy-login@example.test")
        refreshed = get_user_by_email("legacy-login@example.test")
        self.assertTrue(refreshed["email_verified_at"])

    def test_format_datetime_displays_beijing_time(self) -> None:
        self.assertEqual(app_module._display_datetime("2026-06-04T07:29:04+00:00"), "2026-06-04 15:29")
        self.assertEqual(app_module._display_datetime("2026-06-04"), "2026-06-04")

    def test_registration_requires_valid_email_code(self) -> None:
        token = self._csrf_from("/register")
        email = "verified-register@example.test"
        response = self.client.post(
            "/register",
            data={
                "csrf_token": token,
                "action": "register",
                "display_name": "Verified",
                "email": email,
                "password": "correct horse battery staple",
                "confirm_password": "correct horse battery staple",
                "email_code": "000000",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(get_user_by_email(email))

        create_account_email_token(email=email, purpose="register", code="123456", ttl_minutes=15)
        response = self.client.post(
            "/register",
            data={
                "csrf_token": token,
                "action": "register",
                "display_name": "Verified",
                "email": email,
                "password": "correct horse battery staple",
                "confirm_password": "correct horse battery staple",
                "email_code": "123456",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        user = get_user_by_email(email)
        self.assertIsNotNone(user)
        self.assertTrue(user["email_verified_at"])

    def test_register_code_rate_limit_is_less_aggressive(self) -> None:
        original_send = app_module._send_registration_code
        original_email_configured = app_module._account_email_configured
        sent: list[str] = []
        app_module._send_registration_code = lambda email: sent.append(email)
        app_module._account_email_configured = lambda: True
        try:
            for index in range(60):
                token = self._csrf_from("/register")
                response = self.client.post(
                    "/register",
                    data={
                        "csrf_token": token,
                        "action": "send_code",
                        "display_name": "Rate",
                        "email": "rate-limit-code@example.test",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("验证码发送过于频繁", response.get_data(as_text=True))

            token = self._csrf_from("/register")
            blocked = self.client.post(
                "/register",
                data={
                    "csrf_token": token,
                    "action": "send_code",
                    "display_name": "Rate",
                    "email": "rate-limit-code@example.test",
                },
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(len(sent), 60)
        finally:
            app_module._send_registration_code = original_send
            app_module._account_email_configured = original_email_configured

    def test_turnstile_missing_config_is_not_shown_to_visitors(self) -> None:
        original_enabled = os.environ.get("TURNSTILE_ENABLED")
        original_site_key = os.environ.get("TURNSTILE_SITE_KEY")
        original_secret_key = os.environ.get("TURNSTILE_SECRET_KEY")
        os.environ["TURNSTILE_ENABLED"] = "1"
        os.environ.pop("TURNSTILE_SITE_KEY", None)
        os.environ.pop("TURNSTILE_SECRET_KEY", None)
        try:
            register = self.client.get("/register").get_data(as_text=True)
            login = self.client.get("/login").get_data(as_text=True)
        finally:
            if original_enabled is None:
                os.environ.pop("TURNSTILE_ENABLED", None)
            else:
                os.environ["TURNSTILE_ENABLED"] = original_enabled
            if original_site_key is None:
                os.environ.pop("TURNSTILE_SITE_KEY", None)
            else:
                os.environ["TURNSTILE_SITE_KEY"] = original_site_key
            if original_secret_key is None:
                os.environ.pop("TURNSTILE_SECRET_KEY", None)
            else:
                os.environ["TURNSTILE_SECRET_KEY"] = original_secret_key
        self.assertNotIn("人机验证尚未配置", register)
        self.assertNotIn("当前仅启用频率限制", register)
        self.assertNotIn("人机验证尚未配置", login)
        self.assertNotIn("当前仅启用频率限制", login)

    def test_turnstile_configured_blocks_register_without_token(self) -> None:
        original_enabled = os.environ.get("TURNSTILE_ENABLED")
        original_site_key = os.environ.get("TURNSTILE_SITE_KEY")
        original_secret_key = os.environ.get("TURNSTILE_SECRET_KEY")
        original_verify = app_module._verify_turnstile_response
        os.environ["TURNSTILE_ENABLED"] = "1"
        os.environ["TURNSTILE_SITE_KEY"] = "site-key"
        os.environ["TURNSTILE_SECRET_KEY"] = "secret-key"
        app_module._verify_turnstile_response = lambda token: False
        try:
            token = self._csrf_from("/register")
            email = "turnstile-register@example.test"
            create_account_email_token(email=email, purpose="register", code="123456", ttl_minutes=15)
            response = self.client.post(
                "/register",
                data={
                    "csrf_token": token,
                    "action": "register",
                    "display_name": "Turnstile",
                    "email": email,
                    "password": "correct horse battery staple",
                    "confirm_password": "correct horse battery staple",
                    "email_code": "123456",
                },
            )
        finally:
            app_module._verify_turnstile_response = original_verify
            if original_enabled is None:
                os.environ.pop("TURNSTILE_ENABLED", None)
            else:
                os.environ["TURNSTILE_ENABLED"] = original_enabled
            if original_site_key is None:
                os.environ.pop("TURNSTILE_SITE_KEY", None)
            else:
                os.environ["TURNSTILE_SITE_KEY"] = original_site_key
            if original_secret_key is None:
                os.environ.pop("TURNSTILE_SECRET_KEY", None)
            else:
                os.environ["TURNSTILE_SECRET_KEY"] = original_secret_key
        self.assertEqual(response.status_code, 200)
        self.assertIn("人机验证未通过", response.get_data(as_text=True))
        self.assertIsNone(get_user_by_email(email))

    def test_password_reset_token_updates_password_once(self) -> None:
        user = create_user(
            email="reset@example.test",
            display_name="Reset",
            password_hash=generate_password_hash("old password value"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        reset = create_account_email_token(
            email=user["email"],
            user_id=int(user["id"]),
            purpose="password_reset",
            ttl_minutes=30,
        )
        token = self._csrf_from(f"/reset-password/{reset['token']}")
        response = self.client.post(
            f"/reset-password/{reset['token']}",
            data={
                "csrf_token": token,
                "password": "new password value",
                "confirm_password": "new password value",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        updated = get_user_by_email("reset@example.test")
        self.assertTrue(check_password_hash(updated["password_hash"], "new password value"))

        token2 = self._csrf_from("/login")
        response = self.client.post(
            f"/reset-password/{reset['token']}",
            data={
                "csrf_token": token2,
                "password": "second password value",
                "confirm_password": "second password value",
            },
        )
        self.assertIn(response.status_code, {400, 403, 200})
        updated_again = get_user_by_email("reset@example.test")
        self.assertTrue(check_password_hash(updated_again["password_hash"], "new password value"))

    def test_journal_alert_member_can_subscribe_and_confirm(self) -> None:
        user = self._create_active_member("journal-member@example.test")
        self._login("journal-member@example.test")
        sent: list[dict] = []
        original_send = app_module.send_confirmation_email
        original_load_smtp = app_module.load_smtp_config
        app_module.load_smtp_config = lambda: journal_alerts.SMTPConfig(
            host="smtp.example.test",
            port=587,
            username="",
            password="",
            from_email="notice@example.test",
            from_name="Test",
            use_tls=False,
        )
        app_module.send_confirmation_email = lambda subscription, base_url, smtp_config: sent.append(subscription)
        try:
            token = self._csrf_from("/account/journal-alerts")
            response = self.client.post(
                "/account/journal-alerts/subscribe",
                data={"csrf_token": token, "email": "journal-member-alerts@example.test"},
                follow_redirects=False,
            )
        finally:
            app_module.send_confirmation_email = original_send
            app_module.load_smtp_config = original_load_smtp

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["user_id"], user["id"])
        self.assertEqual(sent[0]["status"], "pending")
        confirmed = app_module.confirm_subscription(sent[0]["confirm_token"])
        self.assertIsNotNone(confirmed)
        self.assertEqual(confirmed["status"], "active")

    def test_journal_alert_user_override_denies_page_and_post(self) -> None:
        self._create_active_member("journal-denied@example.test")
        app_module.set_setting(
            "access_policy",
            {"users": {"journal-denied@example.test": {"journal_alerts": False}}},
        )
        self._login("journal-denied@example.test")

        page = self.client.get("/account/journal-alerts")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("暂未开放期刊提醒权限", html)
        self.assertNotIn("发送确认邮件</button>", html)

        token = self._csrf_from("/account")
        response = self.client.post(
            "/account/journal-alerts/subscribe",
            data={"csrf_token": token, "email": "journal-denied@example.test"},
        )
        self.assertEqual(response.status_code, 403)

    def test_journal_alert_worker_skips_denied_user(self) -> None:
        user = self._create_active_member("journal-worker-denied@example.test")
        subscription = journal_alerts.create_or_update_subscription(int(user["id"]), "journal-worker-denied@example.test")
        journal_alerts.confirm_subscription(subscription["confirm_token"])
        app_module.set_setting(
            "access_policy",
            {"users": {"journal-worker-denied@example.test": {"journal_alerts": False}}},
        )
        source = journal_alerts.list_journal_sources(limit=1)[0]
        journal_alerts.upsert_article(
            source,
            {
                "journal_name": "Test Journal",
                "language": "zh",
                "title": "Denied delivery article",
                "abstract": "Denied abstract",
                "authors": ["Author A"],
                "doi": "10.5555/denied-delivery",
                "url": "https://example.test/denied-delivery",
                "published_at": "2026-05-24",
            },
        )

        sent: list[tuple] = []
        original_send = journal_alerts.send_email
        journal_alerts.send_email = lambda *args, **kwargs: sent.append(args)
        try:
            count = journal_alerts.deliver_ready_articles(
                base_url="https://example.test",
                smtp_config=journal_alerts.SMTPConfig(
                    host="smtp.example.test",
                    port=587,
                    username="",
                    password="",
                    from_email="notice@example.test",
                    from_name="Test",
                    use_tls=False,
                ),
            )
        finally:
            journal_alerts.send_email = original_send

        self.assertEqual(count, 0)
        self.assertEqual(sent, [])
        logs = journal_alerts.list_recent_delivery_logs(limit=1)
        self.assertEqual(logs[0]["status"], "skipped")
        self.assertIn("权限", logs[0]["error"])

    def test_journal_alert_daily_digest_sends_once_and_dedupes(self) -> None:
        user = self._create_active_member("journal-digest@example.test")
        subscription = journal_alerts.create_or_update_subscription(int(user["id"]), "journal-digest@example.test")
        journal_alerts.confirm_subscription(subscription["confirm_token"])
        source = journal_alerts.list_journal_sources(limit=1)[0]
        for suffix in ("one", "two"):
            journal_alerts.upsert_article(
                source,
                {
                    "journal_name": "Test Journal",
                    "language": "zh",
                    "title": f"Digest article {suffix}",
                    "abstract": f"Digest abstract {suffix}",
                    "authors": ["Author A"],
                    "doi": f"10.5555/digest-{suffix}",
                    "url": f"https://example.test/digest-{suffix}",
                    "published_at": "2026-05-24",
                },
            )

        sent: list[dict] = []
        original_send = journal_alerts.send_email

        def fake_send(config, to_email, subject, text_body, html_body="") -> None:
            sent.append(
                {
                    "to_email": to_email,
                    "subject": subject,
                    "text_body": text_body,
                    "html_body": html_body,
                }
            )

        journal_alerts.send_email = fake_send
        smtp_config = journal_alerts.SMTPConfig(
            host="smtp.example.test",
            port=587,
            username="",
            password="",
            from_email="notice@example.test",
            from_name="Test",
            use_tls=False,
        )
        try:
            first_count = journal_alerts.deliver_ready_articles(base_url="https://example.test", smtp_config=smtp_config)
            second_count = journal_alerts.deliver_ready_articles(base_url="https://example.test", smtp_config=smtp_config)
        finally:
            journal_alerts.send_email = original_send

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to_email"], "journal-digest@example.test")
        self.assertEqual(sent[0]["subject"], "期刊新文每日摘要：2 篇新文章")
        self.assertIn("Digest article one", sent[0]["text_body"])
        self.assertIn("Digest article two", sent[0]["text_body"])

    def test_journal_alert_digest_content_is_configurable(self) -> None:
        user = self._create_active_member("journal-configurable@example.test")
        subscription = journal_alerts.create_or_update_subscription(int(user["id"]), "journal-configurable@example.test")
        journal_alerts.confirm_subscription(subscription["confirm_token"])
        app_module.set_setting(
            "journal_alerts_settings",
            {
                "subject_prefix": "自定义摘要",
                "intro_text": "今日可读：",
                "include_title": True,
                "include_journal": False,
                "include_authors": False,
                "include_published_at": False,
                "include_abstract": False,
                "include_citation": False,
                "include_url": False,
            },
        )
        source = journal_alerts.list_journal_sources(limit=1)[0]
        journal_alerts.upsert_article(
            source,
            {
                "journal_name": "Hidden Journal",
                "language": "zh",
                "title": "Configurable article",
                "abstract": "Hidden abstract",
                "authors": ["Hidden Author"],
                "doi": "10.5555/configurable",
                "url": "https://example.test/configurable",
                "published_at": "2026-05-24",
            },
        )

        sent: list[dict] = []
        original_send = journal_alerts.send_email
        journal_alerts.send_email = lambda config, to_email, subject, text_body, html_body="": sent.append(
            {"subject": subject, "text_body": text_body}
        )
        try:
            journal_alerts.deliver_ready_articles(
                base_url="https://example.test",
                smtp_config=journal_alerts.SMTPConfig(
                    host="smtp.example.test",
                    port=587,
                    username="",
                    password="",
                    from_email="notice@example.test",
                    from_name="Test",
                    use_tls=False,
                ),
            )
        finally:
            journal_alerts.send_email = original_send

        self.assertEqual(sent[0]["subject"], "自定义摘要：1 篇新文章")
        self.assertIn("今日可读：", sent[0]["text_body"])
        self.assertIn("Configurable article", sent[0]["text_body"])
        self.assertNotIn("Hidden abstract", sent[0]["text_body"])
        self.assertNotIn("Hidden Author", sent[0]["text_body"])

    def test_default_journal_source_backfill_fills_old_blank_defaults(self) -> None:
        with sqlite3.connect(app_module.JOURNAL_ALERTS_DB_PATH) as conn:
            conn.execute(
                """
                UPDATE journal_sources
                SET source_type = 'manual', issn = '', source_url = '', config_json = '{}'
                WHERE name = '马克思主义研究'
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO journal_sources(
                    name, language, issn, source_type, source_url, config_json,
                    is_enabled, created_at, updated_at
                )
                VALUES('自定义期刊', 'zh', '', 'manual', 'https://custom.example.test', '{}', 1, '2026-01-01', '2026-01-01')
                """
            )
            conn.commit()

        changed = journal_alerts.backfill_default_journal_sources()
        self.assertGreaterEqual(changed, 1)
        sources = {source["name"]: source for source in journal_alerts.list_journal_sources(limit=240)}
        self.assertEqual(sources["马克思主义研究"]["issn"], "1006-5199")
        self.assertEqual(sources["马克思主义研究"]["source_type"], "web_html")
        self.assertEqual(sources["自定义期刊"]["source_url"], "https://custom.example.test")

    def test_web_html_articles_require_review_before_delivery(self) -> None:
        user = self._create_active_member("journal-review@example.test")
        subscription = journal_alerts.create_or_update_subscription(int(user["id"]), "journal-review@example.test")
        journal_alerts.confirm_subscription(subscription["confirm_token"])
        # 马克思主义研究 默认走 NCPSSD 网页抓取且未标记为可信来源，应进入人工审核。
        source = next(
            source for source in journal_alerts.list_journal_sources(limit=240) if source["name"] == "马克思主义研究"
        )
        self.assertFalse(source["config"].get("auto_publish"))
        articles = journal_alerts._parse_qstheory_html(
            """
            <html><body>
              <ul>
                <li><a href="/20260524/example/c.html">坚持用马克思主义观察时代</a><span>2026-05-24</span></li>
              </ul>
            </body></html>
            """,
            source,
            "https://www.qstheory.cn/dukan/qs/",
        )
        self.assertEqual(len(articles), 1)
        self.assertTrue(articles[0]["requires_review"])
        article, created = journal_alerts.upsert_article(source, articles[0])
        self.assertTrue(created)
        self.assertEqual(article["status"], "pending_review")

        sent: list[tuple] = []
        original_send = journal_alerts.send_email
        journal_alerts.send_email = lambda *args, **kwargs: sent.append(args)
        smtp_config = journal_alerts.SMTPConfig(
            host="smtp.example.test",
            port=587,
            username="",
            password="",
            from_email="notice@example.test",
            from_name="Test",
            use_tls=False,
        )
        try:
            before_approve = journal_alerts.deliver_ready_articles(
                base_url="https://example.test",
                smtp_config=smtp_config,
            )
            journal_alerts.update_article_review_status(int(article["id"]), "ready")
            after_approve = journal_alerts.deliver_ready_articles(
                base_url="https://example.test",
                smtp_config=smtp_config,
            )
        finally:
            journal_alerts.send_email = original_send

        self.assertEqual(before_approve, 0)
        self.assertEqual(after_approve, 1)
        self.assertEqual(len(sent), 1)

    def test_trusted_web_source_auto_publishes_and_delivers(self) -> None:
        user = self._create_active_member("journal-trusted@example.test")
        subscription = journal_alerts.create_or_update_subscription(int(user["id"]), "journal-trusted@example.test")
        journal_alerts.confirm_subscription(subscription["confirm_token"])
        # 求是默认标记为可信来源，网页抓取的文章应直接进入发送队列，无需人工审核。
        source = next(source for source in journal_alerts.list_journal_sources(limit=240) if source["name"] == "求是")
        self.assertTrue(source["config"].get("auto_publish"))
        articles = journal_alerts._parse_qstheory_html(
            """
            <html><body><ul>
              <li><a href="/20260524/example/c.html">坚持用马克思主义观察时代</a><span>2026-05-24</span></li>
            </ul></body></html>
            """,
            source,
            "https://www.qstheory.cn/dukan/qs/",
        )
        self.assertEqual(len(articles), 1)
        self.assertFalse(articles[0]["requires_review"])
        article, created = journal_alerts.upsert_article(source, articles[0])
        self.assertTrue(created)
        self.assertEqual(article["status"], "ready")

        sent: list[tuple] = []
        original_send = journal_alerts.send_email
        journal_alerts.send_email = lambda *args, **kwargs: sent.append(args)
        try:
            delivered = journal_alerts.deliver_ready_articles(
                base_url="https://example.test",
                smtp_config=journal_alerts.SMTPConfig(
                    host="smtp.example.test",
                    port=587,
                    username="",
                    password="",
                    from_email="notice@example.test",
                    from_name="Test",
                    use_tls=False,
                ),
            )
        finally:
            journal_alerts.send_email = original_send
        self.assertEqual(delivered, 1)
        self.assertEqual(len(sent), 1)

    def test_ncpssd_journal_parser_extracts_articles(self) -> None:
        source = {
            "name": "马克思主义研究",
            "language": "zh",
            "source_type": "web_html",
            "config": {"parser": "ncpssd_journal", "gch": "80453X", "auto_publish": False},
        }
        fixture = (
            "<dl>"
            "<a onclick=\"openDetail('/Literature/articleinfo?id=MKSZYYJ2026004003"
            "&type=journalArticle&typename=中文期刊文章&nav=1&langType=1')\" "
            "href=\"javascript:void (0)\" title='“两个结合”是构建中国自主知识体系的根本路径'> "
            "“两个结合”是构建中国自主知识体系的根本路径 </a>"
            "<span class='writer' title='鲁品越[1,2]'>鲁品越[1,2]</span>"
            "<span class='pages'>(18-31)</span>"
            "</dl>"
        )
        articles = journal_alerts._parse_ncpssd_journal_html(
            fixture, source, "https://www.ncpssd.cn/journal/details?gch=80453X"
        )
        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["title"], "“两个结合”是构建中国自主知识体系的根本路径")
        self.assertEqual(article["authors"], ["鲁品越"])
        self.assertEqual(article["issue"], "4")
        self.assertEqual(article["pages"], "18-31")
        self.assertEqual(article["published_at"], "2026-01-01")
        self.assertTrue(article["requires_review"])
        self.assertIn("articleinfo?id=MKSZYYJ2026004003", article["url"])

    def test_generic_parser_link_selector_filters_anchors(self) -> None:
        source = {
            "name": "示例期刊",
            "language": "zh",
            "source_type": "web_html",
            "config": {"parser": "generic", "link_selector": "/content/"},
        }
        fixture = (
            "<html><body>"
            "<a href=\"/about/联系我们页面说明\">关于本刊的详细介绍信息</a>"
            "<a href=\"/content/2026/05/真正的文章标题示例\">真正的文章标题示例内容</a>"
            "</body></html>"
        )
        articles = journal_alerts._parse_generic_article_list(fixture, source, "https://example.test/")
        self.assertEqual(len(articles), 1)
        self.assertIn("/content/", articles[0]["url"])

    def test_force_publish_and_approve_all_unblock_pending(self) -> None:
        source = next(
            s for s in journal_alerts.list_journal_sources(limit=240) if s["name"] == "马克思主义研究"
        )
        base = {
            "journal_name": "马克思主义研究",
            "language": "zh",
            "abstract": "摘要",
            "authors": ["作者甲"],
            "published_at": "2026-05-01",
            "requires_review": True,
        }
        # 普通抓取进入待审。
        pending, _ = journal_alerts.upsert_article(
            source, {**base, "title": "待审文章一", "url": "https://www.ncpssd.cn/Literature/articleinfo?id=T1"}
        )
        self.assertEqual(pending["status"], "pending_review")
        # force_publish 直接就绪。
        ready, _ = journal_alerts.upsert_article(
            source,
            {**base, "title": "自动发文章", "url": "https://www.ncpssd.cn/Literature/articleinfo?id=T2"},
            force_publish=True,
        )
        self.assertEqual(ready["status"], "ready")
        # 一键批准把剩余待审全部转为就绪。
        approved = journal_alerts.approve_all_pending_articles()
        self.assertGreaterEqual(approved, 1)
        self.assertEqual(len(journal_alerts.list_articles_by_status("pending_review", limit=10)), 0)

    def test_global_auto_publish_setting_round_trips(self) -> None:
        app_module.set_setting("journal_alerts_settings", {"auto_publish_all": True})
        self.assertTrue(journal_alerts.load_alert_settings()["auto_publish_all"])
        app_module.set_setting("journal_alerts_settings", {"auto_publish_all": False})
        self.assertFalse(journal_alerts.load_alert_settings()["auto_publish_all"])

    def test_send_frequency_settings_round_trip(self) -> None:
        app_module.set_setting(
            "journal_alerts_settings", {"send_frequency": "monthly", "send_weekday": 3, "lookback_days": 7}
        )
        settings = journal_alerts.load_alert_settings()
        self.assertEqual(settings["send_frequency"], "monthly")
        self.assertEqual(settings["send_weekday"], 3)
        self.assertEqual(settings["lookback_days"], 7)
        # 非法值回落到默认 / 边界裁剪。
        app_module.set_setting(
            "journal_alerts_settings", {"send_frequency": "hourly", "send_weekday": 99, "lookback_days": 9999}
        )
        settings = journal_alerts.load_alert_settings()
        self.assertEqual(settings["send_frequency"], "weekly")
        self.assertEqual(settings["send_weekday"], 6)
        self.assertEqual(settings["lookback_days"], 365)

    def test_is_send_due_respects_frequency_and_weekday(self) -> None:
        from datetime import datetime, timezone

        monday_utc = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # 北京时间周一
        self.assertTrue(journal_alerts.is_send_due({"send_frequency": "daily"}, now=monday_utc))
        self.assertTrue(
            journal_alerts.is_send_due({"send_frequency": "weekly", "send_weekday": 0}, now=monday_utc)
        )
        self.assertFalse(
            journal_alerts.is_send_due({"send_frequency": "weekly", "send_weekday": 1}, now=monday_utc)
        )

    def test_apply_article_detail_fills_abstract_and_citation(self) -> None:
        source = next(
            s for s in journal_alerts.list_journal_sources(limit=240) if s["name"] == "马克思主义研究"
        )
        article, _ = journal_alerts.upsert_article(
            source,
            {
                "journal_name": "马克思主义研究",
                "language": "zh",
                "title": "测试待补全文章",
                "authors": [],
                "url": "https://www.ncpssd.cn/Literature/articleinfo?id=TESTABS1",
                "metadata": {"ncpssd_id": "TESTABS1"},
                "requires_review": True,
            },
        )
        self.assertEqual(article["abstract"], "")
        updated = journal_alerts._apply_article_detail(
            article,
            {"abstract": "这是补全的中文摘要。", "authors": ["张三"], "pages": "1-10", "doi": "", "issue": "4", "published_at": "2026-01-01"},
        )
        self.assertEqual(updated["abstract"], "这是补全的中文摘要。")
        self.assertEqual(updated["authors"], ["张三"])
        self.assertIn("张三", updated["citation_gb2015"])

    def test_new_english_journals_present_in_catalog(self) -> None:
        names = {s["name"] for s in journal_alerts.journal_source_catalog()["en"]}
        for expected in ("Cambridge Journal of Economics", "New Political Economy", "Economy and Society"):
            self.assertIn(expected, names)

    def test_rss_fixture_parses_articles(self) -> None:
        articles = journal_alerts._parse_rss_text(
            """
            <rss><channel>
              <item>
                <title>RSS Article</title>
                <link>https://example.test/rss-article</link>
                <pubDate>2026-05-24</pubDate>
                <description>RSS abstract</description>
              </item>
            </channel></rss>
            """,
            {"name": "教学与研究", "language": "zh", "source_type": "rss"},
            "https://example.test/feed.xml",
        )
        self.assertEqual(articles[0]["title"], "RSS Article")
        self.assertFalse(articles[0]["requires_review"])

    def test_pricing_page_shows_journal_catalog(self) -> None:
        response = self.client.get("/pricing")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("可订阅期刊目录", html)
        self.assertIn("马克思主义研究", html)
        self.assertIn("Historical Materialism", html)

    def test_library_toc_suggest_returns_results_with_book_and_viewer_url(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"registered": {"search": True, "library": True, "ai": True}}},
        )
        self._create_active_member("toc-suggest@example.test")
        self._login("toc-suggest@example.test")
        resp = self.client.get(
            "/api/library/toc-suggest", query_string={"q": "共产党宣言", "mode": "ai"}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(len(data["results"]), 1)
        self.assertTrue(all("/viewer" in r["url"] for r in data["results"]))
        # 书库是配置驱动的（文集/全集/列宁全集等），断言结果落在已配置书库内，
        # 而非硬编码两套书，避免新增书库时误报。
        valid_books = set(app_module.BOOK_CONFIG_BY_KEY)
        self.assertTrue(all(r["book"] in valid_books for r in data["results"]))
        # 过短查询返回空。
        short = self.client.get("/api/library/toc-suggest", query_string={"q": "x"})
        self.assertEqual(short.get_json()["results"], [])

    def test_library_toc_suggest_prefers_configured_book_order(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"registered": {"search": True, "library": True, "ai": True}}},
        )
        self._create_active_member("toc-order@example.test")
        self._login("toc-order@example.test")

        resp = self.client.get(
            "/api/library/toc-suggest",
            query_string={"q": "\u8d44\u672c\u8bba", "mode": "ai"},
        )
        self.assertEqual(resp.status_code, 200)
        results = resp.get_json()["results"]
        self.assertGreaterEqual(len(results), 3)
        orders = [app_module._book_sort_order(row["book"]) for row in results]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(results[0]["book"], "\u6587\u96c6")

    def test_library_toc_suggest_collapses_exact_large_title_matches(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"registered": {"search": True, "library": True, "ai": True}}},
        )
        self._create_active_member("toc-exact@example.test")
        self._login("toc-exact@example.test")

        query = "\u5171\u4ea7\u515a\u5ba3\u8a00"
        resp = self.client.get(
            "/api/library/toc-suggest",
            query_string={"q": query, "mode": "ai"},
        )
        self.assertEqual(resp.status_code, 200)
        results = resp.get_json()["results"]
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(all(app_module._toc_norm(row["title"]) == app_module._toc_norm(query) for row in results))
        self.assertTrue(all("\u63d2\u56fe" not in row["title"] for row in results))

    def test_homepage_chapter_search_guest_reader_then_member_ai(self) -> None:
        app_module.set_setting(
            "access_policy",
            {
                "audience": {
                    "guest": {"search": True, "library": True, "ai": False},
                    "registered": {"search": True, "library": True, "ai": True},
                }
            },
        )
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("篇章直达", html)
        self.assertIn('id="chapterSearchInput"', html)
        self.assertIn('data-mode="reader"', html)  # 访客落到全文阅读器
        self.assertIn("全文阅读器", html)

        self._create_active_member("chapter-ai@example.test")
        self._login("chapter-ai@example.test")
        html2 = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-mode="ai"', html2)  # 登录用户落到 AI 导学
        self.assertIn("AI 导学阅读器", html2)

    def test_homepage_chapter_search_disabled_prompts_login(self) -> None:
        app_module.set_setting(
            "access_policy",
            {
                "audience": {
                    "guest": {"search": True, "library": False, "ai": False},
                    "registered": {"search": True, "library": True, "ai": True},
                }
            },
        )
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("篇章直达", html)
        self.assertNotIn('id="chapterSearchInput"', html)  # 不可用时无功能输入框
        self.assertIn("登录后即可使用", html)
        self.assertIn("去登录", html)

    def test_member_plan_permission_denies_library_access(self) -> None:
        self._create_active_member("library-denied@example.test")
        app_module.set_setting(
            "access_policy",
            {"plans": {"monthly": {"library": False}}},
        )
        self._login("library-denied@example.test")
        response = self.client.get("/library", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pricing", response.headers["Location"])

    def test_reader_entries_explain_registered_access_to_guests(self) -> None:
        app_module.set_setting(
            "access_policy",
            {
                "audience": {
                    "guest": {"search": True, "library": False, "ai": False},
                    "registered": {"search": True, "library": True, "ai": True},
                }
            },
        )
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("全文阅读器", html)
        self.assertIn("AI 导学阅读器", html)
        self.assertGreaterEqual(html.count("登录即可使用"), 2)
        self.assertIn("/login?next=/reader", html)
        self.assertIn("/login?next=/library", html)

    def test_reader_entries_link_guest_library_when_allowed(self) -> None:
        app_module.set_setting(
            "access_policy",
            {
                "audience": {
                    "guest": {"search": True, "library": True, "ai": False},
                    "registered": {"search": True, "library": True, "ai": False},
                }
            },
        )
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("全文阅读器", html)
        self.assertIn('href="/reader"', html)
        self.assertIn("进入全文阅读", html)

    def test_basic_reader_entry_preserves_non_ai_viewer_mode(self) -> None:
        app_module.set_setting(
            "access_policy",
            {
                "audience": {
                    "guest": {"search": True, "library": True, "ai": False},
                    "registered": {"search": True, "library": True, "ai": True},
                }
            },
        )
        reader = self.client.get("/reader")
        self.assertEqual(reader.status_code, 200)
        self.assertIn("mode=reader", reader.get_data(as_text=True))

        volume = app_module.corpus.get_volumes("文集")[0]
        viewer = self.client.get(f"/viewer?file={volume.source_file}&page=1&mode=reader")
        self.assertEqual(viewer.status_code, 200)
        html = viewer.get_data(as_text=True)
        self.assertIn('<body class="mode-image reader-basic">', html)
        self.assertIn("ai-upsell", html)
        self.assertIn('const viewerMode = "reader";', html)
        self.assertNotIn('id="usePageTextBtn"', html)

    def test_ai_reader_entry_keeps_ai_viewer_tools(self) -> None:
        app_module.set_setting(
            "access_policy",
            {
                "audience": {
                    "guest": {"search": True, "library": True, "ai": False},
                    "registered": {"search": True, "library": True, "ai": True},
                }
            },
        )
        library = self.client.get("/library")
        self.assertEqual(library.status_code, 200)
        self.assertNotIn("mode=reader", library.get_data(as_text=True))

        volume = app_module.corpus.get_volumes("文集")[0]
        viewer = self.client.get(f"/viewer?file={volume.source_file}&page=1&mode=ai")
        self.assertEqual(viewer.status_code, 200)
        html = viewer.get_data(as_text=True)
        self.assertIn('<body class="mode-image">', html)
        self.assertIn('id="usePageTextBtn"', html)
        self.assertIn('const viewerMode = "ai";', html)

    def test_reader_entries_explain_membership_requirement(self) -> None:
        app_module.set_setting(
            "access_policy",
            {
                "audience": {
                    "guest": {"search": True, "library": False, "ai": False},
                    "registered": {"search": True, "library": False, "ai": False},
                },
                "plans": {"monthly": {"library": True, "ai": True}},
            },
        )
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("开通会员后使用", html)
        self.assertIn("/pricing?next=/reader", html)
        self.assertIn("/pricing?next=/library", html)

    def test_reader_entries_explain_unavailable_features(self) -> None:
        app_module.set_setting(
            "access_policy",
            {
                "global": {"library": False, "ai": False},
                "audience": {
                    "guest": {"search": True, "library": False, "ai": False},
                    "registered": {"search": True, "library": False, "ai": False},
                },
                "plans": {"monthly": {"library": False, "ai": False}},
            },
        )
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("暂未开放", html)

    def test_guest_pages_do_not_expose_operator_copy(self) -> None:
        pages = ["/", "/register", "/login", "/pricing"]
        forbidden = ["人机验证尚未配置", "API Key", "PUBLIC_BASE_URL", "异步通知", "config/"]
        for path in pages:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            for text in forbidden:
                self.assertNotIn(text, html, msg=f"{path} exposed {text}")

    def test_ai_locked_pages_do_not_show_default_model(self) -> None:
        self._create_active_member("ai-denied@example.test")
        app_module.set_setting(
            "access_policy",
            {"plans": {"monthly": {"ai": False, "viewer": True, "library": True}}},
        )
        self._login("ai-denied@example.test")

        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        index_html = index.get_data(as_text=True)
        self.assertIn("未开放", index_html)
        self.assertIn("/reader", index_html)
        self.assertNotIn("glm-5.1", index_html)

        reader = self.client.get("/reader")
        self.assertEqual(reader.status_code, 200)
        self.assertIn("点击任一条目即可进入全文阅读器", reader.get_data(as_text=True))

        runtime = self.client.get("/api/ai/runtime")
        self.assertEqual(runtime.status_code, 200)
        payload = runtime.get_json()
        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["model"], "")
        self.assertEqual(payload["status"], "locked")

        volume = app_module.corpus.get_volumes("文集")[0]
        viewer = self.client.get(f"/viewer?file={volume.source_file}&page=1")
        self.assertEqual(viewer.status_code, 200)
        viewer_html = viewer.get_data(as_text=True)
        self.assertIn("未开放", viewer_html)
        self.assertIn("AI 导学示例", viewer_html)
        self.assertNotIn("glm-5.1", viewer_html)

        token = self._csrf_from("/")
        denied = self.client.post(
            "/api/ai/pdf-chat",
            json={"source_file": volume.source_file, "page": 1, "question": "test"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(denied.status_code, 403)

    def test_reader_anomaly_dashboard_lists_suspicious_ip(self) -> None:
        admin = create_user(
            email="reader-audit-admin@example.test",
            display_name="Reader Audit Admin",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        app_module.update_user_account(int(admin["id"]), role="admin", is_active=True)
        day = app_module.china_day_text()
        for page in range(1, 205):
            app_module.record_reader_access_event(
                session_key="scan-session",
                user_id=None,
                client_ip="203.0.113.8",
                user_agent="python-requests/2",
                endpoint="page_image",
                method="GET",
                path=f"/page-image?file=test.pdf&page={page}",
                source_file="test.pdf",
                page=page,
                day=day,
            )

        self._login("reader-audit-admin@example.test")
        overview = self.client.get(f"/admin?date={day}")
        self.assertEqual(overview.status_code, 200)
        html = overview.get_data(as_text=True)
        self.assertIn("203.0.113.8", html)
        self.assertIn("reader-access-detail-btn", html)

        detail = self.client.get(f"/admin/reader-access?date={day}&actor=ip:203.0.113.8")
        self.assertEqual(detail.status_code, 200)
        payload = detail.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["request_count"], 160)

    def test_reader_user_ban_blocks_reader_routes_and_unban_restores_default(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"registered": {"search": True, "library": True, "ai": True}}},
        )
        member = self._create_active_member("reader-ban-user@example.test")
        admin = create_user(
            email="reader-ban-admin@example.test",
            display_name="Reader Ban Admin",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        app_module.update_user_account(int(admin["id"]), role="admin", is_active=True)

        self._force_login_user_id(int(admin["id"]))
        token = self._csrf_from("/admin/members")
        response = self.client.post(
            "/admin/reader-access-ban",
            data={
                "csrf_token": token,
                "actor_type": "user",
                "user_id": str(member["id"]),
                "email": member["email"],
                "action": "ban",
            },
        )
        self.assertEqual(response.status_code, 302)

        self._force_login_user_id(int(member["id"]))
        for path in ("/reader", "/library", "/viewer?file=anything.pdf&page=1", "/page-image?file=anything.pdf&page=1"):
            denied = self.client.get(path)
            self.assertEqual(denied.status_code, 403, msg=path)

        self._force_login_user_id(int(admin["id"]))
        token = self._csrf_from("/admin/members")
        response = self.client.post(
            "/admin/reader-access-ban",
            data={
                "csrf_token": token,
                "actor_type": "user",
                "user_id": str(member["id"]),
                "email": member["email"],
                "action": "unban",
            },
        )
        self.assertEqual(response.status_code, 302)
        self._force_login_user_id(int(member["id"]))
        allowed = self.client.get("/reader")
        self.assertEqual(allowed.status_code, 200)

    def test_reader_ip_ban_blocks_guest_only_and_admin_endpoint_requires_admin(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"guest": {"search": True, "library": True, "ai": False}}},
        )
        guest_client = app_module.app.test_client()
        before = guest_client.get("/reader", environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(before.status_code, 200)

        admin = create_user(
            email="reader-ip-admin@example.test",
            display_name="Reader IP Admin",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        app_module.update_user_account(int(admin["id"]), role="admin", is_active=True)
        self._login("reader-ip-admin@example.test")
        token = self._csrf_from("/admin/members")
        response = self.client.post(
            "/admin/reader-access-ban",
            data={
                "csrf_token": token,
                "actor_type": "ip",
                "client_ip": "203.0.113.9",
                "action": "ban",
            },
        )
        self.assertEqual(response.status_code, 302)

        denied = guest_client.get("/reader", environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(denied.status_code, 403)
        admin_page = self.client.get("/admin")
        self.assertEqual(admin_page.status_code, 200)

        unauth = guest_client.get("/admin/reader-access?actor=ip:203.0.113.9")
        self.assertIn(unauth.status_code, {302, 401, 403})

    def test_monitoring_exemption_excludes_from_reader_audit(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"guest": {"search": True, "library": True, "ai": False}}},
        )
        app_module.set_setting(
            "monitoring_exemptions",
            {"user_agents": ["MarxUptimeBot"], "ips": ["198.51.100.7"]},
        )
        try:
            client = app_module.app.test_client()
            # 普通访客：阅读器审计应记录。
            client.get(
                "/viewer?file=test.pdf&page=1",
                headers={"User-Agent": "Mozilla/5.0 (real reader)"},
                environ_base={"REMOTE_ADDR": "203.0.113.50"},
            )
            # 监控程序(按 UA 豁免)：不应记录。
            client.get(
                "/viewer?file=test.pdf&page=1",
                headers={"User-Agent": "MarxUptimeBot/1.0 health-check"},
                environ_base={"REMOTE_ADDR": "203.0.113.51"},
            )
            # 监控程序(按 IP 豁免)：不应记录。
            client.get(
                "/viewer?file=test.pdf&page=1",
                headers={"User-Agent": "curl/8.0"},
                environ_base={"REMOTE_ADDR": "198.51.100.7"},
            )
            with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
                total = conn.execute("SELECT COUNT(*) FROM reader_access_events").fetchone()[0]
                bot_rows = conn.execute(
                    "SELECT COUNT(*) FROM reader_access_events WHERE user_agent LIKE 'MarxUptimeBot%'"
                ).fetchone()[0]
                exempt_ip_rows = conn.execute(
                    "SELECT COUNT(*) FROM reader_access_events WHERE client_ip = ?",
                    ("198.51.100.7",),
                ).fetchone()[0]
            self.assertEqual(bot_rows, 0, "按 UA 豁免的监控请求不应写入阅读器审计")
            self.assertEqual(exempt_ip_rows, 0, "按 IP 豁免的监控请求不应写入阅读器审计")
            self.assertGreaterEqual(total, 1, "普通访客应被阅读器审计记录")
        finally:
            app_module.delete_setting("monitoring_exemptions")

    def test_monitoring_exemption_by_account_identity(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"registered": {"search": True, "library": True, "ai": True}}},
        )
        monitor = self._create_active_member("monitor-bot@example.test")
        app_module.set_setting(
            "monitoring_exemptions",
            {"emails": ["monitor-bot@example.test"]},
        )
        try:
            self._force_login_user_id(int(monitor["id"]))
            self.client.get(
                "/viewer?file=test.pdf&page=1",
                headers={"User-Agent": "Mozilla/5.0 (headless monitor)"},
                environ_base={"REMOTE_ADDR": "203.0.113.60"},
            )
            with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
                monitor_rows = conn.execute(
                    "SELECT COUNT(*) FROM reader_access_events WHERE user_id = ?",
                    (int(monitor["id"]),),
                ).fetchone()[0]
            self.assertEqual(monitor_rows, 0, "豁免账号(登录态)不应写入阅读器审计")
        finally:
            app_module.delete_setting("monitoring_exemptions")

    def test_known_ai_bot_blocked_on_reader_and_not_audited(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"guest": {"search": True, "library": True, "ai": False}}},
        )
        client = app_module.app.test_client()
        # GPTBot 命中阅读端点：直接 403，且不写入审计。
        blocked = client.get(
            "/viewer?file=test.pdf&page=1",
            headers={"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.4; +https://openai.com/gptbot)"},
            environ_base={"REMOTE_ADDR": "203.0.113.70"},
        )
        self.assertEqual(blocked.status_code, 403)
        # 普通访客同一请求：不被 403(404 因测试文件不存在，但通过了机器人闸)。
        human = client.get(
            "/viewer?file=test.pdf&page=1",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"},
            environ_base={"REMOTE_ADDR": "203.0.113.71"},
        )
        self.assertNotEqual(human.status_code, 403)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            bot_rows = conn.execute(
                "SELECT COUNT(*) FROM reader_access_events WHERE user_agent LIKE '%GPTBot%'"
            ).fetchone()[0]
        self.assertEqual(bot_rows, 0, "被封禁的爬虫不应进入阅读器审计/异常名单")
        # robots.txt 显式列出 AI 爬虫禁令。
        robots = client.get("/robots.txt").get_data(as_text=True)
        self.assertIn("User-agent: GPTBot", robots)

    def test_generic_automation_and_empty_ua_blocked_on_reader(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"guest": {"search": True, "library": True, "ai": False}}},
        )
        client = app_module.app.test_client()
        for ua in ("curl/8.18.0", "python-requests/2.31.0", "Go-http-client/2.0", "okhttp/4.12.0", ""):
            resp = client.get(
                "/viewer?file=test.pdf&page=1",
                headers={"User-Agent": ua},
                environ_base={"REMOTE_ADDR": "203.0.113.80"},
            )
            self.assertEqual(resp.status_code, 403, msg=f"脚本/空 UA 应被拦: {ua!r}")
        # 真人浏览器与 Playwright 监控(HeadlessChrome)不受影响。
        for ua in (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/124.0.0.0 Safari/537.36",
        ):
            resp = client.get(
                "/viewer?file=test.pdf&page=1",
                headers={"User-Agent": ua},
                environ_base={"REMOTE_ADDR": "203.0.113.81"},
            )
            self.assertNotEqual(resp.status_code, 403, msg=f"不应误伤: {ua!r}")

    def test_real_client_ip_taken_from_rightmost_forwarded_for(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"guest": {"search": True, "library": True, "ai": False}}},
        )
        client = app_module.app.test_client()
        client.get(
            "/viewer?file=test.pdf&page=1",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36",
                # 左侧 10.0.0.9 是客户端可伪造的；最右 203.0.113.200 由反代写入、为真实 IP。
                "X-Forwarded-For": "10.0.0.9, 203.0.113.200",
            },
        )
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            row = conn.execute(
                "SELECT client_ip FROM reader_access_events ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            row[0], "203.0.113.200",
            "真实 IP 应取 X-Forwarded-For 最右项，不能是伪造的左侧或塌缩的 127.0.0.1",
        )

    def test_reader_view_ip_rate_limit_and_exemptions(self) -> None:
        app_module.set_setting(
            "access_policy",
            {"audience": {"guest": {"search": True, "library": True, "ai": False}}},
        )
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        os.environ["READER_VIEW_IP_RATE"] = "3,60"
        try:
            app_module._rate_buckets.clear()
            client = app_module.app.test_client()
            codes = []
            for _ in range(5):
                r = client.get(
                    "/viewer?file=test.pdf&page=1&mode=reader",
                    headers={"User-Agent": ua, "X-Forwarded-For": "203.0.113.150"},
                )
                codes.append(r.status_code)
            self.assertNotIn(429, codes[:3], msg=f"前3次不应限速: {codes}")
            self.assertEqual(codes[-1], 429, msg=f"超阈值应 429: {codes}")
            # 不同真实 IP 不受牵连。
            other = client.get(
                "/viewer?file=test.pdf&page=1&mode=reader",
                headers={"User-Agent": ua, "X-Forwarded-For": "203.0.113.151"},
            )
            self.assertNotEqual(other.status_code, 429)
            # 已豁免监控:同一超限 IP 也不被 429。
            app_module.set_setting("monitoring_exemptions", {"user_agents": ["MazhuMon"]})
            try:
                mon = client.get(
                    "/viewer?file=test.pdf&page=1&mode=reader",
                    headers={"User-Agent": "MazhuMon/1.0", "X-Forwarded-For": "203.0.113.150"},
                )
                self.assertNotEqual(mon.status_code, 429, "监控应豁免限速")
            finally:
                app_module.delete_setting("monitoring_exemptions")
        finally:
            os.environ.pop("READER_VIEW_IP_RATE", None)
            app_module._rate_buckets.clear()

    def test_auto_ban_egregious_public_ip_only(self) -> None:
        day = app_module.china_day_text()
        # 用真实可路由(is_global)的公网 IP；RFC5737 文档段(198.51.100/203.0.113)会被 is_public 正确排除。
        scraper_ip = "8.8.8.8"
        member_ip = "9.9.9.9"
        monitor_ip = "1.1.1.1"

        def _seed(actor_key, actor_type, client_ip, user_id, count=92):
            with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
                conn.executemany(
                    "INSERT INTO reader_access_events(day, actor_key, actor_type, session_key, "
                    "user_id, email, client_ip, user_agent, endpoint, method, path, reader_mode, "
                    "source_file, page, is_rate_limited, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (day, actor_key, actor_type, "sess", user_id, "", client_ip,
                         "Mozilla/5.0 Chrome", "pdf_viewer", "GET", "/viewer", "",
                         "f.pdf", i, 0, f"{day}T08:00:{i % 60:02d}+00:00")
                        for i in range(count)
                    ],
                )
                conn.commit()

        # 公网扒站 IP / 登录会员 actor / 内网 IP / 监控豁免 IP，各自都越过异常阈值(同分钟≥90)。
        _seed(f"ip:{scraper_ip}", "ip", scraper_ip, None)
        _seed("user:9001", "user", member_ip, 9001)
        _seed("ip:127.0.0.1", "ip", "127.0.0.1", None)
        _seed(f"ip:{monitor_ip}", "ip", monitor_ip, None)
        app_module.set_setting("monitoring_exemptions", {"ips": [monitor_ip]})
        os.environ["READER_AUTO_BAN_DAILY_MIN"] = "50"
        os.environ["READER_AUTO_BAN_MINUTE_MIN"] = "10"
        try:
            app_module._last_reader_auto_ban[0] = 0.0
            app_module._auto_ban_egregious_scrapers_if_due()
            banned_ips = set(app_module._reader_ip_bans(app_module._reader_bans()).keys())
            self.assertIn(scraper_ip, banned_ips, "公网高频扒站 IP 应被自动封")
            self.assertNotIn(member_ip, banned_ips, "登录会员(user: actor)绝不应被自动封")
            self.assertNotIn("127.0.0.1", banned_ips, "内网/回环 IP 不应被自动封")
            self.assertNotIn(monitor_ip, banned_ips, "监控豁免 IP 不应被自动封")
        finally:
            os.environ.pop("READER_AUTO_BAN_DAILY_MIN", None)
            os.environ.pop("READER_AUTO_BAN_MINUTE_MIN", None)
            app_module.delete_setting("monitoring_exemptions")
            app_module.delete_setting("reader_bans")

    def test_ai_daily_token_limit_zero_blocks_before_model_call(self) -> None:
        app_module.upsert_plan(
            code="token-zero",
            name="Token Zero",
            price_cents=0,
            currency="CNY",
            interval_months=1,
            description="test",
            daily_ai_token_limit=0,
            is_active=True,
            sort_order=99,
        )
        user = create_user(
            email="token-zero@example.test",
            display_name="Token Zero",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        create_manual_subscription(user_email=user["email"], plan_code="token-zero", note="test")
        app_module.set_setting(
            "access_policy",
            {"plans": {"token-zero": {"ai": True, "viewer": True, "library": True}}},
        )
        original_config = app_module.AI_CONFIG
        app_module.AI_CONFIG = replace(original_config, enabled=True, api_key="test-key", problems=())
        try:
            self._login("token-zero@example.test")
            token = self._csrf_from("/")
            response = self.client.post(
                "/api/ai/search-chat",
                json={"question": "test", "messages": []},
                headers={"X-CSRF-Token": token},
            )
        finally:
            app_module.AI_CONFIG = original_config
        self.assertEqual(response.status_code, 429)
        payload = response.get_json()
        self.assertEqual(payload["daily_limit"], 0)
        self.assertEqual(payload["used_tokens"], 0)

    def test_home_feedback_requires_login_and_admin_can_reply(self) -> None:
        guest_token = self._csrf_from("/")
        guest_response = self.client.post(
            "/api/feedback/messages",
            json={"body": "访客留言"},
            headers={"X-CSRF-Token": guest_token},
        )
        self.assertEqual(guest_response.status_code, 401)

        user = create_user(
            email="feedback-user@example.test",
            display_name="Feedback User",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        self._login("feedback-user@example.test")
        sent: list[dict] = []
        original_send = app_module._send_account_email
        app_module._send_account_email = lambda to_email, subject, body: sent.append(
            {"to": to_email, "subject": subject, "body": body}
        )
        try:
            token = self._csrf_from("/")
            response = self.client.post(
                "/api/feedback/messages",
                json={"body": "希望增加一个留言测试。"},
                headers={"X-CSRF-Token": token},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(sent[0]["to"], app_module.FEEDBACK_ADMIN_EMAIL)
            self.assertIn("希望增加一个留言测试", sent[0]["body"])
            thread_id = int(payload["thread"]["id"])

            admin = create_user(
                email="feedback-admin@example.test",
                display_name="Feedback Admin",
                password_hash=generate_password_hash("correct horse battery staple"),
                email_verified_at="2026-01-01T00:00:00+00:00",
            )
            app_module.update_user_account(int(admin["id"]), role="admin", is_active=True)
            with self.client.session_transaction() as sess:
                sess["user_id"] = int(admin["id"])

            content_page = self.client.get("/admin/content")
            self.assertEqual(content_page.status_code, 200)
            content_html = content_page.get_data(as_text=True)
            self.assertIn("用户留言回复", content_html)
            self.assertIn("希望增加一个留言测试", content_html)

            admin_token = self._csrf_from("/admin/content")
            reply = self.client.post(
                f"/admin/feedback/{thread_id}/reply",
                data={"csrf_token": admin_token, "body": "已经收到，我们会评估。"},
                follow_redirects=False,
            )
            self.assertEqual(reply.status_code, 302)
            self.assertEqual(sent[-1]["to"], "feedback-user@example.test")
            self.assertIn("已经收到，我们会评估", sent[-1]["body"])

            with self.client.session_transaction() as sess:
                sess["user_id"] = int(user["id"])
            index = self.client.get("/")
            self.assertEqual(index.status_code, 200)
            index_html = index.get_data(as_text=True)
            self.assertIn("管理员有新的回复", index_html)
            thread_response = self.client.get("/api/feedback/thread")
            self.assertEqual(thread_response.status_code, 200)
            thread_payload = thread_response.get_json()
            self.assertTrue(
                any(
                    message["author_role"] == "admin" and "已经收到，我们会评估" in message["body"]
                    for message in thread_payload["thread"]["messages"]
                )
            )
        finally:
            app_module._send_account_email = original_send

    def test_admin_console_shows_journal_controls_and_permission_key(self) -> None:
        admin = create_user(
            email="journal-admin@example.test",
            display_name="Journal Admin",
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        app_module.update_user_account(int(admin["id"]), role="admin", is_active=True)
        self._login("journal-admin@example.test")

        journal = self.client.get("/admin/journal")
        self.assertEqual(journal.status_code, 200)
        journal_html = journal.get_data(as_text=True)
        self.assertIn("期刊订阅与邮件提醒", journal_html)
        self.assertIn("发送设置", journal_html)
        self.assertIn("抓取时间范围", journal_html)
        self.assertIn("运行与测试", journal_html)
        self.assertIn("补齐默认来源参数", journal_html)
        self.assertIn("待审核文章", journal_html)
        self.assertIn("订阅概览", journal_html)
        self.assertNotIn("本地设备授权", journal_html)

        members = self.client.get("/admin/members")
        self.assertEqual(members.status_code, 200)
        self.assertIn("期刊提醒", members.get_data(as_text=True))
        self.assertIn("每日 AI token", members.get_data(as_text=True))

        overview = self.client.get("/admin")
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.get_data(as_text=True)
        self.assertIn("运营仪表盘", overview_html)
        self.assertIn("AI token", overview_html)


if __name__ == "__main__":
    unittest.main()
