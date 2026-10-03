import copy
import json
from types import SimpleNamespace

import pytest

from scripts.catalog_observe import compare, observe, server_status
from scripts.catalog_review import validate_server_samples


def evidence():
    rows = [{'elapsed': n * 30., 'live': {r: .1 for r in ('/', '/api/runtime', '/v2/read')},
             'candidate': {r: .11 for r in ('/', '/api/runtime', '/v2/read')}} for n in range(61)]
    parent = {'release_id': 'old', 'catalog_release': {'id': 'first', 'sha256': 'a' * 64}}
    metadata = {'release_id': 'new', 'catalog_release': {'id': 'second', 'sha256': 'b' * 64}}
    report = dict(compare(rows), schema_version=2, sample_source='server_loopback',
                  interval_seconds=30, elapsed_seconds=1800., pairs=61,
                  live_release='old', candidate_release='new',
                  live_catalog=parent['catalog_release'], candidate_catalog=metadata['catalog_release'])
    return report, rows, metadata, parent


def test_server_samples_pass_without_client_supplied_observation():
    validate_server_samples(*evidence())


@pytest.mark.parametrize('change', ['summary', 'missing', 'short', 'gap', 'negative', 'stalled', 'source', 'identity'])
def test_server_samples_reject_forged_stale_or_incomplete_evidence(change):
    report, rows, metadata, parent = evidence()
    if change == 'summary': report['routes']['candidate']['/']['p95'] = .01
    if change == 'missing': rows.pop()
    if change == 'short': rows[-1]['elapsed'] = 1799
    if change == 'gap': rows[1]['elapsed'] = 150
    if change == 'negative': rows[1]['live']['/'] = -1
    if change == 'stalled': rows[1]['candidate']['/'] = 6
    if change == 'source': report['sample_source'] = 'same_client_ssh_forward'
    if change == 'identity': report['candidate_release'] = 'stale'
    with pytest.raises(ValueError): validate_server_samples(report, rows, metadata, parent)


def test_server_owned_progress_survives_unavailable_client_and_updates_each_pair(tmp_path, monkeypatch):
    import scripts.catalog_observe as module
    clock = [0.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    saved = []
    def sleep(seconds):
        saved.append(json.loads((tmp_path / 'new/report.json').read_text()))
        clock[0] += seconds
    monkeypatch.setattr(module.time, 'sleep', sleep)
    monkeypatch.setattr(module, 'server_processes', lambda _: {'candidate': 'invocation1', 'live': 'invocation0'})
    # No client, SSH transport or desktop process participates in this loop.
    monkeypatch.setattr(module, 'probe', lambda *a: {r: .1 for r in module.ROUTES})
    args = SimpleNamespace(output=tmp_path / 'new', live='loopback-live', candidate='loopback-candidate',
        live_release='old', candidate_release='new', live_catalog={}, candidate_catalog={}, seconds=1800,
        interval=30, server_app=tmp_path, processes=module.server_processes('new'))
    result = observe(args)
    assert result['result'] == 'pass' and result['pairs'] == 61
    assert [r['pairs'] for r in saved] == list(range(1, 61))
    assert all(r['result'] == 'running' for r in saved)


def test_process_restart_invalidates_running_observation(tmp_path, monkeypatch):
    import scripts.catalog_observe as module
    monkeypatch.setattr(module, 'server_context', lambda _: ({'release_id': 'new'}, {'release_id': 'old'}, tmp_path))
    (tmp_path / 'report.json').write_text(json.dumps(dict(result='pass', processes={'candidate': 'previous'})))
    monkeypatch.setattr(module, 'server_processes', lambda _: {'candidate': 'restarted'})
    with pytest.raises(ValueError, match='process identity'): server_status(tmp_path)


def test_core_failure_is_persisted_as_failure(tmp_path, monkeypatch):
    import scripts.catalog_observe as module
    def failed(*args): raise module.ProbeFailure('candidate', '/', 'timeout', 'timed out', 6)
    monkeypatch.setattr(module, 'probe', failed)
    args = SimpleNamespace(output=tmp_path / 'failed', live='', candidate='', live_release='old',
        candidate_release='new', live_catalog={}, candidate_catalog={}, seconds=1800, interval=30)
    result = observe(args)
    assert result['result'] == 'fail' and result['failure']['category'] == 'timeout'
    assert json.loads((args.output / 'report.json').read_text())['result'] == 'fail'
