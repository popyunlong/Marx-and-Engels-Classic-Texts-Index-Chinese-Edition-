from __future__ import annotations

"""Run the three SHA-pinned administrator web regressions and enforce release gates."""

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

import requests

try:
    from scripts.citation_agent_two_article_web_regression import _wait_job, submit, summarize
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from citation_agent_two_article_web_regression import _wait_job, submit, summarize


CSRF_RE = re.compile(r'data-csrf="([^"]+)"')
GOLDEN_HASHES = {
    "wang": "d8344ed60549f9060736de8e99825992ff4fc7ae8aecbcf9da158630cdcd8ac2",
    "pan": "1aa9ade78959ac2c1bf865f5390211881f02c3a082dc1cb7cebb357205f6dd92",
    "technology": "3eea11c236703787962e5a9fc340930c98a905787a39833a48a025cbefba9613",
}
MARX_SCOPE = ["book:文集", "book:全集", "book:马恩选集", "book:资本论"]
WANG_SCOPE = [
    "book:毛泽东选集", "book:邓小平文选", "book:江泽民文选", "book:胡锦涛文选",
    "book:治国理政", "book:习近平党建文选",
    "book:习近平新时代中国特色社会主义思想学习纲要", "book:习近平经济思想学习纲要",
    "book:论党的宣传思想工作", "book:论党的自我革命", "book:历次党代会报告",
    "book:历届全会公报", "book:建党以来重要文献选编",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_release_gate(name: str, mode: str, result: dict) -> None:
    job = result["job"]
    actionable = list((result.get("actionable") or {}).get("items") or [])
    if job.get("status") not in {"review_ready", "complete"}:
        raise AssertionError(f"{name}: task failed: {job.get('error') or job.get('status')}")
    if job.get("direct_agent_status") == "budget_exhausted":
        raise AssertionError(f"{name}: direct task exhausted its budget")
    if int(job.get("deferred_record_count") or 0):
        raise AssertionError(f"{name}: eligible records were deferred")
    if any(item.get("source_resolution") == "none" for item in actionable):
        raise AssertionError(f"{name}: actionable result without local source evidence")
    if any(item.get("evidence_level") == "paraphrase" and item.get("auto_selected") for item in actionable):
        raise AssertionError(f"{name}: paraphrase was automatically accepted")
    if mode == "generate" and any(
        item.get("writeback_mode") not in {"footnote", "endnote", "none", "readonly"}
        for item in actionable
    ):
        raise AssertionError(f"{name}: insertion task exposed a proofreading writeback")
    if mode == "audit" and any(
        item.get("writeback_mode") not in {"comment", "none", "readonly"}
        for item in actionable
    ):
        raise AssertionError(f"{name}: proofreading task exposed a note rewrite")
    if name in {"pan", "technology"} and job.get("paraphrase_agent_status") != "complete":
        raise AssertionError(f"{name}: viewpoint lane did not complete")
    if name == "pan" and mode in {"audit", "both"}:
        anchors = {str(item.get("paper_text") or "") for item in actionable}
        for expected in ("盗窃他人的劳动时间", "自由的有意识的活动"):
            if not any(expected in value for value in anchors):
                raise AssertionError(f"pan: missing short quote {expected}")
    if name == "technology" and mode in {"audit", "both"}:
        direct = [item for item in actionable if item.get("text_match_level") in {"exact", "near"}]
        if len(direct) != 2:
            raise AssertionError(f"technology: expected 2 direct citations, got {len(direct)}")
        viewpoints = [item for item in actionable if item.get("text_match_level") == "paraphrase"]
        if len(viewpoints) != 1 or int(viewpoints[0].get("existing_note_id") or 0) != 60:
            raise AssertionError("technology: footnote 60 must be the sole viewpoint suggestion")
        if (
            viewpoints[0].get("source_resolution") != "locator_only"
            or viewpoints[0].get("writeback_mode") != "none"
        ):
            raise AssertionError("technology: viewpoint suggestion must remain locator-only and non-writing")


def _accept_hard_evidence(
    session: requests.Session, base_url: str, csrf: str, result: dict, mode: str,
) -> list[dict]:
    allowed = {"footnote", "endnote"} if mode == "generate" else (
        {"comment"} if mode == "audit" else {"footnote", "endnote", "comment"}
    )
    decisions = [
        {
            "id": int(item["id"]), "decision": "accepted",
            "selected_option": int(item.get("selected_option") or 0),
            "proposed_citation": str(item.get("proposed_citation") or ""),
        }
        for item in list((result.get("actionable") or {}).get("items") or [])
        if item.get("text_match_level") == "exact"
        and item.get("source_resolution") in {"unique", "reference_disambiguated"}
        and item.get("writeback_mode") in allowed
    ]
    if not decisions:
        return []
    response = session.patch(
        f"{base_url}/api/admin/citation-agent-test/jobs/{result['job']['id']}/decisions",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        json={"decisions": decisions}, timeout=30,
    )
    response.raise_for_status()
    return decisions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wang", type=Path)
    parser.add_argument("pan", type=Path)
    parser.add_argument("technology", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--timeout", type=float, default=360)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--require-pdf", action="store_true")
    args = parser.parse_args()
    paths = {"wang": args.wang, "pan": args.pan, "technology": args.technology}
    for name, path in paths.items():
        actual = _sha256(path)
        if actual != GOLDEN_HASHES[name]:
            raise AssertionError(f"{name}: SHA-256 changed: {actual}")

    base_url = args.base_url.rstrip("/")
    session = requests.Session()
    response = session.get(f"{base_url}/admin/citation-agent-test?as=admin", timeout=30)
    response.raise_for_status()
    match = CSRF_RE.search(response.text)
    if not match:
        raise RuntimeError("preview page did not expose a CSRF token")
    csrf = html.unescape(match.group(1))
    cases = [
        ("wang", args.wang, WANG_SCOPE, "direct_only"),
        ("pan", args.pan, MARX_SCOPE, "direct_and_paraphrase"),
        ("technology", args.technology, MARX_SCOPE, "direct_and_paraphrase"),
    ]
    output = {}
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, path, scope, depth in cases:
        for mode in ("generate", "audit", "both"):
            result = submit(session, base_url, csrf, path, scope, depth, args.timeout, mode=mode)
            _assert_release_gate(name, mode, result)
            job_id = str(result["job"]["id"])
            accepted = _accept_hard_evidence(session, base_url, csrf, result, mode)
            response = session.post(
                f"{base_url}/api/admin/citation-agent-test/jobs/{job_id}/export",
                headers={"X-CSRF-Token": csrf}, timeout=30,
            )
            response.raise_for_status()
            result["job"] = _wait_job(session, base_url, job_id, {"complete", "failed"}, args.timeout)
            job = result["job"]
            if job.get("status") != "complete" or not job.get("docx_url"):
                raise AssertionError(f"{name}/{mode}: Word export failed: {job.get('error')}")
            if int(job.get("accepted_count") or 0) != len(accepted):
                raise AssertionError(
                    f"{name}/{mode}: accepted count {job.get('accepted_count')} != {len(accepted)}"
                )
            expected_inserted = sum(
                item.get("writeback_mode") in {"footnote", "endnote"}
                for item in (result.get("actionable") or {}).get("items") or []
                if any(int(decision["id"]) == int(item["id"]) for decision in accepted)
            )
            expected_comments = len(accepted) - expected_inserted
            if int(job.get("inserted_count") or 0) != expected_inserted:
                raise AssertionError(f"{name}/{mode}: inserted count mismatch")
            if int(job.get("commented_count") or 0) != expected_comments:
                raise AssertionError(f"{name}/{mode}: comment count mismatch")
            if mode == "generate":
                if job.get("pdf_url") or job.get("pdf_export_status") != "not_requested":
                    raise AssertionError(f"{name}/generate: insertion task unexpectedly produced PDF")
            elif args.require_pdf and not job.get("pdf_url"):
                raise AssertionError(f"{name}/{mode}: annotated PDF is required: {job.get('error')}")
            if args.output_dir:
                for artifact, url in (("docx", job.get("docx_url")), ("pdf", job.get("pdf_url"))):
                    if not url:
                        continue
                    download = session.get(f"{base_url}{url}", timeout=120)
                    download.raise_for_status()
                    (args.output_dir / f"{name}-{mode}.{artifact}").write_bytes(download.content)
            key = f"{name}:{mode}"
            output[key] = summarize(result)
            output[key]["mode"] = mode
            output[key]["word_export_status"] = job.get("word_export_status")
            output[key]["pdf_export_status"] = job.get("pdf_export_status")
            print(json.dumps({key: output[key]}, ensure_ascii=False), flush=True)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
