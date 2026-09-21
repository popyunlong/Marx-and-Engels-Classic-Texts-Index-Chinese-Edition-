# -*- coding: utf-8 -*-
"""从「标题……起页」式印刷目录页解析目录（toc_entries），适用于无可用书签的扫描卷。

适用书目（书签是逐页数字、被 search.py 正确过滤掉，走书签只能得到零条）：
  ·《陈独秀文集》卷 1—4      条目形如「敬告青年 ……… 89（一九一五年九月十五日）」
  ·《李大钊全集》卷 1、5     同上（卷 2—4 自带篇名书签，走 scripts/build_toc.py，不在此列）
  ·《列宁年谱》卷 1—3        条目形如「1895年 ……… 121」，年份即标题
  ·《斯大林年谱》            条目形如「1901年 ……（8）」，页码带圆括号

与 scripts/build_maxuanji_toc.py 的区别：那本书的文本层把「标题 / 页码 / 点引线」拆成
独立的行，要用行状态机；这批书是 OCR 转录，一条目录基本就是一整行「标题+点引线+页码」，
反而更好认，但要处理三件事——标题过长会折行、日期括注可能跟在页码后面或另起一行、
目录页页眉（「2 李大钊全集 第一卷」「目录 3」）必须剔除。

定位方式：**不信目录页上印的页码指向，也不用它做偏移推算**，而是把「印刷页码」经
pages.printed_page 反查出 pdf 页——印刷页码本身已由 build_scan_volumes 的众数法定过，
比在目录页上二次 OCR 出来的数字可靠。

只 DELETE/INSERT 指定 book+volume 的 toc_entries；完成重算 sha256。
须在 scripts/build_scan_volumes.py 之后运行。

用法：
  python scripts/build_printed_toc_dots.py --book 陈独秀文集 [--dry-run]
  python scripts/build_printed_toc_dots.py --book 李大钊全集 --volumes 1 5
  python scripts/build_printed_toc_dots.py --all
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402

HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

# 书 → 处理哪些卷（None = 全部卷）。
# 李大钊全集只处理 1、5：其余三卷有干净的篇名书签，走 scripts/build_toc.py 更准。
# 《斯大林年谱》**不在此列**：它的印刷目录页 OCR 得不全（点引线是「·」长串，有一页整页
# 没解析出条目），实测只得 45 条、约 40 个年份；而从正文题注「1903 年 24 岁」识别能得
# 57 个年份，更完整 → 交 scripts/build_nianpu_toc.py。
DEFAULT_TARGETS: dict[str, list[int] | None] = {
    "陈独秀文集": None,
    "李大钊全集": [1, 5],
    "列宁年谱": [1, 2, 3],
}

# 目录条目行：标题 + ≥2 个点引线 + 页码（可带圆括号），页码后可跟日期括注。
_ENTRY = re.compile(
    r"^(?P<title>.+?)\s*[.．。·・･•∙…⋯‥\-—_]{2,}\s*[（(]?(?P<page>\d{1,4})[）)]?\s*"
    r"(?P<tail>[（(].*?[）)])?\s*$"
)
_INLINE_ENTRY = re.compile(
    r"(?P<title>.*?)[.．。·・･•∙…⋯‥\-—_]{2,}\s*[（(]?(?P<page>\d{1,4})[）)]?(?=\s|$)"
)
# 目录页页眉/页脚：「目录」「目录 3」「2 李大钊全集 第一卷」「陈独秀文集 第二卷 5」等。
_TOC_HEAD = re.compile(r"^\s*(?:\d{1,4}\s*)?(?:目\s*录|目\s*次)\s*(?:\d{1,4})?\s*$")
# 结构性尾部条目：索引/对照表之类，保留但标 kind=section
_SECTION_HINTS = ("索引", "对照表", "目录", "附录", "编者", "后记", "说明", "凡例", "前言")
# 年份型标题（《列宁年谱》《斯大林年谱》）——用于给 kind 打 year，与书签路线一致
_YEAR_TITLE = re.compile(r"^(?:1[89]|20)\d{2}\s*年$")
# 目录块中间允许连续多少页解析不出条目（见 find_toc_pages 的说明）。取 2：
# 正文首页恰好也解析不出条目，连着两页没有才收尾，既能跨过单张坏页又不会冲进正文。
_TOC_GAP_TOLERANCE = 2


def update_hash(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        return
    digest = hashlib.sha256()
    with db_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    db_path.with_suffix(db_path.suffix + ".sha256").write_text(digest.hexdigest() + "\n", encoding="utf-8")


def clean_title(raw: str) -> str:
    s = unicodedata.normalize("NFKC", raw or "")
    s = re.sub(r"[​-‏‪-‮⁦-⁩]", "", s)
    s = s.replace("　", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # 长篇名在目录页会被排版折成两行，拼回来时中间多出一个空格
    # （「……在东方大学中共旅莫支部大会上的 报告」）。中日韩文字之间的空格一律去掉，
    # 但不动「答王庸工 (国体)」这类中西文之间的空格。
    s = re.sub(r"(?<=[　-鿿豈-﫿])\s+(?=[　-鿿豈-﫿])", "", s)
    # 去首尾装饰/引线残余；中点·只出现在人名中部，去首尾安全
    return s.strip(" ·.．。…⋯-—_*＊■▪◆◇○●•∙\t")


def find_toc_pages(rows: list[tuple], max_scan: int = 60) -> list[int]:
    """前置区里找目录页：含「目录」标题行的页，及其后连续「≥2 条条目行」的页。"""
    text_by_page = {r[0]: (r[2] or "") for r in rows}
    pages = sorted(text_by_page)[:max_scan]
    start = None
    for p in pages:
        if any(_TOC_HEAD.match(l.strip()) and "目" in l for l in text_by_page[p].splitlines()):
            start = p
            break
    if start is None:
        # 回退：首个含 ≥4 条条目行的前置页
        for p in pages:
            n = sum(1 for l in text_by_page[p].splitlines() if _ENTRY.match(l.strip()))
            if n >= 4:
                start = p
                break
    if start is None:
        return []
    # 目录块中间**允许出现解析不出条目的坏页**：这批书的目录页排版是密排点引线，
    # 智谱内容过滤偶尔拦下整页、改由 RapidOCR 转录，而 RapidOCR 对点引线的切分方式
    # 得不到「标题……页码」的整行结构 → 该页条目行为 0。若一遇 0 就收尾，整个目录块会
    # 被腰斩：实测《陈独秀文集》卷二在 pdf9 断掉（170 条只剩 55），《李大钊全集》卷五
    # 在 pdf19 断掉（175 条只剩 132）。故改为「连续 _TOC_GAP_TOLERANCE 页都没有条目
    # 才认为目录结束」，中间的坏页跳过但不终止。
    out = [start]
    gap = 0
    for p in pages:
        if p <= start:
            continue
        n = sum(1 for l in text_by_page[p].splitlines() if _ENTRY.match(l.strip()))
        if n >= 2:
            out.append(p)
            gap = 0
        else:
            gap += 1
            if gap >= _TOC_GAP_TOLERANCE and len(out) > 1:
                break   # 目录块确实结束了
    return out


def parse_entries(rows: list[tuple], toc_pages: list[int]) -> list[dict]:
    """解析目录页，返回 [{title, printed}]（按出现顺序）。"""
    text_by_page = {r[0]: (r[2] or "") for r in rows}
    out: list[dict] = []
    acc: list[str] = []
    for p in toc_pages:
        for raw_line in text_by_page.get(p, "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if _TOC_HEAD.match(line):
                acc = []
                continue
            # GLM occasionally preserves a dense two-column TOC as one long
            # physical line. Parse every leader+page pair instead of letting
            # the anchored rule collapse the whole line into its final entry.
            inline = list(_INLINE_ENTRY.finditer(line))
            if len(inline) >= 2:
                for index, item in enumerate(inline):
                    title = clean_title(" ".join((acc if index == 0 else []) + [item.group("title")]))
                    if len(title) >= 2:
                        out.append({"title": title, "printed": int(item.group("page"))})
                acc = []
                continue
            m = _ENTRY.match(line)
            if not m:
                # 没有引线也没有页码：多半是折行的标题上半截，或纯日期括注行。
                flat = clean_title(line)
                if flat and not re.fullmatch(r"[（(].*?[）)]", flat):
                    acc.append(flat)
                continue
            title = clean_title(" ".join(acc + [m.group("title")]))
            acc = []
            if len(title) < 2:
                continue
            out.append({"title": title, "printed": int(m.group("page"))})
    return out


def is_running_head(title: str, book: str) -> bool:
    """目录页的页眉会被当成折行标题拼到下一条前面。用书名做数据驱动判定，不写死。"""
    t = re.sub(r"[\s\d]", "", title)
    return bool(t) and (t == book or t.startswith(book) and len(t) <= len(book) + 6)


def build_for_volume(conn: sqlite3.Connection, book: str, volume: int) -> tuple[list[dict], str]:
    rows = conn.execute(
        "SELECT pdf_page, printed_page, raw_text FROM pages WHERE book=? AND volume=? "
        "ORDER BY pdf_page", (book, volume)).fetchall()
    if not rows:
        return [], "无 pages"

    max_scan = min(len(rows), 180) if book.startswith("中共中央文件选集") else 60
    toc_pages = find_toc_pages(rows, max_scan=max_scan)
    if not toc_pages:
        return [], "未找到印刷目录页"
    parsed = parse_entries(rows, toc_pages)
    if not parsed:
        return [], f"目录页 pdf{toc_pages[0]}-{toc_pages[-1]} 解析出 0 条"

    # 印刷页码 → pdf 页（取该印刷页码首次出现的 pdf 页；目录页自身也带印刷页码，
    # 会与正文重号，故只在正文区建映射——正文区起点 = 目录块之后）
    body_from = max(toc_pages) + 1
    printed_to_pdf: dict[int, int] = {}
    for pdf_page, printed, _raw in rows:
        if pdf_page < body_from or not printed or not str(printed).isdigit():
            continue
        printed_to_pdf.setdefault(int(printed), pdf_page)

    # 篇首页的印刷页码偶尔被 OCR 漏掉（最常见的是正文第一页），但其后大量页码映射
    # 仍能给出稳定偏移。只在至少三页支持、且众数没有并列歧义时，才用该偏移补映射；
    # 这样能恢复「印刷第 1 页 → PDF 第 129 页」，又不会凭单个噪声数字猜页。
    offsets = Counter(pdf_page - printed for printed, pdf_page in printed_to_pdf.items())
    modal_offset: int | None = None
    if offsets:
        ranked = offsets.most_common(2)
        if ranked[0][1] >= 3 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            modal_offset = int(ranked[0][0])

    entries: list[dict] = []
    last_printed = -1
    dropped_back = dropped_unmapped = dropped_head = 0
    for item in parsed:
        title, printed = item["title"], item["printed"]
        if is_running_head(title, book):
            dropped_head += 1
            continue
        if printed < last_printed:      # 目录页码必须单调不减；回头的是 OCR 误读
            dropped_back += 1
            continue
        pdf_page = printed_to_pdf.get(printed)
        if pdf_page is None and modal_offset is not None:
            inferred = printed + modal_offset
            if body_from <= inferred <= int(rows[-1][0]):
                pdf_page = inferred
        if pdf_page is None:
            dropped_unmapped += 1
            continue
        last_printed = printed
        is_year = bool(_YEAR_TITLE.match(title))
        entries.append({
            "title": title,
            "pdf_page": pdf_page,
            "printed_page": str(printed),
            "level": 1,
            "kind": "year" if is_year else
                    ("section" if any(h in title for h in _SECTION_HINTS) else "body"),
        })

    note = (f"目录页 pdf{toc_pages[0]}-{toc_pages[-1]}（{len(toc_pages)} 页）解析 {len(parsed)} 条 → "
            f"落地 {len(entries)} 条（剔页眉 {dropped_head}、回头页码 {dropped_back}、"
            f"印刷页无对应正文 {dropped_unmapped}）")
    return entries, note


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="从印刷目录页（标题……页码）解析目录。")
    ap.add_argument("--book", action="append")
    ap.add_argument("--volumes", nargs="*", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="候选数据库路径")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    if args.book:
        targets = {b: (args.volumes or DEFAULT_TARGETS.get(b)) for b in args.book}
    elif args.all:
        targets = dict(DEFAULT_TARGETS)
    else:
        raise SystemExit("请指定 --book <书库键> 或 --all")

    conn = sqlite3.connect(str(args.db))
    total = 0
    try:
        for book, vols in targets.items():
            have = [r[0] for r in conn.execute(
                "SELECT DISTINCT volume FROM pages WHERE book=? ORDER BY volume", (book,))]
            if not have:
                print(f"[{book}] pages 表里没有这本书，跳过")
                continue
            for v in (vols or have):
                entries, note = build_for_volume(conn, book, v)
                print(f"[{book} 第{v}卷] {note}")
                for e in entries[:3]:
                    print(f"      印{e['printed_page']} pdf{e['pdf_page']}  {e['title'][:44]}")
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
        update_hash(args.db)
        print(f"\n已写入 toc_entries 共 {total} 条，并重算 sha256。")


if __name__ == "__main__":
    main()
