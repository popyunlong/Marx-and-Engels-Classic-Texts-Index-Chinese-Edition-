# -*- coding: utf-8 -*-
"""为《习近平经济文选》第一卷定向构建目录（toc_entries），并清理前置页的误检印刷页码。

为什么不用通用 build_toc.py：本书 PDF 的书签（200 条）把每一篇文章拆成了多条同页书签——
标题折行片段（1~3 条）+ 一条单独的日期行 `（二〇XX年…）`。通用 build_toc 会把每条书签
原样写成一条目录项，导致目录碎成「人民对美好生活的向往，/就是我们的奋斗目标＊/（…日）」三行。

本脚本按「日期行结束一篇」的规律，把同一篇的标题片段合并为一条，并把日期并入标题
（用于消歧——书中有多篇同名的「在中央经济工作会议上的讲话（节选）」，仅靠日期区分），
然后定向 DELETE/INSERT 该 book 的 toc_entries（绝不触碰其它书库）。

另外：build_textbook_index 的 detect_printed_page 在两页前置目录页（pdf 9/15）上误把目录里的
页码引用当成本页印刷页码，故本脚本把正文首页（印刷「1」）之前的所有前置页 printed_page 置空，
确保引文页码干净。该步可在服务器上重复执行（幂等）。

用法：python scripts/build_xi_econ_toc.py
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

from build_index import DB_PATH  # noqa: E402

BOOK = "习近平经济文选"
MANIFEST = ROOT / "config" / "manifest.yaml"
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

# 日期行允许出现的字符集合（中文数字 / 年月日 / 范围连接号 / 顿号 / 空白等）。
# 一行若「（…）」包裹且内部仅由这些字符组成，判定为纯日期行（篇末标记）。
_DATE_CHARS = set("〇○零一二三四五六七八九十百千两年月日—－-―、　 \t至到末初底前后春夏秋冬")
_PAREN_OPEN = "（("
_PAREN_CLOSE = "）)"


def _strip_footnote(s: str) -> str:
    """去掉标题里的脚注星号（全角＊ / 半角*）与首尾空白。"""
    return s.replace("＊", "").replace("*", "").strip()


def _is_date_line(s: str) -> bool:
    t = s.strip()
    if len(t) < 4 or t[0] not in _PAREN_OPEN or t[-1] not in _PAREN_CLOSE:
        return False
    inner = t[1:-1]
    if "年" not in inner:
        return False
    return all(ch in _DATE_CHARS for ch in inner)


def _resolve_source_and_printed() -> tuple[str, dict[int, str]]:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    items = manifest.get(BOOK) or []
    if not items:
        raise SystemExit(f"manifest.yaml 中未找到 {BOOK} 条目。")
    source_file = str(Path(items[0]["file"]).as_posix())

    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT pdf_page, printed_page FROM pages WHERE book = ? ORDER BY pdf_page",
            (BOOK,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise SystemExit(f"pages 表中没有 {BOOK} 的行，请先运行 build_textbook_index.py。")
    printed_by_pdf = {int(p): (str(pr) if pr is not None else None) for p, pr in rows}
    return source_file, printed_by_pdf


def _merge_bookmarks(toc: list[list]) -> list[dict]:
    """把碎片书签合并成「每篇一条」。返回 [{title, pdf_page}]。"""
    articles: list[dict] = []
    buf: list[str] = []
    buf_page: int | None = None
    for level, title, page in toc:
        title = str(title or "")
        page = int(page or 0)
        if _is_date_line(title):
            # 篇末：把累积的标题片段拼成一条，日期并入标题用于消歧。
            merged = _strip_footnote("".join(_strip_footnote(x) for x in buf))
            date = title.strip()
            if merged:
                articles.append({"title": f"{merged}{date}", "pdf_page": buf_page or page})
            buf, buf_page = [], None
        else:
            if buf_page is None:
                buf_page = page
            buf.append(title)
    # 收尾：若最后一篇没有日期行（理论上不会发生），也别丢。
    if buf:
        merged = _strip_footnote("".join(_strip_footnote(x) for x in buf))
        if merged and buf_page:
            articles.append({"title": merged, "pdf_page": buf_page})
    return articles


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    source_file, printed_by_pdf = _resolve_source_and_printed()
    pdf_path = (ROOT / source_file).resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF 缺失：{pdf_path}")

    with fitz.open(pdf_path) as doc:
        toc = doc.get_toc(simple=True)
        page_count = doc.page_count
    if not toc:
        raise SystemExit("PDF 无书签，无法构建目录。")

    articles = _merge_bookmarks(toc)

    # 正文首页（印刷「1」）的 pdf 页号——其前的前置页 printed_page 一律置空。
    body_start = min((p for p, pr in printed_by_pdf.items() if pr == "1"), default=18)

    payload = []
    for idx, art in enumerate(articles, start=1):
        pdf_page = art["pdf_page"]
        if pdf_page < 1 or pdf_page > page_count:
            continue
        payload.append(
            (
                BOOK,
                1,
                source_file,
                art["title"],
                pdf_page,
                printed_by_pdf.get(pdf_page),
                1,        # level：扁平单层（与邓选/正文篇目一致）
                "body",   # kind
                idx,      # sort_order
            )
        )

    conn = sqlite3.connect(str(DB_PATH))
    try:
        # 前置页印刷页码清理（幂等）：正文首页之前的页一律无印刷页码。
        conn.execute(
            "UPDATE pages SET printed_page = NULL WHERE book = ? AND pdf_page < ?",
            (BOOK, body_start),
        )
        conn.execute("DELETE FROM toc_entries WHERE book = ?", (BOOK,))
        conn.executemany(
            "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
            "printed_page, level, kind, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        conn.commit()
    finally:
        conn.close()

    update_hash()
    print(f"已写入目录 {len(payload)} 条（book={BOOK}，正文首页 pdf={body_start}）。")
    for row in payload[:3]:
        print("  示例：", row[3], "-> pdf", row[4], "印刷", row[5])
    print("  ……")
    for row in payload[-2:]:
        print("  示例：", row[3], "-> pdf", row[4], "印刷", row[5])


if __name__ == "__main__":
    main()
