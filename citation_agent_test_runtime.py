from __future__ import annotations

"""Minimal deterministic runtime for the isolated administrator test worker.

Unlike app.py this module does not initialize Flask, the public citation task database,
AI clients, payment services, mail, journals, or web background threads.
"""

import json
from hashlib import sha256

from admin_store import get_setting, init_admin_store_db
from build_index import DB_PATH as CORPUS_INDEX_DB_PATH
from citation_styles import CITATION_FORMAT_KEYS
import citation_agent_test_backend as tasks
import citation_quote_evidence
import personal_corpus
import personal_library
from runtime_env import compute_sha256
from search import Corpus, DEFAULT_CITATION_TEMPLATES


_FORMAT_KEYS = CITATION_FORMAT_KEYS
_FORMAT_MAX_LENGTH = 240


def _citation_formats() -> dict[str, str]:
    raw = get_setting("citation_formats", {})
    raw = raw if isinstance(raw, dict) else {}
    output: dict[str, str] = {}
    for key in _FORMAT_KEYS:
        value = raw.get(key)
        if isinstance(value, dict):
            value = value.get("template")
        template = str(value or "").strip()[:_FORMAT_MAX_LENGTH]
        if template and template != str(DEFAULT_CITATION_TEMPLATES.get(key) or "").strip():
            output[key] = template
    return output


def load_corpus() -> Corpus:
    init_admin_store_db()
    loaded = Corpus.load_default()
    loaded.set_citation_templates(_citation_formats())
    return loaded


def corpus_sha256() -> str:
    if not CORPUS_INDEX_DB_PATH.is_file():
        raise tasks.CitationAssistantError("引文语料库尚未就绪。")
    return compute_sha256(CORPUS_INDEX_DB_PATH)


def template_version() -> str:
    payload = {
        "schema": 2,
        "defaults": DEFAULT_CITATION_TEMPLATES,
        "overrides": _citation_formats(),
        "gb2025_approved": bool(get_setting("citation_gb2025_approved", False)),
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _personal_scope_rows(user_id: int, tokens: list[str]) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    for token in tokens:
        submission_id = personal_corpus.submission_id_from_scope_token(token)
        if not submission_id or submission_id in rows:
            continue
        row = personal_library.get_submission(submission_id, int(user_id))
        if not row or row.get("status") != "ready" or not row.get("searchable"):
            raise tasks.CitationAssistantError("所选个人文库书籍已不可用，请重新选择范围。")
        if int(row.get("derivative_version") or 0) != int(personal_corpus.INDEX_PIPELINE_VERSION):
            raise tasks.CitationAssistantError("所选个人文库索引版本已变更，请等待重建后重试。")
        rows[int(submission_id)] = row
    return rows


def personal_callback(job: dict):
    user_id = int(job["user_id"])
    rows = _personal_scope_rows(user_id, list(job.get("scope") or []))
    if not rows:
        return None
    corpus = personal_corpus.get_personal_corpus(user_id)
    if corpus is None:
        raise tasks.CitationAssistantError("个人文库索引暂不可用。")
    wanted = {personal_corpus.personal_book_key(submission_id) for submission_id in rows}

    def callback(records: list[dict], _tokens: list[str], style: str) -> list[dict]:
        results: list[dict] = []
        selected_volumes = [vol for book in wanted for vol in corpus.get_volumes(book)]
        mixed = any(not token.startswith(("mylib:", "book:mylib:")) for token in _tokens)
        verified = citation_quote_evidence.verify_many(
            corpus, selected_volumes, [r["raw_text"] for r in records if r.get("quoted")],
        )
        for record in records:
            raw = str(record.get("raw_text") or "").strip()
            if not raw:
                continue
            if record.get("quoted"):
                found = verified.get(raw) or []
            else:
                found = [
                    tasks.core._option_from_hit(hit)
                    for hit in corpus.locate_quote(raw, per_book_exact=8, allow_fuzzy=False, book_scope=wanted)
                ]
            options = []
            for option in found:
                submission_id = personal_corpus.submission_id_from_key(option["book"])
                source_row = rows.get(submission_id)
                if not source_row:
                    continue
                confidence = float(personal_library.submission_quality(source_row).get("page_confidence") or 0)
                option.update({
                    "private_source": True, "personal": True, "page_confidence": confidence,
                    "submission_id": submission_id,
                    "display_title": str(source_row.get("title") or option.get("display_title") or "个人文库"),
                    "viewer_url": f"/mylib/{submission_id}",
                })
                if mixed and option.get("quote_evidence"):
                    option["quote_evidence"]["complete_search"] = False
                options.append(option)
            if not options:
                continue
            best = options[0]
            match_type = str(best.get("match_type") or "exact")
            score = float(best.get("score") or 0)
            errors = best.get("fuzzy_errors")
            issue = "ambiguous" if len(options) > 1 else "suggest_add"
            results.append({
                "kind": "generate", "section_id": str(record.get("section_id") or ""),
                "paragraph_index": int(record.get("paragraph_index") or 0),
                "raw_start": int(record.get("citation_start", record.get("raw_start")) or 0),
                "raw_end": int(record.get("citation_end", record.get("raw_end")) or 0), "paper_text": raw,
                "match_type": match_type, "score": score, "fuzzy_errors": errors,
                "issue_code": issue, "issue_label": tasks.core.ISSUE_LABELS[issue],
                "source_options": options, "selected_option": 0,
                "proposed_citation": tasks.core._citation_for_style(best, style),
                "auto_selected": tasks.core._auto_select(
                    match_type, score, errors, options, "conservative",
                ),
            })
        return results

    return callback
