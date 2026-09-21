from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


_PUBLIC_FIELDS = ("id", "git_sha", "built_at", "source_tree_sha256")
_DEVELOPMENT_RELEASE = {
    "id": "development",
    "git_sha": "unknown",
    "built_at": None,
    "source_tree_sha256": None,
}


def _release_file_candidates() -> tuple[Path, ...]:
    configured = os.environ.get("APP_RELEASE_FILE", "").strip()
    module_dir = Path(__file__).resolve().parent
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    # Git checkout: <repo>/release.json. Immutable release: <release>/app/*.py
    candidates.extend((module_dir / "release.json", module_dir.parent / "release.json"))
    return tuple(candidates)


@lru_cache(maxsize=1)
def current_app_release() -> dict[str, Any]:
    """Return non-sensitive release identity for /api/runtime."""

    for candidate in _release_file_candidates():
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("schema_version") != 1 or not payload.get("release_id"):
            continue
        normalized = {
            "id": payload.get("release_id"),
            "git_sha": payload.get("git_sha"),
            "built_at": payload.get("built_at"),
            "source_tree_sha256": payload.get("source_tree_sha256"),
        }
        return {key: normalized.get(key) for key in _PUBLIC_FIELDS}
    return dict(_DEVELOPMENT_RELEASE)


__all__ = ["current_app_release"]
