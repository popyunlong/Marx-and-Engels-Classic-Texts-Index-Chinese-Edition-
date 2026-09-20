from __future__ import annotations

"""Submit two real DOCX files through the isolated administrator web API.

The script intentionally talks only to the localhost preview server.  It is a
repeatable end-to-end check of upload, extraction, scope freeze, deterministic
matching, the queue-backed Agent, and result partitioning.
"""

import argparse
import html
import json
import re
import time
from pathlib import Path

import requests


CSRF_RE = re.compile(r'data-csrf="([^"]+)"')
TERMINAL = {"review_ready", "complete", "failed"}


def _wait_job(
    session: requests.Session,
    base_url: str,
    job_id: str,
    wanted: set[str],
    timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = session.get(
            f"{base_url}/api/admin/citation-agent-test/jobs/{job_id}", timeout=15,
        )
        response.raise_for_status()
        job = response.json()["job"]
        if str(job.get("status") or "") in wanted:
            return job
        time.sleep(1)
    raise TimeoutError(f"job {job_id} did not reach {sorted(wanted)}")


def _candidate_page(
    session: requests.Session, base_url: str, job_id: str, bucket: str,
) -> dict:
    response = session.get(
        f"{base_url}/api/admin/citation-agent-test/jobs/{job_id}/candidates",
        params={"bucket": bucket, "page_size": 200}, timeout=30,
    )
    response.raise_for_status()
    return response.json()


def submit(
    session: requests.Session,
    base_url: str,
    csrf: str,
    path: Path,
    scope: list[str],
    recognition_depth: str,
    timeout: float,
    mode: str = "both",
) -> dict:
    with path.open("rb") as stream:
        response = session.post(
            f"{base_url}/api/admin/citation-agent-test/jobs",
            headers={"X-CSRF-Token": csrf},
            data={
                "mode": mode,
                "note_kind": "footnote",
                "citation_style": "mkszyj",
                "recognition_depth": recognition_depth,
                "scope": json.dumps(scope, ensure_ascii=False),
            },
            files={
                "file": (
                    path.name, stream,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            },
            timeout=60,
        )
    response.raise_for_status()
    job_id = str(response.json()["job"]["id"])
    extracted = _wait_job(session, base_url, job_id, {"awaiting_sections", "failed"}, timeout)
    if extracted["status"] == "failed":
        return {"job": extracted, "actionable": {}, "readonly": {}}
    selected = [
        str(section["id"]) for section in (extracted.get("sections") or [])
        if section.get("default_selected")
    ]
    response = session.post(
        f"{base_url}/api/admin/citation-agent-test/jobs/{job_id}/analyze",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        json={"sections": selected, "scope": scope}, timeout=30,
    )
    response.raise_for_status()
    finished = _wait_job(session, base_url, job_id, TERMINAL, timeout)
    return {
        "job": finished,
        "actionable": _candidate_page(session, base_url, job_id, "actionable"),
        "readonly": _candidate_page(session, base_url, job_id, "unresolved"),
    }


def summarize(result: dict) -> dict:
    job = result["job"]
    actionable = result.get("actionable") or {}
    readonly = result.get("readonly") or {}
    actionable_items = actionable.get("items") or []
    readonly_items = readonly.get("items") or []
    return {
        "job_id": job.get("id"),
        "page_url": f"/admin/citation-agent-test/jobs/{job.get('id')}?as=admin",
        "status": job.get("status"),
        "agent_status": job.get("agent_status"),
        "direct_agent_status": job.get("direct_agent_status"),
        "paraphrase_agent_status": job.get("paraphrase_agent_status"),
        "eligible_record_count": job.get("eligible_record_count"),
        "processed_record_count": job.get("processed_record_count"),
        "deferred_record_count": job.get("deferred_record_count"),
        "completion_reason": job.get("completion_reason"),
        "agent_verified_count": job.get("agent_verified_count"),
        "candidate_count": job.get("candidate_count"),
        "accepted_count": job.get("accepted_count"),
        "unresolved_count": job.get("unresolved_count"),
        "out_of_scope_count": job.get("out_of_scope_count"),
        "skipped_no_evidence_count": job.get("skipped_no_evidence_count"),
        "actionable_exact": sum(item.get("evidence_level") == "exact" for item in actionable_items),
        "readonly_exact": sum(item.get("evidence_level") == "exact" for item in readonly_items),
        "readonly_structure": sum(
            item.get("verification_scope") == "readonly_structure" for item in readonly_items
        ),
        "readonly_quote_samples": [
            str(item.get("paper_text") or "")[:100]
            for item in readonly_items
            if item.get("verification_scope") == "readonly_structure"
        ][:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wang", type=Path)
    parser.add_argument("pan", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    session = requests.Session()
    response = session.get(f"{base_url}/admin/citation-agent-test?as=admin", timeout=30)
    response.raise_for_status()
    match = CSRF_RE.search(response.text)
    if not match:
        raise RuntimeError("preview page did not expose a CSRF token")
    csrf = html.unescape(match.group(1))

    cases = [
        (
            "wang", args.wang,
            [
                "book:毛泽东选集", "book:邓小平文选", "book:江泽民文选",
                "book:胡锦涛文选", "book:治国理政", "book:习近平党建文选",
                "book:习近平新时代中国特色社会主义思想学习纲要",
                "book:习近平经济思想学习纲要", "book:论党的宣传思想工作",
                "book:论党的自我革命", "book:历次党代会报告", "book:历届全会公报",
                "book:建党以来重要文献选编",
            ],
        ),
        (
            "pan", args.pan,
            ["book:文集", "book:全集", "book:马恩选集", "book:资本论"],
        ),
    ]
    output = {}
    for name, path, scope in cases:
        output[name] = summarize(
            submit(session, base_url, csrf, path, scope, "direct_only", args.timeout)
        )
        print(json.dumps({name: output[name]}, ensure_ascii=False), flush=True)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
