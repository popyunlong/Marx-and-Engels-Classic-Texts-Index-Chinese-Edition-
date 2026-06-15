# -*- coding: utf-8 -*-
"""《马恩全集》引文数据精修（轻量版·P1）：定向重写 book=全集 的 pages 文本。

做三件事，仅作用于 book='全集'，绝不触碰其它书库：
  1) 繁→简（opencc t2s）：早期卷(1-18/21/22)OCR 里残留的繁体字(馬/會/戰/問/濟…)统一为简体，
     使简体查询可命中；对已是简体的文本近乎无操作。
  2) 符号乱码清理：把 OCR 误识的比值号 ∶(U+2236)→：、去除 NUL/控制符（仅影响片段显示，
     检索侧 normalize 本就剥标点）。
  3) 高置信「等长整词」误识修正（config/quanji_text_corrections.yaml）：查产阶级→资产阶级、
     惩产阶级→无产阶级、禹克思→马克思、糨济→经济……每条均经「干净卷零出现」经验闸门核验，
     且 find/replace 等长以保高亮对齐。

raw_text 与 normalized_text 都重写并重算 sha256。脚本幂等（再次运行结果不变）。
opencc 缺失→只跳过 t2s；配置缺失→只跳过词典；都不报错、不中断。

用法：python scripts/refine_quanji_text.py        # 全集全部卷
      python scripts/refine_quanji_text.py --dry  # 只统计改动，不写库
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from build_index import DB_PATH, _t2s, normalize  # noqa: E402

BOOK = "全集"
CORRECTIONS_PATH = ROOT / "config" / "quanji_text_corrections.yaml"
HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

# 符号级清理（仅安全、确定性的替换；不动正文字）。
_SYMBOL_MAP = {
    "∶": "：",  # ∶ RATIO → 中文冒号（OCR 常把「：」识别成比值号）
    " ": "",     # 不换行空格
    "\x00": "",
}


def _load_corrections() -> list[tuple[str, str]]:
    if not CORRECTIONS_PATH.exists():
        print(f"  注意：未找到 {CORRECTIONS_PATH.name}，跳过整词修正。", file=sys.stderr)
        return []
    try:
        data = yaml.safe_load(CORRECTIONS_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # 配置损坏不应中断
        print(f"  警告：解析修正字典失败({exc})，跳过整词修正。", file=sys.stderr)
        return []
    raw = data.get("corrections") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return []
    pairs: list[tuple[str, str]] = []
    for find, repl in raw.items():
        find, repl = str(find), str(repl)
        if not find or len(find) != len(repl):
            print(f"  跳过非等长修正项：{find}->{repl}", file=sys.stderr)
            continue
        pairs.append((find, repl))
    # 长键优先，避免短键先替导致的意外覆盖。
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def _symbol_clean(text: str) -> str:
    for a, b in _SYMBOL_MAP.items():
        if a in text:
            text = text.replace(a, b)
    return text


def _apply_corrections(text: str, pairs: list[tuple[str, str]]) -> str:
    for find, repl in pairs:
        if find in text:
            text = text.replace(find, repl)
    return text


def update_hash() -> None:
    if not DB_PATH.exists():
        return
    digest = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    HASH_PATH.write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="定向精修《马恩全集》pages 文本（繁简+乱码+整词修正）。")
    ap.add_argument("--dry", action="store_true", help="只统计改动，不写库")
    args = ap.parse_args()

    pairs = _load_corrections()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT rowid, raw_text, normalized_text FROM pages WHERE book = ?",
            (BOOK,),
        ).fetchall()
        print(f"《{BOOK}》待处理页数：{len(rows)}；整词修正项：{len(pairs)} 条。")

        updates = []
        raw_changed = norm_changed = corr_norm_hits = 0
        for rowid, raw, norm in rows:
            raw = raw or ""
            new_raw = _apply_corrections(_symbol_clean(_t2s(raw)), pairs)
            new_norm = _apply_corrections(normalize(new_raw), pairs)
            if new_raw != raw:
                raw_changed += 1
            if new_norm != (norm or ""):
                norm_changed += 1
            # 统计：本页 normalized 因整词修正净命中（粗略）
            base_norm = normalize(new_raw)
            if new_norm != base_norm:
                corr_norm_hits += 1
            if new_raw != raw or new_norm != (norm or ""):
                updates.append((new_raw, new_norm, rowid))

        print(f"  raw_text 变更页：{raw_changed}；normalized_text 变更页：{norm_changed}；"
              f"含整词修正净生效页：{corr_norm_hits}。")

        if args.dry:
            print("  --dry：未写库。")
            return

        conn.executemany(
            "UPDATE pages SET raw_text = ?, normalized_text = ? WHERE rowid = ?",
            updates,
        )
        conn.commit()
        print(f"  已写回 {len(updates)} 页。")
    finally:
        conn.close()

    update_hash()
    print("  已重算 corpus.sqlite.sha256。")


if __name__ == "__main__":
    main()
