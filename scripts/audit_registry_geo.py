#!/usr/bin/env python3
"""Compare release geo data and historical fallback on one read-only SQLite snapshot.

Run from the candidate tree. Never calls membership initialization or writes user data.
Output contains aggregate categories only, never IPs, user IDs or account details.
"""
from __future__ import annotations
import argparse
from collections import Counter
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geoip
from registry_geo import resolve_user_geo_counts


def audit(conn, baseline):
    old = lru_cache(maxsize=16384)(baseline)
    before, data_only, transitions = Counter(), Counter(), Counter()
    for ip, count in conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(last_ip),''),TRIM(register_ip),''), COUNT(*) "
        "FROM users WHERE role!='system' GROUP BY 1"
    ):
        a, b = old(ip), geoip.classify_ip(ip)
        before[a["scope"]] += count
        data_only[b["scope"]] += count
        ak = (a["scope"], a.get("province") or a.get("country") or "unknown")
        bk = (b["scope"], b.get("province") or b.get("country") or "unknown")
        if ak != bk:
            transitions[ak + bk] += count
    started = time.monotonic()
    rows = resolve_user_geo_counts(conn)
    elapsed = time.monotonic() - started
    after, reasons = Counter(), Counter()
    for result, count in rows:
        after[result["scope"]] += count
        if result["scope"] == "unknown":
            reasons[result["reason"]] += count
    assert sum(before.values()) == sum(data_only.values()) == sum(after.values())
    assert sum(reasons.values()) == after["unknown"]
    return {"before":dict(before), "data_only":dict(data_only), "after":dict(after),
            "unknown_reasons":dict(reasons), "query_seconds":round(elapsed,3),
            "data_transitions":[{"from":list(k[:2]), "to":list(k[2:]), "count":v} for k,v in sorted(transitions.items())]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--baseline-app", type=Path, required=True)
    parser.add_argument("--baseline-data", type=Path, required=True)
    args = parser.parse_args()
    status = geoip.geoip_status()
    if not status["ready"] or not status["ready_v6"]:
        raise RuntimeError("Candidate dual-stack databases are not ready")
    spec = importlib.util.spec_from_file_location("baseline_geoip", args.baseline_app / "geoip.py")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    baseline._xdb_path = lambda family="v4": str(args.baseline_data / f"ip2region_{family}.xdb")
    conn = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        print(json.dumps(audit(conn, baseline.classify_ip), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
