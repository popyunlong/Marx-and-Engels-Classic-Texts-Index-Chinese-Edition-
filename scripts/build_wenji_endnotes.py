# -*- coding: utf-8 -*-
"""从《文集》PDF 语料解析卷末「注释」，注入「流式阅读」产物，使正文尾注号可悬停/点击看注文。

背景：流式源 MEAS_zh 只有正文，卷末注释整节缺失，故正文尾注号是裸 <sup>[9]</sup>、不可点。
注文本身在《文集》PDF 语料（pages 表 book='文集'）的卷末「注释」节里。

体例（人民出版社 2009 版）：注释节每条形如

    9
    约·斯·穆勒《政治经济学原理及其对社会哲学的某些应用》(两卷集)
    1848年伦敦版第1卷第1篇《生产》第1章，所加的标题就是《生产的要素》。——10。

尾部「——页码。」回指该注被引用的印本页 —— 这是本脚本的自证依据：
注 [n] 的回指页必须命中流式正文里 <sup>[n]</sup> 所在的页锚点，否则不挂链接。
这道闸同时挡掉「手稿页码假注号」——正文另有 <sup>[1323]</sup>、<sup>[I—22]</sup>
这类笔记本页码，长得与尾注号一模一样，无回指可证者一律不碰。

文本层是出版方 OCR 产物，已知系统性噪声：
    《→((    》→))/})/)}    。→0/o    ：→z    ——→一一/一-/--/→
噪声只做高置信度清理，其余原样保留并计入校对报告 —— 注文是读者可见文案，
宁可报出来人工过，不可静默改坏（沿用本项目「过 gate + 语料自证」的既有做法）。

产出结构与脚注一致，从而复用阅读器已有的悬浮预览（setupFootnoteTips）：
    正文  <sup><a id="Zpg10_E9" href="#pg10_E9">[9]</a></sup>
    节末  <aside class="endnotes"><p><a id="pg10_E9" href="#Zpg10_E9">[9]</a> 注文…</p></aside>

用法（只读语料，只写 --out 目录，不动 stream_library/wenji-zh/）：
    python scripts/build_wenji_endnotes.py --vol 8 --out stream_library/_endnote_trial
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys

DB_DEFAULT = "data/corpus.sqlite"
SRC_DEFAULT = "stream_library/wenji-zh"
TRANSCRIPT_DEFAULT = "data/wenji_notes_ocr.jsonl"  # transcribe_wenji_notes.py 产出

RUNNING_HEADERS = ("注释", "马克思恩格斯文集")

# ---------------------------------------------------------------- 语料侧：切注释节

def notes_section_range(rows):
    """定位卷末「注释」节的 pdf 页区间 [start, end)。

    rows: [(pdf_page, printed_page, raw_text)]，按 pdf_page 升序。
    起点＝正文之后第一页以「注释」开头者；终点＝其后第一页出现「人名索引」等索引节。
    """
    n = len(rows)
    start = None
    for pdf, _printed, text in rows:
        if text and text.lstrip().startswith("注释") and pdf > n * 0.6:
            start = pdf
            break
    if start is None:
        return None, None
    end = None
    for pdf, _printed, text in rows:
        if pdf <= start or not text:
            continue
        head = text[:150]
        if re.search(r"(人名索引|文献索引|引文索引|名目索引|部分名目索引)", head):
            end = pdf
            break
    return start, (end if end is not None else rows[-1][0] + 1)


def strip_running(text, printed_page):
    """去版心页眉与页码：「注释」/「马克思恩格斯文集」/本页页码（含 OCR 坏相）。

    铁律：**纯数字行一律不当页码清掉**——注号也是纯数字行（「140」与页码「620」
    同为三位纯数字，无法凭字形区分），错清一个注号会让其后整卷注条错位。
    干净页码改用「位置＋精确值」双条件清：只在页首/页尾两行内、且与本页
    printed_page 逐字相等才清。坏相页码（6ω / ω2）必含非数字字符，可全页清。
    """
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    pp = (printed_page or "").strip()

    def garbled_pagenum(s):
        if s.isdigit() or re.search(r"[一-鿿]", s) or len(s) > 4:
            return False
        digits = sum(ch.isdigit() for ch in s)
        return digits >= 1 and digits >= len(s) - 2

    out = []
    for idx, s in enumerate(lines):
        if s in RUNNING_HEADERS:
            continue
        at_edge = idx <= 1 or idx >= len(lines) - 2
        if at_edge and pp and s == pp:
            continue
        if garbled_pagenum(s):
            continue
        out.append(s)
    return out


def join_lines(lines):
    """把一条注的多行合成一段：中文直接接，拉丁词之间补空格。"""
    buf = ""
    for s in lines:
        if not buf:
            buf = s
            continue
        if re.search(r"[0-9A-Za-z]$", buf) and re.match(r"[0-9A-Za-z]", s):
            buf += " " + s
        else:
            buf += s
    return re.sub(r"\s{2,}", " ", buf).strip()


# ---------------------------------------------------------------- GLM-4V 转录侧

def load_transcription(vol, path=TRANSCRIPT_DEFAULT):
    """读 transcribe_wenji_notes.py 的 sidecar，返回该卷 {pdf_page: 转录文本}。

    sidecar 每页可能多行（失败重跑过）——只收 text 非空且无 error 的，同页取最后一条。
    无 sidecar 或该卷无成功页时返回 {}。
    """
    if not os.path.exists(path):
        return {}
    pages = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            # 有 vol 字段则按卷过滤（9卷共用 sidecar）；无 vol 字段＝卷10 专用 sidecar，全收。
            if o.get("vol") is not None and str(o.get("vol")) != str(vol):
                continue
            if o.get("error") or not (o.get("text") or "").strip():
                continue
            pages[int(o["pdf"])] = o["text"]
    return pages


def transcript_lines(text):
    """把一页 GLM 转录文本切成行，供 parse_notes 使用。

    GLM 输出段落用空行分隔、段内是长行；注号常与注文首行同行（「5 市民社会…」）。
    parse_notes 已支持「注号独占行」和「注号+注文粘连」两种，故这里只需按换行切分、
    去空行与偶发页眉残留即可，不强行还原物理行。
    """
    out = []
    for raw in text.split("\n"):
        s = raw.strip().strip("　")
        if not s or s in RUNNING_HEADERS:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------- 语料侧：解析注条

def parse_notes(lines, gap_probe=6):
    """按「注号严格自 1 递增」扫描切条，返回 ({n: 注文}, 未寻获的注号)。

    只认下一个期望注号，不认「任意独占数字行」——后者会被正文里的
    页码、年份、笔记本页码骗走。找不到期望注号时向前试探至多 gap_probe 个，
    跳过的记入 missing 交报告，不猜。

    gap_probe 是"允许向前跳过多少个漏号"。8 卷注号连续密集，默认 6 够且防误识；
    卷 9 注文长、注号在转录里偶有漏识（长注跨页），某处漏号会让 expected 卡死、
    其后所有注号落窗口外全丢（曾致末号止于 172、实际 353）——对这类卷传大 gap
    （如 50）跨过漏号。大 gap 靠"注号后必须紧跟注文"与编号单调递增双重约束防误识：
    正文年份/页码（如 1848）远超 expected+gap 会被挡。
    """
    starts = []            # [(行号, 注号)]
    missing = []
    expected = 1
    i = 0
    while i < len(lines):
        s = lines[i]
        hit = None
        for cand in range(expected, expected + gap_probe):
            token = str(cand)
            if s == token:                                   # 注号独占一行（常态）
                hit = cand
                break
            m = re.match(r"^(\d{1,3})(?=[^\d])", s)           # 注号与注文首行粘连
            if m and m.group(1) == token:
                hit = cand
                break
        if hit is not None:
            for skipped in range(expected, hit):
                missing.append(skipped)
            starts.append((i, hit, s == str(hit)))
            expected = hit + 1
        i += 1

    notes = {}
    for idx, (line_no, num, standalone) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        body = list(lines[line_no:end])
        if standalone:
            body = body[1:]
        else:
            body[0] = re.sub(r"^\d{1,3}", "", body[0], count=1).lstrip()
        notes[num] = join_lines(body)
    return notes, missing


# 回指尾：——页码。
# 破折号坏相：一一 / 一- / -- / → / ←- / 一，偶尔整个成引号（「"64、65。」）
# 句号坏相：0 / o / .
_DASH = r"[—―\-一→←_\"'’′`]"
_BACKREF_RE = re.compile(
    r"(?:" + _DASH + r"\s*){1,4}"
    r"((?:\d[\d\s]{0,4}[、,，.．]?\s*)+)"        # 页码组，可多个顿号分隔；数字内部可能裂空格
    r"[。.、0oO\s]*$"
)


def backrefs(text, max_page):
    """从注文尾部读回指页。返回候选集合（含歧义解），读不出返回 set()。

    两处坑，都由实测样本逼出来：
    ① 数字内部会裂空格 —— 注 238 尾部作「一-4 12 。」，真值 412，
       若按 \\d+ 硬抓会碎成 [4, 12] 而误判。故先按顿号切分，再抹掉段内空格。
    ② 句号常被识成 0 —— 「——11。」落成「110」。这里把「原值」与「剥掉末尾 0」
       两种解都作为候选交给闸门，由「是否命中正文注号所在页」裁决，比在此处猜更稳。
    """
    tail = text[-70:]
    m = _BACKREF_RE.search(tail)
    if not m:
        return set()
    cands = set()
    parts = re.split(r"[、,，.．]", m.group(1))
    for k, part in enumerate(parts):
        raw = re.sub(r"\s+", "", part)                        # 「4 12」→「412」
        if not raw.isdigit():
            continue
        val = int(raw)
        if 0 < val <= max_page:
            cands.add(val)
        if k == len(parts) - 1 and raw.endswith("0") and len(raw) > 1:
            stripped = int(raw[:-1])                          # 「110」→「11。」
            if 0 < stripped <= max_page:
                cands.add(stripped)
    return cands


# ---------------------------------------------------------------- 产物侧：正文注号

MARK_RE = re.compile(r"<sup>\[(\d{1,4})\]</sup>")
ANCHOR_RE = re.compile(r'<a id="s(\d+)"')


def load_markers(src_dir, vol):
    """扫流式产物，返回 [(节文件, 匹配对象位置, 注号, 所在印本页)]，按文件+位置排序。"""
    out = []
    vol_dir = os.path.join(src_dir, str(vol))
    for name in sorted(os.listdir(vol_dir)):
        if not re.fullmatch(r"sec-\d+\.html", name):
            continue
        path = os.path.join(vol_dir, name)
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        anchors = [(m.start(), int(m.group(1))) for m in ANCHOR_RE.finditer(html)]
        for m in MARK_RE.finditer(html):
            prev = [pg for pos, pg in anchors if pos < m.start()]
            out.append({
                "file": name,
                "start": m.start(),
                "end": m.end(),
                "num": int(m.group(1)),
                "page": prev[-1] if prev else None,
            })
    return out


# ---------------------------------------------------------------- 注文清理（高置信度）

def clean_note(text):
    """只做高置信度的版面/标点还原，并报出残留噪声。返回 (清后文本, 残留标记列表)。

    分寸：只改「排版层面可证的」——书名号坏相、栏宽折行留下的游离空格、
    中文句中的半角句点。凡是需要读懂文意才能断定的（杜会→社会、温夫→渔夫、
    IlP→即、希腊文乱码），一律不碰，只计入 residue 交人工/视觉模型复核。
    注文是读者可见文案，静默改坏比留着噪声更糟。
    """
    s = text
    s = re.sub(r"\(\s*\(", "《", s)                # ((导言》 → 《导言》
    s = re.sub(r"\)\s*\)", "》", s)                # 著作)) → 著作》
    s = re.sub(r"\}\s*\)", "》", s)                # 著作}) → 著作》
    s = re.sub(r"\)\s*\}", "》", s)                # 著作)} → 著作》
    s = re.sub(r"\{\s*\(", "《", s)

    # 栏宽折行留下的游离空格：数字↔汉字之间、数字内部（「第1 卷」「1 833年」「4 12」）
    s = re.sub(r"(?<=\d)\s+(?=[一-鿿])", "", s)
    s = re.sub(r"(?<=[一-鿿])\s+(?=\d)", "", s)
    s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)
    # 汉字↔汉字之间的空格：GLM 转录把原书换行处留成空格（「政治经 济学」「总的 导言」）。
    # 中文排版汉字间本无空格，去除安全；中英混排的字母/数字边界已由上面几条处理。
    s = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", s)
    # 中文句中的半角句点 → 句号（「未完成的手稿.马克思在」）
    s = re.sub(r"(?<=[一-鿿])\.(?=[一-鿿])", "。", s)
    # 汉字之间的半角逗号 → 全角（GLM 在含书名/年份的行爱用半角「第1章,所加」）。
    # 只在汉字↔汉字/句号间转，避开「1,000」千分位与「Mill, 1849」外文逗号。
    s = re.sub(r"(?<=[一-鿿]),(?=[一-鿿])", "，", s)
    # 汉字后的半角冒号/分号 → 全角（「写道:」「历史传统;反对」）。
    # 汉字前导限定，避开时间「12:30」等数字场景。
    s = re.sub(r"(?<=[一-鿿]):", "：", s)
    s = re.sub(r"(?<=[一-鿿]);(?=[一-鿿])", "；", s)

    s = re.sub(r"\s+([，。、；：）」』》])", r"\1", s)
    s = re.sub(r"([（「『《])\s+", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s).strip()

    residue = []
    if re.search(r"[（(]\s*[（(]", s):
        residue.append("疑似未还原书名号")
    if re.search(r"[ωÂ]|[a-zA-Z]{1,3}\d[a-zA-Z]", s):
        residue.append("疑似字符级 OCR 乱码")
    if re.search(r"[一-鿿]z", s):
        residue.append("疑似冒号被识成 z")
    if re.search(r"《[^》]{1,40}[队儿川丿](?=[^》]{0,20}$|[，。、和])", s):
        residue.append("疑似书名号尾被识成汉字")
    if s.count("《") != s.count("》"):
        residue.append("书名号不配对")
    if re.search(r"\.\.", s):
        residue.append("疑似引号被识成点")
    return s, residue


def strip_backref_tail(text, max_page):
    """去掉注文末尾的「——页码。」回指（它是编辑体例，读者浮层里不需要）。

    只剥空白，**不可 rstrip 句号**：回指前面那个「。」是注文本身的句末标点
    （「…第11章结束语。——10。」剥过头会变成「…第11章结束语」）。
    """
    m = _BACKREF_RE.search(text[-70:])
    if not m:
        return text
    cut = len(text) - 70 + m.start() if len(text) > 70 else m.start()
    return text[:max(0, cut)].rstrip("　 \t")


# ---------------------------------------------------------------- 注入

def inject(html, marks, notes_for_page, vol):
    """把命中闸门的注号改写成锚链接，并在节末补 <aside>。marks 须按 start 升序。"""
    pieces, cursor, used = [], 0, []
    for mk in marks:
        if not mk.get("ok"):
            continue
        anchor = "pg%d_E%d" % (mk["page"], mk["num"])
        if mk.get("dup"):
            anchor += "_%d" % mk["dup"]
        pieces.append(html[cursor:mk["start"]])
        pieces.append(
            '<sup><a id="Z%s" href="#%s">[%d]</a></sup>' % (anchor, anchor, mk["num"])
        )
        cursor = mk["end"]
        used.append((anchor, mk["num"]))
    pieces.append(html[cursor:])
    out = "".join(pieces)
    if not used:
        return out, 0

    rows = []
    for anchor, num in used:
        body = notes_for_page.get(num, "")
        rows.append(
            '<p><a id="%s" href="#Z%s">[%d]</a>%s</p>' % (anchor, anchor, num, body)
        )
    # 样式由阅读器的 injectReadingStyle() 统一管（aside.endnotes / aside.fn-notes），
    # 这里只产结构——注文块的排版与页末脚注块保持一处真源，勿在构建期另塞 <style>。
    aside = '<aside class="endnotes"><h3>注释</h3>' + "".join(rows) + "</aside>"
    if "</body>" in out:
        out = out.replace("</body>", aside + "\n</body>", 1)
    else:
        out += aside
    return out, len(used)


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol", default="8")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--src", default=SRC_DEFAULT)
    ap.add_argument("--out", default="stream_library/_endnote_trial")
    ap.add_argument("--report", default=None)
    ap.add_argument("--transcript", default=TRANSCRIPT_DEFAULT)
    ap.add_argument("--source", choices=["auto", "transcription", "corpus"], default="auto",
                    help="注文来源：auto=逐页转录优先回退语料；transcription=仅转录页；corpus=仅语料")
    ap.add_argument("--gap-probe", type=int, default=6,
                    help="注释解析允许跳过的漏号数。8卷默认6；卷9注号跳号大传 50")
    ap.add_argument("--gate", choices=["strict", "trust"], default="strict",
                    help="挂链闸门：strict=注号所在页须∈注文回指页（8卷防范围内假注号）；"
                         "trust=注号≤注释末号且注释存在即挂（卷9，支持同一尾注多处引用）")
    ap.add_argument("--fill-gaps", action="store_true",
                    help="转录漏识的注号从语料 OCR 补（卷9长注文致注号丢失，转录/语料漏号互补）")
    args = ap.parse_args()

    vol = str(args.vol)
    conn = sqlite3.connect(args.db)
    rows = list(conn.execute(
        "select pdf_page, printed_page, raw_text from pages "
        "where book='文集' and volume=? order by pdf_page", (vol,)))
    if not rows:
        sys.exit("语料里没有《文集》卷%s" % vol)

    start, end = notes_section_range(rows)
    if start is None:
        sys.exit("卷%s 未检出「注释」节" % vol)

    # 注文文本源：GLM-4V 转录（去 OCR 噪声）优先，**逐页**回退语料 raw_text。
    # 逐页混合而非整卷 all-or-nothing——个别 HTTP 400 失败页只损失本页，不牵累整卷。
    section_pdfs = [pdf for pdf, _p, _t in rows if start <= pdf < end]
    trans = load_transcription(vol, args.transcript) if args.source != "corpus" else {}
    trans_in = {p: t for p, t in trans.items() if start <= p < end}
    coverage = len(trans_in) / len(section_pdfs) if section_pdfs else 0.0

    lines = []
    n_trans = n_corpus = 0
    for pdf, printed, text in rows:
        if not (start <= pdf < end):
            continue
        if args.source != "corpus" and pdf in trans_in:
            lines.extend(transcript_lines(trans_in[pdf]))
            n_trans += 1
        elif args.source == "transcription":
            continue  # 强制转录模式：失败页直接跳过，不混语料
        else:
            lines.extend(strip_running(text, printed))
            n_corpus += 1
    if n_corpus == 0 and n_trans:
        source_label = "glm-transcription"
    elif n_trans == 0:
        source_label = "corpus-ocr"
    else:
        source_label = "mixed(%d转录/%d语料)" % (n_trans, n_corpus)
    notes_raw, missing = parse_notes(lines, args.gap_probe)

    # 注号级补缺：转录长注文会把其后注号「吞掉」（注文在、注号数字丢），
    # 语料 OCR 与转录漏号互补。仅补转录完全缺失的注号，转录已有的不覆盖（转录质量高）。
    n_filled = 0
    if args.fill_gaps:
        corpus_lines = []
        for pdf, printed, text in rows:
            if start <= pdf < end:
                corpus_lines.extend(strip_running(text, printed))
        notes_corpus, _ = parse_notes(corpus_lines, args.gap_probe)
        for num, txt in notes_corpus.items():
            if num not in notes_raw:
                notes_raw[num] = txt
                n_filled += 1

    marks = load_markers(args.src, vol)
    body_pages = [m["page"] for m in marks if m["page"]]
    max_page = max(body_pages) if body_pages else 9999

    # 逐条：清理 + 回指
    notes, residues = {}, {}
    refs = {}
    for num, raw in notes_raw.items():
        refs[num] = backrefs(raw, max_page)
        body = strip_backref_tail(raw, max_page)
        cleaned, res = clean_note(body)
        notes[num] = cleaned
        if res:
            residues[num] = res

    # 闸门：strict=注号所在页∈回指页（8卷防落在注号范围内的手稿页码假注号）；
    #      trust=注号≤注释末号且注释存在即挂（卷9：源[n]标记 99.7% 是真尾注，
    #            同一尾注多处引用属常态，不能强求每处引用页都等于回指页）。
    last_num = max(notes) if notes else 0
    per_page_seen = {}
    stats = {"pass": 0, "page_mismatch": 0, "no_note": 0, "no_backref": 0, "too_big": 0}
    for mk in marks:
        num, page = mk["num"], mk["page"]
        if args.gate == "trust":
            if num > last_num + 5:                           # 远超末号 = 手稿页码/公式，挡
                mk["ok"] = False; mk["why"] = "too_big"; stats["too_big"] += 1; continue
            if num not in notes:                             # 注释解析漏了该号
                mk["ok"] = False; mk["why"] = "no_note"; stats["no_note"] += 1; continue
            if page is None:
                mk["ok"] = False; mk["why"] = "no_backref"; stats["no_backref"] += 1; continue
            mk["ok"] = True; mk["why"] = "pass"; stats["pass"] += 1
        else:
            if num not in notes:
                mk["ok"] = False; mk["why"] = "no_note"; stats["no_note"] += 1; continue
            if not refs.get(num):
                mk["ok"] = False; mk["why"] = "no_backref"; stats["no_backref"] += 1; continue
            # ±1 页容差：跨页断段合并把页锚点挪到断点，注号可能落到相邻页锚点后，
            # 使"注号所在页"较回指页偏 1。容差吸收这个偏移；差很多的手稿页码假注号仍被挡。
            if page is None or not any(abs(page - r) <= 1 for r in refs[num]):
                mk["ok"] = False; mk["why"] = "page_mismatch"; stats["page_mismatch"] += 1; continue
            mk["ok"] = True; mk["why"] = "pass"; stats["pass"] += 1
        key = (mk["file"], page, num)                      # 同页同注号出现多次 → id 加序号
        per_page_seen[key] = per_page_seen.get(key, 0) + 1
        if per_page_seen[key] > 1:
            mk["dup"] = per_page_seen[key]

    # 写产物
    out_dir = os.path.join(args.out, vol)
    os.makedirs(out_dir, exist_ok=True)
    by_file = {}
    for mk in marks:
        by_file.setdefault(mk["file"], []).append(mk)

    src_dir = os.path.join(args.src, vol)
    injected_total = 0
    for name in sorted(os.listdir(src_dir)):
        src_path = os.path.join(src_dir, name)
        dst_path = os.path.join(out_dir, name)
        if not name.endswith(".html"):
            continue
        with open(src_path, encoding="utf-8") as fh:
            html = fh.read()
        mks = sorted(by_file.get(name, []), key=lambda m: m["start"])
        if mks:
            html, k = inject(html, mks, notes, vol)
            injected_total += k
        tmp = dst_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(tmp, dst_path)

    total_notes = max(notes) if notes else 0
    report = {
        "vol": vol,
        "note_source": source_label,
        "transcript_coverage": round(coverage, 3),
        "notes_section_pdf": [start, end - 1],
        "notes_parsed": len(notes),
        "notes_last_num": total_notes,
        "notes_missing": missing,
        "backref_readable": sum(1 for v in refs.values() if v),
        "markers_total": len(marks),
        "markers_linked": stats["pass"],
        "gate": stats,
        "residue_count": len(residues),
        "residue_sample": {str(k): v for k, v in list(sorted(residues.items()))[:20]},
        "rejected_sample": [
            {"num": m["num"], "page": m["page"], "why": m["why"],
             "backref": sorted(refs.get(m["num"], []))[:4]}
            for m in marks if not m.get("ok")
        ][:30],
    }
    rp = args.report or os.path.join(args.out, "report_vol%s.json" % vol)
    with open(rp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    pct = 100.0 * stats["pass"] / len(marks) if marks else 0
    print("卷%s 注释节 pdf %d-%d" % (vol, start, end - 1))
    print("  注文来源        : %s (转录覆盖 %.0f%%)" % (source_label, coverage * 100))
    print("  解析注条        : %d 条 (末号 %d, 未寻获 %d)" % (len(notes), total_notes, len(missing)))
    print("  可读回指页      : %d 条" % report["backref_readable"])
    print("  正文注号        : %d 处" % len(marks))
    print("  过闸挂链        : %d 处 (%.0f%%)" % (stats["pass"], pct))
    print("  被挡            : 无此注 %d / 无回指 %d / 页码不合 %d"
          % (stats["no_note"], stats["no_backref"], stats["page_mismatch"]))
    print("  注文残留噪声    : %d 条" % len(residues))
    print("  产物            : %s" % out_dir)
    print("  报告            : %s" % rp)


if __name__ == "__main__":
    main()
