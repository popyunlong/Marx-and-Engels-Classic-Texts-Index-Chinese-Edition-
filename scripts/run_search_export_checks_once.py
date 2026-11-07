"""Task-specific verification runner: retain every attempt and refuse reruns."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'docs/ops/SEARCH_EXPORT_TEST_LEDGER_20261107.json'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['backend', 'frontend'])
    args = parser.parse_args()
    ledger = json.loads(LEDGER.read_text('utf-8'))
    if any(row['phase'] == args.phase for row in ledger['runs']):
        raise SystemExit('Already attempted. Inspect evidence; explicit user approval is required for any rerun.')
    output = Path(ledger['artifact_root'])
    output.mkdir(parents=True, exist_ok=True)
    temp = Path('D:/CodexData/temp/search-export-consistency-20261107')
    temp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE='1',
               PYTHONUTF8='1', MARX_RUNTIME_LOG_DIR=str(output / 'runtime-logs'))
    files = ['search.py', 'search_exports.py', 'app.py', 'personal_corpus.py', 'ingestion/claims.py',
             'templates/index.html', 'tests/test_search_export_consistency.py', 'tests/search_export_status.cjs']
    command = ([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-q',
                'tests/test_search_export_consistency.py', 'tests/test_layout_exact.py',
                'tests/test_search_overlap_count.py', 'tests/test_cooccurrence_scope.py',
                '--basetemp=' + str(output / 'pytest-temp'), '--junitxml=' + str(output / 'backend.xml')]
               if args.phase == 'backend' else ['node', '--test', 'tests/search_export_status.cjs'])
    record = dict(phase=args.phase, status='running', command=command,
                  started_at=datetime.now(timezone.utc).isoformat(),
                  source_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files},
                  log=str(output / (args.phase + '.log')))
    ledger['runs'].append(record)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    code = 1
    try:
        with Path(record['log']).open('w', encoding='utf-8') as log:
            code = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
        record.update(exit_code=code, status='passed' if code == 0 else 'failed')
    except BaseException as exc:
        record.update(status='interrupted', error=str(exc))
        raise
    finally:
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        if Path(record['log']).exists():
            print(Path(record['log']).read_text('utf-8')[-10000:])
    return code


if __name__ == '__main__':
    raise SystemExit(main())
