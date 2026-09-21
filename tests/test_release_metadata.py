from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

import release_metadata
from scripts import build_release_manifest
from scripts import build_release_archive


@pytest.fixture(autouse=True)
def _clear_release_cache():
    release_metadata.current_app_release.cache_clear()
    yield
    release_metadata.current_app_release.cache_clear()


def test_runtime_metadata_falls_back_safely(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_RELEASE_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(release_metadata, "__file__", str(tmp_path / "app" / "release_metadata.py"))
    release_metadata.current_app_release.cache_clear()
    assert release_metadata.current_app_release() == {
        "id": "development",
        "git_sha": "unknown",
        "built_at": None,
        "source_tree_sha256": None,
    }


def test_runtime_metadata_exposes_only_public_release_fields(monkeypatch, tmp_path: Path) -> None:
    metadata = tmp_path / "release.json"
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": "a" * 40 + "-20260921T140619Z-abcd1234",
                "git_sha": "a" * 40,
                "parent_release_id": "legacy-parent",
                "built_at": "2026-09-21T14:06:19Z",
                "source_tree_sha256": "b" * 64,
                "internal_note": "must not leak",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_RELEASE_FILE", str(metadata))
    release_metadata.current_app_release.cache_clear()
    assert release_metadata.current_app_release() == {
        "id": "a" * 40 + "-20260921T140619Z-abcd1234",
        "git_sha": "a" * 40,
        "built_at": "2026-09-21T14:06:19Z",
        "source_tree_sha256": "b" * 64,
    }


def test_source_tree_hash_is_deterministic_and_detects_tampering(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "b.txt").write_text("two\n", encoding="utf-8")
    (source / "a.txt").write_text("one\n", encoding="utf-8")
    first = build_release_manifest.source_tree_sha256(source)
    second = build_release_manifest.source_tree_sha256(source)
    assert first == second
    (source / "a.txt").write_text("changed\n", encoding="utf-8")
    assert build_release_manifest.source_tree_sha256(source) != first


def test_manifest_create_and_verify_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "entrypoint.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    metadata = tmp_path / "release.json"
    release_id = "a" * 40 + "-20260921T140619Z-abcd1234"
    assert build_release_manifest.main(
        [
            "create",
            "--source-dir",
            str(source),
            "--output",
            str(metadata),
            "--release-id",
            release_id,
            "--git-sha",
            "a" * 40,
            "--parent-release-id",
            "legacy-parent",
            "--built-at",
            "2026-09-21T14:06:19Z",
        ]
    ) == 0
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "release_id": release_id,
        "git_sha": "a" * 40,
        "parent_release_id": "legacy-parent",
        "built_at": "2026-09-21T14:06:19Z",
        "source_tree_sha256": build_release_manifest.source_tree_sha256(source),
    }
    assert build_release_manifest.main(
        [
            "verify",
            "--source-dir",
            str(source),
            "--metadata",
            str(metadata),
            "--release-id",
            release_id,
            "--git-sha",
            "a" * 40,
        ]
    ) == 0
    (source / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert build_release_manifest.main(
        ["verify", "--source-dir", str(source), "--metadata", str(metadata)]
    ) == 2


def test_complete_release_archive_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "app"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "entrypoint.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (nested / "asset.txt").write_text("asset\n", encoding="utf-8")
    metadata = tmp_path / "release.json"
    metadata.write_text('{"schema_version":1}\n', encoding="utf-8")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    build_release_archive.build_archive(source, metadata, first)
    build_release_archive.build_archive(source, metadata, second)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        assert archive.getnames() == [
            "app",
            "app/app.py",
            "app/entrypoint.sh",
            "app/nested",
            "app/nested/asset.txt",
            "release.json",
        ]
        assert archive.getmember("app/entrypoint.sh").mode == 0o755
