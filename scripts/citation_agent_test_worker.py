from __future__ import annotations

"""Private-network deterministic worker for the administrator Agent test lane."""

import argparse
import os
import socket
import sys
import time
from pathlib import Path

os.environ.setdefault("CITATION_ASSISTANT_INLINE_WORKER", "0")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import citation_agent_bridge  # noqa: E402
import citation_agent_test_backend as tasks  # noqa: E402
import citation_agent_test_runtime as runtime  # noqa: E402

CORPUS_INDEX_DB_PATH = runtime.CORPUS_INDEX_DB_PATH
corpus = runtime.load_corpus()


def _corpus_file_fingerprint() -> tuple[int, int, int, int] | None:
    try:
        stat = CORPUS_INDEX_DB_PATH.stat()
    except OSError:
        return None
    return int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns)


def _reload_if_corpus_changed(loaded: tuple[int, int, int, int] | None) -> None:
    if _corpus_file_fingerprint() == loaded:
        return
    print("citation Agent test worker detected a corpus replacement; reloading", flush=True)
    os.execv(sys.executable, [sys.executable, *sys.argv])


def _analysis(job_id: str) -> None:
    job = tasks.get_job(job_id)
    if not job:
        return
    try:
        if job.get("corpus_sha256") != runtime.corpus_sha256():
            raise tasks.CitationAssistantError("语料库版本已变更，请重新创建测试任务。")
        if job.get("template_version") != runtime.template_version():
            raise tasks.CitationAssistantError("引文模板已变更，请重新创建测试任务。")
        mode = str(os.environ.get("CITATION_AGENT_TEST_MODE") or "off").strip().lower()
        # This deployment is intentionally incapable of a member rollout.  A future
        # approval release must add that lane explicitly rather than flipping a typo.
        agent_callback = citation_agent_bridge.make_callback(corpus) if mode == "admin_live" else None
        tasks.run_analysis(
            job_id, corpus, personal_callback=runtime.personal_callback(job),
            agent_callback=agent_callback,
        )
    except Exception as exc:
        tasks.update_job(job_id, status="failed", agent_status="degraded", error=str(exc)[:500])


def _export(job_id: str) -> None:
    job = tasks.get_job(job_id)
    if not job:
        return
    try:
        if job.get("corpus_sha256") != runtime.corpus_sha256():
            raise tasks.CitationAssistantError("语料库版本已变更，为保证结果可复现，已拒绝导出。")
        if job.get("template_version") != runtime.template_version():
            raise tasks.CitationAssistantError("引文模板已变更，为保证结果可复现，已拒绝导出。")
        tasks.run_export(job_id)
    except Exception as exc:
        tasks.update_job(job_id, status="failed", error=str(exc)[:500])


def run(*, once: bool = False, poll_seconds: float = 2.0) -> int:
    worker_id = f"citation-agent-test:{socket.gethostname()}:{os.getpid()}"
    processed = 0
    loaded_fingerprint = _corpus_file_fingerprint()
    tasks.init_db()
    while True:
        _reload_if_corpus_changed(loaded_fingerprint)
        job = tasks.claim_next_job(worker_id, lease_seconds=900)
        if job is None:
            if once:
                return processed
            time.sleep(max(0.5, poll_seconds))
            continue
        job_id = str(job["id"])
        stage = str(job.get("claimed_stage") or job.get("status") or "")
        if stage == "extracting":
            tasks.run_extraction(job_id)
        elif stage in {"queued", "matching"}:
            _analysis(job_id)
        elif stage == "exporting":
            _export(job_id)
        processed += 1
        if once:
            return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="管理员论文校注 Agent 测试工作节点")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    raise SystemExit(run(once=bool(args.once), poll_seconds=float(args.poll_seconds)))


if __name__ == "__main__":
    main()
