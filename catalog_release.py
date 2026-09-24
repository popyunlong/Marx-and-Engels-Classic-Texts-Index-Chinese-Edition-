"""Immutable catalogue snapshots. Missing or stale bound data must never fall back."""
from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath

FIELDS = ('book', 'volume', 'source_file', 'title', 'pdf_page', 'printed_page',
          'level', 'kind', 'sort_order')
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
ROOT = Path(__file__).resolve().parent


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def toc_rows(rows):
    # Stable sorting retains the order of distinct entries sharing sort_order.
    return sorted(({k: r[k] for k in FIELDS} for r in rows),
                  key=lambda r: (r['source_file'], r['sort_order']))


def read_db_toc(conn):
    return toc_rows(dict(zip(FIELDS, row)) for row in conn.execute(
        'SELECT ' + ','.join(FIELDS) + ' FROM toc_entries ORDER BY source_file,sort_order,rowid'))


def safe_file(root, relative):
    p = PurePosixPath(relative)
    if not relative or p.is_absolute() or '..' in p.parts or '\\' in relative or ':' in relative:
        raise ValueError('unsafe catalogue path: ' + relative)
    target = Path(root).joinpath(*p.parts)
    if not target.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('catalogue path escapes root')
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents
                                  if parent != Path(root).parent):
        raise ValueError('catalogue symlinks are forbidden')
    return target


def inventory(root):
    root = Path(root).resolve()
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise ValueError('catalogue symlinks are forbidden')
        if p.is_file() and p != root / 'catalog.json':
            result[p.relative_to(root).as_posix()] = file_digest(p)
    return result


def validate_rows(rows, sources):
    known = {s['source_file']: s for s in sources}
    for row in rows:
        source = known.get(row['source_file'])
        if source is None or row['book'] != source['book'] or row['volume'] != source['volume']:
            raise ValueError('unknown/mismatched catalogue source')
        if type(row['level']) is not int or row['level'] < 1:
            raise ValueError('catalogue level must be a positive integer')
        if type(row['pdf_page']) is not int or not 1 <= row['pdf_page'] <= source['last_page']:
            raise ValueError('catalogue target outside PDF')
        if not str(row['title']).strip():
            raise ValueError('empty catalogue title')


class Catalog:
    def __init__(self, root, expected_sha=None):
        self.root = Path(root).resolve()
        manifest = self.root / 'catalog.json'
        if expected_sha and file_digest(manifest) != expected_sha:
            raise ValueError('catalogue manifest hash mismatch')
        self.manifest = json.loads(manifest.read_text(encoding='utf-8'))
        self.version = self.manifest['id']
        if (self.manifest.get('schema_version') != 1 or not ID_RE.fullmatch(self.version)
                or self.root.name != self.version):
            raise ValueError('invalid catalogue identity')
        actual = inventory(self.root)
        if actual != self.manifest['files']:
            raise ValueError('catalogue files missing, modified or unregistered')
        self.rows = toc_rows(json.loads((self.root / 'toc.json').read_text(encoding='utf-8')))
        self.sources = json.loads((self.root / 'sources.json').read_text(encoding='utf-8'))
        validate_rows(self.rows, self.sources)
        self.rows_by_source = {}
        for row in self.rows:
            self.rows_by_source.setdefault(row['source_file'], []).append(row)
        self.sha256 = file_digest(manifest)

    def verify_corpus(self, conn):
        if digest(read_db_toc(conn)) != self.manifest['input_toc_sha256']:
            raise ValueError('corpus catalogue changed since snapshot; rebuild against latest baseline')
        sources = [dict(zip(('book', 'volume', 'source_file', 'page_count', 'last_page'), row))
                   for row in conn.execute('SELECT book,volume,source_file,count(*),max(pdf_page) '
                                           'FROM pages GROUP BY book,volume,source_file ORDER BY source_file')]
        expected = [{k: s[k] for k in ('book', 'volume', 'source_file', 'page_count', 'last_page')}
                    for s in self.sources]
        if sorted(expected, key=lambda s: s['source_file']) != sources:
            raise ValueError('corpus sources changed; catalogue snapshot is stale')


def binding():
    # A checkout/CI fixture has no release identity. Deployed processes always
    # receive APP_RELEASE_FILE; a missing/invalid configured file is fatal.
    configured = os.environ.get('APP_RELEASE_FILE')
    metadata_path = Path(configured) if configured else ROOT.parent / 'release.json'
    if not configured and not metadata_path.is_file():
        return None
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    path = ROOT / 'config/catalog_release.json'
    if not path.exists():
        if metadata.get('catalog_release'):
            raise ValueError('release catalogue binding is missing from source')
        return None
    result = json.loads(path.read_text(encoding='utf-8'))
    if metadata.get('catalog_release') != result:
        raise ValueError('runtime catalogue binding does not match release metadata')
    if not ID_RE.fullmatch(result['id']) or not re.fullmatch('[0-9a-f]{64}', result['sha256']):
        raise ValueError('invalid catalogue release binding')
    return result


def catalog_root():
    return Path(os.environ.get('MARX_RUNTIME_DATA_DIR') or ROOT / 'data') / 'catalog-releases'


@lru_cache(maxsize=1)
def active_catalog():
    selected = binding()
    if selected is None:
        return None  # Legacy mode exists only before the first bound release.
    return Catalog(catalog_root() / selected['id'], selected['sha256'])


@lru_cache(maxsize=8)
def historic_catalog(version):
    if not ID_RE.fullmatch(version):
        raise ValueError('invalid catalogue version')
    active = active_catalog()
    if active and active.version == version:
        return active
    receipt = catalog_root().parent / 'catalog-accepted' / (version + '.json')
    approved = json.loads(receipt.read_text(encoding='utf-8'))
    if approved.get('id') != version:
        raise ValueError('historical catalogue receipt mismatch')
    return Catalog(catalog_root() / version, approved['sha256'])


def catalog_status():
    catalog = active_catalog()
    return {'id': catalog.version, 'sha256': catalog.sha256} if catalog else {'id': 'legacy', 'sha256': None}


def assert_legacy_catalog_write_allowed():
    if binding() is not None:
        raise RuntimeError('已绑定不可变目录版本；旧整库注入或目录重建入口禁止写入。请使用经审计的目录/入库发布事务。')
