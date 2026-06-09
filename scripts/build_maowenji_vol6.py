# -*- coding: utf-8 -*-
"""把《毛泽东文集》第6卷（扫描件，无文本层）就地写入既有 corpus.sqlite。

正文取自本地 OCR 产物 data/maowenji_vol6_ocr.jsonl（由 scripts/_ocr_maowenji_vol6.py 生成，
服务器无 OCR 依赖、改为上传该 sidecar 后在服务器读取注入）。印刷页码取自 6.pdf 的“页码书签”
（标题为纯数字 → 印刷页，dest → pdf 页；等价于 printed=pdf_page-15）。

第6卷书签是页码不是章节，故目录由 OCR 正文“篇名+（一九××年…日）”识别篇首页推导（best-effort，
单调修复）。只 DELETE/INSERT book='毛泽东文集' 且 volume=6 的 pages/toc_entries 行，
其余卷与书库不动；完成重算 sha256。须在 build_textbook_index + build_toc 之后运行（覆盖其对
第6卷产生的空行/数字书签垃圾目录）。

用法：python scripts/build_maowenji_vol6.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import BUILD_DB_PATH, normalize  # noqa: E402

BOOK = "毛泽东文集"
VOL = 6
PDF = ROOT / "pdfs" / "《毛泽东文集》" / "6.pdf"
SIDECAR = ROOT / "data" / "maowenji_vol6_ocr.jsonl"
SOURCE_FILE = "pdfs/《毛泽东文集》/6.pdf"
HASH_PATH = BUILD_DB_PATH.with_suffix(BUILD_DB_PATH.suffix + ".sha256")

# 篇首页特征：开头(去页码后)即「篇名（一九××年…日/月）」。日期括注 OCR 偶有误差，放宽。
_DATE = re.compile(r"[（(]\s*[一二三四五六七八九十百零〇○两\d]{2,}\s*年[^）)]{0,16}?[日月][^）)]{0,4}[)）]")
_LEAD_NUM = re.compile(r"^[\s\d]+")


def load_sidecar() -> dict[int, str]:
    if not SIDECAR.exists():
        raise SystemExit(f"缺少 OCR sidecar：{SIDECAR}（请先本地跑 scripts/_ocr_maowenji_vol6.py 并上传）")
    texts: dict[int, str] = {}
    for line in SIDECAR.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        texts[int(o["pdf_page"])] = o.get("text") or ""
    return texts


def printed_map() -> dict[int, str]:
    doc = fitz.open(PDF)
    m = {t[2]: t[1].replace("\x00", "").strip() for t in doc.get_toc()
         if t[1].replace("\x00", "").strip().isdigit() and t[2] > 0}
    n = doc.page_count
    doc.close()
    return m, n


def detect_chapter(text: str) -> str | None:
    """若该页为篇首（开头即 标题+日期），返回篇名；否则 None。"""
    flat = re.sub(r"\s+", "", text)[:60]
    flat = _LEAD_NUM.sub("", flat)  # 去掉行首印刷页码
    m = _DATE.search(flat)
    if not m or m.start() < 2 or m.start() > 40:
        return None
    title = flat[:m.start()].strip()
    # 标题不应含句末标点/破折号正文特征
    if any(ch in title for ch in "。！？；，、"):
        return None
    if 2 <= len(title) <= 40:
        return title
    return None


def main() -> None:
    texts = load_sidecar()
    pmap, n = printed_map()
    if not texts:
        raise SystemExit("OCR sidecar 为空。")

    rows = []
    toc = []
    for i in range(1, n + 1):
        raw = texts.get(i, "")
        printed = pmap.get(i) or (str(i - 15) if i > 15 else None)
        rows.append((BOOK, VOL, SOURCE_FILE, i, printed, raw, normalize(raw)))
        if printed:  # 仅正文页参与目录识别
            title = detect_chapter(raw)
            if title:
                toc.append({"title": title, "pdf_page": i, "printed": printed})

    # 目录单调修复（页码必非递减；按阅读顺序）+ 去重相邻同名
    toc.sort(key=lambda e: e["pdf_page"])
    dedup = []
    for e in toc:
        if dedup and dedup[-1]["title"] == e["title"]:
            continue
        dedup.append(e)

    conn = sqlite3.connect(str(BUILD_DB_PATH))
    try:
        conn.execute("DELETE FROM pages WHERE book=? AND volume=?", (BOOK, VOL))
        conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (BOOK, VOL))
        conn.executemany(
            "INSERT INTO pages (book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text) "
            "VALUES (?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, printed_page, level, kind, sort_order) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(BOOK, VOL, SOURCE_FILE, e["title"], e["pdf_page"], e["printed"], 1, "body", k + 1)
             for k, e in enumerate(dedup)])
        conn.commit()
    finally:
        conn.close()
    update_hash()
    nonempty = sum(1 for r in rows if r[6])
    print(f"第6卷：pages {len(rows)} 行（非空 OCR {nonempty}）、目录 {len(dedup)} 条已写入。")


def update_hash() -> None:
    digest = hashlib.sha256()
    with BUILD_DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
