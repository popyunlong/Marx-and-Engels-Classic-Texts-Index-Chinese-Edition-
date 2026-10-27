"""Execute one CI pytest command, preserving its exit status and evidence.

There are no retries. An existing evidence directory is never overwritten.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--output-root', default=os.environ.get('RUNNER_TEMP'))
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', args.label):
        parser.error('label must be a short lowercase slug')
    if not args.output_root:
        parser.error('RUNNER_TEMP or --output-root is required')
    pytest_args = args.pytest_args
    if pytest_args[:1] == ['--']:
        pytest_args = pytest_args[1:]
    if not pytest_args:
        parser.error('pytest arguments are required')
    evidence = Path(args.output_root) / 'pytest-evidence' / args.label
    evidence.mkdir(parents=True, exist_ok=False)
    log = evidence / 'pytest.log'
    command = [sys.executable, '-m', 'pytest', *pytest_args,
               '--junitxml=' + str(evidence / 'junit.xml')]
    record = dict(command=command, head=os.environ.get('GITHUB_SHA'),
                  started=datetime.now(timezone.utc).isoformat(),
                  status='running_or_interrupted')
    metadata = evidence / 'run.json'
    metadata.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    with log.open('wb') as output, subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        while chunk := process.stdout.read1(65536):
            output.write(chunk)
            output.flush()
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        code = process.wait()
    record.update(status='passed' if code == 0 else 'failed', exit_code=code,
                  finished=datetime.now(timezone.utc).isoformat())
    metadata.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    if code:
        with log.open('rb') as source:
            source.seek(max(0, log.stat().st_size - 24000))
            detail = source.read().decode('utf-8', errors='replace')
        detail = detail.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print(f'::error title=pytest {args.label}::{detail}', flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
