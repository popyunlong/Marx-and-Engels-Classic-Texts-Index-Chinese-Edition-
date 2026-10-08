"""An application release pins corpus text, geometry and indexes together.

No binding means the unchanged legacy reader. A broken binding must raise;
falling back to another data generation would silently misplace citations.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from paddle_repair import canonical, file_hash, safe_path

ROOT = Path(__file__).resolve().parent
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
SHA = re.compile(r'[0-9a-f]{64}')
FUNCTIONS = ('search_exact','search_fuzzy','search_combined','search_single_book',
    'search_pagination','citation_copy','citation_formats','citation_export',
    'reader_pdf','reader_toc','reader_navigation','reader_find','reader_highlight',
    'legacy_links','ai_chat','ai_stream','ai_sources','bookmarks','notes',
    'login','membership_permissions','personal_library','library_entries','journal_status')


def binding(app=ROOT):
    path = Path(app)/'config/corpus_release.json'
    metadata_path = Path(app).parent/'release.json'
    metadata = json.loads(metadata_path.read_text('utf-8')) if metadata_path.exists() else {}
    selected = json.loads(path.read_text('utf-8')) if path.exists() else None
    if metadata and metadata.get('corpus_release') != selected:
        raise ValueError('corpus binding differs from application release')
    if selected is not None and (set(selected) != {'id','sha256'} or not ID.fullmatch(str(selected.get('id',''))) or not SHA.fullmatch(str(selected.get('sha256','')))):
        raise ValueError('invalid corpus release binding')
    return selected


def verify_quality(report, sources):
    for key in ('verified_characters','character_error_rate','known_error_reduction',
                'unreviewed_high_risk','geometry_samples','geometry_accuracy','wrong_book_or_page'):
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid quality metric: '+key)
    if (report.get('verified_characters',0)<20000 or report.get('character_error_rate',1)>.003
            or report.get('known_error_reduction',0)<.5 or report.get('no_quality_regression') is not True
            or report.get('unreviewed_high_risk',1)!=0 or report.get('geometry_samples',0)<150
            or report.get('geometry_accuracy',0)<.99 or report.get('wrong_book_or_page',1)!=0):
        raise ValueError('corpus quality acceptance is incomplete')
    reviewed = report.get('volume_samples',{})
    if any(type(reviewed.get(source)) is not int or reviewed[source]<10 for source in sources):
        raise ValueError('each changed source needs at least ten reviewed pages')
    if any(report[key]>1 for key in ('character_error_rate','known_error_reduction','geometry_accuracy')):
        raise ValueError('quality rate is outside [0,1]')


def verify_quality_binding(report, root):
    root=Path(root)
    expected={'candidate_id':root.name,'corpus_sha256':file_hash(root/'corpus.sqlite'),
              'geometry_sha256':file_hash(root/'geometry.sqlite'),'layout_sha256':file_hash(root/'layout/manifest.json')}
    if any(report.get(key)!=value for key,value in expected.items()):
        raise ValueError('quality review belongs to another candidate generation')


class Bundle:
    def __init__(self, root, expected_sha, *, quality=True):
        self.root = Path(root).resolve()
        path = self.root/'manifest.json'
        if file_hash(path) != expected_sha:
            raise ValueError('corpus manifest fingerprint mismatch')
        self.manifest = json.loads(path.read_text('utf-8'))
        m = self.manifest
        if m.get('schema_version')!=1 or not ID.fullmatch(str(m.get('id',''))) or m['id']!=self.root.name:
            raise ValueError('invalid corpus version identity')
        required = {'corpus.sqlite','corpus.sqlite.sha256','geometry.sqlite','layout/manifest.json','quality.json'}
        if not required <= m.get('files',{}).keys():
            raise ValueError('corpus bundle is incomplete')
        for name, sha in m['files'].items():
            p = safe_path(self.root,name)
            if p.is_symlink() or not p.is_file() or not SHA.fullmatch(str(sha)) or file_hash(p)!=sha:
                raise ValueError('corpus component fingerprint mismatch: '+name)
        if (self.root/'corpus.sqlite.sha256').read_text('utf-8').strip()!=m['files']['corpus.sqlite']:
            raise ValueError('database checksum sidecar mismatch')
        actual = {p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file() and p!=path}
        if actual != set(m['files']):
            raise ValueError('unregistered corpus files')
        if quality:
            report=json.loads((self.root/'quality.json').read_text('utf-8'))
            verify_quality(report,m.get('changed_sources',[]))
            verify_quality_binding(report,self.root)
        with sqlite3.connect((self.root/'corpus.sqlite').as_uri()+'?mode=ro',uri=True) as conn:
            if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('candidate corpus database is corrupt')
        self.sha256=expected_sha

    def path(self, kind):
        return self.root/{'database':'corpus.sqlite','geometry':'geometry.sqlite','layout':'layout'}[kind]


@lru_cache(maxsize=1)
def current():
    selected = binding()
    if not selected:
        return None
    root = Path(os.environ.get('MARX_RUNTIME_DATA_DIR') or ROOT/'data')/'corpus-releases'
    bundle = Bundle(root/selected['id'],selected['sha256'])
    catalog_file=ROOT/'config/catalog_release.json'
    catalog=json.loads(catalog_file.read_text('utf-8')) if catalog_file.exists() else None
    if bundle.manifest.get('catalog_release')!=catalog:
        raise ValueError('corpus and catalogue are not from the same accepted baseline')
    return bundle


def page_identity(database):
    """Everything apart from recognized text must survive an OCR repair."""
    import hashlib
    h = hashlib.sha256()
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as conn:
        for row in conn.execute('SELECT id,book,volume,source_file,pdf_page,printed_page FROM pages ORDER BY id'):
            h.update(canonical(list(row))+b'\n')
        for row in conn.execute('SELECT * FROM toc_entries ORDER BY rowid'):
            h.update(canonical(list(row))+b'\n')
    return h.hexdigest()


def pinned_path(kind):
    if kind == 'database':
        from book_data_release import current as book_data
        appended = book_data()
        if appended:
            return appended.path('database')
    bundle=current()
    return bundle.path(kind) if bundle else None


def status():
    bundle=current()
    return {'id':bundle.manifest['id'],'sha256':bundle.sha256} if bundle else {'id':'legacy','sha256':None}


def verify_functional_review(evidence, selected):
    if evidence.get('corpus_release')!=selected or evidence.get('result')!='pass':
        raise ValueError('candidate evidence is for another corpus generation')
    checks=evidence.get('checks',{})
    if not isinstance(checks,dict) or any(checks.get(name) is not True for name in FUNCTIONS):
        raise ValueError('full website functional acceptance is incomplete')
    if not {'chrome','edge','mobile'} <= set(evidence.get('browsers',[])) or evidence.get('rollback_rehearsal') is not True:
        raise ValueError('browser acceptance or rollback rehearsal is missing')
