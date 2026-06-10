from __future__ import annotations

import re
import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "data" / "dictionary.sqlite"

_NORM_RE = re.compile(r"""[\s·.,，。、；;：:！!？?（）()《》<>\[\]【】"'“”‘’\-—_]+""")


def normalize_term(text: str) -> str:
    return _NORM_RE.sub("", str(text or "")).lower()


def dictionary_available(db_path: Path = DB_PATH) -> bool:
    return db_path.exists() and db_path.is_file()


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def dictionary_stats(db_path: Path = DB_PATH) -> dict:
    if not dictionary_available(db_path):
        return {"entry_count": 0, "letter_count": 0, "needs_review_count": 0}
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS entry_count,
              COUNT(DISTINCT letter) AS letter_count,
              SUM(CASE WHEN needs_review THEN 1 ELSE 0 END) AS needs_review_count
            FROM entries
            """
        ).fetchone()
    return {
        "entry_count": int(row["entry_count"] or 0),
        "letter_count": int(row["letter_count"] or 0),
        "needs_review_count": int(row["needs_review_count"] or 0),
    }


def dictionary_groups(db_path: Path = DB_PATH) -> list[dict]:
    if not dictionary_available(db_path):
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT letter, slug, title, start_page, end_page, needs_review
            FROM entries
            ORDER BY
              CASE WHEN letter GLOB '[A-Z]' THEN 0 ELSE 1 END,
              letter,
              sort_order,
              title
            """
        ).fetchall()
    groups: dict[str, list[dict]] = {}
    for row in rows:
        letter = str(row["letter"] or "#")
        groups.setdefault(letter, []).append(
            {
                "slug": row["slug"],
                "title": row["title"],
                "start_page": int(row["start_page"] or 0),
                "end_page": int(row["end_page"] or row["start_page"] or 0),
                "needs_review": bool(row["needs_review"]),
            }
        )
    return [
        {"letter": letter, "entries": entries, "count": len(entries)}
        for letter, entries in groups.items()
    ]


def dictionary_suggest(query: str, *, limit: int = 20, db_path: Path = DB_PATH) -> list[dict]:
    if not dictionary_available(db_path):
        return []
    raw = " ".join(str(query or "").split())
    norm = normalize_term(raw)
    if len(norm) < 1:
        return []
    like = f"%{norm}%"
    prefix = f"{norm}%"
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT slug, title, letter, start_page, end_page, needs_review, norm_title
            FROM entries
            WHERE norm_title LIKE ?
            ORDER BY
              CASE
                WHEN norm_title = ? THEN 0
                WHEN norm_title LIKE ? THEN 1
                ELSE 2
              END,
              length(title),
              sort_order
            LIMIT ?
            """,
            (like, norm, prefix, int(limit)),
        ).fetchall()
    return [
        {
            "slug": row["slug"],
            "title": row["title"],
            "letter": row["letter"],
            "start_page": int(row["start_page"] or 0),
            "end_page": int(row["end_page"] or row["start_page"] or 0),
            "needs_review": bool(row["needs_review"]),
        }
        for row in rows
    ]


def dictionary_entry(slug: str, db_path: Path = DB_PATH) -> dict | None:
    if not dictionary_available(db_path):
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT slug, title, letter, english, content, start_page, end_page,
                   citation, needs_review, confidence, source_file
            FROM entries
            WHERE slug = ?
            """,
            (slug,),
        ).fetchone()
    if row is None:
        return None
    content = str(row["content"] or "").strip()
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", content) if p.strip()]
    if not paragraphs and content:
        paragraphs = [content]
    return {
        "slug": row["slug"],
        "title": row["title"],
        "letter": row["letter"],
        "english": row["english"],
        "content": content,
        "paragraphs": paragraphs,
        "start_page": int(row["start_page"] or 0),
        "end_page": int(row["end_page"] or row["start_page"] or 0),
        "citation": row["citation"],
        "needs_review": bool(row["needs_review"]),
        "confidence": float(row["confidence"] or 0),
        "source_file": row["source_file"],
    }
