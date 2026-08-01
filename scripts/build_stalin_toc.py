# -*- coding: utf-8 -*-
"""《斯大林全集》专用目录构建：印刷目录只取篇名，篇章位置回正文定位。

为什么不能走既有的两条路（都试过、都不行）：
  · scripts/build_printed_toc_dots.py（标题……页码）：这套书目录页的**页码整列没被 OCR
    抄下来**，只剩篇名和点引线，解析器一条也认不出。
  · build_scan_volumes 的 detect 模式（正文识别「篇名（日期）」）：每卷只得 0—9 条。
另外，让模型直接输出「篇名+页码」的专项提示词会**诱发幻觉**（卷九目录页抄出了「论持久战」
等毛泽东篇名），故页码这一列一律不采信——见 scripts/_ocr_stalin_toc.py 的说明。

本脚本的做法：篇名清单（_ocr_stalin_toc.py 产出，glm-4.1v-thinking-flash 抄的，准确）
+ 逐条回正文找篇首页。关键是**全局单调**：目录顺序 = 正文顺序，用最长公共递增匹配
（LIS 变体）在所有候选里选一条既单调又最长的链，孤立误匹配自然被排除。

用法：
  python scripts/build_stalin_toc.py [--volumes 1 2 3] [--dry-run]
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

BOOK = "斯大林全集"
TITLES = ROOT / "data" / "stalin_toc_titles.json"
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

HEAD_CHARS = 150      # 只在页面开头这么多字里找篇名（篇首页的标题一定在最前面）
KEY_MIN = 5           # 用作检索键的篇名前缀最短长度
KEY_MAX = 10          # 前缀过长会被 OCR 的个别错字打断
FUZZY_MIN = 0.72      # 前缀直配不中时，用相似度兜底的门槛


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8", newline="\n")


def flat(s: str) -> str:
    return normalize(re.sub(r"\s+", "", s or ""))


def candidates(titles: list[dict], body: list[tuple]) -> list[list[int]]:
    """每条篇名 → 可能的正文页下标列表（body 的下标）。"""
    heads = [b[2] for b in body]
    out: list[list[int]] = []
    for item in titles:
        key = flat(item["title"])
        hits: list[int] = []
        if len(key) >= KEY_MIN:
            probe = key[:KEY_MAX]
            for j, h in enumerate(heads):
                if probe in h:
                    hits.append(j)
            if not hits:
                # OCR 错字会打断前缀直配；退一步用相似度，只比对与篇名等长的开头
                n = len(key)
                for j, h in enumerate(heads):
                    seg = h[: max(n, 12)]
                    if seg and difflib.SequenceMatcher(None, key, seg).ratio() >= FUZZY_MIN:
                        hits.append(j)
        out.append(hits)
    return out


def longest_monotone(cands: list[list[int]]) -> dict[int, int]:
    """在「篇名序 × 候选页序」里选一条最长的严格递增链。

    目录顺序与正文顺序必然一致，所以真匹配一定构成一条递增链；散落的误匹配进不了最长链。
    规模很小（篇名 ≤ 250、候选 ≤ 数十），O(n²) 的 DP 足够。
    """
    pairs: list[tuple[int, int]] = []      # (篇名下标, 页下标)
    for i, hits in enumerate(cands):
        for j in hits:
            pairs.append((i, j))
    if not pairs:
        return {}
    pairs.sort()
    n = len(pairs)
    best = [1] * n
    prev = [-1] * n
    for a in range(n):
        ia, ja = pairs[a]
        for b in range(a):
            ib, jb = pairs[b]
            if ib < ia and jb < ja and best[b] + 1 > best[a]:
                best[a] = best[b] + 1
                prev[a] = b
    k = max(range(n), key=lambda x: best[x])
    chain: dict[int, int] = {}
    while k != -1:
        i, j = pairs[k]
        chain[i] = j
        k = prev[k]
    return chain


def build_volume(conn: sqlite3.Connection, volume: int, titles: list[dict]) -> tuple[list[dict], str]:
    rows = conn.execute(
        "SELECT pdf_page, printed_page, raw_text FROM pages WHERE book=? AND volume=? "
        "ORDER BY pdf_page", (BOOK, volume)).fetchall()
    if not rows:
        return [], "无 pages"
    # 只在正文区找（前置目录页本身也含篇名，不排除就会全部误匹配到目录页上）
    body = [(p, pr, flat((raw or "")[:HEAD_CHARS])) for p, pr, raw in rows if pr]
    if not body:
        return [], "该卷没有印刷页码，无法定位"

    cands = candidates(titles, body)
    chain = longest_monotone(cands)
    entries: list[dict] = []
    for i in sorted(chain):
        j = chain[i]
        pdf_page, printed, _ = body[j]
        entries.append({
            "title": titles[i]["title"],
            "pdf_page": pdf_page,
            "printed_page": printed,
            "level": titles[i].get("level", 1),
            "kind": "body",
        })
    n_cand = sum(1 for c in cands if c)
    note = (f"篇名 {len(titles)} 条，有候选 {n_cand} 条，单调链选中 {len(entries)} 条"
            f"（定位率 {len(entries)*100//max(1,len(titles))}%）")
    return entries, note


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--volumes", nargs="*", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not TITLES.exists():
        raise SystemExit(f"缺少篇名清单 {TITLES}；请先跑 scripts/_ocr_stalin_toc.py")
    data = json.loads(TITLES.read_text(encoding="utf-8"))

    conn = sqlite3.connect(str(DB_PATH))
    total = 0
    try:
        vols = args.volumes or sorted(int(k) for k in data)
        for v in vols:
            titles = data.get(str(v)) or []
            if not titles:
                print(f"[第{v}卷] 无篇名清单，跳过")
                continue
            entries, note = build_volume(conn, v, titles)
            print(f"[第{v}卷] {note}")
            for e in entries[:3]:
                print(f"      印{e['printed_page']} pdf{e['pdf_page']}  {e['title'][:40]}")
            if args.dry_run or not entries:
                continue
            source_file = conn.execute(
                "SELECT source_file FROM pages WHERE book=? AND volume=? LIMIT 1",
                (BOOK, v)).fetchone()[0]
            conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (BOOK, v))
            conn.executemany(
                "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
                "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
                [(BOOK, v, source_file, e["title"], e["pdf_page"], e["printed_page"],
                  e["level"], e["kind"], i + 1) for i, e in enumerate(entries)])
            total += len(entries)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    if args.dry_run:
        print("\n[dry-run] 未写库。")
    else:
        update_hash()
        print(f"\n已写入 toc_entries 共 {total} 条（book={BOOK}），并重算 sha256。")


if __name__ == "__main__":
    main()
