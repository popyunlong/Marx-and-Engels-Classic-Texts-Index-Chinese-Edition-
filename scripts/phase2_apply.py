# -*- coding: utf-8 -*-
"""Phase2 落库：把 GLM-4V 读到的印刷页码(phase2_folios.jsonl)按「安全闸」写回 corpus.sqlite。

每页决策（vision=v, 记录=rec, 共识=cons，均为 int 或 None）：
  · v==cons 且 v!=rec   → 采纳 v（视觉+共识两法独立吻合，最硬；毛文集试点 39/39 命中）
  · v==rec              → 记录本就对（视觉证实），不动（吸收混进来的假 WRONG）
  · v 是第三值           → 仅当 v 与前后「已确认锚」单调一致才采纳（挡住把年份/脚注误读）
  · v==None 且 missing   → 用清洗后的锚做严格顺序插值补页码；补不上则维持缺失（确系无页码）
  · v==None 且 wrong     → 维持记录（不猜）
最后总体单调安全网：任何采纳值若使卷内页码相对已确认锚出现下降，回退不改。
锚 = 未列入问题清单的正文页(现有记录值) + 本轮已采纳的 v==cons / v==rec 值。

用法：
  python scripts/phase2_apply.py                 # 干跑报告
  python scripts/phase2_apply.py --report-json X
  python scripts/phase2_apply.py --apply [--db ...]
"""
from __future__ import annotations
import argparse, bisect, hashlib, json, shutil, sqlite3, unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "corpus.sqlite"
SIDECAR = ROOT / "data" / "phase2_folios.jsonl"
FW = str.maketrans("０１２３４５６７８９", "0123456789")


def to_int(p):
    if p is None:
        return None
    s = unicodedata.normalize("NFKC", str(p)).translate(FW).strip()
    if s.startswith("pre-"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def load_sidecar():
    by_src = defaultdict(dict)
    if not SIDECAR.exists():
        raise SystemExit("缺少 data/phase2_folios.jsonl（先跑 phase2_vision.py）")
    for line in SIDECAR.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        if o.get("error"):
            continue
        by_src[o["src"]][int(o["pdf"])] = o
    return by_src


def decide_volume(rows, sc):
    """rows:[(pdf,printed)] asc；sc: {pdf: sidecar_row}。返回 {pdf:(old,new,reason)}。"""
    printed = {pdf: pr for pdf, pr in rows}
    order = [pdf for pdf, _ in rows]
    problem = set(sc)
    # 锚：非问题页的记录 int
    anchors = {pdf: to_int(pr) for pdf, pr in rows if pdf not in problem and to_int(pr) is not None}

    changes = {}
    # 第一轮：v==cons / v==rec（强证据），先定，充实锚
    for pdf in order:
        o = sc.get(pdf)
        if not o:
            continue
        v = o.get("vision"); rec = to_int(o.get("recorded")); cons = o.get("consensus")
        if v is None:
            continue
        if cons is not None and v == cons and v != rec:
            changes[pdf] = (o.get("recorded"), str(v), "vis=cons")
            anchors[pdf] = v
        elif v == rec:
            anchors[pdf] = v  # 记录证实为真锚，不改

    def nearest(pdf):
        xs = sorted(anchors)
        i = bisect.bisect_left(xs, pdf)
        lo = anchors[xs[i - 1]] if i > 0 else None
        hi = anchors[xs[i]] if i < len(xs) else None
        return lo, hi

    # 第二轮：第三值 vision → 单调一致才采纳
    for pdf in order:
        o = sc.get(pdf)
        if not o or pdf in changes:
            continue
        v = o.get("vision"); rec = to_int(o.get("recorded"))
        if v is None or v == rec:
            continue
        lo, hi = nearest(pdf)
        if (lo is None or v >= lo) and (hi is None or v <= hi):
            changes[pdf] = (o.get("recorded"), str(v), "vis-mono")
            anchors[pdf] = v

    # 第三轮：missing 且 vision=None → 严格顺序插值
    order_idx = {pdf: k for k, pdf in enumerate(order)}
    for pdf in order:
        o = sc.get(pdf)
        if not o or pdf in changes:
            continue
        v = o.get("vision"); rec = to_int(o.get("recorded"))
        if v is not None or rec is not None:
            continue  # 只补真正缺失且视觉也读不到的
        k = order_idx[pdf]
        # 找左右最近锚（含已采纳），要求等差相邻
        lo = hi = None; lo_pdf = hi_pdf = None
        for d in range(1, 40):
            if lo is None and k - d >= 0 and order[k - d] in anchors:
                lo = anchors[order[k - d]]; lo_pdf = order[k - d]
            if hi is None and k + d < len(order) and order[k + d] in anchors:
                hi = anchors[order[k + d]]; hi_pdf = order[k + d]
            if lo is not None and hi is not None:
                break
        if lo is not None and hi is not None and (hi - lo) == (hi_pdf - lo_pdf):
            val = lo + (pdf - lo_pdf)
            if val >= 1:
                changes[pdf] = (o.get("recorded"), str(val), "interp")
                anchors[pdf] = val

    # 安全网：去掉使序列相对确认锚下降的采纳
    final = dict(anchors)
    for pdf, (_, new, _) in changes.items():
        final[pdf] = to_int(new)
    safe = {}
    for pdf, ch in changes.items():
        k = order_idx[pdf]; v = to_int(ch[1]); ok = True
        for d in range(1, 8):
            if k - d >= 0 and order[k - d] in final and final[order[k - d]] is not None:
                if v < final[order[k - d]] - 0:
                    ok = False
                break
        for d in range(1, 8):
            if k + d < len(order) and order[k + d] in final and final[order[k + d]] is not None:
                if v > final[order[k + d]] + 0:
                    ok = False
                break
        if ok:
            safe[pdf] = ch
    return safe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--report-json", default="")
    args = ap.parse_args()

    by_src = load_sidecar()
    con = sqlite3.connect(args.db); cur = con.cursor()
    all_changes = {}
    grand = Counter()
    report = []
    for src, sc in by_src.items():
        rows = cur.execute("SELECT pdf_page, printed_page FROM pages WHERE source_file=? ORDER BY pdf_page", (src,)).fetchall()
        book = cur.execute("SELECT book FROM pages WHERE source_file=? LIMIT 1", (src,)).fetchone()[0]
        ch = decide_volume(rows, sc)
        if ch:
            all_changes[src] = ch
            reasons = Counter(v[2] for v in ch.values())
            for r, n in reasons.items():
                grand[r] += n
            samples = [f"pdf{pdf}:{o}->{n}[{r}]" for pdf, (o, n, r) in list(sorted(ch.items()))[:3]]
            report.append({"book": book, "src": src.split("/")[-1], "n": len(ch),
                           "reasons": dict(reasons), "samples": samples})
    report.sort(key=lambda r: r["n"], reverse=True)
    print(f"采纳变更: {sum(len(c) for c in all_changes.values())}  分解: {dict(grand)}")
    print("\n=== 变更最多的卷 (top 25) ===")
    for r in report[:25]:
        print(f"  [{r['book']}] n={r['n']:>3} {r['reasons']}  {r['src'][:34]}  {'; '.join(r['samples'][:2])}")

    if args.report_json:
        Path(args.report_json).write_text(json.dumps(
            {"grand": dict(grand), "report": report,
             "changes": {s: {str(k): v for k, v in c.items()} for s, c in all_changes.items()}},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n清单 → {args.report_json}")

    if not args.apply:
        print("\n[dry-run] 未落库。")
        con.close(); return

    backup = Path(args.db + ".bak-phase2")
    shutil.copy2(args.db, backup)
    print(f"\n已备份: {backup}")
    npg = ntoc = 0
    for src, ch in all_changes.items():
        for pdf, (old, new, r) in ch.items():
            cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?", (new, src, pdf))
            npg += cur.rowcount
    for src in all_changes:
        toc = cur.execute("SELECT rowid, pdf_page, printed_page FROM toc_entries WHERE source_file=?", (src,)).fetchall()
        pmap = dict(cur.execute("SELECT pdf_page, printed_page FROM pages WHERE source_file=?", (src,)).fetchall())
        for rowid, pdf, pr in toc:
            if pr is not None and str(pr).startswith("pre-"):
                continue
            newp = pmap.get(pdf)
            if newp is not None and not str(newp).startswith("pre-") and str(newp) != str(pr):
                cur.execute("UPDATE toc_entries SET printed_page=? WHERE rowid=?", (newp, rowid))
                ntoc += cur.rowcount
    con.commit(); con.close()
    print(f"已更新 pages 行: {npg}；toc_entries 行: {ntoc}")
    h = hashlib.sha256(Path(args.db).read_bytes()).hexdigest()
    Path(args.db + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    print(f"新 sha256: {h}")


if __name__ == "__main__":
    main()
