"""Bounded passive monitoring; reuses the site's SMTP channel, never imports app.

Read only one access-log stream. Preserve byte cursor; count skipped bytes.
No service restarts, Cloudflare changes, or historical database scans here.
"""
from collections import Counter
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import smtplib
import ssl
import subprocess
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from reader_security import sensitive_path

STATE = Path("/var/lib/marx-search-security/ops.json")
ACCESS = Path("/var/log/caddy/access.log")


def properties(unit, *names):
    result = subprocess.run(["systemctl", "show", unit, *["--property=" + n for n in names]],
                            capture_output=True, text=True, timeout=5, check=True)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def access_counts(state, path=ACCESS, cap=512 * 1024):
    counts = Counter()
    if not path.exists():
        return counts
    stat = path.stat()
    offset = state.get("offset", 0) if state.get("inode") == stat.st_ino else 0
    if offset > stat.st_size:
        offset = 0
    if stat.st_size - offset > cap:
        counts["log_bytes_skipped"] = stat.st_size - offset - cap
        offset = stat.st_size - cap
    with path.open("rb") as stream:
        stream.seek(offset)
        if counts["log_bytes_skipped"]:
            stream.readline()  # discard partial leading record
        for _ in range(4000):
            start = stream.tell()
            line = stream.readline(65537)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) == 65537:
                    counts["oversize_log_lines"] += 1
                    continue  # advance; a long hostile URI must not pin cursor
                stream.seek(start)
                break
            try:
                event = json.loads(line)
                if not str(event.get("logger", "")).startswith("http.log.access"):
                    continue
                uri = (event.get("request") or {}).get("uri", "")
                if sensitive_path(urllib.parse.unquote(urllib.parse.urlsplit(uri).path)):
                    counts["edge_sensitive_scan"] += 1
                headers = {k.lower(): v for k, v in (event.get("resp_headers") or {}).items()}
                if int(event.get("status", 0)) >= 500 and "x-request-id" not in headers:
                    counts["proxy_5xx"] += 1
            except (ValueError, TypeError):
                counts["log_parse_errors"] += 1
        state.update(inode=stat.st_ino, offset=stream.tell())
    return counts


def send_alert(body):
    target = os.environ.get("SECURITY_ALERT_EMAIL") or "popyunlong@163.com"
    host = os.environ.get("SMTP_HOST", "")
    sender = os.environ.get("SMTP_FROM_EMAIL", "")
    if not host or not sender:
        raise RuntimeError("security alert channel unavailable")
    port = int(os.environ.get("SMTP_PORT", "587"))
    message = EmailMessage()
    message["From"], message["To"] = sender, target
    message["Subject"] = "网站防御运维告警汇总"
    message.set_content(body)
    client = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    kwargs = {"timeout": 15}
    if port == 465:
        kwargs["context"] = ssl.create_default_context()
    with client(host, port, **kwargs) as smtp:
        if port != 465 and os.environ.get("SMTP_USE_TLS", "1").lower() not in {"0", "false", "no", "off"}:
            smtp.starttls(context=ssl.create_default_context())
        if os.environ.get("SMTP_USERNAME"):
            smtp.login(os.environ["SMTP_USERNAME"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(message)


def run():
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    now = time.time()
    counts = Counter(state.get("counts", {}))
    counts.update(access_counts(state))
    issues = []
    for unit in ("marx-search-backup.service", "marx-search-cfip-sync.service", "marx-search-watchdog.service"):
        values = properties(unit, "Result", "ActiveState")
        if values.get("Result") not in {"success", ""} and values.get("ActiveState") != "activating":
            issues.append(unit + ": " + values.get("Result", "unknown"))
    timer = properties("marx-search-watchdog.timer", "ActiveState", "LastTriggerUSecMonotonic")
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    last = int(timer.get("LastTriggerUSecMonotonic") or 0) / 1000000
    if timer.get("ActiveState") != "active" or (uptime > 900 and uptime - last > 300):
        issues.append("watchdog timer inactive or stale > 300s")
    # Preserve aggregations until the five-minute window ends.
    if now - state.get("window", now) >= 300:
        if counts["edge_sensitive_scan"] >= 10 or counts["proxy_5xx"] >= 5 or counts["log_bytes_skipped"]:
            issues.append(json.dumps(dict(counts), sort_keys=True))
        state["counts"], state["window"] = {}, now
    else:
        state["counts"] = dict(counts)
        state.setdefault("window", now)
    signature = hashlib.sha256("\n".join(issues).encode()).hexdigest()
    if issues and (signature != state.get("alert_signature") or now - state.get("last_alert", 0) >= 3600):
        send_alert("\n".join(issues))
        state.update(last_alert=now, alert_signature=signature)
    if not issues:
        state.pop("alert_signature", None)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    temporary.replace(STATE)


if __name__ == "__main__":
    run()
