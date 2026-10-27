"""Record a check before execution; refuse a second run, including interruptions.

This is a session ledger, not permission to rerun a scenario under a new name.
The operator must review existing scopes before choosing an unused check ID.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs/ops/SECURITY_TEST_LEDGER_20261027.json"
OUTPUT = Path("D:/CodexData/outputs/security-defense-20261027")


def source_digest():
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        path = ROOT / name
        if not name or path == LEDGER or not path.is_file():
            continue
        digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("id")
    parser.add_argument("--scope", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("command required")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    if any(run["id"] == args.id for run in ledger["runs"]):
        raise SystemExit("REFUSED: check already recorded; explicit scoped rerun authorization required")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    log = OUTPUT / (args.id + ".log")
    entry = dict(id=args.id, scope=args.scope, command=command, started=datetime.now(timezone.utc).isoformat(),
                 source_sha256=source_digest(), status="running_or_interrupted", artifact=str(log))
    ledger["runs"].append(entry)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with log.open("w", encoding="utf-8") as output:
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, env=environment)
    entry.update(status="passed" if result.returncode == 0 else "failed", exit_code=result.returncode,
                 finished=datetime.now(timezone.utc).isoformat())
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(log.read_text(encoding="utf-8", errors="replace")[-18000:])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
