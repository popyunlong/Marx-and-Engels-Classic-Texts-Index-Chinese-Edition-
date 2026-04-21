"""
索引构建脚本：把 PDF 文件抽取成可搜索的 SQLite 库。

改进点：
1. 不再只看 raw_text 的前2行/后2行，而是优先查看页面边缘区域的 words。
2. 放宽页码格式识别：支持 86 / [86] / (86) / —86— / 86.
3. 对同一 PDF 内的漏识别页做顺序补全：如果前后页页码连续且中间差值唯一，则自动填补。
4. 新增“空白页感知补全”：若缺失段中包含明显空白页，则允许只给非空白页补页码，空白页保持 None。
5. 运行时优先读取 exe 同级目录下的 data/corpus.sqlite；若不存在，则回退到打包进 exe 的 data/corpus.sqlite。
   这样既支持“单 exe 发布”，也支持“exe + 外置 data 目录发布”。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import fitz  # pymupdf
import yaml


# 路径处理：
# - _BUNDLE：PyInstaller onefile 运行时的临时解压目录；未打包时即脚本目录
# - _EXEDIR：exe 所在目录；未打包时也等于脚本目录
if getattr(sys, "frozen", False):
    _BUNDLE = Path(sys._MEIPASS)
    _EXEDIR = Path(sys.executable).resolve().parent
else:
    _BUNDLE = Path(__file__).resolve().parent
    _EXEDIR = _BUNDLE

ROOT = _BUNDLE
PDF_ROOT = _EXEDIR / "pdfs"
MANIFEST = _BUNDLE / "config" / "manifest.yaml"
VOLUMES = _BUNDLE / "config" / "volumes.yaml"

EXTERNAL_DATA_DIR = _EXEDIR / "data"
BUNDLED_DATA_DIR = _BUNDLE / "data"
BUILD_DB_PATH = EXTERNAL_DATA_DIR / "corpus.sqlite"


def _resolve_runtime_db_path() -> Path:
    """
    运行时索引路径选择规则：
    1) 优先使用 exe 同级目录 data/corpus.sqlite（便于外置更新索引）；
    2) 若不存在，再使用打包进 exe 的 data/corpus.sqlite（便于单文件发布）；
    3) 两者都没有时，仍返回外置路径，方便上层报错提示。
    """
    external = EXTERNAL_DATA_DIR / "corpus.sqlite"
    bundled = BUNDLED_DATA_DIR / "corpus.sqlite"

    if external.exists():
        return external
    if bundled.exists():
        return bundled
    return external


# 兼容 app.py 的既有导入方式：from build_index import DB_PATH
DB_PATH = _resolve_runtime_db_path()

# 所有需要在归一化时被剥离的字符类型：标点、空白、控制符
_STRIP_RE = re.compile(
    r"[\s\u3000"
    r"\u2000-\u206f"          # 通用标点
    r"\u2e00-\u2e7f"          # 补充标点
    r"\u3000-\u303f"          # CJK 符号和标点
    r"\uff00-\uffef"          # 全角 ASCII / 半角片假名
    r"!-/:-@\[-`\{-~"         # ASCII 标点
    r"]+"
)

# 允许更宽松的页码形式：86 / [86] / (86) / —86— / 86.
_PAGE_TOKEN_RE = re.compile(
    r"^\s*[\[\(（【<〈『「\-—–·\.]*\s*(\d{1,4})\s*[\]\)）】>〉』」\-—–·\.]*\s*$"
)
_ROMAN_TOKEN_RE = re.compile(
    r"^\s*[\[\(（【<〈『「\-—–·\.]*\s*([ivxlcdm]{1,10})\s*[\]\)）】>〉』」\-—–·\.]*\s*$",
    re.IGNORECASE,
)

# 粗判空白页用的阈值：尽量保守
_BLANK_NORM_LEN_MAX = 20
_BLANK_VISIBLE_CHAR_MAX = 8


def normalize(text: str) -> str:
    """把文字归一化：剥离所有标点和空白。"""
    text = unicodedata.normalize("NFKC", text)
    text = _STRIP_RE.sub("", text)
    return text



def _parse_page_token(text: str) -> str | None:
    """把候选字符串解析为页码。"""
    s = unicodedata.normalize("NFKC", text).strip()
    if not s:
        return None

    m = _PAGE_TOKEN_RE.match(s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 3000:
            return str(n)

    m = _ROMAN_TOKEN_RE.match(s)
    if m:
        return f"pre-{m.group(1).lower()}"

    return None



def _score_candidate(
    text: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    page_width: float,
    page_height: float,
) -> tuple[int, str] | None:
    """
    对候选页码打分。越靠近上/下边缘、越短、越孤立、越靠左右边或底部中部，分数越高。
    返回 (score, parsed_value)。
    """
    parsed = _parse_page_token(text)
    if not parsed:
        return None

    score = 0
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    width = x1 - x0
    height = y1 - y0

    # 边缘优先
    if cy <= page_height * 0.12:
        score += 6
    if cy >= page_height * 0.88:
        score += 8

    # 左下 / 右下 / 底中 都给分
    if cx <= page_width * 0.30:
        score += 3
    elif cx >= page_width * 0.70:
        score += 3
    elif page_width * 0.35 <= cx <= page_width * 0.65:
        score += 2

    # 字串越短越像页码
    clean_len = len(normalize(text))
    if clean_len <= 4:
        score += 5
    elif clean_len <= 8:
        score += 2

    # 小块优先
    if width <= page_width * 0.20:
        score += 2
    if height <= page_height * 0.05:
        score += 2

    # 罗马页码稍微降权，防止误识别正文里的罗马字母
    if parsed.startswith("pre-"):
        score -= 1

    return score, parsed



def detect_printed_page_from_text(raw_text: str) -> str | None:
    """
    兼容旧逻辑：从页面首尾几行中提取印刷页码。
    这里只作为兜底，避免完全依赖行顺序。
    """
    lines = [ln for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        return None

    candidates = []
    if len(lines) <= 6:
        candidates.extend(lines)
    else:
        candidates.extend(lines[:3])
        candidates.extend(lines[-3:])

    for ln in candidates:
        parsed = _parse_page_token(ln)
        if parsed:
            return parsed
    return None



def detect_printed_page_from_page(page: fitz.Page) -> str | None:
    """
    从 page 对象中识别印刷页码：
    1) 优先从 words 中看页面顶部/底部边缘区域；
    2) 再退回到 raw_text 的前后几行。
    """
    page_rect = page.rect
    page_width = float(page_rect.width)
    page_height = float(page_rect.height)

    candidates: list[tuple[int, str]] = []

    # words: (x0, y0, x1, y1, word, block_no, line_no, word_no)
    try:
        words = page.get_text("words")
    except Exception:
        words = []

    for w in words:
        x0, y0, x1, y1, token = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
        # 只看上下边缘区域
        if not (y1 <= page_height * 0.18 or y0 >= page_height * 0.82):
            continue
        scored = _score_candidate(token, x0, y0, x1, y1, page_width, page_height)
        if scored:
            candidates.append(scored)

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # blocks 兜底：有些页码可能被合成一个短块
    try:
        blocks = page.get_text("blocks")
    except Exception:
        blocks = []

    for b in blocks:
        x0, y0, x1, y1, text = float(b[0]), float(b[1]), float(b[2]), float(b[3]), str(b[4])
        if not (y1 <= page_height * 0.18 or y0 >= page_height * 0.82):
            continue
        for part in [p for p in text.splitlines() if p.strip()]:
            scored = _score_candidate(part, x0, y0, x1, y1, page_width, page_height)
            if scored:
                candidates.append(scored)

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # 最后才回退到 raw_text 行顺序法
    raw = page.get_text("text")
    return detect_printed_page_from_text(raw)



def _as_int_page(value: str | None) -> int | None:
    if value is None or value.startswith("pre-"):
        return None
    try:
        return int(value)
    except ValueError:
        return None



def is_probably_blank_page(raw_text: str, norm_text: str) -> bool:
    """
    粗判是否为空白页/隔页。
    只在文本极少时返回 True，尽量避免把短正文页误判为空白页。
    """
    if len(norm_text) > _BLANK_NORM_LEN_MAX:
        return False

    visible = []
    for ch in unicodedata.normalize("NFKC", raw_text):
        if ch.isspace():
            continue
        if unicodedata.category(ch).startswith(("P", "Z", "C")):
            continue
        visible.append(ch)

    return len(visible) <= _BLANK_VISIBLE_CHAR_MAX



def fill_missing_printed_pages(rows: list[tuple[str, int, int, str | None, str, str]]) -> int:
    """
    对同一 PDF 内的 printed_page 缺失项做顺序补全。

    两层策略：
    1) 严格连续补全：前后页码差值与页距完全一致时，全部补上。
    2) 空白页感知补全：若缺失段内含空白页，则允许“只补非空白页”，
       只要前后页码差值与“非空白缺失页数量 + 1”一致。
    返回补全数量。
    """
    filled = 0
    rows_mut = list(rows)

    values = [_as_int_page(r[3]) for r in rows_mut]
    n = len(values)
    i = 0
    while i < n:
        if values[i] is not None:
            i += 1
            continue

        start = i - 1
        j = i
        while j < n and values[j] is None:
            j += 1
        end = j  # 第一个非空的位置

        if not (start >= 0 and end < n and values[start] is not None and values[end] is not None):
            i = j
            continue

        left = values[start]
        right = values[end]
        gap = end - start

        # 方案1：完全连续，全部补
        if right - left == gap:
            for k in range(start + 1, end):
                inferred = str(left + (k - start))
                book, vol, pdf_page, _printed, raw, norm = rows_mut[k]
                rows_mut[k] = (book, vol, pdf_page, inferred, raw, norm)
                values[k] = int(inferred)
                filled += 1
            i = j
            continue

        # 方案2：空白页感知补全，只给非空白页补
        segment_indexes = list(range(start + 1, end))
        nonblank_indexes = [
            k for k in segment_indexes
            if not is_probably_blank_page(rows_mut[k][4], rows_mut[k][5])
        ]

        # 需要填入的非空白页数量 + 两端起点，应该正好对应页码跨度
        # 例如：85 [正文缺失] [空白页] 87
        # 则 right-left = 2, 非空白缺失页数 = 1，可只补正文缺失页为 86。
        if nonblank_indexes and right - left == len(nonblank_indexes) + 1:
            next_num = left + 1
            for k in segment_indexes:
                book, vol, pdf_page, _printed, raw, norm = rows_mut[k]
                if is_probably_blank_page(raw, norm):
                    continue
                inferred = str(next_num)
                rows_mut[k] = (book, vol, pdf_page, inferred, raw, norm)
                values[k] = next_num
                next_num += 1
                filled += 1

        i = j

    rows[:] = rows_mut
    return filled



def guess_volume_from_filename(name: str) -> int | None:
    """从文件名猜测卷号，失败返回 None。"""
    name = unicodedata.normalize("NFKC", name)
    patterns = [
        r"第\s*(\d{1,2})\s*卷",
        r"卷\s*(\d{1,2})",
        r"[Vv]ol\.?\s*(\d{1,2})",
        r"(?<!\d)(\d{1,2})(?!\d)",
    ]
    for pat in patterns:
        m = re.search(pat, name)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 50:
                return n
    return None



def scan_and_write_manifest() -> None:
    """扫描 pdfs/ 目录生成 manifest.yaml 草稿，保留已有条目。"""
    if not PDF_ROOT.exists():
        print(f"未找到目录 {PDF_ROOT}", file=sys.stderr)
        return

    existing: dict[str, dict[str, int]] = {"文集": {}, "全集": {}}
    if MANIFEST.exists():
        try:
            old = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
            for book in ("文集", "全集"):
                for item in old.get(book) or []:
                    existing[book][str(Path(item["file"]).as_posix())] = item["volume"]
        except Exception as e:
            print(f"读取旧 manifest 失败，将重新生成：{e}", file=sys.stderr)

    result: dict[str, list[dict]] = {"文集": [], "全集": []}
    for book in ("文集", "全集"):
        folder = PDF_ROOT / book
        if not folder.exists():
            continue
        for pdf in sorted(folder.glob("*.pdf")):
            rel = pdf.relative_to(_EXEDIR).as_posix()
            vol = existing[book].get(rel) or guess_volume_from_filename(pdf.name)
            entry = {"file": rel, "volume": vol if vol else "# TODO: 请填写卷号"}
            result[book].append(entry)

    MANIFEST.write_text(
        yaml.safe_dump(result, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    n_total = sum(len(v) for v in result.values())
    print(f"已写入 {MANIFEST}，共扫描到 {n_total} 个 PDF。")
    print("请打开此文件核对每个 volume 值，然后运行 `python build_index.py` 构建索引。")



def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS pages;
        CREATE TABLE pages (
            id              INTEGER PRIMARY KEY,
            book            TEXT    NOT NULL,
            volume          INTEGER NOT NULL,
            pdf_page        INTEGER NOT NULL,
            printed_page    TEXT,
            raw_text        TEXT    NOT NULL,
            normalized_text TEXT    NOT NULL
        );
        CREATE INDEX idx_pages_book_vol ON pages(book, volume, pdf_page);
        """
    )



def build() -> None:
    if not MANIFEST.exists():
        print(f"未找到 {MANIFEST}。请先运行 `python build_index.py --scan`。", file=sys.stderr)
        sys.exit(1)

    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}

    BUILD_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if BUILD_DB_PATH.exists():
        BUILD_DB_PATH.unlink()
    conn = sqlite3.connect(BUILD_DB_PATH)
    init_db(conn)

    total_start = time.time()
    total_pages = 0
    total_detected = 0
    total_filled = 0

    for book in ("文集", "全集"):
        items = manifest.get(book) or []
        for item in items:
            vol = item.get("volume")
            if not isinstance(vol, int):
                print(f"跳过 {item.get('file')}：volume 未正确填写 ({vol!r})", file=sys.stderr)
                continue
            pdf_path = (_EXEDIR / item["file"]).resolve()
            if not pdf_path.exists():
                print(f"跳过：文件不存在 {pdf_path}", file=sys.stderr)
                continue

            t0 = time.time()
            detected = 0
            rows: list[tuple[str, int, int, str | None, str, str]] = []

            with fitz.open(pdf_path) as doc:
                for i, page in enumerate(doc, start=1):
                    raw = page.get_text("text")
                    printed = detect_printed_page_from_page(page)
                    if printed and not printed.startswith("pre-"):
                        detected += 1
                    norm = normalize(raw)
                    rows.append((book, vol, i, printed, raw, norm))

            filled = fill_missing_printed_pages(rows)

            conn.executemany(
                "INSERT INTO pages (book, volume, pdf_page, printed_page, raw_text, normalized_text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            elapsed = time.time() - t0
            total_pages += len(rows)
            total_detected += detected
            total_filled += filled

            print(
                f"  [{book}·第{vol:02d}卷] {pdf_path.name}  "
                f"页数={len(rows)}  直接识别={detected}/{len(rows)}  自动补全={filled}  "
                f"耗时={elapsed:.1f}s"
            )

    conn.close()
    total_elapsed = time.time() - total_start
    final_detected = total_detected + total_filled
    print(
        f"\n完成。总页数={total_pages}  直接识别率={total_detected / max(1, total_pages):.1%}  "
        f"补全后识别率={final_detected / max(1, total_pages):.1%}  总耗时={total_elapsed:.1f}s"
    )
    print(f"索引文件：{BUILD_DB_PATH}")
    print(f"运行时默认读取：{DB_PATH}")



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="扫描 pdfs/ 目录生成 manifest.yaml 草稿")
    args = ap.parse_args()

    if args.scan:
        scan_and_write_manifest()
    else:
        build()


if __name__ == "__main__":
    main()
