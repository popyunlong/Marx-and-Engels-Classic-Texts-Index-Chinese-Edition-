"""Previously uncovered transport, revocation, background and locator scenarios."""
import asyncio
import json
import threading
import time
from contextlib import nullcontext

import pytest
from membership_refresh_fixture import A, M, user, login
import ai
from priority_queue import PrioritySemaphore
from test_membership_refresh import offline, wait_until


def queued_sources(store, u, request_id):
    key=store.create_key(u['id'],'edge')
    headers={'Authorization':'Bearer '+key,'X-Query-Request-ID':request_id}
    result=[]
    def run():
        with A.app.test_client() as c: result.append(c.get('/api/v1/sources',headers=headers))
    t=threading.Thread(target=run);t.start()
    def queued():
        with store.connect() as c:return bool(c.execute('SELECT 1 FROM api_waiters WHERE user_id=?',(u['id'],)).fetchone())
    wait_until(queued)
    return t,result,headers


def test_api_downgrade_while_waiting_rechecks_persistent_identity(monkeypatch):
    monkeypatch.setenv('MARX_AGENT_API_LIGHT_CONCURRENCY','1')
    store=A.app.extensions['agent_api']['store'];u=user('support_pro')
    with store._admission_nowait(99001,lane='light'):
        t,result,headers=queued_sources(store,u,'a'*32)
        with M._connect() as c:
            c.execute("UPDATE subscriptions SET status='cancelled' WHERE user_id=?",(u['id'],));c.commit()
    t.join(4)
    assert result[0].status_code==403
    with store.connect() as c:assert not c.execute('SELECT 1 FROM api_calls WHERE user_id=?',(u['id'],)).fetchone()


def test_rest_cancel_control_survives_saturated_data_lane(monkeypatch):
    monkeypatch.setenv('MARX_AGENT_API_LIGHT_CONCURRENCY','1')
    store=A.app.extensions['agent_api']['store'];u=user('support_max')
    with store._admission_nowait(99002,lane='light'):
        t,result,headers=queued_sources(store,u,'b'*32)
        with A.app.test_client() as c:
            limits=c.get('/api/v1/limits',headers=headers)
            assert limits.status_code==200 and limits.json['scheduling']['priority']=='highest'
            assert c.post('/api/v1/cancel',headers=headers,json={'request_id':'b'*32}).status_code==200
        t.join(4)
    assert result[0].status_code==408
    with store.connect() as c:assert not c.execute('SELECT 1 FROM api_calls WHERE user_id=?',(u['id'],)).fetchone()


def test_rest_disconnection_before_admission_does_not_consume():
    store=A.app.extensions['agent_api']['store'];u=user('support_pro');key=store.create_key(u['id'],'disconnect')
    with A.app.test_client() as c:
        result=c.get('/api/v1/sources',headers={'Authorization':'Bearer '+key},environ_overrides={'waitress.client_disconnected':lambda:True})
    assert result.status_code==408
    with store.connect() as c:assert not c.execute('SELECT 1 FROM api_calls WHERE user_id=?',(u['id'],)).fetchone()


def test_mcp_cancel_forwards_owner_identity_to_shared_control():
    import os, subprocess, sys
    from pathlib import Path
    runtime=os.environ.get('MEMBERSHIP_MCP_PYTHON') or sys.executable
    env={**os.environ, 'PYTHONPATH':str(Path(__file__).resolve().parents[1]), 'PYTHONDONTWRITEBYTECODE':'1'}
    result=subprocess.run([runtime,'scripts/membership_mcp_cancel_acceptance.py'],env=env,
        capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.parametrize('mode',['quick','research','comprehensive'])
def test_actual_ai_admission_uses_server_tier_in_each_mode(monkeypatch,mode):
    q=PrioritySemaphore(1)
    monkeypatch.setattr(ai,'_AI_HTTP_INTERACTIVE_SEMAPHORE',q)
    monkeypatch.setattr(ai,'_AI_HTTP_RESEARCH_SEMAPHORE',q)
    if mode=='comprehensive':monkeypatch.setattr(A,'_RESEARCH_PIPELINE_SEMAPHORE',q)
    assert q.acquire();order=[];threads=[]
    def run(label,u):
        with A.app.test_request_context('/api/ai?priority=999'):
            A.g.current_user=u
            if mode=='comprehensive':
                assert A._acquire_research_pipeline_slot(threading.Event())
                order.append(label);q.release()
            else:
                with ai.research_ai_http_context() if mode=='research' else nullcontext():
                    with ai._ai_http_slot():order.append(label)
    for n,(label,code) in enumerate([('regular','support_plus_20'),('pro','support_pro'),('max','support_max')]):
        t=threading.Thread(target=run,args=(label,user(code)));t.start();threads.append(t)
        wait_until(lambda:len(q._tickets)==n+1)
    q.release()
    for t in threads:t.join(4)
    assert order==['max','pro','regular']


def test_old_paragraph_locator_and_filtered_export_endpoint():
    import notes, note_exports
    from urllib.parse import quote
    u=user('support_plus_20')
    note=notes.create_note(user_id=u['id'],reader='liushi',book_key='wenku:wenji-zh|1',doc_path='ch1.html',
        anchor_text='劳动与生产',body='段落定位验证',book_title='文集',page=2)
    assert note_exports.source_url(note).endswith('#:~:text='+quote('劳动与生产',safe=''))
    with A.app.test_client() as c:
        headers=login(c,u)
        result=c.post('/api/notes/export',headers=headers,json={'scope':'filtered','q':'段落定位验证','format':'json'})
        assert result.status_code==200 and len(result.json['notes'])==1


def test_online_translation_and_admin_cannot_route_to_mimo(monkeypatch):
    import journal_fulltext as full
    from werkzeug.exceptions import Forbidden
    captured=[]
    monkeypatch.setattr(ai.ZAIClient,'chat_complete',lambda self,*args,**kw:captured.append(kw) or '译文')
    monkeypatch.setattr(full,'_record_model_usage',lambda *args,**kw:None)
    assert full._mimo_chat([{'role':'user','content':'fixture'}],max_tokens=10,feature='journal_translate')=='译文'
    assert captured[0]['provider']=='deepseek' and captured[0]['model']=='deepseek-v4-flash'
    assert captured[0]['disable_thinking'] is True
    with A.app.test_request_context('/admin'):
        A.g.current_user={'id':0,'role':'admin'}
        with pytest.raises(Forbidden):A._resolve_ai_selection_or_abort({'model':'mimo-v2.5'},feature='search-chat')
