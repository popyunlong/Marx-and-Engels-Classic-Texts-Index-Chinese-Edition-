from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import deployment_smoke


ROOT = Path(__file__).resolve().parents[1]


def _check(monkeypatch: pytest.MonkeyPatch, *, ready: bool, failed_query: str = "") -> None:
    index = SimpleNamespace(
        enabled=True,
        error="" if ready else "layout corpus/TOC fingerprint mismatch",
        projections={"volume": object()} if ready else {},
    )

    def scan(query: str):
        if query == failed_query:
            return {}, False, "layout scan budget exceeded"
        return {}, True, ""

    app = SimpleNamespace(corpus=SimpleNamespace(layout_index=index, _layout_scan=scan))
    monkeypatch.setitem(sys.modules, "app", app)
    monkeypatch.setenv("APP_RELEASE_FILE", str(ROOT / "release.json"))
    deployment_smoke.check_app_import_and_routes(ROOT, "server", True)


def test_release_smoke_rejects_stale_layout_index(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        _check(monkeypatch, ready=False)


def test_release_smoke_rejects_incomplete_common_query(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="劳动"):
        _check(monkeypatch, ready=True, failed_query="劳动")


def test_release_smoke_accepts_complete_common_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    _check(monkeypatch, ready=True)
