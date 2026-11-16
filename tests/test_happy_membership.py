"""Happy's purchase, content rights and registered AI pool, without external calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from _test_env import APPDATA  # noqa: F401 - isolate application imports
import admin_store
import app as web
import feature_access as access
import journal_alerts
import membership as m
import membership_purchase as purchase


@pytest.fixture
def account(tmp_path, monkeypatch):
    db = tmp_path / "membership.sqlite3"
    monkeypatch.setattr(m, "DB_PATH", db)
    monkeypatch.setattr(web, "MEMBERSHIP_DB_PATH", db)
    monkeypatch.setattr(admin_store, "DB_PATH", db)
    monkeypatch.setattr(admin_store, "_SETTINGS_CACHE", {})
    monkeypatch.setattr(admin_store, "_SCHEMA_READY", False)
    monkeypatch.setattr(m, "MEMBER_EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(m, "MEMBER_EXPORT_FILE", tmp_path / "exports" / "members.ndjson")
    monkeypatch.setattr(web, "_enqueue_audit_write", lambda *a, **kw: None)
    monkeypatch.setattr(web, "_refresh_ai_runtime_if_needed", lambda: None)
    monkeypatch.setitem(web.app.config, "TESTING", True)
    monkeypatch.setattr(web, "_rate_buckets", {})
    m.init_membership_db()
    admin_store.init_admin_store_db()
    admin_store.set_setting(purchase.FEATURE_KEY, True)
    admin_store.set_setting(purchase.DISCOUNT_KEY, True)
    user = m.create_user(email="happy@example.test", display_name="Happy", password_hash="unused",
                         email_verified_at="2026-01-01T00:00:00+00:00")
    client = web.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user["id"]
        session["_csrf_token"] = "happy-csrf"
    return user, client


def buy(user, code=m.HAPPY_PLAN_CODE, months=1):
    order = m.create_pending_order(user_id=user["id"], plan_code=code, purchase_months=months)
    return m.mark_order_paid(order_no=order["order_no"], provider="test", payment_reference="test")


@contextmanager
def request_as(user, path="/"):
    with web.app.test_request_context(path):
        web.g.current_user = user
        web.g.membership = m.get_membership_snapshot(user["id"])
        yield


def test_catalog_seed_is_idempotent_and_preserves_operator_copy(account):
    plan = m.get_plan(m.HAPPY_PLAN_CODE)
    assert (plan["name"], plan["price_cents"], plan["billing_cycle_days"]) == ("happy 会员", 666, 30)
    assert plan["tier_rank"] < m.get_plan("monthly")["tier_rank"]
    assert plan["sort_order"] < m.get_plan("support_basic")["sort_order"]
    with m._connect() as conn:
        conn.execute("UPDATE plans SET description='operator copy' WHERE code=?", (m.HAPPY_PLAN_CODE,))
        conn.commit()
    m.init_membership_db()
    assert m.get_plan(m.HAPPY_PLAN_CODE)["description"] == "operator copy"
    assert len([p for p in m.list_active_plans() if p["code"] == m.HAPPY_PLAN_CODE]) == 1


def test_payment_renewal_and_expiry_do_not_create_wallet(account, monkeypatch):
    user, _ = account
    result = buy(user)
    order, sub = result["order"], result["subscription"]
    assert order["amount_cents"] == 666
    assert datetime.fromisoformat(sub["expires_at"]) - datetime.fromisoformat(sub["starts_at"]) == timedelta(days=30)
    repeat = m.mark_order_paid(order_no=order["order_no"], provider="test")
    assert repeat["subscription"]["id"] == sub["id"]
    renewal = buy(user)["subscription"]
    assert renewal["starts_at"] == sub["expires_at"]
    assert m.get_membership_snapshot(user["id"]).plan_code == m.HAPPY_PLAN_CODE
    assert m.get_ai_entitlements(user["id"])["wallets"] == []
    with m._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_wallets").fetchone()[0] == 0
    monkeypatch.setattr(m, "utc_now", lambda: datetime.fromisoformat(renewal["expires_at"]))
    assert not m.get_membership_snapshot(user["id"]).is_active_member
    assert not access.feature_allowed_by_policy(access.load_access_policy(), "library", user)


@pytest.mark.parametrize("graduated,months,total", [
    (True, 3, 1798), (True, 4, 2264), (True, 5, 2664), (True, 6, 2997),
    (False, 4, 2398), (False, 6, 3197),
])
def test_multimonth_amount_and_periods(account, graduated, months, total):
    user, _ = account
    admin_store.set_setting(purchase.DISCOUNT_KEY, graduated)
    result = buy(user, months=months)
    assert result["order"]["amount_cents"] == total
    assert len(result["subscriptions"]) == months
    for previous, current in zip(result["subscriptions"], result["subscriptions"][1:]):
        assert previous["expires_at"] == current["starts_at"]
    assert m.get_ai_entitlements(user["id"])["wallets"] == []


@pytest.mark.parametrize("elapsed,amount", [(10, 890), (20, 445)])
def test_upgrade_budget_refund_and_second_upgrade(account, monkeypatch, elapsed, amount):
    user, _ = account
    clock = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(m, "utc_now", lambda: clock)
    original = buy(user)
    clock += timedelta(days=elapsed)
    upgrade = buy(user, m.PLUS_CODE)
    # 1,334 cents difference, rounded up after prorating the current month.
    assert upgrade["order"]["amount_cents"] == amount
    assert m.get_membership_snapshot(user["id"]).expires_at == original["subscription"]["expires_at"]
    wallet = m.get_ai_entitlements(user["id"])["wallets"][0]
    assert wallet["budget_micros"] == amount * 9000
    assert wallet["released_micros"] == amount * 9000 // (2 if elapsed < 15 else 1)
    m.mark_order_paid(order_no=upgrade["order"]["order_no"], provider="test")
    assert m.get_ai_entitlements(user["id"])["total_micros"] == amount * 9000
    with pytest.raises(ValueError, match="不能降档"):
        m.create_pending_order(user_id=user["id"], plan_code=m.HAPPY_PLAN_CODE)
    assert m.reverse_paid_order(order_no=upgrade["order"]["order_no"], reason="test")["reversed"]
    assert m.get_membership_snapshot(user["id"]).plan_code == m.HAPPY_PLAN_CODE
    assert m.get_ai_entitlements(user["id"])["wallets"] == []
    assert not m.reverse_paid_order(order_no=upgrade["order"]["order_no"], reason="test")["reversed"]
    buy(user, m.PLUS_CODE)
    assert m.get_ai_entitlements(user["id"])["total_micros"] == amount * 9000


def test_checkout_uses_server_price(account, monkeypatch):
    _, client = account
    checkout = Mock(side_effect=lambda order, plan, user: {"amount": order["amount_cents"], "code": plan["code"]})
    monkeypatch.setattr(web, "_build_payment_checkout_redirect", checkout)
    response = client.post("/checkout/support_happy", data={"csrf_token": "happy-csrf", "amount_cents": "1"})
    assert response.status_code == 200
    assert response.get_json() == {"amount": 666, "code": m.HAPPY_PLAN_CODE}
    checkout.assert_called_once()


def test_new_order_retries_a_colliding_number(account, monkeypatch):
    user, _ = account
    clock = m.utc_now()
    monkeypatch.setattr(m, "utc_now", lambda: clock)
    monkeypatch.setattr(m.secrets, "randbelow", Mock(side_effect=[42, 42, 43]))
    first = buy(user)["order"]
    second = m.create_pending_order(user_id=user["id"], plan_code=m.HAPPY_PLAN_CODE)
    assert first["order_no"].endswith("00042")
    assert second["order_no"].endswith("00043")


@pytest.mark.parametrize("code", [m.PLUS_CODE, "support_pro", "support_max"])
def test_existing_paid_tiers_keep_rights_and_wallets(account, code):
    user, _ = account
    result = buy(user, code)
    assert m.get_ai_entitlements(user["id"])["total_micros"] == result["order"]["amount_cents"] * 9000
    assert all(access.feature_allowed_by_policy(access.load_access_policy(), key, user)
               for key in access.HAPPY_EXCLUDED_FEATURES - {"ai_web"})


def test_refund_cancels_all_purchased_happy_periods(account):
    user, _ = account
    result = buy(user, months=3)
    assert m.reverse_paid_order(order_no=result["order"]["order_no"], reason="test")["reversed"]
    assert not m.get_membership_snapshot(user["id"]).is_active_member
    assert all(s["status"] == "refunded" for s in m.list_subscriptions_for_user(user["id"]))


def test_legacy_membership_wins_and_happy_does_not_add_paid_quota(account):
    user, _ = account
    buy(user)
    now = m.utc_now()
    with m._connect() as conn:
        conn.execute(
            "INSERT INTO subscriptions(user_id,plan_code,status,source,starts_at,expires_at,notes,created_at,updated_at) "
            "VALUES(?,'monthly','active','test',?,?,'',?,?)",
            (user["id"], (now-timedelta(days=1)).isoformat(), (now+timedelta(days=10)).isoformat(),
             now.isoformat(), now.isoformat()))
        conn.commit()
    assert m.get_membership_snapshot(user["id"]).plan_code == "monthly"
    assert m.list_users(search_text=user["email"])[0]["membership_plan_code"] == "monthly"
    with m._connect() as conn:
        assert m._current_tier_plan(conn, user["id"], m.utc_now_text())[0] == "monthly"
    policy = access.load_access_policy()
    assert all(access.feature_allowed_by_policy(policy, feature, user)
               for feature in access.HAPPY_EXCLUDED_FEATURES - {"ai_web"})
    assert m.get_user_weekly_token_entitlement(user["id"])["weekly_limit"] == 280_000


def test_feature_matrix_and_operator_overrides(account):
    user, _ = account
    buy(user)
    policy = access.load_access_policy()
    for feature in access.FEATURE_ACCESS_KEYS:
        assert access.feature_allowed_by_policy(policy, feature, user) == (feature not in access.HAPPY_EXCLUDED_FEATURES)
    row = access.plan_feature_access_rows([m.get_plan(m.HAPPY_PLAN_CODE)], policy)[0]
    assert not row["values"]["journal_alerts"]
    policy["global"]["library"] = False
    assert not access.feature_allowed_by_policy(policy, "library", user)
    policy["users"][user["email"]] = {"library": True}
    assert access.feature_allowed_by_policy(policy, "library", user)
    assert access.feature_allowed_by_policy(policy, "journal_alerts", {**user, "role": "admin"})


def test_partial_saved_policy_keeps_happy_exclusions(account):
    user, _ = account
    buy(user)
    admin_store.set_setting("access_policy", {"plans": {m.HAPPY_PLAN_CODE: {"ai": True}}})
    policy = access.load_access_policy()
    assert not access.feature_allowed_by_policy(policy, "personal_library", user)
    assert not access.feature_allowed_by_policy(policy, "search_chat", user)
    assert not access.feature_allowed_by_policy(policy, "research", user)


def test_current_research_updates_denied_and_member_mailing_skips_happy(account, monkeypatch):
    import research_updates

    user, client = account
    buy(user)
    monkeypatch.setattr(research_updates, "get_issue", lambda _: {"status": "published"})
    assert client.get("/research-updates/1").status_code == 403
    assert client.get("/api/account/research-subscriptions").status_code == 403
    assert journal_alerts.resolve_recipients("members")[0] == []


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/ai/search-chat", {"question": "test"}),
    ("post", "/api/search/associative", {"mode": "research", "query": "test"}),
    ("post", "/api/citation-agent/jobs", {}),
    ("get", "/api/citation-agent/jobs/not-owned", None),
    ("get", "/citation-agent/jobs/not-owned", None),
    ("post", "/api/citation-assistant/jobs", {}),
    ("get", "/api/citation-assistant/jobs/not-owned/download/docx", None),
    ("get", "/api/mylib/books", None),
    ("post", "/api/mylib/upload", {}),
    ("get", "/api/mylib/page-image?submission_id=1&page=1", None),
    ("post", "/account/journal-alerts/subscribe", {}),
])
def test_excluded_direct_routes_deny_before_work(account, monkeypatch, method, path, body):
    user, client = account
    buy(user)
    monkeypatch.setattr(web.mylib_store, "configured", lambda: True)
    response = client.open(path, method=method.upper(), json=body, headers={"X-CSRF-Token": "happy-csrf"})
    assert response.status_code == 403


def test_journal_download_and_delivery_respect_happy(account, monkeypatch):
    user, client = account
    buy(user)
    monkeypatch.setattr(web, "get_public_journal_article", lambda _: {"id": 1, "batch_id": 1, "pdf_url": "https://example.test/paper.pdf"})
    monkeypatch.setattr(web, "get_batch", lambda _: {"id": 1, "status": "published"})
    for path in ("/journal-alerts/pdf/1", "/journal-alerts/articles/1", "/journal-alerts/assets/1/test.png"):
        response = client.get(path)
        assert response.status_code == 302
        assert "member_required=1" in response.location
    subscription = {"user_id": user["id"], "user_account_email": user["email"], "is_active": 1}
    assert not journal_alerts.subscription_is_deliverable(subscription)
    monkeypatch.setattr(web, "get_batch", lambda _: {"id": 1, "status": "sample"})
    assert client.get("/journal-alerts/pdf/1").location == "https://example.test/paper.pdf"


def test_pricing_account_and_ai_config_explain_happy(account):
    user, client = account
    buy(user)
    pricing = client.get("/pricing")
    assert pricing.status_code == 200
    text = pricing.get_data(as_text=True)
    assert "6.66" in text and "happy 会员" in text and "每周 2 万 token" in text
    page = client.get("/account")
    assert page.status_code == 200 and "happy 会员" in page.get_data(as_text=True)
    config = client.get("/api/ai/assistant-config").get_json()
    assert not config["access"] and not config["research_access"]
    assert config["access_gate"] == "upgrade"
    assert "升级" in config["locked_message"]
    assert not config["conversation_sync_eligible"]
    assert config["ai_entitlements"]["models"] == {"deepseek-v4-flash": ["off"]}
    assert config["quota"]["limit"] == 20_000


def test_registered_usage_survives_purchase_and_renewal_and_exhausts(account):
    user, _ = account
    m.record_ai_usage(user_id=user["id"], day=m.china_day_text(), feature="pdf-chat", total_tokens=7_000)
    buy(user)
    buy(user)
    with request_as(user):
        quota = web._ai_token_quota_payload(user)
        assert (quota["limit"], quota["used"], quota["remaining"]) == (20_000, 7_000, 13_000)
        selected = web._resolve_ai_selection_or_abort({"model": "deepseek-v4-pro", "reasoning_effort": "high"}, feature="pdf-chat")
        assert selected["model"] == "deepseek-v4-flash" and selected["reasoning_effort"] == "off"
        assert not selected["charge_user_wallet"]
    m.record_ai_usage(user_id=user["id"], day=m.china_day_text(), feature="wenku_ai", total_tokens=13_000)
    with request_as(user):
        with pytest.raises(web._AIQuotaExceeded):
            web._require_ai_quota_or_raise()


def test_previous_week_usage_does_not_consume_current_pool(account):
    user, _ = account
    buy(user)
    with request_as(user):
        window = web._research_quota_week_window()
        prior_day = (datetime.fromisoformat(window["start_day"])-timedelta(days=1)).date().isoformat()
        m.record_ai_usage(user_id=user["id"], day=prior_day, feature="pdf-chat", total_tokens=20_000)
        assert web._ai_token_quota_payload(user)["remaining"] == 20_000


def test_credit_pack_does_not_unlock_excluded_features(account):
    user, _ = account
    buy(user)
    buy(user, "research_pack")
    assert m.get_ai_entitlements(user["id"])["total_micros"] > 0
    assert m.get_user_weekly_token_entitlement(user["id"])["weekly_limit"] is None
    assert all(not access.feature_allowed_by_policy(access.load_access_policy(), key, user)
               for key in access.HAPPY_EXCLUDED_FEATURES)


def test_happy_cannot_enable_or_extend_cloud_sync_but_keeps_read_delete(account, monkeypatch):
    user, client = account
    buy(user)
    monkeypatch.setattr(web.mylib_store, "configured", lambda: True)
    for path, body in (("/api/ai/conversations/consent", {"enabled": True}),
                       ("/api/ai/conversations/old/extend", {}),
                       ("/api/ai/conversations/new", {"conversation": {}})):
        method = "PUT" if path.endswith("/new") else "POST"
        assert client.open(path, method=method, json=body, headers={"X-CSRF-Token": "happy-csrf"}).status_code == 403
    reader = Mock(return_value={"ok": True, "conversations": []})
    deleter = Mock(return_value={"ok": True})
    monkeypatch.setattr(web.mylib_store, "list_conversations", reader)
    monkeypatch.setattr(web.mylib_store, "delete_conversation", deleter)
    assert client.get("/api/ai/conversations").status_code == 200
    assert client.delete("/api/ai/conversations/old", headers={"X-CSRF-Token": "happy-csrf"}).status_code == 200
    reader.assert_called_once_with(user["id"])
    deleter.assert_called_once_with(user["id"], "old")


def test_happy_does_not_extend_previous_cloud_grace(account):
    user, _ = account
    old = buy(user, m.PLUS_CODE)["subscription"]
    expiry = m.utc_now() - timedelta(days=3)
    with m._connect() as conn:
        conn.execute("UPDATE subscriptions SET starts_at=?,expires_at=? WHERE id=?",
                     ((expiry-timedelta(days=30)).isoformat(), expiry.isoformat(), old["id"]))
        conn.commit()
    buy(user)
    with request_as(user):
        policy = web._ai_conversation_membership_policy(user)
        assert not policy["eligible"] and not policy["can_create"]
        assert policy["grace_active"] and policy["can_update_existing"]
        assert policy["retention_floor_ms"] == 0
        assert policy["grace_until_ms"] == int((expiry+timedelta(days=30)).timestamp()*1000)
