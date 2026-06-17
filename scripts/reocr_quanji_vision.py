# -*- coding: utf-8 -*-
"""P2：用智谱 GLM-4V 对《马恩全集》早期卷(1-18/21/22)退化扫描页做多模态重识别。

产出 sidecar：data/quanji_reocr.jsonl（每页一行 {vol,pdf_page,text,finish,ptokens,ctokens}）。
断点续跑：重启后跳过 sidecar 里已完成的页。注入由 scripts/inject_quanji_reocr.py 负责（定向写 DB）。

设计要点：
  - 仅处理 book=全集 早期20卷、且现有 normalized_text 长度>=15 的「非空白」页（空白页保持原样不动）。
  - 渲染在主线程（fitz 非线程安全），API 调用走线程池并发；429/5xx 指数退避重试。
  - 后处理：剥 markdown(**/##/>)、拦截「这是/以下是/图片中…」解说行；结果异常(过短/含解说)则
    标记 reject，注入阶段对 reject 页保留原文（P1 精修版），绝不用坏结果覆盖。
  - 保留 DB 既有印刷页码（模型常省略页眉页码），本脚本只产出正文文本。

环境变量：ZHIPU_API_KEY（必需）。用法：
  python scripts/reocr_quanji_vision.py --workers 6 [--limit N] [--vols 1,4] [--scale 2.4]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
import sqlite3
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402

BOOK = "全集"
EARLY_VOLS = list(range(1, 19)) + [21, 22]
MANIFEST = ROOT / "config" / "manifest.yaml"
SIDECAR = ROOT / "data" / "quanji_reocr.jsonl"
BASE_URL = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
MODEL = os.environ.get("ZHIPU_VISION_MODEL", "glm-4v-flash")
MIN_NORM_LEN = 15  # 现有 normalized_text 短于此视为空白页，跳过

PROMPT = (
    "这是《马克思恩格斯全集》中文版的一页扫描图片。请把页面正文逐字准确转录为规范简体中文，"
    "严格要求：①只输出页面上实际印刷的正文文字，保持原有分段；②不要翻译、不要解释、不要补全、"
    "不要输出任何「这是/以下是/图片中」之类的说明，也不要使用markdown标记；③确实无法辨认的字用「□」代替。"
    "直接输出转录的正文文本。"
)

_META_RE = re.compile(r"^(这是|这段|以下是|图片中|本页|该页|根据|抱歉|无法|页面|很抱歉|图中|此页|这张)")
_print_lock = threading.Lock()
_write_lock = threading.Lock()


def load_done() -> set[tuple[int, int]]:
    done: set[tuple[int, int]] = set()
    if SIDECAR.exists():
        for line in SIDECAR.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                # 仅把「拿到真实响应」的页视为已完成；error 页(网络/接口失败)留待重跑重试。
                if o.get("finish") == "error":
                    continue
                done.add((int(o["vol"]), int(o["pdf_page"])))
            except Exception:
                continue
    return done


def clean_text(txt: str) -> tuple[str, bool]:
    """剥 markdown、去解说行；返回 (clean, ok)。ok=False 表示疑似坏结果(应保留原文)。"""
    if not txt:
        return "", False
    txt = txt.replace("**", "").replace("##", "").replace("```", "")
    lines = []
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if _META_RE.match(s):
            continue  # 丢弃解说行
        s = re.sub(r"^#+\s*", "", s)
        s = re.sub(r"^>\s*", "", s)
        lines.append(s)
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    ok = len(out) >= 10
    return out, ok


def render_b64(doc, pdf_page: int, scale: float) -> str:
    pix = doc[pdf_page - 1].get_pixmap(matrix=fitz.Matrix(scale, scale))
    return base64.b64encode(pix.tobytes("png")).decode()


def call_api(api_key: str, b64: str, retries: int = 5) -> dict:
    url = BASE_URL + "/chat/completions"
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
        "temperature": 0.1,
        "max_tokens": 1024,
    }
    data = json.dumps(body).encode()
    delay = 3.0
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode())
            ch = resp["choices"][0]
            return {
                "text": ch["message"]["content"],
                "finish": ch.get("finish_reason", ""),
                "usage": resp.get("usage", {}),
            }
        except urllib.error.HTTPError as e:
            code = e.code
            last = f"HTTP {code}"
            if code in (429, 500, 502, 503, 529):
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            # 其它 4xx（如内容审核 1301）：不重试，返回空让上层保留原文
            try:
                last = f"HTTP {code}: {e.read().decode('utf-8','replace')[:120]}"
            except Exception:
                pass
            return {"text": "", "finish": "error", "usage": {}, "error": last}
        except Exception as e:  # 网络超时等
            last = str(e)[:120]
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return {"text": "", "finish": "error", "usage": {}, "error": last}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 页（调试）")
    ap.add_argument("--vols", default="", help="只处理指定卷，逗号分隔（调试）")
    ap.add_argument("--scale", type=float, default=2.4)
    args = ap.parse_args()

    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("缺少环境变量 ZHIPU_API_KEY")

    vols = [int(v) for v in args.vols.split(",") if v.strip()] if args.vols else EARLY_VOLS
    man = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    files = {x["volume"]: x["file"] for x in man[BOOK]}

    conn = sqlite3.connect(str(DB_PATH))
    todo: list[tuple[int, int]] = []
    for v in vols:
        rows = conn.execute(
            "SELECT pdf_page FROM pages WHERE book=? AND volume=? AND length(normalized_text)>=? ORDER BY pdf_page",
            (BOOK, v, MIN_NORM_LEN),
        ).fetchall()
        for (p,) in rows:
            todo.append((v, int(p)))
    conn.close()

    done = load_done()
    todo = [t for t in todo if t not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"待处理页：{len(todo)}（已完成 {len(done)}，模型 {MODEL}，并发 {args.workers}）", flush=True)
    if not todo:
        print("无待处理页，结束。")
        return

    # 按卷分组，主线程逐卷渲染、线程池并发调 API
    by_vol: dict[int, list[int]] = {}
    for v, p in todo:
        by_vol.setdefault(v, []).append(p)

    t0 = time.time()
    counter = {"done": 0, "ok": 0, "reject": 0, "trunc": 0, "ptok": 0, "ctok": 0}
    total = len(todo)
    sidecar_fh = SIDECAR.open("a", encoding="utf-8")

    def record(vol, page, res):
        text, ok = clean_text(res.get("text", ""))
        trunc = res.get("finish") == "length"
        row = {"vol": vol, "pdf_page": page, "text": text, "ok": ok,
               "finish": res.get("finish", ""), "trunc": trunc,
               "ptokens": res.get("usage", {}).get("prompt_tokens", 0),
               "ctokens": res.get("usage", {}).get("completion_tokens", 0)}
        with _write_lock:
            sidecar_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            sidecar_fh.flush()
            counter["done"] += 1
            counter["ok"] += 1 if ok else 0
            counter["reject"] += 0 if ok else 1
            counter["trunc"] += 1 if trunc else 0
            counter["ptok"] += row["ptokens"]
            counter["ctok"] += row["ctokens"]
            n = counter["done"]
            if n % 25 == 0 or n == total:
                el = time.time() - t0
                rate = n / el if el else 0
                eta = (total - n) / rate / 60 if rate else 0
                with _print_lock:
                    print(f"  {n}/{total} ok={counter['ok']} reject={counter['reject']} "
                          f"trunc={counter['trunc']} {rate:.2f}页/s ETA{eta:.0f}分 "
                          f"tok(in/out)={counter['ptok']}/{counter['ctok']}", flush=True)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for v in sorted(by_vol):
                doc = fitz.open(ROOT / files[v])
                futs = {}
                # 限制在途任务数，避免一次性渲染整卷占内存
                pages = by_vol[v]
                idx = 0
                inflight = set()
                while idx < len(pages) or inflight:
                    while idx < len(pages) and len(inflight) < args.workers * 3:
                        p = pages[idx]; idx += 1
                        b64 = render_b64(doc, p, args.scale)
                        fut = ex.submit(call_api, api_key, b64)
                        futs[fut] = (v, p)
                        inflight.add(fut)
                    done_futs = [f for f in inflight if f.done()]
                    if not done_futs:
                        nxt = next(as_completed(inflight))
                        done_futs = [nxt]
                    for f in done_futs:
                        vol, page = futs.pop(f)
                        inflight.discard(f)
                        try:
                            record(vol, page, f.result())
                        except Exception as e:
                            record(vol, page, {"text": "", "finish": "error", "error": str(e)[:100]})
                doc.close()
    finally:
        sidecar_fh.close()

    el = (time.time() - t0) / 60
    print(f"\n完成：{counter['done']} 页，ok={counter['ok']} reject={counter['reject']} "
          f"trunc={counter['trunc']}，耗时 {el:.1f} 分。", flush=True)
    print(f"  累计 token in/out = {counter['ptok']}/{counter['ctok']}（glm-4v-flash 免费档）", flush=True)


if __name__ == "__main__":
    main()
