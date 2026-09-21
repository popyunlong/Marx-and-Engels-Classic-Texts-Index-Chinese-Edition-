# -*- coding: utf-8 -*-
"""用非公开总目录复核1949—1966各册的题名、年份和印刷页码。

总目录只作为第二来源读取，永不写入 pages/toc_entries。输出是可审计 JSON；
``--strict`` 在缺页码、题名低相似或年份冲突时失败，供候选库验收使用。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BOOK = "中共中央文件选集（1949—1966）"
AUX_BOOK = BOOK + "总目录"
DEFAULT_DB = ROOT / "tmp" / "new_corpus_202609" / "corpus.sqlite"
DEFAULT_REPORT = ROOT / "tmp" / "new_corpus_202609" / "central_files_index_crosscheck.json"
SIDECAR = ROOT / "data" / "party_files_1949_index_vol01_glm.jsonl"

_CN_DIGITS = {"零": 0, "〇": 0, "○": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_END_PAGE = re.compile(r"(?:[.．…·]{2,}|\s{2,})(\d{1,4})\s*$")
_VOLUME = re.compile(r"第([一二三四五六七八九十百零〇○]+)册\s*[（(]([^）)]+)[）)].*?(\d{1,4})\s*$")
_ARABIC_YEAR = re.compile(r"(?<!\d)((?:18|19|20)\d{2})\s*年")
_CN_YEAR = re.compile(r"([一二三四五六七八九零〇○]{4})\s*年")


def cn_number(text: str) -> int:
    if all(ch in _CN_DIGITS for ch in text):
        return int("".join(str(_CN_DIGITS[ch]) for ch in text))
    total = current = 0
    for ch in text:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
    return total + current


def _norm(text: str) -> str:
    text = (text or "").replace("的的", "的")
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", text).lower()


def _year(text: str) -> int | None:
    match = _ARABIC_YEAR.search(text or "")
    if match:
        return int(match.group(1))
    match = _CN_YEAR.search(text or "")
    if match:
        return int("".join(str(_CN_DIGITS[ch]) for ch in match.group(1)))
    return None


def load_aux_text() -> dict[int, str]:
    if not SIDECAR.exists():
        raise RuntimeError(f"缺少总目录识别结果：{SIDECAR}")
    latest: dict[int, str] = {}
    for line in SIDECAR.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            latest[int(row["pdf_page"])] = str(row.get("text") or "")
        except Exception:
            continue
    expected = int((_aux_spec()).get("page_count") or 0)
    missing = set(range(1, expected + 1)) - set(latest)
    if missing:
        raise RuntimeError(f"总目录识别缺页：{sorted(missing)[:12]}")
    corrections_path = ROOT / "config" / "new_corpus_corrections_202609.yaml"
    corrections = yaml.safe_load(corrections_path.read_text(encoding="utf-8")) or {}
    for item in corrections.get("auxiliary_pages") or []:
        if str(item.get("book")) != AUX_BOOK:
            continue
        page = int(item["pdf_page"])
        original, replacement = str(item["original"]), str(item["replacement"])
        pattern = re.compile(r"\s*".join(re.escape(ch) for ch in original))
        if not pattern.search(latest.get(page, "")):
            raise RuntimeError(f"总目录校订原文未命中：p{page} {original}")
        latest[page] = pattern.sub(replacement, latest[page], count=1)
    return latest


def _aux_spec() -> dict:
    payload = yaml.safe_load((ROOT / "config" / "auxiliary_sources.yaml").read_text(encoding="utf-8")) or {}
    return next(dict(row) for row in payload.get("sources") or [] if row.get("key") == AUX_BOOK)


def parse_volume_map(text_by_page: dict[int, str]) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for pdf_page in range(7, 12):
        for raw in str(text_by_page.get(pdf_page) or "").splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            match = _VOLUME.search(line)
            if not match:
                continue
            volume = cn_number(match.group(1))
            result[volume] = {
                "date_span": match.group(2).strip(),
                "master_printed_page": int(match.group(3)),
                "evidence_pdf_page": pdf_page,
            }
    return result


def parse_aux_entries(text_by_page: dict[int, str], first_pdf: int, last_pdf: int) -> list[dict]:
    entries: list[dict] = []
    pending: list[str] = []
    for pdf_page in range(first_pdf, last_pdf + 1):
        for raw in str(text_by_page.get(pdf_page) or "").splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            if not line or re.fullmatch(r"\d{1,4}", line):
                continue
            if "中共中央文件选集" in line or line in {"总目录", "目录", "目次", "目 次"}:
                continue
            if re.search(r"第[一二三四五六七八九十百零〇○]+册\s*[（(]", line):
                pending.clear()
                continue
            match = _END_PAGE.search(line)
            if not match:
                pending.append(line)
                continue
            head = re.sub(r"[.．…·\s]+$", "", line[:match.start()]).strip()
            if head:
                pending.append(head)
            title = " ".join(pending).strip()
            pending.clear()
            if len(_norm(title)) < 3:
                continue
            entries.append({
                "title": title,
                "printed_page": int(match.group(1)),
                "year": _year(title),
                "evidence_pdf_page": pdf_page,
            })
    return entries


def _score(left: str, right: str) -> float:
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    if a in b or b in a:
        ratio = max(ratio, min(len(a), len(b)) / max(len(a), len(b)))
    return round(ratio, 4)


def _partial_score(needle: str, window: str) -> float:
    """候选题名对总目录上下文的覆盖率；允许跨页及少量OCR异体。"""
    a, b = _norm(needle), _norm(window)
    if not a or not b:
        return 0.0
    if a in b:
        return 1.0
    longest = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b)).size
    return round(longest / len(a), 4)


def _all_years(text: str) -> set[int]:
    years = {int(match.group(1)) for match in _ARABIC_YEAR.finditer(text or "")}
    for match in _CN_YEAR.finditer(text or ""):
        years.add(int("".join(str(_CN_DIGITS[ch]) for ch in match.group(1))))
    return years


def _volume_text_and_pages(text_by_page: dict[int, str], first_pdf: int, last_pdf: int) -> tuple[str, list[tuple[int, int, int]]]:
    chunks: list[str] = []
    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for pdf_page in range(first_pdf, last_pdf + 1):
        chunk = str(text_by_page.get(pdf_page) or "") + "\n"
        chunks.append(chunk)
        spans.append((cursor, cursor + len(chunk), pdf_page))
        cursor += len(chunk)
    return "".join(chunks), spans


def _page_windows(text: str, printed_page: int, radius: int) -> list[tuple[int, str]]:
    # 页码只认点引线/大段空白后的数字，不把成文日期、卷次或页眉数字误当目录落点。
    pattern = re.compile(rf"(?:[.．…·]{{2,}}|\s{{2,}}){printed_page}(?=\s|$)")
    return [(match.start(), text[max(0, match.start() - radius):min(len(text), match.end() + radius)])
            for match in pattern.finditer(text)]


def crosscheck(db: Path) -> dict:
    texts = load_aux_text()
    volume_map = parse_volume_map(texts)
    map_problems: list[str] = []
    if set(volume_map) != set(range(1, 51)):
        map_problems.append(f"总目录卷次映射不完整：{sorted(set(range(1, 51)) - set(volume_map))}")

    manifest = yaml.safe_load((ROOT / "config" / "manifest.yaml").read_text(encoding="utf-8")) or {}
    expected_spans = {int(row["volume"]): str(row.get("date_span") or "") for row in manifest.get(BOOK) or []}
    for volume, item in volume_map.items():
        expected_years = re.findall(r"(?:19|20)\d{2}", expected_spans.get(volume, ""))
        actual_years = []
        for match in _CN_YEAR.finditer(item["date_span"]):
            actual_years.append("".join(str(_CN_DIGITS[ch]) for ch in match.group(1)))
        actual_years += _ARABIC_YEAR.findall(item["date_span"])
        if expected_years and actual_years and set(expected_years) != set(actual_years):
            map_problems.append(f"第{volume}册年份范围冲突：manifest={expected_spans[volume]} 总目录={item['date_span']}")

    with sqlite3.connect(db) as conn:
        candidate = conn.execute(
            "SELECT volume,title,printed_page,pdf_page FROM toc_entries "
            "WHERE book=? AND kind='body' ORDER BY volume,sort_order", (BOOK,),
        ).fetchall()
    by_volume: dict[int, list[tuple]] = {}
    for row in candidate:
        by_volume.setdefault(int(row[0]), []).append(row)
    missing_candidate_volumes = [volume for volume in range(1, 51) if not by_volume.get(volume)]
    if missing_candidate_volumes:
        map_problems.append(f"候选目录缺册：{missing_candidate_volumes}")

    checked: list[dict] = []
    problems: list[dict] = []
    unmatched_aux = 0
    master_page_markers = 0
    volume_stats: dict[str, dict] = {}
    starts = {volume: item["master_printed_page"] for volume, item in volume_map.items()}
    page_offset = 11  # 总目录印刷第1页 == PDF第12页
    for volume in range(1, 51):
        if volume not in starts:
            continue
        first_pdf = starts[volume] + page_offset
        last_printed = (starts.get(volume + 1, 546) - 1)
        last_pdf = min(int(_aux_spec()["page_count"]), last_printed + page_offset)
        aux_entries = parse_aux_entries(texts, first_pdf, last_pdf)
        volume_text, page_spans = _volume_text_and_pages(texts, first_pdf, last_pdf)
        # Count parsed bibliography entries rather than raw numeric markers.
        # Page headers/footers such as ``2 中共中央文件选集`` also satisfy the
        # whitespace+digits layout rule and used to inflate the unmatched count.
        marker_count = len(aux_entries)
        master_page_markers += marker_count
        matched_aux_entries: set[int] = set()
        for _v, title, printed, pdf_page in by_volume.get(volume, []):
            try:
                printed_int = int(str(printed))
            except (TypeError, ValueError):
                problem = {"volume": volume, "title": title, "candidate_pdf_page": pdf_page,
                           "reason": "candidate-missing-printed-page"}
                problems.append(problem)
                checked.append(problem)
                continue
            radius = max(260, min(900, len(str(title)) * 5))
            windows = _page_windows(volume_text, printed_int, radius)
            if not windows:
                problem = {"volume": volume, "title": title, "printed_page": printed_int,
                           "candidate_pdf_page": pdf_page, "reason": "printed-page-not-in-master-index"}
                problems.append(problem)
                checked.append(problem)
                continue
            marker, snippet = max(windows, key=lambda pair: _partial_score(str(title), pair[1]))
            window_score = _partial_score(str(title), snippet)
            aux_options = [
                (index, entry) for index, entry in enumerate(aux_entries)
                if int(entry["printed_page"]) == printed_int and index not in matched_aux_entries
            ]
            if not aux_options:
                aux_options = [
                    (index, entry) for index, entry in enumerate(aux_entries)
                    if int(entry["printed_page"]) == printed_int
                ]
            aux_match = max(
                aux_options,
                key=lambda pair: _partial_score(str(title), str(pair[1]["title"])),
                default=None,
            )
            aux_score = (
                _partial_score(str(title), str(aux_match[1]["title"])) if aux_match else 0.0
            )
            score = max(window_score, aux_score)
            if aux_match and aux_score >= 0.72:
                matched_aux_entries.add(aux_match[0])
            candidate_year = _year(str(title))
            master_years = _all_years(snippet)
            if aux_match:
                master_years.update(_all_years(str(aux_match[1]["title"])))
            reason = "ok"
            if score < 0.72:
                reason = "title-low-similarity"
            elif candidate_year and candidate_year not in master_years:
                reason = "year-conflict"
            evidence_page = (
                int(aux_match[1]["evidence_pdf_page"]) if aux_match
                else next((p for start, end, p in page_spans if start <= marker < end), first_pdf)
            )
            item = {
                "volume": volume, "title": title, "printed_page": printed_int,
                "candidate_pdf_page": pdf_page, "master_evidence_pdf_page": evidence_page,
                "master_evidence_snippet": snippet, "similarity": score,
                "candidate_year": candidate_year, "master_years_in_window": sorted(master_years),
                "reason": reason,
            }
            checked.append(item)
            if reason != "ok":
                problems.append(item)
        unmatched_aux += max(0, marker_count - len(matched_aux_entries))
        volume_stats[str(volume)] = {
            "candidate_entries": len(by_volume.get(volume, [])),
            "master_page_markers": marker_count,
            "line_parser_entries_for_audit": len(aux_entries),
            "matched_distinct_markers": len(matched_aux_entries),
        }

    return {
        "book": BOOK, "master_index_public": False, "volume_map_count": len(volume_map),
        "candidate_entries": len(candidate), "checked_entries": len(checked),
        "master_page_markers": master_page_markers,
        "unmatched_master_entries": unmatched_aux, "volume_map_problems": map_problems,
        "volume_stats": volume_stats, "problems": problems,
        "ok": not map_problems and not problems and unmatched_aux == 0,
        "items": checked,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    report = crosscheck(args.db)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "ok", "volume_map_count", "candidate_entries", "checked_entries",
        "unmatched_master_entries", "volume_map_problems")}, ensure_ascii=False))
    if args.strict and not report["ok"]:
        raise SystemExit(
            f"总目录复核失败：{len(report['problems'])} 条题名/页码问题，"
            f"{report['unmatched_master_entries']} 条总目录未匹配"
        )


if __name__ == "__main__":
    main()
