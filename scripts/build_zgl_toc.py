# -*- coding: utf-8 -*-
"""《治国理政》卷2/4/5 的目录与印刷页码修缮（定向写入 corpus.sqlite）。

这三卷无可用书签（卷2/5 没有书签、卷4 书签为乱码 OCR），但 PDF 自带重排版目录页
（标题[+点引线+页码]，下一行日期括注，间有「一、二、…」专题头）。本脚本：
1. 从文本层目录页解析篇名/专题（标题可跨行；不信目录页码的 OCR 准确性，定位以正文为准）；
2. 在正文页中按「页首含篇名」定位每篇起始 pdf 页；
3. 卷2/卷4 为重排文本 PDF，页面无页码行 → 由「目录页码 vs 正文 pdf 页」偏移众数
   合成 pages.printed_page = pdf − offset（仅正文区间）；卷5 页码已可直接检测，不动 pages；
4. DELETE/INSERT 该卷 toc_entries（专题 kind=section level=1，篇目 kind=body level=2）。

须在 build_textbook_index --book 治国理政 之后运行。用法：
  python scripts/build_zgl_toc.py [--only 2 4 5]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import BUILD_DB_PATH, normalize  # noqa: E402

HASH_PATH = BUILD_DB_PATH.with_suffix(BUILD_DB_PATH.suffix + ".sha256")
BOOK = "治国理政"

# synth: none=页码已可检测不动 pages；const=印刷页=pdf−恒定偏移（重排版与纸书逐页对应）；
# interp=逐篇锚点分段线性插值（重排版页密度与纸书漂移，目录页码=纸书页码）
VOLS = {
    2: {"source_file": "pdfs/《治国理政》/《治国理政》第二卷.pdf", "synth": "interp"},
    4: {"source_file": "pdfs/《治国理政》/《治国理政》第四卷.pdf", "synth": "interp"},
    5: {"source_file": "pdfs/《治国理政》/《治国理政》第五卷.pdf", "synth": "none"},
}

_DATE_LINE = re.compile(
    r"^[（(]\s*[12０-９0-9\s]{4,}年[^）)]{0,30}[日月][^）)]{0,16}[)）]?\s*$"
)
# 页码数字粘在日期行前（卷4 排版提取顺序所致）：捕获前缀数字作为该篇页码
_DATE_LINE_PFX = re.compile(
    r"^(\d{1,3})\s*[（(]\s*[12０-９0-9\s]{4,}年[^）)]{0,30}[日月][^）)]{0,16}[)）]?\s*$"
)
_SECTION = re.compile(r"^[一二三四五六七八九十]{1,3}、")
_TRAIL = re.compile(r"[·⋯…\.、\s\d—一–\-]+$")
_PAGENUM_TAIL = re.compile(r"[·⋯…\.\s]*\d{1,3}\s*$")
_HEADER = re.compile(r"^(目录|习近平谈治国理政|治国理政|第[一二三四五]卷)$")


def flat_norm(s: str) -> str:
    return normalize(re.sub(r"\s+", "", s))


def parse_toc_pages(page_texts: list[tuple[int, str]], first_body: int) -> tuple[list[dict], int]:
    """解析目录页 → ([{kind: section|body, title, toc_page(可无)}], 目录区末页)。"""
    start = None
    for pdf_page, text in page_texts:
        if pdf_page >= first_body:
            break
        first_lines = [l.strip() for l in text.splitlines() if l.strip()][:2]
        if any(re.sub(r"\s", "", l) == "目录" for l in first_lines):
            start = pdf_page
            break
    if start is None:
        return [], 0
    # 目录区末页：自 start 起「每页含 ≥2 条日期行」的连续区段（正文篇首页只有 1 条日期行；
    # 排版空白页不打断区段）
    toc_end = start
    for pdf_page, text in page_texts:
        if pdf_page < start:
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        ndates = sum(1 for l in lines if _DATE_LINE.match(l.strip()) or _DATE_LINE_PFX.match(l.strip()))
        if pdf_page == start or (pdf_page == toc_end + 1 and (ndates >= 2 or not lines)):
            toc_end = pdf_page
        elif pdf_page > toc_end:
            break

    entries: list[dict] = []
    acc: list[str] = []

    def close_article():
        joined = "".join(acc)
        m = re.search(r"(\d{1,3})\s*$", joined)
        toc_page = int(m.group(1)) if m else None
        title = _TRAIL.sub("", joined).strip()
        if 2 <= len(title) <= 60:
            entries.append({"kind": "body", "title": title, "toc_page": toc_page})
        acc.clear()

    for pdf_page, text in page_texts:
        if pdf_page < start:
            continue
        if pdf_page > toc_end:
            break
        for line in text.splitlines():
            l = line.strip()
            if not l or _HEADER.match(re.sub(r"\s", "", l)):
                continue
            if _DATE_LINE.match(l):
                if acc:
                    close_article()
                continue
            m_pfx = _DATE_LINE_PFX.match(l)
            if m_pfx:
                if acc:
                    acc.append(m_pfx.group(1))  # 前缀数字即该篇页码
                    close_article()
                continue
            if _SECTION.match(l) and not acc:
                # 专题头（可跨一行：下一行併入交给下一轮判断，先存成 section，
                # 若下一行不含页码尾且很短则并入）
                entries.append({"kind": "section", "title": _TRAIL.sub("", l).strip(), "toc_page": None})
                continue
            if (entries and entries[-1]["kind"] == "section" and not acc
                    and not entries[-1].get("_cont_done")
                    and not _PAGENUM_TAIL.search(l) and len(l) <= 8 and not _SECTION.match(l)):
                # 专题头换行的续行（最多一行、很短才并入，防吞下一篇篇名）
                entries[-1]["title"] += _TRAIL.sub("", l).strip()
                entries[-1]["_cont_done"] = True
                continue
            acc.append(l)
    if acc:
        close_article()
    return entries, toc_end


def locate_articles(entries: list[dict], rows: list[tuple]) -> int:
    """对 body 条目在正文页定位 pdf_page（页首含篇名前缀），强制单调向前。返回定位数。"""
    import difflib

    located = 0
    pos = 0
    for e in entries:
        if e["kind"] != "body":
            continue
        key = flat_norm(e["title"])[:12]
        if not key:
            continue
        for idx in range(pos, len(rows)):
            pdf_page, raw = rows[idx]
            head = flat_norm(re.sub(r"\s+", "", raw)[:90])
            if key in head[:50]:
                e["pdf_page"] = pdf_page
                pos = idx + 1
                located += 1
                break
    # 第二遍：精确前缀失配的（正文 OCR 误字），在已定位邻居夹出的窗口内模糊匹配
    bodies = [e for e in entries if e["kind"] == "body"]
    page_index = {p: i for i, (p, _r) in enumerate(rows)}
    for k, e in enumerate(bodies):
        if e.get("pdf_page"):
            continue
        prev_p = next((bodies[j]["pdf_page"] for j in range(k - 1, -1, -1) if bodies[j].get("pdf_page")), None)
        next_p = next((bodies[j]["pdf_page"] for j in range(k + 1, len(bodies)) if bodies[j].get("pdf_page")), None)
        lo = page_index.get(prev_p, -1) + 1 if prev_p else 0
        hi = page_index.get(next_p, len(rows)) if next_p else len(rows)
        key = flat_norm(e["title"])
        if len(key) < 4:
            continue
        best, best_r = None, 0.0
        for idx in range(lo, hi):
            pdf_page, raw = rows[idx]
            head = flat_norm(re.sub(r"\s+", "", raw)[:90])[: len(key) + 10]
            r = difflib.SequenceMatcher(None, key, head).ratio()
            if r > best_r:
                best, best_r = pdf_page, r
        if best is not None and best_r >= 0.55:
            e["pdf_page"] = best
            located += 1
    return located


def bookmark_anchors(entries: list[dict], pdf_path: Path) -> int:
    """卷4 专用：正文 OCR 烂、页首匹配不可靠，但 PDF 书签 dest 精确（标题乱码）。
    把干净的目录标题与书签标题模糊配对，取 dest 作为 pdf_page。返回锚定数。"""
    import difflib
    import fitz

    doc = fitz.open(pdf_path)
    marks = [(re.sub(r"\s+", "", t), pg) for _lv, t, pg in doc.get_toc() if pg > 0]
    doc.close()
    used: set[int] = set()
    anchored = 0
    for e in entries:
        if e["kind"] != "body" or e.get("pdf_page"):
            continue
        key = flat_norm(e["title"])
        best, best_r = None, 0.0
        for j, (mt, pg) in enumerate(marks):
            if j in used:
                continue
            r = difflib.SequenceMatcher(None, key, flat_norm(mt)).ratio()
            if r > best_r:
                best, best_r = j, r
        if best is not None and best_r >= 0.55:
            e["pdf_page"] = marks[best][1]
            used.add(best)
            anchored += 1
    return anchored


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", type=int, help="只处理指定卷号（默认 2 4 5）")
    args = ap.parse_args()
    vols = {v: c for v, c in VOLS.items() if not args.only or v in args.only}

    conn = sqlite3.connect(str(BUILD_DB_PATH))
    try:
        for vol, cfg in vols.items():
            sf = cfg["source_file"]
            prows = conn.execute(
                "SELECT pdf_page, raw_text, printed_page FROM pages WHERE book=? AND volume=? ORDER BY pdf_page",
                (BOOK, vol),
            ).fetchall()
            if not prows:
                raise SystemExit(f"卷{vol} pages 为空，请先跑 build_textbook_index --book {BOOK}")
            page_texts = [(p, t or "") for p, t, _pr in prows]
            n = len(prows)

            # 估正文起点：跳过前置（目录最多到前 40 页内结束）
            entries, toc_end = parse_toc_pages(page_texts, first_body=min(40, n))
            if not entries:
                raise SystemExit(f"卷{vol} 未解析到目录条目")
            body_rows = [(p, t) for p, t, _pr in prows if p > toc_end]
            if vol == 4:
                # 卷4 正文 OCR 烂、页首匹配不可靠 → 只用精确的书签 dest 锚定
                located = bookmark_anchors(entries, ROOT / sf)
            else:
                located = locate_articles([e for e in entries], body_rows)

            # 偏移众数（pdf − 目录页码；目录页码即纸书页码）
            offs = Counter(
                e["pdf_page"] - e["toc_page"]
                for e in entries
                if e["kind"] == "body" and e.get("pdf_page") and e.get("toc_page")
            )
            offset = offs.most_common(1)[0][0] if offs else None
            agree = offs.most_common(1)[0][1] if offs else 0

            # 未定位的 body 条目：const 模式用 toc_page+offset 兜底（interp 漂移卷不可）
            if cfg["synth"] == "const" and offset is not None:
                for e in entries:
                    if e["kind"] == "body" and not e.get("pdf_page") and e.get("toc_page"):
                        cand = e["toc_page"] + offset
                        if toc_end < cand <= n:
                            e["pdf_page"] = cand
            # 专题头页 = 下一个有页的 body 条目所在页
            for i, e in enumerate(entries):
                if e["kind"] == "section":
                    nxt = next((x for x in entries[i + 1:] if x.get("pdf_page")), None)
                    e["pdf_page"] = nxt["pdf_page"] if nxt else None
            entries = [e for e in entries if e.get("pdf_page")]
            # 按解析（阅读）顺序做单调过滤：pdf 页倒退的条目视为锚定/页码损坏，丢弃
            mono_entries: list[dict] = []
            for e in entries:
                if mono_entries and e["pdf_page"] < mono_entries[-1]["pdf_page"]:
                    if e["kind"] == "section":
                        continue
                    # body 倒退：丢弃该条（页码尾数字被点引线吃掉等）
                    continue
                mono_entries.append(e)
            entries = mono_entries

            # 印刷页码合成
            printed_by_page: dict[int, str] = {p: pr for p, _t, pr in prows if pr}
            synth = 0
            if cfg["synth"] == "const" and offset is not None:
                body_start = min(e["pdf_page"] for e in entries if e["kind"] == "body")
                upd = [(str(p - offset), BOOK, vol, p) for p, _t, _pr in prows
                       if p >= body_start and p - offset >= 1]
                conn.executemany(
                    "UPDATE pages SET printed_page=? WHERE book=? AND volume=? AND pdf_page=?", upd)
                synth = len(upd)
                printed_by_page.update({p: str(p - offset) for p in range(body_start, n + 1) if p - offset >= 1})
            elif cfg["synth"] == "interp":
                # 锚点 =（定位 pdf 页, 目录纸书页码），逐段线性插值，单调钳制
                anchors = [(e["pdf_page"], e["toc_page"]) for e in entries
                           if e["kind"] == "body" and e.get("toc_page") and e.get("pdf_page")]
                anchors.sort()
                mono: list[tuple[int, int]] = []
                for p, q in anchors:
                    if mono and (p <= mono[-1][0] or q <= mono[-1][1]):
                        continue
                    mono.append((p, q))
                upd = []
                if len(mono) >= 2:
                    for p, _t, _pr in prows:
                        if p < mono[0][0]:
                            continue
                        # 找所在锚段
                        seg = None
                        for j in range(len(mono) - 1):
                            if mono[j][0] <= p < mono[j + 1][0]:
                                seg = (mono[j], mono[j + 1])
                                break
                        if seg is None:  # 末锚之后：斜率 1 顺延
                            q = mono[-1][1] + (p - mono[-1][0])
                        else:
                            (p0, q0), (p1, q1) = seg
                            q = round(q0 + (p - p0) * (q1 - q0) / (p1 - p0))
                        if q >= 1:
                            upd.append((str(q), BOOK, vol, p))
                    conn.executemany(
                        "UPDATE pages SET printed_page=? WHERE book=? AND volume=? AND pdf_page=?", upd)
                    synth = len(upd)
                    printed_by_page.update({int(r[3]): r[0] for r in upd})

            conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (BOOK, vol))
            payload = []
            for k, e in enumerate(entries):
                level = 1 if e["kind"] == "section" else 2
                kind = "section" if e["kind"] == "section" else "body"
                payload.append(
                    (BOOK, vol, sf, e["title"], e["pdf_page"],
                     printed_by_page.get(e["pdf_page"]), level, kind, k + 1)
                )
            conn.executemany(
                "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, printed_page, level, kind, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?)", payload,
            )
            nbody = sum(1 for e in entries if e["kind"] == "body")
            nsec = len(entries) - nbody
            print(f"[卷{vol}] 目录 {len(entries)} 条（篇目{nbody}+专题{nsec}），正文定位 {located}，"
                  f"偏移={offset}（{agree} 点吻合），合成页码 {synth} 页")
        conn.commit()
    finally:
        conn.close()
    digest = hashlib.sha256()
    with BUILD_DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")
    print("完成。")


if __name__ == "__main__":
    main()
