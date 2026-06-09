"""
索引构建脚本：把 PDF 文件抽取成可搜索的 SQLite 库。

改进点：
1. 不再只看 raw_text 的前2行/后2行，而是优先查看页面边缘区域的 words。
2. 放宽页码格式识别：支持 86 / [86] / (86) / —86— / 86.
3. 对同一 PDF 内的漏识别页做顺序补全：如果前后页页码连续且中间差值唯一，则自动填补。
4. 新增“空白页感知补全”：若缺失段中包含明显空白页，则允许只给非空白页补页码，空白页保持 None。
5. 运行时优先读取 exe 同级目录下的 data/corpus.sqlite；若不存在，则回退到打包进 exe 的 data/corpus.sqlite。
   这样既支持“单 exe 发布”，也支持“exe + 外置 data 目录发布”。
"""

from __future__ import annotations

import argparse
import collections
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from pathlib import Path

import fitz  # pymupdf
import yaml

from book_config import load_book_configs
from runtime_env import (
    BUNDLE_ROOT as _BUNDLE,
    CONFIG_DIR,
    EXTERNAL_DATA_DIR,
    RUNTIME_ROOT as _EXEDIR,
    resolve_runtime_db_files,
)


# 路径处理：
# - _BUNDLE：PyInstaller onefile 运行时的临时解压目录；未打包时即脚本目录
# - _EXEDIR：exe 所在目录；未打包时也等于脚本目录
ROOT = _BUNDLE
PDF_ROOT = _EXEDIR / "pdfs"
MANIFEST = CONFIG_DIR / "manifest.yaml"
VOLUMES = CONFIG_DIR / "volumes.yaml"
TEXT_CORRECTIONS = CONFIG_DIR / "wenji_text_corrections.yaml"
BUILD_DB_PATH = EXTERNAL_DATA_DIR / "corpus.sqlite"


def _resolve_runtime_db_path() -> Path:
    """
    运行时索引路径选择规则：
    1) 优先使用 exe 同级目录 data/corpus.sqlite（便于外置更新索引）；
    2) 若不存在，再使用打包进 exe 的 data/corpus.sqlite（便于单文件发布）；
    3) 两者都没有时，仍返回外置路径，方便上层报错提示。
    """
    db_path, _hash_path = resolve_runtime_db_files()
    if db_path is not None:
        return db_path
    return EXTERNAL_DATA_DIR / "corpus.sqlite"


# 兼容 app.py 的既有导入方式：from build_index import DB_PATH
DB_PATH = _resolve_runtime_db_path()

# 所有需要在归一化时被剥离的字符类型：标点、空白、控制符
_STRIP_RE = re.compile(
    r"[\s\u3000"
    r"\u2000-\u206f"          # 通用标点
    r"\u2e00-\u2e7f"          # 补充标点
    r"\u3000-\u303f"          # CJK 符号和标点
    r"\uff00-\uffef"          # 全角 ASCII / 半角片假名
    r"!-/:-@\[-`\{-~"         # ASCII 标点
    r"]+"
)

# 允许更宽松的页码形式：86 / [86] / (86) / —86— / 86.
_PAGE_TOKEN_RE = re.compile(
    r"^\s*[\[\(（【<〈『「\-—–·\.]*\s*(\d{1,4})\s*[\]\)）】>〉』」\-—–·\.]*\s*$"
)
_ROMAN_TOKEN_RE = re.compile(
    r"^\s*[\[\(（【<〈『「\-—–·\.]*\s*([ivxlcdm]{1,10})\s*[\]\)）】>〉』」\-—–·\.]*\s*$",
    re.IGNORECASE,
)

# 粗判空白页用的阈值：尽量保守
_BLANK_NORM_LEN_MAX = 20
_BLANK_VISIBLE_CHAR_MAX = 8


def normalize(text: str) -> str:
    """把文字归一化：剥离所有标点和空白。"""
    text = unicodedata.normalize("NFKC", text)
    text = _STRIP_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# 正文修订映射（text corrections）
#
# 目的：修正 PDF 文本层抽取出的 OCR 缺陷（拉丁/数字混入、形近错别字、阅读顺序
# 错乱），使 引文检索 的 normalized_text 能与 PDF 原文对应、提升召回。
#
# 安全约束（务必保持）：
#   - 修订在 raw_text 抽取后、normalize() 之前应用，仅影响检索/引文片段，
#     不影响阅读器实时渲染的 PDF（用户屏幕所见不受影响）。
#   - 每条修订锁定到 (book, volume, pdf_page)，find 须在该页按期望次数命中，
#     否则只打印告警并跳过，绝不抛错、绝不中断构建。
#   - 仅应用 conf 为 high 或缺省的条目；低置信度条目应放在复核文档而非此处。
#   - 配置缺失/损坏时退化为“无修订”，构建照常进行。
# ---------------------------------------------------------------------------

_TEXT_CORRECTIONS_CACHE: dict | None = None
_corrections_stats = {
    "applied": 0,
    "missing": 0,
    "ambiguous": 0,
    "entries": 0,
    "book_wide": 0,
    "volume_wide": 0,
    "ocr_punct": 0,
    "ocr_skip": 0,
}

# ---------------------------------------------------------------------------
# OCR 标点误识清理（仅 文集）
#
# 《文集》PDF 为扫描件 + 隐藏 OCR 文本层（font=HiddenHorzOCR）。OCR 常把正文中的
# 中文标点（：。，；等）误识为一个孤立拉丁字母，且该字母字号显著小于本行正文。
# 例：“得出结论z 人” 实为 “得出结论：人”。归一化会剥离标点，故只要去掉这个
# 误识字母（及其周边 OCR 空格），normalized_text 即与原文对应、检索可命中。
#
# 判定（三重约束，确保高置信、零误伤）：
#   1) 单个孤立拉丁字母（左右都不是字母）；
#   2) 该字母字号 < 本行 CJK 正文中位字号 × _OCR_PUNCT_RATIO（小字号 = 标点而非正文）；
#   3) 跳过空格后，左右最近字符都是 CJK（位于两段中文之间）。
# 资本论公式变量（G/W/P/m/v/c 等）为正文字号，必不满足约束 2，天然豁免。
# 仅当某锚定串在该页出现次数与被判定次数一致时才替换，否则跳过（绝不误伤）。
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_OCR_PUNCT_RATIO = 0.6
_ROMAN_UPPER = set("IVXLCDM")      # 合法罗马数字大写字母（第I编/第V部类等），豁免移除
_DIGIT_CONFUSABLE = set("lioO")    # 与数字 1/0 形近，禁止自动删除（防删数字/“一”）
_BODYSIZE_Z_EXCLUDE_VOLUMES = {9}  # 第9卷含坐标变量 z，z 仍受字号约束；其余卷孤立 z 必为标点
# 夹在汉字间、被 OCR 误识为标点的"杂符号"（如 $ 误识句号、= 误识冒号）。
# 与字母同样受"小字号"约束：正文字号的 = / | + 是《资本论》公式运算符，必不满足小字号而豁免。
_JUNK_SYMBOLS = set("$=|+*/@^~")
# 这些符号在《文集》正文中永不合法（金额 $100 由"相邻数字"规则排除），不受字号约束、一律删；
# 其余 = | + * / 是《资本论》公式/分隔符，仅删小字号（正文字号者豁免）。
_NEVER_LEGIT_SYMBOLS = set("$@^~")
_SCAN_RE = re.compile(r"[A-Za-z$=|+*/@^~]")
# 引号类：找 CJK 锚点时跳过它们，但替换时保留（如 生产力"s→生产力"，只删 s）。
_QUOTE_CHARS = set('"' + "'" + "“”‘’「」『』")


def _ocr_punct_pairs_for_page(page, volume: int) -> list[tuple[str, str]]:
    """从 PDF 字形几何推断“被误识为拉丁字母的标点”，返回 (find, replace) 上下文锚定移除项。"""
    try:
        d = page.get_text("dict")
    except Exception:  # 单页解析异常不应中断构建
        return []
    z_ignores_size = volume not in _BODYSIZE_Z_EXCLUDE_VOLUMES
    pairs: list[tuple[str, str]] = []
    for blk in d.get("blocks", []):
        if blk.get("type", 0) != 0:  # 跳过图像块
            continue
        for ln in blk.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text")]
            if not spans:
                continue
            cjk_sizes = [s["size"] for s in spans if _CJK_RE.search(s["text"])]
            if not cjk_sizes:
                continue
            body = statistics.median(cjk_sizes)
            if body <= 0:
                continue
            line = ""
            sizes: list[float] = []
            for s in spans:
                line += s["text"]
                sizes.extend([s["size"]] * len(s["text"]))
            n = len(line)
            for m in _SCAN_RE.finditer(line):
                i = m.start()
                ch = line[i]
                is_symbol = ch in _JUNK_SYMBOLS
                if is_symbol:
                    # 排除公式/金额：相邻是同符号或字母数字（如 ==、c+v、$100）→ 跳过
                    prev_bad = i > 0 and (line[i - 1] == ch or (line[i - 1].isascii() and line[i - 1].isalnum()))
                    next_bad = i + 1 < n and (line[i + 1] == ch or (line[i + 1].isascii() and line[i + 1].isalnum()))
                    if prev_bad or next_bad:
                        continue
                else:
                    # 必须是孤立单字母（注意：CJK 的 .isalpha() 为真，故须判 ASCII 字母）
                    prev_lat = i > 0 and line[i - 1].isascii() and line[i - 1].isalpha()
                    next_lat = i + 1 < n and line[i + 1].isascii() and line[i + 1].isalpha()
                    if prev_lat or next_lat:
                        continue
                    if ch in _ROMAN_UPPER or ch in _DIGIT_CONFUSABLE:
                        continue  # 合法罗马数字 / 数字形近字（第I编、第l册=第一册），保留交人工修订
                small = sizes[i] / body < _OCR_PUNCT_RATIO
                z_force = (not is_symbol) and ch == "z" and z_ignores_size
                sym_force = is_symbol and ch in _NEVER_LEGIT_SYMBOLS  # $@^~ 永不合法，不受字号约束
                if not (small or z_force or sym_force):
                    continue  # 正文字号合法字符（公式变量/运算符等）豁免；非科学卷孤立 z 必为标点
                # 注：希腊字母（ω/ι 等）夹汉字间可能是误识标点或误识汉字(法ι克福=法兰克福)，
                #     几何无法区分，故不在此删除，交由权威全文对齐处理。
                # 找左右最近的 CJK 锚点：跳过空格与引号（引号本身保留）
                li = i - 1
                while li >= 0 and (line[li].isspace() or line[li] in _QUOTE_CHARS):
                    li -= 1
                ri = i + 1
                while ri < n and (line[ri].isspace() or line[ri] in _QUOTE_CHARS):
                    ri += 1
                if li < 0 or ri >= n:
                    continue
                if _CJK_RE.match(line[li]) and _CJK_RE.match(line[ri]):
                    window = line[li:ri + 1]
                    jp = i - li  # 杂字符在窗口内的位置
                    kept = []
                    for k, c2 in enumerate(window):
                        if k == jp:
                            continue  # 删掉杂字符本身
                        if c2.isspace() and (k == jp - 1 or k == jp + 1):
                            continue  # 删掉紧邻杂字符的 OCR 空格
                        kept.append(c2)  # 其余（含引号、锚点 CJK）保留
                    replace = "".join(kept)
                    if window != replace:
                        pairs.append((window, replace))
    return pairs


def apply_ocr_punct_cleanup(raw: str, page, volume: int) -> str:
    """对单页 raw_text 应用 OCR 标点误识清理；仅在锚定串数量一致时替换，否则跳过。"""
    pairs = _ocr_punct_pairs_for_page(page, volume)
    if not pairs:
        return raw
    for (find, replace), n in collections.Counter(pairs).items():
        if raw.count(find) == n:
            raw = raw.replace(find, replace)
            _corrections_stats["ocr_punct"] += n
        else:
            _corrections_stats["ocr_skip"] += n
    return raw


def _valid_repl(entry) -> bool:
    f, r = entry.get("find"), entry.get("replace")
    return isinstance(f, str) and f != "" and isinstance(r, str) and entry.get("conf", "high") == "high"


def _load_text_corrections() -> dict:
    """读取 wenji_text_corrections.yaml。

    结构：
      book_wide:   [ {find, replace, conf, evidence} ]   # 对每个 文集 页应用（never-valid 串）
      volume_wide: { <卷号>: [ {find, replace, ...} ] }  # 对该卷每页应用
      pages:       [ {volume, page, find, replace, occ, conf, evidence} ]  # 页锚定（occ 校验）
    返回 {"book_wide":[(f,r)], "volume_wide":{vol:[(f,r)]}, "pages":{(vol,page):[{find,replace,occ}]}}。
    """
    global _TEXT_CORRECTIONS_CACHE
    if _TEXT_CORRECTIONS_CACHE is not None:
        return _TEXT_CORRECTIONS_CACHE

    result: dict = {"book_wide": [], "volume_wide": {}, "pages": {}, "han_pages": {}}
    try:
        if TEXT_CORRECTIONS.exists():
            data = yaml.safe_load(TEXT_CORRECTIONS.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                for e in data.get("book_wide") or []:
                    if isinstance(e, dict) and _valid_repl(e):
                        result["book_wide"].append((e["find"], e["replace"]))
                        _corrections_stats["entries"] += 1
                vw = data.get("volume_wide") or {}
                if isinstance(vw, dict):
                    for vol, entries in vw.items():
                        try:
                            vol_i = int(vol)
                        except (TypeError, ValueError):
                            continue
                        for e in entries or []:
                            if isinstance(e, dict) and _valid_repl(e):
                                result["volume_wide"].setdefault(vol_i, []).append((e["find"], e["replace"]))
                                _corrections_stats["entries"] += 1
                for e in data.get("pages") or []:
                    if not isinstance(e, dict):
                        continue
                    vol, page = e.get("volume"), e.get("page")
                    if not (isinstance(vol, int) and isinstance(page, int) and _valid_repl(e)):
                        print(f"[corrections] 跳过非法 pages 条目: {e!r}", file=sys.stderr)
                        continue
                    occ = e.get("occ", 1)
                    if occ is not None and not isinstance(occ, int):
                        occ = 1
                    result["pages"].setdefault((vol, page), []).append(
                        {"find": e["find"], "replace": e["replace"], "occ": occ}
                    )
                    _corrections_stats["entries"] += 1
                # han_pages：在 raw 的"汉字投影"空间里匹配/替换（兼容标点/换行打断的上下文）
                for e in data.get("han_pages") or []:
                    if not isinstance(e, dict):
                        continue
                    vol, page = e.get("volume"), e.get("page")
                    if not (isinstance(vol, int) and isinstance(page, int) and _valid_repl(e)):
                        print(f"[corrections] 跳过非法 han_pages 条目: {e!r}", file=sys.stderr)
                        continue
                    if len(e["find"]) != len(e["replace"]):
                        print(f"[corrections] han_pages find/replace 不等长，跳过: {e!r}", file=sys.stderr)
                        continue
                    occ = e.get("occ", 1)
                    if occ is not None and not isinstance(occ, int):
                        occ = 1
                    result["han_pages"].setdefault((vol, page), []).append(
                        {"find": e["find"], "replace": e["replace"], "occ": occ}
                    )
                    _corrections_stats["entries"] += 1
    except Exception as exc:  # 配置损坏绝不应中断构建
        print(f"[corrections] 加载 {TEXT_CORRECTIONS} 失败，忽略全部修订：{exc}", file=sys.stderr)
        result = {"book_wide": [], "volume_wide": {}, "pages": {}, "han_pages": {}}

    _TEXT_CORRECTIONS_CACHE = result
    return result


def apply_text_corrections(book: str, volume: int, pdf_page: int, raw: str) -> str:
    """对单页 raw_text 应用手工修订（仅 文集）：先 文集级、再卷级（均为 never-valid 串，
    直接全替换），最后页锚定（occ 校验，命中失败只告警不改）。"""
    if book != "文集":
        return raw
    cfg = _load_text_corrections()
    for f, r in cfg["book_wide"]:
        c = raw.count(f)
        if c:
            raw = raw.replace(f, r)
            _corrections_stats["book_wide"] += c
            _corrections_stats["applied"] += c
    for f, r in cfg["volume_wide"].get(volume, []):
        c = raw.count(f)
        if c:
            raw = raw.replace(f, r)
            _corrections_stats["volume_wide"] += c
            _corrections_stats["applied"] += c
    for entry in cfg["pages"].get((volume, pdf_page), []):
        find = entry["find"]
        expected = entry["occ"]
        count = raw.count(find)
        if count == 0:
            _corrections_stats["missing"] += 1
            print(f"[corrections] 未命中 文集·第{volume}卷·p{pdf_page}: {find!r}", file=sys.stderr)
            continue
        if expected is not None and count != expected:
            _corrections_stats["ambiguous"] += 1
            print(
                f"[corrections] 命中数={count}≠期望{expected}，跳过 文集·第{volume}卷·p{pdf_page}: {find!r}",
                file=sys.stderr,
            )
            continue
        raw = raw.replace(find, entry["replace"])
        _corrections_stats["applied"] += 1
    raw = _apply_han_corrections(volume, pdf_page, raw)
    return raw


def _apply_han_corrections(volume: int, pdf_page: int, raw: str) -> str:
    """在 raw 的"汉字投影"空间内匹配 find（兼容标点/换行），把差异字符替换回 raw 原位置。

    find/replace 等长、han-only；二者差异位即需改的字符。仅当 han 投影中出现次数==occ 时应用，
    否则告警跳过（绝不误伤）。用于承接"权威全文对齐"产出的形近字修订。"""
    entries = _load_text_corrections()["han_pages"].get((volume, pdf_page))
    if not entries:
        return raw
    # 汉字投影 + 原始下标
    han_chars: list[str] = []
    han_idx: list[int] = []
    for i, ch in enumerate(raw):
        if "一" <= ch <= "鿿":
            han_chars.append(ch)
            han_idx.append(i)
    han = "".join(han_chars)
    edits: dict[int, str] = {}
    for entry in entries:
        find = entry["find"]
        repl = entry["replace"]
        occ = entry["occ"]
        positions = []
        start = 0
        while True:
            p = han.find(find, start)
            if p < 0:
                break
            positions.append(p)
            start = p + 1
        if not positions:
            _corrections_stats["missing"] += 1
            print(f"[corrections] han 未命中 文集·第{volume}卷·p{pdf_page}: {find!r}", file=sys.stderr)
            continue
        if occ is not None and len(positions) != occ:
            _corrections_stats["ambiguous"] += 1
            print(
                f"[corrections] han 命中数={len(positions)}≠期望{occ}，跳过 文集·第{volume}卷·p{pdf_page}: {find!r}",
                file=sys.stderr,
            )
            continue
        for p in positions:
            for k in range(len(find)):
                if find[k] != repl[k]:
                    edits[han_idx[p + k]] = repl[k]
        _corrections_stats["applied"] += 1
    if edits:
        buf = list(raw)
        for i, ch in edits.items():
            buf[i] = ch
        raw = "".join(buf)
    return raw


def _parse_page_token(text: str) -> str | None:
    """把候选字符串解析为页码。"""
    s = unicodedata.normalize("NFKC", text).strip()
    if not s:
        return None

    m = _PAGE_TOKEN_RE.match(s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 3000:
            return str(n)

    m = _ROMAN_TOKEN_RE.match(s)
    if m:
        return f"pre-{m.group(1).lower()}"

    return None



def _score_candidate(
    text: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    page_width: float,
    page_height: float,
) -> tuple[int, str] | None:
    """
    对候选页码打分。越靠近上/下边缘、越短、越孤立、越靠左右边或底部中部，分数越高。
    返回 (score, parsed_value)。
    """
    parsed = _parse_page_token(text)
    if not parsed:
        return None

    score = 0
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    width = x1 - x0
    height = y1 - y0

    # 边缘优先
    if cy <= page_height * 0.12:
        score += 6
    if cy >= page_height * 0.88:
        score += 8

    # 左下 / 右下 / 底中 都给分
    if cx <= page_width * 0.30:
        score += 3
    elif cx >= page_width * 0.70:
        score += 3
    elif page_width * 0.35 <= cx <= page_width * 0.65:
        score += 2

    # 字串越短越像页码
    clean_len = len(normalize(text))
    if clean_len <= 4:
        score += 5
    elif clean_len <= 8:
        score += 2

    # 小块优先
    if width <= page_width * 0.20:
        score += 2
    if height <= page_height * 0.05:
        score += 2

    # 罗马页码稍微降权，防止误识别正文里的罗马字母
    if parsed.startswith("pre-"):
        score -= 1

    return score, parsed



def detect_printed_page_from_text(raw_text: str) -> str | None:
    """
    兼容旧逻辑：从页面首尾几行中提取印刷页码。
    这里只作为兜底，避免完全依赖行顺序。
    """
    lines = [ln for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        return None

    candidates = []
    if len(lines) <= 6:
        candidates.extend(lines)
    else:
        candidates.extend(lines[:3])
        candidates.extend(lines[-3:])

    for ln in candidates:
        parsed = _parse_page_token(ln)
        if parsed:
            return parsed
    return None



def detect_printed_page_from_page(page: fitz.Page) -> str | None:
    """
    从 page 对象中识别印刷页码：
    1) 优先从 words 中看页面顶部/底部边缘区域；
    2) 再退回到 raw_text 的前后几行。
    """
    page_rect = page.rect
    page_width = float(page_rect.width)
    page_height = float(page_rect.height)

    candidates: list[tuple[int, str]] = []

    # words: (x0, y0, x1, y1, word, block_no, line_no, word_no)
    try:
        words = page.get_text("words")
    except Exception:
        words = []

    for w in words:
        x0, y0, x1, y1, token = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
        # 只看上下边缘区域
        if not (y1 <= page_height * 0.18 or y0 >= page_height * 0.82):
            continue
        scored = _score_candidate(token, x0, y0, x1, y1, page_width, page_height)
        if scored:
            candidates.append(scored)

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # blocks 兜底：有些页码可能被合成一个短块
    try:
        blocks = page.get_text("blocks")
    except Exception:
        blocks = []

    for b in blocks:
        x0, y0, x1, y1, text = float(b[0]), float(b[1]), float(b[2]), float(b[3]), str(b[4])
        if not (y1 <= page_height * 0.18 or y0 >= page_height * 0.82):
            continue
        for part in [p for p in text.splitlines() if p.strip()]:
            scored = _score_candidate(part, x0, y0, x1, y1, page_width, page_height)
            if scored:
                candidates.append(scored)

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # 最后才回退到 raw_text 行顺序法
    raw = page.get_text("text")
    return detect_printed_page_from_text(raw)



def _as_int_page(value: str | None) -> int | None:
    if value is None or value.startswith("pre-"):
        return None
    try:
        return int(value)
    except ValueError:
        return None



def is_probably_blank_page(raw_text: str, norm_text: str) -> bool:
    """
    粗判是否为空白页/隔页。
    只在文本极少时返回 True，尽量避免把短正文页误判为空白页。
    """
    if len(norm_text) > _BLANK_NORM_LEN_MAX:
        return False

    visible = []
    for ch in unicodedata.normalize("NFKC", raw_text):
        if ch.isspace():
            continue
        if unicodedata.category(ch).startswith(("P", "Z", "C")):
            continue
        visible.append(ch)

    return len(visible) <= _BLANK_VISIBLE_CHAR_MAX



def fill_missing_printed_pages(rows: list[tuple[str, int, str, int, str | None, str, str]]) -> int:
    """
    对同一 PDF 内的 printed_page 缺失项做顺序补全。

    两层策略：
    1) 严格连续补全：前后页码差值与页距完全一致时，全部补上。
    2) 空白页感知补全：若缺失段内含空白页，则允许“只补非空白页”，
       只要前后页码差值与“非空白缺失页数量 + 1”一致。
    返回补全数量。
    """
    filled = 0
    rows_mut = list(rows)

    values = [_as_int_page(r[4]) for r in rows_mut]
    n = len(values)
    i = 0
    while i < n:
        if values[i] is not None:
            i += 1
            continue

        start = i - 1
        j = i
        while j < n and values[j] is None:
            j += 1
        end = j  # 第一个非空的位置

        if not (start >= 0 and end < n and values[start] is not None and values[end] is not None):
            i = j
            continue

        left = values[start]
        right = values[end]
        gap = end - start

        # 方案1：完全连续，全部补
        if right - left == gap:
            for k in range(start + 1, end):
                inferred = str(left + (k - start))
                book, vol, source_file, pdf_page, _printed, raw, norm = rows_mut[k]
                rows_mut[k] = (book, vol, source_file, pdf_page, inferred, raw, norm)
                values[k] = int(inferred)
                filled += 1
            i = j
            continue

        # 方案2：空白页感知补全，只给非空白页补
        segment_indexes = list(range(start + 1, end))
        nonblank_indexes = [
            k for k in segment_indexes
            if not is_probably_blank_page(rows_mut[k][5], rows_mut[k][6])
        ]

        # 需要填入的非空白页数量 + 两端起点，应该正好对应页码跨度
        # 例如：85 [正文缺失] [空白页] 87
        # 则 right-left = 2, 非空白缺失页数 = 1，可只补正文缺失页为 86。
        if nonblank_indexes and right - left == len(nonblank_indexes) + 1:
            next_num = left + 1
            for k in segment_indexes:
                book, vol, source_file, pdf_page, _printed, raw, norm = rows_mut[k]
                if is_probably_blank_page(raw, norm):
                    continue
                inferred = str(next_num)
                rows_mut[k] = (book, vol, source_file, pdf_page, inferred, raw, norm)
                values[k] = next_num
                next_num += 1
                filled += 1

        i = j

    rows[:] = rows_mut
    return filled



def guess_volume_from_filename(name: str) -> int | None:
    """从文件名猜测卷号，失败返回 None。"""
    name = unicodedata.normalize("NFKC", name)
    patterns = [
        r"第\s*(\d{1,3})\s*卷",
        r"卷\s*(\d{1,3})",
        r"[Vv]ol\.?\s*(\d{1,3})",
        r"(?<!\d)(\d{1,3})(?!\d)",
    ]
    for pat in patterns:
        m = re.search(pat, name)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 999:
                return n
    return None



def scan_and_write_manifest() -> None:
    """扫描 pdfs/ 目录生成 manifest.yaml 草稿，保留已有条目。"""
    if not PDF_ROOT.exists():
        print(f"未找到目录 {PDF_ROOT}", file=sys.stderr)
        return

    books = load_book_configs()
    existing: dict[str, dict[str, int]] = {book.key: {} for book in books}
    if MANIFEST.exists():
        try:
            old = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
            for book in books:
                for item in old.get(book.key) or []:
                    existing[book.key][str(Path(item["file"]).as_posix())] = item["volume"]
        except Exception as e:
            print(f"读取旧 manifest 失败，将重新生成：{e}", file=sys.stderr)

    result: dict[str, list[dict]] = {book.key: [] for book in books}
    for book in books:
        folder = (_EXEDIR / book.folder).resolve()
        if not folder.exists():
            continue
        for pdf in sorted(folder.glob("*.pdf")):
            rel = pdf.relative_to(_EXEDIR).as_posix()
            vol = existing[book.key].get(rel) or guess_volume_from_filename(pdf.name)
            entry = {"file": rel, "volume": vol if vol else "# TODO: 请填写卷号"}
            result[book.key].append(entry)

    MANIFEST.write_text(
        yaml.safe_dump(result, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    n_total = sum(len(v) for v in result.values())
    print(f"已写入 {MANIFEST}，共扫描到 {n_total} 个 PDF。")
    print("请打开此文件核对每个 volume 值，然后运行 `python build_index.py` 构建索引。")



def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS pages;
        CREATE TABLE pages (
            id              INTEGER PRIMARY KEY,
            book            TEXT    NOT NULL,
            volume          INTEGER NOT NULL,
            source_file     TEXT    NOT NULL,
            pdf_page        INTEGER NOT NULL,
            printed_page    TEXT,
            raw_text        TEXT    NOT NULL,
            normalized_text TEXT    NOT NULL
        );
        CREATE INDEX idx_pages_book_vol ON pages(book, volume, pdf_page);
        CREATE INDEX idx_pages_source_file ON pages(source_file, pdf_page);
        """
    )



def build() -> None:
    if not MANIFEST.exists():
        print(f"未找到 {MANIFEST}。请先运行 `python build_index.py --scan`。", file=sys.stderr)
        sys.exit(1)

    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}

    BUILD_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if BUILD_DB_PATH.exists():
        BUILD_DB_PATH.unlink()
    conn = sqlite3.connect(BUILD_DB_PATH)
    init_db(conn)

    total_start = time.time()
    total_pages = 0
    total_detected = 0
    total_filled = 0

    for book_cfg in load_book_configs():
        book = book_cfg.key
        items = manifest.get(book) or []
        for item in items:
            vol = item.get("volume")
            if not isinstance(vol, int):
                print(f"跳过 {item.get('file')}：volume 未正确填写 ({vol!r})", file=sys.stderr)
                continue
            pdf_path = (_EXEDIR / item["file"]).resolve()
            if not pdf_path.exists():
                print(f"跳过：文件不存在 {pdf_path}", file=sys.stderr)
                continue

            t0 = time.time()
            detected = 0
            source_file = str(Path(item["file"]).as_posix())
            rows: list[tuple[str, int, str, int, str | None, str, str]] = []

            with fitz.open(pdf_path) as doc:
                for i, page in enumerate(doc, start=1):
                    raw = page.get_text("text")
                    raw = apply_text_corrections(book, vol, i, raw)
                    if book == "文集":
                        raw = apply_ocr_punct_cleanup(raw, page, vol)
                    printed = detect_printed_page_from_page(page)
                    if printed and not printed.startswith("pre-"):
                        detected += 1
                    norm = normalize(raw)
                    rows.append((book, vol, source_file, i, printed, raw, norm))

            filled = fill_missing_printed_pages(rows)

            conn.executemany(
                "INSERT INTO pages (book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            elapsed = time.time() - t0
            total_pages += len(rows)
            total_detected += detected
            total_filled += filled

            print(
                f"  [{book}·第{vol:02d}卷] {pdf_path.name}  "
                f"页数={len(rows)}  直接识别={detected}/{len(rows)}  自动补全={filled}  "
                f"耗时={elapsed:.1f}s"
            )

    conn.close()
    total_elapsed = time.time() - total_start
    final_detected = total_detected + total_filled
    print(
        f"\n完成。总页数={total_pages}  直接识别率={total_detected / max(1, total_pages):.1%}  "
        f"补全后识别率={final_detected / max(1, total_pages):.1%}  总耗时={total_elapsed:.1f}s"
    )
    print(f"索引文件：{BUILD_DB_PATH}")
    print(f"运行时默认读取：{DB_PATH}")
    cs = _corrections_stats
    print(
        f"正文修订：加载条目={cs['entries']}  已应用={cs['applied']}"
        f"（文集级={cs['book_wide']} 卷级={cs['volume_wide']} "
        f"页锚定={cs['applied'] - cs['book_wide'] - cs['volume_wide']}）  "
        f"未命中={cs['missing']}  命中数异常={cs['ambiguous']}"
    )
    print(
        f"OCR标点清理(文集)：已移除={cs['ocr_punct']}  锚定不一致跳过={cs['ocr_skip']}"
    )



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="扫描 pdfs/ 目录生成 manifest.yaml 草稿")
    args = ap.parse_args()

    if args.scan:
        scan_and_write_manifest()
    else:
        build()


if __name__ == "__main__":
    main()
