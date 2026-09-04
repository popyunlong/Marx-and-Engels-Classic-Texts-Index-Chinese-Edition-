from __future__ import annotations

"""Constrained V4 Pro shadow recall for the citation assistant.

The model may only propose short search fragments for deterministic local
lookup.  It never supplies a citation, page number, score, or user-visible
candidate.  Complete paper phrases are re-verified against the local corpus,
and this module returns aggregate metrics only; neither prompts nor model
responses are persisted.
"""

import json
import re
import time
from collections import defaultdict
from hashlib import sha256
from typing import Callable

import citation_assistant as citation_tasks
from build_index import normalize


MODEL = "deepseek-v4-pro"
PROMPT_VERSION = "citation-shadow-fragment-plan-v1"
MAX_RECORDS = 8
MAX_RECORD_CHARS = 320
MAX_SEARCHES_PER_RECORD = 2
MIN_FRAGMENT_CHARS = 4
MAX_FRAGMENT_CHARS = 20
MAX_OCCURRENCES_PER_VOLUME = 16
MAX_VERIFICATIONS_PER_RECORD_VOLUME = 12


def _covered_record(record: dict, candidates: list[dict]) -> bool:
    paragraph_index = int(record.get("paragraph_index") or 0)
    raw_start = int(record.get("raw_start") or 0)
    raw_end = int(record.get("raw_end") or 0)
    for candidate in candidates:
        if str(candidate.get("kind") or "") != "generate":
            continue
        if int(candidate.get("paragraph_index") or 0) != paragraph_index:
            continue
        if int(candidate.get("raw_start") or 0) <= raw_start and int(candidate.get("raw_end") or 0) >= raw_end:
            return True
    return False


def _unmatched_records(records: list[dict], candidates: list[dict]) -> list[dict]:
    unmatched = [
        record for record in records
        if str(record.get("norm") or "") and not _covered_record(record, candidates)
    ]
    unmatched.sort(key=lambda record: (
        0 if record.get("quoted") else 1,
        int(record.get("paragraph_index") or 0),
        int(record.get("raw_start") or 0),
    ))
    return unmatched[:MAX_RECORDS]


def _record_excerpt(record: dict) -> str:
    raw = " ".join(str(record.get("raw_text") or "").split())
    if len(raw) <= MAX_RECORD_CHARS:
        return raw
    half = max(1, (MAX_RECORD_CHARS - 1) // 2)
    return f"{raw[:half]}…{raw[-half:]}"


def _messages(records: list[dict], allowed_books: list[str]) -> list[dict[str, str]]:
    payload = {
        "allowed_book_keys": allowed_books,
        "records": [
            {
                "id": f"r{index}",
                "quoted": bool(record.get("quoted")),
                "text": _record_excerpt(record),
            }
            for index, record in enumerate(records)
        ],
    }
    instructions = (
        "你是论文引文检索规划器，只为本地封闭语料检索提出短片段，不回答出处。"
        "对每条记录最多给2组搜索。paper_fragment必须逐字来自输入记录；source_fragment可以是你推测的"
        "原著写法或与paper_fragment相同。片段应有4至20个归一化汉字且尽量独特。"
        "book_keys只能从allowed_book_keys选择，也可留空表示全部。"
        "严禁输出书名、卷号、页码、引文格式、置信度或解释。"
        "只输出严格JSON对象，结构为"
        '{"plans":[{"id":"r0","searches":[{"paper_fragment":"...",'
        '"source_fragment":"...","book_keys":[]}]}]}。'
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def _json_object(text: str) -> dict | None:
    raw = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.I | re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _all_norm_positions(text: str, fragment: str, limit: int = 4) -> list[int]:
    positions: list[int] = []
    start = 0
    while len(positions) < limit:
        position = text.find(fragment, start)
        if position < 0:
            break
        positions.append(position)
        start = position + 1
    return positions


def _parse_searches(payload: dict, records: list[dict], allowed_books: set[str]) -> list[dict]:
    plans = payload.get("plans")
    if not isinstance(plans, list):
        return []
    by_id = {f"r{index}": record for index, record in enumerate(records)}
    per_record: dict[str, int] = defaultdict(int)
    seen: set[tuple] = set()
    parsed: list[dict] = []
    for plan in plans[:MAX_RECORDS * 2]:
        if not isinstance(plan, dict):
            continue
        record_id = str(plan.get("id") or "")
        record = by_id.get(record_id)
        if record is None or per_record[record_id] >= MAX_SEARCHES_PER_RECORD:
            continue
        searches = plan.get("searches")
        if not isinstance(searches, list):
            continue
        for search in searches:
            if per_record[record_id] >= MAX_SEARCHES_PER_RECORD:
                break
            if not isinstance(search, dict):
                continue
            paper_fragment = normalize(str(search.get("paper_fragment") or ""))
            source_fragment = normalize(str(search.get("source_fragment") or ""))
            if not (MIN_FRAGMENT_CHARS <= len(paper_fragment) <= MAX_FRAGMENT_CHARS):
                continue
            if not (MIN_FRAGMENT_CHARS <= len(source_fragment) <= MAX_FRAGMENT_CHARS):
                continue
            paper_positions = _all_norm_positions(str(record.get("norm") or ""), paper_fragment)
            if not paper_positions:
                continue
            raw_books = search.get("book_keys")
            requested_books = {
                str(book) for book in raw_books
            } if isinstance(raw_books, list) else set()
            books = requested_books & allowed_books
            # A non-empty but wholly invalid scope must be rejected rather than
            # silently widened to every book in the task.
            if requested_books and not books:
                continue
            signature = (record_id, paper_fragment, source_fragment, tuple(sorted(books)))
            if signature in seen:
                continue
            seen.add(signature)
            parsed.append({
                "record_id": record_id,
                "record": record,
                "paper_positions": paper_positions,
                "source_fragment": source_fragment,
                "books": books,
            })
            per_record[record_id] += 1
    return parsed


def _validated_metrics(searches: list[dict], corpus, scope_tokens: list[str]) -> dict[str, int]:
    _, volumes = citation_tasks._selected_volumes(corpus, scope_tokens)
    seeds: dict[str, list[dict]] = defaultdict(list)
    for search in searches:
        seeds[str(search["source_fragment"])].append(search)
    matcher = citation_tasks._build_seed_matcher(seeds)
    verified: dict[str, dict[tuple, dict]] = defaultdict(dict)
    for volume in volumes:
        positions: dict[str, list[int]] = defaultdict(list)
        too_common: set[str] = set()
        for source_start, seed in citation_tasks._iter_seed_hits(volume.norm_full, seeds, matcher):
            if seed in too_common:
                continue
            bucket = positions[seed]
            bucket.append(int(source_start))
            if len(bucket) > MAX_OCCURRENCES_PER_VOLUME:
                positions.pop(seed, None)
                too_common.add(seed)
        verified_count: dict[tuple[str, str], int] = defaultdict(int)
        for seed, source_positions in positions.items():
            for search in seeds.get(seed, []):
                if search["books"] and str(volume.book) not in search["books"]:
                    continue
                record_id = str(search["record_id"])
                budget_key = (record_id, seed)
                for source_start in source_positions:
                    for paper_position in search["paper_positions"]:
                        if verified_count[budget_key] >= MAX_VERIFICATIONS_PER_RECORD_VOLUME:
                            break
                        verified_count[budget_key] += 1
                        estimated = int(source_start) - int(paper_position)
                        aligned = citation_tasks._align_near_seed(
                            search["record"], volume.norm_full, estimated,
                        )
                        if not aligned:
                            continue
                        source_left, source_right, score, errors = aligned
                        try:
                            hit = corpus._make_hit(
                                volume, source_left, source_right, "fuzzy", score,
                                str(search["record"].get("raw_text") or ""),
                                fuzzy_errors=errors,
                            )
                            option = citation_tasks._option_from_hit(hit)
                        except Exception:
                            continue
                        signature = citation_tasks._option_signature(option)
                        verified[record_id][signature] = option
                    if verified_count[budget_key] >= MAX_VERIFICATIONS_PER_RECORD_VOLUME:
                        break
    option_sets = list(verified.values())
    return {
        "verified_match_count": sum(len(options) for options in option_sets),
        "incremental_record_count": len(option_sets),
        "ambiguous_record_count": sum(1 for options in option_sets if len(options) > 1),
        "reliable_record_count": sum(
            1 for options in option_sets
            if any(citation_tasks._is_reliable_page(option) for option in options.values())
        ),
    }


def run_shadow(
    records: list[dict],
    candidates: list[dict],
    corpus,
    scope_tokens: list[str],
    *,
    complete: Callable[[list[dict[str, str]], int], str],
) -> dict:
    """Run a non-mutating Agent shadow and return privacy-safe aggregates."""
    started = time.monotonic()
    unmatched = _unmatched_records(records, candidates)
    report: dict[str, object] = {
        "status": "skipped" if not unmatched else "complete",
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "attempted_record_count": len(unmatched),
        "planned_record_count": 0,
        "search_count": 0,
        "verified_match_count": 0,
        "incremental_record_count": 0,
        "ambiguous_record_count": 0,
        "reliable_record_count": 0,
        "response_sha256": "",
        "error_code": "",
    }
    if not unmatched:
        report["duration_ms"] = int(round((time.monotonic() - started) * 1000))
        return report
    try:
        scope_spec, _ = citation_tasks._selected_volumes(corpus, scope_tokens)
        allowed_books = list(scope_spec)
        response = complete(_messages(unmatched, allowed_books), 1000)
        report["response_sha256"] = sha256(str(response).encode("utf-8")).hexdigest()
        payload = _json_object(response)
        if payload is None:
            report["status"] = "invalid_response"
        else:
            searches = _parse_searches(payload, unmatched, set(allowed_books))
            report["search_count"] = len(searches)
            report["planned_record_count"] = len({str(search["record_id"]) for search in searches})
            report.update(_validated_metrics(searches, corpus, scope_tokens))
    except Exception as exc:  # Shadow failures must never fail the citation job.
        report["status"] = "error"
        report["error_code"] = type(exc).__name__[:80]
    report["duration_ms"] = int(round((time.monotonic() - started) * 1000))
    return report
