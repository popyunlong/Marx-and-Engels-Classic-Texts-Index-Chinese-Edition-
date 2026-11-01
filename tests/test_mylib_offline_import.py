"""First-run scenarios for the newly added complete offline admin import only."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest
from flask import Flask, abort, request

from mylib_ocr.queue import Queue
from mylib_ocr.worker import reconcile_startup
from personal_library_processing import Processing
from personal_library_offline import ImportConflict, OfflineImports, read_bundle, register_admin_routes

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 64
RUN = '1' * 32


def bundle():
    return [dict(type='mylib-offline-ocr', format_version=1, uid=1, sid=1, source_sha256=SHA,
                 total=2, run_id=RUN, layout_schema=3, engine='rapidocr-layout-v3'),
            dict(page=1, text='', layout_schema=3, ocr_profile='legacy-v3', layout_blocks=[], folio_candidates=[]),
            dict(page=2, text='真实OCR文字', layout_schema=3, ocr_engine='rapidocr-layout-v3',
                 ocr_profile='bounded-page-v1', image_width=100, image_height=200,
                 layout_blocks=[dict(text='真实OCR文字', bbox=[.1,.2,.8,.3], confidence=.99)],
                 folio_candidates=[], printed_pages=['999'])]


def payload(values=None):
    return ('\n'.join(json.dumps(row, ensure_ascii=False) for row in (values or bundle()))+'\n').encode('utf-8')


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    membership=types.ModuleType('membership'); membership.utc_now_text=lambda:'2026-11-01T00:00:00+00:00'
    runtime=types.ModuleType('runtime_env'); runtime.APPDATA_DIR=tmp_path; runtime.secure_db_file=lambda p:None
    monkeypatch.setitem(sys.modules,'membership',membership); monkeypatch.setitem(sys.modules,'runtime_env',runtime)
    spec=importlib.util.spec_from_file_location('offline_first_library',ROOT/'personal_library.py')
    library=importlib.util.module_from_spec(spec); spec.loader.exec_module(library)
    library.init_personal_library_db()
    with library._connect() as db:
        db.execute("INSERT INTO personal_library_submissions(id,user_id,title,sha256,source_kind,ocr_consent,license_attested,page_count,status,created_at) VALUES(1,1,'Fixture',?,'scanned',1,1,2,'failed','2026-11-01')",(SHA,))
    monkeypatch.setattr(library,'ocr_quota_allowance',lambda uid,total:(total,''))
    jobs=[]; writes=[]; accepts=[]
    class HeldThread:
        def __init__(self, target,args,**kw): self.target=target; self.args=args
        def start(self): jobs.append(self)
        def finish(self): self.target(*self.args)
    class NoNode:
        def __getattr__(self, name): raise AssertionError('Offline import must not call the storage/OCR node: '+name)
    def write(row,pages,**kw): writes.append((row,pages,kw)); return sum(bool(p['text']) for p in pages)
    def finalize(sid,written): accepts.append((sid,written)); library.set_status(sid,'quality_review')
    processor=Processing(library,NoNode(),None,write,finalize)
    imports=OfflineImports(library,processor,thread=HeldThread)
    return types.SimpleNamespace(library=library,imports=imports,jobs=jobs,writes=writes,accepts=accepts,processor=processor)


def test_complete_offline_bundle_is_async_private_idempotent_and_accepts_once(fixture):
    f=fixture
    raw=payload()
    first=f.imports.submit(1,io.BytesIO(raw))
    assert first['state']=='queued' and not first['duplicate']
    row=f.library.get_submission(1)
    assert row['processing_run_id']==RUN and row['processing_run_kind']=='offline'
    assert not f.writes and not f.accepts and len(f.jobs)==1
    assert f.imports.submit(1,io.BytesIO(raw))['duplicate']
    assert len(f.jobs)==1
    path=f.imports.root/'1'/'1'/(RUN+'.jsonl')
    assert path.read_bytes()==raw
    f.jobs[0].finish()
    assert len(f.writes)==len(f.accepts)==1
    row,pages,_=f.writes[0]
    assert row['user_id']==1 and row['id']==1
    assert len(pages)==2 and pages[0]['text']==''
    assert pages[1]['printed_pages']==[]  # Forged input folio is ignored.
    assert f.imports.status(f.library.get_submission(1),RUN)['state']=='completed'
    assert f.imports.submit(1,io.BytesIO(raw))['duplicate']
    assert len(f.jobs)==len(f.accepts)==1
    assert f.library.ocr_pages_used(1)==1


@pytest.mark.parametrize('invalid',['owner','submission','source','count','run','schema','missing-empty','duplicate-page','extra-page','nonfinite-box','reversed-box','pixel-budget'])
def test_invalid_offline_bundle_never_begins_processing(fixture,invalid):
    f=fixture; values=bundle()
    if invalid=='owner': values[0]['uid']=2
    elif invalid=='submission': values[0]['sid']=2
    elif invalid=='source': values[0]['source_sha256']='b'*64
    elif invalid=='count': values[0]['total']=3
    elif invalid=='run': values[0]['run_id']='../bad'
    elif invalid=='schema': values[1]['layout_schema']=2
    elif invalid=='missing-empty': del values[1]
    elif invalid=='duplicate-page': values[2]['page']=1
    elif invalid=='extra-page': values.append(copy.deepcopy(values[2]))
    elif invalid=='nonfinite-box': values[2]['layout_blocks'][0]['bbox'][0]=float('nan')
    elif invalid=='reversed-box': values[2]['layout_blocks'][0]['bbox']=[.8,.2,.1,.3]
    elif invalid=='pixel-budget': values[2]['image_width']=2201
    with pytest.raises(ValueError): f.imports.submit(1,io.BytesIO(payload(values)))
    assert f.library.get_submission(1)['processing_run_state']==''
    assert not f.jobs and not f.writes and not f.accepts
    assert not list((f.imports.root/'runs').glob('*.json'))
    assert not list((f.imports.root/'incoming').iterdir())


def test_reused_run_with_changed_bundle_or_other_book_is_rejected(fixture):
    f=fixture
    f.imports.submit(1,io.BytesIO(payload()))
    changed=bundle(); changed[2]['text']='Different text'
    with pytest.raises(ImportConflict,match='不同导入包'): f.imports.submit(1,io.BytesIO(payload(changed)))
    with f.library._connect() as db:
        db.execute("INSERT INTO personal_library_submissions(id,user_id,title,sha256,source_kind,ocr_consent,license_attested,page_count,status,created_at) VALUES(2,2,'Other',?,'scanned',1,1,2,'failed','2026-11-01')",(SHA,))
    other=bundle(); other[0].update(sid=2,uid=2)
    with pytest.raises(ImportConflict,match='不属于'): f.imports.submit(2,io.BytesIO(payload(other)))
    assert len(f.jobs)==1 and not f.writes


def test_active_book_and_missing_consent_or_quota_cannot_import(fixture,monkeypatch):
    f=fixture
    f.library.begin_processing_run(1)
    with pytest.raises(ImportConflict,match='无活动任务'): f.imports.submit(1,io.BytesIO(payload()))
    with f.library._connect() as db:
        db.execute("UPDATE personal_library_submissions SET processing_run_state='',status='failed',ocr_consent=0 WHERE id=1")
    with pytest.raises(ValueError,match='未授权'): f.imports.submit(1,io.BytesIO(payload()))
    with f.library._connect() as db: db.execute('UPDATE personal_library_submissions SET ocr_consent=1 WHERE id=1')
    monkeypatch.setattr(f.library,'ocr_quota_allowance',lambda uid,total:(0,'fixture quota'))
    with pytest.raises(ValueError,match='额度不足'): f.imports.submit(1,io.BytesIO(payload()))
    assert not f.jobs and not f.writes and not f.accepts


def test_offline_upload_stream_has_hard_size_bound(fixture,monkeypatch):
    import personal_library_offline as module
    monkeypatch.setattr(module,'MAX_BUNDLE_BYTES',128)
    with pytest.raises(ValueError,match='32MiB'): fixture.imports.submit(1,io.BytesIO(b'x'*129))
    assert not fixture.jobs and not list((fixture.imports.root/'incoming').iterdir())


def test_unknown_prepared_import_is_observed_not_replayed(fixture):
    f=fixture; raw=payload()
    f.imports.submit(1,io.BytesIO(raw))
    receipt=f.imports.status(f.library.get_submission(1),RUN)
    receipt['state']='prepared'; f.imports._update(receipt)
    with f.library._connect() as db: db.execute("UPDATE personal_library_submissions SET processing_run_state='',status='failed' WHERE id=1")
    result=f.imports.submit(1,io.BytesIO(raw))
    assert result['duplicate'] and result['state']=='prepared' and len(f.jobs)==1
    assert not f.writes and not f.accepts


def test_offline_restart_never_watches_node_or_reaccepts(fixture):
    f=fixture
    f.imports.submit(1,io.BytesIO(payload()))
    f.processor.reconcile()
    row=f.library.get_submission(1)
    assert row['processing_run_state']=='interrupted' and row['status']=='failed'
    assert f.imports.status(row,RUN)['state']=='interrupted'
    # An already-created old thread also cannot write after startup interrupted it.
    f.jobs[0].finish()
    assert not f.writes and not f.accepts


def test_queued_only_worker_startup_unconditionally_pauses(tmp_path):
    queue=Queue(tmp_path)
    queue.grant(1,1,'One fixture authorization')
    queue.enqueue(1,1,RUN,SHA,2)
    queue.set_dispatch(True)
    reconcile_startup(queue)
    assert not queue.dispatch_enabled()
    assert queue.status(1,1,RUN)['state']=='queued'


def test_admin_import_route_requires_auth_and_csrf_before_reading_bundle(fixture,monkeypatch):
    import personal_library_offline as module
    f=fixture; calls=[]; app=Flask(__name__)
    def admin():
        if request.headers.get('Fixture-Admin')!='yes': abort(403)
    def csrf():
        if request.headers.get('Fixture-CSRF')!='yes': abort(400)
    def submit(*args): calls.append(args); return {'duplicate':False,'run_id':RUN,'bundle_sha256':SHA}
    monkeypatch.setattr(module.OfflineImports,'submit',submit)
    register_admin_routes(app,library=f.library,processor=lambda:f.processor,require_admin=admin,csrf=csrf,
                          audit=lambda **kw:None,flash=lambda *args:None,redirect=lambda *args:('queued',202))
    client=app.test_client(); url='/admin/library/submissions/1/import-ocr'
    assert client.post(url).status_code==403
    assert client.post(url,headers={'Fixture-Admin':'yes'}).status_code==400
    assert not calls
    response=client.post(url,headers={'Fixture-Admin':'yes','Fixture-CSRF':'yes'},data={'file':(io.BytesIO(payload()),'book.jsonl')})
    assert response.status_code==202 and len(calls)==1
    assert client.get('/admin/library/submissions/1/offline-import/'+RUN).status_code==403
