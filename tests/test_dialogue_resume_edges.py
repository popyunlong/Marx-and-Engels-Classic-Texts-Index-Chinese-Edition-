import json
import time
import pytest
from werkzeug.exceptions import BadRequest

import ai_comprehensive as tasks
from test_comprehensive_mode import PLAN, route_fixture


def test_explicit_resume_after_rejected_supplement_keeps_section_progress():
    state={'supplement_attempted':True,'sections':[{'id':'s1','title':'analysis','calls':1}]}
    replies=iter([
        {'complete':False,'gaps':[{'requirement_id':'r1','existing_coverage':'analysis','missing':'comparison','addition':'different conditions'}]},
        {'complete':True,'paragraphs':[{'index':0,'requirement_id':'r1','novel':True,'new_information':'different conditions'}]},
    ])
    writes=[]
    def stream(messages,budget,meta_out=None):
        writes.append(messages)
        meta_out['finish_reason']='stop'
        yield 'New comparison.'
    result=tasks.generate('analyze and compare',[],{'complex':True,'sections':['analysis'],'requirements':['comparison']},[],
        lambda *_:json.dumps(next(replies)),deadline=time.monotonic()+10,cancelled=lambda:False,checkpoint=lambda *_:None,
        verify=lambda t:t,previous='Accepted analysis.',completed_sections=1,generation_state=state,stream=stream)
    assert result[0]=='Accepted analysis.\n\nNew comparison.' and result[2]=='complete'
    assert len(writes)==1 and state['sections'][0]['calls']==1


def test_finished_parent_result_cannot_bypass_continuation_scope_check():
    with route_fixture(PLAN) as (post,calls,_):
        result=post({'question':'q','request_id':'scope-parent'})
        assert result['completion_status']=='complete'
        count=len(calls)
        with pytest.raises(BadRequest, match='保持原检索范围'):
            post({'question':'q','request_id':'scope-child','continue_request_id':'scope-parent','scope':'all'})
        assert len(calls)==count
