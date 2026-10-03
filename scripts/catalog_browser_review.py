"""Persist one completed browser check at a time for one immutable candidate."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def record(path, release_id, catalog, case_id, result):
    path = Path(path)
    if not case_id or not isinstance(result, dict) or result.get('result') not in ('pass', 'fail'):
        raise ValueError('a named completed browser result is required')
    report = json.loads(path.read_text('utf-8')) if path.exists() else {
        'schema_version': 1, 'release_id': release_id, 'catalog_release': catalog, 'checks': {}}
    if report['release_id'] != release_id or report['catalog_release'] != catalog:
        raise ValueError('browser checkpoint belongs to another candidate')
    report['checks'][case_id] = dict(result, checked_at=datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--catalog', type=json.loads, required=True)
    parser.add_argument('--case', required=True)
    parser.add_argument('--result', type=json.loads, required=True)
    args = parser.parse_args()
    print(json.dumps({'saved_checks': len(record(args.file, args.release_id, args.catalog, args.case, args.result)['checks'])}))
