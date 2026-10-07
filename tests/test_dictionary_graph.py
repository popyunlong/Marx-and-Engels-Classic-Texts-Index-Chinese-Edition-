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


@pytest.fixture
def reading_graph(tmp_path):
    entries = [entry('center', '中心', '原文第一段。\n\n原文第二段。'), entry('empty', '无关系', '无关系正文。')]
    entries += [entry('n-' + str(i), '词条%03d' % i, '相关词条正文。', i + 2) for i in range(105)]
    entries += [entry('dup-a', '重复词', '重复正文。', 8), entry('dup-b', '重复词', '重复正文。', 3)]
    ev = [evidence(entries[0], '原文第一段。'), evidence(entries[0], '原文第二段。')]
    edges = {}
    for other in entries[2:]:
        edge = make_edge('center', other['slug'], 'mention', 'evidence', '正文提及', ev)
        edges[edge['id']] = edge
    for source, target, kind, layer in [('n-0', 'center', 'mention', 'evidence'),
                                         ('center', 'n-0', 'reference', 'evidence'),
                                         ('n-0', 'center', 'reference', 'evidence'),
                                         ('center', 'n-0', 'related', 'inference'),
                                         ('n-0', 'center', 'related', 'inference')]:
        edge = make_edge(source, target, kind, layer, '测试关系', ev)
        edges[edge['id']] = edge
    source = tmp_path / 'source'
    source.write_bytes(b'isolated reading fixture')
    out = tmp_path / 'reading'
    write_graph(source, out, entries, edges, {}, {}, 'reading-v1')
    return Graph(out / 'graph.sqlite', json.loads((out / 'binding.json').read_text('utf-8')), source)


def test_reading_pages_cover_all_neighbours_and_preserve_evidence(reading_graph):
    graph = reading_graph
    slugs, ids = [], []
    for offset in range(0, 107, 20):
        page = graph.relations('center', group='outgoing', offset=offset)
        assert page['total'] == 107
        assert len(page['items']) <= 20
        slugs.extend(item['slug'] for item in page['items'])
        ids.extend(edge['id'] for edge in page['edges'])
        assert all(len(edge['evidence']) == 2 for edge in page['edges'])
    assert len(slugs) == len(set(slugs)) == 107
    assert len(ids) == len(set(ids)) == 107
    assert len(graph.neighborhood('center', limit=59)['nodes']) == 60
    page = graph.relations('center', group='outgoing', query='重复词')
    assert [item['slug'] for item in page['items']] == ['dup-b', 'dup-a']
    assert graph.relations('center', group='outgoing', offset=9999)['offset'] == 100


def test_reading_groups_directions_filters_and_empty_states(reading_graph):
    graph = reading_graph
    page = graph.relations('center')
    assert page['group'] == 'reference'
    assert len(page['items']) == 1 and len(page['edges']) == 2
    assert {edge['source'] for edge in page['edges']} == {'center', 'n-0'}
    assert all(not group['id'].startswith('inference_') for group in page['groups'])
    inferred = graph.relations('center', inference=True, group='inference_related')
    assert inferred['total'] == 1 and len(inferred['edges']) == 2
    assert graph.relations('center', group='incoming')['total'] == 1
    assert graph.relations('center', group='outgoing', query="' OR 1=1 --")['total'] == 0
    assert graph.relations('center', group='outgoing', kind='reference')['items'] == []
    assert graph.relations('empty')['total_relations'] == 0
    assert graph.relations('center', group='inference_related')['group'] == 'reference'
    with pytest.raises(ValueError): graph.relations('center', group='invented')
    with pytest.raises(KeyError): graph.relations('absent')


def test_directory_counts_stable_pages_and_path_order(reading_graph):
    graph = reading_graph
    pages = [graph.browse_page(limit=30, offset=i) for i in range(0, 109, 30)]
    ids = [node['slug'] for page in pages for node in page['nodes']]
    assert len(ids) == len(set(ids)) == 109
    assert all(page['total'] == 109 for page in pages)
    assert graph.browse_page(query='重复词')['total'] == 2
    assert graph.browse_page(theme='不存在')['total'] == 0
    path = graph.path_between('n-2', 'n-1')
    assert [node['slug'] for node in path['nodes']] == ['n-2', 'center', 'n-1']
    assert [(step['from'], step['to']) for step in path['steps']] == [('n-2', 'center'), ('center', 'n-1')]
    assert path['edges'][0]['source'] == 'center'  # Exploration may walk against the arrow.
    assert graph.path_between('center', 'center')['steps'] == []
    assert graph.path_between('center', 'empty')['steps'] == []


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
    reading = api.get('/api/dictionary/relations?center=capital')
    assert reading.status_code == 200
    assert reading.headers['Cache-Control'] == 'private, no-store'
    assert all(node['url'].startswith('/entry/') for node in reading.json['nodes'])
    assert all('#paragraph-' in ev['url'] for edge in reading.json['edges'] for ev in edge['evidence'])
    assert api.get('/api/dictionary/relations?center=capital&group=invalid').status_code == 400
    assert api.get('/api/dictionary/relations?center=capital&limit=invalid').status_code == 400
    assert api.get('/api/dictionary/relations?center=capital&version=old').status_code == 409
    assert api.get('/api/dictionary/relations?center=missing').status_code == 404
    assert api.get('/api/dictionary/graph?limit=30').json['total'] == 5
    fixture.config['DENY']=True
    assert api.get('/api/dictionary/graph').status_code==403
    assert api.get('/api/dictionary/relations?center=capital').status_code == 403
    monkeypatch.setattr(dictionary_map_web,'current_graph',lambda:None)
    fixture.config['DENY']=False
    assert api.get('/api/dictionary/graph').status_code==503
    assert api.get('/api/dictionary/relations?center=capital').status_code == 503


def test_ambiguous_compounds_and_page_qualified_references():
    es=[entry('source','源词条','学习马克思主义。参见8页“重复词”。发展的过程。'),
        entry('marx','马克思','人物'),entry('m1','马克思主义','理论'),entry('m2','马克思主义','理论',2),
        entry('one','重复词','内容',7),entry('two','重复词','内容',8),entry('grow','发展','哲学范畴')]
    _,edges=candidates_and_rules(es)
    links=[e for e in edges.values() if e['source']=='source']
    assert not any(e['target'] in {'marx','grow','one'} for e in links)
    assert any(e['kind']=='reference' and e['target']=='two' for e in links)


def test_laborer_and_natural_philosophy_do_not_misidentify_short_concepts():
    es=[entry('s','人物','培养高素质劳动者，讲授自然哲学。'),entry('labor','劳动','劳动是人类活动。'),entry('nature','自然','哲学范畴。')]
    _,edges=candidates_and_rules(es)
    assert not any(e['source']=='s' for e in edges.values())
    es[0]['content']='劳动者的个体劳动。'
    _,edges=candidates_and_rules(es)
    assert any(e['source']=='s' and e['target']=='labor' for e in edges.values())


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


def test_alias_must_name_the_complete_source_entity():
    from scripts.build_dictionary_graph import explicit_alias
    party=entry('party','爱森纳赫派','政党派别。')
    assert explicit_alias(entry('source','德国社会民主工党','又称“爱森纳赫派”。'),party)
    assert not explicit_alias(entry('meeting','全德社会民主派代表大会','会议成立德国社会民主工党，又称“爱森纳赫派”。'),party)
    assert not explicit_alias(entry('event','德国社会民主工党的建立','历史事件。\n\n又称“爱森纳赫派”。'),party)


def test_complex_review_binds_exact_evidence_and_drops_unreviewed_types(tmp_path):
    from scripts.review_dictionary_relations import apply_review,review_covers
    a=entry('source','整体','整体包括部分。');b=entry('target','部分','组成部分。')
    complex_edge=make_edge('source','target','part','inference','组成关系',[evidence(a,'整体包括部分。')])
    literal=make_edge('source','target','mention','evidence','正文提及',[evidence(a,'整体包括部分。')])
    unseen=make_edge('target','source','broader','inference','不可靠',[])
    edges={e['id']:e for e in (complex_edge,literal,unseen)}
    review=tmp_path/'review.json'
    review.write_text(json.dumps({'protocol':1,'source_sha256':'fingerprint','decisions':{'source':{
        'inputs':[complex_edge],'accepted_ids':[complex_edge['id'],unseen['id']]}}}),encoding='utf-8')
    accepted=apply_review(edges,review,{'source':a,'target':b},'fingerprint')
    assert set(accepted)=={complex_edge['id'],literal['id']}
    assert accepted[complex_edge['id']]['review']=='mimo_v2.6_pro_direction_reviewed'
    changed=dict(complex_edge,explanation='改变了关系的含义')
    assert review_covers({'inputs':[complex_edge,unseen]},[complex_edge])
    assert not review_covers({'inputs':[complex_edge]},[changed])
    assert apply_review({changed['id']:changed},review,{'source':a,'target':b},'fingerprint')=={}
    with pytest.raises(ValueError):apply_review(edges,review,{'source':a,'target':b},'new fingerprint')


def test_complex_type_does_not_confuse_chapters_parts_or_opposition_subjects():
    from scripts.review_dictionary_relations import grounded_type
    source=entry('s','《测试白皮书》','包括尊重人权这一部分。');target=entry('t','尊重人权','概念。')
    e=make_edge('s','t','part','inference','部分',[evidence(source,source['content'])])
    assert not grounded_type(e,{'s':source,'t':target})


def test_reverse_components_and_attributed_exclusions(tmp_path):
    from scripts.review_dictionary_relations import grounded_type
    from scripts.build_dictionary_graph import apply_exclusions
    source=entry('s','地理环境','地理环境是社会存在的组成部分。');target=entry('t','社会存在','物质生活条件。')
    e=make_edge('s','t','part','inference','组成',[evidence(source,source['content'])])
    assert not grounded_type(e,{'s':source,'t':target})
    path=tmp_path/'exclusions.json'
    path.write_text(json.dumps({'source_sha256':'source','reviewer':'Test reviewer','reviewed_at':'2026-10-07',
        'exclusions':[{'id':e['id'],'reason':'reverse component direction'}]}),encoding='utf-8')
    assert apply_exclusions({e['id']:e},path,'source')=={}
    with pytest.raises(ValueError):apply_exclusions({e['id']:e},path,'other source')
    source=entry('s','固定资本','生产资本中用于购买机器的部分资本。');target=entry('t','生产资本','资本形式。')
    e=make_edge('s','t','broader','inference','部分',[evidence(source,source['content'])])
    assert not grounded_type(e,{'s':source,'t':target})
    source=entry('s','发展','哲学范畴。\n\n发展或静止是辩证法和形而上学的区别。');target=entry('t','形而上学','方法。')
    e=make_edge('s','t','opposes','inference','对立',[evidence(source,'发展或静止是辩证法和形而上学的区别。')])
    assert not grounded_type(e,{'s':source,'t':target})
    e['evidence']=[{'quote':'用发展的还是静止的观点看世界，是辩证法和形而上学的根本区别之一。'}]
    assert not grounded_type(e,{'s':source,'t':target})
