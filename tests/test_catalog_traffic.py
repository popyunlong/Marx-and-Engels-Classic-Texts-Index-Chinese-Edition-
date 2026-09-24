"""Execute traffic helpers with isolated filesystem and fake services, never a live host."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_helper(tmp_path, scenario):
    bash = os.environ.get('MARX_TEST_BASH') or shutil.which('bash')
    if not bash:
        pytest.skip('bash unavailable')
    script = r'''
set -eu
source "$1"
REHEARSAL_DIR="$2"
CADDYFILE="$REHEARSAL_DIR/Caddyfile"
CANDIDATE_UNIT=fixture-candidate.service
CANDIDATE_PORT=8001
DRAIN_TIMEOUT_SECONDS=2
printf 'reverse_proxy 127.0.0.1:8000\n' > "$CADDYFILE"
ss() {
  [ "${PROBE_FAIL:-0}" = 0 ] || return 1
  if [ "${ACTIVE:-0}" = 1 ]; then printf 'ESTABLISHED fixture connection\n'; fi
}
sleep() { :; }
systemctl() {
  printf '%s\n' "$*" >> "$REHEARSAL_DIR/calls"
  if [ "$1" = is-active ]; then return 1; fi
  if [ "$1" = reload ] && [ "${RELOAD_FAIL:-0}" = 1 ]; then return 1; fi
}
caddy() { [ "${VALIDATE_FAIL:-0}" = 0 ]; }
mktemp() { command mktemp "$REHEARSAL_DIR/mock.XXXXXX"; }
install() { command cp -- "${@: -2:1}" "${@: -1}"; }
''' + scenario
    result = subprocess.run([bash, '--noprofile', '--norc', '-c', script, '--',
                             str(ROOT / 'deploy/release_traffic.sh'), str(tmp_path)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return (tmp_path / 'calls').read_text() if (tmp_path / 'calls').exists() else ''


def test_active_stream_blocks_retirement(tmp_path):
    calls = run_helper(tmp_path, 'ACTIVE=1\nif retire_candidate_if_drained test; then exit 11; fi\n')
    assert 'stop fixture-candidate' not in calls


def test_failed_connection_probe_blocks_retirement(tmp_path):
    calls = run_helper(tmp_path, 'PROBE_FAIL=1\nif retire_candidate_if_drained test; then exit 11; fi\n')
    assert 'stop fixture-candidate' not in calls


def test_drained_candidate_can_retire(tmp_path):
    calls = run_helper(tmp_path, 'retire_candidate_if_drained test\n')
    assert 'stop fixture-candidate.service' in calls


def test_failed_validation_preserves_original_route(tmp_path):
    calls = run_helper(tmp_path, 'VALIDATE_FAIL=1\nif switch_caddy 8000 8001; then exit 11; fi\n')
    assert (tmp_path / 'Caddyfile').read_text().strip() == 'reverse_proxy 127.0.0.1:8000'
    assert 'reload' not in calls


def test_failed_reload_restores_original_route(tmp_path):
    calls = run_helper(tmp_path, 'RELOAD_FAIL=1\nif switch_caddy 8000 8001; then exit 11; fi\n')
    assert (tmp_path / 'Caddyfile').read_text().strip() == 'reverse_proxy 127.0.0.1:8000'
    assert calls.count('reload caddy') == 2


def test_successful_cutover_uses_expected_upstream(tmp_path):
    calls = run_helper(tmp_path, 'switch_caddy 8000 8001\n')
    assert (tmp_path / 'Caddyfile').read_text().strip() == 'reverse_proxy 127.0.0.1:8001'
    assert calls.count('reload caddy') == 1
