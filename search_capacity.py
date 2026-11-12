"""One shared budget for all public corpus retrieval paths, including nesting."""
import functools
import os
import threading
from contextlib import contextmanager

from werkzeug.exceptions import ServiceUnavailable


class SearchBusy(ServiceUnavailable):
    description = "检索资源正忙，请稍后重试。"


SEARCH_CONCURRENCY = max(1, int(os.environ.get("MARX_SEARCH_CONCURRENCY", "3")))
_slots = threading.BoundedSemaphore(SEARCH_CONCURRENCY)
_local = threading.local()


@contextmanager
def search_slot():
    """Acquire before API charging; nested corpus calls reuse the same slot."""
    if getattr(_local, "active", False):
        yield
        return
    if not _slots.acquire(timeout=0.05):
        raise SearchBusy()
    _local.active = True
    try:
        yield
    finally:
        _local.active = False
        _slots.release()


class ReentrantSemaphore:
    """A thread-owned scan reservation, retained until all nested work exits."""
    def __init__(self, value):
        self.limit = value
        self._semaphore = threading.BoundedSemaphore(value)
        self._local = threading.local()

    def acquire(self, blocking=True, timeout=None):
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            return True
        acquired = self._semaphore.acquire(blocking=blocking, timeout=timeout)
        if acquired:
            self._local.depth = 1
        return acquired

    def release(self):
        depth = getattr(self._local, "depth", 0)
        if not depth:
            raise RuntimeError("Scan slot is not owned by this thread")
        self._local.depth = depth - 1
        if depth == 1:
            self._semaphore.release()


def bounded_search(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with search_slot():
            return function(*args, **kwargs)
    return wrapped
