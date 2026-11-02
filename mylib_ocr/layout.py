# -*- coding: utf-8 -*-
"""Pure layout helpers for personal-library OCR.

Page numbers are evidence about a *sequence*, not isolated OCR tokens.  This
module deliberately has no Flask, S3 or OCR-engine dependency so the same
rules can be regression-tested before the storage node is upgraded.
"""
from __future__ import annotations

from collections import Counter
import re
import statistics
import unicodedata


LAYOUT_SCHEMA_VERSION = 3


_TOC_FOLIO_RE = re.compile(r"(?:\d{1,4}|[IVXLCDMivxlcdm]{1,8})")


def _text_dict_lines(payload: object) -> list[dict]:
    """Flatten a PyMuPDF ``get_text('dict')`` payload into visual lines."""
    if not isinstance(payload, dict):
        return []
    found: list[dict] = []
    for block in payload.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            spans = line.get("spans") or []
            text = "".join(
                str(span.get("text") or "")
                for span in spans if isinstance(span, dict)
            ).strip()
            bbox = line.get("bbox")
            if not text or not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                continue
            try:
                x0, y0, x1, y1 = (float(value) for value in bbox[:4])
            except (TypeError, ValueError):
                continue
            if x1 <= x0 or y1 <= y0:
                continue
            found.append({
                "text": unicodedata.normalize("NFKC", text),
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "yc": (y0 + y1) / 2,
                "height": y1 - y0,
            })
    return found


def reconstruct_text_layer_toc(payload: object, fallback_text: str = "") -> tuple[str, dict]:
    """Restore visual rows when a PDF text layer exports a TOC column-by-column.

    Some born-digital PDFs store every folio in one text column and chapter titles in
    another.  ``get_text('text')`` consequently returns all numbers first, although the
    page looks perfectly normal.  We only switch to visual-row order after at least six
    standalone folios align with textual material and the folios are mostly monotonic.
    Ordinary prose pages therefore retain PyMuPDF's native extraction order.
    """
    visual_lines = _text_dict_lines(payload)
    if len(visual_lines) < 8:
        return fallback_text, {"reordered": False, "matched_rows": 0}
    heights = [item["height"] for item in visual_lines]
    tolerance = max(2.5, min(6.0, statistics.median(heights) * 0.42))
    rows: list[list[dict]] = []
    for item in sorted(visual_lines, key=lambda value: (value["yc"], value["x0"])):
        target = next((row for row in reversed(rows[-4:]) if abs(
            statistics.mean(part["yc"] for part in row) - item["yc"]
        ) <= tolerance), None)
        if target is None:
            rows.append([item])
        else:
            target.append(item)

    matched: list[tuple[list[dict], dict, str, str]] = []
    numeric_values: list[int] = []
    for row in rows:
        numeric: list[tuple[dict, str, str]] = []
        for item in row:
            token = item["text"].strip()
            if _TOC_FOLIO_RE.fullmatch(token):
                numeric.append((item, token, ""))
                continue
            fused = re.match(
                r"^\s*(\d{1,4})\s*[/／]\s*((?:第|引言|导言|前言|后记|附录|注释|参考文献|索引).+)$",
                token,
            )
            if fused:
                numeric.append((item, fused.group(1), fused.group(2)))
        numeric_items = [item for item, _token, _replacement in numeric]
        textual = [item for item in row if item not in numeric_items and re.search(
            r"[\u4e00-\u9fffA-Za-z]", item["text"]
        )]
        textual.extend(
            item for item, _token, replacement in numeric if replacement
        )
        if len(numeric) != 1 or not textual:
            continue
        number_item, token, replacement = numeric[0]
        if token.isdigit():
            value = int(token)
            if not 1 <= value <= 3000:
                continue
            numeric_values.append(value)
        matched.append((row, number_item, token, replacement))
    monotonic_pairs = sum(
        current >= previous
        for previous, current in zip(numeric_values, numeric_values[1:])
    )
    monotonic_ratio = monotonic_pairs / max(1, len(numeric_values) - 1)
    heading_signal = bool(re.search(
        r"(?:目\s*录|CONTENTS)", fallback_text[:1800], re.I,
    ))
    structure_signal = len(re.findall(
        r"(?:^|\n)\s*(?:\d{1,4}\s*)?[/／]?\s*第[一二三四五六七八九十百\d]+[章节编部篇]",
        fallback_text, re.I,
    )) >= 2
    enough = (
        len(matched) >= 6
        and monotonic_ratio >= 0.70
        and (heading_signal or structure_signal or len(matched) >= 9)
    )
    quality = {
        "reordered": bool(enough),
        "matched_rows": len(matched),
        "monotonic_ratio": round(monotonic_ratio, 3),
        "heading_signal": heading_signal,
        "structure_signal": structure_signal,
    }
    if not enough:
        return fallback_text, quality

    row_matches = {
        id(row): (number, token, replacement)
        for row, number, token, replacement in matched
    }
    output: list[str] = []
    for row in rows:
        ordered = sorted(row, key=lambda value: value["x0"])
        match = row_matches.get(id(row))
        pieces: list[str] = []
        for item in ordered:
            if match and item is match[0]:
                if match[2]:
                    pieces.append(match[2])
                continue
            pieces.append(item["text"].strip())
        pieces = [re.sub(r"^\s*[/／]\s*(?=第)", "", piece) for piece in pieces if piece]
        if match:
            pieces.append(match[1])
        row_text = " ".join(pieces).strip()
        if row_text:
            output.append(row_text)
    reconstructed = "\n".join(output).strip()
    return reconstructed or fallback_text, quality


def _box_geometry(box) -> tuple[float, float, float, float] | None:
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    try:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
    except (TypeError, ValueError, IndexError):
        return None
    if max(xs) <= min(xs) or max(ys) <= min(ys):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def layout_blocks(result, image_shape) -> list[dict]:
    """Normalize RapidOCR lines into compact, engine-neutral layout blocks."""
    height, width = int(image_shape[0]), int(image_shape[1])
    if height <= 0 or width <= 0:
        return []
    blocks: list[dict] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        geometry = _box_geometry(item[0])
        text = unicodedata.normalize("NFKC", str(item[1] or "")).strip()
        if geometry is None or not text:
            continue
        min_x, min_y, max_x, max_y = geometry
        try:
            confidence = float(item[2]) if len(item) > 2 else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        blocks.append({
            "text": text,
            "bbox": [
                round(min_x / width, 6), round(min_y / height, 6),
                round(max_x / width, 6), round(max_y / height, 6),
            ],
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
        })
    return blocks


def folio_candidates(result, image_shape) -> list[dict]:
    """Extract *candidates* from physical header/footer bands.

    The old implementation accepted numbers anywhere on the outer horizontal
    edge.  Years in body text therefore became folios.  Here an item must also
    be in the top/bottom 18% and small relative to ordinary OCR lines.  Final
    acceptance still happens across the whole book in ``validate_folios``.
    """
    height, width = int(image_shape[0]), int(image_shape[1])
    if height <= 0 or width <= 0:
        return []
    parsed: list[tuple[float, float, float, float, float, str, float]] = []
    line_heights: list[float] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        geometry = _box_geometry(item[0])
        if geometry is None:
            continue
        min_x, min_y, max_x, max_y = geometry
        box_height = max_y - min_y
        line_heights.append(box_height)
        try:
            confidence = float(item[2]) if len(item) > 2 else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        parsed.append((min_x, min_y, max_x, max_y, box_height,
                       unicodedata.normalize("NFKC", str(item[1] or "")).strip(), confidence))
    if not parsed:
        return []
    median_height = statistics.median(line_heights) if line_heights else 0.0
    found: list[dict] = []
    for min_x, min_y, max_x, max_y, box_height, token, confidence in parsed:
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2
        y_ratio = center_y / height
        if 0.18 < y_ratio < 0.82:
            continue
        if median_height and box_height > median_height * 1.55:
            continue
        values: list[tuple[str, str]] = []
        if re.fullmatch(r"\d{1,4}", token):
            values.append((token, "standalone"))
        if min_x <= width * 0.10:
            match = re.match(r"^(\d{2,4})(?=\D)", token)
            if match:
                values.append((match.group(1), "fused-left"))
        if max_x >= width * 0.90:
            match = re.search(r"(?<=\D)(\d{2,4})$", token)
            if match:
                values.append((match.group(1), "fused-right"))
        for raw_value, source in values:
            value = int(raw_value)
            if not 1 <= value <= 3000:
                continue
            edge = "left" if center_x <= width * 0.25 else (
                "right" if center_x >= width * 0.75 else "center"
            )
            item = {
                "value": value,
                "band": "top" if y_ratio <= 0.18 else "bottom",
                "edge": edge,
                "source": source,
                "x": round(center_x / width, 5),
                "y": round(y_ratio, 5),
                "confidence": round(max(0.0, min(1.0, confidence)), 4),
            }
            if item not in found:
                found.append(item)
    return sorted(found, key=lambda item: (item["y"], item["x"], item["value"]))


def validate_folios(candidates_by_page: dict[int, list[dict]], total_pages: int) -> tuple[dict[int, list[str]], dict]:
    """Confirm folios only when a stable PDF-minus-print sequence supports them."""
    anchors: list[tuple[int, int, int]] = []
    for page, candidates in candidates_by_page.items():
        if page < 1 or page > total_pages:
            continue
        seen: set[int] = set()
        for candidate in candidates or []:
            try:
                value = int(candidate.get("value") or 0)
            except (TypeError, ValueError):
                continue
            if not 1 <= value <= 3000 or value in seen:
                continue
            seen.add(value)
            anchors.append((page, value, page - value))
    offsets = Counter(offset for _page, _value, offset in anchors)
    top_support = offsets.most_common(1)[0][1] if offsets else 0
    evidence = len(anchors)
    if evidence <= 2 or (evidence <= 4 and top_support >= 2 and top_support * 2 >= evidence):
        threshold = 2
    else:
        threshold = max(3, min(8, (top_support + 9) // 10))
    trusted = {offset for offset, support in offsets.items() if support >= threshold}
    printed: dict[int, list[str]] = {}
    for page, value, offset in anchors:
        if offset not in trusted:
            continue
        token = str(value)
        printed.setdefault(page, [])
        if token not in printed[page]:
            printed[page].append(token)
    for values in printed.values():
        values.sort(key=int)
    quality = {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "candidate_pages": len([page for page, values in candidates_by_page.items() if values]),
        "candidate_count": evidence,
        "validated_pages": len(printed),
        "validated_count": sum(len(values) for values in printed.values()),
        "offsets": [
            {"offset": int(offset), "support": int(offsets[offset])}
            for offset in sorted(trusted)
        ],
        "rejected_candidates": max(0, evidence - sum(len(values) for values in printed.values())),
    }
    return printed, quality
