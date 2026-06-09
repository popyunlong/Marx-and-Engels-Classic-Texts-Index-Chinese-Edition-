# -*- coding: utf-8 -*-
"""为《毛泽东选集》就地构建检索/目录数据（pages + toc_entries）。

背景与设计
----------
《毛泽东选集》有两套来源：
  * 扫描版  pdfs/《毛泽东选集》/毛泽东选集第N卷.pdf —— 无文本层（纯图像），但有书页可读、
    可作书页图像渲染；目录书签的“标题”完整可用，但“页码目标”多为损坏（第2/3/4卷大量指向第1页）。
  * 文字版  毛泽东选集文字版/毛泽东选集第N卷文字版.pdf —— 文本层干净、可检索、页码可被
    detect_printed_page_from_page 稳定识别；但其自带书签（目录）不可用。

因此本脚本：
  - 正文 raw_text / normalized_text / printed_page 全部取自“文字版”；
  - 但每行的 source_file 锚定到“扫描版”相对路径、pdf_page 锚定到“扫描版”书页号，
    使「引文检索原文定位」「两个阅读器书页图像」「首页篇章直达」都落在扫描版上；
  - 目录（toc_entries）用“扫描版书签的标题 + 层级”，页码改由“文字版正文内容定位”重新求得，
    从而绕开损坏的书签页码。

页码对齐（关键）
----------------
扫描版与文字版逐卷页数差恒为 +2（扫描版多 2 页封面/书名等前置页），正文版式同为人民出版社
连续页码本，故“正文起始处”二者按 pdf 页索引对齐、偏移恒为 +2；文字版对“原书缺页”保留占位页，
不破坏对齐。脚本以正文最长连续递增页码段（body run）求得锚点，再以
    scan_pdf(P) = P + C_v,   C_v = anchor_pdf_idx + 2 - anchor_printed
将“印刷页码 P”映射到扫描版 pdf 页（印刷页键映射，对个别缺页/占位稳健）；前置页按 idx+2 顺位映射。
vol1 锚验证：中国社会各阶级的分析 文字版 pdf16/印刷3 → 扫描版 pdf18，C_1=15、scan(3)=18 ✓。

隔离原则（务必保持）
--------------------
  - 只 DELETE/INSERT book='毛泽东选集' 的 pages / toc_entries 行，绝不触碰其它书库；
  - 绝不调用 build_index.build()（那会 DROP 整张 pages 表）；在既有 corpus.sqlite 上原地热更新；
  - 可重复运行（先 DELETE 再 INSERT）；完成后重算 data/corpus.sqlite.sha256。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import fitz  # pymupdf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from build_index import (  # noqa: E402
    BUILD_DB_PATH,
    _as_int_page,
    detect_printed_page_from_page,
    normalize,
)

BOOK = "毛泽东选集"
TEXT_DIR = ROOT / "毛泽东选集文字版"
MANIFEST = ROOT / "config" / "manifest.yaml"
HASH_PATH = BUILD_DB_PATH.with_suffix(BUILD_DB_PATH.suffix + ".sha256")

FRONT_DELTA = 2  # 扫描版较文字版多出的前置页数（恒为 2）

# 目录里属于“前置/版权类”的标题，不作为正文章节收录。
NOISE_TITLES = {
    "封面", "书名", "版权", "版权页", "扉页", "内容提要", "插图目录", "图版目录",
}
# 前置导航标题：若扫描版书签给出的小页码可信（前置书签页码未损坏），直接采用其扫描页。
FRONT_NAV_TITLES = {"前言", "出版说明", "说明", "目录", "题词", "编者的话", "出版前言"}

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4}


def vol_of(name: str) -> int | None:
    m = re.search(r"第([一二三四])卷", name)
    return CN_NUM[m.group(1)] if m else None


def clean_title(raw: str) -> str:
    t = raw.replace("\x00", "")
    t = t.replace("　", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_trailing_date(title: str) -> str:
    """去掉标题末尾的（一九××年…）日期括注，便于在正文中定位标题主体。"""
    return re.sub(r"[（(][^）)]*[）)]\s*$", "", title).strip()


def strip_leading_enum(norm_title: str) -> str:
    """去掉归一化标题前缀的序号（一/二/十/数字），用于退化匹配。"""
    return re.sub(r"^(?:[一二三四五六七八九十百零〇]+|\d+)", "", norm_title)


def longest_body_run(dets: list[tuple[int, int | None]]) -> list[tuple[int, int]]:
    """在 (pdf_idx, printed_int|None) 序列中找“最长、按 idx 连续且印刷页 +1 递增”的段。

    这就是正文主体（body）；前置页里被误识为页码的零散短段会被自然排除。"""
    best: list[tuple[int, int]] = []
    cur: list[tuple[int, int]] = []
    prev: tuple[int, int] | None = None
    for idx, p in dets:
        if p is None:
            if len(cur) > len(best):
                best = cur
            cur, prev = [], None
            continue
        if prev is not None and idx == prev[0] + 1 and p == prev[1] + 1:
            cur.append((idx, p))
        else:
            if len(cur) > len(best):
                best = cur
            cur = [(idx, p)]
        prev = (idx, p)
    if len(cur) > len(best):
        best = cur
    return best


def build_volume_pages(vol: int, scan_source: str, scan_count: int, text_pdf: Path):
    """返回 (rows, body_pages)。

    rows: (book, vol, scan_source, scan_pdf_page, printed|None, raw, norm)
    body_pages: [(scan_pdf_page, printed_str, norm_text)]，供目录内容定位使用。
    """
    doc = fitz.open(text_pdf)
    raws: list[str] = []
    dets: list[tuple[int, int | None]] = []
    for i, page in enumerate(doc, start=1):
        raw = page.get_text("text")
        raws.append(raw)
        dets.append((i, _as_int_page(detect_printed_page_from_page(page))))
    doc.close()

    run = longest_body_run(dets)
    if not run:
        raise RuntimeError(f"第{vol}卷：未能在文字版中识别正文页码段（body run 为空）。")
    anchor_idx, anchor_printed = run[0]
    # 正文整体线性：printed(idx) = idx - K；K 由锚点求得。正文与前置页共享同一 +2 偏移，
    # 故 scan_pdf = text_idx + FRONT_DELTA 对全卷一致。
    k = anchor_idx - anchor_printed

    # 真正的正文起点：自第 1 页起，第一张“探测页码恰好落在全局线 idx-K 上”的页；
    # 其前为前置页（printed 置空）。这样即便最长连续段不在正文开头也能正确分界，
    # 并自然排除前置目录里被误识成页码的零散数字。
    body_start = anchor_idx
    for idx, det in dets:
        if det is not None and det == idx - k:
            body_start = idx
            break

    rows = []
    body_pages = []
    disagree = 0
    for i, raw in enumerate(raws, start=1):
        norm = normalize(raw)
        scan_pdf = max(1, min(scan_count, i + FRONT_DELTA))
        if i < body_start:
            printed_str = None  # 前置页：无可靠印刷页码
        else:
            lin = i - k
            det = dets[i - 1][1]
            if det is not None and abs(det - lin) > 3:
                disagree += 1  # 线性外推与探测严重不符——可能存在真实跳页，留作告警
            printed_str = str(lin) if lin >= 1 else None
            if printed_str is not None:
                body_pages.append((scan_pdf, printed_str, norm))
        rows.append((BOOK, vol, scan_source, scan_pdf, printed_str, raw, norm))

    diag = {
        "k": k, "body_start": body_start, "run_len": len(run), "disagree": disagree,
        "printed_lo": next((r[4] for r in rows if r[4]), None),
        "printed_hi": next((r[4] for r in reversed(rows) if r[4]), None),
        "scan_min": min(r[3] for r in rows), "scan_max": max(r[3] for r in rows),
        "scan_count": scan_count,
    }
    return rows, body_pages, diag


def build_volume_toc(vol: int, scan_pdf_path: Path, scan_count: int,
                     body_pages: list[tuple[int, str, str]],
                     printed_by_scan: dict[int, str | None]):
    """用扫描版书签的“标题/层级”，页码按卷分策略求得：

      * 扫描版书签页码可信（第1卷，损坏率低）→ 直接采用书签页码（已是扫描版 pdf 页）；
      * 书签页码损坏（第2/3/4卷大量指向第1页）→ 用“标题+日期”在文字版正文内容定位。
        正文每页都带“running header（卷名/篇名）”，单凭篇名会误命中续页；而页眉绝不含
        “（一九××年…日）”日期，故以“归一化(篇名+日期)”为主键即可锁定篇首页。
    """
    doc = fitz.open(scan_pdf_path)
    toc = doc.get_toc()
    doc.close()

    nonnoise = [(max(1, min(6, int(lv or 1))), clean_title(t), pg) for lv, t, pg in toc
                if clean_title(t) and clean_title(t) not in NOISE_TITLES]
    bad = sum(1 for _, _, pg in nonnoise if pg <= 1)
    bm_reliable = nonnoise and bad <= max(2, len(nonnoise) * 0.2)

    norms = body_pages  # [(scan_pdf, printed, norm)] 升序

    def global_find(key: str, top_only: bool = False):
        """全局最早命中。正文每页页眉重复“篇名”，但篇名只在篇首页及其后续页页眉出现，
        故“篇名（去日期）出现在页顶（前 80 归一化字符）的最早一页”必为篇首——既不依赖
        易受 OCR 干扰的日期（书签用〇，正文常作○/0），也不会被续页页眉前移。"""
        if len(key) < 4:
            return None
        for s, p, nm in norms:
            if key in (nm[:80] if top_only else nm):
                return (s, p)
        return None

    # —— 第 1 趟：可信书签直接采用；否则以“篇名置顶最早页”全局定位 ——
    slots = []  # (lvl, title, page_or_None)
    matched = 0
    for lvl, title, mark_page in nonnoise:
        if title in FRONT_NAV_TITLES and 1 <= mark_page <= 20:
            slots.append((lvl, title, mark_page)); matched += 1; continue
        if bm_reliable and 1 < mark_page <= scan_count:
            slots.append((lvl, title, mark_page)); matched += 1; continue
        ncore = normalize(strip_trailing_date(title))
        hit = global_find(ncore, top_only=True)
        if hit is None:                       # 退化：整名（含日期）正文任意处
            nfull = normalize(title)
            if nfull != ncore:
                hit = global_find(nfull)
        page = hit[0] if hit is not None else None
        if page is not None:
            matched += 1
        slots.append((lvl, title, page))

    # —— 第 2 趟：未定位者（分隔标题/子标题）按邻居补全，保证单调可点 ——
    def next_page(i):
        for j in range(i + 1, len(slots)):
            if slots[j][2] is not None:
                return slots[j][2]
        return None

    def prev_page(i):
        for j in range(i - 1, -1, -1):
            if slots[j][2] is not None:
                return slots[j][2]
        return None

    def is_period_divider(title: str) -> bool:
        # “第一次国内革命战争时期”“抗日战争时期（上）”等分期标题：紧邻其下首篇。
        return "时期" in title and len(title) <= 16

    unmatched = 0
    entries = []
    floor = norms[0][0] if norms else 1
    for i, (lvl, title, page) in enumerate(slots):
        if page is None:
            # 分期分隔标题 → 取下一已定位页（其首篇）；其余未定位者多为子标题（卷内被标为
            # level 1 的“一/二/三…”小节）→ 归入其父篇（上一已定位页），保证单调可点。
            if is_period_divider(title):
                page = next_page(i) or prev_page(i) or floor
            else:
                page = prev_page(i) or next_page(i) or floor
            unmatched += 1
        page = max(1, min(scan_count, int(page)))
        kind = "year" if (title.endswith("年") and title[:-1].isdigit()) else "body"
        entries.append({"title": title, "pdf_page": page,
                        "printed_page": printed_by_scan.get(page),
                        "level": lvl, "kind": kind})

    # —— 单调修复：书签按阅读顺序排列，页码必非递减。保留“最长非递减子序列”为可信锚点，
    #    其余少数离群项夹到其前一锚点页（如把误置的“别了，司徒雷登”从 1448 拉回邻页）。——
    pages = [e["pdf_page"] for e in entries]
    n = len(pages)
    if n > 2:
        dp = [1] * n
        par = [-1] * n
        for i in range(n):
            for j in range(i):
                if pages[j] <= pages[i] and dp[j] + 1 > dp[i]:
                    dp[i] = dp[j] + 1
                    par[i] = j
        end = max(range(n), key=lambda i: dp[i])
        keep = set()
        while end != -1:
            keep.add(end)
            end = par[end]
        prev_anchor = pages[min(keep)]
        for i in range(n):
            if i in keep:
                prev_anchor = pages[i]
            else:
                entries[i]["pdf_page"] = prev_anchor
                entries[i]["printed_page"] = printed_by_scan.get(prev_anchor)

    return entries, matched, unmatched


def update_hash() -> None:
    digest = hashlib.sha256()
    with BUILD_DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    items = manifest.get(BOOK) or []
    if not items:
        raise SystemExit(f"manifest.yaml 中未找到 {BOOK} 条目。")

    all_page_rows = []
    all_toc_payload = []
    for item in sorted(items, key=lambda x: x["volume"]):
        vol = int(item["volume"])
        scan_source = str(Path(item["file"]).as_posix())
        scan_pdf_path = (ROOT / scan_source).resolve()
        if not scan_pdf_path.exists():
            raise SystemExit(f"扫描版缺失：{scan_pdf_path}")
        text_pdf = next((p for p in TEXT_DIR.glob("*.pdf") if vol_of(p.name) == vol), None)
        if text_pdf is None:
            raise SystemExit(f"文字版缺失：第{vol}卷（{TEXT_DIR}）")

        scan_count = fitz.open(scan_pdf_path).page_count
        rows, body_pages, diag = build_volume_pages(vol, scan_source, scan_count, text_pdf)
        printed_by_scan = {r[3]: r[4] for r in rows}
        toc, matched, unmatched = build_volume_toc(
            vol, scan_pdf_path, scan_count, body_pages, printed_by_scan
        )

        all_page_rows.extend(rows)
        for order, e in enumerate(toc, start=1):
            all_toc_payload.append((
                BOOK, vol, scan_source, e["title"], e["pdf_page"],
                e["printed_page"], e["level"], e["kind"], order,
            ))

        print(
            f"[第{vol}卷] 文字版页={len(rows)} 扫描版页={scan_count} "
            f"body起={diag['body_start']}(K={diag['k']},len={diag['run_len']},drift={diag['disagree']}) "
            f"印刷页={diag['printed_lo']}..{diag['printed_hi']} "
            f"scan={diag['scan_min']}..{diag['scan_max']} "
            f"目录={len(toc)}条(定位{matched}/未命中{unmatched})"
        )

    conn = sqlite3.connect(str(BUILD_DB_PATH))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS toc_entries (
                book TEXT NOT NULL, volume INTEGER NOT NULL, source_file TEXT NOT NULL,
                title TEXT NOT NULL, pdf_page INTEGER NOT NULL, printed_page TEXT,
                level INTEGER NOT NULL, kind TEXT NOT NULL, sort_order INTEGER NOT NULL
            )
            """
        )
        conn.execute("DELETE FROM pages WHERE book = ?", (BOOK,))
        conn.execute("DELETE FROM toc_entries WHERE book = ?", (BOOK,))
        conn.executemany(
            "INSERT INTO pages (book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            all_page_rows,
        )
        conn.executemany(
            "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, printed_page, level, kind, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            all_toc_payload,
        )
        conn.commit()
    finally:
        conn.close()

    update_hash()
    print(f"\n已写入 pages {len(all_page_rows)} 行、toc_entries {len(all_toc_payload)} 行（book={BOOK}）。")
    print(f"已更新校验和：{HASH_PATH}")


if __name__ == "__main__":
    main()
