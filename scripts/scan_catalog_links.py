"""Full offline HTML link/anchor scan with exact case-sensitive path matching."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import posixpath
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import html

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import Catalog, canonical


def parse(path):
    raw = path.read_bytes()
    try:
        tree = html.fromstring(raw.decode('utf-8-sig'))
    except UnicodeDecodeError:
        tree = html.fromstring(raw)
    ids = Counter()
    links = []
    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue
        names = {element.get('id')}
        if element.tag.lower() == 'a':
            names.add(element.get('name'))
            if element.get('href') is not None:
                links.append((element.get('href'), element.sourceline))
        ids.update(n for n in names if n)
    return ids, links


def scan(root):
    catalog = Catalog(root)
    files = set(catalog.manifest['files'])
    documents = {}
    issues = []
    count = 0
    for relative in sorted(files):
        if relative.startswith(('static_library/', 'stream_library/')) and Path(relative).suffix.lower() in ('.html', '.htm', '.shtml'):
            try:
                documents[relative] = parse(catalog.root / relative)
            except Exception as exc:
                issues.append({'file': relative, 'kind': 'parse_failure', 'target': '', 'detail': str(exc)})
    for relative, (ids, links) in documents.items():
        for anchor, occurrences in ids.items():
            if occurrences > 1:
                issues.append({'file': relative, 'kind': 'duplicate_anchor', 'target': anchor, 'count': occurrences})
        for href, line in links:
            count += 1
            try:
                url = urlsplit(href.replace('\\', '/'))
            except ValueError:
                issues.append({'file': relative, 'kind': 'invalid_url', 'target': href, 'line': line})
                continue
            if url.scheme or url.netloc or url.path.startswith('/'):
                continue  # Application routes/external links require HTTP validation separately.
            target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), unquote(url.path))) if url.path else relative
            if target not in files:
                issues.append({'file': relative, 'kind': 'missing_file', 'target': href, 'line': line})
            elif url.fragment and target in documents and unquote(url.fragment) not in documents[target][0]:
                issues.append({'file': relative, 'kind': 'missing_anchor', 'target': href, 'line': line})
    return {'catalog': catalog.version, 'documents': len(documents), 'links': count,
            'counts': dict(Counter(i['kind'] for i in issues)), 'issues': issues}


def regressions(before, after):
    def counts(report):
        return Counter((i['file'], i['kind'], i['target']) for i in report['issues'])
    return [{'file': f, 'kind': k, 'target': t, 'new_occurrences': n}
            for (f, k, t), n in (counts(after) - counts(before)).items()]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    result = scan(args.root)
    if args.baseline:
        result['new_errors'] = regressions(json.loads(args.baseline.read_text(encoding='utf-8')), result)
    args.output.write_bytes(canonical(result))
    print(json.dumps({k: v for k, v in result.items() if k not in ('issues',)}, ensure_ascii=True))
    if result.get('new_errors'):
        raise SystemExit(1)
