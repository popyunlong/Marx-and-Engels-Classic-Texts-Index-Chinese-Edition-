# -*- coding: utf-8 -*-
"""用 GLM-4V 整页转录《文集》9 卷卷末「注释」节，产出干净注文，供 build_wenji_endnotes.py 注入。

为什么要重转录：语料 pages 表的注释页文本层是出版方 OCR，带系统性噪声——书名号
《》坏成 ((/))/队、句号成 0、冒号成 z、「即」成「IlP」，注文是读者可见文案，
这些噪声直接进浮层就是「《生产的要素队」。GLM-4V 重识别把这些修好。

复用既有视觉管线：
  · glm_read_folio.render_b64 —— PDF 页渲染成 png base64（本脚本用更高 scale，注释字小）
  · phase2_vision 的并发+断点续跑骨架 —— ThreadPoolExecutor / inflight 控流 / JSONL sidecar

worklist：对 9 卷各自用 build_wenji_endnotes.notes_section_range 定位注释节 [start,end)，
展开为逐页 (vol, source_file, pdf_page, printed_page)。共约 571 页。

sidecar：data/wenji_notes_ocr.jsonl，每页一行 {vol,pdf,printed,text,ptok,ctok}。
断点续跑：已成功转录（无 error 且 text 非空）的页跳过。

用法：
  python scripts/transcribe_wenji_notes.py --build-worklist        # 只生成 worklist 并统计
  python scripts/transcribe_wenji_notes.py --sample 8:3            # 卷8 前3页小样(测 prompt)
  python scripts/transcribe_wenji_notes.py --workers 5             # 全量(需 ZHIPU_API_KEY)
  python scripts/transcribe_wenji_notes.py --vol 8 --workers 5     # 只跑某卷
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location("grf", ROOT / "scripts" / "glm_read_folio.py")
grf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grf)

_spec2 = importlib.util.spec_from_file_location("bwe", ROOT / "scripts" / "build_wenji_endnotes.py")
bwe = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(bwe)

DB = ROOT / "data" / "corpus.sqlite"
WORKLIST = ROOT / "data" / "wenji_notes_worklist.json"
SIDECAR = ROOT / "data" / "wenji_notes_ocr.jsonl"

BASE_URL = grf.BASE_URL
MODEL = os.environ.get("ZHIPU_NOTES_MODEL", grf.MODEL)  # 默认 glm-4v-flash（免费档）

_write_lock = threading.Lock()
_print_lock = threading.Lock()

# 转录提示词：忠实、保标点、防臆造。注文的书名号/破折号回指是下游解析与自证的命脉，
# 尤其强调；页眉页码照录（解析侧 strip_running 会剔除），不劳模型判断版面角色。
PROMPT = (
    "你面前是一页中文书籍《马克思恩格斯文集》卷末“注释”的排版图片，内容是编者注释。"
    "请把这一页上的全部文字**忠实、完整**地转录为纯文本，严格遵守：\n"
    "1. 逐行按原样转录，保留自然段落；注释条目开头那个独立的阿拉伯数字（注号）单独成行。\n"
    "2. 完整保留所有标点，尤其：书名号《》、引号“”‘’、圆括号()、破折号——、顿号、"
    "以及注文末尾指回正文页码的“——123。”这类回指标记，一个都不能丢或改。\n"
    "3. 外文（人名、著作名的拉丁/希腊/俄文原文）、年份、卷次、页码务必按图精确转录。\n"
    "4. 页眉的“马克思恩格斯文集”和版心页码若存在，照原位置转录，不要略去。\n"
    "5. **只转录你确实看得清的字**；个别看不清的字用□占位。绝对不要猜测、脑补或改写任何内容。\n"
    "6. 直接输出转录正文，不要加“以下是转录”“这一页”等任何说明或评论。"
)


def call_transcribe(api_key: str, b64: str, retries: int = 5, max_tokens: int = 1024):
    """整页转录：结构同 grf.call_folio，但 prompt 换成全页转录、max_tokens 放大。

    glm-4v-flash 硬上限 max_tokens≤1024（见项目经验，超限直接 HTTP 400），故 flash 下钳到 1024。
    一页注释约 700~1100 字，1024 token 多数够；不够则 finish_reason=length，record 会计为截断，
    交由上层对截断页降级（提高 tokens 或换模型）处理。"""
    if "flash" in MODEL and max_tokens > 1024:
        max_tokens = 1024
    url = BASE_URL + "/chat/completions"
    body = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
    }
    data = json.dumps(body).encode()
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    delay = 3.0
    last = ""
    for _ in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read().decode())
            ch = resp["choices"][0]
            txt = ch["message"]["content"] or ""
            return {"text": txt.strip(), "finish": ch.get("finish_reason", ""),
                    "usage": resp.get("usage", {})}
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 529):
                return {"text": "", "error": f"HTTP {e.code}"}
            last = f"HTTP {e.code}"
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception as e:
            last = str(e)[:120]
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return {"text": "", "error": last}


def build_worklist():
    con = sqlite3.connect(str(DB))
    work = []
    for v in [str(i) for i in range(1, 10)]:
        rows = list(con.execute(
            "select pdf_page, printed_page, raw_text from pages "
            "where book='文集' and volume=? order by pdf_page", (v,)))
        src = con.execute(
            "select source_file from pages where book='文集' and volume=? limit 1", (v,)).fetchone()[0]
        start, end = bwe.notes_section_range(rows)
        if start is None:
            print(f"  [警告] 卷{v} 未检出注释节，跳过")
            continue
        pp = {r[0]: r[1] for r in rows}
        for pdf in range(start, end):
            work.append({"vol": v, "src": src, "pdf": pdf, "printed": pp.get(pdf, "")})
    con.close()
    WORKLIST.write_text(json.dumps(work, ensure_ascii=False), encoding="utf-8")
    from collections import Counter
    bc = Counter(w["vol"] for w in work)
    print(f"worklist 共 {len(work)} 页")
    for v in sorted(bc, key=int):
        print(f"   卷{v}: {bc[v]} 页")
    return work


def load_done():
    done = set()
    if SIDECAR.exists():
        for line in SIDECAR.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                if o.get("error") or not (o.get("text") or "").strip():
                    continue
                done.add((o["vol"], int(o["pdf"])))
            except Exception:
                continue
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-worklist", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--scale", type=float, default=2.6)  # 注释字小，比正文页码用的 2.0 高
    ap.add_argument("--vol", default="")
    ap.add_argument("--sample", default="")  # 形如 8:3 = 卷8 前3页
    ap.add_argument("--max-tokens", type=int, default=1024)  # glm-4v-flash 硬上限
    args = ap.parse_args()

    if args.build_worklist or not WORKLIST.exists():
        work = build_worklist()
        if args.build_worklist:
            return
    else:
        work = json.loads(WORKLIST.read_text(encoding="utf-8"))

    sample_vol, sample_n = "", 0
    if args.sample:
        sample_vol, sample_n = args.sample.split(":")
        sample_n = int(sample_n)

    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("缺少 ZHIPU_API_KEY")

    done = load_done()
    todo = [w for w in work if (w["vol"], w["pdf"]) not in done]
    if args.vol:
        todo = [w for w in todo if w["vol"] == args.vol]
    if sample_vol:
        todo = [w for w in todo if w["vol"] == sample_vol][:sample_n]

    print(f"待转录 {len(todo)} 页（已完成 {len(done)}，模型 {MODEL}，并发 {args.workers}，scale {args.scale}）", flush=True)
    if not todo:
        print("无待处理页。")
        return

    by_src = {}
    for w in todo:
        by_src.setdefault(w["src"], []).append(w)

    fh = SIDECAR.open("a", encoding="utf-8")
    t0 = time.time()
    cnt = {"n": 0, "ok": 0, "err": 0, "trunc": 0, "ptok": 0, "ctok": 0}
    total = len(todo)

    def record(w, res):
        row = {"vol": w["vol"], "pdf": w["pdf"], "printed": w["printed"],
               "text": res.get("text", ""), "finish": res.get("finish", "")}
        if res.get("error"):
            row["error"] = res["error"]
        u = res.get("usage", {})
        row["ptok"] = u.get("prompt_tokens", 0)
        row["ctok"] = u.get("completion_tokens", 0)
        with _write_lock:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            cnt["n"] += 1
            if res.get("error"):
                cnt["err"] += 1
            else:
                cnt["ok"] += 1
                if res.get("finish") == "length":
                    cnt["trunc"] += 1
            cnt["ptok"] += row["ptok"]
            cnt["ctok"] += row["ctok"]
            n = cnt["n"]
            if n % 10 == 0 or n == total:
                el = time.time() - t0
                rate = n / el if el else 0
                eta = (total - n) / rate / 60 if rate else 0
                with _print_lock:
                    print(f"  {n}/{total} ok={cnt['ok']} err={cnt['err']} 截断={cnt['trunc']} "
                          f"{rate:.2f}页/s ETA{eta:.0f}分 ctok={cnt['ctok']}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for src, ws in by_src.items():
            try:
                doc = fitz.open(ROOT / src)
            except Exception as e:
                for w in ws:
                    record(w, {"error": f"open:{e}"[:60]})
                continue
            idx = 0
            futs = {}
            inflight = set()
            while idx < len(ws) or inflight:
                while idx < len(ws) and len(inflight) < args.workers * 3:
                    w = ws[idx]
                    idx += 1
                    try:
                        b64 = grf.render_b64(doc, w["pdf"], args.scale)
                    except Exception as e:
                        record(w, {"error": f"render:{e}"[:60]})
                        continue
                    fut = ex.submit(call_transcribe, api_key, b64, 5, args.max_tokens)
                    futs[fut] = w
                    inflight.add(fut)
                donef = [f for f in inflight if f.done()]
                if not donef:
                    donef = [next(as_completed(inflight))]
                for f in donef:
                    w = futs.pop(f)
                    inflight.discard(f)
                    try:
                        record(w, f.result())
                    except Exception as e:
                        record(w, {"error": str(e)[:60]})
            doc.close()
    fh.close()
    el = (time.time() - t0) / 60
    print(f"\n完成 {cnt['n']} 页：成功 {cnt['ok']}，失败 {cnt['err']}，截断 {cnt['trunc']}，"
          f"耗时 {el:.1f} 分，completion_tokens {cnt['ctok']}", flush=True)
    if cnt["trunc"]:
        print(f"  ⚠ {cnt['trunc']} 页因 max_tokens 截断，建议对截断页提高 --max-tokens 重跑")


if __name__ == "__main__":
    main()
