"""Run the addendum candidate and its full guest acceptance under the publish lock."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion.publisher import command, publication_lock  # noqa: E402
from ingestion.scheduler import Scheduler, YieldRequired  # noqa: E402
from ingestion.store import Store  # noqa: E402
from scripts.publish_economics28_paddle import PaddlePublisher  # noqa: E402


DATA = Path("/home/data/marx-user-recommended-20260913-02")


def main() -> None:
    prepared = json.loads((DATA / "prepared.json").read_text(encoding="utf-8"))
    directory = Path(prepared["directory"])
    store = Store(DATA / "publication-store")
    scheduler = Scheduler(
        store, Path("/var/www/.marx_search_full/citation_assistant.sqlite3"),
        health_url="http://127.0.0.1:8000/api/runtime",
    )
    publisher = PaddlePublisher(store, scheduler)
    with publication_lock(Path("/home/data/marx-search-data/corpus-publish.lock")):
        try:
            started = time.monotonic()
            while True:
                scheduler.tick()
                try:
                    scheduler.check_quiet()
                    break
                except YieldRequired:
                    if time.monotonic() - started > 90:
                        raise
                    time.sleep(5)
            publisher.prepare_runtime(directory, prepared["record"])
            publisher.start_candidate(directory)
            result = subprocess.run(
                [
                    publisher.python,
                    str(ROOT / "scripts/audit_user_recommended_addendum_live.py"),
                    "--base-url", "http://127.0.0.1:8002",
                    "--release-root", str(DATA),
                    "--corpus-db", str(directory / "data/corpus.sqlite"),
                ],
                check=True, capture_output=True, text=True, timeout=900,
            )
            print(result.stdout.strip())
        finally:
            try:
                command("systemctl", "stop", publisher.unit, timeout=30)
            except Exception:
                pass


if __name__ == "__main__":
    main()
