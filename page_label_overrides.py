from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import yaml


LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "page_label_overrides.yaml"
_VALID_LABEL_RE = re.compile(r"(?:[1-9]\d*|pre-[ivxlcdm]+)", re.IGNORECASE)
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


@dataclass(frozen=True)
class PageLabelOverride:
    source_file: str
    pdf_page: int
    printed_page: str | None
    suppress_text: bool = False
    reason: str = ""
    feedback_ids: tuple[int, ...] = ()


def _normalize_source_file(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or not raw.startswith("pdfs/"):
        raise ValueError(f"invalid source_file: {raw!r}")
    return path.as_posix()


def parse_page_label_overrides(payload: object) -> dict[tuple[str, int], PageLabelOverride]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError("page label override config must be a mapping")
    version = payload.get("version", 1)
    if version != 1:
        raise ValueError(f"unsupported page label override version: {version!r}")
    rows = payload.get("overrides") or []
    if not isinstance(rows, list):
        raise ValueError("overrides must be a list")

    parsed: dict[tuple[str, int], PageLabelOverride] = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise ValueError(f"override #{index} must be a mapping")
        source_file = _normalize_source_file(row.get("source_file"))
        try:
            pdf_page = int(row.get("pdf_page"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"override #{index} has invalid pdf_page") from exc
        if pdf_page < 1:
            raise ValueError(f"override #{index} has invalid pdf_page")
        if "printed_page" not in row:
            raise ValueError(f"override #{index} must explicitly provide printed_page")
        raw_label = row.get("printed_page")
        printed_page = None if raw_label is None else str(raw_label).strip().lower()
        if printed_page == "":
            printed_page = None
        if printed_page is not None and not _VALID_LABEL_RE.fullmatch(printed_page):
            raise ValueError(f"override #{index} has invalid printed_page: {printed_page!r}")

        suppress_text = row.get("suppress_text", False)
        if not isinstance(suppress_text, bool):
            raise ValueError(f"override #{index} suppress_text must be a boolean")

        raw_feedback_ids = row.get("feedback_ids") or []
        if not isinstance(raw_feedback_ids, list):
            raise ValueError(f"override #{index} feedback_ids must be a list")
        try:
            feedback_ids = tuple(sorted({int(value) for value in raw_feedback_ids if int(value) > 0}))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"override #{index} has invalid feedback_ids") from exc

        key = (source_file, pdf_page)
        if key in parsed:
            raise ValueError(f"duplicate page label override: {source_file} PDF {pdf_page}")
        parsed[key] = PageLabelOverride(
            source_file=source_file,
            pdf_page=pdf_page,
            printed_page=printed_page,
            suppress_text=suppress_text,
            reason=str(row.get("reason") or "").strip(),
            feedback_ids=feedback_ids,
        )
    return parsed


def load_page_label_overrides(
    path: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[tuple[str, int], PageLabelOverride]:
    """Load the optional runtime overlay without ever making corpus startup depend on it."""
    env = os.environ if environ is None else environ
    enabled = str(env.get("MARX_PAGE_LABEL_OVERRIDES_ENABLED", "1") or "1").strip().lower()
    if enabled in _FALSE_VALUES:
        return {}
    configured = str(env.get("MARX_PAGE_LABEL_OVERRIDES_PATH", "") or "").strip()
    target = Path(configured or path or DEFAULT_CONFIG_PATH)
    if not target.is_file():
        return {}
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8"))
        return parse_page_label_overrides(payload)
    except Exception as exc:  # noqa: BLE001 - an optional overlay must fail open
        LOGGER.error("Ignoring invalid page label override file %s: %s", target, exc)
        return {}


def apply_page_label_overrides(
    source_file: str,
    pages: Iterable[Any],
    overrides: Mapping[tuple[str, int], PageLabelOverride],
) -> int:
    """Apply exact, reversible page corrections to in-memory Page objects only."""
    if not overrides or not str(source_file).replace("\\", "/").startswith("pdfs/"):
        return 0
    normalized = _normalize_source_file(source_file)
    changed = 0
    for page in pages:
        key = (normalized, int(page.pdf_page))
        override = overrides.get(key)
        if override is None:
            continue
        page_changed = False
        page.page_label_info = {**(getattr(page, "page_label_info", None) or {}),
                                "status": "manual" if override.printed_page else "source_error" if override.suppress_text else "unnumbered",
                                "printed_page": override.printed_page, "basis": override.reason}
        if page.printed_page != override.printed_page:
            page.printed_page = override.printed_page
            page_changed = True
        if override.suppress_text and (page.raw_text or page.norm_text):
            page.raw_text = ""
            page.norm_text = ""
            page_changed = True
        if page_changed:
            changed += 1
    return changed
