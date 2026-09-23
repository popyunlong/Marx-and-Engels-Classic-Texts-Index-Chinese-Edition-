from __future__ import annotations

"""Single-concurrency worker for DOCX extraction, corpus matching, and export.

The application server only writes jobs to the shared application-data volume.  Run this
process on the storage/parsing node with the same read-only corpus and APPDATA_DIR mount.
Claims are atomic and leased, so a killed worker can safely be replaced.
"""

import argparse
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

import citation_assistant as tasks  # noqa: E402
import search_exports as search_export_tasks  # noqa: E402
from app import (  # noqa: E402
    CORPUS_INDEX_DB_PATH,
    _citation_analysis_worker,
    _citation_corpus_sha256,
    _citation_export_worker,
    _citation_template_version,
    _search_export_worker,
    _search_export_template_version,
)


_STOP_REQUESTED = False


def _request_stop(_signum, _frame) -> None:
    """Finish the claimed job before systemd replaces this worker."""
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _corpus_file_fingerprint() -> tuple[int, int, int, int] | None:
    """Identity of the on-disk corpus loaded when this process imported app."""
    try:
        stat = CORPUS_INDEX_DB_PATH.stat()
    except OSError:
        return None
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _reload_if_corpus_changed(loaded_fingerprint: tuple[int, int, int, int] | None) -> None:
    current = _corpus_file_fingerprint()
    if current == loaded_fingerprint:
        return
    print("citation worker detected a corpus replacement; reloading process", flush=True)
    os.execv(sys.executable, [sys.executable, *sys.argv])


def run(*, once: bool = False, poll_seconds: float = 2.0) -> int:
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    processed = 0
    next_cleanup_at = 0.0
    loaded_fingerprint = _corpus_file_fingerprint()
    loaded_corpus_sha256 = _citation_corpus_sha256()
    loaded_template_version = _citation_template_version()
    loaded_export_template_version = _search_export_template_version()
    recovered = tasks.recover_jobs_for_loaded_corpus(
        loaded_corpus_sha256, loaded_template_version,
    )
    recovered_pdf = tasks.recover_pdf_position_failures(
        loaded_corpus_sha256, loaded_template_version,
    )
    if recovered["analysis"] or recovered["export"]:
        print(
            "citation worker recovered version-mismatch jobs "
            f"analysis={recovered['analysis']} export={recovered['export']}",
            flush=True,
        )
    if recovered_pdf:
        print(
            f"citation worker queued PDF-only recovery jobs={recovered_pdf}",
            flush=True,
        )
    while True:
        if _STOP_REQUESTED:
            return processed
        _reload_if_corpus_changed(loaded_fingerprint)
        try:
            search_export_tasks.record_worker_heartbeat(worker_id)
            if time.monotonic() >= next_cleanup_at:
                search_export_tasks.cleanup_expired()
                next_cleanup_at = time.monotonic() + 3600
        except Exception as exc:
            # The new export lane must never prevent the established citation
            # assistant queue from making progress.
            print(f"search export maintenance unavailable: {exc}", flush=True)

        citation_job = None
        export_job = None
        citation_claim_ok = True
        # The established citation-assistant lane always has priority.  Search
        # exports may use this process only while that lane is idle.
        try:
            citation_job = tasks.claim_next_job(
                worker_id, lease_seconds=900,
                corpus_sha256=loaded_corpus_sha256,
                template_version=loaded_template_version,
            )
        except Exception as exc:
            citation_claim_ok = False
            print(f"citation assistant claim unavailable: {exc}", flush=True)
        if citation_claim_ok and citation_job is None and search_export_tasks.resources_allow_start():
            try:
                export_job = search_export_tasks.claim_next_job(
                    worker_id,
                    corpus_version=loaded_corpus_sha256,
                    template_version=loaded_export_template_version,
                )
            except Exception as exc:
                print(f"search export claim unavailable: {exc}", flush=True)

        job = export_job or citation_job
        if job is None:
            if once:
                return processed
            time.sleep(max(0.5, poll_seconds))
            continue
        job_id = str(job["id"])
        try:
            search_export_tasks.record_worker_heartbeat(worker_id, job_id)
        except Exception:
            pass
        if export_job is not None:
            _search_export_worker(job_id, worker_id)
        else:
            stage = str(job.get("claimed_stage") or job.get("status") or "")
            if stage == "extracting":
                tasks.run_extraction(job_id)
            elif stage in {"queued", "matching"}:
                _citation_analysis_worker(job_id)
            elif stage == "exporting":
                _citation_export_worker(job_id)
        processed += 1
        if once or _STOP_REQUESTED:
            return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="论文引文助手工作节点")
    parser.add_argument("--once", action="store_true", help="处理一个任务后退出")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    run(once=bool(args.once), poll_seconds=float(args.poll_seconds))


if __name__ == "__main__":
    main()
