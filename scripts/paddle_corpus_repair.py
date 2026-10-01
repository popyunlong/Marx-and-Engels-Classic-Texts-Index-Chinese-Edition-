#!/usr/bin/env python3
"""Candidate-only Paddle OCR worker. Never starts or modifies the website."""
from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import math
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import threading
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paddle_repair import (Queue, PRIMARY_MODEL, GEOMETRY_MODEL, canonical, digest,
    file_hash, readonly, safe_path, prioritized_pages, write_evidence, parse_layout,
    parse_lines, assess_change, visible, HealthGate, validate_baseline, assert_capacity)

BEIJING = timezone(timedelta(hours=8))
BASE_URL = 'https://paddleocr.aistudio-app.com'


class ProviderError(RuntimeError):
    def __init__(self, message, *, uncertain=False, fatal=False):
        super().__init__(message)
        self.uncertain, self.fatal = uncertain, fatal


class Paddle:
    def __init__(self, token, base=BASE_URL):
        if not token:
            raise ProviderError('Paddle credential is missing', fatal=True)
        if base.rstrip('/') != BASE_URL:
            raise ValueError('unreviewed provider origin')
        self.token, self.base = token, base.rstrip('/')

    def request(self, path, data=None, content_type=None):
        headers = {'Authorization': 'Bearer ' + self.token, 'User-Agent': 'marx-paddle-repair/1'}
        if content_type:
            headers['Content-Type'] = content_type
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read(4 * 1024**2))
            return payload.get('data', payload)
        except urllib.error.HTTPError as exc:
            # Never place provider bodies, authorization or signed URLs in logs.
            raise ProviderError('provider HTTP ' + str(exc.code),
                uncertain=data is not None and exc.code not in {400,401,403,404,413,422,429},
                fatal=exc.code in {400,401,403,404,413,422,429}) from None
        except (OSError, ValueError) as exc:
            raise ProviderError('provider transport/format error: ' + type(exc).__name__, uncertain=data is not None) from None

    @staticmethod
    def batch_id(request_key):
        if len(request_key) != 64 or any(c not in '0123456789abcdef' for c in request_key):
            raise ValueError('invalid persisted request fingerprint')
        return 'marx-' + request_key

    def inspect_batch(self, request_key):
        # A batch labels a request; it is not an idempotency guarantee. Never POST
        # again because a lookup is empty, unavailable, delayed or ambiguous.
        batch_id = self.batch_id(request_key)
        result = self.request('/api/v2/ocr/jobs/batch/' + batch_id)
        if not isinstance(result, dict) or result.get('batchId') != batch_id:
            raise ProviderError('batch identity mismatch', fatal=True)
        rows = result.get('extractResult')
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get('jobId') for r in rows):
            raise ProviderError('invalid batch task identities', fatal=True)
        # Exclude signed result URLs and provider bodies from operator output.
        return [{'jobId': str(r['jobId']), 'state': str(r.get('state', ''))} for r in rows]

    def submit(self, image, model, request_key):
        boundary = 'marx-' + digest(image)[:24]
        options = {'useDocOrientationClassify': False, 'useDocUnwarping': False, 'visualize': False}
        if model == PRIMARY_MODEL:
            options.update(useLayoutDetection=True, prettifyMarkdown=False, temperature=0.0)
        chunks = []
        for name, value in [('model', model), ('batchId', self.batch_id(request_key)),
                            ('optionalPayload', canonical(options).decode())]:
            chunks.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"\r\n\r\n' + value + '\r\n').encode())
        chunks += [('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="page.png"\r\nContent-Type: image/png\r\n\r\n').encode(),
                   image, ('\r\n--' + boundary + '--\r\n').encode()]
        result = self.request('/api/v2/ocr/jobs', b''.join(chunks), 'multipart/form-data; boundary=' + boundary)
        job_id = result.get('jobId') or result.get('id')
        if not job_id:
            raise ProviderError('submission returned no task identity', uncertain=True)
        return str(job_id)

    def finish(self, job_id):
        start, delay = time.monotonic(), 3
        while time.monotonic() - start < 900:
            result = self.request('/api/v2/ocr/jobs/' + urllib.parse.quote(job_id, safe=''))
            state = str(result.get('state') or result.get('status') or '').lower()
            if state in {'failed','error','cancelled','canceled'}:
                raise ProviderError('remote job failed')
            if state in {'done','succeeded','success','completed'}:
                url = result.get('resultJsonUrl') or result.get('resultUrl')
                if isinstance(url, dict):
                    url = url.get('jsonUrl') or url.get('url')
                return self.download(url)
            time.sleep(delay)
            delay = min(12, delay * 1.5)
        raise ProviderError('remote job polling timed out; retain task identity')

    @staticmethod
    def download(url):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        parsed = urllib.parse.urlsplit(url or '')
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderError('invalid result origin')
        for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(item[4][0]).is_global:
                raise ProviderError('nonpublic result origin')
        try:
            with urllib.request.build_opener(NoRedirect).open(url, timeout=90) as response:
                data = response.read(32 * 1024**2 + 1)
        except Exception as exc:
            raise ProviderError('result download failed: ' + type(exc).__name__) from None
        if len(data) > 32 * 1024**2:
            raise ProviderError('oversized result')
        return data


def probe(base_url, started):
    sample = {'at': datetime.now(timezone.utc).isoformat(), 'elapsed': time.monotonic()-started, 'probes': []}
    for path in ['/api/runtime','/','/login']:
        begin = time.monotonic()
        try:
            request=urllib.request.Request(base_url.rstrip('/') + path,
                headers={'User-Agent':'marx-paddle-repair-health/1.0','Cache-Control':'no-cache'})
            with urllib.request.urlopen(request, timeout=8) as response:
                data = response.read(4 * 1024**2)
                item = {'path': path, 'status': response.status, 'ok': response.status == 200}
            if path == '/api/runtime':
                body = json.loads(data)
                item['ok'] = bool(item['ok'] and body.get('ok') and body.get('search_enabled') and body.get('pdf_enabled') and body.get('layout_exact_ready'))
                sample['release'] = body.get('app_release', {}).get('id')
                sample['catalog'] = body.get('catalog_release')
        except Exception as exc:
            item = {'path': path, 'ok': False, 'error': type(exc).__name__}
        item['seconds'] = time.monotonic()-begin
        sample['probes'].append(item)
    return sample


def resource_guard(root):
    if Path('/opt/marx-search').exists():
        raise RuntimeError('OCR is forbidden on the production application host')
    if not Path('/home/data').is_mount() or not root.resolve().is_relative_to(Path('/home/data')):
        raise RuntimeError('dedicated data mount is required')
    if root.stat().st_dev != Path('/home/data').stat().st_dev:
        raise RuntimeError('worker root is not on the data disk')
    disk = shutil.disk_usage(root)
    assert_capacity(disk.total, disk.free)
    available = next(int(s.split()[1])*1024 for s in Path('/proc/meminfo').read_text().splitlines() if s.startswith('MemAvailable:'))
    if available < 2 * 1024**3 or os.getloadavg()[0] > 4:
        raise RuntimeError('resource pressure')
    if (root/'PAUSE').exists():
        raise RuntimeError('operator pause')
    group = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split('::',1)[1].lstrip('/')
    quota, period = (group/'cpu.max').read_text().split()
    memory = (group/'memory.max').read_text().strip()
    if quota == 'max' or int(quota)/int(period)>2 or memory=='max' or int(memory)>3*1024**3:
        raise RuntimeError('worker resource limits are missing')


def render_page(pdf, number):
    # Called only by the scheduler thread; PyMuPDF documents are not shared.
    import fitz
    with fitz.open(pdf) as doc:
        page = doc[number-1]
        image = page.get_pixmap(matrix=fitz.Matrix(200/72, 200/72), alpha=False).tobytes('png')
        lines = []
        for block in page.get_text('rawdict', flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
            for line in block.get('lines', []):
                chars = [c for span in line.get('spans', []) for c in span.get('chars', [])]
                if not chars:
                    continue
                def box(rect):
                    r = fitz.Rect(rect) * page.rotation_matrix
                    return [r.x0/page.rect.width, r.y0/page.rect.height, r.x1/page.rect.width, r.y1/page.rect.height]
                lines.append({'text': ''.join(c['c'] for c in chars), 'bbox': box(line['bbox']),
                              'chars': [{'text': c['c'], 'bbox': box(c['bbox'])} for c in chars], 'confidence': 1.0})
        return image, lines


def fetch_job(queue_path, root, page_id, image, pdf_sha, model, budget, stop=None):
    if page_id in quarantined_pages(root, allow_submitting=True):
        raise ProviderError('page permanently quarantined; submission forbidden', fatal=True)
    queue = Queue(queue_path)
    client = Paddle(os.environ.get('PADDLEOCR_ACCESS_TOKEN', '').strip())
    page = queue.conn.execute('SELECT * FROM pages WHERE page_id=?', (page_id,)).fetchone()
    key = digest(canonical([page_id, page['baseline_hash'], pdf_sha, digest(image), model, 'options-v1']))
    job = queue.prepare_job(page_id, model, key)
    try:
        if job['state'] == 'done':
            path = safe_path(root, job['raw_path'])
            if file_hash(path) != job['raw_hash']:
                raise ValueError('raw evidence hash mismatch')
            return path.read_bytes()
        if job['state'] == 'planned':
            if stop is not None and stop.is_set():
                raise ProviderError('scheduler paused before submission', fatal=True)
            try:
                queue.reserve(page_id, model, datetime.now(BEIJING).date().isoformat(), budget)
            except ValueError as exc:
                if str(exc) == 'daily budget exhausted':
                    if stop is not None:
                        stop.set()
                    raise ProviderError(str(exc), fatal=True) from None
                raise
            try:
                job_id = client.submit(image, model, key)
            except ProviderError as exc:
                queue.transition(page_id, model, 'uncertain' if exc.uncertain else 'failed', error=str(exc))
                if (exc.fatal or exc.uncertain) and stop is not None:
                    stop.set()
                raise
            queue.transition(page_id, model, 'submitted', remote_id=job_id)
        elif job['state'] == 'submitted' and job['remote_id']:
            job_id = job['remote_id']
        else:
            if job['state'] == 'uncertain':
                if stop is not None:
                    stop.set()
                raise ProviderError('uncertain submission requires reconciliation', fatal=True)
            raise ValueError('job needs reconciliation: ' + job['state'])
        raw = client.finish(job_id)
        rel = 'raw/' + key + '.jsonl'
        raw_hash = write_evidence(root/rel, raw)
        queue.transition(page_id, model, 'done', raw_path=rel, raw_hash=raw_hash, error='')
        return raw
    finally:
        queue.conn.close()


def process_page(queue_path, root, page, image, native, pdf_sha, old, budget, stop=None):
    primary = fetch_job(queue_path, root, page['page_id'], image, pdf_sha, PRIMARY_MODEL, budget, stop)
    result = parse_layout(primary)
    result.update(page_id=page['page_id'], source_file=page['source_file'], pdf_page=page['pdf_page'],
        book=page['book'], volume=page['volume'], printed_page=page['printed_page'],
        baseline_hash=page['baseline_hash'], pdf_sha256=pdf_sha, image_sha256=digest(image),
        primary_raw_hash=digest(primary), change=assess_change(old, result['text']), geometry=[],
        processing_parameters={'dpi':200,'options_version':1,'orientation':False,'unwarping':False})
    if visible(''.join(line['text'] for line in native)) == visible(result['text']):
        result['geometry'], result['geometry_precision'] = native, 'character'
    else:
        secondary = fetch_job(queue_path, root, page['page_id'], image, pdf_sha, GEOMETRY_MODEL, budget, stop)
        rows = parse_lines(secondary)
        geometry = []
        for row in rows:
            points = row['polygon']
            if not isinstance(points, list) or len(points) != 4 or any(len(p)!=2 for p in points):
                raise ValueError('invalid line polygon')
            xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
            box = [min(xs)/result['width'], min(ys)/result['height'], max(xs)/result['width'], max(ys)/result['height']]
            if not all(0 <= v <= 1 for v in box) or row['confidence'] < 0 or row['confidence'] > 1:
                raise ValueError('invalid line coordinates or confidence')
            geometry.append({'text': row['text'], 'bbox': box, 'confidence': row['confidence'], 'precision': 'line'})
        result['secondary_raw_hash'] = digest(secondary)
        result['secondary_text'] = '\n'.join(r['text'] for r in geometry)
        # Whole-page agreement is deliberately strict; mismatches require review.
        result['geometry_aligned'] = visible(result['secondary_text']) == visible(result['text'])
        result['geometry'], result['geometry_precision'] = geometry, 'line'
    result.setdefault('geometry_aligned', True)
    result['review_required'] = True
    return result


def plan(args):
    root = args.root.resolve()
    snapshot = json.loads((root/'source/manifest.json').read_text('utf-8'))
    database = root/'source/corpus.sqlite'
    if file_hash(database) != snapshot['corpus_sha256']:
        raise ValueError('source snapshot fingerprint mismatch')
    queue = Queue(root/'jobs'/args.queue)
    hints_file=root/'source/priority-hints.json'
    hints=json.loads(hints_file.read_text('utf-8')) if hints_file.exists() else {}
    queue.bind(snapshot)
    policy_hash=file_hash(hints_file) if hints_file.exists() else 'none'
    previous=queue.conn.execute("SELECT value FROM meta WHERE key='priority_hints_sha256'").fetchone()
    if previous and previous[0]!=policy_hash:
        raise ValueError('priority evidence changed; create a new queue')
    with queue.conn:
        queue.conn.execute("INSERT OR IGNORE INTO meta VALUES('priority_hints_sha256',?)",(policy_hash,))
    with readonly(database) as conn:
        geometry=root/'source/geometry.sqlite'
        if geometry.exists():
            expected=snapshot.get('files',{}).get('geometry.sqlite')
            if not expected or file_hash(geometry)!=expected:
                raise ValueError('source geometry fingerprint mismatch')
            with readonly(geometry) as g:
                ready=dict(g.execute("SELECT source_file,count(*) FROM geometry_pages WHERE status='ready' GROUP BY source_file"))
            for source,total in conn.execute('SELECT source_file,count(*) FROM pages GROUP BY source_file'):
                hints.setdefault(source,{})['missing_geometry_ratio']=max(0,1-ready.get(source,0)/total)
        rows = prioritized_pages(conn, per_group=args.per_group, limit=args.limit,priority_hints=hints)
        queue.plan(database, rows)
    print(json.dumps(queue.status(), ensure_ascii=False))


def quarantined_pages(root, *, allow_submitting=False):
    """An explicit frozen authorization isolates a lost response, never clears it.

    Inspect every queue so creating another queue cannot bypass an unknown POST.
    The exception covers the exact persisted request only; any new uncertainty
    still stops work. A quarantined page is excluded for every model and image.
    """
    policy_path = root/'source/uncertain-quarantine.json'
    entries, snapshot = [], None
    if policy_path.exists():
        stat = policy_path.stat()
        if policy_path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o222:
            raise ValueError('quarantine authorization must be root-owned and frozen')
        policy = json.loads(policy_path.read_text('utf-8'))
        manifest = root/'source/manifest.json'
        if (policy.get('schema_version') != 1 or
                policy.get('source_manifest_sha256') != file_hash(manifest) or
                policy.get('action') != 'exclude_page_permanently_continue_others' or
                not isinstance(policy.get('authorization'), str) or not policy['authorization'].strip()):
            raise ValueError('invalid quarantine authorization or source identity')
        snapshot = json.loads(manifest.read_text('utf-8'))
        entries = policy.get('requests')
        required = {'page_id','model','request_key','baseline_hash','source_file','pdf_page','pdf_sha256','reason'}
        if (not isinstance(entries, list) or not entries or
                any(not isinstance(e, dict) or not required <= e.keys() or not e['reason'] for e in entries)):
            raise ValueError('invalid quarantined request identities')
        if len({e['page_id'] for e in entries}) != len(entries):
            raise ValueError('duplicate quarantined page')
        for entry in entries:
            if snapshot.get('pdfs',{}).get(entry['source_file'],{}).get('sha256') != entry['pdf_sha256']:
                raise ValueError('quarantined PDF identity mismatch')
    matched = set()
    for path in sorted((root/'jobs').glob('*.sqlite')):
        if path.is_symlink():
            raise ValueError('queue symlink is forbidden')
        conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                continue  # Shared account budget has no request table.
            rows = conn.execute("SELECT j.*,p.source_file,p.pdf_page,p.baseline_hash FROM jobs j LEFT JOIN pages p USING(page_id) WHERE j.state IN ('uncertain','submitting')").fetchall()
            for row in rows:
                if allow_submitting and row['state'] == 'submitting':
                    continue  # Another thread may currently await its first POST.
                entry = next((e for e in entries if e['request_key'] == row['request_key']), None)
                source = conn.execute("SELECT value FROM meta WHERE key='source'").fetchone()
                if (not entry or row['state'] != 'uncertain' or row['remote_id'] or
                        not source or json.loads(source[0]) != snapshot or
                        any(row[k] != entry[k] for k in ('page_id','model','request_key','baseline_hash','source_file','pdf_page'))):
                    raise ProviderError('uncertain submission requires reconciliation before any new jobs', fatal=True)
                matched.add(entry['page_id'])
        finally:
            conn.close()
    if matched != {e['page_id'] for e in entries}:
        raise ValueError('quarantine evidence is missing; retain the original uncertain job')
    return matched


def eligible_pages(conn, total, excluded, limit):
    # Count the approved scope AFTER permanent exclusions, before completion
    # filtering. Restarts cannot accumulate extra pages beyond that same scope.
    placeholders = ','.join('?' for _ in excluded) or 'NULL'
    return deque_rows(conn.execute(
        "SELECT * FROM (SELECT * FROM pages WHERE page_id NOT IN ("+placeholders+") ORDER BY priority LIMIT ?) "
        "WHERE state IN ('planned','awaiting_retry') ORDER BY priority LIMIT ?",
        (*sorted(excluded), total, limit))) if excluded else deque_rows(conn.execute(
        "SELECT * FROM (SELECT * FROM pages ORDER BY priority LIMIT ?) WHERE state IN ('planned','awaiting_retry') ORDER BY priority LIMIT ?",
        (total,limit)))


def run_worker(args):
    import fcntl
    root = args.root.resolve()
    resource_guard(root)
    lock = (root/'jobs/worker.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    queue = Queue(root/'jobs'/args.queue)
    queue.recover()
    excluded = quarantined_pages(root)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    snapshot = json.loads(queue.conn.execute("SELECT value FROM meta WHERE key='source'").fetchone()[0])
    scope = json.loads((root/'scope.json').read_text('utf-8'))
    if scope.get('phase') not in {'smoke','pilot','bulk'} or scope.get('daily_budget',0) < args.daily_budget:
        raise ValueError('operator scope does not authorize this run')
    cap = {'smoke':12,'pilot':300,'bulk':166952}[scope['phase']]
    if not 1 <= scope.get('max_total_pages',0) <= cap:
        raise ValueError('invalid approved scope')
    database = root/'source/corpus.sqlite'
    if file_hash(database) != snapshot['corpus_sha256']:
        raise ValueError('source snapshot changed')
    started, baseline_samples = time.monotonic(), []
    report = root/'reports'/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    report.mkdir()
    if args.health_observation_file and not args.baseline_file:
        raise ValueError('relayed observations require a matching protected baseline')
    with (report/'health.jsonl').open('a', encoding='utf-8') as log:
        baseline = None
        if args.baseline_file:
            path=args.baseline_file
            if not path.resolve().is_relative_to((root/'source').resolve()):
                raise ValueError('operator baseline must be in the protected source area')
            baseline=operator_baseline(path,snapshot)
            sample=probe(args.health_url,started)
            if (sample.get('release')!=snapshot['app_release']['id'] or sample.get('catalog')!=snapshot['catalog_release']
                    or any(not p.get('ok') for p in sample['probes'])):
                raise RuntimeError('website changed or unavailable after the operator baseline')
            log.write(canonical(sample).decode()+'\n');log.flush()
            write_evidence(report/'reused-baseline.json',{'sha256':file_hash(path),'path':str(path)})
        while baseline is None:
            if stop.is_set():
                raise RuntimeError('stopped during baseline')
            resource_guard(root)
            sample = probe(args.health_url, started)
            if sample.get('release') != snapshot['app_release']['id'] or sample.get('catalog') != snapshot['catalog_release']:
                raise RuntimeError('website baseline changed or unavailable')
            log.write(canonical(sample).decode()+'\n'); log.flush(); baseline_samples.append(sample)
            if sample['elapsed'] - baseline_samples[0]['elapsed'] >= 1800:
                baseline = validate_baseline(baseline_samples)
                break
            time.sleep(30)
        write_evidence(report/'baseline.json', baseline)
        def current_health():
            if args.health_observation_file:
                return operator_observation(root,args.health_observation_file,baseline,time.monotonic()-started)
            return probe(args.health_url,started)
        if args.health_observation_file:
            sample=current_health()
            if any(not p['ok'] for p in sample['probes']):
                raise RuntimeError('website probe failed before new jobs')
            log.write(canonical(sample).decode()+'\n');log.flush()
        gate, last_probe = HealthGate(baseline), time.monotonic()
        pages = eligible_pages(queue.conn, scope['max_total_pages'], excluded, args.max_pages)
        write_evidence(report/'quarantine.json', {'excluded_page_ids':sorted(excluded),
            'authorization_sha256':file_hash(root/'source/uncertain-quarantine.json') if excluded else None,
            'eligible_scope_pages':scope['max_total_pages'],'unknown_jobs_preserved':True})
        running, pdf_hashes, stopped = {}, {}, ''
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool, readonly(database) as source:
            while pages or running:
                try:
                    resource_guard(root)
                except (RuntimeError, ValueError) as exc:
                    stopped = str(exc)
                if stop.is_set():
                    stopped = stopped or 'scheduler pause requested'
                if time.monotonic()-last_probe >= 30:
                    try:
                        sample = current_health()
                    except (ValueError,OSError,KeyError,TypeError) as exc:
                        stopped = stopped or 'health observation unavailable: '+type(exc).__name__
                    else:
                        log.write(canonical(sample).decode()+'\n');log.flush()
                        stopped = stopped or gate.observe(sample)
                    last_probe=time.monotonic()
                if stopped:
                    stop.set()
                while pages and len(running) < args.concurrency and not stopped:
                    page = pages.popleft()
                    pdf = safe_path(root/'source', page['source_file'])
                    expected = snapshot['pdfs'].get(page['source_file'], {}).get('sha256')
                    if not expected or not pdf.exists():
                        with queue.conn:
                            queue.conn.execute("UPDATE pages SET state='awaiting_retry',error='source PDF not provisioned' WHERE page_id=?", (page['page_id'],))
                        continue
                    if page['source_file'] not in pdf_hashes:
                        pdf_hashes[page['source_file']] = file_hash(pdf)
                    if pdf_hashes[page['source_file']] != expected:
                        raise ValueError('source PDF changed')
                    old = source.execute('SELECT raw_text FROM pages WHERE id=?',(page['page_id'],)).fetchone()[0]
                    if digest(old) != page['baseline_hash']:
                        raise ValueError('page baseline changed')
                    image, native = render_page(pdf, page['pdf_page'])
                    future = pool.submit(process_page, queue.path, root, page, image, native, expected, old, args.daily_budget, stop)
                    running[future] = page
                if not running:
                    break
                done, _ = concurrent.futures.wait(running, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    page = running.pop(future)
                    try:
                        result = future.result()
                        queue.record_result(page['page_id'], root/'geometry'/(str(page['page_id'])+'-'+digest(canonical(result))+'.json'), result)
                    except Exception as exc:
                        message = str(exc) if isinstance(exc, (ProviderError, ValueError)) else type(exc).__name__
                        with queue.conn:
                            queue.conn.execute("UPDATE pages SET state='awaiting_retry',error=? WHERE page_id=?", (message, page['page_id']))
                        if isinstance(exc, ProviderError) and (exc.fatal or exc.uncertain):
                            stopped = message
                            stop.set()
                    print(json.dumps({'page_id': page['page_id'], 'status': queue.status(), 'paused_reason': stopped}, ensure_ascii=False),flush=True)
        write_evidence(report/'completion.json', {'status': queue.status(), 'paused_reason': stopped, 'production_writes': 0})


def deque_rows(cursor):
    from collections import deque
    return deque(dict(r) for r in cursor)


def operator_baseline(path, snapshot, now=None):
    """Reuse a frozen operator observation, retaining the original stop limits."""
    stat = path.stat()
    if path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o222:
        raise ValueError('baseline must be root-owned and frozen')
    payload = json.loads(path.read_text('utf-8'))
    rows, original = payload['samples'], payload['original']
    fresh = validate_baseline(rows)
    now = now or datetime.now(timezone.utc)
    # mtime and now are both on the receiving node; the operator records the
    # observation's existing age plus a transfer allowance when publishing it.
    published_age=payload['observation_age_at_publish_seconds']
    if isinstance(published_age,bool) or not isinstance(published_age,(int,float)) or not math.isfinite(published_age) or published_age<0:
        raise ValueError('invalid observation age')
    age = now.timestamp()-stat.st_mtime+published_age
    if not 0 <= age < 600:
        raise ValueError('operator baseline expired')
    if any(not 0 < b['elapsed']-a['elapsed'] <= 60 for a,b in zip(rows,rows[1:])):
        raise ValueError('operator baseline monitoring gap')
    if any(base.get('release') != snapshot['app_release']['id'] or base.get('catalog') != snapshot['catalog_release']
           for base in (fresh,original)):
        raise ValueError('operator baseline belongs to another production version')
    if (set(original.get('p95',{}))!=set(fresh['p95']) or
            any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<0 for v in original['p95'].values())):
        raise ValueError('invalid original baseline thresholds')
    if any(value>original['p95'][path]*1.2 and value>original['p95'][path]+.2
           for path,value in fresh['p95'].items()):
        raise ValueError('latency has not recovered within original thresholds')
    return original


def operator_observation(root,path,baseline,elapsed,now=None):
    """Compare production loopback samples to production loopback thresholds.

    A root operator relays observations, not credentials, onto the data node.
    If the observer disappears or the proof changes origin/version, fail closed.
    """
    if not path.resolve().is_relative_to((root/'source').resolve()):
        raise ValueError('health observation must be in the protected source area')
    stat=path.stat()
    if path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o222:
        raise ValueError('health observation must be root-owned and frozen')
    payload=json.loads(path.read_text('utf-8'))
    age_at_publish=payload['observation_age_at_publish_seconds']
    if (isinstance(age_at_publish,bool) or not isinstance(age_at_publish,(int,float)) or
            not math.isfinite(age_at_publish) or age_at_publish<0):
        raise ValueError('invalid health observation age')
    age=(now or datetime.now(timezone.utc)).timestamp()-stat.st_mtime+age_at_publish
    if not 0 <= age < 75:
        raise ValueError('health observation expired; stop new jobs')
    if (payload.get('measurement_origin') != 'production-loopback:8000' or
            payload.get('baseline_sha256') != digest(canonical(baseline))):
        raise ValueError('health observation measurement or baseline mismatch')
    sample=payload['sample']
    if sample.get('release')!=baseline['release'] or sample.get('catalog')!=baseline['catalog']:
        raise ValueError('health observation production version changed')
    rows=sample['probes']
    if (not isinstance(rows,list) or len(rows)!=len(baseline['p95']) or
            {p.get('path') for p in rows}!=set(baseline['p95']) or
            any(not isinstance(p.get('ok'),bool) or isinstance(p.get('seconds'),bool) or
                not isinstance(p.get('seconds'),(int,float)) or not math.isfinite(p['seconds']) or p['seconds']<0 for p in rows)):
        raise ValueError('invalid health observation probes')
    return {**sample,'elapsed':elapsed,'measurement_origin':payload['measurement_origin']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan','run','status','review'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--queue', default='pilot.sqlite')
    parser.add_argument('--per-group', type=int)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--max-pages', type=int, default=12)
    parser.add_argument('--concurrency', type=int, choices=[2,4,8], default=2)
    parser.add_argument('--daily-budget', type=int, default=18000)
    parser.add_argument('--health-url', default='https://mazhuzuojiansuo.com')
    parser.add_argument('--baseline-file', type=Path)
    parser.add_argument('--health-observation-file', type=Path)
    parser.add_argument('--review-file', type=Path)
    args=parser.parse_args(argv)
    if Path(args.queue).name != args.queue or not 1 <= args.daily_budget <= 18000:
        raise ValueError('invalid queue or unapproved daily budget; higher quotas need a reviewed configuration change')
    if args.command=='plan':
        plan(args)
    elif args.command=='run':
        run_worker(args)
    else:
        queue=Queue(args.root/'jobs'/args.queue)
        if args.command=='review':
            review=json.loads(args.review_file.read_text('utf-8'))
            for item in review:
                queue.review(**item)
        print(json.dumps(queue.status(),ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
