# -*- coding: utf-8 -*-
"""残余错页码确定性清理：只动『干净单调卷里 offset 大幅偏离本地共识』的页——
这类 recorded 是明确 garbage（年份/脚注号/碎裂数字误当页码，如 615→1859），
而卷本身 offset 稳定，故本地共识值可信，**无需自证即可纠正**（区别于 Phase1 的保守自证要求）。

判据（全满足）：
  1) 卷 reliable（fpa: 非低置信 且 OK 页 offset 单调）；
  2) |（pdf−recorded）− 本地 smooth_off| > DEV(=20)  → recorded 明确是 garbage（非 ±1 边界）；
  3) target=pdf−smooth_off ≥1 且落在前后最近『干净锚(OK页)』之间（单调一致）→ 纠正为 target；
     若确系 garbage 却无法落在锚间（低置信段）→ 置 NULL（诚实「按PDF页码」，去掉误导性假页码）。
低置信卷(毛选扫描卷等假阳性)一律不动。

用法：python scripts/fix_garbage_pagenums.py [--report-json X] [--db ..] [--apply]
"""
from __future__ import annotations
import argparse, bisect, hashlib, json, re, shutil, sqlite3, sys
from collections import Counter
from pathlib import Path

FRONT_SIG = re.compile(r"目录|图书在版编目|责任编辑|策划编辑|S22PDF|ISBN|开本|印张|新华书店|出版发行|版权所有")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import importlib.util
_s = importlib.util.spec_from_file_location("fpa", ROOT / "scripts" / "fix_printed_pages_all.py")
fpa = importlib.util.module_from_spec(_s); _s.loader.exec_module(fpa)

DB = ROOT / "data" / "corpus.sqlite"
DEV = 20  # offset 偏离本地共识超过此值=明确 garbage


def collect(rows, per, stats, src="", vision=None):
    """返回 {pdf:(old,new_or_None,reason)}；new=None 表示置 NULL。
    vision: {(src,pdf): folio} —— 若视觉在该页读到的值 == recorded，视为『真实(章节重编页)』不动，
    避免把 pdf84=1 这类真重编页误纠成 57。但 recorded 为年份/巨号(>=1500)时铁定 garbage，无视视觉。"""
    vision = vision or {}
    reliable = (not stats["low_conf"]) and stats["mono"]
    if not reliable:
        return {}
    anchors = sorted((p["pdf"], fpa.to_int(p["recorded"])) for p in per
                     if p["cls"] == "OK" and fpa.to_int(p["recorded"]) is not None)
    xs = [a[0] for a in anchors]
    raw_by_pdf = {r[0]: (r[2] or "") for r in rows}
    out = {}
    for p in per:
        rec = fpa.to_int(p["recorded"])
        if rec is None or p["cls"] in ("FRONT",):
            continue
        if FRONT_SIG.search(raw_by_pdf.get(p["pdf"], "")[:120]):
            continue  # 版权/目录/CIP 页无正文折页码, 不纠
        dev = abs((p["pdf"] - rec) - p["smooth_off"])
        if dev <= DEV:
            continue  # 不是明确 garbage
        # 视觉证实 recorded 为真(且非年份/巨号)→ 章节重编页, 不动
        if rec < 1500:
            v = vision.get((src, p["pdf"]))
            if v is not None and v == rec:
                continue
        target = p["target"]
        # 最近干净锚
        i = bisect.bisect_left(xs, p["pdf"])
        lo = anchors[i - 1][1] if i > 0 else None
        hi = anchors[i][1] if i < len(anchors) else None
        if target and target >= 1 and (lo is None or target >= lo) and (hi is None or target <= hi):
            out[p["pdf"]] = (p["recorded"], str(target), "garbage→共识")
        else:
            out[p["pdf"]] = (p["recorded"], None, "garbage→NULL")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--report-json", default="")
    args = ap.parse_args()
    con = sqlite3.connect(args.db); cur = con.cursor()
    # 载入 Phase2 视觉结果，用于甄别「真章节重编页」(视觉读到的值==recorded)
    vision = {}
    vp = ROOT / "data" / "phase2_folios.jsonl"
    if vp.exists():
        for line in vp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                if not o.get("error") and o.get("vision") is not None:
                    vision[(o["src"], int(o["pdf"]))] = o["vision"]
            except Exception:
                pass
    all_changes = {}; grand = Counter(); report = []
    srcs = [r[0] for r in cur.execute("SELECT DISTINCT source_file FROM pages").fetchall()]
    for src in srcs:
        rows = cur.execute("SELECT pdf_page, printed_page, raw_text FROM pages WHERE source_file=? ORDER BY pdf_page", (src,)).fetchall()
        book = cur.execute("SELECT book FROM pages WHERE source_file=? LIMIT 1", (src,)).fetchone()[0]
        if book in fpa.DEFER_BOOKS:  # 五年规划=汇编大页码(offset 数百, 超本法量程), 既有 recorded 多为真页码, 勿动
            continue
        per, st = fpa.analyze_volume(rows)
        ch = collect(rows, per, st, src=src, vision=vision)
        if ch:
            all_changes[src] = ch
            for _, (o, n, r) in ch.items():
                grand["FIX" if n is not None else "NULL"] += 1
            samp = [f"pdf{pdf}:{o}→{n}" for pdf, (o, n, r) in list(sorted(ch.items()))[:3]]
            report.append({"book": book, "src": src.split("/")[-1], "n": len(ch), "samples": samp})
    report.sort(key=lambda r: r["n"], reverse=True)
    print(f"残余 garbage 页码: 纠正 {grand['FIX']}  置NULL {grand['NULL']}  合计 {sum(grand.values())}  涉及 {len(all_changes)} 卷")
    for r in report[:20]:
        print(f"  [{r['book']}] n={r['n']:>2}  {r['src'][:36]}  {'; '.join(r['samples'])}")
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(
            {"changes": {s: {str(k): v for k, v in c.items()} for s, c in all_changes.items()}},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"清单 → {args.report_json}")
    if not args.apply:
        print("\n[dry-run] 未落库。"); con.close(); return
    shutil.copy2(args.db, args.db + ".bak-garbage"); print("已备份")
    npg = ntoc = 0
    for src, ch in all_changes.items():
        for pdf, (o, n, r) in ch.items():
            cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?", (n, src, pdf)); npg += cur.rowcount
    for src in all_changes:
        pmap = dict(cur.execute("SELECT pdf_page, printed_page FROM pages WHERE source_file=?", (src,)).fetchall())
        for rowid, pdf, pr in cur.execute("SELECT rowid, pdf_page, printed_page FROM toc_entries WHERE source_file=?", (src,)).fetchall():
            if pr is not None and str(pr).startswith("pre-"): continue
            newp = pmap.get(pdf)
            if str(newp) != str(pr) and not (newp is not None and str(newp).startswith("pre-")):
                cur.execute("UPDATE toc_entries SET printed_page=? WHERE rowid=?", (newp, rowid)); ntoc += cur.rowcount
    con.commit(); con.close()
    print(f"已更新 pages 行: {npg}；toc_entries 行: {ntoc}")
    h = hashlib.sha256(Path(args.db).read_bytes()).hexdigest()
    Path(args.db + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    print(f"新 sha256: {h}")


if __name__ == "__main__":
    main()
