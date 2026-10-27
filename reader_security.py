"""Small, bounded reader risk state and a separate asynchronous security journal.

No request scans historical audit rows. UA correlation is observational only.
Identity comes from the application's authenticated user / signed visitor ID.
"""
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time

LOGGER = logging.getLogger(__name__)

# A registry shared by audit, ban and behavioral guards. Business permission
# checks remain in each route, before any file access or renderer invocation.
PROTECTED_ENDPOINTS = frozenset({
    "reader", "layout_read", "library", "dictionary", "dictionary_entry_page", "pdf_viewer",
    "serve_pdf", "page_image", "reader_cover", "api_dictionary_suggest",
    "api_library_toc_suggest", "api_library_volume_toc", "api_pdf_page_context",
    "api_reader_find", "wenku_home", "wenku_reader", "wenku_raw", "liushi_home",
    "wenku_raw_versioned", "liushi_reader", "liushi_raw", "liushi_raw_versioned",
    "mylib_reader", "api_mylib_page_text", "api_mylib_page_image",
    "api_mylib_book_search", "journal_alerts_article", "journal_alerts_pdf", "journal_alerts_asset",
    "api_search_export_download", "citation_assistant_download",
})


def ban_active(record, now=None):
    """Legacy/malformed/manual records fail closed; never delete old evidence."""
    if not isinstance(record, dict) or not record.get("expires_at"):
        return True
    try:
        expiry = datetime.fromisoformat(str(record["expires_at"]).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry.timestamp() > (time.time() if now is None else now)
    except (ValueError, TypeError, OverflowError):
        return True


def sensitive_path(path):
    """Path components only; query strings are never inspected here."""
    parts = path.lower().split("/")
    return any(p in {".env", ".git", "service_account.json", "gc-service.json",
                     "gcp-service.json"} for p in parts)


@dataclass
class Window:
    started: float
    count: int = 0
    minute: int = -1
    minute_count: int = 0
    books: set = field(default_factory=set)
    endpoints: set = field(default_factory=set)
    ips: set = field(default_factory=set)
    ban_until: float = 0


class ReaderSecurity:
    def __init__(self, path, secret, *, capacity=20000, queue_size=4096, clock=time.time):
        self.path = Path(path)
        self.secret = str(secret).encode()
        self.clock = clock
        self.capacity = capacity
        self.states = OrderedDict()
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=queue_size)
        self.metrics = Counter()
        self.worker = None
        self.alert = None
        self.last_alert = 0
        self.last_prune = 0
        self.alert_counts = Counter()
        self.event_day = -1
        self.event_rows_today = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS security_meta(key TEXT PRIMARY KEY, value REAL);
                CREATE TABLE IF NOT EXISTS reader_verifications(
                    session_hash TEXT PRIMARY KEY, expires REAL NOT NULL, epoch TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS security_events(
                    minute INTEGER, actor TEXT, endpoint TEXT, rule TEXT, outcome TEXT,
                    status INTEGER, release TEXT, count INTEGER, first_request TEXT,
                    last_request TEXT, PRIMARY KEY(minute,actor,endpoint,rule,outcome,status,release));
                CREATE TABLE IF NOT EXISTS security_daily(
                    day INTEGER, endpoint TEXT, rule TEXT, outcome TEXT, status INTEGER,
                    count INTEGER, PRIMARY KEY(day,endpoint,rule,outcome,status));
            """)
            conn.execute("INSERT OR IGNORE INTO security_meta VALUES('observed_since',?)", (clock(),))
            self.observed_since = conn.execute("SELECT value FROM security_meta WHERE key='observed_since'").fetchone()[0]
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=2)
        conn.execute("PRAGMA busy_timeout=2000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def digest(self, value, domain="session"):
        return hmac.new(self.secret, (domain + ":" + value).encode(), hashlib.sha256).hexdigest()

    def mode(self, config):
        # Observation is an explicit deployment gate, not a startup timer.
        requested = config.get("mode", "observe")
        approved = bool(config.get("observation_reviewed"))
        if requested == "enforce" and approved and self.clock() - self.observed_since >= 86400:
            return "enforce"
        return "observe"

    def assess(self, actor, ip, endpoint, book):
        now = self.clock()
        with self.lock:
            state = self.states.pop(actor, None)
            if state is None or now - state.started >= 900:
                state = Window(now)
            state.count += 1
            minute = int(now // 60)
            if state.minute != minute:
                state.minute, state.minute_count = minute, 0
            state.minute_count += 1
            for values, value in ((state.books, book), (state.endpoints, endpoint), (state.ips, ip)):
                if value and len(values) < 64:
                    values.add(value)
            self.states[actor] = state
            if len(self.states) > self.capacity:
                self.states.popitem(last=False)
                self.metrics["state_evictions"] += 1
            # The account/session owns this decision. Shared IP alone never
            # merges authenticated readers or signed visitor sessions.
            if state.ban_until > now:
                return "temporary_ban", max(1, int(state.ban_until - now))
            if state.minute_count > 600 and len(state.books) >= 20:
                state.ban_until = now + 300
                return "temporary_ban", 300
            if state.minute_count > 240:
                return "rate", 60
            if state.count >= 120 and len(state.books) >= 30 and len(state.endpoints) >= 2:
                return "volume_traversal", 60
            if state.count >= 160 and len(state.ips) >= 8 and len(state.books) >= 12:
                return "rotating_ip", 60
            return "allow", 0

    def issue(self, session_id, epoch):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO reader_verifications VALUES(?,?,?)",
                         (self.digest(session_id), self.clock() + 1800, str(epoch)))

    def verified(self, session_id, epoch):
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT expires,epoch FROM reader_verifications WHERE session_hash=?",
                                   (self.digest(session_id),)).fetchone()
            return bool(row and row[0] > self.clock() and row[1] == str(epoch))
        except sqlite3.Error:
            return False

    def revoke(self, session_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM reader_verifications WHERE session_hash=?", (self.digest(session_id),))

    def record(self, *, actor="", endpoint="", rule="", outcome="", status=0, release="", request_id="", start=True):
        now = self.clock()
        item = (int(now // 60), self.digest(actor, str(int(now // 86400)))[:24],
                endpoint[:80], rule[:80], outcome[:32], int(status), str(release)[:100], request_id[:64])
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            with self.lock:
                self.metrics["queue_dropped"] += 1
        if start:
            self.start()

    def start(self):
        with self.lock:
            if self.worker is None:
                self.worker = threading.Thread(target=self._loop, name="security-journal", daemon=True)
                self.worker.start()

    def flush(self):
        rows = {}
        for _ in range(2048):
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            key, request_id = item[:7], item[7]
            if key not in rows:
                rows[key] = [0, request_id, request_id]
            rows[key][0] += 1
            rows[key][2] = request_id
            self.queue.task_done()
        if not rows:
            return
        try:
            with self.connect() as conn:
                today = int(self.clock() // 86400)
                if self.event_day != today:
                    self.event_rows_today = conn.execute("SELECT count(*) FROM security_events WHERE minute>=?",
                                                        (today * 1440,)).fetchone()[0]
                    self.event_day = today
                for key, (count, first, last) in rows.items():
                    original_key = key
                    if self.event_rows_today >= 5000:
                        # Floods with changing identities cannot grow storage
                        # without bound. Keep counts, record loss of detail.
                        key = (today * 1440, "aggregated", "aggregated", "event_capacity", key[4], 0, "aggregated")
                        self.metrics["events_coalesced"] += count
                    cursor = conn.execute("INSERT OR IGNORE INTO security_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                                          (*key, 0, first, last))
                    self.event_rows_today += cursor.rowcount
                    conn.execute("""INSERT INTO security_events VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT DO UPDATE SET count=count+excluded.count,last_request=excluded.last_request""",
                                 (*key, count, first, last))
                    minute, _actor, endpoint, rule, outcome, status, _release = original_key
                    conn.execute("""INSERT INTO security_daily VALUES(?,?,?,?,?,?)
                        ON CONFLICT DO UPDATE SET count=count+excluded.count""",
                                 (minute // 1440, endpoint, rule, outcome, status, count))
                    if outcome in {"denied", "server_error"}:
                        self.alert_counts[rule or outcome] += count
                now = self.clock()
                if now - self.last_prune >= 3600:
                    conn.execute("DELETE FROM security_events WHERE minute < ?", (int(now // 60) - 180 * 1440,))
                    conn.execute("DELETE FROM security_daily WHERE day < ?", (int(now // 86400) - 400,))
                    conn.execute("DELETE FROM reader_verifications WHERE expires < ?", (now,))
                    self.last_prune = now
            with self.lock:
                self.metrics["events_written"] += sum(v[0] for v in rows.values())
        except sqlite3.Error:
            with self.lock:
                self.metrics["write_dropped"] += sum(v[0] for v in rows.values())
            LOGGER.exception("security journal write failed")

    def _loop(self):
        while True:
            time.sleep(2)
            try:
                self.flush()
                now = self.clock()
                if now - self.last_alert >= 300:
                    counts = dict(self.alert_counts)
                    drops = self.metrics["queue_dropped"] + self.metrics["write_dropped"]
                    if self.alert and (sum(counts.values()) >= 100 or counts.get("sensitive_path", 0) >= 10
                                       or counts.get("server_error", 0) >= 5 or drops > self.metrics["alerted_drops"]):
                        self.last_alert = now
                        self.alert(json.dumps({"counts": counts, "dropped_total": drops}, ensure_ascii=False))
                        self.metrics["alerted_drops"] = drops
                    self.alert_counts.clear()
            except Exception:
                LOGGER.exception("security journal worker failed")

    def status(self):
        with self.lock:
            return {**self.metrics, "queue_depth": self.queue.qsize(), "states": len(self.states),
                    "observed_since": self.observed_since, "worker_alive": bool(self.worker and self.worker.is_alive())}
