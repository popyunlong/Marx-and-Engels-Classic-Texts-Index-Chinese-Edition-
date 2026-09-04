from __future__ import annotations

"""Build an exact, feature-only tarball from the search-export allowlist."""

import argparse
import hashlib
import os
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def _normalized_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Keep Windows-built release archives read-only and predictable on Linux."""
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mode = 0o644
    return info


def manifest_entries(manifest: Path) -> list[PurePosixPath]:
    entries: list[PurePosixPath] = []
    seen: set[str] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        relative = PurePosixPath(value.replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"unsafe manifest path: {value}")
        key = relative.as_posix()
        if key in seen:
            raise ValueError(f"duplicate manifest path: {key}")
        seen.add(key)
        entries.append(relative)
    if not entries:
        raise ValueError("release manifest is empty")
    return entries


def build_patch(source_root: Path, manifest: Path, output: Path) -> tuple[list[str], str]:
    root = source_root.resolve(strict=True)
    entries = manifest_entries(manifest.resolve(strict=True))
    sources: list[tuple[Path, str]] = []
    for relative in entries:
        candidate = root / Path(*relative.parts)
        if candidate.is_symlink():
            raise ValueError(f"manifest target must not be a symbolic link: {relative}")
        source = candidate.resolve(strict=True)
        if root not in source.parents or not source.is_file():
            raise ValueError(f"manifest target must be a real file inside source root: {relative}")
        sources.append((source, relative.as_posix()))

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=output.name + ".", suffix=".part", dir=str(output.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for source, relative in sources:
                archive.add(
                    source, arcname=relative, recursive=False, filter=_normalized_tarinfo,
                )
        with tarfile.open(temporary, "r:gz") as archive:
            actual = [member.name.removeprefix("./") for member in archive.getmembers() if member.isfile()]
            unexpected = [member.name for member in archive.getmembers() if not member.isfile()]
        expected = [relative for _source, relative in sources]
        if actual != expected or unexpected:
            raise RuntimeError("built archive does not exactly match the feature allowlist")
        hasher = hashlib.sha256()
        with temporary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        os.replace(temporary, output)
        return expected, digest
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建首页引文汇编功能的隔离发布包")
    parser.add_argument("source_root", type=Path, help="已从实时生产基线创建并仅合入本功能的候选目录")
    parser.add_argument("manifest", type=Path, help="search_export_release_manifest.txt")
    parser.add_argument("output", type=Path, help="输出 .tar.gz 路径")
    args = parser.parse_args()
    entries, digest = build_patch(args.source_root, args.manifest, args.output)
    print(f"SEARCH_EXPORT_PATCH_OK files={len(entries)} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
