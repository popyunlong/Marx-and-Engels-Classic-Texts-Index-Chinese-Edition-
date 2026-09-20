from __future__ import annotations

"""Build the network Agent bundle from an explicit two-file allow-list."""

import argparse
import hashlib
import io
import tarfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "citation_agent_release_manifest.txt"
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def _allowed_files() -> list[tuple[str, Path, bytes]]:
    items: list[tuple[str, Path, bytes]] = []
    seen: set[str] = set()
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        posix = PurePosixPath(name)
        if posix.is_absolute() or ".." in posix.parts or name in seen:
            raise ValueError(f"非法 Agent 发布白名单项：{name}")
        source = (ROOT / Path(*posix.parts)).resolve()
        if ROOT.resolve() not in source.parents or not source.is_file() or source.is_symlink():
            raise ValueError(f"Agent 发布文件不存在或不安全：{name}")
        data = source.read_bytes()
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError(f"Agent 发布文件过大：{name}")
        seen.add(name)
        items.append((name, source, data))
    expected = {"citation_agent_queue.py", "scripts/citation_agent_worker.py"}
    if seen != expected:
        raise ValueError("Agent 发布白名单必须且只能包含队列协议与脱敏模型 worker。")
    return items


def build(output: Path) -> Path:
    files = _allowed_files()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffixes[-2:] != [".tar", ".gz"]:
        raise ValueError("输出文件必须使用 .tar.gz 后缀。")
    checksums = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, _source, data in files
    ).encode("utf-8")
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name, _source, data in files + [("SHA256SUMS", output, checksums)]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o640
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="构建论文校注 Agent 独立白名单发布包")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    built = build(args.output)
    print(f"built {built} sha256={hashlib.sha256(built.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
