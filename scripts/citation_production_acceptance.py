from __future__ import annotations

"""Run an isolated citation-assistant acceptance job against a production corpus.

The caller must point HOME at a temporary directory or pass --work-dir.  The script never
touches membership data, never starts Flask, and deletes its uploaded body/artifacts after
all assertions pass.
"""

import argparse
import json
import sys
import time
import zipfile
from hashlib import sha256
from pathlib import Path

import fitz

# Direct execution from ``scripts/`` must still resolve the project modules.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import citation_assistant as tasks
from search import Corpus


def run(input_docx: Path, work_dir: Path, scope: list[str]) -> dict:
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    tasks.DB_PATH = work_dir / "citation-acceptance.sqlite3"
    tasks.ARTIFACT_ROOT = work_dir / "artifacts"
    tasks.init_db()

    corpus_started = time.perf_counter()
    corpus = Corpus.load_default()
    corpus_seconds = time.perf_counter() - corpus_started
    if corpus is None:
        raise RuntimeError("production corpus failed to load")

    original = input_docx.read_bytes()
    corpus_hash_path = Path(tasks.__file__).resolve().parent / "data" / "corpus.sqlite.sha256"
    corpus_hash = corpus_hash_path.read_text(encoding="utf-8").strip() if corpus_hash_path.is_file() else "acceptance"
    job = tasks.create_job(
        990001,
        input_docx.name,
        original,
        mode="generate",
        note_kind="footnote",
        threshold="conservative",
        citation_style="gb2015",
        scope_tokens=scope,
        corpus_sha256=corpus_hash,
        template_version="production-acceptance-gb2015-v1",
    )

    extraction_started = time.perf_counter()
    tasks.run_extraction(job["id"])
    extraction_seconds = time.perf_counter() - extraction_started
    extracted = tasks.get_job(job["id"], 990001)
    if not extracted or extracted.get("status") != "awaiting_sections":
        raise RuntimeError(f"extraction failed: {(extracted or {}).get('error')}")
    selected = [str(item["id"]) for item in extracted.get("sections") or [] if item.get("default_selected")]
    tasks.set_analysis_config(job["id"], 990001, section_ids=selected, scope_tokens=scope)

    analysis_started = time.perf_counter()
    tasks.run_analysis(job["id"], corpus)
    analysis_seconds = time.perf_counter() - analysis_started
    analyzed = tasks.get_job(job["id"], 990001)
    if not analyzed or analyzed.get("status") != "review_ready":
        raise RuntimeError(f"analysis failed: {(analyzed or {}).get('error')}")
    candidates = tasks.all_candidates(job["id"])
    extraction_payload = json.loads(Path(str(analyzed["extraction_path"])).read_text(encoding="utf-8"))
    paragraphs = list(extraction_payload.get("paragraphs") or [])
    partial_quote_candidates = []
    for item in candidates:
        if item.get("kind") != "generate":
            continue
        paragraph_index = int(item.get("paragraph_index") or 0)
        text = str(paragraphs[paragraph_index].get("text") or "")
        start, end = int(item.get("raw_start") or 0), int(item.get("raw_end") or 0)
        for match in tasks._QUOTE_RE.finditer(text):
            quote_start, quote_end = match.span()
            if start < quote_end and end > quote_start and not (start <= quote_start and end >= quote_end):
                partial_quote_candidates.append({
                    "paragraph_index": paragraph_index, "start": start, "end": end,
                    "quote_start": quote_start, "quote_end": quote_end,
                })
    if partial_quote_candidates:
        raise RuntimeError(f"citation candidates split quotations: {partial_quote_candidates}")
    exact = [item for item in candidates if item.get("match_type") == "exact" and item.get("source_options")]
    if not exact:
        raise RuntimeError("no exact source-backed candidate was generated")
    chosen = exact[0]
    tasks.save_decisions(job["id"], 990001, [{
        "id": chosen["id"],
        "decision": "accepted",
        "selected_option": 0,
        "proposed_citation": chosen.get("proposed_citation") or "",
    }])

    export_started = time.perf_counter()
    tasks.run_export(job["id"])
    export_seconds = time.perf_counter() - export_started
    completed = tasks.get_job(job["id"], 990001)
    if not completed or completed.get("status") != "complete":
        raise RuntimeError(f"export failed: {(completed or {}).get('error')}")
    docx = Path(str(completed.get("output_docx_path") or ""))
    pdf = Path(str(completed.get("output_pdf_path") or ""))
    if not docx.is_file() or not pdf.is_file():
        raise RuntimeError("expected DOCX/PDF artifacts were not produced")
    tasks.validate_exported_docx(docx)
    reparsed = tasks.extract_docx(docx)
    note_refs = [
        ref for paragraph in reparsed["paragraphs"] for ref in paragraph.get("note_refs") or []
        if ref.get("kind") in tasks.VALID_NOTE_KINDS
    ]
    if not note_refs:
        raise RuntimeError("exported DOCX contains no real Word note reference")
    with zipfile.ZipFile(input_docx) as source_package, zipfile.ZipFile(docx) as output_package:
        source_parts = {name: source_package.read(name) for name in source_package.namelist()}
        output_parts = {name: output_package.read(name) for name in output_package.namelist()}
        body = tasks._parse_xml(output_parts["word/document.xml"])
        section_props = body.xpath("./w:body//w:sectPr", namespaces=tasks.NS)
        if not section_props:
            raise RuntimeError("exported DOCX has no section properties")
        if any(
            section.xpath("./w:footnotePr/w:numFmt/@w:val", namespaces=tasks.NS)
            != ["decimalEnclosedCircle"]
            for section in section_props
        ):
            raise RuntimeError("footnote markers are not configured as circled numbers")
        if any(
            section.xpath("./w:footnotePr/w:numRestart/@w:val", namespaces=tasks.NS)
            != ["eachPage"]
            for section in section_props
        ):
            raise RuntimeError("footnote numbering does not restart on every page")
        allowed_changed_parts = {
            "[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels",
            "word/styles.xml", "word/footnotes.xml",
        }
        unexpected_changes = sorted(
            name for name, payload in source_parts.items()
            if name in output_parts and payload != output_parts[name] and name not in allowed_changed_parts
        )
        if unexpected_changes:
            raise RuntimeError(f"unrelated DOCX parts changed: {unexpected_changes}")
    with fitz.open(pdf) as report:
        if report.page_count < 1 or not any(page.get_text().strip() for page in report):
            raise RuntimeError("PDF report is empty")
        pdf_pages = report.page_count
        pdf_text = "\n".join(page.get_text() for page in report)
        if tasks.normalize(str(chosen.get("paper_text") or ""))[:18] not in tasks.normalize(pdf_text):
            raise RuntimeError("annotated PDF did not preserve the original cited sentence")
        if tasks.normalize(str(chosen.get("issue_label") or "")) not in tasks.normalize(pdf_text):
            raise RuntimeError("annotated PDF did not render the review comment in the margin")
        embedded_pdf_font = any(
            font[1] not in {"", "n/a"}
            for page in report for font in page.get_fonts(full=True)
        )
        if not embedded_pdf_font:
            raise RuntimeError("PDF report does not embed a Chinese font")
        rendered_pages = 0
        for page in report:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.7, 0.7), colorspace=fitz.csGRAY, alpha=False)
            if not pixmap.samples or min(pixmap.samples) >= 250:
                raise RuntimeError(f"PDF page {page.number + 1} rendered blank")
            rendered_pages += 1

    original_unchanged = Path(str(job["input_path"])).read_bytes() == original
    result = {
        "ok": True,
        "scope": scope,
        "corpus_load_seconds": round(corpus_seconds, 3),
        "extraction_seconds": round(extraction_seconds, 3),
        "analysis_seconds": round(analysis_seconds, 3),
        "export_seconds": round(export_seconds, 3),
        "candidate_count": len(candidates),
        "exact_candidate_count": len(exact),
        "chosen_issue_code": str(chosen.get("issue_code") or ""),
        "chosen_source_option_count": len(chosen.get("source_options") or []),
        "chosen_auto_selected": bool(chosen.get("auto_selected")),
        "partial_quote_candidates": len(partial_quote_candidates),
        "manual_endnote_count": int((analyzed.get("flags") or {}).get("manual_endnote_count") or 0),
        "word_note_reference_count": len(note_refs),
        "footnote_num_format": "decimalEnclosedCircle",
        "footnote_num_restart": "eachPage",
        "unexpected_changed_parts": unexpected_changes,
        "pdf_pages": pdf_pages,
        "wysiwyg_pdf_name": pdf.name,
        "wysiwyg_original_text_visible": True,
        "wysiwyg_comment_visible": True,
        "pdf_rendered_pages": rendered_pages,
        "embedded_pdf_font": embedded_pdf_font,
        "original_unchanged": original_unchanged,
        "input_sha256": sha256(original).hexdigest(),
        "docx_bytes": docx.stat().st_size,
        "pdf_bytes": pdf.stat().st_size,
    }
    if not original_unchanged:
        raise RuntimeError("input DOCX changed during acceptance run")
    if not tasks.delete_job(job["id"], 990001):
        raise RuntimeError("acceptance cleanup failed")
    result["cleanup_confirmed"] = not (tasks.ARTIFACT_ROOT / "990001" / job["id"]).exists()
    if not result["cleanup_confirmed"]:
        raise RuntimeError("acceptance artifacts remain after deletion")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="论文引文助手·生产语料隔离验收")
    parser.add_argument("input_docx", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--core", action="store_true", help="扫描核心五类马恩书库")
    args = parser.parse_args()
    scope = (
        ["book:文集", "book:全集", "book:全集二版", "book:马恩选集", "book:资本论"]
        if args.core else
        ([str(item) for item in args.scope] or ["vol:文集:1"])
    )
    print(json.dumps(run(args.input_docx, args.work_dir, scope), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
