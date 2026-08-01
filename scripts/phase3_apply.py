# -*- coding: utf-8 -*-
"""Phase3 落库：把 GLM 校对给出的改动(phase3_edits.jsonl)过「严格安全闸」后写回 raw_text，
并用 build_index.normalize 重生成 normalized_text（保持检索一致）。默认干跑，先给人工核。

安全闸（全部满足才采纳，宁缺毋滥——权威原文不可伪造）：
  1) before 长度>=2 且在该页 raw_text 里唯一或可定位；after 与 before 长度差<=1；
  2) 去掉公共首尾后，差异核 <=2 字；
  3) 差异核不含拉丁字母/数字（保护数学变量 x/y/W/G、页码、年份等，绝不臆造/删改）；
  4) after 的新增字符只能是 汉字 或 中文标点（，。、；：《》""''—）——绝不引入拉丁/数字/杂符；
  5) before 不跨《》书名号边界。
不满足者一律列入「rejected」交人工，不自动改（含 GLM 对乱码数学/图版的重构臆测）。

用法：
  python scripts/phase3_apply.py                 # 干跑报告(accepted/rejected 分类+样例)
  python scripts/phase3_apply.py --report-json X
  python scripts/phase3_apply.py --apply
"""
from __future__ import annotations
import argparse, hashlib, json, re, shutil, sqlite3, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from build_index import normalize  # noqa

DB = ROOT / "data" / "corpus.sqlite"
SIDECAR = ROOT / "data" / "phase3_edits.jsonl"
CJK = re.compile(r"[㐀-鿿]")
CN_PUNCT = set("，。、；：？！《》「」『』“”‘’—…·（）")
LATIN_DIGIT = re.compile(r"[A-Za-z0-9]")


def strip_common(a: str, b: str):
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    ja, jb = len(a), len(b)
    while ja > i and jb > i and a[ja - 1] == b[jb - 1]:
        ja -= 1; jb -= 1
    return a[i:ja], b[i:jb]


# 语法/虚词类改动=编者润色而非 OCR 纠错，一律不自动改（易改掉原译文用词）
GRAMMAR_BLOCK = {("他", "它"), ("它", "他"), ("那", "哪"), ("哪", "那"),
                 ("的", "地"), ("地", "的"), ("的", "得"), ("得", "的"), ("象", "像"), ("像", "象")}


def gate(before: str, after: str, raw: str):
    """返回 (ok: bool, reason: str)。只放行『单字形近字替换』与『纯标点规范化』，其余全驳。"""
    if len(before) < 2:
        return False, "before<2(定位不稳)"
    if "《" in before or "》" in before or "《" in after or "》" in after:
        return False, "触及书名号"
    db, da = strip_common(before, after)
    if not db and not da:
        return False, "无实际差异"
    if not db.strip():
        return False, "纯插入(空格处补字/补句号=臆测续写,拒)"
    if not da.strip():
        return False, "纯删除(暂不自动删)"
    if LATIN_DIGIT.search(db) or LATIN_DIGIT.search(da):
        return False, "差异含拉丁/数字(护数学/页码)"
    for ch in da:
        if not (CJK.match(ch) or ch in CN_PUNCT):
            return False, f"新增非汉字/中文标点:{ch!r}"
    da_all_punct = all((ch in CN_PUNCT) for ch in da)
    if da_all_punct:
        # 纯标点规范化（如 一一→——）：允许至多 2 字
        if len(db) > 2 or len(da) > 2:
            return False, "标点差异>2字"
    else:
        # 含汉字改动：只允许单字↔单字形近替换，杜绝多字语义臆测(迂固→曲折)
        if len(db) != 1 or len(da) != 1:
            return False, "非单字替换(多字语义臆测,拒)"
        if (db, da) in GRAMMAR_BLOCK:
            return False, f"语法虚词改动{db}→{da}(润色,拒)"
    cnt = raw.count(before)
    if cnt == 0:
        return False, "before 不在原文"
    if cnt > 5:
        return False, f"before 出现{cnt}次(易误伤)"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--report-json", default="")
    args = ap.parse_args()

    rows = [json.loads(l) for l in SIDECAR.read_text(encoding="utf-8").splitlines() if l.strip()]
    con = sqlite3.connect(args.db); cur = con.cursor()

    accepted = []   # (src, pdf, before, after, why)
    rejected = []   # (src, pdf, before, after, reason)
    reasons = Counter()
    # 页级：需要重放到 raw_text
    page_edits = {}  # (src,pdf) -> list[(before,after)]
    for r in rows:
        if r.get("error") or not r.get("edits"):
            continue
        rec = cur.execute("SELECT raw_text FROM pages WHERE source_file=? AND pdf_page=?", (r["src"], r["pdf"])).fetchone()
        raw = rec[0] if rec else ""
        for e in r["edits"]:
            b, a = e["before"], e["after"]
            ok, reason = gate(b, a, raw)
            if ok:
                accepted.append((r["src"], r["pdf"], b, a, e.get("why", "")))
                page_edits.setdefault((r["src"], r["pdf"]), []).append((b, a))
            else:
                rejected.append((r["src"], r["pdf"], b, a, reason)); reasons[reason] += 1

    print(f"GLM 提出改动的页: {sum(1 for r in rows if r.get('edits'))}")
    print(f"采纳(过闸): {len(accepted)} 处 / {len(page_edits)} 页    驳回(交人工): {len(rejected)} 处")
    print("\n驳回原因分布:")
    for rr, n in reasons.most_common():
        print(f"   {n:>3}  {rr}")
    print("\n=== 采纳样例(将落库) ===")
    for src, pdf, b, a, why in accepted[:30]:
        print(f"   [{src.split('/')[-1][:20]} p{pdf}] 『{b}』→『{a}』  {why}")
    print("\n=== 驳回样例(不自动改) ===")
    for src, pdf, b, a, reason in rejected[:15]:
        print(f"   [{src.split('/')[-1][:16]} p{pdf}] 『{b}』→『{a}』  ✗{reason}")

    if args.report_json:
        Path(args.report_json).write_text(json.dumps({
            "accepted": [{"src": s, "pdf": p, "before": b, "after": a, "why": w} for s, p, b, a, w in accepted],
            "rejected": [{"src": s, "pdf": p, "before": b, "after": a, "reason": r} for s, p, b, a, r in rejected],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n清单 → {args.report_json}")

    if not args.apply:
        print("\n[dry-run] 未落库。")
        con.close(); return

    backup = Path(args.db + ".bak-phase3")
    shutil.copy2(args.db, backup)
    print(f"\n已备份: {backup}")
    npg = 0
    for (src, pdf), edits in page_edits.items():
        rec = cur.execute("SELECT raw_text FROM pages WHERE source_file=? AND pdf_page=?", (src, pdf)).fetchone()
        if not rec:
            continue
        raw = rec[0]; new_raw = raw
        for b, a in edits:
            new_raw = new_raw.replace(b, a)
        if new_raw != raw:
            cur.execute("UPDATE pages SET raw_text=?, normalized_text=? WHERE source_file=? AND pdf_page=?",
                        (new_raw, normalize(new_raw), src, pdf))
            npg += cur.rowcount
    con.commit(); con.close()
    print(f"已更新 pages 行: {npg}")
    h = hashlib.sha256(Path(args.db).read_bytes()).hexdigest()
    Path(args.db + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    print(f"新 sha256: {h}")


if __name__ == "__main__":
    main()
