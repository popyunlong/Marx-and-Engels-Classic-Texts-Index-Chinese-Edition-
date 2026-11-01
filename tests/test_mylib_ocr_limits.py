"""New bounded-OCR scenarios. No model execution, network, or production data."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mylib_ocr.api import create_app
from mylib_ocr.layout import LAYOUT_SCHEMA_VERSION, layout_blocks
from mylib_ocr.page import render_size
from mylib_ocr.queue import Conflict, Paused, Queue
from mylib_ocr import worker
from personal_library_processing import Processing

ROOT=Path(__file__).resolve().parents[1]
SHA='a'*64


def admit(queue,sid=1,run='1'*32,sha=SHA,total=2):
    queue.grant(1,sid,'Explicit test fixture admission')
    return queue.enqueue(1,sid,run,sha,total)


def test_serial_claim_and_idempotent_admission(tmp_path):
    queue=Queue(tmp_path)
    with pytest.raises(Paused):queue.enqueue(1,1,'1'*32,SHA,2)
    first=admit(queue)
    assert queue.enqueue(1,1,'1'*32,SHA,2)['run_id']==first['run_id']
    queue.grant(1,1,'A distinct later authorization')
    with pytest.raises(Conflict):queue.enqueue(1,1,'2'*32,SHA,2)
    admit(queue,sid=2,run='3'*32)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda _:queue.claim(),range(2)))
    assert sum(job is not None for job in claims)==1
    assert next(job for job in claims if job)['state']=='running'


def test_restart_is_terminal_and_does_not_consume_another_admission(tmp_path):
    queue=Queue(tmp_path);admit(queue);queue.claim();queue.set_dispatch(True)
    recreated=Queue(tmp_path)
    assert recreated.recover_running()==1
    assert not recreated.dispatch_enabled()
    assert recreated.claim() is None
    assert recreated.status(1,1,'1'*32)['state']=='interrupted'
    with pytest.raises(Paused):recreated.enqueue(1,1,'2'*32,SHA,2)


def test_checkpoint_mismatch_preserves_original(tmp_path):
    queue=Queue(tmp_path);admit(queue);job=queue.claim()
    queue.save_page(job,{'page':1,'text':'Original page','layout_schema':3})
    queue.terminal(job['run_id'],'interrupted','fixture')
    queue.grant(1,1,'New authorization')
    with pytest.raises(Conflict):queue.enqueue(1,1,'2'*32,'b'*64,2)
    assert queue.pages(job)[0]['text']=='Original page'


def test_completed_text_requires_owner_and_matching_run(tmp_path):
    queue=Queue(tmp_path);admit(queue)
    class Storage:
        def result_key(self,job):return job['run_id']
        def get(self,key):return b'{"page":1,"text":"private"}'
    app=create_app(tmp_path,token='fixture',storage=Storage())
    client=app.test_client();headers={'Authorization':'Bearer fixture'}
    query=f'uid=1&sid=1&run_id={"1"*32}'
    assert client.get('/ocr/status?'+query).status_code==401
    assert client.get('/ocr/status?'+query.replace('uid=1','uid=2'),headers=headers).status_code==404
    assert client.get('/ocr/text?'+query,headers=headers).status_code==409
    job=queue.claim()
    with pytest.raises(Conflict):queue.terminal(job['run_id'],'done')
    queue.save_page(job,{'page':1,'text':'private'})
    queue.save_page(job,{'page':2,'text':''})
    queue.terminal(job['run_id'],'done')
    assert client.get('/ocr/text?'+query,headers=headers).status_code==200
    assert client.post('/ocr/start?'+query+'&source_sha256='+SHA+'&max_pages=2',headers=headers).json['state']=='done'
    assert client.post('/ocr/start?'+query+'&force=1',headers=headers).status_code==409


@pytest.mark.parametrize('width,height',[(850,1200),(1391.81,2108.28),(100,10000),(10000,100),(1,1),(1,1_000_000)])
def test_pixel_budget_and_normalized_layout_coordinates(width,height):
    if (width,height)==(1,1_000_000):
        with pytest.raises(ValueError,match='unreadable'):
            render_size(width,height)
        return
    scale=render_size(width,height)
    assert 0<scale<=4
    assert math.ceil(width*scale)<=2200
    assert math.ceil(width*scale)*math.ceil(height*scale)<=8_000_000
    if width==1391.81:assert scale<3  # The former mandatory 3x enlargement is gone.
    line=[[[width*.1,height*.2],[width*.6,height*.2],[width*.6,height*.3],[width*.1,height*.3]],'A line',.99]
    scaled=[[[x*scale,y*scale] for x,y in line[0]],line[1],line[2]]
    assert layout_blocks([line],(round(height),round(width),3))[0]['text']==layout_blocks([scaled],(round(height*scale),round(width*scale),3))[0]['text']
    assert layout_blocks([scaled],(round(height*scale),round(width*scale),3))[0]['bbox']==pytest.approx([.1,.2,.6,.3],abs=.005)


def test_invalid_page_geometry():
    for size in [(0,1),(-1,1),(float('inf'),1),(float('nan'),1)]:
        with pytest.raises(ValueError):render_size(*size)


@pytest.mark.parametrize('maximum,high,swap,allowed',[(4*worker.GIB,3*worker.GIB,0,True),('max',3*worker.GIB,0,False),(8*worker.GIB,3*worker.GIB,0,False),(4*worker.GIB,3*worker.GIB,1,False)])
def test_worker_refuses_missing_or_unbounded_cgroup(tmp_path,maximum,high,swap,allowed):
    for name,value in [('memory.max',maximum),('memory.high',high),('memory.swap.max',swap)]:
        (tmp_path/name).write_text(str(value))
    if allowed:worker.verify_budget(tmp_path)
    else:
        with pytest.raises((ValueError,RuntimeError)):worker.verify_budget(tmp_path)


def test_resume_imports_legacy_and_persists_empty_page_once(tmp_path):
    import fitz
    pdf=tmp_path/'fixture.pdf'
    with fitz.open() as doc:
        for _ in range(3):doc.new_page()
        doc.save(pdf)
    sha=hashlib.sha256(pdf.read_bytes()).hexdigest()
    queue=Queue(tmp_path/'queue');admit(queue,sha=sha,total=3);job=queue.claim()
    legacy={'page':1,'text':'legacy','layout_schema':LAYOUT_SCHEMA_VERSION,'layout_blocks':[]}
    class Storage:
        def source(self,job):return pdf
        def legacy_pages(self,job):return [legacy]
        def publish_result(self,job,payload):self.payload=payload
    storage=Storage();calls=[]
    def page_runner(pdf,page,directory,**kwargs):
        calls.append(page)
        if len(calls)==1:raise RuntimeError('Fixture process exit')
        return {'page':page,'text':'body' if page==2 else '', 'layout_schema':3,'layout_blocks':[]}
    queue.set_dispatch(True)
    worker.process_job(queue,storage,job,page_runner=page_runner,memory=lambda:8*worker.GIB)
    assert queue.status(1,1,job['run_id'])['state']=='interrupted'
    assert not queue.dispatch_enabled()
    assert [p['page'] for p in queue.pages(job)]==[1]
    admit(queue,run='2'*32,sha=sha,total=3);job2=queue.claim();queue.set_dispatch(True)
    worker.process_job(queue,storage,job2,page_runner=page_runner,memory=lambda:8*worker.GIB)
    result=queue.status(1,1,job2['run_id'])
    assert (result['state'],result['processed_pages'],result['text_pages'])==('done',3,2)
    assert calls==[2,2,3]  # Second attempt exists only after the separate fixture admission.
    assert json.loads(storage.payload.splitlines()[0])['text']=='legacy'
    assert Queue(tmp_path/'queue').pages(job2)[-1]['text']==''
    assert queue.claim() is None


def test_page_process_failure_never_retries_and_omits_credentials(tmp_path,monkeypatch):
    observed=[]
    class Exited:
        returncode=-9
        def poll(self):return self.returncode
    def popen(args,**kwargs):observed.append(kwargs['env']);return Exited()
    monkeypatch.setattr(worker.subprocess,'Popen',popen)
    monkeypatch.setenv('MINIO_ROOT_PASSWORD','do-not-pass')
    monkeypatch.setenv('MYLIB_WORKER_TOKEN','do-not-pass')
    with pytest.raises(RuntimeError,match='no automatic retry'):
        worker.run_page(tmp_path/'source.pdf',1,tmp_path,memory=lambda:8*worker.GIB)
    assert len(observed)==1
    assert not any('TOKEN' in k or k.startswith('MINIO_') for k in observed[0])


@pytest.fixture
def library(tmp_path,monkeypatch):
    membership=types.ModuleType('membership');membership.utc_now_text=lambda:'2026-11-01T00:00:00+00:00'
    runtime=types.ModuleType('runtime_env');runtime.APPDATA_DIR=tmp_path
    runtime.secure_db_file=lambda path:None
    monkeypatch.setitem(sys.modules,'membership',membership)
    monkeypatch.setitem(sys.modules,'runtime_env',runtime)
    spec=importlib.util.spec_from_file_location('mylib_limits_fixture_library',ROOT/'personal_library.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.init_personal_library_db()
    with module._connect() as db:
        db.execute("INSERT INTO personal_library_submissions(id,user_id,title,sha256,source_kind,ocr_consent,license_attested,page_count,status,created_at) VALUES(1,1,'Fixture',?,'scanned',1,1,2,'failed','2026-11-01')",(SHA,))
    monkeypatch.setattr(module,'ocr_quota_allowance',lambda uid,total:(total,''))
    return module


def test_stale_run_cannot_write_and_current_finalizes_once(library):
    writes=[];accepts=[]
    processor=Processing(library,None,None,lambda *a,**k:writes.append(a) or 1,lambda *a:accepts.append(a))
    first=library.begin_processing_run(1)
    with pytest.raises(ValueError):library.begin_processing_run(1)
    library.fail_processing_run(1,first,'Separate fixture authorization required')
    second=library.begin_processing_run(1)
    processor.commit(library.get_submission(1),first,[{'text':'old'}],{},ocr=True)
    assert not writes and not accepts
    assert not library.processing_progress(1,first,99,99)
    processor.commit(library.get_submission(1),second,[{'text':'new'}],{},ocr=True)
    processor.commit(library.get_submission(1),second,[{'text':'new'}],{},ocr=True)
    assert len(writes)==len(accepts)==1
    assert library.get_submission(1)['processing_run_state']=='completed'
    library.record_ocr_usage(1,1,1,run_id='later-run')
    assert library.ocr_pages_used(1)==1


def test_incomplete_node_does_not_build_or_accept(library):
    writes=[];accepts=[];requests=[]
    class Store:
        class StoreError(Exception):pass
        @staticmethod
        def ocr_health():return {'protocol_version':2,'dispatch_enabled':True}
        @staticmethod
        def ocr_start(*args,**kwargs):requests.append(kwargs)
        @staticmethod
        def ocr_status(*args,**kwargs):return {'uid':1,'sid':1,'run_id':run_id,'source_sha256':SHA,'total':2,'state':'done','processed_pages':1,'checkpoint_pages':1}
        @staticmethod
        def parse(*args):raise AssertionError('Resume must not parse/overwrite legacy text')
        @staticmethod
        def fetch_text(*args,**kwargs):raise AssertionError('Partial text must not be fetched')
    processor=Processing(library,Store,None,lambda *a,**k:writes.append(a),lambda *a:accepts.append(a))
    run_id=library.begin_processing_run(1)
    processor.run(1,run_id,resume=True)
    assert not writes and not accepts
    assert library.get_submission(1)['status']=='failed'
    assert len(requests)==1 and 'force' not in requests[0]


def test_legacy_or_paused_ocr_service_cannot_receive_start(library):
    class Store:
        class StoreError(Exception):pass
        @staticmethod
        def ocr_health():return {'protocol_version':1,'dispatch_enabled':True}
        @staticmethod
        def ocr_start(*args,**kwargs):raise AssertionError('Legacy OCR must not be started')
    processor=Processing(library,Store,None,lambda *a:None,lambda *a:None)
    run_id=library.begin_processing_run(1)
    processor.run(1,run_id,resume=True)
    assert library.get_submission(1)['status']=='failed'
    assert library.get_submission(1)['acceptance_checked_at']==''


def test_startup_reconciliation_does_not_restart_or_reaccept(library,monkeypatch):
    with library._connect() as db:db.execute("UPDATE personal_library_submissions SET status='parsing' WHERE id=1")
    def forbidden(*a,**k):raise AssertionError('No startup processing permitted')
    processor=Processing(library,None,None,forbidden,forbidden)
    monkeypatch.setattr(processor,'start',forbidden)
    processor.reconcile()
    assert library.get_submission(1)['status']=='failed'
    assert library.get_submission(1)['acceptance_checked_at']==''


def test_deleted_book_cannot_be_resurrected_by_a_worker(library):
    run_id=library.begin_processing_run(1)
    assert library.mark_deleted(1,user_id=1)
    assert not library.processing_progress(1,run_id,2,2)
    assert not library.claim_finalization(1,run_id)
    with pytest.raises(ValueError):library.begin_processing_run(1)


def load_transaction():
    spec=importlib.util.spec_from_file_location('mylib_limits_transaction',ROOT/'deploy/mylib_ocr_transaction.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_proxy_route_and_closed_rollback_preserve_storage_acl():
    transaction=load_transaction()
    original='server {\n    allow 38.76.174.234;\n    allow 127.0.0.1;\n    deny all;\n    location /node/ {\n        proxy_pass http://127.0.0.1:8800/;\n    }\n}\n'
    active=transaction.route_config(original)
    paused=transaction.route_config(active,paused=True)
    assert active.count('location ^~ /node/ocr/')==1
    assert 'location ^~ /node/ocr/ { return 503; }' in paused
    for preserved in ['allow 38.76.174.234;','allow 127.0.0.1;','deny all;','proxy_pass http://127.0.0.1:8800/;']:
        assert preserved in paused
    with pytest.raises(ValueError):transaction.route_config(original.replace('deny all;',''))


@pytest.fixture(scope='module')
def component_archive(tmp_path_factory):
    spec=importlib.util.spec_from_file_location('mylib_limits_builder',ROOT/'scripts/build_mylib_ocr_release.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    tmp_path=tmp_path_factory.mktemp('ocr-component')
    repository=tmp_path/'fixture-repo';repository.mkdir()
    for name in ['mylib_ocr/api.py','mylib_ocr/worker.py','deploy/mylib_ocr_transaction.py','deploy/mylib-ocr-api.service','deploy/mylib-ocr-worker.service']:
        path=repository/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# fixture\n')
    (repository/'mylib_ocr/runtime.json').write_text('{}')
    hooks=repository/'empty-hooks';hooks.mkdir()
    def git(*args):return subprocess.check_output(['git',*args],cwd=repository,text=True,stderr=subprocess.STDOUT).strip()
    git('init');git('add','.')
    git('-c','user.name=OCR fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','-c','core.hooksPath='+str(hooks),'commit','-m','Fixture source')
    sha=git('rev-parse','HEAD');archive=tmp_path/'fixture.tar.gz'
    builder.build(repository,sha,'fixture-release','absent',archive)
    return archive,sha


def test_component_hashes_and_archive_tamper_rejection(tmp_path,component_archive):
    archive,sha=component_archive
    transaction=load_transaction();target=tmp_path/'fixture-release'
    transaction.unpack(archive,target)
    assert transaction.verify(target)['git_sha']==sha
    changed=target/'mylib_ocr/api.py';changed.chmod(0o644);changed.write_text('# tampered\n')
    with pytest.raises(ValueError,match='hash mismatch'):transaction.verify(target)


@pytest.mark.parametrize('failure',[False,True])
def test_promotion_and_closed_rollback_never_touch_storage(tmp_path,component_archive,monkeypatch,failure):
    transaction=load_transaction();archive,_=component_archive
    root=tmp_path/'root';root.mkdir()
    site=tmp_path/'site'
    site.write_text('server {\n    allow 38.76.174.234;\n    deny all;\n    location /node/ { proxy_pass http://127.0.0.1:8800/; }\n}\n')
    etc=tmp_path/'etc/systemd/system';etc.mkdir(parents=True)
    lock=tmp_path/'run/lock/marx-search-release.lock';lock.parent.mkdir(parents=True)
    def private_path(value):
        if str(value)=='/etc/systemd/system':return etc
        if str(value)=='/run/lock/marx-search-release.lock':return lock
        return Path(value)
    monkeypatch.setattr(transaction,'ROOT',root);monkeypatch.setattr(transaction,'SITE',site)
    monkeypatch.setattr(transaction,'Path',private_path)
    monkeypatch.setitem(sys.modules,'fcntl',types.SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *a:None))
    monkeypatch.setitem(sys.modules,'pwd',types.SimpleNamespace(getpwnam=lambda _:types.SimpleNamespace(pw_uid=1,pw_gid=1)))
    monkeypatch.setattr(transaction.os,'chown',lambda *a:None,raising=False)
    commands=[];health_calls=[]
    monkeypatch.setattr(transaction,'call',lambda *args:commands.append(args) or '')
    monkeypatch.setattr(transaction.subprocess,'run',lambda args,**kw:commands.append(tuple(args)) or types.SimpleNamespace(returncode=1 if 'is-enabled' in args else 0))
    def health():
        health_calls.append(1)
        if failure:raise ValueError('Fixture resource gate rejected')
    monkeypatch.setattr(transaction,'health',health)
    monkeypatch.setattr(transaction,'switch',lambda target:(root/'current').write_text(target.name))
    monkeypatch.setattr(transaction,'current_release',lambda:(root/'current').read_text() if (root/'current').exists() else 'absent')
    args=['transaction','promote','--expected','absent','--target','fixture-release','--archive',str(archive),'--sha256',hashlib.sha256(archive.read_bytes()).hexdigest()]
    monkeypatch.setattr(sys,'argv',args)
    if failure:
        with pytest.raises(ValueError,match='Fixture resource gate'):transaction.main()
    else:
        transaction.main()
        assert '127.0.0.1:8801/ocr/' in site.read_text()
        monkeypatch.setattr(sys,'argv',['transaction','rollback','--expected','fixture-release','--target','absent'])
        transaction.main()
    assert len(health_calls)==1
    assert not (root/'current').exists()
    assert 'return 503' in site.read_text()
    assert 'proxy_pass http://127.0.0.1:8800/' in site.read_text()
    assert not any(command[:2]==('systemctl','stop') and any(unit in ('mylib-node','mylib-node.service','minio','marx-search') for unit in command[2:]) for command in commands)


def test_main_python_and_changed_powershell_syntax():
    ast.parse((ROOT/'app.py').read_text(encoding='utf-8'))
    if sys.platform=='win32':
        names=['release.ps1','rollback_release.ps1','mylib_ocr_component.ps1']
        files=','.join("'"+str(ROOT/'deploy'/name).replace("'","''")+"'" for name in names)
        script="$bad=$false; foreach($p in @("+files+")) {$tokens=$null;$errors=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($p,[ref]$tokens,[ref]$errors);if($errors){$errors | ForEach-Object {$_.Message};$bad=$true}};if($bad){exit 1}"
        subprocess.run(['pwsh','-NoProfile','-Command',script],check=True,capture_output=True,text=True)
