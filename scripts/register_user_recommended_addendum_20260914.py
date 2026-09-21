"""Bind the two staged PDFs to their verified recommending accounts."""
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


DATA = Path("/home/data/marx-user-recommended-20260914-01")
PRODUCTION_APPDATA = Path("/var/www/.marx_search_full")
BATCH = "user-recommended-20260914-01"
ACCOUNTS = {
    "user_rec_marx_biography_mclellan_2016": {
        "user_id": 11, "name": "李博", "mask": "269xxxx264@qq.com",
        "title": "马克思传", "author": "戴维·麦克莱伦",
    },
    "user_rec_china_marxist_party_theory_2021": {
        "user_id": 1929, "name": "superkar1", "mask": "karxxxxxxarx@163.com",
        "title": "中国化的马克思主义党建理论体系概论", "author": "全国党的建设研究会",
    },
}

membership.DB_PATH = PRODUCTION_APPDATA / "membership.sqlite3"
personal_library.PERSONAL_LIB_DIR = PRODUCTION_APPDATA / "personal_library"
personal_library.DB_PATH = personal_library.PERSONAL_LIB_DIR / "personal_library.sqlite3"


def _mask_email(value: str) -> str:
    local, separator, domain = str(value or "").strip().lower().partition("@")
    if not separator or len(local) < 7 or not domain:
        return ""
    return local[:3] + ("x" * (len(local) - 6)) + local[-3:] + "@" + domain


def main() -> None:
    manifest = json.loads((DATA / "source-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("batch") != BATCH or len(manifest.get("files") or []) != 2:
        raise ValueError("staged source manifest does not match the release")
    if not personal_library_store.configured():
        raise RuntimeError("recommendation storage node is not configured")
    results = []
    for source in manifest["files"]:
        account = ACCOUNTS[source["key"]]
        user = membership.get_user_by_id(account["user_id"])
        if (
            not user or not bool(user.get("is_active"))
            or not bool(user.get("email_verified_at"))
            or str(user.get("display_name") or "").strip() != account["name"]
            or _mask_email(user.get("email") or "") != account["mask"]
        ):
            raise ValueError(f"{source['key']}: verified recommending account changed")
        path = DATA / "raw-pdf" / f"{source['stage_name']}.pdf"
        if not path.is_file() or sha256(path) != source["sha256"] or path.stat().st_size != int(source["bytes"]):
            raise ValueError(f"{source['key']}: staged PDF identity mismatch")
        row = personal_library.find_book_recommendation_by_sha(account["user_id"], source["sha256"])
        if row is None:
            # This is an authenticated operator import of an already-staged source.
            # Raise the in-process validator only for this call; the public upload
            # limit and running application configuration remain unchanged.
            public_limit = personal_library.BOOK_RECOMMENDATION_MAX_PDF_BYTES
            try:
                personal_library.BOOK_RECOMMENDATION_MAX_PDF_BYTES = max(
                    public_limit, int(source["bytes"])
                )
                row = personal_library.create_book_recommendation(
                    user_id=account["user_id"], user_email=str(user["email"]),
                    title=account["title"], author=account["author"], note="",
                    original_filename=Path(source["source"]).name,
                    byte_size=int(source["bytes"]), page_count=int(source["pages"]),
                    sha256=source["sha256"],
                )
            finally:
                personal_library.BOOK_RECOMMENDATION_MAX_PDF_BYTES = public_limit
        recommendation_id = int(row["id"])
        if recommendation_id != int(source["recommendation_id"]):
            raise ValueError(
                f"{source['key']}: expected recommendation {source['recommendation_id']}, got {recommendation_id}"
            )
        status = str(row.get("status") or "")
        if status == "storing":
            stored = personal_library_store.ingest_book_recommendation(
                account["user_id"], recommendation_id, path.read_bytes()
            )
            if str((stored or {}).get("sha256") or "") != source["sha256"]:
                raise RuntimeError(f"{source['key']}: storage node returned a different hash")
            personal_library.set_book_recommendation_status(recommendation_id, "pending")
            status = "pending"
        if status != "pending":
            raise ValueError(f"{source['key']}: recommendation is not pending")
        results.append({
            "id": recommendation_id,
            "key": source["key"],
            "status": status,
            "sha256": source["sha256"],
            "user_id": account["user_id"],
            "recommender_name": account["name"],
            "recommender_email_masked": account["mask"],
        })
    atomic_json(DATA / "recommendations.json", {
        "batch": BATCH, "records": results, "full_email_copied": False,
    })
    print(json.dumps({"registered": len(results), "ids": sorted(row["id"] for row in results)}))


if __name__ == "__main__":
    main()
