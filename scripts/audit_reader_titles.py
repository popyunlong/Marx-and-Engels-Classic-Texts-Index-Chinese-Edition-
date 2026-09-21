"""Audit all registered reader titles without loading page text or changing data."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from book_config import load_book_configs
from search import Corpus
from volume_presentation import reader_title


def audit(database: Path | None = None) -> dict:
    corpus = object.__new__(Corpus)
    corpus.book_configs = load_book_configs()
    corpus._manifest_by_file = {}
    corpus._files_by_book_volume = {}
    corpus._load_manifest()
    configs = {b.key: b for b in corpus.book_configs}
    sources = dict(corpus._manifest_by_file)
    indexed = set()
    if database:
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            for book, volume, source in conn.execute('SELECT DISTINCT book, volume, source_file FROM pages'):
                indexed.add(source)
                sources.setdefault(source, dict(book=book, volume=volume, display_title=Path(source).stem))
    rows, errors = [], []
    titles = defaultdict(list)
    noise = re.compile(r'(?i)(?:[a-f0-9]{32,}|\bOCR\b|z-lib|zlibrary|\.pdf(?:$|\s))')
    for source, meta in sorted(sources.items()):
        cfg = configs.get(meta['book'])
        if cfg is None:
            errors.append(dict(source_file=source, reason='missing_book_config'))
            continue
        before = meta.get('display_title', '')
        result = reader_title(cfg, int(meta['volume']), before)
        row = dict(book=cfg.key, volume=meta['volume'], source_file=source,
                   public=cfg.available, indexed=source in indexed,
                   before=before, before_tab=Path(source).name,
                   after=result.title, volume_label=result.volume_label)
        rows.append(row)
        titles[(cfg.key, result.title)].append(source)
        if not result.book_title.strip('《》 ') or noise.search(result.title):
            errors.append(dict(source_file=source, reason='invalid_title', title=result.title))
        if (cfg.single_volume or meta['volume'] in cfg.unnumbered_volumes) and result.volume_label:
            errors.append(dict(source_file=source, reason='unexpected_volume_label'))
    for (book, title), files in titles.items():
        if len(files) > 1:
            errors.append(dict(book=book, title=title, reason='duplicate_title', sources=files))
    return dict(books=len({r['book'] for r in rows}), volumes=len(rows),
                public_volumes=sum(r['public'] for r in rows),
                indexed_volumes=sum(r['indexed'] for r in rows),
                changed_headings=sum(r['before'] != r['after'] for r in rows),
                errors=errors, rows=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'rows'}, ensure_ascii=True))
    raise SystemExit(bool(report['errors']))
