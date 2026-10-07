"""Small immutable graph artifacts; invoked only inside the release transaction."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dictionary_graph import Graph, binding, file_hash, readonly

MAX_BYTES=100*1024*1024
MEMBERS={"graph.sqlite","binding.json","review.json"}


def verify_review(review, selected, database=None):
    if review.get("graph_sha256")!=selected["sha256"] or review.get("source_sha256")!=selected["source_sha256"]:
        raise ValueError("review is not bound to this graph and dictionary")
    rows=review.get("rows",[])
    if len(rows)<100 or len({r.get("id") for r in rows})!=len(rows):
        raise ValueError("at least 100 distinct reviewed relations required")
    if any(r.get("quote_valid") is not True for r in rows):
        raise ValueError("review found unverified quotations")
    if sum(r.get("relation_valid") is True for r in rows)/len(rows)<.95:
        raise ValueError("relation review accuracy below 95 percent")
    if not review.get("reviewer") or not review.get("reviewed_at"):
        raise ValueError("review attribution missing")
    if database:
        with readonly(database) as c:
            known={r[0] for r in c.execute('SELECT id FROM edges')}
        if any(r['id'] not in known for r in rows):
            raise ValueError("review contains relations outside this graph")


def verify(root, app):
    selected=binding(app)
    if not selected:
        return None
    dest=Path(root)/"data/dictionary-graphs"/selected["id"]
    if json.loads((dest/"binding.json").read_text("utf-8"))!=selected:
        raise ValueError("installed dictionary graph binding mismatch")
    graph=Graph(dest/"graph.sqlite",selected,Path(app)/"data/dictionary.sqlite")
    review=json.loads((dest/"review.json").read_text("utf-8"))
    verify_review(review,selected,dest/"graph.sqlite")
    return graph


def install(root, app, archive=None):
    selected=binding(app)
    if not selected:
        if archive:
            raise ValueError("graph archive requires a committed binding")
        return
    dest=Path(root)/"data/dictionary-graphs"/selected["id"]
    if dest.exists():
        verify(root,app)
        return
    if not archive:
        raise ValueError("required dictionary graph archive missing")
    dest.parent.mkdir(parents=True,exist_ok=True)
    # The transaction uses umask 027; the www-data reader still needs traversal.
    dest.parent.chmod(0o755)
    with tarfile.open(archive,"r:gz") as tar:
        members=tar.getmembers()
        if len(members)!=3 or {m.name for m in members}!=MEMBERS or any(not m.isfile() for m in members):
            raise ValueError("graph archive must contain exactly three regular files")
        size=sum(m.size for m in members)
        if size>MAX_BYTES or any(m.size<0 for m in members):
            raise ValueError("graph archive exceeds 100 MiB")
        if shutil.disk_usage(dest.parent).free-size < 5*1024**3:
            raise ValueError("graph installation must leave at least 5 GiB free")
        with tempfile.TemporaryDirectory(prefix=".graph-incoming-",dir=dest.parent) as temp:
            staged=Path(temp)/selected["id"]
            staged.mkdir()
            for member in members:
                with tar.extractfile(member) as src,(staged/member.name).open("xb") as out:
                    shutil.copyfileobj(src,out)
            if json.loads((staged/"binding.json").read_text("utf-8"))!=selected:
                raise ValueError("graph archive binding mismatch")
            Graph(staged/"graph.sqlite",selected,Path(app)/"data/dictionary.sqlite")
            verify_review(json.loads((staged/"review.json").read_text("utf-8")),selected,staged/"graph.sqlite")
            for path in staged.iterdir():path.chmod(0o444)
            staged.chmod(0o755)
            staged.rename(dest)


def package(directory,output):
    directory,output=Path(directory),Path(output)
    selected=json.loads((directory/"binding.json").read_text("utf-8"))
    if file_hash(directory/"graph.sqlite")!=selected["sha256"]:
        raise ValueError("graph hash mismatch")
    verify_review(json.loads((directory/"review.json").read_text("utf-8")),selected,directory/"graph.sqlite")
    if sum((directory/n).stat().st_size for n in MEMBERS)>MAX_BYTES:
        raise ValueError("graph exceeds 100 MiB")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(output,"x:gz") as tar:
        for name in sorted(MEMBERS):tar.add(directory/name,arcname=name,recursive=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=("preflight","rollback","package","health"))
    p.add_argument("--root",type=Path)
    p.add_argument("--app",type=Path)
    p.add_argument("--archive",type=Path)
    p.add_argument("--directory",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--metadata",type=Path)
    a=p.parse_args()
    if a.command=="health":
        selected=json.loads(a.metadata.read_text('utf-8')).get('dictionary_graph_release')
        if selected:
            runtime=json.load(sys.stdin).get('dictionary_map') or {}
            if runtime.get('ready') is not True or runtime.get('version')!=selected['id']:
                raise ValueError('candidate dictionary graph is not ready at the committed version')
    elif a.command=="package":package(a.directory,a.output)
    elif a.command=="preflight":install(a.root,a.app,a.archive)
    else:verify(a.root,a.app)  # An older application without a graph binding needs no graph.


if __name__=="__main__":main()
