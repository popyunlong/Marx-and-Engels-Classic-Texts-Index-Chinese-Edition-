# -*- coding: utf-8 -*-
"""为《X大以来重要文献选编》某卷从**印刷目录页**解析权威章节目录（toc_entries）。

选编卷无书签，build_scan_volumes 的 `detect` 模式对选编召回很低（正文篇首多为无日期起头的
决定/公报/条例，识别不到）。但本系列印刷目录页版式规整：

    高举中国特色社会主义伟大旗帜，为全面建设      ← 标题（可折成 1~2 行）
    社会主义现代化国家而团结奋斗………… 习近平（1）   ← 末行＝标题尾＋点引线＋[责任者]（印刷页码）
    （二〇二二年十月十六日）                     ← 日期括注（可跨行），跳过

故用行状态机：累积标题片段 → 遇「以（印刷页码）收尾的行」结一条（切掉点引线后的责任者）→
跳过日期括注（按括号深度跨行）。因文本层双份嵌入，同一条会重复出现 → **按印刷页码去重**
（留最短净标题，双层偶带责任者会更长）。印刷页码→pdf 页由 pages 表实际映射落地
（本卷为常量 offset 14，但用 DB 映射更稳、且适配后续中/下卷）。

只 DELETE/INSERT 本 book+volume 的 toc_entries；完成重算 sha256。其它书库不动。
须先跑 build_scan_volumes.py（写入 pages 与印刷页码）。

用法：python scripts/build_xuanbian_toc.py [--book 二十大以来重要文献选编] [--volume 1] [--dry-run]
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

# 点引线（点/省略号，1+；不含中点·以免切断人名/引号内容）
_LEADER = re.compile(r"[.．…⋯]+")
# 以（印刷页码）收尾的行（全/半角括号、全/半角数字）
_PAGE_END = re.compile(r"[（(]\s*([0-9０-９]{1,4})\s*[)）]\s*$")
# 单纯的「收尾行」判定（用于识别目录页）
_CLOSE = re.compile(r"[（(]\s*[0-9０-９]{1,4}\s*[)）]\s*$")
_FULL2HALF = str.maketrans("０１２３４５６７８９", "0123456789")


def _has_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


def _nkey(s: str) -> str:
    return re.sub(r"\s", "", s)


def _is_noise(line: str) -> bool:
    """目录页里的结构噪声行：页眉「目 录」、版心 folio 数字、逐页重复的书名running header。"""
    k = _nkey(line)
    if k in ("", "目", "录", "目录"):
        return True
    if re.fullmatch(r"[0-9０-９]{1,3}", k):
        return True
    if k.startswith("重要文献选编") or "以来重要文献选编" in k:
        return True
    return False


def find_toc_pages(text_by_pdf: dict[int, str]) -> list[int]:
    """目录页＝出现≥2 条「（页码）收尾行」的前置页；遇首个非目录页停止（跳过编辑说明等）。"""
    pages: list[int] = []
    started = False
    for p in sorted(text_by_pdf):
        lines = [ln for ln in text_by_pdf[p].splitlines() if ln.strip()]
        nclose = sum(1 for ln in lines if _CLOSE.search(ln.strip()))
        if nclose >= 2:
            pages.append(p)
            started = True
        elif started:
            break
        elif p > 40:
            break
    return pages


def parse_toc(text_by_pdf: dict[int, str], toc_pages: list[int]) -> list[tuple[int, str]]:
    """行状态机解析目录，返回 [(printed_page:int, title:str)]（未去重、未映射）。"""
    entries: list[tuple[int, str]] = []
    acc: list[str] = []
    in_paren = 0
    for pno in toc_pages:
        for raw in text_by_pdf[pno].splitlines():
            line = raw.strip()
            if not line:
                continue
            if in_paren > 0:  # 处于日期/括注内部（可跨行）
                in_paren += line.count("(") + line.count("（") - line.count(")") - line.count("）")
                continue
            if _is_noise(line):
                acc = []
                continue
            if line[0] in "（(":  # 日期/括注起始
                in_paren = max(0, line.count("(") + line.count("（") - line.count(")") - line.count("）"))
                continue
            m = _PAGE_END.search(line)
            if m:
                printed = int(m.group(1).translate(_FULL2HALF))
                rest = line[: m.start()]
                title = "".join(acc) + _LEADER.split(rest)[0]
                title = re.sub(r"\s+", "", title).strip(" 　·.．…⋯-—")
                acc = []
                if _has_cjk(title) and 2 <= len(title) <= 90:
                    entries.append((printed, title))
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="二十大以来重要文献选编")
    ap.add_argument("--volume", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT pdf_page, printed_page, raw_text, source_file FROM pages "
            "WHERE book=? AND volume=? ORDER BY pdf_page",
            (args.book, args.volume),
        ).fetchall()
        if not rows:
            raise SystemExit(f"[{args.book} 第{args.volume}卷] 无 pages，请先跑 build_scan_volumes.py")
        source_file = rows[0][3]
        text_by_pdf = {r[0]: (r[2] or "") for r in rows}

        # 正文印刷页码→pdf 映射（printed 为 None 的前置页天然排除，规避目录页 folio 重号）
        printed_to_pdf: dict[str, int] = {}
        for pdf_page, printed, _raw, _sf in rows:
            if printed and str(printed).isdigit() and str(printed) not in printed_to_pdf:
                printed_to_pdf[str(printed)] = pdf_page

        toc_pages = find_toc_pages(text_by_pdf)
        raw_entries = parse_toc(text_by_pdf, toc_pages)

        # 按印刷页码去重：留最短净标题（双层副本偶带责任者会更长）
        best: dict[int, str] = {}
        for printed, title in raw_entries:
            if printed not in best or len(title) < len(best[printed]):
                best[printed] = title

        final: list[tuple[int, int, str]] = []  # (pdf_page, printed, title)
        unmapped = 0
        for printed in sorted(best):
            pdf_page = printed_to_pdf.get(str(printed))
            if pdf_page is None:
                unmapped += 1
                continue
            final.append((pdf_page, printed, best[printed]))
        final.sort()

        print(f"[{args.book} 第{args.volume}卷] 目录页 pdf{toc_pages[:1]}..{toc_pages[-1:]}"
              f"({len(toc_pages)}页) → 解析 {len(raw_entries)} 原始 / 去重 {len(best)} / 落地 {len(final)}"
              + (f"（{unmapped} 条印刷页未在正文映射，已跳过）" if unmapped else ""))
        for pdf_page, printed, title in final[:3] + final[-3:]:
            print(f"      印{printed:>4} pdf{pdf_page:>4}  {title[:46]}")

        if args.dry_run:
            print("[dry-run] 未写库。")
            return
        if not final:
            raise SystemExit("解析结果为空，未写库。")

        conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (args.book, args.volume))
        conn.executemany(
            "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
            "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (args.book, args.volume, source_file, title, pdf_page, str(printed), 1, "body", i + 1)
                for i, (pdf_page, printed, title) in enumerate(final)
            ],
        )
        conn.commit()
        update_hash()
        print(f"已写入 toc_entries {len(final)} 条（book={args.book} 第{args.volume}卷），重算 sha256。")
    finally:
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
