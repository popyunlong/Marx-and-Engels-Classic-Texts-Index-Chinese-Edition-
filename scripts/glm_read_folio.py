# -*- coding: utf-8 -*-
"""GLM-4V 读印刷页码（folio）——渲染某 PDF 页，让 glm-4v-flash 只回最外侧页眉/页脚的阿拉伯页码。
既做 Phase1 抽样复核，也作 Phase2 视觉补页码的核心。免费档：温和并发+退避。

返回 int 或 None（None=页面无阿拉伯页码/罗马页码/无法辨认）。
环境变量 ZHIPU_API_KEY 提供密钥。
"""
from __future__ import annotations
import base64, json, os, re, sys, time, unicodedata, urllib.request, urllib.error
from pathlib import Path
import fitz

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
MODEL = os.environ.get("ZHIPU_VISION_MODEL", "glm-4v-flash")
FW = str.maketrans("０１２３４５６７８９", "0123456789")

PROMPT = (
    "这是一页中文书籍的扫描或排版图片。请只找出并输出这一页最外侧（页眉最上方或页脚最下方、"
    "靠版心边缘）印刷的『书页码』阿拉伯数字。要求：①只输出那个页码数字本身，不要输出任何其他文字；"
    "②不要把正文里的年份、脚注编号（如(11)）、图表编号、公式里的数字当作页码；"
    "③如果该页没有印刷阿拉伯页码（例如扉页、目录、罗马数字页、纯图版或空白页），只输出 NONE。"
)


def render_b64(doc, pdf_page: int, scale: float = 2.0) -> str:
    pix = doc[pdf_page - 1].get_pixmap(matrix=fitz.Matrix(scale, scale))
    return base64.b64encode(pix.tobytes("png")).decode()


def _parse_folio(text: str):
    if not text:
        return None
    t = unicodedata.normalize("NFKC", text).translate(FW).strip()
    if re.search(r"\bNONE\b", t, re.I) or "无" in t or "没有" in t:
        # 仍可能形如 "NONE" 或 "无页码"
        if not re.search(r"\d", t):
            return None
    # 取第一个 1-4 位整数
    m = re.search(r"(?<!\d)(\d{1,4})(?!\d)", t)
    if not m:
        return None
    v = int(m.group(1))
    return v if 1 <= v <= 2500 else None


def call_folio(api_key: str, b64: str, retries: int = 5):
    url = BASE_URL + "/chat/completions"
    body = {"model": MODEL, "temperature": 0.05, "max_tokens": 12,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    data = json.dumps(body).encode()
    delay = 3.0; last = ""
    for _ in range(retries):
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.loads(r.read().decode())
            ch = resp["choices"][0]
            return {"raw": ch["message"]["content"], "folio": _parse_folio(ch["message"]["content"]),
                    "usage": resp.get("usage", {})}
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (429, 500, 502, 503, 529):
                time.sleep(delay); delay = min(delay * 2, 60); continue
            return {"raw": "", "folio": None, "error": f"HTTP {code}"}
        except Exception as e:
            last = str(e)[:100]; time.sleep(delay); delay = min(delay * 2, 60)
    return {"raw": "", "folio": None, "error": last}


def read_folio(api_key, source_file, pdf_page, scale=2.0):
    with fitz.open(ROOT / source_file) as doc:
        b64 = render_b64(doc, pdf_page, scale)
    return call_folio(api_key, b64)


# ---- 抽样复核 CLI ----
def _spotcheck():
    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("缺少 ZHIPU_API_KEY")
    picks = json.loads((ROOT / "data" / "_pagefix_spotcheck.json").read_text(encoding="utf-8"))
    print(f"复核 {len(picks)} 页（vision=glm-4v-flash）\n")
    agree_target = agree_recorded = 0
    rows = []
    for p in picks:
        res = read_folio(api_key, p["src"], p["pdf"])
        v = res.get("folio")
        tgt = int(p["new"]); old = p["old"]
        mark = "✓采纳" if v == tgt else ("=原值" if (old.isdigit() and v == int(old)) else "≠")
        if v == tgt: agree_target += 1
        if old.isdigit() and v == int(old): agree_recorded += 1
        print(f"  [{p['book'][:6]:6}] pdf{p['pdf']:<4} 原{old:>6} →拟{p['new']:>4}[{p['cls']}] | 视觉读到={str(v):>5} {mark}")
        rows.append({**p, "vision": v, "vraw": res.get("raw", "")})
        time.sleep(0.6)
    (ROOT / "data" / "_spotcheck_result.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(picks)
    print(f"\n视觉与『拟修目标』一致: {agree_target}/{n}   与『原记录值』一致: {agree_recorded}/{n}")


if __name__ == "__main__":
    _spotcheck()
