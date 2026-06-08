# -*- coding: utf-8 -*-
"""预览/审计《文集》OCR 标点误识清理。

调用 build_index._ocr_punct_pairs_for_page（与构建时完全相同的逻辑），对 10 卷
PDF 逐页计算将被移除的“误识标点字母”，输出：
  - 每卷移除总数与字母分布；
  - 全部移除项（卷/页/before/after 上下文）写入 audit_out/ocr_cleanup_<vol>.json，
    供 10 个核对 agent 审阅；
  - 大写字母移除单独列出（优先复核，警惕公式变量/罗马数字误伤）。

只读 PDF，不改任何数据。用法：python scripts/audit_wenji_cleanup.py [vol]
"""
import sys, io, os, json, re, collections
import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_index as b  # noqa: E402

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

PDFS = {
    1: "马克思恩格斯文集[第1卷]马克思恩格斯1843-1848年著作.pdf",
    2: "马克思恩格斯文集[第2卷]马克思恩格斯1848-1859年著作.pdf",
    3: "马克思恩格斯文集[第3卷]马克思恩格斯1864-1883年著作.pdf",
    4: "马克思恩格斯文集[第4卷]恩格斯1884-1895年著作.pdf",
    5: "马克思恩格斯文集[第5卷]马克思《资本论》第一卷.pdf",
    6: "马克思恩格斯文集[第6卷]马克思《资本论》第二卷.pdf",
    7: "马克思恩格斯文集[第7卷]马克思《资本论》第三卷.pdf",
    8: "马克思恩格斯文集[第8卷]马克思《资本论》手稿选编.pdf",
    9: "马克思恩格斯文集[第9卷]恩格斯《反杜林论》《自然辩证法》.pdf",
    10: "马克思恩格斯文集[第10卷]马克思恩格斯书信选编.pdf",
}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit_out")
os.makedirs(OUT, exist_ok=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_vol(vol):
    doc = fitz.open(os.path.join(ROOT, "pdfs", "文集", PDFS[vol]))
    letc = collections.Counter()
    items = []
    upper = []
    for pi in range(len(doc)):
        page = doc[pi]
        raw = page.get_text("text")
        pairs = pairs_applicable(raw, page)
        for find, replace, letter in pairs:
            letc[letter] += 1
            k = raw.find(find)
            ctx_b = raw[max(0, k - 14):k + len(find) + 14].replace("\n", "\\n")
            after = raw.replace(find, replace)
            ka = after.find(replace)
            ctx_a = after[max(0, ka - 14):ka + len(replace) + 14].replace("\n", "\\n")
            rec = {"vol": vol, "pdf_page": pi + 1, "letter": letter,
                   "find": find, "replace": replace, "before": ctx_b, "after": ctx_a}
            items.append(rec)
            if letter.isupper():
                upper.append(rec)
    with open(os.path.join(OUT, f"ocr_cleanup_{vol:02d}.json"), "w", encoding="utf-8") as f:
        json.dump({"volume": vol, "total": len(items),
                   "letters": dict(letc.most_common()), "items": items},
                  f, ensure_ascii=False, indent=1)
    return len(items), letc, upper


def pairs_applicable(raw, page):
    """复用构建逻辑，但仅返回在该页锚定唯一（可安全应用）的项，并附字母。"""
    raw_pairs = b._ocr_punct_pairs_for_page(page)
    counts = collections.Counter(raw_pairs)
    out = []
    for (find, replace), n in counts.items():
        if raw.count(find) == n:
            # 字母 = find 中被去掉的那个 ASCII 字母
            letter = next((c for c in find if c.isascii() and c.isalpha()), "?")
            for _ in range(n):
                out.append((find, replace, letter))
    return out


def main():
    vols = [int(sys.argv[1])] if len(sys.argv) > 1 else list(PDFS)
    grand = 0
    all_letters = collections.Counter()
    all_upper = []
    for vol in vols:
        n, letc, upper = run_vol(vol)
        grand += n
        all_letters.update(letc)
        all_upper.extend(upper)
        print(f"vol{vol:2d}: removals={n:5d}  letters={dict(letc.most_common(12))}")
    print(f"TOTAL removals={grand}  letters={dict(all_letters.most_common(20))}")
    print(f"UPPERCASE removals (review): {len(all_upper)}")
    for r in all_upper[:40]:
        print(f"   v{r['vol']}p{r['pdf_page']} {r['letter']}: {r['before']}  =>  {r['after']}")
    print("DONE_AUDIT")


if __name__ == "__main__":
    main()
