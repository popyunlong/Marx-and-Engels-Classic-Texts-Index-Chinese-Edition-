# -*- coding: utf-8 -*-
"""把 Phase1/Phase2 的页码变更清单(_pagefix_report.json + _phase2_apply_report.json)
定向应用到目标 corpus.sqlite（本地或服务器生产库均可）。纯 UPDATE，不重建、不碰其它字段。

  · 变更 = {source_file: {pdf_page: [old, new, reason]}}；后一个清单覆盖前一个（本无重叠）；
  · 同步把受影响卷的 toc_entries 非 pre 印刷页码对齐到修复后的 pages；
  · 重算 sha256(LF)；打印落库前后的『按PDF页码(缺印刷页)』计数供核验。

用法：
  python scripts/deploy_pagefix.py --changesets a.json,b.json            # 干跑
  python scripts/deploy_pagefix.py --changesets a.json,b.json --apply --db /opt/marx-search/data/corpus.sqlite
"""
from __future__ import annotations
import argparse, hashlib, json, shutil, sqlite3
from pathlib import Path


def load_changes(paths):
    merged = {}
    for p in paths:
        payload = json.loads(Path(p).read_text(encoding="utf-8"))
        for src, ch in payload.get("changes", {}).items():
            d = merged.setdefault(src, {})
            for pdf, triple in ch.items():
                d[int(pdf)] = triple  # [old,new,reason]
    return merged


def count_missing(cur):
    return cur.execute(
        "SELECT COUNT(*) FROM pages WHERE (printed_page IS NULL OR TRIM(printed_page)='') "
        "AND LENGTH(TRIM(normalized_text))>=40").fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--changesets", required=True, help="逗号分隔的 JSON 清单路径")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    changes = load_changes([p.strip() for p in args.changesets.split(",") if p.strip()])
    n_pages = sum(len(v) for v in changes.values())
    print(f"清单：{len(changes)} 卷，{n_pages} 页变更")

    con = sqlite3.connect(args.db); cur = con.cursor()
    before = count_missing(cur)
    # 预检：确认每个 (src,pdf) 存在且当前值 == old（探测漂移）
    drift = 0; missing_row = 0
    for src, ch in changes.items():
        cur_vals = dict(cur.execute("SELECT pdf_page, printed_page FROM pages WHERE source_file=?", (src,)).fetchall())
        for pdf, (old, new, reason) in ch.items():
            if pdf not in cur_vals:
                missing_row += 1
            else:
                cv = cur_vals[pdf]
                cvs = "" if cv is None else str(cv)
                if cvs != ("" if old is None else str(old)) and cvs != str(new):
                    drift += 1
    print(f"预检：缺行 {missing_row}，当前值既非 old 也非 new（漂移）{drift}")
    print(f"当前『按PDF页码(缺印刷页正文)』：{before}")

    if not args.apply:
        print("\n[dry-run] 未落库。")
        con.close(); return

    backup = Path(args.db + ".bak-deploy-pagefix")
    shutil.copy2(args.db, backup)
    print(f"已备份: {backup}")
    npg = ntoc = 0
    for src, ch in changes.items():
        for pdf, (old, new, reason) in ch.items():
            cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?", (str(new), src, pdf))
            npg += cur.rowcount
    for src in changes:
        toc = cur.execute("SELECT rowid, pdf_page, printed_page FROM toc_entries WHERE source_file=?", (src,)).fetchall()
        pmap = dict(cur.execute("SELECT pdf_page, printed_page FROM pages WHERE source_file=?", (src,)).fetchall())
        for rowid, pdf, pr in toc:
            if pr is not None and str(pr).startswith("pre-"):
                continue
            newp = pmap.get(pdf)
            if newp is not None and not str(newp).startswith("pre-") and str(newp) != str(pr):
                cur.execute("UPDATE toc_entries SET printed_page=? WHERE rowid=?", (str(newp), rowid))
                ntoc += cur.rowcount
    con.commit()
    after = count_missing(cur)
    con.close()
    print(f"已更新 pages 行: {npg}；toc_entries 行: {ntoc}")
    print(f"『按PDF页码』：{before} → {after}")
    h = hashlib.sha256(Path(args.db).read_bytes()).hexdigest()
    Path(args.db + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    print(f"新 sha256: {h}")


if __name__ == "__main__":
    main()
