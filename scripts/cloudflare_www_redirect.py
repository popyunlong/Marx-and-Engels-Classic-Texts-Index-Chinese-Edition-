"""Prepare and narrowly manage the www -> apex Cloudflare Single Redirect.

Credentials are read only from CLOUDFLARE_API_TOKEN and CF_ZONE_ID. The default
command prints the intended rule without contacting Cloudflare. Mutations
require an explicit command, a fresh ruleset read, and a local backup.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


HOST = "www.mazhuzuojiansuo.com"
RULE_REF = "marx_www_to_apex_20261010"
PHASE = "http_request_dynamic_redirect"


def proposed_rule(status_code=302):
    if status_code not in (302, 301):
        raise ValueError("only 302 or 301 is supported")
    return {
        "ref": RULE_REF,
        "description": "Redirect GET and HEAD www requests to the canonical HTTPS hostname",
        "expression": f'(http.host eq "{HOST}" and http.request.method in {{"GET" "HEAD"}})',
        "action": "redirect",
        "action_parameters": {"from_value": {
            "target_url": {"expression": 'concat("https://mazhuzuojiansuo.com", http.request.uri.path)'},
            "status_code": status_code,
            "preserve_query_string": True,
        }},
    }


def api_request(method, path, token, payload=None):
    request = urllib.request.Request(
        "https://api.cloudflare.com/client/v4" + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET" and path.endswith("/entrypoint"):
            return None
        raise RuntimeError(f"Cloudflare API returned HTTP {exc.code}") from None
    if not body:
        return {}
    result = json.loads(body)
    if not result.get("success"):
        raise RuntimeError("Cloudflare API declined the request")
    return result["result"]


def entrypoint_path(zone_id):
    return f"/zones/{zone_id}/rulesets/phases/{PHASE}/entrypoint"


def write_backup(directory, ruleset):
    path = Path(directory).resolve() / "www-redirect-ruleset-before.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(ruleset, stream, ensure_ascii=False, indent=2)
    return path


def _same_rule(existing, proposed):
    return all(existing.get(key) == proposed[key] for key in
               ("ref", "expression", "action", "action_parameters"))


@contextmanager
def release_lock(ssh_target):
    """Hold the repository's production lock during a Cloudflare mutation."""
    remote = "flock -x -w 120 /run/lock/marx-search-release.lock sh -c 'printf LOCKED; cat >/dev/null'"
    process = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=10", ssh_target, remote],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        if process.stdout.read(6) != b"LOCKED":
            raise RuntimeError("unable to acquire the production release lock")
        yield
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def run_command(args, zone_id, token):
    entry = api_request("GET", entrypoint_path(zone_id), token)
    rules = list(entry.get("rules") or []) if entry else []
    own = [rule for rule in rules if rule.get("ref") == RULE_REF]
    if len(own) > 1:
        raise RuntimeError("multiple managed www rules found")
    conflicts = [rule for rule in rules if rule.get("ref") != RULE_REF and
                 HOST in str(rule.get("expression") or "") and rule.get("enabled", True)]
    if conflicts:
        raise RuntimeError("an existing enabled www redirect may take precedence")
    if args.command == "inspect":
        print(json.dumps({"ruleset_id": entry.get("id") if entry else None,
                          "managed_rule_id": own[0].get("id") if own else None,
                          "managed_status": own[0].get("action_parameters", {}).get("from_value", {}).get("status_code") if own else None,
                          "other_rule_count": len(rules) - len(own)}))
        return
    if not args.backup_dir:
        parser.error("--backup-dir is required for mutations")
    if args.command == "rollback":
        if not args.expected_rule_id or not own or own[0].get("id") != args.expected_rule_id:
            raise RuntimeError("rollback requires the current managed rule ID")
        if not (_same_rule(own[0], proposed_rule(302)) or
                _same_rule(own[0], proposed_rule(301))):
            raise RuntimeError("managed rule changed; inspect it before rollback")
        write_backup(args.backup_dir, entry)
        api_request("DELETE", f"/zones/{zone_id}/rulesets/{entry['id']}/rules/{args.expected_rule_id}", token)
        print(json.dumps({"deleted_rule_id": args.expected_rule_id}))
        return
    if args.command == "promote":
        if not own or not _same_rule(own[0], proposed_rule(302)):
            raise RuntimeError("only the exact managed 302 rule may be promoted")
        write_backup(args.backup_dir, entry)
        updated = api_request("PATCH", f"/zones/{zone_id}/rulesets/{entry['id']}/rules/{own[0]['id']}", token, proposed_rule(301))
        print(json.dumps({"rule_id": own[0]["id"], "status_code": 301}))
        return
    if own:
        if _same_rule(own[0], proposed_rule(302)):
            print(json.dumps({"rule_id": own[0].get("id"), "unchanged": True}))
            return
        raise RuntimeError("managed rule differs; inspect it before changing")
    write_backup(args.backup_dir, entry or {"phase": PHASE, "rules": []})
    if entry:
        created = api_request("POST", f"/zones/{zone_id}/rulesets/{entry['id']}/rules", token, proposed_rule(302))
    else:
        created = api_request("POST", f"/zones/{zone_id}/rulesets", token, {
            "name": "Redirect rules ruleset", "kind": "zone", "phase": PHASE,
            "rules": [proposed_rule(302)],
        })
    result_rules = created.get("rules") or []
    result_rule = next((row for row in result_rules if row.get("ref") == RULE_REF), None)
    print(json.dumps({"ruleset_id": entry.get("id") if entry else created.get("id"),
                      "rule_id": result_rule.get("id") if result_rule else None,
                      "status_code": 302}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "inspect", "apply", "promote", "rollback"))
    parser.add_argument("--backup-dir", help="required for mutations; keep on D: for local runs")
    parser.add_argument("--expected-rule-id", help="required for rollback")
    parser.add_argument("--ssh-target", default="marx-cloud", help="server holding the production release lock")
    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps(proposed_rule(), ensure_ascii=False, indent=2))
        return
    zone_id = os.environ.get("CF_ZONE_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not zone_id or not token or not all(c in "0123456789abcdefABCDEF" for c in zone_id):
        parser.error("CF_ZONE_ID and CLOUDFLARE_API_TOKEN must be set")
    if args.command in {"apply", "promote", "rollback"} and not args.backup_dir:
        parser.error("--backup-dir is required for mutations")
    lock = release_lock(args.ssh_target) if args.command in {"apply", "promote", "rollback"} else nullcontext()
    with lock:
        run_command(args, zone_id, token)


if __name__ == "__main__":
    main()
