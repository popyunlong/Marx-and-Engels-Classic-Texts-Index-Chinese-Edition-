"""First isolated rollback-sidecar retirement scenario; no system service access."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'docs/ops/AGENT_API_TEST_LEDGER_20261107.json'


def main():
    ledger = json.loads(LEDGER.read_text('utf-8'))
    phase = 'legacy-mcp-retirement-first-integration'
    if any(r.get('phase') == phase for r in ledger['runs']):
        raise SystemExit('Already attempted; no implicit retry.')
    output = Path(ledger['artifact_root']) / 'legacy-mcp-retirement'
    output.mkdir(parents=True, exist_ok=True)
    release = output / 'legacy-release'
    (release / 'app').mkdir(parents=True, exist_ok=True)
    config = output / 'Caddyfile'
    config.write_text('mazhuzuojiansuo.com {\n\t# BEGIN marx-agent-api-v1\n'
                      '\t@agent_api_mcp path /mcp\n\treverse_proxy @agent_api_mcp 127.0.0.1:18001\n'
                      '\t# END marx-agent-api-v1\n\treverse_proxy 127.0.0.1:8000\n}\n', 'utf-8')
    (output / 'unit-state.txt').write_text('active', 'utf-8')
    shell = '''set -eu
PRIMARY_PORT=8000
MCP_TRANSACTION_APP="$(pwd)"
source deploy/agent_api_units.sh
# Mock only the operating-system boundary. Never contact local/remote services.
ss() { return 0; }
drain_port() { echo "unexpected drain" >&2; return 1; }
systemctl() {
  printf '%s\\n' "$*" >> "$AGENT_TEST_OUT/systemctl.log"
  if [ "$*" != 'disable --now marx-search-agent-api-8000.service' ]; then return 1; fi
  printf inactive > "$AGENT_TEST_OUT/unit-state.txt"
}
python3() { "$AGENT_TEST_PYTHON" "$@"; }
agent_api_route "$AGENT_TEST_RELEASE" 8000 "$AGENT_TEST_CONFIG"
'''
    script = output / 'scenario.sh'
    script.write_text(shell, 'utf-8')
    record = {'phase': phase, 'status': 'running', 'started': datetime.now(timezone.utc).isoformat(),
              'scenario': 'Restoring a pre-MCP primary must remove public MCP routing and retire the failed-promotion primary adapter.',
              'scope': 'New service-retirement integration with mocked ss/systemctl; no standalone syntax check rerun and no production action.',
              'artifact_root': str(output), 'source_sha256': {
                  p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in [ROOT / 'deploy/agent_api_units.sh', ROOT / 'deploy/agent_api_caddy.py', Path(__file__)]}}
    ledger['runs'].append(record)

    def save():
        LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n', 'utf-8')

    save()
    try:
        with (output / 'scenario.log').open('w', encoding='utf-8') as log:
            code = subprocess.run(['D:/Git/bin/bash.exe', str(script)], cwd=ROOT,
                env=dict(os.environ, AGENT_TEST_OUT=str(output), AGENT_TEST_RELEASE=str(release),
                         AGENT_TEST_CONFIG=str(config), AGENT_TEST_PYTHON=ledger['backend_python'],
                         PYTHONDONTWRITEBYTECODE='1'), stdout=log, stderr=subprocess.STDOUT,
                timeout=30).returncode
        record['exit_code'] = code
        assert code == 0, 'Isolated shell scenario failed; inspect scenario.log'
        assert (output / 'unit-state.txt').read_text('utf-8') == 'inactive'
        assert (output / 'systemctl.log').read_text('utf-8').splitlines() == [
            'disable --now marx-search-agent-api-8000.service']
        rendered = config.read_text('utf-8')
        assert 'agent_api_mcp' not in rendered and '/mcp' not in rendered
        assert 'reverse_proxy 127.0.0.1:8000' in rendered
        record['status'] = 'passed'
        print('PASS: legacy primary has no MCP route and no enabled/running stale primary adapter (isolated mocks).')
    except BaseException as error:
        record.update(status='failed', error_type=type(error).__name__)
        raise
    finally:
        record['finished'] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == '__main__':
    main()
