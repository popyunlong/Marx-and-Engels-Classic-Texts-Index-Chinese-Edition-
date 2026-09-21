# -*- coding: utf-8 -*-
"""In-process acceptance used by the isolated server candidate before listening."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import html
import json
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_candidate_acceptance import verify_candidate_runtime
from scripts.accept_new_corpus_202609 import accept
from scripts.build_new_corpus_202609 import NEW_BOOKS


def verify_new_runtime(app_module: Any, project_root: Path) -> dict:
    corpus = app_module.corpus
    if corpus is None:
        raise RuntimeError("candidate corpus unavailable")
    root = Path(project_root).resolve()
    db_path = (root / "data" / "corpus.sqlite").resolve()
    # Reuse the application's already-loaded corpus.  Constructing a second
    # 40k-page Corpus here can double candidate memory during the safety gate.
    corpus_result = accept(db_path, root / "pdfs", corpus=corpus)
    if not corpus_result.get("ok"):
        raise RuntimeError("new-corpus acceptance failed: " + "; ".join(corpus_result.get("failures") or []))
    generic = verify_candidate_runtime(corpus, root)
    missing = [book for book in NEW_BOOKS if not corpus.get_volumes(book)]
    if missing:
        raise RuntimeError("candidate runtime missing books: " + ",".join(missing))
    hidden = [book for book in NEW_BOOKS if not corpus.get_book_config(book).available]
    if hidden:
        raise RuntimeError("candidate publish overlay left books hidden: " + ",".join(hidden))

    # Exercise the actual Flask routes before Waitress listens. Server mode normally
    # protects reader/search assets; predicates are patched only inside this isolated
    # pre-listen test and restored before the candidate can accept network traffic.
    source_file = corpus.get_volumes(NEW_BOOKS[0])[0].source_file
    query = str(corpus_result["samples"][0]["query"])
    old_reader_guard = app_module._require_reader_asset_access
    old_admin_guard = app_module._admin_content_access_enabled
    old_search_guard = app_module._require_search
    old_content_guard = app_module._require_content_feature
    old_feature_guard = app_module._require_feature
    try:
        app_module._require_reader_asset_access = lambda: None
        app_module._admin_content_access_enabled = lambda: True
        app_module._require_search = lambda: None
        app_module._require_content_feature = lambda _name: None
        app_module._require_feature = lambda _name: None
        with app_module.app.test_client() as client:
            # Production protects every state-changing request, including search,
            # with the same session/header CSRF token pair emitted by the UI.
            csrf_token = "new-corpus-candidate-preflight"
            with client.session_transaction() as candidate_session:
                candidate_session["_csrf_token"] = csrf_token
            pdf_response = client.get(
                "/pdf", query_string={"file": source_file},
                headers={"Range": "bytes=0-1023"},
            )
            toc_response = client.get("/api/library/volume-toc", query_string={"file": source_file})
            search_response = client.post(
                "/api/search", json={"q": query, "book": NEW_BOOKS[0]},
                headers={"X-CSRF-Token": csrf_token},
            )
            reader_response = client.get("/v2/read")
            cover_response = client.get("/reader/cover", query_string={"file": source_file})
            context_response = client.get(
                "/api/pdf-page-context", query_string={"file": source_file, "page": 1}
            )
            ai_response = client.get("/api/ai/assistant-config")
        if pdf_response.status_code != 206:
            raise RuntimeError(f"candidate PDF Range status={pdf_response.status_code}")
        if not str(pdf_response.headers.get("Content-Range") or "").startswith("bytes 0-1023/"):
            raise RuntimeError("candidate PDF Range header invalid")
        if len(pdf_response.data) != 1024:
            raise RuntimeError(f"candidate PDF Range bytes={len(pdf_response.data)}")
        toc_payload = toc_response.get_json(silent=True) or {}
        if toc_response.status_code != 200 or not toc_payload.get("results"):
            raise RuntimeError(f"candidate TOC route failed status={toc_response.status_code}")
        search_payload = search_response.get_json(silent=True) or {}
        if search_response.status_code != 200 or not search_payload.get("ok"):
            raise RuntimeError(f"candidate search route failed status={search_response.status_code}")
        if NEW_BOOKS[0] not in json.dumps(search_payload, ensure_ascii=False):
            raise RuntimeError("candidate search route did not expose the new book")
        if reader_response.status_code != 200:
            raise RuntimeError(f"candidate reader route status={reader_response.status_code}")
        nav_match = re.search(
            rb'<script id="v2ReadNav" type="application/json">(.*?)</script>',
            reader_response.data,
            flags=re.DOTALL,
        )
        if not nav_match:
            raise RuntimeError("candidate reader navigation payload missing")
        reader_nav = json.loads(html.unescape(nav_match.group(1).decode("utf-8")))
        manifest = yaml.safe_load((root / "config" / "manifest.yaml").read_text(encoding="utf-8")) or {}
        nav_by_key = {str(item.get("key") or ""): item for item in reader_nav}
        for book in NEW_BOOKS:
            expected_sources = {str(row["file"]) for row in manifest.get(book, [])}
            actual = nav_by_key.get(book) or {}
            actual_volumes = actual.get("volumes") or []
            actual_sources = {str(row.get("file") or "") for row in actual_volumes}
            if actual_sources != expected_sources or not all(row.get("indexed") for row in actual_volumes):
                raise RuntimeError(
                    f"candidate reader catalog mismatch for {book}: "
                    f"expected={sorted(expected_sources)!r} actual={sorted(actual_sources)!r}"
                )
        if cover_response.status_code != 200 or not cover_response.data:
            raise RuntimeError(f"candidate cover route failed status={cover_response.status_code}")
        context_payload = context_response.get_json(silent=True) or {}
        if context_response.status_code != 200 or not context_payload.get("ok"):
            raise RuntimeError(f"candidate AI page context failed status={context_response.status_code}")
        ai_payload = ai_response.get_json(silent=True) or {}
        if ai_response.status_code != 200 or NEW_BOOKS[0] not in json.dumps(ai_payload, ensure_ascii=False):
            raise RuntimeError("candidate AI scope tree is missing the new book")
    finally:
        app_module._require_reader_asset_access = old_reader_guard
        app_module._admin_content_access_enabled = old_admin_guard
        app_module._require_search = old_search_guard
        app_module._require_content_feature = old_content_guard
        app_module._require_feature = old_feature_guard
    return {
        "new_corpus": corpus_result, "generic": generic, "pdf_range": True,
        "toc_route": True, "search_route": True, "reader_route": True,
        "cover_route": True, "ai_page_context": True, "ai_scope": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    import app

    result = verify_new_runtime(app, args.root)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    print("NEW_CORPUS_RUNTIME_ACCEPTANCE_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
