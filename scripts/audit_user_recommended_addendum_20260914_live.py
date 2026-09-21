"""Acceptance checks for the two-book recommendation addendum."""
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


BOOKS = {
    "user_rec_marx_biography_mclellan_2016": {
        "title": "马克思传", "toc": 64, "mask": "李博（269xxxx264@qq.com）", "printed": False,
    },
    "user_rec_china_marxist_party_theory_2021": {
        "title": "中国化的马克思主义党建理论体系概论", "toc": 98,
        "mask": "superkar1（karxxxxxxarx@163.com）", "printed": True,
    },
}
EXPECTED_COUNTS = {"pages": 315661, "toc": 53718, "books": 173, "sources": 614}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _decode_cloudflare_emails(html: str) -> str:
    """Restore only Cloudflare-protected text for deterministic live checks."""
    def decode(match: re.Match) -> str:
        payload = match.group(1)
        try:
            raw = bytes.fromhex(payload)
            return bytes(value ^ raw[0] for value in raw[1:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""

    return re.sub(
        r'<span class="__cf_email__" data-cfemail="([0-9a-fA-F]+)">.*?</span>',
        decode,
        html,
    )


def _phrase_near(pages: list[dict], fraction: float) -> tuple[str, int]:
    target = round((len(pages) - 1) * fraction)
    for index in sorted(range(len(pages)), key=lambda item: abs(item - target)):
        runs = re.findall(r"[\u3400-\u9fff]{12,}", str(pages[index].get("text") or ""))
        if runs:
            return max(runs, key=len)[2:14], int(pages[index].get("page") or index + 1)
    raise RuntimeError("no searchable phrase found")


def _counts(path: str) -> dict[str, int]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return {
            "pages": int(connection.execute("select count(*) from pages").fetchone()[0]),
            "toc": int(connection.execute("select count(*) from toc_entries").fetchone()[0]),
            "books": int(connection.execute("select count(distinct book) from pages").fetchone()[0]),
            "sources": int(connection.execute("select count(distinct source_file) from pages").fetchone()[0]),
        }


def _recommendations(path: str) -> tuple[dict[str, str], set[str]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "select id,status,user_email from user_book_recommendations where id in (11,12) order by id"
        ).fetchall()
    return ({str(row[0]): str(row[1]) for row in rows}, {str(row[2]) for row in rows})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--release-root", default="/home/data/marx-user-recommended-20260914-01")
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
            "X-Forwarded-For": f"14.1.0.{request_sequence % 240 + 1}",
            "User-Agent": "Mozilla/5.0 Chrome/140 Safari/537.36",
        }
        request_headers.update(headers or {})
        try:
            with opener.open(Request(args.base_url + path, headers=request_headers), timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    status, _, raw = get("/v2/read")
    html = raw.decode("utf-8", "replace")
    # The public Cloudflare edge obfuscates even already-masked addresses.
    # Decode the data attribute solely for this in-memory acceptance assertion.
    check_html = html + _decode_cloudflare_emails(html)
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
                "X-Forwarded-For": f"14.1.0.{request_sequence % 240 + 1}",
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
    artifact_text = ""
    for path in sorted(glob.glob(os.path.join(args.release_root, "packages", "*.json"))):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        artifact_text += text
        packages.append(json.loads(text))

    book_checks = []
    for package in packages:
        key = package["metadata"]["book_key"]
        expected = BOOKS[key]
        searches, first_hit = [], None
        for fraction in (0.12, 0.50, 0.88):
            phrase, page = _phrase_near(package["pages"], fraction)
            search_status, body = post("/api/search", {"q": phrase, "scope": ["book:" + key]})
            hit = next((item for item in _walk(body) if item.get("book") == key), None)
            first_hit = first_hit or hit
            searches.append({"page": page, "http": search_status, "count": int(body.get("count") or 0), "scoped": bool(hit)})
        source = package["source_file"]
        toc_status, _, toc_raw = get("/api/library/volume-toc?" + urlencode({"file": source, "mode": "reader"}))
        toc_body = json.loads(toc_raw) if toc_status == 200 else {}
        toc = toc_body.get("results") or []
        image_status, image_headers, _ = get(
            "/page-image?" + urlencode({"file": source, "page": package["page_count"] // 2}),
            {"Accept": "image/webp"},
        )
        cover_status, cover_headers, _ = get("/reader/cover?" + urlencode({"file": source}), {"Accept": "image/webp"})
        pdf_status, _, _ = get("/pdf?" + urlencode({"file": source}))
        citation = json.dumps((first_hit or {}).get("citations") or (first_hit or {}).get("citation") or "", ensure_ascii=False)
        fallback = "此为PDF页码，非原书印刷页码" in citation
        book_checks.append({
            "key": key, "search": searches, "toc_http": toc_status, "toc": len(toc),
            "toc_expected": expected["toc"],
            "toc_ordered": [int(row.get("pdf_page") or 0) for row in toc] == sorted(int(row.get("pdf_page") or 0) for row in toc),
            "citation": bool(first_hit and citation), "citation_preview": citation[:300],
            "printed_policy": (not fallback) if expected["printed"] else fallback,
            "viewer_link": bool(first_hit and (first_hit.get("viewer_url") or "/viewer" in json.dumps(first_hit, ensure_ascii=False))),
            "page_image": image_status == 200 and image_headers.get("Content-Type", "").startswith("image/"),
            "cover": cover_status == 200 and cover_headers.get("Content-Type", "").startswith("image/"),
            "raw_pdf_closed": pdf_status == 404,
        })

    co_status, co_body = post(
        "/api/search", {"q": "马克思 生活", "mode": "cooccurrence", "scope": ["book:user_rec_marx_biography_mclellan_2016"]}
    )
    chapter_status, chapter_body = post(
        "/api/search", {"q": "党的建设", "scope": ["book:user_rec_china_marxist_party_theory_2021"]}
    )
    ai_search_status, _ = post(
        "/api/ai/search-chat", {"q": "测试", "scope": ["book:user_rec_marx_biography_mclellan_2016"]}
    )
    ai_pdf_status, _ = post(
        "/api/ai/pdf-chat", {"q": "测试", "file": packages[0]["source_file"], "page": 10}
    )
    export_status, _ = post(
        "/api/search/exports", {"q": "测试", "scope": ["book:user_rec_marx_biography_mclellan_2016"]}
    )
    ai_config_status, _, ai_raw = get("/api/ai/assistant-config")
    try:
        ai_text = json.dumps(json.loads(ai_raw), ensure_ascii=False)
    except Exception:
        ai_text = ai_raw.decode("utf-8", "replace")
    statuses, full_emails = _recommendations(
        "/var/www/.marx_search_full/personal_library/personal_library.sqlite3"
    )
    published_path = os.path.join(args.release_root, "published.json")
    published = json.loads(open(published_path, encoding="utf-8").read()) if os.path.exists(published_path) else {}
    output = {
        "read_http": status,
        "cards": {key: html.count(f"《{value['title']}》") >= 1 for key, value in BOOKS.items()},
        "single_volume_cards": "第1卷" not in "".join(
            html[max(0, html.find(value["title"]) - 200):html.find(value["title"]) + 600] for value in BOOKS.values()
        ),
        "recommenders": {key: check_html.count(value["mask"]) == 1 for key, value in BOOKS.items()},
        "quality_note_hidden": "识别文本持续校对" not in html and "请以 PDF 原文为准" not in html,
        "full_email_absent": all(email not in html and email not in artifact_text for email in full_emails),
        "books": book_checks,
        "cooccurrence": {"http": co_status, "count": int(co_body.get("count") or 0)},
        "chapter_aggregate": {"http": chapter_status, "count": int(chapter_body.get("count") or 0), "display_mode": chapter_body.get("display_mode")},
        "guest_permissions": {"ai_search": ai_search_status, "ai_pdf": ai_pdf_status, "research_export": export_status},
        "ai_scope": {
            "http": ai_config_status, "collection": "用户荐书" in ai_text,
            "marx_biography": "user_rec_marx_biography_mclellan_2016" in ai_text,
            "party_theory": "user_rec_china_marxist_party_theory_2021" in ai_text,
        },
        "recommendation_statuses": statuses,
        "corpus_counts": _counts(args.corpus_db),
        "expected_counts": EXPECTED_COUNTS,
        "public_window": {"from": published.get("public_from"), "until": published.get("public_until")},
    }
    failures = []
    if output["read_http"] != 200 or not all(output["cards"].values()) or not all(output["recommenders"].values()):
        failures.append("reader cards")
    if not output["quality_note_hidden"] or not output["full_email_absent"]:
        failures.append("privacy or hidden quality note")
    for check in book_checks:
        if not (
            all(item["http"] == 200 and item["count"] > 0 and item["scoped"] for item in check["search"])
            and check["toc_http"] == 200 and check["toc"] == check["toc_expected"] and check["toc_ordered"]
            and check["citation"] and check["printed_policy"] and check["viewer_link"]
            and check["page_image"] and check["cover"] and check["raw_pdf_closed"]
        ):
            failures.append("book:" + check["key"])
    if not (co_status == 200 and int(co_body.get("count") or 0) > 0 and chapter_status == 200 and int(chapter_body.get("count") or 0) > 0):
        failures.append("retrieval modes")
    if {ai_search_status, ai_pdf_status, export_status} != {401}:
        failures.append("guest permissions")
    if ai_config_status != 200 or not all(output["ai_scope"].values()):
        failures.append("AI scope")
    if output["corpus_counts"] != EXPECTED_COUNTS:
        failures.append("corpus baseline")
    output["failures"] = failures
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
