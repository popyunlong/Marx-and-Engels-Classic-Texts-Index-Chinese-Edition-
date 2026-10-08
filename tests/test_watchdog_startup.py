"""A slow valid startup must finish; a mature hang must still be recovered."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("age,healthy,last_restart,caught,restart,dump", [
    (119, False, 0, True, False, False),
    (270, False, 0, True, False, False),
    (599, False, 0, True, False, False),
    (600, False, 0, True, True, True),
    (900, True, 0, True, False, False),
    (900, False, 0, False, True, False),
    (900, False, 9990, True, False, False),
])
def test_watchdog_startup_and_mature_hang(tmp_path, age, healthy, last_restart, caught, restart, dump):
    bash = os.environ.get("MARX_TEST_BASH") or shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable")
    script = (ROOT / "deploy/health_watchdog.sh").read_text(encoding="utf-8")
    script = script.replace("/run/lock/marx-search-release.lock", (tmp_path / "lock").as_posix())
    script = script.replace("/run/marx-search-watchdog.last-restart", (tmp_path / "stamp").as_posix())
    script = script.replace("/proc/uptime", (tmp_path / "uptime").as_posix())
    script = script.replace("/proc/$main_pid/status", (tmp_path / "status").as_posix())
    (tmp_path / "uptime").write_text("1000.0 1000.0\n")
    (tmp_path / "status").write_text("SigCgt:\t" + ("0000000000000200" if caught else "0000000000000000") + "\n")
    if last_restart:
        (tmp_path / "stamp").write_text(str(last_restart))
    prefix = f'''
flock() {{ return 0; }}
logger() {{ :; }}
sleep() {{ :; }}
date() {{ echo 10000; }}
curl() {{ echo PROBE; return {0 if healthy else 1}; }}
systemctl() {{
  case "$1" in
    is-active) return 0 ;;
    show) if [ "$4" = MainPID ]; then echo 123; else echo {(1000-age)*1000000}; fi ;;
    *) echo "SYSTEMCTL $*" ;;
  esac
}}
'''
    result = subprocess.run([bash, "--noprofile", "--norc", "-c", prefix + script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert ("SYSTEMCTL restart marx-search" in result.stdout) is restart
    assert ("SYSTEMCTL kill --kill-who=main --signal=SIGUSR1 marx-search" in result.stdout) is dump
    assert result.stdout.count("PROBE") == (0 if age < 600 else 1 if healthy else 3)
    if dump:
        assert result.stdout.index("SYSTEMCTL kill") < result.stdout.index("SYSTEMCTL restart")
    if age < 600:
        assert not (tmp_path / "stamp").exists()


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="Linux runtime signal")
def test_thread_dump_signal_does_not_terminate_process(tmp_path):
    output = tmp_path / "stacks.log"
    with output.open("w") as err:
        child = subprocess.Popen([sys.executable, "-u", "-c", "from runtime_diagnostics import enable_thread_dumps; enable_thread_dumps(); print('ready'); input()"],
                                 cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
        try:
            assert child.stdout.readline().strip() == "ready"
            os.kill(child.pid, signal.SIGUSR1)
            child.communicate("\n", timeout=5)
            assert child.returncode == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
    assert "<module>" in output.read_text()
