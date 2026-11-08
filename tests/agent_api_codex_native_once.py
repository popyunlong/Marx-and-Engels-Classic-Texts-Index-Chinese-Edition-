"""First native Codex app-server MCP test; no inference or persistent chat.

Uses the installed CLI's generated protocol: ephemeral thread/start and direct
mcpServer/tool/call. Does not alter CODEX_HOME or the user's config.toml.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'docs/ops/AGENT_API_TEST_LEDGER_20261107.json'
OUT = Path('D:/CodexData/outputs/agent-query-api-20261107/codex-native')
SERVER = 'marx_acceptance'


class AppServer:
    def __init__(self, executable, endpoint, token, env):
        self.counter = 0
        self.messages = queue.Queue()
        self.trace = []
        self.token = token
        config = ('mcp_servers={' + SERVER + '={url=' + json.dumps(endpoint)
                  + ',bearer_token_env_var="MARX_CODEX_ACCEPTANCE_KEY",startup_timeout_sec=20,tool_timeout_sec=30}}')
        command = [executable, 'app-server', '--listen', 'stdio://', '-c', config,
                   '-c', 'features.apps=false', '-c', 'sqlite_home=' + json.dumps(str(OUT / 'sqlite')),
                   '-c', 'log_dir=' + json.dumps(str(OUT / 'logs'))]
        self.stderr = (OUT / 'app-server.stderr.log').open('w', encoding='utf-8')
        self.process = subprocess.Popen(command, cwd=ROOT, env=dict(env, MARX_CODEX_ACCEPTANCE_KEY=token),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                                        text=True, encoding='utf-8', bufsize=1)

        def read():
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.trace.append({'non_json_stdout': True})
            self.messages.put(None)

        threading.Thread(target=read, daemon=True).start()

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def rpc(self, method, params, timeout=45):
        self.counter += 1
        ident = self.counter
        self.trace.append({'direction': 'request', 'method': method, 'id': ident})
        self.send({'id': ident, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            if message is None:
                raise RuntimeError('Native app-server closed before response')
            self.trace.append({'direction': 'response' if 'method' not in message else 'notification',
                               'method': message.get('method'), 'id': message.get('id'),
                               'error_code': message.get('error', {}).get('code')})
            if message.get('id') == ident and 'method' not in message:
                if 'error' in message:
                    raise RuntimeError(str(message['error']).replace(self.token, '[REDACTED]'))
                return message['result']
            if 'method' in message and 'id' in message:
                # Never grant permissions, authenticate, or approve on behalf of a person.
                self.send({'id': message['id'], 'error': {'code': -32601,
                           'message': 'Test client does not handle interactive requests'}})
        raise TimeoutError('Native app-server response deadline')

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.stderr.close()
        (OUT / 'rpc-trace.json').write_text(json.dumps(self.trace, indent=2) + '\n', 'utf-8')


def main():
    ledger = json.loads(LEDGER.read_text('utf-8'))
    phase = 'codex-native-app-server-first'
    if any(r.get('phase') == phase for r in ledger['runs']):
        raise SystemExit('Already attempted; explicit retry authorization required.')
    OUT.mkdir(parents=True, exist_ok=True)
    temp = Path('D:/CodexData/temp/agent-api-20261107/codex-native')
    temp.mkdir(parents=True, exist_ok=True)
    work = Path('D:/CodexData/workspaces/agent-api-client-acceptance')
    work.mkdir(parents=True, exist_ok=True)
    record = {'phase': phase, 'status': 'running', 'started': datetime.now(timezone.utc).isoformat(),
              'artifact_root': str(OUT), 'scope': 'Native installed Codex app-server: Bearer env, ephemeral context, discovery and direct MCP tool calls; no model turn, persistent chat or UI automation.',
              'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in list(ROOT.glob('agent_api*.py')) + [Path(__file__)]}}
    ledger['runs'].append(record)

    def save():
        LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n', 'utf-8')

    save()
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONUTF8='1',
               PYTHONDONTWRITEBYTECODE='1', MARX_RUNTIME_LOG_DIR=str(OUT / 'runtime-logs'))
    backend = adapter = locker = client = None
    try:
        executable = shutil.which('codex.exe')
        if not executable:
            raise RuntimeError('Native Codex CLI unavailable')
        record['cli_version'] = subprocess.check_output([executable, '--version'], env=env,
                                                       text=True, encoding='utf-8').strip()
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
        env['MARX_AGENT_API_UPSTREAM'] = f"http://127.0.0.1:{fixture['port']}/api/v1"
        alog = (OUT / 'adapter.log').open('w', encoding='utf-8')
        adapter = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'agent_api_mcp:app', '--host',
                                   '127.0.0.1', '--port', str(port), '--no-access-log'],
                                  cwd=ROOT, env=env, stdout=alog, stderr=alog)
        for _ in range(120):
            if adapter.poll() is not None:
                raise RuntimeError('Adapter fixture startup failed')
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    break
            time.sleep(.25)
        client = AppServer(executable, f'http://127.0.0.1:{port}/mcp', fixture['key'], env)
        client.rpc('initialize', {'clientInfo': {'name': 'corpus_api_native_acceptance', 'version': '1.0.0'},
                                 'capabilities': {'experimentalApi': True}})
        client.send({'method': 'initialized', 'params': {}})
        started = client.rpc('thread/start', {'cwd': str(work), 'ephemeral': True,
                                              'approvalPolicy': 'never', 'sandbox': 'read-only'})
        thread_id = started['thread']['id']
        record['ephemeral_context_created'] = True
        save()
        print('Native Codex ephemeral context ready; no model turn started.', flush=True)
        ready = None
        for _ in range(40):
            status = client.rpc('mcpServerStatus/list', {'threadId': thread_id, 'serverName': SERVER,
                                                       'detail': 'toolsAndAuthOnly'})
            candidates = [r for r in status['data'] if r['name'] == SERVER]
            if candidates and candidates[0].get('runtimeStatus') == 'failed':
                raise RuntimeError('Native MCP connection failed')
            if candidates and len(candidates[0].get('tools', {})) == 4:
                ready = candidates[0]
                break
            time.sleep(.25)
        assert ready is not None, 'Native MCP discovery did not complete'
        assert ready['authStatus'] == 'bearerToken'
        names = {value.get('name', key).split('__')[-1] for key, value in ready['tools'].items()}
        assert names == {'list_sources', 'search_corpus', 'get_passage', 'get_api_limits'}

        def tool(name, arguments):
            return client.rpc('mcpServer/tool/call', {'threadId': thread_id, 'server': SERVER,
                                                     'tool': name, 'arguments': arguments})

        sources = tool('list_sources', {})
        assert sources.get('isError') is not True and sources['structuredContent']['sources']
        search = tool('search_corpus', {'q': '生产关系', 'mode': 'exact', 'scope': ['book:文集'], 'limit': 1})
        assert search.get('isError') is not True
        evidence = search['structuredContent']['results'][0]
        passage = tool('get_passage', {'evidence_id': evidence['evidence_id']})
        assert passage.get('isError') is not True and '生产关系' in passage['structuredContent']['result']['text']
        limits = tool('get_api_limits', {})
        assert limits['structuredContent']['global_concurrency'] == 1
        code = 'import sys; from agent_api_store import Store; s=Store(sys.argv[1]).admission(999); s.__enter__(); print("HELD",flush=True); sys.stdin.read(); s.__exit__(None,None,None)'
        locker = subprocess.Popen([sys.executable, '-u', '-c', code, str(OUT / 'state')], cwd=ROOT,
                                  env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding='utf-8')
        assert locker.stdout.readline().strip() == 'HELD'
        busy = tool('list_sources', {})
        assert busy['isError'] is True
        assert busy['structuredContent']['error']['code'] == 'concurrency_limited'
        assert busy['structuredContent']['error']['retry_after_seconds'] == 2
        summary = {'status': 'passed', 'cli_version': record['cli_version'], 'auth_status': ready['authStatus'],
                   'tool_names': sorted(names), 'printed_pages': evidence['printed_pages'],
                   'pdf_pages': evidence['pdf_pages'], 'busy_error': busy['structuredContent']['error'],
                   'ephemeral': True, 'model_turns': 0, 'user_config_modified': False}
        (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        record['status'] = 'passed'
        print('PASS: native Codex Bearer authentication, discovery, search, passage, limits and busy error.', flush=True)
    except BaseException as error:
        record.update(status='failed', error_type=type(error).__name__,
                      error_message=str(error).replace(locals().get('fixture', {}).get('key', '\0'), '[REDACTED]')[:1200])
        raise
    finally:
        if locker is not None:
            locker.communicate('', timeout=10)
        if client is not None:
            client.close()
        db_path = OUT / 'state/agent_api.sqlite3'
        if db_path.exists():
            with sqlite3.connect(db_path) as db:
                db.execute('UPDATE api_keys SET revoked=? WHERE revoked IS NULL', (time.time(),))
        for process in (adapter, backend):
            if process is not None:
                process.terminate()
                process.wait(timeout=15)
        (OUT / 'fixture.json').unlink(missing_ok=True)
        record.update(finished=datetime.now(timezone.utc).isoformat(), synthetic_keys_revoked=True,
                      model_turns_started=0, persistent_chat_created=False, user_config_modified=False)
        save()


if __name__ == '__main__':
    main()
