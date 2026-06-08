# -*- coding: utf-8 -*-
"""汇总 audit_out/align_*.json 候选，按"替换字对频次"分级，并做安全过滤。

分级逻辑：
  - 同一 (ocr_char→auth_char) 字对在全集候选中出现次数越多 = 越像系统性 OCR 混淆 = 越高置信。
  - 单字替换优先；OCR 侧为常见高频字(人/二/官/有 等)且方向两可的，标记 review。
输出：audit_out/align_candidates_all.json（含 tier 字段）+ 控制台分级统计。
只读分析。
"""
import sys, io, os, json, glob, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "audit_out")

# 常见高频字：作为 OCR 侧时方向两可，需 PDF 核验（不自动采纳）
COMMON = set("人入己已二三十大小上下不日曰目自白末未官它有右土王玉天夫"
             "辩辨干千连联里理日月木本田由甲电")

cands = []
for f in sorted(glob.glob(os.path.join(OUT, "align_*.json"))):
    base = os.path.basename(f)
    if not base[6:8].isdigit():
        continue
    d = json.load(open(f, encoding="utf-8"))
    for c in d.get("candidates", []):
        cands.append(c)

pair_freq = collections.Counter((c["ocr"], c["auth"]) for c in cands)

def tier(c):
    o, a = c["ocr"], c["auth"]
    freq = pair_freq[(o, a)]
    # OCR 侧含常见字且单字 → 方向两可，需复核
    ambiguous = len(o) == 1 and o in COMMON
    if ambiguous:
        return "review"
    if freq >= 4:
        return "high"          # 系统性混淆，高置信
    if freq >= 2:
        return "mid"
    return "low"               # 一次性，需复核

for c in cands:
    c["pair_freq"] = pair_freq[(c["ocr"], c["auth"])]
    c["tier"] = tier(c)

by_tier = collections.Counter(c["tier"] for c in cands)
by_vol = collections.Counter((c["volume"], c["tier"]) for c in cands)
json.dump({"total": len(cands), "candidates": cands},
          open(os.path.join(OUT, "align_candidates_all.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

print(f"总候选={len(cands)}  分级: {dict(by_tier)}")
print("各卷分级:")
for vol in range(1, 11):
    row = {t: by_vol[(vol, t)] for t in ("high", "mid", "low", "review") if by_vol[(vol, t)]}
    print(f"  vol{vol}: {row}")
print("high 档 top 字对:", pair_freq.most_common(25))
