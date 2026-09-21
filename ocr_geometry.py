"""Offline OCR geometry store and conservative reader-side lookup.

The public corpus text remains authoritative.  This database contains only
line boxes produced offline for PDF pages without a usable text layer.  Every
page is bound to the SHA-256 of its corpus text so a later corpus repair makes
old geometry fail closed instead of highlighting the wrong sentence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import zlib
from pathlib import Path
from typing import Iterable, Sequence

try:
    from rapidfuzz import fuzz as _rapidfuzz
except ImportError:  # The isolated OCR builder does not need query matching.
    _rapidfuzz = None

_STRIP_RE = re.compile(
    r"[\s\u3000\u2000-\u206f\u2e00-\u2e7f\u3000-\u303f\uff00-\uffef"
    r"!-/:-@\[-`\{-~]+"
)
_T2S = None
_T2S_TRIED = False


def normalize(text: str) -> str:
    """Match the corpus normalizer without importing the full index builder."""
    global _T2S, _T2S_TRIED
    value = unicodedata.normalize("NFKC", str(text or ""))
    if not _T2S_TRIED:
        _T2S_TRIED = True
        try:
            from opencc import OpenCC

            _T2S = OpenCC("t2s")
        except Exception:
            _T2S = None
    if _T2S is not None:
        try:
            value = _T2S.convert(value)
        except Exception:
            pass
    return _STRIP_RE.sub("", value)


SCHEMA_VERSION = "1"
MIN_EXACT_QUERY_CHARS = 2
MIN_QUERY_CHARS = 6  # fuzzy matching and page-quality minimum
MIN_PAGE_COVERAGE = 0.85
MIN_MEAN_CONFIDENCE = 0.75
MIN_LENGTH_RATIO = 0.65
MAX_LENGTH_RATIO = 1.45
MIN_MATCH_SCORE = 82.0
MIN_MATCH_SPAN_RATIO = 0.70
MAX_MATCH_SPAN_RATIO = 1.30


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def default_geometry_path(corpus_db: Path) -> Path:
    configured = str(os.environ.get("MARX_OCR_GEOMETRY_DB") or "").strip()
    return Path(configured).expanduser() if configured else Path(corpus_db).with_name("ocr_geometry.sqlite")


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    target = Path(path).resolve()
    if readonly:
        conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=2)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_geometry_db(path: Path, *, build_id: str = "") -> Path:
    target = Path(path).resolve()
    with _connect(target) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS geometry_meta(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS geometry_pages(
              source_file TEXT NOT NULL,
              pdf_page INTEGER NOT NULL,
              corpus_text_sha256 TEXT NOT NULL,
              pdf_sha256 TEXT NOT NULL DEFAULT '',
              page_width REAL NOT NULL,
              page_height REAL NOT NULL,
              line_count INTEGER NOT NULL DEFAULT 0,
              ocr_chars INTEGER NOT NULL DEFAULT 0,
              mean_confidence REAL NOT NULL DEFAULT 0,
              canonical_coverage REAL NOT NULL DEFAULT 0,
              length_ratio REAL NOT NULL DEFAULT 0,
              status TEXT NOT NULL,
              payload BLOB,
              error TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              PRIMARY KEY(source_file,pdf_page)
            );
            CREATE INDEX IF NOT EXISTS idx_geometry_pages_status
              ON geometry_pages(status,source_file,pdf_page);
            CREATE TABLE IF NOT EXISTS geometry_sources(
              source_file TEXT PRIMARY KEY,
              pdf_sha256 TEXT NOT NULL DEFAULT '',
              corpus_digest TEXT NOT NULL DEFAULT '',
              page_count INTEGER NOT NULL DEFAULT 0,
              textless_pages_json TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'pending',
              completed_pages INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO geometry_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        if build_id:
            conn.execute(
                "INSERT INTO geometry_meta(key,value) VALUES('build_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(build_id),),
            )
        conn.commit()
    return target


def encode_rows(rows: Sequence[dict]) -> bytes:
    compact = []
    for row in rows:
        bbox = row.get("bbox") or ()
        if len(bbox) != 4:
            continue
        compact.append(
            [
                round(float(bbox[0]), 7), round(float(bbox[1]), 7),
                round(float(bbox[2]), 7), round(float(bbox[3]), 7),
                str(row.get("text") or ""), round(float(row.get("confidence") or 0.0), 6),
            ]
        )
    raw = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return zlib.compress(raw, level=9)


def decode_rows(payload: bytes | None) -> list[list]:
    if not payload:
        return []
    value = json.loads(zlib.decompress(payload).decode("utf-8"))
    return value if isinstance(value, list) else []


def alignment_coverage(canonical: str, observed: str) -> float:
    """Fraction of canonical characters covered by order-preserving matches."""
    import difflib

    if not canonical:
        return 0.0
    matcher = difflib.SequenceMatcher(None, canonical, observed, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(canonical)


def assess_page(corpus_text: str, rows: Sequence[dict]) -> dict:
    canonical = normalize(str(corpus_text or ""))
    observed = "".join(normalize(str(row.get("text") or "")) for row in rows)
    confidences = [float(row.get("confidence") or 0.0) for row in rows if str(row.get("text") or "").strip()]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    ratio = len(observed) / max(1, len(canonical))
    coverage = alignment_coverage(canonical, observed)
    status = "ready"
    error = ""
    if len(canonical) < MIN_QUERY_CHARS:
        status, error = "skipped", "canonical-text-too-short"
    elif not observed:
        status, error = "rejected", "ocr-text-empty"
    elif not (MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO):
        status, error = "rejected", "length-ratio"
    elif mean_confidence < MIN_MEAN_CONFIDENCE:
        status, error = "rejected", "mean-confidence"
    elif coverage < MIN_PAGE_COVERAGE:
        status, error = "rejected", "canonical-coverage"
    return {
        "status": status,
        "error": error,
        "canonical_chars": len(canonical),
        "ocr_chars": len(observed),
        "mean_confidence": mean_confidence,
        "canonical_coverage": coverage,
        "length_ratio": ratio,
    }


def upsert_geometry_page(
    db_path: Path,
    *,
    source_file: str,
    pdf_page: int,
    corpus_text: str,
    pdf_sha256: str,
    page_width: float,
    page_height: float,
    rows: Sequence[dict],
    forced_status: str = "",
    forced_error: str = "",
) -> dict:
    assessment = assess_page(corpus_text, rows)
    if forced_status:
        assessment["status"] = forced_status
        assessment["error"] = forced_error
    payload = encode_rows(rows) if assessment["status"] == "ready" else None
    now = utc_now()
    with _connect(Path(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO geometry_pages(
              source_file,pdf_page,corpus_text_sha256,pdf_sha256,page_width,page_height,
              line_count,ocr_chars,mean_confidence,canonical_coverage,length_ratio,
              status,payload,error,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_file,pdf_page) DO UPDATE SET
              corpus_text_sha256=excluded.corpus_text_sha256,
              pdf_sha256=excluded.pdf_sha256,page_width=excluded.page_width,
              page_height=excluded.page_height,line_count=excluded.line_count,
              ocr_chars=excluded.ocr_chars,mean_confidence=excluded.mean_confidence,
              canonical_coverage=excluded.canonical_coverage,length_ratio=excluded.length_ratio,
              status=excluded.status,payload=excluded.payload,error=excluded.error,
              updated_at=excluded.updated_at
            """,
            (
                source_file, int(pdf_page), sha256_text(corpus_text), str(pdf_sha256 or ""),
                float(page_width), float(page_height), len(rows), int(assessment["ocr_chars"]),
                float(assessment["mean_confidence"]), float(assessment["canonical_coverage"]),
                float(assessment["length_ratio"]), str(assessment["status"]), payload,
                str(assessment["error"]), now,
            ),
        )
        conn.commit()
    return assessment


def database_revision(db_path: Path) -> str:
    target = Path(db_path)
    try:
        # ``Path.is_file()`` itself raises PermissionError on Python 3.10 when
        # systemd hides the symlink target (for example via ProtectHome).  The
        # geometry database is an optional enhancement, so even the existence
        # probe must fail closed instead of taking down a highlighted viewer.
        if not target.is_file():
            return ""
        with _connect(target, readonly=True) as conn:
            row = conn.execute("SELECT value FROM geometry_meta WHERE key='build_id'").fetchone()
            return str(row[0] or "")[:32] if row else ""
    except (OSError, sqlite3.Error):
        return ""


def _current_corpus_hash(corpus_db: Path, source_file: str, pdf_page: int) -> str:
    try:
        with _connect(Path(corpus_db), readonly=True) as conn:
            row = conn.execute(
                "SELECT raw_text FROM pages WHERE source_file=? AND pdf_page=? LIMIT 1",
                (source_file, int(pdf_page)),
            ).fetchone()
        return sha256_text(str(row[0] or "")) if row else ""
    except (OSError, sqlite3.Error):
        return ""


def geometry_cache_token(db_path: Path, corpus_db: Path, source_file: str, pdf_page: int) -> str:
    target = Path(db_path)
    try:
        if not target.is_file():
            return "none"
        with _connect(target, readonly=True) as conn:
            row = conn.execute(
                "SELECT corpus_text_sha256,status,updated_at FROM geometry_pages "
                "WHERE source_file=? AND pdf_page=?",
                (source_file, int(pdf_page)),
            ).fetchone()
        if not row or str(row[1]) != "ready":
            return "none"
        if str(row[0]) != _current_corpus_hash(Path(corpus_db), source_file, pdf_page):
            return "stale"
        return hashlib.sha256(f"{row[0]}|{row[2]}".encode("utf-8")).hexdigest()[:16]
    except (OSError, sqlite3.Error):
        return "none"


def _flatten_rows(rows: Sequence[Sequence]) -> tuple[str, list[tuple[int, int, int]]]:
    chars: list[str] = []
    refs: list[tuple[int, int, int]] = []
    for row_index, row in enumerate(rows):
        if len(row) < 5:
            continue
        normalized = normalize(str(row[4] or ""))
        count = len(normalized)
        for local_index, char in enumerate(normalized):
            chars.append(char)
            refs.append((row_index, local_index, count))
    return "".join(chars), refs


def _match_spans(flat: str, query: str, *, max_occurrences: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = flat.find(query)
    while start >= 0 and len(spans) < max_occurrences:
        spans.append((start, start + len(query)))
        start = flat.find(query, start + max(1, len(query)))
    if spans or len(query) < MIN_QUERY_CHARS:
        return spans
    if _rapidfuzz is None:
        return []
    try:
        match = _rapidfuzz.partial_ratio_alignment(query, flat, score_cutoff=MIN_MATCH_SCORE)
    except Exception:
        return []
    if match is None:
        return []
    start, end = int(match.dest_start), int(match.dest_end)
    span_len = end - start
    if start < 0 or end > len(flat):
        return []
    ratio = span_len / max(1, len(query))
    return [(start, end)] if MIN_MATCH_SPAN_RATIO <= ratio <= MAX_MATCH_SPAN_RATIO else []


def _rects_for_spans(rows: Sequence[Sequence], refs: Sequence[tuple[int, int, int]], spans: Iterable[tuple[int, int]], *, max_rects: int) -> list[tuple[float, float, float, float]]:
    rects: list[tuple[float, float, float, float]] = []
    for start, end in spans:
        grouped: dict[int, list[tuple[int, int]]] = {}
        for ref_index in range(start, min(end, len(refs))):
            row_index, local_index, count = refs[ref_index]
            grouped.setdefault(row_index, []).append((local_index, count))
        for row_index in sorted(grouped):
            row = rows[row_index]
            x0, y0, x1, y1 = map(float, row[:4])
            positions = grouped[row_index]
            count = max(1, positions[0][1])
            first = min(item[0] for item in positions)
            last = max(item[0] for item in positions) + 1
            width = max(0.0, x1 - x0)
            rects.append((x0 + width * first / count, y0, x0 + width * last / count, y1))
            if len(rects) >= max_rects:
                return rects
    return rects


def locate_geometry_rects(
    db_path: Path,
    corpus_db: Path,
    source_file: str,
    pdf_page: int,
    query_text: str,
    *,
    max_rects: int = 80,
    max_occurrences: int = 8,
) -> list[tuple[float, float, float, float]]:
    """Return normalized page rectangles, or [] on any uncertainty/error."""
    query = normalize(str(query_text or ""))
    if len(query) < MIN_EXACT_QUERY_CHARS:
        return []
    try:
        if not Path(db_path).is_file():
            return []
        with _connect(Path(db_path), readonly=True) as conn:
            row = conn.execute(
                "SELECT corpus_text_sha256,status,payload FROM geometry_pages "
                "WHERE source_file=? AND pdf_page=?",
                (source_file, int(pdf_page)),
            ).fetchone()
        if not row or str(row[1]) != "ready" or not row[2]:
            return []
        if str(row[0]) != _current_corpus_hash(Path(corpus_db), source_file, pdf_page):
            return []
        rows = decode_rows(row[2])
        flat, refs = _flatten_rows(rows)
        if not flat:
            return []
        spans = _match_spans(flat, query, max_occurrences=max_occurrences)
        rects = _rects_for_spans(rows, refs, spans, max_rects=max_rects)
        if rects:
            return rects
        # Preserve the existing semantics for explicit multi-term highlighting.
        terms = [normalize(term) for term in str(query_text or "").split()]
        terms = [term for term in dict.fromkeys(terms) if len(term) >= MIN_EXACT_QUERY_CHARS]
        for term in terms:
            spans = _match_spans(flat, term, max_occurrences=max_occurrences)
            rects.extend(_rects_for_spans(rows, refs, spans, max_rects=max_rects - len(rects)))
            if len(rects) >= max_rects:
                break
        return rects
    except (OSError, ValueError, TypeError, json.JSONDecodeError, zlib.error, sqlite3.Error):
        return []
