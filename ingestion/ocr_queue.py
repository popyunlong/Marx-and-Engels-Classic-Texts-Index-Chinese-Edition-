"""One global leased free-OCR lane, usable by desktop and server executors."""
from __future__ import annotations
import hashlib
import json
import time
import uuid
import re

from .free_ocr import PROFILE, compare, validate


def setup(store):
    with store.connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS ocr_batches(batch TEXT PRIMARY KEY,profile TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS free_ocr(
          book TEXT NOT NULL,page INTEGER NOT NULL,profile TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
          owner TEXT NOT NULL DEFAULT '',token TEXT NOT NULL DEFAULT '',lease REAL NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,
          image_sha256 TEXT NOT NULL DEFAULT '',result TEXT NOT NULL DEFAULT '{}',
          payload_sha256 TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL DEFAULT 0,
          PRIMARY KEY(book,page,profile));
        CREATE TABLE IF NOT EXISTS ocr_nodes(id TEXT PRIMARY KEY,expires REAL NOT NULL);''')


def enable(store, batch):
    with store.connect() as c:
        if not c.execute('SELECT 1 FROM batches WHERE id=?',(batch,)).fetchone():
            raise KeyError('批次不存在')
        c.execute('INSERT OR REPLACE INTO ocr_batches VALUES(?,?)',(batch,PROFILE))
        c.execute("INSERT OR IGNORE INTO free_ocr(book,page,profile) SELECT p.book,p.page,? "
                  "FROM pages p JOIN books b ON b.id=p.book WHERE b.batch=?",(PROFILE,batch))
    store.event('free_ocr_enabled',batch=batch,profile=PROFILE)


def enroll(store, book):
    with store.connect() as c:
        c.execute("INSERT OR IGNORE INTO free_ocr(book,page,profile) SELECT p.book,p.page,q.profile FROM pages p "
                  "JOIN books b ON b.id=p.book JOIN ocr_batches q ON q.batch=b.batch WHERE b.id=?",(book,))


def claim(store, owner, *, local=False):
    if not owner or len(owner)>100:
        raise ValueError('OCR 节点标识无效')
    now=time.time()
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if local:
            # Covers the entire <=55s page plus transfer; explicit shutdown releases
            # it immediately. Expiry still lets the server recover after a crash.
            c.execute('INSERT OR REPLACE INTO ocr_nodes VALUES(?,?)',(owner,now+75))
        elif c.execute('SELECT 1 FROM ocr_nodes WHERE expires>? LIMIT 1',(now,)).fetchone():
            return None
        if c.execute("SELECT 1 FROM free_ocr WHERE status='running' AND lease>? LIMIT 1",(now,)).fetchone():
            return None
        if c.execute("SELECT 1 FROM pages WHERE stage='ocr' AND lease>? LIMIT 1",(now,)).fetchone():
            return None
        row=c.execute("SELECT q.*,b.path,b.sha,p.pilot,b.batch FROM free_ocr q JOIN books b ON b.id=q.book "
                      "JOIN pages p ON p.book=q.book AND p.page=q.page JOIN batches a ON a.id=b.batch "
                      "WHERE q.status IN ('pending','running') AND q.lease<=? AND q.retry_at<=? AND b.paused=0 "
                      "AND b.status NOT IN ('uploading','published') AND (a.pilot=0 OR p.pilot=1) "
                      "ORDER BY p.pilot DESC,b.created,CASE p.stage WHEN 'ocr_check' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,q.page LIMIT 1",(now,now)).fetchone()
        if row is None:return None
        job=dict(row); job.update(owner=owner,token=uuid.uuid4().hex,lease=now+65)
        c.execute("UPDATE free_ocr SET status='running',owner=?,token=?,lease=?,updated=? WHERE book=? AND page=? AND profile=?",
                  (owner,job['token'],job['lease'],now,job['book'],job['page'],PROFILE))
        return job


def image_issued(store,job,digest):
    with store.connect() as c:
        changed=c.execute('UPDATE free_ocr SET image_sha256=? WHERE book=? AND page=? AND profile=? AND token=? AND lease>?',
                          (digest,job['book'],job['page'],PROFILE,job['token'],time.time())).rowcount
        if changed!=1:raise ValueError('OCR 页面租约已失效')


def leased(store, book, page, token):
    with store.connect() as c:
        row=c.execute("SELECT q.*,b.path FROM free_ocr q JOIN books b ON b.id=q.book "
                      "WHERE q.book=? AND q.page=? AND q.profile=? AND q.token=? "
                      "AND q.status='running' AND q.lease>?",
                      (book,page,PROFILE,token,time.time())).fetchone()
    if row is None:raise ValueError('OCR 页面租约已失效')
    return dict(row)


def submit(store,book,page,token,result):
    validate(result)
    encoded=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    digest=hashlib.sha256(encoded.encode()).hexdigest()
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM free_ocr WHERE book=? AND page=? AND profile=?',(book,page,PROFILE)).fetchone()
        if row is None or row['token']!=token:raise ValueError('OCR 页面租约已变更')
        if row['status']=='done' and row['payload_sha256']==digest:return {'ok':True,'duplicate':True}
        if row['lease']<=time.time() or row['status']!='running':raise ValueError('OCR 页面租约已失效')
        if row['image_sha256']!=result['image_sha256']:raise ValueError('OCR 结果与领取的页图不符')
        c.execute("UPDATE free_ocr SET status='done',lease=0,result=?,payload_sha256=?,error='',updated=? "
                  "WHERE book=? AND page=? AND profile=?",(encoded,digest,time.time(),book,page,PROFILE))
    store.event('free_ocr_received',book,page,profile=PROFILE,image_sha256=result['image_sha256'])
    return {'ok':True}


def failed(store,job,error):
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE free_ocr SET status=CASE WHEN attempts>=2 THEN 'failed' ELSE 'pending' END,attempts=attempts+1,"
                  "lease=0,retry_at=?,error=?,updated=? WHERE book=? AND page=? AND profile=? AND token=? AND status='running'",
                  (time.time()+30,str(error)[:200],time.time(),job['book'],job['page'],PROFILE,job['token']))
        row=c.execute('SELECT status FROM free_ocr WHERE book=? AND page=? AND profile=? AND token=?',
                      (job['book'],job['page'],PROFILE,job['token'])).fetchone()
        if row and row['status']=='failed':
            c.execute("UPDATE pages SET stage='review',error=? WHERE book=? AND page=? AND stage='ocr_check' AND owner=''",
                      ('免费 OCR 重试三次仍失败，待核对：'+str(error)[:160],job['book'],job['page']))


def detail(store,book,page):
    with store.connect() as c:
        row=c.execute('SELECT * FROM free_ocr WHERE book=? AND page=? AND profile=?',(book,page,PROFILE)).fetchone()
        p=c.execute('SELECT text FROM pages WHERE book=? AND page=?',(book,page)).fetchone()
    if row is None:return None
    result=json.loads(row['result'])
    return {'status':row['status'],'error':row['error'],'result':result,
            'comparison':compare(p['text'],result) if row['status']=='done' and p else None}


def release_node(store,owner):
    with store.connect() as c:
        c.execute('DELETE FROM ocr_nodes WHERE id=?',(owner,))


def apply_ready(store):
    """Resume waiting pages from durable evidence, without expanding paid sampling."""
    with store.connect() as c:
        rows=[dict(r) for r in c.execute("SELECT p.*,q.result AS ocr_result FROM pages p JOIN free_ocr q "
             "ON q.book=p.book AND q.page=p.page WHERE p.stage='ocr_check' AND p.owner='' AND q.status='done'")]
        for row in rows:
            result=json.loads(row['ocr_result']); history=json.loads(row['result'])
            comparison=compare(row['text'],result)
            history['free_ocr']={'profile':PROFILE,'image_sha256':result['image_sha256'],
                                 'equal':comparison['equal'],'differences':comparison['differences'],'flags':comparison['flags']}
            label=row['label']; candidates=[]
            if row['kind']=='body' and not label:
                for line in result['lines']:
                    match=re.fullmatch(r'\s*[—–-]?\s*([0-9]{1,4})\s*[—–-]?\s*',line['text'])
                    y=sum(p[1] for p in line['box'])/4
                    if match and line['score']>=.98 and (y<result['height']*.15 or y>result['height']*.85):
                        candidates.append((match.group(1),line['box']))
                if len(candidates)==1:
                    label=candidates[0][0]
                    history['folio_ocr_evidence']={'label':label,'box':candidates[0][1],'profile':PROFILE}
            # Never fabricate missing characters or accept mere OCR confidence.
            # Unselected discrepancies await image review; they do not automatically
            # create another paid MiMo call or trigger whole-book verification.
            issue = '免费 OCR 与原识别有文字或标点分歧，待对照原图' if not comparison['equal'] else ''
            if row['kind']=='body' and not label:issue='免费 OCR 仍未唯一定位印刷页码'
            if comparison['flags']:issue=issue or '免费 OCR 存在低置信度或孤立符号，待核对'
            stage='review' if issue else 'done'
            c.execute('UPDATE pages SET stage=?,label=?,result=?,error=? WHERE book=? AND page=? AND stage=\'ocr_check\' AND owner=\'\'',
                      (stage,label,json.dumps(history,ensure_ascii=False),issue,row['book'],row['page']))
            # Original text, checked flags and prior model runs remain untouched.
