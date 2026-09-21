from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path


def sha256(path: Path, checkpoint=lambda: None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            checkpoint()
            digest.update(block)
    return digest.hexdigest()


def samples(count: int, minimum: int = 20, fraction: float = .1) -> set[int]:
    n = min(count, max(minimum, math.ceil(count * fraction)))
    return {1 + round(i * (count - 1) / max(1, n - 1)) for i in range(n)}


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.sqlite3"
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS batches(
              id TEXT PRIMARY KEY, title TEXT NOT NULL, budget REAL,
              pilot INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS books(
              id TEXT PRIMARY KEY, batch TEXT NOT NULL, sha TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
              path TEXT NOT NULL DEFAULT '', pages INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'uploading', paused INTEGER NOT NULL DEFAULT 0,
              metadata TEXT NOT NULL DEFAULT '{}', toc TEXT NOT NULL DEFAULT '[]',
              error TEXT NOT NULL DEFAULT '', audit_level INTEGER NOT NULL DEFAULT 1,
              created REAL NOT NULL, FOREIGN KEY(batch) REFERENCES batches(id));
            CREATE TABLE IF NOT EXISTS pages(
              book TEXT NOT NULL, page INTEGER NOT NULL, pilot INTEGER NOT NULL DEFAULT 0,
              stage TEXT NOT NULL DEFAULT 'glm', attempts INTEGER NOT NULL DEFAULT 0,
              result TEXT NOT NULL DEFAULT '{}', text TEXT NOT NULL DEFAULT '',
              label TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
              checked INTEGER NOT NULL DEFAULT 0, repairs INTEGER NOT NULL DEFAULT 0,
              owner TEXT NOT NULL DEFAULT '', lease REAL NOT NULL DEFAULT 0,
              retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(book,page), FOREIGN KEY(book) REFERENCES books(id));
            CREATE TABLE IF NOT EXISTS events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, book TEXT,
              page INTEGER, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls(
              id TEXT PRIMARY KEY, batch TEXT NOT NULL, book TEXT NOT NULL, page INTEGER,
              provider TEXT NOT NULL, reserved REAL NOT NULL, cost REAL,
              usage TEXT, seconds REAL, status TEXT NOT NULL DEFAULT 'reserved');
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS page_work ON pages(stage,retry_at,lease);
            """)
        from .ocr_queue import setup
        setup(self)

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=2)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def event(self, kind, book=None, page=None, **payload):
        with self.connect() as c:
            c.execute("INSERT INTO events(at,book,page,kind,payload) VALUES(?,?,?,?,?)",
                      (time.time(), book, page, kind, json.dumps(payload, ensure_ascii=False)))

    def batch(self, title, batch_id=None):
        batch_id = batch_id or uuid.uuid4().hex
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO batches(id,title,created) VALUES(?,?,?)",
                      (batch_id, title, time.time()))
        return batch_id

    def book(self, batch, name, digest, size):
        if len(digest) != 64 or any(x not in "0123456789abcdef" for x in digest):
            raise ValueError("文件哈希无效")
        if not name.lower().endswith(".pdf") or size <= 0 or size > 20 * 1024**3:
            raise ValueError("只接受不超过 20 GiB 的 PDF")
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO books(id,batch,sha,name,size,created) VALUES(?,?,?,?,?,?)",
                      (uuid.uuid4().hex, batch, digest, Path(name).name, size, time.time()))
            return dict(c.execute("SELECT * FROM books WHERE sha=?", (digest,)).fetchone())

    def get_book(self, book):
        with self.connect() as c:
            row = c.execute("SELECT * FROM books WHERE id=?", (book,)).fetchone()
            if row is None:
                raise KeyError("书目不存在")
            return dict(row)

    def initialize_pages(self, book, path, count, pilot_pages):
        with self.connect() as c:
            c.execute("UPDATE books SET path=?,pages=?,status='processing',error='' WHERE id=?",
                      (str(path), count, book))
            c.executemany("INSERT OR IGNORE INTO pages(book,page,pilot) VALUES(?,?,?)",
                          [(book, p, int(p in pilot_pages)) for p in range(1, count + 1)])
        from .ocr_queue import enroll
        enroll(self, book)

    def claim(self, provider: str, owner: str):
        now = time.time()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if provider=='ocr' and c.execute("SELECT 1 FROM free_ocr WHERE status='running' AND lease>? LIMIT 1",(now,)).fetchone():
                return None
            row = c.execute("""SELECT p.*,b.path,b.batch,b.pages AS total,b.name,b.audit_level
              FROM pages p JOIN books b ON b.id=p.book JOIN batches a ON a.id=b.batch
              WHERE p.stage=? AND p.lease<? AND p.retry_at<=? AND b.paused=0
                AND b.status='processing' AND (a.pilot=0 OR p.pilot=1)
                AND (p.stage NOT IN ('mimo','verify') OR NOT EXISTS(SELECT 1 FROM ocr_batches ob WHERE ob.batch=b.batch)
                  OR EXISTS(SELECT 1 FROM free_ocr q WHERE q.book=p.book AND q.page=p.page AND q.status='done'))
              ORDER BY p.pilot DESC,b.created,p.page LIMIT 1""", (provider, now, now)).fetchone()
            if row is None:
                return None
            row = dict(row)
            c.execute("UPDATE pages SET owner=?,lease=? WHERE book=? AND page=?",
                      (owner, now + 65, row["book"], row["page"]))
            return row

    def finish(self, job, owner, *, stage, result=None, text=None, label=None,
               kind=None, checked=None, repairs=None, error="", retry_at=0):
        fields = {"stage": stage, "owner": "", "lease": 0, "error": error,
                  "retry_at": retry_at}
        for key, value in dict(result=result, text=text, label=label, kind=kind,
                               checked=checked, repairs=repairs).items():
            if value is not None:
                fields[key] = json.dumps(value, ensure_ascii=False) if key == "result" else value
        fields["attempts"] = job["attempts"] + 1 if retry_at else 0
        with self.connect() as c:
            changed = c.execute("UPDATE pages SET " + ",".join(k + "=?" for k in fields) +
                                " WHERE book=? AND page=? AND owner=?",
                                (*fields.values(), job["book"], job["page"], owner)).rowcount
            if changed != 1:
                raise RuntimeError("页面租约已变更，拒绝过期结果")
        self.event("page_" + stage, job["book"], job["page"], error=error)

    def reserve(self, job, provider, amount):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            a = c.execute("SELECT * FROM batches WHERE id=?", (job["batch"],)).fetchone()
            spent = c.execute("SELECT COALESCE(SUM(COALESCE(cost,reserved)),0) FROM calls WHERE batch=?",
                              (job["batch"],)).fetchone()[0]
            # Pilot has a small explicit implementation ceiling; full runs require user budget.
            limit = 10.0 if a["pilot"] else a["budget"]
            if limit is None or spent + amount > limit:
                raise BudgetError("达到调用预留上限，等待设置预算")
            call_id = uuid.uuid4().hex
            c.execute("INSERT INTO calls(id,batch,book,page,provider,reserved) VALUES(?,?,?,?,?,?)",
                      (call_id, job["batch"], job["book"], job["page"], provider, amount))
            return call_id

    def charged(self, call_id, cost, usage, seconds, status="complete"):
        with self.connect() as c:
            c.execute("UPDATE calls SET cost=?,usage=?,seconds=?,status=? WHERE id=?",
                      (cost, json.dumps(usage), seconds, status, call_id))

    def snapshot(self):
        readiness_path = self.root / 'toc-page-readiness-20260908.json'
        try:
            readiness = {b['source_sha256']:b for b in json.loads(readiness_path.read_text(encoding='utf-8'))['books']}
        except (OSError, ValueError, KeyError):
            readiness = {}
        with self.connect() as c:
            books = [dict(r) for r in c.execute("SELECT * FROM books ORDER BY created")]
            for b in books:
                b['checked_pages'] = c.execute('SELECT COUNT(*) FROM pages WHERE book=? AND checked=1',(b['id'],)).fetchone()[0]
                b['release_readiness'] = readiness.get(b['sha'], {})
                b['free_ocr_counts'] = {r[0]:r[1] for r in c.execute(
                    'SELECT status,COUNT(*) FROM free_ocr WHERE book=? GROUP BY status',(b['id'],))}
                b["counts"] = {r[0]: r[1] for r in c.execute(
                    "SELECT stage,COUNT(*) FROM pages WHERE book=? GROUP BY stage", (b["id"],))}
                b["pilot_done"] = c.execute(
                    "SELECT COUNT(*) FROM pages WHERE book=? AND pilot=1 AND stage IN ('done','review')",
                    (b["id"],)).fetchone()[0]
            batches = [dict(r) for r in c.execute("SELECT * FROM batches ORDER BY created")]
            for a in batches:
                a["used_or_reserved_yuan"] = c.execute(
                    "SELECT COALESCE(SUM(COALESCE(cost,reserved)),0) FROM calls WHERE batch=?", (a["id"],)).fetchone()[0]
            state = {r[0]: json.loads(r[1]) for r in c.execute("SELECT * FROM state")}
        publication = {}
        try:
            journal=json.loads((self.root.parent/'marx-ingestion-releases/publication.json').read_text(encoding='utf-8'))
            publication={'phase':journal['phase'],'book_ids':[b['id'] for b in journal['record']['books']],
                         'started':journal.get('started'),'candidate_sha256':journal['record']['candidate_sha256']}
        except (OSError,ValueError,KeyError,TypeError):
            pass
        return {"books": books, "batches": batches, "scheduler": state, 'publication':publication}

    def set_state(self, key, value):
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def last_demand(self, observed=None):
        # Every API/worker/publisher observer shares one monotonic demand timestamp.
        with self.connect() as c:
            if observed is not None:
                c.execute("INSERT INTO state(key,value) VALUES('user_demand_at',?) "
                          "ON CONFLICT(key) DO UPDATE SET value="
                          "CAST(MAX(CAST(state.value AS REAL),CAST(excluded.value AS REAL)) AS TEXT)",
                          (json.dumps(observed),))
            row = c.execute("SELECT value FROM state WHERE key='user_demand_at'").fetchone()
            return float(row[0]) if row else 0.0


class BudgetError(RuntimeError):
    pass
