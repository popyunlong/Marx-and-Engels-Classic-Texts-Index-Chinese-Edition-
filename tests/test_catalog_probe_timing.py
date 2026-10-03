import io
import json

import pytest

from catalog_probe_timing import CatalogProbeTiming


class Log:
    def __init__(self): self.rows = []
    def info(self, pattern, value): self.rows.append(json.loads(value))


def environment(**changes):
    return dict(REMOTE_ADDR='127.0.0.1', REQUEST_METHOD='GET', PATH_INFO='/api/runtime',
                HTTP_X_MARX_CATALOG_PROBE='a' * 32, HTTP_COOKIE='private-cookie',
                QUERY_STRING='private-query', **changes)


def test_direct_probe_preserves_wsgi_response_and_excludes_private_data():
    log, responses = Log(), []
    body = [b'private-body']
    def app(env, respond):
        respond('200 OK', [('Content-Type', 'application/json')])
        return body
    wrapped = CatalogProbeTiming(app, log)
    result = wrapped(environment(), lambda *args: responses.append(args))
    assert result is body and responses == [('200 OK', [('Content-Type', 'application/json')], None)]
    assert [r['stage'] for r in log.rows] == ['wsgi_enter', 'response_headers']
    assert all(r['probe'] == 'a' * 32 and r['route'] == '/api/runtime' for r in log.rows)
    assert log.rows[-1]['status'] == '200'
    assert 'private-' not in json.dumps(log.rows)


@pytest.mark.parametrize('change', [
    {'REMOTE_ADDR': '198.51.100.1'}, {'HTTP_X_FORWARDED_FOR': ''},
    {'HTTP_X_FORWARDED_PROTO': 'https'}, {'HTTP_FORWARDED': 'for=127.0.0.1'},
    {'HTTP_X_MARX_CATALOG_PROBE': 'bad\nvalue'}, {'HTTP_X_MARX_CATALOG_PROBE': ''},
    {'PATH_INFO': '/api/ai/stream'}, {'REQUEST_METHOD': 'POST'},
])
def test_public_unmarked_and_stream_requests_are_untouched(change):
    log, env = Log(), environment()
    env.update(change)
    marker = object()
    wrapped = CatalogProbeTiming(lambda e, s: marker, log)
    assert wrapped(env, None) is marker and log.rows == []


def test_application_exception_is_preserved_and_correlated():
    log = Log()
    def broken(e, s): raise ValueError('private-error')
    with pytest.raises(ValueError, match='private-error'):
        CatalogProbeTiming(broken, log)(environment(), None)
    assert log.rows[-1]['stage'] == 'application_error'
    assert 'private-error' not in json.dumps(log.rows)


@pytest.mark.parametrize('stage', ['response_headers', 'response_body'])
def test_observer_timeout_records_request_and_stage_without_response_data(monkeypatch, stage):
    from scripts.catalog_observe import ProbeFailure, probe
    log = io.StringIO()
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): raise TimeoutError('timed out')
    def request(req, **kwargs):
        seen.append(req)
        if stage == 'response_headers': raise TimeoutError('timed out')
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', request)
    with pytest.raises(ProbeFailure) as caught:
        probe('http://127.0.0.1:8001', 'candidate', {}, 'candidate', log)
    row = json.loads(log.getvalue())
    assert row['probe'] == seen[0].get_header('X-marx-catalog-probe')
    assert row['stage'] == caught.value.failure['stage'] == stage
    assert row['result'] == 'fail' and row['category'] == 'timeout'
    assert caught.value.failure['probe'] == row['probe']
    assert ('headers_seconds' in row) == (stage == 'response_body')
