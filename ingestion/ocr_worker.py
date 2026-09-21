"""Low-priority offline OCR executor. No model API credentials or paid calls."""
from __future__ import annotations
import argparse
import multiprocessing as mp
import time
import uuid
from pathlib import Path

from . import ocr_queue
from .scheduler import Scheduler, YieldRequired
from .store import Store


def child(pipe, model_root, path, page, threads):
    try:
        from .free_ocr import engine, render, recognize
        started=time.monotonic()
        ocr=engine(model_root,threads)
        result=recognize(ocr,render(path,page))
        result['unit_seconds']=time.monotonic()-started
        pipe.send({'ok':True,'result':result})
    except Exception as exc:
        pipe.send({'ok':False,'error':str(exc)[:200]})
    finally:
        pipe.close()


def run(store,scheduler,models,*,stop=None,once=False):
    owner='server-ocr-'+uuid.uuid4().hex[:12]
    ctx=mp.get_context('spawn'); active=None; next_tick=0
    try:
        while stop is None or not stop.is_set():
            now=time.monotonic()
            if now>=next_tick:
                scheduler.tick();next_tick=now+2
                store.set_state('free_ocr_worker',{'heartbeat':time.time(),'owner':owner,
                    'state':'正在免费识别' if active else scheduler.reason})
            if active:
                process,pipe,job,started=active; outcome=None
                if pipe.poll():
                    try:outcome=pipe.recv()
                    except EOFError:outcome={'ok':False,'error':'OCR 子进程退出'}
                elif now-started>=55 or not process.is_alive():
                    outcome={'ok':False,'error':'免费 OCR 单页达到 55 秒时限或进程退出，延后重试'}
                if outcome:
                    if process.is_alive():process.join(timeout=.05)
                    if process.is_alive():process.terminate()
                    process.join(timeout=1);pipe.close();active=None
                    if outcome['ok']:
                        try:
                            result=outcome['result']
                            ocr_queue.image_issued(store,job,result['image_sha256'])
                            ocr_queue.submit(store,job['book'],job['page'],job['token'],result)
                            ocr_queue.apply_ready(store)
                        except Exception as exc:ocr_queue.failed(store,job,str(exc))
                    else:ocr_queue.failed(store,job,outcome['error'])
                    if once:return
            if active is None:
                # A crash after saving OCR but before reconciliation is recoverable.
                ocr_queue.apply_ready(store)
                try:
                    with scheduler.admit():job=ocr_queue.claim(store,owner)
                    if job:
                        recv,send=ctx.Pipe(duplex=False)
                        process=ctx.Process(target=child,args=(send,models,job['path'],job['page'],1),daemon=True)
                        process.start();send.close();active=(process,recv,job,time.monotonic())
                except YieldRequired:pass
            time.sleep(.2)
    finally:
        if active:
            active[0].terminate();active[0].join(timeout=1);active[1].close()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,default=Path('/home/data/marx-ingestion'))
    p.add_argument('--models',type=Path,default=Path('/opt/marx-ingestion-ocr/models'))
    p.add_argument('--once',action='store_true')
    p.add_argument('--check',action='store_true',help='Verify runtime and models before admitting page work')
    args=p.parse_args()
    if args.check:
        from .free_ocr import engine
        import fitz
        from PIL import Image
        engine(args.models,1)
        print('Offline OCR runtime and model checks passed',flush=True)
        return
    store=Store(args.root)
    scheduler=Scheduler(store,Path('/var/www/.marx_search_full/citation_assistant.sqlite3'),health_url='http://127.0.0.1:8000/api/runtime')
    run(store,scheduler,args.models,once=args.once)


if __name__=='__main__':
    mp.freeze_support()
    main()
