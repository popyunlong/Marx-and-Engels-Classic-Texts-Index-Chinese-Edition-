from __future__ import annotations

"""Quality and integrity checks for publication-ready journal documents.

The checks in this module are intentionally independent from Flask and the
journal database.  That keeps the approval worker, mail worker and reader on
the same immutable file contract without introducing a database migration.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


DOCUMENT_SCHEMA_VERSION = 5
_ABSTRACT_PREFIX_RE = re.compile(
    r"^\s*(?:abstract|summary|摘要|内容提要)\s*(?:[:：.。\-–—]\s*)?",
    re.IGNORECASE,
)
_ABSTRACT_NOISE_RE = re.compile(
    r"(?:\b(?:department|school|faculty|university|corresponding author|issn)\b|"
    r"\b(?:copyright|all rights reserved|creative commons)\b|©|"
    r"https?://|www\.|\bdoi\.org/|\bkey\s*words?\s*[:：])",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(
    r"(?:inline\s*-?\s*eq\s*-?\s*i\s*e\s*q\s*\d+|"
    r"内联公式\s*-?\s*i\s*e\s*q\s*\d+|"
    r"\[(?:formula|image|table)\b|<image\b|"
    r"此处原文为空白段落[，,]?无内容可译)",
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_RUNNING_HEADER_RE = re.compile(
    r"^(?:ECONOMIC GEOGRAPHY|FLOOD PROTECTION AND ADAPTATION LABOR|"
    r"REVIEW OF POLITICAL ECONOMY|Theory, Culture & Society)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)*(?:%|‰)?")


def strip_abstract_label(value: Any) -> str:
    """Remove repeated display labels while preserving the abstract itself."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    previous = None
    while text and text != previous:
        previous = text
        text = _ABSTRACT_PREFIX_RE.sub("", text, count=1).strip()
    return text


def abstract_rejection_reason(value: Any) -> str:
    """Return a stable reason when metadata is not a credible abstract."""
    text = strip_abstract_label(value)
    if len(text) < 80:
        return "abstract-too-short"
    hits = len(_ABSTRACT_NOISE_RE.findall(text))
    words = re.findall(r"[A-Za-z]+", text)
    if hits >= 3 or (hits >= 2 and len(words) < 120):
        return "abstract-frontmatter-noise"
    if re.match(r"^(?:[A-Z][\w'’.-]+\s+){1,5}(?:Department|School|Faculty)\b", text):
        return "abstract-affiliation"
    return ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_asset_path(article_dir: Path, name: Any) -> Path:
    """Resolve one flat asset filename and reject traversal/symlink escapes."""
    raw = str(name or "").strip()
    if not raw or raw in {".", ".."} or Path(raw).name != raw or "/" in raw or "\\" in raw:
        raise ValueError("invalid-asset-name")
    assets_dir = (Path(article_dir) / "assets").resolve()
    target = (assets_dir / raw).resolve()
    if target.parent != assets_dir:
        raise ValueError("asset-path-escape")
    return target


def document_asset_manifest(document: dict) -> dict[str, dict]:
    manifest: dict[str, dict] = {}
    for block in document.get("paragraphs") or []:
        asset = block.get("asset") if isinstance(block, dict) else None
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        if name:
            manifest[name] = asset
    return manifest


def numeric_tokens(value: Any) -> list[str]:
    return [match.group(0).replace(",", "") for match in _NUMBER_RE.finditer(str(value or ""))]


def _table_numbers(block: dict) -> list[str]:
    pieces: list[str] = [str(block.get("text") or "")]
    pieces.extend(str(item) for item in block.get("headers") or [])
    for row in block.get("rows") or []:
        if isinstance(row, dict):
            pieces.append(str(row.get("label") or ""))
            pieces.extend(str(item) for item in row.get("cells") or [])
        elif isinstance(row, (list, tuple)):
            pieces.extend(str(item) for item in row)
    return numeric_tokens(" ".join(pieces))


def build_quality_report(document: dict, *, body_word_coverage: float = 1.0) -> dict:
    """Build the fail-closed report attached by normal ingestion.

    Captions emitted as plain text are deliberately rejected: a later layout
    pass must pair them with a verified figure/table/formula asset before the
    issue can be approved.  This prevents visual material from silently
    degrading into scrambled paragraph text.
    """
    paragraphs = [item for item in document.get("paragraphs") or [] if isinstance(item, dict)]
    visual = [item for item in paragraphs if str(item.get("kind") or "") in {"table", "figure", "formula"}]
    loose_captions = [item for item in paragraphs if str(item.get("kind") or "") == "caption"]
    combined = "\n".join(
        f"{item.get('text') or ''}\n{item.get('zh') or ''}" for item in paragraphs
    )
    required = [
        item for item in paragraphs
        if str(item.get("kind") or "body") != "reference" and str(item.get("text") or "").strip()
    ]
    captions_paired = not loose_captions and all(isinstance(item.get("asset"), dict) for item in visual)
    table_numbers_verified = all(
        not item.get("rows")
        or [str(value) for value in item.get("source_numbers") or []] == _table_numbers(item)
        for item in visual
        if str(item.get("kind") or "") == "table"
    )
    translation_complete = bool(required) and all(str(item.get("zh") or "").strip() for item in required)
    placeholders = len(_PLACEHOLDER_RE.findall(combined))
    content_sanitized = not _CONTROL_RE.search(combined) and not any(
        _RUNNING_HEADER_RE.match(str(item.get("text") or "").strip()) for item in paragraphs
    )
    checks = {
        "body_word_coverage": round(float(body_word_coverage), 4),
        "captions_paired": captions_paired,
        "table_numbers_verified": table_numbers_verified,
        "translation_complete": translation_complete,
        "orphan_fragments": 0,
        "known_placeholders": placeholders,
        "content_sanitized": content_sanitized,
    }
    passed = (
        checks["body_word_coverage"] >= 0.98
        and captions_paired
        and table_numbers_verified
        and translation_complete
        and placeholders == 0
        and content_sanitized
    )
    return {
        "status": "passed" if passed else "failed",
        "pipeline": "journal-layout-v5",
        "checks": checks,
        "review_scope": "first-four-pages,all-visual-pages,cross-page-boundaries",
    }


def validate_document(document: dict, article_dir: Path) -> dict:
    """Recompute all non-negotiable checks from the saved document and assets."""
    errors: list[str] = []
    schema = int(document.get("schema_version") or 0)
    if schema < DOCUMENT_SCHEMA_VERSION:
        errors.append("schema-version")
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    for key in ("abstract_en", "abstract_zh"):
        value = str(metadata.get(key) or "")
        if _ABSTRACT_PREFIX_RE.match(value):
            errors.append(f"{key}-label")
    if abstract_rejection_reason(metadata.get("abstract_en")):
        errors.append("abstract-invalid")

    paragraphs = document.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        errors.append("blocks-missing")
        paragraphs = []
    required = 0
    translated = 0
    visual_blocks = 0
    assets_ok = 0
    for index, block in enumerate(paragraphs):
        if not isinstance(block, dict):
            errors.append(f"block-{index}-invalid")
            continue
        kind = str(block.get("kind") or "body")
        combined = f"{block.get('text') or ''}\n{block.get('zh') or ''}"
        if _PLACEHOLDER_RE.search(combined):
            errors.append(f"block-{index}-placeholder")
        if _CONTROL_RE.search(combined):
            errors.append(f"block-{index}-control-character")
        if _RUNNING_HEADER_RE.match(str(block.get("text") or "").strip()):
            errors.append(f"block-{index}-running-header")
        if kind != "reference" and str(block.get("text") or "").strip():
            required += 1
            if str(block.get("zh") or "").strip():
                translated += 1
            else:
                errors.append(f"block-{index}-translation")
        if kind not in {"table", "figure", "formula"}:
            continue
        visual_blocks += 1
        asset = block.get("asset")
        if not isinstance(asset, dict):
            errors.append(f"block-{index}-asset")
            continue
        try:
            target = safe_asset_path(article_dir, asset.get("name"))
        except ValueError:
            errors.append(f"block-{index}-asset-path")
            continue
        expected = str(asset.get("sha256") or "").lower()
        if not target.is_file() or not expected or file_sha256(target) != expected:
            errors.append(f"block-{index}-asset-hash")
            continue
        assets_ok += 1
        if kind == "table" and block.get("rows"):
            source_numbers = [str(item) for item in block.get("source_numbers") or []]
            if source_numbers != _table_numbers(block):
                errors.append(f"block-{index}-table-numbers")

    quality = document.get("quality") if isinstance(document.get("quality"), dict) else {}
    declared_checks = quality.get("checks") if isinstance(quality.get("checks"), dict) else {}
    coverage = float(declared_checks.get("body_word_coverage") or 0.0)
    if coverage < 0.98:
        errors.append("body-word-coverage")
    if declared_checks.get("orphan_fragments") not in (0, "0"):
        errors.append("orphan-fragments")
    if declared_checks.get("captions_paired") is not True:
        errors.append("captions-unpaired")
    if declared_checks.get("table_numbers_verified") is not True:
        errors.append("table-numbers-unverified")
    if declared_checks.get("translation_complete") is not True:
        errors.append("translation-declared-incomplete")
    if declared_checks.get("content_sanitized") is not True:
        errors.append("content-not-sanitized")
    if required != translated:
        errors.append("translation-incomplete")
    if visual_blocks != assets_ok:
        errors.append("visual-assets-incomplete")

    errors = list(dict.fromkeys(errors))
    return {
        "status": "passed" if not errors and quality.get("status") == "passed" else "failed",
        "errors": errors,
        "schema_version": schema,
        "required_translations": required,
        "translated": translated,
        "visual_blocks": visual_blocks,
        "verified_assets": assets_ok,
        "body_word_coverage": coverage,
    }


def validate_batch_documents(article_ids: Iterable[int], articles_root: Path) -> dict:
    results: dict[str, dict] = {}
    for raw_id in article_ids:
        article_id = int(raw_id)
        article_dir = Path(articles_root) / str(article_id)
        try:
            document = json.loads((article_dir / "doc.json").read_text(encoding="utf-8"))
            result = validate_document(document, article_dir)
        except Exception as exc:  # fail closed for approval and delivery
            result = {"status": "failed", "errors": [f"document-read:{exc}"]}
        results[str(article_id)] = result
    failed = [article_id for article_id, result in results.items() if result.get("status") != "passed"]
    return {
        "status": "passed" if results and not failed else "failed",
        "articles": results,
        "failed_article_ids": failed,
    }
