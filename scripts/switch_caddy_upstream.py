from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _atomic_copy(source: Path, destination: Path) -> None:
    stat = destination.stat()
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, name)
        os.chmod(name, stat.st_mode)
        if hasattr(os, "chown"):
            os.chown(name, stat.st_uid, stat.st_gid)
        os.replace(name, destination)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("from_port", type=int)
    parser.add_argument("to_port", type=int)
    parser.add_argument("--caddyfile", type=Path, default=Path("/etc/caddy/Caddyfile"))
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.from_port == args.to_port:
        raise RuntimeError("source and destination ports must differ")

    original_text = args.caddyfile.read_text(encoding="utf-8")
    source = f"127.0.0.1:{args.from_port}"
    destination = f"127.0.0.1:{args.to_port}"
    changed = 0
    output: list[str] = []
    for line in original_text.splitlines(keepends=True):
        if line.lstrip().startswith("reverse_proxy ") and source in line:
            occurrences = line.count(source)
            line = line.replace(source, destination)
            changed += occurrences
        output.append(line)
    if changed < 1:
        raise RuntimeError(f"no reverse_proxy lines point to {source}")
    patched_text = "".join(output)
    if any(
        line.lstrip().startswith("reverse_proxy ") and source in line
        for line in patched_text.splitlines()
    ):
        raise RuntimeError(f"a reverse_proxy line still points to {source}")

    fd, candidate_name = tempfile.mkstemp(
        prefix=".Caddyfile.cutover.", dir=args.caddyfile.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(patched_text)
            handle.flush()
            os.fsync(handle.fileno())
        candidate = Path(candidate_name)
        subprocess.run(
            ["caddy", "validate", "--adapter", "caddyfile", "--config", str(candidate)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.backup_dir.mkdir(parents=True, exist_ok=True)
        backup = args.backup_dir / f"Caddyfile.{args.from_port}-to-{args.to_port}.{stamp}"
        shutil.copy2(args.caddyfile, backup)
        _atomic_copy(candidate, args.caddyfile)
        try:
            subprocess.run(["systemctl", "reload", "caddy"], check=True)
            subprocess.run(["systemctl", "is-active", "--quiet", "caddy"], check=True)
        except Exception:
            _atomic_copy(backup, args.caddyfile)
            subprocess.run(["systemctl", "reload", "caddy"], check=False)
            raise
    finally:
        try:
            os.unlink(candidate_name)
        except FileNotFoundError:
            pass

    print(f"CADDY_SWITCH_OK {args.from_port}->{args.to_port} lines={changed} backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
