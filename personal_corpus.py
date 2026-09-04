from __future__ import annotations

import logging
import json
import os
import re
import sqlite3
import threading
import unicodedata
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median

from book_config import BookConfig
from build_index import detect_printed_page_from_text, normalize
from runtime_env import APPDATA_DIR
from search import Corpus, Page

import personal_library as plib

# 每用户的个人检索索引。**必须与检索同机**：本站检索不是 SQL 查询，而是在内存里对
# Corpus.norm_full 做字符串扫描，索引文件要能被跑检索的进程直接读。好在体积极小
# （300 页书的纯文本约几百 KB）。原始 PDF 与页图在另一台存储服务器上。
PERSONAL_INDEX_DIR = APPDATA_DIR / "personal_index"

# 生成规则版本。提升后，应用启动会只回填旧版本的 ready 书；无需用户重传，也不重跑 OCR。
INDEX_PIPELINE_VERSION = 22
LOGGER = logging.getLogger(__name__)


def assess_text_layer_quality(pages: list[dict]) -> dict:
    """用可解释、无外部服务的统计量识别“有文字但大面积乱码”。

    损坏的隐藏 OCR 层往往仍有大量汉字，因此只数字符/页数会误判为可用。
    正常连贯文本存在大量重复词组，单页 zlib 压缩率稳定较低；随机映射的汉字
    序列压缩率会显著升高。只在页数、文字量和汉字占比均足够时才判定，
    避免对短文档、表格或外文书误触发。
    """
    eligible: list[tuple[int, str]] = []
    total_han = 0
    total_visible = 0
    for item in pages:
        text = str(item.get("text") or "").strip()
        raw = text.encode("utf-8")
        if len(raw) < 500:
            continue
        visible = [char for char in text if not char.isspace()]
        han = sum(1 for char in visible if "\u3400" <= char <= "\u9fff")
        total_han += han
        total_visible += len(visible)
        try:
            pno = int(item.get("page") or 0)
        except (TypeError, ValueError):
            pno = 0
        eligible.append((pno, text))

    # 长书等距抽样，限制 CPU，同时覆盖书首、书中和书尾。
    if len(eligible) > 96:
        last = len(eligible) - 1
        indexes = sorted({round(i * last / 95) for i in range(96)})
        sampled = [eligible[index] for index in indexes]
    else:
        sampled = eligible
    ratios: list[float] = []
    for _, text in sampled:
        raw = text.encode("utf-8")
        ratios.append(len(zlib.compress(raw, 6)) / max(1, len(raw)))
    compression = float(median(ratios)) if ratios else 0.0
    han_ratio = total_han / max(1, total_visible)
    enough_evidence = len(eligible) >= 20 and total_han >= 10000 and han_ratio >= 0.45
    requires_ocr = bool(enough_evidence and compression >= 0.64)
    # 线上逐页原图复核表明 0.64—0.655 区间已经会出现连续的页码 O/0、l/1
    # 混淆和目录串行；旧切线让这种“字很多但不可可靠使用”的 ABBYY 隐藏层漏网。
    # 0.58 及以下仍视为高可读，中间区域只降低分数、不自动 OCR。
    confidence = 1.0 if not enough_evidence else max(
        0.0, min(1.0, (0.70 - compression) / 0.12)
    )
    return {
        "sampled_pages": len(sampled),
        "eligible_pages": len(eligible),
        "han_ratio": round(han_ratio, 3),
        "median_page_compression": round(compression, 3),
        "confidence": round(confidence, 3),
        "requires_ocr": requires_ocr,
    }

_TOC_RANGE_RE = re.compile(
    r"^(?P<title>.+?)(?:[·•∙⋯…\.\s]{2,}|[·•∙⋯…\.]+\s*)"
    r"(?P<start>[IVXLCDMivxlcdm\d]+)(?:\s*[-—–~～一至]+\s*[IVXLCDMivxlcdm\d]+)?\s*$"
)
_TOC_INLINE_RE = re.compile(
    r"^(?P<title>.+?)\s*(?:[（(]\s*)?(?P<start>[IVXLCDMivxlcdm\d]+)(?:\s*[）)])?"
    r"(?:\s*[-—–~～一至]+\s*[IVXLCDMivxlcdm\d]+)?\s*$"
)
_TOC_STANDALONE_PAGE_RE = re.compile(
    r"^(?:[（(]\s*)?(?P<start>[IVXLCDMivxlcdm\d]+)"
    r"(?:\s*[-—–~～一至]+\s*[IVXLCDMivxlcdm\d]+)?\s*(?:[）)])?[·•∙⋯…\.]*$"
)
_DATE_LINE_RE = re.compile(
    r"^[（(]?\s*[一二三四五六七八九十〇○O0零年月日、至\-—–\s]+[）)]?$"
)
_NUMERIC_TITLE_RE = re.compile(r"^[0-9IVXLCDMivxlcdm\s\-—–\.]+$")
_SCAN_ARTIFACT_RE = re.compile(
    r"(?i)(?:^[a-z]:[\\/])|[\\/].+\.(?:tif|tiff|jpe?g|png|bmp|gif)$|"
    r"\.(?:tif|tiff|jpe?g|png|bmp|gif)$"
)
_TOC_NOISE = {
    "目", "录", "目录", "图表目录", "插图目录", "表格目录", "图目录", "插图",
    "封面", "书名", "书名页", "版权", "版权页",
}
_TOC_TOP_LEVEL = {"致谢", "中文版前言", "前言", "序言", "导言", "引言", "绪论",
                  "结论", "后记", "附录", "注释", "参考文献", "索引"}
_TOC_HEADING_RE = re.compile(
    r"^(?:第?[一二三四五六七八九十百0-9]+[编部篇章节卷]|[0-9]{1,3}(?:[.、\s]|$))"
)
_COPYRIGHT_MARKERS = (
    "图书在版编目", "CIP", "版权所有", "版权页", "出版发行", "责任编辑",
    "字数", "印张", "版次", "印刷", "书号", "ISBN", "定价",
)

# 同时驻留内存的用户 Corpus 数量。每人约数 MB（纯文本），8 人上限即数十 MB。
_CACHE_MAX = 8
_CACHE: "OrderedDict[int, tuple[str, PersonalCorpus]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_INDEX_WRITE_LOCKS: dict[int, threading.RLock] = {}
_INDEX_WRITE_LOCKS_GUARD = threading.Lock()


def _index_write_lock(user_id: int) -> threading.RLock:
    """Serialize writes to one user's SQLite index while retaining cross-user concurrency."""
    uid = int(user_id)
    with _INDEX_WRITE_LOCKS_GUARD:
        lock = _INDEX_WRITE_LOCKS.get(uid)
        if lock is None:
            lock = threading.RLock()
            _INDEX_WRITE_LOCKS[uid] = lock
        return lock


def personal_book_key(submission_id: int) -> str:
    """个人书在检索体系中的书库键。前缀 mylib: 保证与任何官方书库键不可能相撞。"""
    return f"mylib:{int(submission_id)}"


def submission_id_from_key(book_key: str) -> int | None:
    if isinstance(book_key, str) and book_key.startswith("mylib:"):
        try:
            return int(book_key.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


def submission_id_from_scope_token(token: object) -> int | None:
    """解析“指定著作”控件导出的个人书 token。

    控件会把书库键 ``mylib:1`` 包成 ``book:mylib:1``；旧代码只认前者，导致用户明明
    勾选了个人书，后端却把它当成不存在的公共书库并返回零命中。
    """
    text = str(token or "").strip()
    if text.startswith("book:"):
        text = text[5:].strip()
    return submission_id_from_key(text)


def index_path(user_id: int) -> Path:
    return PERSONAL_INDEX_DIR / f"{int(user_id)}.sqlite"


def _connect(user_id: int) -> sqlite3.Connection:
    PERSONAL_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path(user_id), timeout=60.0)
    conn.execute("PRAGMA busy_timeout = 60000")
    # 与 build_index 的 pages/toc_entries 同构，故可被 Corpus 原样加载。
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book TEXT NOT NULL,
            volume INTEGER NOT NULL,
            source_file TEXT NOT NULL,
            pdf_page INTEGER NOT NULL,
            printed_page TEXT,
            raw_text TEXT NOT NULL,
            normalized_text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pages_book_vol ON pages(book, volume, pdf_page);
        CREATE INDEX IF NOT EXISTS idx_pages_source_file ON pages(source_file, pdf_page);
        CREATE TABLE IF NOT EXISTS toc_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book TEXT NOT NULL,
            volume INTEGER NOT NULL,
            source_file TEXT NOT NULL,
            title TEXT NOT NULL,
            pdf_page INTEGER NOT NULL,
            printed_page TEXT,
            level INTEGER NOT NULL DEFAULT 1,
            kind TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_toc_entries_book_volume ON toc_entries(book, volume);
        CREATE TABLE IF NOT EXISTS derivative_meta (
            book TEXT PRIMARY KEY,
            pipeline_version INTEGER NOT NULL DEFAULT 0,
            page_count INTEGER NOT NULL DEFAULT 0,
            toc_count INTEGER NOT NULL DEFAULT 0,
            quality_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    meta_columns = {row[1] for row in conn.execute("PRAGMA table_info(derivative_meta)")}
    if "quality_json" not in meta_columns:
        conn.execute("ALTER TABLE derivative_meta ADD COLUMN quality_json TEXT NOT NULL DEFAULT '{}'")
        conn.commit()
    return conn


def source_file_for(user_id: int, submission_id: int) -> str:
    return f"mylib/{int(user_id)}/{int(submission_id)}"


def _bib_clean(value: object, limit: int = 160) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\x00", " ")
    text = re.sub(r"(?<=[\u4e00-\u9fff]):(?=[\u4e00-\u9fff])", "：", text)
    return re.sub(r"\s+", " ", text).strip(" /,，.;；:：")[:limit]


def _person_clean(value: object) -> str:
    name = _bib_clean(value, 80)
    name = re.sub(r"^[\[【(（][^\]】)）]{1,16}[\]】)）]\s*", "", name)
    name = re.sub(r"^(?:作者|著者|编者|主编)\s*[:：]?\s*", "", name)
    name = re.sub(r"\s*(?:著|编著|主编|译)\s*$", "", name).strip()
    if re.search(r"[\u4e00-\u9fff]", name):
        name = re.sub(r"\s*[（(][A-Za-z][A-Za-z .·'’-]{2,60}[）)]\s*$", "", name).strip()
    return name.strip(" ·・‧•∙⋯…")


def _split_person_names(value: object) -> list[str]:
    """Split a compact CIP responsibility statement without swallowing roles/publishers."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\[【(（][^\]】)）]{1,16}[\]】)）]", "、", text)
    text = re.sub(r"(?:作者|著者|译者|翻译)\s*[:：]?", "", text)
    text = re.sub(r"\s*(?:著|编著|主编|编|译|校)\s*$", "", text)
    if re.fullmatch(r"[\u4e00-\u9fff·・\s]+", text) and len(text.split()) >= 2:
        raw_values = text.split()
    else:
        raw_values = re.split(r"\s*(?:、|，|,|；|;|和|及|&|\band\b)\s*", text, flags=re.I)
    values: list[str] = []
    for raw in raw_values:
        name = _person_clean(raw)
        if (
            not name or len(name) > 48
            or any(marker in name for marker in (
                "出版社", "出版公司", "印书馆", "责任编辑", "责任印制", "网址", "地址",
            ))
        ):
            continue
        if name not in values:
            values.append(name)
    return values


def _isbn_checksum_valid(value: object) -> bool:
    token = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    if len(token) == 10 and token[:9].isdigit() and (token[-1].isdigit() or token[-1] == "X"):
        check = 10 if token[-1] == "X" else int(token[-1])
        return sum((10 - index) * int(digit) for index, digit in enumerate(token[:9])) + check == (
            (sum((10 - index) * int(digit) for index, digit in enumerate(token[:9])) + check) // 11
        ) * 11
    if len(token) == 13 and token.isdigit():
        expected = (10 - sum(
            int(digit) * (1 if index % 2 == 0 else 3)
            for index, digit in enumerate(token[:12])
        ) % 10) % 10
        return expected == int(token[-1])
    return False


def _extract_isbn(text: str) -> tuple[str, bool]:
    """Read ISBN-10/13 labels without mistaking the label's ``10`` for the value."""
    matches = re.findall(
        r"(?:ISBN|1SBN|IS8N|SBX)\s*(?:(?:10|13)\s*[:：]\s*)?"
        r"([0-9Xx](?:[0-9Xx\-–— \t]*[0-9Xx])?)",
        text, re.I,
    )
    values: list[str] = []
    for raw in matches:
        token = re.sub(r"[^0-9Xx]", "", raw).upper()
        if len(token) in {10, 13} and token not in values:
            values.append(token)
    if not values:
        return "", False
    values.sort(key=lambda token: (
        _isbn_checksum_valid(token), len(token) == 13, len(token)
    ), reverse=True)
    return values[0], _isbn_checksum_valid(values[0])


def _extract_standard_number(text: str) -> tuple[str, str]:
    """Extract the pre-ISBN Chinese unified book number when explicitly labelled."""
    matches = re.findall(
        r"(?:统一\s*书\s*[号號])\s*[:：]?\s*"
        r"([0-9]{2,8}\s*[-—–]\s*[0-9A-Za-z]{1,8})",
        text,
    )
    for raw in matches:
        value = re.sub(r"\s+", "", raw).replace("—", "-").replace("–", "-")
        if re.fullmatch(r"[0-9]{2,8}-[0-9A-Za-z]{1,8}", value):
            return value, "统一书号"
    return "", ""


def _extract_publication_fields(text: str, lines: list[str]) -> tuple[str, str, str]:
    """Extract publisher/place/year from Chinese CIP or an English copyright page."""
    one_line = re.sub(r"\s+", " ", text)
    compact = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", one_line)
    publisher = ""
    place = ""

    cip_imprints = list(re.finditer(
        r"[一\-—–]*\s*([\u4e00-\u9fff]{2,10})\s*[:：]\s*"
        r"([\u4e00-\u9fffA-Za-z·&'’\- ]{2,60}?(?:出版社|出版公司|印书馆))",
        compact,
    ))
    if cip_imprints:
        match = cip_imprints[0]
        candidate_place = re.sub(r"^[一\-—–]+", "", _bib_clean(match.group(1), 20))
        candidate_publisher = _bib_clean(match.group(2), 80)
        # Word/PDF reading order can concatenate ``责任编辑:姓名 封面设计:姓名``
        # with the later publisher line.  Those responsibility labels are not an
        # imprint and must never become the publication place.
        if not re.search(r"(?:编辑|设计|校对|装帧|作者|译者|著者)", candidate_place):
            place = candidate_place
            publisher = candidate_publisher

    if not publisher:
        candidates: list[str] = []
        for source_line in lines:
            line_compact = re.sub(r"\s+", "", source_line)
            candidates.extend(re.findall(
                r"[\u4e00-\u9fffA-Za-z·]{2,50}?(?:出版社|出版公司|印书馆)"
                r"(?=出版|发行|$|[，,。.；;])",
                line_compact,
            ))
        if candidates:
            cleaned_candidates: list[str] = []
            for candidate in candidates:
                candidate = re.sub(
                    r"^.*(?:责任编辑|封面设计|装帧设计|校对|著|译|编)(?="
                    r"[\u4e00-\u9fffA-Za-z·&'’\- ]{2,60}?(?:出版社|出版公司|印书馆)$)",
                    "", candidate,
                )
                candidate = _bib_clean(candidate, 80)
                if candidate:
                    cleaned_candidates.append(candidate)
            # Standalone cover/imprint lines are normally the shortest complete
            # candidate; concatenated layout text is longer and less trustworthy.
            publisher = min(cleaned_candidates or candidates, key=len)
            publisher = re.sub(r"^[A-Za-z.]+向", "", publisher)

    if not publisher:
        for source_line in lines:
            match = re.fullmatch(
                r"(.{2,100}?(?:University Press|Publishing House|Publishers?|Press))[,.;]?",
                source_line, re.I,
            )
            if match:
                publisher = _bib_clean(match.group(1), 120)
                break

    if not place and publisher and re.search(r"[\u4e00-\u9fff]", publisher):
        escaped = re.escape(publisher)
        place_match = re.search(
            r"(?:^|[.。；;，,\s])[一\-—–]*\s*([\u4e00-\u9fff]{2,10})\s*[:：]\s*" + escaped,
            compact,
        )
        if place_match:
            place = _bib_clean(place_match.group(1), 20)
        else:
            address_match = re.search(
                r"(?:地址|邮编|邮政编码)?.{0,20}?"
                r"(北京|上海|天津|重庆|济南|南京|广州|杭州|武汉|成都|西安|长春|沈阳|"
                r"哈尔滨|石家庄|郑州|合肥|福州|南昌|长沙|南宁|海口|贵阳|昆明|拉萨|"
                r"兰州|西宁|银川|乌鲁木齐|呼和浩特)市?",
                text,
            )
            if address_match:
                place = address_match.group(1)
    elif publisher:
        try:
            publisher_index = next(
                index for index, line in enumerate(lines) if publisher.lower() in line.lower()
            )
        except StopIteration:
            publisher_index = -1
        for candidate in lines[publisher_index + 1:publisher_index + 5] if publisher_index >= 0 else []:
            if re.fullmatch(r"[A-Z][A-Za-z .'-]{2,36}", candidate) and not re.search(
                r"(?:Typeset|Printed|Bound|Limited|Ltd)", candidate, re.I,
            ):
                place = _bib_clean(candidate, 40)
                break

    if publisher and re.search(r"[\u4e00-\u9fff]", publisher):
        known_cities = re.findall(
            r"北京|上海|天津|重庆|济南|南京|广州|杭州|武汉|成都|西安|长春|沈阳|"
            r"哈尔滨|石家庄|郑州|合肥|福州|南昌|长沙|南宁|海口|贵阳|昆明|拉萨|"
            r"兰州|西宁|银川|乌鲁木齐|呼和浩特",
            place,
        )
        if known_cities:
            place = known_cities[-1]
        elif not re.fullmatch(r"[\u4e00-\u9fff]{2,6}", place):
            place = ""

    year = ""
    copyright_year = re.search(r"(?:©|Copyright\s*©?)\s*[^\n]{0,100}?[，,\s]((?:19|20)\d{2})\b", text, re.I)
    edition_years = re.findall(
        r"(?<!\d)((?:19|20)\d{2})\s*年.{0,24}?第\s*[一二三四五六七八九十\d]+\s*版",
        one_line,
    )
    imprint_year = None
    if publisher:
        imprint_year = re.search(
            re.escape(publisher) + r".{0,40}?[，,]\s*((?:19|20)\d{2})(?:\s*[年.\-/]|\b)",
            compact, re.I,
        )
    if edition_years:
        year = edition_years[0]
    elif copyright_year:
        year = copyright_year.group(1)
    elif imprint_year:
        year = imprint_year.group(1)
    else:
        candidates = re.findall(r"(?<!\d)((?:19|20)\d{2})(?:\s*[年.\-/]|\b)", one_line)
        year = candidates[0] if candidates else ""
    return publisher, place, year


def _front_matter_identity(
    front_pages: list[tuple[int, str]], fallback_title: str,
) -> dict:
    """Extract only cover/title-page identity, never prose from an introduction.

    Page four and later frequently begin with blurbs such as ``本书是……著作``.  Older
    rules accepted the final ``著`` as an author marker.  Restricting identity to the
    first three pages and requiring a name-shaped responsibility statement keeps that
    prose out while still supporting cover lines and an isolated ``主编`` label.
    """
    fallback_key = normalize(fallback_title)
    people: list[str] = []
    source_page: int | None = None
    cover_title = ""
    original_title = ""
    role = "author"
    translators: list[str] = []
    suspicious = ("本书", "简介", "章节", "思想家", "理论", "安排", "内容", "研究方向")

    for page_no, front_text in front_pages:
        if page_no > 3:
            continue
        lines = [_bib_clean(line, 180) for line in front_text.splitlines() if _bib_clean(line, 180)]
        if not lines:
            continue
        # Preserve the punctuation-rich title printed on the cover when it is the same
        # work as the upload title (e.g. 人物机器人 -> 人、物、机器人).
        for width in (1, 2, 3):
            for start in range(min(4, len(lines))):
                candidate = _bib_clean("".join(lines[start:start + width]), 160)
                if fallback_key and normalize(candidate) == fallback_key:
                    punctuation = len(re.findall(r"[：:、，,\-—–（）()]", candidate))
                    fallback_punctuation = len(re.findall(r"[：:、，,\-—–（）()]", fallback_title))
                    if punctuation >= fallback_punctuation and (not cover_title or len(candidate) > len(cover_title)):
                        cover_title = candidate

        latin_title_parts: list[str] = []
        for line in lines[:6]:
            latin = len(re.findall(r"[A-Za-z]", line))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", line))
            continues_title = bool(
                latin_title_parts
                and re.search(r"(?:\bto|\bof|\band|:)$", latin_title_parts[-1], re.I)
            )
            title_words = bool(re.search(
                r"\b(?:and|of|the|to|is|for|from|beyond|with|in|on|a|an)\b", line, re.I,
            ))
            if latin >= 5 and latin >= cjk * 2 and (
                continues_title or title_words
                or not re.fullmatch(r"[A-Z][A-Za-z .·'’-]{2,50}", line)
            ):
                latin_title_parts.append(line)
            elif latin_title_parts:
                break
        if latin_title_parts:
            candidate = _bib_clean(" ".join(latin_title_parts), 240)
            if len(candidate) >= 8:
                original_title = original_title or candidate

        editor_next = False
        for index, line in enumerate(lines[:12]):
            compact = line.replace(" ", "")
            if compact in {"主编", "编者", "编"}:
                editor_next = True
                role = "editor"
                continue
            if editor_next:
                editor_next = False
                raw_names = re.split(r"\s*(?:、|，|,|和|及|&| and )\s*", line)
                cleaned = [_person_clean(value) for value in raw_names]
                if 1 <= len(cleaned) <= 6 and all(
                    2 <= len(value) <= 30 and not any(word in value for word in suspicious)
                    and re.search(r"[\u4e00-\u9fffA-Za-z]", value)
                    for value in cleaned
                ):
                    for value in cleaned:
                        if value not in people:
                            people.append(value)
                    source_page = source_page or page_no
                continue

            responsibility = re.fullmatch(r"(.{2,45}?)(?:著|编著|主编|编)", line)
            if responsibility and not line.endswith(("摘编", "汇编", "选编")):
                name = _person_clean(responsibility.group(1))
                if (
                    name and len(name) <= 36 and not any(word in name for word in suspicious)
                    and not re.search(r"[。！？!?；;]", name)
                ):
                    if line.endswith(("主编", "编")):
                        role = "editor"
                    if name not in people:
                        people.append(name)
                    source_page = source_page or page_no
            translation = re.fullmatch(r"(.{2,70}?)译", line)
            if translation:
                raw_translation = translation.group(1)
                if not any(marker in raw_translation for marker in (
                    "根据", "版本", "翻译", "本书", "中译", "英译", "文字",
                )):
                    candidates = [
                        name for name in _split_person_names(raw_translation)
                        if len(name) >= 2 and not name.endswith(("译", "校"))
                    ]
                    if candidates and (
                        len(candidates) > len(translators)
                        or (len(candidates) == len(translators)
                            and sum(map(len, candidates)) < sum(map(len, translators)))
                    ):
                        translators = candidates
            if (
                index > 0 and fallback_key and fallback_key in normalize("".join(lines[:index]))
                and re.fullmatch(r"[\u4e00-\u9fff·]{2,20}", line)
                and not any(word in line for word in (
                    "出版社", "研究所", "中心", "委员会", "译", "校",
                ))
            ):
                if line not in people:
                    people.append(line)
                source_page = source_page or page_no
    return {
        "people": people,
        "translators": translators,
        "source_page": source_page,
        "cover_title": cover_title,
        "original_title": original_title,
        "role": role,
    }


def extract_bibliographic_metadata(
    pages: list[dict], *, fallback_title: str = "", fallback_author: str = "",
) -> dict:
    """从前置页自动定位版权页/CIP 数据并提取可验证书目字段。

    只把原文中明确出现的出版项写入结果；无法确认的字段沿用用户上传时填写的值，
    不猜出版社、地点或年份。扫描件在 OCR 完成后复用同一逻辑。
    """
    candidates: list[tuple[int, int, str]] = []
    front_pages: list[tuple[int, str]] = []
    tail_pages: list[tuple[int, str]] = []
    page_numbers: list[int] = []
    for item in pages:
        try:
            page_number = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page_number > 0:
            page_numbers.append(page_number)
    total_pages = max(page_numbers, default=0)
    for item in pages:
        try:
            pno = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        # 版权/CIP 页既可能在扉页之后，也经常被电子书保留在封底之前。
        if pno < 1 or (pno > 30 and (not total_pages or pno < total_pages - 20)):
            continue
        text = unicodedata.normalize("NFKC", str(item.get("text") or "")).replace("\x00", " ")
        if not text.strip():
            continue
        if pno <= 8:
            front_pages.append((pno, text))
        if total_pages and pno >= max(1, total_pages - 4):
            tail_pages.append((pno, text))
        upper = text.upper()
        marker_hits = sum(1 for marker in _COPYRIGHT_MARKERS if marker.upper() in upper)
        score = marker_hits * 2
        has_cip = "图书在版编目" in text or bool(re.search(r"\bCIP\b", upper))
        isbn_value, _isbn_valid = _extract_isbn(text)
        standard_number, _standard_number_type = _extract_standard_number(text)
        has_isbn = bool(isbn_value)
        has_standard_number = bool(standard_number)
        score += 7 if has_cip else 0
        score += 3 if has_isbn else 0
        score += 3 if has_standard_number else 0
        score += 2 if "出版发行" in text else 0
        publisher_signal = bool(re.search(
            r"(?:出版社|出版公司|印书馆|University Press|Publishing House|Publishers?|Press)",
            text, re.I,
        ))
        score += 1 if publisher_signal else 0
        score += 1 if re.search(r"(?:19|20)\d{2}\s*年", text) else 0
        # 出版社和年份也常见于正文脚注，不能单独证明这是版权页。只有 CIP，或 ISBN
        # 与至少两个印制项共同出现，才进入版权页候选池。
        imprint_hits = sum(marker in text for marker in ("出版发行", "出版公司", "版权所有", "版次", "印刷", "定价", "责任编辑"))
        foreign_record = bool(re.search(r"(?:LCCN|国会图书馆控制号|DOI|数字对象标识符)", text, re.I))
        edition_signal = bool(re.search(
            r"(?:19|20)\d{2}\s*年.{0,30}?第\s*[一二三四五六七八九十\d]+\s*(?:版|次印刷)",
            re.sub(r"\s+", " ", text),
        ))
        strong_page = (
            has_cip
            or (has_isbn and (imprint_hits >= 1 or foreign_record or publisher_signal))
            or (has_standard_number and (imprint_hits >= 1 or publisher_signal))
            or (publisher_signal and edition_signal and imprint_hits >= 2)
        )
        if strong_page:
            candidates.append((score, pno, text))

    title = _bib_clean(fallback_title)
    authors = [_bib_clean(fallback_author, 80)] if _bib_clean(fallback_author, 80) else []
    metadata: dict = {
        "title": title,
        "authors": authors,
        "translators": [],
        "publisher": "",
        "place": "",
        "year": "",
        "isbn": "",
        "standard_number": "",
        "standard_number_type": "",
        "source": "user_metadata",
        "copyright_pdf_page": None,
        "confidence": 0.25 if (title or authors) else 0.0,
    }
    identity = _front_matter_identity(front_pages, title)

    def front_matter_people() -> tuple[list[str], int | None]:
        return list(identity.get("people") or []), identity.get("source_page")

    if not candidates:
        cover_people, cover_page = front_matter_people()
        compilation_page: int | None = None
        compilation_text = ""
        for tail_page, tail_text in tail_pages:
            if any(marker in tail_text for marker in ("本次汇编摘自", "本汇编收录", "本汇编摘自")):
                compilation_page = tail_page
                compilation_text = tail_text
                break
        if compilation_page is not None:
            year_map = str.maketrans("〇零一二三四五六七八九", "00123456789")
            chinese_years = [value.translate(year_map) for value in re.findall(
                r"([〇零一二三四五六七八九]{4})\s*年", compilation_text,
            )]
            numeric_years = re.findall(r"(?<!\d)((?:19|20)\d{2})\s*年", compilation_text)
            confirmed_years = [value for value in chinese_years + numeric_years if re.fullmatch(r"(?:19|20)\d{2}", value)]
            metadata.update({
                "authors": cover_people or authors,
                "year": confirmed_years[-1] if confirmed_years else "",
                "source": "institutional_compilation",
                "copyright_pdf_page": compilation_page,
                "document_type": "compilation",
                "publication_status": "unpublished",
                "confidence": 0.84 if cover_people else 0.72,
            })
            return metadata
        front_text = "\n".join(text for _page, text in front_pages)
        fallback_is_chinese = len(re.findall(r"[\u4e00-\u9fff]", fallback_title)) >= 2
        has_translation_signal = (
            any(marker in front_text for marker in ("中文版序言", "中文版前言", "中文译稿"))
            or (fallback_is_chinese and bool(identity.get("original_title")))
        )
        if has_translation_signal:
            metadata.update({
                "title": identity.get("cover_title") or title,
                "authors": cover_people or authors,
                "source": "translation_manuscript",
                "copyright_pdf_page": cover_page or 1,
                "document_type": "translation_manuscript",
                "publication_status": "unpublished_translation",
                "responsibility_role": identity.get("role") or "author",
                "original_title": identity.get("original_title") or "",
                "confidence": 0.78 if (cover_people or authors) else 0.66,
            })
            return metadata
        if cover_people:
            metadata.update({
                "title": identity.get("cover_title") or title,
                "authors": cover_people,
                "source": "front_matter",
                "copyright_pdf_page": cover_page,
                "confidence": 0.58,
            })
        return metadata

    score, pno, text = max(candidates, key=lambda row: (row[0], -row[1]))
    one_line = re.sub(r"\s+", " ", text)
    compact_line = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", one_line)
    lines = [_bib_clean(line, 240) for line in text.splitlines() if _bib_clean(line, 240)]

    isbn_value, isbn_valid = _extract_isbn(text)
    standard_number, standard_number_type = _extract_standard_number(text)
    publisher, place, year = _extract_publication_fields(text, lines)

    # CIP 常见格式：“书名 / （国别）作者著；译者译. -- 出版地：出版社，年份”。
    slash_match = re.search(
        r"([^。；;]{2,120}?)\s*[/／]\s*([^。]{1,140}?)(?="
        r"(?:[.。]\s*[一\-—–]*\s*[\u4e00-\u9fff]{2,10}\s*[:：]|"
        r"\s*[一\-—–]+\s*[\u4e00-\u9fff]{2,10}\s*[:：]|\s+[I1]SBN|$))",
        compact_line, re.I,
    )
    detected_title = ""
    detected_authors: list[str] = []
    editors: list[str] = []
    translators: list[str] = []
    if slash_match:
        detected_title = _bib_clean(slash_match.group(1), 120)
        detected_title = re.sub(r"^.*?(?:图书在版编目\s*\([^)]*\)\s*数据|CIP数据)\s*", "", detected_title,
                                flags=re.I).strip()
        resp = slash_match.group(2)
        translator_match = re.search(
            r"(?:著|编著|主编|编)\s*[；;，,:：]?\s*(.{1,100}?)\s*译(?:[；;，,.。]|$)",
            resp,
        )
        if translator_match:
            raw_translation = translator_match.group(1)
            if "著" in raw_translation:
                raw_translation = raw_translation.rsplit("著", 1)[-1]
            translators = _split_person_names(raw_translation)
        author_match = re.search(r"^(.{1,120}?)\s*(?:著|编著|主编|编)(?:[；;，,:：.]|$)", resp)
        if author_match:
            detected_authors = _split_person_names(author_match.group(1))
        if not translators:
            for segment in re.split(r"[；;]", resp):
                match = re.search(r"(.{1,100}?)\s*译(?:[，,.。]|$)", segment)
                if match:
                    raw_translation = match.group(1)
                    if "著" in raw_translation:
                        raw_translation = raw_translation.rsplit("著", 1)[-1]
                    translators.extend(
                        name for name in _split_person_names(raw_translation)
                        if name not in translators
                    )
        if not detected_authors:
            for segment in re.split(r"[；;]", resp):
                match = re.search(r"(.{1,100}?)\s*(?:著|编著|主编|编)(?:[，,.。]|$)", segment)
                if match:
                    detected_authors.extend(
                        name for name in _split_person_names(match.group(1))
                        if name not in detected_authors
                    )
        # Preserve distinct responsibility roles.  A CIP line such as
        # “鲁宾著；周凡主编；曹江川译” must not turn the editor into a translator
        # or replace the original author.
        role_authors: list[str] = []
        role_editors: list[str] = []
        role_translators: list[str] = []
        for segment in re.split(r"[；;，,]", resp):
            role_match = re.fullmatch(r"\s*(.{1,80}?)\s*(编著|主编|著|编|译)\s*[.。]?\s*", segment)
            if not role_match:
                continue
            people = _split_person_names(role_match.group(1))
            role = role_match.group(2)
            target = (
                role_translators if role == "译"
                else role_editors if role in {"主编", "编"}
                else role_authors
            )
            for person in people:
                if person not in target:
                    target.append(person)
        if role_authors and (
            not detected_authors or len(role_authors) >= len(detected_authors)
        ):
            detected_authors = role_authors
        if role_editors:
            editors = role_editors
        if role_translators:
            translators = role_translators

    # 版权页分行格式没有 CIP 斜线时，仍可识别“作者/译者：姓名”。
    # 结构化“作者：”行比 CIP 责任说明更可靠（CIP OCR 常把外文姓误读或把分类号卷入）。
    for line in lines:
        match = re.match(r"(?:作\s*者|作者|著者|编著|主编)\s*[:：]\s*(.{2,60})", line)
        if match:
            explicit_author = _person_clean(match.group(1))
            if explicit_author:
                detected_authors = [explicit_author]
            break
    if not translators:
        for line in lines:
            match = re.match(r"(?:译者|翻译)\s*[:：]\s*(.{2,60})", line)
            if match:
                translators = [_person_clean(match.group(1))]
                break

    # 只有版权页信号足够、标题形态正常时才覆盖用户题名，避免把责任说明误当书名。
    if detected_title and fallback_title and (
        normalize(fallback_title) in normalize(detected_title)
        or ("在版" in detected_title and ("数据" in detected_title or "GP" in detected_title.upper()))
    ):
        detected_title = _bib_clean(fallback_title, 120)
    elif detected_title and fallback_title:
        responsibility_pollution = bool(re.search(
            r"(?:出版发行|著者|译者|出版社|出版集团|承印|开本|邮编|定价|著.{0,30}译)",
            detected_title,
        ))
        if responsibility_pollution:
            detected_title = _bib_clean(fallback_title, 120)
    if detected_title and 2 <= len(detected_title) <= 120 and score >= 7:
        if not re.search(r"(?:https?|网址|国会图书馆|ISBN|CIP)", detected_title, re.I):
            title = detected_title
    fallback_author_suspicious = bool(re.search(
        r"(?:译|校|出版社|出版公司|印书馆)", str(fallback_author or "")
    ))
    if detected_authors:
        authors = detected_authors
    else:
        cover_people, _cover_page = front_matter_people()
        if cover_people and (
            not authors or identity.get("role") == "editor" or fallback_author_suspicious
        ):
            authors = cover_people
    if not translators and identity.get("translators"):
        translators = list(identity.get("translators") or [])
    fallback_author_clean = _bib_clean(fallback_author, 80)
    if detected_authors and fallback_author_clean:
        detected_clean = _bib_clean(detected_authors[0], 80)
        # 人工输入/封面责任者与版权页 OCR 仅有一个近形字差异时保留前者，
        # 例如“高宣扬”被识别成“高宣插”；完全不同的人名仍以版权页为准。
        if (
            len(normalize(detected_clean)) == len(normalize(fallback_author_clean))
            and SequenceMatcher(
                None, normalize(detected_clean), normalize(fallback_author_clean),
            ).ratio() >= 0.6
        ):
            authors = [fallback_author_clean]
    field_hits = sum(bool(value) for value in (
        publisher, year, isbn_value or standard_number, detected_title,
    ))
    confidence = min(0.98, 0.45 + min(score, 14) * 0.025 + field_hits * 0.05)
    metadata.update({
        "title": identity.get("cover_title") or title or _bib_clean(fallback_title),
        "authors": authors,
        "editors": editors,
        "translators": translators,
        "publisher": publisher,
        "place": place,
        "year": year,
        "isbn": isbn_value,
        "isbn_valid": isbn_valid if isbn_value else None,
        "standard_number": standard_number,
        "standard_number_type": standard_number_type,
        "source": "copyright_page",
        "copyright_pdf_page": pno,
        "confidence": round(confidence, 3),
    })
    compact_isbn = str(metadata.get("isbn") or "")
    cover_text = next((value for page, value in front_pages if page == 1), "")
    cover_is_chinese = len(re.findall(r"[\u4e00-\u9fff]", cover_text)) >= 4
    # A Chinese text carrying only a foreign-edition ISBN and no translator/current
    # Chinese imprint is not evidence that the Chinese file itself was published by
    # that foreign press.  Preserve those facts as original-edition evidence instead.
    chinese_isbn = (
        (len(compact_isbn) == 10 and compact_isbn.startswith("7"))
        or (len(compact_isbn) == 13 and compact_isbn.startswith(("9787", "9797")))
    )
    chinese_publisher = bool(re.search(
        r"[\u4e00-\u9fff].*(?:出版社|出版公司|印书馆)", str(metadata.get("publisher") or "")
    ))
    if (
        cover_is_chinese and compact_isbn and not chinese_isbn and not chinese_publisher
        and not metadata.get("translators")
    ):
        metadata["original_edition"] = {
            "title": identity.get("original_title") or "",
            "authors": [],
            "publisher": metadata.get("publisher") or "",
            "place": metadata.get("place") or "",
            "year": metadata.get("year") or "",
            "isbn": compact_isbn,
            "source": "embedded_foreign_copyright",
            "confidence": metadata.get("confidence") or 0.0,
        }
        metadata.update({
            "publisher": "", "place": "", "year": "", "isbn": "",
            "source": "translation_manuscript",
            "document_type": "translation_manuscript",
            "publication_status": "unpublished_translation",
            "responsibility_role": identity.get("role") or "author",
            "original_title": identity.get("original_title") or "",
            "confidence": min(0.9, max(0.72, float(metadata.get("confidence") or 0.0))),
        })
    return metadata


def _crossref_original_record(
    payload: dict, *, query_title: str = "", query_isbn: str = "",
) -> dict:
    """Select a conservative book match from a Crossref response."""
    message = payload.get("message") if isinstance(payload, dict) else None
    items = message.get("items") if isinstance(message, dict) else None
    if not isinstance(items, list):
        return {}
    wanted_title = normalize(query_title)
    wanted_isbn = re.sub(r"[^0-9Xx]", "", query_isbn).upper()
    candidates: list[tuple[float, dict]] = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        types = str(item.get("type") or "")
        if types not in {"book", "monograph", "reference-book", "edited-book"}:
            continue
        titles = item.get("title") if isinstance(item.get("title"), list) else []
        record_title = _bib_clean(titles[0] if titles else "", 240)
        if not record_title:
            continue
        record_isbns = [
            re.sub(r"[^0-9Xx]", "", str(value)).upper()
            for value in (item.get("ISBN") or [])
        ]
        title_score = SequenceMatcher(None, wanted_title, normalize(record_title)).ratio() if wanted_title else 0.0
        isbn_match = bool(wanted_isbn and wanted_isbn in record_isbns)
        if not isbn_match and (not wanted_title or title_score < 0.72):
            continue
        score = (1.0 if isbn_match else 0.0) + title_score
        candidates.append((score, item))
    if not candidates:
        return {}
    _score, item = max(candidates, key=lambda pair: pair[0])
    titles = item.get("title") if isinstance(item.get("title"), list) else []
    authors = []
    for person in item.get("author") or item.get("editor") or []:
        if not isinstance(person, dict):
            continue
        name = _bib_clean(" ".join(filter(None, [person.get("given"), person.get("family")])), 100)
        if name:
            authors.append(name)
    date_parts = (
        ((item.get("published-print") or {}).get("date-parts") or [])
        or ((item.get("published") or {}).get("date-parts") or [])
        or ((item.get("issued") or {}).get("date-parts") or [])
    )
    year = ""
    try:
        year = str(int(date_parts[0][0]))
    except (IndexError, TypeError, ValueError):
        pass
    isbns = [re.sub(r"[^0-9Xx]", "", str(value)).upper() for value in (item.get("ISBN") or [])]
    doi = _bib_clean(item.get("DOI"), 160)
    return {
        "title": _bib_clean(titles[0] if titles else "", 240),
        "authors": authors,
        "publisher": _bib_clean(item.get("publisher"), 120),
        "place": "",
        "year": year,
        "isbn": wanted_isbn if wanted_isbn in isbns else (isbns[0] if isbns else ""),
        "doi": doi,
        "source": "crossref",
        "source_url": f"https://doi.org/{doi}" if doi else _bib_clean(item.get("URL"), 300),
        "confidence": 0.98 if wanted_isbn and wanted_isbn in isbns else round(min(0.96, 0.7 + _score * 0.13), 3),
    }


def enrich_original_edition_online(metadata: dict, *, timeout: float = 6.0) -> dict:
    """Attach original-edition facts without mislabelling an unpublished translation.

    The lookup is deliberately narrow: it runs only for translation manuscripts and
    accepts either an exact ISBN or a strong normalized original-title match.  Network
    failure leaves the local result untouched.
    """
    result = dict(metadata or {})
    if str(result.get("document_type") or "") != "translation_manuscript":
        return result
    if str(os.environ.get("MYLIB_BIBLIOGRAPHIC_ONLINE", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return result
    existing = result.get("original_edition") if isinstance(result.get("original_edition"), dict) else {}
    query_isbn = str(existing.get("isbn") or result.get("original_isbn") or "")
    query_title = str(existing.get("title") or result.get("original_title") or "")
    if not query_isbn and not query_title:
        return result
    params = {"rows": "8", "select": "DOI,title,author,editor,publisher,published,published-print,issued,ISBN,type,URL"}
    if query_isbn:
        params["filter"] = "isbn:" + re.sub(r"[^0-9Xx]", "", query_isbn)
    else:
        params["query.title"] = query_title
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "MarxSearchPersonalLibrary/1.0 (bibliographic verification)",
    })
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        matched = _crossref_original_record(payload, query_title=query_title, query_isbn=query_isbn)
    except Exception as exc:  # noqa: BLE001 — online evidence is optional
        LOGGER.info("mylib original-edition lookup skipped: %s", exc)
        return result
    if matched:
        result["original_edition"] = {**existing, **matched}
        result["online_verified"] = True
    return result


def merge_bibliographic_metadata(existing: dict | None, detected: dict | None) -> dict:
    """在版本回填时只修复可证明的缺漏/误报，避免覆盖用户已校准的可靠书目。"""
    old = dict(existing or {})
    new = dict(detected or {})
    if not old:
        return new
    old_source = str(old.get("source") or "")
    new_source = str(new.get("source") or "")
    old_conf = float(old.get("confidence") or 0.0)
    new_conf = float(new.get("confidence") or 0.0)

    if old_source == "manual_verified":
        return old

    if new_source in {"translation_manuscript", "institutional_compilation"} and (
        old_source not in {"translation_manuscript", "institutional_compilation"}
        or new_conf >= old_conf
    ):
        # A document-type correction must clear publication fields that belonged to a
        # different edition; otherwise the citation still presents a manuscript as a
        # formally published Chinese book.
        merged = dict(new)
        if isinstance(old.get("original_edition"), dict) and not merged.get("original_edition"):
            merged["original_edition"] = old["original_edition"]
        return merged

    # 旧版低置信“版权页”可能只是正文脚注；新版若只找到封面责任说明，应清除其伪出版项。
    if old_source == "copyright_page" and old_conf < 0.8 and new_source != "copyright_page":
        return new
    merged = dict(old)
    if new_source == "copyright_page" and (
        old_source != "copyright_page" or new_conf >= old_conf - 0.02
    ):
        if old_source == "translation_manuscript":
            # A newly verified Chinese copyright page proves that the file is a
            # published edition.  Start from that record so stale manuscript flags
            # and a mislabelled “original edition” cannot survive the repair.
            return new
        for key in ("title", "authors", "editors", "translators", "publisher", "place", "year", "isbn",
                    "standard_number", "standard_number_type",
                    "isbn_valid", "source", "copyright_pdf_page", "confidence", "document_type",
                    "publication_status", "responsibility_role", "original_title",
                    "original_edition", "online_verified"):
            value = new.get(key)
            if value not in (None, "", []):
                merged[key] = value
        if not new.get("document_type"):
            for key in (
                "document_type", "publication_status", "responsibility_role", "original_title",
                "original_edition", "online_verified",
            ):
                merged.pop(key, None)
        return merged
    for key in ("authors", "editors", "translators", "publisher", "place", "year", "isbn",
                "standard_number", "standard_number_type",
                "document_type", "publication_status", "responsibility_role", "original_title",
                "original_edition", "online_verified"):
        if merged.get(key) in (None, "", []) and new.get(key) not in (None, "", []):
            merged[key] = new[key]
    return merged


# ---------- 索引写入 ----------
def _clean_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\x00", "")
    text = re.sub(r"(?<=[\u4e00-\u9fff]),(?=[\u4e00-\u9fff])", "，", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff]):(?=[\u4e00-\u9fff])", "：", text)
    text = re.sub(r"[‎‏‪-‮⁦-⁩]", "", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"\s*/\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _valid_toc_title(value: object) -> str:
    title = _clean_title(value)
    compact = title.replace(" ", "")
    if (not title or compact in _TOC_NOISE or _NUMERIC_TITLE_RE.fullmatch(title)
            or _SCAN_ARTIFACT_RE.search(title)
            or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", title)):
        return ""
    return title


def _roman_to_int(token: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total, prev = 0, 0
    for char in reversed(token.upper()):
        value = values.get(char)
        if value is None:
            return None
        total += -value if value < prev else value
        prev = max(prev, value)
    return total or None


def _printed_token(value: object) -> str | None:
    token = unicodedata.normalize("NFKC", str(value or "")).strip()
    if token.isdigit() and 1 <= int(token) <= 3000:
        return str(int(token))
    if re.fullmatch(r"[IVXLCDMivxlcdml]+", token):
        # OCR commonly emits lower-case ell for the narrow roman ``I`` (Vll, lX,
        # Xl).  It is never a valid Roman symbol, so this correction is deterministic.
        roman = token.replace("l", "I")
        number = _roman_to_int(roman)
        if number and number <= 100:
            return f"pre-{roman.lower()}"
    if token.startswith("pre-"):
        return token.lower()
    return None


def _printed_page_analysis(page_items: list[dict]) -> tuple[dict[int, str], dict]:
    """从全书候选中推断稳定的印刷页码，而不是逐页轻信一个孤立数字。

    v1 索引曾把 ``PDF页码`` 原样写进 printed_page；v2 回填又读取并沿用了该字段，形成
    “PDF 1 = 原书第1页”的错误链。这里刻意只从原始文字重新识别，并要求多数候选支持同一个
    ``PDF-印刷`` 偏移；一旦成立，再按该偏移补齐正文页码。孤立的章节号、脚注号不会再冒充页码。
    """
    raw: dict[int, str] = {}
    explicit_candidates: dict[int, list[str]] = {}
    page_numbers = []
    for item in page_items:
        try:
            page_number = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page_number > 0:
            page_numbers.append(page_number)
    total_pages = max(page_numbers, default=0)

    def text_folio_candidates(text: str) -> list[str]:
        lines = [unicodedata.normalize("NFKC", line).strip() for line in text.splitlines() if line.strip()]
        candidates: list[str] = []
        for line in (lines[:3] + lines[-2:] if len(lines) > 3 else lines):
            probes = [line]
            leading = re.match(r"^(\d{1,4})(?=\D)", line)
            trailing = re.search(r"(?<=\D)(\d{1,4})$", line)
            if leading:
                probes.insert(0, leading.group(1))
            if trailing:
                probes.insert(0, trailing.group(1))
            for probe in probes:
                token = _printed_token(probe)
                if not token:
                    continue
                if token not in candidates:
                    candidates.append(token)
        fallback = _printed_token(detect_printed_page_from_text(text))
        if fallback and fallback not in candidates:
            candidates.append(fallback)
        return candidates

    for item in page_items:
        try:
            pno = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if pno <= 0:
            continue
        text_tokens = text_folio_candidates(str(item.get("text") or ""))
        token = text_tokens[0] if text_tokens else None
        if token:
            raw[pno] = token
        layout_tokens = item.get("printed_pages")
        if isinstance(layout_tokens, list):
            clean_tokens = []
            for value in layout_tokens:
                layout_token = _printed_token(value)
                if (
                    layout_token
                    and layout_token.isdigit()
                    and int(layout_token) > 3000
                ):
                    layout_token = None
                if layout_token and layout_token not in clean_tokens:
                    clean_tokens.append(layout_token)
            if clean_tokens:
                explicit_candidates[pno] = clean_tokens

    numeric = [(pno, int(token)) for pno, token in raw.items() if token.isdigit()]
    offsets = Counter(pno - printed for pno, printed in numeric)

    # Reflowed e-books (for example Word-to-PDF conversions) can preserve original
    # folios inside the text while repaginating 260 book pages into 161 PDF pages.
    # Their offset changes gradually across many pages; treating every change as a
    # physical omission produces a long list of false "missing page" alarms.  Keep
    # only directly observed folios for these documents and never infer gaps.
    reflow_numeric = [
        (pno, printed) for pno, printed in numeric if pno > 3
    ]
    numeric_pdf_span = (
        max((pno for pno, _printed in reflow_numeric), default=0)
        - min((pno for pno, _printed in reflow_numeric), default=0)
    )
    numeric_printed_span = (
        max((printed for _pno, printed in reflow_numeric), default=0)
        - min((printed for _pno, printed in reflow_numeric), default=0)
    )
    max_printed = max((printed for _pno, printed in reflow_numeric), default=0)
    reflow_offsets = {pno - printed for pno, printed in reflow_numeric}
    dominant_offset_support = max(offsets.values(), default=0)
    reflowed = bool(
        total_pages >= 30
        and len(reflow_numeric) >= max(12, total_pages // 5)
        and len(reflow_numeric) / max(1, total_pages) >= 0.65
        and len(reflow_offsets) >= 5
        and dominant_offset_support <= max(12, int(len(reflow_numeric) * 0.12))
        and numeric_pdf_span >= 10
        and numeric_printed_span >= numeric_pdf_span * 1.18
        and max_printed >= total_pages + max(10, total_pages // 8)
    )
    if reflowed:
        # A reflowed PDF page can contain the end of one original page and the start
        # of the next.  A lone embedded number therefore cannot label the whole PDF
        # page safely.  Preserve original folios on TOC entries, but make page-level
        # citations fall back to an explicitly labelled PDF page instead of guessing.
        return {}, {
            "page_confidence": 0.76,
            "page_offset": None,
            "page_segments": [],
            "page_gaps": [],
            "page_evidence": len(reflow_numeric),
            "page_consistent_evidence": 0,
            "page_explicit_evidence": 0,
            "page_multi_label_pages": 0,
            "page_mapping_mode": "reflowed_explicit",
            "page_reflow_evidence": len(reflow_numeric),
            "page_reflow_offset_count": len(reflow_offsets),
            "page_reflow_dominant_support": dominant_offset_support,
        }

    # 一本 PDF 中途缺页/插页后，PDF-书页码偏移会永久改变。旧实现只取全书“票数最多”
    # 的一个偏移，必然让改变前或改变后的整段引文错两页。这里保留每一段连续、受到足够
    # 多页支持的偏移轨迹，并明确记录轨迹之间缺失或重复的书页。
    top_support = offsets.most_common(1)[0][1] if offsets else 0
    if len(numeric) <= 2 or (
        len(numeric) <= 4 and top_support >= 2 and top_support * 2 >= len(numeric)
    ):
        trusted_threshold = 2
    else:
        trusted_threshold = max(3, min(8, (top_support + 9) // 10))
    trusted_offsets = {
        int(offset) for offset, support in offsets.items() if support >= trusted_threshold
    }
    anchors = sorted(
        (pno, printed, pno - printed)
        for pno, printed in numeric
        if pno - printed in trusted_offsets
    )
    runs: list[dict] = []
    for pno, printed, offset in anchors:
        if not runs or runs[-1]["offset"] != offset:
            runs.append({"offset": offset, "anchors": [(pno, printed)]})
        else:
            runs[-1]["anchors"].append((pno, printed))
    runs = [run for run in runs if len(run["anchors"]) >= trusted_threshold]
    merged_runs: list[dict] = []
    for run in runs:
        if merged_runs and merged_runs[-1]["offset"] == run["offset"]:
            merged_runs[-1]["anchors"].extend(run["anchors"])
        else:
            merged_runs.append(run)
    runs = merged_runs

    segments: list[dict] = []
    for run in runs:
        first_pdf, first_printed = run["anchors"][0]
        last_pdf, last_printed = run["anchors"][-1]
        start_pdf = first_pdf - first_printed + 1 if first_printed <= 3 else first_pdf
        segments.append({
            "pdf_start": max(1, int(start_pdf)),
            "pdf_end": int(last_pdf),
            "printed_start": max(1, int(start_pdf) - int(run["offset"])),
            "printed_end": int(last_printed),
            "offset": int(run["offset"]),
            "support": len(run["anchors"]),
        })
    for index in range(len(segments) - 1):
        segments[index]["pdf_end"] = min(
            int(segments[index]["pdf_end"]), int(segments[index + 1]["pdf_start"]) - 1,
        )

    out: dict[int, str] = {}
    for segment in segments:
        for pno in range(int(segment["pdf_start"]), int(segment["pdf_end"]) + 1):
            printed = pno - int(segment["offset"])
            if 1 <= printed <= 3000:
                out[pno] = str(printed)

    # 页面外缘的结构化 OCR 可以表达同一画布上的多个书页。若已有轨迹，要求它与当前
    # 页或相邻页轨迹相容；完全没有轨迹时仍保留逐页证据，避免双页扫描丢失页码。
    explicit: dict[int, str] = {}
    last_segment_pdf = max((int(segment["pdf_end"]) for segment in segments), default=0)
    for pno, tokens in explicit_candidates.items():
        predicted = out.get(pno)
        if not segments or pno > last_segment_pdf:
            supported = list(tokens)
        else:
            supported = [token for token in tokens if token == predicted]
            for token in tokens:
                if token in supported or not token.isdigit() or not predicted or not predicted.isdigit():
                    continue
                delta = int(token) - int(predicted)
                if abs(delta) > 12:
                    continue
                for step in (-2, -1, 1, 2):
                    neighbor = pno + step
                    if str(int(token) + step) in explicit_candidates.get(neighbor, []):
                        supported.append(token)
                        break
        if supported:
            explicit[pno] = "、".join(dict.fromkeys(supported))
    out.update(explicit)

    # 正文前置罗马页码只采用直接证据，不参与阿拉伯页码轨迹。
    first_body_pdf = min((int(segment["pdf_start"]) for segment in segments), default=total_pages + 1)
    for pno, token in raw.items():
        if pno < first_body_pdf and token.startswith("pre-"):
            out[pno] = token

    page_gaps: list[dict] = []
    for previous, current in zip(segments, segments[1:]):
        previous_printed = int(previous["pdf_end"]) - int(previous["offset"])
        current_printed = int(current["pdf_start"]) - int(current["offset"])
        if current_printed != previous_printed + 1:
            intervening_pdf_pages = max(
                0, int(current["pdf_start"]) - int(previous["pdf_end"]) - 1,
            )
            skipped_count = max(
                0, current_printed - previous_printed - 1 - intervening_pdf_pages,
            )
            range_values = list(range(previous_printed + 1, current_printed))
            page_gaps.append({
                "after_pdf_page": int(previous["pdf_end"]),
                "before_pdf_page": int(current["pdf_start"]),
                "previous_printed_page": previous_printed,
                "next_printed_page": current_printed,
                # Unnumbered divider/blank pages can account for part of the range.
                # Report an exact list only when every skipped folio is absent from
                # the PDF; otherwise report the count and an ambiguous interval.
                "missing_printed_pages": range_values if skipped_count == len(range_values) else [],
                "missing_page_count": skipped_count,
                "possible_printed_range": range_values if skipped_count and not (
                    skipped_count == len(range_values)
                ) else [],
            })
    support = sum(int(segment["support"]) for segment in segments)
    confidence = min(0.99, 0.58 + min(0.36, support * 0.02)) if segments else 0.0
    if page_gaps:
        confidence = min(confidence, 0.9)
    return out, {
        "page_confidence": round(confidence, 3),
        "page_offset": segments[0]["offset"] if len(segments) == 1 else None,
        "page_segments": segments,
        "page_gaps": page_gaps,
        "page_evidence": len(numeric),
        "page_consistent_evidence": support,
        "page_explicit_evidence": len(explicit),
        "page_multi_label_pages": sum("、" in value for value in explicit.values()),
    }


def _stable_printed_page_map(page_items: list[dict]) -> dict[int, str]:
    """兼容测试与旧调用方；完整质量信息由 ``_printed_page_analysis`` 返回。"""
    return _printed_page_analysis(page_items)[0]


def _normalize_explicit_toc(entries: list[dict] | None, total_pages: int) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[int, int, str]] = set()
    for order, item in enumerate(entries or []):
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("pdf_page") or item.get("page") or 0)
            level = max(1, min(12, int(item.get("level") or 1)))
        except (TypeError, ValueError):
            continue
        title = _valid_toc_title(item.get("title"))
        if not title or page < 1 or (total_pages and page > total_pages):
            continue
        key = (level, page, title)
        if key in seen:
            continue
        seen.add(key)
        printed_page = _printed_token(item.get("printed_page"))
        out.append({"title": title, "pdf_page": page, "level": level,
                    "printed_page": printed_page,
                    "kind": "body", "sort_order": order})
    return out


def _parse_toc_line(line: str) -> tuple[str, str | None]:
    line = unicodedata.normalize("NFKC", line).replace("—", "-").replace("–", "-")
    line = re.sub(r"\s+", " ", line).strip()
    standalone = _TOC_STANDALONE_PAGE_RE.match(line)
    if standalone:
        return "", _printed_token(standalone.group("start"))
    for pattern in (_TOC_RANGE_RE, _TOC_INLINE_RE):
        match = pattern.match(line)
        if not match:
            continue
        title = _valid_toc_title(match.group("title"))
        token = _printed_token(match.group("start"))
        if token and token.startswith("pre-") and match.start("start") > 0:
            # Without a visual separator the final d/c/i of an English title such
            # as ``The concept of need`` is a letter, not Roman folio 500/100/1.
            previous = line[match.start("start") - 1]
            if previous.isalpha():
                continue
        if title and token:
            return title, token
    return line, None


def _parse_scan_toc_line(line: str) -> tuple[str, str | None]:
    """Parse OCR TOC rows where the page number is followed by an author.

    Example: ``1生产与去人性化……57杰森·多西``.  A normal text-layer parser
    expects the folio at line end and therefore missed whole scanned contents pages.
    """
    cleaned = _clean_title(line)
    page_first = re.match(
        r"^\s*(?P<start>\d{1,4}|[IVXLCDMivxlcdm]{1,8})\s*[/／]\s*(?P<title>.+?)\s*$",
        cleaned,
    )
    if page_first:
        title = _valid_toc_title(page_first.group("title"))
        token = _printed_token(page_first.group("start"))
        if title and token:
            return title, token
    title, token = _parse_toc_line(line)
    if token is not None:
        return title, token
    compact = cleaned.replace(" ", "")
    if not (
        re.match(r"^\d{1,2}(?=[\u4e00-\u9fffA-Za-z])", compact)
        or compact.startswith(("引言", "导言", "后记", "参考文献", "索引", "撰稿人", "作者注"))
        or (len(cleaned) <= 120 and bool(re.search(r"[\u4e00-\u9fff]", cleaned)))
    ):
        return cleaned, None
    numbers = list(re.finditer(r"(?<!\d)(\d{1,4})(?!\d)", cleaned))
    candidates = []
    for match in numbers:
        value = int(match.group(1))
        if re.match(
            r"^\s*第\s*" + re.escape(match.group(1)) + r"\s*[编部篇章节卷]",
            cleaned,
        ):
            continue  # “第 3 章”的 3 是章序，不是条目页码
        if match.start() <= 2 and re.match(r"^\s*\d{1,2}\s*[^\d]", cleaned):
            continue  # leading chapter number
        if 1 <= value <= 3000 and not (1900 <= value <= 2099):
            candidates.append(match)
    if not candidates:
        return cleaned, None
    match = candidates[-1]
    parsed_title = _valid_toc_title(cleaned[:match.start()].rstrip(" .·•∙⋯…-—–"))
    return (parsed_title, str(int(match.group(1)))) if parsed_title else (cleaned, None)


def _derive_toc_from_pages(
    page_items: list[dict], page_rows: list[tuple], page_quality: dict | None = None,
) -> list[dict]:
    """从文字层的印刷目录页生成目录；书签为空时的生产兜底。

    页码优先使用识别出的印刷页映射；映射稀疏时按多数页的 PDF-印刷偏移估算，并严格限制
    在本书页数内。无法可靠映射的条目宁可跳过，不制造错误目录。
    """
    if not page_rows:
        return []
    reflowed_layout = bool(
        isinstance(page_quality, dict)
        and page_quality.get("page_mapping_mode") == "reflowed_explicit"
    )
    page_map = {str(row[4]): int(row[3]) for row in page_rows if row[4]}
    offsets = [int(row[3]) - int(row[4]) for row in page_rows
               if row[4] and str(row[4]).isdigit()]
    offset_counts = Counter(offsets)
    common_offset = None
    if offset_counts:
        candidate, support = offset_counts.most_common(1)[0]
        # 只有全书近乎单一轨迹时才允许用偏移补洞；分段页码中的缺页不能被“多数偏移”
        # 强行映射到错误 PDF 页。
        if support >= max(3, int(len(offsets) * 0.9)):
            common_offset = int(candidate)
    total_pages = max(int(row[3]) for row in page_rows)

    by_page: list[tuple[int, str]] = []
    for item in page_items:
        try:
            pno = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= pno <= total_pages:
            by_page.append((pno, str(item.get("text") or "")))
    by_page.sort()

    def line_stats(text: str) -> tuple[list[str], int, bool]:
        lines = [_clean_title(line) for line in text.splitlines()]
        lines = [line for line in lines if line]
        hits = sum(1 for line in lines if _parse_scan_toc_line(line)[1] is not None)
        head = "".join(lines[:4]).replace(" ", "")
        specialized_directory = bool(re.search(
            r"(?:图表|插图|表格|图|表)目录", head
        ))
        bibliography_noise = sum(
            marker in text.upper() for marker in ("ISBN", "LCCN", "HTTP", "CIP", "DOI")
        )
        copyright_noise = sum(marker in text for marker in _COPYRIGHT_MARKERS)
        if specialized_directory:
            # 主目录标题偶尔会在 OCR 中整行丢失，而后面的“图表目录”完整保留。
            # 若只按“首个含目录的页”选择，就会把图 3.1/图 4.1 当书的章节。
            # 专项图表目录不参与主目录起点竞争，让前面的结构化页码页兜底。
            hits = 0
        elif (bibliography_noise >= 2 or copyright_noise >= 2) and "目录" not in head:
            hits = 0
        has_embedded_heading = any(
            line.replace(" ", "") == "目录" or line.strip().upper() == "CONTENTS"
            for line in lines[:40]
        )
        has_main_heading = (
            "目录" in head or head.upper().startswith("CONTENTS") or has_embedded_heading
        )
        return lines, hits, has_main_heading and not specialized_directory

    toc_pages: list[tuple[int, list[str]]] = []
    scan = by_page[:min(len(by_page), 80)]
    scanned = [(pno, *line_stats(text)) for pno, text in scan]

    def directory_shape(lines: list[str]) -> bool:
        slash_folios = sum(bool(re.search(r"[/／]\s*\d{1,4}\s*$", line)) for line in lines)
        standalone_folios = sum(bool(re.fullmatch(
            r"[/／]?\s*(?:\d{1,4}|[IVXLCDMivxlcdm]+)\s*", line
        )) for line in lines)
        chapter_lines = sum(bool(_TOC_HEADING_RE.match(line)) for line in lines)
        numbered_entries = sum(bool(re.match(
            r"^\d{1,2}(?=[一-鿿A-Za-z])", line.replace(" ", "")
        )) for line in lines)
        return (
            slash_folios >= 2 or standalone_folios >= 3
            or chapter_lines >= 2 or numbered_entries >= 3
        )

    def strong_headingless_page(lines: list[str], hits: int, *, continuation: bool = False) -> bool:
        """A missing heading is tolerable; prose disguised by footnotes is not."""
        meaningful = [line for line in lines if len(normalize(line)) >= 2]
        parsed = [_parse_scan_toc_line(line) for line in meaningful]
        tokens = [token for _title, token in parsed if token and token.isdigit()]
        values = [int(token) for token in tokens]
        monotonic = sum(
            current >= previous for previous, current in zip(values, values[1:])
        ) / max(1, len(values) - 1)
        hit_ratio = hits / max(1, len(meaningful))
        prose_ratio = sum(
            len(line) > 72 or bool(re.search(r"[。！？；][）)]?$", line))
            for line in meaningful
        ) / max(1, len(meaningful))
        return (
            hits >= (2 if continuation else 4)
            and hit_ratio >= (0.25 if continuation else 0.35)
            and monotonic >= 0.70
            and prose_ratio <= 0.25
        )

    # Prefer an explicit “目录” page even when earlier prefaces contain many years or
    # footnote numbers.  The previous single pass stopped on such numerical prose and
    # never reached the real contents a few pages later (notably 1,000-page scans).
    start_index = next((
        index for index, (_pno, _lines, hits, has_word) in enumerate(scanned)
        if has_word and hits >= 2
    ), None)
    if start_index is not None:
        # The word “目录” can be lost or garbled on page 1 of a multi-page contents
        # section yet survive in a running header on page 3.  Recover immediately
        # preceding, strongly structured pages instead of publishing only the tail.
        while start_index > 0:
            previous = scanned[start_index - 1]
            current = scanned[start_index]
            if (
                previous[0] + 1 != current[0]
                or not directory_shape(previous[1])
                or not strong_headingless_page(previous[1], previous[2])
            ):
                break
            start_index -= 1
    if start_index is None:
        # Some scans genuinely lose the “目录” heading.  Keep the structural fallback,
        # but use it only after proving that no explicit contents page exists.  Merely
        # having four numbers is insufficient: prefaces/copyright pages regularly mention
        # chapter numbers and years.  Require directory-shaped evidence (folio slashes,
        # several standalone page-number lines, or several chapter-heading lines).
        start_index = next((
            index for index, (_pno, _lines, hits, _has_word) in enumerate(scanned)
            if (
                directory_shape(_lines)
                and strong_headingless_page(_lines, hits)
                and index + 1 < len(scanned)
                and scanned[index + 1][0] == _pno + 1
                and strong_headingless_page(
                    scanned[index + 1][1], scanned[index + 1][2], continuation=True,
                )
            )
        ), None)
    if start_index is not None:
        pno, lines, _hits, _has_word = scanned[start_index]
        toc_pages.append((pno, lines))
        for next_pno, next_lines, next_hits, next_has_word in scanned[start_index + 1:start_index + 12]:
            if (
                next_pno != toc_pages[-1][0] + 1
                or (
                    not next_has_word
                    and (
                        (
                            reflowed_layout
                            and not strong_headingless_page(
                                next_lines, next_hits, continuation=True,
                            )
                        )
                        or (not reflowed_layout and next_hits < 3)
                    )
                )
            ):
                break
            toc_pages.append((next_pno, next_lines))
    if not toc_pages:
        return []

    toc_page_numbers = {pno for pno, _lines in toc_pages}
    # 精确标题行可以修正前言/致谢等“目录页码识别失败”的条目。只看每页开头若干行并排除目录页，
    # 避免正文中偶然提到同名短语时跳错。
    title_pages: dict[str, int] = {}
    title_heads: dict[int, str] = {}
    text_by_page = {pno: text for pno, text in by_page}
    printed_by_pdf = {int(row[3]): str(row[4]) for row in page_rows if row[4]}
    body_first_counts: Counter[str] = Counter()
    for pno, text in by_page:
        if pno in toc_page_numbers:
            continue
        raw_head_lines = text.splitlines() if reflowed_layout else text.splitlines()[:16]
        head_lines = [_valid_toc_title(raw_line) for raw_line in raw_head_lines]
        head_lines = [line for line in head_lines if line]
        if head_lines:
            body_first_counts[normalize(head_lines[0])] += 1
        title_heads[pno] = normalize("".join(head_lines[:6]))
        for raw_line in head_lines:
            line = _valid_toc_title(raw_line)
            key = normalize(line) if line else ""
            if len(key) >= 2:
                title_pages.setdefault(key, pno)
        for width in (2, 3):
            if len(head_lines) >= width:
                key = normalize("".join(head_lines[:width]))
                if len(key) >= 2:
                    title_pages.setdefault(key, pno)

    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    pending_heading = ""
    chapter_open = False

    def resolve_target(title: str, token: str) -> tuple[int | None, str | None]:
        mapped = page_map.get(token)
        if mapped is None and token.isdigit() and common_offset is not None:
            estimate = int(token) + int(common_offset)
            mapped = estimate if 1 <= estimate <= total_pages else None
        title_key = normalize(title)
        title_variants = [title_key]
        stripped = re.sub(r"^(?:引言|导言|前言|后记)", "", title_key)
        if stripped and stripped != title_key:
            title_variants.insert(0, stripped)
        matched = next((title_pages.get(key) for key in title_variants if title_pages.get(key)), None)
        if matched is None and any(len(key) >= 4 for key in title_variants):
            matched = next((
                pno for pno, head in title_heads.items()
                if any(key in head for key in title_variants if len(key) >= 4)
            ), None)
        if matched is None:
            best: tuple[float, int] | None = None
            for pno, head in title_heads.items():
                for key in title_variants:
                    if len(key) < 6 or not head:
                        continue
                    score = SequenceMatcher(None, key, head[:max(len(key) + 18, 30)]).ratio()
                    if score >= 0.66 and (best is None or score > best[0]):
                        best = (score, pno)
            matched = best[1] if best else None
        if matched is not None:
            # 标题实页优先决定跳转；展示该实页已经验出的书页码，而不是继续沿用目录里
            # 可能指向缺失页的数字。
            verified_printed = token if reflowed_layout else printed_by_pdf.get(int(matched))
            if (
                not verified_printed and token.isdigit() and common_offset is not None
                and int(matched) == int(token) + int(common_offset)
            ):
                verified_printed = token
            return int(matched), verified_printed
        return (int(mapped), token) if mapped is not None else (None, None)

    def add(title: str, target: int | None, printed: str | None, level: int, kind: str) -> None:
        title = _valid_toc_title(title)
        if not title or target is None or not (1 <= int(target) <= total_pages):
            return
        key = (int(target), title)
        if key in seen:
            return
        seen.add(key)
        out.append({"title": title, "pdf_page": int(target), "printed_page": printed,
                    "level": level, "kind": kind, "sort_order": len(out)})

    # 目录续页常以书名作页眉，先剔除在多个目录页首反复出现的行。
    first_line_counts = Counter(
        normalize(lines[0]) for _pno, lines in toc_pages if lines and "目录" not in lines[0]
    )
    pending_parts: list[str] = []

    def trim_contents_prefix(lines: list[str]) -> list[str]:
        """Discard preface/copyright text that precedes an embedded TOC heading."""
        for index, line in enumerate(lines[:40]):
            compact = line.replace(" ", "")
            upper = line.strip().upper()
            if compact == "目录" or upper == "CONTENTS":
                return lines[index + 1:]
            if index <= 5 and compact.startswith("目录") and len(compact) > 2:
                remainder = _clean_title(compact[2:])
                return ([remainder] if remainder else []) + lines[index + 1:]
            if index <= 5 and upper.startswith("CONTENTS "):
                remainder = _clean_title(line.strip()[8:])
                return ([remainder] if remainder else []) + lines[index + 1:]
        return lines

    def flush_without_token() -> None:
        nonlocal pending_parts
        if not pending_parts:
            return
        candidate = _valid_toc_title("".join(pending_parts))
        pending_parts = []
        if not candidate:
            return
        matched, printed = resolve_target(candidate, "")
        if matched is not None:
            add(candidate, matched, printed, 1, "section")

    def canonical_body_title(title: str, target: int | None) -> str:
        """用目标正文页的标题反校目录 OCR，去掉点线、残字和错序。"""
        if target is None:
            return _valid_toc_title(title)
        source_lines = [_clean_title(line) for line in text_by_page.get(int(target), "").splitlines()[:16]]
        heading_lines: list[str] = []
        for line in source_lines:
            compact = line.replace(" ", "")
            if _DATE_LINE_RE.fullmatch(compact):
                if heading_lines:
                    break
                continue
            if _printed_token(compact) or not _valid_toc_title(line):
                continue
            heading_lines.append(line)
            if len(heading_lines) >= 6:
                break
        wanted = normalize(title)
        best_title = _valid_toc_title(title)
        best_score = 0.0
        for start in range(len(heading_lines)):
            for width in range(1, min(4, len(heading_lines) - start) + 1):
                candidate = _valid_toc_title("".join(heading_lines[start:start + width]))
                candidate_key = normalize(candidate)
                if not candidate_key or not wanted:
                    continue
                score = SequenceMatcher(None, wanted, candidate_key).ratio()
                if wanted in candidate_key or candidate_key in wanted:
                    score = max(score, min(len(wanted), len(candidate_key)) / max(len(wanted), len(candidate_key)))
                if score > best_score:
                    best_score, best_title = score, candidate
        return best_title if best_score >= 0.58 else _valid_toc_title(title)

    # 讲话/文选类目录通常每条都以日期收束。按日期切记录后，页码无论位于标题前、标题中
    # 还是标题后都属于同一条，能够抵抗 PDF 字符对象读序错乱。
    dated_line_count = sum(
        1 for _pno, lines in toc_pages for line in lines
        if _DATE_LINE_RE.fullmatch(line.replace(" ", ""))
    )
    if dated_line_count >= 3:
        embedded_page_re = re.compile(
            r"(?<!\d)(?P<start>[IVXLCDMivxlcdm]+|\d{1,4})"
            r"(?P<range>\s*[-—–~～一至]+\s*(?:[IVXLCDMivxlcdm]+|\d{0,4})?)?"
        )
        last_token = 0

        def add_dated_record(parts: list[str]) -> None:
            nonlocal last_token
            if not parts:
                return
            candidates: list[tuple[int, int, int, re.Match[str]]] = []
            for line_index, part in enumerate(parts):
                for match in embedded_page_re.finditer(part):
                    token = _printed_token(match.group("start"))
                    if not token or not token.isdigit():
                        continue
                    number = int(token)
                    if not (1 <= number <= 3000):
                        continue
                    has_range = bool(match.group("range"))
                    standalone = bool(re.fullmatch(
                        r"[（(]?\s*[IVXLCDMivxlcdm\d]+(?:\s*[-—–~～一至]+\s*"
                        r"[IVXLCDMivxlcdm\d]*)?\s*[）)]?[·•∙⋯…\.]*", part,
                    ))
                    candidates.append((2 if has_range else (1 if standalone else 0), number,
                                       line_index, match))
            ranged = [item for item in candidates if item[0] == 2]
            standalone = [item for item in candidates if item[0] == 1]
            pool = ranged or standalone
            plausible = [item for item in pool if item[1] >= max(1, last_token)]
            chosen = min(plausible or pool, key=lambda item: (item[1], item[2])) if pool else None
            token = str(chosen[1]) if chosen else ""

            title_parts: list[str] = []
            for part in parts:
                cleaned = embedded_page_re.sub(
                    lambda match: "" if match.group("range") or re.fullmatch(
                        r"\s*[（(]?[IVXLCDMivxlcdm\d]+[）)]?\s*", part,
                    ) else match.group(0),
                    part,
                )
                cleaned = re.sub(r"^[·•∙⋯…\.\-\s]+|[·•∙⋯…\.\-\s]+$", "", cleaned)
                compact = normalize(cleaned)
                if not cleaned or compact in _TOC_NOISE:
                    continue
                if len(compact) <= 1 or re.fullmatch(r"[A-Za-z*、，,·•∙⋯…\-]{1,8}", cleaned):
                    continue
                title_parts.append(cleaned)
            title = _valid_toc_title("".join(title_parts))
            title = re.sub(r"[·•∙⋯…\.]{2,}", "", title).strip(" ·•∙⋯….-")
            if not title:
                return
            target, printed = resolve_target(title, token)
            canonical = canonical_body_title(title, target)
            if not token and target is not None:
                token = printed_by_pdf.get(int(target), "")
                printed = token or printed
            if token and token.isdigit():
                last_token = max(last_token, int(token))
            add(canonical, target, printed, 1, "section")

        for _toc_page, original_lines in toc_pages:
            had_explicit_heading = any(
                line.replace(" ", "") == "目录" or line.strip().upper() == "CONTENTS"
                for line in original_lines[:40]
            )
            lines = trim_contents_prefix(list(original_lines))
            if lines and not had_explicit_heading and "目录" not in lines[0].replace(" ", ""):
                first_key = normalize(lines[0])
                first_resolves = first_key in title_pages or any(first_key in head for head in title_heads.values())
                if not _TOC_HEADING_RE.match(lines[0]) and (
                    first_line_counts.get(first_key, 0) >= 2
                    or body_first_counts.get(first_key, 0) >= 3
                    or not first_resolves
                ):
                    lines = lines[1:]
            record: list[str] = []
            for line in lines:
                compact = line.replace(" ", "")
                if compact in _TOC_NOISE:
                    continue
                if _DATE_LINE_RE.fullmatch(compact):
                    add_dated_record(record)
                    record = []
                else:
                    record.append(line)
            add_dated_record(record)
        out.sort(key=lambda item: (item["pdf_page"], item["sort_order"]))
        for order, item in enumerate(out):
            item["sort_order"] = order
            item["_toc_evidence_pdf_page"] = toc_pages[0][0]
        return out

    last_toc_numeric = 0
    for _toc_page, source_lines in toc_pages:
        had_explicit_heading = any(
            line.replace(" ", "") == "目录" or line.strip().upper() == "CONTENTS"
            for line in source_lines[:40]
        )
        lines = trim_contents_prefix(list(source_lines))
        # OCR often wraps a long numbered title immediately before its folio. Join only
        # that unmistakable pair; author lines and part subtitles remain independent.
        coalesced: list[str] = []
        line_index = 0
        while line_index < len(lines):
            current = lines[line_index]
            current_title, current_token = _parse_scan_toc_line(current)
            if (
                current_token is None
                and len(normalize(current_title)) >= 12
                and re.match(
                    r"^\d{1,2}[.、]?(?=[\u4e00-\u9fffA-Za-z])",
                    current.replace(" ", ""),
                )
                and line_index + 1 < len(lines)
            ):
                next_title, next_token = _parse_scan_toc_line(lines[line_index + 1])
                if next_token and next_title and not re.match(
                    r"^\d{1,2}[.、]?(?=[\u4e00-\u9fffA-Za-z])",
                    next_title.replace(" ", ""),
                ):
                    separator = " " if len(re.findall(
                        r"[A-Za-z]", current + lines[line_index + 1],
                    )) >= 4 else ""
                    coalesced.append(current + separator + lines[line_index + 1])
                    line_index += 2
                    continue
            coalesced.append(current)
            line_index += 1
        lines = coalesced
        if lines and not had_explicit_heading and "目录" not in lines[0]:
            parsed_first, _parsed_token = _parse_scan_toc_line(lines[0])
            first_key = normalize(parsed_first or lines[0])
            first_resolves = first_key in title_pages or any(first_key in head for head in title_heads.values())
            if not _TOC_HEADING_RE.match(lines[0]) and (
                first_line_counts.get(first_key, 0) >= 2
                or body_first_counts.get(first_key, 0) >= 3
                or not first_resolves
            ):
                lines = lines[1:]
        for line in lines:
            compact = line.replace(" ", "")
            if compact in _TOC_NOISE:
                continue
            title, token = _parse_scan_toc_line(line)
            if token and token.isdigit() and last_toc_numeric and int(token) < last_toc_numeric:
                # Multi-column English contents often put a subsection number at the
                # left and its printed folio at the right.  Visual row extraction can
                # therefore yield either ``title 1`` followed by ``continuation 73``
                # or ``title 182 6``.  A regressing final number is the subsection
                # ordinal, never a page jump backwards.
                embedded_folio = re.match(r"^(?P<title>.+?)\s+(?P<folio>\d{1,4})$", title)
                if embedded_folio and int(embedded_folio.group("folio")) >= last_toc_numeric:
                    title = _valid_toc_title(
                        f"{token} {embedded_folio.group('title')}"
                    )
                    token = str(int(embedded_folio.group("folio")))
                else:
                    ordinal_part = _valid_toc_title(f"{token} {title}")
                    if ordinal_part:
                        pending_parts.append(ordinal_part)
                    continue
            if token is None and _DATE_LINE_RE.fullmatch(compact):
                flush_without_token()
                continue
            if token is None:
                candidate = _valid_toc_title(line)
                is_part_heading = bool(candidate and re.search(
                    r"第[一二三四五六七八九十0-9]+部分$", normalize(candidate),
                ))
                if is_part_heading:
                    pending_parts = []  # discard a preceding wrapped author credit
                    pending_heading = candidate
                elif candidate and _TOC_HEADING_RE.match(candidate):
                    flush_without_token()
                    pending_heading = candidate
                elif candidate:
                    pending_parts.append(candidate)
                continue
            if not title and not pending_parts and not pending_heading:
                continue
            pending_raw = "".join(pending_parts)
            pending_latin = len(re.findall(r"[A-Za-z]", pending_raw)) >= max(
                4, int(len(re.sub(r"\s+", "", pending_raw)) * 0.45),
            )
            wrapped_title = _valid_toc_title(
                (" " if pending_latin else "").join(pending_parts)
            )
            if not title:
                title = wrapped_title
            elif wrapped_title and not pending_heading:
                separator = " " if pending_latin else ""
                combined = _valid_toc_title(f"{wrapped_title}{separator}{title}")
                combined_key = normalize(combined)
                structured_wrap = bool(re.match(
                    r"^[一二三四五六七八九十]+(?=[\u4e00-\u9fff])",
                    pending_raw.replace(" ", ""),
                ))
                body_verified_wrap = bool(
                    combined_key in title_pages
                    or any(combined_key in head for head in title_heads.values())
                )
                if pending_latin or structured_wrap or body_verified_wrap:
                    title = combined
            pending_parts = []
            if token and token.isdigit():
                last_toc_numeric = max(last_toc_numeric, int(token))
            target, printed = resolve_target(title, token)
            if pending_heading:
                if (
                    len(normalize(pending_heading)) >= 12
                    and re.match(
                        r"^\d{1,2}[.、]?(?=[\u4e00-\u9fffA-Za-z])",
                        pending_heading.replace(" ", ""),
                    )
                    and not re.match(
                    r"^\d{1,2}[.、]?(?=[\u4e00-\u9fffA-Za-z])",
                    title.replace(" ", ""),
                    )
                ):
                    title = pending_heading + title
                    target, printed = resolve_target(title, token)
                    pending_heading = ""
                    chapter_open = False
                else:
                    chapter_title = pending_heading
                    if wrapped_title and normalize(wrapped_title) != normalize(title):
                        # OCR 常把“第 1 章”、章名和首个小节拆成三行，页码只放
                        # 在小节行尾。中间的无页码文字是章名，不应被静默丢弃。
                        chapter_title = f"{pending_heading} {wrapped_title}"
                    heading_target, heading_printed = resolve_target(chapter_title, token)
                    add(chapter_title, heading_target, heading_printed, 1, "chapter")
                    pending_heading = ""
                    chapter_open = True
            compact_title = title.replace(" ", "")
            is_top = compact_title in _TOC_TOP_LEVEL
            reflow_chapter = bool(
                reflowed_layout and (
                    compact_title == "概论"
                    or re.match(r"^[一二三四五六七八九十]+(?=[\u4e00-\u9fff])", compact_title)
                )
            )
            if reflow_chapter:
                add(title, target, printed, 1, "chapter")
                chapter_open = True
            else:
                add(title, target, printed, 1 if is_top or not chapter_open else 2,
                    "front" if is_top and not chapter_open else "section")
    flush_without_token()
    out.sort(key=lambda item: (item["pdf_page"], item["sort_order"]))
    for order, item in enumerate(out):
        item["sort_order"] = order
        item["_toc_evidence_pdf_page"] = toc_pages[0][0]
    return out


def _derive_toc_from_page_headings(page_items: list[dict], page_rows: list[tuple]) -> list[dict]:
    """无标准目录的文稿汇编兜底：仅采用“前页有出处收束 + 本页短标题”的可核验边界。"""
    if not page_rows:
        return []
    total_pages = max(int(row[3]) for row in page_rows)
    printed_by_pdf = {int(row[3]): row[4] for row in page_rows}
    page_text = {}
    first_lines: dict[int, list[str]] = {}
    for item in page_items:
        try:
            pno = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "")
        page_text[pno] = text
        first_lines[pno] = [_clean_title(line) for line in text.splitlines() if _clean_title(line)][:5]

    repeated_heads = Counter(
        normalize(lines[0]) for lines in first_lines.values() if lines and len(normalize(lines[0])) >= 4
    )

    def previous_closes_entry(pno: int) -> bool:
        previous = page_text.get(pno - 1, "")
        if pno == 2 and len(previous.strip()) <= 260:
            return True
        previous_lines = [_clean_title(line) for line in previous.splitlines() if _clean_title(line)]
        if previous_lines and re.fullmatch(r"[一二三四五六七八九十]{1,3}", previous_lines[-1]):
            return False
        tail = previous[-900:]
        return bool(re.search(
            r"(?:第\s*\d+\s*页|出版社\s*(?:19|20)\d{2}\s*年版|出版社单行本|"
            r"《[^》]{2,80}》\s*[（(]?(?:19|20|二[〇○O0])|\d{4}\s*年\s*\d{1,2}\s*月)",
            tail,
        ))

    out: list[dict] = []
    for pno in sorted(first_lines):
        if pno <= 1 or pno > total_pages or not previous_closes_entry(pno):
            continue
        lines = first_lines[pno]
        if not lines:
            continue
        first = lines[0]
        compact = first.replace(" ", "")
        invalid_first = (
            len(compact) < 4 or len(compact) > 36
            or repeated_heads.get(normalize(first), 0) >= 3
            or re.search(r"[。.!?！？；：:]", first)
            or re.match(r"^[一二三四五六七八九十0-9（(《]", compact)
            or compact.startswith(("值此", "本次汇编"))
        )
        title = "" if invalid_first else first
        if first.endswith(("，", ",")) and len(lines) > 1:
            second = lines[1]
            if 2 <= len(second.replace(" ", "")) <= 30 and not re.search(r"[。！？；：:]", second):
                title += second
            else:
                title = ""
        if not title:
            tail_lines = [
                _clean_title(line) for line in page_text.get(pno, "").splitlines()[-12:]
                if _clean_title(line)
            ]
            source_titles = []
            for start in range(len(tail_lines)):
                for width in (1, 2, 3):
                    joined = "".join(tail_lines[start:start + width])
                    source_match = re.match(
                        r"^((?:致(?!以)|给)[^（）()。]{2,80}?(?:回信|贺信))(?=[（(]|$)", joined
                    )
                    if source_match:
                        source_titles.append(source_match.group(1))
            title = min(source_titles, key=len) if source_titles else ""
        title = _valid_toc_title(title)
        if not title:
            continue
        out.append({
            "title": title,
            "pdf_page": pno,
            "printed_page": printed_by_pdf.get(pno),
            "level": 1,
            "kind": "section",
            "sort_order": len(out),
        })
    # 少量偶然标题不足以证明文档结构；达到 5 个独立边界才启用该兜底。
    return out if len(out) >= 5 else []


def _allows_page_heading_toc(page_items: list[dict]) -> bool:
    """The heading-only fallback is valid for verified compilations, not ordinary books."""
    numbered: list[tuple[int, str]] = []
    for item in page_items:
        try:
            pno = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if pno > 0:
            numbered.append((pno, str(item.get("text") or "")))
    if not numbered:
        return False
    numbered.sort()
    first_text = "\n".join(text for _pno, text in numbered[:3])
    tail_text = "\n".join(text for _pno, text in numbered[-4:])
    return (
        any(marker in tail_text for marker in ("本次汇编摘自", "本汇编收录", "本汇编摘自"))
        or ("摘编" in first_text and bool(re.search(r"(?:研究所|研究中心|委员会).{0,20}编", first_text)))
    )


def _toc_quality(entries: list[dict], page_items: list[dict], total_pages: int, *, source: str) -> dict:
    """给一组目录候选做与来源无关的质量评分，防“有目录数据就直接展示”。"""
    count = len(entries)
    if not count:
        return {"source": source, "score": 0.0, "count": 0, "matched_titles": 0,
                "unique_pages": 0, "monotonic_ratio": 0.0}

    pages = [int(item.get("pdf_page") or 0) for item in entries]
    ordered_pairs = max(0, len(pages) - 1)
    monotonic = (sum(1 for a, b in zip(pages, pages[1:]) if b >= a) / ordered_pairs
                 if ordered_pairs else 1.0)
    unique_pages = len(set(pages))
    normalized_titles = [
        normalize(_valid_toc_title(item.get("title"))) for item in entries
    ]
    unique_titles = len({title for title in normalized_titles if title})
    duplicate_titles = max(0, count - unique_titles)
    suspicious_titles = 0
    for item in entries:
        cleaned_title = _clean_title(item.get("title"))
        compact = cleaned_title.replace(" ", "")
        latin_words = re.findall(r"[A-Za-z][A-Za-z'’-]*", cleaned_title)
        implausibly_long = len(compact) > 180 or (
            len(compact) > 100 and len(latin_words) < 8
        )
        if (
            len(normalize(compact)) < 2
            or implausibly_long
            or bool(re.fullmatch(r"(?:\d{1,4}[A-Za-zOoZzGg]?|[A-Za-zOoZzGg])", compact))
            # Keep whitespace here: ``1 On Reasoning`` is a normal English heading,
            # while ``10O``/``3Z`` without a separator is a genuine OCR digit swap.
            or bool(re.search(r"(?:^|\D)\d{1,3}[OoZz](?:\D|$)", cleaned_title))
        ):
            suspicious_titles += 1

    page_heads: dict[int, str] = {}
    for item in page_items:
        try:
            pno = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        lines = [_clean_title(line) for line in str(item.get("text") or "").splitlines()[:18]]
        page_heads[pno] = normalize(" ".join(line for line in lines if line))
    matched = 0
    for item in entries:
        title = normalize(_valid_toc_title(item.get("title")))
        # 去掉章序号再比一次，兼容正文把“1 技艺”拆成“1”与“技艺”两行。
        bare = re.sub(r"^(?:第?[一二三四五六七八九十百0-9]+[编部篇章节卷]?|[0-9]+)", "", title)
        head = page_heads.get(int(item.get("pdf_page") or 0), "")
        if title and (title in head or (len(bare) >= 2 and bare in head)):
            matched += 1

    count_score = min(1.0, count / 5.0)
    spread_score = min(1.0, unique_pages / max(2.0, min(float(count), 8.0)))
    match_ratio = matched / count
    score = 0.20 * count_score + 0.20 * spread_score + 0.20 * monotonic + 0.40 * match_ratio
    suspicious_ratio = suspicious_titles / count
    if suspicious_titles:
        score *= max(0.45, 1.0 - suspicious_ratio * 2.0)
    # PDF 书签常把同一页眉复制到几十个目标页；旧评分只看“目标页能否找到标题”，
    # 重复页眉反而会得到高分并长期压过真正的印刷目录。按重复比例惩罚，但允许同名
    # “前言/附录”偶尔出现两次，不因一个重复项直接否决整份目录。
    if count >= 5 and duplicate_titles:
        duplicate_ratio = duplicate_titles / count
        score *= max(0.45, 1.0 - duplicate_ratio * 1.5)
    # 大量书签挤在同一页是常见坏 PDF；无论标题多漂亮都不能直接当可靠目录。
    if count >= 5 and unique_pages <= 1:
        score *= 0.35
    if any(page < 1 or (total_pages and page > total_pages) for page in pages):
        score *= 0.5
    # A long printed contents list is independently strong evidence when nearly all
    # targets are ordered, well spread and titles are not OCR artefacts.  English
    # books often omit exact running heads, so body-title matching alone must not keep
    # an otherwise clean physical contents page below the publication gate.
    if (
        source == "printed_contents" and count >= 12 and monotonic >= 0.98
        and unique_pages / count >= 0.75 and unique_titles / count >= 0.9
        and suspicious_titles <= max(1, int(count * 0.02))
    ):
        score = max(score, 0.72)
    return {
        "source": source,
        "score": round(max(0.0, min(1.0, score)), 3),
        "count": count,
        "matched_titles": matched,
        "unique_pages": unique_pages,
        "unique_titles": unique_titles,
        "duplicate_titles": duplicate_titles,
        "suspicious_titles": suspicious_titles,
        "suspicious_ratio": round(suspicious_ratio, 3),
        "monotonic_ratio": round(monotonic, 3),
    }


def _choose_toc(
    explicit: list[dict],
    derived: list[dict],
    headings: list[dict],
    legacy: list[dict],
    page_items: list[dict],
    total_pages: int,
    page_quality: dict,
) -> tuple[list[dict], dict]:
    """在 PDF 书签、正文目录、篇章标题和旧版派生目录之间自动择优。

    ``legacy`` 只由版本回填传入：新规则能修复旧目录，但当原 PDF 和当前文字层
    都无法重建出同等质量时，仍可保留明显更优的旧目录，避免升级退化。
    """
    embedded_q = _toc_quality(explicit, page_items, total_pages, source="pdf_outline")
    derived_q = _toc_quality(derived, page_items, total_pages, source="printed_contents")
    headings_q = _toc_quality(headings, page_items, total_pages, source="page_headings")
    legacy_q = _toc_quality(legacy, page_items, total_pages, source="legacy_preserved")
    # 正文目录依赖页码映射；无稳定印刷页证据时降低可信度，但标题实页匹配仍可托底。
    if derived_q["count"]:
        page_confidence = float(page_quality.get("page_confidence") or 0.0)
        derived_q["score"] = round(
            min(1.0, float(derived_q["score"]) * 0.88 + page_confidence * 0.12), 3
        )

    chosen: list[dict] = []
    chosen_q = {"source": "none", "score": 0.0, "count": 0}
    # 允许很短但标题实页完全匹配的两级书签；普通候选须达到 0.45。
    embedded_ok = embedded_q["score"] >= 0.45 or (
        embedded_q["count"] <= 3 and embedded_q["count"] > 0
        and embedded_q["matched_titles"] == embedded_q["count"]
    )
    derived_ok = derived_q["score"] >= 0.45
    headings_ok = headings_q["score"] >= 0.62 and headings_q["matched_titles"] == headings_q["count"]
    if embedded_ok and (not derived_ok or embedded_q["score"] >= derived_q["score"] - 0.02):
        chosen, chosen_q = explicit, embedded_q
    elif headings_ok and (
        not derived_ok or headings_q["score"] >= derived_q["score"] + 0.05
    ):
        # A single accidental title/folio pair must not suppress dozens of verified
        # article boundaries in an institutional compilation.
        chosen, chosen_q = headings, headings_q
    elif derived_ok:
        chosen, chosen_q = derived, derived_q
    elif headings_ok:
        chosen, chosen_q = headings, headings_q

    # 旧目录必须在新评分下仍然可靠，且比重建候选至少高 3 个百分点才保留。
    # 这取代了旧逻辑的“无条件原样灌回”，使错误目录能随管线升级得到修复。
    if (
        legacy_q["score"] >= 0.70
        and (
            not chosen
            or (
                float(chosen_q.get("score") or 0.0) < 0.70
                and legacy_q["score"] >= float(chosen_q.get("score") or 0.0) + 0.03
            )
        )
    ):
        chosen, chosen_q = legacy, legacy_q

    warnings: list[str] = []
    if not chosen and (explicit or derived or headings or legacy):
        warnings.append("目录候选可信度不足，已隐藏以避免错误跳转")
    if page_quality.get("page_mapping_mode") == "reflowed_explicit":
        warnings.append("检测到重排版 PDF；仅采用正文中可直接核验的原书页码，不据此判断缺页")
    elif not page_quality.get("page_offset") and not page_quality.get("page_segments") and page_items:
        warnings.append("未识别稳定印刷页码，引文将使用 PDF 页码")
    for gap in page_quality.get("page_gaps") or []:
        missing = gap.get("missing_printed_pages") if isinstance(gap, dict) else []
        if missing:
            warnings.append(
                "原 PDF 缺少书页 " + "、".join(str(value) for value in missing[:12])
                + (" 等" if len(missing) > 12 else "")
            )
        elif isinstance(gap, dict) and int(gap.get("missing_page_count") or 0) > 0:
            possible = gap.get("possible_printed_range") or []
            interval = (
                f"（书页 {possible[0]}–{possible[-1]} 之间）" if possible else ""
            )
            warnings.append(
                f"原 PDF 疑似缺少 {int(gap['missing_page_count'])} 个物理页{interval}"
            )
    quality = {
        **page_quality,
        "toc_source": chosen_q.get("source", "none"),
        "toc_confidence": float(chosen_q.get("score") or 0.0),
        "toc_evidence_pdf_pages": sorted({
            int(item.get("_toc_evidence_pdf_page") or 0)
            for item in chosen
            if int(item.get("_toc_evidence_pdf_page") or 0) > 0
        }),
        "toc_candidates": {
            "pdf_outline": embedded_q,
            "printed_contents": derived_q,
            "page_headings": headings_q,
            "legacy_preserved": legacy_q,
        },
        "warnings": warnings,
    }
    return chosen, quality


def write_book_index(
    user_id: int,
    submission_id: int,
    pages: list[dict],
    *,
    toc_entries: list[dict] | None = None,
    legacy_toc_entries: list[dict] | None = None,
) -> int:
    """把某本书的逐页文本写入该用户的个人索引（先删后插，可重复执行）。

    pages: [{"page": 1, "text": "..."}, ...]（来自存储节点的 /text）
    返回实际写入的页数（跳过空白页——空页进索引只会稀释检索）。
    """
    key = personal_book_key(submission_id)
    src = source_file_for(user_id, submission_id)
    printed_map, page_quality = _printed_page_analysis(pages)
    text_quality = assess_text_layer_quality(pages)
    rows: list[tuple] = []
    for item in pages:
        try:
            pno = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").strip()
        if pno <= 0 or not text:
            continue
        rows.append((key, 1, src, pno, printed_map.get(pno), text, normalize(text)))
    rows.sort(key=lambda row: row[3])
    # 目录边界应以 PDF 页数为准，不能以“非空文字页”的最大页号为准。
    # 否则封底前的图片页/空白页会让合法 PDF 书签被误删。
    total_pages = max(
        (
            int(item.get("page") or 0)
            for item in pages
            if isinstance(item, dict) and str(item.get("page") or "").isdigit()
        ),
        default=max((r[3] for r in rows), default=0),
    )
    explicit_toc = _normalize_explicit_toc(toc_entries, total_pages)
    legacy_toc = _normalize_explicit_toc(legacy_toc_entries, total_pages)
    derived_toc = _derive_toc_from_pages(pages, rows, page_quality)
    heading_toc = (
        _derive_toc_from_page_headings(pages, rows)
        if _allows_page_heading_toc(pages) else []
    )
    toc, quality = _choose_toc(
        explicit_toc, derived_toc, heading_toc, legacy_toc,
        pages, total_pages, page_quality,
    )
    quality["text_layer"] = text_quality
    # A verified printed-contents entry can safely extend an already-established folio
    # segment a few front pages backwards. This covers books whose page 1 footer was
    # lost by OCR while pages 10 onward form a strong, identical-offset sequence.
    inferred = 0
    for item in toc:
        token = str(item.get("printed_page") or "")
        pdf_page = int(item.get("pdf_page") or 0)
        for segment in page_quality.get("page_segments") or []:
            start = int(segment.get("pdf_start") or 0)
            offset = int(segment.get("offset") or -9999)
            if not (0 < start - pdf_page <= 30):
                continue
            if token.isdigit():
                if pdf_page - int(token) != offset:
                    continue
            else:
                inferred_printed = pdf_page - offset
                if inferred_printed < 1:
                    continue
                token = str(inferred_printed)
                item["printed_page"] = token
            old_start = start
            segment["pdf_start"] = pdf_page
            segment["printed_start"] = int(token)
            for pno in range(pdf_page, old_start):
                printed_map.setdefault(pno, str(pno - offset))
                inferred += 1
            break
    if inferred:
        page_quality["page_inferred_from_toc"] = inferred
        quality["page_segments"] = page_quality.get("page_segments") or []
        quality["page_inferred_from_toc"] = inferred
        rows = [
            (row[0], row[1], row[2], row[3], printed_map.get(int(row[3]), row[4]), row[5], row[6])
            for row in rows
        ]
    printed_by_pdf = {int(row[3]): row[4] for row in rows}
    toc_rows = [
        (key, 1, src, item["title"], int(item["pdf_page"]),
         item.get("printed_page", printed_by_pdf.get(int(item["pdf_page"]))), int(item.get("level") or 1),
         str(item.get("kind") or "body"), int(item.get("sort_order") or order))
        for order, item in enumerate(toc)
    ]
    with _index_write_lock(user_id):
        conn = _connect(user_id)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM pages WHERE book = ?", (key,))
            conn.execute("DELETE FROM toc_entries WHERE book = ?", (key,))
            if rows:
                conn.executemany(
                    "INSERT INTO pages (book, volume, source_file, pdf_page, printed_page, "
                    "raw_text, normalized_text) VALUES (?,?,?,?,?,?,?)",
                    rows,
                )
            if toc_rows:
                conn.executemany(
                    "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, "
                    "printed_page, level, kind, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
                    toc_rows,
                )
            conn.execute(
                "INSERT INTO derivative_meta (book, pipeline_version, page_count, toc_count, quality_json, updated_at) "
                "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(book) DO UPDATE SET pipeline_version=excluded.pipeline_version, "
                "page_count=excluded.page_count, toc_count=excluded.toc_count, "
                "quality_json=excluded.quality_json, "
                "updated_at=CURRENT_TIMESTAMP",
                (key, INDEX_PIPELINE_VERSION, len(rows), len(toc_rows),
                 json.dumps(quality, ensure_ascii=False, separators=(",", ":"))),
            )
            conn.commit()
        finally:
            conn.close()
    invalidate(user_id)
    return len(rows)


def read_book_pages(user_id: int, submission_id: int) -> list[dict]:
    """读取既有个人索引的逐页文本，供版本升级自愈；不触碰远端 PDF/OCR。"""
    path = index_path(user_id)
    if not path.exists():
        return []
    key = personal_book_key(submission_id)
    conn = _connect(user_id)
    try:
        rows = conn.execute(
            "SELECT pdf_page, printed_page, raw_text FROM pages WHERE book = ? ORDER BY pdf_page",
            (key,),
        ).fetchall()
        return [{"page": row[0], "printed_page": row[1], "text": row[2]} for row in rows]
    finally:
        conn.close()


def get_book_page(
    user_id: int,
    submission_id: int,
    pdf_page: int,
    *,
    include_unpublished: bool = False,
) -> dict | None:
    """读取单页可选择文字及同源引文。

    默认只从已上架、可检索的个人 Corpus 生成引文。内部验收可显式传
    ``include_unpublished=True`` 读取同一用户、同一提交的候选索引；该分支仍
    强制数据库归属校验，不得用于公开检索路由。
    """
    pno = int(pdf_page)
    if pno < 1 or not index_path(user_id).exists():
        return None
    key = personal_book_key(submission_id)
    src = source_file_for(user_id, submission_id)
    conn = _connect(user_id)
    try:
        row = conn.execute(
            "SELECT printed_page, raw_text FROM pages WHERE book = ? AND pdf_page = ? LIMIT 1",
            (key, pno),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    text = str(row[1] or "")
    corpus = get_personal_corpus(int(user_id))
    if include_unpublished and (
        corpus is None or corpus.get_volume_by_source_file(src) is None
    ):
        corpus = get_submission_corpus(int(user_id), int(submission_id))
    citation = ""
    citations: dict[str, str] = {}
    section_title = ""
    if corpus is not None:
        volume = corpus.get_volume_by_source_file(src)
        page_obj = None
        if volume is not None:
            page_obj = next((page for page in volume.pages if page.pdf_page == pno), None)
        if page_obj is None:
            page_obj = Page(pno, row[0], text, normalize(text))
        citation = corpus._make_citation(key, 1, [page_obj], source_file=src)
        citations = corpus._make_citations(key, 1, [page_obj], source_file=src)
        section = corpus.get_chapter_for_page(src, pno)
        section_title = section.title if section else ""
    return {
        "pdf_page": pno,
        "printed_page": row[0],
        "text": text,
        "section_title": section_title,
        "citation": citation,
        "citations": citations,
    }


def book_derivative_stats(user_id: int, submission_id: int) -> dict:
    path = index_path(user_id)
    if not path.exists():
        return {"pages": 0, "toc": 0, "version": 0, "quality": {}}
    key = personal_book_key(submission_id)
    conn = _connect(user_id)
    try:
        row = conn.execute(
            "SELECT pipeline_version, page_count, toc_count, quality_json "
            "FROM derivative_meta WHERE book = ?",
            (key,),
        ).fetchone()
        if row:
            try:
                quality = json.loads(str(row[3] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                quality = {}
            return {"version": int(row[0]), "pages": int(row[1]), "toc": int(row[2]),
                    "quality": quality if isinstance(quality, dict) else {}}
        pages = conn.execute("SELECT COUNT(*) FROM pages WHERE book = ?", (key,)).fetchone()[0]
        toc = conn.execute("SELECT COUNT(*) FROM toc_entries WHERE book = ?", (key,)).fetchone()[0]
        return {"version": 0, "pages": int(pages), "toc": int(toc), "quality": {}}
    finally:
        conn.close()


def get_book_toc(user_id: int, submission_id: int) -> list[dict]:
    """直接从该用户独立索引读目录，供个人阅读器使用（不要求该书有可检索文字层）。"""
    path = index_path(user_id)
    if not path.exists():
        return []
    key = personal_book_key(submission_id)
    conn = _connect(user_id)
    try:
        rows = conn.execute(
            "SELECT title, pdf_page, printed_page, level, kind, sort_order "
            "FROM toc_entries WHERE book = ? ORDER BY sort_order, pdf_page, id",
            (key,),
        ).fetchall()
        return [
            {"title": row[0], "pdf_page": int(row[1]), "printed_page": row[2],
             "level": int(row[3] or 1), "kind": row[4] or "body",
             "sort_order": int(row[5] or 0)}
            for row in rows
        ]
    finally:
        conn.close()


def drop_book_index(user_id: int, submission_id: int) -> None:
    """删书时清掉它在个人索引中的所有行。"""
    key = personal_book_key(submission_id)
    if not index_path(user_id).exists():
        invalidate(user_id)
        return
    conn = _connect(user_id)
    try:
        conn.execute("DELETE FROM pages WHERE book = ?", (key,))
        conn.execute("DELETE FROM toc_entries WHERE book = ?", (key,))
        conn.execute("DELETE FROM derivative_meta WHERE book = ?", (key,))
        conn.commit()
    finally:
        conn.close()
    invalidate(user_id)


# ---------- 每用户 Corpus ----------
class PersonalCorpus(Corpus):
    """只承载某一位用户个人文库的 Corpus 实例。

    与全局 corpus 的隔离是**构造性**的：个人书从不写入全局 `corpus.books`，而是各自
    实例化。因此不存在「忘了加过滤条件就串号」这类风险——别人的实例里根本没有这些书。
    """

    def __init__(self, db_path: Path, meta: dict[str, dict]) -> None:
        # 必须在 super().__init__ 之前赋值：父类构造过程中会回调 _load_manifest。
        self._pl_meta: dict[str, dict] = dict(meta)
        super().__init__(db_path=db_path)
        # 只保留真正有内容的书库键：父类会把 books.yaml 里所有官方书库预置成空列表，
        # 对个人 Corpus 是纯噪声（也让 scope 计算多绕路）。
        self.books = {k: v for k, v in self.books.items() if v}
        # 卷标题用用户填写的书名（父类默认取 source_file 的 stem，对个人书没有意义）。
        for key, info in self._pl_meta.items():
            for vol in self.books.get(key, []):
                vol.display_title = info.get("title") or key

    def _load_manifest(self) -> None:
        # 个人书不在全局 manifest 里，跳过其解析（省一次大文件读），只登记本人的书库键。
        # 关键：必须在父类 _load() 之前把键放进 self.books，否则 _load 会以
        # 「book not in self.books」为由静默丢弃所有个人书的页（search.py 中的既有行为）。
        for key in self._pl_meta:
            self.books.setdefault(key, [])

    def get_book_config(self, book: str) -> BookConfig:
        info = self._pl_meta.get(book)
        if not info:
            return super().get_book_config(book)
        bib = info.get("bibliographic") if isinstance(info.get("bibliographic"), dict) else {}
        title = str(bib.get("title") or info.get("title") or book)
        bib_authors = bib.get("authors") if isinstance(bib.get("authors"), list) else []
        authors = tuple(str(a).strip() for a in bib_authors if str(a).strip())
        if not authors:
            authors = tuple(a for a in [str(info.get("author") or "").strip()] if a)
        translators = tuple(
            str(a).strip() for a in (bib.get("translators") or []) if str(a).strip()
        ) if isinstance(bib.get("translators"), list) else ()
        return BookConfig(
            key=book,
            title=f"《{title}》",
            short_title=title,
            citation_title=title,
            folder="",
            sort_order=100000,          # 恒排在官方书库之后
            publisher=str(bib.get("publisher") or ""),
            place=str(bib.get("place") or ""),
            tag_class="book-mylib",
            available=True,
            single_volume=True,         # 个人书不分卷，引文不冠「第N卷」
            authors=authors,
            translators=translators,
        )

    def _bibliographic(self, book: str) -> dict:
        info = self._pl_meta.get(book) or {}
        value = info.get("bibliographic")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _personal_page_label(pages: list[Page], *, gb: bool = False) -> str:
        printed = [str(page.printed_page) for page in pages if page.printed_page]
        if printed:
            def display(value: str) -> str:
                return value[4:].upper() if value.startswith("pre-") else value

            first, last = display(printed[0]), display(printed[-1])
            value = first if first == last else f"{first}-{last}"
            return value if gb else f"第{value}页"
        pdf = [int(page.pdf_page) for page in pages]
        first, last = pdf[0], pdf[-1]
        value = str(first) if first == last else f"{first}-{last}"
        return f"PDF第{value}页"

    def _make_citation(self, book: str, volume: int, pages: list[Page], source_file: str | None = None) -> str:
        """个人上传书缺出版地/出版社/年份时，只用已知元数据，绝不套公共书库默认值。"""
        cfg = self.get_book_config(book)
        bib = self._bibliographic(book)
        author = "、".join(cfg.authors)
        prefix = f"{author}：" if author else ""
        translator = f"，{'、'.join(cfg.translators)}译" if cfg.translators else ""
        publisher = str(bib.get("publisher") or "")
        place = str(bib.get("place") or "")
        year = str(bib.get("year") or "")
        is_compilation = str(bib.get("document_type") or "") == "compilation"
        is_translation = str(bib.get("document_type") or "") == "translation_manuscript"
        is_editor = str(bib.get("responsibility_role") or "") == "editor"
        publication = ""
        if publisher:
            publication = f"，{place + '：' if place else ''}{publisher}"
        if year:
            publication += f"，{year}年"
        responsibility = "编" if is_compilation and author else ("主编" if is_translation and is_editor and author else "")
        document_note = "（机构汇编）" if is_compilation else ""
        if is_translation:
            original = bib.get("original_edition") if isinstance(bib.get("original_edition"), dict) else {}
            original_publisher = str(original.get("publisher") or "")
            original_year = str(original.get("year") or "")
            basis = ""
            if original_publisher or original_year:
                basis = "；据" + " ".join(value for value in (original_publisher, original_year + "年版" if original_year else "") if value)
            document_note = f"（未出版译稿{basis}）"
        return (
            f"{author + responsibility + '：' if author else ''}《{cfg.citation_title}》"
            f"{document_note}{translator}{publication}，"
            f"{self._personal_page_label(pages)}。"
        )

    def _make_citation_gb(self, book: str, volume: int, pages: list[Page], source_file: str | None = None) -> str:
        cfg = self.get_book_config(book)
        bib = self._bibliographic(book)
        author = ",".join(cfg.authors)
        prefix = f"{author}." if author else ""
        page = self._personal_page_label(pages, gb=True)
        publisher = str(bib.get("publisher") or "")
        place = str(bib.get("place") or "")
        year = str(bib.get("year") or "")
        is_compilation = str(bib.get("document_type") or "") == "compilation"
        is_translation = str(bib.get("document_type") or "") == "translation_manuscript"
        if is_compilation or is_translation or not publisher:
            # [Z] 是 GB/T 7714 的“其他文献”类型；出版项未知时不伪造占位出版地/年份。
            if year:
                return f"{prefix}{cfg.citation_title}[Z].{year}:{page}."
            return f"{prefix}{cfg.citation_title}[Z].{page}."
        translator = f".{','.join(cfg.translators)},译" if cfg.translators else ""
        publication = ""
        if publisher:
            publication = f".{place + ':' if place else ''}{publisher}"
        if year:
            publication += f",{year}" if publication else f".{year}"
        return f"{prefix}{cfg.citation_title}[M]{translator}{publication}:{page}."


def _signature(books: list[dict]) -> str:
    """用户书目指纹。变了就重建 Corpus——把缓存失效变成自动行为，杜绝陈旧命中。"""
    return ",".join(
        f"{b['id']}:{b.get('parsed_at') or ''}:{b.get('bibliographic_json') or '{}'}"
        for b in books
    )


def invalidate(user_id: int) -> None:
    with _CACHE_LOCK:
        _CACHE.pop(int(user_id), None)


def invalidate_all() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _load_corpus_for_books(user_id: int, books: list[dict]) -> PersonalCorpus | None:
    """从明确给定的书目加载 Corpus；调用方负责决定可见性范围。"""
    uid = int(user_id)
    path = index_path(uid)
    if not books or not path.exists():
        return None
    meta = {
        personal_book_key(book["id"]): {
            "title": book.get("title") or "",
            "author": book.get("author") or "",
            "bibliographic": plib.submission_bibliographic(book),
        }
        for book in books
    }
    try:
        corpus = PersonalCorpus(path, meta)
    except Exception as exc:  # noqa: BLE001 — 个人索引损坏不应拖垮整站检索
        LOGGER.warning("personal corpus load failed uid=%s path=%s: %s", uid, path, exc)
        return None
    return corpus if corpus.books else None


def get_submission_corpus(user_id: int, submission_id: int) -> PersonalCorpus | None:
    """加载单本候选索引，仅供解析验收/管理员复核内部调用。"""
    uid, sid = int(user_id), int(submission_id)
    row = plib.get_submission(sid, uid)
    if not row or row.get("status") == "deleted":
        return None
    return _load_corpus_for_books(uid, [row])


def get_personal_corpus(user_id: int) -> PersonalCorpus | None:
    """取该用户的个人 Corpus；没有可检索的书则返回 None（调用方据此跳过个人检索）。

    缓存以「书目指纹」为准而非显式失效调用：只要用户增删书或重新解析，指纹即变，
    自动重建。这样即便某条调用路径忘了调 invalidate 也不会读到陈旧数据。
    """
    uid = int(user_id)
    books = plib.list_searchable_books(uid)
    if not books:
        invalidate(uid)
        return None
    sig = _signature(books)
    with _CACHE_LOCK:
        hit = _CACHE.get(uid)
        if hit and hit[0] == sig:
            _CACHE.move_to_end(uid)
            return hit[1]

    corpus = _load_corpus_for_books(uid, books)
    if corpus is None:
        return None

    with _CACHE_LOCK:
        _CACHE[uid] = (sig, corpus)
        _CACHE.move_to_end(uid)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return corpus


def search_book(user_id: int, submission_id: int, query: str, limit: int = 30) -> list:
    """只检索当前用户指定的一本书，供个人阅读器就地检索。

    直接限定 book key，避免用户有多本书时先被其它书的命中占满全局 ``max_results`` 再过滤。
    """
    corpus = get_personal_corpus(int(user_id))
    key = personal_book_key(int(submission_id))
    q_norm = normalize(str(query or ""))
    if corpus is None or key not in corpus.books or len(q_norm) < 2:
        return []
    exact, _truncated = corpus._exact_in_book(key, q_norm, str(query), limit=max(1, int(limit)))
    if exact:
        return corpus._dedupe_hits(exact)[:max(1, int(limit))]
    fuzzy, _meta = corpus._fuzzy_in_book(key, q_norm, str(query))
    fuzzy.sort(key=lambda hit: -float(getattr(hit, "score", 0.0) or 0.0))
    return corpus._dedupe_hits(fuzzy)[:max(1, int(limit))]


def scope_tree_group(user_id: int) -> dict | None:
    """给检索页的「指定著作/卷」控件提供一个「我的文库」分组。无书则不出现该分组。"""
    books = plib.list_searchable_books(int(user_id))
    if not books:
        return None
    return {
        "id": "mylib",
        "label": "我的文库（私有）",
        # 字段名须与 _book_scope_tree 的官方分组一致（key/label/volumes），前端控件统一渲染。
        "books": [
            {"key": personal_book_key(b["id"]), "label": b.get("title") or "",
             "volumes": [], "personal": True}
            for b in books
        ],
    }
