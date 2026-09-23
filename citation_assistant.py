from __future__ import annotations

"""论文引文助手的持久化、DOCX 解析、原文匹配与产物生成。

模块刻意不依赖 Flask：网页进程、独立 worker 和测试都调用同一套纯 Python
能力。所有含论文正文的数据都放在 ``APPDATA_DIR/citation_assistant`` 下，并由
任务过期/删除流程统一清理。
"""

import copy
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable
from urllib.parse import quote, urljoin

import fitz
from lxml import etree
from rapidfuzz.distance import Levenshtein

from build_index import normalize
from citation_styles import CITATION_FORMAT_KEYS
from runtime_env import APPDATA_DIR

try:  # C 扩展快路径；开发环境缺依赖时仍有确定性的低速兜底。
    import ahocorasick  # type: ignore
except Exception:  # pragma: no cover - 由依赖/部署冒烟覆盖
    ahocorasick = None


DB_PATH = APPDATA_DIR / "citation_assistant.sqlite3"
ARTIFACT_ROOT = APPDATA_DIR / "citation_assistant"

MAX_DOCX_BYTES = max(1, int(os.environ.get("CITATION_ASSISTANT_MAX_DOCX_MB", "30"))) * 1024 * 1024
MAX_EXPANDED_BYTES = max(32, int(os.environ.get("CITATION_ASSISTANT_MAX_EXPANDED_MB", "250"))) * 1024 * 1024
MAX_ENTRIES = max(100, int(os.environ.get("CITATION_ASSISTANT_MAX_ZIP_ENTRIES", "5000")))
MAX_NORMALIZED_CHARS = max(10_000, int(os.environ.get("CITATION_ASSISTANT_MAX_CHARS", "300000")))
MAX_PARAGRAPHS = max(1000, int(os.environ.get("CITATION_ASSISTANT_MAX_PARAGRAPHS", "20000")))
MAX_NOTES = max(100, int(os.environ.get("CITATION_ASSISTANT_MAX_NOTES", "5000")))
MAX_ACTIVE_PER_USER = max(1, int(os.environ.get("CITATION_ASSISTANT_MAX_ACTIVE", "2")))
MAX_JOBS_PER_DAY = max(1, int(os.environ.get("CITATION_ASSISTANT_MAX_JOBS_DAY", "5")))
RETENTION_DAYS = max(1, int(os.environ.get("CITATION_ASSISTANT_RETENTION_DAYS", "7")))
MATCH_CONCURRENCY = max(1, int(os.environ.get("CITATION_ASSISTANT_MATCH_CONCURRENCY", "1")))
REVIEW_MATCH_MIN_SCORE = 92
REVIEW_SEED_QUOTED = 10
REVIEW_SEED_ORDINARY = 12
REVIEW_ANCHORS_PER_RECORD_VOLUME = 6
# If OCR/edit noise is distributed through a quotation, it may contain no
# exact 10-character window at all.  The normal review seeds therefore cannot
# shortlist it even when the complete phrase is still >=92% identical.  Use a
# small set of shorter anchors only after the normal pass misses; the complete
# phrase must still pass the same edit-distance verifier, so these anchors can
# improve recall without weakening adoption or review thresholds.
REVIEW_RESCUE_SEED_QUOTED = 5
REVIEW_RESCUE_MAX_SEEDS = 8
REVIEW_RESCUE_MAX_OCCURRENCES = 16
REVIEW_RESCUE_ANCHORS_PER_RECORD_VOLUME = 12

VALID_MODES = {"generate", "audit", "both"}
VALID_NOTE_KINDS = {"footnote", "endnote"}
VALID_THRESHOLDS = {"conservative", "balanced", "broad"}
VALID_CITATION_STYLES = {"auto", *CITATION_FORMAT_KEYS}
INSERTION_BLUE = "5B9BD5"
ACTIVE_STATUSES = {"extracting", "awaiting_sections", "queued", "matching", "review_ready", "exporting"}
JOB_STATUSES = ACTIVE_STATUSES | {"complete", "failed", "expired", "deleted"}
CORPUS_ANALYSIS_VERSION_ERROR = "语料库版本已变更，请重新创建任务。"
CORPUS_EXPORT_VERSION_ERROR = "语料库版本已变更，为保证结果可复现，已拒绝导出。"
LEGACY_PDF_POSITION_ERROR_PREFIX = (
    "Word 已生成；PDF 未生成：PDF 批注定位校验失败：无法唯一定位"
)
LEGACY_PDF_BODY_ERROR_PREFIX = (
    "Word 已生成；PDF 未生成：批注式 PDF 未充分保留原论文正文版式内容。"
)
LEGACY_PDF_RECOVERY_ERROR_PREFIXES = (
    LEGACY_PDF_POSITION_ERROR_PREFIX,
    LEGACY_PDF_BODY_ERROR_PREFIX,
)
REASON_LABELS = {
    "exact_text": "逐字核对一致",
    "near_text_requires_review": "文字接近，仍需人工复核",
    "paraphrase_never_auto": "属于观点转述，不会自动采用",
    "reference_field_disambiguated": "已通过参考文献交叉核对出处",
    "multiple_reprints": "存在多个转载或版本，需人工选择",
    "locator_without_text_match": "仅核对到页码，尚未核实文字",
    "proofreading_comment_only": "校注意见只写入 Word 批注，不改正文",
    "auto_insert_hard_evidence": "证据充分，可安全自动插注",
    "cross_reference_readonly": "尾注交叉引用无法唯一解析，仅供人工复核",
    "unsafe_ooxml_anchor": "Word 结构复杂，不能安全自动写入",
    "no_local_evidence": "未找到可核验的站内证据",
}
LEGACY_ISSUE_LABELS = {
    "已核验原文；管理员确认后仅以 Word 批注写入":
        "已核验原文；采信后将作为 Word 批注写入，不改动正文",
}

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKGREL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
NS = {"w": W_NS, "r": R_NS, "rel": PKGREL_NS, "ct": CT_NS}

REL_TYPES = {
    "footnote": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes",
    "endnote": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes",
}
CONTENT_TYPES = {
    "footnote": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    "endnote": "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
}
COMMENTS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
COMMENTS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
AGENT_COMMENT_AUTHOR = "引文校注 Agent"
AGENT_COMMENT_INITIALS = "校注"

def _safe_xml_parser() -> etree.XMLParser:
    # lxml parsers may retain state after a failed parse; use a fresh parser per part.
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_blank_text=False,
    )
_HEADING_STYLE_RE = re.compile(r"(?:heading|标题|標題)\s*([1-9])", re.I)
_HEADING_TEXT_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百0-9]+[章节篇]|[一二三四五六七八九十]+[、.]|[0-9]+(?:\.[0-9]+)*[、.\s])"
)
_EXCLUDE_SECTION_RE = re.compile(r"目录|目次|参考文献|參考文獻|bibliography|references", re.I)
_REFERENCE_TAIL_HEADING_RE = re.compile(
    r"^\s*(?:参考文献|參考文獻|references?|bibliograph(?:y|ie))\s*[：:]?\s*$",
    re.I,
)
_QUOTE_RE = re.compile(r"[“\"「『]([^“”\"「」『』\n]{2,})[”\"」』]")
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?", re.M)
_CITATION_PAGE_RE = re.compile(r"第?\s*([0-9]{1,4})(?:\s*[-—–至]\s*([0-9]{1,4}))?\s*页")
_BIBLIOGRAPHY_PAGE_RE = re.compile(
    r"[：:]\s*([0-9]{1,4})(?:\s*[-—–至]\s*([0-9]{1,4}))?\s*[。．.]?\s*$"
)
_CITATION_VOL_RE = re.compile(r"第\s*([0-9]{1,3})\s*[卷册]")
_MANUAL_REFERENCE_RE = re.compile(r"^\s*[［\[]\s*([0-9]{1,3})\s*[］\]]\s*(.+)$")
_MANUAL_REFERENCE_MARKER_RE = re.compile(
    r"[［\[]\s*([0-9]{1,3})\s*[］\]](?P<locator>\s*[0-9]{1,4}(?:\s*[-—–至]\s*[0-9]{1,4})?)?"
)
_CIRCLED_NOTE_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")
_NOTE_SEPARATOR_TYPES = {"separator", "continuationSeparator", "continuationNotice"}
_NOTE_MARKER_OPEN = "[［〔【(（"
_NOTE_MARKER_CLOSE = "]］〕】)）"
_NOTE_BIBLIOGRAPHY_RE = re.compile(
    r"《[^》]{2,}》|(?:\bVol\.|\bNo\.|\bpp?\.|\bPress\b|出版社|载《|DOI\s*:)", re.I,
)
_REFERENCE_FIELD_RE = re.compile(r'HYPERLINK\s+\\l\s+"(Ref_([0-9]{1,4}))"', re.I)
_NOTE_REFERENCE_FIELD_RE = re.compile(
    r'\bNOTEREF\s+(?:"([^"]+)"|([^\s\\]+))', re.I,
)

_JOB_LOCK = threading.RLock()
_MATCH_SEMAPHORE = threading.BoundedSemaphore(MATCH_CONCURRENCY)
_RUNNING_JOBS: set[str] = set()


class CitationAssistantError(ValueError):
    pass


def reason_label(code: object) -> str:
    """Return a readable public label without exposing internal reason codes."""
    return REASON_LABELS.get(str(code or "").strip(), "其他需要人工复核的情况")


def readable_issue_label(candidate: dict) -> str:
    """Keep stored decision data stable while modernizing public wording."""
    label = str(candidate.get("issue_label") or candidate.get("issue_code") or "校注建议")
    return LEGACY_ISSUE_LABELS.get(label, label)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utcnow()).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> Path:
    with _connect() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS citation_assistant_jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                original_filename TEXT NOT NULL DEFAULT '',
                byte_size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'both',
                note_kind TEXT NOT NULL DEFAULT 'footnote',
                threshold_mode TEXT NOT NULL DEFAULT 'conservative',
                citation_style TEXT NOT NULL DEFAULT 'auto',
                resolved_style TEXT NOT NULL DEFAULT '',
                style_confidence REAL NOT NULL DEFAULT 0,
                scope_json TEXT NOT NULL DEFAULT '[]',
                sections_json TEXT NOT NULL DEFAULT '[]',
                selected_sections_json TEXT NOT NULL DEFAULT '[]',
                flags_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'extracting',
                progress_done INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                accepted_count INTEGER NOT NULL DEFAULT 0,
                corpus_sha256 TEXT NOT NULL DEFAULT '',
                template_version TEXT NOT NULL DEFAULT '',
                input_path TEXT NOT NULL DEFAULT '',
                extraction_path TEXT NOT NULL DEFAULT '',
                output_docx_path TEXT NOT NULL DEFAULT '',
                output_pdf_path TEXT NOT NULL DEFAULT '',
                auto_insert_eligible_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                not_inserted_count INTEGER NOT NULL DEFAULT 0,
                proofread_eligible_count INTEGER NOT NULL DEFAULT 0,
                commented_count INTEGER NOT NULL DEFAULT 0,
                readonly_count INTEGER NOT NULL DEFAULT 0,
                word_export_status TEXT NOT NULL DEFAULT 'not_started',
                pdf_export_status TEXT NOT NULL DEFAULT 'not_requested',
                pdf_position_failure_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ca_jobs_user_created
                ON citation_assistant_jobs(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ca_jobs_status
                ON citation_assistant_jobs(status, updated_at);

            CREATE TABLE IF NOT EXISTS citation_assistant_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'generate',
                section_id TEXT NOT NULL DEFAULT '',
                paragraph_index INTEGER NOT NULL DEFAULT -1,
                raw_start INTEGER NOT NULL DEFAULT 0,
                raw_end INTEGER NOT NULL DEFAULT 0,
                paper_text TEXT NOT NULL DEFAULT '',
                existing_note_kind TEXT NOT NULL DEFAULT '',
                existing_note_id INTEGER,
                existing_note_text TEXT NOT NULL DEFAULT '',
                match_type TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL DEFAULT 0,
                fuzzy_errors INTEGER,
                issue_code TEXT NOT NULL DEFAULT '',
                issue_label TEXT NOT NULL DEFAULT '',
                source_options_json TEXT NOT NULL DEFAULT '[]',
                selected_option INTEGER NOT NULL DEFAULT 0,
                proposed_citation TEXT NOT NULL DEFAULT '',
                decision TEXT NOT NULL DEFAULT 'pending',
                auto_selected INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES citation_assistant_jobs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ca_candidates_job
                ON citation_assistant_candidates(job_id, id);
            CREATE INDEX IF NOT EXISTS idx_ca_candidates_filter
                ON citation_assistant_candidates(job_id, kind, issue_code, decision);

            CREATE TABLE IF NOT EXISTS citation_assistant_agent_shadow_runs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                attempted_record_count INTEGER NOT NULL DEFAULT 0,
                planned_record_count INTEGER NOT NULL DEFAULT 0,
                search_count INTEGER NOT NULL DEFAULT 0,
                verified_match_count INTEGER NOT NULL DEFAULT 0,
                incremental_record_count INTEGER NOT NULL DEFAULT 0,
                ambiguous_record_count INTEGER NOT NULL DEFAULT 0,
                reliable_record_count INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                response_sha256 TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES citation_assistant_jobs(id) ON DELETE CASCADE
            );
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(citation_assistant_jobs)")}
        for name, declaration in {
            "lease_owner": "TEXT NOT NULL DEFAULT ''",
            "lease_expires_at": "TEXT NOT NULL DEFAULT ''",
            "auto_insert_eligible_count": "INTEGER NOT NULL DEFAULT 0",
            "inserted_count": "INTEGER NOT NULL DEFAULT 0",
            "not_inserted_count": "INTEGER NOT NULL DEFAULT 0",
            "proofread_eligible_count": "INTEGER NOT NULL DEFAULT 0",
            "commented_count": "INTEGER NOT NULL DEFAULT 0",
            "readonly_count": "INTEGER NOT NULL DEFAULT 0",
            "word_export_status": "TEXT NOT NULL DEFAULT 'not_started'",
            "pdf_export_status": "TEXT NOT NULL DEFAULT 'not_requested'",
            "pdf_position_failure_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE citation_assistant_jobs ADD COLUMN {name} {declaration}")
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ARTIFACT_ROOT, 0o700)
    except OSError:
        pass
    return DB_PATH


def runtime_ready() -> bool:
    try:
        init_db()
        return True
    except Exception:
        return False


def _job_dir(user_id: int, job_id: str, *, create: bool = False) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", str(job_id or "")):
        raise CitationAssistantError("任务编号无效。")
    root = ARTIFACT_ROOT.resolve()
    path = (ARTIFACT_ROOT / str(int(user_id)) / job_id).resolve()
    if root not in path.parents:
        raise CitationAssistantError("任务路径无效。")
    if create:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def _safe_docx_filename(filename: str) -> str:
    name = re.split(r"[\\/]", str(filename or ""))[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")[:240]
    if not name.lower().endswith(".docx"):
        name = (name or "论文") + ".docx"
    return name


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("scope_json", "sections_json", "selected_sections_json", "flags_json"):
        try:
            out[key[:-5] if key.endswith("_json") else key] = json.loads(out.get(key) or "[]")
        except Exception:
            out[key[:-5] if key.endswith("_json") else key] = {} if key == "flags_json" else []
    return out


def count_active_jobs(user_id: int) -> int:
    ph = ",".join("?" for _ in ACTIVE_STATUSES)
    with _connect() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM citation_assistant_jobs WHERE user_id=? AND status IN ({ph})",
            (int(user_id), *sorted(ACTIVE_STATUSES)),
        ).fetchone()[0])


def count_jobs_since(user_id: int, since_iso: str) -> int:
    with _connect() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM citation_assistant_jobs WHERE user_id=? AND created_at>=? AND status!='deleted'",
            (int(user_id), str(since_iso)),
        ).fetchone()[0])


def create_job(
    user_id: int,
    filename: str,
    data: bytes,
    *,
    mode: str = "both",
    note_kind: str = "footnote",
    threshold: str = "conservative",
    citation_style: str = "mkszyj",
    scope_tokens: list[str] | None = None,
    corpus_sha256: str = "",
    template_version: str = "",
) -> dict:
    mode = mode if mode in VALID_MODES else "both"
    note_kind = note_kind if note_kind in VALID_NOTE_KINDS else "footnote"
    threshold = threshold if threshold in VALID_THRESHOLDS else "conservative"
    citation_style = citation_style if citation_style in VALID_CITATION_STYLES else "auto"
    validate_docx_bytes(data, filename=filename)
    job_id = uuid.uuid4().hex
    job_dir = _job_dir(user_id, job_id, create=True)
    input_path = job_dir / "input.docx"
    part = job_dir / ".input.docx.part"
    try:
        part.write_bytes(data)
        part.replace(input_path)
    finally:
        part.unlink(missing_ok=True)
    now = _utcnow()
    expires = now + timedelta(days=RETENTION_DAYS)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO citation_assistant_jobs
               (id,user_id,original_filename,byte_size,sha256,mode,note_kind,threshold_mode,
                citation_style,scope_json,status,corpus_sha256,template_version,input_path,
                created_at,updated_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, int(user_id), _safe_docx_filename(filename), len(data), sha256(data).hexdigest(),
                mode, note_kind, threshold, citation_style,
                json.dumps(scope_tokens or [], ensure_ascii=False), "extracting", str(corpus_sha256),
                str(template_version), str(input_path), _iso(now), _iso(now), _iso(expires),
            ),
        )
    return get_job(job_id, user_id) or {}


def get_job(job_id: str, user_id: int | None = None) -> dict | None:
    sql = "SELECT * FROM citation_assistant_jobs WHERE id=?"
    args: tuple[object, ...] = (str(job_id),)
    if user_id is not None:
        sql += " AND user_id=?"
        args += (int(user_id),)
    with _connect() as conn:
        return _row_dict(conn.execute(sql, args).fetchone())


def list_jobs(user_id: int, limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM citation_assistant_jobs WHERE user_id=? AND status!='deleted' "
            "ORDER BY created_at DESC LIMIT ?",
            (int(user_id), max(1, min(int(limit), 100))),
        ).fetchall()
    return [_row_dict(row) or {} for row in rows]


def update_job(job_id: str, **values: object) -> None:
    allowed = {
        "status", "progress_done", "progress_total", "candidate_count", "accepted_count",
        "resolved_style", "style_confidence", "scope_json", "sections_json", "selected_sections_json",
        "flags_json", "extraction_path", "output_docx_path", "output_pdf_path", "error",
        "auto_insert_eligible_count", "inserted_count", "not_inserted_count",
        "proofread_eligible_count", "commented_count", "readonly_count",
        "word_export_status", "pdf_export_status", "pdf_position_failure_count",
        "lease_owner", "lease_expires_at",
    }
    clean = {key: value for key, value in values.items() if key in allowed}
    if not clean:
        return
    if clean.get("status") in {"awaiting_sections", "review_ready", "complete", "failed", "expired", "deleted"}:
        clean.setdefault("lease_owner", "")
        clean.setdefault("lease_expires_at", "")
    clean["updated_at"] = _iso()
    sets = ",".join(f"{key}=?" for key in clean)
    with _connect() as conn:
        conn.execute(f"UPDATE citation_assistant_jobs SET {sets} WHERE id=?", (*clean.values(), str(job_id)))


def recover_jobs_for_loaded_runtime(
    corpus_sha256: str,
    template_version: str,
    *,
    analysis_errors: Iterable[str],
    export_errors: Iterable[str],
) -> dict[str, int]:
    """Requeue only version-mismatch jobs safe for the loaded runtime.

    A corpus promotion may restart the web process before the standalone citation worker.
    Jobs created by the new web process then already carry the *current* corpus/template
    versions, while the stale worker rejects them using its old in-memory corpus.  Once a
    worker has loaded the matching corpus, this repair is safe and lossless: extraction,
    selected sections and scope stay untouched.  Jobs genuinely created against an older
    corpus do not match the supplied hashes and remain failed for reproducibility.
    """
    corpus_version = str(corpus_sha256 or "").strip()
    template = str(template_version or "").strip()
    analysis_messages = {
        str(message or "").strip() for message in analysis_errors if str(message or "").strip()
    }
    export_messages = {
        str(message or "").strip() for message in export_errors if str(message or "").strip()
    }
    version_errors = sorted(analysis_messages | export_messages)
    if not corpus_version or not template or not version_errors:
        return {"analysis": 0, "export": 0}

    now = _iso()
    recovered = {"analysis": 0, "export": 0}
    error_placeholders = ",".join("?" for _ in version_errors)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id,error,extraction_path,selected_sections_json,candidate_count "
            "FROM citation_assistant_jobs WHERE status='failed' AND corpus_sha256=? "
            f"AND template_version=? AND expires_at>? AND error IN ({error_placeholders})",
            (corpus_version, template, now, *version_errors),
        ).fetchall()
        for row in rows:
            error = str(row[1] or "")
            target = ""
            if error in analysis_messages:
                try:
                    selected = json.loads(str(row[3] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    selected = []
                extraction_path = Path(str(row[2] or ""))
                if selected and extraction_path.is_file():
                    target = "queued"
            elif error in export_messages and int(row[4] or 0) > 0:
                target = "exporting"
            if not target:
                continue
            conn.execute(
                "UPDATE citation_assistant_jobs SET status=?,progress_done=0,progress_total=0,"
                "error='',lease_owner='',lease_expires_at='',updated_at=? WHERE id=? AND status='failed'",
                (target, now, str(row[0])),
            )
            recovered["analysis" if target == "queued" else "export"] += 1
        conn.commit()
    return recovered


def recover_jobs_for_loaded_corpus(corpus_sha256: str, template_version: str) -> dict[str, int]:
    """Backward-compatible recovery for the established public corpus errors."""
    return recover_jobs_for_loaded_runtime(
        corpus_sha256,
        template_version,
        analysis_errors=(CORPUS_ANALYSIS_VERSION_ERROR,),
        export_errors=(CORPUS_EXPORT_VERSION_ERROR,),
    )


def recover_pdf_position_failures(
    corpus_sha256: str,
    template_version: str,
    *,
    error_prefixes: Iterable[str] = LEGACY_PDF_RECOVERY_ERROR_PREFIXES,
) -> int:
    """Requeue PDF-only retries that were rejected by the legacy locator.

    Recovery is deliberately narrower than the interactive retry endpoint.  It
    accepts only a current, unexpired audit/both job whose final Word artifact
    still resolves inside its own job directory and which retains at least one
    exportable proofreading record.  A retry that fails under the new locator
    receives a different error message, so later worker restarts cannot create
    an automatic retry loop.
    """
    corpus_version = str(corpus_sha256 or "").strip()
    template = str(template_version or "").strip()
    prefixes = tuple(dict.fromkeys(
        str(prefix or "").strip() for prefix in error_prefixes if str(prefix or "").strip()
    ))
    if not corpus_version or not template or not prefixes:
        return 0

    now = _iso()
    recovered = 0
    error_clause = " OR ".join("error LIKE ?" for _prefix in prefixes)
    error_values = tuple(f"{prefix}%" for prefix in prefixes)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id,user_id,output_docx_path FROM citation_assistant_jobs "
            "WHERE status='complete' AND mode IN ('audit','both') "
            "AND word_export_status='ready' AND pdf_export_status IN ('position_failed','failed') "
            "AND corpus_sha256=? AND template_version=? AND expires_at>? "
            f"AND ({error_clause}) AND EXISTS ("
            " SELECT 1 FROM citation_assistant_candidates candidate "
            " WHERE candidate.job_id=citation_assistant_jobs.id "
            " AND candidate.kind='audit' AND candidate.decision='accepted' "
            " AND candidate.match_type!='locator'"
            ") ORDER BY created_at",
            (corpus_version, template, now, *error_values),
        ).fetchall()
        for row in rows:
            job_id = str(row[0])
            user_id = int(row[1])
            word_path = Path(str(row[2] or ""))
            try:
                resolved_word = word_path.resolve(strict=True)
                job_dir = _job_dir(user_id, job_id).resolve()
            except (OSError, RuntimeError, CitationAssistantError):
                continue
            if job_dir not in resolved_word.parents or not resolved_word.is_file():
                continue
            changed = conn.execute(
                "UPDATE citation_assistant_jobs SET status='exporting',"
                "pdf_export_status='converting',output_pdf_path='',"
                "pdf_position_failure_count=0,progress_done=0,progress_total=1,"
                "error='',lease_owner='',lease_expires_at='',updated_at=? "
                "WHERE id=? AND status='complete' AND pdf_export_status IN ('position_failed','failed') "
                f"AND ({error_clause})",
                (now, job_id, *error_values),
            ).rowcount
            recovered += int(changed == 1)
        conn.commit()
    return recovered


def set_analysis_config(
    job_id: str,
    user_id: int,
    *,
    section_ids: list[str],
    scope_tokens: list[str],
) -> dict:
    row = get_job(job_id, user_id)
    if not row:
        raise CitationAssistantError("任务不存在。")
    if row["status"] not in {"awaiting_sections", "review_ready", "failed"}:
        raise CitationAssistantError("当前任务状态不能重新分析。")
    valid_sections = {str(s.get("id")) for s in (row.get("sections") or [])}
    selected = [str(x) for x in section_ids if str(x) in valid_sections]
    if not selected:
        raise CitationAssistantError("请至少选择一个需要扫描的章节。")
    update_job(
        job_id,
        selected_sections_json=json.dumps(selected, ensure_ascii=False),
        scope_json=json.dumps(scope_tokens or [], ensure_ascii=False),
        status="queued",
        progress_done=0,
        progress_total=0,
        error="",
    )
    return get_job(job_id, user_id) or {}


def _zip_xml_has_unsafe_declaration(zf: zipfile.ZipFile, name: str) -> bool:
    carry = b""
    with zf.open(name) as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                return False
            probe = (carry + chunk).upper()
            if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                return True
            carry = probe[-16:]


def _ooxml_on_off(value: object) -> bool:
    """Parse the OOXML on/off lexical forms without treating ``"0"`` as truthy."""
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _document_protection_enabled(settings: etree._Element) -> bool:
    enforcement = f"{{{W_NS}}}enforcement"
    return any(
        _ooxml_on_off(node.get(enforcement))
        for node in settings.xpath(".//w:documentProtection", namespaces=NS)
    )


def _package_has_digital_signature(zf: zipfile.ZipFile, names: Iterable[str]) -> bool:
    """Require an actual XMLDSig Signature part, not merely a leftover folder/origin file."""
    for name in names:
        lowered = name.lower()
        leaf = PurePosixPath(lowered).name
        if not (lowered.startswith("_xmlsignatures/") and leaf.startswith("sig") and leaf.endswith(".xml")):
            continue
        try:
            root = _parse_xml(zf.read(name))
        except (KeyError, etree.XMLSyntaxError, CitationAssistantError):
            continue
        qname = etree.QName(root)
        if qname.namespace == DS_NS and qname.localname == "Signature":
            return True
    return False


def validate_docx_bytes(data: bytes, *, filename: str = "") -> dict:
    if filename and not str(filename).lower().endswith(".docx"):
        if str(filename).lower().endswith(".doc"):
            raise CitationAssistantError("旧版 .doc 暂不支持，请先在 Word/WPS 中另存为 .docx。")
        raise CitationAssistantError("目前仅支持 .docx 格式。")
    if not data:
        raise CitationAssistantError("文件为空。")
    if len(data) > MAX_DOCX_BYTES:
        raise CitationAssistantError(f"DOCX 不能超过 {MAX_DOCX_BYTES // 1048576}MB。")
    if not data.startswith(b"PK"):
        raise CitationAssistantError("文件不是有效的 DOCX；加密文档需先解除密码保护。")
    flags = {"signed": False, "protected": False, "tracked_changes": False, "has_textboxes": False}
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise CitationAssistantError("DOCX 内部文件数量异常，已拒绝处理。")
            total = 0
            names: set[str] = set()
            for info in infos:
                p = PurePosixPath(info.filename)
                if p.is_absolute() or ".." in p.parts or "\\" in info.filename:
                    raise CitationAssistantError("DOCX 包含不安全路径，已拒绝处理。")
                if info.flag_bits & 0x1:
                    raise CitationAssistantError("加密 DOCX 暂不支持，请解除密码后重试。")
                total += int(info.file_size)
                if total > MAX_EXPANDED_BYTES:
                    raise CitationAssistantError("DOCX 解压后体积过大，已拒绝处理。")
                if info.filename in names:
                    raise CitationAssistantError("DOCX 包含重复内部路径，已拒绝处理。")
                names.add(info.filename)
            required = {"[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels"}
            if not required.issubset(names):
                raise CitationAssistantError("DOCX 结构不完整或已损坏。")
            lowered = {name.lower() for name in names}
            if "word/vbaproject.bin" in lowered or any(name.endswith(".bin") and "vba" in name for name in lowered):
                raise CitationAssistantError("含宏文档不在支持范围内，请另存为不含宏的 .docx。")
            for xml_name in (name for name in names if name.lower().endswith((".xml", ".rels"))):
                if _zip_xml_has_unsafe_declaration(zf, xml_name):
                    raise CitationAssistantError("DOCX XML 含不安全声明，已拒绝处理。")
            flags["signed"] = _package_has_digital_signature(zf, names)
            doc_xml = zf.read("word/document.xml")
            if b"<!DOCTYPE" in doc_xml.upper() or b"<!ENTITY" in doc_xml.upper():
                raise CitationAssistantError("DOCX XML 含不安全声明，已拒绝处理。")
            root = _parse_xml(doc_xml)
            flags["tracked_changes"] = bool(root.xpath(".//w:ins|.//w:del", namespaces=NS))
            flags["has_textboxes"] = bool(root.xpath(".//w:txbxContent", namespaces=NS))
            if "word/settings.xml" in names:
                settings = _parse_xml(zf.read("word/settings.xml"))
                flags["protected"] = _document_protection_enabled(settings)
    except CitationAssistantError:
        raise
    except (zipfile.BadZipFile, etree.XMLSyntaxError, KeyError, OSError) as exc:
        raise CitationAssistantError("DOCX 无法解析或已经损坏。") from exc
    return flags


def _parse_xml(data: bytes) -> etree._Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise CitationAssistantError("XML 含不安全声明。")
    return etree.fromstring(data, parser=_safe_xml_parser())


def _paragraph_text_nodes(paragraph: etree._Element) -> list[etree._Element]:
    out: list[etree._Element] = []
    for node in paragraph.xpath(".//w:t", namespaces=NS):
        if node.xpath("ancestor::w:del", namespaces=NS):
            continue
        if node.xpath("ancestor::w:txbxContent", namespaces=NS):
            continue
        out.append(node)
    return out


def _paragraph_text(paragraph: etree._Element) -> str:
    return "".join(str(node.text or "") for node in _paragraph_text_nodes(paragraph))


def _safe_comment_spans(paragraph: etree._Element) -> list[list[int]]:
    """Visible ranges that can be split without touching fields/revisions/comments."""
    spans: list[list[int]] = []
    cursor = 0
    field_depth = 0
    for node in paragraph.iter():
        if node.tag == f"{{{W_NS}}}fldChar":
            field_type = str(node.get(f"{{{W_NS}}}fldCharType") or "")
            if field_type == "begin":
                field_depth += 1
            elif field_type == "end":
                field_depth = max(0, field_depth - 1)
            continue
        if node.tag != f"{{{W_NS}}}t" or node.xpath("ancestor::w:del", namespaces=NS):
            continue
        value = str(node.text or "")
        start, end = cursor, cursor + len(value)
        cursor = end
        run = node
        while run is not None and run.tag != f"{{{W_NS}}}r":
            run = run.getparent()
        unsafe_ancestor = bool(node.xpath(
            "ancestor::w:ins|ancestor::w:del|ancestor::w:txbxContent",
            namespaces=NS,
        ))
        simple_run = bool(run is not None) and all(
            child.tag in {f"{{{W_NS}}}rPr", f"{{{W_NS}}}t"} for child in run
        )
        if value and field_depth == 0 and not unsafe_ancestor and simple_run:
            if spans and spans[-1][1] == start:
                spans[-1][1] = end
            else:
                spans.append([start, end])
    marker_offsets: list[int] = []
    cursor = 0
    for node in paragraph.iter():
        if node.tag == f"{{{W_NS}}}t" and not node.xpath("ancestor::w:del", namespaces=NS):
            cursor += len(str(node.text or ""))
        elif node.tag in {
            f"{{{W_NS}}}commentRangeStart", f"{{{W_NS}}}commentRangeEnd",
            f"{{{W_NS}}}commentReference",
        }:
            marker_offsets.append(cursor)
    if marker_offsets:
        split: list[list[int]] = []
        for start, end in spans:
            points = [point for point in marker_offsets if start <= point <= end]
            current = start
            for point in sorted(set(points)):
                if current < point:
                    split.append([current, point])
                current = point
            if current < end:
                split.append([current, end])
        spans = split
    return spans


def _comment_span_is_safe(paragraph: etree._Element, start: int, end: int) -> bool:
    if end <= start:
        return False
    return any(start >= left and end <= right for left, right in _safe_comment_spans(paragraph))


def _manual_note_definitions(paragraphs: list[dict]) -> tuple[dict[int, str], dict[int, str], set[int]]:
    """Read common Chinese-paper manual notes/reference lists.

    Many submitted papers only *look* as if they contain Word endnotes: the body
    contains superscript ``［12］34`` text and the bibliography is ordinary body
    paragraphs.  Those documents have no ``w:endnoteReference`` nodes at all.
    Keep these synthetic notes report-only, but make them visible to the auditor.
    """
    references: dict[int, str] = {}
    circled: dict[int, str] = {}
    definition_paragraphs: set[int] = set()
    for paragraph in paragraphs:
        text = str(paragraph.get("text") or "").strip()
        match = _MANUAL_REFERENCE_RE.match(text)
        if match:
            references[int(match.group(1))] = match.group(2).strip()
            definition_paragraphs.add(int(paragraph["index"]))
            continue
        if text and text[0] in "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳":
            number = ord(text[0]) - ord("①") + 1
            circled[number] = text[1:].lstrip(" 、.．\u3000")
            definition_paragraphs.add(int(paragraph["index"]))
    # Do not classify an isolated bracketed paragraph in the body as a bibliography.
    if len(references) < 2:
        definition_paragraphs.difference_update(
            int(p["index"]) for p in paragraphs if _MANUAL_REFERENCE_RE.match(str(p.get("text") or "").strip())
        )
        references = {}
    return references, circled, definition_paragraphs


def _attach_manual_note_refs(
    paragraphs: list[dict], paragraph_elements: list[etree._Element],
) -> int:
    references, circled, definition_paragraphs = _manual_note_definitions(paragraphs)
    if not references and not circled:
        return 0
    attached = 0
    for paragraph, element in zip(paragraphs, paragraph_elements):
        pindex = int(paragraph["index"])
        if pindex in definition_paragraphs:
            paragraph["reference_list"] = True
            continue
        cursor = 0
        for run in element.xpath(".//w:r", namespaces=NS):
            run_text = "".join(
                str(node.text or "") for node in _paragraph_text_nodes(run)
            )
            vertical = run.xpath("./w:rPr/w:vertAlign/@w:val", namespaces=NS)
            if vertical and str(vertical[0]).lower() == "superscript":
                for match in _MANUAL_REFERENCE_MARKER_RE.finditer(run_text):
                    note_id = int(match.group(1))
                    note_text = references.get(note_id)
                    if not note_text:
                        continue
                    locator = re.sub(r"\s+", "", str(match.group("locator") or ""))
                    paragraph["note_refs"].append({
                        "kind": "manual_endnote", "id": note_id,
                        "offset": cursor + match.start(), "text": note_text,
                        "locator": locator, "marker_text": match.group(0),
                    })
                    attached += 1
                for match in _CIRCLED_NOTE_RE.finditer(run_text):
                    note_id = ord(match.group(0)) - ord("①") + 1
                    note_text = circled.get(note_id)
                    if not note_text:
                        continue
                    paragraph["note_refs"].append({
                        "kind": "manual_endnote", "id": 10000 + note_id,
                        "offset": cursor + match.start(), "text": note_text,
                        "locator": "", "marker_text": match.group(0),
                    })
                    attached += 1
            cursor += len(run_text)
        paragraph["note_refs"].sort(key=lambda item: int(item.get("offset") or 0))
    return attached


def _paragraph_style(paragraph: etree._Element) -> tuple[str, int | None]:
    style = ""
    level: int | None = None
    style_nodes = paragraph.xpath("./w:pPr/w:pStyle", namespaces=NS)
    if style_nodes:
        style = str(style_nodes[0].get(f"{{{W_NS}}}val") or "")
        match = _HEADING_STYLE_RE.search(style)
        if match:
            level = int(match.group(1))
    outline = paragraph.xpath("./w:pPr/w:outlineLvl", namespaces=NS)
    if outline:
        try:
            level = int(outline[0].get(f"{{{W_NS}}}val") or 0) + 1
        except ValueError:
            pass
    return style, level


def _validated_heading_level(text: str, level: int | None, note_refs: list[dict]) -> int | None:
    """Reject body paragraphs that inherit a broken heading/outline style.

    Some journal templates put outline level 9 on ordinary body paragraphs.  A
    style is only a heading signal when the text also looks heading-like; an
    explicit Chinese numeric heading remains valid even if its style is plain.
    """
    clean = str(text or "").strip()
    explicit = bool(clean and len(clean) <= 80 and _HEADING_TEXT_RE.match(clean))
    if explicit:
        return level if level is not None and 1 <= int(level) <= 9 else 1
    if level is None or not clean:
        return None
    if not 1 <= int(level) <= 9:
        return None
    sentence_marks = len(re.findall(r"[。！？!?；;]", clean))
    if len(clean) > 80 or sentence_marks > 1 or note_refs:
        return None
    return int(level)


def _reference_bookmarks(root: etree._Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for marker in root.xpath('.//w:bookmarkStart[starts-with(@w:name,"Ref_")]', namespaces=NS):
        name = str(marker.get(f"{{{W_NS}}}name") or "")
        paragraph = marker
        while paragraph is not None and paragraph.tag != f"{{{W_NS}}}p":
            paragraph = paragraph.getparent()
        if name and paragraph is not None:
            value = _paragraph_text(paragraph).strip()
            if value:
                result[name] = value
    return result


def _paragraph_reference_fields(paragraph: etree._Element, references: dict[str, str]) -> list[dict]:
    """Read bibliography and note cross-reference fields without mutating them."""
    result: list[dict] = []
    cursor = 0
    seen: set[tuple[str, int]] = set()
    for child in paragraph.iter():
        if child.tag == f"{{{W_NS}}}t" and not child.xpath("ancestor::w:del", namespaces=NS):
            cursor += len(str(child.text or ""))
            continue
        if child.tag != f"{{{W_NS}}}instrText":
            continue
        instruction = str(child.text or "")
        for match in _REFERENCE_FIELD_RE.finditer(instruction):
            target, raw_id = match.group(1), match.group(2)
            signature = (target, cursor)
            if signature in seen or not references.get(target):
                continue
            seen.add(signature)
            result.append({
                "kind": "reference_field", "id": int(raw_id), "field_target": target,
                "offset": cursor, "text": references[target], "readonly": True,
            })
        for match in _NOTE_REFERENCE_FIELD_RE.finditer(instruction):
            target = str(match.group(1) or match.group(2) or "").strip()
            signature = (f"NOTEREF:{target}", cursor)
            if not target or signature in seen:
                continue
            seen.add(signature)
            numeric = re.search(r"([0-9]{1,18})$", target)
            result.append({
                "kind": "reference_field",
                "id": int(numeric.group(1)) if numeric else 0,
                "field_target": target,
                "offset": cursor,
                "text": references.get(target, ""),
                "readonly": True,
                "reference_mode": "note_cross_reference",
            })
    return result


def _is_real_note_element(note: etree._Element) -> bool:
    """Classify definitions by semantic type instead of a presumed ID range.

    Word usually gives separators IDs -1/0. WPS may give separators large
    positive IDs and use 0 for the first real note, so ``w:type`` is the only
    portable discriminator.
    """
    return str(note.get(f"{{{W_NS}}}type") or "") not in _NOTE_SEPARATOR_TYPES


def _note_element_text(note: etree._Element, kind: str) -> str:
    paragraphs = list(note.xpath("./w:p", namespaces=NS))
    reference_index = next(
        (
            index for index, paragraph in enumerate(paragraphs)
            if paragraph.xpath(f".//w:{kind}Ref", namespaces=NS)
        ),
        None,
    )
    values: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        value = "".join(str(node.text or "") for node in paragraph.xpath(".//w:t", namespaces=NS))
        # WPS sometimes stores a visible heading inside the first real note.
        if (
            reference_index is not None
            and index < reference_index
            and value.strip().lower()
            in {"注释", "脚注", "尾注", "notes", "footnotes", "endnotes"}
        ):
            continue
        if paragraph.xpath(f".//w:{kind}Ref", namespaces=NS):
            # WPS marker furniture is commonly three runs: "[" + noteRef + "]".
            marker_pattern = (
                rf"^\s*[{re.escape(_NOTE_MARKER_OPEN)}]?"
                rf"\s*[{re.escape(_NOTE_MARKER_CLOSE)}]?\s*"
            )
            value = re.sub(marker_pattern, "", value, count=1)
        value = value.strip()
        if value:
            values.append(value)
    return "\n".join(values).strip()


def _note_part_texts(zf: zipfile.ZipFile, kind: str) -> dict[int, str]:
    name = f"word/{kind}s.xml"
    if name not in zf.namelist():
        return {}
    root = _parse_xml(zf.read(name))
    result: dict[int, str] = {}
    for note in root.xpath(f"./w:{kind}", namespaces=NS):
        try:
            note_id = int(note.get(f"{{{W_NS}}}id") or "")
        except ValueError:
            continue
        if not _is_real_note_element(note):
            continue
        result[note_id] = _note_element_text(note, kind)
    return result


def extract_docx(path: str | Path) -> dict:
    data = Path(path).read_bytes()
    flags = validate_docx_bytes(data, filename=Path(path).name)
    with zipfile.ZipFile(BytesIO(data)) as zf:
        root = _parse_xml(zf.read("word/document.xml"))
        reference_bookmarks = _reference_bookmarks(root)
        footnotes = _note_part_texts(zf, "footnote")
        endnotes = _note_part_texts(zf, "endnote")
        if len(footnotes) + len(endnotes) > MAX_NOTES:
            raise CitationAssistantError("文档中的脚注/尾注数量超过处理上限。")
        paragraphs: list[dict] = []
        paragraph_elements = list(root.xpath("./w:body//w:p", namespaces=NS))
        for idx, paragraph in enumerate(paragraph_elements):
            if idx >= MAX_PARAGRAPHS:
                raise CitationAssistantError("文档段落数量超过处理上限。")
            text = _paragraph_text(paragraph)
            style, heading_level = _paragraph_style(paragraph)
            note_refs: list[dict] = []
            cursor = 0
            for child in paragraph.iter():
                if child.tag == f"{{{W_NS}}}t" and not child.xpath("ancestor::w:del", namespaces=NS):
                    cursor += len(str(child.text or ""))
                elif child.tag in {f"{{{W_NS}}}footnoteReference", f"{{{W_NS}}}endnoteReference"}:
                    kind = "footnote" if child.tag.endswith("footnoteReference") else "endnote"
                    try:
                        nid = int(child.get(f"{{{W_NS}}}id") or "")
                    except ValueError:
                        continue
                    note_refs.append({
                        "kind": kind,
                        "id": nid,
                        "offset": cursor,
                        "text": (footnotes if kind == "footnote" else endnotes).get(nid, ""),
                    })
            note_refs.extend(_paragraph_reference_fields(paragraph, reference_bookmarks))
            note_refs.sort(key=lambda item: int(item.get("offset") or 0))
            heading_level = _validated_heading_level(text, heading_level, note_refs)
            unsupported = bool(paragraph.xpath("ancestor::w:txbxContent", namespaces=NS))
            has_field = bool(paragraph.xpath(".//w:fldChar|.//w:instrText", namespaces=NS))
            tracked = bool(paragraph.xpath(".//w:ins|.//w:del", namespaces=NS))
            paragraphs.append({
                "index": idx,
                "text": text,
                "style": style,
                "heading_level": heading_level,
                "section_id": "",
                "note_refs": note_refs,
                "unsupported": unsupported or has_field or tracked,
                "has_field": has_field,
                "tracked": tracked,
                "safe_comment_spans": _safe_comment_spans(paragraph),
            })
        manual_note_count = _attach_manual_note_refs(paragraphs, paragraph_elements)

    nonempty = [p for p in paragraphs if p["text"].strip()]
    total_norm = sum(len(normalize(p["text"])) for p in nonempty)
    if total_norm > MAX_NORMALIZED_CHARS:
        raise CitationAssistantError("论文正文超过 30 万有效字符上限。")

    headings = [p for p in nonempty if p["heading_level"]]
    # Some journal templates render the bibliography title as an ordinary body
    # paragraph. Treat it as a hard tail boundary even without a heading style,
    # otherwise a selected conclusion can absorb references and the abstract.
    reference_tail = next(
        (
            p for p in nonempty
            if _REFERENCE_TAIL_HEADING_RE.match(str(p.get("text") or "").strip())
        ),
        None,
    )
    if reference_tail is not None and not any(
        int(p["index"]) == int(reference_tail["index"]) for p in headings
    ):
        reference_tail = dict(reference_tail)
        reference_tail["heading_level"] = 1
        headings.append(reference_tail)
        headings.sort(key=lambda item: int(item["index"]))
    sections: list[dict] = []
    if not headings:
        section = {"id": "sec-1", "title": "全文正文", "start": 0, "end": len(paragraphs) - 1, "default_selected": True}
        sections.append(section)
        for p in paragraphs:
            p["section_id"] = section["id"]
    else:
        first_heading_index = min(int(p["index"]) for p in headings)
        opening_title = "正文导语" if first_heading_index == 0 and len(headings) > 1 else "文档开头"
        boundaries = [(0, opening_title, None)] + [
            (int(p["index"]), str(p["text"]).strip()[:100], p["heading_level"])
            for p in headings if int(p["index"]) != 0
        ]
        dedup: list[tuple[int, str, int | None]] = []
        for item in boundaries:
            if dedup and dedup[-1][0] == item[0]:
                dedup[-1] = item
            else:
                dedup.append(item)
        for pos, (start, title, level) in enumerate(dedup):
            end = dedup[pos + 1][0] - 1 if pos + 1 < len(dedup) else len(paragraphs) - 1
            # A real heading may legitimately be the first paragraph.  Only the synthetic
            # pre-heading bucket is an opening section; do not exclude an explicit “正文”.
            default_selected = title != "文档开头" and not bool(_EXCLUDE_SECTION_RE.search(title))
            section = {
                "id": f"sec-{pos + 1}", "title": title or f"第{pos + 1}部分",
                "level": level, "start": start, "end": end, "default_selected": default_selected,
            }
            sections.append(section)
            for p in paragraphs[start:end + 1]:
                p["section_id"] = section["id"]

    flags.update({
        # The original is never overwritten.  A protected/signed source can still
        # produce a writable, unsigned copy; export_docx strips those restrictions
        # only from the generated copy.
        "modify_blocked": False,
        "output_unprotects_copy": bool(flags.get("protected")),
        "output_removes_signature": bool(flags.get("signed")),
        "paragraph_count": len(paragraphs),
        "normalized_chars": total_norm,
        "footnote_count": len(footnotes),
        "endnote_count": len(endnotes),
        "manual_endnote_count": manual_note_count,
        "reference_field_count": len({
            str(note.get("field_target") or note.get("id") or "")
            for paragraph in paragraphs for note in paragraph.get("note_refs", [])
            if note.get("kind") == "reference_field"
        }),
        "reference_field_occurrence_count": sum(
            1 for paragraph in paragraphs for note in paragraph.get("note_refs", [])
            if note.get("kind") == "reference_field"
        ),
    })
    return {"sections": sections, "paragraphs": paragraphs, "flags": flags}


def run_extraction(job_id: str) -> None:
    row = get_job(job_id)
    if not row:
        return
    try:
        extracted = extract_docx(row["input_path"])
        out = _job_dir(int(row["user_id"]), job_id, create=True) / "extraction.json"
        out.write_text(json.dumps(extracted, ensure_ascii=False), encoding="utf-8")
        update_job(
            job_id,
            status="awaiting_sections",
            sections_json=json.dumps(extracted["sections"], ensure_ascii=False),
            selected_sections_json=json.dumps(
                [s["id"] for s in extracted["sections"] if s.get("default_selected")], ensure_ascii=False
            ),
            flags_json=json.dumps(extracted["flags"], ensure_ascii=False),
            extraction_path=str(out),
            progress_done=1,
            progress_total=1,
            error="",
        )
    except Exception as exc:
        update_job(job_id, status="failed", error=str(exc)[:500])


def replace_candidates(job_id: str, candidates: list[dict]) -> None:
    now = _iso()
    with _connect() as conn:
        conn.execute("DELETE FROM citation_assistant_candidates WHERE job_id=?", (str(job_id),))
        for item in candidates:
            conn.execute(
                """INSERT INTO citation_assistant_candidates
                   (job_id,kind,section_id,paragraph_index,raw_start,raw_end,paper_text,
                    existing_note_kind,existing_note_id,existing_note_text,match_type,score,
                    fuzzy_errors,issue_code,issue_label,source_options_json,selected_option,
                    proposed_citation,decision,auto_selected,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(job_id), str(item.get("kind") or "generate"), str(item.get("section_id") or ""),
                    int(item.get("paragraph_index", -1)), int(item.get("raw_start") or 0),
                    int(item.get("raw_end") or 0), str(item.get("paper_text") or ""),
                    str(item.get("existing_note_kind") or ""), item.get("existing_note_id"),
                    str(item.get("existing_note_text") or ""), str(item.get("match_type") or ""),
                    float(item.get("score") or 0), item.get("fuzzy_errors"),
                    str(item.get("issue_code") or ""), str(item.get("issue_label") or ""),
                    json.dumps(item.get("source_options") or [], ensure_ascii=False),
                    int(item.get("selected_option") or 0), str(item.get("proposed_citation") or ""),
                    "accepted" if item.get("auto_selected") else "pending",
                    1 if item.get("auto_selected") else 0, now,
                ),
            )
        accepted = sum(1 for item in candidates if item.get("auto_selected"))
        conn.execute(
            "UPDATE citation_assistant_jobs SET candidate_count=?,accepted_count=?,updated_at=? WHERE id=?",
            (len(candidates), accepted, now, str(job_id)),
        )


def delete_agent_shadow_run(job_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM citation_assistant_agent_shadow_runs WHERE job_id=?",
            (str(job_id),),
        )


def save_agent_shadow_run(job_id: str, report: dict) -> None:
    """Persist privacy-safe aggregate Agent metrics, never prompts or paper text."""
    now = _iso()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO citation_assistant_agent_shadow_runs
               (job_id,status,model,prompt_version,attempted_record_count,planned_record_count,
                search_count,verified_match_count,incremental_record_count,ambiguous_record_count,
                reliable_record_count,duration_ms,response_sha256,error_code,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 status=excluded.status,model=excluded.model,prompt_version=excluded.prompt_version,
                 attempted_record_count=excluded.attempted_record_count,
                 planned_record_count=excluded.planned_record_count,search_count=excluded.search_count,
                 verified_match_count=excluded.verified_match_count,
                 incremental_record_count=excluded.incremental_record_count,
                 ambiguous_record_count=excluded.ambiguous_record_count,
                 reliable_record_count=excluded.reliable_record_count,duration_ms=excluded.duration_ms,
                 response_sha256=excluded.response_sha256,error_code=excluded.error_code,
                 updated_at=excluded.updated_at""",
            (
                str(job_id), str(report.get("status") or "")[:40],
                str(report.get("model") or "")[:120], str(report.get("prompt_version") or "")[:120],
                int(report.get("attempted_record_count") or 0),
                int(report.get("planned_record_count") or 0), int(report.get("search_count") or 0),
                int(report.get("verified_match_count") or 0),
                int(report.get("incremental_record_count") or 0),
                int(report.get("ambiguous_record_count") or 0),
                int(report.get("reliable_record_count") or 0), int(report.get("duration_ms") or 0),
                str(report.get("response_sha256") or "")[:64], str(report.get("error_code") or "")[:80],
                now, now,
            ),
        )


def get_agent_shadow_run(job_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM citation_assistant_agent_shadow_runs WHERE job_id=?",
            (str(job_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def _candidate_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    try:
        out["source_options"] = json.loads(out.pop("source_options_json") or "[]")
    except Exception:
        out["source_options"] = []
    out["auto_selected"] = bool(out.get("auto_selected"))
    return out


def list_candidates(
    job_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    kind: str = "",
    issue: str = "",
    decision: str = "",
    section: str = "",
) -> dict:
    where = ["job_id=?"]
    args: list[object] = [str(job_id)]
    if kind:
        where.append("kind=?")
        args.append(str(kind))
    if issue:
        where.append("issue_code=?")
        args.append(str(issue))
    if decision:
        where.append("decision=?")
        args.append(str(decision))
    if section:
        where.append("section_id=?")
        args.append(str(section))
    clause = " AND ".join(where)
    page_size = max(1, min(int(page_size), 200))
    page = max(1, int(page))
    with _connect() as conn:
        summary_row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN decision='pending' THEN 1 ELSE 0 END) AS pending,
                      SUM(CASE WHEN decision='rejected' THEN 1 ELSE 0 END) AS rejected,
                      SUM(CASE WHEN decision='accepted' AND auto_selected=1 THEN 1 ELSE 0 END) AS auto_accepted
               FROM citation_assistant_candidates WHERE job_id=?""",
            (str(job_id),),
        ).fetchone()
        total = int(conn.execute(f"SELECT COUNT(*) FROM citation_assistant_candidates WHERE {clause}", args).fetchone()[0])
        rows = conn.execute(
            f"SELECT * FROM citation_assistant_candidates WHERE {clause} ORDER BY paragraph_index,id LIMIT ? OFFSET ?",
            (*args, page_size, (page - 1) * page_size),
        ).fetchall()
    summary = {
        "total": int(summary_row["total"] or 0),
        "accepted": int(summary_row["accepted"] or 0),
        "pending": int(summary_row["pending"] or 0),
        "rejected": int(summary_row["rejected"] or 0),
        "auto_accepted": int(summary_row["auto_accepted"] or 0),
    }
    return {
        "total": total, "page": page, "page_size": page_size,
        "summary": summary, "items": [_candidate_dict(r) for r in rows],
    }


def all_candidates(job_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM citation_assistant_candidates WHERE job_id=? ORDER BY paragraph_index,id",
            (str(job_id),),
        ).fetchall()
    return [_candidate_dict(r) for r in rows]


def save_decisions(job_id: str, user_id: int, decisions: list[dict]) -> int:
    if not get_job(job_id, user_id):
        raise CitationAssistantError("任务不存在。")
    with _connect() as conn:
        for item in decisions[:2000]:
            try:
                candidate_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            decision = str(item.get("decision") or "pending")
            if decision not in {"pending", "accepted", "rejected"}:
                continue
            row = conn.execute(
                "SELECT source_options_json FROM citation_assistant_candidates WHERE id=? AND job_id=?",
                (candidate_id, str(job_id)),
            ).fetchone()
            if not row:
                continue
            try:
                options = json.loads(row[0] or "[]")
            except Exception:
                options = []
            selected = max(0, int(item.get("selected_option") or 0))
            if options:
                selected = min(selected, len(options) - 1)
            proposed = str(item.get("proposed_citation") or "")[:2000]
            if not proposed and options:
                proposed = str(options[selected].get("proposed_citation") or options[selected].get("citation") or "")
            conn.execute(
                "UPDATE citation_assistant_candidates SET decision=?,selected_option=?,proposed_citation=? "
                "WHERE id=? AND job_id=?",
                (decision, selected, proposed, candidate_id, str(job_id)),
            )
        accepted = int(conn.execute(
            "SELECT COUNT(*) FROM citation_assistant_candidates WHERE job_id=? AND decision='accepted'",
            (str(job_id),),
        ).fetchone()[0])
        conn.execute(
            "UPDATE citation_assistant_jobs SET accepted_count=?,updated_at=? WHERE id=?",
            (accepted, _iso(), str(job_id)),
        )
    return accepted


def bulk_decide_pending(job_id: str, user_id: int, decision: str) -> dict:
    """Apply one explicit user decision to every still-pending candidate."""
    if decision not in {"accepted", "rejected"}:
        raise CitationAssistantError("批量决定只能是采信或弃用。")
    if not get_job(job_id, user_id):
        raise CitationAssistantError("任务不存在。")
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE citation_assistant_candidates SET decision=? WHERE job_id=? AND decision='pending'",
            (decision, str(job_id)),
        )
        updated = max(0, int(cursor.rowcount or 0))
        accepted = int(conn.execute(
            "SELECT COUNT(*) FROM citation_assistant_candidates WHERE job_id=? AND decision='accepted'",
            (str(job_id),),
        ).fetchone()[0])
        conn.execute(
            "UPDATE citation_assistant_jobs SET accepted_count=?,updated_at=? WHERE id=?",
            (accepted, _iso(), str(job_id)),
        )
    return {"updated": updated, "accepted_count": accepted}


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    raw_indexes: list[int] = []
    for idx, char in enumerate(str(text or "")):
        norm = normalize(char)
        for nchar in norm:
            chars.append(nchar)
            raw_indexes.append(idx)
    return "".join(chars), raw_indexes


def _norm_offset_for_raw(raw_map: list[int], raw_offset: int) -> int:
    lo, hi = 0, len(raw_map)
    while lo < hi:
        mid = (lo + hi) // 2
        if raw_map[mid] < raw_offset:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _selected_volumes(corpus, scope_tokens: list[str]) -> tuple[dict[str, set[int] | None], list]:
    public_tokens = [str(t) for t in scope_tokens if not str(t).startswith("mylib:")]
    if not public_tokens:
        # The assistant serves papers across the whole public collection.  An
        # empty selector therefore means “all public books”, not only the five
        # Marx/Engels collections; exact uniqueness still governs auto-adoption.
        public_tokens = [f"book:{key}" for key in corpus.books]
    spec: dict[str, set[int] | None] = {}
    for token in public_tokens:
        if token.startswith("book:"):
            key = token[5:].strip()
            if key in corpus.books:
                spec[key] = None
        elif token.startswith("vol:"):
            key, _, raw_vol = token[4:].rpartition(":")
            if key in corpus.books and raw_vol.isdigit() and spec.get(key, set()) is not None:
                spec.setdefault(key, set()).add(int(raw_vol))  # type: ignore[union-attr]
        elif token in corpus.books:
            spec[token] = None
    volumes: list = []
    for key in corpus.books:
        if key not in spec:
            continue
        allowed = spec[key]
        volumes.extend(v for v in corpus.books[key] if allowed is None or int(v.volume) in allowed)
    return spec, volumes


def _phrase_records(
    paragraphs: list[dict],
    selected_sections: set[str],
    *,
    include_readonly_structures: bool = False,
) -> tuple[list[dict], dict[int, tuple[str, list[int]]]]:
    records: list[dict] = []
    paragraph_norms: dict[int, tuple[str, list[int]]] = {}
    for paragraph in paragraphs:
        readonly_structure = bool(paragraph.get("unsupported")) and bool(
            paragraph.get("has_field") or paragraph.get("tracked")
        )
        if (
            paragraph.get("section_id") not in selected_sections
            or (
                paragraph.get("unsupported")
                and not (include_readonly_structures and readonly_structure)
            )
            or paragraph.get("reference_list")
        ):
            continue
        text = str(paragraph.get("text") or "")
        if not text.strip():
            continue
        pnorm, pmap = _normalized_with_map(text)
        paragraph_norms[int(paragraph["index"])] = (pnorm, pmap)
        quoted_spans: list[tuple[int, int]] = []
        for match in _QUOTE_RE.finditer(text):
            start, end = match.span(1)
            quoted_spans.append((start, end))
            qnorm = normalize(match.group(1))
            if len(qnorm) < 12:
                continue
            nstart = _norm_offset_for_raw(pmap, start)
            nend = _norm_offset_for_raw(pmap, end)
            records.append({
                "paragraph_index": int(paragraph["index"]), "section_id": paragraph.get("section_id") or "",
                "raw_start": start, "raw_end": end, "norm_start": nstart, "norm_end": nend,
                "citation_start": match.start(), "citation_end": match.end(),
                "norm": pnorm[nstart:nend], "raw_text": match.group(1), "quoted": True,
                "context_text": text[max(0, start - 120):min(len(text), end + 120)],
                "write_blocked": readonly_structure,
            })
        if readonly_structure:
            # The isolated Agent may read explicit quotations from field/tracked
            # paragraphs, but ordinary prose in those paragraphs is not a safe
            # write target and would crowd direct quotations out of the budget.
            continue
        for match in _SENTENCE_RE.finditer(text):
            start, end = match.span()
            # Any sentence touching a quote is handled only by the full-quote
            # record above.  This prevents a short exact fragment inside a longer
            # quotation from receiving a note in the middle of the quotation.
            if any(start < qe and end > qs for qs, qe in quoted_spans):
                continue
            snorm = normalize(match.group())
            if len(snorm) < 24:
                continue
            nstart = _norm_offset_for_raw(pmap, start)
            nend = _norm_offset_for_raw(pmap, end)
            records.append({
                "paragraph_index": int(paragraph["index"]), "section_id": paragraph.get("section_id") or "",
                "raw_start": start, "raw_end": end, "norm_start": nstart, "norm_end": nend,
                "norm": pnorm[nstart:nend], "raw_text": match.group(), "quoted": False,
                "context_text": match.group(),
                "write_blocked": readonly_structure,
            })
    return records, paragraph_norms


def _seed_records(records: list[dict]) -> dict[str, list[dict]]:
    seeds: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        norm = str(record.get("norm") or "")
        seed_len = 12 if record.get("quoted") else 24
        if len(norm) < seed_len:
            continue
        stride = max(6, seed_len // 2)
        starts = list(range(0, max(1, len(norm) - seed_len + 1), stride))
        last = len(norm) - seed_len
        if last >= 0 and last not in starts:
            starts.append(last)
        for local in starts:
            item = dict(record)
            item["seed_local"] = local
            item["seed_len"] = seed_len
            seeds[norm[local:local + seed_len]].append(item)
    return seeds


def _review_seed_records(
    records: list[dict],
    *,
    quoted_len: int = REVIEW_SEED_QUOTED,
    ordinary_len: int = REVIEW_SEED_ORDINARY,
    max_seeds: int = 8,
) -> dict[str, list[dict]]:
    """Build shorter anchors used only to shortlist edit-distance review.

    These anchors never create a candidate by themselves.  A complete paper
    phrase must still pass the 92% local alignment and edit-distance checks.
    """
    seeds: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        norm = str(record.get("norm") or "")
        record_identity = (
            int(record.get("paragraph_index") or 0),
            int(record.get("raw_start") or 0),
            int(record.get("raw_end") or 0),
            bool(record.get("quoted")),
        )
        seed_len = quoted_len if record.get("quoted") else ordinary_len
        if len(norm) < seed_len:
            continue
        stride = max(seed_len, len(norm) // 6)
        starts = list(range(0, max(1, len(norm) - seed_len + 1), stride))
        last = len(norm) - seed_len
        if last >= 0 and last not in starts:
            starts.append(last)
        for local in starts[:max_seeds]:
            item = dict(record)
            item["seed_local"] = local
            item["seed_len"] = seed_len
            item["record_identity"] = record_identity
            seeds[norm[local:local + seed_len]].append(item)
    return seeds


def _record_identity(record: dict) -> tuple[int, int, int, bool]:
    return (
        int(record.get("paragraph_index") or 0),
        int(record.get("raw_start") or 0),
        int(record.get("raw_end") or 0),
        bool(record.get("quoted")),
    )


def _review_allowed_errors(length: int) -> int:
    return min(24, max(1, math.ceil(length * (100 - REVIEW_MATCH_MIN_SCORE) / 100)))


def _align_near_seed(record: dict, source: str, estimated_start: int) -> tuple[int, int, int, int] | None:
    """Verify one seed-shortlisted source window against the complete phrase."""
    query = str(record.get("norm") or "")
    if not query:
        return None
    max_errors = _review_allowed_errors(len(query))
    # An exact seed fixes the approximate start.  Enumerating only the possible
    # edit drift around it is both more reliable than partial_ratio (which
    # under-scores insertions) and far cheaper than a full-corpus fuzzy scan.
    start_low = max(0, int(estimated_start) - max_errors)
    start_high = min(len(source), int(estimated_start) + max_errors)
    best: tuple[int, int, int, float] | None = None
    query_len = len(query)
    for source_start in range(start_low, start_high + 1):
        min_length = max(1, query_len - max_errors)
        max_length = min(len(source) - source_start, query_len + max_errors)
        for source_length in range(min_length, max_length + 1):
            if best is not None and abs(query_len - source_length) > best[2]:
                continue
            source_end = source_start + source_length
            cutoff = max_errors if best is None else min(max_errors, best[2] - 1)
            if cutoff < 0:
                continue
            errors = int(Levenshtein.distance(
                query, source[source_start:source_end], score_cutoff=cutoff,
            ))
            if errors > cutoff:
                continue
            score = 100 * (1 - errors / max(query_len, source_length, 1))
            if score + 1e-9 < REVIEW_MATCH_MIN_SCORE:
                continue
            candidate = (source_start, source_end, errors, score)
            if best is None or (candidate[2], -candidate[3], abs(source_start - estimated_start)) < (
                best[2], -best[3], abs(best[0] - estimated_start)
            ):
                best = candidate
    if best is None:
        return None
    return best[0], best[1], int(round(best[3])), best[2]


def _build_seed_matcher(seeds: dict[str, list[dict]]):
    if ahocorasick is None or not seeds:
        return None
    automaton = ahocorasick.Automaton()
    for seed in seeds:
        automaton.add_word(seed, seed)
    automaton.make_automaton()
    return automaton


def _iter_seed_hits(
    text: str,
    seeds: dict[str, list[dict]],
    matcher=None,
) -> Iterable[tuple[int, str]]:
    if matcher is not None:
        for end, seed in matcher.iter(text):
            yield int(end) - len(seed) + 1, str(seed)
        return
    for seed in seeds:  # pragma: no cover - 小型开发兜底
        start = 0
        while True:
            pos = text.find(seed, start)
            if pos < 0:
                break
            yield pos, seed
            start = pos + 1


def _extend_exact(paper: str, pseed: int, source: str, sseed: int, seed_len: int) -> tuple[int, int, int, int]:
    pleft, sleft = pseed, sseed
    while pleft > 0 and sleft > 0 and paper[pleft - 1] == source[sleft - 1]:
        pleft -= 1
        sleft -= 1
    pright, sright = pseed + seed_len, sseed + seed_len
    while pright < len(paper) and sright < len(source) and paper[pright] == source[sright]:
        pright += 1
        sright += 1
    return pleft, pright, sleft, sright


def _anchor_end(text: str, raw_end: int) -> int:
    end = max(0, min(int(raw_end), len(text)))
    while end < len(text) and text[end] in "”\"」』）)。！？；;!?，,":
        end += 1
    return end


def _option_from_hit(hit, *, personal: bool = False, personal_confidence: float = 1.0) -> dict:
    item = hit.to_dict() if hasattr(hit, "to_dict") else dict(hit)
    citations = dict(item.get("citations") or {})
    if "gb2025" not in citations:
        citations["gb2025"] = citations.get("gb2015") or item.get("citation") or ""
    pdf_pages = item.get("pdf_pages") or []
    source_file = item.get("source_file") or ""
    return {
        "book": item.get("book") or "",
        "volume": item.get("volume") or 0,
        "source_file": source_file,
        "display_title": item.get("display_title") or item.get("book_title") or "",
        "citation_title": item.get("citation_title") or item.get("book_title") or "",
        "pdf_pages": pdf_pages,
        "printed_pages": item.get("printed_pages") or [],
        "section_title": item.get("section_title") or "",
        "context": item.get("context") or "",
        "citation": item.get("citation") or "",
        "citations": citations,
        "match_type": item.get("match_type") or "",
        "score": int(item.get("score") or 0),
        "fuzzy_errors": item.get("fuzzy_errors"),
        "personal": bool(personal),
        "page_confidence": float(personal_confidence),
        "viewer_url": (
            f"/reader?mode=reader&file={quote(str(source_file))}&page={int(pdf_pages[0])}"
            if source_file and pdf_pages else ""
        ),
    }


def _option_signature(option: dict) -> tuple:
    return (
        option.get("book"), int(option.get("volume") or 0), option.get("source_file"),
        tuple(option.get("pdf_pages") or []), tuple(option.get("printed_pages") or []),
    )


def _citation_for_style(option: dict, style: str) -> str:
    citations = option.get("citations") or {}
    if style == "auto":
        style = "gb2025"
    return str(citations.get(style) or citations.get("gb2025") or citations.get("gb2015") or option.get("citation") or "")


def choose_citation_style(job_id: str, user_id: int, style: str) -> dict:
    """Resolve a failed auto-detection before export and refresh safe generated notes."""
    style = str(style or "").strip()
    if style not in VALID_CITATION_STYLES - {"auto"}:
        raise CitationAssistantError("请选择有效的引文格式。")
    row = get_job(job_id, user_id)
    if not row:
        raise CitationAssistantError("任务不存在。")
    if row.get("status") != "review_ready":
        raise CitationAssistantError("只有待审核任务可以修改引文格式。")
    with _connect() as conn:
        candidates = conn.execute(
            "SELECT id,kind,source_options_json,selected_option FROM citation_assistant_candidates "
            "WHERE job_id=?",
            (str(job_id),),
        ).fetchall()
        for candidate in candidates:
            # Existing mixed/comment notes may contain user prose. Do not rewrite them wholesale.
            if str(candidate[1] or "") != "generate":
                continue
            try:
                options = json.loads(candidate[2] or "[]")
            except Exception:
                options = []
            selected = max(0, int(candidate[3] or 0))
            if not options:
                continue
            selected = min(selected, len(options) - 1)
            proposed = _citation_for_style(options[selected], style)
            conn.execute(
                "UPDATE citation_assistant_candidates SET proposed_citation=? WHERE id=? AND job_id=?",
                (proposed[:2000], int(candidate[0]), str(job_id)),
            )
        conn.execute(
            "UPDATE citation_assistant_jobs SET citation_style=?,resolved_style=?,style_confidence=1,updated_at=? "
            "WHERE id=? AND user_id=?",
            (style, style, _iso(), str(job_id), int(user_id)),
        )
    return get_job(job_id, user_id) or row


def _is_reliable_page(option: dict) -> bool:
    printed = [str(x or "") for x in (option.get("printed_pages") or [])]
    if option.get("personal") and float(option.get("page_confidence") or 0) < 0.70:
        return False
    return bool(printed and all(x and not x.startswith("pre-") for x in printed))


def _auto_select(match_type: str, score: float, errors: int | None, options: list[dict], threshold: str) -> bool:
    # The simplified workflow has one invariant instead of three presets:
    # only a unique, character-exact hit with a dependable printed page is safe
    # to adopt without review.  Every fuzzy, ambiguous or low-page-confidence
    # result remains pending regardless of a legacy task's threshold value.
    if len(options) != 1:
        return False
    return (
        match_type == "exact"
        and float(score) == 100.0
        and int(errors or 0) == 0
        and _is_reliable_page(options[0])
    )


def _candidate_has_existing_note(paragraph: dict, raw_end: int) -> bool:
    """Return whether an existing note already belongs to this paper span.

    Word places a note reference immediately before or after closing punctuation
    depending on the author's style.  A four-character tolerance covers closing
    quotes plus terminal punctuation and prevents generate+audit duplicates.
    """
    return any(
        abs(int(ref.get("offset") or 0) - int(raw_end)) <= 4
        for ref in (paragraph.get("note_refs") or [])
    )


def _detect_style(notes: list[str], requested: str) -> tuple[str, float]:
    if requested != "auto":
        return requested, 1.0
    # 自动识别只确认有足够文本特征的“格式族”。[M] 可以证明是国标型，
    # 却不足以区分 2015/2025；多本期刊使用完全相同的脚注时也不猜具体刊名。
    # 因此仅把家族代表键作为候选返回，置低置信度，由用户明确选择后
    # 才保存确切期刊键。
    labels: list[str] = []
    for note in notes:
        if "[M]" in note or "［M］" in note:
            labels.append("gb2015")
        elif "人民出版社" in note or "中央文献出版社" in note or re.search(r"，\s*(?:北京|上海|重庆)[：:]", note):
            labels.append("zgshkx")
    if len(labels) >= 3:
        winner = max(set(labels), key=labels.count)
        confidence = labels.count(winner) / len(labels)
        if confidence >= 0.80:
            return winner, 0.79  # 只能到格式族，不能越过具体格式的人工确认闸门
    return "gb2015", 0.0


ISSUE_LABELS = {
    "suggest_add": "建议新增注释",
    "verified": "已核验",
    "text_mismatch": "引文文字有出入",
    "wrong_page": "页码错误",
    "wrong_source": "著作或卷次错误",
    "wrong_version": "版本信息错误",
    "format_mismatch": "注释格式不符",
    "ambiguous": "多个可能出处",
    "unverifiable": "本站无法核验",
    "unsupported": "文档结构不支持",
}


def _trim_note_anchor_span(text: str, start: int, end: int) -> tuple[int, int, str]:
    start = max(0, min(int(start), len(text)))
    end = max(start, min(int(end), len(text)))
    while start < end:
        while start < end and text[start].isspace():
            start += 1
        probe = start + 1
        while probe < end and text[probe].isspace():
            probe += 1
        if (
            start < end and text[start] in _NOTE_MARKER_OPEN
            and probe < end and text[probe] in _NOTE_MARKER_CLOSE
        ):
            start = probe + 1
            continue
        if start < end and text[start] in _NOTE_MARKER_CLOSE:
            start += 1
            continue
        break
    while end > start and (text[end - 1].isspace() or text[end - 1] in _NOTE_MARKER_OPEN):
        end -= 1
    return start, end, text[start:end].strip()


def _note_anchor(text: str, offset: int) -> tuple[int, int, str]:
    boundary = max(0, min(int(offset), len(text)))
    while boundary > 0 and text[boundary - 1].isspace():
        boundary -= 1
    # A WPS note reference can sit between literal "[" and "]" runs.  The
    # opening bracket is immediately before the zero-width OOXML reference.
    if boundary > 0 and text[boundary - 1] in _NOTE_MARKER_OPEN:
        boundary -= 1
        while boundary > 0 and text[boundary - 1].isspace():
            boundary -= 1
    before = text[:boundary]
    sentences = list(_SENTENCE_RE.finditer(before))
    sentence_span: tuple[int, int, str] | None = None
    for sentence in reversed(sentences):
        candidate = _trim_note_anchor_span(text, *sentence.span())
        if len(normalize(candidate[2])) >= 4:
            sentence_span = candidate
            break
    sentence_start = sentence_span[0] if sentence_span else 0
    quotes = [
        match for match in _QUOTE_RE.finditer(before)
        if match.end() >= sentence_start
        and len(normalize(match.group(1))) >= 6
        and len(normalize(before[match.end():boundary])) <= 40
    ]
    if quotes:
        anchor_start, anchor_end = quotes[-1].span(1)
        anchor_text = quotes[-1].group(1)
    elif sentence_span:
        anchor_start, anchor_end, anchor_text = sentence_span
    else:
        anchor_start, anchor_end = max(0, boundary - 100), boundary
        anchor_start, anchor_end, anchor_text = _trim_note_anchor_span(
            text, anchor_start, anchor_end,
        )
    if not quotes and len(normalize(anchor_text)) > 120:
        anchor_text = anchor_text[-120:]
        anchor_start = max(0, anchor_end - len(anchor_text))
    return anchor_start, anchor_end, anchor_text


def _mentioned_corpus_book(note_text: str, corpus) -> str:
    bracketed_titles = [value.strip() for value in re.findall(r"《([^》]{1,120})》", note_text)]
    aliases: list[tuple[int, str, str]] = []
    for config in getattr(corpus, "book_configs", []):
        key = str(getattr(config, "key", "") or "")
        if not key:
            continue
        raw_aliases = {
            str(getattr(config, "title", "") or ""),
            str(getattr(config, "short_title", "") or ""),
            str(getattr(config, "citation_title", "") or ""),
        }
        if len(normalize(key)) >= 3:
            raw_aliases.add(key)
        for alias in raw_aliases:
            alias = alias.strip()
            if not alias:
                continue
            clean = alias.replace("《", "").replace("》", "").strip()
            exact_bracketed = bool(
                bracketed_titles
                and any(normalize(title) == normalize(clean) for title in bracketed_titles)
            )
            if not bracketed_titles or "《" in alias or exact_bracketed:
                aliases.append((len(normalize(alias)), alias, key))
            # If the note already contains explicit Chinese book/article titles,
            # never match a short corpus name merely because it is nested inside a
            # different title (for example a monograph *about* Capital).
            if len(normalize(clean)) >= 3 and not bracketed_titles:
                aliases.append((len(normalize(clean)), clean, key))
            if exact_bracketed:
                aliases.append((len(normalize(clean)) + 1000, f"《{clean}》", key))
    for _length, alias, key in sorted(aliases, key=lambda item: (-item[0], item[2])):
        if alias in note_text:
            return key
    return ""


def _citation_locator_options(
    corpus, mentioned_book: str, page_probe: str, scope_spec: dict,
) -> tuple[list[dict], str]:
    """Resolve an explicitly cited in-library volume/page without semantic guessing.

    A locator-only result exposes the cited page for human verification but never
    claims that the surrounding paraphrase is textually verified.
    """
    if not mentioned_book or mentioned_book not in scope_spec:
        return [], ""
    page_match = _CITATION_PAGE_RE.search(page_probe)
    if not page_match:
        return [], ""
    volumes = list((getattr(corpus, "books", {}) or {}).get(mentioned_book) or [])
    allowed = scope_spec.get(mentioned_book)
    if allowed is not None:
        volumes = [volume for volume in volumes if int(volume.volume) in allowed]
    volume_match = _CITATION_VOL_RE.search(page_probe)
    if volume_match:
        requested_volume = int(volume_match.group(1))
        volumes = [volume for volume in volumes if int(volume.volume) == requested_volume]
        if not volumes:
            return [], "wrong_source"
    first_page = int(page_match.group(1))
    last_page = int(page_match.group(2) or first_page)
    if last_page < first_page:
        first_page, last_page = last_page, first_page
    options: list[dict] = []
    for volume in volumes:
        page_indexes = [
            index for index, page in enumerate(getattr(volume, "pages", []))
            if str(getattr(page, "printed_page", "") or "").isdigit()
            and first_page <= int(str(page.printed_page)) <= last_page
        ]
        if not page_indexes or not getattr(volume, "page_offsets", None):
            continue
        start_index, end_index = min(page_indexes), max(page_indexes)
        norm_start = int(volume.page_offsets[start_index])
        norm_end = int(volume.page_offsets[end_index + 1])
        sample_text = str(volume.pages[start_index].raw_text or "").strip()[:120]
        try:
            hit = corpus._make_hit(
                volume, norm_start, max(norm_start + 1, norm_end),
                "locator", 0, sample_text or str(first_page),
            )
        except Exception:
            continue
        options.append(_option_from_hit(hit))
    return options[:20], ("" if options else "wrong_page")


def _prefer_exact_options(options: list[dict]) -> list[dict]:
    exact = [item for item in options if str(item.get("match_type") or "") == "exact"]
    return exact or options


def _reference_disambiguate_options(
    options: list[dict], note_text: str, corpus,
) -> tuple[list[dict], bool]:
    """Use explicit bibliography metadata only when it uniquely selects a copy."""
    if len(options) <= 1:
        return options, False
    filtered = list(options)
    mentioned = _mentioned_corpus_book(note_text, corpus)
    volume_match = _CITATION_VOL_RE.search(note_text)
    page_match = _CITATION_PAGE_RE.search(note_text) or _BIBLIOGRAPHY_PAGE_RE.search(note_text)
    if mentioned:
        by_book = [item for item in filtered if str(item.get("book") or "") == mentioned]
        if by_book:
            filtered = by_book
    if volume_match:
        by_volume = [
            item for item in filtered
            if int(item.get("volume") or 0) == int(volume_match.group(1))
        ]
        if by_volume:
            filtered = by_volume
    if page_match:
        cited = {page_match.group(1), page_match.group(2) or page_match.group(1)}
        by_page = [
            item for item in filtered
            if cited & {str(page) for page in item.get("printed_pages") or [] if page}
        ]
        if by_page:
            filtered = by_page
    return (filtered, True) if len(filtered) == 1 and len(options) > 1 else (options, False)


def _locate_ellipsis_quote(corpus, text: str, scope_spec: dict) -> list:
    """Match every ellipsis-separated segment in order inside one source window."""
    raw_segments = re.split(r"(?:\.{3,}|…+|⋯+)", str(text or ""))
    segments = [normalize(segment) for segment in raw_segments if len(normalize(segment)) >= 6]
    if len(segments) < 2:
        return []
    hits: list = []
    for book, allowed in scope_spec.items():
        for volume in list((getattr(corpus, "books", {}) or {}).get(book) or []):
            if allowed is not None and int(volume.volume) not in allowed:
                continue
            source = str(getattr(volume, "norm_full", "") or "")
            search_from = 0
            while len(hits) < 20:
                first = source.find(segments[0], search_from)
                if first < 0:
                    break
                cursor = first + len(segments[0])
                end = cursor
                valid = True
                for segment in segments[1:]:
                    found = source.find(segment, cursor, min(len(source), first + 8000))
                    if found < 0:
                        valid = False
                        break
                    cursor = found + len(segment)
                    end = cursor
                if valid:
                    try:
                        hits.append(corpus._make_hit(volume, first, end, "exact", 100, text))
                    except Exception:
                        pass
                search_from = first + 1
    return hits


def _existing_note_candidate(
    paragraph: dict,
    note: dict,
    corpus,
    scope_spec: dict,
    style: str,
    previous_option: dict | None = None,
) -> dict:
    text = str(paragraph.get("text") or "")
    offset = max(0, min(int(note.get("offset") or 0), len(text)))
    note_text = str(note.get("text") or "")
    anchor_start, anchor_end, anchor_text = _note_anchor(text, offset)
    ibid = bool(re.search(r"(?:同上|同前注|ibid\.?|ibidem)", note_text, re.I))
    inherited_option = dict(previous_option) if ibid and previous_option else None
    mentioned_book = (
        str(inherited_option.get("book") or "")
        if inherited_option else _mentioned_corpus_book(note_text, corpus)
    )
    volume_match = _CITATION_VOL_RE.search(note_text)
    explicit_external_source = bool(_NOTE_BIBLIOGRAPHY_RE.search(note_text)) and not mentioned_book
    lookup_scope = scope_spec
    can_search = (
        len(normalize(anchor_text)) >= 8
        and not explicit_external_source
        and not (ibid and not inherited_option)
    )
    if mentioned_book:
        can_search = can_search and mentioned_book in scope_spec
        if can_search:
            allowed = scope_spec.get(mentioned_book)
            if volume_match:
                requested_volume = int(volume_match.group(1))
                if allowed is not None and requested_volume not in allowed:
                    can_search = False
                else:
                    lookup_scope = {mentioned_book: {requested_volume}}
            elif inherited_option and int(inherited_option.get("volume") or 0) > 0:
                inherited_volume = int(inherited_option.get("volume") or 0)
                if allowed is not None and inherited_volume not in allowed:
                    can_search = False
                else:
                    lookup_scope = {mentioned_book: {inherited_volume}}
            else:
                lookup_scope = {mentioned_book: allowed}
    hits = []
    if can_search:
        hits = _locate_ellipsis_quote(corpus, anchor_text, lookup_scope)
        if not hits:
            hits = corpus.locate_quote(
                anchor_text, per_book_exact=8, allow_fuzzy=True, book_scope=lookup_scope,
            )
    # A cited book/volume is evidence to check first, not proof that the citation
    # is correct.  Otherwise a note that names the wrong edition can never be
    # classified as wrong_source/wrong_version.  Search the wider user-selected
    # scope for exact copies, and only pay for a wider fuzzy scan when the claimed
    # source did not yield a textual match.
    if can_search and mentioned_book and lookup_scope != scope_spec:
        narrow_exact = any(str(getattr(hit, "match_type", "")) == "exact" for hit in hits)
        if not narrow_exact:
            wider_exact = corpus.locate_quote(
                anchor_text, per_book_exact=8, allow_fuzzy=False, book_scope=scope_spec,
            )
            if wider_exact:
                hits = wider_exact
            else:
                wider_fuzzy = corpus.locate_quote(
                    anchor_text, per_book_exact=8, allow_fuzzy=True, book_scope=scope_spec,
                )
                if wider_fuzzy:
                    hits = wider_fuzzy
    options: list[dict] = []
    seen: set[tuple] = set()
    for hit in hits[:20]:
        option = _option_from_hit(hit)
        sig = _option_signature(option)
        if sig not in seen:
            seen.add(sig)
            options.append(option)
    options = _prefer_exact_options(options)
    options, reference_disambiguated = _reference_disambiguate_options(options, note_text, corpus)
    locator_only = False
    locator_failure = ""
    page_probe = note_text
    locator = str(note.get("locator") or "").strip()
    if locator:
        page_probe += f" 第{locator}页"
    locator_scope = scope_spec
    if inherited_option and mentioned_book and int(inherited_option.get("volume") or 0) > 0:
        locator_scope = {mentioned_book: {int(inherited_option.get("volume") or 0)}}
    if not options and inherited_option and _CITATION_PAGE_RE.search(page_probe):
        locator_options, locator_failure = _citation_locator_options(
            corpus, mentioned_book, page_probe, locator_scope,
        )
        if locator_options:
            options = locator_options
            locator_only = True
    if not options and inherited_option:
        options = [inherited_option]
        locator_only = True
    if not options and mentioned_book:
        locator_options, locator_failure = _citation_locator_options(
            corpus, mentioned_book, page_probe, scope_spec,
        )
        if locator_options:
            options = locator_options
            locator_only = True
    issue = locator_failure or "unverifiable"
    if options:
        issue = "ambiguous" if len(options) > 1 else ("unverifiable" if locator_only else "verified")
        best = options[0]
        if not locator_only and str(best.get("match_type") or "") == "fuzzy":
            issue = "text_mismatch"
        page_match = _CITATION_PAGE_RE.search(page_probe)
        if page_match and not locator_only:
            cited_pages = {page_match.group(1), page_match.group(2) or page_match.group(1)}
            actual = {str(x) for x in (best.get("printed_pages") or []) if x}
            if actual and not (actual & cited_pages):
                issue = "wrong_page"
        if volume_match and int(volume_match.group(1)) != int(best.get("volume") or 0):
            issue = "wrong_source"
        if mentioned_book and mentioned_book != best.get("book"):
            mentioned_title = str(corpus.get_book_config(mentioned_book).title or "")
            actual_title = str(corpus.get_book_config(str(best.get("book") or "")).title or "")
            same_series = "马克思恩格斯全集" in mentioned_title and "马克思恩格斯全集" in actual_title
            issue = "wrong_version" if same_series else "wrong_source"
        if issue == "verified" and not locator_only and not ibid:
            expected = _citation_for_style(best, style)
            if normalize(expected) != normalize(note_text):
                issue = "format_mismatch"
    proposed = _citation_for_style(options[0], style) if options else note_text
    display_note_text = note_text
    if locator and not _CITATION_PAGE_RE.search(note_text):
        display_note_text = f"{note_text}（文中标注页码：{locator}）"
    return {
        "kind": "audit", "section_id": paragraph.get("section_id") or "",
        "paragraph_index": int(paragraph["index"]), "raw_start": anchor_start, "raw_end": anchor_end,
        "paper_text": anchor_text, "existing_note_kind": note.get("kind") or "",
        "existing_note_id": int(note.get("id") or 0), "existing_note_text": display_note_text,
        "match_type": options[0].get("match_type") if options else "", "score": options[0].get("score") if options else 0,
        "fuzzy_errors": options[0].get("fuzzy_errors") if options else None,
        "issue_code": issue, "issue_label": ISSUE_LABELS[issue], "source_options": options,
        "selected_option": 0, "proposed_citation": proposed,
        "reference_disambiguated": bool(reference_disambiguated),
        "auto_selected": (
            issue == "verified"
            and _auto_select(
                str(options[0].get("match_type") or "") if options else "",
                float(options[0].get("score") or 0) if options else 0,
                options[0].get("fuzzy_errors") if options else None,
                options, "conservative",
            )
        ),
    }


def analyze_extraction(
    extracted: dict,
    corpus,
    *,
    selected_sections: list[str],
    scope_tokens: list[str],
    mode: str,
    threshold: str,
    citation_style: str,
    progress: Callable[[int, int], None] | None = None,
    personal_callback: Callable[[list[dict], list[str], str], list[dict]] | None = None,
    shadow_callback: Callable[[list[dict], list[dict], list[str]], None] | None = None,
    include_readonly_structures: bool = False,
) -> tuple[list[dict], str, float]:
    selected = {str(x) for x in selected_sections}
    paragraphs = list(extracted.get("paragraphs") or [])
    scope_spec, volumes = _selected_volumes(corpus, scope_tokens)
    note_texts = [
        str(ref.get("text") or "") for paragraph in paragraphs for ref in (paragraph.get("note_refs") or [])
        if str(ref.get("text") or "").strip()
    ]
    resolved_style, style_confidence = _detect_style(note_texts, citation_style)
    candidates: list[dict] = []
    records, paragraph_norms = _phrase_records(
        paragraphs, selected,
        include_readonly_structures=include_readonly_structures,
    )
    if mode in {"generate", "both"} and records and volumes:
        seeds = _seed_records(records)
        review_seeds = _review_seed_records(records)
        grouped: dict[tuple[int, int, int], dict] = {}
        seed_occurrences: dict[tuple[str, str], int] = defaultdict(int)
        review_occurrences: dict[tuple[str, str], int] = defaultdict(int)
        review_checked: set[tuple[int, str, int]] = set()
        matcher = _build_seed_matcher(seeds)
        review_matcher = _build_seed_matcher(review_seeds)
        total = max(1, len(volumes))
        for vindex, volume in enumerate(volumes, start=1):
            if progress:
                progress(vindex - 1, total)
            for source_seed_start, seed in _iter_seed_hits(volume.norm_full, seeds, matcher):
                freq_key = (str(volume.source_file), seed)
                seed_occurrences[freq_key] += 1
                if seed_occurrences[freq_key] > 40:
                    continue
                for record in seeds.get(seed, []):
                    pidx = int(record["paragraph_index"])
                    pnorm, pmap = paragraph_norms[pidx]
                    paper_seed_start = int(record["norm_start"]) + int(record["seed_local"])
                    pleft, pright, sleft, sright = _extend_exact(
                        pnorm, paper_seed_start, volume.norm_full, source_seed_start, len(seed)
                    )
                    if record.get("quoted"):
                        # A quotation is an indivisible audit unit: accept an exact
                        # seed only when extension covers the complete quoted text,
                        # then clamp both paper/source spans to that full quotation.
                        quote_left = int(record["norm_start"])
                        quote_right = int(record["norm_end"])
                        if pleft > quote_left or pright < quote_right:
                            continue
                        sleft += quote_left - pleft
                        sright -= pright - quote_right
                        pleft, pright = quote_left, quote_right
                    min_len = 12 if record.get("quoted") else 24
                    if pright - pleft < min_len:
                        continue
                    paragraph = paragraphs[pidx]
                    paragraph_text = str(paragraph.get("text") or "")
                    if record.get("quoted"):
                        raw_start = int(record.get("citation_start", record["raw_start"]))
                        raw_end = _anchor_end(
                            paragraph_text, int(record.get("citation_end", record["raw_end"]))
                        )
                        matched_paper_text = paragraph_text[
                            int(record["raw_start"]):int(record["raw_end"])
                        ]
                    else:
                        raw_start = pmap[pleft] if pleft < len(pmap) else int(record["raw_start"])
                        raw_end = (pmap[pright - 1] + 1) if pright and pright - 1 < len(pmap) else int(record["raw_end"])
                        raw_end = _anchor_end(paragraph_text, raw_end)
                        matched_paper_text = paragraph_text[raw_start:raw_end]
                    key = (pidx, raw_start, raw_end)
                    try:
                        hit = corpus._make_hit(
                            volume, sleft, sright, "exact", 100,
                            matched_paper_text,
                        )
                    except Exception:
                        continue
                    option = _option_from_hit(hit)
                    entry = grouped.setdefault(key, {
                        "record": record, "options": {}, "paper_text": paragraph_text[raw_start:raw_end],
                    })
                    entry["options"][_option_signature(option)] = option

            # A shorter exact anchor only shortlists a local source window.  The
            # full quotation/sentence must then pass >=92% edit-distance review.
            # Keep the paper span whole so notes are never inserted mid-quote.
            per_record_volume: dict[tuple, int] = defaultdict(int)
            for source_seed_start, seed in _iter_seed_hits(volume.norm_full, review_seeds, review_matcher):
                freq_key = (str(volume.source_file), seed)
                review_occurrences[freq_key] += 1
                if review_occurrences[freq_key] > 80:
                    continue
                for record in review_seeds.get(seed, []):
                    record_key = tuple(record.get("record_identity") or ())
                    if per_record_volume[record_key] >= REVIEW_ANCHORS_PER_RECORD_VOLUME:
                        continue
                    per_record_volume[record_key] += 1
                    estimated = int(source_seed_start) - int(record.get("seed_local") or 0)
                    check_key = (record_key, str(volume.source_file), estimated)
                    if check_key in review_checked:
                        continue
                    review_checked.add(check_key)
                    aligned = _align_near_seed(record, volume.norm_full, estimated)
                    if not aligned:
                        continue
                    source_left, source_right, score, errors = aligned
                    pidx = int(record["paragraph_index"])
                    paragraph = paragraphs[pidx]
                    paragraph_text = str(paragraph.get("text") or "")
                    raw_start = (
                        int(record.get("citation_start", record["raw_start"]))
                        if record.get("quoted") else int(record["raw_start"])
                    )
                    raw_end = _anchor_end(
                        paragraph_text,
                        int(record.get("citation_end", record["raw_end"]))
                        if record.get("quoted") else int(record["raw_end"]),
                    )
                    key = (pidx, raw_start, raw_end)
                    try:
                        hit = corpus._make_hit(
                            volume, source_left, source_right, "fuzzy", score,
                            paragraph_text[int(record["raw_start"]):int(record["raw_end"])],
                            fuzzy_errors=errors,
                        )
                    except Exception:
                        continue
                    option = _option_from_hit(hit)
                    signature = _option_signature(option)
                    entry = grouped.setdefault(key, {
                        "record": record,
                        "options": {},
                        "paper_text": paragraph_text[raw_start:raw_end],
                    })
                    existing = entry["options"].get(signature)
                    if not existing or existing.get("match_type") != "exact":
                        entry["options"][signature] = option

        # A second pass rescues only explicit quotations that the full-library
        # exact/normal-review pass missed.  This avoids charging every already
        # located quote (and every ordinary sentence) for short-anchor scans.
        matched_records = {_record_identity(value["record"]) for value in grouped.values()}
        rescue_records = [
            record for record in records
            if record.get("quoted") and _record_identity(record) not in matched_records
        ]
        rescue_seeds = _review_seed_records(
            rescue_records,
            quoted_len=REVIEW_RESCUE_SEED_QUOTED,
            max_seeds=REVIEW_RESCUE_MAX_SEEDS,
        )
        rescue_matcher = _build_seed_matcher(rescue_seeds)
        for volume in volumes:
            # Count before verification so a common short seed is discarded as
            # a whole instead of privileging its first N occurrences (the true
            # source may be later in the volume).
            rescue_positions: dict[str, list[int]] = defaultdict(list)
            too_common: set[str] = set()
            for source_seed_start, seed in _iter_seed_hits(
                volume.norm_full, rescue_seeds, rescue_matcher,
            ):
                if seed in too_common:
                    continue
                positions = rescue_positions[seed]
                positions.append(source_seed_start)
                if len(positions) > REVIEW_RESCUE_MAX_OCCURRENCES:
                    rescue_positions.pop(seed, None)
                    too_common.add(seed)
            rescue_checked: set[tuple[tuple[int, int, int, bool], int]] = set()
            rescue_per_record: dict[tuple[int, int, int, bool], int] = defaultdict(int)
            for seed, positions in rescue_positions.items():
                for source_seed_start in positions:
                    for record in rescue_seeds.get(seed, []):
                        record_key = _record_identity(record)
                        if rescue_per_record[record_key] >= REVIEW_RESCUE_ANCHORS_PER_RECORD_VOLUME:
                            continue
                        rescue_per_record[record_key] += 1
                        estimated = int(source_seed_start) - int(record.get("seed_local") or 0)
                        check_key = (record_key, estimated)
                        if check_key in rescue_checked:
                            continue
                        rescue_checked.add(check_key)
                        aligned = _align_near_seed(record, volume.norm_full, estimated)
                        if not aligned:
                            continue
                        source_left, source_right, score, errors = aligned
                        pidx = int(record["paragraph_index"])
                        paragraph = paragraphs[pidx]
                        paragraph_text = str(paragraph.get("text") or "")
                        raw_start = int(record.get("citation_start", record["raw_start"]))
                        raw_end = _anchor_end(
                            paragraph_text, int(record.get("citation_end", record["raw_end"])),
                        )
                        try:
                            hit = corpus._make_hit(
                                volume, source_left, source_right, "fuzzy", score,
                                paragraph_text[int(record["raw_start"]):int(record["raw_end"])],
                                fuzzy_errors=errors,
                            )
                        except Exception:
                            continue
                        option = _option_from_hit(hit)
                        entry = grouped.setdefault((pidx, raw_start, raw_end), {
                            "record": record,
                            "options": {},
                            "paper_text": paragraph_text[raw_start:raw_end],
                        })
                        entry["options"][_option_signature(option)] = option
        # 相同段落内包含关系只保留最长；相同论文区间聚合跨版本出处。
        by_para: dict[int, list[tuple[tuple[int, int, int], dict]]] = defaultdict(list)
        for key, value in grouped.items():
            by_para[key[0]].append((key, value))
        kept: list[tuple[tuple[int, int, int], dict]] = []
        for items in by_para.values():
            items.sort(key=lambda kv: (-(kv[0][2] - kv[0][1]), kv[0][1]))
            spans: list[tuple[int, int]] = []
            for key, value in items:
                if any(key[1] >= start and key[2] <= end for start, end in spans):
                    continue
                spans.append((key[1], key[2]))
                kept.append((key, value))
        for key, value in kept:
            paragraph = paragraphs[key[0]]
            # 已有注释锚点落在命中结尾附近时不再重复补注，由 audit 分支处理。
            if _candidate_has_existing_note(paragraph, key[2]):
                continue
            options = _prefer_exact_options(list(value["options"].values()))[:20]
            best_match_type = "exact" if any(o.get("match_type") == "exact" for o in options) else "fuzzy"
            best_score = 100 if best_match_type == "exact" else max(int(o.get("score") or 0) for o in options)
            best_errors = 0 if best_match_type == "exact" else min(
                int(o.get("fuzzy_errors") or 0) for o in options if o.get("fuzzy_errors") is not None
            )
            options.sort(key=lambda option: (
                0 if option.get("match_type") == "exact" else 1,
                -int(option.get("score") or 0),
                str(option.get("book") or ""),
            ))
            issue = "ambiguous" if len(options) > 1 else "suggest_add"
            proposed = _citation_for_style(options[0], resolved_style) if options else ""
            auto = _auto_select(best_match_type, best_score, best_errors, options, threshold)
            candidates.append({
                "kind": "generate", "section_id": paragraph.get("section_id") or "",
                "quoted": bool(value["record"].get("quoted")),
                "paragraph_index": key[0], "raw_start": key[1], "raw_end": key[2],
                "paper_text": value["paper_text"], "match_type": best_match_type, "score": best_score,
                "fuzzy_errors": best_errors, "issue_code": issue, "issue_label": ISSUE_LABELS[issue],
                "source_options": options, "selected_option": 0, "proposed_citation": proposed,
                "auto_selected": auto,
            })
        # 近似兜底聚焦显式引文，避免对整篇论文做逐句全库模糊扫描。
        covered = {(c["paragraph_index"], c["raw_start"], c["raw_end"]) for c in candidates}
        fuzzy_records = [r for r in records if r.get("quoted")][:200]
        for record in fuzzy_records:
            if any(p == int(record["paragraph_index"]) and s <= int(record["raw_start"]) and e >= int(record["raw_end"])
                   for p, s, e in covered):
                continue
            paragraph = paragraphs[int(record["paragraph_index"])]
            paragraph_text = str(paragraph.get("text") or "")
            candidate_raw_end = _anchor_end(
                paragraph_text, int(record.get("citation_end", record["raw_end"]))
            )
            if _candidate_has_existing_note(paragraph, candidate_raw_end):
                continue
            raw = paragraph_text[int(record["raw_start"]):int(record["raw_end"])]
            hits = _locate_ellipsis_quote(corpus, raw, scope_spec)
            if not hits:
                hits = corpus.locate_quote(raw, per_book_exact=8, allow_fuzzy=True, book_scope=scope_spec)
                hits = [h for h in hits if str(getattr(h, "match_type", "")) == "fuzzy"]
            if not hits:
                continue
            options = []
            seen = set()
            for hit in hits[:20]:
                option = _option_from_hit(hit)
                sig = _option_signature(option)
                if sig not in seen:
                    seen.add(sig)
                    options.append(option)
            best = options[0]
            issue = "ambiguous" if len(options) > 1 else "suggest_add"
            candidates.append({
                "kind": "generate", "section_id": paragraph.get("section_id") or "",
                "quoted": bool(record.get("quoted")),
                "paragraph_index": int(record["paragraph_index"]),
                "raw_start": int(record.get("citation_start", record["raw_start"])),
                "raw_end": candidate_raw_end,
                "paper_text": paragraph_text[
                    int(record.get("citation_start", record["raw_start"])):
                    candidate_raw_end
                ], "match_type": "fuzzy", "score": best.get("score") or 0,
                "fuzzy_errors": best.get("fuzzy_errors"), "issue_code": issue, "issue_label": ISSUE_LABELS[issue],
                "source_options": options, "selected_option": 0,
                "proposed_citation": _citation_for_style(best, resolved_style),
                "auto_selected": _auto_select("fuzzy", float(best.get("score") or 0), best.get("fuzzy_errors"), options, threshold),
            })
        if progress:
            progress(total, total)

    if personal_callback and mode in {"generate", "both"}:
        try:
            candidates.extend(personal_callback(records, scope_tokens, resolved_style) or [])
        except Exception:
            pass

    if shadow_callback and mode in {"generate", "both"}:
        try:
            shadow_callback(records, candidates, scope_tokens)
        except Exception:
            # The experimental shadow is observational and must never affect
            # the deterministic candidate set or the job's completion state.
            pass

    if mode in {"audit", "both"}:
        previous_options: dict[str, dict | None] = {}
        for paragraph in paragraphs:
            if paragraph.get("section_id") not in selected:
                continue
            for note in paragraph.get("note_refs") or []:
                readonly_structure = bool(paragraph.get("unsupported")) and bool(
                    paragraph.get("has_field") or paragraph.get("tracked")
                )
                if paragraph.get("unsupported") and not (
                    include_readonly_structures and readonly_structure
                ):
                    note_kind = str(note.get("kind") or "footnote")
                    candidates.append({
                        "kind": "audit", "section_id": paragraph.get("section_id") or "",
                        "paragraph_index": int(paragraph["index"]), "raw_start": int(note.get("offset") or 0),
                        "raw_end": int(note.get("offset") or 0), "paper_text": "",
                        "existing_note_kind": note.get("kind") or "", "existing_note_id": note.get("id"),
                        "existing_note_text": note.get("text") or "", "match_type": "", "score": 0,
                        "issue_code": "unsupported", "issue_label": ISSUE_LABELS["unsupported"],
                        "source_options": [], "selected_option": 0,
                        "proposed_citation": note.get("text") or "", "auto_selected": False,
                    })
                    previous_options[note_kind] = None
                else:
                    note_kind = str(note.get("kind") or "footnote")
                    candidate = _existing_note_candidate(
                        paragraph, note, corpus, scope_spec, resolved_style,
                        previous_option=previous_options.get(note_kind),
                    )
                    candidates.append(candidate)
                    if readonly_structure:
                        candidate["write_blocked"] = True
                    previous_options[note_kind] = (
                        dict(candidate["source_options"][0])
                        if candidate.get("source_options") else None
                    )

        # A bibliography field can point to the same quote already found by the
        # generate pass.  Prefer the audit record because it carries the user's
        # source metadata and can disambiguate reprints; never show two review
        # cards or write two annotations for the same wording.
        audit_texts: set[tuple[int, str]] = {
            (int(item.get("paragraph_index") or 0), normalize(str(item.get("paper_text") or "")))
            for item in candidates
            if item.get("kind") == "audit" and item.get("source_options")
            and len(normalize(str(item.get("paper_text") or ""))) >= 6
        }
        candidates = [
            item for item in candidates
            if not (
                item.get("kind") == "generate"
                and (
                    int(item.get("paragraph_index") or 0),
                    normalize(str(item.get("paper_text") or "")),
                ) in audit_texts
            )
        ]

    candidates.sort(key=lambda c: (int(c.get("paragraph_index", -1)), int(c.get("raw_start", 0)), c.get("kind") or ""))
    return candidates, resolved_style, style_confidence


def run_analysis(
    job_id: str,
    corpus,
    *,
    personal_callback: Callable[[list[dict], list[str], str], list[dict]] | None = None,
    shadow_callback: Callable[[list[dict], list[dict], list[str]], None] | None = None,
) -> None:
    row = get_job(job_id)
    if not row:
        return
    acquired = _MATCH_SEMAPHORE.acquire(timeout=1)
    if not acquired:
        update_job(job_id, status="queued")
        return
    try:
        update_job(job_id, status="matching", progress_done=0, progress_total=1, error="")
        delete_agent_shadow_run(job_id)
        extracted = json.loads(Path(row["extraction_path"]).read_text(encoding="utf-8"))

        def _progress(done: int, total: int) -> None:
            update_job(job_id, progress_done=int(done), progress_total=int(total))

        candidates, resolved, confidence = analyze_extraction(
            extracted,
            corpus,
            selected_sections=list(row.get("selected_sections") or []),
            scope_tokens=list(row.get("scope") or []),
            mode=str(row.get("mode") or "both"),
            threshold=str(row.get("threshold_mode") or "conservative"),
            citation_style=str(row.get("citation_style") or "auto"),
            progress=_progress,
            personal_callback=personal_callback,
            shadow_callback=shadow_callback,
        )
        replace_candidates(job_id, candidates)
        update_job(
            job_id, status="review_ready", resolved_style=resolved, style_confidence=float(confidence),
            progress_done=1, progress_total=1, error="",
        )
    except Exception as exc:
        update_job(job_id, status="failed", error=str(exc)[:500])
    finally:
        _MATCH_SEMAPHORE.release()


# ---------------------------------------------------------------------------
# DOCX / PDF export
# ---------------------------------------------------------------------------

def _xml_bytes(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _new_run_text(text: str, *, style: str = "", color: str = "") -> etree._Element:
    run = etree.Element(f"{{{W_NS}}}r")
    if style or color:
        props = etree.SubElement(run, f"{{{W_NS}}}rPr")
        if style:
            etree.SubElement(props, f"{{{W_NS}}}rStyle").set(f"{{{W_NS}}}val", style)
        if color:
            etree.SubElement(props, f"{{{W_NS}}}color").set(f"{{{W_NS}}}val", color)
    node = etree.SubElement(run, f"{{{W_NS}}}t")
    if text.startswith(" ") or text.endswith(" "):
        node.set(f"{{{XML_NS}}}space", "preserve")
    node.text = text
    return run


def _reference_run(kind: str, note_id: int, *, color: str = INSERTION_BLUE) -> etree._Element:
    run = etree.Element(f"{{{W_NS}}}r")
    props = etree.SubElement(run, f"{{{W_NS}}}rPr")
    etree.SubElement(props, f"{{{W_NS}}}rStyle").set(
        f"{{{W_NS}}}val", "FootnoteReference" if kind == "footnote" else "EndnoteReference"
    )
    if color:
        etree.SubElement(props, f"{{{W_NS}}}color").set(f"{{{W_NS}}}val", color)
    ref = etree.SubElement(run, f"{{{W_NS}}}{kind}Reference")
    ref.set(f"{{{W_NS}}}id", str(note_id))
    return run


def _insert_reference_at(paragraph: etree._Element, raw_offset: int, ref_run: etree._Element) -> None:
    nodes = _paragraph_text_nodes(paragraph)
    offset = max(0, int(raw_offset))
    cursor = 0
    if not nodes:
        paragraph.append(ref_run)
        return
    for node in nodes:
        value = str(node.text or "")
        end = cursor + len(value)
        if offset <= end:
            run = node
            while run is not None and run.tag != f"{{{W_NS}}}r":
                run = run.getparent()
            if run is None or run.getparent() is None:
                paragraph.append(ref_run)
                return
            parent = run.getparent()
            index = parent.index(run)
            local = max(0, min(offset - cursor, len(value)))
            if local == 0:
                parent.insert(index, ref_run)
            elif local == len(value):
                parent.insert(index + 1, ref_run)
            else:
                before, after = value[:local], value[local:]
                node.text = before
                trailing = copy.deepcopy(run)
                trailing_nodes = trailing.xpath(".//w:t", namespaces=NS)
                if trailing_nodes:
                    trailing_nodes[0].text = after
                    for extra in trailing_nodes[1:]:
                        extra.text = ""
                parent.insert(index + 1, ref_run)
                parent.insert(index + 2, trailing)
            return
        cursor = end
    last_run = nodes[-1]
    while last_run is not None and last_run.tag != f"{{{W_NS}}}r":
        last_run = last_run.getparent()
    if last_run is not None and last_run.getparent() is not None:
        parent = last_run.getparent()
        parent.insert(parent.index(last_run) + 1, ref_run)
    else:
        paragraph.append(ref_run)


def _empty_note_part(kind: str) -> etree._Element:
    root = etree.Element(f"{{{W_NS}}}{kind}s", nsmap={"w": W_NS})
    for note_id, note_type, marker in ((-1, "separator", "separator"), (0, "continuationSeparator", "continuationSeparator")):
        note = etree.SubElement(root, f"{{{W_NS}}}{kind}")
        note.set(f"{{{W_NS}}}id", str(note_id))
        note.set(f"{{{W_NS}}}type", note_type)
        para = etree.SubElement(note, f"{{{W_NS}}}p")
        run = etree.SubElement(para, f"{{{W_NS}}}r")
        etree.SubElement(run, f"{{{W_NS}}}{marker}")
    return root


def _note_element(
    kind: str, note_id: int, text: str, *, color: str = INSERTION_BLUE,
) -> etree._Element:
    note = etree.Element(f"{{{W_NS}}}{kind}")
    note.set(f"{{{W_NS}}}id", str(note_id))
    para = etree.SubElement(note, f"{{{W_NS}}}p")
    props = etree.SubElement(para, f"{{{W_NS}}}pPr")
    etree.SubElement(props, f"{{{W_NS}}}pStyle").set(
        f"{{{W_NS}}}val", "FootnoteText" if kind == "footnote" else "EndnoteText"
    )
    ref_run = etree.SubElement(para, f"{{{W_NS}}}r")
    ref_props = etree.SubElement(ref_run, f"{{{W_NS}}}rPr")
    etree.SubElement(ref_props, f"{{{W_NS}}}rStyle").set(
        f"{{{W_NS}}}val", "FootnoteReference" if kind == "footnote" else "EndnoteReference"
    )
    if color:
        etree.SubElement(ref_props, f"{{{W_NS}}}color").set(f"{{{W_NS}}}val", color)
    etree.SubElement(ref_run, f"{{{W_NS}}}{kind}Ref")
    para.append(_new_run_text(" " + str(text or ""), color=color))
    return note


def _ensure_note_relationship(rels: etree._Element, kind: str) -> None:
    rel_type = REL_TYPES[kind]
    if rels.xpath("./rel:Relationship[@Type=$kind]", namespaces=NS, kind=rel_type):
        return
    used = {str(node.get("Id") or "") for node in rels}
    index = 1
    while f"rId{index}" in used:
        index += 1
    rel = etree.SubElement(rels, f"{{{PKGREL_NS}}}Relationship")
    rel.set("Id", f"rId{index}")
    rel.set("Type", rel_type)
    rel.set("Target", f"{kind}s.xml")


def _ensure_note_content_type(content_types: etree._Element, kind: str) -> None:
    part = f"/word/{kind}s.xml"
    found = content_types.xpath("./ct:Override[@PartName=$part]", namespaces=NS, part=part)
    if found:
        return
    node = etree.SubElement(content_types, f"{{{CT_NS}}}Override")
    node.set("PartName", part)
    node.set("ContentType", CONTENT_TYPES[kind])


def _ensure_note_styles(styles: etree._Element, kind: str) -> None:
    text_style = "FootnoteText" if kind == "footnote" else "EndnoteText"
    ref_style = "FootnoteReference" if kind == "footnote" else "EndnoteReference"
    if not styles.xpath("./w:style[@w:styleId=$sid]", namespaces=NS, sid=text_style):
        style = etree.SubElement(styles, f"{{{W_NS}}}style")
        style.set(f"{{{W_NS}}}type", "paragraph")
        style.set(f"{{{W_NS}}}styleId", text_style)
        etree.SubElement(style, f"{{{W_NS}}}name").set(f"{{{W_NS}}}val", text_style)
    if not styles.xpath("./w:style[@w:styleId=$sid]", namespaces=NS, sid=ref_style):
        style = etree.SubElement(styles, f"{{{W_NS}}}style")
        style.set(f"{{{W_NS}}}type", "character")
        style.set(f"{{{W_NS}}}styleId", ref_style)
        etree.SubElement(style, f"{{{W_NS}}}name").set(f"{{{W_NS}}}val", ref_style)
        rpr = etree.SubElement(style, f"{{{W_NS}}}rPr")
        vert = etree.SubElement(rpr, f"{{{W_NS}}}vertAlign")
        vert.set(f"{{{W_NS}}}val", "superscript")


def _ensure_comments_relationship(rels: etree._Element) -> None:
    if rels.xpath("./rel:Relationship[@Type=$kind]", namespaces=NS, kind=COMMENTS_REL_TYPE):
        return
    used = {str(node.get("Id") or "") for node in rels}
    index = 1
    while f"rId{index}" in used:
        index += 1
    relation = etree.SubElement(rels, f"{{{PKGREL_NS}}}Relationship")
    relation.set("Id", f"rId{index}")
    relation.set("Type", COMMENTS_REL_TYPE)
    relation.set("Target", "comments.xml")


def _ensure_comments_content_type(content_types: etree._Element) -> None:
    part = "/word/comments.xml"
    if content_types.xpath("./ct:Override[@PartName=$part]", namespaces=NS, part=part):
        return
    node = etree.SubElement(content_types, f"{{{CT_NS}}}Override")
    node.set("PartName", part)
    node.set("ContentType", COMMENTS_CONTENT_TYPE)


def _ensure_comment_style(styles: etree._Element) -> None:
    if styles.xpath("./w:style[@w:styleId='CommentReference']", namespaces=NS):
        return
    style = etree.SubElement(styles, f"{{{W_NS}}}style")
    style.set(f"{{{W_NS}}}type", "character")
    style.set(f"{{{W_NS}}}styleId", "CommentReference")
    etree.SubElement(style, f"{{{W_NS}}}name").set(f"{{{W_NS}}}val", "Comment Reference")
    rpr = etree.SubElement(style, f"{{{W_NS}}}rPr")
    etree.SubElement(rpr, f"{{{W_NS}}}vanish")


def _empty_comments_part() -> etree._Element:
    return etree.Element(f"{{{W_NS}}}comments", nsmap={"w": W_NS})


def _comment_reference_run(comment_id: int) -> etree._Element:
    run = etree.Element(f"{{{W_NS}}}r")
    props = etree.SubElement(run, f"{{{W_NS}}}rPr")
    etree.SubElement(props, f"{{{W_NS}}}rStyle").set(f"{{{W_NS}}}val", "CommentReference")
    etree.SubElement(run, f"{{{W_NS}}}commentReference").set(f"{{{W_NS}}}id", str(comment_id))
    return run


def _comment_marker(name: str, comment_id: int) -> etree._Element:
    node = etree.Element(f"{{{W_NS}}}{name}")
    node.set(f"{{{W_NS}}}id", str(comment_id))
    return node


def _comment_element(comment_id: int, text: str) -> etree._Element:
    comment = etree.Element(f"{{{W_NS}}}comment")
    comment.set(f"{{{W_NS}}}id", str(comment_id))
    comment.set(f"{{{W_NS}}}author", AGENT_COMMENT_AUTHOR)
    comment.set(f"{{{W_NS}}}initials", AGENT_COMMENT_INITIALS)
    comment.set(f"{{{W_NS}}}date", _iso())
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()] or ["校注建议"]
    for line in lines:
        paragraph = etree.SubElement(comment, f"{{{W_NS}}}p")
        run = etree.SubElement(paragraph, f"{{{W_NS}}}r")
        node = etree.SubElement(run, f"{{{W_NS}}}t")
        node.text = line[:1200]
    return comment


def _candidate_comment_text(candidate: dict) -> str:
    option = _selected_option(candidate)
    decision = {"accepted": "已采用", "pending": "待确认", "rejected": "已排除"}.get(
        str(candidate.get("decision") or "pending"), "待确认",
    )
    lines = [f"{readable_issue_label(candidate)}｜{decision}"]
    match_labels = {"exact": "逐字一致", "near": "近似文字", "paraphrase": "观点依据", "none": "文字未核验"}
    resolution_labels = {
        "unique": "唯一出处", "reference_disambiguated": "参考文献消歧",
        "multiple": "多个转载版本", "locator_only": "仅页码定位", "none": "无站内出处",
    }
    if candidate.get("text_match_level"):
        lines.append(f"文字核验：{match_labels.get(str(candidate.get('text_match_level')), candidate.get('text_match_level'))}")
    if candidate.get("source_resolution"):
        lines.append(f"来源判定：{resolution_labels.get(str(candidate.get('source_resolution')), candidate.get('source_resolution'))}")
    paper_text = str(candidate.get("paper_text") or "").strip()
    if paper_text:
        lines.append(f"实际核对文字：{paper_text}")
    note_kind_value = str(candidate.get("existing_note_kind") or "")
    note_kind = {
        "footnote": "脚注", "endnote": "尾注", "manual_endnote": "手工尾注",
        "reference_field": "交叉引用",
    }.get(note_kind_value, "原注")
    explicit_marker = re.sub(
        r"\s+", " ", str(candidate.get("existing_note_marker") or ""),
    ).strip()[:40]
    if explicit_marker:
        lines.append(f"对应{note_kind}标记：{explicit_marker}")
    elif candidate.get("existing_note_id") is not None or candidate.get("existing_note_text"):
        note_guidance = {
            "footnote": "见正文引文处原注标记（编号可能按页或分节重排）",
            "endnote": "见正文引文处原注标记（交叉引用可能共享同一尾注）",
            "manual_endnote": "见正文显式标记",
            "reference_field": "见正文域标记（更新域后可能变化，仅供人工复核）",
        }.get(note_kind_value, "见正文引文处原注标记")
        lines.append(f"对应{note_kind}：{note_guidance}")
    if candidate.get("existing_note_text"):
        lines.append(f"原注：{candidate.get('existing_note_text')}")
    if option:
        title = option.get("display_title") or option.get("citation_title") or option.get("source_file") or ""
        printed = "、".join(map(str, option.get("printed_pages") or []))
        pdf_pages = "、".join(map(str, option.get("pdf_pages") or []))
        source_line = f"出处：{title}"
        if printed:
            source_line += f"，第{printed}页"
        elif pdf_pages:
            source_line += f"，PDF第{pdf_pages}页"
        lines.append(source_line)
    proposed = str(candidate.get("proposed_citation") or "").strip()
    if proposed:
        lines.append(f"建议：{proposed}")
    score = int(round(float(candidate.get("score") or 0)))
    if score:
        lines.append(f"匹配置信度：{score}%")
    reasons = [reason_label(value) for value in candidate.get("reason_codes") or [] if str(value)]
    if reasons:
        lines.append("审核原因：" + "、".join(reasons))
    reader_url = str(option.get("viewer_url") or "").strip() if option else ""
    if reader_url:
        public_base = str(os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
        if public_base and reader_url.startswith("/"):
            reader_url = urljoin(f"{public_base}/", reader_url.lstrip("/"))
        lines.append(f"原文链接：{reader_url}")
    return "\n".join(lines)[:4000]


def _ensure_footnote_page_numbering(document: etree._Element) -> None:
    """Use circled automatic footnote marks and restart them on every page.

    The internal footnote IDs remain package-wide unique; Word/WPS derive the
    visible per-page numbers from these section properties.
    """
    section_props = list(document.xpath("./w:body//w:sectPr", namespaces=NS))
    if not section_props:
        bodies = document.xpath("./w:body", namespaces=NS)
        if not bodies:
            return
        section_props = [etree.SubElement(bodies[0], f"{{{W_NS}}}sectPr")]

    property_order = ["pos", "numFmt", "numStart", "numRestart"]
    section_order = ["headerReference", "footerReference", "footnotePr", "endnotePr"]

    def upsert_ordered(parent: etree._Element, local_name: str, value: str, order: list[str]) -> None:
        nodes = parent.xpath(f"./w:{local_name}", namespaces=NS)
        node = nodes[0] if nodes else etree.Element(f"{{{W_NS}}}{local_name}")
        node.set(f"{{{W_NS}}}val", value)
        if nodes:
            return
        wanted = order.index(local_name)
        insert_at = len(parent)
        for index, child in enumerate(parent):
            child_name = etree.QName(child).localname
            if child_name in order and order.index(child_name) > wanted:
                insert_at = index
                break
        parent.insert(insert_at, node)

    for section in section_props:
        existing = section.xpath("./w:footnotePr", namespaces=NS)
        if existing:
            properties = existing[0]
        else:
            properties = etree.Element(f"{{{W_NS}}}footnotePr")
            wanted = section_order.index("footnotePr")
            insert_at = len(section)
            for index, child in enumerate(section):
                child_name = etree.QName(child).localname
                if child_name not in section_order or section_order.index(child_name) > wanted:
                    insert_at = index
                    break
            section.insert(insert_at, properties)
        upsert_ordered(properties, "numFmt", "decimalEnclosedCircle", property_order)
        upsert_ordered(properties, "numRestart", "eachPage", property_order)


def _selected_option(candidate: dict) -> dict:
    options = list(candidate.get("source_options") or [])
    if not options:
        return {}
    index = max(0, min(int(candidate.get("selected_option") or 0), len(options) - 1))
    return dict(options[index])


def _candidate_with_verified_note_marker(candidate: dict, paragraph: dict | None) -> dict:
    """Attach only a literal marker that is present in the source document.

    Automatic OOXML note IDs are package relationships, not visible numbers.
    In particular they cannot represent per-page/per-section numbering.  Manual
    markers are stored as literal source text and may therefore be displayed.
    """
    result = dict(candidate)
    if not paragraph:
        return result
    kind = str(result.get("existing_note_kind") or "")
    note_id = result.get("existing_note_id")
    matches = [
        note for note in (paragraph.get("note_refs") or [])
        if str(note.get("kind") or "") == kind and note.get("id") == note_id
    ]
    if not matches:
        return result
    anchor = int(result.get("raw_end") or 0)
    selected = min(matches, key=lambda note: abs(int(note.get("offset") or 0) - anchor))
    marker = re.sub(r"\s+", " ", str(selected.get("marker_text") or "")).strip()
    if marker:
        result["existing_note_marker"] = marker[:40]
    return result


def validate_exported_docx(path: str | Path, *, filename: str = "output.docx") -> None:
    payload = Path(path).read_bytes()
    validate_docx_bytes(payload, filename=filename)
    with zipfile.ZipFile(BytesIO(payload)) as package:
        names = set(package.namelist())
        body = _parse_xml(package.read("word/document.xml"))
        rels = _parse_xml(package.read("word/_rels/document.xml.rels"))
        content_types = _parse_xml(package.read("[Content_Types].xml"))
        for kind in VALID_NOTE_KINDS:
            ref_ids = [
                str(node.get(f"{{{W_NS}}}id") or "")
                for node in body.xpath(f".//w:{kind}Reference", namespaces=NS)
            ]
            if not ref_ids:
                continue
            part_name = f"word/{kind}s.xml"
            if part_name not in names:
                raise CitationAssistantError(f"DOCX {kind} 引用缺少定义部件。")
            if not rels.xpath("./rel:Relationship[@Type=$kind]", namespaces=NS, kind=REL_TYPES[kind]):
                raise CitationAssistantError(f"DOCX {kind} 关系缺失。")
            part_path = f"/word/{kind}s.xml"
            if not content_types.xpath("./ct:Override[@PartName=$part]", namespaces=NS, part=part_path):
                raise CitationAssistantError(f"DOCX {kind} 内容类型缺失。")
            notes = _parse_xml(package.read(part_name))
            defined = [
                str(node.get(f"{{{W_NS}}}id") or "")
                for node in notes.xpath(f"./w:{kind}", namespaces=NS)
                if _is_real_note_element(node)
            ]
            if len(defined) != len(set(defined)):
                raise CitationAssistantError(f"DOCX {kind} 编号重复。")
            if not set(ref_ids).issubset(set(defined)):
                raise CitationAssistantError(f"DOCX {kind} 引用与定义编号不一致。")
        comment_refs = {
            str(node.get(f"{{{W_NS}}}id") or "")
            for node in body.xpath(".//w:commentReference", namespaces=NS)
        }
        if comment_refs:
            if "word/comments.xml" not in names:
                raise CitationAssistantError("DOCX 批注引用缺少 comments.xml。")
            if not rels.xpath("./rel:Relationship[@Type=$kind]", namespaces=NS, kind=COMMENTS_REL_TYPE):
                raise CitationAssistantError("DOCX 批注关系缺失。")
            comment_part = "/word/comments.xml"
            if not content_types.xpath("./ct:Override[@PartName=$part]", namespaces=NS, part=comment_part):
                raise CitationAssistantError("DOCX 批注内容类型缺失。")
            comments = _parse_xml(package.read("word/comments.xml"))
            defined_comments = {
                str(node.get(f"{{{W_NS}}}id") or "")
                for node in comments.xpath("./w:comment", namespaces=NS)
            }
            starts = {
                str(node.get(f"{{{W_NS}}}id") or "")
                for node in body.xpath(".//w:commentRangeStart", namespaces=NS)
            }
            ends = {
                str(node.get(f"{{{W_NS}}}id") or "")
                for node in body.xpath(".//w:commentRangeEnd", namespaces=NS)
            }
            if not comment_refs.issubset(defined_comments & starts & ends):
                raise CitationAssistantError("DOCX 批注定义、锚点或引用不完整。")


def _make_output_copy_writable(parts: dict[str, bytes]) -> None:
    """Remove editing restrictions and invalidated signatures from the new copy only."""
    settings_name = next((name for name in parts if name.lower() == "word/settings.xml"), "")
    if settings_name:
        settings = _parse_xml(parts[settings_name])
        changed = False
        enforcement = f"{{{W_NS}}}enforcement"
        for node in settings.xpath(".//w:documentProtection", namespaces=NS):
            if _ooxml_on_off(node.get(enforcement)):
                parent = node.getparent()
                if parent is not None:
                    parent.remove(node)
                    changed = True
        if changed:
            parts[settings_name] = _xml_bytes(settings)

    signature_names = [name for name in parts if name.lower().startswith("_xmlsignatures/")]
    if not signature_names:
        return
    for name in signature_names:
        parts.pop(name, None)

    package_rels_name = next((name for name in parts if name.lower() == "_rels/.rels"), "")
    if package_rels_name:
        package_rels = _parse_xml(parts[package_rels_name])
        for relationship in list(package_rels.xpath("./rel:Relationship", namespaces=NS)):
            rel_type = str(relationship.get("Type") or "").lower()
            target = str(relationship.get("Target") or "").replace("\\", "/").lower()
            if "/digital-signature/" in rel_type or target.lstrip("/").startswith("_xmlsignatures/"):
                package_rels.remove(relationship)
        parts[package_rels_name] = _xml_bytes(package_rels)

    content_types_name = next((name for name in parts if name.lower() == "[content_types].xml"), "")
    if content_types_name:
        content_types = _parse_xml(parts[content_types_name])
        for override in list(content_types.xpath("./ct:Override", namespaces=NS)):
            if str(override.get("PartName") or "").lower().lstrip("/").startswith("_xmlsignatures/"):
                content_types.remove(override)
        for default in list(content_types.xpath("./ct:Default", namespaces=NS)):
            if str(default.get("Extension") or "").lower() == "sigs":
                content_types.remove(default)
        parts[content_types_name] = _xml_bytes(content_types)


def _mode_allows(job: dict, kind: str) -> bool:
    mode = str(job.get("mode") or "both")
    return mode == "both" or (mode == "generate" and kind == "generate") or (mode == "audit" and kind == "audit")


def _paragraph_has_unsafe_note_anchor(paragraph: etree._Element) -> bool:
    return bool(paragraph.xpath(
        ".//w:fldChar | .//w:instrText | .//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo",
        namespaces=NS,
    ))


def _candidate_is_insertion_eligible(
    job: dict, candidate: dict, paragraphs: list[etree._Element],
) -> bool:
    if candidate.get("decision") != "accepted" or str(candidate.get("kind") or "") != "generate":
        return False
    if not _mode_allows(job, "generate"):
        return False
    writeback = str(candidate.get("writeback_mode") or "")
    if writeback in {"none", "readonly", "comment"}:
        return False
    note_kind = str(job.get("note_kind") or "footnote")
    if writeback in VALID_NOTE_KINDS and writeback != note_kind:
        return False
    exact = str(candidate.get("text_match_level") or "") == "exact"
    if not exact:
        exact = (
            str(candidate.get("match_type") or "") == "exact"
            and float(candidate.get("score") or 0) >= 100
            and int(candidate.get("fuzzy_errors") or 0) == 0
        )
    options = list(candidate.get("source_options") or [])
    resolution = str(candidate.get("source_resolution") or "")
    uniquely_resolved = resolution in {"unique", "reference_disambiguated"}
    if not resolution:
        uniquely_resolved = len(options) == 1 or bool(candidate.get("reference_disambiguated"))
    if not exact or not uniquely_resolved or not options or not _is_reliable_page(options[0]):
        return False
    pindex = int(candidate.get("paragraph_index", -1))
    if not 0 <= pindex < len(paragraphs):
        return False
    paragraph = paragraphs[pindex]
    if _paragraph_has_unsafe_note_anchor(paragraph):
        return False
    offset = int(candidate.get("raw_end") or 0)
    return any(
        int(left) <= offset <= int(right)
        for left, right in _safe_comment_spans(paragraph)
    )


def _candidate_is_comment_eligible(job: dict, candidate: dict) -> bool:
    if candidate.get("decision") != "accepted" or str(candidate.get("kind") or "") != "audit":
        return False
    if str(candidate.get("existing_note_kind") or "") == "reference_field":
        return False
    if not _mode_allows(job, "audit"):
        return False
    if str(candidate.get("writeback_mode") or "") in {"none", "readonly", "footnote", "endnote"}:
        return False
    if str(candidate.get("text_match_level") or "") == "paraphrase":
        return False
    if str(candidate.get("source_resolution") or "") in {"none", "locator_only"}:
        return False
    if str(candidate.get("match_type") or "") == "locator":
        return False
    return True


def _output_docx_name(job: dict) -> str:
    stem = Path(str(job.get("original_filename") or "论文.docx")).stem
    suffix = {
        "generate": "_插注版.docx",
        "audit": "_校注批注版.docx",
        "both": "_插注校注版.docx",
    }.get(str(job.get("mode") or "both"), "_插注校注版.docx")
    return f"{stem}{suffix}"


def _note_nodes(path: str | Path, kind: str) -> dict[str, bytes]:
    part_name = f"word/{kind}s.xml"
    with zipfile.ZipFile(path) as package:
        if part_name not in package.namelist():
            return {}
        root = _parse_xml(package.read(part_name))
    return {
        str(node.get(f"{{{W_NS}}}id") or ""): etree.tostring(node, method="c14n")
        for node in root.xpath(f"./w:{kind}", namespaces=NS)
    }


def _validate_existing_notes_unchanged(
    source: str | Path, output: str | Path, *, allow_new: bool,
) -> None:
    for kind in VALID_NOTE_KINDS:
        original = _note_nodes(source, kind)
        exported = _note_nodes(output, kind)
        if any(exported.get(note_id) != payload for note_id, payload in original.items()):
            raise CitationAssistantError("校注导出改变了原有脚注或尾注，已拒绝发布。")
        if not allow_new and set(exported) != set(original):
            raise CitationAssistantError("纯校注导出不得新增脚注或尾注。")


def export_docx(job: dict, candidates: list[dict]) -> Path:
    input_path = Path(str(job["input_path"]))
    with zipfile.ZipFile(input_path) as source_zip:
        parts = {name: source_zip.read(name) for name in source_zip.namelist()}
    _make_output_copy_writable(parts)
    document = _parse_xml(parts["word/document.xml"])
    paragraphs = document.xpath("./w:body//w:p", namespaces=NS)
    rels = _parse_xml(parts["word/_rels/document.xml.rels"])
    content_types = _parse_xml(parts["[Content_Types].xml"])
    styles = _parse_xml(parts["word/styles.xml"]) if "word/styles.xml" in parts else None

    note_roots: dict[str, etree._Element] = {}
    for kind in VALID_NOTE_KINDS:
        part_name = f"word/{kind}s.xml"
        note_roots[kind] = _parse_xml(parts[part_name]) if part_name in parts else _empty_note_part(kind)

    accepted = [c for c in candidates if c.get("decision") == "accepted"]
    comment_candidates = [c for c in accepted if _candidate_is_comment_eligible(job, c)]
    if comment_candidates:
        try:
            extraction_path = Path(str(job.get("extraction_path") or ""))
            if extraction_path.is_file():
                extracted = json.loads(extraction_path.read_text(encoding="utf-8"))
            else:
                extracted = extract_docx(input_path)
            source_paragraphs = {
                int(item.get("index") or 0): item
                for item in (extracted.get("paragraphs") or [])
            }
            comment_candidates = [
                _candidate_with_verified_note_marker(
                    candidate,
                    source_paragraphs.get(int(candidate.get("paragraph_index") or 0)),
                )
                for candidate in comment_candidates
            ]
        except (OSError, TypeError, ValueError, json.JSONDecodeError, CitationAssistantError):
            # A missing optional marker must never block an otherwise safe comment.
            comment_candidates = [dict(candidate) for candidate in comment_candidates]

    generate = [
        c for c in accepted if _candidate_is_insertion_eligible(job, c, paragraphs)
    ]
    # Descending offsets keep the stored visible-text offsets stable while runs are split.
    generate.sort(key=lambda c: (int(c.get("paragraph_index", -1)), int(c.get("raw_end") or 0)), reverse=True)
    kind = str(job.get("note_kind") or "footnote")
    if kind not in VALID_NOTE_KINDS:
        kind = "footnote"
    existing_ids = []
    for node in note_roots[kind].xpath(f"./w:{kind}", namespaces=NS):
        try:
            existing_ids.append(int(node.get(f"{{{W_NS}}}id") or 0))
        except ValueError:
            pass
    next_id = max([x for x in existing_ids if x > 0] or [0]) + 1
    for candidate in generate:
        pindex = int(candidate["paragraph_index"])
        proposed = str(candidate.get("proposed_citation") or "").strip()
        if not proposed:
            proposed = _citation_for_style(_selected_option(candidate), str(job.get("resolved_style") or "gb2025"))
        if not proposed:
            continue
        _insert_reference_at(paragraphs[pindex], int(candidate.get("raw_end") or 0), _reference_run(kind, next_id))
        note_roots[kind].append(_note_element(kind, next_id, proposed))
        next_id += 1

    if generate and kind == "footnote":
        _ensure_footnote_page_numbering(document)

    used_kinds = {kind} if generate else set()
    for used_kind in used_kinds & VALID_NOTE_KINDS:
        _ensure_note_relationship(rels, used_kind)
        _ensure_note_content_type(content_types, used_kind)
        if styles is not None:
            _ensure_note_styles(styles, used_kind)
        parts[f"word/{used_kind}s.xml"] = _xml_bytes(note_roots[used_kind])
    if comment_candidates:
        comments = (
            _parse_xml(parts["word/comments.xml"])
            if "word/comments.xml" in parts else _empty_comments_part()
        )
        existing_ids: list[int] = []
        for node in comments.xpath("./w:comment", namespaces=NS):
            try:
                existing_ids.append(int(node.get(f"{{{W_NS}}}id") or 0))
            except ValueError:
                pass
        next_comment_id = max(existing_ids or [-1]) + 1
        ordered_comments = sorted(
            comment_candidates,
            key=lambda item: (int(item.get("paragraph_index", -1)), int(item.get("raw_start") or 0)),
            reverse=True,
        )
        for candidate in ordered_comments:
            pindex = int(candidate.get("paragraph_index", -1))
            if not 0 <= pindex < len(paragraphs):
                raise CitationAssistantError("批注锚点段落已失效。")
            paragraph = paragraphs[pindex]
            start = int(candidate.get("raw_start") or 0)
            end = int(candidate.get("raw_end") or 0)
            if not _comment_span_is_safe(paragraph, start, end):
                raise CitationAssistantError("批注锚点跨越 Word 域、修订或现有批注，已停止写入。")
            comment_id = next_comment_id
            next_comment_id += 1
            _insert_reference_at(paragraph, end, _comment_reference_run(comment_id))
            _insert_reference_at(paragraph, end, _comment_marker("commentRangeEnd", comment_id))
            _insert_reference_at(paragraph, start, _comment_marker("commentRangeStart", comment_id))
            comments.append(_comment_element(comment_id, _candidate_comment_text(candidate)))
        _ensure_comments_relationship(rels)
        _ensure_comments_content_type(content_types)
        if styles is not None:
            _ensure_comment_style(styles)
        parts["word/comments.xml"] = _xml_bytes(comments)
    parts["word/document.xml"] = _xml_bytes(document)
    parts["word/_rels/document.xml.rels"] = _xml_bytes(rels)
    parts["[Content_Types].xml"] = _xml_bytes(content_types)
    if styles is not None:
        parts["word/styles.xml"] = _xml_bytes(styles)

    output = _job_dir(int(job["user_id"]), str(job["id"]), create=True) / _output_docx_name(job)
    partial = output.with_suffix(".docx.part")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target_zip:
        for name, payload in parts.items():
            target_zip.writestr(name, payload)
    validate_exported_docx(partial, filename=output.name)
    partial.replace(output)
    _validate_existing_notes_unchanged(
        input_path, output, allow_new=str(job.get("mode") or "both") in {"generate", "both"},
    )
    return output


def build_annotated_review_docx(
    job: dict, candidates: list[dict], *, source_path: str | Path | None = None,
) -> Path:
    """Create a layout-preserving copy with Word comments anchored to paper text."""
    input_path = Path(source_path) if source_path is not None else Path(str(job["input_path"]))
    with zipfile.ZipFile(input_path) as source_zip:
        parts = {name: source_zip.read(name) for name in source_zip.namelist()}
    _make_output_copy_writable(parts)
    document = _parse_xml(parts["word/document.xml"])
    paragraphs = document.xpath("./w:body//w:p", namespaces=NS)
    rels = _parse_xml(parts["word/_rels/document.xml.rels"])
    content_types = _parse_xml(parts["[Content_Types].xml"])
    styles = _parse_xml(parts["word/styles.xml"]) if "word/styles.xml" in parts else None
    comments = (
        _parse_xml(parts["word/comments.xml"])
        if "word/comments.xml" in parts else _empty_comments_part()
    )
    existing_ids: list[int] = []
    for node in comments.xpath("./w:comment", namespaces=NS):
        try:
            existing_ids.append(int(node.get(f"{{{W_NS}}}id") or 0))
        except ValueError:
            pass
    next_id = max(existing_ids or [-1]) + 1

    review_items = [
        candidate for candidate in candidates
        if _candidate_is_comment_eligible(job, candidate)
        and 0 <= int(candidate.get("paragraph_index", -1)) < len(paragraphs)
    ][:2000]

    # Work backwards so visible-text offsets remain stable as range markers are inserted.
    review_items.sort(
        key=lambda item: (int(item.get("paragraph_index", -1)), int(item.get("raw_start") or 0)),
        reverse=True,
    )
    for candidate in review_items:
        pindex = int(candidate["paragraph_index"])
        paragraph = paragraphs[pindex]
        text_length = len(_paragraph_text(paragraph))
        start = max(0, min(int(candidate.get("raw_start") or 0), text_length))
        end = max(start, min(int(candidate.get("raw_end") or start), text_length))
        if not _comment_span_is_safe(paragraph, start, end):
            raise CitationAssistantError("批注锚点跨越 Word 域、修订或现有批注，已停止写入。")
        comment_id = next_id
        next_id += 1
        # Inserting the reference first and range-end second at the same offset
        # yields the required text → rangeEnd → commentReference order.
        _insert_reference_at(paragraph, end, _comment_reference_run(comment_id))
        _insert_reference_at(paragraph, end, _comment_marker("commentRangeEnd", comment_id))
        _insert_reference_at(paragraph, start, _comment_marker("commentRangeStart", comment_id))
        comments.append(_comment_element(comment_id, _candidate_comment_text(candidate)))

    _ensure_comments_relationship(rels)
    _ensure_comments_content_type(content_types)
    if styles is not None:
        _ensure_comment_style(styles)
    parts["word/document.xml"] = _xml_bytes(document)
    parts["word/_rels/document.xml.rels"] = _xml_bytes(rels)
    parts["[Content_Types].xml"] = _xml_bytes(content_types)
    parts["word/comments.xml"] = _xml_bytes(comments)
    if styles is not None:
        parts["word/styles.xml"] = _xml_bytes(styles)

    comment_ids = {
        str(node.get(f"{{{W_NS}}}id") or "")
        for node in comments.xpath("./w:comment", namespaces=NS)
    }
    reference_ids = {
        str(node.get(f"{{{W_NS}}}id") or "")
        for node in document.xpath(".//w:commentReference", namespaces=NS)
    }
    if not reference_ids.issubset(comment_ids):
        raise CitationAssistantError("DOCX 批注引用与批注定义不一致。")

    output = _job_dir(int(job["user_id"]), str(job["id"]), create=True) / "._校注批注转换源.docx"
    partial = output.with_suffix(".docx.part")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target_zip:
        for name, payload in parts.items():
            target_zip.writestr(name, payload)
    validate_exported_docx(partial, filename="annotated-review.docx")
    partial.replace(output)
    return output


def _soffice_binary() -> str:
    configured = str(os.environ.get("CITATION_ASSISTANT_SOFFICE") or "").strip()
    if configured and Path(configured).is_file():
        return configured
    for command in ("soffice", "libreoffice", "lowriter"):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return ""


def _pdf_timeout_seconds() -> int:
    try:
        configured = int(str(os.environ.get("CITATION_ASSISTANT_PDF_TIMEOUT") or "180"))
    except (TypeError, ValueError):
        configured = 180
    return max(30, min(configured, 600))


def _build_pdf_conversion_copy_without_agent_comments(source: str | Path) -> Path:
    """Create a transient DOCX whose PDF rendering cannot echo Agent comments.

    Only comments carrying both of the exact Agent identity fields are removed.
    Existing author comments, body content, notes and formatting stay byte-for-byte
    identical at the package-part level unless their XML contains one of the
    removed Agent comment markers.
    """
    source_path = Path(source)
    temporary = source_path.with_name(
        f".{source_path.stem}-{uuid.uuid4().hex}.pdf-source.docx"
    )
    partial = temporary.with_suffix(".docx.part")
    try:
        with zipfile.ZipFile(source_path) as source_zip:
            parts = {name: source_zip.read(name) for name in source_zip.namelist()}

        comments_name = next(
            (name for name in parts if name.lower() == "word/comments.xml"), "",
        )
        agent_ids: set[str] = set()
        if comments_name:
            comments = _parse_xml(parts[comments_name])
            for comment in list(comments.xpath("./w:comment", namespaces=NS)):
                author = str(comment.get(f"{{{W_NS}}}author") or "")
                initials = str(comment.get(f"{{{W_NS}}}initials") or "")
                if author != AGENT_COMMENT_AUTHOR or initials != AGENT_COMMENT_INITIALS:
                    continue
                comment_id = str(comment.get(f"{{{W_NS}}}id") or "")
                if not comment_id:
                    continue
                agent_ids.add(comment_id)
                comments.remove(comment)
            parts[comments_name] = _xml_bytes(comments)

        if agent_ids:
            marker_names = {"commentRangeStart", "commentRangeEnd", "commentReference"}
            for name, payload in list(parts.items()):
                normalized_name = name.replace("\\", "/").lower()
                if (
                    not normalized_name.startswith("word/")
                    or not normalized_name.endswith(".xml")
                    or normalized_name == "word/comments.xml"
                    or not any(marker.encode("ascii") in payload for marker in marker_names)
                ):
                    continue
                root = _parse_xml(payload)
                changed = False
                for marker_name in marker_names:
                    for marker in list(root.xpath(f".//w:{marker_name}", namespaces=NS)):
                        if str(marker.get(f"{{{W_NS}}}id") or "") not in agent_ids:
                            continue
                        parent = marker.getparent()
                        if parent is not None:
                            parent.remove(marker)
                            changed = True
                if changed:
                    parts[name] = _xml_bytes(root)

        with zipfile.ZipFile(
            partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
        ) as target_zip:
            for name, payload in parts.items():
                target_zip.writestr(name, payload)
        validate_exported_docx(partial, filename="pdf-conversion-source.docx")
        partial.replace(temporary)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        partial.unlink(missing_ok=True)


def _convert_annotated_docx_to_pdf(annotated_docx: Path, output: Path) -> None:
    office = _soffice_binary()
    if not office:
        raise CitationAssistantError("批注式 PDF 转换组件尚未就绪，请联系管理员安装独立文档转换组件。")
    token = uuid.uuid4().hex
    profile = annotated_docx.parent / f".lo-profile-{token}"
    converted_dir = annotated_docx.parent / f".lo-output-{token}"
    profile.mkdir(parents=True, exist_ok=False)
    for private_dir in (profile / ".config", profile / ".cache", profile / ".tmp"):
        private_dir.mkdir(parents=True, exist_ok=False)
    converted_dir.mkdir(parents=True, exist_ok=False)
    try:
        # LibreOffice's headless Writer rejects ExportNotesInMargin for some DOCX
        # documents even though the same document renders normally.  Keep Writer
        # responsible for the faithful page layout and add the visible review
        # margin in a deterministic PDF post-processing step below.
        filter_options = "pdf:writer_pdf_Export"
        timeout = _pdf_timeout_seconds()
        office_env = os.environ.copy()
        office_env.update({
            "HOME": str(profile),
            "XDG_CONFIG_HOME": str(profile / ".config"),
            "XDG_CACHE_HOME": str(profile / ".cache"),
            "TMPDIR": str(profile / ".tmp"),
        })
        completed = subprocess.run(
            [
                office, "--headless", "--nologo", "--nodefault", "--nolockcheck",
                "--norestore", "--nofirststartwizard", f"-env:UserInstallation={profile.resolve().as_uri()}",
                "--convert-to", filter_options, "--outdir", str(converted_dir), str(annotated_docx),
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
            env=office_env,
        )
        converted = converted_dir / f"{annotated_docx.stem}.pdf"
        if completed.returncode != 0 or not converted.is_file() or converted.stat().st_size < 1000:
            detail = re.sub(r"\s+", " ", str(completed.stdout or "")).strip()[-300:]
            raise CitationAssistantError(f"批注式 PDF 转换失败。{detail}".strip())
        converted.replace(output)
    except subprocess.TimeoutExpired as exc:
        raise CitationAssistantError("批注式 PDF 转换超时，请稍后重试或缩小文档。") from exc
    finally:
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        if converted_dir.exists():
            shutil.rmtree(converted_dir, ignore_errors=True)


def _candidate_pdf_search_terms(candidate: dict) -> list[str]:
    value = re.sub(r"\s+", " ", str(candidate.get("paper_text") or "")).strip()
    if not value:
        return []
    terms: list[str] = []
    ellipsis_parts = [
        part.strip() for part in re.split(r"(?:\.{3,}|…{1,}|⋯{1,})", value)
        if len(normalize(part)) >= 6
    ]
    for candidate_term in (
        value,
        *ellipsis_parts,
        value[:120],
        value[:48],
        value[-48:],
        value[:24],
        value[-24:],
        value[:12],
    ):
        candidate_term = candidate_term.strip()
        if len(candidate_term) >= 6 and candidate_term not in terms:
            terms.append(candidate_term)
    return terms


def _pdf_review_manifest(job: dict, candidates: list[dict]) -> list[dict]:
    review_items = [dict(item) for item in candidates if _candidate_is_comment_eligible(job, item)]
    try:
        extracted = extract_docx(job["input_path"])
        paragraph_records = {
            int(item.get("index") or 0): item
            for item in extracted.get("paragraphs") or []
        }
    except Exception:
        paragraph_records = {}
    review_items.sort(key=lambda item: (
        int(item.get("paragraph_index", -1)), int(item.get("raw_start") or 0), int(item.get("id") or 0),
    ))
    for number, item in enumerate(review_items, start=1):
        paragraph = paragraph_records.get(int(item.get("paragraph_index") or 0), {})
        enriched = _candidate_with_verified_note_marker(item, paragraph)
        item.clear()
        item.update(enriched)
        text = str(paragraph.get("text") or "")
        start = max(0, min(int(item.get("raw_start") or 0), len(text)))
        end = max(start, min(int(item.get("raw_end") or start), len(text)))
        item["context_before"] = text[max(0, start - 48):start]
        item["context_after"] = text[end:end + 48]
        item["review_number"] = number
    return review_items


def _pdf_normalized_geometry(page: fitz.Page) -> tuple[str, list[fitz.Rect]]:
    """Return normalized text and boxes in visual reading order.

    LibreOffice can emit one justified Chinese line as several PDF text objects
    whose storage order differs from their left-to-right visual order.  PyMuPDF's
    sorted word extraction resolves that geometry while preserving enough boxes
    for deterministic highlighting.  The raw-character fallback remains fail-
    closed for unusual PDFs that expose no words.
    """
    normalized_chars: list[str] = []
    boxes: list[fitz.Rect] = []
    for word in page.get_text("words", sort=True) or []:
        if len(word) < 5:
            continue
        value = normalize(str(word[4] or ""))
        if not value:
            continue
        box = fitz.Rect(word[:4])
        for normalized_char in value:
            normalized_chars.append(normalized_char)
            boxes.append(box)
    if normalized_chars:
        return "".join(normalized_chars), boxes

    raw = page.get_text("rawdict", sort=True)
    for block in raw.get("blocks") or []:
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                for char in span.get("chars") or []:
                    value = normalize(str(char.get("c") or ""))
                    if not value:
                        continue
                    box = fitz.Rect(char.get("bbox") or span.get("bbox"))
                    for normalized_char in value:
                        normalized_chars.append(normalized_char)
                        boxes.append(box)
    return "".join(normalized_chars), boxes


def _normalized_pdf_occurrences(
    page: fitz.Page, term: str, *, before: str = "", after: str = "",
    geometry: tuple[str, list[fitz.Rect]] | None = None,
) -> list[fitz.Rect]:
    """Locate one unique normalized occurrence in visual PDF reading order."""
    haystack, boxes = geometry if geometry is not None else _pdf_normalized_geometry(page)
    needle = normalize(term)
    if len(needle) < 6 or not haystack:
        return []
    starts: list[int] = []
    offset = 0
    while True:
        found = haystack.find(needle, offset)
        if found < 0:
            break
        starts.append(found)
        offset = found + 1
    before_tail = normalize(before)[-12:]
    after_head = normalize(after)[:12]
    if len(starts) > 1 and (before_tail or after_head):
        contextual = [
            start for start in starts
            if (not before_tail or haystack[max(0, start - len(before_tail)):start] == before_tail)
            and (
                not after_head
                or haystack[start + len(needle):start + len(needle) + len(after_head)] == after_head
            )
        ]
        if contextual:
            starts = contextual
    if len(starts) != 1:
        return []
    selected = boxes[starts[0]:starts[0] + len(needle)]
    rectangles: list[fitz.Rect] = []
    for box in selected:
        if (
            rectangles
            and abs(rectangles[-1].y0 - box.y0) < 1.5
            and box.x0 <= rectangles[-1].x1 + 3.0
        ):
            rectangles[-1] |= box
        else:
            rectangles.append(fitz.Rect(box))
    return rectangles


def _locate_pdf_candidate(
    document: fitz.Document, page_texts: list[str], candidate: dict,
    *, page_geometries: list[tuple[str, list[fitz.Rect]]] | None = None,
) -> tuple[int, list[fitz.Rect]]:
    value = re.sub(r"\s+", " ", str(candidate.get("paper_text") or "")).strip()
    if not value:
        raise CitationAssistantError("PDF 批注定位校验失败：校注记录缺少正文引文。")
    ellipsis_parts = [
        part.strip() for part in re.split(r"(?:\.{3,}|…{1,}|⋯{1,})", value)
        if len(normalize(part)) >= 6
    ]
    search_groups = [ellipsis_parts] if len(ellipsis_parts) >= 2 else []
    search_groups.extend([[term] for term in _candidate_pdf_search_terms(candidate)])
    before = normalize(str(candidate.get("context_before") or ""))
    after = normalize(str(candidate.get("context_after") or ""))
    had_page_match = False
    had_ambiguous_match = False
    for terms in search_groups:
        normalized_terms = [normalize(term) for term in terms if len(normalize(term)) >= 6]
        if not normalized_terms:
            continue
        pages = [
            index for index, page_text in enumerate(page_texts)
            if all(term in page_text for term in normalized_terms)
        ]
        had_page_match = had_page_match or bool(pages)
        if len(pages) > 1 and (before or after):
            contextual = [
                index for index in pages
                if (not before or before[-12:] in page_texts[index])
                and (not after or after[:12] in page_texts[index])
            ]
            if contextual:
                pages = contextual
        if len(pages) != 1:
            had_ambiguous_match = had_ambiguous_match or len(pages) > 1
            continue
        page_index = pages[0]
        page = document[page_index]
        geometry = (
            page_geometries[page_index]
            if page_geometries is not None and page_index < len(page_geometries)
            else None
        )
        rectangles: list[fitz.Rect] = []
        ordered_y = -1.0
        valid = True
        for term_index, term in enumerate(terms):
            found = _normalized_pdf_occurrences(
                page, term,
                before=str(candidate.get("context_before") or "") if term_index == 0 else "",
                after=str(candidate.get("context_after") or "") if term_index == len(terms) - 1 else "",
                geometry=geometry,
            )
            if not found:
                had_ambiguous_match = True
                valid = False
                break
            if ordered_y >= 0:
                found = [rect for rect in found if rect.y1 + 1 >= ordered_y]
                if not found:
                    valid = False
                    break
            rectangles.extend(found)
            ordered_y = found[-1].y1
        if valid and rectangles:
            return page_index, rectangles
    if had_ambiguous_match:
        raise CitationAssistantError(
            f"PDF 批注定位校验失败：存在多个候选位置“{value[:40]}”。"
        )
    detail = "文本层未找到可验证位置" if not had_page_match else "文本层位置无法验证"
    raise CitationAssistantError(f"PDF 批注定位校验失败：{detail}“{value[:40]}”。")


def _candidate_viewer_url(candidate: dict) -> str:
    option = _selected_option(candidate)
    reader_url = str(option.get("viewer_url") or "").strip() if option else ""
    public_base = str(os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if public_base and reader_url.startswith("/"):
        return urljoin(f"{public_base}/", reader_url.lstrip("/"))
    return reader_url


def _render_review_margin_annotations(path: Path, candidates: list[dict]) -> dict:
    """Add a printable review margin while keeping Writer's original page intact."""
    review_items = list(candidates)[:2000]
    if not review_items:
        return {"located": 0, "failed": 0}

    temporary = path.with_name(f".{path.stem}-{uuid.uuid4().hex}.annotated.pdf")
    document = fitz.open(path)
    try:
        if document.page_count < 1:
            raise CitationAssistantError("批注式 PDF 没有有效页面。")
        page_geometries = [_pdf_normalized_geometry(page) for page in document]
        page_texts = [geometry[0] for geometry in page_geometries]
        grouped: dict[int, list[tuple[dict, list[fitz.Rect]]]] = defaultdict(list)

        for candidate in review_items:
            page_index, rectangles = _locate_pdf_candidate(
                document, page_texts, candidate, page_geometries=page_geometries,
            )
            grouped[page_index].append((candidate, rectangles))

        font_file = _pdf_font_file()
        font_name = "ca-review-cjk" if font_file else "china-s"
        margin_width = 310.0
        for page_index in range(document.page_count):
            page = document[page_index]
            original_width, page_height = page.rect.width, page.rect.height
            page.set_mediabox(fitz.Rect(0, 0, original_width + margin_width, page_height))
            page = document.reload_page(page)
            if font_file:
                page.insert_font(fontname=font_name, fontfile=str(font_file))

            separator_x = original_width + 12
            page.draw_line(
                fitz.Point(separator_x, 20), fitz.Point(separator_x, page_height - 20),
                color=(0.72, 0.72, 0.72), width=0.7, overlay=True,
            )
            page.insert_text(
                (separator_x + 12, 24), "引文校注批注",
                fontsize=10, fontname=font_name, color=(0.55, 0.08, 0.08), overlay=True,
            )
            items = grouped.get(page_index) or []
            if not items:
                page.insert_text(
                    (separator_x + 12, 43), "本页无校注事项",
                    fontsize=7.2, fontname=font_name, color=(0.45, 0.45, 0.45), overlay=True,
                )
                continue

            available_height = max(80.0, page_height - 58.0)
            slot_height = available_height / len(items)
            font_size = 7.2 if len(items) <= 5 else (6.4 if len(items) <= 8 else 5.5)
            char_limit = 720 if len(items) <= 3 else (420 if len(items) <= 5 else (240 if len(items) <= 8 else 140))
            for item_index, (candidate, rectangles) in enumerate(items, start=1):
                box_top = 38.0 + (item_index - 1) * slot_height
                box_bottom = min(page_height - 12.0, box_top + slot_height - 5.0)
                box = fitz.Rect(separator_x + 10, box_top, original_width + margin_width - 10, box_bottom)
                page.draw_rect(
                    box, color=(0.75, 0.24, 0.20), fill=(1.0, 0.97, 0.94),
                    width=0.65, fill_opacity=0.96, overlay=True,
                )
                for rectangle in rectangles:
                    page.draw_rect(
                        rectangle, color=(0.95, 0.55, 0.0), fill=(1.0, 0.92, 0.18),
                        width=0.6, fill_opacity=0.24, overlay=True,
                    )
                anchor = rectangles[0] if rectangles else fitz.Rect(original_width - 2, box_top, original_width, box_top + 2)
                page.draw_line(
                    fitz.Point(min(original_width, anchor.x1), anchor.y0),
                    fitz.Point(box.x0, box.y0 + 9),
                    color=(0.75, 0.24, 0.20), width=0.55, overlay=True,
                )

                reader_url = _candidate_viewer_url(candidate)
                comment = _candidate_comment_text(candidate)
                if reader_url:
                    comment = comment.replace(
                        f"原文链接：{reader_url}", "原文链接：点击本批注框打开",
                    )
                comment = f"批注 {int(candidate.get('review_number') or item_index)}\n{comment}"
                if len(comment) > char_limit:
                    comment = comment[: max(20, char_limit - 1)].rstrip() + "…"
                page.insert_textbox(
                    fitz.Rect(box.x0 + 7, box.y0 + 5, box.x1 - 7, box.y1 - 4),
                    comment, fontsize=font_size, lineheight=1.18, fontname=font_name,
                    color=(0.18, 0.13, 0.11), overlay=True,
                )
                if reader_url:
                    page.insert_link({"kind": fitz.LINK_URI, "from": box, "uri": reader_url})

        metadata = document.metadata
        metadata.update({"title": "论文引文校注批注版", "author": "论文引文助手"})
        document.set_metadata(metadata)
        if font_file and hasattr(document, "subset_fonts"):
            document.subset_fonts(fallback=True)
        document.save(temporary, garbage=4, deflate=True)
    finally:
        document.close()
    try:
        if not temporary.is_file() or temporary.stat().st_size < 1000:
            raise CitationAssistantError("批注式 PDF 后处理未生成有效文件。")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"located": len(review_items), "failed": 0}


def _validate_annotated_pdf(path: Path, job: dict, candidates: list[dict]) -> dict:
    page_count = 0
    rendered_nonblank = 0
    text_parts: list[str] = []
    with fitz.open(path) as document:
        if document.page_count < 1:
            raise CitationAssistantError("批注式 PDF 没有有效页面。")
        page_count = document.page_count
        for page in document:
            visual_text, _boxes = _pdf_normalized_geometry(page)
            text_parts.append(visual_text)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.6, 0.6), colorspace=fitz.csGRAY, alpha=False)
            if pixmap.samples and min(pixmap.samples) < 250:
                rendered_nonblank += 1
    if rendered_nonblank != page_count:
        raise CitationAssistantError("批注式 PDF 存在无法正常渲染的空白页面。")
    pdf_norm = normalize("\n".join(text_parts))
    visible = [candidate for candidate in candidates if candidate.get("decision") != "rejected"]
    labels = [normalize(str(candidate.get("issue_label") or "")) for candidate in visible]
    labels = [label for label in labels if label]
    if labels and not any(label in pdf_norm for label in labels[:20]):
        raise CitationAssistantError("批注式 PDF 未呈现校注批注，已拒绝发布机械报告。")

    try:
        original = extract_docx(job["input_path"])
        samples = [
            normalize(str(paragraph.get("text") or ""))[:24]
            for paragraph in original.get("paragraphs") or []
            if len(normalize(str(paragraph.get("text") or ""))) >= 18
        ][:40]
        retained = sum(bool(sample and sample in pdf_norm) for sample in samples)
        if samples and retained / len(samples) < 0.60:
            raise CitationAssistantError("批注式 PDF 正文视觉文本完整性校验未通过。")
    except CitationAssistantError:
        raise
    except Exception as exc:
        raise CitationAssistantError("无法核验批注式 PDF 与原论文的一致性。") from exc
    return {"pages": page_count, "rendered_pages": rendered_nonblank}


def _pdf_lines(text: str, width_chars: int = 48) -> list[str]:
    value = str(text or "").replace("\r", "").strip()
    if not value:
        return [""]
    lines: list[str] = []
    for source in value.split("\n"):
        source = source.strip()
        while len(source) > width_chars:
            cut = width_chars
            for marker in "。；，、.!?; ":
                pos = source.rfind(marker, max(1, width_chars - 10), width_chars + 1)
                if pos >= width_chars - 10:
                    cut = pos + 1
                    break
            lines.append(source[:cut])
            source = source[cut:]
        lines.append(source)
    return lines


def _pdf_font_file() -> Path | None:
    configured = str(os.environ.get("CITATION_ASSISTANT_PDF_FONT") or "").strip()
    candidates = [
        configured,
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\Deng.ttf",
    ]
    for value in candidates:
        if value and Path(value).is_file():
            return Path(value)
    return None


def export_legacy_report_pdf(job: dict, candidates: list[dict]) -> Path:
    """Legacy list-style report retained only for explicit diagnostics/tests."""
    output = _job_dir(int(job["user_id"]), str(job["id"]), create=True) / "论文引文校对报告.pdf"
    document = fitz.open()
    page = None
    y = 0.0
    page_no = 0
    margin_x, top, bottom = 46.0, 50.0, 46.0
    body_size, leading = 9.2, 14.0
    font_file = _pdf_font_file()
    pdf_font_name = "ca-cjk" if font_file else "china-s"

    def new_page() -> None:
        nonlocal page, y, page_no
        page = document.new_page(width=595, height=842)
        if font_file:
            page.insert_font(fontname=pdf_font_name, fontfile=str(font_file))
        page_no += 1
        y = top
        page.insert_text((margin_x, 28), "论文引文与注释校对报告", fontsize=8, fontname=pdf_font_name, color=(0.35, 0.35, 0.35))

    def write(
        text: str, *, size: float = body_size, color=(0, 0, 0), gap: float = 0.0,
        uri: str = "",
    ) -> None:
        nonlocal y
        assert page is not None
        width_chars = max(20, int((595 - 2 * margin_x) / size))
        for line_index, line in enumerate(_pdf_lines(text, width_chars)):
            if y > 842 - bottom:
                new_page()
            baseline = y
            page.insert_text((margin_x, y), line, fontsize=size, fontname=pdf_font_name, color=color)
            if uri and line_index == 0:
                page.insert_link({
                    "kind": fitz.LINK_URI,
                    "from": fitz.Rect(margin_x, baseline - size, min(595 - margin_x, margin_x + len(line) * size), baseline + 3),
                    "uri": uri,
                })
            y += max(leading, size * 1.45)
        y += gap

    new_page()
    write("论文引文与注释校对报告", size=17, gap=8)
    write(f"文件：{job.get('original_filename') or ''}")
    write(f"任务：{job.get('id')}  模式：{job.get('mode')}  注释：{job.get('note_kind')}")
    write(f"引文格式：{job.get('resolved_style') or job.get('citation_style')}  匹配阈值：{job.get('threshold_mode')}")
    write(f"语料版本：{job.get('corpus_sha256') or '未记录'}  模板版本：{job.get('template_version') or '未记录'}")
    write(f"生成时间：{_iso()}  候选：{len(candidates)}  已采用：{sum(c.get('decision') == 'accepted' for c in candidates)}", gap=8)

    for index, candidate in enumerate(candidates, start=1):
        option = _selected_option(candidate)
        write(f"{index}. {candidate.get('issue_label') or candidate.get('issue_code') or '待处理'} · {candidate.get('decision')}", size=11, color=(0.08, 0.24, 0.42), gap=2)
        write(f"论文原句：{candidate.get('paper_text') or '（无）'}")
        if candidate.get("existing_note_text"):
            write(f"原注：{candidate.get('existing_note_text')}")
        if option:
            write(f"原著：{option.get('display_title') or option.get('citation_title') or option.get('source_file') or ''}")
            write(f"页码：印刷页 {','.join(map(str, option.get('printed_pages') or [])) or '无'}；PDF 页 {','.join(map(str, option.get('pdf_pages') or [])) or '无'}")
            write(f"原文上下文：{option.get('context') or ''}")
            source = str(option.get("source_file") or "")
            pages = list(option.get("pdf_pages") or [])
            reader_url = str(option.get("viewer_url") or "").strip()
            if not reader_url and source and pages:
                reader_url = f"/reader?file={quote(source)}&page={pages[0]}"
            if reader_url:
                public_base = str(os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
                if public_base and reader_url.startswith("/"):
                    reader_url = urljoin(f"{public_base}/", reader_url.lstrip("/"))
                write(f"打开原文：{reader_url}", color=(0.05, 0.3, 0.65), uri=reader_url)
        write(f"建议：{candidate.get('proposed_citation') or '（仅报告）'}", gap=7)

    for number, pdf_page in enumerate(document, start=1):
        footer = f"第 {number} / {document.page_count} 页"
        if font_file:
            pdf_page.insert_font(fontname=pdf_font_name, fontfile=str(font_file))
        pdf_page.insert_text((595 - margin_x - 62, 818), footer, fontsize=8, fontname=pdf_font_name, color=(0.4, 0.4, 0.4))
    metadata = document.metadata
    metadata.update({"title": "论文引文与注释校对报告", "author": "论文引文助手"})
    document.set_metadata(metadata)
    # CJK TTC files are often tens of megabytes.  Keep the font embedded for reliable
    # printing, but subset it to the glyphs actually used by this report.
    if font_file and hasattr(document, "subset_fonts"):
        document.subset_fonts(fallback=True)
    document.save(output, garbage=4, deflate=True)
    document.close()
    # Reopen and parse every page: catches truncated/corrupt report output before publishing it.
    with fitz.open(output) as check:
        for report_page in check:
            report_page.get_text("text")
    return output


def export_pdf(
    job: dict, candidates: list[dict], *, source_docx: str | Path | None = None,
    suppress_agent_comments_for_conversion: bool = False,
) -> Path:
    """Render the final Word copy, then add strictly located proofreading margins."""
    mode = str(job.get("mode") or "both")
    if mode == "generate":
        raise CitationAssistantError("插注模式只输出 Word，不生成 PDF。")
    source = Path(source_docx) if source_docx is not None else export_docx(job, candidates)
    if not source.is_file():
        raise CitationAssistantError("最终 Word 副本不存在，无法生成对应 PDF。")
    stem = Path(str(job.get("original_filename") or "论文.docx")).stem
    name = f"{stem}_校注批注版.pdf" if mode == "audit" else f"{stem}_插注校注批注版.pdf"
    output = _job_dir(int(job["user_id"]), str(job["id"]), create=True) / name
    review_items = _pdf_review_manifest(job, candidates)
    conversion_source = source
    transient_source: Path | None = None
    try:
        if suppress_agent_comments_for_conversion:
            transient_source = _build_pdf_conversion_copy_without_agent_comments(source)
            conversion_source = transient_source
        _convert_annotated_docx_to_pdf(conversion_source, output)
        _render_review_margin_annotations(output, review_items)
        _validate_annotated_pdf(output, job, review_items)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        if transient_source is not None:
            transient_source.unlink(missing_ok=True)
    return output


def _export_candidate_counts(job: dict, candidates: list[dict]) -> dict[str, int]:
    input_path = Path(str(job["input_path"]))
    try:
        with zipfile.ZipFile(input_path) as package:
            document = _parse_xml(package.read("word/document.xml"))
        paragraphs = document.xpath("./w:body//w:p", namespaces=NS)
    except Exception:
        paragraphs = []
    inserted = sum(_candidate_is_insertion_eligible(job, item, paragraphs) for item in candidates)
    generated = sum(str(item.get("kind") or "") == "generate" for item in candidates)
    commented = sum(_candidate_is_comment_eligible(job, item) for item in candidates)
    audit_candidates = [item for item in candidates if str(item.get("kind") or "") == "audit"]
    readonly = sum(str(item.get("writeback_mode") or "") == "readonly" for item in audit_candidates)
    return {
        "auto_insert_eligible_count": int(inserted),
        "inserted_count": int(inserted),
        "not_inserted_count": max(0, int(generated) - int(inserted)),
        "proofread_eligible_count": int(commented),
        "commented_count": int(commented),
        "readonly_count": int(readonly),
    }


def queue_pdf_retry(job_id: str, user_id: int) -> dict:
    """Atomically queue PDF-only recovery while preserving the final Word artifact."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _row_dict(conn.execute(
            "SELECT * FROM citation_assistant_jobs WHERE id=? AND user_id=?",
            (str(job_id), int(user_id)),
        ).fetchone())
        if not row:
            raise CitationAssistantError("任务不存在。")
        if str(row.get("mode") or "") not in {"audit", "both"}:
            raise CitationAssistantError("插注模式不生成 PDF。")
        if str(row.get("status") or "") != "complete":
            raise CitationAssistantError("当前任务不能重试 PDF。")
        if str(row.get("pdf_export_status") or "") == "converting":
            raise CitationAssistantError("PDF 正在生成，请勿重复提交。")
        if str(row.get("word_export_status") or "") != "ready":
            raise CitationAssistantError("最终 Word 副本尚未就绪。")
        word_path = Path(str(row.get("output_docx_path") or ""))
        job_dir = _job_dir(int(user_id), str(job_id)).resolve()
        try:
            resolved = word_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CitationAssistantError("最终 Word 副本不存在，无法重试 PDF。") from exc
        if job_dir not in resolved.parents:
            raise CitationAssistantError("Word 副本路径校验失败。")
        now = _iso()
        changed = conn.execute(
            "UPDATE citation_assistant_jobs SET status='exporting',pdf_export_status='converting',"
            "output_pdf_path='',pdf_position_failure_count=0,error='',lease_owner='',"
            "lease_expires_at='',updated_at=? WHERE id=? AND user_id=? AND status='complete' "
            "AND pdf_export_status!='converting'",
            (now, str(job_id), int(user_id)),
        ).rowcount
        if changed != 1:
            raise CitationAssistantError("PDF 重试状态已变更，请刷新后重试。")
    return get_job(job_id, user_id) or {}


def run_pdf_export(job_id: str) -> None:
    """Regenerate only PDF from the already published final Word copy."""
    row = get_job(job_id)
    if not row:
        return
    docx_path = Path(str(row.get("output_docx_path") or ""))
    try:
        if str(row.get("mode") or "") not in {"audit", "both"}:
            raise CitationAssistantError("插注模式不生成 PDF。")
        if str(row.get("word_export_status") or "") != "ready" or not docx_path.is_file():
            raise CitationAssistantError("最终 Word 副本不存在，无法重试 PDF。")
        candidates = all_candidates(job_id)
        pdf_path = export_pdf(row, candidates, source_docx=docx_path)
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
        job_id, status="complete", output_pdf_path=str(pdf_path), pdf_export_status="ready",
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
        candidates = all_candidates(job_id)
        counts = _export_candidate_counts(row, candidates)
        docx_path = str(export_docx(row, candidates))
        update_job(job_id, output_docx_path=docx_path, word_export_status="ready", **counts)
        if str(row.get("mode") or "both") == "generate":
            update_job(
                job_id, status="complete", output_pdf_path="", pdf_export_status="not_requested",
                pdf_position_failure_count=0, progress_done=1, progress_total=1, error="",
            )
            return
        update_job(job_id, pdf_export_status="converting", output_pdf_path="", error="")
        try:
            pdf_path = export_pdf(row, candidates, source_docx=docx_path)
        except Exception as pdf_exc:
            position_failed = "定位校验失败" in str(pdf_exc)
            update_job(
                job_id, status="complete", output_docx_path=docx_path, output_pdf_path="",
                pdf_export_status="position_failed" if position_failed else "failed",
                pdf_position_failure_count=1 if position_failed else 0,
                progress_done=1, progress_total=1,
                error=f"Word 已生成；PDF 未生成：{str(pdf_exc)[:360]}",
            )
            return
        update_job(
            job_id, status="complete", output_docx_path=docx_path,
            output_pdf_path=str(pdf_path), pdf_export_status="ready", pdf_position_failure_count=0,
            progress_done=1, progress_total=1, error="",
        )
    except Exception as exc:
        update_job(job_id, status="failed", word_export_status="failed", error=str(exc)[:500])


def delete_job(job_id: str, user_id: int) -> bool:
    row = get_job(job_id, user_id)
    if not row:
        return False
    directory = _job_dir(int(user_id), str(job_id))
    with _connect() as conn:
        conn.execute("DELETE FROM citation_assistant_candidates WHERE job_id=?", (str(job_id),))
        conn.execute("DELETE FROM citation_assistant_agent_shadow_runs WHERE job_id=?", (str(job_id),))
        conn.execute(
            "UPDATE citation_assistant_jobs SET status='deleted',input_path='',extraction_path='',"
            "output_docx_path='',output_pdf_path='',sections_json='[]',selected_sections_json='[]',"
            "flags_json='{}',error='',updated_at=? WHERE id=? AND user_id=?",
            (_iso(), str(job_id), int(user_id)),
        )
    if directory.exists():
        shutil.rmtree(directory)
    return True


def purge_expired(now: datetime | None = None) -> int:
    cutoff = _iso(now or _utcnow())
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id,user_id FROM citation_assistant_jobs WHERE expires_at<=? AND status NOT IN ('expired','deleted')",
            (cutoff,),
        ).fetchall()
    removed = 0
    for row in rows:
        directory = _job_dir(int(row["user_id"]), str(row["id"]))
        with _connect() as conn:
            conn.execute("DELETE FROM citation_assistant_candidates WHERE job_id=?", (str(row["id"]),))
            conn.execute("DELETE FROM citation_assistant_agent_shadow_runs WHERE job_id=?", (str(row["id"]),))
            conn.execute(
                "UPDATE citation_assistant_jobs SET status='expired',input_path='',extraction_path='',"
                "output_docx_path='',output_pdf_path='',sections_json='[]',selected_sections_json='[]',"
                "flags_json='{}',error='',updated_at=? WHERE id=?",
                (_iso(), str(row["id"])),
            )
        if directory.exists():
            shutil.rmtree(directory)
        removed += 1
    return removed


def start_background(job_id: str, target: Callable[..., None], *args, **kwargs) -> bool:
    with _JOB_LOCK:
        if job_id in _RUNNING_JOBS:
            return False
        _RUNNING_JOBS.add(job_id)

    def runner() -> None:
        try:
            target(job_id, *args, **kwargs)
        finally:
            with _JOB_LOCK:
                _RUNNING_JOBS.discard(job_id)

    threading.Thread(target=runner, name=f"citation-{job_id[:8]}", daemon=True).start()
    return True


def claim_next_job(worker_id: str, lease_seconds: int = 600, *,
                   corpus_sha256: str | None = None,
                   template_version: str | None = None) -> dict | None:
    """Atomically claim one runnable stage; an expired lease is recoverable after a crash."""
    owner = re.sub(r"[^A-Za-z0-9_.:-]", "", str(worker_id or ""))[:100]
    if not owner:
        raise CitationAssistantError("工作节点标识无效。")
    now = _utcnow()
    now_text = _iso(now)
    lease_until = _iso(now + timedelta(seconds=max(60, int(lease_seconds))))
    version_clause = ""
    version_values: list[str] = []
    if corpus_sha256 is not None:
        version_clause += " AND corpus_sha256=?"
        version_values.append(str(corpus_sha256))
    if template_version is not None:
        version_clause += " AND template_version=?"
        version_values.append(str(template_version))
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM citation_assistant_jobs "
            "WHERE status IN ('extracting','queued','matching','exporting') AND expires_at>? "
            "AND (lease_owner='' OR lease_expires_at='' OR lease_expires_at<=?) "
            + version_clause +
            "AND (status NOT IN ('queued','matching') OR "
            "(SELECT COUNT(*) FROM citation_assistant_jobs active "
            " WHERE active.status='matching' AND active.lease_owner!='' AND active.lease_expires_at>?) < ?) "
            "ORDER BY CASE status WHEN 'exporting' THEN 0 WHEN 'extracting' THEN 1 ELSE 2 END,created_at LIMIT 1",
            (now_text, now_text, *version_values, now_text, MATCH_CONCURRENCY),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        job_id = str(row["id"])
        stage = str(row["status"])
        next_status = "matching" if stage in {"queued", "matching"} else stage
        changed = conn.execute(
            "UPDATE citation_assistant_jobs SET status=?,lease_owner=?,lease_expires_at=?,updated_at=? "
            "WHERE id=? AND (lease_owner='' OR lease_expires_at='' OR lease_expires_at<=?)",
            (next_status, owner, lease_until, now_text, job_id, now_text),
        ).rowcount
        conn.commit()
    if not changed:
        return None
    claimed = get_job(job_id)
    if claimed is not None:
        claimed["claimed_stage"] = stage
    return claimed


def has_runnable_job() -> bool:
    """Return whether the established citation lane has work ready for this worker."""
    now_text = _iso()
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM citation_assistant_jobs "
            "WHERE status IN ('extracting','queued','matching','exporting') AND expires_at>? "
            "AND (lease_owner='' OR lease_expires_at='' OR lease_expires_at<=?) "
            "AND (status NOT IN ('queued','matching') OR "
            "(SELECT COUNT(*) FROM citation_assistant_jobs active "
            " WHERE active.status='matching' AND active.lease_owner!='' AND active.lease_expires_at>?) < ?) "
            "LIMIT 1",
            (now_text, now_text, now_text, MATCH_CONCURRENCY),
        ).fetchone()
    return row is not None


def renew_lease(job_id: str, worker_id: str, lease_seconds: int = 600) -> bool:
    until = _iso(_utcnow() + timedelta(seconds=max(60, int(lease_seconds))))
    with _connect() as conn:
        changed = conn.execute(
            "UPDATE citation_assistant_jobs SET lease_expires_at=?,updated_at=? WHERE id=? AND lease_owner=?",
            (until, _iso(), str(job_id), str(worker_id)),
        ).rowcount
    return bool(changed)


def resume_jobs(
    corpus,
    *,
    analysis_target: Callable[[str], None] | None = None,
    export_target: Callable[[str], None] | None = None,
) -> int:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id,status FROM citation_assistant_jobs WHERE status IN ('extracting','queued','matching','exporting') "
            "AND expires_at>? ORDER BY created_at",
            (_iso(),),
        ).fetchall()
    resumed = 0
    for row in rows:
        job_id, status = str(row["id"]), str(row["status"])
        if status == "extracting":
            resumed += int(start_background(job_id, run_extraction))
        elif status in {"queued", "matching"}:
            update_job(job_id, status="queued")
            if analysis_target is not None:
                resumed += int(start_background(job_id, analysis_target))
            else:
                resumed += int(start_background(job_id, run_analysis, corpus))
        elif status == "exporting":
            resumed += int(start_background(job_id, export_target or run_export))
    return resumed
