"""Generation-guarded private-library processing, with no import-time jobs."""
from __future__ import annotations

import threading
import time


class Processing:
    def __init__(self, library, store, corpus, write, finalize, *, sleep=time.sleep):
        self.library=library
        self.store=store
        self.corpus=corpus
        self.write=write
        self.finalize=finalize
        self.sleep=sleep

    def start(self,sid,*,resume=False,force=False):
        if force:
            raise ValueError('完整OCR重建已暂停；请使用已单独授权的“从断点继续OCR”')
        row=self.library.get_submission(sid)
        if resume and (not row or row.get('source_kind')!='scanned' or not row.get('ocr_consent') or not row.get('license_attested')):
            raise ValueError('该书未授权扫描件OCR')
        run_id=self.library.begin_processing_run(sid)
        threading.Thread(target=self.run,args=(sid,run_id),kwargs={'resume':resume},
                         name=f'mylib-process-{sid}-{run_id[:8]}',daemon=True).start()
        return run_id

    def wait(self,row,run_id,total,*,start=True):
        uid,sid=int(row['user_id']),int(row['id'])
        if start:
            health=self.store.ocr_health()
            if health.get('protocol_version')!=2 or not health.get('dispatch_enabled'):
                raise ValueError('受限OCR服务尚未放行；未向旧存储进程启动OCR')
            allowed,_=self.library.ocr_quota_allowance(uid,total)
            if allowed<total:
                raise ValueError('现有OCR额度不足以完成本次处理；未启动部分OCR')
            # One POST only. An ambiguous response is reconciled by this same run_id.
            try:
                self.store.ocr_start(uid,sid,max_pages=total,run_id=run_id,source_sha256=row['sha256'])
            except self.store.StoreError as exc:
                if getattr(exc,'status_code',None) in (400,401,403,409,423):
                    raise ValueError(str(exc)) from exc
        unavailable_since=None
        while self.library.processing_run_current(sid,run_id):
            try:
                status=self.store.ocr_status(uid,sid,run_id=run_id)
            except self.store.StoreError as exc:
                if getattr(exc,'status_code',None) in (400,401,403,404):
                    raise ValueError('原任务不可访问或已丢失，未自动恢复：'+str(exc)) from exc
                if unavailable_since is None:
                    unavailable_since=time.monotonic()
                if time.monotonic()-unavailable_since>180:
                    raise ValueError('节点状态连续180秒不可达；请先核对原任务，禁止盲目重新启动')
                self.sleep(15)
                continue
            unavailable_since=None
            if (status.get('run_id')!=run_id or status.get('source_sha256')!=row['sha256']
                    or int(status.get('uid',0))!=uid or int(status.get('sid',0))!=sid
                    or int(status.get('total',0))!=total):
                raise ValueError('节点任务标识、原件指纹或页数不匹配，结果未采用')
            self.library.processing_progress(sid,run_id,int(status.get('checkpoint_pages',0)),total)
            state=status.get('state')
            if state=='done':
                if int(status.get('processed_pages',0))!=total or int(status.get('checkpoint_pages',0))!=total:
                    raise ValueError('节点只返回部分结果，未生成派生数据或执行验收')
                return status
            if state not in ('queued','running'):
                raise ValueError('OCR任务已中断，未生成派生数据或执行验收：'+str(status.get('error') or state))
            self.sleep(15)
        raise ValueError('该处理任务已被替代，结果未采用')

    def commit(self,row,run_id,pages,result,*,ocr=False):
        sid,uid=int(row['id']),int(row['user_id'])
        with self.library.submission_lock(sid):
            if not self.library.claim_finalization(sid,run_id):
                return
            current=self.library.get_submission(sid)
            written=self.write(current,pages,result=result,preserve_bibliographic=ocr)
            if ocr:
                self.library.record_ocr_usage(uid,sid,sum(bool(p.get('text')) for p in pages),run_id=run_id)
            self.library.set_progress(sid,written,int(current.get('page_count') or written))
            self.finalize(sid,written)
            self.library.complete_processing_run(sid,run_id)

    def run(self,sid,run_id,*,resume=False,reattach=False):
        row=self.library.get_submission(sid)
        if not row or not self.library.processing_run_current(sid,run_id):
            return
        uid=int(row['user_id'])
        try:
            if resume or reattach:
                result={}
                total=int(row.get('page_count') or 0)
                needs_ocr=True
                pages=[]
            else:
                result=self.store.parse(uid,sid)
                total=int(result.get('pages') or row.get('page_count') or 0)
                with self.library.submission_lock(sid):
                    if not self.library.processing_run_current(sid,run_id):
                        return
                    self.library.set_page_count(sid,total)
                pages=[] if result.get('needs_ocr') else self.store.fetch_text(uid,sid)
                needs_ocr=bool(result.get('needs_ocr') or self.corpus.assess_text_layer_quality(pages).get('requires_ocr'))
            if needs_ocr:
                with self.library.submission_lock(sid):
                    if not self.library.processing_run_current(sid,run_id):
                        return
                    self.library.set_page_count(sid,total,source_kind='scanned')
                if not row.get('ocr_consent'):
                    self.commit(row,run_id,pages,result)
                    return
                self.wait(row,run_id,total,start=not reattach)
                pages=self.store.fetch_text(uid,sid,run_id=run_id)
                self.commit(row,run_id,pages,result,ocr=True)
            else:
                with self.library.submission_lock(sid):
                    if not self.library.processing_run_current(sid,run_id):
                        return
                    self.library.set_page_count(sid,total,source_kind='text_layer')
                self.commit(row,run_id,pages,result)
        except Exception as exc:
            self.library.fail_processing_run(sid,run_id,str(exc))

    def reconcile(self):
        rows={int(row['id']):row for row in self.library.list_resumable()+self.library.list_processing_runs()}
        for row in rows.values():
            sid=int(row['id'])
            run_id=row.get('processing_run_id') or ''
            if run_id and row.get('processing_run_state')=='active' and row.get('source_kind')=='scanned' and row.get('processing_run_kind')!='offline':
                threading.Thread(target=self.run,args=(sid,run_id),kwargs={'reattach':True},
                    name=f'mylib-watch-{sid}',daemon=True).start()
            elif run_id:
                self.library.fail_processing_run(sid,run_id,'上次处理未完整结束，需管理员核对；未自动重启或验收')
            else:
                self.library.set_status(sid,'failed',fail_reason='旧解析任务已中断；需使用已单独授权的断点续跑，未自动恢复或验收')
