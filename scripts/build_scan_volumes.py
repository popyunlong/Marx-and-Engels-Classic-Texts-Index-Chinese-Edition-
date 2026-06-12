# -*- coding: utf-8 -*-
"""把扫描卷（邓小平文选第1、2卷 / 胡锦涛文选全三卷）的 OCR 正文就地注入 corpus.sqlite。

正文取自本地 OCR sidecar（scripts/_ocr_scan_volume.py 生成的 data/*_ocr.jsonl，
服务器无 OCR 依赖，上传 sidecar 后在服务器读取注入——同毛文集第6卷模式）。

印刷页码：从 OCR 文本的页眉/页脚独立数字行检测，再取「pdf页−印刷页」偏移量的
众数推出全卷常量偏移（扫描件一页一面、偏移恒定，对 OCR 误读免疫）；若众数
覆盖率不足则回退为逐页检测＋fill_missing_printed_pages 顺序补全。

目录：
- 邓1、邓2 无 PDF 书签，由 OCR 正文「篇名（一九××年…日）」识别篇首页推导
  （best-effort，限正文页、去重相邻同名）——同 build_maowenji_vol6 思路。
- 胡选三卷书签目录已由 build_toc 写入，此处仅回填 toc_entries.printed_page。

只 DELETE/INSERT 指定 book+volume 的行，其余书库与卷不动；完成重算 sha256。
须在 build_textbook_index + build_toc 之后运行（覆盖扫描卷的空行）。

用法：python scripts/build_scan_volumes.py [--only dengxuan_vol1 ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import BUILD_DB_PATH, fill_missing_printed_pages, normalize  # noqa: E402

HASH_PATH = BUILD_DB_PATH.with_suffix(BUILD_DB_PATH.suffix + ".sha256")

VOLUMES = [
    {
        "id": "dengxuan_vol1",
        "book": "邓小平文选",
        "volume": 1,
        "source_file": "pdfs/邓小平文选/邓小平文选（第1卷）1994年版.pdf",
        "sidecar": "data/dengxuan_vol1_ocr.jsonl",
        "toc": "detect",
    },
    {
        "id": "dengxuan_vol2",
        "book": "邓小平文选",
        "volume": 2,
        "source_file": "pdfs/邓小平文选/邓小平文选（第2卷）1994年版.pdf",
        "sidecar": "data/dengxuan_vol2_ocr.jsonl",
        "toc": "detect",
    },
    {
        "id": "huxuan_vol1",
        "book": "胡锦涛文选",
        "volume": 1,
        "source_file": "pdfs/胡锦涛文选/胡锦涛文选（第一卷）_B_01016717_001.pdf",
        "sidecar": "data/huxuan_vol1_ocr.jsonl",
        "toc": "refresh_printed",
    },
    {
        "id": "huxuan_vol2",
        "book": "胡锦涛文选",
        "volume": 2,
        "source_file": "pdfs/胡锦涛文选/胡锦涛文选（第二卷）_B_01016720_001.pdf",
        "sidecar": "data/huxuan_vol2_ocr.jsonl",
        "toc": "refresh_printed",
    },
    {
        "id": "huxuan_vol3",
        "book": "胡锦涛文选",
        "volume": 3,
        "source_file": "pdfs/胡锦涛文选/胡锦涛文选（第三卷）_B_01016724_001.pdf",
        "sidecar": "data/huxuan_vol3_ocr.jsonl",
        "toc": "refresh_printed",
    },
    {
        "id": "zgl_vol3",
        "book": "治国理政",
        "volume": 3,
        "source_file": "pdfs/《治国理政》/《治国理政》第三卷.pdf",
        "sidecar": "data/zgl_vol3_ocr.jsonl",
        "toc": "detect",
    },
    # 卷2 扫描版（2022-09 版本）：书签为「页码书签」（1..569 → pdf−19），无篇章书签，
    # 目录由 OCR 正文识别 + 印刷目录页补齐；build_toc 会插入数字垃圾目录，本脚本覆盖之。
    {
        "id": "zgl_vol2",
        "book": "治国理政",
        "volume": 2,
        "source_file": "pdfs/《治国理政》/《治国理政》第二卷.pdf",
        "sidecar": "data/zgl_vol2_ocr.jsonl",
        "toc": "detect",
    },
    # 卷4 扫描版：134 条干净篇章书签（build_toc 写入），此处仅回填 printed_page。
    {
        "id": "zgl_vol4",
        "book": "治国理政",
        "volume": 4,
        "source_file": "pdfs/《治国理政》/《治国理政》第四卷.pdf",
        "sidecar": "data/zgl_vol4_ocr.jsonl",
        "toc": "refresh_printed",
    },
]

# 篇首页特征：开头(去页码后)即「篇名（一九××年…日/月）」。日期可为时间段
# （如「一九四一年四月十五日—六月十日」），尾部放宽到 12 字。
_DATE = re.compile(
    r"[（(]\s*[一二三四五六七八九十百零〇○二〇\d]{2,}\s*年[^）)]{0,16}?[日月][^）)]{0,12}[)）]"
)
_LEAD_NUM = re.compile(r"^[\s\d]+")
_PAGE_NUM_LINE = re.compile(r"^\s*(\d{1,4})\s*$")
# 标题里不应出现的字符：句中标点（正文特征）与目录页特征（省略号/页码连线）
_TITLE_BAD = set("。！？；，、⋯…·.0123456789—–-")


def load_sidecar(path: Path) -> dict[int, str]:
    if not path.exists():
        raise SystemExit(f"缺少 OCR sidecar：{path}（请先本地跑 scripts/_ocr_scan_volume.py 并上传）")
    texts: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        texts[int(o["pdf_page"])] = o.get("text") or ""
    return texts


def detect_page_number(text: str, page_count: int) -> int | None:
    """从页眉/页脚找独立数字行（前 3 行与后 3 行），返回印刷页码。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    for l in lines[:3] + lines[-3:]:
        m = _PAGE_NUM_LINE.match(l)
        if m:
            v = int(m.group(1))
            if 1 <= v <= page_count + 60:
                return v
    return None


def detect_chapter(text: str) -> str | None:
    """若该页为篇首（开头即 标题+日期），返回篇名；否则 None。"""
    flat = re.sub(r"\s+", "", text)[:80]
    flat = _LEAD_NUM.sub("", flat)  # 去掉行首印刷页码
    m = _DATE.search(flat)
    if not m or m.start() < 2 or m.start() > 44:
        return None
    title = flat[: m.start()].strip()
    if any(ch in _TITLE_BAD for ch in title):
        return None
    if 2 <= len(title) <= 44:
        return title
    return None


def build_rows(spec: dict, texts: dict[int, str]) -> tuple[list[tuple], str]:
    """生成该卷 pages 行（含印刷页码）。返回 (rows, 页码策略说明)。"""
    book, vol, source_file = spec["book"], spec["volume"], spec["source_file"]
    n = max(texts) if texts else 0

    detections: list[tuple[int, int]] = []
    for i in range(1, n + 1):
        v = detect_page_number(texts.get(i, ""), n)
        if v is not None:
            detections.append((i, v))

    offsets = Counter(i - v for i, v in detections)
    strategy = "per-page"
    const_offset: int | None = None
    if detections:
        best, cnt = offsets.most_common(1)[0]
        if cnt >= max(20, len(detections) * 0.6):
            const_offset = best
            strategy = f"const-offset={best}（{cnt}/{len(detections)} 检测点吻合）"

    rows: list[tuple] = []
    for i in range(1, n + 1):
        raw = texts.get(i, "")
        if const_offset is not None:
            printed = str(i - const_offset) if i - const_offset >= 1 else None
        else:
            v = detect_page_number(raw, n)
            printed = str(v) if v is not None else None
        rows.append((book, vol, source_file, i, printed, raw, normalize(raw)))

    if const_offset is None:
        # 邻居偏移补全：检测点缺失页取前/后最近检测点的偏移，两侧一致才回填；
        # 不一致（无页码插页带，偏移在此跳变）保持 None。首/尾延伸段用单侧偏移。
        # 比 fill_missing 的顺序补全更稳：开篇未检出段、卷末索引段都能按邻段偏移补齐。
        # 只用「偏移获得 ≥3 个检测点支持」的可信锚点（前置目录页自带页码会产生杂散偏移）
        trusted = [(i, v) for i, v in detections if offsets[i - v] >= 3]
        det_pages = [i for i, _v in trusted]
        det_off = {i: i - v for i, v in trusted}
        import bisect
        filled = 0
        for idx in range(len(rows)):
            b, v_, sf, i, printed, raw, norm = rows[idx]
            if printed is not None or not norm:
                continue
            k = bisect.bisect_left(det_pages, i)
            off_prev = det_off[det_pages[k - 1]] if k > 0 else None
            off_next = det_off[det_pages[k]] if k < len(det_pages) else None
            off = None
            if off_prev is not None and off_next is not None:
                if off_prev == off_next:
                    off = off_prev
            else:
                off = off_prev if off_prev is not None else off_next
            if off is not None and i - off >= 1:
                rows[idx] = (b, v_, sf, i, str(i - off), raw, norm)
                filled += 1
        strategy += f"，邻居偏移补全 {filled} 页"
    return rows, strategy


def detect_toc(rows: list[tuple]) -> list[dict]:
    """从正文页识别篇章（标题+日期开头的页）。"""
    toc: list[dict] = []
    for _book, _vol, _sf, pdf_page, printed, raw, _norm in rows:
        if not printed:  # 仅正文页（有印刷页码）参与
            continue
        title = detect_chapter(raw)
        if title:
            toc.append({"title": title, "pdf_page": pdf_page, "printed": printed})
    toc.sort(key=lambda e: e["pdf_page"])
    dedup: list[dict] = []
    for e in toc:
        if dedup and dedup[-1]["title"] == e["title"]:
            continue
        dedup.append(e)
    return dedup


# ---- 印刷目录页解析（邓1/邓2 等无书签卷的目录权威来源）----
# 目录页版式：标题（可跨行，行尾常带点引线/页码区间），下一行为「（一九××年…日）」日期括注。
_TOC_TRAIL = re.compile(r"[·⋯…\.、：:\s\d—一–\-]+$")
_TOC_HEADER = re.compile(r"^(目录|邓小平文选|习近平谈治国理政|治国理政|第[一二三四五]卷)$")
_TOC_SECTION = re.compile(r"^[一二三四五六七八九十]{1,3}、")


def _flat_norm(s: str) -> str:
    return normalize(re.sub(r"\s+", "", s))


def parse_printed_toc(texts: dict[int, str], first_body_pdf: int) -> list[str]:
    """从前置目录页解析篇名列表（按出现顺序）。只取标题，不信目录页里的页码
    （OCR 常把「33—44」并成「3344」），定位交给正文匹配。"""
    start = None
    for i in range(1, min(first_body_pdf, 30)):
        if any(l.strip() == "目录" for l in (texts.get(i) or "").splitlines()):
            start = i
            break
    if start is None:
        # 回退：目录页特征 = 单页含 ≥2 条「日期括注独行」（扫描 OCR 偶尔丢失「目录」标题行）
        for i in range(1, min(first_body_pdf, 30)):
            lines = [l.strip() for l in (texts.get(i) or "").splitlines() if l.strip()]
            ndates = sum(1 for l in lines
                         if _DATE.search(re.sub(r"\s+", "", l))
                         and len(re.sub(r"[（(].*?[)）]", "", re.sub(r"\s+", "", l)).strip()) <= 2)
            if ndates >= 2:
                start = i
                break
    if start is None:
        return []
    titles: list[str] = []
    acc: list[str] = []
    for i in range(start, first_body_pdf):
        for line in (texts.get(i) or "").splitlines():
            l = line.strip()
            if not l or _TOC_HEADER.match(l):
                continue
            if _TOC_SECTION.match(l) and not acc:
                continue  # 专题头（治国理政），不计入篇名
            flat = re.sub(r"\s+", "", l)
            if _DATE.search(flat) and len(re.sub(r"[（(].*?[)）]", "", flat).strip()) <= 2:
                # 日期行 → 收束当前条目
                title = _TOC_TRAIL.sub("", "".join(acc)).strip()
                if 2 <= len(title) <= 50:
                    titles.append(title)
                acc = []
                continue
            acc.append(l)
    return [t for t in titles if t != "注释"]


def merge_printed_toc(toc: list[dict], texts: dict[int, str], rows: list[tuple],
                      first_body_pdf: int) -> tuple[list[dict], int]:
    """用印刷目录补齐正文检测漏掉的篇章。返回 (合并后的目录, 补齐条数)。"""
    import difflib

    parsed = parse_printed_toc(texts, first_body_pdf)
    if not parsed:
        return toc, 0

    def match(a: str, b: str) -> bool:
        na, nb = _flat_norm(a), _flat_norm(b)
        if not na or not nb:
            return False
        if na == nb or na in nb or nb in na:
            return True
        return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.75

    max_page = max((r[3] for r in rows), default=0)
    merged: list[dict] = []
    added = 0
    det = list(toc)  # 已按 pdf_page 升序
    for k, title in enumerate(parsed):
        hit = next((e for e in det if match(title, e["title"])), None)
        if hit is not None:
            det.remove(hit)
            merged.append(hit)
            continue
        # 未检出 → 在相邻已定位条目之间的页窗口内按「页首含标题」搜索
        lo = merged[-1]["pdf_page"] + 1 if merged else first_body_pdf
        nxt = next((e for e in det if any(match(t, e["title"]) for t in parsed[k + 1:])), None)
        hi = nxt["pdf_page"] - 1 if nxt else max_page
        key = _flat_norm(title)[:10]
        found = None
        for _b, _v, _sf, p, pr, raw, _n in rows:
            if p < lo or p > hi or not pr:
                continue
            head = _flat_norm(re.sub(r"\s+", "", raw)[:70])
            if key and key in head[:44]:
                found = (p, pr)
                break
        if found:
            merged.append({"title": title, "pdf_page": found[0], "printed": found[1]})
            added += 1
    # 检出但不在印刷目录里的（目录页 OCR 烂掉等）也保留，按页序插回
    merged.extend(det)
    merged.sort(key=lambda e: e["pdf_page"])
    return merged, added


def update_hash() -> None:
    digest = hashlib.sha256()
    with BUILD_DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="注入扫描卷 OCR 正文与页码/目录。")
    ap.add_argument("--only", nargs="*", help="只处理指定卷 id（默认全部五卷）")
    args = ap.parse_args()
    specs = [s for s in VOLUMES if not args.only or s["id"] in args.only]
    if not specs:
        raise SystemExit("没有匹配的卷。可选：" + ", ".join(s["id"] for s in VOLUMES))

    conn = sqlite3.connect(str(BUILD_DB_PATH))
    try:
        for spec in specs:
            texts = load_sidecar(ROOT / spec["sidecar"])
            expected = conn.execute(
                "SELECT MAX(pdf_page) FROM pages WHERE book=? AND volume=?",
                (spec["book"], spec["volume"]),
            ).fetchone()[0]
            if expected and (not texts or max(texts) < expected or len(texts) < expected):
                raise SystemExit(
                    f"[{spec['id']}] OCR sidecar 不完整：{len(texts)}/{expected} 页"
                    f"（max={max(texts) if texts else 0}）。请等 OCR 跑完再注入。"
                )
            rows, strategy = build_rows(spec, texts)
            nonempty = sum(1 for r in rows if r[6])
            conn.execute(
                "DELETE FROM pages WHERE book=? AND volume=?", (spec["book"], spec["volume"])
            )
            conn.executemany(
                "INSERT INTO pages (book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            msg = f"[{spec['id']}] pages {len(rows)} 行（非空 {nonempty}），页码策略：{strategy}"

            if spec["toc"] == "detect":
                toc = detect_toc(rows)
                first_body = next((r[3] for r in rows if r[4]), 1)
                toc, added = merge_printed_toc(toc, texts, rows, first_body)
                conn.execute(
                    "DELETE FROM toc_entries WHERE book=? AND volume=?",
                    (spec["book"], spec["volume"]),
                )
                conn.executemany(
                    "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, printed_page, level, kind, sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (spec["book"], spec["volume"], spec["source_file"], e["title"],
                         e["pdf_page"], e["printed"], 1, "body", k + 1)
                        for k, e in enumerate(toc)
                    ],
                )
                msg += f"；识别目录 {len(toc)} 条（印刷目录补齐 {added} 条）"
            elif spec["toc"] == "refresh_printed":
                # 书名页书签会把全名带进界面目录（治国理政文案分流要求），删除之；不动正文篇名。
                conn.execute(
                    "DELETE FROM toc_entries WHERE book=? AND volume=? AND title LIKE '%习近平谈治国理政%'",
                    (spec["book"], spec["volume"]),
                )
                cur = conn.execute(
                    "UPDATE toc_entries SET printed_page = ("
                    "  SELECT p.printed_page FROM pages p"
                    "  WHERE p.source_file = toc_entries.source_file AND p.pdf_page = toc_entries.pdf_page)"
                    " WHERE book=? AND volume=?",
                    (spec["book"], spec["volume"]),
                )
                msg += f"；目录 printed_page 回填 {cur.rowcount} 条"
            print(msg)
        conn.commit()
    finally:
        conn.close()
    update_hash()
    print("完成。")


if __name__ == "__main__":
    main()
