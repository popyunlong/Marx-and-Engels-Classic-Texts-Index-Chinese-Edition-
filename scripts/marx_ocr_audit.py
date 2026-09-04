#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only 24-hour OCR audit for the first-edition Marx corpus.

The program reads the production corpus and source PDFs, but writes only to a
dedicated audit run directory.  It never promotes a candidate database.  A
Paddle job id is checkpointed before the job is polled; a submission whose
outcome is uncertain is deliberately stranded for manual recovery rather than
submitted a second time.
"""
from __future__ import annotations

import argparse
import base64
import csv
import difflib
import hashlib
import html
import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import statistics
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import fitz


AUDIT_VERSION = "1.0"
ANALYZER_VERSION = "2"
TARGET_BOOKS = ("文集", "全集")
EXPECTED_SOURCE_COUNT = 62
SAMPLE_SIZE = 1000
CONTROL_PER_SOURCE = 4
RISK_PER_SOURCE = 8
DEFAULT_HOURS = 24.0
DEFAULT_PER_HOUR = 42
DEFAULT_PADDLE_MODEL = "PaddleOCR-VL-1.6"
DEFAULT_PADDLE_BASE = "https://paddleocr.aistudio-app.com"
DEFAULT_GLM_MODEL = "glm-4.6v-flash"
DEFAULT_GLM_BASE = "https://open.bigmodel.cn/api/paas/v4"
PADDLE_PRICE_RMB_PER_PAGE = 0.09
KNOWN_PRINTED_PAGE_PRIORITIES = (("文集", 2, "63", "已知第2卷第63页标点案例"),)
MIN_ANCHOR_BODY_CHARS = 8
MAX_DIFFS_PER_PAGE = 40
MAX_AUTOMATIC_DIFF_GROUPS_PER_PAGE = 4
RETRIABLE_GET_HTTP = {408, 409, 425, 429, 500, 502, 503, 504, 529}
SAFE_POST_RETRY_HTTP = {429}
PUNCT_RE = re.compile(r"^[\W_]*$", re.UNICODE)
CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
BODY_CHAR_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaffa-zA-Z0-9]")
LATIN_RE = re.compile(r"[A-Za-z]")
DATE_RE = re.compile(r"(?:1[5-9]\d{2}|20\d{2})\s*[年./—-]|\d{1,2}\s*月\s*\d{1,2}\s*日")
FORMULA_RE = re.compile(r"(?:[A-Za-z]\s*[=+*/^<>]|[=+*/^<>]\s*[A-Za-z0-9])")
ODD_RE = re.compile(r"[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]|(?:[?？!！.,，。；;:：]){4,}")
INLINE_DIGIT_RE = re.compile(r"(?<=[\u3400-\u9fff])\d{1,3}(?=[\u3400-\u9fff])")
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,}\]]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token)\s*[:=]\s*)[^\s,}\]]+"),
)
STRIP_SEARCH_RE = re.compile(
    r"[\s\u3000\u2000-\u206f\u2e00-\u2e7f\u3000-\u303f\uff00-\uffef"
    r"!-/:-@\[-`\{-~]+"
)


try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


class AuditError(RuntimeError):
    pass


class FatalProviderError(AuditError):
    pass


class SubmissionUncertain(AuditError):
    pass


class TransientProviderError(AuditError):
    pass


@dataclass(frozen=True)
class PageRow:
    id: int
    book: str
    volume: int
    source_file: str
    pdf_page: int
    printed_page: str
    raw_text: str
    normalized_text: str
    risk_score: float = 0.0
    risk_reasons: tuple[str, ...] = ()
    priority_reason: str = ""
    sample_kind: str = ""


@dataclass(frozen=True)
class TextDiff:
    before: str
    after: str
    left_anchor: str
    right_anchor: str
    a_start: int
    a_end: int
    kind: str = "ocr_difference"


@dataclass(frozen=True)
class Glyph:
    char: str
    bbox: tuple[float, float, float, float]
    size: float
    line: int


@dataclass(frozen=True)
class AuditConfig:
    database: Path
    project_root: Path
    feedback_db: Path | None
    output_root: Path
    run_id: str
    hours: float = DEFAULT_HOURS
    max_success: int = SAMPLE_SIZE
    per_hour: int = DEFAULT_PER_HOUR
    expected_sources: int = EXPECTED_SOURCE_COUNT
    health_url: str = "http://127.0.0.1:8000/"
    guard_poll_seconds: float = 60.0
    healthy_resume_seconds: float = 300.0
    paddle_poll_seconds: float = 900.0
    paddle_poll_interval: float = 3.0

    @property
    def run_dir(self) -> Path:
        return self.output_root / self.run_id


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write(path: Path, data: str | bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": ""}
    with tempfile.NamedTemporaryFile(mode, dir=path.parent, delete=False, **kwargs) as handle:
        handle.write(data)
        temp_name = handle.name
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_name, path)


def redact(value: Any, secrets: Sequence[str] = ()) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:2000]


def nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def canonical_visible(value: str) -> str:
    """Text used for visual alignment; whitespace/control and Markdown wrappers vanish."""
    value = nfc(value).replace("\u00ad", "")
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", value)
    value = re.sub(r"\$\^\{([^{}\n]+)\}\$", r"\1", value)
    value = re.sub(r"\^\{([^{}\n]+)\}", r"\1", value)
    value = re.sub(r"\$([^$\n]{1,80})\$", r"\1", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    return "".join(ch for ch in value if not ch.isspace() and unicodedata.category(ch)[0] != "C")


_T2S: Any = None
_T2S_TRIED = False


def normalize_search(value: str) -> str:
    global _T2S, _T2S_TRIED
    value = nfkc(value)
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
    return STRIP_SEARCH_RE.sub("", value)


def body_char_count(value: str) -> int:
    return sum(1 for char in value if BODY_CHAR_RE.fullmatch(char))


def deterministic_key(*parts: Any) -> str:
    return sha256_text("\0".join(str(part) for part in parts))


def risk_features(raw_text: str, normalized_text: str, *, prior_correction: bool = False,
                  user_report: bool = False) -> tuple[float, tuple[str, ...]]:
    raw = str(raw_text or "")
    compact = canonical_visible(raw)
    reasons: list[str] = []
    score = 0.0
    isolated_latin = re.findall(r"(?<=[\u3400-\u9fff])[A-Za-z](?=[\u3400-\u9fff])", compact)
    if isolated_latin:
        score += 18.0 + min(20, len(isolated_latin) * 2)
        reasons.append("夹杂拉丁字符")
    inline_digits = INLINE_DIGIT_RE.findall(compact)
    if inline_digits:
        score += 14.0 + min(16, len(inline_digits) * 2)
        reasons.append("句中数字")
    if ODD_RE.search(raw):
        score += 28.0
        reasons.append("异常标点或乱码")
    line_fragments = len(re.findall(r"[\u3400-\u9fff][-—]\s*\n\s*[\u3400-\u9fff]", raw))
    if line_fragments:
        score += min(15.0, line_fragments * 3.0)
        reasons.append("文本断裂")
    nlen = len(str(normalized_text or ""))
    if nlen < 80:
        score += 24.0
        reasons.append("低字数")
    elif nlen < 180:
        score += 10.0
        reasons.append("偏低字数")
    punct_total = sum(1 for ch in compact if unicodedata.category(ch).startswith("P"))
    if compact and punct_total / len(compact) > 0.16:
        score += 10.0
        reasons.append("标点密度异常")
    if prior_correction:
        score += 30.0
        reasons.append("既有修订")
    if user_report:
        score += 1000.0
        reasons.append("用户报告")
    return score, tuple(dict.fromkeys(reasons))


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def load_prior_corrections(project_root: Path) -> tuple[set[tuple[str, int, int]], list[dict[str, Any]]]:
    page_keys: set[tuple[str, int, int]] = set()
    authority: list[dict[str, Any]] = []
    wenji = load_yaml(project_root / "config" / "wenji_text_corrections.yaml")
    for item in wenji.get("pages") or []:
        if not isinstance(item, dict):
            continue
        try:
            volume, page = int(item["volume"]), int(item["page"])
        except (KeyError, TypeError, ValueError):
            continue
        page_keys.add(("文集", volume, page))
        authority.append({
            "book": "文集", "volume": volume, "pdf_page": page,
            "find": str(item.get("find") or ""), "replace": str(item.get("replace") or ""),
            "evidence": str(item.get("evidence") or ""), "scope": "page",
        })
    for item in wenji.get("book_wide") or []:
        if isinstance(item, dict):
            authority.append({"book": "文集", "volume": None, "pdf_page": None,
                              "find": str(item.get("find") or ""),
                              "replace": str(item.get("replace") or ""),
                              "evidence": str(item.get("evidence") or ""), "scope": "book"})
    for volume, items in (wenji.get("volume_wide") or {}).items():
        for item in items or []:
            if isinstance(item, dict):
                authority.append({"book": "文集", "volume": int(volume), "pdf_page": None,
                                  "find": str(item.get("find") or ""),
                                  "replace": str(item.get("replace") or ""),
                                  "evidence": str(item.get("evidence") or ""), "scope": "volume"})
    return page_keys, authority


def load_inventory(database: Path) -> list[PageRow]:
    uri = "file:" + urllib.parse.quote(str(Path(database).resolve()).replace("\\", "/")) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id,book,volume,source_file,pdf_page,COALESCE(printed_page,''),raw_text,normalized_text "
            "FROM pages WHERE book IN (?,?) ORDER BY source_file,pdf_page", TARGET_BOOKS
        ).fetchall()
    finally:
        conn.close()
    return [PageRow(int(r[0]), str(r[1]), int(r[2]), str(r[3]), int(r[4]), str(r[5]),
                    str(r[6]), str(r[7])) for r in rows]


def parse_ints(value: str) -> list[int]:
    return [int(raw) for raw in re.findall(r"(?<!\d)(\d{1,4})(?!\d)", str(value or ""))]


def feedback_priority_keys(feedback_db: Path | None, pages: Sequence[PageRow]) -> dict[int, str]:
    by_id: dict[int, str] = {}
    for page in pages:
        for book, volume, printed_page, reason in KNOWN_PRINTED_PAGE_PRIORITIES:
            if page.book == book and page.volume == volume and str(page.printed_page).strip() == printed_page:
                by_id[page.id] = reason
    if feedback_db is None or not Path(feedback_db).is_file():
        return by_id
    by_source_page = {(p.source_file.replace("\\", "/"), p.pdf_page): p for p in pages}
    by_book_volume: dict[tuple[str, int], list[PageRow]] = defaultdict(list)
    for page in pages:
        by_book_volume[(page.book, page.volume)].append(page)
    try:
        conn = sqlite3.connect(f"file:{Path(feedback_db).resolve().as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_error_reports'"
        ).fetchone()
        if not exists:
            conn.close()
            return by_id
        reports = conn.execute(
            "SELECT id,book_title,volume_label,page,source_ref,note,status FROM page_error_reports"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return by_id
    for row in reports:
        book_title = str(row["book_title"] or "")
        source_ref = str(row["source_ref"] or "").replace("\\", "/")
        note = str(row["note"] or "")
        if "第二版" in book_title or "第二版" in source_ref or "第二版" in note:
            continue
        book = "文集" if "文集" in book_title or "/文集/" in source_ref else (
            "全集" if "全集" in book_title or "/全集/" in source_ref else "")
        if not book:
            continue
        volume_values = parse_ints(str(row["volume_label"] or ""))
        if not volume_values:
            match = re.search(r"第\s*(\d{1,2})\s*卷", source_ref)
            volume_values = [int(match.group(1))] if match else []
        volume = volume_values[0] if volume_values else None
        page_values = parse_ints(str(row["page"] or ""))
        note_pdf = re.findall(r"(?:PDF|pdf)\s*(?:第)?\s*(\d{1,4})", note)
        page_values.extend(int(value) for value in note_pdf)
        candidates: set[int] = set()
        for number in page_values:
            for (source, pdf_page), page in by_source_page.items():
                if source_ref and (source.endswith(source_ref) or source_ref.endswith(source)) and pdf_page == number:
                    candidates.add(page.id)
            if volume is not None:
                for page in by_book_volume.get((book, volume), []):
                    if page.pdf_page == number or str(page.printed_page).strip() == str(number):
                        candidates.add(page.id)
        for page_id in candidates:
            by_id[page_id] = f"用户页码报告#{int(row['id'])}"
    return by_id


def sample_pages(pages: Sequence[PageRow], *, expected_sources: int = EXPECTED_SOURCE_COUNT,
                 sample_size: int = SAMPLE_SIZE, project_root: Path | None = None,
                 feedback_db: Path | None = None) -> list[PageRow]:
    if not pages:
        raise ValueError("target corpus is empty")
    project_root = Path(project_root or ".")
    correction_keys, _ = load_prior_corrections(project_root)
    priorities = feedback_priority_keys(feedback_db, pages)
    scored: list[PageRow] = []
    for page in pages:
        prior = (page.book, page.volume, page.pdf_page) in correction_keys
        score, reasons = risk_features(page.raw_text, page.normalized_text,
                                       prior_correction=prior, user_report=page.id in priorities)
        scored.append(PageRow(**{**asdict(page), "risk_score": score, "risk_reasons": reasons,
                                 "priority_reason": priorities.get(page.id, "")}))
    groups: dict[str, list[PageRow]] = defaultdict(list)
    for page in scored:
        groups[page.source_file].append(page)
    if len(groups) != int(expected_sources):
        raise ValueError(f"expected {expected_sources} source PDFs, found {len(groups)}")
    selected: dict[int, PageRow] = {}
    for source in sorted(groups):
        source_pages = groups[source]
        body_pages = [p for p in source_pages if len(p.normalized_text) >= 180 and p.risk_score < 1000]
        low_risk_cutoff = percentile([p.risk_score for p in body_pages], 50) if body_pages else 0.0
        normal = [p for p in body_pages if p.risk_score <= low_risk_cutoff]
        if len(normal) < CONTROL_PER_SOURCE:
            normal = [p for p in source_pages if len(p.normalized_text) >= 80]
        normal.sort(key=lambda p: deterministic_key("marx-ocr-control-v1", source, p.pdf_page))
        controls = normal[:CONTROL_PER_SOURCE]
        if len(controls) != CONTROL_PER_SOURCE:
            raise ValueError(f"{source}: fewer than {CONTROL_PER_SOURCE} body control pages")
        for page in controls:
            selected[page.id] = PageRow(**{**asdict(page), "sample_kind": "control"})
        risk_pool = [p for p in source_pages if p.id not in selected]
        risk_pool.sort(key=lambda p: (-p.risk_score, deterministic_key("marx-ocr-risk-v1", source, p.pdf_page)))
        risks = risk_pool[:RISK_PER_SOURCE]
        if len(risks) != RISK_PER_SOURCE:
            raise ValueError(f"{source}: fewer than {RISK_PER_SOURCE} risk pages")
        for page in risks:
            selected[page.id] = PageRow(**{**asdict(page), "sample_kind": "source-risk"})
    remaining = [p for p in scored if p.id not in selected]
    remaining.sort(key=lambda p: (-p.risk_score, deterministic_key("marx-ocr-global-v1", p.source_file, p.pdf_page)))
    need = sample_size - len(selected)
    if need < 0 or len(remaining) < need:
        raise ValueError(f"cannot produce exact {sample_size}-page sample")
    for page in remaining[:need]:
        selected[page.id] = PageRow(**{**asdict(page), "sample_kind": "global-risk"})
    # Round-robin sources so the first 20 calls form a representative canary.
    ordered_sources = sorted(groups, key=lambda source: deterministic_key("marx-ocr-run-source-v1", source))
    selected_by_source: dict[str, list[PageRow]] = defaultdict(list)
    for page in selected.values():
        selected_by_source[page.source_file].append(page)
    for source in ordered_sources:
        selected_by_source[source].sort(
            key=lambda p: (0 if p.priority_reason else 1,
                           {"source-risk": 0, "control": 1, "global-risk": 2}.get(p.sample_kind, 3),
                           -p.risk_score, p.pdf_page))
    result: list[PageRow] = []
    for index in range(max(len(values) for values in selected_by_source.values())):
        for source in ordered_sources:
            values = selected_by_source[source]
            if index < len(values):
                result.append(values[index])
    if len(result) != sample_size or len({p.id for p in result}) != sample_size:
        raise AssertionError("sample is not exactly unique")
    if len({p.source_file for p in result}) != expected_sources:
        raise AssertionError("sample does not cover every source")
    return result


def init_checkpoint(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pages(
          page_id INTEGER PRIMARY KEY, plan_order INTEGER NOT NULL UNIQUE,
          book TEXT NOT NULL, volume INTEGER NOT NULL, source_file TEXT NOT NULL,
          pdf_page INTEGER NOT NULL, printed_page TEXT NOT NULL DEFAULT '',
          baseline_hash TEXT NOT NULL, baseline_raw TEXT NOT NULL, baseline_normalized TEXT NOT NULL,
          risk_score REAL NOT NULL, risk_reasons TEXT NOT NULL, sample_kind TEXT NOT NULL,
          priority_reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',
          submit_state TEXT NOT NULL DEFAULT 'never', job_id TEXT NOT NULL DEFAULT '',
          paddle_text TEXT NOT NULL DEFAULT '', paddle_hash TEXT NOT NULL DEFAULT '',
          paddle_raw_path TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL DEFAULT '', completed_at TEXT NOT NULL DEFAULT '',
          UNIQUE(source_file,pdf_page)
        );
        CREATE TABLE IF NOT EXISTS candidates(
          id INTEGER PRIMARY KEY AUTOINCREMENT, page_id INTEGER NOT NULL,
          error_type TEXT NOT NULL, before_text TEXT NOT NULL, after_text TEXT NOT NULL,
          left_anchor TEXT NOT NULL, right_anchor TEXT NOT NULL,
          paddle_value TEXT NOT NULL DEFAULT '', rapid_value TEXT NOT NULL DEFAULT '',
          glm_value TEXT NOT NULL DEFAULT '', authority_value TEXT NOT NULL DEFAULT '',
          authority_evidence TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL,
          status TEXT NOT NULL, protected_reasons TEXT NOT NULL DEFAULT '',
          evidence_crop TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, UNIQUE(page_id,error_type,left_anchor,right_anchor,before_text,after_text)
        );
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, level TEXT NOT NULL,
          event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS metrics(
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def event(conn: sqlite3.Connection, level: str, name: str, detail: str = "") -> None:
    conn.execute("INSERT INTO events(at,level,event,detail) VALUES(?,?,?,?)",
                 (utc_now(), level, name, redact(detail)))
    conn.commit()
    print(f"[{utc_now()}] {level.upper()} {name}" + (f": {redact(detail)}" if detail else ""), flush=True)


def initialize_run(config: AuditConfig, *, force_plan: bool = False) -> sqlite3.Connection:
    config.run_dir.mkdir(parents=True, exist_ok=True)
    for name in ("raw/paddle", "raw/glm", "evidence"):
        (config.run_dir / name).mkdir(parents=True, exist_ok=True)
    conn = init_checkpoint(config.run_dir / "checkpoint.sqlite3")
    existing = int(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
    if existing and not force_plan:
        return conn
    if existing and force_plan:
        submitted = int(conn.execute(
            "SELECT COUNT(*) FROM pages WHERE submit_state<>'never' OR job_id<>''"
        ).fetchone()[0])
        if submitted:
            raise AuditError("refusing to replace a plan after any provider submission")
        conn.execute("DELETE FROM candidates")
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM metrics")
        conn.execute("DELETE FROM meta")
        conn.commit()
    inventory = load_inventory(config.database)
    planned = sample_pages(inventory, expected_sources=config.expected_sources,
                           sample_size=config.max_success, project_root=config.project_root,
                           feedback_db=config.feedback_db)
    rows = []
    for order, page in enumerate(planned, 1):
        rows.append((page.id, order, page.book, page.volume, page.source_file, page.pdf_page,
                     page.printed_page, sha256_text(page.raw_text), page.raw_text, page.normalized_text,
                     page.risk_score, canonical_json(page.risk_reasons), page.sample_kind,
                     page.priority_reason))
    conn.executemany(
        "INSERT INTO pages(page_id,plan_order,book,volume,source_file,pdf_page,printed_page,"
        "baseline_hash,baseline_raw,baseline_normalized,risk_score,risk_reasons,sample_kind,priority_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    started = utc_now()
    for key, value in {
        "audit_version": AUDIT_VERSION, "run_id": config.run_id, "created_at": started,
        "database": str(config.database), "project_root": str(config.project_root),
        "planned_pages": len(planned), "expected_sources": config.expected_sources,
        "hours": config.hours, "max_success": config.max_success, "per_hour": config.per_hour,
        "production_write_count": 0, "state": "planned",
    }.items():
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))
    conn.commit()
    write_plan_artifacts(config, conn)
    return conn


def write_plan_artifacts(config: AuditConfig, conn: sqlite3.Connection) -> None:
    rows = [dict(row) for row in conn.execute(
        "SELECT plan_order,page_id,book,volume,source_file,pdf_page,printed_page,risk_score,"
        "risk_reasons,sample_kind,priority_reason FROM pages ORDER BY plan_order"
    )]
    atomic_write(config.run_dir / "sample_plan.json", json.dumps(rows, ensure_ascii=False, indent=2))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]) if rows else [])
    if rows:
        writer.writeheader()
        writer.writerows(rows)
    atomic_write(config.run_dir / "sample_plan.csv", output.getvalue())


def multipart_image_body(image: bytes, filename: str, model: str) -> tuple[bytes, str]:
    boundary = "----MarxCorpusAudit" + hashlib.sha256(os.urandom(32)).hexdigest()[:24]
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend((f"--{boundary}\r\n".encode(),
                       f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                       value.encode("utf-8"), b"\r\n"))

    field("model", model)
    field("optionalPayload", canonical_json({"useLayoutDetection": True,
                                              "prettifyMarkdown": False, "temperature": 0.0}))
    chunks.extend((f"--{boundary}\r\n".encode(),
                   f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                   "Content-Type: image/png\r\n\r\n".encode(), image, b"\r\n",
                   f"--{boundary}--\r\n".encode()))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def response_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AuditError("provider response is not an object")
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


class PaddleClient:
    def __init__(self, token: str, *, base_url: str = DEFAULT_PADDLE_BASE,
                 model: str = DEFAULT_PADDLE_MODEL, opener: Callable[..., Any] = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep):
        self.token = str(token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.opener = opener
        self.sleep = sleep
        if not self.token:
            raise FatalProviderError("PaddleOCR credential is not configured")

    def _json(self, url: str, *, data: bytes | None = None, content_type: str = "application/json",
              timeout: int = 120) -> dict[str, Any]:
        headers = {"Authorization": "Bearer " + self.token, "User-Agent": "marx-ocr-audit/1.0"}
        if data is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers)
        with self.opener(request, timeout=timeout) as response:
            return response_data(json.loads(response.read().decode("utf-8")))

    def submit(self, image: bytes, filename: str) -> str:
        body, content_type = multipart_image_body(image, filename, self.model)
        try:
            payload = self._json(self.base_url + "/api/v2/ocr/jobs", data=body,
                                 content_type=content_type, timeout=180)
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 403, 404}:
                raise FatalProviderError(f"PaddleOCR authentication/model endpoint failed (HTTP {exc.code})") from exc
            if exc.code in SAFE_POST_RETRY_HTTP:
                raise TransientProviderError(f"PaddleOCR rate limited (HTTP {exc.code})") from exc
            raise SubmissionUncertain(f"PaddleOCR submission outcome uncertain (HTTP {exc.code})") from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            raise SubmissionUncertain("PaddleOCR submission outcome uncertain after network failure") from exc
        job_id = str(payload.get("jobId") or payload.get("id") or "").strip()
        if not job_id:
            raise SubmissionUncertain("PaddleOCR accepted response without a job id")
        return job_id

    def poll(self, job_id: str, *, timeout_seconds: float, interval: float) -> tuple[str, dict[str, Any]]:
        started = time.monotonic()
        delay = max(0.1, interval)
        last_error = ""
        while time.monotonic() - started < timeout_seconds:
            try:
                payload = self._json(self.base_url + "/api/v2/ocr/jobs/" + urllib.parse.quote(job_id),
                                     timeout=90)
                state = str(payload.get("state") or payload.get("status") or "").lower()
                if state in {"done", "succeeded", "success", "completed"}:
                    return "done", payload
                if state in {"failed", "error", "cancelled", "canceled"}:
                    return "failed", payload
                last_error = ""
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403, 404}:
                    raise FatalProviderError(f"PaddleOCR poll failed (HTTP {exc.code})") from exc
                if exc.code not in RETRIABLE_GET_HTTP:
                    raise AuditError(f"PaddleOCR poll failed (HTTP {exc.code})") from exc
                last_error = f"HTTP {exc.code}"
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                last_error = type(exc).__name__
            self.sleep(delay)
            delay = min(12.0, delay * 1.5)
        raise TransientProviderError("PaddleOCR poll timed out" + (f" after {last_error}" if last_error else ""))

    def fetch_result(self, status: dict[str, Any]) -> tuple[str, str]:
        ref: Any = status.get("resultJsonUrl") or status.get("resultUrl") or ""
        if isinstance(ref, dict):
            url = str(ref.get("jsonUrl") or ref.get("url") or "")
        else:
            url = str(ref or "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise AuditError("PaddleOCR result URL is invalid")
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                raise AuditError("PaddleOCR result URL resolved to a private address")
        except socket.gaierror as exc:
            raise AuditError("PaddleOCR result host could not be resolved") from exc
        raw = ""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with self.opener(url, timeout=120) as response:
                    raw = response.read().decode("utf-8", "replace")
                break
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                last_error = exc
                if attempt < 2:
                    self.sleep(2.0 ** attempt)
        else:
            raise TransientProviderError("PaddleOCR result download failed") from last_error
        texts: list[str] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            result = row.get("result") if isinstance(row, dict) else None
            for item in ((result or {}).get("layoutParsingResults") or []):
                text = str((item.get("markdown") or {}).get("text") or "").strip()
                if text:
                    texts.append(text)
        output = "\n\n".join(texts).strip()
        if not output:
            raise AuditError("PaddleOCR returned empty text")
        return output, raw


class RapidClient:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as exc:
            raise FatalProviderError("RapidOCR runtime is unavailable") from exc
        try:
            self.engine = RapidOCR(intra_op_num_threads=1)
        except TypeError:
            self.engine = RapidOCR()

    def recognize(self, png: bytes) -> tuple[str, list[dict[str, Any]]]:
        try:
            import cv2
            import numpy as np

            image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
            result, _elapsed = self.engine(image)
        except Exception as exc:
            raise AuditError(f"RapidOCR failed: {type(exc).__name__}") from exc
        rows: list[dict[str, Any]] = []
        for item in result or []:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            rows.append({"bbox": item[0], "text": str(item[1]), "confidence": float(item[2])})
        rows.sort(key=lambda r: (min(float(p[1]) for p in r["bbox"]), min(float(p[0]) for p in r["bbox"])))
        return "\n".join(row["text"] for row in rows), rows


class GlmClient:
    def __init__(self, key: str, *, base_url: str = DEFAULT_GLM_BASE,
                 model: str = DEFAULT_GLM_MODEL, opener: Callable[..., Any] = urllib.request.urlopen):
        self.key = str(key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.opener = opener
        if not self.key:
            raise FatalProviderError("Zhipu credential is not configured")

    def verify_crop(self, png: bytes, *, left: str, right: str) -> tuple[dict[str, Any], dict[str, Any]]:
        prompt = (
            "逐字查看这张中文书页局部裁剪。左锚点和右锚点来自版面，只用于定位，不要猜测或润色。"
            "返回两锚点之间肉眼可见的全部字符（可为空），并判断其中的纯数字是否为小字号/上标脚注标记。"
            "只返回JSON对象：{\"between\":\"原样字符\",\"is_footnote\":false,\"readable\":true}。"
            f"\n左锚点：{left}\n右锚点：{right}"
        )
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
            ]}],
            "temperature": 0.0,
            "max_tokens": 256,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        encoded = canonical_json(body).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/chat/completions", data=encoded,
                                         headers={"Authorization": "Bearer " + self.key,
                                                  "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=60) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            if exc.code in {401, 403, 404} or (
                    exc.code == 400 and re.search(r"(?i)model|thinking|response.?format|not supported|模型", detail)):
                raise FatalProviderError(f"GLM authentication/model endpoint failed (HTTP {exc.code})") from exc
            suffix = ": " + redact(detail) if detail else ""
            raise TransientProviderError(f"GLM unavailable (HTTP {exc.code}){suffix}") from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            raise TransientProviderError("GLM request timed out") from exc
        message = ((response_payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(message, list):
            message = "\n".join(str(item.get("text") or "") for item in message if isinstance(item, dict))
        text = str(message or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuditError("GLM returned invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("between"), str):
            raise AuditError("GLM response omitted exact between text")
        audit_payload = {
            "model": self.model, "result": result,
            "finish_reason": str((response_payload.get("choices") or [{}])[0].get("finish_reason") or ""),
            "usage": response_payload.get("usage") if isinstance(response_payload.get("usage"), dict) else {},
        }
        return result, audit_payload


def choose_anchor(text: str, start: int, direction: int, *, minimum: int = MIN_ANCHOR_BODY_CHARS,
                  maximum: int = 48) -> str:
    if direction < 0:
        for width in range(minimum, maximum + 1):
            left = max(0, start - width)
            candidate = text[left:start]
            if body_char_count(candidate) >= minimum and text.count(candidate) == 1:
                return candidate
    else:
        for width in range(minimum, maximum + 1):
            candidate = text[start:min(len(text), start + width)]
            if body_char_count(candidate) >= minimum and text.count(candidate) == 1:
                return candidate
    return ""


def detect_text_diffs(baseline: str, paddle: str) -> tuple[list[TextDiff], dict[str, Any]]:
    left = canonical_visible(baseline)
    right = canonical_visible(paddle)
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    opcodes = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    meta = {"similarity": matcher.ratio(), "difference_groups": len(opcodes),
            "baseline_length": len(left),
            "reading_order_suspect": (
                matcher.ratio() < 0.90 or len(opcodes) > MAX_AUTOMATIC_DIFF_GROUPS_PER_PAGE
            )}
    diffs: list[TextDiff] = []
    for tag, i1, i2, j1, j2 in opcodes[:MAX_DIFFS_PER_PAGE]:
        before, after = left[i1:i2], right[j1:j2]
        left_anchor = choose_anchor(left, i1, -1)
        right_anchor = choose_anchor(left, i2, 1)
        diffs.append(TextDiff(before, after, left_anchor, right_anchor, i1, i2,
                              "punctuation" if PUNCT_RE.fullmatch(before + after) else "ocr_difference"))
    return diffs, meta


def diff_size_allowed(diff: TextDiff) -> bool:
    chars = diff.before + diff.after
    body = body_char_count(chars)
    punctuation = sum(1 for char in chars if unicodedata.category(char).startswith("P"))
    if body:
        return body <= 4 and body_char_count(diff.before) <= 2 and body_char_count(diff.after) <= 2 and punctuation == 0
    return punctuation <= 2 and len(diff.before) <= 1 and len(diff.after) <= 1


def extract_between(text: str, left_anchor: str, right_anchor: str) -> str | None:
    compact = canonical_visible(text)
    if not left_anchor or not right_anchor or compact.count(left_anchor) != 1:
        return None
    start = compact.find(left_anchor) + len(left_anchor)
    end = compact.find(right_anchor, start)
    if end < start or compact.find(right_anchor, end + 1) >= 0:
        return None
    return compact[start:end]


def page_glyphs(page: fitz.Page) -> list[Glyph]:
    glyphs: list[Glyph] = []
    try:
        payload = page.get_text("rawdict")
    except Exception:
        return glyphs
    line_no = 0
    for block in payload.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            line_no += 1
            for span in line.get("spans", []):
                size = float(span.get("size") or 0.0)
                for item in span.get("chars", []):
                    char = nfc(str(item.get("c") or ""))
                    bbox = item.get("bbox") or span.get("bbox")
                    if len(char) == 1 and bbox and len(bbox) == 4 and not char.isspace():
                        glyphs.append(Glyph(char, tuple(float(v) for v in bbox), size, line_no))
    return glyphs


def glyph_compact(glyphs: Sequence[Glyph]) -> tuple[str, list[Glyph]]:
    chars: list[str] = []
    mapped: list[Glyph] = []
    for glyph in glyphs:
        for char in canonical_visible(glyph.char):
            chars.append(char)
            mapped.append(glyph)
    return "".join(chars), mapped


def crop_for_anchors(page: fitz.Page, left_anchor: str, between: str,
                     right_anchor: str, *, dpi: int = 300) -> bytes | None:
    text, mapped = glyph_compact(page_glyphs(page))
    needle = left_anchor + between + right_anchor
    start = text.find(needle)
    if start < 0 or text.find(needle, start + 1) >= 0:
        # Existing DB corrections may differ from the hidden layer; locate the two unique anchors.
        if text.count(left_anchor) != 1 or text.count(right_anchor) != 1:
            return None
        start = text.find(left_anchor)
        end_anchor = text.find(right_anchor, start + len(left_anchor))
        if end_anchor < 0:
            return None
        end = end_anchor + len(right_anchor)
    else:
        end = start + len(needle)
    if start < 0 or end > len(mapped) or end <= start:
        return None
    rect = fitz.Rect(mapped[start].bbox)
    for glyph in mapped[start + 1:end]:
        rect.include_rect(fitz.Rect(glyph.bbox))
    page_rect = page.rect
    rect.x0 = max(page_rect.x0, rect.x0 - 18)
    rect.x1 = min(page_rect.x1, rect.x1 + 18)
    rect.y0 = max(page_rect.y0, rect.y0 - 12)
    rect.y1 = min(page_rect.y1, rect.y1 + 12)
    if rect.is_empty or rect.width * rect.height > page_rect.width * page_rect.height * 0.72:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), clip=rect, alpha=False)
    return pix.tobytes("png")


def render_page(page: fitz.Page, *, dpi: int = 150) -> bytes:
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
    return pix.tobytes("png")


def inline_footnotes(page: fitz.Page, baseline: str) -> list[TextDiff]:
    glyphs = page_glyphs(page)
    if not glyphs:
        return []
    text, mapped = glyph_compact(glyphs)
    sizes = [g.size for g in glyphs if CJK_RE.fullmatch(g.char) and g.size > 0]
    if not sizes:
        return []
    median_size = statistics.median(sizes)
    baseline_compact = canonical_visible(baseline)
    output: list[TextDiff] = []
    for index, glyph in enumerate(mapped):
        if not glyph.char.isdigit() or glyph.size <= 0 or glyph.size >= median_size * 0.78:
            continue
        if glyph.bbox[1] < page.rect.height * 0.10 or glyph.bbox[3] > page.rect.height * 0.90:
            continue
        start, end = index, index + 1
        while end < len(mapped) and end - start < 3 and mapped[end].char.isdigit() and mapped[end].line == glyph.line:
            end += 1
        token = text[start:end]
        if not token.isdigit():
            continue
        left_anchor = choose_anchor(text, start, -1)
        right_anchor = choose_anchor(text, end, 1)
        if not left_anchor or not right_anchor:
            continue
        segment = left_anchor + token + right_anchor
        if text.count(segment) != 1 or baseline_compact.count(segment) != 1:
            continue
        output.append(TextDiff(token, token, left_anchor, right_anchor, start, end, "inline_footnote"))
        if len(output) >= 3:
            break
    return output


def protected_reasons(page: fitz.Page, diff: TextDiff, page_meta: dict[str, Any],
                      paddle_text: str) -> list[str]:
    context = diff.left_anchor[-24:] + diff.before + diff.after + diff.right_anchor[:24]
    reasons: list[str] = []
    if not diff.left_anchor or not diff.right_anchor:
        reasons.append("锚点不足或不唯一")
    if not diff_size_allowed(diff) and diff.kind != "inline_footnote":
        reasons.append("差异超过自动范围")
    if DATE_RE.search(context):
        reasons.append("日期")
    if FORMULA_RE.search(context):
        reasons.append("公式")
    if LATIN_RE.search(diff.before + diff.after) or len(LATIN_RE.findall(context)) >= 3:
        reasons.append("外文")
    if re.search(r"(?:马克思|恩格斯|列宁|译者|著者|姓|人名)", context):
        reasons.append("人名或专名")
    if "|" in paddle_text or re.search(r"\t.*\t", paddle_text):
        reasons.append("表格")
    if page_meta.get("reading_order_suspect"):
        reasons.append("阅读顺序疑似变化")
    baseline_length = int(page_meta.get("baseline_length") or 0)
    if baseline_length and (diff.a_start < 60 or diff.a_end > baseline_length - 60):
        reasons.append("页眉页脚或页面边缘")
    return list(dict.fromkeys(reasons))


def authority_for(authority: Sequence[dict[str, Any]], page: sqlite3.Row,
                  diff: TextDiff) -> tuple[str, str]:
    if str(page["book"]) != "文集":
        return "", ""
    for item in authority:
        if item.get("book") != "文集":
            continue
        if item.get("volume") is not None and int(item["volume"]) != int(page["volume"]):
            continue
        if item.get("pdf_page") is not None and int(item["pdf_page"]) != int(page["pdf_page"]):
            continue
        before = canonical_visible(str(item.get("find") or ""))
        after = canonical_visible(str(item.get("replace") or ""))
        configured, _meta = detect_text_diffs(before, after)
        for configured_diff in configured:
            observed_before = canonical_visible(diff.before)
            observed_after = canonical_visible(diff.after)
            if (configured_diff.before == observed_before and configured_diff.after == observed_after) or (
                    configured_diff.after == observed_before and configured_diff.before == observed_after):
                return configured_diff.after, str(item.get("evidence") or "")
    return "", ""


def strict_consensus(diff: TextDiff, *, paddle_value: str, rapid_value: str,
                     glm_value: str, authority_value: str = "",
                     protected: Sequence[str] = (), glm_footnote: bool = False) -> bool:
    consensus = paddle_value == rapid_value == glm_value
    if diff.kind == "inline_footnote":
        consensus = diff.before == paddle_value == rapid_value == glm_value and glm_footnote
    authority_ok = not authority_value or authority_value == diff.after
    return bool(consensus and authority_ok and not protected)


def replace_canonical_segment(raw: str, left_anchor: str, before: str,
                              right_anchor: str, after: str) -> str:
    chars: list[str] = []
    raw_indexes: list[int] = []
    for index, char in enumerate(raw):
        normalized = canonical_visible(char)
        for visible in normalized:
            chars.append(visible)
            raw_indexes.append(index)
    compact = "".join(chars)
    needle = left_anchor + before + right_anchor
    pos = compact.find(needle)
    if pos < 0 or compact.find(needle, pos + 1) >= 0:
        raise AuditError("candidate anchors no longer identify exactly one raw segment")
    segment_start = pos + len(left_anchor)
    segment_end = segment_start + len(before)
    if before:
        raw_start = raw_indexes[segment_start]
        raw_end = raw_indexes[segment_end - 1] + 1
    else:
        raw_start = raw_indexes[segment_start] if segment_start < len(raw_indexes) else len(raw)
        raw_end = raw_start
    return raw[:raw_start] + after + raw[raw_end:]


class HealthGuard:
    def __init__(self, url: str, *, opener: Callable[..., Any] = urllib.request.urlopen):
        self.url = url
        self.opener = opener
        self.baseline_ms = 0.0
        self.latencies: deque[float] = deque(maxlen=25)
        self.web_failures = 0
        self.load_high_streak = 0

    def web_probe(self) -> tuple[bool, float]:
        started = time.perf_counter()
        try:
            request = urllib.request.Request(self.url, headers={"User-Agent": "marx-ocr-health/1.0"})
            with self.opener(request, timeout=8) as response:
                response.read(1)
                ok = 200 <= int(getattr(response, "status", 200)) < 500
        except Exception:
            ok = False
        elapsed = (time.perf_counter() - started) * 1000.0
        return ok, elapsed

    def establish_baseline(self, count: int = 25) -> float:
        values = []
        for _ in range(count):
            ok, elapsed = self.web_probe()
            if ok:
                values.append(elapsed)
            time.sleep(0.03)
        if len(values) < max(5, count // 2):
            raise AuditError("web baseline health check failed")
        self.baseline_ms = percentile(values, 95)
        return self.baseline_ms

    def evaluate(self, *, mem_available_gib: float, load1: float, root_free_gib: float,
                 data_free_gib: float, web_ok: bool, web_latency_ms: float) -> list[str]:
        reasons: list[str] = []
        self.load_high_streak = self.load_high_streak + 1 if load1 > 2.0 else 0
        self.web_failures = self.web_failures + 1 if not web_ok else 0
        if web_ok:
            self.latencies.append(web_latency_ms)
        if mem_available_gib < 2.5:
            reasons.append("可用内存低于2.5GB")
        if self.load_high_streak >= 3:
            reasons.append("一分钟负载连续三次高于2.0")
        if self.web_failures >= 2:
            reasons.append("网页检查连续失败")
        if len(self.latencies) >= self.latencies.maxlen and self.baseline_ms:
            rolling = percentile(list(self.latencies), 95)
            if rolling > max(120.0, self.baseline_ms * 1.5):
                reasons.append("网页滚动P95超过守卫阈值")
        if root_free_gib < 10.0:
            reasons.append("系统盘不足10GB")
        if data_free_gib < 15.0:
            reasons.append("数据盘不足15GB")
        return reasons


def percentile(values: Sequence[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    index = max(0, min(len(ordered) - 1, math.ceil((value / 100.0) * len(ordered)) - 1))
    return ordered[index]


def mem_available_gib() -> float:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) / 1024.0 / 1024.0
    except Exception:
        pass
    return 999.0


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024.0 ** 3


def load1() -> float:
    try:
        return float(os.getloadavg()[0])
    except (AttributeError, OSError):
        return 0.0


def resolve_pdf(project_root: Path, source_file: str) -> Path:
    root = project_root.resolve()
    path = (root / source_file).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AuditError(f"source file escapes project root: {source_file}") from exc
    if not path.is_file():
        raise AuditError(f"source PDF missing: {source_file}")
    return path


class AuditRunner:
    def __init__(self, config: AuditConfig, conn: sqlite3.Connection, *,
                 paddle: PaddleClient | Any, rapid_factory: Callable[[], Any] = RapidClient,
                 glm: GlmClient | Any, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.config = config
        self.conn = conn
        self.paddle = paddle
        self.rapid_factory = rapid_factory
        self.rapid: Any = None
        self.glm = glm
        self.sleep = sleep
        self.monotonic = monotonic
        self.guard = HealthGuard(config.health_url)
        _, self.authority = load_prior_corrections(config.project_root)
        self.provider_secrets = [getattr(paddle, "token", ""), getattr(glm, "key", "")]

    def rapid_client(self) -> Any:
        if self.rapid is None:
            self.rapid = self.rapid_factory()
        return self.rapid

    def guard_snapshot(self) -> tuple[list[str], dict[str, float]]:
        ok, latency = self.guard.web_probe()
        metrics = {"mem_available_gib": mem_available_gib(), "load1": load1(),
                   "root_free_gib": disk_free_gib(Path("/")),
                   "data_free_gib": disk_free_gib(self.config.output_root),
                   "web_ok": 1.0 if ok else 0.0, "web_latency_ms": latency}
        reasons = self.guard.evaluate(mem_available_gib=metrics["mem_available_gib"],
                                      load1=metrics["load1"], root_free_gib=metrics["root_free_gib"],
                                      data_free_gib=metrics["data_free_gib"], web_ok=ok,
                                      web_latency_ms=latency)
        for name, value in metrics.items():
            self.conn.execute("INSERT INTO metrics(at,name,value) VALUES(?,?,?)", (utc_now(), name, value))
        self.conn.commit()
        return reasons, metrics

    def wait_for_health(self, deadline_epoch: float | None = None) -> bool:
        healthy_since: float | None = None
        announced = False
        while True:
            if deadline_epoch is not None and time.time() >= deadline_epoch:
                return False
            reasons, _metrics = self.guard_snapshot()
            now = self.monotonic()
            if reasons:
                healthy_since = None
                if not announced:
                    event(self.conn, "warning", "resource_guard_paused", "; ".join(reasons))
                    announced = True
            else:
                if not announced:
                    return True
                if healthy_since is None:
                    healthy_since = now
                if now - healthy_since >= self.config.healthy_resume_seconds:
                    event(self.conn, "info", "resource_guard_resumed", "连续健康达到恢复窗口")
                    return True
            self.sleep(self.config.guard_poll_seconds)

    def rate_limit(self) -> None:
        last = float(meta_get(self.conn, "last_submission_epoch", "0") or 0)
        wait = max(0.0, (3600.0 / max(1, self.config.per_hour)) - (time.time() - last))
        if wait:
            self.sleep(wait)

    def mark_submission_start(self, page_id: int) -> None:
        with self.conn:
            changed = self.conn.execute(
                "UPDATE pages SET submit_state='submitting',status='submitting',started_at=?,attempts=attempts+1 "
                "WHERE page_id=? AND submit_state='never'", (utc_now(), page_id)
            ).rowcount
            if changed != 1:
                raise AuditError("submission state is not eligible")
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_submission_epoch',?)",
                              (str(time.time()),))

    def submit_or_resume(self, page: sqlite3.Row, image: bytes | None) -> tuple[str, dict[str, Any]] | None:
        page_id = int(page["page_id"])
        state, job_id = str(page["submit_state"]), str(page["job_id"] or "")
        if state == "submitting" and not job_id:
            self.conn.execute(
                "UPDATE pages SET status='review',submit_state='uncertain',error=?,completed_at=? WHERE page_id=?",
                ("提交结果不确定；为防重复计费已禁止自动重发", utc_now(), page_id))
            self.conn.commit()
            return None
        if not job_id:
            if image is None:
                raise AuditError("page image is required for a new submission")
            self.rate_limit()
            self.mark_submission_start(page_id)
            try:
                job_id = self.paddle.submit(image, f"audit-{page_id}.png")
            except TransientProviderError as exc:
                with self.conn:
                    self.conn.execute("UPDATE pages SET submit_state='never',status='planned',error=? WHERE page_id=?",
                                      (redact(exc, self.provider_secrets), page_id))
                self.sleep(60.0)
                return ("retry", {})
            except SubmissionUncertain as exc:
                with self.conn:
                    self.conn.execute("UPDATE pages SET submit_state='uncertain',status='review',error=?,completed_at=? WHERE page_id=?",
                                      (redact(exc, self.provider_secrets), utc_now(), page_id))
                return None
            with self.conn:
                self.conn.execute("UPDATE pages SET submit_state='accepted',status='submitted',job_id=?,error='' WHERE page_id=?",
                                  (job_id, page_id))
            event(self.conn, "info", "paddle_job_accepted", f"page_id={page_id} job_id={job_id}")
        state_name, status = self.paddle.poll(job_id, timeout_seconds=self.config.paddle_poll_seconds,
                                              interval=self.config.paddle_poll_interval)
        if state_name == "failed":
            error = redact(status.get("errorMsg") or status.get("message") or "provider job failed",
                           self.provider_secrets)
            with self.conn:
                self.conn.execute("UPDATE pages SET status='failed',error=?,completed_at=? WHERE page_id=?",
                                  (error, utc_now(), page_id))
            return None
        return job_id, status

    def store_candidate(self, page: sqlite3.Row, diff: TextDiff, *, paddle_value: str,
                        rapid_value: str, glm_value: str, authority_value: str,
                        authority_evidence: str, protected: Sequence[str], crop_path: str,
                        evidence: dict[str, Any], automatic: bool) -> None:
        status = "automatic_candidate" if automatic else "manual_review"
        confidence = "high" if automatic else "review"
        self.conn.execute(
            "INSERT OR REPLACE INTO candidates(page_id,error_type,before_text,after_text,left_anchor,right_anchor,"
            "paddle_value,rapid_value,glm_value,authority_value,authority_evidence,confidence,status,"
            "protected_reasons,evidence_crop,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(page["page_id"]), diff.kind, diff.before, diff.after, diff.left_anchor, diff.right_anchor,
             paddle_value, rapid_value, glm_value, authority_value, authority_evidence, confidence, status,
             canonical_json(list(protected)), crop_path, canonical_json(evidence), utc_now()))
        self.conn.commit()

    def inspect_difference(self, document: fitz.Document, page: sqlite3.Row, diff: TextDiff,
                           page_meta: dict[str, Any], paddle_text: str, index: int) -> bool:
        pdf_page = document[int(page["pdf_page"]) - 1]
        protected = protected_reasons(pdf_page, diff, page_meta, paddle_text)
        authority_value, authority_evidence = authority_for(self.authority, page, diff)
        hard_preclusions = {"锚点不足或不唯一", "差异超过自动范围", "阅读顺序疑似变化"}
        crop = None if hard_preclusions.intersection(protected) else crop_for_anchors(
            pdf_page, diff.left_anchor, diff.before, diff.right_anchor)
        crop_rel = ""
        rapid_value = ""
        glm_value = ""
        glm_footnote = False
        evidence: dict[str, Any] = {"paddle_model": DEFAULT_PADDLE_MODEL,
                                    "rapid_model": "rapidocr-onnxruntime",
                                    "glm_model": DEFAULT_GLM_MODEL,
                                    "similarity": page_meta.get("similarity")}
        if crop is None:
            protected.append("无法可靠定位局部裁剪")
        else:
            crop_rel = f"evidence/{int(page['page_id'])}-{index}.png"
            atomic_write(self.config.run_dir / crop_rel, crop)
            try:
                rapid_text, rapid_rows = self.rapid_client().recognize(crop)
                rapid_between = extract_between(rapid_text, diff.left_anchor, diff.right_anchor)
                rapid_value = rapid_between if rapid_between is not None else ""
                evidence["rapid_confidence_min"] = min(
                    (float(row.get("confidence", 0.0)) for row in rapid_rows), default=0.0)
            except Exception as exc:
                protected.append("RapidOCR失败")
                evidence["rapid_error"] = redact(exc, self.provider_secrets)
            try:
                glm_result, glm_audit = self.glm.verify_crop(crop, left=diff.left_anchor, right=diff.right_anchor)
                glm_value = canonical_visible(str(glm_result.get("between") or ""))
                glm_footnote = glm_result.get("is_footnote") is True
                glm_rel = f"raw/glm/{int(page['page_id'])}-{index}.json"
                atomic_write(self.config.run_dir / glm_rel, json.dumps(glm_audit, ensure_ascii=False, indent=2))
                evidence["glm_path"] = glm_rel
                evidence["glm_readable"] = glm_result.get("readable") is True
            except FatalProviderError:
                raise
            except Exception as exc:
                protected.append("GLM不可用或限流")
                evidence["glm_error"] = redact(exc, self.provider_secrets)
        paddle_value = diff.after
        automatic = strict_consensus(diff, paddle_value=paddle_value, rapid_value=rapid_value,
                                     glm_value=glm_value, authority_value=authority_value,
                                     protected=protected, glm_footnote=glm_footnote)
        self.store_candidate(page, diff, paddle_value=paddle_value, rapid_value=rapid_value,
                             glm_value=glm_value, authority_value=authority_value,
                             authority_evidence=authority_evidence, protected=protected,
                             crop_path=crop_rel, evidence=evidence, automatic=automatic)
        return automatic

    def process_page(self, page: sqlite3.Row) -> str:
        pdf_path = resolve_pdf(self.config.project_root, str(page["source_file"]))
        with fitz.open(pdf_path) as document:
            if not 1 <= int(page["pdf_page"]) <= document.page_count:
                raise AuditError("PDF page is outside source document")
            pdf_page = document[int(page["pdf_page"]) - 1]
            image = None if str(page["job_id"] or "") else render_page(pdf_page, dpi=150)
            result = self.submit_or_resume(page, image)
            if result is None:
                return "review"
            if result[0] == "retry":
                return "retry"
            job_id, status = result
            paddle_text, raw = self.paddle.fetch_result(status)
            raw_rel = f"raw/paddle/{int(page['page_id'])}-{sha256_text(job_id)[:16]}.jsonl"
            atomic_write(self.config.run_dir / raw_rel, raw)
            with self.conn:
                self.conn.execute(
                    "UPDATE pages SET status='paddle_done',paddle_text=?,paddle_hash=?,paddle_raw_path=?,error='' "
                    "WHERE page_id=?", (paddle_text, sha256_text(paddle_text), raw_rel, int(page["page_id"])))
            diffs, page_meta = detect_text_diffs(str(page["baseline_raw"]), paddle_text)
            footnotes = inline_footnotes(pdf_page, str(page["baseline_raw"]))
            seen = {(d.left_anchor, d.before, d.right_anchor, d.kind) for d in diffs}
            diffs.extend(d for d in footnotes if (d.left_anchor, d.before, d.right_anchor, d.kind) not in seen)
            automatic_count = 0
            for index, diff in enumerate(diffs, 1):
                if self.inspect_difference(document, page, diff, page_meta, paddle_text, index):
                    automatic_count += 1
            page_status = "matched" if not diffs else ("candidates" if automatic_count else "review")
            with self.conn:
                self.conn.execute("UPDATE pages SET status=?,completed_at=? WHERE page_id=?",
                                  (page_status, utc_now(), int(page["page_id"])))
            return page_status

    def canary_gate(self) -> None:
        attempted = int(self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE submit_state<>'never' OR status IN ('failed','review')"
        ).fetchone()[0])
        failures = int(self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status='failed' OR submit_state='uncertain'"
        ).fetchone()[0])
        if attempted >= 20 and failures / attempted > 0.10:
            raise FatalProviderError(f"canary failed: {failures}/{attempted} pages failed")

    def run(self) -> None:
        started_epoch = float(meta_get(self.conn, "started_epoch", "0") or 0)
        if not started_epoch:
            started_epoch = time.time()
            meta_set(self.conn, "started_epoch", started_epoch)
            meta_set(self.conn, "deadline_epoch", started_epoch + self.config.hours * 3600.0)
        deadline = float(meta_get(self.conn, "deadline_epoch"))
        baseline = float(meta_get(self.conn, "web_baseline_p95_ms", "0") or 0)
        if not baseline:
            baseline = self.guard.establish_baseline(25)
            meta_set(self.conn, "web_baseline_p95_ms", f"{baseline:.3f}")
        self.guard.baseline_ms = baseline
        self.rapid_client()
        if meta_get(self.conn, "analyzer_version") != ANALYZER_VERSION:
            with self.conn:
                self.conn.execute("DELETE FROM candidates WHERE page_id IN "
                                  "(SELECT page_id FROM pages WHERE paddle_hash<>'')")
                self.conn.execute(
                    "UPDATE pages SET status='paddle_done',completed_at='' WHERE paddle_hash<>''")
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('analyzer_version',?)",
                    (ANALYZER_VERSION,))
            event(self.conn, "info", "analyzer_upgraded",
                  "已完成页面仅使用已保存的 Paddle 结果重新分析；不会重复提交")
        meta_set(self.conn, "state", "running")
        event(self.conn, "info", "run_started", f"baseline_p95_ms={baseline:.1f}")
        try:
            while time.time() < deadline:
                success = int(self.conn.execute(
                    "SELECT COUNT(*) FROM pages WHERE paddle_hash<>''"
                ).fetchone()[0])
                if success >= self.config.max_success:
                    break
                page = self.conn.execute(
                    "SELECT * FROM pages WHERE status IN ('planned','submitted','submitting','paddle_done') "
                    "ORDER BY plan_order LIMIT 1"
                ).fetchone()
                if page is None:
                    break
                if str(page["status"]) == "paddle_done":
                    # The page was fetched before interruption. Re-analysis is intentionally local.
                    with self.conn:
                        self.conn.execute("DELETE FROM candidates WHERE page_id=?", (int(page["page_id"]),))
                    with fitz.open(resolve_pdf(self.config.project_root, str(page["source_file"]))) as doc:
                        diffs, page_meta = detect_text_diffs(str(page["baseline_raw"]), str(page["paddle_text"]))
                        seen = {(d.left_anchor, d.before, d.right_anchor, d.kind) for d in diffs}
                        diffs.extend(
                            d for d in inline_footnotes(
                                doc[int(page["pdf_page"]) - 1], str(page["baseline_raw"]))
                            if (d.left_anchor, d.before, d.right_anchor, d.kind) not in seen
                        )
                        auto = sum(1 for i, d in enumerate(diffs, 1)
                                   if self.inspect_difference(doc, page, d, page_meta, str(page["paddle_text"]), i))
                    with self.conn:
                        self.conn.execute("UPDATE pages SET status=?,completed_at=? WHERE page_id=?",
                                          ("matched" if not diffs else ("candidates" if auto else "review"),
                                           utc_now(), int(page["page_id"])))
                    continue
                if not self.wait_for_health(deadline):
                    break
                try:
                    outcome = self.process_page(page)
                except FatalProviderError:
                    raise
                except TransientProviderError as exc:
                    event(self.conn, "warning", "provider_retry", redact(exc, self.provider_secrets))
                    self.sleep(60.0)
                    continue
                except Exception as exc:
                    with self.conn:
                        self.conn.execute(
                            "UPDATE pages SET status='failed',error=?,completed_at=? WHERE page_id=?",
                            (redact(exc, self.provider_secrets), utc_now(), int(page["page_id"])))
                    event(self.conn, "warning", "page_failed",
                          f"page_id={int(page['page_id'])}: {redact(exc, self.provider_secrets)}")
                    outcome = "failed"
                if outcome != "retry":
                    self.canary_gate()
            meta_set(self.conn, "state", "finalizing")
            finalize_run(self.config, self.conn)
            meta_set(self.conn, "state", "complete")
            meta_set(self.conn, "completed_at", utc_now())
            event(self.conn, "info", "run_complete", "候选库和报告已生成；生产写入为零")
            generate_report(self.config, self.conn)
        except Exception as exc:
            meta_set(self.conn, "state", "failed")
            meta_set(self.conn, "fatal_error", redact(exc, self.provider_secrets))
            event(self.conn, "error", "run_failed", redact(exc, self.provider_secrets))
            generate_report(self.config, self.conn)
            raise


def selected_row_fingerprint(conn: sqlite3.Connection, excluded_ids: set[int]) -> str:
    digest = hashlib.sha256()
    for row in conn.execute("SELECT id,book,volume,source_file,pdf_page,printed_page,raw_text,normalized_text FROM pages ORDER BY id"):
        if int(row[0]) in excluded_ids:
            continue
        digest.update(canonical_json(list(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def identity_fingerprint(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(
            "SELECT id,book,volume,source_file,pdf_page,printed_page FROM pages ORDER BY id"):
        digest.update(canonical_json(list(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_candidate_database(config: AuditConfig, checkpoint: sqlite3.Connection) -> dict[str, Any]:
    candidate_path = config.run_dir / "candidate-corpus.sqlite"
    candidate_temp = config.run_dir / "candidate-corpus.building.sqlite"
    for stale in (candidate_temp, Path(str(candidate_temp) + "-wal"), Path(str(candidate_temp) + "-shm")):
        if stale.exists():
            stale.unlink()
    source = sqlite3.connect(f"file:{config.database.resolve().as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(candidate_temp)
    try:
        source.backup(target, pages=4096)
    finally:
        source.close()
    candidates = checkpoint.execute(
        "SELECT c.*,p.baseline_hash FROM candidates c JOIN pages p ON p.page_id=c.page_id "
        "WHERE c.status='automatic_candidate' ORDER BY c.page_id,c.id"
    ).fetchall()
    selected_ids = {int(row["page_id"]) for row in candidates}
    before_counts = {name: int(target.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                     for name in ("pages", "toc_entries", "toc") if target.execute(
                         "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()}
    unchanged_before = selected_row_fingerprint(target, selected_ids)
    identity_before = identity_fingerprint(target)
    applied = 0
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in candidates:
        grouped[int(row["page_id"])].append(row)
    for page_id, page_candidates in grouped.items():
        current = target.execute(
            "SELECT id,book,volume,source_file,pdf_page,raw_text,normalized_text FROM pages WHERE id=?",
            (page_id,)).fetchone()
        baseline_hash = str(page_candidates[0]["baseline_hash"])
        if current is None or sha256_text(str(current[5])) != baseline_hash:
            raise AuditError(f"candidate baseline hash mismatch for page id {page_id}")
        display_raw = str(current[5])
        search_source = display_raw
        for row in page_candidates:
            if str(row["error_type"]) == "inline_footnote":
                search_source = replace_canonical_segment(
                    search_source, str(row["left_anchor"]), str(row["before_text"]),
                    str(row["right_anchor"]), "")
            else:
                display_raw = replace_canonical_segment(
                    display_raw, str(row["left_anchor"]), str(row["before_text"]),
                    str(row["right_anchor"]), str(row["after_text"]))
                search_source = replace_canonical_segment(
                    search_source, str(row["left_anchor"]), str(row["before_text"]),
                    str(row["right_anchor"]), str(row["after_text"]))
            applied += 1
        target.execute("UPDATE pages SET raw_text=?,normalized_text=? WHERE id=?",
                       (display_raw, normalize_search(search_source), page_id))
    target.commit()
    quick_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
    after_counts = {name: int(target.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                    for name in before_counts}
    unchanged_after = selected_row_fingerprint(target, selected_ids)
    identity_after = identity_fingerprint(target)
    target_gates: list[dict[str, Any]] = []
    for row in candidates:
        updated = target.execute("SELECT raw_text,normalized_text FROM pages WHERE id=?",
                                 (int(row["page_id"]),)).fetchone()
        anchor_probe = normalize_search(str(row["left_anchor"]) + (
            "" if str(row["error_type"]) == "inline_footnote" else str(row["after_text"])
        ) + str(row["right_anchor"]))
        target_gates.append({
            "candidate_id": int(row["id"]), "page_id": int(row["page_id"]),
            "target_probe": anchor_probe, "target_probe_present": anchor_probe in str(updated[1]),
            "display_footnote_preserved": (
                str(row["before_text"]) in str(updated[0]) if str(row["error_type"]) == "inline_footnote" else None
            ),
        })
    target_gates_ok = all(
        row["target_probe_present"] and row["display_footnote_preserved"] is not False
        for row in target_gates
    )
    gates_ok = (quick_check == "ok" and before_counts == after_counts
                and unchanged_before == unchanged_after and identity_before == identity_after
                and target_gates_ok)
    target.close()
    if not gates_ok:
        candidate_temp.unlink(missing_ok=True)
        raise AuditError("candidate database failed integrity/regression gates")
    os.replace(candidate_temp, candidate_path)
    result = {
        "candidate_database": str(candidate_path), "source_database_sha256": sha256_file(config.database),
        "candidate_database_sha256": sha256_file(candidate_path), "automatic_repairs_applied": applied,
        "quick_check": quick_check, "counts_before": before_counts, "counts_after": after_counts,
        "unchanged_rows_sha256_before": unchanged_before, "unchanged_rows_sha256_after": unchanged_after,
        "unchanged_rows_match": unchanged_before == unchanged_after,
        "identity_sha256_before": identity_before, "identity_sha256_after": identity_after,
        "identity_fields_modified": 0 if identity_before == identity_after else 1,
        "target_regression_gates": target_gates,
        "target_regression_gates_ok": target_gates_ok,
        "production_write_count": 0,
    }
    atomic_write(config.run_dir / "candidate-validation.json", json.dumps(result, ensure_ascii=False, indent=2))
    return result


def report_payload(config: AuditConfig, conn: sqlite3.Connection) -> dict[str, Any]:
    page_status = dict(conn.execute("SELECT status,COUNT(*) FROM pages GROUP BY status").fetchall())
    candidate_status = dict(conn.execute("SELECT status,COUNT(*) FROM candidates GROUP BY status").fetchall())
    successful = int(conn.execute("SELECT COUNT(*) FROM pages WHERE paddle_hash<>''").fetchone()[0])
    submissions = int(conn.execute("SELECT COUNT(*) FROM pages WHERE job_id<>''").fetchone()[0])
    sources = int(conn.execute("SELECT COUNT(DISTINCT source_file) FROM pages").fetchone()[0])
    per_volume = [dict(row) for row in conn.execute(
        "SELECT book,volume,COUNT(*) AS planned,SUM(CASE WHEN paddle_hash<>'' THEN 1 ELSE 0 END) AS successful,"
        "SUM(CASE WHEN status='matched' THEN 1 ELSE 0 END) AS matched,"
        "SUM(CASE WHEN status='review' THEN 1 ELSE 0 END) AS review FROM pages GROUP BY book,volume ORDER BY book,volume"
    )]
    candidates = [dict(row) for row in conn.execute(
        "SELECT c.id,c.page_id,p.book,p.volume,p.source_file,p.pdf_page,p.printed_page,c.error_type,"
        "c.before_text,c.after_text,c.left_anchor,c.right_anchor,c.paddle_value,c.rapid_value,c.glm_value,"
        "c.authority_value,c.authority_evidence,c.confidence,c.status,c.protected_reasons,c.evidence_crop,"
        "c.evidence_json "
        "FROM candidates c JOIN pages p ON p.page_id=c.page_id ORDER BY c.status,c.page_id,c.id"
    )]
    failures = [dict(row) for row in conn.execute(
        "SELECT page_id,book,volume,source_file,pdf_page,status,error FROM pages WHERE error<>'' ORDER BY plan_order"
    )]
    metrics: dict[str, Any] = {}
    latencies = [float(row[0]) for row in conn.execute("SELECT value FROM metrics WHERE name='web_latency_ms'")]
    if latencies:
        metrics["web_latency_p95_ms"] = percentile(latencies, 95)
        metrics["web_latency_max_ms"] = max(latencies)
    metrics["web_baseline_p95_ms"] = float(meta_get(conn, "web_baseline_p95_ms", "0") or 0)
    for row in conn.execute(
            "SELECT name,value FROM metrics WHERE id IN (SELECT MAX(id) FROM metrics GROUP BY name)"):
        if str(row[0]) != "web_latency_ms":
            metrics[str(row[0])] = float(row[1])
    validation_path = config.run_dir / "candidate-validation.json"
    validation: dict[str, Any] = {}
    if validation_path.is_file():
        try:
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            validation = {}
    return {
        "schema_version": 1, "audit_version": AUDIT_VERSION, "run_id": config.run_id,
        "state": meta_get(conn, "state", "unknown"), "created_at": meta_get(conn, "created_at"),
        "completed_at": meta_get(conn, "completed_at"), "planned_pages": int(meta_get(conn, "planned_pages", "0") or 0),
        "successful_paddle_pages": successful, "unique_paddle_submissions": submissions,
        "unique_source_pdfs": sources,
        "paddle_model": DEFAULT_PADDLE_MODEL, "rapid_model": "rapidocr-onnxruntime",
        "glm_model": DEFAULT_GLM_MODEL, "luna_server_dependency": False,
        "estimated_paddle_cost_rmb": round(successful * PADDLE_PRICE_RMB_PER_PAGE, 2),
        "production_write_count": int(meta_get(conn, "production_write_count", "0") or 0),
        "page_status": page_status, "candidate_status": candidate_status,
        "per_volume": per_volume, "candidates": candidates, "failures": failures, "metrics": metrics,
        "candidate_validation": validation,
        "acceptance": {
            "all_62_sources": sources == EXPECTED_SOURCE_COUNT,
            "at_least_950_successful": successful >= 950,
            "production_write_zero": int(meta_get(conn, "production_write_count", "0") or 0) == 0,
        },
    }


def generate_report(config: AuditConfig, conn: sqlite3.Connection) -> dict[str, Any]:
    payload = report_payload(config, conn)
    atomic_write(config.run_dir / "report.json", json.dumps(payload, ensure_ascii=False, indent=2))
    csv_fields = ["id", "page_id", "book", "volume", "source_file", "pdf_page", "printed_page",
                  "error_type", "before_text", "after_text", "left_anchor", "right_anchor",
                  "paddle_value", "rapid_value", "glm_value", "authority_value", "authority_evidence",
                  "confidence", "status", "protected_reasons", "evidence_crop", "evidence_json"]
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=csv_fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(payload["candidates"])
    atomic_write(config.run_dir / "report.csv", out.getvalue())
    candidate_rows = []
    for row in payload["candidates"]:
        image_html = ""
        crop = config.run_dir / str(row.get("evidence_crop") or "")
        if crop.is_file():
            image_html = '<img alt="证据裁剪" src="data:image/png;base64,' + base64.b64encode(crop.read_bytes()).decode("ascii") + '">'
        candidate_rows.append(
            "<tr><td>{book} 第{volume}卷<br>PDF {pdf_page}</td><td>{error_type}</td>"
            "<td><del>{before}</del> → <ins>{after}</ins></td><td>Paddle: {paddle}<br>Rapid: {rapid}<br>GLM: {glm}</td>"
            "<td>{status}<br>{protected}</td><td>{image}</td></tr>".format(
                book=html.escape(str(row["book"])), volume=int(row["volume"]), pdf_page=int(row["pdf_page"]),
                error_type=html.escape(str(row["error_type"])), before=html.escape(str(row["before_text"])),
                after=html.escape(str(row["after_text"])), paddle=html.escape(str(row["paddle_value"])),
                rapid=html.escape(str(row["rapid_value"])), glm=html.escape(str(row["glm_value"])),
                status=html.escape(str(row["status"])), protected=html.escape(str(row["protected_reasons"])), image=image_html))
    volume_rows = "".join(
        f"<tr><td>{html.escape(str(row['book']))}</td><td>{int(row['volume'])}</td><td>{int(row['planned'])}</td>"
        f"<td>{int(row['successful'] or 0)}</td><td>{int(row['matched'] or 0)}</td><td>{int(row['review'] or 0)}</td></tr>"
        for row in payload["per_volume"])
    failures = "".join(
        f"<li>{html.escape(str(row['book']))} 第{int(row['volume'])}卷 PDF {int(row['pdf_page'])}: "
        f"{html.escape(str(row['error']))}</li>" for row in payload["failures"])
    document = f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>OCR修复试点报告 {html.escape(config.run_id)}</title>
<style>body{{font-family:system-ui,-apple-system,'Noto Sans SC',sans-serif;max-width:1280px;margin:32px auto;padding:0 20px;color:#202124}}
h1,h2{{color:#17223b}}.cards{{display:flex;flex-wrap:wrap;gap:12px}}.card{{padding:14px 18px;border:1px solid #dfe3e8;border-radius:10px;background:#fafbfc}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{border:1px solid #dfe3e8;padding:8px;vertical-align:top}}th{{background:#f3f5f7}}
img{{max-width:420px;max-height:220px}}del{{background:#ffe4e4}}ins{{background:#ddf6df;text-decoration:none}}code{{word-break:break-all}}</style></head><body>
<h1>《文集》《全集》24 小时 OCR 修复试点</h1><div class=\"cards\">
<div class=\"card\">运行状态<br><strong>{html.escape(str(payload['state']))}</strong></div>
<div class=\"card\">成功页面<br><strong>{payload['successful_paddle_pages']} / {payload['planned_pages']}</strong></div>
<div class=\"card\">源 PDF<br><strong>{payload['unique_source_pdfs']} / 62</strong></div>
<div class=\"card\">Paddle 估算费用<br><strong>¥{payload['estimated_paddle_cost_rmb']:.2f}</strong></div>
<div class=\"card\">生产写入<br><strong>{payload['production_write_count']}</strong></div></div>
<p>识别链：PaddleOCR‑VL 1.6 → RapidOCR 本地复核 → 免费 glm‑4.6v‑flash 差异裁剪核对。Luna 未作为服务器依赖。</p>
<h2>逐卷统计</h2><table><thead><tr><th>书库</th><th>卷</th><th>计划</th><th>成功</th><th>一致</th><th>复核</th></tr></thead><tbody>{volume_rows}</tbody></table>
<h2>修复候选与证据</h2><table><thead><tr><th>页面</th><th>类型</th><th>前后</th><th>三路证据</th><th>状态</th><th>裁剪</th></tr></thead><tbody>{''.join(candidate_rows)}</tbody></table>
<h2>失败与暂停原因</h2><ul>{failures or '<li>无</li>'}</ul>
<h2>资源与网页影响</h2><pre>{html.escape(json.dumps(payload['metrics'], ensure_ascii=False, indent=2))}</pre>
<p>本报告仅对应候选库。原 PDF 未修改，生产数据库未切换；必须经用户确认后才能另行发布。</p></body></html>"""
    atomic_write(config.run_dir / "report.html", document)
    return payload


def finalize_run(config: AuditConfig, conn: sqlite3.Connection) -> None:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        conn.execute("INSERT INTO metrics(at,name,value) VALUES(?,?,?)",
                     (utc_now(), "process_cpu_seconds", float(usage.ru_utime + usage.ru_stime)))
        conn.execute("INSERT INTO metrics(at,name,value) VALUES(?,?,?)",
                     (utc_now(), "process_peak_rss_kib", float(usage.ru_maxrss)))
        conn.commit()
    except Exception:
        pass
    generate_report(config, conn)
    try:
        build_candidate_database(config, conn)
    finally:
        generate_report(config, conn)


def active_run_id(output_root: Path, explicit: str = "") -> str:
    if explicit:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,80}", explicit):
            raise ValueError("invalid run id")
        output_root.mkdir(parents=True, exist_ok=True)
        atomic_write(output_root / "active-run-id", explicit + "\n")
        return explicit
    env_value = str(os.environ.get("MARX_OCR_RUN_ID") or "").strip()
    if env_value:
        return active_run_id(output_root, env_value)
    marker = output_root / "active-run-id"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return active_run_id(output_root, value)
    value = "pilot-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root.mkdir(parents=True, exist_ok=True)
    atomic_write(marker, value + "\n")
    return value


def build_config(args: argparse.Namespace) -> AuditConfig:
    output_root = Path(args.output_root).resolve()
    run_id = active_run_id(output_root, str(args.run_id or ""))
    feedback = Path(args.feedback_db).resolve() if str(args.feedback_db or "") else None
    return AuditConfig(database=Path(args.database).resolve(), project_root=Path(args.project_root).resolve(),
                       feedback_db=feedback, output_root=output_root, run_id=run_id,
                       hours=float(args.hours), max_success=int(args.max_success), per_hour=int(args.per_hour),
                       expected_sources=int(args.expected_sources), health_url=str(args.health_url),
                       guard_poll_seconds=float(args.guard_poll_seconds),
                       healthy_resume_seconds=float(args.healthy_resume_seconds),
                       paddle_poll_seconds=float(args.paddle_poll_seconds),
                       paddle_poll_interval=float(args.paddle_poll_interval))


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", default="/opt/marx-search/data/corpus.sqlite")
    parser.add_argument("--project-root", default="/opt/marx-search")
    parser.add_argument("--feedback-db", default="/var/www/.marx_search_full/feedback.sqlite3")
    parser.add_argument("--output-root", default="/home/data/marx-search-ocr-audit")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS)
    parser.add_argument("--max-success", type=int, default=SAMPLE_SIZE)
    parser.add_argument("--per-hour", type=int, default=DEFAULT_PER_HOUR)
    parser.add_argument("--expected-sources", type=int, default=EXPECTED_SOURCE_COUNT)
    parser.add_argument("--health-url", default=os.environ.get("MARX_OCR_HEALTH_URL", "http://127.0.0.1:8000/"))
    parser.add_argument("--guard-poll-seconds", type=float, default=60.0)
    parser.add_argument("--healthy-resume-seconds", type=float, default=300.0)
    parser.add_argument("--paddle-poll-seconds", type=float, default=900.0)
    parser.add_argument("--paddle-poll-interval", type=float, default=3.0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="24-hour read-only OCR audit for 文集/全集")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("plan", "build the exact deterministic sample without provider calls"),
                            ("run", "resume or run the audited provider pipeline"),
                            ("report", "regenerate candidate database and reports"),
                            ("status", "print checkpoint status as JSON")):
        child = sub.add_parser(name, help=help_text)
        add_common(child)
        if name == "plan":
            child.add_argument("--force", action="store_true")
        if name == "status":
            child.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    config = build_config(args)
    conn = initialize_run(config, force_plan=bool(getattr(args, "force", False)))
    try:
        if args.command == "plan":
            counts = dict(conn.execute("SELECT sample_kind,COUNT(*) FROM pages GROUP BY sample_kind").fetchall())
            result = {"run_id": config.run_id, "planned_pages": int(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]),
                      "source_pdfs": int(conn.execute("SELECT COUNT(DISTINCT source_file) FROM pages").fetchone()[0]),
                      "sample_kinds": counts, "production_write_count": 0}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "status":
            payload = report_payload(config, conn)
            if bool(getattr(args, "compact", False)):
                payload = {key: payload[key] for key in (
                    "run_id", "state", "planned_pages", "successful_paddle_pages",
                    "unique_paddle_submissions", "unique_source_pdfs", "estimated_paddle_cost_rmb",
                    "production_write_count", "page_status", "candidate_status", "metrics", "acceptance"
                )}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "report":
            finalize_run(config, conn)
            print(str(config.run_dir / "report.html"))
            return 0
        try:
            paddle = PaddleClient(os.environ.get("PADDLEOCR_ACCESS_TOKEN", ""),
                                  base_url=os.environ.get("PADDLEOCR_BASE_URL", DEFAULT_PADDLE_BASE),
                                  model=os.environ.get("PADDLEOCR_MODEL", DEFAULT_PADDLE_MODEL))
            glm = GlmClient(os.environ.get("ZHIPU_API_KEY", ""),
                            base_url=os.environ.get("ZHIPU_BASE_URL", DEFAULT_GLM_BASE),
                            model=DEFAULT_GLM_MODEL)
            AuditRunner(config, conn, paddle=paddle, glm=glm).run()
        except FatalProviderError:
            return 78
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
