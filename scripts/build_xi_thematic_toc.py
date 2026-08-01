# -*- coding: utf-8 -*-
"""为习近平专题文献 4 本无书签的「学习纲要 / 概论」从**印刷目录页**解析章节目录（toc_entries）。

这 4 本（生态文明 / 党建概论 / 文化 / 法治）的目录是「提纲式」而非「篇名（日期）」式，
build_scan_volumes 的 detect 模式不适用。印刷目录版式规整（两种）：

  纲要式（生态/文化/法治）：            概论式（党建）：
    篇首综述 …………… 1  ← L1(无前缀)     绪论 深入学习…… 1        ← L1
    一、专题标题                            一、…… 2  二、…… 7      ← L2
    ——关于…… 11        ← L2(可折 2 行)     第一章 坚持和加强…… 17  ← L1
    1. 条目 …… 11        ← L3               一、…… 17  二、…… 23    ← L2
    2. 条目 …… 14

故用行状态机：累积标题片段 → 遇「以裸页码收尾的行」结一条；层级由该条**首行前缀**判定
（第X章/绪论/前言/结束语 或无前缀→L1；一、二、…→L2；1. 2. …→L3）。印刷页码→pdf 页由
pages 表实际映射落地；个别页码 OCR 误读致映射不到的，回退到正文页首标题匹配定位。

只 DELETE/INSERT 本 book+volume 的 toc_entries；完成重算 sha256。其它书库不动。
须先跑 build_scan_volumes.py（写入 pages 与印刷页码）。

用法：python scripts/build_xi_thematic_toc.py [--book <key>] [--dry-run]
      不带 --book 时处理全部 4 本。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402

HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

BOOKS = [
    "习近平生态文明思想学习纲要",
    "习近平总书记关于党的建设的重要思想概论",
    "习近平文化思想学习纲要",
    "习近平法治思想学习纲要",
]

_FULL2HALF = str.maketrans("０１２３４５６７８９", "0123456789")
# 行尾裸页码：点引线/空格/间隔号后接 1~4 位数字收尾
_PAGE_END = re.compile(r"[·.．。…⋯\-—\s]*([0-9０-９]{1,4})\s*$")
_CJK = "一二三四五六七八九十"
_CHAP = re.compile(r"^第\s*[一二三四五六七八九十百零〇]+\s*[章篇节]")
_PREF_L1 = re.compile(r"^(绪\s*论|前\s*言|引\s*言|结\s*束\s*语|结\s*语|余\s*论|后\s*记|导\s*论)")
_SEC = re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、,，]")
_SUB = re.compile(r"^[0-9０-９]{1,2}\s*[.．、]")
_LEADER = re.compile(r"[·.．。…⋯]{2,}")


def _nkey(s: str) -> str:
    return re.sub(r"\s", "", s)


def detect_level(first_line: str) -> int:
    s = first_line.strip()
    if _CHAP.match(s) or _PREF_L1.match(s):
        return 1
    if _SEC.match(s):
        return 2
    if _SUB.match(s):
        return 3
    return 1  # 无前缀 → 篇首综述（L1）


def is_noise(line: str, book: str) -> bool:
    k = _nkey(line)
    if k in ("", "目", "录", "目录"):
        return True
    if re.fullmatch(r"[0-9０-９]{1,3}", k):  # 版心 folio 独行
        return True
    # 逐页重复的书名 running header（取书名去括注后主体）
    stem = re.sub(r"[（(].*?[)）]", "", book)
    if k and (k in _nkey(stem) or _nkey(stem) in k) and len(k) >= 6:
        return True
    return False


def find_toc_pages(text_by_pdf: dict[int, str]) -> list[int]:
    """从含「目录」标题的前置页起，取连续的目录页（每页 ≥2 条裸页码收尾行）。"""
    start = None
    for p in sorted(text_by_pdf):
        if p > 40:
            break
        if any(_nkey(l) == "目录" for l in text_by_pdf[p].splitlines()):
            start = p
            break
    if start is None:
        # 回退：首个含 ≥3 条裸页码收尾行的前置页
        for p in sorted(text_by_pdf):
            if p > 40:
                break
            lines = [l for l in text_by_pdf[p].splitlines() if l.strip()]
            if sum(1 for l in lines if _PAGE_END.search(l.strip())) >= 3:
                start = p
                break
    if start is None:
        return []
    pages = [start]
    p = start + 1
    while p in text_by_pdf:
        lines = [l for l in text_by_pdf[p].splitlines() if l.strip()]
        nclose = sum(1 for l in lines if _PAGE_END.search(l.strip()))
        # 目录页：多条页码收尾行；正文页几乎没有 → 连续判定，遇非目录页停止
        if nclose >= 2 and len(lines) <= 30:
            pages.append(p)
            p += 1
        else:
            break
    return pages


def parse_toc(text_by_pdf: dict[int, str], toc_pages: list[int], book: str):
    """返回 [(printed_page:int, level:int, title:str)]（按出现顺序，未映射未去重）。"""
    entries = []
    acc: list[str] = []
    for pno in toc_pages:
        for raw in text_by_pdf[pno].splitlines():
            line = raw.strip()
            if not line or is_noise(line, book):
                continue
            m = _PAGE_END.search(line)
            if m and _nkey(line[: m.start()]):  # 收尾行须在页码前还有文字
                printed = int(m.group(1).translate(_FULL2HALF))
                head = acc[0] if acc else line
                level = detect_level(head)
                body = "".join(acc) + line[: m.start()]
                # 清洗：去点引线、去首尾装饰
                title = _LEADER.sub("", body)
                title = re.sub(r"\s+", "", title).strip(" 　·.．。…⋯-—")
                acc = []
                if _nkey(title) and 2 <= len(title) <= 90:
                    entries.append((printed, level, title))
            else:
                acc.append(line)
    return entries


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def locate_in_body(title: str, rows, lo_pdf: int):
    """页码映射失败时的回退：在 lo_pdf 之后的正文页里找页首含标题的页。"""
    key = _nkey(re.sub(r"^[0-9０-９一二三四五六七八九十]{0,3}[.．、,，]?", "", title))[:12]
    if len(key) < 6:
        return None
    for pdf_page, printed, raw in rows:
        if pdf_page < lo_pdf or not printed:
            continue
        head = _nkey(raw[:80])
        if key and key in head:
            return pdf_page, printed
    return None


def build_book(conn: sqlite3.Connection, book: str, dry_run: bool) -> int:
    rows_raw = conn.execute(
        "SELECT pdf_page, printed_page, raw_text, source_file FROM pages "
        "WHERE book=? AND volume=1 ORDER BY pdf_page",
        (book,),
    ).fetchall()
    if not rows_raw:
        print(f"[{book}] 无 pages，跳过（先跑 build_scan_volumes.py）。")
        return 0
    source_file = rows_raw[0][3]
    text_by_pdf = {r[0]: (r[2] or "") for r in rows_raw}
    rows = [(r[0], r[1], r[2] or "") for r in rows_raw]

    printed_to_pdf: dict[str, int] = {}
    for pdf_page, printed, _raw in rows:
        if printed and str(printed).isdigit() and str(printed) not in printed_to_pdf:
            printed_to_pdf[str(printed)] = pdf_page

    toc_pages = find_toc_pages(text_by_pdf)
    raw_entries = parse_toc(text_by_pdf, toc_pages, book)

    final = []  # (pdf_page, printed, level, title)
    unmapped = 0
    last_pdf = 0
    for printed, level, title in raw_entries:
        pdf_page = printed_to_pdf.get(str(printed))
        if pdf_page is None or pdf_page < last_pdf:
            got = locate_in_body(title, rows, max(1, last_pdf))
            if got:
                pdf_page, printed = got
            else:
                unmapped += 1
                continue
        final.append((pdf_page, printed, level, title))
        last_pdf = pdf_page

    lvl_hist = {}
    for _p, _pr, lv, _t in final:
        lvl_hist[lv] = lvl_hist.get(lv, 0) + 1
    print(f"[{book}] 目录页 pdf{toc_pages} → 解析 {len(raw_entries)} / 落地 {len(final)}"
          f"（层级 {lvl_hist}）" + (f"，{unmapped} 条未映射跳过" if unmapped else ""))
    for pdf_page, printed, lv, title in final[:4]:
        print(f"      L{lv} 印{printed:>4} pdf{pdf_page:>4}  {title[:44]}")
    print("      ...")
    for pdf_page, printed, lv, title in final[-3:]:
        print(f"      L{lv} 印{printed:>4} pdf{pdf_page:>4}  {title[:44]}")

    if dry_run or not final:
        if not final:
            print(f"[{book}] 解析为空，未写库。")
        return 0
    conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=1", (book,))
    conn.executemany(
        "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
        "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (book, 1, source_file, title, pdf_page, str(printed), level, "body", i + 1)
            for i, (pdf_page, printed, level, title) in enumerate(final)
        ],
    )
    return len(final)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", action="append", help="书库 key，可重复；默认全部 4 本")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    books = args.book or BOOKS
    unknown = [b for b in books if b not in BOOKS]
    if unknown:
        raise SystemExit("非本脚本处理的书库：" + ", ".join(unknown))

    conn = sqlite3.connect(str(DB_PATH))
    total = 0
    try:
        for book in books:
            total += build_book(conn, book, args.dry_run)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    if not args.dry_run and total:
        update_hash()
        print(f"\n已写入 toc_entries 共 {total} 条，重算 sha256。")
    elif args.dry_run:
        print("\n[dry-run] 未写库。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
