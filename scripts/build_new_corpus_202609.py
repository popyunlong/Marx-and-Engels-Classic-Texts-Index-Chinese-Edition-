# -*- coding: utf-8 -*-
"""2026-09 新文献候选库：白名单校验、离线构建与不可变性验收。"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import fitz
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import BUILD_DB_PATH, normalize  # noqa: E402
from scripts.build_western_marxism import build_rows, load_specs, sidecar_path  # noqa: E402

NEW_BOOKS = (
    "现代君主论", "论文学", "葛兰西政治著作选（1921—1926）", "葛兰西文选",
    "刘少奇年谱", "刘少奇选集", "中共中央文件选集（1921—1949）",
    "中共中央文件选集（1949—1966）",
)
WESTERN_BOOKS = NEW_BOOKS[:4]
EXPECTED_FILES = 76
EXPECTED_PAGES = 40871
DEFAULT_STAGE = ROOT / "tmp" / "new_corpus_202609" / "corpus.sqlite"
REPORT_PATH = ROOT / "tmp" / "new_corpus_202609" / "verification.json"
CORRECTIONS_PATH = ROOT / "config" / "new_corpus_corrections_202609.yaml"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_rows() -> list[tuple[str, dict]]:
    manifest = yaml.safe_load((ROOT / "config" / "manifest.yaml").read_text(encoding="utf-8")) or {}
    return [(book, dict(row)) for book in NEW_BOOKS for row in (manifest.get(book) or [])]


def validate_sources() -> dict:
    rows = manifest_rows()
    errors: list[str] = []
    pages = 0
    seen_targets: set[str] = set()
    for book, row in rows:
        source = ROOT / str(row.get("source") or row["file"])
        target = str(row["file"])
        if target in seen_targets:
            errors.append(f"重复稳定路径：{target}")
        seen_targets.add(target)
        if not source.exists():
            errors.append(f"缺文件：{source}")
            continue
        with fitz.open(source) as doc:
            actual_pages = doc.page_count
        expected_pages = int(row.get("page_count") or 0)
        pages += expected_pages
        if actual_pages != expected_pages:
            errors.append(f"页数不符：{book} v{row['volume']} {actual_pages}!={expected_pages}")
        actual_hash = _sha(source)
        if actual_hash != str(row.get("sha256") or "").lower():
            errors.append(f"SHA-256不符：{book} v{row['volume']}")
    aux = yaml.safe_load((ROOT / "config" / "auxiliary_sources.yaml").read_text(encoding="utf-8")) or {}
    for row in aux.get("sources") or []:
        source = ROOT / str(row["source"])
        if not source.exists() or _sha(source) != str(row["sha256"]).lower():
            errors.append(f"辅助资料不符：{row.get('key')}")
    if len(rows) != EXPECTED_FILES or pages != EXPECTED_PAGES:
        errors.append(f"批次总量不符：{len(rows)}册/{pages}页")
    if errors:
        raise RuntimeError("\n".join(errors))
    return {"files": len(rows), "pages": pages, "targets": len(seen_targets)}


def prepare(stage: Path, base: Path, *, fresh: bool) -> None:
    validate_sources()
    stage.parent.mkdir(parents=True, exist_ok=True)
    if fresh or not stage.exists():
        shutil.copy2(base, stage)
    with sqlite3.connect(stage) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("候选库副本 integrity_check 失败")


def _insert_western(stage: Path) -> None:
    specs = {str(row.get("key")): row for row in load_specs()}
    all_pages: list[tuple] = []
    all_toc: list[tuple] = []
    for key in WESTERN_BOOKS:
        pages, toc, _stats = build_rows(specs[key], allow_unverified_metadata=False)
        if not toc:
            raise RuntimeError(f"{key}：目录为空")
        all_pages.extend(pages)
        all_toc.extend(toc)
    with sqlite3.connect(stage) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for key in WESTERN_BOOKS:
            conn.execute("DELETE FROM pages WHERE book=?", (key,))
            conn.execute("DELETE FROM toc_entries WHERE book=?", (key,))
        conn.executemany(
            "INSERT INTO pages(book,volume,source_file,pdf_page,printed_page,raw_text,normalized_text) "
            "VALUES(?,?,?,?,?,?,?)", all_pages,
        )
        conn.executemany(
            "INSERT INTO toc_entries(book,volume,source_file,title,pdf_page,printed_page,level,kind,sort_order) "
            "VALUES(?,?,?,?,?,?,?,?,?)", all_toc,
        )
        conn.commit()


def _run(*args: str) -> None:
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def build(stage: Path) -> None:
    _insert_western(stage)
    ids = ["liu_np_vol01", "liu_np_vol02", "liu_xuan_vol01", "liu_xuan_vol02"]
    ids += [f"party_files_1921_vol{x:02d}" for x in range(1, 19)]
    ids += [f"party_files_1949_vol{x:02d}" for x in range(1, 51)]
    _run("scripts/build_scan_volumes.py", "--db", str(stage), "--only", *ids)
    _run("scripts/build_nianpu_toc.py", "--db", str(stage), "--book", "刘少奇年谱")
    _run("scripts/build_printed_toc_dots.py", "--db", str(stage), "--book", "刘少奇选集")
    _run("scripts/build_central_files_toc.py", "--db", str(stage))
    _apply_corrections(stage)


def _apply_corrections(stage: Path) -> None:
    """Apply the Terra-reviewed, versioned correction list after every rebuild."""
    if not CORRECTIONS_PATH.exists():
        return
    payload = yaml.safe_load(CORRECTIONS_PATH.read_text(encoding="utf-8")) or {}
    page_rows = payload.get("pages") or []
    toc_rows = payload.get("toc") or []
    with sqlite3.connect(stage) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for item in page_rows:
            book = str(item["book"])
            volume = int(item["volume"])
            pdf_page = int(item["pdf_page"])
            raw_text = str(item["raw_text"])
            if book not in NEW_BOOKS or not raw_text.strip():
                raise RuntimeError(f"非法正文校订：{book} v{volume} p{pdf_page}")
            cur = conn.execute(
                "UPDATE pages SET raw_text=?,normalized_text=? "
                "WHERE book=? AND volume=? AND pdf_page=?",
                (raw_text, normalize(raw_text), book, volume, pdf_page),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"正文校订落点不唯一：{book} v{volume} p{pdf_page}")
        for item in toc_rows:
            book = str(item["book"])
            volume = int(item["volume"])
            old_title = str(item["old_title"])
            if book not in NEW_BOOKS:
                raise RuntimeError(f"非法目录校订：{book} v{volume}")
            updates = {
                key: item[key] for key in ("title", "pdf_page", "printed_page", "level", "kind")
                if key in item
            }
            if not updates:
                raise RuntimeError(f"空目录校订：{book} v{volume} {old_title}")
            columns = ",".join(f"{key}=?" for key in updates)
            cur = conn.execute(
                f"UPDATE toc_entries SET {columns} WHERE book=? AND volume=? AND title=?",
                (*updates.values(), book, volume, old_title),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"目录校订落点不唯一：{book} v{volume} {old_title}")
        conn.commit()


def _blank_pages() -> set[tuple[str, int, int]]:
    prefixes = {
        "刘少奇年谱": "liu_np", "刘少奇选集": "liu_xuan",
        "中共中央文件选集（1921—1949）": "party_files_1921",
        "中共中央文件选集（1949—1966）": "party_files_1949",
    }
    allowed: set[tuple[str, int, int]] = set()
    for book in WESTERN_BOOKS:
        path = sidecar_path(book)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if item.get("blank") and item.get("src") == "blank-confirmed":
                    allowed.add((book, 1, int(item["pdf_page"])))
            except Exception:
                continue
    for book, prefix in prefixes.items():
        for _book, row in (x for x in manifest_rows() if x[0] == book):
            path = ROOT / "data" / f"{prefix}_vol{int(row['volume']):02d}_glm.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                    if item.get("blank") and str(item.get("src") or "").startswith("blank-confirmed"):
                        allowed.add((book, int(row["volume"]), int(item["pdf_page"])))
                except Exception:
                    continue
    # 极低对比度的纸边、装订线可能使自动墨量略高于阈值；只有 Terra 对同版页面
    # 视觉确认后，才允许在可重复校订清单中把这种页加入真空白白名单。
    payload = yaml.safe_load(CORRECTIONS_PATH.read_text(encoding="utf-8")) or {}
    for item in payload.get("confirmed_blank_pages") or []:
        key = (str(item["book"]), int(item["volume"]), int(item["pdf_page"]))
        if key[0] not in NEW_BOOKS:
            raise RuntimeError(f"非法空白页校订：{key}")
        allowed.add(key)
    return allowed


def verify(stage: Path, base: Path) -> dict:
    rows = manifest_rows()
    blank = _blank_pages()
    problems: list[str] = []
    with sqlite3.connect(stage) as conn:
        conn.execute("ATTACH DATABASE ? AS base", (str(base),))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        total = conn.execute(
            f"SELECT COUNT(*) FROM pages WHERE book IN ({','.join('?' for _ in NEW_BOOKS)})", NEW_BOOKS
        ).fetchone()[0]
        duplicates = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT book,volume,pdf_page,COUNT(*) n FROM pages "
            f"WHERE book IN ({','.join('?' for _ in NEW_BOOKS)}) GROUP BY book,volume,pdf_page HAVING n<>1)", NEW_BOOKS
        ).fetchone()[0]
        empties = [(str(b), int(v), int(p)) for b, v, p in conn.execute(
            f"SELECT book,volume,pdf_page FROM pages WHERE book IN ({','.join('?' for _ in NEW_BOOKS)}) "
            "AND length(trim(raw_text))=0", NEW_BOOKS
        )]
        unexpected_empty = [x for x in empties if x not in blank]
        rejected = conn.execute(
            f"SELECT COUNT(*) FROM pages WHERE book IN ({','.join('?' for _ in NEW_BOOKS)}) "
            "AND (raw_text LIKE '抱歉%' OR raw_text LIKE '以下是图片%' OR raw_text LIKE '无法识别%')", NEW_BOOKS
        ).fetchone()[0]
        ordered_text = conn.execute(
            f"SELECT book,volume,pdf_page,normalized_text FROM pages "
            f"WHERE book IN ({','.join('?' for _ in NEW_BOOKS)}) "
            "ORDER BY book,volume,pdf_page", NEW_BOOKS
        ).fetchall()
        adjacent_repeats: list[tuple[str, int, int, int]] = []
        previous: tuple[str, int, int, str] | None = None
        for book, volume, pdf_page, text in ordered_text:
            current = (str(book), int(volume), int(pdf_page), str(text or "").strip())
            if (
                previous is not None
                and current[0] == previous[0]
                and current[1] == previous[1]
                and current[2] == previous[2] + 1
                and len(current[3]) >= 80
                and current[3] == previous[3]
            ):
                adjacent_repeats.append((current[0], current[1], previous[2], current[2]))
            previous = current
        missing_toc = []
        for book, row in rows:
            count = conn.execute("SELECT COUNT(*) FROM pages WHERE book=? AND volume=?", (book, row["volume"])).fetchone()[0]
            toc = conn.execute("SELECT COUNT(*) FROM toc_entries WHERE book=? AND volume=?", (book, row["volume"])).fetchone()[0]
            if count != int(row["page_count"]):
                problems.append(f"{book} v{row['volume']} pages={count}")
            if toc == 0:
                missing_toc.append(f"{book} v{row['volume']}")
        placeholders = ",".join("?" for _ in NEW_BOOKS)
        old_pages_delta = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT * FROM main.pages WHERE book NOT IN ({placeholders}) "
            f"EXCEPT SELECT * FROM base.pages WHERE book NOT IN ({placeholders}))", NEW_BOOKS + NEW_BOOKS
        ).fetchone()[0]
        old_pages_reverse = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT * FROM base.pages WHERE book NOT IN ({placeholders}) "
            f"EXCEPT SELECT * FROM main.pages WHERE book NOT IN ({placeholders}))", NEW_BOOKS + NEW_BOOKS
        ).fetchone()[0]
        old_toc_delta = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT * FROM main.toc_entries WHERE book NOT IN ({placeholders}) "
            f"EXCEPT SELECT * FROM base.toc_entries WHERE book NOT IN ({placeholders}))", NEW_BOOKS + NEW_BOOKS
        ).fetchone()[0]
        old_toc_reverse = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT * FROM base.toc_entries WHERE book NOT IN ({placeholders}) "
            f"EXCEPT SELECT * FROM main.toc_entries WHERE book NOT IN ({placeholders}))", NEW_BOOKS + NEW_BOOKS
        ).fetchone()[0]
        aux_hits = conn.execute("SELECT COUNT(*) FROM pages WHERE source_file LIKE '%总目录%'").fetchone()[0]
    if integrity != "ok": problems.append("integrity_check=" + str(integrity))
    if total != EXPECTED_PAGES: problems.append(f"新书总页数={total}")
    if duplicates: problems.append(f"重复页={duplicates}")
    if unexpected_empty: problems.append(f"非确认空白页的空正文={len(unexpected_empty)}")
    if rejected: problems.append(f"模型拒答文本={rejected}")
    if adjacent_repeats: problems.append(f"相邻页异常重复={len(adjacent_repeats)}")
    if missing_toc: problems.append("无目录=" + ",".join(missing_toc))
    if any((old_pages_delta, old_pages_reverse, old_toc_delta, old_toc_reverse)):
        problems.append("既有书目数据发生变化")
    if aux_hits: problems.append(f"总目录进入公开 pages={aux_hits}")
    report = {
        "ok": not problems, "files": len(rows), "pages": total, "confirmed_blank_pages": len(blank),
        "unexpected_empty_pages": len(unexpected_empty), "duplicate_pages": duplicates,
        "rejected_pages": rejected, "missing_toc": missing_toc,
        "adjacent_repeated_pages": adjacent_repeats,
        "old_pages_delta": old_pages_delta + old_pages_reverse,
        "old_toc_delta": old_toc_delta + old_toc_reverse, "auxiliary_public_hits": aux_hits,
        "sha256": _sha(stage), "problems": problems,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    stage.with_suffix(stage.suffix + ".sha256").write_text(report["sha256"] + "\n", encoding="utf-8")
    if problems:
        raise RuntimeError("；".join(problems))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "build", "verify", "all"))
    parser.add_argument("--base", type=Path, default=BUILD_DB_PATH)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    if args.command in {"prepare", "all"}:
        prepare(args.stage, args.base, fresh=args.fresh or args.command == "all")
        print(json.dumps(validate_sources(), ensure_ascii=False))
    if args.command in {"build", "all"}:
        build(args.stage)
    if args.command in {"verify", "all"}:
        print(json.dumps(verify(args.stage, args.base), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
