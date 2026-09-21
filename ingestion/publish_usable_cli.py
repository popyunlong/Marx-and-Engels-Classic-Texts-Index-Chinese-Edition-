"""Explicit one-shot publication of a prepared usable release, with normal gates."""
import argparse
import json
import hashlib
import time
from pathlib import Path

from .publisher import Publisher, publication_lock
from .scheduler import Scheduler, YieldRequired
from .store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('packages', nargs='+', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume-prepared', type=Path)
    parser.add_argument('--retry-yields', action='store_true', help='让行后自动等候重新准入；验收错误仍停止')
    args = parser.parse_args()
    packages = [json.loads(p.read_text(encoding='utf-8')) for p in args.packages]
    for package in packages:
        if package.get('release_quality') != 'provisional_text' or not package.get('toc_complete'):
            raise ValueError('缺少可用版本质量声明或完整目录验收')
        if any(p.get('mapping_basis') == 'needs_review' for p in package['pages']):
            raise ValueError('页码定位待处理')
    while True:
        try:
            attempt(args, packages)
            return
        except YieldRequired as exc:
            if not args.retry_yields:
                raise
            print(json.dumps({'yielded':str(exc),'at':time.time()},ensure_ascii=False),flush=True)
            time.sleep(2)


def attempt(args, packages):
    store = Store(Path('/home/data/marx-ingestion'))
    scheduler = Scheduler(store, Path('/var/www/.marx_search_full/citation_assistant.sqlite3'),
                          health_url='http://127.0.0.1:8000/api/runtime')
    deadline = time.monotonic() + 90
    while True:
        scheduler.tick()
        try:
            with scheduler.admit():
                break
        except YieldRequired:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    publisher = Publisher(store, scheduler)
    with publication_lock(Path('/home/data/marx-search-data/corpus-publish.lock')):
        if publisher.journal.exists():
            previous=json.loads(publisher.journal.read_text())
            if previous['phase'] not in ('published','rolled_back'):
                raise RuntimeError('存在未结束的发布记录，需要先恢复该版本')
            publisher.retire_candidate(previous)
            fingerprint=hashlib.sha256(json.dumps(packages,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            if previous['phase']=='published' and previous['record'].get('packages_sha256')==fingerprint:
                return  # A delayed executor retirement must not republish this batch.
        if args.prepare_only:
            from .candidate import build_candidate, validate_prepared
            directory = args.resume_prepared or publisher.root / ('usable-preflight-' + str(int(time.time())))
            if args.resume_prepared:
                if directory.resolve().parent != publisher.root.resolve():
                    raise ValueError('只能续用独立发布目录中的候选')
                record = validate_prepared(directory, publisher.app_root, packages, publisher.checkpoint)
            else:
                record = build_candidate(packages, publisher.app_root, directory, publisher.checkpoint, unit_checkpoint=publisher.admit_unit)
            publisher.prepare_runtime(directory, record)
            state = {'directory': str(directory), 'phase': 'preparing'}
            try:
                publisher.start_candidate(directory)
                print(json.dumps({'prepared': str(directory), 'record': record, 'health': publisher.health(publisher.port)}, ensure_ascii=False), flush=True)
            finally:
                publisher.retire_candidate(state)
        else:
            try:
                publisher.publish(packages, prepared=args.resume_prepared)
            except BaseException:
                if publisher.journal.exists():
                    publisher.retire_candidate(json.loads(publisher.journal.read_text()))
                raise


if __name__ == '__main__':
    main()
