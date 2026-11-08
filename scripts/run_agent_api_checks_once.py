"""Record first attempts before execution and reject all implicit reruns."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs/ops/AGENT_API_TEST_LEDGER_20261107.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["backend", "protocol", "main-integration", "static"])
    args = parser.parse_args()
    ledger = json.loads(LEDGER.read_text("utf-8"))
    if any(run["phase"] == args.phase for run in ledger["runs"]):
        raise SystemExit("Already attempted; inspect the ledger and obtain explicit rerun authorization.")
    output = Path(ledger["artifact_root"])
    output.mkdir(parents=True, exist_ok=True)
    temp = Path("D:/CodexData/temp/agent-api-20261107")
    temp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
               MARX_RUNTIME_LOG_DIR=str(output / "runtime-logs"), AGENT_API_TEST_OUTPUT=str(output / "protocol"),
               AGENT_API_BACKEND_PYTHON=ledger["backend_python"])
    commands = {
        "main-integration": [[ledger["backend_python"], "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests/test_agent_api_main_integration.py",
                              "--basetemp=" + str(temp / "main-integration"), "--junitxml=" + str(output / "main-integration.xml")]],
        "backend": [[ledger["backend_python"], "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests/test_agent_api.py", "tests/test_agent_api_edges.py",
                     "--basetemp=" + str(temp / "backend"), "--junitxml=" + str(output / "backend.xml")]],
        "protocol": [[ledger["mcp_python"], "tests/agent_api_protocol_once.py"]],
        "static": [["git", "diff", "--check"], ["D:/Git/bin/bash.exe", "-n", "deploy/agent_api_units.sh"],
                   ["D:/Git/bin/bash.exe", "-n", "deploy/promote_release.sh"], ["D:/Git/bin/bash.exe", "-n", "deploy/rollback_release.sh"],
                   ["D:/Git/bin/bash.exe", "-n", "deploy/release_traffic.sh"]],
    }[args.phase]
    source_files = list(ROOT.glob("agent_api*.py")) + [ROOT / p for p in ("app.py", "search.py", "feature_access.py", "templates/agent_api.html")]
    source_files += list((ROOT / "deploy").glob("agent_api*")) + list((ROOT / "tests").glob("*agent_api*"))
    source_files += [ROOT / p for p in ("deploy/promote_release.sh", "deploy/rollback_release.sh", "deploy/release_traffic.sh",
                                      "deploy/requirements-agent-api.lock", ".github/workflows/ci.yml", "templates/account.html",
                                      "templates/citation_agent_test.html")]
    record = {"phase": args.phase, "status": "running", "commands": commands, "started": datetime.now(timezone.utc).isoformat(),
              "source_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files if p.is_file()},
              "log": str(output / (args.phase + ".log"))}
    ledger["runs"].append(record)
    def save(): LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", "utf-8")
    save()
    code = 1
    try:
        with Path(record["log"]).open("w", encoding="utf-8") as log:
            for command in commands:
                code = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
                if code: break
        record.update(status="passed" if code == 0 else "failed", exit_code=code)
    except BaseException as exc:
        record.update(status="interrupted", error=type(exc).__name__)
        raise
    finally:
        record["finished"] = datetime.now(timezone.utc).isoformat()
        save()
        print(Path(record["log"]).read_text("utf-8")[-12000:])
    return code


if __name__ == "__main__": raise SystemExit(main())
