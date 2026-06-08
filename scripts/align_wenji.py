# -*- coding: utf-8 -*-
"""权威全文对齐校勘：用 marxists.org 的 marx-engels2(=《文集》同一版本) 全文，
逐部著作与本地 OCR 对齐，抽取单字形近误识候选（OCR侧≠权威侧、等长、上下文锚定）。

用法: python scripts/align_wenji.py <volume>  →  audit_out/align_<vol>.json
依赖 curl 抓取(GBK/gb18030 解码)。只读，不改库。
"""
import sys, io, os, re, json, subprocess, difflib, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAN = re.compile(r"[一-鿿]")
BASE = "https://www.marxists.org/chinese/marx-engels2"


def curl(url):
    """GET with retry; follow redirects. Returns bytes (empty on persistent failure)."""
    for attempt in range(4):
        r = subprocess.run(
            ["curl", "-sSL", "--retry", "2", "--retry-delay", "1", "--max-time", "50", url],
            capture_output=True,
        )
        if r.returncode == 0 and len(r.stdout) > 200:
            return r.stdout
    return r.stdout  # bytes


def html_text(raw_bytes):
    t = raw_bytes.decode("gb18030", errors="replace")
    t = re.sub(r"<[^>]+>", " ", t)
    return t


def han_only_with_map(s):
    """return (han_string, [orig_index...]) — not needed here; keep pure han."""
    return "".join(ch for ch in s if HAN.match(ch))


def src_base(vol):
    """权威全文来源目录；可用第2个命令行参数覆盖（vol5 资本论用全集23）。"""
    if len(sys.argv) > 2 and sys.argv[2].startswith("http"):
        return sys.argv[2].rstrip("/")
    return f"{BASE}/{vol:02d}"


def work_htms(vol):
    """fetch volume index, return ordered list of work htm filenames (NN.htm)."""
    raw = curl(f"{src_base(vol)}/index.htm").decode("gb18030", errors="replace")
    files = re.findall(r"""href=['"](\d{2,3}\.htm)['"]""", raw)  # 2位(NN)或3位(NNN，如第10卷书信)
    seen, out = set(), []
    for f in files:
        if f not in seen and f not in ("00.htm", "000.htm"):  # 卷说明，跳过
            seen.add(f); out.append(f)
    return sorted(out)


def main():
    vol = int(sys.argv[1])
    c = sqlite3.connect(os.path.join(ROOT, "data", "corpus.sqlite"))
    rows = c.execute(
        "select pdf_page, raw_text, normalized_text from pages where book='文集' and volume=? order by pdf_page",
        (vol,),
    ).fetchall()
    # OCR han stream with page map
    ocr_han = []
    ocr_page = []
    for pg, raw, norm in rows:
        for ch in norm:
            if HAN.match(ch):
                ocr_han.append(ch); ocr_page.append(pg)
    ocr_han = "".join(ocr_han)
    raw_by_page = {pg: raw for pg, raw, _ in rows}

    files = work_htms(vol)
    print(f"vol{vol}: works={len(files)} ocr_han={len(ocr_han)}", flush=True)

    candidates = []
    cursor = 0
    for f in files:
        aw = han_only_with_map(html_text(curl(f"{BASE}/{vol:02d}/{f}")))
        if len(aw) < 80:
            continue
        win = ocr_han[cursor:cursor + len(aw) + 4000]
        if len(win) < 80:
            win = ocr_han[cursor:]
        sm = difflib.SequenceMatcher(None, win, aw, autojunk=False)
        last_ocr_end = cursor
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                last_ocr_end = cursor + i2
            elif tag == "replace" and (i2 - i1) == (j2 - j1) and 1 <= (i2 - i1) <= 2:
                o = win[i1:i2]; a = aw[j1:j2]
                abs_i = cursor + i1
                pg = ocr_page[abs_i] if abs_i < len(ocr_page) else None
                lo = max(0, abs_i - 3); hi = min(len(ocr_han), cursor + i2 + 3)
                find = ocr_han[lo:abs_i] + o + ocr_han[cursor + i2:hi]
                repl = ocr_han[lo:abs_i] + a + ocr_han[cursor + i2:hi]
                candidates.append({"volume": vol, "pdf_page": pg, "ocr": o, "auth": a,
                                   "find_han": find, "replace_han": repl, "work": f})
        # advance cursor near end of this work's matched region
        if last_ocr_end > cursor:
            cursor = last_ocr_end

    # dedup
    uniq = {}
    for x in candidates:
        k = (x["pdf_page"], x["find_han"], x["replace_han"])
        uniq.setdefault(k, x)
    cands = list(uniq.values())
    os.makedirs(os.path.join(ROOT, "audit_out"), exist_ok=True)
    out = os.path.join(ROOT, "audit_out", f"align_{vol:02d}.json")
    json.dump({"volume": vol, "count": len(cands), "candidates": cands},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"candidates(unique)={len(cands)} -> {out}", flush=True)
    for x in cands[:50]:
        print(f"  p{x['pdf_page']} {x['ocr']}->{x['auth']}  ctx={x['find_han']}")
    print("ALIGN_DONE")


if __name__ == "__main__":
    main()
