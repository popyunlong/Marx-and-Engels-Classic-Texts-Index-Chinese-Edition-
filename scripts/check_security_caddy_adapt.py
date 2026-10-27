"""Read-only adapter acceptance against the installed Caddy; never reloads it."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.security_caddy import patch

baseline = ROOT / "docs/ops/security-defense-20261027/Caddyfile.baseline"
ssh = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=yes", "marx-cloud"]
actual = subprocess.check_output(ssh + ["sha256sum /etc/caddy/Caddyfile"], timeout=25).decode().split()[0]
if actual != hashlib.sha256(baseline.read_bytes()).hexdigest():
    raise SystemExit("live configuration changed; revise baseline before validation")
candidate = patch(baseline.read_text(encoding="utf-8"))
result = subprocess.run(ssh + ["caddy adapt --adapter caddyfile --config /dev/stdin"],
                        input=candidate.encode(), capture_output=True, timeout=25)
if result.returncode:
    print(result.stderr.decode(errors="replace"))
    raise SystemExit(result.returncode)
data = json.loads(result.stdout)
destination = Path("D:/CodexData/outputs/security-defense-20261027")
(destination / "Caddyfile.candidate").write_text(candidate, encoding="utf-8")
(destination / "caddy-adapt.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
print("Installed Caddy adapted the incremental candidate successfully. No validate/provision/reload or live config writes performed.")
