#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_tree_sha256(source_dir: Path) -> str:
    """Hash a deterministic, sorted manifest of all regular source files."""

    if not source_dir.is_dir():
        raise ValueError(f"source directory does not exist: {source_dir}")
    entries: list[tuple[str, str]] = []
    for path in source_dir.rglob("*"):
        relative = path.relative_to(source_dir).as_posix()
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"source tree must not contain symlinks: {relative}")
        if path.is_file() and not relative.endswith((".pyc", ".pyo")):
            entries.append((relative, _file_sha256(path)))
    digest = hashlib.sha256()
    for relative, file_hash in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _load_metadata(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("release metadata must be a JSON object")
    required = {
        "schema_version",
        "release_id",
        "git_sha",
        "parent_release_id",
        "built_at",
        "source_tree_sha256",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"release metadata missing fields: {', '.join(missing)}")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported release schema_version")
    if not RELEASE_RE.fullmatch(str(payload["release_id"])):
        raise ValueError("invalid release_id")
    if not SHA_RE.fullmatch(str(payload["git_sha"])):
        raise ValueError("git_sha must be a full lowercase commit SHA")
    if not str(payload["parent_release_id"]).strip():
        raise ValueError("parent_release_id must not be empty")
    try:
        datetime.fromisoformat(str(payload["built_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("built_at must be ISO-8601") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload["source_tree_sha256"])):
        raise ValueError("source_tree_sha256 must be a lowercase SHA-256")
    return payload


def create_manifest(args: argparse.Namespace) -> int:
    source_dir = args.source_dir.resolve()
    output = args.output.resolve()
    if not SHA_RE.fullmatch(args.git_sha):
        raise ValueError("--git-sha must be a full lowercase commit SHA")
    if not RELEASE_RE.fullmatch(args.release_id):
        raise ValueError("--release-id contains unsafe characters")
    if not args.parent_release_id.strip():
        raise ValueError("--parent-release-id must not be empty")
    built_at = args.built_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "schema_version": 1,
        "release_id": args.release_id,
        "git_sha": args.git_sha,
        "parent_release_id": args.parent_release_id,
        "built_at": built_at,
        "source_tree_sha256": source_tree_sha256(source_dir),
    }
    if (source_dir / 'catalog_release.py').is_file():
        payload['catalog_protocol'] = 1
    catalog_binding = source_dir / 'config/catalog_release.json'
    if catalog_binding.exists():
        payload['catalog_release'] = json.loads(catalog_binding.read_text(encoding='utf-8'))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def verify_manifest(args: argparse.Namespace) -> int:
    payload = _load_metadata(args.metadata.resolve())
    binding_path = args.source_dir / 'config/catalog_release.json'
    bound = json.loads(binding_path.read_text(encoding='utf-8')) if binding_path.exists() else None
    if payload.get('catalog_release') != bound:
        raise ValueError('catalogue binding differs from committed source')
    if bound and (not RELEASE_RE.fullmatch(str(bound.get('id', ''))) or
                  not re.fullmatch('[0-9a-f]{64}', str(bound.get('sha256', '')))):
        raise ValueError('invalid catalogue release binding')
    actual = source_tree_sha256(args.source_dir.resolve())
    if actual != payload["source_tree_sha256"]:
        raise ValueError(
            f"source tree hash mismatch: expected {payload['source_tree_sha256']}, got {actual}"
        )
    if args.release_id and payload["release_id"] != args.release_id:
        raise ValueError("release_id does not match the requested release")
    if args.git_sha and payload["git_sha"] != args.git_sha:
        raise ValueError("git_sha does not match the requested commit")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or verify immutable release metadata")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--source-dir", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--release-id", required=True)
    create.add_argument("--git-sha", required=True)
    create.add_argument("--parent-release-id", required=True)
    create.add_argument("--built-at")
    create.set_defaults(func=create_manifest)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--source-dir", type=Path, required=True)
    verify.add_argument("--metadata", type=Path, required=True)
    verify.add_argument("--release-id")
    verify.add_argument("--git-sha")
    verify.set_defaults(func=verify_manifest)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return int(args.func(args))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"release manifest error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
