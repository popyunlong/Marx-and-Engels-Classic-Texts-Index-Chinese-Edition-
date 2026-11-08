"""One-shot desktop fixture: synthetic corpus, temporary Key, reversible config.

Create busy.flag to hold the real cross-process API slot; create stop.flag to
revoke the Key, stop the servers and restore only this fixture's config entry.
This starts no pytest/protocol scenarios and never uses production credentials.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_api_store import Store

OUT = Path('D:/CodexData/outputs/agent-query-api-20261107/workbuddy-client')
TEMP = Path('D:/CodexData/temp/agent-api-20261107/workbuddy-client')
LEDGER = ROOT / 'docs/ops/AGENT_API_TEST_LEDGER_20261107.json'
CONFIG = Path('C:/Users/10108/.workbuddy/mcp.json')
NAME = 'marx_api_acceptance'


def main():
    global OUT, TEMP
    parser = argparse.ArgumentParser()
    parser.add_argument('--client', choices=('workbuddy', 'harness'), default='workbuddy')
    parser.add_argument('--authorization')
    args = parser.parse_args()
    ledger = json.loads(LEDGER.read_text('utf-8'))
    phase = 'workbuddy-real-client'
    if args.authorization:
        grants = ledger.get('repeat_authorizations', [])
        grant = next((g for g in grants if g.get('id') == args.authorization), None)
        if not grant or grant.get('desktop_clients') != ['workbuddy', 'harness']:
            raise SystemExit('No matching explicit desktop repeat authorization.')
        phase = f'{args.client}-desktop-{args.authorization}'
        OUT = OUT.parent / args.authorization / args.client
        TEMP = TEMP.parent / args.authorization / args.client
    elif args.client != 'workbuddy':
        raise SystemExit('Harness repeat requires explicit authorization.')
    if any(r.get('phase') == phase for r in ledger['runs']):
        raise SystemExit('Already attempted: inspect the ledger; never restart implicitly.')
    OUT.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir(parents=True, exist_ok=True)
    original = CONFIG.read_bytes() if args.client == 'workbuddy' and CONFIG.exists() else None
    settings = json.loads(original) if original else {'mcpServers': {}}
    if NAME in settings.get('mcpServers', {}):
        raise SystemExit('Test server name already exists; preserving it.')
    if original is not None:
        (OUT / 'mcp.before.json').write_bytes(original)
    record = {'phase': phase, 'status': 'preparing',
              'started': datetime.now(timezone.utc).isoformat(), 'client': args.client,
              'authorization': args.authorization,
              'artifact_root': str(OUT), 'scenario': 'Native desktop MCP connection, sources, search, passage and busy error',
              'original_config_existed': original is not None}
    ledger['runs'].append(record)

    def save():
        # Keep any separately recorded results, updating only this attempt.
        current = json.loads(LEDGER.read_text('utf-8'))
        entries = current['runs']
        matches = [i for i, r in enumerate(entries) if r.get('phase') == record['phase']]
        if matches:
            entries[matches[0]] = record
        else:
            entries.append(record)
        LEDGER.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n', 'utf-8')

    save()
    env = dict(os.environ, TEMP=str(TEMP), TMP=str(TEMP), PYTHONUTF8='1',
               PYTHONDONTWRITEBYTECODE='1', MARX_RUNTIME_LOG_DIR=str(OUT / 'runtime-logs'))
    backend = adapter = slot = None
    configured = False
    try:
        backend_log = (OUT / 'backend.log').open('w', encoding='utf-8')
        backend = subprocess.Popen(['C:/Users/10108/AppData/Local/Programs/Python/Python310/python.exe',
            str(ROOT / 'tests/agent_api_fixture_server.py'), str(OUT)], cwd=ROOT, env=env,
            stdout=backend_log, stderr=backend_log)
        for _ in range(120):
            if (OUT / 'fixture.json').exists():
                break
            if backend.poll() is not None:
                raise RuntimeError('Backend startup failed')
            time.sleep(.25)
        fixture = json.loads((OUT / 'fixture.json').read_text('utf-8'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env['MARX_AGENT_API_UPSTREAM'] = f"http://127.0.0.1:{fixture['port']}/api/v1"
        adapter_log = (OUT / 'adapter.log').open('w', encoding='utf-8')
        adapter = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'agent_api_mcp:app',
            '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=ROOT,
            env=env, stdout=adapter_log, stderr=adapter_log)
        # Wait for a listening socket, without making API or MCP requests.
        for _ in range(120):
            if adapter.poll() is not None:
                raise RuntimeError('MCP startup failed')
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    break
            time.sleep(.25)
        settings.setdefault('mcpServers', {})[NAME] = {
            'type': 'streamableHttp', 'url': f'http://127.0.0.1:{port}/mcp',
            'headers': {'Authorization': 'Bearer ' + fixture['key']}, 'timeout': 30000}
        if args.client == 'workbuddy':
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', 'utf-8')
            configured = True
        record.update(status='client_configuration_ready', mcp_url=f'http://127.0.0.1:{port}/mcp',
                      backend_pid=backend.pid, adapter_pid=adapter.pid)
        save()
        print(f'Native {args.client} loopback fixture ready; no test Key printed.', flush=True)
        while not (OUT / 'stop.flag').exists():
            if backend.poll() is not None or adapter.poll() is not None:
                raise RuntimeError('Fixture process exited')
            if (OUT / 'busy.flag').exists() and slot is None:
                slot = Store(OUT / 'state').admission(999)
                slot.__enter__()
                print('Shared API slot held for native-client busy scenario.', flush=True)
            elif not (OUT / 'busy.flag').exists() and slot is not None:
                slot.__exit__(None, None, None)
                slot = None
            time.sleep(.25)
    finally:
        if slot is not None:
            slot.__exit__(None, None, None)
        db_path = OUT / 'state/agent_api.sqlite3'
        if db_path.exists():
            with sqlite3.connect(db_path) as db:
                db.execute('UPDATE api_keys SET revoked=? WHERE revoked IS NULL', (time.time(),))
        if configured:
            current = json.loads(CONFIG.read_text('utf-8'))
            if current.get('mcpServers', {}).get(NAME) == settings['mcpServers'][NAME]:
                del current['mcpServers'][NAME]
                baseline = json.loads(original) if original else {'mcpServers': {}}
                if original is not None and current == baseline:
                    CONFIG.write_bytes(original)
                else:
                    CONFIG.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n', 'utf-8')
                record['temporary_entry_removed'] = True
        for process in (adapter, backend):
            if process is not None:
                process.terminate()
                process.wait(timeout=15)
        (OUT / 'fixture.json').unlink(missing_ok=True)
        record.update(fixture_stopped=True, synthetic_keys_revoked=True,
                      fixture_finished=datetime.now(timezone.utc).isoformat())
        save()


if __name__ == '__main__':
    main()
