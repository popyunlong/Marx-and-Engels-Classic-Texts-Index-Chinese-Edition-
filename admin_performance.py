"""Request-local counters: never persist SQL, user data, or model prompts."""
from contextvars import ContextVar
from functools import wraps
import logging
import time

_measurement = ContextVar("admin_measurement", default=None)


def observe_connection(conn):
    measurement = _measurement.get()
    if measurement is not None:
        def counted(_statement):
            measurement["queries"] += 1
        conn.set_trace_callback(counted)
    return conn


def timed(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from flask import current_app
        data = {"queries": 0}
        token = _measurement.set(data)
        started = time.monotonic()
        try:
            response = current_app.make_response(fn(*args, **kwargs))
            elapsed = (time.monotonic()-started)*1000
            response.headers["Server-Timing"] = f'admin;dur={elapsed:.1f}, db;desc="{data["queries"]} statements"'
            response.headers["Cache-Control"] = "no-store"
            logging.getLogger(__name__).info("admin loader=%s duration_ms=%.1f db_statements=%d", fn.__name__, elapsed, data["queries"])
            return response
        finally:
            _measurement.reset(token)
    return wrapper
