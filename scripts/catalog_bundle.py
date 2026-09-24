"""Build/verify catalogue artifacts offline; never writes to a production database."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import Catalog, ID_RE, canonical, digest, file_digest, inventory, toc_rows, validate_rows


def groups(rows):
    result = {}
    for row in toc_rows(rows):
        result.setdefault(row['source_file'], []).append(row)
    return {key: digest(value) for key, value in result.items()}


def difference(before, after):
    return {key: {'before': before.get(key), 'after': after.get(key)}
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)}


def validate_changes(before_rows, after_rows, before_files, after_files, approvals):
    changes = {'toc': difference(groups(before_rows), groups(after_rows)),
               'files': difference(before_files, after_files)}
    # Exact allowlist: stale approvals, omitted changes and overly broad approvals fail.
    for kind in changes:
        approved = approvals.get(kind, {})
        if set(approved) != set(changes[kind]):
            raise ValueError('unapproved or stale ' + kind + ' change set')
        for key, values in changes[kind].items():
            record = approved[key]
            if any(record.get(k) != v for k, v in values.items()) or not record.get('evidence'):
                raise ValueError('missing evidence or stale fingerprints: ' + key)
    return changes


def build(snapshot, output, version, parent=None, approvals=None):
    snapshot, output = Path(snapshot), Path(output)
    if not ID_RE.fullmatch(version) or output.name != version or output.exists():
        raise ValueError('output must be a new directory named after the catalogue version')
    rows = toc_rows(json.loads((snapshot / 'toc_entries.json').read_text(encoding='utf-8')))
    sources = json.loads((snapshot / 'sources.json').read_text(encoding='utf-8'))
    validate_rows(rows, sources)
    prior = Catalog(parent) if parent else None
    # Baseline snapshots include EVERY asset, not only HTML. Manifest includes images/fonts.
    for name in ('static_library', 'stream_library'):
        if not (snapshot / name).is_dir():
            raise ValueError('missing snapshot root: ' + name)
        inventory(snapshot / name)  # Reject symlinks before copytree can dereference them.
    output.mkdir(parents=True)
    for name in ('static_library', 'stream_library'):
        shutil.copytree(snapshot / name, output / name)
    (output / 'toc.json').write_bytes(canonical(rows))
    (output / 'sources.json').write_bytes(canonical(sources))
    if (snapshot / 'config').is_dir():
        shutil.copytree(snapshot / 'config', output / 'config')
    files = inventory(output)
    html_files = {k: v for k, v in files.items() if k not in ('toc.json', 'sources.json')}
    changes = {}
    if prior:
        if digest(sources) != digest(prior.sources):
            raise ValueError('source membership changed; refresh baseline through ingestion acceptance first')
        old_files = {k: v for k, v in prior.manifest['files'].items() if k not in ('toc.json', 'sources.json')}
        changes = validate_changes(prior.rows, rows, old_files, html_files, approvals or {})
    manifest = {'schema_version': 1, 'id': version,
                'parent': {'id': prior.version, 'sha256': prior.sha256} if prior else None,
                'input_toc_sha256': prior.manifest['input_toc_sha256'] if prior else digest(rows),
                'files': files, 'changes': changes, 'approvals': approvals or {}}
    (output / 'catalog.json').write_bytes(canonical(manifest))
    Catalog(output)
    return {'id': version, 'sha256': file_digest(output / 'catalog.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    make = sub.add_parser('build')
    make.add_argument('--snapshot', type=Path, required=True)
    make.add_argument('--output', type=Path, required=True)
    make.add_argument('--version', required=True)
    make.add_argument('--parent', type=Path)
    make.add_argument('--approvals', type=Path)
    verify = sub.add_parser('verify')
    verify.add_argument('directory', type=Path)
    verify.add_argument('--sha256')
    args = parser.parse_args()
    if args.command == 'build':
        approved = json.loads(args.approvals.read_text(encoding='utf-8')) if args.approvals else None
        print(json.dumps(build(args.snapshot, args.output, args.version, args.parent, approved)))
    else:
        catalog = Catalog(args.directory, args.sha256)
        print(json.dumps({'id': catalog.version, 'sha256': catalog.sha256, 'entries': len(catalog.rows)}))


if __name__ == '__main__':
    main()
