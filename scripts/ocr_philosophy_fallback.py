#!/usr/bin/env python3
"""Explicit, user-authorized local OCR fallback for unresolved philosophy pages."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

import fitz
import numpy as np
from rapidocr_onnxruntime import RapidOCR

try:
    from . import ocr_hegel_mimo as engine
    from . import ocr_philosophy_mimo as contract
    from .philosophy_catalog import load_volumes, sha256_file
except ImportError:  # pragma: no cover
    import ocr_hegel_mimo as engine
    import ocr_philosophy_mimo as contract
    from philosophy_catalog import load_volumes, sha256_file


AUTH_SCHEMA = "kant-feuerbach-ocr-fallback-authorization-v1"


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.incoming-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_authorization() -> dict:
    if not contract.FALLBACK_AUTH_PATH.exists():
        return {
            "schema": AUTH_SCHEMA,
            "engine": contract.FALLBACK_MODEL,
            "prompt_version": contract.FALLBACK_PROMPT_VERSION,
            "authorization": "user explicitly authorized OCR fallback on 2026-08-18",
            "pages": [],
        }
    payload = json.loads(contract.FALLBACK_AUTH_PATH.read_text(encoding="utf-8"))
    if payload.get("schema") != AUTH_SCHEMA or payload.get("engine") != contract.FALLBACK_MODEL:
        raise ValueError("existing fallback authorization has the wrong contract")
    return payload


def _render(page: fitz.Page, scale: float) -> tuple[np.ndarray, bytes, float]:
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()
    png = pix.tobytes("png")
    gray = image.mean(axis=2)
    ink = float((gray <= 225).mean())
    return image, png, ink


def _recognize(ocr: RapidOCR, image: np.ndarray) -> tuple[str, list[float], float]:
    started = time.monotonic()
    result, _ = ocr(image)
    elapsed = time.monotonic() - started
    rows = result or []
    text = "\n".join(str(row[1]).strip() for row in rows if str(row[1]).strip())
    confidence = [float(row[2]) for row in rows if str(row[1]).strip()]
    return text, confidence, elapsed


def run(*, scale: float, minimum_mean_confidence: float) -> dict:
    contract._install_contract()
    volumes = load_volumes()
    authorization = _load_authorization()
    authorized = {
        (str(row["id"]), int(row["pdf_page"])): row for row in authorization.get("pages") or []
    }
    missing: list[tuple[object, int]] = []
    for item in volumes:
        records = engine._read_records(item.sidecar_path)
        missing.extend((item, page) for page in range(1, item.pages + 1) if page not in records)
    print(f"OCR_FALLBACK missing={len(missing)} engine={contract.FALLBACK_MODEL} workers=1", flush=True)

    ocr = RapidOCR()
    accepted = 0
    failures: list[dict[str, object]] = []
    source_hashes: dict[str, str] = {}
    contract.FALLBACK_EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    for index, (item, page_number) in enumerate(missing, 1):
        if item.id not in source_hashes:
            source_hashes[item.id] = sha256_file(item.pdf_path)
        source_sha = source_hashes[item.id]
        if source_sha != item.sha256:
            raise ValueError(f"source hash mismatch: {item.id}")
        with fitz.open(item.pdf_path) as document:
            image, png, ink = _render(document[page_number - 1], scale)
        text, confidences, elapsed = _recognize(ocr, image)
        why = engine.validate_transcript(
            text, ink=ink, finish_reason=contract.FALLBACK_FINISH_REASON
        )
        mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
        if why or not confidences or mean_confidence < minimum_mean_confidence:
            failure = {
                "id": item.id,
                "pdf_page": page_number,
                "reason": why or "low-confidence",
                "line_count": len(confidences),
                "mean_confidence": round(mean_confidence, 6),
            }
            failures.append(failure)
            print(f"  [reject] {failure}", flush=True)
            continue

        render_file = f"{item.id}-p{page_number}.png"
        evidence_path = contract.FALLBACK_EVIDENCE_ROOT / render_file
        temporary = evidence_path.with_name(f"{evidence_path.name}.incoming-{os.getpid()}")
        temporary.write_bytes(png)
        os.replace(temporary, evidence_path)
        render_sha = contract._sha256(evidence_path)
        auth_row = {
            "id": item.id,
            "pdf_page": page_number,
            "source_sha256": source_sha,
            "render_file": render_file,
            "render_sha256": render_sha,
            "render_scale": scale,
            "line_count": len(confidences),
            "mean_confidence": round(mean_confidence, 6),
            "minimum_line_confidence": round(min(confidences), 6),
        }
        authorized[(item.id, page_number)] = auth_row
        authorization["pages"] = [authorized[key] for key in sorted(authorized)]
        _write_json_atomic(contract.FALLBACK_AUTH_PATH, authorization)

        row = {
            "schema": contract.TRANSCRIPT_SCHEMA,
            "pdf_page": page_number,
            "text": text,
            "blank": False,
            "ink": round(ink, 6),
            "model": contract.FALLBACK_MODEL,
            "prompt_version": contract.FALLBACK_PROMPT_VERSION,
            "source_sha256": source_sha,
            "finish_reason": contract.FALLBACK_FINISH_REASON,
            "attempt": 1,
            "latency_ms": int(elapsed * 1000),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
            "ocr_confidence": {
                "line_count": len(confidences),
                "mean": round(mean_confidence, 6),
                "minimum": round(min(confidences), 6),
            },
        }
        with item.sidecar_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
        accepted += 1
        print(
            f"  {index}/{len(missing)} {item.id} p{page_number} lines={len(confidences)} "
            f"confidence={mean_confidence:.3f} elapsed={elapsed:.2f}s",
            flush=True,
        )
    report = {
        "schema": "kant-feuerbach-ocr-fallback-run-v1",
        "engine": contract.FALLBACK_MODEL,
        "selected_missing_pages": len(missing),
        "accepted": accepted,
        "failures": failures,
        "passed": not failures,
    }
    _write_json_atomic(contract.OUTPUT_ROOT / "audit" / "fallback_run.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=float, default=3.0)
    parser.add_argument("--minimum-mean-confidence", type=float, default=0.75)
    args = parser.parse_args()
    if not 2.0 <= args.scale <= 4.0:
        parser.error("scale must be between 2 and 4")
    report = run(scale=args.scale, minimum_mean_confidence=args.minimum_mean_confidence)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
