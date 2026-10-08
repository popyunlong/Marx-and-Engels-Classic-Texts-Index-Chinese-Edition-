"""A single immutable binding for appended corpus, bibliography and article TOC."""
from __future__ import annotations
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath

ROOT=Path(__file__).resolve().parent
ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
SHA=re.compile(r'[0-9a-f]{64}')


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):
            h.update(block)
    return h.hexdigest()


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def safe_path(root,name):
    p=PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
        raise ValueError('unsafe book data path')
    target=Path(root).joinpath(*p.parts)
    if not target.resolve().is_relative_to(Path(root).resolve()) or target.is_symlink():
        raise ValueError('book data path escapes immutable root')
    return target


def metadata(app=ROOT):
    explicit=os.environ.get('APP_RELEASE_FILE') if Path(app)==ROOT else None
    path=Path(explicit) if explicit else Path(app).parent/'release.json'
    if explicit and not path.is_file():
        raise ValueError('bound application metadata is missing')
    return json.loads(path.read_text('utf-8')) if path.exists() else {}


def binding(app=ROOT):
    value=metadata(app).get('book_data_release')
    if value is not None and (not isinstance(value,dict) or set(value)!={'id','sha256'}
            or not ID.fullmatch(str(value.get('id',''))) or not SHA.fullmatch(str(value.get('sha256','')))):
        raise ValueError('invalid book data binding')
    return value


class Bundle:
    def __init__(self,root,expected):
        self.root=Path(root).resolve()
        manifest=self.root/'book-data.json'
        if file_hash(manifest)!=expected:
            raise ValueError('book data fingerprint mismatch')
        self.manifest=json.loads(manifest.read_text('utf-8'))
        m=self.manifest
        if m.get('schema')!=1 or m.get('id')!=self.root.name or not ID.fullmatch(self.root.name):
            raise ValueError('invalid book data identity')
        files=m.get('files',{})
        required={'data/corpus.sqlite','data/corpus.sqlite.sha256','config/books.yaml',
                  'config/manifest.yaml','config/volumes.yaml','packages.json'}
        if not required<=files.keys():
            raise ValueError('book data bundle is incomplete')
        actual={p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file() and p!=manifest}
        if actual!=set(files):
            raise ValueError('unregistered or missing book data files')
        for name,sha in files.items():
            if not SHA.fullmatch(str(sha)) or file_hash(safe_path(self.root,name))!=sha:
                raise ValueError('book data component changed: '+name)
        if (self.root/'data/corpus.sqlite.sha256').read_text('ascii').strip()!=files['data/corpus.sqlite']:
            raise ValueError('book database sidecar mismatch')
        self.sha256=expected

    def path(self,kind):
        return self.root/({'database':'data/corpus.sqlite','config':'config'}[kind])

    def catalog(self):
        from catalog_release import Catalog
        value=self.manifest['catalog']
        return Catalog(self.root/'catalog'/value['id'],value['sha256'])


@lru_cache(maxsize=1)
def current():
    value=binding()
    if not value:
        return None
    root=Path(os.environ.get('MARX_RUNTIME_DATA_DIR') or ROOT/'data')/'book-data-releases'
    return Bundle(root/value['id'],value['sha256'])


def status():
    selected=binding()
    return selected or {'id':'legacy','sha256':None}


def append_ancestors(current_sha):
    """Read ancestry only from the verified bundle matching the loaded DB."""
    bundle=current()
    if bundle is None or 'data/ingestion-generations.json' not in bundle.manifest['files']:
        return {}
    proof=json.loads((bundle.root/'data/ingestion-generations.json').read_text('utf-8'))
    return proof.get('ancestors',{}) if proof.get('schema')==1 and proof.get('current')==current_sha else {}


def compatible_corpus(generation,current_sha):
    """Only immutable, verified append ancestry can resume a previous job."""
    return generation == current_sha or generation in append_ancestors(current_sha)


def text_only(source):
    # Manifest membership, rather than an arbitrary client flag, controls rendering.
    bundle=current()
    if bundle and source in bundle.manifest.get('text_only_sources',[]):
        return True
    return False


@lru_cache(maxsize=1)
def article_index():
    bundle=current()
    if not bundle or 'articles.json' not in bundle.manifest['files']:
        return {}
    rows=json.loads((bundle.root/'articles.json').read_text('utf-8'))
    return {(r['source_file'],int(r['pdf_page'])):r for r in rows}


def article_fields(source,page,title):
    row=article_index().get((source,int(page)))
    # Historic TOCs must never acquire attribution from a different title.
    if not row or re.sub(r'\W','',row['title'])!=re.sub(r'\W','',title):
        return {}
    return dict(authors=tuple(row.get('authors',[])),date=row.get('date',''),
                end_pdf_page=row['end_pdf_page'],evidence_method=row['evidence_method'],
                provenance_verified=bool(row.get('provenance_verified')))
