# -*- coding: utf-8 -*-
"""Phase3：GLM 逐页校对『可见正文(raw_text)』，只收集「改动 diff」，不落库。

对疑似含乱码/错别字的页(garble 扫描命中)，让 glm-4.5-flash 只挑出**明显 OCR 错误**并给出
最小替换 {before, after, why}。严格禁止润色改写、禁止动数学变量/公式/外文/数字/书名号内容。
产出 sidecar：data/phase3_edits.jsonl（每页一行，含模型给出的 edits 原样）。断点续跑。
落库(过安全闸+再生成 normalized_text)由 phase3_apply.py 负责，且先给人工抽样复核。

用法：
  python scripts/glm_proofread.py --build-worklist
  python scripts/glm_proofread.py --workers 4 [--limit N]   # 需 ZHIPU_API_KEY
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, threading, unicodedata, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "corpus.sqlite"
WORKLIST = ROOT / "data" / "phase3_worklist.json"
SIDECAR = ROOT / "data" / "phase3_edits.jsonl"
BASE_URL = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
MODEL = os.environ.get("ZHIPU_TEXT_MODEL", "glm-4.5-flash")
_wlock = threading.Lock(); _plock = threading.Lock()

REPL = "�"
CJK = re.compile(r"[一-鿿]")
NOISE_INLINE = re.compile(r"[一-鿿][A-Za-zΑ-Ωα-ωℓ][一-鿿]")

SYS = "你是严谨的古典文献校对员，只做 OCR 错误校正，绝不改写润色。"
PROMPT_TMPL = (
    "下面是《{book}》某页经 OCR 得到的中文正文，可能夹杂个别错别字、乱码杂符或错误标点。\n"
    "请只找出**明显的 OCR 识别错误**，输出一个 JSON 数组，每个元素形如 "
    "{{\"before\":\"原文中错误的最小片段\",\"after\":\"更正后的片段\",\"why\":\"简短原因\"}}。\n"
    "严格规则：\n"
    "① 只改：形近错别字(如『晶』应为『品』)、明显乱码杂符、错误标点符号；\n"
    "② 绝不改动：数学变量与公式(如 x、y、W、G、m-c-m)、外文原词、任何数字、书名号《》内的书名、人名地名专名；\n"
    "③ before 必须是页面里**确实出现**的原文片段，且尽量短(几个字)；after 与 before 长度相近；\n"
    "④ 拿不准就不改；不做同义替换、不润色、不增删句子、不调整语序；\n"
    "⑤ 若没有明显错误，输出 []。只输出 JSON 数组本身，不要任何解释文字。\n\n"
    "正文：\n{body}"
)


def build_worklist():
    con = sqlite3.connect(str(DB)); cur = con.cursor()
    work = []
    for book, sf, pdf, raw, norm in cur.execute(
            "SELECT book, source_file, pdf_page, raw_text, normalized_text FROM pages"):
        t = norm or ""
        flags = []
        if any(ord(c) < 9 or (14 <= ord(c) <= 31) for c in t): flags.append("ctrl")
        if REPL in (raw or "") or REPL in t: flags.append("repl")
        if any(0xE000 <= ord(c) <= 0xF8FF or 0xF900 <= ord(c) <= 0xFAFF for c in t): flags.append("pua")
        if len(CJK.findall(t)) >= 30 and len(NOISE_INLINE.findall(t)) >= 4: flags.append("inline")
        if flags:
            work.append({"src": sf, "book": book, "pdf": pdf, "flags": flags})
    con.close()
    WORKLIST.write_text(json.dumps(work, ensure_ascii=False), encoding="utf-8")
    from collections import Counter
    fc = Counter(f for w in work for f in w["flags"])
    print(f"worklist 页数: {len(work)}   标记分布: {dict(fc)}")
    return work


def call_proof(api_key, book, body, retries=5):
    url = BASE_URL + "/chat/completions"
    payload = {"model": MODEL, "temperature": 0.1, "max_tokens": 1024,
               "messages": [{"role": "system", "content": SYS},
                            {"role": "user", "content": PROMPT_TMPL.format(book=book, body=body[:2400])}]}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    delay = 3.0; last = ""
    for _ in range(retries):
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode())
            ch = resp["choices"][0]
            return {"text": ch["message"]["content"], "usage": resp.get("usage", {})}
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (429, 500, 502, 503, 529):
                time.sleep(delay); delay = min(delay * 2, 60); continue
            return {"text": "", "error": f"HTTP {code}"}
        except Exception as e:
            last = str(e)[:100]; time.sleep(delay); delay = min(delay * 2, 60)
    return {"text": "", "error": last}


def parse_edits(text):
    if not text:
        return []
    t = text.strip()
    t = re.sub(r"^```(json)?", "", t).strip().rstrip("`").strip()
    m = re.search(r"\[.*\]", t, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    if isinstance(arr, list):
        for e in arr:
            if isinstance(e, dict) and "before" in e and "after" in e:
                out.append({"before": str(e["before"]), "after": str(e["after"]), "why": str(e.get("why", ""))[:40]})
    return out


def load_done():
    done = set()
    if SIDECAR.exists():
        for line in SIDECAR.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line: continue
            try:
                o = json.loads(line)
                if o.get("error"): continue
                done.add((o["src"], int(o["pdf"])))
            except Exception: continue
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-worklist", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
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
    con = sqlite3.connect(str(DB)); cur = con.cursor()
    done = load_done()
    todo = [w for w in work if (w["src"], w["pdf"]) not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"待校对页: {len(todo)}（已完成 {len(done)}，模型 {MODEL}，并发 {args.workers}）", flush=True)
    if not todo:
        return
    fh = SIDECAR.open("a", encoding="utf-8")
    t0 = time.time(); cnt = {"n": 0, "edits": 0, "pages_with": 0, "err": 0}
    total = len(todo)

    # SQLite 连接非线程安全 → 每线程独立连接
    def work_one_safe(w):
        c = sqlite3.connect(str(DB))
        try:
            row = c.execute("SELECT raw_text FROM pages WHERE source_file=? AND pdf_page=?", (w["src"], w["pdf"])).fetchone()
        finally:
            c.close()
        raw = row[0] if row else ""
        res = call_proof(api_key, w["book"], raw)
        edits = parse_edits(res.get("text", ""))
        edits = [e for e in edits if e["before"] and e["before"] in raw and e["before"] != e["after"]]
        rec = {"src": w["src"], "pdf": w["pdf"], "book": w["book"], "flags": w["flags"], "edits": edits}
        if res.get("error"): rec["error"] = res["error"]
        with _wlock:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n"); fh.flush()
            cnt["n"] += 1
            if res.get("error"): cnt["err"] += 1
            cnt["edits"] += len(edits); cnt["pages_with"] += 1 if edits else 0
            n = cnt["n"]
            if n % 20 == 0 or n == total:
                el = time.time() - t0; rate = n / el if el else 0
                with _plock:
                    print(f"  {n}/{total} 有改动页={cnt['pages_with']} 累计edits={cnt['edits']} err={cnt['err']} {rate:.2f}页/s", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work_one_safe, todo))
    fh.close()
    print(f"\n完成 {cnt['n']} 页：有改动 {cnt['pages_with']} 页，累计 {cnt['edits']} 处，err {cnt['err']}", flush=True)


if __name__ == "__main__":
    main()
