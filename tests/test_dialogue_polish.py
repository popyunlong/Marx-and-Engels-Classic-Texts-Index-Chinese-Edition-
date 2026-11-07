"""First-run scenarios for bounded variants and verified paragraph streaming."""
import contextlib
import io
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import ai
import ai_citations as C
import ai_comprehensive as T
import ai_quote_variants as V
import ai_web

SOURCE = '交换的深度、广度和方式都是由生产的发展和结构决定的。'
NEAR = SOURCE.replace('方式', '形式')
P = {'index': 1, 'text': SOURCE, 'quote_segments': [SOURCE], 'source_file': 'edition-a.pdf'}


@pytest.mark.parametrize('kind', ['quick', 'research', 'comprehensive'])
def test_shared_repair_keeps_variant_text_and_exact_stats_separate(kind):
    result = ai.ZAIClient.repair_grounded_answer('“'+NEAR+'”[1]', [P])
    assert NEAR in result['answer_markdown']
    assert '[1]' in result['answer_markdown']
    assert result['citation_stats']['direct_quotes'] == 0
    assert any(r['kind'] == 'approximate_quote' for r in result['citation_records'])


@pytest.mark.parametrize('text,kind', [(NEAR,'approximate_quote'),
    (SOURCE.replace('的发展','发展'),'approximate_quote'),
    ('交换的深度、广度……生产的发展和结构决定的。','elided_quote'),
    (SOURCE.replace('都是','都不是'),'pending_quote')])
def test_local_variant_classification(text,kind):
    result=V.Matcher([P]).match(text,[1])
    assert result['kind']==kind
    assert result['text_verified'] is False
    assert result['answer_text']==text


def test_exact_retrieval_label_and_minor_variant_are_independent():
    assert V.Matcher([{**P,'match_type':'fuzzy'}]).match(SOURCE,[1]) is None
    assert C.ledger('“'+SOURCE+'”[1]',[P])['citation_stats']['direct_quotes']==1


def test_ambiguity_dedup_and_explicit_edition():
    dup={**P,'index':2}
    other={**P,'index':3,'source_file':'edition-b.pdf'}
    assert V.Matcher([P,dup]).match(NEAR)['kind']=='approximate_quote'
    assert V.Matcher([P,other]).match(NEAR)['kind']=='pending_quote'
    assert V.Matcher([P,other]).match(NEAR,[3])['index']==3


def test_paraphrase_short_terms_and_unsafe_segments_stay_unpromoted():
    matcher=V.Matcher([P])
    assert matcher.match('生产关系') is None
    assert matcher.match('交换的具体组织形式受到社会生产方式的深刻影响。') is None
    assert not V.variants('`'+NEAR+'`[1]',[P])
    assert not V.Matcher([{**P,'quote_segments':[SOURCE[:12],SOURCE[12:]]}]).match(NEAR)


def test_unmarked_near_reference_is_added_without_rewording():
    restored=V.restore_references(NEAR,[P])
    assert restored==NEAR+'[1]'
    assert C.repair_missing_references(restored,[P])[0]==restored


@pytest.mark.parametrize('unfinished', ['“尚未结束\n\n', '"unfinished\n\n', '文字[1\n\n', '```python\nx=1\n\n', '文字<sect'])
def test_partial_markdown_is_never_published(unfinished):
    assert T.complete_blocks(unfinished,final=True)==('',unfinished)


def test_first_paragraph_precedes_second_and_simple_skips_checker():
    checkpoints=[]; verified=[]
    def stream(*_):
        yield '第一段完整正文。\n\n'
        assert checkpoints and checkpoints[0][0]=='第一段完整正文。'
        yield '第二段完整正文。<section_'
        yield 'complete/>'
    def verify(text): verified.append(text); return text
    def no_check(*_): raise AssertionError('simple answer must not call completion checker')
    result=T.generate('解释概念',[],{'complex':False,'sections':['一','二']},[],no_check,
        deadline=time.monotonic()+10,cancelled=lambda:False,checkpoint=lambda *v:checkpoints.append(v),verify=verify,stream=stream)
    assert result[2]=='complete'
    assert verified==['第一段完整正文。','第二段完整正文。']
    assert '<section_' not in result[0]


def test_cancel_preserves_only_verified_paragraph_and_closes_stream():
    state={'cancelled':False,'closed':False}; saved=[]
    def stream(*_):
        try:
            yield '已完成段落。\n\n'
            state['cancelled']=True
            yield '尚未完成的文字'
        finally: state['closed']=True
    result=T.generate('写作',[],{'complex':False},[],lambda *_:'',deadline=time.monotonic()+10,
        cancelled=lambda:state['cancelled'],checkpoint=lambda *v:saved.append(v),verify=lambda x:x,stream=stream)
    assert result[0]=='已完成段落。' and result[2]=='partial' and state['closed']


def test_stream_reuses_web_context_and_reports_actual_usage():
    client=object.__new__(ai.ZAIClient)
    client.config=SimpleNamespace(temperature=.2,request_timeout_seconds=30)
    route={'provider':'deepseek','base_url':'https://example.test','api_key':'fixture','auth_header':'Authorization','label':'fixture'}
    events=[]; requests=[]
    data=[{'choices':[{'delta':{'reasoning_content':'private reasoning'}}]},
          {'choices':[{'delta':{'content':'公开正文'}}]},
          {'usage':{'prompt_tokens':12,'completion_tokens':5}}]
    body=('\n'.join('data: '+json.dumps(x) for x in data)+'\ndata: [DONE]\n').encode()
    def response(req,**kwargs): requests.append(req); return io.BytesIO(body)
    web=SimpleNamespace(sources=[{}],prompt=lambda:'公开背景材料')
    with patch.object(client,'_ensure_enabled'),patch.object(client,'_route',return_value=route), \
         patch.object(ai,'_ai_http_slot',return_value=contextlib.nullcontext()), \
         patch.object(ai.urllib_request,'urlopen',side_effect=response), \
         patch.object(ai,'_emit_ai_call_event',side_effect=lambda e:events.append(e)),ai_web.answer_context(web):
        result=''.join(client.chat_complete_stream([{'role':'user','content':'问题'}],100,provider='deepseek',model='deepseek-v4-flash',reasoning_effort='off',deadline=time.monotonic()+10))
    assert result=='公开正文'
    assert json.loads(requests[0].data)['messages'][-1]['content']=='公开背景材料'
    assert events[-1]['phase']=='after' and events[-1]['completion_tokens']==5


def test_route_parallel_preparation_and_streamed_simple_result():
    import threading
    from flask import g
    import app as A
    import comprehensive_web as routes
    from test_comprehensive_mode import route_fixture, PLAN
    entered=threading.Event(); searched=threading.Event()
    def web(*args,**kwargs):
        assert g.current_user['id']==900001
        entered.set()
        assert searched.wait(3)
        return None
    with route_fixture(plan={**PLAN,'needs_corpus':True},passages=[P]) as (post,calls,grounding):
        def corpus(*args,**kwargs):
            assert entered.wait(3)
            searched.set()
            return [P],[{'grounding_index':1,'citation':'出处','context':SOURCE}],[],{}
        grounding.side_effect=corpus
        with patch.object(routes,'prepare_web',side_effect=web):
            result=post({'question':'新的段落流式验收','request_id':'polish-parallel-once','grounding_mode':'always'})
    assert result['ok'] and result['completion_status']=='complete', result
    assert len(calls)==2 and searched.is_set()
    assert 'first_visible_seconds' in result['timings']
