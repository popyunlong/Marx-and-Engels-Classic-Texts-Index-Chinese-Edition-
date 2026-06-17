# -*- coding: utf-8 -*-
"""P2 注入：把 reocr_quanji_vision.py 产出的 sidecar（data/quanji_reocr.jsonl）写回 DB。

定向只改 book=全集 早期卷的 raw_text/normalized_text，**保留既有印刷页码**（模型常省略页眉页码）。
带质量闸门：只有「ok 且未截断且长度不显著缩水」的重识别结果才覆盖原文；否则保留 P1 精修原文，
绝不用坏结果（空/解说/截断/严重缩水）覆盖。重算 sha256，幂等。

用法：python scripts/inject_quanji_reocr.py [--dry] [--sidecar data/quanji_reocr.jsonl]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH, normalize, _t2s  # noqa: E402

BOOK = "全集"
DEFAULT_SIDECAR = ROOT / "data" / "quanji_reocr.jsonl"
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")
MIN_KEEP_LEN = 30          # 重识别正文过短 → 视为坏结果，保留原文
MIN_RATIO = 0.5            # 重识别归一长度 < 原文 0.5 倍 → 疑似缺损，保留原文


def load_latest(sidecar: Path) -> dict[tuple[int, int], dict]:
    """每页取最后一条 ok=True 的记录（重试场景下后写覆盖先写）。"""
    latest: dict[tuple[int, int], dict] = {}
    if not sidecar.exists():
        raise SystemExit(f"sidecar 不存在：{sidecar}")
    for line in sidecar.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if not o.get("ok"):
            continue
        latest[(int(o["vol"]), int(o["pdf_page"]))] = o
    return latest


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--sidecar", default=str(DEFAULT_SIDECAR))
    args = ap.parse_args()

    latest = load_latest(Path(args.sidecar))
    print(f"sidecar ok 记录页数：{len(latest)}")

    conn = sqlite3.connect(str(DB_PATH))
    updates = []
    stat = {"replace": 0, "keep_short": 0, "keep_trunc": 0, "keep_ratio": 0, "no_row": 0}
    try:
        for (vol, page), o in latest.items():
            row = conn.execute(
                "SELECT rowid, normalized_text FROM pages WHERE book=? AND volume=? AND pdf_page=?",
                (BOOK, vol, page),
            ).fetchone()
            if not row:
                stat["no_row"] += 1
                continue
            rowid, orig_norm = row
            # 模型偶尔输出繁体；统一 t2s 转简体，使 raw_text(片段显示)也为简体、与检索侧一致。
            text = _t2s((o.get("text") or "").strip())
            if o.get("trunc"):
                stat["keep_trunc"] += 1
                continue
            if len(text) < MIN_KEEP_LEN:
                stat["keep_short"] += 1
                continue
            new_norm = normalize(text)
            if len(new_norm) < MIN_RATIO * len(orig_norm or ""):
                stat["keep_ratio"] += 1
                continue
            updates.append((text, new_norm, rowid))
            stat["replace"] += 1

        print(f"  覆盖：{stat['replace']}  保留(截断{stat['keep_trunc']}/过短{stat['keep_short']}/"
              f"缩水{stat['keep_ratio']}/无此页{stat['no_row']})")
        if args.dry:
            print("  --dry：未写库。")
            return
        conn.executemany(
            "UPDATE pages SET raw_text=?, normalized_text=? WHERE rowid=?", updates
        )
        conn.commit()
        print(f"  已写回 {len(updates)} 页。")
    finally:
        conn.close()
    update_hash()
    print("  已重算 corpus.sqlite.sha256。")


if __name__ == "__main__":
    main()
