import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dictionary_graph import Graph, GraphUnavailable, file_hash
from scripts.build_dictionary_graph import (Budget, BudgetExceeded, candidates_and_rules,
    evidence, make_edge, validate_relations, write_graph, build_lock, Mimo)


def entry(slug, title, content, page=1, review=0):
    return dict(slug=slug,title=title,content=content,start_page=page,end_page=page,
                citation=f"测试辞典，第{page}页",needs_review=review)


@pytest.fixture
def sample(tmp_path):
    entries=[entry('capital','资本','资本与剩余价值有关。\n\n劳动并不等于劳动力。'),
             entry('surplus','剩余价值','剩余价值与劳动有关。',2),
             entry('labor','劳动','劳动与劳动力需要区别。',3),
             entry('power','劳动力','劳动力并非劳动。',4),
             entry('bad','疑点','资本。',5,1)]
    source=tmp_path/'source.sqlite'
    source.write_bytes(b'test immutable source')
    candidates,edges=candidates_and_rules(entries)
    inferred=make_edge('capital','power','related','inference','探索线索',[evidence(entries[0],'劳动并不等于劳动力。')], 'mimo-v2.6-flash','quote_checked')
    edges[inferred['id']]=inferred
    out=tmp_path/'v1'
    write_graph(source,out,entries,edges,{}, {},'v1')
    selected=json.loads((out/'binding.json').read_text('utf-8'))
    return entries,edges,Graph(out/'graph.sqlite',selected,source),selected,source


def test_literal_mentions_do_not_become_semantic_assertions(sample):
    entries,edges,graph,_,_=sample
    original=[e for e in edges.values() if e['layer']=='evidence']
    assert original and all(e['kind']=='mention' for e in original)
    assert not any(e['source']=='bad' or e['target']=='bad' for e in edges.values())
    assert any(e['target']=='power' for e in original)


def test_graph_filters_caps_and_paths(sample):
    _,_,graph,_,_=sample
    small=graph.neighborhood('capital',limit=1)
    assert len(small['nodes'])==2
    assert all(e['layer']=='evidence' for e in small['edges'])
    assert any(e['layer']=='inference' for e in graph.neighborhood('capital',inference=True)['edges'])
    assert graph.path_between('capital','power')['found']
    assert graph.path_between('capital','capital')['edges']==[]
    assert not graph.path_between('capital','bad')['found']
    assert graph.browse("' OR 1=1 --")==[]
    with pytest.raises(KeyError):graph.neighborhood('missing')


def test_wrong_fingerprint_and_corruption_rejected(sample):
    _,_,graph,selected,source=sample
    with pytest.raises(GraphUnavailable):Graph(graph.path,dict(selected,source_sha256='0'*64),source)
    with graph.path.open('ab') as f:f.write(b'corrupt')
    with pytest.raises(GraphUnavailable):Graph(graph.path,selected,source)


def test_exact_quotes_and_target_allowlist(sample):
    entries,_,_,_,_=sample
    e=entries[0]; by={v['slug']:v for v in entries}
    valid={'target':'surplus','kind':'related','quote':'资本与剩余价值有关。','explanation':'探索联系'}
    accepted,rejected=validate_relations({'relations':[valid,dict(valid,quote='不存在的引文'),dict(valid,target='injected')]},e,by,{'surplus'})
    assert len(accepted)==1 and rejected==2
    assert accepted[0]['layer']=='inference'
    assert accepted[0]['evidence'][0]['paragraph']==1
    assert evidence(e,'资本与剩余价值有关。\n\n劳动并不等于劳动力。') is None


def test_budget_unknown_charges_survive_restart_and_ceiling_is_capped(tmp_path):
    path=tmp_path/'budget.sqlite'
    b=Budget(path,500)
    assert b.report()['ceiling_yuan']==50
    b.reserve('first','mimo-v2.6-flash',[{'role':'user','content':'hello'}],100)
    before=b.report()['charged_or_reserved_yuan']
    assert before>0 and Budget(path).report()['charged_or_reserved_yuan']==before
    with pytest.raises(BudgetExceeded):b.reserve('first','mimo-v2.6-flash',[],1)
    b.settle('first',{'prompt_tokens':100,'completion_tokens':10})
    assert b.report()['charged_or_reserved_yuan']==.00012


def test_budget_reservation_is_atomic_across_workers(tmp_path):
    path=tmp_path/'budget.sqlite';Budget(path,.006)
    def reserve(i):
        try:Budget(path,.006).reserve(str(i),'mimo-v2.6-flash',[],100);return True
        except BudgetExceeded:return False
    with ThreadPoolExecutor(max_workers=8) as pool:result=list(pool.map(reserve,range(10)))
    assert sum(result)==2
    assert Budget(path,.006).report()['charged_or_reserved_yuan']<=.006


def test_more_than_four_steps_is_not_a_path(tmp_path):
    es=[entry(str(i),str(i),'测试词条') for i in range(6)]
    edges={}
    for i in range(5):
        edge=make_edge(str(i),str(i+1),'mention','evidence','提及',[])
        edges[edge['id']]=edge
    source=tmp_path/'source';source.write_text('source');out=tmp_path/'long'
    write_graph(source,out,es,edges,{}, {},'long')
    graph=Graph(out/'graph.sqlite',json.loads((out/'binding.json').read_text()),source)
    assert graph.path_between('0','4')['found']
    assert not graph.path_between('0','5')['found']


def test_api_access_stale_version_and_limits(sample,monkeypatch):
    import dictionary_map_web
    from werkzeug.exceptions import Forbidden
    graph=sample[2]
    monkeypatch.setattr(dictionary_map_web,'current_graph',lambda:graph)
    # Isolated blueprint exercises the same endpoints without importing any external services.
    from flask import Flask
    fixture=Flask(__name__)
    fixture.add_url_rule('/entry/<slug>',endpoint='dictionary_entry_page',view_func=lambda slug:slug)
    def access(feature):
        assert feature=='dictionary'
        if fixture.config.get('DENY'):raise Forbidden()
    fixture.register_blueprint(dictionary_map_web.create_blueprint(access,lambda:{}))
    api=fixture.test_client()
    assert api.get('/api/dictionary/graph?center=capital&limit=invalid').status_code==400
    assert api.get('/api/dictionary/graph?center=capital&version=old').status_code==409
    assert api.get('/api/dictionary/graph?center=missing').status_code==404
    result=api.get('/api/dictionary/graph?center=capital&limit=999')
    assert result.status_code==200 and len(result.json['nodes'])<=60
    assert result.headers['Cache-Control']=='private, no-store'
    fixture.config['DENY']=True
    assert api.get('/api/dictionary/graph').status_code==403
    monkeypatch.setattr(dictionary_map_web,'current_graph',lambda:None)
    fixture.config['DENY']=False
    assert api.get('/api/dictionary/graph').status_code==503


def test_ambiguous_compounds_and_page_qualified_references():
    es=[entry('source','源词条','学习马克思主义。参见8页“重复词”。发展的过程。'),
        entry('marx','马克思','人物'),entry('m1','马克思主义','理论'),entry('m2','马克思主义','理论',2),
        entry('one','重复词','内容',7),entry('two','重复词','内容',8),entry('grow','发展','哲学范畴')]
    _,edges=candidates_and_rules(es)
    links=[e for e in edges.values() if e['source']=='source']
    assert not any(e['target'] in {'marx','grow','one'} for e in links)
    assert any(e['kind']=='reference' and e['target']=='two' for e in links)


def test_checkpoint_has_only_one_writer(tmp_path):
    with build_lock(tmp_path):
        with pytest.raises(OSError):
            with build_lock(tmp_path):pass
    with build_lock(tmp_path):pass


def test_provider_filter_is_terminal_and_format_retry_is_bounded(tmp_path,monkeypatch):
    budget=Budget(tmp_path/'budget.sqlite')
    client=Mimo('fake-key',tmp_path,budget)
    calls=[]
    def response(reason,content):
        value={'choices':[{'finish_reason':reason,'message':{'content':content}}],
               'usage':{'prompt_tokens':10,'completion_tokens':10}}
        from types import SimpleNamespace
        return SimpleNamespace(status_code=200,content=b'fake response',json=lambda:value)
    def filtered(*a,**kw):calls.append(kw);return response('content_filter','')
    monkeypatch.setattr(client.session,'post',filtered)
    assert client.ask('mimo-v2.6-flash',[])['status']=='provider_filtered'
    assert client.ask('mimo-v2.6-flash',[])['status']=='provider_filtered'
    assert len(calls)==1
    def malformed(*a,**kw):calls.append(kw);return response('stop','not-json')
    monkeypatch.setattr(client.session,'post',malformed)
    assert client.ask('mimo-v2.6-flash',[{'role':'user','content':'fixture'}])['status']=='model_format_error'
    assert len(calls)==3 and budget.report()['requests']==3
