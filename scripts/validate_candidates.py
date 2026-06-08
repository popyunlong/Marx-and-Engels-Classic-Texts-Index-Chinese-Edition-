# -*- coding: utf-8 -*-
"""语料频次二次校验对齐候选（独立于权威对齐的第二信号）。

思路：真实 OCR 形近错误，其"修正后"的二元组(bigram)在全语料(文集+全集+列宁)远比
"错误"二元组常见。对每个候选，比较改字处左右两个 bigram 的 ocr 侧 vs auth 侧频次：
  corpus_ok = auth 侧两个 bigram 都比对应 ocr 侧常见，且 auth 侧至少一个达到阈值。
转写变体(如 做→作)通常两侧都常见 → 不被判为 corpus_ok（留给 agent + PDF 定夺）。
只读；输出 audit_out/align_candidates_validated.json，附 corpus_ok / freq 信息。
"""
import sys, io, os, json, sqlite3, collections, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAN = re.compile(r"[一-鿿]")

c = sqlite3.connect(os.path.join(ROOT, "data", "corpus.sqlite"))
print("building corpus bigram frequencies ...", flush=True)
bigram = collections.Counter()
for (norm,) in c.execute("select normalized_text from pages"):
    prev = ""
    for ch in norm:
        if HAN.match(ch):
            if prev:
                bigram[prev + ch] += 1
            prev = ch
        else:
            prev = ""
print(f"distinct bigrams={len(bigram)}", flush=True)

allf = os.path.join(ROOT, "audit_out", "align_candidates_all.json")
cands = json.load(open(allf, encoding="utf-8"))["candidates"]

THRESH = 3
RATIO = 3.0


def site_bigrams(find_han, replace_han):
    """find/replace 等长，定位差异区间，返回 (ocr_left,ocr_right,auth_left,auth_right) bigrams。"""
    if len(find_han) != len(replace_han):
        return None
    diff = [i for i in range(len(find_han)) if find_han[i] != replace_han[i]]
    if not diff:
        return None
    a, b = diff[0], diff[-1]  # 差异区间 [a,b]
    obs = []
    for s, src in (("ocr", find_han), ("auth", replace_han)):
        left = src[a - 1:a + 1] if a >= 1 else ""
        right = src[b:b + 2] if b + 1 < len(src) else ""
        obs.append((left, right))
    return obs  # [(ocr_left,ocr_right),(auth_left,auth_right)]


ok = 0
for x in cands:
    sb = site_bigrams(x["find_han"], x["replace_han"])
    if not sb:
        x["corpus_ok"] = False
        continue
    (ol, orr), (al, ar) = sb
    ofl, ofr = bigram.get(ol, 0), bigram.get(orr, 0)
    afl, afr = bigram.get(al, 0), bigram.get(ar, 0)
    x["freq"] = {"ocr": [ofl, ofr], "auth": [afl, afr]}
    # auth 两侧都不少于 ocr 侧，且 auth 至少一侧达阈值且明显占优
    cond = (afl >= ofl and afr >= ofr
            and max(afl, afr) >= THRESH
            and (afl + afr) >= RATIO * (ofl + ofr + 1))
    x["corpus_ok"] = bool(cond)
    if cond:
        ok += 1

json.dump({"total": len(cands), "corpus_ok": ok, "candidates": cands},
          open(os.path.join(ROOT, "audit_out", "align_candidates_validated.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

by = collections.Counter()
for x in cands:
    by[(x["tier"], x["corpus_ok"])] += 1
print(f"corpus_ok={ok}/{len(cands)}")
print("tier × corpus_ok:")
for t in ("high", "mid", "low", "review"):
    print(f"  {t}: ok={by[(t, True)]}  not_ok={by[(t, False)]}")
print("DONE_VALIDATE")
