"""Bounded, health-guarded upload of unique immutable release artifacts."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.snapshot_catalog import Remote, SnapshotMonitor

RATE = 2 * 1024 * 1024
CHUNK = 64 * 1024
RECEIVER = '''import hashlib, json, pathlib, sys
target = pathlib.Path(sys.argv[1])
size, expected = int(sys.argv[2]), sys.argv[3]
digest, count = hashlib.sha256(), 0
owned = False
try:
    with target.open('xb') as out:
        owned = True
        while True:
            data = sys.stdin.buffer.read(65536)
            if not data: break
            count += len(data)
            if count > size: raise ValueError('upload exceeds declared size')
            out.write(data); digest.update(data)
    if count != size or digest.hexdigest() != expected:
        raise ValueError('incomplete or corrupt release upload')
    print(json.dumps({'bytes': count, 'sha256': digest.hexdigest()}))
except BaseException:
    if owned: target.unlink(missing_ok=True)
    raise
'''


def receive_command(destination, size, digest):
    if not re.fullmatch(r'/var/tmp/marx-(?:search|catalog|corpus)-[A-Za-z0-9._-]+\.tar\.gz', destination):
        raise ValueError('upload destination must be a unique release archive')
    source = base64.b64encode(RECEIVER.encode()).decode()
    code = "import base64; exec(compile(base64.b64decode('" + source + "'), '<receiver>', 'exec'))"
    return ('flock -s -n /run/lock/marx-search-release.lock ionice -c3 nice -n19 python3 -B -c '
            + shlex.quote(code) + ' ' + shlex.quote(destination) + ' ' + str(size) + ' ' + shlex.quote(digest))


def stream(source, sink, monitor, *, clock=time.monotonic, sleep=time.sleep):
    started, count = clock(), 0
    while data := source.read(CHUNK):
        monitor.check()
        # Pace before writing, so even the initial chunk respects the cap.
        count += len(data)
        delay = count / RATE - (clock() - started)
        if delay > 0:
            sleep(delay)
        monitor.check()
        sink.write(data)
        sink.flush()
    return count


class UploadRemote(Remote):
    def __init__(self, host, key, port):
        super().__init__(host, key)
        self.port = port

    def command(self, request):
        command = super().command(request)
        return command[:1] + ['-p', str(self.port)] + command[1:]

    def upload(self, path, destination, monitor):
        path = Path(path)
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        size, expected = path.stat().st_size, digest.hexdigest()
        command = self.command({})
        command[-1] = receive_command(destination, size, expected)
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as child:
            with self.active_lock:
                self.active = child
            try:
                with path.open('rb') as source:
                    stream(source, child.stdin, monitor)
                child.stdin.close()
                child.stdin = None
                stdout, stderr = child.communicate(timeout=60)
                monitor.check()
                if child.returncode:
                    raise RuntimeError('release upload failed: ' + stderr.decode(errors='replace')[-1000:])
                if json.loads(stdout) != {'bytes': size, 'sha256': expected}:
                    raise RuntimeError('remote upload verification mismatch')
                return {'path': destination, 'bytes': size, 'sha256': expected}
            finally:
                if child.poll() is None:
                    child.terminate()
                with self.active_lock:
                    self.active = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--expected-live', required=True)
    parser.add_argument('--file', nargs=2, action='append', required=True, metavar=('LOCAL', 'REMOTE'))
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    remote = UploadRemote(args.host, args.key, args.port)
    monitor = SnapshotMonitor(remote)
    report = {'result': 'running', 'expected_live': args.expected_live, 'files': []}
    try:
        report['baseline'] = monitor.measure_baseline()
        if monitor.policy.expected[0] != args.expected_live:
            raise RuntimeError('live release changed before upload')
        monitor.start()
        for path, destination in args.file:
            report['files'].append(remote.upload(path, destination, monitor))
        monitor.finish()
        report['result'] = 'pass'
    except BaseException as exc:
        report.update(result='fail', reason=str(exc))
        raise
    finally:
        monitor.stopped.set()
        remote.terminate_active()
        if monitor.thread:
            monitor.thread.join(timeout=60)
        report['observations'] = monitor.observations
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
