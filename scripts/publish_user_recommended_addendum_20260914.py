"""Publish the verified two-book recommendation addendum as one unit."""
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


DATA = Path("/home/data/marx-user-recommended-20260914-01")
PDF_ROOT = Path("/home/data/pdfs/自动入库")
BATCH = "user-recommended-20260914-01"
BOOK_KEYS = {
    "user_rec_marx_biography_mclellan_2016",
    "user_rec_china_marxist_party_theory_2021",
}
EXPECTED_TOTALS = {"logical_books": 2, "volumes": 2, "sources": 2, "pages": 858, "toc": 162}
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
        raise ValueError("package totals changed")
    allowed = {row["key"]: row for row in source_manifest.get("files") or []}
    packages, recommendations = [], []
    for receipt in package_manifest.get("books") or []:
        source = allowed.get(receipt["key"])
        package_path = DATA / "packages" / receipt["package_file"]
        package = json.loads(package_path.read_text(encoding="utf-8"))
        metadata = package.get("metadata") or {}
        if not source or sha256(package_path) != receipt["package_sha256"]:
            raise ValueError(f"{receipt['key']}: package identity mismatch")
        if (
            package.get("batch") != BATCH
            or package.get("source_sha256") != source["sha256"]
            or int(package.get("page_count") or 0) != int(source["pages"])
            or int(metadata.get("recommendation_id") or 0) != int(source["recommendation_id"])
            or not bool(metadata.get("single_volume"))
        ):
            raise ValueError(f"{receipt['key']}: source mapping mismatch")
        if sha256(PDF_ROOT / f"{source['sha256']}.pdf") != source["sha256"]:
            raise ValueError(f"{receipt['key']}: uploaded PDF hash mismatch")
        if sha256(DATA / "raw-json" / f"{source['stage_name']}.json") != source["json_sha256"]:
            raise ValueError(f"{receipt['key']}: uploaded OCR hash mismatch")
        if [page["page"] for page in package["pages"]] != list(range(1, int(source["pages"]) + 1)):
            raise ValueError(f"{receipt['key']}: incomplete page sequence")
        if any(page.get("checked") for page in package["pages"]):
            raise ValueError("provisional OCR cannot claim full-text review")
        if len(package.get("toc") or []) != int(receipt["toc"]):
            raise ValueError(f"{receipt['key']}: directory count changed")
        recommendation = personal_library.get_book_recommendation(int(source["recommendation_id"]))
        user = membership.get_user_by_id(int((recommendation or {}).get("user_id") or 0))
        if not recommendation or recommendation.get("status") != "pending" or not user:
            raise ValueError(f"{receipt['key']}: recommendation is unavailable")
        if (
            recommendation.get("sha256") != source["sha256"]
            or int(recommendation.get("byte_size") or 0) != int(source["bytes"])
            or int(recommendation.get("page_count") or 0) != int(source["pages"])
            or str(user.get("display_name") or "").strip() != metadata["recommender_name"]
            or _mask_email(user.get("email") or "") != metadata["recommender_email_masked"]
        ):
            raise ValueError(f"{receipt['key']}: recommendation snapshot changed")
        packages.append(package)
        recommendations.append(recommendation)
    if (
        len(packages) != 2
        or {item["metadata"]["book_key"] for item in packages} != BOOK_KEYS
        or len({item["source_sha256"] for item in packages}) != 2
    ):
        raise ValueError("duplicate or missing book")
    return packages, recommendations


def finish_publication(state: dict, recommendations: list[dict]) -> None:
    """Commit the operator-facing receipts after an already-safe data switch."""
    personal_library.archive_book_recommendations(tuple(int(row["id"]) for row in recommendations))
    public_window = state["record"].get("public_window") or {}
    atomic_json(DATA / "published.json", {
        "batch": BATCH, **EXPECTED_TOTALS,
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    packages, recommendations = load_packages()
    store = Store(DATA / "publication-store")
    batch = store.batch("用户荐书两册增量", BATCH)
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
    resume_only = False
    if publisher.journal.exists():
        try:
            prior = json.loads(publisher.journal.read_text(encoding="utf-8"))
            prior_books = set((prior.get("record", {}).get("public_window") or {}).get("books") or [])
            resume_only = prior.get("phase") == "published" and prior_books == BOOK_KEYS
        except (OSError, ValueError):
            resume_only = False
    # The 45 GiB line guards construction and switching. A completed switch may
    # temporarily sit below it because both generations are retained; finishing
    # that exact release only retires data and therefore releases space.
    if not resume_only and shutil.disk_usage("/home/data").free < MIN_FREE_BYTES:
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
            previous_books = set((previous.get("record", {}).get("public_window") or {}).get("books") or [])
            if previous["phase"] == "published" and previous_books == BOOK_KEYS:
                # A prior invocation may have completed the data switch but
                # paused while safely draining old candidate connections.
                finish_publication(previous, recommendations)
                return
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
        finish_publication(state, recommendations)


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
