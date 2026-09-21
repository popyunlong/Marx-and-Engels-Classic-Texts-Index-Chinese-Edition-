# -*- coding: utf-8 -*-
"""由保持隐藏的源配置生成一次性公开配置覆盖层。"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NEW_BOOKS = {
    "现代君主论", "论文学", "葛兰西政治著作选（1921—1926）", "葛兰西文选",
    "刘少奇年谱", "刘少奇选集", "中共中央文件选集（1921—1949）",
    "中共中央文件选集（1949—1966）",
}
FILES = (
    "books.yaml", "manifest.yaml", "volumes.yaml", "western_marxism_sources.yaml",
    "western_marxism_reviewed.yaml", "auxiliary_sources.yaml",
    "bibliographic_evidence_202609.yaml", "new_corpus_corrections_202609.yaml",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(output: Path) -> dict:
    source = ROOT / "config"
    output.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy2(source / name, output / name)

    books_path = output / "books.yaml"
    books = yaml.safe_load(books_path.read_text(encoding="utf-8")) or {}
    exposed: set[str] = set()
    for row in books.get("books") or []:
        key = str(row.get("key") or "")
        if key in NEW_BOOKS:
            row["available"] = True
            exposed.add(key)
    books_path.write_text(yaml.safe_dump(books, allow_unicode=True, sort_keys=False), encoding="utf-8")

    western_path = output / "western_marxism_reviewed.yaml"
    western = yaml.safe_load(western_path.read_text(encoding="utf-8")) or {}
    for row in western.get("records") or []:
        key = str(row.get("key") or "")
        if key in NEW_BOOKS:
            row["available"] = True
            exposed.add(key)
    western_path.write_text(yaml.safe_dump(western, allow_unicode=True, sort_keys=False), encoding="utf-8")

    if exposed != NEW_BOOKS:
        raise RuntimeError(f"公开配置书目不完整：missing={sorted(NEW_BOOKS - exposed)}")
    manifest = {
        "version": 1, "available_books": sorted(exposed),
        "files": {name: _sha(output / name) for name in FILES},
    }
    (output / "publish-config.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
