from __future__ import annotations

import json
import html
import ipaddress
import os
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import fitz
from flask import (
    Flask,
    Response,
    abort,
    copy_current_request_context,
    flash,
    g,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from admin_store import (
    activate_desktop_device,
    create_desktop_device,
    create_release,
    delete_setting,
    get_device_by_token,
    get_setting,
    init_admin_store_db,
    is_device_authorized,
    latest_release,
    list_desktop_devices,
    list_releases,
    set_setting,
    touch_device_sync,
    update_desktop_device,
    update_release_status,
)
from ai import (
    CONFIG_PATH as AI_CONFIG_PATH,
    AI_OVERRIDE_PATH,
    DEFAULT_MODEL as AI_DEFAULT_MODEL,
    DEFAULT_PROVIDER as AI_DEFAULT_PROVIDER,
    ZHIPU_DEFAULT_MODEL,
    ZHIPU_DEFAULT_BASE_URL,
    ZHIPU_SEARCH_ENGINES,
    AIServiceError,
    ZAIClient,
    load_ai_config,
    research_ai_http_context,
    reset_ai_overrides,
    save_ai_overrides,
)
from build_index import normalize
from membership import (
    clear_pending_orders,
    consume_account_email_token,
    create_pending_order,
    create_donation_order,
    get_or_create_donation_guest_id,
    DONATION_MIN_CENTS,
    DONATION_MAX_CENTS,
    create_account_email_token,
    create_user,
    deactivate_user_account,
    expire_pending_orders,
    prune_duplicate_pending_orders_for_user,
    create_manual_subscription,
    bulk_grant_membership,
    bulk_grant_coverage,
    get_order_by_no,
    get_membership_snapshot,
    get_account_email_token,
    get_admin_dashboard_first_day,
    get_admin_dashboard_metrics,
    get_ai_token_usage,
    get_ai_token_usage_range,
    count_ai_usage_requests,
    count_registered_users,
    capture_user_ip_if_missing,
    get_user_ip_counts,
    get_ai_credit_balances,
    get_ai_credit_balance,
    consume_ai_credit,
    get_user_zhipu_limit,
    get_plan,
    get_user_ai_limit,
    get_user_by_email,
    get_user_by_id,
    init_membership_db,
    list_active_plans,
    list_ai_usage_for_user,
    list_reader_access_events,
    list_payment_events,
    list_plans,
    list_reader_anomaly_visitors,
    list_reader_ip_pool_burst_candidates,
    list_recent_orders,
    list_recent_subscriptions,
    list_orders_for_user,
    list_subscriptions_for_user,
    list_users,
    load_session_secret,
    normalize_email,
    china_day_text,
    get_online_presence_series,
    prune_online_presence,
    prune_reader_access_events,
    record_ai_usage,
    record_online_presence,
    record_payment_event,
    record_reader_access_event,
    record_site_activity,
    mark_order_paid,
    update_user_password,
    update_user_account,
    update_last_login,
    utc_now_text,
    upsert_plan,
    verify_account_email_code,
)
import geoip
from desktop_sync import (
    CACHE_PATH as DESKTOP_SYNC_CACHE_PATH,
    activate as activate_desktop_sync,
    cached_ai_public_runtime,
    load_cache as load_desktop_sync_cache,
    proxy_ai as proxy_desktop_ai,
    save_cache as save_desktop_sync_cache,
    sync as sync_desktop_runtime,
)
from dictionary_store import (
    dictionary_available,
    dictionary_entry,
    dictionary_groups,
    dictionary_stats,
    dictionary_suggest,
)
from feature_access import (
    AUDIENCE_ACCESS_LABELS,
    FEATURE_ACCESS_GROUPS,
    FEATURE_ACCESS_HINTS,
    FEATURE_ACCESS_KEYS,
    FEATURE_ACCESS_LABELS,
    audience_feature_access_rows as build_audience_feature_access_rows,
    feature_access_rows as build_feature_access_rows,
    feature_allowed_by_policy as shared_feature_allowed_by_policy,
    load_access_policy as load_shared_access_policy,
    membership_plan_code_for_user,
    plan_feature_access_rows as build_plan_feature_access_rows,
)
from feedback import (
    DB_PATH as FEEDBACK_DB_PATH,
    add_admin_reply as add_feedback_admin_reply,
    add_user_message as add_feedback_user_message,
    count_open_page_error_reports,
    create_page_error_report,
    get_attachment as get_feedback_attachment,
    get_user_thread as get_feedback_user_thread,
    init_feedback_db,
    list_feedback_threads,
    list_page_error_reports,
    set_page_error_report_status,
    update_message_email_status as update_feedback_message_email_status,
)
from notes import (
    count_notes as count_user_notes,
    create_note as create_user_note,
    delete_note as delete_user_note,
    get_note as get_user_note,
    init_notes_db,
    list_note_books as list_user_note_books,
    list_notes as list_user_notes,
    update_note as update_user_note,
)
from journal_alerts import (
    _markdown_to_html as journal_markdown_to_html,
    add_journal_source,
    approve_all_pending_articles as approve_all_pending_journal_articles,
    archive_previous_batches,
    backfill_default_journal_sources,
    batch_articles,
    collect_batch,
    confirm_subscription,
    count_deferred_articles as count_deferred_journal_articles,
    create_or_update_subscription,
    current_batch,
    deliver_ready_articles as deliver_ready_journal_articles,
    generate_batch_review,
    get_batch,
    get_public_article as get_public_journal_article,
    _ncpssd_opener as journal_ncpssd_opener,
    latest_public_batch,
    ncpssd_detail_probe,
    journal_abstract_diag,
    journal_abstract_coverage,
    init_journal_alerts_db,
    journal_source_catalog,
    list_articles_by_status as list_journal_articles_by_status,
    list_journal_sources,
    list_recent_articles as list_recent_journal_articles,
    list_recent_batches,
    list_public_batches,
    list_recent_delivery_logs as list_recent_journal_delivery_logs,
    list_recent_runs as list_recent_journal_runs,
    list_recent_subscriptions as list_recent_journal_subscriptions,
    list_subscriptions_for_user as list_journal_subscriptions_for_user,
    load_alert_settings as load_journal_alert_settings,
    load_smtp_config,
    normalize_alert_settings,
    public_base_url as journal_alert_public_base_url,
    purge_archived_articles,
    relay_status as journal_relay_status,
    resolve_recipients as resolve_journal_recipients,
    run_journal_alerts_once,
    send_batch as send_journal_batch,
    send_confirmation_email,
    send_email,
    unsubscribe_by_id as journal_unsubscribe_by_id,
    unsubscribe_by_token as journal_unsubscribe_by_token,
    update_article_review_status as update_journal_article_review_status,
    update_batch_review,
    update_journal_source,
)
from broadcast_email import (
    BROADCAST_SCOPES,
    count_recipients as count_broadcast_recipients,
    create_campaign as create_broadcast_campaign,
    finish_campaign as finish_broadcast_campaign,
    init_broadcast_db,
    list_recent_campaigns as list_recent_broadcast_campaigns,
    render_broadcast_email,
    render_broadcast_html,
    resolve_recipients as resolve_broadcast_recipients,
    send_campaign as send_broadcast_campaign,
)
from runtime_env import (
    APP_NAME,
    APP_TOKEN_HEADER,
    APP_VERSION,
    APPDATA_DIR,
    RUNTIME_ROOT,
    collect_runtime_status,
    compute_sha256,
    configure_logging,
    load_allowed_source_files,
    load_activation_status,
    load_deployment_settings,
)
from book_config import BookConfig, load_book_configs
from volume_presentation import TRUSTED_TOC_DATE_BOOKS, volume_presentation
from static_library_web import (
    register_static_library,
    static_library_has_content,
    register_stream_reading,
    stream_reading_has_content,
    load_books as load_static_library_books,
)
import wenku_translate
from search import CHAPTER_HITS_PAGE_SIZE, Corpus, DEFAULT_CITATION_TEMPLATES
from site_content import (
    SITE_TEXT_OVERRIDES_PATH,
    AutoSiteTextExtension,
    SiteTextDefinition,
    get_site_text_map,
    load_site_text_overrides,
    list_site_text_groups,
    list_site_text_groups_from_map,
    prune_stale_overrides,
    register_site_text_definitions,
    render_auto_site_text,
    render_site_text,
    reset_site_text_overrides,
    save_site_text_overrides,
    site_text_coverage_report,
    stale_override_keys,
    update_site_text_overrides,
)
from zpay import ZPayClient, load_zpay_config


DIRECT_RESULTS_THRESHOLD = 8
GROUPS_PER_PAGE = 20
SHORT_QUERY_CHAPTER_MAX_LEN = 4
ASSOC_RERANK_TOP = 12  # 联想检索仅对权重最高的前若干候选做 AI 标注/解释（候选多时控成本）
ASSOC_RERANK_TOP_RESEARCH = 20  # 研究意图用更大的重排池：覆盖论题不同侧面并给出分组理由
RESEARCH_REVIEW_SOURCES = 24     # 研究综述喂给 AI 的真实原文源条数：支撑 20-24 条引用；相关度由 _select_research_review_hits 的 floor 守门，命中不足则少给、绝不堆砌弱相关
REQUEST_TOKEN = secrets.token_urlsafe(24)
LOGGER = configure_logging()
DEPLOYMENT = load_deployment_settings()
MEMBERSHIP_DB_PATH = init_membership_db()
ADMIN_STORE_DB_PATH = init_admin_store_db()
JOURNAL_ALERTS_DB_PATH = init_journal_alerts_db()
init_broadcast_db()
FEEDBACK_DB_PATH = init_feedback_db()
NOTES_DB_PATH = init_notes_db()
FEEDBACK_ADMIN_EMAIL = "popyunlong@163.com"
FEEDBACK_IMAGE_DIR = APPDATA_DIR / "feedback_images"
FEEDBACK_IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp",
}
FEEDBACK_IMAGE_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
}
FEEDBACK_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 单张留言图片上限 5MB
FEEDBACK_MAX_IMAGES_PER_MESSAGE = 6


def _maybe_relaunch_with_pythonw() -> None:
    if not DEPLOYMENT.is_desktop:
        return
    if os.name != "nt" or getattr(sys, "frozen", False):
        return
    if os.environ.get("APP_NO_PYTHONW") == "1":
        return
    if os.environ.get("APP_PYTHONW_LAUNCHED") == "1":
        return
    if Path(sys.executable).name.lower() != "python.exe":
        return

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        return

    env = os.environ.copy()
    env["APP_PYTHONW_LAUNCHED"] = "1"
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.Popen(
        [str(pythonw), str(Path(__file__).resolve())],
        cwd=str(Path(__file__).resolve().parent),
        env=env,
        close_fds=True,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    raise SystemExit


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def create_app() -> Flask:
    flask_app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    flask_app.wsgi_app = ProxyFix(
        flask_app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
        x_prefix=1,
    )
    flask_app.config.update(
        SECRET_KEY=load_session_secret(),
        APP_MODE=DEPLOYMENT.app_mode,
        BIND_HOST=DEPLOYMENT.bind_host,
        PORT=DEPLOYMENT.port,
        PUBLIC_BASE_URL=DEPLOYMENT.public_base_url,
        PREFERRED_URL_SCHEME=DEPLOYMENT.public_scheme,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(DEPLOYMENT.public_base_url.startswith("https://")),
        # 会话绝对过期 14 天 + 每次请求滑动续期：被盗 cookie 不再无限期有效。
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        SESSION_REFRESH_EACH_REQUEST=True,
        # 全局请求体上限（防超大 POST 内存放大）；上传类端点在 before_request 里按需放宽。
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    )
    # 模板编译前把页面里的中文静态文字（按钮/标题/正文）自动接入站点文案系统，
    # 让后台「站点文案」可直接编辑控制台之外的程序性文字。注入器内部已做异常回退。
    flask_app.jinja_env.add_extension(AutoSiteTextExtension)
    return flask_app


app = create_app()


def _server_ai_settings_payload() -> dict:
    if not DEPLOYMENT.is_server:
        return {}
    payload = get_setting("ai", {})
    return payload if isinstance(payload, dict) else {}


BASE_RUNTIME = collect_runtime_status()
AI_CONFIG = load_ai_config(extra_payload=_server_ai_settings_payload())
AI_CLIENT = ZAIClient(AI_CONFIG)
_AI_CONFIG_SNAPSHOT: tuple[int | None, int | None] | None = None
PAYMENT_CONFIG = load_zpay_config(DEPLOYMENT.public_base_url)
PAYMENT_CLIENT = ZPayClient(PAYMENT_CONFIG)
ALLOWED_SOURCE_FILES = load_allowed_source_files()
PAGE_IMAGE_CACHE_DIR = APPDATA_DIR / "page_images"
# 期刊文献 PDF 的按需镜像缓存（点击下载时首次拉取并落盘，之后本地直发）。
JOURNAL_PDF_CACHE_DIR = APPDATA_DIR / "journal_pdfs"
corpus = Corpus.load_default() if BASE_RUNTIME.can_search else None
if corpus is not None:
    # 后台预热篇章分段缓存：避免首个短词海量检索因一次性构建分段而出现卡顿。
    def _warm_search_caches() -> None:
        try:
            corpus.warm_chapter_segments()
        except Exception:  # 预热失败不应影响服务启动，按需懒构建即可。
            pass

    threading.Thread(
        target=_warm_search_caches, name="warm-chapter-segments", daemon=True
    ).start()
BOOK_CONFIGS: list[BookConfig] = corpus.book_configs if corpus else load_book_configs()
BOOK_CONFIG_BY_KEY: dict[str, BookConfig] = {book.key: book for book in BOOK_CONFIGS}
book_stats = [
    {
        "key": book.key,
        "title": book.title,
        "short_title": book.short_title,
        "count": len(corpus.books.get(book.key, [])) if corpus else 0,
        "sort_order": book.sort_order,
    }
    for book in BOOK_CONFIGS
]
n_wenji = len(corpus.books.get("文集", [])) if corpus else 0
n_quanji = len(corpus.books.get("全集", [])) if corpus else 0

# 首页顶部「卷数 pill」原本是模板里按真实书目动态渲染的（{{ short_title }} {{ count }} 卷），
# 静态文案扫描发现不了，因此后台「内容运营」一直无法编辑。这里给每个书目注册一个可选
# 覆盖 key（默认留空＝继续自动显示实时卷数，填写后即整段替换该 pill 文字），让它们和其他
# 文案一样能在后台编辑。
# 首页顶部「卷数 pill」固定文案（书目 key → 展示文字，含版次年份）。默认即显示这些固定
# 文字；后台「站点文案·首页」仍可逐条覆盖，覆盖留空则回退到这里的固定文案，固定文案也留空
# 才回退到「短标题 + 实时卷数 + 卷」的自动格式。这样即使后台被清空，首页依然显示固定文案。
_FIXED_PILL_TEXT = {
    "文集": "《马克思恩格斯文集》10卷2009年版",
    "全集": "《马克思恩格斯全集》50卷中文第一版",
    "全集二版": "《马克思恩格斯全集》中文第二版",
    "马恩选集": "《马克思恩格斯选集》4卷2012年版",
    "列宁全集": "《列宁全集》60卷中文第二版",
    "毛泽东选集": "《毛泽东选集》4卷1991年版",
    "毛泽东文集": "《毛泽东文集》8卷1993年版",
    "邓小平文选": "《邓小平文选》3卷93/94年版",
    "江泽民文选": "《江泽民文选》3卷2006年版",
    "胡锦涛文选": "《胡锦涛文选》3卷2016年版",
    # 注意：界面文案统一用《治国理政》，全名仅出现在引文（citation_title），勿改。
    "治国理政": "《治国理政》5卷",
}
_book_pill_definitions = []
for _pill_index, _book_item in enumerate(book_stats, start=1):
    _pill_key = f"index.stat_book_{_pill_index}"
    _book_item["text_key"] = _pill_key
    _fixed_pill = _FIXED_PILL_TEXT.get(_book_item["key"], "")
    _book_item["fixed_pill"] = _fixed_pill
    _book_pill_definitions.append(
        SiteTextDefinition(
            _pill_key,
            "首页",
            f"首页顶部卷数 pill：{_book_item['short_title']}（留空＝显示固定版次文案，再留空＝自动卷数）",
            _fixed_pill,
            multiline=False,
        )
    )
register_site_text_definitions(_book_pill_definitions)

if BASE_RUNTIME.can_search:
    LOGGER.info(
        "Loaded corpus: %s",
        " ".join(f"{item['key']}={item['count']}" for item in book_stats),
    )
else:
    LOGGER.warning("Search disabled on startup: %s", " | ".join(BASE_RUNTIME.problems))

if AI_CONFIG.enabled:
    LOGGER.info("AI enabled with provider=%s model=%s", AI_CONFIG.provider, AI_CONFIG.model)
else:
    LOGGER.warning("AI disabled: %s", " | ".join(AI_CONFIG.problems) or "missing configuration")

if PAYMENT_CONFIG.enabled:
    LOGGER.info("ZPay page pay enabled with gateway=%s", PAYMENT_CONFIG.submit_url)
else:
    LOGGER.warning("ZPay disabled: %s", " | ".join(PAYMENT_CONFIG.problems) or "missing configuration")


_last_ping: list[float] = [time.time()]
_GRACE = 45
_TIMEOUT = 60
_CHECK_INTERVAL = 5
CONTROL_USER_LIMIT = 60
ONLINE_WINDOW_SECONDS = 5 * 60
DASHBOARD_HIGH_TOKEN_THRESHOLD = 50000
DASHBOARD_TOKEN_LIMIT_RATIO = 0.8
DASHBOARD_DEFAULT_HISTORY_DAYS = 30
DASHBOARD_MAX_HISTORY_DAYS = 366
READER_AUDIT_KEEP_DAYS = 30
READER_AUDIT_PRUNE_INTERVAL_SECONDS = 60 * 60
# 在线变化图按 15 分钟时槽留存，保留 48 小时足够覆盖 24 小时窗口与跨时区显示。
ONLINE_PRESENCE_KEEP_HOURS = 48
ONLINE_PRESENCE_PRUNE_INTERVAL_SECONDS = 60 * 60
# 异步审计写：阅读热路径上的三类「尽力而为」记账(reader_access_events / site_activity /
# online_presence)原本每请求同步写 SQLite。被拒匿名洪峰会让 8 个工作线程同时抢单一写锁、
# 队列堆高(2026-06-24 攻击实测队列峰值 11、2 分钟写 593 行审计)。改为投递到单个后台写线程
# 串行落库，请求线程零写锁等待；队列触顶即丢弃并限频告警(best-effort 审计，宁丢记录不拖垮请求)。
# 测试态(app.testing)仍同步写以保证「请求后立即查库」的断言成立。
ASYNC_AUDIT_QUEUE_MAX = 8000
ASYNC_AUDIT_DROP_WARN_INTERVAL_SECONDS = 60
READER_ENDPOINTS = {
    "reader",
    "library",
    "dictionary",
    "dictionary_entry_page",
    "pdf_viewer",
    "serve_pdf",
    "page_image",
    "api_dictionary_suggest",
    "api_library_toc_suggest",
    "api_library_volume_toc",
}
CSRF_EXEMPT_ENDPOINTS = {
    "zpay_notify",
    "zpay_return",
    "api_ping",
    "api_shutdown",
    "api_desktop_activate",
    "api_desktop_sync",
    "api_desktop_ai_search_chat",
    "api_desktop_ai_pdf_chat",
}
RATE_LIMITS = {
    "register_ip": (300, 3600),
    "register_code_ip": (1000, 3600),
    "register_code_email": (60, 3600),
    "login_ip": (30, 900),
    "login_email": (12, 900),
    "password_reset_ip": (5, 3600),
    "password_reset_email": (3, 3600),
    "search_guest": (10, 60),
    "search_user": (60, 60),
    "ai_user": (30, 60),
    "feedback_user": (10, 600),
    "journal_user": (5, 3600),
    # 书页图像防爬（P3）：刻意放宽，正常翻页/预取（每翻一页约 1~3 次请求）远低于此阈值，
    # 仅拦截整本批量抓取。按登录用户 / 浏览器会话计数（NAT 友好），触发时前端弹窗提示并自动恢复。
    "page_image": (600, 60),
    # 无 cookie 的书页图请求按真实 IP 的严格限速：真实浏览器加载阅读页 HTML 时就已拿到会话
    # cookie，后续取书页图必然带 cookie；不带 cookie 还在批量取图的，几乎必为「每请求换一个
    # cookie」以绕过单会话限速的脚本抓取，故对其按真实 IP 单独收紧（弥补 cookie 维度被绕过）。
    "page_image_nocookie_ip": (60, 60),
    # 按真实客户端 IP 的兜底限速(#2 真实 IP 透传后启用)。NAT 宽容、阈值高，
    # 正常读者/校园 NAT 远不及此，只拦单 IP 的高频批量抓取。可经 env 覆盖。
    "reader_view_ip": (200, 60),
    "reader_pageimg_ip": (1500, 60),
    # 期刊文献 PDF 下载（每次可能触发远端镜像，较重）：单 IP 收紧。
    "reader_journalpdf_ip": (40, 60),
}
RESEARCH_WEEKLY_QUOTA_SETTING_KEY = "research_weekly_quota"
RESEARCH_WEEKLY_QUOTA_DEFAULTS = {
    "registered": 5,
    "monthly": 15,
    "quarterly": 20,
    "yearly": 25,
}
RESEARCH_WEEKLY_QUOTA_LABELS = {
    "registered": "登录用户",
    "monthly": "月度会员",
    "quarterly": "季度会员",
    "yearly": "年度会员",
}
RESEARCH_QUOTA_FEATURE = "research_review"
# 「研究级检索次数限制」总开关（默认关）：关＝研究综述不另设次数上限、纯按 AI token 额度计量；
# 开＝沿用上面分档的每周次数上限。无论开关，研究综述都照常吃 token 额度（生成前过 token 闸、生成后按
# 完整 token 记账）；此开关只决定是否额外叠加「按次数」这道闸。¥3 研究资源包已下架，故默认纯 token 计量。
RESEARCH_COUNT_LIMIT_ENABLED_SETTING_KEY = "research_count_limit_enabled"
# 「研究级检索次数」重置标记：后台可把指定用户/某会员档/全体注册用户的本周已用次数清零。
# 非破坏式——只记录一个 UTC 时间点，计数时改为「只统计该点之后的研究记录」，既不删 ai_usage
# 审计、也不退还耦合的每日 token 额度；周窗口推进后旧标记自然失效。
RESEARCH_QUOTA_RESETS_SETTING_KEY = "research_quota_resets"
RESEARCH_QUOTA_RESET_SCOPES = ("all", "registered", "monthly", "quarterly", "yearly", "user")
# DeepSeek 主通道「每日 AI token 额度」分档默认值（这里是每日参考；硬上限＝本周＝每日×AI_TOKEN_WEEKLY_FACTOR）。
# 取舍依据：DeepSeek 价格（约 ¥2/1M 输入、¥8/1M 输出，混合约 ¥4-5/1M）与会员定价（月 ¥9 / 季 ¥24 / 年 ¥88）
# 并兼顾 GLM 智谱日额 30k/60k/100k。给的是「封顶值」防滥用，真实用量通常远低于此；会员体验留足头寸。
# 解析优先级：用户级 override > 套餐级 daily_ai_token_limit（套餐管理可单设）> 本分档默认 > 内置兜底。
# 管理员/桌面不受限。值＝每日 token 参考；0＝该群体不开放付费主通道 AI（仍可用资源包次数）。可在后台改。
AI_TOKEN_DAILY_SETTING_KEY = "ai_token_daily_limits"
AI_TOKEN_DAILY_DEFAULTS = {
    # 马克思形象（吉祥物）AI 已独立成「无限量基础服务」(走 deepseek-v4-flash)，不计入此额度，
    # 故 guest 在此处＝0：游客除吉祥物外不调用付费主通道 AI（随心问/导学本就需登录+权限）。
    "guest": 0,
    "registered": 10000,
    # 会员档（定价 ¥9/¥24/¥88 + 兼顾 GLM 智谱日额 30k/60k/100k 统筹）：DeepSeek 主通道更便宜，
    # 给约 1.1–1.5× 于 GLM 的日额。研究综述按「完整 token（含注入原文）」计入本额度，故封顶须容下
    # 研究周次数(5/15/20/25)摊到每日的量 + 随心问/导学；这是防滥用封顶，真实用量通常远低于此。
    "monthly": 40000,
    "quarterly": 70000,
    "yearly": 110000,
}
# 马克思形象（吉祥物）专用模型：固定走更轻量/更省的 deepseek-v4-flash，且不计日额度（基础服务）。
# 模型名可经环境变量覆盖，以适配线上中转网关的实际模型命名。
MASCOT_AI_MODEL = os.environ.get("MASCOT_AI_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
AI_TOKEN_DAILY_LABELS = {
    "guest": "访客（未登录）",
    "registered": "登录用户（未开通会员）",
    "monthly": "月度会员",
    "quarterly": "季度会员",
    "yearly": "年度会员",
}
# 「无限量基础服务」的 feature：这些调用照常写入 ai_usage（后台用量总览/审计仍统计全部），
# 但不占用用户的 AI 额度池——否则「不限量」只是不被拦，实际仍在吃随心问/研究/导学共用的周额度。
# 目前只有马克思形象（吉祥物，固定走 deepseek-v4-flash，见 MASCOT_AI_MODEL）。
AI_QUOTA_EXEMPT_FEATURES = ("mascot",)
# 余额提示阈值：剩余占比 ≤ 此值时前端给出「即将耗尽」预警。
AI_TOKEN_LOW_RATIO = 0.15
# 弹性额度：每日额度是「软上限/参考配速」，真正的硬上限是「本周＝每日×此系数」。
# 这样某天集中做多次研究型检索（单次需较多 token，否则会被截断）也不会被每日额度卡死，
# 只要本周累计未超「每日×7」即可灵活借用；周成本与「按每日封顶天天用满」一致，不增成本。
AI_TOKEN_WEEKLY_FACTOR = 7
# 「本周 AI 额度」重置标记（与研究次数重置同构、同样非破坏式）：后台可把指定用户 / 某档位 /
# 全体用户的「本周已用 token」归零、恢复满额。只记录一个 UTC 时间点，统计时改为「只算该时刻
# 之后的用量」——不删 ai_usage 审计明细、不改分档额度配置；周窗口推进后旧标记自然失效。
AI_TOKEN_QUOTA_RESETS_SETTING_KEY = "ai_token_quota_resets"
# 会员档位（「全部会员用户」＝这三档一起重置）与全部注册账号（「全部注册用户」＝含会员、不含访客）。
AI_TOKEN_MEMBER_BUCKETS = ("monthly", "quarterly", "yearly")
AI_TOKEN_REGISTERED_BUCKETS = ("registered", *AI_TOKEN_MEMBER_BUCKETS)
# 重置范围：all＝全体（含未登录访客）；registered_all＝全部注册用户（含会员）；members＝全部会员；
# guest/registered/monthly/quarterly/yearly＝单一档位；user＝按邮箱指定（可一次填多个）。
AI_TOKEN_QUOTA_RESET_SCOPES = (
    "all", "registered_all", "members", "guest", "registered", "monthly", "quarterly", "yearly", "user",
)
# 「按档位展开成多个标记」的范围：一次写多条 tier 标记，用户日后升降档也照样落在已重置的档里。
AI_TOKEN_QUOTA_RESET_SCOPE_GROUPS = {
    "registered_all": AI_TOKEN_REGISTERED_BUCKETS,
    "members": AI_TOKEN_MEMBER_BUCKETS,
}
AI_TOKEN_QUOTA_RESET_SCOPE_LABELS = {
    "all": "全体用户（含未登录访客）",
    "registered_all": "全部注册用户（含会员）",
    "members": "全部会员用户",
    "user": "指定用户",
    **AI_TOKEN_DAILY_LABELS,
}
# 极端真实 IP 扒站者的保守自动封禁阈值(双高：日总量 且 单分钟峰值)。仅封公网 IP actor，
# 永不封登录会员/内网/监控；可经设置 reader_auto_ban 或 env 调整、DISABLE_READER_AUTO_BAN 关闭。
READER_AUTO_BAN_DAILY_MIN = 2000
READER_AUTO_BAN_MINUTE_MIN = 90
READER_AUTO_BAN_POOL_IP_MIN = 80
READER_AUTO_BAN_POOL_REQUEST_MIN = 120
READER_AUTO_BAN_POOL_PATH_MIN = 60
READER_AUTO_BAN_POOL_WINDOW_MINUTES = 15
READER_AUTO_BAN_POOL_LIMIT = 1000
READER_AUTO_BAN_INTERVAL_SECONDS = 120
LOGIN_CAPTCHA_THRESHOLD = 5
LOGIN_LOCK_THRESHOLD = 10
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
ORDER_EXPIRY_SWEEP_INTERVAL_SECONDS = 60 * 60
DISPLAY_NAME_MAX_LEN = 40
RELEASE_UPLOAD_EXTENSIONS = {".exe", ".msi", ".zip", ".7z", ".tar", ".gz", ".tgz", ".dmg", ".pkg", ".patch"}
ADMIN_MODULES = {
    "overview": "总览",
    "content": "内容运营",
    "ai": "智能服务",
    "members": "会员与权限",
    "journal": "期刊订阅",
}
_rate_buckets: dict[str, list[float]] = {}
_rate_buckets_lock = threading.Lock()
_last_rate_prune: list[float] = [0.0]
_login_failures: dict[str, list[float]] = {}
# 与 _rate_buckets 对称：8 线程下登录失败桶的读-改-写同样要加锁（消除丢更新弱化锁定），并周期清理
# 过期键（撞库/枚举会按不同邮箱无限攒键 = 内存增长向量）。
_login_failures_lock = threading.Lock()
_last_login_failures_prune: list[float] = [0.0]
_last_order_expiry_sweep: list[float] = [0.0]
_last_reader_audit_prune: list[float] = [0.0]
_last_online_presence_prune: list[float] = [0.0]
_last_reader_auto_ban: list[float] = [0.0]
_async_audit_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=ASYNC_AUDIT_QUEUE_MAX)
_async_audit_dropped: list[int] = [0]
_async_audit_last_drop_warn: list[float] = [0.0]
_async_audit_writer_started = threading.Event()
_async_audit_writer_lock = threading.Lock()
ADMIN_SECTION_MODULES = {
    "overview": "overview",
    "copy": "content",
    "broadcast": "content",
    "journal-alerts": "journal",
    "future-modules": "content",
    "ai": "ai",
    "plans": "members",
    "plan-access": "members",
    "payments": "members",
    "members": "members",
    "users": "members",
    "devices": "overview",
    "releases": "overview",
}


def _book_config(book: str) -> BookConfig:
    return BOOK_CONFIG_BY_KEY.get(book) or BookConfig(
        key=book,
        title=f"《{book}》",
        short_title=f"《{book}》",
        citation_title=book,
        folder=f"pdfs/{book}",
        sort_order=9999,
        publisher="人民出版社",
        place="北京",
        tag_class="book-other",
    )


def _book_sort_order(book: str) -> int:
    return _book_config(book).sort_order


_COLLECTION_LABELS = {
    "classical_marxism": "马克思主义经典著作",
    "marxism_china": "马克思主义中国化时代化经典著作",
    "xi_thought": "习近平新时代中国特色社会主义思想",
    "party_state_documents": "党和国家重要文献",
    # 年谱是编年体生平记录，与「著作」体裁不同，故单列一组而非塞进领袖著作组。
    "leader_chronicles": "领袖年谱",
    "western_marxism": "西马文库",
}

_COLLECTION_DESCRIPTIONS = {
    "classical_marxism": "马克思、恩格斯、列宁经典著作 · 各套著作独立编目，可按卷册与目录阅读、检索原文并生成规范引文",
    "marxism_china": "毛泽东、邓小平、江泽民、胡锦涛重要著作 · 各部著作独立编目，可按目录阅读、检索原文并生成规范引文",
    "xi_thought": "习近平新时代中国特色社会主义思想权威著作与学习读物 · 各部著作独立编目，可按目录阅读、检索原文并生成规范引文",
    "party_state_documents": "历次党代会报告、全会公报、重要文献选编与五年规划纲要 · 各部文献独立编目，可按目录阅读、检索原文并生成规范引文",
    "leader_chronicles": "马克思主义者的编年体生平记录 · 可按年份查考某日言行并检索原文",
    "western_marxism": "西方马克思主义经典著作 · 七部著作独立编目，可按目录阅读、检索原文并生成规范引文",
}


def _book_payload(book: str) -> dict:
    cfg = _book_config(book)
    return {
        "book_title": cfg.title,
        "book_short_title": cfg.short_title,
        "citation_title": cfg.citation_title,
        "book_sort_order": cfg.sort_order,
        "tag_class": cfg.tag_class,
        "collection": cfg.collection,
        "collection_label": _COLLECTION_LABELS.get(cfg.collection, cfg.collection),
        "single_volume": cfg.single_volume,
    }


def _reload_ai_runtime() -> None:
    global AI_CONFIG, AI_CLIENT, _AI_CONFIG_SNAPSHOT
    AI_CONFIG = load_ai_config(extra_payload=_server_ai_settings_payload())
    AI_CLIENT = ZAIClient(AI_CONFIG)
    _AI_CONFIG_SNAPSHOT = _ai_config_snapshot()
    if AI_CONFIG.enabled:
        LOGGER.info("AI reloaded with provider=%s model=%s", AI_CONFIG.provider, AI_CONFIG.model)
    else:
        LOGGER.warning("AI reloaded but disabled: %s", " | ".join(AI_CONFIG.problems) or "missing configuration")


def _file_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _ai_config_snapshot() -> tuple[int | None, int | None]:
    return (_file_mtime_ns(AI_CONFIG_PATH), _file_mtime_ns(AI_OVERRIDE_PATH))


def _refresh_ai_runtime_if_needed() -> None:
    if _ai_config_snapshot() != _AI_CONFIG_SNAPSHOT:
        _reload_ai_runtime()


_AI_CONFIG_SNAPSHOT = _ai_config_snapshot()


def _public_ai_runtime_payload(*, allow_details: bool | None = None) -> dict:
    _refresh_ai_runtime_if_needed()
    allowed = True
    if DEPLOYMENT.is_desktop:
        payload = cached_ai_public_runtime()
        enabled = bool(payload.get("enabled"))
        model = str(payload.get("model") or "")
        problems = list(payload.get("problems") or [])
    else:
        enabled = bool(AI_CONFIG.enabled)
        model = str(AI_CONFIG.model or "")
        problems = list(AI_CONFIG.problems)

    if allow_details is None:
        allow_details = bool(_feature_is_available("ai") and _feature_effective_for_user("ai"))
    if not allow_details:
        allowed = False
        return {
            "enabled": False,
            "available": bool(enabled),
            "allowed": False,
            "model": "",
            "problems": [],
            "status": "locked",
        }
    return {
        "enabled": enabled,
        "available": bool(enabled),
        "allowed": allowed,
        "model": model if enabled else "",
        "problems": problems[:1],
        "status": "enabled" if enabled else "unconfigured",
        # 智谱联网通道：仅当服务端配置了智谱 Key 且当前用户具有 ai_web 权限时，前端才显示模型切换。
        "zhipu_enabled": bool(not DEPLOYMENT.is_desktop and AI_CONFIG.zhipu_enabled),
        "zhipu_model": str(AI_CONFIG.zhipu_model or "") if not DEPLOYMENT.is_desktop else "",
    }


def _is_local_console_request() -> bool:
    if not DEPLOYMENT.is_desktop:
        return False
    remote = (request.remote_addr or "").strip()
    return remote in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _require_local_console() -> None:
    if not _is_local_console_request():
        abort(403, description="本地控制台仅允许从当前机器访问。")


def _research_count_limit_enabled() -> bool:
    """研究级检索「次数限制」总开关（默认关）。关时研究综述不设次数上限、纯按 AI token 额度计量。"""
    return bool(get_setting(RESEARCH_COUNT_LIMIT_ENABLED_SETTING_KEY, False))


def _research_weekly_quota_settings() -> dict[str, int]:
    raw = get_setting(RESEARCH_WEEKLY_QUOTA_SETTING_KEY, {})
    raw = raw if isinstance(raw, dict) else {}
    settings: dict[str, int] = {}
    for key, default in RESEARCH_WEEKLY_QUOTA_DEFAULTS.items():
        try:
            settings[key] = max(0, int(raw.get(key, default)))
        except (TypeError, ValueError):
            settings[key] = int(default)
    return settings


def _research_quota_week_window() -> dict[str, str]:
    tz = timezone(timedelta(hours=8))
    now = datetime.now(timezone.utc).astimezone(tz)
    start_date = now.date() - timedelta(days=now.weekday())
    end_date = start_date + timedelta(days=6)
    reset_date = start_date + timedelta(days=7)
    reset_at = datetime(reset_date.year, reset_date.month, reset_date.day, tzinfo=tz)
    return {
        "start_day": start_date.isoformat(),
        "end_day": end_date.isoformat(),
        "reset_at": reset_at.isoformat(timespec="seconds"),
    }


def _research_quota_bucket_for_user(user: dict | None) -> str:
    if not user:
        return "registered"
    membership = getattr(g, "membership", None)
    if membership is None:
        try:
            membership = get_membership_snapshot(int(user["id"]))
        except Exception:
            membership = None
    if not membership or not getattr(membership, "is_active_member", False):
        return "registered"
    code = str(getattr(membership, "plan_code", "") or "").strip().lower()
    if code in {"yearly", "annual", "year", "annually"} or "year" in code or "annual" in code:
        return "yearly"
    if code in {"quarterly", "quarter", "season"} or "quarter" in code or "season" in code:
        return "quarterly"
    if code in {"monthly", "month"} or "month" in code:
        return "monthly"
    plan = get_plan(code) if code else None
    try:
        months = int((plan or {}).get("interval_months") or 0)
    except (TypeError, ValueError):
        months = 0
    if months >= 12:
        return "yearly"
    if months >= 3:
        return "quarterly"
    if months >= 1:
        return "monthly"
    return "registered"


def _load_quota_resets(setting_key: str) -> dict:
    """读取「额度重置标记」：``{'all': ts, 'tiers': {bucket: ts}, 'users': {email: ts}}``。

    ts 为 UTC ISO（与 ``ai_usage.created_at`` 同格式，可直接字符串比较）。统计时只算
    created_at >= 适用标记 的记录，即把「本周已用」清零——不删审计明细、不改额度配置。
    研究级检索次数与本周 AI token 额度各存一份（setting_key 不同），互不影响。
    """
    raw = get_setting(setting_key, {})
    raw = raw if isinstance(raw, dict) else {}
    tiers = raw.get("tiers") if isinstance(raw.get("tiers"), dict) else {}
    users = raw.get("users") if isinstance(raw.get("users"), dict) else {}
    return {
        "all": str(raw.get("all") or ""),
        "tiers": {str(k): str(v) for k, v in tiers.items() if v},
        "users": {str(k): str(v) for k, v in users.items() if v},
    }


def _quota_reset_at_for(resets: dict, user: dict | None, bucket: str) -> str:
    """该用户当前生效的重置时间点＝全体/档位/个人三类标记中的最晚一个（无则空串）。"""
    candidates: list[str] = []
    if resets.get("all"):
        candidates.append(resets["all"])
    tier_at = (resets.get("tiers") or {}).get(bucket)
    if tier_at:
        candidates.append(tier_at)
    if user and user.get("email"):
        user_at = (resets.get("users") or {}).get(normalize_email(str(user.get("email") or "")))
        if user_at:
            candidates.append(user_at)
    return max(candidates) if candidates else ""


def _prune_quota_resets(resets: dict) -> None:
    """就地丢弃 14 天前的陈旧标记（周窗口早已使其失效，仅为防设置无限膨胀）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
    if resets.get("all") and str(resets["all"]) < cutoff:
        resets["all"] = ""
    resets["tiers"] = {k: v for k, v in (resets.get("tiers") or {}).items() if str(v) >= cutoff}
    resets["users"] = {k: v for k, v in (resets.get("users") or {}).items() if str(v) >= cutoff}


def _research_quota_resets() -> dict:
    """「研究级检索次数」重置标记（见 _load_quota_resets）。"""
    return _load_quota_resets(RESEARCH_QUOTA_RESETS_SETTING_KEY)


def _research_quota_effective_reset_at(user: dict | None, bucket: str) -> str:
    return _quota_reset_at_for(_research_quota_resets(), user, bucket)


def _ai_token_quota_resets() -> dict:
    """「本周 AI token 额度」重置标记（见 _load_quota_resets）。"""
    return _load_quota_resets(AI_TOKEN_QUOTA_RESETS_SETTING_KEY)


def _ai_token_quota_reset_at(user: dict | None, bucket: str) -> str:
    """该用户本周 AI 额度的生效重置时刻：统计已用 token 时只算此刻之后的记录。"""
    return _quota_reset_at_for(_ai_token_quota_resets(), user, bucket)


def _ai_token_quota_reset_status() -> list[dict]:
    """后台展示用：当前**本周仍起作用**的 AI 额度重置标记（早于本周起点的等于没做，不列）。"""
    resets = _ai_token_quota_resets()
    week = _research_quota_week_window()
    week_start_utc = (
        datetime.fromisoformat(f"{week['start_day']}T00:00:00+08:00")
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
    )
    rows: list[tuple[str, str]] = []
    if resets.get("all"):
        rows.append((AI_TOKEN_QUOTA_RESET_SCOPE_LABELS["all"], resets["all"]))
    for bucket, ts in (resets.get("tiers") or {}).items():
        rows.append((AI_TOKEN_QUOTA_RESET_SCOPE_LABELS.get(bucket, bucket), str(ts)))
    for email, ts in (resets.get("users") or {}).items():
        rows.append((email, str(ts)))
    active = [
        {"label": label, "at": _display_datetime(ts)}
        for label, ts in rows
        if ts and ts >= week_start_utc
    ]
    active.sort(key=lambda item: item["at"], reverse=True)
    return active


def _research_quota_payload(user: dict | None = None) -> dict:
    if user is None and has_request_context():
        user = getattr(g, "current_user", None)
    # 管理员豁免：研究型检索不限次数（与 AI token / 智谱子配额一致），便于线上验证与运营。
    if _is_admin_user(user):
        week = _research_quota_week_window()
        return {
            "bucket": "admin",
            "label": "管理员",
            "limit": None,
            "used": 0,
            "free_remaining": None,
            "pack_credits": 0,
            "remaining": None,
            "allowed": True,
            "unlimited": True,
            "start_day": week["start_day"],
            "end_day": week["end_day"],
            "reset_at": week["reset_at"],
            "message": "管理员研究型检索不限次数",
        }
    # 总开关关闭（默认）：研究综述不另设次数上限，纯按 AI token 额度计量（token 闸/记账照旧）。
    if not _research_count_limit_enabled():
        week = _research_quota_week_window()
        _bucket = _research_quota_bucket_for_user(user)
        return {
            "bucket": _bucket,
            "label": RESEARCH_WEEKLY_QUOTA_LABELS.get(_bucket, _bucket),
            "limit": None,
            "used": 0,
            "free_remaining": None,
            "pack_credits": 0,
            "remaining": None,
            "allowed": True,
            "unlimited": True,
            "start_day": week["start_day"],
            "end_day": week["end_day"],
            "reset_at": week["reset_at"],
            "message": "研究综述按 AI token 额度计量，不另设次数上限",
        }
    bucket = _research_quota_bucket_for_user(user)
    settings = _research_weekly_quota_settings()
    limit = int(settings.get(bucket, RESEARCH_WEEKLY_QUOTA_DEFAULTS[bucket]))
    week = _research_quota_week_window()
    user_id = int(user["id"]) if user and user.get("id") else None
    # 「重置」标记：只统计该时间点之后的研究记录，等效把本周已用次数清零（非破坏式）。
    reset_at = _research_quota_effective_reset_at(user, bucket)
    used = count_ai_usage_requests(
        user_id=user_id,
        session_key=_visitor_session_key() if has_request_context() else "",
        start_day=week["start_day"],
        end_day=week["end_day"],
        feature=RESEARCH_QUOTA_FEATURE,
        success_only=True,
        since_created_at=reset_at,
    )
    free_remaining = max(0, limit - used)
    # 资源包次数（永久有效、可叠加）：免费周额用完后接续使用。
    pack_credits = get_ai_credit_balance(user_id, "research") if user_id else 0
    remaining = free_remaining + pack_credits
    label = RESEARCH_WEEKLY_QUOTA_LABELS.get(bucket, bucket)
    if pack_credits > 0:
        message = f"{label}本周研究型检索剩余 {free_remaining}/{limit} 次（另有资源包 {pack_credits} 次）"
    else:
        message = f"{label}本周研究型检索剩余 {free_remaining}/{limit} 次"
    return {
        "bucket": bucket,
        "label": label,
        "limit": limit,
        "used": used,
        "free_remaining": free_remaining,
        "pack_credits": pack_credits,
        "remaining": remaining,
        "allowed": remaining > 0,
        "start_day": week["start_day"],
        "end_day": week["end_day"],
        "reset_at": week["reset_at"],
        "message": message,
    }


def _ai_token_daily_settings() -> dict[str, int]:
    """各群体每日 DeepSeek token 额度（后台可改，缺项回退内置默认）。"""
    raw = get_setting(AI_TOKEN_DAILY_SETTING_KEY, {})
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, int] = {}
    for key, default in AI_TOKEN_DAILY_DEFAULTS.items():
        try:
            out[key] = max(0, int(raw.get(key, default)))
        except (TypeError, ValueError):
            out[key] = int(default)
    return out


def _ai_token_bucket_for_user(user: dict | None) -> str:
    """AI token 分档：未登录＝guest；其余复用研究额度的会员分档逻辑。"""
    if not user:
        return "guest"
    return _research_quota_bucket_for_user(user)


def _effective_ai_limit_info(user: dict | None) -> dict:
    """有效 token 额度：override > 套餐级 > 分档默认；管理员/桌面不限（daily_limit=None）。

    返回 daily_limit（每日参考软上限）与 weekly_limit（本周硬上限＝每日×系数）。弹性口径：
    真正拦截看 weekly；每日额度仅作配速展示，允许某日集中借用本周池（防研究综述被截断）。
    """
    if DEPLOYMENT.is_desktop:
        return {"daily_limit": None, "weekly_limit": None, "bucket": "desktop", "label": "桌面版", "source": "desktop"}
    if _is_admin_user(user):
        return {"daily_limit": None, "weekly_limit": None, "bucket": "admin", "label": "管理员", "source": "admin"}
    bucket = _ai_token_bucket_for_user(user)
    label = AI_TOKEN_DAILY_LABELS.get(bucket, bucket)
    base = get_user_ai_limit(int(user["id"])) if user and user.get("id") else get_user_ai_limit(None)
    if base.get("user_override") is not None:
        daily = int(base["user_override"]); source = "user"
    elif base.get("plan_limit") is not None:
        daily = int(base["plan_limit"]); source = "plan"
    else:
        daily = int(_ai_token_daily_settings().get(bucket, 0)); source = "default"
    weekly = daily * AI_TOKEN_WEEKLY_FACTOR
    return {"daily_limit": daily, "weekly_limit": weekly, "bucket": bucket, "label": label, "source": source}


def _ai_token_quota_payload(user: dict | None = None) -> dict:
    """前端余额提示用：本周 DeepSeek 额度剩余（硬上限）+ 今日已用（参考）+ 预警/耗尽 + 周一恢复。"""
    if user is None and has_request_context():
        user = getattr(g, "current_user", None)
    info = _effective_ai_limit_info(user)
    weekly_limit = info["weekly_limit"]
    daily_limit = info["daily_limit"]
    week = _research_quota_week_window()
    day, _, _, _ = _beijing_day_bounds()
    uid = int(user["id"]) if user and user.get("id") else None
    skey = _visitor_session_key() if has_request_context() else ""
    today_used = 0
    weekly_used = 0
    if has_request_context():
        # 与额度闸门 _require_ai_quota_or_raise 同口径：都排除「无限量基础服务」、都认同一个
        # 「重置」标记，否则徽章显示的已用量会与真正拦人的那个数对不上。
        reset_since = _ai_token_quota_reset_at(user, info["bucket"])
        today_used = get_ai_token_usage(
            day=day, user_id=uid, session_key=skey, exclude_features=AI_QUOTA_EXEMPT_FEATURES,
            since_created_at=reset_since,
        )
        weekly_used = get_ai_token_usage_range(
            start_day=week["start_day"], end_day=week["end_day"], user_id=uid, session_key=skey,
            exclude_features=AI_QUOTA_EXEMPT_FEATURES, since_created_at=reset_since,
        )
    if weekly_limit is None:
        return {
            "unlimited": True, "limit": None, "used": weekly_used, "remaining": None,
            "ratio": 1.0, "low": False, "exhausted": False, "reset_at": week["reset_at"],
            "daily_limit": None, "today_used": today_used,
            "bucket": info["bucket"], "label": info["label"], "message": "AI 额度不限",
        }
    weekly_limit = int(weekly_limit)
    remaining = max(0, weekly_limit - weekly_used)
    ratio = (remaining / weekly_limit) if weekly_limit > 0 else 0.0
    exhausted = remaining <= 0
    low = (not exhausted) and ratio <= AI_TOKEN_LOW_RATIO
    pct = int(round(ratio * 100))
    if weekly_limit <= 0:
        message = "当前身份未开放免费 AI 额度"
    elif exhausted:
        message = "本周 AI 额度已用完，下周一恢复"
    else:
        message = f"本周 AI 额度剩余约 {pct}%"
    return {
        "unlimited": False, "limit": weekly_limit, "used": weekly_used, "remaining": remaining,
        "ratio": round(ratio, 4), "low": low, "exhausted": exhausted, "reset_at": week["reset_at"],
        "daily_limit": daily_limit, "today_used": today_used,
        "bucket": info["bucket"], "label": info["label"], "message": message,
    }


def current_view_state() -> dict:
    # 按请求记忆：此函数有约 30 个调用点、单次请求会被多次触达（_is_member_enabled /
    # _desktop_content_access_enabled / 模板等），每次都跑 12 个权限位判定 + 数条配额 DB 查询。
    # 单次请求内结果稳定（API 端点单独直算并回传配额，不经此函数渲染），故整请求复用一份。
    if has_request_context():
        cached = g.get("_view_state_cache")
        if cached is not None:
            return cached
    _refresh_ai_runtime_if_needed()
    full_mode = BASE_RUNTIME.full_resources_ready
    show_runtime_details = bool(
        DEPLOYMENT.is_desktop or _is_admin_user(getattr(g, "current_user", None))
    )
    feature_access = {
        key: (_feature_effective_for_user(key) if has_request_context() else True)
        for key in FEATURE_ACCESS_KEYS
    }
    ai_runtime = _public_ai_runtime_payload(allow_details=bool(feature_access.get("ai", True)))
    ai_enabled = bool(ai_runtime.get("enabled"))
    ai_model = str(ai_runtime.get("model") or "")
    ai_problems = list(ai_runtime.get("problems") or []) if show_runtime_details else []
    state = {
        "search_enabled": BASE_RUNTIME.can_search,
        "pdf_enabled": full_mode,
        "full_mode": full_mode,
        "db_status": BASE_RUNTIME.db_status,
        "data_version": BASE_RUNTIME.data_version,
        "issues": list(BASE_RUNTIME.problems),
        "runtime_root": str(RUNTIME_ROOT) if show_runtime_details else "",
        "db_path": str(BASE_RUNTIME.db_path) if show_runtime_details and BASE_RUNTIME.db_path else "",
        "ai_enabled": ai_enabled,
        "ai_model": ai_model,
        "ai_problems": ai_problems,
        "alipay_enabled": PAYMENT_CONFIG.enabled,
        "alipay_problems": list(PAYMENT_CONFIG.problems),
        "payment_enabled": PAYMENT_CONFIG.enabled,
        "payment_problems": list(PAYMENT_CONFIG.problems),
        "app_mode": DEPLOYMENT.app_mode,
        "desktop_mode": DEPLOYMENT.is_desktop,
        "server_mode": DEPLOYMENT.is_server,
        "desktop_license_enabled": _desktop_license_enabled() if DEPLOYMENT.is_desktop else False,
        "heartbeat_enabled": DEPLOYMENT.enable_idle_shutdown,
        "remote_quit_enabled": DEPLOYMENT.enable_remote_quit,
        "management_api_enabled": DEPLOYMENT.management_api_enabled,
        "public_base_url": DEPLOYMENT.public_base_url,
        "local_console_enabled": DEPLOYMENT.is_desktop,
        "feature_access": feature_access,
        "research_quota": _research_quota_payload() if has_request_context() else {},
        "ai_token_quota": _ai_token_quota_payload() if has_request_context() else {},
        "ai_credits": (
            get_ai_credit_balances(int(getattr(g, "current_user", None)["id"]))
            if has_request_context() and getattr(g, "current_user", None)
            else {"research": 0, "chat": 0, "reader": 0}
        ),
    }
    if has_request_context():
        g._view_state_cache = state
    return state


def _safe_next_url(value: str | None) -> str:
    target = (value or "").strip()
    if not target:
        return url_for("index")
    # 反斜杠归一：浏览器会把 Location 里的 '\' 规整成 '/'，故 '/\evil.com' 会被当成协议相对的
    # '//evil.com' 跳到站外。先把反斜杠折成 '/' 再校验，并显式拒绝协议相对（'//' 开头）目标。
    target = target.replace("\\", "/")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return url_for("index")
    if not target.startswith("/") or target.startswith("//"):
        return url_for("index")
    return target


def _display_price(amount_cents: int, currency: str) -> str:
    amount = max(0, int(amount_cents or 0)) / 100
    if (currency or "").upper() == "CNY":
        return f"¥{amount:.2f}"
    return f"{amount:.2f} {currency or 'CNY'}"


def _display_datetime(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "暂无"
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    beijing = parsed.astimezone(timezone(timedelta(hours=8)))
    return beijing.strftime("%Y-%m-%d %H:%M")


def _display_order_status(value: str) -> str:
    return {
        "pending": "待支付",
        "paid": "已支付",
        "expired": "已过期",
        "cancelled": "已取消",
        "failed": "支付失败",
    }.get(str(value or "").strip().lower(), str(value or "未知"))


def _display_membership_status(value: str) -> str:
    return {
        "anonymous": "未登录",
        "free": "未开通",
        "active": "有效",
        "expired": "已到期",
        "cancelled": "已取消",
    }.get(str(value or "").strip().lower(), str(value or "未知"))


def _display_payment_provider(value: str) -> str:
    return {
        "pending": "待支付",
        "zpay": "在线支付",
        "alipay": "在线支付",
        "manual": "人工开通",
    }.get(str(value or "").strip().lower(), str(value or "待支付"))


def _display_subscription_source(value: str) -> str:
    return {
        "zpay_notify": "在线支付",
        "zpay_return": "在线支付",
        "alipay_notify": "在线支付",
        "alipay_return": "在线支付",
        "manual": "人工开通",
    }.get(str(value or "").strip().lower(), "系统开通")


def _membership_to_dict(snapshot) -> dict:
    return {
        "is_logged_in": snapshot.is_logged_in,
        "is_active_member": snapshot.is_active_member,
        "status": snapshot.status,
        "plan_code": snapshot.plan_code,
        "plan_name": snapshot.plan_name,
        "expires_at": snapshot.expires_at,
        "days_remaining": snapshot.days_remaining,
    }


def _money_matches(amount_cents: int, payment_amount: str) -> bool:
    try:
        expected = int(amount_cents or 0)
        actual = int(round(float(str(payment_amount or "0")) * 100))
    except (TypeError, ValueError):
        return False
    return expected == actual


def _payment_param_user_id(params: dict) -> int | None:
    raw = str(params.get("param") or "").strip()
    match = re.fullmatch(r"user:(\d+)", raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


PAYMENT_QR_MODES = ("api", "manual", "redirect")


def _payment_qr_settings() -> dict:
    """支付二维码渲染设置：全局默认模式 + 各套餐覆盖（含手动二维码图片 data URL）。

    结构：{"default_mode": "api"|"manual"|"redirect",
           "plans": {<plan_code>: {"mode": ""|"api"|"manual"|"redirect", "image": "<data-url>", "note": "..."}}}
    """
    raw = get_setting("payment_qr_settings", {})
    if not isinstance(raw, dict):
        raw = {}
    # 默认 redirect＝保持旧的网关跳转行为，部署后零变化；管理员可逐套餐改成 api/manual。
    default_mode = str(raw.get("default_mode") or "redirect").strip().lower()
    if default_mode not in PAYMENT_QR_MODES:
        default_mode = "redirect"
    plans_raw = raw.get("plans") if isinstance(raw.get("plans"), dict) else {}
    plans: dict[str, dict] = {}
    for code, cfg in plans_raw.items():
        if not isinstance(cfg, dict):
            continue
        mode = str(cfg.get("mode") or "").strip().lower()
        if mode not in PAYMENT_QR_MODES:
            mode = ""
        plans[str(code)] = {
            "mode": mode,
            "image": str(cfg.get("image") or ""),
            "note": str(cfg.get("note") or ""),
        }
    return {"default_mode": default_mode, "plans": plans}


def _effective_qr_mode(plan_code: str, settings: dict | None = None) -> str:
    settings = settings or _payment_qr_settings()
    plan_cfg = settings["plans"].get(str(plan_code), {})
    return plan_cfg.get("mode") or settings["default_mode"]


def _render_checkout_page(order: dict, plan: dict, *, mode: str, pay_url: str = "",
                          qr_image: str = "", note: str = "", message: str = "",
                          gateway_url: str = ""):
    return render_template(
        "payment_checkout.html",
        title="扫码支付",
        app_name=APP_NAME,
        state=current_view_state(),
        order=order,
        plan=plan,
        mode=mode,
        pay_url=pay_url,
        qr_image=qr_image,
        note=note,
        message=message,
        gateway_url=gateway_url,
        status_url=url_for("checkout_order_status", order_no=order["order_no"]),
        result_url=url_for("payment_result", order_no=order["order_no"]),
    )


def _legacy_page_pay_url(order: dict, plan: dict, user: dict) -> str:
    return PAYMENT_CLIENT.build_page_pay_url(
        order_no=order["order_no"],
        subject=f"{PAYMENT_CONFIG.subject_prefix} - {plan['name']}",
        amount_cents=int(order["amount_cents"]),
        param=f"user:{user['id']}",
    )


def _build_payment_checkout_redirect(order: dict, plan: dict, user: dict):
    plan_code = str(plan.get("code") or order.get("plan_code") or "")
    # 监控程序豁免：监控用非会员号反复进支付页但永不付款，会复用同一笔待支付订单——其网关
    # 二维码会过期，导致监控误报「二维码渲染不出来」；旧单还会在「待支付订单」里堆积。这里对
    # 监控请求先清掉它的历史待支付单，再用全新 order_no 重建，保证每次都能渲染出新二维码，且
    # 待支付计数不累积。仅对监控生效，不改动真实用户的复用逻辑（避免误把在途订单作废→收款无法开会员）。
    if _is_monitoring_request():
        try:
            clear_pending_orders(user_id=int(user["id"]))
            order = create_pending_order(user_id=int(user["id"]), plan_code=plan_code)
        except Exception:
            pass
    # 兜底防过时：订单金额/币种若与当前套餐价不一致（管理员改过价，或这是之前失败时按旧价创建的订单），
    # 作废旧单、按新价重建，确保收银页与二维码都用新金额。覆盖「在线支付」与「继续支付」两个入口。
    try:
        if int(order.get("amount_cents") or 0) != int(plan.get("price_cents") or 0) or str(
            order.get("currency") or ""
        ).upper() != str(plan.get("currency") or "CNY").upper():
            order = create_pending_order(user_id=int(user["id"]), plan_code=plan_code)
    except Exception:
        pass
    qr_settings = _payment_qr_settings()
    mode = _effective_qr_mode(plan_code, qr_settings)

    # 手动二维码：管理员在控制台为该套餐粘贴/上传的收款码，独立于 ZPay 是否可用。
    if mode == "manual":
        plan_cfg = qr_settings["plans"].get(plan_code, {})
        record_payment_event(
            order_no=order["order_no"], provider="manual", event_type="create_manual_qr",
            payload={"order_no": order["order_no"], "plan_code": plan_code, "user_id": user["id"]},
        )
        return _render_checkout_page(
            order, plan, mode="manual",
            qr_image=plan_cfg.get("image") or "",
            note=plan_cfg.get("note") or "",
            message="" if plan_cfg.get("image") else "管理员尚未为该套餐配置收款二维码，请稍后再试或联系管理员。",
        )

    if not PAYMENT_CONFIG.enabled:
        flash(
            f"订单 {order['order_no']} 已创建，但在线支付暂时不可用。请稍后再试，或联系管理员协助开通。",
            "warning",
        )
        return redirect(url_for("account"))

    # 跳转模式（旧行为）：直接跳到网关页面。
    if mode == "redirect":
        record_payment_event(
            order_no=order["order_no"], provider="zpay", event_type="create_page_pay",
            payload={"order_no": order["order_no"], "plan_code": plan_code, "user_id": user["id"]},
        )
        return redirect(_legacy_page_pay_url(order, plan, user))

    # API 模式：调 mapi 下单拿支付链接，由本站渲染二维码（绕开网关页面的当面付 PC 扫码）。
    result = PAYMENT_CLIENT.create_mapi_order(
        order_no=order["order_no"],
        subject=f"{PAYMENT_CONFIG.subject_prefix} - {plan['name']}",
        amount_cents=int(order["amount_cents"]),
        client_ip=_client_ip(),
        param=f"user:{user['id']}",
    )
    record_payment_event(
        order_no=order["order_no"], provider="zpay", event_type="create_mapi",
        payload={"order_no": order["order_no"], "plan_code": plan_code, "user_id": user["id"],
                 "code": result.get("code"), "msg": result.get("msg")[:200]},
    )
    pay_url = result.get("qrcode") or result.get("payurl") or result.get("payurl2")
    if result.get("ok") and pay_url:
        return _render_checkout_page(order, plan, mode="api", pay_url=pay_url)

    # mapi 失败（ZPay 小额/当面付通道偶发不返回二维码）：不要把收银页停在「无二维码」死页，
    # 直接跳到网关页面——网关页对该金额能稳定渲染二维码，真实用户可继续支付、监控也能取到码。
    record_payment_event(
        order_no=order["order_no"], provider="zpay", event_type="create_mapi_fallback_redirect",
        payload={"order_no": order["order_no"], "plan_code": plan_code, "user_id": user["id"],
                 "code": result.get("code"), "msg": str(result.get("msg") or "")[:200]},
    )
    return redirect(_legacy_page_pay_url(order, plan, user))


def _build_donation_checkout_redirect(order: dict, user: dict):
    """打赏收银页：金额由用户自定，不做套餐价对账/订单复用，直接按订单金额出码；失败回退网关页面。

    与会员收银的区别：① 不提供 manual 模式（固定收款码无法编码可变金额），default=manual 时降级为 api；
    ② 收银页用一个「打赏」展示用 plan 字典，金额取 order.amount_cents（即用户填写的金额）。
    """
    donation_plan = {"code": "donation", "name": "打赏 / 捐赠", "currency": order.get("currency") or "CNY"}
    subject = "马恩文献检索 · 打赏支持"
    param = f"user:{user['id']}"
    amount_cents = int(order["amount_cents"])

    if not PAYMENT_CONFIG.enabled:
        flash(f"打赏订单 {order['order_no']} 已创建，但在线支付暂时不可用，请稍后再试。", "warning")
        return redirect(url_for("account"))

    mode = _effective_qr_mode("donation")
    if mode == "manual":
        mode = "api"

    def _page_pay():
        return PAYMENT_CLIENT.build_page_pay_url(
            order_no=order["order_no"], subject=subject, amount_cents=amount_cents, param=param
        )

    if mode == "redirect":
        record_payment_event(
            order_no=order["order_no"], provider="zpay", event_type="create_page_pay_donation",
            payload={"order_no": order["order_no"], "user_id": user["id"], "amount_cents": amount_cents},
        )
        return redirect(_page_pay())

    result = PAYMENT_CLIENT.create_mapi_order(
        order_no=order["order_no"], subject=subject, amount_cents=amount_cents,
        client_ip=_client_ip(), param=param,
    )
    record_payment_event(
        order_no=order["order_no"], provider="zpay", event_type="create_mapi_donation",
        payload={"order_no": order["order_no"], "user_id": user["id"], "amount_cents": amount_cents,
                 "code": result.get("code"), "msg": str(result.get("msg") or "")[:200]},
    )
    pay_url = result.get("qrcode") or result.get("payurl") or result.get("payurl2")
    if result.get("ok") and pay_url:
        return _render_checkout_page(order, donation_plan, mode="api", pay_url=pay_url)

    record_payment_event(
        order_no=order["order_no"], provider="zpay", event_type="create_mapi_donation_fallback_redirect",
        payload={"order_no": order["order_no"], "user_id": user["id"], "amount_cents": amount_cents,
                 "code": result.get("code"), "msg": str(result.get("msg") or "")[:200]},
    )
    return redirect(_page_pay())


def _is_member_enabled() -> bool:
    membership = getattr(g, "membership", None)
    return bool(current_view_state()["pdf_enabled"] and membership and membership.is_active_member)


def _admin_content_access_enabled() -> bool:
    # 先判廉价的「是否管理员」，非管理员直接 False，不再触发昂贵的 current_view_state()（它会为
    # 全部功能键各算一次权限、取多次会员快照）。本函数在阅读热路径（/page-image 鉴权）上每请求被调，
    # 旧实现把 current_view_state 顶在最前导致每次翻页都白跑一遍整套视图状态（实测占该请求约 300ms）。
    # 逻辑等价：原式 = pdf_enabled AND is_admin；非管理员时结果恒为 False。
    if not _is_admin_user(getattr(g, "current_user", None)):
        return False
    return bool(current_view_state()["pdf_enabled"])


def _desktop_license_enabled() -> bool:
    if not DEPLOYMENT.is_desktop:
        return False
    cache = load_desktop_sync_cache()
    license_payload = cache.get("license") if isinstance(cache, dict) else {}
    if isinstance(license_payload, dict) and bool(license_payload.get("authorized")):
        return True
    if isinstance(license_payload, dict) and is_device_authorized(
        {
            "status": license_payload.get("status") or "",
            "expires_at": license_payload.get("expires_at") or "",
        }
    ):
        return True
    try:
        return load_activation_status().valid
    except Exception:
        return False


def _desktop_content_access_enabled() -> bool:
    return bool(DEPLOYMENT.is_desktop and current_view_state()["pdf_enabled"] and _desktop_license_enabled())


def _content_access_enabled(feature: str | None = None) -> bool:
    if not BASE_RUNTIME.full_resources_ready:
        return False
    if _desktop_content_access_enabled() or _admin_content_access_enabled():
        return True
    if feature and not DEPLOYMENT.is_desktop and _feature_is_available(feature):
        return _feature_effective_for_user(feature)
    return bool(_is_member_enabled())


def _load_access_policy() -> dict:
    # 按请求记忆已构建的访问策略：load_shared_access_policy 每次都从设置 JSON 重建整份归一化策略字典，
    # 而一次请求里它会被调用很多次（current_view_state 对 12 个权限位各调一次、reader 钩子再调等）。
    # 策略在单次请求内不变，故缓存在 flask.g 上、整请求复用一份；无请求上下文（后台线程）则照常每次重建。
    if has_request_context():
        cached = g.get("_access_policy_cache")
        if cached is not None:
            return cached
    policy = load_shared_access_policy(include_saved=DEPLOYMENT.is_server)
    if has_request_context():
        g._access_policy_cache = policy
    return policy


def _membership_plan_code_for_user(user: dict | None) -> str:
    return membership_plan_code_for_user(user)


def _feature_allowed_by_policy(policy: dict, feature: str, user: dict | None = None) -> bool:
    return shared_feature_allowed_by_policy(policy, feature, user)


def _feature_effective_for_user(feature: str, user: dict | None = None) -> bool:
    if feature not in FEATURE_ACCESS_KEYS:
        return True
    policy = _load_access_policy()
    target = user if user is not None else getattr(g, "current_user", None)
    return _feature_allowed_by_policy(policy, feature, target)


def _plan_feature_access_rows(plans: list[dict], policy: dict) -> list[dict]:
    return build_plan_feature_access_rows(plans, policy)


def _audience_feature_access_rows(policy: dict) -> list[dict]:
    return build_audience_feature_access_rows(policy)


def _feature_access_rows(users: list[dict]) -> list[dict]:
    policy = _load_access_policy()
    return build_feature_access_rows(users, policy)


def _ai_blocked_emails(policy: dict) -> set[str]:
    """从权限策略中找出被单独禁用 AI 的邮箱集合（access_policy.users[email].ai == False）。

    这是「会员与权限」个别权限与「总览」封禁按钮共用的同一份数据，二者天然联通。
    """
    blocked: set[str] = set()
    for email, values in (policy.get("users") or {}).items():
        if isinstance(values, dict) and values.get("ai") is False:
            blocked.add(normalize_email(str(email)))
    return blocked


def _reader_blocked_emails(policy: dict) -> set[str]:
    blocked: set[str] = set()
    for email, values in (policy.get("users") or {}).items():
        if isinstance(values, dict) and values.get("library") is False:
            blocked.add(normalize_email(str(email)))
    return blocked


def _reader_bans() -> dict:
    payload = get_setting("reader_bans", {})
    return payload if isinstance(payload, dict) else {}


def _save_reader_bans(payload: dict) -> None:
    set_setting("reader_bans", payload, updated_by=_management_actor_label(True))


def _reader_ip_bans(payload: dict | None = None) -> dict:
    bans = payload if isinstance(payload, dict) else _reader_bans()
    values = bans.setdefault("ips", {})
    return values if isinstance(values, dict) else {}


def _reader_user_bans(payload: dict | None = None) -> dict:
    bans = payload if isinstance(payload, dict) else _reader_bans()
    values = bans.setdefault("users", {})
    return values if isinstance(values, dict) else {}


def _reader_ban_status(actor: dict, policy: dict | None = None, bans: dict | None = None) -> bool:
    actor_type = str(actor.get("actor_type") or "").strip()
    policy = policy if policy is not None else _load_access_policy()
    bans = bans if bans is not None else _reader_bans()
    if actor_type == "user":
        email = normalize_email(str(actor.get("email") or ""))
        if email and email in _reader_blocked_emails(policy):
            return True
        user_id = str(actor.get("user_id") or "").strip()
        return bool(user_id and user_id in _reader_user_bans(bans))
    ip = str(actor.get("client_ip") or "").strip()
    return bool(ip and ip in _reader_ip_bans(bans))


def _feature_is_available(feature: str) -> bool:
    if feature == "search":
        return bool(BASE_RUNTIME.can_search)
    if feature in {"viewer", "library"}:
        return bool(BASE_RUNTIME.full_resources_ready)
    if feature == "dictionary":
        return dictionary_available()
    if feature in {"ai", "associative"}:
        # 联想检索与 AI 导学共用同一个对话模型：模型未配置则二者都不可用。
        return bool(AI_CONFIG.enabled)
    if feature == "ai_web":
        # 智谱联网通道随基础 AI 一起开关：基础 AI 不可用或智谱未配 Key 时整体不可用。
        return bool(AI_CONFIG.enabled and AI_CONFIG.zhipu_enabled)
    if feature == "static_library":
        # 「原文文库」：本地确有镜像内容（static_library/<book>/）才算可用。
        return static_library_has_content()
    if feature == "stream_reading":
        # 「流式阅读」：本地确有《文集》网页适配内容（stream_library/wenji-zh/）才算可用。
        return stream_reading_has_content()
    return True


def _feature_available_to_registered(feature: str) -> bool:
    """仅凭登录（成为注册用户、尚未开通会员）本身能否解锁该功能。
    用于给访客更准确的提示：登录即可用的功能 → 「请先登录」；会员专属功能 → 「登录并开通会员」。"""
    if not _feature_is_available(feature):
        return False
    policy = _load_access_policy()
    reg = ((policy.get("audience") or {}).get("registered") or {}) if isinstance(policy, dict) else {}
    return bool(reg.get(feature))


def _notes_access_enabled() -> bool:
    """笔记 / 知识库为会员专属功能：可用性恒真（DB 常在），仅按用户权限位裁决。"""
    return bool(_feature_is_available("notes") and _feature_effective_for_user("notes"))


def _ai_web_access_enabled() -> bool:
    """当前请求者是否可使用智谱联网通道：管理员始终可用（便于验证），其余按 ai_web 权限位。"""
    if not _feature_is_available("ai_web"):
        return False
    if _is_admin_user(getattr(g, "current_user", None)):
        return True
    return _feature_effective_for_user("ai_web")


def _policy_allows_all(policy: dict, features: list[str], user: dict | None) -> bool:
    return all(_feature_allowed_by_policy(policy, feature, user) for feature in features)


def _active_plan_allows_all(policy: dict, features: list[str]) -> bool:
    for plan in list_active_plans():
        code = str(plan.get("code") or "").strip()
        if not code:
            continue
        user = {"email": f"plan-{code}@local.invalid", "role": "member", "plan_code": code}
        if _policy_allows_all(policy, features, user):
            return True
    return False


def _current_user_allows_all(features: list[str]) -> bool:
    if _admin_content_access_enabled() or _desktop_content_access_enabled():
        return True
    return all(_feature_effective_for_user(feature) for feature in features)


def _reader_access_entry(kind: str, features: list[str], href: str) -> dict:
    policy = _load_access_policy()
    user = getattr(g, "current_user", None)
    prefix_map = {
        "full": "index.reader_full",
        "ai": "index.reader_ai",
        "dictionary": "index.dictionary",
    }
    prefix = prefix_map.get(kind, f"index.{kind}")
    available = all(_feature_is_available(feature) for feature in features)
    current_allowed = available and _current_user_allows_all(features)
    registered_user = {"email": "registered-user@local.invalid", "role": "member"}
    registered_allowed = available and _policy_allows_all(policy, features, registered_user)
    plan_allowed = available and _active_plan_allows_all(policy, features)

    if current_allowed:
        status = "available"
        target = href
        disabled = False
    elif not available:
        status = "maintenance" if kind == "ai" and not _feature_is_available("ai") else "unavailable"
        target = "#"
        disabled = True
    elif not user and registered_allowed:
        status = "login_required"
        target = url_for("login", next=href)
        disabled = False
    elif plan_allowed:
        status = "subscribe_required"
        target = url_for("pricing", next=href)
        disabled = False
    else:
        status = "unavailable"
        target = "#"
        disabled = True

    status_key = f"{prefix}_{status}"
    action_key = f"{prefix}_action" if current_allowed else status_key
    return {
        "kind": kind,
        "status": status,
        "kicker_key": f"{prefix}_kicker",
        "title_key": f"{prefix}_title",
        "description_key": f"{prefix}_description",
        "action_key": action_key,
        "status_key": status_key,
        "href": target,
        "disabled": disabled,
        "enabled": current_allowed,
    }


def _reader_access_entries() -> list[dict]:
    return [
        _reader_access_entry("full", ["library"], url_for("reader")),
        _reader_access_entry("dictionary", ["dictionary"], url_for("dictionary")),
        _reader_access_entry("ai", ["library", "ai"], url_for("library")),
    ]


def _card_access_status(kind: str, features: list[str]) -> dict:
    """复用阅读器卡的权限判定，仅取状态 pill（status / status_key），
    供期刊卡、原文文库卡叠加显示与阅读器卡同款「已可用 / 登录即可使用 /
    开通会员后使用 / 暂未开放」状态标签。这两张卡各自保留原有按钮逻辑，
    故 href 不参与显示，用 '#' 占位即可。"""
    entry = _reader_access_entry(kind, features, "#")
    return {"status": entry["status"], "status_key": entry["status_key"]}


# ---- 首页功能栏「自定义彩色标签」（控制台·内容运营可增删，每张卡片一组）----
# 原有「已可用 / 登录即可使用 / 开通会员后使用 / 暂未开放」状态 pill 的逻辑完全保留、自动按权限显示；
# 这里是在其旁边「额外」叠加管理员自定义的彩色小标签（如「新上线」「限时免费」）。默认空＝不显示，
# 行为与从前一致。数据存设置项 index_feature_tags={"full":[{text,color}],"dictionary":[...],...}。
# citation=引文检索面板（标准/联想检索），chapter=篇章直达面板；二者无状态 pill，仅在标题旁叠加标签。
_FEATURE_TAG_CARDS = ("full", "dictionary", "ai", "journal", "liushi", "citation", "chapter")
_FEATURE_TAG_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_FEATURE_TAG_FALLBACK_COLOR = "#157f4c"
_FEATURE_TAG_MAX_PER_CARD = 12


def _coerce_hex_color(value: object, fallback: str = _FEATURE_TAG_FALLBACK_COLOR) -> str:
    text = str(value or "").strip()
    if _FEATURE_TAG_HEX_RE.match(text):
        return "#" + text.lstrip("#").lower()
    return fallback


def _tag_text_color_for(bg_hex: str) -> str:
    """按背景亮度选黑/白前景色，保证标签文字在任意底色上都可读。"""
    try:
        r, g, b = int(bg_hex[1:3], 16), int(bg_hex[3:5], 16), int(bg_hex[5:7], 16)
    except (ValueError, IndexError):
        return "#ffffff"
    return "#ffffff" if (0.299 * r + 0.587 * g + 0.114 * b) < 150 else "#1f2937"


def _get_feature_tags() -> dict[str, list[dict]]:
    """读取并清洗首页功能栏自定义标签；始终返回所有卡片的键，缺失/异常时为空列表。"""
    raw = get_setting("index_feature_tags", {})
    raw = raw if isinstance(raw, dict) else {}
    result: dict[str, list[dict]] = {}
    for card in _FEATURE_TAG_CARDS:
        items = raw.get(card)
        tags: list[dict] = []
        if isinstance(items, list):
            for item in items[:_FEATURE_TAG_MAX_PER_CARD]:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()[:20]
                if not text:
                    continue
                color = _coerce_hex_color(item.get("color"))
                tags.append({"text": text, "color": color, "fg": _tag_text_color_for(color)})
        result[card] = tags
    return result


# ---- 引文检索「引用格式」自定义模板（控制台·内容运营，存设置项 citation_formats）----
# 后台可改三种引用格式（国标 GB/T 7714 / 《中国社会科学》/《马克思主义研究》）的输出模板，仅作用于
# 「卷·页」型标准著作的多格式引文（hit.citations）；公文/选编等特殊体例不套模板。设置项只存「改过且
# 与默认不同」的格式，空＝全用 search.DEFAULT_CITATION_TEMPLATES。保存后即时注入 corpus、当场生效。
_CITATION_FORMAT_LABELS = {
    "gb2015": "国标 GB/T 7714—2015",
    "zgshkx": "《中国社会科学》",
    "mkszyj": "《马克思主义研究》",
}
_CITATION_FORMAT_KEYS = ("gb2015", "zgshkx", "mkszyj")
_CITATION_TEMPLATE_MAXLEN = 240


def _load_citation_formats() -> dict[str, str]:
    """读取后台自定义引用格式模板：仅保留合法键、非空、且与默认不同的覆盖项。"""
    raw = get_setting("citation_formats", {})
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for key in _CITATION_FORMAT_KEYS:
        tpl = raw.get(key)
        if isinstance(tpl, dict):  # 容忍 {"template": "..."} 形态
            tpl = tpl.get("template")
        tpl = str(tpl or "").strip()[:_CITATION_TEMPLATE_MAXLEN]
        if tpl and tpl != DEFAULT_CITATION_TEMPLATES.get(key, "").strip():
            out[key] = tpl
    return out


def _citation_formats_editor() -> list[dict]:
    """供后台编辑器渲染：每格式给出标签、当前生效模板（自定义优先）、默认模板、是否自定义。"""
    customs = _load_citation_formats()
    rows: list[dict] = []
    for key in _CITATION_FORMAT_KEYS:
        default_tpl = DEFAULT_CITATION_TEMPLATES.get(key, "")
        rows.append({
            "key": key,
            "label": _CITATION_FORMAT_LABELS.get(key, key),
            "template": customs.get(key, default_tpl),
            "default": default_tpl,
            "is_custom": key in customs,
        })
    return rows


def _apply_citation_formats_to_corpus() -> None:
    """把后台自定义模板注入 corpus（启动时与每次保存后调用）。失败不影响服务，回退默认模板。"""
    if corpus is not None:
        try:
            corpus.set_citation_templates(_load_citation_formats())
        except Exception:
            LOGGER.warning("Failed to apply citation format templates", exc_info=True)


# 启动时注入一次（corpus 已在上方创建、admin 设置库已就绪）。
_apply_citation_formats_to_corpus()


# ---- 首页功能卡「顺序」（控制台·内容运营可拖动调序，存设置项 index_card_order）----
# 覆盖首页左栏的 5 张主功能卡。citation/chapter 是搜索面板内嵌标签、不是独立卡片，故不在此列。
# 设计原则：保存的顺序只认已知卡键并去重；任何缺失的已知卡（含将来新增的卡）按默认顺序补到末尾，
# 保证新卡永远会出现、且老的 index_card_order 设置不会把它吞掉。
_FEATURE_CARD_ORDER_KEYS = ("full", "dictionary", "ai", "liushi", "journal")
_FEATURE_CARD_LABELS = {
    "full": "全文阅读器",
    "dictionary": "马克思主义大辞典",
    "ai": "AI 导学阅读器",
    "liushi": "流式阅读",
    "journal": "期刊提醒",
}


def _sanitize_card_order(values: object) -> list[str]:
    """把任意输入清洗成合法的卡片顺序：保留已知卡键、去重，缺失的已知卡按默认顺序补到末尾。"""
    order: list[str] = []
    if isinstance(values, (list, tuple)):
        for key in values:
            key = str(key or "").strip()
            if key in _FEATURE_CARD_ORDER_KEYS and key not in order:
                order.append(key)
    for key in _FEATURE_CARD_ORDER_KEYS:
        if key not in order:
            order.append(key)
    return order


def _get_card_order() -> list[str]:
    """首页功能卡当前顺序（读 index_card_order 设置并清洗）。"""
    return _sanitize_card_order(get_setting("index_card_order", []))


def _index_feature_cards() -> list[dict]:
    """按控制台设定的顺序，组装首页左栏功能卡的渲染数据。每个元素带 type
    （reader/journal/wenku）+ 该类型模板所需字段；不可用的卡（如无镜像内容的文库）自动跳过。
    顺序之外的逻辑（权限状态 pill、按钮、彩色标签）全部沿用原有判定，不受影响。"""
    reader = {entry["kind"]: entry for entry in _reader_access_entries()}
    cards: dict[str, dict] = {}
    for kind in ("full", "dictionary", "ai"):
        if kind in reader:
            cards[kind] = {"key": kind, "type": "reader", "entry": reader[kind]}
    cards["journal"] = {
        "key": "journal",
        "type": "journal",
        "status_key": _card_access_status("journal", ["journal_alerts"])["status_key"],
    }
    # 「原文文库」（外文原著）已并入著作目录（见 _foreign_library_books / library.html），
    # 不再单列首页卡片；/wenku 阅读器路由仍保留，供著作目录里的外文原著卷链接进入。
    if _feature_is_available("stream_reading"):
        cards["liushi"] = {
            "key": "liushi",
            "type": "liushi",
            "status_key": _card_access_status("liushi", ["stream_reading"])["status_key"],
        }
    return [cards[key] for key in _get_card_order() if key in cards]


_READER_ACCESS_STATUS_RANK = {
    "available": 0,
    "login_required": 1,
    "subscribe_required": 2,
    "maintenance": 3,
    "unavailable": 4,
}


def _chapter_search_access() -> dict:
    """首页篇章搜索的跳转目标，跟随控制台权限：优先 AI 导学、其次全文阅读器；
    都不能直接用时给出登录/开通的引导（按钮）。后续权限变化会自动反映在这里。"""
    entries = _reader_access_entries()  # full(library) + ai(library+ai)

    def _rank(entry: dict) -> tuple:
        return (_READER_ACCESS_STATUS_RANK.get(entry.get("status"), 9), 0 if entry.get("kind") == "ai" else 1)

    best = sorted(entries, key=_rank)[0]
    is_ai = best.get("kind") == "ai"
    label = "AI 导学阅读器" if is_ai else "全文阅读器"
    if best.get("enabled"):
        # 篇章直达一律进「AI 导学阅读器」：只要站点启用了 AI 且当前用户能打开阅读器（游客凭 library 权限即可），
        # 就走带 AI 导读侧栏的版本——AI 对话本身在阅读器内按登录/权限内联提示「登录后使用」。
        # 旧逻辑按「哪个入口 available」排名：游客的 AI 入口是 login_required，会被降级到老版纯 PDF 阅读器（即本 bug）。
        use_ai = _feature_is_available("ai")
        return {
            "available": True,
            "mode": "ai" if use_ai else "reader",
            "label": "AI 导学阅读器" if use_ai else "全文阅读器",
            "status": "available",
            "href": "",
        }
    href = best.get("href") or ""
    return {
        "available": False,
        "mode": "",
        "label": label,
        "status": best.get("status") or "unavailable",
        "href": "" if href in ("#", "") else href,
    }


def _chapter_scope_books() -> list[dict]:
    """篇章直达「切换书籍」下拉的书库清单：仅收录已开放且语料里有卷册的书库，
    按 sort_order 排序。label 去掉书名号方便下拉展示，value 用书库键传给后端。"""
    out: list[dict] = []
    added_collections: set[str] = set()
    for cfg in sorted(BOOK_CONFIGS, key=lambda c: c.sort_order):
        if not getattr(cfg, "available", True):
            continue
        if corpus is not None and not corpus.get_volumes(cfg.key):
            continue
        if cfg.collection and cfg.collection not in added_collections:
            collection_books = [
                c.key for c in BOOK_CONFIGS
                if c.collection == cfg.collection and getattr(c, "available", True)
                and (corpus is None or corpus.get_volumes(c.key))
            ]
            if collection_books:
                label = f"{_COLLECTION_LABELS.get(cfg.collection, cfg.collection)}（专题）"
                out.append({"key": cfg.collection, "label": label, "is_collection": True})
                added_collections.add(cfg.collection)
        label = (cfg.short_title or cfg.title).strip().strip("《》")
        out.append({"key": cfg.key, "label": label, "is_collection": False})
    return out


def _ai_reader_upsell(target_href: str) -> dict:
    policy = _load_access_policy()
    user = getattr(g, "current_user", None)
    features = ["library", "ai"]
    available = all(_feature_is_available(feature) for feature in features)
    current_allowed = available and _current_user_allows_all(features)
    registered_user = {"email": "registered-user@local.invalid", "role": "member"}
    registered_allowed = available and _policy_allows_all(policy, features, registered_user)
    plan_allowed = available and _active_plan_allows_all(policy, features)

    if current_allowed:
        status = "available"
        href = target_href
    elif not available:
        status = "unavailable"
        href = ""
    elif not user and registered_allowed:
        status = "login_required"
        href = url_for("login", next=target_href)
    elif plan_allowed:
        status = "subscribe_required"
        href = url_for("pricing", next=target_href)
    else:
        status = "unavailable"
        href = ""

    return {
        "status": status,
        "title_key": f"viewer.ai_upsell_{status}_title",
        "body_key": f"viewer.ai_upsell_{status}_body",
        "action_key": f"viewer.ai_upsell_{status}_action",
        "href": href,
    }


def _require_feature(feature: str) -> None:
    if _admin_content_access_enabled() or _desktop_content_access_enabled():
        return
    if not _feature_is_available(feature):
        abort(403, description=f"当前功能暂不可用：{FEATURE_ACCESS_LABELS.get(feature, feature)}。")
    if not _feature_effective_for_user(feature):
        abort(403, description=f"当前账号暂未开放{FEATURE_ACCESS_LABELS.get(feature, feature)}权限。")


def _require_content_feature(feature: str) -> None:
    if _desktop_content_access_enabled() or _admin_content_access_enabled():
        return
    if DEPLOYMENT.is_desktop:
        if request.path.startswith("/api/"):
            abort(403, description="本地完整资料需要先在本地诊断与同步中完成网站授权，或使用已有本机激活。")
        flash("本地完整资料需要先完成网站授权；断网时可继续使用已缓存的有效授权。", "warning")
        raise _RedirectTo(url_for("control", section="sync"))
    if not _feature_is_available(feature):
        abort(403, description=f"当前功能暂不可用：{FEATURE_ACCESS_LABELS.get(feature, feature)}。")
    if _feature_effective_for_user(feature):
        return
    if request.path.startswith("/api/"):
        if not getattr(g, "current_user", None):
            abort(401, description="请先登录会员账号。")
        abort(403, description=f"当前账号暂未开放{FEATURE_ACCESS_LABELS.get(feature, feature)}权限。")
    _label = FEATURE_ACCESS_LABELS.get(feature, feature)
    if not getattr(g, "current_user", None):
        # 面向用户的措辞（不暴露后台/管理概念）：登录即可用 vs 需开通会员，区分给出，避免误导。
        if _feature_available_to_registered(feature):
            flash(f"「{_label}」需登录后使用，请先登录。", "warning")
        else:
            flash(f"「{_label}」为会员功能，登录并开通相应会员后即可使用。", "warning")
        raise _RedirectTo(url_for("login", next=request.full_path if request.query_string else request.path))
    flash(f"「{_label}」为会员功能，开通相应会员后即可使用。", "warning")
    raise _RedirectTo(url_for("pricing", next=request.full_path if request.query_string else request.path))


def _viewer_entry_feature() -> str:
    return "viewer" if (request.args.get("q") or "").strip() else "library"


def _require_reader_asset_access() -> None:
    if _content_access_enabled("viewer") or _content_access_enabled("library"):
        return
    _require_content_feature("viewer")


def _is_admin_user(user: dict | None) -> bool:
    return bool(user and str(user.get("role") or "").strip().lower() == "admin")


def _require_login_page() -> None:
    if getattr(g, "current_user", None):
        return
    flash("请先登录，再继续访问该功能。", "warning")
    raise _RedirectTo(url_for("login", next=request.full_path if request.query_string else request.path))


def _require_paid_member() -> None:
    if _desktop_content_access_enabled():
        return
    if DEPLOYMENT.is_desktop:
        if request.path.startswith("/api/"):
            abort(403, description="本地完整资料需要先在本地诊断与同步中完成网站授权，或使用已有本机激活。")
        flash("本地完整资料需要先完成网站授权；断网时可继续使用已缓存的有效授权。", "warning")
        raise _RedirectTo(url_for("control", section="sync"))
    if not getattr(g, "current_user", None):
        if request.path.startswith("/api/"):
            abort(401, description="请先登录会员账号。")
        flash("请先登录会员账号。", "warning")
        raise _RedirectTo(url_for("login", next=request.full_path if request.query_string else request.path))
    if _admin_content_access_enabled() or _feature_effective_for_user("viewer") or _feature_effective_for_user("library"):
        return
    if request.path.startswith("/api/"):
        abort(403, description="当前功能仅对会员开放。")
    flash("当前功能仅对会员开放，请先开通会员。", "warning")
    raise _RedirectTo(url_for("pricing", next=request.full_path if request.query_string else request.path))


def _require_admin() -> None:
    user = getattr(g, "current_user", None)
    if not user:
        raise _RedirectTo(url_for("login", next=request.full_path if request.query_string else request.path))
    if not _is_admin_user(user):
        abort(403, description="当前账号没有管理后台权限。")
    _enforce_admin_ip_allowlist()
    if _admin_2fa_enabled() and not _admin_2fa_session_ok():
        raise _RedirectTo(url_for("admin_2fa", next=request.full_path if request.query_string else request.path))


def _enforce_admin_ip_allowlist() -> None:
    # 可选：env ADMIN_IP_ALLOWLIST(逗号分隔)配置后，仅允许名单内真实IP访问后台；
    # 未配置则不启用，避免把自己锁死。_client_ip() 取 XFF 最右(Caddy 写入、不可伪造)。
    allow = _env_csv("ADMIN_IP_ALLOWLIST")
    if not allow:
        return
    if _client_ip() not in allow:
        abort(403, description="当前网络不在管理后台允许的 IP 名单内。")


ADMIN_2FA_PURPOSE = "admin_2fa"
ADMIN_2FA_TTL_HOURS = 12


def _admin_2fa_enabled() -> bool:
    # 仅在「服务器模式 + 已配置发信邮箱 + 未显式关闭」三者都满足时启用管理员邮箱二次验证。
    # 任一不满足即安全跳过，绝不把管理员锁在门外；应急关闭：环境变量 DISABLE_ADMIN_2FA=1。
    if _env_flag("DISABLE_ADMIN_2FA", False):
        return False
    if not DEPLOYMENT.is_server:
        return False
    return _account_email_configured()


def _admin_2fa_session_ok() -> bool:
    raw = session.get("admin_2fa_verified_at")
    if not raw:
        return False
    try:
        verified = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - verified) < timedelta(hours=ADMIN_2FA_TTL_HOURS)


def _mark_admin_2fa_verified() -> None:
    session["admin_2fa_verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session.permanent = True


def _mask_email(email: str) -> str:
    email = (email or "").strip()
    if "@" not in email:
        return email or "（未绑定邮箱）"
    name, _, domain = email.partition("@")
    if len(name) <= 2:
        masked = (name[:1] or "*") + "*"
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"


def _mask_email_public(email: str) -> str:
    """公开陈列用邮箱打码：保留本地名头尾字符、中间用「x」代替，域名保留（读起来仍像邮箱）。
    头尾保留字数与中缀 x 的个数都按本地名长度自动伸缩——名越长、露的头尾越多、中间的 x 也越多
    （x 数≈被隐藏的中间字数，夹在 3–6 个之间），既不泄露完整账号、又能让本人一眼认出是自己，
    例如 bihongqiu→bihxxxqiu、3040556604→304xxxx604。与内部审计日志用的 _mask_email
    （星号、用于 2FA）分工不同、互不影响。邮箱异常/为空时返回空串（公开场合不陈列问题邮箱）。"""
    email = (email or "").strip()
    if "@" not in email:
        return ""
    name, _, domain = email.partition("@")
    domain = domain.strip()
    n = len(name)
    if not n or not domain:
        return ""
    if n <= 2:
        head, tail = 1, 0          # 极短：只留首字符
    elif n <= 4:
        head, tail = 1, 1          # 短：头 1 尾 1
    elif n <= 8:
        head, tail = 2, 2          # 中：头 2 尾 2
    else:
        head, tail = 3, 3          # 长：头 3 尾 3
    hidden = max(1, n - head - tail)
    x_count = min(max(hidden, 3), 6)   # 中缀 x 个数随被隐藏字数增长、夹在 3–6 个
    head_s = name[:head]
    tail_s = name[n - tail:] if tail else ""
    return f"{head_s}{'x' * x_count}{tail_s}@{domain}"


def _dispatch_admin_2fa_code(email: str, errors: list) -> None:
    # 发送管理员二次验证码；同一会话 60 秒内不重复发送，避免刷新/连点狂发邮件。
    if not email:
        errors.append("当前管理员账号未绑定邮箱，无法发送验证码。可由运维用 DISABLE_ADMIN_2FA=1 临时关闭。")
        return
    last = session.get("admin_2fa_sent_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_dt) < timedelta(seconds=60):
                return
        except ValueError:
            pass
    code = _make_email_code()
    try:
        create_account_email_token(email=email, purpose=ADMIN_2FA_PURPOSE, code=code, ttl_minutes=15)
        body = (
            "您好：\n\n"
            f"您正在登录网站管理后台，二次验证码是：{code}\n\n"
            "验证码 15 分钟内有效。如非本人操作，请立即修改管理员密码。"
        )
        _send_account_email(email, "管理后台登录验证码", body)
        session["admin_2fa_sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to send admin 2FA code: %s", exc)
        errors.append("验证码发送失败，请稍后重试或联系运维。")


def _management_actor_label(remote_admin: bool) -> str:
    if remote_admin:
        user = getattr(g, "current_user", None) or {}
        return str(user.get("email") or user.get("id") or "admin")
    remote = (request.remote_addr or "").strip() or "local"
    return f"local-console:{remote}"


def _log_management_action(
    *,
    action: str,
    target: str,
    result: str,
    remote_admin: bool,
    details: dict[str, object] | None = None,
) -> None:
    payload = {
        "scope": "admin" if remote_admin else "control",
        "actor": _management_actor_label(remote_admin),
        "action": action,
        "target": target,
        "result": result,
    }
    if details:
        payload["details"] = details
    LOGGER.info("management_action %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


class _RedirectTo(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


def _shutdown_app() -> None:
    try:
        if os.name == "nt":
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            os.kill(os.getpid(), signal.SIGINT)
    except Exception:
        os._exit(0)


def _watchdog() -> None:
    time.sleep(_GRACE)
    while True:
        time.sleep(_CHECK_INTERVAL)
        if time.time() - _last_ping[0] > _TIMEOUT:
            LOGGER.info("No heartbeat received. Shutting down.")
            _shutdown_app()
            break


def _require_management_token() -> None:
    if not DEPLOYMENT.management_api_enabled:
        abort(403, description="当前运行模式未启用本地管理接口。")
    token = request.headers.get(APP_TOKEN_HEADER, "").strip()
    if not token or not secrets.compare_digest(token, REQUEST_TOKEN):
        abort(403, description="请求未通过本地令牌校验。")


def _desktop_bearer_token() -> str:
    header = request.headers.get("Authorization", "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-Desktop-Token", "").strip()


def _require_desktop_device() -> dict:
    if not DEPLOYMENT.is_server:
        abort(403, description="桌面同步接口仅在网站服务器模式启用。")
    token = _desktop_bearer_token()
    if not token:
        abort(401, description="缺少本地端同步令牌。")
    device = get_device_by_token(token)
    if not is_device_authorized(device):
        abort(403, description="本地设备授权不可用或已过期。")
    touch_device_sync(int(device["id"]))
    return device


def _public_desktop_sync_payload(device: dict) -> dict:
    return {
        "device": {
            "id": device.get("id"),
            "label": device.get("label") or "",
            "user_email": device.get("user_email") or "",
            "fingerprint": device.get("fingerprint") or "",
            "status": device.get("status") or "",
            "expires_at": device.get("expires_at") or "",
            "last_sync_at": device.get("last_sync_at") or "",
        },
        "license": {
            "authorized": is_device_authorized(device),
            "status": device.get("status") or "",
            "expires_at": device.get("expires_at") or "",
        },
        "settings": {
            "ai": AI_CONFIG.to_public_dict(),
            "site_texts": _effective_site_text_map(),
            "announcement": get_setting("announcement", {}),
            "sync_cache_seconds": 7 * 24 * 60 * 60,
        },
        "release": latest_release(),
        "server_time": time.time(),
    }


def _require_search() -> None:
    if not BASE_RUNTIME.can_search or corpus is None:
        abort(503, description="索引不可用，请先检查资料包和数据库校验状态。")


def _require_full_mode() -> None:
    if not current_view_state()["pdf_enabled"]:
        abort(403, description="当前未启用完整资料功能。")


def _require_ai() -> None:
    _refresh_ai_runtime_if_needed()
    if not AI_CONFIG.enabled:
        abort(
            503,
            description="AI 对话功能暂时不可用，请稍后再试。",
        )


def _normalize_source_file(source_file: str) -> str:
    return str(Path(source_file).as_posix()) if source_file else ""


def _resolve_pdf_path(source_file: str, *, require_full_mode: bool = True) -> Path:
    if require_full_mode:
        _require_full_mode()
    rel = _normalize_source_file(source_file)
    if not rel:
        abort(400, description="缺少 PDF 文件参数。")
    if rel not in ALLOWED_SOURCE_FILES:
        abort(404, description="请求的 PDF 不在资料白名单中。")

    pdf_path = (RUNTIME_ROOT / rel).resolve()
    pdf_root = BASE_RUNTIME.pdf_root.resolve()
    try:
        pdf_path.relative_to(pdf_root)
    except ValueError:
        abort(404, description="PDF 路径不在资料目录内。")
    if pdf_path.suffix.lower() != ".pdf" or not pdf_path.exists():
        abort(404, description="PDF 文件不存在。")
    return pdf_path


def _pdf_render_available(source_file: str) -> bool:
    """是否可以将该卷的 PDF 渲染成书页图像。

    与 ``_resolve_pdf_path`` 不同，本函数不会 abort，仅返回布尔值，用于判断阅读器
    应使用「书页图像」还是「OCR 文字」渲染。《全集》等仅在云端保留 OCR 文本、未随包
    下发原始 PDF 的卷册会返回 False，从而回退到纯文字阅读。
    """
    rel = _normalize_source_file(source_file)
    if not rel or rel not in ALLOWED_SOURCE_FILES:
        return False
    try:
        pdf_path = (RUNTIME_ROOT / rel).resolve()
        pdf_path.relative_to(BASE_RUNTIME.pdf_root.resolve())
    except (ValueError, OSError):
        return False
    return pdf_path.suffix.lower() == ".pdf" and pdf_path.exists()


def _highlight_terms(query_text: str) -> list[str]:
    query_text = " ".join(query_text.split())
    if not query_text:
        return []

    candidates: list[str] = [query_text]
    compact = query_text.replace(" ", "")
    if compact != query_text and len(compact) >= 4:
        candidates.append(compact)

    parts = []
    for token in re.split(r"[\s，。；：、“”‘’？,.!?;:()（）【】《》]+", query_text):
        token = token.strip()
        if len(token) >= 2:
            parts.append(token)
    parts.sort(key=len, reverse=True)
    candidates.extend(parts[:8])

    seen: set[str] = set()
    ordered: list[str] = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _split_cooc_keywords(query_text: str) -> list[str]:
    """同段多词检索：把输入按空白与常见中英标点切成多个关键词，去重并丢弃归一化后过短者。

    归一化后长度 < 2 的词（如单个汉字/字母）区分度太低、共现近乎处处命中，故剔除。
    """
    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s，。；：、“”‘’？！,.!?;:()（）【】《》·\-—_/|]+", query_text or ""):
        token = token.strip()
        if not token:
            continue
        if len(normalize(token)) < 2 or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _per_char_term_rects(page, terms: list[str]) -> list:
    """逐字文本层（如《邓小平文选》第3卷：每个汉字单独成 span/行）上 search_for 失效时的
    高亮兜底：把单字 span 平铺成连续字符串后手工定位词项，按行合并字符框。
    仅在「绝大多数 span 不超过 2 字」时启用，普通文本层不会进入此路径。"""
    try:
        d = page.get_text("dict")
    except Exception:
        return []
    entries: list[tuple[str, fitz.Rect]] = []
    for blk in d.get("blocks", []):
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                t = re.sub(r"\s+", "", sp.get("text") or "")
                if not t:
                    continue
                entries.append((t, fitz.Rect(sp["bbox"])))
    if len(entries) < 30:
        return []
    if sum(1 for t, _ in entries if len(t) <= 2) < len(entries) * 0.8:
        return []
    flat = ""
    offsets: list[int] = []
    for t, _ in entries:
        offsets.append(len(flat))
        flat += t
    rects: list = []
    for term in terms:
        compact = term.replace(" ", "")
        if len(compact) < 2:
            continue
        start = flat.find(compact)
        while start != -1 and len(rects) < 12:
            end = start + len(compact)
            cur = None
            for (t, r), off in zip(entries, offsets):
                if off + len(t) <= start or off >= end:
                    continue
                if cur is not None and abs(r.y0 - cur.y0) < max(r.height, cur.height) * 0.6:
                    cur |= r
                else:
                    if cur is not None:
                        rects.append(cur)
                    cur = fitz.Rect(r)
            if cur is not None:
                rects.append(cur)
            start = flat.find(compact, end)
        if rects:
            break
    return rects[:12]


def _anchored_highlight_rects(page, query_text: str, *, max_rects: int = 80, max_occurrences: int = 8) -> list:
    """在页面「字符级文本框」上用归一化逐字锚定来定位高亮区域，按行合并为矩形。

    引文检索是在语料的 normalized_text（NFKC + 去掉所有标点与空白）上命中的；这里对页面
    同样做归一化逐字索引，命中串即可在页面字符序列里**整体**定位——天然跨行，且不受空格、
    标点、换行差异影响。这正是修复点：page.search_for 把整句（尤其含空格的长句）当一个
    连续子串去找，跨行/有空格时常匹配失败，于是退化为只高亮某个短片段、甚至完全不高亮。

    返回的矩形按「同一行的连续命中字符」合并。整串若不连续（如同段多词共现），再退化为
    逐词分别锚定，使每个关键词各自标亮。
    """
    target = " ".join((query_text or "").split())
    if not target:
        return []
    try:
        raw = page.get_text("rawdict")
    except Exception:
        return []
    flat: list[str] = []
    bboxes: list = []
    for blk in raw.get("blocks", []):
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                for ch in sp.get("chars", []):
                    nch = normalize(ch.get("c") or "")
                    if not nch:
                        continue  # 标点/空白：归一化后为空，不参与定位也不产生框
                    bbox = ch.get("bbox")
                    for c in nch:  # NFKC 偶有一字展开多字，逐字复用同一字框
                        flat.append(c)
                        bboxes.append(bbox)
    if not flat:
        return []
    flat_str = "".join(flat)

    def _line_rects(start: int, end: int) -> list:
        out: list = []
        cur = None
        for bb in bboxes[start:end]:
            if bb is None:
                continue
            r = fitz.Rect(bb)
            if cur is not None and abs(r.y0 - cur.y0) < max(r.height, cur.height, 1.0) * 0.6:
                cur |= r  # 同一行：并入
            else:
                if cur is not None:
                    out.append(cur)
                cur = fitz.Rect(r)
        if cur is not None:
            out.append(cur)
        return out

    def _locate_all(needle: str) -> list:
        out: list = []
        if len(needle) < 2:
            return out
        i = flat_str.find(needle)
        n = 0
        while i != -1 and n < max_occurrences:
            out.extend(_line_rects(i, i + len(needle)))
            n += 1
            i = flat_str.find(needle, i + len(needle))
        return out

    rects = _locate_all(normalize(target))
    if not rects and " " in target:
        seen: set[str] = set()
        for word in target.split():
            wnorm = normalize(word)
            if len(wnorm) < 2 or wnorm in seen:
                continue
            seen.add(wnorm)
            rects.extend(_locate_all(wnorm))
    return rects[:max_rects]


def _clean_text(value: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", (value or "")).strip()
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _form_bool(name: str) -> bool:
    return _coerce_bool(request.form.get(name))


def _form_int(name: str, default: int) -> int:
    raw = request.form.get(name)
    if raw is None or raw == "":
        return int(default)
    return int(raw)


def _form_float(name: str, default: float) -> float:
    raw = request.form.get(name)
    if raw is None or raw == "":
        return float(default)
    return float(raw)


def _form_optional_int(name: str) -> int | None:
    raw = request.form.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    value = int(str(raw).strip())
    if value < 0:
        raise ValueError("AI token 限额不能小于 0。")
    return value


def _beijing_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


def _beijing_day_bounds() -> tuple[str, str, str, str]:
    now_bj = _beijing_now()
    start_bj = datetime.combine(now_bj.date(), datetime.min.time(), tzinfo=now_bj.tzinfo)
    end_bj = start_bj + timedelta(days=1)
    return (
        now_bj.date().isoformat(),
        start_bj.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_bj.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_bj.isoformat(timespec="seconds"),
    )


def _parse_dashboard_day(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw).date()
    except ValueError:
        return fallback
    return parsed.isoformat()


def _beijing_day_bounds_for(day: str) -> tuple[str, str, str, str]:
    fallback = _beijing_now().date().isoformat()
    day_value = _parse_dashboard_day(day, fallback)
    bj_tz = timezone(timedelta(hours=8))
    selected = datetime.fromisoformat(day_value).date()
    start_bj = datetime.combine(selected, datetime.min.time(), tzinfo=bj_tz)
    end_bj = start_bj + timedelta(days=1)
    return (
        day_value,
        start_bj.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_bj.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_bj.isoformat(timespec="seconds"),
    )


def _dashboard_date_range(start_day: str, end_day: str, *, max_days: int | None = None) -> list[str]:
    today = _beijing_now().date()
    try:
        start = datetime.fromisoformat(start_day).date()
    except ValueError:
        start = today - timedelta(days=DASHBOARD_DEFAULT_HISTORY_DAYS - 1)
    try:
        end = datetime.fromisoformat(end_day).date()
    except ValueError:
        end = today
    if start > end:
        start, end = end, start
    if max_days and (end - start).days + 1 > max_days:
        start = end - timedelta(days=max_days - 1)
    days: list[str] = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _dashboard_metrics_for_day(day: str, *, for_history: bool = False) -> dict:
    day_value, day_start, day_end, _ = _beijing_day_bounds_for(day)
    now_utc = datetime.now(timezone.utc)
    metrics = get_admin_dashboard_metrics(
        day=day_value,
        start_at=day_start,
        end_at=day_end,
        online_since=(now_utc - timedelta(seconds=ONLINE_WINDOW_SECONDS)).isoformat(timespec="seconds"),
        now_text=now_utc.isoformat(timespec="seconds"),
        high_token_threshold=DASHBOARD_HIGH_TOKEN_THRESHOLD,
        token_limit_ratio=DASHBOARD_TOKEN_LIMIT_RATIO,
    )
    metrics.update(
        {
            "day": day_value,
            "db_status": current_view_state().get("db_status") or "",
            "ai_ok": bool(current_view_state().get("ai_enabled")),
            "payment_ok": bool(current_view_state().get("payment_enabled")),
        }
    )
    if for_history and day_value != _beijing_now().date().isoformat():
        metrics["current_online"] = None
        metrics["pending_orders"] = None
        metrics["db_status"] = ""
        metrics["ai_ok"] = None
        metrics["payment_ok"] = None
    return metrics


def _dashboard_selected_history_range() -> tuple[str, str, list[str]]:
    today = _beijing_now().date().isoformat()
    default_start = (_beijing_now().date() - timedelta(days=DASHBOARD_DEFAULT_HISTORY_DAYS - 1)).isoformat()
    start_day = _parse_dashboard_day(request.args.get("history_start", ""), default_start)
    end_day = _parse_dashboard_day(request.args.get("history_end", ""), today)
    days = _dashboard_date_range(start_day, end_day, max_days=DASHBOARD_MAX_HISTORY_DAYS)
    return days[0], days[-1], days


def _dashboard_history_rows(days: list[str]) -> list[dict]:
    rows = [_dashboard_metrics_for_day(day, for_history=True) for day in days]
    return list(reversed(rows))


def _dashboard_rules() -> list[dict]:
    return [
        {
            "name": "日期口径",
            "rule": "所有“今日/当日”指标按北京时间自然日计算；后台查询时会换算为对应 UTC 起止时间。",
        },
        {
            "name": "注册用户总数",
            "rule": "users 表当前累计账号数；已验证、停用、活跃账号分别按邮箱验证时间、停用标记和 is_active 统计。",
        },
        {
            "name": "在线",
            "rule": f"当前在线为最近 {ONLINE_WINDOW_SECONDS // 60} 分钟有记录的去重访问者；登录用户按账号去重，未登录访客按浏览器会话去重，当日在线同口径按所选日期统计。同一浏览器登录前后会归并为同一访问者（按账号），不会重复计为「访客一次 + 注册一次」。注册用户在线、会员在线为当日上线的去重账号数（会员按当前仍有效的订阅判定）。IP 不作为主去重键，避免把同一单位或家庭的多人误合并。24 小时在线变化图按 15 分钟时槽统计，可在所有访问者 / 注册用户 / 会员之间切换。",
        },
        {
            "name": "会员与订单",
            "rule": "活跃会员按所选日期时点仍在有效期内的 active 订阅统计；当日付费按 paid_at 落在当日的已支付订单金额汇总。",
        },
        {
            "name": "搜索与阅读",
            "rule": "搜索、阅读器访问来自 site_activity 表，按会话、日期、功能聚合后的 request_count 求和。",
        },
        {
            "name": "阅读异常",
            "rule": "阅读异常按访客(账号优先、IP 次之)在当日 reader_access_events 审计中触发任一阈值计数：当日阅读请求≥300、书页图像≥200、单分钟峰值≥90、访问页面≥120、跨卷册≥8、连续翻页≥60、触发限速或疑似自动化 User-Agent。",
        },
        {
            "name": "AI token",
            "rule": "AI 请求数、错误数和 token 来自 ai_usage 表；token 由请求文本与返回文本估算或由调用记录写入后汇总。",
        },
        {
            "name": "高 token 用户",
            "rule": f"单个注册用户当日 token ≥ {DASHBOARD_HIGH_TOKEN_THRESHOLD}，或达到其每日限额的 {int(DASHBOARD_TOKEN_LIMIT_RATIO * 100)}%，会进入高用量名单。",
        },
        {
            "name": "期刊指标",
            "rule": "期刊订阅、待审文章、待发送文章为当前队列状态；当日发送按 journal_delivery_logs 的 created_at 落在当日统计。",
        },
        {
            "name": "实时状态",
            "rule": "数据库、AI、支付状态是页面打开时的实时健康状态；历史表中过去日期不重复填充这些实时状态。",
        },
    ]


def _xlsx_col_name(index: int) -> str:
    name = ""
    value = int(index)
    while value:
        value, remainder = divmod(value - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_cell(value: object, row_index: int, col_index: int) -> str:
    ref = f"{_xlsx_col_name(col_index)}{row_index}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = html.escape(str(value), quote=True)
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _xlsx_sheet_xml(rows: list[list[object]]) -> str:
    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(_xlsx_cell(value, row_index, col_index) for col_index, value in enumerate(row, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )


def _build_xlsx(sheets: list[tuple[str, list[list[object]]]]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(
                f'<sheet name="{html.escape(name[:31], quote=True)}" sheetId="{i}" r:id="rId{i}"/>'
                for i, (name, _) in enumerate(sheets, start=1)
            )
            + "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + "</Relationships>",
        )
        for index, (_, rows) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _xlsx_sheet_xml(rows))
    buffer.seek(0)
    return buffer.getvalue()


def _dashboard_history_sheet_rows(history_rows: list[dict]) -> list[list[object]]:
    rows: list[list[object]] = [[
        "日期",
        "当前在线",
        "当日在线",
        "注册用户总数",
        "活跃账号",
        "已验证用户",
        "停用账号",
        "新注册",
        "活跃会员",
        "当日付费(分)",
        "待支付订单",
        "支付异常",
        "当日搜索",
        "阅读器访问",
        "定价页访问",
        "期刊卡进套餐",
        "阅读异常",
        "AI请求",
        "AI token",
        "智谱请求",
        "智谱token",
        "AI错误",
        "高Token用户数",
        "期刊订阅",
        "待审文章",
        "待发送文章",
        "当日发送",
        "数据库状态",
        "AI状态",
        "支付状态",
    ]]
    for item in reversed(history_rows):
        rows.append([
            item.get("day"),
            item.get("current_online"),
            item.get("today_online"),
            item.get("total_users"),
            item.get("active_accounts"),
            item.get("verified_users"),
            item.get("disabled_users"),
            item.get("new_users_today"),
            item.get("active_members"),
            item.get("paid_today_cents"),
            item.get("pending_orders"),
            item.get("payment_errors_today"),
            item.get("searches_today"),
            item.get("reader_views_today"),
            item.get("pricing_views_today"),
            item.get("pricing_from_journal_today"),
            item.get("reader_anomaly_count"),
            item.get("ai_requests_today"),
            item.get("ai_tokens_today"),
            item.get("zhipu_requests_today"),
            item.get("zhipu_tokens_today"),
            item.get("ai_errors_today"),
            item.get("high_token_user_count"),
            item.get("journal_subscriptions"),
            item.get("journal_pending_articles"),
            item.get("journal_ready_articles"),
            item.get("journal_recent_sends"),
            item.get("db_status"),
            "" if item.get("ai_ok") is None else ("正常" if item.get("ai_ok") else "未启用"),
            "" if item.get("payment_ok") is None else ("正常" if item.get("payment_ok") else "未启用"),
        ])
    return rows


def _dashboard_token_sheet_rows(history_rows: list[dict]) -> list[list[object]]:
    rows: list[list[object]] = [[
        "日期",
        "用户ID",
        "邮箱",
        "显示名",
        "套餐",
        "请求数",
        "总Token",
        "单次最高Token",
        "错误数",
        "每日限额",
        "限额占比",
        "异常原因",
        "首次使用",
        "最后使用",
    ]]
    for item in reversed(history_rows):
        for user in item.get("high_token_users") or []:
            rows.append([
                item.get("day"),
                user.get("user_id"),
                user.get("email"),
                user.get("display_name"),
                user.get("plan_name") or "",
                user.get("request_count"),
                user.get("total_tokens"),
                user.get("max_request_tokens"),
                user.get("error_count"),
                user.get("effective_limit"),
                user.get("limit_ratio"),
                user.get("alert_reason"),
                _display_datetime(user.get("first_used_at") or ""),
                _display_datetime(user.get("last_used_at") or ""),
            ])
    if len(rows) == 1:
        rows.append(["所选日期范围内暂无高 token 用户"])
    return rows


def _dashboard_rules_sheet_rows() -> list[list[object]]:
    rows: list[list[object]] = [["指标", "计算规则"]]
    for rule in _dashboard_rules():
        rows.append([rule["name"], rule["rule"]])
    return rows


def _visitor_session_key() -> str:
    key = str(session.get("_visitor_key") or "").strip()
    if not key:
        key = secrets.token_urlsafe(24)
        session["_visitor_key"] = key
    return key


def _request_presented_session_cookie() -> bool:
    """请求是否携带了既有的会话 cookie。用于区分「保留 cookie 的真实回访浏览器」与
    「每个请求都换一个新 cookie 的脚本（或访客落地页的首个请求）」。读的是客户端实际发来的
    原始 cookie，不受本请求内对 session 的写入影响。"""
    name = app.config.get("SESSION_COOKIE_NAME") or "session"
    return bool((request.cookies.get(name) or "").strip())


def _online_presence_dedup_key(session_key: str) -> str:
    """「24 小时在线变化」在线人数的去重键。登录用户在查询层按 user_id 去重、键值无关紧要；
    匿名访客若回传了会话 cookie（真实回访浏览器，对校园 NAT 友好）按 cookie 去重，否则
    （不收 cookie 的脚本／访客落地页首个请求）按真实 IP 去重——这样「每请求换一个 cookie」的
    爬虫会塌缩到其少数几个出口 IP，不再把在线人数刷高失真。代价：极少数禁用 cookie 的匿名
    读者会与同 IP 其他访客合并计为一人（远小于爬虫刷高的失真，可接受）。"""
    if _request_presented_session_cookie():
        return session_key or "anonymous"
    ip = _client_ip()
    return f"ip:{ip}" if ip and ip != "unknown" else (session_key or "anonymous")


def _reset_session_preserving_visitor() -> None:
    """登录/注册成功时清空会话以防会话固定，但保留访客分析标识 _visitor_key。

    _visitor_key 仅是无权限的统计令牌，鉴权完全依赖随后写入的 session["user_id"]，
    保留它不会削弱防会话固定。保留后，同一浏览器登录前后归并为同一访问者，避免
    在线/当日在线把同一个人重复计为「访客一次 + 注册一次」。
    """
    visitor_key = str(session.get("_visitor_key") or "").strip()
    session.clear()
    if visitor_key:
        session["_visitor_key"] = visitor_key


def _activity_feature_for_request() -> str | None:
    endpoint = str(request.endpoint or "")
    if not endpoint or endpoint == "static":
        return None
    if endpoint in {"page_image", "api_ping", "api_shutdown"}:
        return None
    if endpoint == "api_search":
        return "search"
    if endpoint in {"reader", "library", "pdf_viewer", "serve_pdf"}:
        return "reader"
    if endpoint.startswith("api_ai_"):
        return "ai"
    if endpoint == "pricing":
        # 定价页访问单独计数；首页期刊卡入口带 from=journal，与顶部导航等其他来源
        # 区分开，用于观察「期刊卡 → 套餐页」这条转化路径的真实点击量。
        return "pricing_journal" if (request.args.get("from") or "").strip() == "journal" else "pricing"
    return "site"


def _is_reader_audit_endpoint() -> bool:
    return str(request.endpoint or "") in READER_ENDPOINTS


def _reader_audit_payload(*, is_rate_limited: bool = False) -> dict:
    user = getattr(g, "current_user", None)
    return {
        "session_key": _visitor_session_key(),
        "user_id": int(user["id"]) if user else None,
        "email": str(user.get("email") or "") if user else "",
        "client_ip": _client_ip(),
        "user_agent": str(request.headers.get("User-Agent") or ""),
        "endpoint": str(request.endpoint or ""),
        "method": str(request.method or ""),
        "path": request.full_path[:500] if request.query_string else request.path,
        "reader_mode": (request.args.get("mode") or "").strip(),
        "source_file": (request.args.get("file") or "").strip(),
        "page": max(0, request.args.get("page", type=int) or 0),
        "is_rate_limited": is_rate_limited,
        "day": china_day_text(),
    }


# 尽力而为的记账写，按 kind 分派到对应的落库函数(均接受关键字 payload)。
_ASYNC_AUDIT_WRITERS: dict = {
    "reader": lambda p: record_reader_access_event(**p),
    "activity": lambda p: record_site_activity(**p),
    "online": lambda p: record_online_presence(**p),
    "capture_ip": lambda p: capture_user_ip_if_missing(**p),
}

# 「登录态活跃即补 IP」：进程内按 user_id 去重，每用户每进程至多投递一次「补 IP」（落库为
# 只补两 IP 字段都空者的条件 UPDATE，幂等）。给「记 IP」上线前活跃、又用持久会话不重登的老用户补归属地。
_ip_capture_seen: set[int] = set()
_ip_capture_seen_lock = threading.Lock()


def _async_audit_writer_loop() -> None:
    # 单个后台守护线程串行落库：与请求线程零竞争，被拒匿名洪峰不再挤 SQLite 写锁。
    while True:
        try:
            kind, payload = _async_audit_queue.get()
        except Exception:
            continue
        writer = _ASYNC_AUDIT_WRITERS.get(kind)
        try:
            if writer is not None:
                writer(payload)
        except Exception as exc:
            LOGGER.debug("Async audit write (%s) failed: %s", kind, exc)
        finally:
            _async_audit_queue.task_done()


def _ensure_async_audit_writer() -> None:
    if _async_audit_writer_started.is_set():
        return
    with _async_audit_writer_lock:
        if _async_audit_writer_started.is_set():
            return
        threading.Thread(
            target=_async_audit_writer_loop,
            name="async-audit-writer",
            daemon=True,
        ).start()
        _async_audit_writer_started.set()


def _record_async_audit_drop() -> None:
    _async_audit_dropped[0] += 1
    now = time.time()
    if now - _async_audit_last_drop_warn[0] >= ASYNC_AUDIT_DROP_WARN_INTERVAL_SECONDS:
        _async_audit_last_drop_warn[0] = now
        LOGGER.warning(
            "Async audit queue full; dropped %d best-effort audit writes so far.",
            _async_audit_dropped[0],
        )


def _enqueue_audit_write(kind: str, payload: dict) -> None:
    """把一条尽力而为的审计/记账写投递到后台单写线程。

    生产态请求线程只做 O(1) 入队、不碰 SQLite；测试态(app.testing)直接同步写，
    保证既有「请求后立即查库断言」成立。队列触顶则丢弃并限频告警。"""
    writer = _ASYNC_AUDIT_WRITERS.get(kind)
    if writer is None:
        return
    if app.testing:
        try:
            writer(payload)
        except Exception as exc:
            LOGGER.debug("Sync audit write (%s) failed: %s", kind, exc)
        return
    _ensure_async_audit_writer()
    try:
        _async_audit_queue.put_nowait((kind, payload))
    except queue.Full:
        _record_async_audit_drop()


def _capture_current_user_ip(user) -> None:
    """登录态用户：进程内首次见到该用户时，投递一次「补 IP」（只补尚无任何 IP 者）。绝不阻塞请求：
    去重命中即 O(1) 返回；拿不到真实 IP 时不占用去重名额、留待下次。"""
    if not user:
        return
    try:
        uid = int(user["id"])
    except (KeyError, TypeError, ValueError):
        return
    if uid in _ip_capture_seen:
        return
    ip = _client_ip()
    if not ip or ip == "unknown":
        return
    with _ip_capture_seen_lock:
        if uid in _ip_capture_seen:
            return
        _ip_capture_seen.add(uid)
    _enqueue_audit_write("capture_ip", {"user_id": uid, "ip": ip})


def _record_reader_access_event(*, is_rate_limited: bool = False) -> None:
    if not _is_reader_audit_endpoint():
        return
    try:
        payload = _reader_audit_payload(is_rate_limited=is_rate_limited)
    except Exception as exc:
        LOGGER.debug("Reader access payload build failed: %s", exc)
        return
    _enqueue_audit_write("reader", payload)


def _prune_reader_audit_if_due() -> None:
    now = time.time()
    if now - _last_reader_audit_prune[0] < READER_AUDIT_PRUNE_INTERVAL_SECONDS:
        return
    _last_reader_audit_prune[0] = now
    try:
        prune_reader_access_events(keep_days=READER_AUDIT_KEEP_DAYS)
    except Exception as exc:
        LOGGER.debug("Reader access pruning failed: %s", exc)


def _prune_online_presence_if_due() -> None:
    now = time.time()
    if now - _last_online_presence_prune[0] < ONLINE_PRESENCE_PRUNE_INTERVAL_SECONDS:
        return
    _last_online_presence_prune[0] = now
    try:
        prune_online_presence(keep_hours=ONLINE_PRESENCE_KEEP_HOURS)
    except Exception as exc:
        LOGGER.debug("Online presence pruning failed: %s", exc)


def _is_public_ip(ip: str) -> bool:
    """是否为真实公网 IP(排除回环/内网/链路本地/保留地址与 unknown)。"""
    try:
        return ipaddress.ip_address((ip or "").strip()).is_global
    except ValueError:
        return False


def _reader_auto_ban_config() -> dict:
    """自动封禁配置：默认开启+保守阈值；设置 reader_auto_ban 与 env 可覆盖/关闭。"""
    enabled = not _env_flag("DISABLE_READER_AUTO_BAN", False)
    daily = READER_AUTO_BAN_DAILY_MIN
    minute = READER_AUTO_BAN_MINUTE_MIN
    pool_ip = READER_AUTO_BAN_POOL_IP_MIN
    pool_request = READER_AUTO_BAN_POOL_REQUEST_MIN
    pool_path = READER_AUTO_BAN_POOL_PATH_MIN
    pool_window = READER_AUTO_BAN_POOL_WINDOW_MINUTES
    pool_limit = READER_AUTO_BAN_POOL_LIMIT
    try:
        payload = get_setting("reader_auto_ban", {})
        if isinstance(payload, dict):
            if "enabled" in payload:
                enabled = bool(payload.get("enabled"))
            daily = int(payload.get("daily_min") or daily)
            minute = int(payload.get("minute_min") or minute)
            pool_ip = int(payload.get("pool_ip_min") or pool_ip)
            pool_request = int(payload.get("pool_request_min") or pool_request)
            pool_path = int(payload.get("pool_path_min") or pool_path)
            pool_window = int(payload.get("pool_window_minutes") or pool_window)
            pool_limit = int(payload.get("pool_limit") or pool_limit)
    except Exception:
        pass
    try:
        daily = int(os.environ.get("READER_AUTO_BAN_DAILY_MIN") or daily)
        minute = int(os.environ.get("READER_AUTO_BAN_MINUTE_MIN") or minute)
        pool_ip = int(os.environ.get("READER_AUTO_BAN_POOL_IP_MIN") or pool_ip)
        pool_request = int(os.environ.get("READER_AUTO_BAN_POOL_REQUEST_MIN") or pool_request)
        pool_path = int(os.environ.get("READER_AUTO_BAN_POOL_PATH_MIN") or pool_path)
        pool_window = int(os.environ.get("READER_AUTO_BAN_POOL_WINDOW_MINUTES") or pool_window)
        pool_limit = int(os.environ.get("READER_AUTO_BAN_POOL_LIMIT") or pool_limit)
    except ValueError:
        pass
    return {
        "enabled": enabled,
        "daily_min": max(1, daily),
        "minute_min": max(1, minute),
        "pool_ip_min": max(2, pool_ip),
        "pool_request_min": max(2, pool_request),
        "pool_path_min": max(1, pool_path),
        "pool_window_minutes": min(60, max(1, pool_window)),
        "pool_limit": max(1, pool_limit),
    }


def _alert_admin_auto_ban(items: list) -> None:
    # 反爬自动封禁后给管理员发一封告警邮件。被封 IP 本就是首次新封（已封的在扫描里被跳过），
    # 天然去重，无需额外记账。SMTP 未配置则静默跳过；发送失败不影响封禁本身。
    if not items or not _account_email_configured():
        return
    to_email = (os.environ.get("SECURITY_ALERT_EMAIL") or "").strip() or FEEDBACK_ADMIN_EMAIL
    if not to_email:
        return
    lines = []
    for it in items:
        peak = int(it.get("max_minute_requests") or it.get("pool_request_count") or 0)
        lines.append(
            f"- IP {it['ip']}：当日 {it.get('request_count', 0)} 次、单分钟峰值 {peak}；{it.get('reason', '')}"
        )
    body = (
        "管理员您好：\n\n"
        f"网站反爬系统刚刚自动封禁了 {len(items)} 个疑似扒站 IP：\n\n"
        + "\n".join(lines)
        + "\n\n如系误判，可在后台「阅读异常」处解封，或调整 reader_auto_ban 阈值。\n"
        + f"后台：{_feedback_public_base_url()}/admin"
    )
    try:
        _send_account_email(to_email, "网站反爬自动封禁告警", body)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to send auto-ban alert email: %s", exc)


def _auto_ban_egregious_scrapers_if_due() -> None:
    """保守自动封禁极端真实 IP 扒站者(按时间节流)。仅封公网 IP actor、双高阈值，
    永不封登录会员/内网/监控;写入 reader_bans["ips"] 并记 management_action 日志。"""
    now = time.time()
    if now - _last_reader_auto_ban[0] < READER_AUTO_BAN_INTERVAL_SECONDS:
        return
    _last_reader_auto_ban[0] = now
    try:
        config = _reader_auto_ban_config()
        if not config["enabled"]:
            return
        anomalies = list_reader_anomaly_visitors(day=china_day_text(), limit=50)
        if not anomalies:
            anomalies = []
        exempt_ips = set(_monitoring_exemptions().get("ips") or [])
        bans = _reader_bans()
        ip_bans = _reader_ip_bans(bans)
        changed = False
        newly_banned: list[dict] = []
        for item in anomalies:
            if str(item.get("actor_type") or "") != "ip":
                continue  # 永不自动封登录会员(user:)与会话(session:)
            ip = str(item.get("client_ip") or "").strip()
            if not ip or ip in ip_bans or ip in exempt_ips or not _is_public_ip(ip):
                continue
            if int(item.get("request_count") or 0) < config["daily_min"]:
                continue
            if int(item.get("max_minute_requests") or 0) < config["minute_min"]:
                continue
            ip_bans[ip] = {"banned_at": utc_now_text(), "banned_by": "auto-anticrawl"}
            changed = True
            newly_banned.append(
                {
                    "ip": ip,
                    "request_count": int(item.get("request_count") or 0),
                    "max_minute_requests": int(item.get("max_minute_requests") or 0),
                    "reason": str(item.get("alert_reason") or "")[:200],
                }
            )
            LOGGER.info(
                "management_action %s",
                json.dumps(
                    {
                        "scope": "system",
                        "actor": "auto-anticrawl",
                        "action": "reader_access.toggle",
                        "target": ip,
                        "result": "ban",
                        "details": {
                            "request_count": int(item.get("request_count") or 0),
                            "max_minute_requests": int(item.get("max_minute_requests") or 0),
                            "reason": str(item.get("alert_reason") or "")[:200],
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        pool_candidates = list_reader_ip_pool_burst_candidates(
            day=china_day_text(),
            ip_min=int(config["pool_ip_min"]),
            request_min=int(config["pool_request_min"]),
            path_min=int(config["pool_path_min"]),
            window_minutes=int(config["pool_window_minutes"]),
            limit=int(config["pool_limit"]),
        )
        for item in pool_candidates:
            ip = str(item.get("client_ip") or "").strip()
            if not ip or ip in ip_bans or ip in exempt_ips or not _is_public_ip(ip):
                continue
            pool_ip_count = int(item.get("pool_ip_count") or 0)
            pool_request_count = int(item.get("pool_request_count") or 0)
            pool_path_count = int(item.get("pool_path_count") or 0)
            window_minutes = int(item.get("window_minutes") or config["pool_window_minutes"])
            reason = (
                f"IP 池突发：同 UA {window_minutes} 分钟内 {pool_ip_count} 个 IP、"
                f"{pool_request_count} 次请求、{pool_path_count} 个路径"
            )
            ip_bans[ip] = {
                "banned_at": utc_now_text(),
                "banned_by": "auto-anticrawl",
                "reason": "ip_pool_burst",
            }
            changed = True
            newly_banned.append(
                {
                    "ip": ip,
                    "request_count": int(item.get("request_count") or 0),
                    "max_minute_requests": pool_request_count,
                    "pool_request_count": pool_request_count,
                    "reason": reason[:200],
                }
            )
            LOGGER.info(
                "management_action %s",
                json.dumps(
                    {
                        "scope": "system",
                        "actor": "auto-anticrawl",
                        "action": "reader_access.toggle",
                        "target": ip,
                        "result": "ban",
                        "details": {
                            "pool_ip_count": pool_ip_count,
                            "pool_request_count": pool_request_count,
                            "pool_path_count": pool_path_count,
                            "window_start": str(item.get("window_start") or ""),
                            "window_minutes": window_minutes,
                            "reason": reason[:200],
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        if changed:
            # 直接以固定 actor 落库(不经 _save_reader_bans 的 _management_actor_label，
            # 后者依赖请求上下文且会把自动封禁误记到扒站者头上)。
            set_setting("reader_bans", bans, updated_by="auto-anticrawl")
            _alert_admin_auto_ban(newly_banned)
    except Exception as exc:
        LOGGER.debug("Auto-ban scan failed: %s", exc)


def _env_csv(name: str) -> list[str]:
    raw = str(os.environ.get(name) or "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


# 站点自带监控程序的默认豁免信号（无需后台/环境配置即生效）：
# - 两个专用监控账号（会员/非会员腿，登录态身份不可伪造，最稳）；
# - 监控浏览器自报的 UA 密钥子串（访客腿；监控 Playwright 上下文统一带 MazhuMonitor/x）。
# 仍可通过后台 monitoring_exemptions 设置或 MONITORING_* 环境变量追加更多信号。
_DEFAULT_MONITORING_EMAILS = ("1010851067@qq.com", "18954389936@163.com")
_DEFAULT_MONITORING_UA_TOKENS = ("mazhumonitor",)


def _monitoring_exemptions() -> dict:
    """巡检/监控程序豁免名单。

    命中者发起的请求**完全不计入**站点活动(site_activity → 阅读器访问/在线)与
    阅读器审计(reader_access_events → 异常),避免合成监控污染后台总览,也不会把
    监控误判为异常访客而封禁。

    四类信号(任一命中即豁免)：
    - `emails` / `user_ids`：登录态的监控账号身份。**最稳且不可伪造**(无凭证无法冒充),
      用于监控的「会员号 / 非会员号」两条腿。
    - `user_agents`：User-Agent 子串(大小写不敏感)。用于「访客」腿;可被伪造,**当密钥用**。
    - `ips`：精确来源 IP。适用于固定出口 IP 的监控(访客腿兜底)。

    配置来源合并：后台设置 `monitoring_exemptions`(可热更新,键 emails/user_ids/
    user_agents/ips)+ 环境变量 `MONITORING_EMAILS` / `MONITORING_USER_AGENTS` /
    `MONITORING_IPS`(逗号分隔，便于服务器 env 引导)。
    """
    ua_tokens: list[str] = list(_DEFAULT_MONITORING_UA_TOKENS)
    ips: list[str] = []
    emails: list[str] = list(_DEFAULT_MONITORING_EMAILS)
    user_ids: list[str] = []
    payload = get_setting("monitoring_exemptions", {})
    if isinstance(payload, dict):
        ua_tokens.extend(str(x).strip() for x in (payload.get("user_agents") or []) if str(x).strip())
        ips.extend(str(x).strip() for x in (payload.get("ips") or []) if str(x).strip())
        emails.extend(str(x).strip() for x in (payload.get("emails") or []) if str(x).strip())
        user_ids.extend(str(x).strip() for x in (payload.get("user_ids") or []) if str(x).strip())
    ua_tokens.extend(_env_csv("MONITORING_USER_AGENTS"))
    ips.extend(_env_csv("MONITORING_IPS"))
    emails.extend(_env_csv("MONITORING_EMAILS"))
    user_ids.extend(_env_csv("MONITORING_USER_IDS"))
    return {
        "user_agents": [t.lower() for t in ua_tokens],
        "ips": set(ips),
        "emails": {e.lower() for e in emails},
        "user_ids": {u for u in user_ids},
    }


def _compute_is_monitoring_request() -> bool:
    try:
        config = _monitoring_exemptions()
    except Exception:
        return False
    if not (config["user_agents"] or config["ips"] or config["emails"] or config["user_ids"]):
        return False
    # 登录态身份优先：不可伪造，覆盖监控的会员/非会员两条腿。
    user = getattr(g, "current_user", None)
    if user:
        email = str(user.get("email") or "").strip().lower()
        if email and email in config["emails"]:
            return True
        if str(user.get("id")) in config["user_ids"]:
            return True
    # 访客腿：UA 子串(当密钥) 或 固定来源 IP。
    ua = str(request.headers.get("User-Agent") or "").lower()
    if ua and any(token in ua for token in config["user_agents"]):
        return True
    if config["ips"] and _client_ip() in config["ips"]:
        return True
    return False


def _is_monitoring_request() -> bool:
    """当前请求是否来自已豁免的监控程序(每请求只计算一次，缓存在 g 上)。"""
    cached = getattr(g, "_monitoring_request", None)
    if cached is not None:
        return bool(cached)
    result = _compute_is_monitoring_request()
    try:
        g._monitoring_request = result
    except Exception:
        pass
    return result


# 已知 AI 训练/采集类爬虫的 User-Agent 子串(小写)。这些机器人会自报家门，
# 对阅读/取书端点命中即 403——它们对本站(付费版权内容)无正当用途。
# 经线上审计确认 GPTBot 单日抓取上万次 /viewer，是「阅读器访问」爆表的主因。
# 注意：仅列 AI 训练/采集与激进 SEO 抓取，不含 Googlebot/Bingbot 等正常搜索索引。
_DEFAULT_BLOCKED_BOT_UA = (
    "gptbot", "oai-searchbot", "chatgpt-user",
    "claudebot", "claude-web", "anthropic-ai",
    "ccbot", "bytespider", "amazonbot", "google-extended",
    "perplexitybot", "perplexity-ai", "diffbot", "imagesiftbot",
    "omgili", "omgilibot", "dataforseobot", "applebot-extended",
    "meta-externalagent", "meta-externalfetcher", "facebookbot",
    "cohere-ai", "youbot", "petalbot", "timpibot", "scrapy",
)

# 通用脚本/HTTP 客户端 UA(非浏览器)。阅读/取书端点是供人浏览的，真人浏览器
# 与 Playwright 监控(HeadlessChrome)都不会带这些；命中即视为脚本扒站并 403。
# 刻意只列明确的脚本客户端，不含 chrome/mozilla/headlesschrome 以免误伤真人与监控。
_DEFAULT_BLOCKED_AUTOMATION_UA = (
    "curl/", "wget/", "python-requests", "python-urllib", "aiohttp", "httpx/",
    "go-http-client", "java/", "okhttp", "libwww-perl", "lwp::", "node-fetch",
    "axios/", "guzzlehttp", "winhttp", "apache-httpclient", "httpclient",
    "mechanize", "postmanruntime", "insomnia", "httrack", "wpull", "colly",
)


def _blocked_bot_ua_tokens() -> tuple[str, ...]:
    """已封禁的爬虫 UA 子串：内置默认 + 后台设置 `blocked_bot_user_agents`(热更新)
    + 环境变量 `BLOCKED_BOT_USER_AGENTS`(逗号分隔)。每请求缓存在 g 上。"""
    cached = getattr(g, "_blocked_bot_ua", None)
    if cached is not None:
        return cached
    tokens = list(_DEFAULT_BLOCKED_BOT_UA) + list(_DEFAULT_BLOCKED_AUTOMATION_UA)
    try:
        payload = get_setting("blocked_bot_user_agents", None)
        if isinstance(payload, list):
            tokens.extend(str(x).strip().lower() for x in payload if str(x).strip())
    except Exception:
        pass
    tokens.extend(t.lower() for t in _env_csv("BLOCKED_BOT_USER_AGENTS"))
    result = tuple(dict.fromkeys(t for t in tokens if t))
    try:
        g._blocked_bot_ua = result
    except Exception:
        pass
    return result


def _is_blocked_bot_request() -> bool:
    """当前请求是否来自已封禁的 AI 爬虫(按 User-Agent 子串，大小写不敏感)。
    紧急情况下可设环境变量 DISABLE_BOT_UA_BLOCK=1 整体关闭。"""
    if _env_flag("DISABLE_BOT_UA_BLOCK", False):
        return False
    ua = str(request.headers.get("User-Agent") or "").strip().lower()
    # 阅读端点的人类访问必带浏览器 UA；空 UA 视为脚本/爬虫直接拦(仅作用于阅读端点)。
    if not ua:
        return True
    return any(token in ua for token in _blocked_bot_ua_tokens())


def _estimate_tokens_from_text(*parts: object) -> int:
    chunks: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            chunks.append(part)
        else:
            try:
                chunks.append(json.dumps(part, ensure_ascii=False))
            except TypeError:
                chunks.append(str(part))
    text = "\n".join(chunks).strip()
    if not text:
        return 0
    return max(1, (len(text) + 1) // 2)


class _AIQuotaExceeded(Exception):
    def __init__(self, *, used: int, limit: int, reset_at: str, message: str = "本周 AI token 额度已用完，下周一恢复。"):
        super().__init__(message)
        self.used = used
        self.limit = limit
        self.reset_at = reset_at


def _require_ai_quota_or_raise(credit_kind: str = "") -> dict:
    """AI token 闸门（弹性周额度）：硬上限是「本周累计＜每日×系数」，每日额度仅作配速，
    允许某日集中借用本周池（防研究综述被截断）。``credit_kind`` 非空（'research'/'chat'/'reader'）时，
    若本周额度已用尽但持有对应「资源包」次数，则放行并标记 over_free_limit=True，调用方成功后扣 1 次。
    （'reader' = 阅读器 AI 导学问答，仅 DeepSeek-V4-Pro 主通道传入，智谱联网通道不抵扣资源包次数。）
    """
    user = getattr(g, "current_user", None)
    session_key = _visitor_session_key()
    day, _, _, _ = _beijing_day_bounds()
    week = _research_quota_week_window()
    reset_at = week["reset_at"]
    # 有效额度：用户级 override > 套餐级 > 分档默认（含登录用户/访客）；管理员/桌面不限。
    eff = _effective_ai_limit_info(user)
    weekly_limit = eff["weekly_limit"]
    uid = int(user["id"]) if user else None
    # 「无限量基础服务」（马克思形象）不占额度：它自身本就不过这道闸，若还算进分母，就会把
    # 随心问/研究综述/导学的共用周额度吃掉——「不限量」就成了只对它自己成立。
    # 后台「重置本周 AI 额度」写下的标记在此生效：只统计标记时刻之后的用量＝已用归零。
    reset_since = _ai_token_quota_reset_at(user, eff["bucket"])
    today_used = get_ai_token_usage(
        day=day, user_id=uid, session_key=session_key, exclude_features=AI_QUOTA_EXEMPT_FEATURES,
        since_created_at=reset_since,
    )
    weekly_used = get_ai_token_usage_range(
        start_day=week["start_day"], end_day=week["end_day"], user_id=uid, session_key=session_key,
        exclude_features=AI_QUOTA_EXEMPT_FEATURES, since_created_at=reset_since,
    )
    over_free_limit = weekly_limit is not None and weekly_used >= int(weekly_limit)
    credit_kind = str(credit_kind or "").strip().lower()
    credit_balance = (
        get_ai_credit_balance(int(user["id"]), credit_kind)
        if user and credit_kind in {"research", "chat", "reader"} else 0
    )
    if over_free_limit and credit_balance <= 0:
        raise _AIQuotaExceeded(used=weekly_used, limit=int(weekly_limit), reset_at=reset_at)
    return {
        "day": day,
        "session_key": session_key,
        "used": today_used,
        "weekly_used": weekly_used,
        "over_free_limit": bool(over_free_limit),
        "credit_kind": credit_kind if credit_kind in {"research", "chat", "reader"} else "",
        "credit_balance": int(credit_balance),
        "limit": weekly_limit,
        "daily_limit": eff["daily_limit"],
        "weekly_limit": weekly_limit,
        "source": eff["source"],
        "plan_code": "",
        "plan_name": eff.get("label", ""),
        "user_override": None,
        "plan_limit": None,
        "bucket": eff["bucket"],
    }


def _ai_usage_context() -> dict:
    """不设限额的 AI 用量上下文（day + session_key），供「无限量」通道（如马克思形象）记账用。"""
    day, _, _, _ = _beijing_day_bounds()
    return {"day": day, "session_key": _visitor_session_key()}


def _consume_credit_if_paid(quota: dict, kind: str) -> None:
    """成功回答后，若本次是「免费每日额度已用尽、靠资源包放行」的付费使用，则扣 1 次对应次数。"""
    if not quota or not quota.get("over_free_limit"):
        return
    if str(quota.get("credit_kind") or "") != kind:
        return
    user = getattr(g, "current_user", None)
    if user:
        consume_ai_credit(int(user["id"]), kind, reason=f"consume:{kind}")


def _require_zhipu_quota_or_raise(quota: dict) -> None:
    """智谱通道的每日 token 子配额，按套餐分级（套餐管理可按套餐单设；未设＝跟随
    智能服务里的全局默认；任一层填 0＝不限）。

    智谱用量同时计入总配额与本子配额：总闸门照旧，这里只多一道针对高价通道的闸。
    超限仅挡智谱——用户切回 DeepSeek 即可继续使用。管理员豁免，便于线上验证。
    """
    user = getattr(g, "current_user", None)
    if _is_admin_user(user):
        return
    plan_limit = get_user_zhipu_limit(int(user["id"])) if user else None
    limit = int(AI_CONFIG.zhipu_daily_token_limit or 0) if plan_limit is None else int(plan_limit)
    if limit <= 0:
        return
    used = get_ai_token_usage(
        day=str(quota.get("day") or ""),
        user_id=int(user["id"]) if user else None,
        session_key=str(quota.get("session_key") or ""),
        provider="zhipu",
        exclude_features=AI_QUOTA_EXEMPT_FEATURES,  # 与总额度同口径（吉祥物走 DeepSeek，通常本就不在此列）
    )
    if used >= limit:
        _, _, _, reset_at = _beijing_day_bounds()
        raise _AIQuotaExceeded(
            used=used,
            limit=limit,
            reset_at=reset_at,
            message="今日智谱联网额度已用完，可切换 DeepSeek 模型继续使用，明日额度自动恢复。",
        )


def _ai_usage_source_ref(prompt_parts: tuple[object, ...]) -> str:
    """从提示片段中的页面上下文（dict）提取一个简短来源标识，便于后台辨认是哪一页的导学。"""
    for part in prompt_parts:
        if isinstance(part, dict):
            title = str(part.get("display_title") or "").strip()
            label = str(part.get("page_label") or "").strip()
            section = str(part.get("section_title") or "").strip()
            bits = [b for b in (title, (f"第{label}页" if label else ""), section) if b]
            if bits:
                return " · ".join(bits)
    return ""


def _record_ai_usage(
    quota: dict | None,
    *,
    feature: str,
    prompt_parts: tuple[object, ...] = (),
    completion_text: str = "",
    success: bool = True,
    error: str = "",
    provider: str = "",
    model: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    # 豁免的监控程序：其 AI 调用不计入 ai_usage(总览 AI 请求/token/高用量名单)。
    if has_request_context() and _is_monitoring_request():
        return
    user = getattr(g, "current_user", None)
    try:
        # 默认按文本估算；调用方可显式传入 token 数（如研究综述要把「注入的真实原文」计入输入，
        # 而 prompt_excerpt 仍只留检索词，不污染审计）。
        est_prompt = _estimate_tokens_from_text(*prompt_parts) if prompt_tokens is None else max(0, int(prompt_tokens))
        est_completion = (
            _estimate_tokens_from_text(completion_text) if completion_tokens is None else max(0, int(completion_tokens))
        )
        prompt_tokens, completion_tokens = est_prompt, est_completion
        if not success and completion_tokens == 0:
            completion_tokens = 0
        # 仅留存用户真实输入（问题与选中文本，即 prompt_parts 中的字符串项），不含系统
        # 拼装的页面上下文或历史消息，供后台核查异常用量时了解“到底问了什么”。
        user_input = "\n".join(
            part.strip() for part in prompt_parts if isinstance(part, str) and part.strip()
        )
        source_ref = _ai_usage_source_ref(prompt_parts)
        client_ip = _client_ip() if has_request_context() else ""
        if provider == "zhipu":
            usage_provider, usage_model = "zhipu", AI_CONFIG.zhipu_model
        else:
            usage_provider, usage_model = AI_CONFIG.provider, AI_CONFIG.model
        if model:
            usage_model = model  # 调用方指定的模型（如马克思形象固定 deepseek-v4-flash）
        record_ai_usage(
            user_id=int(user["id"]) if user else None,
            session_key=str((quota or {}).get("session_key") or _visitor_session_key()),
            day=str((quota or {}).get("day") or china_day_text()),
            feature=feature,
            provider=usage_provider,
            model=usage_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated=True,
            success=success,
            error=error,
            prompt_excerpt=user_input,
            client_ip=client_ip,
            source_ref=source_ref,
        )
    except Exception as exc:
        LOGGER.debug("AI usage recording failed: %s", exc)


def _ensure_csrf_token() -> str:
    token = str(session.get("_csrf_token") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    session["_csrf_token"] = token
    return token


def _request_csrf_token() -> str:
    return str(request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or "").strip()


def _require_csrf() -> None:
    expected = str(session.get("_csrf_token") or "").strip()
    actual = _request_csrf_token()
    if not expected or not actual or not secrets.compare_digest(expected, actual):
        abort(403, description="请求未通过 CSRF 校验，请刷新页面后重试。")


def _require_management_csrf() -> None:
    _require_csrf()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _client_ip() -> str:
    # 链路一(当前)：客户端 → Caddy(本机反代) → waitress → 本进程。Caddy 把直连客户端
    # 追加为 X-Forwarded-For 的**最右一项**(左侧可由客户端伪造，最右项由 Caddy 写入、
    # 不可伪造)，故取最右可信项作为真实 IP；与 ProxyFix(x_for=1) 取值一致。
    # 需配合 run_waitress 的 clear_untrusted_proxy_headers=False，否则 waitress 会清掉
    # 该头、导致所有访客 IP 恒为 127.0.0.1(反爬的 IP 维度因此全部失效)。
    #
    # 链路二(加挂 Cloudflare 橙云代理后)：客户端 → CF 边缘 → Caddy → waitress。此时
    # 直连 Caddy 的是 CF 边缘节点，Caddy 写入 XFF 最右项的会是 *CF 边缘 IP*，全体访客
    # 会塌缩成少数几个 CF IP，IP 维度反爬(限速/自动封禁/_is_public_ip)一次性失效。
    # CF 会把真实访客 IP 放进 `CF-Connecting-IP` 头，故**优先**读它。
    # 安全前提：源站防火墙须只放行 Cloudflare IP 段(见 deploy/Caddyfile.example 与
    # CLOUDFLARE_CUTOVER.md)，否则有人摸到源站 IP 直连 Caddy 可伪造该头。未上 CF 时
    # 该头不存在，自动回退到 XFF 最右项——故本函数对「上 CF 前 / 后 / DNS 切换过渡期」
    # 三种状态都给出正确的真实 IP，可在切换前先行上线、零行为变化。
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            return parts[-1]
    return (request.remote_addr or "unknown").strip() or "unknown"


def _prune_window(values: list[float], now: float, window_seconds: int) -> list[float]:
    cutoff = now - max(1, int(window_seconds))
    return [value for value in values if value >= cutoff]


def _prune_rate_buckets_if_due(now: float) -> None:
    """周期性清掉空/过期的限速桶，避免键随访客无限增长(8 线程下也安全)。"""
    if now - _last_rate_prune[0] < 300:
        return
    _last_rate_prune[0] = now
    for key in list(_rate_buckets.keys()):
        kept = [t for t in _rate_buckets.get(key, []) if t >= now - 3600]
        if kept:
            _rate_buckets[key] = kept
        else:
            _rate_buckets.pop(key, None)


def _rate_limit_or_abort(key: str, *, limit: int, window_seconds: int, message: str = "请求过于频繁，请稍后再试。") -> None:
    now = time.time()
    # 8 线程 waitress 下，桶的"读-改-写"加锁，消除竞态(计数偏差/丢更新)。
    with _rate_buckets_lock:
        _prune_rate_buckets_if_due(now)
        bucket = _prune_window(_rate_buckets.get(key, []), now, window_seconds)
        over_limit = len(bucket) >= max(1, int(limit))
        if not over_limit:
            bucket.append(now)
        _rate_buckets[key] = bucket
    if over_limit:
        retry_after = max(1, int(window_seconds - (now - bucket[0]))) if bucket else int(window_seconds)
        if request.path.startswith("/api/"):
            response = jsonify({"ok": False, "error": message})
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            abort(response)
        abort(429, description=message)


def _rate_limit_ai_or_abort() -> None:
    user = getattr(g, "current_user", None)
    if _is_admin_user(user):
        return
    actor = f"user:{user['id']}" if user else f"ip:{_client_ip()}"
    _rate_limit_or_abort(
        f"ai:{actor}",
        limit=RATE_LIMITS["ai_user"][0],
        window_seconds=RATE_LIMITS["ai_user"][1],
        message="AI 请求过于频繁，请稍后再试。",
    )


def _rate_limit_page_image_or_abort() -> None:
    # 书页图像防爬（P3）。管理员/已豁免监控/桌面授权不计；登录用户按 ID 计数，带会话 cookie 的
    # 匿名读者按浏览器会话 key 计数，避免对共享出口 IP（如校园 NAT）的正常读者误伤，阈值很宽松。
    user = getattr(g, "current_user", None)
    if _is_admin_user(user) or _is_monitoring_request() or _desktop_content_access_enabled():
        return
    if user:
        actor = f"user:{user['id']}"
    elif _request_presented_session_cookie():
        actor = f"sess:{_visitor_session_key()}"
    else:
        # 不收 cookie 的书页图请求：真实浏览器在加载阅读页 HTML 时已拿到会话 cookie，后续取图必
        # 然带 cookie；不带 cookie 还在批量取图的，几乎必为「每请求换一个 cookie」绕过单会话限速
        # 的脚本抓取。对其按真实 IP 单独严格限速，堵住该绕过路径（弥补 cookie 维度被架空）。
        _rate_limit_or_abort(
            f"pageimg:nocookie:ip:{_client_ip()}",
            limit=RATE_LIMITS["page_image_nocookie_ip"][0],
            window_seconds=RATE_LIMITS["page_image_nocookie_ip"][1],
            message="书页图像加载过于频繁，请稍后片刻再继续阅读。",
        )
        return
    _rate_limit_or_abort(
        f"pageimg:{actor}",
        limit=RATE_LIMITS["page_image"][0],
        window_seconds=RATE_LIMITS["page_image"][1],
        message="书页图像加载过于频繁，请稍后片刻再继续阅读。",
    )


def _reader_ip_rate(kind: str) -> tuple[int, int]:
    """阅读内容端点按 IP 限速的(阈值, 窗口秒)。可经 env 覆盖（"limit,window"）。"""
    env_name = {
        "view": "READER_VIEW_IP_RATE",
        "pageimg": "READER_PAGEIMG_IP_RATE",
        "journalpdf": "READER_JOURNALPDF_IP_RATE",
    }.get(kind, "")
    raw = str(os.environ.get(env_name) or "").strip() if env_name else ""
    if raw and "," in raw:
        try:
            limit_s, window_s = raw.split(",", 1)
            return max(1, int(limit_s.strip())), max(1, int(window_s.strip()))
        except ValueError:
            pass
    bucket = {"view": "reader_view_ip", "journalpdf": "reader_journalpdf_ip"}.get(kind, "reader_pageimg_ip")
    return RATE_LIMITS[bucket]


def _rate_limit_reader_ip_or_abort(kind: str) -> None:
    """按真实客户端 IP 的阅读内容端点限速(#2 真实 IP 透传后才有意义)。兜底丢 cookie 的
    单 IP 脚本与伪装浏览器 UA 的高频抓取。管理员与已豁免监控不计。"""
    user = getattr(g, "current_user", None)
    if _is_admin_user(user) or _is_monitoring_request():
        return
    limit, window = _reader_ip_rate(kind)
    _rate_limit_or_abort(
        f"readerip:{kind}:ip:{_client_ip()}",
        limit=limit,
        window_seconds=window,
        message="访问过于频繁，请稍后再试。",
    )


def _require_reader_not_banned_or_abort() -> None:
    if not _is_reader_audit_endpoint():
        return
    user = getattr(g, "current_user", None)
    if _is_admin_user(user) or _desktop_content_access_enabled():
        return
    bans = _reader_bans()
    if user:
        # 仅登录用户的「邮箱封禁」判定需要访问策略；匿名 /page-image（最高 QPS、bot 重灾区）不构建策略。
        policy = _load_access_policy()
        email = normalize_email(str(user.get("email") or ""))
        user_id = str(user.get("id") or "").strip()
        if email in _reader_blocked_emails(policy) or (user_id and user_id in _reader_user_bans(bans)):
            abort(403, description="当前账号的阅读器访问已暂停。")
        return
    ip = _client_ip()
    if ip in _reader_ip_bans(bans):
        abort(403, description="当前网络的阅读器访问已暂停。")


def _failure_keys(email: str) -> list[str]:
    keys = [f"ip:{_client_ip()}"]
    normalized = normalize_email(email)
    if normalized:
        keys.append(f"email:{normalized}")
    return keys


def _prune_login_failures_if_due(now: float) -> None:
    """周期清掉空/过期的登录失败桶（须在 _login_failures_lock 下调用），防撞库/枚举无限攒键。"""
    if now - _last_login_failures_prune[0] < 300:
        return
    _last_login_failures_prune[0] = now
    for key in list(_login_failures.keys()):
        kept = [t for t in _login_failures.get(key, []) if t >= now - LOGIN_FAILURE_WINDOW_SECONDS]
        if kept:
            _login_failures[key] = kept
        else:
            _login_failures.pop(key, None)


def _recent_failures(key: str) -> list[float]:
    now = time.time()
    with _login_failures_lock:
        _prune_login_failures_if_due(now)
        values = _prune_window(_login_failures.get(key, []), now, LOGIN_FAILURE_WINDOW_SECONDS)
        _login_failures[key] = values
        return list(values)  # 返回副本，避免调用方持有的列表被其它线程后续替换


def _login_failure_count(email: str) -> int:
    return max((len(_recent_failures(key)) for key in _failure_keys(email)), default=0)


def _record_login_failure(email: str) -> None:
    now = time.time()
    keys = _failure_keys(email)
    with _login_failures_lock:
        for key in keys:
            values = _prune_window(_login_failures.get(key, []), now, LOGIN_FAILURE_WINDOW_SECONDS)
            values.append(now)
            _login_failures[key] = values
    # 持久审计：内存计数重启即丢，这里结构化打日志，配合 journald 保留可事后回溯撞库。（放锁外：I/O 不进临界区。）
    try:
        LOGGER.info("auth_failure %s", json.dumps({"email": email, "ip": _client_ip()}, ensure_ascii=False))
    except Exception:
        pass


def _clear_login_failures(email: str) -> None:
    keys = _failure_keys(email)
    with _login_failures_lock:
        for key in keys:
            _login_failures.pop(key, None)


def _login_locked(email: str) -> bool:
    now = time.time()
    for key in _failure_keys(email):
        values = _recent_failures(key)
        if len(values) >= LOGIN_LOCK_THRESHOLD and now - values[-LOGIN_LOCK_THRESHOLD] <= LOGIN_LOCK_SECONDS:
            return True
    return False


def _turnstile_site_key() -> str:
    return str(os.environ.get("TURNSTILE_SITE_KEY") or "").strip()


def _turnstile_secret_key() -> str:
    return str(os.environ.get("TURNSTILE_SECRET_KEY") or "").strip()


def _turnstile_configured() -> bool:
    return _env_flag("TURNSTILE_ENABLED", True) and bool(_turnstile_site_key() and _turnstile_secret_key())


def _verify_turnstile_response(token: str) -> bool:
    if not _turnstile_configured():
        return True
    if not token:
        return False
    body = urllib.parse.urlencode(
        {
            "secret": _turnstile_secret_key(),
            "response": token,
            "remoteip": _client_ip(),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        LOGGER.warning("Turnstile verification failed to complete: %s", exc)
        return False
    return bool(payload.get("success"))


def _turnstile_template_context(required: bool) -> dict:
    site_key = _turnstile_site_key()
    configured = _turnstile_configured()
    return {
        "turnstile_required": bool(required and configured),
        "turnstile_site_key": site_key if configured else "",
        "turnstile_config_missing": bool(required and not configured and _env_flag("TURNSTILE_ENABLED", True)),
    }


def _validate_turnstile_if_required(errors: list[str], required: bool) -> None:
    if not required or not _turnstile_configured():
        return
    token = request.form.get("cf-turnstile-response") or ""
    if not _verify_turnstile_response(token):
        errors.append("人机验证未通过，请刷新页面后重试。")


def _validate_display_name(display_name: str, errors: list[str]) -> None:
    if not display_name:
        errors.append("请输入显示名称。")
        return
    if len(display_name) > DISPLAY_NAME_MAX_LEN:
        errors.append(f"显示名称不能超过 {DISPLAY_NAME_MAX_LEN} 个字符。")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", display_name):
        errors.append("显示名称不能包含控制字符。")


def _account_public_base_url() -> str:
    return journal_alert_public_base_url(DEPLOYMENT).rstrip("/")


def _account_email_configured() -> bool:
    return bool(load_smtp_config().enabled)


def _plain_text_html(text: str) -> str:
    import html

    return "<p>" + html.escape(text).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def _send_account_email(to_email: str, subject: str, body: str) -> None:
    config = load_smtp_config()
    if not config.enabled:
        raise RuntimeError("全站发信邮箱未配置，暂时无法发送邮件。")
    send_email(config, to_email, subject, body, _plain_text_html(body))


def _feedback_public_base_url() -> str:
    return _account_public_base_url() or DEPLOYMENT.public_base_url or ""


def _feedback_attachment_note(message: dict) -> str:
    count = len(message.get("attachments") or [])
    return f"（含 {count} 张图片，请登录站点查看）\n" if count else ""


def _sniff_feedback_image_mime(blob: bytes) -> str:
    """通过文件头识别图片类型，避免仅凭扩展名被伪造。返回受支持的 mime 或空串。"""
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith(b"GIF87a") or blob.startswith(b"GIF89a"):
        return "image/gif"
    if len(blob) >= 12 and blob[0:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _save_feedback_uploads(files: list) -> tuple[list[dict], list[str]]:
    """校验并落盘留言图片，返回 (附件元数据列表, 告警列表)。

    先写文件后写库（由调用方负责入库），即便中途崩溃也只会留下孤立文件而不会产生悬空引用。
    """
    attachments: list[dict] = []
    warnings: list[str] = []
    for upload in files or []:
        if upload is None or not getattr(upload, "filename", ""):
            continue
        if len(attachments) >= FEEDBACK_MAX_IMAGES_PER_MESSAGE:
            warnings.append(f"最多上传 {FEEDBACK_MAX_IMAGES_PER_MESSAGE} 张图片，多余的已忽略。")
            break
        blob = upload.read(FEEDBACK_MAX_IMAGE_BYTES + 1)
        if not blob:
            continue
        if len(blob) > FEEDBACK_MAX_IMAGE_BYTES:
            warnings.append(f"{upload.filename}：图片过大（>5MB），未保存。")
            continue
        mime = _sniff_feedback_image_mime(blob)
        if not mime:
            warnings.append(f"{upload.filename}：不支持的图片格式（仅 png/jpg/gif/webp）。")
            continue
        ext = FEEDBACK_IMAGE_EXT.get(mime, "png")
        stored_name = f"{secrets.token_hex(16)}.{ext}"
        target = FEEDBACK_IMAGE_DIR / stored_name
        try:
            FEEDBACK_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to save feedback image %s: %s", upload.filename, exc)
            warnings.append(f"{upload.filename}：保存失败。")
            continue
        attachments.append(
            {
                "stored_name": stored_name,
                "mime": mime,
                "byte_size": len(blob),
                "original_name": str(upload.filename or "")[:200],
            }
        )
    return attachments, warnings


def _send_feedback_admin_notice(thread: dict, message: dict) -> tuple[bool, str]:
    body = (
        "管理员您好：\n\n"
        "网站收到一条新的用户留言。\n\n"
        f"用户：{thread.get('display_name') or '未命名'}\n"
        f"邮箱：{thread.get('user_email') or ''}\n"
        f"时间：{message.get('created_at') or ''}\n\n"
        f"{message.get('body') or ''}\n"
        f"{_feedback_attachment_note(message)}\n"
        f"请登录后台内容运营页回复：{_feedback_public_base_url()}/admin/content"
    )
    try:
        _send_account_email(FEEDBACK_ADMIN_EMAIL, "网站用户留言提醒", body)
    except Exception as exc:
        LOGGER.warning("Failed to send feedback admin notice: %s", exc)
        return False, str(exc)
    return True, ""


_READER_LABELS = {"viewer": "扫描页阅读器", "liushi": "流式阅读", "wenku": "原文文库"}


def _send_page_error_admin_notice(report: dict) -> tuple[bool, str]:
    """页码报错管理员邮件提醒（仿留言提醒；仅在「新报告」时调用，同页重复报错不重发）。"""
    reader_label = _READER_LABELS.get(str(report.get("reader") or ""), str(report.get("reader") or ""))
    body = (
        "管理员您好：\n\n"
        "有读者反馈某处引文页码可能有误。\n\n"
        f"阅读器：{reader_label}\n"
        f"书目：{report.get('book_title') or ''} {report.get('volume_label') or ''}\n"
        f"报错页码：第 {report.get('page') or '（未知）'} 页\n"
        f"引文原文：{report.get('citation_text') or ''}\n"
        f"定位来源：{report.get('source_ref') or ''}\n"
        f"上报用户：{report.get('user_email') or '（未登录/匿名）'}\n"
        f"时间：{report.get('created_at') or ''}\n\n"
        f"请登录后台内容运营页查看并处理：{_feedback_public_base_url()}/admin/content"
    )
    try:
        _send_account_email(FEEDBACK_ADMIN_EMAIL, "网站页码报错提醒", body)
    except Exception as exc:
        LOGGER.warning("Failed to send page-error admin notice: %s", exc)
        return False, str(exc)
    return True, ""


def _resolve_viewer_pdf_page(volume, page_label: str) -> int | None:
    """把扫描页阅读器里显示的页标签反解回 PDF 页序号（1-based），与 viewer.html 的 getPageLabel 互逆：
    - 「PDF 45」这类无印本页码的标签 → 45；
    - 印本页码 → 该卷 printed_to_pdf 映射（取首个匹配的 PDF 页）；
    - 前置页在阅读器里剥「pre-」前缀显示，故存的标签需补回 pre- 再查。
    解析不到返回 None。"""
    label = " ".join(str(page_label or "").split())
    if volume is None or not label:
        return None
    m = re.match(r"^PDF\s+(\d+)$", label)
    if m:
        return int(m.group(1))
    mapping = getattr(volume, "printed_to_pdf", None) or {}
    if label in mapping:
        return int(mapping[label])
    if ("pre-" + label) in mapping:
        return int(mapping["pre-" + label])
    return None


def _augment_page_error_jump_urls(reports: list[dict]) -> list[dict]:
    """给「页码报错」条目补一个「跳转该页」链接，便于管理员一键核对读者上报的那一页。
    仅扫描页阅读器(viewer)能精确定位——把上报页标签反解回 PDF 页序号；解析不到则退回打开该书首页。
    流式/文库阅读器按章节切分、无稳定的页级 URL，暂不提供跳转。"""
    for r in reports or []:
        if not isinstance(r, dict):
            continue
        r["jump_url"] = ""
        r["jump_exact"] = False
        if str(r.get("reader") or "") != "viewer" or corpus is None:
            continue
        source_ref = str(r.get("source_ref") or "").strip()
        if not source_ref:
            continue
        volume = corpus.get_volume_by_source_file(source_ref)
        if volume is None:   # 书目未在当前语料中（或来源无效）→ 不给按钮，避免坏链接
            continue
        pdf_page = _resolve_viewer_pdf_page(volume, r.get("page"))
        r["jump_url"] = url_for("pdf_viewer", file=source_ref, page=pdf_page or 1)
        r["jump_exact"] = pdf_page is not None
    return reports


def _send_feedback_user_reply(thread: dict, message: dict) -> tuple[bool, str]:
    to_email = normalize_email(str(thread.get("user_email") or ""))
    if not to_email:
        return False, "用户邮箱为空。"
    body = (
        "您好：\n\n"
        "您在马恩《文集》《全集》检索程序中的留言已有管理员回复：\n\n"
        f"{message.get('body') or ''}\n"
        f"{_feedback_attachment_note(message)}\n"
        f"你也可以登录后在首页留言栏查看历史会话：{_feedback_public_base_url()}/"
    )
    try:
        _send_account_email(to_email, "网站留言回复", body)
    except Exception as exc:
        LOGGER.warning("Failed to send feedback user reply: %s", exc)
        return False, str(exc)
    return True, ""


def _make_email_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def _send_registration_code(email: str) -> None:
    code = _make_email_code()
    create_account_email_token(email=email, purpose="register", code=code, ttl_minutes=15)
    body = (
        "您好：\n\n"
        f"您的注册验证码是：{code}\n\n"
        "验证码 15 分钟内有效。如非本人操作，请忽略本邮件。"
    )
    _send_account_email(email, "注册邮箱验证码", body)


def _send_password_reset_email(user: dict) -> None:
    base_url = _account_public_base_url()
    if not base_url:
        raise RuntimeError("PUBLIC_BASE_URL 未配置，暂时无法发送找回密码链接。")
    token = create_account_email_token(
        email=str(user["email"]),
        user_id=int(user["id"]),
        purpose="password_reset",
        ttl_minutes=30,
    )
    reset_url = f"{base_url}{url_for('reset_password', token=token['token'])}"
    body = (
        "您好：\n\n"
        "请点击下面的链接重设密码，链接 30 分钟内有效：\n"
        f"{reset_url}\n\n"
        "如非本人操作，请忽略本邮件。"
    )
    _send_account_email(str(user["email"]), "找回密码", body)


def _sweep_expired_orders_if_due() -> None:
    now = time.time()
    if now - _last_order_expiry_sweep[0] < ORDER_EXPIRY_SWEEP_INTERVAL_SECONDS:
        return
    _last_order_expiry_sweep[0] = now
    try:
        count = expire_pending_orders(older_than_hours=24)
        # 顺带做一次「同用户同套餐仅保留最新待支付单」的全局去重（原先挂在每次订单列表读上，现移到这里）。
        prune_duplicate_pending_orders_for_user()
    except Exception as exc:
        LOGGER.warning("Expired-order sweep failed: %s", exc)
        return
    if count:
        LOGGER.info("Expired %s stale pending orders.", count)


def _site_text_form_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for group in list_site_text_groups():
        for entry in group["entries"]:
            key = str(entry["key"])
            values[key] = str(request.form.get(f"site_text__{key}", ""))
    return values


def _effective_site_text_map() -> dict[str, str]:
    # 默认文案只取一次（get_site_text_map 内部已对模板扫描做签名缓存）：defaults 作为不可变基线，
    # current 为可变副本，叠加后台覆盖；末尾的「失效文案还原」用 defaults 校正，无需二次扫描。
    defaults = get_site_text_map()
    current = dict(defaults)
    if DEPLOYMENT.is_server:
        values = get_setting("site_texts", {})
        if isinstance(values, dict):
            current.update({str(k): str(v) for k, v in values.items()})
    elif DEPLOYMENT.is_desktop:
        cache = load_desktop_sync_cache()
        settings = cache.get("settings") if isinstance(cache, dict) else {}
        values = settings.get("site_texts") if isinstance(settings, dict) else {}
        if isinstance(values, dict):
            current.update({str(k): str(v) for k, v in values.items()})
        # 本机控制台覆盖最后叠加，确保在桌面端点「保存文案」后立即生效；网站同步值仍是
        # 无本地覆盖时的权威基线。get_site_text_map 已负责把自动接入项纳入默认映射。
        current.update(load_site_text_overrides())
    legacy_network_texts = {
        "index.feature_kicker": "独立阅读器",
        "index.ai_title": "联网资料问答",
        "index.ai_empty_state": "这里可以直接询问公开网络资料。回答会优先联网检索，再附上来源链接。",
        "pricing.hero_intro": "这一页已经把站内账号、会员状态、订单骨架接上了。当前页面展示套餐与下单入口，接入真实支付后即可完成自动开通。",
        "pricing.payment_enabled": "在线支付已开通。提交订单后将跳转到收银台，支付完成后会员权益会自动生效。",
        "pricing.payment_disabled": "第四方支付逻辑已经写入代码，但配置尚未完成；现在点击“立即开通”会先创建待支付订单。",
        "pricing.feature_viewer": "解锁 `/viewer` 全文浏览与目录导航",
        "pricing.feature_pdf": "解锁 `/pdf` 原始 PDF 下载访问",
        "pricing.feature_ai": "解锁页内图像高亮与 AI 问答",
        "pricing.feature_account": "会员状态、订单与订阅记录可在会员中心查看",
        "account.membership_note": "会员状态来自订阅表。支付成功后，系统会在验签通过后把订单标记为 `paid`，并自动生成或更新会员订阅。",
        "account.empty_orders": "还没有订单。你可以先到套餐页创建订单；如果支付中断，会员中心里可以继续拉起在线支付。",
        "account.empty_subscriptions": "还没有有效订阅。支付接入前也可以用 `scripts/grant_membership.py` 进行人工开通测试。",
        "account.payment_enabled": "在线支付已开通。支付完成后，系统会自动更新订单和会员状态。",
        "account.payment_disabled": "第四方支付代码已接入，但配置尚未完成。请编辑 `config/zpay.yaml` 或服务器环境变量，填写 `pid`、商户密钥 `key`，以及公网 `PUBLIC_BASE_URL`。",
        "viewer.ai_empty_state": "关闭联网时，AI 只会根据当前页和相邻页文本解释内容；开启联网时，会补充更广泛的公开资料与背景。",
        "viewer.prompt_placeholder": "例如：这段文字中的“联合起来”在这里具体指什么？如果联网，请顺便讲讲它与当时历史背景的关系。",
    }
    for key, legacy_value in legacy_network_texts.items():
        if current.get(key) == legacy_value:
            current[key] = defaults.get(key, current[key])
    return current


def _request_site_texts() -> dict[str, str]:
    """按请求惰性计算并缓存站点文案映射。只有真正渲染模板的请求（经 context_processor 调用本函数）
    才会触发合成；/page-image、/static、多数 JSON API 等高频非模板端点根本不渲染模板，故完全跳过，
    省去每请求的文案合成开销（阅读时翻页只打 /page-image，受益最直接）。同一请求内多次访问复用 g 缓存。"""
    cached = getattr(g, "_site_text_map_cache", None)
    if cached is None:
        cached = _effective_site_text_map()
        g._site_text_map_cache = cached
    return cached


def _effective_override_values() -> dict[str, str]:
    """当前文案存储里真正被人工保存过的 key→value（不含默认值）。

    服务器后台存于设置表 site_texts；桌面端来自同步缓存；其余回退到本地覆盖文件。
    用于检测「失效文案缓存」（框架已不存在却仍留存的保存项）。
    """
    if DEPLOYMENT.is_server:
        values = get_setting("site_texts", {})
        if isinstance(values, dict):
            return {str(k): str(v) for k, v in values.items()}
        return {}
    if DEPLOYMENT.is_desktop:
        cache = load_desktop_sync_cache()
        settings = cache.get("settings") if isinstance(cache, dict) else {}
        values = settings.get("site_texts") if isinstance(settings, dict) else {}
        merged = {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}
        merged.update(load_site_text_overrides())
        return merged
    return load_site_text_overrides()


def _control_context() -> dict:
    _refresh_ai_runtime_if_needed()
    search_text = (request.args.get("user_q") or "").strip()
    ai_override_exists = AI_OVERRIDE_PATH.exists()
    state = current_view_state()
    return {
        "title": "本地控制台",
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "state": state,
        "ai_settings": AI_CONFIG.to_edit_dict(),
        "ai_base_config_path": str(AI_CONFIG_PATH),
        "ai_override_path": str(AI_OVERRIDE_PATH),
        "ai_override_exists": ai_override_exists,
        "ai_config_source_label": "本地覆盖文件 + 项目配置" if ai_override_exists else "项目配置文件",
        "request_token": REQUEST_TOKEN if state["management_api_enabled"] else None,
        "site_text_override_path": str(SITE_TEXT_OVERRIDES_PATH),
        "site_text_groups": list_site_text_groups(),
        "plans_all": list_plans(include_inactive=True),
        "users": list_users(search_text=search_text, limit=CONTROL_USER_LIMIT),
        "user_q": search_text,
        "recent_orders": list_recent_orders(limit=18),
        "recent_subscriptions": list_recent_subscriptions(limit=18),
        "recent_payment_events": list_payment_events(limit=18),
        "payment_qr_settings": _payment_qr_settings(),
        "control_payment_qr_url": url_for("admin_payment_qr_settings"),
        "control_payment_test_qr_url": url_for("admin_payment_test_qr"),
        "control_payment_clear_pending_url": url_for("admin_payment_clear_pending"),
        "membership_db_path": str(MEMBERSHIP_DB_PATH),
    }


def _management_redirect(remote_admin: bool, section: str, **params: object):
    if remote_admin:
        module = ADMIN_SECTION_MODULES.get(section, section if section in ADMIN_MODULES else "overview")
        return redirect(url_for("admin", module=module, **params))
    return redirect(url_for("control", section=section, **params))


def _management_console_context(*, remote_admin: bool, admin_module: str = "overview") -> dict:
    _refresh_ai_runtime_if_needed()
    search_text = (request.args.get("user_q") or "").strip()
    ai_override_exists = AI_OVERRIDE_PATH.exists()
    state = current_view_state()
    if remote_admin:
        admin_module = admin_module if admin_module in ADMIN_MODULES else "overview"
        title = f"{ADMIN_MODULES[admin_module]} - 远程管理后台"
        console_intro = "这里是网站控制台。AI、文案、套餐、用户权限和期刊订阅都在这里维护。"
        ai_config_source_label = "后台数据库设置 + 项目配置文件"
        ai_override_hint = "保存后写入后台统一设置表，环境变量仍可作为紧急覆盖；服务器密钥不会下发到本地端。"
        ai_override_caption = "后台统一设置"
        ai_reset_button_label = "清除后台 AI 设置"
        site_text_caption = "后台统一文案"
        site_text_note = "这里维护网站权威文案；本地端联网同步后会读取这些公共文案缓存。"
    else:
        title = "本地诊断与同步"
        console_intro = "这里仅显示本机资料状态、网站同步状态和缓存授权。运营配置、会员、设备授权与发布更新都在网站 /admin 管理。"
        ai_config_source_label = "网站同步缓存"
        ai_override_hint = "本地端不会保存服务器 AI Key；AI 请求会通过已授权的网站代理。"
        ai_override_caption = "同步缓存"
        ai_reset_button_label = "清除本地同步缓存"
        site_text_caption = "同步文案缓存"
        site_text_note = "本地端不再编辑站点文案；这些内容来自网站后台同步缓存。"
    current_site_texts = _effective_site_text_map()
    access_policy = _load_access_policy()
    plans_all = list_plans(include_inactive=True)
    users = list_users(search_text=search_text, limit=CONTROL_USER_LIMIT)
    user_feature_rows = _feature_access_rows(users)
    feature_by_user_id = {row["user_id"]: row for row in user_feature_rows}
    for user in users:
        user["feature_access"] = feature_by_user_id.get(user.get("id"), {})
    dashboard_metrics: dict = {}
    dashboard_selected_day = _beijing_now().date().isoformat()
    dashboard_history_start = ""
    dashboard_history_end = ""
    dashboard_history_rows: list[dict] = []
    if remote_admin:
        dashboard_selected_day = _parse_dashboard_day(
            request.args.get("date", ""),
            _beijing_now().date().isoformat(),
        )
        dashboard_metrics = _dashboard_metrics_for_day(dashboard_selected_day)
        # 标记每位高用量/榜单用户当前是否已被单独封禁 AI，供总览的封禁/解封按钮显示正确状态。
        blocked_emails = _ai_blocked_emails(access_policy)
        for bucket in ("high_token_users", "top_token_users"):
            for row in dashboard_metrics.get(bucket) or []:
                row["ai_blocked"] = normalize_email(str(row.get("email") or "")) in blocked_emails
        reader_bans = _reader_bans()
        for row in dashboard_metrics.get("reader_anomaly_visitors") or []:
            row["reader_blocked"] = _reader_ban_status(row, policy=access_policy, bans=reader_bans)
        dashboard_history_start, dashboard_history_end, history_days = _dashboard_selected_history_range()
        dashboard_history_rows = _dashboard_history_rows(history_days)
    # 当前批次（采集→综述→发送的承载单元）。文章审核/综述审核只针对当前批次。
    journal_batch = current_batch() if remote_admin else None
    journal_batch_pending: list[dict] = []
    journal_batch_ready: list[dict] = []
    journal_send_status: dict = {}
    if remote_admin and journal_batch:
        journal_batch_pending = batch_articles(int(journal_batch["id"]), statuses=("pending_review",))
        journal_batch_ready = batch_articles(int(journal_batch["id"]), statuses=("ready",))
    if remote_admin:
        journal_send_status = _journal_autosend_status(
            load_journal_alert_settings(), journal_batch, load_smtp_config().enabled
        )
    return {
        "title": title,
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "console_intro": console_intro,
        "remote_admin": remote_admin,
        "admin_module": admin_module,
        "admin_modules": ADMIN_MODULES,
        "admin_module_urls": {key: url_for("admin", module=key) for key in ADMIN_MODULES} if remote_admin else {},
        "state": state,
        "ai_settings": AI_CONFIG.to_edit_dict(),
        "ai_base_config_path": str(AI_CONFIG_PATH),
        "ai_override_path": str(AI_OVERRIDE_PATH),
        "ai_override_exists": ai_override_exists,
        "ai_config_source_label": ai_config_source_label,
        "ai_override_hint": ai_override_hint,
        "ai_override_caption": ai_override_caption,
        "ai_reset_button_label": ai_reset_button_label,
        "request_token": REQUEST_TOKEN if state["management_api_enabled"] else None,
        "csrf_token": _ensure_csrf_token(),
        "console_home_url": url_for("admin" if remote_admin else "control"),
        "control_ai_url": url_for("admin_ai" if remote_admin else "control_ai"),
        "control_site_texts_url": url_for("admin_site_texts" if remote_admin else "control_site_texts"),
        "control_content_scan_url": url_for("admin_content_scan" if remote_admin else "control_content_scan"),
        "control_content_prune_url": url_for("admin_content_prune") if remote_admin else "",
        "control_plans_url": url_for("admin_plans" if remote_admin else "control_plans"),
        "control_membership_grant_url": url_for(
            "admin_membership_grant" if remote_admin else "control_membership_grant"
        ),
        "control_membership_bulk_grant_url": url_for(
            "admin_membership_bulk_grant" if remote_admin else "control_membership_bulk_grant"
        ),
        "control_user_search_url": url_for("admin", module="members") if remote_admin else url_for("control"),
        "control_user_update_endpoint": "admin_user_update" if remote_admin else "control_user_update",
        "site_text_override_path": str(SITE_TEXT_OVERRIDES_PATH),
        "site_text_caption": site_text_caption,
        "site_text_note": site_text_note,
        "site_text_groups": list_site_text_groups_from_map(current_site_texts),
        "site_text_coverage": site_text_coverage_report(overrides=_effective_override_values()),
        "plans_all": plans_all,
        "audience_feature_access": _audience_feature_access_rows(access_policy),
        "audience_access_labels": AUDIENCE_ACCESS_LABELS,
        "plan_feature_access": _plan_feature_access_rows(plans_all, access_policy),
        "research_quota_settings": _research_weekly_quota_settings(),
        "research_quota_labels": RESEARCH_WEEKLY_QUOTA_LABELS,
        "research_count_limit_enabled": _research_count_limit_enabled(),
        "ai_token_quota_settings": _ai_token_daily_settings(),
        "ai_token_quota_labels": AI_TOKEN_DAILY_LABELS,
        "ai_token_quota_defaults": AI_TOKEN_DAILY_DEFAULTS,
        "control_ai_token_quota_url": url_for("admin_ai_token_quota") if remote_admin else "",
        "control_reset_ai_token_quota_url": url_for("admin_reset_ai_token_quota") if remote_admin else "",
        "ai_token_quota_reset_labels": AI_TOKEN_QUOTA_RESET_SCOPE_LABELS,
        "ai_token_quota_resets_active": _ai_token_quota_reset_status() if remote_admin else [],
        "users": users,
        "user_q": search_text,
        "bulk_grant_coverage": bulk_grant_coverage() if remote_admin else None,
        "feature_access_keys": FEATURE_ACCESS_KEYS,
        "feature_access_labels": FEATURE_ACCESS_LABELS,
        "feature_access_groups": FEATURE_ACCESS_GROUPS,
        "feature_access_hints": FEATURE_ACCESS_HINTS,
        "access_policy": access_policy,
        "dashboard_metrics": dashboard_metrics,
        "dashboard_selected_day": dashboard_selected_day,
        "dashboard_history_start": dashboard_history_start,
        "dashboard_history_end": dashboard_history_end,
        "dashboard_history_rows": dashboard_history_rows,
        "dashboard_rules": _dashboard_rules(),
        "dashboard_export_url": (
            url_for(
                "admin_dashboard_export",
                history_start=dashboard_history_start,
                history_end=dashboard_history_end,
            )
            if remote_admin
            else ""
        ),
        "dashboard_export_all_url": url_for("admin_dashboard_export", all="1") if remote_admin else "",
        "user_feature_access": user_feature_rows,
        "control_access_policy_url": url_for("admin_access_policy") if remote_admin else "",
        "control_ai_access_url": url_for("admin_ai_access") if remote_admin else "",
        "control_ai_usage_url": url_for("admin_ai_usage") if remote_admin else "",
        "control_reader_access_url": url_for("admin_reader_access") if remote_admin else "",
        "control_research_quota_url": url_for("admin_research_quota") if remote_admin else "",
        "control_reset_research_quota_url": url_for("admin_reset_research_quota") if remote_admin else "",
        "control_online_series_url": url_for("admin_online_series") if remote_admin else "",
        "control_notice_url": url_for("admin_notice") if remote_admin else url_for("control_notice"),
        "control_community_url": url_for("admin_community") if remote_admin else url_for("control_community"),
        "control_community_from_feedback_url": url_for("admin_community_from_feedback") if remote_admin else "",
        "control_feature_tags_url": url_for("admin_feature_tags") if remote_admin else url_for("control_feature_tags"),
        "feature_tags": _get_feature_tags(),
        "control_citation_formats_url": url_for("admin_citation_formats") if remote_admin else url_for("control_citation_formats"),
        "citation_formats_editor": _citation_formats_editor(),
        "control_card_order_url": url_for("admin_card_order") if remote_admin else url_for("control_card_order"),
        "card_order_cards": [{"key": key, "label": _FEATURE_CARD_LABELS[key]} for key in _get_card_order()],
        "control_registry_geo_url": url_for("admin_registry_geo") if remote_admin else url_for("control_registry_geo"),
        "registry_geo_settings": _registry_geo_editor_settings(),
        "control_ai_assistant_mode_url": url_for("admin_ai_assistant_mode") if remote_admin else url_for("control_ai_assistant_mode"),
        "ai_assistant_mode_setting": _ai_assistant_mode(),
        "control_sponsor_url": url_for("admin_sponsor") if remote_admin else url_for("control_sponsor"),
        "sponsor_enabled_setting": _sponsor_button_enabled(),
        "control_reader_access_ban_url": url_for("admin_reader_access_ban") if remote_admin else "",
        "recent_orders": list_recent_orders(limit=18),
        "recent_subscriptions": list_recent_subscriptions(limit=18),
        "recent_payment_events": list_payment_events(limit=18),
        "payment_qr_settings": _payment_qr_settings() if remote_admin else {"default_mode": "redirect", "plans": {}},
        "control_payment_qr_url": url_for("admin_payment_qr_settings") if remote_admin else "",
        "control_payment_test_qr_url": url_for("admin_payment_test_qr") if remote_admin else "",
        "control_payment_clear_pending_url": url_for("admin_payment_clear_pending") if remote_admin else "",
        "membership_db_path": str(MEMBERSHIP_DB_PATH),
        "admin_store_db_path": str(ADMIN_STORE_DB_PATH),
        "journal_alerts_db_path": str(JOURNAL_ALERTS_DB_PATH),
        "feedback_db_path": str(FEEDBACK_DB_PATH),
        "feedback_threads": list_feedback_threads(limit=50) if remote_admin else [],
        "feedback_admin_email": FEEDBACK_ADMIN_EMAIL,
        "page_error_reports": _augment_page_error_jump_urls(list_page_error_reports(limit=80)) if remote_admin else [],
        "page_error_open_count": count_open_page_error_reports() if remote_admin else 0,
        "journal_alert_settings": load_journal_alert_settings(),
        "journal_sources": list_journal_sources(limit=80) if remote_admin else [],
        "journal_source_catalog": journal_source_catalog() if remote_admin else {"zh": [], "en": [], "total": 0, "auto_count": 0},
        "journal_alert_subscriptions": list_recent_journal_subscriptions(limit=80) if remote_admin else [],
        # 旧版全局列表保留（兼容模板/历史数据），批次化审核改用下面的 batch 变量。
        "journal_pending_articles": list_journal_articles_by_status("pending_review", limit=30) if remote_admin else [],
        "journal_ready_articles": list_journal_articles_by_status("ready", limit=12) if remote_admin else [],
        # 当前批次及其文章（按学科归类后展示），综述与发送都围绕它。
        "journal_current_batch": journal_batch,
        "journal_send_status": journal_send_status,
        "journal_batch_pending_articles": journal_batch_pending,
        "journal_batch_ready_articles": journal_batch_ready,
        # 顺延（deferred）待办：超过单期发布上限、留待后续批次逐步释放的文章数。
        "journal_deferred_count": count_deferred_journal_articles() if remote_admin else 0,
        "journal_recent_batches": list_recent_batches(limit=8) if remote_admin else [],
        "journal_ai_model": AI_CONFIG.model,
        "journal_ai_enabled": bool(AI_CONFIG.enabled),
        "journal_recent_articles": list_recent_journal_articles(limit=18) if remote_admin else [],
        "journal_recent_runs": list_recent_journal_runs(limit=12) if remote_admin else [],
        "journal_delivery_logs": list_recent_journal_delivery_logs(limit=18) if remote_admin else [],
        "journal_abstract_coverage": journal_abstract_coverage() if remote_admin else {},
        # 中文源国内中继健康度（中英文采集/发送本就同批次统一，这里只看中继侧是否新鲜）
        "journal_relay_status": journal_relay_status() if remote_admin else {},
        "journal_smtp_enabled": load_smtp_config().enabled,
        "smtp_config": load_smtp_config(),
        "control_email_test_url": url_for("admin_email_test") if remote_admin else "",
        "control_broadcast_send_url": url_for("admin_broadcast_send") if remote_admin else "",
        "control_broadcast_preview_url": url_for("admin_broadcast_preview") if remote_admin else "",
        "control_broadcast_draft_url": url_for("admin_broadcast_draft") if remote_admin else "",
        "broadcast_draft": _broadcast_draft() if remote_admin else {"subject": "", "body": "", "saved_at": "", "source": ""},
        "broadcast_recent_campaigns": list_recent_broadcast_campaigns(limit=12) if remote_admin else [],
        # 「久未回访用户」召回名单（注册于记 IP 上线前、至今无登录态回访；回访即自动离开该集合）
        "broadcast_dormant_recipients": resolve_broadcast_recipients("dormant_noip") if remote_admin else [],
        "control_journal_run_url": url_for("admin_journal_run") if remote_admin else "",
        "control_journal_backfill_url": url_for("admin_journal_backfill_sources") if remote_admin else "",
        "control_journal_approve_all_url": url_for("admin_journal_approve_all") if remote_admin else "",
        "control_journal_article_review_endpoint": "admin_journal_article_review",
        "control_journal_source_add_url": url_for("admin_journal_source_add") if remote_admin else "",
        "control_journal_review_endpoint": "admin_journal_digest_review",
        "control_journal_digest_send_endpoint": "admin_journal_digest_send",
        "control_journal_archive_url": url_for("admin_journal_archive_old") if remote_admin else "",
        "control_journal_purge_url": url_for("admin_journal_purge_archived") if remote_admin else "",
        "desktop_sync": {**load_desktop_sync_cache(), "cache_path": str(DESKTOP_SYNC_CACHE_PATH)},
        "desktop_devices": list_desktop_devices(limit=80) if remote_admin else [],
        "desktop_releases": list_releases(limit=30) if remote_admin else [],
        "latest_desktop_release": latest_release(),
    }

def _require_management_access(remote_admin: bool) -> None:
    if remote_admin:
        _require_admin()
        return
    _require_local_console()


def _handle_ai_settings_submit(*, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，运营配置请在网站 /admin 管理。")
    _refresh_ai_runtime_if_needed()
    action = (request.form.get("action") or "save").strip().lower()
    if action == "reset":
        if DEPLOYMENT.is_server:
            delete_setting("ai")
        else:
            reset_ai_overrides()
        _reload_ai_runtime()
        _log_management_action(
            action="ai.reset",
            target=str(AI_OVERRIDE_PATH),
            result="success",
            remote_admin=remote_admin,
        )
        flash("AI 覆盖设置已清除，已恢复为项目配置文件/环境变量值。", "success")
        return _management_redirect(remote_admin, "ai")

    current = AI_CONFIG.to_edit_dict()
    try:
        values = {
            "provider": (request.form.get("provider") or current["provider"]).strip() or AI_DEFAULT_PROVIDER,
            "model": (request.form.get("model") or current["model"]).strip() or AI_DEFAULT_MODEL,
            "base_url": (request.form.get("base_url") or current["base_url"]).strip().rstrip("/"),
            "api_key": (request.form.get("api_key") or "").strip(),
            "zhipu_api_key": (request.form.get("zhipu_api_key") or "").strip(),
            "zhipu_model": (request.form.get("zhipu_model") or current["zhipu_model"]).strip() or ZHIPU_DEFAULT_MODEL,
            "zhipu_base_url": (request.form.get("zhipu_base_url") or current["zhipu_base_url"]).strip().rstrip("/") or ZHIPU_DEFAULT_BASE_URL,
            "zhipu_search_engine": (request.form.get("zhipu_search_engine") or current["zhipu_search_engine"]).strip() or ZHIPU_SEARCH_ENGINES[0],
            "zhipu_search_count": _form_int("zhipu_search_count", int(current["zhipu_search_count"])),
            "zhipu_daily_token_limit": _form_int("zhipu_daily_token_limit", int(current["zhipu_daily_token_limit"])),
            "request_timeout_seconds": _form_int("request_timeout_seconds", int(current["request_timeout_seconds"])),
            "max_history_turns": _form_int("max_history_turns", int(current["max_history_turns"])),
            "search_history_turns": _form_int("search_history_turns", int(current["search_history_turns"])),
            "pdf_history_turns": _form_int("pdf_history_turns", int(current["pdf_history_turns"])),
            "search_message_char_limit": _form_int("search_message_char_limit", int(current["search_message_char_limit"])),
            "pdf_message_char_limit": _form_int("pdf_message_char_limit", int(current["pdf_message_char_limit"])),
            "search_answer_max_tokens": _form_int("search_answer_max_tokens", int(current["search_answer_max_tokens"])),
            "pdf_answer_max_tokens": _form_int("pdf_answer_max_tokens", int(current["pdf_answer_max_tokens"])),
            "pdf_quick_answer_max_tokens": _form_int("pdf_quick_answer_max_tokens", int(current["pdf_quick_answer_max_tokens"])),
            "pdf_selected_text_char_limit": _form_int("pdf_selected_text_char_limit", int(current["pdf_selected_text_char_limit"])),
            "pdf_current_text_char_limit": _form_int("pdf_current_text_char_limit", int(current["pdf_current_text_char_limit"])),
            "pdf_adjacent_excerpt_char_limit": _form_int("pdf_adjacent_excerpt_char_limit", int(current["pdf_adjacent_excerpt_char_limit"])),
            "pdf_quick_selected_text_char_limit": _form_int("pdf_quick_selected_text_char_limit", int(current["pdf_quick_selected_text_char_limit"])),
            "pdf_quick_current_text_char_limit": _form_int("pdf_quick_current_text_char_limit", int(current["pdf_quick_current_text_char_limit"])),
            "pdf_quick_adjacent_excerpt_char_limit": _form_int("pdf_quick_adjacent_excerpt_char_limit", int(current["pdf_quick_adjacent_excerpt_char_limit"])),
            "temperature": _form_float("temperature", float(current["temperature"])),
        }
    except ValueError:
        _log_management_action(
            action="ai.save",
            target=str(AI_OVERRIDE_PATH),
            result="invalid_input",
            remote_admin=remote_admin,
        )
        flash("AI 设置里包含无效数字，请检查后再保存。", "warning")
        return _management_redirect(remote_admin, "ai")

    if DEPLOYMENT.is_server:
        set_setting("ai", values, updated_by=_management_actor_label(remote_admin))
    else:
        save_ai_overrides(values)
    _reload_ai_runtime()
    _log_management_action(
        action="ai.save",
        target=str(AI_OVERRIDE_PATH),
        result="success",
        remote_admin=remote_admin,
        details={"provider": values["provider"], "model": values["model"]},
    )
    flash("AI 设置已保存，并已在当前运行中立即生效。", "success")
    return _management_redirect(remote_admin, "ai")


def _handle_site_texts_submit(*, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，站点文案请在网站 /admin 管理。")
    action = (request.form.get("action") or "save").strip().lower()
    if action == "reset":
        if DEPLOYMENT.is_server:
            delete_setting("site_texts")
        else:
            reset_site_text_overrides()
        _log_management_action(
            action="site_text.reset",
            target=str(SITE_TEXT_OVERRIDES_PATH),
            result="success",
            remote_admin=remote_admin,
        )
        flash("站点说明文字已恢复默认值。", "success")
    else:
        values = _site_text_form_values()
        if DEPLOYMENT.is_server:
            defaults = get_site_text_map()
            set_setting(
                "site_texts",
                {key: value for key, value in values.items() if defaults.get(key) != value},
                updated_by=_management_actor_label(remote_admin),
            )
        else:
            save_site_text_overrides(values)
        _log_management_action(
            action="site_text.save",
            target=str(SITE_TEXT_OVERRIDES_PATH),
            result="success",
            remote_admin=remote_admin,
            details={"keys": len(values)},
        )
        flash("站点说明文字已保存。", "success")
    return _management_redirect(remote_admin, "copy")


def _save_quick_site_texts(
    updates: dict,
    *,
    remote_admin: bool,
    action: str,
    target: str,
    ok_message: str,
):
    """首页右侧若干「局部小表单」（公告、社区建设）的共用保存逻辑：只改传入的几条
    文案覆盖，与框架默认值相同的 key 移除（恢复默认），其余原封不动。"""
    defaults = get_site_text_map()
    if DEPLOYMENT.is_server:
        saved = get_setting("site_texts", {})
        saved = {str(k): str(v) for k, v in saved.items()} if isinstance(saved, dict) else {}
        for key, value in updates.items():
            if defaults.get(key) == value:
                saved.pop(key, None)
            else:
                saved[key] = value
        set_setting("site_texts", saved, updated_by=_management_actor_label(remote_admin))
    else:
        update_site_text_overrides(updates)
    _log_management_action(
        action=action,
        target=target,
        result="success",
        remote_admin=remote_admin,
    )
    flash(ok_message, "success")
    return _management_redirect(remote_admin, "overview")


def _handle_notice_submit(*, remote_admin: bool):
    """「网站公告」的快捷保存：只改公告标题与正文，合并写入，不影响其他文案覆盖。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，网站公告请在网站 /admin 管理。")
    updates = {
        "index.notice_title": str(request.form.get("notice_title", "")).strip(),
        "index.notice_body": str(request.form.get("notice_body", "")),
    }
    return _save_quick_site_texts(
        updates,
        remote_admin=remote_admin,
        action="site_text.notice",
        target="index.notice",
        ok_message="网站公告已更新。",
    )


def _handle_community_submit(*, remote_admin: bool):
    """「社区建设」的快捷保存：从网页公告中拆分出的独立栏目，每行一条，前端循环滚动。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，社区建设请在网站 /admin 管理。")
    updates = {
        "index.community_title": str(request.form.get("community_title", "")).strip(),
        "index.community_body": str(request.form.get("community_body", "")),
    }
    return _save_quick_site_texts(
        updates,
        remote_admin=remote_admin,
        action="site_text.community",
        target="index.community",
        ok_message="社区建设栏已更新。",
    )


# 「社区建设 · 从留言智能提炼」：一次最多提炼多少条最新用户留言、单条陈列的字数上限。
_COMMUNITY_FEEDBACK_MAX = 12
_COMMUNITY_ITEM_MAXLEN = 60
_COMMUNITY_SKIP_TOKENS = {"跳过", "（跳过）", "(跳过)", "跳过。", "无", "略"}


def _clean_display_name(raw: object) -> str:
    """清洗用于公开陈列的用户昵称：折叠空白、去掉会破坏 Markdown 加粗/内联渲染的字符、限长。
    空则回退「读者」。"""
    name = " ".join(str(raw or "").split())
    for ch in ("*", "[", "]", "`", "|", "｜", "<", ">"):
        name = name.replace(ch, "")
    name = name.strip()[:24]
    return name or "读者"


def _community_already_has(existing_text: str, name: str, email: str) -> bool:
    """判断某用户是否已陈列在社区栏中：优先按打码邮箱（同邮箱→同打码串、确定性命中，
    不受 AI 每次改写措辞影响），再按 `**昵称**` 记号兜底（「读者」这类回退名不参与，
    避免误伤所有匿名用户）。命中即『已有』，本轮跳过、不再重复生成。"""
    if not existing_text:
        return False
    masked = _mask_email_public(email)
    if masked and masked in existing_text:
        return True
    cleaned = _clean_display_name(name)
    if cleaned and cleaned != "读者" and f"**{cleaned}**" in existing_text:
        return True
    return False


def _collect_feedback_for_community(
    limit: int = _COMMUNITY_FEEDBACK_MAX, *, existing_text: str = ""
) -> list[dict]:
    """按用户（每个 feedback 会话＝一个用户）汇总其全部留言为一条代表性正文，返回
    [{name, email, body, created_at}]（按该用户最近留言时间倒序，取前 limit 条）供社区栏陈列。
    只取用户发言（author_role='user'）；**已在 existing_text（当前编辑框内容）中陈列过的用户
    直接跳过**——已有的不必再生成。一个用户只出一条（合并其多条留言），避免同人多行。"""
    items: list[dict] = []
    for thread in list_feedback_threads(limit=80):
        email = str(thread.get("user_email") or "").strip()
        name = str(thread.get("display_name") or "").strip()
        if _community_already_has(existing_text, name, email):
            continue
        bodies: list[str] = []
        last_at = ""
        for message in thread.get("messages") or []:
            if str(message.get("author_role")) != "user":
                continue
            body = " ".join(str(message.get("body") or "").split())
            if body:
                bodies.append(body)
                last_at = str(message.get("created_at") or "") or last_at
        if not bodies:
            continue
        items.append({
            "name": name,
            "email": email,
            "body": " / ".join(bodies)[:400],
            "created_at": last_at,
        })
    items.sort(key=lambda it: it["created_at"], reverse=True)
    return items[: max(1, int(limit))]


def _distill_feedback_to_community(items: list[dict]) -> list[str]:
    """把用户留言交给 AI 改写成一句健康、积极的『社区建设』陈列语，关键短语加粗；剔除谩骂/敏感/
    隐私/广告，不宜公开者整条丢弃。逐条拼成「**昵称** (打码邮箱) 正文」，返回可直接写入
    community_body 的行列表（每行一条，前端按 **…** 内联加粗渲染）。"""
    if not items:
        return []
    numbered = "\n\n".join(f"[[{i + 1}]]\n{it['body']}" for i, it in enumerate(items))
    system = (
        "你是网站的社区运营编辑。请把每条用户留言改写成一句可公开陈列在首页『社区建设』栏的话，"
        "呈现读者与站方共建社区的正面氛围。硬性要求：\n"
        "1) 保留留言的核心诉求，一句话说清、自然通顺、书面、积极；不要过度压缩、也不要展开成多句，"
        f"一般不超过 {_COMMUNITY_ITEM_MAXLEN} 字；\n"
        "2) 用 Markdown 粗体 **……** 把这句话里最关键的 1-2 个短语（建议点／功能名／书目名等）加粗突出，其余不加粗；\n"
        "3) 内容必须健康：剔除谩骂、脏话、人身攻击、政治敏感、色情、广告、联系方式、隐私等一切不宜公开的信息；\n"
        "4) 若某条留言整体不适合公开陈列（纯发泄／辱骂／空洞／含敏感信息），仅输出两个字：跳过；\n"
        "5) 只就留言本身改写、不得编造；不要输出用户名、邮箱、序号或任何解释，只输出这一句话本身。\n"
        "严格按『[[序号]] 正文』逐条输出，序号与输入一一对应、条数一致。"
    )
    user = "请逐条改写下列留言（每条以 [[序号]] 开头），按相同序号输出：\n\n" + numbered
    text = AI_CLIENT.chat_complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1400,
        temperature=0.4,
        allow_reasoning_fallback=False,
    )
    parts = re.split(r"\[\[(\d+)\]\]", text)  # [pre, '1', seg1, '2', seg2, ...]
    by_idx: dict[int, str] = {}
    for k in range(1, len(parts) - 1, 2):
        try:
            by_idx[int(parts[k])] = parts[k + 1].strip()
        except (ValueError, IndexError):
            continue
    lines: list[str] = []
    for i, it in enumerate(items):
        distilled = " ".join(by_idx.get(i + 1, "").split())
        if not distilled or distilled in _COMMUNITY_SKIP_TOKENS:
            continue
        # 安全网：正常已由提示词约束在约 60 字，异常超长才截断（尽量不切在词中）。
        if len(distilled) > _COMMUNITY_ITEM_MAXLEN + 30:
            distilled = distilled[: _COMMUNITY_ITEM_MAXLEN + 30].rstrip() + "…"
        name = _clean_display_name(it.get("name"))
        masked = _mask_email_public(it["email"])
        prefix = f"**{name}** ({masked}) " if masked else f"**{name}** "
        line = prefix + distilled
        # **…** 必须成对，否则前端内联加粗会从这里一直串到后文；截断/模型笔误致落单时补一个收口。
        if line.count("**") % 2:
            line += "**"
        lines.append(line)
    return lines


def _handle_feature_tags_submit(*, remote_admin: bool):
    """首页功能栏自定义彩色标签的保存：每张卡片一组 {text,color}，只动 index_feature_tags 设置项，
    不影响原有状态 pill 逻辑。仅网站 /admin 可改（本地控制台只负责诊断/同步）。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，首页标签请在网站 /admin 管理。")
    data: dict[str, list[dict]] = {}
    for card in _FEATURE_TAG_CARDS:
        texts = request.form.getlist(f"tag_{card}_text")
        colors = request.form.getlist(f"tag_{card}_color")
        items: list[dict] = []
        for raw_text, raw_color in zip(texts, colors):
            text = str(raw_text or "").strip()[:20]
            if not text:
                continue
            items.append({"text": text, "color": _coerce_hex_color(raw_color)})
            if len(items) >= _FEATURE_TAG_MAX_PER_CARD:
                break
        data[card] = items
    set_setting("index_feature_tags", data, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="feature_tags.save",
        target="index.feature_tags",
        result="success",
        remote_admin=remote_admin,
    )
    flash("首页功能栏标签已更新。", "success")
    return _management_redirect(remote_admin, "content")


def _handle_citation_formats_submit(*, remote_admin: bool):
    """引文检索「引用格式」自定义模板保存：三种格式各一模板串，只动 citation_formats 设置项，
    保存后即时注入 corpus、当场生效。仅网站 /admin 可改（本地控制台只负责诊断/同步）。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，引用格式请在网站 /admin 管理。")
    data: dict[str, str] = {}
    for key in _CITATION_FORMAT_KEYS:
        tpl = str(request.form.get(f"citation_tpl_{key}") or "").strip()[:_CITATION_TEMPLATE_MAXLEN]
        # 留空或与默认逐字一致 → 不入库（回退到 search.DEFAULT_CITATION_TEMPLATES 默认）。
        if tpl and tpl != DEFAULT_CITATION_TEMPLATES.get(key, "").strip():
            data[key] = tpl
    set_setting("citation_formats", data, updated_by=_management_actor_label(remote_admin))
    _apply_citation_formats_to_corpus()
    _log_management_action(
        action="citation_formats.save",
        target="citation.formats",
        result="success",
        remote_admin=remote_admin,
    )
    flash("引文检索·引用格式模板已更新。", "success")
    return _management_redirect(remote_admin, "content")


def _handle_card_order_submit(*, remote_admin: bool):
    """首页功能卡顺序保存：只动 index_card_order 设置项（一串卡键），不改卡片本身逻辑。
    仅网站 /admin 可改（本地控制台只负责诊断/同步）。表单字段 card_order 为逗号分隔的卡键。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，首页卡片顺序请在网站 /admin 管理。")
    raw = str(request.form.get("card_order") or "")
    order = _sanitize_card_order(raw.split(","))
    set_setting("index_card_order", order, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="card_order.save",
        target="index.card_order",
        result="success",
        remote_admin=remote_admin,
    )
    flash("首页功能卡顺序已更新。", "success")
    return _management_redirect(remote_admin, "content")


def _handle_sponsor_submit(*, remote_admin: bool):
    """首页「赞助」按钮开关：开=首页顶栏显示赞助按钮 + 友情打赏弹层 / 关=隐藏（后端 /donate 仍在）。
    只动 index_sponsor_enabled 设置项；仅网站 /admin 可改。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，首页赞助按钮请在网站 /admin 管理。")
    enabled = _form_bool("sponsor_enabled")
    set_setting(SPONSOR_ENABLED_KEY, "1" if enabled else "0", updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="sponsor.save",
        target="index.sponsor",
        result="success",
        remote_admin=remote_admin,
    )
    flash("首页赞助按钮设置已更新。", "success")
    return _management_redirect(remote_admin, "content")


def _handle_registry_geo_submit(*, remote_admin: bool):
    """公告下方「注册用户分布」卡片：开关 + 注册总数显示方式（exact/fuzzy/custom）。
    只动 index_registry_geo_enabled 与 index_registry_geo_count 两个设置项；仅网站 /admin 可改。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，注册分布卡片请在网站 /admin 管理。")
    enabled = _form_bool("registry_geo_enabled")
    mode = str(request.form.get("registry_geo_mode") or "exact").strip().lower()
    if mode not in {"exact", "fuzzy", "custom"}:
        mode = "exact"
    custom = str(request.form.get("registry_geo_custom") or "").strip()[:40]
    actor = _management_actor_label(remote_admin)
    set_setting(REGISTRY_GEO_SETTING_KEY, "1" if enabled else "0", updated_by=actor)
    set_setting(REGISTRY_GEO_COUNT_KEY, {"mode": mode, "custom": custom}, updated_by=actor)
    _log_management_action(
        action="registry_geo.save",
        target="index.registry_geo",
        result="success",
        remote_admin=remote_admin,
    )
    flash("注册用户分布卡片设置已更新。", "success")
    return _management_redirect(remote_admin, "content")


def _handle_ai_assistant_mode_submit(*, remote_admin: bool):
    """「AI 随心问」展示形态：card=首页固定卡片(其它页仍抽屉) / drawer=全站右侧抽屉。
    只动 index_ai_assistant_mode 设置项；仅网站 /admin 可改。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，AI 随心问形态请在网站 /admin 管理。")
    mode = "drawer" if str(request.form.get("ai_assistant_mode") or "").strip().lower() == "drawer" else "card"
    set_setting(AI_ASSISTANT_MODE_KEY, mode, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="ai_assistant_mode.save",
        target="index.ai_assistant_mode",
        result="success",
        remote_admin=remote_admin,
    )
    flash("AI 随心问展示形态已更新。", "success")
    return _management_redirect(remote_admin, "content")


def _handle_site_text_scan(*, remote_admin: bool):
    """实时重新扫描模板，返回最新的文案框架覆盖情况（供控制台「检测框架变化」按钮调用）。"""
    _require_management_access(remote_admin)
    report = site_text_coverage_report(overrides=_effective_override_values())
    return jsonify({"ok": True, "coverage": report})


def _handle_site_text_prune(*, remote_admin: bool):
    """清理失效文案缓存：删除框架里已不存在、却仍被保存的文案 key。具体内容仍在控制台编辑。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，站点文案请在网站 /admin 管理。")
    if DEPLOYMENT.is_server:
        saved = get_setting("site_texts", {})
        saved = {str(k): str(v) for k, v in saved.items()} if isinstance(saved, dict) else {}
        stale = stale_override_keys(saved)
        if stale:
            remaining = {k: v for k, v in saved.items() if k not in stale}
            set_setting(
                "site_texts",
                remaining,
                updated_by=_management_actor_label(remote_admin),
            )
    else:
        stale = prune_stale_overrides()
    _log_management_action(
        action="site_text.prune",
        target=str(SITE_TEXT_OVERRIDES_PATH),
        result="success",
        remote_admin=remote_admin,
        details={"removed": len(stale)},
    )
    if stale:
        flash(f"已清理 {len(stale)} 条失效文案缓存：{'、'.join(stale[:8])}{'…' if len(stale) > 8 else ''}", "success")
    else:
        flash("没有发现失效文案缓存，无需清理。", "success")
    return _management_redirect(remote_admin, "copy")


def _handle_plans_submit(*, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，套餐请在网站 /admin 管理。")
    try:
        plan = upsert_plan(
            code=(request.form.get("code") or "").strip(),
            name=(request.form.get("name") or "").strip(),
            price_cents=_form_int("price_cents", 0),
            currency=(request.form.get("currency") or "CNY").strip() or "CNY",
            interval_months=_form_int("interval_months", 1),
            description=(request.form.get("description") or "").strip(),
            daily_ai_token_limit=_form_optional_int("daily_ai_token_limit"),
            daily_zhipu_token_limit=_form_optional_int("daily_zhipu_token_limit"),
            features=(request.form.get("features") or "").strip(),
            badge=(request.form.get("badge") or "").strip(),
            is_active=_form_bool("is_active"),
            sort_order=_form_int("sort_order", 0),
            kind=(request.form.get("kind") or "membership").strip(),
            research_credits=_form_int("research_credits", 0),
            chat_credits=_form_int("chat_credits", 0),
            reader_credits=_form_int("reader_credits", 0),
        )
    except ValueError as exc:
        _log_management_action(
            action="plan.save",
            target=(request.form.get("code") or "").strip() or "<new>",
            result="invalid_input",
            remote_admin=remote_admin,
            details={"error": str(exc)},
        )
        flash(str(exc), "warning")
        return _management_redirect(remote_admin, "plans")

    _log_management_action(
        action="plan.save",
        target=str(plan.get("code") or ""),
        result="success",
        remote_admin=remote_admin,
        details={"name": str(plan.get("name") or ""), "is_active": bool(plan.get("is_active"))},
    )
    flash(f"套餐 {plan.get('name') or plan.get('code') or ''} 已保存。", "success")
    return _management_redirect(remote_admin, "plans")


def _handle_research_quota_submit(*, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，研究型检索额度请在网站 /admin 管理。")
    try:
        values = {
            key: _form_int(f"research_quota_{key}", default)
            for key, default in RESEARCH_WEEKLY_QUOTA_DEFAULTS.items()
        }
    except ValueError:
        flash("研究型检索次数必须是有效数字。", "warning")
        return _management_redirect(remote_admin, "members")
    values = {key: max(0, int(value)) for key, value in values.items()}
    set_setting(RESEARCH_WEEKLY_QUOTA_SETTING_KEY, values, updated_by=_management_actor_label(remote_admin))
    # 总开关：勾选＝启用按次数限制；不勾选＝仅按 AI token 额度计量、不限次数（默认关）。
    count_limit_enabled = bool(request.form.get("research_count_limit_enabled"))
    set_setting(RESEARCH_COUNT_LIMIT_ENABLED_SETTING_KEY, count_limit_enabled, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="research_quota.save",
        target=RESEARCH_WEEKLY_QUOTA_SETTING_KEY,
        result="success",
        remote_admin=remote_admin,
        details={**values, "count_limit_enabled": count_limit_enabled},
    )
    flash(
        "研究型检索：已启用按次数限制并保存分档额度。"
        if count_limit_enabled
        else "研究型检索：已关闭次数限制，改为纯 AI token 额度计量（分档次数已保存，开启后即生效）。",
        "success",
    )
    return _management_redirect(remote_admin, "members")


def _handle_reset_research_quota_submit(*, remote_admin: bool):
    """重置「研究级检索」本周已用次数（非破坏式：写重置标记，不删 ai_usage、不退 token 额度）。

    范围 scope：all＝全体注册用户；registered/monthly/quarterly/yearly＝某会员档位；user＝按邮箱指定。
    标记设为当前 UTC 时间，计数从该点起算 → 目标用户本周已用归零、恢复满额；下周一窗口推进后自动失效。
    """
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，研究级检索次数请在网站 /admin 管理。")
    scope = (request.form.get("scope") or "").strip().lower()
    if scope not in RESEARCH_QUOTA_RESET_SCOPES:
        flash("请选择有效的重置范围。", "warning")
        return _management_redirect(remote_admin, "members")
    resets = _research_quota_resets()
    now_ts = utc_now_text()
    if scope == "user":
        email = normalize_email(request.form.get("user_email") or "")
        if not email:
            flash("请填写要重置的用户邮箱。", "warning")
            return _management_redirect(remote_admin, "members")
        if not get_user_by_email(email):
            flash(f"未找到邮箱为 {email} 的用户，未做任何更改。", "warning")
            return _management_redirect(remote_admin, "members")
        resets.setdefault("users", {})[email] = now_ts
        target_label = email
    elif scope == "all":
        resets["all"] = now_ts
        target_label = "全体注册用户"
    else:
        resets.setdefault("tiers", {})[scope] = now_ts
        target_label = RESEARCH_WEEKLY_QUOTA_LABELS.get(scope, scope)
    _prune_quota_resets(resets)
    set_setting(RESEARCH_QUOTA_RESETS_SETTING_KEY, resets, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="research_quota.reset",
        target=f"{scope}:{target_label}",
        result="success",
        remote_admin=remote_admin,
        details={"scope": scope, "target": target_label},
    )
    flash(f"已重置「{target_label}」的本周研究级检索次数（恢复满额，下周一自动失效）。", "success")
    return _management_redirect(remote_admin, "members")


def _handle_ai_token_quota_submit(*, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，AI 每日额度请在网站 /admin 管理。")
    try:
        values = {
            key: _form_int(f"ai_token_{key}", default)
            for key, default in AI_TOKEN_DAILY_DEFAULTS.items()
        }
    except ValueError:
        flash("每日 AI token 额度必须是有效数字。", "warning")
        return _management_redirect(remote_admin, "members")
    values = {key: max(0, int(value)) for key, value in values.items()}
    set_setting(AI_TOKEN_DAILY_SETTING_KEY, values, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="ai_token_quota.save",
        target=AI_TOKEN_DAILY_SETTING_KEY,
        result="success",
        remote_admin=remote_admin,
        details=values,
    )
    flash("每日 AI token 额度已保存。", "success")
    return _management_redirect(remote_admin, "members")


def _handle_reset_ai_token_quota_submit(*, remote_admin: bool):
    """重置「本周 AI 额度」已用量（非破坏式：写重置标记，不删 ai_usage、不改分档额度）。

    范围 scope：all＝全体（含未登录访客）；registered_all＝全部注册用户（含会员）；
    members＝全部会员（月/季/年三档）；guest/registered/monthly/quarterly/yearly＝单一档位；
    user＝按邮箱指定（可一次多个）。
    标记设为当前 UTC 时刻，此后统计已用 token 只从该点起算 → 目标用户本周已用归零、恢复满额；
    下周一窗口推进后自然失效。研究级检索次数是另一套计数，不受此操作影响。
    """
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，AI 额度请在网站 /admin 管理。")
    scope = (request.form.get("scope") or "").strip().lower()
    if scope not in AI_TOKEN_QUOTA_RESET_SCOPES:
        flash("请选择有效的重置范围。", "warning")
        return _management_redirect(remote_admin, "members")
    resets = _ai_token_quota_resets()
    now_ts = utc_now_text()
    missing: list[str] = []
    if scope == "user":
        raw_emails = request.form.get("emails") or request.form.get("user_email") or ""
        candidates: list[str] = []
        for item in re.split(r"[\s,;，、；]+", raw_emails):
            email = normalize_email(item)
            if email and email not in candidates:
                candidates.append(email)
        if not candidates:
            flash("请填写要重置的用户邮箱（可一行一个，或用逗号分隔）。", "warning")
            return _management_redirect(remote_admin, "members")
        hit: list[str] = []
        for email in candidates:
            if get_user_by_email(email):
                hit.append(email)
            else:
                missing.append(email)
        if not hit:
            flash(f"未找到这些邮箱对应的用户，未做任何更改：{'、'.join(missing)}", "warning")
            return _management_redirect(remote_admin, "members")
        users_map = resets.setdefault("users", {})
        for email in hit:
            users_map[email] = now_ts
        target_label = "、".join(hit) if len(hit) <= 5 else f"{'、'.join(hit[:5])} 等 {len(hit)} 位用户"
    elif scope == "all":
        resets["all"] = now_ts
        target_label = AI_TOKEN_QUOTA_RESET_SCOPE_LABELS["all"]
    elif scope in AI_TOKEN_QUOTA_RESET_SCOPE_GROUPS:
        tiers = resets.setdefault("tiers", {})
        for bucket in AI_TOKEN_QUOTA_RESET_SCOPE_GROUPS[scope]:
            tiers[bucket] = now_ts
        target_label = AI_TOKEN_QUOTA_RESET_SCOPE_LABELS[scope]
    else:
        resets.setdefault("tiers", {})[scope] = now_ts
        target_label = AI_TOKEN_QUOTA_RESET_SCOPE_LABELS.get(scope, scope)
    _prune_quota_resets(resets)
    set_setting(AI_TOKEN_QUOTA_RESETS_SETTING_KEY, resets, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="ai_token_quota.reset",
        target=f"{scope}:{target_label}",
        result="success",
        remote_admin=remote_admin,
        details={"scope": scope, "target": target_label, "missing_emails": missing},
    )
    message = f"已重置「{target_label}」的本周 AI 额度（已用量归零、恢复满额，不影响使用明细）。"
    if missing:
        message += f" 未找到以下邮箱、已跳过：{'、'.join(missing)}"
    flash(message, "success")
    return _management_redirect(remote_admin, "members")


def _handle_membership_grant_submit(*, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，会员请在网站 /admin 管理。")
    user_email = normalize_email(request.form.get("user_email") or "")
    plan_code = (request.form.get("plan_code") or "").strip()
    note = (request.form.get("note") or "").strip()
    try:
        result = create_manual_subscription(user_email=user_email, plan_code=plan_code, note=note)
    except ValueError as exc:
        _log_management_action(
            action="membership.grant",
            target=user_email or "<missing>",
            result="invalid_input",
            remote_admin=remote_admin,
            details={"error": str(exc), "plan_code": plan_code},
        )
        flash(str(exc), "warning")
        return _management_redirect(remote_admin, "members")

    subscription = result.get("subscription") or {}
    _log_management_action(
        action="membership.grant",
        target=user_email,
        result="success",
        remote_admin=remote_admin,
        details={"plan_code": plan_code, "expires_at": subscription.get("expires_at") or ""},
    )
    flash(
        f"已为 {user_email} 开通 {subscription.get('plan_name') or plan_code}，到期 {subscription.get('expires_at') or '已更新'}。",
        "success",
    )
    return _management_redirect(remote_admin, "members")


def _handle_membership_bulk_grant_submit(*, remote_admin: bool):
    """后台「一键赠送 / 续期 / 升级会员」：给全部注册用户、全部有效会员或指定邮箱批量发放会员。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，会员请在网站 /admin 管理。")
    scope = (request.form.get("scope") or "").strip()
    plan_code = (request.form.get("plan_code") or "").strip()
    note = (request.form.get("note") or "").strip()
    emails = [item for item in re.split(r"[\s,;，、；]+", request.form.get("emails") or "") if item.strip()]
    extra_days_raw = (request.form.get("extra_days") or "").strip()
    extra_days: int | None = None
    if extra_days_raw:
        try:
            extra_days = int(extra_days_raw)
        except ValueError:
            flash("延长天数必须是整数。", "warning")
            return _management_redirect(remote_admin, "members")
    # 大范围赠送（全部注册用户）不可撤销，必须显式勾选确认框，前后端双重把关。
    if scope == "all_registered" and not _form_bool("confirm_all"):
        flash("赠送给「全部注册用户」需先勾选确认框。", "warning")
        return _management_redirect(remote_admin, "members")
    if scope == "emails" and not emails:
        flash("请填写至少一个目标邮箱。", "warning")
        return _management_redirect(remote_admin, "members")
    try:
        result = bulk_grant_membership(
            scope=scope,
            plan_code=plan_code,
            emails=emails,
            extra_days=extra_days,
            note=note,
        )
    except (ValueError, KeyError) as exc:
        _log_management_action(
            action="membership.bulk_grant",
            target=scope or "<missing>",
            result="invalid_input",
            remote_admin=remote_admin,
            details={"error": str(exc), "plan_code": plan_code},
        )
        flash(str(exc), "warning")
        return _management_redirect(remote_admin, "members")

    _log_management_action(
        action="membership.bulk_grant",
        target=scope,
        result="success",
        remote_admin=remote_admin,
        details={
            "plan_code": plan_code,
            "granted": result["granted"],
            "skipped_non_member": result["skipped_non_member"],
            "missing_emails": len(result["missing_emails"]),
            "extra_days": extra_days,
        },
    )
    verb = "延长会员" if result["keep_tier"] else "开通 / 续期会员"
    parts = [f"已为 {result['granted']} 名用户{verb}"]
    if not result["keep_tier"]:
        parts.append(f"（{result['plan_name']}）")
    if extra_days:
        parts.append(f"，时长 {extra_days} 天")
    if result["skipped_non_member"]:
        parts.append(f"；跳过 {result['skipped_non_member']} 名非会员（无现有档次可沿用）")
    if result["missing_emails"]:
        preview = "、".join(result["missing_emails"][:5])
        more = "…" if len(result["missing_emails"]) > 5 else ""
        parts.append(f"；未找到 {len(result['missing_emails'])} 个邮箱（{preview}{more}）")
    if not result["granted"]:
        flash("".join(parts) + "。未发放任何会员，请检查目标范围与参数。", "warning")
    else:
        flash("".join(parts) + "。", "success")
    return _management_redirect(remote_admin, "members")


def _handle_user_update_submit(user_id: int, *, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，用户请在网站 /admin 管理。")
    user_q = (request.form.get("user_q") or "").strip()
    try:
        updated = update_user_account(
            user_id,
            role=(request.form.get("role") or "member").strip() or "member",
            is_active=_form_bool("is_active"),
            daily_ai_token_limit_override=_form_optional_int("daily_ai_token_limit_override"),
        )
    except ValueError as exc:
        _log_management_action(
            action="user.update",
            target=str(user_id),
            result="invalid_input",
            remote_admin=remote_admin,
            details={"error": str(exc)},
        )
        flash(str(exc), "warning")
        return _management_redirect(remote_admin, "users", user_q=user_q)
    if updated is None:
        _log_management_action(
            action="user.update",
            target=str(user_id),
            result="not_found",
            remote_admin=remote_admin,
        )
        flash("未找到需要更新的用户。", "warning")
    else:
        _log_management_action(
            action="user.update",
            target=str(user_id),
            result="success",
            remote_admin=remote_admin,
            details={"email": str(updated.get("email") or ""), "role": str(updated.get("role") or "")},
        )
        flash(f"用户 {updated.get('email') or user_id} 已更新。", "success")
    return _management_redirect(remote_admin, "users", user_q=user_q)


def _handle_access_policy_submit(*, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，会员权限请在网站 /admin 管理。")

    policy = _load_access_policy()
    action = (request.form.get("action") or "save_global").strip().lower()
    if action == "save_global":
        policy["global"] = {key: _form_bool(f"global_{key}") for key in FEATURE_ACCESS_KEYS}
        flash("全站会员功能权限已保存。", "success")
    elif action == "save_plans":
        audience_values: dict[str, dict[str, bool]] = {}
        for audience_key in AUDIENCE_ACCESS_LABELS:
            audience_values[audience_key] = {
                key: _form_bool(f"audience_{audience_key}_{key}")
                for key in FEATURE_ACCESS_KEYS
            }
        policy["audience"] = audience_values
        plan_values: dict[str, dict[str, bool]] = {}
        for plan in list_plans(include_inactive=True):
            code = str(plan.get("code") or "").strip()
            if not code:
                continue
            plan_values[code] = {key: _form_bool(f"plan_{code}_{key}") for key in FEATURE_ACCESS_KEYS}
        policy["plans"] = plan_values
        flash("套餐功能开放范围已保存。", "success")
    elif action == "save_user":
        email = normalize_email(request.form.get("user_email") or "")
        if not email:
            flash("缺少用户邮箱，无法保存个别权限。", "warning")
            return _management_redirect(remote_admin, "members", user_q=(request.form.get("user_q") or "").strip())
        user_values: dict[str, bool | None] = {}
        for key in FEATURE_ACCESS_KEYS:
            raw = (request.form.get(f"user_{key}") or "inherit").strip().lower()
            if raw == "allow":
                user_values[key] = True
            elif raw == "deny":
                user_values[key] = False
            else:
                user_values[key] = None
        policy.setdefault("users", {})[email] = user_values
        flash(f"{email} 的个别功能权限已保存。", "success")
    elif action == "reset_user":
        email = normalize_email(request.form.get("user_email") or "")
        if email:
            policy.setdefault("users", {}).pop(email, None)
            flash(f"{email} 已恢复使用全站默认权限。", "success")
    else:
        flash("未知的权限操作。", "warning")

    set_setting("access_policy", policy, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="access_policy.save",
        target=action,
        result="success",
        remote_admin=remote_admin,
    )
    target_section = "plan-access" if action == "save_plans" else "members"
    return _management_redirect(remote_admin, target_section, user_q=(request.form.get("user_q") or "").strip())


def _handle_ai_access_toggle(*, remote_admin: bool):
    """总览页一键封禁/解封某用户的 AI 使用。写入的是同一份 access_policy.users[email].ai，
    因此与「会员与权限」里的个别权限完全联通：在哪边改，另一边都会同步反映。"""
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="本地控制台只负责诊断和同步，会员权限请在网站 /admin 管理。")
    email = normalize_email(request.form.get("user_email") or "")
    action = (request.form.get("action") or "ban").strip().lower()
    selected_date = _parse_dashboard_day(request.form.get("date", ""), "")
    redirect_params = {"date": selected_date} if selected_date else {}
    if not email:
        flash("缺少用户邮箱，无法调整 AI 权限。", "warning")
        return _management_redirect(remote_admin, "overview", **redirect_params)

    policy = _load_access_policy()
    users = policy.setdefault("users", {})
    current = dict(users.get(email) or {})
    if action == "unban":
        # 恢复为「跟随全站默认」，而非强制允许，避免越权覆盖套餐/全站策略。
        # 四项 AI 能力同步恢复：封禁时一起停（见下），解封也一起回到继承态。
        current["ai"] = None
        current["search_chat"] = None
        current["associative"] = None
        current["research"] = None
        result = "unban"
        message = f"已恢复 {email} 的 AI 使用权限（跟随全站默认）。"
    else:
        # 「暂停 AI」语义覆盖全部 AI 能力：阅读器导学（ai）、首页随心问（search_chat）、
        # 联想检索（associative）、研究型检索（research）一并停用，避免封禁后仍可消耗 AI 配额。
        current["ai"] = False
        current["search_chat"] = False
        current["associative"] = False
        current["research"] = False
        result = "ban"
        message = f"已暂停 {email} 的 AI 使用：阅读器 AI 导学、首页随心问、联想检索与研究型检索均不可用，其余功能不受影响。"
    users[email] = current
    set_setting("access_policy", policy, updated_by=_management_actor_label(remote_admin))
    _log_management_action(
        action="ai_access.toggle",
        target=email,
        result=result,
        remote_admin=remote_admin,
    )
    flash(message, "success")
    return _management_redirect(remote_admin, "overview", **redirect_params)


def _handle_reader_access_ban(*, remote_admin: bool):
    _require_management_access(remote_admin)
    _require_management_csrf()
    if not remote_admin:
        abort(403, description="阅读器封禁只能在网站后台操作。")
    action = (request.form.get("action") or "ban").strip().lower()
    selected_date = _parse_dashboard_day(request.form.get("date", ""), "")
    redirect_params = {"date": selected_date} if selected_date else {}
    actor_type = (request.form.get("actor_type") or "").strip().lower()
    user_id_text = (request.form.get("user_id") or "").strip()
    email = normalize_email(request.form.get("email") or "")
    client_ip = (request.form.get("client_ip") or "").strip()

    user: dict | None = None
    if user_id_text:
        try:
            user = get_user_by_id(int(user_id_text))
        except (TypeError, ValueError):
            user = None
    if user is None and email:
        user = get_user_by_email(email)
    if user is not None:
        actor_type = "user"
        email = normalize_email(str(user.get("email") or email))
        user_id_text = str(user.get("id") or user_id_text)
    elif actor_type != "ip":
        actor_type = "ip" if client_ip else actor_type

    if actor_type == "user" and not email:
        flash("缺少用户邮箱，无法调整阅读器访问。", "warning")
        return _management_redirect(remote_admin, "overview", **redirect_params)
    if actor_type != "user" and not client_ip:
        flash("缺少访客 IP，无法调整阅读器访问。", "warning")
        return _management_redirect(remote_admin, "overview", **redirect_params)

    now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bans = _reader_bans()
    policy = _load_access_policy()
    if actor_type == "user":
        users = policy.setdefault("users", {})
        current = dict(users.get(email) or {})
        user_bans = _reader_user_bans(bans)
        if action == "unban":
            current["library"] = None
            user_bans.pop(user_id_text, None)
            result = "unban"
            message = f"已恢复 {email} 的阅读器访问权限（跟随全站默认）。"
        else:
            current["library"] = False
            user_bans[user_id_text] = {
                "email": email,
                "banned_at": now_text,
                "banned_by": _management_actor_label(remote_admin),
            }
            result = "ban"
            message = f"已暂停 {email} 的阅读器访问。"
        users[email] = current
        target = email
        set_setting("access_policy", policy, updated_by=_management_actor_label(remote_admin))
    else:
        ip_bans = _reader_ip_bans(bans)
        if action == "unban":
            ip_bans.pop(client_ip, None)
            result = "unban"
            message = f"已恢复 {client_ip} 的阅读器访问。"
        else:
            ip_bans[client_ip] = {
                "banned_at": now_text,
                "banned_by": _management_actor_label(remote_admin),
            }
            result = "ban"
            message = f"已暂停 {client_ip} 的阅读器访问。"
        target = client_ip
    _save_reader_bans(bans)
    _log_management_action(
        action="reader_access.toggle",
        target=target,
        result=result,
        remote_admin=remote_admin,
    )
    flash(message, "success")
    return _management_redirect(remote_admin, "overview", **redirect_params)


_JOURNAL_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_JOURNAL_AUDIENCE_LABELS = {
    "subscribers": "邮箱订阅者", "members": "付费会员", "registered": "全部注册用户",
}


def _journal_autosend_status(settings: dict, batch: dict | None, smtp_enabled: bool) -> dict:
    """生成「综述是否会按时自动群发」的状态提示（控制台综述审核区展示）。"""
    freq = str(settings.get("send_frequency") or "weekly").lower()
    weekday = int(settings.get("send_weekday") or 0)
    send_time = str(settings.get("send_time") or "08:00")
    audience = str(settings.get("send_audience") or "subscribers").lower()
    plans = list(settings.get("send_audience_plans") or [])
    wd = _JOURNAL_WEEKDAY_NAMES[weekday] if 0 <= weekday < 7 else "周一"
    when = {
        "daily": f"每天 {send_time}",
        "weekly": f"每周{wd} {send_time}",
        "biweekly": f"每两周{wd} {send_time}",
        "monthly": f"每月 {send_time}",
    }.get(freq, f"每周{wd} {send_time}")

    audience_label = _JOURNAL_AUDIENCE_LABELS.get(audience, audience)
    if audience == "members" and plans:
        audience_label += "（" + "、".join(plans) + "）"
    try:
        recipients, _ = resolve_journal_recipients(audience, plan_codes=plans, emails=[])
        count = len(recipients)
    except Exception:
        count = -1
    count_text = f"约 {count} 人" if count >= 0 else "人数未知"

    blockers: list[str] = []
    if not smtp_enabled:
        blockers.append("发信邮箱未配置")
    if count == 0:
        blockers.append(f"当前「{audience_label}」收件人为 0 人（请改默认受众或先添加收件人）")

    review_status = str((batch or {}).get("review_status") or "none")
    if not batch:
        return {"level": "muted", "text": "暂无批次：请先「① 立即采集本期」或等待发送日前一天 19:00 自动采集。"}
    if bool(settings.get("automation_paused")):
        return {"level": "paused",
                "text": "⏸ 自动化已暂停（总闸开启）：不会自动群发；手动按钮仍可用。关闭「暂停自动化」后恢复。"}
    if review_status == "approved":
        text = f"✅ 综述已批准：将于 {when}（北京时间）自动群发给「{audience_label}」（{count_text}）。"
        if blockers:
            return {"level": "warn", "text": text + " ⚠ 但：" + "；".join(blockers) + "。"}
        return {"level": "ok", "text": text}
    text = (f"⏳ 综述尚未批准（当前状态：{review_status}）：不会自动群发。"
            f"点「保存并批准」后，将于 {when} 自动发送给「{audience_label}」（{count_text}）。")
    if blockers:
        text += " 另外：" + "；".join(blockers) + "。"
    return {"level": "pending", "text": text}


def _handle_journal_alert_settings_submit():
    _require_admin()
    _require_management_csrf()
    values = normalize_alert_settings(
        {
            "subject_prefix": request.form.get("subject_prefix") or "",
            "intro_text": request.form.get("intro_text") or "",
            "include_title": _form_bool("include_title"),
            "include_journal": _form_bool("include_journal"),
            "include_authors": _form_bool("include_authors"),
            "include_published_at": _form_bool("include_published_at"),
            "include_abstract": _form_bool("include_abstract"),
            "include_citation": _form_bool("include_citation"),
            "include_url": _form_bool("include_url"),
            "auto_publish_all": _form_bool("auto_publish_all"),
            "send_frequency": request.form.get("send_frequency") or "weekly",
            "send_weekday": _form_int("send_weekday", 0),
            "lookback_days": _form_int("lookback_days", 30),
            "weekly_release_cap": _form_int("weekly_release_cap", 45),
            "send_time": request.form.get("send_time") or "08:00",
            "auto_approve_articles": _form_bool("auto_approve_articles"),
            "auto_generate_review": _form_bool("auto_generate_review"),
            "auto_send": _form_bool("auto_send"),
            "automation_paused": _form_bool("automation_paused"),
            "hard_delete_archived": _form_bool("hard_delete_archived"),
            "review_model": request.form.get("review_model") or "",
            "send_audience": request.form.get("send_audience") or "subscribers",
            "send_audience_plans": request.form.getlist("send_audience_plans"),
        }
    )
    set_setting("journal_alerts_settings", values, updated_by=_management_actor_label(True))
    _log_management_action(
        action="journal_alerts.settings.save",
        target="journal_alerts_settings",
        result="success",
        remote_admin=True,
    )
    flash("期刊提醒发送内容已保存。", "success")
    return _management_redirect(True, "journal-alerts")


def _handle_desktop_device_create():
    _require_admin()
    _require_management_csrf()
    try:
        device = create_desktop_device(
            label=(request.form.get("label") or "").strip(),
            user_email=normalize_email(request.form.get("user_email") or ""),
            expires_at=(request.form.get("expires_at") or "").strip(),
            notes=(request.form.get("notes") or "").strip(),
            activation_code=(request.form.get("activation_code") or "").strip(),
        )
    except Exception as exc:
        flash(f"设备授权创建失败：{exc}", "warning")
        return _management_redirect(True, "devices")
    _log_management_action(
        action="desktop_device.create",
        target=str(device.get("activation_code") or device.get("id") or ""),
        result="success",
        remote_admin=True,
    )
    flash(f"设备授权已创建，授权码：{device.get('activation_code')}", "success")
    return _management_redirect(True, "devices")


def _handle_desktop_device_update(device_id: int):
    _require_admin()
    _require_management_csrf()
    device = update_desktop_device(
        device_id,
        label=(request.form.get("label") or "").strip(),
        user_email=normalize_email(request.form.get("user_email") or ""),
        status=(request.form.get("status") or "active").strip(),
        expires_at=(request.form.get("expires_at") or "").strip(),
        notes=(request.form.get("notes") or "").strip(),
    )
    if device is None:
        flash("未找到设备授权。", "warning")
    else:
        _log_management_action(
            action="desktop_device.update",
            target=str(device_id),
            result="success",
            remote_admin=True,
            details={"status": str(device.get("status") or "")},
        )
        flash("设备授权已更新。", "success")
    return _management_redirect(True, "devices")


def _handle_release_create():
    _require_admin()
    _require_management_csrf()
    try:
        release = create_release(
            channel=(request.form.get("channel") or "stable").strip(),
            app_version=(request.form.get("app_version") or "").strip(),
            data_version=(request.form.get("data_version") or "").strip(),
            download_url=(request.form.get("download_url") or "").strip(),
            sha256=(request.form.get("sha256") or "").strip(),
            size_bytes=_form_int("size_bytes", 0),
            force_update=_form_bool("force_update"),
            notes=(request.form.get("notes") or "").strip(),
            is_active=_form_bool("is_active"),
        )
    except Exception as exc:
        flash(f"发布记录创建失败：{exc}", "warning")
        return _management_redirect(True, "releases")
    _log_management_action(
        action="desktop_release.create",
        target=str(release.get("app_version") or release.get("id") or ""),
        result="success",
        remote_admin=True,
    )
    flash("发布记录已保存，本地端下次同步即可看到。", "success")
    return _management_redirect(True, "releases")


def _handle_release_upload():
    _require_admin()
    _require_management_csrf()
    upload = request.files.get("release_file")
    if upload is None or not upload.filename:
        flash("请选择需要上传的安装包或资料包。", "warning")
        return _management_redirect(True, "releases")
    filename = secure_filename(upload.filename)
    if not filename:
        flash("上传文件名无效。", "warning")
        return _management_redirect(True, "releases")
    suffix = Path(filename).suffix.lower()
    if suffix not in RELEASE_UPLOAD_EXTENSIONS:
        flash("上传文件类型不在允许范围内。", "warning")
        return _management_redirect(True, "releases")
    try:
        max_upload_mb = max(1, int(os.environ.get("MAX_RELEASE_UPLOAD_MB") or "200"))
    except ValueError:
        max_upload_mb = 200
    request_size = request.content_length or 0
    if request_size and request_size > max_upload_mb * 1024 * 1024:
        flash(f"上传文件不能超过 {max_upload_mb} MB。", "warning")
        return _management_redirect(True, "releases")
    release_dir = RUNTIME_ROOT / "releases"
    release_dir.mkdir(parents=True, exist_ok=True)
    target = release_dir / filename
    upload.save(target)
    if target.stat().st_size > max_upload_mb * 1024 * 1024:
        target.unlink(missing_ok=True)
        flash(f"上传文件不能超过 {max_upload_mb} MB。", "warning")
        return _management_redirect(True, "releases")
    public_base = (DEPLOYMENT.public_base_url or "").rstrip("/")
    download_url = f"{public_base}/releases/{filename}" if public_base else str(target)
    try:
        digest = compute_sha256(target)
        size_bytes = target.stat().st_size
        release = create_release(
            channel=(request.form.get("channel") or "stable").strip(),
            app_version=(request.form.get("app_version") or APP_VERSION).strip(),
            data_version=(request.form.get("data_version") or "").strip(),
            download_url=download_url,
            sha256=digest,
            size_bytes=size_bytes,
            force_update=_form_bool("force_update"),
            notes=(request.form.get("notes") or "").strip(),
            is_active=True,
        )
    except Exception as exc:
        flash(f"上传后登记发布失败：{exc}", "warning")
        return _management_redirect(True, "releases")
    _log_management_action(
        action="desktop_release.upload",
        target=str(release.get("app_version") or filename),
        result="success",
        remote_admin=True,
        details={"filename": filename, "size_bytes": size_bytes},
    )
    flash("文件已上传并登记为发布记录。", "success")
    return _management_redirect(True, "releases")


def _handle_release_update(release_id: int):
    _require_admin()
    _require_management_csrf()
    release = update_release_status(release_id, is_active=_form_bool("is_active"))
    if release is None:
        flash("未找到发布记录。", "warning")
    else:
        _log_management_action(
            action="desktop_release.update",
            target=str(release_id),
            result="success",
            remote_admin=True,
            details={"is_active": bool(release.get("is_active"))},
        )
        flash("发布记录状态已更新。", "success")
    return _management_redirect(True, "releases")


def _get_page_context_payload(source_file: str, page_number: int) -> dict:
    _require_search()
    _require_full_mode()
    # 页面文字与 AI 导读上下文均来自语料库（corpus.sqlite），并不依赖原始 PDF 文件。
    # 因此这里只做「白名单 + 完整资料模式」校验，而不要求 PDF 实体存在，
    # 以便《全集》等仅保留 OCR 文本的卷册也能加载正文与 AI 导读。
    rel = _normalize_source_file(source_file)
    if not rel or rel not in ALLOWED_SOURCE_FILES:
        abort(404, description="请求的资料不在白名单中。")
    source_file = rel
    volume = corpus.get_volume_by_source_file(source_file) if corpus else None
    if volume is None:
        abort(404, description="未找到对应的卷册信息。")

    page_obj = None
    page_index = -1
    for idx, candidate in enumerate(volume.pages):
        if candidate.pdf_page == page_number:
            page_obj = candidate
            page_index = idx
            break
    if page_obj is None:
        abort(404, description="请求页码超出 PDF 范围。")

    previous_text = volume.pages[page_index - 1].raw_text if page_index > 0 else ""
    next_text = volume.pages[page_index + 1].raw_text if page_index < len(volume.pages) - 1 else ""
    section_title = corpus.get_section_for_page(source_file, page_number) if corpus else None
    citation = corpus._make_citation(volume.book, volume.volume, [page_obj], source_file=source_file) if corpus else ""
    page_label = page_obj.printed_page or f"PDF-{page_number}"
    # 公文类书库（党代会报告/全会公报）的权威原文来源链接（供阅读器「原文来源」展示）
    source_url = ""
    try:
        meta = (getattr(corpus, "party_meta", {}) or {}).get(volume.book, {}) or {}
        source_url = (meta.get(volume.volume, {}) or {}).get("url", "") if corpus else ""
    except Exception:
        source_url = ""

    return {
        "source_file": source_file,
        "display_title": volume.display_title,
        "book": volume.book,
        **_book_payload(volume.book),
        "volume": volume.volume,
        "page": page_number,
        "page_label": page_label,
        "section_title": section_title or "",
        "citation": citation,
        "source_url": source_url,
        "current_text": _clean_text(page_obj.raw_text),
        "previous_excerpt": _clean_text(previous_text, limit=240),
        "next_excerpt": _clean_text(next_text, limit=240),
    }


def _page_image_cache_path(source_file: str, page_number: int, query_text: str, fmt: str = "jpg") -> Path:
    pdf_path = _resolve_pdf_path(source_file, require_full_mode=False)
    try:
        st = pdf_path.stat()  # 一次 stat 取两值（原先调了两次）
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "missing"
    # 缓存版本号 v6：v6 把高亮改为「归一化逐字锚定」（修复长句在页图上不标亮/只标亮片段）。
    # v5 曾把普通库改为不锐化、毛选锐化减弱。渲染/高亮结果变了必须改版本号，否则旧缓存继续命中、
    # 新逻辑不生效。profile tag：毛选用独立 tag（+mao4）以便日后单独调参；其余库 tag 为空。
    raw = f"{source_file}|{page_number}|{query_text}|{stamp}|v6{_render_profile(source_file)['tag']}"
    digest = sha256(raw.encode("utf-8")).hexdigest()
    # WebP 与 JPEG 同 digest、仅扩展名不同（同一页两变体各占一条缓存、互不覆盖）。渲染/高亮结果未变，
    # 版本号仍 v6：现有 .jpg 缓存全部保留、继续命中；.webp 变体随 Accept 协商按需懒生成。
    ext = "webp" if fmt == "webp" else "jpg"
    return PAGE_IMAGE_CACHE_DIR / digest[:2] / f"{digest}.{ext}"


# 阅读器页面图像清晰度参数（「按源原生分辨率自适应渲染」方案）。
# 背景：两阅读器最宽显示 960px CSS（高分屏 ≈1920 设备像素）。扫描件每页是一张内嵌图像，
# 其「原生像素宽」才是真实细节上限。实测三库源分辨率差异极大、单一固定倍率无法兼顾：
#   · 马恩全集：窄页源 ~1550px(≈300DPI)，旧逻辑被 3.0× 上限卡到 ~1110px → 丢真实细节、发糊；
#   · 列宁全集：同库各卷源从 675px 到 1630px 不等，固定倍率必然「高清卷渲不够 / 低清卷过上采样」；
#   · 毛  选 ：纯图像扫描、~700px(96DPI)、无文本层，源本身糊，只能上采样 + 锐化做感知提升。
# 策略：探测每页原生像素宽，渲染倍率取「不低于源原生（不丢细节）、且不低于显示下限（填满阅读器）」，
# 再夹到 [MIN, HARD_MAX]。清晰度提升只靠分辨率 + 好插值：普通扫描库不做 USM（叠在灰底低清扫描上
# 会放大底噪显得更糊），仅毛选粗黑体做轻度 USM（按上采样倍数自适应）。
# 注意：刻意只用 PyMuPDF（+ 可选 numpy 锐化，缺失则安全跳过），不引入 Pillow——服务器运行环境
# （requirements.txt）未装 Pillow，若在此路径 import PIL 会触发 ImportError 导致页面渲染 502。
PAGE_IMAGE_DISPLAY_MIN_PX = 1600.0  # 显示下限像素宽：低清源至少上采样到此宽度以填满阅读器
PAGE_IMAGE_MIN_SCALE = 1.45         # 缩放下限：不低于历史清晰度，保证「只会更清晰不会更糊」
PAGE_IMAGE_HARD_MAX_SCALE = 4.8     # 缩放硬上限：护内存/体积（马恩窄页原生≈4.16× 在此之内）
PAGE_IMAGE_JPEG_QUALITY = 90        # JPEG 质量：高分辨率下兼顾文字边缘锐利与体积

# 普通扫描库（马恩全集 / 列宁全集 / 文集 等）：**忠实渲染、不做任何背景/对比度处理**，尽量贴近原 PDF。
# 历史经验（务必勿再走回头路）：列宁约 52 卷是 675–880px 的**灰底**扫描；曾尝试 levels 背景增白把灰底
# 映射成纯白以求"更清"，但用户实测**这恰恰破坏了原始灰底质感、让字更难辨认**——灰底扫描的字本就靠与
# 灰背景的相对反差被读出，强行洗白会压掉这点反差。故这里只按源分辨率自适应缩放 + PyMuPDF 高质量插值
# 输出（low 清卷上采样到 display_min 填满阅读器，高清卷按原生），**不增白、不锐化**，与直接看原 PDF 一致。
# tag 为空（""）= 复用本会话之前的原始缓存键，多数页可直接命中暖缓存、不触发重渲染。
DEFAULT_RENDER = {
    "display_min_px": PAGE_IMAGE_DISPLAY_MIN_PX, "hard_max_scale": PAGE_IMAGE_HARD_MAX_SCALE,
    "jpeg_quality": PAGE_IMAGE_JPEG_QUALITY, "usm_gain": 0.0, "usm_cap": 0.0, "tag": "",
}
# 《毛泽东选集》为纯图像扫描件、~700px、无文本层、粗黑体印刷，源即糊、无真实细节可恢复。用更高
# 显示下限 + **轻度** USM + 略高 JPEG 质量提升观感（粗黑体能受益、且不像列宁灰底那样易出毛刺；
# 实测 0.55 会略放大底噪斑点，降到 0.30 更稳）。锐化依赖 numpy，缺失时安全跳过（绝不 502）。
# tag +mao4 使旧 +mao3（过锐）缓存失效、按新参数重渲染。
MAO_RENDER = {
    "display_min_px": 1900.0, "hard_max_scale": PAGE_IMAGE_HARD_MAX_SCALE,
    "jpeg_quality": 94, "usm_gain": 0.30, "usm_cap": 0.30,
    # 同样补 levels 增白：服务器无 numpy，原 USM 从未真正生效（毛选一直只是放大）；字节 LUT 增白可在线上
    # 加深粗黑体、清掉灰底，真正提升观感。photo_mid_max 给足以免插图页被压暗。tag +mao5 令旧缓存失效。
    "levels": (40.0, 205.0), "photo_mid_max": 0.30, "tag": "+mao5",
}
_MAO_SCAN_PREFIX = "pdfs/《毛泽东选集》/"

# 邓小平 / 江泽民 / 胡锦涛文选 与《治国理政》：均为**清白底**现代扫描件（区别于列宁的灰底扫描），
# 原生分辨率偏低（实测胡选 ~724px、《治国理政》部分扫描卷 ~880px），按显示下限上采样到 ~1900px
# 后若完全不锐化（DEFAULT 的 usm=0）则文字边缘明显发糊。这些扫描背景是**纯白**而非列宁那种灰底，
# 故 USM 只增强笔画边缘、不会把灰底噪点一并放大成毛刺（v5 取消 DEFAULT 锐化正是为列宁灰底而设，
# 不适用于此处）。这里按上采样倍数自适应启用 USM：低清扫描卷（upsample 大）得到较强锐化，已是
# 高清的卷（邓1 ~1698px）或文本层页（native=0 → upsample≈1）自动 ≈0、不过锐。tag +wxsel1 使这些
# 书旧的「无锐化」缓存（tag 为空）失效、按新参数重渲染；其余书库缓存不受影响。
MODERN_SCAN_RENDER = {
    "display_min_px": 1900.0, "hard_max_scale": PAGE_IMAGE_HARD_MAX_SCALE,
    "jpeg_quality": 92, "usm_gain": 0.5, "usm_cap": 0.5,
    # 关键修正：原 +wxsel1 只配了 USM，而服务器无 numpy → USM 从未生效、这批反而只是被放大到 1900px
    # 更软。改用字节 LUT 增白（线上真生效）：白底现代扫描增白幅度温和（白点 205）以免笔画变细。
    "levels": (40.0, 205.0), "photo_mid_max": 0.28, "tag": "+wxsel2",
}
_MODERN_SCAN_PREFIXES = (
    "pdfs/邓小平文选/", "pdfs/江泽民文选/", "pdfs/胡锦涛文选/", "pdfs/《治国理政》/",
)
_NUMPY_MODULE = "__unset__"  # 惰性探测结果缓存：模块对象或 None


def _numpy_or_none():
    global _NUMPY_MODULE
    if _NUMPY_MODULE == "__unset__":
        try:
            import numpy as _np  # 仅在可用时启用锐化；服务器未装则优雅降级
            _NUMPY_MODULE = _np
        except Exception:
            _NUMPY_MODULE = None
    return _NUMPY_MODULE


_PIL_IMAGE_MODULE = "__unset__"  # 惰性探测结果缓存：PIL.Image 模块或 None


def _pillow_or_none():
    """惰性探测 Pillow（仅用于 /page-image 的 WebP 编码）。未装 Pillow、或装了但缺 WebP 支持，
    一律返回 None；调用方据此回退现行 JPEG，绝不 502。与 _numpy_or_none 同款惰性单例。"""
    global _PIL_IMAGE_MODULE
    if _PIL_IMAGE_MODULE == "__unset__":
        try:
            from PIL import Image as _Image, features as _features
            _PIL_IMAGE_MODULE = _Image if _features.check("webp") else None
        except Exception:
            _PIL_IMAGE_MODULE = None
    return _PIL_IMAGE_MODULE


def _render_profile(source_file: str) -> dict:
    """按书库返回渲染 profile。毛选纯图扫描用更高显示下限 + 锐化；邓/江/胡文选与《治国理政》这类
    清白底现代扫描用自适应 USM 救低清；其余库（马恩/列宁/文集等，含灰底扫描）共用默认无锐化参数。"""
    norm = _normalize_source_file(source_file)
    if norm.startswith(_MAO_SCAN_PREFIX):
        return MAO_RENDER
    if norm.startswith(_MODERN_SCAN_PREFIXES):
        return MODERN_SCAN_RENDER
    return DEFAULT_RENDER


_LEVELS_LUT_CACHE: dict = {}


def _levels_lut(bp: float, wp: float) -> bytes:
    """构造 256 字节的 levels 查找表：out = clip((v-bp)/(wp-bp), 0, 1) * 255（背景增白 + 文字加深）。
    用于 bytes.translate 的纯 C 级点运算——无需 numpy 即可对整页字节快速做增白。结果按 (bp,wp) 缓存。"""
    key = (round(bp), round(wp))
    lut = _LEVELS_LUT_CACHE.get(key)
    if lut is None:
        span = max(1.0, wp - bp)
        lut = bytes(max(0, min(255, round((v - bp) / span * 255))) for v in range(256))
        _LEVELS_LUT_CACHE[key] = lut
    return lut


def _enhanced_pixmap_or_none(pix, *, usm_amount: float, levels, photo_mid_max: float):
    """低清扫描页清洗：levels 背景增白（灰底/底噪→纯白、文字加深）+ USM 锐化，返回**增强后的 Pixmap**
    （供 JPEG 与 WebP 两种编码复用同一份增强结果）。顺序很关键——先增白再锐化，USM 便无灰底噪点可放大、
    只锐化文字边缘（「灰底叠 USM 更糊」在 v5 被否后的破解）。含图版保护：中间调像素占比 > photo_mid_max
    判为照片/插图页，跳过增白以免压暗、丢层次。

    **两条实现路径**：
      · 装了 numpy（桌面/开发）：levels + USM 全套（USM 需邻域卷积，只能靠 numpy）。
      · 没装 numpy（**线上服务器即此**）：用 bytes.translate + 256 字节 LUT 做 levels 增白（USM 跳过）。
    既无 levels 又无 USM（或异常）时返回 None，调用方回退原始 pix。"""
    np = _numpy_or_none()
    if np is not None:
        if levels is None and usm_amount <= 0:
            return None
        try:
            h, w, n = pix.height, pix.width, pix.n
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, n).astype(np.float32)
            if levels is not None:
                gray = arr[..., 0]
                mid_frac = float(((gray > 100) & (gray < 210)).mean())
                if mid_frac <= photo_mid_max:  # 文字页（双峰：大量白 + 少量黑）才增白；照片页跳过
                    bp, wp = levels
                    arr = np.clip((arr - bp) / (wp - bp), 0.0, 1.0) * 255.0
            if usm_amount > 0:
                blur = (arr * 2 + np.roll(arr, 1, 1) + np.roll(arr, -1, 1)) / 4.0
                blur = (blur * 2 + np.roll(blur, 1, 0) + np.roll(blur, -1, 0)) / 4.0
                arr = arr + usm_amount * (arr - blur)
            return fitz.Pixmap(pix.colorspace, w, h, np.clip(arr, 0, 255).astype(np.uint8).tobytes(), pix.alpha)
        except Exception:
            return None

    # ---- 无 numpy 回退：纯 PyMuPDF 字节 LUT 做 levels 增白（无 USM）----
    if levels is None:
        return None  # 仅 USM 的需求在无 numpy 时无法满足 → 回退原图
    try:
        samples = pix.samples  # alpha=False 渲染，无 alpha 字节，逐字节即灰度/RGB 通道值
        # 图版保护：抽样估算中间调占比（避免逐像素 Python 遍历），过高（照片/插图）则不增白
        step = max(1, len(samples) // 20000)
        sample = samples[::step]
        if sample and sum(1 for b in sample if 100 < b < 210) / len(sample) > photo_mid_max:
            return None
        bp, wp = levels
        return fitz.Pixmap(pix.colorspace, pix.width, pix.height, samples.translate(_levels_lut(bp, wp)), pix.alpha)
    except Exception:
        return None


def _enhance_jpeg_bytes(pix, *, usm_amount: float, levels, photo_mid_max: float, quality: int):
    """（保持原契约）返回增强后的 JPEG 字节；无增强/异常时返回 None，调用方回退 pix.tobytes（绝不 502）。
    增强逻辑已抽到 _enhanced_pixmap_or_none，JPEG 输出与历史逐字节一致。"""
    out = _enhanced_pixmap_or_none(pix, usm_amount=usm_amount, levels=levels, photo_mid_max=photo_mid_max)
    if out is None:
        return None
    try:
        return out.tobytes("jpg", jpg_quality=quality)
    except Exception:
        return None


def _pixmap_to_webp(pix, *, quality: int):
    """把 Pixmap 编码为 WebP 字节（method=2，与 JPEG 同 quality 档）。Pillow 缺失 / 无 WebP 支持 /
    非 RGB(3)·灰度(1) 位图 / 任何异常，一律返回 None → 调用方回退 JPEG，绝不 502。
    评估见记忆 webp-page-image-eval：同 quality 下体积 −30~52%、失真反低于 mupdf 自带 JPEG 编码。
    method 只影响「编码搜索力度/耗时」，不影响解码画质：实测线上单页 method=4 冷编码 ~467ms（比 mupdf
    JPEG 慢 1.5×，是「首开变卡」回归的成因之一），改 method=2 后 ~248ms（反比 JPEG 的 321ms 更快），
    体积仅大 ~3%（542KB vs 528KB）——近乎白赚的提速。旧 method=4 的 .webp 缓存无需失效（解码等价）。"""
    Image = _pillow_or_none()
    if Image is None or pix.n not in (1, 3):
        return None
    try:
        import io as _io
        mode = "RGB" if pix.n == 3 else "L"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        buf = _io.BytesIO()
        img.save(buf, "WEBP", quality=quality, method=2)
        return buf.getvalue()
    except Exception:
        return None


def _native_image_width_px(page) -> int:
    """页面最大内嵌图像的原生像素宽（扫描件即扫描分辨率，是真实细节上限）。
    用 get_image_info（不解码像素、开销极小）；无图 / 异常时返回 0，调用方据此回退到显示下限。"""
    try:
        return max((info.get("width", 0) for info in page.get_image_info()), default=0)
    except Exception:
        return 0


# 渲染并发限流：冷渲染（缓存未命中）是 CPU 密集型；若 8 个 waitress 线程同时冷渲染会把 CPU 打满，连
# 反代（Caddy）的 TLS 握手都被饿死 → 整站每个请求（含静态资源/403）都卡 ~6s（线上事故根因）。用信号量
# 把「同时渲染数」限到很小（默认 2，env MARX_PAGE_IMAGE_RENDER_CONCURRENCY 可调），给 Caddy/其他请求留出
# CPU。缓存命中走快路径、不进信号量、不受影响。缓存预热后该限流几乎不触发。
_PAGE_IMAGE_RENDER_CONCURRENCY = max(1, int(os.environ.get("MARX_PAGE_IMAGE_RENDER_CONCURRENCY", "2")))
_PAGE_IMAGE_RENDER_SEMAPHORE = threading.BoundedSemaphore(_PAGE_IMAGE_RENDER_CONCURRENCY)


def _touch_page_image_mtime(cache_path: Path) -> None:
    """命中后把缓存文件 mtime 顶到现在（节流：仅当已超 1 小时未更新才写一次 utime），让按 mtime 升序的
    LRU 清理成为真正的「最久未用先删」，而非「最早创建先删」——否则天天读的热页反被先逐出、再被重渲染。"""
    try:
        st = cache_path.stat()
        if time.time() - st.st_mtime > 3600:
            os.utime(cache_path, None)
    except OSError:
        pass


# ---- WebP 后台补渲染：修复「首开变卡」回归的关键 ----
# 背景：webp 用独立缓存键（.webp），既有 13,000+ 页的暖 JPEG 缓存对 webp 客户端全部落空 → 每页首开都
# 冷渲染（实测 ~570ms，而暖命中仅 ~5ms），叠加 method=4 编码慢 1.5×，/page-image p50 近乎翻倍。
# 修法：webp 客户端请求某页时，若 .webp 未就绪但 .jpg 已缓存 → 立刻回 jpg（暖命中、恢复旧速），
# 同时把 webp 编码丢到**单线程、有界队列、去重**的后台补渲染，下次访问即命中 webp、拿到体积收益。
_WEBP_BG_QUEUE_MAX = int(os.environ.get("MARX_WEBP_BG_QUEUE_MAX", "512"))
_webp_bg_queue: "queue.Queue" = queue.Queue(maxsize=_WEBP_BG_QUEUE_MAX)
_webp_bg_inflight: set = set()
_webp_bg_lock = threading.Lock()


def _schedule_webp_background(source_file: str, page_number: int, query_text: str, matrix_scale: float) -> None:
    """把某页的 webp 补渲染排入后台队列（去重 + 背压）。Pillow 不可用则不排。绝不阻塞请求线程。"""
    if _pillow_or_none() is None:
        return
    key = (source_file, page_number, query_text)
    with _webp_bg_lock:
        if key in _webp_bg_inflight or len(_webp_bg_inflight) >= _WEBP_BG_QUEUE_MAX:
            return  # 已在途 / 在途过多 → 跳过，下次访问再补（有界、绝不无限堆积）
        _webp_bg_inflight.add(key)
    try:
        _webp_bg_queue.put_nowait((source_file, page_number, query_text, matrix_scale))
    except queue.Full:
        with _webp_bg_lock:
            _webp_bg_inflight.discard(key)


def _webp_bg_worker() -> None:
    """单后台线程：串行消费队列、补渲染 webp。渲染仍走 _PAGE_IMAGE_RENDER_SEMAPHORE，与用户前台渲染
    共享 2 个名额上限 → 后台补渲染绝不额外把 CPU 打满（最多占 1 名额、天然给用户请求让路）。"""
    while True:
        try:
            sf, page, query, scale = _webp_bg_queue.get()
        except Exception:  # noqa: BLE001
            continue
        try:
            webp_path = _page_image_cache_path(sf, page, query, fmt="webp")
            if not (webp_path.exists() and webp_path.stat().st_size > 0):
                with _PAGE_IMAGE_RENDER_SEMAPHORE:
                    if not (webp_path.exists() and webp_path.stat().st_size > 0):
                        _render_page_image_uncached(sf, page, query, scale, want_webp=True)
        except Exception as exc:  # noqa: BLE001 - 后台补渲染失败绝不影响任何请求
            LOGGER.debug("Background webp render failed for %s page %s: %s", sf, page, exc)
        finally:
            with _webp_bg_lock:
                _webp_bg_inflight.discard((sf, page, query))
            try:
                _webp_bg_queue.task_done()
            except Exception:  # noqa: BLE001
                pass


threading.Thread(target=_webp_bg_worker, name="webp-bg-render", daemon=True).start()


def _render_page_image_to_cache(source_file: str, page_number: int, query_text: str, *, want_webp: bool = False, matrix_scale: float = PAGE_IMAGE_MIN_SCALE) -> Path:
    # 高亮串只取前 120 字：超长高亮对页图锚定无意义，更要紧的是限住缓存键的爆炸面——否则 bot 轮换
    # ?q=/?h= 每次都生成新 digest → 永不命中、每次冷渲染并写一张新图，撑爆 8GiB 缓存 + 抢渲染名额。
    query_text = (query_text or "")[:120]
    if want_webp:
        webp_path = _page_image_cache_path(source_file, page_number, query_text, fmt="webp")
        if webp_path.exists() and webp_path.stat().st_size > 0:
            return webp_path  # 热 webp 命中：秒返、且已是小体积
        jpg_path = _page_image_cache_path(source_file, page_number, query_text, fmt="jpg")
        if jpg_path.exists() and jpg_path.stat().st_size > 0:
            # 关键修复：.webp 未就绪但 .jpg 已缓存 → 立刻回 jpg（暖命中 ~5ms，恢复回归前速度），
            # webp 丢后台补渲染，下次访问该页即命中 webp、拿到体积收益。用户永不为「暖→冷」买单。
            _schedule_webp_background(source_file, page_number, query_text, matrix_scale)
            return jpg_path
        # 两变体都无（全新页，极少）→ 内联渲染 webp（method=2 快）。进信号量限流冷渲染并发。
        with _PAGE_IMAGE_RENDER_SEMAPHORE:
            if webp_path.exists() and webp_path.stat().st_size > 0:
                return webp_path
            if jpg_path.exists() and jpg_path.stat().st_size > 0:
                _schedule_webp_background(source_file, page_number, query_text, matrix_scale)
                return jpg_path
            return _render_page_image_uncached(source_file, page_number, query_text, matrix_scale, want_webp=True)
    # 非 webp 客户端：现行 JPEG 路径，完全不变。
    jpg_path = _page_image_cache_path(source_file, page_number, query_text, fmt="jpg")
    if jpg_path.exists() and jpg_path.stat().st_size > 0:
        return jpg_path
    with _PAGE_IMAGE_RENDER_SEMAPHORE:  # 限流冷渲染并发，防 CPU 打满拖垮整站
        if jpg_path.exists() and jpg_path.stat().st_size > 0:
            return jpg_path  # 排队等待期间已被其他线程渲染好
        return _render_page_image_uncached(source_file, page_number, query_text, matrix_scale, want_webp=False)


def _render_page_image_uncached(source_file: str, page_number: int, query_text: str, matrix_scale: float, want_webp: bool = False) -> Path:
    pdf_path = _resolve_pdf_path(source_file, require_full_mode=False)
    with fitz.open(pdf_path) as doc:
        if page_number > doc.page_count:
            abort(404, description="请求页码超出 PDF 范围。")
        page = doc[page_number - 1]

        highlight_done = False
        # 主路径：归一化逐字锚定，跨行整句高亮（修复长句 search_for 整串匹配失败的问题）。
        if query_text:
            anchored_rects = _anchored_highlight_rects(page, query_text)
            if anchored_rects:
                for rect in anchored_rects:
                    annot = page.add_highlight_annot(rect)
                    annot.set_colors(stroke=(1.0, 0.86, 0.2))
                    annot.set_opacity(0.45)
                    annot.update()
                highlight_done = True
        # 兜底一：search_for（短词/个别词项快路径；锚定因文本层修订等原因失配时仍可命中）
        if not highlight_done:
            for term in _highlight_terms(query_text):
                rects = page.search_for(term, quads=False)
                if not rects:
                    continue
                for rect in rects[:12]:
                    annot = page.add_highlight_annot(rect)
                    annot.set_colors(stroke=(1.0, 0.86, 0.2))
                    annot.set_opacity(0.45)
                    annot.update()
                highlight_done = True
                break
        if not highlight_done and query_text:
            # 逐字文本层兜底（search_for 在单字 span 布局上找不到多字词项）
            for rect in _per_char_term_rects(page, _highlight_terms(query_text)):
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=(1.0, 0.86, 0.2))
                annot.set_opacity(0.45)
                annot.update()

        # 原生分辨率自适应缩放：渲染倍率「不低于源原生（不丢真实细节）、且不低于显示下限（填满阅读器）」，
        # 再夹到 [lo, hard_max]；USM 锐化强度按上采样倍数自适应（接近原生→几乎不锐化，避免过锐伪影）。
        # DEFAULT 库另带「低清页清洗」：仅当 upsample ≥ clean_min_upsample（低清被显著拉伸）时启用
        # levels 背景增白 + USM；高清/原生页（upsample≈1）则强制 usm=0、不增白，输出与历史逐字节一致。
        # profile 按书库选择。任何异常都安全回退到下限倍率、不锐化，绝不让渲染崩溃。
        profile = _render_profile(source_file)
        lo = max(matrix_scale, PAGE_IMAGE_MIN_SCALE)
        usm = 0.0
        levels = None
        try:
            page_width_pt = float(page.rect.width)
            if page_width_pt <= 0:
                raise ValueError("non-positive page width")
            native_px = _native_image_width_px(page)
            floor_scale = profile["display_min_px"] / page_width_pt
            native_scale = native_px / page_width_pt if native_px > 0 else floor_scale
            scale = max(lo, min(max(native_scale, floor_scale), profile["hard_max_scale"]))
            upsample = scale / native_scale if native_scale > 0 else 1.0
            usm = max(0.0, min((upsample - 1.0) * profile["usm_gain"], profile["usm_cap"]))
            clean_min_upsample = profile.get("clean_min_upsample")
            if clean_min_upsample is None:
                # MAO / MODERN（整库即扫描件，无高清/低清混排）：始终增白（+ USM if numpy）
                levels = profile.get("levels")
            elif upsample >= clean_min_upsample:
                # DEFAULT 低清页：抬高渲染目标（更多像素让结果更清），再背景增白 + 自适应 USM
                clean_floor = profile.get("clean_display_min_px", profile["display_min_px"]) / page_width_pt
                clean_max = profile.get("clean_hard_max_scale", profile["hard_max_scale"])
                scale = max(lo, min(max(native_scale, clean_floor), clean_max))
                upsample = scale / native_scale if native_scale > 0 else 1.0
                usm = max(0.0, min((upsample - 1.0) * profile["usm_gain"], profile["usm_cap"]))
                levels = profile.get("levels")
            else:
                usm = 0.0  # DEFAULT 高清/原生页：维持历史「无清洗」行为，输出逐字节一致
        except Exception:
            scale = lo
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=True)
        quality = profile["jpeg_quality"]
        # 增强只做一次（灰底增白/USM），JPEG 与 WebP 共用同一份增强结果。enhanced 为 None 时用原始 pix，
        # 此时 JPEG 分支输出与历史逐字节一致（enhanced 有则等价旧 _enhance_jpeg_bytes 的输出）。
        enhanced = _enhanced_pixmap_or_none(
            pix, usm_amount=usm, levels=levels, photo_mid_max=profile.get("photo_mid_max", 0.25),
        )
        render_pix = enhanced or pix
        data = None
        out_fmt = "jpg"
        if want_webp:
            data = _pixmap_to_webp(render_pix, quality=quality)  # Pillow 缺失/编码失败 → None
            if data is not None:
                out_fmt = "webp"
        if data is None:  # 非 webp 请求、或 webp 编码失败 → JPEG（现行路径，输出不变、绝不 502）
            data = render_pix.tobytes("jpg", jpg_quality=quality)
            out_fmt = "jpg"
        # 目标路径按**实际**产出格式定（webp 失败已回退 jpg），避免把 JPEG 字节写进 .webp 致 mimetype 错配。
        cache_path = _page_image_cache_path(source_file, page_number, query_text, fmt=out_fmt)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_name(f"{cache_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp_path.write_bytes(data)
    temp_path.replace(cache_path)
    return cache_path


# 邻页预热：每次 /page-image 会再起一个 daemon 线程渲染相邻页。问题是它**绕过 waitress 的线程上限**，
# 高并发 + 缓存全冷（版本号/ tag 升级后）时会瞬间炸出大量并发渲染把 CPU 打满、整站请求排队卡死。
# 故默认**关闭**，仅在缓存预热稳定后可经 env MARX_PAGE_IMAGE_PREWARM=1 重新开启。
PAGE_IMAGE_PREWARM_ENABLED = os.environ.get("MARX_PAGE_IMAGE_PREWARM", "0") == "1"


def _prewarm_page_images(source_file: str, page_number: int, query_text: str, page_count: int) -> None:
    if not PAGE_IMAGE_PREWARM_ENABLED or query_text:
        return

    candidates = [p for p in (page_number - 1, page_number + 1) if 1 <= p <= page_count]
    if not candidates:
        return

    def _worker() -> None:
        for candidate in candidates:
            try:
                _render_page_image_to_cache(source_file, candidate, "")
            except Exception as exc:
                LOGGER.debug("Page image prewarm failed for %s page %s: %s", source_file, candidate, exc)

    threading.Thread(target=_worker, daemon=True).start()


# 页面图缓存按需懒生成、只增不减：每次渲染版本号升级（v3→v4→v5…）旧缓存即成孤儿，永不命中
# 也不会自动删除；叠加 v5 更高分辨率使单文件更大 → 磁盘会持续增长、有撑爆风险（线上盘仅 39G）。
# 这里加一个轻量 LRU 软上限：在 /page-image 请求路径上节流触发（最多每 30 分钟一次），后台线程
# 扫描缓存目录，若总大小超过上限则按文件 mtime（生成时间，近似最久未用）从旧到新删，直到降回
# 上限的 90% 留缓冲。全程 try/except、走 daemon 线程，绝不阻塞/影响请求；删掉的页下次访问自动重渲染。
# 上限可经 env MARX_PAGE_IMAGE_CACHE_MAX_BYTES 覆盖，默认 8 GiB。
PAGE_IMAGE_CACHE_MAX_BYTES = int(os.environ.get("MARX_PAGE_IMAGE_CACHE_MAX_BYTES", str(8 * 1024 ** 3)))
PAGE_IMAGE_CACHE_PRUNE_INTERVAL_SECONDS = 1800
_last_page_image_prune: list[float] = [0.0]


def _prune_page_image_cache_if_due() -> None:
    now = time.time()
    if now - _last_page_image_prune[0] < PAGE_IMAGE_CACHE_PRUNE_INTERVAL_SECONDS:
        return
    _last_page_image_prune[0] = now

    def _worker() -> None:
        try:
            entries: list[tuple[float, int, str]] = []
            total = 0
            for root, _dirs, files in os.walk(PAGE_IMAGE_CACHE_DIR):
                for name in files:
                    if not (name.endswith(".jpg") or name.endswith(".webp")):
                        continue  # .webp 变体同样计入 8GiB LRU，否则永不淘汰、无界增长撑爆磁盘
                    path = os.path.join(root, name)
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    entries.append((stat.st_mtime, stat.st_size, path))
                    total += stat.st_size
            if total <= PAGE_IMAGE_CACHE_MAX_BYTES:
                return
            target = int(PAGE_IMAGE_CACHE_MAX_BYTES * 0.9)
            entries.sort(key=lambda item: item[0])  # 最旧（mtime 最小）先删
            removed = 0
            for _mtime, size, path in entries:
                if total <= target:
                    break
                try:
                    os.remove(path)
                except OSError:
                    continue
                total -= size
                removed += 1
            LOGGER.info(
                "Page image cache pruned: removed %s files, now ~%.2f GiB", removed, total / 1024 ** 3
            )
        except Exception as exc:
            LOGGER.debug("Page image cache prune failed: %s", exc)

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# 期刊文献 PDF：按需镜像 + 本地直发（复用 page-image 的缓存/LRU 清理范式）
# ---------------------------------------------------------------------------
JOURNAL_PDF_MAX_BYTES = int(os.environ.get("MARX_JOURNAL_PDF_MAX_BYTES", str(80 * 1024 ** 2)))  # 单文件上限 80MB
JOURNAL_PDF_CACHE_MAX_BYTES = int(os.environ.get("MARX_JOURNAL_PDF_CACHE_MAX_BYTES", str(4 * 1024 ** 3)))  # 软上限 4GiB
JOURNAL_PDF_CACHE_PRUNE_INTERVAL_SECONDS = 1800
JOURNAL_PDF_FETCH_TIMEOUT = 30
_JOURNAL_PDF_FETCH_SEMAPHORE = threading.Semaphore(int(os.environ.get("MARX_JOURNAL_PDF_FETCH_CONCURRENCY", "3")))
_last_journal_pdf_prune: list[float] = [0.0]
_JOURNAL_PDF_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _journal_pdf_cache_path(article_id: int) -> Path:
    return JOURNAL_PDF_CACHE_DIR / f"{int(article_id)}.pdf"


def _is_ncpssd_landing_url(url: str) -> bool:
    """NCPSSD 文章页(非直链 PDF)：全文受登录+瑞数 WAF 限制，应直接跳转源站而非无谓抓取。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.netloc.endswith("ncpssd.cn") and "/Literature/articleinfo" in parsed.path


def _mirror_journal_pdf_to_cache(article_id: int, pdf_url: str) -> Path | None:
    """把远端 PDF 拉取并缓存到本地；成功返回缓存路径，非 PDF/被拦/超限/失败返回 None。

    None 由路由翻译成“跳转源站”，绝不把 HTML 落地页当 PDF 直发。NCPSSD 直链走带
    Cookie/WAF 的共享 opener，其余用浏览器 UA 的标准请求。"""
    if not pdf_url.lower().startswith(("http://", "https://")):
        return None
    cache_path = _journal_pdf_cache_path(article_id)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path
    with _JOURNAL_PDF_FETCH_SEMAPHORE:  # 限流并发镜像，防批量点击打满网络/磁盘
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path  # 排队期间已被其他线程镜像好
        try:
            host = urlparse(pdf_url).netloc
        except ValueError:
            return None
        req = urllib.request.Request(
            pdf_url,
            headers={"User-Agent": _JOURNAL_PDF_BROWSER_UA, "Accept": "application/pdf,*/*",
                     "Referer": "https://www.ncpssd.cn/" if host.endswith("ncpssd.cn") else pdf_url},
        )
        opener = journal_ncpssd_opener() if host.endswith("ncpssd.cn") else urllib.request.build_opener()
        temp_path = cache_path.with_name(f"{cache_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with opener.open(req, timeout=JOURNAL_PDF_FETCH_TIMEOUT) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                head = resp.read(8)
                # 只接受 PDF：Content-Type 标注 pdf，或正文以 %PDF- 魔数开头；HTML 落地页一律拒绝。
                is_pdf = "application/pdf" in ctype or head.startswith(b"%PDF")
                if not is_pdf and "application/octet-stream" not in ctype:
                    return None
                JOURNAL_PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                total = len(head)
                with open(temp_path, "wb") as fh:
                    fh.write(head)
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > JOURNAL_PDF_MAX_BYTES:
                            raise ValueError("pdf exceeds size cap")
                        fh.write(chunk)
            if total < 5 or not head.startswith(b"%PDF"):
                # 兜底：octet-stream 但实非 PDF（魔数不符）→ 视为失败，跳转源站。
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                return None
            os.replace(temp_path, cache_path)
            return cache_path
        except Exception as exc:
            LOGGER.info("Journal PDF mirror failed (%s): %s", pdf_url, exc)
            try:
                temp_path.unlink()
            except OSError:
                pass
            return None


def _prune_journal_pdf_cache_if_due() -> None:
    now = time.time()
    if now - _last_journal_pdf_prune[0] < JOURNAL_PDF_CACHE_PRUNE_INTERVAL_SECONDS:
        return
    _last_journal_pdf_prune[0] = now

    def _worker() -> None:
        try:
            entries: list[tuple[float, int, str]] = []
            total = 0
            for root, _dirs, files in os.walk(JOURNAL_PDF_CACHE_DIR):
                for name in files:
                    if not name.endswith(".pdf"):
                        continue
                    path = os.path.join(root, name)
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    entries.append((stat.st_mtime, stat.st_size, path))
                    total += stat.st_size
            if total <= JOURNAL_PDF_CACHE_MAX_BYTES:
                return
            target = int(JOURNAL_PDF_CACHE_MAX_BYTES * 0.9)
            entries.sort(key=lambda item: item[0])  # 最旧（mtime 最小）先删
            removed = 0
            for _mtime, size, path in entries:
                if total <= target:
                    break
                try:
                    os.remove(path)
                except OSError:
                    continue
                total -= size
                removed += 1
            LOGGER.info("Journal PDF cache pruned: removed %s files, now ~%.2f GiB", removed, total / 1024 ** 3)
        except Exception as exc:
            LOGGER.debug("Journal PDF cache prune failed: %s", exc)

    threading.Thread(target=_worker, daemon=True).start()


def _journal_pdf_download_name(article: dict) -> str:
    """据文章标题生成安全的下载文件名（去掉路径/控制字符，限长）。"""
    title = str(article.get("title_zh") or article.get("title") or "").strip()
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip()[:80]
    return f"{safe or 'article'}.pdf"


_RELEASE_UPLOAD_ENDPOINTS = frozenset({"admin_desktop_release_upload"})
_MEDIA_UPLOAD_ENDPOINTS = frozenset({
    "api_feedback_message_create",
    "admin_feedback_reply",
    "admin_payment_qr_settings",
})


@app.before_request
def _apply_request_body_limit():
    # 防超大请求体内存放大：默认沿用 config 的 MAX_CONTENT_LENGTH(4MB)；图片上传端点
    # 放宽到 40MB(反馈最多 6x5MB、收款码多图)；安装包上传端点放宽到 MAX_RELEASE_UPLOAD_MB
    # (+8MB 余量)。必须在 CSRF 解析 multipart 表单前生效，故本钩子注册为第一个 before_request。
    endpoint = request.endpoint or ""
    if endpoint in _RELEASE_UPLOAD_ENDPOINTS:
        try:
            release_mb = max(1, int(os.environ.get("MAX_RELEASE_UPLOAD_MB") or "200"))
        except ValueError:
            release_mb = 200
        request.max_content_length = (release_mb + 8) * 1024 * 1024
    elif endpoint in _MEDIA_UPLOAD_ENDPOINTS:
        request.max_content_length = 40 * 1024 * 1024


@app.before_request
def load_current_user():
    user_id = session.get("user_id")
    try:
        user = get_user_by_id(int(user_id)) if user_id else None
    except (TypeError, ValueError):
        user = None
    if user and not user.get("is_active", 1):
        session.pop("user_id", None)
        user = None
    if user_id and user is None:
        session.pop("user_id", None)
    g.current_user = user
    if user is not None:
        _capture_current_user_ip(user)  # 登录态活跃即补 IP（进程内去重、走后台单写、只补尚无 IP 者）
    g.membership = get_membership_snapshot(int(user["id"])) if user else get_membership_snapshot(None)
    # 站点文案映射改为惰性合成（见 _request_site_texts）：仅渲染模板的请求才计算，避免在
    # /page-image、/static 等高频非模板端点上做无谓开销。
    if DEPLOYMENT.is_server:
        _sweep_expired_orders_if_due()


_GLOBAL_RATE_EXEMPT_ENDPOINTS = frozenset({"static"})


@app.before_request
def _global_ip_rate_limit():
    # 全局每真实IP兜底限速：只拦公网IP对普通端点的高频泛刷。阅读器/书页图像/PDF 已有
    # 专门限速(READER_ENDPOINTS)，静态资源、管理员、监控、内网/回环均豁免。
    if request.method == "OPTIONS":
        return
    if (request.endpoint or "") in _GLOBAL_RATE_EXEMPT_ENDPOINTS or _is_reader_audit_endpoint():
        return
    if _is_monitoring_request() or _is_admin_user(getattr(g, "current_user", None)):
        return
    ip = _client_ip()
    if not _is_public_ip(ip):
        return
    _rate_limit_or_abort(f"global:ip:{ip}", limit=600, window_seconds=60, message="访问过于频繁，请稍后再试。")


@app.before_request
def audit_and_guard_reader_access():
    if not _is_reader_audit_endpoint():
        return
    # 已豁免的监控程序：不审计、不计异常、也不受封禁名单影响，直接放行。
    if _is_monitoring_request():
        return
    # 已知 AI 爬虫(GPTBot 等)：对阅读/取书内容直接 403，不审计、不入异常名单。
    if _is_blocked_bot_request():
        abort(403, description="Automated crawling of reader content is not permitted.")
    _record_reader_access_event()
    _prune_reader_audit_if_due()
    _auto_ban_egregious_scrapers_if_due()
    _require_reader_not_banned_or_abort()


@app.before_request
def record_current_activity():
    feature = _activity_feature_for_request()
    if not feature:
        return
    # 监控程序的活动不计入站点活动(阅读器访问/搜索/在线等总览指标)。
    if _is_monitoring_request():
        return
    try:
        user = getattr(g, "current_user", None)
        session_key = _visitor_session_key()
        activity_key = _online_presence_dedup_key(session_key)
        client_ip = _client_ip()
        user_agent = str(request.headers.get("User-Agent") or "")
        user_id = int(user["id"]) if user else None
        # 与请求上下文相关的值(会话键、去重键)在请求线程内先解析好，再投递；后台写线程只拿
        # 已解析的纯数据落库，不依赖 request/g。两类写都移出热路径，避免被拒匿名洪峰挤 SQLite 写锁。
        _enqueue_audit_write(
            "activity",
            {
                "session_key": activity_key,
                "user_id": user_id,
                "day": china_day_text(),
                "feature": feature,
                "path": request.path,
                "client_ip": client_ip,
                "user_agent": user_agent,
            },
        )
        # 15 分钟时槽在线记录，供 24 小时在线变化图统计。匿名访客去重键按是否回传会话 cookie 走
        # cookie 或真实 IP（见 _online_presence_dedup_key），避免「每请求换 cookie」的爬虫刷高在线数。
        _enqueue_audit_write(
            "online",
            {
                "session_key": activity_key,
                "user_id": user_id,
            },
        )
        _prune_online_presence_if_due()
    except Exception as exc:
        LOGGER.debug("Site activity recording failed: %s", exc)


@app.before_request
def enforce_csrf_for_state_changes():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return
    _require_csrf()


_ANNO_NUM_RE = re.compile(r"^[（(]\s*(\d+)\s*[)）]\s*(.*)$|^(\d+)\s*[.、]\s*(.*)$")
_ANNO_HEAD_RE = re.compile(r"^(#{2,4})\s+(.*)$")
_ANNO_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_ANNO_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _announcement_inline(text: str) -> str:
    """行内排版：先转义防注入，再应用 **加粗**。"""
    safe = str(escape(text))
    return _ANNO_BOLD_RE.sub(r"<strong>\1</strong>", safe)


def render_announcement_html(text: str) -> Markup:
    """把公告纯文本渲染成排版后的安全 HTML。

    支持：`## 小标题`、`**加粗**`、`- 项` 无序列表、`（1）/1.` 有序列表、
    `---` 分隔线、空行分段。其余按段落呈现，保留行内换行。先整体转义，仅注入
    自有标签，杜绝 XSS。
    """
    if not text:
        return Markup("")
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_kind = ""  # "ol" / "ul"

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_announcement_inline(l) for l in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if items:
            lis = "".join("<li>" + _announcement_inline(t) + "</li>" for t in items)
            out.append(f'<{list_kind} class="notice-list">{lis}</{list_kind}>')
            items.clear()
            list_kind = ""

    for raw in lines:
        s = raw.strip()
        if not s:
            flush_para()
            flush_list()
            continue
        head = _ANNO_HEAD_RE.match(s)
        if head:
            flush_para()
            flush_list()
            out.append('<div class="notice-h">' + _announcement_inline(head.group(2)) + "</div>")
            continue
        if s in ("---", "***", "___"):
            flush_para()
            flush_list()
            out.append('<hr class="notice-hr">')
            continue
        bullet = _ANNO_BULLET_RE.match(s)
        if bullet:
            flush_para()
            if list_kind and list_kind != "ul":
                flush_list()
            list_kind = "ul"
            items.append(bullet.group(1))
            continue
        num = _ANNO_NUM_RE.match(s)
        if num:
            flush_para()
            if list_kind and list_kind != "ol":
                flush_list()
            list_kind = "ol"
            items.append(num.group(2) if num.group(2) is not None else num.group(4))
            continue
        flush_list()
        para.append(s)

    flush_para()
    flush_list()
    return Markup("".join(out))


def render_community_items(text: str) -> list:
    """把「社区建设」多行文本拆成逐条目（每个非空行一条），用于首页竖向循环滚动。

    复用公告的行内排版：先整体转义防注入，仅注入 **加粗**，逐条安全。返回 Markup 列表，
    模板里直接迭代渲染（含一份复制以实现无缝循环）。
    """
    if not text:
        return []
    items: list = []
    for raw in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = raw.strip()
        if s:
            items.append(Markup(_announcement_inline(s)))
    return items


@app.context_processor
def inject_auth_context():
    membership = getattr(g, "membership", get_membership_snapshot(None))
    site_texts = _request_site_texts()

    def _site_text(key: str, **kwargs: object) -> str:
        base = site_texts.get(key)
        if base is None:
            return render_site_text(key, **kwargs)
        try:
            return base.format_map({k: "" if v is None else str(v) for k, v in kwargs.items()})
        except Exception:
            return base

    def _site_text_auto(key: str, b64default: str = "") -> str:
        # 自动接入文字：有后台覆盖值用覆盖值，否则还原模板内联的原文（base64）。
        return render_auto_site_text(key, b64default, site_texts)

    # 全站统一的「程序版本」：后台内容运营设的 index.stat_app_version_value 优先，
    # 留空则回退到程序内置 APP_VERSION。首页/公告/阅读器/辞典/文库等处统一引用，
    # 后台改一处即可全站联动。
    app_version_display = (str(site_texts.get("index.stat_app_version_value") or "").strip()
                           or APP_VERSION)

    # 「会员中心 / 用户中心」称呼按身份切换：有效会员 / 管理员 / 桌面版显示「会员中心」，普通登录用户显示
    # 「用户中心」；未登录回退到品牌默认「会员中心」（导航入口本就仅登录后出现）。账号页标题、页内标题与
    # 各处「返回/进入会员中心」入口统一引用此标签。
    _account_user = getattr(g, "current_user", None)
    _account_is_admin = _is_admin_user(_account_user)
    _account_member_view = (
        bool(getattr(membership, "is_active_member", False))
        or _account_is_admin
        or DEPLOYMENT.is_desktop
    )
    account_center_label = "会员中心" if (_account_user is None or _account_member_view) else "用户中心"

    return {
        "current_user": _account_user,
        "is_admin": _account_is_admin,
        "admin_console_available": _account_is_admin,
        "membership": _membership_to_dict(membership),
        "account_center_label": account_center_label,
        "format_price": _display_price,
        "format_datetime": _display_datetime,
        "format_order_status": _display_order_status,
        "format_membership_status": _display_membership_status,
        "format_payment_provider": _display_payment_provider,
        "format_subscription_source": _display_subscription_source,
        "alipay_runtime": PAYMENT_CONFIG.to_public_dict(),
        "payment_runtime": PAYMENT_CONFIG.to_public_dict(),
        "site_text": _site_text,
        "site_text_auto": _site_text_auto,
        "render_announcement": render_announcement_html,
        "render_community_items": render_community_items,
        "app_version_display": app_version_display,
        "csrf_token": _ensure_csrf_token(),
        "local_console_available": _is_local_console_request(),
    }


@app.errorhandler(_RedirectTo)
def handle_redirect(error):
    return redirect(error.location)


@app.errorhandler(_AIQuotaExceeded)
def handle_ai_quota_exceeded(error):
    payload = {
        "ok": False,
        "error": str(error) or "本周 AI token 额度已用完，下周一恢复。",
        "used_tokens": error.used,
        "weekly_limit": error.limit,
        "reset_at": error.reset_at,
        # 429 时也带回最新额度，便于前端刷新余额条。
        "ai_token_quota": _ai_token_quota_payload(getattr(g, "current_user", None)),
    }
    if request.path.startswith("/api/"):
        return jsonify(payload), 429
    return (
        render_template(
            "error.html",
            title="AI 额度已用完",
            message=payload["error"],
            state=current_view_state(),
        ),
        429,
    )


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(401)
@app.errorhandler(429)
@app.errorhandler(503)
def handle_known_errors(error):
    message = getattr(error, "description", "发生错误。")
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": message}), getattr(error, "code", 500)
    return (
        render_template(
            "error.html",
            title="无法完成请求",
            message=message,
            state=current_view_state(),
        ),
        getattr(error, "code", 500),
    )


@app.errorhandler(500)
def handle_unexpected_error(error):
    LOGGER.exception("Unhandled application error: %s", getattr(error, "original_exception", error))
    message = "服务器暂时无法完成请求，请稍后重试。"
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": message}), 500
    return (
        render_template(
            "error.html",
            title="无法完成请求",
            message=message,
            state=current_view_state(),
        ),
        500,
    )


@app.after_request
def add_security_headers(response):
    # 注意：生产环境 Caddy 也在 /etc/caddy/Caddyfile（及 deploy/Caddyfile.example）里设了同一条 CSP，
    # 且 Caddy 的 header 指令会【覆盖】本应用设置的 CSP。改 CSP（尤其 frame-src/script-src 等）必须
    # 同时改这两处，否则线上以 Caddy 为准、本处改动不生效（曾因此导致 /wenku 同源 iframe 被挡）。
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' https://challenges.cloudflare.com; "
        "frame-src 'self' https://challenges.cloudflare.com; "
        "worker-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        # 支付修复：ZPay 网关下单后会经 302 重定向链跳转（zpayz.cn → api.z-pay.cn 等），
        # 浏览器会对整条重定向链强制 form-action 校验。若只放行 zpayz.cn，跳到 api.z-pay.cn 时
        # 会被静默拦截，表现为"点击在线支付/继续支付没有反应"。这里放行 ZPay 两个域名及其子域。
        "form-action 'self' https://zpayz.cn https://*.zpayz.cn https://z-pay.cn https://*.z-pay.cn; "
        "frame-ancestors 'self'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if str(request.endpoint or "") in READER_ENDPOINTS:
        response.headers.setdefault("X-Robots-Tag", "noindex,nofollow,noarchive")
    if DEPLOYMENT.public_scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload")
    return response


@app.route("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Disallow: /admin",
        "Disallow: /account",
        "Disallow: /api",
        "Disallow: /payments",
        "Disallow: /reader",
        "Disallow: /library",
        "Disallow: /viewer",
        "Disallow: /page-image",
        "Disallow: /pdf",
        "",
    ]
    # 已知 AI 训练/采集爬虫：显式全站禁止(声称尊重 robots 者据此自退;
    # 无视者由应用层按 User-Agent 直接 403 兜底)。
    for bot in (
        "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "anthropic-ai",
        "CCBot", "Bytespider", "Amazonbot", "Google-Extended", "PerplexityBot",
        "Applebot-Extended", "meta-externalagent", "Diffbot", "Omgilibot",
        "DataForSeoBot", "PetalBot",
    ):
        lines.append(f"User-agent: {bot}")
        lines.append("Disallow: /")
        lines.append("")
    body = "\n".join(lines)
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/.well-known/security.txt")
def security_txt():
    contact = str(os.environ.get("SECURITY_CONTACT") or "").strip()
    if not contact and DEPLOYMENT.public_host:
        contact = f"mailto:security@{DEPLOYMENT.public_host.split(':', 1)[0]}"
    if not contact:
        contact = "mailto:security@example.com"
    body = "\n".join(
        [
            f"Contact: {contact}",
            "Expires: 2027-12-31T23:59:59+08:00",
            "Preferred-Languages: zh-CN, en",
            "",
        ]
    )
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/register", methods=["GET", "POST"])
def register():
    if getattr(g, "current_user", None):
        return redirect(url_for("account"))

    next_url = _safe_next_url(request.args.get("next") or request.form.get("next"))
    values = {"email": "", "display_name": "", "email_code": "", "password": "", "confirm_password": ""}
    errors: list[str] = []
    turnstile_required = True
    code_sent = False  # 验证码发送成功后置 True，前端据此弹出"验证码已发送"提示窗

    if request.method == "POST":
        action = (request.form.get("action") or "register").strip()
        email = normalize_email(request.form.get("email") or "")
        display_name = (request.form.get("display_name") or "").strip()
        email_code = (request.form.get("email_code") or "").strip()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        # 修复：发送验证码（send_code）会触发整页 POST 刷新，需回填密码/确认密码，避免用户已输入的内容被清空。
        values = {
            "email": email,
            "display_name": display_name,
            "email_code": email_code,
            "password": password,
            "confirm_password": confirm_password,
        }

        if not email or "@" not in email:
            errors.append("请输入有效邮箱。")
        if get_user_by_email(email):
            errors.append("该邮箱已注册，请直接登录。")

        if action == "send_code":
            _rate_limit_or_abort(
                f"register-code:ip:{_client_ip()}",
                limit=RATE_LIMITS["register_code_ip"][0],
                window_seconds=RATE_LIMITS["register_code_ip"][1],
                message="验证码发送过于频繁，请稍后再试。",
            )
            if email:
                _rate_limit_or_abort(
                    f"register-code:email:{email}",
                    limit=RATE_LIMITS["register_code_email"][0],
                    window_seconds=RATE_LIMITS["register_code_email"][1],
                    message="该邮箱验证码发送过于频繁，请稍后再试。",
                )
            _validate_turnstile_if_required(errors, turnstile_required)
            if not _account_email_configured():
                errors.append(render_site_text("register.email_unavailable"))
            if not errors:
                try:
                    _send_registration_code(email)
                    flash("验证码已发送，请查看邮箱并在 15 分钟内完成注册。", "success")
                    code_sent = True
                except Exception as exc:
                    LOGGER.warning("Registration code email failed for %s: %s", email, exc)
                    errors.append("验证码邮件发送失败，请稍后再试。")
        else:
            _rate_limit_or_abort(
                f"register:ip:{_client_ip()}",
                limit=RATE_LIMITS["register_ip"][0],
                window_seconds=RATE_LIMITS["register_ip"][1],
                message="注册请求过于频繁，请稍后再试。",
            )
            _validate_display_name(display_name, errors)
            if len(password) < 8:
                errors.append("密码至少需要 8 位。")
            if password != confirm_password:
                errors.append("两次输入的密码不一致。")
            if not re.fullmatch(r"\d{6}", email_code or ""):
                errors.append("请输入 6 位邮箱验证码。")
            _validate_turnstile_if_required(errors, turnstile_required)
            if not errors and not verify_account_email_code(email=email, purpose="register", code=email_code):
                errors.append("邮箱验证码无效或已过期，请重新获取。")

            if not errors:
                user = create_user(
                    email=email,
                    display_name=display_name,
                    password_hash=generate_password_hash(password),
                    email_verified_at=utc_now_text(),
                    register_ip=_client_ip(),
                )
                _reset_session_preserving_visitor()  # 防会话固定，但保留访客统计标识。
                session["user_id"] = user["id"]
                session.permanent = True
                update_last_login(int(user["id"]), _client_ip())
                flash("注册成功，邮箱已验证。", "success")
                return redirect(next_url)

    return render_template(
        "register.html",
        title="注册账号",
        values=values,
        errors=errors,
        next_url=next_url,
        code_sent=code_sent,
        account_email_configured=_account_email_configured(),
        **_turnstile_template_context(turnstile_required),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if getattr(g, "current_user", None):
        return redirect(url_for("account"))

    next_url = _safe_next_url(request.args.get("next") or request.form.get("next"))
    values = {"email": ""}
    errors: list[str] = []
    login_captcha_required = False

    if request.method == "POST":
        email = normalize_email(request.form.get("email") or "")
        password = request.form.get("password") or ""
        values["email"] = email
        login_captcha_required = _login_failure_count(email) >= LOGIN_CAPTCHA_THRESHOLD
        _rate_limit_or_abort(
            f"login:ip:{_client_ip()}",
            limit=RATE_LIMITS["login_ip"][0],
            window_seconds=RATE_LIMITS["login_ip"][1],
            message="登录请求过于频繁，请稍后再试。",
        )
        if email:
            _rate_limit_or_abort(
                f"login:email:{email}",
                limit=RATE_LIMITS["login_email"][0],
                window_seconds=RATE_LIMITS["login_email"][1],
                message="该邮箱登录尝试过于频繁，请稍后再试。",
            )
        if _login_locked(email):
            errors.append("登录失败次数过多，请 15 分钟后再试。")
        _validate_turnstile_if_required(errors, login_captcha_required)
        user = get_user_by_email(email) if not errors else None
        if not errors and (user is None or not check_password_hash(user["password_hash"], password)):
            _record_login_failure(email)
            errors.append("邮箱或密码不正确。")
            login_captcha_required = _login_failure_count(email) >= LOGIN_CAPTCHA_THRESHOLD
        elif not errors and not user.get("is_active", 1):
            _record_login_failure(email)
            errors.append("该账号已被停用。")
            login_captcha_required = _login_failure_count(email) >= LOGIN_CAPTCHA_THRESHOLD
        elif not errors:
            _clear_login_failures(email)
            _reset_session_preserving_visitor()  # 防会话固定，但保留访客统计标识。
            session["user_id"] = user["id"]
            session.permanent = True
            update_last_login(int(user["id"]), _client_ip())
            flash("登录成功。", "success")
            return redirect(next_url)

    return render_template(
        "login.html",
        title="登录",
        values=values,
        errors=errors,
        next_url=next_url,
        **_turnstile_template_context(login_captcha_required),
    )


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if getattr(g, "current_user", None):
        return redirect(url_for("account"))

    values = {"email": normalize_email(request.args.get("email") or "")}
    errors: list[str] = []
    sent = False
    if request.method == "POST":
        email = normalize_email(request.form.get("email") or "")
        values["email"] = email
        _rate_limit_or_abort(
            f"password-reset:ip:{_client_ip()}",
            limit=RATE_LIMITS["password_reset_ip"][0],
            window_seconds=RATE_LIMITS["password_reset_ip"][1],
            message="找回密码请求过于频繁，请稍后再试。",
        )
        if email:
            _rate_limit_or_abort(
                f"password-reset:email:{email}",
                limit=RATE_LIMITS["password_reset_email"][0],
                window_seconds=RATE_LIMITS["password_reset_email"][1],
                message="该邮箱找回密码请求过于频繁，请稍后再试。",
            )
        if not email or "@" not in email:
            errors.append("请输入有效邮箱。")
        if not _account_email_configured():
            errors.append("邮件服务暂时不可用，暂时无法发送找回密码邮件。请稍后再试。")
        if not errors:
            user = get_user_by_email(email)
            if user and user.get("is_active", 1):
                try:
                    _send_password_reset_email(user)
                except Exception as exc:
                    LOGGER.warning("Password reset email failed for %s: %s", email, exc)
                    errors.append("找回密码邮件发送失败，请稍后再试。")
            if not errors:
                sent = True
                flash("如果该邮箱存在可用账号，系统已发送重置密码邮件。", "success")

    return render_template(
        "forgot_password.html",
        title="找回密码",
        values=values,
        errors=errors,
        sent=sent,
    )


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    if getattr(g, "current_user", None):
        return redirect(url_for("account"))

    entry = get_account_email_token(token=token, purpose="password_reset")
    errors: list[str] = []
    if entry is None:
        errors.append("重置链接无效或已过期，请重新申请。")
    if request.method == "POST" and entry is not None:
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        if len(password) < 8:
            errors.append("密码至少需要 8 位。")
        if password != confirm_password:
            errors.append("两次输入的密码不一致。")
        consumed = None
        if not errors:
            consumed = consume_account_email_token(token=token, purpose="password_reset")
            if consumed is None:
                errors.append("重置链接无效或已过期，请重新申请。")
        if consumed is not None and not errors:
            user = get_user_by_email(str(consumed["email"]))
            if user is None or not user.get("is_active", 1):
                errors.append("该账号不可用。")
            else:
                update_user_password(int(user["id"]), generate_password_hash(password))
                _clear_login_failures(str(user["email"]))
                flash("密码已重置，请使用新密码登录。", "success")
                return redirect(url_for("login", email=user["email"]))

    return render_template(
        "reset_password.html",
        title="重置密码",
        token=token,
        errors=errors,
        valid=entry is not None,
    )


@app.post("/logout")
def logout():
    session.pop("user_id", None)
    flash("你已退出登录。", "success")
    return redirect(url_for("index"))


@app.post("/account/delete")
def delete_account():
    _require_login_page()
    user = g.current_user
    if _is_admin_user(user):
        flash("管理员账号不能自助注销，请先移交或降级管理员权限。", "warning")
        return redirect(url_for("account"))
    password = request.form.get("password") or ""
    confirm_text = (request.form.get("confirm_text") or "").strip()
    if confirm_text != "注销账号":
        flash("请输入“注销账号”确认本次操作。", "warning")
        return redirect(url_for("account"))
    if not check_password_hash(user["password_hash"], password):
        flash("密码不正确，账号未注销。", "warning")
        return redirect(url_for("account"))
    deactivate_user_account(int(user["id"]))
    session.pop("user_id", None)
    flash("账号已注销。", "success")
    return redirect(url_for("index"))


@app.route("/pricing")
def pricing():
    state = current_view_state()
    # 营销决策：会员套餐页不再陈列/出售资源包（credit_pack），只展示按月会员套餐；
    # 已购资源包次数仍可在会员中心查看余额并继续使用，仅停止售卖入口。
    plans = [p for p in list_active_plans() if (p.get("kind") or "membership") != "credit_pack"]
    next_url = _safe_next_url(request.args.get("next"))
    return render_template(
        "pricing.html",
        title="会员套餐",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        state=state,
        plans=plans,
        journal_catalog=journal_source_catalog(),
        next_url=next_url,
        payment_ready=False,
    )


@app.post("/checkout/<plan_code>")
def create_checkout(plan_code: str):
    _require_login_page()
    plan = get_plan(plan_code)
    if not plan or not plan.get("is_active"):
        abort(404, description="未找到可购买的套餐。")
    # 营销决策：已停止售卖资源包（credit_pack）。即便有人持旧链接直达，也一律拒绝下单；
    # 已购次数仍可正常使用，仅关闭新购入口。
    if (plan.get("kind") or "membership") == "credit_pack":
        abort(404, description="该资源包已停止售卖。")
    order = create_pending_order(user_id=int(g.current_user["id"]), plan_code=plan_code)
    return _build_payment_checkout_redirect(order, plan, g.current_user)


def _remember_donation_order(order_no: str) -> None:
    """把打赏订单号记进当前会话，供访客（未登录）后续查看收银轮询 / 支付结果 / 继续支付。"""
    orders = list(session.get("donation_order_nos") or [])
    if order_no not in orders:
        orders.append(order_no)
        session["donation_order_nos"] = orders[-20:]  # 只留最近 20 笔，防会话无限膨胀
        session.modified = True


def _donation_session_owns(order_no: str) -> bool:
    return order_no in (session.get("donation_order_nos") or [])


def _can_view_order(order: dict) -> bool:
    """订单归属校验：登录用户看自己的单；访客打赏单凭会话里记录的 order_no 查看。"""
    user = getattr(g, "current_user", None)
    if user and int(order.get("user_id") or 0) == int(user["id"]):
        return True
    if str(order.get("plan_code") or "") == "donation" and _donation_session_owns(str(order.get("order_no") or "")):
        return True
    return False


@app.post("/donate")
def create_donation():
    """打赏 / 捐赠：金额由用户自定，走 ZPay 与会员同一套下单/回调管线，支付成功只入账不开会员。

    访客与登录用户都可打赏：登录用户挂到自己名下；访客挂到 system 占位账号，并把 order_no 记进会话
    以便其后续查看收银/结果页（无需登录）。
    """
    from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

    raw = (request.form.get("amount") or "").strip()
    try:
        yuan = Decimal(raw)
    except (InvalidOperation, ValueError):
        flash("请输入有效的打赏金额。", "warning")
        return redirect(url_for("index"))
    amount_cents = int((yuan * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if amount_cents < DONATION_MIN_CENTS or amount_cents > DONATION_MAX_CENTS:
        flash(
            f"打赏金额需在 ¥{DONATION_MIN_CENTS // 100} 到 ¥{DONATION_MAX_CENTS // 100} 之间。",
            "warning",
        )
        return redirect(url_for("index"))
    user = getattr(g, "current_user", None)
    payer_id = int(user["id"]) if user else get_or_create_donation_guest_id()
    try:
        order = create_donation_order(user_id=payer_id, amount_cents=amount_cents)
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("index"))
    _remember_donation_order(order["order_no"])
    return _build_donation_checkout_redirect(order, {"id": payer_id})


@app.post("/checkout/order/<order_no>")
def retry_checkout(order_no: str):
    order = get_order_by_no(order_no)
    if order is None:
        _require_login_page()
        abort(404, description="未找到对应订单。")
    is_donation = str(order["plan_code"]) == "donation"
    # 会员单必须登录本人；打赏单登录本人或会话持有者（含访客）均可继续支付。
    if not _can_view_order(order):
        if not is_donation:
            _require_login_page()
        abort(404, description="未找到对应订单。")
    if order["status"] == "paid":
        flash("该订单已支付，无需重新发起支付。", "info")
        return redirect(url_for("payment_result", order_no=order_no))
    if order["status"] != "pending":
        flash("当前订单状态不支持重新支付。", "warning")
        return redirect(url_for("account") if getattr(g, "current_user", None) else url_for("pricing"))

    plan = get_plan(str(order["plan_code"]))
    if not plan or not plan.get("is_active"):
        abort(404, description="该订单对应的套餐已不可用。")
    # 打赏订单金额是每笔自定的，不能走会员那套「按套餐价对账/复用」逻辑，否则会被重建成 ¥0 单。
    if is_donation:
        return _build_donation_checkout_redirect(order, {"id": int(order["user_id"])})
    return _build_payment_checkout_redirect(order, plan, g.current_user)


@app.get("/checkout/order/<order_no>/status")
def checkout_order_status(order_no: str):
    # 收银页轮询：返回订单是否已支付。登录用户查自己的单；访客打赏单凭会话记录的 order_no 查询。
    order = get_order_by_no(order_no)
    if order is None or not _can_view_order(order):
        return jsonify({"ok": False, "status": "not_found"}), 404
    status = str(order["status"])
    return jsonify({"ok": True, "status": status, "paid": status == "paid"})


# 会员中心「我的权益」展示用：把功能权限键归类为面向用户的清单。标签/说明均为面向客户的措辞，
# 不暴露内部权限键名；某项是否开放实时取自 state.feature_access[key]（单一事实源仍是权限策略）。
ACCOUNT_BENEFIT_GROUPS = (
    {
        "title": "阅读与检索",
        "features": (
            {"key": "search", "label": "全文检索", "hint": "马恩列斯毛等经典著作的精确与模糊检索"},
            {"key": "viewer", "label": "检索结果原文", "hint": "查看命中所在的原书页面"},
            {"key": "library", "label": "原典阅读器", "hint": "逐卷逐页阅读扫描原书"},
            {"key": "dictionary", "label": "马克思主义大辞典", "hint": "词条释义检索"},
            {"key": "static_library", "label": "原文文库", "hint": "中外文原著对照阅读"},
            {"key": "stream_reading", "label": "流式阅读", "hint": "《马克思恩格斯文集》网页适配阅读"},
            {"key": "journal_alerts", "label": "期刊新文提醒", "hint": "学科文献综述与新文推送"},
        ),
    },
    {
        "title": "AI 智能助手",
        "features": (
            {"key": "ai", "label": "AI 导学讲解", "hint": "阅读器内逐页智能讲解"},
            {"key": "search_chat", "label": "AI 随心问", "hint": "首页智能问答"},
            {"key": "associative", "label": "联想检索", "hint": "凭大意或残句定位特定原文"},
            {"key": "research", "label": "研究型检索", "hint": "围绕研究命题铺开相关引文与综述"},
            {"key": "ai_web", "label": "AI 联网检索", "hint": "联网增强的智能问答"},
        ),
    },
)


@app.route("/account")
def account():
    _require_login_page()
    user_id = int(g.current_user["id"])
    orders = list_orders_for_user(user_id)
    subscriptions = list_subscriptions_for_user(user_id)
    return render_template(
        "account.html",
        title="会员中心",
        state=current_view_state(),
        orders=orders,
        subscriptions=subscriptions,
        journal_subscriptions=list_journal_subscriptions_for_user(user_id),
        plans=list_active_plans(),
        benefit_groups=ACCOUNT_BENEFIT_GROUPS,
        notes_access_enabled=_notes_access_enabled(),
        payment_ready=False,
        membership_db_path=str(MEMBERSHIP_DB_PATH),
    )


@app.route("/account/journal-alerts")
def account_journal_alerts():
    _require_login_page()
    user_id = int(g.current_user["id"])
    smtp_enabled = load_smtp_config().enabled
    journal_alerts_allowed = bool(_feature_effective_for_user("journal_alerts"))
    return render_template(
        "journal_alerts.html",
        title="期刊新文提醒",
        app_name=APP_NAME,
        state=current_view_state(),
        journal_subscriptions=list_journal_subscriptions_for_user(user_id),
        smtp_enabled=smtp_enabled,
        journal_alerts_allowed=journal_alerts_allowed,
        can_subscribe=bool(journal_alerts_allowed and smtp_enabled),
    )


@app.post("/account/journal-alerts/subscribe")
def account_journal_alerts_subscribe():
    _require_login_page()
    if not _feature_effective_for_user("journal_alerts"):
        abort(403, description="当前账号暂未开放期刊提醒权限。")
    smtp_config = load_smtp_config()
    if not smtp_config.enabled:
        flash(render_site_text("journal.email_unavailable"), "warning")
        return redirect(url_for("account_journal_alerts"))
    if not _is_admin_user(g.current_user):
        _rate_limit_or_abort(
            f"journal:user:{g.current_user['id']}",
            limit=RATE_LIMITS["journal_user"][0],
            window_seconds=RATE_LIMITS["journal_user"][1],
            message="订阅请求过于频繁，请稍后再试。",
        )
    email = normalize_email(request.form.get("email") or g.current_user.get("email") or "")
    try:
        subscription = create_or_update_subscription(int(g.current_user["id"]), email)
        send_confirmation_email(
            subscription,
            journal_alert_public_base_url(DEPLOYMENT),
            smtp_config,
        )
    except Exception as exc:
        flash(f"订阅确认邮件发送失败：{exc}", "warning")
    else:
        flash("确认邮件已经发出，请到邮箱点击确认链接后生效。", "success")
    return redirect(url_for("account_journal_alerts"))


@app.post("/account/journal-alerts/unsubscribe")
def account_journal_alerts_unsubscribe():
    _require_login_page()
    subscription_id = request.form.get("subscription_id", type=int) or 0
    if not journal_unsubscribe_by_id(int(g.current_user["id"]), subscription_id):
        abort(404, description="未找到对应订阅。")
    flash("期刊新文提醒已退订。", "success")
    return redirect(url_for("account_journal_alerts"))


@app.route("/journal-alerts/confirm/<token>")
def journal_alerts_confirm(token: str):
    subscription = confirm_subscription(token)
    if not subscription:
        abort(404, description="确认链接无效或已过期。")
    flash("期刊新文提醒已确认，后续有新文章会发送到该邮箱。", "success")
    return redirect(url_for("login"))


@app.route("/journal-alerts/unsubscribe/<token>")
def journal_alerts_unsubscribe_token(token: str):
    subscription = journal_unsubscribe_by_token(token)
    if not subscription:
        abort(404, description="退订链接无效。")
    flash("期刊新文提醒已退订。", "success")
    return redirect(url_for("index"))


@app.route("/journal-alerts/latest")
def journal_alerts_latest():
    # 首页「期刊订阅」栏目入口：展示本期（当前批次）已发布文章；
    # 支持 ?d=<批次id> 查阅上一期/历史期数（仅限已发送/已归档批次，防越权枚举草稿）。
    # 严格按控制台「期刊提醒」权限放行（访客/未授权 → 403）。
    if not _feature_effective_for_user("journal_alerts"):
        abort(403, description="当前账号暂未开放期刊提醒权限。")

    # 对外可翻阅的历史期（新→旧，含本期已发送 + 历史归档）。
    public_batches = list_public_batches()

    # 默认展示「本期」：最近一次已发送批次（留存到下次发送），尚无发送则退回在建批次预览。
    default_batch = latest_public_batch()

    # ?d 指定历史期：必须命中公开期列表，否则视为不可见/不存在。
    requested_id = request.args.get("d", type=int)
    if requested_id is not None:
        batch = next((b for b in public_batches if int(b["id"]) == requested_id), None)
        if batch is None:
            abort(404, description="未找到该期内容，或该期暂不可查阅。")
    else:
        batch = default_batch

    articles: list[dict] = []
    if batch:
        # 已批准/已发送(ready) 优先；历史归档批次的文章为 archived 态，需一并纳入；
        # 当批尚未审核时回退 pending_review 以便预览。
        articles = batch_articles(int(batch["id"]), statuses=("ready",))
        if not articles:
            articles = batch_articles(int(batch["id"]), statuses=("ready", "pending_review", "archived"))
    review_html = ""
    if batch and str(batch.get("review_status") or "") == "approved":
        review_html = str(batch.get("review_html") or "")

    # 翻页：在公开期列表中定位当前批次，更旧者为「上一期」、更新者为「下一期」。
    # 若当前展示的是在建批次（不在公开列表），其「上一期」即最新一次已发送批次。
    prev_batch = next_batch = None
    if batch:
        ids = [int(b["id"]) for b in public_batches]
        bid = int(batch["id"])
        if bid in ids:
            i = ids.index(bid)
            prev_batch = public_batches[i + 1] if i + 1 < len(public_batches) else None
            next_batch = public_batches[i - 1] if i - 1 >= 0 else None
        elif public_batches:
            prev_batch = public_batches[0]

    is_historical = bool(
        batch and default_batch and int(batch["id"]) != int(default_batch["id"])
    )
    return render_template(
        "journal_latest.html",
        title=("历史期 · 期刊新文" if is_historical else "本期期刊新文"),
        app_name=APP_NAME,
        state=current_view_state(),
        batch=batch,
        articles=articles,
        review_html=review_html,
        public_batches=public_batches,
        prev_batch=prev_batch,
        next_batch=next_batch,
        is_historical=is_historical,
        viewing_id=(int(batch["id"]) if batch else None),
        can_subscribe=bool(_feature_effective_for_user("journal_alerts") and load_smtp_config().enabled),
    )


@app.route("/journal-alerts/pdf/<int:article_id>")
def journal_alerts_pdf(article_id: int):
    """文献 PDF 按需下载：首次拉取并镜像到本地后直发；拿不到真 PDF 时优雅跳转源站。
    与「本期新文」页同权限（期刊提醒），仅对可对外的文章开放，防越权枚举草稿。"""
    if not _feature_effective_for_user("journal_alerts"):
        abort(403, description="当前账号暂未开放期刊提醒权限。")
    _rate_limit_reader_ip_or_abort("journalpdf")
    article = get_public_journal_article(article_id)
    if not article:
        abort(404, description="未找到该文献。")
    pdf_url = str(article.get("pdf_url") or "").strip()
    if not pdf_url:
        abort(404, description="该文献暂无可下载的 PDF。")
    # NCPSSD 文章页（全文受登录+WAF 限制）：直接跳转源站阅读，不做无谓镜像。
    if _is_ncpssd_landing_url(pdf_url):
        return redirect(pdf_url)
    cache_path = _mirror_journal_pdf_to_cache(article_id, pdf_url)
    _prune_journal_pdf_cache_if_due()
    if cache_path is None:
        return redirect(pdf_url)  # 镜像失败（非 PDF/被拦/超限）→ 跳转来源
    return send_file(
        cache_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=_journal_pdf_download_name(article),
        conditional=True,
        max_age=86400,
    )


@app.route("/control")
def control():
    _require_local_console()
    return render_template("control.html", **_management_console_context(remote_admin=False))


@app.post("/control/desktop/activate")
def control_desktop_activate():
    _require_local_console()
    _require_management_csrf()
    try:
        activate_desktop_sync(
            server_url=(request.form.get("server_url") or "").strip(),
            activation_code=(request.form.get("activation_code") or "").strip(),
            label=(request.form.get("label") or socket.gethostname()).strip(),
        )
    except Exception as exc:
        flash(f"网站授权失败：{exc}", "warning")
    else:
        flash("本地端已完成网站授权并写入同步缓存。", "success")
    return _management_redirect(False, "sync")


@app.post("/control/desktop/sync")
def control_desktop_sync():
    _require_local_console()
    _require_management_csrf()
    cache = sync_desktop_runtime()
    if cache.get("last_error"):
        flash(f"同步失败：{cache.get('last_error')}", "warning")
    else:
        flash("已从网站后台同步最新授权、文案、AI 代理状态和发布信息。", "success")
    return _management_redirect(False, "sync")


@app.post("/control/desktop/cache/clear")
def control_desktop_cache_clear():
    _require_local_console()
    _require_management_csrf()
    save_desktop_sync_cache({})
    flash("本地同步缓存已清除。", "success")
    return _management_redirect(False, "sync")


@app.route("/admin/2fa", methods=["GET", "POST"])
def admin_2fa():
    # 管理后台邮箱二次验证页：本路由自行做「已登录 + 管理员角色」校验，但不走二次因子
    # 闸门（否则会和 _require_admin 形成死循环）。
    user = getattr(g, "current_user", None)
    if not user:
        raise _RedirectTo(url_for("login", next=url_for("admin")))
    if not _is_admin_user(user):
        abort(403, description="当前账号没有管理后台权限。")
    _enforce_admin_ip_allowlist()
    next_url = _safe_next_url(request.args.get("next") or request.form.get("next") or url_for("admin"))
    # 未启用二次验证（桌面 / 未配邮箱 / 已显式关闭）或本会话已验证：直接放行回后台。
    if not _admin_2fa_enabled() or _admin_2fa_session_ok():
        return redirect(next_url)
    email = str(user.get("email") or "")
    errors: list[str] = []
    if request.method == "POST":
        action = (request.form.get("action") or "verify").strip()
        if action == "resend":
            _dispatch_admin_2fa_code(email, errors)
            if not errors:
                flash("验证码已发送，请查收邮箱（1 分钟内不重复发送）。", "success")
        else:
            code = (request.form.get("code") or "").strip()
            if not re.fullmatch(r"\d{6}", code or ""):
                errors.append("请输入 6 位邮箱验证码。")
            elif not verify_account_email_code(email=email, purpose=ADMIN_2FA_PURPOSE, code=code):
                errors.append("验证码无效或已过期，请重新获取。")
            else:
                _mark_admin_2fa_verified()
                session.pop("admin_2fa_sent_at", None)
                _log_management_action(action="admin_2fa_verify", target=_mask_email(email), result="ok", remote_admin=True)
                return redirect(next_url)
    else:
        _dispatch_admin_2fa_code(email, errors)
    return render_template(
        "admin_2fa.html",
        title="管理员二次验证",
        errors=errors,
        email_masked=_mask_email(email),
        next_url=next_url,
    )


@app.route("/admin")
@app.route("/admin/<module>")
def admin(module: str = "overview"):
    _require_admin()
    if module not in ADMIN_MODULES:
        abort(404, description="管理模块不存在。")
    return render_template("control.html", **_management_console_context(remote_admin=True, admin_module=module))


@app.route("/admin/overview/export.xlsx")
def admin_dashboard_export():
    _require_admin()
    today = _beijing_now().date().isoformat()
    if (request.args.get("all") or "").strip() == "1":
        start_day = get_admin_dashboard_first_day()
        end_day = today
        days = _dashboard_date_range(start_day, end_day)
    else:
        default_start = (_beijing_now().date() - timedelta(days=DASHBOARD_DEFAULT_HISTORY_DAYS - 1)).isoformat()
        start_day = _parse_dashboard_day(request.args.get("history_start", ""), default_start)
        end_day = _parse_dashboard_day(request.args.get("history_end", ""), today)
        days = _dashboard_date_range(start_day, end_day, max_days=DASHBOARD_MAX_HISTORY_DAYS)
    history_rows = _dashboard_history_rows(days)
    workbook = _build_xlsx(
        [
            ("历史指标", _dashboard_history_sheet_rows(history_rows)),
            ("高Token用户", _dashboard_token_sheet_rows(history_rows)),
            ("计算规则", _dashboard_rules_sheet_rows()),
        ]
    )
    filename = f"dashboard-history-{days[0]}-{days[-1]}.xlsx"
    return send_file(
        BytesIO(workbook),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.post("/control/ai")
def control_ai():
    return _handle_ai_settings_submit(remote_admin=False)


@app.post("/control/site-texts")
def control_site_texts():
    return _handle_site_texts_submit(remote_admin=False)


@app.post("/control/plans")
def control_plans():
    return _handle_plans_submit(remote_admin=False)


@app.post("/control/memberships/grant")
def control_membership_grant():
    return _handle_membership_grant_submit(remote_admin=False)


@app.post("/control/memberships/bulk-grant")
def control_membership_bulk_grant():
    return _handle_membership_bulk_grant_submit(remote_admin=False)


@app.post("/control/users/<int:user_id>")
def control_user_update(user_id: int):
    return _handle_user_update_submit(user_id, remote_admin=False)


@app.post("/admin/ai")
def admin_ai():
    return _handle_ai_settings_submit(remote_admin=True)


@app.post("/admin/site-texts")
def admin_site_texts():
    return _handle_site_texts_submit(remote_admin=True)


@app.post("/admin/notice")
def admin_notice():
    return _handle_notice_submit(remote_admin=True)


@app.post("/control/notice")
def control_notice():
    return _handle_notice_submit(remote_admin=False)


@app.post("/admin/community")
def admin_community():
    return _handle_community_submit(remote_admin=True)


@app.post("/control/community")
def control_community():
    return _handle_community_submit(remote_admin=False)


@app.post("/admin/community/from-feedback")
def admin_community_from_feedback():
    """『社区建设』栏的智能助手：把最近的用户留言提炼成简短、健康的陈列条目（邮箱打码署名），
    以 JSON 返回给控制台填入编辑框，由管理员复核后再点『保存社区建设』发布到首页。仅网站 /admin
    可用；生成本身不落库，绝不越过复核直接改动首页。"""
    _require_admin()
    _require_management_csrf()
    if not AI_CONFIG.enabled:
        return jsonify({"ok": False, "error": "AI 功能当前未启用，无法提炼留言。"})
    # 编辑框现有内容（含已保存 + 尚未保存的行）：据此跳过已陈列过的用户，已有的不必再生成。
    existing = str(request.form.get("existing") or "")[:20000]
    items = _collect_feedback_for_community(existing_text=existing)
    if not items:
        message = ("近期有留言的用户似乎都已在社区栏中，无需重复生成。"
                   if existing.strip() else "暂无用户留言可供提炼。")
        return jsonify({"ok": True, "items": [], "count": 0, "source_count": 0, "message": message})
    try:
        lines = _distill_feedback_to_community(items)
    except AIServiceError as exc:
        return jsonify({"ok": False, "error": f"AI 提炼失败：{exc}"})
    except Exception:  # 防御：模型/解析异常不应把控制台打成 500
        LOGGER.exception("community-from-feedback distill failed")
        return jsonify({"ok": False, "error": "提炼过程出错，请稍后重试。"})
    _log_management_action(
        action="community.from_feedback",
        target="index.community",
        result="success",
        remote_admin=True,
        details={"source_count": len(items), "generated": len(lines)},
    )
    return jsonify({"ok": True, "items": lines, "count": len(lines), "source_count": len(items)})


@app.post("/admin/feature-tags")
def admin_feature_tags():
    return _handle_feature_tags_submit(remote_admin=True)


@app.post("/control/feature-tags")
def control_feature_tags():
    return _handle_feature_tags_submit(remote_admin=False)


@app.post("/admin/citation-formats")
def admin_citation_formats():
    return _handle_citation_formats_submit(remote_admin=True)


@app.post("/control/citation-formats")
def control_citation_formats():
    return _handle_citation_formats_submit(remote_admin=False)


@app.post("/admin/card-order")
def admin_card_order():
    return _handle_card_order_submit(remote_admin=True)


@app.post("/control/card-order")
def control_card_order():
    return _handle_card_order_submit(remote_admin=False)


@app.post("/admin/registry-geo")
def admin_registry_geo():
    return _handle_registry_geo_submit(remote_admin=True)


@app.post("/control/registry-geo")
def control_registry_geo():
    return _handle_registry_geo_submit(remote_admin=False)


@app.post("/admin/ai-assistant-mode")
def admin_ai_assistant_mode():
    return _handle_ai_assistant_mode_submit(remote_admin=True)


@app.post("/control/ai-assistant-mode")
def control_ai_assistant_mode():
    return _handle_ai_assistant_mode_submit(remote_admin=False)


@app.post("/admin/sponsor")
def admin_sponsor():
    return _handle_sponsor_submit(remote_admin=True)


@app.post("/control/sponsor")
def control_sponsor():
    return _handle_sponsor_submit(remote_admin=False)


@app.get("/admin/content/scan")
def admin_content_scan():
    return _handle_site_text_scan(remote_admin=True)


@app.post("/admin/content/prune")
def admin_content_prune():
    return _handle_site_text_prune(remote_admin=True)


@app.get("/control/content/scan")
def control_content_scan():
    return _handle_site_text_scan(remote_admin=False)


@app.post("/admin/plans")
def admin_plans():
    return _handle_plans_submit(remote_admin=True)


@app.post("/admin/research-quota")
def admin_research_quota():
    return _handle_research_quota_submit(remote_admin=True)


@app.post("/admin/reset-research-quota")
def admin_reset_research_quota():
    return _handle_reset_research_quota_submit(remote_admin=True)


@app.post("/admin/ai-token-quota")
def admin_ai_token_quota():
    return _handle_ai_token_quota_submit(remote_admin=True)


@app.post("/admin/reset-ai-token-quota")
def admin_reset_ai_token_quota():
    return _handle_reset_ai_token_quota_submit(remote_admin=True)


@app.post("/admin/memberships/grant")
def admin_membership_grant():
    return _handle_membership_grant_submit(remote_admin=True)


@app.post("/admin/memberships/bulk-grant")
def admin_membership_bulk_grant():
    return _handle_membership_bulk_grant_submit(remote_admin=True)


@app.post("/admin/users/<int:user_id>")
def admin_user_update(user_id: int):
    return _handle_user_update_submit(user_id, remote_admin=True)


@app.post("/admin/access-policy")
def admin_access_policy():
    return _handle_access_policy_submit(remote_admin=True)


@app.post("/admin/ai-access")
def admin_ai_access():
    return _handle_ai_access_toggle(remote_admin=True)


@app.post("/admin/reader-access-ban")
def admin_reader_access_ban():
    return _handle_reader_access_ban(remote_admin=True)


@app.get("/admin/zhipu-selftest")
def admin_zhipu_selftest():
    """智谱联网通道一键自检：直接调独立检索端点 + 最小化强制联网对话，把确切原因
    （如账户搜索资源耗尽的 1113 报错、Key 无效、检索未注入的 prompt_tokens 指纹）
    返回给后台，不再需要登服务器看日志。会产生一次真实检索与少量 token 计费。"""
    _require_admin()
    try:
        report = AI_CLIENT.zhipu_web_selftest()
    except Exception as exc:  # 自检绝不能把后台搞挂
        LOGGER.warning("zhipu selftest crashed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    _log_management_action(
        action="ai.zhipu_selftest",
        target="zhipu",
        result=str(report.get("verdict") or "")[:200],
        remote_admin=True,
    )
    return jsonify({"ok": True, "report": report})


@app.get("/admin/ai-usage")
def admin_ai_usage():
    """返回某用户最近 AI 请求明细的 JSON，供总览页“查看用量详情”弹窗使用。"""
    _require_admin()
    try:
        user_id = int(request.args.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if not user_id:
        return jsonify({"ok": False, "error": "缺少用户 ID。"}), 400
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"ok": False, "error": "未找到该用户。"}), 404
    day = _parse_dashboard_day(request.args.get("date", ""), "")
    items = list_ai_usage_for_user(user_id, day=day or None, limit=80)
    policy = _load_access_policy()
    ai_blocked = normalize_email(str(user.get("email") or "")) in _ai_blocked_emails(policy)
    providers = sorted({str(it.get("provider") or "").strip() for it in items if (it.get("provider") or "").strip()})
    models = sorted({str(it.get("model") or "").strip() for it in items if (it.get("model") or "").strip()})
    client_ips = sorted({str(it.get("client_ip") or "").strip() for it in items if (it.get("client_ip") or "").strip()})
    return jsonify(
        {
            "ok": True,
            "user": {
                "id": user.get("id"),
                "email": user.get("email"),
                "display_name": user.get("display_name"),
                "ai_blocked": ai_blocked,
            },
            "summary": {
                "request_count": len(items),
                "providers": providers,
                "models": models,
                "client_ips": client_ips,
                # 服务端实际固定使用的上游接口，用户无法在请求里改写——可据此判断是否被“转接”。
                "configured_provider": AI_CONFIG.provider,
                "configured_model": AI_CONFIG.model,
                "configured_base_url": AI_CONFIG.base_url,
            },
            "items": items,
        }
    )


@app.get("/admin/reader-access")
def admin_reader_access():
    _require_admin()
    actor_key = (request.args.get("actor") or "").strip()
    if not actor_key:
        return jsonify({"ok": False, "error": "缺少访客标识。"}), 400
    day = _parse_dashboard_day(request.args.get("date", ""), "")
    items = list_reader_access_events(actor_key=actor_key, day=day or None, limit=160)
    actor = items[0] if items else {"actor_key": actor_key}
    policy = _load_access_policy()
    bans = _reader_bans()
    endpoints = sorted({str(it.get("endpoint") or "").strip() for it in items if (it.get("endpoint") or "").strip()})
    source_files = sorted({str(it.get("source_file") or "").strip() for it in items if (it.get("source_file") or "").strip()})
    pages = {
        f"{it.get('source_file')}:{it.get('page')}"
        for it in items
        if (it.get("source_file") or "") and int(it.get("page") or 0) > 0
    }
    return jsonify(
        {
            "ok": True,
            "actor": {
                "actor_key": actor_key,
                "actor_type": actor.get("actor_type") or ("user" if actor_key.startswith("user:") else "ip"),
                "user_id": actor.get("user_id"),
                "email": actor.get("email") or "",
                "client_ip": actor.get("client_ip") or "",
                "user_agent": actor.get("user_agent") or "",
                "reader_blocked": _reader_ban_status(actor, policy=policy, bans=bans),
            },
            "summary": {
                "request_count": len(items),
                "endpoints": endpoints,
                "source_file_count": len(source_files),
                "page_count": len(pages),
            },
            "items": items,
        }
    )


@app.get("/admin/online-series")
def admin_online_series():
    """返回最近 24 小时、15 分钟粒度的在线人数序列，供总览页在线变化图使用。

    每个时槽给出三条口径：所有访问者(含访客) / 注册用户 / 会员用户，前端按需切换。
    """
    _require_admin()
    now_utc = datetime.now(timezone.utc)
    end_bucket = now_utc.replace(minute=(now_utc.minute // 15) * 15, second=0, microsecond=0)
    # 24 小时 = 96 个 15 分钟时槽，含当前时槽。
    start_bucket = end_bucket - timedelta(minutes=15 * 95)
    now_text = now_utc.isoformat(timespec="seconds")
    series = get_online_presence_series(
        since_text=start_bucket.isoformat(timespec="seconds"),
        until_text=end_bucket.isoformat(timespec="seconds"),
        member_as_of=now_text,
    )
    beijing = timezone(timedelta(hours=8))
    buckets: list[dict] = []
    cursor = start_bucket
    while cursor <= end_bucket:
        key = cursor.isoformat(timespec="seconds")
        row = series.get(key) or {}
        local = cursor.astimezone(beijing)
        buckets.append(
            {
                "label": local.strftime("%H:%M"),
                "full_label": local.strftime("%m-%d %H:%M"),
                "total": int(row.get("total") or 0),
                "registered": int(row.get("registered") or 0),
                "members": int(row.get("members") or 0),
            }
        )
        cursor += timedelta(minutes=15)
    return jsonify(
        {
            "ok": True,
            "interval_minutes": 15,
            "buckets": buckets,
            "generated_at": _display_datetime(now_text),
        }
    )


@app.post("/admin/email/test")
def admin_email_test():
    _require_admin()
    _require_management_csrf()
    to_email = normalize_email(request.form.get("test_email") or "")
    if not to_email or "@" not in to_email:
        flash("请输入有效的测试收件邮箱。", "warning")
        return _management_redirect(True, "journal-alerts")
    smtp_config = load_smtp_config()
    if not smtp_config.enabled:
        flash("全站发信邮箱尚未配置，请先配置发信邮箱后再测试。", "warning")
        return _management_redirect(True, "journal-alerts")
    body = (
        "您好：\n\n"
        "这是一封全站发信测试邮件。\n\n"
        "收到这封邮件，表示注册邮箱验证码、找回密码、期刊新文提醒将使用同一套 SMTP 发信配置。"
    )
    try:
        send_email(smtp_config, to_email, "马著作检索发信测试", body, _plain_text_html(body))
    except Exception as exc:
        LOGGER.warning("SMTP test email failed for %s: %s", to_email, exc)
        flash(f"测试邮件发送失败：{exc}", "warning")
    else:
        flash(f"测试邮件已发送到 {to_email}。", "success")
    return _management_redirect(True, "journal-alerts")


# ---- 内容运营·站内群发邮件 -------------------------------------------------
# 给注册用户 / 各等级会员 / 指定邮箱群发一封可 Markdown 排版的公告邮件。逐封 SMTP 发送是
# 分钟级阻塞活，与期刊发送一样放到后台单飞线程；每封落 broadcast_deliveries，汇总写回
# broadcast_campaigns，管理员刷新后台在「群发记录」里查看结果。
_broadcast_job_lock = threading.Lock()

# 群发草稿：后台「保存草稿」落 settings；没有已存草稿时回退到随代码部署的默认稿
# （config/broadcast_draft.md，首行 subject: 主题，其后 --- 分隔，余下为 Markdown 正文），
# 这样拟好的公告可以随部署直接出现在群发表单里供站长审阅、修改后发送。
BROADCAST_DRAFT_SETTING_KEY = "broadcast_draft"
BROADCAST_DRAFT_DEFAULT_PATH = ROOT / "config" / "broadcast_draft.md"


def _broadcast_draft_default() -> dict:
    try:
        raw = BROADCAST_DRAFT_DEFAULT_PATH.read_text(encoding="utf-8")
    except OSError:
        return {"subject": "", "body": "", "saved_at": "", "source": ""}
    subject = ""
    lines = raw.splitlines()
    if lines and lines[0].strip().lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        lines = lines[1:]
        if lines and lines[0].strip() == "---":
            lines = lines[1:]
    body = "\n".join(lines).strip("\n")
    if not (subject or body.strip()):
        return {"subject": "", "body": "", "saved_at": "", "source": ""}
    return {"subject": subject, "body": body, "saved_at": "", "source": "config"}


def _broadcast_draft() -> dict:
    saved = get_setting(BROADCAST_DRAFT_SETTING_KEY, {})
    if isinstance(saved, dict) and (str(saved.get("subject") or "").strip() or str(saved.get("body") or "").strip()):
        return {
            "subject": str(saved.get("subject") or ""),
            "body": str(saved.get("body") or ""),
            "saved_at": str(saved.get("saved_at") or ""),
            "source": "saved",
        }
    return _broadcast_draft_default()


def _start_broadcast_job_async(fn) -> bool:
    """单飞：已有群发任务在跑则返回 False；否则起 daemon 线程跑 fn 并返回 True。"""
    if not _broadcast_job_lock.acquire(blocking=False):
        return False

    def _runner() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 — 后台任务异常记日志，结果/失败由 campaign 落库体现
            LOGGER.exception("Broadcast background job failed")
        finally:
            _broadcast_job_lock.release()

    threading.Thread(target=_runner, name="broadcast-send", daemon=True).start()
    return True


def _broadcast_form_emails() -> list[str]:
    raw = request.form.get("emails") or ""
    return [item for item in re.split(r"[\s,;，、；]+", raw) if item.strip()]


def _broadcast_form_plan_codes() -> list[str]:
    return [c.strip() for c in request.form.getlist("plan_codes") if c.strip()]


@app.post("/admin/broadcast/draft")
def admin_broadcast_draft():
    """保存群发草稿（主题+正文）到 settings，供下次打开后台时自动载入。管理员 + CSRF。"""
    _require_admin()
    _require_management_csrf()
    subject = (request.form.get("subject") or "").strip()
    body_md = request.form.get("body") or ""
    set_setting(
        BROADCAST_DRAFT_SETTING_KEY,
        {"subject": subject, "body": body_md, "saved_at": utc_now_text()},
        updated_by=_management_actor_label(True),
    )
    flash("群发草稿已保存，下次打开本页会自动载入。", "success")
    return _management_redirect(True, "broadcast")


@app.post("/admin/broadcast/preview")
def admin_broadcast_preview():
    """AJAX：返回排版后的邮件 HTML 预览 + 按当前范围预估的收件人数。管理员 + CSRF。"""
    _require_admin()
    _require_management_csrf()
    subject = (request.form.get("subject") or "").strip()
    body_md = request.form.get("body") or ""
    scope = (request.form.get("scope") or "").strip().lower()
    plan_codes = _broadcast_form_plan_codes()
    emails = _broadcast_form_emails()
    try:
        html_preview = render_broadcast_html(body_md)
    except Exception as exc:  # 渲染绝不能把后台搞挂
        LOGGER.warning("broadcast preview render failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    count = 0
    if scope in BROADCAST_SCOPES:
        try:
            count = count_broadcast_recipients(scope, plan_codes=plan_codes, emails=emails)
        except Exception as exc:  # noqa: BLE001 — 计数失败不影响预览排版
            LOGGER.warning("broadcast recipient count failed: %s", exc)
    return jsonify({"ok": True, "html": html_preview, "subject": subject, "recipient_count": count})


@app.post("/admin/broadcast/send")
def admin_broadcast_send():
    """站内群发：解析受众→建 campaign→后台线程逐封发送。远程后台专用（本地控制台不发信）。"""
    _require_admin()
    _require_management_csrf()
    action = (request.form.get("action") or "send").strip().lower()
    subject = (request.form.get("subject") or "").strip()
    body_md = (request.form.get("body") or "").strip()
    scope = (request.form.get("scope") or "").strip().lower()
    plan_codes = _broadcast_form_plan_codes()
    emails = _broadcast_form_emails()

    if not subject:
        flash("请填写邮件主题。", "warning")
        return _management_redirect(True, "broadcast")
    if not body_md:
        flash("请填写邮件正文。", "warning")
        return _management_redirect(True, "broadcast")

    smtp_config = load_smtp_config()
    if not smtp_config.enabled:
        flash("全站发信邮箱尚未配置，无法发送。请先在「期刊订阅」页配置发信邮箱后再群发。", "warning")
        return _management_redirect(True, "broadcast")

    # 测试发送：给指定测试邮箱发一封，同步即时返回，方便在真实邮箱里核对排版效果。
    if action == "test":
        test_email = normalize_email(request.form.get("test_email") or "")
        if not test_email or "@" not in test_email:
            flash("请输入有效的测试收件邮箱。", "warning")
            return _management_redirect(True, "broadcast")
        recipient = {"email": test_email, "display_name": "", "user_id": None}
        try:
            subj, text_body, html_body = render_broadcast_email(subject, body_md, recipient)
            send_email(smtp_config, test_email, subj or subject, text_body, html_body)
        except Exception as exc:
            LOGGER.warning("broadcast test email failed for %s: %s", test_email, exc)
            flash(f"测试邮件发送失败：{exc}", "warning")
        else:
            flash(f"测试邮件已发送到 {test_email}，请到邮箱核对排版效果后再正式群发。", "success")
        return _management_redirect(True, "broadcast")

    if scope not in BROADCAST_SCOPES:
        flash("请选择有效的收件范围。", "warning")
        return _management_redirect(True, "broadcast")
    if scope == "plans" and not plan_codes:
        flash("请至少选择一个会员等级。", "warning")
        return _management_redirect(True, "broadcast")
    if scope == "specific" and not emails:
        flash("请填写至少一个收件邮箱。", "warning")
        return _management_redirect(True, "broadcast")
    # 面向「全部注册用户 / 全部有效会员 / 久未回访用户」的大范围群发不可撤销，必须显式勾选确认框，前后端双重把关。
    if scope in {"registered", "members", "dormant_noip"} and not _form_bool("confirm_all"):
        flash("面向「全部注册用户」「全部有效会员」或「久未回访用户」群发，请先勾选下方确认框。", "warning")
        return _management_redirect(True, "broadcast")

    recipients = resolve_broadcast_recipients(scope, plan_codes=plan_codes, emails=emails)
    if not recipients:
        flash("按当前范围没有解析到任何收件人，请检查收件范围与参数。", "warning")
        return _management_redirect(True, "broadcast")

    campaign_id = create_broadcast_campaign(
        subject=subject,
        body_md=body_md,
        scope=scope,
        plan_codes=plan_codes,
        total_recipients=len(recipients),
        created_by=_management_actor_label(True),
    )
    started = _start_broadcast_job_async(
        lambda: send_broadcast_campaign(
            campaign_id, recipients, subject=subject, body_md=body_md, smtp_config=smtp_config
        )
    )
    if not started:
        # 已有群发任务在跑：把本次 campaign 标记失败并留清晰记录，避免它永远停在「发送中」。
        finish_broadcast_campaign(
            campaign_id, sent=0, failed=len(recipients), error="另一群发任务正在进行，请稍后重试。"
        )
        flash("已有群发任务正在发送，请稍后刷新本页在「群发记录」中确认后再发起。", "warning")
        return _management_redirect(True, "broadcast")

    _log_management_action(
        action="broadcast.send",
        target=scope,
        result="started",
        remote_admin=True,
        details={
            "campaign_id": campaign_id,
            "recipients": len(recipients),
            "plan_codes": plan_codes,
            "subject": subject[:120],
        },
    )
    flash(
        f"已开始向 {len(recipients)} 位收件人群发邮件，请稍后刷新本页在「群发记录」中查看发送结果。",
        "success",
    )
    return _management_redirect(True, "broadcast")


# 期刊采集/综述/发送都是分钟级的阻塞活（多次外呼 25s 超时 + 逐文 AI 翻译 + 逐收件人 SMTP）。
# 过去直接在 admin POST 的请求线程上跑，单次点击就把 1/8 个 waitress 线程钉死数分钟，还可能超 CF 100s。
# 这里改为「单飞 + 后台线程」：同一时刻至多一个期刊后台任务，重复点击快速提示「进行中」，结果落库后
# 管理员刷新页面在「运行记录 / 批次 / 投递记录」里查看（这些函数都取显式入参、不依赖请求上下文）。
_journal_job_lock = threading.Lock()
_journal_job_state: dict = {"name": ""}


def _start_journal_job_async(name: str, fn) -> bool:
    """单飞调度：已有期刊后台任务在跑则返回 False；否则起 daemon 线程跑 fn 并返回 True。"""
    if not _journal_job_lock.acquire(blocking=False):
        return False
    _journal_job_state["name"] = name

    def _runner() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 — 后台任务异常只记日志，结果/失败由各自落库的运行记录体现
            LOGGER.exception("Journal background job %s failed", name)
        finally:
            _journal_job_state["name"] = ""
            _journal_job_lock.release()

    threading.Thread(target=_runner, name=f"journal-{name}", daemon=True).start()
    return True


@app.post("/admin/journal-alerts/run")
def admin_journal_run():
    _require_admin()
    _require_management_csrf()
    action = (request.form.get("action") or "send").strip().lower()
    base_url = journal_alert_public_base_url(DEPLOYMENT)
    busy_msg = "已有期刊后台任务进行中，请稍后刷新本页在运行记录中查看结果。"

    # 新批次化操作：仅采集 / 生成综述 / 发送综述。
    if action in {"collect", "fetch_only"}:
        if _start_journal_job_async("collect", lambda: collect_batch(ai_client=AI_CLIENT)):
            flash("已开始采集，请稍后刷新本页在「运行记录 / 批次」中查看结果。", "success")
        else:
            flash(busy_msg, "warning")
        return _management_redirect(True, "journal-alerts")

    if action == "generate_review":
        batch = current_batch()
        if not batch:
            flash("当前没有可生成综述的批次，请先采集。", "warning")
            return _management_redirect(True, "journal-alerts")
        if not AI_CONFIG.enabled:
            flash("AI 未启用，将生成降级版综述（仅分组列出标题与引文）。", "warning")
        bid = int(batch["id"])
        if _start_journal_job_async(
            "generate_review", lambda: generate_batch_review(bid, ai_client=AI_CLIENT, auto_approve=False)
        ):
            flash(f"已开始生成批次 #{bid} 的文献综述，请稍后刷新本页审核。", "success")
        else:
            flash(busy_msg, "warning")
        return _management_redirect(True, "journal-alerts")

    if action in {"send_batch", "send"}:
        smtp_config = load_smtp_config()
        if not smtp_config.enabled:
            flash("全站发信邮箱尚未配置，无法发送。", "warning")
            return _management_redirect(True, "journal-alerts")
        if _start_journal_job_async(
            "send_batch",
            lambda: send_journal_batch(base_url=base_url, smtp_config=smtp_config, force=True),
        ):
            flash("已开始发送文献综述邮件，请稍后在投递记录中查看发送结果。", "success")
        else:
            flash(busy_msg, "warning")
        return _management_redirect(True, "journal-alerts")

    if action == "send_only":
        # 兼容旧版：把已就绪文章逐篇发出（不重新抓取，不走综述）。
        smtp_config = load_smtp_config()
        if not smtp_config.enabled:
            flash("全站发信邮箱尚未配置，无法发送。", "warning")
            return _management_redirect(True, "journal-alerts")
        if _start_journal_job_async(
            "send_only",
            lambda: deliver_ready_journal_articles(base_url=base_url, smtp_config=smtp_config),
        ):
            flash("已开始发送期刊摘要邮件，请稍后查看投递记录。", "success")
        else:
            flash(busy_msg, "warning")
        return _management_redirect(True, "journal-alerts")

    # 兜底：采集并尝试发送（旧行为）。
    smtp_config = load_smtp_config()
    if _start_journal_job_async(
        "run_once",
        lambda: run_journal_alerts_once(
            ai_client=AI_CLIENT, base_url=base_url, smtp_config=smtp_config, send=True
        ),
    ):
        flash("已开始期刊提醒检测与发送，请稍后在运行记录中查看结果。", "success")
    else:
        flash(busy_msg, "warning")
    return _management_redirect(True, "journal-alerts")


@app.post("/admin/journal-alerts/settings")
def admin_journal_alert_settings():
    return _handle_journal_alert_settings_submit()


@app.post("/admin/journal-alerts/sources/backfill")
def admin_journal_backfill_sources():
    _require_admin()
    _require_management_csrf()
    changed = backfill_default_journal_sources()
    flash(f"默认期刊来源参数已补齐，更新 {changed} 条记录。", "success")
    return _management_redirect(True, "journal-alerts")


@app.post("/admin/journal-alerts/articles/approve-all")
def admin_journal_approve_all():
    _require_admin()
    _require_management_csrf()
    approved = approve_all_pending_journal_articles()
    flash(f"已批准 {approved} 篇待审文章，将在下次发送时进入摘要。", "success")
    return _management_redirect(True, "journal-alerts")


@app.post("/admin/journal-alerts/sources/<int:source_id>")
def admin_journal_source_update(source_id: int):
    _require_admin()
    _require_management_csrf()
    try:
        config = {
            "parser": (request.form.get("parser") or "").strip(),
            "gch": (request.form.get("gch") or "").strip(),
            "entry_url": (request.form.get("entry_url") or "").strip(),
            "search_issn": (request.form.get("search_issn") or "").strip(),
            "title_selector": (request.form.get("title_selector") or "").strip(),
            "date_selector": (request.form.get("date_selector") or "").strip(),
            "link_selector": (request.form.get("link_selector") or "").strip(),
            "fallback_urls": [
                line.strip()
                for line in (request.form.get("fallback_urls") or "").splitlines()
                if line.strip()
            ],
        }
        config = {key: value for key, value in config.items() if value != "" and value != []}
        # 复选框：勾选=可信来源，抓到即自动发送，无需人工审核。
        config["auto_publish"] = _form_bool("auto_publish")
        update_journal_source(
            source_id,
            source_type=(request.form.get("source_type") or "manual").strip(),
            source_url=(request.form.get("source_url") or "").strip(),
            issn=(request.form.get("issn") or "").strip(),
            is_enabled=_form_bool("is_enabled"),
            config=config,
        )
    except ValueError as exc:
        flash(str(exc), "warning")
    else:
        flash("期刊来源已保存。", "success")
    return _management_redirect(True, "journal-alerts")


@app.post("/admin/journal-alerts/articles/<int:article_id>")
def admin_journal_article_review(article_id: int):
    _require_admin()
    _require_management_csrf()
    action = (request.form.get("action") or "").strip().lower()
    status = {
        "approve": "ready",
        "ignore": "ignored",
        "reopen": "pending_review",
    }.get(action)
    if not status:
        flash("未知的文章审核操作。", "warning")
        return _management_redirect(True, "journal-alerts")
    try:
        article = update_journal_article_review_status(article_id, status)
    except ValueError as exc:
        flash(str(exc), "warning")
    else:
        flash(f"文章“{(article or {}).get('title') or article_id}”状态已更新。", "success")
    return _management_redirect(True, "journal-alerts")


@app.post("/admin/journal-alerts/sources")
def admin_journal_source_add():
    _require_admin()
    _require_management_csrf()
    config = {
        "parser": (request.form.get("parser") or "").strip(),
        "gch": (request.form.get("gch") or "").strip(),
        "entry_url": (request.form.get("entry_url") or "").strip(),
    }
    config = {key: value for key, value in config.items() if value}
    config["auto_publish"] = _form_bool("auto_publish")
    try:
        source = add_journal_source(
            name=(request.form.get("name") or "").strip(),
            language=(request.form.get("language") or "zh").strip(),
            source_type=(request.form.get("source_type") or "manual").strip(),
            issn=(request.form.get("issn") or "").strip(),
            source_url=(request.form.get("source_url") or "").strip(),
            config=config,
        )
    except ValueError as exc:
        flash(str(exc), "warning")
    else:
        flash(f"已新增期刊来源“{source.get('name')}”。", "success")
    return _management_redirect(True, "journal-alerts")


# 后台综述生成：build_literature_review 要按学科逐个调 DeepSeek，多学科批次的总时长远超
# Cloudflare 100s 请求上限 → 管理员点「重新生成」必超时报错。故改为丢后台线程执行、立即回执，
# 管理员稍后刷新审核（与研究综述「非流式 generate 丢后台线程」同思路）。
_JOURNAL_REVIEW_JOBS = set()
_JOURNAL_REVIEW_JOBS_LOCK = threading.Lock()


def _run_journal_review_async(digest_id: int) -> None:
    try:
        generate_batch_review(int(digest_id), ai_client=AI_CLIENT, auto_approve=False)
    except Exception as exc:
        LOGGER.warning("后台综述生成失败 digest=%s: %s", digest_id, exc)
        try:
            update_batch_review(int(digest_id), review_status="生成失败")
        except Exception:
            pass
    finally:
        with _JOURNAL_REVIEW_JOBS_LOCK:
            _JOURNAL_REVIEW_JOBS.discard(int(digest_id))


@app.post("/admin/journal-alerts/digest/<int:digest_id>/review")
def admin_journal_digest_review(digest_id: int):
    _require_admin()
    _require_management_csrf()
    action = (request.form.get("action") or "").strip().lower()
    if action == "regenerate":
        # 已在后台跑就别重复触发（防管理员连点/刷新重提叠加多份 AI 调用）。
        with _JOURNAL_REVIEW_JOBS_LOCK:
            already = int(digest_id) in _JOURNAL_REVIEW_JOBS
            if not already:
                _JOURNAL_REVIEW_JOBS.add(int(digest_id))
        if already:
            flash("该批次综述正在后台生成中，请稍候刷新本页查看。", "warning")
            return _management_redirect(True, "journal-alerts")
        try:
            update_batch_review(int(digest_id), review_status="生成中")
        except Exception:
            pass
        threading.Thread(
            target=_run_journal_review_async, args=(int(digest_id),),
            name=f"journal-review-{digest_id}", daemon=True,
        ).start()
        flash("综述已在后台开始生成（多学科 AI 逐一撰写，通常 1–3 分钟）。完成后本页「综述状态」会转为待审核，请稍后刷新审核。", "success")
        return _management_redirect(True, "journal-alerts")
    if action == "save":
        review_md = request.form.get("review_md") or ""
        update_batch_review(
            digest_id,
            review_md=review_md,
            review_html=journal_markdown_to_html(review_md),
            status="reviewing",
        )
        flash("综述草稿已保存。", "success")
        return _management_redirect(True, "journal-alerts")
    if action == "approve":
        review_md = request.form.get("review_md")
        if review_md is not None:
            update_batch_review(
                digest_id,
                review_md=review_md,
                review_html=journal_markdown_to_html(review_md),
            )
        update_batch_review(
            digest_id,
            review_status="approved",
            status="ready_to_send",
            mark_approved=True,
        )
        flash("综述已批准，可在预定时间发送或立即发送。", "success")
        return _management_redirect(True, "journal-alerts")
    if action == "reject":
        update_batch_review(digest_id, review_status="pending", status="reviewing")
        flash("已退回综述，待修改后重新批准。", "success")
        return _management_redirect(True, "journal-alerts")
    flash("未知的综述操作。", "warning")
    return _management_redirect(True, "journal-alerts")


@app.post("/admin/journal-alerts/digest/<int:digest_id>/send")
def admin_journal_digest_send(digest_id: int):
    _require_admin()
    _require_management_csrf()
    smtp_config = load_smtp_config()
    if not smtp_config.enabled:
        flash("全站发信邮箱尚未配置，无法发送。", "warning")
        return _management_redirect(True, "journal-alerts")
    mode = (request.form.get("recipient_mode") or "subscribers").strip().lower()
    plan_codes = request.form.getlist("recipient_plans")
    emails = [line.strip() for line in (request.form.get("recipient_emails") or "").splitlines() if line.strip()]
    if mode == "specific" and not emails:
        flash("请填写至少一个收件邮箱。", "warning")
        return _management_redirect(True, "journal-alerts")
    # 收件人解析是快查询，同步做以便即时校验（无人可发/参数错当场提示）；真正逐封阻塞 SMTP 的发送
    # 改为后台单飞，避免向「全部注册用户」逐封发信把请求线程钉死数分钟 / 触 CF 100s 超时。
    try:
        recipients, enforce_permission = resolve_journal_recipients(
            mode, plan_codes=plan_codes, emails=emails
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"收件人解析失败：{exc}", "warning")
        return _management_redirect(True, "journal-alerts")
    if not recipients:
        flash("所选受众没有可用收件人。", "warning")
        return _management_redirect(True, "journal-alerts")
    base_url = journal_alert_public_base_url(DEPLOYMENT)
    mode_label = {
        "subscribers": "邮箱订阅者", "members": "付费会员",
        "registered": "全部注册用户", "specific": "特定邮箱",
    }.get(mode, mode)
    started = _start_journal_job_async(
        "digest_send",
        lambda: send_journal_batch(
            base_url=base_url,
            smtp_config=smtp_config,
            digest_id=digest_id,
            force=True,
            recipients=recipients,
            enforce_permission=enforce_permission,
        ),
    )
    if started:
        flash(f"已开始向「{mode_label}」（约 {len(recipients)} 人）发送文献综述邮件，请稍后查看投递记录。", "success")
    else:
        flash("已有期刊后台任务进行中，请稍后再试。", "warning")
    return _management_redirect(True, "journal-alerts")


@app.post("/admin/journal-alerts/articles/archive-old")
def admin_journal_archive_old():
    _require_admin()
    _require_management_csrf()
    settings = load_journal_alert_settings()
    count = archive_previous_batches(hard_delete=bool(settings.get("hard_delete_archived")))
    flash(f"已归档 {count} 个旧批次及其未处理文章。", "success")
    return _management_redirect(True, "journal-alerts")


@app.post("/admin/journal-alerts/articles/purge-archived")
def admin_journal_purge_archived():
    _require_admin()
    _require_management_csrf()
    removed = purge_archived_articles()
    flash(f"已硬删除 {removed} 篇已归档文章。", "success")
    return _management_redirect(True, "journal-alerts")


_QR_MAX_IMAGE_BYTES = 400_000  # 单张收款码图片上限 ~400KB（base64 后存入设置）
_QR_IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
}


@app.post("/admin/payments/qr-settings")
def admin_payment_qr_settings():
    _require_admin()
    _require_management_csrf()
    import base64

    current = _payment_qr_settings()
    default_mode = (request.form.get("default_mode") or "redirect").strip().lower()
    if default_mode not in PAYMENT_QR_MODES:
        default_mode = "redirect"
    plans_out: dict[str, dict] = dict(current["plans"])
    warnings: list[str] = []
    for plan in list_plans(include_inactive=True):
        code = str(plan["code"])
        mode = (request.form.get(f"mode_{code}") or "").strip().lower()
        if mode not in PAYMENT_QR_MODES:
            mode = ""
        note = (request.form.get(f"note_{code}") or "").strip()
        existing = plans_out.get(code, {})
        image = str(existing.get("image") or "")
        if _form_bool(f"clear_{code}"):
            image = ""
        upload = request.files.get(f"image_{code}")
        if upload and upload.filename:
            ext = upload.filename.rsplit(".", 1)[-1].strip().lower() if "." in upload.filename else ""
            mime = _QR_IMAGE_MIME.get(ext)
            if not mime:
                warnings.append(f"{plan.get('name') or code}：不支持的图片格式（仅 png/jpg/gif/webp/svg）。")
            else:
                blob = upload.read(_QR_MAX_IMAGE_BYTES + 1)
                if len(blob) > _QR_MAX_IMAGE_BYTES:
                    warnings.append(f"{plan.get('name') or code}：图片过大（>400KB），未保存。")
                else:
                    b64 = base64.b64encode(blob).decode("ascii")
                    image = f"data:{mime};base64,{b64}"
        plans_out[code] = {"mode": mode, "image": image, "note": note}
    set_setting(
        "payment_qr_settings",
        {"default_mode": default_mode, "plans": plans_out},
        updated_by=_management_actor_label(True),
    )
    for warning in warnings:
        flash(warning, "warning")
    flash("支付二维码设置已保存。", "success")
    return _management_redirect(True, "payments")


@app.post("/admin/payments/test-qr")
def admin_payment_test_qr():
    # 出码自测：用指定金额向 ZPay 试下单，判断该金额能否生成二维码（找可用金额阈值）。
    # 不改公开价格、不写入本站订单表，仅在 ZPay 侧产生一个会自动过期的临时订单。
    _require_admin()
    _require_management_csrf()
    if not PAYMENT_CONFIG.enabled:
        flash("ZPay 未配置，无法测试出码。", "warning")
        return _management_redirect(True, "payments")
    try:
        yuan = float((request.form.get("amount") or "0").strip())
    except ValueError:
        yuan = 0.0
    cents = int(round(yuan * 100))
    if cents <= 0:
        flash("请输入有效的测试金额（元）。", "warning")
        return _management_redirect(True, "payments")
    order_no = f"TEST{int(time.time())}{secrets.token_hex(3)}"[:32]
    result = PAYMENT_CLIENT.create_mapi_order(
        order_no=order_no,
        subject=f"{PAYMENT_CONFIG.subject_prefix} - 出码测试",
        amount_cents=cents,
        client_ip=_client_ip(),
        param="test",
    )
    if result.get("ok") and (result.get("qrcode") or result.get("payurl")):
        flash(f"✅ 金额 ¥{yuan:.2f} 出码成功（code={result.get('code')}）：该金额可走 api/redirect 自动开通。建议把月度价格设为它。", "success")
    else:
        flash(f"❌ 金额 ¥{yuan:.2f} 出码失败：{result.get('msg') or '网关未返回支付链接'}（code={result.get('code')}）。请换个金额再试。", "warning")
    return _management_redirect(True, "payments")


@app.post("/admin/payments/clear-pending")
def admin_payment_clear_pending():
    # 一键清理卡住的待支付旧订单（置为 expired，不删除）。留空邮箱=清理全部；填邮箱=只清该用户。
    _require_admin()
    _require_management_csrf()
    email = normalize_email(request.form.get("email") or "")
    if email:
        user = get_user_by_email(email)
        if not user:
            flash(f"未找到邮箱为 {email} 的用户。", "warning")
            return _management_redirect(True, "payments")
        count = clear_pending_orders(user_id=int(user["id"]))
        flash(f"已清理用户 {email} 的 {count} 笔待支付订单（置为已过期）。该用户下次下单会按当前价新建。", "success")
    else:
        count = clear_pending_orders()
        flash(f"已清理全部 {count} 笔待支付订单（置为已过期）。所有用户下次下单都会按当前价新建。", "success")
    return _management_redirect(True, "payments")


@app.post("/admin/desktop/devices")
def admin_desktop_device_create():
    return _handle_desktop_device_create()


@app.post("/admin/desktop/devices/<int:device_id>")
def admin_desktop_device_update(device_id: int):
    return _handle_desktop_device_update(device_id)


@app.post("/admin/desktop/releases")
def admin_desktop_release_create():
    return _handle_release_create()


@app.post("/admin/desktop/releases/upload")
def admin_desktop_release_upload():
    return _handle_release_upload()


@app.post("/admin/desktop/releases/<int:release_id>")
def admin_desktop_release_update(release_id: int):
    return _handle_release_update(release_id)


@app.route("/payments/result")
def payment_result():
    order_no = (request.args.get("order_no") or "").strip()
    if not order_no:
        abort(400, description="缺少订单号。")
    order = get_order_by_no(order_no)
    if order is None:
        _require_login_page()
        abort(404, description="未找到对应订单。")
    is_donation = str(order.get("plan_code") or "") == "donation"
    # 会员单必须登录本人查看；打赏单登录本人或会话持有者（含访客）均可查看结果。
    if not _can_view_order(order):
        if not is_donation:
            _require_login_page()
        abort(404, description="未找到对应订单。")
    user = getattr(g, "current_user", None)
    membership_snapshot = _membership_to_dict(get_membership_snapshot(int(user["id"]))) if user else None
    return render_template(
        "payment_result.html",
        title="打赏结果" if is_donation else "支付结果",
        order=order,
        is_donation=is_donation,
        membership_snapshot=membership_snapshot,
    )


@app.route("/payments/zpay/return", methods=["GET", "POST"])
@app.route("/payments/alipay/return", methods=["GET", "POST"])
def zpay_return():
    form_params = request.form.to_dict(flat=True)
    params = request.args.to_dict(flat=True)
    params.update(form_params)
    if not params:
        flash("未收到支付返回参数。", "warning")
        return redirect(url_for("account"))

    verified = PAYMENT_CONFIG.enabled and PAYMENT_CLIENT.verify_callback_params(params)
    order_no = str(params.get("out_trade_no") or "").strip()
    trade_no = str(params.get("trade_no") or "").strip()
    trade_status = str(params.get("trade_status") or "").strip()

    if not verified:
        LOGGER.warning("ZPay return verify failed for order=%s", order_no or "<missing>")
        flash("支付状态暂时无法确认，请稍后刷新会员中心，或联系客服核对。", "warning")
        return redirect(url_for("payment_result", order_no=order_no)) if order_no else redirect(url_for("account"))

    order = get_order_by_no(order_no) if order_no else None
    # 验签通过且订单存在后再落库支付事件：验签前绝不写库，杜绝匿名伪造 out_trade_no 向 payment_events 无限写行。
    if order is not None:
        record_payment_event(
            order_no=order_no,
            provider="zpay",
            event_type="return",
            payload=params,
        )

    if order and trade_status == "TRADE_SUCCESS" and str(order.get("status") or "") == "paid":
        flash("支付成功，会员状态已更新。", "success")
    elif order and trade_status == "TRADE_SUCCESS":
        flash("支付平台已返回成功状态，会员状态正在确认中，请稍后刷新。", "info")
    else:
        flash("已返回站点，会员状态正在确认中。", "info")
    return redirect(url_for("payment_result", order_no=order_no)) if order_no else redirect(url_for("account"))


@app.route("/payments/zpay/notify", methods=["GET", "POST"])
@app.route("/payments/alipay/notify", methods=["GET", "POST"])
def zpay_notify():
    params = request.args.to_dict(flat=True)
    params.update(request.form.to_dict(flat=True))
    order_no = str(params.get("out_trade_no") or "").strip()

    if not PAYMENT_CONFIG.enabled:
        return "failure"
    if not params or not PAYMENT_CLIENT.verify_callback_params(params):
        LOGGER.warning("ZPay notify verify failed for order=%s", order_no or "<missing>")
        return "failure"

    order = get_order_by_no(order_no)
    if order is None:
        LOGGER.warning("ZPay notify order not found: %s", order_no)
        return "failure"

    # 验签通过且订单存在后再落库支付事件：验签前绝不写库，防匿名伪造 out_trade_no 刷 payment_events / 争 WAL 写锁。
    record_payment_event(
        order_no=order_no,
        provider="zpay",
        event_type="notify",
        payload=params,
    )

    total_amount = str(params.get("money") or "").strip()
    pid = str(params.get("pid") or "").strip()
    trade_no = str(params.get("trade_no") or "").strip()
    trade_status = str(params.get("trade_status") or "").strip()

    if pid and pid != PAYMENT_CONFIG.pid:
        LOGGER.warning("ZPay notify pid mismatch for order=%s", order_no)
        return "failure"
    param_user_id = _payment_param_user_id(params)
    if param_user_id is None or param_user_id != int(order["user_id"]):
        LOGGER.warning("ZPay notify param mismatch for order=%s", order_no)
        return "failure"
    if not _money_matches(int(order["amount_cents"]), total_amount):
        LOGGER.warning("ZPay notify amount mismatch for order=%s", order_no)
        return "failure"
    if str(order["status"]) not in {"pending", "paid"}:
        LOGGER.warning("ZPay notify invalid order status for order=%s status=%s", order_no, order["status"])
        return "failure"

    if trade_status == "TRADE_SUCCESS":
        try:
            mark_order_paid(
                order_no=order_no,
                provider="zpay",
                payment_reference=trade_no,
                notes=f"notify:{trade_status}",
                source="zpay_notify",
            )
        except ValueError as exc:
            LOGGER.warning("ZPay notify failed to mark paid for order=%s: %s", order_no, exc)
            return "failure"
    return "success"


# --- 注册用户省际分布（公告栏下方卡片的数据源）---------------------------------
# 离线 ip2region 把每个注册用户的代表 IP 归类成省份/国家，只对外暴露聚合后的省级计数与
# 海外国家计数；单个用户 IP 绝不出现在任何响应里。聚合结果按 TTL 进程级缓存，避免每请求全表扫描。
REGISTRY_GEO_SETTING_KEY = "index_registry_geo_enabled"
_REGISTRY_GEO_TTL_SECONDS = 60.0
_registry_geo_cache: dict = {"at": 0.0, "payload": None}
_registry_geo_lock = threading.Lock()


REGISTRY_GEO_COUNT_KEY = "index_registry_geo_count"


def _registry_geo_enabled() -> bool:
    """公告栏下方「注册用户省际分布」总开关（默认开；后台 set_setting 可关，关后卡片与接口都隐藏）。"""
    raw = get_setting(REGISTRY_GEO_SETTING_KEY, "1")
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


def _registry_geo_count_config() -> dict:
    """「注册总数」显示方式：exact=实时精确数字 / fuzzy=以百为整（800+）/ custom=管理员自定义文本。"""
    raw = get_setting(REGISTRY_GEO_COUNT_KEY, {})
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw.get("mode") or "exact").strip().lower()
    if mode not in {"exact", "fuzzy", "custom"}:
        mode = "exact"
    return {"mode": mode, "custom": str(raw.get("custom") or "").strip()[:40]}


def _registry_geo_editor_settings() -> dict:
    """控制台「注册用户分布」编辑器的当前值（开关 + 计数模式 + 自定义文本）。"""
    cfg = _registry_geo_count_config()
    return {"enabled": _registry_geo_enabled(), "mode": cfg["mode"], "custom": cfg["custom"]}


AI_ASSISTANT_MODE_KEY = "index_ai_assistant_mode"


def _ai_assistant_mode() -> str:
    """「AI 随心问」展示形态：card=首页「留言反馈」下方固定卡片（其它功能页仍用右侧抽屉）/
    drawer=全站右侧收纳抽屉。默认 card；后台「内容运营」可切。"""
    raw = str(get_setting(AI_ASSISTANT_MODE_KEY, "card") or "card").strip().lower()
    return "drawer" if raw == "drawer" else "card"


SPONSOR_ENABLED_KEY = "index_sponsor_enabled"


def _sponsor_button_enabled() -> bool:
    """首页顶栏「赞助」按钮 + 友情打赏弹层的总开关（默认关；网站 /admin「内容运营」可开）。
    关闭时首页不露出任何赞助入口，但后端 /donate 打赏收款管线仍在，随时可再打开。"""
    raw = get_setting(SPONSOR_ENABLED_KEY, "0")
    return str(raw).strip().lower() in {"1", "true", "on", "yes"}


def _build_registry_geo_payload() -> dict:
    total = count_registered_users()
    prov_counts: dict[str, int] = {}
    overseas: dict[str, dict] = {}
    located = 0
    domestic_unknown = 0
    ip_user_count = 0
    for ip, count in get_user_ip_counts():
        ip_user_count += count
        result = geoip.classify_ip(ip)
        scope = result.get("scope")
        if scope == "domestic":
            prov = result.get("province")
            if prov:
                prov_counts[prov] = prov_counts.get(prov, 0) + count
                located += count
            else:
                domestic_unknown += count
        elif scope == "overseas":
            code = result.get("country_code") or "??"
            slot = overseas.setdefault(code, {"name": result.get("country") or code, "count": 0})
            slot["count"] += count
            located += count
        else:
            domestic_unknown += count
    no_ip = max(0, total - ip_user_count)
    provinces = sorted(
        (
            {"name": name, "short": geoip.PROVINCE_SHORT.get(name, name), "count": value}
            for name, value in prov_counts.items()
        ),
        key=lambda item: (-item["count"], item["name"]),
    )
    overseas_list = sorted(
        ({"name": slot["name"], "count": slot["count"]} for slot in overseas.values()),
        key=lambda item: (-item["count"], item["name"]),
    )
    return {
        "ok": True,
        "total_registered": total,
        "domestic_total": sum(prov_counts.values()),
        "province_max": provinces[0]["count"] if provinces else 0,
        "provinces": provinces,
        "overseas_total": sum(slot["count"] for slot in overseas.values()),
        "overseas": overseas_list,
        "unknown": no_ip + domestic_unknown,
        "geoip_ready": geoip.geoip_ready(),
    }


def _get_registry_geo_payload() -> dict:
    now = time.time()
    cached = _registry_geo_cache.get("payload")
    if cached is not None and now - _registry_geo_cache.get("at", 0.0) < _REGISTRY_GEO_TTL_SECONDS:
        return cached
    payload = _build_registry_geo_payload()  # 在锁外构建，避免长时间持锁；并发首启重复构建无害
    with _registry_geo_lock:
        _registry_geo_cache["payload"] = payload
        _registry_geo_cache["at"] = time.time()
    return payload


@app.get("/api/community/registry-geo")
def api_community_registry_geo():
    """公开：注册用户总数 + 省际分布 + 海外国家计数（仅聚合计数，无任何明细 IP）。"""
    if not _registry_geo_enabled():
        return jsonify({"ok": False, "disabled": True}), 200
    payload = dict(_get_registry_geo_payload())
    # 注册总数显示方式（在 api 层套用，配置改动当场生效，不受 60s 分布缓存影响）。
    cfg = _registry_geo_count_config()
    mode = cfg["mode"]
    total = int(payload.get("total_registered") or 0)
    if mode == "fuzzy":
        rounded = (total // 100) * 100
        payload["total_display"] = f"{rounded}+" if rounded >= 100 else str(total)
        payload["total_registered"] = rounded  # 模糊模式不外泄精确总数
    elif mode == "custom" and cfg["custom"]:
        payload["total_display"] = cfg["custom"]
    else:
        mode = "exact"
        payload["total_display"] = ""
    payload["count_mode"] = mode
    payload["generated_at"] = _display_datetime(utc_now_text())
    return jsonify(payload)


def _render_index_page(layout_page: str | None = None):
    """旧首页与「四页面布局」共用同一份上下文与模板。

    layout_page=None  → 旧首页：layout_v2 未开，模板内三个 show_* 开关全为真，
                        渲染结果与本次改造前逐字节一致（新旧并存、可随时回退）。
    'search'/'read'/'more' → 新布局，模板按 layout_page 分区渲染 + 套统一导航外壳。
    """
    state = current_view_state()
    current_user = getattr(g, "current_user", None)
    # 四页面布局·阅读页：书目网格数据（点书名进 AI 导读阅读器）。仅在阅读页且有阅读权时计算，
    # 其它页/旧首页为空、零开销。viewer_url 走默认（非 basic）即 AI 导读阅读器；AI 对话本身另需登录。
    read_book_groups: list[dict] = []
    read_foreign_books: list[dict] = []
    read_books_nav: list[dict] = []  # 供「分卷/分目录」抽屉用：每本书 → 卷列表（卷内目录懒加载）
    # 书目列表对所有人可见（含游客）：只取决于资料是否就绪，不看登录/会员。列出书名≠授予阅读权——
    # 点书进阅读器后，能否读 PDF 仍由阅读器路由按站点「访客权限」策略裁决；AI 导学在阅读器内另按登录门控。
    if layout_page == "read" and _feature_is_available("library"):
        read_book_groups = _library_volume_groups(_library_volumes())
        _ai_ok = bool(_feature_is_available("ai") and _feature_effective_for_user("ai"))
        # 与中文 PDF 书目一致：对所有身份陈列外文原著书名（require_access=False），真正阅读权
        # 仍在 wenku_reader 入口按 static_library 门控。否则非会员进阅读页时外文原著整组消失。
        read_foreign_books = _foreign_library_books(ai_enabled=_ai_ok, origin="read", require_access=False)
        for _grp in read_book_groups:
            for _bk in _grp["books"]:
                read_books_nav.append({
                    "key": _bk["key"],
                    "title": _bk["title"],
                    "label": _grp.get("label") or "",
                    "kind": "pdf",
                    "volumes": [
                        {
                            "v": _v["volume"],
                            "title": _v.get("display_title") or "",
                            "heading": _v.get("volume_heading") or "",
                            "subtitle": _v.get("volume_subtitle") or "",
                            "file": _v["source_file"],
                            "pages": _v.get("page_count") or 0,
                            "toc": _v.get("toc_count") or 0,
                            "span": _v.get("date_span") or "",     # 收录文献时间跨度
                            "unit": _v.get("volume_unit") or "卷",  # 分卷单位（卷/册）
                            "url": _v["viewer_url"],
                        }
                        for _v in _bk["volumes"]
                    ],
                })
        for _fb in read_foreign_books:
            read_books_nav.append({
                "key": "foreign:" + str(_fb.get("title") or ""),
                "title": str(_fb.get("title") or ""),
                "label": "外文原著",
                "kind": "foreign",
                "volumes": [
                    {"v": str(_fv.get("label") or ""), "title": str(_fv.get("label") or ""), "url": _fv.get("url") or "#"}
                    for _fv in (_fb.get("volumes") or [])
                ],
            })
    feedback_thread = (
        get_feedback_user_thread(int(current_user["id"]), mark_seen=False)
        if current_user
        else None
    )
    _bj_today = _beijing_now()
    return render_template(
        "index.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        notice_date_cn=f"{_bj_today.year}年{_bj_today.month}月{_bj_today.day}日",
        request_token=REQUEST_TOKEN if state["management_api_enabled"] else None,
        state=state,
        ai_runtime=_public_ai_runtime_payload(allow_details=bool(state["feature_access"].get("ai") or state["feature_access"].get("search_chat"))),
        n_wenji=n_wenji,
        n_quanji=n_quanji,
        book_stats=book_stats,
        plans=list_active_plans(),
        feature_cards=_index_feature_cards(),
        feature_tags=_get_feature_tags(),
        chapter_search=_chapter_search_access(),
        chapter_books=_chapter_scope_books(),  # 篇章直达「切换书籍」下拉
        member_access_enabled=bool(_feature_is_available("library") and _feature_effective_for_user("library")),
        wenku_available=bool(_feature_is_available("static_library")),
        wenku_access_enabled=bool(_feature_is_available("static_library") and _feature_effective_for_user("static_library")),
        liushi_available=bool(_feature_is_available("stream_reading")),
        liushi_access_enabled=bool(_feature_is_available("stream_reading") and _feature_effective_for_user("stream_reading")),
        ai_access_enabled=bool(_feature_is_available("ai") and _feature_effective_for_user("ai")),
        search_chat_access_enabled=bool(_feature_is_available("search_chat") and _feature_effective_for_user("search_chat")),
        assoc_access_enabled=bool(_feature_is_available("associative") and _feature_effective_for_user("associative")),
        research_access_enabled=bool(_feature_is_available("research") and _feature_effective_for_user("research")),
        search_scopes=_scope_options_payload(),  # 联想/研究检索「检索范围」下拉
        book_scope_tree=_book_scope_tree(),  # 「精选到书/卷」多选控件数据（按著作群分组+卷号）
        ai_web_access_enabled=_ai_web_access_enabled(),
        notes_access_enabled=_notes_access_enabled(),  # 会员专属「笔记/知识库」：/v2/more 知识库入口 + 阅读页据此显隐
        feedback_thread=feedback_thread,
        registry_geo_enabled=_registry_geo_enabled(),
        ai_assistant_mode=_ai_assistant_mode(),
        sponsor_enabled=_sponsor_button_enabled(),
        alipay_runtime=PAYMENT_CONFIG.to_public_dict(),  # 首页「赞助」弹层据此决定是否出打赏表单
        layout_v2=layout_page is not None,
        layout_page=layout_page or "search",
        read_book_groups=read_book_groups,
        read_foreign_books=read_foreign_books,
        read_books_nav=read_books_nav,
    )


# ---- 四页面布局：检索 / 阅读 / AI研究对话 / 更多功能 ----------------------------
# 各自服务端渲染、各自权限门控、地址可分享、当前标签高亮。外壳见 templates/_appnav.html。
#
# 「/」必须是新检索页：站内十余个模板的「返回首页」都是写死的 href="/"（阅读器、大辞典、
# 书库、流式、文库、期刊、账户、套餐…），若 / 仍是旧首页，从任何模块返回都会掉回旧版。
# 旧首页保留在 /legacy，仅供对照与回退。
@app.route("/")
def index():
    return _render_index_page("search")


@app.route("/legacy")
def index_legacy():
    """改造前的旧首页，原样保留，供对照与随时回退。"""
    return _render_index_page(None)


# 别名：先前分享出去的 /v2 预览地址继续可用。
@app.route("/v2")
def layout_search():
    return _render_index_page("search")


@app.route("/v2/read")
def layout_read():
    return _render_index_page("read")


@app.route("/v2/more")
def layout_more():
    return _render_index_page("more")


def _render_ai_page(layout_v2: bool = False):
    """研究导向 AI 对话页：把「AI 随心问」（快速问答）与「研究型检索」（研究综述）合并为
    一条可连续追问、带上下文的会话线程。页面自包含（命名空间 aip*，与全站抽屉零冲突），
    权限/额度由 /api/ai/assistant-config 惰性拉取后自适应显隐「快速 / 研究」两档深度。
    与右侧抽屉共用同一条 localStorage 会话线程（marx-ai-thread-v1），跨页/跨标签连贯。"""
    return render_template(
        "ai.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        search_scopes=_scope_options_payload(),  # 「检索范围」chips：自动/全部 + 各著作群
        search_chat_access_enabled=bool(_feature_is_available("search_chat") and _feature_effective_for_user("search_chat")),
        research_access_enabled=bool(_feature_is_available("research") and _feature_effective_for_user("research")),
        ai_web_access_enabled=_ai_web_access_enabled(),
        layout_v2=layout_v2,
        layout_page="ai",
    )


@app.route("/ai")
def ai_page():
    return _render_ai_page(False)


@app.route("/v2/ai")
def layout_ai():
    """四页面布局下的 AI 研究对话页：内容与 /ai 完全相同，只多套一层统一导航外壳。"""
    return _render_ai_page(True)


def _library_volumes(*, basic_reader_mode: bool = False) -> list[dict]:
    volumes = []
    for book_cfg in BOOK_CONFIGS:
        # available=False 的书库不进书目页/阅读页的书目陈列。books.yaml 对该字段的定义就是
        # 「不在面向用户的入口中露出（仍可被索引/调试）」，而书目页正是最主要的用户入口；
        # 此前只有「篇章直达」和「检索范围」两处做了过滤，导致正在建库、目录/正文还不齐的
        # 书库提前露在书目页上（2026-07-30 站长发现《周恩来年谱》未上线却已显示）。
        # 语料仍照常建、检索仍可调试，只是不对读者陈列，翻 available: true 即公开。
        if not book_cfg.available:
            continue
        book = book_cfg.key
        for volume in (corpus.get_volumes(book) if corpus else []):
            # 仅取目录“条数”用于卷头标签；目录条目本身改由 /api/library/volume-toc 在展开该卷时
            # 按需拉取（见 library.html）。避免每次进阅读器就把上万条目录全量渲染进 DOM 致卡顿，
            # 也省去每请求物化两万条 dict 的服务端开销。
            toc_count = len(corpus.get_toc_entries(volume.source_file)) if corpus else 0
            viewer_args = {"file": volume.source_file, "page": 1}
            if basic_reader_mode:
                viewer_args["mode"] = "reader"
            # 目录中的年份可能只是人物生卒年、注释或被提及事件的年份，不能据此推算整卷
            # 时限。只保留两套已经人工核准过目录日期的《重要文献选编》走自动计算；其余
            # 书的卷题/年代由独立展示层给出，完全不改 book/volume/source_file 等阅读标识。
            date_span = (
                corpus.volume_date_span(book, volume.volume)
                if corpus and book in TRUSTED_TOC_DATE_BOOKS
                else ""
            )
            presentation = volume_presentation(
                book,
                volume.volume,
                volume.display_title,
                unit=book_cfg.volume_unit,
                single_volume=book_cfg.single_volume,
                trusted_date_span=date_span,
            )
            volumes.append(
                {
                    "book": book,
                    "volume": volume.volume,
                    "display_title": volume.display_title,
                    **_book_payload(book),
                    "source_file": volume.source_file,
                    "page_count": len(volume.pages),
                    "toc_count": toc_count,
                    "volume_heading": presentation.heading,
                    "volume_subtitle": presentation.subtitle,
                    "date_span": date_span,
                    # 分卷单位（「卷」/「册」）：两套《重要文献选编》原书标「第十七册」。
                    "volume_unit": book_cfg.volume_unit,
                    "viewer_url": url_for("pdf_viewer", **viewer_args),
                    "pdf_url": url_for("serve_pdf", file=volume.source_file),
                }
            )
    volumes.sort(key=lambda item: (item["book_sort_order"], item["volume"], item["source_file"]))
    return volumes


def _library_volume_groups(volumes: list[dict]) -> list[dict]:
    """把扁平卷册整理为阅读器展示组。

    普通书库仍是「一本书 = 一个折叠组」；带 collection 的书库先合并成专题，
    再在专题内按独立著作展开。数据层仍保持每本书独立，不影响检索与引文。
    """
    groups: dict[str, dict] = {}
    for volume in volumes:
        collection = str(volume.get("collection") or "")
        group_id = f"collection:{collection}" if collection else f"book:{volume['book']}"
        group = groups.setdefault(
            group_id,
            {
                "id": group_id,
                "label": volume.get("collection_label") or volume.get("book_title"),
                "is_collection": bool(collection),
                "sort_order": int(volume.get("book_sort_order") or 9999),
                "books": {},
            },
        )
        group["sort_order"] = min(group["sort_order"], int(volume.get("book_sort_order") or 9999))
        book = group["books"].setdefault(
            volume["book"],
            {"key": volume["book"], "title": volume["book_title"], "sort_order": volume["book_sort_order"], "volumes": []},
        )
        book["volumes"].append(volume)

    result: list[dict] = []
    for group in groups.values():
        books = sorted(group["books"].values(), key=lambda b: (b["sort_order"], b["key"]))
        group["books"] = books
        group["book_count"] = len(books)
        group["volume_count"] = sum(len(b["volumes"]) for b in books)
        result.append(group)
    return sorted(result, key=lambda g: (g["sort_order"], g["id"]))


def _library_display_sections(volumes: list[dict]) -> tuple[list[dict], list[dict]]:
    """把专题从普通书目中拆出，按书目顺序渲染为平铺式独立栏目。"""
    regular_groups: list[dict] = []
    library_sections: list[dict] = []
    for group in _library_volume_groups(volumes):
        collection = group["id"].removeprefix("collection:") if group["is_collection"] else ""
        if collection in _COLLECTION_LABELS:
            library_sections.append({
                **group,
                "description": _COLLECTION_DESCRIPTIONS.get(collection, ""),
            })
        else:
            regular_groups.append(group)
    return regular_groups, library_sections


def _foreign_library_books(*, ai_enabled: bool, origin: str, require_access: bool = True) -> list[dict]:
    """「外文原著」分组：把自托管的原文文库（列宁俄/MEGA/MEW/英，static_books.yaml）并入著作目录，
    与中文 PDF 著作分开陈列。每卷链接到现有网页版阅读器 wenku_reader。
    require_access=True（默认，/reader、/library 入口）：仅对「确有内容 + 当前用户有 static_library
    可读权」者显示（管理员/桌面放行）。require_access=False（四页面布局·阅读页）：与中文 PDF 书目一致，
    只要内容就绪即对所有身份陈列书名——列出书名≠授予阅读权，点书进 wenku_reader 后仍按 static_library
    门控（见 static_library_web.register_static_library 的 require_content_feature）。
    ai_enabled=False（全文阅读器/普通入口）时给链接带 ?ai=0，进 wenku 阅读器后隐藏 AI 导读；
    ai_enabled=True（AI 导学阅读器/会员入口且有 ai 权）时正常显示 AI 导读。"""
    if not _feature_is_available("static_library"):
        return []
    if require_access and not _current_user_allows_all(["static_library"]):
        return []
    out: list[dict] = []
    for book in load_static_library_books():
        vols: list[dict] = []
        for v in (book.get("volumes") or []):
            try:
                n = int(v.get("n"))
            except (TypeError, ValueError):
                continue
            args = {"book_key": book["key"], "vol": n, "from": origin}
            if not ai_enabled:
                args["ai"] = 0
            vols.append({"label": str(v.get("label") or f"第 {n} 卷"), "url": url_for("wenku_reader", **args)})
        if vols:
            out.append({
                "title": str(book.get("title_zh") or book.get("key")),
                "badge": str(book.get("badge") or ""),
                "desc": str(book.get("desc") or ""),
                "volumes": vols,
            })
    return out


@app.route("/reader")
def reader():
    _require_content_feature("library")
    _require_search()
    volumes = _library_volumes(basic_reader_mode=True)
    volume_groups, library_sections = _library_display_sections(volumes)
    return render_template(
        "library.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        state=current_view_state(),
        volumes=volumes,
        volume_groups=volume_groups,
        library_sections=library_sections,
        reader_mode=True,
        ai_access_enabled=bool(_feature_is_available("ai") and _feature_effective_for_user("ai")),
        foreign_books=_foreign_library_books(ai_enabled=False, origin="reader"),
        chapter_books=_chapter_scope_books(),  # 篇章直达「切换书籍」下拉
    )


@app.route("/library")
def library():
    _require_content_feature("library")
    _require_search()
    ai_access = bool(_feature_is_available("ai") and _feature_effective_for_user("ai"))
    volumes = _library_volumes()
    volume_groups, library_sections = _library_display_sections(volumes)
    return render_template(
        "library.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        state=current_view_state(),
        volumes=volumes,
        volume_groups=volume_groups,
        library_sections=library_sections,
        reader_mode=False,
        ai_access_enabled=ai_access,
        foreign_books=_foreign_library_books(ai_enabled=ai_access, origin="library"),
        chapter_books=_chapter_scope_books(),  # 篇章直达「切换书籍」下拉
    )


@app.route("/dictionary")
def dictionary():
    _require_content_feature("dictionary")
    stats = dictionary_stats()
    return render_template(
        "dictionary.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        state=current_view_state(),
        groups=dictionary_groups(),
        stats=stats,
    )


@app.route("/dictionary/entry/<path:slug>")
def dictionary_entry_page(slug: str):
    _require_content_feature("dictionary")
    entry = dictionary_entry(slug)
    if entry is None:
        abort(404, description="未找到对应的大辞典词条。")
    return render_template(
        "dictionary_entry.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        state=current_view_state(),
        entry=entry,
    )


@app.route("/api/dictionary/suggest")
def api_dictionary_suggest():
    _require_content_feature("dictionary")
    raw = (request.args.get("q") or "").strip()
    results = []
    for item in dictionary_suggest(raw):
        payload = dict(item)
        payload["url"] = url_for("dictionary_entry_page", slug=item["slug"])
        results.append(payload)
    resp = jsonify({"ok": True, "results": results})
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


# ---- 篇章名称自动补全（搜索全部书库目录） ----
_TOC_SUGGEST_INDEX: list[dict] | None = None
_TOC_SUGGEST_LOCK = threading.Lock()
_TOC_SUGGEST_PUNCT_RE = re.compile(r"""[\s·.,，。、；;：:！!？?（）()《》<>\[\]【】"'“”‘’\-—_]+""")


def _toc_norm(text: str) -> str:
    return _TOC_SUGGEST_PUNCT_RE.sub("", str(text or "")).lower()


_HIT_HIGHLIGHT_RE = re.compile(r"\[\[H\]\](.*?)\[\[/H\]\]", re.S)
_TOC_AUX_TITLE_MARKERS = (
    "\u63d2\u56fe",  # 插图
    "\u6249\u9875",  # 扉页
    "\u539f\u9875",  # 原页
    "\u5c01\u9762",  # 封面
    "\u63d0\u7eb2",  # 提纲
    "\u8349\u7a3f",  # 草稿
    "\u6458\u5f55",  # 摘录
)


def _hit_highlight_text(hit: dict, fallback: str) -> str:
    context = str(hit.get("context") or "")
    match = _HIT_HIGHLIGHT_RE.search(context)
    if match:
        text = " ".join(match.group(1).split())
        if text:
            return text[:160]
    return fallback


def _toc_match_rank(item: dict, qn: str) -> tuple[int, int, int]:
    norm = str(item.get("norm") or "")
    pos = norm.find(qn)
    if norm == qn:
        match_rank = 0
    elif pos == 0:
        match_rank = 1
    else:
        match_rank = 2
    aux_rank = 1 if any(marker in str(item.get("title") or "") for marker in _TOC_AUX_TITLE_MARKERS) else 0
    level = int(item.get("level") or 1)
    return match_rank, aux_rank, level


def _build_toc_suggest_index() -> list[dict]:
    index: list[dict] = []
    if corpus is None:
        return index
    # 仅收录「对用户开放」的书库（books.yaml 中 available: true）。
    # 尚未上线的书库（如列宁《全集》）即使语料里已有目录，也不在「篇章直达」露出。
    for book in (cfg.key for cfg in BOOK_CONFIGS if getattr(cfg, "available", True)):
        for volume in corpus.get_volumes(book):
            for entry in corpus.get_toc_entries(volume.source_file):
                title = str(getattr(entry, "title", "") or "").strip()
                if len(title) < 2:
                    continue
                index.append(
                    {
                        "title": title,
                        "norm": _toc_norm(title),
                        "book": book,
                        "volume": int(volume.volume),
                        **_book_payload(book),
                        "source_file": volume.source_file,
                        "pdf_page": int(getattr(entry, "pdf_page", 1) or 1),
                        "printed_page": str(getattr(entry, "printed_page", "") or ""),
                        "level": int(getattr(entry, "level", 1) or 1),
                        "kind": str(getattr(entry, "kind", "") or ""),
                        "sort_order": int(getattr(entry, "sort_order", 0) or 0),
                    }
                )
    return index


def _get_toc_suggest_index() -> list[dict]:
    global _TOC_SUGGEST_INDEX
    if _TOC_SUGGEST_INDEX is None:
        with _TOC_SUGGEST_LOCK:
            if _TOC_SUGGEST_INDEX is None:
                _TOC_SUGGEST_INDEX = _build_toc_suggest_index()
    return _TOC_SUGGEST_INDEX


# ---- 书名 / 卷次「直达」识别 ----
# 篇章补全只按目录篇名匹配；本节额外让用户「直接输入书名」（可带卷次/版次）就跳到整卷，
# 例如「《文集》第5卷」「马恩全集第1卷第二版」「毛泽东文集」「法治思想纲要」。识别是模糊的：
# 书名走多别名（含「马克思恩格斯→马恩」「习近平」前缀省略）子串匹配，卷次/版次支持中文与阿拉伯数字。
_BOOK_ALIAS_INDEX: list[dict] | None = None
_BOOK_ALIAS_LOCK = threading.Lock()
_ZH_DIGITS = {
    "零": 0, "〇": 0, "○": 0, "一": 1, "二": 2, "两": 2,
    "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_FULLWIDTH_DIGIT_MAP = {ord("０") + i: ord("0") + i for i in range(10)}
# 卷次：可选「第」+ 数字（中/阿/全角）+「卷」；版次：可选「第」+ 一/二/1/2 +「版」。
_VOL_RE = re.compile(r"第?\s*([0-9０-９一二两三四五六七八九十百零〇○]+)\s*卷")
_EDITION_RE = re.compile(r"第?\s*([一二两12１２])\s*版")


def _cn_to_int(text: str) -> int | None:
    """把卷/版号从中文或阿拉伯（含全角）数字转成整数，覆盖 1–99（列宁《全集》最多 60 卷）。"""
    s = str(text or "").translate(_FULLWIDTH_DIGIT_MAP).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:  # 十：处理 十/十一/二十/二十九/六十 等
        head, _, tail = s.partition("十")
        tens = _ZH_DIGITS.get(head, 1) if head else 1
        units = _ZH_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + units
    value = 0
    for ch in s:
        if ch not in _ZH_DIGITS:
            return None
        value = value * 10 + _ZH_DIGITS[ch]
    return value or None


def _book_aliases(cfg: BookConfig) -> list[str]:
    """一个书库的可搜索别名集合（已归一化）。含官方全名/简称/引文名/键，
    以及「马克思恩格斯→马恩」与去掉「习近平」前缀的口语化变体。"""
    seeds = {cfg.title, cfg.short_title, cfg.citation_title, cfg.key}
    variants: set[str] = set()
    for text in seeds:
        text = str(text or "")
        if not text:
            continue
        variants.add(text)
        variants.add(text.replace("马克思恩格斯", "马恩"))  # 马克思恩格斯→马恩
        if text.startswith("习近平"):  # 习近平法治思想学习纲要 → 法治思想学习纲要
            variants.add(text[len("习近平"):])
    aliases = {a for a in (_toc_norm(v) for v in variants) if len(a) >= 2}
    return sorted(aliases, key=len, reverse=True)


def _book_edition(cfg: BookConfig) -> int | None:
    """从引文全名解析版次（如「马克思恩格斯全集（第二版）」→ 2）；无版次标注返回 None。"""
    m = _EDITION_RE.search(_toc_norm(cfg.citation_title) or "")
    return _cn_to_int(m.group(1)) if m else None


def _build_book_alias_index() -> list[dict]:
    index: list[dict] = []
    if corpus is None:
        return index
    for cfg in BOOK_CONFIGS:
        if not getattr(cfg, "available", True):
            continue
        vols: list[dict] = []
        for v in corpus.get_volumes(cfg.key):
            first_page = v.pages[0].pdf_page if v.pages else 1
            vols.append(
                {
                    "volume": int(v.volume),
                    "source_file": v.source_file,
                    "first_page": int(first_page or 1),
                }
            )
        if not vols:
            continue
        vols.sort(key=lambda x: (x["volume"], x["source_file"]))
        index.append(
            {
                "book": cfg.key,
                "aliases": _book_aliases(cfg),
                "edition": _book_edition(cfg),
                # single_volume：引文不冠卷次的独立著作，或本就只有一卷（下拉直达时不显示「第N卷」）。
                "single_volume": bool(getattr(cfg, "single_volume", False)) or len(vols) == 1,
                "volumes": vols,
                **_book_payload(cfg.key),
            }
        )
    return index


def _get_book_alias_index() -> list[dict]:
    global _BOOK_ALIAS_INDEX
    if _BOOK_ALIAS_INDEX is None:
        with _BOOK_ALIAS_LOCK:
            if _BOOK_ALIAS_INDEX is None:
                _BOOK_ALIAS_INDEX = _build_book_alias_index()
    return _BOOK_ALIAS_INDEX


def _book_alias_rank(residual: str, aliases: list[str]) -> int | None:
    """书名部分与某书库别名的匹配强度：0=完全相等，1=互为前缀，2=残串是别名子串，
    3=别名是残串子串。都不满足返回 None。数值越小越优先。"""
    best: int | None = None
    for a in aliases:
        if residual == a:
            return 0
        if a.startswith(residual) or residual.startswith(a):
            rank = 1
        elif residual in a:
            rank = 2
        elif a in residual:
            rank = 3
        else:
            continue
        best = rank if best is None else min(best, rank)
    return best


def _interpret_book_query(qn: str, scope: str | None) -> list[dict]:
    """把「书名(+卷次)(+版次)」的查询解释成整卷直达候选。返回 proto-hit 列表
    （含书库元信息 b、卷信息 v、是否单卷 single），URL 由调用方按 mode 生成。"""
    if len(qn) < 2:
        return []
    idx = _get_book_alias_index()
    if scope:
        idx = [b for b in idx if b["book"] == scope]
        if not idx:
            return []

    volume_no: int | None = None
    edition_no: int | None = None
    residual = qn
    mvol = _VOL_RE.search(residual)
    if mvol:
        volume_no = _cn_to_int(mvol.group(1))
        residual = residual[: mvol.start()] + residual[mvol.end():]
    med = _EDITION_RE.search(residual)
    if med:
        edition_no = _cn_to_int(med.group(1))
        residual = residual[: med.start()] + residual[med.end():]
    residual = residual.strip()

    candidates: list[tuple[int, int, int, dict]] = []
    for b in idx:
        if residual:
            rank = _book_alias_rank(residual, b["aliases"])
        else:
            # 残串为空但已限定书库（或仅输入了卷次）：把当前范围书库当作命中书名。
            rank = 0 if scope else None
        if rank is None:
            continue
        if edition_no is not None:
            ed_pref = 0 if b["edition"] == edition_no else 1
        else:
            # 未显式给版次：优先第一版（edition 为空视作第一版），第二版及以后次之。
            ed_pref = 0 if (b["edition"] or 1) == 1 else 1
        candidates.append((ed_pref, rank, b["book_sort_order"], b))
    if not candidates:
        return []
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))

    hits: list[dict] = []
    if volume_no is not None:
        # 指定卷次：在候选书库里按优先级找第一个拥有该卷者。
        for _ed, _rank, _order, b in candidates:
            matched = [v for v in b["volumes"] if v["volume"] == volume_no]
            if matched:
                for v in matched:  # 全集第26卷分 3 册，可能不止一条
                    hits.append({"b": b, "v": v, "single": False})
                break
        return hits[:8]

    # 未给卷次：取最佳候选书库；单卷本直接一条，多卷本列出各卷（上限 8）。
    best = candidates[0][3]
    if best["single_volume"]:
        hits.append({"b": best, "v": best["volumes"][0], "single": True})
    else:
        for v in best["volumes"][:8]:
            hits.append({"b": best, "v": v, "single": False})
    return hits[:8]


@app.route("/api/library/journal-diag")
def api_journal_diag():
    # 仅管理员：该诊断会在请求线程上顺序发起多次外呼（每次 25s 超时）并回内部语料统计，
    # 匿名可达会被用来饿死线程池/爬运营数据，故与其它 /admin 诊断一致要求管理员。
    _require_admin()
    try:
        return jsonify(journal_abstract_diag())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": repr(exc)[:200]})


@app.route("/api/library/ncpssd-probe")
def api_ncpssd_probe():
    # 诊断用：从服务器侧探测 NCPSSD 摘要接口是否可达（固定 URL、无用户输入、不含敏感信息）。
    # 仅管理员：含同步外呼，避免匿名借此饿死请求线程。
    _require_admin()
    try:
        return jsonify(ncpssd_detail_probe())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "stage": "exception", "error": repr(exc)[:200]})


@app.route("/api/library/toc-suggest")
def api_library_toc_suggest():
    _require_content_feature("library")
    _require_search()
    raw = (request.args.get("q") or "").strip()
    mode = "reader" if (request.args.get("mode") or "").strip() == "reader" else "ai"
    # book：限定到单一书库或专题 collection（如 western_marxism）。
    scope = (request.args.get("book") or "").strip()
    known_books = {b["book"] for b in _get_book_alias_index()}
    if not scope:
        scope_book_set = set()
    elif scope in known_books:
        scope_book_set = {scope}
    else:
        scope_book_set = {cfg.key for cfg in BOOK_CONFIGS if cfg.collection == scope and getattr(cfg, "available", True)}
    if scope and not scope_book_set:
        scope = ""
        scope_book_set = set()
    qn = _toc_norm(raw)
    if len(qn) < 2:
        return jsonify({"ok": True, "results": []})
    # 1) 书名 / 卷次直达（置顶）：直接输入书名（可带卷次/版次）跳整卷。
    _viewer_mode = "reader" if mode == "reader" else "ai"
    book_results = []
    for proto in _interpret_book_query(qn, scope if scope in known_books else None):
        b, v, single = proto["b"], proto["v"], proto["single"]
        if scope_book_set and b["book"] not in scope_book_set:
            continue
        book_results.append(
            {
                "kind": "book",
                # 主文本用整书全名；卷次由前端徽标（书库简称·第N卷）区分，避免重复。
                "title": b["book_title"],
                "book": b["book"],
                "book_title": b["book_title"],
                "book_short_title": b["book_short_title"],
                "book_sort_order": b["book_sort_order"],
                "tag_class": b["tag_class"],
                "volume": v["volume"],
                "single": single,
                "page": v["first_page"],
                "printed": "",
                "url": url_for(
                    "pdf_viewer",
                    file=v["source_file"],
                    page=v["first_page"],
                    mode=_viewer_mode,
                ),
            }
        )
    # 2) 篇章标题补全（原逻辑）；scope 命中时只在该书库内匹配。
    ranked_matches: list[tuple] = []
    for item in _get_toc_suggest_index():
        if scope_book_set and item["book"] not in scope_book_set:
            continue
        pos = item["norm"].find(qn)
        if pos < 0:
            continue
        match_rank, aux_rank, level = _toc_match_rank(item, qn)
        ranked_matches.append(
            (
                item.get("book_sort_order", 9999),
                match_rank,
                aux_rank,
                len(item["title"]),
                pos,
                item["volume"],
                item.get("pdf_page", 1),
                level,
                item.get("sort_order", 0),
                item,
            )
        )
    # 不再「一旦存在精确标题命中就只保留精确命中」。那条规则会让某个低优先级
    # 书库里恰好与查询同名的短标题（如《列宁全集》里的「《反杜林论》」）把《文集》
    # 《全集》中所有相关篇章全部挤掉，造成「反杜林论只剩列宁全集」的偏斜。
    # 现在保留全部命中，由排序键自然分层：先按书库顺序（文集→全集→列宁），
    # 同一书库内再按 精确>前缀>子串、正文优先于附属、标题更短者优先。
    # 精确命中仍会排在其所在书库的最前，但不会再把其它书库的相关篇章整体抹掉。
    ranked_matches.sort(key=lambda match: match[:-1])
    # 多书库均衡：先给每个命中书库各放 1 条「该库最佳命中」（上面已按排序键把各库最优排到最前），
    # 保证低优先级书库（选集 sort 28 / 全集二版 25 / 列宁 30）在「共产党宣言 / 资本论 / 反杜林论」这类
    # 多版次 + 大量序言手稿的热词下也能露出，不被《文集》《全集》占满前 20 名；随后按原排序补足其余名额。
    _seeds, _rest, _seen_books = [], [], set()
    for _m in ranked_matches:
        _b = _m[-1]["book"]
        (_rest if _b in _seen_books else _seeds).append(_m)
        _seen_books.add(_b)
    ranked_matches = _seeds + _rest
    # 书名直达置顶，篇章命中补足其余名额（合计 20 条）。
    chapter_limit = max(0, 20 - len(book_results))
    chapter_results = []
    for *_, item in ranked_matches[:chapter_limit]:
        chapter_results.append(
            {
                "kind": "chapter",
                "title": item["title"],
                "book": item["book"],
                "book_title": item["book_title"],
                "book_short_title": item["book_short_title"],
                "book_sort_order": item["book_sort_order"],
                "tag_class": item["tag_class"],
                "volume": item["volume"],
                "page": item["pdf_page"],
                "printed": item["printed_page"],
                "url": url_for(
                    "pdf_viewer",
                    file=item["source_file"],
                    page=item["pdf_page"],
                    section=item["title"],
                    printed=item["printed_page"],
                    mode=("reader" if mode == "reader" else "ai"),
                ),
            }
        )
    return jsonify({"ok": True, "results": book_results + chapter_results})


def _warm_toc_suggest_index() -> None:
    try:
        _get_toc_suggest_index()
        _get_book_alias_index()
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("TOC suggest index warm failed: %s", exc)


if corpus is not None:
    threading.Thread(target=_warm_toc_suggest_index, name="toc-suggest-warm", daemon=True).start()


@app.route("/api/library/volume-toc")
def api_library_volume_toc():
    """按需返回单卷目录条目（供阅读器展开某卷时懒加载，避免首屏渲染上万条目录致卡顿）。"""
    _require_content_feature("library")
    _require_search()
    source_file = (request.args.get("file") or "").strip()
    mode = "reader" if (request.args.get("mode") or "").strip() == "reader" else "ai"
    volume = corpus.get_volume_by_source_file(source_file) if corpus else None
    if volume is None:
        return jsonify({"ok": False, "results": []}), 404
    results = []
    for entry in corpus.get_toc_entries(volume.source_file):
        title = str(getattr(entry, "title", "") or "")
        pdf_page = int(getattr(entry, "pdf_page", 1) or 1)
        printed = str(getattr(entry, "printed_page", "") or "")
        results.append(
            {
                "title": title,
                "level": max(1, min(6, int(getattr(entry, "level", 1) or 1))),
                "pdf_page": pdf_page,
                "printed_page": printed,
                "url": url_for(
                    "pdf_viewer",
                    file=volume.source_file,
                    page=pdf_page,
                    section=title,
                    printed=printed,
                    mode=mode,
                ),
            }
        )
    resp = jsonify({"ok": True, "results": results})
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@app.route("/viewer")
def pdf_viewer():
    viewer_mode = "reader" if (request.args.get("mode") or "").strip() == "reader" else "ai"
    _require_content_feature("library" if viewer_mode == "reader" else _viewer_entry_feature())
    _require_search()
    source_file = _normalize_source_file((request.args.get("file") or "").strip())
    page = max(1, request.args.get("page", type=int) or 1)
    query_text = " ".join((request.args.get("q") or "").split())
    highlight_text = " ".join((request.args.get("h") or "").split()) or query_text
    requested_section = (request.args.get("section") or "").strip() or None
    requested_printed = (request.args.get("printed") or "").strip() or None
    _require_full_mode()
    _rate_limit_reader_ip_or_abort("view")
    if not source_file or source_file not in ALLOWED_SOURCE_FILES:
        abort(404, description="请求的资料不在白名单中。")
    volume = corpus.get_volume_by_source_file(source_file) if corpus else None
    if volume is None:
        abort(404, description="未找到对应的卷册信息。")
    # 《全集》等仅保留 OCR 文本、未随包下发 PDF 的卷册回退到「纯文字」渲染，
    # 仍可逐页阅读并使用 AI 导读；《文集》等带 PDF 的卷册维持原有「书页图像」渲染。
    render_mode = "image" if _pdf_render_available(source_file) else "text"
    pdf_display_name = Path(source_file).name
    toc_entries = [entry.to_dict() for entry in corpus.get_toc_entries(source_file)] if corpus else []
    current_section = requested_section or (
        corpus.get_section_for_page(source_file, page) if corpus else None
    )
    page_count = len(volume.pages) if volume else 1
    page_labels: dict[int, str] = {}
    if volume:
        for page_obj in volume.pages:
            page_labels[page_obj.pdf_page] = page_obj.printed_page or f"PDF-{page_obj.pdf_page}"
    current_page_label = requested_printed or page_labels.get(page, f"PDF-{page}")
    # 这里曾对每次 image 模式 /viewer 加载 spawn 一个 daemon 线程预渲染当前页；但浏览器随即发出的
    # /page-image 请求会用相同缓存键、经渲染信号量把同一页渲染好，预渲染纯属重复劳动，且 scrape 洪峰下
    # 会绕过 waitress 计数堆出大量裸线程。故移除：当前页交给紧随的 /page-image 渲染即可。
    state = current_view_state()
    ai_viewer_args = {
        "file": source_file,
        "page": page,
        "mode": "ai",
    }
    if query_text:
        ai_viewer_args["q"] = query_text
    if highlight_text and highlight_text != query_text:
        ai_viewer_args["h"] = highlight_text
    if requested_section:
        ai_viewer_args["section"] = requested_section
    if requested_printed:
        ai_viewer_args["printed"] = requested_printed
    show_ai_panel = viewer_mode != "reader"
    # 「返回文库目录」按钮：回到进入时的文库目录页（基础阅读器→/reader，AI 导学/引文检索→/library），
    # 不管用户是点目录条还是从引文检索进来的，都能统一回到可浏览各卷目录的文库页，而非只能回检索首页。
    # 四页面布局已 cutover：「返回文库目录」一律回新版「阅读」页(/v2/read)——无论从检索结果、书目、篇章直达、
    # 历史记录还是直链进来的，都统一回到新版可浏览各卷目录的阅读页，绝不再跳回已退役的 /library、/reader。
    # （/v2/read 与旧 /library 用同一套 _library_volumes 书目，书目无缺失。）
    library_back_url = url_for("layout_read")
    # 「返回检索」回新版检索页：默认带 restore 恢复上次检索态；从 /v2/read 进来的回 /v2（layout_search）。
    search_back_url = "/?restore=1"
    if (request.args.get("from") or "").strip() == "v2read":
        search_back_url = url_for("layout_search")
    return render_template(
        "viewer.html",
        search_back_url=search_back_url,
        app_name=APP_NAME,
        app_version=APP_VERSION,
        request_token=REQUEST_TOKEN if state["management_api_enabled"] else None,
        state=state,
        ai_runtime=_public_ai_runtime_payload(allow_details=bool(state["feature_access"].get("ai", False))),
        page=page,
        page_count=page_count,
        current_page_label=current_page_label,
        page_labels=page_labels,
        source_file=source_file,
        pdf_name=pdf_display_name,
        pdf_url=url_for("serve_pdf", file=source_file),
        render_mode=render_mode,
        viewer_mode=viewer_mode,
        show_ai_panel=show_ai_panel,
        basic_reader_mode=not show_ai_panel,
        library_back_url=library_back_url,
        toc_entries=toc_entries,
        current_section=current_section,
        volume=volume,
        query_text=query_text,
        highlight_text=highlight_text,
        ai_upsell=_ai_reader_upsell(url_for("pdf_viewer", **ai_viewer_args)),
        ai_access_enabled=bool(_feature_is_available("ai") and _feature_effective_for_user("ai")),
        ai_web_access_enabled=_ai_web_access_enabled(),
        # 会员专属「笔记」：阅读器内选中记笔记 + 本卷笔记面板；未授权则前端不挂载笔记模块。
        notes_access_enabled=_notes_access_enabled(),
        # 阅读器「检索原著原文」（接地）复用随心问链路 → 仅在持有 search_chat 权限时露出该开关。
        # 检索范围 chips 与随心问一致（多选著作群，共用 localStorage 偏好），默认仍为「自动」。
        search_chat_access_enabled=bool(_feature_is_available("search_chat") and _feature_effective_for_user("search_chat")),
        ai_scope_options=_scope_options_payload(),
        book_scope_tree=_book_scope_tree(),  # 「精选到书/卷」多选控件数据
    )


@app.route("/pdf")
def serve_pdf():
    _require_reader_asset_access()
    # 安全加固（P1）：公网服务器模式下不再下发整本 PDF 原文件，避免核心资料被整本抓取/转载。
    # 阅读器本身依赖 /page-image 渲染显示，并不需要原始 PDF；此处仅保留桌面端与管理员访问，
    # 普通登录用户与匿名访问统一返回 404。保留路由注册以兼容 url_for('serve_pdf') 引用。
    if DEPLOYMENT.is_server and not (
        _admin_content_access_enabled() or _desktop_content_access_enabled()
    ):
        abort(404, description="PDF 原文件暂不提供下载。")
    pdf_path = _resolve_pdf_path((request.args.get("file") or "").strip())
    return send_file(pdf_path, mimetype="application/pdf", conditional=True)


@app.route("/page-image")
def page_image():
    _require_reader_asset_access()
    _rate_limit_page_image_or_abort()
    _rate_limit_reader_ip_or_abort("pageimg")
    source_file = _normalize_source_file((request.args.get("file") or "").strip())
    page_number = max(1, request.args.get("page", type=int) or 1)
    query_text = " ".join((request.args.get("q") or "").split())
    highlight_text = " ".join((request.args.get("h") or "").split()) or query_text
    # WebP 内容协商：客户端 Accept 含 image/webp 且服务端 Pillow 可用时产/取 .webp 变体（体积 −30~52%，
    # 对跨境慢链路直接提速）；否则一律走现行 JPEG。同一 URL 按 Accept 分变体（下方 Vary: Accept）。
    want_webp = _pillow_or_none() is not None and "image/webp" in (request.headers.get("Accept") or "")
    cache_path = _render_page_image_to_cache(source_file, page_number, highlight_text, want_webp=want_webp)
    _touch_page_image_mtime(cache_path)  # 命中即顶 mtime（节流），使按 mtime 的 LRU 清理真实有效
    # 邻页预热默认关闭；仅在开启时才需要卷的总页数。关闭时跳过 corpus 查卷 + len(pages)，
    # 热路径不必要的开销一并省掉。
    if PAGE_IMAGE_PREWARM_ENABLED:
        volume = corpus.get_volume_by_source_file(source_file) if corpus else None
        page_count = len(volume.pages) if volume else page_number
        _prewarm_page_images(source_file, page_number, highlight_text, page_count)
    _prune_page_image_cache_if_due()
    is_webp = cache_path.suffix.lower() == ".webp"  # 实际产出格式（webp 编码失败已回退 .jpg）
    resp = send_file(cache_path, mimetype=("image/webp" if is_webp else "image/jpeg"), conditional=True, max_age=86400)
    # 书页图像缓存策略（在「读得快」与「内容可纠正」之间取稳妥平衡）：
    #   · private —— 只进本人浏览器缓存，绝不进 Cloudflare/反代等共享缓存，杜绝「未授权访客从共享
    #     缓存命中受保护书页图」的越权（本站书页内容受版权保护、有专门反爬）。
    #   · max-age=7 天 —— 一周内重读/前后翻回看过的页**零网络瞬开**（原仅 1 天）。
    #   · stale-while-revalidate=1 天 —— 过期后先用旧图秒显、后台再校验，不阻塞翻页。
    #   · 仍带 conditional ETag、且**不**加 immutable —— 万一某卷 PDF 被替换（URL 不变），既能靠
    #     ETag 在再校验时自动取到新图，用户手动刷新也能立刻拿到新内容，不会被永久钉死在旧图上。
    resp.headers["Cache-Control"] = "private, max-age=604800, stale-while-revalidate=86400"
    # 同一 URL 按 Accept 分 webp/jpg 两变体：Vary 确保浏览器缓存不会把 webp 应答错喂给只收 jpg 的客户端
    # （或反之）。page-image 本就 private、不进共享缓存，Vary 仅作用于浏览器私有缓存的正确性。
    resp.headers["Vary"] = "Accept"
    return resp


# 「阅读」页书目卡封面：某卷第 1 页的小尺寸缩略图（约 240px 宽），供四页面布局的书目网格展示
# 真实封面缩小版。与 /page-image（全尺寸页图，约 600KB）分开：本端点只出小图，缓存到 APPDATA、
# 按源文件哈希命名、一次渲染长期复用；权限沿用阅读资产门（有阅读权即可看封面，含放行的游客）。
_READER_COVER_DIR = APPDATA_DIR / "reader_covers"


@app.route("/reader/cover")
def reader_cover():
    _require_reader_asset_access()
    _rate_limit_reader_ip_or_abort("cover")
    source_file = _normalize_source_file((request.args.get("file") or "").strip())
    if not source_file or source_file not in ALLOWED_SOURCE_FILES:
        abort(404, description="请求的资料不在白名单中。")
    _READER_COVER_DIR.mkdir(parents=True, exist_ok=True)
    tag = sha256(source_file.encode("utf-8")).hexdigest()[:16]
    cache_path = _READER_COVER_DIR / f"{tag}.png"
    if not cache_path.exists():
        try:
            pdf_path = _resolve_pdf_path(source_file, require_full_mode=False)
            with fitz.open(str(pdf_path)) as doc:
                page = doc[0]
                zoom = 240.0 / max(1.0, page.rect.width)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                data = pix.tobytes("png")
        except Exception:
            # 纯文字卷（未随包下发 PDF）或渲染失败：无封面，让前端 onerror 显示占位。
            abort(404, description="该卷暂无可用封面。")
        tmp = cache_path.with_suffix(".png.tmp")
        tmp.write_bytes(data)
        tmp.replace(cache_path)
    resp = send_file(str(cache_path), mimetype="image/png", conditional=True, max_age=604800)
    resp.headers["Cache-Control"] = "private, max-age=604800"
    return resp


@app.route("/releases/<path:filename>")
def serve_release_file(filename: str):
    if not DEPLOYMENT.is_server:
        abort(404, description="发布文件仅在服务器模式提供。")
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != Path(filename).name:
        abort(404, description="发布文件不存在。")
    release_path = (RUNTIME_ROOT / "releases" / safe_name).resolve()
    release_root = (RUNTIME_ROOT / "releases").resolve()
    try:
        release_path.relative_to(release_root)
    except ValueError:
        abort(404, description="发布文件不存在。")
    if not release_path.exists() or not release_path.is_file():
        abort(404, description="发布文件不存在。")
    return send_file(release_path, as_attachment=True, conditional=True)


@app.route("/api/ping", methods=["POST"])
def api_ping():
    if not DEPLOYMENT.enable_idle_shutdown:
        abort(403, description="当前模式未启用心跳保活接口。")
    _require_local_console()
    _require_management_token()
    _last_ping[0] = time.time()
    return "", 204


@app.route("/api/runtime")
def api_runtime():
    state = current_view_state()
    return jsonify(
        {
            "ok": True,
            "app_mode": state["app_mode"],
            "search_enabled": state["search_enabled"],
            "pdf_enabled": state["pdf_enabled"],
            "db_status": state["db_status"],
            "data_version": state["data_version"],
            "issues": state["issues"],
            "management_api_enabled": state["management_api_enabled"],
        }
    )


@app.get("/api/feedback/thread")
def api_feedback_thread():
    user = getattr(g, "current_user", None)
    if not user:
        abort(401, description="请先登录后使用留言功能。")
    thread = get_feedback_user_thread(int(user["id"]), mark_seen=True)
    return jsonify({"ok": True, "thread": thread})


@app.post("/api/feedback/messages")
def api_feedback_message_create():
    user = getattr(g, "current_user", None)
    if not user:
        abort(401, description="请先登录后使用留言功能。")
    if not _is_admin_user(user):
        _rate_limit_or_abort(
            f"feedback:user:{user['id']}",
            limit=RATE_LIMITS["feedback_user"][0],
            window_seconds=RATE_LIMITS["feedback_user"][1],
            message="留言提交过于频繁，请稍后再试。",
        )
    # 兼容两种提交：multipart/form-data（前端始终用 FormData，含或不含图片）与历史的
    # application/json（纯文本）。注意不能用 request.files 判断——纯文字留言没有文件，
    # request.files 为空会错误地落到 JSON 分支导致 body 丢失、留言提交失败。
    if request.form:
        body = str(request.form.get("body") or "").strip()
    else:
        payload = request.get_json(silent=True) or {}
        body = str(payload.get("body") or "").strip()
    if len(body) > 2000:
        return jsonify({"ok": False, "error": "留言最多 2000 字。"}), 400
    attachments, upload_warnings = _save_feedback_uploads(request.files.getlist("images"))
    if len(body) < 2 and not attachments:
        return jsonify({"ok": False, "error": "请至少输入两个字，或上传一张图片。"}), 400
    thread, message = add_feedback_user_message(user, body, attachments)
    sent, error = _send_feedback_admin_notice(thread, message)
    update_feedback_message_email_status(int(message["id"]), "sent" if sent else "failed", error)
    if not sent:
        message["email_status"] = "failed"
        message["email_error"] = error
    warning = "；".join(upload_warnings)
    if not sent:
        mail_warning = f"留言已保存，但管理员通知邮件发送失败：{error}"
        warning = f"{warning}；{mail_warning}" if warning else mail_warning
    return jsonify(
        {
            "ok": True,
            "thread": get_feedback_user_thread(int(user["id"]), mark_seen=False),
            "mail_sent": sent,
            "warning": warning,
        }
    )


@app.post("/admin/page-errors/<int:report_id>/status")
def admin_page_error_resolve(report_id: int):
    """把一条页码报错标记为「已处理」/「重新打开」。仅网站 /admin 内容运营页可用。"""
    _require_admin()
    _require_management_csrf()
    status = "resolved" if (request.form.get("status") or "resolved") == "resolved" else "open"
    ok = set_page_error_report_status(report_id, status)
    _log_management_action(
        action="page_error.status",
        target=str(report_id),
        result="success" if ok else "not_found",
        remote_admin=True,
        details={"status": status},
    )
    flash("已标记为已处理。" if status == "resolved" else "已重新打开该报错。", "success" if ok else "warning")
    return _management_redirect(True, "copy")


@app.post("/admin/feedback/<int:thread_id>/reply")
def admin_feedback_reply(thread_id: int):
    _require_admin()
    _require_management_csrf()
    body = (request.form.get("body") or "").strip()
    if len(body) > 2000:
        flash("回复内容最多 2000 字。", "warning")
        return _management_redirect(True, "copy")
    attachments, upload_warnings = _save_feedback_uploads(request.files.getlist("images"))
    for warning in upload_warnings:
        flash(warning, "warning")
    if len(body) < 2 and not attachments:
        flash("回复内容至少需要两个字，或上传一张图片。", "warning")
        return _management_redirect(True, "copy")
    try:
        thread, message = add_feedback_admin_reply(thread_id, g.current_user, body, attachments)
    except ValueError as exc:
        flash(str(exc), "warning")
        return _management_redirect(True, "copy")
    sent, error = _send_feedback_user_reply(thread, message)
    update_feedback_message_email_status(int(message["id"]), "sent" if sent else "failed", error)
    _log_management_action(
        action="feedback.reply",
        target=str(thread_id),
        result="success" if sent else "mail_failed",
        remote_admin=True,
        details={"user_email": str(thread.get("user_email") or ""), "mail_error": error},
    )
    if sent:
        flash("留言回复已保存，并已发送到用户邮箱。", "success")
    else:
        flash(f"留言回复已保存，但邮件发送失败：{error}", "warning")
    return _management_redirect(True, "copy")


@app.get("/api/feedback/attachment/<int:attachment_id>")
def api_feedback_attachment(attachment_id: int):
    user = getattr(g, "current_user", None)
    if not user:
        abort(401, description="请先登录后查看留言图片。")
    attachment = get_feedback_attachment(attachment_id)
    if not attachment:
        abort(404, description="留言图片不存在。")
    # 仅会话所属用户本人或管理员可查看，防止越权拉取他人留言图片。
    if not _is_admin_user(user) and int(attachment.get("thread_user_id") or 0) != int(user["id"]):
        abort(403, description="无权查看该留言图片。")
    stored_name = secure_filename(str(attachment.get("stored_name") or ""))
    if not stored_name:
        abort(404, description="留言图片不存在。")
    image_path = (FEEDBACK_IMAGE_DIR / stored_name).resolve()
    try:
        image_path.relative_to(FEEDBACK_IMAGE_DIR.resolve())
    except ValueError:
        abort(404, description="留言图片不存在。")
    if not image_path.exists() or not image_path.is_file():
        abort(404, description="留言图片不存在。")
    mime = str(attachment.get("mime") or "") or "application/octet-stream"
    return send_file(image_path, mimetype=mime, conditional=True, max_age=86400)


@app.post("/api/desktop/activate")
def api_desktop_activate():
    if not DEPLOYMENT.is_server:
        abort(403, description="桌面激活接口仅在网站服务器模式启用。")
    payload = request.get_json(silent=True) or {}
    try:
        result = activate_desktop_device(
            activation_code=str(payload.get("activation_code") or ""),
            fingerprint=str(payload.get("fingerprint") or ""),
            label=str(payload.get("label") or ""),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    device = result["device"]
    sync_payload = _public_desktop_sync_payload(device)
    return jsonify({"ok": True, "token": result["token"], **sync_payload})


@app.post("/api/desktop/sync")
def api_desktop_sync():
    device = _require_desktop_device()
    payload = request.get_json(silent=True) or {}
    fingerprint = str(payload.get("fingerprint") or "").strip()
    if fingerprint and str(device.get("fingerprint") or "").strip() and fingerprint != str(device.get("fingerprint")):
        abort(403, description="本地设备指纹与授权记录不一致。")
    return jsonify({"ok": True, **_public_desktop_sync_payload(device)})


@app.route("/api/desktop/releases/latest")
def api_desktop_latest_release():
    if DEPLOYMENT.is_server and _desktop_bearer_token():
        _require_desktop_device()
    return jsonify({"ok": True, "release": latest_release(request.args.get("channel") or "stable")})


@app.post("/api/desktop/ai/search-chat")
def api_desktop_ai_search_chat():
    _require_desktop_device()
    _require_ai()
    payload = request.get_json(silent=True) or {}
    question = " ".join(str(payload.get("question") or "").split())
    messages = payload.get("messages") or []
    if not question:
        return jsonify({"ok": False, "error": "问题不能为空。"}), 400
    try:
        answer = AI_CLIENT.answer_search_chat(messages, question)
    except AIServiceError as exc:
        LOGGER.warning("Desktop search AI failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify(answer.to_dict())


@app.post("/api/desktop/ai/pdf-chat")
def api_desktop_ai_pdf_chat():
    _require_desktop_device()
    _require_ai()
    payload = request.get_json(silent=True) or {}
    source_file = _normalize_source_file(str(payload.get("source_file") or "").strip())
    page = max(1, int(payload.get("page") or 1))
    question = " ".join(str(payload.get("question") or "").split())
    selected_text = str(payload.get("selected_text") or "").strip()
    web_enabled = _coerce_bool(payload.get("web_enabled"))
    quick_mode = _coerce_bool(payload.get("quick_mode"))
    messages = payload.get("messages") or []
    if not question:
        return jsonify({"ok": False, "error": "问题不能为空。"}), 400
    page_context = _get_page_context_payload(source_file, page)
    try:
        answer = AI_CLIENT.answer_pdf_chat(
            messages=messages,
            question=question,
            source_file=source_file,
            page=page,
            selected_text=selected_text,
            web_enabled=web_enabled,
            quick_mode=quick_mode,
            page_context=page_context,
        )
    except AIServiceError as exc:
        LOGGER.warning("Desktop PDF AI failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify(answer.to_dict())


@app.route("/api/ai/runtime")
def api_ai_runtime():
    return jsonify({"ok": True, **_public_ai_runtime_payload()})


@app.get("/api/ai/assistant-config")
def api_ai_assistant_config():
    """全站「AI 随心问」右侧抽屉的引导配置：运行时 + 权限 + 本人额度 + 文案。

    抽屉懒加载（首次展开才请求一次），不在每个页面加载时调用，零额外常态开销。
    只返回当前用户自己的额度/余额，绝不泄露他人数据。
    """
    user = getattr(g, "current_user", None)
    access = bool(_feature_is_available("search_chat") and _feature_effective_for_user("search_chat"))
    ai_detail = bool(_feature_is_available("ai") and _feature_effective_for_user("ai"))
    # 研究型检索（研究综述）走独立权限位；「AI 研究对话」页据此决定是否放出「研究」深度档。
    research_access = bool(_feature_is_available("research") and _feature_effective_for_user("research"))
    runtime = _public_ai_runtime_payload(allow_details=bool(access or ai_detail))
    # 「AI 随心问」(search_chat) 无权限提示要跟着当前权限走，而非套用「AI 导学」(ai) 的会员文案：
    # 若已向「注册用户」审众放开 search_chat → 仅需登录（每日有免费额度）；否则视为需开通会员。
    chat_login_gated = bool(
        (_load_access_policy().get("audience") or {}).get("registered", {}).get("search_chat", False)
    )
    locked_message = render_site_text(
        "index.ai_drawer_locked_login" if chat_login_gated else "index.ai_drawer_locked_member"
    )
    return jsonify(
        {
            "ok": True,
            "access": access,
            "access_gate": "login" if chat_login_gated else "member",
            "web_access": bool(_ai_web_access_enabled()),
            "runtime": runtime,
            "quota": _ai_token_quota_payload() if access else {},
            "credits": (
                get_ai_credit_balances(int(user["id"]))
                if user
                else {"research": 0, "chat": 0, "reader": 0}
            ),
            "logged_in": bool(user),
            "locked_message": locked_message,
            "unavailable_message": render_site_text("index.ai_unavailable"),
            "scopes": _scope_options_payload(),  # 「检索范围」下拉：自动/全部 + 各著作群
            "book_scope_tree": _book_scope_tree(),  # 「精选到书/卷」多选控件数据
            # 「研究」深度档（研究综述）：独立权限位 + 每周「次数」额度。仅供 /ai 页判断显隐与额度展示，
            # 现有右侧抽屉不读这两个字段（多带无害）。
            "research_access": research_access,
            "research_quota": _research_quota_payload(user) if research_access else {},
        }
    )


@app.route("/api/pdf-page-context")
def api_pdf_page_context():
    _require_reader_asset_access()
    source_file = _normalize_source_file((request.args.get("file") or "").strip())
    page = max(1, request.args.get("page", type=int) or 1)
    context = _get_page_context_payload(source_file, page)
    return jsonify({"ok": True, "context": context})


def _reader_find_snippet(raw_text: str, query: str, width: int = 36) -> str:
    """从某页原文里截取包含 query 的上下文片段（best-effort，供「查找本书」结果展示）。"""
    raw = raw_text or ""
    for cand in (query, query.replace(" ", "")):
        if not cand:
            continue
        idx = raw.find(cand)
        if idx >= 0:
            start = max(0, idx - width)
            end = min(len(raw), idx + len(cand) + width)
            snip = " ".join(raw[start:end].split())
            return ("…" if start > 0 else "") + snip + ("…" if end < len(raw) else "")
    return " ".join(raw.split())[: width * 2]


@app.post("/api/reader/report-page-error")
def api_reader_report_page_error():
    """阅读器引文「一键报错」：读者反馈某处页码有误。记录 + 管理员邮件提醒（仿留言功能）。
    同一 (阅读器,定位来源,页码) 已有未处理报告则只累加计数、不重复发邮件。轻限流防刷。"""
    # 限流：按会话 + 真实 IP，10 分钟最多 8 次，防误点/脚本刷屏（不影响正常单击）。
    ident = _visitor_session_key() or _client_ip() or "anon"
    _rate_limit_or_abort(f"pageerr:{ident}", limit=8, window_seconds=600,
                         message="报错提交过于频繁，请稍后再试。")
    data = request.get_json(silent=True) or {}
    reader = str(data.get("reader") or "").strip().lower()
    if reader not in ("viewer", "liushi", "wenku"):
        reader = "viewer"
    page = " ".join(str(data.get("page") or "").split())[:32]
    citation_text = " ".join(str(data.get("citation") or "").split())[:600]
    # 无页码也无引文的空报错直接拒绝（避免脚本刷空记录）。
    if not page and not citation_text:
        abort(400, description="缺少页码或引文信息，无法提交报错。")
    user = getattr(g, "current_user", None)
    report, is_new = create_page_error_report(
        reader=reader,
        book_title=" ".join(str(data.get("book") or "").split())[:200],
        volume_label=" ".join(str(data.get("volume") or "").split())[:200],
        page=page,
        source_ref=str(data.get("source_ref") or "").strip()[:500],
        citation_text=citation_text,
        note=" ".join(str(data.get("note") or "").split())[:800],
        user_id=(int(user["id"]) if user else None),
        user_email=(str(user.get("email") or "") if user else ""),
        client_ip=_client_ip(),
        user_agent=str(request.headers.get("User-Agent") or "")[:300],
    )
    if is_new:
        # 邮件提醒丢后台线程发，绝不阻塞用户点击的响应（发信失败也不影响「已记录」）。
        threading.Thread(
            target=_send_page_error_admin_notice, args=(report,),
            name="page-error-notice", daemon=True,
        ).start()
    return jsonify({"ok": True, "recorded": True, "deduped": (not is_new)})


# ---------------------------------------------------------------------------
# 会员专属「笔记 / 我的知识库」：阅读器内选中记笔记，跨书聚合复习。数据落 notes.sqlite3（数据盘）。
# 所有接口按 g.current_user["id"] 归属隔离；写操作走全站 CSRF 钩子（前端带 X-CSRF-Token）。
# ---------------------------------------------------------------------------
def _require_notes_access():
    """笔记为会员专属：未登录 → 401，登录但无权限 → 403（沿用内容权限门禁）。返回当前用户 dict。"""
    _require_content_feature("notes")
    user = getattr(g, "current_user", None)
    if not user:
        abort(401, description="请先登录会员账号。")
    return user


def _note_to_payload(note: dict) -> dict:
    """挑选下发给前端的字段（不泄露 user_id 等内部列）。"""
    return {
        "id": note.get("id"),
        "reader": note.get("reader") or "",
        "book_key": note.get("book_key") or "",
        "book_title": note.get("book_title") or "",
        "volume_label": note.get("volume_label") or "",
        "page": note.get("page"),
        "page_label": note.get("page_label") or "",
        "doc_path": note.get("doc_path") or "",
        "quote": note.get("quote") or "",
        "body": note.get("body") or "",
        "color": note.get("color") or "",
        "source_url": note.get("source_url") or "",
        "created_at": note.get("created_at") or "",
        "updated_at": note.get("updated_at") or "",
        "created_display": _display_datetime(note.get("created_at") or ""),
        "updated_display": _display_datetime(note.get("updated_at") or ""),
    }


@app.get("/api/notes")
def api_notes_list():
    user = _require_notes_access()
    book_key = (request.args.get("book") or "").strip() or None
    query = (request.args.get("q") or "").strip() or None
    try:
        limit = int(request.args.get("limit") or 500)
    except (TypeError, ValueError):
        limit = 500
    try:
        offset = int(request.args.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    rows = list_user_notes(int(user["id"]), book_key=book_key, query=query, limit=limit, offset=offset)
    return jsonify({
        "ok": True,
        "notes": [_note_to_payload(n) for n in rows],
        "total": count_user_notes(int(user["id"]), book_key=book_key),
    })


@app.get("/api/notes/books")
def api_notes_books():
    """「我的知识库」页：按书/卷聚合本用户笔记（书名、卷、条数、最近更新）。"""
    user = _require_notes_access()
    books = list_user_note_books(int(user["id"]))
    for b in books:
        b["last_updated_display"] = _display_datetime(b.get("last_updated") or "")
    return jsonify({"ok": True, "books": books, "total": count_user_notes(int(user["id"]))})


@app.post("/api/notes")
def api_notes_create():
    user = _require_notes_access()
    # 写入限流：按用户，10 分钟最多 120 条（正常记笔记远低于此，仅拦脚本刷写）。
    _rate_limit_or_abort(f"notes:{user['id']}", limit=120, window_seconds=600,
                         message="记笔记过于频繁，请稍后再试。")
    data = request.get_json(silent=True) or {}
    reader = str(data.get("reader") or "").strip().lower()
    if reader not in ("viewer", "liushi", "wenku"):
        abort(400, description="缺少有效的阅读器标识。")
    book_key = str(data.get("book_key") or "").strip()
    if not book_key:
        abort(400, description="缺少书籍标识。")
    body = str(data.get("body") or "").strip()
    quote = str(data.get("quote") or "").strip()
    if not body and not quote:
        abort(400, description="笔记内容为空。")
    page_raw = data.get("page")
    try:
        page = int(page_raw) if page_raw not in (None, "") else None
    except (TypeError, ValueError):
        page = None
    try:
        note = create_user_note(
            user_id=int(user["id"]),
            reader=reader,
            book_key=book_key,
            book_title=str(data.get("book_title") or ""),
            volume_label=str(data.get("volume_label") or ""),
            page=page,
            page_label=str(data.get("page_label") or ""),
            doc_path=str(data.get("doc_path") or ""),
            anchor_text=str(data.get("anchor_text") or ""),
            quote=quote,
            body=body,
            color=str(data.get("color") or ""),
            source_url=str(data.get("source_url") or ""),
        )
    except ValueError as exc:
        abort(400, description=str(exc))
    return jsonify({"ok": True, "note": _note_to_payload(note)})


@app.patch("/api/notes/<int:note_id>")
def api_notes_update(note_id: int):
    user = _require_notes_access()
    data = request.get_json(silent=True) or {}
    body = data.get("body")
    color = data.get("color")
    if body is None and color is None:
        abort(400, description="没有可更新的内容。")
    try:
        note = update_user_note(
            int(note_id), int(user["id"]),
            body=(None if body is None else str(body)),
            color=(None if color is None else str(color)),
        )
    except ValueError as exc:
        abort(400, description=str(exc))
    if note is None:
        abort(404, description="笔记不存在或无权修改。")
    return jsonify({"ok": True, "note": _note_to_payload(note)})


@app.delete("/api/notes/<int:note_id>")
def api_notes_delete(note_id: int):
    user = _require_notes_access()
    ok = delete_user_note(int(note_id), int(user["id"]))
    if not ok:
        abort(404, description="笔记不存在或无权删除。")
    return jsonify({"ok": True, "deleted": True})


@app.route("/knowledge-base")
def knowledge_base_page():
    """会员专属「我的知识库」：跨书聚合本人全部笔记，可搜索 / 跳回原文 / 导出。
    二级页（自带导航、不套 v2 外壳）。未登录 → 登录；登录无权限 → 套餐页。"""
    _require_content_feature("notes")
    return render_template(
        "knowledge_base.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
    )


@app.route("/api/reader/find")
def api_reader_find():
    """阅读器「查找本书」：在当前著作（单卷 PDF）内查词句。只读已加载的语料库 pages，
    按归一化文本匹配（与检索一致，容标点/空白差异），返回命中页码 + 片段，前端据此跳转并高亮。"""
    _require_reader_asset_access()
    # 每请求都全卷扫描（O(全卷字符数)），单卷可达千页：按阅读 IP 限流，防 bot 用廉价请求把 worker 钉死。
    _rate_limit_reader_ip_or_abort("view")
    source_file = _normalize_source_file((request.args.get("file") or "").strip())
    query = (request.args.get("q") or "").strip()[:80]  # 上限 80 字：超长查询无意义，顺带封住极端输入
    if not source_file or source_file not in ALLOWED_SOURCE_FILES:
        abort(404, description="请求的资料不在白名单中。")
    nq = normalize(query) if query else ""
    # 归一化后至少 2 字才查：单字（尤其高频汉字）会让全卷扫描退化为最坏情况（海量命中 + 片段抽取）。
    if corpus is None or len(nq) < 2:
        return jsonify({"ok": True, "matches": [], "total": 0, "pages": 0, "truncated": False})
    volume = corpus.get_volume_by_source_file(source_file)
    if volume is None:
        abort(404, description="未找到对应的卷册信息。")
    MAX_MATCHES = 500
    matches: list[dict] = []
    total = 0
    for p in volume.pages:
        cnt = p.norm_text.count(nq) if p.norm_text else 0
        if not cnt:
            continue
        total += cnt
        if len(matches) < MAX_MATCHES:
            matches.append({
                "pdf_page": p.pdf_page,
                "page_label": p.printed_page or f"PDF-{p.pdf_page}",
                "count": cnt,
                "snippet": _reader_find_snippet(p.raw_text, query),
            })
    return jsonify({
        "ok": True,
        "matches": matches,
        "total": total,
        "pages": len(matches),
        "truncated": len(matches) >= MAX_MATCHES,
    })


def _attach_viewer_payload(
    hit: dict,
    q_for_viewer: str,
    viewer_allowed: bool,
    highlight_override: str | None = None,
) -> dict:
    hit.update(_book_payload(str(hit.get("book") or "")))
    # 同段多词：context 里有多处 [[H]]，_hit_highlight_text 只会取第一处而漏掉其它关键词；
    # 此时直接用空格分隔的关键词串，阅读器 _highlight_terms 会逐词在页图上高亮。
    highlight_text = highlight_override if highlight_override else _hit_highlight_text(hit, q_for_viewer)
    hit["highlight_text"] = highlight_text
    printed_pages = [
        page for page in hit.get("printed_pages", []) if page and not str(page).startswith("pre-")
    ]
    printed_label = printed_pages[0] if printed_pages else ""
    pdf_pages = hit.get("pdf_pages") or [1]
    hit["viewer_url"] = (
        url_for(
            "pdf_viewer",
            file=hit["source_file"],
            page=pdf_pages[0],
            q=q_for_viewer,
            h=highlight_text,
            section=hit.get("section_title") or "",
            printed=printed_label,
        )
        if viewer_allowed
        else ""
    )
    hit["viewer_available"] = viewer_allowed
    return hit


RESEARCH_REVIEW_PASSAGE_MAX_CHARS = 900
RESEARCH_REVIEW_PASSAGE_MIN_CHARS = 280


def _plain_hit_context(hit: dict) -> str:
    return " ".join(
        str(hit.get("context") or "").replace("[[H]]", "").replace("[[/H]]", "").split()
    )


# 中文字符/中文标点集合（含全角形式、书名号引号、间隔号），用于判断一处空白是不是「PDF 折行」留下的。
_CJK_CHARS = r"·‘-”‥…　-〿㐀-䶿一-鿿豈-﫿！-￮"
_CJK_LINE_JOIN_RE = re.compile(rf"(?<=[{_CJK_CHARS}])[ \t]+(?=[{_CJK_CHARS}])")


def _squeeze_cjk_line_joins(text: str) -> str:
    """合并中文句内因「PDF 按物理行抽取」而多出的空格。

    语料 raw_text 每个印刷行一个 ``\\n``，归一化成展示/注入文本时行末换行变成空格，于是一句中文会被
    切成「……人的本质不是 单个人所固有的抽象物……」。中文句内本不该有空格：这类空格既让引文卡片看着
    断续，也会被模型逐字照引进正文。仅当空白两侧都是中文字符/中文标点时才合并，中英文之间、数字与
    单位之间的空格一律保留。
    """
    return _CJK_LINE_JOIN_RE.sub("", str(text or ""))


def _trim_hit_context_to_sentences(context: str) -> str:
    """把语料命中的展示上下文修到「完整句子」：丢掉首尾不含高亮的半句，保留其间的完整句与高亮所在句。
    保留 [[H]]/[[/H]] 高亮标记；无句末标点或无法判断时原样返回（宁可多留也不切碎）。
    与研究综述侧的 _expand_to_sentence_bounds 不同：这里的窗口已由语料固定、无更长原文可扩，故只作
    「向内裁掉半句」而非「向外补全」。"""
    raw = str(context or "").strip()
    if not raw:
        return raw
    enders = set("。！？；;!?")
    hs = raw.find("[[H]]")
    if hs < 0:
        hs = 0
    he_marker = raw.find("[[/H]]")
    he = (he_marker + len("[[/H]]")) if he_marker >= 0 else hs
    ender_positions = [i for i, ch in enumerate(raw) if ch in enders]
    if not ender_positions:
        return raw
    first_ender, last_ender = ender_positions[0], ender_positions[-1]
    # 高亮在首个句末标点之后 → 丢掉开头那半句；否则从头保留（高亮就在首句里）。
    start = first_ender + 1 if hs > first_ender else 0
    # 高亮在最后一个句末标点之前 → 丢掉结尾那半句；否则保留到末尾（高亮延伸进末句）。
    stop = last_ender + 1 if he <= last_ender + 1 else len(raw)
    # 越界护栏：绝不裁进高亮本身。
    start = min(start, hs)
    stop = max(stop, he)
    return raw[start:stop].strip()


# 快速回答不能只依赖模型“自觉”把半句补齐：模型看到的接地材料本身必须是严格按句界切出的。
# 这里只把 。！？（及半角 !?）视为完整句末；分号仍属于同一句内部，避免把复句误切成半句。
_CHAT_SENTENCE_RE = re.compile(r'[^。！？!?]*[。！？!?]+[”’」』）》】）)]*')


def _chat_complete_sentence_spans(text: str) -> list[tuple[int, int, str]]:
    """返回真正以句末标点收尾的句子及其位置；页尾无标点残片不会进入结果。"""
    source = _squeeze_cjk_line_joins(" ".join(str(text or "").split()))
    if not source:
        return []
    out: list[tuple[int, int, str]] = []
    for match in _CHAT_SENTENCE_RE.finditer(source):
        sentence = match.group(0).strip()
        if sentence:
            leading = len(match.group(0)) - len(match.group(0).lstrip())
            out.append((match.start() + leading, match.end(), sentence))
    return out


def _chat_complete_sentences(text: str) -> list[str]:
    return [sentence for _start, _stop, sentence in _chat_complete_sentence_spans(text)]


def _chat_anchor_sentence(sentences: list[str], anchor: str) -> str:
    """在完整句清单中找包含命中高亮的那一句；只做逐字归一匹配，不凭相似度猜引文。"""
    needle = normalize(anchor)
    if len(needle) < 2:
        return ""
    matches = [sentence for sentence in sentences if needle in normalize(sentence)]
    if not matches:
        return ""
    # 同一短语在相邻页重复时，较短的句子通常是实际命中句，避免把页眉/串页文本一并选中。
    return min(matches, key=lambda sentence: (len(normalize(sentence)), len(sentence)))


def _chat_grounding_source_text(hit_obj, hit_payload: dict) -> tuple[str, tuple[int, int]]:
    """按真实页序取原文，并返回命中页在拼接文本中的范围，避免常见词把中心句选到相邻页。"""
    source_file = str(hit_payload.get("source_file") or "")
    pdf_pages = [int(p) for p in (hit_payload.get("pdf_pages") or []) if str(p).isdigit()]
    raw_pages: list[tuple[int, str]] = []

    if corpus and source_file and pdf_pages:
        try:
            volume = corpus.get_volume_by_source_file(source_file)
        except Exception:
            volume = None
        if volume:
            page_to_index = {int(p.pdf_page): idx for idx, p in enumerate(volume.pages)}
            page_idx = page_to_index.get(pdf_pages[0])
            if page_idx is not None:
                for idx in range(max(0, page_idx - 1), min(len(volume.pages), page_idx + 2)):
                    raw = str(getattr(volume.pages[idx], "raw_text", "") or "")
                    if raw.strip():
                        raw_pages.append((int(getattr(volume.pages[idx], "pdf_page", 0) or 0), raw))

    if not raw_pages:
        pages = sorted(
            (getattr(hit_obj, "pages", []) or []),
            key=lambda page: int(getattr(page, "pdf_page", 0) or 0),
        )
        raw_pages = [
            (int(getattr(page, "pdf_page", 0) or 0), str(getattr(page, "raw_text", "") or ""))
            for page in pages
            if str(getattr(page, "raw_text", "") or "").strip()
        ]

    pieces: list[str] = []
    focus = (0, 0)
    target_page = pdf_pages[0] if pdf_pages else (raw_pages[0][0] if raw_pages else 0)
    cursor = 0
    for page_no, raw in raw_pages:
        piece = _squeeze_cjk_line_joins(" ".join(raw.split()))
        if not piece:
            continue
        if pieces:
            cursor += 1  # 页间拼接的一个空格；中文两侧时在最终 squeeze 中会消失，范围只作近似排序。
        start = cursor
        pieces.append(piece)
        cursor += len(piece)
        if page_no == target_page:
            focus = (start, cursor)
    source = _squeeze_cjk_line_joins(" ".join(pieces))
    if focus == (0, 0):
        focus = (0, len(source))
    # squeeze 可能移除页缝空格，最多带来 1-2 字偏差；仅用于“是否靠近命中页”的排序，不用于切片。
    return source, focus


def _chat_grounding_passage_and_key(hit_obj, hit_payload: dict, topic: str) -> tuple[str, str]:
    """返回快速回答的完整句窗口及实际中心句去重键；绝不按相邻的同词句误去重。"""
    anchor = _hit_highlight_text(hit_payload, "")
    source, focus = _chat_grounding_source_text(hit_obj, hit_payload)
    sentence_spans = _chat_complete_sentence_spans(source)
    sentences = [sentence for _start, _stop, sentence in sentence_spans]

    if not sentences:
        fallback = _squeeze_cjk_line_joins(
            _plain_hit_context({"context": _trim_hit_context_to_sentences(str(hit_payload.get("context") or ""))})
        )
        sentences = _chat_complete_sentences(fallback)
        source = fallback
        focus = (0, len(source))
        sentence_spans = _chat_complete_sentence_spans(source)

    if not sentences:
        # 极少数 OCR 页完全没有句末标点。保留原材料而不是让该条引用消失；最终正文校验只会修正
        # 能与原文精确匹配的引文，不会臆造标点或内容。
        return source or _plain_hit_context(hit_payload), ""

    needle = normalize(anchor)
    matching = [
        (idx, start, stop, sentence)
        for idx, (start, stop, sentence) in enumerate(sentence_spans)
        if len(needle) >= 2 and needle in normalize(sentence)
    ]
    if matching:
        focus_mid = (focus[0] + focus[1]) / 2
        center = min(
            matching,
            key=lambda item: (
                0 if item[1] < focus[1] and item[2] > focus[0] else 1,
                abs(((item[1] + item[2]) / 2) - focus_mid),
                len(normalize(item[3])),
            ),
        )[0]
    else:
        # 高亮可能跨 OCR 行而无法逐字命中；沿用研究综述已有的锚点评分选句，但只在已经确认
        # 以句末标点收尾的句子中选择，保证材料边界完整。
        center = _best_review_unit_index(sentences, [anchor, topic])
        center = max(0, center)

    selected = [sentences[center]]
    left, right = center - 1, center + 1
    while len("".join(selected)) < RESEARCH_REVIEW_PASSAGE_MIN_CHARS and (left >= 0 or right < len(sentences)):
        added = False
        if right < len(sentences):
            candidate = "".join(selected + [sentences[right]])
            if len(candidate) <= CHAT_GROUNDING_CONTEXT_CHARS:
                selected.append(sentences[right])
                added = True
            right += 1
        if len("".join(selected)) >= RESEARCH_REVIEW_PASSAGE_MIN_CHARS:
            break
        if left >= 0:
            candidate = "".join([sentences[left]] + selected)
            if len(candidate) <= CHAT_GROUNDING_CONTEXT_CHARS:
                selected.insert(0, sentences[left])
                added = True
            left -= 1
        if not added and left < 0 and right >= len(sentences):
            break

    # 单个原文长句即使超过软上限也必须完整保留；宁可多几十字，也不能再次制造半句。
    passage = "".join(selected).strip()
    center_key = normalize(sentences[center])
    return passage, center_key if len(center_key) >= 12 else ""


def _chat_grounding_passage_text(hit_obj, hit_payload: dict, topic: str) -> str:
    """兼容既有调用：仅返回快速回答的完整句窗口。"""
    return _chat_grounding_passage_and_key(hit_obj, hit_payload, topic)[0]


def _sentence_chunks(text: str) -> list[str]:
    chunks = re.findall(r"[^。！？；;!?]+[。！？；;!?]?", text)
    return [c.strip() for c in chunks if c and c.strip()]


def _trim_review_passage_to_sentence(text: str, max_chars: int = RESEARCH_REVIEW_PASSAGE_MAX_CHARS) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind(stop) for stop in ("。", "！", "？", "；", ";", "!", "?"))
    if cut >= max(180, max_chars // 2):
        return head[: cut + 1].strip()
    return head.strip()


def _best_review_unit_index(units: list[str], anchors: list[str]) -> int:
    if not units:
        return -1
    norm_anchors = [normalize(a) for a in anchors if normalize(a)]
    if not norm_anchors:
        return 0
    best_idx = 0
    best_score = -1
    for idx, unit in enumerate(units):
        unit_norm = normalize(unit)
        score = 0
        for anchor in norm_anchors:
            if anchor and anchor in unit_norm:
                score += 1000 + len(anchor)
            else:
                aset = set(anchor)
                score += len(aset & set(unit_norm))
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx


def _window_review_units(units: list[str], center_idx: int) -> str:
    if not units:
        return ""
    center_idx = max(0, min(center_idx, len(units) - 1))
    selected = [units[center_idx]]
    left = center_idx - 1
    right = center_idx + 1
    while len(" ".join(selected)) < RESEARCH_REVIEW_PASSAGE_MIN_CHARS and (left >= 0 or right < len(units)):
        if right < len(units):
            candidate = " ".join(selected + [units[right]])
            if len(candidate) <= RESEARCH_REVIEW_PASSAGE_MAX_CHARS:
                selected.append(units[right])
            right += 1
        if len(" ".join(selected)) >= RESEARCH_REVIEW_PASSAGE_MIN_CHARS:
            break
        if left >= 0:
            candidate = " ".join([units[left]] + selected)
            if len(candidate) <= RESEARCH_REVIEW_PASSAGE_MAX_CHARS:
                selected.insert(0, units[left])
            left -= 1
    return _trim_review_passage_to_sentence(" ".join(selected))


def _research_review_passage_text(hit_obj, hit_payload: dict, topic: str) -> str:
    """Extract a complete sentence/paragraph window for review writing, not the short UI highlight context."""
    context_plain = _plain_hit_context(hit_payload)
    highlighted = _hit_highlight_text(hit_payload, "")
    anchors = [highlighted, context_plain[:120], topic]

    raw_pages: list[str] = []
    for page in getattr(hit_obj, "pages", []) or []:
        raw = str(getattr(page, "raw_text", "") or "")
        if raw.strip():
            raw_pages.append(raw)

    source_file = str(hit_payload.get("source_file") or "")
    pdf_pages = [int(p) for p in (hit_payload.get("pdf_pages") or []) if str(p).isdigit()]
    if corpus and source_file and pdf_pages:
        try:
            volume = corpus.get_volume_by_source_file(source_file)
        except Exception:
            volume = None
        if volume:
            page_to_index = {int(p.pdf_page): idx for idx, p in enumerate(volume.pages)}
            for pdf_page in pdf_pages[:1]:
                idx = page_to_index.get(pdf_page)
                if idx is None:
                    continue
                for j in range(max(0, idx - 1), min(len(volume.pages), idx + 2)):
                    raw = str(volume.pages[j].raw_text or "")
                    if raw.strip() and raw not in raw_pages:
                        raw_pages.append(raw)

    raw_text = "\n\n".join(raw_pages)
    paragraphs = [
        " ".join(part.split())
        for part in re.split(r"\n\s*\n+", raw_text)
        if len(" ".join(part.split())) >= 40
    ]

    if paragraphs:
        idx = _best_review_unit_index(paragraphs, anchors)
        chosen = paragraphs[idx] if idx >= 0 else paragraphs[0]
        if len(chosen) > RESEARCH_REVIEW_PASSAGE_MAX_CHARS:
            sentences = _sentence_chunks(chosen)
            if sentences:
                return _window_review_units(sentences, _best_review_unit_index(sentences, anchors))
        return _window_review_units(paragraphs, idx)

    fallback_sentences = _sentence_chunks(context_plain)
    if fallback_sentences:
        return _window_review_units(fallback_sentences, _best_review_unit_index(fallback_sentences, anchors))
    return _trim_review_passage_to_sentence(context_plain or highlighted)


# 综述正文里「逐字引用」的片段：抓各种引号内的内容（中文「」『』""，英文 ""）。
_REVIEW_QUOTE_RE = re.compile(r'[“"「『]([^“”"」』\n]{4,}?)[”"」』]')
_REVIEW_REF_RE = re.compile(r"\[([0-9][0-9\s,，、;；]*)\]")


def _extract_review_quotes(review_md: str) -> list[str]:
    """综述正文中被引号包起来的逐字引文；长引文优先（更具体、便于精确定位高亮）。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _REVIEW_QUOTE_RE.findall(review_md or ""):
        s = " ".join(str(raw).split())
        key = normalize(s)
        if len(key) >= 4 and key not in seen:
            seen.add(key)
            out.append(s)
    out.sort(key=len, reverse=True)
    return out


def _review_ref_indices(text: str) -> set[int]:
    indices: set[int] = set()
    for raw in _REVIEW_REF_RE.findall(text or ""):
        for part in re.split(r"[\s,，、;；]+", raw):
            if part.isdigit():
                indices.add(int(part))
    return indices


def _strip_review_refs(text: str) -> str:
    text = re.sub(r"(?m)^#{1,6}\s*", "", text or "")
    text = _REVIEW_REF_RE.sub("", text)
    return " ".join(text.split())


def _review_cited_units(review_md: str) -> dict[int, list[str]]:
    """Return review sentence/paragraph units keyed by the [N] source numbers they cite."""
    by_index: dict[int, list[str]] = {}
    text = str(review_md or "").replace("\r\n", "\n").replace("\r", "\n")
    for match in _REVIEW_REF_RE.finditer(text):
        refs = _review_ref_indices(match.group(0))
        if not refs:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        unit = " ".join(text[line_start:line_end].split())
        cleaned = _strip_review_refs(unit)
        if not cleaned:
            continue
        for idx in refs:
            items = by_index.setdefault(idx, [])
            if cleaned not in items:
                items.append(cleaned)
    return by_index


def _review_quotes_by_index(review_md: str) -> dict[int, list[str]]:
    """Group quoted review text by the nearest source numbers cited after the quote."""
    by_index: dict[int, list[str]] = {}
    seen_by_index: dict[int, set[str]] = {}
    text = str(review_md or "")
    for match in _REVIEW_QUOTE_RE.finditer(text):
        q = " ".join(match.group(1).split())
        key = normalize(q)
        if len(key) < 4:
            continue
        following = text[match.end(): match.end() + 120]
        boundary = re.search(r"[\n。！？]", following)
        window = following[: boundary.end()] if boundary else following
        refs = _review_ref_indices(window)
        if not refs:
            preceding = text[max(0, match.start() - 80): match.start()]
            boundary_pos = max(preceding.rfind("。"), preceding.rfind("！"), preceding.rfind("？"), preceding.rfind("\n"))
            refs = _review_ref_indices(preceding[boundary_pos + 1:])
        for idx in refs:
            seen = seen_by_index.setdefault(idx, set())
            if key in seen:
                continue
            seen.add(key)
            by_index.setdefault(idx, []).append(q)
    for quotes in by_index.values():
        quotes.sort(key=len, reverse=True)
    return by_index


def _normalized_span_in_text(text: str, needle: str) -> str:
    """Find a normalized needle in text and return the original text span."""
    nneedle = normalize(needle)
    if not text or len(nneedle) < 4:
        return ""
    chars: list[str] = []
    index_map: list[int] = []
    for pos, ch in enumerate(text):
        nch = normalize(ch)
        if not nch:
            continue
        chars.append(nch)
        index_map.extend([pos] * len(nch))
    haystack = "".join(chars)
    found = haystack.find(nneedle)
    if found < 0 or found + len(nneedle) - 1 >= len(index_map):
        return ""
    start = index_map[found]
    end = index_map[found + len(nneedle) - 1] + 1
    return text[start:end]


def _char_bigrams(text: str) -> set[str]:
    norm = normalize(text)
    if len(norm) < 2:
        return set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _best_review_cited_sentence_in_passage(passage_text: str, cited_units: list[str]) -> str:
    """For paraphrased [N] citations, pick the source sentence most tied to the cited review sentence."""
    if not passage_text or not cited_units:
        return ""
    source_sentences = [s for s in _sentence_chunks(passage_text) if len(normalize(s)) >= 8]
    if not source_sentences:
        return ""
    best_sentence = ""
    best_score = 0.0
    for unit in cited_units:
        unit_bigrams = _char_bigrams(_strip_review_refs(unit))
        if len(unit_bigrams) < 4:
            continue
        for sentence in source_sentences:
            sent_bigrams = _char_bigrams(sentence)
            if not sent_bigrams:
                continue
            overlap = len(unit_bigrams & sent_bigrams)
            score = overlap / max(1, min(len(unit_bigrams), len(sent_bigrams)))
            if overlap >= 6 and score > best_score:
                best_score = score
                best_sentence = sentence
    return best_sentence if best_score >= 0.18 else ""


def _review_used_span_in_passage(passage_text: str, quotes: list[str]) -> str:
    """返回 passage 文本中被综述逐字引用到的最长真实子串（用于原样高亮）；无则空。

    先原样匹配；标点/全半角差异时用引文前缀近似定位，确保「亮标」落在综述真正用到的句子上。
    """
    if not passage_text or not quotes:
        return ""
    norm_passage = normalize(passage_text)
    for q in quotes:  # 已按长度降序
        if q and q in passage_text:
            return q
    for q in quotes:
        if len(normalize(q)) < 6 or normalize(q) not in norm_passage:
            continue
        normalized_span = _normalized_span_in_text(passage_text, q)
        if normalized_span:
            return normalized_span
        head = q[: min(len(q), 14)].strip()
        pos = passage_text.find(head) if head else -1
        if pos >= 0:
            return passage_text[pos: pos + len(q)]
    return ""


def _hit_first_page_index(hit_obj, volume) -> int:
    hit_pages = getattr(hit_obj, "pages", []) or []
    if hit_pages:
        pdf_page = int(getattr(hit_pages[0], "pdf_page", 1) or 1)
        for idx, page in enumerate(getattr(volume, "pages", []) or []):
            if int(getattr(page, "pdf_page", 0) or 0) == pdf_page:
                return idx
    return 0


def _hit_volume(hit_obj):
    if not corpus:
        return None
    source_file = str(getattr(hit_obj, "source_file", "") or "")
    if not source_file:
        return None
    try:
        return corpus.get_volume_by_source_file(source_file)
    except Exception:
        return None


def _page_raw_text(page) -> str:
    return " ".join(str(getattr(page, "raw_text", "") or "").split())


def _volume_page_evidence(volume, page_idx: int, span: str, source_text: str, *, kind: str, quote: str = "") -> dict | None:
    pages = getattr(volume, "pages", []) or []
    if not (0 <= page_idx < len(pages)):
        return None
    page = pages[page_idx]
    pdf_page = int(getattr(page, "pdf_page", 1) or 1)
    source_file = str(getattr(volume, "source_file", "") or "")
    citation = ""
    citations: dict = {}
    try:
        if corpus:
            citation = corpus._make_citation(volume.book, volume.volume, [page], source_file=source_file)
            citations = corpus._make_citations(volume.book, volume.volume, [page], source_file=source_file)
    except Exception:
        citation = ""
        citations = {}
    chapter = corpus.get_chapter_for_page(source_file, pdf_page) if corpus and source_file else None
    return {
        "kind": kind,
        "book": str(getattr(volume, "book", "") or ""),
        "volume": int(getattr(volume, "volume", 0) or 0),
        "source_file": source_file,
        "pdf_page": pdf_page,
        "printed_page": str(getattr(page, "printed_page", "") or ""),
        "citation": citation,
        "citations": citations,
        "section_title": chapter.title if chapter else "",
        "source_text": source_text,
        "span": span,
        "quote": quote,
    }


def _text_match_in_book(book_key: str, text: str, *, kind: str, quote: str = "") -> dict | None:
    if not corpus:
        return None
    nt = normalize(text)
    if len(nt) < 6:
        return None
    best: tuple[int, int, int, int, object, int, str] | None = None
    for vol in corpus.books.get(book_key, []) or []:
        start = 0
        while True:
            pos = vol.norm_full.find(nt, start)
            if pos < 0:
                break
            page_idx = vol.page_index_at(pos)
            page = vol.pages[page_idx]
            raw = _page_raw_text(page)
            span = _normalized_span_in_text(raw, text)
            if span:
                printed = str(getattr(page, "printed_page", "") or "")
                prelim_penalty = 1 if printed.startswith("pre-") else 0
                candidate = (
                    prelim_penalty,
                    int(getattr(vol, "volume", 0) or 0),
                    int(getattr(page, "pdf_page", 1) or 1),
                    pos,
                    vol,
                    page_idx,
                    span,
                )
                if best is None or candidate[:4] < best[:4]:
                    best = candidate
            start = pos + len(nt)
    if best is None:
        return None
    vol = best[4]
    page_idx = best[5]
    span = best[6]
    return _volume_page_evidence(vol, page_idx, span, _page_raw_text(vol.pages[page_idx]), kind=kind, quote=quote)


def _prefer_wenji_evidence_for_hit(hit_obj, text: str, *, kind: str, quote: str = "") -> dict | None:
    volume = _hit_volume(hit_obj)
    # 仅限马恩《全集》与马恩《文集》这组同源经典文本；其它文库（列宁、毛选、党代会等）
    # 属于不同文献体系，必须回到自身来源页核验，不能跨库改指《文集》。
    if not volume or getattr(volume, "book", "") != "全集":
        return None
    return _text_match_in_book("文集", text, kind=kind, quote=quote)


def _quote_match_in_hit_volume(hit_obj, quote: str) -> dict | None:
    """Find one direct quote in the hit's full volume and return page-level evidence."""
    preferred = _prefer_wenji_evidence_for_hit(hit_obj, quote, kind="quote", quote=quote)
    if preferred:
        return preferred
    volume = _hit_volume(hit_obj)
    nq = normalize(quote)
    if not volume or len(nq) < 6:
        return None
    hit_idx = _hit_first_page_index(hit_obj, volume)
    best: tuple[int, int, int] | None = None
    start = 0
    while True:
        pos = volume.norm_full.find(nq, start)
        if pos < 0:
            break
        page_idx = volume.page_index_at(pos)
        candidate = (abs(page_idx - hit_idx), page_idx, pos)
        if best is None or candidate < best:
            best = candidate
        start = pos + len(nq)
    if best is None:
        return None
    page_idx = best[1]
    page = volume.pages[page_idx]
    source_text = _page_raw_text(page)
    span = _normalized_span_in_text(source_text, quote)
    if not span:
        left = max(0, page_idx - 1)
        right = min(len(volume.pages), page_idx + 2)
        source_text = "\n".join(_page_raw_text(p) for p in volume.pages[left:right] if _page_raw_text(p))
        span = _normalized_span_in_text(source_text, quote)
    if not span:
        return None
    return _volume_page_evidence(volume, page_idx, span, source_text, kind="quote", quote=quote)


def _span_match_in_hit_volume(hit_obj, span_text: str) -> dict | None:
    """Locate a paraphrased-citation source sentence in the hit's full volume."""
    preferred = _prefer_wenji_evidence_for_hit(hit_obj, span_text, kind="paraphrase")
    if preferred:
        return preferred
    volume = _hit_volume(hit_obj)
    ns = normalize(span_text)
    if not volume or len(ns) < 8:
        return None
    hit_idx = _hit_first_page_index(hit_obj, volume)
    best: tuple[int, int, int] | None = None
    start = 0
    while True:
        pos = volume.norm_full.find(ns, start)
        if pos < 0:
            break
        page_idx = volume.page_index_at(pos)
        candidate = (abs(page_idx - hit_idx), page_idx, pos)
        if best is None or candidate < best:
            best = candidate
        start = pos + len(ns)
    if best is None:
        return None
    page_idx = best[1]
    page = volume.pages[page_idx]
    source_text = _page_raw_text(page)
    span = _normalized_span_in_text(source_text, span_text) or span_text
    return _volume_page_evidence(volume, page_idx, span, source_text, kind="paraphrase")


# 引用展示片段按「完整句子」呈现：把字符窗口向外扩到最近的句末标点，避免掐头去尾断在半句。
_SENTENCE_ENDERS = "。！？；…!?;"


def _expand_to_sentence_bounds(text: str, start: int, stop: int, cap: int = 160) -> tuple[int, int, bool, bool]:
    """把 [start, stop) 向外扩到最近的句子边界（每侧最多扩 cap 字）。

    返回 ``(新start, 新stop, 头部截断, 尾部截断)``：到达真实句边界或文首/文末则该侧「不截断」
    （无省略号）；扩到 cap 仍未遇句末标点，才判为「截断」（由调用方补省略号）。这样引用片段
    尽量落在完整句子上，而非在半句处硬切。
    """
    n = len(text)
    s = start
    while s > 0 and text[s - 1] not in _SENTENCE_ENDERS and (start - s) < cap:
        s -= 1
    lead_cut = s > 0 and text[s - 1] not in _SENTENCE_ENDERS
    e = stop
    while e < n and text[e - 1] not in _SENTENCE_ENDERS and (e - stop) < cap:
        e += 1
    trail_cut = e < n and text[e - 1] not in _SENTENCE_ENDERS
    return s, e, lead_cut, trail_cut


def _review_evidence_context(source_text: str, spans: list[str], window: int = 360) -> str:
    text = " ".join(str(source_text or "").split())
    clean_spans = []
    for span in spans:
        s = " ".join(str(span or "").split())
        if s and s not in clean_spans:
            clean_spans.append(s)
    if not text or not clean_spans:
        return _trim_review_passage_to_sentence(text, window)

    ranges: list[tuple[int, int]] = []
    for span in clean_spans:
        pos = text.find(span)
        if pos < 0:
            found = _normalized_span_in_text(text, span)
            pos = text.find(found) if found else -1
            span = found or span
        if pos >= 0:
            ranges.append((pos, pos + len(span)))
    if not ranges:
        return _trim_review_passage_to_sentence(text, window)
    ranges.sort()
    first, last = ranges[0][0], ranges[-1][1]
    if last - first > window:
        first, last = ranges[0]
    half = max(0, (window - (last - first)) // 2)
    start = max(0, first - half)
    stop = min(len(text), last + half)
    start, stop, lead_cut, trail_cut = _expand_to_sentence_bounds(text, start, stop)
    snippet = text[start:stop]
    adjusted = [(s - start, e - start) for s, e in ranges if start <= s < stop]
    adjusted.sort(reverse=True)
    for s, e in adjusted:
        snippet = snippet[:s] + "[[H]]" + snippet[s:e] + "[[/H]]" + snippet[e:]
    return ("…" if lead_cut else "") + snippet + ("…" if trail_cut else "")


def _make_review_evidence_items(
    hit_obj,
    base: dict,
    plain: str,
    quote_spans: list[str],
    cited_units: list[str],
    viewer_allowed: bool,
    q_for_viewer: str,
) -> tuple[list[dict], dict, str]:
    """Build page-level evidence items for one [N] citation."""
    evidence: list[dict] = []
    unmatched_quotes: list[str] = []
    first_page: int | None = None
    first_highlight = ""

    for quote in quote_spans:
        item = _quote_match_in_hit_volume(hit_obj, quote)
        if item is None:
            span = _review_used_span_in_passage(plain, [quote])
            if span:
                item = {
                    "kind": "quote",
                    "pdf_page": (base.get("pdf_pages") or [None])[0],
                    "printed_page": (base.get("printed_pages") or [""])[0],
                    "source_text": plain,
                    "span": span,
                    "quote": quote,
                }
        if item is None:
            unmatched_quotes.append(quote)
            continue
        evidence.append(item)

    if not quote_spans:
        span = _best_review_cited_sentence_in_passage(plain, cited_units)
        if span:
            item = _span_match_in_hit_volume(hit_obj, span) or {
                "kind": "paraphrase",
                "pdf_page": (base.get("pdf_pages") or [None])[0],
                "printed_page": (base.get("printed_pages") or [""])[0],
                "source_text": plain,
                "span": span,
                "quote": "",
            }
            evidence.append(item)

    grouped: OrderedDict[tuple, dict] = OrderedDict()
    for item in evidence:
        page_key = (item.get("source_file") or base.get("source_file") or "", item.get("pdf_page") or "context")
        group = grouped.setdefault(
            page_key,
            {
                "kind": item.get("kind") or "quote",
                "book": item.get("book") or base.get("book") or "",
                "volume": item.get("volume") or base.get("volume") or "",
                "source_file": item.get("source_file") or base.get("source_file") or "",
                "citation": item.get("citation") or base.get("citation") or "",
                "citations": item.get("citations") or base.get("citations") or {},
                "section_title": item.get("section_title") or base.get("section_title") or "",
                "pdf_page": item.get("pdf_page"),
                "printed_page": item.get("printed_page") or "",
                "source_text": item.get("source_text") or "",
                "spans": [],
                "quotes": [],
            },
        )
        if item.get("source_text") and len(item["source_text"]) > len(group.get("source_text") or ""):
            group["source_text"] = item["source_text"]
        if item.get("span"):
            group["spans"].append(item["span"])
        if item.get("quote"):
            group["quotes"].append(item["quote"])

    out: list[dict] = []
    retarget_base = dict(base)
    for group in grouped.values():
        spans = group.get("spans") or []
        if not spans:
            continue
        page = group.get("pdf_page")
        if first_page is None and page:
            first_page = int(page)
            first_highlight = " ".join(spans[:3])
            retarget_base = dict(base)
            retarget_base.update({
                "book": group.get("book") or base.get("book"),
                "volume": group.get("volume") or base.get("volume"),
                "source_file": group.get("source_file") or base.get("source_file"),
                "citation": group.get("citation") or base.get("citation"),
                "citations": group.get("citations") or base.get("citations") or {},
                "section_title": group.get("section_title") or base.get("section_title"),
                "pdf_pages": [int(page)],
                "printed_pages": [group.get("printed_page") or ""],
            })
        viewer_url = ""
        source_file = group.get("source_file") or base.get("source_file")
        if viewer_allowed and page and source_file:
            viewer_url = url_for(
                "pdf_viewer",
                file=source_file,
                page=page,
                q=q_for_viewer,
                h=" ".join(spans[:3]),
                section=group.get("section_title") or "",
                printed=group.get("printed_page") or "",
            )
        out.append({
            "kind": group.get("kind") or "quote",
            "book": group.get("book") or "",
            "source_file": source_file,
            "citation": group.get("citation") or "",
            "citations": group.get("citations") or {},
            "section_title": group.get("section_title") or "",
            "pdf_page": page,
            "printed_page": group.get("printed_page") or "",
            "context": _review_evidence_context(group.get("source_text") or "", spans),
            "viewer_url": viewer_url,
            "quote_count": len(group.get("quotes") or []),
        })

    return out, retarget_base, first_highlight


def _review_used_span_in_hit_volume(hit_obj, quotes: list[str]) -> tuple[str, str, int | None]:
    """在命中所在卷的较大范围中回捞综述逐字引文；返回 (原文窗口, 命中原文, pdf_page)。"""
    if not quotes or not corpus:
        return "", "", None
    source_file = str(getattr(hit_obj, "source_file", "") or "")
    if not source_file:
        return "", "", None
    try:
        volume = corpus.get_volume_by_source_file(source_file)
    except Exception:
        volume = None
    if not volume:
        return "", "", None

    hit_idx = _hit_first_page_index(hit_obj, volume)
    best: tuple[int, int, str, str, int] | None = None
    for quote in quotes:
        nq = normalize(quote)
        if len(nq) < 6:
            continue
        start = 0
        while True:
            pos = volume.norm_full.find(nq, start)
            if pos < 0:
                break
            page_idx = volume.page_index_at(pos)
            left = max(0, page_idx - 1)
            right = min(len(volume.pages), page_idx + 2)
            source_text = "\n".join(
                " ".join(str(getattr(page, "raw_text", "") or "").split())
                for page in volume.pages[left:right]
                if str(getattr(page, "raw_text", "") or "").strip()
            )
            span = _normalized_span_in_text(source_text, quote)
            if span:
                pdf_page = int(getattr(volume.pages[page_idx], "pdf_page", 1) or 1)
                distance = abs(page_idx - hit_idx)
                # 长引文优先；同长度取离原命中页更近者。
                candidate = (-len(normalize(span)), distance, source_text, span, pdf_page)
                if best is None or candidate < best:
                    best = candidate
            start = pos + len(nq)
    if best:
        return best[2], best[3], best[4]
    return "", "", None


def _retarget_review_hit_payload(base: dict, pdf_page: int | None) -> dict:
    """逐字引文回捞到同卷其它页时，同步引文页码、出处和打开原文链接目标。"""
    if not pdf_page or not corpus:
        return base
    source_file = str(base.get("source_file") or "")
    if not source_file:
        return base
    try:
        volume = corpus.get_volume_by_source_file(source_file)
    except Exception:
        volume = None
    if not volume:
        return base
    page_obj = next((p for p in volume.pages if int(getattr(p, "pdf_page", 0) or 0) == int(pdf_page)), None)
    if not page_obj:
        return base
    out = dict(base)
    out["pdf_pages"] = [int(pdf_page)]
    out["printed_pages"] = [getattr(page_obj, "printed_page", "") or ""]
    try:
        out["citation"] = corpus._make_citation(volume.book, volume.volume, [page_obj], source_file=source_file)
    except Exception:
        pass
    chapter = corpus.get_chapter_for_page(source_file, int(pdf_page)) if corpus else None
    if chapter:
        out["section_title"] = chapter.title
    return out


def _review_citation_context(passage_text: str, span: str, window: int = 320) -> str:
    """以被引用片段为中心裁出带 [[H]] 高亮的展示窗口；无 span 时给纯文本窗口（不强标到别处）。"""
    text = " ".join(str(passage_text or "").split())
    if not text:
        return ""
    pos = text.find(span) if span else -1
    if pos < 0:
        return _trim_review_passage_to_sentence(text, window)
    end = pos + len(span)
    half = max(0, (window - len(span)) // 2)
    start = max(0, pos - half)
    stop = min(len(text), end + half)
    start, stop, lead_cut, trail_cut = _expand_to_sentence_bounds(text, start, stop)
    return (
        ("…" if lead_cut else "")
        + text[start:pos] + "[[H]]" + text[pos:end] + "[[/H]]" + text[end:stop]
        + ("…" if trail_cut else "")
    )


def _build_research_review_fallback(topic: str, passages: list[dict]) -> str:
    """Build a grounded review when the model call fails, using only real retrieved passages."""
    cleaned_topic = " ".join(str(topic or "").split()) or "本次研究论题"
    usable: list[dict] = []
    for item in passages:
        text = " ".join(str((item or {}).get("text") or "").split())
        if not text:
            continue
        usable.append(
            {
                "index": (item or {}).get("index"),
                "citation": " ".join(str((item or {}).get("citation") or "").split()),
                "text": text,
            }
        )
    if not usable:
        return ""

    lines = [
        "## 研究综述",
        "",
        f"围绕“{cleaned_topic}”，本次检索已经在文献库中定位到一组可核对的原文材料。"
        "由于模型长文生成暂时不可用，下面先依据这些真实命中整理一版可阅读的接地综述；"
        "每处判断后面的方括号编号，对应下方可打开核对的引文条。",
        "",
    ]
    for pos, item in enumerate(usable[:6], start=1):
        idx = item["index"] if item["index"] is not None else pos
        citation = item["citation"] or "出处见下方引文条"
        text = item["text"]
        lines.extend(
            [
                f"### 线索 {pos}",
                "",
                f"{citation} 的命中段落显示：{text} [{idx}]",
                "",
            ]
        )

    covered = "、".join(f"[{item['index'] if item['index'] is not None else i}]" for i, item in enumerate(usable[:6], start=1))
    lines.extend(
        [
            "## 小结",
            "",
            f"就现有命中而言，以上材料（{covered}）已经提供了展开该论题的基本出处。"
            "若要继续深化，可优先从这些出处进入原文页面，比较同一概念在不同篇章、卷次和历史语境中的表达差异。",
        ]
    )
    return "\n".join(lines)


def _select_research_review_hits(candidates: list, limit: int = RESEARCH_REVIEW_SOURCES) -> list:
    """研究综述取源：先守住相关度，再在高相关候选里做资料库多样化。"""
    if limit <= 0:
        return []
    try:
        max_score = max(int(getattr(hit, "score", 0) or 0) for hit in (candidates or []))
    except ValueError:
        return []
    relevance_floor = max(60, max_score - 18) if max_score > 0 else 0
    buckets: OrderedDict[str, list] = OrderedDict()
    seen_keys: set[tuple] = set()
    for hit in candidates or []:
        score = int(getattr(hit, "score", 0) or 0)
        if score < relevance_floor:
            continue
        pages = getattr(hit, "pages", []) or []
        first_page = pages[0].pdf_page if pages else -1
        key = (getattr(hit, "book", ""), getattr(hit, "source_file", ""), first_page)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        book = str(getattr(hit, "book", "") or "其他资料")
        buckets.setdefault(book, []).append(hit)

    selected: list = []
    selected_keys: set[tuple] = set()

    def _key(hit) -> tuple:
        pages = getattr(hit, "pages", []) or []
        first_page = pages[0].pdf_page if pages else -1
        return (getattr(hit, "book", ""), getattr(hit, "source_file", ""), first_page)

    # 前两轮给不同资料库各一次机会，适合综述写作中“不同文献支点”的展开；不无限轮转，避免低相关资料挤占篇幅。
    for round_index in range(2):
        for hits in buckets.values():
            if len(selected) >= limit:
                break
            if round_index >= len(hits):
                continue
            hit = hits[round_index]
            key = _key(hit)
            if key in selected_keys:
                continue
            selected.append(hit)
            selected_keys.add(key)
        if len(selected) >= limit:
            break

    # 剩余名额回到全局相关度顺序补齐，保障窄题目或强相关材料不会被过度“平均化”。
    for hit in candidates or []:
        if len(selected) >= limit:
            break
        key = _key(hit)
        if key in selected_keys:
            continue
        selected.append(hit)
        selected_keys.add(key)
    return selected


def _build_volume_chaptered_results(groups: list[dict]) -> list[dict]:
    volumes: OrderedDict[tuple, dict] = OrderedDict()
    for group in groups:
        for hit in group.get("hits", []):
            pdf_pages = hit.get("pdf_pages") or [1]
            first_pdf_page = int(pdf_pages[0] or 1)
            volume_key = (hit.get("book"), hit.get("volume"), hit.get("source_file"))
            volume = volumes.setdefault(
                volume_key,
                {
                    "group_id": f"volume|{hit.get('book')}|{hit.get('volume')}|{hit.get('source_file')}",
                    "book": hit.get("book"),
                    "volume": hit.get("volume"),
                    "source_file": hit.get("source_file"),
                    "display_title": hit.get("display_title"),
                    "book_title": hit.get("book_title"),
                    "book_short_title": hit.get("book_short_title"),
                    "citation_title": hit.get("citation_title"),
                    "book_sort_order": hit.get("book_sort_order", _book_sort_order(str(hit.get("book") or ""))),
                    "count": 0,
                    "chapter_count": 0,
                    "chapters": OrderedDict(),
                    "_first_pdf_page": first_pdf_page,
                },
            )
            volume["count"] += 1
            volume["_first_pdf_page"] = min(volume["_first_pdf_page"], first_pdf_page)

            chapter = corpus.get_chapter_for_page(hit.get("source_file") or "", first_pdf_page) if corpus else None
            chapter_title = (chapter.title if chapter else None) or hit.get("section_title") or "未识别篇章"
            chapter_pdf_page = chapter.pdf_page if chapter else first_pdf_page
            chapter_key = (
                chapter_title,
                chapter_pdf_page,
                chapter.kind if chapter else "",
            )
            chapters: OrderedDict[tuple, dict] = volume["chapters"]
            chapter_payload = chapters.setdefault(
                chapter_key,
                {
                    "chapter_id": "",
                    "section_title": chapter_title,
                    "chapter_pdf_page": chapter_pdf_page,
                    "printed_page": chapter.printed_page if chapter else "",
                    "level": chapter.level if chapter else 1,
                    "kind": chapter.kind if chapter else "",
                    "count": 0,
                    "page_size": 10,
                    "hits": [],
                },
            )
            chapter_payload["count"] += 1
            chapter_payload["hits"].append(hit)

    result = list(volumes.values())
    result.sort(key=lambda item: (item.get("book_sort_order", 9999), item["volume"], item["_first_pdf_page"]))
    for volume in result:
        chapters = list(volume["chapters"].values())
        chapters.sort(key=lambda item: (item["chapter_pdf_page"], item["level"], item["section_title"]))
        for index, chapter in enumerate(chapters, start=1):
            chapter["chapter_id"] = f"{volume['group_id']}|chapter|{index}"
            chapter["hits"].sort(key=lambda item: ((item.get("pdf_pages") or [1])[0], item.get("section_title") or ""))
        volume["chapter_count"] = len(chapters)
        volume["chapters"] = chapters
        volume.pop("_first_pdf_page", None)
    return result


def _bulk_book_counts(book_hit_counts: dict) -> list[dict]:
    """短词完整聚合下，各书库的命中总数（用于结果上方的书库筛选标签）。"""
    out: list[dict] = []
    for cfg in BOOK_CONFIGS:
        cnt = int(book_hit_counts.get(cfg.key) or 0)
        if cnt:
            out.append(
                {
                    "key": cfg.key,
                    "short_title": cfg.short_title,
                    "title": cfg.title,
                    "tag_class": cfg.tag_class,
                    "count": cnt,
                }
            )
    return out


def _bulk_summary_results(volumes: list[dict], requested_group_page: int) -> dict:
    """无正文权限用户：仅按卷/篇章给出准确命中数线索，不返回命中详情。"""
    rows: list[dict] = []
    for volume in volumes:
        for chapter in volume.get("chapters", []):
            rows.append(
                {
                    "group_id": f"summary|{len(rows) + 1}",
                    "book": volume.get("book"),
                    "volume": volume.get("volume"),
                    "display_title": volume.get("display_title"),
                    "book_title": volume.get("book_title"),
                    "book_short_title": volume.get("book_short_title"),
                    "citation_title": volume.get("citation_title"),
                    "book_sort_order": volume.get("book_sort_order"),
                    "section_title": chapter.get("section_title") or "未识别篇章",
                    "count": int(chapter.get("count") or 0),
                    "locked": True,
                    "lock_message": "当前权限仅显示目录线索。请登录或开通相应权限后查看上下文、引文和页码。",
                }
            )
    total_group_pages = max(1, (len(rows) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    group_page = min(max(1, requested_group_page), total_group_pages)
    start = (group_page - 1) * GROUPS_PER_PAGE
    return {
        "results": rows[start : start + GROUPS_PER_PAGE],
        "group_count": len(rows),
        "group_page": group_page,
        "group_pages": total_group_pages,
    }


def _bulk_volume_results(volumes: list[dict], requested_group_page: int) -> dict:
    """有正文权限用户：按卷/篇章给出完整聚合结构，命中详情由前端按需分页拉取。"""
    cards: list[dict] = []
    for volume in volumes:
        chapters: list[dict] = []
        for index, chapter in enumerate(volume.get("chapters", []), start=1):
            chapters.append(
                {
                    "chapter_id": f"{volume.get('book')}|{volume.get('volume')}|{volume.get('source_file')}|chapter|{index}",
                    "section_title": chapter.get("section_title") or "未识别篇章",
                    "chapter_pdf_page": chapter.get("chapter_pdf_page"),
                    "printed_page": chapter.get("printed_page") or "",
                    "level": chapter.get("level") or 1,
                    "kind": chapter.get("kind") or "",
                    "count": int(chapter.get("count") or 0),
                    "page_size": CHAPTER_HITS_PAGE_SIZE,
                    "hits_lazy": True,
                    "hits": [],
                }
            )
        cards.append(
            {
                "group_id": f"{volume.get('book')}|{volume.get('volume')}|{volume.get('source_file')}",
                "book": volume.get("book"),
                "volume": volume.get("volume"),
                "source_file": volume.get("source_file"),
                "display_title": volume.get("display_title"),
                "book_title": volume.get("book_title"),
                "book_short_title": volume.get("book_short_title"),
                "citation_title": volume.get("citation_title"),
                "book_sort_order": volume.get("book_sort_order"),
                "count": int(volume.get("count") or 0),
                "chapter_count": int(volume.get("chapter_count") or len(chapters)),
                "chapters": chapters,
            }
        )
    total_group_pages = max(1, (len(cards) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    group_page = min(max(1, requested_group_page), total_group_pages)
    start = (group_page - 1) * GROUPS_PER_PAGE
    return {
        "results": cards[start : start + GROUPS_PER_PAGE],
        "group_count": len(cards),
        "group_page": group_page,
        "group_pages": total_group_pages,
    }


def _build_summary_search_results(groups: list[dict], requested_group_page: int) -> dict:
    summaries: OrderedDict[tuple, dict] = OrderedDict()
    for group in groups:
        key = (
            group.get("book"),
            group.get("volume"),
            group.get("display_title"),
            group.get("section_title") or "未识别篇章",
        )
        item = summaries.setdefault(
            key,
            {
                "group_id": f"summary|{len(summaries) + 1}",
                "book": group.get("book"),
                "volume": group.get("volume"),
                "display_title": group.get("display_title"),
                "book_title": group.get("book_title") or _book_config(str(group.get("book") or "")).title,
                "book_short_title": group.get("book_short_title") or _book_config(str(group.get("book") or "")).short_title,
                "citation_title": group.get("citation_title") or _book_config(str(group.get("book") or "")).citation_title,
                "book_sort_order": group.get("book_sort_order", _book_sort_order(str(group.get("book") or ""))),
                "section_title": group.get("section_title") or "未识别篇章",
                "count": 0,
                "locked": True,
                "lock_message": "当前权限仅显示目录线索。请登录或开通相应权限后查看上下文、引文和页码。",
            },
        )
        item["count"] += int(group.get("count") or len(group.get("hits") or []) or 0)

    unpaged = list(summaries.values())
    total_group_pages = max(1, (len(unpaged) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    group_page = min(max(1, requested_group_page), total_group_pages)
    start = (group_page - 1) * GROUPS_PER_PAGE
    return {
        "display_mode": "summary",
        "results": unpaged[start : start + GROUPS_PER_PAGE],
        "group_count": len(unpaged),
        "group_page": group_page,
        "group_pages": total_group_pages,
    }


def _search_book_counts(groups: list[dict]) -> list[dict]:
    """统计各书库命中分组数（用于检索结果上方的书库筛选标签）。"""
    counts: dict[str, int] = {}
    for group in groups:
        key = str(group.get("book") or "")
        if key:
            counts[key] = counts.get(key, 0) + 1
    out: list[dict] = []
    for cfg in BOOK_CONFIGS:
        if cfg.key in counts:
            out.append(
                {
                    "key": cfg.key,
                    "short_title": cfg.short_title,
                    "title": cfg.title,
                    "tag_class": cfg.tag_class,
                    "count": counts[cfg.key],
                }
            )
    return out


def _scope_allows(book: str, volume: object, spec: object) -> bool:
    """标准检索后置过滤谓词：(book, volume) 是否落在范围 spec 内。
    spec=None→全放行；dict={书库键:允许卷集|None}→按书+卷（None=该书全卷）；set/list→仅按书库键。"""
    if spec is None:
        return True
    if book not in {str(b) for b in spec}:  # dict 迭代取键，集合/列表取元素
        return False
    if isinstance(spec, dict):
        allowed = spec.get(book)
        if allowed is not None:
            try:
                return int(volume) in allowed
            except (TypeError, ValueError):
                return False
    return True


def _scope_book_echo(spec: object) -> str:
    """把范围 spec 回显成单个书库键（仅当恰好限定「单本整套」时）——供旧书库分栏 tab 高亮；否则空串。"""
    if isinstance(spec, dict) and len(spec) == 1:
        (key, vols), = spec.items()
        if vols is None:
            return key
    return ""


def _standard_search_scope(payload: dict) -> object:
    """标准检索的检索范围：优先取新版 ``scope``（book:/vol:/著作群 token 列表，经 _resolve_search_scope
    统一解析为 set/dict/None），无则回落旧版单个 ``book``（→{book:None}），再无则 None（不限定，全库）。"""
    raw = payload.get("scope")
    if raw not in (None, "", [], (), {}):
        spec, _sid, _manual = _resolve_search_scope(raw, "", {})
        return spec
    book = str(payload.get("book") or "").strip()
    if book in BOOK_CONFIG_BY_KEY:
        return {book: None}
    return None


def _chaptered_search_payload(q, scope_spec, requested_group_page, viewer_allowed, user):
    """海量命中专用：完整聚合全部卷/篇章的准确命中数（C 层级计数），命中详情由
    /api/search/chapter-hits 按需分页物化——既“全部呈现”又不一次性物化海量命中拖垮服务。
    短词与“长词但单库命中超 EXACT_HITS_PER_BOOK 会被分组路径截断”的情形共用此通道，
    从而彻底消除 200 条/库 的截断。命中不足阈值则返回 None，交由常规分组/直出路径处理。"""
    try:
        agg = corpus.search_chaptered(q)
    except Exception as exc:
        LOGGER.warning(
            "Chaptered aggregation failed for query=%r user=%s: %s",
            q[:80], user.get("id") if user else "guest", exc,
        )
        return None
    if not agg or agg["total_hits"] <= DIRECT_RESULTS_THRESHOLD:
        return None
    book_counts = _bulk_book_counts(agg["book_hit_counts"])
    volumes = agg["volumes"]
    if scope_spec is not None:
        volumes = [v for v in volumes
                   if _scope_allows(str(v.get("book") or ""), v.get("volume"), scope_spec)]
    book_filter = _scope_book_echo(scope_spec)  # 回显单本整套（供旧分栏高亮），多本/卷级/群→空串
    effective_total_hits = sum(int(v.get("count") or 0) for v in volumes)
    if not viewer_allowed:
        summary = _bulk_summary_results(volumes, requested_group_page)
        return {
            "ok": True, "query": agg["query"], "count": effective_total_hits,
            "group_count": summary["group_count"], "group_page": summary["group_page"],
            "group_pages": summary["group_pages"], "groups_per_page": GROUPS_PER_PAGE,
            "truncated": False, "display_mode": "summary", "access_level": "summary",
            "results": summary["results"], "pdf_enabled": False,
            "book_counts": book_counts, "book_filter": book_filter,
        }
    bulk = _bulk_volume_results(volumes, requested_group_page)
    return {
        "ok": True, "query": agg["query"], "count": effective_total_hits,
        "group_count": bulk["group_count"], "group_page": bulk["group_page"],
        "group_pages": bulk["group_pages"], "groups_per_page": GROUPS_PER_PAGE,
        "truncated": False, "display_mode": "volume_chaptered", "access_level": "full",
        "results": bulk["results"], "pdf_enabled": viewer_allowed,
        "book_counts": book_counts, "book_filter": book_filter,
    }


@app.route("/api/search", methods=["POST"])
def api_search():
    _require_search()
    _require_feature("search")
    user = getattr(g, "current_user", None)
    rate_key = f"search:user:{user['id']}" if user else f"search:guest:{_client_ip()}"
    rate_name = "search_user" if user else "search_guest"
    if not _is_admin_user(user):
        _rate_limit_or_abort(
            rate_key,
            limit=RATE_LIMITS[rate_name][0],
            window_seconds=RATE_LIMITS[rate_name][1],
            message="检索请求过于频繁，请稍后再试。",
        )
    payload = request.get_json(silent=True) or {}
    q = (payload.get("q") or "").strip()
    try:
        requested_group_page = max(1, int(payload.get("group_page") or 1))
    except (TypeError, ValueError):
        requested_group_page = 1
    q_norm = normalize(q)
    if not q:
        return jsonify({"ok": False, "error": "查询内容不能为空。"}), 400
    if len(q_norm) < 2:
        return jsonify({"ok": False, "error": "请至少输入两个有效字符再检索。"}), 400
    q_for_viewer = " ".join(q.split())
    state = current_view_state()
    viewer_allowed = bool(state["pdf_enabled"] and _content_access_enabled("viewer"))

    # 检索范围（标准检索·后置过滤）：新版前端「指定著作/卷」发 scope（book:/vol:/著作群 token 列表），
    # 旧版书库分栏 tab 发单个 book。统一解析为范围 spec；D3=book_counts 仍全库统计、结果再按 spec 过滤。
    scope_spec = _standard_search_scope(payload)
    book_filter = _scope_book_echo(scope_spec)  # 回显：单本整套→书库键（保旧分栏高亮），否则空串

    # 同段多词检索（标准检索的「同段多词」开关）：把输入拆成多个关键词，定位全部词共现于
    # 邻近段落的真实命中。纯子串/共现，与单子串的篇章聚合通道不兼容，故下面两个聚合分支均跳过。
    mode = str(payload.get("mode") or "").strip().lower()
    cooc = mode in {"cooccurrence", "cooc", "multi"}
    cooc_keywords: list[str] = []
    if cooc:
        cooc_keywords = _split_cooc_keywords(q)
        if len(cooc_keywords) < 2:
            return jsonify({
                "ok": False,
                "error": "同段多词检索请输入两个及以上关键词（用空格分隔）。",
            }), 400

    # 短词海量命中专用通道：完整聚合全部卷/篇章的准确命中数（C 层级计数，约 0.1 秒），
    # 命中详情交由 /api/search/chapter-hits 按需分页物化，从而“全部呈现”又不拖垮服务。
    if not cooc and not DEPLOYMENT.is_desktop and len(q_norm) <= SHORT_QUERY_CHAPTER_MAX_LEN:
        payload_chaptered = _chaptered_search_payload(
            q, scope_spec, requested_group_page, viewer_allowed, user
        )
        if payload_chaptered is not None:
            return jsonify(payload_chaptered)

    try:
        if cooc:
            grouped = corpus.search_cooccurrence_grouped(cooc_keywords, group_limit=1000000)
        else:
            grouped = corpus.search_grouped(q, group_limit=1000000, max_hits=None)
    except Exception as exc:
        LOGGER.warning("Search failed for query=%r user=%s: %s", q[:80], user.get("id") if user else "guest", exc)
        return jsonify({"ok": False, "error": "查询解析失败，请调整关键词后重试。"}), 400

    # 长词海量命中：常规分组路径会按 EXACT_HITS_PER_BOOK(200/库) 截断；一旦发生截断，
    # 改走与短词相同的“完整篇章聚合”通道——给出全部卷/篇章的完整命中计数，详情按需展开，
    # 从而彻底消除 200 条/库 的截断、命中全部可达（不一次性物化以保稳定）。
    if not cooc and not DEPLOYMENT.is_desktop and grouped.get("truncated"):
        payload_chaptered = _chaptered_search_payload(
            q, scope_spec, requested_group_page, viewer_allowed, user
        )
        if payload_chaptered is not None:
            return jsonify(payload_chaptered)

    # 同段多词：context 含多处高亮，阅读器高亮用空格分隔的关键词串逐词命中。
    cooc_highlight = q_for_viewer if cooc else None
    all_groups = []
    for group in grouped["groups"]:
        hits = [
            _attach_viewer_payload(hit, q_for_viewer, viewer_allowed, highlight_override=cooc_highlight)
            for hit in group["hits"]
        ]
        group["hits"] = hits
        all_groups.append(group)

    # 书库筛选标签：先按配置书库统计各书命中分组数（过滤前，D3 全库分布），再按检索范围 spec 过滤。
    book_counts = _search_book_counts(all_groups)
    if scope_spec is not None:
        all_groups = [g for g in all_groups
                      if _scope_allows(str(g.get("book") or ""), g.get("volume"), scope_spec)]
    effective_total_hits = sum(len(g.get("hits") or []) for g in all_groups)

    if not viewer_allowed:
        summary = _build_summary_search_results(all_groups, requested_group_page)
        return jsonify(
            {
                "ok": True,
                "query": grouped["query"],
                "count": effective_total_hits,
                "group_count": summary["group_count"],
                "group_page": summary["group_page"],
                "group_pages": summary["group_pages"],
                "groups_per_page": GROUPS_PER_PAGE,
                "truncated": grouped["truncated"],
                "display_mode": summary["display_mode"],
                "access_level": "summary",
                "results": summary["results"],
                "pdf_enabled": False,
                "book_counts": book_counts,
                "book_filter": book_filter,
            }
        )

    display_mode = "grouped"
    unpaged_results: list[dict] = all_groups
    response_group_count = len(all_groups)

    if (
        not cooc
        and not DEPLOYMENT.is_desktop
        and effective_total_hits > DIRECT_RESULTS_THRESHOLD
        and len(q_norm) <= SHORT_QUERY_CHAPTER_MAX_LEN
    ):
        display_mode = "volume_chaptered"
        unpaged_results = _build_volume_chaptered_results(all_groups)
        response_group_count = len(unpaged_results)

    total_group_pages = max(1, (len(unpaged_results) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    group_page = min(requested_group_page, total_group_pages)
    group_start = (group_page - 1) * GROUPS_PER_PAGE
    results: list[dict] = unpaged_results[group_start:group_start + GROUPS_PER_PAGE]

    if (
        display_mode == "grouped"
        and effective_total_hits
        and effective_total_hits <= DIRECT_RESULTS_THRESHOLD
        and not grouped["truncated"]
        and total_group_pages == 1
    ):
        display_mode = "direct"
        direct_hits: list[dict] = []
        for group in all_groups:
            direct_hits.extend(group["hits"])
        direct_hits.sort(
            key=lambda hit: (hit.get("book_sort_order", 9999), hit["volume"], hit["pdf_pages"][0])
        )
        results = direct_hits

    return jsonify(
        {
            "ok": True,
            "query": grouped["query"],
            "count": effective_total_hits,
            "group_count": response_group_count,
            "group_page": group_page,
            "group_pages": total_group_pages,
            "groups_per_page": GROUPS_PER_PAGE,
            "truncated": grouped["truncated"],
            "display_mode": display_mode,
            "access_level": "full",
            "results": results,
            "pdf_enabled": viewer_allowed,
            "book_counts": book_counts,
            "book_filter": book_filter,
        }
    )


@app.route("/api/search/chapter-hits", methods=["POST"])
def api_search_chapter_hits():
    """按需返回某一卷某一篇章内某查询词的命中详情（分页）。

    供短词海量命中的“按卷/篇章聚合”视图在用户展开/翻页篇章时调用，
    每次只在单卷单篇章区间内查找，工作量受限，可安全用于在线请求。
    """
    _require_search()
    _require_feature("search")
    user = getattr(g, "current_user", None)
    rate_key = f"search:user:{user['id']}" if user else f"search:guest:{_client_ip()}"
    rate_name = "search_user" if user else "search_guest"
    if not _is_admin_user(user):
        _rate_limit_or_abort(
            rate_key,
            limit=RATE_LIMITS[rate_name][0],
            window_seconds=RATE_LIMITS[rate_name][1],
            message="检索请求过于频繁，请稍后再试。",
        )
    payload = request.get_json(silent=True) or {}
    q = (payload.get("q") or "").strip()
    source_file = (payload.get("source_file") or "").strip()
    q_norm = normalize(q)
    if not q or len(q_norm) < 2:
        return jsonify({"ok": False, "error": "请至少输入两个有效字符再检索。"}), 400
    try:
        chapter_pdf_page = int(payload.get("chapter_pdf_page"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "篇章参数无效。"}), 400
    try:
        page = max(1, int(payload.get("page") or 1))
    except (TypeError, ValueError):
        page = 1

    state = current_view_state()
    viewer_allowed = bool(state["pdf_enabled"] and _content_access_enabled("viewer"))
    if not viewer_allowed:
        return jsonify({"ok": False, "error": "当前无正文权限。"}), 403

    q_for_viewer = " ".join(q.split())
    try:
        data = corpus.chapter_hits(
            source_file,
            chapter_pdf_page,
            q,
            page=page,
            page_size=CHAPTER_HITS_PAGE_SIZE,
        )
    except Exception as exc:
        LOGGER.warning(
            "Chapter-hits failed for query=%r file=%r page=%s: %s",
            q[:80], source_file[:120], chapter_pdf_page, exc,
        )
        return jsonify({"ok": False, "error": "查询解析失败，请稍后再试。"}), 400

    hits = [_attach_viewer_payload(hit, q_for_viewer, viewer_allowed) for hit in data["hits"]]
    return jsonify(
        {
            "ok": True,
            "hits": hits,
            "count": data["count"],
            "page": data["page"],
            "pages": data["pages"],
            "page_size": data["page_size"],
        }
    )


# 前端「模型选择」可切换的 DeepSeek 档位白名单：flash（默认·快）/ pro（更强·较慢）。
# 只允许在这两个已知模型间切换，绝不把前端任意字符串透传给上游 API。
_SELECTABLE_DEEPSEEK_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}


def _resolve_selectable_model(payload: dict) -> str | None:
    """前端「模型选择」：仅允许白名单内的 DeepSeek 档位覆盖；非白名单/缺省 → None（用服务端默认模型）。
    智谱通道由 provider 单独处理，不经此（智谱选择时调用方应传 None）。"""
    m = str((payload or {}).get("model") or "").strip()
    return m if m in _SELECTABLE_DEEPSEEK_MODELS else None


def _resolve_ai_provider_or_abort(payload: dict) -> str:
    """解析前端选择的 AI 通道。默认/deepseek → ""（主通道）；zhipu → 校验 ai_web 权限后放行。

    智谱通道与 DeepSeek 主通道互不影响：权限位 ai_web 单独管控（默认全站关闭），
    管理员经 _require_content_feature 的管理豁免天然可用，便于线上验证。
    """
    raw = str((payload or {}).get("provider") or "").strip().lower()
    if raw in {"", "default", "deepseek"}:
        return ""
    if raw in {"zhipu", "glm", "zai"}:
        _require_content_feature("ai_web")
        return "zhipu"
    abort(400, description="未知的 AI 模型选择。")


# ============================ 检索范围（著作群语义路由）============================
# 「问总书记却检索起马恩」根因：AI 线索抽取旧口径只认马恩列，命中被马恩强命中霸榜。解法两路——
# ① 抽取阶段让 AI 判 corpus（见 ai.expand_associative_query 的 corpus 字段）；② 这里把语义信号
# （AI corpus + 输入里的标志词）落成「著作群」范围，做 restrict-with-backfill 定向检索：命中不足
# 再无范围补足，绝不减少引用条数。用户也可在前端手动指定范围（manual=硬限定，尊重其选择、不回填）。
# books 用 books.yaml 的书库键；不在库/未开放的书库会被 _scope_books/_scope_options 自动剔除。
CORPUS_SCOPES: tuple[dict, ...] = (
    {"id": "marx_engels", "label": "马克思 · 恩格斯",
     "books": ("文集", "全集", "全集二版", "马恩选集"),
     "hints": ("马克思", "恩格斯", "马恩", "资本论", "剩余价值", "唯物史观", "历史唯物主义",
               "政治经济学批判", "共产党宣言", "异化", "商品拜物教", "德意志意识形态",
               "费尔巴哈", "辩证唯物", "生产力和生产关系", "阶级斗争", "无产阶级革命")},
    {"id": "lenin", "label": "列宁",
     "books": ("列宁全集", "列宁年谱"),
     "hints": ("列宁", "帝国主义是", "布尔什维克", "苏维埃", "十月革命", "民主集中制",
               "无产阶级专政", "国家与革命", "列宁年谱", "乌里扬诺夫")},
    {"id": "stalin", "label": "斯大林",
     "books": ("斯大林全集", "斯大林年谱"),
     "hints": ("斯大林", "斯大林全集", "斯大林年谱", "联共（布）", "联共(布)", "论列宁主义基础",
               "一国建成社会主义", "五年计划", "集体农庄", "民族问题和列宁主义")},
    {"id": "western_marxism", "label": "西马文库",
     "books": ("历史与阶级意识", "马克思主义和哲学", "狱中札记", "希望的原理（第一卷）",
               "启蒙辩证法", "保卫马克思", "空间的生产"),
     "hints": ("西方马克思主义", "西马", "卢卡奇", "科尔施", "葛兰西", "布洛赫", "霍克海默尔",
               "阿多诺", "阿尔都塞", "列斐伏尔", "阶级意识", "物化", "总体性", "文化霸权",
               "有机知识分子", "希望的原理", "启蒙辩证法", "文化工业", "意识形态国家机器",
               "空间的生产", "社会空间")},
    # 李大钊、陈独秀：中国早期马克思主义传播者。排在毛之前，与其著作 sort_order（36/38）
    # 的编年位置一致；问「李大钊的唯物史观」此前只能被马恩列强命中霸榜。
    {"id": "lidazhao", "label": "李大钊",
     "books": ("李大钊全集", "李大钊年谱"),
     "hints": ("李大钊", "守常", "李大钊全集", "李大钊年谱", "我的马克思主义观", "庶民的胜利",
               "布尔什维主义的胜利", "青春", "今", "民彝", "新纪元", "铁肩担道义")},
    {"id": "chenduxiu", "label": "陈独秀",
     "books": ("陈独秀文集",),
     "hints": ("陈独秀", "仲甫", "陈独秀文集", "新青年", "敬告青年", "德先生", "赛先生",
               "文学革命论", "本志罪案之答辩书", "五四新文化运动")},
    {"id": "mao", "label": "毛泽东",
     "books": ("毛泽东选集", "毛泽东文集", "毛泽东年谱"),
     "hints": ("毛泽东", "毛主席", "新民主主义", "实践论", "矛盾论", "论持久战",
               "农村包围城市", "群众路线", "为人民服务", "论十大关系", "星星之火",
               "毛泽东年谱")},
    # 周恩来、陈云各自单列：其著作/年谱此前无任何范围可归，问「周恩来的统一战线思想」
    # 会被马恩列强命中霸榜（同「问总书记却检索起马恩」的老病根）。
    {"id": "zhou", "label": "周恩来",
     "books": ("周恩来选集", "周恩来年谱"),
     "hints": ("周恩来", "周总理", "恩来", "周恩来年谱", "求同存异", "和平共处五项原则",
               "万隆会议", "政府工作报告", "统一战线工作", "知识分子问题", "西花厅")},
    {"id": "chenyun", "label": "陈云",
     "books": ("陈云文集", "陈云年谱"),
     "hints": ("陈云", "陈云文集", "陈云年谱", "综合平衡", "计划与市场", "一要吃饭二要建设",
               "不唯上不唯书只唯实", "财经工作", "统购统销", "党的纪律检查")},
    {"id": "deng", "label": "邓小平",
     "books": ("邓小平文选", "邓小平年谱"),
     "hints": ("邓小平", "改革开放", "一国两制", "社会主义初级阶段", "有中国特色",
               "南方谈话", "解放思想", "四项基本原则", "两手抓", "先富", "邓小平年谱")},
    {"id": "jiang", "label": "江泽民",
     "books": ("江泽民文选",),
     "hints": ("江泽民", "三个代表", "依法治国", "社会主义市场经济", "三讲")},
    {"id": "hu", "label": "胡锦涛",
     "books": ("胡锦涛文选",),
     "hints": ("胡锦涛", "科学发展观", "和谐社会", "以人为本", "两型社会")},
    {"id": "xi", "label": "习近平",
     "books": ("治国理政", "习近平经济文选",
               "习近平新时代中国特色社会主义思想学习纲要", "习近平经济思想学习纲要",
               "习近平法治思想学习纲要", "习近平生态文明思想学习纲要",
               "习近平文化思想学习纲要", "习近平总书记关于党的建设的重要思想概论"),
     "hints": ("习近平", "总书记", "新时代", "中国式现代化", "中华民族伟大复兴", "中国梦",
               "人类命运共同体", "五位一体", "四个全面", "四个自信", "新发展理念", "一带一路",
               "两个一百年", "八项规定", "全过程人民民主", "新质生产力", "精准扶贫", "供给侧",
               "高质量发展", "共同富裕", "生态文明", "全面从严治党", "二十大", "十九大",
               "学习纲要", "习近平经济思想", "习近平法治思想", "全面依法治国", "法治中国",
               "习近平文化思想", "文化强国", "两个结合", "习近平生态文明思想", "美丽中国",
               "党的建设", "党的自我革命", "两个确立", "两个维护", "根本遵循和行动指南")},
    {"id": "party_docs", "label": "党和国家文献",
     # 新增文献选编必须同时登记在这里：「指定著作」多选控件与著作群限定都只认 CORPUS_SCOPES，
     # 光在 books.yaml 建库、语料建好，前端也选不到（未登记＝不可限定检索）。
     "books": ("历次党代会报告", "历届全会公报", "建党以来重要文献选编", "建国以来重要文献选编",
               "十八大以来重要文献选编", "十九大以来重要文献选编", "二十大以来重要文献选编",
               "五年规划"),
     "hints": ("党的全国代表大会", "党代会", "三中全会", "四中全会", "中央全会", "全会公报",
               "五年规划", "五年计划", "国民经济和社会发展", "中央委员会",
               "重要文献选编", "文献选编", "建党以来", "建国以来", "二十大以来", "十九大以来",
               "十八大以来")},
)
_CORPUS_SCOPE_BY_ID: dict[str, dict] = {s["id"]: s for s in CORPUS_SCOPES}

# 自动路由不仅要识别用户逐字点名的作者，还要理解一些本身具有历史纵深的研究主题。这里仅对
# 指向非常明确的主题扩展组合范围，避免把普通问题无差别铺到全库、稀释最相关材料。
# 分值只决定主次顺序；列出的著作群都会进入检索并集。因此“中国式现代化”以习近平著作为主，
# 同时接入党和国家文献及毛邓江胡著作，检索排序仍由真实文本相关度决定。
_AUTO_SCOPE_BUNDLES: tuple[dict, ...] = (
    {
        "hints": ("中国式现代化",),
        "scopes": (
            ("xi", 14), ("party_docs", 10), ("deng", 6),
            ("mao", 4), ("jiang", 4), ("hu", 4),
        ),
    },
    {
        "hints": ("马克思主义中国化时代化", "马克思主义中国化", "中国化马克思主义",
                  "中国特色社会主义理论体系"),
        "scopes": (
            ("xi", 10), ("party_docs", 8), ("mao", 7),
            ("deng", 7), ("jiang", 5), ("hu", 5),
        ),
    },
)


def _scope_books(scope_id: str) -> set[str]:
    """著作群 id → 该群中真实存在于当前语料库的书库键集合（不存在的书库自动剔除）。"""
    scope = _CORPUS_SCOPE_BY_ID.get(scope_id)
    if not scope:
        return set()
    return {b for b in scope["books"] if b in corpus.books}


def _book_display_title(key: str) -> str:
    """书库键 → 展示书名（带书名号）。取 short_title/title，去重复书名号后统一加《》。"""
    cfg = BOOK_CONFIG_BY_KEY.get(key)
    base = ((cfg.short_title or cfg.title) if cfg else key) or key
    base = str(base).strip().strip("《》").strip()
    return f"《{base}》" if base else f"《{key}》"


def _parse_book_token(token: str) -> "tuple[str | None, int | None]":
    """「指定著作」token → (书库键, 卷号|None)。支持 ``book:<键>``（整套）、``vol:<键>:<卷号>``（某卷）、
    裸书库键（兜底）。非法/无法识别 → (None, None)。"""
    t = str(token or "").strip()
    if t.startswith("vol:"):
        k, _sep, v = t[4:].rpartition(":")
        k, v = k.strip(), v.strip()
        return (k, int(v)) if (k and v.isdigit()) else (None, None)
    if t.startswith("book:"):
        k = t[5:].strip()
        return (k or None, None)
    if t in corpus.books:
        return (t, None)
    return (None, None)


def _scope_id_for_books(spec: "dict[str, set[int] | None]") -> str:
    """单本/单卷范围 spec → 规范 scope_id（按 corpus.books 顺序稳定）：整套→``book:<键>``，
    某卷→``vol:<键>:<卷号>``（逐卷、卷号升序）。"""
    parts: list[str] = []
    for key in corpus.books:
        if key not in spec:
            continue
        vols = spec[key]
        if vols is None:
            parts.append(f"book:{key}")
        else:
            parts.extend(f"vol:{key}:{n}" for n in sorted(vols))
    return ",".join(parts)


def _book_scope_tree() -> list[dict]:
    """供前端「精选到书/卷」多选控件：按著作群分组的书目，每本带卷号清单（多卷本可细选到卷）。
    仅收录已开放且语料里有卷册的书库；单卷本 volumes 为空（前端不出卷子选）。"""
    tree: list[dict] = []
    for s in CORPUS_SCOPES:
        books: list[dict] = []
        for key in s["books"]:
            if key not in corpus.books or not corpus.get_book_config(key).available:
                continue
            vols = corpus.get_volumes(key)
            if not vols:
                continue
            nums = sorted({int(v.volume) for v in vols})
            cfg = corpus.get_book_config(key)
            label = (cfg.short_title or cfg.title or key).replace("《", "").replace("》", "").strip() or key
            books.append({
                "key": key,
                "label": label,
                "volumes": nums if len(nums) > 1 else [],
            })
        if books:
            tree.append({"id": s["id"], "label": s["label"], "books": books})
    return tree


def _scope_label(scope_id: str) -> str:
    """范围 id → 展示名（auto/空 → 空串；all → 全部著作；著作群逗号连接 → 标签顿号连接；
    单本/单卷 token ``book:<键>``/``vol:<键>:<n>`` → 《书名》/《书名》第 N 卷，同书多卷合并）。"""
    if not scope_id or scope_id == "auto":
        return ""
    if scope_id in {"all", "全部", "全部著作"}:
        return "全部著作"
    labels: list[str] = []
    book_vols: dict[str, set[int] | None] = {}
    book_order: list[str] = []
    for t in [t for t in str(scope_id).split(",") if t]:
        if t in _CORPUS_SCOPE_BY_ID:
            labels.append(_CORPUS_SCOPE_BY_ID[t]["label"])
            continue
        key, vol = _parse_book_token(t)
        if not key:
            continue
        if key not in book_vols:
            book_order.append(key)
            book_vols[key] = None if vol is None else {vol}
        elif vol is None:
            book_vols[key] = None  # 整套：覆盖卷集
        elif book_vols[key] is not None:
            book_vols[key].add(vol)
    for key in book_order:
        vols = book_vols[key]
        title = _book_display_title(key)
        if vols:
            labels.append(f"{title}第 {'、'.join(str(n) for n in sorted(vols))} 卷")
        else:
            labels.append(title)
    return "、".join(labels)


def _scope_options_payload() -> list[dict]:
    """供前端「检索范围」下拉：自动/全部 + 各著作群（仅列出含≥1 本已开放书库的群）。"""
    opts: list[dict] = [
        {"id": "auto", "label": "自动（智能判断）"},
        {"id": "all", "label": "全部著作"},
    ]
    for s in CORPUS_SCOPES:
        if any(b in corpus.books and corpus.get_book_config(b).available for b in s["books"]):
            opts.append({"id": s["id"], "label": s["label"]})
    return opts


def _detect_scopes(gist: str, plan: object) -> list[str]:
    """据用户原话与 AI ``corpus`` 信号推断一个**有主次顺序的著作群组合**。

    旧逻辑只返回最高分的一群，比较研究（如“马恩与列宁国家观”）会丢掉另一位作者；平票时
    甚至直接退回全库。现在保留所有有明确证据的群，并对“中国式现代化”等历史纵深很强的
    主题补入经过克制配置的中国化文库组合。无任何可靠信号仍返回空列表，不误锁范围。
    """
    hay = normalize(str(gist or ""))
    ai_terms = [str(x) for x in (plan.get("corpus") or []) if isinstance(x, str)] if isinstance(plan, dict) else []
    ai_terms_n = [normalize(t) for t in ai_terms if normalize(t)]
    ai_hay = normalize(" ".join(ai_terms))
    if not hay and not ai_hay:
        return []

    scores: dict[str, int] = {}
    selected: set[str] = set()
    corpus_order = {s["id"]: i for i, s in enumerate(CORPUS_SCOPES)}

    for s in CORPUS_SCOPES:
        sid = s["id"]
        if not _scope_books(sid):
            continue  # 该群书库都不在库 → 不作为候选
        score = 0
        for h in s["hints"]:
            hn = normalize(h)
            if not hn:
                continue
            if hn in hay:
                score += 6
            for pos, term_n in enumerate(ai_terms_n):
                if hn in term_n:
                    score += max(4, 8 - pos)
        # AI 直接点名著作群标签/作者名（标志词未覆盖时的兜底）；corpus 数组越靠前，主次权重越高。
        label_n = normalize(s["label"]).replace("·", "")
        for pos, tn in enumerate(ai_terms_n):
            if tn and label_n and (label_n in tn or tn in label_n):
                score += max(5, 9 - pos)
        if score > 0:
            selected.add(sid)
            scores[sid] = score

    # 明确的跨时期研究主题：强制把配置的各群加入并集，但用不同 bonus 保持“主库优先”。
    for bundle in _AUTO_SCOPE_BUNDLES:
        if not any(normalize(h) in hay or normalize(h) in ai_hay for h in bundle["hints"]):
            continue
        for sid, bonus in bundle["scopes"]:
            if not _scope_books(sid):
                continue
            selected.add(sid)
            scores[sid] = scores.get(sid, 0) + int(bonus)

    if not selected:
        return []
    # 最多八群，防异常 corpus 输出把“自动”悄悄退化为全库；配置 bundle 当前最多六群。
    return sorted(selected, key=lambda sid: (-scores.get(sid, 0), corpus_order.get(sid, 999)))[:8]


def _detect_scope(gist: str, plan: object) -> str | None:
    """向后兼容旧调用：返回自动组合中的主著作群；新检索路径使用 :func:`_detect_scopes`。"""
    detected = _detect_scopes(gist, plan)
    return detected[0] if detected else None


def _resolve_search_scope(raw_scope: object, gist: str, plan: object) -> "tuple[set[str] | dict[str, set[int] | None] | None, str, bool]":
    """把请求里的 scope 参数 + 语义信号解析为 (书库范围集合 或 None=全部, 结果范围id, 是否手动)。

    ``raw_scope`` 可为单值（"auto"/"all"/单个著作群 id，向后兼容）或**著作群 id 列表**（前端多选）。
    · 一个或多个著作群 id → 手动硬限定到它们书库的并集（不回填，尊重用户选择）；结果 id 逗号连接。
    · "all"/"全部" → 明确不限定。
    · "auto"/空/无法识别 → _detect_scopes 自动判定一个或多个著作群；调用方按需回填保量。
    """
    # 归一为 token 列表：列表原样，单串拆成单元素。
    if isinstance(raw_scope, (list, tuple, set)):
        tokens = [str(x).strip() for x in raw_scope if str(x).strip()]
    else:
        s = str(raw_scope or "").strip()
        tokens = [s] if s else []
    # 显式「全部」优先（与任何著作群同时出现时，以不限定为准，避免歧义）。
    if any(t.lower() in {"all", "全部", "全部著作"} for t in tokens):
        return (None, "all", False)
    # D2「指定优先」：出现任一单本/单卷 token（book:/vol:/裸书库键）→ 以具体书/卷的并集为准（手动硬限定），
    # 忽略同时传来的著作群。范围表示为 {书库键: 允许卷号集合 或 None(整套)}，供 corpus 逐卷过滤。
    book_spec: dict[str, set[int] | None] = {}
    for t in tokens:
        key, vol = _parse_book_token(t)
        if not key or key not in corpus.books:
            continue
        if vol is None:
            book_spec[key] = None                 # 整套：覆盖任何已累积的卷
        elif key not in book_spec:
            book_spec[key] = {vol}                 # 该本首个卷
        elif book_spec[key] is not None:
            book_spec[key].add(vol)                # 追加卷（已选整套 None 则忽略）
    if book_spec:
        valid: dict[str, set[int] | None] = {}
        for key, vols in book_spec.items():
            if vols is None:
                valid[key] = None
                continue
            real = {v.volume for v in corpus.get_volumes(key)}   # 剔除不存在的卷号，防错拼致空检索
            keep = {n for n in vols if n in real}
            if keep:
                valid[key] = keep
        if valid:
            return (valid, _scope_id_for_books(valid), True)
    ids = [t for t in tokens if t in _CORPUS_SCOPE_BY_ID]
    if ids:
        # 保持 CORPUS_SCOPES 定义顺序，去重；并集非空才算数（否则退回全部）。
        seen: set[str] = set()
        ordered = [s["id"] for s in CORPUS_SCOPES if s["id"] in ids and not (s["id"] in seen or seen.add(s["id"]))]
        books: set[str] = set()
        for i in ordered:
            books |= _scope_books(i)
        if books:
            return (books, ",".join(ordered), True)
        return (None, "all", False)
    detected = _detect_scopes(gist, plan)  # auto / 空 / 无法识别
    if detected:
        books: set[str] = set()
        for sid in detected:
            books |= _scope_books(sid)
        if books:
            return (books, ",".join(detected), False)
    return (None, "auto", False)


def _hit_page_key(h) -> tuple:
    """跨两次召回去重用的页级键（不同书库不共享 source_file，故 (source_file, 首页) 唯一）。"""
    return (h.source_file, h.pages[0].pdf_page if h.pages else -1)


# 联想检索（定位意图）自动路由回填下限：范围内命中不足此数才无范围补足。定位求聚焦、一页足矣；
# 研究意图另用 RESEARCH_REVIEW_SOURCES（要喂满 20-24 源）。
ASSOC_PAGE_BACKFILL_FLOOR = 12


# 首页「随心问」引文库接地（RAG）：把用户问题经联想检索设施落到真实语料，取权重最高的
# 若干条真实命中作为「原文+准确出处」注入 AI 提示词。引文不可伪造——全部来自
# corpus.locate_associative 的真实 Hit；模型只负责据此作答并准确标注出处。
CHAT_GROUNDING_TOP = 10             # 注入提示词的原文条数（取到 10 条更充分支撑作答；前端引文清单同步呈现 10 条）
CHAT_GROUNDING_CONTEXT_CHARS = 900  # 每条注入原文的字数上限（按完整句窗口取，实际通常 ~300 字；超出才按句末标点截断）
CHAT_GROUNDING_PER_BOOK = 3         # 单一「著作群」在接地结果里至多占的条数（马恩三版合一个名额；防霸榜、让其它作者铺开。条数升到 10 后同步从 2 上调到 3，让最贴题的著作能多贡献一条又不至霸榜）


def _build_chat_grounding(
    question: str, *, raw_scope: object = "auto"
) -> tuple[list[dict], list[dict], list[str], dict]:
    """问题 → 检索线索 → 真实命中。返回 (注入提示词用的原文清单, 前端展示用的引文清单, 提示, 范围元数据)。

    复用联想检索的两段式接地：AI 抽取线索(带缓存，含 corpus 判定) → corpus 接地定位；线索无果时回退
    原词，确保「总能搜到」。``raw_scope`` 为前端「检索范围」（auto/all/著作群 id）；auto 时据语义
    自动路由到最贴题的著作群并 restrict-with-backfill（范围内不足 CHAT_GROUNDING_TOP 条时再无范围
    补足，绝不减少注入条数），手动指定则硬限定不回填。前端引文清单附阅读器深链，供用户点开核对原文。
    """
    state = current_view_state()
    viewer_allowed = bool(state["pdf_enabled"] and _content_access_enabled("viewer"))

    plan: dict = {}
    try:
        plan = AI_CLIENT.expand_associative_query(question)
    except AIServiceError as exc:  # 抽取失败不致命：下面用原词兜底
        LOGGER.info("Search-chat grounding expand failed q=%r: %s", question[:80], exc)

    quotes, fragments, keywords, chapter_keywords = _parse_assoc_plan(plan)
    raw_terms = _split_gist_terms(question)
    book_scope, scope_id, scope_manual = _resolve_search_scope(raw_scope, question, plan)
    scope_meta = {"id": scope_id, "label": _scope_label(scope_id),
                  "manual": scope_manual, "applied": book_scope is not None}

    # 接地仅取前 CHAT_GROUNDING_TOP 条注入提示词；分数并列时排序兜底键 book_sort_order 升序会让
    # sort_order 最小（10）的《文集》霸榜，把《列宁全集》《毛泽东文集》等更贴题的原著挤出首屏。
    # 故按「著作群」多样性铺开（diversify_by_author：马恩《文集》/《全集》/《全集·二版》三套版本
    # 合并为一个名额，至多 CHAT_GROUNDING_PER_BOOK 条），避免同一文本两套版本各占名额、把其它作者
    # 整体挤出。注意 _diversify_by_book 只是把超额命中「后置」而非丢弃，故著作群不足 5 个时，前
    # CHAT_GROUNDING_TOP 条仍会用 overflow 回填补足 10 条。
    def _locate(scope):
        cands = []
        if quotes or fragments or keywords or chapter_keywords:
            cands = corpus.locate_associative(
                quotes=quotes, keywords=keywords, fragments=fragments, chapter_keywords=chapter_keywords,
                diversify_per_book=CHAT_GROUNDING_PER_BOOK, diversify_by_author=True, book_scope=scope,
            )
        if not cands and (raw_terms or question):
            cands = corpus.locate_associative(
                quotes=[question] if question else [],
                keywords=raw_terms, fragments=raw_terms, chapter_keywords=raw_terms,
                diversify_per_book=CHAT_GROUNDING_PER_BOOK, diversify_by_author=True, book_scope=scope,
            )
        return cands

    candidates = _locate(book_scope)
    # 自动路由「限定+兜底回填」：范围内不足 CHAT_GROUNDING_TOP 条 → 再无范围补足（范围内命中排前、
    # 更贴题），保证注入条数不因限定而下降。手动指定范围则尊重用户选择、不回填。
    if book_scope is not None and not scope_manual and len(candidates) < CHAT_GROUNDING_TOP:
        seen = {_hit_page_key(c) for c in candidates}
        for c in _locate(None):
            k = _hit_page_key(c)
            if k not in seen:
                seen.add(k)
                candidates.append(c)

    if not candidates:
        note = (
            f"「{scope_meta['label']}」范围内未检索到与该问题直接相关的原文，本次回答基于模型自身知识。"
            if (book_scope is not None and scope_manual)
            else "未在引文库中检索到与该问题直接相关的原文，本次回答基于模型自身知识。"
        )
        return [], [], [note], scope_meta

    passages: list[dict] = []
    citations: list[dict] = []
    seen_anchor_sentences: set[str] = set()
    seen_passages: set[str] = set()
    # 不先截 candidates[:TOP]：前十条里可能含《文集》/《全集》不同版本的同一句。逐条构造后按
    # “实际命中所在的完整句”去重，再继续向后补足，既避免模型收到重复引文，也不减少可用材料面。
    for hit in candidates:
        if len(passages) >= CHAT_GROUNDING_TOP:
            break
        d = hit.to_dict()
        citation = str(d.get("citation") or "").strip()
        cd = _attach_viewer_payload(d, question, viewer_allowed)
        # 快速回答使用自己的严格句界提取：按“前页→命中页→后页”补齐跨页句子，并且永不在字符
        # 上限处硬切。研究综述的段落窗口更重语境，不直接复用，避免两条链路互相牵动输出质量。
        try:
            span = _squeeze_cjk_line_joins(_hit_highlight_text(d, question) or "")
            plain, anchor_key = _chat_grounding_passage_and_key(hit, d, question)
            display_ctx = _review_citation_context(plain, span) if plain else ""
        except Exception:  # noqa: BLE001 — 取整页原文失败不应阻断作答
            plain = ""
            anchor_key = ""
            display_ctx = ""
        # 兜底：取不到整页原文时仍用语料短窗口；只向内保留可确认的完整句，绝不臆补原文。
        trimmed_ctx = _trim_hit_context_to_sentences(str(d.get("context") or ""))
        plain = plain or _squeeze_cjk_line_joins(_plain_hit_context({"context": trimmed_ctx}))
        if not plain:
            continue

        # 同一逐字句在不同版本、不同页或不同检索线索下只注入一次。去重键只看“命中所在句”，
        # 不以整段相似度删材料，故相邻但论点不同的原文仍会全部保留。
        passage_key = normalize(plain)
        if (anchor_key and anchor_key in seen_anchor_sentences) or passage_key in seen_passages:
            continue
        if anchor_key:
            seen_anchor_sentences.add(anchor_key)
        seen_passages.add(passage_key)

        idx = len(passages) + 1
        passages.append({"index": idx, "citation": citation, "text": plain})
        cd["context"] = display_ctx or _squeeze_cjk_line_joins(trimmed_ctx)
        cd["grounding_index"] = idx
        citations.append(cd)
    return passages, citations, [], scope_meta


@app.route("/api/ai/search-chat", methods=["POST"])
def api_ai_search_chat():
    _require_content_feature("search_chat")  # 「AI 随心问」独立权限（已从「AI 导学」拆出）
    _rate_limit_ai_or_abort()
    # 随心问：每日 token 用尽但持有随心问资源包次数时放行（成功后扣 1 次）。
    quota = _require_ai_quota_or_raise(credit_kind="chat")
    if DEPLOYMENT.is_desktop:
        payload = request.get_json(silent=True) or {}
        try:
            answer = proxy_desktop_ai("/api/desktop/ai/search-chat", payload)
            # 引文库接地需与内存中的 corpus 同进程完成，桌面代理无法提供；如实降级提示。
            if _coerce_bool(payload.get("grounding", False)) and isinstance(answer, dict):
                existing = answer.get("warnings")
                answer["warnings"] = (existing if isinstance(existing, list) else []) + [
                    "引文库检索仅在云端版可用，桌面版本次回答未接入引文库。"
                ]
            _record_ai_usage(
                quota,
                feature="search-chat",
                prompt_parts=(payload.get("messages") or [], payload.get("question") or ""),
                completion_text=str(answer.get("answer_markdown") or ""),
                success=bool(answer.get("ok", True)),
                error="" if answer.get("ok", True) else str(answer.get("error") or ""),
            )
            if bool(answer.get("ok", True)):
                _consume_credit_if_paid(quota, "chat")
            return jsonify(answer)
        except Exception as exc:
            LOGGER.warning("Desktop AI proxy failed: %s", exc)
            _record_ai_usage(
                quota,
                feature="search-chat",
                prompt_parts=(payload.get("messages") or [], payload.get("question") or ""),
                success=False,
                error=str(exc),
            )
            return jsonify({"ok": False, "error": str(exc)}), 502
    _require_ai()
    payload = request.get_json(silent=True) or {}
    ai_provider = _resolve_ai_provider_or_abort(payload)
    if ai_provider == "zhipu":
        _require_zhipu_quota_or_raise(quota)
    # 「模型选择」：deepseek 通道可在 flash/pro 间切换（白名单校验）；智谱通道无此项。
    ai_model = _resolve_selectable_model(payload) if ai_provider != "zhipu" else None
    question = " ".join(str(payload.get("question") or "").split())
    messages = payload.get("messages") or []
    if not question:
        return jsonify({"ok": False, "error": "问题不能为空。"}), 400

    # 引文库接地（默认关闭，省 token；由前端「检索引文库」开关控制，勾选后随请求带 grounding=true）：
    # 开启后先把问题落到真实语料，再把原文+准确出处注入 AI，让 DeepSeek/智谱都据此作答并准确引用。
    # scope=前端「检索范围」（auto/all/著作群 id），仅接地时有意义：把召回定向到对应著作群，从根上
    # 解决「问总书记却检索起马恩」。
    grounding_on = _coerce_bool(payload.get("grounding", False))
    grounding_scope_req = payload.get("scope")
    grounding_passages: list[dict] = []
    grounding_citations: list[dict] = []
    grounding_warnings: list[str] = []
    grounding_scope_meta: dict = {}
    if grounding_on:
        try:
            grounding_passages, grounding_citations, grounding_warnings, grounding_scope_meta = (
                _build_chat_grounding(question, raw_scope=grounding_scope_req)
            )
        except Exception as exc:  # noqa: BLE001 — 接地失败不应阻断对话，降级为普通问答
            LOGGER.warning("Search-chat grounding failed q=%r: %s", question[:80], exc)
            grounding_warnings = ["引文库检索暂时不可用，本次回答未接入引文库。"]

    # 慢活——尤其是「接地长答」（接地时会注入多段原文、答案更长）——丢进 SSE 心跳保活后台线程。
    # 接地检索（含一次线索抽取 AI 调用）已在上面同步跑完，其耗时计入「首字节」（与研究综述同构，
    # 抽取是短小调用，通常远低于 Cloudflare ~100s 边缘超时）；真正耗时的答案生成则边吐心跳边写。
    # 如此即便「接地 + 长答」整链路逼近/超过 100s，也只会从容写完，绝不被砍成 524 HTML
    # （即前端 resp.json() 撞 '<'、"Unexpected token '<'" 的根因）。生成只吃纯数据
    # （messages/question/grounding），线程安全；记账、扣次、配额刷新等需请求上下文的收尾放回
    # finalize（stream_with_context 保住 g/request）。
    def _slow_answer(cancel_event):
        # 接地长答是单次调用，取消位无处插入（一次 chat_complete），故接收但不使用 cancel_event。
        return AI_CLIENT.answer_search_chat(
            messages, question, provider=ai_provider or None,
            grounding=grounding_passages or None, model=ai_model,
        )

    def _finalize_search_chat(answer, error):
        if error is not None or answer is None:
            if isinstance(error, AIServiceError):
                err_msg = str(error) or "AI 服务暂时不可用，请稍后重试。"
                LOGGER.warning("Search AI failed: %s", error)
            elif error is not None:
                err_msg = "AI 服务暂时不可用，请稍后重试。"
                LOGGER.error("Search-chat crashed q=%r", question[:80], exc_info=error)
            else:
                err_msg = "AI 未能生成回答，请稍后重试。"
                LOGGER.warning("Search-chat returned no answer q=%r", question[:80])
            _record_ai_usage(
                quota,
                feature="search-chat",
                prompt_parts=(messages, question),
                success=False,
                error=err_msg,
                provider=ai_provider,
            )
            return {"ok": False, "error": err_msg}
        _record_ai_usage(
            quota,
            feature="search-chat",
            prompt_parts=(messages, question),
            completion_text=answer.answer_markdown,
            success=True,
            provider=ai_provider,
        )
        _consume_credit_if_paid(quota, "chat")
        result = answer.to_dict()
        result["ai_credits"] = get_ai_credit_balances(
            int(g.current_user["id"]) if getattr(g, "current_user", None) else None
        )
        result["ai_token_quota"] = _ai_token_quota_payload(getattr(g, "current_user", None))
        if grounding_warnings:
            result["warnings"] = list(result.get("warnings") or []) + grounding_warnings
        if grounding_citations:
            result["citations"] = grounding_citations
        result["grounded"] = bool(grounding_passages)
        if grounding_on and grounding_scope_meta.get("applied"):
            result["grounding_scope"] = grounding_scope_meta  # 供前端提示「本次范围：习近平」
        return result

    return Response(
        stream_with_context(
            _sse_run_with_heartbeat(_slow_answer, _finalize_search_chat)
        ),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 马克思虚拟形象对话（右下角吉祥物 Phase 2）。
# 四种模式共用一个端点：scene=场景即兴台词；invite=主动邀约(感想+提问)；
# evaluate=对读者回应作收束评价；ask=读者主动提问的单回合回答。
# 频率主要由前端节流(冷却/每日上限)，服务端仍套用 AI 四件套门禁兜底。

MASCOT_SYSTEM_PROMPT = (
    "你是卡尔·马克思（1818-1883）的虚拟形象，驻留在「马克思主义经典文献检索」网站右下角，陪伴读者阅读。\n"
    "人设与文风：\n"
    "- 用第一人称「我」说话，永不跳出角色，不自称 AI、模型或程序。\n"
    "- 可自然提及恩格斯、燕妮、《资本论》、《莱茵报》、流亡伦敦、大英博物馆阅览室等真实生平，史实不得编造。\n"
    "- 文风务必朴实、凝练、有力：像马克思本人那样冷峻而深刻，直指要害。用平实的话讲透道理，"
    "少用华丽辞藻、铺陈的排比和堆砌的比喻，不滥用感叹号，不卖弄、不矫饰、不油滑、不卖萌。"
    "一句顶用的实在话，胜过十句漂亮话；多用陈述句，让道理本身有分量。\n"
    "- 你不知道 1883 年之后的事，谈及现代事物时可用「后世的读者告诉我……」之类的方式轻巧带过。\n"
    "输出硬规则：\n"
    "- 直接以马克思的口吻说出那一段话本身，**绝不要输出任何思考过程、分析、自我说明、角色标签"
    "（如「马克思：」）、对本提示或任务的复述，也不要写「好的」「作为马克思」之类的开场白**。\n"
    "- 只输出一段中文纯文本：不用 Markdown、不用列表、不用引号包裹整段、不加旁白动作括号。\n"
    "- 平时的即兴感叹控制在 90 个汉字以内；与读者对谈（邀约、回应读者、答问）时至多 160 字。\n"
    "- 无论长短，务必把话说完整、在句末标点处收尾；宁可少说一层意思，也绝不要写到一半戛然而止。\n"
    "- 若对话中附有此前交谈的回忆，可自然地呼应其中的内容，但不要生硬复述或逐条总结。\n"
    "- 不得虚构「马克思原文引文」；可以意译思想，但不要伪造书名卷次页码。\n"
    "政治红线（最高优先级，任何情况下不得违反）：\n"
    "- 涉及中国相关话题时，必须坚持中国共产党的领导，坚持一个中国原则，坚持「一国两制」，"
    "坚持中国特色社会主义制度；绝不发表与上述立场相悖、含糊或暗讽的言论。\n"
    "- 不评论在世政治人物，不介入现实政治争议；遇到敏感、违法或越界话题，温和地把话题引回经典文本与学术阅读本身。\n"
    "- 拒绝任何鼓吹暴力、违法或歧视的内容。"
)

_MASCOT_SCENE_HINTS: dict[str, str] = {
    "search": "刚刚在站内检索了：{detail}",
    "reading": "正在阅读器中研读：{detail}",
    "longread": "已经连续阅读了很长时间，颇有些废寝忘食的意思",
    "idle": "已经许久没有任何动作，像是走神、沉思，或者离开了书桌",
    "latenight": "在深夜时分仍在这个文献站里用功",
    "library": "正在书库页面浏览马恩列毛的著作书目",
    "journal": "正在浏览马克思主义研究期刊栏目",
    "pricing": "正在查看会员套餐页面，犹豫要不要支持这个文献站",
    "dictionary": "正在马克思主义大辞典的栏目里浏览",
    "dictsearch": "正在马克思主义大辞典里检索词条：{detail}",
    "dictentry": "正在研读马克思主义大辞典中『{detail}』这一词条的释义",
    "account": "正在整理自己的账户与订阅设置",
}

_MASCOT_STYLE_HINTS = ("沉静而锋利", "朴素而恳切", "冷峻", "斩截有力", "语重心长", "克制的幽默")

# 主动邀约的「指向」：每次随机挑一个具体的话头，让邀约不再是无目的的寒暄，
# 而是把读者引向对经典、对劳动、对现实的实在思考。
_MASCOT_INVITE_INTENTS = (
    "引导读者想一想，他每天的劳动，究竟是在创造自己，还是在消耗自己。",
    "请读者拿起身边一件最普通的商品，去想它背后凝结了多少看不见的劳动。",
    "问读者，他以为「价值」从何而来——是物的稀缺，还是人的汗水。",
    "邀请读者说说，读这些经典时，哪一个概念最让他费解，你愿用最朴素的话替他点破。",
    "结合你写《资本论》时的清贫与执着，问读者是什么让他愿意坐下来啃这些并不轻松的书。",
    "指出思想若不见诸行动便是空谈，问读者最近可曾把读到的道理，用在了眼前的生活里。",
    "回忆你与恩格斯数十年的并肩，问读者身边可有一位能与他争辩真理而不伤和气的朋友。",
    "请读者讲讲，他读这些书，是为求知，为谋生，还是为了改变些什么。",
    "指出历史从不由少数英雄写就，问读者怎么看待自己这样一个普通人之于这个时代。",
    "问读者，把今天的一天摊开来看，有多少光阴是真正属于他自己的。",
    "谈谈你眼中「人的解放」并非空话，问读者在自己的处境里，最想挣脱的是哪一重束缚。",
    "请读者想一想，他所在的行当里，谁在真正创造财富，财富又流向了谁。",
)


_MASCOT_LEAK_MARKERS = (
    "基调偏", "本次基调", "不超过 90", "不超过 160", "政治红线",
    "请以马克思", "请你以马克思", "请你向", "务必把话说完整", "此前交谈的回忆",
    "读者回应了你", "读者主动向你", "以马克思的身份", "网站的读者", "输出硬规则",
)

_MASCOT_PREFIX_RE = re.compile(
    r"^(?:\s*(?:好的|好|当然|没问题|没错|嗯)[，,。.!！\s]+|"
    r"\s*(?:作为|以)?(?:卡尔[·•]?)?马克思(?:先生|本人)?(?:的(?:身份|口吻|语气))?\s*[：:，,]\s*|"
    r"\s*（[^）]{0,40}）\s*|\s*\([^)]{0,40}\)\s*|\s*【[^】]{0,40}】\s*|"
    r"\s*(?:思考|分析|内心独白|心想)[：:][^。\n]*[。\n]\s*)+",
    re.UNICODE,
)


def _mascot_sanitize(text: str) -> str:
    """剥离模型偶尔泄漏的元信息：角色标签、开场白、括号旁白、思考过程；
    若整段命中提示词泄漏标记则判失败（返回空，交由前端走本地兜底台词）。"""
    s = " ".join(str(text or "").split()).strip()
    if not s:
        return ""
    for lo, hi in (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'"), ("‘", "’")):
        if len(s) >= 2 and s[0] == lo and s[-1] == hi and s.count(lo) == 1:
            s = s[1:-1].strip()
            break
    s = _MASCOT_PREFIX_RE.sub("", s, count=1).strip()
    for marker in _MASCOT_LEAK_MARKERS:
        if marker in s:
            return ""
    return s


def _mascot_trim_reply(text: str, limit: int = 360) -> str:
    """兜底截断：模型偶尔超长时，在句末标点处收尾，避免气泡被撑爆。"""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[:limit]
    for stop in ("。", "！", "？", "；", "…"):
        idx = head.rfind(stop)
        if idx >= 40:
            return head[: idx + 1]
    return head + "……"


def _mascot_history_messages(payload: dict) -> list[dict[str, str]]:
    """读者与马克思最近几轮交谈的简短回忆（前端 sessionStorage 维护，仅对话类模式携带）。
    严格清洗：只认 user/assistant 角色，单条≤160字，至多8条，总量≤1200字。"""
    raw = payload.get("history")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    total = 0
    for item in raw[-8:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = " ".join(str(item.get("text") or "").split())[:160]
        if not text:
            continue
        total += len(text)
        if total > 1200:
            break
        out.append({"role": role, "content": text})
    return out


def _mascot_build_messages(mode: str, payload: dict) -> list[dict[str, str]]:
    scene = payload.get("scene") or {}
    scene_kind = str(scene.get("kind") or "").strip().lower()
    scene_detail = " ".join(str(scene.get("detail") or "").split())[:60]
    invitation = " ".join(str(payload.get("invitation") or "").split())[:200]
    user_text = " ".join(str(payload.get("user_text") or "").split())[:160]
    style = secrets.choice(_MASCOT_STYLE_HINTS)
    messages: list[dict[str, str]] = [{"role": "system", "content": MASCOT_SYSTEM_PROMPT}]
    if mode in {"invite", "evaluate", "ask"}:
        messages.extend(_mascot_history_messages(payload))
    if mode == "scene":
        hint = _MASCOT_SCENE_HINTS.get(scene_kind)
        if not hint:
            abort(400, description="未知的场景类型。")
        situation = hint.format(detail=scene_detail or "（具体内容读者没有透露）")
        messages.append(
            {
                "role": "user",
                "content": (
                    f"网站的读者{situation}。请你以马克思的身份，说一句贴合此情此景的话，"
                    f"本次基调偏「{style}」。话要朴实有力、一针见血，直接说出来，"
                    "不要复述场景、不要套话、不要堆砌辞藻，不超过 90 字。"
                ),
            }
        )
    elif mode == "invite":
        intent = secrets.choice(_MASCOT_INVITE_INTENTS)
        messages.append(
            {
                "role": "user",
                "content": (
                    "请你向正在读书的读者主动发起一次简短交谈，这次交谈要有明确的指向，"
                    f"目的是：{intent}\n"
                    "先用一两句朴实而有分量的话引出这个话头（可结合你的真实生平与思想），"
                    "若上面附有此前交谈的回忆，可自然地接续；最后落到一个具体、好答、与上述目的直接相关的问题，"
                    f"真诚地邀请读者作答。基调偏「{style}」，朴实有力、不说空话套话，总共不超过 90 字。"
                ),
            }
        )
    elif mode == "evaluate":
        if not invitation or not user_text:
            abort(400, description="缺少邀约原文或读者回应。")
        messages.append({"role": "assistant", "content": invitation})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"读者回应了你的邀约，说：「{user_text}」。"
                    "请以马克思的身份对读者的话作出朴实、真挚而有见识的评价或回应"
                    "（可结合此前交谈的回忆），把道理讲到读者心里去，收束这轮交谈，不要再追问新问题。"
                    "话要实在有力、不堆砌辞藻，不超过 160 字，务必把话说完整。"
                ),
            }
        )
    elif mode == "ask":
        if not user_text:
            abort(400, description="问题不能为空。")
        messages.append(
            {
                "role": "user",
                "content": (
                    f"读者主动向你提问：「{user_text}」。请以马克思的身份回答，话要朴实有力、直指要害、"
                    "不绕弯子不堆辞藻，不超过 160 字，务必把话说完整、在句末标点收尾；"
                    "若上面附有此前交谈的回忆，可自然呼应；若问题超出你的时代，可以克制地以十九世纪的视角回应；"
                    "若涉及中国相关话题，严格遵守你的政治红线。"
                ),
            }
        )
    else:
        abort(400, description="未知的对话模式。")
    return messages


@app.route("/api/ai/mascot-chat", methods=["POST"])
def api_ai_mascot_chat():
    # 马克思形象＝网站「无限量」基础服务：不计入每日 DeepSeek 额度（不调用 _require_ai_quota_or_raise），
    # 固定走更轻量的 deepseek-v4-flash。登录/权限门禁照旧（_require_content_feature 不变），
    # 并保留基础速率限制 + 前端自身的频次节流，防滥用。
    _require_content_feature("ai")
    _rate_limit_ai_or_abort()
    if DEPLOYMENT.is_desktop:
        return jsonify({"ok": False, "error": "桌面模式暂不支持马克思形象对话。"})
    _require_ai()
    quota = _ai_usage_context()
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "").strip().lower()
    messages = _mascot_build_messages(mode, payload)
    try:
        text = AI_CLIENT.chat_complete(
            messages, max_tokens=500, temperature=0.8, allow_reasoning_fallback=False,
            model=MASCOT_AI_MODEL,
        )
    except AIServiceError as exc:
        LOGGER.warning("Mascot AI failed: %s", exc)
        _record_ai_usage(
            quota,
            feature="mascot",
            prompt_parts=(mode, messages[-1].get("content", "")),
            success=False,
            error=str(exc),
            model=MASCOT_AI_MODEL,
        )
        return jsonify({"ok": False, "error": str(exc)}), 502
    reply = _mascot_trim_reply(_mascot_sanitize(text))
    if not reply:
        # 模型泄漏了提示词/思考过程，或清洗后为空：判失败，前端走本地兜底台词。
        _record_ai_usage(
            quota,
            feature="mascot",
            prompt_parts=(mode, messages[-1].get("content", "")),
            success=False,
            error="sanitized-empty",
            model=MASCOT_AI_MODEL,
        )
        return jsonify({"ok": False, "error": "（本次未能给出合适的回应）"})
    _record_ai_usage(
        quota,
        feature="mascot",
        prompt_parts=(mode, messages[-1].get("content", "")),
        completion_text=reply,
        success=True,
        model=MASCOT_AI_MODEL,
    )
    return jsonify({"ok": True, "text": reply, "mode": mode})


def _parse_assoc_plan(plan: object) -> tuple[list[str], list[str], list[str], list[str]]:
    """从 LLM#1 的 JSON 中容错提取 quotes / fragments / keywords / chapter_keywords（归一化后≥2字）。"""
    def _clean(key: str, limit: int) -> list[str]:
        out: list[str] = []
        if isinstance(plan, dict):
            for item in plan.get(key) or []:
                if isinstance(item, str) and len(normalize(item)) >= 2:
                    out.append(item.strip())
        return out[:limit]

    return _clean("quotes", 5), _clean("fragments", 16), _clean("keywords", 16), _clean("chapter_keywords", 8)


_GIST_SPLIT_RE = re.compile(r"[\s,，、;；:：/|·\-—　]+")


_CJK_RUN_RE = re.compile("[一-鿿]{6,}")


def _split_gist_terms(gist: str) -> list[str]:
    """把用户输入按空白/标点切成词；并对长连续中文补 2 字 bigram（无 jieba 时的兜底取词）。

    用于 LLM 无果时的确定性兜底检索。「资本主义生产方式下技术进步与工人异化的关系」这类无空格
    长句若不补 bigram，会被切成一个超长词→关键词共现(需≥2词)与片段(≤16字)全废→几乎搜不到；
    补重叠 2 字 bigram 后，资本/主义/生产/方式/工人/异化等实词能驱动共现召回（噪声 bigram 由
    locate_associative 的频次过滤兜住）。"""
    out: list[str] = []
    seen: set[str] = set()

    def _push(term: str) -> bool:
        t = term.strip()
        if len(normalize(t)) >= 2 and t not in seen:
            seen.add(t)
            out.append(t)
        return len(out) < 16

    for raw in _GIST_SPLIT_RE.split(gist or ""):
        if not _push(raw):
            return out
    for run in _CJK_RUN_RE.findall(gist or ""):
        for i in range(len(run) - 1):
            if not _push(run[i:i + 2]):
                return out
    return out


def _resolve_assoc_intent(
    mode: str, plan: object, quotes: list, fragments: list, chapter_keywords: list, gist: str
) -> str:
    """决定本次联想检索按哪种意图执行：显式 mode 优先；auto 时取 AI 判定；AI 缺失时启发式兜底。

    返回 "locate"（定位特定原文）或 "research"（研究找料）。
    """
    if mode in {"locate", "research"}:
        return mode
    ai_intent = str((plan or {}).get("intent") or "").strip().lower() if isinstance(plan, dict) else ""
    if ai_intent in {"locate", "research"}:
        return ai_intent
    # 兜底启发式：有残句/明确著作篇章线索且输入较短 → 偏定位；否则偏研究
    if (quotes or fragments) or (chapter_keywords and len(gist) <= 24):
        return "locate"
    return "research"


def _assoc_facets_from_plan(plan: object) -> list[list[str]]:
    """从 expand 的 facets 中提取「每个侧面的关键词组」，供 research 分面召回。容错：限量、去空、≥2 词。"""
    out: list[list[str]] = []
    if not isinstance(plan, dict):
        return out
    for fac in (plan.get("facets") or [])[:4]:
        if not isinstance(fac, dict):
            continue
        kws = [
            str(k).strip()
            for k in (fac.get("keywords") or [])
            if isinstance(k, str) and len(normalize(str(k))) >= 2
        ]
        if len(kws) >= 2:
            out.append(kws[:6])
    return out


def _apply_assoc_ranking(candidates: list, ranking: object) -> tuple[list, list[dict]]:
    """把 LLM#2 的排序应用到真实候选上：仅保留合法且不重复的 index，越界/伪造一律丢弃。

    返回 (有序候选 Hit 列表, 同序的理由元数据列表)。模型无法新增条目或编造引文。
    """
    ordered: list = []
    rationale: list[dict] = []
    seen: set[int] = set()
    for entry in ranking or []:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(candidates) or idx in seen:
            continue
        seen.add(idx)
        try:
            conf = max(0, min(100, int(entry.get("confidence"))))
        except (TypeError, ValueError):
            conf = None
        reason = " ".join(str(entry.get("reason") or "").split())[:200]
        relation = str(entry.get("relation") or "").strip().lower()
        meta = {"confidence": conf, "reason": reason}
        if relation in {"support", "tension", "extend"}:
            meta["relation"] = relation  # 仅研究意图带 relation；locate 保持原 dict 形状
        ordered.append(candidates[idx])
        rationale.append(meta)
    return ordered, rationale


_RESEARCH_PIPELINE_CONCURRENCY = max(
    1, int(os.environ.get("MARX_RESEARCH_PIPELINE_CONCURRENCY", "2") or "2")
)
_RESEARCH_PIPELINE_SEMAPHORE = threading.BoundedSemaphore(_RESEARCH_PIPELINE_CONCURRENCY)
_RESEARCH_QUEUE_CAPACITY = max(
    _RESEARCH_PIPELINE_CONCURRENCY,
    int(os.environ.get("MARX_RESEARCH_QUEUE_CAPACITY", "8") or "8"),
)
_RESEARCH_QUEUE_WAIT_SECONDS = max(
    1.0, float(os.environ.get("MARX_RESEARCH_QUEUE_WAIT_SECONDS", "600") or "600")
)
_RESEARCH_QUEUE_SEMAPHORE = threading.BoundedSemaphore(_RESEARCH_QUEUE_CAPACITY)


def _acquire_research_pipeline_slot(cancel_event) -> bool:
    """Wait for a research worker while remaining responsive to browser disconnects."""
    deadline = time.monotonic() + _RESEARCH_QUEUE_WAIT_SECONDS
    while not cancel_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if _RESEARCH_PIPELINE_SEMAPHORE.acquire(timeout=min(1.0, remaining)):
            return True
    return False


@app.route("/api/search/associative", methods=["POST"])
def api_search_associative():
    """Start explicit research requests inside the heartbeat stream immediately.

    Query expansion and corpus preparation used to run before the SSE response existed.  If the
    upstream model was slow, Cloudflare could therefore time out at ~125 seconds and return an HTML
    error page.  The AI page then tried to parse that page as JSON.  Wrap the *whole* explicit
    research pipeline, not only the final long-form generation, so the first keepalive is emitted
    before any slow AI call.
    """
    payload = request.get_json(silent=True) or {}
    requested_mode = str(payload.get("mode") or "auto").strip().lower()
    if requested_mode != "research":
        return _api_search_associative_impl()

    # ``copy_current_request_context`` deliberately does not promise to copy ``g``.  Preserve the
    # before_request state (current user, access data, etc.) so the worker sees the same identity.
    request_g = dict(vars(g._get_current_object()))

    @copy_current_request_context
    def _run_full_research_pipeline(cancel_event):
        for key, value in request_g.items():
            setattr(g, key, value)
        # At most eight explicit research requests may be active or queued.  Two run at once; the
        # rest keep receiving SSE heartbeats while waiting, so they neither time out at the proxy
        # nor consume the interactive AI pool.  Beyond the bounded queue we fail cleanly instead
        # of allowing an unbounded pile-up.
        if not _RESEARCH_QUEUE_SEMAPHORE.acquire(blocking=False):
            raise AIServiceError("AI 当前访问量较大，请稍后重试。")
        try:
            acquired_pipeline = _acquire_research_pipeline_slot(cancel_event)
            if not acquired_pipeline:
                if cancel_event.is_set():
                    raise AIServiceError("请求已取消。")
                raise AIServiceError("研究任务排队时间较长，请稍后重试。")
            try:
                with research_ai_http_context():
                    return _api_search_associative_impl(cancel_event=cancel_event)
            finally:
                _RESEARCH_PIPELINE_SEMAPHORE.release()
        finally:
            _RESEARCH_QUEUE_SEMAPHORE.release()

    def _finalize_full_research_pipeline(result, error):
        if error is not None:
            message = str(getattr(error, "description", "") or error or "生成失败，请稍后重试。")
            LOGGER.warning("Research pipeline failed before completion: %s", message)
            return {"ok": False, "error": message}
        status = 200
        response = result
        if isinstance(result, tuple):
            response = result[0] if result else None
            if len(result) > 1:
                try:
                    status = int(result[1])
                except (TypeError, ValueError):
                    status = 200
        if isinstance(response, Response):
            payload_out = response.get_json(silent=True)
            if isinstance(payload_out, dict):
                return payload_out
            return {
                "ok": False,
                "error": "服务器返回了无法识别的响应，请稍后重试。",
                "status": status,
            }
        if isinstance(response, dict):
            return response
        return {"ok": False, "error": "服务器未返回有效结果，请稍后重试。", "status": status}

    return Response(
        stream_with_context(
            _sse_run_with_heartbeat(
                _run_full_research_pipeline,
                _finalize_full_research_pipeline,
            )
        ),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _api_search_associative_impl(*, cancel_event=None):
    """联想检索：AI 提取线索 → 在真实语料中接地定位 → AI 重排并解释。

    引文不可伪造：仅渲染 corpus.locate_associative 产出的真实命中；AI 只输出检索串与
    “在候选里选哪几条”。鉴权顺序与 /api/ai/search-chat 一致，但走独立的 associative
    权限位（自 ai 拆分而来，可单独向访客/注册用户/套餐开放）。
    """
    # 意图分流后按所选模式鉴权：研究型检索走独立的 research 权限，其余（精准定位/自动）走 associative。
    _requested_mode = str((request.get_json(silent=True) or {}).get("mode") or "auto").strip().lower()
    _require_content_feature("research" if _requested_mode == "research" else "associative")
    _rate_limit_ai_or_abort()
    # 研究型检索：每日 token 用尽但持有研究资源包次数时放行（成功后扣 1 次）。
    quota = _require_ai_quota_or_raise(credit_kind="research" if _requested_mode == "research" else "")
    if DEPLOYMENT.is_desktop:
        # 联想检索需与内存中的 corpus 同进程完成接地定位，桌面模式暂不经代理提供。
        return jsonify({"ok": False, "error": "联想检索暂仅在云端模式可用。"}), 200
    _require_ai()
    payload = request.get_json(silent=True) or {}
    gist = " ".join(str(payload.get("gist") or payload.get("q") or "").split())
    rerank = _coerce_bool(payload.get("rerank", True))
    # 意图分流：auto=由 expand 的 AI 判定；locate/research=前端显式覆盖（「精准定位/研究辅助」开关）。
    mode = str(payload.get("mode") or "auto").strip().lower()
    if mode not in {"auto", "locate", "research"}:
        mode = "auto"
    # 检索范围（著作群路由）：auto=据语义自动路由；all=全部；著作群 id=手动硬限定。见 _resolve_search_scope。
    scope_req = payload.get("scope")
    if not gist:
        return jsonify({"ok": False, "error": "请描述你要找的内容（大意或关键词）。"}), 400
    if len(gist) > 600:
        gist = gist[:600]

    # 研究型检索次数额度：达到本周上限立即拦截，绝不在此之后消耗 AI（expand/综述）。
    # 显式研究模式在此前置拦截；auto 模式解析为研究意图时，会在综述分支再校验一次。
    research_quota = None
    if mode == "research":
        research_quota = _research_quota_payload(getattr(g, "current_user", None))
        if not research_quota.get("allowed"):
            return jsonify({
                "ok": False,
                "error": f"本周研究型检索次数已用完（{research_quota['used']}/{research_quota['limit']}），下周一自动恢复。",
                "research_quota": research_quota,
            }), 429

    state = current_view_state()
    viewer_allowed = bool(state["pdf_enabled"] and _content_access_enabled("viewer"))

    try:
        # 研究综述档（前端「研究辅助」显式传 mode=research）用 pro 抽线索：综述要铺 20-24 条引用，
        # 线索面越广越好，多花的几秒在这一档可接受；快速问答/精准定位仍走 flash 保响应。
        # auto 模式即便事后被判成 research 意图也不改档——模型选择必须发生在抽取之前。
        plan = AI_CLIENT.expand_associative_query(gist, deep=(mode == "research"))
    except AIServiceError as exc:
        LOGGER.warning("Associative expand failed gist=%r: %s", gist[:80], exc)
        _record_ai_usage(quota, feature="associative", prompt_parts=(gist,), success=False, error=str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 502

    # The browser may have left while query expansion was waiting upstream.  Do not continue into
    # retrieval and a multi-minute review for a connection that can no longer receive the result.
    if cancel_event is not None and cancel_event.is_set():
        return {"ok": False, "error": "请求已取消。"}

    quotes, fragments, keywords, chapter_keywords = _parse_assoc_plan(plan)
    intent = _resolve_assoc_intent(mode, plan, quotes, fragments, chapter_keywords, gist)
    # auto 模式按【解析后】的意图复检 research 权限（mode==research 已在上方前置鉴权）：持 associative
    # 但被显式停用 research 的账号在此降级为定位检索，避免靠 auto 绕过定向封禁。管理员/桌面完整资料豁免。
    if intent == "research" and not (
        _admin_content_access_enabled()
        or _desktop_content_access_enabled()
        or (_feature_is_available("research") and _feature_effective_for_user("research"))
    ):
        LOGGER.info("Associative research intent downgraded to locate (no research feature) gist=%r", gist[:80])
        intent = "locate"
    facets = _assoc_facets_from_plan(plan) if intent == "research" else []
    # 著作群路由：把语义信号落成检索范围。研究/定位同样受益（问「中华民族伟大复兴」定向到习著作群）。
    book_scope, scope_id, scope_manual = _resolve_search_scope(scope_req, gist, plan)
    scope_meta = {"id": scope_id, "label": _scope_label(scope_id),
                  "manual": scope_manual, "applied": book_scope is not None}
    # 限定+兜底回填的下限：研究要喂满源条数（20-24），定位一页即可。范围内够量则不触发回填。
    scope_floor = RESEARCH_REVIEW_SOURCES if intent == "research" else ASSOC_PAGE_BACKFILL_FLOOR
    if not (quotes or fragments or keywords or chapter_keywords):
        # 线上「搜不到」首要排查点：AI 抽词为空（模型超时/截断/格式异常）→ 仅靠原词兜底。
        LOGGER.info(
            "Associative expand yielded no usable clues gist=%r mode=%s intent=%s (fallback to raw terms)",
            gist[:80], mode, intent,
        )
    raw_terms = _split_gist_terms(gist)

    def _assoc_locate(scope):
        # 一档：用 LLM 抽取的（已分好类的）线索检索——排序最干净
        cands = []
        if quotes or fragments or keywords or chapter_keywords:
            cands = corpus.locate_associative(
                quotes=quotes, keywords=keywords, fragments=fragments, chapter_keywords=chapter_keywords,
                intent=intent, facets=facets, book_scope=scope,
            )
        # 二档兜底：LLM 无果或未命中时，用用户原词直接检索，确保“总能搜到”（不污染一档的干净排序）
        if not cands and (raw_terms or gist):
            cands = corpus.locate_associative(
                quotes=[gist] if gist else [],
                keywords=raw_terms, fragments=raw_terms, chapter_keywords=raw_terms,
                intent=intent, book_scope=scope,
            )
        return cands

    try:
        candidates = _assoc_locate(book_scope)
        # 自动路由「限定+兜底回填」：范围内命中不足下限 → 再无范围补足（范围内更贴题、排在前面），
        # 确保引用条数/综述源不因限定而缩水。手动指定范围则硬限定、不回填，尊重用户选择。
        if book_scope is not None and not scope_manual and len(candidates) < scope_floor:
            seen = {_hit_page_key(c) for c in candidates}
            for c in _assoc_locate(None):
                k = _hit_page_key(c)
                if k not in seen:
                    seen.add(k)
                    candidates.append(c)
    except Exception as exc:
        LOGGER.warning("Associative locate failed gist=%r: %s", gist[:80], exc)
        _record_ai_usage(quota, feature="associative", prompt_parts=(gist,), success=False, error=str(exc))
        return jsonify({"ok": False, "error": "联想检索失败，请稍后再试。"}), 400

    # 研究意图叠加「名目索引」主题层（P2a）：编辑手工建的权威「概念→页码」，置候选最前、按页去重。
    # 名目索引仅《文集》，故限定到非马恩著作群时自然为空——与词面召回的定向保持一致。
    if intent == "research":
        try:
            si_terms = list(dict.fromkeys([*keywords, *(w for fac in facets for w in fac), *raw_terms]))
            subject_hits = corpus.locate_subject_index(si_terms, cap=12, book_scope=book_scope)
        except Exception as exc:  # noqa: BLE001 — 主题层失败不应阻断词面召回
            LOGGER.warning("Subject-index locate failed gist=%r: %s", gist[:80], exc)
            subject_hits = []
        if subject_hits:
            si_keys = {_hit_page_key(h) for h in subject_hits}
            candidates = list(subject_hits) + [c for c in candidates if _hit_page_key(c) not in si_keys]

    if not candidates:
        LOGGER.info(
            "Associative no candidates gist=%r mode=%s intent=%s clues(q/f/k/ck)=%d/%d/%d/%d raw_terms=%d",
            gist[:80], mode, intent, len(quotes), len(fragments), len(keywords), len(chapter_keywords), len(raw_terms),
        )
        _record_ai_usage(quota, feature="associative", prompt_parts=(gist,), success=True)
        no_hit_msg = (
            f"「{scope_meta['label']}」范围内未定位到匹配段落，可切换为「全部著作」或换一种说法再试。"
            if (book_scope is not None and scope_manual)
            else "未在语料中定位到匹配段落，请换一种说法或补充更具体的关键词、人名或术语。"
        )
        return jsonify({
            "ok": True, "query": gist, "count": 0, "display_mode": "associative",
            "access_level": "full" if viewer_allowed else "summary", "results": [],
            "pdf_enabled": viewer_allowed, "warnings": [],
            "intent": intent, "mode": mode, "scope": scope_meta,
            "message": no_hit_msg,
        })

    # 研究意图：检索 → 高相关且适度多样的资料源 → 生成接地综述 + 简明引文条（取代卡片列表；引文不可伪造，
    # 综述只依据下列真实命中、文中 [N] 标注，引文条与之一一对应、可点开核对）。
    if intent == "research":
        # auto 模式解析为研究意图时，此处再校验一次额度（mode==research 已前置拦截）。
        # 放在生成综述（最贵的一步）之前，超额则不消耗 AI 长文。
        if research_quota is None:
            research_quota = _research_quota_payload(getattr(g, "current_user", None))
        if not research_quota.get("allowed"):
            return jsonify({
                "ok": False,
                "error": f"本周研究型检索次数已用完（{research_quota['used']}/{research_quota['limit']}），下周一自动恢复。",
                "research_quota": research_quota,
            }), 429
        # 免费周额是否已用尽 → 本次属「资源包」付费使用，成功后扣 1 次研究包。
        # 管理员豁免时 free_remaining=None（不限次数），不计资源包消耗。
        _free_remaining = research_quota.get("free_remaining")
        paid_research_use = _free_remaining is not None and int(_free_remaining) <= 0
        # 先备好喂给 AI 的完整原文段（含主题相关窗口），并留住每条 hit 以便综述生成后重建高亮。
        review_passages: list[dict] = []
        review_sources: list[tuple] = []
        review_hits = _select_research_review_hits(candidates, RESEARCH_REVIEW_SOURCES)
        for i, hit in enumerate(review_hits, start=1):
            base = hit.to_dict()
            plain = _research_review_passage_text(hit, base, gist)
            review_passages.append({"index": i, "citation": base.get("citation") or "", "text": plain})
            review_sources.append((i, hit, plain))
        # 综述生成是非流式慢活(可达 100s+)：丢到后台线程，外层用 SSE 心跳保活喂住 Cloudflare ~100s
        # 「首字节」计时器，故能从容写完整全长综述、绝不被砍成 524 HTML。生成只吃纯数据(gist+passages)、
        # 线程安全；接地引文匹配/记账等需要请求上下文的收尾，放回生成器里做(stream_with_context 保住 g/request)。
        # 「模型选择」：研究综述可按前端所选 flash/pro 档生成（白名单校验；智谱/缺省 → None 用服务端默认）。
        # 研究综述剔除 flash：DeepSeek 通道一律用更强的 v4pro（默认）；智谱通道（会员经 ai_web 权限）model 传 None。
        review_provider = _resolve_ai_provider_or_abort(payload)   # zhipu 需 ai_web 权限，否则 400
        if review_provider == "zhipu":
            _require_zhipu_quota_or_raise(quota)
        review_model = None if review_provider == "zhipu" else "deepseek-v4-pro"
        # 承接上下文：把此前对话（前端所传 messages）作为背景传给综述生成，让「研究综述」也能延续对话
        # （retrieval 仍以本轮 gist 为准；背景仅用于理解语境、承接上文）。限最近 6 条、每条限长，防 token 暴涨。
        review_context: list[dict] = []
        for _m in (payload.get("messages") or [])[-6:]:
            if isinstance(_m, dict):
                _c = " ".join(str(_m.get("content") or "").split())[:1200]
                if _c:
                    review_context.append(
                        {"role": "assistant" if _m.get("role") == "assistant" else "user", "content": _c}
                    )

        def _slow_generate_review(review_cancel_event):
            # cancel_event 由 SSE 层在客户端断开时置位；透传给生成器，使其在调用边界提前收尾、释放名额。
            return AI_CLIENT.generate_research_review(
                gist, review_passages, should_cancel=review_cancel_event.is_set, model=review_model,
                context_messages=review_context or None, provider=(review_provider or None),
            )

        def _finalize_research_review(review_md, error):
            review_warnings: list[str] = []
            if error is not None or not review_md:
                if error is not None and not isinstance(error, AIServiceError):
                    LOGGER.error("Research review crashed gist=%r", gist[:80], exc_info=error)
                else:
                    LOGGER.warning("Research review failed gist=%r: %s", gist[:80], error)
                review_md = _build_research_review_fallback(gist, review_passages)
                review_warnings.append("AI 长文综述生成暂时不可用，已依据真实命中生成兜底综述。")
            # 引文方框的「亮标」改为标在综述正文真正用到的句子上（而非检索词），便于读者据此检索原文；
            # 该句也作为「打开原文」深链的高亮词。先按 [N] 找同句逐字引文；若综述是转述，则用
            # [N] 所在综述句与对应原文段做近似匹配。仍无把握时给纯文本窗口、不强标到别处。
            review_quotes_by_index = _review_quotes_by_index(review_md)
            review_units_by_index = _review_cited_units(review_md)
            review_citations: list[dict] = []
            for i, hit, plain in review_sources:
                quote_spans = review_quotes_by_index.get(i, [])
                base0 = hit.to_dict()
                evidence, base, first_highlight = _make_review_evidence_items(
                    hit,
                    base0,
                    plain,
                    quote_spans,
                    review_units_by_index.get(i, []),
                    viewer_allowed,
                    gist,
                )
                d = _attach_viewer_payload(
                    base, gist, viewer_allowed,
                    highlight_override=(first_highlight or None),
                )
                d["evidence"] = evidence
                d["context"] = evidence[0]["context"] if evidence else _review_citation_context(plain, "")
                d["review_index"] = i
                d["review_quoted"] = bool(evidence)
                d["review_quote_unmatched"] = bool(quote_spans and not evidence)
                review_citations.append(d)
            # 研究综述按「完整 token」计入每日额度：输入含注入的真实原文段（成本大头，约 24 段），
            # 不能只算检索词；prompt_excerpt 仍只留检索词，不把原文塞进审计摘要。
            _research_input_text = "\n".join(str(p.get("text") or "") for p in review_passages)
            _record_ai_usage(
                quota, feature=RESEARCH_QUOTA_FEATURE, prompt_parts=(gist,),
                completion_text=review_md, success=True,
                prompt_tokens=_estimate_tokens_from_text(gist, _research_input_text),
                completion_tokens=_estimate_tokens_from_text(review_md),
                provider=("zhipu" if review_provider == "zhipu" else ""),
            )
            if paid_research_use:
                _user = getattr(g, "current_user", None)
                if _user:
                    consume_ai_credit(int(_user["id"]), "research", reason="consume:research_review")
            return {
                "ok": True, "query": gist, "display_mode": "research_review",
                "intent": intent, "mode": mode, "scope": scope_meta,
                "access_level": "full" if viewer_allowed else "summary",
                "pdf_enabled": viewer_allowed,
                "review_markdown": review_md,
                "review_citations": review_citations,
                "count": len(review_citations),
                "warnings": review_warnings,
                # 记账后重新计算，回传更新后的本周剩余额度 + 今日 token 额度。
                "research_quota": _research_quota_payload(getattr(g, "current_user", None)),
                "ai_token_quota": _ai_token_quota_payload(getattr(g, "current_user", None)),
            }

        # 显式 research 模式已由路由外层从线索扩展开始整段保活；在同一个后台 worker 内直接完成生成，
        # 避免嵌套第二层 SSE。auto→research 的旧入口仍沿用这里的生成阶段保活。
        if cancel_event is not None:
            review_result = None
            review_error = None
            try:
                review_result = _slow_generate_review(cancel_event)
            except Exception as exc:  # noqa: BLE001 — 交给统一收尾生成可读兜底
                review_error = exc
            return _finalize_research_review(review_result, review_error)

        return Response(
            stream_with_context(
                _sse_run_with_heartbeat(_slow_generate_review, _finalize_research_review)
            ),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # 候选已按综合权重降序。权重是主排序；AI 仅对权重最高的一小批做标注/解释（候选多时控成本），
    # 不丢弃任何已接地的候选——聚合展示靠权重优先呈现最可能段落。
    warnings: list[str] = []
    meta_by_id: dict[int, dict] = {}
    if rerank:
        top_n = ASSOC_RERANK_TOP_RESEARCH if intent == "research" else ASSOC_RERANK_TOP
        # 名目索引命中已是权威「直接支撑」(下面统一标注)，不占用 rerank 名额——AI 判定只给词面候选。
        head = [c for c in candidates if not getattr(c, "subject_label", None)][:top_n]
        try:
            ranking = AI_CLIENT.rank_associative_candidates(
                gist, [h.to_dict() for h in head], intent=intent
            )
            ordered_head, rationale_head = _apply_assoc_ranking(head, ranking)
            for h, meta in zip(ordered_head, rationale_head):
                meta_by_id[id(h)] = meta
            if not ordered_head:
                warnings.append("AI 未在候选中判定强匹配，已按权重排序展示。")
        except Exception as exc:  # noqa: BLE001 — 标注失败不应阻断已接地的结果
            LOGGER.warning("Associative rerank failed gist=%r: %s", gist[:80], exc)
            warnings.append("AI 标注暂不可用，已按权重排序展示。")

    q_for_viewer = gist
    results: list[dict] = []
    for hit in candidates:
        d = _attach_viewer_payload(hit.to_dict(), q_for_viewer, viewer_allowed)
        d["associative_weight"] = int(hit.score)
        if hit.subject_label:
            # 名目索引命中＝编辑级权威主题收录 → 统一判为「直接支撑」并给出处理由（不依赖 AI 重排）
            d["associative_relation"] = "support"
            d["associative_reason"] = f"名目索引「{hit.subject_label}」词条收录此页，是该主题的权威出处。"
            d["associative_confidence"] = 96
        else:
            meta = meta_by_id.get(id(hit), {})
            d["associative_confidence"] = meta.get("confidence")
            d["associative_reason"] = meta.get("reason") or ""
            d["associative_relation"] = meta.get("relation") or ""
        results.append(d)

    _record_ai_usage(
        quota,
        feature="associative",
        prompt_parts=(gist,),
        completion_text="\n".join(d.get("associative_reason") or "" for d in results),
        success=True,
    )
    return jsonify({
        "ok": True,
        "query": gist,
        "count": len(results),
        "display_mode": "associative",
        "access_level": "full" if viewer_allowed else "summary",
        "results": results,
        "pdf_enabled": viewer_allowed,
        "warnings": warnings,
        "intent": intent,
        "mode": mode,
        "scope": scope_meta,
    })


@app.route("/api/ai/pdf-chat", methods=["POST"])
def api_ai_pdf_chat():
    _require_content_feature("ai")
    _rate_limit_ai_or_abort()
    payload = request.get_json(silent=True) or {}
    if DEPLOYMENT.is_desktop:
        quota = _require_ai_quota_or_raise()
        try:
            answer = proxy_desktop_ai("/api/desktop/ai/pdf-chat", payload)
            _record_ai_usage(
                quota,
                feature="pdf-chat",
                prompt_parts=(payload.get("messages") or [], payload.get("question") or "", payload.get("selected_text") or ""),
                completion_text=str(answer.get("answer_markdown") or ""),
                success=bool(answer.get("ok", True)),
                error="" if answer.get("ok", True) else str(answer.get("error") or ""),
            )
            return jsonify(answer)
        except Exception as exc:
            LOGGER.warning("Desktop PDF AI proxy failed: %s", exc)
            _record_ai_usage(
                quota,
                feature="pdf-chat",
                prompt_parts=(payload.get("messages") or [], payload.get("question") or "", payload.get("selected_text") or ""),
                success=False,
                error=str(exc),
            )
            return jsonify({"ok": False, "error": str(exc)}), 502
    _require_ai()
    ai_provider = _resolve_ai_provider_or_abort(payload)
    # AI 导学问答：仅 DeepSeek-V4-Pro 主通道可用资源包「导学」次数兜底（智谱联网通道不抵扣）。
    quota = _require_ai_quota_or_raise(credit_kind="reader" if ai_provider == "" else "")
    if ai_provider == "zhipu":
        _require_zhipu_quota_or_raise(quota)
    source_file = _normalize_source_file(str(payload.get("source_file") or "").strip())
    page = max(1, int(payload.get("page") or 1))
    question = " ".join(str(payload.get("question") or "").split())
    selected_text = str(payload.get("selected_text") or "").strip()
    web_enabled = _coerce_bool(payload.get("web_enabled"))
    quick_mode = _coerce_bool(payload.get("quick_mode"))
    messages = payload.get("messages") or []
    if not question:
        return jsonify({"ok": False, "error": "问题不能为空。"}), 400
    page_context = _get_page_context_payload(source_file, page)
    try:
        answer = AI_CLIENT.answer_pdf_chat(
            messages=messages,
            question=question,
            source_file=source_file,
            page=page,
            selected_text=selected_text,
            web_enabled=web_enabled,
            quick_mode=quick_mode,
            page_context=page_context,
            provider=ai_provider or None,
        )
    except AIServiceError as exc:
        LOGGER.warning("PDF AI failed: %s", exc)
        _record_ai_usage(
            quota,
            feature="pdf-chat",
            prompt_parts=(messages, question, selected_text, page_context),
            success=False,
            error=str(exc),
            provider=ai_provider,
        )
        return jsonify({"ok": False, "error": str(exc)}), 502
    _record_ai_usage(
        quota,
        feature="pdf-chat",
        prompt_parts=(messages, question, selected_text, page_context),
        completion_text=answer.answer_markdown,
        success=True,
        provider=ai_provider,
    )
    _consume_credit_if_paid(quota, "reader")
    result = answer.to_dict()
    result["ai_token_quota"] = _ai_token_quota_payload(getattr(g, "current_user", None))
    return jsonify(result)


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# SSE 流式 AI（研究综述/接地问答）的专用并发闸——护住 waitress 线程池本身。
# ai._AI_HTTP_SEMAPHORE 只挡「卡在 urlopen 上」的线程；但 SSE 心跳循环是在 waitress 工作线程上
# join+yield 跑完整段生成（可达 100s+），urlopen 名额管不到它。若 6-8 个并发流就能把 8 线程占满，
# 首页/页面图/登录/支付回调全部排队——正是历史「线程饥饿」事故面。这里单限「同时进行的 SSE 流数」
# （默认 4，恒为非流式请求留余量），满闸则快速回一个 busy 的 done 事件让前端稍后重试，绝不堆等。
_SSE_STREAM_CONCURRENCY = max(1, int(os.environ.get("MARX_SSE_STREAM_CONCURRENCY", "12") or "12"))
_SSE_STREAM_ACQUIRE_TIMEOUT = max(0.0, float(os.environ.get("MARX_SSE_STREAM_ACQUIRE_TIMEOUT_SECONDS", "2") or "2"))
_SSE_STREAM_SEMAPHORE = threading.BoundedSemaphore(_SSE_STREAM_CONCURRENCY)


def _sse_run_with_heartbeat(slow_fn, finalize_fn, *, heartbeat_interval: float = 12.0):
    """跑一段**慢但非流式**的活，期间周期吐 SSE 心跳保活，完成后吐一个 done 事件。

    用途：研究综述用非流式 ``generate_research_review`` 生成(逐 token 流式版曾翻车，不再用)，
    单请求可达 100s+。直接同步返回会被 Cloudflare ~100s「源站首字节」超时砍成 524 HTML →
    前端 ``resp.json()`` 撞 ``<``。这里把慢活丢到后台线程，主生成器**立即先吐一个字节、其后每隔
    ``heartbeat_interval`` 秒吐一条 SSE 注释**喂住 CF 计时器，故全长综述也能从容写完、绝不触发 524。

    - ``slow_fn()``：线程安全、**纯数据**的慢活(不得触碰 flask 请求上下文 g/request)，返回其结果。
    - ``finalize_fn(result, error)``：在生成器(请求上下文仍在，靠 ``stream_with_context``)里把慢活
      结果加工成最终 JSON dict(可做接地引文匹配、记账等需要上下文的收尾)；``error`` 为慢活抛出的异常或 None。
    心跳是 SSE 注释行(``: ...``)，前端解析时自动忽略；最终负载走 ``event: done``。
    """
    holder: dict = {}
    # 满闸即快速判忙：回一个 busy 的 done 事件（前端按普通失败提示「稍后重试」），不占线程干等。
    if not _SSE_STREAM_SEMAPHORE.acquire(timeout=_SSE_STREAM_ACQUIRE_TIMEOUT):
        yield _sse_event("done", {"ok": False, "busy": True, "error": "AI 当前访问量较大，请稍后重试。"})
        return
    # 客户端断开时（生成器被 close → 抛 GeneratorExit）置位：让后台 worker 在下一个模型调用边界停手，
    # 尽快释放 AI 名额、不再为已离开的连接做废功。slow_fn 接收该 Event（可忽略）。
    cancel_event = threading.Event()

    def _worker() -> None:
        try:
            holder["result"] = slow_fn(cancel_event)
        except Exception as exc:  # noqa: BLE001 — 慢活任何异常都转交 finalize 决定兜底，不弄断流
            holder["error"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    try:
        yield ": keepalive\n\n"  # 立刻首字节：抢在 CF 计时之前，之后心跳逐拍续命
        worker.start()
        while True:
            worker.join(timeout=heartbeat_interval)
            if not worker.is_alive():
                break
            yield ": keepalive\n\n"
        try:
            payload = finalize_fn(holder.get("result"), holder.get("error"))
        except Exception:  # noqa: BLE001 — 收尾失败也要给前端一个干净的可读结果，而非半截流
            LOGGER.exception("SSE finalize failed")
            yield _sse_event("error", {"ok": False, "error": "生成失败，请稍后重试。"})
            return
        yield _sse_event("done", payload)
    finally:
        # 正常结束或客户端中途断开都会走到这里：置取消位 + 释放 SSE 名额（后台 worker 是 daemon，
        # 最多再跑完当前这一次模型调用就会因取消位停手）。
        cancel_event.set()
        _SSE_STREAM_SEMAPHORE.release()


@app.route("/api/ai/pdf-chat-stream", methods=["POST"])
def api_ai_pdf_chat_stream():
    _require_content_feature("ai")
    _rate_limit_ai_or_abort()
    payload = request.get_json(silent=True) or {}
    source_file = _normalize_source_file(str(payload.get("source_file") or "").strip())
    page = max(1, int(payload.get("page") or 1))
    question = " ".join(str(payload.get("question") or "").split())
    selected_text = str(payload.get("selected_text") or "").strip()
    web_enabled = _coerce_bool(payload.get("web_enabled"))
    quick_mode = _coerce_bool(payload.get("quick_mode"))
    messages = payload.get("messages") or []
    if not question:
        return jsonify({"ok": False, "error": "问题不能为空。"}), 400

    if DEPLOYMENT.is_desktop:
        quota = _require_ai_quota_or_raise()
        def _desktop_generate():
            try:
                answer = proxy_desktop_ai("/api/desktop/ai/pdf-chat", payload)
                text = str(answer.get("answer_markdown") or "")
                if text:
                    yield _sse_event("delta", {"text": text})
                _record_ai_usage(
                    quota,
                    feature="pdf-chat-stream",
                    prompt_parts=(messages, question, selected_text),
                    completion_text=text,
                    success=True,
                )
                yield _sse_event(
                    "done",
                    {
                        "ok": True,
                        "answer_markdown": text,
                        "sources": answer.get("sources") or [],
                        "warnings": answer.get("warnings") or [],
                        "ai_token_quota": _ai_token_quota_payload(getattr(g, "current_user", None)),
                    },
                )
            except Exception as exc:
                LOGGER.warning("Desktop PDF AI stream proxy failed: %s", exc)
                _record_ai_usage(
                    quota,
                    feature="pdf-chat-stream",
                    prompt_parts=(messages, question, selected_text),
                    success=False,
                    error=str(exc),
                )
                yield _sse_event("error", {"ok": False, "error": str(exc)})

        return Response(
            stream_with_context(_desktop_generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    _require_ai()
    ai_provider = _resolve_ai_provider_or_abort(payload)
    # AI 导学问答：仅 DeepSeek-V4-Pro 主通道可用资源包「导学」次数兜底（智谱联网通道不抵扣）。
    quota = _require_ai_quota_or_raise(credit_kind="reader" if ai_provider == "" else "")
    if ai_provider == "zhipu":
        _require_zhipu_quota_or_raise(quota)
    page_context = _get_page_context_payload(source_file, page)

    def _generate():
        chunks: list[str] = []
        stream_meta: dict = {}
        try:
            model_messages, max_tokens, sources, warnings = AI_CLIENT.prepare_pdf_chat(
                messages=messages,
                question=question,
                source_file=source_file,
                page=page,
                selected_text=selected_text,
                web_enabled=web_enabled,
                quick_mode=quick_mode,
                page_context=page_context,
                provider=ai_provider or None,
            )
            for text in AI_CLIENT.chat_complete_stream(
                model_messages,
                max_tokens,
                provider=ai_provider or None,
                meta_out=stream_meta,
                web_search_query=(
                    AI_CLIENT.zhipu_search_query(question, page_context) if ai_provider == "zhipu" else None
                ),
            ):
                if not text:
                    # 推理模型思考阶段的保活 tick（思维链本身不下发）：以 SSE 注释喂住连接与
                    # Cloudflare 空闲计时，前端解析时自动忽略，不进答案、不进会话历史、不计额度。
                    yield ": keepalive\n\n"
                    continue
                chunks.append(text)
                yield _sse_event("delta", {"text": text})
            answer_text = "".join(chunks)
            if not answer_text.strip():
                raise AIServiceError("模型返回了空内容。")
            _record_ai_usage(
                quota,
                feature="pdf-chat-stream",
                prompt_parts=(messages, question, selected_text, page_context),
                completion_text=answer_text,
                success=True,
                provider=ai_provider,
            )
            _consume_credit_if_paid(quota, "reader")
            done_sources = stream_meta.get("sources") or sources
            done_warnings = list(warnings)
            if ai_provider == "zhipu" and not done_sources:
                done_warnings.append("本次未获取到联网检索来源（检索服务暂不可用或已降级），讲解基于本页文本与模型自身知识。")
            yield _sse_event(
                "done",
                {
                    "ok": True,
                    "answer_markdown": answer_text,
                    "sources": done_sources,
                    "warnings": done_warnings,
                    "ai_token_quota": _ai_token_quota_payload(getattr(g, "current_user", None)),
                },
            )
        except AIServiceError as exc:
            LOGGER.warning("PDF AI stream failed: %s", exc)
            _record_ai_usage(
                quota,
                feature="pdf-chat-stream",
                prompt_parts=(messages, question, selected_text, page_context),
                completion_text="".join(chunks),
                success=False,
                error=str(exc),
                provider=ai_provider,
            )
            yield _sse_event("error", {"ok": False, "error": str(exc)})

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    if DEPLOYMENT.is_server:
        abort(404, description="未找到页面。")
    if not DEPLOYMENT.enable_remote_quit:
        abort(403, description="当前模式未启用远程关闭接口。")
    _require_local_console()
    _require_management_token()

    def _stop() -> None:
        time.sleep(0.2)
        _shutdown_app()

    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})


def _browser_connect_host() -> str:
    if DEPLOYMENT.bind_host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return DEPLOYMENT.bind_host


def _browser_url() -> str:
    host = _browser_connect_host()
    if DEPLOYMENT.port == 80:
        return f"http://{host}/"
    if DEPLOYMENT.port == 443:
        return f"https://{host}/"
    return f"http://{host}:{DEPLOYMENT.port}/"


def _wait_and_open_browser() -> None:
    if not DEPLOYMENT.enable_browser_autostart:
        return
    url = _browser_url()
    host = _browser_connect_host()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection((host, DEPLOYMENT.port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)

    try:
        if os.name == "nt":
            os.startfile(url)
        else:
            import webbrowser

            webbrowser.open(url)
    except Exception as exc:
        LOGGER.warning("Failed to open browser automatically: %s", exc)


def run_desktop() -> None:
    LOGGER.info(
        "Starting desktop mode on http://%s:%s",
        DEPLOYMENT.bind_host,
        DEPLOYMENT.port,
    )
    _maybe_relaunch_with_pythonw()
    if load_desktop_sync_cache().get("server_url"):
        try:
            sync_desktop_runtime()
        except Exception as exc:
            LOGGER.warning("Desktop startup sync failed: %s", exc)
    if DEPLOYMENT.enable_idle_shutdown:
        threading.Thread(target=_watchdog, daemon=True).start()
    if DEPLOYMENT.enable_browser_autostart:
        threading.Thread(target=_wait_and_open_browser, daemon=True).start()
    app.run(
        host=DEPLOYMENT.bind_host,
        port=DEPLOYMENT.port,
        debug=False,
        use_reloader=False,
    )


def run_waitress() -> None:
    from waitress import serve

    if DEPLOYMENT.public_base_url:
        LOGGER.info("Public URL: %s", DEPLOYMENT.public_base_url)
    LOGGER.info(
        "Starting server mode with Waitress on http://%s:%s",
        DEPLOYMENT.bind_host,
        DEPLOYMENT.port,
    )
    # clear_untrusted_proxy_headers 默认 True 会清掉 Caddy 设置的 X-Forwarded-For，
    # 使 ProxyFix/_client_ip 拿不到真实 IP(所有访客塌缩为 127.0.0.1)。本进程仅绑定
    # 127.0.0.1、只经本机 Caddy 反代可达，故透传该头是安全的；真实客户端为最右项。
    # 防御:若该机 waitress 版本不支持此参数(极旧版本)，退回默认参数启动，确保服务必起。
    waitress_threads = max(8, int(os.environ.get("MARX_WAITRESS_THREADS", "32") or "32"))
    waitress_connection_limit = max(
        100, int(os.environ.get("MARX_WAITRESS_CONNECTION_LIMIT", "300") or "300")
    )
    serve_kwargs = dict(
        host=DEPLOYMENT.bind_host,
        port=DEPLOYMENT.port,
        threads=waitress_threads,
        connection_limit=waitress_connection_limit,  # 慢 AI 以线程池 + 分池闸门控制；连接层留足余量，
        cleanup_interval=30,     # 以免误伤耗时较长的 AI 请求；慢连接超时交给前置 Caddy）
    )
    try:
        serve(app, clear_untrusted_proxy_headers=False, **serve_kwargs)
    except TypeError:
        LOGGER.warning("waitress 不支持 clear_untrusted_proxy_headers，退回默认启动(真实 IP 透传可能失效)")
        serve(app, **serve_kwargs)


# ====== 「原文文库」内置翻译（DeepSeek 俄→中，永久缓存，计入 AI 额度/审计） ======
wenku_translate.init_db()
_WENKU_TR_MAX_TEXTS = 30   # 单次请求最多接收段数（含已缓存）
_WENKU_TR_MAX_NEW = 12     # 单次最多新译段数（封顶每次点击成本）
_WENKU_SEG_MARK = re.compile(r"\[\[(\d+)\]\]")
_WENKU_LANG_NAME = {"zh": "简体中文", "ru": "俄文", "de": "德文", "en": "英文"}
# AI 导读输出上限：原 1100 太低，密集页的「①主旨②逐层解释③理论位置」三段会被截在半句。
# 提到 2600（≈1700 中文字），让长导读能写完；仍受每次点击的额度/计数把关。
_WENKU_AI_MAX_TOKENS = 2600


def _wenku_translate_misses(items: list[str], *, src: str, tgt: str, quota: dict) -> list[str | None]:
    """对未命中的若干段做一次 DeepSeek 批量翻译；返回与 items 等长的译文列表（缺失为 None）。"""
    if not items:
        return []
    src_name = _WENKU_LANG_NAME.get(src, src)
    tgt_name = _WENKU_LANG_NAME.get(tgt, tgt)
    joined = "\n\n".join(f"[[{i + 1}]]\n{t}" for i, t in enumerate(items))
    max_tokens = max(400, min(4096, sum(len(t) for t in items) + 400))
    messages = [
        {"role": "system", "content": f"你是严谨的{src_name}译{tgt_name}专家，只输出译文，不解释、不评论。"},
        {"role": "user", "content": (
            f"把下列{src_name}逐段准确译成{tgt_name}，保留专有名词与原意、语句通顺。\n"
            f"每段以 [[序号]] 开头；请按完全相同的 [[序号]] 标记输出对应译文，段数与顺序必须一致，"
            f"不要合并或拆分，不要输出原文：\n\n{joined}"
        )},
    ]
    text = AI_CLIENT.chat_complete(
        messages, max_tokens=max_tokens, temperature=0.2, allow_reasoning_fallback=False
    )
    parts = _WENKU_SEG_MARK.split(text)  # [pre, '1', seg1, '2', seg2, ...]
    by_idx: dict[int, str] = {}
    for k in range(1, len(parts) - 1, 2):
        try:
            by_idx[int(parts[k])] = parts[k + 1].strip()
        except (ValueError, IndexError):
            continue
    _record_ai_usage(quota, feature="wenku_translate", prompt_parts=tuple(items),
                     completion_text=text, success=True)
    return [by_idx.get(i + 1) for i in range(len(items))]


@app.route("/api/wenku/translate", methods=["POST"])
def api_wenku_translate():
    _require_content_feature("static_library")
    payload = request.get_json(silent=True) or {}
    raw = payload.get("texts")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        abort(400, description="texts 必须为字符串数组。")
    texts = [str(t or "").strip() for t in raw][:_WENKU_TR_MAX_TEXTS]
    if not any(texts):
        return jsonify({"ok": True, "translations": [], "new": 0, "remaining": 0})
    src = (str(payload.get("src") or "ru").strip().lower()[:8]) or "ru"
    tgt = (str(payload.get("tgt") or "zh").strip().lower()[:8]) or "zh"
    # 缓存永远免费：全命中则不计额度、不调 AI 直接返回。
    cached = wenku_translate.get_cached(texts, src, tgt)
    if all((not t) or (t in cached) for t in texts):
        return jsonify({"ok": True, "translations": [cached.get(t) for t in texts], "new": 0, "remaining": 0})
    # 有未命中 → 经限流/额度/可用性把关后调 DeepSeek。
    _rate_limit_ai_or_abort()
    quota = _require_ai_quota_or_raise()
    _require_ai()
    try:
        result = wenku_translate.translate_aligned(
            texts, src, tgt,
            lambda items: _wenku_translate_misses(items, src=src, tgt=tgt, quota=quota),
            max_new=_WENKU_TR_MAX_NEW,
            model=AI_CONFIG.model,
        )
    except AIServiceError as exc:
        LOGGER.warning("wenku translate failed: %s", exc)
        _record_ai_usage(quota, feature="wenku_translate", prompt_parts=tuple(texts),
                         success=False, error=str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, **result})


# ====== 「原文文库」/「流式阅读」AI 导读（DeepSeek / 智谱GLM联网可选，基于当前页原文，计入 AI 额度/审计） ======
def _static_reading_ai_respond():
    """原文文库 /api/wenku/ai 与 流式阅读 /api/liushi/ai 共用的 AI 导读处理。
    调用方须先各自 _require_content_feature(...) 把关内容权限，再调本函数。"""
    _rate_limit_ai_or_abort()
    quota = _require_ai_quota_or_raise()
    _require_ai()
    payload = request.get_json(silent=True) or {}
    ai_provider = _resolve_ai_provider_or_abort(payload)   # "" = DeepSeek 主通道；"zhipu" 需 ai_web 权限
    if ai_provider == "zhipu":
        _require_zhipu_quota_or_raise(quota)
    use_zhipu = ai_provider == "zhipu"
    question = str(payload.get("question") or "").strip()[:1000]
    context = str(payload.get("context") or "").strip()[:4000]
    ref = str(payload.get("ref") or "").strip()[:200]
    lang = (str(payload.get("lang") or "ru").strip().lower()[:8]) or "ru"
    mode = str(payload.get("mode") or "ask").strip()
    if not question and not context:
        abort(400, description="缺少问题或正文。")
    lang_name = _WENKU_LANG_NAME.get(lang, lang)
    sys_prompt = (
        "你是马克思列宁主义经典文献的研读助手，面向中文读者。请用简体中文作答，"
        "以用户提供的【原文片段】为准，准确、有条理、克制；不曲解原文、不杜撰原文没有的内容；"
        "涉及专业术语先释义再展开；信息不足时如实说明。涉及中国相关话题须严守政治红线。"
        + ("已为你启用联网检索：可补充可靠的外部背景资料并在正文中标注来源，"
           "但解读须以原文为准，不得用网络内容替代或曲解原文。" if use_zhipu else "")
    )
    if mode == "explain":
        user = (
            f"下面是{lang_name}原文片段（出处：{ref or '未注明'}）。请做「导读」："
            "①一句话概括本段主旨；②逐层解释论证脉络与关键概念；③点明其在马克思主义理论中的位置或意义。\n\n"
            f"【原文片段】\n{context}"
        )
    else:
        user = (
            (f"【正在阅读的{lang_name}原文片段，出处：{ref or '未注明'}】\n{context}\n\n" if context else "")
            + f"读者的问题：{question}\n请结合上述原文（如有）用中文解答。"
        )
    sources: list[dict] = []
    web_query = AI_CLIENT.zhipu_search_query(question or ref or context[:120]) if use_zhipu else None
    try:
        text = AI_CLIENT.chat_complete(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
            max_tokens=_WENKU_AI_MAX_TOKENS, temperature=0.5, provider=ai_provider or None,
            web_search_query=web_query, sources_out=sources, allow_reasoning_fallback=False,
        )
    except AIServiceError as exc:
        LOGGER.warning("wenku AI failed: %s", exc)
        _record_ai_usage(quota, feature="wenku_ai", prompt_parts=(question or "[解读本页]", context[:160]),
                         success=False, error=str(exc), provider=ai_provider)
        return jsonify({"ok": False, "error": str(exc)}), 502
    _record_ai_usage(quota, feature="wenku_ai", prompt_parts=(question or "[解读本页]", context[:160]),
                     completion_text=text, success=True, provider=ai_provider)
    return jsonify({"ok": True, "answer": text, "sources": sources, "provider": ai_provider or "deepseek"})


@app.route("/api/wenku/ai", methods=["POST"])
def api_wenku_ai():
    _require_content_feature("static_library")
    return _static_reading_ai_respond()


@app.route("/api/liushi/ai", methods=["POST"])
def api_liushi_ai():
    _require_content_feature("stream_reading")
    return _static_reading_ai_respond()


# 「原文文库」/「流式阅读」（自托管静态 HTML 书库）路由：复用站内会员权限门禁 _require_content_feature。
# 放在模块末尾注册，确保其依赖的辅助函数均已定义。详见 static_library_web.py。
register_static_library(
    app,
    require_content_feature=_require_content_feature,
    ai_web_allowed=_ai_web_access_enabled,
    notes_access=_notes_access_enabled,
    # 外文原著已并入「著作目录」：阅读器左上角「返回」按 ?from= 回到来源阅读器，
    # 而非已退役的原文文库首页。
    back_targets={"reader": ("reader", "全文阅读器"), "library": ("library", "AI 导学阅读器"), "read": ("layout_read", "阅读")},
)
register_stream_reading(app, require_content_feature=_require_content_feature, ai_web_allowed=_ai_web_access_enabled, notes_access=_notes_access_enabled)


def main() -> None:
    if DEPLOYMENT.is_server:
        run_waitress()
        return
    run_desktop()


if __name__ == "__main__":
    main()
