"""Observe paired release endpoints through SSH forwards; never alter production.

Run during the locked release transaction, after candidate warm-up. Each pair
uses the same routes, request source, timeout and alternating request order.
The report is evidence, not a candidate approval receipt.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_health import distribution, slow_routes

ROUTES = ('/api/runtime', '/', '/v2/read')


def probe(base, expected, catalog):
    result = {}
    for route in ROUTES:
        started = time.monotonic()
        with urllib.request.urlopen(base.rstrip('/') + route, timeout=6) as response:
            body = response.read() if route == '/api/runtime' else response.read(1)
            if response.status != 200:
                raise RuntimeError('core endpoint failed: ' + route)
            result[route] = time.monotonic() - started
        if route == '/api/runtime':
            value = json.loads(body)
            if (value.get('ok') is not True or value['app_release']['id'] != expected
                    or value['catalog_release'] != catalog):
                raise RuntimeError('release or catalogue identity changed')
    return result


def compare(rows):
    summaries = {}
    for side in ('live', 'candidate'):
        summaries[side] = {route: distribution([r[side][route] for r in rows]) for route in ROUTES}
    if any(v['p95'] is None for side in summaries.values() for v in side.values()):
        return {'result': 'insufficient_samples', 'routes': summaries}
    slow = slow_routes(summaries['live'], summaries['candidate'])
    return {'result': 'fail' if slow else 'pass', 'slow_routes': slow, 'routes': summaries}


def observe(args):
    args.output.mkdir(parents=True, exist_ok=False)
    report_path = args.output / 'report.json'
    report = {'result': 'running', 'started_at': datetime.now(timezone.utc).isoformat(),
              'live_release': args.live_release, 'candidate_release': args.candidate_release,
              'live_catalog': args.live_catalog, 'candidate_catalog': args.candidate_catalog,
              'sample_source': 'same_client_ssh_forward', 'duration_required': args.seconds}
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    rows = []
    try:
        # Warm both versions before starting the measured interval.
        for _ in range(2):
            probe(args.live, args.live_release, args.live_catalog)
            probe(args.candidate, args.candidate_release, args.candidate_catalog)
        started = time.monotonic()
        with (args.output / 'samples.jsonl').open('x', encoding='utf-8') as out:
            while True:
                cycle = time.monotonic()
                row = {'at': datetime.now(timezone.utc).isoformat(), 'elapsed': cycle - started}
                order = ('live', 'candidate') if len(rows) % 2 == 0 else ('candidate', 'live')
                for side in order:
                    row[side] = probe(getattr(args, side), getattr(args, side + '_release'),
                                      getattr(args, side + '_catalog'))
                    if sum(v >= 6 for v in row[side].values()) >= 2:
                        raise RuntimeError('multiple core routes stalled')
                rows.append(row)
                out.write(json.dumps(row) + '\n'); out.flush()
                if time.monotonic() - started >= args.seconds:
                    break
                time.sleep(max(0, args.interval - (time.monotonic() - cycle)))
        report.update(compare(rows))
        report['elapsed_seconds'] = time.monotonic() - started
        report['pairs'] = len(rows)
    except Exception as exc:
        report.update(result='fail', reason=str(exc), pairs=len(rows))
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for side in ('live', 'candidate'):
        parser.add_argument('--' + side, required=True)
        parser.add_argument('--' + side + '-release', required=True)
        parser.add_argument('--' + side + '-catalog', type=json.loads, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--interval', type=int, default=15)
    args = parser.parse_args()
    if args.seconds < 1800 or not 10 <= args.interval <= 30:
        parser.error('candidate observation requires >=1800s and a 10-30s interval')
    result = observe(args)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['result'] == 'pass' else 1)


if __name__ == '__main__':
    main()
