"""Prepare a bounded MEGA² IV/3 text-volume TOC from the 1998 printed contents.

The supplied facsimile contains the Text volume only. The Apparat and indexes
named on printed contents pages V–VIII must not be presented as available.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import fitz
from lxml import html as lhtml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import Catalog, canonical, inventory
from scripts.catalog_bundle import build, difference


PDF_SHA256 = '0b9978fd41f0ba1ab6511dc0a85657e6cdd6c313970b1986ce3fc6a910b61967'
SOURCE = 'static_library/mega-full/mega2-iv-3/index.html'

# Exact positions visible in the facsimile's body, not OCR h2 guesses. These
# extra IDs leave all page IDs and the text nodes themselves unchanged.
PRECISE = {
    'Exzerpte aus Pierre de Boisguillebert': (35, '[1) Boisguillebert:', 'iv3-toc-boisguillebert'),
    'Le détail de la France': (35, '[a) Le Détail de la France', 'iv3-toc-detail'),
    "Dissertation sur la nature des richesses, de l'argent et des tributs":
        (43, 'b) Dissertation sur la nature', 'iv3-toc-dissertation'),
    'Traité de la nature, culture, commerce et intérêt des grains':
        (57, 'c ) Traité de la nature', 'iv3-toc-traite'),
    'Exzerpte aus François Louis Auguste Ferrier: Du gouvernement considéré dans ses rapports avec le commerce':
        (210, '|[1]| Ferrier. F.LA.', 'iv3-toc-ferrier'),
    "Exzerpte aus Théodore Fix: De l'esprit progressif et de l'esprit de conservation en économie politique":
        (231, 'In einem Aufsatz v. Fix', 'iv3-toc-fix'),
    "Exzerpte aus Alexandre Moreau de Jonnès: Aperçus statistiques sur la vie civile et l'économie domestique des Romains au commencement du quatrième siècle de notre ère":
        (231, 'Aus einem Aufsatz v. Moreau de Jonnès:', 'iv3-toc-moreau'),
    "Exzerpte aus Henri Storch: Cours d'économie politique. T. III, V, IV":
        (273, 'i| 1) Storch. Suite.', 'iv3-toc-storch-4'),
    'Exzerpte aus Auguste de Gasparin: Considérations sur les machines':
        (322, '|[2]| 2) Gasparin', 'iv3-toc-gasparin'),
    "Exzerpte aus Joseph Pecchio: Histoire de l'économie politique en Italie":
        (389, '1) ... le comte Joseph Pecchio:', 'iv3-toc-pecchio'),
}
CONTENTS_PRECISE = {
    115: (115, '[Inhaltsverzeichnis]', 'iv3-toc-inhalt-1'),
    141: (141, '[Inhaltsverzeichnis]', 'iv3-toc-inhalt-2'),
}

# (level, title, printed Text page, facsimile contents PDF page). The hierarchy
# and typography were reviewed against the original's printed pages V–VII.
# The main title is on the text title leaf (PDF page 10).
PRINTED_TOC = [
    (1, 'Karl Marx: Exzerpte und Notizen 1844–1847', None, 5),
    (2, 'Notizbuch aus den Jahren 1844–1847', 5, 5),
    (2, 'Pariser Hefte 1844/1845', None, 5),
    (3, 'Exzerpte aus Werken von Pierre de Boisguillebert und John Law sowie aus einer „Römischen Geschichte“', 35, 5),
    (4, 'Exzerpte aus Pierre de Boisguillebert', 35, 5),
    (5, 'Le détail de la France', 35, 5),
    (5, "Dissertation sur la nature des richesses, de l'argent et des tributs", 43, 5),
    (5, 'Traité de la nature, culture, commerce et intérêt des grains', 57, 5),
    (4, 'Exzerpte aus Jean Law: Considérations sur le numéraire et le commerce', 66, 5),
    (4, 'Exzerpte aus einer „Römischen Geschichte“', 69, 5),
    (3, "Exzerpte aus James Lauderdale: Recherches sur la nature et l'origine de la richesse publique", 84, 5),
    (2, 'Brüsseler Hefte 1845', None, 6),
    (3, 'Heft 1', 115, 6),
    (4, 'Inhaltsverzeichnis', 115, 6),
    (4, 'Exzerpte aus Louis Say: Principales causes de la richesse', 116, 6),
    (4, "Exzerpte aus Jean Charles Léonard Simonde de Sismondi: Études sur l'économie politique. T. 1", 123, 6),
    (4, 'Exzerpte aus C. G. de Chamborant: Du paupérisme', 137, 6),
    (4, 'Exzerpte aus Alban de Villeneuve-Bargemont: Économie politique chrétienne', 138, 6),
    (3, 'Heft 2', 141, 6),
    (4, 'Inhaltsverzeichnis', 141, 6),
    (4, 'Exzerpte aus Eugène Buret: De la misère des classes laborieuses en Angleterre et en France', 142, 6),
    (4, "Exzerpte aus Nassau William Senior: Principes fondamentaux de l'économie politique", 157, 6),
    (4, "Exzerpte aus Jean Charles Léonard Simonde de Sismondi: Études sur l'économie politique. T. 2", 175, 6),
    (3, 'Heft 3', 210, 6),
    (4, 'Exzerpte aus François Louis Auguste Ferrier: Du gouvernement considéré dans ses rapports avec le commerce', 210, 6),
    (4, "Exzerpte aus Alexandre de Laborde: De l'esprit d'association dans tous les intérêts de la communauté", 219, 6),
    (4, "Exzerpte aus Ramon de la Sagra: De l'industrie cotonnière et des ouvriers en Catalogne", 229, 6),
    (4, "Exzerpte aus Théodore Fix: De l'esprit progressif et de l'esprit de conservation en économie politique", 231, 6),
    (4, "Exzerpte aus Alexandre Moreau de Jonnès: Aperçus statistiques sur la vie civile et l'économie domestique des Romains au commencement du quatrième siècle de notre ère", 231, 6),
    (4, "Exzerpte aus Henri Storch: Cours d'économie politique. T. I, II, III", 233, 6),
    (4, "Exzerpt aus Louis François Bernard Trioen: Essais sur les abus de l'agiotage", 272, 6),
    (3, 'Heft 4', 273, 7),
    (4, "Exzerpte aus Henri Storch: Cours d'économie politique. T. III, V, IV", 273, 7),
    (4, 'Exzerpte aus Nicolas François Dupré de Saint Maur: Essai sur les monnoies', 281, 7),
    (4, 'Exzerpte aus Isaac de Pinto: Traité de la circulation et du crédit', 283, 7),
    (4, 'Exzerpte aus Josias Child: Traités sur le commerce et sur les avantages', 297, 7),
    (4, 'Exzerpte aus Benjamin Bell: De la disette', 317, 7),
    (3, 'Heft 5', 322, 7),
    (4, 'Exzerpte aus Auguste de Gasparin: Considérations sur les machines', 322, 7),
    (4, "Exzerpte aus Charles Babbage: Traité sur l'économie des machines et des manufactures", 325, 7),
    (4, 'Exzerpte aus Andrew Ure: Philosophie des manufactures', 342, 7),
    (4, "Exzerpte aus Isaac Pereire: Leçons sur l'industrie et les finances", 352, 7),
    (4, "Exzerpte aus Pellegrino Rossi: Cours d'économie politique", 354, 7),
    (3, 'Heft 6', 389, 7),
    (4, "Exzerpte aus Joseph Pecchio: Histoire de l'économie politique en Italie", 389, 7),
    (4, "Exzerpte aus John Ramsay MacCulloch: Discours sur l'origine, les progrès, les objets particuliers, et l'importance de l'économie politique", 407, 7),
    (4, "Exzerpte aus Charles Ganilh: Des systèmes d'économie politique", 413, 7),
    (4, "Exzerpte aus Adolphe Blanqui: Histoire de l'économie politique en Europe", 424, 7),
    (4, "Exzerpte aus François Villegardelle: Histoire des idées sociales avant la révolution française", 426, 7),
    (4, 'Exzerpte aus John Watts: The facts and fictions of political economists', 430, 7),
]


def page_targets(pdf: Path, folder: Path):
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != PDF_SHA256:
        raise ValueError('MEGA IV/3 original PDF fingerprint changed')
    document = fitz.open(pdf)
    footers = defaultdict(list)
    for index, page in enumerate(document):
        lines = page.get_text().strip().splitlines()
        if lines and lines[-1].strip().isdecimal():
            footers[int(lines[-1].strip())].append(index + 1)
    by_anchor = {}
    for path in folder.glob('sec-*.html'):
        tree = lhtml.fromstring(path.read_bytes())
        for node in tree.xpath('//*[@id]'):
            anchor = node.get('id')
            if re.fullmatch(r's\d+', anchor):
                if anchor in by_anchor:
                    raise ValueError('duplicate PDF-page anchor ' + anchor)
                by_anchor[anchor] = path.name
    targets = {}
    for printed in {row[2] for row in PRINTED_TOC if row[2] is not None}:
        if printed == 5:
            pdf_page = 13
        elif printed in (115, 272):
            # Both pages contain a final line-number 5 after the printed footer.
            pdf_page = printed + 6
        else:
            candidates = [p for p in footers[printed] if abs(p - (printed + 6)) <= 3]
            if len(candidates) != 1:
                raise ValueError(f'ambiguous printed page {printed}: {candidates}')
            pdf_page = candidates[0]
        anchor = 's' + str(pdf_page)
        if anchor not in by_anchor:
            raise ValueError('missing original PDF-page anchor ' + anchor)
        targets[printed] = (pdf_page, by_anchor[anchor], anchor)
    return targets


def precise_target(title: str, printed: int):
    return CONTENTS_PRECISE[printed] if title == 'Inhaltsverzeichnis' else PRECISE.get(title)


def add_precise_anchors(folder: Path, targets):
    changed = set()
    for level, title, printed, _ in PRINTED_TOC:
        selected = precise_target(title, printed)
        if selected is None:
            continue
        expected_page, needle, new_id = selected
        if printed != expected_page:
            raise ValueError('intra-page target has moved: ' + title)
        pdf_page, filename, old_anchor = targets[printed]
        path = folder / filename
        raw = path.read_text(encoding='utf-8')
        marker = re.search(r'<a id="' + old_anchor + r'"[^>]*></a>(.*?)(?=<a id="s\d+"|</article>)', raw, re.S)
        if marker is None or marker.group(1).count(needle) != 1 or ('id="' + new_id + '"') in raw:
            raise ValueError('precise source heading is missing or ambiguous: ' + title)
        section = marker.group(1).replace(needle, '<span id="' + new_id + '"></span>' + needle, 1)
        updated = raw[:marker.start(1)] + section + raw[marker.end(1):]
        # Span insertion must never edit OCR or editorial text.
        if lhtml.fromstring(raw.encode()).xpath('//article')[0].text_content() != lhtml.fromstring(updated.encode()).xpath('//article')[0].text_content():
            raise ValueError('body text changed while adding an anchor')
        path.write_text(updated, encoding='utf-8')
        changed.add(filename)
    return changed


def prepare(parent: Path, work: Path, output: Path, version: str, pdf: Path):
    prior = Catalog(parent)
    shutil.copytree(prior.root, work, ignore=lambda directory, names:
                    {'catalog.json', 'toc.json'} if Path(directory) == prior.root else set())
    work = Path(work)
    folder = work / 'static_library/mega-full/mega2-iv-3'
    targets = page_targets(pdf, folder)
    changed_sections = add_precise_anchors(folder, targets)
    parts, depth, evidence = [], 0, []
    for level, title, printed, evidence_pdf_page in PRINTED_TOC:
        if level > depth + 1:
            raise ValueError('printed TOC level jump')
        if level > depth:
            parts.append('<ol>')
        elif level == depth:
            parts.append('</li>')
        else:
            parts.append('</li>' + '</ol></li>' * (depth - level))
        if printed is None:
            if level == 1:
                target = 'sec-002.html#s10'
            else:
                target = 'sec-006.html#s41' if 'Pariser' in title else 'sec-012.html#s121'
            pages = ''
        else:
            pdf_page, filename, anchor = targets[printed]
            precise = precise_target(title, printed)
            target = filename + '#' + (precise[2] if precise else anchor)
            pages = f' <span class="pages">{printed}</span>'
        parts.append(f'<li data-level="{level}"><a href="{target}">{html.escape(title)}</a>{pages}')
        evidence.append({'level': level, 'title': title, 'printed_page': printed,
                         'pdf_page': None if printed is None else targets[printed][0],
                         'contents_pdf_page': evidence_pdf_page, 'target': target})
        depth = level
    parts.append('</li>' + '</ol></li>' * (depth - 1) + '</ol>')
    index = folder / 'index.html'
    raw = index.read_text(encoding='utf-8')
    marker = '<ol class="toc">'
    if raw.count(marker) != 1:
        raise ValueError('unexpected legacy IV/3 index structure')
    first, tail = raw.split(marker, 1)
    legacy, ending = tail.rsplit('</ol>', 1)
    first = first.replace('</style>', '.reviewed-toc ol{padding-left:1.3em}.reviewed-toc li{margin:.45em 0}'
                          '@media(max-width:600px){.reviewed-toc ol{padding-left:.8em}}' + '</style>')
    warning = ('<p class="meta">Der vorliegende Faksimile-Bestand enthält nur den Textband. '
               'Die im gedruckten Inhalt aufgeführten Abkürzungen, Einführung, Apparat und Register '
               'sind hier nicht vorhanden.</p>')
    navigation = ('<nav aria-label="Geprüftes Inhaltsverzeichnis des Textbandes" class="reviewed-toc">'
                  + ''.join(parts) + '</nav>' + warning
                  + '<details><summary>Bisherige Seitenabschnitte</summary>' + marker + legacy + '</ol></details>')
    index.write_text(first + navigation + ending, encoding='utf-8')
    (work / 'toc_entries.json').write_bytes(canonical(prior.rows))
    old = {k: v for k, v in prior.manifest['files'].items() if k not in ('toc.json', 'sources.json')}
    new = {k: v for k, v in inventory(work).items() if k not in ('toc_entries.json', 'sources.json')}
    changed = difference(old, new)
    expected_files = {SOURCE} | {'static_library/mega-full/mega2-iv-3/' + name for name in changed_sections}
    if set(changed) != expected_files or len(changed_sections) != 9:
        raise ValueError('IV/3 candidate changed files outside reviewed volume scope')
    for name in changed:
        changed[name]['evidence'] = ('1998 Akademie Verlag MEGA² IV/3 Text original SHA-256 '
                                     + PDF_SHA256 + '; printed contents V–VII; verified body position; old anchors retained')
    binding = build(work, output, version, parent=parent, approvals={'toc': {}, 'files': changed})
    report = {'binding': binding, 'entries': evidence, 'approvals': changed,
              'limitations': ['The original PDF and HTML contain only Text, not Apparat and indexes.',
                              'Body independent heading coverage is still unverified.']}
    (work.parent / (version + '-repairs.json')).write_bytes(canonical(report))
    return binding


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('parent', 'work', 'output', 'pdf'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.parent, args.work, args.output, args.version, args.pdf)))
