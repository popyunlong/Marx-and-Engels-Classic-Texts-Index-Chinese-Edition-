from types import SimpleNamespace
import zipfile

import pytest
from flask import g

from chapter_fixture import make_corpus
from chapter_search import ChapterQuery, ChapterScopeError, chapter_rows, resolve
from search import TocEntry, Volume, Page
from build_index import normalize


@pytest.fixture
def data():
    return make_corpus()


def choose(corpus, indexes=(0,), book='文集', volume=1, extra=()):
    vol = next(v for v in corpus.books[book] if v.volume == volume)
    rows = chapter_rows(corpus, vol)
    tokens = [rows[i]['token'] for i in indexes] + list(extra)
    return resolve(corpus, tokens, 'v1', 'v1', corpus.books)


@pytest.mark.parametrize('indexes,count', [((0,), 3), ((1,), 1), ((0, 1), 3), ((0, 2), 4), ((2,), 1)])
def test_nested_same_page_counts_pages_exports(data, indexes, count):
    corpus, _ = data
    selection = choose(corpus, indexes)
    query = ChapterQuery(corpus, selection, '劳动')
    agg = query.aggregate()
    exported = list(query.iter_hits())
    details = [h for doc in selection.documents for page in range(1, 5)
               for h in (query.page(doc.document_id, page, 1)['hits'] if page <= query.page(doc.document_id, 1, 1)['pages'] else [])]
    assert agg['total_hits'] == query.count() == len(exported) == len(details) == count
    assert details == exported
    assert all('外部前文' not in h['context'] for h in exported)
    if 2 not in indexes:
        assert all('边界外部秘密' not in h['context'] for h in exported)


def test_multivolume_union_parent_override_and_missing_result(data):
    corpus, _ = data
    other = choose(corpus, (1,), volume=2)
    selection = choose(corpus, (1,), extra=other.tokens)
    assert ChapterQuery(corpus, selection, '劳动').count() == 2
    assert {d.volume for d in selection.documents} == {1, 2}
    assert ChapterQuery(corpus, selection, '绝无此句').count() == 0
    all_volume = choose(corpus, (1,), extra=('vol:文集:1',))
    assert len(all_volume.documents) == 1
    assert all_volume.documents[0].norm_start == 0
    assert ChapterQuery(corpus, all_volume, '劳动').count() == 4
    assert list(ChapterQuery(corpus, selection, '劳动').iter_hits(0)) == []


def test_unreliable_boundary_and_empty_toc_fail_closed(data):
    corpus, toc = data
    vol = corpus.books['文集'][0]
    toc[vol.source_file].insert(2, TocEntry('未能定位的相邻篇名', 1, sort_order=2))
    rows = chapter_rows(corpus, vol)
    assert not rows[0]['selectable']
    assert not next(r for r in rows if r['title'] == '未能定位的相邻篇名')['selectable']
    toc[vol.source_file] = []
    assert chapter_rows(corpus, vol) == []


@pytest.mark.parametrize('case', ['version', 'missing', 'hidden', 'private', 'malformed', 'empty'])
def test_invalid_scope_never_widens(data, case):
    corpus, _ = data
    tokens = list(choose(corpus).tokens)
    version, public = 'v1', corpus.books
    if case == 'version': version = 'old'
    if case == 'missing': tokens[0] += '-invalid'
    if case == 'hidden': public = {'全集'}
    if case == 'private': tokens += ['book:mylib:42']
    if case == 'malformed': tokens = [None]
    if case == 'empty': tokens = []
    with pytest.raises(ChapterScopeError) as error:
        resolve(corpus, tokens, version, 'v1', public)
    assert error.value.status == (409 if case == 'version' else 400)


def test_cooccurrence_does_not_join_adjacent_selected_works(data):
    corpus, _ = data
    selection = choose(corpus, (0, 2))
    assert ChapterQuery(corpus, selection, '财富 秘密', 'cooccurrence').count() == 0
    query = ChapterQuery(corpus, selection, '劳动 财富', 'cooccurrence')
    assert query.count() == len(list(query.iter_hits())) == 3


def test_layout_supplements_cannot_cross_selected_boundary(data):
    corpus, _ = data
    selection = choose(corpus, (1,))
    doc = selection.documents[0]
    corpus._layout_scan = lambda *a, **k: ({doc.source_file: [
        {'start': doc.norm_end - 1, 'end': doc.norm_end + 2, 'spans': [(doc.norm_end - 1, doc.norm_end + 2)]}]}, True, '')
    assert ChapterQuery(corpus, selection, '不存在').count() == 0


def test_real_textual_and_semantic_retrievers_stay_in_subsection(data):
    from textual_search import retrieve_textual
    corpus, _ = data
    selection = choose(corpus, (1,))
    textual = retrieve_textual(corpus, '劳动创造社会财富', documents=selection.documents)
    semantic = corpus.locate_associative_in_documents(selection.documents,
        quotes=['劳动创造社会财富'], keywords=['劳动', '财富'], clip_context=True)
    for hits in (textual.hits, semantic):
        assert hits
        doc = selection.documents[0]
        assert all(doc.norm_start <= h.norm_start < h.norm_end <= doc.norm_end for h in hits)
        assert all('第一作品' not in h.context and '边界外部秘密' not in h.context for h in hits)


@pytest.fixture
def web_data(data, monkeypatch):
    import app as web
    corpus, _ = data
    monkeypatch.setattr(web, 'corpus', corpus)
    monkeypatch.setattr(web, '_public_book_keys', lambda **kwargs: set(corpus.books))
    monkeypatch.setattr(web, '_chapter_scope_version', lambda: 'v1')
    for name in ('_require_search', '_require_feature', '_rate_limit_or_abort', '_record_community_trend'):
        monkeypatch.setattr(web, name, lambda *a, **k: None)
    monkeypatch.setattr(web, 'current_view_state', lambda: {'pdf_enabled': True})
    monkeypatch.setattr(web, '_content_access_enabled', lambda *a, **k: True)
    monkeypatch.setattr(web, '_attach_viewer_payload', lambda h, *a, **k: h)
    return web, corpus


def test_actual_web_handlers_and_unsupported_consumers(web_data):
    web, corpus = web_data
    selection = choose(corpus, (1,))
    payload = {'q': '劳动', 'scope': list(selection.tokens), 'scope_version': 'v1'}
    with web.app.test_request_context('/api/search', method='POST', json=payload):
        g.current_user = None
        web._bind_chapter_selection()
        response = web.api_search().get_json()
        assert response['count'] == 1 and response['personal_results'] == []
        chapter = response['results'][0]['chapters'][0]
        assert chapter['scope_document_id'] == selection.documents[0].document_id
    payload.update(scope_document_id=chapter['scope_document_id'], chapter_pdf_page=1,
                   source_file=selection.documents[0].source_file)
    with web.app.test_request_context('/api/search/chapter-hits', method='POST', json=payload):
        g.current_user = None
        web._bind_chapter_selection()
        details = web.api_search_chapter_hits().get_json()
        assert details['count'] == len(details['hits']) == 1
    with web.app.test_request_context('/api/search/chapters?book=文集&volume=1'):
        result = web.api_search_chapters().get_json()
        assert result['scope_version'] == 'v1' and len(result['results']) == 3
        assert all('scope' not in row for row in result['results'])
    with web.app.test_request_context('/api/ai/chat', method='POST', json=payload):
        with pytest.raises(ChapterScopeError, match='暂不支持'):
            web._bind_chapter_selection()


@pytest.mark.parametrize('fmt', ['html', 'docx'])
def test_export_worker_snapshot_and_actual_artifact(web_data, tmp_path, monkeypatch, fmt):
    import search_exports as tasks
    web, corpus = web_data
    selection = choose(corpus, (1,))
    monkeypatch.setattr(tasks, 'DB_PATH', tmp_path / 'jobs.sqlite')
    monkeypatch.setattr(tasks, 'ARTIFACT_ROOT', tmp_path / 'artifacts')
    monkeypatch.setattr(tasks, '_INITIALIZED', False)
    job, reused = tasks.create_job(user_id=1, query_text='劳动', normalized_query='劳动', search_mode='exact',
        scope=selection.snapshot(), book_filter='', citation_style='gb2015', output_format=fmt,
        corpus_version='fixture', template_version='fixture', base_url='https://example.test', active_member=True)
    job = tasks.claim_next_job('fixture', corpus_version='fixture', template_version='fixture')
    with web.app.app_context():
        context = web._search_export_context(job)
        assert context['personal'] is None
        result = tasks.run_job(job['id'], worker_id='fixture',
            count_hits=lambda j, stop_after: web._search_export_count(j, stop_after, context=context),
            iter_hits=lambda j, limit: web._search_export_iter(j, limit, context=context))
    assert result['status'] == 'complete', result['error']
    artifact = tasks.completed_artifact(result)
    if fmt == 'docx':
        with zipfile.ZipFile(artifact) as package:
            text = package.read('word/document.xml').decode()
    else:
        text = artifact.read_text('utf-8')
    assert '内部子节' in text and '社会财富' in text and '边界外部秘密' not in text
    assert result['result_count'] == 1
    monkeypatch.setattr(web, '_chapter_scope_version', lambda: 'v2')
    with pytest.raises(ChapterScopeError, match='版本'):
        web._search_export_context(job)
