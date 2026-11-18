"""Regression cases for the isolated model queue and AI evidence review."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

import citation_agent_bridge as bridge
import citation_agent_queue as queue
from citation_quote_evidence import compare_text

BOOK = "一部选集（第2版）：著作"
QUOTE = "有人在漫长的实践过程中认识周围世界变化发展的规律"
SOURCE = QUOTE.replace("漫长", "慢长")


def _job():
    evidence = compare_text(QUOTE, SOURCE)
    evidence["complete_search"] = True
    option = {"book": BOOK, "volume": 1, "quote_evidence": evidence, "score": 96}
    candidate = {"kind": "generate", "paper_text": QUOTE, "source_options": [option],
                 "paragraph_index": 0, "section_id": "body", "raw_start": 0,
                 "raw_end": len(QUOTE), "evidence_level": "near_text"}
    books = {BOOK: [], **{f"选集（{i}）": [] for i in range(499)}}
    return dict(job={"id": "v2", "scope": [f"book:{b}" for b in books]},
                extracted={}, records=[], deterministic_candidates=[candidate],
                resolved_style="mkszyj", corpus=SimpleNamespace(books=books))


def test_review_with_500_public_titles_binds_only_supplied_candidate_and_resumes(monkeypatch):
    requests, saves = [], []
    def exchange(payload, remaining, check_active):
        request = queue.validate_request(payload)
        requests.append(request)
        assert request["protocol_version"] == 2
        assert len(request["allowed_public_books"]) == 500
        assert all(b.startswith("b") and b[1:].isdigit() for b in request["allowed_public_books"])
        record = request["records"][0]
        proof = record["evidence"][0]
        assert proof["quotation"] == QUOTE and proof["excerpt"] == SOURCE
        verdict = {"record_id": record["record_id"], "candidate_id": proof["candidate_id"],
                   "judgment": "minor_difference"}
        with pytest.raises(queue.AgentQueueError):
            queue.validate_response({"actions": [], "verdicts": [{**verdict, "candidate_id": "c99"}]}, request)
        return {"status": "complete", "response": {"actions": [], "verdicts": [verdict]}}

    monkeypatch.setattr(bridge, "_queue_exchange", exchange)
    inputs = _job()
    additions, report = bridge.run_agent(**inputs, on_checkpoint=lambda state, adds: saves.append(deepcopy(state)))
    assert report["processed_record_count"] == 1 and report["deferred_record_count"] == 0
    assert additions[0]["source_options"][0]["quote_evidence"]["ai_confirmed"]
    resumed, _ = bridge.run_agent(**inputs, checkpoint=saves[-1])
    assert resumed == additions and len(requests) == 1


def test_long_quote_tail_review_excludes_private_options_and_secrets():
    paper = "群众的实践经验" * 80 + QUOTE
    original = "群众的实践经验" * 80 + SOURCE
    proof = compare_text(paper, original)
    public = {"book": BOOK, "volume": 2, "quote_evidence": proof}
    options = [{"private_source": True, **public}, public, {**public, "volume": 3}]
    candidate = {"source_options": options}
    sent = bridge._evidence_packet(candidate, paper, {BOOK: {2}})
    assert len(sent) == 1 and sent[0]["candidate_id"] == "c2"
    assert len(sent[0]["quotation"]) < 320 and "慢长" in sent[0]["excerpt"]
    assert len(bridge._redact_fragment(paper)) == 320
    assert not bridge._evidence_packet(candidate, paper[:320], {BOOK: {2}})
    assert bridge._scope(SimpleNamespace(books={BOOK: []}), ["mylib:45"])[0] == {}


def test_legacy_queue_rejects_new_evidence_and_unsupported_protocol():
    r = {"record_id": "r1", "kind": "quote", "fragment": QUOTE}
    payload = {"records": [r], "allowed_public_books": ["文集"]}
    assert queue.validate_request(payload)["protocol_version"] == 1
    with pytest.raises(queue.AgentQueueError):
        queue.validate_request({**payload, "records": [{**r, "evidence": []}]})
    with pytest.raises(queue.AgentQueueError):
        queue.validate_request({**payload, "protocol_version": 3})


def test_model_ocr_variant_must_stay_close_to_paper():
    record = {"_raw": QUOTE, "_agent_fragment": QUOTE}
    assert bridge._action_allowed({"tool": "near_quote", "fragment": SOURCE}, record, "direct_only")
    assert not bridge._action_allowed({"tool": "near_quote", "fragment": "完全无关的另一段文章讨论不同的历史人物"},
                                      record, "direct_only")
