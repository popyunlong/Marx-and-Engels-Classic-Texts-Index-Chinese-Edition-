"""Read-only local journal-monitor exporter. Upload only changed, stable documents."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_updates import digest, parse_import


def documents(root: Path):
    # Only direct current document folders; never recurse into history or links.
    root = root.resolve(strict=True)
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name == "history" or directory.is_symlink() or directory.resolve().parent != root:
            continue
        md = directory / "catalogue.md"
        meta = directory / "metadata.json"
        if not md.is_file() or md.is_symlink():
            continue
        files = [md] + ([meta] if meta.is_file() else [])
        if any(p.is_symlink() or p.resolve().parent != directory.resolve() for p in files):
            continue
        before = [(p.stat().st_size, p.stat().st_mtime_ns) for p in files]
        if any(size > 2_000_000 for size, _ in before):
            raise ValueError("单份国内资料超过 2 MB：" + directory.name)
        if any(time.time() - p.stat().st_mtime < 30 for p in files):
            continue
        markdown = md.read_text(encoding="utf-8-sig")
        metadata = json.loads(meta.read_text(encoding="utf-8-sig")) if meta in files else None
        after = [(p.stat().st_size, p.stat().st_mtime_ns) for p in files]
        if before != after:
            continue
        source_id = (metadata or {}).get("source", {}).get("source_id")
        if not source_id:
            import re
            source = re.search(r"(?m)^来源[：:]\s*(.+)", markdown)
            source_id = hashlib.sha256((source[1] if source else directory.name).encode()).hexdigest()
        payload = {"version": 1, "source_id": source_id, "markdown": markdown, "metadata": metadata}
        payload["content_hash"] = digest({"markdown": markdown, "metadata": metadata})
        parse_import(payload)
        yield payload


def sync(root: Path, state_path: Path, server: str = "", token: str = "", dry_run: bool = False, post=None):
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    results = []
    for payload in documents(root):
        source = payload["source_id"]
        if state.get(source) == payload["content_hash"]:
            results.append({"source_id": source, "unchanged": True})
            continue
        if dry_run:
            papers, warnings = parse_import(payload)
            results.append({"source_id": source, "papers": len(papers), "warnings": warnings})
            continue
        if urlsplit(server).scheme != "https" or not token:
            raise ValueError("需要 HTTPS 网站地址与专用导入凭据")
        response = (post or requests.post)(server.rstrip("/") + "/api/research-updates/imports", json=payload,
                                          headers={"Authorization": "Bearer " + token}, timeout=60, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"推送失败 HTTP {response.status_code}；未记录成功，下轮重试")
        result = response.json()
        if not result.get("import_id"):
            raise RuntimeError("服务器未返回导入凭据，未记录成功")
        state[source] = payload["content_hash"]
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(state_path)
        results.append(result)
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(r"D:\CodexData\outputs\journal-monitor"))
    p.add_argument("--state", type=Path, default=Path(r"D:\CodexData\data\research-sync\state.json"))
    p.add_argument("--server", default="https://mazhuzuojiansuo.com")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    result = sync(args.root, args.state, args.server, os.environ.get("MARX_RESEARCH_IMPORT_TOKEN", ""), args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
