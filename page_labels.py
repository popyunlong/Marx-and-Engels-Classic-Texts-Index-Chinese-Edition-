"""Evidence-aware book pagination shared by citations and readers.

PDF positions remain the navigation identity. Numbering systems and independently
numbered sections are presentation metadata, never offsets into the source text.
"""
from __future__ import annotations

import logging
from typing import Any

VERSION = "book-pages-20260912-v1"
PDF_NOTE = "（此为PDF页码，非原书印刷页码）"
from ingestion.page_tokens import parse_label


def shown_label(value: Any) -> str:
    parsed = parse_label(value)
    if not parsed:
        return ""
    return parsed[0][4:].upper() if parsed[1] == "roman" else parsed[0]


def page_reference(page: Any) -> dict:
    info = getattr(page, "page_label_info", None) or {}
    parsed = parse_label(getattr(page, "printed_page", None))
    status = info.get("status", "legacy")
    # Existing numeric pagination remains compatible. Old roman OCR requires
    # evidence before becoming a book citation; a syntactically valid L may be 1.
    trusted = status in {"verified", "inferred", "manual"}
    usable = parsed is not None and status not in {"needs_review", "unnumbered", "source_error"}
    usable = usable and (parsed[1] != "roman" or trusted)
    label = parsed[0] if usable else None
    return {"pdf_page": int(page.pdf_page), "printed_page": label,
            "display_label": shown_label(label) if label else f"PDF {int(page.pdf_page)}",
            "numbering_system": parsed[1] if usable else "unknown",
            "segment_id": info.get("segment_id", ""),
            "segment_title": info.get("segment_title", ""),
            "status": status if usable or status != "legacy" else "needs_review"}


def citation_pages(pages: list[Any]) -> dict:
    refs = [page_reference(p) for p in pages]
    groups: list[list[dict]] = []
    for ref in refs:
        previous = groups[-1][-1] if groups else None
        compatible = previous and all(previous[k] == ref[k] for k in
                                      ("numbering_system", "segment_id", "segment_title"))
        consecutive = previous and ref["pdf_page"] == previous["pdf_page"] + 1
        if compatible and consecutive:
            a, b = parse_label(previous["printed_page"]), parse_label(ref["printed_page"])
            consecutive = (not a and not b) or (a and b and b[2] == a[2] + 1)
        if compatible and consecutive:
            groups[-1].append(ref)
        else:
            groups.append([ref])
    full, compact = [], []
    for group in groups:
        first, last = group[0], group[-1]
        known = bool(first["printed_page"])
        a = first["display_label"] if known else str(first["pdf_page"])
        b = last["display_label"] if known else str(last["pdf_page"])
        span = a if a == b else a + "-" + b
        title = first["segment_title"] if known else ""
        note = "" if known else PDF_NOTE
        full.append((title + "，" if title else "") + "第" + span + "页" + note)
        compact.append((title + ":" if title else "") + span + note)
    page_note = PDF_NOTE if len(groups) == 1 and not groups[0][0]["printed_page"] else ""
    page_range = "；".join(compact) or "页码未核实"
    if page_note:
        page_range = page_range.removesuffix(page_note)
    return {"page": "；".join(full) or "页码未核实", "page_range": page_range,
            "page_note": page_note, "page_refs": refs}


def load_page_evidence(conn) -> dict[tuple[str, int], dict]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_label_evidence'").fetchone():
        return {}
    import json
    result = {}
    invalid = 0
    for source, pdf, payload in conn.execute("SELECT source_file,pdf_page,payload FROM page_label_evidence"):
        try:
            info = json.loads(payload)
            if not isinstance(info, dict) or info.get('status') not in {'manual','verified','inferred','needs_review','unnumbered','source_error'}:
                raise ValueError('Invalid pagination evidence status')
            if info.get('printed_page') is not None and not parse_label(info['printed_page']):
                raise ValueError('Invalid folio')
            if not all(isinstance(info.get(key,''), str) for key in ('segment_id','segment_title')):
                raise ValueError('Invalid pagination segment')
        except (TypeError, ValueError):
            info = {'status':'needs_review','printed_page':None,'basis':'invalid_evidence_record'}
            invalid += 1
        result[source, int(pdf)] = info
    if invalid:
        logging.getLogger(__name__).warning('Ignored %d invalid pagination evidence records; affected pages use PDF locations', invalid)
    return result


def apply_page_evidence(source: str, pages: list[Any], evidence: dict) -> None:
    for page in pages:
        info = evidence.get((source, int(page.pdf_page)))
        if info is not None:
            page.page_label_info = info
            if info.get("status") in {"verified", "inferred", "manual", "unnumbered", "source_error", "needs_review"}:
                page.printed_page = info.get("printed_page")
        # Keep all consumers consistent with the citation renderer, including
        # the reader's legacy printed_pages dictionary.
        page.printed_page = page_reference(page)["printed_page"]
