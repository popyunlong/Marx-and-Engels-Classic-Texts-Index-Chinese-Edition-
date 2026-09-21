"""Register the operator-supplied recommendation sources against user 296.

This script is intentionally production-only.  It validates the existing user
without embedding the complete email address, stores both immutable PDFs in the
recommendation object namespace, and leaves the records pending until the data
publication commits.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion.publisher import atomic_json  # noqa: E402
from ingestion.store import sha256  # noqa: E402
import membership  # noqa: E402
import personal_library  # noqa: E402
import personal_library_store  # noqa: E402


DATA = Path("/home/data/marx-user-recommended-20260913-02")
PRODUCTION_APPDATA = Path("/var/www/.marx_search_full")
EXPECTED_USER_ID = 296
EXPECTED_NAME = "abc"
EXPECTED_MASK = "267xxxx670@qq.com"

membership.DB_PATH = PRODUCTION_APPDATA / "membership.sqlite3"
personal_library.PERSONAL_LIB_DIR = PRODUCTION_APPDATA / "personal_library"
personal_library.DB_PATH = personal_library.PERSONAL_LIB_DIR / "personal_library.sqlite3"


def _mask_email(value: str) -> str:
    local, separator, domain = str(value or "").strip().lower().partition("@")
    if not separator or len(local) < 7 or not domain:
        return ""
    return local[:3] + ("x" * (len(local) - 6)) + local[-3:] + "@" + domain


def main() -> None:
    manifest_path = DATA / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("batch") != "user-recommended-20260913-02":
        raise ValueError("recommendation addendum batch identity mismatch")
    user = membership.get_user_by_id(EXPECTED_USER_ID)
    if (
        not user or not bool(user.get("is_active"))
        or str(user.get("display_name") or "").strip() != EXPECTED_NAME
        or _mask_email(user.get("email") or "") != EXPECTED_MASK
    ):
        raise ValueError("the verified recommending account changed")
    if not personal_library_store.configured():
        raise RuntimeError("recommendation storage node is not configured")

    results: list[dict] = []
    for source in sorted(manifest.get("files") or [], key=lambda row: int(row["volume"])):
        path = DATA / "raw-pdf" / f"volume-{int(source['volume'])}.pdf"
        if not path.is_file() or sha256(path) != source["sha256"] or path.stat().st_size != int(source["bytes"]):
            raise ValueError(f"volume {source['volume']}: staged PDF identity mismatch")
        row = personal_library.find_book_recommendation_by_sha(EXPECTED_USER_ID, source["sha256"])
        if row is None:
            row = personal_library.create_book_recommendation(
                user_id=EXPECTED_USER_ID,
                user_email=str(user["email"]),
                title=f"习近平文化文选 第{int(source['volume'])}卷",
                author="习近平",
                note="",
                original_filename=Path(source["source"]).name,
                byte_size=int(source["bytes"]),
                page_count=int(source["pages"]),
                sha256=source["sha256"],
            )
        rid = int(row["id"])
        if rid != int(source["recommendation_id"]):
            raise ValueError(
                f"volume {source['volume']}: expected recommendation id "
                f"{source['recommendation_id']}, got {rid}"
            )
        status = str(row.get("status") or "")
        if status == "storing":
            stored = personal_library_store.ingest_book_recommendation(EXPECTED_USER_ID, rid, path.read_bytes())
            if str((stored or {}).get("sha256") or "") != source["sha256"]:
                raise RuntimeError(f"volume {source['volume']}: storage node returned a different hash")
            personal_library.set_book_recommendation_status(rid, "pending")
            status = "pending"
        if status != "pending":
            raise ValueError(f"volume {source['volume']}: recommendation is not pending")
        results.append({
            "id": rid,
            "volume": int(source["volume"]),
            "status": status,
            "sha256": source["sha256"],
            "user_id": EXPECTED_USER_ID,
            "recommender_name": EXPECTED_NAME,
            "recommender_email_masked": EXPECTED_MASK,
        })
    atomic_json(DATA / "recommendations.json", {
        "batch": manifest["batch"], "records": results, "full_email_copied": False,
    })
    print(json.dumps({"registered": len(results), "ids": [row["id"] for row in results]}))


if __name__ == "__main__":
    main()
