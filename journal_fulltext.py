# -*- coding: utf-8 -*-
"""外文期刊「公开全文 → 流式阅读 + 逐段中译」管线。

**边界（很重要）**：本模块只处理**公开可获取**的全文——开放获取期刊自己发布的 PDF，
或作者存入机构仓储库的公开副本。出版商站点用登录、付费或反爬防护挡住的链接一律放弃，
不做任何绕过；拿不到完整全文或译文不完整的文章不会进入网站或邮件。

流程（每篇文章）：
    定位公开 PDF → 下载校验 → PyMuPDF 抽文本层分段
    → 免费 GLM-4 识别结构（扫描件用 GLM-4V）→ PaddleOCR 独立抽样复核/失败兜底
    → MiMo V2.5 非思考逐段中译 → 完整性校验 → 产物落数据盘 + 状态入库

成本：翻译/转录统一走站方 MiMo 账户，不扣用户额度；参考文献区保留原文。

产物与状态库均落 ``MARX_JOURNAL_DATA_ROOT`` 指向的服务器数据盘。
"""
from __future__ import annotations

import json
import hashlib
import concurrent.futures
import difflib
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from runtime_env import secure_db_file
from journal_storage import JOURNAL_ARTICLES_DIR, JOURNAL_ISSUES_DIR, ensure_journal_storage

LOGGER = logging.getLogger("marx_search.journal_fulltext")

# ---------------------------------------------------------------- 路径与常量

FULLTEXT_DIR = JOURNAL_ARTICLES_DIR

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
API_UA = "marx-search-journal-fulltext/1.0 (+https://www.makesizhuyi.com)"

HTTP_TIMEOUT = 40
RESOLVER_TIMEOUT = min(20, HTTP_TIMEOUT)
LANDING_PAGE_TIMEOUT = min(15, HTTP_TIMEOUT)
PDF_DOWNLOAD_TIMEOUT = min(20, HTTP_TIMEOUT)
PDF_MAX_BYTES = int(os.environ.get("MARX_JOURNAL_FT_MAX_PDF_MB", "40")) * 1024 * 1024
PDF_MAX_PAGES = int(os.environ.get("MARX_JOURNAL_FT_MAX_PAGES", "80"))
PDF_MIN_PAGES_WITHOUT_RANGE = max(
    2, int(os.environ.get("MARX_JOURNAL_FT_MIN_PAGES_WITHOUT_RANGE", "3"))
)
OCR_MAX_PAGES = int(os.environ.get("MARX_JOURNAL_FT_OCR_MAX_PAGES", str(PDF_MAX_PAGES)))
TRANSLATE_MAX_CHARS = int(os.environ.get("MARX_JOURNAL_FT_MAX_TR_CHARS", "160000"))
ARTICLES_PER_RUN = int(os.environ.get("MARX_JOURNAL_FT_ARTICLES_PER_RUN", "40"))

MIMO_MODEL = "mimo-v2.5"
MIMO_PROVIDER = "mimo"
MIMO_REASONING_EFFORT = "off"
MIMO_BASE_URL = (os.environ.get("MIMO_BASE_URL") or "https://api.xiaomimimo.com/v1").rstrip("/")

# 识别与翻译分开路由：结构/扫描识别走智谱免费 GLM-4，翻译仍走已验收的 MiMo。
# 免费视觉模型偶发拥堵时，PaddleOCR-VL 既做独立抽样复核，也作为逐页 OCR 兜底。
GLM_BASE_URL = (os.environ.get("ZHIPU_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
GLM_TEXT_MODEL = (
    os.environ.get("MARX_JOURNAL_RECOGNITION_MODEL") or "glm-4-flash-250414"
).strip()
GLM_VISION_MODEL = (
    os.environ.get("MARX_JOURNAL_VISION_MODEL") or "glm-4v-flash"
).strip()
PADDLEOCR_BASE_URL = (
    os.environ.get("PADDLEOCR_BASE_URL") or "https://paddleocr.aistudio-app.com"
).rstrip("/")
PADDLEOCR_MODEL = (
    os.environ.get("MARX_JOURNAL_PADDLEOCR_MODEL") or "PaddleOCR-VL-1.6"
).strip()
PADDLEOCR_VERIFY_ENABLED = str(
    os.environ.get("MARX_JOURNAL_PADDLEOCR_VERIFY", "1")
).strip().lower() not in {"0", "false", "no", "off"}
PADDLEOCR_VERIFY_MIN_SIMILARITY = max(
    0.0,
    min(1.0, float(os.environ.get("MARX_JOURNAL_PADDLEOCR_MIN_SIMILARITY", "0.62"))),
)
PADDLEOCR_POLL_SECONDS = max(
    30, int(os.environ.get("MARX_JOURNAL_PADDLEOCR_POLL_SECONDS", "300"))
)

# 单次送译的字符预算：免费档单次输出上限 4096 token，留足余量避免截断。
_TR_CHUNK_CHARS = 1600
_TR_MAX_RETRY = 3
_SEG_MARK = re.compile(r"\[\[(\d+)\]\]")

STATUSES = ("pending", "processing", "ready", "unavailable", "failed")

_WAL_ENABLED = False
_DB_LOCK = threading.Lock()
_CORE_RATE_LOCK = threading.Lock()
_CORE_LAST_REQUEST = 0.0


# ---------------------------------------------------------------- 状态库


def _connect() -> sqlite3.Connection:
    from journal_alerts import DB_PATH

    ensure_journal_storage()
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    secure_db_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 8000")
    global _WAL_ENABLED
    if not _WAL_ENABLED:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            _WAL_ENABLED = True
        except sqlite3.Error:
            pass
    return conn


def init_fulltext_db() -> Path:
    from journal_alerts import DB_PATH, init_journal_alerts_db

    init_journal_alerts_db()
    FULLTEXT_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS journal_fulltext (
                article_id      INTEGER PRIMARY KEY,
                batch_id        INTEGER,
                status          TEXT NOT NULL DEFAULT 'pending',
                pdf_url         TEXT NOT NULL DEFAULT '',
                pdf_host_type   TEXT NOT NULL DEFAULT '',
                pdf_bytes       INTEGER NOT NULL DEFAULT 0,
                pdf_sha256      TEXT NOT NULL DEFAULT '',
                page_count      INTEGER NOT NULL DEFAULT 0,
                para_count      INTEGER NOT NULL DEFAULT 0,
                translated      INTEGER NOT NULL DEFAULT 0,
                required_translations INTEGER NOT NULL DEFAULT 0,
                src_lang        TEXT NOT NULL DEFAULT '',
                ocr_pages       INTEGER NOT NULL DEFAULT 0,
                provider        TEXT NOT NULL DEFAULT 'mimo',
                model           TEXT NOT NULL DEFAULT '',
                reasoning_effort TEXT NOT NULL DEFAULT 'off',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_retry_at   TEXT NOT NULL DEFAULT '',
                error           TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL DEFAULT '',
                updated_at      TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_journal_fulltext_status
                ON journal_fulltext(status);
            CREATE INDEX IF NOT EXISTS idx_journal_fulltext_batch
                ON journal_fulltext(batch_id);
            """
        )
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(journal_fulltext)").fetchall()}
        additions = {
            "pdf_sha256": "TEXT NOT NULL DEFAULT ''",
            "required_translations": "INTEGER NOT NULL DEFAULT 0",
            "provider": "TEXT NOT NULL DEFAULT 'mimo'",
            "reasoning_effort": "TEXT NOT NULL DEFAULT 'off'",
            "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_retry_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE journal_fulltext ADD COLUMN {name} {ddl}")
        conn.commit()
    return DB_PATH


def _now_text() -> str:
    from journal_alerts import utc_now_text

    return utc_now_text()


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else {k: row[k] for k in row.keys()}


def get_fulltext_state(article_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM journal_fulltext WHERE article_id = ?", (int(article_id),)
        ).fetchone()
    return _row_to_dict(row)


def fulltext_states_for_batch(batch_id: int) -> dict[int, dict]:
    """一次取回整批状态，供列表页判断哪些文章可读（避免逐篇查库）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM journal_fulltext WHERE batch_id = ?", (int(batch_id),)
        ).fetchall()
    return {int(r["article_id"]): dict(r) for r in rows}


def list_processing_states(batch_id: int, *, limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT f.*, a.title, a.title_zh, a.journal_name, a.ai_discipline
            FROM journal_fulltext f
            JOIN journal_articles a ON a.id = f.article_id
            WHERE f.batch_id = ?
            ORDER BY CASE f.status WHEN 'failed' THEN 0 WHEN 'unavailable' THEN 1
                         WHEN 'processing' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END,
                     f.updated_at DESC
            LIMIT ?
            """,
            (int(batch_id), max(1, int(limit))),
        ).fetchall()
    items: list[dict] = []
    for row in rows:
        item = dict(row)
        recognition: dict = {}
        try:
            provenance = json.loads(str(item.get("provenance_json") or "{}"))
            if isinstance(provenance, dict) and isinstance(provenance.get("recognition"), dict):
                recognition = dict(provenance["recognition"])
        except (TypeError, ValueError, json.JSONDecodeError):
            recognition = {}
        review = recognition.get("text_layer_review")
        if isinstance(review, dict):
            item["recognition_qa_status"] = str(review.get("status") or "")
            item["recognition_qa_similarity"] = review.get("similarity")
            item["recognition_qa_page"] = int(review.get("page") or 0)
            item["recognition_paddle_fallback_pages"] = 0
        else:
            scan = recognition.get("scan_ocr") if isinstance(recognition.get("scan_ocr"), dict) else {}
            similarities = list(scan.get("similarities") or []) if scan else []
            item["recognition_qa_status"] = (
                "passed" if similarities and not int(scan.get("paddle_fallback_pages") or 0)
                else "fallback" if int(scan.get("paddle_fallback_pages") or 0)
                else ""
            )
            item["recognition_qa_similarity"] = scan.get("average_similarity") if scan else None
            item["recognition_qa_page"] = 0
            item["recognition_paddle_fallback_pages"] = int(scan.get("paddle_fallback_pages") or 0)
        item["recognition_structure_model"] = str(recognition.get("structure_model") or "")
        items.append(item)
    return items


def ready_article_ids(article_ids: Iterable[int]) -> set[int]:
    ids = [int(x) for x in article_ids]
    if not ids:
        return set()
    out: set[int] = set()
    with _connect() as conn:
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT article_id FROM journal_fulltext "
                f"WHERE status = 'ready' AND article_id IN ({placeholders})",
                chunk,
            ).fetchall()
            out.update(int(r["article_id"]) for r in rows)
    return out


def _upsert_state(article_id: int, **fields: Any) -> None:
    fields.setdefault("updated_at", _now_text())
    with _DB_LOCK, _connect() as conn:
        exists = conn.execute(
            "SELECT * FROM journal_fulltext WHERE article_id = ?", (int(article_id),)
        ).fetchone()
        pipeline_status = str(fields.get("status") or "")
        if pipeline_status == "processing":
            fields.setdefault("attempts", int(exists["attempts"] or 0) + 1 if exists else 1)
            fields.setdefault("next_retry_at", "")
        elif pipeline_status == "failed" and "next_retry_at" not in fields:
            attempts = int(fields.get("attempts") or (exists["attempts"] if exists else 1) or 1)
            delay_minutes = min(24 * 60, 15 * (2 ** max(0, attempts - 1)))
            fields["next_retry_at"] = (
                datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
            ).isoformat(timespec="seconds")
        elif pipeline_status == "unavailable" and "next_retry_at" not in fields:
            # OA locations often appear or recover shortly after the metadata
            # record.  Keep the active weekly issue moving without hammering
            # publishers on every hourly worker tick.
            fields["next_retry_at"] = (
                datetime.now(timezone.utc) + timedelta(hours=3)
            ).isoformat(timespec="seconds")
        if exists:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE journal_fulltext SET {sets} WHERE article_id = ?",
                (*fields.values(), int(article_id)),
            )
        else:
            fields.setdefault("created_at", _now_text())
            cols = ", ".join(["article_id", *fields])
            marks = ", ".join("?" for _ in range(len(fields) + 1))
            conn.execute(
                f"INSERT INTO journal_fulltext({cols}) VALUES({marks})",
                (int(article_id), *fields.values()),
            )
        if pipeline_status == "unavailable":
            conn.execute(
                "UPDATE journal_articles SET status='fulltext_unavailable', updated_at=? WHERE id=?",
                (fields.get("updated_at") or _now_text(), int(article_id)),
            )
        elif pipeline_status == "failed":
            conn.execute(
                "UPDATE journal_articles SET status='processing_failed', updated_at=? WHERE id=?",
                (fields.get("updated_at") or _now_text(), int(article_id)),
            )
        conn.commit()


# ---------------------------------------------------------------- 产物读写


def article_dir(article_id: int) -> Path:
    return FULLTEXT_DIR / str(int(article_id))


def _doc_path(article_id: int) -> Path:
    return article_dir(article_id) / "doc.json"


def _pdf_path(article_id: int) -> Path:
    return article_dir(article_id) / "source.pdf"


def load_document(article_id: int) -> dict | None:
    """读回某篇的流式阅读产物（段落 + 中译）。没有产物返回 None。"""
    path = _doc_path(article_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("journal fulltext doc unreadable (%s): %s", article_id, exc)
        return None


def _save_document(article_id: int, doc: dict) -> None:
    directory = article_dir(article_id)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "doc.json.tmp"
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _doc_path(article_id))


def _save_provenance(article_id: int, provenance: dict) -> None:
    directory = article_dir(article_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "provenance.json"
    tmp = directory / "provenance.json.tmp"
    tmp.write_text(json.dumps(provenance, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def purge_articles(article_ids: Iterable[int]) -> int:
    """删除这些文章的全文产物与状态（供「只保留最近 7 期」的保留策略调用）。"""
    ids = [int(x) for x in article_ids]
    if not ids:
        return 0
    removed = 0
    for article_id in ids:
        directory = article_dir(article_id)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    with _DB_LOCK, _connect() as conn:
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM journal_fulltext WHERE article_id IN ({placeholders})", chunk
            )
        conn.commit()
    return removed


def purge_source_pdfs(*, retain_issues: int = 12) -> dict[str, int]:
    """Remove only source PDFs older than the newest N public issues.

    Reflow JSON and provenance are retained indefinitely, so historical reader
    links stay valid and the original public location remains auditable.
    """
    from journal_alerts import _connect as journal_connect

    keep = max(1, int(retain_issues))
    with journal_connect() as conn:
        issue_rows = conn.execute(
            "SELECT id FROM journal_digests WHERE status IN ('sent','archived') ORDER BY id DESC"
        ).fetchall()
        old_issue_ids = [int(row["id"]) for row in issue_rows[keep:]]
        if not old_issue_ids:
            return {"issues": 0, "pdfs": 0, "bytes": 0}
        placeholders = ",".join("?" for _ in old_issue_ids)
        rows = conn.execute(
            f"SELECT article_id FROM journal_fulltext WHERE batch_id IN ({placeholders})",
            old_issue_ids,
        ).fetchall()
    removed = 0
    reclaimed = 0
    ids: list[int] = []
    for row in rows:
        article_id = int(row["article_id"])
        path = _pdf_path(article_id)
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            LOGGER.warning("could not purge old journal PDF %s", path, exc_info=True)
            continue
        ids.append(article_id)
        removed += 1
        reclaimed += size
    if ids:
        with _connect() as conn:
            conn.executemany(
                "UPDATE journal_fulltext SET pdf_bytes = 0, updated_at = ? WHERE article_id = ?",
                [(_now_text(), article_id) for article_id in ids],
            )
            conn.commit()
    return {"issues": len(old_issue_ids), "pdfs": removed, "bytes": reclaimed}


def storage_usage() -> dict:
    """数据盘占用概览（控制台展示用）。"""
    total = 0
    files = 0
    if FULLTEXT_DIR.exists():
        for root, _dirs, names in os.walk(FULLTEXT_DIR):
            for name in names:
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                    files += 1
                except OSError:
                    continue
    return {"bytes": total, "files": files, "path": str(FULLTEXT_DIR)}


def write_issue_snapshot(digest: dict, articles: list[dict]) -> Path:
    """Persist the immutable issue manifest on the data disk at approval/send."""
    ensure_journal_storage()
    issue_key = str(digest.get("issue_key") or f"issue-{int(digest['id'])}")
    target_dir = JOURNAL_ISSUES_DIR / issue_key
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "issue": {
            key: digest.get(key)
            for key in ("id", "issue_key", "period_start", "period_end", "status", "sent_at")
        },
        "articles": [
            {
                "id": int(article["id"]),
                "discipline": article.get("ai_discipline") or "",
                "title_en": article.get("title") or "",
                "title_zh": article.get("title_zh") or "",
                "document_sha256": _sha256_file(_doc_path(int(article["id"])))
                if _doc_path(int(article["id"])).exists() else "",
            }
            for article in articles
        ],
        "generated_at": _now_text(),
    }
    target = target_dir / "issue.json"
    tmp = target_dir / "issue.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------- HTTP 工具


def _assert_public_http_url(url: str) -> None:
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported URL")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("private host")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except socket.gaierror as exc:
        raise ValueError("host resolution failed") from exc
    for raw in addresses:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("private address")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_open(req: urllib.request.Request, *, timeout: int):
    _assert_public_http_url(req.full_url)
    return urllib.request.build_opener(_SafeRedirectHandler()).open(req, timeout=timeout)


def _http_get(
    url: str,
    *,
    ua: str = API_UA,
    timeout: int = HTTP_TIMEOUT,
    accept: str = "*/*",
    headers: dict[str, str] | None = None,
) -> bytes:
    request_headers = {"User-Agent": ua, "Accept": accept}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    with _safe_open(req, timeout=timeout) as resp:
        return resp.read(8 * 1024 * 1024 + 1)


def _http_json(
    url: str, *, timeout: int = HTTP_TIMEOUT, headers: dict[str, str] | None = None
) -> Any:
    return json.loads(_http_get(url, timeout=timeout, headers=headers).decode("utf-8", "ignore"))


def _polite_email() -> str:
    """Unpaywall/OpenAlex 的礼貌池联系邮箱：优先站点发信邮箱，可用 env 覆盖。"""
    override = (os.environ.get("MARX_JOURNAL_CONTACT_EMAIL") or "").strip()
    if override:
        return override
    try:
        from journal_alerts import load_smtp_config

        email = str(load_smtp_config().from_email or "").strip()
        if email and "@" in email:
            return email
    except Exception:
        pass
    return "journal-alerts@makesizhuyi.com"


# ---------------------------------------------------------------- 公开 PDF 定位

# No publisher is rejected by name: a genuinely public direct PDF is eligible.
# Authentication, paywalls, CAPTCHA and anti-bot responses fail naturally and
# are never bypassed.  Network-address checks independently block SSRF targets.
_BLOCKED_PDF_HOSTS: tuple[str, ...] = (
    # Never accept shadow-library endpoints, even if a third-party metadata
    # aggregator accidentally exposes one.  The product is public-OA only.
    "sci-hub.se", "sci-hub.ru", "sci-hub.st", "libgen.is", "library.lol",
)


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def _is_blocked_host(url: str) -> bool:
    host = _host_of(url)
    return any(host == h or host.endswith("." + h) for h in _BLOCKED_PDF_HOSTS)


def _candidate_urls_from_unpaywall(doi: str) -> list[tuple[str, str]]:
    """Unpaywall 的 OA 位置，**仓储库副本优先**（出版商站点多被反爬挡住）。"""
    if not doi:
        return []
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={urllib.parse.quote(_polite_email())}"
    try:
        data = _http_json(url, timeout=RESOLVER_TIMEOUT)
    except Exception as exc:
        LOGGER.debug("unpaywall lookup failed (%s): %s", doi, exc)
        return []
    if not isinstance(data, dict) or not data.get("is_oa"):
        return []
    out: list[tuple[str, str]] = []
    locations = list(data.get("oa_locations") or [])
    best = data.get("best_oa_location") or {}
    if best:
        locations.insert(0, best)
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        host_type = str(loc.get("host_type") or "")
        for key in ("url_for_pdf", "url"):
            candidate = str(loc.get(key) or "").strip()
            if candidate:
                out.append((candidate, host_type))
    # 仓储库在前、出版商在后
    out.sort(key=lambda item: 0 if item[1] == "repository" else 1)
    return out


def _candidate_urls_from_openalex(doi: str) -> list[tuple[str, str]]:
    if not doi:
        return []
    params = {"mailto": _polite_email()}
    api_key = str(os.environ.get("OPENALEX_API_KEY") or "").strip()
    if api_key:
        params["api_key"] = api_key
    url = (
        f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}"
        f"?{urllib.parse.urlencode(params)}"
    )
    try:
        data = _http_json(url, timeout=RESOLVER_TIMEOUT)
    except Exception as exc:
        LOGGER.debug("openalex lookup failed (%s): %s", doi, exc)
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    work_id = str(data.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
    if api_key and re.fullmatch(r"W\d+", work_id):
        # OpenAlex's official cached-content endpoint can succeed when the
        # publisher URL is blocked.  Keep the credential out of provenance;
        # _download_request injects it only into the outbound request.
        out.append((f"https://content.openalex.org/works/{work_id}.pdf", "openalex-cache"))
    locations = list(data.get("locations") or [])
    best = data.get("best_oa_location") or {}
    if best:
        locations.insert(0, best)
    for loc in locations:
        if not isinstance(loc, dict) or not loc.get("is_oa"):
            continue
        source = loc.get("source") or {}
        host_type = str((source or {}).get("type") or "")
        for key in ("pdf_url", "landing_page_url"):
            candidate = str(loc.get(key) or "").strip()
            if candidate:
                out.append((candidate, host_type))
    oa_url = str(((data.get("open_access") or {}).get("oa_url")) or "").strip()
    if oa_url:
        out.append((oa_url, ""))
    return out


def _core_rate_limit() -> None:
    """Respect CORE's free-tier limit of five requests per ten seconds."""
    global _CORE_LAST_REQUEST
    with _CORE_RATE_LOCK:
        delay = 2.05 - (time.monotonic() - _CORE_LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        _CORE_LAST_REQUEST = time.monotonic()


def _candidate_urls_from_core(doi: str) -> list[tuple[str, str]]:
    api_key = str(os.environ.get("CORE_API_KEY") or "").strip()
    if not doi or not api_key:
        return []
    query = urllib.parse.urlencode({"q": f"doi:{doi}", "limit": 3})
    try:
        _core_rate_limit()
        data = _http_json(
            f"https://api.core.ac.uk/v3/search/works?{query}",
            timeout=RESOLVER_TIMEOUT,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    except Exception as exc:
        LOGGER.debug("CORE lookup failed (%s): %s", doi, exc)
        return []
    expected = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi.strip(), flags=re.I).casefold()
    out: list[tuple[str, str]] = []
    for item in (data.get("results") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        returned = str(item.get("doi") or "")
        returned = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", returned.strip(), flags=re.I).casefold()
        if returned != expected:
            continue
        core_id = str(item.get("id") or "").strip()
        if core_id.isdigit():
            out.append((f"https://api.core.ac.uk/v3/outputs/{core_id}/download", "core-cache"))
        for value in [item.get("downloadUrl"), *(item.get("sourceFulltextUrls") or [])]:
            candidate = str(value or "").strip()
            if candidate:
                out.append((candidate, "core-repository"))
    return out


def _candidate_urls_from_metadata(article: dict) -> list[tuple[str, str]]:
    """Use every link already discovered by OpenAlex/Crossref/DOAJ before new API calls."""
    metadata = article.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []
    out: list[tuple[str, str]] = []
    for loc in metadata.get("openalex_locations") or []:
        if not isinstance(loc, dict) or not loc.get("is_oa"):
            continue
        host_type = str(loc.get("host_type") or "openalex")
        for key in ("pdf_url", "landing_page_url"):
            value = str(loc.get(key) or "").strip()
            if value:
                out.append((value, f"openalex:{host_type}"))
    for link in metadata.get("crossref_links") or []:
        if not isinstance(link, dict):
            continue
        value = str(link.get("url") or "").strip()
        content_type = str(link.get("content_type") or "").lower()
        if value and (
            "pdf" in content_type or ".pdf" in value.lower() or "downloadpdf" in value.lower()
            or ("html" in content_type and not value.lower().endswith(".xml"))
        ):
            out.append((value, "crossref"))
    resource = str(metadata.get("crossref_resource_url") or "").strip()
    if resource and not resource.lower().endswith(".xml"):
        out.append((resource, "crossref"))
    for link in metadata.get("doaj_links") or []:
        if isinstance(link, dict) and str(link.get("url") or "").strip():
            out.append((str(link["url"]).strip(), "doaj"))
    for value in metadata.get("discovery_urls") or []:
        if str(value or "").strip():
            out.append((str(value).strip(), "discovery"))
    return out


def _candidate_urls_from_crossref(doi: str) -> list[tuple[str, str]]:
    if not doi:
        return []
    params = urllib.parse.urlencode({"mailto": _polite_email()})
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?{params}"
    try:
        data = _http_json(url, timeout=RESOLVER_TIMEOUT)
    except Exception as exc:
        LOGGER.debug("crossref fulltext lookup failed (%s): %s", doi, exc)
        return []
    message = (data.get("message") or {}) if isinstance(data, dict) else {}
    out: list[tuple[str, str]] = []
    for link in message.get("link") or []:
        if not isinstance(link, dict):
            continue
        value = str(link.get("URL") or "").strip()
        content_type = str(link.get("content-type") or "").lower()
        if value and (
            "pdf" in content_type or ".pdf" in value.lower() or "downloadpdf" in value.lower()
            or ("html" in content_type and not value.lower().endswith(".xml"))
        ):
            out.append((value, "crossref"))
    resource = str((((message.get("resource") or {}).get("primary") or {}).get("URL")) or "").strip()
    if resource and not resource.lower().endswith(".xml"):
        out.append((resource, "crossref"))
    landing = str(message.get("URL") or "").strip()
    if landing:
        out.append((landing, "crossref"))
    return out


def _candidate_urls_from_semantic_scholar(doi: str) -> list[tuple[str, str]]:
    if not doi:
        return []
    paper_id = "DOI:" + doi
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/"
        + urllib.parse.quote(paper_id, safe=":")
        + "?fields=url,isOpenAccess,openAccessPdf,externalIds"
    )
    api_key = str(os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    headers = {"x-api-key": api_key} if api_key else None
    try:
        data = _http_json(url, timeout=RESOLVER_TIMEOUT, headers=headers)
    except Exception as exc:
        LOGGER.debug("semantic scholar lookup failed (%s): %s", doi, exc)
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    oa = data.get("openAccessPdf") or {}
    if isinstance(oa, dict):
        value = str(oa.get("url") or "").strip()
        if value:
            out.append((value, "semantic-scholar"))
    return out


def _candidate_urls_from_doaj(doi: str) -> list[tuple[str, str]]:
    if not doi:
        return []
    query = "index.doi.exact:" + doi
    url = (
        "https://doaj.org/api/search/articles/"
        + urllib.parse.quote(query, safe="")
        + "?pageSize=3"
    )
    try:
        data = _http_json(url, timeout=RESOLVER_TIMEOUT)
    except Exception as exc:
        LOGGER.debug("DOAJ lookup failed (%s): %s", doi, exc)
        return []
    out: list[tuple[str, str]] = []
    for result in (data.get("results") or []) if isinstance(data, dict) else []:
        bib = (result or {}).get("bibjson") or {}
        for link in bib.get("link") or []:
            if not isinstance(link, dict):
                continue
            value = str(link.get("url") or "").strip()
            if value:
                out.append((value, "doaj"))
    return out


def _listify(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _candidate_urls_from_openaire(doi: str) -> list[tuple[str, str]]:
    """Return repository/publisher fulltext URLs aggregated by OpenAIRE."""
    if not doi:
        return []
    url = "https://api.openaire.eu/search/publications?" + urllib.parse.urlencode(
        {"doi": doi, "OA": "true", "format": "json", "size": "3"}
    )
    try:
        data = _http_json(url, timeout=RESOLVER_TIMEOUT)
    except Exception as exc:
        LOGGER.debug("OpenAIRE lookup failed (%s): %s", doi, exc)
        return []
    response = data.get("response") or {} if isinstance(data, dict) else {}
    results = (response.get("results") or {}).get("result") if isinstance(response, dict) else None
    out: list[tuple[str, str]] = []
    for result in _listify(results):
        try:
            entity = ((result.get("metadata") or {}).get("oaf:entity") or {})
            record = entity.get("oaf:result") or {}
        except AttributeError:
            continue
        for entry in _listify(record.get("fulltext")):
            value = str(entry.get("$") if isinstance(entry, dict) else entry or "").strip()
            if value:
                out.append((value, "openaire-repository"))
    return out


# OJS（开放期刊系统，绝大多数小型开放获取刊都用它）：文章落地页
# /article/view/<id>  →  全文 galley  /article/view/<id>/<galley>  或 /article/download/<id>/<galley>
_OJS_VIEW_RE = re.compile(r"/article/view/(\d+)(?:/(\d+))?", re.I)
_OJS_GALLEY_RE = re.compile(r'href="([^"]*?/article/(?:view|download)/\d+/\d+[^"]*?)"', re.I)


def _resolve_landing_page(url: str) -> list[str]:
    """Extract public PDF links from common journal/repository landing pages."""
    if _is_blocked_host(url):
        return []
    try:
        raw = _http_get(
            url, ua=BROWSER_UA, timeout=LANDING_PAGE_TIMEOUT, accept="text/html,*/*"
        ).decode("utf-8", "ignore")
    except Exception as exc:
        LOGGER.debug("landing page fetch failed (%s): %s", url, exc)
        return []
    out: list[str] = []
    # 学术站点通用元数据（Google Scholar 约定），最可靠
    for match in re.finditer(
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']', raw, re.I
    ):
        out.append(match.group(1))
    for match in re.finditer(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']', raw, re.I
    ):
        out.append(match.group(1))
    # Repository and publisher pages also advertise PDFs through <link> tags.
    for tag in re.findall(r"<link\b[^>]*>", raw, re.I):
        if not re.search(r'(?:application/pdf|type=["\']?pdf)', tag, re.I):
            continue
        href = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if href:
            out.append(urllib.parse.urljoin(url, href.group(1).replace("&amp;", "&")))
    # JSON-LD MediaObject/ScholarlyArticle metadata commonly exposes contentUrl.
    for match in re.finditer(r'["\'](?:contentUrl|encodingUrl)["\']\s*:\s*["\']([^"\']+)["\']', raw, re.I):
        value = match.group(1).replace("\\/", "/")
        if ".pdf" in value.lower() or "/download" in value.lower():
            out.append(urllib.parse.urljoin(url, value))
    # Finally accept explicit PDF/download anchors; the downloader still checks
    # the PDF magic header and the completeness gate before publication.
    for tag in re.findall(r'''<a\b[^>]*href=["'][^"']+["'][^>]*>''', raw, re.I):
        href = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if not href:
            continue
        value = href.group(1).replace("&amp;", "&")
        if re.search(r'(?:\.pdf(?:[?#]|$)|/download(?:/|\?|$)|downloadpdf)', value, re.I):
            out.append(urllib.parse.urljoin(url, value))
    # OJS galley 链接
    for match in _OJS_GALLEY_RE.finditer(raw):
        href = match.group(1).replace("&amp;", "&")
        out.append(urllib.parse.urljoin(url, href))
    # /article/view/123/456 形式的 galley 页本身仍是 HTML，转成 download 直链
    extra: list[str] = []
    for candidate in out:
        m = _OJS_VIEW_RE.search(candidate)
        if m and m.group(2):
            extra.append(candidate.replace(f"/article/view/{m.group(1)}/{m.group(2)}",
                                           f"/article/download/{m.group(1)}/{m.group(2)}"))
    out.extend(extra)
    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in out:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered[:6]


def resolve_pdf_candidates(article: dict) -> list[tuple[str, str]]:
    """Build a deduplicated public-fulltext chain with repository copies first."""
    doi = str(article.get("doi") or "").strip()
    if doi.lower().startswith("http"):
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    candidates: list[tuple[str, str]] = []
    # Wiley's /doi/pdf/ route can return an HTML interstitial even for a CC
    # article. /pdfdirect/ is the publisher's actual PDF route (and is also
    # used by the official Wiley TDM tooling). Datacenter IPs may still be
    # rejected, in which case the owner-side OA relay supplies the same file.
    if doi.lower().startswith("10.1111/"):
        candidates.append((
            f"https://onlinelibrary.wiley.com/doi/pdfdirect/"
            f"{urllib.parse.quote(doi, safe='/.-')}",
            "wiley-pdfdirect",
        ))
    stored = str(article.get("pdf_url") or "").strip()
    if stored:
        candidates.append((stored, "stored"))
    candidates.extend(_candidate_urls_from_metadata(article))
    # These independent public indexes are queried concurrently.  A slow or
    # rate-limited provider therefore costs at most one timeout, not six serial
    # timeouts per article, while result order remains deterministic.
    resolvers = (
        _candidate_urls_from_unpaywall,
        _candidate_urls_from_openalex,
        _candidate_urls_from_core,
        _candidate_urls_from_doaj,
        _candidate_urls_from_openaire,
        _candidate_urls_from_crossref,
        _candidate_urls_from_semantic_scholar,
    )
    if doi:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(resolvers)) as pool:
            futures = [pool.submit(resolver, doi) for resolver in resolvers]
            for future in futures:
                try:
                    candidates.extend(future.result())
                except Exception as exc:
                    LOGGER.debug("public PDF resolver failed (%s): %s", doi, exc)
    if doi:
        candidates.append((f"https://doi.org/{urllib.parse.quote(doi, safe='/')}", "doi"))
    url = str(article.get("url") or "").strip()
    if url:
        candidates.append((url, "landing"))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    per_host: dict[str, int] = {}
    for candidate, host_type in candidates:
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        if _is_preview_pdf_url(candidate):
            continue
        if _is_blocked_host(candidate):
            continue
        if candidate in seen:
            continue
        host = _host_of(candidate)
        if per_host.get(host, 0) >= 3:
            continue
        seen.add(candidate)
        per_host[host] = per_host.get(host, 0) + 1
        out.append((candidate, host_type))
    return out[:20]


def _pdf_candidate_completeness_error(article: dict, path: Path, url: str) -> str:
    """Cheap structural preflight so one bad/preview PDF cannot stop later candidates."""
    try:
        import fitz

        doc = fitz.open(path)
        try:
            page_count = int(doc.page_count)
        finally:
            doc.close()
    except Exception as exc:
        return f"invalid-pdf:{type(exc).__name__}"
    return validate_pdf_completeness(article, {"page_count": page_count}, url)


def _download_pdf(url: str, dest: Path) -> tuple[bool, str]:
    """下载并校验为真 PDF（魔数 + 体积闸）。返回 (成功, 说明)。"""
    if _is_preview_pdf_url(url):
        return False, "preview-pdf-url"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.tmp")
    try:
        req = _download_request(url)
        if _host_of(url) == "api.core.ac.uk":
            _core_rate_limit()
        with _safe_open(req, timeout=PDF_DOWNLOAD_TIMEOUT) as resp:
            head = resp.read(5)
            if not head.startswith(b"%PDF"):
                return False, "not-a-pdf"
            total = len(head)
            with open(tmp, "wb") as fh:
                fh.write(head)
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > PDF_MAX_BYTES:
                        raise ValueError("pdf exceeds size cap")
                    fh.write(chunk)
        os.replace(tmp, dest)
        return True, ""
    except urllib.error.HTTPError as exc:
        return False, f"http{exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}"
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _download_request(url: str) -> urllib.request.Request:
    """Build a credentialed request without ever persisting credentials in the source URL."""
    request_url = str(url or "")
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"}
    host = _host_of(request_url)
    if host == "content.openalex.org":
        api_key = str(os.environ.get("OPENALEX_API_KEY") or "").strip()
        if api_key:
            parsed = urllib.parse.urlsplit(request_url)
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if not any(key == "api_key" for key, _value in query):
                query.append(("api_key", api_key))
            request_url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
            )
    elif host == "api.core.ac.uk":
        api_key = str(os.environ.get("CORE_API_KEY") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    return urllib.request.Request(request_url, headers=headers)


def fetch_public_pdf(article: dict, article_id: int) -> tuple[Path | None, str, str, str]:
    """定位并下载公开 PDF。返回 (路径, 命中的 url, host_type, 失败原因)。"""
    dest = _pdf_path(article_id)
    reasons: list[str] = []
    if dest.exists() and dest.stat().st_size > 0:
        state = get_fulltext_state(article_id) or {}
        cached_url = str(state.get("pdf_url") or article.get("pdf_url") or "")
        if _is_preview_pdf_url(cached_url):
            _quarantine_rejected_pdf(dest, "preview")
            reasons.append("cached:preview-pdf-url")
        else:
            cached_error = _pdf_candidate_completeness_error(article, dest, cached_url)
            if not cached_error:
                return dest, cached_url, str(state.get("pdf_host_type") or ""), ""
            _quarantine_rejected_pdf(dest, cached_error.split(":", 1)[0])
            reasons.append(f"cached:{cached_error}")
    if _is_preview_pdf_url(str(article.get("pdf_url") or "")):
        reasons.append("stored:preview-pdf-url")
    for url, host_type in resolve_pdf_candidates(article):
        ok, reason = _download_pdf(url, dest)
        if ok:
            completeness_error = _pdf_candidate_completeness_error(article, dest, url)
            if not completeness_error:
                return dest, url, host_type, ""
            _quarantine_rejected_pdf(dest, completeness_error.split(":", 1)[0])
            reasons.append(f"{_host_of(url)}:{completeness_error}")
            continue
        reasons.append(f"{_host_of(url)}:{reason}")
        if reason == "not-a-pdf":
            # 落地页 → 解析出真正的全文直链再试
            for resolved in _resolve_landing_page(url):
                if _is_preview_pdf_url(resolved):
                    reasons.append(f"{_host_of(resolved)}:preview-pdf-url")
                    continue
                if _is_blocked_host(resolved):
                    continue
                ok, reason2 = _download_pdf(resolved, dest)
                if ok:
                    completeness_error = _pdf_candidate_completeness_error(article, dest, resolved)
                    if not completeness_error:
                        return dest, resolved, host_type or "landing", ""
                    _quarantine_rejected_pdf(dest, completeness_error.split(":", 1)[0])
                    reasons.append(f"{_host_of(resolved)}:{completeness_error}")
                    continue
                reasons.append(f"{_host_of(resolved)}:{reason2}")
    return None, "", "", "; ".join(reasons[:12]) or "no-open-access-copy"


def _is_preview_pdf_url(url: str) -> bool:
    """Reject publisher preview endpoints even when they return a syntactically valid PDF."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    path = parsed.path.lower()
    query = parsed.query.lower()
    return any(
        marker in path
        for marker in (
            "/previewpdf/", "/preview-pdf/", "/pdf-preview/", "/preview/",
            "_preview.pdf", "-preview.pdf", ".preview.pdf",
        )
    ) or bool(re.search(r"(?:^|&)(?:preview|sample)=(?:1|true|yes)(?:&|$)", query))


def _quarantine_rejected_pdf(path: Path, label: str) -> Path | None:
    """Move a rejected processing cache aside for audit without deleting source data."""
    if not path.exists():
        return None
    safe_label = re.sub(r"[^a-z0-9_-]+", "-", str(label or "rejected").lower()).strip("-") or "rejected"
    target = path.with_name(f"{path.stem}.{safe_label}-{int(time.time())}{path.suffix}")
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target


def _declared_page_count(pages: str) -> int | None:
    """Return the inclusive article page span when citation metadata is trustworthy."""
    match = re.search(r"(?<!\d)(\d{1,4})\s*[-–—]\s*(\d{1,4})(?!\d)", str(pages or ""))
    if not match:
        return None
    first, last = int(match.group(1)), int(match.group(2))
    if last < first:
        return None
    count = last - first + 1
    return count if count >= 2 else None


def validate_pdf_completeness(article: dict, extracted: dict, pdf_url: str = "") -> str:
    """Return an empty string only when the downloaded PDF can plausibly be the whole article.

    A PDF magic header merely proves the file format.  This gate also rejects known
    preview routes and compares the physical page count with the citation page span.
    """
    if _is_preview_pdf_url(pdf_url or str(article.get("pdf_url") or "")):
        return "preview-pdf-url"
    actual = max(0, int(extracted.get("page_count") or 0))
    if actual <= 0:
        return "pdf-has-no-pages"
    if actual > PDF_MAX_PAGES:
        return f"pdf-page-cap-exceeded:{actual}/{PDF_MAX_PAGES}"
    declared = _declared_page_count(str(article.get("pages") or ""))
    # When citation feeds omit a page span, a one/two-page file is more often a
    # cover, abstract or publisher teaser than a complete research paper.  The
    # weekly deliberately prefers a false negative over publishing a preview.
    if declared is None and actual < PDF_MIN_PAGES_WITHOUT_RANGE:
        return f"unverified-short-pdf:{actual}/{PDF_MIN_PAGES_WITHOUT_RANGE}"
    # One page of tolerance covers unnumbered separators and imperfect feed metadata,
    # while still catching two-page publisher previews of 20–60 page articles.
    if declared and actual + 1 < declared:
        return f"truncated-pdf:{actual}/{declared}"
    return ""


# ---------------------------------------------------------------- PDF 抽段

_HEADING_MAX_CHARS = 120
_REFERENCE_HEADINGS = re.compile(
    r"^\s*(references?|bibliograph(y|ie)|works cited|literature cited|referencias|"
    r"bibliograf[ií]a|refer[êe]ncias|bibliografia|literatura|literaturverzeichnis|"
    r"参考文献)\s*:?\s*$",
    re.I,
)
_PAGE_NUMBER_RE = re.compile(r"^[\s\-–—|]*\d{1,4}[\s\-–—|]*$")
_RUNNING_PUBLICATION_RE = re.compile(
    r"^\s*(?:\d{1,4}\s+)?[A-Z][A-Z &:'’.-]{4,}\s+\d+(?:\.\d+)*"
    r"(?:\s*/\s*(?:Spring|Summer|Autumn|Fall|Winter)\s+\d{4})?(?:\s+\d{1,4})?\s*$"
)
# 参考文献之后往往还有「作者简介 / 附录 / 致谢」等正文性小节，遇到它们要退出「参考文献区」，
# 否则这些段落会被当成参考条目而不翻译。
_POST_REFERENCE_HEADINGS = re.compile(
    r"^\s*(about the authors?|author (biograph|information|note)|appendix|annex|"
    r"acknowledge?ments?|notes?|endnotes|sobre (el|los) autor|apêndice|anexo|"
    r"作者简介|附录|致谢)\b",
    re.I,
)
_LAYOUT_FOOTNOTE_START_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,3}[.)]\s+|"
    r"\d{1,3}\s+(?=[A-ZÀ-Þ\[('‘“])|"
    r"\d{1,3}[\s\x00-\x1f]*(?=[A-ZÀ-Þ‘“])|"
    r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+\s*|"
    r"[*†‡§¶]\s*"
    r")"
)
_LAYOUT_NON_NOTE_RE = re.compile(
    r"^\s*\d{1,3}\s+(?:school|department|faculty|university|institute)\b|"
    r"^\s*(?:figure|table|data availability|conflict of interest|open access|copyright|"
    r"full terms|published with license)\b|"
    r"^\s*©|"
    r"@|"
    r"^\s*[A-ZÀ-Þ][A-Za-zÀ-ɏ'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ɏ'’.-]+){1,3}\s*$",
    re.I,
)
_LAYOUT_NOTE_CONTINUATION_RE = re.compile(r"^\s*[a-zà-öø-ÿ]")


def _edge_repeat_key(text: str) -> str:
    """Canonicalise mirrored running heads (page number left on even, right on odd pages)."""
    value = re.sub(r"^\s*\d{1,4}\s+", "", str(text or ""))
    value = re.sub(r"\s+\d{1,4}\s*$", "", value)
    return re.sub(r"\d+", "#", value).strip().casefold()


def _block_text(block: dict) -> str:
    lines = []
    for line in block.get("lines") or []:
        spans = [str(span.get("text") or "") for span in line.get("spans") or []]
        lines.append("".join(spans))
    text = "\n".join(lines)
    # 行末连字符断词还原（PDF 排版常见）；其余换行按空格合并
    text = re.sub(r"(\w)[-‐‑]\n(\w)", r"\1\2", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"[ \t ]{2,}", " ", text).strip()


def _block_font_size(block: dict) -> float:
    sizes = [
        float(span.get("size") or 0)
        for line in block.get("lines") or []
        for span in line.get("spans") or []
    ]
    return max(sizes) if sizes else 0.0


def _block_is_bold(block: dict) -> bool:
    spans = [span for line in block.get("lines") or [] for span in line.get("spans") or []]
    return bool(spans) and sum(1 for span in spans if int(span.get("flags") or 0) & 16) >= max(1, len(spans) // 2)


def _dominant_body_font_size(pages_blocks: list[tuple[int, list[dict], float, float]]) -> float:
    """Estimate body type from central text, without letting small notes win the median.

    Footnote-heavy philosophy papers can devote a large fraction of their PDF
    blocks to 7–9 point notes. A plain median therefore mistakes the note font
    for body type and makes deterministic note detection impossible. Weighting
    the modal size by visible characters in the central page band reliably
    selects the prose font while headings remain too sparse to dominate.
    """
    weights: dict[float, int] = {}
    for _index, blocks, _width, height in pages_blocks:
        for block in blocks:
            text = _block_text(block)
            if len(text) < 20:
                continue
            bbox = block.get("bbox") or (0, 0, 0, 0)
            top = float(bbox[1])
            bottom = float(bbox[3])
            size = _block_font_size(block)
            if not (7.0 <= size <= 15.0 and top >= height * 0.10 and bottom <= height * 0.78):
                continue
            bucket = round(size * 4.0) / 4.0
            weights[bucket] = weights.get(bucket, 0) + min(len(text), 800)
    if not weights:
        return 10.0
    return max(weights, key=lambda size: (weights[size], size))


def _layout_footnote_indexes(blocks: list[dict], *, body_size: float, page_height: float) -> set[int]:
    """Return high-confidence page-bottom note blocks.

    Notes must use visibly smaller type than the article body. A numbered note
    anchors the note band; smaller continuation/equation blocks below it are
    included as well. An unnumbered continuation at the extreme bottom is also
    retained, which covers notes split across physical pages. The conservative
    font and position gates avoid treating numbered arguments or section titles
    as notes.
    """
    candidates: list[tuple[int, float, float, str]] = []
    for index, block in enumerate(blocks):
        text = _block_text(block)
        if not text:
            continue
        bbox = block.get("bbox") or (0, 0, 0, 0)
        top = float(bbox[1])
        size = _block_font_size(block)
        if (
            top >= page_height * 0.45
            and size <= body_size - 0.45
            and not _LAYOUT_NON_NOTE_RE.search(text)
        ):
            candidates.append((index, top, size, text))
    anchors = [
        item for item in candidates
        if _LAYOUT_FOOTNOTE_START_RE.match(item[3])
        and not _LAYOUT_NON_NOTE_RE.match(item[3])
    ]
    indexes: set[int] = set()
    if anchors:
        note_top = min(item[1] for item in anchors)
        indexes.update(item[0] for item in candidates if item[1] >= note_top - 3.0)
    # A long note may continue onto the next page without repeating its number.
    # Require a lowercase continuation at the extreme bottom so captions,
    # affiliations and publisher bands cannot become locked notes.
    indexes.update(
        item[0]
        for item in candidates
        if item[1] >= page_height * 0.78
        and len(re.sub(r"\s+", " ", item[3]).strip()) >= 24
        and _LAYOUT_NOTE_CONTINUATION_RE.match(item[3])
    )
    return indexes


def _column_side(block: dict, page_width: float) -> int | None:
    mid = page_width / 2
    bbox = block.get("bbox") or (0, 0, 0, 0)
    if float(bbox[2]) <= mid + 8:
        return 0
    if float(bbox[0]) >= mid - 8:
        return 1
    return None


def _detect_columns(blocks: list[dict], page_width: float) -> bool:
    """Detect two substantial text columns while ignoring full-width front matter."""
    char_totals = [0, 0]
    for block in blocks:
        side = _column_side(block, page_width)
        text = _block_text(block)
        if side is not None and len(text) >= 20:
            char_totals[side] += len(text)
    return char_totals[0] >= 160 and char_totals[1] >= 160


def _order_two_column_blocks(blocks: list[dict], page_width: float) -> list[dict]:
    """Read left column before right while keeping full-width bands in place."""
    columns: list[list[dict]] = [[], []]
    spanning: list[dict] = []
    substantial: list[dict] = []
    for block in blocks:
        side = _column_side(block, page_width)
        if side is None:
            spanning.append(block)
            continue
        columns[side].append(block)
        if len(_block_text(block)) >= 80:
            substantial.append(block)
    if not substantial:
        return sorted(blocks, key=lambda block: (block["bbox"][1], block["bbox"][0]))
    column_top = min(float(block["bbox"][1]) for block in substantial)
    before = [block for block in spanning if float(block["bbox"][1]) < column_top]
    after = [block for block in spanning if block not in before]
    return [
        *sorted(before, key=lambda block: (block["bbox"][1], block["bbox"][0])),
        *sorted(columns[0], key=lambda block: (block["bbox"][1], block["bbox"][0])),
        *sorted(columns[1], key=lambda block: (block["bbox"][1], block["bbox"][0])),
        *sorted(after, key=lambda block: (block["bbox"][1], block["bbox"][0])),
    ]


def extract_paragraphs(pdf_path: Path) -> dict:
    """把 PDF 文本层抽成有序段落。返回 {paragraphs, page_count, chars, has_text_layer}。

    段落带 page（原刊页序）与 kind（heading/body/reference），供阅读器显示与翻译取舍。
    """
    import fitz  # 延迟导入：仅全文管线需要

    doc = fitz.open(pdf_path)
    try:
        page_count = min(doc.page_count, PDF_MAX_PAGES)
        # 先统计页眉页脚候选：贴边且在多页重复出现的短文本
        edge_counter: dict[str, int] = {}
        pages_blocks: list[tuple[int, list[dict], float, float]] = []
        for index in range(page_count):
            page = doc[index]
            height = float(page.rect.height) or 1.0
            width = float(page.rect.width) or 1.0
            raw = page.get_text("dict")
            blocks = [b for b in (raw.get("blocks") or []) if b.get("type") == 0 and b.get("lines")]
            pages_blocks.append((index, blocks, width, height))
            for block in blocks:
                top = float(block["bbox"][1])
                bottom = float(block["bbox"][3])
                if top < height * 0.075 or bottom > height * 0.925:
                    text = _block_text(block)
                    if text and len(text) <= 200:
                        key = _edge_repeat_key(text)
                        edge_counter[key] = edge_counter.get(key, 0) + 1
        repeat_threshold = max(2, int(page_count * 0.3))
        running = {k for k, v in edge_counter.items() if v >= repeat_threshold}

        # Most born-digital journals expose one visual line as one PDF block.
        # Estimate the true body font globally so a short line is not mistaken for
        # a heading merely because it lacks sentence-final punctuation.
        body_size = _dominant_body_font_size(pages_blocks)

        paragraphs: list[dict] = []
        in_references = False
        body_sizes: list[float] = []
        for index, blocks, width, height in pages_blocks:
            if not blocks:
                continue
            two_columns = _detect_columns(blocks, width)
            if two_columns:
                blocks = _order_two_column_blocks(blocks, width)
            else:
                blocks = sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))
            layout_footnotes = _layout_footnote_indexes(
                blocks, body_size=body_size, page_height=height
            )
            for block_index, block in enumerate(blocks):
                text = _block_text(block)
                if not text:
                    continue
                key = _edge_repeat_key(text)
                if key in running or _PAGE_NUMBER_RE.match(text) or _RUNNING_PUBLICATION_RE.match(text):
                    continue
                size = _block_font_size(block)
                layout = {
                    "bbox": [round(float(value), 2) for value in block.get("bbox") or (0, 0, 0, 0)],
                    "column": (0 if float(block["bbox"][0]) < width / 2 else 1) if two_columns else 0,
                    "page_width": round(float(width), 2),
                    "bold": _block_is_bold(block),
                }
                if _REFERENCE_HEADINGS.match(text):
                    in_references = True
                    paragraphs.append({"page": index + 1, "kind": "heading", "text": text, "size": size, **layout})
                    continue
                if in_references and len(text) <= _HEADING_MAX_CHARS and _POST_REFERENCE_HEADINGS.match(text):
                    in_references = False
                    paragraphs.append({"page": index + 1, "kind": "heading", "text": text, "size": size, **layout})
                    continue
                kind = "reference" if in_references else "body"
                layout_locked = False
                if not in_references and block_index in layout_footnotes:
                    kind = "footnote"
                    layout_locked = True
                if not in_references and len(text) <= _HEADING_MAX_CHARS and (
                    size >= body_size + 1.35
                    or (layout["bold"] and size >= body_size + 0.2)
                    or re.match(r"^\s*(?:abstract|keywords?|introduction|conclusion|notes?|endnotes|appendix)\s*:?[\s.]*$", text, re.I)
                ):
                    kind = "heading"
                    layout_locked = False
                if kind == "body":
                    body_sizes.append(size)
                paragraph = {"page": index + 1, "kind": kind, "text": text, "size": size, **layout}
                if layout_locked:
                    paragraph["_layout_role_locked"] = True
                paragraphs.append(paragraph)

        # 将“每行一个 block”恢复为完整段落，并处理跨栏/跨页续接。
        flow_widths: dict[tuple[int, int], float] = {}
        for para in paragraphs:
            if para.get("kind") not in {"body", "reference"}:
                continue
            bbox = para.get("bbox") or [0, 0, 0, 0]
            key = (int(para.get("page") or 0), int(para.get("column") or 0))
            flow_widths[key] = max(flow_widths.get(key, 0.0), float(bbox[2]) - float(bbox[0]))

        merged: list[dict] = []
        for para in paragraphs:
            join = False
            if merged and para.get("kind") == merged[-1].get("kind") and para.get("kind") in {"body", "reference"}:
                previous = merged[-1]
                same_style = abs(float(para.get("size") or 0) - float(previous.get("size") or 0)) < 0.9
                prev_bbox = previous.get("last_bbox") or previous.get("bbox") or [0, 0, 0, 0]
                box = para.get("bbox") or [0, 0, 0, 0]
                previous_flow_page = int(previous.get("page_end") or previous.get("page") or 0)
                same_page = int(para.get("page") or 0) == previous_flow_page
                same_column = int(para.get("column") or 0) == int(previous.get("column") or 0)
                terminal = bool(re.search(r"[.。!?？！:：”\"')\]]$", str(previous.get("text") or "")))
                expected_width = flow_widths.get(
                    (previous_flow_page, int(previous.get("column") or 0)), 0.0
                )
                previous_width = float(prev_bbox[2]) - float(prev_bbox[0])
                previous_short = bool(expected_width and previous_width < expected_width * 0.76)
                if same_page and same_column and same_style:
                    vertical_gap = float(box[1]) - float(prev_bbox[3])
                    indented = float(box[0]) - float(prev_bbox[0]) > body_size * 0.85
                    join = vertical_gap <= max(8.0, body_size * 0.9) and not (
                        terminal and (previous_short or indented)
                    )
                elif same_page and same_style and int(previous.get("column") or 0) == 0 and int(para.get("column") or 0) == 1:
                    join = not terminal or bool(re.match(r"^[a-zá-úñçāăĉ(]", str(para.get("text") or "")))
                elif int(para.get("page") or 0) == previous_flow_page + 1 and same_style:
                    join = not terminal and bool(re.match(r"^[a-zá-úñçāăĉ(\d]", str(para.get("text") or "")))
                if para.get("kind") == "reference" and re.match(r"^\s*\d+[.)]\s+", str(para.get("text") or "")):
                    join = False
            if join:
                previous_text = str(merged[-1].get("text") or "")
                current_text = str(para.get("text") or "")
                if re.search(r"[A-Za-z][-‐‑]$", previous_text) and re.match(r"^[A-Za-z]", current_text):
                    merged[-1]["text"] = previous_text[:-1] + current_text
                else:
                    merged[-1]["text"] = f"{previous_text} {current_text}".strip()
                prev_bbox = merged[-1].get("bbox") or [0, 0, 0, 0]
                box = para.get("bbox") or [0, 0, 0, 0]
                merged[-1]["bbox"] = [
                    min(float(prev_bbox[0]), float(box[0])), min(float(prev_bbox[1]), float(box[1])),
                    max(float(prev_bbox[2]), float(box[2])), max(float(prev_bbox[3]), float(box[3])),
                ]
                merged[-1]["last_bbox"] = list(box)
                merged[-1]["column"] = int(para.get("column") or 0)
                merged[-1]["page_end"] = int(para.get("page") or merged[-1].get("page") or 0)
                continue
            merged.append(para)
        body_sorted = sorted(body_sizes)
        body_size = body_sorted[len(body_sorted) // 2] if body_sorted else body_size
        heading_sizes = sorted(
            {round(float(p.get("size") or body_size), 1) for p in merged if p.get("kind") == "heading"},
            reverse=True,
        )
        for para in merged:
            if para.get("kind") == "heading":
                text = str(para.get("text") or "")
                size = round(float(para.get("size") or body_size), 1)
                if re.match(r"^\s*\d+(?:\.\d+){2,}\b", text):
                    level = 3
                elif re.match(r"^\s*\d+\.\d+\b", text):
                    level = 2
                elif re.match(r"^\s*(?:\d+|[IVX]+)[.)]?\s+", text, re.I):
                    level = 1
                else:
                    level = min(3, heading_sizes.index(size) + 1) if size in heading_sizes else 3
                para["level"] = level
            para.pop("size", None)
            para.pop("bbox", None)
            para.pop("last_bbox", None)
            para.pop("column", None)
            para.pop("page_width", None)
            para.pop("bold", None)
        chars = sum(len(p["text"]) for p in merged)
        return {
            "paragraphs": merged,
            "page_count": doc.page_count,
            "chars": chars,
            "has_text_layer": chars >= 800,
        }
    finally:
        doc.close()


# ---------------------------------------------------------------- MiMo V2.5 non-thinking calls


class MimoUnavailable(RuntimeError):
    """The site-funded MiMo account is not configured or unavailable."""


class RecognitionUnavailable(RuntimeError):
    """The free GLM recognition channel is not configured or temporarily unavailable."""


class PaddleOCRUnavailable(RuntimeError):
    """The PaddleOCR verification/fallback channel is unavailable."""


# Compatibility name for old operational imports.
ZhipuUnavailable = RecognitionUnavailable


def _record_model_usage(
    article_id: int | None,
    feature: str,
    *,
    success: bool,
    error: str = "",
    provider: str = MIMO_PROVIDER,
    model: str = MIMO_MODEL,
    reasoning_effort: str = MIMO_REASONING_EFFORT,
) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO journal_model_usage(
                    article_id, feature, provider, model, reasoning_effort, success, error, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_id,
                    feature,
                    str(provider or ""),
                    str(model or ""),
                    str(reasoning_effort or "off"),
                    1 if success else 0,
                    str(error or "")[:1200],
                    _now_text(),
                ),
            )
            conn.commit()
    except Exception:
        LOGGER.debug("could not record journal model usage", exc_info=True)


def _mimo_chat(
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.1,
    feature: str,
    article_id: int | None = None,
) -> str:
    key = str(os.environ.get("MIMO_API_KEY") or "").strip()
    if not key:
        raise MimoUnavailable("MIMO_API_KEY 未配置")
    body = json.dumps(
        {
            "model": MIMO_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": int(max_tokens),
            "thinking": {"type": "disabled"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(_TR_MAX_RETRY):
        req = urllib.request.Request(
            f"{MIMO_BASE_URL}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": API_UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=240) as response:
                payload = json.loads(response.read())
            choices = payload.get("choices") or []
            if not choices:
                raise ValueError("MiMo returned no choices")
            text = str((choices[0].get("message") or {}).get("content") or "").strip()
            if not text:
                raise ValueError("MiMo returned empty content")
            _record_model_usage(article_id, feature, success=True)
            return text
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "ignore")
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                break
        except Exception as exc:
            last_error = exc
        if attempt + 1 < _TR_MAX_RETRY:
            time.sleep(2 ** attempt)
    message = str(last_error or "MiMo request failed")
    _record_model_usage(article_id, feature, success=False, error=message)
    raise MimoUnavailable(message)


def _glm_chat(
    messages: list[dict],
    *,
    max_tokens: int,
    feature: str,
    article_id: int | None = None,
    model: str = GLM_TEXT_MODEL,
    temperature: float = 0.0,
) -> str:
    """Call a free GLM-4 recognition model with bounded retry on free-tier congestion."""
    key = str(
        os.environ.get("ZHIPU_API_KEY")
        or os.environ.get("APP_AI_ZHIPU_API_KEY")
        or ""
    ).strip()
    if not key:
        raise RecognitionUnavailable("ZHIPU_API_KEY 未配置")
    requested_tokens = max(1, int(max_tokens))
    # The free legacy vision endpoint currently enforces a 1,024-token output
    # ceiling even though the text Flash model accepts a larger value.
    model_max_tokens = 1024 if str(model).strip().lower() == "glm-4v-flash" else requested_tokens
    body = json.dumps(
        {
            "model": str(model),
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": min(requested_tokens, model_max_tokens),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(
            f"{GLM_BASE_URL}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": API_UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=240) as response:
                payload = json.loads(response.read())
            choices = payload.get("choices") or []
            text = str(
                ((choices[0].get("message") or {}).get("content") if choices else "") or ""
            ).strip()
            if not text:
                raise ValueError("GLM returned empty content")
            _record_model_usage(
                article_id,
                feature,
                success=True,
                provider="zhipu",
                model=model,
                reasoning_effort="off",
            )
            return text
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "ignore")
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                break
        except Exception as exc:
            last_error = exc
        if attempt < 3:
            time.sleep(min(12, 2 ** (attempt + 1)))
    message = str(last_error or "GLM recognition request failed")
    _record_model_usage(
        article_id,
        feature,
        success=False,
        error=message,
        provider="zhipu",
        model=model,
        reasoning_effort="off",
    )
    raise RecognitionUnavailable(message)


def _paddle_response_data(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise PaddleOCRUnavailable("PaddleOCR response is not an object")
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _paddle_json_request(
    url: str,
    token: str,
    *,
    data: bytes | None = None,
    content_type: str = "application/json",
    timeout: int = 120,
) -> dict:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": API_UA}
    if data is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return _paddle_response_data(json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "ignore")
        raise PaddleOCRUnavailable(f"HTTP {exc.code}: {detail}") from exc
    except PaddleOCRUnavailable:
        raise
    except Exception as exc:
        raise PaddleOCRUnavailable(str(exc)) from exc


def _multipart_image_body(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
    boundary = "----MarxJournalOCR" + secrets.token_hex(12)
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            )
        )

    field("model", PADDLEOCR_MODEL)
    field(
        "optionalPayload",
        json.dumps(
            {
                "useLayoutDetection": True,
                "prettifyMarkdown": False,
                "temperature": 0.0,
            },
            separators=(",", ":"),
        ),
    )
    chunks.extend(
        (
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode(),
            image_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _paddle_ocr_image(
    image_bytes: bytes,
    *,
    article_id: int | None,
    page_number: int,
    feature: str = "journal_paddleocr_verify",
) -> str:
    """Run one rendered page through PaddleOCR's official asynchronous API."""
    token = str(os.environ.get("PADDLEOCR_ACCESS_TOKEN") or "").strip()
    if not token:
        raise PaddleOCRUnavailable("PADDLEOCR_ACCESS_TOKEN 未配置")
    body, content_type = _multipart_image_body(image_bytes, f"article-page-{page_number}.png")
    started = time.monotonic()
    try:
        submitted = _paddle_json_request(
            f"{PADDLEOCR_BASE_URL}/api/v2/ocr/jobs",
            token,
            data=body,
            content_type=content_type,
            timeout=180,
        )
        job_id = str(submitted.get("jobId") or submitted.get("id") or "").strip()
        if not job_id:
            raise PaddleOCRUnavailable("PaddleOCR job id missing")
        status: dict = {}
        delay = 3.0
        while time.monotonic() - started < PADDLEOCR_POLL_SECONDS:
            status = _paddle_json_request(
                f"{PADDLEOCR_BASE_URL}/api/v2/ocr/jobs/{urllib.parse.quote(job_id)}",
                token,
                timeout=90,
            )
            state = str(status.get("state") or status.get("status") or "").lower()
            if state == "done":
                break
            if state in {"failed", "error", "cancelled"}:
                raise PaddleOCRUnavailable(
                    str(status.get("errorMsg") or status.get("message") or state)
                )
            time.sleep(delay)
            delay = min(12.0, delay * 1.5)
        else:
            raise PaddleOCRUnavailable("PaddleOCR poll timeout")
        result_ref: Any = status.get("resultJsonUrl") or status.get("resultUrl") or ""
        if isinstance(result_ref, dict):
            result_url = str(result_ref.get("jsonUrl") or result_ref.get("url") or "")
        else:
            result_url = str(result_ref or "")
        _assert_public_http_url(result_url)
        with urllib.request.urlopen(result_url, timeout=120) as response:
            raw = response.read().decode("utf-8", "replace")
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        pages: list[str] = []
        for row in rows:
            result = row.get("result") if isinstance(row, dict) else None
            for item in ((result or {}).get("layoutParsingResults") or []):
                markdown = item.get("markdown") or {}
                text = str(markdown.get("text") or "").strip()
                if text:
                    pages.append(text)
        output = "\n\n".join(pages).strip()
        if not output:
            raise PaddleOCRUnavailable("PaddleOCR returned no markdown text")
        _record_model_usage(
            article_id,
            feature,
            success=True,
            provider="paddleocr",
            model=PADDLEOCR_MODEL,
            reasoning_effort="off",
        )
        return output
    except Exception as exc:
        error = exc if isinstance(exc, PaddleOCRUnavailable) else PaddleOCRUnavailable(str(exc))
        _record_model_usage(
            article_id,
            feature,
            success=False,
            error=str(error),
            provider="paddleocr",
            model=PADDLEOCR_MODEL,
            reasoning_effort="off",
        )
        raise error


def _ocr_similarity(left: str, right: str) -> float:
    def normalized(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    a, b = normalized(left), normalized(right)
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def _markdown_ocr_paragraphs(markdown: str, page_number: int) -> list[dict]:
    """Convert PaddleOCR-VL markdown into the reflow block contract."""
    out: list[dict] = []
    buffer: list[str] = []

    def flush() -> None:
        text = re.sub(r"\s+", " ", " ".join(buffer)).strip()
        buffer.clear()
        if len(text) >= 2:
            out.append({"page": page_number, "kind": "body", "text": text})

    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush()
            out.append(
                {
                    "page": page_number,
                    "kind": "heading",
                    "level": len(heading.group(1)),
                    "text": heading.group(2).strip(),
                }
            )
            continue
        if re.match(r"^!\[[^]]*]\([^)]*\)$", line):
            continue
        buffer.append(re.sub(r"^[-*+]\s+", "", line))
    flush()
    return out


def _paddle_verify_text_layer(
    pdf_path: Path,
    paragraphs: list[dict],
    *,
    article_id: int,
) -> dict:
    """Review one representative born-digital page; this gate never rewrites good PDF text."""
    result = {
        "enabled": bool(PADDLEOCR_VERIFY_ENABLED),
        "model": PADDLEOCR_MODEL,
        "status": "disabled",
        "page": 0,
        "similarity": None,
        "threshold": PADDLEOCR_VERIFY_MIN_SIMILARITY,
    }
    if not PADDLEOCR_VERIFY_ENABLED:
        return result
    candidates: dict[int, str] = {}
    for para in paragraphs:
        page = int(para.get("page") or 0)
        text = str(para.get("text") or "").strip()
        if page > 0 and len(text) >= 2:
            candidates[page] = (candidates.get(page, "") + " " + text).strip()
    eligible = [(page, text) for page, text in candidates.items() if len(text) >= 350]
    if not eligible:
        result["status"] = "no-representative-page"
        return result
    page_number, source_text = max(eligible[: max(1, min(8, len(eligible)))], key=lambda item: len(item[1]))
    result["page"] = page_number
    try:
        import fitz

        doc = fitz.open(pdf_path)
        try:
            pix = doc[page_number - 1].get_pixmap(dpi=150, alpha=False)
            png = pix.tobytes("png")
        finally:
            doc.close()
        paddle_text = _paddle_ocr_image(
            png,
            article_id=article_id,
            page_number=page_number,
        )
        similarity = _ocr_similarity(source_text, paddle_text)
        result["similarity"] = similarity
        result["status"] = "passed" if similarity >= PADDLEOCR_VERIFY_MIN_SIMILARITY else "warning"
        result["paddle_chars"] = len(paddle_text)
        result["source_chars"] = len(source_text)
    except Exception as exc:
        # Paddle is an independent QA/fallback channel. A temporary quota or
        # network failure must not discard a complete, verified PDF text layer.
        result["status"] = "unavailable"
        result["error"] = str(exc)[:240]
    return result


_LANG_NAMES = {
    "en": "英文",
    "es": "西班牙文",
    "pt": "葡萄牙文",
    "pl": "波兰文",
    "fr": "法文",
    "de": "德文",
    "ru": "俄文",
    "it": "意大利文",
}


def _guess_lang(text: str, fallback: str = "en") -> str:
    """粗判源语种，只为给翻译提示词一个准确的语种名（判错也不影响结果）。"""
    sample = text[:4000].lower()
    if not sample.strip():
        return fallback
    scores = {
        "es": len(re.findall(r"\b(el|la|los|las|que|para|desde|según|también)\b", sample)) + sample.count("ñ"),
        "pt": len(re.findall(r"\b(que|não|para|como|dos|das|uma|pelo)\b", sample)) + sample.count("ção"),
        "pl": len(re.findall(r"\b(nie|jest|oraz|które|przez|tego)\b", sample)) + len(re.findall(r"[ąćęłńśźż]", sample)),
        "fr": len(re.findall(r"\b(les|des|une|pour|dans|cette|nous)\b", sample)),
        "de": len(re.findall(r"\b(und|der|die|das|nicht|eine|werden)\b", sample)),
        "en": len(re.findall(r"\b(the|of|and|that|with|from|this)\b", sample)),
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 3 else fallback


def _translate_chunk(texts: list[str], src_lang: str, *, article_id: int | None = None) -> list[str | None]:
    """一次把若干段送译，按 [[序号]] 对齐回来（与文库翻译同一套协议）。"""
    if not texts:
        return []
    src_name = _LANG_NAMES.get(src_lang, "外文")
    joined = "\n\n".join(f"[[{i + 1}]]\n{t}" for i, t in enumerate(texts))
    max_tokens = max(1000, min(8192, int(sum(len(t) for t in texts) * 1.4) + 600))
    messages = [
        {
            "role": "system",
            "content": f"你是严谨的{src_name}学术翻译，精通马克思主义与政治经济学术语。只输出译文，不解释、不评论、不加注。",
        },
        {
            "role": "user",
            "content": (
                f"把下列{src_name}学术论文段落逐段准确译成简体中文，保留专有名词、人名、术语与原意，"
                f"语句通顺、符合学术汉语表达。\n"
                f"每段以 [[序号]] 开头；请按完全相同的 [[序号]] 标记输出对应译文，段数与顺序必须一致，"
                f"不要合并或拆分段落，不要输出原文：\n\n{joined}"
            ),
        },
    ]
    text = _mimo_chat(
        messages,
        max_tokens=max_tokens,
        feature="journal_paragraph_translate",
        article_id=article_id,
    )
    parts = _SEG_MARK.split(text)   # [pre, '1', seg1, '2', seg2, ...]
    by_index: dict[int, str] = {}
    for k in range(1, len(parts) - 1, 2):
        try:
            by_index[int(parts[k])] = parts[k + 1].strip()
        except (ValueError, IndexError):
            continue
    if not by_index and len(texts) == 1:
        # 单段时模型偶尔省略标记，直接采用整段返回
        cleaned = text.strip()
        return [cleaned or None]
    return [by_index.get(i + 1) or None for i in range(len(texts))]


def translate_paragraphs(
    paragraphs: list[dict],
    src_lang: str,
    *,
    max_chars: int = TRANSLATE_MAX_CHARS,
    progress: Callable[[int, int], None] | None = None,
    article_id: int | None = None,
) -> int:
    """给段落逐个补 zh 字段（就地写入）。返回新译段数。

    References remain original-only. Every other complete text block must be
    translated; a budget overflow is an error rather than a partial release.
    """
    todo: list[int] = []
    budget = max_chars
    for index, para in enumerate(paragraphs):
        if para.get("zh"):
            continue
        if para.get("kind") == "reference":
            continue
        text = str(para.get("text") or "")
        if len(text.strip()) < 2:
            continue
        if budget - len(text) < 0:
            raise ValueError("translation-budget-exceeded")
        budget -= len(text)
        todo.append(index)
    done = 0
    chunk: list[int] = []
    chunk_chars = 0

    def flush() -> None:
        nonlocal chunk, chunk_chars, done
        if not chunk:
            return
        texts = [str(paragraphs[i]["text"]) for i in chunk]
        try:
            outs = _translate_chunk(texts, src_lang, article_id=article_id)
        except MimoUnavailable:
            raise
        except Exception as exc:
            LOGGER.warning("journal fulltext translate chunk failed: %s", exc)
            outs = [None] * len(texts)
        for i, out in zip(chunk, outs):
            if out:
                paragraphs[i]["zh"] = out
                done += 1
        chunk = []
        chunk_chars = 0
        if progress:
            progress(done, len(todo))

    for index in todo:
        text_len = len(str(paragraphs[index]["text"]))
        if chunk and chunk_chars + text_len > _TR_CHUNK_CHARS:
            flush()
        chunk.append(index)
        chunk_chars += text_len
    flush()
    # A long multi-segment response occasionally omits one marker even though
    # the surrounding translations are sound. Retry only those missing blocks
    # one-by-one; single-block responses also tolerate a missing [[1]] marker.
    for _attempt in range(2):
        missing = [index for index in todo if not str(paragraphs[index].get("zh") or "").strip()]
        if not missing:
            break
        for index in missing:
            try:
                outs = _translate_chunk(
                    [str(paragraphs[index]["text"])], src_lang, article_id=article_id
                )
            except MimoUnavailable:
                raise
            except Exception as exc:
                LOGGER.warning("journal fulltext single-paragraph retry failed: %s", exc)
                continue
            if outs and outs[0]:
                paragraphs[index]["zh"] = outs[0]
                done += 1
        if progress:
            progress(done, len(todo))
    return done


# ---------------------------------------------------------------- OCR 兜底


_OCR_PROMPT = (
    "This is one scanned page of an English academic article. Transcribe only text visibly printed on "
    "the page; never infer missing words. Omit running headers, footers and page numbers. Output one "
    "block per line using exactly [[heading:1]], [[heading:2]], [[heading:3]], [[body]], [[footnote]], "
    "or [[caption]] followed by the original English text. Preserve complete paragraph boundaries."
)


def ocr_pdf_pages(
    pdf_path: Path,
    *,
    max_pages: int = OCR_MAX_PAGES,
    article_id: int | None = None,
    quality_out: dict | None = None,
) -> tuple[list[dict], int]:
    """Transcribe every scan page with free GLM-4V; PaddleOCR verifies/falls back."""
    import base64

    import fitz

    doc = fitz.open(pdf_path)
    paragraphs: list[dict] = []
    pages_done = 0
    quality = quality_out if quality_out is not None else {}
    quality.update(
        {
            "primary": {"provider": "zhipu", "model": GLM_VISION_MODEL},
            "reviewer": {"provider": "paddleocr", "model": PADDLEOCR_MODEL},
            "paddle_verified_pages": 0,
            "paddle_fallback_pages": 0,
            "similarities": [],
        }
    )
    try:
        if doc.page_count > max_pages:
            raise ValueError(f"ocr-page-limit:{doc.page_count}>{max_pages}")
        limit = doc.page_count
        for index in range(limit):
            page = doc[index]
            pix = page.get_pixmap(dpi=160)
            payload = base64.b64encode(pix.tobytes("png")).decode("ascii")
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
                        {"type": "text", "text": _OCR_PROMPT},
                    ],
                }
            ]
            glm_error: Exception | None = None
            try:
                text = _glm_chat(
                    messages,
                    max_tokens=8192,
                    feature="journal_page_ocr",
                    article_id=article_id,
                    model=GLM_VISION_MODEL,
                )
            except Exception as exc:
                glm_error = exc
                text = ""
                LOGGER.warning("journal GLM OCR page %s failed: %s", index + 1, exc)
            glm_blocks: list[dict] = []
            matched = 0
            for line in str(text).splitlines():
                match = re.match(r"^\[\[(heading(?::[123])?|body|footnote|caption)\]\]\s*(.+)$", line.strip(), re.I)
                if not match:
                    continue
                marker, cleaned = match.group(1).lower(), match.group(2).strip()
                if len(cleaned) < 2 or _PAGE_NUMBER_RE.match(cleaned):
                    continue
                kind = "heading" if marker.startswith("heading") else marker
                block = {"page": index + 1, "kind": kind, "text": cleaned}
                if kind == "heading":
                    block["level"] = int(marker.split(":", 1)[1]) if ":" in marker else 2
                glm_blocks.append(block)
                matched += 1
            paddle_blocks: list[dict] = []
            paddle_error: Exception | None = None
            try:
                paddle_text = _paddle_ocr_image(
                    pix.tobytes("png"),
                    article_id=article_id,
                    page_number=index + 1,
                    feature="journal_paddleocr_scan_review",
                )
                paddle_blocks = _markdown_ocr_paragraphs(paddle_text, index + 1)
                quality["paddle_verified_pages"] += 1
                if glm_blocks:
                    glm_text = " ".join(str(item.get("text") or "") for item in glm_blocks)
                    similarity = _ocr_similarity(glm_text, paddle_text)
                    quality["similarities"].append(similarity)
            except Exception as exc:
                paddle_error = exc
                LOGGER.warning("journal PaddleOCR page %s failed: %s", index + 1, exc)

            selected = glm_blocks
            if not selected and paddle_blocks:
                selected = paddle_blocks
                quality["paddle_fallback_pages"] += 1
            elif selected and paddle_blocks:
                glm_chars = sum(len(str(item.get("text") or "")) for item in selected)
                paddle_chars = sum(len(str(item.get("text") or "")) for item in paddle_blocks)
                similarity = quality["similarities"][-1] if quality["similarities"] else 1.0
                if similarity < 0.45 and paddle_chars > glm_chars * 1.12:
                    selected = paddle_blocks
                    quality["paddle_fallback_pages"] += 1
            if not selected:
                raise ValueError(
                    f"ocr-page-failed:{index + 1}:glm={glm_error};paddle={paddle_error}"
                )
            paragraphs.extend(selected)
            pages_done += 1
        similarities = list(quality.get("similarities") or [])
        quality["average_similarity"] = (
            round(sum(similarities) / len(similarities), 4) if similarities else None
        )
        return paragraphs, pages_done
    finally:
        doc.close()


# ---------------------------------------------------------------- metadata, classification and completeness


def recognize_document_structure(paragraphs: list[dict], *, article_id: int) -> str:
    """Use free GLM-4 to label blocks; retain deterministic PDF labels if it is busy."""
    allowed = {"heading", "body", "footnote", "caption", "reference"}
    used_fallback = False
    # Layout-locked notes are based on smaller type in a numbered page-bottom
    # band. Do not let a model that sees only the first 900 characters relabel
    # them as body text. Remove the private extraction marker before saving.
    pending: list[int] = []
    for index, paragraph in enumerate(paragraphs):
        if paragraph.pop("_layout_role_locked", False):
            paragraph["kind"] = "footnote"
            paragraph.pop("level", None)
        else:
            pending.append(index)
    while pending:
        chunk: list[int] = []
        chars = 0
        while pending and len(chunk) < 30:
            index = pending[0]
            sample = str(paragraphs[index].get("text") or "")[:900]
            if chunk and chars + len(sample) > 12000:
                break
            pending.pop(0)
            chunk.append(index)
            chars += len(sample)
        blocks = [
            {
                "id": index,
                "page": paragraphs[index].get("page"),
                "current_kind": paragraphs[index].get("kind"),
                "text": str(paragraphs[index].get("text") or "")[:900],
            }
            for index in chunk
        ]
        try:
            response = _glm_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Classify the layout role of every supplied English academic PDF block. "
                            "Do not rewrite or translate text. Return JSON only and include every id exactly once."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Allowed kind values are heading, body, footnote, caption, and reference. "
                            "Return an object shaped like {\"blocks\":[{\"id\":0,\"kind\":\"body\","
                            "\"level\":null}]}. Never join the allowed values with punctuation. "
                            "Heading level must be 1, 2, or 3; use null for every non-heading block.\n"
                            + json.dumps(blocks, ensure_ascii=False)
                        ),
                    },
                ],
                max_tokens=5000,
                feature="journal_structure_recognition",
                article_id=article_id,
                model=GLM_TEXT_MODEL,
            )
            parsed = _json_object(response)
            labels = parsed.get("blocks")
            if not isinstance(labels, list):
                raise ValueError("structure-blocks-missing")
            by_id = {
                int(item.get("id")): item
                for item in labels
                if isinstance(item, dict) and str(item.get("id", "")).isdigit()
            }
            if set(by_id) != set(chunk):
                raise ValueError("structure-id-coverage-mismatch")
            for index in chunk:
                kind = str(by_id[index].get("kind") or "").strip().lower()
                if kind not in allowed:
                    raise ValueError(f"structure-kind-invalid:{kind}")
        except Exception as exc:
            LOGGER.warning(
                "journal GLM structure recognition invalid/unavailable; retaining PDF layout labels: %s",
                exc,
            )
            used_fallback = True
            for index in chunk:
                kind = str(paragraphs[index].get("kind") or "body").lower()
                if kind not in allowed:
                    paragraphs[index]["kind"] = "body"
            continue
        for index in chunk:
            item = by_id[index]
            kind = str(item.get("kind") or "").strip().lower()
            paragraphs[index]["kind"] = kind
            if kind == "heading":
                level = int(item.get("level") or 2)
                paragraphs[index]["level"] = max(1, min(3, level))
            else:
                paragraphs[index].pop("level", None)
    return (
        f"{GLM_TEXT_MODEL}+deterministic-fallback"
        if used_fallback else GLM_TEXT_MODEL
    )


def _json_object(text: str) -> dict:
    raw = str(text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("metadata-json-missing")
    data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("metadata-json-invalid")
    return data


def _abstract_from_paragraphs(paragraphs: list[dict]) -> str:
    collecting = False
    pieces: list[str] = []
    for para in paragraphs:
        text = str(para.get("text") or "").strip()
        kind = str(para.get("kind") or "")
        if kind == "heading" and re.match(r"^abstract\b", text, re.I):
            collecting = True
            inline = re.sub(r"^abstract\s*[:.—-]?\s*", "", text, flags=re.I).strip()
            if inline:
                pieces.append(inline)
            continue
        if collecting and kind == "heading":
            break
        if collecting and kind in {"body", "footnote"} and text:
            pieces.append(text)
            if sum(len(piece) for piece in pieces) >= 3000:
                break
    return " ".join(pieces).strip()


_ABSTRACT_HEADING_RE = re.compile(r"^\s*abstract\b", re.I)
_KEYWORDS_HEADING_RE = re.compile(r"^\s*(?:key\s*words?|keywords?|index\s+terms?)\b", re.I)
_EMBEDDED_KEYWORDS_RE = re.compile(
    r"\b(?:key\s*words?|keywords?|index\s+terms?)\s*[:：]\s*",
    re.I,
)
_BODY_START_HEADING_RE = re.compile(
    r"^\s*(?:(?:section|part)\s+)?"
    r"(?:1(?:\.0)?(?:[.)]|\s*[|｜]\s*)?\s+|I(?:[.)]|\s*[|｜]\s*)?\s+)?"
    r"(?:introduction|introductory remarks?|preface|prelude|prologue|background|"
    r"theoretical framework|methodology|methods?|main text)\b",
    re.I,
)
_NUMBERED_SECTION_RE = re.compile(r"^\s*(?:1|I)(?:[.)]|\s*[|｜]\s*)\s+[A-Z]", re.I)
_FRONTMATTER_NOISE_RE = re.compile(
    r"(?:\bORCID\b|\b(?:received|revised|accepted)\s*[:：]?\s+\d|"
    r"\bpublished\s+(?:online|with\s+license)|"
    r"\bcorresponding author\b|\baffiliation\b|\bISSN\b|\be-?mail\b|@|"
    r"creativecommons\.org|creative commons|open access article|all rights reserved|"
    r"©|\bcopyright\b|\bdoi\.org/|\bdoi\s*:\s*10\.)",
    re.I,
)
_RUNNING_AUTHOR_RE = re.compile(r"^\s*(?:\d{1,4}\s+[A-Z][A-Za-z'’.-]+|[A-Z][A-Za-z'’.-]+\s+\d{1,4})\s*$")
_AUTHOR_BIO_RE = re.compile(
    r"^\s*[A-Z][A-Za-zÀ-ɏ'’.-]+(?:\s+[A-Z][A-Za-zÀ-ɏ'’.-]+){0,4}\s+"
    r"is\s+(?:an?\s+)?(?:Emeritus\s+|Associate\s+|Assistant\s+|Adjunct\s+)?"
    r"(?:Professor|Lecturer|Reader|research scholar|poet|critic|editor|fellow)\b",
    re.I,
)
_PUBLISHER_FOOTER_RE = re.compile(
    r"(?:downloaded\s+from\s+https?://|see\s+the\s+terms\s+and\s+conditions|"
    r"oa\s+articles?\s+are\s+governed\s+by)",
    re.I,
)
_ACKNOWLEDGMENT_NOTE_RE = re.compile(
    r"(?:^earlier\s+drafts?\s+(?:benefited|were)|\bi\s+am\s+grateful\b|"
    r"\bwe\s+(?:are\s+)?grateful\b|\bthanks\s+(?:also\s+)?to\b)",
    re.I,
)


def _is_frontmatter_noise(para: dict) -> bool:
    text = re.sub(r"\s+", " ", str(para.get("text") or "")).strip()
    if not text:
        return True
    page = int(para.get("page") or 1)
    if _PUBLISHER_FOOTER_RE.search(text):
        return True
    if page <= 3 and (
        _ACKNOWLEDGMENT_NOTE_RE.search(text)
        or re.search(r"creative commons|open access article|all rights reserved|©|\bcopyright\b", text, re.I)
    ):
        return True
    if str(para.get("kind") or "") in {"footnote", "reference"}:
        return False
    return bool(
        _FRONTMATTER_NOISE_RE.search(text)
        or _RUNNING_AUTHOR_RE.match(text)
        or _RUNNING_PUBLICATION_RE.match(text)
        or (len(text) <= 800 and _AUTHOR_BIO_RE.match(text))
    )


def _is_abstract_frontmatter(para: dict) -> bool:
    """Distinguish the abstract label from prose such as "abstract principles"."""
    if int(para.get("page") or 1) > 3:
        return False
    text = str(para.get("text") or "").strip()
    if not _ABSTRACT_HEADING_RE.match(text):
        return False
    return bool(
        str(para.get("kind") or "") == "heading"
        or re.match(r"^\s*ABSTRACT\b", text)
        or re.match(r"^\s*Abstract\s*[:.—-]", text)
    )


def _keyword_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        text = re.sub(
            r"^\s*(?:key\s*words?|keywords?|index\s+terms?)\s*[:.—-]?\s*",
            "", str(value or ""), flags=re.I,
        )
        raw = re.split(r"\s*[;•|]\s*|\s+[–—]\s+|\s*,\s*|[\r\n]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        cleaned = re.sub(r"\s+", " ", str(item)).strip(" .;,:，；、–—-")
        key = cleaned.casefold()
        if cleaned and key not in seen and len(cleaned) <= 120:
            seen.add(key)
            out.append(cleaned)
    return out[:20]


def _stored_keywords(article: dict) -> list[str]:
    metadata = article.get("metadata") or {}
    for value in (metadata.get("keywords_en"), metadata.get("keywords"), article.get("keywords")):
        keywords = _keyword_list(value)
        if keywords:
            return keywords
    return []


def normalize_reflow_content(paragraphs: list[dict], *, article: dict | None = None) -> dict:
    """Promote abstract/keywords to the header and return only readable article content.

    The body begins at Introduction, or at the first substantial prose block when
    a journal has no named introduction. Notes, references, appendices and
    supplementary information after that point are deliberately retained.
    """
    article = article or {}
    items = [dict(para) for para in paragraphs if str(para.get("text") or "").strip()]
    keywords = _stored_keywords(article)
    front_end = 0
    metadata_labels = [str(article.get("title") or "").strip(), *(str(name).strip() for name in article.get("authors") or [])]
    metadata_labels = [re.sub(r"\W+", "", label).casefold() for label in metadata_labels if label]

    for index, para in enumerate(items):
        text = str(para.get("text") or "").strip()
        if _is_abstract_frontmatter(para):
            front_end = max(front_end, index + 1)
            cursor = index + 1
            while cursor < len(items):
                candidate = str(items[cursor].get("text") or "").strip()
                if _KEYWORDS_HEADING_RE.match(candidate) or _BODY_START_HEADING_RE.match(candidate):
                    break
                if str(items[cursor].get("kind") or "") == "heading" and cursor > index + 1:
                    break
                front_end = cursor + 1
                cursor += 1

        keyword_label = _KEYWORDS_HEADING_RE.match(text)
        if (
            keyword_label is None
            and int(para.get("page") or 1) <= 3
            and _is_frontmatter_noise(para)
        ):
            # Wiley and other two-column layouts sometimes merge the dates and
            # keyword line into one PDF block ("Accepted: ... | Keywords: ...").
            # Only scan recognized front-matter noise so prose mentioning the
            # word "keywords" later in the article cannot become metadata.
            keyword_label = _EMBEDDED_KEYWORDS_RE.search(text)
        if keyword_label is not None:
            inline = text[keyword_label.end() :].strip()
            extracted_keywords = _keyword_list(inline)
            cursor = index + 1
            captured = 0
            while cursor < len(items) and captured < 2:
                candidate = str(items[cursor].get("text") or "").strip()
                if (
                    _is_abstract_frontmatter(items[cursor])
                    or _BODY_START_HEADING_RE.match(candidate)
                    or _NUMBERED_SECTION_RE.match(candidate)
                ):
                    break
                if _is_frontmatter_noise(items[cursor]):
                    break
                parsed = _keyword_list(candidate)
                if not parsed or len(candidate) > 600:
                    break
                extracted_keywords.extend(parsed)
                front_end = cursor + 1
                captured += 1
                cursor += 1
            if extracted_keywords:
                keywords = _keyword_list(extracted_keywords)
            front_end = max(front_end, index + 1)

    start: int | None = None
    for index in range(front_end, len(items)):
        para = items[index]
        text = str(items[index].get("text") or "").strip()
        if (
            str(para.get("kind") or "") == "heading"
            and int(para.get("page") or 1) <= 3
            and (_BODY_START_HEADING_RE.match(text) or _NUMBERED_SECTION_RE.match(text))
        ):
            start = index
            break
    if start is None:
        for index in range(front_end, len(items)):
            para = items[index]
            text = str(para.get("text") or "").strip()
            compact = re.sub(r"\W+", "", text).casefold()
            if (
                str(para.get("kind") or "") in {"body", "footnote"}
                and len(text) >= 20
                and compact not in metadata_labels
                and not _is_frontmatter_noise(para)
            ):
                start = index
                break
    if start is None:
        start = min(front_end, len(items))

    readable: list[dict] = []
    for para in items[start:]:
        text = str(para.get("text") or "").strip()
        if _is_abstract_frontmatter(para) or _KEYWORDS_HEADING_RE.match(text):
            continue
        if _is_frontmatter_noise(para):
            continue
        readable.append(para)
    # Publisher footers and licence bands can sit between two physical pieces
    # of one paragraph.  Once those bands are removed, stitch only an obvious
    # lowercase continuation across the same/next page.
    stitched: list[dict] = []
    for para in readable:
        current = dict(para)
        if stitched and current.get("kind") == stitched[-1].get("kind") == "body":
            previous = stitched[-1]
            previous_text = str(previous.get("text") or "").strip()
            current_text = str(current.get("text") or "").strip()
            previous_page = int(previous.get("page_end") or previous.get("page") or 0)
            current_page = int(current.get("page") or 0)
            terminal = bool(re.search(r"[.。!?？！:：”\"')\]]$", previous_text))
            continuation = bool(re.match(r"^[a-zá-úñçāăĉ(\d]", current_text))
            if current_page <= previous_page + 1 and not terminal and continuation:
                if re.search(r"[A-Za-z][-‐‑]$", previous_text) and re.match(r"^[A-Za-z]", current_text):
                    previous["text"] = previous_text[:-1] + current_text
                else:
                    previous["text"] = f"{previous_text} {current_text}".strip()
                previous["page_end"] = int(current.get("page_end") or current_page or previous_page)
                continue
        stitched.append(current)
    return {"paragraphs": stitched, "keywords_en": keywords, "body_start": start}


def _generate_editorial_abstract(paragraphs: list[dict], *, article_id: int) -> str:
    """Create a transparent editorial abstract only when the source paper has none."""
    prose = [
        str(para.get("text") or "").strip()
        for para in paragraphs
        if para.get("kind") not in {"reference", "footnote"} and str(para.get("text") or "").strip()
    ]
    source = "\n\n".join(prose)
    if len(source) > 14000:
        source = source[:10500] + "\n\n[... middle omitted ...]\n\n" + source[-3500:]
    response = _mimo_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are a scholarly journal editor. The source article has no author-supplied abstract. "
                    "Write a neutral 120–180 word English editorial abstract based only on the supplied text. "
                    "State the paper's object, argument and method without inventing evidence or claims. Return JSON only."
                ),
            },
            {"role": "user", "content": "Return {\"abstract_en\":\"\"}.\n\n" + source},
        ],
        max_tokens=1200,
        temperature=0.0,
        feature="journal_editorial_abstract",
        article_id=article_id,
    )
    abstract = str(_json_object(response).get("abstract_en") or "").strip()
    if len(abstract) < 120:
        raise ValueError("editorial-abstract-incomplete")
    return abstract


def _translate_article_metadata(
    article: dict, abstract_en: str, keywords_en: list[str] | None = None,
) -> dict:
    from journal_taxonomy import DISCIPLINES, DISCIPLINE_HINTS, is_valid_discipline

    payload = {
        "title_en": str(article.get("title") or "").strip(),
        "journal_en": str(article.get("journal_name") or "").strip(),
        "authors_en": list(article.get("authors") or []),
        "abstract_en": abstract_en,
        "keywords_en": list(keywords_en or []),
        "disciplines": [
            {"name": name, "definition": DISCIPLINE_HINTS[name]} for name in DISCIPLINES
        ],
    }
    response = _mimo_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是英文哲学与马克思主义研究期刊的严谨编译。只能依据输入翻译，"
                    "不得改写摘要、补充事实或创造作者中文名。作者没有公认中文名时使用规范音译。"
                    "学科必须从给定名称中选择一个。只返回 JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "返回且仅返回：{\"title_zh\":\"\",\"journal_name_zh\":\"\","
                    "\"authors_zh\":[\"\"],\"abstract_zh\":\"\",\"keywords_zh\":[\"\"],"
                    "\"discipline\":\"\"}。keywords_zh 必须与 keywords_en 等长、逐项对应；"
                    "若输入为空则返回空数组。\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ],
        max_tokens=6000,
        temperature=0.0,
        feature="journal_metadata_translate_classify",
        article_id=int(article["id"]),
    )
    parsed = _json_object(response)
    translated = {
        "title_zh": str(parsed.get("title_zh") or "").strip(),
        "journal_name_zh": str(parsed.get("journal_name_zh") or "").strip(),
        "authors_zh": [str(item).strip() for item in (parsed.get("authors_zh") or []) if str(item).strip()],
        "abstract_zh": str(parsed.get("abstract_zh") or "").strip(),
        "keywords_zh": [str(item).strip() for item in (parsed.get("keywords_zh") or []) if str(item).strip()],
        "discipline": str(parsed.get("discipline") or "").strip(),
    }
    if not all((translated["title_zh"], translated["journal_name_zh"], translated["abstract_zh"])):
        raise ValueError("metadata-translation-incomplete")
    if len(translated["authors_zh"]) != len(list(article.get("authors") or [])):
        raise ValueError("author-translation-count-mismatch")
    expected_keywords = list(keywords_en or [])
    if not expected_keywords:
        translated["keywords_zh"] = []
    elif len(translated["keywords_zh"]) != len(expected_keywords):
        # Repair only the malformed array instead of discarding an otherwise
        # complete 20-page translation. The numbered segment protocol enforces
        # one Chinese value per original keyword and retains the strict count gate.
        repaired = _translate_chunk(
            [str(item) for item in expected_keywords], "en", article_id=int(article["id"])
        )
        if len(repaired) == len(expected_keywords) and all(str(item or "").strip() for item in repaired):
            translated["keywords_zh"] = [str(item).strip() for item in repaired]
        else:
            raise ValueError("keyword-translation-count-mismatch")
    if not is_valid_discipline(translated["discipline"]):
        raise ValueError("discipline-invalid")
    return translated


def _translation_requirements(paragraphs: list[dict]) -> tuple[int, int]:
    required = [
        para for para in paragraphs
        if para.get("kind") != "reference" and len(str(para.get("text") or "").strip()) >= 2
    ]
    complete = [para for para in required if str(para.get("zh") or "").strip()]
    return len(required), len(complete)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- 编排


def _mark_article_fulltext_unavailable(article_id: int) -> None:
    """Immediately withdraw a rejected/incomplete source from every public gate."""
    with _connect() as conn:
        conn.execute(
            "UPDATE journal_articles SET public_pdf_verified = 0, status = 'fulltext_unavailable', "
            "updated_at = ? WHERE id = ?",
            (_now_text(), int(article_id)),
        )
        conn.commit()


def build_article_fulltext(article: dict, *, translate: bool = True, allow_ocr: bool = True) -> dict:
    """Build a publication-gated bilingual reflow document for one article."""
    init_fulltext_db()
    article_id = int(article["id"])
    batch_id = article.get("batch_id")
    _upsert_state(
        article_id,
        batch_id=batch_id,
        status="processing",
        provider=MIMO_PROVIDER,
        model=MIMO_MODEL,
        reasoning_effort=MIMO_REASONING_EFFORT,
        error="",
    )

    pdf_path, pdf_url, host_type, reason = fetch_public_pdf(article, article_id)
    if pdf_path is None:
        _upsert_state(article_id, batch_id=batch_id, status="unavailable", error=reason[:600])
        _mark_article_fulltext_unavailable(article_id)
        return get_fulltext_state(article_id) or {}

    pdf_sha256 = _sha256_file(pdf_path)
    provenance = {
        "policy": "public-downloadable",
        "source_url": pdf_url,
        "host_type": host_type,
        "verified_at": _now_text(),
        "sha256": pdf_sha256,
        "doi": str(article.get("doi") or ""),
        "license": str((article.get("metadata") or {}).get("oa_license") or ""),
        "version": str((article.get("metadata") or {}).get("oa_version") or ""),
        "no_authentication_bypass": True,
    }
    _save_provenance(article_id, provenance)

    try:
        extracted = extract_paragraphs(pdf_path)
    except Exception as exc:
        _upsert_state(
            article_id, batch_id=batch_id, status="failed",
            pdf_url=pdf_url, pdf_host_type=host_type, pdf_sha256=pdf_sha256,
            provenance_json=json.dumps(provenance, ensure_ascii=False), error=f"extract: {exc}"[:600],
        )
        return get_fulltext_state(article_id) or {}

    completeness_error = validate_pdf_completeness(article, extracted, pdf_url)
    if completeness_error:
        _quarantine_rejected_pdf(pdf_path, completeness_error.split(":", 1)[0])
        _upsert_state(
            article_id, batch_id=batch_id, status="unavailable",
            pdf_url=pdf_url, pdf_host_type=host_type, pdf_sha256=pdf_sha256,
            pdf_bytes=pdf_path.stat().st_size, page_count=extracted["page_count"],
            provenance_json=json.dumps(provenance, ensure_ascii=False),
            error=completeness_error[:600],
        )
        _mark_article_fulltext_unavailable(article_id)
        return get_fulltext_state(article_id) or {}

    paragraphs = extracted["paragraphs"]
    ocr_pages = 0
    recognition_info: dict = {
        "text_extractor": "pymupdf",
        "structure_provider": "zhipu",
        "structure_model": GLM_TEXT_MODEL,
        "vision_provider": "zhipu",
        "vision_model": GLM_VISION_MODEL,
        "paddle_provider": "paddleocr",
        "paddle_model": PADDLEOCR_MODEL,
        "paddle_role": "independent-review-and-fallback",
    }
    if not extracted["has_text_layer"] and allow_ocr:
        try:
            scan_quality: dict = {}
            ocr_paragraphs, ocr_pages = ocr_pdf_pages(
                pdf_path,
                article_id=article_id,
                quality_out=scan_quality,
            )
            if ocr_paragraphs:
                paragraphs = ocr_paragraphs
            recognition_info["scan_ocr"] = scan_quality
        except Exception as exc:
            _upsert_state(
                article_id, batch_id=batch_id, status="failed",
                pdf_url=pdf_url, pdf_host_type=host_type, pdf_sha256=pdf_sha256,
                provenance_json=json.dumps(provenance, ensure_ascii=False), error=f"ocr: {exc}"[:600],
            )
            return get_fulltext_state(article_id) or {}

    if not extracted["has_text_layer"] and not allow_ocr:
        paragraphs = []

    if not paragraphs:
        _upsert_state(
            article_id, batch_id=batch_id, status="failed",
            pdf_url=pdf_url, pdf_host_type=host_type, pdf_sha256=pdf_sha256,
            provenance_json=json.dumps(provenance, ensure_ascii=False), error="no-text-extracted",
        )
        return get_fulltext_state(article_id) or {}

    if extracted["has_text_layer"]:
        recognition_info["text_layer_review"] = _paddle_verify_text_layer(
            pdf_path,
            paragraphs,
            article_id=article_id,
        )

    try:
        recognition_info["structure_model"] = recognize_document_structure(
            paragraphs, article_id=article_id
        )
    except Exception as exc:
        _upsert_state(
            article_id, batch_id=batch_id, status="failed", pdf_url=pdf_url,
            pdf_host_type=host_type, pdf_sha256=pdf_sha256,
            provenance_json=json.dumps(provenance, ensure_ascii=False),
            error=f"structure: {exc}"[:600],
        )
        return get_fulltext_state(article_id) or {}

    provenance["recognition"] = recognition_info
    _save_provenance(article_id, provenance)

    abstract_en = str(article.get("abstract") or "").strip() or _abstract_from_paragraphs(paragraphs)
    abstract_source = "original"
    normalized = normalize_reflow_content(paragraphs, article=article)
    paragraphs = normalized["paragraphs"]
    keywords_en = normalized["keywords_en"]
    if not paragraphs:
        _upsert_state(
            article_id, batch_id=batch_id, status="failed", pdf_url=pdf_url,
            pdf_host_type=host_type, pdf_sha256=pdf_sha256,
            provenance_json=json.dumps(provenance, ensure_ascii=False), error="body-missing-after-frontmatter-cleanup",
        )
        return get_fulltext_state(article_id) or {}

    body_text = " ".join(p["text"] for p in paragraphs if p.get("kind") == "body")[:6000]
    src_lang = _guess_lang(body_text or paragraphs[0]["text"])
    if src_lang != "en":
        _upsert_state(
            article_id, batch_id=batch_id, status="failed", pdf_url=pdf_url,
            pdf_host_type=host_type, pdf_sha256=pdf_sha256,
            provenance_json=json.dumps(provenance, ensure_ascii=False), error=f"not-english:{src_lang}",
        )
        return get_fulltext_state(article_id) or {}

    if not abstract_en:
        try:
            abstract_en = _generate_editorial_abstract(paragraphs, article_id=article_id)
            abstract_source = "ai_editorial_summary"
        except Exception as exc:
            _upsert_state(
                article_id, batch_id=batch_id, status="failed", pdf_url=pdf_url,
                pdf_host_type=host_type, pdf_sha256=pdf_sha256,
                provenance_json=json.dumps(provenance, ensure_ascii=False),
                error=f"abstract: {exc}"[:600],
            )
            return get_fulltext_state(article_id) or {}

    if not translate:
        _upsert_state(
            article_id, batch_id=batch_id, status="failed", pdf_url=pdf_url,
            pdf_host_type=host_type, pdf_sha256=pdf_sha256,
            provenance_json=json.dumps(provenance, ensure_ascii=False), error="translation-required",
        )
        return get_fulltext_state(article_id) or {}

    try:
        translated = translate_paragraphs(
            paragraphs, src_lang, article_id=article_id
        )
        required, completed = _translation_requirements(paragraphs)
        if required == 0 or completed != required:
            raise ValueError(f"translation-incomplete:{completed}/{required}")
        metadata_zh = _translate_article_metadata(article, abstract_en, keywords_en)
        from journal_alerts import update_article_bilingual_metadata

        refreshed = update_article_bilingual_metadata(
            article_id,
            title_zh=metadata_zh["title_zh"],
            journal_name_zh=metadata_zh["journal_name_zh"],
            authors_zh=metadata_zh["authors_zh"],
            abstract_zh=metadata_zh["abstract_zh"],
            discipline=metadata_zh["discipline"],
            abstract_en=abstract_en,
            keywords_en=keywords_en,
            keywords_zh=metadata_zh["keywords_zh"],
            abstract_source=abstract_source,
        )
        if not refreshed:
            raise ValueError("article-metadata-row-missing")
        required_citations = (
            refreshed.get("citation_gb2015"), refreshed.get("citation_mks_en"),
        )
        if not all(str(value or "").strip() for value in required_citations):
            raise ValueError("citation-incomplete")
    except Exception as exc:
        _upsert_state(
            article_id, batch_id=batch_id, status="failed", pdf_url=pdf_url,
            pdf_host_type=host_type, pdf_sha256=pdf_sha256,
            pdf_bytes=pdf_path.stat().st_size, page_count=extracted["page_count"],
            para_count=len(paragraphs), translated=sum(1 for p in paragraphs if p.get("zh")),
            required_translations=_translation_requirements(paragraphs)[0], src_lang=src_lang,
            ocr_pages=ocr_pages, provider=MIMO_PROVIDER, model=MIMO_MODEL,
            reasoning_effort=MIMO_REASONING_EFFORT,
            provenance_json=json.dumps(provenance, ensure_ascii=False), error=f"translate: {exc}"[:600],
        )
        return get_fulltext_state(article_id) or {}

    doc = {
        "schema_version": 4,
        "article_id": article_id,
        "issue_id": batch_id,
        "metadata": {
            "title_en": refreshed.get("title") or "",
            "title_zh": refreshed.get("title_zh") or "",
            "journal_en": refreshed.get("journal_name") or "",
            "journal_zh": refreshed.get("journal_name_zh") or "",
            "authors_en": refreshed.get("authors") or [],
            "authors_zh": refreshed.get("authors_zh") or [],
            "abstract_en": refreshed.get("abstract") or "",
            "abstract_zh": refreshed.get("abstract_zh") or "",
            "abstract_source": abstract_source,
            "keywords_en": keywords_en,
            "keywords_zh": metadata_zh["keywords_zh"],
            "discipline": refreshed.get("ai_discipline") or "",
            "published_at": refreshed.get("published_at") or "",
            "citations": {
                "gb2015_en": refreshed.get("citation_gb2015") or "",
                "mks_en": refreshed.get("citation_mks_en") or "",
            },
        },
        "url": article.get("url") or "",
        "pdf_url": pdf_url,
        "provenance": provenance,
        "recognition": recognition_info,
        "src_lang": src_lang,
        "page_count": extracted["page_count"],
        "ocr_pages": ocr_pages,
        "provider": MIMO_PROVIDER,
        "model": MIMO_MODEL,
        "reasoning_effort": MIMO_REASONING_EFFORT,
        "generated_at": _now_text(),
        "paragraphs": paragraphs,
    }
    _save_document(article_id, doc)
    _upsert_state(
        article_id,
        batch_id=batch_id,
        status="ready",
        pdf_url=pdf_url,
        pdf_host_type=host_type,
        pdf_bytes=pdf_path.stat().st_size,
        pdf_sha256=pdf_sha256,
        page_count=extracted["page_count"],
        para_count=len(paragraphs),
        translated=translated,
        required_translations=required,
        src_lang=src_lang,
        ocr_pages=ocr_pages,
        provider=MIMO_PROVIDER,
        model=MIMO_MODEL,
        reasoning_effort=MIMO_REASONING_EFFORT,
        provenance_json=json.dumps(provenance, ensure_ascii=False),
        error="",
    )
    with _connect() as conn:
        conn.execute(
            "UPDATE journal_articles SET public_pdf_verified = 1, status = 'ready', updated_at = ? WHERE id = ?",
            (_now_text(), article_id),
        )
        conn.commit()
    return get_fulltext_state(article_id) or {}


def process_batch_fulltext(
    digest_id: int,
    *,
    limit: int = ARTICLES_PER_RUN,
    retry_unavailable: bool = False,
    translate: bool = True,
) -> dict:
    """Build complete bilingual documents for one draft issue."""
    from journal_alerts import batch_articles, get_batch

    init_fulltext_db()
    digest = get_batch(int(digest_id)) or {}
    if str(digest.get("status") or "") in {"published", "sent", "archived"}:
        raise RuntimeError("approved/sent journal issues are immutable")
    statuses = [
        "ready", "pending_review", "translation_pending",
        "processing_failed", "fulltext_unavailable",
    ]
    articles = batch_articles(int(digest_id), statuses=tuple(statuses))
    states = fulltext_states_for_batch(int(digest_id))
    # A due ``unavailable`` retry used to retain the normal article ordering and
    # consume the small hourly limit before brand-new records later in the
    # issue.  In a busy issue this could starve never-attempted articles for
    # hours.  Always admit fresh work first; retries remain ordered and keep
    # their existing backoff checks below.
    state_priority = {
        "processing": 1,
        "pending": 1,
        "failed": 2,
        "unavailable": 3,
        "ready": 4,
    }
    articles.sort(
        key=lambda article: (
            0
            if int(article["id"]) not in states
            else state_priority.get(
                str(states[int(article["id"])].get("status") or ""), 2
            ),
            str(article.get("first_seen_at") or ""),
            int(article["id"]),
        )
    )
    summary = {"total": len(articles), "ready": 0, "unavailable": 0, "failed": 0, "skipped": 0}
    processed = 0
    mimo_down = False
    for article in articles:
        if processed >= limit:
            summary["skipped"] += 1
            continue
        state = states.get(int(article["id"]))
        if state and state.get("status") == "ready" and (
            int(state.get("required_translations") or 0) > 0
            and int(state.get("translated") or 0) == int(state.get("required_translations") or 0)
        ):
            summary["ready"] += 1
            continue
        if state and state.get("status") == "unavailable" and not retry_unavailable:
            try:
                unavailable_retry_at = datetime.fromisoformat(str(state.get("next_retry_at") or ""))
                if unavailable_retry_at.tzinfo is None:
                    unavailable_retry_at = unavailable_retry_at.replace(tzinfo=timezone.utc)
            except ValueError:
                unavailable_retry_at = datetime.now(timezone.utc)
            if unavailable_retry_at > datetime.now(timezone.utc):
                summary["unavailable"] += 1
                continue
        if state and state.get("status") == "failed" and not retry_unavailable:
            try:
                retry_at = datetime.fromisoformat(str(state.get("next_retry_at") or ""))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
            except ValueError:
                retry_at = datetime.now(timezone.utc)
            if retry_at > datetime.now(timezone.utc):
                summary["skipped"] += 1
                continue
        processed += 1
        try:
            result = build_article_fulltext(article, translate=translate and not mimo_down)
        except MimoUnavailable as exc:
            mimo_down = True
            LOGGER.warning("journal fulltext: MiMo unavailable; stop publishing work: %s", exc)
            summary["failed"] += 1
            break
        except Exception as exc:
            LOGGER.warning("journal fulltext failed for article %s: %s", article.get("id"), exc)
            summary["failed"] += 1
            continue
        status = str(result.get("status") or "")
        if status in summary:
            summary[status] += 1
    summary["processed"] = processed
    summary["mimo_unavailable"] = mimo_down
    try:
        from journal_alerts import (
            _connect as journal_connect,
            _select_release_articles,
            public_batch_articles,
            update_batch_review,
        )

        publishable = public_batch_articles(int(digest_id))
        if len(publishable) > 45:
            selected, overflow = _select_release_articles(publishable, 45)
            with journal_connect() as conn:
                conn.executemany(
                    "UPDATE journal_articles SET status='deferred', batch_id=NULL, updated_at=? WHERE id=?",
                    [(_now_text(), int(item["id"])) for item in overflow],
                )
                conn.commit()
            publishable = selected
        digest = get_batch(int(digest_id)) or {}
        if publishable and str(digest.get("review_status") or "") != "approved":
            update_batch_review(
                int(digest_id), review_status="pending", status="reviewing"
            )
        summary["publishable"] = len(publishable)
    except Exception as exc:
        LOGGER.warning("could not prepare issue approval state: %s", exc)
        summary["publishable"] = 0
    return summary


def backfill_translations(limit_articles: int = 10) -> dict:
    """给已有产物但翻译未补齐的文章续译（Key 后配、或上次超预算时用）。"""
    init_fulltext_db()
    done = {"articles": 0, "paragraphs": 0}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT article_id, src_lang FROM journal_fulltext WHERE status = 'ready' "
            "ORDER BY updated_at DESC LIMIT 400"
        ).fetchall()
    for row in rows:
        if done["articles"] >= limit_articles:
            break
        article_id = int(row["article_id"])
        doc = load_document(article_id)
        if not doc:
            continue
        paragraphs = doc.get("paragraphs") or []
        pending = [
            p for p in paragraphs
            if not p.get("zh") and p.get("kind") != "reference" and len(str(p.get("text") or "").strip()) >= 2
        ]
        if not pending:
            continue
        try:
            added = translate_paragraphs(
                paragraphs, str(row["src_lang"] or "en"), article_id=article_id
            )
        except MimoUnavailable:
            break
        if added:
            doc["paragraphs"] = paragraphs
            _save_document(article_id, doc)
            translated = sum(1 for p in paragraphs if p.get("zh"))
            _upsert_state(article_id, translated=translated)
            done["articles"] += 1
            done["paragraphs"] += added
    return done
