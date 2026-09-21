from __future__ import annotations

"""Administrator-only citation Agent test backend.

The proven DOCX/corpus implementation is loaded as a second Python module and pointed
at a different database and artifact root.  The public citation assistant therefore
keeps its original database, files, workers, routes, and behaviour.
"""

import importlib.util
import json
import threading
from pathlib import Path
from typing import Callable

from build_index import normalize
from runtime_env import APPDATA_DIR, secure_db_file


_CORE_PATH = Path(__file__).with_name("citation_assistant.py")
_SPEC = importlib.util.spec_from_file_location("_citation_agent_test_core", _CORE_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - deployment corruption
    raise RuntimeError("无法载入论文校注核心。")
core = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(core)

# These assignments happen before init_db and before any job operation.  Function
# globals in the isolated module resolve to these values for the lifetime of the
# process; the public citation_assistant module remains untouched.
core.DB_PATH = APPDATA_DIR / "citation_agent_test.sqlite3"
core.ARTIFACT_ROOT = APPDATA_DIR / "citation_agent_test"

DB_PATH = core.DB_PATH
ARTIFACT_ROOT = core.ARTIFACT_ROOT
CitationAssistantError = core.CitationAssistantError
MAX_DOCX_BYTES = core.MAX_DOCX_BYTES
MAX_ACTIVE_PER_USER = core.MAX_ACTIVE_PER_USER
MAX_JOBS_PER_DAY = core.MAX_JOBS_PER_DAY
VALID_CITATION_STYLES = core.VALID_CITATION_STYLES
VALID_RECOGNITION_DEPTHS = {"direct_only", "direct_and_paraphrase"}
VALID_REVIEW_BUCKETS = {"actionable", "unresolved"}
VALID_AGENT_STATUSES = {"not_run", "running", "complete", "degraded", "budget_exhausted"}
VALID_VERIFICATION_SCOPES = {
    "full_quote", "unquoted_text", "note_sentence", "locator_only", "paraphrase", "unknown",
}
_EXTENDED_JOB_FIELDS = {
    "recognition_depth", "analysis_stage", "agent_status", "agent_verified_count",
    "viewpoint_suggestion_count",
    "unresolved_count", "out_of_scope_count", "skipped_no_evidence_count",
    "direct_agent_status", "paraphrase_agent_status", "eligible_record_count",
    "processed_record_count", "deferred_record_count", "completion_reason",
}
_INIT_LOCK = threading.Lock()
_READY_DB_PATH = ""


def __getattr__(name: str):
    """Forward stable parsing/export constants and helpers to the isolated core."""
    return getattr(core, name)


def init_db() -> Path:
    global _READY_DB_PATH
    target = str(core.DB_PATH)
    if _READY_DB_PATH == target and core.DB_PATH.is_file():
        return core.DB_PATH
    with _INIT_LOCK:
        if _READY_DB_PATH == target and core.DB_PATH.is_file():
            return core.DB_PATH
        core.init_db()
        secure_db_file(core.DB_PATH)
        with core._connect() as conn:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(citation_assistant_jobs)")}
            for name, declaration in {
                "recognition_depth": "TEXT NOT NULL DEFAULT 'direct_only'",
                "analysis_stage": "TEXT NOT NULL DEFAULT ''",
                "agent_status": "TEXT NOT NULL DEFAULT 'not_run'",
                "agent_verified_count": "INTEGER NOT NULL DEFAULT 0",
                "viewpoint_suggestion_count": "INTEGER NOT NULL DEFAULT 0",
                "unresolved_count": "INTEGER NOT NULL DEFAULT 0",
                "out_of_scope_count": "INTEGER NOT NULL DEFAULT 0",
                "skipped_no_evidence_count": "INTEGER NOT NULL DEFAULT 0",
                "direct_agent_status": "TEXT NOT NULL DEFAULT 'not_run'",
                "paraphrase_agent_status": "TEXT NOT NULL DEFAULT 'not_run'",
                "eligible_record_count": "INTEGER NOT NULL DEFAULT 0",
                "processed_record_count": "INTEGER NOT NULL DEFAULT 0",
                "deferred_record_count": "INTEGER NOT NULL DEFAULT 0",
                "completion_reason": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE citation_assistant_jobs ADD COLUMN {name} {declaration}")

            candidate_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(citation_assistant_candidates)")
            }
            for name, declaration in {
                "review_bucket": "TEXT NOT NULL DEFAULT 'actionable'",
                "evidence_origin": "TEXT NOT NULL DEFAULT 'deterministic'",
                "evidence_level": "TEXT NOT NULL DEFAULT ''",
                "verification_scope": "TEXT NOT NULL DEFAULT 'unknown'",
                "text_match_level": "TEXT NOT NULL DEFAULT 'none'",
                "source_resolution": "TEXT NOT NULL DEFAULT 'none'",
                "writeback_mode": "TEXT NOT NULL DEFAULT 'none'",
                "reason_codes_json": "TEXT NOT NULL DEFAULT '[]'",
            }.items():
                if name not in candidate_columns:
                    conn.execute(f"ALTER TABLE citation_assistant_candidates ADD COLUMN {name} {declaration}")
            # Safety backfill for existing administrator-test tasks. A page
            # locator was never textual evidence, even if an older build exposed
            # it as an actionable near-text candidate.
            locator_jobs = [
                str(row[0]) for row in conn.execute(
                    "SELECT DISTINCT job_id FROM citation_assistant_candidates WHERE match_type='locator'"
                )
            ]
            conn.execute(
                """UPDATE citation_assistant_candidates
                   SET review_bucket='unresolved',evidence_level='',verification_scope='locator_only',
                       issue_code='unverifiable',issue_label='仅定位到原注所写页码，文字未核验',
                       decision='informational',auto_selected=0
                   WHERE match_type='locator'"""
            )
            for locator_job_id in locator_jobs:
                conn.execute(
                    """UPDATE citation_assistant_jobs
                       SET candidate_count=(SELECT COUNT(*) FROM citation_assistant_candidates
                                            WHERE job_id=? AND review_bucket='actionable'),
                           accepted_count=(SELECT COUNT(*) FROM citation_assistant_candidates
                                           WHERE job_id=? AND review_bucket='actionable'
                                             AND decision='accepted'),
                           unresolved_count=(SELECT COUNT(*) FROM citation_assistant_candidates
                                             WHERE job_id=? AND review_bucket='unresolved')
                       WHERE id=?""",
                    (locator_job_id, locator_job_id, locator_job_id, locator_job_id),
                )
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_cat_candidates_bucket
                  ON citation_assistant_candidates(job_id,review_bucket,decision);
                CREATE TABLE IF NOT EXISTS citation_agent_test_runs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    attempted_record_count INTEGER NOT NULL DEFAULT 0,
                    round_count INTEGER NOT NULL DEFAULT 0,
                    tool_call_count INTEGER NOT NULL DEFAULT 0,
                    verified_match_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES citation_assistant_jobs(id) ON DELETE CASCADE
                );
                """
            )
            run_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(citation_agent_test_runs)")
            }
            for name, declaration in {
                "direct_verified_match_count": "INTEGER NOT NULL DEFAULT 0",
                "viewpoint_suggestion_count": "INTEGER NOT NULL DEFAULT 0",
                "direct_status": "TEXT NOT NULL DEFAULT 'not_run'",
                "paraphrase_status": "TEXT NOT NULL DEFAULT 'not_run'",
                "eligible_record_count": "INTEGER NOT NULL DEFAULT 0",
                "processed_record_count": "INTEGER NOT NULL DEFAULT 0",
                "deferred_record_count": "INTEGER NOT NULL DEFAULT 0",
                "completion_reason": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in run_columns:
                    conn.execute(f"ALTER TABLE citation_agent_test_runs ADD COLUMN {name} {declaration}")
            _backfill_existing_candidate_rules(conn)
        _READY_DB_PATH = target
    return core.DB_PATH


def _required_scope_tokens(values: object) -> list[str]:
    tokens = list(dict.fromkeys(
        str(value).strip() for value in (values or []) if str(value).strip()
    ))
    if not tokens:
        raise CitationAssistantError("请先指定至少一部站内著作、卷册或个人文库资料。")
    return tokens[:500]


def create_job(*args, recognition_depth: str = "direct_only", **kwargs) -> dict:
    init_db()
    depth = recognition_depth if recognition_depth in VALID_RECOGNITION_DEPTHS else "direct_only"
    kwargs["scope_tokens"] = _required_scope_tokens(kwargs.get("scope_tokens"))
    job = core.create_job(*args, **kwargs)
    update_job(
        str(job["id"]), recognition_depth=depth, analysis_stage="deterministic",
        agent_status="not_run", agent_verified_count=0, viewpoint_suggestion_count=0,
        unresolved_count=0,
        out_of_scope_count=0, skipped_no_evidence_count=0,
    )
    return get_job(str(job["id"])) or job


def update_job(job_id: str, **values: object) -> None:
    base = {key: value for key, value in values.items() if key not in _EXTENDED_JOB_FIELDS}
    if base:
        core.update_job(job_id, **base)
    extended = {key: value for key, value in values.items() if key in _EXTENDED_JOB_FIELDS}
    if not extended:
        return
    extended["updated_at"] = core._iso()
    sets = ",".join(f"{key}=?" for key in extended)
    with core._connect() as conn:
        conn.execute(
            f"UPDATE citation_assistant_jobs SET {sets} WHERE id=?",
            (*extended.values(), str(job_id)),
        )


def get_job(job_id: str, user_id: int | None = None) -> dict | None:
    init_db()
    return core.get_job(job_id, user_id)


def list_jobs(user_id: int, limit: int = 30) -> list[dict]:
    init_db()
    return core.list_jobs(user_id, limit)


def count_active_jobs(user_id: int) -> int:
    init_db()
    return core.count_active_jobs(user_id)


def count_jobs_since(user_id: int, since_iso: str) -> int:
    init_db()
    return core.count_jobs_since(user_id, since_iso)


def set_analysis_config(
    job_id: str,
    user_id: int,
    *,
    section_ids: list[str],
    scope_tokens: list[str],
) -> dict:
    scope_tokens = _required_scope_tokens(scope_tokens)
    row = core.set_analysis_config(
        job_id, user_id, section_ids=section_ids, scope_tokens=scope_tokens,
    )
    update_job(
        job_id, analysis_stage="deterministic", agent_status="not_run",
        agent_verified_count=0, viewpoint_suggestion_count=0,
        unresolved_count=0, out_of_scope_count=0,
        skipped_no_evidence_count=0,
    )
    return get_job(job_id, user_id) or row


def _candidate_level(item: dict) -> str:
    if str(item.get("evidence_level") or "") == "paraphrase":
        return "paraphrase"
    # A locator only proves that the volume/page named by the existing note can
    # be opened. It is deliberately not near-text evidence.
    if str(item.get("match_type") or "") == "locator":
        return ""
    if str(item.get("evidence_level") or "") in {"exact", "near_text", "paraphrase"}:
        return str(item["evidence_level"])
    options = list(item.get("source_options") or [])
    if (
        len(options) == 1
        and str(item.get("match_type") or "") == "exact"
        and float(item.get("score") or 0) == 100.0
        and core._is_reliable_page(options[0])
    ):
        return "exact"
    return "near_text" if options else ""


_OPEN_QUOTES = "\u201c\"\u300c\u300e"
_CLOSE_QUOTES = "\u201d\"\u300d\u300f"
_TRAILING_PUNCTUATION = "\u3002\uff01\uff1f\uff1b;!?\uff0c,\uff09)"
_AUTO_EXACT_ISSUES = {
    "suggest_add", "verified", "wrong_page", "wrong_source", "wrong_version",
    "format_mismatch",
}


def _paragraphs_by_index(extracted: dict | None) -> dict[int, dict]:
    return {
        int(paragraph.get("index", index)): paragraph
        for index, paragraph in enumerate((extracted or {}).get("paragraphs") or [])
    }


def _is_full_quote_span(item: dict, paragraphs: dict[int, dict]) -> bool:
    paragraph = paragraphs.get(int(item.get("paragraph_index") or 0))
    text = str((paragraph or {}).get("text") or "")
    start = max(0, min(int(item.get("raw_start") or 0), len(text)))
    end = max(start, min(int(item.get("raw_end") or 0), len(text)))
    if text:
        left = start - 1
        while left >= 0 and text[left].isspace():
            left -= 1
        right = end
        while right < len(text) and text[right].isspace():
            right += 1
        if left >= 0 and right < len(text):
            if text[left] in _OPEN_QUOTES and text[right] in _CLOSE_QUOTES:
                return True
        span = text[start:end].strip().rstrip(_TRAILING_PUNCTUATION)
    else:
        span = str(item.get("paper_text") or "").strip().rstrip(_TRAILING_PUNCTUATION)
    return len(span) >= 2 and span[0] in _OPEN_QUOTES and span[-1] in _CLOSE_QUOTES


def _verification_scope(item: dict, paragraphs: dict[int, dict]) -> str:
    if str(item.get("evidence_level") or "") == "paraphrase":
        return "paraphrase"
    if str(item.get("match_type") or "") == "locator":
        return "locator_only"
    explicit = str(item.get("verification_scope") or "")
    if not paragraphs and explicit in VALID_VERIFICATION_SCOPES and explicit != "unknown":
        return explicit
    if _is_full_quote_span(item, paragraphs):
        return "full_quote"
    if str(item.get("kind") or "") == "audit":
        return "note_sentence"
    if str(item.get("paper_text") or "").strip():
        return "unquoted_text"
    return "unknown"


def _apply_test_evidence_rules(
    item: dict, paragraphs: dict[int, dict], *, note_kind: str = "footnote",
) -> dict:
    """Apply admin-test-only evidence gates without changing the public workflow."""
    result = dict(item)
    scope = _verification_scope(result, paragraphs)
    result["verification_scope"] = scope

    def finalize() -> dict:
        level = str(result.get("evidence_level") or "")
        result["text_match_level"] = {
            "exact": "exact", "near_text": "near", "paraphrase": "paraphrase",
        }.get(level, "none")
        options = list(result.get("source_options") or [])
        if str(result.get("match_type") or "") == "locator" or scope == "locator_only":
            resolution = "locator_only"
        elif bool(result.get("reference_disambiguated")):
            resolution = "reference_disambiguated"
        elif len(options) > 1:
            resolution = "multiple"
        elif len(options) == 1:
            resolution = "unique"
        else:
            resolution = "none"
        result["source_resolution"] = resolution
        paragraph = paragraphs.get(int(result.get("paragraph_index") or 0), {})
        candidate_kind = str(result.get("kind") or "generate")
        start = int(result.get("raw_start") or 0)
        end = int(result.get("raw_end") or 0)
        safe_spans = list(paragraph.get("safe_comment_spans") or [])
        span_safe = (
            any(start >= int(span[0]) and end <= int(span[1]) and end > start for span in safe_spans)
            if safe_spans else not bool(paragraph.get("has_field"))
        )
        safe_anchor = (
            not bool(paragraph.get("tracked"))
            and span_safe
            and (
                not bool(paragraph.get("unsupported"))
                or (candidate_kind == "audit" and bool(paragraph.get("has_field")))
            )
        )
        if level == "paraphrase" or scope == "locator_only":
            writeback = "none"
        elif bool(paragraph.get("tracked")) or str(result.get("review_bucket") or "") == "unresolved":
            writeback = "readonly"
        elif candidate_kind == "audit" and options and safe_anchor:
            writeback = "comment"
        elif (
            candidate_kind == "generate" and options and safe_anchor
            and bool(result.get("auto_selected")) and level == "exact"
            and resolution in {"unique", "reference_disambiguated"}
        ):
            writeback = note_kind if note_kind in core.VALID_NOTE_KINDS else "footnote"
        elif candidate_kind == "audit" and options:
            writeback = "readonly"
        else:
            writeback = "none"
        result["writeback_mode"] = writeback
        reasons: list[str] = []
        if level == "exact": reasons.append("exact_text")
        if level == "near_text": reasons.append("near_text_requires_review")
        if level == "paraphrase": reasons.append("paraphrase_never_auto")
        if resolution == "reference_disambiguated": reasons.append("reference_field_disambiguated")
        if resolution == "multiple": reasons.append("multiple_reprints")
        if resolution == "locator_only": reasons.append("locator_without_text_match")
        if writeback == "comment": reasons.append("proofreading_comment_only")
        if writeback in core.VALID_NOTE_KINDS: reasons.append("auto_insert_hard_evidence")
        if writeback == "readonly": reasons.append("unsafe_ooxml_anchor")
        if not options: reasons.append("no_local_evidence")
        result["reason_codes"] = list(dict.fromkeys(reasons))
        return result

    if scope == "locator_only":
        result.update({
            "review_bucket": "unresolved",
            "evidence_level": "",
            "issue_code": "unverifiable",
            "issue_label": "仅定位到原注所写页码，文字未核验",
            "auto_selected": False,
            "proposed_citation": str(result.get("existing_note_text") or ""),
        })
        return finalize()

    level = _candidate_level(result)
    result["evidence_level"] = level
    paragraph = paragraphs.get(int(result.get("paragraph_index") or 0), {})
    readonly_structure = bool(paragraph.get("unsupported")) and bool(
        paragraph.get("has_field") or paragraph.get("tracked")
    )
    if readonly_structure and bool(result.get("source_options")):
        # Field/tracked-change paragraphs are safe to inspect but not to mutate
        # with the current writer. Keep their verified evidence visible while
        # making acceptance and export impossible.
        start = int(result.get("raw_start") or 0)
        end = int(result.get("raw_end") or 0)
        field_comment = (
            bool(paragraph.get("has_field"))
            and not bool(paragraph.get("tracked"))
            and any(
                start >= int(span[0]) and end <= int(span[1]) and end > start
                for span in paragraph.get("safe_comment_spans") or []
            )
        )
        audit_comment = str(result.get("kind") or "generate") == "audit" and field_comment
        result.update({
            "review_bucket": "actionable" if audit_comment else "unresolved",
            "verification_scope": "readonly_structure",
            "issue_label": (
                "已核验原文；管理员确认后仅以 Word 批注写入"
                if audit_comment else "已核验原文；该段含复杂结构，仅供只读复核"
            ),
            "auto_selected": False,
        })
        return finalize()
    if level == "paraphrase":
        result["auto_selected"] = False
        return finalize()

    options = list(result.get("source_options") or [])
    issue = str(result.get("issue_code") or "")
    exact_is_safe = (
        scope == "full_quote"
        and level == "exact"
        and issue in _AUTO_EXACT_ISSUES
        and core._auto_select(
            str(result.get("match_type") or ""),
            float(result.get("score") or 0),
            result.get("fuzzy_errors"),
            options,
            "conservative",
        )
    )
    result["auto_selected"] = bool(exact_is_safe)
    if scope in {"unquoted_text", "note_sentence"} and level == "exact":
        # Exact unquoted wording can reveal a missing quotation mark, but changing
        # the document still requires an administrator's decision.
        result["auto_selected"] = False
    return finalize()


def _backfill_existing_candidate_rules(conn) -> None:
    """Reclassify legacy admin-test rows without changing human decisions."""
    jobs = conn.execute(
        "SELECT id,extraction_path,note_kind FROM citation_assistant_jobs "
        "WHERE extraction_path!='' AND EXISTS ("
        "SELECT 1 FROM citation_assistant_candidates WHERE job_id=citation_assistant_jobs.id)"
    ).fetchall()
    for job in jobs:
        try:
            extracted = json.loads(Path(str(job["extraction_path"])).read_text(encoding="utf-8"))
        except Exception:
            continue
        paragraphs = _paragraphs_by_index(extracted)
        rows = conn.execute(
            "SELECT * FROM citation_assistant_candidates WHERE job_id=?",
            (str(job["id"]),),
        ).fetchall()
        for row in rows:
            item = core._candidate_dict(row)
            normalized = _apply_test_evidence_rules(
                item, paragraphs, note_kind=str(job["note_kind"] or "footnote"),
            )
            current_decision = str(item.get("decision") or "pending")
            current_auto = bool(item.get("auto_selected"))
            decision = current_decision
            auto = current_auto
            if str(normalized.get("verification_scope") or "") == "locator_only":
                decision, auto = "informational", False
            elif current_decision == "pending" and bool(normalized.get("auto_selected")):
                decision, auto = "accepted", True
            elif current_decision == "accepted" and current_auto and not normalized.get("auto_selected"):
                # Only reverse a previous automatic choice; a human acceptance is
                # never changed by this compatibility pass.
                decision, auto = "pending", False
            conn.execute(
                """UPDATE citation_assistant_candidates
                   SET review_bucket=?,evidence_level=?,verification_scope=?,issue_code=?,
                       issue_label=?,proposed_citation=?,decision=?,auto_selected=?,
                       text_match_level=?,source_resolution=?,writeback_mode=?,reason_codes_json=?
                   WHERE id=? AND job_id=?""",
                (
                    str(normalized.get("review_bucket") or item.get("review_bucket") or "actionable"),
                    str(normalized.get("evidence_level") or ""),
                    str(normalized.get("verification_scope") or "unknown"),
                    str(normalized.get("issue_code") or ""),
                    str(normalized.get("issue_label") or ""),
                    str(normalized.get("proposed_citation") or "")[:2000],
                    decision, 1 if auto else 0,
                    str(normalized.get("text_match_level") or "none"),
                    str(normalized.get("source_resolution") or "none"),
                    str(normalized.get("writeback_mode") or "none"),
                    json.dumps(normalized.get("reason_codes") or [], ensure_ascii=False),
                    int(item["id"]), str(job["id"]),
                ),
            )
        conn.execute(
            """UPDATE citation_assistant_jobs
               SET candidate_count=(SELECT COUNT(*) FROM citation_assistant_candidates
                                    WHERE job_id=? AND review_bucket='actionable'),
                   accepted_count=(SELECT COUNT(*) FROM citation_assistant_candidates
                                   WHERE job_id=? AND review_bucket='actionable'
                                     AND decision='accepted'),
                   unresolved_count=(SELECT COUNT(*) FROM citation_assistant_candidates
                                     WHERE job_id=? AND review_bucket='unresolved')
               WHERE id=?""",
            (str(job["id"]), str(job["id"]), str(job["id"]), str(job["id"])),
        )


def replace_candidates(
    job_id: str,
    candidates: list[dict],
    *,
    out_of_scope_count: int = 0,
    skipped_no_evidence_count: int = 0,
) -> None:
    now = core._iso()
    job_row = core.get_job(job_id) or {}
    note_kind = str(job_row.get("note_kind") or "footnote")
    actionable = 0
    unresolved = 0
    accepted = 0
    with core._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM citation_assistant_candidates WHERE job_id=?", (str(job_id),))
        for item in candidates:
            if str(item.get("match_type") or "") == "locator":
                item = _apply_test_evidence_rules(dict(item), {}, note_kind=note_kind)
            bucket = str(item.get("review_bucket") or "actionable")
            if bucket not in VALID_REVIEW_BUCKETS:
                bucket = "actionable"
            origin = "agent" if str(item.get("evidence_origin") or "") == "agent" else "deterministic"
            item = (
                _apply_test_evidence_rules(dict(item), {}, note_kind=note_kind)
                if not item.get("text_match_level") else item
            )
            level = _candidate_level(item)
            auto = bool(item.get("auto_selected")) and bucket == "actionable" and level == "exact"
            decision = "informational" if bucket == "unresolved" else ("accepted" if auto else "pending")
            actionable += int(bucket == "actionable")
            unresolved += int(bucket == "unresolved")
            accepted += int(decision == "accepted")
            conn.execute(
                """INSERT INTO citation_assistant_candidates
                   (job_id,kind,section_id,paragraph_index,raw_start,raw_end,paper_text,
                    existing_note_kind,existing_note_id,existing_note_text,match_type,score,
                     fuzzy_errors,issue_code,issue_label,source_options_json,selected_option,
                     proposed_citation,decision,auto_selected,review_bucket,evidence_origin,
                     evidence_level,verification_scope,text_match_level,source_resolution,
                     writeback_mode,reason_codes_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(job_id), str(item.get("kind") or "generate"), str(item.get("section_id") or ""),
                    int(item.get("paragraph_index", -1)), int(item.get("raw_start") or 0),
                    int(item.get("raw_end") or 0), str(item.get("paper_text") or "")[:2000],
                    str(item.get("existing_note_kind") or ""), item.get("existing_note_id"),
                    str(item.get("existing_note_text") or "")[:4000], str(item.get("match_type") or ""),
                    float(item.get("score") or 0), item.get("fuzzy_errors"),
                    str(item.get("issue_code") or ""), str(item.get("issue_label") or ""),
                    json.dumps(item.get("source_options") or [], ensure_ascii=False),
                    int(item.get("selected_option") or 0), str(item.get("proposed_citation") or "")[:2000],
                    decision, 1 if auto else 0, bucket, origin, level,
                    str(item.get("verification_scope") or "unknown"),
                    str(item.get("text_match_level") or "none"),
                    str(item.get("source_resolution") or "none"),
                    str(item.get("writeback_mode") or "none"),
                    json.dumps(item.get("reason_codes") or [], ensure_ascii=False), now,
                ),
            )
        conn.execute(
            """UPDATE citation_assistant_jobs
               SET candidate_count=?,accepted_count=?,unresolved_count=?,out_of_scope_count=?,
                   skipped_no_evidence_count=?,updated_at=? WHERE id=?""",
            (
                actionable, accepted, unresolved, max(0, int(out_of_scope_count)),
                max(0, int(skipped_no_evidence_count)), now, str(job_id),
            ),
        )
        conn.commit()


def _candidate_dict(row) -> dict:
    item = core._candidate_dict(row)
    raw = item.pop("reason_codes_json", "[]")
    try:
        item["reason_codes"] = json.loads(raw or "[]")
    except Exception:
        item["reason_codes"] = []
    return item


def list_candidates(
    job_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    kind: str = "",
    issue: str = "",
    decision: str = "",
    section: str = "",
    bucket: str = "actionable",
) -> dict:
    bucket = bucket if bucket in VALID_REVIEW_BUCKETS else "actionable"
    where = ["job_id=?", "review_bucket=?"]
    args: list[object] = [str(job_id), bucket]
    for column, value in (
        ("kind", kind), ("issue_code", issue), ("decision", decision), ("section_id", section),
    ):
        if value:
            where.append(f"{column}=?")
            args.append(str(value))
    clause = " AND ".join(where)
    page_size = max(1, min(int(page_size), 200))
    page = max(1, int(page))
    with core._connect() as conn:
        summary_row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN decision='pending' THEN 1 ELSE 0 END) AS pending,
                      SUM(CASE WHEN decision='rejected' THEN 1 ELSE 0 END) AS rejected,
                      SUM(CASE WHEN decision='accepted' AND auto_selected=1 THEN 1 ELSE 0 END) AS auto_accepted
               FROM citation_assistant_candidates
               WHERE job_id=? AND review_bucket='actionable'""",
            (str(job_id),),
        ).fetchone()
        unresolved = int(conn.execute(
            "SELECT COUNT(*) FROM citation_assistant_candidates WHERE job_id=? AND review_bucket='unresolved'",
            (str(job_id),),
        ).fetchone()[0])
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM citation_assistant_candidates WHERE {clause}", args,
        ).fetchone()[0])
        rows = conn.execute(
            f"SELECT * FROM citation_assistant_candidates WHERE {clause} "
            "ORDER BY paragraph_index,id LIMIT ? OFFSET ?",
            (*args, page_size, (page - 1) * page_size),
        ).fetchall()
    summary = {
        "total": int(summary_row["total"] or 0),
        "accepted": int(summary_row["accepted"] or 0),
        "pending": int(summary_row["pending"] or 0),
        "rejected": int(summary_row["rejected"] or 0),
        "auto_accepted": int(summary_row["auto_accepted"] or 0),
        "unresolved": unresolved,
    }
    return {
        "total": total, "page": page, "page_size": page_size,
        "summary": summary, "items": [_candidate_dict(row) for row in rows],
    }


def all_candidates(job_id: str, *, bucket: str = "") -> list[dict]:
    sql = "SELECT * FROM citation_assistant_candidates WHERE job_id=?"
    args: list[object] = [str(job_id)]
    if bucket in VALID_REVIEW_BUCKETS:
        sql += " AND review_bucket=?"
        args.append(bucket)
    sql += " ORDER BY paragraph_index,id"
    with core._connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_candidate_dict(row) for row in rows]


def save_decisions(job_id: str, user_id: int, decisions: list[dict]) -> int:
    if not get_job(job_id, user_id):
        raise CitationAssistantError("任务不存在。")
    ids: list[int] = []
    for item in decisions[:2000]:
        try:
            ids.append(int(item.get("id")))
        except (TypeError, ValueError):
            continue
    if ids:
        placeholders = ",".join("?" for _ in ids)
        with core._connect() as conn:
            readonly = conn.execute(
                f"SELECT COUNT(*) FROM citation_assistant_candidates WHERE job_id=? "
                f"AND id IN ({placeholders}) "
                "AND (review_bucket!='actionable' OR match_type='locator')",
                (str(job_id), *ids),
            ).fetchone()[0]
        if readonly:
            raise CitationAssistantError("未能核实项为只读内容，不能采信或写入文件。")
    return core.save_decisions(job_id, user_id, decisions)


def bulk_decide_pending(job_id: str, user_id: int, decision: str) -> dict:
    if decision not in {"accepted", "rejected"}:
        raise CitationAssistantError("批量决定只能是采信或弃用。")
    if not get_job(job_id, user_id):
        raise CitationAssistantError("任务不存在。")
    with core._connect() as conn:
        evidence_guard = " AND match_type!='locator'"
        if decision == "accepted":
            evidence_guard += " AND evidence_level!='paraphrase'"
        cursor = conn.execute(
            "UPDATE citation_assistant_candidates SET decision=? WHERE job_id=? "
            "AND review_bucket='actionable' AND decision='pending'" + evidence_guard,
            (decision, str(job_id)),
        )
        updated = max(0, int(cursor.rowcount or 0))
        accepted = int(conn.execute(
            "SELECT COUNT(*) FROM citation_assistant_candidates WHERE job_id=? "
            "AND review_bucket='actionable' AND decision='accepted'",
            (str(job_id),),
        ).fetchone()[0])
        conn.execute(
            "UPDATE citation_assistant_jobs SET accepted_count=?,updated_at=? WHERE id=?",
            (accepted, core._iso(), str(job_id)),
        )
    return {"updated": updated, "accepted_count": accepted}


def save_agent_run(job_id: str, report: dict) -> None:
    now = core._iso()
    with core._connect() as conn:
        conn.execute(
            """INSERT INTO citation_agent_test_runs
               (job_id,status,mode,model,prompt_version,attempted_record_count,round_count,
                tool_call_count,verified_match_count,direct_verified_match_count,
                viewpoint_suggestion_count,duration_ms,error_code,direct_status,paraphrase_status,
                eligible_record_count,processed_record_count,deferred_record_count,completion_reason,
                created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,mode=excluded.mode,
                 model=excluded.model,prompt_version=excluded.prompt_version,
                 attempted_record_count=excluded.attempted_record_count,
                 round_count=excluded.round_count,tool_call_count=excluded.tool_call_count,
                 verified_match_count=excluded.verified_match_count,
                 direct_verified_match_count=excluded.direct_verified_match_count,
                 viewpoint_suggestion_count=excluded.viewpoint_suggestion_count,
                 duration_ms=excluded.duration_ms,
                 error_code=excluded.error_code,direct_status=excluded.direct_status,
                 paraphrase_status=excluded.paraphrase_status,
                 eligible_record_count=excluded.eligible_record_count,
                 processed_record_count=excluded.processed_record_count,
                 deferred_record_count=excluded.deferred_record_count,
                 completion_reason=excluded.completion_reason,updated_at=excluded.updated_at""",
            (
                str(job_id), str(report.get("status") or "")[:40], str(report.get("mode") or "")[:40],
                str(report.get("model") or "")[:120], str(report.get("prompt_version") or "")[:120],
                int(report.get("attempted_record_count") or 0), int(report.get("round_count") or 0),
                int(report.get("tool_call_count") or 0), int(report.get("verified_match_count") or 0),
                int(report.get("direct_verified_match_count") or 0),
                int(report.get("viewpoint_suggestion_count") or 0),
                int(report.get("duration_ms") or 0), str(report.get("error_code") or "")[:80],
                str(report.get("direct_status") or "not_run")[:40],
                str(report.get("paraphrase_status") or "not_run")[:40],
                int(report.get("eligible_record_count") or 0),
                int(report.get("processed_record_count") or 0),
                int(report.get("deferred_record_count") or 0),
                str(report.get("completion_reason") or "")[:120], now, now,
            ),
        )


def get_agent_run(job_id: str) -> dict | None:
    with core._connect() as conn:
        row = conn.execute("SELECT * FROM citation_agent_test_runs WHERE job_id=?", (str(job_id),)).fetchone()
    return dict(row) if row is not None else None


def _recover_audit_candidate(item: dict, corpus, scope_spec: dict, style: str) -> dict:
    if item.get("source_options") or str(item.get("issue_code") or "") != "unverifiable":
        return item
    anchor = str(item.get("paper_text") or "")
    if len(normalize(anchor)) < 8:
        return item
    try:
        hits = corpus.locate_quote(anchor, per_book_exact=8, allow_fuzzy=True, book_scope=scope_spec)
    except Exception:
        return item
    options: list[dict] = []
    seen: set[tuple] = set()
    for hit in hits[:20]:
        option = core._option_from_hit(hit)
        signature = core._option_signature(option)
        if signature not in seen:
            seen.add(signature)
            options.append(option)
    options = core._prefer_exact_options(options)
    options, reference_disambiguated = core._reference_disambiguate_options(
        options, str(item.get("existing_note_text") or ""), corpus,
    )
    if not options:
        return item
    recovered = dict(item)
    recovered.update({
        "source_options": options,
        "selected_option": 0,
        "match_type": str(options[0].get("match_type") or ""),
        "score": float(options[0].get("score") or 0),
        "fuzzy_errors": options[0].get("fuzzy_errors"),
        "issue_code": "ambiguous" if len(options) > 1 else "format_mismatch",
        "issue_label": core.ISSUE_LABELS["ambiguous" if len(options) > 1 else "format_mismatch"],
        "proposed_citation": core._citation_for_style(options[0], style),
        "auto_selected": False,
        "reference_disambiguated": bool(reference_disambiguated),
    })
    return recovered


def _partition_deterministic(
    candidates: list[dict],
    corpus,
    scope_tokens: list[str],
    style: str,
    *,
    extracted: dict | None = None,
    note_kind: str = "footnote",
) -> tuple[list[dict], dict]:
    scope_spec, _volumes = core._selected_volumes(corpus, scope_tokens)
    paragraphs = _paragraphs_by_index(extracted)
    kept: list[dict] = []
    counts = {"out_of_scope_count": 0, "skipped_no_evidence_count": 0}
    for original in candidates:
        item = _recover_audit_candidate(dict(original), corpus, scope_spec, style)
        base_scope = _verification_scope(item, paragraphs)
        item["evidence_origin"] = "deterministic"
        item = _apply_test_evidence_rules(item, paragraphs, note_kind=note_kind)
        if (
            bool(paragraphs)
            and
            str(item.get("kind") or "") == "generate"
            and item.get("quoted") is False
            and base_scope == "unquoted_text"
        ):
            counts["skipped_no_evidence_count"] += 1
            continue
        if (
            bool(paragraphs)
            and
            str(item.get("kind") or "") == "audit"
            and base_scope == "note_sentence"
            and not item.get("source_options")
        ):
            counts["skipped_no_evidence_count"] += 1
            continue
        if str(item.get("review_bucket") or "") == "unresolved":
            kept.append(item)
            continue
        if str(item.get("verification_scope") or "") == "locator_only":
            kept.append(item)
            continue
        if str(item.get("issue_code") or "") != "unverifiable" or item.get("source_options"):
            item["review_bucket"] = "actionable"
            kept.append(item)
            continue
        note_text = str(item.get("existing_note_text") or "")
        mentioned = core._mentioned_corpus_book(note_text, corpus)
        if mentioned and mentioned not in scope_spec:
            counts["out_of_scope_count"] += 1
            continue
        explicit_bibliography = bool(core._NOTE_BIBLIOGRAPHY_RE.search(note_text))
        if explicit_bibliography and not mentioned:
            counts["skipped_no_evidence_count"] += 1
            continue
        if mentioned and mentioned in scope_spec:
            item.update({
                "review_bucket": "unresolved", "evidence_level": "",
                "issue_label": "疑似属于所选文库，但尚未核实",
                "auto_selected": False, "proposed_citation": str(item.get("existing_note_text") or ""),
            })
            kept.append(item)
        else:
            counts["skipped_no_evidence_count"] += 1
    return kept, counts


def _promote_locator_viewpoints(
    candidates: list[dict], paragraphs: dict[int, dict], recognition_depth: str,
) -> list[dict]:
    """Expose local page locators as non-writing viewpoint suggestions.

    A locator proves only that the cited local page exists; it does not prove
    verbatim agreement.  In the opt-in viewpoint mode we therefore preserve it
    as a manual suggestion with ``paraphrase`` text level and ``locator_only``
    source resolution.  Direct-only tasks keep the same record unresolved and
    it is subsequently count-only.
    """
    if str(recognition_depth or "") != "direct_and_paraphrase":
        return candidates
    promoted: list[dict] = []
    for item in candidates:
        current = dict(item)
        if (
            str(current.get("verification_scope") or "") == "locator_only"
            and bool(current.get("source_options"))
        ):
            current.update({
                "review_bucket": "actionable",
                "evidence_level": "paraphrase",
                "issue_code": "viewpoint_suggestion",
                "issue_label": "观点依据建议；仅页码定位，须人工复核",
                "auto_selected": False,
                "proposed_citation": str(current.get("existing_note_text") or ""),
            })
            current = _apply_test_evidence_rules(current, paragraphs)
        promoted.append(current)
    return promoted


def _filter_mode_additions(additions: list[dict], mode: str) -> list[dict]:
    """Keep Agent discoveries inside the selected insertion/proofreading lane."""
    selected = str(mode or "both")
    if selected == "both":
        return additions
    wanted_kind = "generate" if selected == "generate" else "audit"
    return [
        item for item in additions
        if str(item.get("kind") or "") == wanted_kind
        or str(item.get("evidence_level") or "") == "paraphrase"
    ]


def run_analysis(
    job_id: str,
    corpus,
    *,
    personal_callback: Callable | None = None,
    agent_callback: Callable | None = None,
) -> None:
    row = get_job(job_id)
    if not row:
        return
    acquired = core._MATCH_SEMAPHORE.acquire(timeout=1)
    if not acquired:
        update_job(job_id, status="queued")
        return
    try:
        update_job(
            job_id, status="matching", analysis_stage="deterministic",
            progress_done=0, progress_total=1, error="", agent_status="not_run",
        )
        extracted = json.loads(Path(str(row["extraction_path"])).read_text(encoding="utf-8"))

        def progress(done: int, total: int) -> None:
            update_job(job_id, progress_done=int(done), progress_total=int(total))

        records, _paragraph_norms = core._phrase_records(
            list(extracted.get("paragraphs") or []),
            set(str(x) for x in (row.get("selected_sections") or [])),
            include_readonly_structures=True,
        )
        candidates, resolved, confidence = core.analyze_extraction(
            extracted, corpus,
            selected_sections=list(row.get("selected_sections") or []),
            scope_tokens=list(row.get("scope") or []),
            mode=str(row.get("mode") or "both"),
            threshold="conservative",
            citation_style=str(row.get("citation_style") or "auto"),
            progress=progress, personal_callback=personal_callback, shadow_callback=None,
            include_readonly_structures=True,
        )
        kept, counts = _partition_deterministic(
            candidates, corpus, list(row.get("scope") or []), resolved, extracted=extracted,
            note_kind=str(row.get("note_kind") or "footnote"),
        )
        paragraphs = _paragraphs_by_index(extracted)
        kept = _promote_locator_viewpoints(
            kept, paragraphs, str(row.get("recognition_depth") or "direct_only"),
        )
        report = {"status": "not_run", "mode": "off"}
        if agent_callback:
            update_job(job_id, analysis_stage="agent_planning", agent_status="running")
            try:
                additions, report = agent_callback(
                    job=dict(row), extracted=extracted, records=records,
                    deterministic_candidates=kept, resolved_style=resolved,
                )
            except Exception:
                # Queue corruption, model failure, timeout, or an Agent bug must
                # never discard the already completed deterministic result.
                additions, report = [], {
                    "status": "degraded", "mode": "admin_live",
                    "model": "", "prompt_version": "", "error_code": "agent_internal_error",
                    "attempted_record_count": 0, "round_count": 0,
                    "tool_call_count": 0, "verified_match_count": 0,
                    "direct_verified_match_count": 0, "viewpoint_suggestion_count": 0,
                }
            additions = [
                _apply_test_evidence_rules(
                    dict(item), paragraphs, note_kind=str(row.get("note_kind") or "footnote"),
                )
                for item in (additions or [])
            ]
            additions = _filter_mode_additions(additions, str(row.get("mode") or "both"))
            resolved_audit = {
                (int(item.get("paragraph_index") or 0), int(item.get("existing_note_id") or 0))
                for item in (additions or [])
                if str(item.get("kind") or "") == "audit"
                and str(item.get("review_bucket") or "actionable") == "actionable"
            }
            if resolved_audit:
                kept = [
                    item for item in kept
                    if not (
                        str(item.get("kind") or "") == "audit"
                        and str(item.get("review_bucket") or "") == "unresolved"
                        and (
                            int(item.get("paragraph_index") or 0),
                            int(item.get("existing_note_id") or 0),
                        ) in resolved_audit
                    )
                ]
            kept.extend(additions or [])
            counts["out_of_scope_count"] += int(report.get("out_of_scope_count") or 0)
            counts["skipped_no_evidence_count"] += int(report.get("skipped_no_evidence_count") or 0)
        viewpoint_count = sum(
            str(item.get("evidence_level") or "") == "paraphrase"
            and str(item.get("review_bucket") or "actionable") == "actionable"
            for item in kept
        )
        report["viewpoint_suggestion_count"] = max(
            viewpoint_count, int(report.get("viewpoint_suggestion_count") or 0),
        )
        if (
            str(row.get("recognition_depth") or "") == "direct_and_paraphrase"
            and viewpoint_count
            and str(report.get("paraphrase_status") or "not_run") == "not_run"
        ):
            report["paraphrase_status"] = "complete"
        hidden_no_evidence = [
            item for item in kept
            if str(item.get("review_bucket") or "") == "unresolved"
            and (
                not item.get("source_options")
                or str(item.get("verification_scope") or "") == "locator_only"
            )
        ]
        if hidden_no_evidence:
            counts["skipped_no_evidence_count"] += len(hidden_no_evidence)
            kept = [item for item in kept if item not in hidden_no_evidence]
        update_job(job_id, analysis_stage="finalizing")
        replace_candidates(job_id, kept, **counts)
        agent_status = str(report.get("status") or "not_run")
        if agent_status not in VALID_AGENT_STATUSES:
            agent_status = "degraded"
        save_agent_run(job_id, {**report, "status": agent_status})
        update_job(
            job_id, status="review_ready", resolved_style=resolved,
            style_confidence=float(confidence), progress_done=1, progress_total=1,
            analysis_stage="finalizing", agent_status=agent_status,
            agent_verified_count=int(
                report.get("direct_verified_match_count", report.get("verified_match_count")) or 0
            ),
            viewpoint_suggestion_count=int(report.get("viewpoint_suggestion_count") or 0),
            direct_agent_status=str(report.get("direct_status") or "not_run")[:40],
            paraphrase_agent_status=str(report.get("paraphrase_status") or "not_run")[:40],
            eligible_record_count=int(report.get("eligible_record_count") or 0),
            processed_record_count=int(report.get("processed_record_count") or 0),
            deferred_record_count=int(report.get("deferred_record_count") or 0),
            completion_reason=str(report.get("completion_reason") or "")[:120],
            error="",
        )
    except Exception as exc:
        update_job(job_id, status="failed", agent_status="degraded", error=str(exc)[:500])
    finally:
        core._MATCH_SEMAPHORE.release()


def _exportable_candidates(job_id: str) -> tuple[list[dict], list[dict]]:
    all_records = all_candidates(job_id)
    candidates = [
        item for item in all_records
        if str(item.get("review_bucket") or "actionable") == "actionable"
        if str(item.get("match_type") or "") != "locator"
    ]
    return all_records, candidates


def run_pdf_export(job_id: str) -> None:
    row = get_job(job_id)
    if not row:
        return
    docx_path = Path(str(row.get("output_docx_path") or ""))
    try:
        if str(row.get("mode") or "") not in {"audit", "both"}:
            raise CitationAssistantError("插注模式不生成 PDF。")
        if str(row.get("word_export_status") or "") != "ready" or not docx_path.is_file():
            raise CitationAssistantError("最终 Word 副本不存在，无法重试 PDF。")
        _all_records, candidates = _exportable_candidates(job_id)
        pdf_path = str(core.export_pdf(
            row, candidates, source_docx=docx_path,
            suppress_agent_comments_for_conversion=True,
        ))
    except Exception as exc:
        position_failed = "定位校验失败" in str(exc)
        update_job(
            job_id, status="complete", output_pdf_path="",
            pdf_export_status="position_failed" if position_failed else "failed",
            pdf_position_failure_count=1 if position_failed else 0,
            progress_done=1, progress_total=1,
            error=f"Word 已生成；PDF 未生成：{str(exc)[:360]}",
        )
        return
    update_job(
        job_id, status="complete", output_pdf_path=pdf_path, pdf_export_status="ready",
        pdf_position_failure_count=0, progress_done=1, progress_total=1, error="",
    )


def run_export(job_id: str) -> None:
    row = get_job(job_id)
    if not row:
        return
    try:
        if (
            str(row.get("pdf_export_status") or "") == "converting"
            and str(row.get("word_export_status") or "") == "ready"
            and Path(str(row.get("output_docx_path") or "")).is_file()
        ):
            run_pdf_export(job_id)
            return
        if row.get("status") not in {"review_ready", "exporting", "complete"}:
            raise CitationAssistantError("任务尚未进入导出阶段。")
        update_job(job_id, status="exporting", error="")
        # Read-only unresolved records are deliberately excluded from both Word
        # changes and PDF annotations, not merely marked as rejected.
        all_records, candidates = _exportable_candidates(job_id)
        counts = core._export_candidate_counts(row, all_records)
        docx_path = str(core.export_docx(row, candidates))
        update_job(
            job_id, output_docx_path=docx_path, word_export_status="ready", **counts,
        )
        if str(row.get("mode") or "both") == "generate":
            update_job(
                job_id, status="complete", output_pdf_path="", pdf_export_status="not_requested",
                pdf_position_failure_count=0, progress_done=1, progress_total=1, error="",
            )
            return
        update_job(job_id, pdf_export_status="converting", output_pdf_path="", error="")
        try:
            pdf_path = str(core.export_pdf(
                row, candidates, source_docx=docx_path,
                suppress_agent_comments_for_conversion=True,
            ))
        except Exception as pdf_exc:
            # A missing isolated PDF converter must not discard a successfully
            # generated Word file.
            update_job(
                job_id, status="complete", output_docx_path=docx_path,
                output_pdf_path="", progress_done=1, progress_total=1,
                pdf_export_status=(
                    "position_failed" if "定位校验失败" in str(pdf_exc) else "failed"
                ),
                pdf_position_failure_count=1 if "定位校验失败" in str(pdf_exc) else 0,
                error=f"Word 已生成；PDF 未生成：{str(pdf_exc)[:360]}",
            )
            return
        update_job(
            job_id, status="complete", output_docx_path=docx_path,
            output_pdf_path=pdf_path, pdf_export_status="ready", pdf_position_failure_count=0,
            progress_done=1, progress_total=1, error="",
        )
    except Exception as exc:
        update_job(job_id, status="failed", word_export_status="failed", error=str(exc)[:500])


def delete_job(job_id: str, user_id: int) -> bool:
    removed = core.delete_job(job_id, user_id)
    if removed:
        with core._connect() as conn:
            conn.execute("DELETE FROM citation_agent_test_runs WHERE job_id=?", (str(job_id),))
    return removed


def purge_expired(*args, **kwargs) -> int:
    removed = core.purge_expired(*args, **kwargs)
    with core._connect() as conn:
        conn.execute(
            "DELETE FROM citation_agent_test_runs WHERE job_id IN "
            "(SELECT id FROM citation_assistant_jobs WHERE status IN ('expired','deleted'))"
        )
    return removed


def _job_dir(*args, **kwargs):
    return core._job_dir(*args, **kwargs)


def start_background(*args, **kwargs):
    return core.start_background(*args, **kwargs)


def claim_next_job(*args, **kwargs):
    return core.claim_next_job(*args, **kwargs)


def choose_citation_style(*args, **kwargs):
    return core.choose_citation_style(*args, **kwargs)


def queue_pdf_retry(*args, **kwargs):
    return core.queue_pdf_retry(*args, **kwargs)


def run_extraction(*args, **kwargs):
    return core.run_extraction(*args, **kwargs)
