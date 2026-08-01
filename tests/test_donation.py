from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
import unittest
import warnings


warnings.filterwarnings("ignore", category=ResourceWarning)
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-search-donation-")
atexit.register(lambda: shutil.rmtree(_TMP_APPDATA, ignore_errors=True))
os.environ["APPDATA"] = _TMP_APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["ZPAY_PID"] = "test-pid"
os.environ["ZPAY_KEY"] = "test-secret"
os.environ["TURNSTILE_ENABLED"] = "0"

import app as app_module  # noqa: E402
import membership as membership_module  # noqa: E402
from membership import (  # noqa: E402
    DONATION_MAX_CENTS,
    DONATION_MIN_CENTS,
    create_donation_order,
    create_user,
    get_membership_snapshot,
    get_order_by_no,
    get_plan,
    list_active_plans,
    list_plans,
    list_subscriptions_for_user,
    mark_order_paid,
)
from zpay import _amount_to_yuan  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


class DonationTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM subscriptions")
            conn.execute("DELETE FROM payment_events")
            conn.execute("DELETE FROM users")
            conn.commit()
        # setUp 清空了 users，进程级缓存里的占位账号 id 已失效，重置以免下次打赏引用到已删行。
        membership_module._donation_guest_id[0] = None
        self.client = app_module.app.test_client()

    def _new_user(self, email: str) -> dict:
        return create_user(
            email=email,
            display_name=email.split("@", 1)[0],
            password_hash=generate_password_hash("correct horse battery staple"),
            email_verified_at="2026-01-01T00:00:00+00:00",
        )

    def _login(self, user_id: int, csrf: str = "testcsrf") -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = int(user_id)
            sess["_csrf_token"] = csrf

    # ---- plan visibility -------------------------------------------------
    def test_donation_plan_hidden_but_resolvable(self) -> None:
        self.assertNotIn("donation", {p["code"] for p in list_active_plans()})
        self.assertNotIn("donation", {p["code"] for p in list_plans(include_inactive=True)})
        plan = get_plan("donation")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "donation")

    # ---- order creation + bounds ----------------------------------------
    def test_create_donation_order_and_bounds(self) -> None:
        user = self._new_user("d1@x.com")
        order = create_donation_order(user_id=int(user["id"]), amount_cents=1000)
        self.assertEqual(order["plan_code"], "donation")
        self.assertEqual(order["amount_cents"], 1000)
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["currency"], "CNY")
        with self.assertRaises(ValueError):
            create_donation_order(user_id=int(user["id"]), amount_cents=DONATION_MIN_CENTS - 1)
        with self.assertRaises(ValueError):
            create_donation_order(user_id=int(user["id"]), amount_cents=DONATION_MAX_CENTS + 1)

    # ---- mark paid does NOT grant membership ----------------------------
    def test_mark_donation_paid_grants_no_membership(self) -> None:
        user = self._new_user("d2@x.com")
        order = create_donation_order(user_id=int(user["id"]), amount_cents=2000)
        result = mark_order_paid(
            order_no=order["order_no"], provider="zpay", payment_reference="T1", source="zpay_notify"
        )
        self.assertIsNone(result["subscription"])
        self.assertEqual(result["order"]["status"], "paid")
        self.assertEqual(list_subscriptions_for_user(int(user["id"])), [])
        self.assertFalse(get_membership_snapshot(int(user["id"])).is_active_member)
        # 幂等：重复回调不报错、仍不开会员。
        again = mark_order_paid(order_no=order["order_no"], provider="zpay", source="zpay_notify")
        self.assertIsNone(again["subscription"])
        self.assertEqual(again["order"]["status"], "paid")

    # ---- HTTP: /donate ---------------------------------------------------
    def test_anonymous_visitor_can_donate(self) -> None:
        # 访客（未登录）也能打赏：挂到 system 占位账号，跳网关，且 order_no 记进会话。
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "anon"
        resp = self.client.post(
            "/donate", data={"csrf_token": "anon", "amount": "8"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 302)  # 跳网关，不再被登录闸拦
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            row = conn.execute(
                "SELECT o.plan_code, o.amount_cents, u.role FROM orders o JOIN users u ON u.id=o.user_id"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "donation")
        self.assertEqual(row[1], 800)
        self.assertEqual(row[2], "system")  # 挂在占位账号名下
        with self.client.session_transaction() as sess:
            self.assertEqual(len(sess.get("donation_order_nos") or []), 1)

    def test_guest_donor_excluded_from_counts(self) -> None:
        from membership import count_registered_users, get_or_create_donation_guest_id, list_users

        base = count_registered_users()
        self._new_user("real@x.com")
        gid = get_or_create_donation_guest_id()
        self.assertIsInstance(gid, int)
        # 占位账号不计入注册数、不出现在后台用户列表。
        self.assertEqual(count_registered_users(), base + 1)
        self.assertNotIn("donation-guest@system.local", {u["email"] for u in list_users()})

    def test_anonymous_donor_can_view_status_and_result(self) -> None:
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "anon"
        resp = self.client.post("/donate", data={"csrf_token": "anon", "amount": "15"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            order_no = conn.execute("SELECT order_no FROM orders").fetchone()[0]
        # 访客凭会话可轮询状态与查看结果页（无需登录）。
        status = self.client.get(f"/checkout/order/{order_no}/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.get_json()["paid"])
        result = self.client.get(f"/payments/result?order_no={order_no}")
        self.assertEqual(result.status_code, 200)
        self.assertIn("打赏", result.get_data(as_text=True))

    def test_other_session_cannot_view_anonymous_donation(self) -> None:
        # A 会话打赏，B 会话（无该 order_no）不能查看其收银状态。
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "anon"
        self.client.post("/donate", data={"csrf_token": "anon", "amount": "9"}, follow_redirects=False)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            order_no = conn.execute("SELECT order_no FROM orders").fetchone()[0]
        other = app_module.app.test_client()
        status = other.get(f"/checkout/order/{order_no}/status")
        self.assertEqual(status.status_code, 404)

    def test_donate_creates_order_and_redirects_to_gateway(self) -> None:
        user = self._new_user("d3@x.com")
        self._login(int(user["id"]))
        resp = self.client.post(
            "/donate", data={"csrf_token": "testcsrf", "amount": "12.50"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 302)  # 默认 redirect 模式 → 跳网关
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            row = conn.execute(
                "SELECT plan_code, amount_cents, status FROM orders WHERE user_id=?", (int(user["id"]),)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "donation")
        self.assertEqual(row[1], 1250)
        self.assertEqual(row[2], "pending")

    def test_donate_rejects_bad_amount(self) -> None:
        user = self._new_user("d4@x.com")
        self._login(int(user["id"]))
        for bad in ("0", "-5", "abc", "99999"):
            resp = self.client.post(
                "/donate", data={"csrf_token": "testcsrf", "amount": bad}, follow_redirects=False
            )
            self.assertEqual(resp.status_code, 302)
        with sqlite3.connect(app_module.MEMBERSHIP_DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    # ---- HTTP: ZPay notify for a donation --------------------------------
    def test_zpay_notify_marks_donation_paid(self) -> None:
        user = self._new_user("d5@x.com")
        order = create_donation_order(user_id=int(user["id"]), amount_cents=3000)
        params = {
            "pid": "test-pid",
            "type": "alipay",
            "out_trade_no": order["order_no"],
            "trade_no": "ZP123456",
            "trade_status": "TRADE_SUCCESS",
            "money": _amount_to_yuan(3000),
            "name": "马恩文献检索 · 打赏支持",
            "param": f"user:{int(user['id'])}",
        }
        params["sign"] = app_module.PAYMENT_CLIENT.sign(params)
        params["sign_type"] = "MD5"
        resp = self.client.post("/payments/zpay/notify", data=params)
        self.assertEqual(resp.get_data(as_text=True), "success")
        refreshed = get_order_by_no(order["order_no"])
        self.assertEqual(refreshed["status"], "paid")
        self.assertEqual(list_subscriptions_for_user(int(user["id"])), [])
        self.assertFalse(get_membership_snapshot(int(user["id"])).is_active_member)

    def test_homepage_hides_donation_entry_by_default(self) -> None:
        # 首页「赞助」入口默认关闭：不出现赞助按钮或打赏弹层（后端 /donate 管线仍保留）。
        from admin_store import delete_setting

        delete_setting("index_sponsor_enabled")  # 确保为默认（未设置=关）
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn('id="sponsorBtn"', body)
        self.assertNotIn('id="donateForm"', body)

    def test_sponsor_toggle_shows_entry_when_enabled(self) -> None:
        # 后台开关打开后，首页顶栏出现「赞助」按钮 + 友情打赏弹层表单（控制台 /admin 可切）。
        from admin_store import set_setting, delete_setting

        set_setting("index_sponsor_enabled", "1")
        try:
            body = self.client.get("/").get_data(as_text=True)
            self.assertIn('id="sponsorBtn"', body)
            self.assertIn('action="/donate"', body)
        finally:
            delete_setting("index_sponsor_enabled")

    def test_pricing_page_no_longer_shows_donation(self) -> None:
        # 会员套餐页只保留套餐与期刊目录，不再内嵌打赏表单。
        resp = self.client.get("/pricing")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('id="donateForm"', resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
