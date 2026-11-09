"""Reviewed electronic excerpts retain honest PDF locations through publication."""
import ast
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import personal_library as library
import personal_corpus as personal
from build_index import normalize


def reviewed_row():
    bib = dict(title='原书节选（电子重排本）', authors=['作者'], publisher='出版机构',
               place='北京', year='2016', source='manual_verified', confidence=1,
               reproduction_kind='unpaginated_excerpt', pagination_basis={
                   'kind':'pdf', 'source_sha256':'a'*64, 'page_count':253,
                   'original_images_reviewed':True, 'reason':'原件无印刷页码'})
    return dict(id=41, user_id=11, sha256='a'*64, page_count=253,
                source_kind='text_layer', ocr_consent=0,
                bibliographic_json=json.dumps(bib))


def evaluate(row, *, coverage=249, toc=True):
    quality = dict(toc_confidence=1, page_confidence=0, page_segments=[],
                   text_layer={'requires_ocr':False})
    module = ast.parse((Path(__file__).parents[1]/'app.py').read_text(encoding='utf-8'))
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                    and n.name == '_mylib_derivative_acceptance')
    namespace = dict(MYLIB_ACCEPTANCE_GATE_VERSION=3, mylib=SimpleNamespace(
        submission_bibliographic=library.submission_bibliographic,
        submission_quality=lambda _row: quality,
        verified_pdf_pagination=library.verified_pdf_pagination),
        mylib_corpus=SimpleNamespace(
            book_derivative_stats=lambda *_: {'pages':coverage,'version':22},
            get_book_toc=lambda *_: [{'pdf_page':7,'title':'正文'}] if toc else [],
            read_book_pages=lambda *_: [{'page':7}],
            get_book_page=lambda *_args, **_kw: {
                'pdf_page':7,'citation':'作者：原书节选，第7页（此为PDF页码，非原书印刷页码）。'}))
    exec(compile(ast.Module(body=[function], type_ignores=[]),'app.py','exec'),namespace)
    return namespace[function.name](row)


def test_reviewed_excerpt_passes_with_explicit_pdf_basis():
    report=evaluate(reviewed_row())
    assert report['status']=='pass'
    assert report['checks']['pagination_basis']=='pdf'
    assert 'PDF' in report['sample_citation']
    assert any('PDF' in w for w in report['warnings'])


@pytest.mark.parametrize('mutation',['source','pages','images','trust'])
def test_changed_original_or_unreviewed_basis_stays_blocked(mutation):
    row=reviewed_row();bib=json.loads(row['bibliographic_json'])
    if mutation=='source':row['sha256']='b'*64
    if mutation=='pages':row['page_count']=254
    if mutation=='images':bib['pagination_basis']['original_images_reviewed']='true'
    if mutation=='trust':bib['source']='user_metadata'
    row['bibliographic_json']=json.dumps(bib)
    report=evaluate(row)
    assert report['status']=='review'
    assert report['dimensions']['pagination']['status']=='review'


def test_pdf_basis_never_exempts_text_coverage_or_directory():
    report=evaluate(reviewed_row(),coverage=100,toc=False)
    assert report['status']=='review'
    assert report['dimensions']['text']['status']=='review'
    assert report['dimensions']['toc']['status']=='review'


def test_replacing_original_invalidates_cached_pagination_review(monkeypatch):
    row=reviewed_row()
    monkeypatch.setattr(library,'list_searchable_books',lambda _uid:[copy.deepcopy(row)])
    monkeypatch.setattr(personal,'_load_corpus_for_books',lambda _uid,books:
                        SimpleNamespace(reviewed=library.verified_pdf_pagination(books[0])))
    personal.invalidate(11)
    try:
        old=personal.get_personal_corpus(11)
        assert old.reviewed
        row['sha256']='b'*64
        new=personal.get_personal_corpus(11)
        assert new is not old and not new.reviewed
    finally:
        personal.invalidate(11)


def test_reader_toc_and_all_citation_styles_ignore_untrusted_printed_labels(tmp_path,monkeypatch):
    row=reviewed_row();source=personal.source_file_for(11,41)
    path=tmp_path/'11.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''
          CREATE TABLE pages(id INTEGER PRIMARY KEY,book TEXT,volume INTEGER,
            source_file TEXT,pdf_page INTEGER,printed_page TEXT,raw_text TEXT,normalized_text TEXT);
          CREATE TABLE toc_entries(id INTEGER PRIMARY KEY,book TEXT,volume INTEGER,
            source_file TEXT,title TEXT,pdf_page INTEGER,printed_page TEXT,
            level INTEGER,kind TEXT,sort_order INTEGER);
        ''')
        db.execute('INSERT INTO pages VALUES(1,?,?,?,?,?,?,?)',
                   ('mylib:41',1,source,7,'999','原件正文',normalize('原件正文')))
        db.execute('INSERT INTO toc_entries VALUES(1,?,?,?,?,?,?,?,?,?)',
                   ('mylib:41',1,source,'正文',7,'999',1,'body',1))
    monkeypatch.setattr(personal,'index_path',lambda _uid:path)
    monkeypatch.setattr(library,'get_submission',lambda *_:copy.deepcopy(row))
    corpus=personal._load_corpus_for_books(11,[row])
    assert corpus is not None
    monkeypatch.setattr(personal,'get_personal_corpus',lambda _:corpus)
    result=personal.get_book_page(11,41,7)
    assert result['printed_page'] is None
    assert 'PDF' in result['citation'] and '999' not in result['citation']
    assert result['citations']
    assert all('PDF' in citation and '999' not in citation
               for citation in result['citations'].values())
    assert not corpus.get_volume_by_source_file(source).printed_to_pdf
    assert personal.get_book_toc(11,41)[0]['printed_page'] is None
