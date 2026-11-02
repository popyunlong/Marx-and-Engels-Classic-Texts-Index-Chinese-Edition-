"""Single durable worker, cgroup-limited separately from storage and its API."""
from __future__ import annotations

import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .layout import validate_folios
from .queue import Queue
from .storage import Storage

GIB=1024**3


def available_bytes():
    values=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
    return int(values['MemAvailable'].strip().split()[0])*1024


def group_peak():
    try:
        group=next(line.split('::',1)[1] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        root=Path('/sys/fs/cgroup')/group.lstrip('/')
        peak=root/'memory.peak'
        return int((peak if peak.exists() else root/'memory.current').read_text())
    except (OSError,ValueError,StopIteration):
        return 0


def verify_budget(root=None):
    """Refuse model work outside the installed, effective worker cgroup budget."""
    if root is None:
        group=next(line.split('::',1)[1] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        root=Path('/sys/fs/cgroup')/group.lstrip('/')
    root=Path(root)
    maximum=int((root/'memory.max').read_text())
    high=int((root/'memory.high').read_text())
    swap=int((root/'memory.swap.max').read_text())
    if not (0<high<=3*GIB and high<=maximum<=4*GIB and swap==0):
        raise RuntimeError('Effective OCR cgroup memory budget is missing or unsafe')


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)


def run_page(pdf,page,directory,*,memory=available_bytes,timeout=180,stopping=lambda:False):
    with tempfile.TemporaryDirectory(prefix='page-',dir=directory) as temp:
        output=Path(temp)/'result.json'
        # The OCR child gets no storage/API credentials and can only read its local PDF.
        allowed={'PATH','PYTHONPATH','PYTHONHOME','PYTHONDONTWRITEBYTECODE','LANG','LC_ALL','TMPDIR','TMP','TEMP','SystemRoot','WINDIR'}
        env={k:v for k,v in os.environ.items() if k in allowed}
        env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
        process=subprocess.Popen([sys.executable,'-m','mylib_ocr.page','--pdf',str(pdf),'--page',str(page),'--output',str(output)],
                                 env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        started=time.monotonic()
        try:
            while process.poll() is None:
                if stopping() or time.monotonic()-started>timeout or memory()<GIB:
                    raise RuntimeError('OCR page interrupted by stop/time/memory budget; no automatic retry')
                time.sleep(.25)
            if process.returncode!=0:
                raise RuntimeError(f'OCR page process exited ({process.returncode}); no automatic retry')
            if not output.exists() or output.stat().st_size>8*1024*1024:
                raise RuntimeError('Missing or oversized page result')
            result=json.loads(output.read_text(encoding='utf-8'))
            if int(result.get('page',0))!=page:
                raise RuntimeError('Unexpected result page')
            if result.get('layout_schema')!=3 or not isinstance(result.get('text'),str):
                raise RuntimeError('Unexpected result schema')
            return result
        finally:
            stop_child(process)


def results_payload(pages,total):
    candidates={int(p['page']):p.get('folio_candidates',[]) for p in pages}
    printed,quality=validate_folios(candidates,total)
    confidence=[float(b.get('confidence',0)) for p in pages for b in p.get('layout_blocks',[]) if b.get('confidence')]
    quality['ocr']={'engine':'rapidocr-layout-v3','pages_with_text':sum(bool(p.get('text')) for p in pages),
                    'total_pages':total,'coverage':round(sum(bool(p.get('text')) for p in pages)/max(1,total),4),
                    'median_line_confidence':round(statistics.median(confidence),4) if confidence else 0.0}
    output=[]
    for page in pages:
        value=dict(page,printed_pages=printed.get(int(page['page']),[]))
        if value.get('text'):
            output.append(json.dumps(value,ensure_ascii=False))
    return '\n'.join(output).encode('utf-8'),quality


def process_job(queue,storage,job,*,page_runner=run_page,memory=available_bytes,stopping=lambda:False,sleep=time.sleep):
    try:
        pdf=storage.source(job)
        # Check the page range without rendering or importing the OCR model.
        import fitz
        with fitz.open(pdf) as doc:
            if len(doc)!=job['total']:
                raise ValueError('Admitted page count does not match the complete original PDF')
        for page in storage.legacy_pages(job):
            queue.save_page(job,page)  # Idempotent even if importing was interrupted previously.
        done={int(p['page']) for p in queue.pages(job)}
        for page in range(1,job['total']+1):
            if page in done:
                continue
            waiting=time.monotonic()
            while memory()<5*GIB or not queue.dispatch_enabled():
                if stopping() or time.monotonic()-waiting>1800:
                    raise RuntimeError('Resource/admission wait expired; explicit recovery required')
                sleep(2)
            if stopping():
                raise RuntimeError('OCR worker stopping')
            try:
                value=page_runner(pdf,page,queue.root,stopping=stopping)
            except Exception as exc:
                raise RuntimeError(f'Page {page}: {exc}') from exc
            queue.save_page(job,value)  # Includes successful empty text pages; persisted before next page.
        payload,quality=results_payload(queue.pages(job),job['total'])
        storage.publish_result(job,payload)
        queue.terminal(job['run_id'],'done',peak_bytes=group_peak())
    except Exception as exc:
        queue.terminal(job['run_id'],'interrupted',str(exc),group_peak())
        queue.set_dispatch(False)  # One failure stops the following books as well.


def reconcile_startup(queue):
    # Even a queued-only predecessor must never cause startup to dispatch work.
    queue.set_dispatch(False)
    queue.recover_running()


def main():
    import fcntl
    root=Path(os.environ.get('MYLIB_OCR_ROOT','/opt/mylib-ocr/shared'))
    queue=Queue(root)
    with (root/'worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        reconcile_startup(queue)
        verify_budget()
        stopping=False
        def stop(*_):
            nonlocal stopping
            stopping=True
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        storage=Storage(root)
        while not stopping:
            job=queue.claim() if queue.dispatch_enabled() else None
            if job:
                process_job(queue,storage,job,stopping=lambda:stopping)
            else:
                time.sleep(1)


if __name__=='__main__':
    main()
