# -*- coding: utf-8 -*-
"""自托管「静态 HTML 书库」的 Web 接入（原文文库 /wenku + 流式阅读 /liushi 共用一套机制）。

设计目标：在不占用服务器渲染算力（无 PDF 页面图渲染）的前提下，把镜像进
<root>/<folder>/ 的纯静态 HTML 书库以「自托管 + 后台可控权限」的方式开放为阅读器卡片。
与既有的 PDF 页面图阅读器完全独立、互不影响。

两套书库共用同一份路由/阅读器代码，仅数据源（配置 + 内容根）、路由前缀、权限键不同：
  - 原文文库 /wenku   ：列宁ПСС俄文 / MEGA / MEW 德文 / marxists.org 英文，权限 static_library
  - 流式阅读 /liushi  ：《马克思恩格斯文集》中文网页适配版，权限 stream_reading

三条路由（按 prefix 复用）：
  GET /<prefix>                      文库首页：陈列各书
  GET /<prefix>/<book_key>           轻阅读器外壳（同源 iframe 加载静态页，读页码锚点生成引文）
  GET /<prefix>/raw/<book_key>/<p>   直发静态文件（HTML/CSS/图片）——权限硬门禁 + 防路径穿越

权限：全部经 app 传入的 require_content_feature(<feature>) 把关，行为与站内其它会员内容一致
（管理员放行、未登录跳登录、未开通跳套餐）。

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
STREAM_LIBRARY_ROOT = PROJECT_ROOT / "stream_library"
STREAM_CONFIG_PATH = PROJECT_ROOT / "config" / "stream_books.yaml"


class _BookSource:
    """一个自托管 HTML 书库的数据源：配置文件 + 内容根目录。按配置文件 mtime 缓存解析结果；
    每本书「是否有本地内容」仍每次实时按目录存在性过滤，保证文库卡片不出现空壳。"""

    def __init__(self, config_path: Path, root: Path) -> None:
        self.config_path = Path(config_path)
        self.root = Path(root)
        self._cache: dict = {"mtime": None, "raw": []}

    def _load_raw_books(self) -> list[dict]:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return []
        if self._cache["mtime"] != mtime:
            raw: list[dict] = []
            try:
                data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                for item in (data.get("books") or []):
                    if isinstance(item, dict) and item.get("key"):
                        raw.append(item)
            except Exception:
                raw = []
            self._cache["mtime"] = mtime
            self._cache["raw"] = raw
        return self._cache["raw"]

    def book_folder(self, book: dict) -> Path:
        return self.root / str(book.get("folder") or book.get("key") or "")

    def load_books(self) -> list[dict]:
        """只返回「本地确有内容」的书（目录存在）。"""
        return [b for b in self._load_raw_books() if self.book_folder(b).is_dir()]

    def has_content(self) -> bool:
        return bool(self.load_books())

    def get_book(self, key: str) -> dict | None:
        for b in self.load_books():
            if str(b.get("key")) == str(key):
                return b
        return None


# 默认数据源。模块级 load_books()/static_library_has_content() 维持旧接口不变（被 app.py 引用）。
_STATIC = _BookSource(CONFIG_PATH, STATIC_LIBRARY_ROOT)
_STREAM = _BookSource(STREAM_CONFIG_PATH, STREAM_LIBRARY_ROOT)


def load_books() -> list[dict]:
    return _STATIC.load_books()


def static_library_has_content() -> bool:
    return _STATIC.has_content()


def stream_reading_has_content() -> bool:
    return _STREAM.has_content()


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


def register_static_library(
    app,
    *,
    require_content_feature,
    ai_web_allowed=None,
    notes_access=None,
    source: _BookSource = _STATIC,
    prefix: str = "wenku",
    feature: str = "static_library",
    endpoint_prefix: str = "wenku",
    home_template: str = "wenku.html",
    reader_template: str = "wenku_reader.html",
    home_title: str = "原文文库",
    home_intro: str = "",
    ai_api_url: str = "/api/wenku/ai",
    translate_api_url: str = "/api/wenku/translate",
    back_targets: dict | None = None,
    reader_back_default: tuple | None = None,
) -> None:
    """在 app 上注册一套静态书库路由。默认参数＝原文文库 /wenku（行为与从前逐字一致）；
    传入不同 source/prefix/feature/endpoint_prefix 即可注册第二套（如流式阅读 /liushi）。
    require_content_feature 由 app.py 传入（其 _require_content_feature）；
    ai_web_allowed 为可选回调（app 的 _ai_web_access_enabled），决定阅读器是否显示「智谱联网」引擎选项。"""

    home_ep = f"{endpoint_prefix}_home"
    reader_ep = f"{endpoint_prefix}_reader"
    raw_ep = f"{endpoint_prefix}_raw"

    def home():  # endpoint: <endpoint_prefix>_home
        require_content_feature(feature)
        return render_template(
            home_template,
            app_name=app.config.get("APP_NAME", ""),
            books=source.load_books(),
            home_title=home_title,
            home_intro=home_intro,
            reader_endpoint=reader_ep,
        )

    def reader(book_key):  # endpoint: <endpoint_prefix>_reader
        require_content_feature(feature)
        book = source.get_book(book_key)
        if not book:
            abort(404)
        volume = _pick_volume(book)
        if not volume:
            abort(404)
        start_doc = (request.args.get("doc") or volume.get("index") or "").lstrip("/")
        # ?ai=0 → 隐藏「AI 导读」（普通阅读器/全文阅读器入口）；缺省显示（会员/AI 导学入口、/liushi、直链）。
        ai_enabled = request.args.get("ai") != "0"
        # 返回按钮目标：从「著作目录」(全文/AI导学阅读器)经 ?from= 进来的，回到该阅读器，
        # 而非已退役的原文文库首页；直链/流式则用注册时的默认 home。
        back_ep, back_title = home_ep, home_title
        bf = request.args.get("from")
        if back_targets and bf in back_targets:
            back_ep, back_title = back_targets[bf]
        serve_prefix = str(book.get("serve_prefix") or f"/{prefix}/raw/{book_key}").rstrip("/")
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
            reader_template,
            book=book,
            volume=volume,
            serve_prefix=serve_prefix,
            start_doc=start_doc,
            ai_web_allowed=bool(ai_web_allowed()) if ai_web_allowed else False,
            notes_access_enabled=bool(notes_access()) if notes_access else False,
            wenku_config_json=json.dumps(config_payload, ensure_ascii=False),
            home_endpoint=back_ep,
            home_title=back_title,
            ai_api_url=ai_api_url,
            translate_api_url=translate_api_url,
            ai_enabled=ai_enabled,
        )

    def raw(book_key, relpath):  # endpoint: <endpoint_prefix>_raw
        require_content_feature(feature)
        book = source.get_book(book_key)
        if not book:
            abort(404)
        base = source.book_folder(book).resolve()
        target = (base / relpath).resolve()
        try:
            target.relative_to(base)  # 防 ../ 路径穿越
        except ValueError:
            abort(404)
        if not target.is_file():
            abort(404)
        # 纯文本/图片直发，无渲染、无缓存目录膨胀。
        return send_file(str(target))

    app.add_url_rule(f"/{prefix}", endpoint=home_ep, view_func=home)
    app.add_url_rule(f"/{prefix}/<book_key>", endpoint=reader_ep, view_func=reader)
    app.add_url_rule(f"/{prefix}/raw/<book_key>/<path:relpath>", endpoint=raw_ep, view_func=raw)


def register_stream_reading(app, *, require_content_feature, ai_web_allowed=None, notes_access=None) -> None:
    """流式阅读 /liushi：《马克思恩格斯文集》中文网页适配版。复用上面的整套机制，
    仅换数据源/前缀/权限键/首页模板，并把翻译入口置空（中文书不做对照/划词翻译）。"""
    register_static_library(
        app,
        require_content_feature=require_content_feature,
        ai_web_allowed=ai_web_allowed,
        notes_access=notes_access,
        source=_STREAM,
        prefix="liushi",
        feature="stream_reading",
        endpoint_prefix="liushi",
        home_template="liushi_home.html",
        home_title="流式阅读",
        home_intro="《马克思恩格斯文集》网页适配版 · 文字可检索、可复制 · 内嵌印本页码，一键生成规范引文 · 支持 AI 导读",
        ai_api_url="/api/liushi/ai",
        translate_api_url="",
    )
