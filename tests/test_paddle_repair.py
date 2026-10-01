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
