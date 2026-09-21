"""Local, provenance-preserving cleanup for AI evidence and rendered answers.

No corpus writes or model calls. Offsets always refer to the supplied raw text;
unsafe deletions split quote_segments so missing OCR text cannot be glued into
a newly 'verbatim' quotation. Known page furniture may join across page breaks.
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache


_EMAIL = r"[A-Za-z0-9_.+\-]+\s*[@＠]\s*[A-Za-z0-9\-]+(?:\s*[.．]\s*[A-Za-z0-9\-]+)+"
_URL = r"(?:https?://|www\.)[A-Za-z0-9./_?=&%#\-]+"
_S22 = r"S\s*2\s*2\s*P\s*D\s*F"
PDF_WATERMARK_RE = re.compile(
    rf"(?:本\s*)?PDF\s*文件\s*由\s*{_S22}\s*生成\s*[,，]?\s*|"
    rf"{_S22}\s*的版权由\s*郭力\s*所有\s*|"
    r"pdf\s*[@＠]\s*home\s*[.．]\s*icm\s*[.．]\s*ac\s*[.．]\s*cn|"
    r"PDF\s*文件使用\s*[“\"']?\s*pdfFactory\s*Pro\s*[”\"']?\s*试用版本创建\s*|"
    r"(?:https?://)?(?:www\.)?(?:fineprint\.com\.cn|mzdbl\.cn)/?",
    re.I,
)
_CONTACT = re.compile(
    rf"(?:(?:本)?PDF\s*)?(?:电子邮箱|联系邮箱|邮箱|投稿信箱|技术支持|资料下载|下载地址|E-?mail)"
    rf"[：:\s]*(?:{_EMAIL}|{_URL})", re.I,
)
_CONTACT_LINE = re.compile(rf"\s*(?:{_EMAIL}|{_URL})\s*", re.I)
_PAGE = re.compile(r"[-—–]?\s*[0-9０-９]{1,4}\s*[-—–]?")
_VOLUME = re.compile(r"第?[〇○零一二三四五六七八九十百千0-9０-９]+[卷册輯辑部]")
_YEARS = re.compile(r"[（(]?[〇○零一二三四五六七八九十0-9０-９\s—–\-―→←]+[）)]?")
_NOTE_START = r"[①-⑳]?\s*(?:手稿|原稿)中\s*(?:删去|刪去|删除|刪除|加上|写有|寫有|插入|改为|改為)"
_NOTE_END = r"(?:编者|編者|译者|譯者|校者)\s*(?:注|按)"
_NOTE_SPAN = re.compile(_NOTE_START + r".*?" + _NOTE_END, re.S)
_NOTE_MARK = re.compile(_NOTE_START + "|" + _NOTE_END)
_NUMBER = re.compile(r"(?<![0-9０-９])([0-9０-９]{2,3})(?![0-9０-９])(?=\s*[\u3400-\u9fff])")
_UNITS = frozenset("年月日时分秒世纪个名位条项章节卷册页人元角分倍种类篇部款期届级小米吨斤亩度％%万亿岁点成天次组所家套件份公里两多余")
_LEADS = frozenset("第约近超逾增减各每共达为有从至到以按占比是及和与余剩")
_BAD = frozenset("�□■◆◇●○¤")
_CJK_JOIN = re.compile(r"(?<=[\u3400-\u9fff，。；：！？、“”‘’《》])[ \t]+(?=[\u3400-\u9fff，。；：！？、“”‘’《》])")


@dataclass(frozen=True)
class CleanedEvidence:
    text: str
    source_positions: tuple[int, ...]
    quote_segments: tuple[str, ...]
    issues: tuple[str, ...]

    def map_focus(self, focus: tuple[int, int]) -> tuple[int, int]:
        return (bisect_left(self.source_positions, focus[0]),
                bisect_left(self.source_positions, focus[1]))


def _label(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value or "")) if c.isalnum())


def clean_evidence(text: object, metadata: dict | None = None, *, markdown: bool = False,
                   furniture_only: bool = False, preserve_lines: bool = False) -> CleanedEvidence:
    """Clean text once while retaining raw offsets and safe quotation boundaries.

    Markdown mode preserves layout and code; unknown damaged wording is dropped
    only within its sentence, never repaired by guessing the missing character.
    """
    source = str(text or "")
    metadata = metadata or {}
    removed = bytearray(len(source))
    unsafe: list[tuple[int, int]] = []
    issues: list[str] = []
    protected = bytearray(len(source))
    lines = list(re.finditer(r"[^\n]*(?:\n|$)", source))
    fenced = False
    for row in lines:
        s = row.group().strip()
        if markdown and re.match(r"^(?:`{3,}|~{3,})", s):
            fenced = not fenced
            protected[row.start():row.end()] = b"\1" * (row.end() - row.start())
        elif markdown and fenced:
            protected[row.start():row.end()] = b"\1" * (row.end() - row.start())
    if markdown:
        for m in re.finditer(r"`[^`\n]+`", source):
            protected[m.start():m.end()] = b"\1" * (m.end() - m.start())

    def drop(start: int, end: int, issue: str, *, barrier: bool = False) -> None:
        if start >= end or any(protected[start:end]):
            return
        removed[start:end] = b"\1" * (end - start)
        if barrier:
            unsafe.append((start, end))
        issues.append(issue)

    labels = {_label(metadata.get(k)) for k in ("book", "book_title", "display_title")}
    labels.update(_label(value) for value in metadata.get("_ai_page_headers", []))
    labels.discard("")
    pages, titles = set(), set()
    for i, row in enumerate(lines):
        line = row.group().strip()
        value = _label(line)
        if _PAGE.fullmatch(line):
            number = int(unicodedata.normalize("NFKC", line).strip("-—– "))
            next_line = lines[i + 1].group().lstrip() if i + 1 < len(lines) else ""
            # A year or a quantity whose unit wrapped to the next physical line
            # is not sufficient evidence of a page footer.
            if not (1500 <= number <= 2099 or next_line[:1] in _UNITS):
                pages.add(i)
        if value and any(value == label or (value.startswith(label) and _VOLUME.fullmatch(value[len(label):])) for label in labels):
            titles.add(i)
    for i, row in enumerate(lines):
        line = row.group().strip()
        if i in pages | titles or (
            any(abs(i - a) <= 3 for a in pages | titles)
            and _YEARS.fullmatch(line) and any(c in line for c in "—–-―→←")
        ) or (_VOLUME.fullmatch(line) and any(abs(i - a) <= 2 for a in titles)):
            drop(row.start(), row.end(), "page_furniture_removed")
        if not furniture_only and _CONTACT_LINE.fullmatch(line):
            drop(row.start(), row.end(), "contact_metadata_removed")

    if not furniture_only:
        for rx, issue in ((PDF_WATERMARK_RE, "pdf_watermark_removed"), (_CONTACT, "contact_metadata_removed")):
            for m in rx.finditer(source):
                drop(m.start(), m.end(), issue)
        for m in _NOTE_SPAN.finditer(source):
            drop(m.start(), m.end(), "editorial_note_removed", barrier=True)
        for m in _NOTE_MARK.finditer(source):
            if removed[m.start()]:
                continue
            start = max(source.rfind("。", 0, m.start()) + 1, source.rfind("\n", 0, m.start()) + 1)
            end = source.find("\n", m.end()) if markdown else len(source)
            if end < 0:
                end = len(source)
            drop(start, end, "editorial_note_removed", barrier=True)
        for m in _NUMBER.finditer(source):
            if removed[m.start()]:
                continue
            before = source[source.rfind("\n", 0, m.start()) + 1:m.start()].rstrip()[-1:]
            after = source[m.end():].lstrip()[:1]
            if before in _LEADS or after in _UNITS or (before and (before.isdigit() or before in ".．/:：—–-%,，")):
                continue
            prefix = source[max(0, m.start() - 16):m.start()]
            if re.search(r"(?:收入|产量|数量|数值|指数|金额|价格|编号|编码|序号|数目|得分|增长率)\s*$", prefix):
                continue
            at_line_start = not source[source.rfind("\n", 0, m.start()) + 1:m.start()].strip()
            adjacent_page = any(abs(m.start() - lines[p].end()) < 12 for p in pages)
            if not ((at_line_start and adjacent_page) or (before and before.isalpha() and source[m.end():m.end() + 1].isspace())):
                continue
            drop(m.start(), m.end(), "inline_note_number_removed")
        for m in re.finditer(r"[①-⑳]|〔\s*\d{1,3}\s*〕|<{2,}|(?<!^)>[>]+", source, re.M):
            drop(m.start(), m.end(), "editorial_marker_removed")
        for i, ch in enumerate(source):
            if removed[i] or protected[i]:
                continue
            cat = unicodedata.category(ch)
            if ch in _BAD or (cat in {"Cc", "Co", "Cs", "Cn"} and not ch.isspace()):
                # A glyph inside a word may represent missing author text. Drop
                # that sentence and put a quotation barrier at the deletion.
                start = max(source.rfind("。", 0, i) + 1, source.rfind("！", 0, i) + 1,
                            source.rfind("？", 0, i) + 1, source.rfind("\n", 0, i) + 1)
                ends = [p for p in (source.find(c, i + 1) for c in "。！？\n") if p >= 0]
                end = min(ends) + 1 if ends else len(source)
                if markdown:
                    suffix = re.match(r'[”」』\" \t]*(?:\[\d+\][ \t]*)*', source[end:])
                    end += len(suffix.group()) if suffix else 0
                drop(start, end, "damaged_sentence_removed", barrier=True)
            elif cat == "Cf":
                drop(i, i + 1, "invisible_character_removed")

    chars, positions = [], []
    for i, ch in enumerate(source):
        if not removed[i]:
            chars.append(ch)
            positions.append(i)
    cleaned = "".join(chars)

    def remove_matches(rx: re.Pattern, keep_space: bool = False) -> None:
        nonlocal cleaned, positions
        keep = bytearray(b"\1" * len(cleaned))
        for m in rx.finditer(cleaned):
            for i in range(m.start() + int(keep_space), m.end()):
                keep[i] = 0
        cleaned = "".join(c for i, c in enumerate(cleaned) if keep[i])
        positions = [p for i, p in enumerate(positions) if keep[i]]

    if not furniture_only:
        if not markdown:
            cleaned = "".join(" " if c.isspace() and not (preserve_lines and c == "\n") else c for c in cleaned)
            remove_matches(re.compile(r" +"), True)
            remove_matches(_CJK_JOIN)
        # Unpaired OCR quote marks are not repaired in generated prose: the
        # quotation parser, not the cleaner, owns Markdown quotation structure.
        if not markdown:
            orphaned = set()
            for opening, closing in (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』")):
                stack = []
                for i, c in enumerate(cleaned):
                    if c == opening:
                        stack.append(i)
                    elif c == closing:
                        if stack:
                            stack.pop()
                        else:
                            orphaned.add(i)
                orphaned.update(stack)
            cleaned = "".join(c for i, c in enumerate(cleaned) if i not in orphaned)
            positions = [p for i, p in enumerate(positions) if i not in orphaned]

    left = len(cleaned) - len(cleaned.lstrip())
    right = len(cleaned.rstrip())
    cleaned, positions = cleaned[left:right], positions[left:right]
    breaks = sorted({bisect_left(positions, end) for _start, end in unsafe})
    boundaries = [0, *breaks, len(cleaned)]
    segments = tuple(cleaned[a:b].strip() for a, b in zip(boundaries, boundaries[1:]) if cleaned[a:b].strip())
    return CleanedEvidence(cleaned, tuple(positions), segments, tuple(dict.fromkeys(issues)))


_QUOTE_TRANSLATION = str.maketrans("“”‘’「」『』", '\"\"\'\'\"\"\'\'')


@lru_cache(maxsize=128)
def _quote_map(value: str):
    chars, offsets = [], []
    for pos, c in enumerate(value):
        for folded in unicodedata.normalize("NFKC", c):
            if not folded.isspace():
                chars.append(folded.translate(_QUOTE_TRANSLATION))
                offsets.append(pos)
    return "".join(chars), tuple(offsets)


def exact_quote(quote: str, source: str) -> str:
    """Match a contiguous excerpt, preserving punctuation and letter case.

    Only physical whitespace, width and typographic quote variants are folded;
    punctuation is never discarded (in particular, ellipses cannot hide cuts).
    Return the source's literal span, not an expanded sentence.
    """
    needle, _ = _quote_map(quote)
    haystack, offsets = _quote_map(source)
    if len(needle) < 2:
        return ""
    at = haystack.find(needle)
    return source[offsets[at]:offsets[at + len(needle) - 1] + 1] if at >= 0 else ""
