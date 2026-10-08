"""Append approved book packages inside the immutable application transaction."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from book_data_release import Bundle,binding,canonical,file_hash,metadata


def resolve_parent(root,live_app,live):
    selected=live.get('book_data_release')
    if selected:
        bundle=Bundle(root/'data/book-data-releases'/selected['id'],selected['sha256'])
        return bundle.path('database'),bundle.path('config'),bundle.catalog(),bundle.manifest['text_only_sources']
    from catalog_release import Catalog
    selected=live.get('corpus_release')
    db=root/'data/corpus-releases'/selected['id']/'corpus.sqlite' if selected else root/'data/corpus.sqlite'
    selected=live.get('catalog_release')
    catalog=Catalog(root/'data/catalog-releases'/selected['id'],selected['sha256']) if selected else None
    return db,live_app/'config',catalog,[]


def prepare(root,app):
    root,app=Path(root),Path(app)
    live=metadata(root/'current/app');candidate=metadata(app)
    if candidate['parent_release_id']!=live['release_id']:
        raise ValueError('live parent changed before book import')
    for field in ('book_data_release','book_data_catalog','book_import_batch'):
        if field in live:candidate[field]=live[field]
    specfile=app/'config/state_documents_batch.json'
    if specfile.exists():
        spec=json.loads(specfile.read_text('utf-8'))
        if spec['id']!=live.get('book_import_batch'):
            if spec['expected_parent_release']!=live['release_id']:
                raise ValueError('reviewed batch belongs to another live release')
            incoming=Path('/home/data/marx-state-documents')/spec['id']
            package_file=incoming/'packages.json'
            if file_hash(package_file)!=spec['packages_sha256']:
                raise ValueError('approved packages changed')
            packages=json.loads(package_file.read_text('utf-8'))
            if {p['source_sha256'] for p in packages}!=set(spec['pdf_sha256']):
                raise ValueError('approved PDF membership differs')
            for p in packages:
                expected='pdfs/自动入库/'+p['source_sha256']+'.pdf'
                if p['source_file']!=expected or p.get('issues') or not p.get('audit',{}).get('release_reviewed'):
                    raise ValueError('unreviewed package or unsafe PDF path')
                if file_hash(root/expected)!=p['source_sha256']:
                    raise ValueError('shared PDF fingerprint differs')
            db,config,catalog,old_text=resolve_parent(root,root/'current/app',live)
            versions=root/'data/book-data-releases';versions.mkdir(exist_ok=True)
            versions.chmod(0o755)
            # Production data is a bind mount: realpath keeps /opt/... even
            # though the actual storage is /home/data on the data device.
            if versions.stat().st_dev!=Path('/home/data').stat().st_dev:
                raise ValueError('book versions must reside on data disk')
            disk=shutil.disk_usage(versions)
            # Two full DBs, reviewed HTML catalogue, input packages and headroom.
            estimated=Path(db).stat().st_size*2+sum(p.stat().st_size for p in incoming.rglob('*') if p.is_file())+(2<<30)
            if estimated>(10<<30) or disk.free-estimated<(15<<30):
                raise OSError('book import peak would exceed task budget or reserve')
            # SQLite's full old/new-row comparison can spill to disk. Keep
            # those transient sort files on the same budgeted data device.
            scratch=incoming/'sqlite-temp'
            scratch.mkdir(mode=0o700,exist_ok=True)
            os.environ['SQLITE_TMPDIR']=str(scratch)
            version=spec['id']+'-'+candidate['release_id'][-8:]
            from ingestion.book_data import build
            selected=build(packages,app,versions/version,baseline=db,config=config,pdfs=root/'pdfs',
                parent=live.get('book_data_release'),old_catalog=catalog,text_only_sources=old_text)
            bundle=Bundle(versions/version,selected['sha256'])
            candidate.update(book_data_release=selected,book_data_catalog=bundle.manifest['catalog'],
                book_import_batch=spec['id'],release_type='new_books')
    (app.parent/'release.json').write_bytes(canonical(candidate))
    verify(root,app)
    return candidate


def verify(root,app):
    selected=binding(app)
    if not selected:return
    bundle=Bundle(Path(root)/'data/book-data-releases'/selected['id'],selected['sha256'])
    if bundle.manifest['catalog']!=metadata(app).get('book_data_catalog'):
        raise ValueError('book corpus/catalogue generation mismatch')
    with sqlite3.connect(bundle.path('database').as_uri()+'?mode=ro',uri=True) as c:
        bundle.catalog().verify_corpus(c)


def accept(root,app):
    verify(root,app);selected=binding(app)
    if selected:
        path=Path(root)/'data/book-data-accepted'/(selected['id']+'.json')
        path.parent.mkdir(exist_ok=True)
        path.parent.chmod(0o755)
        if path.exists() and json.loads(path.read_text('utf-8'))!=selected:
            raise ValueError('immutable data acceptance differs')
        path.write_bytes(canonical(selected));path.chmod(0o644)


def rollback_guard(root,app):
    live=metadata(Path(root)/'current/app');target=metadata(app)
    if live.get('book_data_release')!=target.get('book_data_release'):
        if live.get('release_type')!='new_books' or live.get('parent_release_id')!=target.get('release_id'):
            raise ValueError('book rollback requires exact direct predecessor')
        selected=live['book_data_release']
        current=Bundle(Path(root)/'data/book-data-releases'/selected['id'],selected['sha256'])
        baseline,_,_,_=resolve_parent(Path(root),Path(app),target)
        if current.manifest['baseline_sha256']!=file_hash(baseline):
            raise ValueError('rollback baseline changed')
    verify(root,app)


def review(app,evidence):
    selected=binding(app)
    if not selected:return
    if evidence.get('book_data_release')!=selected:
        raise ValueError('book functional review belongs to another data release')
    # Appending books has its own acceptance policy. The stricter OCR-repair
    # browser/geometry rules in corpus_release remain unchanged.
    from corpus_release import FUNCTIONS
    if evidence.get('result')!='pass' or any(evidence.get('checks',{}).get(k) is not True for k in FUNCTIONS):
        raise ValueError('full book functional acceptance is incomplete')
    if not {'desktop','mobile'}<=set(evidence.get('browsers',[])) or evidence.get('rollback_rehearsal') is not True:
        raise ValueError('book desktop/mobile review or rollback rehearsal is missing')
    for name in ('ai_guide','citation_annotation_agent','dictionary_map'):
        if evidence.get('checks',{}).get(name) is not True:
            raise ValueError('book functional check missing: '+name)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','verify','accept','rollback'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--app',type=Path,required=True)
    a=p.parse_args();{'prepare':prepare,'verify':verify,'accept':accept,'rollback':rollback_guard}[a.action](a.root,a.app)
