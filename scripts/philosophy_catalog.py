#!/usr/bin/env python3
"""Validate the pinned Kant/Feuerbach source catalogue without changing data."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "kant_feuerbach_volumes.yaml"


@dataclass(frozen=True)
class PhilosophyVolume:
    id: str
    series_volume: int
    book: str
    volume: int
    display_title: str
    file: str
    pages: int
    sha256: str
    pilot_pages: tuple[int, ...]
    printed_page_resets: tuple[int, ...]

    @property
    def pdf_path(self) -> Path:
        return ROOT / self.file

    @property
    def sidecar_path(self) -> Path:
        return ROOT / "data" / "kant_feuerbach" / f"{self.id}.jsonl"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_payload(path: Path = CATALOG_PATH) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("philosophy catalogue root must be a mapping")
    return payload


def load_volumes(path: Path = CATALOG_PATH) -> list[PhilosophyVolume]:
    payload = load_payload(path)
    volumes: list[PhilosophyVolume] = []
    seen_ids: set[str] = set()
    seen_slots: set[tuple[str, int]] = set()
    seen_files: set[str] = set()
    series_by_collection: dict[str, set[int]] = {"kant": set(), "feuerbach": set()}
    for raw in payload.get("volumes") or []:
        item = PhilosophyVolume(
            id=str(raw.get("id") or "").strip(),
            series_volume=int(raw.get("series_volume") or 0),
            book=str(raw.get("book") or "").strip(),
            volume=int(raw.get("volume") or 0),
            display_title=str(raw.get("display_title") or "").strip(),
            file=str(raw.get("file") or "").replace("\\", "/").strip(),
            pages=int(raw.get("pages") or 0),
            sha256=str(raw.get("sha256") or "").lower().strip(),
            pilot_pages=tuple(int(page) for page in (raw.get("pilot_pages") or [])),
            printed_page_resets=tuple(
                int(page) for page in (raw.get("printed_page_resets") or [])
            ),
        )
        if not all((item.id, item.book, item.display_title, item.file, item.sha256)):
            raise ValueError(f"incomplete catalogue row: {raw!r}")
        if item.series_volume < 1 or item.volume < 1 or item.pages < 1 or len(item.sha256) != 64:
            raise ValueError(f"invalid catalogue row: {raw!r}")
        if len(item.pilot_pages) != 3 or any(page < 1 or page > item.pages for page in item.pilot_pages):
            raise ValueError(f"invalid pilot pages for {item.id}: {item.pilot_pages}")
        if len(set(item.printed_page_resets)) != len(item.printed_page_resets) or any(
            page < 2 or page > item.pages for page in item.printed_page_resets
        ):
            raise ValueError(f"invalid printed-page resets for {item.id}: {item.printed_page_resets}")
        if item.id in seen_ids or (item.book, item.volume) in seen_slots or item.file in seen_files:
            raise ValueError(f"duplicate catalogue identity: {raw!r}")
        seen_ids.add(item.id)
        seen_slots.add((item.book, item.volume))
        seen_files.add(item.file)
        collection = "kant" if item.file.startswith("pdfs/康德全集/") else "feuerbach"
        if item.series_volume in series_by_collection[collection]:
            raise ValueError(f"duplicate {collection} series volume: {item.series_volume}")
        series_by_collection[collection].add(item.series_volume)
        volumes.append(item)
    expected_files = int(payload.get("expected_files") or 0)
    expected_pages = int(payload.get("expected_pages") or 0)
    if len(volumes) != expected_files:
        raise ValueError(f"catalogue has {len(volumes)}/{expected_files} files")
    pages = sum(item.pages for item in volumes)
    if pages != expected_pages:
        raise ValueError(f"catalogue has {pages}/{expected_pages} pages")
    if series_by_collection["kant"] != {1, 2, 3, 4, 5, 7, 8, 9}:
        raise ValueError(f"Kant series volumes changed: {sorted(series_by_collection['kant'])}")
    if series_by_collection["feuerbach"] != set(range(1, 12)):
        raise ValueError(f"Feuerbach series volumes changed: {sorted(series_by_collection['feuerbach'])}")
    return volumes


def validate_sources(*, hashes: bool = True) -> dict[str, Any]:
    import fitz

    volumes = load_volumes()
    failures: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for item in volumes:
        actual_pages = 0
        actual_sha = ""
        if item.pdf_path.is_file():
            with fitz.open(item.pdf_path) as document:
                actual_pages = document.page_count
            if hashes:
                actual_sha = sha256_file(item.pdf_path)
        reasons: list[str] = []
        if not item.pdf_path.is_file():
            reasons.append("missing-file")
        if actual_pages != item.pages:
            reasons.append("page-count")
        if hashes and actual_sha != item.sha256:
            reasons.append("sha256")
        row = {
            "id": item.id,
            "file": item.file,
            "pages": actual_pages,
            "expected_pages": item.pages,
            "sha256": actual_sha,
            "expected_sha256": item.sha256,
            "passed": not reasons,
            "reasons": reasons,
        }
        rows.append(row)
        if reasons:
            failures.append(row)
    return {
        "schema": "kant-feuerbach-source-audit-v1",
        "files": len(volumes),
        "pages": sum(item.pages for item in volumes),
        "pilot_pages": sum(len(item.pilot_pages) for item in volumes),
        "hashes_checked": hashes,
        "failures": failures,
        "volumes": rows,
        "passed": not failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-hashes", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = validate_sources(hashes=not args.skip_hashes)
    except Exception as exc:  # noqa: BLE001 - CLI must fail closed with structured output
        report = {"passed": False, "errors": [f"{type(exc).__name__}: {exc}"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
