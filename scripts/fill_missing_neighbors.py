# -*- coding: utf-8 -*-
"""在清洗后的干净锚上重跑「缺页码邻页插值」——消除可恢复的『按PDF页码』。
复用 build_index.fill_missing_printed_pages（严格等差 方案1 + 空白页感知 方案2），
但作用于 Phase1/2/garbage 清洗后的当前 DB（原构建期是在含垃圾锚上跑的，故能多补一批）。
只补『两侧干净锚正好接得上』的缺页，绝不臆造。

用法：python scripts/fill_missing_neighbors.py [--report-json X] [--db ..] [--apply]
"""
from __future__ import annotations
import argparse, hashlib, json, shutil, sqlite3, sys, unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from build_index import fill_missing_printed_pages  # noqa

DB = ROOT / "data" / "corpus.sqlite"
FW = str.maketrans("０１２３４５６７８９", "0123456789")


def is_missing(pr):
    return pr is None or str(pr).strip() == "" or str(pr).startswith("pre-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--report-json", default="")
    args = ap.parse_args()
    con = sqlite3.connect(args.db); cur = con.cursor()
    srcs = [r[0] for r in cur.execute("SELECT DISTINCT source_file FROM pages").fetchall()]
    changes = {}  # (src,pdf) -> new
    by_book = Counter()
    for src in srcs:
        book = cur.execute("SELECT book FROM pages WHERE source_file=? LIMIT 1", (src,)).fetchone()[0]
        rows = cur.execute(
            "SELECT book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text "
            "FROM pages WHERE source_file=? ORDER BY pdf_page", (src,)).fetchall()
        rows = [list(r) for r in rows]
        before = {r[3]: r[4] for r in rows}
        fill_missing_printed_pages(rows)  # 就地补 printed(index 4)
        for r in rows:
            pdf, newp = r[3], r[4]
            old = before[pdf]
            if is_missing(old) and not is_missing(newp) and str(newp) != str(old):
                changes[(src, pdf)] = str(newp)
                by_book[book] += 1
    total = len(changes)
    print(f"邻页插值可补缺页码: {total} 处，涉及 {len({s for s,_ in changes})} 卷")
    for b, n in by_book.most_common(15):
        print(f"   {n:>4}  {b}")
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(
            {"changes": {f"{s}\t{p}": v for (s, p), v in changes.items()}}, ensure_ascii=False), encoding="utf-8")
    if not args.apply:
        print("\n[dry-run] 未落库。"); con.close(); return
    shutil.copy2(args.db, args.db + ".bak-fill"); print("已备份")
    npg = 0
    for (src, pdf), newp in changes.items():
        cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?", (newp, src, pdf)); npg += cur.rowcount
    con.commit(); con.close()
    print(f"已更新 pages 行: {npg}")
    h = hashlib.sha256(Path(args.db).read_bytes()).hexdigest()
    Path(args.db + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    print(f"新 sha256: {h}")


if __name__ == "__main__":
    main()
