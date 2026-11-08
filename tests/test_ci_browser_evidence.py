"""Exercise the actual browser job pipeline without rerunning browser tests."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('exit_code', [0, 19])
def test_browser_pipeline_preserves_failure_and_invokes_tests_once(tmp_path, exit_code):
    bash = os.environ.get('MARX_TEST_BASH') or shutil.which('bash')
    if not bash:
        pytest.skip('bash unavailable')
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text('utf-8'))
    step = next(s for s in workflow['jobs']['browser-gate']['steps']
                if s.get('name') == 'Check AI controls in a real browser without live API calls')
    # Stub only the test runner. Execute the workflow's real shell pipeline and
    # logging, so a false-green regression cannot be hidden behind tee again.
    shell = r'''
node() {
  printf 'one invocation\n' >> "$RUNNER_TEMP/invocations"
  printf 'fixture browser output\n'
  return "$BROWSER_FIXTURE_EXIT"
}
''' + step['run']
    result = subprocess.run([bash, '--noprofile', '--norc', '-c', shell],
                            cwd=ROOT, capture_output=True, text=True, timeout=15,
                            env=dict(os.environ, RUNNER_TEMP=tmp_path.as_posix(),
                                     BROWSER_FIXTURE_EXIT=str(exit_code)))
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert (tmp_path / 'invocations').read_text() == 'one invocation\n'
    assert (tmp_path / 'ai-controls-browser.log').read_text() == 'fixture browser output\n'
