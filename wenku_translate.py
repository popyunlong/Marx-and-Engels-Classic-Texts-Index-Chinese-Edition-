# -*- coding: utf-8 -*-
"""「原文文库」阅读器内置翻译的**永久缓存**。

核心成本逻辑：语料是固定文本，同一段原文+目标语只需翻译一次，结果入库永久缓存、
全站所有用户共享——于是翻译成本是「一次性 + 随阅读自然摊销」，不是每次阅读都烧钱。

本模块只管缓存与编排（命中直接返回、未命中交给回调去调 AI），不依赖 app.py，
故 app.py import 它不构成循环。实际的 AI 调用与用量记账留在 app.py 路由里。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Callable

from runtime_env import APPDATA_DIR, secure_db_file

DB_PATH = APPDATA_DIR / "wenku_translations.sqlite3"


def _conn() -> sqlite3.Connection:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    secure_db_file(DB_PATH)
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS wenku_translations(
                key TEXT PRIMARY KEY,
                src_lang TEXT,
                tgt_lang TEXT,
                src_text TEXT,
                translated TEXT,
                model TEXT,
                created_at REAL
            )"""
        )


def _key(src_text: str, src: str, tgt: str) -> str:
    return hashlib.sha256(f"{src}␟{tgt}␟{src_text}".encode("utf-8")).hexdigest()


def get_cached(texts: list[str], src: str, tgt: str) -> dict[str, str]:
    """返回 {原文: 译文}，仅含已缓存命中的。"""
    out: dict[str, str] = {}
    uniq = [t for t in dict.fromkeys(texts) if t]
    if not uniq:
        return out
    with _conn() as c:
        for t in uniq:
            row = c.execute(
                "SELECT translated FROM wenku_translations WHERE key=?", (_key(t, src, tgt),)
            ).fetchone()
            if row and row[0]:
                out[t] = row[0]
    return out


def put(src_text: str, translated: str, src: str, tgt: str, model: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO wenku_translations"
            "(key, src_lang, tgt_lang, src_text, translated, model, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (_key(src_text, src, tgt), src, tgt, src_text, translated, model, time.time()),
        )


def translate_aligned(
    texts: list[str],
    src: str,
    tgt: str,
    translate_misses: Callable[[list[str]], list[str | None]],
    *,
    max_new: int = 12,
    model: str = "",
) -> dict:
    """对 texts（可含重复）返回与之等长对齐的译文列表。

    - 先查缓存；未命中的最多取 max_new 段交给 translate_misses 回调（一次 AI 批量）；
      回调返回与入参等长的译文列表（缺失项为 None）。命中与新译都写回缓存。
    - 超过 max_new 的未命中本次不译（aligned 里为 None），由前端滚动时再次请求补齐。
    返回 {"translations": [...对齐...], "new": 本次新译段数, "remaining": 仍未译段数}。
    """
    cached = get_cached(texts, src, tgt)
    uniq_missing = [t for t in dict.fromkeys(texts) if t and t not in cached]
    todo = uniq_missing[:max_new]
    new_count = 0
    if todo:
        outs = translate_misses(todo) or []
        for t, o in zip(todo, outs):
            o = (o or "").strip()
            if o:
                put(t, o, src, tgt, model=model)
                cached[t] = o
                new_count += 1
    aligned = [cached.get(t) for t in texts]
    return {
        "translations": aligned,
        "new": new_count,
        "remaining": max(0, len(uniq_missing) - len(todo)),
    }
