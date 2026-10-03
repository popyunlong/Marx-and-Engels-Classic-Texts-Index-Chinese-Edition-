"""Exercise interrupted cleanup and the detached coordinator client contract."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

def bash_path():
    bash = os.environ.get('MARX_TEST_BASH') or shutil.which('bash')
    if not bash:
        pytest.skip('bash unavailable')
    return bash

def unix_path(bash, path):
    if os.name == 'nt':
        return subprocess.check_output([bash, '-c', 'cygpath -u "$1"', '--', str(path)], text=True).strip()
    return str(path)

@pytest.mark.parametrize('signal,exit_code', [('HUP',129), ('TERM',143)])
@pytest.mark.parametrize('drained', [True, False])
def test_interrupted_candidate_restores_watchdog_and_preserves_busy_source(tmp_path, signal, exit_code, drained):
    bash = bash_path()
    source = (ROOT/'deploy/promote_release.sh').read_text(encoding='utf-8')
    cleanup = source[source.index('cleanup_incomplete() {'):source.index('python3 - "$ARCHIVE"')]
    final = tmp_path/'candidate'; final.mkdir()
    staging = tmp_path/'staging'; staging.mkdir()
    script = '''set -Eeuo pipefail
FINAL="$1"; STAGING="$2"; KEEP_FINAL=0; CANDIDATE_UNIT=isolated.service
WATCHDOG_TIMER_WAS_ACTIVE=1
systemctl() { [ "$1" = is-active ]; }
resume_watchdog_after_release() { echo restored; }
retire_candidate_if_drained() { return DRAIN_STATUS; }
'''.replace('DRAIN_STATUS','0' if drained else '1') + cleanup + '\nkill -SIGNAL "$$"\n'.replace('SIGNAL', signal)
    result = subprocess.run([bash, '--noprofile','--norc','-c',script,'--',
        unix_path(bash,final), unix_path(bash,staging)], capture_output=True,text=True,timeout=10)
    assert result.returncode == exit_code, result.stderr
    assert 'restored' in result.stdout
    assert final.exists() is not drained
    assert not staging.exists()

@pytest.mark.parametrize('status', [0, 7])
def test_supervised_transaction_client_reports_unit_exit_and_uses_journal(tmp_path, status):
    bash = bash_path()
    bin_dir = tmp_path/'bin'; bin_dir.mkdir()
    args_file = tmp_path/'args'
    (bin_dir/'systemd-run').write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$TEST_ARGS"\nsleep .2\nexit "$TEST_STATUS"\n')
    (bin_dir/'journalctl').write_text('#!/usr/bin/env bash\nexec sleep 60\n')
    subprocess.run([bash,'-c','chmod +x "$1/systemd-run" "$1/journalctl"','--',unix_path(bash,bin_dir)],check=True)
    script = (ROOT/'deploy/run_release_transaction.sh').read_text()
    # Add the fixture PATH inside bash so Git Bash handles drive-letter PATHs.
    script = 'PATH="$TEST_BIN:$PATH"\n' + script
    result = subprocess.run([bash,'--noprofile','--norc','-c',script,'--',
        '/opt/app','/var/tmp/archive.tar.gz','old','release-a','','0123456789abcdef0123456789abcdef',''],
        env=dict(os.environ, TEST_BIN=unix_path(bash,bin_dir), TEST_ARGS=unix_path(bash,args_file), TEST_STATUS=str(status)),
        capture_output=True,text=True,timeout=10)
    assert result.returncode == status, result.stderr
    args = args_file.read_text().splitlines()
    assert '--unit=marx-search-release-release-a' in args
    assert '--wait' in args and '--collect' in args
    assert '--property=StandardOutput=journal' in args
    assert '--property=StandardError=journal' in args
    assert '--pipe' not in args and '--send-sighup' not in args
    assert args[-7:] == ['/opt/app','/var/tmp/archive.tar.gz','old','release-a','','0123456789abcdef0123456789abcdef','']
