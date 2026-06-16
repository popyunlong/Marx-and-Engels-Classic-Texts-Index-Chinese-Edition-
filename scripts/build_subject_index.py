"""构建「名目索引」主题检索库：从《文集》各卷卷末名目索引抽取「概念→页码」权威映射，
落地校验后写入 data/subject_index.sqlite，供研究型检索作高精度「主题索引」层。

技术闭环（详见打样记录）：
- 两栏：fitz clip 矩形按左右栏分别抽取，绕开 OCR 串栏。
- 层级解析：OCR 的 ——/一一 连接符归一为 ⊳；有状态扫描区分主词条/子侧面，页码归属当前 (主,子)。
- 落地+校验：印刷页→pdf页(语料 printed↔pdf)；(子或主) 词条 2-gram 出现在落地页(±1) 才入库，
  自动剔除 OCR 错号/编辑泛指。

用法：python scripts/build_subject_index.py [--books 文集]
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

import fitz  # PyMuPDF

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402
from search import normalize  # noqa: E402

_CJK = "一-鿿"
_EDGE = "一—－-→·.、，。:：|‖⊳ \t"
DB_PATH = os.path.join("data", "subject_index.sqlite")


def index_region_pages(doc: "fitz.Document") -> list[int]:
    n = doc.page_count
    return [i for i in range(n) if i > int(n * 0.78) and "名目索引" in doc[i].get_text("text")]


def extract_index_text(doc: "fitz.Document", pages: list[int]) -> str:
    out: list[str] = []
    for i in pages:
        p = doc[i]
        gut = p.rect.width / 2
        for x0, x1 in ((0, gut), (gut, p.rect.width)):
            txt = p.get_text("text", clip=fitz.Rect(x0, 0, x1, p.rect.height))
            for ln in txt.splitlines():
                ln = ln.strip()
                if ln and ln != "名目索引":
                    out.append(ln)
    return "".join(out)


def parse_pages(blob: str, max_printed: int) -> list[int]:
    pages: set[int] = set()
    s = re.sub(r"[一—－→]", "-", blob)
    s = re.sub(r"(\d)[ \t]+(\d)", r"\1\2", s)
    for m in re.finditer(r"(\d{1,4})\s*-\s*(\d{1,4})", s):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b <= max_printed and b - a <= 40:
            pages.update(range(a, b + 1))
    for m in re.finditer(r"\d{1,4}", s):
        v = int(m.group(0))
        if 1 <= v <= max_printed:
            pages.add(v)
    return sorted(pages)


def _clean_label(s: str) -> str:
    return s.strip(_EDGE).strip("一—－-")


def parse_entries(text: str, max_printed: int) -> list[tuple[str, str, list[int]]]:
    canon = re.sub(rf"[一—－→]{{2,}}", "⊳", text)
    toks = re.findall(rf"⊳|[{_CJK}]{{2,}}|\d[\d、，。:：．.\-_ ]*", canon)
    entries: list[tuple[str, str, list[int]]] = []
    main = sub = ""
    cur = None
    after_marker = False
    for tok in toks:
        if tok == "⊳":
            after_marker = True
            continue
        if tok[0].isdigit():
            pages = parse_pages(tok, max_printed)
            if cur and pages:
                entries.append((cur[0], cur[1], pages))
            after_marker = False
            continue
        lab = _clean_label(tok)
        if len(lab) < 2 or re.fullmatch(r"[A-Za-z]+", lab):
            after_marker = False
            continue
        if after_marker:
            sub = lab
        else:
            main, sub = lab, ""
        cur = (main, sub)
        after_marker = False
    return entries


def collect_volume(book: str, vol) -> list[tuple]:
    pdf = vol.source_file
    if not os.path.exists(pdf):
        print(f"  {book} 第{vol.volume}卷: pdf 缺失，跳过")
        return []
    printed2pdf: dict[int, int] = {}
    pdf2text: dict[int, str] = {}
    for pg in vol.pages:
        pdf2text[pg.pdf_page] = pg.raw_text or ""
        pp = str(pg.printed_page or "")
        if pp.isdigit():
            printed2pdf.setdefault(int(pp), pg.pdf_page)
    if not printed2pdf:
        return []
    vals = sorted(printed2pdf)
    max_printed = vals[int(len(vals) * 0.97)]

    doc = fitz.open(pdf)
    region = index_region_pages(doc)
    if not region:
        doc.close()
        print(f"  {book} 第{vol.volume}卷: 未检出名目索引")
        return []
    text = extract_index_text(doc, region)
    doc.close()

    rows: list[tuple] = []
    kept = dropped = 0
    for main, sub, pages in parse_entries(text, max_printed):
        full = f"{main}·{sub}" if sub else main
        label = sub or main
        grams = {label[i:i + 2] for i in range(len(label) - 1)} or {label}
        for printed in pages:
            pdfp = printed2pdf.get(printed)
            if not pdfp:
                dropped += 1
                continue
            ctx = "".join(pdf2text.get(pdfp + d, "") for d in (0, -1, 1))
            if any(g in ctx for g in grams):
                rows.append((book, vol.volume, vol.source_file, main, sub, full,
                             normalize(main), normalize(full), str(printed), pdfp))
                kept += 1
            else:
                dropped += 1
    rate = kept / (kept + dropped) if (kept + dropped) else 0
    print(f"  {book} 第{vol.volume}卷: 索引页{len(region)} 入库{kept}条 通过率{rate:.0%}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--books", nargs="*", default=["文集"])
    args = ap.parse_args()

    corpus = app.corpus
    all_rows: list[tuple] = []
    for book in args.books:
        vols = corpus.books.get(book, [])
        print(f"《{book}》共 {len(vols)} 卷")
        for vol in vols:
            all_rows.extend(collect_volume(book, vol))

    os.makedirs("data", exist_ok=True)
    tmp = DB_PATH + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    conn.execute("""
        CREATE TABLE subject_index (
            book TEXT, volume INTEGER, source_file TEXT,
            term TEXT, sub TEXT, full_label TEXT,
            norm_term TEXT, norm_label TEXT,
            printed_page TEXT, pdf_page INTEGER
        )""")
    conn.executemany(
        "INSERT INTO subject_index "
        "(book,volume,source_file,term,sub,full_label,norm_term,norm_label,printed_page,pdf_page) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", all_rows)
    conn.execute("CREATE INDEX idx_si_norm_label ON subject_index(norm_label)")
    conn.execute("CREATE INDEX idx_si_norm_term ON subject_index(norm_term)")
    conn.commit()
    n_terms = conn.execute("SELECT COUNT(DISTINCT norm_term) FROM subject_index").fetchone()[0]
    conn.close()
    os.replace(tmp, DB_PATH)
    print(f"\n写入 {DB_PATH}: {len(all_rows)} 条引用, {n_terms} 去重词条")


if __name__ == "__main__":
    main()
