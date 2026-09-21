"""Public acceptance checks for the two-volume recommendation addendum."""
from __future__ import annotations

import argparse
import glob
import http.cookiejar
import json
import os
import re
import sqlite3
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


BOOK_KEY = "user_rec_xi_culture_selected_2026"
EXPECTED_COUNTS = {"pages": 314803, "toc": 53556, "books": 171, "sources": 612}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _phrase_near(pages: list[dict], fraction: float) -> tuple[str, int]:
    target = round((len(pages) - 1) * fraction)
    for index in sorted(range(len(pages)), key=lambda item: abs(item - target)):
        text = str(pages[index].get("text") or "")
        runs = re.findall(r"[\u3400-\u9fff]{12,}", text)
        if runs:
            run = max(runs, key=len)
            return run[2:14], int(pages[index].get("page") or index + 1)
    raise RuntimeError("no searchable phrase found")


def _corpus_counts(path: str) -> dict[str, int]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return {
            "pages": int(connection.execute("select count(*) from pages").fetchone()[0]),
            "toc": int(connection.execute("select count(*) from toc_entries").fetchone()[0]),
            "books": int(connection.execute("select count(distinct book) from pages").fetchone()[0]),
            "sources": int(connection.execute("select count(distinct source_file) from pages").fetchone()[0]),
        }


def _recommendation_state(path: str) -> tuple[dict[str, str], set[str]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "select id,status,user_email from user_book_recommendations where id in (8,9) order by id"
        ).fetchall()
    return ({str(row[0]): str(row[1]) for row in rows}, {str(row[2]) for row in rows if "@" in str(row[2])})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--release-root", default="/home/data/marx-user-recommended-20260913-02")
    parser.add_argument("--corpus-db", default="/opt/marx-search/data/corpus.sqlite")
    args = parser.parse_args()
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    request_sequence = 0

    def get(path: str, headers: dict | None = None):
        nonlocal request_sequence
        request_sequence += 1
        request_headers = {
            "X-Community-Analytics": "exclude",
            "X-Forwarded-For": f"14.0.0.{request_sequence % 240 + 1}",
            "User-Agent": "Mozilla/5.0 Chrome/140 Safari/537.36",
        }
        request_headers.update(headers or {})
        request = Request(args.base_url + path, headers=request_headers)
        try:
            with opener.open(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    status, _, raw = get("/v2/read")
    html = raw.decode("utf-8", "replace")
    match = re.search(r'const csrfToken = ("(?:[^"\\]|\\.)*")', html)
    if not match:
        _, _, home_raw = get("/")
        match = re.search(r'const csrfToken = ("(?:[^"\\]|\\.)*")', home_raw.decode("utf-8", "replace"))
    if not match:
        raise RuntimeError("csrf token missing")
    csrf = json.loads(match.group(1))
    cookie_header = "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)

    def post(path: str, payload: dict):
        nonlocal request_sequence
        request_sequence += 1
        request = Request(
            args.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json", "X-CSRF-Token": csrf,
                "X-Community-Analytics": "exclude",
                "X-Forwarded-For": f"14.0.0.{request_sequence % 240 + 1}",
                "User-Agent": "Mozilla/5.0 Chrome/140 Safari/537.36", "Cookie": cookie_header,
            }, method="POST",
        )
        try:
            with opener.open(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body)
            except Exception:
                return exc.code, {"raw": body[:120].decode("utf-8", "replace")}

    packages = []
    for path in sorted(glob.glob(os.path.join(args.release_root, "packages", "*.json"))):
        with open(path, encoding="utf-8") as handle:
            packages.append(json.load(handle))
    volume_checks = []
    for package in packages:
        search_checks = []
        first_hit = None
        for fraction in (0.12, 0.50, 0.88):
            phrase, page = _phrase_near(package["pages"], fraction)
            search_status, body = post("/api/search", {"q": phrase, "scope": ["book:" + BOOK_KEY]})
            hit = next((item for item in _walk(body) if item.get("book") == BOOK_KEY), None)
            if first_hit is None and hit:
                first_hit = hit
            search_checks.append({
                "page": page, "http": search_status, "count": int(body.get("count") or 0),
                "scoped": BOOK_KEY in json.dumps(body, ensure_ascii=False),
            })
        source = package["source_file"]
        toc_status, _, toc_raw = get(
            "/api/library/volume-toc?" + urlencode({"file": source, "mode": "reader"})
        )
        toc_body = json.loads(toc_raw) if toc_status == 200 else {}
        toc = toc_body.get("results") or []
        image_status, image_headers, _ = get(
            "/page-image?" + urlencode({"file": source, "page": package["page_count"] // 2}),
            {"Accept": "image/webp"},
        )
        cover_status, cover_headers, _ = get(
            "/reader/cover?" + urlencode({"file": source}), {"Accept": "image/webp"}
        )
        pdf_status, _, _ = get("/pdf?" + urlencode({"file": source}))
        citation = json.dumps(
            (first_hit or {}).get("citations") or (first_hit or {}).get("citation") or "",
            ensure_ascii=False,
        )
        volume_checks.append({
            "volume": int(package["metadata"]["volume"]),
            "search": search_checks,
            "toc_http": toc_status,
            "toc": len(toc),
            "toc_expected": len(package["toc"]),
            "toc_ordered": [int(row.get("pdf_page") or 0) for row in toc] == sorted(
                int(row.get("pdf_page") or 0) for row in toc
            ),
            "citation": bool(first_hit and citation),
            "printed_page_citation": "此为PDF页码，非原书印刷页码" not in citation,
            "viewer_link": bool(first_hit and (first_hit.get("viewer_url") or "/viewer" in json.dumps(first_hit, ensure_ascii=False))),
            "page_image_http": image_status,
            "page_image_type": image_headers.get("Content-Type", ""),
            "cover_http": cover_status,
            "cover_type": cover_headers.get("Content-Type", ""),
            "pdf_http": pdf_status,
        })

    co_status, co_body = post(
        "/api/search", {"q": "文化 自信", "mode": "cooccurrence", "scope": ["book:" + BOOK_KEY]}
    )
    chapter_status, chapter_body = post(
        "/api/search", {"q": "文化", "scope": ["book:" + BOOK_KEY]}
    )
    ai_search_status, _ = post(
        "/api/ai/search-chat", {"q": "测试", "scope": ["book:" + BOOK_KEY]}
    )
    ai_pdf_status, _ = post(
        "/api/ai/pdf-chat", {"q": "测试", "file": packages[0]["source_file"], "page": 9}
    )
    export_status, _ = post(
        "/api/search/exports", {"q": "测试", "scope": ["book:" + BOOK_KEY]}
    )
    ai_config_status, _, ai_config_raw = get("/api/ai/assistant-config")
    try:
        ai_config = json.loads(ai_config_raw)
        ai_scope_text = json.dumps(ai_config, ensure_ascii=False)
    except Exception:
        ai_config = {}
        ai_scope_text = ai_config_raw.decode("utf-8", "replace")
    ai_book = next(
        (
            book for group in ai_config.get("book_scope_tree") or []
            for book in group.get("books") or []
            if book.get("key") == BOOK_KEY
        ),
        {},
    )
    recommendation_db = "/var/www/.marx_search_full/personal_library/personal_library.sqlite3"
    recommendation_statuses, full_emails = _recommendation_state(recommendation_db)
    artifact_text = ""
    for path in sorted(glob.glob(os.path.join(args.release_root, "packages", "*.json"))):
        with open(path, encoding="utf-8") as handle:
            artifact_text += handle.read()
    published_path = os.path.join(args.release_root, "published.json")
    published = json.loads(open(published_path, encoding="utf-8").read()) if os.path.exists(published_path) else {}
    output = {
        "read_http": status,
        "logical_book_card": html.count("《习近平文化文选》") >= 1,
        "two_volume_card": "2 卷" in html,
        "recommender_exact": html.count("abc（267xxxx670@qq.com）") == 1,
        "quality_note_hidden": "识别文本持续校对" not in html and "请以 PDF 原文为准" not in html,
        "full_email_absent_public": all(email not in html for email in full_emails),
        "full_email_absent_artifacts": all(email not in artifact_text for email in full_emails),
        "volumes": volume_checks,
        "cooccurrence": {"http": co_status, "count": int(co_body.get("count") or 0), "scoped": BOOK_KEY in json.dumps(co_body, ensure_ascii=False)},
        "chapter_aggregate": {"http": chapter_status, "count": int(chapter_body.get("count") or 0), "display_mode": chapter_body.get("display_mode")},
        "guest_permissions": {"ai_search": ai_search_status, "ai_pdf": ai_pdf_status, "research_export": export_status},
        "ai_scope_visible": {
            "http": ai_config_status,
            "collection": "用户荐书" in ai_scope_text,
            "book": "习近平文化文选" in ai_scope_text,
            "two_volumes": ai_book.get("volumes") == [1, 2],
        },
        "recommendation_statuses": recommendation_statuses,
        "corpus_counts": _corpus_counts(args.corpus_db),
        "corpus_counts_expected": EXPECTED_COUNTS,
        "public_window": {"from": published.get("public_from"), "until": published.get("public_until")},
    }
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
