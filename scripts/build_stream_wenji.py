# -*- coding: utf-8 -*-
"""把 GitHub 仓库 MARX-EBOOKS/MLRAW 的 MEAS_zh（《马克思恩格斯文集》逐页 HTML）
构建为「流式阅读」可用的自托管静态书库 stream_library/wenji-zh/。

源数据特征（实测）：
  - 路径  MEAS_zh/<卷>/ME{卷}-{页}.html，一文件＝印本一页，页码在文件名里。
  - 每文件是 LLM 处理过的产物：开头有空行 + ```html 代码围栏，末尾可能有 ``` 围栏，且无 <meta charset>。
  - <title> 是「版心running header」，左右页交替（篇名/章名），不可用作篇目边界。
  - 真正的篇目/章边界是正文里的 <h1>（著作）与 <h2>（章/篇内分章）。
  - 脚注用页内锚点 <sup><a id="ZFn" href="#Fn">…  与  <aside><a id="Fn" href="#ZFn">…，
    逐页复用 F1/ZF1，拼接成长文档时必须按页加前缀，否则 id 冲突、脚注跳错。

构建策略（与站内 /wenku 阅读器零改动对接）：
  1) 清洗每页：去 ```html / ``` 围栏与前导空行，取 <body> 内部。
  2) 按卷把各页正文顺序拼接；每页边界注入 <a id="s{页}"> 页锚点 —— 阅读器 detectPage() 据此读出「当前页」。
  3) 以 <h1>/<h2> 为界把整卷切成「节文档」sec-NNN.html（控制单篇体量，滚动顺畅）。
  4) 每卷生成 index.html 目录（两级：著作/章），作为该卷入口 volume.index。
  5) 每节文档加「← 目录 / 上一节 / 下一节」导航；阅读器 injectReadingStyle() 统一书页式排版。

用法：
  python scripts/build_stream_wenji.py --src <MEAS_zh所在目录> [--out stream_library/wenji-zh]
  默认 --src 取临时克隆目录（见 DEFAULT_SRC），--out 取项目内 stream_library/wenji-zh。
"""
from __future__ import annotations

import argparse
import html as _html
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 源 9 个文件夹 → 文集卷次映射。源 26 = 反杜林论 + 自然辩证法 ≈ 文集第 9 卷主体。
VOLUME_MAP = [
    ("1", 1, "第一卷"),
    ("2", 2, "第二卷"),
    ("3", 3, "第三卷"),
    ("4", 4, "第四卷"),
    ("5", 5, "第五卷"),
    ("6", 6, "第六卷"),
    ("7", 7, "第七卷"),
    ("8", 8, "第八卷"),
    ("26", 9, "第九卷（反杜林论·自然辩证法）"),
]

FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$", re.M)
BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.S | re.I)
HEAD_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
PAGE_RE = re.compile(r"-(\d+)\.html?$", re.I)
HEADING_RE = re.compile(r"<(h[12])\b[^>]*>(.*?)</\1>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _clean_page_html(raw: str) -> str:
    """去掉 ```html / ``` 代码围栏与前后空白，返回纯 HTML。"""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = FENCE_RE.sub("", text)
    return text.strip()


def _page_body(raw: str) -> str:
    """取 <body> 内部；无 body 标签时退回整段（去掉 <head>… 与文档外壳）。"""
    cleaned = _clean_page_html(raw)
    m = BODY_RE.search(cleaned)
    if m:
        return m.group(1).strip()
    # 兜底：剥掉 <head>…</head> 与 <html>/<!doctype> 外壳
    cleaned = re.sub(r"<head\b.*?</head>", "", cleaned, flags=re.S | re.I)
    cleaned = re.sub(r"</?(?:html|body)[^>]*>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<!doctype[^>]*>", "", cleaned, flags=re.I)
    return cleaned.strip()


def _namespace_footnotes(body: str, page: str) -> str:
    """按页给脚注锚点加前缀，避免拼接后 id 冲突：
    id="F1"/id="ZF1" → id="pg{page}_F1"；href="#F1" → href="#pg{page}_F1"。"""
    pref = f"pg{page}_"

    def fix_id(m: re.Match) -> str:
        return f'id="{pref}{m.group(1)}"'

    def fix_href(m: re.Match) -> str:
        return f'href="#{pref}{m.group(1)}"'

    body = re.sub(r'id="(Z?F\d+)"', fix_id, body)
    body = re.sub(r'href="#(Z?F\d+)"', fix_href, body)
    # 页末脚注块打 class：阅读器据此把它排成「注」的样子（小字号/浅色/分隔线）。
    # 源里是裸 <aside>，与正文同字号同色，读者会当成正文读——见 wenku_reader.html
    # 的 aside.fn-notes 规则。按 class 限定作用域，/wenku 的俄德书不受牵连。
    body = re.sub(r"<aside(?![^>]*\bclass=)", '<aside class="fn-notes"', body)
    return body


# 句末标点：判断上一页末段是否已收句。含书名号/引号收尾（「…《资本论》」「…是社会动物。"」）。
_TERMINAL = "。！？…；：》」』）】.!?;:)»\"'"


def _merge_across_page_break(prev_part: str, anchor: str, body: str) -> str | None:
    """印本分页把一个自然段切成两半时，把两半接回一段；接不上返回 None。

    源是逐页 HTML，一段跨页就成了两个 <p>（页 N 末尾一个、页 N+1 开头一个），
    拼接后读者看到的就是句子中间凭空断开、且多出一个段间距：
        <p>…总是指在一定社会发展阶段上的生产——社会</p>
        <a id="s9"></a>
        <p style="text-indent: 0;">个人的生产。因而…</p>

    判据用双条件合取，缺一不可：
      ① 页 N+1 首段带 text-indent:0 —— 源已标明「接排、非新段」；
      ② 页 N 末段未收句 —— 结尾不是句末标点。
    实测卷 8：单用①有 22 例上段其实已收句（真新段）会被误并；单用②则以注号
    或书名号收尾的正常段会被误判未收句。合取后卷 8 命中 92 例，全部夹着页锚点。

    页锚点合并后落在**断点的确切位置**（段内），detectPage/pageOfSelection 均按
    文档序取锚点，故不仅不受影响，取页反而更准。

    页 N 以脚注 <aside> 收尾时（正文段…+页末脚注块），先把脚注块暂摘下，合并正文段
    与下页首段后，再把脚注块移到合并段之后——脚注注号在正文段内，块移位后 id→注文
    链接不变。否则这类页的正文段（以 </aside> 结尾的 body）无法接排，是断裂盲区。
    """
    prev = prev_part.rstrip()
    # 摘下页末**紧邻**的一个或多个脚注块（合并后回插到合并段之后）。
    # 关键：单块用 (?:(?!</aside>)[\s\S])*? 而非 .*?——否则 prev 内有多个不相邻 aside 时
    # （多页已合并、脚注块散落在正文之间），.*? 会因末尾 $ 约束回溯、跨 </aside> 吞掉
    # 中间正文，摘出一大段含正文的"伪 aside"，导致合并错乱或漏合并。
    trailing_aside = ""
    m_as = re.search(r"((?:<aside\b(?:(?!</aside>)[\s\S])*?</aside>\s*)+)$", prev, re.S)
    if m_as:
        trailing_aside = m_as.group(1).rstrip()
        prev = prev[:m_as.start()].rstrip()
    if not prev.endswith("</p>"):
        return None
    opens = list(re.finditer(r"<p\b[^>]*>", prev))
    if not opens:
        return None
    last_open = opens[-1]
    inner_prev = prev[last_open.end():-len("</p>")]
    text_prev = TAG_RE.sub("", _html.unescape(inner_prev)).strip()
    if not text_prev or text_prev[-1] in _TERMINAL:
        return None

    mb = re.match(r"\s*<p\b([^>]*)>(.*?)</p>", body, re.S)
    if not mb:
        return None
    attrs, inner_next = mb.group(1), mb.group(2)
    # 本函数只在**页接缝**（页 N 末段 ↔ 页 N+1 首段）调用，故"上段未收句"本身
    # 就是分页切断的强信号。上面已用 _TERMINAL 排除了正常收句（。！？"』」》）等）；
    # 剩下能走到这里的，末字要么是汉字（如"德国政"↵"府…"、"敷粉的"↵"辫子…"，词被
    # 切在中间），要么是连词/逗号顿号——都必是断裂，直接接回。
    # 仍排除的：末字为注号/破折号/冒号等非汉字非连词标记（可能是句末注号或引出冒号，
    # 收句与否需更多上下文），这类回退到"下段带 text-indent:0"才合并，稳妥。
    last = text_prev[-1]
    han_ending = "一" <= last <= "鿿"                 # 末字是汉字
    strong_break = last in "和与及或而且，、"                    # 连词/逗号/顿号
    if not (han_ending or strong_break) and not re.search(r"text-indent:\s*0", attrs):
        return None

    merged = prev[:-len("</p>")] + anchor + inner_next + "</p>"
    if trailing_aside:                        # 脚注块回插到合并段之后
        merged += "\n" + trailing_aside
    return merged + body[mb.end():]


def _text_of(fragment: str) -> str:
    """取标签内纯文本（去 <sup>注号</sup> 等），用于目录标题。"""
    # 去掉上标注号 <sup>…</sup>（含编者尾注 [n]）
    fragment = re.sub(r"<sup\b.*?</sup>", "", fragment, flags=re.S | re.I)
    txt = TAG_RE.sub("", fragment)
    txt = _html.unescape(txt)
    # 折叠空白；删除形如 [16] 的尾注号
    txt = re.sub(r"\s+", " ", txt).strip()
    txt = re.sub(r"\[\d+\]", "", txt).strip()
    return txt


def _build_volume_stream(src_vol_dir: Path) -> tuple[str, list[str], int]:
    """把一卷所有页拼成单一 HTML 串，逐页前插 <a id="s{页}">；
    返回 (拼接串, 页码列表, 跨页断段合并数)。"""
    files = sorted(
        (p for p in src_vol_dir.glob("*.htm*")),
        key=lambda p: int(PAGE_RE.search(p.name).group(1)) if PAGE_RE.search(p.name) else 0,
    )
    parts: list[str] = []
    pages: list[str] = []
    merged = 0
    for fp in files:
        m = PAGE_RE.search(fp.name)
        if not m:
            continue
        page = str(int(m.group(1)))  # 去前导零
        raw = fp.read_text(encoding="utf-8", errors="replace")
        body = _page_body(raw)
        if not body:
            continue
        body = _namespace_footnotes(body, page)
        anchor = f'<a id="s{page}" class="pgmark" aria-hidden="true"></a>'
        if parts:
            # 上一页末段与本页首段本是同一段被印本切开 → 接回一段，页锚点落到断点处
            joined = _merge_across_page_break(parts[-1], anchor, body)
            if joined is not None:
                parts[-1] = joined
                pages.append(page)
                merged += 1
                continue
        parts.append(f"{anchor}\n{body}")
        pages.append(page)
    return "\n".join(parts), pages, merged


def _split_into_sections(stream: str) -> list[dict]:
    """以 <h1>/<h2> 为界把整卷串切成节。每节带：level、title、start_page、html。
    首个标题前的内容（卷首/序）单列为一节。start_page 取该节起点前最近的页锚点。"""
    heads = list(HEADING_RE.finditer(stream))
    anchor_re = re.compile(r'<a id="s(\d+)"')

    def page_before(pos: int) -> str:
        last = ""
        for am in anchor_re.finditer(stream, 0, pos + 1):
            last = am.group(1)
        return last

    sections: list[dict] = []
    # 卷首（首个标题之前）。仅当内容较长（真有正文/序）才单列；否则（多为孤立的作者署名行）
    # 并入紧随其后的首节，避免目录出现「卷首：卡·马克思」之类的碎片条目。
    first = heads[0].start() if heads else len(stream)
    pre = stream[:first].strip()
    pre_text = TAG_RE.sub("", pre).strip()
    carry = ""  # 待并入首个标题节的前导碎片（短署名/锚点）
    if pre and pre_text:
        if len(pre_text) >= 60 or not heads:
            sections.append({"level": 0, "title": "卷首", "start_page": page_before(first) or "", "html": pre})
        else:
            carry = pre
    # 各标题节
    for i, h in enumerate(heads):
        start = h.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(stream)
        level = 1 if h.group(1).lower() == "h1" else 2
        title = _text_of(h.group(2)) or ("（无题）")
        body_html = stream[start:end].strip()
        start_page = page_before(start)
        if carry:
            body_html = carry + "\n" + body_html
            sp = page_before(first)
            if sp:
                start_page = sp
            carry = ""
        sections.append({
            "level": level,
            "title": title,
            "start_page": start_page,
            "html": body_html,
        })
    # 保证每节开头都带本节起始页锚点：标题常位于页锚点之后，分节后该锚点会落到上一节，
    # 导致「篇名页」整节无页码（detectPage 读不到当前页）。这里按 start_page 在节首补一个锚点。
    for sec in sections:
        sp = sec["start_page"]
        if sp and f'id="s{sp}"' not in sec["html"][:160]:
            sec["html"] = f'<a id="s{sp}" class="pgmark" aria-hidden="true"></a>\n' + sec["html"]
    return sections


SEC_DOC = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title} · 马克思恩格斯文集{vol_label}</title>
</head>
<body>
<nav class="nav">{nav_top}</nav>
{content}
<nav class="nav">{nav_bottom}</nav>
</body>
</html>
"""


def _nav(idx: int, total: int) -> str:
    links = []
    if idx > 0:
        links.append(f'<a href="sec-{idx:03d}.html">← 上一节</a>')
    links.append('<a href="index.html">目录</a>')
    if idx + 1 < total:
        links.append(f'<a href="sec-{idx + 2:03d}.html">下一节 →</a>')
    return " · ".join(links)


def _write_volume(out_vol_dir: Path, vol_label: str, sections: list[dict]) -> None:
    out_vol_dir.mkdir(parents=True, exist_ok=True)
    # 先清掉旧产物，避免上一次构建多出来的 sec-*.html 成为无人链接的残留孤儿文件。
    for old in out_vol_dir.glob("sec-*.html"):
        old.unlink()
    (out_vol_dir / "index.html").unlink(missing_ok=True)
    total = len(sections)
    for i, sec in enumerate(sections):
        doc = SEC_DOC.format(
            title=_html.escape(sec["title"]),
            vol_label=_html.escape(vol_label),
            nav_top=_nav(i, total),
            nav_bottom=_nav(i, total),
            content=sec["html"],
        )
        (out_vol_dir / f"sec-{i + 1:03d}.html").write_text(doc, encoding="utf-8")
    _write_toc(out_vol_dir, vol_label, sections)


def _write_toc(out_vol_dir: Path, vol_label: str, sections: list[dict]) -> None:
    rows = []
    for i, sec in enumerate(sections):
        href = f"sec-{i + 1:03d}.html"
        title = _html.escape(sec["title"])
        page = sec["start_page"]
        page_html = f'<span class="pg">第 {page} 页</span>' if page else ""
        cls = {0: "lvl0", 1: "lvl1", 2: "lvl2"}.get(sec["level"], "lvl1")
        rows.append(
            f'<li class="{cls}"><a href="{href}"><span class="t">{title}</span>{page_html}</a></li>'
        )
    toc = TOC_DOC.format(vol_label=_html.escape(vol_label), rows="\n".join(rows))
    (out_vol_dir / "index.html").write_text(toc, encoding="utf-8")


# 目录在阅读器 iframe 内打开，会被 wenku_reader 的 injectReadingStyle 叠加（强制衬线、暖纸底、
# a{color/text-decoration!important}）。故这里用更高优先级选择器 + 必要处 !important 守住版式：
# 每条目纵向舒展成整行可点的「行卡」，悬停高亮 + 左侧红条，著作(lvl1)分组留白、章(lvl2)缩进轻量。
TOC_DOC = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>目录 · 马克思恩格斯文集{vol_label}</title>
<style>
  body{{max-width:820px;margin:0 auto;padding:30px 26px 96px;line-height:1.8;color:#241d17;}}
  h1.toc-h{{font-size:1.5em;text-align:center;font-weight:700;margin:.1em 0 1.3em;padding-bottom:.55em;border-bottom:2px solid #e3d8c8;}}
  ul.toc{{list-style:none;padding:0;margin:0;font-size:.92em;}}
  ul.toc li{{margin:0;}}
  ul.toc li a{{display:flex;align-items:baseline;gap:16px;padding:13px 16px;border-radius:10px;text-decoration:none!important;color:inherit;transition:background .12s,box-shadow .12s;}}
  ul.toc li a:hover{{background:#fbf3ef;box-shadow:inset 3px 0 0 #8f1d1d;text-decoration:none!important;}}
  ul.toc li a .t{{flex:1 1 auto;line-height:1.5;}}
  ul.toc li a .pg{{flex:0 0 auto;font-size:.8em;color:#b0a89e;white-space:nowrap;align-self:flex-start;margin-top:.2em;}}
  /* 著作：分组标题感，组间留白 + 细分隔线 */
  ul.toc li.lvl1{{margin-top:.55em;border-top:1px solid #ede5d8;}}
  ul.toc li.lvl1:first-child{{border-top:none;margin-top:0;}}
  ul.toc li.lvl1 a .t{{font-weight:700;font-size:1.08em;color:#1c1917;}}
  /* 章：缩进、轻量 */
  ul.toc li.lvl2 a{{padding-left:2.5em;}}
  ul.toc li.lvl2 a .t{{color:#5c554d;font-size:.99em;}}
  ul.toc li.lvl0 a .t{{color:#8a7f72;font-style:italic;}}
</style>
</head>
<body>
<h1 class="toc-h">马克思恩格斯文集 · {vol_label}</h1>
<ul class="toc">
{rows}
</ul>
</body>
</html>
"""


def build(src_root: Path, out_root: Path) -> None:
    if not src_root.is_dir():
        raise SystemExit(f"源目录不存在：{src_root}")
    out_root.mkdir(parents=True, exist_ok=True)
    grand_total_sec = 0
    for src_name, vol_n, vol_label in VOLUME_MAP:
        src_vol = src_root / src_name
        if not src_vol.is_dir():
            print(f"  [跳过] 源缺卷 {src_name}")
            continue
        stream, pages, merged = _build_volume_stream(src_vol)
        sections = _split_into_sections(stream)
        out_vol = out_root / str(vol_n)
        _write_volume(out_vol, vol_label, sections)
        grand_total_sec += len(sections)
        print(f"  卷{vol_n}（源{src_name} {vol_label}）：{len(pages)} 页 → {len(sections)} 节"
              f"，接回跨页断段 {merged} 处  [{out_vol}]")
    print(f"完成：共 {grand_total_sec} 节，输出于 {out_root}")


def main() -> None:
    default_src = PROJECT_ROOT / ".cache" / "MLRAW" / "MEAS_zh"
    ap = argparse.ArgumentParser(description="构建《马克思恩格斯文集》流式阅读静态书库")
    ap.add_argument("--src", default=str(default_src), help="MEAS_zh 源目录（含 1..8、26 子文件夹）")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "stream_library" / "wenji-zh"), help="输出目录")
    args = ap.parse_args()
    build(Path(args.src), Path(args.out))


if __name__ == "__main__":
    main()
