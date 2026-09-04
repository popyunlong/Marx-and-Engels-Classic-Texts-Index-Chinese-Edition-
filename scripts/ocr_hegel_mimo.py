#!/usr/bin/env python3
"""Use MiMo-V2.5 vision to build faithful page transcripts for Hegel scans.

The API key is read only from ``MIMO_API_KEY``.  It is never written to the
sidecars, audit reports, command line, or logs.  Output is append-only JSONL so
an interrupted multi-hour scan can resume without repeating accepted pages.

Examples::

    python scripts/ocr_hegel_mimo.py --list
    python scripts/ocr_hegel_mimo.py --id hegel-logic-upper --limit 3
    python scripts/ocr_hegel_mimo.py --all --workers 8
    python scripts/ocr_hegel_mimo.py --all --audit-only
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "hegel_volumes.yaml"
OUTPUT_ROOT = ROOT / "data" / "hegel"
AUDIT_ROOT = OUTPUT_ROOT / "audit"
DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_MODEL = "mimo-v2.5"
TRANSCRIPT_SCHEMA = "hegel-mimo-page-v1"
AUDIT_SCHEMA = "hegel-mimo-audit-v1"

SYSTEM_PROMPT = (
    "执行扫描文字 OCR。逐字、逐行转录图片全部可见印刷文字。输出将直接进入学术引文数据库。"
    "不得解释、改写、概括、翻译、补全或重新组织层级；不得把目录转成结构化条目；"
    "不得添加礼貌用语、说明、Markdown 或原图没有的符号；页眉、页码、脚注、外文、点线均须保留，"
    "保持原始阅读顺序和换行。图片没有文字时 text 为空字符串，无法辨认的单字写□。"
    "输出必须严格匹配给定 JSON Schema：只有 text 字符串，不得有任何其他字段。"
)
USER_PROMPT = "将这页黑格尔著作扫描图的全部视觉文字原样 OCR 到 text 字符串。"
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "page_transcription",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}

BLANK_INK = 0.004
LOW_INK = 0.009
META_RE = re.compile(
    r"^(以下是|这是|这是一|您提供|图片中|图中|本页|该页|页面内容|转录如下|OCR|抱歉|无法识别)"
)
# ``*``, ``**`` and ``***`` are real translator-footnote markers in these editions.
# Multiple markers can legitimately occur on one OCR line, so star-based Markdown
# detection is intrinsically ambiguous and would destroy source text.  Strict JSON,
# explicit prompting, heading/fence/HTML checks still catch unambiguous wrappers.
MARKUP_RE = re.compile(r"(^#{1,6}\s|```|</?[A-Za-z][^>]*>)", re.MULTILINE)
_print_lock = threading.Lock()
_thread_local = threading.local()


@dataclass(frozen=True)
class VolumeSpec:
    id: str
    book: str
    volume: int
    display_title: str
    file: str
    pages: int

    @property
    def pdf_path(self) -> Path:
        return ROOT / self.file

    @property
    def sidecar_path(self) -> Path:
        return OUTPUT_ROOT / f"{self.id}.jsonl"


def load_catalog(path: Path = CATALOG_PATH) -> tuple[list[VolumeSpec], str, str]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    model = str(payload.get("model") or DEFAULT_MODEL).strip()
    prompt_version = str(payload.get("prompt_version") or "").strip()
    specs: list[VolumeSpec] = []
    seen_ids: set[str] = set()
    seen_slots: set[tuple[str, int]] = set()
    for raw in payload.get("volumes") or []:
        spec = VolumeSpec(
            id=str(raw.get("id") or "").strip(),
            book=str(raw.get("book") or "").strip(),
            volume=int(raw.get("volume") or 0),
            display_title=str(raw.get("display_title") or "").strip(),
            file=str(raw.get("file") or "").replace("\\", "/").strip(),
            pages=int(raw.get("pages") or 0),
        )
        if not spec.id or not spec.book or spec.volume < 1 or not spec.file or spec.pages < 1:
            raise ValueError(f"invalid Hegel catalogue row: {raw!r}")
        if spec.id in seen_ids:
            raise ValueError(f"duplicate Hegel volume id: {spec.id}")
        if (spec.book, spec.volume) in seen_slots:
            raise ValueError(f"duplicate Hegel book/volume: {spec.book} {spec.volume}")
        seen_ids.add(spec.id)
        seen_slots.add((spec.book, spec.volume))
        specs.append(spec)
    if not specs:
        raise ValueError("Hegel catalogue is empty")
    return specs, model, prompt_version


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_records(path: Path) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            page = int(row["pdf_page"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        records[page] = row
    return records


def parse_transcription_content(content: str) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("response is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"text"} or not isinstance(payload["text"], str):
        raise ValueError("response does not match the one-field transcription schema")
    return payload["text"].replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def degeneration(text: str) -> str:
    compact = re.sub(r"[·．.。…\-—–_~\s]+", "", text)
    if len(compact) >= 400 and re.search(r"(.)\1{40,}", compact):
        return "repeat-character"
    if len(text) >= 600:
        windows = [text[i : i + 24] for i in range(0, len(text) - 24, 24)]
        if windows and len(set(windows)) / len(windows) < 0.22:
            return "repeated-window"
    return ""


def validate_transcript(text: str, *, ink: float, finish_reason: str) -> str:
    if finish_reason == "length":
        return "truncated"
    stripped = text.strip()
    if META_RE.match(stripped):
        return "assistant-meta-text"
    if MARKUP_RE.search(stripped):
        return "unexpected-markup"
    if ink >= LOW_INK and not stripped:
        return "nonblank-page-returned-empty"
    if ink < LOW_INK and len(stripped) > 600:
        return "low-ink-long-text"
    return degeneration(stripped)


def _usage(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("usage") or {}
    details = raw.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
        "reasoning_tokens": int(details.get("reasoning_tokens") or 0),
    }


def _request_body(model: str, image_b64: str, repair_reason: str = "") -> dict[str, Any]:
    instruction = USER_PROMPT
    if repair_reason:
        if "unexpected-markup" in repair_reason:
            instruction += (
                "\n上一次响应误加了 Markdown。本次禁止 # 标题、** 粗体、"
                "代码围栏或 HTML；只把图中实际印刷字符放入 text。"
            )
        elif "not JSON" in repair_reason or "one-field" in repair_reason:
            instruction += "\n上一次响应不是合法的单字段 JSON；本次必须严格遵守 response_format。"
        else:
            instruction += "\n上一次转录未通过质量门禁；请重新核对图片并严格保真输出。"
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ],
        "temperature": 0,
        "max_completion_tokens": 5000,
        "thinking": {"type": "disabled"},
        "response_format": RESPONSE_FORMAT,
        "stream": False,
    }


def call_mimo(
    *, api_key: str, base_url: str, model: str, image_b64: str, ink: float, attempts: int
) -> dict[str, Any]:
    last_error = ""
    try:
        request_timeout = max(20.0, min(float(os.environ.get("MIMO_TIMEOUT_SECONDS", "210")), 210.0))
    except (TypeError, ValueError):
        request_timeout = 210.0
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            body = json.dumps(
                _request_body(model, image_b64, last_error), ensure_ascii=False
            ).encode("utf-8")
            request = urllib.request.Request(
                f"{base_url.rstrip('/')}/chat/completions",
                data=body,
                headers={"Content-Type": "application/json", "api-key": api_key},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            choice = (payload.get("choices") or [{}])[0]
            finish = str(choice.get("finish_reason") or "")
            content = str((choice.get("message") or {}).get("content") or "")
            text = parse_transcription_content(content)
            rejected = validate_transcript(text, ink=ink, finish_reason=finish)
            if rejected:
                last_error = rejected
                raise ValueError(rejected)
            return {
                "text": text,
                "finish_reason": finish,
                "usage": _usage(payload),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "attempt": attempt,
            }
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:240]
            last_error = f"HTTP {exc.code}: {detail}"
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(30.0, 2.0 * attempt**2)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            delay = min(30.0, 2.0 * attempt**2)
        if attempt < attempts:
            time.sleep(delay)
    raise RuntimeError(last_error or "MiMo transcription failed")


def _thread_doc(path: Path):
    import fitz

    docs = getattr(_thread_local, "docs", None)
    if docs is None:
        docs = {}
        _thread_local.docs = docs
    key = str(path)
    doc = docs.get(key)
    if doc is None:
        doc = fitz.open(path)
        docs[key] = doc
    return doc


def render_page(spec: VolumeSpec, page_number: int, target_height: int) -> tuple[float, str]:
    import fitz
    import numpy as np

    page = _thread_doc(spec.pdf_path)[page_number - 1]
    thumb = page.get_pixmap(matrix=fitz.Matrix(0.22, 0.22), colorspace=fitz.csGRAY, alpha=False)
    ink = float((np.frombuffer(thumb.samples, dtype=np.uint8) <= 230).mean())
    if ink < BLANK_INK:
        return ink, ""
    scale = max(1.0, min(6.0, target_height / max(1.0, float(page.rect.height))))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    encoded = base64.b64encode(pix.tobytes("jpeg", jpg_quality=92)).decode("ascii")
    return ink, encoded


def render_sparse_page_regions(
    spec: VolumeSpec, page_number: int, target_height: int, *, binarize: bool = False
) -> list[tuple[float, str]]:
    """Render top text and bottom footnote separately for an explicit sparse-page retry.

    Some exceptionally sparse scans make the vision endpoint stall on the mostly-white
    full canvas.  This opt-in path still uses MiMo for every character, but removes the
    empty middle band.  It must not be enabled for ordinary dense pages.
    """
    import fitz
    import numpy as np

    page = _thread_doc(spec.pdf_path)[page_number - 1]
    width, height = float(page.rect.width), float(page.rect.height)
    clips = (
        fitz.Rect(0, 0, width, height * 0.38),
        fitz.Rect(0, height * 0.42, width, height * 0.66),
        fitz.Rect(0, height * 0.80, width, height),
    )
    rendered: list[tuple[float, str]] = []
    for clip in clips:
        thumb = page.get_pixmap(
            matrix=fitz.Matrix(0.22, 0.22), clip=clip, colorspace=fitz.csGRAY, alpha=False
        )
        ink = float((np.frombuffer(thumb.samples, dtype=np.uint8) <= 230).mean())
        if ink < BLANK_INK:
            continue
        scale = max(1.0, min(6.0, target_height / max(1.0, float(clip.height))))
        pix = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale), clip=clip, colorspace=fitz.csRGB, alpha=False
        )
        if binarize:
            import cv2

            rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            binary = np.where(gray <= 205, 0, 255).astype(np.uint8)
            ok, encoded = cv2.imencode(".jpg", binary, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                raise RuntimeError("could not encode binarized sparse region")
            image_b64 = base64.b64encode(encoded.tobytes()).decode("ascii")
        else:
            image_b64 = base64.b64encode(pix.tobytes("jpeg", jpg_quality=92)).decode("ascii")
        rendered.append(
            (ink, image_b64)
        )
    return rendered


def _atomic_remove_pages(path: Path, pages: set[int]) -> None:
    if not path.exists() or not pages:
        return
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            page = int(json.loads(line).get("pdf_page") or 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            page = 0
        if page not in pages:
            kept.append(line)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    tmp.replace(path)


def scan_volume(
    spec: VolumeSpec,
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt_version: str,
    workers: int,
    target_height: int,
    attempts: int,
    start: int,
    end: int,
    limit: int,
    force_rescan: bool,
    sparse_regions: bool,
    top_region_only: bool,
    body_region_only: bool,
    binarize_sparse: bool,
    dry_run: bool,
) -> dict[str, Any]:
    import fitz

    if not spec.pdf_path.exists():
        raise FileNotFoundError(spec.pdf_path)
    with fitz.open(spec.pdf_path) as doc:
        actual_pages = doc.page_count
    if actual_pages != spec.pages:
        raise ValueError(f"{spec.id}: catalogue pages={spec.pages}, PDF pages={actual_pages}")
    source_sha = sha256_file(spec.pdf_path)
    existing = _read_records(spec.sidecar_path)
    first = max(1, start)
    last = min(spec.pages, end or spec.pages)
    selected = list(range(first, last + 1))
    if limit > 0:
        selected = selected[:limit]
    if force_rescan:
        _atomic_remove_pages(spec.sidecar_path, set(selected))
        existing = _read_records(spec.sidecar_path)
    todo = [page for page in selected if page not in existing]
    stats: dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "id": spec.id,
        "book": spec.book,
        "volume": spec.volume,
        "source_file": spec.file,
        "source_sha256": source_sha,
        "model": model,
        "prompt_version": prompt_version,
        "expected_pages": spec.pages,
        "selected_pages": len(selected),
        "already_done": len(selected) - len(todo),
        "todo": len(todo),
        "accepted": 0,
        "blank": 0,
        "errors": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
    }
    print(
        f"[{spec.id}] {spec.display_title}: pages={spec.pages} selected={len(selected)} "
        f"done={stats['already_done']} todo={len(todo)} workers={workers} model={model}",
        flush=True,
    )
    if dry_run or not todo:
        return stats

    spec.sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def work(page_number: int) -> tuple[int, dict[str, Any]]:
        if sparse_regions:
            regions = render_sparse_page_regions(
                spec, page_number, target_height, binarize=binarize_sparse
            )
            if body_region_only:
                regions = regions[:1]
            elif top_region_only:
                regions = regions[:2]
            if not regions:
                return page_number, {
                    "text": "", "blank": True, "ink": 0.0, "finish_reason": "blank",
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
                    "latency_ms": 0, "attempt": 0,
                }
            parts: list[str] = []
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
            latency_ms = 0
            used_attempt = 0
            for region_ink, region_b64 in regions:
                region = call_mimo(
                    api_key=api_key, base_url=base_url, model=model,
                    image_b64=region_b64, ink=region_ink, attempts=attempts,
                )
                if str(region.get("text") or "").strip():
                    parts.append(str(region["text"]).strip("\n"))
                for key in usage:
                    usage[key] += int((region.get("usage") or {}).get(key) or 0)
                latency_ms += int(region.get("latency_ms") or 0)
                used_attempt = max(used_attempt, int(region.get("attempt") or 0))
            return page_number, {
                "text": "\n".join(parts), "blank": False,
                "ink": max(item[0] for item in regions), "finish_reason": "sparse-regions",
                "usage": usage, "latency_ms": latency_ms, "attempt": used_attempt,
            }
        ink, image_b64 = render_page(spec, page_number, target_height)
        if not image_b64:
            return page_number, {
                "text": "",
                "blank": True,
                "ink": ink,
                "finish_reason": "blank",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
                "latency_ms": 0,
                "attempt": 0,
            }
        result = call_mimo(
            api_key=api_key,
            base_url=base_url,
            model=model,
            image_b64=image_b64,
            ink=ink,
            attempts=attempts,
        )
        result.update({"blank": False, "ink": ink})
        return page_number, result

    completed = 0
    with spec.sidecar_path.open("a", encoding="utf-8") as output, ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=f"mimo-{spec.id}"
    ) as pool:
        futures = {pool.submit(work, page): page for page in todo}
        for future in as_completed(futures):
            page = futures[future]
            try:
                page, result = future.result()
            except Exception as exc:  # noqa: BLE001 - retain page for the next resumable run
                stats["errors"] += 1
                with _print_lock:
                    print(f"  [error] {spec.id} p{page}: {type(exc).__name__}: {str(exc)[:260]}", flush=True)
                continue
            usage = result.get("usage") or {}
            row = {
                "schema": TRANSCRIPT_SCHEMA,
                "pdf_page": page,
                "text": result.get("text") or "",
                "blank": bool(result.get("blank")),
                "ink": round(float(result.get("ink") or 0.0), 6),
                "model": model,
                "prompt_version": prompt_version,
                "source_sha256": source_sha,
                "finish_reason": result.get("finish_reason") or "",
                "attempt": int(result.get("attempt") or 0),
                "latency_ms": int(result.get("latency_ms") or 0),
                "usage": usage,
            }
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            stats["blank" if row["blank"] else "accepted"] += 1
            for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                stats[key] += int(usage.get(key) or 0)
            completed += 1
            if completed % 20 == 0 or completed == len(todo):
                elapsed = time.monotonic() - started
                rate = elapsed / max(1, completed)
                eta = (len(todo) - completed) * rate / 60
                with _print_lock:
                    print(
                        f"  {completed}/{len(todo)} {rate:.2f}s/page wall ETA={eta:.0f}m "
                        f"errors={stats['errors']}",
                        flush=True,
                    )
    stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
    AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
    (AUDIT_ROOT / f"{spec.id}.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return stats


def audit_volume(spec: VolumeSpec, *, model: str, prompt_version: str) -> dict[str, Any]:
    records = _read_records(spec.sidecar_path)
    missing = sorted(set(range(1, spec.pages + 1)) - set(records))
    wrong_schema: list[int] = []
    wrong_model: list[int] = []
    wrong_prompt: list[int] = []
    suspect: list[dict[str, Any]] = []
    source_hashes: set[str] = set()
    for page, row in records.items():
        if row.get("schema") != TRANSCRIPT_SCHEMA:
            wrong_schema.append(page)
        if row.get("model") != model:
            wrong_model.append(page)
        if row.get("prompt_version") != prompt_version:
            wrong_prompt.append(page)
        source_hashes.add(str(row.get("source_sha256") or ""))
        why = validate_transcript(
            str(row.get("text") or ""),
            ink=float(row.get("ink") or 0.0),
            finish_reason=str(row.get("finish_reason") or ""),
        )
        if why and not (row.get("blank") and why == "nonblank-page-returned-empty"):
            suspect.append({"pdf_page": page, "reason": why})
    actual_hash = sha256_file(spec.pdf_path) if spec.pdf_path.exists() else ""
    passed = not any((missing, wrong_schema, wrong_model, wrong_prompt, suspect)) and source_hashes == {actual_hash}
    return {
        "schema": AUDIT_SCHEMA,
        "id": spec.id,
        "expected_pages": spec.pages,
        "records": len(records),
        "missing_pages": missing,
        "wrong_schema_pages": wrong_schema,
        "wrong_model_pages": wrong_model,
        "wrong_prompt_pages": wrong_prompt,
        "suspect_pages": suspect,
        "source_hash_matches": source_hashes == {actual_hash},
        "passed": passed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiMo-V2.5 transcription for the Hegel scan collection")
    parser.add_argument("--list", action="store_true", help="list configured volumes and exit")
    parser.add_argument("--all", action="store_true", help="scan/audit all configured volumes")
    parser.add_argument("--id", action="append", default=[], help="volume id (repeatable)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--target-height", type=int, default=1800)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force-rescan", action="store_true")
    parser.add_argument(
        "--sparse-regions", action="store_true",
        help="opt-in top/bottom crop retry for explicitly selected sparse pages",
    )
    parser.add_argument(
        "--top-region-only", action="store_true",
        help="with --sparse-regions, send body and folio regions but no bottom footnote",
    )
    parser.add_argument(
        "--body-region-only", action="store_true",
        help="with --sparse-regions, send only the upper body region",
    )
    parser.add_argument(
        "--binarize-sparse", action="store_true",
        help="with --sparse-regions, remove faint scan bleed-through before MiMo",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    specs, catalog_model, prompt_version = load_catalog()
    by_id = {spec.id: spec for spec in specs}
    if args.list:
        for spec in specs:
            print(f"{spec.id}\t{spec.pages}\t{spec.book}\t{spec.display_title}\t{spec.file}")
        print(f"TOTAL\t{sum(spec.pages for spec in specs)}\t{len(specs)} files")
        return 0
    selected = specs if args.all else [by_id[item] for item in args.id if item in by_id]
    unknown = sorted(set(args.id) - set(by_id))
    if unknown:
        print("unknown volume id(s): " + ", ".join(unknown), file=sys.stderr)
        return 2
    if not selected:
        print("select --all or at least one --id", file=sys.stderr)
        return 2
    model = str(os.environ.get("MIMO_VISION_MODEL") or catalog_model or DEFAULT_MODEL).strip()
    base_url = str(os.environ.get("MIMO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if args.audit_only:
        reports = [audit_volume(spec, model=model, prompt_version=prompt_version) for spec in selected]
        payload = {"schema": AUDIT_SCHEMA, "volumes": reports}
        AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
        (AUDIT_ROOT / "final.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if all(report["passed"] for report in reports) else 1
    api_key = str(os.environ.get("MIMO_API_KEY") or "").strip()
    if not api_key and not args.dry_run:
        print("MIMO_API_KEY is required", file=sys.stderr)
        return 2
    workers = max(1, min(16, args.workers))
    reports: list[dict[str, Any]] = []
    for spec in selected:
        reports.append(
            scan_volume(
                spec,
                api_key=api_key,
                base_url=base_url,
                model=model,
                prompt_version=prompt_version,
                workers=workers,
                target_height=max(600 if args.sparse_regions else 1200, min(2600, args.target_height)),
                attempts=max(1, min(8, args.attempts)),
                start=args.start,
                end=args.end,
                limit=max(0, args.limit),
                force_rescan=args.force_rescan,
                sparse_regions=args.sparse_regions,
                top_region_only=args.top_region_only,
                body_region_only=args.body_region_only,
                binarize_sparse=args.binarize_sparse,
                dry_run=args.dry_run,
            )
        )
    print(json.dumps({"schema": AUDIT_SCHEMA, "runs": reports}, ensure_ascii=False, indent=2))
    return 1 if any(report.get("errors") for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
