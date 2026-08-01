# -*- coding: utf-8 -*-
"""用『语料自证』校验 Phase3 采纳的形近字改动：真正的纠错是把「错字词」改成语料里**大量出现的
正词**（商品到处有、商晶几乎没有）；有害改动(签署→签暑)会把正词改成语料里**根本不存在**的非词。

规则（仅对 CJK→CJK 单字替换）：
  取含改动位的最长汉字词 core_before / core_after，在全库 normalized_text 里数频次：
    采纳  ⇔  freq(after) >= MIN_ATTEST 且 freq(after) >= freq(before)*RATIO
    （纠正后的词被语料充分佐证，且明显比原词常见=原词是错字）
  否则驳回（after 是非词，或 before 本就是常用正词=不该改）。
符号→汉字/标点(→→一 等系统性 OCR 混淆) 与纯标点改动不走此校验，另行处理。

输出 data/_phase3_validated.json：{apply:[...], reject:[...(带频次)]}。
"""
from __future__ import annotations
import json, re, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "corpus.sqlite"
REPORT = ROOT / "data" / "_phase3_report.json"
CJK = re.compile(r"[一-鿿]")
MIN_ATTEST = 5     # after 词至少在全库出现这么多次才算「被佐证」(挡住 署→暑/战→汇 造非词)
LOWATT = 150       # after 频次低于此=弱佐证，单列供人工再看(如 资通×30)


def strip_common(a, b):
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    ja, jb = len(a), len(b)
    while ja > i and jb > i and a[ja - 1] == b[jb - 1]:
        ja -= 1; jb -= 1
    return i, a[i:ja], b[i:jb]


def cjk_run_at(s, pos):
    """s 中含 index pos 的最长连续汉字子串。"""
    if pos >= len(s) or not CJK.match(s[pos]):
        # 取邻近汉字run
        pass
    lo = pos
    while lo > 0 and CJK.match(s[lo - 1]):
        lo -= 1
    hi = pos
    while hi < len(s) and CJK.match(s[hi]):
        hi += 1
    return s[lo:hi]


def main():
    big = []
    con = sqlite3.connect(str(DB)); cur = con.cursor()
    for (t,) in cur.execute("SELECT normalized_text FROM pages"):
        if t:
            big.append(t)
    con.close()
    CORPUS = "\n".join(big)
    print(f"语料载入: {len(CORPUS)/1e6:.1f}M 字")

    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    acc = rep["accepted"]
    apply_list, reject_list, other_list = [], [], []
    freq_cache = {}

    def freq(s):
        if s not in freq_cache:
            freq_cache[s] = CORPUS.count(s) if s else 0
        return freq_cache[s]

    for e in acc:
        b, a = e["before"], e["after"]
        i, db, da = strip_common(b, a)
        # 仅处理「单字↔单字且两侧均为汉字」的形近替换
        if len(db) == 1 and len(da) == 1 and CJK.match(db) and CJK.match(da):
            cb = cjk_run_at(b, i)
            ca = cjk_run_at(a, i)
            fa, fb = freq(ca), freq(cb)
            rec = {**e, "core_b": cb, "core_a": ca, "fa": fa, "fb": fb}
            # after 被充分佐证(是常见正词) 且 比原词更常见(原词是错字) → 采纳
            if fa >= MIN_ATTEST and fa > fb:
                rec["weak"] = fa < LOWATT
                apply_list.append(rec)
            else:
                reject_list.append(rec)
        else:
            other_list.append(e)  # 符号→汉字 / 标点，另行处理

    weak = [r for r in apply_list if r.get("weak")]
    out = {"apply": apply_list, "reject": reject_list, "other": other_list}
    (ROOT / "data" / "_phase3_validated.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"CJK 形近字改动: 语料自证通过 {len(apply_list)}(其中弱佐证需再看 {len(weak)})  驳回 {len(reject_list)}")
    print(f"非CJK(符号/标点)另处理: {len(other_list)}")
    print("\n=== 自证通过样例(将采纳) ===")
    for r in apply_list[:18]:
        print(f"  {r['core_b']}(×{r['fb']}) → {r['core_a']}(×{r['fa']})")
    print("\n=== 自证驳回样例(after是非词/before本就常用) ===")
    for r in reject_list[:18]:
        print(f"  {r['before']}→{r['after']}  [{r['core_b']}×{r['fb']} → {r['core_a']}×{r['fa']}]")


if __name__ == "__main__":
    main()
