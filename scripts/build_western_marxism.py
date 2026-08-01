# -*- coding: utf-8 -*-
"""「西马文库」七部单行本的可重复构建管线。

阶段：
  inspect  检查文件、授权记录、SHA-256、页数、文本层与书签。
  ocr      仅把缺失/乱码页交给 glm-4v-flash，按页断点写入 JSONL；可选用 GLM 解析目录页。
  build    在 tmp/pdfs/ 复制库中定向替换 pages/toc_entries，验证后才可 --commit 原子替换正式库。

密钥只从 ZHIPU_API_KEY 读取，不接受命令行密钥，不写入任何产物。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import fitz
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import BUILD_DB_PATH, detect_printed_page_from_page, fill_missing_printed_pages, normalize

SOURCE_CONFIG = ROOT / "config" / "western_marxism_sources.yaml"
TMP_DIR = ROOT / "tmp" / "pdfs"
SIDECAR_DIR = ROOT / "data" / "western_marxism"
MODEL = os.environ.get("ZHIPU_VISION_MODEL", "glm-4v-flash").strip() or "glm-4v-flash"
BASE_URL = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")

OCR_PROMPT = (
    "这是一页中文学术著作的扫描图。请逐字转录页面上实际印刷的文字，保留分段。"
    "只输出转录文字；不要翻译、解释、补全或使用 Markdown。无法辨认的字用「□」。"
)
TOC_PROMPT = (
    "这是一页中文图书目录。请只输出 JSON 数组，每项格式为"
    '{"title":"篇章标题","level":1,"printed_page":"123"}。'
    "不要输出页眉、「目录」二字、省略号或解释。level 取 1-6，printed_page 为原书印刷页码。"
)
META_PREFIX = re.compile(r"^(这是|以下是|图片中|本页|该页|抱歉|无法|根据|页面)")
PRINTED_PAGE_LINE = re.compile(r"^[\s·•‧・—–\-]*(\d{1,4})[\s·•‧・—–\-]*$")


def load_specs() -> list[dict[str, Any]]:
    payload = yaml.safe_load(SOURCE_CONFIG.read_text(encoding="utf-8")) or {}
    return [dict(x) for x in payload.get("books") or [] if isinstance(x, dict)]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_layer_usable(text: str) -> bool:
    """宁可把疑似乱码页送 OCR，也不把坏文本层带入检索库。"""
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 24:
        return False
    cjk = len(re.findall(r"[\u3400-\u9fff]", compact))
    bad = compact.count("�") + compact.count("□")
    return cjk >= 18 and cjk / max(1, len(compact)) >= 0.45 and bad / len(compact) < 0.03


def clean_model_text(text: str) -> tuple[str, bool]:
    text = (text or "").replace("```", "").replace("**", "")
    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"^#+\s*", "", raw.strip())
        if not line or META_PREFIX.match(line):
            continue
        lines.append(line)
    result = "\n".join(lines).strip()
    return result, len(normalize(result)) >= 8


def detect_printed_page_from_text(text: str, page_count: int) -> str | None:
    """OCR 文本中读取页眉/页脚印刷页码，不把正文中的脚注数字当页码。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None
    edge = lines[:3] + lines[-3:]
    for line in edge:
        match = PRINTED_PAGE_LINE.fullmatch(line)
        if match:
            value = int(match.group(1))
            if 1 <= value <= page_count + 200:
                return str(value)
    # 常见页眉：「222 知识分子图书馆」或「保卫马克思 217」。只看首尾行，并要求另一侧有非数字文字。
    patterns = (re.compile(r"^(\d{1,4})\s+\D"), re.compile(r"\D\s+(\d{1,4})$"))
    for line in (lines[0], lines[-1]):
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                value = int(match.group(1))
                if 1 <= value <= page_count + 200:
                    return str(value)
    return None


def page_ink_ratio(page, clip=None) -> float:
    pix = page.get_pixmap(matrix=fitz.Matrix(0.25, 0.25), colorspace=fitz.csGRAY, clip=clip)
    w, h, samples = pix.width, pix.height, pix.samples
    x0, x1, y0, y1 = int(w * .1), int(w * .9), int(h * .1), int(h * .9)
    total = dark = 0
    for y in range(y0, y1):
        row = samples[y * w:(y + 1) * w]
        for value in row[x0:x1]:
            total += 1
            dark += int(value < 190)
    return dark / total if total else 0.0


def is_blank_page(page) -> bool:
    return page_ink_ratio(page) < 0.002


def render_b64(page, scale: float, clip=None) -> str:
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip)
    return base64.b64encode(pix.tobytes("png")).decode("ascii")


def call_glm(api_key: str, image_b64: str, prompt: str, *, max_tokens: int = 1024, retries: int = 6) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}},
        ]}],
        "temperature": 0.05,
        "max_tokens": max_tokens,
    }
    delay = 3.0
    last_error = ""
    for _ in range(retries):
        request = urllib.request.Request(
            BASE_URL + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            choice = payload["choices"][0]
            return {"text": choice["message"]["content"], "finish": choice.get("finish_reason", ""), "usage": payload.get("usage", {})}
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code not in {429, 500, 502, 503, 529}:
                break
        except Exception as exc:  # network errors are retriable
            last_error = str(exc)[:160]
        time.sleep(delay)
        delay = min(60.0, delay * 2)
    return {"text": "", "finish": "error", "error": last_error}


_LOCAL_OCR = None


def local_ocr_page(page, target_width: float = 1800.0) -> str:
    """GLM 连续拒绝的异常页用本地 OCR 保底；不向模型提交空白图，也不会生成性补文。"""
    global _LOCAL_OCR
    import cv2
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    if _LOCAL_OCR is None:
        _LOCAL_OCR = RapidOCR()
    scale = max(1.0, target_width / max(1.0, page.rect.width))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
    image = cv2.imdecode(np.frombuffer(pix.tobytes("png"), np.uint8), cv2.IMREAD_COLOR)
    result, _ = _LOCAL_OCR(image)
    return "\n".join(str(segment[1]) for segment in (result or []) if len(segment) > 1).strip()


def sidecar_path(key: str) -> Path:
    return SIDECAR_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:12] + ".jsonl")


def toc_sidecar_path(key: str) -> Path:
    return SIDECAR_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:12] + ".toc.json")


def load_page_sidecar(path: Path) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if row.get("finish") != "error" and (row.get("blank") or (row.get("ok") and not row.get("trunc"))):
                rows[int(row["pdf_page"])] = row
        except Exception:
            continue
    return rows


def inspect_spec(spec: dict) -> dict:
    path = ROOT / str(spec["file"])
    result = {"key": spec["key"], "file": str(spec["file"]), "exists": path.exists(), "status": spec.get("status")}
    if not path.exists():
        return result
    with fitz.open(path) as doc:
        usable = sum(text_layer_usable(page.get_text("text")) for page in doc)
        result.update({
            "pages": doc.page_count,
            "text_pages": usable,
            "scan_pages": doc.page_count - usable,
            "bookmarks": len(doc.get_toc(simple=True) or []),
            "sha256": sha256_file(path),
            "license_recorded": bool(spec.get("license_basis")),
            "metadata_verified": bool(spec.get("metadata_verified")),
        })
    return result


def run_ocr(spec: dict, *, workers: int, scale: float, limit: int, extract_toc: bool) -> dict:
    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("缺少环境变量 ZHIPU_API_KEY")
    pdf_path = ROOT / str(spec["file"])
    if not pdf_path.exists():
        return {"key": spec["key"], "missing": True}
    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    out = sidecar_path(str(spec["key"]))
    done = load_page_sidecar(out)
    doc = fitz.open(pdf_path)
    todo: list[int] = []
    blanks: list[int] = []
    for n, page in enumerate(doc, 1):
        if text_layer_usable(page.get_text("text")) or n in done:
            continue
        if is_blank_page(page):
            blanks.append(n)
        else:
            todo.append(n)
    if limit:
        todo = todo[:limit]

    with out.open("a", encoding="utf-8") as fh:
        for page_no in blanks:
            fh.write(json.dumps({"pdf_page": page_no, "text": "", "ok": True, "blank": True, "finish": "stop"}, ensure_ascii=False) + "\n")
        fallback_pages: list[int] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures: dict = {}
            inflight: set = set()
            index = completed = 0
            while index < len(todo) or inflight:
                # fitz 渲染留在主线程；最多只缓冲 workers*2 页，避免大书爆内存。
                while index < len(todo) and len(inflight) < max(2, workers * 2):
                    page_no = todo[index]
                    index += 1
                    image = render_b64(doc[page_no - 1], scale)
                    future = pool.submit(call_glm, api_key, image, OCR_PROMPT)
                    futures[future] = page_no
                    inflight.add(future)
                ready = [future for future in inflight if future.done()]
                if not ready:
                    ready = [next(as_completed(inflight))]
                for future in ready:
                    inflight.discard(future)
                    page_no = futures.pop(future)
                    response = future.result()
                    cleaned, ok = clean_model_text(response.get("text", ""))
                    is_truncated = response.get("finish") == "length"
                    row = {"pdf_page": page_no, "text": cleaned, "ok": ok, "finish": response.get("finish", ""), "trunc": is_truncated}
                    if response.get("finish") == "error":
                        row["error"] = response.get("error", "")
                    if is_truncated or response.get("finish") == "error":
                        fallback_pages.append(page_no)
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    completed += 1
                    if completed % 25 == 0 or completed == len(todo):
                        print(f"[{spec['key']}] OCR {completed}/{len(todo)} fallback={len(fallback_pages)}", flush=True)

        # glm-4v-flash 单次最多 1024 tokens；密排正文若被截断，按上/下半页重做并覆盖前记录。
        for page_no in fallback_pages:
            page = doc[page_no - 1]
            rect = page.rect
            overlap = rect.height * 0.025
            clips = [
                fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + rect.height / 2 + overlap),
                fitz.Rect(rect.x0, rect.y0 + rect.height / 2 - overlap, rect.x1, rect.y1),
            ]
            # 单页 HTTP 400 多为图像太大/编码限制；分半页且略降倍率可同时解决请求大小与输出截断。
            retry_scale = min(scale, 1.25)
            responses = []
            for clip in clips:
                if page_ink_ratio(page, clip) < 0.002:
                    responses.append({"text": "", "finish": "stop", "blank_clip": True})
                else:
                    responses.append(call_glm(api_key, render_b64(page, retry_scale, clip), OCR_PROMPT))
            pieces = [clean_model_text(response.get("text", ""))[0] for response in responses]
            ok = all(
                response.get("blank_clip")
                or (clean_model_text(response.get("text", ""))[1] and response.get("finish") != "error")
                for response in responses
            )
            combined = "\n".join(pieces).strip()
            used_local = False
            if not ok:
                combined = local_ocr_page(page)
                ok = len(normalize(combined)) >= 8
                used_local = True
            row = {"pdf_page": page_no, "text": combined, "ok": ok, "finish": "stop" if ok else "error", "trunc": False, "split_retry": True, "local_fallback": used_local}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()

    toc_count = 0
    if extract_toc:
        page_texts = {}
        sidecar = load_page_sidecar(out)
        for n, page in enumerate(doc, 1):
            raw = page.get_text("text")
            page_texts[n] = raw if text_layer_usable(raw) else str(sidecar.get(n, {}).get("text") or "")
        candidates: set[int] = set(int(x) for x in (spec.get("toc_pages") or []) if str(x).isdigit())
        for n, text in page_texts.items():
            if "目录" in text[:500]:
                candidates.update(range(n, min(doc.page_count, n + 12) + 1))
        entries: list[dict] = []
        for n in sorted(candidates):
            response = call_glm(api_key, render_b64(doc[n - 1], scale), TOC_PROMPT)
            entries.extend(parse_toc_response(response.get("text", ""), source_pdf_page=n))
        dedup: dict[tuple[str, str], dict] = {}
        for entry in entries:
            dedup.setdefault((entry["title"], entry["printed_page"]), entry)
        entries = list(dedup.values())
        toc_sidecar_path(str(spec["key"])).write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        toc_count = len(entries)
    doc.close()
    return {"key": spec["key"], "ocr_requested": len(todo), "blank": len(blanks), "toc_entries": toc_count}


def parse_toc_response(text: str, *, source_pdf_page: int = 0) -> list[dict]:
    cleaned = (text or "").strip().replace("```json", "").replace("```", "")
    match = re.search(r"\[.*\]", cleaned, re.S)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    entries: list[dict] = []
    for item in payload if isinstance(payload, list) else []:
        title = re.sub(r"[.…·\s]+\d*\s*$", "", str(item.get("title") or "").strip())
        printed = str(item.get("printed_page") or "").strip()
        if len(title) < 2 or not printed:
            continue
        try:
            level = max(1, min(6, int(item.get("level") or 1)))
        except (TypeError, ValueError):
            level = 1
        entries.append({"title": title, "level": level, "printed_page": printed, "source_pdf_page": source_pdf_page})
    return entries


def load_toc(doc, spec: dict, printed_to_pdf: dict[str, int], pages: list[tuple]) -> list[dict]:
    rows: list[dict] = []
    for level, title, pdf_page, *_ in doc.get_toc(simple=True) or []:
        title = str(title or "").strip()
        if title and 1 <= int(pdf_page) <= doc.page_count:
            rows.append({"title": title, "pdf_page": int(pdf_page), "printed_page": "", "level": max(1, min(6, int(level or 1)))})
    if rows:
        return rows
    sidecar = toc_sidecar_path(str(spec["key"]))
    if not sidecar.exists():
        return []
    items = json.loads(sidecar.read_text(encoding="utf-8"))
    printed_counts: dict[str, int] = {}
    for item in items:
        printed = str(item.get("printed_page") or "")
        printed_counts[printed] = printed_counts.get(printed, 0) + 1
    for item in items:
        printed = str(item.get("printed_page") or "")
        # 视觉模型在密排目录页上偶尔会把一整页的页码都识别成同一个数。
        # 同一 printed_page 大量重复时不信任它，改用「目录标题回查 OCR 正文」定位。
        pdf_page = None if printed_counts.get(printed, 0) > 3 else printed_to_pdf.get(printed)
        title_norm = normalize(str(item.get("title") or ""))
        source_toc_page = int(item.get("source_pdf_page") or 0)
        if title_norm:
            for page_row in pages:
                if int(page_row[3]) <= source_toc_page:
                    continue
                if title_norm in str(page_row[6] or "")[:500]:
                    pdf_page = int(page_row[3])
                    break
        if pdf_page is None and printed_counts.get(printed, 0) <= 3:
            pdf_page = printed_to_pdf.get(printed)
        if pdf_page:
            rows.append({"title": item["title"], "pdf_page": pdf_page, "printed_page": printed, "level": item.get("level", 1)})
    return rows


def build_rows(spec: dict, *, allow_unverified_metadata: bool) -> tuple[list[tuple], list[tuple], dict]:
    if not allow_unverified_metadata and not spec.get("metadata_verified"):
        raise RuntimeError(f"{spec['key']}: 尚未核定版权页/CIP（metadata_verified=false）")
    if not spec.get("license_basis"):
        raise RuntimeError(f"{spec['key']}: 未记录 license_basis")
    pdf_path = ROOT / str(spec["file"])
    if not pdf_path.exists():
        raise RuntimeError(f"{spec['key']}: PDF 缺失 {pdf_path}")
    expected_hash = str(spec.get("sha256") or "").lower()
    actual_hash = sha256_file(pdf_path)
    if expected_hash and expected_hash != actual_hash:
        raise RuntimeError(f"{spec['key']}: SHA-256 与来源清单不一致")

    sidecar = load_page_sidecar(sidecar_path(str(spec["key"])))
    pages: list[tuple] = []
    suspicious: list[dict] = []
    with fitz.open(pdf_path) as doc:
        for n, page in enumerate(doc, 1):
            extracted = page.get_text("text")
            if text_layer_usable(extracted):
                raw = extracted
            else:
                row = sidecar.get(n)
                if row is None:
                    raise RuntimeError(f"{spec['key']}: 第 {n} 页无可用文本且 OCR 未完成")
                if not row.get("blank") and (not row.get("ok") or row.get("trunc")):
                    raise RuntimeError(f"{spec['key']}: 第 {n} 页 OCR 失败或被截断")
                raw = str(row.get("text") or "")
                # 空白/近空白页是视觉模型最容易幻觉的场景。即使 OCR 阶段没被空白阈值拦下，
                # 入库前仍重新测量墨迹：低墨迹却产出长文本时一律拒绝，不让「看似正常」的编造正文入库。
                ink = page_ink_ratio(page)
                reviewed_low_ink = {int(x) for x in (spec.get("reviewed_low_ink_pages") or [])}
                if n not in reviewed_low_ink and not row.get("blank") and ink < 0.004 and len(normalize(raw)) >= 80:
                    suspicious.append({"pdf_page": n, "reason": "low_ink_long_text", "ink_ratio": round(ink, 6), "chars": len(normalize(raw))})
            printed = detect_printed_page_from_page(page) or detect_printed_page_from_text(raw, doc.page_count)
            pages.append((spec["key"], 1, str(Path(spec["file"]).as_posix()), n, printed, raw, normalize(raw)))
        # 相邻页出现长文本完全相同，通常是模型对空白/模糊页重复编造。
        for left, right in zip(pages, pages[1:]):
            if len(left[6]) >= 120 and left[6] == right[6]:
                suspicious.append({"pdf_page": right[3], "reason": "duplicate_adjacent_ocr", "previous_page": left[3], "chars": len(right[6])})
        if suspicious:
            report = TMP_DIR / (hashlib.sha1(str(spec["key"]).encode("utf-8")).hexdigest()[:12] + ".hallucination_review.json")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps({"book": spec["key"], "suspects": suspicious}, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(f"{spec['key']}: 发现 {len(suspicious)} 个空白页/重复文本幻觉疑点，请复核 {report}")
        fill_missing_printed_pages(pages)
        printed_to_pdf = {str(row[4]): int(row[3]) for row in pages if row[4] and not str(row[4]).startswith("pre-")}
        toc = load_toc(doc, spec, printed_to_pdf, pages)
        toc_rows = [
            (spec["key"], 1, str(Path(spec["file"]).as_posix()), row["title"], row["pdf_page"],
             row.get("printed_page") or next((p[4] for p in pages if p[3] == row["pdf_page"]), None),
             row["level"], "body", i)
            for i, row in enumerate(toc, 1)
        ]
    stats = {"key": spec["key"], "pages": len(pages), "text_pages": sum(bool(x[6]) for x in pages), "toc": len(toc_rows), "sha256": actual_hash}
    return pages, toc_rows, stats


def update_hash(path: Path) -> None:
    path.with_suffix(path.suffix + ".sha256").write_text(sha256_file(path) + "\n", encoding="utf-8")


def build_database(specs: list[dict], *, commit: bool, allow_unverified_metadata: bool, allow_empty_toc: bool) -> list[dict]:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stage = TMP_DIR / "western_marxism_corpus.sqlite"
    shutil.copy2(BUILD_DB_PATH, stage)
    all_pages: list[tuple] = []
    all_toc: list[tuple] = []
    stats: list[dict] = []
    for spec in specs:
        pages, toc, item_stats = build_rows(spec, allow_unverified_metadata=allow_unverified_metadata)
        if not toc and not allow_empty_toc:
            raise RuntimeError(f"{spec['key']}: 目录为空，拒绝发布")
        all_pages.extend(pages)
        all_toc.extend(toc)
        stats.append(item_stats)

    selected = [str(s["key"]) for s in specs]
    with sqlite3.connect(stage) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for key in selected:
            conn.execute("DELETE FROM pages WHERE book = ?", (key,))
            conn.execute("DELETE FROM toc_entries WHERE book = ?", (key,))
        conn.executemany("INSERT INTO pages(book,volume,source_file,pdf_page,printed_page,raw_text,normalized_text) VALUES(?,?,?,?,?,?,?)", all_pages)
        conn.executemany("INSERT INTO toc_entries(book,volume,source_file,title,pdf_page,printed_page,level,kind,sort_order) VALUES(?,?,?,?,?,?,?,?,?)", all_toc)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("staging DB integrity_check 失败: " + str(integrity))
        for key in selected:
            page_count = conn.execute("SELECT COUNT(*) FROM pages WHERE book=?", (key,)).fetchone()[0]
            bad_toc = conn.execute("SELECT COUNT(*) FROM toc_entries t WHERE t.book=? AND NOT EXISTS (SELECT 1 FROM pages p WHERE p.source_file=t.source_file AND p.pdf_page=t.pdf_page)", (key,)).fetchone()[0]
            if not page_count or bad_toc:
                raise RuntimeError(f"{key}: 验证失败 pages={page_count} bad_toc={bad_toc}")
        conn.commit()
    # sqlite3.Connection 的上下文管理器只提交/回滚，不会关闭连接。Windows 上若不显式关闭，
    # 同一进程紧接着 os.replace 也会因文件锁失败。
    conn.close()
    update_hash(stage)
    if commit:
        backup = TMP_DIR / "corpus.sqlite.before-western-marxism"
        shutil.copy2(BUILD_DB_PATH, backup)
        os.replace(stage, BUILD_DB_PATH)
        update_hash(BUILD_DB_PATH)
    return stats


def selected_specs(keys: list[str]) -> list[dict]:
    specs = load_specs()
    if not keys:
        return specs
    wanted = set(keys)
    selected = [s for s in specs if str(s.get("key")) in wanted]
    missing = wanted - {str(s.get("key")) for s in selected}
    if missing:
        raise SystemExit("未知书目：" + "、".join(sorted(missing)))
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="构建西马文库引文与目录数据")
    parser.add_argument("command", choices=("inspect", "ocr", "build"), nargs="?", default="inspect")
    parser.add_argument("--book", action="append", default=[], help="仅处理指定书目 key，可重复")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--scale", type=float, default=1.5)
    parser.add_argument("--limit", type=int, default=0, help="OCR 调试：每本最多新处理 N 页")
    parser.add_argument("--extract-toc", action="store_true", help="无可用书签时让 GLM-4V 解析印刷目录")
    parser.add_argument("--commit", action="store_true", help="验证通过后原子替换正式 corpus.sqlite")
    parser.add_argument("--allow-unverified-metadata", action="store_true", help="仅调试：允许 CIP 未复核")
    parser.add_argument("--allow-empty-toc", action="store_true", help="仅调试：允许目录为空")
    args = parser.parse_args()
    specs = selected_specs(args.book)

    if args.command == "inspect":
        print(json.dumps([inspect_spec(s) for s in specs], ensure_ascii=False, indent=2))
    elif args.command == "ocr":
        results = [run_ocr(s, workers=args.workers, scale=args.scale, limit=args.limit, extract_toc=args.extract_toc) for s in specs]
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        results = build_database(specs, commit=args.commit, allow_unverified_metadata=args.allow_unverified_metadata, allow_empty_toc=args.allow_empty_toc)
        print(json.dumps({"committed": args.commit, "books": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
