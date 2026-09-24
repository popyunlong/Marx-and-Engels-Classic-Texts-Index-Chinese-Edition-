"""Bounded second batch: original-verified Wenji 5 depth and MEGA II/5 printed TOC."""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

from lxml import html as lhtml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import Catalog, canonical, inventory
from scripts.catalog_bundle import build, difference, groups
from scripts.prepare_catalog_repairs import PDFS

WENJI_LEVELS = {
    16: ('(1)价值表现的两极:相对价值形式和等价形式', 7),
    17: ('(2)相对价值形式', 7), 18: ('(a)相对价值形式的内容', 8),
    19: ('(b)相对价值形式的量的规定性', 8), 20: ('(3)等价形式', 7),
    21: ('(4)简单价值形式的总体', 7), 23: ('(1)扩大的相对价值形式', 7),
    24: ('(2)特殊等价形式', 7), 25: ('(3)总和的或扩大的价值形式的缺点', 7),
    27: ('(1)价值形式的变化了的性质', 7), 28: ('(2)相对价值形式和等价形式的发展关系', 7),
    29: ('(3)从一般价值形式到货币形式的过渡', 7),
}

# Read from and visually checked against MEGA II/5 PDF pages 6–7 (printed 7*–8*).
# Level 1 is a top-level item under the work heading, not an inferred HTML h1.
MEGA_TOC = [
    (1, 'Vorwort', 11), (1, 'Erstes Buch. Der Produktionsprozeß des Kapitals', 17),
    (2, 'Erstes Kapitel. Ware und Geld', 17), (3, '1. Die Ware', 17),
    (3, '2. Der Austauschprozeß der Waren', 51), (3, '3. Das Geld oder die Warenzirkulation', 59),
    (4, 'A. Maß der Werte', 59), (4, 'B. Zirkulationsmittel', 65),
    (5, 'a) Die Metamorphose der Waren', 65), (5, 'b) Der Umlauf des Geldes', 74),
    (5, 'c) Die Münze. Das Wertzeichen', 83), (4, 'C. Geld', 87),
    (5, 'a) Schatzbildung', 88), (5, 'b) Zahlungsmittel', 92), (5, 'c) Weltgeld', 98),
    (2, 'Zweites Kapitel. Die Verwandlung von Geld in Kapital', 102),
    (3, '1. Die allgemeine Formel des Kapitals', 102), (3, '2. Widersprüche der allgemeinen Formel', 110),
    (3, '3. Kauf und Verkauf der Arbeitskraft', 120),
    (2, 'Drittes Kapitel. Die Produktion des absoluten Mehrwerts', 129),
    (3, '1. Arbeitsprozeß und Verwertungsprozeß', 129), (3, '2. Konstantes und variables Kapital', 148),
    (3, '3. Die Rate des Mehrwerts', 158), (3, '4. Der Arbeitstag', 177),
    (3, '5. Rate und Masse des Mehrwerts', 241),
    (2, 'Viertes Kapitel. Die Produktion des relativen Mehrwerts', 251),
    (3, '1. Begriff des relativen Mehrwerts', 251), (3, '2. Kooperation', 259),
    (3, '3. Teilung der Arbeit und Manufaktur', 272), (3, '4. Maschinerie und große Industrie', 301),
    (2, 'Fünftes Kapitel. Weitere Untersuchungen über die Produktion des absoluten und relativen Mehrwerts', 413),
    (3, '1. Absoluter und relativer Mehrwert', 413),
    (3, '2. Größenwechsel von Preis der Arbeitskraft und Mehrwert', 420),
    (4, 'A. Größe des Arbeitstags und Intensität der Arbeit konstant (gegeben), Produktivkraft der Arbeit variabel', 421),
    (4, 'B. Konstanter Arbeitstag, konstante Produktivkraft der Arbeit, Intensivität der Arbeit variabel', 424),
    (4, 'C. Produktivkraft und Intensivität der Arbeit konstant, Arbeitstag variabel', 426),
    (4, 'D. Gleichzeitige Variationen in Länge des Arbeitstags, Produktivkraft und Intensivität der Arbeit', 427),
    (3, '3. Verschiedne Formeln für die Rate des Mehrwerts', 430),
    (3, '4. Wert, resp. Preis der Arbeitskraft in der verwandelten Form des Arbeitslohns', 433),
    (4, 'a) Die Formverwandlung', 433), (4, 'b) Die beiden Grundformen des Arbeitslohns: Zeitlohn und Stücklohn', 440),
    (2, 'Sechstes Kapitel. Der Akkumulationsprozeß des Kapitals', 456),
    (3, '1. Die kapitalistische Akkumulation', 457), (4, 'a) Einfache Reproduktion', 457),
    (4, 'b) Verwandlung von Mehrwert in Kapital', 469),
    (4, 'c) Das allgemeine Gesetz der kapitalistischen Akkumulation', 494),
    (3, '2. Die sog. ursprüngliche Akkumulation', 574), (3, '3. Die moderne Kolonisationstheorie', 610),
    (2, 'Nachtrag zu den Noten des ersten Buchs', 620), (2, 'Anhang zu Kapitel I, 1. Die Wertform', 626),
]


def mega_index(folder):
    page_map = {}
    for path in folder.glob('sec-*.html'):
        tree = lhtml.fromstring(path.read_bytes())
        for node in tree.xpath('//*[@id]'):
            anchor = node.get('id')
            if re.fullmatch(r's\d+', anchor):
                if anchor in page_map:
                    raise ValueError('ambiguous MEGA page anchor ' + anchor)
                page_map[anchor] = path.name
    parts, depth = [], 0
    for level, title, printed in MEGA_TOC:
        # Every target verified against the original's numeric printed-page run.
        pdf = printed + 9
        anchor = 's' + str(pdf)
        target = page_map[anchor]
        if level > depth + 1:
            raise ValueError('unexpected printed TOC level jump')
        if level > depth:
            parts.append('<ol>')
        elif level == depth:
            parts.append('</li>')
        else:
            parts.append('</li>' + '</ol></li>' * (depth - level))
        parts.append(f'<li data-level="{level}"><a href="{target}#{anchor}">{html.escape(title)}</a> '
                     f'<span class="pages">{printed}</span>')
        depth = level
    parts.append('</li>' + '</ol></li>' * (depth - 1) + '</ol>')
    index = folder / 'index.html'
    raw = index.read_text(encoding='utf-8')
    marker = '<ol class="toc">'
    if raw.count(marker) != 1:
        raise ValueError('unexpected MEGA legacy index structure')
    first, tail = raw.split(marker, 1)
    legacy, ending = tail.rsplit('</ol>', 1)
    navigation = ('<nav aria-label="Inhalt des Textbandes" class="reviewed-toc">' + ''.join(parts) + '</nav>'
                  '<p class="meta">Diese Datei enthält den Textband. Einleitung, vollständige editorische Hinweise, '
                  'Apparat und Register sind nicht vollständig enthalten.</p>'
                  '<details><summary>Alle bisherigen Seitenabschnitte</summary>' + marker + legacy + '</ol></details>')
    first = first.replace('</style>', '.reviewed-toc ol{padding-left:1.3em}.reviewed-toc li{margin:.45em 0}'
                          '@media(max-width:600px){.reviewed-toc ol{padding-left:.8em}}' + '</style>')
    index.write_text(first + navigation + ending, encoding='utf-8')


def stream_chapter_one(folder):
    path = folder / 'sec-010.html'
    raw = path.read_text(encoding='utf-8')
    headings = re.findall(r'<h3>(.*?)</h3>', raw, flags=re.S)
    selected = [s for s in headings if re.match(r'[1-4]\.\s', s)]
    if len(selected) != 4:
        raise ValueError('Wenji stream chapter-one headings require re-review')
    links = []
    for number, title in enumerate(selected, 1):
        anchor = f'toc-ch1-section-{number}'
        raw = raw.replace('<h3>' + title + '</h3>', '<h3 id="' + anchor + '">' + title + '</h3>', 1)
        links.append('<li><a href="sec-010.html#' + anchor + '">' + title + '</a></li>')
    path.write_text(raw, encoding='utf-8')
    index = folder / 'index.html'
    raw = index.read_text(encoding='utf-8')
    pattern = r'(<li[^>]*>\s*<a href="sec-010.html[^\"]*"[^>]*>.*?</a>.*?)</li>'
    updated, count = re.subn(pattern, lambda m: m.group(1) + '<ol>' + ''.join(links) + '</ol></li>', raw, count=1, flags=re.S)
    if count != 1:
        raise ValueError('stream chapter-one index entry missing')
    index.write_text(updated, encoding='utf-8')


def prepare(parent, work, output, version):
    prior = Catalog(parent)
    shutil.copytree(prior.root, work, ignore=lambda directory, names:
                    {'catalog.json', 'toc.json'} if Path(directory) == prior.root else set())
    rows = json.loads(canonical(prior.rows))
    records = []
    for order, (title, level) in WENJI_LEVELS.items():
        matches = [r for r in rows if r['book'] == '文集' and r['volume'] == 5 and r['sort_order'] == order]
        if len(matches) != 1 or matches[0]['level'] != 6 or matches[0]['title'] != title:
            raise ValueError('Wenji 5 reviewed subtree changed')
        row = matches[0]
        before = dict(row)
        row['level'] = level
        records.append({'before': before, 'after': dict(row), 'evidence': {'pdf_sha256': PDFS[5],
                        'pdf_page': 9, 'scope': 'Printed contents and verified A/B/C numeric/letter subtree'}})
    work = Path(work)
    (work / 'toc_entries.json').write_bytes(canonical(rows))
    mega_index(work / 'static_library/mega-full/mega2-ii-5')
    stream_chapter_one(work / 'stream_library/wenji-zh/5')
    old = {k: v for k, v in prior.manifest['files'].items() if k not in ('toc.json', 'sources.json')}
    new = {k: v for k, v in inventory(work).items() if k not in ('toc_entries.json', 'sources.json')}
    approvals = {'toc': difference(groups(prior.rows), groups(rows)), 'files': difference(old, new)}
    if set(approvals['files']) != {'static_library/mega-full/mega2-ii-5/index.html',
                                  'stream_library/wenji-zh/5/index.html',
                                  'stream_library/wenji-zh/5/sec-010.html'}:
        raise ValueError('pilot changed files outside its reviewed scope')
    for change in approvals['toc'].values():
        change['evidence'] = records
    for key, change in approvals['files'].items():
        change['evidence'] = ('MEGA II/5 original PDF contents pp6–7; numeric page run offset +9; all existing anchors retained'
                              if 'mega2-ii-5' in key else 'Wenji 5 original contents pp8–9; four chapter-one numbered sections')
    binding = build(work, output, version, parent=parent, approvals=approvals)
    report = {'binding': binding, 'rows': records, 'approvals': approvals,
              'limitations': ['MEGA II/5 original printed contents includes unavailable companion material',
                              'MEGA body subheadings beyond printed TOC remain unverified',
                              'Wenji 5 stream pilot covers four chapter-one sections only']}
    (work.parent / (version + '-repairs.json')).write_bytes(canonical(report))
    return binding


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('parent', 'work', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.parent, args.work, args.output, args.version)))
