from types import SimpleNamespace
import threading

import pytest

from agent_api_service import QueryService
from agent_api_store import APIError, Store
from build_index import normalize
from search import Corpus, Page, Volume


def miniature(texts, numbers, bounds, extras=(), complete=True):
    vol = Volume.build("文集", 1, "fixture.pdf", "测试卷", [Page(n, str(n), text, normalize(text)) for text, n in zip(texts, numbers)])
    corpus = object.__new__(Corpus)
    corpus.books = {"文集": [vol]}
    corpus._chapter_segments = lambda _: [{"norm_start": a, "norm_end": b} for a, b in zip(bounds, bounds[1:])]
    corpus._layout_scan = lambda *a, **kw: ({"fixture.pdf": list(extras)}, complete, "")
    corpus._make_hit = lambda volume, start, end, *args, **kw: SimpleNamespace(norm_start=start, norm_end=end, layout=False)
    corpus._make_layout_hit = lambda volume, match, q: SimpleNamespace(norm_start=match["start"], norm_end=match["end"], layout=True)
    return corpus, vol


@pytest.mark.parametrize("numbers,bounds,expected", [([1, 2], [0, 4], 1), ([1, 3], [0, 4], 0), ([1, 2], [0, 2, 4], 0)])
def test_api_cross_page_and_work_boundaries(numbers, bounds, expected):
    corpus, _ = miniature(["甲乙", "丙丁"], numbers, bounds)
    hits, truncated, complete = corpus.query_api("乙丙", mode="exact", book_scope={"文集": {1}}, limit=10, check=lambda: None)
    assert len(hits) == expected and not truncated and complete


def test_api_layout_preference_dedupe_and_partial_flag():
    corpus, _ = miniature(["甲乙乙"], [1], [0, 3], [{"start": 0, "end": 3}] * 2, complete=False)
    hits, truncated, complete = corpus.query_api("甲乙", mode="exact", book_scope={"文集": None}, limit=10, check=lambda: None)
    assert len(hits) == 1 and hits[0].layout
    assert not complete and not truncated


def test_raw_context_is_source_text_clipped_to_work(tmp_path):
    corpus, vol = miniature(["前作结束甲乙", "丙丁本作结束后作"], [1, 2], [0, 14])
    corpus.get_volume_by_source_file = lambda _: vol
    corpus.document_scope_for_hit = lambda _: SimpleNamespace(norm_start=4, norm_end=8)
    hit = SimpleNamespace(source_file="fixture.pdf", norm_start=5, norm_end=7)
    service = QueryService(corpus, Store(tmp_path), lambda: {"文集"}, lambda: "one", lambda: "")
    text, pages, clipped = service.raw_window(hit, 6000)
    assert text == "甲乙\n丙丁"
    assert [p["pdf_page"] for p in pages] == [1, 2]
    assert not clipped


def test_active_work_keeps_slot_until_actual_exit(tmp_path):
    store = Store(tmp_path)
    accepted, finish, caller_gone = threading.Event(), threading.Event(), threading.Event()
    def work():
        with store.admission(1):
            accepted.set()
            finish.wait(5)
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert accepted.wait(3)
        caller_gone.set()  # Transport cancellation is not actual worker completion.
        with pytest.raises(APIError):
            with store.admission(1): pass
    finally:
        finish.set()
        worker.join(5)
    with store.admission(1): pass


def test_cooccurrence_checks_budget_even_before_a_window_is_emitted():
    checks = []
    def check():
        checks.append(1)
        if len(checks) == 4: raise APIError("query_timeout", "budget", 504)
    with pytest.raises(APIError):
        list(Corpus._iter_export_cooccurrence_windows("甲乙" * 10000 + "丙丁", ["甲乙", "丙丁"], check=check))
    assert len(checks) == 4
