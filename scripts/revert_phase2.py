# -*- coding: utf-8 -*-
"""回退 Phase2 视觉页码里「疑瞎编」的改动(data/_phase2_revert.json)——把 printed_page 恢复到
改动前的 old 值（FILL 的 old=None → 恢复缺失=诚实的『按PDF页码』；WRONG 的 old=原值）。
适配本地与服务器生产库（--db）。同步回退受影响卷的 toc_entries 到当前 pages 值，重算 sha256。

用法：
  python scripts/revert_phase2.py --db data/corpus.sqlite            # 干跑
  python scripts/revert_phase2.py --db <path> --apply
"""
from __future__ import annotations
import argparse, hashlib, json, shutil, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVERT = ROOT / "data" / "_phase2_revert.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--revert-json", default=str(REVERT))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    revert = json.loads(Path(args.revert_json).read_text(encoding="utf-8"))
    con = sqlite3.connect(args.db); cur = con.cursor()

    # 预检：确认当前值 == 我们落的 new（没被其它改动动过）
    drift = 0
    for r in revert:
        cv = cur.execute("SELECT printed_page FROM pages WHERE source_file=? AND pdf_page=?", (r["src"], r["pdf"])).fetchone()
        cvs = "" if (not cv or cv[0] is None) else str(cv[0])
        if cvs != str(r["new"]):
            drift += 1
    print(f"回退清单: {len(revert)} 处；预检当前值≠我们落的 new（漂移）: {drift}")
    srcs = sorted({r["src"] for r in revert})
    print(f"涉及卷: {len(srcs)}")

    if not args.apply:
        print("[dry-run] 未落库。")
        con.close(); return

    backup = Path(args.db + ".bak-revert-phase2")
    shutil.copy2(args.db, backup)
    print(f"已备份: {backup}")
    n = 0
    for r in revert:
        old = r["old"]
        val = None if (old is None or str(old) == "None") else str(old)
        cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?", (val, r["src"], r["pdf"]))
        n += cur.rowcount
    # toc 对齐回当前 pages
    ntoc = 0
    for src in srcs:
        toc = cur.execute("SELECT rowid, pdf_page, printed_page FROM toc_entries WHERE source_file=?", (src,)).fetchall()
        pmap = dict(cur.execute("SELECT pdf_page, printed_page FROM pages WHERE source_file=?", (src,)).fetchall())
        for rowid, pdf, pr in toc:
            if pr is not None and str(pr).startswith("pre-"):
                continue
            newp = pmap.get(pdf)
            newp_s = None if newp is None else str(newp)
            if (newp_s or "") != ("" if pr is None else str(pr)) and not (newp is not None and str(newp).startswith("pre-")):
                cur.execute("UPDATE toc_entries SET printed_page=? WHERE rowid=?", (newp_s, rowid))
                ntoc += cur.rowcount
    con.commit(); con.close()
    print(f"已回退 pages 行: {n}；toc_entries 行: {ntoc}")
    h = hashlib.sha256(Path(args.db).read_bytes()).hexdigest()
    Path(args.db + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    print(f"新 sha256: {h}")


if __name__ == "__main__":
    main()
