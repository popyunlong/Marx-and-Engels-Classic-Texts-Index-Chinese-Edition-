"""Build and seal a new corpus generation; never writes a live data pointer."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paddle_repair import Queue, canonical, digest, file_hash, readonly, safe_path, write_evidence, visible
from corpus_release import Bundle, ID, page_identity, verify_quality, verify_quality_binding


def copy_sqlite(source,target):
    with readonly(source) as src,sqlite3.connect(target) as dst:
        src.backup(dst,pages=256,sleep=.05)
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('invalid snapshot database')


def verify_source_snapshot(source):
    """Reject mixed or incomplete source components before allocating a candidate."""
    snapshot=json.loads((source/'manifest.json').read_text('utf-8'))
    files=snapshot.get('files',{})
    if (not isinstance(files,dict) or not {'corpus.sqlite','geometry.sqlite'} <= files.keys()
            or files.get('corpus.sqlite')!=snapshot.get('corpus_sha256')):
        raise ValueError('incomplete core snapshot binding')
    if (source/'subject_index.sqlite').exists() and 'subject_index.sqlite' not in files:
        raise ValueError('unbound subject index')
    for name,sha in files.items():
        path=safe_path(source,name)
        if path.is_symlink() or not path.is_file() or file_hash(path)!=sha:
            raise ValueError('core snapshot hash mismatch: '+name)
    proof=source/'auxiliary-manifest.json'
    if not proof.is_file() or proof.is_symlink():
        raise ValueError('verified auxiliary snapshot is required')
    auxiliary=json.loads(proof.read_text('utf-8'))
    if (auxiliary.get('schema_version')!=1
            or auxiliary.get('parent_snapshot_sha256')!=file_hash(source/'manifest.json')
            or auxiliary.get('app_release')!=snapshot.get('app_release')
            or auxiliary.get('catalog_release')!=snapshot.get('catalog_release')):
        raise ValueError('auxiliary snapshot belongs to a different core version')
    bound=auxiliary.get('files',{})
    if not isinstance(bound,dict) or not {'layout/manifest.json','catalog/catalog.json'} <= bound.keys():
        raise ValueError('incomplete auxiliary snapshot binding')
    for name,sha in bound.items():
        if not (name.startswith('layout/') or name=='catalog/catalog.json'):
            raise ValueError('invalid auxiliary component')
        path=safe_path(source,name)
        if path.is_symlink() or not path.is_file() or file_hash(path)!=sha:
            raise ValueError('auxiliary snapshot hash mismatch: '+name)
    if bound['catalog/catalog.json']!=snapshot['catalog_release']['sha256']:
        raise ValueError('catalogue binding mismatch')
    previous=json.loads((source/'layout/manifest.json').read_text('utf-8'))
    required={'layout/manifest.json','catalog/catalog.json'}
    for entry in previous['volumes'].values():
        for suffix in ('text','runs','evidence.json'):
            required.add('layout/'+entry['id']+'.'+suffix)
    if set(bound)!=required:
        raise ValueError('incomplete or unexpected auxiliary files')
    return snapshot


def apply_reviews(source, queue, output):
    """Only exact, source-reviewed page versions may change candidate text."""
    from build_index import normalize
    import ocr_geometry
    changes=[]
    with sqlite3.connect(output/'corpus.sqlite') as conn:
        conn.row_factory=sqlite3.Row
        for row in queue.conn.execute("SELECT p.*,r.result_hash AS reviewed_hash,r.evidence_json FROM pages p JOIN reviews r USING(page_id) WHERE p.state='accepted' AND r.decision='accepted' ORDER BY p.page_id"):
            path=Path(row['result_path'])
            if not path.resolve().is_relative_to(source.parent.resolve()) or file_hash(path)!=row['result_hash'] or row['reviewed_hash']!=row['result_hash']:
                raise ValueError('reviewed result identity mismatch')
            result=json.loads(path.read_text('utf-8'))
            old=conn.execute('SELECT * FROM pages WHERE id=?',(row['page_id'],)).fetchone()
            if old is None or digest(old['raw_text'])!=row['baseline_hash'] or result['baseline_hash']!=row['baseline_hash']:
                raise ValueError('page changed after OCR review')
            for key in ('book','volume','source_file','pdf_page','printed_page'):
                if result[key]!=old[key]:
                    raise ValueError('stable page identity changed: '+key)
            rows=result.get('geometry',[])
            if (result.get('geometry_aligned') is not True or result.get('geometry_precision') not in {'character','line'}
                    or visible(''.join(r['text'] for r in rows))!=visible(result['text'])):
                raise ValueError('corrected page needs proven line or character alignment')
            for geometry in rows:
                x0,y0,x1,y1=geometry['bbox']
                if not (0<=x0<x1<=1 and 0<=y0<y1<=1):
                    raise ValueError('invalid page geometry')
            assessment=ocr_geometry.upsert_geometry_page(output/'geometry.sqlite',source_file=old['source_file'],
                pdf_page=old['pdf_page'],corpus_text=result['text'],pdf_sha256=result['pdf_sha256'],
                page_width=result['width'],page_height=result['height'],rows=rows)
            if assessment['status']!='ready':
                raise ValueError('reviewed geometry fails quality thresholds')
            conn.execute('UPDATE pages SET raw_text=?,normalized_text=? WHERE id=?',
                         (result['text'],normalize(result['text']),old['id']))
            changes.append({'page_id':old['id'],'source_file':old['source_file'],'pdf_page':old['pdf_page'],
                            'before':row['baseline_hash'],'after':digest(result['text']),
                            'result_hash':row['result_hash'],'review':json.loads(row['evidence_json'])})
    return changes


def build(root,version,queue_name,baseline=False):
    if not ID.fullmatch(version):
        raise ValueError('invalid candidate identity')
    source=root/'source'
    os.environ['MARX_RUNTIME_DATA_DIR']=str(source)
    os.environ['MARX_RUNTIME_PDF_DIR']=str(source/'pdfs')
    snapshot=verify_source_snapshot(source)
    target=root/'candidates'/version
    target.mkdir(parents=False,exist_ok=False)
    # A failed build is retained for diagnosis and never gets manifest.json.
    shutil.copy2(source/'corpus.sqlite',target/'corpus.sqlite')
    copy_sqlite(source/'geometry.sqlite',target/'geometry.sqlite')
    if (source/'subject_index.sqlite').exists():
        shutil.copy2(source/'subject_index.sqlite',target/'subject_index.sqlite')
    queue=Queue(root/'jobs'/queue_name)
    queue.bind(snapshot)
    changes=[] if baseline else apply_reviews(source,queue,target)
    if not baseline and not changes:
        raise ValueError('no accepted changes to build')
    if page_identity(source/'corpus.sqlite')!=page_identity(target/'corpus.sqlite'):
        raise ValueError('candidate changed source identity or catalogue')
    for db in ('corpus.sqlite','geometry.sqlite'):
        with sqlite3.connect(target/db) as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute('PRAGMA journal_mode=DELETE')
    if baseline and file_hash(target/'corpus.sqlite')!=snapshot['corpus_sha256']:
        raise ValueError('baseline must be byte-identical to the current corpus')
    (target/'corpus.sqlite.sha256').write_text(file_hash(target/'corpus.sqlite'),'utf-8')
    write_evidence(target/'changes.json',changes)
    changed={c['source_file'] for c in changes}
    previous=json.loads((source/'layout/manifest.json').read_text('utf-8'))
    layout=target/'layout'
    layout.mkdir()
    manifest={'version':previous['version'],'volumes':{}}
    os.environ['MARX_RUNTIME_DATA_DIR']=str(source)
    os.environ['MARX_RUNTIME_PDF_DIR']=str(source/'pdfs')
    from search import Corpus
    from scripts.build_layout_exact import build_volume
    from layout_exact import volume_fingerprint
    corpus=Corpus(db_path=target/'corpus.sqlite')
    for sf,entry in previous['volumes'].items():
        volume=corpus.get_volume_by_source_file(sf)
        if volume is None:
            raise ValueError('layout source disappeared')
        if sf in changed:
            pdf=safe_path(source,sf)
            expected=snapshot['pdfs'][sf]
            if file_hash(pdf)!=expected['sha256']:
                raise ValueError('original PDF changed')
            entry=build_volume(corpus,volume,pdf,layout)
            entry['pdf_stat']=expected['stat']
        else:
            if volume_fingerprint(corpus,volume)!=entry['fingerprint']:
                raise ValueError('unchanged source layout fingerprint mismatch')
            for suffix in ('text','runs','evidence.json'):
                path=safe_path(source/'layout',entry['id']+'.'+suffix)
                if suffix in ('text','runs') and file_hash(path)!=entry[suffix+'_sha256']:
                    raise ValueError('source layout hash mismatch')
                shutil.copy2(path,layout/path.name)
        manifest['volumes'][sf]=entry
    for sf in changed-set(previous['volumes']):
        volume=corpus.get_volume_by_source_file(sf)
        pdf=safe_path(source,sf)
        expected=snapshot['pdfs'][sf]
        if file_hash(pdf)!=expected['sha256']:
            raise ValueError('original PDF changed')
        entry=build_volume(corpus,volume,pdf,layout)
        entry['pdf_stat']=expected['stat']
        manifest['volumes'][sf]=entry
    write_evidence(layout/'manifest.json',manifest)
    draft={'schema_version':1,'id':version,'parent':snapshot.get('corpus_release'),
           'source_snapshot_sha256':file_hash(source/'manifest.json'),
           'auxiliary_snapshot_sha256':file_hash(source/'auxiliary-manifest.json'),
           'catalog_release':snapshot['catalog_release'],'baseline_sha256':snapshot['corpus_sha256'],
           'changed_pages':[c['page_id'] for c in changes],'changed_sources':sorted(changed),
           'pdfs':{sf:snapshot['pdfs'][sf] for sf in changed},'page_identity':page_identity(target/'corpus.sqlite')}
    write_evidence(target/'draft.json',draft)
    return target


def seal(target,quality):
    if (target/'manifest.json').exists():
        raise ValueError('candidate already sealed')
    draft=json.loads((target/'draft.json').read_text('utf-8'))
    verify_quality(quality,draft['changed_sources'])
    verify_quality_binding(quality,target)
    write_evidence(target/'quality.json',quality)
    draft['files']={p.relative_to(target).as_posix():file_hash(p) for p in sorted(target.rglob('*')) if p.is_file()}
    write_evidence(target/'manifest.json',draft)
    sha=file_hash(target/'manifest.json')
    Bundle(target,sha)
    for p in target.rglob('*'):
        if p.is_file(): p.chmod(0o444)
    return {'id':draft['id'],'sha256':sha}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['build','seal'])
    p.add_argument('--root',required=True,type=Path)
    p.add_argument('--version',required=True)
    p.add_argument('--queue',default='pilot.sqlite')
    p.add_argument('--baseline',action='store_true')
    p.add_argument('--quality-file',type=Path)
    args=p.parse_args()
    if not ID.fullmatch(args.version) or Path(args.queue).name!=args.queue:
        raise ValueError('unsafe candidate or queue path')
    if args.action=='build': print(build(args.root,args.version,args.queue,args.baseline))
    else: print(json.dumps(seal(args.root/'candidates'/args.version,json.loads(args.quality_file.read_text('utf-8')))))


if __name__=='__main__': main()
