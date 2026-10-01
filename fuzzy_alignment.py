"""Run whole-volume fuzzy alignment without holding the web interpreter's GIL.

The subprocess imports only RapidFuzz, receives one string pair, and returns the
original alignment. It never loads the application, corpus, or user databases.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path


class _Worker:
    def __init__(self):
        import rapidfuzz

        dependency_root = str(Path(rapidfuzz.__file__).resolve().parent.parent)
        # Do not pass application credentials to the calculator. -I also keeps
        # user site packages and unrelated PYTHONPATH entries out of the child.
        env = {k: v for k, v in os.environ.items()
               if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-u", str(Path(__file__).resolve()),
             "--worker", dependency_root],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def align(self, query, text, cutoff):
        from rapidfuzz.distance import ScoreAlignment

        if self.process.poll() is not None:
            raise RuntimeError("Fuzzy alignment calculator exited")
        try:
            self.process.stdin.write(json.dumps([query, text, cutoff], ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            response = self.process.stdout.readline()
            if not response:
                raise RuntimeError("Fuzzy alignment calculator exited")
            value = json.loads(response)
            if value == {"error": True}:
                raise RuntimeError("Fuzzy alignment calculator failed")
            return None if value is None else ScoreAlignment(*value)
        except (OSError, ValueError) as exc:
            # Never fall back to the same GIL-blocking scan on the web thread.
            # No query text, child stderr, or application credentials are logged.
            raise RuntimeError("Fuzzy alignment calculator unavailable") from exc

    def close(self):
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        if self.process.stdout:
            self.process.stdout.close()


class _Pool:
    def __init__(self, size=2):
        self.pid = os.getpid()
        self.slots = queue.LifoQueue(size)
        self.workers = []
        self.lock = threading.Lock()
        for _ in range(size):
            self.slots.put(None)

    def align(self, query, text, cutoff):
        worker = self.slots.get()
        try:
            if worker is None:
                worker = _Worker()
                with self.lock:
                    self.workers.append(worker)
            return worker.align(query, text, cutoff)
        except BaseException:
            if worker is not None:
                worker.close()
                with self.lock:
                    self.workers.remove(worker)
                worker = None
            raise
        finally:
            self.slots.put(worker)

    def close(self):
        # Called after request threads finish, at interpreter shutdown.
        if self.pid != os.getpid():
            return
        for worker in self.workers:
            worker.close()
        self.workers.clear()


_pool = None
_pool_pid = None
_pool_lock = threading.Lock()


def partial_ratio_alignment(query: str, text: str, *, score_cutoff: float):
    if getattr(sys, "frozen", False):
        # A packaged desktop executable is not a standalone Python interpreter.
        # Preserve desktop behavior; the production web runtime is not frozen.
        from rapidfuzz import fuzz
        return fuzz.partial_ratio_alignment(query, text, score_cutoff=score_cutoff)
    global _pool, _pool_pid
    with _pool_lock:
        if _pool is None or _pool_pid != os.getpid():
            _pool = _Pool()
            _pool_pid = os.getpid()
            atexit.register(_pool.close)
        pool = _pool
    return pool.align(query, text, score_cutoff)


def _worker_main(dependency_root):
    sys.path.insert(0, dependency_root)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    from rapidfuzz import fuzz

    for line in sys.stdin:
        try:
            query, text, cutoff = json.loads(line)
            result = fuzz.partial_ratio_alignment(query, text, score_cutoff=cutoff)
            value = None if result is None else list(result)
        except Exception:
            value = {"error": True}
        print(json.dumps(value), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("This module is an internal fuzzy alignment calculator")
    _worker_main(sys.argv[2])
