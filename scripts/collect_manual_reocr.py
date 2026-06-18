# -*- coding: utf-8 -*-
"""把 Claude 手工转录的单页 txt（data/_manual_txt/v{vol}_p{page}.txt）汇总追加进
重识别 sidecar（data/quanji_reocr.jsonl），格式与 reocr_quanji_vision.py 一致，
finish="manual"、ok=true。之后照常跑 inject_quanji_reocr.py 注入。

只收非空、长度>=10 的转录；文件名解析 v<卷>_p<pdf页>.txt。幂等：重复运行同一 txt 会
再追加一行，但 inject 的 load_latest 取每页最后一条 ok 行，结果一致。

用法：python scripts/collect_manual_reocr.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TXT_DIR = ROOT / "data" / "_manual_txt"
SIDECAR = ROOT / "data" / "quanji_reocr.jsonl"
_NAME_RE = re.compile(r"^v(\d+)_p(\d+)\.txt$")


def main() -> None:
    if not TXT_DIR.exists():
        raise SystemExit(f"目录不存在：{TXT_DIR}")
    added = skipped = 0
    with SIDECAR.open("a", encoding="utf-8") as fh:
        for txt in sorted(TXT_DIR.glob("v*_p*.txt")):
            m = _NAME_RE.match(txt.name)
            if not m:
                continue
            vol, page = int(m.group(1)), int(m.group(2))
            text = txt.read_text(encoding="utf-8").strip()
            if len(text) < 10:
                skipped += 1
                continue
            row = {"vol": vol, "pdf_page": page, "text": text, "ok": True,
                   "finish": "manual", "trunc": False, "ptokens": 0, "ctokens": 0}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            added += 1
    print(f"已追加 {added} 页手工转录，跳过(过短) {skipped}。", file=sys.stderr)
    print(f"已追加 {added} 页手工转录，跳过(过短) {skipped}。")


if __name__ == "__main__":
    main()
