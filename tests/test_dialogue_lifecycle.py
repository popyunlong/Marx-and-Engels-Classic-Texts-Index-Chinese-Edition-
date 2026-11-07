import json
import time
from unittest.mock import patch

import ai_comprehensive as tasks
from ai_generation_protocol import section_end, MARKER


def run(chunks, reasons=('stop',), complex=False, checks=()):
    writes, points, state, prompts = [], [], {}, []
    replies = iter(checks)
    def stream(messages, budget, meta_out=None):
        i = len(writes)
        writes.append(i)
        assert i < len(chunks), 'unexpected automatic rewrite'
        meta_out['finish_reason'] = reasons[min(i, len(reasons)-1)]
        yield from chunks[i]
    def call(messages, budget):
        prompts.append(messages)
        return json.dumps(next(replies))
    result = tasks.generate('explain and compare', [], {'complex': complex, 'sections': ['explain'], 'requirements': ['compare']}, [], call,
                            deadline=time.monotonic()+10, cancelled=lambda: False, checkpoint=lambda *a: points.append(a),
                            verify=lambda t:t, stream=stream, generation_state=state)
    return result, writes, points, state, prompts


def test_natural_stop_without_private_marker_does_not_restart():
    result, writes, *_ = run([['Original argument.\n\nConclusion.']])
    assert result[2] == 'complete' and writes == [0]


def test_private_marker_discards_all_following_tokens_and_handles_split():
    result, writes, *_ = run([['Argument.\n\nConclusion.<section_', 'complete/>\n\nArgument.', 'Argument.']])
    assert result[0] == 'Argument.\n\nConclusion.' and writes == [0]


def test_marker_inside_code_quote_and_blockquote_is_literal():
    for text in [f'`{MARKER}`', f'"{MARKER}"', f'```text\n{MARKER}\n```', f'> {MARKER}\n']:
        assert section_end(text) == -1
        result, *_ = run([[text+'\n\nFinal.']])
        assert MARKER in result[0] and result[2] == 'complete'


def test_only_length_truncation_can_continue_once():
    result, writes, *_ = run([['First sentence\n\n'], ['Second sentence.']], reasons=('length','stop'))
    assert result[2] == 'complete' and len(writes) == 2
    assert result[0].count('First sentence') == 1


def test_second_truncation_or_unknown_eof_stops():
    result, writes, *_ = run([['First.\n\n'], ['Second.']], reasons=('length','length'))
    assert result[2] == 'partial' and len(writes) == 2
    result, writes, *_ = run([['Content.']], reasons=('unknown',))
    assert result[2] == 'partial' and len(writes) == 1


GAP = {'complete':False, 'gaps':[{'requirement_id':'r1','existing_coverage':'explanation only','missing':'comparison','addition':'compare differences'}]}


def test_semantically_repeated_supplement_never_reaches_checkpoint():
    result, writes, points, state, checks = run([['Original explanation.'], ['Same explanation in other words.']], complex=True,
        checks=[GAP, {'complete':False,'paragraphs':[{'index':0,'requirement_id':'r1','novel':False,'new_information':''}]}])
    assert result[0] == 'Original explanation.' and result[2] == 'partial'
    assert all('other words' not in p[0] for p in points)
    assert len(writes) == 2 and len(checks) == 2 and state['supplement_accepted'] == 0


def test_genuine_supplement_published_only_after_final_check():
    result, writes, points, state, _ = run([['Explanation.'], ['New comparison.']], complex=True,
        checks=[GAP, {'complete':True,'paragraphs':[{'index':0,'requirement_id':'r1','novel':True,'new_information':'different conditions'}]}])
    assert result[2] == 'complete' and result[0] == 'Explanation.\n\nNew comparison.'
    assert state['supplement_accepted'] == 1


def test_checker_cannot_expand_unrecognized_requirements():
    result, writes, *_ = run([['Explanation.']], complex=True, checks=[{'complete':False,'remaining':['write everything again']}])
    assert writes == [0] and result[2] == 'partial'


def test_repeated_narrative_stops_but_shared_quotation_is_not_removed():
    text = 'This is an already established argument with enough detail to compare reliably.'
    result, *_ = run([[text+'\n\n'+text]])
    assert result[0] == text and result[2] == 'partial'
    text = '“'+text+'”'
    result, *_ = run([[text+'\n\n'+text]])
    assert result[0].count(text) == 2 and result[2] == 'complete'


def test_provider_metadata_and_missing_usage_estimate():
    import io
    import contextlib
    import ai
    from types import SimpleNamespace
    client=object.__new__(ai.ZAIClient)
    client.config=SimpleNamespace(temperature=.2,request_timeout_seconds=30)
    route={'provider':'deepseek','base_url':'https://example.test','api_key':'fixture','auth_header':'Authorization','label':'fixture'}
    events=[]
    body=('data: '+json.dumps({'choices':[{'delta':{'content':'Some output'},'finish_reason':'stop'}]})+'\ndata: [DONE]\n').encode()
    meta={}
    with patch.object(client,'_ensure_enabled'),patch.object(client,'_route',return_value=route),patch.object(ai,'_ai_http_slot',return_value=contextlib.nullcontext()),patch.object(ai.urllib_request,'urlopen',return_value=io.BytesIO(body)),patch.object(ai,'_emit_ai_call_event',side_effect=lambda e:events.append(e)):
        assert ''.join(client.chat_complete_stream([{'role':'user','content':'question'}],100,provider='deepseek',model='deepseek-v4-flash',reasoning_effort='off',meta_out=meta)) == 'Some output'
    assert meta['finish_reason']=='stop' and meta['usage_estimated']
    assert events[-1]['completion_tokens'] > 0 and len(events)==2
