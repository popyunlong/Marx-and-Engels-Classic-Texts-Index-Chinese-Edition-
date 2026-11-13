"""Small shared runtime store and nonblocking, process-safe API admission."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from contextvars import ContextVar
QUERY_DEADLINE = ContextVar("api_query_deadline", default=None)


class APIError(Exception):
    def __init__(self, code, message, status=400, retry_after=0, *, retryable=None):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after
        self.retryable = bool(retry_after) if retryable is None else bool(retryable)

    def payload(self):
        return {"ok": False, "error": {"code": self.code, "message": str(self),
                "retry_after_seconds": self.retry_after, "retryable": self.retryable}}


def setting(name, default):
    return max(1, int(os.environ.get("MARX_AGENT_API_" + name, default)))


def scheduling(priority=0):
    return {"heavy_concurrency": setting("CONCURRENCY", 2), "light_concurrency": setting("LIGHT_CONCURRENCY", 2),
            "near_text_concurrency": 1, "user_data_concurrency": setting("USER_CONCURRENCY", 1),
            "user_control_concurrency": 1, "control_concurrency": setting("CONTROL_CONCURRENCY", 2), "queue": True,
            "queue_capacity": setting("QUEUE_CAPACITY", 8), "queue_wait_seconds": setting("QUEUE_WAIT_SECONDS", 5),
            "priority": "highest" if priority == 2 else "normal"}


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "agent_api.sqlite3"
        self._ready = False
        self._mutex = threading.Lock()

    def initialize(self):
        with self._mutex:
            if self._ready:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=2) as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS api_waiters (
                      id TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, lane TEXT NOT NULL,
                      priority INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS api_cancellations (
                      user_id INTEGER NOT NULL, request_id TEXT NOT NULL, at REAL NOT NULL,
                      PRIMARY KEY(user_id,request_id));
                    CREATE TABLE IF NOT EXISTS api_keys (
                      id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                      digest TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
                      created REAL NOT NULL, revoked REAL, last_used REAL);
                    CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys(user_id,revoked);
                    CREATE TABLE IF NOT EXISTS api_calls (user_id INTEGER NOT NULL, at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS api_calls_user ON api_calls(user_id,at);
                    CREATE TABLE IF NOT EXISTS api_evidence (
                      id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, version TEXT NOT NULL,
                      ref TEXT NOT NULL, at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS api_evidence_age ON api_evidence(at);
                    CREATE INDEX IF NOT EXISTS api_evidence_user ON api_evidence(user_id,at);
                ''')
            self._ready = True

    @contextmanager
    def connect(self, write=False):
        self.initialize()
        db = sqlite3.connect(self.path, timeout=0.25)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.OperationalError:
            db.rollback()
            raise APIError("store_busy", "接口存储暂忙，请稍后重试。", 503, 2) from None
        finally:
            db.close()

    def keys(self, uid):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,name,prefix,created,last_used FROM api_keys WHERE user_id=? AND revoked IS NULL ORDER BY created", (uid,))]

    def create_key(self, uid, name, replace=None):
        name = str(name or "").strip()
        if not 1 <= len(name) <= 60:
            raise APIError("invalid_name", "请输入 1–60 字的 Key 名称。")
        token = "marx_" + secrets.token_urlsafe(32)
        with self.connect(True) as db:
            if replace:
                changed = db.execute("UPDATE api_keys SET revoked=? WHERE id=? AND user_id=? AND revoked IS NULL",
                                     (time.time(), replace, uid)).rowcount
                if not changed:
                    raise APIError("key_not_found", "Key 不存在。", 404)
            if db.execute("SELECT COUNT(*) FROM api_keys WHERE user_id=? AND revoked IS NULL", (uid,)).fetchone()[0] >= 3:
                raise APIError("key_limit", "每个账号最多保留三个有效 Key。", 409)
            db.execute("INSERT INTO api_keys(id,user_id,name,digest,prefix,created) VALUES(?,?,?,?,?,?)",
                       (secrets.token_hex(12), uid, name, hashlib.sha256(token.encode()).hexdigest(), token[:12], time.time()))
        return token

    def revoke(self, uid, key_id):
        with self.connect(True) as db:
            db.execute("UPDATE api_keys SET revoked=? WHERE id=? AND user_id=? AND revoked IS NULL", (time.time(), key_id, uid))

    def authenticate(self, header):
        scheme, _, token = str(header or "").partition(" ")
        if scheme.lower() != "bearer" or not token.startswith("marx_") or len(token) > 128:
            raise APIError("unauthorized", "请提供有效的个人 API Key。", 401)
        with self.connect() as db:
            row = db.execute("SELECT id,user_id FROM api_keys WHERE digest=? AND revoked IS NULL",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise APIError("unauthorized", "Key 无效或已撤销。", 401)
        return dict(row)

    def rate(self, uid, *, consume=False, key_id=None):
        now, limit = time.time(), setting("PER_MINUTE", 30)
        with self.connect(consume) as db:
            if consume:
                db.execute("DELETE FROM api_calls WHERE at<=?", (now - 60,))
            count, oldest = db.execute("SELECT COUNT(*),MIN(at) FROM api_calls WHERE user_id=? AND at>?", (uid, now - 60)).fetchone()
            retry = max(1, int(oldest + 61 - now)) if oldest else 0
            if consume and count >= limit:
                raise APIError("rate_limited", "本分钟查询次数已达上限。", 429, retry)
            if consume:
                db.execute("INSERT INTO api_calls VALUES(?,?)", (uid, now))
                db.execute("UPDATE api_keys SET last_used=? WHERE id=?", (now, key_id))
                count += 1
        return {"per_minute": limit, "remaining": max(0, limit - count), "reset_after_seconds": retry}

    def put_evidence(self, uid, version, refs):
        now = time.time()
        ids = []
        with self.connect(True) as db:
            db.execute("DELETE FROM api_evidence WHERE at<?", (now - 86400,))
            for ref in refs:
                encoded = json.dumps(ref, ensure_ascii=False, sort_keys=True)
                evidence_id = hashlib.sha256(f"{uid}:{version}:{encoded}".encode()).hexdigest()
                db.execute("INSERT OR REPLACE INTO api_evidence VALUES(?,?,?,?,?)", (evidence_id, uid, version, encoded, now))
                ids.append(evidence_id)
            # Bounded references, never paper/query text or model-generated claims.
            db.execute("DELETE FROM api_evidence WHERE user_id=? AND id NOT IN (SELECT id FROM api_evidence WHERE user_id=? ORDER BY at DESC LIMIT 1000)", (uid, uid))
            db.execute("DELETE FROM api_evidence WHERE id NOT IN (SELECT id FROM api_evidence ORDER BY at DESC LIMIT 50000)")
        return ids

    def evidence(self, uid, evidence_id, version):
        with self.connect() as db:
            row = db.execute("SELECT version,ref FROM api_evidence WHERE id=? AND user_id=? AND at>?", (evidence_id, uid, time.time() - 86400)).fetchone()
        if not row:
            raise APIError("evidence_expired", "证据引用不存在或已过期，请重新查询。", 404)
        if row["version"] != version:
            raise APIError("corpus_changed", "语料版本已变化，请重新查询。", 409)
        return json.loads(row["ref"])

    def cancel_waiting(self, uid, request_id):
        with self.connect(True) as db:
            db.execute("DELETE FROM api_cancellations WHERE at<?", (time.time()-60,))
            db.execute("INSERT OR REPLACE INTO api_cancellations VALUES(?,?,?)", (uid, request_id, time.time()))
            db.execute("DELETE FROM api_cancellations WHERE user_id=? AND request_id NOT IN "
                       "(SELECT request_id FROM api_cancellations WHERE user_id=? ORDER BY at DESC LIMIT 32)", (uid, uid))

    def waiting_cancelled(self, uid, request_id):
        if not request_id:
            return False
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM api_cancellations WHERE user_id=? AND request_id=? AND at>?",
                                   (uid, request_id, time.time()-60)).fetchone())

    @contextmanager
    def admission(self, uid, *, lane="heavy", near_text=False, control=False, priority=0, cancelled=lambda: False, request_id=""):
        if control:
            with self._admission_nowait(uid, lane=lane, near_text=near_text, control=True):
                yield
            return
        from contextlib import ExitStack
        ticket = secrets.token_hex(16)
        wait = setting("QUEUE_WAIT_SECONDS", 5)
        deadline = time.monotonic() + wait
        with self.connect(True) as db:
            db.execute("DELETE FROM api_waiters WHERE expires<=?", (time.time(),))
            if db.execute("SELECT COUNT(*) FROM api_waiters").fetchone()[0] >= setting("QUEUE_CAPACITY", 8):
                raise APIError("concurrency_limited", "等待队列已满，请稍后重试。", 429, 2)
            if db.execute("SELECT 1 FROM api_waiters WHERE user_id=?", (uid,)).fetchone():
                raise APIError("concurrency_limited", "同账号已有等待中的查询。", 429, 2)
            db.execute("INSERT INTO api_waiters VALUES(?,?,?,?,?,?)", (ticket, uid, lane, priority, time.time(), time.time()+wait+1))
        try:
            while True:
                if cancelled() or self.waiting_cancelled(uid, request_id):
                    raise APIError("cancelled", "查询已取消。", 408)
                if time.monotonic() >= deadline:
                    raise APIError("concurrency_limited", "等待执行名额超时，请稍后重试。", 429, 2)
                stack = ExitStack()
                admitted = False
                try:
                    with self.connect(True) as db:
                        db.execute("DELETE FROM api_waiters WHERE expires<=?", (time.time(),))
                        first = db.execute("SELECT id FROM api_waiters WHERE lane=? ORDER BY priority DESC,rowid LIMIT 1", (lane,)).fetchone()
                        if first and first["id"] == ticket:
                            try:
                                stack.enter_context(self._admission_nowait(uid, lane=lane, near_text=near_text))
                                db.execute("DELETE FROM api_waiters WHERE id=?", (ticket,))
                                admitted = True
                            except APIError as exc:
                                if exc.code != "concurrency_limited":
                                    raise
                    if admitted:
                        if cancelled() or self.waiting_cancelled(uid, request_id):
                            raise APIError("cancelled", "查询已取消。", 408)
                        yield
                        return
                finally:
                    stack.close()
                if time.monotonic() >= deadline:
                    raise APIError("concurrency_limited", "等待执行名额超时，请稍后重试。", 429, 2)
                time.sleep(min(0.05, max(0, deadline-time.monotonic())))
        finally:
            with self.connect(True) as db:
                db.execute("DELETE FROM api_waiters WHERE id=?", (ticket,))

    @contextmanager
    def _admission_nowait(self, uid, *, lane="heavy", near_text=False, control=False):
        """File descriptors own slots, never leases that can expire during work."""
        directory = self.root / "agent_api_locks"
        directory.mkdir(parents=True, exist_ok=True)
        handles = []
        try:
            limits = scheduling()
            if lane not in {"heavy", "light"} or (control and lane != "light") or (near_text and lane != "heavy"):
                raise ValueError("Invalid API admission lane")
            groups = [(f"control-{int(uid)}", 1) if control else
                      (f"user-{int(uid)}", limits["user_data_concurrency"]),
                      ("control" if control else "global" if lane == "heavy" else "light", limits["control_concurrency" if control else lane + "_concurrency"])]
            if near_text:
                groups.append(("near-text", 1))
            for group, limit in groups:
                for slot in range(limit):
                    fd = os.open(directory / f"{group}-{slot}.lock", os.O_CREAT | os.O_RDWR, 0o600)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        os.close(fd)
                    else:
                        handles.append(fd)
                        break
                else:
                    raise APIError("concurrency_limited", "查询名额已占用，请串行调用并稍后重试。", 429, 2)
            yield
        finally:
            for fd in reversed(handles):
                os.close(fd)
