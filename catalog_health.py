"""Read-only catalogue health decisions; offline work never uses these gates.

Compare like routes and observation sources. Retain all errors, including
correlated business conflicts. A sparse sample never masquerades as a p95.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from pathlib import Path

MIN_P95_SAMPLES = 20
MIN_WINDOW_SAMPLES = 5
WINDOW_SECONDS = 300
CORE_ROUTES = frozenset(('/', '/api/runtime', '/login', '/v2/read', '/reader', '/viewer'))


def resource_pressure(proc=Path('/proc')):
    """Linux PSI provides contention evidence while the release owns its lock."""
    values = {}
    for name, kind in (('cpu', 'some'), ('io', 'full'), ('memory', 'full')):
        rows = (proc / 'pressure' / name).read_text().splitlines()
        row = next(line for line in rows if line.startswith(kind + ' '))
        fields = dict(item.split('=', 1) for item in row.split()[1:])
        values[name + '_avg10'] = float(fields['avg10'])
    memory = dict(line.split(':', 1) for line in (proc / 'meminfo').read_text().splitlines())
    values['available_mib'] = int(memory['MemAvailable'].strip().split()[0]) / 1024
    if any(not math.isfinite(v) or v < 0 for v in values.values()):
        raise ValueError('invalid resource pressure metrics')
    values['pressured'] = (values['cpu_avg10'] > 90 or values['io_avg10'] > 5
                          or values['memory_avg10'] > 1 or values['available_mib'] < 512)
    return values


def distribution(values):
    values = [float(v) for v in values]
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError('invalid latency observation')
    values.sort()
    return {'count': len(values), 'median': statistics.median(values) if values else None,
            'p95': values[math.ceil(.95 * len(values)) - 1]
            if len(values) >= MIN_P95_SAMPLES else None,
            'max': max(values) if values else None}


def classify_errors(access, conflicts):
    """Correlate only exact PUT paths with proven recovery-bin 409 events.

    Caddy access timestamps mark request completion. Require a matching warning
    within that request's interval; a bare 409, another route or another method
    is never exempt. This classification does not erase the HTTP 502 count.
    """
    remaining = list(conflicts)
    result = {'five_xx': 0, 'business_conflict_5xx': 0, 'unclassified_5xx': 0,
              'core_5xx': 0}
    for row in access:
        if int(row.get('status') or 0) < 500:
            continue
        result['five_xx'] += 1
        req = row.get('request', {})
        path = req.get('uri', '').split('?', 1)[0]
        if path in CORE_ROUTES or path.startswith('/api/library/'):
            result['core_5xx'] += 1
        end = float(row.get('ts') or 0)
        start = end - float(row.get('duration') or 0)
        match = None
        if row.get('status') == 502 and req.get('method') == 'PUT' and path.startswith('/api/ai/conversations/'):
            for index, event in enumerate(remaining):
                if (event.get('path') == path and event.get('status') == 409
                        and event.get('deleted') is True
                        and event.get('error') == 'conversation is in recovery bin'
                        and start - 1 <= float(event['at']) <= end + 1):
                    match = index
                    break
        if match is None:
            result['unclassified_5xx'] += 1
        else:
            remaining.pop(match)
            result['business_conflict_5xx'] += 1
    return result


def identity(sample):
    return (sample['app_release'], sample['catalog_release'])


def validate_sample(sample, expected=None):
    if sample.get('schema_version') != 2 or not sample.get('ok'):
        raise RuntimeError('production health metrics invalid or unavailable')
    if expected is not None and identity(sample) != expected:
        raise RuntimeError('production release identity changed')
    if sample['errors']['unclassified_5xx'] or sample['errors']['core_5xx']:
        raise RuntimeError('unclassified or core production 5xx')
    required = {'/', '/api/runtime', '/v2/read'}
    if not required <= sample['probes'].keys():
        raise RuntimeError('missing core health probe')
    for probe in sample['probes'].values():
        if (probe.get('status') != 200 or not math.isfinite(probe.get('seconds', math.nan))
                or probe['seconds'] < 0):
            raise RuntimeError('core health probe failed')
    # Multiple endpoints delayed together are a real availability risk, even
    # when a sparse window cannot support a percentile.
    if sum(p['seconds'] >= 6 for p in sample['probes'].values()) >= 2:
        raise RuntimeError('multiple core endpoints stalled')


def route_distributions(samples):
    routes = defaultdict(list)
    for sample in samples:
        for route, probe in sample['probes'].items():
            routes[route].append(probe['seconds'])
    return {route: distribution(values) for route, values in routes.items()}


def slow_routes(baseline, current):
    slow = []
    for route, stats in current.items():
        before = baseline.get(route)
        if not before or min(stats['count'], before['count']) < MIN_WINDOW_SAMPLES:
            continue
        # A p95 comparison is valid only when BOTH samples support it.
        field = 'p95' if stats['p95'] is not None and before['p95'] is not None else 'median'
        threshold = max(before[field] * 1.2, before[field] + .1)
        if stats[field] > threshold:
            slow.append(route)
    return slow


class HealthWindow:
    def __init__(self, baseline_samples):
        if not baseline_samples:
            raise ValueError('health baseline is empty')
        self.expected = identity(baseline_samples[0])
        for sample in baseline_samples:
            validate_sample(sample, self.expected)
        self.baseline = route_distributions(baseline_samples)
        waits = []
        for before, after in zip(baseline_samples, baseline_samples[1:]):
            total = after['cpu_total'] - before['cpu_total']
            if total > 0:
                waits.append(max(0, after['cpu_iowait'] - before['cpu_iowait']) / total)
        self.baseline_iowait = statistics.median(waits) if waits else 0
        self.samples = []
        self.previous = baseline_samples[-1]
        self.started = self.previous['at']
        self.slow_windows = self.busy_samples = 0

    def observe(self, sample):
        validate_sample(sample, self.expected)
        if sample['at'] <= self.previous['at']:
            raise RuntimeError('health observation did not advance')
        total = sample['cpu_total'] - self.previous['cpu_total']
        if total <= 0:
            raise RuntimeError('CPU observation counter did not advance')
        wait = max(0, sample['cpu_iowait'] - self.previous['cpu_iowait']) / total
        self.busy_samples = self.busy_samples + 1 if wait > self.baseline_iowait + .05 else 0
        self.previous = sample
        self.samples.append(sample)
        if self.busy_samples >= 2:
            raise RuntimeError('production disk wait rose during transfer')
        if sample['at'] - self.started >= WINDOW_SECONDS:
            slow = slow_routes(self.baseline, route_distributions(self.samples))
            self.slow_windows = self.slow_windows + 1 if slow else 0
            self.started = sample['at']
            self.samples = []
            if self.slow_windows >= 2:
                raise RuntimeError('production route latency rose for two complete windows')
