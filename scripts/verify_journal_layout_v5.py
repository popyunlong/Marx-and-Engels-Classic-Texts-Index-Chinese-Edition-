from __future__ import annotations

"""Verify the rebuilt issue, website documents and frozen mail catalogue together."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from journal_alerts import (  # noqa: E402
    generate_batch_review,
    get_batch,
    load_alert_settings,
    public_batch_articles,
    render_review_email,
)
from journal_fulltext import (  # noqa: E402
    load_issue_snapshot,
    write_issue_snapshot,
)
from journal_quality import numeric_tokens, validate_batch_documents  # noqa: E402
from journal_storage import JOURNAL_ARTICLES_DIR  # noqa: E402


EXPECTED_IDS = (1326, 1327, 1330, 1332, 1339, 1340, 1342, 1350, 1357)
REPEATED_LABEL = re.compile(r"(?:摘要\s*[：:]\s*){2,}|(?:Abstract\s*[：:]\s*){2,}", re.I)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_document(article_id: int) -> dict:
    path = JOURNAL_ARTICLES_DIR / str(article_id) / "doc.json"
    return json.loads(path.read_text(encoding="utf-8"))


def verify_issue(digest_id: int, *, refresh: bool) -> dict:
    batch = get_batch(digest_id)
    if not batch:
        raise AssertionError("batch-missing")
    if refresh:
        batch = generate_batch_review(digest_id)
    articles = public_batch_articles(digest_id)
    ids = tuple(sorted(int(article["id"]) for article in articles))
    if ids != EXPECTED_IDS:
        raise AssertionError(f"catalog-ids:{ids}")

    quality = validate_batch_documents(ids, JOURNAL_ARTICLES_DIR)
    if quality.get("status") != "passed":
        raise AssertionError(f"quality:{quality}")

    documents = {article_id: _load_document(article_id) for article_id in ids}
    table_blocks = [block for block in documents[1350]["paragraphs"] if block.get("kind") == "table"]
    if len(table_blocks) != 4:
        raise AssertionError("article-1350-table-count")
    for block in table_blocks:
        actual = numeric_tokens(
            " ".join(
                [str(block.get("text") or ""), *(str(item) for item in block.get("headers") or [])]
                + [
                    " ".join([str(row.get("label") or ""), *(str(item) for item in row.get("cells") or [])])
                    for row in block.get("rows") or []
                ]
            )
        )
        if actual != [str(item) for item in block.get("source_numbers") or []]:
            raise AssertionError("article-1350-table-numbers")
    body_1350 = " ".join(str(block.get("text") or "") for block in documents[1350]["paragraphs"])
    if "quarter of a century" not in body_1350:
        raise AssertionError("article-1350-cross-page-sentence")
    if sum(block.get("kind") == "figure" for block in documents[1327]["paragraphs"]) != 4:
        raise AssertionError("article-1327-figure-count")
    if sum(block.get("kind") in {"figure", "table"} for block in documents[1342]["paragraphs"]) != 4:
        raise AssertionError("article-1342-visual-count")

    snapshot_path = write_issue_snapshot(batch, articles) if refresh else None
    snapshot = load_issue_snapshot(batch, verify=True)
    if not snapshot:
        raise AssertionError("snapshot-missing-or-changed")
    frozen = list(snapshot.get("articles") or [])
    frozen_ids = tuple(sorted(int(article["id"]) for article in frozen))
    if frozen_ids != ids:
        raise AssertionError("snapshot-catalog-mismatch")
    settings = load_alert_settings()
    if int(settings.get("send_weekday", -1)) != 0 or str(settings.get("send_time")) != "09:00":
        raise AssertionError(f"send-schedule:{settings.get('send_weekday')}:{settings.get('send_time')}")
    text_mail, html_mail = render_review_email(
        batch,
        {"unsubscribe_token": "candidate-preview"},
        "https://mazhuzuojiansuo.com",
        settings,
        articles=frozen,
    )
    combined_mail = text_mail + "\n" + html_mail
    if REPEATED_LABEL.search(combined_mail):
        raise AssertionError("repeated-abstract-label")
    for article in frozen:
        title = str(article.get("title_zh") or article.get("title") or "")
        if not title or title not in combined_mail:
            raise AssertionError(f"mail-title-missing:{article.get('id')}")

    website_catalog = [
        {
            "id": int(article["id"]),
            "title": article.get("title"),
            "title_zh": article.get("title_zh"),
            "abstract": article.get("abstract"),
            "abstract_zh": article.get("abstract_zh"),
        }
        for article in articles
    ]
    mail_catalog = [
        {
            "id": int(article["id"]),
            "title": article.get("title"),
            "title_zh": article.get("title_zh"),
            "abstract": article.get("abstract"),
            "abstract_zh": article.get("abstract_zh"),
        }
        for article in frozen
    ]
    if _digest(website_catalog) != _digest(mail_catalog):
        raise AssertionError("website-mail-catalog-hash-mismatch")
    return {
        "status": "passed",
        "batch_id": digest_id,
        "article_ids": list(ids),
        "document_hashes": {str(article["id"]): article["document_sha256"] for article in frozen},
        "website_catalog_sha256": _digest(website_catalog),
        "mail_catalog_sha256": _digest(mail_catalog),
        "mail_text_bytes": len(text_mail.encode("utf-8")),
        "mail_html_bytes": len(html_mail.encode("utf-8")),
        "snapshot": str(snapshot_path) if snapshot_path else "verified-existing",
        "send_weekday": 0,
        "send_time": "09:00",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--digest-id", type=int, default=24)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify_issue(args.digest_id, refresh=args.refresh), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
