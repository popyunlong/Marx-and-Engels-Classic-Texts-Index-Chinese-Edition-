#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import os
import sys
import tarfile
import tempfile
from pathlib import Path


def _normalized_info(tar: tarfile.TarFile, path: Path, arcname: str) -> tarfile.TarInfo:
    if path.is_symlink():
        raise ValueError(f"release input must not contain symlinks: {path}")
    info = tar.gettarinfo(str(path), arcname=arcname)
    if not (info.isfile() or info.isdir()):
        raise ValueError(f"unsupported release input type: {path}")
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    info.pax_headers = {}
    if info.isdir():
        info.mode = 0o755
    else:
        # Git executable bits are not materialized by Expand-Archive on
        # Windows. Derive the archive mode from content/type so the same Git
        # tree produces the same package on Windows and Linux.
        with path.open("rb") as handle:
            has_shebang = handle.read(2) == b"#!"
        executable = path.suffix.lower() == ".sh" or has_shebang
        info.mode = 0o755 if executable else 0o644
    return info


def build_archive(source_dir: Path, metadata: Path, output: Path) -> None:
    source_dir = source_dir.resolve()
    metadata = metadata.resolve()
    output = output.resolve()
    if not source_dir.is_dir():
        raise ValueError(f"source directory does not exist: {source_dir}")
    if not metadata.is_file():
        raise ValueError(f"release metadata does not exist: {metadata}")
    output.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    root_info = _normalized_info(archive, source_dir, "app")
                    archive.addfile(root_info)
                    for path in sorted(source_dir.rglob("*"), key=lambda item: item.relative_to(source_dir).as_posix()):
                        relative = path.relative_to(source_dir).as_posix()
                        info = _normalized_info(archive, path, f"app/{relative}")
                        if info.isfile():
                            with path.open("rb") as handle:
                                archive.addfile(info, handle)
                        else:
                            archive.addfile(info)
                    metadata_info = _normalized_info(archive, metadata, "release.json")
                    with metadata.open("rb") as handle:
                        archive.addfile(metadata_info, handle)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a deterministic immutable release archive")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        build_archive(args.source_dir, args.metadata, args.output)
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"release archive error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
