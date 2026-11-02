"""Durable single-slot queue. Importing this module never starts work."""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
from pathlib import Path

from .layout import LAYOUT_SCHEMA_VERSION

ACTIVE = ("queued", "running")


class Conflict(ValueError):
    pass


class Paused(ValueError):
    pass


class Queue:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "jobs.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(
                  run_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, sid INTEGER NOT NULL,
                  source_sha256 TEXT NOT NULL, layout_schema INTEGER NOT NULL,
                  total INTEGER NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                  peak_bytes INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_book_job ON jobs(uid,sid)
                  WHERE state IN ('queued','running');
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_job ON jobs((1)) WHERE state='running';
                CREATE TABLE IF NOT EXISTS pages(
                  uid INTEGER NOT NULL, sid INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
                  layout_schema INTEGER NOT NULL, page INTEGER NOT NULL, payload TEXT NOT NULL,
                  has_text INTEGER NOT NULL,
                  PRIMARY KEY(uid,sid,source_sha256,layout_schema,page));
                CREATE TABLE IF NOT EXISTS admissions(
                  uid INTEGER NOT NULL,sid INTEGER NOT NULL,remaining INTEGER NOT NULL,
                  reason TEXT NOT NULL,PRIMARY KEY(uid,sid));
                CREATE TABLE IF NOT EXISTS audit(at REAL NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def audit(db, event, payload):
        db.execute("INSERT INTO audit VALUES(?,?,?)", (time.time(), event, json.dumps(payload)))

    def grant(self, uid: int, sid: int, reason: str):
        if uid <= 0 or sid <= 0 or not reason.strip():
            raise ValueError("uid, sid and authorization reason are required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT remaining FROM admissions WHERE uid=? AND sid=?", (uid,sid)).fetchone()
            if row and row[0]:
                raise Conflict("An unused admission already exists")
            db.execute("INSERT INTO admissions VALUES(?,?,1,?) ON CONFLICT(uid,sid) DO UPDATE SET remaining=1,reason=excluded.reason", (uid,sid,reason))
            self.audit(db, "grant", {"uid":uid,"sid":sid,"reason":reason})

    def enqueue(self, uid: int, sid: int, run_id: str, sha: str, total: int):
        if uid <= 0 or sid <= 0 or total <= 0 or total > 10000:
            raise ValueError("Invalid uid/sid/page budget")
        if not re.fullmatch(r"[a-f0-9]{32}", run_id) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Invalid run_id or source fingerprint")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            if old:
                if (old['uid'],old['sid'],old['source_sha256'],old['total']) != (uid,sid,sha,total):
                    raise Conflict("run_id was already bound to different input")
                return self._status(db, old)
            if db.execute("SELECT 1 FROM jobs WHERE uid=? AND sid=? AND state IN ('queued','running')",(uid,sid)).fetchone():
                raise Conflict("This book already has an active task")
            admission = db.execute("SELECT remaining FROM admissions WHERE uid=? AND sid=?", (uid,sid)).fetchone()
            if not admission or admission[0] != 1:
                raise Paused("No unused administrator-authorized admission")
            incompatible = db.execute("SELECT 1 FROM pages WHERE uid=? AND sid=? AND (source_sha256<>? OR layout_schema<>?) LIMIT 1",(uid,sid,sha,LAYOUT_SCHEMA_VERSION)).fetchone()
            if incompatible:
                raise Conflict("Checkpoint fingerprint/schema mismatch; preserved for review")
            now = time.time()
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,'queued','',0,?,?)",
                       (run_id,uid,sid,sha,LAYOUT_SCHEMA_VERSION,total,now,now))
            db.execute("UPDATE admissions SET remaining=0 WHERE uid=? AND sid=?",(uid,sid))
            self.audit(db,"enqueue",{"uid":uid,"sid":sid,"run_id":run_id})
            return self._status(db,db.execute("SELECT * FROM jobs WHERE run_id=?",(run_id,)).fetchone())

    def _status(self, db, row):
        if not row:
            return None
        result = dict(row)
        count = db.execute("SELECT COUNT(*),COALESCE(SUM(has_text),0) FROM pages WHERE uid=? AND sid=? AND source_sha256=? AND layout_schema=? AND page<=?", (row['uid'],row['sid'],row['source_sha256'],row['layout_schema'],row['total'])).fetchone()
        result.update(processed_pages=count[0], checkpoint_pages=count[0], text_pages=count[1],
                      done=count[0], protocol_version=2, peak_scope='worker-cgroup-lifetime')
        return result

    def status(self, uid, sid, run_id):
        with self.connect() as db:
            return self._status(db,db.execute("SELECT * FROM jobs WHERE uid=? AND sid=? AND run_id=?",(uid,sid,run_id)).fetchone())

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                return None
            row=db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created,run_id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running',updated=? WHERE run_id=?",(time.time(),row['run_id']))
                return dict(row, state='running')
        return None

    def save_page(self, job, payload):
        page=int(payload['page'])
        if not 1 <= page <= job['total']:
            raise ValueError("Page outside admitted range")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE run_id=? AND state='running'",(job['run_id'],)).fetchone():
                raise Conflict("Task is no longer active")
            db.execute("INSERT OR IGNORE INTO pages VALUES(?,?,?,?,?,?,?)",(job['uid'],job['sid'],job['source_sha256'],job['layout_schema'],page,json.dumps(payload,ensure_ascii=False),int(bool(payload.get('text')))))
            db.execute("UPDATE jobs SET updated=? WHERE run_id=?",(time.time(),job['run_id']))

    def pages(self, job):
        with self.connect() as db:
            rows=db.execute("SELECT payload FROM pages WHERE uid=? AND sid=? AND source_sha256=? AND layout_schema=? AND page<=? ORDER BY page",(job['uid'],job['sid'],job['source_sha256'],job['layout_schema'],job['total'])).fetchall()
            return [json.loads(r[0]) for r in rows]

    def terminal(self, run_id, state, error='', peak_bytes=0):
        if state not in ('done','failed','interrupted'):
            raise ValueError("Not a terminal state")
        with self.connect() as db:
            row=db.execute("SELECT * FROM jobs WHERE run_id=? AND state='running'",(run_id,)).fetchone()
            if not row:
                raise Conflict("Task no longer running")
            if state=='done' and self._status(db,row)['processed_pages'] != row['total']:
                raise Conflict("Incomplete task cannot finish")
            db.execute("UPDATE jobs SET state=?,error=?,peak_bytes=max(peak_bytes,?),updated=? WHERE run_id=?",(state,error[:300],peak_bytes,time.time(),run_id))
            self.audit(db,state,{"run_id":run_id,"error":error[:300]})

    def recover_running(self):
        """Only the exclusively locked worker may reconcile its former jobs."""
        with self.connect() as db:
            count=db.execute("UPDATE jobs SET state='interrupted',error='OCR worker exited; explicit recovery required',updated=? WHERE state='running'",(time.time(),)).rowcount
            if count:
                self.audit(db,'worker_restart',{'interrupted':count})
        if count:
            self.set_dispatch(False)
        return count

    def set_dispatch(self, enabled):
        flag=self.root/'dispatch.enabled'
        if enabled:
            flag.write_text('enabled\n')
        else:
            flag.unlink(missing_ok=True)
        with self.connect() as db:
            self.audit(db,'dispatch',{'enabled':bool(enabled)})

    def dispatch_enabled(self):
        return (self.root/'dispatch.enabled').is_file()
