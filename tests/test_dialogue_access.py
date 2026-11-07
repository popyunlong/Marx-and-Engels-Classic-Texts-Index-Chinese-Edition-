"""First-run coverage for registered/member rollout, without real user stores."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from flask import Flask, g
import comprehensive_web as routes


@pytest.mark.parametrize('role', ['registered', 'member', 'admin'])
def test_comprehensive_uses_chat_permission_for_all_logged_in_roles(role):
    app=Flask(__name__)
    stub=SimpleNamespace(_env_flag=lambda name,default: default,
        _is_admin_user=lambda user: user.get('role')=='admin',
        _feature_effective_for_user=lambda feature: feature=='search_chat')
    with app.test_request_context('/'):
        g.current_user={'id':1,'role':role}
        with patch.object(routes,'web_allowed',return_value=False):
            assert routes.capabilities(stub)['comprehensive_access'] is True
            stub._feature_effective_for_user=lambda _:False
            assert routes.capabilities(stub)['comprehensive_access'] is False


def test_anonymous_and_rollout_kill_switch_cannot_gain_access():
    app=Flask(__name__)
    stub=SimpleNamespace(_env_flag=lambda name,default:False,_is_admin_user=lambda _:False,
        _feature_effective_for_user=lambda _:True)
    with app.test_request_context('/'):
        assert routes.enabled(stub) is False
        g.current_user={'id':1,'role':'registered'}
        assert routes.enabled(stub) is False


@pytest.mark.parametrize('quota', [
    {'weekly_limit':8000,'weekly_used':0},
    {'weekly_limit':None,'weekly_used':90000},
    {'weekly_limit':0,'weekly_used':8000,'over_free_limit':True,'credit_balance':1},
])
def test_registered_flash_uses_existing_free_unlimited_or_resource_pack_quota(quota):
    import app as A
    from test_comprehensive_mode import route_fixture
    selections=[]
    with route_fixture() as (post,calls,_):
        def select(selection,**kwargs):
            assert selection['model']=='deepseek-v4-flash' and selection['reasoning_effort']=='off'
            selections.append(selection)
            return {**selection,'charge_user_wallet':False}
        with patch.object(A,'_is_admin_user',return_value=False), \
             patch.object(A,'_require_ai_quota_or_raise',return_value=dict(quota)), \
             patch.object(A,'_resolve_ai_selection_or_abort',side_effect=select):
            result=post({'question':'注册用户综合问答','request_id':'registered-access-once'})
    assert result['ok'] and result['completion_status']=='complete',result
    assert result['actual_model']['model']=='deepseek-v4-flash' and len(calls)==2
    assert selections
