"""Membership rights, scoped copy, and independently loaded admin panels."""
import json
import time
from datetime import timedelta

import pytest
from membership_refresh_fixture import A, M, user, login
import admin_store as S
import model_policies as P
import member_copy as C
import admin_dashboard as D


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    import urllib.request
    import smtplib
    def forbidden(*args, **kwargs):
        raise AssertionError('External service is forbidden')
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(smtplib, 'SMTP', forbidden)
    A.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    old = {key:S.get_setting(key) for key in (P.KEY,'site_texts','access_policy')}
    for key in old: S.delete_setting(key)
    yield
    for key,value in old.items():
        S.delete_setting(key) if value is None else S.set_setting(key,value)


def admin_login(client):
    u=user()
    M.update_user_account(u['id'],role='admin')
    headers=login(client,u)
    with client.session_transaction() as session:
        session['admin_2fa_verified_at']=M.utc_now_text()
    return headers


def reserve(uid, model=P.FLASH, effort='off', **kw):
    return M.reserve_ai_budget(user_id=uid,provider='deepseek',model=model,reasoning_effort=effort,
                              feature='research_review',estimated_prompt_tokens=50,max_completion_tokens=100,**kw)


@pytest.mark.parametrize('code',['support_basic','support_plus','support_plus_20'])
def test_plus_stale_wallet_cannot_restore_pro_or_thinking(code):
    u=user(code)
    with M._connect() as db:
        db.execute('UPDATE ai_wallets SET model_policy=? WHERE user_id=?',
                   (json.dumps(P.default_policy('support_max')),u['id']))
        before=dict(db.execute('SELECT * FROM ai_wallets WHERE user_id=?',(u['id'],)).fetchone())
        db.commit()
    rights=M.get_ai_entitlements(u['id'])
    assert rights['models']=={P.FLASH:['off']}
    for feature in ('quick','research_review','pdf-chat','comprehensive-chat'):
        selection=M.authorize_ai_selection(user_id=u['id'],feature=feature)
        assert (selection['model'],selection['reasoning_effort'])==(P.FLASH,'off')
    for model,effort in [(P.PRO,'high'),(P.FLASH,'high')]:
        with pytest.raises(ValueError): reserve(u['id'],model,effort)
    with M._connect() as db:
        after=dict(db.execute('SELECT * FROM ai_wallets WHERE user_id=?',(u['id'],)).fetchone())
        assert db.execute('SELECT COUNT(*) FROM ai_budget_reservations WHERE user_id=?',(u['id'],)).fetchone()[0]==0
    for key in ('budget_micros','released_micros','spent_micros','reserved_micros','expires_at','model_policy'):
        assert before[key]==after[key]
    assert __import__('member_catalog').api_eligible(code)


@pytest.mark.parametrize('code',['support_pro','support_max'])
def test_premium_and_individual_feature_boundary(code):
    u=user(code)
    assert M.get_ai_entitlements(u['id'])['models']=={P.FLASH:['high','off'],P.PRO:['high']}
    r=reserve(u['id'],P.PRO,'high')
    M.release_ai_reservation(r['id'])
    with pytest.raises(ValueError):
        M.authorize_ai_selection(user_id=u['id'],feature='pdf-chat',model=P.PRO,reasoning_effort='high')


def test_revocation_rechecks_open_request_resume_and_uncached_settings():
    u=user('support_plus_20')
    P.save('support_plus_20',P.make_policy(True,True,'pro'),expected_version=0,actor='test')
    selected=M.authorize_ai_selection(user_id=u['id'],feature='research_review')
    assert selected['model']==P.PRO
    pending=reserve(u['id'],P.PRO,'high',request_id='queued-before-revocation')
    # Independent process-equivalent transaction: bypass the settings TTL invalidator.
    with M._connect() as db:
        db.execute('UPDATE unified_settings SET value_json=? WHERE key=?',(json.dumps({'version':2,'plans':{}}),P.KEY));db.commit()
    with pytest.raises(ValueError): reserve(u['id'],P.PRO,'high',request_id='queued-before-revocation')
    with pytest.raises(ValueError): M.affordable_completion_tokens(user_id=u['id'],selection=selected,messages=[],requested=1000)
    with pytest.raises(ValueError): P.save('support_plus_20',None,expected_version=1,actor='stale')
    M.release_ai_reservation(pending['id'])
    assert M.get_ai_entitlements(u['id'])['models']=={P.FLASH:['off']}


def test_resource_pack_funds_only_its_own_thinking():
    u=user('support_plus_20')
    order=M.create_pending_order(user_id=u['id'],plan_code='research_pack')
    M.mark_order_paid(order_no=order['order_no'],provider='fixture')
    assert M.get_ai_entitlements(u['id'])['models']=={P.FLASH:['high','off']}
    reservation=reserve(u['id'],P.FLASH,'high')
    with M._connect() as db:
        codes=[row[0] for row in db.execute('SELECT w.plan_code FROM ai_reservation_allocations a JOIN ai_wallets w ON w.id=a.wallet_id WHERE a.reservation_id=?',(reservation['id'],))]
    assert codes==['research_pack']
    M.release_ai_reservation(reservation['id'])
    with pytest.raises(ValueError): reserve(u['id'],P.PRO,'high')


def test_pack_cannot_authorize_reusing_a_revoked_plus_allocation():
    u=user('support_plus_20')
    order=M.create_pending_order(user_id=u['id'],plan_code='research_pack')
    M.mark_order_paid(order_no=order['order_no'],provider='fixture')
    P.save('support_plus_20',P.make_policy(True,False,'thinking'),expected_version=0,actor='test')
    pending=reserve(u['id'],P.FLASH,'high',request_id='plus-and-pack-before-revocation')
    assert reserve(u['id'],P.FLASH,'high',request_id=pending['request_id'])['id']==pending['id']
    with M._connect() as db:
        codes=[row[0] for row in db.execute('SELECT w.plan_code FROM ai_reservation_allocations a JOIN ai_wallets w ON w.id=a.wallet_id WHERE a.reservation_id=?',(pending['id'],))]
    assert codes==['support_plus_20']
    P.save('support_plus_20',None,expected_version=1,actor='test')
    assert 'high' in M.get_ai_entitlements(u['id'])['models'][P.FLASH]
    with pytest.raises(ValueError,match='原请求使用的权益已变更'):
        reserve(u['id'],P.FLASH,'high',request_id=pending['request_id'])
    M.release_ai_reservation(pending['id'])
    fresh=reserve(u['id'],P.FLASH,'high',request_id='pack-after-revocation')
    with M._connect() as db:
        codes=[row[0] for row in db.execute('SELECT w.plan_code FROM ai_reservation_allocations a JOIN ai_wallets w ON w.id=a.wallet_id WHERE a.reservation_id=?',(fresh['id'],))]
        assert db.execute("SELECT reserved_micros FROM ai_wallets WHERE user_id=? AND plan_code='support_plus_20'",(u['id'],)).fetchone()[0]==0
    assert codes==['research_pack']
    M.release_ai_reservation(fresh['id'])


def test_startup_preserves_existing_policies_copy_and_accounting():
    u=user('support_plus')
    P.save('support_plus',P.make_policy(True,False,'thinking'),expected_version=0,actor='test')
    S.patch_site_texts({'pricing.plan.support_plus_20.description':'custom'},defaults=A.get_site_text_map(),actor='test')
    with M._connect() as db:
        before=dict(db.execute('SELECT * FROM ai_wallets WHERE user_id=?',(u['id'],)).fetchone())
        plan=db.execute("SELECT model_policy FROM plans WHERE code='support_plus'").fetchone()[0]
    M.init_membership_db()
    with M._connect() as db:
        after=dict(db.execute('SELECT * FROM ai_wallets WHERE user_id=?',(u['id'],)).fetchone())
        assert db.execute("SELECT model_policy FROM plans WHERE code='support_plus'").fetchone()[0]==plan
    assert before==after
    assert P.read()['version']==1
    assert S.get_setting('site_texts')['pricing.plan.support_plus_20.description']=='custom'


def test_model_admin_validation_csrf_and_nonadmin_denial():
    with A.app.test_client() as client:
        login(client,user('support_plus_20'))
        assert client.get('/admin/overview/data').status_code==403
        assert client.post('/admin/model-policies',data={'csrf_token':'refresh-fixture-csrf'}).status_code==403
        headers=admin_login(client)
        assert client.post('/admin/model-policies',data={'code':'support_plus_20','research':'pro','version':'0'}).status_code in (400,403)
        bad=client.post('/admin/model-policies',headers=headers,data={'code':'support_plus_20','research':'pro','version':'0'})
        assert bad.status_code==302 and P.read()['version']==0
        good=client.post('/admin/model-policies',headers=headers,data={'code':'support_pro','research':'flash','version':'0'})
        assert good.status_code==302 and P.read()['plans']['support_pro']['models']=={P.FLASH:['off']}


def test_copy_preview_save_reset_and_partial_merge():
    defaults=A.get_site_text_map();key='pricing.notice.purchase.body'
    S.set_setting('site_texts',{'index.notice_title':'保留公告','auto.a8b6aa6947':'四档旧规则'})
    with A.app.test_client() as client:
        headers=admin_login(client)
        data={'group':'purchase','action':'preview','site_text__'+key:'自定义购买说明 <script>坏脚本</script>',
              'original__'+key:defaults[key]}
        preview=client.post('/admin/membership-copy',headers=headers,data=data)
        assert preview.status_code==200 and '&lt;script&gt;' in preview.get_data(as_text=True)
        assert key not in S.get_setting('site_texts')
        data['action']='save'
        assert client.post('/admin/membership-copy',headers=headers,data=data).status_code==302
        html=client.get('/pricing').get_data(as_text=True)
        assert '自定义购买说明' in html and '四档旧规则' not in html
        assert S.get_setting('site_texts')['index.notice_title']=='保留公告'
        data['action']='reset';data['original__'+key]=data['site_text__'+key]
        assert client.post('/admin/membership-copy',headers=headers,data=data).status_code==302
        assert key not in S.get_setting('site_texts')
        assert client.post('/admin/site-texts',headers=headers,data={'site_text__index.notice_title':'局部修改'}).status_code==302
        assert S.get_setting('site_texts')['auto.a8b6aa6947']=='四档旧规则'


@pytest.mark.parametrize('module',['overview','members','ai','content'])
def test_admin_modules_do_not_load_unrelated_statistics_or_journals(monkeypatch,module):
    def unwanted(*a,**k): raise AssertionError('Unrelated loader executed')
    for name in ('_dashboard_metrics_for_day','_dashboard_history_rows','current_batch','journal_fulltext_storage_usage','render_journal_review_email'):
        monkeypatch.setattr(A,name,unwanted)
    if module!='members':monkeypatch.setattr(A,'list_users',unwanted)
    with A.app.test_client() as client:
        admin_login(client)
        response=client.get('/admin/'+module)
        assert response.status_code==200,response.get_data(as_text=True)[:1000]
        assert 'Server-Timing' in response.headers
        if module=='members':assert '套餐模型权限' in response.get_data(as_text=True)


def test_content_editors_keep_their_saved_values_when_loading_is_scoped():
    # These editors live under content, although their settings affect the homepage.
    with A.app.test_client() as client:
        admin_login(client)
        with A.app.test_request_context('/admin/content'):
            context=A._management_console_context(remote_admin=True,admin_module='content')
            assert context['feature_tags']==A._get_feature_tags()
            assert context['citation_formats_editor']==A._citation_formats_editor()
            assert context['citation_formats_editor']
            assert [c['key'] for c in context['card_order_cards']]==A._get_card_order()
            assert context['card_order_cards']
            assert context['registry_geo_settings']==A._registry_geo_editor_settings()
            assert context['ai_assistant_mode_setting']==A._ai_assistant_mode()
            assert context['sponsor_enabled_setting']==A._sponsor_button_enabled()


def test_history_batch_matches_existing_metrics_without_per_day_live_scans(monkeypatch):
    u=user('support_pro');today=A._beijing_now().date();days=[(today-timedelta(days=i)).isoformat() for i in range(3)]
    for day in days:
        M.record_ai_usage(user_id=u['id'],day=day,total_tokens=65432,provider='deepseek',model=P.FLASH)
        M.record_site_activity(session_key='batch-'+day,user_id=u['id'],day=day,feature='search')
        M.record_site_activity(session_key='batch-'+day,user_id=None,day=day,feature='reader')

    # Compare the two queries at the same instant. A subscription that starts
    # in the current second can otherwise become active between the calls.
    as_of = M.utc_now().replace(microsecond=0) + timedelta(seconds=1)

    class DashboardClock(A.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return as_of.astimezone().replace(tzinfo=None)
            return as_of.astimezone(tz)

    monkeypatch.setattr(A, 'datetime', DashboardClock)
    monkeypatch.setattr(M, 'utc_now_text', lambda: as_of.isoformat(timespec='seconds'))
    with A.app.test_request_context('/'):
        A.g.current_user=None
        reference=[A._dashboard_metrics_for_day(day,for_history=True) for day in days]
    def unwanted(*a,**k):raise AssertionError('Range summary loaded per-day details')
    monkeypatch.setattr(M,'get_admin_dashboard_metrics',unwanted)
    monkeypatch.setattr(M,'list_reader_anomaly_visitors',unwanted)
    actual=D.history(days)
    keys=('today_online','new_users_today','active_members','paid_today_cents','searches_today','reader_views_today',
          'ai_requests_today','ai_tokens_today','high_token_user_count','payment_errors_today','journal_recent_sends')
    for row,old in zip(actual,reference):
        assert {k:row.get(k,0) or 0 for k in keys}=={k:old.get(k,0) or 0 for k in keys}
        fields=('user_id','total_tokens','request_count','max_request_tokens','effective_limit','alert_reason')
        assert [tuple(r[k] for k in fields) for r in row['high_token_users']]==[tuple(r[k] for k in fields) for r in old['high_token_users']]


@pytest.mark.parametrize('section',['summary','tokens','readers','history'])
def test_fragment_routes_and_authenticated_rendering(section):
    with A.app.test_client() as client:
        admin_login(client)
        response=client.get('/admin/overview/data?section='+section)
        assert response.status_code==200,response.get_data(as_text=True)[:1000]
        assert response.headers['Cache-Control']=='no-store'
        assert 'db;desc=' in response.headers['Server-Timing']


def test_user_pagination_has_same_snapshots_and_no_per_user_connections(monkeypatch):
    users=[user('support_plus_20'),user('support_pro')]
    expected={u['id']:M.get_membership_snapshot(u['id']) for u in users}
    def unwanted(*a,**k):raise AssertionError('Per-user snapshot query')
    monkeypatch.setattr(M,'_compute_membership_snapshot',unwanted)
    assert M.membership_snapshots(list(expected))==expected
    first=M.list_users(limit=1);second=M.list_users(limit=1,offset=1)
    assert first[0]['id']!=second[0]['id']
