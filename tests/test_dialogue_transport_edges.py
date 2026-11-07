import contextlib
import io
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import ai


def exercise(response, *, cancel=None):
    client = object.__new__(ai.ZAIClient)
    client.config = SimpleNamespace(temperature=.2, request_timeout_seconds=30)
    route = dict(provider='deepseek',base_url='https://example.test',api_key='fixture',auth_header='Authorization',label='fixture')
    events, meta = [], {'section_control':True}
    with patch.object(client,'_ensure_enabled'), patch.object(client,'_route',return_value=route), patch.object(ai,'_ai_http_slot',return_value=contextlib.nullcontext()), patch.object(ai.urllib_request,'urlopen',return_value=response), patch.object(ai,'_emit_ai_call_event',side_effect=lambda e:events.append(e)):
        try:
            content = ''.join(client.chat_complete_stream([{'role':'user','content':'q'}],100,provider='deepseek',model='deepseek-v4-flash',reasoning_effort='off',meta_out=meta,should_cancel=cancel))
        except ai.AIServiceError:
            content = ''
    return content,meta,events


def event(value):
    return ('data: '+json.dumps(value)+'\n').encode()


def test_actual_usage_tail_after_marker_is_recorded_once_and_text_suppressed():
    response = io.BytesIO(event({'choices':[{'delta':{'content':'Body.<section_complete/>'}}]})+
                          event({'choices':[{'delta':{'content':'repeat body'},'finish_reason':'stop'}]})+
                          event({'usage':{'prompt_tokens':11,'completion_tokens':17}})+b'data: [DONE]\n')
    content,meta,events = exercise(response)
    assert content == 'Body.<section_complete/>'
    assert meta['usage']['completion_tokens'] == 17 and not meta.get('usage_estimated')
    assert len(events) == 2 and events[-1]['completion_tokens'] == 17


class BlockedResponse:
    def __init__(self, marker):
        self.marker=marker
        self.closed=threading.Event()
        self.fp=SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(shutdown=lambda *_:self.closed.set())))
    def __enter__(self): return self
    def __exit__(self,*_): self.close()
    def close(self): self.closed.set()
    def __iter__(self):
        yield event({'choices':[{'delta':{'content':self.marker}}]})
        assert self.closed.wait(7), 'socket watchdog failed to unblock read'
        raise OSError('connection closed by watchdog')


def test_usage_tail_wait_is_bounded_and_estimation_is_explicit():
    start=time.monotonic()
    content,meta,events=exercise(BlockedResponse('Done.<section_complete/>'))
    assert 4.5 < time.monotonic()-start < 7
    assert content == 'Done.<section_complete/>' and meta['finish_reason']=='section_complete'
    assert events[-1]['error']=='usage_estimated' and events[-1]['completion_tokens'] > 0
    assert len(events)==2


def test_cancellation_unblocks_silent_upstream():
    cancelled=threading.Event()
    timer=threading.Timer(.2,cancelled.set);timer.start()
    try:
        _,meta,events=exercise(BlockedResponse('Partial.'),cancel=cancelled.is_set)
    finally:
        timer.cancel()
    assert meta['finish_reason']=='cancelled' and len(events)==2
