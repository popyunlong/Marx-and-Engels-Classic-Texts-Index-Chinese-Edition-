"""Count/detail/export contract and job integrity using synthetic, private fixtures."""
from __future__ import annotations

import json
import threading
import zipfile
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from search import Corpus, Page, Volume
import search_exports as tasks


def miniature(texts, page_numbers, boundaries, supplements):
    vol = Volume.build('book', 1, 'fixture.pdf', '测试卷', [
        Page(number, str(number), text, text) for number, text in zip(page_numbers, texts)
    ])
    corpus = object.__new__(Corpus)
    corpus.books = {'book': [vol]}
    segments = [dict(norm_start=a, norm_end=b, chapter_pdf_page=i + 1,
                     title=f'篇章{i}', printed_page=str(i + 1), level=1, kind='body')
                for i, (a, b) in enumerate(zip(boundaries, boundaries[1:]))]
    corpus._chapter_segments = lambda _vol: segments
    corpus._layout_scan = lambda *a, **kw: ({vol.source_file: supplements}, True, '')
    corpus._chaptered_cache = OrderedDict()
    corpus._chaptered_cache_lock = threading.Lock()
    corpus.get_book_config = lambda _book: SimpleNamespace(
        title='测试', short_title='测试', citation_title='测试', sort_order=1)
    corpus.get_volume_by_source_file = lambda _source: vol

    def hit(start, layout):
        return SimpleNamespace(to_dict=lambda: {'start': start, 'layout': layout})
    corpus._make_hit = lambda _vol, start, *a, **kw: hit(start, False)
    corpus._make_layout_hit = lambda _vol, match, _q: hit(match['start'], True)
    return corpus, vol, segments


@pytest.mark.parametrize('texts,pages,bounds,extras,query,expected,layout', [
    (['甲注', '乙'], [1, 2], [0, 2, 3], [{'start': 0, 'end': 3}], '甲乙', 0, False),
    (['甲注', '乙'], [1, 2], [0, 3], [{'start': 0, 'end': 3}], '甲乙', 1, True),
    (['甲注', '乙'], [1, 3], [0, 3], [{'start': 0, 'end': 3}], '甲乙', 0, False),
    (['甲乙乙'], [1], [0, 3], [{'start': 0, 'end': 3}] * 2, '甲乙', 1, True),
    (['一一一一'], [1], [0, 4], [], '一一', 3, False),
    (['甲', '乙'], [1, 2], [0, 2], [], '甲乙', 1, False),
    (['甲', '乙'], [1, 3], [0, 2], [], '甲乙', 0, False),
    (['甲', '乙'], [1, 2], [0, 1, 2], [], '甲乙', 0, False),
], ids=['layout-cross-chapter', 'layout-continuous-pages', 'layout-missing-page',
        'canonical-layout-duplicate', 'overlapping', 'canonical-continuous-pages',
        'canonical-missing-page', 'canonical-cross-chapter'])
def test_three_search_views_agree(texts, pages, bounds, extras, query, expected, layout):
    corpus, vol, segments = miniature(texts, pages, bounds, extras)
    aggregate = corpus.search_chaptered(query)
    details = [hit for seg in segments for hit in corpus.chapter_hits(
        vol.source_file, seg['chapter_pdf_page'], query)['hits']]
    exported = list(corpus.iter_exact_export_hits(query, limit=100))
    assert aggregate['total_hits'] == corpus.count_exact_export(query) == len(details) == len(exported) == expected
    assert details == exported
    if expected:
        assert exported[0]['layout'] is layout


@pytest.mark.parametrize('scope,expected', [(None, 2), ({'book': {1}}, 1),
    ({'book': {2}}, 1), ({'book': {3}}, 0), ({'elsewhere'}, 0), ({}, 0)])
def test_book_and_volume_scope(scope, expected):
    corpus, vol, _ = miniature(['甲乙'], [1], [0, 2], [])
    corpus.books['book'].append(Volume.build('book', 2, 'second.pdf', '第二卷', vol.pages))
    assert corpus.search_chaptered('甲乙', book_scope=scope)['total_hits'] == expected
    assert corpus.count_exact_export('甲乙', book_scope=scope) == expected
    assert len(list(corpus.iter_exact_export_hits('甲乙', book_scope=scope))) == expected


def test_count_budget_does_not_materialize_or_scan_later_segments():
    corpus, _vol, segments = miniature(['甲乙甲乙'], [1], [0, 4], [])
    corpus._chapter_segments = lambda _vol: [segments[0], {'unreachable': True}]
    corpus._make_hit = lambda *a, **k: pytest.fail('count must not materialize hits')
    assert corpus.count_exact_export('甲乙', stop_after=1) == 2
    assert list(corpus.iter_exact_export_hits('甲乙', limit=0)) == []


def test_incomplete_layout_rejects_count_and_collection():
    corpus, _vol, _ = miniature(['甲乙'], [1], [0, 2], [])
    corpus._layout_scan = lambda *a, **kw: ({}, False, 'budget exceeded')
    assert corpus.search_chaptered('甲乙')['exact_search_complete'] is False
    with pytest.raises(RuntimeError, match='未完成'):
        corpus.count_exact_export('甲乙')
    with pytest.raises(RuntimeError, match='未完成'):
        list(corpus.iter_exact_export_hits('甲乙'))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, 'DB_PATH', tmp_path / 'exports.sqlite3')
    monkeypatch.setattr(tasks, 'ARTIFACT_ROOT', tmp_path / 'artifacts')
    monkeypatch.setattr(tasks, '_INITIALIZED', False)
    return tmp_path


def create_claimed(fmt='html', *, member=True, template='template', claim=True):
    job, _ = tasks.create_job(user_id=1, query_text='合成测试', normalized_query='合成测试',
        search_mode='exact', scope=['book:book'], book_filter='',
        citation_style=sorted(tasks.VALID_STYLES)[0], output_format=fmt,
        corpus_version='corpus', template_version=template, base_url='https://example.test', active_member=member)
    return tasks.claim_next_job('worker') if claim else job


def sample_hits(n):
    return [dict(book='book', volume=1, source_file='fixture.pdf', section_title='测试篇章',
                 context=f'完整句子第{i}处[[H]]合成测试[[/H]]。', citation='合成出处',
                 book_title='测试书', norm_start=i, pdf_pages=[1],
                 viewer_url='https://example.test/viewer?page=1') for i in range(n)]


@pytest.mark.parametrize('fmt', ['html', 'docx'])
def test_real_documents_preserve_all_hits_and_citations(store, fmt):
    job = create_claimed(fmt)
    hits = sample_hits(3)
    done = tasks.run_job(job['id'], worker_id='worker', count_hits=lambda *_: len(hits),
                         iter_hits=lambda *_: iter(hits))
    assert done['status'] == 'complete', done['error']
    artifact = tasks.completed_artifact(done)
    if fmt == 'html':
        document = artifact.read_text('utf-8')
    else:
        with zipfile.ZipFile(artifact) as package:
            document = package.read('word/document.xml').decode()
    for i in range(3):
        assert f'完整句子第{i}处' in document
    assert document.count('合成出处') == 3
    assert done['result_count'] == done['progress_done'] == 3
    assert done['manifest']['parts'][0]['sha256'] == tasks._file_digest(artifact)
    original = artifact.read_bytes()
    artifact.write_bytes(b'X' + original[1:])
    assert tasks.completed_artifact(done) is None  # same-sized corruption is not downloadable


@pytest.mark.parametrize('count,member,status,parts', [
    (0, False, 'complete', 1), (1000, False, 'complete', 1),
    (1001, False, 'too_large', 0), (1001, True, 'complete', 3),
    (5000, True, 'complete', 10), (5001, True, 'too_large', 0),
])
def test_entitlement_and_part_boundaries(store, monkeypatch, count, member, status, parts):
    rendered = []
    def write(path, fmt, hits, meta):
        rendered.append(len(hits))
        path.write_text(str(len(hits)), encoding='utf-8')
    monkeypatch.setattr(tasks, '_write_part', write)
    job = create_claimed(member=member)
    done = tasks.run_job(job['id'], worker_id='worker', count_hits=lambda *_: count,
                         iter_hits=lambda _job, limit: iter(sample_hits(min(count, limit))))
    assert done['status'] == status, done['error']
    assert len(rendered) == parts
    if parts:
        assert sum(rendered) == count
        if count > 1000:
            assert max(rendered) <= 500
    else:
        assert tasks.completed_artifact(done) is None


@pytest.mark.parametrize('attempt', [1, 2])
def test_mismatch_retry_budget_and_diagnostics(store, attempt):
    job = create_claimed()
    with tasks._connect() as conn:
        conn.execute('UPDATE search_export_jobs SET attempts=? WHERE id=?', (attempt, job['id']))
    done = tasks.run_job(job['id'], worker_id='worker', count_hits=lambda *_: 2,
                         iter_hits=lambda *_: iter(sample_hits(1)))
    payload = tasks.public_payload(done)
    assert done['status'] == ('queued' if attempt == 1 else 'failed')
    assert payload['error_code'] == 'count_mismatch'
    assert payload['attempts'] == attempt and payload['max_attempts'] == 2
    assert payload['retryable'] is (attempt == 1)
    assert ('自动重试已结束' in payload['error']) is (attempt == 2)
    assert '发生变化' not in payload['error']
    assert done['manifest']['diagnostics']['actual_count'] == 1
    assert done['manifest']['diagnostics']['expected_count'] == 2
    assert '合成测试' not in json.dumps(done['manifest'], ensure_ascii=False)
    assert tasks.completed_artifact(done) is None


def test_actual_source_change_is_terminal(store):
    job = create_claimed()
    current = {'version': 1}
    def count(*_):
        current['version'] = 2
        return 1
    def validate():
        if current['version'] != 1:
            raise tasks.ExportSourceChangedError('检索来源版本已变更，请重新创建任务。')
    done = tasks.run_job(job['id'], worker_id='worker', count_hits=count,
                         iter_hits=lambda *_: pytest.fail('changed source must not be collected'), validate_sources=validate)
    assert done['status'] == 'failed'
    assert tasks.public_payload(done)['error_code'] == 'source_changed'
    assert tasks.public_payload(done)['retryable'] is False
    assert tasks.completed_artifact(done) is None


def test_multipart_mismatch_clears_reusable_parts_and_never_publishes(store, monkeypatch):
    monkeypatch.setattr(tasks, '_write_part', lambda path, *_: path.write_bytes(b'part'))
    job = create_claimed()
    done = tasks.run_job(job['id'], worker_id='worker', count_hits=lambda *_: 1001,
                         iter_hits=lambda *_: iter(sample_hits(1002)))
    assert done['status'] == 'queued'
    assert done['manifest']['parts'] == []
    assert done['manifest']['diagnostics']['actual_count'] == 1002
    assert tasks.completed_artifact(done) is None


def test_source_change_after_render_prevents_completion(store, monkeypatch):
    changed = []
    def write(path, *_):
        path.write_bytes(b'part')
        changed.append(True)
    def validate():
        if changed:
            raise tasks.ExportSourceChangedError('来源已变更')
    monkeypatch.setattr(tasks, '_write_part', write)
    job = create_claimed()
    done = tasks.run_job(job['id'], worker_id='worker', count_hits=lambda *_: 1,
                         iter_hits=lambda *_: iter(sample_hits(1)), validate_sources=validate)
    assert done['status'] == 'failed'
    assert tasks.public_payload(done)['error_code'] == 'source_changed'
    assert tasks.completed_artifact(done) is None


@pytest.mark.parametrize('condition', ['valid', 'legacy', 'content_changed', 'bytes_changed'])
def test_part_reuse_requires_content_and_file_integrity(store, monkeypatch, condition):
    job = create_claimed()
    hits = sample_hits(1)
    meta = dict(title='“合成测试”引文检索汇编', query='合成测试', total=1,
                citation_style=job['citation_style'], citation_label=tasks._citation_label(job['citation_style']), start_index=1)
    identity = tasks._part_identity(job, hits, meta)
    target = tasks._job_directory(job['id'], create=True) / '合成测试_引文检索汇编.html'
    target.write_bytes(b'old')
    part = dict(name=target.name, size=3, count=1, identity=identity, sha256=tasks._file_digest(target))
    if condition == 'legacy':
        part.pop('identity')
    elif condition == 'content_changed':
        hits[0]['context'] = '另一条完整原文。'
    elif condition == 'bytes_changed':
        target.write_bytes(b'bad')
    tasks.update_job(job['id'], manifest_json={'parts': [part]})
    rendered = []
    def write(path, *_):
        rendered.append(True)
        path.write_bytes(b'new')
    monkeypatch.setattr(tasks, '_write_part', write)
    done = tasks.run_job(job['id'], worker_id='worker', count_hits=lambda *_: 1, iter_hits=lambda *_: iter(hits))
    assert done['status'] == 'complete'
    assert bool(rendered) is (condition != 'valid')
    assert target.read_bytes() == (b'old' if condition == 'valid' else b'new')


@pytest.mark.parametrize('generation_claim', [False, True])
def test_old_template_tasks_retire_without_consuming_attempts(store, generation_claim):
    old = create_claimed(template='old', claim=False)
    if generation_claim:
        from ingestion.claims import search_export
        claimed = search_export(tasks, ['corpus', 'ancestor'], 'worker', template_version='new')
    else:
        claimed = tasks.claim_next_job('worker', corpus_version='corpus', template_version='new')
    assert claimed is None
    done = tasks.get_job(old['id'])
    assert done['status'] == 'failed' and done['attempts'] == 0
    assert tasks.public_payload(done)['error_code'] == 'source_changed'


def test_application_worker_pins_context_and_checks_personal_ownership(store, monkeypatch):
    import app as web
    job = create_claimed()
    events = []
    personal = SimpleNamespace(count_exact_export=lambda *a, **k: 1,
        iter_exact_export_hits=lambda *a, **k: iter([{'book': 'mylib:7', 'context': '私有原文'}]))
    public = SimpleNamespace(count_exact_export=lambda *a, **k: 0,
        iter_exact_export_hits=lambda *a, **k: iter([]))
    monkeypatch.setattr(web, 'corpus', public)
    monkeypatch.setattr(web, '_citation_corpus_sha256', lambda: 'corpus')
    monkeypatch.setattr(web, '_search_export_template_version', lambda: 'template')
    monkeypatch.setattr(web, '_search_export_scope_context', lambda _: ({'book': {1}}, True, [7], False))
    monkeypatch.setattr(web, '_search_export_personal_context', lambda *a: (personal, {'mylib:7': None}))
    monkeypatch.setattr(web.mylib_corpus, 'personal_corpus_version', lambda uid: 'private-version')
    monkeypatch.setattr(web, '_citation_personal_scope_rows', lambda uid, tokens: events.append((uid, tokens)))
    monkeypatch.setattr(web.mylib_corpus, 'submission_id_from_key', lambda key: 7)
    monkeypatch.setattr(web.mylib, 'get_submission', lambda sid, uid: None)
    web._search_export_worker(job['id'], 'worker')
    done = tasks.get_job(job['id'])
    assert events == [(1, ['mylib:7'])]
    assert done['status'] == 'failed'
    assert tasks.public_payload(done)['error_code'] == 'source_changed'
    assert '个人文库资料在导出期间已不可用' in done['error']
    assert tasks.completed_artifact(done) is None


def test_count_and_iter_use_pinned_public_and_personal_objects(monkeypatch):
    import app as web
    seen = []
    def source(name):
        return SimpleNamespace(count_exact_export=lambda q, **kw: seen.append((name, kw['book_scope'])) or 1,
            iter_exact_export_hits=lambda q, **kw: iter([{'source': name}]))
    context = dict(public=source('public'), personal=source('personal'), public_enabled=True,
                   public_spec={'book': {2}}, personal_spec={'private': None})
    monkeypatch.setattr(web, '_search_export_context', lambda _: pytest.fail('must reuse context'))
    monkeypatch.setattr(web, '_search_export_prepare_hit', lambda _job, hit, **kw: hit)
    job = {'query_text': '甲乙', 'search_mode': 'exact'}
    assert web._search_export_count(job, 5, context=context) == 2
    assert list(web._search_export_iter(job, 5, context=context)) == [{'source': 'personal'}, {'source': 'public'}]
    assert seen == [('public', {'book': {2}}), ('personal', {'private': None})]


def test_personal_version_tracks_data_instead_of_cached_object(monkeypatch):
    import personal_corpus
    rows = [{'id': 7, 'parsed_at': 'first', 'bibliographic_json': '{}'}]
    monkeypatch.setattr(personal_corpus.plib, 'list_searchable_books', lambda _uid: [dict(row) for row in rows])
    before = personal_corpus.personal_corpus_version(1)
    personal_corpus.invalidate(1)
    assert personal_corpus.personal_corpus_version(1) == before
    rows[0]['parsed_at'] = 'second'
    assert personal_corpus.personal_corpus_version(1) != before


def test_export_content_revision_is_part_of_worker_compatibility(monkeypatch):
    import app as web
    monkeypatch.setattr(web, '_citation_template_version', lambda: 'unchanged-citation')
    monkeypatch.setattr(web, 'corpus', SimpleNamespace(layout_index=None))
    first = web._search_export_template_version()
    monkeypatch.setattr(tasks, 'EXPORT_CONTENT_VERSION', 'different-export-semantics')
    assert web._search_export_template_version() != first
