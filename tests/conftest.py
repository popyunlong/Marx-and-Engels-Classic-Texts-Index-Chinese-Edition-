"""Hermetic process environment and deterministic miniature corpus for pytest."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(tempfile.mkdtemp(prefix="marx-search-pytest-"))
DATA_DIR = TEST_ROOT / "data"
PDF_DIR = TEST_ROOT / "pdfs"
APPDATA_DIR = TEST_ROOT / "appdata"
for directory in (DATA_DIR, PDF_DIR, APPDATA_DIR):
    directory.mkdir(parents=True, exist_ok=True)
atexit.register(lambda: shutil.rmtree(TEST_ROOT, ignore_errors=True))

os.environ["APPDATA"] = str(APPDATA_DIR)
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["TURNSTILE_ENABLED"] = "0"
os.environ["ZPAY_PID"] = "test-pid"
os.environ["ZPAY_KEY"] = "test-secret"
os.environ["APP_AI_PROVIDER"] = "deepseek"
os.environ["APP_AI_MODEL"] = "test-model"
os.environ["APP_AI_API_KEY"] = "test-api-key-not-a-real-secret"
os.environ["MARX_RUNTIME_DATA_DIR"] = str(DATA_DIR)
os.environ["MARX_RUNTIME_PDF_DIR"] = str(PDF_DIR)


def _normalize(value: str) -> str:
    # Use the exact production normalization path. In particular, CI installs
    # OpenCC while some developer machines use the supported no-OpenCC
    # fallback; a hand-written fixture normalizer would make DB text and query
    # text diverge only in the clean CI environment.
    from build_index import normalize

    return normalize(value)


def _build_corpus_fixture() -> None:
    import fitz

    config = yaml.safe_load((ROOT / "config" / "books.yaml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load((ROOT / "config" / "manifest.yaml").read_text(encoding="utf-8")) or {}
    books = [str(row["key"]) for row in config.get("books") or [] if row.get("key")]
    db_path = DATA_DIR / "corpus.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            book TEXT NOT NULL,
            volume INTEGER NOT NULL,
            source_file TEXT NOT NULL,
            pdf_page INTEGER NOT NULL,
            printed_page TEXT,
            raw_text TEXT NOT NULL,
            normalized_text TEXT NOT NULL
        );
        CREATE INDEX idx_pages_book_vol ON pages(book, volume, pdf_page);
        CREATE INDEX idx_pages_source_file ON pages(source_file, pdf_page);
        CREATE TABLE toc_entries (
            source_file TEXT NOT NULL,
            title TEXT NOT NULL,
            pdf_page INTEGER NOT NULL,
            printed_page TEXT,
            level INTEGER NOT NULL,
            kind TEXT NOT NULL,
            sort_order INTEGER NOT NULL
        );
        """
    )
    common = (
        "国家革命生产关系社会生产力资产阶级无产阶级工人阶级剩余价值劳动异化"
        "帝国主义论粮食税共产党宣言资本论"
        "中华民族伟大复兴中国式现代化生态文明法治文化党的建设共同发展实践理论。"
    )
    fixture_pdf = fitz.open()
    fixture_page = fixture_pdf.new_page()
    fixture_page.insert_text((72, 72), "pytest fixture page")
    fixture_pdf_bytes = fixture_pdf.tobytes()
    fixture_pdf.close()
    row_id = 1
    for book_index, book in enumerate(books, start=1):
        entries = manifest.get(book) or []
        first = next((row for row in entries if isinstance(row.get("volume"), int)), None)
        first_volume = int(first.get("volume")) if first else 1
        volumes = [1, 5] if book in {"文集", "全集"} else [first_volume]
        for volume in volumes:
            matching = next((row for row in entries if row.get("volume") == volume), None)
            source_file = str(
                (matching or {}).get("file")
                or f"pdfs/pytest/{book}-第{volume}卷.pdf"
            ).replace("\\", "/")
            pdf_relative = Path(source_file)
            if pdf_relative.parts and pdf_relative.parts[0].lower() == "pdfs":
                pdf_relative = Path(*pdf_relative.parts[1:])
            pdf_path = PDF_DIR / pdf_relative
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(fixture_pdf_bytes)
            title = f"{book}测试篇章第{volume}卷"
            # Keep the sampling region unique to this book/volume. Search tests
            # take a substring around offset 2000; if every fixture volume used
            # identical prose, one exact quote would fan out across the entire
            # catalogue and make citation assertions meaningless.
            # Alternate a volume-specific marker with a different CJK code
            # point at every position.  The sampling helper reads from offset
            # 2000, so its 2+ character windows must be both low-frequency and
            # unique to one fixture volume.
            volume_marker = chr(0x3400 + row_id)
            unique = title + "".join(
                volume_marker + chr(0x4E00 + offset)
                for offset in range(2100)
            )
            raw = unique + "\n\n" + ((common + title + "。") * 40)
            midpoint = len(raw) // 2
            pages = (raw[:midpoint], raw[midpoint:])
            for page_number, page_text in enumerate(pages, start=1):
                conn.execute(
                    "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        book,
                        volume,
                        source_file,
                        page_number,
                        str(page_number),
                        page_text,
                        _normalize(page_text),
                    ),
                )
                row_id += 1
            toc_rows = [
                (source_file, title, 1, "1", 1, "body", 1),
                (source_file, f"{book}生产关系专题", 2, "2", 1, "body", 2),
            ]
            # A small cross-library title set exercises ordering and exact vs.
            # prefix matches without filling the 20-result cap with one exact
            # duplicate from every configured book.
            if book in {"文集", "全集", "全集二版", "马恩选集", "列宁全集"}:
                toc_rows.extend(
                    (
                        (source_file, "共产党宣言", 1, "1", 1, "body", 3),
                        (source_file, "共产党宣言序言", 1, "1", 2, "preface", 4),
                        (source_file, "资本论", 1, "1", 1, "body", 5),
                        (source_file, "资本论第一卷序言", 1, "1", 2, "preface", 6),
                    )
                )
            conn.executemany(
                "INSERT INTO toc_entries VALUES (?, ?, ?, ?, ?, ?, ?)",
                toc_rows,
            )
    conn.commit()
    conn.close()
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    (DATA_DIR / "corpus.sqlite.sha256").write_text(digest + "\n", encoding="utf-8")
    (DATA_DIR / "release.json").write_text(
        json.dumps({"data_version": "pytest-fixture-v1"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


_build_corpus_fixture()
