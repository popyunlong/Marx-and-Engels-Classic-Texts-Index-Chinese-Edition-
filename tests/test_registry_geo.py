import ast
from collections import Counter
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

from registry_geo import resolve_user_geo_counts

D = {"scope": "domestic", "province": "北京市"}
O = {"scope": "overseas", "country_code": "US", "country": "美国"}
U = {"scope": "unknown", "reason": "no_ip"}


def classifier(ip):
    return {"d": D, "o": O, "bad": {"scope": "unknown", "reason": "invalid_ip"}}.get(ip, U)


def database():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY, register_ip TEXT, last_ip TEXT, role TEXT);
        CREATE TABLE reader_access_events(id INTEGER PRIMARY KEY, user_id INTEGER, client_ip TEXT, created_at TEXT);
        CREATE TABLE ai_usage(id INTEGER PRIMARY KEY, user_id INTEGER, client_ip TEXT, created_at TEXT);
        CREATE INDEX idx_reader_access_user_created ON reader_access_events(user_id, created_at DESC, id DESC);
        CREATE INDEX idx_ai_usage_user_created ON ai_usage(user_id, created_at DESC, id DESC);
    """)
    return conn


def test_fallback_order_preserves_users_and_valid_overseas():
    c = database()
    c.executemany("INSERT INTO users VALUES (?,?,?,?)", [(1,"d","o","user"),(2,"d","bad","user"),(3,"d","","user"),(4,"d","bad","user"),(5,"","","user"),(6,"d","d","system"),(7,"","bad","user")])
    c.executemany("INSERT INTO reader_access_events VALUES (?,?,?,?)", [(1,1,"d","2026-10-15"),(2,2,"d","2026-10-14"),(3,3,"d","2026-10-15"),(4,3,"o","2026-10-15"),(5,7,"bad","2026-10-16")])
    c.executemany("INSERT INTO ai_usage VALUES (?,?,?,?)", [(1,2,"o","2026-10-15"),(2,3,"d","2026-10-15")])
    before = list(c.iterdump())
    queries = []
    c.set_trace_callback(queries.append)
    results = resolve_user_geo_counts(c, classifier)
    assert Counter({r["scope"]: sum(n for a,n in results if a["scope"]==r["scope"]) for r,_ in results}) == {"domestic":1,"overseas":3,"unknown":2}
    assert list(c.iterdump()) == before
    assert not any("WHERE user_id=1 " in q for q in queries)
    assert sum(n for _,n in results) == 6
    for table in ("reader_access_events", "ai_usage"):
        plan = c.execute(f"EXPLAIN QUERY PLAN SELECT created_at,id,client_ip FROM {table} WHERE user_id=2 AND TRIM(COALESCE(client_ip,''))<>'' ORDER BY created_at DESC,id DESC").fetchall()
        assert any("USING INDEX" in row[3] for row in plan)
    c.close()


def app_functions(*names):
    # Exercise real aggregation/cache functions without importing unrelated app startup services.
    tree = ast.parse(Path("app.py").read_text(encoding="utf-8"))
    return compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names], type_ignores=[]), "app.py", "exec")


def test_payload_conserves_total_and_does_not_expose_paths():
    ns = {"get_user_geo_counts": lambda: [(D,2),(O,3),(U,4)], "geoip": SimpleNamespace(PROVINCE_SHORT={"北京市":"北京"},geoip_ready=lambda:True,geoip_status=lambda:{"ready_v6":False,"xdb":"SECRET_PATH"})}
    exec(app_functions("_build_registry_geo_payload"), ns)
    p = ns["_build_registry_geo_payload"]()
    assert p["total_registered"] == p["domestic_total"]+p["overseas_total"]+p["unknown"] == 9
    assert p["unknown_reasons"] == {"no_ip":4}
    assert p["geoip_ready"] and not p["geoip_ready_v6"]
    assert "SECRET_PATH" not in str(p)


def test_minute_cache_avoids_repeat_history_reads():
    import threading
    calls=[]
    ns={"time":time,"_registry_geo_cache":{},"_REGISTRY_GEO_TTL_SECONDS":60,"_registry_geo_lock":threading.Lock(),"_build_registry_geo_payload":lambda:calls.append(1) or {"ok":True}}
    exec(app_functions("_get_registry_geo_payload"), ns)
    ns["_get_registry_geo_payload"]();ns["_get_registry_geo_payload"]()
    assert len(calls)==1
    ns["_registry_geo_cache"]["at"] -= 61
    ns["_get_registry_geo_payload"]()
    assert len(calls)==2


def test_startup_installs_history_indexes(monkeypatch, tmp_path):
    import membership
    monkeypatch.setattr(membership, "APPDATA_DIR", tmp_path)
    monkeypatch.setattr(membership, "DB_PATH", tmp_path / "membership.sqlite3")
    monkeypatch.setattr(membership, "_WAL_ENABLED", False)
    membership.init_membership_db()
    membership.init_membership_db()  # idempotent on existing schemas
    with sqlite3.connect(membership.DB_PATH) as conn:
        for table in ("reader_access_events", "ai_usage"):
            plan = conn.execute(f"EXPLAIN QUERY PLAN SELECT created_at,id,client_ip FROM {table} WHERE user_id=2 AND TRIM(COALESCE(client_ip,''))<>'' ORDER BY created_at DESC,id DESC").fetchall()
            assert any("USING INDEX idx_" + ("reader_access" if table == "reader_access_events" else table) + "_user_created" in row[3] for row in plan)


def test_concurrent_cache_misses_build_once():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    calls=[]
    def build():
        calls.append(1)
        time.sleep(0.03)
        return {"ok":True}
    ns={"time":time,"_registry_geo_cache":{},"_REGISTRY_GEO_TTL_SECONDS":60,"_registry_geo_lock":threading.Lock(),"_build_registry_geo_payload":build}
    exec(app_functions("_get_registry_geo_payload"), ns)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(r == {"ok":True} for r in pool.map(lambda _:ns["_get_registry_geo_payload"](),range(8)))
    assert len(calls)==1


def test_unknown_reasons_and_missing_legacy_ai_ip_column():
    c=database()
    c.execute("DROP TABLE ai_usage")
    c.execute("CREATE TABLE ai_usage(id INTEGER PRIMARY KEY, user_id INTEGER, created_at TEXT)")
    c.execute("INSERT INTO users VALUES(1,'','','user')")
    c.execute("INSERT INTO reader_access_events VALUES(1,1,'bad','2026-10-15')")
    assert resolve_user_geo_counts(c,classifier)==[({"scope":"unknown","reason":"invalid_ip"},1)]
    c.close()


def test_audit_conserves_counts_without_writes(monkeypatch):
    from scripts import audit_registry_geo
    c=database()
    c.execute("INSERT INTO users VALUES(1,'d','bad','user')")
    monkeypatch.setattr(audit_registry_geo.geoip,"classify_ip",classifier)
    before=list(c.iterdump())
    c.execute("PRAGMA query_only=ON")
    report=audit_registry_geo.audit(c,classifier)
    assert report["before"] == {"unknown":1}
    assert report["after"] == {"domestic":1}
    assert list(c.iterdump())==before
    c.close()
