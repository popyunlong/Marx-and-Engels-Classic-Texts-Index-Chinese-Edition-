"""Explicit one-shot publication of the new user-supplied JSON source batch.

The stopped RapidOCR/Luna queue and its quality hold are never consumed or
modified. All existing data, locks, baseline checks and rollback are retained.
"""
from pathlib import Path
import argparse
import json
import shutil
import sqlite3
import sys
import time
import contextlib
import socket
import urllib.request
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from ingestion.store import Store,sha256
from ingestion.publisher import Publisher,publication_lock,atomic_json,command
from ingestion.idle_stop import stop_idle_executor
from ingestion.scheduler import Scheduler,YieldRequired

DATA=Path('/home/data/marx-economics28-paddle')


class PaddlePublisher(Publisher):
    candidate_readiness_timeout=300
    main_readiness_attempts=150

    def health(self, port):
        elapsed=super().health(port)
        available=next(int(l.split()[1]) for l in Path('/proc/meminfo').read_text().splitlines()
                       if l.startswith('MemAvailable:'))
        # Resource pressure rejects the trial, but must not prevent checking the
        # old endpoint while routing traffic back during rollback.
        if port==self.port and available<768*1024:
            raise RuntimeError('候选运行期间可用内存低于安全余量')
        if time.monotonic()>=getattr(self, 'next_resource_record', 0):
            atomic_json(DATA/'publication-health.json',dict(at=time.time(),port=port,
                available_mib=available//1024,health_seconds=elapsed))
            self.next_resource_record=time.monotonic()+30
        return elapsed

    def require_executor(self, port):
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/runtime', timeout=5) as response:
            payload=json.load(response)
        if not payload.get('ok') or payload.get('ingestion_worker')!={'alive':True,'enabled':True}:
            raise YieldRequired('接管任务的内置执行器尚未正常运行')

    def worker_memory_sample(self):
        return getattr(self, 'measured_worker_memory', None) or super().worker_memory_sample()

    def stop_idle_unit(self, unit):
        pid=command('systemctl','show',unit,'-p','MainPID','--value')
        if not pid.isdigit() or int(pid)<=0:
            raise YieldRequired('无法确认执行器进程，暂不回收')
        owner=f'{socket.gethostname()}:{pid}'
        def stop():
            if command('systemctl','show',unit,'-p','MainPID','--value')!=pid:
                raise YieldRequired('执行器已更换进程，重新核实后再回收')
            command('systemctl','stop',unit,timeout=5)
        stop_idle_executor(self.scheduler.citation_db.parent,owner,stop)

    @contextlib.contextmanager
    def pause_duplicate(self):
        unit='marx-search-citation-worker.service'
        self.require_executor(8000)
        self.measured_worker_memory=super().worker_memory_sample()
        if not self.measured_worker_memory.isdigit():
            raise YieldRequired('独立执行器内存无法实测')
        atomic_json(DATA/'worker-handoff.json',dict(stage='stopping_duplicate',
            measured_memory_bytes=int(self.measured_worker_memory),at=time.time()))
        try:
            self.stop_idle_unit(unit)
            self.require_executor(8000)
            atomic_json(DATA/'worker-handoff.json',dict(stage='embedded_executor_serving',
                measured_memory_bytes=int(self.measured_worker_memory),at=time.time()))
            yield
        finally:
            # No enablement/configuration changes: restore the original worker.
            # Rollback may retain a candidate for user stages; don't force another
            # full corpus into that constrained state.
            candidate=command('systemctl','show',self.unit,'-p','ActiveState','--value')
            if candidate not in ('active','activating'):
                command('systemctl','start','--no-block',unit)
                atomic_json(DATA/'worker-handoff.json',dict(stage='standalone_restart_requested',at=time.time()))
            else:
                atomic_json(DATA/'worker-handoff.json',dict(stage='restore_after_candidate_retirement',at=time.time()))

    def restart_main(self):
        deadline=time.monotonic()+120
        while True:
            self.require_executor(self.port)
            self.drain(8000)
            try:
                self.stop_idle_unit('marx-search.service')
                break
            except YieldRequired:
                if time.monotonic()>=deadline:raise
                time.sleep(2)
        command('systemctl','start','--no-block','marx-search.service')


def load_packages():
    manifest=json.loads((DATA/'packages-manifest.json').read_text())
    allowed=json.loads((DATA/'pdf-manifest.json').read_text())['files']
    allowed={b['sha256']:b for b in allowed}
    if len(manifest['books'])!=28:raise ValueError('Expected exactly28 input packages')
    packages=[]
    for row in manifest['books']:
        path=DATA/'packages'/(row['id']+'.json')
        if sha256(path)!=row['package_sha256']:raise ValueError('Package hash mismatch')
        package=json.loads(path.read_text());digest=package['source_sha256']
        if digest not in allowed or package['page_count']!=allowed[digest]['pages']:raise ValueError('PDF allowlist mismatch')
        if package.get('economics28'):raise ValueError('Old model review must not be reused')
        if any(p['checked'] for p in package['pages']):raise ValueError('Imported OCR must not claim full-text review')
        if package['paddle_source']['json_sha256']!=sha256(DATA/'raw-json'/(row['id']+'.json')):raise ValueError('Raw OCR hash mismatch')
        if [p['page'] for p in package['pages']]!=list(range(1,package['page_count']+1)):raise ValueError('Incomplete page sequence')
        if not package['toc'] or any(not 1<=t['pdf_page']<=package['page_count'] for t in package['toc']):raise ValueError('Invalid TOC destination')
        packages.append(package)
    if len({p['source_sha256'] for p in packages})!=28:raise ValueError('Duplicate source package')
    return packages,allowed


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prepare-only',action='store_true');args=parser.parse_args()
    packages,allowed=load_packages();store=Store(DATA/'publication-store')
    batch=store.batch('经济学28册（用户PaddleOCR底本）','economics28-paddle-20260909')
    with store.connect() as c:
        for package in packages:
            b=allowed[package['source_sha256']]
            c.execute("INSERT OR IGNORE INTO books(id,batch,sha,name,size,path,pages,status,created) VALUES(?,?,?,?,?,?,?,'ready',?)",
                      (package['book_id'],batch,b['sha256'],b['name'],b['bytes'],'/home/data/pdfs/自动入库/'+b['sha256']+'.pdf',b['pages'],time.time()))
    scheduler=Scheduler(store,Path('/var/www/.marx_search_full/citation_assistant.sqlite3'),health_url='http://127.0.0.1:8000/api/runtime')
    pub=PaddlePublisher(store,scheduler)
    if shutil.disk_usage('/home/data').free<12*1024**3+3*(pub.app_root/'data/corpus.sqlite').stat().st_size:raise YieldRequired('Candidate and rollback disk reserve unavailable')
    # Scheduler enforces the existing quiet period and user-job priority.
    start=time.monotonic()
    while True:
        scheduler.tick()
        try:scheduler.check_quiet();break
        except YieldRequired:
            if time.monotonic()-start>60:raise
            time.sleep(5)
    with publication_lock(Path('/home/data/marx-search-data/corpus-publish.lock')), contextlib.ExitStack() as legacy_locks:
        for path in [Path('/home/data/marx-search-corpus-repair/promotion.lock'),
                     Path('/home/data/marx-search-new-corpus-202609/promotion.lock')]:
            if path.parent.is_dir():legacy_locks.enter_context(publication_lock(path))
        if pub.journal.exists():
            previous=json.loads(pub.journal.read_text())
            if previous['phase'] not in ('published','rolled_back'):raise YieldRequired('Another release is active')
            pub.retire_candidate(previous)
        if args.prepare_only:
            from ingestion.candidate import build_candidate
            directory=pub.root/('paddle28-'+str(int(time.time())))
            record=build_candidate(packages,pub.app_root,directory,pub.checkpoint,unit_checkpoint=pub.admit_unit)
            pub.prepare_runtime(directory,record)
            atomic_json(DATA/'prepared.json',dict(directory=str(directory),record=record,stage='built_not_serving'))
            print(json.dumps(dict(stage='candidate_built',directory=str(directory))))
        else:
            prepared=json.loads((DATA/'prepared.json').read_text())['directory'] if (DATA/'prepared.json').exists() else None
            with pub.pause_duplicate():
                try:
                    pub.publish(packages,prepared=prepared)
                finally:
                    if pub.journal.exists():
                        state=json.loads(pub.journal.read_text())
                        if state['phase']=='rolled_back':pub.retire_candidate(state)
            atomic_json(DATA/'published.json',dict(at=time.time(),books=28,pages=sum(p['page_count'] for p in packages)))


if __name__=='__main__':
    try:main()
    except YieldRequired as exc:
        atomic_json(DATA/'waiting.json',dict(at=time.time(),reason=str(exc),production_preserved=True))
        print(json.dumps(dict(stage='waiting',reason=str(exc)),ensure_ascii=False));sys.exit(75)
