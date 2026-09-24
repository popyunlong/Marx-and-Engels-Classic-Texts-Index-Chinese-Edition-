"""Apply only the first batch's reviewed changes to an offline snapshot."""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import Catalog, canonical, file_digest, inventory
from scripts.catalog_bundle import build, difference, groups

PDFS = {
    4: 'daa18017f299d4cba38ab34ee2e6c9360cd5b29296a8eb468906c4db1abaea5e',
    5: '3500f59c5c7e9b10bbf8b95db48987dbb89b47185139eb2530a0ef7e7c5b91b2',
    10: '629e7b25944f1ee6f274bde0b852fe2c7ab938c9add2fc2befaa68b96e9b6525',
}
# Exact original row guards, including order and title; no fuzzy automatic repair.
FIXES = [
    (4, 48, '弗·恩格斯 答可尊敬的乔万尼·博维奥', 466, '446', 462, '442'),
    (4, 49, '弗·恩格斯 致国际社会主义者大学生代表大会', 462, '442', 466, '446'),
    (10, 253, '1885年', 538, '503', 565, '530'),
    (5, 141, '文献索引', 1030, 'pre-m', 1030, '1008'),
    (5, 142, '马克思恩格斯的著作', 1030, 'pre-m', 1030, '1008'),
    (5, 143, '马克思的著作', 1030, 'pre-m', 1030, '1008'),
]


def prepare(parent, work, output, version):
    prior = Catalog(parent)
    work = Path(work)
    if work.exists():
        raise ValueError('repair workspace must be new')
    shutil.copytree(prior.root, work, ignore=lambda directory, names:
                    {'catalog.json', 'toc.json'} if Path(directory) == prior.root else set())
    rows = json.loads(canonical(prior.rows))
    records = []
    for volume, order, title, old_page, old_label, page, label in FIXES:
        matching = [r for r in rows if r['book'] == '文集' and r['volume'] == volume and r['sort_order'] == order]
        if len(matching) != 1:
            raise ValueError('reviewed row not unique')
        row = matching[0]
        if (row['title'], row['pdf_page'], row['printed_page']) != (title, old_page, old_label):
            raise ValueError('reviewed baseline changed: ' + title)
        before = dict(row)
        row.update(pdf_page=page, printed_page=label)
        records.append({'before': before, 'after': dict(row),
                        'evidence': {'pdf_sha256': PDFS[volume], 'pdf_page': page, 'printed_page': label,
                                     'audit': 'toc-audit-20260924; same-edition original verified'}})
    (work / 'toc_entries.json').write_bytes(canonical(rows))

    # Restore the series index that all 55 volume indexes already link to.
    config = yaml.safe_load((work / 'config/static_books.yaml').read_text(encoding='utf-8'))
    book = next(b for b in config['books'] if b['key'] == 'lenin-ru')
    root = work / 'static_library' / book['folder']
    if (root / 'index.html').exists():
        raise ValueError('Lenin series index now exists; re-review before replacing it')
    links = []
    for volume in book['volumes']:
        if not (root / volume['index']).is_file():
            raise ValueError('series index target missing')
        links.append('<li><a href="' + html.escape(volume['index'], quote=True) + '">' +
                     html.escape(volume['label']) + '</a></li>')
    text = ('<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>В. И. Ленин — Полное собрание сочинений</title>'
            '<style>body{max-width:60rem;margin:2rem auto;padding:0 1rem;font:18px/1.6 serif}'
            'a{overflow-wrap:anywhere}</style><h1>В. И. Ленин — Полное собрание сочинений</h1>'
            '<ul>' + ''.join(links) + '</ul></html>')
    (root / 'index.html').write_text(text, encoding='utf-8')

    # The first letter and remainder of this SAME heading were split into two links.
    path = work / 'static_library/mew-de/42/index.htm'
    old = b'karl-marx-grundrisse-der-kritik-der-politischen-oekonomie/ergaenzungen-zu-den-kapiteln-von-geld-und-vom-kapital.html'
    content = path.read_bytes()
    if content.count(old) != 1 or b'href="me42_670.htm">' not in content:
        raise ValueError('MEW 42 reviewed split-link evidence changed')
    if not path.with_name('me42_670.htm').is_file():
        raise ValueError('MEW 42 target missing')
    path.write_bytes(content.replace(old, b'me42_670.htm'))

    before_files = {k: v for k, v in prior.manifest['files'].items() if k not in ('toc.json', 'sources.json')}
    after_files = {k: v for k, v in inventory(work).items() if k not in ('toc_entries.json', 'sources.json')}
    approvals = {'toc': difference(groups(prior.rows), groups(rows)),
                 'files': difference(before_files, after_files)}
    if set(approvals['files']) != {'static_library/lenin-ru/index.html', 'static_library/mew-de/42/index.htm'}:
        raise ValueError('first batch changed files outside its reviewed scope')
    for key, change in approvals['toc'].items():
        change['evidence'] = [r for r in records if r['before']['source_file'] == key]
    for key, change in approvals['files'].items():
        change['evidence'] = ('Existing 55 volume indexes point to ../index.html; targets from current static_books.yaml'
                              if key.endswith('lenin-ru/index.html') else
                              'MEW 42 index heading first letter has wrong link; remainder points to existing me42_670.htm')
    binding = build(work, output, version, parent=parent, approvals=approvals)
    (work.parent / (version + '-repairs.json')).write_bytes(canonical({'binding': binding, 'rows': records,
                                                                     'approvals': approvals}))
    return binding


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.parent, args.work, args.output, args.version)))
