"""Rebase verified IV/3 deltas on the live parent and connect bounded navigation.

The input evidence is data, not a release approval. All content fingerprints,
existing links, text, PDF positions and the exact changed-file set are checked.
Only one new complete package tree is materialized; source snapshots are read-only.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from lxml import html, etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import Catalog, canonical, file_digest, ID_RE, safe_file
from scripts.catalog_bundle import difference, validate_changes
from scripts.prepare_mega_iv3 import PDF_SHA256

PREFIX = 'static_library/mega-full/mega2-iv-3/'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest_bytes(body):
    return hashlib.sha256(body).hexdigest()


def align_verified_anchors(body):
    """Keep a selected body heading at the reader's 96px page-detection line."""
    def align(match):
        opening = match[1]
        style = re.search(rb'\bstyle="([^"]*)"', opening)
        if style and b'scroll-margin-top:96px' in style[1]: return match[0]
        if style:
            opening = opening[:style.start(1)] + style[1] + b';scroll-margin-top:96px' + opening[style.end(1):]
        else:
            opening += b' style="scroll-margin-top:96px"'
        return opening + b'>'
    result = re.sub(rb'(<[a-z][a-z0-9]*\b[^>]*\bid="iv3-[^"]+"[^>]*)(>)', align, body)
    preserve_body(body, result)
    return result


def preserve_body(before, after):
    a, b = html.fromstring(before), html.fromstring(after)
    ids = lambda tree: Counter(tree.xpath('//*[@id]/@id'))
    if a.text_content() != b.text_content() or ids(a) - ids(b):
        raise ValueError('body text or existing IDs changed')
    for xpath in ('//a/@href', '//img/@src', '//*[@data-pdf-page]/@data-pdf-page'):
        if a.xpath(xpath) != b.xpath(xpath):
            raise ValueError('existing links, images or PDF positions changed')


def load_entries(composed, evidence_roots):
    summary = read_json(composed / 'evidence.json')
    entries = []
    for group in summary['source_drafts']:
        matches = [root / group / 'evidence.json' for root in evidence_roots if (root / group / 'evidence.json').is_file()]
        if len(matches) != 1:
            raise ValueError('missing or ambiguous evidence group: ' + group)
        report = read_json(matches[0])
        if report['original_pdf_sha256'] != PDF_SHA256:
            raise ValueError('original PDF evidence differs')
        for file in report['files']:
            for heading in file.get('anchors', file.get('headings', [])):
                entries.append(dict(heading, file=file['file'], evidence_group=group))
    if len(entries) != summary['new_anchors'] or len({e['anchor'] for e in entries}) != len(entries):
        raise ValueError('heading evidence missing or duplicated')
    return summary, entries


def anchor_positions(bodies):
    positions = {}
    for name, body in bodies.items():
        if not name.endswith('.html') or name.endswith('/index.html'):
            continue
        page = None
        for ordinal, node in enumerate(html.fromstring(body).iter()):
            if node.get('data-pdf-page'):
                page = int(node.get('data-pdf-page'))
            anchor = node.get('id')
            if anchor:
                key = Path(name).name + '#' + anchor
                if key in positions:
                    raise ValueError('duplicate anchor: ' + key)
                positions[key] = (page, ordinal)
    return positions


def connect_navigation(index_body, bodies, entries):
    tree = html.fromstring(index_body)
    nav = tree.xpath('//nav[@class="reviewed-toc"]')
    if len(nav) != 1:
        raise ValueError('printed navigation missing')
    positions = anchor_positions(bodies)
    printed = []
    for li in nav[0].xpath('.//li'):
        links = li.xpath('./a')
        if len(links) != 1:
            raise ValueError('ambiguous printed navigation entry')
        link = links[0]
        printed.append(dict(li=li, title=link.text_content(), target=link.get('href'), is_leaf=not li.xpath('./ol'),
                            position=positions[link.get('href')]))
    if len(printed) != 50:
        raise ValueError('printed navigation differs from reviewed 50 entries')
    # Select only the known enclosing work. Unproven finer groupings stay flat
    # under that work rather than inventing intermediate titles or ordinals.
    def root_for(entry):
        context = entry.get('parent_context', '')
        terms = []
        if entry['evidence_group'] == 'iv3-notebook-anchors': terms = ['Notizbuch']
        elif 'Lauderdale' in context: terms = ['James Lauderdale']
        elif 'Louis Say' in context: terms = ['Louis Say']
        elif 'Sismondi' in context: terms = ['Simonde de Sismondi', 'T. 1']
        elif 'Buret' in context: terms = ['Eugène Buret']
        elif 'Senior' in context: terms = ['Nassau William Senior']
        elif 'Jean Law' in context: terms = ['Exzerpte aus Jean Law']
        elif 'Römische Geschichte' in context: terms = ['Exzerpte aus einer']
        elif context == 'Pariser Hefte': terms = ['Pariser Hefte']
        elif context.startswith('Boisguillebert'): terms = ['Traité de la nature']
        elif context.startswith('Le détail'): terms = ['Le détail de la France']
        elif context.startswith('Dissertation'): terms = ['Dissertation sur la nature']
        if terms:
            matches = [item for item in printed if all(t in item['title'] for t in terms)]
            if len(matches) != 1: raise ValueError('ambiguous enclosing work: ' + context)
            return matches[0]
        target = Path(entry['file']).name + '#' + entry['anchor']
        candidates = [item for item in printed if item['position'] <= positions[target] and item['is_leaf']]
        if not candidates: raise ValueError('no verified enclosing work for ' + target)
        return max(candidates, key=lambda item: item['position'])

    ordered = sorted(entries, key=lambda e: positions[Path(e['file']).name + '#' + e['anchor']])
    inserted, accepted, structures = {}, [], {}
    for entry in ordered:
        target = Path(entry['file']).name + '#' + entry['anchor']
        if positions[target][0] != entry['pdf_page']:
            raise ValueError('verified heading PDF position changed: ' + target)
        root = root_for(entry)
        parent = root['li']
        structure = structures.setdefault(root['target'], {})
        title = entry['title']
        context = entry.get('parent_context', '')
        # Use only divisions actually present among the verified headings.
        # Numbering gaps and unreadable divisions never create synthetic nodes.
        if context.startswith('Le détail'):
            if re.match(r'(Première|Seconde|Troisième)\s+[Pp]artie', title):
                structure['part'] = entry['anchor']
            elif '/' in context and 'part' in structure:
                parent = inserted[structure['part']]
        elif context.startswith('Boisguillebert'):
            if re.match(r'I{1,2}\.?\s+Partie', title):
                structure['part'] = entry['anchor']
            elif '/' in context and 'part' in structure:
                parent = inserted[structure['part']]
        elif 'Lauderdale' in context:
            if re.match(r'Ch\.\s+[IVX]+\.', title):
                structure['chapter'] = entry['anchor']
                structure.pop('capital', None)
            elif '/ capitaux' in context and 'capital' in structure:
                parent = inserted[structure['capital']]
            elif '/ Ch.' in context and 'chapter' in structure:
                parent = inserted[structure['chapter']]
                if title.startswith('3) des capitaux'):
                    structure['capital'] = entry['anchor']
        elif 'Buret' in context:
            if re.match(r'livr(?:e)?\.?\s+[IVX]+\.', title):
                structure['book'] = entry['anchor']
                structure.pop('chapter', None)
            elif title.startswith('ch.'):
                if 'book' in structure: parent = inserted[structure['book']]
                structure['chapter'] = entry['anchor']
            elif title.startswith('sect.') and 'chapter' in structure:
                parent = inserted[structure['chapter']]
        elif context.startswith('Jean Law'):
            if title.startswith('a) Considérations'):
                structure['work'] = entry['anchor']
            elif '/ Considérations' in context and 'work' in structure:
                parent = inserted[structure['work']]
        # Numbered Feuerbach propositions have an explicit reviewed parent.
        if re.fullmatch(r'iv3-notebook-feuerbach-\d+', entry['anchor']):
            parent = inserted['iv3-notebook-feuerbach']
        elif entry['anchor'] in ('iv3-notebook-douane', 'iv3-notebook-wohnung'):
            parent = inserted['iv3-notebook-sociale']
        children = parent.xpath('./ol[@class="verified-body-toc"]')
        if not children:
            children = [etree.SubElement(parent, 'ol', {'class': 'verified-body-toc'})]
        li = etree.SubElement(children[0], 'li', {'data-verified-body-anchor': entry['anchor']})
        link = etree.SubElement(li, 'a', href=target)
        link.text = entry['title']
        if entry.get('printed_page') is not None:
            page = etree.SubElement(li, 'span', {'class': 'pages'})
            page.text = ' ' + str(entry['printed_page'])
        inserted[entry['anchor']] = li
        accepted.append(dict(entry, target=target, enclosing_work=root['title']))
    note = etree.Element('p', {'class': 'meta'})
    note.text = '原书主目录及已核实的正文细目。正文细目持续补充；原有分页入口保留。'
    nav[0].addprevious(note)
    result = b'<!DOCTYPE html>\n' + html.tostring(tree, encoding='utf-8')
    old = html.fromstring(index_body)
    new = html.fromstring(result)
    if Counter(old.xpath('//a/@href')) - Counter(new.xpath('//a/@href')):
        raise ValueError('existing navigation links lost')
    return result, accepted


def prepare(parent, draft_parent, composed, evidence_roots, pdf, output, version, report_path):
    if file_digest(pdf) != PDF_SHA256:
        raise ValueError('original PDF fingerprint differs')
    prior, draft = Catalog(parent), Catalog(draft_parent)
    output = Path(output)
    if not ID_RE.fullmatch(version) or output.name != version or output.exists():
        raise ValueError('new version directory required')
    if prior.sources != draft.sources or prior.rows != draft.rows:
        raise ValueError('draft and live parent TOC differ; rebase the source data explicitly')
    summary, entries = load_entries(Path(composed), evidence_roots)
    replacements = {}
    for name, change in draft.manifest['changes']['files'].items():
        if not name.startswith(PREFIX) or prior.manifest['files'].get(name) != change['before']:
            raise ValueError('printed-navigation draft parent is stale: ' + name)
        replacements[name] = safe_file(draft.root, name).read_bytes()
    for item in summary['files']:
        name = item['file']
        before = safe_file(draft.root, name).read_bytes()
        after = safe_file(Path(composed) / 'tree', name).read_bytes()
        if not name.startswith(PREFIX) or digest_bytes(before) != item['before_sha256'] or digest_bytes(after) != item['after_sha256']:
            raise ValueError('composed draft fingerprint differs: ' + name)
        preserve_body(before, after)
        replacements[name] = after
    bodies = {name: replacements.get(name, safe_file(prior.root, name).read_bytes())
              for name in prior.manifest['files'] if name.startswith(PREFIX) and name.endswith('.html')}
    index, accepted = connect_navigation(replacements[PREFIX + 'index.html'], bodies, entries)
    replacements[PREFIX + 'index.html'] = index
    for name, after in replacements.items():
        if not name.endswith('/index.html'):
            replacements[name] = after = align_verified_anchors(after)
            preserve_body(safe_file(prior.root, name).read_bytes(), after)
    new_files = dict(prior.manifest['files'])
    new_files.update({name: digest_bytes(body) for name, body in replacements.items()})
    old_files = {k: v for k, v in prior.manifest['files'].items() if k not in ('toc.json', 'sources.json')}
    after_files = {k: v for k, v in new_files.items() if k not in ('toc.json', 'sources.json')}
    changes = difference(old_files, after_files)
    if set(changes) != set(replacements): raise ValueError('exact reviewed change set differs')
    approvals = {'toc': {}, 'files': {name: dict(change, evidence={
        'pdf_sha256': PDF_SHA256, 'printed_contents': 'original PDF5-7',
        'body_evidence_groups': sorted({e['evidence_group'] for e in entries if e['file'] == name}),
        'preserves': 'body text, prior IDs, hrefs, images and PDF positions',
        'navigation_alignment': 'new IV/3 anchors align with the existing reader detection line at 96px',
        'scope': 'reviewed printed navigation and bounded verified body headings'}) for name, change in changes.items()}}
    validated = validate_changes(prior.rows, prior.rows, old_files, after_files, approvals)
    manifest = dict(schema_version=1, id=version, parent={'id': prior.version, 'sha256': prior.sha256},
        input_toc_sha256=prior.manifest['input_toc_sha256'], files=new_files, changes=validated, approvals=approvals)
    output.mkdir(parents=True)
    for name in sorted(new_files):
        target = safe_file(output, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if name in replacements: target.write_bytes(replacements[name])
        else: shutil.copyfile(safe_file(prior.root, name), target)
    (output / 'catalog.json').write_bytes(canonical(manifest))
    final = Catalog(output)
    report = dict(binding={'id': version, 'sha256': final.sha256}, parent=manifest['parent'],
        changed_files=changes, entries=accepted, printed_entries=50,
        new_body_anchors=len(accepted), page_label_corrections=summary['page_label_corrections'],
        prose_tags_corrected=summary['prose_tags_corrected'], original_pdf_sha256=PDF_SHA256,
        remaining=['unreviewed body headings', 'ordinal glyphs on PDF163/169',
                   'unprinted page label at PDF118', 'unproven finer grouping retained under confirmed work'],
        full_volume_complete=False)
    Path(report_path).write_bytes(canonical(report))
    return {k: v for k, v in report.items() if k not in ('entries', 'changed_files')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('parent', 'draft-parent', 'composed', 'pdf', 'output', 'report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--evidence-root', type=Path, action='append', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.parent, args.draft_parent, args.composed, args.evidence_root,
                            args.pdf, args.output, args.version, args.report)))
