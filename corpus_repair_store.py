from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from runtime_env import APPDATA_DIR


STATUS_FLOW = (
    "detected",
    "ai_reviewed",
    "automatic_candidate",
    "manual_review",
    "approved",
    "rejected",
    "deferred",
    "applied",
)
DECISION_STATUSES = {"approved", "rejected", "deferred"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def repair_root() -> Path:
    configured = str(os.environ.get("MARX_CORPUS_REPAIR_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name != "nt":
        return Path("/home/data/marx-search-corpus-repair")
    return APPDATA_DIR / "corpus_repair"


def review_db_path() -> Path:
    configured = str(os.environ.get("MARX_CORPUS_REPAIR_DB") or "").strip()
    return Path(configured).expanduser() if configured else repair_root() / "review.sqlite3"


def _connect(path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    target = Path(path or review_db_path()).resolve()
    if readonly:
        conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=10)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        if os.name != "nt":
            for shared in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
                try:
                    if shared.exists():
                        shared.chmod(0o660)
                except OSError:
                    pass
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def init_db(path: Path | None = None) -> Path:
    target = Path(path or review_db_path()).resolve()
    with _connect(target) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repair_batches(
              id TEXT PRIMARY KEY,
              phase INTEGER NOT NULL,
              state TEXT NOT NULL DEFAULT 'planned',
              source_database TEXT NOT NULL,
              source_database_sha256 TEXT NOT NULL,
              candidate_database TEXT NOT NULL DEFAULT '',
              candidate_database_sha256 TEXT NOT NULL DEFAULT '',
              validation_json TEXT NOT NULL DEFAULT '{}',
              planned_pages INTEGER NOT NULL DEFAULT 0,
              scanned_pages INTEGER NOT NULL DEFAULT 0,
              flagged_pages INTEGER NOT NULL DEFAULT 0,
              api_calls_mimo INTEGER NOT NULL DEFAULT 0,
              api_calls_deepseek INTEGER NOT NULL DEFAULT 0,
              canary_stage INTEGER NOT NULL DEFAULT 0,
              canary_progress INTEGER NOT NULL DEFAULT 0,
              canary_unsafe INTEGER NOT NULL DEFAULT 0,
              paused_reason TEXT NOT NULL DEFAULT '',
              last_checkpoint_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              completed_at TEXT NOT NULL DEFAULT '',
              confirmed_at TEXT NOT NULL DEFAULT '',
              confirmed_by TEXT NOT NULL DEFAULT '',
              promotion_status TEXT NOT NULL DEFAULT '',
              promotion_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS repair_pages(
              batch_id TEXT NOT NULL,
              page_id INTEGER NOT NULL,
              plan_order INTEGER NOT NULL,
              book TEXT NOT NULL,
              volume INTEGER NOT NULL,
              source_file TEXT NOT NULL,
              pdf_page INTEGER NOT NULL,
              printed_page TEXT NOT NULL DEFAULT '',
              baseline_hash TEXT NOT NULL,
              baseline_raw TEXT NOT NULL,
              baseline_normalized TEXT NOT NULL,
              risk_score REAL NOT NULL DEFAULT 0,
              risk_reasons TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'planned',
              attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              completed_at TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(batch_id,page_id),
              UNIQUE(batch_id,plan_order),
              FOREIGN KEY(batch_id) REFERENCES repair_batches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS repair_issues(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id TEXT NOT NULL,
              page_id INTEGER NOT NULL,
              book TEXT NOT NULL,
              volume INTEGER NOT NULL,
              source_file TEXT NOT NULL,
              pdf_page INTEGER NOT NULL,
              printed_page TEXT NOT NULL DEFAULT '',
              baseline_hash TEXT NOT NULL,
              error_type TEXT NOT NULL,
              before_text TEXT NOT NULL,
              proposed_text TEXT NOT NULL DEFAULT '',
              left_anchor TEXT NOT NULL DEFAULT '',
              right_anchor TEXT NOT NULL DEFAULT '',
              rapid_value TEXT NOT NULL DEFAULT '',
              mimo_value TEXT NOT NULL DEFAULT '',
              deepseek_decision TEXT NOT NULL DEFAULT '',
              deepseek_risks TEXT NOT NULL DEFAULT '[]',
              authority_value TEXT NOT NULL DEFAULT '',
              authority_evidence TEXT NOT NULL DEFAULT '',
              confidence TEXT NOT NULL DEFAULT 'review',
              status TEXT NOT NULL DEFAULT 'detected',
              protected_reasons TEXT NOT NULL DEFAULT '[]',
              evidence_crop TEXT NOT NULL DEFAULT '',
              evidence_json TEXT NOT NULL DEFAULT '{}',
              decision_note TEXT NOT NULL DEFAULT '',
              decided_by TEXT NOT NULL DEFAULT '',
              detected_at TEXT NOT NULL,
              reviewed_at TEXT NOT NULL DEFAULT '',
              decided_at TEXT NOT NULL DEFAULT '',
              applied_at TEXT NOT NULL DEFAULT '',
              UNIQUE(batch_id,page_id,error_type,left_anchor,right_anchor,before_text),
              FOREIGN KEY(batch_id,page_id) REFERENCES repair_pages(batch_id,page_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS repair_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id TEXT NOT NULL,
              at TEXT NOT NULL,
              level TEXT NOT NULL,
              event TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS repair_metrics(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id TEXT NOT NULL,
              at TEXT NOT NULL,
              name TEXT NOT NULL,
              value REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_repair_issues_review
              ON repair_issues(status,batch_id,id);
            CREATE INDEX IF NOT EXISTS idx_repair_pages_status
              ON repair_pages(batch_id,status,plan_order);
            CREATE INDEX IF NOT EXISTS idx_repair_events_batch
              ON repair_events(batch_id,id DESC);
            """
        )
        batch_columns = _column_names(conn, "repair_batches")
        for name, declaration in (
            ("canary_stage", "INTEGER NOT NULL DEFAULT 0"),
            ("canary_progress", "INTEGER NOT NULL DEFAULT 0"),
            ("canary_unsafe", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in batch_columns:
                conn.execute(f'ALTER TABLE repair_batches ADD COLUMN "{name}" {declaration}')
        conn.commit()
    return target


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return {key: row[key] for key in row.keys()} if row is not None else None


def _decode_json_fields(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("risk_reasons", "deepseek_risks", "protected_reasons", "evidence_json", "validation_json"):
        if key not in row:
            continue
        wants_list = key in {"risk_reasons", "deepseek_risks", "protected_reasons"}
        try:
            row[key] = json.loads(str(row[key] or ("[]" if wants_list else "{}")))
        except (TypeError, json.JSONDecodeError):
            row[key] = [] if wants_list else {}
    return row


def status_snapshot(path: Path | None = None) -> dict[str, Any]:
    target = Path(path or review_db_path())
    planning: dict[str, Any] = {}
    planning_path = target.parent / "planning-paused.json"
    try:
        if planning_path.is_file():
            loaded = json.loads(planning_path.read_text(encoding="utf-8"))
            planning = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        planning = {}
    try:
        target_exists = target.is_file()
    except OSError as exc:
        return {"configured": False, "state": "unavailable", "planning": planning,
                "database": str(target), "error": type(exc).__name__, "batches": []}
    if not target_exists:
        return {"configured": False, "state": str(planning.get("state") or "not_initialized"),
                "planning": planning, "database": str(target), "batches": []}
    try:
        with _connect(target, readonly=True) as conn:
            batches = [_decode_json_fields(_dict(row) or {}) for row in conn.execute(
                "SELECT * FROM repair_batches ORDER BY created_at DESC LIMIT 12"
            )]
            latest = batches[0] if batches else {}
            issue_counts = dict(conn.execute(
                "SELECT status,COUNT(*) FROM repair_issues WHERE batch_id=? GROUP BY status",
                (str(latest.get("id") or ""),),
            ).fetchall()) if latest else {}
            page_counts = dict(conn.execute(
                "SELECT status,COUNT(*) FROM repair_pages WHERE batch_id=? GROUP BY status",
                (str(latest.get("id") or ""),),
            ).fetchall()) if latest else {}
            recent_events = [_dict(row) or {} for row in conn.execute(
                "SELECT at,level,event,detail FROM repair_events WHERE batch_id=? ORDER BY id DESC LIMIT 20",
                (str(latest.get("id") or ""),),
            )] if latest else []
            latest_metrics: dict[str, float] = {}
            metrics_at = ""
            if latest:
                metric_rows = conn.execute(
                    "SELECT name,value,at FROM repair_metrics WHERE batch_id=? ORDER BY id DESC LIMIT 64",
                    (str(latest.get("id") or ""),),
                ).fetchall()
                for metric in metric_rows:
                    name = str(metric["name"])
                    if name not in latest_metrics:
                        latest_metrics[name] = float(metric["value"])
                    if not metrics_at:
                        metrics_at = str(metric["at"])
    except sqlite3.Error as exc:
        return {"configured": False, "state": "unavailable", "database": str(target),
                "error": type(exc).__name__, "batches": []}
    return {
        "configured": True,
        "database": str(target),
        "state": str(latest.get("state") or planning.get("state") or "idle"),
        "planning": planning,
        "latest_batch": latest,
        "batches": batches,
        "issue_counts": issue_counts,
        "page_counts": page_counts,
        "resources": {"at": metrics_at, **latest_metrics},
        "recent_events": recent_events,
    }


def list_issues(
    *, path: Path | None = None, status: str = "", batch_id: str = "", page: int = 1, per_page: int = 50
) -> dict[str, Any]:
    target = Path(path or review_db_path())
    page = max(1, int(page))
    per_page = max(1, min(200, int(per_page)))
    try:
        target_exists = target.is_file()
    except OSError:
        return {"items": [], "page": page, "per_page": per_page, "total": 0, "unavailable": True}
    if not target_exists:
        return {"items": [], "page": page, "per_page": per_page, "total": 0}
    clauses: list[str] = []
    values: list[Any] = []
    if status:
        if status not in STATUS_FLOW:
            raise ValueError("invalid repair issue status")
        clauses.append("status=?")
        values.append(status)
    if batch_id:
        clauses.append("batch_id=?")
        values.append(batch_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    try:
        with _connect(target, readonly=True) as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM repair_issues" + where, values).fetchone()[0])
            rows = conn.execute(
                "SELECT * FROM repair_issues" + where
                + " ORDER BY CASE status WHEN 'manual_review' THEN 0 WHEN 'automatic_candidate' THEN 1 ELSE 2 END,id "
                + "LIMIT ? OFFSET ?",
                (*values, per_page, (page - 1) * per_page),
            ).fetchall()
    except sqlite3.Error:
        return {"items": [], "page": page, "per_page": per_page, "total": 0, "unavailable": True}
    return {
        "items": [_decode_json_fields(_dict(row) or {}) for row in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
    }


def get_issue(issue_id: int, *, path: Path | None = None) -> dict[str, Any] | None:
    target = Path(path or review_db_path())
    try:
        target_exists = target.is_file()
    except OSError:
        return None
    if not target_exists:
        return None
    try:
        with _connect(target, readonly=True) as conn:
            row = conn.execute("SELECT * FROM repair_issues WHERE id=?", (int(issue_id),)).fetchone()
    except sqlite3.Error:
        return None
    return _decode_json_fields(_dict(row) or {}) if row is not None else None


def decide_issue(
    issue_id: int, *, decision: str, actor: str, proposed_text: str | None = None,
    note: str = "", path: Path | None = None,
) -> dict[str, Any]:
    if decision not in DECISION_STATUSES:
        raise ValueError("decision must be approved, rejected, or deferred")
    actor = str(actor or "").strip()[:200]
    if not actor:
        raise ValueError("decision actor is required")
    target = init_db(path)
    with _connect(target) as conn:
        row = conn.execute("SELECT * FROM repair_issues WHERE id=?", (int(issue_id),)).fetchone()
        if row is None:
            raise LookupError("repair issue not found")
        current = str(row["status"])
        if current not in {"manual_review", "automatic_candidate", "approved", "rejected", "deferred"}:
            raise ValueError(f"issue in {current!r} cannot be decided")
        replacement = str(row["proposed_text"] if proposed_text is None else proposed_text)
        if decision == "approved" and replacement == str(row["before_text"]):
            raise ValueError("approved repair must change the source text")
        conn.execute(
            "UPDATE repair_issues SET status=?,proposed_text=?,decision_note=?,decided_by=?,decided_at=? WHERE id=?",
            (decision, replacement, str(note or "")[:1000], actor, utc_now(), int(issue_id)),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM repair_issues WHERE id=?", (int(issue_id),)).fetchone()
    return _decode_json_fields(_dict(updated) or {})


def confirm_batch(batch_id: str, *, actor: str, path: Path | None = None) -> dict[str, Any]:
    target = init_db(path)
    actor = str(actor or "").strip()[:200]
    if not actor:
        raise ValueError("confirmation actor is required")
    with _connect(target) as conn:
        batch = conn.execute("SELECT * FROM repair_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise LookupError("repair batch not found")
        state = str(batch["state"])
        promotion_status = str(batch["promotion_status"] or "")
        retrying_failed_promotion = state == "confirmed" and promotion_status == "failed"
        if state != "candidate_ready" and not retrying_failed_promotion:
            raise ValueError("batch candidate is not ready")
        unresolved = int(conn.execute(
            "SELECT COUNT(*) FROM repair_issues WHERE batch_id=? AND status IN ('detected','ai_reviewed','manual_review')",
            (batch_id,),
        ).fetchone()[0])
        if unresolved:
            raise ValueError(f"batch still has {unresolved} unresolved review items")
        validation = json.loads(str(batch["validation_json"] or "{}"))
        if not validation.get("gates_ok") or not str(batch["candidate_database_sha256"] or ""):
            raise ValueError("candidate validation gate has not passed")
        now = utc_now()
        conn.execute(
            "UPDATE repair_batches SET state='confirmed',confirmed_at=?,confirmed_by=?,promotion_status='requested' WHERE id=?",
            (now, actor, batch_id),
        )
        conn.execute(
            "INSERT INTO repair_events(batch_id,at,level,event,detail) VALUES(?,?,'info','batch_confirmed',?)",
            (batch_id, now, actor),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM repair_batches WHERE id=?", (batch_id,)).fetchone()
    return _decode_json_fields(_dict(updated) or {})


def record_event(conn: sqlite3.Connection, batch_id: str, level: str, event: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO repair_events(batch_id,at,level,event,detail) VALUES(?,?,?,?,?)",
        (batch_id, utc_now(), str(level)[:16], str(event)[:80], str(detail or "")[:2000]),
    )
    conn.commit()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def upsert_pages(conn: sqlite3.Connection, rows: Iterable[tuple[Any, ...]], *,
                 checkpoint: Any | None = None, chunk_size: int = 128) -> None:
    sql = (
        "INSERT OR IGNORE INTO repair_pages(batch_id,page_id,plan_order,book,volume,source_file,pdf_page,"
        "printed_page,baseline_hash,baseline_raw,baseline_normalized,risk_score,risk_reasons) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    chunk: list[tuple[Any, ...]] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) < chunk_size:
            continue
        conn.executemany(sql, chunk)
        conn.commit()
        chunk.clear()
        if checkpoint is not None:
            checkpoint()
    if chunk:
        conn.executemany(sql, chunk)
        conn.commit()
    if checkpoint is not None:
        checkpoint()
