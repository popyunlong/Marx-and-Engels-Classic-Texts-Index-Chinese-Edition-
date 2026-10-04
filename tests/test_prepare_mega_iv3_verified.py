from lxml import html
import pytest

from scripts.prepare_mega_iv3_verified import connect_navigation, preserve_body


def fixture():
    navigation = '<html><head><meta charset="utf-8"></head><body><nav class="reviewed-toc"><ol>'
    navigation += ''.join(f'<li><a href="sec-{n:03}.html#work">Work {n}</a></li>' for n in range(1, 51))
    navigation += '</ol></nav><a href="old.html#old">Old link</a></body></html>'
    bodies = {f'sec-{n:03}.html': f'<html><a data-pdf-page="{n}" id="page"></a><h1 id="work">Work {n}</h1><h2 id="detail">Detail</h2></html>'.encode() for n in range(1, 51)}
    entries = [dict(file='sec-003.html', anchor='detail', pdf_page=3, printed_page='1',
                    title='Confirmed detail', evidence_group='reviewed')]
    return navigation.encode(), bodies, entries


def test_navigation_uses_actual_body_position_and_keeps_prior_links():
    before, bodies, entries = fixture()
    after, accepted = connect_navigation(before, bodies, entries)
    tree = html.fromstring(after)
    added = tree.xpath('//li[@data-verified-body-anchor="detail"]')[0]
    assert added.getparent().getparent().xpath('./a/text()') == ['Work 3']
    assert tree.xpath('//a[@href="old.html#old"]')
    assert accepted[0]['target'] == 'sec-003.html#detail'


def test_navigation_rejects_evidence_at_wrong_pdf_position():
    before, bodies, entries = fixture()
    entries[0]['pdf_page'] = 4
    with pytest.raises(ValueError, match='PDF position'): connect_navigation(before, bodies, entries)


def test_navigation_rejects_missing_target():
    before, bodies, entries = fixture()
    entries[0]['anchor'] = 'missing'
    with pytest.raises(KeyError): connect_navigation(before, bodies, entries)


def test_explicit_reviewed_parts_get_their_own_chapters():
    before, bodies, _ = fixture()
    before = before.replace(b'>Work 3<', b'>Le d\xc3\xa9tail de la France<')
    bodies['sec-003.html'] = b'<html><a data-pdf-page="3" id="page"></a><h1 id="work">Work</h1><h2 id="part">Part</h2><h2 id="chapter">Chapter</h2></html>'
    entries = [dict(file='sec-003.html', anchor='part', pdf_page=3, title='Premi\u00e8re partie. Title', evidence_group='reviewed', parent_context='Le d\u00e9tail de la France'),
               dict(file='sec-003.html', anchor='chapter', pdf_page=3, title='Ch. I.', evidence_group='reviewed', parent_context='Le d\u00e9tail de la France / Premi\u00e8re partie')]
    after, _ = connect_navigation(before, bodies, entries)
    tree = html.fromstring(after)
    chapter = tree.xpath('//li[@data-verified-body-anchor="chapter"]')[0]
    assert chapter.getparent().getparent().get('data-verified-body-anchor') == 'part'


def test_only_anchor_and_reviewed_heading_tag_changes_preserve_body():
    before = b'<html><a id="old" data-pdf-page="3"></a><h2>Body</h2><a href="old.html">Link</a></html>'
    after = b'<html><a id="old" data-pdf-page="3"></a><p id="new">Body</p><a href="old.html">Link</a></html>'
    preserve_body(before, after)
    for changed in [after.replace(b'Body', b'Other'), after.replace(b'id="old"', b'id="lost"'),
                    after.replace(b'old.html', b'other.html'), after.replace(b'page="3"', b'page="4"')]:
        with pytest.raises(ValueError): preserve_body(before, changed)
