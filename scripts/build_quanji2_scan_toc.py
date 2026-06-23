# -*- coding: utf-8 -*-
"""为《全集二版》7 个垃圾书签扫描卷(16/30/32/34/39/40/48)从 **OCR 正文里的印刷目录页**
解析章节目录，写入 corpus.sqlite 的 toc_entries（替换这些卷原有的极简目录）。

这些卷 PDF 书签是页码锚点/tif 文件名/无，故 build_toc 拿不到章节；但 Phase2 注入后
pages.raw_text 已有 OCR 正文，其前置「目录」页是规整的「标题……起页[-止页]」列表
（书信卷=逐封信、手稿卷=各节）。本脚本据此解析：
  - 定位目录页：前 ~130 个 pdf 页中，首非空行为「目录」的页起，连续收集仍像目录的页。
  - 逐行解析：复用 search 的 _TOC_RANGE_RE 等；标题跨行则向前累积(carry)。
  - 印刷起页→pdf 页：用本卷 pages.printed_page 建 printed→pdf 映射落地。
只 DELETE/INSERT 指定卷的 toc_entries，其余不动；完成重算 sha256。

用法：python scripts/build_quanji2_scan_toc.py [--only 48 39 ...]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402
from search import (  # noqa: E402
    _TOC_RANGE_RE,
    _TOC_INLINE_RANGE_RE,
    _TOC_TRAILING_PAGE_RE,
)

BOOK = "全集二版"
DEFAULT_VOLS = [16, 30, 32, 34, 39, 40, 48]
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")
MAX_SCAN = 140  # 前置目录最多扫到第 140 个 pdf 页（个别卷前言很长）

_NUM = re.compile(r"^[0-9IVXLCDMivxlcdm\s\-—–\.]+$")
# 目录里的「专题/著作组标题」行（无页码、作为层级头）：如「资本论」「经济学手稿」「1849年」
_HEADER_HINT = re.compile(r"^[\[（(]?[一二三四五六七八九十*]*[卡·弗一-鿿（）()\[\]\d\s—\-]{1,30}[\]）)]?$")


def clean_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\x00", "")
    text = re.sub(r"[‎‏‪-‮⁦-⁩]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_line(line: str):
    """返回 (title, start_token) 或 (None, None)。仅用「标题…(点引线2+)…页码」规则：
    点引线是目录条目的强特征，能干净排除正文/页眉（避免「标题 数字」松规则的误匹配）。"""
    cand = clean_title(line)
    if not cand:
        return None, None
    m = _TOC_RANGE_RE.match(cand)
    if m:
        return clean_title(m.group("title")), m.group("start")
    return None, None


_ENTRY_START = re.compile(r"^[*\s]*[（(]?[0-9]{1,4}[)）.、]")
_YEAR_LINE = re.compile(r"^\s*\d{3,4}\s*年\s*$")


def strip_lead_index(title: str) -> str:
    # 去掉行首序号「12.」「(2)」「*」与多余点引线
    t = re.sub(r"^[*\s]*", "", title)
    t = re.sub(r"^[（(]?\d{1,3}[)）.、]\s*", "", t)
    t = t.strip(" ·.．…-—")
    return t.strip()


def is_toc_header_page(lines: list[str]) -> bool:
    head = [l.strip() for l in lines[:4] if l.strip()]
    return any(l.replace(" ", "") == "目录" for l in head)


def count_entries(text: str) -> int:
    n = 0
    for line in text.splitlines():
        t, s = parse_line(line)
        if s is not None and not _NUM.fullmatch(clean_title(line) or ""):
            n += 1
    return n


def find_toc_pages(text_by_pdf: dict[int, str]) -> list[int]:
    pdfs = sorted(p for p in text_by_pdf if p <= MAX_SCAN)
    start = None
    for p in pdfs:
        lines = text_by_pdf[p].splitlines()
        if is_toc_header_page(lines) and count_entries(text_by_pdf[p]) >= 2:
            start = p
            break
    if start is None:
        return []
    pages = [start]
    for p in range(start + 1, min(max(text_by_pdf) + 1, MAX_SCAN + 1)):
        txt = text_by_pdf.get(p, "")
        if count_entries(txt) >= 2 or is_toc_header_page(txt.splitlines()):
            pages.append(p)
        else:
            break
    return pages


def parse_volume_toc(text_by_pdf: dict[int, str], toc_pages: list[int],
                     printed_to_pdf: dict[str, int]) -> list[dict]:
    entries: list[dict] = []
    pending = ""
    seen: set[tuple[int, str]] = set()
    for pdf in toc_pages:
        for raw in text_by_pdf[pdf].splitlines():
            line = clean_title(raw)
            if not line or _NUM.fullmatch(line):  # 空行 / 页眉页码行
                continue
            title, token = parse_line(line)
            if token is None:
                # 无「…页码」：判断是「跨行条目片段(累积)」还是「结构性标题行(重置)」
                if _YEAR_LINE.match(line) or "目录" in line.replace(" ", ""):
                    pending = ""                       # 年份/「目录」表头 → 重置
                elif _ENTRY_START.match(line):
                    pending = line                     # 编号条目折行的首段
                elif pending and 1 <= len(line) <= 30:
                    pending = pending + line           # 折行条目的后续段
                else:
                    pending = ""                       # 作者/著作组等结构标题 → 丢弃，不并入下一条
                continue
            full = strip_lead_index((pending + title) if pending else title)
            pending = ""
            if len(full) < 2 or _NUM.fullmatch(full) or "目录" in full.replace(" ", ""):
                continue
            pdf_target = printed_to_pdf.get(str(token))
            if pdf_target is None:
                continue
            key = (pdf_target, full)
            if key in seen:
                continue
            seen.add(key)
            entries.append({"title": full, "pdf_page": pdf_target, "printed_page": str(token)})

    # 偏移共识过滤：正文条目的 (pdf−印刷) 偏移应一致；偏离中位数过大者多为印刷页码检测噪声
    # （把目录页/前置页的页码误当正文页码），剔除之，避免目录项跳到错误页。
    offs = []
    for e in entries:
        try:
            offs.append(e["pdf_page"] - int(e["printed_page"]))
        except ValueError:
            offs.append(None)
    valid = sorted(o for o in offs if o is not None)
    if valid:
        med = valid[len(valid) // 2]
        kept = []
        for e, o in zip(entries, offs):
            if o is None or abs(o - med) <= 4:
                kept.append(e)
        entries = kept
    # 去掉相邻重复标题（目录里同一节标题在多子页重复列出）
    dedup: list[dict] = []
    for e in entries:
        if dedup and dedup[-1]["title"] == e["title"]:
            continue
        dedup.append(e)
    dedup.sort(key=lambda e: e["pdf_page"])
    return dedup


def update_hash() -> None:
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", type=int, help="只处理指定卷号（默认全部 7 卷）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    vols = args.only or DEFAULT_VOLS

    conn = sqlite3.connect(str(DB_PATH))
    try:
        for v in vols:
            rows = conn.execute(
                "SELECT pdf_page, printed_page, raw_text, source_file FROM pages "
                "WHERE book=? AND volume=? ORDER BY pdf_page", (BOOK, v)
            ).fetchall()
            if not rows:
                print(f"[vol {v}] 无 pages，跳过")
                continue
            source_file = rows[0][3]
            text_by_pdf = {r[0]: (r[2] or "") for r in rows}
            printed_to_pdf: dict[str, int] = {}
            for pdf_page, printed, _t, _sf in rows:
                if printed and str(printed) not in printed_to_pdf:
                    printed_to_pdf[str(printed)] = pdf_page
            toc_pages = find_toc_pages(text_by_pdf)
            entries = parse_volume_toc(text_by_pdf, toc_pages, printed_to_pdf) if toc_pages else []
            print(f"[vol {v}] 目录页={toc_pages[:3]}{'...' if len(toc_pages) > 3 else ''}"
                  f"({len(toc_pages)}页) → 解析章节 {len(entries)} 条")
            if entries[:3]:
                for e in entries[:3]:
                    print(f"      pdf{e['pdf_page']} 印{e['printed_page']} {e['title'][:40]}")
            if args.dry_run or not entries:
                continue
            conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (BOOK, v))
            conn.executemany(
                "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, printed_page, level, kind, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(BOOK, v, source_file, e["title"], e["pdf_page"], e["printed_page"], 1, "body", i + 1)
                 for i, e in enumerate(entries)],
            )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    if not args.dry_run:
        update_hash()
    print("完成。")


if __name__ == "__main__":
    main()
