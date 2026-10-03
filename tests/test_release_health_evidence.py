"""Diagnose forward timeouts without logging data or weakening health checks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('side,timeout,invalid', [
    ('live', False, False), ('candidate', False, False),
    ('candidate', True, False), ('candidate', False, True),
])
def test_release_records_server_timing_and_stops_on_failure(tmp_path, side, timeout, invalid):
    bash = os.environ.get('MARX_TEST_BASH') or shutil.which('bash')
    if not bash:
        pytest.skip('bash unavailable')
    source = (ROOT/'deploy/promote_release.sh').read_text(encoding='utf-8')
    functions = source[source.index('record_health_probe() {'):source.index('wait_health() {')]
    executable = sys.executable
    if os.name == 'nt':
        executable = subprocess.check_output([bash,'-c','cygpath -u "$1"','--',executable],text=True).strip()
    fixture = '''set -Eeuo pipefail
PRIMARY_PORT=8000; RELEASE_ID=new; RELEASES=/isolated/nonexistent; FINAL=/isolated/nonexistent
python3() { "$TEST_PYTHON" "$@"; }
curl() {
  local url="${@: -1}"
  if [[ "$url" == */api/runtime ]]; then
    printf '{"app_release":{"id":"new"},"layout_exact_ready":true,"private":"DO_NOT_LOG_RESPONSE"}\\n'
    if [ "$INVALID_TEST" = 1 ]; then printf 'invalid'; else printf '0.015:200'; fi
  elif [ "$TIMEOUT_TEST" = 1 ] && [[ "$url" == *:8001/ ]]; then
    printf '10.001:000'; return 28
  else
    printf '0.020:200'
  fi
}
'''
    script = fixture + functions + '\nhealth ' + ('8000' if side == 'live' else '8001') + ' new\n'
    result = subprocess.run([bash,'--noprofile','--norc','-c',script],capture_output=True,text=True,
                            env=dict(os.environ,TEST_PYTHON=executable,TIMEOUT_TEST='1' if timeout else '0',
                                     INVALID_TEST='1' if invalid else '0'),timeout=10)
    assert result.returncode == (1 if timeout or invalid else 0), result.stderr
    lines = [json.loads(line.split('=',1)[1]) for line in result.stderr.splitlines()
             if line.startswith('RELEASE_HEALTH_OBSERVATION=')]
    expected = ['/api/runtime'] if invalid else (['/api/runtime','/'] if timeout else
                ['/api/runtime','/','/pricing','/ai','/v2/ai','/v2/read'])
    assert [r['route'] for r in lines] == expected
    assert all(r['side'] == side and r['source'] == 'server_loopback' and r['at'] > 0 for r in lines)
    assert 'DO_NOT_LOG_RESPONSE' not in result.stdout + result.stderr
    if invalid:
        assert lines[-1]['curl_exit'] == 0
        assert lines[-1]['status'] is None and lines[-1]['seconds'] is None
        assert lines[-1]['category'] == 'invalid_timing'
    elif timeout:
        assert lines[-1]['curl_exit'] == 28 and lines[-1]['status'] == '000'
        assert lines[-1]['seconds'] == 10.001
    else:
        assert all(r['curl_exit'] == 0 and r['status'] == '200' for r in lines)
