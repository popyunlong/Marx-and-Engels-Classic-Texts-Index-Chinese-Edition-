#!/usr/bin/env python3
"""Build navigable Kant/Feuerbach TOCs from their MiMo transcripts."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

try:  # package import in tests / direct script execution in operations
    from . import build_hegel_toc as builder
    from . import ocr_hegel_mimo as ocr_engine
    from . import ocr_philosophy_mimo as philosophy_ocr
    from .philosophy_catalog import CATALOG_PATH, ROOT, load_volumes
except ImportError:  # pragma: no cover
    import build_hegel_toc as builder
    import ocr_hegel_mimo as ocr_engine
    import ocr_philosophy_mimo as philosophy_ocr
    from philosophy_catalog import CATALOG_PATH, ROOT, load_volumes


OUTPUT_ROOT = ROOT / "data" / "kant_feuerbach"
REPORT_PATH = OUTPUT_ROOT / "toc_audit.json"
REPORT_SCHEMA = "kant-feuerbach-toc-audit-v1"
_UNDERSCORE_TRAIL = re.compile(
    r"^(?P<title>.*?)(?:[_＿]{2,})\s*[\(（]?(?P<page>\d{1,4})[\)）]?\s*$"
)
_VISIBLE_NUMBERED_SECTION = re.compile(r"^(?P<section>[1-9]|[1-5][0-9]|6[0-5])$")
_NO_PRINTED_TOC_VOLUME = "feuerbach-future-philosophy"


def _trim_to_explicit_directory(
    transcripts: dict[int, str], toc_pages: list[int]
) -> list[int]:
    """Discard bibliography/history pages before a literal 目录 heading."""
    anchors = [
        page
        for page in toc_pages
        if any(builder._HEADER.fullmatch(line.strip()) for line in transcripts.get(page, "").splitlines())
    ]
    if not anchors:
        return toc_pages
    start = min(anchors)
    return [page for page in toc_pages if page >= start]


def _install_contract() -> None:
    philosophy_ocr._install_contract()

    original_toc_match = builder._toc_match

    def local_toc_match(line: str):
        return _UNDERSCORE_TRAIL.match(line.rstrip()) or original_toc_match(line)

    def local_toc_context(rows, transcripts, expected):
        page_map, offset = builder.printed_page_map(rows)
        first_body = min(
            (int(row["pdf_page"]) for row in rows if str(row["printed_page"] or "") == "1"),
            default=min(page_map.values()) if page_map else 0,
        )
        toc_pages = builder.discover_toc_pages(transcripts, first_body) if first_body else []
        toc_pages = _trim_to_explicit_directory(transcripts, toc_pages)
        if not toc_pages:
            # A front-matter directory can itself be numbered 1..N, making the
            # generic first-body guess land inside the directory.  Probe only
            # the bounded front matter, then rebuild mappings after its end.
            probe_end = min(expected + 1, 81)
            toc_pages = builder.discover_toc_pages(transcripts, probe_end)
            toc_pages = _trim_to_explicit_directory(transcripts, toc_pages)
        if not toc_pages:
            return page_map, offset, first_body, []
        toc_end = max(toc_pages)
        body_rows = [row for row in rows if int(row["pdf_page"]) > toc_end]
        body_map, body_offset = builder.printed_page_map(body_rows)
        if not body_map:
            raise RuntimeError("no printed-page mapping after directory")
        body_first = min(
            (
                int(row["pdf_page"])
                for row in body_rows
                if str(row["printed_page"] or "") == "1"
            ),
            default=min(body_map.values()),
        )
        return body_map, body_offset, body_first, toc_pages

    def local_catalog():
        return [
            {
                "id": item.id,
                "book": item.book,
                "volume": item.volume,
                "display_title": item.display_title,
                "file": item.file,
                "pages": item.pages,
                "sha256": item.sha256,
            }
            for item in load_volumes(CATALOG_PATH)
        ]

    def local_visible_body_headings(spec, rows, transcripts, toc_pages):
        if str(spec.get("id")) != _NO_PRINTED_TOC_VOLUME or toc_pages:
            return []
        printed_by_pdf = {
            int(row["pdf_page"]): str(row["printed_page"] or "").strip()
            for row in rows
        }
        found: dict[int, dict[str, object]] = {}
        for pdf_page in sorted(transcripts):
            for raw in transcripts[pdf_page].splitlines():
                heading = raw.strip()
                match = _VISIBLE_NUMBERED_SECTION.fullmatch(heading)
                if not match:
                    continue
                section = int(match.group("section"))
                if section in found:
                    raise RuntimeError(
                        f"duplicate visible section heading {section} on PDF page {pdf_page}"
                    )
                printed_page = printed_by_pdf.get(pdf_page, "")
                if not printed_page.isdigit():
                    raise RuntimeError(
                        f"visible section heading {section} has no printed-page mapping"
                    )
                found[section] = {
                    # Preserve the heading exactly as printed; do not invent a
                    # descriptive title for this directory-less work.
                    "title": heading,
                    "printed_page": printed_page,
                    "pdf_page": pdf_page,
                    "toc_pdf_page": pdf_page,
                    "level": 1,
                }
        expected_sections = set(range(1, 66))
        if set(found) != expected_sections:
            missing = sorted(expected_sections - set(found))
            extra = sorted(set(found) - expected_sections)
            raise RuntimeError(
                f"visible section audit failed: missing={missing}, extra={extra}"
            )
        return [found[number] for number in range(1, 66)]

    def local_transcripts(volume_id: str) -> dict[int, str]:
        path = OUTPUT_ROOT / f"{volume_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(path)
        pages: dict[int, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            pages[int(row["pdf_page"])] = str(row.get("text") or "")
        return pages

    builder.CATALOG = CATALOG_PATH
    builder.REPORT_PATH = REPORT_PATH
    builder.REPORT_SCHEMA = REPORT_SCHEMA
    builder._toc_match = local_toc_match
    builder.resolve_toc_context = local_toc_context
    builder.visible_body_headings = local_visible_body_headings
    builder.load_catalog = local_catalog
    builder.load_transcripts = local_transcripts


def _require_ocr_audit() -> None:
    final = philosophy_ocr.audit_all_pages()
    failed = [report["id"] for report in final["volumes"] if not report["passed"]]
    if failed:
        raise RuntimeError("OCR audit failed: " + ", ".join(failed))


def main(argv: list[str] | None = None) -> int:
    _install_contract()
    _require_ocr_audit()
    return builder.main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
