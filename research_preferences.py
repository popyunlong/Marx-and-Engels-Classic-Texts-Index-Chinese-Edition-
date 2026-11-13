"""Per-member mail selection, independent of public bulletin availability."""
import hashlib
import json

import research_updates as r
from research_catalog import research_source_catalog


def journal_id(name):
    return hashlib.sha256(str(name).strip().encode()).hexdigest()[:20]


def catalog():
    source = research_source_catalog()
    return [{"id": journal_id(item["name"]), "name": item["name"],
             "label": item.get("name_zh") or item["name"], "group": group}
            for group in ("zh", "en") for item in source[group]]


def init(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS research_journal_preferences (
        user_id INTEGER PRIMARY KEY, email_enabled INTEGER NOT NULL,
        mode TEXT NOT NULL, journal_ids TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS research_public_samples (
        id INTEGER PRIMARY KEY CHECK(id=1), snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL);
    """)


def get_preferences(uid, email=""):
    with r.connect() as c:
        row = c.execute("SELECT * FROM research_journal_preferences WHERE user_id=?", (uid,)).fetchone()
        optout = c.execute("SELECT 1 FROM research_mail_preferences WHERE user_id=? AND opted_out=1", (uid,)).fetchone()
    if row:
        return dict(email_enabled=bool(row["email_enabled"]) and not optout, mode=row["mode"],
                    journal_ids=json.loads(row["journal_ids"]), explicit=True)
    import journal_alerts as ja
    legacy_optout = any(s["status"] == "unsubscribed" for s in ja.list_subscriptions_for_user(uid))
    return dict(email_enabled=not (optout or legacy_optout), mode="all", journal_ids=[], explicit=False)


def save_preferences(uid, email, payload):
    enabled, mode, ids = payload.get("email_enabled"), payload.get("mode"), payload.get("journal_ids")
    allowed = {x["id"] for x in catalog()}
    if type(enabled) is not bool or mode not in {"all", "selected"} or not isinstance(ids, list):
        raise ValueError("订阅设置格式无效。")
    if len(ids) > len(allowed) or any(not isinstance(x, str) or x not in allowed for x in ids):
        raise ValueError("期刊目录已变化，请刷新后重选。")
    ids = sorted(set(ids))
    if mode == "selected" and not ids:
        enabled = False
    with r.connect(True) as c:
        c.execute("INSERT INTO research_journal_preferences VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                  "email_enabled=excluded.email_enabled,mode=excluded.mode,journal_ids=excluded.journal_ids,updated_at=excluded.updated_at",
                  (uid, int(enabled), mode, json.dumps(ids), r.now_text()))
        # An explicit opt-in supersedes an old unsubscribe, never an implicit page load.
        c.execute("UPDATE research_mail_preferences SET opted_out=? WHERE user_id=?", (int(not enabled), uid))
    return get_preferences(uid, email)


def filter_snapshot(snapshot, preferences):
    if not preferences["email_enabled"]:
        return {**snapshot, "articles": []}
    if preferences["mode"] == "all":
        return snapshot
    ids = set(preferences["journal_ids"])
    return {**snapshot, "articles": [a for a in snapshot.get("articles", []) if journal_id(a["journal"]) in ids]}


def prepare_sample():
    """Explicit one-time publication from reviewed published snapshots; never refresh on GET."""
    with r.connect(True) as c:
        if c.execute("SELECT 1 FROM research_public_samples WHERE id=1").fetchone():
            return False
        row = c.execute("SELECT snapshot FROM research_issues WHERE status='published' ORDER BY period_end DESC,id DESC LIMIT 1").fetchone()
        if not row:
            return False
        source = json.loads(row["snapshot"])
        articles = source.get("articles", [])
        selected = []
        for domestic in (True, False):
            candidates = [a for a in articles if (a.get("origin") == "domestic") == domestic]
            # Round-robin over journals gives a representative fixed selection.
            groups = {}
            for a in candidates:
                groups.setdefault(a["journal"], []).append(a)
            chosen = []
            while groups and len(chosen) < 6:
                for key in list(groups):
                    chosen.append(groups[key].pop(0))
                    if not groups[key]:
                        del groups[key]
                    if len(chosen) == 6:
                        break
            selected.extend(chosen)
        snapshot = {k: source[k] for k in ("id", "title", "period_start", "period_end") if k in source}
        snapshot.update(articles=selected, public_sample=True)
        c.execute("INSERT INTO research_public_samples VALUES(1,?,?)", (r.dumps(snapshot), r.now_text()))
        return True


def sample():
    with r.connect() as c:
        row = c.execute("SELECT snapshot FROM research_public_samples WHERE id=1").fetchone()
    return json.loads(row["snapshot"]) if row else None
