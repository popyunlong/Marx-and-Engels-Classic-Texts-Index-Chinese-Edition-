import hashlib
import io
import subprocess
import sys

import pytest

from scripts.upload_release import CHUNK, RATE, RECEIVER, receive_command, stream


def receive(path, payload, *, size=None, digest=None):
    return subprocess.run([sys.executable, '-c', RECEIVER, str(path),
                           str(len(payload) if size is None else size),
                           digest or hashlib.sha256(payload).hexdigest()],
                          input=payload, capture_output=True)


def test_upload_never_replaces_an_existing_archive(tmp_path):
    target = tmp_path / 'archive'
    target.write_bytes(b'previous')
    assert receive(target, b'new').returncode != 0
    assert target.read_bytes() == b'previous'


@pytest.mark.parametrize('size,digest', [(100, None), (1, None), (None, '0' * 64)])
def test_incomplete_or_corrupt_upload_removes_only_its_file(tmp_path, size, digest):
    target = tmp_path / 'archive'
    assert receive(target, b'payload', size=size, digest=digest).returncode != 0
    assert not target.exists()


def test_verified_upload_and_safe_locked_destination(tmp_path):
    target = tmp_path / 'archive'
    assert receive(target, b'payload').returncode == 0
    assert target.read_bytes() == b'payload'
    command = receive_command('/var/tmp/marx-catalog-review.tar.gz', 7, 'a' * 64)
    assert 'flock -s -n /run/lock/marx-search-release.lock ionice -c3 nice -n19' in command
    with pytest.raises(ValueError):
        receive_command('/opt/marx-search/current/app.py', 7, 'a' * 64)


def test_transfer_paces_every_chunk_and_stops_on_health_alarm():
    clock = [0.0]
    class Monitor:
        checks = 0
        def check(self):
            self.checks += 1
            if self.checks == 5:
                raise RuntimeError('health alarm')
    def sleep(seconds):
        clock[0] += seconds
    class Sink(io.BytesIO):
        def write(self, data):
            assert (self.tell() + len(data)) / RATE <= clock[0]
            return super().write(data)
    sink = Sink()
    with pytest.raises(RuntimeError, match='health alarm'):
        stream(io.BytesIO(b'x' * CHUNK * 4), sink, Monitor(), clock=lambda: clock[0], sleep=sleep)
    assert len(sink.getvalue()) == CHUNK * 2
