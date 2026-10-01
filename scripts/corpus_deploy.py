"""Corpus checks inside the existing locked release/rollback transaction."""
from __future__ import annotations
import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from corpus_release import Bundle, binding, page_identity, verify_functional_review
from paddle_repair import file_hash, safe_path


def install_archive(archive, destination, selected):
    if destination.exists():
        return Bundle(destination, selected['sha256'])
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(archive,'r:gz') as tar:
        members=tar.getmembers()
        if len({m.name for m in members})!=len(members) or any(not m.isfile() for m in members):
            raise ValueError('corpus archive must contain unique regular files')
        size=sum(m.size for m in members)
        # Keep the current and rollback versions; never reclaim them to fit a candidate.
        disk=shutil.disk_usage(destination.parent)
        if size>16*1024**3 or disk.free-size<max(5*1024**3,disk.total*.1):
            raise ValueError('insufficient production space for candidate and rollback')
        staging=Path(tempfile.mkdtemp(prefix='.corpus-incoming-',dir=destination.parent))
        try:
            root=staging/destination.name
            root.mkdir()
            for member in members:
                path=safe_path(root,member.name)
                path.parent.mkdir(parents=True,exist_ok=True)
                with tar.extractfile(member) as source,path.open('xb') as out:
                    shutil.copyfileobj(source,out)
                path.chmod(0o444)
            Bundle(root,selected['sha256'])
            for directory in root.rglob('*'):
                if directory.is_dir():
                    directory.chmod(0o755)
            root.chmod(0o755)
            root.rename(destination)
        finally:
            shutil.rmtree(staging)
    return Bundle(destination,selected['sha256'])


def selected_bundle(root, app):
    selected=binding(app)
    return Bundle(Path(root)/'data/corpus-releases'/selected['id'],selected['sha256']) if selected else None


def preflight(root, app, archive=None):
    root,app=Path(root),Path(app)
    selected,previous=binding(app),binding(root/'current/app')
    if not selected:
        if previous:
            raise ValueError('cannot release an unbound application over versioned corpus')
        return
    metadata=json.loads((root/'current/release.json').read_text('utf-8'))
    if metadata.get('corpus_protocol')!=1:
        raise ValueError('deploy corpus compatibility foundation first')
    destination=root/'data/corpus-releases'/selected['id']
    candidate=install_archive(archive,destination,selected) if archive else Bundle(destination,selected['sha256'])
    current=selected_bundle(root,root/'current/app')
    baseline=current.path('database') if current else root/'data/corpus.sqlite'
    m=candidate.manifest
    if m.get('catalog_release')!=json.loads((app/'config/catalog_release.json').read_text('utf-8')):
        raise ValueError('catalogue binding mismatch')
    if page_identity(baseline)!=page_identity(candidate.path('database')):
        raise ValueError('OCR repair changed page identity, page labels or TOC')
    if selected!=previous:
        if m.get('parent')!=previous or m.get('baseline_sha256')!=file_hash(baseline):
            raise ValueError('stale corpus parent; rebase candidate on current generation')
    for source,record in m.get('pdfs',{}).items():
        if not source.startswith('pdfs/') or file_hash(safe_path((root/'pdfs').resolve(),source[5:]))!=record['sha256']:
            raise ValueError('candidate original PDF fingerprint mismatch: '+source)


def rollback_guard(root, app):
    root,app=Path(root),Path(app)
    current=selected_bundle(root,root/'current/app')
    target=selected_bundle(root,app)
    if not current and not target:
        return
    if target is None:
        metadata=json.loads((app.parent/'release.json').read_text('utf-8'))
        if metadata.get('corpus_protocol')!=1 or current.manifest.get('parent') is not None:
            raise ValueError('legacy rollback requires the first generation and a compatible foundation')
        target_db=root/'data/corpus.sqlite'
        if current.manifest.get('baseline_sha256')!=file_hash(target_db):
            raise ValueError('legacy corpus changed since baseline')
    else:
        target_db=target.path('database')
        if target.manifest.get('catalog_release')!=json.loads((app/'config/catalog_release.json').read_text('utf-8')):
            raise ValueError('rollback corpus/catalogue mismatch')
    if current and page_identity(current.path('database'))!=page_identity(target_db):
        raise ValueError('rollback would change stable page identities')


def review(app,evidence):
    selected=binding(app)
    if selected:
        verify_functional_review(evidence,selected)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['preflight','rollback','review','pack'])
    p.add_argument('--root',type=Path)
    p.add_argument('--app',type=Path)
    p.add_argument('--archive',type=Path)
    args=p.parse_args()
    if args.action=='preflight': preflight(args.root,args.app,args.archive)
    elif args.action=='rollback': rollback_guard(args.root,args.app)
    elif args.action=='review': review(args.app,json.load(sys.stdin))
    else:
        bundle=Bundle(args.root,file_hash(args.root/'manifest.json'))
        with tarfile.open(args.archive,'w:gz') as tar:
            for name in ['manifest.json',*sorted(bundle.manifest['files'])]:
                tar.add(bundle.root/name,arcname=name,recursive=False)


if __name__=='__main__': main()
