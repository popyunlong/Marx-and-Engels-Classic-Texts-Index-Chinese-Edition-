import pytest
from flask import g
from werkzeug.exceptions import Forbidden

from _test_env import APPDATA
import app as web


def test_guest_documentation_and_legacy_redirect():
    client = web.app.test_client()
    page = client.get('/api-access')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    for text in ('文献 API', 'REST API', 'MCP 接入', '登录管理 Key', '/api/v1/search', '/mcp'):
        assert text in html
    assert 'name="action" value="create"' not in html
    assert page.headers['Cache-Control'] == 'private, no-store'
    old = client.get('/account/agent-api')
    assert old.status_code == 302 and old.location.endswith('/api-access')
    assert 'api_access' not in web.CSRF_EXEMPT_ENDPOINTS


def test_both_key_management_routes_share_store_and_csrf(monkeypatch):
    user = {'id': 91234, 'email': 'chapter-api@example.test', 'is_active': 1, 'role': 'user'}
    monkeypatch.setattr(web, '_membership_plan_code_for_user', lambda u: 'support')
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
    monkeypatch.setattr(web, '_membership_plan_code_for_user', lambda u: 'support')
    monkeypatch.setattr(web, '_feature_effective_for_user', lambda feature, u: feature != 'agent_api')
    with web.app.test_request_context('/api-access'):
        g.current_user = user
        html = web.app.view_functions['api_access']().get_data(as_text=True)
        assert '当前账号没有接口使用资格' in html
        assert 'name="action" value="create"' not in html
