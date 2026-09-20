from __future__ import annotations

import json
from pathlib import Path

import citation_agent_shadow as shadow
import citation_assistant as ca
import pytest


class _Volume:
    book = "test"
    volume = 1
    source_file = "source.pdf"
    norm_full = ""


class _Corpus:
    books = {"test": [_Volume()]}

    @staticmethod
    def _make_hit(volume, start, end, match_type, score, paper_text, fuzzy_errors=None):
        return {
            "book": volume.book,
            "volume": volume.volume,
            "source_file": volume.source_file,
            "display_title": "测试文集",
            "citation_title": "测试文集",
            "printed_pages": ["12"],
            "pdf_pages": [1],
            "match_type": match_type,
            "score": score,
            "fuzzy_errors": fuzzy_errors,
            "citation": "测试引文，第12页。",
            "citations": {"mkszyj": "测试引文，第12页。"},
            "context": paper_text,
        }


def _record(text: str) -> dict:
    return {
        "paragraph_index": 3,
        "section_id": "body",
        "raw_start": 2,
        "raw_end": 2 + len(text),
        "norm_start": 0,
        "norm_end": len(text),
        "norm": text,
        "raw_text": text,
        "quoted": True,
    }


def test_v4_pro_shadow_only_counts_locally_verified_incremental_matches() -> None:
    source = "".join(chr(0x4E00 + index) for index in range(80))
    paper_chars = list(source)
    for index in (20, 21, 22, 23):
        paper_chars[index] = chr(0x5200 + index)
    paper = "".join(paper_chars)
    _Volume.norm_full = "前置文字" + source + "后置文字"
    record = _record(paper)
    captured: dict[str, object] = {}

    def complete(messages: list[dict[str, str]], max_tokens: int) -> str:
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        return json.dumps({
            "plans": [{
                "id": "r0",
                "searches": [{
                    "paper_fragment": paper[20:25],
                    "source_fragment": source[20:25],
                    "book_keys": ["test", "invented"],
                }],
            }],
        }, ensure_ascii=False)

    report = shadow.run_shadow(
        [record], [], _Corpus(), ["book:test"], complete=complete,
    )

    assert report["status"] == "complete"
    assert report["model"] == "deepseek-v4-pro"
    assert report["planned_record_count"] == 1
    assert report["search_count"] == 1
    assert report["verified_match_count"] == 1
    assert report["incremental_record_count"] == 1
    assert report["ambiguous_record_count"] == 0
    assert report["reliable_record_count"] == 1
    assert report["response_sha256"]
    assert captured["max_tokens"] == 1000
    assert paper in str(captured["messages"])
    assert "invented" not in str(report)


def test_shadow_rejects_fragments_not_copied_from_paper_and_never_raises() -> None:
    record = _record("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉")

    report = shadow.run_shadow(
        [record], [], _Corpus(), ["book:test"],
        complete=lambda _messages, _max_tokens: json.dumps({
            "plans": [{"id": "r0", "searches": [{
                "paper_fragment": "并不存在于论文",
                "source_fragment": "甲乙丙丁",
                "book_keys": ["test"],
            }]}],
        }, ensure_ascii=False),
    )
    assert report["status"] == "complete"
    assert report["search_count"] == 0
    assert report["incremental_record_count"] == 0

    failed = shadow.run_shadow(
        [record], [], _Corpus(), ["book:test"],
        complete=lambda _messages, _max_tokens: (_ for _ in ()).throw(TimeoutError("secret text")),
    )
    assert failed["status"] == "error"
    assert failed["error_code"] == "TimeoutError"
    assert "secret" not in str(failed)


def test_shadow_rejects_a_wholly_out_of_scope_book_request() -> None:
    record = _record("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉")
    report = shadow.run_shadow(
        [record], [], _Corpus(), ["book:test"],
        complete=lambda _messages, _max_tokens: json.dumps({
            "plans": [{"id": "r0", "searches": [{
                "paper_fragment": "甲乙丙丁",
                "source_fragment": "甲乙丙丁",
                "book_keys": ["invented"],
            }]}],
        }, ensure_ascii=False),
    )
    assert report["status"] == "complete"
    assert report["search_count"] == 0
    assert report["verified_match_count"] == 0


def test_covered_records_skip_agent_call() -> None:
    record = _record("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉")
    called = False

    def complete(_messages: list[dict[str, str]], _max_tokens: int) -> str:
        nonlocal called
        called = True
        return "{}"

    report = shadow.run_shadow(
        [record], [{
            "kind": "generate",
            "paragraph_index": record["paragraph_index"],
            "raw_start": record["raw_start"] - 1,
            "raw_end": record["raw_end"] + 1,
        }], _Corpus(), ["book:test"], complete=complete,
    )
    assert report["status"] == "skipped"
    assert report["attempted_record_count"] == 0
    assert called is False


def test_shadow_store_contains_aggregates_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ca, "DB_PATH", tmp_path / "citation.sqlite3")
    monkeypatch.setattr(ca, "ARTIFACT_ROOT", tmp_path / "artifacts")
    ca.init_db()
    # A foreign-keyed run cannot exist without a real task.
    with pytest.raises(Exception):
        ca.save_agent_shadow_run("missing-job", {
            "status": "complete", "model": shadow.MODEL,
            "prompt_version": shadow.PROMPT_VERSION,
        })

    source = tmp_path / "paper.docx"
    from docx import Document
    document = Document()
    document.add_paragraph("正文")
    document.save(source)
    job = ca.create_job(9, source.name, source.read_bytes())
    ca.save_agent_shadow_run(job["id"], {
        "status": "complete", "model": shadow.MODEL,
        "prompt_version": shadow.PROMPT_VERSION,
        "attempted_record_count": 2, "planned_record_count": 1,
        "search_count": 2, "verified_match_count": 1,
        "incremental_record_count": 1, "ambiguous_record_count": 0,
        "reliable_record_count": 1, "duration_ms": 25,
        "response_sha256": "a" * 64, "error_code": "",
        "paper_text": "must not persist",
    })
    stored = ca.get_agent_shadow_run(job["id"])
    assert stored and stored["model"] == shadow.MODEL
    assert stored["incremental_record_count"] == 1
    assert "paper_text" not in stored
    assert "must not persist" not in str(stored)
    assert ca.delete_job(job["id"], 9) is True
    assert ca.get_agent_shadow_run(job["id"]) is None


def test_web_shadow_is_admin_only_and_forces_v4_pro(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as web

    monkeypatch.setattr(web, "CITATION_AGENT_SHADOW_ENABLED", False)
    assert web._citation_agent_shadow_callback({"id": "off", "user_id": 1}) is None

    monkeypatch.setattr(web, "CITATION_AGENT_SHADOW_ENABLED", True)
    monkeypatch.setattr(web, "get_user_by_id", lambda _user_id: {"id": 2, "role": "member"})
    assert web._citation_agent_shadow_callback({"id": "member", "user_id": 2}) is None

    monkeypatch.setattr(web, "get_user_by_id", lambda _user_id: {"id": 1, "role": "admin"})
    monkeypatch.setattr(web, "_refresh_ai_runtime_if_needed", lambda: None)
    captured: dict[str, object] = {}

    class Client:
        @staticmethod
        def chat_complete(messages, max_tokens, **kwargs):
            captured.update({"messages": messages, "max_tokens": max_tokens, **kwargs})
            return "{}"

    def fake_run_shadow(records, candidates, corpus, tokens, *, complete):
        complete([], 123)
        return {
            "status": "complete", "model": shadow.MODEL,
            "attempted_record_count": 0, "planned_record_count": 0,
            "verified_match_count": 0, "incremental_record_count": 0,
        }

    monkeypatch.setattr(web, "AI_CLIENT", Client())
    monkeypatch.setattr(web.citation_agent_shadow, "run_shadow", fake_run_shadow)
    monkeypatch.setattr(web.citation_tasks, "save_agent_shadow_run", lambda *_args: None)
    callback = web._citation_agent_shadow_callback({"id": "admin", "user_id": 1})
    assert callback is not None
    callback([], [], [])
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["temperature"] == 0
    assert captured["http_timeout"] == web.CITATION_AGENT_SHADOW_TIMEOUT_SECONDS
    assert captured["disable_thinking"] is True
