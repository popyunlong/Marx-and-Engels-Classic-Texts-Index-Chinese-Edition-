#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed, resumable corpus text repair pipeline.

The worker reads the live corpus and source PDFs but can write only its review
directory.  It never promotes a database.  Full-page images stay local: only a
tightly anchored crop may be sent to MiMo, while DeepSeek receives text JSON.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import html
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

import fitz
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import normalize as production_normalize  # noqa: E402
from corpus_repair_store import (  # noqa: E402
    _connect,
    confirm_batch,
    init_db,
    json_text,
    record_event,
    review_db_path,
    status_snapshot,
    upsert_pages,
    utc_now,
)
from scripts.marx_ocr_audit import (  # noqa: E402
    AuditError,
    FatalProviderError,
    RapidClient,
    canonical_visible,
    choose_anchor,
    crop_for_anchors,
    identity_fingerprint,
    replace_canonical_segment,
    selected_row_fingerprint,
    sha256_file,
    sha256_text,
)


PIPELINE_VERSION = "2.0"
DETECTOR_VERSION = "3"
MIMO_MODEL = "mimo-v2.5"
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
PHASE_BOOKS = {
    1: ("文集", "全集"),
    2: ("全集二版", "马恩选集"),
}
PHASE_EXPECTED = {1: (51793, 62), 2: (37064, 41)}
CANARY_BUDGETS = (100, 500, 1000)
MAX_ISSUES_PER_PAGE = 64
MIN_ANCHOR_CHARS = 8


def resolve_deepseek_runtime(
    environ: dict[str, str] | os._Environ[str] | None = None,
    *,
    config_loader: Callable[[], Any] | None = None,
) -> tuple[str, str, str]:
    """Resolve text adjudication without exposing or copying credentials.

    Explicit environment values remain authoritative.  If the isolated repair
    service has no separate key, it may reuse the website's existing DeepSeek
    configuration read-only.  Keys for other providers are never sent to the
    DeepSeek endpoint.
    """
    env = os.environ if environ is None else environ
    key = str(env.get("DEEPSEEK_API_KEY", "") or env.get("APP_AI_API_KEY", "")).strip()
    base_url = str(env.get("DEEPSEEK_BASE_URL", "") or DEEPSEEK_BASE_URL).strip().rstrip("/")
    model = str(env.get("CORPUS_REPAIR_DEEPSEEK_MODEL", "") or DEEPSEEK_MODEL).strip()
    if key:
        return key, base_url, model

    try:
        if config_loader is None:
            from ai import load_ai_config  # Imported lazily; never imports the web app.
            config_loader = load_ai_config
        config = config_loader()
    except Exception:  # noqa: BLE001 - caller/provider validation remains fail-closed
        return "", base_url, model
    if str(getattr(config, "provider", "") or "").strip().lower() != "deepseek":
        return "", base_url, model
    config_key = str(getattr(config, "api_key", "") or "").strip()
    config_base = str(getattr(config, "base_url", "") or "").strip().rstrip("/")
    return config_key, (config_base or base_url), model

CJK = r"\u3400-\u9fff\uf900-\ufaff"
LATIN1_SUSPECT_RE = re.compile(
    r"\ufffd|(?:Ã.|Â.|â[€\u0080-\u00bf])|[\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff]"
)
ISOLATED_ASCII_RE = re.compile(rf"(?<=[{CJK}])[A-Za-z](?=(?:[{CJK}]|[，。；：！？、）】』”’\s]|$))")
HALF_PUNCT_RE = re.compile(rf"(?<=[{CJK}])[,;:!?](?=[{CJK}])")
PUNCT_RUN_RE = re.compile(r"[?？!！,，。；;:：、.]{4,}")
LINE_BREAK_RE = re.compile(rf"(?<=[{CJK}])[-—]\s*\n\s*(?=[{CJK}])")
FORMULA_RE = re.compile(r"(?:[A-Za-z]\s*[=+*/^<>]|[=+*/^<>]\s*[A-Za-z0-9])")
DATE_RE = re.compile(r"(?:1[5-9]\d{2}|20\d{2})\s*[年./—-]|\d{1,2}\s*月\s*\d{1,2}\s*日")


class ProviderError(RuntimeError):
    pass


class SubmissionUncertain(ProviderError):
    pass


class PauseRequested(RuntimeError):
    pass


@dataclass(frozen=True)
class InventoryPage:
    id: int
    book: str
    volume: int
    source_file: str
    pdf_page: int
    printed_page: str
    raw_text: str
    normalized_text: str
    risk_score: float
    risk_reasons: tuple[str, ...]


@dataclass(frozen=True)
class Suspect:
    error_type: str
    reason: str
    before: str
    left_anchor: str
    right_anchor: str
    start: int
    end: int


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": ""}
    with tempfile.NamedTemporaryFile(mode, dir=path.parent, delete=False, **kwargs) as handle:
        handle.write(data)
        temp = Path(handle.name)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def redact(value: Any, secrets: Sequence[str] = ()) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,}\]]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token)\s*[:=]\s*)[^\s,}\]]+", r"\1[REDACTED]", text)
    return text[:2000]


def checked_sha256_file(path: Path, checkpoint: Callable[[], None] | None = None) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if checkpoint is not None:
                checkpoint()
    return digest.hexdigest()


def available_public_books(project_root: Path) -> tuple[str, ...]:
    payload = yaml.safe_load((project_root / "config" / "books.yaml").read_text(encoding="utf-8")) or {}
    return tuple(
        str(item.get("key") or "")
        for item in payload.get("books") or []
        if isinstance(item, dict) and item.get("available") is True and str(item.get("key") or "")
    )


def phase_books(project_root: Path, phase: int) -> tuple[str, ...]:
    if phase in PHASE_BOOKS:
        return PHASE_BOOKS[phase]
    if phase != 3:
        raise ValueError("phase must be 1, 2, or 3")
    excluded = set(PHASE_BOOKS[1]) | set(PHASE_BOOKS[2])
    return tuple(book for book in available_public_books(project_root) if book not in excluded)


def load_authority(project_root: Path) -> list[dict[str, Any]]:
    authority: list[dict[str, Any]] = []
    wenji_path = project_root / "config" / "wenji_text_corrections.yaml"
    if wenji_path.is_file():
        payload = yaml.safe_load(wenji_path.read_text(encoding="utf-8")) or {}
        for section in ("pages", "han_pages"):
            for item in payload.get(section) or []:
                if not isinstance(item, dict):
                    continue
                authority.append({
                    "book": "文集", "volume": int(item.get("volume") or 0),
                    "pdf_page": int(item.get("page") or 0), "find": str(item.get("find") or ""),
                    "replace": str(item.get("replace") or ""), "evidence": str(item.get("evidence") or ""),
                    "scope": section,
                })
        for item in payload.get("book_wide") or []:
            if isinstance(item, dict):
                authority.append({"book": "文集", "volume": None, "pdf_page": None,
                                  "find": str(item.get("find") or ""),
                                  "replace": str(item.get("replace") or ""),
                                  "evidence": str(item.get("evidence") or ""), "scope": "book"})
        for volume, items in (payload.get("volume_wide") or {}).items():
            for item in items or []:
                if isinstance(item, dict):
                    authority.append({"book": "文集", "volume": int(volume), "pdf_page": None,
                                      "find": str(item.get("find") or ""),
                                      "replace": str(item.get("replace") or ""),
                                      "evidence": str(item.get("evidence") or ""), "scope": "volume"})
    quanji_path = project_root / "config" / "quanji_text_corrections.yaml"
    if quanji_path.is_file():
        payload = yaml.safe_load(quanji_path.read_text(encoding="utf-8")) or {}
        for before, after in (payload.get("corrections") or {}).items():
            authority.append({"book": "全集", "volume": None, "pdf_page": None,
                              "find": str(before), "replace": str(after),
                              "evidence": "全集高置信等长整词校勘表", "scope": "book"})
    return authority


def index_authority_terms(authority: Sequence[dict[str, Any]]) -> dict[tuple[str, int | None, int | None], tuple[str, ...]]:
    indexed: dict[tuple[str, int | None, int | None], set[str]] = defaultdict(set)
    for item in authority:
        term = str(item.get("find") or "")
        if not term:
            continue
        key = (
            str(item.get("book") or ""),
            int(item["volume"]) if item.get("volume") is not None else None,
            int(item["pdf_page"]) if item.get("pdf_page") is not None else None,
        )
        indexed[key].add(term)
    return {key: tuple(sorted(values, key=lambda value: (-len(value), value))) for key, values in indexed.items()}


def authority_terms_for(indexed: dict[tuple[str, int | None, int | None], tuple[str, ...]],
                        book: str, volume: int, pdf_page: int) -> tuple[str, ...]:
    values: list[str] = []
    for key in ((book, None, None), (book, volume, None), (book, volume, pdf_page)):
        values.extend(indexed.get(key, ()))
    return tuple(dict.fromkeys(values))


def authority_for(page: sqlite3.Row | InventoryPage, suspect: Suspect,
                  authority: Sequence[dict[str, Any]]) -> tuple[str, str]:
    book = str(page["book"] if isinstance(page, sqlite3.Row) else page.book)
    volume = int(page["volume"] if isinstance(page, sqlite3.Row) else page.volume)
    pdf_page = int(page["pdf_page"] if isinstance(page, sqlite3.Row) else page.pdf_page)
    before = canonical_visible(suspect.before)
    for item in authority:
        if str(item.get("book") or "") != book:
            continue
        if item.get("volume") is not None and int(item["volume"]) != volume:
            continue
        if item.get("pdf_page") is not None and int(item["pdf_page"]) != pdf_page:
            continue
        if canonical_visible(str(item.get("find") or "")) == before:
            return canonical_visible(str(item.get("replace") or "")), str(item.get("evidence") or "")
        configured_before = canonical_visible(str(item.get("find") or ""))
        configured_after = canonical_visible(str(item.get("replace") or ""))
        changes = [opcode for opcode in difflib.SequenceMatcher(
            None, configured_before, configured_after, autojunk=False
        ).get_opcodes() if opcode[0] != "equal"]
        if len(changes) == 1:
            _tag, i1, i2, j1, j2 = changes[0]
            if configured_before[i1:i2] == before:
                return configured_after[j1:j2], str(item.get("evidence") or "")
    return "", ""


def _suspicious_line_groups(raw: str) -> list[tuple[int, int, str]]:
    lines = list(re.finditer(r"[^\r\n]*(?:\r?\n|$)", raw))
    groups: list[tuple[int, int, str]] = []
    pending: list[re.Match[str]] = []
    for match in lines:
        value = match.group(0).strip()
        suspicious = bool(
            value and len(canonical_visible(value)) <= 16
            and not re.search(rf"[{CJK}]", value)
            and (LATIN1_SUSPECT_RE.search(value) or re.search(r"[A-Za-z].*[./\\]|[./\\].*[A-Za-z]|^[./\\、…-]+$", value))
        )
        if suspicious:
            pending.append(match)
            continue
        if pending:
            start, end = pending[0].start(), pending[-1].end()
            groups.append((start, end, raw[start:end].strip()))
            pending = []
    if pending:
        start, end = pending[0].start(), pending[-1].end()
        groups.append((start, end, raw[start:end].strip()))
    return groups


def _span_to_suspect(raw: str, start: int, end: int, error_type: str, reason: str) -> Suspect | None:
    compact = canonical_visible(raw)
    prefix = canonical_visible(raw[:start])
    before = canonical_visible(raw[start:end])
    compact_start = len(prefix)
    compact_end = compact_start + len(before)
    if not before:
        return None
    left = choose_anchor(compact, compact_start, -1, minimum=MIN_ANCHOR_CHARS, maximum=64)
    right = choose_anchor(compact, compact_end, 1, minimum=MIN_ANCHOR_CHARS, maximum=64)
    # A section heading can sit near the very top of the stored page text.  In
    # that edge case there may be fewer than eight preceding body characters;
    # retain a shorter anchor only when it is still unique on the page.
    if not left:
        left = choose_anchor(compact, compact_start, -1, minimum=4, maximum=64)
    if not right:
        right = choose_anchor(compact, compact_end, 1, minimum=4, maximum=64)
    return Suspect(error_type, reason, before, left, right, compact_start, compact_end)


def detect_suspects(raw: str, *, known_terms: Iterable[str] = ()) -> list[Suspect]:
    ranges: list[tuple[int, int, str, str]] = []
    for start, end, _value in _suspicious_line_groups(raw):
        ranges.append((start, end, "standalone_garbage", "孤立乱码或拟形标题"))
    detectors = (
        (LATIN1_SUSPECT_RE, "mojibake", "异常拉丁字符或乱码"),
        (ISOLATED_ASCII_RE, "shape_character", "中文语境中的孤立英文字母"),
        (HALF_PUNCT_RE, "punctuation", "中文语境中的半角标点"),
        (PUNCT_RUN_RE, "punctuation_run", "异常连续标点"),
        (LINE_BREAK_RE, "line_break", "疑似错误断行"),
    )
    for pattern, kind, reason in detectors:
        for match in pattern.finditer(raw):
            if kind == "mojibake" and "�" not in match.group(0) and not match.group(0).startswith(("Ã", "Â", "â")):
                left_char = raw[match.start() - 1:match.start()]
                right_char = raw[match.end():match.end() + 1]
                if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", left_char + right_char):
                    continue
            ranges.append((match.start(), match.end(), kind, reason))
    for term in known_terms:
        if not term:
            continue
        for match in re.finditer(re.escape(term), raw):
            ranges.append((match.start(), match.end(), "known_ocr_confusion", "命中既有校勘规则"))
    ranges.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    kept: list[tuple[int, int, str, str]] = []
    overflow: tuple[int, int, str, str] | None = None
    for item in ranges:
        if any(item[0] >= old[0] and item[1] <= old[1] for old in kept):
            continue
        if len(kept) >= MAX_ISSUES_PER_PAGE:
            overflow = item
            break
        kept.append(item)
    output: list[Suspect] = []
    seen: set[tuple[str, str, str]] = set()
    for start, end, kind, reason in kept:
        suspect = _span_to_suspect(raw, start, end, kind, reason)
        if suspect is None:
            continue
        key = (suspect.before, suspect.left_anchor, suspect.right_anchor)
        if key not in seen:
            output.append(suspect)
            seen.add(key)
    if overflow is not None:
        sentinel = _span_to_suspect(
            raw, overflow[0], overflow[1], "page_complexity", "单页可疑区域超过安全上限，剩余项须整页人工复核",
        )
        if sentinel is not None:
            output.append(sentinel)
    return output


def risk_features(raw: str, *, known_terms: Iterable[str] = ()) -> tuple[float, tuple[str, ...]]:
    suspects = detect_suspects(raw, known_terms=known_terms)
    reasons = list(dict.fromkeys(item.reason for item in suspects))
    score = float(sum({
        "standalone_garbage": 80, "mojibake": 60, "shape_character": 45,
        "known_ocr_confusion": 40, "punctuation_run": 25, "punctuation": 15, "line_break": 15,
    }.get(item.error_type, 10) for item in suspects))
    compact_len = len(canonical_visible(raw))
    if compact_len < 80:
        score += 5
        reasons.append("低字数页面")
    return score, tuple(dict.fromkeys(reasons))


def protected_reasons(suspect: Suspect, *, page_raw: str, crop_readable: bool | None,
                      anchor_unique: bool, authority_value: str = "") -> list[str]:
    context = suspect.left_anchor[-32:] + suspect.before + suspect.right_anchor[:32]
    reasons: list[str] = []
    if not suspect.left_anchor or not suspect.right_anchor or not anchor_unique:
        reasons.append("锚点不足或不唯一")
    if suspect.error_type == "page_complexity":
        reasons.append("单页可疑区域超过安全上限")
    if crop_readable is False:
        reasons.append("局部图无法可靠辨认")
    if DATE_RE.search(context):
        reasons.append("日期")
    if FORMULA_RE.search(context):
        reasons.append("公式")
    if re.search(r"[A-Za-z]{2,}|[A-Za-z][’'-][A-Za-z]", context):
        reasons.append("真实外文或专名")
    if re.search(r"(?:马克思|恩格斯|列宁|译者|著者|姓|人名)", context):
        reasons.append("人名或专名")
    if "\t" in page_raw or "|" in page_raw:
        reasons.append("表格")
    if suspect.error_type in {"standalone_garbage", "punctuation_run"} and re.fullmatch(r"[.。·…—-]{4,}", suspect.before):
        reasons.append("目录点线或原书特殊标点")
    if authority_value and authority_value == suspect.before:
        reasons.append("既有校勘规则要求保留原文")
    return list(dict.fromkeys(reasons))


def _json_response(payload: dict[str, Any]) -> dict[str, Any]:
    content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
    if isinstance(content, list):
        content = "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content or "").strip(), flags=re.I)
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError("provider returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ProviderError("provider JSON is not an object")
    return result


class OpenAIJsonClient:
    def __init__(self, *, key: str, base_url: str, model: str, auth_header: str = "Authorization",
                 auth_prefix: str = "Bearer ", opener: Callable[..., Any] = urllib.request.urlopen):
        self.key = str(key or "").strip()
        self.base_url = str(base_url or "").rstrip("/")
        self.model = str(model or "").strip()
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.opener = opener
        if not self.key:
            raise FatalProviderError(f"{self.model} credential is not configured")

    def complete(self, messages: list[dict[str, Any]], *, max_tokens: int = 320) -> tuple[dict[str, Any], dict[str, Any]]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        if self.model.startswith("mimo-"):
            body["max_completion_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens
        encoded = canonical_json(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=encoded,
            headers={self.auth_header: self.auth_prefix + self.key, "Content-Type": "application/json",
                     "User-Agent": "marx-corpus-repair/2.0"},
        )
        try:
            with self.opener(request, timeout=75) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 403, 404}:
                raise FatalProviderError(f"{self.model} endpoint rejected request (HTTP {exc.code})") from exc
            if exc.code != 429:
                raise SubmissionUncertain(f"{self.model} response outcome uncertain (HTTP {exc.code})") from exc
            raise ProviderError(f"{self.model} unavailable (HTTP {exc.code})") from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            raise SubmissionUncertain(f"{self.model} response outcome uncertain after timeout") from exc
        return _json_response(payload), {
            "model": self.model,
            "finish_reason": str((payload.get("choices") or [{}])[0].get("finish_reason") or ""),
            "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
        }


class MimoClient(OpenAIJsonClient):
    def __init__(self, key: str, *, base_url: str = MIMO_BASE_URL, model: str = MIMO_MODEL,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        super().__init__(key=key, base_url=base_url, model=model, auth_header="api-key", auth_prefix="", opener=opener)

    def transcribe_crop(self, png: bytes, *, left: str, right: str) -> tuple[dict[str, Any], dict[str, Any]]:
        prompt = (
            "你是中文历史文献逐字转写员。图片只是一小块原书局部。禁止润色、补写或依据常识猜测。"
            "读取给定左右锚点之间肉眼可见的全部字符；看不清就将 readable 设为 false。"
            "只返回JSON：{\"between\":\"逐字字符\",\"readable\":true,\"layout_risk\":false}。"
            f"\n左锚点：{left}\n右锚点：{right}"
        )
        return self.complete([{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
        ]}], max_tokens=256)


class DeepSeekClient(OpenAIJsonClient):
    def __init__(self, key: str, *, base_url: str = DEEPSEEK_BASE_URL, model: str = DEEPSEEK_MODEL,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        super().__init__(key=key, base_url=base_url, model=model, opener=opener)

    def adjudicate(self, *, baseline: str, rapid: str, mimo: str, left: str, right: str,
                   protected: Sequence[str], authority: str) -> tuple[dict[str, Any], dict[str, Any]]:
        evidence = {
            "baseline": baseline, "rapid_ocr": rapid, "mimo_visual": mimo,
            "left_anchor": left, "right_anchor": right,
            "protected_reasons": list(protected), "authority_value": authority,
        }
        prompt = (
            "你只审核证据一致性，不能提出第三种文本，也不能凭知识猜字。"
            "仅当 rapid_ocr 与 mimo_visual 逐字完全相同、不是 baseline、保护原因为空、"
            "且 authority_value 为空或与二者相同，才返回 decision=agree。"
            "否则 decision 必须是 reject 或 manual。只返回JSON："
            "{\"decision\":\"agree|reject|manual\",\"proposed\":\"只能复制两路一致文本\","
            "\"risks\":[\"原因\"]}。\n证据：" + canonical_json(evidence)
        )
        return self.complete([{"role": "user", "content": prompt}], max_tokens=320)


def strict_eligibility(*, baseline: str, rapid: str, mimo: str, deepseek: dict[str, Any],
                       protected: Sequence[str], authority: str, mimo_readable: bool,
                       layout_risk: bool) -> bool:
    proposed = canonical_visible(str(deepseek.get("proposed") or ""))
    decision = str(deepseek.get("decision") or "").lower()
    return bool(
        mimo_readable and not layout_risk and not protected and rapid and rapid == mimo
        and rapid != baseline and decision == "agree" and proposed == rapid
        and (not authority or authority == rapid)
    )


def _crop_from_rapid_rows(full_png: bytes, rows: Sequence[dict[str, Any]], *, left: str,
                          before: str, right: str) -> bytes | None:
    text = canonical_visible("\n".join(str(row.get("text") or "") for row in rows))
    needle = left + before + right
    if text.count(needle) != 1:
        if text.count(left) != 1 or text.count(right) != 1:
            return None
        start_row = end_row = None
        compact = ""
        ranges: list[tuple[int, int]] = []
        for index, row in enumerate(rows):
            value = canonical_visible(str(row.get("text") or ""))
            ranges.append((len(compact), len(compact) + len(value)))
            compact += value
        start = compact.find(left)
        end = compact.find(right, start + len(left)) + len(right)
        if start < 0 or end <= 0:
            return None
        for index, (a, b) in enumerate(ranges):
            if start_row is None and b > start:
                start_row = index
            if a < end:
                end_row = index
        if start_row is None or end_row is None:
            return None
    else:
        start_row, end_row = 0, len(rows) - 1
        compact = ""
        ranges = []
        for row in rows:
            value = canonical_visible(str(row.get("text") or ""))
            ranges.append((len(compact), len(compact) + len(value)))
            compact += value
        start = compact.find(needle)
        end = start + len(needle)
        start_row = next((i for i, (_a, b) in enumerate(ranges) if b > start), 0)
        end_row = max(i for i, (a, _b) in enumerate(ranges) if a < end)
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(full_png, dtype=np.uint8), cv2.IMREAD_COLOR)
        boxes = [rows[index].get("bbox") for index in range(max(0, start_row - 1), min(len(rows), end_row + 2))]
        points = [point for box in boxes if box for point in box]
        if image is None or not points:
            return None
        x0 = max(0, int(min(float(point[0]) for point in points)) - 36)
        y0 = max(0, int(min(float(point[1]) for point in points)) - 28)
        x1 = min(image.shape[1], int(max(float(point[0]) for point in points)) + 36)
        y1 = min(image.shape[0], int(max(float(point[1]) for point in points)) + 28)
        if x1 <= x0 or y1 <= y0 or (x1 - x0) * (y1 - y0) > image.shape[0] * image.shape[1] * 0.20:
            return None
        ok, encoded = cv2.imencode(".png", image[y0:y1, x0:x1])
        return encoded.tobytes() if ok else None
    except Exception:
        return None


def local_crop_and_rapid(page: fitz.Page, suspect: Suspect, rapid: RapidClient) -> tuple[bytes | None, str, dict[str, Any]]:
    crop = crop_for_anchors(page, suspect.left_anchor, suspect.before, suspect.right_anchor, dpi=300)
    if crop:
        try:
            crop_pix = fitz.Pixmap(crop)
            full_pixels = (page.rect.width * 300 / 72.0) * (page.rect.height * 300 / 72.0)
            if crop_pix.width * crop_pix.height > full_pixels * 0.20:
                crop = None
        except Exception:
            crop = None
    metadata: dict[str, Any] = {"crop_source": "pdf_text_layer" if crop else "rapid_full_page_alignment"}
    if crop:
        rapid_text, rows = rapid.recognize(crop)
        between = extract_between_loose(rapid_text, suspect.left_anchor, suspect.right_anchor)
        metadata["rapid_confidence_min"] = min((float(row.get("confidence") or 0) for row in rows), default=0.0)
        return crop, between or "", metadata
    pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72.0, 150 / 72.0), alpha=False)
    full_png = pix.tobytes("png")
    rapid_text, rows = rapid.recognize(full_png)
    crop = _crop_from_rapid_rows(full_png, rows, left=suspect.left_anchor,
                                 before=suspect.before, right=suspect.right_anchor)
    if not crop:
        return None, "", metadata
    crop_text, crop_rows = rapid.recognize(crop)
    metadata["rapid_confidence_min"] = min((float(row.get("confidence") or 0) for row in crop_rows), default=0.0)
    return crop, extract_between_loose(crop_text, suspect.left_anchor, suspect.right_anchor) or "", metadata


def local_page_worker(*, pdf_path: Path, pdf_page: int, suspects_path: Path, output_dir: Path) -> dict[str, Any]:
    """Child-process entrypoint. It never calls a network provider."""
    payload = json.loads(suspects_path.read_text(encoding="utf-8"))
    suspects = [Suspect(**item) for item in payload]
    output_dir.mkdir(parents=True, exist_ok=True)
    rapid = RapidClient()
    results: list[dict[str, Any]] = []
    with fitz.open(pdf_path) as document:
        index = int(pdf_page) - 1
        if not 0 <= index < document.page_count:
            raise AuditError("PDF page outside source document")
        page = document[index]
        for number, suspect in enumerate(suspects, 1):
            crop, value, metadata = local_crop_and_rapid(page, suspect, rapid)
            crop_name = ""
            if crop:
                crop_name = f"crop-{number}.png"
                atomic_write(output_dir / crop_name, crop)
            results.append({"crop": crop_name, "rapid_value": value, "metadata": metadata})
    result = {"results": results}
    atomic_write(output_dir / "result.json", json.dumps(result, ensure_ascii=False))
    return result


def extract_between_loose(text: str, left: str, right: str) -> str | None:
    compact = canonical_visible(text)
    if not left or not right:
        return None
    start = compact.find(left)
    if start < 0 or compact.find(left, start + 1) >= 0:
        return None
    start += len(left)
    end = compact.find(right, start)
    if end < start or compact.find(right, end + 1) >= 0:
        return None
    return compact[start:end]


def resolve_pdf(project_root: Path, source_file: str) -> Path:
    root = project_root.resolve()
    target = (root / source_file).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise AuditError("source PDF escapes project root") from exc
    if not target.is_file():
        raise AuditError(f"source PDF missing: {source_file}")
    return target


def _read_pressure(kind: str) -> float:
    try:
        line = (Path("/proc/pressure") / kind).read_text(encoding="ascii").splitlines()[0]
        match = re.search(r"avg10=([0-9.]+)", line)
        return float(match.group(1)) if match else 0.0
    except Exception:
        return 0.0


def _memory() -> tuple[float, float]:
    values: dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemAvailable", "MemTotal"}:
                values[key] = float(raw.split()[0]) / 1024 / 1024
    except Exception:
        return 999.0, 999.0
    return values.get("MemAvailable", 0.0), values.get("MemTotal", 0.0)


def _swap_counters() -> tuple[int, int]:
    """Return cumulative swap-in/out page counters from procfs.

    A single delta may be a harmless delayed write, so the guard only pauses
    after activity is observed on two consecutive probes.
    """
    values = {"pswpin": 0, "pswpout": 0}
    try:
        for line in Path("/proc/vmstat").read_text(encoding="ascii").splitlines():
            key, _, raw = line.partition(" ")
            if key in values:
                values[key] = int(raw.strip())
    except Exception:
        return 0, 0
    return values["pswpin"], values["pswpout"]


class HealthGuard:
    def __init__(self, health_base: str, output_root: Path, *, opener: Callable[..., Any] = urllib.request.urlopen,
                 activity_marker: Path = Path("/run/marx-search/interactive-render")):
        self.health_base = health_base.rstrip("/")
        self.output_root = output_root
        self.opener = opener
        self.activity_marker = activity_marker
        self.baseline_ms = 0.0
        self.latencies: deque[float] = deque(maxlen=30)
        self._last_swap = _swap_counters()
        self._swap_active_streak = 0

    def interactive_render_active(self) -> bool:
        try:
            if self.activity_marker.is_file():
                return True
            if self.activity_marker.is_dir():
                now = time.time()
                return any(item.is_file() and now - item.stat().st_mtime <= 120
                           for item in self.activity_marker.iterdir())
        except OSError:
            return False
        return False

    def probe(self, path: str) -> tuple[bool, float]:
        started = time.perf_counter()
        try:
            req = urllib.request.Request(self.health_base + path, headers={"User-Agent": "marx-corpus-repair-health/2.0"})
            with self.opener(req, timeout=8) as response:
                response.read(1)
                ok = 200 <= int(getattr(response, "status", 200)) < 400
        except Exception:
            ok = False
        return ok, (time.perf_counter() - started) * 1000

    def establish_baseline(self, count: int = 25) -> float:
        samples: list[float] = []
        for index in range(count):
            ok, elapsed = self.probe("/api/runtime" if index % 2 == 0 else "/")
            if ok:
                samples.append(elapsed)
            time.sleep(0.03)
        if len(samples) < max(10, count // 2):
            raise AuditError("website baseline probes failed")
        self.baseline_ms = percentile(samples, 95)
        return self.baseline_ms

    def reasons(self) -> tuple[list[str], dict[str, float]]:
        ok_runtime, latency_runtime = self.probe("/api/runtime")
        ok_home, latency_home = self.probe("/")
        latency = max(latency_runtime, latency_home)
        if ok_runtime and ok_home:
            self.latencies.append(latency)
        available, total = _memory()
        try:
            load = float(os.getloadavg()[0])
        except (AttributeError, OSError):
            load = 0.0
        cpu_count = max(1, os.cpu_count() or 1)
        free = shutil.disk_usage(self.output_root).free / 1024 ** 3
        cpu_psi, io_psi = _read_pressure("cpu"), _read_pressure("io")
        swap_now = _swap_counters()
        swap_delta = max(0, swap_now[0] - self._last_swap[0]) + max(0, swap_now[1] - self._last_swap[1])
        self._last_swap = swap_now
        self._swap_active_streak = self._swap_active_streak + 1 if swap_delta else 0
        reasons: list[str] = []
        if not ok_runtime or not ok_home:
            reasons.append("网站健康检查失败")
        if available < 3.0 or (total > 0 and available / total < 0.35):
            reasons.append("可用内存低于安全门槛")
        if load > cpu_count * 0.5:
            reasons.append("系统负载超过逻辑核数50%")
        if cpu_psi > 20.0 or io_psi > 10.0:
            reasons.append("CPU或I/O压力超过安全门槛")
        if self._swap_active_streak >= 2:
            reasons.append("检测到持续换页活动")
        if free < 15.0:
            reasons.append("数据盘可用空间低于15GiB")
        if self.interactive_render_active():
            reasons.append("前台正在渲染PDF页面")
        if len(self.latencies) >= 10 and self.baseline_ms:
            rolling = percentile(self.latencies, 95)
            if rolling > min(self.baseline_ms * 1.15, self.baseline_ms + 50.0):
                reasons.append("网站滚动P95超过守卫阈值")
        metrics = {
            "web_ok": 1.0 if ok_runtime and ok_home else 0.0,
            "web_latency_ms": latency, "mem_available_gib": available, "mem_total_gib": total,
            "load1": load, "cpu_psi_avg10": cpu_psi, "io_psi_avg10": io_psi,
            "swap_pages_delta": float(swap_delta), "data_free_gib": free,
        }
        return reasons, metrics


def percentile(values: Iterable[float], percentile_value: float) -> float:
    items = sorted(float(value) for value in values)
    if not items:
        return 0.0
    index = max(0, min(len(items) - 1, math.ceil(percentile_value / 100 * len(items)) - 1))
    return items[index]


def inventory(database: Path, books: Sequence[str], authority: Sequence[dict[str, Any]], *,
              checkpoint: Callable[[], None] | None = None) -> list[InventoryPage]:
    term_index = index_authority_terms(authority)
    placeholders = ",".join("?" for _ in books)
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    output = []
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            "SELECT id,book,volume,source_file,pdf_page,COALESCE(printed_page,''),raw_text,normalized_text "
            f"FROM pages WHERE book IN ({placeholders}) ORDER BY source_file,pdf_page", tuple(books)
        )
        for index, row in enumerate(rows, 1):
            score, reasons = risk_features(
                str(row[6]), known_terms=authority_terms_for(
                    term_index, str(row[1]), int(row[2]), int(row[4]),
                ),
            )
            output.append(InventoryPage(int(row[0]), str(row[1]), int(row[2]), str(row[3]), int(row[4]),
                                        str(row[5]), str(row[6]), str(row[7]), score, reasons))
            if checkpoint is not None and index % 128 == 0:
                checkpoint()
    if checkpoint is not None:
        checkpoint()
    return output


def apply_feedback_priority(pages: Sequence[InventoryPage], feedback_db: Path | None) -> list[InventoryPage]:
    if feedback_db is None or not feedback_db.is_file():
        return list(pages)
    by_id = {page.id: page for page in pages}
    by_source_page = {(page.source_file.replace("\\", "/"), page.pdf_page): page.id for page in pages}
    by_printed: dict[tuple[str, str], list[int]] = defaultdict(list)
    for page in pages:
        by_printed[(page.source_file.replace("\\", "/"), page.printed_page.strip())].append(page.id)
    priority: set[int] = set()
    try:
        with sqlite3.connect(f"file:{feedback_db.resolve().as_posix()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_error_reports'").fetchone():
                return list(pages)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(page_error_reports)")}
            selected = [name for name in ("corpus_page_id", "source_ref", "page", "status", "issue_type") if name in columns]
            for row in conn.execute("SELECT " + ",".join(selected) + " FROM page_error_reports"):
                data = dict(row)
                if str(data.get("status") or "open") != "open":
                    continue
                if str(data.get("issue_type") or "page_number") not in {"text_error", "punctuation", "other"}:
                    continue
                page_id = int(data.get("corpus_page_id") or 0)
                source_ref = str(data.get("source_ref") or "").replace("\\", "/")
                exact_page = by_id.get(page_id)
                if exact_page is not None and (
                    not source_ref
                    or exact_page.source_file.replace("\\", "/").endswith(source_ref)
                    or source_ref.endswith(exact_page.source_file.replace("\\", "/"))
                ):
                    priority.add(page_id)
                    continue
                number_match = re.search(r"\d{1,5}", str(data.get("page") or ""))
                number = number_match.group(0) if number_match else ""
                for (source, pdf_page), candidate_id in by_source_page.items():
                    if source_ref and (source.endswith(source_ref) or source_ref.endswith(source)):
                        if str(pdf_page) == number:
                            priority.add(candidate_id)
                        priority.update(by_printed.get((source, number), ()))
    except (OSError, sqlite3.Error):
        return list(pages)
    return [replace(page, risk_score=page.risk_score + 10000.0,
                    risk_reasons=tuple(dict.fromkeys((*page.risk_reasons, "用户文字报告"))))
            if page.id in priority else page for page in pages]


def order_inventory(pages: Sequence[InventoryPage], *, reliability_tiers: bool = False) -> list[InventoryPage]:
    user_priority = sorted(
        (page for page in pages if "用户文字报告" in page.risk_reasons),
        key=lambda page: (-page.risk_score, page.source_file, page.pdf_page),
    )
    groups: dict[str, list[InventoryPage]] = defaultdict(list)
    for page in pages:
        if "用户文字报告" in page.risk_reasons:
            continue
        groups[page.source_file].append(page)
    for source in groups:
        groups[source].sort(key=lambda page: (-page.risk_score, page.pdf_page))
    if reliability_tiers:
        sources = sorted(groups, key=lambda source: (
            sum(page.risk_score > 0 for page in groups[source]) / max(1, len(groups[source])),
            sum(page.risk_score for page in groups[source]) / max(1, len(groups[source])),
            -sum(len(page.normalized_text) for page in groups[source]) / max(1, len(groups[source])),
            source,
        ))
        tier_size = max(1, math.ceil(len(sources) / 3))
        source_tiers = [sources[index:index + tier_size] for index in range(0, len(sources), tier_size)]
    else:
        sources = sorted(groups, key=lambda value: sha256_text("corpus-repair-v2\0" + value))
        source_tiers = [sources]
    ordered: list[InventoryPage] = list(user_priority)
    for tier in source_tiers:
        index = 0
        while True:
            added = False
            for source in tier:
                if index < len(groups[source]):
                    ordered.append(groups[source][index])
                    added = True
            if not added:
                break
            index += 1
    return ordered


def create_batch(*, database: Path, project_root: Path, output_root: Path, phase: int,
                 batch_id: str = "", feedback_db: Path | None = None,
                 checkpoint: Callable[[], None] | None = None) -> str:
    db = init_db(output_root / "review.sqlite3")
    source_hash = checked_sha256_file(database, checkpoint)
    batch_id = batch_id or f"phase{phase}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{source_hash[:8]}"
    authority = load_authority(project_root)
    pages = order_inventory(apply_feedback_priority(
        inventory(database, phase_books(project_root, phase), authority, checkpoint=checkpoint), feedback_db
    ), reliability_tiers=(phase == 3))
    if not pages:
        raise AuditError("selected phase has no corpus pages")
    expected = PHASE_EXPECTED.get(phase)
    sources = len({page.source_file for page in pages})
    if expected and (len(pages), sources) != expected:
        raise AuditError(f"phase {phase} inventory mismatch: got {len(pages)} pages/{sources} sources, expected {expected}")
    with _connect(db) as conn:
        try:
            conn.execute(
                "INSERT INTO repair_batches(id,phase,state,source_database,source_database_sha256,planned_pages,created_at) "
                "VALUES(?,?,'planned',?,?,?,?)",
                (batch_id, phase, str(database.resolve()), source_hash, len(pages), utc_now()),
            )
            conn.commit()
            upsert_pages(conn, (
                (batch_id, page.id, order, page.book, page.volume, page.source_file, page.pdf_page,
                 page.printed_page, sha256_text(page.raw_text), page.raw_text, page.normalized_text,
                 page.risk_score, json_text(page.risk_reasons))
                for order, page in enumerate(pages, 1)
            ), checkpoint=checkpoint)
            record_event(conn, batch_id, "info", "batch_planned", f"pages={len(pages)} sources={sources}")
        except Exception:
            conn.execute("DELETE FROM repair_batches WHERE id=?", (batch_id,))
            conn.commit()
            raise
    return batch_id


class RepairRunner:
    def __init__(self, *, database: Path, project_root: Path, output_root: Path, batch_id: str,
                 mimo: MimoClient | Any, deepseek: DeepSeekClient | Any,
                 rapid_factory: Callable[[], Any] = RapidClient, health_base: str = "http://127.0.0.1:8000",
                 guard_poll_seconds: float = 10.0, healthy_resume_seconds: float = 900.0,
                 sleep: Callable[[float], None] = time.sleep, guard: HealthGuard | None = None):
        self.database = database.resolve()
        self.project_root = project_root.resolve()
        self.output_root = output_root.resolve()
        self.db_path = self.output_root / "review.sqlite3"
        self.batch_id = batch_id
        self.conn = _connect(self.db_path)
        self.mimo = mimo
        self.deepseek = deepseek
        self.rapid_factory = rapid_factory
        self._rapid: Any = None
        self.guard = guard or HealthGuard(health_base, self.output_root)
        self.guard_poll_seconds = guard_poll_seconds
        self.healthy_resume_seconds = healthy_resume_seconds
        self.sleep = sleep
        self.authority = load_authority(self.project_root)
        self.known_terms = index_authority_terms(self.authority)
        self.secrets = [getattr(mimo, "key", ""), getattr(deepseek, "key", "")]
        self._last_checkpoint_probe = 0.0

    def close(self) -> None:
        self.conn.close()

    def rapid(self) -> Any:
        if self._rapid is None:
            self._rapid = self.rapid_factory()
        return self._rapid

    def _metrics(self, values: dict[str, float]) -> None:
        self.conn.executemany(
            "INSERT INTO repair_metrics(batch_id,at,name,value) VALUES(?,?,?,?)",
            [(self.batch_id, utc_now(), key, float(value)) for key, value in values.items()],
        )
        self.conn.commit()

    def guarded_checkpoint(self) -> None:
        if time.monotonic() - self._last_checkpoint_probe < self.guard_poll_seconds:
            return
        reasons, metrics = self.guard.reasons()
        self._metrics(metrics)
        self._last_checkpoint_probe = time.monotonic()
        if reasons:
            reason = "; ".join(reasons)
            self.conn.execute(
                "UPDATE repair_batches SET state='paused',paused_reason=?,last_checkpoint_at=? WHERE id=?",
                (reason, utc_now(), self.batch_id),
            )
            self.conn.commit()
            record_event(self.conn, self.batch_id, "warning", "resource_guard_paused", reason)
            raise PauseRequested(reason)

    def wait_healthy(self, *, deadline: float) -> bool:
        healthy_since: float | None = None
        state_row = self.conn.execute(
            "SELECT state FROM repair_batches WHERE id=?", (self.batch_id,)
        ).fetchone()
        paused = bool(state_row and str(state_row[0]) == "paused")
        while time.time() < deadline:
            reasons, metrics = self.guard.reasons()
            self._metrics(metrics)
            if reasons:
                healthy_since = None
                reason = "; ".join(reasons)
                self.conn.execute("UPDATE repair_batches SET state='paused',paused_reason=?,last_checkpoint_at=? WHERE id=?",
                                  (reason, utc_now(), self.batch_id))
                self.conn.commit()
                if not paused:
                    record_event(self.conn, self.batch_id, "warning", "resource_guard_paused", reason)
                    paused = True
            else:
                if not paused:
                    return True
                if healthy_since is None:
                    healthy_since = time.monotonic()
                if time.monotonic() - healthy_since >= self.healthy_resume_seconds:
                    self.conn.execute("UPDATE repair_batches SET state='running',paused_reason='' WHERE id=?", (self.batch_id,))
                    self.conn.commit()
                    record_event(self.conn, self.batch_id, "info", "resource_guard_resumed", "continuous healthy window passed")
                    return True
            self.sleep(self.guard_poll_seconds)
        return False

    def interruptible_page_evidence(self, page: sqlite3.Row, suspects: Sequence[Suspect],
                                    *, deadline: float) -> list[tuple[bytes | None, str, dict[str, Any]]]:
        raw = str(page["baseline_raw"])
        visible = canonical_visible(raw)
        eligible: list[tuple[int, Suspect]] = []
        for index, item in enumerate(suspects):
            if not item.left_anchor or not item.right_anchor:
                continue
            anchor_unique = visible.count(item.left_anchor) == 1 and visible.count(item.right_anchor) == 1
            authority, _evidence = authority_for(page, item, self.authority)
            if protected_reasons(
                item, page_raw=raw, crop_readable=None, anchor_unique=anchor_unique,
                authority_value=authority,
            ):
                continue
            eligible.append((index, item))
        empty: list[tuple[bytes | None, str, dict[str, Any]]] = [(None, "", {}) for _ in suspects]
        if not eligible:
            return empty
        temp_parent = self.output_root / "batches" / self.batch_id / "tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"page-{int(page['page_id'])}-", dir=temp_parent))
        input_path = work / "suspects.json"
        atomic_write(input_path, json.dumps([item.__dict__ for _index, item in eligible], ensure_ascii=False))
        command = [
            sys.executable, str(Path(__file__).resolve()), "local-page",
            "--pdf", str(resolve_pdf(self.project_root, str(page["source_file"]))),
            "--pdf-page", str(int(page["pdf_page"])), "--suspects", str(input_path),
            "--output-dir", str(work),
        ]
        env = os.environ.copy()
        for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            env[name] = "1"
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        last_guard = 0.0
        try:
            while process.poll() is None:
                if time.time() >= deadline:
                    process.terminate()
                    raise PauseRequested("夜间运行窗口结束")
                if self.guard.interactive_render_active():
                    process.terminate()
                    raise PauseRequested("前台开始渲染PDF，后台OCR已让行")
                if time.monotonic() - last_guard >= self.guard_poll_seconds:
                    reasons, metrics = self.guard.reasons()
                    self._metrics(metrics)
                    last_guard = time.monotonic()
                    if reasons:
                        process.terminate()
                        raise PauseRequested("; ".join(reasons))
                self.sleep(0.25)
            if process.returncode != 0:
                detail = (process.stderr.read(2000) if process.stderr else b"").decode("utf-8", "replace")
                raise AuditError("local OCR child failed: " + redact(detail, self.secrets))
            payload = json.loads((work / "result.json").read_text(encoding="utf-8"))
            worker_output: list[tuple[bytes | None, str, dict[str, Any]]] = []
            for item in payload.get("results") or []:
                crop_name = str(item.get("crop") or "")
                crop = (work / crop_name).read_bytes() if crop_name else None
                worker_output.append((crop, canonical_visible(str(item.get("rapid_value") or "")),
                                      item.get("metadata") if isinstance(item.get("metadata"), dict) else {}))
            if len(worker_output) != len(eligible):
                raise AuditError("local OCR child returned incomplete results")
            for (original_index, _suspect), item in zip(eligible, worker_output):
                empty[original_index] = item
            return empty
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            shutil.rmtree(work, ignore_errors=True)

    def _insert_issue(self, page: sqlite3.Row, suspect: Suspect, *, proposed: str, rapid: str, mimo: str,
                      deepseek_result: dict[str, Any], protected: Sequence[str], authority: str,
                      authority_evidence: str, crop_rel: str, evidence: dict[str, Any], automatic: bool) -> None:
        now = utc_now()
        existing = self.conn.execute(
            "SELECT status FROM repair_issues WHERE batch_id=? AND page_id=? AND error_type=? "
            "AND left_anchor=? AND right_anchor=? AND before_text=?",
            (self.batch_id, int(page["page_id"]), suspect.error_type, suspect.left_anchor,
             suspect.right_anchor, suspect.before),
        ).fetchone()
        if existing is not None and str(existing["status"]) in {"approved", "rejected", "deferred", "applied"}:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO repair_issues(batch_id,page_id,book,volume,source_file,pdf_page,printed_page,"
            "baseline_hash,error_type,before_text,proposed_text,left_anchor,right_anchor,rapid_value,mimo_value,"
            "deepseek_decision,deepseek_risks,authority_value,authority_evidence,confidence,status,protected_reasons,"
            "evidence_crop,evidence_json,detected_at,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, int(page["page_id"]), str(page["book"]), int(page["volume"]), str(page["source_file"]),
             int(page["pdf_page"]), str(page["printed_page"]), str(page["baseline_hash"]), suspect.error_type,
             suspect.before, proposed, suspect.left_anchor, suspect.right_anchor, rapid, mimo,
             str(deepseek_result.get("decision") or ""), json_text(deepseek_result.get("risks") or []),
             authority, authority_evidence, "high" if automatic else "review",
             "automatic_candidate" if automatic else "manual_review", json_text(list(protected)), crop_rel,
             json_text(evidence), now, now),
        )
        self.conn.commit()

    def process_page(self, page: sqlite3.Row) -> None:
        raw = str(page["baseline_raw"])
        suspects = detect_suspects(
            raw,
            known_terms=authority_terms_for(
                self.known_terms, str(page["book"]), int(page["volume"]), int(page["pdf_page"]),
            ),
        )
        if not suspects:
            self.conn.execute("UPDATE repair_pages SET status='no_issue',completed_at=? WHERE batch_id=? AND page_id=?",
                              (utc_now(), self.batch_id, int(page["page_id"])))
            self.conn.commit()
            return
        local_evidence = self.interruptible_page_evidence(
            page, suspects, deadline=float(getattr(self, "current_deadline", time.time() + 3600))
        )
        for index, suspect in enumerate(suspects, 1):
                authority, authority_evidence = authority_for(page, suspect, self.authority)
                anchor_unique = bool(
                    suspect.left_anchor and suspect.right_anchor
                    and canonical_visible(raw).count(suspect.left_anchor) == 1
                    and canonical_visible(raw).count(suspect.right_anchor) == 1
                )
                crop, rapid_value, crop_meta = local_evidence[index - 1] if anchor_unique else (None, "", {})
                crop_rel = ""
                if crop:
                    crop_rel = f"evidence/{self.batch_id}/{int(page['page_id'])}-{index}.png"
                    atomic_write(self.output_root / crop_rel, crop)
                protected = protected_reasons(
                    suspect, page_raw=raw, crop_readable=bool(crop), anchor_unique=anchor_unique,
                    authority_value=authority,
                )
                mimo_value = ""
                mimo_result: dict[str, Any] = {}
                mimo_audit: dict[str, Any] = {}
                deepseek_result: dict[str, Any] = {"decision": "manual", "proposed": "", "risks": []}
                deepseek_audit: dict[str, Any] = {}
                if crop and not protected:
                    raw_dir = self.output_root / "batches" / self.batch_id / "raw"
                    mimo_key = sha256_text("\0".join((
                        hashlib.sha256(crop).hexdigest(), suspect.left_anchor, suspect.right_anchor,
                        str(getattr(self.mimo, "model", MIMO_MODEL)),
                    )))
                    mimo_cache = raw_dir / "mimo" / f"{mimo_key}.json"
                    if mimo_cache.is_file():
                        cached = json.loads(mimo_cache.read_text(encoding="utf-8"))
                        mimo_result = cached.get("result") if isinstance(cached.get("result"), dict) else {}
                        mimo_audit = cached.get("audit") if isinstance(cached.get("audit"), dict) else {}
                    else:
                        try:
                            mimo_result, mimo_audit = self.mimo.transcribe_crop(
                                crop, left=suspect.left_anchor, right=suspect.right_anchor
                            )
                            atomic_write(mimo_cache, json.dumps(
                                {"result": mimo_result, "audit": mimo_audit}, ensure_ascii=False, indent=2
                            ))
                            self.conn.execute("UPDATE repair_batches SET api_calls_mimo=api_calls_mimo+1 WHERE id=?", (self.batch_id,))
                            self.conn.commit()
                        except SubmissionUncertain as exc:
                            protected.append("MiMo提交结果不确定，禁止自动重发")
                            mimo_audit = {"error": redact(exc, self.secrets), "submission_uncertain": True}
                        except ProviderError as exc:
                            protected.append("MiMo不可用或输出无效")
                            mimo_audit = {"error": redact(exc, self.secrets)}
                    mimo_value = canonical_visible(str(mimo_result.get("between") or ""))
                    if mimo_result:
                        deep_input = canonical_json({
                            "baseline": suspect.before, "rapid": rapid_value, "mimo": mimo_value,
                            "left": suspect.left_anchor, "right": suspect.right_anchor,
                            "protected": protected, "authority": authority,
                            "model": str(getattr(self.deepseek, "model", DEEPSEEK_MODEL)),
                        })
                        deep_cache = raw_dir / "deepseek" / f"{sha256_text(deep_input)}.json"
                        if deep_cache.is_file():
                            cached = json.loads(deep_cache.read_text(encoding="utf-8"))
                            deepseek_result = cached.get("result") if isinstance(cached.get("result"), dict) else deepseek_result
                            deepseek_audit = cached.get("audit") if isinstance(cached.get("audit"), dict) else {}
                        else:
                            try:
                                deepseek_result, deepseek_audit = self.deepseek.adjudicate(
                                    baseline=suspect.before, rapid=rapid_value, mimo=mimo_value,
                                    left=suspect.left_anchor, right=suspect.right_anchor,
                                    protected=protected, authority=authority,
                                )
                                atomic_write(deep_cache, json.dumps(
                                    {"result": deepseek_result, "audit": deepseek_audit}, ensure_ascii=False, indent=2
                                ))
                                self.conn.execute("UPDATE repair_batches SET api_calls_deepseek=api_calls_deepseek+1 WHERE id=?", (self.batch_id,))
                                self.conn.commit()
                            except SubmissionUncertain as exc:
                                protected.append("DeepSeek提交结果不确定，禁止自动重发")
                                deepseek_result = {"decision": "manual", "proposed": "", "risks": [str(exc)]}
                                deepseek_audit = {"error": redact(exc, self.secrets), "submission_uncertain": True}
                            except ProviderError as exc:
                                protected.append("DeepSeek不可用或输出无效")
                                deepseek_result = {"decision": "manual", "proposed": "", "risks": [str(exc)]}
                                deepseek_audit = {"error": redact(exc, self.secrets)}
                automatic = strict_eligibility(
                    baseline=suspect.before, rapid=rapid_value, mimo=mimo_value,
                    deepseek=deepseek_result, protected=protected, authority=authority,
                    mimo_readable=mimo_result.get("readable") is True,
                    layout_risk=mimo_result.get("layout_risk") is True,
                )
                # DeepSeek is an adjudicator only.  Even for manual review it
                # cannot introduce a third spelling into the editable proposal.
                proposed = rapid_value if rapid_value and rapid_value == mimo_value else (authority or mimo_value or rapid_value)
                evidence = {
                    "pipeline_version": PIPELINE_VERSION, "detector_version": DETECTOR_VERSION,
                    "status_history": ["detected", "ai_reviewed",
                                       "automatic_candidate" if automatic else "manual_review"],
                    "reason": suspect.reason, "crop_only_external": True,
                    "crop_sha256": hashlib.sha256(crop).hexdigest() if crop else "",
                    "crop_bytes": len(crop or b""), "crop": crop_meta,
                    "mimo": mimo_audit, "deepseek": deepseek_audit,
                }
                self._insert_issue(page, suspect, proposed=proposed, rapid=rapid_value, mimo=mimo_value,
                                   deepseek_result=deepseek_result, protected=protected, authority=authority,
                                   authority_evidence=authority_evidence, crop_rel=crop_rel,
                                   evidence=evidence, automatic=automatic)
        self.conn.execute(
            "UPDATE repair_pages SET status='reviewed',completed_at=? WHERE batch_id=? AND page_id=?",
            (utc_now(), self.batch_id, int(page["page_id"])),
        )
        self.conn.commit()

    def run(self, *, page_budget: int, deadline: float) -> dict[str, Any]:
        batch = self.conn.execute("SELECT * FROM repair_batches WHERE id=?", (self.batch_id,)).fetchone()
        if batch is None:
            raise AuditError("repair batch not found")
        if not self.guard.baseline_ms:
            self.guard.establish_baseline(25)
        if not self.wait_healthy(deadline=deadline):
            remaining = int(self.conn.execute(
                "SELECT COUNT(*) FROM repair_pages WHERE batch_id=? AND status IN ('planned','processing','failed')",
                (self.batch_id,),
            ).fetchone()[0])
            return {"batch_id": self.batch_id, "processed": 0, "remaining": remaining}
        try:
            current_source_hash = checked_sha256_file(self.database, self.guarded_checkpoint)
        except PauseRequested:
            remaining = int(self.conn.execute(
                "SELECT COUNT(*) FROM repair_pages WHERE batch_id=? AND status IN ('planned','processing','failed')",
                (self.batch_id,),
            ).fetchone()[0])
            return {"batch_id": self.batch_id, "processed": 0, "remaining": remaining}
        if current_source_hash != str(batch["source_database_sha256"]):
            self.conn.execute("UPDATE repair_batches SET state='stale',paused_reason='线上数据库哈希已变化' WHERE id=?",
                              (self.batch_id,))
            self.conn.commit()
            raise AuditError("live corpus changed; batch must be replanned")
        recovered = int(self.conn.execute(
            "SELECT COUNT(*) FROM repair_pages WHERE batch_id=? AND status='processing'", (self.batch_id,)
        ).fetchone()[0])
        if recovered:
            self.conn.execute(
                "UPDATE repair_pages SET status='planned',error='recovered after interrupted worker' "
                "WHERE batch_id=? AND status='processing'", (self.batch_id,),
            )
            self.conn.commit()
            record_event(self.conn, self.batch_id, "warning", "interrupted_pages_recovered", str(recovered))
        self.conn.execute("UPDATE repair_batches SET state='running',paused_reason='' WHERE id=?", (self.batch_id,))
        self.conn.commit()
        processed = 0
        self.current_deadline = deadline
        while time.time() < deadline and (page_budget <= 0 or processed < page_budget):
            page = self.conn.execute(
                "SELECT * FROM repair_pages WHERE batch_id=? AND "
                "(status='planned' OR (status='failed' AND attempts<3)) ORDER BY plan_order LIMIT 1",
                (self.batch_id,),
            ).fetchone()
            if page is None:
                break
            if not self.wait_healthy(deadline=deadline):
                break
            try:
                self.conn.execute(
                    "UPDATE repair_pages SET status='processing',attempts=attempts+1 WHERE batch_id=? AND page_id=?",
                    (self.batch_id, int(page["page_id"])),
                )
                self.conn.commit()
                self.process_page(page)
            except PauseRequested as exc:
                with self.conn:
                    self.conn.execute(
                        "UPDATE repair_pages SET status='planned',error=? WHERE batch_id=? AND page_id=?",
                        (str(exc)[:1000], self.batch_id, int(page["page_id"])),
                    )
                    self.conn.execute(
                        "UPDATE repair_batches SET state='paused',paused_reason=?,last_checkpoint_at=? WHERE id=?",
                        (str(exc)[:1000], utc_now(), self.batch_id),
                    )
                record_event(self.conn, self.batch_id, "warning", "foreground_yield", str(exc))
                if not self.wait_healthy(deadline=deadline):
                    break
                continue
            except FatalProviderError:
                raise
            except ProviderError as exc:
                self.conn.execute(
                    "UPDATE repair_pages SET status='planned',error=? WHERE batch_id=? AND page_id=?",
                    (redact(exc, self.secrets), self.batch_id, int(page["page_id"])),
                )
                self.conn.commit()
                record_event(self.conn, self.batch_id, "warning", "provider_deferred", redact(exc, self.secrets))
                break
            except Exception as exc:
                self.conn.execute(
                    "UPDATE repair_pages SET status='failed',error=?,completed_at=? WHERE batch_id=? AND page_id=?",
                    (redact(exc, self.secrets), utc_now(), self.batch_id, int(page["page_id"])),
                )
                self.conn.commit()
                record_event(self.conn, self.batch_id, "warning", "page_failed",
                             f"page_id={int(page['page_id'])}: {redact(exc, self.secrets)}")
                break
            processed += 1
            self.conn.execute(
                "UPDATE repair_batches SET scanned_pages=(SELECT COUNT(*) FROM repair_pages WHERE batch_id=? AND status NOT IN ('planned','processing')) ,"
                "flagged_pages=(SELECT COUNT(DISTINCT page_id) FROM repair_issues WHERE batch_id=?),last_checkpoint_at=? WHERE id=?",
                (self.batch_id, self.batch_id, utc_now(), self.batch_id),
            )
            self.conn.commit()
        remaining = int(self.conn.execute(
            "SELECT COUNT(*) FROM repair_pages WHERE batch_id=? AND status IN ('planned','processing','failed')", (self.batch_id,)
        ).fetchone()[0])
        if remaining == 0:
            unresolved = int(self.conn.execute(
                "SELECT COUNT(*) FROM repair_issues WHERE batch_id=? AND status='manual_review'",
                (self.batch_id,),
            ).fetchone()[0])
            state = "awaiting_review" if unresolved else "scanned"
            self.conn.execute("UPDATE repair_batches SET state=?,completed_at=? WHERE id=?",
                              (state, utc_now(), self.batch_id))
        else:
            hard_failed = int(self.conn.execute(
                "SELECT COUNT(*) FROM repair_pages WHERE batch_id=? AND status='failed' AND attempts>=3",
                (self.batch_id,),
            ).fetchone()[0])
            current_state = str(self.conn.execute(
                "SELECT state FROM repair_batches WHERE id=?", (self.batch_id,)
            ).fetchone()[0])
            if hard_failed:
                self.conn.execute("UPDATE repair_batches SET state='failed' WHERE id=?", (self.batch_id,))
            elif current_state != "paused":
                self.conn.execute("UPDATE repair_batches SET state='checkpointed' WHERE id=?", (self.batch_id,))
        self.conn.commit()
        return {"batch_id": self.batch_id, "processed": processed, "remaining": remaining}


def build_candidate(*, database: Path, output_root: Path, batch_id: str,
                    checkpoint: Callable[[], None] | None = None) -> dict[str, Any]:
    db_path = output_root / "review.sqlite3"
    with _connect(db_path) as review:
        batch = review.execute("SELECT * FROM repair_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None or str(batch["state"]) not in {"scanned", "candidate_ready"}:
            raise AuditError("batch has not completed scanning")
        unresolved = int(review.execute(
            "SELECT COUNT(*) FROM repair_issues WHERE batch_id=? AND status='manual_review'", (batch_id,)
        ).fetchone()[0])
        if unresolved:
            raise AuditError(f"batch still has {unresolved} manual review items")
        source_hash = checked_sha256_file(database, checkpoint)
        if source_hash != str(batch["source_database_sha256"]):
            raise AuditError("live corpus hash changed before candidate build")
        batch_dir = output_root / "batches" / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        candidate = batch_dir / "candidate-corpus.sqlite"
        building = batch_dir / "candidate-corpus.building.sqlite"
        for path in (building, Path(str(building) + "-wal"), Path(str(building) + "-shm")):
            path.unlink(missing_ok=True)
        source = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(building)
        try:
            source.backup(
                target, pages=256, sleep=0.05,
                progress=((lambda _status, _remaining, _total: checkpoint()) if checkpoint else None),
            )
        except Exception:
            target.close()
            building.unlink(missing_ok=True)
            raise
        finally:
            source.close()
        rows = review.execute(
            "SELECT i.*,p.baseline_raw FROM repair_issues i JOIN repair_pages p "
            "ON p.batch_id=i.batch_id AND p.page_id=i.page_id "
            "WHERE i.batch_id=? AND i.status IN ('automatic_candidate','approved') ORDER BY i.page_id,i.id",
            (batch_id,),
        ).fetchall()
        selected_ids = {int(row["page_id"]) for row in rows}
        counts_before = {name: int(target.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                         for name in ("pages", "toc_entries", "toc") if target.execute(
                             "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()}
        unchanged_before = selected_row_fingerprint(target, selected_ids)
        identity_before = identity_fingerprint(target)
        grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[int(row["page_id"])].append(row)
        applied_issue_ids: list[int] = []
        for page_id, issues in grouped.items():
            current = target.execute("SELECT raw_text FROM pages WHERE id=?", (page_id,)).fetchone()
            if current is None or sha256_text(str(current[0])) != str(issues[0]["baseline_hash"]):
                raise AuditError(f"baseline mismatch for page {page_id}")
            raw = str(current[0])
            for issue in issues:
                raw = replace_canonical_segment(
                    raw, str(issue["left_anchor"]), str(issue["before_text"]),
                    str(issue["right_anchor"]), str(issue["proposed_text"]),
                )
                applied_issue_ids.append(int(issue["id"]))
            target.execute("UPDATE pages SET raw_text=?,normalized_text=? WHERE id=?",
                           (raw, production_normalize(raw), page_id))
        target.commit()
        repair_checks: list[dict[str, Any]] = []
        for issue in rows:
            updated = target.execute(
                "SELECT raw_text,normalized_text FROM pages WHERE id=?", (int(issue["page_id"]),)
            ).fetchone()
            visible = canonical_visible(str(updated[0])) if updated else ""
            expected_probe = canonical_visible(
                str(issue["left_anchor"]) + str(issue["proposed_text"]) + str(issue["right_anchor"])
            )
            old_probe = canonical_visible(
                str(issue["left_anchor"]) + str(issue["before_text"]) + str(issue["right_anchor"])
            )
            repair_checks.append({
                "issue_id": int(issue["id"]),
                "page_id": int(issue["page_id"]),
                "replacement_present_once": bool(expected_probe and visible.count(expected_probe) == 1),
                "old_anchored_value_absent": bool(not old_probe or old_probe not in visible),
                "normalized_matches_production": bool(
                    updated and str(updated[1]) == production_normalize(str(updated[0]))
                ),
            })
        quick_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
        counts_after = {name: int(target.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in counts_before}
        unchanged_after = selected_row_fingerprint(target, selected_ids)
        identity_after = identity_fingerprint(target)
        target.close()
        stable_source = checked_sha256_file(database, checkpoint) == source_hash
        repair_checks_ok = all(
            item["replacement_present_once"]
            and item["old_anchored_value_absent"]
            and item["normalized_matches_production"]
            for item in repair_checks
        )
        gates_ok = bool(
            quick_check == "ok" and counts_before == counts_after and unchanged_before == unchanged_after
            and identity_before == identity_after and stable_source and repair_checks_ok
        )
        validation = {
            "gates_ok": gates_ok, "quick_check": quick_check,
            "counts_before": counts_before, "counts_after": counts_after,
            "unchanged_rows_match": unchanged_before == unchanged_after,
            "identity_fields_modified": 0 if identity_before == identity_after else 1,
            "source_database_sha256": source_hash, "source_hash_stable": stable_source,
            "repair_checks_ok": repair_checks_ok, "repair_checks": repair_checks,
            "applied_issue_ids": applied_issue_ids, "production_write_count": 0,
        }
        if not gates_ok:
            building.unlink(missing_ok=True)
            raise AuditError("candidate database failed regression gates")
        os.replace(building, candidate)
        candidate_hash = checked_sha256_file(candidate, checkpoint)
        validation["candidate_database_sha256"] = candidate_hash
        atomic_write(batch_dir / "candidate-validation.json", json.dumps(validation, ensure_ascii=False, indent=2))
        review.execute(
            "UPDATE repair_batches SET state='candidate_ready',candidate_database=?,candidate_database_sha256=?,"
            "validation_json=?,completed_at=? WHERE id=?",
            (str(candidate), candidate_hash, json_text(validation), utc_now(), batch_id),
        )
        review.commit()
        record_event(review, batch_id, "info", "candidate_ready", f"applied={len(applied_issue_ids)}")
    generate_batch_report(output_root, batch_id)
    return validation


def generate_batch_report(output_root: Path, batch_id: str) -> dict[str, Any]:
    db_path = output_root / "review.sqlite3"
    with _connect(db_path) as conn:
        batch_row = conn.execute("SELECT * FROM repair_batches WHERE id=?", (batch_id,)).fetchone()
        if batch_row is None:
            raise AuditError("batch not found")
        batch = dict(batch_row)
        try:
            batch["validation"] = json.loads(str(batch.pop("validation_json") or "{}"))
        except json.JSONDecodeError:
            batch["validation"] = {}
        issue_counts = dict(conn.execute(
            "SELECT status,COUNT(*) FROM repair_issues WHERE batch_id=? GROUP BY status", (batch_id,)
        ).fetchall())
        page_counts = dict(conn.execute(
            "SELECT status,COUNT(*) FROM repair_pages WHERE batch_id=? GROUP BY status", (batch_id,)
        ).fetchall())
        issues = [dict(row) for row in conn.execute(
            "SELECT id,page_id,book,volume,source_file,pdf_page,printed_page,error_type,before_text,"
            "proposed_text,rapid_value,mimo_value,deepseek_decision,status,protected_reasons,evidence_crop "
            "FROM repair_issues WHERE batch_id=? ORDER BY id", (batch_id,)
        )]
        events = [dict(row) for row in conn.execute(
            "SELECT at,level,event,detail FROM repair_events WHERE batch_id=? ORDER BY id", (batch_id,)
        )]
    payload = {
        "schema_version": 1, "pipeline_version": PIPELINE_VERSION,
        "production_write_count": 0, "batch": batch,
        "page_counts": page_counts, "issue_counts": issue_counts,
        "issues": issues, "events": events,
    }
    batch_dir = output_root / "batches" / batch_id
    atomic_write(batch_dir / "report.json", json.dumps(payload, ensure_ascii=False, indent=2))
    rows = "".join(
        "<tr><td>{book} 第{volume}卷<br>PDF {pdf_page}</td><td>{kind}</td>"
        "<td><del>{before}</del> → <ins>{after}</ins></td><td>{status}</td><td>{protected}</td></tr>".format(
            book=html.escape(str(item["book"])), volume=int(item["volume"]), pdf_page=int(item["pdf_page"]),
            kind=html.escape(str(item["error_type"])), before=html.escape(str(item["before_text"])),
            after=html.escape(str(item["proposed_text"])), status=html.escape(str(item["status"])),
            protected=html.escape(str(item["protected_reasons"])),
        ) for item in issues
    )
    document = f"""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>引文修复批次 {html.escape(batch_id)}</title>
<style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:1200px;margin:32px auto;padding:0 20px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:8px;vertical-align:top}}del{{background:#fee}}ins{{background:#dfeddf;text-decoration:none}}</style>
<h1>引文修复批次 {html.escape(batch_id)}</h1><p>状态：<b>{html.escape(str(batch['state']))}</b>；扫描 {int(batch['scanned_pages'])}/{int(batch['planned_pages'])} 页；生产写入：0。</p>
<p>问题状态：{html.escape(json.dumps(issue_counts, ensure_ascii=False))}</p>
<table><thead><tr><th>页面</th><th>类型</th><th>建议</th><th>状态</th><th>保护原因</th></tr></thead><tbody>{rows}</tbody></table></html>"""
    atomic_write(batch_dir / "report.html", document)
    return payload


def current_or_new_batch(*, database: Path, project_root: Path, output_root: Path, phase: int,
                         feedback_db: Path | None = None,
                         checkpoint: Callable[[], None] | None = None) -> str:
    db = init_db(output_root / "review.sqlite3")
    if phase == 0:
        with _connect(db) as conn:
            for candidate_phase in (1, 2, 3):
                latest = conn.execute(
                    "SELECT id,state FROM repair_batches WHERE phase=? ORDER BY created_at DESC LIMIT 1",
                    (candidate_phase,),
                ).fetchone()
                if latest is None or str(latest["state"]) != "applied":
                    phase = candidate_phase
                    break
            else:
                raise AuditError("all corpus repair phases are complete")
    with _connect(db) as conn:
        row = conn.execute(
            "SELECT id,state FROM repair_batches WHERE phase=? ORDER BY created_at DESC LIMIT 1", (phase,)
        ).fetchone()
        if row and str(row["state"]) not in {"stale", "applied"}:
            return str(row["id"])
    return create_batch(database=database, project_root=project_root, output_root=output_root,
                        phase=phase, feedback_db=feedback_db, checkpoint=checkpoint)


def seconds_remaining_in_window(*, start: str, end: str, timezone_name: str,
                                now: datetime | None = None) -> float:
    """Return remaining seconds only while inside the configured local window."""
    zone = ZoneInfo(timezone_name)
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    start_hour, start_minute = (int(part) for part in start.split(":", 1))
    end_hour, end_minute = (int(part) for part in end.split(":", 1))
    begins = current.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    finishes = current.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if finishes <= begins:
        raise ValueError("overnight repair windows are not supported")
    if current < begins or current >= finishes:
        return 0.0
    return (finishes - current).total_seconds()


def nightly(args: argparse.Namespace) -> dict[str, Any]:
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    window_remaining = seconds_remaining_in_window(
        start=args.window_start, end=args.window_end, timezone_name=args.timezone,
    )
    if window_remaining <= 0:
        return {"batch_id": "", "state": "outside_window", "processed": 0}
    planning_deadline = time.time() + min(float(args.max_runtime_hours) * 3600, window_remaining)
    planning_guard = HealthGuard(args.health_base, output_root)
    last_planning_probe = 0.0

    def planning_checkpoint() -> None:
        nonlocal last_planning_probe
        if time.time() >= planning_deadline:
            raise PauseRequested("夜间运行窗口结束")
        if time.monotonic() - last_planning_probe < args.guard_poll_seconds:
            return
        if not planning_guard.baseline_ms:
            planning_guard.establish_baseline(25)
        reasons, _metrics = planning_guard.reasons()
        last_planning_probe = time.monotonic()
        if reasons:
            raise PauseRequested("; ".join(reasons))

    try:
        batch_id = current_or_new_batch(
            database=Path(args.database), project_root=Path(args.project_root), output_root=output_root,
            phase=args.phase, feedback_db=Path(args.feedback_db) if args.feedback_db else None,
            checkpoint=planning_checkpoint,
        )
    except PauseRequested as exc:
        atomic_write(output_root / "planning-paused.json", json.dumps(
            {"state": "planning_paused", "reason": str(exc), "at": utc_now()}, ensure_ascii=False, indent=2,
        ))
        return {"batch_id": "", "state": "planning_paused", "processed": 0, "reason": str(exc)}
    (output_root / "planning-paused.json").unlink(missing_ok=True)
    with _connect(output_root / "review.sqlite3") as conn:
        batch = conn.execute("SELECT * FROM repair_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise AuditError("batch not found")
        batch_state = str(batch["state"])
        if batch_state in {"candidate_ready", "confirmed", "failed"}:
            return {"batch_id": batch_id, "state": batch_state, "processed": 0}
        if batch_state == "awaiting_review":
            unresolved = int(conn.execute(
                "SELECT COUNT(*) FROM repair_issues WHERE batch_id=? AND status='manual_review'", (batch_id,)
            ).fetchone()[0])
            if unresolved:
                return {"batch_id": batch_id, "state": batch_state, "processed": 0,
                        "manual_review": unresolved}
            conn.execute("UPDATE repair_batches SET state='scanned' WHERE id=?", (batch_id,))
            conn.commit()
            batch_state = "scanned"
        if batch_state == "scanned":
            validation = build_candidate(
                database=Path(args.database), output_root=output_root, batch_id=batch_id,
                checkpoint=planning_checkpoint,
            )
            return {"batch_id": batch_id, "state": "candidate_ready", "processed": 0,
                    "validation": validation}
        canary_stage = int(batch["canary_stage"] or 0)
        canary_progress = int(batch["canary_progress"] or 0)
        canary_unsafe = bool(batch["canary_unsafe"] or 0)
        canary_active = canary_stage < len(CANARY_BUDGETS)
        canary_target = CANARY_BUDGETS[canary_stage] if canary_active else 0
        budget = max(0, canary_target - canary_progress) if canary_active else 0
        record_event(
            conn, batch_id, "info", "night_started",
            f"canary_stage={canary_stage + 1 if canary_active else 'complete'} budget={budget or 'remaining'}",
        )
        night_event_id = int(conn.execute("SELECT MAX(id) FROM repair_events WHERE batch_id=?", (batch_id,)).fetchone()[0])
    fresh_window_remaining = seconds_remaining_in_window(
        start=args.window_start, end=args.window_end, timezone_name=args.timezone,
    )
    if fresh_window_remaining <= 0:
        return {"batch_id": batch_id, "state": "outside_window", "processed": 0}
    deadline = time.time() + min(float(args.max_runtime_hours) * 3600, fresh_window_remaining)
    deepseek_key, deepseek_base_url, deepseek_model = resolve_deepseek_runtime()
    runner = RepairRunner(
        database=Path(args.database), project_root=Path(args.project_root), output_root=output_root,
        batch_id=batch_id,
        mimo=MimoClient(os.environ.get("MIMO_API_KEY", ""),
                        base_url=os.environ.get("MIMO_BASE_URL", MIMO_BASE_URL),
                        model=os.environ.get("MIMO_MODEL", MIMO_MODEL)),
        deepseek=DeepSeekClient(deepseek_key, base_url=deepseek_base_url, model=deepseek_model),
        health_base=args.health_base, guard_poll_seconds=args.guard_poll_seconds,
        healthy_resume_seconds=args.healthy_resume_seconds, guard=planning_guard,
    )
    try:
        result = runner.run(page_budget=budget, deadline=deadline)
    finally:
        runner.close()
    if canary_active:
        with _connect(output_root / "review.sqlite3") as conn:
            unsafe_events = int(conn.execute(
                "SELECT COUNT(*) FROM repair_events WHERE batch_id=? AND id>? AND level IN ('warning','error')",
                (batch_id, night_event_id),
            ).fetchone()[0])
            progress = canary_progress + int(result.get("processed") or 0)
            unsafe = canary_unsafe or unsafe_events > 0
            if progress >= canary_target:
                if unsafe:
                    conn.execute(
                        "UPDATE repair_batches SET canary_progress=0,canary_unsafe=0 WHERE id=?", (batch_id,),
                    )
                    conn.commit()
                    record_event(conn, batch_id, "warning", "canary_retry_required",
                                 f"stage={canary_stage + 1} target={canary_target}")
                else:
                    conn.execute(
                        "UPDATE repair_batches SET canary_stage=canary_stage+1,canary_progress=0,canary_unsafe=0 WHERE id=?",
                        (batch_id,),
                    )
                    conn.commit()
                    record_event(conn, batch_id, "info", "canary_passed",
                                 f"stage={canary_stage + 1} pages={canary_target}")
            else:
                conn.execute(
                    "UPDATE repair_batches SET canary_progress=?,canary_unsafe=? WHERE id=?",
                    (progress, 1 if unsafe else 0, batch_id),
                )
                conn.commit()
    if result["remaining"] == 0:
        with _connect(output_root / "review.sqlite3") as conn:
            state = str(conn.execute("SELECT state FROM repair_batches WHERE id=?", (batch_id,)).fetchone()[0])
        result["state"] = state
        if state == "scanned":
            result["validation"] = build_candidate(
                database=Path(args.database), output_root=output_root, batch_id=batch_id,
                checkpoint=planning_checkpoint,
            )
    generate_batch_report(output_root, batch_id)
    return result


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", default="/opt/marx-search/data/corpus.sqlite")
    parser.add_argument("--project-root", default="/opt/marx-search")
    parser.add_argument("--output-root", default="/home/data/marx-search-corpus-repair")
    parser.add_argument("--feedback-db", default="/var/www/.marx_search_full/feedback.sqlite3")
    parser.add_argument("--phase", type=int, choices=(0, 1, 2, 3), default=0,
                        help="0=按阶段顺序自动选择，1/2/3=固定阶段")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="全站语料引文文字自动修复（只写候选区）")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    add_common(plan)
    plan.add_argument("--batch-id", default="")
    night = sub.add_parser("nightly")
    add_common(night)
    night.add_argument("--health-base", default="http://127.0.0.1:8000")
    night.add_argument("--window-start", default="01:00")
    night.add_argument("--window-end", default="06:00")
    night.add_argument("--timezone", default="Asia/Shanghai")
    night.add_argument("--max-runtime-hours", type=float, default=5.0)
    night.add_argument("--guard-poll-seconds", type=float, default=10.0)
    night.add_argument("--healthy-resume-seconds", type=float, default=900.0)
    build = sub.add_parser("build-candidate")
    add_common(build)
    build.add_argument("--batch-id", required=True)
    status = sub.add_parser("status")
    status.add_argument("--output-root", default="/home/data/marx-search-corpus-repair")
    status.add_argument("--compact", action="store_true")
    confirm = sub.add_parser("confirm")
    confirm.add_argument("--output-root", default="/home/data/marx-search-corpus-repair")
    confirm.add_argument("--batch-id", required=True)
    confirm.add_argument("--actor", required=True)
    local_page = sub.add_parser("local-page", help=argparse.SUPPRESS)
    local_page.add_argument("--pdf", type=Path, required=True)
    local_page.add_argument("--pdf-page", type=int, required=True)
    local_page.add_argument("--suspects", type=Path, required=True)
    local_page.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "local-page":
            result = local_page_worker(pdf_path=args.pdf, pdf_page=args.pdf_page,
                                       suspects_path=args.suspects, output_dir=args.output_dir)
        elif args.command == "plan":
            if args.phase == 0 and not args.batch_id:
                batch_id = current_or_new_batch(
                    database=Path(args.database), project_root=Path(args.project_root),
                    output_root=Path(args.output_root), phase=0,
                    feedback_db=Path(args.feedback_db) if args.feedback_db else None,
                )
            else:
                if args.phase == 0:
                    raise ValueError("--batch-id requires an explicit --phase 1/2/3")
                batch_id = create_batch(
                    database=Path(args.database), project_root=Path(args.project_root),
                    output_root=Path(args.output_root), phase=args.phase, batch_id=args.batch_id,
                    feedback_db=Path(args.feedback_db) if args.feedback_db else None,
                )
            result = {"batch_id": batch_id, "production_write_count": 0}
        elif args.command == "nightly":
            result = nightly(args)
        elif args.command == "build-candidate":
            result = build_candidate(database=Path(args.database), output_root=Path(args.output_root), batch_id=args.batch_id)
        elif args.command == "confirm":
            result = confirm_batch(args.batch_id, actor=args.actor, path=Path(args.output_root) / "review.sqlite3")
        else:
            result = status_snapshot(Path(args.output_root) / "review.sqlite3")
            if args.compact:
                result.pop("batches", None)
                result.pop("recent_events", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except FatalProviderError as exc:
        print(redact(exc), file=sys.stderr)
        return 78
    except Exception as exc:
        print(redact(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
