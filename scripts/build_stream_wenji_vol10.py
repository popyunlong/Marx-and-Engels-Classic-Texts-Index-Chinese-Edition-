# -*- coding: utf-8 -*-
"""从 GLM-4V 转录 + 语料 TOC 生成《文集》第十卷《书信选编》流式阅读产物。

卷 1-9 由 MEAS_zh 网页源经 build_stream_wenji.py 生成；卷 10 无网页源，改由
transcribe_wenji_vol10.py 转录（干净正文）+ corpus 的 toc_entries（书信篇目）拼装，
产出与 1-9 同构的 sec-NNN.html / index.html（页锚点 <a id="s{印本页}">、h2 篇题），
故 /liushi 阅读器零改动接入；尾注由 build_wenji_endnotes.py 事后注入。

切章：TOC 的 year 条目分节（每年一节），letter 条目作节内 <h2> 篇题（按 pdf 页定位）。
正文：每 pdf 页转录文本按行成段（GLM 已合并版面折行），页首插印本页锚点。

用法：
  python scripts/build_stream_wenji_vol10.py --out stream_library/wenji-zh
"""
from __future__ import annotations

import argparse
import html as _html
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("bsw", ROOT / "scripts" / "build_stream_wenji.py")
bsw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsw)

DB = ROOT / "data" / "corpus.sqlite"
SIDECAR = ROOT / "data" / "wenji_vol10_ocr.jsonl"


def load_transcription():
    """返回 {pdf_page: (text, printed_page)}，只收成功页。"""
    pages = {}
    if not SIDECAR.exists():
        return pages
    for line in SIDECAR.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("error") or not (o.get("text") or "").strip():
            continue
        pages[int(o["pdf"])] = (o["text"], o.get("printed") or "")
    return pages


def load_toc():
    con = sqlite3.connect(str(DB))
    rows = list(con.execute(
        "select title, printed_page, pdf_page, kind, sort_order "
        "from toc_entries where book='文集' and volume='10' order by sort_order"))
    con.close()
    years, letters = [], []
    for title, pp, pdf, kind, so in rows:
        if pdf is None:
            continue
        if kind == "year":
            years.append({"title": (title or "").strip(), "pdf": int(pdf), "printed": pp})
        elif kind == "letter":
            letters.append({"title": (title or "").strip(), "pdf": int(pdf), "printed": pp})
    return years, letters


def page_to_html(text, letter_title=None, year_title=None):
    """一页转录文本 → HTML 正文体（可选篇题 h2 + 段落 p）。**不含页锚点**——锚点与
    跨页段落合并由 build() 层用 build_stream_wenji._merge_across_page_break 统一处理。

    去重：转录保留了版面里的年份行、「马克思致某某」信头行、以及长信每页重复的
    running header，而篇题已由 h1（年份）、h2（TOC 篇题）呈现——正文里再出现即视觉重复，
    故跳过。地点、日期落款是信件内容的一部分，保留。
    """
    out = []
    core = None
    if letter_title:
        out.append("<h2>%s</h2>" % _html.escape(letter_title))
        core = re.sub(r"^\d+\s*[.．]\s*", "", letter_title)   # 去「N.」
        core = re.sub(r"\s*[(（].*?[)）]\s*$", "", core).strip()  # 去「(日期)」
    for raw in text.split("\n"):
        s = raw.strip().strip("　")
        if not s:
            continue
        if s in ("书信选编", "马克思恩格斯文集") or re.fullmatch(r"\d{1,4}", s):
            continue                                          # 页眉书名 / 版心页码
        # 书信页眉 running header：长信跨页，每页上方重复印「X致Y(日期)」，GLM 逐页都转了。
        # 篇题已由 h2 呈现，正文里这些重复行会打断句子（如"…都是社会"↵页眉↵"关系…"），跳过。
        if re.match(r"^[^，。；：、]{2,10}致[^，。；：、]{2,28}[(（]\d{1,4}\s*年.{0,16}[)）]\s*$", s):
            continue
        if year_title and s == year_title:
            continue                                          # 与 h1 年份重复
        if core and s == core:
            continue                                          # 与 h2 信头重复
        out.append("<p>%s</p>" % _render_inline(s))
    return "\n".join(out)


def _render_inline(s):
    """转义 HTML 后，把转录里的 Markdown 加粗 **文字** 还原成 <b>——保留原文着重强调
    （马恩原著黑体，卷 1-9 的 MEAS 源用 <b>，卷 10 走 glm-4v-plus 转录时以 ** 标出）。"""
    s = _html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return s


def build(out_root: Path):
    trans = load_transcription()
    if not trans:
        sys.exit("卷10 转录 sidecar 为空，先跑 transcribe_wenji_vol10.py")
    years, letters = load_toc()
    letter_at = {}                                   # pdf页 → 篇题（该页起一封新信）
    for lt in letters:
        letter_at.setdefault(lt["pdf"], lt["title"])
    year_starts = {y["pdf"]: y["title"] for y in years}

    # 从正文首个 year 起（跳过卷首：书名页、编译说明、目录页——目录由 index.html 提供）
    first_year = min((y["pdf"] for y in years), default=0)
    pdfs = [p for p in sorted(trans) if p >= first_year]
    # 按 year 边界切节：每个 year 起始 pdf 开新节
    sections = []
    cur = None
    for pdf in pdfs:
        if pdf in year_starts or cur is None:
            title = year_starts.get(pdf, "书信")
            cur = {"title": title, "start_page": trans[pdf][1], "parts": []}
            sections.append(cur)
        text, printed = trans[pdf]
        yt = cur["title"] if cur["title"] != "书信" else None
        body = page_to_html(text, letter_at.get(pdf), yt)
        if not body.strip():
            continue
        anchor = '<a id="s%s" class="pgmark" aria-hidden="true"></a>' % printed
        # 跨页段落合并（复用①逻辑）：上页末段未收句 + 本页首段（非 h2 新信）→ 接回，
        # 修书信长信跨页把句子切成两段（"…都是社会"↵页锚点↵"关系…"）。
        if cur["parts"]:
            joined = bsw._merge_across_page_break(cur["parts"][-1], anchor, body)
            if joined is not None:
                cur["parts"][-1] = joined
                continue
        cur["parts"].append(anchor + "\n" + body)

    # 组织成 build_stream_wenji 的 section 结构后复用其写出
    secs = []
    for i, sec in enumerate(sections):
        body = "\n".join(sec["parts"])
        # year 节标题作 h1；卷首无
        h1 = "" if sec["title"] == "卷首" else "<h1>%s</h1>\n" % _html.escape(sec["title"])
        secs.append({"level": 1 if h1 else 0, "title": sec["title"],
                     "start_page": sec["start_page"], "html": h1 + body})

    out_vol = out_root / "10"
    bsw._write_volume(out_vol, "马克思恩格斯文集 · 第十卷", secs)
    print("卷10：%d 页 → %d 节（%d 封信）  [%s]" % (len(pdfs), len(secs), len(letters), out_vol))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="stream_library/wenji-zh")
    args = ap.parse_args()
    build(Path(args.out))


if __name__ == "__main__":
    main()
