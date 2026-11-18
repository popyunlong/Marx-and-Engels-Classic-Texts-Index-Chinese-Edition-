"""Durable execution state for the member citation Agent's private task database."""

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

_active = ContextVar("citation_job_execution", default=None)

JOB_COLUMNS = {
    "execution_generation": "INTEGER NOT NULL DEFAULT 0",
    "recovery_count": "INTEGER NOT NULL DEFAULT 0",
    "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
    "last_progress_at": "TEXT NOT NULL DEFAULT ''",
    "partial_results": "INTEGER NOT NULL DEFAULT 0",
    "resumable": "INTEGER NOT NULL DEFAULT 0",
    "execution_reason": "TEXT NOT NULL DEFAULT ''",
    "resume_stage": "TEXT NOT NULL DEFAULT ''",
    "paragraph_batch_size": "INTEGER NOT NULL DEFAULT 24",
    "review_style": "TEXT NOT NULL DEFAULT ''",
    "policy_version": "INTEGER NOT NULL DEFAULT 1",
}


def init_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS citation_job_checkpoints (
            job_id TEXT NOT NULL REFERENCES citation_assistant_jobs(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(job_id,name)
        )""")
    # This trigger runs only for explicit decisions. Checkpoint writes use INSERT.
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS citation_mark_reviewed
        AFTER UPDATE OF decision,selected_option ON citation_assistant_candidates
        WHEN NEW.decision IN ('accepted','rejected','pending') AND NEW.manually_reviewed=0
        BEGIN
          UPDATE citation_assistant_candidates SET manually_reviewed=1 WHERE id=NEW.id;
        END""")


def put(core, job_id, name, payload, conn=None):
    if conn is None:
        with core._connect() as own:
            put(core, job_id, name, payload, own)
        return
    conn.execute(
        "INSERT OR REPLACE INTO citation_job_checkpoints(job_id,name,payload) VALUES(?,?,?)",
        (job_id, name, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
    )


def read(core, job_id, name):
    with core._connect() as conn:
        row = conn.execute(
            "SELECT payload FROM citation_job_checkpoints WHERE job_id=? AND name=?", (job_id, name)
        ).fetchone()
    return json.loads(row[0]) if row else None


def identity(item):
    paragraph = int(item.get("paragraph_index", -1))
    start = int(item.get("raw_start") or 0)
    end = int(item.get("raw_end") or 0)
    # Legacy candidates sometimes have no Word anchor. Distinct pieces of text
    # must remain independently reviewable even when both have the empty span.
    unpositioned = str(item.get("paper_text") or "") if paragraph < 0 or end <= start else ""
    return (item.get("kind") or "generate", paragraph, start, end, unpositioned,
            item.get("existing_note_kind") or "", item.get("existing_note_id"))



def install_isolation(core):
    """The isolated core has its own module globals. Only claimed task workers get fences."""
    connect = core._connect
    job_dir = core._job_dir

    def fenced_connect():
        conn = connect()
        claim = _active.get()
        if claim is not None:
            # Install connection-local guards: fence writes inside the actual writer
            # transaction, including calls made by the existing extraction/export core.
            job_id, owner, generation = claim
            conn.create_function("worker_job_id", 0, lambda: job_id)
            conn.create_function("worker_owner", 0, lambda: owner)
            conn.create_function("worker_generation", 0, lambda: generation)
            for table, column in (
                ("citation_assistant_jobs", "id"), ("citation_assistant_candidates", "job_id"),
                ("citation_agent_test_runs", "job_id"), ("citation_job_checkpoints", "job_id")
            ):
                for op in ("INSERT", "UPDATE", "DELETE"):
                    ref = "OLD" if op == "DELETE" else "NEW"
                    conn.execute(f"""
                      CREATE TEMP TRIGGER fence_{table}_{op} BEFORE {op} ON main.{table}
                      WHEN {ref}.{column} != worker_job_id() OR NOT EXISTS (
                         SELECT 1 FROM citation_assistant_jobs WHERE id=worker_job_id()
                         AND lease_owner=worker_owner() AND execution_generation=worker_generation()
                         AND lease_expires_at > strftime('%Y-%m-%dT%H:%M:%S+00:00','now')
                         AND expires_at > strftime('%Y-%m-%dT%H:%M:%S+00:00','now')
                         AND status IN ('extracting','queued','matching','exporting')
                      ) BEGIN SELECT RAISE(ABORT,'citation worker lease lost'); END""")
        return conn

    def fenced_directory(user_id, job_id, *, create=False):
        claim = _active.get()
        if create and claim and job_id == claim[0]:
            # Upload created the root. A deleted task must never recreate it.
            check_active(core)
            directory = job_dir(user_id, job_id) / f"run-{claim[2]}"
            directory.mkdir(mode=0o700, exist_ok=True)
            return directory
        return job_dir(user_id, job_id, create=create)

    core._connect = fenced_connect
    core._job_dir = fenced_directory


@contextmanager
def worker_claim(job):
    token = _active.set((job["id"], job["lease_owner"], job["execution_generation"]))
    try:
        yield
    finally:
        _active.reset(token)


def check_active(core):
    claim = _active.get()
    if claim is None:
        return
    with core._connect() as conn:
        live = conn.execute(
            "SELECT 1 FROM citation_assistant_jobs WHERE id=? AND lease_owner=? "
            "AND execution_generation=? AND lease_expires_at>? AND expires_at>? "
            "AND status IN ('extracting','matching','exporting')", (*claim, core._iso(), core._iso()),
        ).fetchone()
    if not live:
        raise core.CitationAssistantError("任务执行代次已变更。")


def claim_next(core, worker_id, *, versions, template_version, lease_seconds=60):
    owner = re.sub(r"[^A-Za-z0-9_.:-]", "", worker_id)[:100]
    if not owner:
        raise core.CitationAssistantError("无效的工作节点。")
    now = core._utcnow()
    stamp = core._iso(now)
    version_sql = ""
    params = []
    if versions:
        version_sql += " AND corpus_sha256 IN (" + ",".join("?" for _ in versions) + ")"
        params += list(versions)
    if template_version is not None:
        version_sql += " AND template_version=?"
        params.append(template_version)
    with core._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        while True:
            row = conn.execute(
                "SELECT * FROM citation_assistant_jobs WHERE status IN ('extracting','exporting','queued','matching') "
                "AND expires_at>? AND (lease_owner='' OR lease_expires_at<=?)" + version_sql +
                " AND (status NOT IN ('queued','matching') OR (SELECT COUNT(*) FROM citation_assistant_jobs "
                "WHERE status='matching' AND lease_owner!='' AND lease_expires_at>?) < ?) "
                "ORDER BY CASE status WHEN 'exporting' THEN 0 WHEN 'extracting' THEN 1 ELSE 2 END,created_at LIMIT 1",
                (stamp, stamp, *params, stamp, core.MATCH_CONCURRENCY)
            ).fetchone()
            if row is None:
                return None
            stage = row["status"]
            recovery = row["recovery_count"] + int(bool(row["lease_owner"]))
            if recovery <= 2:
                break
            interrupt(conn, core, row, "recovery_limit", "任务连续中断，已保留进度。")
        conn.execute(
            "UPDATE citation_assistant_jobs SET status=?,lease_owner=?,lease_expires_at=?,"
            "execution_generation=execution_generation+1,recovery_count=?,heartbeat_at=?,updated_at=?,"
            "execution_reason='',resumable=CASE WHEN status='exporting' THEN resumable ELSE 0 END WHERE id=?",
            ("matching" if stage in {"queued","matching"} else stage, owner,
             core._iso(now + timedelta(seconds=lease_seconds)), recovery, stamp, stamp, row["id"])
        )
        job = core._row_dict(conn.execute("SELECT * FROM citation_assistant_jobs WHERE id=?", (row["id"],)).fetchone())
        return dict(job, claimed_stage=stage)


def interrupt(conn, core, row, reason, message):
    resume = "queued" if row["status"] in {"matching", "queued"} else row["status"]
    # A single slow batch must not consume the same full budget on every resume.
    batch_size = int(row["paragraph_batch_size"])
    if reason == "matching_timeout":
        batch_size = max(1, batch_size // 2)
    conn.execute("UPDATE citation_assistant_jobs SET status='stopped',execution_reason=?,error=?,resumable=1,paragraph_batch_size=?,"
                 "resume_stage=?,execution_generation=execution_generation+1,lease_owner='',lease_expires_at='',"
                 "agent_status=CASE WHEN agent_status='running' THEN 'budget_exhausted' ELSE agent_status END,"
                 "updated_at=? WHERE id=?", (reason, message, batch_size, resume, core._iso(), row["id"]))


def transition(core, job_id, user_id, action, decode_candidate):
    with core._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM citation_assistant_jobs WHERE id=? AND user_id=? AND expires_at>?",
                           (job_id, user_id, core._iso())).fetchone()
        if not row or row["status"] in {"deleted", "expired"}:
            raise core.CitationAssistantError("任务不存在或已过期。")
        status = row["status"]
        if action == "stop":
            if status in {"queued", "matching", "extracting"}:
                interrupt(conn, core, row, "user_stopped", "已停止，可以继续处理。")
            elif status != "stopped":
                raise core.CitationAssistantError("当前阶段无法停止。")
        elif action == "resume":
            if row["resumable"] and status in {"review_ready", "stopped", "complete"}:
                next_status = row["resume_stage"] or "queued"
                conn.execute("UPDATE citation_assistant_jobs SET status=?,recovery_count=0,resumable=0,error='',"
                             "execution_reason='',execution_generation=execution_generation+1,"
                             "lease_owner='',lease_expires_at='',updated_at=? WHERE id=?",
                             (next_status, core._iso(), job_id))
            elif status not in {"queued","matching","extracting"}:
                raise core.CitationAssistantError("没有可继续的工作。")
        elif action == "export":
            if status == "exporting":
                return core._row_dict(row)
            if status != "review_ready" and not (row["partial_results"] and status in {"matching","stopped"}):
                raise core.CitationAssistantError("尚无可审核的核验结果。")
            if row["citation_style"] == "auto" and row["style_confidence"] < 0.8:
                raise core.CitationAssistantError("请先明确选择引文格式。")
            items = conn.execute("SELECT * FROM citation_assistant_candidates WHERE job_id=? ORDER BY id", (job_id,))
            snap = {"items": [decode_candidate(item) for item in items], "style": row["resolved_style"],
                    "citation_style": row["citation_style"]}
            put(core, job_id, "export", snap, conn)
            conn.execute("UPDATE citation_assistant_jobs SET status='exporting',execution_generation=execution_generation+1,"
                         "lease_owner='',lease_expires_at='',resume_stage='queued',resumable=?,"
                         "word_export_status='not_started',pdf_export_status='not_requested',output_docx_path='',"
                         "output_pdf_path='',error='',updated_at=? WHERE id=?",
                         (int(bool(row["resumable"]) or status in {"matching","stopped"}), core._iso(), job_id))
        else:
            raise ValueError("unsupported job action")
        return core._row_dict(conn.execute("SELECT * FROM citation_assistant_jobs WHERE id=?", (job_id,)).fetchone())
