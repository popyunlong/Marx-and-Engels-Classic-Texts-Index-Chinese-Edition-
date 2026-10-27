import json
import os
from pathlib import Path
import re
import subprocess

import pytest
from deploy.security_caddy import patch, BEGIN
from scripts.security_ops_watch import access_counts

ROOT = Path(__file__).resolve().parents[1]


def test_caddy_incremental_patch_and_exact_rollback():
    source = (ROOT / "docs/ops/security-defense-20261027/Caddyfile.baseline").read_text(encoding="utf-8")
    changed = patch(source)
    assert patch(changed) == changed
    assert patch(changed, remove=True) == source
    assert changed.count("127.0.0.1:8011") == source.count("127.0.0.1:8011")
    assert "roll_size 25MiB" in changed and "roll_keep 8" in changed
    assert "not header_regexp User-Agent" not in changed
    pattern = re.search(r"path_regexp reader_sensitive (.+)", changed).group(1)
    for path in ("/.env", "/.git/config", "/backend/.env", "/gcp-service.json"):
        assert re.search(pattern, path)
    for path in ("/.well-known/acme-challenge/x", "/book.git", "/images.env.jpg", "/filename.env"):
        assert not re.search(pattern, path)
    with pytest.raises(ValueError): patch("other.example {\n log\n}\n")


def test_access_log_cursor_deduplication_and_bounded_skip(tmp_path):
    path = tmp_path / "access.log"
    events = [
        {"logger": "http.log.access.log0", "request": {"uri": "/.env"}, "status": 403},
        {"logger": "http.log.error", "request": {"uri": "/.env"}, "status": 403},
        {"logger": "http.log.access.log0", "request": {"uri": "/ok?next=/.env"}, "status": 502},
        {"logger": "http.log.access.log0", "request": {"uri": "/ok"}, "status": 500, "resp_headers": {"X-Request-ID": ["app-id"]}},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    state = {}
    counts = access_counts(state, path)
    assert counts["edge_sensitive_scan"] == 1 and counts["proxy_5xx"] == 1
    assert not access_counts(state, path)
    state.clear()
    assert access_counts(state, path, cap=100)["log_bytes_skipped"] > 0


@pytest.mark.parametrize("lock_ok,changed_pid,recovered", [(False, False, False), (True, True, False), (True, False, True)])
def test_watchdog_observes_lock_busy_and_rechecks_before_restart(tmp_path, lock_ok, changed_pid, recovered):
    script = (ROOT / "deploy/health_watchdog.sh").read_text(encoding="utf-8")
    for src, dst in (("/run/lock/marx-search-release.lock", "lock"), ("/run/marx-search-watchdog.last-restart", "stamp"), ("/proc/uptime", "uptime")):
        script = script.replace(src, (tmp_path / dst).as_posix())
    (tmp_path / "uptime").write_text("10000.0 10000.0\n")
    counter = (tmp_path / "pid-counter").as_posix()
    prefix = f'''
logger() {{ :; }}
sleep() {{ :; }}
readlink() {{ echo release-one; }}
flock() {{ return {0 if lock_ok else 1}; }}
probe=0
curl() {{ probe=$((probe+1)); echo PROBE; [ "$probe" -eq 4 ] && [ "{int(recovered)}" -eq 1 ]; }}
systemctl() {{
 case "$1" in
 is-active) return 0 ;;
 show) if [ "$4" = MainPID ]; then
   if [ -f '{counter}' ] && [ '{int(changed_pid)}' = 1 ]; then echo 222; else echo 111; touch '{counter}'; fi
   else echo 1000000; fi ;;
 restart) echo FORBIDDEN_RESTART ;;
 esac
}}
'''
    result = subprocess.run([os.environ["MARX_TEST_BASH"], "--noprofile", "--norc", "-c", prefix + script],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("PROBE") == (4 if recovered else 3)
    assert "FORBIDDEN_RESTART" not in result.stdout


def test_backup_and_sync_keep_recovery_contract():
    backup = (ROOT / "deploy/backup.sh").read_text(encoding="utf-8")
    assert "rm -rf" not in backup and "tail -n" not in backup
    assert "security.sqlite3" in backup and "flock -w 5 9" in backup
    sync = (ROOT / "deploy/update_cf_ips.sh").read_text(encoding="utf-8")
    assert "not header_regexp" not in sync
    assert sync.index("caddy validate") < sync.index('install -m 0644')
    assert 'cp -a "$work/previous" "$SNIPPET"' in sync
    wrapper = (ROOT / "deploy/server_helpers/update_cf_ips.sh").read_text(encoding="utf-8")
    assert "curl" not in wrapper and "/current/app/deploy/update_cf_ips.sh" in wrapper
