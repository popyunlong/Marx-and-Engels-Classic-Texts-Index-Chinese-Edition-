#!/usr/bin/env python3
"""Build navigable Hegel TOCs from MiMo-faithful printed-directory transcripts."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "hegel_volumes.yaml"
DB_PATH = ROOT / "data" / "corpus.sqlite"
REPORT_PATH = ROOT / "data" / "hegel" / "toc_audit.json"
REPORT_SCHEMA = "hegel-toc-audit-v1"

_DOT_TRAIL = re.compile(
    r"^(?P<title>.*?)(?:[·⋯…．.。]{2,}|\s{3,})\s*"
    r"[\(（]?(?P<page>\d{1,4})[\)）]?\s*$"
)
_SPACE_TRAIL = re.compile(
    r"^(?P<title>\S.{1,90}?)\s+[\(（]?(?P<page>\d{1,4})[\)）]?\s*$"
)
_PURE_NUMBER = re.compile(r"^\d{1,4}$")
_HEADER = re.compile(r"^(目录|目\s*录|上卷目录|下卷目录|第[一二三四五六七八九十]+卷目录)$")
_STRUCTURAL_HEADER = re.compile(
    r"^第[一二三四五六七八九十百]+(?:部|篇|期|章)(?:\s|$|[：:])"
)
_LEADING_HEADER_PAGE = re.compile(r"^\s*\d{1,4}\s+(?=\D)")
_TRAILING_NOISE = re.compile(r"[·⋯…．.。\s]+$")


def load_catalog() -> list[dict[str, Any]]:
    payload = yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or {}
    return [dict(row) for row in payload.get("volumes") or []]


def load_transcripts(volume_id: str) -> dict[int, str]:
    path = ROOT / "data" / "hegel" / f"{volume_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    pages: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        pages[int(row["pdf_page"])] = str(row.get("text") or "")
    return pages


def _toc_match(line: str) -> re.Match[str] | None:
    match = _DOT_TRAIL.match(line.rstrip())
    if match:
        return match
    return _SPACE_TRAIL.match(line.rstrip())


def toc_page_score(text: str) -> int:
    return sum(1 for line in text.splitlines() if _toc_match(line.strip()))


def discover_toc_pages(transcripts: dict[int, str], first_body_pdf: int) -> list[int]:
    front = range(1, max(1, first_body_pdf))
    scored = {page: toc_page_score(transcripts.get(page, "")) for page in front}
    anchors = [
        page
        for page in front
        if any(_HEADER.match(line.strip()) for line in transcripts.get(page, "").splitlines())
    ]
    candidates = [page for page, score in scored.items() if score >= 2]
    if not candidates:
        return []
    start = min(candidates + anchors) if anchors else min(candidates)
    end = max(candidates)
    # Include low-density continuation pages between strong pages and immediately after
    # the last one.  A final TOC leaf often occupies a page by itself; omitting that page
    # would silently remove a valid chapter while the remaining directory still "passes".
    while start > 1 and scored.get(start - 1, 0) >= 1:
        start -= 1
    while end + 1 < first_body_pdf and scored.get(end + 1, 0) >= 1:
        end += 1
    return list(range(start, end + 1))


def clean_title(value: str) -> str:
    title = value.replace("\u3000", " ").strip()
    title = _LEADING_HEADER_PAGE.sub("", title)
    title = _TRAILING_NOISE.sub("", title).strip(" \t-—–")
    title = re.sub(r"\s+", " ", title)
    return title


def infer_level(title: str, indent: int) -> int:
    if re.match(r"^第[一二三四五六七八九十百]+(部|篇)", title):
        return 1
    if re.match(r"^第[一二三四五六七八九十百]+章", title):
        return 2
    if re.match(r"^第[一二三四五六七八九十百]+期", title):
        return 2
    if re.match(r"^第[一二三四五六七八九十百]+节", title):
        return 3
    base = 1 + min(3, indent // 4)
    if re.match(r"^[甲乙丙丁戊己庚辛壬癸]、", title):
        return max(2, base)
    if re.match(r"^[一二三四五六七八九十]+、", title):
        return max(2, base)
    if re.match(r"^\d+[.、]", title):
        return max(3, base)
    return base


def parse_toc_page(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    pending_wrap: list[tuple[int, str]] = []
    pending_structural: list[tuple[int, str]] = []
    for raw in text.splitlines():
        expanded = raw.replace("\u3000", "  ").rstrip()
        stripped = expanded.strip()
        if not stripped:
            pending_wrap.clear()
            continue
        if _HEADER.match(stripped) or _PURE_NUMBER.match(stripped):
            pending_wrap.clear()
            pending_structural.clear()
            continue
        match = _toc_match(expanded)
        if not match:
            # A long TOC title can wrap; keep only plausible short fragments and bind them
            # to the next line that has a dotted page number.
            fragment = clean_title(stripped)
            if 2 <= len(fragment) <= 80 and not re.search(r"[。！？!?]$", fragment):
                item = (len(expanded) - len(expanded.lstrip(" ")), fragment)
                if _STRUCTURAL_HEADER.match(fragment):
                    pending_structural.append(item)
                    pending_structural = pending_structural[-4:]
                    pending_wrap.clear()
                else:
                    pending_wrap.append(item)
                    pending_wrap = pending_wrap[-2:]
            else:
                pending_wrap.clear()
            continue
        indent = len(expanded) - len(expanded.lstrip(" "))
        title = clean_title(match.group("title"))
        page = int(match.group("page"))
        if pending_structural:
            for structural_indent, structural_title in pending_structural:
                entries.append({
                    "title": structural_title,
                    "printed_page": str(page),
                    "level": infer_level(structural_title, structural_indent),
                })
            pending_structural.clear()
        if pending_wrap and title:
            pending_indent = pending_wrap[0][0]
            title = clean_title("".join(fragment for _indent, fragment in pending_wrap) + title)
            indent = min(indent, pending_indent)
        pending_wrap.clear()
        if not (2 <= len(title) <= 140) or page < 1:
            continue
        entries.append(
            {"title": title, "printed_page": str(page), "level": infer_level(title, indent)}
        )
    return entries


def parse_toc(transcripts: dict[int, str], toc_pages: list[int]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for page in toc_pages:
        for entry in parse_toc_page(transcripts.get(page, "")):
            entry["toc_pdf_page"] = page
            combined.append(entry)
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in combined:
        key = (re.sub(r"\s+", "", entry["title"]), entry["printed_page"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def printed_page_map(rows: list[sqlite3.Row]) -> tuple[dict[str, int], int | None]:
    mapping: dict[str, int] = {}
    offsets: Counter[int] = Counter()
    for row in rows:
        printed = str(row["printed_page"] or "").strip()
        if not printed.isdigit():
            continue
        mapping.setdefault(printed, int(row["pdf_page"]))
        offsets[int(row["pdf_page"]) - int(printed)] += 1
    offset = offsets.most_common(1)[0][0] if offsets else None
    return mapping, offset


def resolve_toc_context(
    rows: list[sqlite3.Row], transcripts: dict[int, str], expected: int
) -> tuple[dict[str, int], int | None, int, list[int]]:
    """Return page mapping/body anchor/TOC pages; collection wrappers may override."""
    page_map, offset = printed_page_map(rows)
    first_body = min(
        (int(row["pdf_page"]) for row in rows if str(row["printed_page"] or "") == "1"),
        default=0,
    )
    if not first_body and page_map:
        first_body = min(page_map.values())
    if not first_body:
        raise RuntimeError("no printed-page mapping")
    return page_map, offset, first_body, discover_toc_pages(transcripts, first_body)


def visible_body_headings(
    spec: dict[str, Any],
    rows: list[sqlite3.Row],
    transcripts: dict[int, str],
    toc_pages: list[int],
) -> list[dict[str, Any]]:
    """Collection hook for books without a printed directory.

    The Hegel collection deliberately has no fallback here.  A collection
    wrapper may return only headings literally visible in the source pages;
    generated or summarised headings are not accepted by this builder.
    """
    return []


def find_frontmatter_title_anchor(
    title: str, transcripts: dict[int, str], toc_pages: list[int], first_body_pdf: int
) -> int | None:
    """Resolve a separately numbered preface whose printed page collides with body page 1."""
    title_key = re.sub(r"[^0-9A-Za-z一-鿿]+", "", title)
    if len(title_key) < 4:
        return None
    excluded = set(toc_pages)
    for pdf_page in range(1, first_body_pdf):
        if pdf_page in excluded:
            continue
        lines = [line.strip() for line in transcripts.get(pdf_page, "").splitlines() if line.strip()]
        for line in lines[:4]:
            line_key = re.sub(r"[^0-9A-Za-z一-鿿]+", "", line)
            if line_key == title_key or line_key.startswith(title_key):
                return pdf_page
    return None


def build_volume(
    conn: sqlite3.Connection, spec: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any]:
    volume_id = str(spec["id"])
    book = str(spec["book"])
    volume = int(spec["volume"])
    source_file = str(spec["file"])
    rows = conn.execute(
        "SELECT pdf_page, printed_page, raw_text FROM pages WHERE book=? AND volume=? ORDER BY pdf_page",
        (book, volume),
    ).fetchall()
    expected = int(spec["pages"])
    if len(rows) != expected:
        raise RuntimeError(f"{volume_id}: pages table has {len(rows)}/{expected} rows")
    transcripts = load_transcripts(volume_id)
    if len(transcripts) != expected:
        raise RuntimeError(f"{volume_id}: transcript has {len(transcripts)}/{expected} rows")
    try:
        page_map, offset, first_body, toc_pages = resolve_toc_context(rows, transcripts, expected)
    except RuntimeError as exc:
        raise RuntimeError(f"{volume_id}: {exc}") from exc
    parsed = parse_toc(transcripts, toc_pages)
    toc_source_kind = "printed-directory"
    if not parsed and not toc_pages:
        parsed = visible_body_headings(spec, rows, transcripts, toc_pages)
        if parsed:
            toc_source_kind = "visible-body-headings"
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for entry in parsed:
        # A visible-body-heading hook binds the literal heading to its source
        # page directly.  Printed-directory entries keep the established
        # printed-page mapping path below.
        pdf_page = entry.get("pdf_page")
        if pdf_page is None:
            pdf_page = page_map.get(entry["printed_page"])
        frontmatter_anchor = find_frontmatter_title_anchor(
            entry["title"], transcripts, toc_pages, first_body
        )
        if frontmatter_anchor is not None and (pdf_page is None or pdf_page >= first_body):
            pdf_page = frontmatter_anchor
        if pdf_page is None and offset is not None:
            candidate = int(entry["printed_page"]) + offset
            if 1 <= candidate <= expected:
                pdf_page = candidate
        if pdf_page is None:
            unresolved.append(entry)
            continue
        resolved.append({**entry, "pdf_page": pdf_page})
    if len(resolved) < 3:
        raise RuntimeError(
            f"{volume_id}: only {len(resolved)} navigable TOC entries "
            f"from pages {toc_pages}; refusing to publish"
        )
    if not dry_run:
        conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (book, volume))
        conn.executemany(
            "INSERT INTO toc_entries "
            "(book, volume, source_file, title, pdf_page, printed_page, level, kind, sort_order) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    book,
                    volume,
                    source_file,
                    entry["title"],
                    entry["pdf_page"],
                    entry["printed_page"],
                    entry["level"],
                    "body",
                    index,
                )
                for index, entry in enumerate(resolved, 1)
            ],
        )
    return {
        "id": volume_id,
        "book": book,
        "volume": volume,
        "source_file": source_file,
        "first_body_pdf_page": first_body,
        "printed_page_offset": offset,
        "toc_source_kind": toc_source_kind,
        "toc_source_pdf_pages": toc_pages,
        "parsed_entries": len(parsed),
        "published_entries": len(resolved),
        "unresolved_entries": unresolved,
        "passed": len(resolved) >= 3 and not unresolved,
    }


def write_db_hash(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    path.with_suffix(path.suffix + ".sha256").write_text(
        digest.hexdigest() + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    selected = [row for row in load_catalog() if not args.only or str(row["id"]) in args.only]
    if not selected:
        print("no matching Hegel volumes", file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        reports = [build_volume(conn, spec, dry_run=args.dry_run) for spec in selected]
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    report = {"schema": REPORT_SCHEMA, "volumes": reports, "passed": all(row["passed"] for row in reports)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.dry_run:
        write_db_hash(args.db)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
