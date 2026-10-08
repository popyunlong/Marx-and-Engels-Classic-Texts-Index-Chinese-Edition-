from types import SimpleNamespace
import pytest
from _test_env import APPDATA  # noqa: F401
import app as web


@pytest.mark.parametrize('scope,expected', [
    ({'new': {2}}, {'new': {2}}),
    ({'new': {2}, 'private': None}, {'new': {2}}),
    ({'private': None}, {}),
    (None, {'old', 'new'}),
])
def test_selected_public_volumes_reach_capped_cooccurrence_scan(monkeypatch, scope, expected):
    seen=[]
    def capped_search(keywords, *, book_scope, **kwargs):
        seen.append(book_scope)
        return dict(query='自由 政治', groups=[], truncated=False)
    monkeypatch.setattr(web,'corpus',SimpleNamespace(search_cooccurrence_grouped=capped_search))
    monkeypatch.setattr(web,'_public_book_keys',lambda:{'old','new'})
    monkeypatch.setattr(web,'_standard_search_scope',lambda _:scope)
    for name in ('_require_search','_require_feature','_rate_limit_or_abort','_record_community_trend'):
        monkeypatch.setattr(web,name,lambda *a,**k:None)
    monkeypatch.setattr(web,'current_view_state',lambda:{'pdf_enabled':False})
    with web.app.test_request_context('/api/search',method='POST',json={
            'q':'自由 政治','mode':'cooccurrence','scope':['vol:new:2']}):
        response=web.api_search()
    assert response.status_code==200
    assert seen==[expected]
