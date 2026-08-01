from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
import unittest
import warnings


warnings.filterwarnings("ignore", category=ResourceWarning)
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-search-bulkcov-")
atexit.register(lambda: shutil.rmtree(_TMP_APPDATA, ignore_errors=True))
os.environ["APPDATA"] = _TMP_APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["ZPAY_PID"] = "test-pid"
os.environ["ZPAY_KEY"] = "test-secret"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
from membership import (  # noqa: E402
    bulk_grant_coverage,
    bulk_grant_membership,
    create_manual_subscription,
    create_user,
    list_active_plans,
    list_users,
)
from werkzeug.security import generate_password_hash  # noqa: E402


class BulkGrantCoverageTests(unittest.TestCase):
    """「批量续期覆盖情况」：已获 / 未获批量赠送的会员必须能区分，且能只对未获者补发。"""

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM subscriptions")
            conn.execute("DELETE FROM payment_events")
            conn.execute("DELETE FROM users")
            conn.commit()
        self.plan_code = list_active_plans()[0]["code"]

    def _new_member(self, email: str) -> dict:
        user = create_user(
            email=email,
            display_name=email.split("@", 1)[0],
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )
        create_manual_subscription(user_email=email, plan_code=self.plan_code)
        return user

    def test_coverage_distinguishes_granted_and_ungranted(self) -> None:
        self._new_member("old1@example.com")
        self._new_member("old2@example.com")

        # 第一轮批量续期：覆盖当时的全部有效会员。
        result = bulk_grant_membership(scope="active_members", plan_code="__keep__", extra_days=60)
        self.assertEqual(result["granted"], 2)

        # 批量续期之后才订阅的新会员。
        self._new_member("newbie@example.com")

        coverage = bulk_grant_coverage()
        self.assertEqual(coverage["active_total"], 3)
        self.assertEqual(coverage["granted_total"], 2)
        self.assertEqual([m["email"] for m in coverage["ungranted"]], ["newbie@example.com"])
        self.assertEqual(len(coverage["batches"]), 1)
        self.assertEqual(coverage["batches"][0]["granted"], 2)

        # 用户列表能按人看到「最近一次批量续期时间」。
        by_email = {u["email"]: u for u in list_users()}
        self.assertTrue(by_email["old1@example.com"]["last_bulk_grant_at"])
        self.assertIsNone(by_email["newbie@example.com"]["last_bulk_grant_at"])

    def test_ungranted_scope_only_targets_members_without_bulk_grant(self) -> None:
        self._new_member("old@example.com")
        bulk_grant_membership(scope="active_members", plan_code="__keep__", extra_days=60)
        self._new_member("newbie@example.com")

        # 「仅未获批量续期的有效会员」补发：只应命中新会员。
        result = bulk_grant_membership(
            scope="active_members_ungranted", plan_code="__keep__", extra_days=60
        )
        self.assertEqual(result["granted"], 1)

        coverage = bulk_grant_coverage()
        self.assertEqual(coverage["ungranted"], [])
        self.assertEqual(coverage["granted_total"], 2)
        # 再跑一次同范围：所有人都已覆盖，不应再有目标。
        result = bulk_grant_membership(
            scope="active_members_ungranted", plan_code="__keep__", extra_days=60
        )
        self.assertEqual(result["granted"], 0)


if __name__ == "__main__":
    unittest.main()
