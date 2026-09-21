"""Publish the verified two-volume user-recommendation addendum as one unit."""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion.candidate import build_candidate  # noqa: E402
from ingestion.publisher import atomic_json, publication_lock  # noqa: E402
from ingestion.scheduler import Scheduler, YieldRequired  # noqa: E402
from ingestion.store import Store, sha256  # noqa: E402
import membership  # noqa: E402
import personal_library  # noqa: E402
import scripts.publish_economics28_paddle as paddle_publish  # noqa: E402


DATA = Path("/home/data/marx-user-recommended-20260913-02")
PDF_ROOT = Path("/home/data/pdfs/自动入库")
BATCH = "user-recommended-20260913-02"
BOOK_KEY = "user_rec_xi_culture_selected_2026"
EXPECTED_TOTALS = {"logical_books": 1, "volumes": 2, "sources": 2, "pages": 514, "toc": 78}
PRODUCTION_APPDATA = Path("/var/www/.marx_search_full")
MIN_FREE_BYTES = 45 * 1024**3

membership.DB_PATH = PRODUCTION_APPDATA / "membership.sqlite3"
personal_library.PERSONAL_LIB_DIR = PRODUCTION_APPDATA / "personal_library"
personal_library.DB_PATH = personal_library.PERSONAL_LIB_DIR / "personal_library.sqlite3"


def _mask_email(value: str) -> str:
    local, separator, domain = str(value or "").strip().lower().partition("@")
    if not separator or len(local) < 7 or not domain:
        return ""
    return local[:3] + ("x" * (len(local) - 6)) + local[-3:] + "@" + domain


def load_packages() -> tuple[list[dict], list[dict]]:
    source_manifest = json.loads((DATA / "source-manifest.json").read_text(encoding="utf-8"))
    package_manifest = json.loads((DATA / "packages-manifest.json").read_text(encoding="utf-8"))
    if source_manifest.get("batch") != BATCH or package_manifest.get("batch") != BATCH:
        raise ValueError("batch identity mismatch")
    if package_manifest.get("totals") != EXPECTED_TOTALS:
        raise ValueError("expected one logical book, two sources, 514 pages and 78 directory entries")
    allowed = {
        (row["key"], int(row["volume"])): row for row in source_manifest.get("files") or []
    }
    packages: list[dict] = []
    recommendations: list[dict] = []
    for receipt in sorted(package_manifest.get("books") or [], key=lambda row: int(row["volume"])):
        identity = (receipt["key"], int(receipt["volume"]))
        source = allowed.get(identity)
        package_path = DATA / "packages" / receipt["package_file"]
        package = json.loads(package_path.read_text(encoding="utf-8"))
        if not source or sha256(package_path) != receipt["package_sha256"]:
            raise ValueError(f"{identity}: package identity mismatch")
        metadata = package.get("metadata") or {}
        if (
            package.get("batch") != BATCH or package.get("source_sha256") != source["sha256"]
            or int(package.get("page_count") or 0) != int(source["pages"])
            or int(metadata.get("volume") or 0) != int(source["volume"])
            or int(metadata.get("recommendation_id") or 0) != int(source["recommendation_id"])
        ):
            raise ValueError(f"{identity}: source allowlist mapping mismatch")
        if sha256(PDF_ROOT / f"{source['sha256']}.pdf") != source["sha256"]:
            raise ValueError(f"{identity}: uploaded PDF hash mismatch")
        if sha256(DATA / "raw-json" / f"volume-{source['volume']}.json") != source["json_sha256"]:
            raise ValueError(f"{identity}: uploaded OCR JSON hash mismatch")
        if [page["page"] for page in package["pages"]] != list(range(1, int(source["pages"]) + 1)):
            raise ValueError(f"{identity}: incomplete page sequence")
        if any(page.get("checked") for page in package["pages"]):
            raise ValueError(f"{identity}: provisional OCR must not claim full-text review")
        toc = package.get("toc") or []
        if len(toc) != int(receipt["toc"]) or any(
            not 1 <= int(item["pdf_page"]) <= int(source["pages"]) for item in toc
        ):
            raise ValueError(f"{identity}: invalid directory")

        recommendation = personal_library.get_book_recommendation(int(source["recommendation_id"]))
        user = membership.get_user_by_id(int((recommendation or {}).get("user_id") or 0))
        if not recommendation or recommendation.get("status") != "pending":
            raise ValueError(f"{identity}: recommendation is no longer pending")
        if (
            recommendation.get("sha256") != source["sha256"]
            or int(recommendation.get("byte_size") or 0) != int(source["bytes"])
            or int(recommendation.get("page_count") or 0) != int(source["pages"])
        ):
            raise ValueError(f"{identity}: online recommendation source changed")
        if not user or int(user["id"]) != int(recommendation["user_id"]):
            raise ValueError(f"{identity}: recommending user is unavailable")
        if str(user.get("email") or "").strip().lower() != str(recommendation.get("user_email") or "").strip().lower():
            raise ValueError(f"{identity}: online recommendation account changed")
        if str(user.get("display_name") or "").strip() != metadata["recommender_name"]:
            raise ValueError(f"{identity}: recommender display name changed")
        if _mask_email(user["email"]) != metadata["recommender_email_masked"]:
            raise ValueError(f"{identity}: recommender mask snapshot changed")
        recommendations.append(recommendation)
        packages.append(package)
    if (
        len(packages) != 2 or len({item["source_sha256"] for item in packages}) != 2
        or {item["metadata"]["book_key"] for item in packages} != {BOOK_KEY}
        or {int(item["metadata"]["volume"]) for item in packages} != {1, 2}
    ):
        raise ValueError("duplicate or missing source volume")
    return packages, recommendations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    packages, recommendations = load_packages()
    store = Store(DATA / "publication-store")
    batch = store.batch("用户荐书《习近平文化文选》两卷", BATCH)
    source_manifest = json.loads((DATA / "source-manifest.json").read_text(encoding="utf-8"))
    allowed = {row["sha256"]: row for row in source_manifest["files"]}
    with store.connect() as connection:
        for package in packages:
            item = allowed[package["source_sha256"]]
            connection.execute(
                "INSERT OR IGNORE INTO books(id,batch,sha,name,size,path,pages,status,created) "
                "VALUES(?,?,?,?,?,?,?,'ready',?)",
                (
                    package["book_id"], batch, item["sha256"], Path(item["source"]).name,
                    item["bytes"], str(PDF_ROOT / f"{item['sha256']}.pdf"), item["pages"], time.time(),
                ),
            )
    scheduler = Scheduler(
        store, PRODUCTION_APPDATA / "citation_assistant.sqlite3",
        health_url="http://127.0.0.1:8000/api/runtime",
    )
    paddle_publish.DATA = DATA
    publisher = paddle_publish.PaddlePublisher(store, scheduler)
    if shutil.disk_usage("/home/data").free < MIN_FREE_BYTES:
        raise YieldRequired("data disk is below the unchanged 45 GiB publication safety line")
    started = time.monotonic()
    while True:
        scheduler.tick()
        try:
            scheduler.check_quiet()
            break
        except YieldRequired:
            if time.monotonic() - started > 60:
                raise
            time.sleep(5)
    with publication_lock(Path("/home/data/marx-search-data/corpus-publish.lock")), contextlib.ExitStack() as locks:
        for path in (
            Path("/home/data/marx-search-corpus-repair/promotion.lock"),
            Path("/home/data/marx-search-new-corpus-202609/promotion.lock"),
        ):
            if path.parent.is_dir():
                locks.enter_context(publication_lock(path))
        if publisher.journal.exists():
            previous = json.loads(publisher.journal.read_text(encoding="utf-8"))
            if previous["phase"] not in ("published", "rolled_back"):
                raise YieldRequired("another release is active")
            publisher.retire_candidate(previous)
        if args.prepare_only:
            directory = publisher.root / ("user-rec-addendum-preflight-" + str(int(time.time())))
            record = build_candidate(
                packages, publisher.app_root, directory, publisher.checkpoint,
                unit_checkpoint=publisher.admit_unit,
            )
            publisher.prepare_runtime(directory, record)
            atomic_json(DATA / "prepared.json", {
                "directory": str(directory), "record": record, "stage": "built_not_serving",
            })
            print(json.dumps({"stage": "candidate_built", **EXPECTED_TOTALS}))
            return
        prepared = (
            json.loads((DATA / "prepared.json").read_text(encoding="utf-8"))["directory"]
            if (DATA / "prepared.json").exists() else None
        )
        with publisher.pause_duplicate():
            try:
                publisher.publish(packages, prepared=prepared)
            finally:
                if publisher.journal.exists():
                    state = json.loads(publisher.journal.read_text(encoding="utf-8"))
                    if state["phase"] == "rolled_back":
                        publisher.retire_candidate(state)
        state = json.loads(publisher.journal.read_text(encoding="utf-8"))
        if state.get("phase") != "published":
            raise RuntimeError("publication did not commit")
        personal_library.archive_book_recommendations(
            tuple(int(row["id"]) for row in recommendations)
        )
        public_window = state["record"].get("public_window") or {}
        atomic_json(DATA / "published.json", {
            "batch": BATCH,
            **EXPECTED_TOTALS,
            "public_from": public_window.get("public_from"),
            "public_until": public_window.get("public_until"),
            "candidate_sha256": state["record"].get("candidate_sha256"),
            "packages_sha256": state["record"].get("packages_sha256"),
            "recommendation_ids": sorted(int(row["id"]) for row in recommendations),
        })
        print(json.dumps({
            "stage": "published", **EXPECTED_TOTALS,
            "public_from": public_window.get("public_from"),
            "public_until": public_window.get("public_until"),
        }))


if __name__ == "__main__":
    try:
        main()
    except YieldRequired as exc:
        DATA.mkdir(parents=True, exist_ok=True)
        atomic_json(DATA / "waiting.json", {
            "at": time.time(), "reason": str(exc), "production_preserved": True,
        })
        print(json.dumps({"stage": "waiting", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(75)
