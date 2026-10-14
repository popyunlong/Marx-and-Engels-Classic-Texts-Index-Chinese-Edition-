"""Resumable preparation only. Publication always belongs to release.ps1."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time

import personal_library as library
import personal_library_store as storage
import recommendations as jobs


def root():
    default = (Path('/home/data/marx-book-imports/recommendation-processing')
               if os.name == 'posix' else library.PERSONAL_LIB_DIR / 'recommendation_processing')
    return Path(os.environ.get('RECOMMENDATION_WORK_DIR', str(default)))


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.incoming')
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding='utf8')
    tmp.replace(path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def prepare(job):
    import fitz
    row = library.get_book_recommendation(job['recommendation_id'])
    if not row or row['sha256'] != job['sha256']:
        raise ValueError('荐书来源已变化')
    directory = root() / job['sha256']
    directory.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(directory).free < max(2 * row['byte_size'], 256 << 20) + (2 << 30):
        raise ValueError('荐书处理空间不足，原件仍保存在存储节点')
    pdf = directory / 'source.pdf'
    if not pdf.exists():
        blob, _ = storage.fetch_book_recommendation(row['user_id'], row['id'])
        if len(blob) != row['byte_size'] or hashlib.sha256(blob).hexdigest() != job['sha256']:
            raise ValueError('荐书原件指纹或大小不符')
        incoming = pdf.with_suffix('.incoming')
        incoming.write_bytes(blob)
        incoming.replace(pdf)
    if sha(pdf) != job['sha256']:
        raise ValueError('处理缓存与上传原件不符')
    with fitz.open(pdf) as doc:
        if len(doc) != row['page_count'] or doc.needs_pass:
            raise ValueError('荐书页数不符或 PDF 已加密')
        for number, page in enumerate(doc, 1):
            saved = directory / f'page-{number:05}.json'
            if saved.exists():
                cached = json.loads(saved.read_text('utf8'))
                if cached.get('source_sha256') != job['sha256'] or cached.get('page') != number:
                    raise ValueError('页面缓存身份不符')
            else:
                text = page.get_text(sort=True).strip()
                method = 'pdf_text'
                if len(text) < 40 and page.get_images():
                    python = os.environ.get('RECOMMENDATION_OCR_PYTHON', '/opt/marx-ingestion-ocr/.venv/bin/python')
                    if not Path(python).is_file():
                        raise ValueError(f'第 {number} 页需要 OCR，现有识别环境不可用')
                    result = subprocess.run([python, str(Path(__file__).resolve().parents[1] / 'scripts/recommendation_ocr.py'),
                                             str(pdf), str(number)], capture_output=True, text=True, timeout=75, check=True)
                    text = json.loads(result.stdout)['text']
                    method = 'reviewed_later_ocr'
                write_json(saved, dict(source_sha256=job['sha256'], page=number, text=text,
                                       label='', kind='body', checked=False, repairs=0, method=method))
            jobs.progress(job, number)
            time.sleep(.01)
        write_json(directory / 'draft.json', dict(source_sha256=job['sha256'],
                   recommendation_id=row['id'], title=row['title'], page_count=len(doc),
                   bookmarks=doc.get_toc(), status='needs_original_page_review'))
    jobs.progress(job, row['page_count'], state='review')


def run_once():
    job = jobs.claim()
    if not job:
        return False
    try:
        prepare(job)
    except Exception as exc:
        # Do not expose upstream URLs, credentials or raw HTTP responses.
        error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        jobs.progress(job, (jobs.get(job['sha256']) or {}).get('completed_pages', 0), state='failed',
                      error='准备失败，可重试：' + error[:300])
    return True


def consume(activation_file=None):
    while True:
        try:
            if activation_file is None or activation_file.exists():
                reconcile_published()
                run_once()
        except Exception as exc:
            # A revoked lease or transient DB error must not kill the consumer.
            logging.getLogger(__name__).warning('Recommendation consumer: %s', type(exc).__name__)
        time.sleep(10)


def committed_release(app, current_app, ledger):
    """A warm candidate or an uncommitted/rolled-back cutover is not published."""
    from book_data_release import metadata
    if not current_app.exists() or current_app.resolve() != app:
        return ''
    release = metadata(app).get('release_id')
    lines=ledger.read_text('utf8').splitlines() if ledger.exists() else []
    if not lines:
        return ''
    last = json.loads(lines[-1])
    if last.get('event') != 'promote' or last.get('release_id') != release:
        return ''
    return release or ''


def reconcile_published():
    """Record publication only after the live transaction has committed its ledger."""
    from app import _published_recommendation_sources
    app=Path(__file__).resolve().parents[1]
    release=committed_release(app,Path('/opt/marx-search/current/app'),Path('/opt/marx-search/release-ledger.jsonl'))
    if not release:
        return
    with library._connect() as c:
        jobs.setup(c)
        ready = {r['sha256']: dict(r) for r in c.execute("SELECT * FROM recommendation_publications WHERE state='ready'")}
    sources=_published_recommendation_sources()
    for digest,key in sources.items():
        job=ready.get(digest)
        if job and job['book_key']==key:
            jobs.mark_published(digest,key,release)
    jobs.register_historical(sources,release)

