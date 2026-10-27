"""Exercise CI evidence handling without rerunning application tests."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('exit_code', [0, 1])
def test_ci_preserves_exit_status_logs_junit_and_single_invocation(tmp_path, exit_code):
    # This fake pytest module records a single invocation; it collects no tests.
    (tmp_path / 'pytest.py').write_text('''
import os
from pathlib import Path
import sys
with Path('invocations').open('a') as output:
    output.write('one invocation\\n')
Path('arguments.json').write_text(__import__('json').dumps(sys.argv[1:]))
junit = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--junitxml='))
Path(junit).write_text('<testsuite tests="1"/>')
print('fakecase.py:12: AssertionError: detail 100%')
print('second line')
raise SystemExit(int(os.environ['FIXTURE_EXIT']))
''', encoding='utf-8')
    result = subprocess.run([
        sys.executable, '-B', str(ROOT / 'scripts/ci_pytest.py'),
        '--label', 'fixture', '--output-root', str(tmp_path), '--',
        'fakecase.py::test_example', '-q',
    ], cwd=tmp_path, env=dict(os.environ, FIXTURE_EXIT=str(exit_code),
                            PYTHONPATH=str(tmp_path), PYTHONIOENCODING='utf-8'),
        capture_output=True, text=True, encoding='utf-8', timeout=10)
    evidence = tmp_path / 'pytest-evidence/fixture'
    record = json.loads((evidence / 'run.json').read_text())
    assert result.returncode == record['exit_code'] == exit_code
    assert record['status'] == ('failed' if exit_code else 'passed')
    assert (tmp_path / 'invocations').read_text() == 'one invocation\n'
    arguments = json.loads((tmp_path / 'arguments.json').read_text())
    assert arguments == ['fakecase.py::test_example', '-q',
                         '--junitxml=' + str(evidence / 'junit.xml')]
    assert (evidence / 'junit.xml').read_text() == '<testsuite tests="1"/>'
    assert 'detail 100%\nsecond line' in (evidence / 'pytest.log').read_text()
    if exit_code:
        assert '::error title=pytest fixture::' in result.stdout
        newline = '%0D%0A' if os.name == 'nt' else '%0A'
        assert f'detail 100%25{newline}second line{newline}' in result.stdout
    else:
        assert '::error ' not in result.stdout
