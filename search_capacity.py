"""One shared budget for all public corpus retrieval paths, including nesting."""
import functools
import os
import threading

from werkzeug.exceptions import ServiceUnavailable


class SearchBusy(ServiceUnavailable):
    description = "检索资源正忙，请稍后重试。"


_slots = threading.BoundedSemaphore(max(1, int(os.environ.get("MARX_SEARCH_CONCURRENCY", "2"))))
_local = threading.local()


def bounded_search(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if getattr(_local, "active", False):
            return function(*args, **kwargs)
        if not _slots.acquire(timeout=0.05):
            raise SearchBusy()
        _local.active = True
        try:
            return function(*args, **kwargs)
        finally:
            _local.active = False
            _slots.release()
    return wrapped
