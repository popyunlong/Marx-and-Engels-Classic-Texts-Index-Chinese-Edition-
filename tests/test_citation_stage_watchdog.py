"""Exercise the worker supervisor with an unresponsive child and a deterministic clock."""
import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

import citation_agent_test_backend as tasks
import citation_agent_test_runtime as runtime
import citation_job_execution as execution
from test_citation_job_execution import queued_job


@pytest.mark.parametrize(("stage", "budget"), [("matching", 180), ("exporting", 360)])
def test_watchdog_renews_then_terminates_blocked_stage(queued_job, monkeypatch, tmp_path, stage, budget):
    monkeypatch.setattr(runtime, "load_corpus", lambda: None)
    worker = importlib.import_module("scripts.citation_agent_test_worker")
    initial_time = tasks.core._utcnow().replace(microsecond=0)
    elapsed = 0
    heartbeats = []
    leases = []
    output = tmp_path / "output.docx"
    if stage == "matching":
        tasks.update_job(queued_job, partial_results=1, progress_done=24, progress_total=48)
        execution.put(tasks.core, queued_job, "paragraphs-0", {"end":24})
    else:
        output.write_bytes(b"published word artifact")
        tasks.update_job(queued_job, status="exporting", word_export_status="ready",
                         pdf_export_status="converting", output_docx_path=str(output))
    job = tasks.claim_next_job("watchdog", corpus_sha256="index", template_version="style")

    class HungProcess:
        running = False
        closed = False
        killed = False

        def start(self):
            self.running = True

        def is_alive(self):
            return self.running

        def join(self, timeout=None):
            nonlocal elapsed
            if timeout is not None:
                # Capture live DB state *after* the supervisor's renewal transaction.
                row = tasks.get_job(queued_job)
                leases.append((elapsed, row["heartbeat_at"], row["lease_expires_at"]))
                elapsed += 10
                assert elapsed <= budget, "Watchdog failed to enforce the stage budget"
            else:
                assert self.killed

        def kill(self):
            self.killed = True
            self.running = False

        def close(self):
            self.closed = True

    child = HungProcess()
    process_context = SimpleNamespace(Process=lambda **_kwargs: child)
    monkeypatch.setattr(worker, "multiprocessing", SimpleNamespace(get_context=lambda _: process_context))
    monkeypatch.setattr(worker, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(tasks.core, "_utcnow", lambda: initial_time + timedelta(seconds=elapsed))
    monkeypatch.setattr(worker.citation_agent_queue, "write_worker_ready", lambda: heartbeats.append(elapsed))

    worker._supervise(job, "index", "style")

    assert child.closed and child.killed and elapsed == budget
    assert heartbeats == list(range(0, budget, 10))
    assert len(leases) == len(heartbeats)
    for offset, beat, until in leases:
        assert beat == tasks.core._iso(initial_time + timedelta(seconds=offset))
        assert until == tasks.core._iso(initial_time + timedelta(seconds=offset+60))
    row = tasks.get_job(queued_job)
    assert not row["lease_owner"] and not row["lease_expires_at"]
    if stage == "matching":
        assert row["status"] == "stopped" and row["resumable"]
        assert row["execution_reason"] == "matching_timeout" and row["partial_results"]
        assert row["execution_generation"] > job["execution_generation"]
        assert row["progress_done"] == 24 and row["progress_total"] == 48
        assert execution.read(tasks.core, queued_job, "paragraphs-0") == {"end":24}
    else:
        assert row["status"] == "complete"
        assert row["word_export_status"] == "ready" and row["output_docx_path"] == str(output)
        assert row["pdf_export_status"] == "failed" and row["output_pdf_path"] == ""
        assert output.read_bytes() == b"published word artifact"
