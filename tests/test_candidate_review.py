"""Exercise the release's one-use, identity-bound pause before changing traffic."""
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('receipt,accepted', [
    ('release-a:abcdef:PASS', True),
    ('release-b:abcdef:PASS', False),
    ('release-a:wrong:PASS', False),
    ('release-a:abcdef:FAIL', False),
])
def test_candidate_review_requires_matching_receipt(tmp_path, receipt, accepted):
    bash = os.environ.get('MARX_TEST_BASH') or shutil.which('bash')
    if not bash:
        pytest.skip('bash unavailable')
    bash_dir = str(tmp_path)
    if os.name == 'nt':
        bash_dir = subprocess.check_output([bash, '-c', 'cygpath -u "$1"', '--', str(tmp_path)], text=True).strip()
    env = dict(os.environ, MARX_REVIEW_FIFO_DIR=bash_dir, MARX_REVIEW_TIMEOUT_SECONDS='3')
    script = f'source "{(ROOT / "deploy/review_gate.sh").as_posix()}"; RELEASE_ID=release-a; REVIEW_NONCE=abcdef; review_candidate'
    process = subprocess.Popen([bash, '--noprofile', '--norc', '-c', script],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env)
    pipe = tmp_path / 'marx-search-candidate-release-a.fifo'
    bash_pipe = bash_dir + '/marx-search-candidate-release-a.fifo'
    def pipe_exists():
        if os.name == 'nt':
            return subprocess.run([bash, '-c', 'test -p "$1"', '--', bash_pipe]).returncode == 0
        return pipe.exists()
    try:
        for _ in range(100):
            if pipe_exists():
                break
            time.sleep(.02)
        assert pipe_exists(), process.poll()
        subprocess.run([bash, '-c', 'printf "%s\\n" "$2" > "$1"', '--', bash_pipe, receipt], check=True, timeout=5)
        _, stderr = process.communicate(timeout=5)
        assert (process.returncode == 0) is accepted, stderr
        assert not pipe_exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
