# -*- coding: utf-8 -*-
"""
修复《马恩全集》印刷页码截断错误。

根因：早期/部分卷的 OCR 文本层把页码数字拆成单字符「词」，build_index 的
detect_printed_page_from_page 逐词打分时只取到其中一位（567→5、16→1、177→1…），
于是阅读器「本页引文定位」与引文检索的页码大面积错位。

修复思路（纯 DB，不重开 PDF）：
  raw_text 里其实仍保留了拼合后的整数页码（"英国工人阶级状况 567"）。
  逐卷：
    1) 从每页页眉/页脚抽取候选整数；
    2) 统计 offset = pdf_page - 候选/已记录值，出现「够多次」的 offset 即真实
       分段偏移（正文/注释/加插页板会让 offset 阶梯式 +1，但绝不减小）；
    3) 以「非递减」方式沿 pdf 顺序给每页指派 offset：有本页证据支持更大的合法
       offset 才前进，否则沿用上一档；single-page 垃圾 offset 因从不累积而被剔除；
    4) new_printed = pdf_page - offset（含本页印刷页码）。
  仅改 book='全集' 行；前言罗马页码(pre-*)与正文起始前的页一律不动。
  另把 toc_entries.printed_page 与修复后的 pages 对齐。

用法：
  python scripts/fix_quanji_printed_pages.py            # 干跑，仅报告
  python scripts/fix_quanji_printed_pages.py --apply     # 落库 + 备份 + 重算 sha256
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "corpus.sqlite"
FW = str.maketrans("０１２３４５６７８９", "0123456789")

BOOK_LIKE = "pdfs/全集/%"          # 只动《全集》
OFF_MIN, OFF_MAX = 1, 260          # 合法偏移范围（前言+正文起点的页差）
MEDIAN_WIN = 21                    # 局部中位数窗口（抗孤立尖刺、保留持续的 +1 阶梯）
MIN_CONFIDENCE = 0.45             # 有效候选页占比低于此值则判该卷不可靠、整卷跳过
KEEP_TOL = 1                       # 已记录值与局部共识相差 <=1 视为「本就正确」，不动


def norm_digits(s: str) -> str:
    return unicodedata.normalize("NFKC", s).translate(FW)


def to_int(printed) -> int | None:
    if printed is None:
        return None
    p = norm_digits(str(printed)).strip()
    if p.startswith("pre-"):
        return None
    try:
        return int(p)
    except ValueError:
        return None


def is_pre(printed) -> bool:
    return printed is not None and str(printed).startswith("pre-")


def candidates(raw_text: str) -> set[int]:
    """页眉/页脚区(首2行+末2行)行首或行尾的整数候选。"""
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        return set()
    zone = lines[:2] + lines[-2:]
    out: set[int] = set()
    for ln in zone:
        s = norm_digits(ln)
        m = re.match(r"^(\d{1,4})(?!\d)", s)
        if m:
            out.add(int(m.group(1)))
        m = re.search(r"(?<!\d)(\d{1,4})\s*$", s)
        if m:
            out.add(int(m.group(1)))
    return {c for c in out if 1 <= c <= 2500}


def _median(vals: list[int]) -> int:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else s[n // 2 - 1]  # 偏小中位数，保持整数


def repair_volume(rows):
    """
    rows: list[(pdf_page, printed_page, raw_text)] 按 pdf_page 升序。

    思路（局部中位数共识，抗噪、最小改动）：
      offset = pdf_page - 真实印刷页码，在卷内分段恒定且非递减（加插页板 +1）。
      1) 每页从页眉/页脚候选取「最接近全卷众数 offset」者为本页 offset 估计；
      2) 前后填补空缺后做窗口中位数平滑 → 每页 smooth_off（孤立的截断尖刺被中位数吃掉，
         持续的 +1 阶梯得以保留）；
      3) 仅当已记录值与 smooth_off 相差 >1 时才判为错并改写为 pdf-smooth_off；
         相差 <=1（含 13/14 边界）视为本就正确、保持不动。
      4) 有效候选页占比过低（如第19卷文本层损毁）→ 整卷跳过、交多模态重识别。
    返回 {pdf_page: new_printed_str} + 统计。
    """
    cand_by_pdf = {pdf: candidates(raw) for pdf, _, raw in rows}
    n = len(rows)
    stats = {"mode_off": None, "changed": 0, "confidence": 0.0, "low_conf": False}

    # 全卷众数 offset（以候选为主，候选缺失时退而用已记录值）
    cand_votes: Counter[int] = Counter()
    rec_votes: Counter[int] = Counter()
    for pdf, printed, _ in rows:
        for c in cand_by_pdf[pdf]:
            off = pdf - c
            if OFF_MIN <= off <= OFF_MAX:
                cand_votes[off] += 1
        ip = to_int(printed)
        if ip is not None and OFF_MIN <= pdf - ip <= OFF_MAX:
            rec_votes[pdf - ip] += 1
    base_votes = cand_votes or rec_votes
    if not base_votes:
        stats["low_conf"] = True
        return {}, stats
    mode_off = base_votes.most_common(1)[0][0]
    stats["mode_off"] = mode_off

    # 每页 offset 估计 = 候选/记录中最接近众数者
    raw_off: list[int | None] = [None] * n
    for i, (pdf, printed, _) in enumerate(rows):
        opts = {pdf - c for c in cand_by_pdf[pdf]}
        ip = to_int(printed)
        if ip is not None:
            opts.add(pdf - ip)
        opts = [o for o in opts if OFF_MIN <= o <= OFF_MAX]
        if opts:
            raw_off[i] = min(opts, key=lambda o: (abs(o - mode_off), o))

    confidence = sum(v is not None for v in raw_off) / n
    stats["confidence"] = round(confidence, 3)
    if confidence < MIN_CONFIDENCE:
        stats["low_conf"] = True
        return {}, stats

    # 前后填补空缺
    filled = list(raw_off)
    last = None
    for i in range(n):
        if filled[i] is None:
            filled[i] = last
        else:
            last = filled[i]
    last = None
    for i in range(n - 1, -1, -1):
        if filled[i] is None:
            filled[i] = last
        else:
            last = filled[i]

    # 窗口中位数平滑
    smooth = [0] * n
    half = MEDIAN_WIN // 2
    for i in range(n):
        win = [filled[j] for j in range(max(0, i - half), min(n, i + half + 1)) if filled[j] is not None]
        smooth[i] = _median(win) if win else mode_off

    # 正文起点：首个 smooth 落到众数附近的页（其前视为前言，不动）
    body_start = next((i for i in range(n) if abs(smooth[i] - mode_off) <= 1), 0)

    changes: dict[int, str] = {}
    for i, (pdf, printed, _) in enumerate(rows):
        if i < body_start or is_pre(printed):
            continue
        target = pdf - smooth[i]
        if target < 1:
            continue
        rec = to_int(printed)
        if rec is not None and abs((pdf - rec) - smooth[i]) <= KEEP_TOL:
            continue  # 本就正确（含 ±1 边界），不动
        changes[pdf] = str(target)
    stats["changed"] = len(changes)
    return changes, stats


def vol_label(src: str) -> str:
    m = re.search(r"第\s*([0-9]+)\s*卷(?:\s*第\s*([0-9]+)\s*册)?", src)
    if not m:
        return src.split("/")[-1]
    return f"{m.group(1)}卷" + (f"-{m.group(2)}册" if m.group(2) else "")


def vol_sort(src: str):
    m = re.search(r"第\s*([0-9]+)\s*卷(?:\s*第\s*([0-9]+)\s*册)?", src)
    return (int(m.group(1)) if m else 999, int(m.group(2)) if (m and m.group(2)) else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="落库（默认仅干跑报告）")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()
    srcs = [r[0] for r in cur.execute(
        "SELECT DISTINCT source_file FROM pages WHERE source_file LIKE ? ORDER BY source_file",
        (BOOK_LIKE,)).fetchall()]
    srcs.sort(key=vol_sort)

    all_changes: dict[str, dict[int, str]] = {}
    total_changed = 0
    low_conf_vols = []
    print(f"{'vol':>9} {'pages':>5} {'mode':>5} {'conf':>5} {'changed':>7} {'dip':>4}")
    print("-" * 60)
    for src in srcs:
        rows = list(cur.execute(
            "SELECT pdf_page, printed_page, raw_text FROM pages WHERE source_file=? ORDER BY pdf_page",
            (src,)).fetchall())
        changes, st = repair_volume(rows)
        all_changes[src] = changes
        total_changed += len(changes)

        # 修复后非递减残留检查
        newseq = []
        for pdf, printed, _ in rows:
            v = to_int(changes.get(pdf, printed))
            if v is not None:
                newseq.append(v)
        dips = sum(1 for a, b in zip(newseq, newseq[1:]) if b < a)
        flag = " LOW-CONF→SKIP" if st["low_conf"] else ""
        if st["low_conf"]:
            low_conf_vols.append(vol_label(src))
        print(f"{vol_label(src):>9} {len(rows):>5} {str(st['mode_off']):>5} "
              f"{st['confidence']:>5.2f} {len(changes):>7} {dips:>4}{flag}")

    print("-" * 60)
    print(f"TOTAL pages to change: {total_changed}")
    if low_conf_vols:
        print(f"低置信跳过(交多模态重识别): {', '.join(low_conf_vols)}")

    if not args.apply:
        print("\n[dry-run] 未落库。加 --apply 写入。")
        return

    # 备份
    backup = Path(args.db + ".bak-prefix-fix")
    shutil.copy2(args.db, backup)
    print(f"\n已备份: {backup}")

    npg = ntoc = 0
    for src, changes in all_changes.items():
        for pdf, val in changes.items():
            cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?",
                        (val, src, pdf))
            npg += cur.rowcount
    # 把 toc_entries 的非 pre 印刷页码对齐到修复后的 pages
    for src in all_changes:
        toc = cur.execute(
            "SELECT rowid, pdf_page, printed_page FROM toc_entries WHERE source_file=?",
            (src,)).fetchall()
        pmap = dict(cur.execute(
            "SELECT pdf_page, printed_page FROM pages WHERE source_file=?", (src,)).fetchall())
        for rowid, pdf, printed in toc:
            if is_pre(printed):
                continue
            newp = pmap.get(pdf)
            if newp is not None and not is_pre(newp) and str(newp) != str(printed):
                cur.execute("UPDATE toc_entries SET printed_page=? WHERE rowid=?", (newp, rowid))
                ntoc += cur.rowcount
    con.commit()
    print(f"已更新 pages 行: {npg}；toc_entries 行: {ntoc}")
    con.close()
    print("完成。请运行 scripts/write_release_metadata.py 重算 sha256。")


if __name__ == "__main__":
    main()
