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
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from flask import abort, redirect, render_template, request, send_file, url_for
from catalog_release import active_catalog, historic_catalog

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_LIBRARY_ROOT = Path(
    os.environ.get("MARX_RUNTIME_STATIC_LIBRARY_DIR") or (PROJECT_ROOT / "static_library")
).expanduser().resolve()
CONFIG_PATH = PROJECT_ROOT / "config" / "static_books.yaml"
STREAM_LIBRARY_ROOT = Path(
    os.environ.get("MARX_RUNTIME_STREAM_LIBRARY_DIR") or (PROJECT_ROOT / "stream_library")
).expanduser().resolve()
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
        """返回面向公共目录的书：内容目录存在，且未显式隐藏。"""
        return [
            b
            for b in self._load_raw_books()
            if self.book_folder(b).is_dir() and b.get("catalog_hidden") is not True
        ]

    def has_content(self) -> bool:
        return bool(self.load_books())

    def get_book(self, key: str) -> dict | None:
        # catalog_hidden 只控制首页/公共书目展示；历史阅读器与 raw URL 必须继续可达。
        for b in self._load_raw_books():
            if str(b.get("key")) == str(key) and self.book_folder(b).is_dir():
                return b
        return None


# 默认数据源。模块级 load_books()/static_library_has_content() 维持旧接口不变（被 app.py 引用）。
_STATIC = _BookSource(CONFIG_PATH, STATIC_LIBRARY_ROOT)
_STREAM = _BookSource(STREAM_CONFIG_PATH, STREAM_LIBRARY_ROOT)

# Resolve once per process: candidate and current instances may use different versions.
_CATALOG = active_catalog()
if _CATALOG:
    _STATIC.root = _CATALOG.root / 'static_library'
    _STREAM.root = _CATALOG.root / 'stream_library'


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
    record_book_read=None,
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
        if record_book_read:
            # 阅读周榜按具体卷册统计；多卷本的不同卷不再合并为整套书。
            book_title = str(book.get("title_zh") or book.get("key") or "").strip()
            volume_title = str(volume.get("vol_zh") or "").strip()
            if not volume_title:
                try:
                    volume_title = f"第{int(volume.get('n'))}卷"
                except (TypeError, ValueError):
                    volume_title = str(volume.get("label") or "").strip()
            record_book_read(f"{book_title}{volume_title}" if volume_title else book_title)
        # A single-document volume is deliberately one continuous reading surface.
        # Ignore stale ``?doc=sec-*.html`` resume links so they cannot drop readers
        # back into the retired generated section catalogue; ``?pg=`` remains
        # available to restore the printed-page anchor inside the full document.
        single_document = volume.get("single_document") is True
        requested_doc = None if single_document else request.args.get("doc")
        start_doc = (requested_doc or volume.get("index") or "").lstrip("/")
        # ?ai=0 → 隐藏「AI 导读」（普通阅读器/全文阅读器入口）；缺省显示（会员/AI 导学入口、/liushi、直链）。
        ai_enabled = request.args.get("ai") != "0"
        # 返回按钮目标：从「著作目录」(全文/AI导学阅读器)经 ?from= 进来的，回到该阅读器，
        # 而非已退役的原文文库首页；直链/流式则用注册时的默认 home。
        back_ep, back_title = home_ep, home_title
        bf = request.args.get("from")
        if back_targets and bf in back_targets:
            back_ep, back_title = back_targets[bf]
        serve_prefix = str(book.get("serve_prefix") or f"/{prefix}/raw/{book_key}").rstrip("/")
        if _CATALOG:
            serve_prefix = f'/{prefix}/v/{_CATALOG.version}/raw/{book_key}'
        source_lang = volume.get("lang") or book.get("lang") or "ru"

        def reader_flag(name: str, default: bool) -> bool:
            """Resolve an explicit per-volume/book reader capability without truthy strings."""
            value = volume.get(name, book.get(name, default))
            return value if isinstance(value, bool) else default

        config_payload = {
            "book_key": book_key,
            "serve_prefix": serve_prefix,
            "start_doc": start_doc,
            "single_document": single_document,
            # MEGA 等多语种套书会在同一书目下包含德/法/英卷；允许卷级语言覆盖书级默认值。
            "lang": source_lang,
            # Keep language and document capabilities separate.  In particular, an
            # English MEGA volume still has stable printed-page anchors, whereas the
            # legacy English collected works intentionally use article-only citations.
            "has_page_labels": reader_flag("has_page_labels", source_lang != "en"),
            "selection_citation": reader_flag(
                "selection_citation", source_lang == "zh"
            ),
            "translation_enabled": reader_flag(
                "translation_enabled", source_lang != "zh"
            ),
            # 合集首项可为独立研究专著；AI 导读、笔记与报错上下文使用卷级题名。
            "title_zh": volume.get("title_zh") or book.get("title_zh"),
            # 合集内可混入不同著者/语种的独立专著。卷级引文契约一旦存在就
            # 整体覆盖书级模板，避免残留合集的著者或外文模板。
            "citation": volume.get("citation") or book.get("citation") or {},
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

    def raw(book_key, relpath, catalog_version=None):  # endpoint: <endpoint_prefix>_raw
        require_content_feature(feature)
        book = source.get_book(book_key)
        if not book:
            abort(404)
        base = source.book_folder(book).resolve()
        if catalog_version:
            try:
                historical = historic_catalog(catalog_version)
                folder = 'stream_library' if prefix == 'liushi' else 'static_library'
                base = (historical.root / folder / str(book.get('folder') or book_key)).resolve()
                base.relative_to(historical.root / folder)
            except (OSError, ValueError, KeyError):
                abort(404)
        elif _CATALOG:
            # A path prefix, unlike a query parameter, survives relative HTML links.
            version = _CATALOG.version
            referrer = urlsplit(request.referrer or '')
            match = re.match(r'^/' + re.escape(prefix) + r'/v/([A-Za-z0-9._-]+)/raw/', referrer.path)
            if referrer.netloc == request.host and match:
                # A few mirrored documents contain absolute legacy raw URLs.
                # Keep those links on the referring page's accepted snapshot too.
                try:
                    version = historic_catalog(match.group(1)).version
                except (OSError, ValueError, KeyError):
                    abort(404)
            response = redirect(url_for(raw_ep + '_versioned', catalog_version=version,
                                        book_key=book_key, relpath=relpath), code=302)
            response.headers['Cache-Control'] = 'private, no-cache'
            return response
        target = (base / relpath).resolve()
        try:
            target.relative_to(base)  # 防 ../ 路径穿越
        except ValueError:
            abort(404)
        if not target.is_file():
            abort(404)
        # 纯文本/图片直发，无渲染、无缓存目录膨胀。
        response = send_file(str(target))
        # Preserve ETag/conditional loading while forbidding shared caches from
        # reusing reader content across permissions or sessions.
        response.headers["Cache-Control"] = "private, no-cache"
        return response

    app.add_url_rule(f"/{prefix}", endpoint=home_ep, view_func=home)
    app.add_url_rule(f"/{prefix}/<book_key>", endpoint=reader_ep, view_func=reader)
    app.add_url_rule(f"/{prefix}/raw/<book_key>/<path:relpath>", endpoint=raw_ep, view_func=raw)
    app.add_url_rule(f"/{prefix}/v/<catalog_version>/raw/<book_key>/<path:relpath>",
                     endpoint=raw_ep + '_versioned', view_func=raw)


def register_stream_reading(
    app, *, require_content_feature, ai_web_allowed=None, notes_access=None, record_book_read=None
) -> None:
    """流式阅读 /liushi：《马克思恩格斯文集》中文网页适配版。复用上面的整套机制，
    仅换数据源/前缀/权限键/首页模板，并把翻译入口置空（中文书不做对照/划词翻译）。"""
    register_static_library(
        app,
        require_content_feature=require_content_feature,
        ai_web_allowed=ai_web_allowed,
        notes_access=notes_access,
        record_book_read=record_book_read,
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
