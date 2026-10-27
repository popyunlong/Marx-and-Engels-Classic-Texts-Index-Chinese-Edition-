import random
import re
import threading
from types import SimpleNamespace

import pytest
from _test_env import APPDATA  # noqa: F401
from offset_match import literal_gap_spans
from search import Corpus
import search_capacity


def test_small_contract_equivalent_to_old_literal_gap_semantics():
    # Small bounded fixtures only: never replay the old timed-out corpus case.
    rng = random.Random(20261027)
    for _ in range(250):
        text = "".join(rng.choice("甲乙a_ ·!\n") for _ in range(35))
        chars = [rng.choice("甲乙a·") for _ in range(rng.randrange(1, 5))]
        expected = [match.span() for match in re.finditer(r"\W*".join(map(re.escape, chars)), text)]
        assert literal_gap_spans(text, chars) == expected


def test_synthetic_long_punctuation_is_bounded_and_preserves_offsets():
    text = "序" + "·" * 3000 + "终"
    assert literal_gap_spans(text, list("序" + "·" * 80 + "缺")) == []
    assert literal_gap_spans(text, list("序··终")) == [(0, len(text))]


def test_single_multi_and_repeated_occurrence_highlighting():
    corpus = Corpus.__new__(Corpus)
    pages = [SimpleNamespace(raw_text="甲，乙。甲\n乙。丙丁。")]
    assert corpus._extract_context(pages, "甲乙", 1) == "甲，乙。[[H]]甲 乙[[/H]]。丙丁。"
    value = corpus._extract_context_multi(pages, ["甲乙", "丙丁"])
    assert value.count("[[H]]") == 3 and "[[H]]丙丁[[/H]]" in value


def test_shared_capacity_nested_release_and_busy(monkeypatch):
    slot = threading.BoundedSemaphore(1)
    monkeypatch.setattr(search_capacity, "_slots", slot)
    @search_capacity.bounded_search
    def inner(): return 1
    @search_capacity.bounded_search
    def outer(): return inner()
    assert outer() == 1
    slot.acquire()
    try:
        with pytest.raises(search_capacity.SearchBusy): outer()
    finally:
        slot.release()
    @search_capacity.bounded_search
    def broken(): raise ValueError()
    with pytest.raises(ValueError): broken()
    assert slot.acquire(blocking=False)
    slot.release()
