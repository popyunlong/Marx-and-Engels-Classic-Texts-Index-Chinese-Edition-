# -*- coding: utf-8 -*-
"""为「无可用书签的年谱」从 OCR 正文识别年份，建立年份级目录（toc_entries）。

适用：《斯大林年谱》《邓小平年谱》《列宁年谱》卷1—3。
不适用（这些书签本身就是年份级或篇名级，走通用 scripts/build_toc.py）：
《陈云年谱》《李大钊年谱》《毛泽东年谱》《列宁年谱》卷4。

年谱是编年体，读者按「1938 年」定位远比按页码自然，故目录做到「年」这一级即可
（与《陈云年谱》《毛泽东年谱》的书签粒度一致，界面上三套年谱观感统一）。

三套书的版面写法各不相同（实测各卷 sidecar 归纳），故两条判据并行、最后合流：

  A 题注式（强）：「1903 年 24 岁」「1879年诞生」「1915年 十一岁」——年份后跟年龄或
    「诞生」。它就印在年段开头那一行，**可能落在页面中部**（上一年的条目还没排满这页），
    故全页搜索。年龄的数字可能是阿拉伯数字也可能是汉字（《邓小平年谱》前几年用汉字）。
  B 页眉式（弱）：页首或页尾整行只有「年（+月）（+页码）」，如《列宁年谱》的「1887年 22」、
    《邓小平年谱》的「1938年5月 211」。这类页眉**每页都有**，本身不标记年段起点，
    只有「页眉年份发生变化」的那一页才是。

两者合流取每个年份的最早出现页（同一年若 A、B 都命中，以 A 的页为准——A 是真正印着
小标题的那一页）。早先版本写成「有 A 就不看 B」，结果《邓小平年谱》卷一只有 p16 一处
「1904年诞生」命中 A，整卷 43 个年份塌成 1 条；《列宁年谱》卷一也只识出 7 条。

三道防伪：
  · **只在正文页（printed_page 非空）上识别**。前置的印刷「目录」页本身就是一列年份
    （「1952年……(759)」），把它算进来会毒死整卷：实测《斯大林年谱》目录页 p19 先给出
    1952，随后 1879—1951 全被单调过滤当成「回头年份」剔掉，60 条塌成 2 条。
  · 页眉派生的年份必须在卷内出现足够页数（见 _HEAD_* 常量），挡一次性 OCR 误读；
    A 判据有「岁/诞生」做锚，单页即可信。
  · 单调过滤取**最长递增子序列**而非「从第一条起贪心」。年谱是严格编年的，但只要有一个
    偏大的假年份混在前面，贪心法就会把它之后的真年份全部误杀；LIS 反过来会丢掉那个孤立
    的假年份、保住主干。

只 DELETE/INSERT 指定 book+volume 的 toc_entries，其余书库与卷不动；完成重算 sha256。
须在 scripts/build_scan_volumes.py 注入正文之后运行。

用法：
  python scripts/build_nianpu_toc.py --book 邓小平年谱                 # 整套
  python scripts/build_nianpu_toc.py --book 列宁年谱 --volumes 1 2 3   # 指定卷
  python scripts/build_nianpu_toc.py --all --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402

HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

# 默认处理的书与卷（None = 该书全部卷）。
# 《列宁年谱》不在此列：它的印刷目录页把年份和起始页码印得很干净（「1895年……121」），
#   由 scripts/build_printed_toc_dots.py 解析可得约 36 个年份，而从正文识别只得 23 个
#   （该书只在年段首页印年份，OCR 漏掉一些就少一年）——用印刷目录更完整。卷 4 另有
#   22 条月份级书签，走 scripts/build_toc.py。
# 《斯大林年谱》反过来：印刷目录页 OCR 得不全（45 条），而正文题注「1903 年 24 岁」
#   能识出 57 个年份，故用本脚本。
DEFAULT_TARGETS: dict[str, list[int] | None] = {
    "斯大林年谱": None,
    "邓小平年谱": None,
}

# 年谱合理年份区间：马克思生年之后、当下之前。用于挡掉 OCR 把「第1879页」之类读成年份。
YEAR_MIN, YEAR_MAX = 1800, 2030

# A 题注式：年份 + 年龄/诞生。年龄可为阿拉伯数字（斯大林年谱「24 岁」）或汉字
# （邓小平年谱前期「十一岁」）；允许数字与「年」之间有空格（OCR 常拆开）。
_RE_AGE = re.compile(
    r"(?<![\d])((?:1[89]|20)\d{2})\s*年\s*(?:\d{1,3}\s*岁|[一二三四五六七八九十百廿卅]{1,5}\s*岁|诞\s*生)"
)
# B 页眉式：整行只有「年」，可选「月」，前后可各带一个页码。
# 覆盖实测的三种写法：「1883年」「1887年 22」「1938年5月 211」（页码在前的也认）。
_RE_HEAD = re.compile(
    r"^\s*(?:\d{1,4}\s+)?((?:1[89]|20)\d{2})\s*年\s*(?:\d{1,2}\s*月)?\s*(?:\d{1,4})?\s*$"
)
# 「页眉年份至少出现几页才采信」这条防伪规则，只在本书真的每页都有页眉时才成立。
# 实测两种版式并存：《邓小平年谱》88% 的页都有页眉（年份重复几十上百页，要求 ≥2 页
# 能挡住 OCR 误读）；《列宁年谱》只有年段起始页印年份、覆盖率约 2%（每个年份就出现
# 一次），同一条规则会把整卷目录全过滤光。故按覆盖率自适应。
_HEAD_DENSE_COVERAGE = 0.30   # 页眉覆盖率高于此值 = 逐页页眉版式
_HEAD_MIN_PAGES_DENSE = 2     # 逐页页眉版式下的最少出现页数
_HEAD_MIN_PAGES_SPARSE = 1    # 稀疏版式（年份只印在年段首页）下单页即采信


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def detect_page_years(text: str) -> tuple[int | None, int | None]:
    """返回 (题注年份 A, 页眉年份 B)；识别不到则为 None。"""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return None, None

    # A：题注式「19xx 年 nn 岁 / 十一岁 / 诞生」，全页搜索（年段起点常在页面中部）
    strong: int | None = None
    m = _RE_AGE.search(re.sub(r"[ \t]+", " ", text or ""))
    if m:
        strong = int(m.group(1))

    # B：页眉/页脚（首行或末行整行只有年月页码）
    head: int | None = None
    for line in (lines[0], lines[-1]):
        m = _RE_HEAD.match(line)
        if m:
            head = int(m.group(1))
            break
    return strong, head


def build_entries(rows: list[tuple]) -> tuple[list[dict], str]:
    """rows = [(pdf_page, printed_page, raw_text)] 按 pdf_page 升序。返回 (目录条目, 说明)。"""
    printed_by_page = {r[0]: r[1] for r in rows}
    # 只认正文页：前置的印刷「目录」页整页都是年份，会把整卷目录带偏（见模块注释）。
    # 若整卷都没检出印刷页码（罕见），退回全部页，总比一条不出好。
    body = [r for r in rows if r[1] not in (None, "")]
    if not body:
        body = rows

    strong_by_page: dict[int, int] = {}
    head_by_page: dict[int, int] = {}
    for pdf_page, _printed, raw in body:
        s, h = detect_page_years(raw)
        if s and YEAR_MIN <= s <= YEAR_MAX:
            strong_by_page[pdf_page] = s
        if h and YEAR_MIN <= h <= YEAR_MAX:
            head_by_page[pdf_page] = h

    # 页眉年份的出现页数，配合覆盖率决定「几页起采信」（见常量处说明）
    head_pages: dict[int, int] = {}
    for year in head_by_page.values():
        head_pages[year] = head_pages.get(year, 0) + 1
    coverage = len(head_by_page) / max(1, len(body))
    min_pages = _HEAD_MIN_PAGES_DENSE if coverage >= _HEAD_DENSE_COVERAGE else _HEAD_MIN_PAGES_SPARSE

    # 合流：每个年份取最早出现页；A（真正印着小标题那页）优先于 B（页眉）
    first_page: dict[int, int] = {}
    for page in sorted(head_by_page):
        year = head_by_page[page]
        if head_pages.get(year, 0) >= min_pages:
            first_page.setdefault(year, page)
    for page in sorted(strong_by_page):
        year = strong_by_page[page]
        if year not in first_page or page < first_page[year]:
            first_page[year] = page

    if not first_page:
        return [], "未识别到任何年份"
    source = (f"题注 {len(strong_by_page)} 处 / 页眉 {len(head_by_page)} 页"
              f"（覆盖率 {coverage:.0%}，采信门槛 {min_pages} 页）"
              f" → 合流得 {len(first_page)} 个年份")
    candidates = [(p, y) for y, p in sorted(first_page.items(), key=lambda kv: (kv[1], kv[0]))]

    # 单调过滤：取「按页序年份递增」的最长子序列（LIS）。
    # 不用贪心：一个偏大的假年份混在前面就会误杀其后全部真年份（实测目录页给出的 1952
    # 让 1879—1951 全军覆没）。LIS 会丢掉那个孤立假年份、保住主干。n 只有几十，O(n²) 足够。
    n = len(candidates)
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if candidates[j][1] < candidates[i][1] and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
                prev[i] = j
    keep: list[int] = []
    if n:
        k = max(range(n), key=lambda i: best[i])
        while k != -1:
            keep.append(k)
            k = prev[k]
        keep.reverse()

    entries = [{
        "title": f"{candidates[i][1]}年",
        "pdf_page": candidates[i][0],
        "printed_page": printed_by_page.get(candidates[i][0]),
        "level": 1,
        "kind": "year",
    } for i in keep]
    dropped = n - len(keep)
    if dropped:
        source += f"，单调（LIS）过滤剔除 {dropped} 条乱序年份"
    return entries, source


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="从 OCR 正文识别年份，为年谱建年份级目录。")
    ap.add_argument("--book", action="append", help="书库键，可重复。缺省用内置目标表。")
    ap.add_argument("--volumes", nargs="*", type=int, help="只处理指定卷（配合单个 --book）")
    ap.add_argument("--all", action="store_true", help="处理内置目标表里的全部书")
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    if args.book:
        targets = {b: (args.volumes or None) for b in args.book}
    elif args.all:
        targets = dict(DEFAULT_TARGETS)
    else:
        raise SystemExit("请指定 --book <书库键> 或 --all")

    conn = sqlite3.connect(str(DB_PATH))
    total = 0
    try:
        for book, vols in targets.items():
            have = [r[0] for r in conn.execute(
                "SELECT DISTINCT volume FROM pages WHERE book=? ORDER BY volume", (book,))]
            if not have:
                print(f"[{book}] pages 表里没有这本书，跳过（请先跑 build_scan_volumes.py）")
                continue
            for v in (vols or have):
                rows = conn.execute(
                    "SELECT pdf_page, printed_page, raw_text FROM pages "
                    "WHERE book=? AND volume=? ORDER BY pdf_page", (book, v)).fetchall()
                if not rows:
                    print(f"[{book} 第{v}卷] 无 pages，跳过")
                    continue
                entries, note = build_entries(rows)
                span = f"{entries[0]['title']}—{entries[-1]['title']}" if entries else "—"
                print(f"[{book} 第{v}卷] {len(rows)} 页 → 目录 {len(entries)} 条（{span}）；{note}")
                for e in entries[:3]:
                    print(f"      印{e['printed_page']} pdf{e['pdf_page']}  {e['title']}")
                if args.dry_run or not entries:
                    continue
                source_file = conn.execute(
                    "SELECT source_file FROM pages WHERE book=? AND volume=? LIMIT 1",
                    (book, v)).fetchone()[0]
                conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (book, v))
                conn.executemany(
                    "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
                    "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
                    [(book, v, source_file, e["title"], e["pdf_page"], e["printed_page"],
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
        print(f"\n已写入 toc_entries 共 {total} 条，并重算 sha256。")


if __name__ == "__main__":
    main()
