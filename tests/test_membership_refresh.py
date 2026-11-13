"""New refresh acceptance. Run only through the guarded once runner."""
import io
import json
import threading
import time
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from membership_refresh_fixture import A, M, R, P, user, login, bulletin
from member_catalog import PLUS_CODE
import feature_access as F
import note_exports as N
import notes
from priority_queue import PrioritySemaphore
from agent_api_store import Store, APIError
import ai


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import urllib.request
    import smtplib
    def denied(*args, **kwargs):
        raise AssertionError('Unexpected external service call')
    monkeypatch.setattr(urllib.request, 'urlopen', denied)
    monkeypatch.setattr(smtplib, 'SMTP', denied)
    A.app.config.update(TESTING=True,SESSION_COOKIE_SECURE=False)


def test_new_plus_sale_wallet_and_weekly_limit():
    u=user(PLUS_CODE)
    assert M.get_plan(PLUS_CODE)['price_cents']==2000
    wallet=M.get_ai_entitlements(u['id'])['wallets'][0]
    assert (wallet['budget_micros'],wallet['released_micros'])==(18_000_000,9_000_000)
    assert M.get_user_weekly_token_entitlement(u['id'])['weekly_limit']==1_000_000
    assert M.get_deepseek_web_quota(u['id'])['limit']==40
    for code in ('support_basic','support_plus'):
        with pytest.raises(ValueError): M.create_pending_order(user_id=u['id'],plan_code=code)


@pytest.mark.parametrize('code,budget,weekly',[('support_basic',13_500_000,770_000),('support_plus',22_500_000,None)])
def test_original_wallet_and_dates_survive_new_catalog(code,budget,weekly):
    u=user(code)
    before=M.get_membership_snapshot(u['id'])
    initial=M.get_ai_entitlements(u['id'])['wallets'][0]
    M.init_membership_db()
    after=M.get_ai_entitlements(u['id'])['wallets'][0]
    assert after['budget_micros']==budget
    for key in ('budget_micros','spent_micros','reserved_micros','released_micros','starts_at','expires_at'):
        assert initial[key]==after[key]
    assert M.get_membership_snapshot(u['id']).expires_at==before.expires_at
    assert M.get_user_weekly_token_entitlement(u['id'])['weekly_limit']==weekly


@pytest.mark.parametrize('code,api',[('support_basic',False),('support_plus',False),(PLUS_CODE,False),('support_pro',True),('support_max',True),('monthly',True),(None,False)])
def test_authoritative_feature_matrix(code,api):
    u=user(code)
    policy=F.load_access_policy(include_saved=False)
    policy['users'][u['email']]={'agent_api':True,'associative':True,'concept_map':True}
    assert F.feature_allowed_by_policy(policy,'agent_api',u)==api
    for feature in ('associative','concept_map'):
        assert F.feature_allowed_by_policy(policy,feature,u)==bool(code)
    assert F.feature_allowed_by_policy(policy,'search_chat',u)


def test_saved_guest_override_cannot_unlock_paid_features():
    policy=F.load_access_policy(include_saved=False)
    policy['audience']['guest'].update(associative=True,concept_map=True,agent_api=True)
    assert not any(F.feature_allowed_by_policy(policy,key,None) for key in ('associative','concept_map','agent_api'))


def test_retired_basic_renews_into_new_plus_without_upgrade():
    u=user('support_basic'); before=M.get_membership_snapshot(u['id'])
    order=M.create_pending_order(user_id=u['id'],plan_code=PLUS_CODE)
    assert order['amount_cents']==2000 and order['purchase_action']=='renew'
    result=M.mark_order_paid(order_no=order['order_no'],provider='fixture')
    assert result['subscription']['starts_at'] >= before.expires_at


def test_multimonth_discount_preserves_monthly_18(monkeypatch):
    import membership_purchase as purchase
    monkeypatch.setattr(purchase,'enabled',lambda:True)
    u=user(); order=M.create_pending_order(user_id=u['id'],plan_code=PLUS_CODE,purchase_months=3)
    M.mark_order_paid(order_no=order['order_no'],provider='fixture')
    with M._connect() as c:
        rows=c.execute('SELECT budget_micros FROM ai_wallets WHERE user_id=?',(u['id'],)).fetchall()
    assert len(rows)==3 and all(r[0]==18_000_000 for r in rows)


def test_plus_api_key_immediate_denial_and_pro_self_service():
    store=A.app.extensions['agent_api']['store']
    low=user('support_plus'); high=user('support_pro')
    with A.app.test_client() as c:
        for u,status in [(low,403),(high,200)]:
            key=store.create_key(u['id'],'fixture')
            response=c.get('/api/v1/limits',headers={'Authorization':'Bearer '+key})
            assert response.status_code==status,response.get_data(as_text=True)
            if status==200:
                assert response.json['scheduling']['queue'] is True


def wait_until(fn):
    end=time.monotonic()+3
    while not fn():
        if time.monotonic()>end: raise AssertionError('waiter did not register')
        time.sleep(.01)


def test_priority_fifo_cancellation_and_running_nonpreemption():
    queue=PrioritySemaphore(1); assert queue.acquire()
    order=[]; threads=[]
    def work(label,priority):
        assert queue.acquire(timeout=3,priority=priority)
        order.append(label); queue.release()
    for n,(label,priority) in enumerate([('ordinary',0),('pro',1),('max-first',2),('max-second',2)]):
        t=threading.Thread(target=work,args=(label,priority));t.start();threads.append(t)
        wait_until(lambda:len(queue._tickets)==n+1)
    assert order==[]
    queue.release()
    for t in threads:t.join(4)
    assert order==['max-first','max-second','pro','ordinary']
    assert not queue.acquire(timeout=1,cancelled=lambda:True)
    assert queue.acquire(blocking=False);queue.release()


def test_shared_api_priority_queue_preserves_free_limits(tmp_path,monkeypatch):
    monkeypatch.setenv('MARX_AGENT_API_CONCURRENCY','1')
    store=Store(tmp_path);order=[];threads=[]
    def work(uid,p):
        with Store(tmp_path).admission(uid,priority=p): order.append(uid)
    with store._admission_nowait(90):
        for n,(uid,p) in enumerate([(91,0),(92,2)]):
            t=threading.Thread(target=work,args=(uid,p));t.start();threads.append(t)
            def ready():
                with store.connect() as c:return c.execute('SELECT COUNT(*) FROM api_waiters').fetchone()[0]==n+1
            wait_until(ready)
        with store.admission(90,lane='light',control=True):pass
        assert order==[]
    for t in threads:t.join(4)
    assert order==[92,91]
    with store.connect() as c:assert c.execute('SELECT COUNT(*) FROM api_waiters').fetchone()[0]==0
    assert store.rate(91)['remaining']==store.rate(92)['remaining']


def test_api_wait_cancel_and_timeout_do_not_charge(tmp_path,monkeypatch):
    monkeypatch.setenv('MARX_AGENT_API_CONCURRENCY','1');monkeypatch.setenv('MARX_AGENT_API_QUEUE_WAIT_SECONDS','1')
    store=Store(tmp_path)
    with pytest.raises(APIError,match='取消'):
        with store.admission(1,cancelled=lambda:True):pass
    with store._admission_nowait(2):
        with pytest.raises(APIError,match='超时'):
            with store.admission(3):pass
    with store.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM api_calls').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM api_waiters').fetchone()[0]==0


def test_all_note_formats_include_2301_and_owner_boundary():
    u=user(PLUS_CODE);other=user(PLUS_CODE)
    now=M.utc_now_text()
    with notes._connect() as c:
        c.executemany('INSERT INTO notes(user_id,reader,book_key,book_title,page,quote,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
            [(u['id'],'viewer','sample.pdf','笔记书籍',1,'摘录',f'笔记-{n}',now,now) for n in range(2301)])
        c.commit()
    rows=N.collect(u['id'],{'scope':'all'});assert len(rows)==2301
    assert not N.collect(other['id'],{'scope':'all'})
    with pytest.raises(LookupError):N.collect(other['id'],{'scope':'single','note_id':rows[0]['id']})
    for fmt in ('md','json','docx'):
        data,mime=N.render(rows,fmt,'https://example.test')
        if fmt=='json':assert len(json.loads(data)['notes'])==2301 and 'user_id' not in json.loads(data)['notes'][0]
        elif fmt=='md':assert data.decode().count('## 笔记书籍')==2301
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                assert z.read('word/document.xml').decode().count('返回原文')==2301
                assert 'https://example.test/viewer?' in z.read('word/_rels/document.xml.rels').decode()


@pytest.mark.parametrize('url',['//evil.test/x','/\\evil.test/x','/%2f%2fevil.test','/admin','javascript:alert(1)'])
def test_note_export_rejects_untrusted_source_links(url):
    assert N.source_url({'source_url':url})==''


def test_reader_source_reconstruction():
    assert N.source_url(dict(reader='mylib',book_key='mylib:12',page=3))=='/mylib/12?page=3'
    assert N.source_url(dict(reader='wenku',book_key='wenku:capital|1',page=22,doc_path='chapter.htm')).startswith('/wenku/capital?vol=1&doc=chapter.htm&pg=22')
    assert N.source_url(dict(reader='liushi',book_key='wenku:wenji-zh|2',page=8)).startswith('/liushi/wenji-zh?')


def test_preferences_selection_optout_and_empty_mail():
    u=user(PLUS_CODE);snapshot=bulletin()
    assert P.get_preferences(u['id'])['mode']=='all'
    selected=P.catalog()[0]['id']
    value=P.save_preferences(u['id'],u['email'],dict(email_enabled=True,mode='selected',journal_ids=[selected]))
    filtered=P.filter_snapshot(snapshot,value)
    assert filtered['articles'] and all(P.journal_id(a['journal'])==selected for a in filtered['articles'])
    assert len(snapshot['articles'])==16
    empty=P.save_preferences(u['id'],u['email'],dict(email_enabled=True,mode='selected',journal_ids=[]))
    assert not empty['email_enabled'] and not P.filter_snapshot(snapshot,empty)['articles']
    import research_delivery as delivery
    rec=delivery.with_unsubscribe({'user_id':u['id'],'email':u['email']})
    assert delivery.unsubscribe(rec['research_unsubscribe_token'])
    assert not P.get_preferences(u['id'])['email_enabled']


def test_sample_fixed_twelve_and_direct_url_authorization(monkeypatch,tmp_path):
    monkeypatch.setattr(R,'DB_PATH',tmp_path/'research.sqlite');R.init_db();snapshot=bulletin()
    assert P.prepare_sample();first=P.sample();assert len(first['articles'])==12
    assert sum(a['origin']=='domestic' for a in first['articles'])==6
    assert not P.prepare_sample() and P.sample()==first
    with A.app.test_client() as c:
        response=c.get('/research-updates/sample');assert response.status_code==200,response.get_data(as_text=True)
        assert '公开样刊' in response.get_data(as_text=True)
        entry=first['articles'][0]['entry_id']
        assert c.get(f'/research-updates/sample/articles/{entry}').status_code==200
        assert c.get(f'/research-updates/sample/articles/{entry}/citation.ris').status_code==200
        omitted=next(a['entry_id'] for a in snapshot['articles'] if a['entry_id'] not in {b['entry_id'] for b in first['articles']})
        assert c.get(f'/research-updates/sample/articles/{omitted}').status_code==404
        assert c.get('/research-updates/70001').status_code==403


def test_stale_mimo_environment_cannot_reenable_model(monkeypatch):
    monkeypatch.setenv('MIMO_MODEL_ACCESS_ENABLED','1');monkeypatch.setenv('MIMO_API_KEY','fixture-secret')
    monkeypatch.setenv('APP_AI_PROVIDER','mimo');monkeypatch.setenv('APP_AI_MODEL','mimo-v2.5')
    monkeypatch.setenv('APP_AI_BASE_URL','https://api.xiaomimimo.com/v1')
    config=ai.load_ai_config()
    assert not config.mimo_enabled and config.provider=='deepseek' and 'xiaomimimo' not in config.base_url
    with pytest.raises(ai.AIServiceError,match='停用'):
        ai.ZAIClient(config).chat_complete([{'role':'user','content':'fixture'}],10,provider='mimo',model='mimo-v2.5')
    assert all(not k.startswith('mimo') for k in M._effective_model_policy({'models':{'mimo-v2.5-pro':['on']}})['models'])
    assert A._WENKU_TRANSLATE_PROVIDER=='deepseek'


def test_account_and_preference_endpoint_actual_member_permissions():
    u=user(PLUS_CODE)
    with A.app.test_client() as c:
        headers=login(c,u)
        response=c.get('/account');assert response.status_code==200,response.get_data(as_text=True)
        assert '1000000' in response.get_data(as_text=True) and 'MiMo' not in response.get_data(as_text=True)
        result=c.put('/api/account/research-subscriptions',json=dict(email_enabled=True,mode='all',journal_ids=[]),headers=headers)
        assert result.status_code==200,result.get_data(as_text=True)
        assert c.put('/api/account/research-subscriptions',json={}).status_code==403
        pricing=c.get('/pricing').get_data(as_text=True)
        assert '¥20.00' in pricing and 'MiMo' not in pricing


@pytest.mark.parametrize('code,price,budget',[('support_basic',1500,13_500_000),('support_plus',2500,22_500_000)])
def test_pre_cutover_pending_order_settles_original_snapshot(code,price,budget):
    import membership_purchase as purchase
    u=user(); now=M.utc_now_text(); order_no='old-'+str(u['id'])
    snapshot=purchase.quote(M.get_plan(code),1)
    with M._connect() as c:
        c.execute("INSERT INTO orders(order_no,user_id,plan_code,status,amount_cents,currency,created_at,expires_at,entitlement_snapshot_json) VALUES(?,?,?,'pending',?,'CNY',?,?,?)",
            (order_no,u['id'],code,price,now,(M.utc_now()+timedelta(hours=1)).isoformat(),json.dumps(snapshot)))
        c.commit()
    settled=M.mark_order_paid(order_no=order_no,provider='fixture')
    assert settled['order']['amount_cents']==price
    assert M.get_ai_entitlements(u['id'])['wallets'][0]['budget_micros']==budget
    assert not F.feature_effective_for_user('agent_api',u)


def test_monday_beijing_reset_preserves_whole_cycle_wallet(monkeypatch):
    from datetime import datetime, timezone
    u=user(PLUS_CODE)
    wallet=M.get_ai_entitlements(u['id'])['wallets'][0]
    clock=[datetime(2026,11,15,15,59,59,tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return clock[0].astimezone(tz)
    monkeypatch.setattr(A,'datetime',Clock)
    before=A._research_quota_week_window()
    clock[0]+=timedelta(seconds=1)
    after=A._research_quota_week_window()
    assert before['start_day']=='2026-11-09'
    assert after['start_day']=='2026-11-16' and after['reset_at']=='2026-11-23T00:00:00+08:00'
    current=M.get_ai_entitlements(u['id'])['wallets'][0]
    assert (current['budget_micros'],current['released_micros'],current['spent_micros'])==(wallet['budget_micros'],wallet['released_micros'],wallet['spent_micros'])


def test_original_price_upgrade_difference():
    u=user('support_basic')
    order=M.create_pending_order(user_id=u['id'],plan_code='support_pro')
    assert order['purchase_action']=='upgrade'
    assert 2999<=order['amount_cents']<=3000
    M.mark_order_paid(order_no=order['order_no'],provider='fixture')
    assert M.get_membership_snapshot(u['id']).plan_code=='support_pro'


def test_mail_rechecks_selection_expiry_optout_and_deduplicates(monkeypatch,tmp_path):
    import research_delivery as delivery
    import journal_alerts as ja
    monkeypatch.setattr(R,'DB_PATH',tmp_path/'mail.sqlite')
    R.init_db(); snapshot=bulletin()
    users=[user(PLUS_CODE) for _ in range(4)]
    recipients=[{'user_id':u['id'],'email':u['email']} for u in users]
    monkeypatch.setattr(ja,'resolve_recipients',lambda mode:(list(recipients),False))
    now=R.now_text()
    with R.connect(True) as c:
        c.execute("INSERT INTO research_mail_schedules(issue_id,snapshot,scheduled_at,status,actor,updated_at,audience) VALUES(?,?,?,'scheduled','fixture',?,'members')",
            (snapshot['id'],R.dumps(snapshot),now,now))
    selected=P.journal_id(snapshot['articles'][0]['journal'])
    observed=[]
    def sender(smtp,email,subject,plain,rich):
        observed.append((email,plain))
        if len(observed)==1:
            P.save_preferences(users[1]['id'],users[1]['email'],dict(email_enabled=True,mode='selected',journal_ids=[selected]))
            P.save_preferences(users[2]['id'],users[2]['email'],dict(email_enabled=False,mode='all',journal_ids=[]))
            recipients[:]=[r for r in recipients if r['user_id']!=users[3]['id']]
    result=delivery.deliver('https://example.test',sender=sender,smtp=object())
    assert result==dict(sent=2,failed=0,uncertain=0,skipped=2)
    expected=P.filter_snapshot(snapshot,P.get_preferences(users[1]['id']))
    assert all(a['title'] in observed[1][1] for a in expected['articles'])
    assert not any(a['title'] in observed[1][1] for a in snapshot['articles'] if a['journal']!=snapshot['articles'][0]['journal'])
    delivery.deliver('https://example.test',sender=sender,smtp=object())
    assert len(observed)==2
    with R.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM research_mail_events WHERE action='selection'").fetchone()[0]==2
