# -*- coding: utf-8 -*-
"""把 91 改动页的最终 raw/normalized 文本定向写入目标库(guaranteed 与本地一致)。"""
import argparse, hashlib, json, shutil, sqlite3
from pathlib import Path
ap=argparse.ArgumentParser(); ap.add_argument("--db",required=True); ap.add_argument("--pagetext",required=True); ap.add_argument("--apply",action="store_true")
a=ap.parse_args()
rows=json.loads(Path(a.pagetext).read_text(encoding="utf-8"))
con=sqlite3.connect(a.db); cur=con.cursor()
# 预检: 目标页存在、当前 norm != 新 norm(即确有改动)
miss=chg=0
for r in rows:
    cur.execute("SELECT normalized_text FROM pages WHERE source_file=? AND pdf_page=?",(r["src"],r["pdf"]))
    x=cur.fetchone()
    if not x: miss+=1
    elif x[0]!=r["norm"]: chg+=1
print(f"清单 {len(rows)} 页; 缺行 {miss}; 待改(当前norm≠新norm) {chg}")
if not a.apply:
    print("[dry-run]"); raise SystemExit
shutil.copy2(a.db, a.db+".bak-phase3-text"); print("已备份")
n=0
for r in rows:
    cur.execute("UPDATE pages SET raw_text=?, normalized_text=? WHERE source_file=? AND pdf_page=?",(r["raw"],r["norm"],r["src"],r["pdf"])); n+=cur.rowcount
con.commit(); con.close()
print("已更新 pages 行:", n)
h=hashlib.sha256(Path(a.db).read_bytes()).hexdigest()
Path(a.db+".sha256").write_text(h+"\n",encoding="utf-8",newline="\n"); print("新 sha256:",h)
