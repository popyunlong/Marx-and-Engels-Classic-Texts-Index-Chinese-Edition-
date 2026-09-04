from __future__ import annotations

import os
import re
import sqlite3
import unittest
import warnings
from datetime import datetime, timezone
from unittest import mock


warnings.filterwarnings("ignore", category=ResourceWarning)
from _test_env import APPDATA as _TMP_APPDATA  # noqa: E402
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
import broadcast_email  # noqa: E402
from admin_store import delete_setting  # noqa: E402
from journal_alerts import SMTPConfig  # noqa: E402
from membership import (  # noqa: E402
    create_manual_subscription,
    create_user,
)
from werkzeug.security import generate_password_hash  # noqa: E402


_ENABLED_SMTP = SMTPConfig(
    host="smtp.test",
    port=465,
    username="u",
    password="p",
    from_email="from@test",
    from_name="马著作检索",
    use_tls=True,
)


class BroadcastEmailTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with sqlite3.connect(app_module.JOURNAL_ALERTS_DB_PATH) as conn:
            conn.execute("DELETE FROM broadcast_deliveries")
            conn.execute("DELETE FROM broadcast_campaigns")
            conn.commit()
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("DELETE FROM subscriptions")
            conn.execute("DELETE FROM users")
            conn.commit()
        delete_setting("broadcast_draft")
        self.client = app_module.app.test_client()

    # ---- fixtures --------------------------------------------------------
    def _new_user(self, email: str, *, name: str = "") -> dict:
        return create_user(
            email=email,
            display_name=name or email.split("@", 1)[0],
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )

    def _new_member(self, email: str, plan_code: str = "support_basic", *, name: str = "") -> dict:
        user = self._new_user(email, name=name)
        create_manual_subscription(user_email=email, plan_code=plan_code, note="test")
        return user

    def _create_admin(self, email: str) -> dict:
        user = self._new_user(email, name="admin")
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (int(user["id"]),))
            conn.commit()
        return user

    def _login_admin(self, user_id: int) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = int(user_id)
            # 配置了发信邮箱时后台会启用二次验证；预置已验证时间戳，避免被 /admin/2fa 拦截。
            sess["admin_2fa_verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _csrf_from(self, path: str) -> str:
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', resp.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match.group(1)

    # ---- audiences -------------------------------------------------------
    def test_dormant_noip_scope_targets_only_users_without_ip(self) -> None:
        """「久未回访用户」受众＝register_ip/last_ip 都为空的启用账号（记 IP 上线前注册且
        至今无回访）；有任一 IP 记录即不在名单；须列入 BROADCAST_SCOPES 供表单校验放行。"""
        self._new_user("dormant@test.io")
        fresh = self._new_user("active@test.io")
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("UPDATE users SET last_ip='1.2.3.4' WHERE id=?", (int(fresh["id"]),))
            conn.commit()
        got = broadcast_email.resolve_recipients("dormant_noip")
        self.assertEqual([r["email"] for r in got], ["dormant@test.io"])
        self.assertIn("dormant_noip", broadcast_email.BROADCAST_SCOPES)

    # ---- rendering / personalization ------------------------------------
    def test_personalize_placeholders(self) -> None:
        recipient = {"email": "wang@x.com", "display_name": "王同志"}
        self.assertEqual(broadcast_email.personalize("你好 {name}（{email}）", recipient), "你好 王同志（wang@x.com）")
        # 无昵称时用邮箱 @ 前缀兜底。
        self.assertEqual(broadcast_email.personalize("{name}", {"email": "li@x.com"}), "li")

    def test_render_markdown_layout(self) -> None:
        html = broadcast_email.render_broadcast_html("## 标题\n\n正文 **重点**\n\n- 一\n- 二")
        self.assertIn("<h2", html)  # 标题被排版
        self.assertIn("<strong", html)  # 加粗被排版
        self.assertIn("<li", html)  # 列表被排版
        self.assertIn("style=", html)  # 邮件内联样式已注入

    def test_render_escapes_html(self) -> None:
        html = broadcast_email.render_broadcast_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_render_image_and_link(self) -> None:
        html = broadcast_email.render_broadcast_html(
            "看图：\n\n![检索演示](https://example.test/static/broadcast/demo.gif)\n\n"
            "去[马著作检索](https://example.test/)看看。"
        )
        self.assertIn('<img src="https://example.test/static/broadcast/demo.gif"', html)
        self.assertIn('alt="检索演示"', html)
        self.assertIn("max-width:100%", html)  # 邮件内联样式（客户端不读外链 CSS）
        self.assertIn('<a href="https://example.test/"', html)
        self.assertIn(">马著作检索</a>", html)

    def test_render_rejects_non_http_urls(self) -> None:
        html = broadcast_email.render_broadcast_html(
            "[点我](javascript:alert(1)) ![x](data:image/gif;base64,AAAA)"
        )
        self.assertNotIn("<a ", html)
        self.assertNotIn("<img ", html)

    # ---- audience resolution --------------------------------------------
    def test_resolve_scopes(self) -> None:
        self._new_user("plain@x.com")
        self._new_member("m1@x.com", "support_basic")
        self._new_member("plus1@x.com", "support_plus")

        registered = {r["email"] for r in broadcast_email.resolve_recipients("registered")}
        self.assertEqual(registered, {"plain@x.com", "m1@x.com", "plus1@x.com"})

        members = {r["email"] for r in broadcast_email.resolve_recipients("members")}
        self.assertEqual(members, {"m1@x.com", "plus1@x.com"})

        plus = {r["email"] for r in broadcast_email.resolve_recipients("plans", plan_codes=["support_plus"])}
        self.assertEqual(plus, {"plus1@x.com"})

        specific = {r["email"] for r in broadcast_email.resolve_recipients("specific", emails=["a@x.com", "a@x.com", "b@x.com"])}
        self.assertEqual(specific, {"a@x.com", "b@x.com"})  # 去重

        self.assertEqual(broadcast_email.resolve_recipients("plans", plan_codes=[]), [])

    # ---- worker: send + persistence -------------------------------------
    def test_send_campaign_records_deliveries(self) -> None:
        recipients = [
            {"email": "ok@x.com", "user_id": None, "display_name": "甲"},
            {"email": "boom@x.com", "user_id": None, "display_name": "乙"},
        ]
        cid = broadcast_email.create_campaign(
            subject="主题", body_md="正文 {name}", scope="specific", plan_codes=[],
            total_recipients=len(recipients), created_by="tester",
        )

        def _send(config, to_email, subject, text_body, html_body=""):
            if to_email == "boom@x.com":
                raise RuntimeError("mailbox full")

        with mock.patch.object(broadcast_email, "send_email", side_effect=_send) as sent:
            result = broadcast_email.send_campaign(
                cid, recipients, subject="主题", body_md="正文 {name}", smtp_config=_ENABLED_SMTP
            )
        self.assertEqual(sent.call_count, 2)
        self.assertEqual(result, {"sent": 1, "failed": 1})

        campaigns = broadcast_email.list_recent_campaigns()
        self.assertEqual(campaigns[0]["status"], "partial")
        self.assertEqual(campaigns[0]["sent"], 1)
        self.assertEqual(campaigns[0]["failed"], 1)
        failures = broadcast_email.list_campaign_failures(cid)
        self.assertEqual([f["email"] for f in failures], ["boom@x.com"])

    def test_send_campaign_personalizes_per_recipient(self) -> None:
        recipients = [
            {"email": "a@x.com", "display_name": "甲"},
            {"email": "b@x.com", "display_name": "乙"},
        ]
        cid = broadcast_email.create_campaign(
            subject="致 {name}", body_md="你好 {name}", scope="specific", plan_codes=[],
            total_recipients=2, created_by="t",
        )
        seen: list[tuple[str, str]] = []

        def _send(config, to_email, subject, text_body, html_body=""):
            seen.append((subject, text_body))

        with mock.patch.object(broadcast_email, "send_email", side_effect=_send):
            broadcast_email.send_campaign(cid, recipients, subject="致 {name}", body_md="你好 {name}", smtp_config=_ENABLED_SMTP)
        self.assertIn(("致 甲", "你好 甲"), seen)
        self.assertIn(("致 乙", "你好 乙"), seen)

    # ---- HTTP route ------------------------------------------------------
    def test_send_route_requires_confirm_for_all(self) -> None:
        admin = self._create_admin("admin@x.com")
        self._login_admin(int(admin["id"]))
        self._new_member("m@x.com")
        token = self._csrf_from("/admin/content")
        with mock.patch.object(app_module, "load_smtp_config", return_value=_ENABLED_SMTP):
            resp = self.client.post(
                "/admin/broadcast/send",
                data={"csrf_token": token, "action": "send", "subject": "S", "body": "B", "scope": "members"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 302)
        # 未勾选确认框：不建 campaign。
        self.assertEqual(broadcast_email.list_recent_campaigns(), [])

    def test_send_route_creates_and_dispatches(self) -> None:
        admin = self._create_admin("admin@x.com")
        self._login_admin(int(admin["id"]))
        self._new_member("m1@x.com", "support_basic")
        self._new_member("m2@x.com", "support_plus")
        token = self._csrf_from("/admin/content")

        # 让后台调度同步执行，便于确定性断言。
        def _run_sync(fn):
            fn()
            return True

        with mock.patch.object(app_module, "load_smtp_config", return_value=_ENABLED_SMTP), \
             mock.patch.object(app_module, "_start_broadcast_job_async", side_effect=_run_sync), \
             mock.patch.object(broadcast_email, "send_email") as sent:
            resp = self.client.post(
                "/admin/broadcast/send",
                data={
                    "csrf_token": token, "action": "send", "subject": "公告",
                    "body": "## 你好 {name}\n\n正文", "scope": "members", "confirm_all": "1",
                },
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sent.call_count, 2)
        campaigns = broadcast_email.list_recent_campaigns()
        self.assertEqual(len(campaigns), 1)
        self.assertEqual(campaigns[0]["scope"], "members")
        self.assertEqual(campaigns[0]["sent"], 2)
        self.assertEqual(campaigns[0]["status"], "done")

    def test_preview_route_returns_html_and_count(self) -> None:
        admin = self._create_admin("admin@x.com")
        self._login_admin(int(admin["id"]))
        self._new_member("m1@x.com", "support_basic")
        token = self._csrf_from("/admin/content")
        resp = self.client.post(
            "/admin/broadcast/preview",
            data={"csrf_token": token, "subject": "S", "body": "## 标题", "scope": "members"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("<h2", payload["html"])
        self.assertEqual(payload["recipient_count"], 1)

    # ---- draft -----------------------------------------------------------
    def test_draft_route_saves_and_prefills_form(self) -> None:
        admin = self._create_admin("admin@x.com")
        self._login_admin(int(admin["id"]))
        token = self._csrf_from("/admin/content")
        resp = self.client.post(
            "/admin/broadcast/draft",
            data={"csrf_token": token, "subject": "致全体用户", "body": "## 你好 {name}\n\n草稿正文"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        draft = app_module._broadcast_draft()
        self.assertEqual(draft["source"], "saved")
        self.assertEqual(draft["subject"], "致全体用户")
        # 重新打开后台：表单预填草稿。
        page = self.client.get("/admin/content").get_data(as_text=True)
        self.assertIn('value="致全体用户"', page)
        self.assertIn("草稿正文", page)

    def test_draft_falls_back_to_config_default(self) -> None:
        import pathlib

        default_md = pathlib.Path(_TMP_APPDATA) / "broadcast_draft.md"
        default_md.write_text("subject: 默认公告\n---\n## 默认正文\n", encoding="utf-8")
        with mock.patch.object(app_module, "BROADCAST_DRAFT_DEFAULT_PATH", default_md):
            draft = app_module._broadcast_draft()
        self.assertEqual(draft["source"], "config")
        self.assertEqual(draft["subject"], "默认公告")
        self.assertEqual(draft["body"], "## 默认正文")

    def test_test_send_does_not_require_confirm(self) -> None:
        admin = self._create_admin("admin@x.com")
        self._login_admin(int(admin["id"]))
        token = self._csrf_from("/admin/content")
        with mock.patch.object(app_module, "load_smtp_config", return_value=_ENABLED_SMTP), \
             mock.patch.object(app_module, "send_email") as sent:
            resp = self.client.post(
                "/admin/broadcast/send",
                data={
                    "csrf_token": token, "action": "test", "subject": "S",
                    "body": "B", "scope": "members", "test_email": "me@x.com",
                },
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 302)
        sent.assert_called_once()
        # 测试发送不建 campaign。
        self.assertEqual(broadcast_email.list_recent_campaigns(), [])


if __name__ == "__main__":
    unittest.main()
