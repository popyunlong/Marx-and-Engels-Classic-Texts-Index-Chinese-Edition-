# -*- coding: utf-8 -*-
"""Phase2：对 Phase1 后仍存在页码问题的页(FILL/WRONG/UNRESOLVED)用 GLM-4V 读印刷页码。

产出 sidecar：data/phase2_folios.jsonl（每页一行）。断点续跑。落库由 phase2_apply.py 负责。
worklist = 当前 DB 上 analyze_volume 判为 FILL/WRONG/UNRESOLVED 的所有正文页（含五年规划等不可靠卷）。

用法：
  python scripts/phase2_vision.py --build-worklist          # 只生成 worklist 并统计
  python scripts/phase2_vision.py --workers 5 [--limit N]   # 跑视觉(需 ZHIPU_API_KEY)
"""
from __future__ import annotations
import argparse, json, os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import fitz, sqlite3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import importlib.util
_spec = importlib.util.spec_from_file_location("fpa", ROOT / "scripts" / "fix_printed_pages_all.py")
fpa = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(fpa)
_spec2 = importlib.util.spec_from_file_location("grf", ROOT / "scripts" / "glm_read_folio.py")
grf = importlib.util.module_from_spec(_spec2); _spec2.loader.exec_module(grf)

DB = ROOT / "data" / "corpus.sqlite"
WORKLIST = ROOT / "data" / "phase2_worklist.json"
SIDECAR = ROOT / "data" / "phase2_folios.jsonl"
_write_lock = threading.Lock()
_print_lock = threading.Lock()


BODY_MIN_TEXT = 40  # 缺页码页要有实质正文才值得视觉(空白/插页板 vision 也读不到，跳过省额度)


def build_worklist():
    """精炼 worklist，剔除「低置信卷的假 WRONG」(如毛选扫描卷 vol2/3/4：文本层无页码→
    检测出零候选、共识乱锁 offset=4，把 offset=15 的正确记录全判成 WRONG)。规则：
      · 缺页码(FILL/UNRESOLVED) 且有实质正文 → 收(所有卷)，含真正显示「按PDF页码」的页；
      · WRONG → 仅当该卷高置信(conf>=0.85，即文本层能读到页码、判定可信) 才收；
        低置信卷一律信任其既有记录值(多来自专门构建逻辑)，不送视觉、不改。
      · 五年规划 → 汇编页码特殊，全部问题页都收(视觉按最外侧汇编页码读)。"""
    con = sqlite3.connect(str(DB)); cur = con.cursor()
    srcs = [r[0] for r in cur.execute("SELECT DISTINCT source_file FROM pages").fetchall()]
    work = []
    for src in sorted(srcs):
        rows = cur.execute("SELECT pdf_page, printed_page, raw_text, normalized_text FROM pages WHERE source_file=? ORDER BY pdf_page", (src,)).fetchall()
        book = cur.execute("SELECT book FROM pages WHERE source_file=? LIMIT 1", (src,)).fetchone()[0]
        per, stats = fpa.analyze_volume([(r[0], r[1], r[2]) for r in rows])
        textlen = {r[0]: len((r[3] or "").strip()) for r in rows}
        high_conf = (not stats["low_conf"])
        is_defer = book in fpa.DEFER_BOOKS
        for p in per:
            cls = p["cls"]; kind = None
            if cls in ("FILL", "UNRESOLVED"):
                if textlen.get(p["pdf"], 0) >= BODY_MIN_TEXT or is_defer:
                    kind = "missing"
            elif cls == "WRONG":
                if high_conf or is_defer:
                    kind = "wrong"
                # 低置信卷的 WRONG = 假阳性(信任记录值)，跳过
            if kind:
                work.append({"src": src, "book": book, "pdf": p["pdf"],
                             "recorded": (str(p["recorded"]) if p["recorded"] is not None else None),
                             "cls": cls, "kind": kind, "consensus": p["target"],
                             "mode_off": stats["mode_off"], "defer_book": is_defer})
    con.close()
    WORKLIST.write_text(json.dumps(work, ensure_ascii=False), encoding="utf-8")
    from collections import Counter
    bc = Counter(w["book"] for w in work); cc = Counter(w["cls"] for w in work)
    print(f"worklist 页数: {len(work)}")
    print("按类别:", dict(cc))
    print("按书(前15):")
    for b, n in bc.most_common(15):
        print(f"   {n:>4}  {b}")
    return work


def load_done():
    done = set()
    if SIDECAR.exists():
        for line in SIDECAR.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line: continue
            try:
                o = json.loads(line)
                if o.get("error"): continue  # 失败页重跑
                done.add((o["src"], int(o["pdf"])))
            except Exception: continue
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-worklist", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--scale", type=float, default=2.0)
    args = ap.parse_args()

    if args.build_worklist or not WORKLIST.exists():
        work = build_worklist()
        if args.build_worklist:
            return
    else:
        work = json.loads(WORKLIST.read_text(encoding="utf-8"))

    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("缺少 ZHIPU_API_KEY")
    done = load_done()
    todo = [w for w in work if (w["src"], w["pdf"]) not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"待视觉页: {len(todo)}（已完成 {len(done)}，模型 {grf.MODEL}，并发 {args.workers}）", flush=True)
    if not todo:
        print("无待处理页。"); return

    by_src = {}
    for w in todo:
        by_src.setdefault(w["src"], []).append(w)

    fh = SIDECAR.open("a", encoding="utf-8")
    t0 = time.time(); cnt = {"n": 0, "num": 0, "none": 0, "err": 0, "ptok": 0, "ctok": 0}
    total = len(todo)

    def record(w, res):
        row = {"src": w["src"], "pdf": w["pdf"], "book": w["book"], "cls": w["cls"],
               "recorded": w["recorded"], "consensus": w["consensus"],
               "vision": res.get("folio"), "vraw": (res.get("raw") or "")[:20]}
        if res.get("error"): row["error"] = res["error"]
        u = res.get("usage", {})
        with _write_lock:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n"); fh.flush()
            cnt["n"] += 1
            if res.get("error"): cnt["err"] += 1
            elif res.get("folio") is not None: cnt["num"] += 1
            else: cnt["none"] += 1
            cnt["ptok"] += u.get("prompt_tokens", 0); cnt["ctok"] += u.get("completion_tokens", 0)
            n = cnt["n"]
            if n % 25 == 0 or n == total:
                el = time.time() - t0; rate = n / el if el else 0
                eta = (total - n) / rate / 60 if rate else 0
                with _print_lock:
                    print(f"  {n}/{total} num={cnt['num']} none={cnt['none']} err={cnt['err']} "
                          f"{rate:.2f}页/s ETA{eta:.0f}分", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for src, ws in by_src.items():
            try:
                doc = fitz.open(ROOT / src)
            except Exception as e:
                for w in ws:
                    record(w, {"error": f"open:{e}"[:60]})
                continue
            futs = {}; idx = 0; inflight = set()
            while idx < len(ws) or inflight:
                while idx < len(ws) and len(inflight) < args.workers * 3:
                    w = ws[idx]; idx += 1
                    try:
                        b64 = grf.render_b64(doc, w["pdf"], args.scale)
                    except Exception as e:
                        record(w, {"error": f"render:{e}"[:60]}); continue
                    fut = ex.submit(grf.call_folio, api_key, b64); futs[fut] = w; inflight.add(fut)
                donef = [f for f in inflight if f.done()]
                if not donef:
                    donef = [next(as_completed(inflight))]
                for f in donef:
                    w = futs.pop(f); inflight.discard(f)
                    try: record(w, f.result())
                    except Exception as e: record(w, {"error": str(e)[:60]})
            doc.close()
    fh.close()
    el = (time.time() - t0) / 60
    print(f"\n完成 {cnt['n']} 页：读到数字 {cnt['num']}，NONE {cnt['none']}，err {cnt['err']}，耗时 {el:.1f} 分", flush=True)


if __name__ == "__main__":
    main()
