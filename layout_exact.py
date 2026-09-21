"""Read-only, versioned layout projection. Never opens a PDF or builds an index.

Offsets remain canonical corpus offsets. The UTF-32 projection and run table are
memory mapped; only matches crossing an evidenced deletion are supplemental.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import mmap
import os
import struct
import threading
import time
from pathlib import Path

RUN = struct.Struct('<QQQ')  # projected character start, canonical start, length
VERSION = 1


def volume_fingerprint(corpus, vol):
    h = hashlib.sha256()
    for p in vol.pages:
        h.update(json.dumps([p.pdf_page, p.printed_page, p.raw_text, p.norm_text],
                            ensure_ascii=False, separators=(',', ':')).encode())
    h.update(json.dumps(corpus._chapter_segments(vol), ensure_ascii=False,
                        sort_keys=True, separators=(',', ':')).encode())
    return h.hexdigest()


class Projection:
    def __init__(self, root, entry):
        self.entry = entry
        self.handles = []
        self.maps = []
        with (root / (entry['id'] + '.text')).open('rb') as handle:
            self.text = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            self.maps.append(self.text)
        # A compact run table is small; bytes avoid an extra descriptor for
        # every volume while retaining 24-byte records rather than Python tuples.
        self.runs = (root / (entry['id'] + '.runs')).read_bytes()
        self.count = len(self.runs) // RUN.size

    def run(self, i):
        return RUN.unpack_from(self.runs, i * RUN.size)

    def run_at(self, pos):
        lo, hi = 0, self.count
        while lo < hi:
            mid = (lo + hi) // 2
            if self.run(mid)[0] <= pos:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    def spans(self, start, length):
        end = start + length
        out = []
        i = self.run_at(start)
        if i < 0:
            return []
        covered = 0
        while i < self.count:
            ps, original, size = self.run(i)
            if ps >= end:
                break
            a, b = max(ps, start), min(ps + size, end)
            if a < b:
                out.append((original + a - ps, original + b - ps))
                covered += b - a
            i += 1
        return out if covered == length else []

    def find(self, query, deadline):
        needle = query.encode('utf-32-le')
        start = 0
        last_yield = time.monotonic()
        emitted = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('layout scan budget exceeded')
            # Bounded C searches avoid holding the GIL over a whole large volume.
            boundary = min(len(self.text), start + 256 * 1024)
            pos = self.text.find(needle, start, min(len(self.text), boundary + len(needle) - 4))
            if pos < 0:
                if boundary >= len(self.text):
                    break
                start = boundary
            else:
                start = pos + 4
                if pos % 4 == 0:
                    spans = self.spans(pos // 4, len(query))
                    if spans and spans[-1][1] - spans[0][0] > len(query):
                        emitted += 1
                        if emitted > 10000:
                            raise TimeoutError('layout candidate budget exceeded')
                        yield {'start': spans[0][0], 'end': spans[-1][1],
                               'spans': spans, 'ref': f"{self.entry['id']}:{pos // 4}:{len(query)}",
                               'types': sorted({r['kind'] for r in self.entry['removed']
                                                if spans[0][0] <= r['start'] < spans[-1][1]})}
            if time.monotonic() - last_yield >= .004:
                time.sleep(.001)
                last_yield = time.monotonic()

    def close(self):
        for m in self.maps:
            m.close()
        for h in self.handles:
            h.close()


class LayoutIndex:
    """Construct once at corpus startup; configured but invalid indices fail closed."""
    def __init__(self, corpus, root=None):
        self.projections = {}
        self.error = ''
        self.revision = ''
        if not root and not os.environ.get('MARX_LAYOUT_EXACT_DIR'):
            from build_index import _EXEDIR
            pointer = Path(_EXEDIR) / 'config/layout_exact_runtime.json'
            if pointer.is_file():
                try:
                    root = json.loads(pointer.read_text('utf-8'))['directory']
                except Exception:
                    root = pointer  # configured but invalid: fail closed below
        self.enabled = bool(root or os.environ.get('MARX_LAYOUT_EXACT_DIR'))
        self.lock = threading.Lock()
        self.cache = {}
        if not self.enabled:
            return
        root = Path(root or os.environ['MARX_LAYOUT_EXACT_DIR'])
        try:
            manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
            self.revision = hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()
            if manifest['version'] != VERSION:
                raise ValueError('unsupported layout index version')
            for sf, entry in manifest['volumes'].items():
                if entry.get('supported') is False:
                    continue
                vol = corpus.get_volume_by_source_file(sf)
                if vol is None or volume_fingerprint(corpus, vol) != entry['fingerprint']:
                    raise ValueError('layout corpus/TOC fingerprint mismatch')
                if 'pdf_stat' in entry:
                    from build_index import _EXEDIR
                    stat = (Path(_EXEDIR) / sf).stat()
                    if [stat.st_size, stat.st_mtime_ns] != entry['pdf_stat']:
                        raise ValueError('layout PDF fingerprint requires revalidation')
                if not all(c in '0123456789abcdef' for c in entry['id']) or len(entry['id']) != 64:
                    raise ValueError('invalid projection id')
                for suffix in ('text', 'runs'):
                    path = root / (entry['id'] + '.' + suffix)
                    h = hashlib.sha256()
                    with path.open('rb') as handle:
                        for block in iter(lambda: handle.read(1024 * 1024), b''):
                            h.update(block)
                    if h.hexdigest() != entry[suffix + '_sha256']:
                        raise ValueError('layout artifact checksum mismatch')
                self.projections[sf] = Projection(root, entry)
        except Exception as exc:
            self.error = str(exc)
            for p in self.projections.values():
                p.close()
            self.projections.clear()

    def scan(self, query, volumes, semaphore):
        key = (query, tuple(v.source_file for v in volumes))
        with self.lock:
            cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.error:
            return {}, False, self.error
        if not self.enabled:
            return {}, True, ''
        if not semaphore.acquire(blocking=False):
            return {}, False, 'layout scan busy'
        found = {}
        try:
            deadline = time.monotonic() + 3
            for vol in volumes:
                projection = self.projections.get(vol.source_file)
                if projection:
                    found[vol.source_file] = list(projection.find(query, deadline))
            result = (found, True, '')
            if sum(len(matches) for matches in found.values()) <= 2000:
                with self.lock:
                    if len(self.cache) >= 32:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[key] = result
            return result
        except Exception as exc:
            return found, False, str(exc)
        finally:
            semaphore.release()

    def resolve(self, source_file, reference, query=None):
        projection = self.projections.get(source_file)
        if not projection:
            return None
        try:
            identity, offset, length = reference.split(':')
            offset, length = int(offset), int(length)
            if identity != projection.entry['id'] or offset < 0 or not 1 <= length <= 4096:
                return None
            text = projection.text[offset * 4:(offset + length) * 4].decode('utf-32-le')
            if len(text) != length or '\0' in text or (query is not None and text != query):
                return None
            spans = projection.spans(offset, length)
            if not spans:
                return None
            return spans
        except (ValueError, UnicodeError):
            return None
