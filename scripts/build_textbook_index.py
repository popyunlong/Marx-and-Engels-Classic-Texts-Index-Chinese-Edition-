# -*- coding: utf-8 -*-
"""把某个「文本层 PDF」书库的 pages 行就地写入既有 corpus.sqlite（定向、不全量重建）。

适用于正文可选择（有文本层）的书库，如《毛泽东文集》——正文/页码直接从 PDF 文本层抽取，
逻辑与 build_index 的逐页循环一致（detect_printed_page + fill_missing + normalize），
但只 DELETE/INSERT 指定 book 的 pages 行，绝不 DROP 全表、绝不触碰其它书库。

目录（toc_entries）请在本脚本之后运行 `python scripts/build_toc.py --book <KEY>`，
它会从 PDF 书签生成该书目录（同样是定向写入）。

用法：python scripts/build_textbook_index.py --book 毛泽东文集
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path

import fitz  # pymupdf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from book_config import book_config_map  # noqa: E402
from build_index import (  # noqa: E402
    BUILD_DB_PATH,
    detect_printed_page_from_page,
    fill_missing_printed_pages,
    normalize,
)

MANIFEST = ROOT / "config" / "manifest.yaml"
HASH_PATH = BUILD_DB_PATH.with_suffix(BUILD_DB_PATH.suffix + ".sha256")


def _looks_per_char(raw: str) -> bool:
    """文本层是否为「逐字一行」式（如《邓小平文选》第3卷：每个汉字单独成行）。"""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if len(lines) < 50:
        return False
    avg = sum(len(l) for l in lines) / len(lines)
    return avg < 2.5


def reflow_char_lines(page) -> str:
    """按 span 中心 y 坐标把逐字 span 重组为视觉行（保持提取顺序，不按 x 重排）。

    仅用于逐字一行的文本层；正常文本层直接用 get_text("text") 原文。
    """
    d = page.get_text("dict")
    out_lines: list[str] = []
    cur: list[str] = []
    cur_y: float | None = None
    for blk in d.get("blocks", []):
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                t = (sp.get("text") or "").strip()
                if not t:
                    continue
                y0, y1 = sp["bbox"][1], sp["bbox"][3]
                yc = (y0 + y1) / 2
                h = max(1.0, y1 - y0)
                if cur_y is None or abs(yc - cur_y) <= h * 0.6:
                    cur.append(t)
                    cur_y = yc if cur_y is None else (cur_y * 0.8 + yc * 0.2)
                else:
                    out_lines.append("".join(cur))
                    cur = [t]
                    cur_y = yc
    if cur:
        out_lines.append("".join(cur))
    return "\n".join(out_lines)


def update_hash() -> None:
    digest = hashlib.sha256()
    with BUILD_DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def build_book(book: str) -> int:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    items = manifest.get(book) or []
    if not items:
        raise SystemExit(f"manifest.yaml 中未找到 {book} 条目。")

    all_rows = []
    for item in sorted(items, key=lambda x: x.get("volume", 0)):
        vol = item.get("volume")
        if not isinstance(vol, int):
            print(f"跳过 {item.get('file')}：volume 未填写", file=sys.stderr)
            continue
        source_file = str(Path(item["file"]).as_posix())
        pdf_path = (ROOT / source_file).resolve()
        if not pdf_path.exists():
            raise SystemExit(f"PDF 缺失：{pdf_path}")

        t0 = time.time()
        rows = []
        detected = 0
        empty = 0
        with fitz.open(pdf_path) as doc:
            for i, page in enumerate(doc, start=1):
                raw = page.get_text("text")
                if _looks_per_char(raw):
                    raw = reflow_char_lines(page)
                printed = detect_printed_page_from_page(page)
                if printed and not printed.startswith("pre-"):
                    detected += 1
                norm = normalize(raw)
                if not norm:
                    empty += 1
                rows.append((book, vol, source_file, i, printed, raw, norm))
        filled = fill_missing_printed_pages(rows)
        all_rows.extend(rows)
        print(f"  [第{vol}卷] {pdf_path.name} 页={len(rows)} 直接识别={detected} 补全={filled} "
              f"空文本页={empty} 耗时={time.time()-t0:.1f}s")

    conn = sqlite3.connect(str(BUILD_DB_PATH))
    try:
        conn.execute("DELETE FROM pages WHERE book = ?", (book,))
        conn.executemany(
            "INSERT INTO pages (book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            all_rows,
        )
        conn.commit()
    finally:
        conn.close()
    update_hash()
    return len(all_rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="定向写入文本层书库的 pages 行。")
    ap.add_argument("--book", required=True, help="书库 key（须在 books.yaml/manifest.yaml 中）")
    args = ap.parse_args()
    if args.book not in book_config_map():
        raise SystemExit(f"未知书库：{args.book}")
    n = build_book(args.book)
    print(f"\n已写入 pages {n} 行（book={args.book}）。请接着运行："
          f"\n  python scripts/build_toc.py --book {args.book}")


if __name__ == "__main__":
    main()
