# -*- coding: utf-8 -*-
"""自托管「原文文库」（static library）的 Web 接入。

设计目标：在不占用服务器渲染算力（无 PDF 页面图渲染）的前提下，把镜像进
static_library/<folder>/ 的纯静态 HTML 书库（如列宁 ПСС 俄文原文）以「自托管 +
后台可控权限」的方式开放为阅读器卡片。与既有的 PDF 页面图阅读器完全独立、互不影响。

三条路由：
  GET /wenku                      文库首页：陈列各书
  GET /wenku/<book_key>           轻阅读器外壳（同源 iframe 加载静态页，读页码锚点生成引文）
  GET /wenku/raw/<book_key>/<p>   直发静态文件（HTML/CSS/图片）——权限硬门禁 + 防路径穿越

权限：全部经 app 传入的 require_content_feature("static_library") 把关，行为与站内其它
会员内容一致（管理员放行、未登录跳登录、未开通跳套餐）。因此内容由本服务器发出、可被
后台权限真正拦住（这正是当初否掉「iframe 公开站」方案、改自托管的原因）。

本模块只依赖 flask + pyyaml，不反向依赖 app.py，故 app.py 顶部 import 它不构成循环。
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from flask import abort, render_template, request, send_file

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_LIBRARY_ROOT = PROJECT_ROOT / "static_library"
CONFIG_PATH = PROJECT_ROOT / "config" / "static_books.yaml"

# 按配置文件 mtime 缓存解析结果；书的「是否有本地内容」仍每次实时按目录存在性过滤。
_cache: dict = {"mtime": None, "raw": []}


def _load_raw_books() -> list[dict]:
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return []
    if _cache["mtime"] != mtime:
        raw: list[dict] = []
        try:
            data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            for item in (data.get("books") or []):
                if isinstance(item, dict) and item.get("key"):
                    raw.append(item)
        except Exception:
            raw = []
        _cache["mtime"] = mtime
        _cache["raw"] = raw
    return _cache["raw"]


def _book_folder(book: dict) -> Path:
    return STATIC_LIBRARY_ROOT / str(book.get("folder") or book.get("key") or "")


def load_books() -> list[dict]:
    """只返回「本地确有内容」的书（目录存在），保证文库卡片不出现空壳。"""
    return [b for b in _load_raw_books() if _book_folder(b).is_dir()]


def static_library_has_content() -> bool:
    return bool(load_books())


def _get_book(key: str) -> dict | None:
    for b in load_books():
        if str(b.get("key")) == str(key):
            return b
    return None


def _pick_volume(book: dict) -> dict | None:
    volumes = book.get("volumes") or []
    if not volumes:
        return None
    vol_n = request.args.get("vol", type=int)
    if vol_n is not None:
        for v in volumes:
            try:
                if int(v.get("n")) == vol_n:
                    return v
            except (TypeError, ValueError):
                continue
    return volumes[0]


def register_static_library(app, *, require_content_feature, ai_web_allowed=None) -> None:
    """在 app 上注册文库路由。require_content_feature 由 app.py 传入（其 _require_content_feature）；
    ai_web_allowed 为可选回调（app 的 _ai_web_access_enabled），决定阅读器是否显示「智谱联网」引擎选项。"""

    @app.route("/wenku")
    def wenku_home():  # endpoint: wenku_home
        require_content_feature("static_library")
        return render_template(
            "wenku.html",
            app_name=app.config.get("APP_NAME", ""),
            books=load_books(),
        )

    @app.route("/wenku/<book_key>")
    def wenku_reader(book_key):  # endpoint: wenku_reader
        require_content_feature("static_library")
        book = _get_book(book_key)
        if not book:
            abort(404)
        volume = _pick_volume(book)
        if not volume:
            abort(404)
        start_doc = (request.args.get("doc") or volume.get("index") or "").lstrip("/")
        serve_prefix = str(book.get("serve_prefix") or f"/wenku/raw/{book_key}").rstrip("/")
        config_payload = {
            "book_key": book_key,
            "serve_prefix": serve_prefix,
            "start_doc": start_doc,
            "lang": book.get("lang") or "ru",
            "title_zh": book.get("title_zh"),
            "citation": book.get("citation") or {},
            # 整卷字典都给前端（引文模板可引用任意逐卷字段，如 vol_zh/vol_de/publisher_*）。
            "volume": {k: v for k, v in volume.items() if k != "index"},
        }
        return render_template(
            "wenku_reader.html",
            book=book,
            volume=volume,
            serve_prefix=serve_prefix,
            start_doc=start_doc,
            ai_web_allowed=bool(ai_web_allowed()) if ai_web_allowed else False,
            wenku_config_json=json.dumps(config_payload, ensure_ascii=False),
        )

    @app.route("/wenku/raw/<book_key>/<path:relpath>")
    def wenku_raw(book_key, relpath):  # endpoint: wenku_raw
        require_content_feature("static_library")
        book = _get_book(book_key)
        if not book:
            abort(404)
        base = _book_folder(book).resolve()
        target = (base / relpath).resolve()
        try:
            target.relative_to(base)  # 防 ../ 路径穿越
        except ValueError:
            abort(404)
        if not target.is_file():
            abort(404)
        # 纯文本/图片直发，无渲染、无缓存目录膨胀。
        return send_file(str(target))
