"""Evidence-based, nested sampling. A disagreement is not a confirmed error."""
from __future__ import annotations
import json
import math
import time

from .recognize import key


def enable_full(store, batch):
    """Explicit batch-wide visual audit; retain accepted pages and all charges."""
    state_key='full_audit_'+batch
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing=c.execute('SELECT value FROM state WHERE key=?',(state_key,)).fetchone()
        if existing:return {**json.loads(existing[0]),'already_enabled':True}
        if not c.execute('SELECT 1 FROM batches WHERE id=?',(batch,)).fetchone():
            raise KeyError('批次不存在')
        rows=[dict(r) for r in c.execute("SELECT p.* FROM pages p JOIN books b ON b.id=p.book "
              "WHERE b.batch=? AND b.status!='published' ORDER BY b.created,p.page",(batch,))]
        if not rows:raise ValueError('批次没有可核验页面')
        if any(r['lease']>time.time() for r in rows):raise ValueError('等待当前页面处理结束后切换核验模式')
        if any(r['stage'] in {'glm','ocr'} for r in rows):raise ValueError('先完成首轮识别再切换全量核验')
        counts={'total':len(rows),'reused_verified':0,'queued':0,'filtered':0,'policy':'full-v1'}
        for row in rows:
            if row['checked']:
                counts['reused_verified']+=1
                continue
            history=json.loads(row['result'])
            history['previous_audit_policy']=history.get('audit',{})
            history['full_audit_previous_error']=row['error']
            history['audit']={'policy':'full-v1','round':1,'reasons':['用户要求本批逐页 MiMo 核验']}
            filtered=any(r.get('stage') in {'mimo','verify'} and r.get('finish')=='content_filter'
                         for r in history.get('runs',[]))
            counts['filtered' if filtered else 'queued']+=1
            c.execute("UPDATE pages SET stage=?,result=?,owner='',lease=0,attempts=0,retry_at=0,error=? "
                      "WHERE book=? AND page=?",
                      ('review' if filtered else 'mimo',json.dumps(history,ensure_ascii=False),
                       'MiMo 已明确内容过滤；保留原图与 OCR，避免重复收费重试' if filtered else '',row['book'],row['page']))
        c.execute("UPDATE books SET audit_level=3,status='processing' WHERE batch=? AND status!='published'",(batch,))
        c.execute('INSERT INTO state VALUES(?,?)',(state_key,json.dumps(counts)))
        c.execute('INSERT INTO events(at,kind,payload) VALUES(?,?,?)',
                  (time.time(),'full_audit_enabled',json.dumps({'batch':batch,**counts},ensure_ascii=False)))
    return counts


def body_pages(rows):
    return [r['page'] for r in rows if r['kind'] not in {'toc','copyright','cover','blank'}
            and (r['kind']=='body' or len(key(r['text']))>=100)]


def targets(rows, level=1):
    pages=body_pages(rows)
    if not pages:return set()
    n=len(pages)
    count=min(n,max(20,math.ceil(n*.1)))
    indices={round(i*(n-1)/max(1,count-1)) for i in range(count)}
    if level>=3:return set(pages)
    if level==2:
        # For short books the 20-page minimum may already exceed 20%.
        # Add at least one new page before judging the expanded round.
        target=min(n,max(count+1,math.ceil(n*.2)))
        while len(indices)<target:
            point=max((i for i in range(n) if i not in indices),key=lambda i:min(abs(i-j) for j in indices))
            indices.add(point)
    return {pages[i] for i in indices}


def mandatory(row):
    history=json.loads(row['result']) if isinstance(row['result'],str) else row['result']
    reasons=[]
    if row.get('pilot'):reasons.append('试跑核验')
    if row['kind']=='toc':reasons.append('目录全查')
    if row['kind'] in {'copyright','cover'}:reasons.append('书目版权证据')
    if any(r.get('stage')=='ocr' for r in history.get('runs',[])):reasons.append('OCR 兜底页全查')
    if history.get('folio_conflict_confirmed'):reasons.append('OCR 定位后仍有页码冲突')
    return reasons


def mark(history, reasons, round_number=1):
    history['audit']={'reasons':reasons,'round':round_number,'policy':'sample-v2'}
    return history


def confirmed_error(store, job, history):
    # Only call after the second independent visual read confirms a correction.
    round_number=history.get('audit',{}).get('round',1)
    wanted=3 if round_number>=2 else 2
    with store.connect() as c:
        c.execute('UPDATE books SET audit_level=MAX(audit_level,?) WHERE id=?',(wanted,job['book']))
    store.event('confirmed_audit_error',job['book'],job['page'],round=round_number,expand_to=wanted)


def schedule(store, book):
    with store.connect() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM pages WHERE book=? ORDER BY page',(book['id'],))]
        if any(r['stage']=='glm' for r in rows):return
        base=targets(rows,1); selected=targets(rows,book['audit_level'])
        for row in rows:
            if row['page'] not in selected or row['checked'] or row['stage'] not in {'done','ocr_check'} or row['lease']:
                continue
            history=json.loads(row['result'])
            round_number=1 if row['page'] in base else (2 if book['audit_level']==2 else 3)
            mark(history,['正文分层抽检'],round_number)
            c.execute("UPDATE pages SET stage='mimo',result=? WHERE book=? AND page=? AND owner=''",
                      (json.dumps(history,ensure_ascii=False),book['id'],row['page']))


def reconcile_legacy(store, *, apply=False):
    """Only withdraw unstarted blanket checks. Preserve all reads, disputes and charges."""
    changes=[]; levels=[]
    with store.connect() as c:
        books=[dict(r) for r in c.execute('SELECT * FROM books')]
        for book in books:
            rows=[dict(r) for r in c.execute('SELECT * FROM pages WHERE book=? ORDER BY page',(book['id'],))]
            # Legacy code did not record a genuine expanded sample round.
            # Existing confirmed repairs warrant 20%, not automatic 100%.
            confirmed=any(r['repairs']>0 for r in rows)
            level=2 if confirmed else 1
            levels.append({'book':book['id'],'before':book['audit_level'],'after':level})
            first=targets(rows,1); selected=targets(rows,level)
            for row in rows:
                if row['stage']!='mimo' or row['owner'] or row['lease']:
                    continue
                history=json.loads(row['result'])
                if any(r.get('stage') in {'mimo','verify'} for r in history.get('runs',[])):
                    continue
                reasons=mandatory(row)
                if row['page'] in selected:reasons.append('正文分层抽检')
                stage='mimo' if reasons else 'ocr_check'
                mark(history,reasons,1 if row['page'] in first or mandatory(row) else 2)
                changes.append({'book':book['id'],'page':row['page'],'before':'mimo','after':stage,'reasons':reasons})
                if apply:
                    c.execute('UPDATE pages SET stage=?,result=?,error=?,retry_at=0 WHERE book=? AND page=?',
                              (stage,json.dumps(history,ensure_ascii=False),'' if reasons else '等待免费 OCR 复读与页码定位',book['id'],row['page']))
            if apply:
                c.execute('UPDATE books SET audit_level=? WHERE id=?',(level,book['id']))
    if apply:store.event('audit_policy_reconciled',policy='sample-v2',levels=levels,changes=changes)
    return {'levels':levels,'changes':changes}
