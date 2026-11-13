import pytest
from flask import g, render_template
from html import unescape
from werkzeug.exceptions import Forbidden

from _test_env import APPDATA
import app as web


def test_guest_documentation_and_legacy_redirect():
    client = web.app.test_client()
    page = client.get('/api-access')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    for text in ('API/MCP 数据调用', 'REST API', 'MCP 接入', '登录管理 Key', '/api/v1/search', '/mcp'):
        assert text in html
    assert 'name="action" value="create"' not in html
    assert page.headers['Cache-Control'] == 'private, no-store'
    old = client.get('/account/agent-api')
    assert old.status_code == 302 and old.location.endswith('/api-access')
    assert 'api_access' not in web.CSRF_EXEMPT_ENDPOINTS


@pytest.mark.parametrize('has_results', [True, False])
def test_documented_python_workflow_uses_returned_scope_and_evidence(monkeypatch, has_results):
    import io
    import json
    import urllib.request
    monkeypatch.setenv('MARX_API_KEY', 'example-only-key')
    calls = []
    replies = [
        {'sources': [{'book': '文集', 'scope': 'book:文集'}]},
        {'results': [{'evidence_id': 'a' * 64}] if has_results else [],
         'search_complete': True, 'truncated': False},
    ]
    if has_results:
        replies.append({'result': {'text': '原文', 'printed_pages': ['12'], 'pdf_pages': [24]}})
    replies.append({'status': 'available'})
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 30
            assert request.get_header('Authorization') == 'Bearer example-only-key'
            assert request.get_header('User-agent') == 'MarxCitationClient/1.0'
            return io.BytesIO(json.dumps(replies.pop(0)).encode())
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    with web.app.test_request_context('/api-access'):
        code = unescape(render_template('_api_rest_example.html', api_base='https://example.test'))
    exec(compile(code, '<documented REST example>', 'exec'), {})
    assert not replies
    assert calls[0].full_url == 'https://example.test/api/v1/sources'
    assert json.loads(calls[1].data)['scope'] == ['book:文集']
    assert calls[-1].full_url.endswith('/limits')
    if has_results:
        assert calls[2].full_url.endswith('/passages/' + 'a' * 64)


def test_documented_python_error_is_bounded_and_redacted(monkeypatch):
    import io
    import urllib.error
    import urllib.request
    key = 'example-sensitive-key'
    monkeypatch.setenv('MARX_API_KEY', key)
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden',
                {'CF-Ray': 'ray-' + key, 'Content-Type': 'text/plain'},
                io.BytesIO(('error code: 1010 ' + key + 'x' * 5000).encode()))
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    with web.app.test_request_context('/api-access'):
        code = unescape(render_template('_api_rest_example.html', api_base='https://example.test'))
    with pytest.raises(SystemExit) as error:
        exec(compile(code, '<documented REST example>', 'exec'), {})
    assert len(calls) == 1
    assert key not in str(error.value)
    assert '1010' in str(error.value) and '[REDACTED]' in str(error.value)
    assert len(str(error.value)) < 1000


def test_both_key_management_routes_share_store_and_csrf(monkeypatch):
    user = {'id': 91234, 'email': 'chapter-api@example.test', 'is_active': 1, 'role': 'user'}
    monkeypatch.setattr(web, '_membership_plan_code_for_user', lambda u: 'support_pro')
    monkeypatch.setattr(web, '_feature_effective_for_user', lambda feature, u: feature != 'citation_assistant')
    calls = []
    monkeypatch.setattr(web, '_require_csrf', lambda: calls.append('csrf'))
    for path, endpoint in [('/account/agent-api', 'agent_api_center'), ('/api-access', 'api_access')]:
        with web.app.test_request_context(path, method='POST', data={'action': 'create', 'name': path}):
            g.current_user = user
            page = web.app.view_functions[endpoint]()
            assert page.status_code == 200
            assert '保存你的新 Key' in page.get_data(as_text=True)
    store = web.app.extensions['agent_api']['store']
    assert len(store.keys(user['id'])) == 2 and calls == ['csrf', 'csrf']
    monkeypatch.setattr(web, '_require_csrf', lambda: (_ for _ in ()).throw(Forbidden()))
    with web.app.test_request_context('/api-access', method='POST', data={'action': 'create', 'name': 'blocked'}):
        g.current_user = user
        with pytest.raises(Forbidden):
            web.app.view_functions['api_access']()
    assert len(store.keys(user['id'])) == 2


def test_api_permission_does_not_follow_agent_permission(monkeypatch):
    user = {'id': 91235, 'email': 'disabled-api@example.test', 'is_active': 1, 'role': 'user'}
    monkeypatch.setattr(web, '_membership_plan_code_for_user', lambda u: 'support_pro')
    monkeypatch.setattr(web, '_feature_effective_for_user', lambda feature, u: feature != 'agent_api')
    with web.app.test_request_context('/api-access'):
        g.current_user = user
        html = web.app.view_functions['api_access']().get_data(as_text=True)
        assert '当前账号没有接口使用资格' in html
        assert 'name="action" value="create"' not in html
