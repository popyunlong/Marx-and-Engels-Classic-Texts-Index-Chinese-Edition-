from __future__ import annotations

"""Isolated production-stack acceptance suite for the citation assistant.

The suite loads the deployed application and production corpus, but redirects the
citation database and every generated artifact to a caller-provided temporary
directory.  Synthetic identities are injected in-process; no production user,
membership, job, or uploaded paper is read or mutated.
"""

import argparse
import copy
import json
import os
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import fitz
from lxml import etree

PROJECT_ROOT = Path(
    os.environ.get("CITATION_TEST_PROJECT_ROOT") or Path(__file__).resolve().parents[1]
).resolve()
sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != PROJECT_ROOT]
sys.path.insert(0, str(PROJECT_ROOT))

import citation_assistant as tasks


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _rewrite_settings(payload: bytes, enforcement: str) -> bytes:
    source, output = BytesIO(payload), BytesIO()
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(output, "w") as zout:
        for name in zin.namelist():
            content = zin.read(name)
            if name == "word/settings.xml":
                settings = tasks._parse_xml(content)
                for existing in settings.xpath(".//w:documentProtection", namespaces=tasks.NS):
                    existing.getparent().remove(existing)
                protection = etree.Element(f"{{{tasks.W_NS}}}documentProtection")
                protection.set(f"{{{tasks.W_NS}}}edit", "readOnly")
                protection.set(f"{{{tasks.W_NS}}}enforcement", enforcement)
                settings.insert(0, protection)
                content = tasks._xml_bytes(settings)
            zout.writestr(name, content)
    return output.getvalue()


def _partial_quote_candidates(candidates: list[dict], extracted: dict) -> list[dict]:
    paragraphs = list(extracted.get("paragraphs") or [])
    partial: list[dict] = []
    for candidate in candidates:
        if candidate.get("kind") != "generate":
            continue
        pindex = int(candidate.get("paragraph_index") or 0)
        text = str(paragraphs[pindex].get("text") or "")
        start, end = int(candidate.get("raw_start") or 0), int(candidate.get("raw_end") or 0)
        for match in tasks._QUOTE_RE.finditer(text):
            quote_start, quote_end = match.span()
            if start < quote_end and end > quote_start and not (start <= quote_start and end >= quote_end):
                partial.append({
                    "paragraph_index": pindex, "start": start, "end": end,
                    "quote_start": quote_start, "quote_end": quote_end,
                })
    return partial


def _login(client, user_id: int, csrf: str = "isolated-csrf-token") -> None:
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["_csrf_token"] = csrf


def run(fixture: Path, true_endnote_fixture: Path, work_dir: Path) -> dict:
    work_dir = work_dir.resolve()
    _assert(work_dir.name.startswith("citation-isolated-"), "refusing a non-isolated work directory")
    work_dir.mkdir(parents=True, exist_ok=False)
    tasks.DB_PATH = work_dir / "citation.sqlite3"
    tasks.ARTIFACT_ROOT = work_dir / "artifacts"
    tasks.init_db()

    # Import only after redirecting the shared citation module.  app.py calls
    # citation_tasks.init_db() at import time, which therefore remains isolated.
    import runtime_env
    runtime_env.APPDATA_DIR = work_dir / "appdata"
    runtime_env.ACTIVATION_FILE = runtime_env.APPDATA_DIR / "activation.json"
    import app as web

    web.CITATION_INLINE_WORKER = False
    web._load_access_policy = lambda: {"global": {"citation_assistant": False}}
    web._feature_is_available = lambda feature: True
    web._capture_current_user_ip = lambda user: None
    web._sweep_expired_orders_if_due = lambda: None
    synthetic_users = {
        990001: {"id": 990001, "email": "isolated-admin-1@example.invalid", "role": "admin", "is_active": 1},
        990002: {"id": 990002, "email": "isolated-admin-2@example.invalid", "role": "admin", "is_active": 1},
        990003: {"id": 990003, "email": "isolated-member@example.invalid", "role": "member", "is_active": 1},
    }
    web.get_user_by_id = lambda user_id: copy.deepcopy(synthetic_users.get(int(user_id)))

    results: dict[str, object] = {}
    client = web.app.test_client()

    anonymous = client.get("/citation-assistant")
    results["anonymous_status"] = anonymous.status_code
    _assert(anonymous.status_code == 404, "unreleased assistant is visible anonymously")

    _login(client, 990003)
    member = client.get("/citation-assistant")
    results["member_status"] = member.status_code
    _assert(member.status_code == 404, "unreleased assistant is visible to a member")

    _login(client, 990001)
    admin = client.get("/citation-assistant")
    results["admin_status"] = admin.status_code
    _assert(admin.status_code == 200, "synthetic admin cannot open citation assistant")
    _assert("private, no-store" in str(admin.headers.get("Cache-Control") or ""), "private cache header missing")
    _assert("noindex" in str(admin.headers.get("X-Robots-Tag") or ""), "robots exclusion missing")

    missing_csrf = client.post("/api/citation-assistant/jobs", data={})
    results["missing_csrf_status"] = missing_csrf.status_code
    _assert(missing_csrf.status_code == 403, "citation write endpoint accepted a request without CSRF")

    upload_data = {
        "file": (BytesIO(fixture.read_bytes()), fixture.name),
        "mode": "both", "note_kind": "footnote", "threshold": "conservative",
        "citation_style": "gb2015", "scope": json.dumps(["vol:文集:1"], ensure_ascii=False),
    }
    created_response = client.post(
        "/api/citation-assistant/jobs", data=upload_data,
        headers={"X-CSRF-Token": "isolated-csrf-token"}, content_type="multipart/form-data",
    )
    _assert(created_response.status_code == 202, f"isolated upload failed: {created_response.get_data(as_text=True)[:300]}")
    job_id = str(created_response.get_json()["job"]["id"])
    tasks.run_extraction(job_id)
    job = tasks.get_job(job_id, 990001)
    _assert(bool(job) and job["status"] == "awaiting_sections", f"isolated extraction failed: {(job or {}).get('error')}")
    selected = [str(section["id"]) for section in job["sections"] if section.get("default_selected")]

    analyze_response = client.post(
        f"/api/citation-assistant/jobs/{job_id}/analyze",
        json={"sections": selected, "scope": ["vol:文集:1"]},
        headers={"X-CSRF-Token": "isolated-csrf-token"},
    )
    _assert(analyze_response.status_code == 202, "analysis API did not queue the isolated job")
    tasks.run_analysis(job_id, web.corpus)
    analyzed = tasks.get_job(job_id, 990001)
    _assert(bool(analyzed) and analyzed["status"] == "review_ready", f"isolated analysis failed: {(analyzed or {}).get('error')}")
    extracted = json.loads(Path(str(analyzed["extraction_path"])).read_text(encoding="utf-8"))
    candidates_response = client.get(f"/api/citation-assistant/jobs/{job_id}/candidates?page_size=200")
    _assert(candidates_response.status_code == 200, "candidate API failed")
    candidates = list(candidates_response.get_json().get("items") or [])
    generated = [candidate for candidate in candidates if candidate.get("kind") == "generate"]
    audited = [candidate for candidate in candidates if candidate.get("kind") == "audit"]
    partial = _partial_quote_candidates(candidates, extracted)
    results.update({
        "candidate_count": len(candidates), "generate_count": len(generated),
        "audit_count": len(audited), "manual_endnote_count": analyzed["flags"].get("manual_endnote_count"),
        "partial_quote_candidates": len(partial),
    })
    _assert(generated, "no source-backed generation candidate was created")
    _assert(audited, "manual superscript/endnote citation was not audited")
    _assert(not partial, f"candidate splits a complete quotation: {partial}")

    _login(client, 990002)
    foreign_read = client.get(f"/api/citation-assistant/jobs/{job_id}")
    foreign_delete = client.delete(
        f"/api/citation-assistant/jobs/{job_id}", headers={"X-CSRF-Token": "isolated-csrf-token"},
    )
    results["cross_user_read_status"] = foreign_read.status_code
    results["cross_user_delete_status"] = foreign_delete.status_code
    _assert(foreign_read.status_code == 404 and foreign_delete.status_code == 404, "cross-user job isolation failed")

    _login(client, 990001)
    chosen = generated[0]
    decision_response = client.patch(
        f"/api/citation-assistant/jobs/{job_id}/decisions",
        json={"decisions": [{
            "id": chosen["id"], "decision": "accepted", "selected_option": 0,
            "proposed_citation": chosen.get("proposed_citation") or "测试引文",
        }]}, headers={"X-CSRF-Token": "isolated-csrf-token"},
    )
    _assert(decision_response.status_code == 200, "decision API failed")
    export_response = client.post(
        f"/api/citation-assistant/jobs/{job_id}/export",
        headers={"X-CSRF-Token": "isolated-csrf-token"},
    )
    _assert(export_response.status_code == 202, "export API did not queue the isolated job")
    tasks.run_export(job_id)
    completed = tasks.get_job(job_id, 990001)
    _assert(bool(completed) and completed["status"] == "complete", f"isolated export failed: {(completed or {}).get('error')}")

    docx_response = client.get(f"/api/citation-assistant/jobs/{job_id}/download/docx")
    pdf_response = client.get(f"/api/citation-assistant/jobs/{job_id}/download/pdf")
    results["docx_download_status"] = docx_response.status_code
    results["pdf_download_status"] = pdf_response.status_code
    _assert(docx_response.status_code == 200 and pdf_response.status_code == 200, "artifact download failed")
    _assert("wordprocessingml.document" in str(docx_response.content_type), "DOCX MIME type is wrong")
    _assert("application/pdf" in str(pdf_response.content_type), "PDF MIME type is wrong")
    docx_response.close()
    pdf_response.close()

    output_docx = Path(str(completed["output_docx_path"]))
    with zipfile.ZipFile(output_docx) as package:
        document = tasks._parse_xml(package.read("word/document.xml"))
        formats = document.xpath("./w:body//w:sectPr/w:footnotePr/w:numFmt/@w:val", namespaces=tasks.NS)
        restarts = document.xpath("./w:body//w:sectPr/w:footnotePr/w:numRestart/@w:val", namespaces=tasks.NS)
        real_footnote_refs = len(document.xpath(".//w:footnoteReference", namespaces=tasks.NS))
    results.update({
        "footnote_formats": formats, "footnote_restarts": restarts,
        "real_footnote_refs": real_footnote_refs,
    })
    _assert(formats and set(formats) == {"decimalEnclosedCircle"}, "footnotes are not circled")
    _assert(restarts and set(restarts) == {"eachPage"}, "footnotes do not restart per page")
    _assert(real_footnote_refs >= 1, "exported Word has no genuine footnote reference")

    with fitz.open(str(completed["output_pdf_path"])) as report:
        rendered = []
        pdf_text = "\n".join(page.get_text("text") for page in report)
        for page in report:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.7, 0.7), colorspace=fitz.csGRAY, alpha=False)
            rendered.append(bool(pixmap.samples) and min(pixmap.samples) < 250)
        results["pdf_pages"] = report.page_count
        results["pdf_pages_nonblank"] = all(rendered)
        results["wysiwyg_pdf_name"] = Path(str(completed["output_pdf_path"])).name
        results["wysiwyg_original_text_visible"] = (
            tasks.normalize(str(chosen.get("paper_text") or ""))[:18] in tasks.normalize(pdf_text)
        )
        results["wysiwyg_comment_visible"] = (
            tasks.normalize(str(chosen.get("issue_label") or "")) in tasks.normalize(pdf_text)
        )
        _assert(report.page_count >= 1 and all(rendered), "PDF did not render correctly")
        _assert(bool(results["wysiwyg_original_text_visible"]), "PDF lost the original cited text")
        _assert(bool(results["wysiwyg_comment_visible"]), "PDF did not visibly render the margin comment")

    # Genuine Word endnote parsing/audit, still in the isolated database.
    endnote_job = tasks.create_job(
        990001, true_endnote_fixture.name, true_endnote_fixture.read_bytes(), mode="audit",
        note_kind="endnote", threshold="conservative", citation_style="gb2015",
        scope_tokens=["vol:文集:1"], corpus_sha256="isolated", template_version="isolated",
    )
    tasks.run_extraction(endnote_job["id"])
    endnote_job = tasks.get_job(endnote_job["id"], 990001)
    endnote_sections = [str(section["id"]) for section in endnote_job["sections"] if section.get("default_selected")]
    tasks.set_analysis_config(endnote_job["id"], 990001, section_ids=endnote_sections, scope_tokens=["vol:文集:1"])
    tasks.run_analysis(endnote_job["id"], web.corpus)
    endnote_candidates = tasks.all_candidates(endnote_job["id"])
    results["true_endnote_count"] = endnote_job["flags"].get("endnote_count")
    results["true_endnote_audit_candidates"] = sum(item.get("kind") == "audit" for item in endnote_candidates)
    _assert(results["true_endnote_count"] and results["true_endnote_audit_candidates"], "genuine Word endnote audit failed")
    _assert(tasks.delete_job(endnote_job["id"], 990001), "true-endnote cleanup failed")

    # Protection parsing and hardened ZIP/XML rejection.
    fixture_bytes = fixture.read_bytes()
    protected = _rewrite_settings(fixture_bytes, "1")
    disabled_protection = _rewrite_settings(fixture_bytes, "0")
    results["protected_detected"] = tasks.validate_docx_bytes(protected, filename="protected.docx")["protected"]
    results["disabled_protection_detected"] = tasks.validate_docx_bytes(
        disabled_protection, filename="unprotected.docx",
    )["protected"]
    _assert(results["protected_detected"] is True, "enabled protection was not detected")
    _assert(results["disabled_protection_detected"] is False, "disabled protection was treated as enabled")

    unsafe = BytesIO()
    with zipfile.ZipFile(unsafe, "w") as package:
        package.writestr("../escape.xml", b"x")
    rejected_traversal = False
    try:
        tasks.validate_docx_bytes(unsafe.getvalue(), filename="unsafe.docx")
    except tasks.CitationAssistantError:
        rejected_traversal = True
    results["path_traversal_rejected"] = rejected_traversal
    _assert(rejected_traversal, "ZIP path traversal was accepted")

    dtd_payload = BytesIO()
    with zipfile.ZipFile(BytesIO(fixture_bytes)) as zin, zipfile.ZipFile(dtd_payload, "w") as zout:
        for name in zin.namelist():
            content = zin.read(name)
            if name == "word/document.xml":
                content = b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]>' + content
            zout.writestr(name, content)
    rejected_dtd = False
    try:
        tasks.validate_docx_bytes(dtd_payload.getvalue(), filename="unsafe-docx.docx")
    except tasks.CitationAssistantError:
        rejected_dtd = True
    results["dtd_rejected"] = rejected_dtd
    _assert(rejected_dtd, "DTD/external entity payload was accepted")

    bad_internal_token = client.post(
        "/internal/citation-assistant/claim",
        headers={"Authorization": "Bearer invalid", "X-Worker-ID": "isolated"}, json={},
    )
    results["invalid_worker_token_status"] = bad_internal_token.status_code
    _assert(bad_internal_token.status_code == 404, "invalid internal worker token was disclosed or accepted")

    delete_response = client.delete(
        f"/api/citation-assistant/jobs/{job_id}", headers={"X-CSRF-Token": "isolated-csrf-token"},
    )
    _assert(delete_response.status_code == 200, "owner could not delete isolated job")
    _assert(not (tasks.ARTIFACT_ROOT / "990001" / job_id).exists(), "isolated job artifacts remain")

    # The caller removes the outer directory after this process exits.  Some
    # platforms keep application SQLite handles open until interpreter shutdown.
    results["citation_job_artifacts_removed"] = not (tasks.ARTIFACT_ROOT / "990001" / job_id).exists()
    results["ok"] = True
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="论文引文助手生产栈隔离验收")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--true-endnote-fixture", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.fixture, args.true_endnote_fixture, args.work_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
