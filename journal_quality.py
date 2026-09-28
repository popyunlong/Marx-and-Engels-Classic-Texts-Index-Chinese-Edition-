from __future__ import annotations

"""Quality and integrity checks for publication-ready journal documents.

The checks in this module are intentionally independent from Flask and the
journal database.  That keeps the approval worker, mail worker and reader on
the same immutable file contract without introducing a database migration.
"""

import hashlib
import json
import re
import unicodedata
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


def _identity_words(value: Any) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.findall(r"[a-z0-9]+", normalized)


def pdf_identity_error(article: dict, pdf_path: Path) -> str:
    """Reject a plausible-sized PDF belonging to a different paper."""
    import fitz

    with fitz.open(pdf_path) as pdf:
        first_pages = " ".join(pdf[index].get_text() for index in range(min(3, len(pdf))))
    words = _identity_words(first_pages)
    title = [word for word in _identity_words(article.get("title")) if len(word) > 2]
    if len(title) < 2 or not words:
        return "pdf-title-unverifiable"
    title_hits = sum(word in words for word in title)
    if title_hits / len(title) < 0.75:
        return "pdf-title-mismatch"
    authors = article.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    surnames = [parts[-1] for name in authors if (parts := _identity_words(name)) and len(parts[-1]) >= 3]
    if surnames and not any(surname in words for surname in surnames):
        return "pdf-author-mismatch"
    expected_doi = re.sub(r"\s+", "", str(article.get("doi") or "")).casefold()
    pdf_dois = {match.rstrip(".,;)").casefold() for match in re.findall(
        r"10\.\d{4,9}/[^\s<>]+", first_pages, flags=re.I
    )}
    if expected_doi and pdf_dois and expected_doi not in pdf_dois:
        return "pdf-doi-mismatch"
    return ""


def source_body_page_coverage(pdf_path: Path, paragraphs: list[dict]) -> tuple[float, dict]:
    """Measure physical source body pages represented by readable document blocks."""
    import fitz

    with fitz.open(pdf_path) as pdf:
        pages = [page.get_text() for page in pdf]
    intro = [index + 1 for index, value in enumerate(pages) if re.search(
        r"(?im)^\s*(?:\d+(?:\.\d+)?[.)]?\s*)?introduction\s*$", value
    )]
    dense = [index + 1 for index, value in enumerate(pages)
             if len(re.findall(r"[A-Za-z]+", value)) >= 120]
    start = intro[0] if intro else (dense[0] if dense else 0)
    # Repository cover sheets may precede an abstract-only first article page.
    if (not intro and len(pages) > 2
            and re.search(r"research online|deposited via", pages[0], re.I)
            and re.search(r"\babstract\b", pages[1], re.I)):
        section = [index + 1 for index, value in enumerate(pages[1:5], 1)
                   if re.search(r"(?im)^\s*[1-9]\d*(?:\.\d+)*[.)]\s+[A-Za-z]", value)]
        if section:
            start = section[0]
    references = [index + 1 for index, value in enumerate(pages) if re.search(
        r"(?im)^\s*(?:references|bibliography)\s*$", value
    )]
    end = next((page - 1 for page in references if page > start), len(pages))
    expected = set(range(start, end + 1)) if start and end >= start else set()
    covered: set[int] = set()
    for block in paragraphs:
        if not isinstance(block, dict) or block.get("kind") == "reference":
            continue
        if len(re.findall(r"[A-Za-z]+", str(block.get("text") or ""))) < 5:
            continue
        spans = block.get("source_spans") or []
        if spans:
            span_pages = [int(span.get("page") or 0) for span in spans if isinstance(span, dict)]
            if span_pages and min(span_pages) > 0 and max(span_pages) <= len(pages):
                covered.update(range(min(span_pages), max(span_pages) + 1))
        else:
            first = int(block.get("page") or 0)
            last = int(block.get("page_end") or first)
            if first > 0 and first <= last <= len(pages):
                covered.update(range(first, last + 1))
    ratio = len(expected & covered) / len(expected) if expected else 0.0
    return ratio, {
        "source_sha256": file_sha256(pdf_path),
        "source_body_pages": sorted(expected),
        "covered_body_pages": sorted(expected & covered),
        "missing_body_pages": sorted(expected - covered),
    }


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


def build_quality_report(document: dict, *, body_word_coverage: float) -> dict:
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
        "orphan_fragments": sum(
            bool(re.fullmatch(r"[A-Za-z.]", str(item.get("text") or "").strip()))
            for item in paragraphs if item.get("kind") == "body"
        ),
        "known_placeholders": placeholders,
        "content_sanitized": content_sanitized,
    }
    passed = (
        checks["body_word_coverage"] >= 0.98
        and captions_paired
        and table_numbers_verified
        and translation_complete
        and checks["orphan_fragments"] == 0
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
    source_pdf = Path(article_dir) / "source.pdf"
    try:
        if not source_pdf.is_file():
            raise FileNotFoundError("source.pdf")
        coverage, evidence = source_body_page_coverage(source_pdf, paragraphs)
        source_article = {
            "title": metadata.get("title_en"),
            "authors": metadata.get("authors_en"),
            "doi": (document.get("provenance") or {}).get("doi"),
        }
        identity_error = pdf_identity_error(source_article, source_pdf)
        if identity_error:
            errors.append(identity_error)
        declared_source = str(declared_checks.get("source_sha256") or "")
        if declared_source and declared_source != evidence["source_sha256"]:
            errors.append("source-changed")
    except Exception as exc:
        coverage = 0.0
        evidence = {"missing_body_pages": []}
        errors.append(f"source-validation:{type(exc).__name__}")
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
