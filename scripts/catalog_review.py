"""Validate release-bound, comparable observation evidence before TOC cutover."""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_health import slow_routes, resource_pressure


def validate_observation(report, metadata, parent, sample_source='same_client_ssh_forward'):
    expected = {'result': 'pass', 'sample_source': sample_source,
                'live_release': parent['release_id'], 'candidate_release': metadata['release_id'],
                'live_catalog': parent.get('book_data_catalog') or parent['catalog_release'],
                'candidate_catalog': metadata.get('book_data_catalog') or metadata['catalog_release']}
    if any(report.get(k) != v for k, v in expected.items()):
        raise ValueError('catalogue observation does not match this release and its live parent')
    elapsed = report.get('elapsed_seconds', 0)
    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 1800:
        raise ValueError('catalogue candidate requires at least 30 minutes of observation')
    if report.get('pairs', 0) < 60:
        raise ValueError('catalogue paired observations are incomplete')
    routes = report.get('routes', {})
    for side in ('live', 'candidate'):
        for route in ('/api/runtime', '/', '/v2/read'):
            stats = routes.get(side, {}).get(route, {})
            if stats.get('count') != report['pairs'] or stats.get('p95') is None:
                raise ValueError('missing comparable route observations')
            if any(not isinstance(stats.get(k), (int, float)) or not math.isfinite(stats[k]) or stats[k] < 0
                   for k in ('median', 'p95', 'max')):
                raise ValueError('invalid catalogue observation latency')
    if slow_routes(routes['live'], routes['candidate']):
        raise ValueError('catalogue candidate latency regressed')


def validate_server_samples(report, rows, metadata, parent):
    from scripts.catalog_observe import compare, ROUTES
    if report.get('schema_version') != 2 or report.get('interval_seconds') != 30:
        raise ValueError('server observation schema or interval mismatch')
    if len(rows) < 60 or len(rows) != report.get('pairs'):
        raise ValueError('server raw observations missing')
    previous = -1
    for row in rows:
        elapsed = row.get('elapsed')
        if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= previous:
            raise ValueError('invalid server observation timeline')
        if previous >= 0 and not 29 <= elapsed - previous <= 90:
            raise ValueError('server observation gap or oversampling')
        previous = elapsed
        for side in ('live', 'candidate'):
            if set(row.get(side, {})) != set(ROUTES):
                raise ValueError('server route samples incomplete')
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v < 6
                   for v in row[side].values()):
                raise ValueError('server core route stalled')
    if rows[0]['elapsed'] > 1 or rows[-1]['elapsed'] - rows[0]['elapsed'] < 1800:
        raise ValueError('server observation window incomplete')
    actual = compare(rows)
    if any(report.get(k) != v for k, v in actual.items()):
        raise ValueError('server summary does not match raw observations')
    if report.get('elapsed_seconds') != rows[-1]['elapsed']:
        raise ValueError('server elapsed time does not match raw observations')
    validate_observation(report, metadata, parent, sample_source='server_loopback')


def validate_review(app, evidence):
    app = Path(app)
    metadata = json.loads((app.parent / 'release.json').read_text('utf-8'))
    parent = json.loads((app.parent.parent / metadata['parent_release_id'] / 'release.json').read_text('utf-8'))
    if (metadata.get('catalog_release') == parent.get('catalog_release')
            and metadata.get('book_data_release') == parent.get('book_data_release')
            and metadata.get('dictionary_graph_release') == parent.get('dictionary_graph_release')):
        return
    if evidence.get('release_id') != metadata['release_id'] or evidence.get('result') != 'pass':
        raise ValueError('catalogue review identity or decision mismatch')
    # Never trust client-supplied timing summaries or an arbitrary evidence path.
    from scripts.catalog_observe import server_context, server_status
    report = server_status(app)
    _, _, directory = server_context(app)
    rows = [json.loads(line) for line in (directory / 'samples.jsonl').read_text('utf-8').splitlines()]
    validate_server_samples(report, rows, metadata, parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--resources', action='store_true')
    args = parser.parse_args()
    if args.resources:
        result = resource_pressure()
        print(json.dumps(result))
        return
    if args.app is None:
        parser.error('--app is required for evidence validation')
    validate_review(args.app, json.load(sys.stdin))
    print('Catalogue candidate evidence accepted')


if __name__ == '__main__':
    main()
