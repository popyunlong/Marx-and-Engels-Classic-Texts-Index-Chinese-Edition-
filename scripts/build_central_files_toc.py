# -*- coding: utf-8 -*-
"""为两套《中共中央文件选集》建立“年份—文献标题”两级目录。

先复用印刷目录的点引线解析与印刷页→PDF 页校验，再从篇首页日期识别年份；识别不到时
只在该册 manifest.date_span 的可信范围内沿用上一条年份。总目录辅助 PDF 不在 manifest
书目中，因此本脚本不会把它写进正文或公开索引。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402
from scripts.build_printed_toc_dots import build_for_volume  # noqa: E402

BOOKS = ("中共中央文件选集（1921—1949）", "中共中央文件选集（1949—1966）")
_ARABIC_YEAR = re.compile(r"(?<!\d)((?:18|19|20)\d{2})\s*年")
_CN_YEAR = re.compile(r"([一二三四五六七八九零〇○]{4})\s*年")
_CN_DIGIT = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
             "六": "6", "七": "7", "八": "8", "九": "9", "零": "0",
             "〇": "0", "○": "0"}
_DATED_ARABIC_YEAR = re.compile(r"[（(]\s*((?:18|19|20)\d{2})\s*年")
_DATED_CN_YEAR = re.compile(r"[（(]\s*([一二三四五六七八九零〇○]{4})\s*年")


def _manifest_spans() -> dict[tuple[str, int], tuple[int, int]]:
    data = yaml.safe_load((ROOT / "config" / "manifest.yaml").read_text(encoding="utf-8")) or {}
    out: dict[tuple[str, int], tuple[int, int]] = {}
    for book in BOOKS:
        for row in data.get(book) or []:
            years = [int(x) for x in re.findall(r"(?:18|19|20)\d{2}", str(row.get("date_span") or ""))]
            if years:
                out[(book, int(row["volume"]))] = (min(years), max(years))
    return out


def _reviewed_base_additions(book: str, volume: int) -> list[dict]:
    """Load visually verified entries that OCR could not recover from a damaged TOC page."""
    path = ROOT / "config" / "new_corpus_corrections_202609.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[dict] = []
    for item in payload.get("toc_base_additions") or []:
        if str(item.get("book")) != book or int(item.get("volume") or 0) != volume:
            continue
        title = str(item.get("title") or "").strip()
        printed = int(item["printed_page"])
        pdf_page = int(item["pdf_page"])
        if not title or printed < 1 or pdf_page < 1:
            raise RuntimeError(f"非法目录补录：{book} v{volume}")
        out.append({"title": title, "pdf_page": pdf_page, "printed_page": str(printed),
                    "level": 1, "kind": "body"})
    return out


def _year(text: str) -> int | None:
    match = _ARABIC_YEAR.search(text or "")
    if match:
        return int(match.group(1))
    match = _CN_YEAR.search(text or "")
    if match:
        return int("".join(_CN_DIGIT[c] for c in match.group(1)))
    return None


def _dated_year(text: str) -> int | None:
    """优先取题名附近括注的成文日期，避免正文提及的历史年份抢占目录年份。"""
    match = _DATED_ARABIC_YEAR.search(text or "")
    if match:
        return int(match.group(1))
    match = _DATED_CN_YEAR.search(text or "")
    if match:
        return int("".join(_CN_DIGIT[c] for c in match.group(1)))
    return None


def _hierarchy(entries: list[dict], raw_by_page: dict[int, str], allowed: tuple[int, int]) -> list[dict]:
    result: list[dict] = []
    current: int | None = None
    lo, hi = allowed
    for entry in entries:
        page_head = "\n".join((raw_by_page.get(int(entry["pdf_page"])) or "").splitlines()[:14])
        title = str(entry.get("title") or "")
        detected = (_dated_year(title) or _year(title) or
                    _dated_year(page_head) or _year(page_head))
        if detected is None or not (lo <= detected <= hi) or (current is not None and detected < current):
            detected = current if current is not None else lo
        if detected != current:
            result.append({
                "title": f"{detected}年", "pdf_page": int(entry["pdf_page"]),
                "printed_page": entry.get("printed_page"), "level": 1, "kind": "year",
            })
            current = detected
        result.append({**entry, "level": 2, "kind": "body"})
    return result


def _update_hash(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    path.with_suffix(path.suffix + ".sha256").write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="构建中央文件选集年份—篇名两级目录")
    parser.add_argument("--book", action="append", choices=BOOKS)
    parser.add_argument("--volumes", nargs="*", type=int)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    targets = args.book or list(BOOKS)
    spans = _manifest_spans()
    changed = 0
    with sqlite3.connect(args.db) as conn:
        for book in targets:
            have = [int(r[0]) for r in conn.execute(
                "SELECT DISTINCT volume FROM pages WHERE book=? ORDER BY volume", (book,)
            )]
            vols = args.volumes if args.volumes and len(targets) == 1 else have
            for volume in vols:
                if volume not in have:
                    continue
                allowed = spans.get((book, volume))
                if allowed is None:
                    raise SystemExit(f"{book} 第{volume}册缺少 manifest.date_span，拒绝猜测年份")
                base, note = build_for_volume(conn, book, volume)
                additions = _reviewed_base_additions(book, volume)
                if additions:
                    known = {(str(x.get("title")), str(x.get("printed_page"))) for x in base}
                    base = [x for x in additions if (x["title"], x["printed_page"]) not in known] + base
                raw_by_page = {int(p): str(t or "") for p, t in conn.execute(
                    "SELECT pdf_page, raw_text FROM pages WHERE book=? AND volume=?", (book, volume)
                )}
                entries = _hierarchy(base, raw_by_page, allowed)
                years = [e["title"] for e in entries if e["kind"] == "year"]
                print(f"[{book} 第{volume}册] {note}；年份层 {years or ['—']}；总条目 {len(entries)}")
                if args.dry_run or not base:
                    continue
                source_file = conn.execute(
                    "SELECT source_file FROM pages WHERE book=? AND volume=? LIMIT 1", (book, volume)
                ).fetchone()[0]
                conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (book, volume))
                conn.executemany(
                    "INSERT INTO toc_entries(book,volume,source_file,title,pdf_page,printed_page,level,kind,sort_order) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [(book, volume, source_file, e["title"], e["pdf_page"], e.get("printed_page"),
                      e["level"], e["kind"], i) for i, e in enumerate(entries, 1)],
                )
                changed += len(entries)
        if not args.dry_run:
            conn.commit()
    if not args.dry_run:
        _update_hash(args.db)
    print(("[dry-run] " if args.dry_run else "") + f"目录条目 {changed}")


if __name__ == "__main__":
    main()
