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


@pytest.mark.parametrize('timer_active,fail', [(True,False),(True,True),(False,False)])
def test_release_restores_watchdog_on_success_and_failure(tmp_path, timer_active, fail):
    bash = os.environ.get('MARX_TEST_BASH') or shutil.which('bash')
    if not bash: pytest.skip('bash unavailable')
    script = """
set -e
systemctl() {
  echo "$*"
  if [ "$1" = is-active ]; then return STATUS; fi
  return 0
}
source "HELPER"
trap resume_watchdog_after_release EXIT
pause_watchdog_for_release
echo PREFLIGHT
END
""".replace('STATUS', '0' if timer_active else '1').replace('HELPER', (ROOT/'deploy/watchdog_release.sh').as_posix()).replace('END', 'false' if fail else 'true')
    result = subprocess.run([bash,'--noprofile','--norc','-c',script],capture_output=True,text=True)
    assert result.returncode == (1 if fail else 0)
    lines=result.stdout.splitlines()
    assert lines.index('stop marx-search-watchdog.service') < lines.index('PREFLIGHT')
    assert ('start marx-search-watchdog.timer' in lines) == timer_active
    if timer_active: assert lines[-1]=='start marx-search-watchdog.timer'


def test_watchdog_does_not_probe_or_record_restart_while_release_owns_lock(tmp_path):
    bash = os.environ.get('MARX_TEST_BASH') or shutil.which('bash')
    if not bash: pytest.skip('bash unavailable')
    # Use an injectable shell lock outcome; the real flock is exercised by release integration.
    script=(ROOT/'deploy/health_watchdog.sh').read_text(encoding='utf-8')
    script=script.replace('/run/lock/marx-search-release.lock',(tmp_path/'release.lock').as_posix())
    script=script.replace('/run/marx-search-watchdog.last-restart',(tmp_path/'restart-stamp').as_posix())
    prefix='flock() { return 1; }; logger() { :; }; systemctl() { echo UNEXPECTED; return 0; }; curl() { echo UNEXPECTED; return 1; }; '
    result=subprocess.run([bash,'--noprofile','--norc','-c',prefix+script],capture_output=True,text=True)
    assert result.returncode==0, result.stderr
    assert 'UNEXPECTED' not in result.stdout
    assert not (tmp_path/'restart-stamp').exists()
    assert 'deferring watchdog probe' in result.stdout


def test_all_release_exit_paths_restore_watchdog_and_use_managed_script():
    promote=(ROOT/'deploy/promote_release.sh').read_text(encoding='utf-8')
    rollback=(ROOT/'deploy/rollback_release.sh').read_text(encoding='utf-8')
    unit=(ROOT/'deploy/marx-search-watchdog.service').read_text(encoding='utf-8')
    assert 'ExecStart=/bin/bash /opt/marx-search/current/app/deploy/health_watchdog.sh' in unit
    assert promote.index('pause_watchdog_for_release') < promote.index('scripts/deployment_smoke.py --mode server')
    assert 'resume_watchdog_after_release' in promote[promote.index('cleanup_incomplete()'):promote.index('trap cleanup_incomplete EXIT')]
    assert rollback.index('pause_watchdog_for_release') < rollback.index('systemd-run')
    assert 'trap cleanup_rollback EXIT' in rollback
    assert 'resume_watchdog_after_release' in rollback[rollback.index('cleanup_rollback()'):rollback.index('trap cleanup_rollback EXIT')]
