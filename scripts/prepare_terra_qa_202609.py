# -*- coding: utf-8 -*-
"""Create deterministic Terra visual-QA samples and PDF-page contact sheets."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import fitz
import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_new_corpus_202609 import NEW_BOOKS, manifest_rows  # noqa: E402

DEFAULT_DB = ROOT / "tmp" / "new_corpus_202609" / "corpus.sqlite"
DEFAULT_OUT = ROOT / "tmp" / "new_corpus_202609" / "terra_qa"
SPECIAL_RE = re.compile(
    r"(?:图书在版编目|\bCIP\b|\bISBN\b|版权页|责任编辑|责任校对|出版发行|"
    r"版次|印次|印刷厂|开本|字数|定价|目\s*录|目\s*次)", re.I
)
YEAR_RE = re.compile(r"(?<!\d)(?:18|19|20)\d{2}年")


def _manifest_index() -> dict[tuple[str, int], dict]:
    return {(book, int(row["volume"])): row for book, row in manifest_rows()}


def _sidecar_path(book: str, volume: int) -> Path:
    prefixes = {
        "刘少奇年谱": "liu_np", "刘少奇选集": "liu_xuan",
        "中共中央文件选集（1921—1949）": "party_files_1921",
        "中共中央文件选集（1949—1966）": "party_files_1949",
    }
    if book in prefixes:
        return ROOT / "data" / f"{prefixes[book]}_vol{volume:02d}_glm.jsonl"
    reviewed = yaml.safe_load((ROOT / "config" / "western_marxism_sources.yaml").read_text(encoding="utf-8")) or {}
    for row in reviewed.get("books") or []:
        if row.get("key") == book and int(row.get("volume") or 1) == volume:
            digest = hashlib.sha1(book.encode("utf-8")).hexdigest()[:12]
            return ROOT / "data" / "western_marxism" / f"{digest}.jsonl"
    raise KeyError((book, volume))


def _sidecar_flags() -> dict[tuple[str, int, int], list[str]]:
    out: dict[tuple[str, int, int], list[str]] = defaultdict(list)
    for book, row in manifest_rows():
        volume = int(row["volume"])
        path = _sidecar_path(book, volume)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                page = int(item["pdf_page"])
            except Exception:
                continue
            source = str(item.get("src") or "")
            if source.startswith("rapidocr"):
                out[(book, volume, page)].append(source)
            if source == "glm-verified":
                out[(book, volume, page)].append(source)
            if item.get("blank"):
                out[(book, volume, page)].append(source or "blank")
            if item.get("suspect"):
                out[(book, volume, page)].append("suspect:" + str(item["suspect"]))
            if item.get("trunc") or item.get("finish") == "length":
                out[(book, volume, page)].append("truncated")
    return out


def _even_pages(pages: list[int], count: int) -> set[int]:
    if not pages or count <= 0:
        return set()
    if count >= len(pages):
        return set(pages)
    if count == 1:
        return {pages[len(pages) // 2]}
    return {pages[round(i * (len(pages) - 1) / (count - 1))] for i in range(count)}


def make_manifest(db: Path) -> dict:
    sources = _manifest_index()
    flags = _sidecar_flags()
    reasons: dict[tuple[str, int, int], set[str]] = defaultdict(set)
    with sqlite3.connect(db) as conn:
        for book, row in manifest_rows():
            volume = int(row["volume"])
            records = conn.execute(
                "SELECT pdf_page,raw_text FROM pages WHERE book=? AND volume=? ORDER BY pdf_page",
                (book, volume),
            ).fetchall()
            nonempty = [int(page) for page, text in records if str(text or "").strip()]
            sample_n = max(5, math.ceil(int(row["page_count"]) * 0.01))
            for page in _even_pages(nonempty, sample_n):
                reasons[(book, volume, page)].add("fixed-1-percent")
            for page, text in records:
                text = str(text or "")
                if SPECIAL_RE.search(text[:3000]):
                    reasons[(book, volume, int(page))].add("copyright-cip-or-printed-toc")
            # Every year boundary and the first/last chapter landing exercise direct navigation.
            toc = conn.execute(
                "SELECT pdf_page,kind,title FROM toc_entries WHERE book=? AND volume=? ORDER BY sort_order",
                (book, volume),
            ).fetchall()
            if toc:
                for page, kind, title in (toc[0], toc[-1]):
                    reasons[(book, volume, int(page))].add("toc-first-or-last-landing")
                for page, kind, title in toc:
                    if str(kind or "") == "year" or YEAR_RE.search(str(title or "")):
                        reasons[(book, volume, int(page))].add("year-boundary-landing")
        for key, values in flags.items():
            for value in values:
                reasons[key].add("anomaly-or-fallback:" + value)

        items = []
        for sequence, ((book, volume, page), why) in enumerate(sorted(reasons.items()), 1):
            source_row = sources[(book, volume)]
            dbrow = conn.execute(
                "SELECT printed_page,raw_text FROM pages WHERE book=? AND volume=? AND pdf_page=?",
                (book, volume, page),
            ).fetchone()
            if not dbrow:
                raise RuntimeError(f"QA落点缺页：{book} v{volume} p{page}")
            items.append({
                "id": f"Q{sequence:05d}", "book": book, "volume": volume,
                "pdf_page": page, "printed_page": dbrow[0],
                "source": str(source_row.get("source") or source_row["file"]),
                "reasons": sorted(why), "ocr_text": str(dbrow[1] or ""),
                "mandatory_visual": any(
                    x.startswith(("copyright", "anomaly")) for x in why
                ),
            })
    by_reason: dict[str, int] = defaultdict(int)
    for item in items:
        for reason in item["reasons"]:
            by_reason[reason.split(":", 1)[0]] += 1
    return {"version": 1, "database": str(db), "count": len(items),
            "by_reason": dict(sorted(by_reason.items())), "items": items}


def _font(size: int):
    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def render(manifest: dict, out_dir: Path, *, columns: int = 3, rows: int = 3) -> list[str]:
    page_dir = out_dir / "pages"
    sheet_dir = out_dir / "sheets"
    page_dir.mkdir(parents=True, exist_ok=True)
    sheet_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[tuple[dict, Path]] = []
    docs: dict[str, fitz.Document] = {}
    try:
        for item in manifest["items"]:
            source = str(item["source"])
            doc = docs.setdefault(source, fitz.open(ROOT / source))
            pix = doc[int(item["pdf_page"]) - 1].get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
            path = page_dir / f'{item["id"]}-v{int(item["volume"]):02d}-p{int(item["pdf_page"]):04d}.jpg'
            pix.save(path)
            item["image"] = str(path.relative_to(out_dir)).replace("\\", "/")
            rendered.append((item, path))
    finally:
        for doc in docs.values():
            doc.close()

    cell_w, cell_h, label_h = 1050, 1450, 54
    font = _font(30)
    per_sheet = columns * rows
    sheets: list[str] = []
    for offset in range(0, len(rendered), per_sheet):
        batch = rendered[offset:offset + per_sheet]
        canvas = Image.new("RGB", (columns * cell_w, rows * (cell_h + label_h)), "white")
        draw = ImageDraw.Draw(canvas)
        for index, (item, path) in enumerate(batch):
            image = Image.open(path).convert("RGB")
            image.thumbnail((cell_w - 20, cell_h - 20), Image.Resampling.LANCZOS)
            x = (index % columns) * cell_w + (cell_w - image.width) // 2
            y0 = (index // columns) * (cell_h + label_h)
            y = y0 + label_h + (cell_h - image.height) // 2
            canvas.paste(image, (x, y))
            draw.text(((index % columns) * cell_w + 14, y0 + 10),
                      f'{item["id"]}  v{item["volume"]}  pdf {item["pdf_page"]}', fill="black", font=font)
        path = sheet_dir / f"sheet-{offset // per_sheet + 1:04d}.jpg"
        canvas.save(path, quality=92, optimize=True)
        sheets.append(str(path.relative_to(out_dir)).replace("\\", "/"))
    return sheets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    payload = make_manifest(args.db)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.render:
        payload["sheets"] = render(payload, args.output)
    target = args.output / "manifest.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(target), "count": payload["count"],
                      "sheets": len(payload.get("sheets") or [])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
