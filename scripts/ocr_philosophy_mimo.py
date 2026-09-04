#!/usr/bin/env python3
"""MiMo-V2.5 faithful OCR for the pinned Kant and Feuerbach scans.

This is deliberately a separate entry point and output namespace from the
Hegel collection.  It reuses the already-tested transport/rendering engine,
while replacing the catalogue, prompts, schemas and source-hash contract.
"""
from __future__ import annotations

import os
import sys
import base64
import hashlib
import json
from pathlib import Path

import yaml

try:  # package import in tests / direct script execution in operations
    from . import ocr_hegel_mimo as engine
    from .philosophy_catalog import CATALOG_PATH, ROOT, load_payload, load_volumes
except ImportError:  # pragma: no cover - exercised by the operational CLI
    import ocr_hegel_mimo as engine
    from philosophy_catalog import CATALOG_PATH, ROOT, load_payload, load_volumes


OUTPUT_ROOT = ROOT / "data" / "kant_feuerbach"
TRANSCRIPT_SCHEMA = "kant-feuerbach-mimo-page-v1"
AUDIT_SCHEMA = "kant-feuerbach-mimo-audit-v1"
REVIEWED_BLANKS_PATH = ROOT / "config" / "philosophy_reviewed_blank_pages.yaml"
FALLBACK_MODEL = "rapidocr-onnxruntime"
FALLBACK_PROMPT_VERSION = "user-authorized-ocr-fallback-20260818"
FALLBACK_FINISH_REASON = "user-authorized-ocr-fallback"
FALLBACK_AUTH_PATH = OUTPUT_ROOT / "audit" / "fallback_authorization.json"
FALLBACK_EVIDENCE_ROOT = OUTPUT_ROOT / "audit" / "fallback_pages"

SYSTEM_PROMPT = (
    "执行扫描文字 OCR。逐字、逐行转录图片全部可见印刷文字。输出将直接进入学术引文数据库。"
    "不得解释、改写、概括、翻译、补全或重新组织层级；不得把目录转成结构化条目；"
    "不得添加礼貌用语、说明、Markdown 或原图没有的符号；页眉、页码、脚注、外文、点线均须保留，"
    "保持原始阅读顺序和换行。图片没有文字时 text 为空字符串，无法辨认的单字写□。"
    "输出必须严格匹配给定 JSON Schema：只有 text 字符串，不得有任何其他字段。"
)
USER_PROMPT = "将这页康德或费尔巴哈著作扫描图的全部视觉文字原样 OCR 到 text 字符串。"


def _load_reviewed_blank_pages() -> set[tuple[str, int]]:
    """Load individually rendered and visually reviewed text-free pages.

    Paper texture or a watermark can exceed the generic ink threshold. Entries
    are therefore page-specific, source-hash-bound, and only consumed by this
    philosophy pipeline's sparse retry path.
    """
    if not REVIEWED_BLANKS_PATH.exists():
        return set()
    payload = yaml.safe_load(REVIEWED_BLANKS_PATH.read_text(encoding="utf-8")) or {}
    if payload.get("schema") != "kant-feuerbach-reviewed-blank-pages-v1":
        raise ValueError("wrong reviewed-blank schema")
    volumes = {item.id: item for item in load_volumes(CATALOG_PATH)}
    reviewed: set[tuple[str, int]] = set()
    for raw in payload.get("pages") or []:
        volume_id = str(raw.get("id") or "").strip()
        page = int(raw.get("pdf_page") or 0)
        item = volumes.get(volume_id)
        if item is None or not 1 <= page <= item.pages:
            raise ValueError(f"invalid reviewed blank page: {volume_id} p{page}")
        if str(raw.get("source_sha256") or "").lower() != item.sha256:
            raise ValueError(f"reviewed blank source hash mismatch: {volume_id} p{page}")
        render_sha = str(raw.get("render_sha256") or "").lower()
        render_file = str(raw.get("render_file") or "").strip()
        if not render_file or Path(render_file).name != render_file:
            raise ValueError(f"invalid reviewed blank render file: {volume_id} p{page}")
        if len(render_sha) != 64 or any(ch not in "0123456789abcdef" for ch in render_sha):
            raise ValueError(f"invalid reviewed blank render hash: {volume_id} p{page}")
        if not str(raw.get("review") or "").strip():
            raise ValueError(f"missing reviewed blank evidence: {volume_id} p{page}")
        key = (volume_id, page)
        if key in reviewed:
            raise ValueError(f"duplicate reviewed blank page: {volume_id} p{page}")
        reviewed.add(key)
    return reviewed


REVIEWED_BLANK_PAGES = _load_reviewed_blank_pages()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_reviewed_blank_pages() -> dict[str, object]:
    payload = yaml.safe_load(REVIEWED_BLANKS_PATH.read_text(encoding="utf-8")) or {}
    volumes = {item.id: item for item in load_volumes(CATALOG_PATH)}
    catalog = load_payload(CATALOG_PATH)
    model = str(catalog.get("model") or engine.DEFAULT_MODEL).strip()
    prompt_version = str(catalog.get("prompt_version") or "").strip()
    evidence_root = OUTPUT_ROOT / "audit" / "reviewed_blanks"
    rows: list[dict[str, object]] = []
    for raw in payload.get("pages") or []:
        volume_id = str(raw["id"])
        page = int(raw["pdf_page"])
        item = volumes[volume_id]
        evidence = evidence_root / str(raw["render_file"])
        record = engine._read_records(item.sidecar_path).get(page)
        checks = {
            "evidence_file": evidence.is_file(),
            "evidence_sha256": evidence.is_file() and _sha256(evidence) == raw["render_sha256"],
            "sidecar_record": record is not None,
            "blank": bool(record and record.get("blank")),
            "empty_text": bool(record is not None and not str(record.get("text") or "")),
            "schema": bool(record and record.get("schema") == TRANSCRIPT_SCHEMA),
            "model": bool(record and record.get("model") == model),
            "prompt_version": bool(record and record.get("prompt_version") == prompt_version),
            "source_sha256": bool(record and record.get("source_sha256") == item.sha256),
        }
        rows.append({
            "id": volume_id,
            "pdf_page": page,
            "render_file": str(raw["render_file"]),
            "render_sha256": str(raw["render_sha256"]),
            "checks": checks,
            "passed": all(checks.values()),
        })
    result: dict[str, object] = {
        "schema": "kant-feuerbach-reviewed-blank-audit-v1",
        "expected_pages": len(payload.get("pages") or []),
        "pages": rows,
        "passed": bool(rows) and all(bool(row["passed"]) for row in rows),
    }
    audit_path = OUTPUT_ROOT / "audit" / "reviewed_blanks.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _load_fallback_authorizations() -> dict[tuple[str, int], dict[str, object]]:
    if not FALLBACK_AUTH_PATH.exists():
        return {}
    payload = json.loads(FALLBACK_AUTH_PATH.read_text(encoding="utf-8"))
    if payload.get("schema") != "kant-feuerbach-ocr-fallback-authorization-v1":
        raise ValueError("wrong OCR fallback authorization schema")
    if payload.get("engine") != FALLBACK_MODEL:
        raise ValueError("wrong OCR fallback engine")
    volumes = {item.id: item for item in load_volumes(CATALOG_PATH)}
    rows: dict[tuple[str, int], dict[str, object]] = {}
    for raw in payload.get("pages") or []:
        key = (str(raw.get("id") or ""), int(raw.get("pdf_page") or 0))
        if key in rows:
            raise ValueError(f"duplicate OCR fallback authorization: {key}")
        render_file = str(raw.get("render_file") or "")
        render_sha = str(raw.get("render_sha256") or "").lower()
        if not key[0] or key[1] < 1 or Path(render_file).name != render_file:
            raise ValueError(f"invalid OCR fallback authorization: {key}")
        item = volumes.get(key[0])
        if item is None or key[1] > item.pages or raw.get("source_sha256") != item.sha256:
            raise ValueError(f"OCR fallback authorization is not source-bound: {key}")
        if len(render_sha) != 64 or any(ch not in "0123456789abcdef" for ch in render_sha):
            raise ValueError(f"invalid OCR fallback evidence hash: {key}")
        rows[key] = raw
    return rows


def audit_all_pages(ids: set[str] | None = None) -> dict[str, object]:
    """Audit MiMo rows plus only the explicitly authorized OCR fallback rows."""
    catalog = load_payload(CATALOG_PATH)
    model = str(catalog.get("model") or engine.DEFAULT_MODEL).strip()
    prompt_version = str(catalog.get("prompt_version") or "").strip()
    authorizations = _load_fallback_authorizations()
    reports: list[dict[str, object]] = []
    for item in load_volumes(CATALOG_PATH):
        if ids is not None and item.id not in ids:
            continue
        records = engine._read_records(item.sidecar_path)
        missing = sorted(set(range(1, item.pages + 1)) - set(records))
        wrong_schema: list[int] = []
        wrong_model: list[int] = []
        wrong_prompt: list[int] = []
        reasoning_pages: list[int] = []
        suspect: list[dict[str, object]] = []
        fallback_pages: list[int] = []
        fallback_evidence_failures: list[dict[str, object]] = []
        source_hashes: set[str] = set()
        for page, row in records.items():
            if row.get("schema") != TRANSCRIPT_SCHEMA:
                wrong_schema.append(page)
            row_model = str(row.get("model") or "")
            if row_model == model:
                if row.get("prompt_version") != prompt_version:
                    wrong_prompt.append(page)
            elif row_model == FALLBACK_MODEL:
                fallback_pages.append(page)
                auth = authorizations.get((item.id, page))
                evidence = FALLBACK_EVIDENCE_ROOT / str((auth or {}).get("render_file") or "")
                checks = {
                    "authorized": auth is not None,
                    "prompt_version": row.get("prompt_version") == FALLBACK_PROMPT_VERSION,
                    "finish_reason": row.get("finish_reason") == FALLBACK_FINISH_REASON,
                    "source_sha256": bool(auth and auth.get("source_sha256") == item.sha256),
                    "evidence_file": bool(auth and evidence.is_file()),
                    "evidence_sha256": bool(
                        auth and evidence.is_file() and _sha256(evidence) == auth.get("render_sha256")
                    ),
                }
                if not all(checks.values()):
                    fallback_evidence_failures.append({"pdf_page": page, "checks": checks})
            else:
                wrong_model.append(page)
            source_hashes.add(str(row.get("source_sha256") or ""))
            if int((row.get("usage") or {}).get("reasoning_tokens") or 0):
                reasoning_pages.append(page)
            why = engine.validate_transcript(
                str(row.get("text") or ""),
                ink=float(row.get("ink") or 0.0),
                finish_reason=str(row.get("finish_reason") or ""),
            )
            if why and not (row.get("blank") and why == "nonblank-page-returned-empty"):
                suspect.append({"pdf_page": page, "reason": why})
        orphan_auth = sorted(
            page for (volume_id, page) in authorizations
            if volume_id == item.id and not (records.get(page) or {}).get("model") == FALLBACK_MODEL
        )
        if orphan_auth:
            fallback_evidence_failures.append({"orphan_authorizations": orphan_auth})
        actual_hash = _sha256(item.pdf_path) if item.pdf_path.exists() else ""
        source_hash_matches = source_hashes == {actual_hash}
        passed = not any((
            missing, wrong_schema, wrong_model, wrong_prompt, reasoning_pages,
            suspect, fallback_evidence_failures,
        )) and source_hash_matches
        reports.append({
            "schema": AUDIT_SCHEMA,
            "id": item.id,
            "expected_pages": item.pages,
            "records": len(records),
            "missing_pages": missing,
            "wrong_schema_pages": wrong_schema,
            "wrong_model_pages": wrong_model,
            "wrong_prompt_pages": wrong_prompt,
            "reasoning_pages": reasoning_pages,
            "suspect_pages": suspect,
            "fallback_pages": sorted(fallback_pages),
            "fallback_evidence_failures": fallback_evidence_failures,
            "source_hash_matches": source_hash_matches,
            "passed": passed,
        })
    payload: dict[str, object] = {
        "schema": AUDIT_SCHEMA,
        "volumes": reports,
        "fallback_model": FALLBACK_MODEL,
        "fallback_authorized_pages": len(authorizations),
        "passed": bool(reports) and all(bool(report["passed"]) for report in reports),
    }
    audit_path = OUTPUT_ROOT / "audit" / "final.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _render_sparse_ink_bands(
    spec: engine.VolumeSpec,
    page_number: int,
    target_height: int,
    *,
    binarize: bool = False,
) -> list[tuple[float, str]]:
    """Render every visible ink band while removing only genuinely empty gaps.

    The scans contain TOC pages with a running head, a large white gap, a dense
    body and a folio.  Sending that full canvas can make MiMo encode the white
    gap as thousands of newlines.  Fixed top/middle/bottom crops are unsafe
    because they can cut or omit TOC lines, so bands are derived from the scan's
    own row ink profile and retain their original top-to-bottom order.
    """
    if (spec.id, page_number) in REVIEWED_BLANK_PAGES:
        return []

    import fitz
    import numpy as np

    page = engine._thread_doc(spec.pdf_path)[page_number - 1]
    width, height = float(page.rect.width), float(page.rect.height)
    detect_scale = max(0.65, min(1.2, 720.0 / max(1.0, width)))
    thumb = page.get_pixmap(
        matrix=fitz.Matrix(detect_scale, detect_scale),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    gray = np.frombuffer(thumb.samples, dtype=np.uint8).reshape(thumb.height, thumb.width)
    ink_mask = gray <= 225
    minimum_row_ink = max(3, int(round(thumb.width * 0.0025)))
    active = ink_mask.sum(axis=1) >= minimum_row_ink
    active_rows = np.flatnonzero(active)
    if not len(active_rows):
        return []

    # A gap of this size is visual whitespace rather than line leading.  Smaller
    # gaps remain inside one crop so multi-line headings and TOC entries cannot
    # be split or reordered.
    split_gap = max(18, int(round(thumb.height * 0.035)))
    groups: list[tuple[int, int]] = []
    start = previous = int(active_rows[0])
    for raw_row in active_rows[1:]:
        row = int(raw_row)
        if row - previous > split_gap:
            groups.append((start, previous))
            start = row
        previous = row
    groups.append((start, previous))

    margin = max(8, int(round(thumb.height * 0.012)))
    rendered: list[tuple[float, str]] = []
    for first, last in groups:
        top_px = max(0, first - margin)
        bottom_px = min(thumb.height, last + margin + 1)
        clip = fitz.Rect(
            0,
            height * top_px / thumb.height,
            width,
            height * bottom_px / thumb.height,
        )
        band_mask = ink_mask[top_px:bottom_px]
        ink = float(band_mask.mean())
        scale = max(1.0, min(6.0, target_height / max(1.0, float(clip.height))))
        pix = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            clip=clip,
            colorspace=fitz.csRGB,
            alpha=False,
        )
        if binarize:
            import cv2

            rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            band_gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            binary = np.where(band_gray <= 205, 0, 255).astype(np.uint8)
            ok, encoded = cv2.imencode(".jpg", binary, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                raise RuntimeError("could not encode binarized sparse ink band")
            data = encoded.tobytes()
        else:
            data = pix.tobytes("jpeg", jpg_quality=92)
        rendered.append((ink, base64.b64encode(data).decode("ascii")))
    return rendered


def _install_contract() -> None:
    payload = load_payload(CATALOG_PATH)
    pinned = {item.pdf_path.resolve(): item.sha256 for item in load_volumes(CATALOG_PATH)}
    original_sha256 = engine.sha256_file

    def checked_sha256(path: Path) -> str:
        actual = original_sha256(path)
        expected = pinned.get(path.resolve())
        if expected is not None and actual != expected:
            raise ValueError(f"pinned source SHA-256 mismatch: {path}")
        return actual

    def local_catalog(path: Path = CATALOG_PATH):  # noqa: ARG001 - compatible engine hook
        specs = [
            engine.VolumeSpec(
                id=item.id,
                book=item.book,
                volume=item.volume,
                display_title=item.display_title,
                file=item.file,
                pages=item.pages,
            )
            for item in load_volumes(CATALOG_PATH)
        ]
        return (
            specs,
            str(payload.get("model") or engine.DEFAULT_MODEL).strip(),
            str(payload.get("prompt_version") or "").strip(),
        )

    engine.CATALOG_PATH = CATALOG_PATH
    engine.OUTPUT_ROOT = OUTPUT_ROOT
    engine.AUDIT_ROOT = OUTPUT_ROOT / "audit"
    engine.TRANSCRIPT_SCHEMA = TRANSCRIPT_SCHEMA
    engine.AUDIT_SCHEMA = AUDIT_SCHEMA
    engine.SYSTEM_PROMPT = SYSTEM_PROMPT
    engine.USER_PROMPT = USER_PROMPT
    engine.sha256_file = checked_sha256
    engine.load_catalog = local_catalog
    engine.render_sparse_page_regions = _render_sparse_ink_bands


def _pilot(extra: list[str]) -> int:
    disallowed = {"--all", "--id", "--start", "--end", "--limit", "--audit-only", "--force-rescan"}
    if any(arg in disallowed for arg in extra):
        print("--pilot cannot be combined with selection/rescan arguments", file=sys.stderr)
        return 2
    common = [arg for arg in extra if arg != "--pilot"]
    for item in load_volumes():
        for page in item.pilot_pages:
            result = engine.main(
                ["--id", item.id, "--start", str(page), "--end", str(page), "--workers", "1", *common]
            )
            if result:
                return result
    print(f"PILOT\t57 accepted page selections\t{len(load_volumes())} files")
    return 0


def _pilot_audit() -> int:
    payload = load_payload(CATALOG_PATH)
    model = str(payload.get("model") or engine.DEFAULT_MODEL).strip()
    prompt_version = str(payload.get("prompt_version") or "").strip()
    reports: list[dict[str, object]] = []
    total = 0
    for item in load_volumes(CATALOG_PATH):
        spec = engine.VolumeSpec(
            id=item.id,
            book=item.book,
            volume=item.volume,
            display_title=item.display_title,
            file=item.file,
            pages=item.pages,
        )
        records = engine._read_records(spec.sidecar_path)
        failures: list[dict[str, object]] = []
        for page in item.pilot_pages:
            total += 1
            row = records.get(page)
            if row is None:
                failures.append({"pdf_page": page, "reason": "missing"})
                continue
            checks = {
                "schema": row.get("schema") == TRANSCRIPT_SCHEMA,
                "model": row.get("model") == model,
                "prompt_version": row.get("prompt_version") == prompt_version,
                "source_sha256": row.get("source_sha256") == item.sha256,
                "reasoning_disabled": int((row.get("usage") or {}).get("reasoning_tokens") or 0) == 0,
                "not_truncated": row.get("finish_reason") != "length",
            }
            why = engine.validate_transcript(
                str(row.get("text") or ""),
                ink=float(row.get("ink") or 0.0),
                finish_reason=str(row.get("finish_reason") or ""),
            )
            if why and not (row.get("blank") and why == "nonblank-page-returned-empty"):
                checks["transcript"] = False
                checks["transcript_reason"] = why
            failed = [name for name, passed in checks.items() if passed is False]
            if failed:
                failures.append({"pdf_page": page, "reason": ",".join(failed)})
        reports.append(
            {
                "id": item.id,
                "pilot_pages": list(item.pilot_pages),
                "failures": failures,
                "passed": not failures,
            }
        )
    result = {
        "schema": "kant-feuerbach-mimo-pilot-audit-v1",
        "expected_pages": 57,
        "audited_pages": total,
        "volumes": reports,
        "passed": total == 57 and all(bool(report["passed"]) for report in reports),
    }
    audit_path = OUTPUT_ROOT / "audit" / "pilot.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    _install_contract()
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--pilot-audit"]:
        return _pilot_audit()
    if args == ["--reviewed-blank-audit"]:
        result = audit_reviewed_blank_pages()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    if "--audit-only" in args:
        ids: set[str] | None = None
        if "--all" not in args:
            ids = {args[index + 1] for index, value in enumerate(args[:-1]) if value == "--id"}
            if not ids:
                print("select --all or at least one --id", file=sys.stderr)
                return 2
        result = audit_all_pages(ids)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    if "--pilot" in args:
        return _pilot(args)
    return engine.main(args)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
