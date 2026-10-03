"""Correlate explicit direct-loopback release probes without logging user data."""
import json
import re
import time

ROUTES = frozenset(('/api/runtime', '/', '/v2/read'))


class CatalogProbeTiming:
    def __init__(self, app, logger):
        self.app, self.logger = app, logger

    def __call__(self, environ, start_response):
        probe = environ.get('HTTP_X_MARX_CATALOG_PROBE', '')
        route = environ.get('PATH_INFO', '')
        # Run outside ProxyFix: public proxy requests must never enter this
        # diagnostic path, even if a visitor copies the marker header.
        if (environ.get('REMOTE_ADDR') not in ('127.0.0.1', '::1')
                or environ.get('REQUEST_METHOD') != 'GET' or route not in ROUTES
                or any(k.startswith('HTTP_X_FORWARDED_') for k in environ)
                or 'HTTP_FORWARDED' in environ
                or not re.fullmatch(r'[0-9a-f]{32}', probe)):
            return self.app(environ, start_response)
        started = time.monotonic()

        def record(stage, status=None):
            value = dict(at=time.time(), probe=probe, route=route, stage=stage,
                         seconds=time.monotonic() - started, status=status)
            self.logger.info('CATALOG_PROBE_TIMING=%s', json.dumps(value))

        record('wsgi_enter')

        def respond(status, headers, exc_info=None):
            record('response_headers', status.split(' ', 1)[0])
            return start_response(status, headers, exc_info)

        try:
            return self.app(environ, respond)
        except Exception:
            record('application_error')
            raise
