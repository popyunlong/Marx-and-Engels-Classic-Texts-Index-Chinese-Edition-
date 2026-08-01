# -*- coding: utf-8 -*-
"""据 GLM-4V 重扫的目录页结果（data/book_toc_scan.json）重建 toc_entries。

两种定位方式自动择一，同一卷内可混用：
  ① 扫描抄到了页码 → 经 pages.printed_page 反查 pdf 页。**不信目录页上的页码去算偏移**，
     印刷页码本身已由 build_scan_volumes 的众数/分段法定过，比二次 OCR 的数字可靠。
  ② 没抄到页码（《斯大林全集》整套如此，其目录页的页码列根本没印清楚）→ 拿篇名回正文
     找篇首页。

两种方式得到的候选一律再过一遍**全局最长递增链**：目录顺序必然等于正文顺序，
真匹配构成递增链，散落的误匹配进不了链。这一步还顺带解决了前置条目的重号问题——
《斯大林年谱》的「凡例(1) 说明(1) 斯大林小传(7)」与正文「1879年(1)」用的是两套页码，
按印刷页映射会把前三条落到正文首页附近、造成回头，LIS 会自动把少数派剔除。

写库前与现有目录逐卷比较，**只在「核验通过的条数」更多时才替换**，避免把书签来源的
准确目录换成更差的结果。

用法：
  python scripts/build_toc_from_scan.py --all-new --dry-run
  python scripts/build_toc_from_scan.py --book 邓小平年谱 --apply
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from build_index import DB_PATH, normalize  # noqa: E402

SCAN = ROOT / "data" / "book_toc_scan.json"
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")
HEAD_CHARS = 150
KEY_MIN, KEY_MAX = 5, 10
FUZZY_MIN = 0.72
_YEAR = re.compile(r"^(?:1[89]|20)\d{2}\s*年")
_SECTION = ("索引", "对照表", "附录", "凡例", "说明", "后记", "前言", "编者", "年表")


def update_hash() -> None:
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8", newline="\n")


def flat(s: str) -> str:
    return normalize(re.sub(r"\s+", "", s or ""))


def longest_monotone(pairs: list[tuple[int, int]]) -> set[int]:
    """pairs=(条目序, 页下标)，返回构成最长严格递增链的 pairs 下标集合。"""
    if not pairs:
        return set()
    n = len(pairs)
    best = [1] * n
    prev = [-1] * n
    for a in range(n):
        for b in range(a):
            if pairs[b][0] < pairs[a][0] and pairs[b][1] < pairs[a][1] and best[b] + 1 > best[a]:
                best[a] = best[b] + 1
                prev[a] = b
    k = max(range(n), key=lambda x: best[x])
    keep = set()
    while k != -1:
        keep.add(k)
        k = prev[k]
    return keep


def build_volume(conn, book: str, volume: int, items: list[dict]) -> list[dict]:
    rows = conn.execute(
        "SELECT pdf_page, printed_page, raw_text FROM pages WHERE book=? AND volume=? "
        "ORDER BY pdf_page", (book, volume)).fetchall()
    body = [(p, pr, flat((raw or "")[:HEAD_CHARS])) for p, pr, raw in rows if pr]
    if not body:
        return []
    idx_by_pdf = {b[0]: i for i, b in enumerate(body)}
    printed_to_pdf: dict[int, int] = {}
    for p, pr, _ in body:
        if str(pr).isdigit():
            printed_to_pdf.setdefault(int(pr), p)

    pairs: list[tuple[int, int]] = []
    meta: list[tuple[int, int]] = []          # 与 pairs 同序：(条目序, body 下标)
    for i, it in enumerate(items):
        page = it.get("page")
        if page is not None and page in printed_to_pdf:
            j = idx_by_pdf[printed_to_pdf[page]]
            pairs.append((i, j))
            meta.append((i, j))
            continue
        key = flat(it["title"])
        if len(key) < KEY_MIN:
            continue
        probe = key[:KEY_MAX]
        hits = [j for j, b in enumerate(body) if probe in b[2]]
        if not hits:
            n = len(key)
            hits = [j for j, b in enumerate(body)
                    if b[2] and difflib.SequenceMatcher(None, key, b[2][: max(n, 12)]).ratio() >= FUZZY_MIN]
        for j in hits:
            pairs.append((i, j))
            meta.append((i, j))

    keep = longest_monotone(pairs)
    out: list[dict] = []
    for k in sorted(keep, key=lambda x: pairs[x]):
        i, j = meta[k]
        pdf_page, printed, _ = body[j]
        title = items[i]["title"]
        out.append({
            "title": title,
            "pdf_page": pdf_page,
            "printed_page": printed,
            "level": items[i].get("level", 1),
            "kind": "year" if _YEAR.match(title) else
                    ("section" if any(h in title for h in _SECTION) else "body"),
        })
    return out


def verified_count(conn, book: str, volume: int, entries: list[dict]) -> int:
    """核验：条目落到的那一页，开头是否确实含该篇名。"""
    pages = dict(conn.execute(
        "SELECT pdf_page, raw_text FROM pages WHERE book=? AND volume=?", (book, volume)))
    ok = 0
    for e in entries:
        head = flat((pages.get(e["pdf_page"]) or "")[:HEAD_CHARS])
        key = flat(e["title"])[:8]
        if key and key in head:
            ok += 1
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", action="append")
    ap.add_argument("--all-new", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not SCAN.exists():
        raise SystemExit(f"缺少 {SCAN}；请先跑 scripts/_ocr_book_toc.py")
    data = json.loads(SCAN.read_text(encoding="utf-8"))
    books = args.book or (sorted(data) if args.all_new else None)
    if not books:
        raise SystemExit("请指定 --book 或 --all-new")

    conn = sqlite3.connect(str(DB_PATH))
    changed = 0
    try:
        for book in books:
            for vs in sorted(data.get(book, {}), key=int):
                v = int(vs)
                items = data[book][vs]
                if not items:
                    continue
                new = build_volume(conn, book, v, items)
                new_ok = verified_count(conn, book, v, new)
                cur = [dict(zip(("title", "pdf_page", "printed_page", "level", "kind"), r))
                       for r in conn.execute(
                           "SELECT title,pdf_page,printed_page,level,kind FROM toc_entries "
                           "WHERE book=? AND volume=? ORDER BY sort_order", (book, v))]
                cur_ok = verified_count(conn, book, v, cur)
                win = new_ok > cur_ok
                print(f"[{book} 卷{v}] 现有 {len(cur):>3} 条(核验 {cur_ok:>3})  "
                      f"重扫 {len(new):>3} 条(核验 {new_ok:>3})  → {'替换 ✅' if win else '保留现有'}")
                if win and args.apply and not args.dry_run:
                    sf = conn.execute("SELECT source_file FROM pages WHERE book=? AND volume=? LIMIT 1",
                                      (book, v)).fetchone()[0]
                    conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (book, v))
                    conn.executemany(
                        "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
                        "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
                        [(book, v, sf, e["title"], e["pdf_page"], e["printed_page"],
                          e["level"], e["kind"], i + 1) for i, e in enumerate(new)])
                    changed += 1
        if args.apply and not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    if args.apply and not args.dry_run:
        update_hash()
        print(f"\n已替换 {changed} 卷的目录，并重算 sha256。")
    else:
        print("\n[未写库] 加 --apply 生效。")


if __name__ == "__main__":
    main()
