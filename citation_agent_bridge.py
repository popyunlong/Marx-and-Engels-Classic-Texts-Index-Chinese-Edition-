from __future__ import annotations

"""Local-only executor for redacted Agent plans.

The network Agent proposes bounded actions.  This module rejects out-of-scope or
invented arguments, executes all corpus access locally, and creates candidates only
from real Hit objects returned by the frozen selected corpus.
"""

import hashlib
import json
import os
import re
import time
import copy
from collections import defaultdict
from typing import Iterable

from rapidfuzz.distance import Levenshtein

import citation_agent_queue as agent_queue
import citation_agent_test_backend as tasks
from build_index import normalize


PROMPT_VERSION = "citation-agent-evidence-v2"
MAX_RECORDS = 512
MAX_PARAPHRASE_RECORDS = 64
MAX_TOOL_ACTIONS = 64
MAX_ROUNDS = 2
MAX_SECONDS = 180.0
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
    # Revisit locally aligned quotations, including already-actionable near matches.
    for candidate in candidates:
        if candidate.get("evidence_level") != "near_text":
            continue
        options = candidate.get("source_options") or []
        proof = next((o["quote_evidence"] for o in options if o.get("quote_evidence")
                      and not o.get("personal") and not o.get("private_source")), {})
        # Audit candidates produced by existing-note matching can carry a source
        # page but no whole-quote proof. Verify their original wording locally
        # before considering any public excerpt for the model.
        if not proof and (candidate.get("verification_scope") != "full_quote" or
                          not any(not (o.get("private_source") or o.get("personal")) for o in options)):
            continue
        raw = str(proof.get("paper_text") or candidate.get("paper_text") or "")
        if not raw or not _redact_fragment(raw):
            continue
        identity = (int(candidate.get("paragraph_index") or 0), int(candidate.get("raw_start") or 0),
                    int(candidate.get("raw_end") or 0))
        seen.add(identity)
        is_audit = candidate.get("kind") == "audit"
        chosen.append({
            "section_id": candidate.get("section_id"), "paragraph_index": identity[0],
            "raw_start": identity[1], "raw_end": identity[2],
            "_kind": "note_anchor" if is_audit else "quote", "_raw": raw,
            "_agent_fragment": _redact_fragment(raw), "_priority": -1, "_review": candidate,
            "_source_candidate": candidate if is_audit else None,
        })
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
        if identity in seen:
            continue
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
            "_raw": raw,
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
        seen.add(identity)
        chosen.append({
            **record, "_kind": kind, "_raw": raw,
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
    # Caps apply to each execution after completed records have been removed.
    selected = direct + paraphrase
    deferred = 0
    redacted: list[dict] = []
    record_map: dict[str, dict] = {}
    for record in selected:
        identity = (record.get("_kind"), record.get("section_id"), record.get("paragraph_index"),
                    record.get("raw_start"), record.get("raw_end"), record.get("_raw"))
        opaque = "r" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
        if opaque in record_map:
            continue
        redacted.append({
            "record_id": opaque,
            "kind": str(record.get("_kind") or "direct"),
            "fragment": str(record.get("_agent_fragment") or "")[:agent_queue.MAX_FRAGMENT_CHARS],
        })
        record_map[opaque] = record
    return redacted, record_map, deferred


def _scope(corpus, tokens: list[str]) -> tuple[dict, list[str], list[str]]:
    public = [t for t in tokens if not str(t).startswith(("mylib:", "book:mylib:"))]
    scope_spec, _volumes = tasks.core._selected_volumes(corpus, public) if public else ({}, [])
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
        if len(fragment_norm) < 6 or len(fragment_norm) > 120:
            return False
        if fragment_norm in record_norm:
            return True
        if tool != "near_quote" or len(fragment_norm) < 12:
            return False
        # A variant may change two characters, but must still align to the sent fragment.
        query = normalize(str(record.get("_agent_fragment") or record_norm[:320]))
        width = len(fragment_norm)
        return any(Levenshtein.distance(fragment_norm, query[i:i + n], score_cutoff=2) <= 2
                   for n in range(width - 2, width + 3) for i in range(len(query) - n + 1))
    if tool == "keyword_cooccurrence" and depth == "direct_and_paraphrase":
        keywords = [normalize(str(value)) for value in (action.get("keywords") or [])]
        # Paraphrase evidence is intentionally stricter than a general search.
        # Two broad concepts (for example “社会/生产”) routinely find a related
        # passage that does not support the proposition being cited.  Require at
        # least three concepts, all copied from the paper fragment; the local
        # executor below then requires every one of them in the same short window.
        return 3 <= len(keywords) <= 5 and all(len(value) >= 2 and value in record_norm for value in keywords)
    return False


def _retrieve_hits(action, corpus, scope, check_active):
    # Poll between actual volumes, including while searching common keywords.
    # A direct hit is only a lead; verify_options later covers the full scope.
    if check_active and hasattr(corpus, "_scoped_volumes"):
        scopes = ({book: {v.volume}} for book in scope for v in corpus._scoped_volumes(book, scope))
    else:
        scopes = (scope,)
    results = []
    for selected in scopes:
        if check_active:
            check_active()
        if action["tool"] == "keyword_cooccurrence":
            keywords = action["keywords"]
            results.extend(corpus.keyword_cooccurrence(
                keywords, window=180, min_distinct=len(keywords), occ_cap=120,
                book_scope=selected, expand_synonyms=False,
            ))
            if len(results) > MAX_PARAPHRASE_SOURCE_OPTIONS:
                break
        else:
            results = corpus.locate_quote(action["fragment"], per_book_exact=8,
                                         allow_fuzzy=action["tool"] == "near_quote", book_scope=selected)
            if results:
                break
    return results


def _execute_action(action: dict, record: dict, corpus, scope_spec: dict, style: str, depth: str, *,
                    verify_full=None, cached_hits=None, check_active=None) -> tuple[dict, dict | None]:
    action_id = str(action.get("action_id") or "")
    record_id = str(action.get("record_id") or "")
    if not scope_spec or not _action_allowed(action, record, depth):
        return {
            "record_id": record_id, "action_id": action_id,
            "tool": str(action.get("tool") or ""),
            "fragment": str(action.get("fragment") or "")[:240],
            "keywords": list(action.get("keywords") or [])[:5],
            "hit_count": 0,
            "exact_count": 0, "unique_source_count": 0, "result": "rejected",
        }, None
    tool = str(action.get("tool") or "")
    cache_key = (tool, str(action.get("fragment") or ""), tuple(action.get("keywords") or []))
    if cached_hits is not None and cache_key in cached_hits:
        hits = cached_hits[cache_key]
    else:
        hits = _retrieve_hits(action, corpus, scope_spec, check_active)
    if cached_hits is not None:
        cached_hits[cache_key] = hits
    if check_active:
        check_active()
    options = _options(hits)
    if options and tool != "keyword_cooccurrence" and verify_full:
        options = verify_full(record)
        # A fragment is only a retrieval lead. A full-scope verifier returns the actual
        # spans and printed pages (including any competing reprints) for this quote.
        options = [o for o in options if not _AUXILIARY_SECTION_RE.fullmatch(str(o.get("section_title") or "").strip())]
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
            (normalize(str(action.get("fragment") or "")) == normalize(record_text)
             or bool(best.get("quote_evidence", {}).get("complete_search")))
            and (verify_full is None or bool((best.get("quote_evidence") or {}).get("complete_search")))
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


def _evidence_packet(candidate, raw, scope):
    """Send only public excerpts that account for every locally measured edit."""
    excerpts = []
    if not candidate:
        return excerpts
    for i, option in enumerate(candidate.get("source_options") or []):
        book = option.get("book")
        if (book not in scope or option.get("private_source") or option.get("personal")
                or (scope[book] is not None and option.get("volume") not in scope[book])):
            continue
        proof = option.get("quote_evidence") or {}
        if proof.get("coverage") != 1 or proof.get("paper_text") != raw:
            continue
        edits = proof.get("differences") or []
        if not edits or not 1 <= proof.get("errors", 0) <= 2:
            continue
        pair = {}
        for key, text, field in (("paper", raw, "quotation"), ("source", proof["source_text"], "excerpt")):
            left = min(e[key + "_start"] for e in edits)
            right = max(e[key + "_end"] for e in edits)
            # Distant edits get individual context windows. Every edited character
            # is shown in both versions, including insertions at a window end.
            if right - left <= 120:
                window = text[max(0, left - 40):right + 40]
            else:
                window = " … ".join(text[max(0, e[key + "_start"] - 35):e[key + "_end"] + 35] for e in edits)
            redacted = _redact_fragment(window)
            if (not redacted or len(window) > 320 or
                    any(pattern.search(window) for pattern in (_EMAIL_RE, _URL_RE, _PHONE_RE, _LONG_ID_RE))):
                break
            pair[field] = redacted
        if len(pair) == 2:
            excerpts.append({"candidate_id": f"c{i + 1}", **pair})
            if len(excerpts) == 3:
                break
    return excerpts


def _with_verdict(candidate, judgments, *, mixed_scope):
    result = copy.deepcopy(candidate)
    for judgment in judgments:
        index = int(judgment["candidate_id"][1:]) - 1
        evidence = result["source_options"][index]["quote_evidence"]
        evidence["ai_verdict"] = judgment["judgment"]
        evidence["ai_confirmed"] = not mixed_scope and judgment["judgment"] == "minor_difference"
    result["evidence_origin"] = "agent"
    return result


def _queue_exchange(payload, remaining, check_active):
    queue_id = agent_queue.enqueue(payload, ttl_seconds=max(1, int(remaining)))
    try:
        return agent_queue.wait_result(queue_id, timeout_seconds=remaining, check_active=check_active)
    finally:
        agent_queue.delete(queue_id)


class _BudgetReached(Exception):
    pass


def run_agent(
    *, job: dict, extracted: dict, records: list[dict], deterministic_candidates: list[dict],
    resolved_style: str, corpus, checkpoint=None, on_checkpoint=None, check_active=None,
) -> tuple[list[dict], dict]:
    del extracted
    started = time.monotonic()
    deadline = started + _env_limit("CITATION_AGENT_TIMEOUT_SECONDS", int(MAX_SECONDS), 1, 600)
    max_actions = _env_limit("CITATION_AGENT_MAX_TOOL_ACTIONS", MAX_TOOL_ACTIONS, 1, 512)
    depth = str(job.get("recognition_depth") or "direct_only")
    tokens = list(job.get("scope") or [])
    scope, public, personal = _scope(corpus, tokens)
    volumes = [v for b, allowed in scope.items() for v in corpus.books[b]
               if allowed is None or int(v.volume) in allowed]
    wire, local, _ = _redacted_records(records, deterministic_candidates, depth, str(job.get("mode") or "both"))
    if not scope:
        # All private matching stays with the ownership-aware deterministic callback.
        wire, local = [], {}
    state = dict(checkpoint or {})
    # A continuation is always tied to the same upload, scope, template and corpus.
    # Version this shape so a prior protocol cannot attest to current evidence.
    if state.get("protocol") != 2:
        state = {}
    completed = set(state.get("terminal", ())) & set(local)
    pending = dict(state.get("round_one", {}))
    best = dict(state.get("best", {}))
    cached_hits, cached_full = {}, {}
    action_count = 0
    max_round = 0
    lane_status = {"direct": "complete", "paraphrase": "complete" if depth == "direct_and_paraphrase" else "not_run"}
    error_code = ""

    def active():
        if check_active and check_active() is False:
            raise _BudgetReached()
        if time.monotonic() >= deadline:
            raise _BudgetReached()

    def verify(record):
        from citation_quote_evidence import verify_options
        text = record["_raw"]
        if text not in cached_full:
            cached_full[text] = verify_options(corpus, volumes, text, resolved_style, check_active=active)
        return cached_full[text]

    def attach_missing_proof(candidate, record):
        if (not candidate or not volumes or
                any(o.get("quote_evidence") for o in candidate.get("source_options") or [])):
            return candidate
        verified = verify(record)
        if not verified:
            return candidate
        enriched = copy.deepcopy(candidate)
        for option in enriched["source_options"]:
            if option.get("private_source") or option.get("personal"):
                continue
            same = [v for v in verified if tasks.core._option_signature(v) == tasks.core._option_signature(option)]
            if len(same) == 1:
                proof = dict(same[0]["quote_evidence"])
                proof["complete_search"] = bool(proof["complete_search"] and len(verified) == 1 and not personal)
                option["quote_evidence"] = proof
        return enriched

    def publish(changed):
        if on_checkpoint:
            on_checkpoint({"protocol": 2, "terminal": sorted(completed), "round_one": pending, "best": best,
                           "eligible_record_count": len(wire), "processed_record_count": len(completed)},
                          changed)

    def process_batch(batch, round_no, lane):
        nonlocal action_count, max_round
        active()
        max_round = max(max_round, round_no)
        outgoing = []
        for entry in batch:
            rid = entry["record_id"]
            record = local[rid]
            candidate = best.get(rid) or record.get("_review")
            candidate = attach_missing_proof(candidate, record)
            if candidate is not None:
                record["_review"] = candidate
            packet = _evidence_packet(candidate, record["_raw"], scope)
            context = str(record.get("context_text") or "")
            # Keep just the immediately preceding context when it helps identify a speaker.
            before, sep, _after = context.partition(record["_raw"])
            surrounding = _redact_fragment(before[-96:]) if sep else ""
            outgoing.append({**entry, "evidence": packet, "surrounding": surrounding})
        payload = {
            "protocol_version": 2, "prompt_version": PROMPT_VERSION, "round": round_no,
            "planning_effort": "fast" if round_no == 2 else "deep",
            "recognition_depth": "direct_and_paraphrase" if lane == "paraphrase" else "direct_only",
            "allowed_public_books": [f"b{i}" for i in range(1, len(public) + 1)],
            "personal_source_ids": personal, "records": outgoing,
            "observations": [pending[e["record_id"]]["observation"] for e in batch
                             if pending.get(e["record_id"], {}).get("observation")],
        }
        result = _queue_exchange(payload, deadline - time.monotonic(), active)
        if result.get("status") != "complete":
            return str(result.get("error_code") or result.get("status") or "agent_unavailable")
        answer = agent_queue.validate_response(result.get("response") or {}, payload)
        for entry in outgoing:
            rid = entry["record_id"]
            record = local[rid]
            candidate = best.get(rid) or record.get("_review")
            verdicts = [v for v in answer["verdicts"] if v["record_id"] == rid]
            if candidate and verdicts:
                candidate = _with_verdict(candidate, verdicts, mixed_scope=bool(personal))
                best[rid] = candidate
            obs = pending.get(rid, {}).get("observation")
            # Up to two searches in each of two rounds. Prior completed work is
            # retained after a budget boundary, including a reviewed candidate.
            actions = [a for a in answer["actions"] if a["record_id"] == rid][:2]
            for action in actions:
                active()
                if action_count >= max_actions:
                    publish([candidate] if candidate else [])
                    raise _BudgetReached()
                action_count += 1
                obs, found = _execute_action(action, record, corpus, scope, resolved_style, depth,
                                             verify_full=verify if volumes else None,
                                             cached_hits=cached_hits, check_active=active)
                if found and personal:
                    # Public proof cannot establish uniqueness in a mixed scope.
                    for option in found.get("source_options") or []:
                        if option.get("quote_evidence"):
                            option["quote_evidence"]["complete_search"] = False
                    found["auto_selected"] = False
                    if found.get("evidence_level") == "exact":
                        found["evidence_level"] = "near_text"
                if found and (not candidate or _candidate_rank(found) < _candidate_rank(candidate)):
                    best[rid] = candidate = found
            terminal = (round_no == 2 or bool(verdicts) or
                        (candidate or {}).get("evidence_level") in {"exact", "paraphrase"})
            if terminal:
                completed.add(rid)
                pending.pop(rid, None)
            else:
                pending[rid] = {"observation": obs}
            publish([best[rid]] if rid in best else [])
        return ""

    for lane in ("direct", "paraphrase"):
        lane_items = [r for r in wire if (r["kind"] == "paraphrase") == (lane == "paraphrase")
                      and r["record_id"] not in completed]
        cap = (_env_limit("CITATION_AGENT_MAX_RECORDS", MAX_RECORDS, 1, 2000) if lane == "direct"
               else _env_limit("CITATION_AGENT_MAX_PARAPHRASE_RECORDS", MAX_PARAPHRASE_RECORDS, 1, 1000))
        for start in range(0, min(cap, len(lane_items)), agent_queue.MAX_BATCH_RECORDS):
            chunk = lane_items[start:min(start + agent_queue.MAX_BATCH_RECORDS, cap)]
            failure = ""
            for round_no in range(1, MAX_ROUNDS + 1):
                batch = [r for r in chunk if r["record_id"] not in completed
                         and (r["record_id"] in pending) == (round_no == 2)]
                if not batch:
                    continue
                try:
                    failure = process_batch(batch, round_no, lane)
                except _BudgetReached:
                    failure = "budget_exhausted"
                except agent_queue.AgentQueueError:
                    failure = "queue_validation"
                if failure:
                    error_code = failure[:80]
                    lane_status[lane] = ("budget_exhausted" if failure in {"timeout", "expired", "budget_exhausted"}
                                         else "degraded")
                    break
            if failure and failure not in {"invalid_json", "invalid_json_shape", "invalid_action_schema"}:
                break
        if len(lane_items) > cap and lane_status[lane] == "complete":
            lane_status[lane] = "budget_exhausted"

    additions = list(best.values())
    used = len(completed)
    exhausted = "budget_exhausted" in lane_status.values()
    degraded = "degraded" in lane_status.values()
    status = "budget_exhausted" if exhausted else "degraded" if degraded else "complete"
    report = {
        "status": status, "mode": "live", "prompt_version": PROMPT_VERSION,
        "model": str(os.environ.get("CITATION_AGENT_MODEL") or "deepseek-v4-flash")[:120],
        "attempted_record_count": len(wire), "eligible_record_count": len(wire),
        "processed_record_count": used, "deferred_record_count": max(0, len(wire) - used),
        "verified_match_count": len(additions),
        "direct_verified_match_count": sum(c.get("evidence_level") in {"exact", "near_text"} for c in additions),
        "viewpoint_suggestion_count": sum(c.get("evidence_level") == "paraphrase" for c in additions),
        "direct_status": lane_status["direct"], "paraphrase_status": lane_status["paraphrase"],
        "skipped_no_evidence_count": len(completed - set(best)), "out_of_scope_count": 0,
        "duration_ms": int(1000 * (time.monotonic() - started)),
        "round_count": max_round, "tool_call_count": action_count, "error_code": error_code,
        "completion_reason": {"complete": "all_records_terminal", "degraded": "agent_lane_degraded",
                              "budget_exhausted": "tool_or_time_budget_exhausted"}[status],
    }
    return additions, report


def make_callback(corpus):
    def callback(**kwargs):
        return run_agent(corpus=corpus, **kwargs)
    return callback
