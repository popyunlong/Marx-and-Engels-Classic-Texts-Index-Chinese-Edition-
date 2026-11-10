"""One execution per phase, preserving failures/interruption and exact source hashes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'docs/ops/CHAPTER_SCOPE_API_TEST_LEDGER_20261110.json'


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['backend', 'browser', 'edges', 'directory-edge', 'export-history-edge', 'static'])
    args = parser.parse_args()
    ledger = json.loads(LEDGER.read_text('utf-8'))
    if any(r['phase'] == args.phase for r in ledger['runs']):
        raise SystemExit('This phase already has a record; explicit rerun authorization is required.')
    output = Path(ledger['artifact_root'])
    output.mkdir(parents=True, exist_ok=True)
    temp = Path('D:/CodexData/temp/chapter-scope-api-20261110')
    temp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE='1',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHON=sys.executable,
               TEST_OUTPUT=str(output), BROWSER_CHANNEL='msedge',
               NODE_PATH='C:/Users/10108/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules')
    commands = {
        'backend': [[sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-q',
                     'tests/test_chapter_scope.py', 'tests/test_api_access.py',
                     'tests/test_agent_api.py', 'tests/test_agent_api_edges.py', 'tests/test_agent_api_main_integration.py',
                     'tests/test_search_export_consistency.py', 'tests/test_search_overlap_count.py',
                     '--basetemp=' + str(temp / 'pytest'), '--junitxml=' + str(output / 'backend.xml')]],
        'browser': [['node', '--test', 'tests/browser/chapter_scope.cjs']],
        'edges': [[sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-q',
                   'tests/test_chapter_edges.py', 'tests/test_layout_exact.py',
                   '--basetemp=' + str(temp / 'edges'), '--junitxml=' + str(output / 'edges.xml')]],
        'directory-edge': [['node', '--test', 'tests/browser/chapter_stale_directory.cjs']],
        'export-history-edge': [[sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-q',
                      'tests/test_chapter_export_filter.py', '--basetemp=' + str(temp / 'export-filter'),
                      '--junitxml=' + str(output / 'export-filter.xml')],
                      ['node', '--test', 'tests/chapter_empty_history.cjs']],
        'static': [['git', 'diff', '--check'], ['node', '--check', 'static/book-scope/chapter-scope.js'],
                   [sys.executable, '-c', "import pathlib,yaml; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text('utf-8'))"]],
    }[args.phase]
    names = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD'], cwd=ROOT, text=True).splitlines()
    names += subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=ROOT, text=True).splitlines()
    record = {'phase': args.phase, 'status': 'running', 'commands': commands,
              'started_at': datetime.now(timezone.utc).isoformat(),
              'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                for name in names if (ROOT / name).is_file() and not name.endswith('LEDGER_20261110.json')},
              'log': str(output / (args.phase + '.log'))}
    ledger['runs'].append(record)
    def save():
        LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    save()
    code = 1
    try:
        with Path(record['log']).open('w', encoding='utf-8') as stream:
            for command in commands:
                code = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT).returncode
                if code:
                    break
        record.update(status='passed' if code == 0 else 'failed', exit_code=code)
    except BaseException as exc:
        record.update(status='interrupted', error=type(exc).__name__)
        raise
    finally:
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        print(Path(record['log']).read_text('utf-8')[-16000:])
    return code


if __name__ == '__main__':
    raise SystemExit(main())
