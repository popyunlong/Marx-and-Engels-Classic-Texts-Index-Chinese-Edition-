from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class YieldRequired(RuntimeError):
    pass


class Scheduler:
    """Admission only: never stops or imports the production citation worker.

    A short existing queue DB write transaction orders admission against job
    creation/claims. No production schema/data is changed. Once released, new
    user jobs run immediately, alongside only the already admitted bounded units.
    """

    def __init__(self, store, citation_db: Path, *, quiet_seconds=30, health_url="",
                 min_available_mib=1400, clock=time.time):
        self.store, self.citation_db = store, Path(citation_db)
        self.quiet_seconds, self.health_url = quiet_seconds, health_url
        self.min_available_mib, self.clock = min_available_mib, clock
        self.last_busy = clock()
        self.heartbeat = 0.0
        self.reason = "等待校注队列空闲"

    @contextlib.contextmanager
    def queue_guard(self, *, read_only=False):
        if not self.citation_db.is_file():
            raise YieldRequired("无法读取校注队列，扩库等待")
        # Idle workers also briefly write claim/cleanup state. Allow that tiny
        # transaction to finish before checking real demand, bounded to 1 s.
        # Waiting here consumes no unit and never holds the queue lock.
        c = sqlite3.connect(self.citation_db.as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True, timeout=1)
        try:
            if not read_only:
                c.execute("BEGIN IMMEDIATE")
            now = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
            # Include active leases, not just unclaimed jobs. Human review is excluded.
            busy = c.execute("SELECT COUNT(*) FROM citation_assistant_jobs WHERE "
                             "status IN ('extracting','queued','matching','exporting') AND expires_at>?",
                             (now,)).fetchone()[0]
            if busy:
                self.last_busy = self.clock()
                self.store.last_demand(self.last_busy)
                raise YieldRequired("正在为用户校注让出资源")
            yield
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                raise
            self.last_busy = self.clock()
            self.store.last_demand(self.last_busy)
            raise YieldRequired('校注队列正在写入，扩库稍后重试') from exc
        finally:
            c.rollback()
            c.close()

    def tick(self):
        now = self.clock()
        try:
            self.last_busy = max(self.last_busy, self.store.last_demand())
            with self.queue_guard(read_only=True):
                pass
            if self.health_url:
                with urllib.request.urlopen(self.health_url, timeout=2) as response:
                    health = json.load(response)
                if not health.get("ok") or health.get("db_status") != "ok":
                    raise YieldRequired("网站健康检查未通过")
            mem = Path("/proc/meminfo")
            if mem.exists():
                available = next(int(line.split()[1]) // 1024 for line in mem.read_text().splitlines()
                                 if line.startswith("MemAvailable:"))
                if available < self.min_available_mib:
                    raise YieldRequired("网站可用内存不足，扩库等待")
            self.reason = "可处理扩库单元" if now - self.last_busy >= self.quiet_seconds else "等待校注队列连续空闲 30 秒"
            self.heartbeat = now
        except Exception as exc:
            self.last_busy = now
            self.store.last_demand(now)
            self.reason = str(exc) if isinstance(exc, YieldRequired) else "队列或健康检查暂不可用"
            self.heartbeat = 0
            self.store.set_state('admission_last_failure', {'at': now, 'reason': self.reason})
        self.store.set_state("admission", {"heartbeat": self.heartbeat, "reason": self.reason,
                                           "last_busy": self.last_busy})

    @contextlib.contextmanager
    def admit(self):
        now = self.clock()
        if not self.heartbeat or now - self.heartbeat > 6:
            raise YieldRequired("调度心跳未就绪，扩库等待")
        with self.queue_guard():
            self.last_busy = max(self.last_busy, self.store.last_demand())
            if now - self.last_busy < self.quiet_seconds:
                raise YieldRequired("等待校注队列连续空闲 30 秒")
            yield  # Caller performs only a short atomic claim, never network/CPU work.

    def check_quiet(self):
        """Interrupt admitted long work without competing for queue writes."""
        now=self.clock()
        if not self.heartbeat or now-self.heartbeat>6:
            raise YieldRequired('调度心跳未就绪，扩库等待')
        self.last_busy=max(self.last_busy,self.store.last_demand())
        if now-self.last_busy<self.quiet_seconds:
            raise YieldRequired('等待校注队列连续空闲 30 秒')
