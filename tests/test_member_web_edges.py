import json
import time
from unittest.mock import patch
import pytest
from flask import g
import app as A
import ai
import ai_comprehensive as task
import comprehensive_web as W
import membership as M
from test_member_web_20261112 import member, FLASH


@pytest.mark.parametrize("phase", ["writing", "verifying", "checking"])
def test_partial_generation_retains_body_and_specific_cause(phase):
    state={}
    def call(messages, budget):
        if "只检查原始要求" in messages[0]["content"]:
            raise json.JSONDecodeError("bad", "", 0)
        return "新增正文。"
    def stream(messages, budget, meta_out=None):
        yield "已经核验的段落。\n\n"
        if phase=="writing":
            raise ai.AIServiceError("AI 使用额度余额不足")
        yield "第二个段落。"
        meta_out["finish_reason"]="stop"
    def verify(text):
        if phase=="verifying" and "第二个" in text:
            raise ValueError("invalid evidence")
        return text
    result=task.generate("问题",[],{"complex":True,"sections":["正文"]},[],call,
        deadline=time.monotonic()+30,cancelled=lambda:False,checkpoint=lambda *args:None,
        verify=verify,stream=stream,generation_state=state)
    assert result[2]=="partial" and "已经核验" in result[0]
    expected={"writing":"wallet_quota","verifying":"verification_error","checking":"structured_output"}
    assert state["stop_reason"]==expected[phase]
    assert state["recoverable"] is (phase!="writing")


def test_auto_quota_exhaustion_and_explicit_web_request():
    user=member()
    with A.app.test_request_context('/'):
        g.current_user=user
        with patch.object(W,"web_quota",return_value={"allowed":False,"message":"今日联网次数已用完"}), \
             patch.object(W,"web_allowed",return_value=True),patch.object(A.AI_CLIENT,"_route") as network:
            web=W.prepare_web(A,{"question":"最新研究","web_mode":"auto"},FLASH)
            assert web.warnings==["今日联网次数已用完"]
            with pytest.raises(ai.AIServiceError,match="次数已用完"):
                W.prepare_web(A,{"question":"最新研究","web_mode":"always"},FLASH)
            network.assert_not_called()


def test_structured_response_exposes_upstream_finish_reason():
    metadata={}
    with patch.object(A.AI_CLIENT,"_ensure_enabled"), patch.object(A.AI_CLIENT,"_post_json",return_value={
        "choices":[{"message":{"content":"{broken"},"finish_reason":"length"}]}):
        result=A.AI_CLIENT.chat_complete([{"role":"user","content":"JSON"}],max_tokens=256,
            provider="deepseek",model="deepseek-v4-flash",reasoning_effort="off",meta_out=metadata)
    assert result=="{broken" and metadata["finish_reason"]=="length"
