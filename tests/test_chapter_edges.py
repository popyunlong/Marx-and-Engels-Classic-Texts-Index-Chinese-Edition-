"""First coverage of projection budgets and changes during an active query."""
import threading
import time
from types import SimpleNamespace

import pytest

from chapter_fixture import make_corpus
from chapter_search import ChapterQuery, ChapterScopeError, chapter_rows, resolve
from layout_exact import Projection, LayoutIndex, RUN


def projection(repetitions):
    value = object.__new__(Projection)
    value.text = ('甲乙' * repetitions).encode('utf-32-le')
    value.runs = b''.join(RUN.pack(i * 2 + j, i * 3 + j * 2, 1)
                          for i in range(repetitions) for j in (0, 1))
    value.count = repetitions * 2
    value.entry = {'id': 'a' * 64, 'removed': []}
    return value


def test_unselected_layout_candidates_do_not_spend_selected_budget():
    value = projection(10002)
    lo = 10001 * 3
    hits = list(value.find('甲乙', time.monotonic() + 10, [(lo, lo + 3)]))
    assert len(hits) == 1 and hits[0]['start'] == lo and hits[0]['end'] == lo + 3


def test_projection_clips_boundary_and_never_joins_disjoint_ranges():
    value = projection(4)
    assert list(value.find('甲乙', time.monotonic() + 2, [(0, 2)])) == []
    hits = list(value.find('甲乙', time.monotonic() + 2, [(0, 3), (9, 12)]))
    assert [h['start'] for h in hits] == [0, 9]
    assert list(value.find('乙甲', time.monotonic() + 2, [(0, 3), (9, 12)])) == []
    assert list(value.find('甲乙', time.monotonic() + 2, [])) == []


def test_layout_cache_is_partitioned_by_chapter_interval():
    index = object.__new__(LayoutIndex)
    index.cache, index.lock, index.error, index.enabled = {}, threading.Lock(), '', True
    index.projections = {'book.pdf': projection(3)}
    volumes = [SimpleNamespace(source_file='book.pdf')]
    lock = threading.Semaphore(1)
    first = index.scan('甲乙', volumes, lock, {'book.pdf': [(0, 3)]})
    second = index.scan('甲乙', volumes, lock, {'book.pdf': [(6, 9)]})
    whole = index.scan('甲乙', volumes, lock)
    assert [m['start'] for m in first[0]['book.pdf']] == [0]
    assert [m['start'] for m in second[0]['book.pdf']] == [6]
    assert len(whole[0]['book.pdf']) == 3 and len(index.cache) == 3


def test_corpus_wrapper_passes_only_selected_projection_intervals(monkeypatch):
    corpus, _ = make_corpus()
    vol = corpus.books['文集'][0]
    token = chapter_rows(corpus, vol)[1]['token']
    selection = resolve(corpus, [token], 'v1', 'v1', corpus.books)
    seen = []
    # Restore the real wrapper hidden by the fixture's empty layout lane.
    from search import Corpus
    corpus._layout_scan = Corpus._layout_scan.__get__(corpus)
    corpus.layout_index = SimpleNamespace(scan=lambda *a, **k: seen.append(k['document_ranges']) or ({}, True, ''))
    query = ChapterQuery(corpus, selection, '劳动')
    doc = selection.documents[0]
    assert query.count() == 1 and seen == [{doc.source_file: [(doc.norm_start, doc.norm_end)]}]


@pytest.mark.parametrize('change', ['version', 'permission'])
def test_midquery_version_or_publication_change_rejects_response(monkeypatch, change):
    import app as web
    corpus, _ = make_corpus()
    token = chapter_rows(corpus, corpus.books['文集'][0])[1]['token']
    selection = resolve(corpus, [token], 'v1', 'v1', corpus.books)
    monkeypatch.setattr(web, 'corpus', corpus)
    monkeypatch.setattr(web, '_chapter_scope_version', lambda: 'v1')
    monkeypatch.setattr(web, '_public_book_keys', lambda **k: set(corpus.books))
    aggregate = ChapterQuery.aggregate
    def changed(query):
        result = aggregate(query)
        if change == 'version':
            monkeypatch.setattr(web, '_chapter_scope_version', lambda: 'v2')
        else:
            monkeypatch.setattr(web, '_public_book_keys', lambda **k: {'全集'})
        return result
    monkeypatch.setattr(ChapterQuery, 'aggregate', changed)
    with web.app.test_request_context('/api/search'):
        with pytest.raises(ChapterScopeError) as error:
            web._chapter_search_payload(selection, '劳动', 'exact', 1, True)
    assert error.value.status == 409
