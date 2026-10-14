from __future__ import annotations
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from _test_env import APPDATA
import personal_library as library
import recommendations as jobs


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(library, 'PERSONAL_LIB_DIR', tmp_path)
    monkeypatch.setattr(library, 'DB_PATH', tmp_path/'library.sqlite3')
    library.init_personal_library_db()
    row=library.create_book_recommendation(user_id=1,user_email='reader@example.test',title='Book',
        author='',note='',original_filename='book.pdf',byte_size=100,page_count=3,sha256='a'*64)
    library.mark_book_recommendation_stored(row['id'])
    return row


def test_duplicate_approval_and_cross_user_upload_share_job(store):
    row=library.create_book_recommendation(user_id=2,user_email='second@example.test',title='Same book',
        author='',note='',original_filename='book.pdf',byte_size=100,page_count=3,sha256='a'*64)
    library.mark_book_recommendation_stored(row['id'])
    for rid in [store['id'],store['id'],row['id']]:jobs.decide(rid,'approve','admin')
    first=jobs.claim()
    assert first and jobs.claim() is None
    assert jobs.get('a'*64)['attempts']==1


def test_reject_requires_reason_and_cannot_reject_active_job(store):
    with pytest.raises(ValueError):jobs.decide(store['id'],'reject','admin')
    jobs.decide(store['id'],'reject','admin','Wrong edition')
    assert library.get_book_recommendation(store['id'])['status']=='archived'
    assert jobs.claim() is None
    jobs.decide(store['id'],'approve','admin')
    jobs.claim()
    with pytest.raises(ValueError):jobs.decide(store['id'],'reject','admin','Changed mind')


def test_failed_retry_and_lease_recovery(store):
    jobs.decide(store['id'],'approve','admin')
    old=jobs.claim()
    with library._connect() as c:c.execute('UPDATE recommendation_publications SET lease=0')
    new=jobs.claim()
    assert new['owner']!=old['owner']
    with pytest.raises(RuntimeError):jobs.progress(old,1)
    jobs.progress(new,2,state='failed',error='Unavailable OCR')
    jobs.decide(store['id'],'retry','admin')
    resumed=jobs.claim()
    assert resumed['completed_pages']==2


def test_no_publication_before_quality_review(store):
    jobs.decide(store['id'],'approve','admin')
    with pytest.raises(ValueError):jobs.mark_published('a'*64,'book','release')
    job=jobs.claim();jobs.progress(job,3,state='review')
    jobs.mark_ready('a'*64,'b'*64,'book','reviewer')
    with pytest.raises(ValueError):jobs.mark_published('a'*64,'different','release')
    jobs.mark_published('a'*64,'book','release')
    jobs.mark_published('a'*64,'book','release')
    assert library.get_book_recommendation(store['id'])['status']=='archived'
    with library._connect() as c:
        assert c.execute("SELECT count(*) FROM recommendation_publication_events WHERE action='published'").fetchone()[0]==1


def test_archived_is_not_published_and_rollback_restores_ready(store):
    library.set_book_recommendation_status(store['id'],'archived')
    assert not jobs.decorate([library.get_book_recommendation(store['id'])])[0]['publication']
    jobs.decide(store['id'],'approve','admin');job=jobs.claim();jobs.progress(job,3,state='review')
    jobs.mark_ready('a'*64,'b'*64,'book','reviewer');jobs.mark_published('a'*64,'book','r1')
    assert jobs.decorate([dict(store)],{})[0]['publication']['state']=='ready'
    assert jobs.decorate([dict(store)],{'a'*64:'book'})[0]['publication']['state']=='published'


@pytest.fixture
def visibility():
    # Execute the actual visibility boundary without starting app databases or workers.
    tree=ast.parse((Path(__file__).parents[1]/'app.py').read_text('utf8'))
    names={'_parse_public_timestamp','_runtime_public_window','_book_is_public','_search_export_crossed_public_boundary'}
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    context=dict(datetime=datetime,timezone=timezone,BookConfig=SimpleNamespace,
                 BOOK_CONFIG_BY_KEY={},BOOK_CONFIGS=[],_CORPUS_SCOPE_BY_ID={})
    code=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(code),'app.py','exec'),context)
    return context


@pytest.mark.parametrize('year',[2025,2026,2027,2036])
def test_recommendation_never_expires_but_unavailable_stays_hidden(visibility,year):
    cfg=SimpleNamespace(key='book',available=True,collection='user_recommended',
                        public_from='2026-09-01T00:00:00Z',public_until='2026-10-01T00:00:00Z')
    assert visibility['_book_is_public'](cfg,now=datetime(year,10,14,tzinfo=timezone.utc))
    cfg.available=False
    assert not visibility['_book_is_public'](cfg)


def test_unrelated_timed_books_still_expire(visibility):
    cfg=SimpleNamespace(available=True,collection='other',public_from='2026-09-01T00:00:00Z',public_until='2026-10-01T00:00:00Z')
    assert not visibility['_book_is_public'](cfg,now=datetime(2027,1,1,tzinfo=timezone.utc))


def test_old_export_is_not_blocked_by_historic_recommendation_expiry(visibility):
    visibility['BOOK_CONFIGS']=[SimpleNamespace(key='book',collection='user_recommended',
        public_from='2020-01-01T00:00:00Z',public_until='2020-02-01T00:00:00Z')]
    assert not visibility['_search_export_crossed_public_boundary']({'created_at':'2020-01-05T00:00:00Z'})


def test_new_recommendations_do_not_stamp_expiration(tmp_path):
    from ingestion.candidate import stamp_public_windows
    record={'candidate_config':{}}
    assert stamp_public_windows(tmp_path,[{'metadata':{'collection':'user_recommended','public_window_days':30}}],record)==record
    assert not list(tmp_path.iterdir())


@pytest.fixture
def real_pdf(store, tmp_path, monkeypatch):
    import hashlib
    import fitz
    import ingestion.recommendations as worker
    doc=fitz.open()
    for n in range(3):
        page=doc.new_page()
        page.insert_text((40,80), f'Chapter {n+1}. Original source text with sufficient words for the searchable body.')
    blob=doc.tobytes();doc.close()
    digest=hashlib.sha256(blob).hexdigest()
    with library._connect() as c:
        c.execute('UPDATE user_book_recommendations SET sha256=?,byte_size=? WHERE id=?',
                  (digest,len(blob),store['id']))
    monkeypatch.setenv('RECOMMENDATION_WORK_DIR',str(tmp_path/'processing'))
    monkeypatch.setattr(worker.storage,'fetch_book_recommendation',lambda *_:(blob,'application/pdf'))
    return library.get_book_recommendation(store['id'])


def test_interrupted_worker_resumes_saved_pages_without_redownload(real_pdf, monkeypatch):
    import ingestion.recommendations as worker
    jobs.decide(real_pdf['id'],'approve','admin')
    progress=jobs.progress
    def interrupt(job, page, **kwargs):
        progress(job,page,**kwargs)
        if page==1 and kwargs.get('state','processing')=='processing':
            raise RuntimeError('simulated interruption')
    monkeypatch.setattr(jobs,'progress',interrupt)
    worker.run_once()
    assert jobs.get(real_pdf['sha256'])['state']=='failed'
    saved=worker.root()/real_pdf['sha256']/'page-00001.json'
    stamp=saved.stat().st_mtime_ns
    monkeypatch.setattr(jobs,'progress',progress)
    monkeypatch.setattr(worker.storage,'fetch_book_recommendation',lambda *_:pytest.fail('download repeated'))
    jobs.decide(real_pdf['id'],'retry','admin');worker.run_once()
    assert jobs.get(real_pdf['sha256'])['state']=='review'
    assert saved.stat().st_mtime_ns==stamp


def test_original_page_review_required_and_source_tamper_rejected(real_pdf):
    import copy
    import json
    import ingestion.recommendations as worker
    from scripts.review_recommendation import finalize
    jobs.decide(real_pdf['id'],'approve','admin');worker.run_once()
    review=dict(source_sha256=real_pdf['sha256'],
        metadata=dict(title='Book',authors=['Author'],publisher='Publisher',year='2020',place='Beijing',
                      edition='First',recommender_name='Reader',recommender_email_masked='reaXXX@example.test'),
        toc=[dict(title='Chapter 1',pdf_page=1)],body_samples=[1,2,3],bibliography_pages=[1],
        reviewed_pdf_pages=[1,2,3])
    bad=copy.deepcopy(review);bad['reviewed_pdf_pages']=[2,3]
    with pytest.raises(ValueError):finalize(real_pdf['id'],bad,'reviewer')
    bad=copy.deepcopy(review);bad['reviewed_pdf_pages'].append(4)
    with pytest.raises(ValueError):finalize(real_pdf['id'],bad,'reviewer')
    target=finalize(real_pdf['id'],review,'reviewer')
    package=json.loads(target.read_text('utf8'))
    assert package['metadata']['public_window_days']==0
    assert all(p['label']=='' for p in package['pages'])
    assert jobs.get(real_pdf['sha256'])['state']=='ready'
    (worker.root()/real_pdf['sha256']/'source.pdf').write_bytes(b'changed')
    with pytest.raises(ValueError):finalize(real_pdf['id'],review,'reviewer')


def test_admin_review_rejects_non_admin_and_missing_csrf(store, monkeypatch):
    import uuid
    import app as web
    import membership
    user=membership.create_user(email=uuid.uuid4().hex+'@example.test',display_name='Review test',
        password_hash='not-a-login-password',email_verified_at='2026-01-01T00:00:00+00:00')
    monkeypatch.setattr(web,'_published_recommendation_sources',lambda:{})
    client=web.app.test_client()
    url=f"/admin/book-recommendations/{store['id']}/approve"
    with client.session_transaction() as s:
        s['user_id']=user['id'];s['_csrf_token']='review-token'
        s['admin_2fa_verified_at']=datetime.now(timezone.utc).isoformat()
    assert client.post(url,data={'csrf_token':'review-token'}).status_code==403
    assert jobs.get(store['sha256']) is None
    import sqlite3
    with sqlite3.connect(web.MEMBERSHIP_DB_PATH) as c:
        c.execute("UPDATE users SET role='admin' WHERE id=?",(user['id'],))
    assert client.post(url).status_code==403
    assert jobs.get(store['sha256']) is None
    assert client.post(url,data={'csrf_token':'review-token'}).status_code==302
    assert jobs.get(store['sha256'])['state']=='queued'
    assert client.post(url,data={'csrf_token':'review-token'}).status_code==302
    assert jobs.get(store['sha256'])['attempts']==0


def test_historical_named_pdf_is_recognized_as_already_published(store, monkeypatch):
    import app as web
    monkeypatch.setattr(web,'BOOK_CONFIGS',[SimpleNamespace(key='historic',collection='user_recommended',recommendation_id=store['id'])])
    monkeypatch.setattr(web,'corpus',SimpleNamespace(books={'historic':[SimpleNamespace(source_file='pdfs/旧书名.pdf')]}))
    assert web._published_recommendation_sources()=={store['sha256']:'historic'}


def test_historical_backfill_does_not_invent_review_or_overwrite_new_jobs(store):
    jobs.register_historical({store['sha256']:'historic'},'r1')
    row=jobs.get(store['sha256'])
    assert row['state']=='published' and row['reviewed_at']==''
    jobs.register_historical({store['sha256']:'historic'},'r2')
    assert jobs.get(store['sha256'])['release_id']=='r1'
    with library._connect() as c:
        assert c.execute('SELECT count(*) FROM recommendation_publication_events').fetchone()[0]==1


def test_failed_publish_and_rollback_never_confirm_candidate(tmp_path):
    import json
    from ingestion.recommendations import committed_release
    live=tmp_path/'live'/'app';candidate=tmp_path/'candidate'/'app'
    live.mkdir(parents=True);candidate.mkdir(parents=True)
    for folder,name in [(live,'r1'),(candidate,'r2')]:
        (folder.parent/'release.json').write_text(json.dumps({'release_id':name}))
    ledger=tmp_path/'ledger.jsonl'
    assert committed_release(candidate,live,ledger)==''
    ledger.write_text(json.dumps({'event':'promote','release_id':'r1'}))
    assert committed_release(candidate,candidate,ledger)==''
    ledger.write_text(json.dumps({'event':'promote','release_id':'r2'}))
    assert committed_release(candidate,candidate,ledger)=='r2'
    ledger.write_text(json.dumps({'event':'rollback','release_id':'r1'}))
    assert committed_release(candidate,live,ledger)==''
