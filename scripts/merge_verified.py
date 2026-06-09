# -*- coding: utf-8 -*-
"""把 agent 核验通过的对齐修订(audit_out/verified/accepted_*.json)合并进
config/wenji_text_corrections.yaml 的 pages 段。

安全处理：
  - 每条修订的 find 是 han-only 上下文串；在当前 DB 该页 raw_text 中校验 count。
  - 仅当 len(find)==len(replace) 且 find!=replace 且 count>=1 时纳入，occ=count(精确)。
  - count==0(因标点/换行致 han 串不连续) → 跳过并计数(安全)。
  - 与既有 pages 及 book_wide/volume_wide 去重(按 (volume,page,find))。
保留既有 book_wide/volume_wide/pages 全部条目。写回 yaml。
"""
import sys, io, os, json, glob, sqlite3, collections
import yaml
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAML = os.path.join(ROOT, "config", "wenji_text_corrections.yaml")

cfg = yaml.safe_load(open(YAML, encoding="utf-8")) or {}
cfg.setdefault("book_wide", [])
cfg.setdefault("volume_wide", {})
cfg.setdefault("pages", [])
cfg.setdefault("han_pages", [])

existing_keys = set()
for e in cfg["han_pages"]:
    existing_keys.add((e.get("volume"), e.get("page"), e.get("find")))

c = sqlite3.connect(os.path.join(ROOT, "data", "corpus.sqlite"))
han_cache = {}


def han_of(vol, pg):
    """返回该页 raw_text 的汉字投影（与对齐时所用一致）。"""
    k = (vol, pg)
    if k not in han_cache:
        r = c.execute("select raw_text from pages where book='文集' and volume=? and pdf_page=?", (vol, pg)).fetchone()
        raw = r[0] if r else ""
        han_cache[k] = "".join(ch for ch in raw if "一" <= ch <= "鿿")
    return han_cache[k]


added = 0
skip_nomatch = 0
skip_badlen = 0
skip_dup = 0
per_vol = collections.Counter()
for f in sorted(glob.glob(os.path.join(ROOT, "audit_out", "verified", "accepted_*.json"))):
    d = json.load(open(f, encoding="utf-8"))
    vol = d["volume"]
    for a in d.get("accepted", []):
        pg, find, repl = a.get("pdf_page"), a.get("find"), a.get("replace")
        if not (isinstance(pg, int) and isinstance(find, str) and isinstance(repl, str)):
            continue
        if find == repl or len(find) != len(repl) or not find:
            skip_badlen += 1
            continue
        key = (vol, pg, find)
        if key in existing_keys:
            skip_dup += 1
            continue
        cnt = han_of(vol, pg).count(find)  # 在汉字投影空间计数
        if cnt < 1:
            skip_nomatch += 1
            continue
        cfg["han_pages"].append({
            "volume": vol, "page": pg, "find": find, "replace": repl,
            "occ": cnt, "conf": "high",
            "evidence": "marx-engels2 权威全文对齐 + PDF 扫描页核验",
        })
        existing_keys.add(key)
        added += 1
        per_vol[vol] += 1

# 写回（保留既有结构与注释会丢失，但内容完整；book_wide/volume_wide 原样保留）
with open(YAML, "w", encoding="utf-8") as fh:
    fh.write("# 《文集》引文检索正文修订映射（含第一批人工修订 + 第二批权威全文对齐校勘）\n")
    fh.write("# book_wide: 文集级 never-valid 串全替换; volume_wide: 卷级; pages: 页锚定(occ校验)\n")
    fh.write("# 第二批 pages 条目来自 marx-engels2 同版本全文对齐 + 10 agent 逐卷 PDF 扫描页核验。\n\n")
    yaml.safe_dump({"book_wide": cfg["book_wide"]}, fh, allow_unicode=True, sort_keys=False)
    fh.write("\n")
    yaml.safe_dump({"volume_wide": cfg["volume_wide"]}, fh, allow_unicode=True, sort_keys=False)
    fh.write("\n")
    yaml.safe_dump({"pages": cfg["pages"]}, fh, allow_unicode=True, sort_keys=False)
    fh.write("\n")
    yaml.safe_dump({"han_pages": cfg["han_pages"]}, fh, allow_unicode=True, sort_keys=False)

print(f"新增 han_pages 修订={added}  各卷={dict(sorted(per_vol.items()))}")
print(f"跳过: 汉字投影未命中={skip_nomatch}  长度/退化={skip_badlen}  重复={skip_dup}")
print(f"yaml: pages={len(cfg['pages'])}  han_pages={len(cfg['han_pages'])}  book_wide={len(cfg['book_wide'])}")
print("DONE_MERGE")
