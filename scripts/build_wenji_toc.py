from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any

import fitz
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402
from search import Corpus  # noqa: E402


OVERRIDES_PATH = ROOT / "config" / "wenji_toc_overrides.yaml"
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

BODY_START_RE = re.compile(r"^第[一二三四五六七八九十]+卷说明$")
EDITORIAL_RE = re.compile(r"^第[一二三四五六七八九十]+卷编审人员$")
LETTER_NUMBER_RE = re.compile(r"^\d+[．.、]\s*")

NOISE_TITLES = {
    "封面",
    "书名页",
    "版权页",
    "编辑说明",
    "目录",
    "插图目录",
    "马克思像",
    "恩格斯像",
    "文集编审人员",
    "《马克思恩格斯文集》编审委员会",
    "马研建工程经典作家重点著作译文审核和修订课题组",
}


def clean_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()


def is_body_start(title: str) -> bool:
    return bool(BODY_START_RE.match(title) or title.startswith("正文"))


def is_end_matter(title: str) -> bool:
    return title == "文集编审人员" or EDITORIAL_RE.match(title) is not None


def is_noise(title: str) -> bool:
    if title in NOISE_TITLES:
        return True
    if title.startswith("马研建工程经典作家重点著作译文审核"):
        return True
    if title.startswith("《马克思恩格斯文集》编审委员会"):
        return True
    return False


def entry_kind(title: str) -> str:
    if is_body_start(title) or title.startswith("正文"):
        return "section"
    if re.fullmatch(r"\d{4}年", title):
        return "year"
    if LETTER_NUMBER_RE.match(title):
        return "letter"
    return "body"


def load_overrides() -> dict[str, Any]:
    if not OVERRIDES_PATH.exists():
        return {}
    payload = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def apply_overrides(entries: list[dict[str, Any]], volume: int, overrides: dict[str, Any]) -> list[dict[str, Any]]:
    by_volume = ((overrides.get("文集") or {}).get(volume) or {})
    remove_titles = {clean_title(item) for item in by_volume.get("remove_titles") or []}
    if remove_titles:
        entries = [entry for entry in entries if entry["title"] not in remove_titles]

    replace_entries = by_volume.get("entries")
    if isinstance(replace_entries, list) and replace_entries:
        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(replace_entries, start=1):
            if not isinstance(item, dict):
                continue
            title = clean_title(str(item.get("title") or ""))
            try:
                pdf_page = int(item.get("pdf_page") or 0)
            except (TypeError, ValueError):
                pdf_page = 0
            if not title or pdf_page < 1:
                continue
            normalized.append(
                {
                    "title": title,
                    "pdf_page": pdf_page,
                    "printed_page": str(item.get("printed_page") or "") or None,
                    "level": max(1, int(item.get("level") or 1)),
                    "kind": str(item.get("kind") or entry_kind(title)),
                    "sort_order": idx,
                }
            )
        if normalized:
            return normalized
    return entries


def build_entries_for_volume(volume_obj, overrides: dict[str, Any]) -> list[dict[str, Any]]:
    pdf_path = ROOT / volume_obj.source_file
    printed_by_pdf = {page.pdf_page: page.printed_page for page in volume_obj.pages}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    body_started = False

    with fitz.open(pdf_path) as doc:
        for raw_level, raw_title, raw_page in doc.get_toc():
            title = clean_title(raw_title)
            try:
                pdf_page = int(raw_page)
            except (TypeError, ValueError):
                continue
            if not title or pdf_page < 1 or pdf_page > doc.page_count:
                continue

            if is_body_start(title):
                body_started = True
            if not body_started:
                continue
            if is_end_matter(title):
                break
            if is_noise(title):
                continue

            key = (pdf_page, title)
            if key in seen:
                continue
            seen.add(key)
            level = max(1, min(6, int(raw_level or 1)))
            rows.append(
                {
                    "title": title,
                    "pdf_page": pdf_page,
                    "printed_page": printed_by_pdf.get(pdf_page),
                    "level": level,
                    "kind": entry_kind(title),
                    "sort_order": len(rows) + 1,
                }
            )

    rows = apply_overrides(rows, volume_obj.volume, overrides)
    for idx, row in enumerate(rows, start=1):
        row["sort_order"] = idx
    return rows


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS toc_entries (
            book         TEXT    NOT NULL,
            volume       INTEGER NOT NULL,
            source_file  TEXT    NOT NULL,
            title        TEXT    NOT NULL,
            pdf_page     INTEGER NOT NULL,
            printed_page TEXT,
            level        INTEGER NOT NULL,
            kind         TEXT    NOT NULL,
            sort_order   INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_toc_entries_source_order "
        "ON toc_entries(source_file, sort_order)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_toc_entries_book_volume "
        "ON toc_entries(book, volume, sort_order)"
    )


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    corpus = Corpus.load_default()
    overrides = load_overrides()
    volumes = corpus.get_volumes("文集")
    if not volumes:
        raise SystemExit("没有找到《文集》卷册，无法生成目录。")

    payload: list[tuple[Any, ...]] = []
    summary: list[str] = []
    for volume_obj in volumes:
        entries = build_entries_for_volume(volume_obj, overrides)
        if not entries:
            raise SystemExit(f"第 {volume_obj.volume} 卷没有生成目录项，请检查 PDF 书签或覆盖配置。")
        summary.append(f"文集第 {volume_obj.volume} 卷：{len(entries)} 项")
        for entry in entries:
            payload.append(
                (
                    "文集",
                    volume_obj.volume,
                    volume_obj.source_file,
                    entry["title"],
                    entry["pdf_page"],
                    entry.get("printed_page"),
                    entry["level"],
                    entry["kind"],
                    entry["sort_order"],
                )
            )

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_schema(conn)
        conn.execute("DELETE FROM toc_entries WHERE book = ?", ("文集",))
        conn.executemany(
            """
            INSERT INTO toc_entries (
                book, volume, source_file, title, pdf_page,
                printed_page, level, kind, sort_order
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        conn.commit()
    finally:
        conn.close()

    update_hash()
    print("\n".join(summary))
    print(f"已写入 {len(payload)} 条《文集》目录。")


if __name__ == "__main__":
    main()
