from __future__ import annotations

"""Repair page-bottom footnotes that were merged into journal body paragraphs.

The repair is deliberately scoped to one unlocked issue. It preserves every
unchanged paragraph and translation, re-translates only paragraphs that must be
split, and writes a database/document backup before replacing any live file.
Run without ``--apply`` for a read-only report.
"""

import argparse
import copy
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import journal_alerts as ja  # noqa: E402
import journal_fulltext as jf  # noqa: E402
from journal_storage import JOURNAL_BACKUP_DIR, JOURNAL_DB_PATH  # noqa: E402


LOCKED_ISSUE_STATUSES = {"published", "sent", "archived"}


def _note_fragments(pdf_path: Path) -> list[dict[str, Any]]:
    extracted = jf.extract_paragraphs(pdf_path)
    return [
        dict(paragraph)
        for paragraph in extracted.get("paragraphs") or []
        if paragraph.get("kind") == "footnote"
        and str(paragraph.get("text") or "").strip()
    ]


def _matches_for_paragraph(text: str, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    # Prefer longer blocks if a publisher exposes both a container and a child.
    for fragment in sorted(
        fragments, key=lambda item: len(str(item.get("text") or "")), reverse=True
    ):
        needle = str(fragment.get("text") or "")
        start = text.find(needle)
        while start >= 0:
            end = start + len(needle)
            if not any(start < used_end and end > used_start for used_start, used_end in occupied):
                matches.append({"start": start, "end": end, "fragment": fragment})
                occupied.append((start, end))
                break
            start = text.find(needle, start + 1)
    return sorted(matches, key=lambda item: (int(item["start"]), int(item["end"])))


def split_embedded_footnotes(
    paragraphs: list[dict[str, Any]], fragments: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Split only exact, layout-confirmed note substrings from body paragraphs."""
    repaired: list[dict[str, Any]] = []
    stats = {
        "matched_fragments": 0,
        "reclassified_paragraphs": 0,
        "split_paragraphs": 0,
        "translation_segments": 0,
    }
    remaining = list(fragments)
    for original in paragraphs:
        paragraph = copy.deepcopy(original)
        text = str(paragraph.get("text") or "")
        if paragraph.get("kind") != "body" or not text:
            repaired.append(paragraph)
            continue
        matches = _matches_for_paragraph(text, remaining)
        if not matches:
            repaired.append(paragraph)
            continue
        matched_ids = {id(match["fragment"]) for match in matches}
        remaining = [fragment for fragment in remaining if id(fragment) not in matched_ids]
        stats["matched_fragments"] += len(matches)

        uncovered = []
        cursor = 0
        for match in matches:
            uncovered.append(text[cursor : int(match["start"])])
            cursor = int(match["end"])
        uncovered.append(text[cursor:])
        if not "".join(uncovered).strip():
            paragraph["kind"] = "footnote"
            paragraph.pop("level", None)
            stats["reclassified_paragraphs"] += 1
            repaired.append(paragraph)
            continue

        stats["split_paragraphs"] += 1
        cursor = 0
        pieces: list[dict[str, Any]] = []
        for match in matches:
            start = int(match["start"])
            end = int(match["end"])
            before = text[cursor:start].strip()
            if before:
                body = copy.deepcopy(paragraph)
                body["text"] = before
                body["kind"] = "body"
                body["zh"] = ""
                body.pop("level", None)
                pieces.append(body)
                stats["translation_segments"] += 1
            note = copy.deepcopy(paragraph)
            note["text"] = text[start:end].strip()
            note["kind"] = "footnote"
            note["page"] = int(match["fragment"].get("page") or note.get("page") or 1)
            note["zh"] = ""
            note.pop("level", None)
            pieces.append(note)
            stats["translation_segments"] += 1
            cursor = end
        after = text[cursor:].strip()
        if after:
            body = copy.deepcopy(paragraph)
            body["text"] = after
            body["kind"] = "body"
            body["zh"] = ""
            body.pop("level", None)
            pieces.append(body)
            stats["translation_segments"] += 1

        # Adjacent layout blocks belonging to one note should render as one note,
        # and need only one translation call.
        coalesced: list[dict[str, Any]] = []
        for piece in pieces:
            if coalesced and piece.get("kind") == coalesced[-1].get("kind") == "footnote":
                coalesced[-1]["text"] = (
                    f"{str(coalesced[-1].get('text') or '').strip()} "
                    f"{str(piece.get('text') or '').strip()}"
                ).strip()
                stats["translation_segments"] -= 1
            else:
                coalesced.append(piece)
        repaired.extend(coalesced)
    return repaired, stats


def _backup(issue_id: int, article_ids: list[int]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = JOURNAL_BACKUP_DIR / f"footnote-repair-issue-{issue_id}-{stamp}"
    (root / "articles").mkdir(parents=True, exist_ok=False)
    source = sqlite3.connect(JOURNAL_DB_PATH)
    target = sqlite3.connect(root / "journal.sqlite3")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    for article_id in article_ids:
        src = jf.article_dir(article_id) / "doc.json"
        if src.exists():
            destination = root / "articles" / str(article_id)
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, destination / "doc.json")
    return root


def repair_issue(issue_id: int, *, apply: bool = False) -> dict[str, Any]:
    issue = ja.get_batch(int(issue_id))
    if not issue:
        raise RuntimeError(f"journal issue {issue_id} does not exist")
    status = str(issue.get("status") or "")
    if status in LOCKED_ISSUE_STATUSES:
        raise RuntimeError(f"journal issue {issue_id} is locked ({status})")

    articles = ja.public_batch_articles(int(issue_id))
    changes: list[dict[str, Any]] = []
    prepared: list[tuple[int, dict[str, Any], dict[str, int]]] = []
    for article in articles:
        article_id = int(article["id"])
        pdf_path = jf.article_dir(article_id) / "source.pdf"
        document = jf.load_document(article_id)
        if not document or not pdf_path.exists():
            continue
        fragments = _note_fragments(pdf_path)
        repaired, stats = split_embedded_footnotes(document.get("paragraphs") or [], fragments)
        if not (stats["reclassified_paragraphs"] or stats["split_paragraphs"]):
            continue
        payload = copy.deepcopy(document)
        payload["paragraphs"] = repaired
        changes.append({"article_id": article_id, **stats})
        prepared.append((article_id, payload, stats))

    report: dict[str, Any] = {
        "issue_id": int(issue_id),
        "issue_status": status,
        "articles_scanned": len(articles),
        "articles_changed": len(prepared),
        "changes": changes,
        "applied": False,
    }
    if not apply or not prepared:
        return report

    backup_root = _backup(int(issue_id), [article_id for article_id, _doc, _stats in prepared])
    completed_payloads: list[tuple[int, dict[str, Any], int, int]] = []
    for article_id, payload, _stats in prepared:
        paragraphs = payload.get("paragraphs") or []
        jf.translate_paragraphs(paragraphs, "en", article_id=article_id)
        required, completed = jf._translation_requirements(paragraphs)
        if required <= 0 or completed != required:
            raise RuntimeError(
                f"article {article_id} repair translation incomplete: {completed}/{required}"
            )
        now = ja.utc_now_text()
        payload["generated_at"] = now
        history = list(payload.get("repair_history") or [])
        history.append(
            {
                "kind": "layout-footnote-repair-v1",
                "repaired_at": now,
                "backup": str(backup_root),
            }
        )
        payload["repair_history"] = history
        completed_payloads.append((article_id, payload, required, completed))

    # No live document is replaced until every changed segment has a complete
    # translation. An upstream outage therefore leaves the issue untouched.
    for article_id, payload, required, completed in completed_payloads:
        paragraphs = payload.get("paragraphs") or []
        jf._save_document(article_id, payload)
        jf._upsert_state(
            article_id,
            para_count=len(paragraphs),
            translated=completed,
            required_translations=required,
            error="",
        )
    report["applied"] = True
    report["backup"] = str(backup_root)
    (backup_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair merged journal footnotes in one unlocked issue")
    parser.add_argument("--issue-id", type=int, default=0, help="Issue id; defaults to the current issue")
    parser.add_argument("--apply", action="store_true", help="Back up and apply the repair")
    args = parser.parse_args()
    issue = ja.get_batch(args.issue_id) if args.issue_id else ja.current_batch()
    if not issue:
        raise SystemExit("no current journal issue")
    report = repair_issue(int(issue["id"]), apply=bool(args.apply))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
