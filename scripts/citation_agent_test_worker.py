from __future__ import annotations

"""Private-network deterministic worker for the administrator Agent test lane."""

import argparse
import multiprocessing
from datetime import timedelta
import os
import signal
import socket
import sys
import time
from pathlib import Path

os.environ.setdefault("CITATION_ASSISTANT_INLINE_WORKER", "0")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import citation_agent_queue  # noqa: E402
import citation_job_execution as execution  # noqa: E402
import citation_agent_bridge  # noqa: E402
import citation_agent_test_backend as tasks  # noqa: E402
import citation_agent_test_runtime as runtime  # noqa: E402
from book_data_release import append_ancestors, compatible_corpus  # noqa: E402

CORPUS_INDEX_DB_PATH = runtime.CORPUS_INDEX_DB_PATH
corpus = runtime.load_corpus()
CORPUS_ANALYSIS_VERSION_ERROR = "语料库版本已变更，请重新创建测试任务。"
CORPUS_EXPORT_VERSION_ERROR = "语料库版本已变更，为保证结果可复现，已拒绝导出。"
TEMPLATE_ANALYSIS_VERSION_ERROR = "引文模板已变更，请重新创建测试任务。"
TEMPLATE_EXPORT_VERSION_ERROR = "引文模板已变更，为保证结果可复现，已拒绝导出。"
_STOP_REQUESTED = False


def _request_stop(_signum, _frame) -> None:
    """Finish the claimed test job before systemd replaces this worker."""
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _corpus_file_fingerprint() -> tuple[int, int, int, int] | None:
    try:
        stat = CORPUS_INDEX_DB_PATH.stat()
    except OSError:
        return None
    return int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns)


def _reload_if_runtime_changed(
    loaded_corpus: tuple[int, int, int, int] | None,
    loaded_template_version: str,
) -> None:
    if _corpus_file_fingerprint() != loaded_corpus:
        print("citation Agent test worker detected a corpus replacement; reloading", flush=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])
    if runtime.template_version() != loaded_template_version:
        print("citation Agent test worker detected a template change; reloading", flush=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])


def _analysis(job_id: str, corpus_sha256: str, template_version: str) -> None:
    job = tasks.get_job(job_id)
    if not job:
        return
    try:
        if not compatible_corpus(job.get("corpus_sha256"),corpus_sha256):
            raise tasks.CitationAssistantError(CORPUS_ANALYSIS_VERSION_ERROR)
        if job.get("template_version") != template_version:
            raise tasks.CitationAssistantError(TEMPLATE_ANALYSIS_VERSION_ERROR)
        mode = str(os.environ.get("CITATION_AGENT_TEST_MODE") or "off").strip().lower()
        agent_callback = citation_agent_bridge.make_callback(corpus) if mode in {"admin_live", "live"} else None
        options={}
        ancestor=append_ancestors(corpus_sha256).get(job.get('corpus_sha256'))
        scope=list(job.get('scope') or [])
        if ancestor and not scope:
            options['scope_override']=scope+['book:'+b for b in ancestor['books']]
        tasks.run_analysis(
            job_id, corpus, personal_callback=runtime.personal_callback(job),
            agent_callback=agent_callback,**options,
        )
    except Exception as exc:
        tasks.update_job(job_id, status="failed", agent_status="degraded", error=str(exc)[:500])


def _export(job_id: str, corpus_sha256: str, template_version: str) -> None:
    job = tasks.get_job(job_id)
    if not job:
        return
    try:
        if not compatible_corpus(job.get("corpus_sha256"),corpus_sha256):
            raise tasks.CitationAssistantError(CORPUS_EXPORT_VERSION_ERROR)
        if job.get("template_version") != template_version:
            raise tasks.CitationAssistantError(TEMPLATE_EXPORT_VERSION_ERROR)
        tasks.run_export(job_id)
    except Exception as exc:
        tasks.update_job(job_id, status="failed", error=str(exc)[:500])


def _execute_stage(job, corpus_sha256, template_version):
    # In Linux production fork shares the loaded read-only corpus. Isolate native
    # calls and LibreOffice in this process group so the supervisor can bound them.
    if os.name == "posix":
        os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    tasks.init_db()
    with execution.worker_claim(job):
        stage = job["claimed_stage"]
        if stage == "extracting":
            tasks.run_extraction(job["id"])
        elif stage in {"queued","matching"}:
            _analysis(job["id"], corpus_sha256, template_version)
        else:
            _export(job["id"], corpus_sha256, template_version)


def _terminate_stage(process):
    if process.is_alive():
        try:
            if os.name == "posix" and os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    process.join()


def _supervise(job, corpus_sha256, template_version):
    ctx = multiprocessing.get_context("fork" if os.name == "posix" else "spawn")
    child = ctx.Process(target=_execute_stage, args=(job, corpus_sha256, template_version))
    child.start()
    stage = job["claimed_stage"]
    phase = {"queued":"matching"}.get(stage,stage)
    limit = {"extracting":60,"matching":180,"agent":180,"exporting":360}
    started = time.monotonic()
    last_renewed = started - 10
    try:
        while child.is_alive():
            now = time.monotonic()
            with tasks.core._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM citation_assistant_jobs WHERE id=?",(job["id"],)).fetchone()
                if (not row or row["lease_owner"] != job["lease_owner"] or
                    row["execution_generation"] != job["execution_generation"] or
                    row["expires_at"] <= tasks.core._iso() or row["status"] not in {"extracting","matching","exporting"}):
                    break
                if stage in {"queued","matching"} and phase == "matching" and row["analysis_stage"] in {
                    "agent_planning","local_verification","agent_verification","finalizing"
                }:
                    phase = "agent"
                    started = now
                if now - started >= limit[phase]:
                    if phase == "exporting" and row["word_export_status"] == "ready":
                        conn.execute("""UPDATE citation_assistant_jobs SET status='complete',lease_owner='',
                            lease_expires_at='',pdf_export_status='failed',output_pdf_path='',
                            error='Word 已完成，PDF 生成超时，可以单独重试。',updated_at=? WHERE id=?""",
                            (tasks.core._iso(),job["id"]))
                    else:
                        execution.interrupt(conn,tasks.core,row,phase+"_timeout","本阶段已超时，已保留完成的结果。")
                    break
                if now - last_renewed >= 10:
                    stamp = tasks.core._utcnow()
                    conn.execute("UPDATE citation_assistant_jobs SET heartbeat_at=?,lease_expires_at=? WHERE id=?",
                                 (tasks.core._iso(stamp),tasks.core._iso(stamp+timedelta(seconds=60)),job["id"]))
                    citation_agent_queue.write_worker_ready()
                    last_renewed = now
            child.join(timeout=1)
    finally:
        try:
            _terminate_stage(child)
        finally:
            child.close()
    # A crashed child leaves its stage and checkpoint intact. All subsequent
    # claims (including crashes of this supervisor) use the same recovery limit.
    with tasks.core._connect() as conn:
        conn.execute("UPDATE citation_assistant_jobs SET lease_expires_at=? "
                     "WHERE id=? AND lease_owner=? AND execution_generation=?",
                     (tasks.core._iso(),job["id"],job["lease_owner"],job["execution_generation"]))


def _retire_incompatible(corpus_versions, template):
    versions = list(corpus_versions)
    placeholders = ",".join("?" for _ in versions)
    with tasks.core._connect() as conn:
        conn.execute(f"""UPDATE citation_assistant_jobs SET status='failed',
            error=CASE WHEN template_version!=? THEN ? ELSE ? END,execution_reason='incompatible_version',
            lease_owner='',lease_expires_at='',resumable=0,updated_at=?
            WHERE status IN ('extracting','queued','matching','exporting','stopped') AND expires_at>?
            AND (lease_owner='' OR lease_expires_at<=?)
            AND (template_version!=? OR corpus_sha256 NOT IN ({placeholders}))""",
            (template,TEMPLATE_ANALYSIS_VERSION_ERROR,CORPUS_ANALYSIS_VERSION_ERROR,
             tasks.core._iso(), tasks.core._iso(), tasks.core._iso(),template,*versions))


def run(*, once: bool = False, poll_seconds: float = 2.0) -> int:
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    worker_id = f"citation-agent-test:{socket.gethostname()}:{os.getpid()}"
    processed = 0
    loaded_fingerprint = _corpus_file_fingerprint()
    tasks.init_db()
    loaded_corpus_sha256 = runtime.corpus_sha256()
    loaded_template_version = runtime.template_version()
    recovered = tasks.recover_jobs_for_loaded_runtime(
        loaded_corpus_sha256,
        loaded_template_version,
        analysis_errors=(CORPUS_ANALYSIS_VERSION_ERROR, TEMPLATE_ANALYSIS_VERSION_ERROR),
        export_errors=(CORPUS_EXPORT_VERSION_ERROR, TEMPLATE_EXPORT_VERSION_ERROR),
    )
    recovered_pdf = tasks.recover_pdf_position_failures(
        loaded_corpus_sha256, loaded_template_version,
    )
    if recovered["analysis"] or recovered["export"]:
        print(
            "citation Agent test worker recovered version-mismatch jobs "
            f"analysis={recovered['analysis']} export={recovered['export']}",
            flush=True,
        )
    if recovered_pdf:
        print(
            f"citation Agent test worker queued PDF-only recovery jobs={recovered_pdf}",
            flush=True,
        )
    last_ready = 0.0
    last_maintenance = 0.0
    while True:
        if _STOP_REQUESTED:
            return processed
        _reload_if_runtime_changed(loaded_fingerprint, loaded_template_version)
        now = time.monotonic()
        ancestors = append_ancestors(loaded_corpus_sha256)
        if now - last_ready >= 10 or not last_ready:
            citation_agent_queue.write_worker_ready()
            last_ready = now
        if now - last_maintenance >= 3600 or not last_maintenance:
            _retire_incompatible([loaded_corpus_sha256,*ancestors],loaded_template_version)
            last_maintenance = now
        job = tasks.claim_next_job(
            worker_id, lease_seconds=60, ancestors=ancestors,
            corpus_sha256=loaded_corpus_sha256,
            template_version=loaded_template_version,
        )
        if job is None:
            if once:
                return processed
            time.sleep(max(0.5, poll_seconds))
            continue
        _supervise(job, loaded_corpus_sha256, loaded_template_version)
        processed += 1
        if once or _STOP_REQUESTED:
            return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="管理员论文校注 Agent 测试工作节点")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    raise SystemExit(run(once=bool(args.once), poll_seconds=float(args.poll_seconds)))


if __name__ == "__main__":
    main()
