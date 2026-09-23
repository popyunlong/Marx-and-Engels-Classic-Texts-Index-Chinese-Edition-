from __future__ import annotations

"""Generate non-destructive three-article Word QA copies from hard local evidence."""

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from search import Corpus
import citation_assistant as core
import citation_agent_test_backend as backend


HASHES = {
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


def _xml_part(path: Path, name: str):
    with zipfile.ZipFile(path) as package:
        if name not in package.namelist():
            return None
        return core._parse_xml(package.read(name))


def _real_note_ids(path: Path, kind: str) -> set[str]:
    root = _xml_part(path, f"word/{kind}s.xml")
    if root is None:
        return set()
    return {
        str(node.get(f"{{{core.W_NS}}}id") or "")
        for node in root.xpath(f"./w:{kind}", namespaces=core.NS)
        if core._is_real_note_element(node)
    }


def _comment_ids(path: Path) -> set[str]:
    root = _xml_part(path, "word/comments.xml")
    if root is None:
        return set()
    return {
        str(node.get(f"{{{core.W_NS}}}id") or "")
        for node in root.xpath("./w:comment", namespaces=core.NS)
    }


def _assert_blue_insertions(output: Path, new_ids: dict[str, set[str]]) -> None:
    document = _xml_part(output, "word/document.xml")
    if document is None:
        raise RuntimeError("missing word/document.xml")
    for kind, ids in new_ids.items():
        if not ids:
            continue
        for note_id in ids:
            body_runs = document.xpath(
                f'.//w:r[w:{kind}Reference[@w:id="{note_id}"]] ', namespaces=core.NS,
            )
            if len(body_runs) != 1:
                raise RuntimeError(f"{kind} {note_id} body reference count is not one")
            colors = body_runs[0].xpath("./w:rPr/w:color/@w:val", namespaces=core.NS)
            if colors != [core.INSERTION_BLUE]:
                raise RuntimeError(f"{kind} {note_id} body reference is not light blue")
        notes = _xml_part(output, f"word/{kind}s.xml")
        for note_id in ids:
            note = notes.xpath(
                f'./w:{kind}[@w:id="{note_id}"]', namespaces=core.NS,
            )[0]
            colored_runs = note.xpath(".//w:r[w:t or w:footnoteRef or w:endnoteRef]", namespaces=core.NS)
            if not colored_runs or any(
                run.xpath("./w:rPr/w:color/@w:val", namespaces=core.NS) != [core.INSERTION_BLUE]
                for run in colored_runs
            ):
                raise RuntimeError(f"{kind} {note_id} marker or text is not light blue")


def _inspect_word(source: Path, output: Path, mode: str, expected_notes: int, expected_comments: int) -> dict:
    core.validate_exported_docx(output, filename=output.name)
    core._validate_existing_notes_unchanged(source, output, allow_new=mode in {"generate", "both"})
    source_document = _xml_part(source, "word/document.xml")
    output_document = _xml_part(output, "word/document.xml")
    source_text = "".join(source_document.xpath(".//w:body//w:t/text()", namespaces=core.NS))
    output_text = "".join(output_document.xpath(".//w:body//w:t/text()", namespaces=core.NS))
    if source_text != output_text:
        raise RuntimeError("visible body text changed")
    new_ids = {
        kind: _real_note_ids(output, kind) - _real_note_ids(source, kind)
        for kind in core.VALID_NOTE_KINDS
    }
    new_note_count = sum(len(ids) for ids in new_ids.values())
    if new_note_count != expected_notes:
        raise RuntimeError(f"new note count {new_note_count} != {expected_notes}")
    new_comment_count = len(_comment_ids(output) - _comment_ids(source))
    if new_comment_count != expected_comments:
        raise RuntimeError(f"new comment count {new_comment_count} != {expected_comments}")
    _assert_blue_insertions(output, new_ids)
    return {
        "word_valid": True,
        "visible_body_unchanged": True,
        "existing_notes_unchanged": True,
        "blue_insertions": new_note_count,
        "new_comments": new_comment_count,
    }


def run_case(
    name: str, path: Path, scope: list[str], corpus: Corpus, output_root: Path, mode: str,
    *, with_pdf: bool = False, locate_pdf_root: Path | None = None,
) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != HASHES[name]:
        raise RuntimeError(f"{name} SHA-256 mismatch: {digest}")
    extracted = core.extract_docx(path)
    sections = [str(item["id"]) for item in extracted["sections"] if item.get("default_selected")]
    candidates, style, _confidence = core.analyze_extraction(
        extracted, corpus, selected_sections=sections, scope_tokens=scope,
        mode=mode, threshold="conservative", citation_style="mkszyj",
        include_readonly_structures=True,
    )
    visible, counts = backend._partition_deterministic(
        candidates, corpus, scope, style, extracted=extracted, note_kind="footnote",
    )
    hidden = [
        item for item in visible
        if str(item.get("review_bucket") or "") == "unresolved"
        and (
            not item.get("source_options")
            or str(item.get("verification_scope") or "") == "locator_only"
        )
    ]
    visible = [item for item in visible if item not in hidden]
    counts["skipped_no_evidence_count"] += len(hidden)
    accepted = []
    for item in visible:
        copy = dict(item)
        copy["decision"] = (
            "accepted"
            if copy.get("text_match_level") == "exact"
            and copy.get("source_resolution") in {"unique", "reference_disambiguated"}
            and copy.get("writeback_mode") in {"footnote", "endnote", "comment"}
            else "pending"
        )
        if copy["decision"] == "accepted":
            accepted.append(copy)
    job_id = hashlib.md5((name + mode + digest).encode("utf-8"), usedforsecurity=False).hexdigest()
    core.ARTIFACT_ROOT = output_root
    job = {
        "id": job_id, "user_id": 1, "input_path": str(path),
        "original_filename": path.name, "mode": mode,
        "note_kind": "footnote", "resolved_style": style,
    }
    output = core.export_docx(job, accepted)
    expected_comments = sum(item.get("writeback_mode") == "comment" for item in accepted)
    expected_notes = sum(item.get("writeback_mode") in {"footnote", "endnote"} for item in accepted)
    inspection = _inspect_word(path, output, mode, expected_notes, expected_comments)
    located_comments = 0
    if locate_pdf_root is not None and mode in {"audit", "both"}:
        rendered_pdf = locate_pdf_root / f"{output.stem}.pdf"
        if not rendered_pdf.is_file():
            raise RuntimeError(f"missing rendered QA PDF: {rendered_pdf}")
        review_items = core._pdf_review_manifest(job, accepted)
        with core.fitz.open(rendered_pdf) as rendered:
            page_geometries = [core._pdf_normalized_geometry(page) for page in rendered]
            page_texts = [geometry[0] for geometry in page_geometries]
            for item in review_items:
                core._locate_pdf_candidate(
                    rendered, page_texts, item, page_geometries=page_geometries,
                )
                located_comments += 1
        if located_comments != expected_comments:
            raise RuntimeError(
                f"located comment count {located_comments} != {expected_comments}"
            )
    pdf_output = ""
    pdf_error = ""
    if with_pdf and mode in {"audit", "both"}:
        try:
            pdf_output = str(core.export_pdf(job, accepted, source_docx=output))
        except Exception as exc:
            pdf_error = str(exc)
    return {
        "name": name, "mode": mode, "sha256": digest, "output": str(output),
        "pdf_output": pdf_output, "pdf_error": pdf_error,
        "sections": len(extracted["sections"]), "actionable": len(visible),
        "accepted": len(accepted),
        "comments": expected_comments,
        "footnotes": expected_notes,
        "exact": sum(item.get("text_match_level") == "exact" for item in visible),
        "near": sum(item.get("text_match_level") == "near" for item in visible),
        "unresolved_visible": sum(item.get("review_bucket") == "unresolved" for item in visible),
        **counts, **inspection, "located_pdf_comments": located_comments,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wang", type=Path)
    parser.add_argument("pan", type=Path)
    parser.add_argument("technology", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--with-pdf", action="store_true")
    parser.add_argument("--locate-pdf-root", type=Path)
    args = parser.parse_args()
    corpus = Corpus.load_default()
    cases = [
        ("wang", args.wang, WANG_SCOPE),
        ("pan", args.pan, MARX_SCOPE),
        ("technology", args.technology, MARX_SCOPE),
    ]
    reports = [
        run_case(
            name, path, scope, corpus, args.output_root, mode, with_pdf=args.with_pdf,
            locate_pdf_root=args.locate_pdf_root,
        )
        for name, path, scope in cases
        for mode in ("generate", "audit", "both")
    ]
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
