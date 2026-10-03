import copy

import pytest

from catalog_health import HealthWindow, classify_errors, distribution, route_distributions, slow_routes, validate_sample


def sample(at, latency=.05):
    return {'schema_version': 2, 'at': at, 'ok': True,
            'app_release': 'app1', 'catalog_release': {'id': 'catalog1', 'sha256': 'a' * 64},
            'errors': {'five_xx': 0, 'business_conflict_5xx': 0, 'unclassified_5xx': 0, 'core_5xx': 0},
            'probes': {r: {'status': 200, 'seconds': latency} for r in ('/', '/api/runtime', '/v2/read')},
            'cpu_total': at * 100, 'cpu_iowait': at}


def test_sparse_percentiles_and_different_route_baselines():
    assert distribution([.01] * 19)['p95'] is None
    assert distribution([.01] * 20)['p95'] == .01
    base = {'/': distribution([.4] * 20), '/api/runtime': distribution([.01] * 20)}
    assert slow_routes(base, base) == []
    changed = copy.deepcopy(base)
    changed['/api/runtime'] = distribution([.3] * 20)
    assert slow_routes(base, changed) == ['/api/runtime']


def test_only_correlated_recovery_bin_conflicts_are_classified():
    path = '/api/ai/conversations/example'
    conflict = {'path': path, 'at': 99.5, 'status': 409,
                'deleted': True, 'error': 'conversation is in recovery bin'}
    req = {'ts': 100, 'duration': 1, 'status': 502, 'request': {'method': 'PUT', 'uri': path}}
    assert classify_errors([req], [conflict]) == {
        'five_xx': 1, 'business_conflict_5xx': 1, 'unclassified_5xx': 0, 'core_5xx': 0}
    for field, value in [('path', path + 'different'), ('at', 90), ('status', 403), ('deleted', False), ('error', 'timeout')]:
        wrong = dict(conflict, **{field: value})
        assert classify_errors([req], [wrong])['unclassified_5xx'] == 1
    assert classify_errors([req, req], [conflict])['unclassified_5xx'] == 1
    req['request']['method'] = 'GET'
    assert classify_errors([req], [conflict])['unclassified_5xx'] == 1


@pytest.mark.parametrize('failure', ['identity', 'core', 'unclassified', 'missing', 'stall', 'unhealthy'])
def test_real_failures_always_stop_production_step(failure):
    first = sample(100)
    policy = HealthWindow([first])
    bad = sample(130)
    if failure == 'identity': bad['app_release'] = 'app2'
    if failure == 'core': bad['probes']['/']['status'] = 503
    if failure == 'unclassified': bad['errors']['unclassified_5xx'] = 1
    if failure == 'missing': del bad['probes']['/api/runtime']
    if failure == 'stall':
        bad['probes']['/']['seconds'] = 6
        bad['probes']['/v2/read']['seconds'] = 6
    if failure == 'unhealthy': bad['ok'] = False
    with pytest.raises(RuntimeError): policy.observe(bad)


def test_single_slow_observation_does_not_reset_work():
    policy = HealthWindow([sample(i) for i in range(30, 331, 30)])
    one = sample(360)
    one['probes']['/']['seconds'] = 2
    policy.observe(one)
    for at in range(390, 991, 30): policy.observe(sample(at))
    assert policy.slow_windows == 0


def test_two_complete_slow_windows_stop_transfer():
    policy = HealthWindow([sample(i) for i in range(30, 331, 30)])
    for at in range(360, 930, 30): policy.observe(sample(at, .8))
    with pytest.raises(RuntimeError, match='two complete windows'):
        policy.observe(sample(930, .8))


def test_disk_pressure_and_stale_metrics_fail_closed():
    policy = HealthWindow([sample(30), sample(60)])
    one = sample(90); one['cpu_iowait'] = 1000
    policy.observe(one)
    two = sample(120); two['cpu_iowait'] = 2000
    with pytest.raises(RuntimeError, match='disk wait'): policy.observe(two)
    with pytest.raises(RuntimeError, match='did not advance'):
        HealthWindow([sample(30)]).observe(sample(30))


def test_business_conflict_remains_visible_without_failing_core_health():
    s = sample(30)
    s['errors'].update(five_xx=1, business_conflict_5xx=1)
    validate_sample(s)
    assert s['errors']['five_xx'] == 1


def test_streamed_helper_is_self_contained():
    from scripts.snapshot_catalog import REMOTE_SOURCE
    # It must compile and import without any production filesystem install.
    namespace = {'__name__': 'catalog_helper_test'}
    exec(compile(REMOTE_SOURCE, '<streamed catalogue helper>', 'exec'), namespace)
    assert callable(namespace['metrics'])


def test_candidate_observation_rejects_short_stale_and_regressed_evidence():
    from scripts.catalog_observe import compare
    from scripts.catalog_review import validate_observation
    routes = ('/', '/api/runtime', '/v2/read')
    rows = [{'live': {r: .1 for r in routes}, 'candidate': {r: .11 for r in routes}} for _ in range(121)]
    before = {'release_id': 'live', 'catalog_release': {'id': 'c1', 'sha256': 'a' * 64}}
    after = {'release_id': 'candidate', 'catalog_release': {'id': 'c2', 'sha256': 'b' * 64}}
    report = dict(compare(rows), elapsed_seconds=1801, pairs=121,
                  sample_source='same_client_ssh_forward', live_release='live', candidate_release='candidate',
                  live_catalog=before['catalog_release'], candidate_catalog=after['catalog_release'])
    validate_observation(report, after, before)
    for key, value in [('elapsed_seconds', 1799), ('pairs', 5), ('live_release', 'stale'), ('result', 'fail')]:
        bad = dict(report, **{key: value})
        with pytest.raises(ValueError): validate_observation(bad, after, before)
    bad = copy.deepcopy(report)
    bad['routes']['candidate']['/']['p95'] = 2
    with pytest.raises(ValueError, match='regressed'): validate_observation(bad, after, before)


def test_candidate_compare_does_not_pass_sparse_observation():
    from scripts.catalog_observe import compare
    assert compare([{'live': {r: .1 for r in ('/', '/api/runtime', '/v2/read')},
                     'candidate': {r: .1 for r in ('/', '/api/runtime', '/v2/read')}}])['result'] == 'insufficient_samples'


@pytest.mark.parametrize('kind', ['timeout', 'http', 'identity'])
def test_observation_retains_failed_side_route_and_category(monkeypatch, kind):
    import urllib.error
    from scripts.catalog_observe import probe, ProbeFailure

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args):
            return b'{"ok":true,"app_release":{"id":"other"},"catalog_release":{}}'

    def request(url, **kwargs):
        if kind == 'timeout': raise urllib.error.URLError(TimeoutError('timed out'))
        if kind == 'http': raise urllib.error.HTTPError(url, 502, 'gateway', {}, None)
        return Response()

    monkeypatch.setattr('urllib.request.urlopen', request)
    with pytest.raises(ProbeFailure) as caught:
        probe('http://candidate', 'expected', {}, 'candidate')
    failure = caught.value.failure
    assert failure['side'] == 'candidate' and failure['route'] == '/api/runtime'
    assert failure['category'] == {'timeout': 'timeout', 'http': 'http_error', 'identity': 'identity_drift'}[kind]
    assert failure['status'] == (502 if kind == 'http' else 200 if kind == 'identity' else None)


def test_candidate_resource_pressure_requires_real_counters(tmp_path):
    from catalog_health import resource_pressure
    (tmp_path / 'pressure').mkdir()
    for name in ('cpu', 'io', 'memory'):
        (tmp_path / 'pressure' / name).write_text('some avg10=0.00\nfull avg10=0.00\n')
    (tmp_path / 'meminfo').write_text('MemAvailable: 1048576 kB\n')
    assert not resource_pressure(tmp_path)['pressured']
    (tmp_path / 'pressure' / 'io').write_text('full avg10=6.00\n')
    assert resource_pressure(tmp_path)['pressured']
    (tmp_path / 'pressure' / 'io').write_text('full avg10=nan\n')
    with pytest.raises(ValueError): resource_pressure(tmp_path)
