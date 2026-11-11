"""An export from a selected book tab must keep the tab's chapter intersection."""
import pytest
from chapter_fixture import make_corpus
from chapter_search import chapter_rows, resolve, ChapterScopeError


def test_book_tab_export_snapshot_restricts_worker_and_fingerprint(monkeypatch):
    import app as web
    import search_exports as tasks
    corpus, _ = make_corpus()
    tokens = [chapter_rows(corpus, corpus.books[b][0])[1]['token'] for b in ('文集', '全集')]
    monkeypatch.setattr(web, 'corpus', corpus)
    monkeypatch.setattr(web, '_public_book_keys', lambda **k: set(corpus.books))
    monkeypatch.setattr(web, '_chapter_scope_version', lambda: 'v1')
    payload = {'scope': tokens, 'scope_version': 'v1', 'book': '全集'}
    scoped = web._validated_search_export_scope(payload, {'id': 1})
    whole = resolve(corpus, tokens, 'v1', 'v1', corpus.books).snapshot()
    assert scoped['book_filter'] == '全集'
    assert tasks.request_fingerprint(scoped) != tasks.request_fingerprint(whole)
    job = {'scope': scoped, 'search_mode': 'exact', 'query_text': '劳动'}
    with web.app.app_context():
        spec, enabled, private, default_private = web._search_export_scope_context(job)
        context = web._search_export_context(job)
        hits = list(web._search_export_iter(job, 20, context=context))
    assert enabled and not private and not default_private
    assert set(spec) == {'全集'}
    assert len(hits) == web._search_export_count(job, 20, context=context) == 1
    assert all(h['book'] == '全集' and '边界外部秘密' not in h['context'] for h in hits)
    with pytest.raises(ChapterScopeError):
        web._validated_search_export_scope(dict(payload, book='不存在的书'), {'id': 1})
