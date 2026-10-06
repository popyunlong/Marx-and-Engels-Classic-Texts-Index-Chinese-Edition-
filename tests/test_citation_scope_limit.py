"""Scope limits must reject oversized requests rather than silently changing them."""
import io
import json

import pytest

import citation_agent_test_backend as tasks
from test_citation_agent_web_access import _test_app


def test_scope_limit_preserves_exact_selection_and_deduplicates():
    tokens = [f"book:fixture-{n}" for n in range(500)]
    assert tasks._required_scope_tokens(tokens + tokens) == tokens
    with pytest.raises(tasks.CitationAssistantError, match="500"):
        tasks._required_scope_tokens(tokens + ["book:extra"])
    with pytest.raises(tasks.CitationAssistantError):
        tasks._required_scope_tokens([])


@pytest.mark.parametrize("stage", ["upload", "analyze"])
def test_http_scope_limit_rejects_before_processing(monkeypatch, stage):
    app = _test_app(monkeypatch)
    monkeypatch.setattr(tasks, "get_job", lambda *_args: {"id": "fixture"})
    def unexpected(*_args, **_kwargs):
        pytest.fail("An oversized selection must not create or start a job")
    monkeypatch.setattr(tasks, "create_job", unexpected)
    monkeypatch.setattr(tasks, "set_analysis_config", unexpected)
    scope = [f"book:fixture-{n}" for n in range(501)]
    client = app.test_client()
    if stage == "upload":
        response = client.post("/api/citation-agent/jobs", headers={"X-Test-User": "admin"},
            data={"file": (io.BytesIO(b"fixture"), "test.docx"), "scope": json.dumps(scope)})
    else:
        response = client.post("/api/citation-agent/jobs/fixture/analyze", headers={"X-Test-User": "admin"},
            json={"sections": ["body"], "scope": scope})
    assert response.status_code == 400
    assert "500" in response.get_data(as_text=True)
