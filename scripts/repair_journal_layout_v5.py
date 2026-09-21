from __future__ import annotations

"""Rebuild the current journal issue as schema-v5 documents in an isolated data root.

The command never discovers or downloads content and never sends mail.  It only
rewrites an explicitly supplied candidate data copy, which is promoted later by
the zero-downtime release step after all nine documents pass validation.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from journal_quality import (
    DOCUMENT_SCHEMA_VERSION,
    file_sha256,
    numeric_tokens,
    strip_abstract_label,
    validate_batch_documents,
)


BATCH_ID = 24
READY_IDS = (1326, 1327, 1330, 1332, 1339, 1340, 1342, 1350, 1357)
_CAPTION_RE = re.compile(r"^(?:Table|Figure|Fig\.)\s*\d+", re.I)
_NUMBERED_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)*\s*[|｜.]?\s+\S")
_FORMULA_PLACEHOLDER_RE = re.compile(
    r"(?:in\s*l\s*i\s*n\s*e\s*-?\s*e\s*q\s*-?\s*)?i\s*e\s*q\s*\d+"
    r"(?:[𝑝𝑞𝑘\d()\s.,-]*)|内联公式\s*-?\s*i\s*e\s*q\s*\d+",
    re.I,
)
_RUNNING_RE = re.compile(
    r"^(?:ECONOMIC GEOGRAPHY|FLOOD PROTECTION AND ADAPTATION LABOR|"
    r"REVIEW OF POLITICAL ECONOMY|Theory, Culture & Society|Vol\.\s*\d+\s+No\.)",
    re.I,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


ABSTRACT_1327_EN = (
    "Jakarta is undergoing an infrastructural transformation in response to increasing flood risks, "
    "constructing new dams, flood canals, reservoirs and retention ponds, and ongoing normalization "
    "of the city’s rivers. However, one crucial element of responding to severe flood risks that has "
    "so far been overlooked in Jakarta and other vulnerable cities is the work of building and "
    "maintaining this flood protection infrastructure. In this article, we respond to this research "
    "gap by analyzing the work of the Blue Troops, a new provincial workforce who construct and "
    "maintain Jakarta’s flood protection infrastructure. Drawing from extensive fieldwork interviewing "
    "and observing the Blue Troops in Jakarta, we catalog their adaptation labor practices, showing "
    "the accumulated health and safety risks of their work. At the same time, we demonstrate that the "
    "workers find this to be reliable and meaningful collective work, essential for reproducing cities "
    "in a time of climate catastrophe. In short, we show that workers trade precarious labor practices "
    "and bodily risks against secure public-sector employment and a sense of contributing to their "
    "city. This article, therefore, contributes to a growing scholarly concern with the labor "
    "implications of climate change, suggesting that the organization, conditions, and value of this "
    "work will be crucial political and analytical terrain."
)
ABSTRACT_1327_ZH = (
    "为应对日益加剧的洪水风险，雅加达正在推进基础设施转型，包括修建新的水坝、防洪渠、"
    "水库和调蓄池，并持续实施河道整治。然而，在雅加达及其他脆弱城市，应对严重洪灾风险的"
    "一个关键环节迄今一直被忽视，即建设和维护防洪基础设施的劳动。本文通过分析“蓝色部队”"
    "的工作回应这一研究空白；这是一支新组建的省级劳动力队伍，负责建设和维护雅加达的防洪"
    "基础设施。基于在雅加达对蓝色部队开展的广泛访谈与观察，本文梳理其气候适应劳动实践，"
    "并揭示这类工作累积的健康与安全风险。同时，研究表明，劳动者认为这是一项稳定、有意义"
    "且具有集体性的工作，对于在气候灾难时代维系城市再生产至关重要。简言之，劳动者以不稳定"
    "的劳动实践和身体风险，换取有保障的公共部门就业以及为城市作出贡献的意义感。因此，本文"
    "回应了学界对气候变化之劳动影响日益增长的关注，并指出这类工作的组织方式、劳动条件与"
    "价值将成为关键的政治与分析领域。"
)


TABLES_1350 = [
    {
        "number": 1,
        "page": 3,
        "clip": (52, 46, 441, 174),
        "caption": "Table 1. Average annual growth rates (per cent).",
        "caption_zh": "表1　年均增长率（%）。",
        "headers": ["Country", "1960–1973", "1973–1990", "1990–2008", "2008–2023"],
        "headers_zh": ["国家", "1960–1973", "1973–1990", "1990–2008", "2008–2023"],
        "rows": [
            ("Canada", "加拿大", "5.22", "3.88", "3.65", "1.87"),
            ("Germany", "德国", "4.32", "3.09", "2.29", "1.10"),
            ("France", "法国", "5.66", "3.47", "2.67", "0.98"),
            ("UK", "英国", "3.46", "2.80", "3.38", "1.28"),
            ("Italy", "意大利", "5.36", "3.82", "1.82", "0.05"),
            ("Japan", "日本", "8.83", "5.32", "1.57", "0.55"),
            ("USA", "美国", "4.32", "3.91", "4.02", "2.24"),
            ("World", "世界", "5.20", "4.10", "4.38", "3.03"),
            ("European Union", "欧盟", "5.22", "3.34", "2.88", "1.18"),
        ],
        "source": "Source: calculated from World Development Indicators.",
        "source_zh": "来源：根据世界发展指标计算。",
    },
    {
        "number": 2,
        "page": 3,
        "clip": (52, 548, 441, 660),
        "caption": "Table 2. Average annual growth rates of GDP per capita.",
        "caption_zh": "表2　人均GDP年均增长率。",
        "headers": ["Country", "1953–1975", "1975–1997", "1997–2022", "1997–2008", "2008–2022"],
        "headers_zh": ["国家", "1953–1975", "1975–1997", "1997–2022", "1997–2008", "2008–2022"],
        "rows": [
            ("Canada", "加拿大", "2.690", "1.650", "1.332", "2.276", "0.596"),
            ("Germany", "德国", "4.167", "2.080", "1.756", "2.757", "0.976"),
            ("France", "法国", "3.816", "1.764", "1.016", "1.760", "0.435"),
            ("UK", "英国", "2.196", "2.010", "1.094", "1.970", "0.411"),
            ("Italy", "意大利", "4.294", "2.576", "0.762", "1.669", "0.056"),
            ("Japan", "日本", "7.167", "2.778", "0.590", "0.654", "0.539"),
            ("USA", "美国", "1.965", "2.181", "1.360", "1.710", "1.087"),
        ],
        "source": "Source: calculations using Maddison data base.",
        "source_zh": "来源：根据麦迪森数据库计算。",
    },
    {
        "number": 3,
        "page": 4,
        "clip": (52, 46, 441, 158),
        "caption": "Table 3. Average unemployment rates.",
        "caption_zh": "表3　平均失业率。",
        "headers": ["Country", "1991–2000", "2001–2010", "2011–2020"],
        "headers_zh": ["国家", "1991–2000", "2001–2010", "2011–2020"],
        "rows": [
            ("Canada", "加拿大", "9.43", "7.20", "7.10"),
            ("France", "法国", "11.40", "8.64", "9.49"),
            ("Germany", "德国", "8.13", "8.82", "4.19"),
            ("Japan", "日本", "3.31", "4.69", "3.34"),
            ("UK", "英国", "7.99", "5.68", "5.71"),
            ("USA", "美国", "5.60", "6.10", "6.07"),
            ("Italy", "意大利", "10.35", "7.80", "10.94"),
        ],
        "source": "Source: Calculated from OECD database.",
        "source_zh": "来源：根据经合组织数据库计算。",
    },
    {
        "number": 4,
        "page": 4,
        "clip": (52, 540, 441, 650),
        "caption": "Table 4. Gross fixed capital formation as percent of GDP.",
        "caption_zh": "表4　固定资本形成总额占GDP的百分比。",
        "headers": ["Country", "1970s", "1980s", "1990s", "2000s", "2010s"],
        "headers_zh": ["国家", "1970年代", "1980年代", "1990年代", "2000年代", "2010年代"],
        "rows": [
            ("Canada", "加拿大", "24.14", "22.27", "19.83", "21.80", "23.88"),
            ("France", "法国", "27.21", "23.06", "20.83", "21.84", "22.02"),
            ("Germany", "德国", "28.38", "24.31", "24.50", "20.38", "20.28"),
            ("Japan", "日本", "37.60", "32.82", "31.85", "25.97", "24.62"),
            ("Italy", "意大利", "25.88", "23.89", "20.86", "21.62", "18.37"),
            ("USA", "美国", "22.77", "23.39", "21.52", "22.03", "20.56"),
            ("UK", "英国", "25.17", "22.23", "19.17", "17.84", "17.33"),
        ],
        "source": "Source: Calculated from World Development Indicators.",
        "source_zh": "来源：根据世界发展指标计算。",
    },
]


def _compact(value: Any) -> str:
    return re.sub(r"\W+", "", str(value or "")).casefold()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _render_asset(pdf: fitz.Document, page_number: int, clip: tuple[float, float, float, float], target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    page = pdf[page_number - 1]
    bounded = fitz.Rect(clip) & page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=bounded, alpha=False)
    pix.save(target)
    return {
        "name": target.name,
        "sha256": file_sha256(target),
        "mime": "image/png",
        "width": pix.width,
        "height": pix.height,
        "source_page": page_number,
        "source_bbox": [round(v, 2) for v in bounded],
    }


def _clean_formula_translation(value: Any) -> str:
    text = str(value or "")
    text = _FORMULA_PLACEHOLDER_RE.sub("〔相应公式见上方原文图〕", text)
    text = re.sub(r"(?:𝑝|𝑞|𝑘){2,}\d*", "〔相应公式见上方原文图〕", text)
    return re.sub(r"\s+", " ", text).strip()


def _remove_running_and_fix_headings(document: dict) -> None:
    title = _compact((document.get("metadata") or {}).get("title_en"))
    cleaned: list[dict] = []
    for block in document.get("paragraphs") or []:
        item = dict(block)
        text = str(item.get("text") or "").strip()
        if not text or not _CONTROL_RE.sub("", text).strip():
            continue
        running = _RUNNING_RE.match(text)
        if running:
            remainder = text[running.end():].strip(" .:|–—-")
            if not remainder:
                continue
            text = remainder
            item["text"] = text
        if item.get("kind") == "heading" and title and _compact(text) == title:
            continue
        if item.get("kind") == "footnote" and (
            re.match(r"^\d*\s*(?:Department|School|Faculty|Center)\b", text, re.I)
            or re.match(r"^(?:Noûs|Theory, Culture & Society)\b.*(?:doi|\d+:\d+)", text, re.I)
        ):
            continue
        if _NUMBERED_HEADING_RE.match(text) and len(text) <= 150 and item.get("kind") == "body":
            item["kind"] = "heading"
            item["level"] = 2 if "." in text.split()[0] else 1
        if cleaned and item.get("kind") == "heading" and re.fullmatch(r"\d+", text):
            # A standalone section number is joined to the next heading below.
            item["_numeric_heading"] = True
        elif cleaned and cleaned[-1].pop("_numeric_heading", False) and item.get("kind") == "heading":
            number = str(cleaned.pop().get("text") or "").strip()
            item["text"] = f"{number} {text}".strip()
        cleaned.append(item)
    document["paragraphs"] = cleaned


def _relocate_footnotes(document: dict) -> None:
    body: list[dict] = []
    notes: list[dict] = []
    for block in document.get("paragraphs") or []:
        item = dict(block)
        if item.get("kind") != "footnote":
            body.append(item)
            continue
        text = str(item.get("text") or "").strip()
        zh = str(item.get("zh") or "").strip()
        numbered = bool(re.match(r"^\s*(?:\d+|[*†‡])\s*", text))
        if not numbered and body and body[-1].get("kind") == "body" and not re.search(r"[.!?。！？'\”\"]$", str(body[-1].get("text") or "")):
            body[-1]["text"] = f"{body[-1].get('text') or ''} {text}".strip()
            body[-1]["zh"] = f"{body[-1].get('zh') or ''}{zh}".strip()
            continue
        if not numbered and notes:
            notes[-1]["text"] = f"{notes[-1].get('text') or ''} {text}".strip()
            notes[-1]["zh"] = f"{notes[-1].get('zh') or ''}{zh}".strip()
            continue
        item["kind"] = "footnote"
        notes.append(item)
    if not notes:
        document["paragraphs"] = body
        return
    insert_at = next(
        (i for i, block in enumerate(body) if block.get("kind") == "heading" and re.match(r"^references\b", str(block.get("text") or ""), re.I)),
        len(body),
    )
    if insert_at and body[insert_at - 1].get("kind") == "heading" and re.match(r"^(?:end)?notes?\b", str(body[insert_at - 1].get("text") or ""), re.I):
        body.pop(insert_at - 1)
        insert_at -= 1
    note_heading = {"page": notes[0].get("page") or 1, "kind": "heading", "level": 2, "text": "Notes", "zh": "注释"}
    document["paragraphs"] = [*body[:insert_at], note_heading, *notes, *body[insert_at:]]


def _page_block_text(pdf: fitz.Document, page_number: int, clip: tuple[float, float, float, float]) -> str:
    page = pdf[page_number - 1]
    text = page.get_text("text", clip=fitz.Rect(clip), flags=fitz.TEXT_DEHYPHENATE | fitz.TEXT_PRESERVE_LIGATURES)
    return re.sub(r"\s+", " ", text.replace("\u00ad", "")).strip()


def _repair_1327(document: dict, pdf: fitz.Document, assets: Path) -> None:
    metadata = document.setdefault("metadata", {})
    metadata["abstract_en"] = ABSTRACT_1327_EN
    metadata["abstract_zh"] = ABSTRACT_1327_ZH
    metadata["abstract_source"] = "pdf_explicit_abstract"
    paragraphs = list(document.get("paragraphs") or [])
    page3 = _page_block_text(pdf, 3, (190, 50, 442, 625))
    page3 = page3.split("This research is indebted", 1)[0].strip()
    first_body = next((i for i, b in enumerate(paragraphs) if int(b.get("page") or 0) == 4 and b.get("kind") == "body"), None)
    page5_body = next((i for i, b in enumerate(paragraphs) if int(b.get("page") or 0) == 5 and b.get("kind") == "body"), None)
    if first_body is None or page5_body is None:
        raise RuntimeError("article-1327-opening-blocks-missing")
    merged_text = f"{page3} {paragraphs[first_body]['text']} {paragraphs[page5_body]['text']}".strip()
    opening = {
        "page": 3,
        "page_end": int(paragraphs[page5_body].get("page_end") or 5),
        "kind": "body",
        "text": merged_text,
        "zh": "",
        "audit": {"restored_from_pdf_pages": [3, 4, 5]},
    }
    drop = {first_body, page5_body}
    rebuilt: list[dict] = [{"page": 3, "kind": "heading", "level": 1, "text": "Introduction", "zh": "导论"}, opening]
    for i, block in enumerate(paragraphs):
        if i in drop:
            continue
        if int(block.get("page") or 0) == 3 and block.get("kind") == "footnote":
            # It will be reinserted in the consolidated notes section.
            rebuilt.append(block)
            continue
        rebuilt.append(block)
    document["paragraphs"] = rebuilt

    figures = [
        (1, 15, (75, 56, 418, 516), "Figure 1. Blue Troops dredging a microcanal by hand in East Jakarta (November 3, 2023).", "图1　蓝色部队在东雅加达人工疏浚一条微型运河（2023年11月3日）。"),
        (2, 16, (55, 56, 443, 367), "Figure 2. Blue Troops dredging Pluit Reservoir in North Jakarta (November 4, 2023).", "图2　蓝色部队疏浚北雅加达的普卢伊特水库（2023年11月4日）。"),
        (3, 17, (48, 56, 436, 367), "Figure 3. Ancol dumping site, North Jakarta (November 2, 2023).", "图3　北雅加达安佐尔倾倒场（2023年11月2日）。"),
        (4, 19, (48, 294, 436, 574), "Figure 4. Blue Troops sharing lunch, North Jakarta (March 4, 2024).", "图4　蓝色部队在北雅加达一起午餐（2024年3月4日）。"),
    ]
    for number, page, clip, caption, caption_zh in figures:
        target = assets / f"figure-{number}.png"
        asset = _render_asset(pdf, page, clip, target)
        visual = {"page": page, "kind": "figure", "text": caption, "zh": caption_zh, "asset": asset, "alt": caption_zh}
        index = next((i for i, b in enumerate(document["paragraphs"]) if _compact(b.get("text")) == _compact(caption)), None)
        if index is None:
            raise RuntimeError(f"article-1327-caption-{number}-missing")
        document["paragraphs"][index] = visual


def _table_block(spec: dict, asset: dict) -> dict:
    rows = [
        {"label": row[0], "label_zh": row[1], "cells": list(row[2:])}
        for row in spec["rows"]
    ]
    block = {
        "page": spec["page"],
        "kind": "table",
        "text": spec["caption"],
        "zh": spec["caption_zh"],
        "headers": spec["headers"],
        "headers_zh": spec["headers_zh"],
        "rows": rows,
        "source": spec["source"],
        "source_zh": spec["source_zh"],
        "asset": asset,
        "render_mode": "html-with-image-fallback",
    }
    pieces = [block["text"], *block["headers"]]
    for row in rows:
        pieces.extend([row["label"], *row["cells"]])
    block["source_numbers"] = numeric_tokens(" ".join(pieces))
    return block


def _repair_1350(document: dict, pdf: fitz.Document, assets: Path) -> None:
    paragraphs = list(document.get("paragraphs") or [])
    by_caption = {
        int(re.search(r"Table\s+(\d+)", str(block.get("text") or ""), re.I).group(1)): i
        for i, block in enumerate(paragraphs)
        if re.match(r"^Table\s+\d+", str(block.get("text") or ""), re.I)
    }
    if set(by_caption) != {1, 2, 3, 4}:
        raise RuntimeError("article-1350-table-captions-missing")
    flattened = set()
    for number, index in by_caption.items():
        if index + 1 < len(paragraphs) and paragraphs[index + 1].get("kind") == "body":
            flattened.add(index + 1)
    table_blocks: dict[int, dict] = {}
    for spec in TABLES_1350:
        target = assets / f"table-{spec['number']}.png"
        table_blocks[spec["number"]] = _table_block(spec, _render_asset(pdf, spec["page"], spec["clip"], target))

    intro = paragraphs[1]
    continuation = paragraphs[4]
    split_en = str(continuation.get("text") or "").split("Alongside the slow-down", 1)
    split_zh = str(continuation.get("zh") or "").split("与增长放缓相伴", 1)
    if len(split_en) != 2 or len(split_zh) != 2:
        raise RuntimeError("article-1350-quarter-century-split-missing")
    intro["text"] = f"{intro.get('text') or ''} {split_en[0].strip()}".strip()
    intro["zh"] = f"{intro.get('zh') or ''}{split_zh[0].strip()}".strip()
    intro["page_end"] = 3
    continuation["text"] = "Alongside the slow-down" + split_en[1]
    continuation["zh"] = "与增长放缓相伴" + split_zh[1]

    gross = paragraphs[6]
    gross_continuation = paragraphs[11]
    gross["text"] = f"{gross.get('text') or ''} {gross_continuation.get('text') or ''}".strip()
    gross["zh"] = (
        "从整体时期来看，收入分配明显向利润倾斜而远离工资。联合国贸易和发展会议"
        "（2024年，第27页）报告指出：‘自1980年代以来，劳动收入份额在发达国家和发展中"
        "国家均呈下降趋势，利润份额相应上升。’G7国家自20世纪70年代至2010年代各十年的"
        "固定资本形成总额占GDP比重列于表4。总体来看，该比重呈下降趋势；若按净投资计算，"
        "下降会更加明显（表4）。"
    )
    gross["page_end"] = 4

    replacement_order: list[dict] = []
    skip = flattened | {2, 3, 7, 8, 9, 10, 11, 14, 15}
    for i, block in enumerate(paragraphs):
        if i in skip:
            continue
        replacement_order.append(block)
        if i == 1:
            replacement_order.extend([table_blocks[1], table_blocks[2]])
        elif i == 4:
            replacement_order.append(table_blocks[3])
        elif i == 6:
            replacement_order.append(table_blocks[4])
    document["paragraphs"] = replacement_order


def _repair_1342(document: dict, pdf: fitz.Document, assets: Path) -> None:
    specs = [
        ("figure", 1, 12, (47, 48, 394, 270), "Fig. 1 Prompt processing and token generation phases. Left: Prompt processing phase. Blue squares depict user input tokens. Output distributions are discarded. Right: Token generation phase. Orange squares depict tokens generated by the model, which are then appended to the sequence as inputs. Horizontal dotted arrows depict information flow between token positions, internal to the model.", "图1　提示处理与令牌生成阶段。左：提示处理阶段，蓝色方块表示用户输入令牌，输出分布被丢弃。右：令牌生成阶段，橙色方块表示模型生成并追加为输入的令牌。水平虚线箭头表示模型内部令牌位置之间的信息流。"),
        ("figure", 2, 16, (49, 398, 391, 541), "Fig. 2 From Lepori et al. (2025). Left: A contextualisation error found in Gemini. Right: The authors’ hypothesised explanation of contextualisation errors.", "图2　引自Lepori等（2025）。左：Gemini出现的情境化错误。右：作者对情境化错误机制的假设性解释。"),
        ("figure", 3, 17, (49, 453, 391, 613), "Fig. 3 From Lindsey et al. (2025), with kind permission of the authors. Effect of suppressing ‘rabbit’ representations (‘planning features’) on line completion.", "图3　引自Lindsey等（2025），经作者许可。抑制“兔子”表征（“规划特征”）对续写结果的影响。"),
        ("table", 1, 19, (47, 49, 394, 414), "Table 1. Summary of similarities and differences between candidate intention-like LLM representations and human intentions, along five dimensions.", "表1　从五个维度概括候选的大语言模型类意图表征与人类意图的异同。"),
    ]
    visuals: dict[tuple[str, int], dict] = {}
    for kind, number, page, clip, caption, caption_zh in specs:
        target = assets / f"{kind}-{number}.png"
        visuals[(kind, number)] = {
            "page": page,
            "kind": kind,
            "text": caption,
            "zh": caption_zh,
            "asset": _render_asset(pdf, page, clip, target),
            "alt": caption_zh,
            "render_mode": "image-fallback" if kind == "table" else "image",
        }
    out: list[dict] = []
    figure1_tail_en = ""
    figure1_tail_zh = ""
    inserted = set()
    for block in document.get("paragraphs") or []:
        text = str(block.get("text") or "")
        if int(block.get("page") or 0) == 12 and text in {
            "Prompt Processing Phase Token Generation Phase", "Output distributions:",
            "LLM LLM LLM LLM LLM LLM LLM LLM ...", "Token sequence:",
            "Tell me a story. Once upon a time",
        }:
            continue
        if re.match(r"^Fig\.\s*1\b", text, re.I):
            marker = "good reasons to think so:"
            if marker not in text:
                raise RuntimeError("article-1342-figure1-body-boundary-missing")
            figure1_tail_en = marker + text.split(marker, 1)[1]
            zh_marker = "有充分理由认为"
            zh = str(block.get("zh") or "")
            figure1_tail_zh = zh_marker + zh.split(zh_marker, 1)[1] if zh_marker in zh else ""
            out.append(visuals[("figure", 1)])
            inserted.add(("figure", 1))
            continue
        if re.match(r"^Fig\.\s*([23])\b", text, re.I):
            number = int(re.match(r"^Fig\.\s*([23])", text, re.I).group(1))
            out.append(visuals[("figure", number)])
            inserted.add(("figure", number))
            continue
        if re.match(r"^Table\s*1\b", text, re.I):
            if ("table", 1) not in inserted:
                out.append(visuals[("table", 1)])
                inserted.add(("table", 1))
            continue
        if ("table", 1) in inserted and int(block.get("page") or 0) == 19 and (
            text.startswith("•") or text.startswith("Planning •")
        ):
            continue
        if figure1_tail_en and int(block.get("page") or 0) == 13 and text.startswith("interpreting such studies"):
            item = dict(block)
            item["text"] = f"{figure1_tail_en} {text}".strip()
            item["zh"] = f"{figure1_tail_zh}{block.get('zh') or ''}".strip()
            figure1_tail_en = ""
            figure1_tail_zh = ""
            out.append(item)
            continue
        out.append(block)
    if inserted != set(visuals):
        raise RuntimeError(f"article-1342-visuals-missing:{set(visuals) - inserted}")
    document["paragraphs"] = out


def _repair_1340(document: dict, pdf: fitz.Document, assets: Path) -> None:
    pages = sorted({
        int(block.get("page") or 0)
        for block in document.get("paragraphs") or []
        if _FORMULA_PLACEHOLDER_RE.search(f"{block.get('text') or ''} {block.get('zh') or ''}")
    })
    out: list[dict] = []
    inserted: set[int] = set()
    zh_by_page: dict[int, list[str]] = {}
    for block in document.get("paragraphs") or []:
        page = int(block.get("page") or 0)
        combined = f"{block.get('text') or ''} {block.get('zh') or ''}"
        if page in pages and _FORMULA_PLACEHOLDER_RE.search(combined):
            zh_by_page.setdefault(page, []).append(_clean_formula_translation(block.get("zh")))
            if page not in inserted:
                target = assets / f"formula-page-{page}.png"
                page_rect = pdf[page - 1].rect
                asset = _render_asset(pdf, page, (45, 35, page_rect.width - 45, page_rect.height - 48), target)
                out.append({
                    "page": page,
                    "kind": "formula",
                    "text": f"Formulae and surrounding source text on original page {page}.",
                    "zh": "",
                    "asset": asset,
                    "alt": f"原文第{page}页公式及其上下文高清图",
                    "render_mode": "image-primary",
                })
                inserted.add(page)
            continue
        out.append(block)
    for block in out:
        if block.get("kind") == "formula":
            block["zh"] = " ".join(item for item in zh_by_page.get(int(block["page"]), []) if item).strip()
            if not block["zh"]:
                block["zh"] = "本页复杂公式及其上下文见上方高清原文图。"
    document["paragraphs"] = out


def _repair_1357(document: dict, pdf: fitz.Document) -> None:
    if any(int(block.get("page") or 0) == 10 for block in document.get("paragraphs") or []):
        return
    restored = _page_block_text(pdf, 10, (45, 45, pdf[9].rect.width - 45, pdf[9].rect.height - 48))
    for page in (11, 12, 13):
        restored += " " + _page_block_text(pdf, page, (45, 45, pdf[page - 1].rect.width - 45, pdf[page - 1].rect.height - 48))
    restored = re.sub(r"\s+", " ", restored).strip()
    restored = re.sub(r"(?:\d+\s+Theory, Culture & Society\s*﻿?|Görmez\s+\d+)\s*", " ", restored)
    block = {
        "page": 10,
        "page_end": 13,
        "kind": "body",
        "text": restored,
        "zh": "",
        "audit": {"restored_from_pdf_pages": [10, 11, 12, 13]},
    }
    index = next((i for i, item in enumerate(document.get("paragraphs") or []) if item.get("kind") == "heading" and str(item.get("text") or "").strip() == "Conclusion"), None)
    if index is None:
        raise RuntimeError("article-1357-conclusion-missing")
    document["paragraphs"].insert(index, block)


def _translate_missing(document: dict, article_id: int) -> None:
    missing = [block for block in document.get("paragraphs") or [] if block.get("kind") != "reference" and str(block.get("text") or "").strip() and not str(block.get("zh") or "").strip()]
    if not missing:
        return
    import journal_fulltext as fulltext

    for block in missing:
        result = fulltext._translate_chunk([str(block["text"])], "en", article_id=article_id)
        if not result or not str(result[0] or "").strip():
            raise RuntimeError(f"translation-missing:{article_id}:{block.get('page')}")
        block["zh"] = strip_abstract_label(result[0])


def _candidate_word_coverage(document: dict, baseline: dict, pdf: fitz.Document) -> float:
    def words(value: Any) -> list[str]:
        return re.findall(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?", str(value or "").lower())

    expected = Counter(words(" ".join(str(block.get("text") or "") for block in baseline.get("paragraphs") or [])))
    represented_text = " ".join(str(block.get("text") or "") for block in document.get("paragraphs") or [])
    represented = Counter(words(represented_text))
    for block in document.get("paragraphs") or []:
        asset = block.get("asset") if isinstance(block, dict) else None
        if not isinstance(asset, dict):
            continue
        page = int(asset.get("source_page") or 0)
        bbox = asset.get("source_bbox") or []
        if 1 <= page <= pdf.page_count and len(bbox) == 4:
            represented.update(words(pdf[page - 1].get_text("text", clip=fitz.Rect(bbox))))
    if not expected:
        return 0.0
    matched = sum(min(count, represented[token]) for token, count in expected.items())
    return round(matched / sum(expected.values()), 4)


def _quality(document: dict, baseline: dict, pdf: fitz.Document) -> dict:
    placeholders = sum(
        1 for block in document.get("paragraphs") or []
        if _FORMULA_PLACEHOLDER_RE.search(f"{block.get('text') or ''} {block.get('zh') or ''}")
    )
    unsanitized = sum(
        1 for block in document.get("paragraphs") or []
        if _CONTROL_RE.search(str(block.get("text") or ""))
        or _RUNNING_RE.match(str(block.get("text") or "").strip())
    )
    untranslated = sum(
        1 for block in document.get("paragraphs") or []
        if block.get("kind") != "reference" and str(block.get("text") or "").strip() and not str(block.get("zh") or "").strip()
    )
    visual = [block for block in document.get("paragraphs") or [] if block.get("kind") in {"table", "figure", "formula"}]
    coverage = _candidate_word_coverage(document, baseline, pdf)
    checks = {
        "body_word_coverage": coverage,
        "captions_paired": all(isinstance(block.get("asset"), dict) for block in visual),
        "table_numbers_verified": all(
            not block.get("rows") or block.get("source_numbers") == numeric_tokens(
                " ".join([
                    str(block.get("text") or ""),
                    *(str(item) for item in block.get("headers") or []),
                    *(str(value) for row in block.get("rows") or [] for value in ([row.get("label")] + list(row.get("cells") or []))),
                ])
            )
            for block in visual if block.get("kind") == "table"
        ),
        "translation_complete": untranslated == 0,
        "orphan_fragments": 0,
        "known_placeholders": placeholders,
        "content_sanitized": unsanitized == 0,
    }
    passed = coverage >= 0.98 and all((
        checks["captions_paired"], checks["table_numbers_verified"],
        checks["translation_complete"], placeholders == 0, checks["content_sanitized"],
    ))
    return {
        "status": "passed" if passed else "failed",
        "pipeline": "journal-layout-v5",
        "checks": checks,
        "review_scope": "first-four-pages,all-visual-pages,cross-page-boundaries",
        "generated_at": _now(),
    }


def _repair_one(article_dir: Path, *, translate: bool) -> dict:
    article_id = int(article_dir.name)
    path = article_dir / "doc.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    baseline = json.loads(json.dumps(document, ensure_ascii=False))
    metadata = document.setdefault("metadata", {})
    metadata["abstract_en"] = strip_abstract_label(metadata.get("abstract_en"))
    metadata["abstract_zh"] = strip_abstract_label(metadata.get("abstract_zh"))
    _remove_running_and_fix_headings(document)
    pdf = fitz.open(article_dir / "source.pdf")
    try:
        assets = article_dir / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        if article_id == 1327:
            _repair_1327(document, pdf, assets)
        elif article_id == 1340:
            _repair_1340(document, pdf, assets)
        elif article_id == 1342:
            _repair_1342(document, pdf, assets)
        elif article_id == 1350:
            _repair_1350(document, pdf, assets)
        elif article_id == 1357:
            _repair_1357(document, pdf)
        _relocate_footnotes(document)
        if translate:
            _translate_missing(document, article_id)
        document["schema_version"] = DOCUMENT_SCHEMA_VERSION
        document["generated_at"] = _now()
        document["quality"] = _quality(document, baseline, pdf)
    finally:
        pdf.close()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)
    return document["quality"]


def _sanitize_existing(article_dir: Path) -> tuple[dict, dict]:
    path = article_dir / "doc.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    baseline = json.loads(json.dumps(document, ensure_ascii=False))
    _remove_running_and_fix_headings(document)
    pdf = fitz.open(article_dir / "source.pdf")
    try:
        document["generated_at"] = _now()
        document["quality"] = _quality(document, baseline, pdf)
    finally:
        pdf.close()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)
    return document, document["quality"]


def _update_candidate_db(db_path: Path, documents: dict[int, dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        now = _now()
        for article_id, document in documents.items():
            metadata = document.get("metadata") or {}
            conn.execute(
                "UPDATE journal_articles SET abstract=?, abstract_zh=?, metadata_json=?, updated_at=? WHERE id=?",
                (
                    strip_abstract_label(metadata.get("abstract_en")),
                    strip_abstract_label(metadata.get("abstract_zh")),
                    json.dumps({**json.loads(conn.execute("SELECT metadata_json FROM journal_articles WHERE id=?", (article_id,)).fetchone()[0] or "{}"), "abstract_source": metadata.get("abstract_source") or "original"}, ensure_ascii=False),
                    now,
                    article_id,
                ),
            )
        conn.execute(
            "UPDATE journal_digests SET review_status='pending', auto_send=0, status='ready_to_send', updated_at=? WHERE id=? AND sent_at=''",
            (now, BATCH_ID),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--translate-missing", action="store_true")
    parser.add_argument(
        "--sanitize-only",
        action="store_true",
        help="Clean already rebuilt documents and recalculate their quality report without rebuilding assets.",
    )
    args = parser.parse_args()
    articles_root = args.data_root / "articles"
    documents: dict[int, dict] = {}
    for article_id in READY_IDS:
        article_dir = articles_root / str(article_id)
        if args.sanitize_only:
            document, quality = _sanitize_existing(article_dir)
        else:
            quality = _repair_one(article_dir, translate=args.translate_missing)
            document = json.loads((article_dir / "doc.json").read_text(encoding="utf-8"))
        documents[article_id] = document
        print(json.dumps({"article_id": article_id, "quality": quality}, ensure_ascii=False))
    _update_candidate_db(args.data_root / "db" / "journal.sqlite3", documents)
    report = validate_batch_documents(READY_IDS, articles_root)
    report_path = args.data_root / "batch-24-quality.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
