"""Observe paired release endpoints through SSH forwards; never alter production.

Run during the locked release transaction, after candidate warm-up. Each pair
uses the same routes, request source, timeout and alternating request order.
The report is evidence, not a candidate approval receipt.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_health import distribution, slow_routes, resource_pressure
from release_review_policy import append_fast

ROUTES = ('/api/runtime', '/', '/v2/read')


class ProbeFailure(RuntimeError):
    def __init__(self, side, route, category, detail, seconds, status=None):
        super().__init__(f'{side} {route}: {category}: {detail}')
        self.failure = {'side': side, 'route': route, 'category': category,
                        'detail': detail, 'seconds': seconds, 'status': status}


def probe(base, expected, catalog, side='unspecified', evidence=None):
    result = {}
    for route in ROUTES:
        started = time.monotonic()
        category, status = 'invalid_response', None
        record = dict(at=datetime.now(timezone.utc).isoformat(), side=side,
                      route=route, probe=uuid.uuid4().hex, stage='response_headers')
        try:
            request = urllib.request.Request(base.rstrip('/') + route,
                                            headers={'X-Marx-Catalog-Probe': record['probe']})
            with urllib.request.urlopen(request, timeout=6) as response:
                status = response.status
                record['headers_seconds'] = time.monotonic() - started
                record['stage'] = 'response_body'
                # Compare complete responses on both sides, including HTML bodies.
                body = response.read()
                if status != 200:
                    category = 'http_error'
                    raise RuntimeError('core endpoint failed')
                result[route] = time.monotonic() - started
                record['stage'] = 'validate_response'
            if route == '/api/runtime':
                value = json.loads(body)
                if value.get('ok') is not True:
                    category = 'core_not_ok'
                    raise RuntimeError('runtime reports unhealthy')
                if (value['app_release']['id'] != expected or value['catalog_release'] != catalog):
                    category = 'identity_drift'
                    raise RuntimeError('release or catalogue identity changed')
        except Exception as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(exc, urllib.error.HTTPError):
                category, status = 'http_error', exc.code
            elif isinstance(reason, (TimeoutError, socket.timeout)):
                category = 'timeout'
            elif isinstance(exc, (urllib.error.URLError, OSError)):
                category = 'transport_error'
            failure = ProbeFailure(side, route, category, str(exc), time.monotonic() - started, status)
            failure.failure.update(probe=record['probe'], stage=record['stage'], at=record['at'])
            record.update(result='fail', category=category)
            raise failure from exc
        finally:
            record.update(seconds=time.monotonic() - started, status=status)
            record.setdefault('result', 'pass')
            if evidence is not None:
                evidence.write(json.dumps(record) + '\n'); evidence.flush()
    return result


def compare(rows):
    summaries = {}
    for side in ('live', 'candidate'):
        summaries[side] = {route: distribution([r[side][route] for r in rows]) for route in ROUTES}
    if any(v['p95'] is None for side in summaries.values() for v in side.values()):
        return {'result': 'insufficient_samples', 'routes': summaries}
    slow = slow_routes(summaries['live'], summaries['candidate'])
    return {'result': 'fail' if slow else 'pass', 'slow_routes': slow, 'routes': summaries}


def compare_fast(rows):
    """Three actual rounds; do not claim a p95 for this small sample."""
    summaries={side:{route:distribution([r[side][route] for r in rows])
        for route in ROUTES} for side in ('live','candidate')}
    slow=[r for r in ROUTES if
        summaries['candidate'][r]['median']>max(1.,3*summaries['live'][r]['median'])
        or summaries['candidate'][r]['max']>max(2.,3*summaries['live'][r]['max'])]
    failed=len(rows)<3 or slow or any(r.get('resources',{}).get('pressured') is not False for r in rows)
    return dict(result='fail' if failed else 'pass',slow_routes=slow,routes=summaries)


def observe(args):
    args.output.mkdir(parents=True, exist_ok=False)
    report_path = args.output / 'report.json'
    report = {'result': 'running', 'started_at': datetime.now(timezone.utc).isoformat(),
              'live_release': args.live_release, 'candidate_release': args.candidate_release,
              'live_catalog': args.live_catalog, 'candidate_catalog': args.candidate_catalog,
              'sample_source': 'same_client_ssh_forward', 'duration_required': args.seconds}
    if getattr(args, 'server_app', None):
        report.update(schema_version=2, sample_source='server_loopback',
                      processes=args.processes, interval_seconds=args.interval)
    if getattr(args,'append_fast',False):
        report.update(schema_version=3,review_mode='append-fast')
    save_report(report_path, report)
    rows = []
    with (args.output / 'probe-evidence.jsonl').open('x', encoding='utf-8') as evidence:
        return _observe_measured(args, report, report_path, rows, evidence)


def _observe_measured(args, report, report_path, rows, evidence):
    try:
        # Warm both versions before starting the measured interval.
        for _ in range(2):
            probe(args.live, args.live_release, args.live_catalog, 'live', evidence)
            probe(args.candidate, args.candidate_release, args.candidate_catalog, 'candidate', evidence)
        started = time.monotonic()
        with (args.output / 'samples.jsonl').open('x', encoding='utf-8') as out:
            while True:
                if getattr(args, 'server_app', None) and server_processes(args.candidate_release) != args.processes:
                    raise RuntimeError('primary or candidate process changed during observation')
                cycle = time.monotonic()
                row = {'at': datetime.now(timezone.utc).isoformat(), 'elapsed': cycle - started}
                if getattr(args,'append_fast',False):
                    row['resources']=resource_pressure()
                order = ('live', 'candidate') if len(rows) % 2 == 0 else ('candidate', 'live')
                for side in order:
                    row[side] = probe(getattr(args, side), getattr(args, side + '_release'),
                                      getattr(args, side + '_catalog'), side, evidence)
                    if sum(v >= 6 for v in row[side].values()) >= 2:
                        raise RuntimeError('multiple core routes stalled')
                rows.append(row)
                out.write(json.dumps(row) + '\n'); out.flush()
                report.update(pairs=len(rows), elapsed_seconds=row['elapsed'])
                save_report(report_path, report)
                if (len(rows)>=3 if getattr(args,'append_fast',False)
                        else row['elapsed'] - rows[0]['elapsed'] >= args.seconds):
                    break
                time.sleep(max(0, args.interval - (time.monotonic() - cycle)))
        report.update(compare_fast(rows) if getattr(args,'append_fast',False) else compare(rows))
        report['elapsed_seconds'] = rows[-1]['elapsed']
        report['pairs'] = len(rows)
    except Exception as exc:
        report.update(result='fail', reason=str(exc), pairs=len(rows))
        if isinstance(exc, ProbeFailure):
            report['failure'] = exc.failure
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save_report(report_path, report)
    return report


def save_report(path, report):
    report['updated_at'] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
    temporary.replace(path)


def server_processes(release):
    from catalog_release import ID_RE
    if not ID_RE.fullmatch(release):
        raise ValueError('invalid candidate identity')
    result = {}
    for side, unit in [('live', 'marx-search.service'),
                       ('candidate', 'marx-search-candidate-' + release + '.service')]:
        output = subprocess.check_output(['systemctl', 'show', unit,
                    '--property=InvocationID,MainPID,ActiveState'], text=True, timeout=5)
        values = dict(line.split('=', 1) for line in output.splitlines())
        if values.get('ActiveState') != 'active' or int(values.get('MainPID', '0')) <= 0 or not values.get('InvocationID'):
            raise ValueError(side + ' process is not active')
        result[side] = values
    return result


def server_context(app):
    app = Path(app).resolve()
    metadata = json.loads((app.parent / 'release.json').read_text('utf-8'))
    from catalog_release import ID_RE
    if not all(ID_RE.fullmatch(metadata.get(k, '')) for k in ('release_id', 'parent_release_id')):
        raise ValueError('invalid release metadata')
    parent = json.loads((app.parent.parent / metadata['parent_release_id'] / 'release.json').read_text('utf-8'))
    output = app.parent.parent.parent / 'data/release-observations' / metadata['release_id']
    return metadata, parent, output


def server_status(app):
    metadata, parent, output = server_context(app)
    report = json.loads((output / 'report.json').read_text('utf-8'))
    if report.get('processes') != server_processes(metadata['release_id']):
        raise ValueError('observed process identity changed')
    if report.get('candidate_release') != metadata['release_id'] or report.get('live_release') != parent['release_id']:
        raise ValueError('observation release identity changed')
    if report.get('result') not in ('running', 'pass'):
        raise ValueError('server observation failed: ' + report.get('reason', 'unknown'))
    if report['result'] == 'running':
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(report['updated_at'])).total_seconds()
        if not 0 <= age <= 90:
            raise ValueError('server observation stopped advancing')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-app', type=Path)
    parser.add_argument('--server-status', action='store_true')
    for side in ('live', 'candidate'):
        parser.add_argument('--' + side)
        parser.add_argument('--' + side + '-release')
        parser.add_argument('--' + side + '-catalog', type=json.loads)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--interval', type=int, default=15)
    args = parser.parse_args()
    if args.server_app:
        if args.server_status:
            print(json.dumps({'result': server_status(args.server_app)['result']}))
            return
        os.umask(0o077)
        metadata, parent, args.output = server_context(args.server_app)
        args.append_fast=append_fast(metadata,parent)
        args.live, args.candidate = 'http://127.0.0.1:8000', 'http://127.0.0.1:8001'
        args.live_release, args.candidate_release = parent['release_id'], metadata['release_id']
        args.live_catalog = parent.get('book_data_catalog') or parent['catalog_release']
        args.candidate_catalog = metadata.get('book_data_catalog') or metadata['catalog_release']
        args.processes = server_processes(args.candidate_release)
        args.interval = 30
        if args.append_fast:args.seconds,args.interval=0,1
    elif not all(getattr(args, k) is not None for k in ('live', 'candidate', 'live_release', 'candidate_release', 'live_catalog', 'candidate_catalog', 'output')):
        parser.error('supply --server-app or all paired endpoint arguments')
    if not getattr(args,'append_fast',False) and (args.seconds < 1800 or not 10 <= args.interval <= 30):
        parser.error('candidate observation requires >=1800s and a 10-30s interval')
    result = observe(args)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['result'] == 'pass' else 1)


if __name__ == '__main__':
    main()
