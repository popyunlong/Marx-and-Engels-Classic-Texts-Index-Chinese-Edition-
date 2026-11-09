"""Private indexes must load with a public catalogue active in the same process."""
import sqlite3

import pytest

from build_index import normalize
from personal_corpus import PersonalCorpus, source_file_for
from search import Corpus


def private_index(tmp_path):
    path = tmp_path / 'private.sqlite'
    source = source_file_for(7, 51)
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE pages(id INTEGER PRIMARY KEY, book TEXT, volume INTEGER,
                source_file TEXT, pdf_page INTEGER, printed_page TEXT,
                raw_text TEXT, normalized_text TEXT);
            CREATE TABLE toc_entries(source_file TEXT, title TEXT, pdf_page INTEGER,
                printed_page TEXT, level INTEGER, kind TEXT, sort_order INTEGER);
        ''')
        text = '个人书库正文与准确的引文'
        db.execute('INSERT INTO pages VALUES(1,?,?,?,?,?,?,?)',
                   ('mylib:51', 1, source, 10, '2', text, normalize(text)))
        db.execute('INSERT INTO toc_entries VALUES(?,?,?,?,?,?,?)',
                   (source, '私人目录', 10, '2', 1, 'body', 1))
    return path, source


def test_private_text_toc_and_citation_ignore_active_public_snapshot(tmp_path, monkeypatch):
    path, source = private_index(tmp_path)

    def public_catalog():
        raise AssertionError('private loader requested a public snapshot')

    monkeypatch.setattr('catalog_release.active_catalog', public_catalog)
    corpus = PersonalCorpus(path, {'mylib:51': {
        'title': '个人书籍', 'author': '作者',
        'bibliographic': {'publisher': '出版社', 'place': '北京', 'year': '2026'},
    }})
    volume = corpus.get_volume_by_source_file(source)
    assert volume.pages[0].raw_text == '个人书库正文与准确的引文'
    assert corpus.get_chapter_for_page(source, 10).title == '私人目录'
    citation = corpus._make_citation('mylib:51', 1, volume.pages, source_file=source)
    assert '个人书籍' in citation and '2026' in citation
    assert corpus.catalog is None


def test_public_loader_still_rejects_mismatched_snapshot(tmp_path, monkeypatch):
    path, _source = private_index(tmp_path)

    class PublicSnapshot:
        def verify_corpus(self, conn):
            assert conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 1
            raise ValueError('public catalogue mismatch')

    monkeypatch.setattr('catalog_release.active_catalog', lambda: PublicSnapshot())
    with pytest.raises(ValueError, match='public catalogue mismatch'):
        Corpus(path)
