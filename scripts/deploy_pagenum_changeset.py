import argparse,hashlib,json,shutil,sqlite3
from pathlib import Path
ap=argparse.ArgumentParser();ap.add_argument("--db",required=True);ap.add_argument("--changeset",required=True);ap.add_argument("--apply",action="store_true")
a=ap.parse_args();ch=json.loads(Path(a.changeset).read_text(encoding="utf-8"))["changes"]
con=sqlite3.connect(a.db);cur=con.cursor()
n=sum(len(v) for v in ch.values());print(f"变更 {n} 页 / {len(ch)} 卷")
if not a.apply: print("[dry-run]");raise SystemExit
shutil.copy2(a.db,a.db+".bak-round5");print("已备份")
npg=ntoc=0
for src,c in ch.items():
    for pdf,val in c.items():
        cur.execute("UPDATE pages SET printed_page=? WHERE source_file=? AND pdf_page=?",(val,src,int(pdf)));npg+=cur.rowcount
for src in ch:
    pmap=dict(cur.execute("SELECT pdf_page,printed_page FROM pages WHERE source_file=?",(src,)).fetchall())
    for rowid,pdf,pr in cur.execute("SELECT rowid,pdf_page,printed_page FROM toc_entries WHERE source_file=?",(src,)).fetchall():
        if pr is not None and str(pr).startswith("pre-"): continue
        newp=pmap.get(pdf)
        if str(newp)!=str(pr) and not(newp is not None and str(newp).startswith("pre-")):
            cur.execute("UPDATE toc_entries SET printed_page=? WHERE rowid=?",(newp,rowid));ntoc+=cur.rowcount
con.commit();con.close()
print(f"已更新 pages {npg}; toc {ntoc}")
h=hashlib.sha256(Path(a.db).read_bytes()).hexdigest();Path(a.db+".sha256").write_text(h+"\n",encoding="utf-8",newline="\n");print("sha256:",h)
