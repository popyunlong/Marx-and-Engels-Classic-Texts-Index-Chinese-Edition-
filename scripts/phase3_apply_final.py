# -*- coding: utf-8 -*-
"""Phase3 最终落库：只改『语料自证通过的形近错字』+『符号误识成汉字(→切→一切)』，
逐页在 raw_text 上替换并用 build_index.normalize 重生成 normalized_text。

采纳来源 = data/_phase3_validated.json 的 apply（已过：单字形近 + after 词被语料充分佐证且比原词常见），
再剔除少量语义换词(SEMANTIC_BLOCK)；另从 other 里补入『符号→汉字且纠正后词被佐证』的形近修复。
**不动任何标点**（半→全角/标点互换风险高、且非错字乱码，本轮一律跳过）。

用法：
  python scripts/phase3_apply_final.py                 # 干跑+导出最终清单
  python scripts/phase3_apply_final.py --db <path> --apply
"""
from __future__ import annotations
import argparse, hashlib, json, re, shutil, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from build_index import normalize  # noqa

DB = ROOT / "data" / "corpus.sqlite"
VALID = ROOT / "data" / "_phase3_validated.json"
CJK = re.compile(r"[一-鿿]")

# 人工复核后剔除：①语义换词(非形近)；②结构不相似/OCR 不可能如此混淆的单次改动
# （虽然语料里改后短语也出现，但字形对不上，多半是 GLM 猜的，宁可不改）。
SEMANTIC_BLOCK = {
    # 语义换词
    ("主", "义"), ("渍", "资"), ("言", "同"), ("给", "把"), ("继", "持"),
    ("许", "忍"), ("然", "乃"), ("到", "至"), ("是", "为"), ("做", "作"),
    ("及", "和"), ("和", "与"), ("同", "与"), ("篇", "卷"), ("超", "起"),
    # 结构不相似/存疑
    ("醚", "议"), ("兆", "米"), ("品", "价"), ("咖", "资"), ("减", "畴"),
    ("象", "如"), ("原", "但"), ("湿", "涅"), ("词", "诃"), ("评", "浮"),
    ("戳", "教"), ("辆", "牺"), ("输", "翰"), ("打", "第"), ("号", "马"),
    ("患", "思"), ("悲", "思"), ("人", "录"), ("墨", "部"), ("智", "资"),
    ("军", "罕"), ("该", "当"), ("己", "记"), ("计", "表"), ("獊", "数"),
    ("功", "动"), ("子", "于"), ("比", "然"), ("悬", "设"), ("甲", "中"),
    ("局", "均"), ("辅", "转"), ("面", "而"), ("时", "际"), ("从", "因"),
    ("晴", "减"), ("言", "一"), ("夷", "表"), ("∞", "万"), ("甲", "润"), ("国", "目"),
}


def strip_common(a, b):
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    ja, jb = len(a), len(b)
    while ja > i and jb > i and a[ja - 1] == b[jb - 1]:
        ja -= 1; jb -= 1
    return i, a[i:ja], b[i:jb]


def cjk_run_at(s, pos):
    lo = pos
    while lo > 0 and CJK.match(s[lo - 1]):
        lo -= 1
    hi = pos
    while hi < len(s) and CJK.match(s[hi]):
        hi += 1
    return s[lo:hi]


def build_final(corpus_count):
    v = json.loads(VALID.read_text(encoding="utf-8"))
    final = []
    # 1) 形近字（语料自证通过），剔除语义换词
    for r in v["apply"]:
        i, db, da = strip_common(r["before"], r["after"])
        if (db, da) in SEMANTIC_BLOCK:
            continue
        final.append({"src": r["src"], "pdf": r["pdf"], "before": r["before"], "after": r["after"],
                      "kind": "形近字", "note": f"{r['core_b']}×{r['fb']}→{r['core_a']}×{r['fa']}"})
    # 2) 符号→汉字：da 为汉字、db 为非汉字符号，且纠正后词被语料佐证(>=5)
    for e in v["other"]:
        i, db, da = strip_common(e["before"], e["after"])
        if (db, da) in SEMANTIC_BLOCK:
            continue
        if len(da) == 1 and CJK.match(da) and db and not CJK.match(db[0]) and not db.strip().isdigit():
            ca = cjk_run_at(e["after"], i)
            if corpus_count(ca) >= 5:
                final.append({"src": e["src"], "pdf": e["pdf"], "before": e["before"], "after": e["after"],
                              "kind": "符号→字", "note": f"→{ca}×{corpus_count(ca)}"})
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db); cur = con.cursor()
    CORPUS = "\n".join(t for (t,) in cur.execute("SELECT normalized_text FROM pages") if t)
    _cache = {}
    def cc(s):
        if s not in _cache: _cache[s] = CORPUS.count(s)
        return _cache[s]

    final = build_final(cc)
    # 逐页归并
    by_page = defaultdict(list)
    for e in final:
        by_page[(e["src"], e["pdf"])].append((e["before"], e["after"]))
    kinds = Counter(e["kind"] for e in final)
    print(f"最终采纳: {len(final)} 处 / {len(by_page)} 页   分类: {dict(kinds)}")
    pairs = Counter((strip_common(e['before'], e['after'])[1], strip_common(e['before'], e['after'])[2]) for e in final)
    print("\n=== 全部字替换(去重) ===")
    line = []
    for (db, da), n in pairs.most_common():
        line.append(f"{db}→{da}×{n}")
        if len(line) == 8:
            print("  " + "   ".join(line)); line = []
    if line: print("  " + "   ".join(line))

    (ROOT / "data" / "_phase3_final.json").write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.apply:
        print("\n[dry-run] 未落库。")
        con.close(); return

    backup = Path(args.db + ".bak-phase3-text")
    shutil.copy2(args.db, backup)
    print(f"\n已备份: {backup}")
    npg = 0
    for (src, pdf), edits in by_page.items():
        rec = cur.execute("SELECT raw_text FROM pages WHERE source_file=? AND pdf_page=?", (src, pdf)).fetchone()
        if not rec:
            continue
        raw = rec[0]; new = raw
        for b, a in edits:
            new = new.replace(b, a)
        if new != raw:
            cur.execute("UPDATE pages SET raw_text=?, normalized_text=? WHERE source_file=? AND pdf_page=?",
                        (new, normalize(new), src, pdf))
            npg += cur.rowcount
    con.commit(); con.close()
    print(f"已更新 pages 行: {npg}")
    h = hashlib.sha256(Path(args.db).read_bytes()).hexdigest()
    Path(args.db + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    print(f"新 sha256: {h}")


if __name__ == "__main__":
    main()
