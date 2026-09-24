"""Read-only helper streamed over SSH by snapshot_catalog.py; never installed remotely.

The caller holds a nonblocking shared release lock and gives this process idle I/O
and low CPU priority. Nothing here writes to the production filesystem.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

BASE = Path('/opt/marx-search')
ROOTS = ('static_library', 'stream_library')
CONFIGS = ('books.yaml', 'volumes.yaml', 'static_books.yaml', 'stream_books.yaml')
MAX_FILES = 100
MAX_BYTES = 32 * 1024 * 1024
RATE = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def current_app():
    return (BASE / 'current/app').resolve(strict=True)


def runtime():
    with urllib.request.urlopen('http://127.0.0.1:8000/api/runtime', timeout=6) as response:
        return json.load(response)


def source_path(name, app):
    part = PurePosixPath(name)
    if not name or part.is_absolute() or '..' in part.parts or '\\' in name or ':' in name:
        raise ValueError('unsafe snapshot path')
    if part.parts[0] in ROOTS:
        root = BASE / part.parts[0]
        path = root.joinpath(*part.parts[1:])
    elif len(part.parts) == 2 and part.parts[0] == 'config' and part.parts[1] in CONFIGS:
        root = app / 'config'
        path = root / part.parts[1]
    else:
        raise ValueError('path outside snapshot roots')
    if root.is_symlink() or path.is_symlink():
        raise ValueError('snapshot refuses symlink')
    for parent in path.parents:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError('snapshot refuses symlink parent')
    return path


def file_record(name, app):
    value = source_path(name, app).lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('snapshot expects a regular file: ' + name)
    return {'size': value.st_size, 'mtime_ns': value.st_mtime_ns}


def inventory(app):
    result = {}
    for folder in ROOTS:
        root = BASE / folder
        if root.is_symlink() or not root.is_dir():
            raise ValueError('invalid snapshot root: ' + folder)
        for directory, dirs, files in os.walk(root, followlinks=False):
            for dirname in dirs:
                if (Path(directory) / dirname).is_symlink():
                    raise ValueError('snapshot refuses symlink directory')
            for filename in files:
                path = Path(directory) / filename
                name = path.relative_to(BASE).as_posix()
                result[name] = file_record(name, app)
    for name in CONFIGS:
        key = 'config/' + name
        if (app / key).is_file():
            result[key] = file_record(key, app)
    return dict(sorted(result.items()))


def database(include_rows):
    path = (BASE / 'data/corpus.sqlite').resolve(strict=True)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        try:
            rows = [dict(row) for row in conn.execute(
                'SELECT * FROM toc_entries ORDER BY source_file,sort_order,rowid')]
            sources = [dict(row) for row in conn.execute(
                'SELECT book,volume,source_file,count(*) page_count,'
                'min(pdf_page) first_page,max(pdf_page) last_page '
                'FROM pages GROUP BY source_file ORDER BY book,volume,source_file')]
        finally:
            conn.rollback()  # Never hold the DB read transaction during file transfer.
    result = {'sha256': hashlib.sha256(canonical([rows, sources])).hexdigest(),
              'toc_count': len(rows), 'source_count': len(sources)}
    if include_rows:
        result.update(toc_entries=rows, sources=sources)
    return result


def identity(include_rows):
    before = runtime()
    app = current_app()
    db = database(include_rows)
    files = inventory(app)
    after = runtime()
    if before['app_release'] != after['app_release'] or before['catalog_release'] != after['catalog_release']:
        raise ValueError('live release changed during snapshot inventory')
    ledger = BASE / 'release-ledger.jsonl'
    return {'runtime': after, 'app': str(app), 'database': db, 'files': files,
            'ledger_bytes': ledger.stat().st_size if ledger.exists() else 0}


class ThrottledOutput:
    def __init__(self):
        self.out = sys.stdout.buffer
        self.started = time.monotonic()
        self.sent = 0

    def write(self, value):
        self.sent += len(value)
        pause = self.sent / RATE - (time.monotonic() - self.started)
        if pause > 0:
            time.sleep(pause)
        self.out.write(value)
        return len(value)

    def flush(self):
        self.out.flush()


def transfer(request):
    expected = request['files']
    if not isinstance(expected, dict) or not 1 <= len(expected) <= MAX_FILES:
        raise ValueError('invalid snapshot batch size')
    if sum(item['size'] for item in expected.values()) > MAX_BYTES:
        raise ValueError('snapshot batch exceeds byte budget')
    app = current_app()
    out = ThrottledOutput()
    with tarfile.open(fileobj=out, mode='w|') as tar:
        for name, record in expected.items():
            path = source_path(name, app)
            if file_record(name, app) != record:
                raise ValueError('snapshot source changed before read: ' + name)
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as handle:
                info = tarfile.TarInfo(name)
                info.size = record['size']
                info.mtime = record['mtime_ns'] // 1_000_000_000
                info.mode = 0o644
                tar.addfile(info, handle)
                after = os.fstat(handle.fileno())
                if (after.st_size, after.st_mtime_ns) != (record['size'], record['mtime_ns']):
                    raise ValueError('snapshot source changed during read: ' + name)
    out.flush()


def emit_json(value):
    out = ThrottledOutput()
    out.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    out.flush()


def metrics():
    now = time.time()
    command = ['journalctl', '-u', 'caddy', '--since', '@' + str(int(now - 30)),
               '--until', '@' + str(int(now)), '-o', 'cat', '--no-pager']
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, errors='replace')
    durations, errors = [], 0
    for line in proc.stdout:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('logger') != 'http.log.access':
            continue
        status = int(row.get('status') or 0)
        errors += status >= 500
        uri = str(row.get('request', {}).get('uri') or '').split('?', 1)[0]
        if uri in ('/', '/v2/read', '/reader', '/library', '/viewer') or uri.startswith('/api/library/'):
            durations.append(float(row.get('duration') or 0))
    if proc.wait(timeout=10) != 0:
        raise RuntimeError('Caddy access metrics unavailable')
    durations.sort()
    p95 = durations[max(0, (95 * len(durations) + 99) // 100 - 1)] if durations else None
    probes = {}
    for route in ('/api/runtime', '/', '/v2/read'):
        start = time.monotonic()
        with urllib.request.urlopen('http://127.0.0.1:8000' + route, timeout=6) as response:
            response.read(1)
            probes[route] = {'status': response.status, 'seconds': time.monotonic() - start}
    if not runtime().get('ok'):
        raise RuntimeError('runtime health is not ok')
    cpu = (Path('/proc/stat').read_text().splitlines()[0]).split()[1:]
    counters = [int(value) for value in cpu]
    return {'at': now, 'p95': p95, 'samples': len(durations), 'five_xx': errors,
            'probes': probes, 'cpu_total': sum(counters), 'cpu_iowait': counters[4]}


def main():
    request = json.loads(base64.b64decode(sys.argv[1], validate=True))
    action = request['action']
    if action == 'metadata':
        emit_json(identity(True))
    elif action == 'verify':
        emit_json(identity(False))
    elif action == 'files':
        transfer(request)
    elif action == 'metrics':
        emit_json(metrics())
    else:
        raise ValueError('unknown snapshot action')


if __name__ == '__main__':
    main()
