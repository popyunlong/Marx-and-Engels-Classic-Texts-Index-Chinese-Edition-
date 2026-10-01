import json
import sqlite3
from pathlib import Path
import threading
import pytest

from paddle_repair import (Queue, canonical, digest, parse_layout, parse_lines, prioritized_pages,
    validate_baseline, HealthGate, assert_capacity, write_evidence, assess_change)
from corpus_release import verify_quality, verify_functional_review, FUNCTIONS, page_identity
from scripts.paddle_corpus_repair import fetch_job, ProviderError


@pytest.fixture
def source(tmp_path):
    path=tmp_path/'source.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('CREATE TABLE pages(id INTEGER PRIMARY KEY,book TEXT,volume INTEGER,source_file TEXT,pdf_page INTEGER,printed_page TEXT,raw_text TEXT,normalized_text TEXT); CREATE TABLE toc_entries(id INTEGER PRIMARY KEY,title TEXT);')
        for i,(book,volume) in enumerate([('文集',1),('毛泽东选集',1),('习近平著作选读',1),('文集',2),('斯大林全集',1)],1):
            c.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?,?)',(i,book,volume,f'pdfs/{i}.pdf',1,'1','人民不是没有力量。','人民不是没有力量'))
    return path


def queue_for(source,tmp_path):
    q=Queue(tmp_path/'jobs/pilot.sqlite')
    q.bind({'corpus_sha256':'x'})
    q.plan(source,[{'id':1}])
    return q


def test_priority_round_robin(source):
    from paddle_repair import readonly
    with readonly(source) as conn:
        assert [r['id'] for r in prioritized_pages(conn)]==[1,2,3,4]
        assert [r['id'] for r in prioritized_pages(conn,per_group=1)]==[1,2,3]


def test_reported_errors_prioritize_later_volumes_after_named_first_batch(source):
    from paddle_repair import readonly
    with sqlite3.connect(source) as conn:
        conn.execute('INSERT INTO pages VALUES(6,?,?,?,?,?,?,?)',('文集',3,'pdfs/6.pdf',1,'1','原文','原文'))
    with readonly(source) as conn:
        rows=list(prioritized_pages(conn,priority_hints={'pdfs/6.pdf':{'reported_errors':5}}))
    assert [r['id'] for r in rows]==[1,2,3,6,4]


def test_crash_never_reposts_uncertain_submission(source,tmp_path):
    q=queue_for(source,tmp_path)
    q.prepare_job(1,'model','key')
    q.reserve(1,'model','2026-10-01',1)
    q.recover()
    assert q.prepare_job(1,'model','key')['state']=='uncertain'
    with pytest.raises(ValueError,match='reconcile'):
        q.reserve(1,'model','2026-10-01',1)
    with pytest.raises(ValueError,match='identity'):
        q.prepare_job(1,'model','another')


def test_budget_persists_and_review_is_version_bound(source,tmp_path):
    q=queue_for(source,tmp_path)
    q.prepare_job(1,'model','key');q.reserve(1,'model','2026-10-01',1)
    q.prepare_job(2,'model','key2')
    with pytest.raises(ValueError,match='budget'):
        q.reserve(2,'model','2026-10-01',1)
    q.record_result(1,tmp_path/'result.json',{'text':'人民是有力量的。'})
    assert q.status()['pages']=={'awaiting_review':1}
    with pytest.raises(ValueError,match='match'):
        q.review(1,decision='accepted',reviewer='reviewer',evidence={'source_verified':True})


def test_pause_prevents_new_provider_submission(source,tmp_path,monkeypatch):
    q=queue_for(source,tmp_path)
    monkeypatch.setenv('PADDLEOCR_ACCESS_TOKEN','test-only')
    stop=threading.Event();stop.set()
    with pytest.raises(ProviderError,match='paused'):
        fetch_job(q.path,tmp_path,1,b'image','pdfsha','model',12,stop)
    assert q.conn.execute('SELECT count(*) FROM usage').fetchone()[0]==0


def test_unknown_submission_pauses_all_jobs_and_preserves_identity(source,tmp_path,monkeypatch):
    from scripts.paddle_corpus_repair import Paddle
    q=queue_for(source,tmp_path)
    monkeypatch.setenv('PADDLEOCR_ACCESS_TOKEN','test-only')
    calls=[]
    def ambiguous(self,*args):
        calls.append(1)
        raise ProviderError('transport response unknown',uncertain=True)
    monkeypatch.setattr(Paddle,'submit',ambiguous)
    stop=threading.Event()
    with pytest.raises(ProviderError,match='unknown'):
        fetch_job(q.path,tmp_path,1,b'image','pdfsha','model',12,stop)
    assert stop.is_set()
    assert q.conn.execute('SELECT state FROM jobs').fetchone()[0]=='uncertain'
    with pytest.raises(ProviderError,match='reconciliation'):
        fetch_job(q.path,tmp_path,1,b'image','pdfsha','model',12,stop)
    assert len(calls)==1


def test_submission_carries_persisted_fingerprint_for_lost_response_lookup(monkeypatch):
    from scripts.paddle_corpus_repair import Paddle, PRIMARY_MODEL
    client=Paddle('test-only');calls=[];key='a'*64
    def request(path,data=None,content_type=None):
        calls.append((path,data,content_type))
        if data is not None:
            assert b'name="batchId"\r\n\r\nmarx-'+key.encode() in data
            raise ProviderError('unknown response',uncertain=True)
        return {'batchId':'marx-'+key,'extractResult':[{'jobId':'known-job','state':'done','resultUrl':'secret-url'}]}
    monkeypatch.setattr(client,'request',request)
    with pytest.raises(ProviderError,match='unknown'):
        client.submit(b'image',PRIMARY_MODEL,key)
    assert client.inspect_batch(key)==[{'jobId':'known-job','state':'done'}]
    assert len(calls)==2 and calls[1][1] is None
    assert calls[1][0]=='/api/v2/ocr/jobs/batch/marx-'+key


def test_batch_lookup_empty_or_wrong_identity_never_submits(monkeypatch):
    from scripts.paddle_corpus_repair import Paddle
    client=Paddle('test-only');key='b'*64;calls=[]
    def request(path,data=None,content_type=None):
        assert data is None
        calls.append(path)
        return {'batchId':'marx-'+key,'extractResult':[]}
    monkeypatch.setattr(client,'request',request)
    assert client.inspect_batch(key)==[]
    monkeypatch.setattr(client,'request',lambda *args: {'batchId':'another','extractResult':[]})
    with pytest.raises(ProviderError,match='identity mismatch'):client.inspect_batch(key)
    with pytest.raises(ValueError):client.batch_id('unpersisted-key')
    assert len(calls)==1


def layout(text='人民不是没有力量。',box=None):
    return canonical({'result':{'layoutParsingResults':[{'prunedResult':{'width':100,'height':200,
        'parsing_res_list':[{'block_label':'text','block_content':text,'block_bbox':box or [10,20,90,80]}]}}]}})


def test_block_geometry_cannot_masquerade_as_lines():
    result=parse_layout(layout())
    assert result['geometry_precision']=='block'
    assert '不' in result['text']
    with pytest.raises(ValueError):parse_layout(layout(box=[0,0,101,20]))
    with pytest.raises(ValueError):parse_lines(layout())
    assert 'negation' in assess_change('人民不是没有力量。','人民是没有力量。')['risks']


def test_line_confidence_rejects_nonfinite_values():
    for score in (float('nan'),float('inf'),-0.1,1.1):
        raw=canonical({'result':{'ocrResults':[{'prunedResult':{
            'rec_texts':['原文'],'rec_polys':[[[0,0],[1,0],[1,1],[0,1]]],'rec_scores':[score]}}]}})
        with pytest.raises(ValueError,match='confidence'):parse_lines(raw)


def test_frozen_wal_header_needs_no_source_sidecars(tmp_path):
    from paddle_repair import readonly
    path=tmp_path/'wal.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE evidence(value TEXT)')
        c.execute("INSERT INTO evidence VALUES('preserved')");c.commit()
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    c.close()
    before=set(tmp_path.iterdir())
    with readonly(path) as snapshot:
        assert snapshot.execute('SELECT value FROM evidence').fetchone()[0]=='preserved'
        with pytest.raises(sqlite3.OperationalError):snapshot.execute("DELETE FROM evidence")
    assert set(tmp_path.iterdir())==before


def test_worker_archive_validator_accepts_release_directories_and_rejects_links(tmp_path,monkeypatch):
    import io,sys,tarfile,shutil
    from types import SimpleNamespace
    monkeypatch.setattr(shutil,'disk_usage',lambda _:SimpleNamespace(total=100*1024**3,free=80*1024**3))
    from scripts.build_release_archive import build_archive
    app=tmp_path/'app';(app/'scripts').mkdir(parents=True)
    (app/'scripts/worker.py').write_text('pass\n')
    metadata=tmp_path/'release.json';metadata.write_text('{}')
    archive=tmp_path/'worker.tar.gz';build_archive(app,metadata,archive)
    installer=(Path(__file__).resolve().parents[1]/'deploy/install_paddle_worker.sh').read_text('utf-8')
    validator=installer.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
    monkeypatch.setattr(sys,'argv',['validator',str(tmp_path),str(archive)])
    exec(compile(validator,'archive-validator','exec'),{})
    for name,kind in [('app/link',tarfile.SYMTYPE),('../escape',tarfile.REGTYPE),
                      ('app/../escape',tarfile.DIRTYPE),('outside',tarfile.REGTYPE)]:
        with tarfile.open(archive,'w:gz') as tar:
            member=tarfile.TarInfo(name);member.type=kind;member.linkname='/etc/passwd'
            tar.addfile(member,io.BytesIO(b''))
        with pytest.raises(SystemExit,match='safe'):exec(compile(validator,'archive-validator','exec'),{})


def sample(t,ok=True,latency=.1,release='a'):
    return {'elapsed':t,'release':release,'catalog':{'id':'catalog'},'probes':[
        {'path':p,'ok':ok,'seconds':latency} for p in ('/api/runtime','/','/login')]}


def test_health_requires_full_baseline_and_two_failing_windows():
    rows=[sample(t) for t in range(0,1801,30)]
    baseline=validate_baseline(rows)
    with pytest.raises(ValueError):validate_baseline(rows[:-1])
    gate=HealthGate(baseline)
    assert gate.observe(sample(1830,False))==''
    assert gate.observe(sample(1860,False))=='critical_probe_failed'
    gate=HealthGate(baseline)
    outcomes=[gate.observe(sample(t,latency=.4)) for t in range(1830,2431,30)]
    assert 'sustained_latency_regression' in outcomes
    assert HealthGate(baseline).observe(sample(1800,release='b'))=='production_version_changed'
    with pytest.raises(ValueError):assert_capacity(100*1024**3,19*1024**3)


def test_operator_baseline_reuse_keeps_duration_freshness_and_original_limits(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from types import SimpleNamespace
    from scripts.paddle_corpus_repair import operator_baseline
    now=datetime.now(timezone.utc);rows=[sample(t) for t in range(0,1801,30)]
    # A remote timestamp can be ahead; receiver mtime and published age decide freshness.
    for row in rows:row['observed_at']='2099-01-01T00:00:00+00:00'
    original=validate_baseline(rows);snapshot={'app_release':{'id':'a'},'catalog_release':{'id':'catalog'}}
    path=tmp_path/'proof.json';path.write_bytes(canonical({'samples':rows,'original':original,'observation_age_at_publish_seconds':60}))
    path.chmod(0o444)
    real_stat=Path.stat
    def trusted_stat(self,*args,**kwargs):
        stat=real_stat(self,*args,**kwargs)
        if self==path:return SimpleNamespace(st_uid=0,st_mode=stat.st_mode,st_mtime=now.timestamp()-30)
        return stat
    monkeypatch.setattr(Path,'stat',trusted_stat)
    assert operator_baseline(path,snapshot,now)==original
    for bad in ({'samples':rows[:-1]}, {'observation_age_at_publish_seconds':650},
                {'original':{**original,'release':'different'}},
                {'original':{**original,'p95':{p:float('nan') for p in original['p95']}}}):
        payload={'samples':rows,'original':original,'observation_age_at_publish_seconds':60,**bad}
        path.chmod(0o644);path.write_bytes(canonical(payload));path.chmod(0o444)
        with pytest.raises(ValueError):operator_baseline(path,snapshot,now)
    regressed=[sample(t,latency=.4) for t in range(0,1801,30)]
    path.chmod(0o644);path.write_bytes(canonical({'samples':regressed,'original':original,'observation_age_at_publish_seconds':60}));path.chmod(0o444)
    with pytest.raises(ValueError,match='original thresholds'):operator_baseline(path,snapshot,now)
    path.chmod(0o644)
    with pytest.raises(ValueError,match='frozen'):operator_baseline(path,snapshot,now)


def test_worker_python_wrapper_keeps_venv_entrypoint(tmp_path,monkeypatch):
    import sys,venv,subprocess,os,shutil
    bash=os.environ.get('MARX_TEST_BASH') or shutil.which('bash')
    if not bash:pytest.skip('bash required to exercise the generated launcher')
    root=tmp_path/'root';root.mkdir();env=tmp_path/'venv with spaces'
    venv.EnvBuilder(with_pip=False).create(env)
    entry=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    installer=(Path(__file__).resolve().parents[1]/'deploy/install_paddle_worker.sh').read_text('utf-8')
    wrapper=installer.split('python3 - "$PYTHON" "$ROOT" <<\'PY\'\n',1)[1].split('\nPY\n',1)[0]
    monkeypatch.setattr(sys,'argv',['wrapper',str(entry),str(root)])
    exec(compile(wrapper,'venv-wrapper','exec'),{})
    prefix=subprocess.check_output([bash,str(root/'runtime-python'),'-c','import sys;print(sys.prefix)'],text=True).strip()
    assert Path(prefix).resolve()==env.resolve()
    assert not (root/'runtime-python').is_symlink()


def quality():
    return dict(verified_characters=20000,character_error_rate=.003,known_error_reduction=.5,
        no_quality_regression=True,unreviewed_high_risk=0,geometry_samples=150,
        geometry_accuracy=.99,wrong_book_or_page=0,volume_samples={'pdfs/1.pdf':10})


def test_publication_needs_quality_full_flows_and_rollback():
    verify_quality(quality(),['pdfs/1.pdf'])
    with pytest.raises(ValueError):verify_quality({**quality(),'character_error_rate':float('nan')},[])
    with pytest.raises(ValueError):verify_quality(quality(),['pdfs/missing.pdf'])
    selected={'id':'candidate','sha256':'a'*64}
    evidence={'corpus_release':selected,'result':'pass','checks':dict.fromkeys(FUNCTIONS,True),
              'browsers':['chrome','edge','mobile'],'rollback_rehearsal':True}
    verify_functional_review(evidence,selected)
    with pytest.raises(ValueError):verify_functional_review({**evidence,'rollback_rehearsal':False},selected)
    with pytest.raises(ValueError):verify_functional_review({**evidence,'checks':{'homepage':True}},selected)


def test_identity_ignores_only_text_and_evidence_never_overwrites(source,tmp_path):
    before=page_identity(source)
    with sqlite3.connect(source) as c:c.execute("UPDATE pages SET raw_text='repaired' WHERE id=1")
    assert page_identity(source)==before
    with sqlite3.connect(source) as c:c.execute("UPDATE pages SET printed_page='2' WHERE id=1")
    assert page_identity(source)!=before
    target=tmp_path/'evidence.json'
    write_evidence(target,{'raw':1})
    with pytest.raises(ValueError):write_evidence(target,{'raw':2})


def test_highlights_use_real_character_widths_or_entire_proven_line():
    from ocr_geometry import encode_rows,decode_rows,_flatten_rows,_rects_for_spans
    row={'text':'中文','bbox':[.1,.1,.9,.2],'confidence':1,
         'chars':[{'text':'中','bbox':[.1,.1,.3,.2]},{'text':'文','bbox':[.3,.1,.9,.2]}]}
    rows=decode_rows(encode_rows([row]));flat,refs=_flatten_rows(rows)
    assert flat=='中文'
    assert _rects_for_spans(rows,refs,[(1,2)],max_rects=2)==[(.3,.1,.9,.2)]
    row.pop('chars');row['precision']='line'
    rows=decode_rows(encode_rows([row]));_,refs=_flatten_rows(rows)
    assert _rects_for_spans(rows,refs,[(1,2)],max_rects=2)==[(.1,.1,.9,.2)]


def test_reviewed_update_changes_only_candidate_text_and_bound_geometry(source,tmp_path):
    import shutil
    from paddle_repair import file_hash
    from ocr_geometry import init_geometry_db
    from scripts.build_paddle_candidate import apply_reviews
    q=queue_for(source,tmp_path)
    target=tmp_path/'candidate';target.mkdir()
    shutil.copy2(source,target/'corpus.sqlite');init_geometry_db(target/'geometry.sqlite')
    source_hash=file_hash(source)
    result={'page_id':1,'book':'文集','volume':1,'source_file':'pdfs/1.pdf','pdf_page':1,'printed_page':'1',
        'baseline_hash':digest('人民不是没有力量。'),'pdf_sha256':'a'*64,'width':100,'height':100,
        'text':'人民是有力量的。','geometry_precision':'line','geometry_aligned':True,
        'geometry':[{'text':'人民是有力量的。','bbox':[.1,.1,.9,.2],'confidence':1,'precision':'line'}]}
    path=tmp_path/'geometry/1.json';q.record_result(1,path,result)
    q.review(1,decision='accepted',reviewer='test reviewer',evidence={
        'baseline_hash':result['baseline_hash'],'result_hash':file_hash(path),
        'reason':'fixture source verified','source_verified':True})
    changes=apply_reviews(tmp_path/'source',q,target)
    assert len(changes)==1 and file_hash(source)==source_hash
    with sqlite3.connect(target/'corpus.sqlite') as c:
        assert c.execute('SELECT raw_text FROM pages WHERE id=1').fetchone()[0]=='人民是有力量的。'
        assert c.execute('SELECT raw_text FROM pages WHERE id=2').fetchone()[0]=='人民不是没有力量。'
    assert page_identity(source)==page_identity(target/'corpus.sqlite')
    assert not (target/'manifest.json').exists()
