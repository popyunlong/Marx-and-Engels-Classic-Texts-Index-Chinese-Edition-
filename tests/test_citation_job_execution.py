import sqlite3
from datetime import timedelta

import pytest

import citation_agent_test_backend as tasks
import citation_job_execution as execution


@pytest.fixture()
def queued_job(tmp_path, monkeypatch):
    core = tasks.core
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(core, "ARTIFACT_ROOT", tmp_path / "papers")
    tasks.init_db()
    now = core._utcnow()
    job_id = "a" * 32
    with core._connect() as conn:
        conn.execute("""INSERT INTO citation_assistant_jobs
            (id,user_id,status,corpus_sha256,template_version,created_at,updated_at,expires_at,citation_style)
            VALUES (?,7,'queued','index','style',?,?,?,'mkszyj')""",
            (job_id, core._iso(now), core._iso(now), core._iso(now + timedelta(days=1))))
    core._job_dir(7, job_id, create=True)
    return job_id


def claim(owner):
    return tasks.claim_next_job(owner, corpus_sha256="index", template_version="style")


def test_stop_resume_fences_old_writes_and_artifacts(queued_job):
    job = claim("first")
    with execution.worker_claim(job):
        execution.put(tasks.core, queued_job, "paragraphs-0", {"end": 24})
        directory = tasks.core._job_dir(7, queued_job, create=True)
    tasks.stop_job(queued_job, 7)
    tasks.resume_job(queued_job, 7)
    resumed = claim("second")
    with execution.worker_claim(job), pytest.raises(sqlite3.IntegrityError, match="lease lost"):
        execution.put(tasks.core, queued_job, "paragraphs-0", {"end": 0})
    with execution.worker_claim(resumed):
        assert execution.read(tasks.core, queued_job, "paragraphs-0") == {"end":24}
        assert directory != tasks.core._job_dir(7, queued_job, create=True)
        tasks.update_job(queued_job, status="review_ready")
    assert tasks.get_job(queued_job)["lease_owner"] == ""


def test_only_two_crash_recoveries_are_automatic(queued_job):
    initial = claim("first")
    for expected in (1, 2, 3):
        with tasks.core._connect() as conn:
            conn.execute("UPDATE citation_assistant_jobs SET lease_expires_at='' WHERE id=?", (queued_job,))
        recovered = claim("replacement")
        if expected <= 2:
            assert recovered["recovery_count"] == expected
            assert recovered["execution_generation"] > initial["execution_generation"]
        else:
            assert recovered is None
    row = tasks.get_job(queued_job)
    assert row["status"] == "stopped" and row["resumable"]
    assert row["execution_reason"] == "recovery_limit"


def test_partial_export_is_a_frozen_snapshot(queued_job):
    worker = claim("worker")
    with execution.worker_claim(worker):
        tasks.update_job(queued_job, partial_results=1, analysis_stage="agent_planning")
        with tasks.core._connect() as conn:
            conn.execute("""INSERT INTO citation_assistant_candidates(job_id,kind,paragraph_index,raw_start,raw_end,
                decision,created_at) VALUES (?,'generate',0,1,4,'accepted',?)""", (queued_job,tasks.core._iso()))
    tasks.queue_export_snapshot(queued_job, 7)
    assert claim("exporter")["resumable"]
    with tasks.core._connect() as conn:
        conn.execute("UPDATE citation_assistant_candidates SET decision='rejected' WHERE job_id=?", (queued_job,))
    _, candidates = tasks._exportable_candidates(queued_job)
    assert [item["decision"] for item in candidates] == ["accepted"]
    assert tasks.get_job(queued_job)["resumable"]
    with execution.worker_claim(worker), pytest.raises(sqlite3.IntegrityError, match="lease lost"):
        tasks.update_job(queued_job, status="review_ready")




def test_progressive_reviews_bind_source_revision_and_expiry(queued_job):
    claim("matcher")
    tasks.update_job(queued_job, partial_results=1)
    def add_option(label):
        item = {"kind":"audit", "paragraph_index":1, "raw_start":0, "raw_end":4, "paper_text":"论文文字",
                "review_bucket":"actionable", "text_match_level":"near", "source_options":[{"citation":label}],
                "issue_code":"text_mismatch","writeback_mode":"comment"}
        tasks.replace_candidates(queued_job, [item])
        return tasks.all_candidates(queued_job)[0]

    first = add_option("版本甲")
    current = add_option("版本乙")
    assert first["id"] == current["id"] and first["review_revision"] != current["review_revision"]
    payload = {"id":first["id"],"decision":"accepted","review_revision":first["review_revision"]}
    with pytest.raises(tasks.CandidateRevisionConflict):
        tasks.save_decisions(queued_job, 7, [payload], web_review=True)
    with pytest.raises(tasks.CandidateRevisionConflict):
        tasks.bulk_decide_pending(queued_job, 7, "accepted", web_review=True)
    assert tasks.save_decisions(queued_job, 7, [{**payload,"review_revision":current["review_revision"]}],
                                web_review=True) == 1

    with tasks.core._connect() as conn:
        conn.execute("UPDATE citation_assistant_jobs SET expires_at=? WHERE id=?",
                     ("2000-01-01T00:00:00+00:00",queued_job))
    assert tasks.visible_job(queued_job,7) is None
    assert tasks.list_jobs(7) == []
    with pytest.raises(tasks.CitationAssistantError, match="过期"):
        tasks.queue_pdf_retry(queued_job,7)
