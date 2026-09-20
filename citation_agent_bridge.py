from __future__ import annotations

"""Local-only executor for redacted Agent plans.

The network Agent proposes bounded actions.  This module rejects out-of-scope or
invented arguments, executes all corpus access locally, and creates candidates only
from real Hit objects returned by the frozen selected corpus.
"""

import os
import re
import time
import copy
from collections import defaultdict
from typing import Iterable

import citation_agent_queue as agent_queue
import citation_agent_test_backend as tasks
from build_index import normalize


PROMPT_VERSION = "citation-agent-actions-v3-separated-budgets"
MAX_RECORDS = 512
MAX_PARAPHRASE_RECORDS = 64
MAX_TOOL_ACTIONS = 64
MAX_ROUNDS = 2
MAX_SECONDS = 90.0
MAX_PARAPHRASE_SOURCE_OPTIONS = 3
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_LONG_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])")
_DIRECT_CITATION_CUE_RE = re.compile(
    r"马克思|恩格斯|列宁|毛泽东|指出|写道|强调|认为|所谓|正如|按照|这就是说|用他的话说"
)
_SELECTED_SOURCE_CUE_RE = re.compile(
    r"马克思|恩格斯|列宁|毛泽东|邓小平|江泽民|胡锦涛|习近平|"
    r"《马克思恩格斯|《资本论|《共产党宣言|《反杜林论"
)
_AUXILIARY_SECTION_RE = re.compile(
    r"^(?:注释|目录|大事年表|.*索引|.*卷说明)$"
)


def _env_limit(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(os.environ.get(name, str(default)) or default), maximum))
    except (TypeError, ValueError):
        return default


def _covered(record: dict, candidates: list[dict], *, candidate_kind: str = "generate") -> bool:
    paragraph = int(record.get("paragraph_index") or 0)
    start = int(record.get("raw_start") or 0)
    end = int(record.get("raw_end") or 0)
    return any(
        (
            str(item.get("review_bucket") or "actionable") == "actionable"
            or (
                str(item.get("verification_scope") or "") == "readonly_structure"
                and bool(item.get("source_options"))
            )
        )
        and str(item.get("kind") or "") == candidate_kind
        and int(item.get("paragraph_index") or 0) == paragraph
        and int(item.get("raw_start") or 0) <= start
        and int(item.get("raw_end") or 0) >= end
        for item in candidates
    )


def _redact_fragment(text: str) -> str:
    value = str(text or "")
    for pattern in (_EMAIL_RE, _URL_RE, _PHONE_RE, _LONG_ID_RE):
        value = pattern.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()[:agent_queue.MAX_FRAGMENT_CHARS]


def _redacted_records(
    records: list[dict], candidates: list[dict], depth: str, mode: str = "both",
) -> tuple[list[dict], dict[str, dict], int]:
    chosen: list[dict] = []
    seen: set[tuple[int, int, int]] = set()
    # Existing notes that explicitly point at a selected library are the first
    # supplement target.  Only their local paper anchor is sent; the note body,
    # cited title and locator remain inside the deterministic worker.
    for candidate in candidates:
        if (
            str(candidate.get("review_bucket") or "") != "unresolved"
            or str(candidate.get("kind") or "") != "audit"
        ):
            continue
        raw = str(candidate.get("paper_text") or "").strip()
        if len(normalize(raw)) < 8:
            continue
        identity = (
            int(candidate.get("paragraph_index") or 0),
            int(candidate.get("raw_start") or 0), int(candidate.get("raw_end") or 0),
        )
        seen.add(identity)
        agent_fragment = _redact_fragment(raw)
        if len(normalize(agent_fragment)) < 8:
            continue
        locator_only = str(candidate.get("verification_scope") or "") == "locator_only"
        if locator_only and depth != "direct_and_paraphrase":
            continue
        chosen.append({
            "paragraph_index": identity[0], "section_id": str(candidate.get("section_id") or ""),
            "raw_start": identity[1], "raw_end": identity[2], "raw_text": raw,
            # A page locator without matched wording is a viewpoint lead, not a
            # direct quotation.  It enters only the independently budgeted
            # paraphrase lane; the direct lane never spends quota on it.
            "_kind": "paraphrase" if locator_only else "note_anchor",
            "_raw": raw[:agent_queue.MAX_FRAGMENT_CHARS],
            "_agent_fragment": agent_fragment,
            "_priority": 2 if locator_only else 0, "_source_candidate": dict(candidate),
        })
    for record in records:
        if str(mode or "both") == "audit":
            continue
        # A proofreading result covering the same text does not satisfy the
        # insertion lane in combined mode.  Only an existing generate result
        # may suppress a generate supplement record.
        if _covered(record, candidates, candidate_kind="generate"):
            continue
        identity = (
            int(record.get("paragraph_index") or 0),
            int(record.get("raw_start") or 0), int(record.get("raw_end") or 0),
        )
        if identity in seen:
            continue
        raw = str(record.get("raw_text") or "").strip()
        context = str(record.get("context_text") or raw)
        if record.get("quoted") and "context_text" in record and not _SELECTED_SOURCE_CUE_RE.search(context):
            continue
        # Ordinary prose is not an Agent candidate.  Viewpoint discovery starts
        # only from an existing note whose locator could not be textually
        # verified above.  This keeps a whole article's prose from consuming the
        # secondary lane and prevents generic long sentences from being treated
        # as implicit quotations.
        if not record.get("quoted"):
            continue
        minimum = 6
        if len(normalize(raw)) < minimum:
            continue
        kind = "quote"
        agent_fragment = _redact_fragment(raw)
        if len(normalize(agent_fragment)) < minimum:
            continue
        chosen.append({
            **record, "_kind": kind, "_raw": raw[:agent_queue.MAX_FRAGMENT_CHARS],
            "_agent_fragment": agent_fragment,
            "_priority": 1,
        })
    chosen.sort(key=lambda item: (
        int(item.get("_priority") or 0),
        -len(normalize(str(item.get("_raw") or ""))),
        int(item.get("paragraph_index") or 0),
    ))
    direct = [item for item in chosen if item.get("_kind") != "paraphrase"]
    paraphrase = [item for item in chosen if item.get("_kind") == "paraphrase"]
    direct_limit = _env_limit("CITATION_AGENT_MAX_RECORDS", MAX_RECORDS, 1, 2000)
    paraphrase_limit = _env_limit(
        "CITATION_AGENT_MAX_PARAPHRASE_RECORDS", MAX_PARAPHRASE_RECORDS, 1, 1000,
    )
    selected = direct[:direct_limit] + paraphrase[:paraphrase_limit]
    deferred = max(0, len(direct) - direct_limit) + max(0, len(paraphrase) - paraphrase_limit)
    redacted: list[dict] = []
    record_map: dict[str, dict] = {}
    for index, record in enumerate(selected, start=1):
        opaque = f"r{index}"
        redacted.append({
            "record_id": opaque,
            "kind": str(record.get("_kind") or "direct"),
            "fragment": str(record.get("_agent_fragment") or "")[:agent_queue.MAX_FRAGMENT_CHARS],
        })
        record_map[opaque] = record
    return redacted, record_map, deferred


def _scope(corpus, tokens: list[str]) -> tuple[dict, list[str], list[str]]:
    scope_spec, _volumes = tasks.core._selected_volumes(corpus, tokens)
    allowed_public = list(scope_spec)
    private_tokens = [
        str(value) for value in tokens
        if str(value).startswith("mylib:") or str(value).startswith("book:mylib:")
    ]
    # The model receives only per-request opaque labels; the deterministic worker
    # retains the actual ownership-bound submission tokens.
    personal = [f"p{index}" for index, _value in enumerate(private_tokens, start=1)]
    return scope_spec, allowed_public, personal


def _options(hits: Iterable) -> list[dict]:
    options: list[dict] = []
    seen: set[tuple] = set()
    for hit in list(hits)[:20]:
        option = tasks.core._option_from_hit(hit)
        section = re.sub(r"\s+", "", str(option.get("section_title") or ""))
        if section and _AUXILIARY_SECTION_RE.fullmatch(section):
            continue
        signature = tasks.core._option_signature(option)
        if signature not in seen:
            seen.add(signature)
            options.append(option)
    exact = [item for item in options if str(item.get("match_type") or "") == "exact"]
    if exact:
        options = exact
    options.sort(key=lambda option: (
        0 if str(option.get("match_type") or "") == "exact" else 1,
        -int(option.get("score") or 0),
        str(option.get("book") or ""), int(option.get("volume") or 0),
    ))
    return options


def _action_allowed(action: dict, record: dict, depth: str) -> bool:
    record_norm = normalize(str(record.get("_raw") or record.get("raw_text") or ""))
    tool = str(action.get("tool") or "")
    record_kind = str(record.get("_kind") or "direct")
    if record_kind == "paraphrase" and tool != "keyword_cooccurrence":
        return False
    if record_kind != "paraphrase" and tool == "keyword_cooccurrence":
        return False
    if tool in {"exact_fragment", "near_quote"}:
        fragment_norm = normalize(str(action.get("fragment") or ""))
        return len(fragment_norm) >= 6 and fragment_norm in record_norm
    if tool == "keyword_cooccurrence" and depth == "direct_and_paraphrase":
        keywords = [normalize(str(value)) for value in (action.get("keywords") or [])]
        # Paraphrase evidence is intentionally stricter than a general search.
        # Two broad concepts (for example “社会/生产”) routinely find a related
        # passage that does not support the proposition being cited.  Require at
        # least three concepts, all copied from the paper fragment; the local
        # executor below then requires every one of them in the same short window.
        return 3 <= len(keywords) <= 5 and all(len(value) >= 2 and value in record_norm for value in keywords)
    return False


def _execute_action(action: dict, record: dict, corpus, scope_spec: dict, style: str, depth: str) -> tuple[dict, dict | None]:
    action_id = str(action.get("action_id") or "")
    record_id = str(action.get("record_id") or "")
    if not _action_allowed(action, record, depth):
        return {
            "record_id": record_id, "action_id": action_id,
            "tool": str(action.get("tool") or ""),
            "fragment": str(action.get("fragment") or "")[:240],
            "keywords": list(action.get("keywords") or [])[:5],
            "hit_count": 0,
            "exact_count": 0, "unique_source_count": 0, "result": "rejected",
        }, None
    tool = str(action.get("tool") or "")
    try:
        if tool == "exact_fragment":
            hits = corpus.locate_quote(
                str(action.get("fragment") or ""), per_book_exact=8,
                allow_fuzzy=False, book_scope=scope_spec,
            )
        elif tool == "near_quote":
            hits = corpus.locate_quote(
                str(action.get("fragment") or ""), per_book_exact=8,
                allow_fuzzy=True, book_scope=scope_spec,
            )
        else:
            keywords = list(action.get("keywords") or [])
            hits = corpus.keyword_cooccurrence(
                keywords, window=180, min_distinct=len(keywords), occ_cap=120,
                book_scope=scope_spec, expand_synonyms=False,
            )
    except Exception:
        hits = []
    options = _options(hits)
    exact_count = sum(str(option.get("match_type") or "") == "exact" for option in options)
    unique_sources = {
        (str(option.get("source_file") or ""), tuple(option.get("printed_pages") or []))
        for option in options
    }
    result = "none"
    if len(options) > 1:
        result = "ambiguous"
    elif options:
        result = "unique_exact" if exact_count == 1 else "near"
    observation = {
        "record_id": record_id, "action_id": action_id, "hit_count": len(options),
        "tool": tool, "fragment": str(action.get("fragment") or "")[:240],
        "keywords": list(action.get("keywords") or [])[:5],
        "exact_count": exact_count, "unique_source_count": len(unique_sources), "result": result,
    }
    if not options:
        return observation, None
    if tool == "keyword_cooccurrence" and len(options) > MAX_PARAPHRASE_SOURCE_OPTIONS:
        # Broad keyword overlap is useful for planning but is not yet a useful
        # citation suggestion.  Keep the ambiguous observation for round-two
        # refinement and suppress the noisy candidate from the review screen.
        return observation, None

    paragraph_index = int(record.get("paragraph_index") or 0)
    raw_start = int(record.get("citation_start", record.get("raw_start") or 0))
    raw_end = int(record.get("citation_end", record.get("raw_end") or 0))
    record_text = str(record.get("_raw") or record.get("raw_text") or "")
    best = options[0]
    if tool == "keyword_cooccurrence":
        evidence_level = "paraphrase"
        match_type = "semantic_local"
        score = float(best.get("score") or 0)
        errors = None
    else:
        complete_exact = (
            normalize(str(action.get("fragment") or "")) == normalize(record_text)
            and len(options) == 1
            and str(best.get("match_type") or "") == "exact"
            and float(best.get("score") or 0) == 100.0
        )
        evidence_level = "exact" if complete_exact else "near_text"
        match_type = str(best.get("match_type") or "")
        score = float(best.get("score") or 0)
        errors = best.get("fuzzy_errors")
    auto = (
        evidence_level == "exact"
        and tasks.core._auto_select(match_type, score, errors, options, "conservative")
    )
    issue = "ambiguous" if len(options) > 1 else "suggest_add"
    candidate = {
        "kind": "generate", "section_id": str(record.get("section_id") or ""),
        "paragraph_index": paragraph_index, "raw_start": raw_start, "raw_end": raw_end,
        "paper_text": record_text, "match_type": match_type, "score": score,
        "fuzzy_errors": errors, "issue_code": issue,
        "issue_label": (
            "观点依据建议" if evidence_level == "paraphrase" else tasks.core.ISSUE_LABELS[issue]
        ),
        "source_options": options, "selected_option": 0,
        "proposed_citation": tasks.core._citation_for_style(best, style),
        "auto_selected": auto, "review_bucket": "actionable",
        "evidence_origin": "agent", "evidence_level": evidence_level,
    }
    source_candidate = record.get("_source_candidate")
    if isinstance(source_candidate, dict):
        candidate.update({
            "kind": "audit",
            "existing_note_kind": str(source_candidate.get("existing_note_kind") or ""),
            "existing_note_id": source_candidate.get("existing_note_id"),
            "existing_note_text": str(source_candidate.get("existing_note_text") or ""),
            "issue_code": "ambiguous" if len(options) > 1 else "format_mismatch",
            "issue_label": tasks.core.ISSUE_LABELS[
                "ambiguous" if len(options) > 1 else "format_mismatch"
            ],
        })
    return observation, candidate


def _candidate_rank(candidate: dict) -> tuple:
    level = str(candidate.get("evidence_level") or "")
    return (
        {"exact": 0, "near_text": 1, "paraphrase": 2}.get(level, 3),
        len(candidate.get("source_options") or []),
        -float(candidate.get("score") or 0),
    )


def _unresolved_candidate(record: dict) -> dict:
    return {
        "kind": "generate", "section_id": str(record.get("section_id") or ""),
        "paragraph_index": int(record.get("paragraph_index") or 0),
        "raw_start": int(record.get("citation_start", record.get("raw_start") or 0)),
        "raw_end": int(record.get("citation_end", record.get("raw_end") or 0)),
        "paper_text": str(record.get("_raw") or record.get("raw_text") or ""),
        "match_type": "", "score": 0, "fuzzy_errors": None,
        "issue_code": "unverifiable", "issue_label": "疑似属于所选文库，但尚未核实",
        "source_options": [], "selected_option": 0, "proposed_citation": "",
        "auto_selected": False, "review_bucket": "unresolved",
        "evidence_origin": "agent", "evidence_level": "",
    }


def run_agent(
    *,
    job: dict,
    extracted: dict,
    records: list[dict],
    deterministic_candidates: list[dict],
    resolved_style: str,
    corpus,
) -> tuple[list[dict], dict]:
    del extracted  # Deliberately never serialized into the Agent queue.
    started = time.monotonic()
    depth = str(job.get("recognition_depth") or "direct_only")
    if depth not in tasks.VALID_RECOGNITION_DEPTHS:
        depth = "direct_only"
    redacted, record_map, deferred_by_selection = _redacted_records(
        records, deterministic_candidates, depth, str(job.get("mode") or "both"),
    )
    model = str(os.environ.get("CITATION_AGENT_MODEL") or "deepseek-v4-flash")[:120]
    report = {
        "status": "complete", "mode": "admin_live", "model": model,
        "prompt_version": PROMPT_VERSION, "attempted_record_count": len(redacted),
        "round_count": 0, "tool_call_count": 0, "verified_match_count": 0,
        "direct_verified_match_count": 0, "viewpoint_suggestion_count": 0,
        "duration_ms": 0, "error_code": "", "out_of_scope_count": 0,
        "skipped_no_evidence_count": 0,
        "direct_status": "complete",
        "paraphrase_status": "complete" if depth == "direct_and_paraphrase" else "not_run",
        "eligible_record_count": len(redacted) + deferred_by_selection,
        "processed_record_count": 0, "deferred_record_count": deferred_by_selection,
        "completion_reason": "all_records_terminal",
    }
    if not redacted:
        report["duration_ms"] = int((time.monotonic() - started) * 1000)
        return [], report

    scope_spec, allowed_books, personal_ids = _scope(corpus, list(job.get("scope") or []))
    best_by_record: dict[str, dict] = {}
    observations_by_record: dict[str, list[dict]] = defaultdict(list)
    action_cache: dict[tuple, tuple[dict, dict | None]] = {}
    processed_ids: set[str] = set()
    lane_results: dict[str, str] = {}

    for lane_name, lane_items in (
        ("direct", [item for item in redacted if item.get("kind") != "paraphrase"]),
        ("paraphrase", [item for item in redacted if item.get("kind") == "paraphrase"]),
    ):
        if not lane_items:
            lane_results[lane_name] = (
                "complete"
                if lane_name != "paraphrase" or depth == "direct_and_paraphrase"
                else "not_run"
            )
            continue
        lane_started = time.monotonic()
        action_env = (
            "CITATION_AGENT_MAX_PARAPHRASE_TOOL_ACTIONS"
            if lane_name == "paraphrase" else "CITATION_AGENT_MAX_TOOL_ACTIONS"
        )
        timeout_env = (
            "CITATION_AGENT_PARAPHRASE_TIMEOUT_SECONDS"
            if lane_name == "paraphrase" else "CITATION_AGENT_TIMEOUT_SECONDS"
        )
        lane_max_actions = _env_limit(action_env, MAX_TOOL_ACTIONS, 1, 512)
        lane_max_seconds = float(_env_limit(timeout_env, int(MAX_SECONDS), 10, 600))
        lane_actions = 0
        lane_status = "complete"
        lane_depth = "direct_and_paraphrase" if lane_name == "paraphrase" else "direct_only"
        for round_no in range(1, MAX_ROUNDS + 1):
            report["round_count"] = max(int(report["round_count"]), round_no)
            if round_no == 1:
                retry = lane_items
            else:
                retry = [
                    item for item in lane_items
                    if str(best_by_record.get(str(item["record_id"]), {}).get("evidence_level") or "")
                    not in {"exact", "paraphrase"}
                ]
            if not retry:
                break
            round_batches = [
                retry[index:index + agent_queue.MAX_BATCH_RECORDS]
                for index in range(0, len(retry), agent_queue.MAX_BATCH_RECORDS)
            ]
            for batch_index, batch in enumerate(round_batches):
                remaining = lane_max_seconds - (time.monotonic() - lane_started)
                if remaining <= 0 or lane_actions >= lane_max_actions:
                    lane_status = "budget_exhausted"
                    break
                batch_ids = [str(item["record_id"]) for item in batch]
                observations = []
                if round_no == 2:
                    # Preserve at least the latest result for every record in the
                    # batch instead of globally truncating observations.
                    observations = [
                        observations_by_record[record_id][-1]
                        for record_id in batch_ids if observations_by_record.get(record_id)
                    ]
                request_payload = {
                    "protocol_version": 1, "prompt_version": PROMPT_VERSION, "round": round_no,
                    "planning_effort": "deep" if round_no == 1 and batch_index == 0 else "fast",
                    "recognition_depth": lane_depth, "allowed_public_books": allowed_books,
                    "personal_source_ids": personal_ids, "records": batch,
                    "observations": observations,
                }
                queue_id = ""
                try:
                    queue_id = agent_queue.enqueue(
                        request_payload, ttl_seconds=min(180, int(remaining) + 15),
                    )
                    result = agent_queue.wait_result(queue_id, timeout_seconds=remaining)
                except agent_queue.AgentQueueError:
                    lane_status = "degraded"
                    report["error_code"] = "queue_validation"
                    break
                finally:
                    if queue_id:
                        try:
                            agent_queue.delete(queue_id)
                        except Exception:
                            pass
                if result.get("status") != "complete":
                    code = str(result.get("error_code") or result.get("status") or "agent_unavailable")
                    lane_status = "budget_exhausted" if code in {"timeout", "expired"} else "degraded"
                    report["error_code"] = code[:80]
                    if code in {"invalid_json", "invalid_json_shape", "invalid_action_schema"}:
                        continue
                    break
                try:
                    clean = agent_queue.validate_response(dict(result.get("response") or {}), request_payload)
                except agent_queue.AgentQueueError:
                    lane_status = "degraded"
                    report["error_code"] = "invalid_action_schema"
                    continue
                if clean.get("actions"):
                    tasks.update_job(str(job.get("id") or ""), analysis_stage="local_verification")
                for action in clean.get("actions") or []:
                    if lane_actions >= lane_max_actions:
                        lane_status = "budget_exhausted"
                        break
                    record_id = str(action.get("record_id") or "")
                    record = record_map.get(record_id)
                    if record is None:
                        continue
                    query = normalize(str(action.get("fragment") or "")) or tuple(
                        normalize(str(value)) for value in action.get("keywords") or []
                    )
                    cache_key = (lane_name, str(action.get("tool") or ""), str(query), tuple(sorted(scope_spec)))
                    if cache_key in action_cache:
                        observation, candidate = copy.deepcopy(action_cache[cache_key])
                        observation["record_id"] = record_id
                        observation["action_id"] = str(action.get("action_id") or "")
                        if candidate is not None:
                            candidate.update({
                                "paragraph_index": int(record.get("paragraph_index") or 0),
                                "raw_start": int(record.get("citation_start", record.get("raw_start") or 0)),
                                "raw_end": int(record.get("citation_end", record.get("raw_end") or 0)),
                                "paper_text": str(record.get("_raw") or record.get("raw_text") or ""),
                            })
                            source_candidate = record.get("_source_candidate")
                            if isinstance(source_candidate, dict):
                                candidate.update({
                                    "kind": "audit",
                                    "existing_note_kind": str(source_candidate.get("existing_note_kind") or ""),
                                    "existing_note_id": source_candidate.get("existing_note_id"),
                                    "existing_note_text": str(source_candidate.get("existing_note_text") or ""),
                                })
                    else:
                        observation, candidate = _execute_action(
                            action, record, corpus, scope_spec, resolved_style, lane_depth,
                        )
                        action_cache[cache_key] = copy.deepcopy((observation, candidate))
                        lane_actions += 1
                        report["tool_call_count"] = int(report["tool_call_count"]) + 1
                    processed_ids.add(record_id)
                    observations_by_record[record_id].append(observation)
                    if candidate is not None:
                        previous = best_by_record.get(record_id)
                        if previous is None or _candidate_rank(candidate) < _candidate_rank(previous):
                            best_by_record[record_id] = candidate
                if lane_status != "complete":
                    break
            if lane_status != "complete":
                break
        lane_results[lane_name] = lane_status

    additions = list(best_by_record.values())
    report["verified_match_count"] = len(additions)
    report["direct_verified_match_count"] = sum(
        str(item.get("evidence_level") or "") in {"exact", "near_text"}
        for item in additions
    )
    report["viewpoint_suggestion_count"] = sum(
        str(item.get("evidence_level") or "") == "paraphrase"
        for item in additions
    )
    # Only an existing note/reference-field record with selected-corpus
    # provenance may remain visible without a match.  Unattributed and clearly
    # external quotations are counted, never labelled as selected-library text.
    for record_id, record in record_map.items():
        if record_id in best_by_record:
            continue
        report["skipped_no_evidence_count"] = int(report["skipped_no_evidence_count"]) + 1
    report["direct_status"] = lane_results.get("direct", "complete")
    report["paraphrase_status"] = lane_results.get("paraphrase", "not_run")
    exhausted = "budget_exhausted" in lane_results.values()
    degraded = "degraded" in lane_results.values()
    if exhausted:
        report["status"] = "budget_exhausted"
        report["error_code"] = report.get("error_code") or "budget_exhausted"
        report["completion_reason"] = "tool_or_time_budget_exhausted"
    elif degraded:
        report["status"] = "degraded"
        report["completion_reason"] = "agent_lane_degraded"
    report["processed_record_count"] = (
        len(redacted) if not exhausted and not degraded else len(processed_ids)
    )
    report["deferred_record_count"] = deferred_by_selection + (
        0 if not exhausted and not degraded else max(0, len(redacted) - len(processed_ids))
    )
    report["duration_ms"] = int((time.monotonic() - started) * 1000)
    return additions, report


def make_callback(corpus):
    def callback(**kwargs):
        return run_agent(corpus=corpus, **kwargs)
    return callback
