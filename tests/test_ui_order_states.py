"""Render terminal orders and exercise cancelled/late AI replies without services."""
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess

from jinja2 import Environment, FileSystemLoader
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('status,label', [
    ('expired', '订单已过期'), ('cancelled', '订单已取消'),
    ('failed', '支付未完成'), ('refunded', '订单已退款'),
    ('pending', 'WAITING_PAYMENT'), ('paid', 'PAYMENT_COMPLETE'),
])
def test_payment_state_has_correct_message_and_retry(status, label):
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'))
    env.globals.update(
        get_flashed_messages=lambda **kw: [], url_for=lambda *a, **kw: '/test',
        site_text=lambda key: {'payment_result.status_pending': 'WAITING_PAYMENT',
                              'payment_result.status_paid': 'PAYMENT_COMPLETE'}.get(key, key),
        format_price=lambda *a: '75.00', format_order_status=lambda value: value,
        format_payment_provider=lambda value: value, format_membership_status=lambda value: value,
    )
    body = env.get_template('payment_result.html').render(
        order={'status': status, 'order_no': 'test', 'plan_name': 'Max会员',
               'amount_cents': 7500, 'currency': 'CNY', 'payment_provider': 'test'},
        membership_snapshot=SimpleNamespace(plan_name='Max会员', status='active', expires_at=None),
    )
    assert label in body
    assert ('继续支付' in body) == (status == 'pending')
    assert ('WAITING_PAYMENT' in body) == (status == 'pending')


def test_cancelled_ai_reply_cannot_replace_new_request():
    node = shutil.which('node')
    assert node, 'Node is required for the AI cancellation regression gate'
    subprocess.run([node, str(ROOT / 'tests/ai_stop_harness.cjs')], cwd=ROOT,
                   check=True, timeout=20)
