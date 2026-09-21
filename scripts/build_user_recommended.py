"""Build the immutable seven-book user recommendation ingestion batch.

The source PDFs and PaddleOCR JSON files are never modified.  Every generated
package keeps the OCR blocks and geometry as provenance, while the runtime text
remains explicitly marked provisional.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion.paddle_source import (  # noqa: E402
    consolidate_sequences,
    fingerprint,
    key as text_key,
    pages_from_json,
    toc_from_pages,
)

BATCH = "user-recommended-20260913"
QUALITY_NOTE = "识别文本持续校对，请以 PDF 原文为准"
SPAM_MARKERS = (
    "朋友圈", "微信", "书籍免费", "z-library", "zlibrary", "图书在版编目",
    "版权信息", "版权声明", "关注公众号", "扫码", "目录",
)
LEADING_MARKER = re.compile(
    r"^(?:第[一二三四五六七八九十百零〇0-9]+(?:章|篇|部|编|部分)(?:[（(]续[）)])?|"
    r"附录[一二三四五六七八九十百零〇0-9]*|序言|前言|导言|引言|结语|后记|致谢)$"
)
PART_PREFIX = re.compile(r"^第[一二三四五六七八九十百零〇0-9]+(?:部|编|部分)")
CHAPTER_PREFIX = re.compile(r"^第[一二三四五六七八九十百零〇0-9]+(?:章|篇)")
SUBSECTION_PREFIX = re.compile(r"^(?:[一二三四五六七八九十百]+[、.]|\d+[、.]|第.+节)")
SLASH_TOC_ENTRY = re.compile(r"^(?P<title>.+?)/(?P<page>[0-9]{1,4})$")
PAREN_TOC_ENTRY = re.compile(r"^(?P<title>.+?)(?:…+|\.{2,})[（(](?P<page>[0-9]{1,4})[）)]$")


def _hash_bytes(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 << 20), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _clean_heading(value: str) -> str:
    value = " ".join(str(value or "").split()).strip(" ·.．—-\t")
    value = re.sub(r"\s*\$\s*\^\{\[[0-9]+\]\}\s*\$\s*", "", value)
    return re.sub(r"\s+([，。；：、？！])", r"\1", value)


def _usable_heading(title: str, *, book_title: str) -> bool:
    compact = text_key(title)
    if len(compact) < 2 or len(title) > 120:
        return False
    lowered = title.lower()
    if any(marker in lowered for marker in SPAM_MARKERS):
        return False
    book_key = text_key(book_title)
    if compact in {book_key, "注释", "名词解释", "说明"}:
        return False
    if (
        compact.endswith(book_key)
        and len(compact) <= len(book_key) + 20
        and not (CHAPTER_PREFIX.match(title) or PART_PREFIX.match(title))
    ):
        return False
    if title.startswith(("图", "表")) and re.match(r"^[图表][序0-9一二三四五六七八九十-]", title):
        return False
    if re.fullmatch(r"[\W_0-9ivxlcdmIVXLCDM]+", title):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", title))


def _heading_level(title: str) -> int:
    if PART_PREFIX.match(title) or title.startswith(("序言", "前言", "译者前言", "导言", "引言", "绪论", "结语", "后记", "附录", "参考文献")):
        return 1
    if CHAPTER_PREFIX.match(title) or title.startswith("序章"):
        return 2
    if SUBSECTION_PREFIX.match(title):
        return 3
    return 3


def structural_toc(pages: list[dict], metadata: dict) -> list[dict]:
    """Build ordered landings only from title blocks on their target pages."""
    start = int(metadata["body_page_start"])
    end = int(metadata["body_page_end"])
    entries: list[dict] = []
    seen: set[tuple[str, int]] = set()
    seen_appendices: set[str] = set()
    for page in pages[start - 1:end]:
        headings = [_clean_heading(item) for item in page.get("headings") or []]
        headings = [item for item in headings if _usable_heading(item, book_title=metadata["title"])]
        combined: list[str] = []
        pending = ""
        for title in headings:
            if pending:
                if LEADING_MARKER.match(title):
                    combined.append(pending)
                    pending = title
                else:
                    combined.append(f"{pending} {title}".strip())
                    pending = ""
            elif LEADING_MARKER.match(title):
                pending = title
            else:
                combined.append(title)
        if pending:
            combined.append(pending)
        for title in combined:
            identity = (text_key(title), int(page["page"]))
            if not identity[0] or identity in seen:
                continue
            if title.startswith("附录") and identity[0] in seen_appendices:
                continue
            seen.add(identity)
            if title.startswith("附录"):
                seen_appendices.add(identity[0])
            entries.append({
                "title": title,
                "pdf_page": int(page["page"]),
                "printed_page": page.get("label") or None,
                "level": _heading_level(title),
                "kind": "chapter",
                "sort_order": len(entries),
                "evidence": {
                    "method": "target_page_title_block",
                    "source": "user_paddle_json",
                    "block_geometry_retained": True,
                },
            })
    return entries


def _printed_toc(pages: list[dict], book_key: str) -> list[dict]:
    toc, unresolved = toc_from_pages(pages)
    allowed = {"user_rec_gao_german_philosophy_2020": ("高宣扬文集总序", "自序")}
    remaining = []
    for item in unresolved:
        title = str(item.get("title") or item.get("text") or "")
        if not any(text_key(title).startswith(text_key(prefix)) for prefix in allowed.get(book_key, ())):
            remaining.append(item)
    if remaining:
        raise ValueError(f"{book_key}: unresolved printed TOC entries: {remaining}")
    return toc


def _layout_printed_toc(pages: list[dict], book_key: str, metadata: dict) -> list[dict]:
    """Parse the two non-standard printed contents layouts, then verify landings.

    ``历史哲学`` uses ``标题/页码`` while ``技术封建主义`` uses full-width
    parenthesised page numbers.  Each printed reference is mapped to its physical
    PDF page by the edition-specific offset, with four front-matter exceptions
    recorded explicitly in the catalog.  Body title blocks supplement the
    printed top level without replacing its provenance.
    """
    first = int(metadata["toc_page_start"])
    last = int(metadata["toc_page_end"])
    offset = int(metadata["printed_to_pdf_offset"])
    overrides = {
        text_key(title): int(pdf_page)
        for title, pdf_page in (metadata.get("toc_manual_overrides") or {}).items()
    }
    title_corrections = {
        text_key(title): str(corrected).strip()
        for title, corrected in (metadata.get("toc_title_corrections") or {}).items()
    }
    pattern = SLASH_TOC_ENTRY if book_key == "user_rec_hegel_history_2006" else PAREN_TOC_ENTRY
    entries: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for toc_page in pages[first - 1:last]:
        for raw_line in str(toc_page.get("text") or "").splitlines():
            line = " ".join(raw_line.split()).strip()
            match = pattern.match(line)
            if not match:
                continue
            title = _clean_heading(re.sub(r"(?:…+|\.{2,})$", "", match.group("title")))
            if not _usable_heading(title, book_title=metadata["title"]):
                continue
            printed_page = int(match.group("page"))
            title_id = text_key(title)
            title = title_corrections.get(title_id, title)
            title_id = text_key(title)
            pdf_page = overrides.get(title_id, printed_page + offset)
            if not 1 <= pdf_page <= len(pages):
                raise ValueError(f"{book_key}: printed TOC landing outside PDF: {title!r}")
            target_text = text_key(str(pages[pdf_page - 1].get("text") or ""))
            # Synopsis-only rows in the printed contents do not reappear as a
            # heading in the body.  They are intentionally omitted; only a
            # verifiable title landing becomes a public directory item.
            if title_id not in target_text:
                if title_id in overrides:
                    raise ValueError(
                        f"{book_key}: manually corrected TOC title does not match landing: {title!r} -> {pdf_page}"
                    )
                continue
            identity = (title_id, pdf_page)
            if identity in seen:
                continue
            seen.add(identity)
            entries.append({
                "title": title,
                "pdf_page": pdf_page,
                "printed_page": str(printed_page),
                "level": _heading_level(title),
                "kind": "chapter",
                "sort_order": len(entries),
                "evidence": {
                    "method": "edition_specific_printed_toc_layout",
                    "source": "user_paddle_json",
                    "toc_pdf_page": int(toc_page["page"]),
                    "printed_reference": printed_page,
                    "target_rule": "manual_override" if title_id in overrides else f"printed_plus_{offset}",
                    "landing_title_verified": True,
                    "block_geometry_retained": True,
                },
            })

    if not entries:
        raise ValueError(f"{book_key}: no entries parsed from special printed TOC")

    # Preserve useful subsection title blocks which the compact printed contents omits.
    for item in structural_toc(pages, metadata):
        identity = (text_key(item["title"]), int(item["pdf_page"]))
        if identity not in seen:
            seen.add(identity)
            entries.append(item)
    return entries


def _metadata(book_key: str, row: dict) -> dict:
    return {
        "book_key": book_key,
        "title": row["title"],
        "display_title": row["title"],
        "citation_title": row["citation_title"],
        "authors": row["authors"],
        "translators": row.get("translators", []),
        "editors": row.get("editors", []),
        "place": row["place"],
        "publisher": row["publisher"],
        "year": row["year"],
        "edition": row.get("edition", ""),
        "collection": "user_recommended",
        "volume": 1,
        "volume_label": "",
        "single_volume": True,
        "recommendation_id": int(row["recommendation_id"]),
        "recommender_name": row["recommender_name"],
        "recommender_email_masked": row["recommender_email_masked"],
        "public_window_days": 30,
        "bibliography_source_pages": row.get("bibliography_source_pages", []),
    }


def build(output: Path) -> dict:
    manifest_path = ROOT / "config/ingestion_user_recommended_202609.json"
    catalog_path = ROOT / "config/user_recommended_catalog.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if manifest.get("batch") != BATCH or len(manifest.get("files") or []) != 7:
        raise ValueError("user recommendation allowlist must contain exactly seven books")
    if catalog.get("quality_note") != QUALITY_NOTE:
        raise ValueError("provisional-text notice changed")
    output.mkdir(parents=True, exist_ok=True)
    packages_dir = output / "packages"
    packages_dir.mkdir(exist_ok=True)
    report = []
    seen_sources: set[str] = set()
    seen_recommendations: set[int] = set()
    for source in manifest["files"]:
        key = source["key"]
        row = catalog["books"].get(key)
        if not row or int(row["recommendation_id"]) != int(source["recommendation_id"]):
            raise ValueError(f"{key}: recommendation record mapping mismatch")
        pdf_path = ROOT / source["source"]
        json_path = ROOT / source["json"]
        pdf_hash, byte_count = _hash_bytes(pdf_path)
        json_hash, _ = _hash_bytes(json_path)
        if (pdf_hash, byte_count, json_hash) != (source["sha256"], source["bytes"], source["json_sha256"]):
            raise ValueError(f"{key}: source identity mismatch")
        with fitz.open(pdf_path) as document:
            pdf_pages = document.page_count
        if pdf_pages != int(source["pages"]):
            raise ValueError(f"{key}: verified PDF page count changed")
        if pdf_hash in seen_sources or int(source["recommendation_id"]) in seen_recommendations:
            raise ValueError("duplicate source or recommendation record")
        seen_sources.add(pdf_hash)
        seen_recommendations.add(int(source["recommendation_id"]))

        pages = pages_from_json(json_path, pdf_pages)
        consolidate_sequences(pages)
        if not row.get("reliable_printed_pages"):
            for page in pages:
                page["label"] = ""
                page["label_evidence"] = None
        if row["toc_strategy"] == "printed":
            toc = _printed_toc(pages, key)
        elif row["toc_strategy"] == "layout_printed":
            toc = _layout_printed_toc(pages, key, row)
        else:
            toc = structural_toc(pages, row)
        if not toc:
            raise ValueError(f"{key}: TOC is empty")
        toc.sort(key=lambda item: (int(item["pdf_page"]), int(item.get("level") or 1)))
        for sort_order, item in enumerate(toc):
            item["sort_order"] = sort_order
        if any(not 1 <= int(item["pdf_page"]) <= pdf_pages for item in toc):
            raise ValueError(f"{key}: TOC target outside PDF")
        if [item["pdf_page"] for item in toc] != sorted(item["pdf_page"] for item in toc):
            raise ValueError(f"{key}: TOC targets are not monotonic")
        if any(not text_key(item["title"]) for item in toc):
            raise ValueError(f"{key}: empty TOC title")

        metadata = _metadata(key, row)
        package = {
            "schema": 1,
            "batch": BATCH,
            "book_id": pdf_hash[:32],
            "source_sha256": pdf_hash,
            "source_file": f"pdfs/自动入库/{pdf_hash}.pdf",
            "page_count": pdf_pages,
            "metadata": metadata,
            "pages": [
                {
                    "page": page["page"], "text": page["text"], "label": page["label"],
                    "checked": False, "label_evidence": page["label_evidence"],
                    "blocks": page["blocks"],
                }
                for page in pages
            ],
            "toc": toc,
            "release_quality": "provisional_text",
            "pending_text_pages": pdf_pages,
            "quality_note": QUALITY_NOTE,
            "paddle_source": {
                "schema": 1,
                "json_sha256": json_hash,
                "source_json_name": json_path.name,
                "source_pdf_name": pdf_path.name,
                "manifest_sha256": fingerprint(manifest_path),
                "catalog_sha256": fingerprint(catalog_path),
                "text_review": "not_full_text_verified",
                "layout_geometry": "retained_in_package",
            },
        }
        package_path = packages_dir / f"{key}.json"
        package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        report.append({
            "key": key, "recommendation_id": source["recommendation_id"], "source_sha256": pdf_hash,
            "json_sha256": json_hash, "bytes": byte_count, "pages": pdf_pages, "toc": len(toc),
            "package_sha256": fingerprint(package_path),
        })
    summary = {
        "schema": 1,
        "batch": BATCH,
        "books": report,
        "totals": {"books": len(report), "sources": len(seen_sources), "pages": sum(row["pages"] for row in report)},
    }
    if summary["totals"] != {"books": 7, "sources": 7, "pages": 2648}:
        raise ValueError(f"batch totals mismatch: {summary['totals']}")
    (output / "packages-manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/user-recommended")
    args = parser.parse_args()
    summary = build(args.output)
    print(json.dumps(summary["totals"], ensure_ascii=False))


if __name__ == "__main__":
    main()
