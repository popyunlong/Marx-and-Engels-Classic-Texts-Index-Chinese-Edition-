# -*- coding: utf-8 -*-
"""用 GLM-4V 整页转录《文集》第十卷《书信选编》，生成流式阅读所需的干净文本。

卷 10 无 MEAS_zh 网页源（源只到卷 1-8、26），只有 PDF 语料（扫描文字版，文本层带
OCR 噪声、版面行未成段）。这里让 glm-4v-flash 逐页重转录：把版面折行合并成自然段、
修 OCR 噪声，产出与卷 1-9（MEAS_zh）同级的干净文本，供 build_stream_wenji_vol10 切章成流式。

复用 transcribe_wenji_notes 的并发框架（render_b64 + ThreadPoolExecutor + JSONL sidecar
断点续跑），仅换成书信正文 prompt。glm-4v-flash 硬顶 max_tokens≤1024。

用法：
  python scripts/transcribe_wenji_vol10.py --build-worklist
  python scripts/transcribe_wenji_vol10.py --sample 3      # 前3页小样
  python scripts/transcribe_wenji_vol10.py --workers 5     # 全量(需 ZHIPU_API_KEY)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("grf", ROOT / "scripts" / "glm_read_folio.py")
grf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grf)
_spec2 = importlib.util.spec_from_file_location("twn", ROOT / "scripts" / "transcribe_wenji_notes.py")
twn = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(twn)

DB = ROOT / "data" / "corpus.sqlite"
WORKLIST = ROOT / "data" / "wenji_vol10_worklist.json"
SIDECAR = ROOT / "data" / "wenji_vol10_ocr.jsonl"
BODY_MIN = 30  # 少于此字数的页（扉页/空白）不转录

_write_lock = threading.Lock()
_print_lock = threading.Lock()

PROMPT = (
    "这是《马克思恩格斯文集》第十卷《书信选编》的一页排版图片（马克思、恩格斯的书信）。"
    "请把整页文字**忠实、完整**地转录为纯文本，严格遵守：\n"
    "1. 书信标题（如“马克思致某某”）、写信地点、日期落款，各自单独成行。\n"
    "2. 正文按**自然段落**转录：同一段落内因排版折行产生的换行要合并成一行，"
    "只在真正的段落之间换行——书信是连续论述，不要保留每一行的硬换行。\n"
    "3. 完整保留所有标点，尤其书名号《》、引号“”、破折号——、圆括号；"
    "保留正文中的注释号（上标数字、脚注号①②等）以及注文末尾的“——编者注”。\n"
    "3a. 原文用**黑体加粗**印刷的强调字词（笔画明显比周围正文更粗、更黑，是作者的着重），"
    "用 Markdown 加粗标记转出：在强调字词前后各加两个星号，如 **应有**、**自由的**。"
    "只标真正加粗的字，普通正文不要加星号。\n"
    "4. 页末脚注（如“①……——编者注”）照原样转录在正文之后。\n"
    "5. 页眉的书名与版心页码若有，照原位置转录。\n"
    "6. **只转录看得清的字**，看不清用□占位，绝对不要猜测、脑补或改写内容。\n"
    "7. 直接输出转录正文，不加“以下是转录”等任何说明。"
)


def build_worklist():
    con = sqlite3.connect(str(DB))
    rows = list(con.execute(
        "select pdf_page, printed_page, length(coalesce(normalized_text,'')) "
        "from pages where book='文集' and volume='10' order by pdf_page"))
    con.close()
    work = [{"pdf": p, "printed": pp} for p, pp, n in rows if n >= BODY_MIN]
    WORKLIST.write_text(json.dumps(work, ensure_ascii=False), encoding="utf-8")
    print("卷10 worklist: %d 页（跳过 %d 页空白/扉页）" % (len(work), len(rows) - len(work)))
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
                done.add(int(o["pdf"]))
            except Exception:
                continue
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-worklist", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--scale", type=float, default=2.4)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--model", default="", help="覆盖模型，如 glm-4v-plus（识别黑体加粗，需付费额度）")
    args = ap.parse_args()
    if args.model:
        twn.MODEL = args.model

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
    todo = [w for w in work if w["pdf"] not in done]
    if args.sample:
        todo = todo[: args.sample]
    print("待转录 %d 页（已完成 %d，模型 %s，并发 %d）" % (len(todo), len(done), twn.MODEL, args.workers), flush=True)
    if not todo:
        print("无待处理页。")
        return

    src = "pdfs/文集/马克思恩格斯文集[第10卷]马克思恩格斯书信选编.pdf"
    doc = fitz.open(ROOT / src)
    fh = SIDECAR.open("a", encoding="utf-8")
    t0 = time.time()
    cnt = {"n": 0, "ok": 0, "err": 0, "trunc": 0, "ctok": 0}
    total = len(todo)

    def record(w, res):
        row = {"pdf": w["pdf"], "printed": w["printed"], "text": res.get("text", ""),
               "finish": res.get("finish", "")}
        if res.get("error"):
            row["error"] = res["error"]
        u = res.get("usage", {})
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
            cnt["ctok"] += row["ctok"]
            n = cnt["n"]
            if n % 20 == 0 or n == total:
                el = time.time() - t0
                rate = n / el if el else 0
                eta = (total - n) / rate / 60 if rate else 0
                with _print_lock:
                    print("  %d/%d ok=%d err=%d trunc=%d %.2f页/s ETA%.0f分" % (
                        n, total, cnt["ok"], cnt["err"], cnt["trunc"], rate, eta), flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        idx = 0
        futs = {}
        inflight = set()
        while idx < len(todo) or inflight:
            while idx < len(todo) and len(inflight) < args.workers * 3:
                w = todo[idx]
                idx += 1
                try:
                    b64 = grf.render_b64(doc, w["pdf"], args.scale)
                except Exception as e:
                    record(w, {"error": ("render:%s" % e)[:60]})
                    continue
                # plus 模型上限更高，可容纳整页正文 + 加粗标记；flash 硬顶 1024
                mt = 2048 if "plus" in twn.MODEL else 1024
                fut = ex.submit(twn.call_transcribe, api_key, b64, 5, mt)
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
    print("\n完成 %d 页：成功 %d 失败 %d 截断 %d，耗时 %.1f 分，ctok %d" % (
        cnt["n"], cnt["ok"], cnt["err"], cnt["trunc"], el, cnt["ctok"]), flush=True)


if __name__ == "__main__":
    main()
