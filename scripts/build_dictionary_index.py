from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "dictionary.sqlite"
DEFAULT_REPORT = ROOT / "data" / "dictionary_polish_report.json"

INDEX_START_PAGE = 644
UPPER_PRINTED_OFFSET = 46
LOWER_PRINTED_OFFSET = -711
MAX_ENTRY_PAGES = 8
# 词目拼音索引从下册 pdf 第 644 页起，对应印刷页 1355；正文内容止于其前一页。
LAST_CONTENT_PRINTED_PAGE = 1354
# 段首缩进约 2 个汉字（≈21pt），栏基线本身有 ±5pt 的 OCR 抖动。
PARAGRAPH_INDENT_PT = 12.0

LEADER_RE = re.compile(r"[⋯·\.。．]{2,}")
PAGE_RE = re.compile(r"(?P<title>.*?)[⋯·\.。．\s]+(?P<page>\d{1,4})(?:[、,，]\d{1,4})?\s*$")
HEADER_RE = re.compile(
    r"^(?:\d+\s*)?马克思主义大辞典$|^词目拼音索引(?:\s+\d+)?$|^\d{1,4}$|^第[一二三四五六七八九十]+编"
)
LETTER_RE = re.compile(r"^[A-Z]$")
BAD_CHAR_RE = re.compile(r"[�□■◆◇]")
SPACE_BETWEEN_CJK_RE = re.compile(r"(?<=[一-鿿])\s+(?=[一-鿿])")
# 允许“邓小平(1904—1997)☆参见1236页”这类带人名前缀的交叉引用。
CROSS_REF_RE = re.compile(r"^.{0,40}?[☆★]\s*参见\s*\d{1,4}\s*页|^\s*参见\s*\d{1,4}\s*页", re.S)

# 标题匹配时忽略的字符：书名号/括号/引号/常见标点在索引与正文之间常被 OCR 混排。
_MATCH_IGNORED = set("《》〈〉<>()（）“”‘’\"'[]【】 \t　，、。；：！？—–·・•…")
_OPEN_BRACKETS = "《〈<(（"
_CLOSE_BRACKETS = "》〉>)）"
_PAIR_OK = {("(", ")"), ("（", "）"), ("《", "》"), ("〈", "〉"), ("<", ">")}
_TRAILING_CLOSERS = "》〉>)）”\""
# 行尾断词连字符的占位符，润色阶段删除。
SOFT_HYPHEN = "­"


@dataclass
class IndexEntry:
    title: str
    letter: str
    start_page: int
    sort_order: int


@dataclass
class Line:
    text: str
    para_start: bool


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _find_dictionary_pdfs(pdf_root: Path) -> tuple[Path, Path]:
    direct = pdf_root / "辞典"
    candidates = list(direct.glob("*.pdf")) if direct.exists() else []
    if len(candidates) != 2:
        for child in pdf_root.iterdir():
            if not child.is_dir():
                continue
            pdfs = list(child.glob("*.pdf"))
            if len(pdfs) == 2 and all("OCR" in pdf.name.upper() for pdf in pdfs):
                candidates = pdfs
                break
    if len(candidates) != 2:
        raise SystemExit("未找到 pdfs/辞典 下的上下册 OCR PDF。")
    upper = next((p for p in candidates if "_1-757" in p.name or p.name.startswith("上-")), None)
    lower = next((p for p in candidates if "_758-1420" in p.name or p.name.startswith("下-")), None)
    if upper is None or lower is None:
        ordered = sorted(candidates, key=lambda p: p.name)
        upper, lower = ordered[0], ordered[1]
    return upper, lower


def _clean_index_title(text: str) -> str:
    text = LEADER_RE.sub("", text)
    text = re.sub(r"\s+", "", text)
    return text.strip()


def _parse_index_entries(lower_pdf: Path) -> list[IndexEntry]:
    doc = fitz.open(lower_pdf)
    current_letter = "#"
    pending: list[str] = []
    entries: list[IndexEntry] = []
    sort_order = 0

    def emit(title_parts: list[str], page: int) -> None:
        nonlocal sort_order
        title = _clean_index_title("".join(title_parts))
        if not title or len(title) > 80:
            return
        sort_order += 1
        entries.append(
            IndexEntry(title=title, letter=current_letter, start_page=page, sort_order=sort_order)
        )

    try:
        for page_no in range(INDEX_START_PAGE, doc.page_count + 1):
            lines = doc.load_page(page_no - 1).get_text("text").replace("\r", "").splitlines()
            for raw in lines:
                line = raw.strip()
                if not line:
                    continue
                # 词目页码被 OCR 拆成独立的纯数字行（如“基本经济制度 / 1071”）：
                # 有待配对的词目且数值落在正文页范围内时按页码处理，避免词目粘连。
                if re.fullmatch(r"\d{1,4}", line):
                    page = int(line)
                    if pending and 2 <= page <= LAST_CONTENT_PRINTED_PAGE:
                        emit(pending, page)
                        pending.clear()
                    continue
                if HEADER_RE.match(line) or line.startswith("【说明】") or line.startswith("数排列"):
                    continue
                if "按“马克思”检索" in line or "词目中引号" in line:
                    continue
                if LETTER_RE.match(line):
                    current_letter = line
                    pending.clear()
                    continue
                match = PAGE_RE.match(line)
                if match:
                    parts = pending + [match.group("title")]
                    pending.clear()
                    emit(parts, int(match.group("page")))
                else:
                    if not LEADER_RE.fullmatch(line):
                        pending.append(line)
    finally:
        doc.close()
    deduped: list[IndexEntry] = []
    seen: set[tuple[str, int]] = set()
    for entry in entries:
        key = (entry.title, entry.start_page)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _page_to_pdf_index(printed_page: int) -> tuple[str, int]:
    if printed_page <= 711:
        return "upper", printed_page + UPPER_PRINTED_OFFSET
    return "lower", printed_page + LOWER_PRINTED_OFFSET


def _page_lines(page: fitz.Page) -> list[Line]:
    """抽取行并用段首缩进恢复分段信息。

    保留 OCR 文本层的原生流序（即正确的双栏阅读序）；按坐标重排反而会把
    居中的分节标题/跨行词条标题插错位置。x 坐标只用来判断段首缩进。
    """
    mid = page.rect.width / 2
    raw: list[tuple[int, float, str]] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not text:
                continue
            x0 = float(line["bbox"][0])
            col = 0 if x0 < mid else 1
            raw.append((col, x0, text))
    bases: dict[int, float] = {}
    for col in (0, 1):
        xs = [x0 for c, x0, text in raw if c == col and len(text) >= 4]
        if xs:
            bases[col] = min(xs)
    lines: list[Line] = []
    for col, x0, text in raw:
        base = bases.get(col)
        para = base is not None and PARAGRAPH_INDENT_PT <= (x0 - base) < 40.0
        lines.append(Line(text=text, para_start=para))
    return lines


def _extract_printed_pages(upper_pdf: Path, lower_pdf: Path) -> dict[int, list[Line]]:
    pages: dict[int, list[Line]] = {}
    for volume, path in (("upper", upper_pdf), ("lower", lower_pdf)):
        doc = fitz.open(path)
        try:
            for printed in range(2, LAST_CONTENT_PRINTED_PAGE + 1):
                which, pdf_page = _page_to_pdf_index(printed)
                if which != volume or pdf_page < 1 or pdf_page > doc.page_count:
                    continue
                pages[printed] = _page_lines(doc.load_page(pdf_page - 1))
        finally:
            doc.close()
    return pages


def _strip_noise_lines(lines: list[Line]) -> list[Line]:
    result: list[Line] = []
    for item in lines:
        line = item.text.strip()
        if not line:
            continue
        compact = re.sub(r"\s+", "", line)
        if HEADER_RE.match(compact):
            continue
        if re.match(r"^\d{1,4}\s+马克思主义大辞典$", line):
            continue
        if re.match(r"^马克思主义大辞典\s*\d{1,4}$", line):
            continue
        if "词目拼音索引" in line or "目录" == line:
            continue
        # 各编内的分节标题（如“五、重要会议”）不属于任何词条正文。
        if re.fullmatch(r"[一二三四五六七八九十]+、[一-鿿]{2,12}", compact):
            continue
        result.append(Line(text=line, para_start=item.para_start))
    return result


def _title_core(title: str) -> str:
    return "".join(ch for ch in title if not ch.isspace() and ch not in _MATCH_IGNORED)


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


@dataclass
class HeadingMatch:
    line_index: int       # 词条正文起始行（rest 非空时该行以 rest 开头）
    rest: str             # 标题被吃掉后同行剩余文本（keep_full_line 时为整行）
    keep_full_line: bool  # 人名条目保留“全名·惯用名”整行
    body_has_outer_marks: bool  # 正文标题外层带《》而索引词目没带


def _match_heading_from(lines: list[Line], start: int, title: str) -> HeadingMatch | None:
    """从 lines[start] 起做跨行模糊前缀匹配（忽略括号/书名号/标点差异）。"""
    core = _title_core(title)
    if not core:
        return None
    li, ci, ti = start, 0, 0
    first_sig: str | None = None
    opened_with_mark = False
    # 长标题允许 1 个 OCR 形近错字（如“杜会”对“社会”），首末字必须吻合。
    typo_budget = 1 if len(core) >= 10 else 0
    while ti < len(core):
        if li >= len(lines) or li - start >= 4:
            return None
        text = lines[li].text
        if ci >= len(text):
            li += 1
            ci = 0
            continue
        ch = text[ci]
        if ch.isspace() or ch in _MATCH_IGNORED:
            if first_sig is None and ch == "《":
                opened_with_mark = True
            ci += 1
            continue
        if first_sig is None:
            first_sig = ch
        if ch != core[ti]:
            if typo_budget > 0 and 0 < ti < len(core) - 1 and _is_cjk(ch) and _is_cjk(core[ti]):
                typo_budget -= 1
            else:
                return None
        ti += 1
        ci += 1
    # 吞掉标题尾部的闭合括号/书名号
    consumed_closer = False
    while li < len(lines):
        text = lines[li].text
        if ci >= len(text):
            break
        ch = text[ci]
        if ch in _TRAILING_CLOSERS:
            consumed_closer = True
            ci += 1
            continue
        if ch.isspace():
            ci += 1
            continue
        break
    rest = lines[li].text[ci:].strip() if li < len(lines) else ""
    # 边界校验：真正的词条标题行要么到此结束，要么紧跟英文译名括号/交叉引用符；
    # 紧跟汉字说明只是命中了正文里的同形词（如“资本”命中“资本主义”）。
    if rest and rest[0] not in "(（☆" and not rest[0].isascii():
        return None
    if rest:
        return HeadingMatch(
            line_index=li,
            rest=rest,
            keep_full_line=False,
            body_has_outer_marks=opened_with_mark and consumed_closer,
        )
    return HeadingMatch(
        line_index=li + 1,
        rest="",
        keep_full_line=False,
        body_has_outer_marks=opened_with_mark and consumed_closer,
    )


def _match_person_heading(lines: list[Line], idx: int, title: str) -> bool:
    """人名词条：正文标题用全名（如“伊曼努尔·康德(Immanuel Kant)”），索引取惯用名。"""
    if not (2 <= len(title) <= 8) or not all(_is_cjk(ch) for ch in title):
        return False
    compact = re.sub(r"\s+", "", lines[idx].text)
    for dot in ("·", "・", "•"):
        pos = compact.find(dot + title)
        if pos < 0 or pos > 20:
            continue
        after = compact[pos + len(dot) + len(title):]
        if not after:
            # 全名独占一行（如“弗里德里希·威廉·约瑟夫·冯·谢林”），
            # 外文译名括号折到下一行。
            nxt = lines[idx + 1].text.strip() if idx + 1 < len(lines) else ""
            if nxt[:1] in ("(", "（") and re.search(r"[A-Za-z]{2}", nxt):
                return True
            continue
        if after[:1] not in ("(", "（"):
            continue
        # 标题行应在译名括号后结束（或紧跟生卒年括号）；正文行文里提到的
        # “卡尔·马克思(1818—1883)是……”后面还有内容，不算标题。
        close = min((i for i in (after.find(")"), after.find("）")) if i >= 0), default=-1)
        if close >= 0:
            remainder = after[close + 1:]
            if remainder and remainder[0] not in "(（":
                continue
        else:
            if not re.search(r"[A-Za-z]{2}", after[1:]):
                continue
        return True
    return False


def _match_midline_heading(line: str, title: str) -> tuple[bool, str]:
    """标题混入上一行尾部时的兜底：限定出现在行首附近且后跟括号。"""
    compact_line = re.sub(r"\s+", "", line)
    compact_title = re.sub(r"\s+", "", title)
    if len(compact_title) < 3:
        return False, ""
    pos = compact_line.find(compact_title)
    if pos <= 0 or pos > 24:
        return False, ""
    rest = compact_line[pos + len(compact_title):]
    if not rest or rest[0] not in "（(":
        return False, ""
    consumed = 0
    cut = 0
    while cut < len(line) and consumed < pos + len(compact_title):
        if not line[cut].isspace():
            consumed += 1
        cut += 1
    return True, line[cut:].strip()


def _has_english_evidence(lines: list[Line], match: HeadingMatch) -> bool:
    """真正的词条标题后面紧跟外文译名括号；正文里的同形词没有。"""
    if match.keep_full_line:
        return True  # 人名规则本身已校验译名括号
    if match.rest:
        probe = match.rest[:90]
        return probe[:1] in "(（" and bool(re.search(r"[A-Za-z]{2}", probe))
    if match.line_index < len(lines):
        nxt = lines[match.line_index].text.strip()
        return nxt[:1] in "(（" and bool(re.search(r"[A-Za-z]{2}", nxt[:90]))
    return False


def _candidate_matches(lines: list[Line], title: str):
    for idx in range(len(lines)):
        match = _match_heading_from(lines, idx, title)
        if match is not None:
            yield match
            continue
        if _match_person_heading(lines, idx, title):
            yield HeadingMatch(
                line_index=idx, rest=lines[idx].text, keep_full_line=True, body_has_outer_marks=False
            )
            continue
        ok, rest = _match_midline_heading(lines[idx].text, title)
        if ok:
            yield HeadingMatch(
                line_index=idx, rest=rest, keep_full_line=False, body_has_outer_marks=False
            )


def _find_entry_start(lines: list[Line], title: str) -> HeadingMatch | None:
    fallback: HeadingMatch | None = None
    for match in _candidate_matches(lines, title):
        if _has_english_evidence(lines, match):
            return match
        if fallback is None:
            fallback = match
    return fallback


def _find_next_heading(
    content_pairs: list[tuple[int, Line]],
    start_idx: int,
    nearby: list[IndexEntry],
    current_title: str,
) -> int:
    lines = [line for _, line in content_pairs]
    for idx in range(start_idx, len(content_pairs)):
        page = content_pairs[idx][0]
        for item in nearby:
            # 词条标题必然出现在它在索引中登记的起始页上，跨页命中都是正文引用；
            # 且标题后必有外文译名括号，正文里的同形词没有。
            if item.title == current_title or item.start_page != page:
                continue
            match = _match_heading_from(lines, idx, item.title)
            if match is not None and _has_english_evidence(lines, match):
                return idx
            if _match_person_heading(lines, idx, item.title):
                return idx
    return -1


def _join_content_lines(lines: list[Line]) -> str:
    paragraphs: list[str] = []
    current = ""
    for item in lines:
        text = SPACE_BETWEEN_CJK_RE.sub("", item.text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if item.para_start and current:
            paragraphs.append(current)
            current = text
            continue
        if current:
            if (
                current.endswith("-")
                and len(current) >= 2
                and current[-2].isascii()
                and current[-2].isalpha()
                and text[0].isascii()
                and text[0].isalpha()
            ):
                # 行尾断词连字符：标成软连字符，润色阶段整体删除。
                current = current[:-1] + SOFT_HYPHEN + text
            else:
                sep = (
                    " "
                    if current[-1].isascii() and current[-1].isalnum() and text[0].isascii() and text[0].isalnum()
                    else ""
                )
                current = current + sep + text
        else:
            current = text
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def _polish_paragraph(text: str) -> tuple[str, list[str]]:
    issues: list[str] = []
    text = text.replace(SOFT_HYPHEN, "")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("　", " ").replace("\xa0", " ")
    text = BAD_CHAR_RE.sub("", text)
    text = re.sub(r"[⋯]{2,}", "", text)
    text = re.sub(r"([，。；：！？、])\s+", r"\1", text)
    text = re.sub(r"\s+([，。；：！？、])", r"\1", text)
    text = SPACE_BETWEEN_CJK_RE.sub("", text)
    text = re.sub(r"(?<=[A-Za-z])-\s+(?=[A-Za-z])", "", text)
    if "??" in text:
        text = re.sub(r"(?<=\d)\?\?(?=\d)", "%—", text)
        text = re.sub(r"(?<=\d)\?\?", "%", text)
        text = text.replace("??", "")
        issues.append("auto_fix:question_marks")
    text = re.sub(r"(?<=[一-鿿”])\s*,\s*(?=[一-鿿“])", "，", text)
    text = re.sub(r"(?<=[一-鿿”])\s*;\s*(?=[一-鿿“])", "；", text)
    text = re.sub(r"(?<=[一-鿿”])\s*:\s*(?=[一-鿿“])", "：", text)
    text = re.sub(r"(?<=[一-鿿”])\s*!\s*", "！", text)
    text = re.sub(r"(?<=[一-鿿”])\s*\?\s*", "？", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text, issues


def _polish_text(text: str) -> tuple[str, list[str]]:
    issues: list[str] = []
    paragraphs: list[str] = []
    for paragraph in text.split("\n\n"):
        polished, para_issues = _polish_paragraph(paragraph)
        issues.extend(para_issues)
        if polished:
            paragraphs.append(polished)
    text = "\n\n".join(paragraphs)
    replacements = {
        "马思主义": "马克思主义",
        "马克恩": "马克思",
        "恩格期": "恩格斯",
        "列宇": "列宁",
        "毛泽乐": "毛泽东",
        "中因": "中国",
        "社会主文": "社会主义",
        "共产主文": "共产主义",
    }
    for wrong, right in replacements.items():
        if wrong in text:
            text = text.replace(wrong, right)
            issues.append(f"auto_fix:{wrong}->{right}")
    if BAD_CHAR_RE.search(text):
        issues.append("bad_char")
    if re.search(r"[A-Za-z]{18,}", text):
        issues.append("long_latin_run")
    # 单个 ⋯ 是公式记号（如 G—W⋯P⋯W′—G′），连续两个以上才算目录导点残留。
    if "⋯⋯" in text or "词目拼音索引" in text or "马克思主义大辞典" in text[:40]:
        issues.append("noise_residue")
    return text, sorted(set(issues))


def _extract_english(content: str) -> tuple[str, str]:
    """把正文开头括号里的外文译名抽出来（年份括号保留在正文中）。

    译名括号可能嵌套（“联共(布)…(the C.P.S.U.(B))”）或被 OCR 打成
    〔〕等残破符号，所以按深度扫描，扫到第一个汉字即视为译名结束。
    """
    pos = 0
    while True:
        starts = [i for i in (content.find("(", pos), content.find("（", pos)) if i >= 0]
        if not starts:
            return "", content
        start = min(starts)
        if start > 48:
            return "", content
        depth = 1
        j = start + 1
        end = -1
        while j < len(content):
            ch = content[j]
            if _is_cjk(ch):
                break
            if ch in "(（":
                depth += 1
            elif ch in ")）":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
            j += 1
        if end < 0:
            # 括号没有正常闭合：截到汉字前最后一个闭合符（OCR 把 ) 打成 〕等）。
            span = content[start:j]
            last = max(span.rfind(c) for c in ")）〕]")
            if last <= 0:
                pos = start + 1
                continue
            end = start + last + 1
        inner = content[start + 1 : end - 1].strip()
        if not re.search(r"[A-Za-z]{2}", inner):
            pos = end
            continue
        latin = sum(1 for ch in inner if ord(ch) < 0x2E80)
        if latin / max(1, len(inner)) < 0.7:
            pos = end
            continue
        english = re.sub(r"\s+", " ", inner).strip(" 〔〕[]")
        if english.count("(") == english.count(")") + 1:
            english += ")"
        content = (content[:start] + content[end:]).strip()
        return english, content
    return "", content


def _repair_title_brackets(title: str) -> tuple[str, bool, bool]:
    """修复 OCR 打乱的书名号嵌套：内层一律 〈〉，配对失败返回 unbalanced=True。"""
    stack: list[tuple[int, str]] = []
    pairs: list[tuple[int, int, str, str]] = []
    for idx, ch in enumerate(title):
        if ch in _OPEN_BRACKETS:
            stack.append((idx, ch))
        elif ch in _CLOSE_BRACKETS:
            if not stack:
                return title, False, True
            open_idx, open_ch = stack.pop()
            pairs.append((open_idx, idx, open_ch, ch))
    if stack:
        return title, False, True
    chars = list(title)
    changed = False
    for open_idx, close_idx, open_ch, close_ch in pairs:
        depth = sum(1 for i, j, _, _ in pairs if i < open_idx and j > close_idx)
        mismatched = (open_ch, close_ch) not in _PAIR_OK
        plain_paren = open_ch in "(（" and close_ch in ")）"
        if depth == 0:
            if mismatched:
                if open_ch == "《" or close_ch == "》":
                    repl = ("《", "》")
                elif open_ch in "(（" or close_ch in ")）":
                    repl = ("(", ")")
                else:
                    repl = ("〈", "〉")
                chars[open_idx], chars[close_idx] = repl
                changed = True
            continue
        if plain_paren and not mismatched:
            continue
        if chars[open_idx] != "〈" or chars[close_idx] != "〉":
            chars[open_idx], chars[close_idx] = "〈", "〉"
            changed = True
    return "".join(chars), changed, False


def _extract_entry_content(
    entry: IndexEntry, pages: dict[int, list[Line]], title_catalog: list[IndexEntry]
) -> tuple[str, str, int, float, list[str], bool]:
    page_limit = min(entry.start_page + MAX_ENTRY_PAGES, LAST_CONTENT_PRINTED_PAGE)

    def _collect(first_page: int) -> list[tuple[int, Line]]:
        pairs: list[tuple[int, Line]] = []
        for page in range(first_page, page_limit + 1):
            for line in _strip_noise_lines(pages.get(page, [])):
                pairs.append((page, line))
        return pairs

    marked_lines = _collect(entry.start_page)
    lines = [line for _, line in marked_lines]
    nearby = [
        item
        for item in title_catalog
        if entry.start_page <= item.start_page <= page_limit
    ]
    match = _find_entry_start(lines, entry.title)
    if match is None and entry.start_page > 2:
        # 词条标题偶尔排在前一页页脚：仅当命中点落在前一页尾部时才采用。
        prev_count = len(_strip_noise_lines(pages.get(entry.start_page - 1, [])))
        alt_lines_pairs = _collect(entry.start_page - 1)
        alt_match = _find_entry_start([line for _, line in alt_lines_pairs], entry.title)
        if alt_match is not None and alt_match.line_index >= prev_count - 12:
            marked_lines = alt_lines_pairs
            lines = [line for _, line in marked_lines]
            match = alt_match
    confidence = 0.55
    issues: list[str] = []
    body_has_outer_marks = False
    if match is not None:
        confidence += 0.25
        body_has_outer_marks = match.body_has_outer_marks
        content_pairs = marked_lines[match.line_index:]
        if content_pairs:
            first_text = match.rest if (match.rest and not match.keep_full_line) else content_pairs[0][1].text
            content_pairs[0] = (content_pairs[0][0], Line(text=first_text, para_start=True))
    else:
        content_pairs = marked_lines
        # 找不到标题时，从第一个段首行起步，甩掉上一词条跨页的残尾句。
        first_para = next(
            (i for i, (_, line) in enumerate(content_pairs) if line.para_start), 0
        )
        content_pairs = content_pairs[first_para:]
        issues.append("title_not_found")

    next_idx = _find_next_heading(content_pairs, 1, nearby, entry.title)
    if next_idx >= 0:
        content_pairs = content_pairs[:next_idx]
        confidence += 0.15
    else:
        issues.append("next_heading_not_found")

    content = _join_content_lines([line for _, line in content_pairs])
    content, polish_issues = _polish_text(content)
    issues.extend(polish_issues)

    english, content = _extract_english(content)

    if CROSS_REF_RE.match(content):
        issues.append("cross_reference")
    elif len(content) < 60:
        issues.append("short_content")

    if content_pairs:
        end_printed = max(page for page, _ in content_pairs)
    else:
        end_printed = entry.start_page
    if end_printed >= entry.start_page + MAX_ENTRY_PAGES and next_idx < 0:
        end_printed = entry.start_page + MAX_ENTRY_PAGES
        issues.append("long_entry_capped")
    confidence = min(0.99, confidence)
    if issues:
        confidence = min(confidence, 0.78)
    return content, english, end_printed, confidence, sorted(set(issues)), body_has_outer_marks


_REVIEW_ISSUES = {
    "title_not_found",
    "bad_char",
    "noise_residue",
    "long_entry_capped",
    "short_content",
    "title_bracket_unbalanced",
}


def _slugify(title: str, start_page: int, used: set[str]) -> str:
    base = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", title).strip("-").lower()
    if not base:
        base = "entry"
    slug = f"{base}-{start_page}"
    if slug not in used:
        used.add(slug)
        return slug
    n = 2
    while f"{slug}-{n}" in used:
        n += 1
    final = f"{slug}-{n}"
    used.add(final)
    return final


def _norm_term(text: str) -> str:
    return re.sub(r"""[\s·.,，。、；;：:！!？?（）()《》〈〉<>\[\]【】"'“”‘’\-—_]+""", "", text).lower()


def _citation(start: int, end: int) -> str:
    page_text = f"第 {start} 页" if start == end else f"第 {start}-{end} 页"
    return f"徐光春主编：《马克思主义大辞典》，武汉：崇文书局，2017年，{page_text}。"


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS entries;
        DROP TABLE IF EXISTS metadata;
        CREATE TABLE entries (
          slug TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          norm_title TEXT NOT NULL,
          letter TEXT NOT NULL,
          english TEXT,
          content TEXT NOT NULL,
          start_page INTEGER NOT NULL,
          end_page INTEGER NOT NULL,
          citation TEXT NOT NULL,
          source_file TEXT NOT NULL,
          sort_order INTEGER NOT NULL,
          confidence REAL NOT NULL,
          needs_review INTEGER NOT NULL DEFAULT 0,
          issues TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX idx_entries_letter_sort ON entries(letter, sort_order);
        CREATE INDEX idx_entries_norm ON entries(norm_title);
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )


def build_dictionary(db_path: Path, report_path: Path) -> dict:
    upper_pdf, lower_pdf = _find_dictionary_pdfs(ROOT / "pdfs")
    entries = _parse_index_entries(lower_pdf)
    if len(entries) < 500:
        raise SystemExit(f"词目拼音索引解析过少，仅 {len(entries)} 条。")
    pages = _extract_printed_pages(upper_pdf, lower_pdf)
    used_slugs: set[str] = set()
    rows = []
    report_items = []
    title_changes = []
    english_count = 0
    for entry in entries:
        content, english, end_page, confidence, issues, body_marks = _extract_entry_content(
            entry, pages, entries
        )
        title = entry.title
        if body_marks and not title.startswith("《"):
            title = f"《{title}》"
            title_changes.append(
                {"page": entry.start_page, "old": entry.title, "new": title, "reason": "body_has_book_marks"}
            )
        repaired, changed, unbalanced = _repair_title_brackets(title)
        if unbalanced:
            issues = sorted(set(issues + ["title_bracket_unbalanced"]))
        elif changed:
            title_changes.append(
                {"page": entry.start_page, "old": title, "new": repaired, "reason": "bracket_repair"}
            )
            title = repaired
        if english:
            english_count += 1
        source_key, _ = _page_to_pdf_index(entry.start_page)
        source_file = str(upper_pdf if source_key == "upper" else lower_pdf)
        needs_review = bool(_REVIEW_ISSUES.intersection(issues))
        slug = _slugify(title, entry.start_page, used_slugs)
        rows.append(
            (
                slug,
                title,
                _norm_term(title),
                entry.letter,
                english,
                content,
                entry.start_page,
                end_page,
                _citation(entry.start_page, end_page),
                source_file,
                entry.sort_order,
                confidence,
                1 if needs_review else 0,
                json.dumps(issues, ensure_ascii=False),
            )
        )
        if needs_review:
            report_items.append(
                {
                    "title": title,
                    "page": entry.start_page,
                    "confidence": confidence,
                    "issues": issues,
                    "preview": content[:120],
                }
            )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".sqlite.tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        _init_db(conn)
        conn.executemany(
            """
            INSERT INTO entries
            (slug, title, norm_title, letter, english, content, start_page, end_page,
             citation, source_file, sort_order, confidence, needs_review, issues)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            [
                ("entry_count", str(len(rows))),
                ("upper_pdf", upper_pdf.name),
                ("lower_pdf", lower_pdf.name),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    tmp.replace(db_path)
    report = {
        "entry_count": len(rows),
        "needs_review_count": len(report_items),
        "english_count": english_count,
        "upper_pdf": upper_pdf.name,
        "lower_pdf": lower_pdf.name,
        "title_changes": title_changes,
        "items": report_items[:500],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    _stdout_utf8()
    parser = argparse.ArgumentParser(description="Build the Marxism dictionary SQLite index.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    report = build_dictionary(args.db, args.report)
    print(
        f"Dictionary index built: {args.db} "
        f"({report['entry_count']} entries, {report['needs_review_count']} review candidates, "
        f"{report['english_count']} english names, {len(report['title_changes'])} title fixes)"
    )
    print(f"Polish report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
