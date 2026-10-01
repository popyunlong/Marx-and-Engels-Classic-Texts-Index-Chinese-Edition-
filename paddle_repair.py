"""Isolated, resumable OCR preparation. This module has no production-write API.

Raw provider results are immutable evidence. Acceptance and publication are
separate operations; a completed OCR job is never an approved corpus page.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath

PRIMARY_MODEL = 'PaddleOCR-VL-1.6'
GEOMETRY_MODEL = 'PP-OCRv6'
MARX = ('文集', '马恩选集', '资本论', '全集二版', '全集', '列宁全集')
CHINA = ('毛泽东选集', '毛泽东文集', '邓小平文选', '江泽民文选', '胡锦涛文选',
         '周恩来选集', '刘少奇选集', '陈云文集', '李大钊全集', '陈独秀文集')
XI = ('治国理政', '论中国共产党历史', '论党的宣传思想工作', '论党的自我革命',
      '论坚持党对一切工作的领导', '论把握新发展阶段、贯彻新发展理念、构建新发展格局',
      'user_rec_xi_culture_selected_2026', 'user_rec_zhijiang_xinyu_2007')
FIRST_VOLUMES = (('文集', 1), ('毛泽东选集', 1), ('邓小平文选', 3),
                 ('习近平著作选读', 1), ('治国理政', 5))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else str(value).encode('utf-8')).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def visible(text):
    # Preserve punctuation, digits, negatives and traditional characters.
    return re.sub(r'\s+', '', str(text))


def source_group(book):
    if book in MARX:
        return 'A'
    if book in CHINA:
        return 'B'
    if book.startswith('习近平') or book in XI:
        return 'C'
    if ('年谱' in book or book.startswith(('斯大林', '中共中央文件', '建党以来', '建国以来'))
            or book in ('十八大以来重要文献选编', '十九大以来重要文献选编', '二十大以来重要文献选编')):
        return 'D'
    return 'E'


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def safe_path(root, relative):
    p = PurePosixPath(relative)
    if not relative or p.is_absolute() or '..' in p.parts or '\\' in relative or ':' in relative:
        raise ValueError('unsafe relative path')
    target = Path(root).joinpath(*p.parts)
    if not target.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('path escapes workspace')
    return target


def write_evidence(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload if isinstance(payload, bytes) else canonical(payload)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('immutable evidence already exists with different content')
        return digest(data)
    # An exclusive create prevents silent overwrites after retries or races.
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        import os
        os.fsync(handle.fileno())
    return digest(data)


def prioritized_pages(conn, *, per_group=None, limit=None):
    volumes = defaultdict(list)
    for row in conn.execute('SELECT id,book,volume,source_file,pdf_page FROM pages ORDER BY source_file,pdf_page,id'):
        if source_group(row['book']) in 'ABC':
            volumes[(row['book'], row['volume'], row['source_file'])].append(dict(row))
    if per_group is not None:
        # Start with the five named priority volumes, spread through their pages,
        # then interleave A/B/C so even a 12-page smoke run covers all groups.
        samples = {}
        for group in 'ABC':
            keys = [k for k in sorted(volumes) if source_group(k[0]) == group]
            preferred = [k for k in keys if k[:2] in FIRST_VOLUMES]
            rows = [r for k in (preferred or keys) for r in volumes[k]]
            n = min(per_group, len(rows))
            samples[group] = [rows[round(i * (len(rows) - 1) / max(1, n - 1))] for i in range(n)]
        for i in range(max(map(len,samples.values()),default=0)):
            for group in 'ABC':
                if i<len(samples[group]):
                    yield samples[group][i]
        return
    first = sorted((k for k in volumes if k[:2] in FIRST_VOLUMES), key=lambda k: (FIRST_VOLUMES.index(k[:2]), k[2]))
    queues = {g: deque(sorted((k for k in volumes if source_group(k[0]) == g and k not in first),
              key=lambda k: ((MARX if g == 'A' else CHINA).index(k[0]) if g in 'AB' else 0, k))) for g in 'ABC'}
    order = list(first)
    while any(queues.values()):
        for g in 'ABC':
            if queues[g]:
                order.append(queues[g].popleft())
    emitted = 0
    for key in order:
        for row in volumes[key]:
            if limit is not None and emitted >= limit:
                return
            yield row
            emitted += 1


class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS pages(
            page_id INTEGER PRIMARY KEY,priority INTEGER NOT NULL,group_id TEXT NOT NULL,
            source_file TEXT NOT NULL,pdf_page INTEGER NOT NULL,book TEXT NOT NULL,
            volume INTEGER NOT NULL,printed_page TEXT,baseline_hash TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'planned',result_path TEXT NOT NULL DEFAULT '',
            result_hash TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS jobs(
            page_id INTEGER NOT NULL,model TEXT NOT NULL,request_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'planned',remote_id TEXT NOT NULL DEFAULT '',
            raw_path TEXT NOT NULL DEFAULT '',raw_hash TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL,
            PRIMARY KEY(page_id,model));
          CREATE TABLE IF NOT EXISTS usage(day TEXT,model TEXT,count INTEGER NOT NULL,
            PRIMARY KEY(day,model));
          CREATE TABLE IF NOT EXISTS reviews(page_id INTEGER PRIMARY KEY,
            result_hash TEXT NOT NULL,baseline_hash TEXT NOT NULL,decision TEXT NOT NULL,
            reviewer TEXT NOT NULL,evidence_json TEXT NOT NULL,created REAL NOT NULL);
        ''')

    def bind(self, manifest):
        value = canonical(manifest).decode('utf-8')
        previous = self.conn.execute("SELECT value FROM meta WHERE key='source'").fetchone()
        if previous and previous[0] != value:
            raise ValueError('stale source snapshot; create a new queue')
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES('source',?)", (value,))

    def plan(self, database, rows):
        source = readonly(database)
        try:
            with self.conn:
                for order, row in enumerate(rows):
                    p = source.execute('SELECT * FROM pages WHERE id=?', (row['id'],)).fetchone()
                    self.conn.execute('INSERT OR IGNORE INTO pages(page_id,priority,group_id,source_file,pdf_page,book,volume,printed_page,baseline_hash) VALUES(?,?,?,?,?,?,?,?,?)',
                        (p['id'], order, source_group(p['book']), p['source_file'], p['pdf_page'], p['book'], p['volume'], p['printed_page'], digest(p['raw_text'])))
        finally:
            source.close()

    def prepare_job(self, page_id, model, request_key):
        with self.conn:
            old = self.conn.execute('SELECT * FROM jobs WHERE page_id=? AND model=?', (page_id, model)).fetchone()
            if old and old['request_key'] != request_key:
                raise ValueError('request identity changed')
            self.conn.execute('INSERT OR IGNORE INTO jobs(page_id,model,request_key,updated) VALUES(?,?,?,?)',
                              (page_id, model, request_key, time.time()))
        return dict(self.conn.execute('SELECT * FROM jobs WHERE page_id=? AND model=?', (page_id, model)).fetchone())

    def reserve(self, page_id, model, day, quota):
        # All queues on this worker share one account budget. An interruption
        # may conservatively charge a request that never left; it cannot make a
        # request free or permit another queue to consume the website reserve.
        account = sqlite3.connect(self.path.with_name('provider-budget.sqlite'),timeout=30)
        account.execute('CREATE TABLE IF NOT EXISTS usage(day TEXT,model TEXT,count INTEGER NOT NULL,PRIMARY KEY(day,model))')
        account.commit()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            job = self.conn.execute('SELECT * FROM jobs WHERE page_id=? AND model=?', (page_id, model)).fetchone()
            if job is None or job['state'] != 'planned':
                raise ValueError('submission is not eligible; reconcile before resubmitting')
            account.execute('BEGIN IMMEDIATE')
            used = account.execute('SELECT count FROM usage WHERE day=? AND model=?', (day, model)).fetchone()
            if (used[0] if used else 0) >= quota:
                raise ValueError('daily budget exhausted')
            account.execute('INSERT INTO usage VALUES(?,?,1) ON CONFLICT(day,model) DO UPDATE SET count=count+1',(day,model))
            account.commit()
            self.conn.execute('INSERT INTO usage VALUES(?,?,1) ON CONFLICT(day,model) DO UPDATE SET count=count+1', (day, model))
            self.conn.execute("UPDATE jobs SET state='submitting',updated=? WHERE page_id=? AND model=?", (time.time(), page_id, model))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            account.close()

    def transition(self, page_id, model, state, **fields):
        allowed = {'remote_id', 'raw_path', 'raw_hash', 'error'}
        if set(fields) - allowed or state not in {'planned','submitting','submitted','done','failed','uncertain'}:
            raise ValueError('invalid job transition')
        with self.conn:
            self.conn.execute('UPDATE jobs SET state=?,updated=?' + ''.join(',' + k + '=?' for k in fields) + ' WHERE page_id=? AND model=?',
                              (state, time.time(), *fields.values(), page_id, model))

    def recover(self):
        with self.conn:
            self.conn.execute("UPDATE jobs SET state='uncertain',error='interrupted submission; reconcile remote task before retry' WHERE state='submitting' AND remote_id=''")

    def record_result(self, page_id, result_path, result):
        result_hash = write_evidence(result_path, result)
        with self.conn:
            self.conn.execute("UPDATE pages SET state='awaiting_review',result_path=?,result_hash=? WHERE page_id=?", (str(result_path), result_hash, page_id))

    def review(self, page_id, *, decision, reviewer, evidence):
        page = self.conn.execute('SELECT * FROM pages WHERE page_id=?', (page_id,)).fetchone()
        if not page or not page['result_hash'] or decision not in {'accepted','rejected','deferred'} or not reviewer.strip():
            raise ValueError('invalid review')
        if evidence.get('baseline_hash') != page['baseline_hash'] or evidence.get('result_hash') != page['result_hash']:
            raise ValueError('review evidence does not match this page version')
        if not evidence.get('reason') or (decision == 'accepted' and evidence.get('source_verified') is not True):
            raise ValueError('acceptance requires source verification')
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?,?,?)',
                 (page_id, page['result_hash'], page['baseline_hash'], decision, reviewer, canonical(evidence).decode(), time.time()))
            self.conn.execute('UPDATE pages SET state=? WHERE page_id=?', (decision, page_id))

    def status(self):
        return {'pages': dict(self.conn.execute('SELECT state,count(*) FROM pages GROUP BY state').fetchall()),
                'jobs': [dict(r) for r in self.conn.execute('SELECT model,state,count(*) AS count FROM jobs GROUP BY model,state')],
                'usage': [dict(r) for r in self.conn.execute('SELECT * FROM usage')], 'production_writes': 0}


def parse_layout(raw):
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    pages = [p for row in rows for p in row.get('result', {}).get('layoutParsingResults', [])]
    if len(pages) != 1 or any(row.get('errorCode', 0) for row in rows):
        raise ValueError('expected exactly one successful provider page')
    pr = pages[0].get('prunedResult') or {}
    width, height = float(pr.get('width', 0)), float(pr.get('height', 0))
    if not width > 0 or not height > 0:
        raise ValueError('provider coordinate dimensions missing')
    blocks = []
    for position, block in enumerate(pr.get('parsing_res_list') or []):
        box = block.get('block_bbox')
        text = str(block.get('block_content') or '')
        if not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box):
            raise ValueError('invalid block rectangle')
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError('block rectangle outside rendered page')
        blocks.append({'kind': str(block.get('block_label') or 'unknown'), 'text': text,
                       'box': [x0/width, y0/height, x1/width, y1/height],
                       'order': block.get('block_order', position), 'provider_position': position})
    if not blocks:
        raise ValueError('empty layout result requires visual review')
    # Retain every block, including notes/furniture, for auditable reconstruction.
    text_blocks = [b for b in blocks if b['kind'] not in {'image', 'chart'}]
    text = '\n\n'.join(b['text'] for b in text_blocks if b['text'].strip())
    if not visible(text):
        raise ValueError('no recognized text')
    return {'text': text, 'blocks': blocks, 'width': width, 'height': height,
            'geometry_precision': 'block', 'source_model': PRIMARY_MODEL}


def parse_lines(raw):
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    results = [p for r in rows for p in r.get('result', {}).get('ocrResults', [])]
    if len(results) != 1 or any(row.get('errorCode', 0) for row in rows):
        raise ValueError('expected one line OCR result')
    pr = results[0].get('prunedResult') or results[0]
    texts, boxes, scores = pr.get('rec_texts', []), pr.get('rec_polys', []), pr.get('rec_scores', [])
    if not texts or not len(texts) == len(boxes) == len(scores):
        raise ValueError('incomplete line OCR arrays')
    return [{'text': str(t), 'polygon': b, 'confidence': float(s)} for t,b,s in zip(texts,boxes,scores)]


def assess_change(old, new):
    left, right = visible(old), visible(new)
    if left == right:
        return {'state': 'unchanged', 'risks': [], 'ratio': 1.0}
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    risks = set()
    for tag,a,b,c,d in matcher.get_opcodes():
        if tag == 'equal':
            continue
        before, after = left[a:b], right[c:d]
        if re.search(r'\d|[〇零一二三四五六七八九十百千万亿]', before + after):
            risks.add('number')
        if any(x in before + after for x in '不未无非否勿'):
            risks.add('negation')
        if re.search(r'[①-⑳\[\]〔〕]', before + after):
            risks.add('note_marker')
        if max(len(before), len(after)) > 12:
            risks.add('large_change')
    if matcher.ratio() < .98:
        risks.add('substantial_difference')
    return {'state': 'review_required', 'risks': sorted(risks), 'ratio': matcher.ratio()}


def percentile95(values):
    values = sorted(values)
    return values[max(0, math.ceil(.95 * len(values)) - 1)]


def validate_baseline(samples, *, minimum_seconds=1800):
    rows = [r for r in samples if not r.get('skipped')]
    if len(rows) < 50 or rows[-1]['elapsed'] - rows[0]['elapsed'] < minimum_seconds:
        raise ValueError('30-minute healthy baseline is not complete')
    if any(not p.get('ok') for r in rows for p in r['probes']):
        raise ValueError('baseline includes failing probes')
    if len({r['release'] for r in rows}) != 1 or len({canonical(r.get('catalog')) for r in rows}) != 1:
        raise ValueError('production changed during baseline')
    values = defaultdict(list)
    for row in rows:
        for p in row['probes']:
            values[p['path']].append(p['seconds'])
    if not {'/api/runtime','/','/login'} <= values.keys() or any(len(v) != len(rows) for v in values.values()):
        raise ValueError('incomplete baseline probe coverage')
    return {'release': rows[0]['release'], 'catalog': rows[0]['catalog'],
            'p95': {k: percentile95(v) for k,v in values.items()}, 'samples': len(rows),
            'duration_seconds': rows[-1]['elapsed'] - rows[0]['elapsed']}


class HealthGate:
    def __init__(self, baseline):
        self.baseline = baseline
        self.failures = defaultdict(int)
        self.samples = []
        self.last_good = None

    def observe(self, sample):
        if sample.get('release') != self.baseline['release'] or sample.get('catalog') != self.baseline.get('catalog'):
            return 'production_version_changed'
        if sample.get('skipped'):
            return 'health_observation_unavailable'
        probes = {p['path']: p for p in sample.get('probes', [])}
        for path in self.baseline['p95']:
            p = probes.get(path, {})
            self.failures[path] = 0 if p.get('ok') else self.failures[path] + 1
            if self.failures[path] >= 2:
                return 'critical_probe_failed'
        now = sample['elapsed']
        self.samples.append(sample)
        self.samples = [r for r in self.samples if r['elapsed'] >= now - 600]
        for path, base in self.baseline['p95'].items():
            bad = 0
            for low, high in [(now-600, now-300), (now-300, now+0.001)]:
                values = [p['seconds'] for r in self.samples if low <= r['elapsed'] < high
                          for p in r['probes'] if p['path'] == path and p.get('ok')]
                if len(values) >= 10:
                    p95 = percentile95(values)
                    bad += p95 > base * 1.2 and p95 > base + .2
            if bad == 2:
                return 'sustained_latency_regression'
        if all(probes.get(p, {}).get('ok') for p in self.baseline['p95']):
            self.last_good = now
        return ''


def assert_capacity(total, free):
    if free < max(15 * 1024**3, math.ceil(total * .2)):
        raise ValueError('disk reserve below required minimum')
