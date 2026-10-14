"""Durable public-book workflow, separate from recommendation file storage."""
from __future__ import annotations

import time
import uuid

import personal_library as library
from membership import utc_now_text

LABELS = {
    'queued': '审核通过，等待处理', 'processing': '正在处理',
    'review': '正文已准备，等待目录和版本校对', 'ready': '验收通过，等待正式发布',
    'published': '已永久收录', 'rejected': '已退回', 'failed': '处理失败',
}


def setup(c):
    c.executescript('''
        CREATE TABLE IF NOT EXISTS recommendation_publications (
          sha256 TEXT PRIMARY KEY, recommendation_id INTEGER NOT NULL,
          state TEXT NOT NULL, reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, batch_id TEXT NOT NULL, book_key TEXT NOT NULL DEFAULT '',
          release_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
          completed_pages INTEGER NOT NULL DEFAULT 0, total_pages INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '', lease REAL NOT NULL DEFAULT 0,
          package_sha256 TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS recommendation_publication_events (
          id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, recommendation_id INTEGER NOT NULL,
          at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
    ''')


def event(c, row, actor, action, detail=''):
    c.execute('INSERT INTO recommendation_publication_events '
              '(sha256,recommendation_id,at,actor,action,detail) VALUES(?,?,?,?,?,?)',
              (row['sha256'], row.get('recommendation_id', row.get('id')), utc_now_text(),
               str(actor), action, str(detail)[:1000]))


def get(digest):
    with library._connect() as c:
        setup(c)
        row = c.execute('SELECT * FROM recommendation_publications WHERE sha256=?', (digest,)).fetchone()
        return dict(row) if row else None


def decorate(rows, sources=None, release_id=''):
    """Recognize historical publications by PDF identity, never archived status."""
    sources = sources or {}
    with library._connect() as c:
        setup(c)
        jobs = {r['sha256']: dict(r) for r in c.execute('SELECT * FROM recommendation_publications')}
    for row in rows:
        job = jobs.get(row['sha256']) or {}
        published = sources.get(row['sha256'])
        if published:
            job = dict(job, state='published', book_key=published, release_id=release_id)
        elif job.get('state') == 'published':
            job = dict(job, state='ready', error='当前版本未包含此书，等待重新发布')
        row['publication'] = job
        row['publication_label'] = LABELS.get(job.get('state'), '等待审核')
    return rows


def decide(rid, action, actor, reason=''):
    if action not in {'approve', 'reject', 'retry'}:
        raise ValueError('无效审核操作')
    with library._connect() as c:
        setup(c)
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM user_book_recommendations WHERE id=?', (rid,)).fetchone()
        if not row or row['status'] not in {'pending', 'archived'} or not row['stored_at']:
            raise ValueError('PDF 尚未完成转存，不能审核入库')
        row = dict(row)
        old = c.execute('SELECT * FROM recommendation_publications WHERE sha256=?', (row['sha256'],)).fetchone()
        state = old['state'] if old else None
        if action == 'approve' and state in {'queued', 'processing', 'review', 'ready', 'published'}:
            return  # Duplicate approvals share one job by content hash.
        if action == 'retry' and state != 'failed':
            raise ValueError('只有处理失败的任务可以重试')
        if action == 'reject' and state in {'processing', 'ready', 'published'}:
            raise ValueError('正在处理或发布的书籍不能退回，请先完成当前任务')
        if action == 'reject' and not reason.strip():
            raise ValueError('请填写退回原因')
        now = utc_now_text()
        next_state = 'rejected' if action == 'reject' else 'queued'
        c.execute('''INSERT INTO recommendation_publications
            (sha256,recommendation_id,state,reviewer,reviewed_at,updated_at,batch_id,total_pages,error)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET
            state=excluded.state,reviewer=excluded.reviewer,reviewed_at=excluded.reviewed_at,
            updated_at=excluded.updated_at,error=excluded.error,owner='',lease=0''',
            (row['sha256'], rid, next_state, str(actor), now, now,
             'recommendation-' + row['sha256'][:16], row['page_count'], reason.strip()[:1000] if action == 'reject' else ''))
        event(c, row, actor, action, reason)
        if action == 'reject':
            c.execute("UPDATE user_book_recommendations SET status='archived' WHERE sha256=? AND status='pending'",
                      (row['sha256'],))


def claim():
    with library._connect() as c:
        setup(c)
        c.execute('BEGIN IMMEDIATE')
        now = time.time()
        if c.execute("SELECT 1 FROM recommendation_publications WHERE state='processing' AND lease>?", (now,)).fetchone():
            return None
        row = c.execute("SELECT * FROM recommendation_publications WHERE state='queued' "
                        "OR (state='processing' AND lease<=?) ORDER BY reviewed_at LIMIT 1", (now,)).fetchone()
        if not row:
            return None
        job = dict(row, owner=uuid.uuid4().hex)
        c.execute("UPDATE recommendation_publications SET state='processing',owner=?,lease=?,attempts=attempts+1 "
                  'WHERE sha256=?', (job['owner'], now + 180, job['sha256']))
        event(c, job, 'worker', 'claim')
        return job


def progress(job, completed, *, state='processing', error=''):
    if state not in {'processing', 'review', 'failed'}:
        raise ValueError('无效处理状态')
    with library._connect() as c:
        changed = c.execute('UPDATE recommendation_publications SET state=?,completed_pages=?,error=?,updated_at=?,lease=? '
                            'WHERE sha256=? AND owner=? AND state=\'processing\'',
                            (state, completed, str(error)[:1000], utc_now_text(), time.time()+180,
                             job['sha256'], job['owner'])).rowcount
        if changed != 1:
            raise RuntimeError('任务租约已变化，停止旧执行器')
        if state != 'processing':
            event(c, job, 'worker', state, error)


def mark_ready(digest, package_hash, book_key, actor):
    with library._connect() as c:
        setup(c)
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM recommendation_publications WHERE sha256=?', (digest,)).fetchone()
        if not row or row['state'] not in {'review', 'ready'}:
            raise ValueError('任务尚未完成正文处理')
        c.execute("UPDATE recommendation_publications SET state='ready',package_sha256=?,book_key=?,error='',updated_at=? WHERE sha256=?",
                  (package_hash, book_key, utc_now_text(), digest))
        event(c, dict(row), actor, 'quality_review', package_hash)


def mark_published(digest, book_key, release_id):
    if not release_id or not book_key:
        raise ValueError('缺少正式发布身份')
    with library._connect() as c:
        setup(c)
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM recommendation_publications WHERE sha256=?', (digest,)).fetchone()
        if not row or row['state'] not in {'ready', 'published'}:
            raise ValueError('荐书未通过发布前校对')
        if row['book_key'] != book_key:
            raise ValueError('发布书目与已验收书目不符')
        if row['state']=='published' and row['release_id']==release_id:
            return
        c.execute("UPDATE recommendation_publications SET state='published',book_key=?,release_id=?,updated_at=? WHERE sha256=?",
                  (book_key, release_id, utc_now_text(), digest))
        c.execute("UPDATE user_book_recommendations SET status='archived' WHERE sha256=? AND status='pending'", (digest,))
        event(c, dict(row), 'release', 'published', release_id)


def register_historical(sources, release_id):
    """Link already serving originals without inventing a historical reviewer."""
    if not release_id:
        raise ValueError('缺少正式发布身份')
    with library._connect() as c:
        setup(c)
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute('SELECT * FROM user_book_recommendations ORDER BY id').fetchall()
        for raw in rows:
            row=dict(raw);key=sources.get(row['sha256'])
            if not key:
                continue
            now=utc_now_text()
            added=c.execute('''INSERT OR IGNORE INTO recommendation_publications
                (sha256,recommendation_id,state,reviewer,reviewed_at,updated_at,batch_id,
                 total_pages,completed_pages,book_key,release_id)
                VALUES(?,?,'published','historical_import','',?,'historical-recommendations',?,?,?,?)''',
                (row['sha256'],row['id'],now,row['page_count'],row['page_count'],key,release_id)).rowcount
            if added:
                event(c,row,'release','historical_publication_link',release_id)
