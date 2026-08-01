# -*- coding: utf-8 -*-
"""为《马克思恩格斯选集》(key=马恩选集) 定向构建目录(toc_entries)，并清理前置页的误检印刷页码。

本书 4 卷均为**文本层 PDF**（正文/页码由 build_textbook_index.py 直接抽取），但 PDF **无书签**，
故目录不能走通用 build_toc.py（书签路线），改从前置「印刷目录页」的文本层解析。

目录页的文本层呈「逐片段成行」结构（实测）：
    '卡·马克思　《黑格尔法哲学批判》导言'   ← 标题行（责任者　题名，可折成多行）
    '１—１６'                                    ← 页码行（起页[—止页]，单独成行）
    '…………………'                              ← 点引线行（分隔，单独成行；卷三/四里渲染成 '!!!!'）
故用行状态机解析：累积标题片段 → 遇「纯页码行」结一条 → 点引线行跳过。
  · 有页码区间(起—止)者＝顶层著作/卷说明/卷末索引 → level 1；
  · 单页码者＝其下子目(序言/章节/书信等) → level 2。

**前置页印刷页码冲突**（关键）：每卷前面有两段各自从「1」开始编号的前置内容——目录页(印 1..a)
与「第×卷说明」(印 1..b)——它们的印刷页码与正文(印 1..N)**重号**。若不处理，印刷「1」会同时落在
目录页/说明页/正文首页三处，引文页码与篇章直达全乱。故：
  1) 自动判定正文首页 body_start（detect 出的印刷「1」中，开启最长连续递增序列者）；
  2) 把 body_start 之前所有页的 printed_page 置空（幂等，正文页码自此干净）；
  3) printed→pdf 映射只用正文区(pdf≥body_start)构建，目录条目据此落地。
  4) 「第×卷说明」自身是前置页(已置空)，无法走印刷映射 → 特判映射到 说明首页 pdf(=目录末页+1)。

只 DELETE/INSERT 本 book 的 toc_entries，并把前置页 printed_page 置空；完成重算 sha256。其它书库不动。

用法：python scripts/build_maxuanji_toc.py [--dry-run] [--only 1 3]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402

BOOK = "马恩选集"
DEFAULT_VOLS = [1, 2, 3, 4]
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

# 点引线字符集合（不同卷渲染不同：卷一/二为 '…'，卷三/四为 '!'，另兼容半角点/中点等）。
_LEADER_CHARS = set("…⋯·•∙.。．!！　 \t")
# 纯页码行：起页[—止页]（NFKC 后均为半角数字；区间连接号兼容多种长横/波浪/至到）。
_PAGE_LINE_RE = re.compile(r"^(\d{1,4})(?:\s*[—–~〜\-－至到]\s*(\d{1,4}))?$")
# 目录里的结构噪声行（页眉「目 录」拆片、卷末页脚书名）。精确匹配紧凑形，避免误伤正文条目。
_NOISE_COMPACT = {"目", "录", "目录", "马克思恩格斯选集", "目次"}
# 卷末/卷首结构性条目（归 kind=section，仅影响分组展示，不影响导航）。
_SECTION_HINTS = ("卷说明", "注释", "索引", "附录", "大事年表", "大事记", "插图", "编者")
# PUA 私用区（脚注剑标 / 之类）。
_PUA_RE = re.compile(r"[-]")


def clean_fragment(raw: str) -> str:
    """标题片段清洗：NFKC + 去 PUA/脚注星号，全角空格→半角，折叠空白，去首尾点引线/空白。"""
    s = unicodedata.normalize("NFKC", raw or "").replace("\x00", "")
    s = _PUA_RE.sub("", s)
    s = s.replace("　", " ").replace("＊", "").replace("*", "")
    # 去双向控制符（与既有 _clean_title 一致）
    s = re.sub(r"[‎‏‪-‮⁦-⁩]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # 去首尾点引线/装饰符（卷三/四里 '!'/'■' 等会粘到标题首尾）；中点·仅出现在人名中部，去首尾安全。
    return s.strip(" ·.．。…⋯-—!！■▪◆◇○●•∙*＊　\t")


def is_leader_line(raw: str) -> bool:
    """整行是否仅由点引线/空白组成（≥2 个引线字符）——目录条目间的分隔行。"""
    s = unicodedata.normalize("NFKC", raw or "").strip()
    if len(s.replace(" ", "")) < 2:
        return False
    return all(ch in _LEADER_CHARS for ch in s)


def page_line_match(s: str):
    """清洗后的行若是「纯页码行」返回 (start:int, end:int|None)，否则 None。"""
    m = _PAGE_LINE_RE.match(s)
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    return start, end


def _has_cjk(s: str) -> bool:
    return any("㐀" <= ch <= "鿿" for ch in s)


def find_toc_pages(text_by_pdf: dict[int, str]) -> list[int]:
    """从 pdf1 起、连续且「含≥2 条点引线行」的页即目录页；遇首个非目录页停止。"""
    pages: list[int] = []
    for p in range(1, max(text_by_pdf) + 1 if text_by_pdf else 1):
        txt = text_by_pdf.get(p, "")
        leaders = sum(1 for ln in txt.splitlines() if is_leader_line(ln))
        if leaders >= 2:
            pages.append(p)
        elif pages:
            break  # 目录块结束
        elif p > 20:
            break  # 容错：前 20 页都没目录则放弃
    return pages


def detect_body_start(printed_by_pdf: dict[int, str]) -> int:
    """正文首页 pdf：在所有「印刷=1」的候选里，取开启最长连续递增印刷序列者。

    每卷前置有两段从 1 起的编号(目录页/卷说明)，正文又从 1 起，故印刷「1」出现 3 次；
    正文那段后面跟着最长的 1,2,3,… 连号，据此可靠区分（不靠固定偏移、不怕前置页数差异）。"""
    ints: dict[int, int] = {}
    for pdf, pr in printed_by_pdf.items():
        if pr and str(pr).isdigit():
            ints[pdf] = int(pr)
    candidates = [pdf for pdf, v in ints.items() if v == 1]
    if not candidates:
        return 1

    def run_len(start_pdf: int) -> int:
        n, p, expect = 0, start_pdf, 1
        while ints.get(p) == expect:
            n += 1
            p += 1
            expect += 1
        return n

    return max(candidates, key=run_len)


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def parse_volume(text_by_pdf: dict[int, str], toc_pages: list[int],
                 printed_to_pdf: dict[str, int], shuoming_start_pdf: int) -> list[dict]:
    """行状态机解析目录页，返回 [{title, pdf_page, printed_page, level, kind}]。"""
    raw_entries: list[dict] = []
    acc: list[str] = []
    for pdf in toc_pages:
        first_nonempty = True  # 每个目录页顶部都有本页页码(版心folio)，须跳过——否则当标题
        for raw in text_by_pdf[pdf].splitlines():  # 跨页折行时它会被误当成标题的页码
            if is_leader_line(raw):
                continue  # 分隔行
            s = clean_fragment(raw)
            if not s:
                continue
            if first_nonempty:
                first_nonempty = False
                pm0 = page_line_match(s)
                if pm0 is not None and pm0[1] is None:
                    continue  # 页首单数字＝本页 folio，跳过（保留已累积的折行标题）
            compact = s.replace(" ", "")
            if compact in _NOISE_COMPACT:
                acc = []  # 页眉/页脚结构行 → 重置，不并入下一条
                continue
            pm = page_line_match(s)
            if pm is None:
                acc.append(s)  # 标题片段累积（折行无分隔，片段内 　 已成空格）
                continue
            # 纯页码行：结一条目录项
            start, end = pm
            title = "".join(acc).strip(" ·.．…⋯-—")
            acc = []
            if not title or not _has_cjk(title) or title.replace(" ", "") in _NOISE_COMPACT:
                continue
            is_range = end is not None
            is_section = any(h in title for h in _SECTION_HINTS)
            raw_entries.append({
                "title": title,
                "start": start,
                "is_range": is_range,
                "level": 1 if (is_range or is_section) else 2,
                "kind": "section" if is_section else "body",
            })

    # 落地 pdf 页：「卷说明」特判到说明首页；其余走正文印刷映射。
    entries: list[dict] = []
    for e in raw_entries:
        if "卷说明" in e["title"]:
            pdf_page = shuoming_start_pdf
            printed = None  # 前置页，无正文印刷页码
        else:
            pdf_page = printed_to_pdf.get(str(e["start"]))
            printed = str(e["start"])
            if pdf_page is None:
                continue  # 印刷页不在正文映射里（异常）→ 丢弃
        entries.append({
            "title": e["title"], "pdf_page": pdf_page, "printed_page": printed,
            "level": e["level"], "kind": e["kind"], "is_special": "卷说明" in e["title"],
        })

    # 偏移共识过滤：正文条目 (pdf−印刷) 偏移应一致；偏离中位数 >4 者多为映射噪声，剔除。
    # 特判条目（卷说明，无印刷页/偏移异常）不参与统计、永远保留。
    offs = sorted(e["pdf_page"] - int(e["printed_page"])
                  for e in entries if not e["is_special"] and e["printed_page"])
    if offs:
        med = offs[len(offs) // 2]
        entries = [e for e in entries
                   if e["is_special"] or not e["printed_page"]
                   or abs((e["pdf_page"] - int(e["printed_page"])) - med) <= 4]

    # 相邻同标题去重（目录里偶有重复列出）
    dedup: list[dict] = []
    for e in entries:
        if dedup and dedup[-1]["title"] == e["title"] and dedup[-1]["pdf_page"] == e["pdf_page"]:
            continue
        dedup.append(e)
    # 稳定按 pdf 页排序（卷说明在最前）
    dedup.sort(key=lambda e: (e["pdf_page"], 0 if e["is_special"] else 1))
    return dedup


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", type=int, help="只处理指定卷（默认 1 2 3 4）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    vols = args.only or DEFAULT_VOLS

    conn = sqlite3.connect(str(DB_PATH))
    total = 0
    try:
        for v in vols:
            rows = conn.execute(
                "SELECT pdf_page, printed_page, raw_text, source_file FROM pages "
                "WHERE book=? AND volume=? ORDER BY pdf_page", (BOOK, v),
            ).fetchall()
            if not rows:
                print(f"[第{v}卷] 无 pages，跳过（先跑 build_textbook_index.py）")
                continue
            source_file = rows[0][3]
            text_by_pdf = {r[0]: (r[2] or "") for r in rows}
            printed_by_pdf = {r[0]: r[1] for r in rows}

            body_start = detect_body_start(printed_by_pdf)
            toc_pages = find_toc_pages(text_by_pdf)
            shuoming_start_pdf = (max(toc_pages) + 1) if toc_pages else 1

            # 正文区印刷→pdf 映射（只取 pdf≥body_start，规避前置页重号）
            printed_to_pdf: dict[str, int] = {}
            for pdf in sorted(p for p in text_by_pdf if p >= body_start):
                pr = printed_by_pdf.get(pdf)
                if pr and str(pr).isdigit() and str(pr) not in printed_to_pdf:
                    printed_to_pdf[str(pr)] = pdf

            entries = parse_volume(text_by_pdf, toc_pages, printed_to_pdf, shuoming_start_pdf)
            print(f"[第{v}卷] 目录页 pdf{toc_pages[:1]}..{toc_pages[-1:]}({len(toc_pages)}页) "
                  f"正文首页 pdf{body_start} 说明首页 pdf{shuoming_start_pdf} → 解析 {len(entries)} 条")
            for e in entries[:4]:
                print(f"      L{e['level']} 印{e['printed_page']} pdf{e['pdf_page']}  {e['title'][:42]}")

            if args.dry_run or not entries:
                continue

            # 前置页印刷页码置空（幂等）：body_start 之前一律无正文印刷页码
            conn.execute(
                "UPDATE pages SET printed_page=NULL WHERE book=? AND volume=? AND pdf_page<?",
                (BOOK, v, body_start),
            )
            conn.execute("DELETE FROM toc_entries WHERE book=? AND volume=?", (BOOK, v))
            conn.executemany(
                "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
                "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
                [(BOOK, v, source_file, e["title"], e["pdf_page"], e["printed_page"],
                  e["level"], e["kind"], i + 1) for i, e in enumerate(entries)],
            )
            total += len(entries)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    if not args.dry_run:
        update_hash()
        print(f"\n已写入 toc_entries 共 {total} 条（book={BOOK}），并清理前置页印刷页码、重算 sha256。")
    else:
        print("\n[dry-run] 未写库。")


if __name__ == "__main__":
    main()
