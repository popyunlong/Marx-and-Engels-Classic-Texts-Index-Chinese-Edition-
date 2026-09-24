"""Catalogue portion of the release transaction. Caller must hold the global release lock."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import Catalog, ID_RE, file_digest, inventory, safe_file
from scripts.catalog_bundle import validate_changes


def read_binding(app):
    path = Path(app) / 'config/catalog_release.json'
    if not path.exists():
        return None
    result = json.loads(path.read_text(encoding='utf-8'))
    if not ID_RE.fullmatch(result['id']):
        raise ValueError('invalid catalogue binding')
    return result


def install_archive(archive, destination, binding):
    if destination.exists():
        return Catalog(destination, binding['sha256'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.chmod(0o755)
    staging = Path(tempfile.mkdtemp(prefix='.incoming-', dir=destination.parent))
    try:
        root = staging / destination.name
        root.mkdir()
        with tarfile.open(archive, 'r:gz') as tar:
            seen = set()
            total = 0
            for member in tar.getmembers():
                if not member.isfile() or member.name in seen:
                    raise ValueError('catalogue archive must contain unique regular files only')
                seen.add(member.name)
                target = safe_file(root, member.name)
                total += member.size
                if total > 4 * 1024**3:
                    raise ValueError('catalogue archive exceeds 4 GiB limit')
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open('xb') as out:
                    shutil.copyfileobj(source, out)
                target.chmod(0o644)
        Catalog(root, binding['sha256'])
        for directory in root.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o755)
        root.rename(destination)  # Single-filesystem atomic installation; never replace a version.
        destination.chmod(0o755)
    finally:
        shutil.rmtree(staging)
    return Catalog(destination, binding['sha256'])


def preflight(app_root, candidate_app, archive=None):
    app_root, candidate_app = Path(app_root), Path(candidate_app)
    selected = read_binding(candidate_app)
    previous = read_binding(app_root / 'current/app')
    if selected is None:
        if previous is not None:
            raise ValueError('bound catalogue cannot be downgraded to legacy mode')
        return {'id': 'legacy', 'sha256': None}
    if previous is None:
        current_metadata = json.loads((app_root / 'current/release.json').read_text(encoding='utf-8'))
        if current_metadata.get('catalog_protocol') != 1:
            raise ValueError('deploy compatibility foundation before binding the first catalogue')
    destination = app_root / 'data/catalog-releases' / selected['id']
    if archive:
        candidate = install_archive(archive, destination, selected)
    else:
        candidate = Catalog(destination, selected['sha256'])
    # Verify source membership, original TOC input and reviewed configuration against this release.
    with sqlite3.connect((app_root / 'data/corpus.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as conn:
        candidate.verify_corpus(conn)
    checked_pdfs = set()
    for source, change in candidate.manifest.get('approvals', {}).get('toc', {}).items():
        evidence = change.get('evidence')
        if not isinstance(evidence, list):
            raise ValueError('TOC changes require structured original-PDF evidence')
        for record in evidence:
            expected = record.get('evidence', {}).get('pdf_sha256')
            if not expected or not source.startswith('pdfs/'):
                raise ValueError('missing original PDF fingerprint')
            key = (source, expected)
            if key not in checked_pdfs:
                pdf = safe_file((app_root / 'pdfs').resolve(), source[5:])
                if file_digest(pdf) != expected:
                    raise ValueError('original PDF changed since review: ' + source)
                checked_pdfs.add(key)
    for name, expected in candidate.manifest['files'].items():
        if name.startswith('config/') and file_digest(safe_file(candidate_app, name)) != expected:
            raise ValueError('book configuration changed since catalogue snapshot: ' + name)
    if previous:
        if selected == previous:
            return selected
        if candidate.manifest['parent'] != previous:
            raise ValueError('stale catalogue parent: rebase approved changes on the current version')
        parent = Catalog(app_root / 'data/catalog-releases' / previous['id'], previous['sha256'])
        validate_changes(parent.rows, candidate.rows,
                         {k: v for k, v in parent.manifest['files'].items() if k not in ('toc.json', 'sources.json')},
                         {k: v for k, v in candidate.manifest['files'].items() if k not in ('toc.json', 'sources.json')},
                         candidate.manifest['approvals'])
    else:
        if candidate.manifest['parent'] is not None or candidate.manifest['changes']:
            raise ValueError('first bound release must be an unchanged baseline')
        from catalog_release import digest
        if digest(candidate.rows) != candidate.manifest['input_toc_sha256']:
            raise ValueError('first bound release changes TOC data')
        for folder in ('static_library', 'stream_library'):
            expected = {k[len(folder)+1:]: v for k, v in candidate.manifest['files'].items()
                        if k.startswith(folder + '/')}
            if inventory(app_root / folder) != expected:
                raise ValueError('first bound release changes live HTML/assets')
    return selected


def rollback_guard(app_root, target_app):
    current = read_binding(Path(app_root) / 'current/app')
    target = read_binding(target_app)
    if current != target:
        if current and not target:
            metadata = json.loads((Path(target_app).parent / 'release.json').read_text(encoding='utf-8'))
            if metadata.get('catalog_protocol') != 1:
                raise ValueError('legacy target cannot preserve versioned reader links; deploy compatibility foundation first')
            # One-time migration rollback is safe only while the bound version
            # remains byte-for-byte equivalent to the untouched legacy roots.
            baseline = Catalog(Path(app_root) / 'data/catalog-releases' / current['id'], current['sha256'])
            if baseline.manifest['parent'] is None and not baseline.manifest['changes']:
                # Reuse first-install equivalence checks using an isolated view
                # of the current binding, without changing either live directory.
                for folder in ('static_library', 'stream_library'):
                    expected = {k[len(folder)+1:]: v for k, v in baseline.manifest['files'].items()
                                if k.startswith(folder + '/')}
                    if inventory(Path(app_root) / folder) != expected:
                        raise ValueError('legacy roots changed; baseline rollback is unsafe')
                for name, expected in baseline.manifest['files'].items():
                    if name.startswith('config/') and file_digest(safe_file(target_app, name)) != expected:
                        raise ValueError('rollback target book configuration differs from baseline')
                with sqlite3.connect((Path(app_root) / 'data/corpus.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as conn:
                    baseline.verify_corpus(conn)
                return
        raise ValueError('rollback changes catalogue version; release a reviewed inverse patch on the latest baseline')
    if target:
        preflight(app_root, target_app)


def check_health(payload, metadata):
    expected = metadata.get('catalog_release') or {'id': 'legacy', 'sha256': None}
    actual = payload.get('catalog_release') or {'id': 'legacy', 'sha256': None}
    if (payload.get('ok') is not True or payload.get('app_release', {}).get('id') != metadata['release_id']
            or actual != expected):
        raise ValueError('runtime code/catalogue identity mismatch')


def accept_catalog(root, app):
    selected = read_binding(app)
    if selected:
        Catalog(Path(root) / 'data/catalog-releases' / selected['id'], selected['sha256'])
        receipts = Path(root) / 'data/catalog-accepted'
        receipts.mkdir(exist_ok=True)
        receipts.chmod(0o755)
        target = receipts / (selected['id'] + '.json')
        if target.exists():
            if json.loads(target.read_text(encoding='utf-8')) != selected:
                raise ValueError('accepted catalogue identity cannot change')
            return
        temporary = target.with_suffix('.incoming')
        temporary.write_text(json.dumps(selected), encoding='utf-8')
        temporary.chmod(0o644)
        os.replace(temporary, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'rollback', 'health', 'pack', 'accept'])
    parser.add_argument('--root', type=Path)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--metadata', type=Path)
    args = parser.parse_args()
    if args.action == 'preflight':
        print(json.dumps(preflight(args.root, args.app, args.archive)))
    elif args.action == 'rollback':
        rollback_guard(args.root, args.app)
    elif args.action == 'health':
        check_health(json.load(sys.stdin), json.loads(args.metadata.read_text(encoding='utf-8')))
    elif args.action == 'accept':
        accept_catalog(args.root, args.app)
    elif args.action == 'pack':
        catalog = Catalog(args.root)
        with tarfile.open(args.archive, 'w:gz') as tar:
            for name in ['catalog.json', *sorted(catalog.manifest['files'])]:
                tar.add(catalog.root / name, arcname=name, recursive=False)


if __name__ == '__main__':
    main()
