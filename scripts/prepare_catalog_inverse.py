"""Build a reviewed direct inverse as a NEW child package, without restoring a DB."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import Catalog, ID_RE, canonical, file_digest
from scripts.catalog_bundle import validate_changes


def prepare(current_root, previous_root, version, archive):
    current, previous = Catalog(current_root), Catalog(previous_root)
    archive = Path(archive)
    if not ID_RE.fullmatch(version) or version in (current.version, previous.version) or archive.exists():
        raise ValueError('inverse requires a new version and archive')
    if current.manifest['parent'] != {'id': previous.version, 'sha256': previous.sha256}:
        raise ValueError('inverse target must be the verified direct predecessor')
    if current.sources != previous.sources:
        raise ValueError('inverse cannot change source membership')
    approvals = copy.deepcopy(current.manifest['approvals'])
    for kind in ('toc', 'files'):
        for record in approvals.get(kind, {}).values():
            record['before'], record['after'] = record['after'], record['before']
            if kind == 'toc':
                for entry in record['evidence']:
                    entry['before'], entry['after'] = entry['after'], entry['before']
                    entry['evidence']['inverse_of'] = current.version
            else:
                record['evidence'] = 'Inverse of ' + current.version + ': ' + str(record['evidence'])
    subset = lambda c: {k: v for k, v in c.manifest['files'].items() if k not in ('toc.json', 'sources.json')}
    changes = validate_changes(current.rows, previous.rows, subset(current), subset(previous), approvals)
    manifest = {'schema_version': 1, 'id': version,
                'parent': {'id': current.version, 'sha256': current.sha256},
                'input_toc_sha256': current.manifest['input_toc_sha256'],
                'files': previous.manifest['files'], 'changes': changes, 'approvals': approvals}
    body = canonical(manifest)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open('xb') as output:
        with tarfile.open(fileobj=output, mode='w:gz') as tar:
            info = tarfile.TarInfo('catalog.json'); info.size = len(body); info.mode = 0o644
            tar.addfile(info, io.BytesIO(body))
            for name, expected in sorted(previous.manifest['files'].items()):
                path = previous.root / name
                if file_digest(path) != expected:
                    raise ValueError('inverse input changed: ' + name)
                tar.add(path, arcname=name, recursive=False)
    return {'id': version, 'sha256': hashlib.sha256(body).hexdigest(),
            'archive_sha256': file_digest(archive), 'parent': manifest['parent'],
            'restored_content_from': previous.version, 'changes': changes}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('current', 'previous', 'archive'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.current, args.previous, args.version, args.archive)
    args.report.write_bytes(canonical(result))
    print(json.dumps({k: v for k, v in result.items() if k != 'changes'}))
