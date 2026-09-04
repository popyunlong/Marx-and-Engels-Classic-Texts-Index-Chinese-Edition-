# -*- coding: utf-8 -*-
"""为 2026-08-02 新增的 12 个书库从**印刷目录页**解析目录（toc_entries）。

这批书没有一本带可用的 PDF 书签（《作风建设摘编》有 18 条，但每条标题被排版折成
两行、书签自身也只到专题级，还不如印刷目录准），故一律回到印刷「目录」页解析。
三种版式，各一个 parser：

  topic —— 论述摘编 / 论述选编 / 学习纲要（8 种单卷本）
      「一、专题名 ……………（1）」：专题级一层目录，页码或在行尾、或单独成行。
  ziben —— 《资本论》三卷
      多级：第X册/第X篇（居中无页码）→ 第X章 …… 47-102 → 1. 节 …… 47。
      更深的 A./（1）/（a）层不收（对「篇章直达」只是噪声）。
  essay —— 《习近平著作选读》《习近平党建文选》
      「篇名 …… 5-7」+ 下一行「（二〇一二年十一月十五日）」：篇章级目录，
      日期并入标题消歧（同名篇目在不同年份出现时可区分）。

印刷页码 → pdf 页：优先用 pages 表里「众数偏移」直接换算（对整卷恒定偏移的扫描件
最稳），换算不到或落点不单调时回退到 ①pages 表的 printed→pdf 实测映射、
②在正文里按页首标题搜索定位。三条都不成的条目丢弃并计数报告——宁可少一条，
也不要把读者送到错的页。

只 DELETE/INSERT 本 book(+volume) 的 toc_entries；完成重算 sha256，其它书库不动。
须先跑 build_scan_volumes.py / build_textbook_index.py（pages 表要先有）。

用法：
  python scripts/build_newbooks12_toc.py --dry-run              # 全部 12 个书库试跑
  python scripts/build_newbooks12_toc.py --book 资本论
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

from build_index import DB_PATH  # noqa: E402

HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

# 书库 → 版式。顺序即默认处理顺序。
BOOKS: dict[str, str] = {
    "资本论": "ziben",
    "习近平著作选读": "essay",
    "习近平党建文选": "essay",
    "习近平外交思想学习纲要": "topic",
    "习近平关于全面深化改革论述摘编": "topic",
    "习近平关于全面依法治国论述摘编": "topic",
    "习近平关于科技创新论述摘编": "topic",
    "习近平关于全面从严治党论述摘编": "topic",
    "习近平关于社会主义生态文明建设论述摘编": "topic",
    "习近平关于力戒形式主义官僚主义重要论述选编": "topic",
    "习近平关于加强党的作风建设论述摘编": "topic",
    "论坚持党对一切工作的领导": "essay",
    "论党的宣传思想工作": "essay",
    "论中国共产党历史": "essay",
    "论把握新发展阶段、贯彻新发展理念、构建新发展格局": "essay",
    "论党的自我革命": "essay",
    "习近平关于党的群众路线教育实践活动论述摘编": "topic",
    "习近平关于总体国家安全观论述摘编": "topic",
    "习近平关于网络强国论述摘编": "topic",
    "习近平关于社会主义精神文明建设论述摘编": "topic",
    "习近平关于树立和践行正确政绩观论述摘编": "topic",
}

_FULL2HALF = str.maketrans("０１２３４５６７８９", "0123456789")
_LEADER = r"[·．.。…⋯⋅•\-—–_\s]"
# 行尾页码：可裸写（…… 47）、带括号（……（1））、或页码区间（…… 47-102 / 47—102）。
# 取区间的**起页**——目录里的区间是「起—止」，直达当然要落在起页。
_TAIL_PAGE = re.compile(
    rf"{_LEADER}*[（(]?\s*([0-9０-９]{{1,4}})\s*(?:[-—–~至]\s*[0-9０-９]{{1,4}}\s*)?[)）]?\s*$"
)
# 整行只有一个带括号的页码（《力戒形式主义》《生态文明》两种的目录把页码单独排一行）
_ALONE_PAGE = re.compile(r"^[（(]\s*([0-9０-９]{1,4})\s*[)）]$")
# 整行只有点引线/装饰
_ONLY_LEADER = re.compile(rf"^{_LEADER}+$")
# 整行只有一个裸数字 = 版心 folio，不是目录条目
_ONLY_DIGITS = re.compile(r"^[0-9０-９]{1,4}$")
# 日期括注独行（目录里跟在篇名后的那一行）
_DATE_ONLY = re.compile(
    r"^[（(][一二三四五六七八九十百零〇○Oo\d]{2,}\s*年[^）)]{0,24}[)）]$"
)
# 篇名后的日期括注（essay 版式用它切标题与日期）
_DATE_IN = re.compile(
    r"[（(]\s*[一二三四五六七八九十百零〇○Oo\d]{2,}\s*年[^）)]{0,24}?[)）]"
)


def nkey(s: str) -> str:
    return re.sub(r"\s", "", str(s or ""))


def to_int(s: str) -> int:
    return int(str(s).translate(_FULL2HALF))


# ---------------------------------------------------------------- 页码映射
class PageMapper:
    """印刷页码 → pdf 页。众数偏移优先，实测映射与正文搜索兜底。"""

    def __init__(self, rows: list[tuple[int, str | None, str]]):
        # rows: [(pdf_page, printed_page, raw_text)]，按 pdf_page 升序
        self.rows = rows
        self.max_pdf = rows[-1][0] if rows else 0
        known = [(p, int(pr)) for p, pr, _ in rows if pr and str(pr).isdigit()]

        # ---- 正文段切分 ----
        # 「一卷一个恒定偏移」在这批书上不成立：前置页（出版说明/目录）自带一套从 1 起的
        # 编号，与正文重号；而扫描件本身还会漏页（《党建文选》卷二缺印刷 157、158 页，
        # 偏移在 pdf169 处由 10 跳到 8）。只取众数偏移外推，会把「印第1页」解到目录页、
        # 把后半卷整体错 2 页。故先按「偏移恒定」切成若干连续段，再挑出正文段。
        segs: list[tuple[int, int, int, int]] = []   # (off, 起printed, 止printed, 起pdf)
        for pdf_page, v in known:
            off = pdf_page - v
            if segs and segs[-1][0] == off and v > segs[-1][2]:
                o, lo, _hi, sp = segs[-1]
                segs[-1] = (o, lo, v, sp)
            else:
                segs.append((off, v, v, pdf_page))
        segs = [s for s in segs if s[2] - s[1] >= 4]        # 太短的段是噪声
        self.all_segs = list(segs)
        # 从后往前保留：与后面（更靠正文）的段印刷页码区间重叠的，就是前置页那套编号，
        # 正文优先。但前置段并不丢弃（存在 all_segs 里）——《资本论》的「第X卷说明」
        # 用的正是那套独立编号（说明 1—5，正文序言又从 3 起），只认正文段会把这一条
        # 解到十几页之后的分册扉页上。歧义交给 resolve() 用「落点正文是否真含该标题」裁决。
        body_segs: list[tuple[int, int, int, int]] = []
        for seg in reversed(segs):
            if any(not (seg[2] < k[1] or seg[1] > k[2]) for k in body_segs):
                continue
            body_segs.append(seg)
        body_segs.reverse()
        self.body_segs = body_segs

        self.printed_to_pdf: dict[int, int] = {}
        body_pdf: set[int] = set()
        for off, lo, hi, _sp in body_segs:
            for v in range(lo, hi + 1):
                self.printed_to_pdf.setdefault(v, v + off)
                body_pdf.add(v + off)
        for pdf_page, v in known:                            # 段外的实测点兜底
            self.printed_to_pdf.setdefault(v, pdf_page)
        self.body_pdf = body_pdf

        offsets = Counter(p - v for p, v in known)
        self.offset: int | None = None
        if offsets:
            best, cnt = offsets.most_common(1)[0]
            # 恒定偏移要有压倒性支持才敢用来外推；否则只信实测映射
            if cnt >= max(10, 0.5 * sum(offsets.values())):
                self.offset = best

    def by_offset(self, printed: int) -> int | None:
        """外推：先看落在哪个正文段里，段外才退回全卷众数偏移。"""
        for off, lo, hi, _sp in self.body_segs:
            if lo <= printed <= hi:
                p = printed + off
                return p if 1 <= p <= self.max_pdf else None
        if self.offset is None:
            return None
        p = printed + self.offset
        return p if 1 <= p <= self.max_pdf else None

    def locate(self, title: str, lo_pdf: int = 1, body_only: bool = False) -> tuple[int, int] | None:
        """在正文里按页首标题定位（页码映射失败时的最后手段）。"""
        key = nkey(re.sub(r"^[0-9０-９一二三四五六七八九十]{0,4}[.．、,，]?", "", title))[:12]
        if len(key) < 5:
            return None
        for pdf_page, printed, raw in self.rows:
            if pdf_page < lo_pdf:
                continue
            if body_only and self.body_pdf and pdf_page not in self.body_pdf:
                continue
            if key in nkey(raw[:90]):
                return pdf_page, (int(printed) if printed and str(printed).isdigit() else 0)
        return None

    def _head_matches(self, pdf_page: int, title: str, body_only: bool = False) -> bool:
        """落点那一页的开头是否真的就是这条标题（用来在重号候选之间裁决）。"""
        key = nkey(re.sub(r"^[0-9０-９一二三四五六七八九十]{0,4}[.．、,，]?", "", title))[:12]
        if len(key) < 5:
            return False
        if body_only and self.body_pdf and pdf_page not in self.body_pdf:
            return False
        row = next((r for r in self.rows if r[0] == pdf_page), None)
        return bool(row) and key in nkey(row[2][:90])

    def resolve(self, printed: int, title: str, last_pdf: int,
                body_only: bool = False) -> tuple[int, int] | None:
        """返回 (pdf_page, printed)；解析不到返回 None。

        允许与上一条同页：「第一章」和它的「1. 第一节」、「一、专题」和它的「1. 条目」
        本来就印在同一页上，要求严格递增会把每个专题的第一个条目全丢掉。
        """
        # 同一个印刷页码可能落在多段（前置页与正文各有一套从 1 起的编号）。先按
        # 「正文段优先」排出候选，再看哪一个的落点开头真的是这条标题——《资本论》的
        # 「第二卷说明」印的是说明自己那套第 1 页，只认正文段会解到十几页后的分册扉页。
        cands: list[int] = []
        c0 = self.by_offset(printed)
        if c0 is not None:
            cands.append(c0)
        for off, lo, hi, _sp in self.all_segs:
            if lo <= printed <= hi:
                p = printed + off
                if 1 <= p <= self.max_pdf and p not in cands:
                    cands.append(p)
        alt = self.printed_to_pdf.get(printed)
        if alt is not None and alt not in cands:
            cands.append(alt)
        cands = [p for p in cands if p >= last_pdf]
        if body_only and self.body_pdf:
            cands = [p for p in cands if p in self.body_pdf]

        verified = [p for p in cands if self._head_matches(p, title, body_only)]
        if verified:
            return verified[0], printed
        if cands:
            # 没有一个候选的落点开头是这条标题时，再拿标题去正文里找一次。
            # 《资本论》的「第X卷说明」就属于这一类：它印的是说明自己那套页码，而
            # build_scan_volumes 对全卷用了正文的恒定偏移，说明那几页的 printed_page
            # 干脆是空的，靠页码根本推不出来，只能按标题定位（否则落到十几页后的分册扉页）。
            # 结果限制在候选附近的窗口内，避免一条对不上的标题把读者甩到卷子另一头。
            got = self.locate(title, max(1, last_pdf), body_only)
            if (got and last_pdf <= got[0] <= cands[0] + 20
                    and (not body_only or not got[1] or got[1] == printed)):
                return got[0], printed
            return cands[0], printed
        got = self.locate(title, max(1, last_pdf), body_only)
        return got if got else None


# ---------------------------------------------------------------- 目录页定位
def toc_hits(text: str) -> int:
    """本页上「条目页码标记」的个数。

    按**标记数**而不是**行数**计：OCR 时不时把整页目录拼成一整行（《党建文选》卷二
    pdf7 就是十来条首尾相接的一行），按行数判定会认为这页只有 1 条而提前收尾，
    卷二 56 篇因此只认到 23 篇。
    """
    n = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _ONLY_DIGITS.match(nkey(line)):
            continue
        c = len(_ESSAY_PAGE.findall(line))       # 点引线 + 页码（篇章/多级目录）
        if c:
            n += c
        elif _ALONE_PAGE.match(nkey(line)) or (_TAIL_PAGE.search(line) and nkey(line)):
            n += 1
    return n


def find_toc_pages(text_by_pdf: dict[int, str], limit: int = 60) -> list[int]:
    """从含「目录」标题的前置页起，取连续目录页（含条目标记的页）。

    「连续 2 页无条目才收尾」是《陈独秀文集》踩出来的教训：单页 OCR 烂掉
    （或本就是空白隔页）会把后半个目录整段腰斩。
    """
    start = None
    for p in sorted(text_by_pdf):
        if p > limit:
            break
        if any(nkey(l) == "目录" for l in text_by_pdf[p].splitlines()):
            start = p
            break
    if start is None:
        for p in sorted(text_by_pdf):
            if p > limit:
                break
            if toc_hits(text_by_pdf[p]) >= 3:
                start = p
                break
    if start is None:
        return []
    # GLM 偶尔会漏掉目录首页的「目录」二字，而后续页又完整识别出该页眉。找到明确页眉后，
    # 只要前一页仍有至少两条目录页码标记，就向前并入；避免《论党的自我革命》从 pdf8
    # 才开始解析、静默丢掉 pdf6-7 的 17 篇。
    while start > 1 and toc_hits(text_by_pdf.get(start - 1, "")) >= 2:
        start -= 1
    pages = [start]
    blanks = 0
    p = start + 1
    while p in text_by_pdf and p <= limit + 30:
        text = text_by_pdf[p]
        n = toc_hits(text)
        # 把正文页挡在外面，同时别误伤条目又长又多的目录页。两个判据取或：
        #   ① 点引线成排 —— 目录页每条一串「……」，正文页几乎不出现；
        #   ② 字数密度 —— 用于《生态文明》《力戒形式主义》那种页码单排一行、根本没有
        #      点引线的目录。长度要先把点引线剥掉再算，否则《著作选读》卷二那种一条
        #      四十个点的目录页会被自己的引线撑爆密度判据而被当成正文页（实测卷二
        #      75 篇因此只认到 61 篇）。
        core = _DOTS.sub("", nkey(text))
        leaders = len(_DOTS.findall(text))
        dense = n >= 2 and (leaders >= 2 or n * 120 >= len(core))
        if dense:
            pages.extend(range(pages[-1] + 1, p + 1))  # 把中间跳过的空白页一并纳入
            blanks = 0
        else:
            blanks += 1
            if blanks >= 2:
                break
        p += 1
    return pages


def running_heads(text_by_pdf: dict[int, str], pages: list[int], book: str) -> set[str]:
    """目录页上重复出现的页眉（书名/「目录」），解析时要跳过，否则会被拼进篇名。"""
    cnt: Counter[str] = Counter()
    for p in pages:
        for line in text_by_pdf.get(p, "").splitlines():
            if _DATE_ONLY.match(nkey(line.strip())):
                continue
            k = re.sub(r"[\s\d０-９]+", "", line)
            # 只有「含 ≥2 个汉字」的行才可能是页眉。少了这一条，页码独行「（１）」
            # 去掉数字后剩下的「（）」会在一页里重复十来次而被当成页眉，
            # 于是整页目录条目全被当噪声丢掉（实测《生态文明》《力戒形式主义》解析出 0 条）。
            if 2 <= len(k) <= 30 and len(re.findall(r"[一-鿿]", k)) >= 2:
                cnt[k] += 1
    heads = {k for k, n in cnt.items() if n >= 2}
    stem = re.sub(r"[（(].*?[)）]", "", book)
    for k in list(cnt):
        if len(k) >= 6 and (k in nkey(stem) or nkey(stem) in k):
            heads.add(k)
    heads.add("目录")
    return heads


def is_noise(line: str, heads: set[str]) -> bool:
    k = re.sub(r"[\s\d０-９]+", "", line)
    if not nkey(line):
        return True
    if _ONLY_LEADER.match(line) or _ONLY_DIGITS.match(nkey(line)):
        return True
    # 「目　　录」常被排成「目」「录」两行（竖排标题），单字行照样是标题不是条目
    if nkey(line) in ("目", "录", "目录"):
        return True
    return bool(k) and k in heads


# 目录点引线（连续 2 个以上的点/间隔号）。**不能**把破折号和空格算进来一起剥：
# 学习纲要式条目「一、…—关于新时代中国外交的根本保证」的「——」是标题的一部分，
# 序号「2. 中国特色…」里的「. 」也是；早先按 _LEADER{2,} 一刀切，把它们全吃掉了。
_DOTS = re.compile(r"[·．.。…⋯⋅•_]{2,}")


def clean_title(parts: list[str], tail: str = "") -> str:
    body = "".join(p.strip() for p in parts) + tail
    body = _DOTS.sub("", body)
    body = re.sub(r"\s+", "", body)
    return body.strip("　 ·．.。…⋯–_")


# 解析时为了拼接折行标题把所有空白都去掉了，直接入库会得到「第一章商品」这种挤成一团的
# 条目。写库前按序号前缀补回一个空格，读起来才像原书目录（「第一章 商品」「1. 价值尺度」）。
# 只给「第X章」「1.」这类**后面直接顶着标题**的序号补空格。「一、」式序号自带顿号，
# 现有书库（《新时代思想学习纲要》等）也一律写成「十七、推动构建…」不带空格，跟着来。
_RESPACE = re.compile(r"^(第[一二三四五六七八九十百零〇]{1,4}[册篇章节]|[0-9]{1,2}[.．])")
_TOC_BAD_DATE_DASH = re.compile(
    r"(?<=月)一{1,2}(?=二〇[一二三四五六七八九十百零〇○]{2,4}年)"
)
_TOC_OCR_FIXES = {
    "光照干秋": "光照千秋",
    "深人推进": "深入推进",
    "切人口和动员令": "切入口和动员令",
    "自我革命弓领": "自我革命引领",
    "二〇三年六月": "二〇一三年六月",
}


def respace_title(title: str) -> str:
    for bad, good in _TOC_OCR_FIXES.items():
        title = title.replace(bad, good)
    title = _TOC_BAD_DATE_DASH.sub("——", title)
    m = _RESPACE.match(title)
    if not m or len(title) <= m.end():
        return title
    return f"{title[:m.end()]} {title[m.end():]}"


# ---------------------------------------------------------------- topic 版式
_TOPIC_L1 = re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、,，]")
_TOPIC_L2 = re.compile(r"^[0-9０-９]{1,2}\s*[.．、]")


def topic_level(title: str) -> int:
    """专题级=1；纲要式里「1. 条目」这一层=2；无前缀（篇首综述/开卷篇）=1。"""
    t = title.strip()
    if _TOPIC_L2.match(t):
        return 2
    return 1


def parse_topic(text_by_pdf: dict[int, str], pages: list[int], heads: set[str]):
    """返回 [(printed:int, level:int, title:str)]。专题级目录（纲要式带一层条目）。"""
    out: list[tuple[int, int, str]] = []
    acc: list[str] = []
    for p in pages:
        for raw in text_by_pdf.get(p, "").splitlines():
            line = raw.strip()
            if is_noise(line, heads):
                continue
            if _DATE_ONLY.match(nkey(line)):
                # 日期括注属于**上一条**（《力戒形式主义》开卷篇那样），不要拼进下一条
                continue
            m_alone = _ALONE_PAGE.match(nkey(line))
            if m_alone:
                title = clean_title(acc)
                acc = []
                if 2 <= len(title) <= 90:
                    out.append((to_int(m_alone.group(1)), topic_level(title), title))
                continue
            m = _TAIL_PAGE.search(line)
            if m and nkey(line[: m.start()]):
                title = clean_title(acc, line[: m.start()])
                acc = []
                if 2 <= len(title) <= 90:
                    out.append((to_int(m.group(1)), topic_level(title), title))
                continue
            acc.append(line)
    return out


# ---------------------------------------------------------------- ziben 版式
_ZB_CE = re.compile(r"^第[一二三四五六七八九十]{1,3}册")
_ZB_PIAN = re.compile(r"^第[一二三四五六七八九十]{1,3}篇")
_ZB_ZHANG = re.compile(r"^第[一二三四五六七八九十百]{1,4}章")
_ZB_SEC = re.compile(r"^[0-9０-９]{1,2}\s*[.．、]")
# 更深的层级（A. / （1） / （a） / I. II.）不收：对篇章直达只是噪声
_ZB_DEEP = re.compile(r"^([A-Za-z]\s*[.．、]|[（(]\s*[0-9０-９a-zA-Z]{1,3}\s*[)）]|[IVX]{1,5}\s*[.．、])")
_ZB_HEAD_PREFIX = re.compile(r"^第[一二三四五六七八九十]{1,3}[册篇]")
# 卷末的插图目录：条目页码指的是图版位置，收进来只会把读者送错页
_ZB_STOP = re.compile(r"^(插图|插\s*图)$")


# 「第X章」「N.」这类结构标记出现在标题**中间**，说明前面粘进了不带页码的邻行
# （第三卷目录里的下册分册页、以及编者加的方括号小标题就是这样）。截到最后一个标记处，
# 否则会产出「资本主义生产的总过程（下）第五篇…（续）第二十九章银行资本的组成部分」
# 这种把三条揉成一条的目录项，真正的「第二十九章」条目则整条消失。
_ZB_INNER = re.compile(r"(第[一二三四五六七八九十百]{1,5}[章篇]|(?<![0-9])[0-9]{1,2}[.．])")


def _zb_trim(title: str) -> str:
    last = None
    for mm in _ZB_INNER.finditer(title):
        if mm.start() > 0:
            last = mm
    if last is not None and len(title) - last.start() >= 6:
        return title[last.start():]
    return title


_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ZB_CHAP_NO = re.compile(r"^第([一二三四五六七八九十百零〇]{1,5})章")


def _cn_int(s: str) -> int | None:
    """「五十一」→51。只认目录里会出现的十/百进位写法。"""
    section = num = 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        else:
            return None
    return (section + num) or None


def _int_cn(n: int) -> str:
    """51 → 「五十一」。与 _cn_int 互逆，只覆盖 1—199（章序号足够）。"""
    d = "〇一二三四五六七八九"
    if n < 10:
        return d[n]
    if n < 20:
        return "十" + (d[n % 10] if n % 10 else "")
    if n < 100:
        return d[n // 10] + "十" + (d[n % 10] if n % 10 else "")
    return d[n // 100] + "百" + (_int_cn(n % 100) if n % 100 else "")


def repair_chapter_numbers(entries: list[tuple[int, int, str]]) -> tuple[list[tuple[int, int, str]], int]:
    """修 OCR 把章序号**丢掉前缀**造成的错号（「第五十章」读成「第十章」）。

    《资本论》第三卷目录页实测就出了这一处：第四十九章、**第十章**、第五十一章。
    与 [[斯大林全集]] 的「一二三→二三」是同一个 OCR 失败模式。只在
    「前一章号 +1 == 应有章号」且「读到的汉字数字是应有数字的**后缀**」时改写，
    别的情形一律不动——宁可留着一个怪条目，也不许自作主张改书。
    """
    fixed = 0
    out = list(entries)
    prev_no = None
    for k, (pr, lv, title) in enumerate(out):
        m = _ZB_CHAP_NO.match(title)
        if not m:
            continue
        no = _cn_int(m.group(1))
        if no is None:
            continue
        if prev_no is not None and no <= prev_no:
            want = prev_no + 1
            want_cn = _int_cn(want)
            if want_cn.endswith(m.group(1)):
                out[k] = (pr, lv, f"第{want_cn}章" + title[m.end():])
                fixed += 1
                no = want
        prev_no = no
    return out, fixed


def parse_ziben(text_by_pdf: dict[int, str], pages: list[int], heads: set[str]):
    """《资本论》多级目录。册/篇（无页码）→ 章 → 数字节；更深层不收。"""
    out: list[tuple[int, int, str]] = []
    acc: list[str] = []
    pending_head: list[tuple[int, str]] = []   # 待落地的「册/篇」（等下一条带页码的条目）
    for p in pages:
        for raw in text_by_pdf.get(p, "").splitlines():
            line = raw.strip()
            if is_noise(line, heads):
                continue
            if _ZB_STOP.match(nkey(line)):
                return out, pending_head
            m = _TAIL_PAGE.search(line)
            has_page = bool(m and nkey(line[: m.start()]))
            head = (acc[0] if acc else line).strip()
            if not has_page:
                # 册/篇标题独占一行且不带页码。两种排法都要认：
                #   「第一篇」换行「商品和货币」    → 攒够两行才是完整一条；
                #   「第四篇 相对剩余价值的生产」   → 一行就完整，必须当场收束。
                # 只按「攒够两行」收的话，单行式的篇标题会一直挂在累积区，把紧随其后的
                # 「第十章 …… 363」整条拼进来，产出「第四篇相对剩余价值的生产第十章
                # 相对剩余价值的概念」这样的怪条目，而真正的第十章条目则整条消失。
                acc.append(line)
                flat = clean_title(acc)
                if _ZB_CE.match(nkey(flat)) or _ZB_PIAN.match(nkey(flat)):
                    if len(_ZB_HEAD_PREFIX.sub("", nkey(flat))) >= 2:   # 已带主题名
                        pending_head.append((1, flat))
                        acc = []
                continue
            if _ZB_DEEP.match(nkey(head)):
                acc = []          # 深层条目：整条丢弃，不参与后续拼接
                continue
            title = _zb_trim(clean_title(acc, line[: m.start()]))
            acc = []
            if not (2 <= len(title) <= 110):
                continue
            level = 2 if _ZB_ZHANG.match(nkey(title)) else (3 if _ZB_SEC.match(nkey(title)) else 1)
            printed = to_int(m.group(1))
            for lv, t in pending_head:
                out.append((printed, lv, t))   # 册/篇落在其下第一条的页上
            pending_head = []
            out.append((printed, level, title))
    return out, pending_head


# ---------------------------------------------------------------- essay 版式
# 篇章目录的页码标记：**至少两个点引线字符** + 起页（可带「—止页」）。要求点引线在前，
# 是为了和日期括注里的数字区分开——「（二〇二二年十月十六日）」里也有数字，但它前面
# 没有点引线。
_ESSAY_PAGE = re.compile(
    # 常规排法：「篇名……47」或「篇名……（47）」；也兼容 GLM 把点引线省掉后留下的
    # 「篇名 （47）」以及把页码单独识别成一行的「(47)」。第二个分支必须要求完整括号，
    # 因而不会把日期中的年份误当页码。
    r"(?:[·．.。…⋯⋅•\-—–_\s]{2,}[（(]?\s*([0-9０-９]{1,4})"
    r"(?:\s*[-—–~]\s*[0-9０-９]{1,4})?\s*[)）]?"
    r"|\s*[（(]\s*([0-9０-９]{1,4})\s*[)）])"
)
_DATE_LEAD = re.compile(
    r"^[（(]\s*[一二三四五六七八九十百零〇○Oo\d]{2,}\s*年[^）)]{0,28}[)）]"
)


def _attach_date(out: list, date: str) -> None:
    """把日期括注补到上一条篇名后（同名篇目靠日期消歧）。"""
    if not out:
        return
    printed, lv, title = out[-1]
    if "（" in title or "(" in title:
        return
    out[-1] = (printed, lv, f"{title}{nkey(date)}")


def parse_essay(text_by_pdf: dict[int, str], pages: list[int], heads: set[str]):
    """「篇名 …… 5-7」＋「（二〇一二年十一月十五日）」的篇章目录。

    同一本书里这三种排法都会出现，故按「页码标记」切分而不是按行切分：
      ① 篇名一行、页码在行尾、日期独占下一行（《党建文选》卷一、卷二前半）；
      ② 篇名+页码+日期挤在同一行（卷二 pdf8/9 那几页）；
      ③ 整页被 OCR 拼成一整行、十来条首尾相接（卷二 pdf7）。
    早先按行尾匹配页码，②③ 两种整页都收不到一条（卷二 56 篇只解析出 23 篇）。
    """
    out: list[tuple[int, int, str]] = []
    acc: list[str] = []
    for p in pages:
        for raw in text_by_pdf.get(p, "").splitlines():
            line = raw.strip()
            if is_noise(line, heads):
                continue
            pos = 0
            hit = False
            for mm in _ESSAY_PAGE.finditer(line):
                seg = line[pos:mm.start()].strip()
                pos = mm.end()
                hit = True
                dm = _DATE_LEAD.match(seg)      # 段首的日期属于上一条
                if dm:
                    _attach_date(out, dm.group(0))
                    seg = seg[dm.end():].strip()
                title = clean_title(acc, seg)
                acc = []
                if 2 <= len(title) <= 90:
                    page_group = mm.group(1) or mm.group(2)
                    out.append((to_int(page_group), 1, title))
            rest = line[pos:].strip() if hit else line
            if hit:
                dm = _DATE_LEAD.match(rest)
                if dm:
                    _attach_date(out, dm.group(0))
                    rest = rest[dm.end():].strip()
                if rest:
                    acc.append(rest)
            elif _DATE_ONLY.match(nkey(line)):
                if acc:
                    # 个别扫描目录页会把页码整行漏掉，但标题和日期仍完整（例如
                    # 《论党的自我革命》目录首页的前两篇）。先保留为 printed=0，
                    # 落地阶段再按正文页首标题定位并读取该页已识别的印刷页码。
                    title = clean_title(acc)
                    acc = []
                    if 2 <= len(title) <= 90:
                        out.append((0, 1, f"{title}{nkey(line)}"))
                else:
                    _attach_date(out, line)
            else:
                acc.append(line)
    return out


# ---------------------------------------------------------------- 落地
def build_volume(conn: sqlite3.Connection, book: str, volume: int, mode: str,
                 dry_run: bool, verbose: bool) -> list[tuple]:
    rows_raw = conn.execute(
        "SELECT pdf_page, printed_page, raw_text, source_file FROM pages "
        "WHERE book=? AND volume=? ORDER BY pdf_page",
        (book, volume),
    ).fetchall()
    if not rows_raw:
        print(f"[{book} 第{volume}卷] 无 pages，跳过（先跑 build_scan_volumes/build_textbook_index）。")
        return []
    source_file = rows_raw[0][3]
    text_by_pdf = {r[0]: (r[2] or "") for r in rows_raw}
    rows = [(r[0], r[1], r[2] or "") for r in rows_raw]
    mapper = PageMapper(rows)

    toc_pages = find_toc_pages(text_by_pdf)
    heads = running_heads(text_by_pdf, toc_pages, book)
    if mode == "topic":
        raw_entries = parse_topic(text_by_pdf, toc_pages, heads)
    elif mode == "ziben":
        raw_entries, leftover = parse_ziben(text_by_pdf, toc_pages, heads)
        if leftover:
            print(f"    提示：{len(leftover)} 条册/篇标题无后续条目，已丢弃")
        raw_entries, nfix = repair_chapter_numbers(raw_entries)
        if nfix:
            print(f"    修正 {nfix} 处 OCR 丢前缀的章序号（如「第五十章」被读成「第十章」）")
    else:
        raw_entries = parse_essay(text_by_pdf, toc_pages, heads)

    final: list[tuple[int, int, int, str]] = []   # (pdf, printed, level, title)
    last_pdf = 0
    dropped = 0
    for printed, level, title in raw_entries:
        if printed:
            got = mapper.resolve(printed, title, last_pdf, body_only=(mode == "essay"))
        else:
            got = mapper.locate(title, max(1, last_pdf), body_only=(mode == "essay"))
        if got is None:
            dropped += 1
            continue
        pdf_page, printed_real = got
        final.append((pdf_page, printed_real, level, respace_title(title)))
        last_pdf = pdf_page

    lvl = Counter(e[2] for e in final)
    print(f"[{book} 第{volume}卷] 目录页 pdf{toc_pages[:3]}…{toc_pages[-1:] } "
          f"→ 解析 {len(raw_entries)} / 落地 {len(final)}（层级 {dict(sorted(lvl.items()))}）"
          f"{f'，{dropped} 条定位失败丢弃' if dropped else ''}，偏移={mapper.offset}")
    show = final if verbose else (final[:5] + final[-3:] if len(final) > 8 else final)
    for pdf_page, printed, lv, title in show:
        print(f"      L{lv} 印{printed:>5} pdf{pdf_page:>5}  {title[:60]}")

    if dry_run or not final:
        return []
    return [
        (book, volume, source_file, title, pdf_page, str(printed), level, "body", i + 1)
        for i, (pdf_page, printed, level, title) in enumerate(final)
    ]


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="2026-08 新增书库的印刷目录解析。")
    ap.add_argument("--book", action="append", help="书库 key，可重复；默认全部")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="打印全部条目")
    args = ap.parse_args()
    books = args.book or list(BOOKS)
    unknown = [b for b in books if b not in BOOKS]
    if unknown:
        raise SystemExit("非本脚本处理的书库：" + ", ".join(unknown))

    conn = sqlite3.connect(str(DB_PATH))
    total = 0
    try:
        for book in books:
            vols = [r[0] for r in conn.execute(
                "SELECT DISTINCT volume FROM pages WHERE book=? ORDER BY volume", (book,))]
            if not vols:
                print(f"[{book}] pages 表里没有这个书库，跳过。")
                continue
            payload: list[tuple] = []
            for v in vols:
                payload.extend(build_volume(conn, book, v, BOOKS[book], args.dry_run, args.verbose))
            if payload and not args.dry_run:
                conn.execute("DELETE FROM toc_entries WHERE book=?", (book,))
                conn.executemany(
                    "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
                    "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
                    payload,
                )
                total += len(payload)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    if args.dry_run:
        print("\n[dry-run] 未写库。")
    elif total:
        update_hash()
        print(f"\n已写入 toc_entries 共 {total} 条，重算 sha256。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
