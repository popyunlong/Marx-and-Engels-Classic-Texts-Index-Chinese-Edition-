"""Stop a redundant executor only while both atomic claim queues are locked.

No jobs are changed. Existing stages, including expired but still owned stages,
prevent retirement. Keep the stop callback short; release locks before startup.
"""
import contextlib
import sqlite3
from pathlib import Path

from .scheduler import YieldRequired


def stop_idle_executor(data_dir, owner, stop):
    if not owner:
        raise ValueError('A concrete executor identity is required')
    lanes = [
        ('citation_assistant.sqlite3', 'citation_assistant_jobs',
         ('extracting', 'queued', 'matching', 'exporting')),
        ('search_exports.sqlite3', 'search_export_jobs',
         ('queued', 'counting', 'collecting', 'rendering', 'packaging')),
    ]
    with contextlib.ExitStack() as stack:
        connections = []
        try:
            for filename, table, statuses in lanes:
                path = Path(data_dir) / filename
                c = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=1)
                stack.callback(c.close)
                stack.callback(c.rollback)
                c.execute('BEGIN IMMEDIATE')
                connections.append((c, table, statuses))
            for c, table, statuses in connections:
                marks = ','.join('?' for _ in statuses)
                count = c.execute(f'SELECT COUNT(*) FROM {table} WHERE lease_owner=? '
                                  f'AND status IN ({marks})', (owner, *statuses)).fetchone()[0]
                if count:
                    raise YieldRequired('执行器仍在处理用户任务，保持运行')
            stop()
        except sqlite3.OperationalError as exc:
            raise YieldRequired('无法锁定并核实两条任务队列，保持执行器运行') from exc
