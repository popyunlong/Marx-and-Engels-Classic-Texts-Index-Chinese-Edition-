"""构建《马克思恩格斯全集》目录并写入 corpus.sqlite 的 toc_entries 表（book='全集'）。

数据来源：现有 PDF 的书签 + 印刷目录页解析（复用 search.Corpus 的运行时抽取），
再做一轮 OCR 清洗（繁简归一、去点线/页码残留、修高频错字、剔除碎片噪声），
最后叠加 config/quanji_toc_overrides.yaml 的逐卷人工覆盖。

写入后所有阅读器、篇章搜索、引文搜索都会自动从 toc_entries 读取《全集》目录。
缺失/质量差的卷可在覆盖文件中逐卷修订；个别空卷不会中断构建。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402
from search import Corpus  # noqa: E402

OVERRIDES_PATH = ROOT / "config" / "quanji_toc_overrides.yaml"
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

LETTER_NUMBER_RE = re.compile(r"^\d+[．.、]\s*")
VOLUME_NOTE_RE = re.compile(r"^(第[一-鿿\d]+卷)[一-鿿]?明$")
DOT_LEADER_RE = re.compile(r"[.·…⋯・．‧•]{2,}")
TRAILING_PAGE_RE = re.compile(r"[\s—\-]+(?:[IVXLCDMivxlcdm]{1,7}|\d{1,4})$")
PURE_NOISE_RE = re.compile(r"^[\s\d.IVXLCDMivxlcdm一二三四五六七八九十、，。·\-—()（）]+$")
MAKES_MAKESI_RE = re.compile(r"[馬屬属禹厨耦镭騙禺]克思")

# 繁体→简体的高频字（语料本为简体，OCR 误识为繁体，归一总是正确且安全）。
TRAD_SIMP = str.maketrans(
    {
        "馬": "马", "屬": "属", "蘇": "苏", "黨": "党", "經": "经", "濟": "济", "報": "报",
        "國": "国", "會": "会", "義": "义", "譯": "译", "階": "阶", "級": "级", "産": "产",
        "軍": "军", "學": "学", "對": "对", "點": "点", "號": "号", "議": "议", "論": "论",
        "雜": "杂", "誌": "志", "編": "编", "輯": "辑", "關": "关", "於": "于", "與": "与",
        "問": "问", "題": "题", "戰": "战", "爭": "争", "歷": "历", "傑": "杰", "鬥": "斗",
        "鈔": "钞", "聯": "联", "權": "权", "務": "务", "後": "后", "選": "选",
    }
)

# 常见整词级 OCR 错字修正（高置信度）。
PHRASE_FIXES = {
    "意藏形态": "意识形态",
    "意藏形態": "意识形态",
}

NOISE_TITLES = {
    "封面",
    "书名",
    "书名页",
    "版权",
    "版权页",
    "扉页",
    "目录",
    "插图目录",
    "马克思像",
    "恩格斯像",
}


def clean_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()


def clean_ocr_title(text: str) -> str:
    """对 OCR 抽取的标题做清洗：繁简归一、去点线/页码、修高频错字。"""
    t = clean_title(text).translate(TRAD_SIMP)
    # 在第一段点线（目录点引线）处截断，丢弃其后的页码碎片。
    t = DOT_LEADER_RE.split(t)[0]
    t = MAKES_MAKESI_RE.sub("马克思", t)
    # “说明”的高频 OCR 错读（貌明/就明/哉明/胡明/梭明等，均非真实词），统一修正。
    t = re.sub(r"[貌就哉胡梭悦阅]明", "说明", t)
    t = VOLUME_NOTE_RE.sub(r"\1说明", t)
    for bad, good in PHRASE_FIXES.items():
        t = t.replace(bad, good)
    t = TRAILING_PAGE_RE.sub("", t)
    return clean_title(t).strip(" .·—-、，")


def entry_kind(title: str) -> str:
    if title.startswith("正文") or re.match(r"^第[一-鿿\d]+卷说明$", title):
        return "section"
    if re.fullmatch(r"\d{4}年", title):
        return "year"
    if LETTER_NUMBER_RE.match(title):
        return "letter"
    return "body"


def is_noise(title: str) -> bool:
    if not title or len(title) < 2:
        return True
    if title in NOISE_TITLES:
        return True
    if PURE_NOISE_RE.fullmatch(title):
        return True
    return False


def load_overrides() -> dict[str, Any]:
    if not OVERRIDES_PATH.exists():
        return {}
    payload = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def apply_overrides(entries: list[dict[str, Any]], volume: int, overrides: dict[str, Any]) -> list[dict[str, Any]]:
    by_volume = ((overrides.get("全集") or {}).get(volume) or {})
    remove_titles = {clean_title(item) for item in by_volume.get("remove_titles") or []}
    if remove_titles:
        entries = [entry for entry in entries if entry["title"] not in remove_titles]

    replace_entries = by_volume.get("entries")
    if isinstance(replace_entries, list) and replace_entries:
        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(replace_entries, start=1):
            if not isinstance(item, dict):
                continue
            title = clean_title(str(item.get("title") or ""))
            try:
                pdf_page = int(item.get("pdf_page") or 0)
            except (TypeError, ValueError):
                pdf_page = 0
            if not title or pdf_page < 1:
                continue
            normalized.append(
                {
                    "title": title,
                    "pdf_page": pdf_page,
                    "printed_page": str(item.get("printed_page") or "") or None,
                    "level": max(1, int(item.get("level") or 1)),
                    "kind": str(item.get("kind") or entry_kind(title)),
                    "sort_order": idx,
                }
            )
        if normalized:
            return normalized
    return entries


def build_entries_for_volume(corpus: Corpus, volume_obj, overrides: dict[str, Any]) -> list[dict[str, Any]]:
    printed_by_pdf = {page.pdf_page: page.printed_page for page in volume_obj.pages}
    page_count = len(volume_obj.pages)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for entry in corpus.get_toc_entries(volume_obj.source_file):
        title = clean_ocr_title(entry.title)
        if is_noise(title):
            continue
        pdf_page = int(getattr(entry, "pdf_page", 0) or 0)
        if pdf_page < 1 or (page_count and pdf_page > page_count):
            continue
        key = (pdf_page, title)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "title": title,
                "pdf_page": pdf_page,
                "printed_page": getattr(entry, "printed_page", None) or printed_by_pdf.get(pdf_page),
                "level": max(1, min(6, int(getattr(entry, "level", 1) or 1))),
                "kind": entry_kind(title),
                "sort_order": len(rows) + 1,
            }
        )
    rows = apply_overrides(rows, volume_obj.volume, overrides)
    for idx, row in enumerate(rows, start=1):
        row["sort_order"] = idx
    return rows


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS toc_entries (
            book         TEXT    NOT NULL,
            volume       INTEGER NOT NULL,
            source_file  TEXT    NOT NULL,
            title        TEXT    NOT NULL,
            pdf_page     INTEGER NOT NULL,
            printed_page TEXT,
            level        INTEGER NOT NULL,
            kind         TEXT    NOT NULL,
            sort_order   INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_toc_entries_source_order ON toc_entries(source_file, sort_order)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_toc_entries_book_volume ON toc_entries(book, volume, sort_order)")


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    corpus = Corpus.load_default()
    overrides = load_overrides()
    volumes = corpus.get_volumes("全集")
    if not volumes:
        raise SystemExit("没有找到《全集》卷册，无法生成目录。")

    payload: list[tuple[Any, ...]] = []
    summary: list[str] = []
    empty: list[int] = []
    for volume_obj in volumes:
        entries = build_entries_for_volume(corpus, volume_obj, overrides)
        if not entries:
            empty.append(volume_obj.volume)
        summary.append(f"全集第 {volume_obj.volume} 卷：{len(entries)} 项")
        for entry in entries:
            payload.append(
                (
                    "全集",
                    volume_obj.volume,
                    volume_obj.source_file,
                    entry["title"],
                    entry["pdf_page"],
                    entry.get("printed_page"),
                    entry["level"],
                    entry["kind"],
                    entry["sort_order"],
                )
            )

    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")  # 运行中的应用在读库，等待写锁，避免 database is locked。
        ensure_schema(conn)
        conn.execute("DELETE FROM toc_entries WHERE book = ?", ("全集",))
        conn.executemany(
            """
            INSERT INTO toc_entries (
                book, volume, source_file, title, pdf_page,
                printed_page, level, kind, sort_order
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        conn.commit()
    finally:
        conn.close()

    update_hash()
    print("\n".join(summary))
    if empty:
        print("以下卷未生成目录（可在 config/quanji_toc_overrides.yaml 中补充）：" + ", ".join(map(str, empty)))
    print(f"已写入 {len(payload)} 条《全集》目录，覆盖 {len(volumes) - len(empty)}/{len(volumes)} 卷。")


if __name__ == "__main__":
    main()
