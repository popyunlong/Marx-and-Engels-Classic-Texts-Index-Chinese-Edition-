# -*- coding: utf-8 -*-
"""全库印刷页码修复（确定性、无 AI）——把 fix_quanji_printed_pages 的局部中位数 offset
共识法泛化到所有书库，同时修复两类问题：
  · 缺页码(FILL) —— 正文页 printed_page 为空，引文降级显示「按PDF页码」；
  · 错页码(WRONG) —— OCR 把脚注号(11)/年份(2012)/乱码(1821)当成了页码，引文静默错标。

安全闸（绝不在不可靠卷上乱改）：
  1) 仅处理「可靠卷」：有效候选占比 conf>=0.85 且 OK 页的 offset 序列非递减（单调）。
     多文档各自重编页码(offset 会下降，如部分五年规划/治国理政五卷)一律判为不可靠 → 交 Phase2 视觉。
  2) offset = pdf_page - 印刷页码，卷内分段恒定且非递减（加插页板 +1）；窗口中位数平滑抗孤立尖刺。
  3) 前言/罗马页码(pre-*)与正文起点前的页一律不动。
  4) WRONG 纠正额外要求「本地 offset 稳定」或「target 恰在本页页眉页脚可见(自证)」，避免误伤分段边界。
  5) FILL 仅当 target>=1 且落在正文段内。

用法：
  python scripts/fix_printed_pages_all.py                    # 干跑报告
  python scripts/fix_printed_pages_all.py --report-json X    # 干跑并导出变更清单
  python scripts/fix_printed_pages_all.py --apply [--db ...] # 落库(备份+对齐toc+重算sha256)
  python scripts/fix_printed_pages_all.py --only "pdfs/全集/%"  # 仅处理匹配 source_file 的卷
"""
from __future__ import annotations
import argparse, hashlib, json, re, shutil, sqlite3, unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "corpus.sqlite"
FW = str.maketrans("０１２３４５６７８９", "0123456789")
OFF_MIN, OFF_MAX = 0, 400
MEDIAN_WIN = 21
MIN_CONFIDENCE = 0.85     # 可靠卷阈值（比 quanji 版更严，因要跨全库自动落库）
KEEP_TOL = 1
STABLE_WIN = 5            # WRONG 纠正要求 ±5 页内 smooth offset 恒定
# 编入大型「文件汇编」的书：每页同时印着汇编大页码(517+)与文档内小页码(1,2..)，自证会误取小页码，
# offset 共识也会锁错 → 一律不在 Phase1 动，交 Phase2 视觉按「最外侧汇编页码」读。
DEFER_BOOKS = {"五年规划"}


def norm_digits(s: str) -> str:
    return unicodedata.normalize("NFKC", s).translate(FW)

def to_int(printed):
    if printed is None:
        return None
    p = norm_digits(str(printed)).strip()
    if p.startswith("pre-"):
        return None
    try:
        return int(p)
    except ValueError:
        return None

def is_pre(printed):
    return printed is not None and str(printed).startswith("pre-")

def candidates(raw_text: str) -> set[int]:
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

def _median(vals):
    s = sorted(vals); n = len(s)
    return s[n // 2] if n % 2 else s[n // 2 - 1]


def analyze_volume(rows):
    """rows:[(pdf,printed,raw)] asc → (per_page, stats).
    per_page[i] = dict(pdf, recorded, smooth_off, target, cls, cand)."""
    n = len(rows)
    cand_by_pdf = {pdf: candidates(raw) for pdf, _, raw in rows}
    cand_votes, rec_votes = Counter(), Counter()
    for pdf, printed, _ in rows:
        for c in cand_by_pdf[pdf]:
            off = pdf - c
            if OFF_MIN <= off <= OFF_MAX:
                cand_votes[off] += 1
        ip = to_int(printed)
        if ip is not None and OFF_MIN <= pdf - ip <= OFF_MAX:
            rec_votes[pdf - ip] += 1
    base = cand_votes or rec_votes
    stats = {"mode_off": None, "conf": 0.0, "low_conf": True, "mono": True, "body_start": 0}
    if not base:
        per = [{"pdf": p, "recorded": pr, "smooth_off": None, "target": None,
                "cls": "FRONT" if (is_pre(pr) or to_int(pr) is None) else "OK",
                "cand": cand_by_pdf[p]} for p, pr, _ in rows]
        return per, stats
    mode_off = base.most_common(1)[0][0]
    stats["mode_off"] = mode_off

    raw_off = [None] * n
    for i, (pdf, printed, _) in enumerate(rows):
        opts = {pdf - c for c in cand_by_pdf[pdf]}
        ip = to_int(printed)
        if ip is not None:
            opts.add(pdf - ip)
        opts = [o for o in opts if OFF_MIN <= o <= OFF_MAX]
        if opts:
            raw_off[i] = min(opts, key=lambda o: (abs(o - mode_off), o))
    conf = sum(v is not None for v in raw_off) / n if n else 0.0
    stats["conf"] = round(conf, 3)
    stats["low_conf"] = conf < MIN_CONFIDENCE

    filled = list(raw_off)
    last = None
    for i in range(n):
        if filled[i] is None: filled[i] = last
        else: last = filled[i]
    last = None
    for i in range(n - 1, -1, -1):
        if filled[i] is None: filled[i] = last
        else: last = filled[i]
    smooth = [0] * n
    half = MEDIAN_WIN // 2
    for i in range(n):
        win = [filled[j] for j in range(max(0, i - half), min(n, i + half + 1)) if filled[j] is not None]
        smooth[i] = _median(win) if win else mode_off
    body_start = next((i for i in range(n) if abs(smooth[i] - mode_off) <= 1), 0)
    stats["body_start"] = body_start

    # OK 页 offset 单调性（用于可靠性判定）
    ok_offsets = []
    per = []
    for i, (pdf, printed, raw) in enumerate(rows):
        rec = to_int(printed)
        target = pdf - smooth[i]
        cls = "OK"
        if is_pre(printed) or i < body_start:
            cls = "FRONT"
        elif rec is None:
            cls = "FILL" if target >= 1 else "UNRESOLVED"
        elif abs((pdf - rec) - smooth[i]) > KEEP_TOL and target >= 1:
            cls = "WRONG"
        else:
            cls = "OK"
            ok_offsets.append(pdf - rec)
        per.append({"pdf": pdf, "recorded": printed, "smooth_off": smooth[i],
                    "target": target if target >= 1 else None, "cls": cls,
                    "cand": cand_by_pdf[pdf], "stable": _stable(smooth, i)})
    stats["mono"] = all(b >= a for a, b in zip(ok_offsets, ok_offsets[1:]))
    return per, stats


def _stable(smooth, i):
    lo = max(0, i - STABLE_WIN); hi = min(len(smooth), i + STABLE_WIN + 1)
    seg = smooth[lo:hi]
    return all(x == smooth[i] for x in seg)


def collect_changes(rows, per, stats):
    """返回该卷要改的 {pdf:(old,new,cls)}；不可靠卷返回空。

    Phase1 只落「确定性」变更（其余交 Phase2 视觉），两步：
      (1) 自证 WRONG：target 恰在本页页眉页脚可见 —— GLM-4V 抽检 10/10 吻合，最硬。
          这类先纠正，等于把「把年份/脚注号误当页码」的垃圾从锚里清掉。
      (2) 严格顺序 FILL：在(1)清洗后的「干净锚」上，仅当缺页两侧锚恰好等差相邻
          (after-before == pdf 跨距) 时插值补全 —— 绝不在插页板/重编段乱补。
    干净锚 = OK 页(记录值与共识一致) + 已自证纠正的 WRONG 页。
    非自证 WRONG、补不上的 FILL、不可靠卷 → 全部留给 Phase2 视觉。
    """
    reliable = (not stats["low_conf"]) and stats["mono"]
    if not reliable:
        return {}, reliable
    n = len(per)
    changes = {}
    clean = [None] * n  # 每页「可信印刷页码」：OK记录 / 自证纠正；垃圾与缺页为 None
    for i, p in enumerate(per):
        if p["cls"] == "OK":
            clean[i] = to_int(p["recorded"])
        elif p["cls"] == "WRONG" and p["target"] in p["cand"]:  # 自证
            clean[i] = p["target"]
            changes[p["pdf"]] = (p["recorded"], str(p["target"]), "WRONG")
    # 严格顺序 FILL：对每段连续缺页，两侧干净锚等差相邻才补
    i = 0
    while i < n:
        if per[i]["cls"] != "FILL":
            i += 1
            continue
        j = i
        while j < n and per[j]["cls"] == "FILL":
            j += 1
        left, right = i - 1, j  # 缺页段 [i, j-1]，两侧 index
        if left >= 0 and right < n and clean[left] is not None and clean[right] is not None:
            gap = per[right]["pdf"] - per[left]["pdf"]
            if clean[right] - clean[left] == gap:  # 完全等差、无插页板
                for k in range(i, j):
                    val = clean[left] + (per[k]["pdf"] - per[left]["pdf"])
                    if val >= 1:
                        clean[k] = val
                        changes[per[k]["pdf"]] = (per[k]["recorded"], str(val), "FILL")
        i = j
    return changes, reliable


def vol_short(src): return src.split("/")[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--only", default="", help="仅处理 source_file LIKE 此模式的卷")
    ap.add_argument("--report-json", default="")
    args = ap.parse_args()

    con = sqlite3.connect(args.db); cur = con.cursor()
    if args.only:
        srcs = [r[0] for r in cur.execute(
            "SELECT DISTINCT source_file FROM pages WHERE source_file LIKE ? ", (args.only,)).fetchall()]
    else:
        srcs = [r[0] for r in cur.execute("SELECT DISTINCT source_file FROM pages").fetchall()]

    all_changes = {}
    skipped_unreliable = []
    grand = Counter()
    report = []
    for src in sorted(srcs):
        rows = cur.execute("SELECT pdf_page, printed_page, raw_text FROM pages WHERE source_file=? ORDER BY pdf_page", (src,)).fetchall()
        book = cur.execute("SELECT book FROM pages WHERE source_file=? LIMIT 1", (src,)).fetchone()[0]
        per, stats = analyze_volume(rows)
        if book in DEFER_BOOKS:
            nprob = sum(1 for p in per if p["cls"] in ("FILL", "WRONG", "UNRESOLVED"))
            if nprob:
                skipped_unreliable.append({"book": book, "src": vol_short(src), "conf": stats["conf"],
                                           "mono": stats["mono"], "deferred": nprob})
            continue
        changes, reliable = collect_changes(rows, per, stats)
        if not reliable:
            # 统计该卷有多少问题页被留给 Phase2
            nprob = sum(1 for p in per if p["cls"] in ("FILL", "WRONG", "UNRESOLVED"))
            if nprob:
                skipped_unreliable.append({"book": book, "src": vol_short(src), "conf": stats["conf"],
                                           "mono": stats["mono"], "deferred": nprob})
            continue
        if changes:
            all_changes[src] = changes
            nf = sum(1 for v in changes.values() if v[2] == "FILL")
            nw = sum(1 for v in changes.values() if v[2] == "WRONG")
            grand["FILL"] += nf; grand["WRONG"] += nw
            samples = []
            for pdf, (old, new, cls) in list(sorted(changes.items()))[:4]:
                samples.append(f"pdf{pdf}:{old}→{new}[{cls}]")
            report.append({"book": book, "src": vol_short(src), "conf": stats["conf"],
                           "FILL": nf, "WRONG": nw, "samples": samples})

    report.sort(key=lambda r: (r["FILL"] + r["WRONG"]), reverse=True)
    print(f"可修复卷: {len(all_changes)}   FILL(补缺): {grand['FILL']}   WRONG(纠错): {grand['WRONG']}   合计: {grand['FILL']+grand['WRONG']}")
    print(f"不可靠卷(留给Phase2视觉): {len(skipped_unreliable)}   涉及问题页: {sum(v['deferred'] for v in skipped_unreliable)}")
    print("\n=== 变更最多的卷 (top 25) ===")
    for r in report[:25]:
        print(f"  [{r['book']}] FILL={r['FILL']:>3} WRONG={r['WRONG']:>3} conf={r['conf']:.2f}  {r['src'][:40]}  e.g. {'; '.join(r['samples'][:3])}")

    if args.report_json:
        payload = {"grand": dict(grand), "n_reliable_vols": len(all_changes),
                   "reliable_report": report, "deferred_unreliable": skipped_unreliable,
                   "changes": {src: {str(k): v for k, v in ch.items()} for src, ch in all_changes.items()}}
        Path(args.report_json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n变更清单 → {args.report_json}")

    if not args.apply:
        print("\n[dry-run] 未落库。加 --apply 写入。")
        con.close(); return

    backup = Path(args.db + ".bak-pagefix-all")
    shutil.copy2(args.db, backup)
    print(f"\n已备份: {backup}")
    npg = ntoc = 0
    for src, changes in all_changes.items():
        for pdf, (old, new, cls) in changes.items():
            cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?", (new, src, pdf))
            npg += cur.rowcount
    # 对齐 toc_entries 的非 pre 印刷页码
    for src in all_changes:
        toc = cur.execute("SELECT rowid, pdf_page, printed_page FROM toc_entries WHERE source_file=?", (src,)).fetchall()
        pmap = dict(cur.execute("SELECT pdf_page, printed_page FROM pages WHERE source_file=?", (src,)).fetchall())
        for rowid, pdf, printed in toc:
            if is_pre(printed):
                continue
            newp = pmap.get(pdf)
            if newp is not None and not is_pre(newp) and str(newp) != str(printed):
                cur.execute("UPDATE toc_entries SET printed_page=? WHERE rowid=?", (newp, rowid))
                ntoc += cur.rowcount
    con.commit(); con.close()
    print(f"已更新 pages 行: {npg}；toc_entries 行: {ntoc}")
    # 重算 sha256 (LF)
    h = hashlib.sha256(Path(args.db).read_bytes()).hexdigest()
    Path(args.db + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    print(f"新 sha256: {h}")


if __name__ == "__main__":
    main()
