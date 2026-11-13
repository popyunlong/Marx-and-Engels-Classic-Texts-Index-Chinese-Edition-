"""Shared membership-store compatibility while two releases serve traffic."""
import subprocess
import sys
import types
from datetime import timedelta
from pathlib import Path

from membership_refresh_fixture import A, M, login, user
from member_catalog import PLUS_CODE, RETIRED_PLUS_CODES


def load_predecessor(monkeypatch, name):
    revision = "ba1d884aeac0cd1bb2a505eaf2971920f350e29b"
    source = subprocess.check_output(
        ["git", "show", f"{revision}:{name}.py"],
        cwd=Path(__file__).resolve().parents[1],
    )
    module = types.ModuleType(f"cutover_predecessor_{name}")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, f"{revision}:{name}.py", "exec"), module.__dict__)
    return module


def test_candidate_seed_preserves_predecessor_catalog_and_rollback_wallet(tmp_path, monkeypatch):
    db = tmp_path / "membership.sqlite3"
    monkeypatch.setattr(M, "DB_PATH", db)
    monkeypatch.setattr(M, "MEMBER_EXPORT_FILE", tmp_path / "members.ndjson")
    monkeypatch.setattr(M, "MEMBER_EXPORT_DIR", tmp_path)
    predecessor_purchase = load_predecessor(monkeypatch, "membership_purchase")
    with monkeypatch.context() as importing:
        importing.setitem(sys.modules, "membership_purchase", predecessor_purchase)
        predecessor = load_predecessor(monkeypatch, "membership")
    predecessor.DB_PATH = db
    predecessor.MEMBER_EXPORT_FILE = tmp_path / "members.ndjson"
    predecessor.MEMBER_EXPORT_DIR = tmp_path
    predecessor.init_membership_db()
    previous_sales = {plan["code"] for plan in predecessor.list_active_plans()}
    assert RETIRED_PLUS_CODES <= previous_sales
    assert PLUS_CODE not in previous_sales

    M.init_membership_db()
    assert {plan["code"] for plan in predecessor.list_active_plans()} == previous_sales
    candidate_sales = {plan["code"] for plan in M.list_active_plans()}
    assert candidate_sales == (previous_sales - RETIRED_PLUS_CODES) | {PLUS_CODE}

    member = user()
    order = M.create_pending_order(user_id=member["id"], plan_code=PLUS_CODE)
    # A payment notification can arrive after traffic returns to the predecessor.
    with monkeypatch.context() as settling:
        settling.setitem(sys.modules, "membership", predecessor)
        predecessor.mark_order_paid(order_no=order["order_no"], provider="fixture")
    assert predecessor.get_order_by_no(order["order_no"])["status"] == "paid"
    assert predecessor.get_membership_snapshot(member["id"]).plan_code == PLUS_CODE
    assert M.get_ai_entitlements(member["id"])["wallets"][0]["budget_micros"] == 18_000_000
    assert {plan["code"] for plan in predecessor.list_active_plans()} == previous_sales


def test_retired_pending_order_reopens_with_frozen_checkout(monkeypatch):
    member = user()
    order_no = f"cutover-pending-{member['id']}"
    now = M.utc_now()
    with M._connect() as db:
        db.execute(
            "INSERT INTO orders(order_no,user_id,plan_code,status,amount_cents,currency,created_at,expires_at) "
            "VALUES(?,?,'support_basic','pending',1500,'CNY',?,?)",
            (order_no, member["id"], now.isoformat(), (now + timedelta(hours=1)).isoformat()),
        )
    def checkout(order, plan, viewer, *, frozen_order=False):
        assert order["order_no"] == order_no
        assert order["amount_cents"] == 1500
        assert plan["is_active"] == 0
        assert viewer["id"] == member["id"]
        assert frozen_order
        return "frozen checkout"

    monkeypatch.setattr(A, "_build_payment_checkout_redirect", checkout)
    with A.app.test_client() as client:
        headers = login(client, member)
        response = client.post(f"/checkout/order/{order_no}", headers=headers)
        assert response.status_code == 200
        assert response.get_data(as_text=True) == "frozen checkout"
