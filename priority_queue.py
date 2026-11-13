"""Bounded, cancellable priority admission. A running holder is never preempted."""
import contextvars
import itertools
import threading
import time

CURRENT_PRIORITY = contextvars.ContextVar("member_priority", default=0)


class PrioritySemaphore:
    def __init__(self, value, waiting=64):
        self.limit = value
        self._available = value
        self._waiting = waiting
        self._condition = threading.Condition()
        self._tickets = []
        self._sequence = itertools.count()

    def acquire(self, blocking=True, timeout=None, *, priority=None, cancelled=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        ticket = (-(CURRENT_PRIORITY.get() if priority is None else priority), next(self._sequence))
        with self._condition:
            if len(self._tickets) >= self._waiting:
                return False
            self._tickets.append(ticket)
            try:
                while True:
                    if cancelled and cancelled():
                        return False
                    if self._available and ticket == min(self._tickets):
                        self._available -= 1
                        return True
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if not blocking or (remaining is not None and remaining <= 0):
                        return False
                    self._condition.wait(min(0.1, remaining) if remaining is not None else 0.1)
            finally:
                self._tickets.remove(ticket)
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._available >= self.limit:
                raise ValueError("released more slots than acquired")
            self._available += 1
            self._condition.notify_all()
