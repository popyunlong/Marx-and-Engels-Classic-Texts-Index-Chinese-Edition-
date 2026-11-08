"""One recorded attempt per installed client SDK; no desktop automation."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'docs/ops/AGENT_API_TEST_LEDGER_20261107.json'
OUT = Path('D:/CodexData/outputs/agent-query-api-20261107/bundled-sdk')


def main():
    ledger = json.loads(LEDGER.read_text('utf-8'))
    if any(r.get('phase') == 'bundled-sdk-first-compatibility' for r in ledger['runs']):
        raise SystemExit('Already attempted; implicit retry prohibited.')
    OUT.mkdir(parents=True, exist_ok=True)
    temp = Path('D:/CodexData/temp/agent-api-20261107/bundled-sdk')
    temp.mkdir(parents=True, exist_ok=True)
    record = {'phase': 'bundled-sdk-first-compatibility', 'status': 'running',
              'started': datetime.now(timezone.utc).isoformat(), 'attempts': [],
              'new_coverage': 'Installed WorkBuddy SDK 1.29.0 and Harness SDK 2.2.0, including automatic era negotiation and SDK result/error validation; not a rerun of manual Python HTTP protocol test.',
              'desktop_acceptance': False, 'artifact_root': str(OUT),
              'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in list(ROOT.glob('agent_api*.py')) + [Path(__file__), Path(__file__).with_suffix('.cjs')]}}
    ledger['runs'].append(record)

    def save():
        LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n', 'utf-8')

    save()
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONUTF8='1',
               PYTHONDONTWRITEBYTECODE='1', AGENT_API_TEST_OUTPUT=str(OUT),
               AGENT_API_ROOT=str(ROOT), AGENT_API_MCP_PYTHON=sys.executable,
               MARX_RUNTIME_LOG_DIR=str(OUT / 'runtime-logs'))
    backend = adapter = None
    try:
        log = (OUT / 'backend.log').open('w', encoding='utf-8')
        backend = subprocess.Popen([ledger['backend_python'], str(ROOT / 'tests/agent_api_fixture_server.py'), str(OUT)],
                                   cwd=ROOT, env=env, stdout=log, stderr=log)
        for _ in range(120):
            if (OUT / 'fixture.json').exists():
                break
            if backend.poll() is not None:
                raise RuntimeError('Backend fixture startup failed')
            time.sleep(.25)
        fixture = json.loads((OUT / 'fixture.json').read_text('utf-8'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        fixture['mcp_url'] = f'http://127.0.0.1:{port}/mcp'
        (OUT / 'fixture.json').write_text(json.dumps(fixture), 'utf-8')
        env['MARX_AGENT_API_UPSTREAM'] = f"http://127.0.0.1:{fixture['port']}/api/v1"
        alog = (OUT / 'adapter.log').open('w', encoding='utf-8')
        adapter = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'agent_api_mcp:app', '--host',
                                   '127.0.0.1', '--port', str(port), '--no-access-log'],
                                  cwd=ROOT, env=env, stdout=alog, stderr=alog)
        for _ in range(120):
            if adapter.poll() is not None:
                raise RuntimeError('MCP fixture startup failed')
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    break
            time.sleep(.25)
        for profile in ('workbuddy', 'harness'):
            command = ['C:/Users/10108/AppData/Local/Programs/DeepSeek Harness/DeepSeek Harness.exe',
                       str(Path(__file__).with_suffix('.cjs')), profile]
            attempt = {'profile': profile, 'command': command, 'status': 'running',
                       'log': str(OUT / (profile + '.log'))}
            record['attempts'].append(attempt)
            save()
            try:
                with Path(attempt['log']).open('w', encoding='utf-8') as stream:
                    code = subprocess.run(command, cwd=ROOT, env=dict(env, ELECTRON_RUN_AS_NODE='1'),
                                          stdout=stream, stderr=subprocess.STDOUT, timeout=90).returncode
                attempt.update(status='passed' if code == 0 else 'failed', exit_code=code)
            except subprocess.TimeoutExpired:
                attempt.update(status='interrupted', reason='90 second client deadline')
            save()
            print(profile + ': ' + attempt['status'], flush=True)
            print(Path(attempt['log']).read_text('utf-8')[-2500:], flush=True)
        record['status'] = 'passed' if all(r['status'] == 'passed' for r in record['attempts']) else 'failed'
    except BaseException as error:
        record.update(status='interrupted', error_type=type(error).__name__)
        raise
    finally:
        db_path = OUT / 'state/agent_api.sqlite3'
        if db_path.exists():
            with sqlite3.connect(db_path) as db:
                db.execute('UPDATE api_keys SET revoked=? WHERE revoked IS NULL', (time.time(),))
        for process in (adapter, backend):
            if process is not None:
                process.terminate()
                process.wait(timeout=15)
        (OUT / 'fixture.json').unlink(missing_ok=True)
        record.update(finished=datetime.now(timezone.utc).isoformat(), synthetic_keys_revoked=True)
        save()


if __name__ == '__main__':
    main()
