# -*- coding: utf-8 -*-
"""
第19卷印刷页码重建（多模态视觉核定）。

第19卷的 OCR 文本层**完全没有**正文页码（页码印在版心顶端外侧角、扫描 OCR 整体漏识，
全卷正文匹配 pdf-off 仅 0/50），故 scripts/fix_quanji_printed_pages.py 的「文本共识」
对该卷置信 0.31 < 0.45 整卷跳过。

改用视觉核定：渲染各页顶端外侧角，人工/视觉读取真实页码。在 pdf 12/13/14/30/80/140/
200/260/320/380/440/500/560/620/680/700/715/730/745/753/754 共 21 个横跨全卷的取样点
**全部得到 offset 恰为 9**（printed = pdf_page - 9）；offset = pdf-printed 单调非递减且末页
(pdf754=后记)仍为 9 → 全卷无加插页/无分段重排，offset 恒为 9，可精确推导。
前言经视觉核定：pdf1-3=封面/扉页(无页码)，pdf4-9=目录罗马页码 Ⅰ-Ⅵ。

映射（已核定）：
  pdf 1-3   → None（无印刷页码）
  pdf 4-9   → pre-i … pre-vi（前言罗马页码）
  pdf 10-754→ str(pdf_page - 9)（正文 1…745；pdf10/11 为篇章首页，页码印刷时省略但仍计数）

用法： python scripts/fix_quanji_vol19_pagenums.py [--apply]
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "corpus.sqlite"
SRC = "pdfs/全集/马克思恩格斯全集 第19卷(OCR).pdf"
OFFSET = 9
ROMAN = {4: "pre-i", 5: "pre-ii", 6: "pre-iii", 7: "pre-iv", 8: "pre-v", 9: "pre-vi"}


def target_printed(pdf_page: int) -> str | None:
    if pdf_page in ROMAN:
        return ROMAN[pdf_page]
    if pdf_page >= 10:
        return str(pdf_page - OFFSET)
    return None  # pdf 1-3 封面/扉页


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT pdf_page, printed_page FROM pages WHERE source_file=? ORDER BY pdf_page",
        (SRC,)).fetchall()
    if not rows:
        raise SystemExit("第19卷未找到，检查 source_file。")

    changes = []
    for pdf, printed in rows:
        new = target_printed(pdf)
        if (str(printed) if printed is not None else None) != (str(new) if new is not None else None):
            changes.append((pdf, printed, new))

    # 推导后正文页码序列单调性自检
    seq = [int(target_printed(p)) for p, _ in rows
           if (target_printed(p) or "").isdigit()]
    dips = sum(1 for a, b in zip(seq, seq[1:]) if b < a)
    print(f"第19卷 {len(rows)} 页：拟改 {len(changes)} 页；正文序列 {seq[0]}…{seq[-1]}，残留逆序 {dips}")
    print("样例：", [(p, o, n) for p, o, n in changes[:6]])

    if not args.apply:
        print("\n[dry-run] 未落库。加 --apply 写入。")
        return

    backup = Path(args.db + ".bak-vol19")
    shutil.copy2(args.db, backup)
    print(f"已备份: {backup}")

    npg = 0
    for pdf, _old, new in changes:
        cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?",
                    (new, SRC, pdf))
        npg += cur.rowcount
    # toc_entries 对齐到修复后的 pages（同 pdf_page）
    pmap = dict(cur.execute("SELECT pdf_page, printed_page FROM pages WHERE source_file=?", (SRC,)).fetchall())
    ntoc = 0
    for rowid, pdf, printed in cur.execute(
            "SELECT rowid, pdf_page, printed_page FROM toc_entries WHERE source_file=?", (SRC,)).fetchall():
        newp = pmap.get(pdf)
        if (str(printed) if printed is not None else None) != (str(newp) if newp is not None else None):
            cur.execute("UPDATE toc_entries SET printed_page=? WHERE rowid=?", (newp, rowid))
            ntoc += cur.rowcount
    con.commit()
    con.close()
    print(f"已更新 pages 行: {npg}；toc_entries 行: {ntoc}")
    print("完成。请运行 scripts/write_release_metadata.py 重算 sha256。")


if __name__ == "__main__":
    main()
