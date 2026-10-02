"""Provider wire names, stable entitlements and effective dated Flash prices."""
from dataclasses import replace
import json
from unittest import mock

import pytest

from _test_env import APPDATA  # noqa: F401
import ai
import app as A
import membership as M
from ai_models import application_model, provider_model


@pytest.mark.parametrize('name', ['deepseek-v4-flash', 'deepseek-flash', 'deepseek-v4-flash-vision-exp'])
def test_flash_names_share_one_right_and_current_wire_name(name):
    assert application_model(name) == 'deepseek-v4-flash'
    assert provider_model(name) == 'deepseek-flash'
    assert A._resolve_selectable_model({'model': name}) == 'deepseek-v4-flash'
    policy = M._effective_model_policy({'models': {name: ['off', 'high']},
                                      'defaults': {'quick': {'model': name, 'reasoning_effort': 'off'}}})
    assert policy['models'] == {'deepseek-v4-flash': ['off', 'high']}
    assert policy['defaults']['quick']['model'] == 'deepseek-v4-flash'
    assert M._normalized_ai_selection('deepseek', name, 'high') == ('deepseek', 'deepseek-v4-flash', 'high')


@pytest.mark.parametrize('effort,thinking', [('off', 'disabled'), ('high', 'enabled')])
def test_nonstream_flash_keeps_thinking_and_token_budget(effort, thinking):
    client = ai.ZAIClient(replace(A.AI_CLIENT.config, provider='deepseek', model='deepseek-v4-flash'))
    response = {'choices': [{'message': {'content': '完整的回答。'}}]}
    with mock.patch.object(client, '_post_json', return_value=response) as post:
        result = client.chat_complete([{'role': 'user', 'content': '问题'}], max_tokens=6000,
                                      model='deepseek-v4-flash', reasoning_effort=effort)
    payload = post.call_args.args[1]
    assert result == '完整的回答。'
    assert payload['model'] == 'deepseek-flash'
    assert payload['thinking'] == {'type': thinking}
    assert payload['max_tokens'] == 6000


def test_prices_effective_date_alias_and_weekday_weekend_holiday():
    M.init_membership_db()
    checks = [
        ('2026-09-09T02:00:00+00:00', 'peak', (100_000, 3_000_000, 9_000_000)),
        ('2026-09-11T02:00:00+00:00', 'peak', (40_000, 2_000_000, 8_000_000)),
        ('2026-09-11T04:00:00+00:00', 'offpeak', (20_000, 1_000_000, 4_000_000)),
        ('2026-09-12T02:00:00+00:00', 'offpeak', (20_000, 1_000_000, 4_000_000)),
        ('2026-10-02T02:00:00+00:00', 'offpeak', (20_000, 1_000_000, 4_000_000)),
        ('2026-10-10T02:00:00+00:00', 'offpeak', (20_000, 1_000_000, 4_000_000)),
    ]
    for when, band, prices in checks:
        for name in ('deepseek-v4-flash', 'deepseek-flash'):
            row = M.resolve_ai_price(provider='deepseek', model=name, occurred_at=when)
            assert row['resolved_time_band'] == band
            assert tuple(row[k] for k in ('cache_input_per_million_micros', 'input_per_million_micros', 'output_per_million_micros')) == prices


def test_agent_worker_payload_uses_new_name_without_changing_endpoint(monkeypatch):
    from scripts import citation_agent_worker as worker
    monkeypatch.setenv('CITATION_AGENT_API_KEY', 'test-key-123456789')
    monkeypatch.setenv('CITATION_AGENT_MODEL', 'deepseek-v4-flash')
    endpoint, _, model = worker._endpoint()
    assert endpoint == 'https://api.deepseek.com/chat/completions'
    assert provider_model(model) == 'deepseek-flash'


def test_other_models_keep_their_identity():
    for model in ('deepseek-v4-pro', 'mimo-v2.5', 'mimo-v2.5-pro', 'glm-5.1'):
        assert application_model(model) == provider_model(model) == model


def test_canonical_wire_name_cannot_bypass_wallet_reservation_or_usage():
    event = {'provider': 'deepseek', 'model': 'deepseek-flash', 'user_id': 123,
             'feature': 'search-chat', 'charge_user': True, 'request_id': 'test',
             'max_completion_tokens': 6000, 'phase': 'before'}
    with mock.patch.object(A, 'reserve_ai_budget', return_value={'id': 42}) as reserve:
        preflight = A._ai_provider_call_sink(event)
    assert preflight['reservation_id'] == 42
    assert reserve.call_args.kwargs['model'] == 'deepseek-v4-flash'
    with mock.patch.object(A, 'record_ai_provider_call', return_value={'id': 43}) as record:
        result = A._ai_provider_call_sink({**event, 'phase': 'after', 'preflight': preflight, 'success': True})
    assert result['id'] == 43
    assert record.call_args.kwargs['reservation_id'] == 42
    assert record.call_args.kwargs['model'] == 'deepseek-v4-flash'


def test_stream_flash_uses_current_model_and_keeps_reasoning_out_of_answer():
    import io
    body = b'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}\n\ndata: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n'
    with mock.patch.object(ai.urllib_request, 'urlopen', return_value=io.BytesIO(body)) as opened:
        answer = ''.join(A.AI_CLIENT.chat_complete_stream([{'role': 'user', 'content': 'question'}], 200,
                       model='deepseek-v4-flash', reasoning_effort='high'))
    payload = json.loads(opened.call_args.args[0].data)
    assert payload['model'] == 'deepseek-flash'
    assert payload['thinking'] == {'type': 'enabled'}
    assert payload['reasoning_effort'] == 'high'
    assert answer == 'answer'
