from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import fitz

from build_index import normalize


def verify_candidate_runtime(corpus: Any, project_root: Path) -> dict[str, Any]:
    """Exercise the candidate's real search/reader/citation objects before it listens.

    The probe is derived from the candidate itself and never logged.  It avoids
    an external AI call; the HTTP health gate separately verifies that the AI
    page can start while this function validates citations used by AI answers.
    """
    if corpus is None:
        raise RuntimeError("candidate corpus is unavailable")
    selected = None
    for book in ("文集", "全集", *tuple(getattr(corpus, "books", {}).keys())):
        for volume in corpus.get_volumes(book):
            for page in volume.pages:
                match = re.search(r"[\u3400-\u9fff]{24,}", str(page.raw_text or ""))
                if match:
                    selected = (book, volume, page, match.group(0)[:24])
                    break
            if selected:
                break
        if selected:
            break
    if selected is None:
        raise RuntimeError("candidate has no stable Chinese search probe")

    book, volume, page, probe = selected
    probe_norm = normalize(probe)
    exact, _truncated = corpus._exact_in_book(book, probe_norm, probe, limit=8)
    if not exact or not any(
        hit.source_file == volume.source_file and any(item.pdf_page == page.pdf_page for item in hit.pages)
        for hit in exact
    ):
        raise RuntimeError("candidate exact-search acceptance failed")

    replacement = "龘" if probe[12] != "龘" else "靐"
    fuzzy_probe = probe[:12] + replacement + probe[13:]
    fuzzy, _truncated = corpus._fuzzy_in_book(book, normalize(fuzzy_probe), fuzzy_probe, limit=8)
    if not fuzzy or not any(hit.source_file == volume.source_file for hit in fuzzy):
        raise RuntimeError("candidate fuzzy-search acceptance failed")

    payload = exact[0].to_dict()
    citations = payload.get("citations") or {}
    if not payload.get("citation") or not citations.get("mkszyj") or not citations.get("gb2015"):
        raise RuntimeError("candidate citation/copy acceptance failed")
    if corpus.get_volume_by_source_file(volume.source_file) is None:
        raise RuntimeError("candidate reader lookup acceptance failed")

    root = Path(project_root).resolve()
    source = (root / volume.source_file).resolve()
    allowed_roots = [
        (root / name).resolve()
        for name in ("pdfs", "static_library", "stream_library")
        if (root / name).exists()
    ]
    if not allowed_roots or not any(
        _is_relative_to(source, allowed_root) for allowed_root in allowed_roots
    ):
        raise RuntimeError("candidate source path escapes approved source roots")
    if not source.is_file():
        raise RuntimeError("candidate original PDF is missing")
    with fitz.open(source) as document:
        if not 1 <= int(page.pdf_page) <= document.page_count:
            raise RuntimeError("candidate original PDF page is invalid")

    return {
        "exact": True,
        "fuzzy": True,
        "reader": True,
        "citation": True,
        "source_pdf": True,
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
