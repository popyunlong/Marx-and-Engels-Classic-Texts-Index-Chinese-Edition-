"""Task-scoped attempt records; never rerun a stage implicitly."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("D:/CodexData/outputs/membership-admin-20261115")
TEMP = Path("D:/CodexData/temp/membership-admin-20261115")
BACKEND = "C:/Users/10108/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
MCP = "D:/CodexData/dependencies/agent-api-mcp-20261107/Scripts/python.exe"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["backend", "browser", "static"])
    stage = parser.parse_args().stage
    OUTPUT.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir(parents=True, exist_ok=True)
    record_path = OUTPUT / (stage + ".json")
    # Exclusive creation preserves failed and interrupted attempts, too.
    with record_path.open("x", encoding="utf-8") as f:
        json.dump({"status": "preparing_or_interrupted", "stage": stage}, f)
    backend_path = os.pathsep.join([
        "D:/CodexData/dependencies/ai-comprehensive-20261105",
        "D:/CodexData/dependencies/history-opensource-20261031/mineru-env/Lib/site-packages",
        str(ROOT), str(ROOT / "tests")])
    env = dict(os.environ, PYTHON=BACKEND, PYTHONPATH=backend_path, ADMIN_BACKEND_PATH=backend_path,
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               TEMP=str(TEMP), TMP=str(TEMP), MARX_SKIP_STARTUP_MAINTENANCE="1",
               MARX_RUNTIME_LOG_DIR=str(OUTPUT / "runtime-logs"), ADMIN_OUTPUT=str(OUTPUT),
               NODE_PATH="C:/Users/10108/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules",
               BROWSER_CHANNEL="chrome")
    commands = {
        "backend": [BACKEND, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q",
            "tests/test_membership_admin.py", "tests/test_membership_tier.py", "tests/test_auto_site_text.py",
            "tests/test_ai_quota_reset.py", "tests/test_member_web_20261112.py",
            "tests/test_plus_api.py::test_self_service_and_real_limits_match_current_tier",
            "--basetemp=" + str(TEMP / "pytest"), "--junitxml=" + str(OUTPUT / "backend.xml")],
        "browser": ["node", "--test", "tests/browser/membership_admin.cjs"],
        "static": [BACKEND, "-B", "scripts/check_membership_admin_syntax.py"],
    }
    files = set(subprocess.check_output(["git", "diff", "--name-only"], cwd=ROOT, text=True).splitlines())
    files.update(subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT, text=True).splitlines())
    record = dict(stage=stage, status="running_or_interrupted", started=datetime.now(timezone.utc).isoformat(),
        baseline=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        command=commands[stage], log=str(OUTPUT / (stage + ".log")),
        hashes={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sorted(files) if (ROOT / name).is_file()})
    def save():
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    save()
    code = 1
    try:
        with Path(record["log"]).open("w", encoding="utf-8") as log:
            code = subprocess.run(commands[stage], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
        record.update(status="passed" if code == 0 else "failed", exit_code=code)
    except BaseException as exc:
        record.update(status="interrupted", error=type(exc).__name__)
        raise
    finally:
        record["finished"] = datetime.now(timezone.utc).isoformat()
        save()
        print(Path(record["log"]).read_text("utf-8")[-16000:])
    return code


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    raise SystemExit(main())
