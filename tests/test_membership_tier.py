from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import warnings
from datetime import timedelta


warnings.filterwarnings("ignore", category=ResourceWarning)
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-membership-tier-")
atexit.register(lambda: shutil.rmtree(_TMP_APPDATA, ignore_errors=True))
os.environ["APPDATA"] = _TMP_APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["ZPAY_PID"] = "test-pid"
os.environ["ZPAY_KEY"] = "test-secret"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
from membership import (  # noqa: E402
    create_manual_subscription,
    create_user,
    get_membership_snapshot,
    list_users,
    upsert_plan,
    utc_now,
)
from werkzeug.security import generate_password_hash  # noqa: E402


class MembershipTierTests(unittest.TestCase):
    """会员身份按「当前有效订阅中档次最高者」判定（升级即时生效、永不降级）。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 默认套餐只种了月度(1)与年度(12)；季度(3)由后台维护，这里补齐以覆盖三档。
        upsert_plan(code="monthly", name="月度会员", price_cents=900, interval_months=1, sort_order=10)
        upsert_plan(code="quarterly", name="季度会员", price_cents=2400, interval_months=3, sort_order=15)
        upsert_plan(code="yearly", name="年度会员", price_cents=8800, interval_months=12, sort_order=20)

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("DELETE FROM subscriptions")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM users")
            conn.commit()

    def _new_user(self, email: str) -> dict:
        return create_user(
            email=email,
            display_name=email.split("@", 1)[0],
            password_hash=generate_password_hash("x"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )

    def _insert_sub(
        self, user_id: int, plan_code: str, *, starts_days: int, expires_days: int, created_iso: str, status: str = "active"
    ) -> None:
        # 直接写订阅行以构造任意时间窗（含已过期），绕过 mark_order_paid 的叠加逻辑。
        now = utc_now()
        starts = (now + timedelta(days=starts_days)).isoformat(timespec="seconds")
        expires = (now + timedelta(days=expires_days)).isoformat(timespec="seconds")
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO subscriptions(user_id, plan_code, status, source, starts_at, expires_at, notes, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (int(user_id), plan_code, status, "test", starts, expires, "", created_iso, created_iso),
            )
            conn.commit()

    def test_month_then_quarter_upgrades_identity(self) -> None:
        u = self._new_user("m2q@example.test")
        create_manual_subscription(user_email=u["email"], plan_code="monthly", note="t")
        create_manual_subscription(user_email=u["email"], plan_code="quarterly", note="t")
        snap = get_membership_snapshot(int(u["id"]))
        self.assertTrue(snap.is_active_member)
        self.assertEqual(snap.plan_code, "quarterly")
        self.assertGreaterEqual(snap.days_remaining, 110)  # 30 + 90 叠加

    def test_quarter_then_month_not_downgraded(self) -> None:
        # 回归核心：高档会员叠买低档套餐，身份不得降级。
        u = self._new_user("q2m@example.test")
        create_manual_subscription(user_email=u["email"], plan_code="quarterly", note="t")
        create_manual_subscription(user_email=u["email"], plan_code="monthly", note="t")
        snap = get_membership_snapshot(int(u["id"]))
        self.assertTrue(snap.is_active_member)
        self.assertEqual(snap.plan_code, "quarterly")
        self.assertGreaterEqual(snap.days_remaining, 110)
        # 入口权益分档也应保持季度（token / 研究配额由此 bucket 决定）。
        with app_module.app.app_context():
            self.assertEqual(app_module._research_quota_bucket_for_user(dict(u)), "quarterly")

    def test_same_tier_renew_extends(self) -> None:
        u = self._new_user("renew@example.test")
        create_manual_subscription(user_email=u["email"], plan_code="monthly", note="t")
        create_manual_subscription(user_email=u["email"], plan_code="monthly", note="t")
        snap = get_membership_snapshot(int(u["id"]))
        self.assertTrue(snap.is_active_member)
        self.assertEqual(snap.plan_code, "monthly")
        self.assertGreaterEqual(snap.days_remaining, 55)  # ~60

    def test_yearly_dominates_when_stacked(self) -> None:
        u = self._new_user("y@example.test")
        create_manual_subscription(user_email=u["email"], plan_code="monthly", note="t")
        create_manual_subscription(user_email=u["email"], plan_code="yearly", note="t")
        snap = get_membership_snapshot(int(u["id"]))
        self.assertEqual(snap.plan_code, "yearly")

    def test_expired_high_tier_ignored(self) -> None:
        # 已过期的年度（高档但失效）+ 当前有效的月度 → 身份取有效集中唯一的月度。
        u = self._new_user("exp@example.test")
        self._insert_sub(int(u["id"]), "yearly", starts_days=-400, expires_days=-10, created_iso="2025-01-01T00:00:00+00:00")
        self._insert_sub(int(u["id"]), "monthly", starts_days=0, expires_days=30, created_iso="2026-06-01T00:00:00+00:00")
        snap = get_membership_snapshot(int(u["id"]))
        self.assertTrue(snap.is_active_member)
        self.assertEqual(snap.plan_code, "monthly")

    def test_all_expired_not_active(self) -> None:
        u = self._new_user("allexp@example.test")
        self._insert_sub(int(u["id"]), "quarterly", starts_days=-200, expires_days=-5, created_iso="2026-01-01T00:00:00+00:00")
        snap = get_membership_snapshot(int(u["id"]))
        self.assertFalse(snap.is_active_member)
        self.assertEqual(snap.plan_code, "quarterly")  # 无有效订阅时回退到最近一条用于展示

    def test_admin_list_reflects_highest_tier(self) -> None:
        # 后台用户列表也须按最高档展示（不被叠加的低档订阅拉低）。
        u = self._new_user("adminlist@example.test")
        create_manual_subscription(user_email=u["email"], plan_code="quarterly", note="t")
        create_manual_subscription(user_email=u["email"], plan_code="monthly", note="t")
        row = next(x for x in list_users(search_text="adminlist@example.test") if int(x["id"]) == int(u["id"]))
        self.assertEqual(row["membership_status"], "active")
        self.assertEqual(row["membership_plan_code"], "quarterly")
        self.assertEqual(row["membership_plan_name"], "季度会员")

    def test_payment_appends_member_export(self) -> None:
        # 灾备：每笔会员开通即时写入 append-only 导出账本，含完整账号信息（含 password_hash 以便迁移）。
        from membership import MEMBER_EXPORT_FILE

        u = self._new_user("export@example.test")
        create_manual_subscription(user_email=u["email"], plan_code="monthly", note="t")
        self.assertTrue(MEMBER_EXPORT_FILE.exists())
        lines = [ln for ln in MEMBER_EXPORT_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
        rec = None
        for ln in reversed(lines):
            data = json.loads(ln)
            if data.get("user", {}).get("email") == "export@example.test":
                rec = data
                break
        self.assertIsNotNone(rec, "导出账本未捕获该会员")
        self.assertEqual(rec["event"], "membership_paid")
        self.assertEqual(rec["subscription"]["plan_code"], "monthly")
        self.assertIn("password_hash", rec["user"])

    def test_no_subscription_free(self) -> None:
        u = self._new_user("free@example.test")
        snap = get_membership_snapshot(int(u["id"]))
        self.assertFalse(snap.is_active_member)
        self.assertEqual(snap.status, "free")
        self.assertEqual(snap.plan_code, "")


if __name__ == "__main__":
    unittest.main()
