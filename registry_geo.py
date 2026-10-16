"""Read-only per-user geo resolution, also usable against a SQLite read-only snapshot."""
from collections import Counter
from functools import lru_cache
import heapq
import json

import geoip


def resolve_user_geo_counts(conn, classify=None):
    classify = lru_cache(maxsize=16384)(classify or geoip.classify_ip)
    counts = Counter()
    ai_has_ip = any(row[1] == "client_ip" for row in conn.execute("PRAGMA table_info(ai_usage)"))
    for uid, register_ip, last_ip in conn.execute(
        "SELECT id, register_ip, last_ip FROM users WHERE role != 'system' ORDER BY id"
    ):
        result = classify(last_ip or "")
        if result["scope"] == "unknown":
            cursors = []
            # Equal timestamps: reader first, then AI; larger record ID first.
            for source, table in enumerate(("reader_access_events", "ai_usage")):
                if table == "ai_usage" and not ai_has_ip:
                    continue
                cursor = conn.execute(
                    f"SELECT created_at, id, client_ip FROM {table} "
                    "WHERE user_id=? AND TRIM(COALESCE(client_ip,''))<>'' "
                    "ORDER BY created_at DESC, id DESC", (uid,)
                )
                cursors.append((source, cursor))
            def events(source, cursor):
                for stamp, event_id, ip in cursor:
                    yield (stamp, -source, event_id, ip)
            try:
                for _stamp, _source, _event_id, ip in heapq.merge(
                    *(events(source, cursor) for source, cursor in cursors), reverse=True
                ):
                    candidate = classify(ip)
                    if candidate["scope"] != "unknown":
                        result = candidate
                        break
                    if result.get("reason") == "no_ip":
                        result = candidate
            finally:
                for _, cursor in cursors:
                    cursor.close()
            if result["scope"] == "unknown":
                candidate = classify(register_ip or "")
                if candidate["scope"] != "unknown" or result.get("reason") == "no_ip":
                    result = candidate
        counts[json.dumps(result, sort_keys=True, ensure_ascii=False)] += 1
    return [(json.loads(key), count) for key, count in sorted(counts.items())]
