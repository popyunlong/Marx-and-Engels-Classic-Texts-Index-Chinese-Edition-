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

LEADER_RE = re.compile(r"[⋯·\.。．]{2,}")
PAGE_RE = re.compile(r"(?P<title>.*?)[⋯·\.。．\s]+(?P<page>\d{1,4})(?:[、,，]\d{1,4})?\s*$")
HEADER_RE = re.compile(
    r"^(?:\d+\s*)?马克思主义大辞典$|^词目拼音索引(?:\s+\d+)?$|^\d{1,4}$|^第[一二三四五六七八九十]+编"
)
LETTER_RE = re.compile(r"^[A-Z]$")
BAD_CHAR_RE = re.compile(r"[�□■◆◇]")
SPACE_BETWEEN_CJK_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


@dataclass
class IndexEntry:
    title: str
    letter: str
    start_page: int
    sort_order: int


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
    try:
        for page_no in range(INDEX_START_PAGE, doc.page_count + 1):
            lines = doc.load_page(page_no - 1).get_text("text").replace("\r", "").splitlines()
            for raw in lines:
                line = raw.strip()
                if not line:
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
                    title_parts = pending + [match.group("title")]
                    pending.clear()
                    title = _clean_index_title("".join(title_parts))
                    if not title or len(title) > 80:
                        continue
                    sort_order += 1
                    entries.append(
                        IndexEntry(
                            title=title,
                            letter=current_letter,
                            start_page=int(match.group("page")),
                            sort_order=sort_order,
                        )
                    )
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


def _extract_printed_pages(upper_pdf: Path, lower_pdf: Path) -> dict[int, str]:
    pages: dict[int, str] = {}
    for volume, path in (("upper", upper_pdf), ("lower", lower_pdf)):
        doc = fitz.open(path)
        try:
            for printed in range(2, 1421):
                which, pdf_page = _page_to_pdf_index(printed)
                if which != volume or pdf_page < 1 or pdf_page > doc.page_count:
                    continue
                pages[printed] = doc.load_page(pdf_page - 1).get_text("text").replace("\r", "")
        finally:
            doc.close()
    return pages


def _strip_noise_lines(text: str) -> list[str]:
    result: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
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
        result.append(line)
    return result


def _normalize_title_for_find(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _heading_match(line: str, title: str) -> tuple[bool, str]:
    compact_line = _normalize_title_for_find(line)
    compact_title = _normalize_title_for_find(title)
    candidates = [compact_title]
    stripped = compact_title.strip("《》“”")
    if stripped != compact_title:
        candidates.append(stripped)
    for candidate in candidates:
        if not candidate or not compact_line.startswith(candidate):
            if len(candidate) < 3:
                continue
            pos = compact_line.find(candidate)
            if pos <= 0 or pos > 24:
                continue
            rest = compact_line[pos + len(candidate):]
            if not rest or rest[0] not in "（(":
                continue
            consumed = 0
            cut = 0
            while cut < len(line) and consumed < pos + len(candidate):
                if not line[cut].isspace():
                    consumed += 1
                cut += 1
            return True, line[cut:].strip()
        rest = compact_line[len(candidate):]
        if rest and _is_cjk(rest[0]):
            # Avoid matching “资本” against “资本主义...” or “矛盾” against “矛盾规律...”.
            continue
        consumed = 0
        cut = 0
        while cut < len(line) and consumed < len(candidate):
            if not line[cut].isspace():
                consumed += 1
            cut += 1
        return True, line[cut:].strip()
    return False, ""


def _find_entry_start(lines: list[str], title: str) -> tuple[int, str]:
    for idx, line in enumerate(lines):
        matched, rest = _heading_match(line, title)
        if matched:
            return idx, rest
    return -1, ""


def _find_next_heading(lines: list[str], start_idx: int, titles: list[str], current_title: str) -> int:
    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        for title in titles:
            if title == current_title:
                continue
            matched, _ = _heading_match(line, title)
            if matched:
                return idx
    return -1


def _join_content_lines(lines: list[str]) -> str:
    paragraphs: list[str] = []
    current = ""
    for line in lines:
        line = SPACE_BETWEEN_CJK_RE.sub("", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if current and re.search(r"[。！？；：]$|[.!?;:]$", current) and len(current) > 120:
            paragraphs.append(current)
            current = line
        else:
            if current:
                sep = " " if current[-1].isascii() and current[-1].isalnum() and line[0].isascii() and line[0].isalnum() else ""
                current = current + sep + line
            else:
                current = line
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def _polish_text(text: str) -> tuple[str, list[str]]:
    issues: list[str] = []
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
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
    text = re.sub(r"(?<=[\u4e00-\u9fff”])\s*,\s*(?=[\u4e00-\u9fff“])", "，", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff”])\s*;\s*(?=[\u4e00-\u9fff“])", "；", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff”])\s*:\s*(?=[\u4e00-\u9fff“])", "：", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff”])\s*!\s*", "！", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff”])\s*\?\s*", "？", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
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
    if len(text) < 60:
        issues.append("short_content")
    if "⋯" in text or "词目拼音索引" in text or "马克思主义大辞典" in text[:40]:
        issues.append("noise_residue")
    return text, issues


def _extract_entry_content(entry: IndexEntry, pages: dict[int, str], title_catalog: list[IndexEntry]) -> tuple[str, int, float, list[str]]:
    page_limit = min(entry.start_page + MAX_ENTRY_PAGES, 1420)
    marked_lines: list[tuple[int, str]] = []
    for page in range(entry.start_page, page_limit + 1):
        for line in _strip_noise_lines(pages.get(page, "")):
            marked_lines.append((page, line))
    lines = [line for _, line in marked_lines]
    nearby_titles = [
        item.title
        for item in title_catalog
        if entry.start_page <= item.start_page <= page_limit
    ]
    start_idx, rest = _find_entry_start(lines, entry.title)
    confidence = 0.55
    issues: list[str] = []
    if start_idx >= 0:
        confidence += 0.25
        content_pairs = marked_lines[start_idx:]
        if rest:
            content_pairs[0] = (content_pairs[0][0], rest)
        else:
            content_pairs = content_pairs[1:]
    else:
        content_pairs = marked_lines
        issues.append("title_not_found")

    next_idx = _find_next_heading([line for _, line in content_pairs], 1, nearby_titles, entry.title)
    if next_idx >= 0:
        content_pairs = content_pairs[:next_idx]
        confidence += 0.15
    else:
        issues.append("next_heading_not_found")

    content_lines = [line for _, line in content_pairs]
    content = _join_content_lines(content_lines)
    polish_issues: list[str]
    content, polish_issues = _polish_text(content)
    issues.extend(polish_issues)
    if start_idx < 0 and "title_not_found" not in issues:
        issues.append("title_not_found")
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
    return content, end_printed, confidence, issues


def _slugify(title: str, start_page: int, used: set[str]) -> str:
    base = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", title).strip("-").lower()
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
    return re.sub(r"""[\s·.,，。、；;：:！!？?（）()《》<>\[\]【】"'“”‘’\-—_]+""", "", text).lower()


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
    for idx, entry in enumerate(entries):
        content, end_page, confidence, issues = _extract_entry_content(entry, pages, entries)
        source_key, _ = _page_to_pdf_index(entry.start_page)
        source_file = str(upper_pdf if source_key == "upper" else lower_pdf)
        needs_review = bool(issues or confidence < 0.8)
        slug = _slugify(entry.title, entry.start_page, used_slugs)
        rows.append(
            (
                slug,
                entry.title,
                _norm_term(entry.title),
                entry.letter,
                "",
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
                    "title": entry.title,
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
        "upper_pdf": upper_pdf.name,
        "lower_pdf": lower_pdf.name,
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
        f"({report['entry_count']} entries, {report['needs_review_count']} review candidates)"
    )
    print(f"Polish report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
