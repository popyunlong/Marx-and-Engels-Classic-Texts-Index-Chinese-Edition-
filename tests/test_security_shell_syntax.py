import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", ["deploy/update_cf_ips.sh", "deploy/server_helpers/update_cf_ips.sh",
    "deploy/health_watchdog.sh", "deploy/backup.sh", "deploy/promote_release.sh", "deploy/rollback_release.sh",
    "deploy/release_traffic.sh", "deploy/security_units.sh"])
def test_new_security_shell_syntax(path):
    result = subprocess.run([os.environ["MARX_TEST_BASH"], "-n", (ROOT / path).as_posix()],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
