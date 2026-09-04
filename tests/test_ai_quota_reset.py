from __future__ import annotations

import os
import re
import sqlite3
import unittest
import warnings
from datetime import datetime, timedelta, timezone
from unittest import mock


warnings.filterwarnings("ignore", category=ResourceWarning)
from _test_env import APPDATA as _TMP_APPDATA  # noqa: E402
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
from admin_store import delete_setting, get_setting  # noqa: E402
from membership import (  # noqa: E402
    china_day_text,
    create_manual_subscription,
    create_user,
    record_ai_usage,
)
from werkzeug.security import generate_password_hash  # noqa: E402


class AiTokenQuotaResetTests(unittest.TestCase):
    """后台「重置本周 AI 额度」：按范围把已用 token 归零，且只动计数、不动审计与额度配置。"""

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("DELETE FROM ai_usage")
            conn.execute("DELETE FROM subscriptions")
            conn.execute("DELETE FROM users")
            conn.commit()
        delete_setting(app_module.AI_TOKEN_QUOTA_RESETS_SETTING_KEY)
        delete_setting(app_module.RESEARCH_QUOTA_RESETS_SETTING_KEY)
        self.client = app_module.app.test_client()

    # ---- fixtures --------------------------------------------------------
    def _new_user(self, email: str) -> dict:
        return create_user(
            email=email,
            display_name=email.split("@", 1)[0],
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )

    def _new_member(self, email: str, plan_code: str = "support_basic") -> dict:
        user = self._new_user(email)
        create_manual_subscription(user_email=email, plan_code=plan_code, note="test")
        return user

    def _login_admin(self, email: str = "boss@test.io") -> dict:
        user = self._new_user(email)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (int(user["id"]),))
            conn.commit()
        with self.client.session_transaction() as sess:
            sess["user_id"] = int(user["id"])
            # 配置了发信邮箱时后台会启用二次验证；预置已验证时间戳，避免被 /admin/2fa 拦截。
            sess["admin_2fa_verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return user

    def _burn(self, user: dict, tokens: int, *, feature: str = "chat") -> None:
        """记一笔「本周较早时候」的 AI 用量：day 落在本周，created_at 回拨 1 小时。

        回拨是必要的——重置标记与 created_at 都只精确到秒，同一秒写入的记录仍会被计入
        （判据是 created_at >= 标记），测试里若不回拨就会偶发地「重置后仍有用量」。
        """
        record_ai_usage(
            user_id=int(user["id"]),
            day=china_day_text(),
            feature=feature,
            provider="deepseek",
            model="deepseek-v4-pro",
            total_tokens=tokens,
        )
        earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute(
                "UPDATE ai_usage SET created_at=? WHERE user_id=? AND created_at > ?",
                (earlier, int(user["id"]), earlier),
            )
            conn.commit()

    def _quota(self, user: dict) -> dict:
        with app_module.app.test_request_context("/"):
            app_module.g.current_user = user
            return app_module._ai_token_quota_payload(user)

    def _csrf_token(self) -> str:
        html = self.client.get("/admin/members").get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        self.assertIsNotNone(match)
        return match.group(1)

    def _reset(self, *, scope: str, emails: str = "") -> str:
        resp = self.client.post(
            "/admin/reset-ai-token-quota",
            data={"scope": scope, "emails": emails, "csrf_token": self._csrf_token()},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    # ---- 范围 -------------------------------------------------------------
    def test_reset_specific_user_only_zeroes_that_user(self) -> None:
        target = self._new_user("target@test.io")
        bystander = self._new_user("bystander@test.io")
        self._burn(target, 12000)
        self._burn(bystander, 9000)
        self.assertEqual(self._quota(target)["used"], 12000)

        self._login_admin()
        self._reset(scope="user", emails="target@test.io")

        after = self._quota(target)
        self.assertEqual(after["used"], 0)
        self.assertEqual(after["remaining"], after["limit"])
        self.assertFalse(after["exhausted"])
        self.assertEqual(self._quota(bystander)["used"], 9000)

    def test_reset_specific_user_accepts_multiple_emails(self) -> None:
        first = self._new_user("a@test.io")
        second = self._new_user("b@test.io")
        third = self._new_user("c@test.io")
        for user in (first, second, third):
            self._burn(user, 5000)

        self._login_admin()
        self._reset(scope="user", emails="a@test.io, b@test.io\nnobody@test.io")

        self.assertEqual(self._quota(first)["used"], 0)
        self.assertEqual(self._quota(second)["used"], 0)
        self.assertEqual(self._quota(third)["used"], 5000)
        # 查无此人的邮箱只跳过并回报，不影响其余目标。
        markers = get_setting(app_module.AI_TOKEN_QUOTA_RESETS_SETTING_KEY, {})
        self.assertEqual(sorted((markers.get("users") or {}).keys()), ["a@test.io", "b@test.io"])

    def test_reset_members_scope_covers_members_but_not_plain_registered(self) -> None:
        member = self._new_member("member@test.io")
        plain = self._new_user("plain@test.io")
        self._burn(member, 30000)
        self._burn(plain, 6000)

        self._login_admin()
        self._reset(scope="members")

        self.assertEqual(self._quota(member)["used"], 0)
        self.assertEqual(self._quota(plain)["used"], 6000)
        # 「全部会员」＝月/季/年三档一起标记，新升级到其它档次的会员也照样归零。
        tiers = (get_setting(app_module.AI_TOKEN_QUOTA_RESETS_SETTING_KEY, {}).get("tiers") or {})
        self.assertEqual(sorted(tiers.keys()), ["monthly", "quarterly", "yearly"])

    def test_registered_all_scope_covers_members_and_non_members(self) -> None:
        member = self._new_member("member4@test.io")
        plain = self._new_user("plain4@test.io")
        self._burn(member, 30000)
        self._burn(plain, 6000)

        self._login_admin()
        self._reset(scope="registered_all")

        self.assertEqual(self._quota(member)["used"], 0)
        self.assertEqual(self._quota(plain)["used"], 0)
        # 注册账号的四个档位都打标记，访客池不动。
        markers = get_setting(app_module.AI_TOKEN_QUOTA_RESETS_SETTING_KEY, {})
        self.assertEqual(
            sorted((markers.get("tiers") or {}).keys()),
            ["monthly", "quarterly", "registered", "yearly"],
        )
        self.assertFalse(markers.get("all"))

    def test_reset_registered_scope_leaves_members_alone(self) -> None:
        member = self._new_member("member2@test.io")
        plain = self._new_user("plain2@test.io")
        self._burn(member, 30000)
        self._burn(plain, 6000)

        self._login_admin()
        self._reset(scope="registered")

        self.assertEqual(self._quota(plain)["used"], 0)
        self.assertEqual(self._quota(member)["used"], 30000)

    def test_reset_all_scope_covers_everyone(self) -> None:
        member = self._new_member("member3@test.io")
        plain = self._new_user("plain3@test.io")
        self._burn(member, 30000)
        self._burn(plain, 6000)

        self._login_admin()
        self._reset(scope="all")

        self.assertEqual(self._quota(member)["used"], 0)
        self.assertEqual(self._quota(plain)["used"], 0)

    # ---- 闸门与副作用 -----------------------------------------------------
    def test_reset_reopens_the_quota_gate(self) -> None:
        """真正的验收点：额度用尽被拦的用户，重置后能立刻继续用 AI。"""
        user = self._new_user("burned@test.io")
        weekly_limit = int(self._quota(user)["limit"])
        self.assertGreater(weekly_limit, 0)
        self._burn(user, weekly_limit + 1000)
        self.assertTrue(self._quota(user)["exhausted"])

        with app_module.app.test_request_context("/"):
            app_module.g.current_user = user
            with self.assertRaises(app_module._AIQuotaExceeded):
                app_module._require_ai_quota_or_raise("chat")

        self._login_admin()
        self._reset(scope="user", emails="burned@test.io")

        with app_module.app.test_request_context("/"):
            app_module.g.current_user = user
            quota = app_module._require_ai_quota_or_raise("chat")
        self.assertEqual(quota["weekly_used"], 0)
        self.assertFalse(quota["over_free_limit"])

    def test_reset_keeps_audit_rows_and_quota_settings(self) -> None:
        user = self._new_user("audited@test.io")
        self._burn(user, 8000)
        before_limits = app_module._ai_token_daily_settings()

        self._login_admin()
        self._reset(scope="all")

        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            rows, total = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0) FROM ai_usage"
            ).fetchone()
        self.assertEqual(int(rows), 1)
        self.assertEqual(int(total), 8000)
        self.assertEqual(app_module._ai_token_daily_settings(), before_limits)

    def test_reset_does_not_touch_research_review_count(self) -> None:
        """AI token 额度与研究级检索次数是两套计数，各有各的重置。"""
        user = self._new_user("researcher@test.io")
        self._burn(user, 4000, feature=app_module.RESEARCH_QUOTA_FEATURE)

        self._login_admin()
        self._reset(scope="all")

        with app_module.app.test_request_context("/"):
            app_module.g.current_user = user
            with mock.patch.object(app_module, "_research_count_limit_enabled", return_value=True):
                research = app_module._research_quota_payload(user)
        self.assertEqual(research["used"], 1)
        self.assertEqual(self._quota(user)["used"], 0)

    # ---- 入参与权限 -------------------------------------------------------
    def test_invalid_scope_and_unknown_email_change_nothing(self) -> None:
        user = self._new_user("safe@test.io")
        self._burn(user, 7000)
        self._login_admin()

        self._reset(scope="not-a-scope")
        self.assertEqual(self._quota(user)["used"], 7000)

        self._reset(scope="user", emails="ghost@test.io")
        self.assertEqual(self._quota(user)["used"], 7000)

        self._reset(scope="user", emails="   ")
        self.assertEqual(self._quota(user)["used"], 7000)
        self.assertEqual(get_setting(app_module.AI_TOKEN_QUOTA_RESETS_SETTING_KEY, {}), {})

    def test_reset_requires_admin(self) -> None:
        user = self._new_user("nosy@test.io")
        self._burn(user, 5000)
        with self.client.session_transaction() as sess:
            sess["user_id"] = int(user["id"])
        resp = self.client.post(
            "/admin/reset-ai-token-quota", data={"scope": "all"}, follow_redirects=False
        )
        self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(self._quota(user)["used"], 5000)

    def test_admin_members_page_exposes_the_reset_form(self) -> None:
        self._login_admin()
        html = self.client.get("/admin/members").get_data(as_text=True)
        self.assertIn("重置本周 AI 额度", html)
        self.assertIn('action="/admin/reset-ai-token-quota"', html)
        # 范围下拉必须覆盖：全部注册用户 / 全部会员 / 指定用户。
        for scope in ("all", "registered_all", "members", "registered", "user"):
            self.assertIn(f'value="{scope}"', html)
        self.assertIsNotNone(re.search(r'name="emails"', html))


if __name__ == "__main__":
    unittest.main()
