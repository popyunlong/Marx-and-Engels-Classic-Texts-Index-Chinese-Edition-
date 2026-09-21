"""Offline layout sidecar builder. Run in an isolated, resource-limited worker.

Canonical SQLite/PDF inputs are read-only. Publication is a separate operation.
Ambiguous geometry/canonical alignment is retained, never guessed or corrected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from functools import lru_cache

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fitz
from build_index import normalize
from layout_exact import RUN, VERSION, volume_fingerprint

CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
norm_char = lru_cache(maxsize=16384)(normalize)


def geometry_lines(page):
    out = []
    for block in page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
        for line in block.get('lines', []):
            spans = line.get('spans', [])
            text = ''.join(s['text'] for s in spans)
            if normalize(text):
                out.append({'text': text, 'norm': normalize(text), 'box': line['bbox'],
                            'size': max((s['size'] for s in spans), default=0)})
    return out


def raw_mapping(raw, canonical):
    parts = [norm_char(c) for c in raw]
    if ''.join(parts) != canonical:
        return None
    offsets = [0]
    for part in parts:
        offsets.append(offsets[-1] + len(part))
    return offsets


def page_evidence(page, pdf_page, lines, neighbor_headers, titles):
    """Return canonical deletion spans plus evidence. No ordinary digit stripping."""
    raw, norm = page.raw_text, page.norm_text
    offsets = raw_mapping(raw, norm)
    if offsets is None:
        return [], (False, False)
    height = pdf_page.rect.height
    removed = []
    note_safe = True
    matched_lines = []
    for line in lines:
        n = line['norm']
        if norm.count(n) != 1:
            continue
        start = norm.find(n)
        matched_lines.append((start, start + len(n), line))
        box = line['box']
        edge = box[3] < height * .16 or box[1] > height * .90
        kind = ''
        if edge and page.printed_page and n == normalize(str(page.printed_page)):
            kind = 'printed_page_number'
        elif (box[3] < height * .16 and n in neighbor_headers and
              any(n == title or (len(n) >= 4 and n in title) for title in titles)):
            kind = 'running_header'
        if kind:
            removed.append({'start': start, 'end': start + len(n), 'kind': kind,
                            'evidence': {'pdf_page': page.pdf_page, 'box': list(box), 'text': line['text']}})

    # A note must be geometrically distinct, start with the same circled marker,
    # and have an editorial note payload. Literal circled lists are not notes.
    notes = [line for line in lines if re.match(r'^\s*[' + CIRCLED + r']', line['text'])
             and line['box'][1] > height * .42
             and ('编者' in line['text'] or '手稿' in line['text'] or '原文' in line['text'])]
    # Extraction can put the marker on its own line. Pair only a nearby marker
    # and explanatory text at the same smaller-font note region.
    for line in lines:
        if line['text'].strip() not in CIRCLED or len(line['text'].strip()) != 1:
            continue
        if line['box'][1] <= height * .42:
            continue
        if any(abs(other['box'][1] - line['box'][1]) < 20 and
               any(t in other['text'] for t in ('编者', '手稿', '原文')) for other in lines):
            notes.append(line)
    body_sizes = [l['size'] for l in lines if height * .18 < l['box'][1] < height * .45]
    body_size = sorted(body_sizes)[len(body_sizes) // 2] if body_sizes else 0
    notes = [l for l in notes if body_size and l['size'] < body_size * .98]
    markers = {l['text'].strip()[0] for l in notes}
    if notes:
        note_y = min(l['box'][1] for l in notes)
        note_intervals = [(a, b, l) for a, b, l in matched_lines if note_y - 2 <= l['box'][1] < height * .90
                          and l['size'] < body_size * .99]
        region_lines = [l for l in lines if note_y - 2 <= l['box'][1] < height * .90]
        note_safe = all(any(l is matched for _, _, matched in note_intervals) for l in region_lines)
        if not note_safe and note_intervals:
            boundary = min(a for a, _, _ in note_intervals)
            removed.append({'start': boundary, 'end': boundary, 'kind': 'section_break',
                            'evidence': {'pdf_page': page.pdf_page, 'reason': 'uncertain note continuation'}})
        # Remove note text from BODY projection only. The canonical index retains
        # it, including the note labels. A tail that cannot be mapped fences pages.
        for a, b, line in note_intervals if note_safe else []:
            removed.append({'start': a, 'end': b, 'kind': 'footnote_region',
                            'evidence': {'pdf_page': page.pdf_page, 'box': list(line['box']), 'text': line['text']}})
        for i, char in enumerate(raw):
            if (char in markers and raw.count(char) >= 2 and
                    (i > 0 and raw[i - 1] not in '\r\n' and bool(normalize(raw[i - 1])))):
                removed.append({'start': offsets[i], 'end': offsets[i + 1], 'kind': 'footnote_anchor',
                                'evidence': {'pdf_page': page.pdf_page, 'raw_offset': i, 'marker': char}})
    # Continuity requires all meaningful extracted lines to map uniquely and a
    # body text layer. Corrected lines may differ from PDF and must fence joins.
    mapped_chars = sum(b - a for a, b, _ in matched_lines)
    body_lines = [l for l in lines if .16 * height < l['box'][1] < .92 * height and l not in notes]
    confident = bool(body_lines) and mapped_chars >= len(norm) * .90
    return removed, (confident, confident and note_safe)


def build_volume(corpus, vol, pdf_path, output):
    fingerprint = volume_fingerprint(corpus, vol)
    pdf_hash = hashlib.sha256()
    with pdf_path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            pdf_hash.update(block)
    identity = hashlib.sha256((str(VERSION) + hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + fingerprint + pdf_hash.hexdigest()).encode()).hexdigest()
    text_path, run_path = output / (identity + '.text'), output / (identity + '.runs')
    removed_all = []
    projected = 0
    last_page = None
    last_chapter = None
    last_confident = False
    titles = {normalize(t.title) for t in corpus.get_toc_entries(vol.source_file)}
    titles.add(normalize(vol.display_title))
    with fitz.open(pdf_path) as doc, text_path.open('wb') as text_file, run_path.open('wb') as run_file:
        line_cache = {}
        def get_lines(number):
            if 1 <= number <= len(doc):
                if number not in line_cache:
                    line_cache[number] = geometry_lines(doc[number - 1])
                return line_cache[number]
            return []
        for pi, page in enumerate(vol.pages):
            if not 1 <= page.pdf_page <= len(doc):
                last_confident = False
                continue
            pdf_page = doc[page.pdf_page - 1]
            neighbor_headers = {l['norm'] for n in (page.pdf_page - 2, page.pdf_page - 1, page.pdf_page + 1, page.pdf_page + 2)
                                for l in get_lines(n) if l['box'][3] < doc[n - 1].rect.height * .16}
            removed, confident = page_evidence(page, pdf_page, get_lines(page.pdf_page), neighbor_headers, titles)
            chapter = corpus.get_chapter_for_page(vol.source_file, page.pdf_page)
            chapter_id = (chapter.pdf_page, chapter.title) if chapter else None
            if not (last_confident and confident[0] and last_page == page.pdf_page - 1
                    and chapter_id is not None and last_chapter == chapter_id):
                text_file.write('\0'.encode('utf-32-le'))
                projected += 1
            intervals = sorted((r['start'], r['end']) for r in removed if r['end'] > r['start'])
            merged = []
            for a, b in intervals:
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            cursor = 0
            fences = {r['start'] for r in removed if r['kind'] == 'section_break'}
            for a, b in merged + [[len(page.norm_text), len(page.norm_text)]]:
                if cursor < a:
                    left = cursor
                    for right in sorted(f for f in fences if cursor <= f < a) + [a]:
                        part = page.norm_text[left:right]
                        if part:
                            text_file.write(part.encode('utf-32-le'))
                            run_file.write(RUN.pack(projected, vol.page_offsets[pi] + left, len(part)))
                            projected += len(part)
                        if right in fences:
                            text_file.write('\0'.encode('utf-32-le'))
                            projected += 1
                        left = right
                cursor = b
            for row in removed:
                removed_all.append({**row, 'start': row['start'] + vol.page_offsets[pi],
                                    'end': row['end'] + vol.page_offsets[pi]})
            last_page, last_chapter, last_confident = page.pdf_page, chapter_id, confident[1]
            line_cache = {n: ls for n, ls in line_cache.items() if n >= page.pdf_page - 2}
            if pi % 16 == 0:
                time.sleep(.001)
    # Detailed audit evidence stays offline; workers need only compact intervals.
    (output / (identity + '.evidence.json')).write_text(json.dumps(removed_all, ensure_ascii=False), 'utf-8')
    stat = pdf_path.stat()
    return {'id': identity, 'fingerprint': fingerprint, 'pdf_sha256': pdf_hash.hexdigest(),
            'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'supported': any(r['end'] > r['start'] for r in removed_all),
            'pdf_stat': [stat.st_size, stat.st_mtime_ns],
            'text_sha256': hashlib.sha256(text_path.read_bytes()).hexdigest(),
            'runs_sha256': hashlib.sha256(run_path.read_bytes()).hexdigest(),
            'removed': [{k: r[k] for k in ('start', 'end', 'kind')} for r in removed_all]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pdf-root', type=Path, default=Path('.'))
    parser.add_argument('--source', action='append', default=[])
    parser.add_argument('--reuse', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Use a new isolated output directory')
    args.output.mkdir(parents=True)
    os.environ.pop('MARX_LAYOUT_EXACT_DIR', None)
    from search import Corpus
    corpus = Corpus.load_default()
    manifest = {'version': VERSION, 'volumes': {}}
    reused = {}
    if args.reuse:
        previous = args.reuse / 'manifest.json'
        if not previous.exists():
            previous = args.reuse / 'manifest.partial.json'
        reused = json.loads(previous.read_text('utf-8')).get('volumes', {})
    for vol in corpus._volumes_by_source_file.values():
        if args.source and vol.source_file not in args.source:
            continue
        if not args.source and not corpus.get_book_config(vol.book).available:
            continue
        path = args.pdf_root / vol.source_file
        if not path.is_file():
            continue
        old = reused.get(vol.source_file)
        stat = path.stat()
        if (old and old.get('builder_sha256') == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                and old.get('fingerprint') == volume_fingerprint(corpus, vol)
                and old.get('pdf_stat') == [stat.st_size, stat.st_mtime_ns]):
            import shutil
            for suffix in ('text', 'runs', 'evidence.json'):
                source = args.reuse / (old['id'] + '.' + suffix)
                if suffix in ('text', 'runs'):
                    assert hashlib.sha256(source.read_bytes()).hexdigest() == old[suffix + '_sha256']
                shutil.copy2(source, args.output / source.name)
            manifest['volumes'][vol.source_file] = old
        else:
            manifest['volumes'][vol.source_file] = build_volume(corpus, vol, path, args.output)
        print(json.dumps({'source': vol.source_file, 'removed': len(manifest['volumes'][vol.source_file]['removed'])}, ensure_ascii=False), flush=True)
        pending = args.output / 'manifest.pending.json'
        pending.write_text(json.dumps(manifest, ensure_ascii=False), 'utf-8')
        pending.replace(args.output / 'manifest.partial.json')
    (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), 'utf-8')


if __name__ == '__main__':
    main()


