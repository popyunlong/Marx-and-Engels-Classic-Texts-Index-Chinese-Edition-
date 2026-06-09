from __future__ import annotations

import json
import html
import ipaddress
import os
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
    AIServiceError,
    ZAIClient,
    load_ai_config,
    reset_ai_overrides,
    save_ai_overrides,
)
from build_index import normalize
from membership import (
    clear_pending_orders,
    consume_account_email_token,
    create_pending_order,
    create_account_email_token,
    create_user,
    deactivate_user_account,
    expire_pending_orders,
    create_manual_subscription,
    get_order_by_no,
    get_membership_snapshot,
    get_account_email_token,
    get_admin_dashboard_first_day,
    get_admin_dashboard_metrics,
    get_ai_token_usage,
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
    list_recent_orders,
    list_recent_subscriptions,
    list_orders_for_user,
    list_subscriptions_for_user,
    list_users,
    load_session_secret,
    normalize_email,
    china_day_text,
    prune_reader_access_events,
    record_ai_usage,
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
from desktop_sync import (
    CACHE_PATH as DESKTOP_SYNC_CACHE_PATH,
    activate as activate_desktop_sync,
    cached_ai_public_runtime,
    load_cache as load_desktop_sync_cache,
    proxy_ai as proxy_desktop_ai,
    save_cache as save_desktop_sync_cache,
    sync as sync_desktop_runtime,
)
from feature_access import (
    AUDIENCE_ACCESS_LABELS,
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
    get_attachment as get_feedback_attachment,
    get_user_thread as get_feedback_user_thread,
    init_feedback_db,
    list_feedback_threads,
    update_message_email_status as update_feedback_message_email_status,
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
    create_or_update_subscription,
    current_batch,
    deliver_ready_articles as deliver_ready_journal_articles,
    generate_batch_review,
    get_batch,
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
    list_recent_delivery_logs as list_recent_journal_delivery_logs,
    list_recent_runs as list_recent_journal_runs,
    list_recent_subscriptions as list_recent_journal_subscriptions,
    list_subscriptions_for_user as list_journal_subscriptions_for_user,
    load_alert_settings as load_journal_alert_settings,
    load_smtp_config,
    normalize_alert_settings,
    public_base_url as journal_alert_public_base_url,
    purge_archived_articles,
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
from search import CHAPTER_HITS_PAGE_SIZE, Corpus
from site_content import (
    SITE_TEXT_OVERRIDES_PATH,
    AutoSiteTextExtension,
    get_site_text_map,
    list_site_text_groups,
    list_site_text_groups_from_map,
    prune_stale_overrides,
    render_auto_site_text,
    render_site_text,
    reset_site_text_overrides,
    save_site_text_overrides,
    site_text_coverage_report,
    stale_override_keys,
)
from zpay import ZPayClient, load_zpay_config


DIRECT_RESULTS_THRESHOLD = 8
GROUPS_PER_PAGE = 20
SHORT_QUERY_CHAPTER_MAX_LEN = 4
ASSOC_RERANK_TOP = 12  # 联想检索仅对权重最高的前若干候选做 AI 标注/解释（候选多时控成本）
REQUEST_TOKEN = secrets.token_urlsafe(24)
LOGGER = configure_logging()
DEPLOYMENT = load_deployment_settings()
MEMBERSHIP_DB_PATH = init_membership_db()
ADMIN_STORE_DB_PATH = init_admin_store_db()
JOURNAL_ALERTS_DB_PATH = init_journal_alerts_db()
FEEDBACK_DB_PATH = init_feedback_db()
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
READER_ENDPOINTS = {
    "reader",
    "library",
    "pdf_viewer",
    "serve_pdf",
    "page_image",
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
    # 按真实客户端 IP 的兜底限速(#2 真实 IP 透传后启用)。NAT 宽容、阈值高，
    # 正常读者/校园 NAT 远不及此，只拦单 IP 的高频批量抓取。可经 env 覆盖。
    "reader_view_ip": (200, 60),
    "reader_pageimg_ip": (1500, 60),
}
# 极端真实 IP 扒站者的保守自动封禁阈值(双高：日总量 且 单分钟峰值)。仅封公网 IP actor，
# 永不封登录会员/内网/监控；可经设置 reader_auto_ban 或 env 调整、DISABLE_READER_AUTO_BAN 关闭。
READER_AUTO_BAN_DAILY_MIN = 2000
READER_AUTO_BAN_MINUTE_MIN = 90
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
_last_order_expiry_sweep: list[float] = [0.0]
_last_reader_audit_prune: list[float] = [0.0]
_last_reader_auto_ban: list[float] = [0.0]
ADMIN_SECTION_MODULES = {
    "overview": "overview",
    "copy": "content",
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


def _book_payload(book: str) -> dict:
    cfg = _book_config(book)
    return {
        "book_title": cfg.title,
        "book_short_title": cfg.short_title,
        "citation_title": cfg.citation_title,
        "book_sort_order": cfg.sort_order,
        "tag_class": cfg.tag_class,
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
    }


def _is_local_console_request() -> bool:
    if not DEPLOYMENT.is_desktop:
        return False
    remote = (request.remote_addr or "").strip()
    return remote in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _require_local_console() -> None:
    if not _is_local_console_request():
        abort(403, description="本地控制台仅允许从当前机器访问。")


def current_view_state() -> dict:
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
    return {
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
    }


def _safe_next_url(value: str | None) -> str:
    target = (value or "").strip()
    if not target:
        return url_for("index")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return url_for("index")
    if not target.startswith("/"):
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

    # mapi 失败：回退到网关跳转（年度等可用渠道仍可支付）；同时给出可读提示。
    return _render_checkout_page(
        order, plan, mode="api", pay_url="",
        message=f"二维码下单失败：{result.get('msg') or '网关未返回支付链接'}。可点击下方按钮改用网关页面支付。",
        gateway_url=_legacy_page_pay_url(order, plan, user),
    )


def _is_member_enabled() -> bool:
    membership = getattr(g, "membership", None)
    return bool(current_view_state()["pdf_enabled"] and membership and membership.is_active_member)


def _admin_content_access_enabled() -> bool:
    return bool(current_view_state()["pdf_enabled"] and _is_admin_user(getattr(g, "current_user", None)))


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
    return load_shared_access_policy(include_saved=DEPLOYMENT.is_server)


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
    if feature == "ai":
        return bool(AI_CONFIG.enabled)
    return True


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
    prefix = "index.reader_full" if kind == "full" else "index.reader_ai"
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
        _reader_access_entry("ai", ["library", "ai"], url_for("library")),
    ]


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
        return {"available": True, "mode": "ai" if is_ai else "reader", "label": label, "status": "available", "href": ""}
    href = best.get("href") or ""
    return {
        "available": False,
        "mode": "",
        "label": label,
        "status": best.get("status") or "unavailable",
        "href": "" if href in ("#", "") else href,
    }


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
    if not getattr(g, "current_user", None):
        flash("请先登录会员账号，或在后台开放访客权限。", "warning")
        raise _RedirectTo(url_for("login", next=request.full_path if request.query_string else request.path))
    flash(f"当前账号暂未开放{FEATURE_ACCESS_LABELS.get(feature, feature)}权限。", "warning")
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
            "rule": f"当前在线为最近 {ONLINE_WINDOW_SECONDS // 60} 分钟有记录的去重访问者；登录用户按账号去重，未登录访客按浏览器会话去重，当日在线同口径按所选日期统计。IP 不作为主去重键，避免把同一单位或家庭的多人误合并。",
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
        "阅读异常",
        "AI请求",
        "AI token",
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
            item.get("reader_anomaly_count"),
            item.get("ai_requests_today"),
            item.get("ai_tokens_today"),
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


def _record_reader_access_event(*, is_rate_limited: bool = False) -> None:
    if not _is_reader_audit_endpoint():
        return
    try:
        record_reader_access_event(**_reader_audit_payload(is_rate_limited=is_rate_limited))
    except Exception as exc:
        LOGGER.debug("Reader access recording failed: %s", exc)


def _prune_reader_audit_if_due() -> None:
    now = time.time()
    if now - _last_reader_audit_prune[0] < READER_AUDIT_PRUNE_INTERVAL_SECONDS:
        return
    _last_reader_audit_prune[0] = now
    try:
        prune_reader_access_events(keep_days=READER_AUDIT_KEEP_DAYS)
    except Exception as exc:
        LOGGER.debug("Reader access pruning failed: %s", exc)


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
    try:
        payload = get_setting("reader_auto_ban", {})
        if isinstance(payload, dict):
            if "enabled" in payload:
                enabled = bool(payload.get("enabled"))
            daily = int(payload.get("daily_min") or daily)
            minute = int(payload.get("minute_min") or minute)
    except Exception:
        pass
    try:
        daily = int(os.environ.get("READER_AUTO_BAN_DAILY_MIN") or daily)
        minute = int(os.environ.get("READER_AUTO_BAN_MINUTE_MIN") or minute)
    except ValueError:
        pass
    return {"enabled": enabled, "daily_min": max(1, daily), "minute_min": max(1, minute)}


def _alert_admin_auto_ban(items: list) -> None:
    # 反爬自动封禁后给管理员发一封告警邮件。被封 IP 本就是首次新封（已封的在扫描里被跳过），
    # 天然去重，无需额外记账。SMTP 未配置则静默跳过；发送失败不影响封禁本身。
    if not items or not _account_email_configured():
        return
    to_email = (os.environ.get("SECURITY_ALERT_EMAIL") or "").strip() or FEEDBACK_ADMIN_EMAIL
    if not to_email:
        return
    lines = [
        f"- IP {it['ip']}：当日 {it['request_count']} 次、单分钟峰值 {it['max_minute_requests']}；{it.get('reason', '')}"
        for it in items
    ]
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
            return
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
    ua_tokens: list[str] = []
    ips: list[str] = []
    emails: list[str] = []
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
    def __init__(self, *, used: int, limit: int, reset_at: str):
        super().__init__("今日 AI token 已达到限额。")
        self.used = used
        self.limit = limit
        self.reset_at = reset_at


def _require_ai_quota_or_raise() -> dict:
    user = getattr(g, "current_user", None)
    session_key = _visitor_session_key()
    day, _, _, reset_at = _beijing_day_bounds()
    limit_info = get_user_ai_limit(int(user["id"])) if user else get_user_ai_limit(None)
    used = get_ai_token_usage(day=day, user_id=int(user["id"]) if user else None, session_key=session_key)
    limit = limit_info.get("limit")
    if limit is not None and used >= int(limit):
        raise _AIQuotaExceeded(used=used, limit=int(limit), reset_at=reset_at)
    return {"day": day, "session_key": session_key, "used": used, **limit_info}


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
) -> None:
    # 豁免的监控程序：其 AI 调用不计入 ai_usage(总览 AI 请求/token/高用量名单)。
    if has_request_context() and _is_monitoring_request():
        return
    user = getattr(g, "current_user", None)
    try:
        prompt_tokens = _estimate_tokens_from_text(*prompt_parts)
        completion_tokens = _estimate_tokens_from_text(completion_text)
        if not success and completion_tokens == 0:
            completion_tokens = 0
        # 仅留存用户真实输入（问题与选中文本，即 prompt_parts 中的字符串项），不含系统
        # 拼装的页面上下文或历史消息，供后台核查异常用量时了解“到底问了什么”。
        user_input = "\n".join(
            part.strip() for part in prompt_parts if isinstance(part, str) and part.strip()
        )
        source_ref = _ai_usage_source_ref(prompt_parts)
        client_ip = _client_ip() if has_request_context() else ""
        record_ai_usage(
            user_id=int(user["id"]) if user else None,
            session_key=str((quota or {}).get("session_key") or _visitor_session_key()),
            day=str((quota or {}).get("day") or china_day_text()),
            feature=feature,
            provider=AI_CONFIG.provider,
            model=AI_CONFIG.model,
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
    # 生产链路：客户端 → Caddy(本机反代) → waitress → 本进程。Caddy 会把直连客户端
    # 追加为 X-Forwarded-For 的**最右一项**(左侧可由客户端伪造，最右项由 Caddy 写入、
    # 不可伪造)，故取最右可信项作为真实 IP；与 ProxyFix(x_for=1) 取值一致。
    # 需配合 run_waitress 的 clear_untrusted_proxy_headers=False，否则 waitress 会清掉
    # 该头、导致所有访客 IP 恒为 127.0.0.1(反爬的 IP 维度因此全部失效)。
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
    # 书页图像防爬（P3）。管理员/已豁免监控不计；其余按登录用户 ID 计数，匿名按浏览器会话
    # key 计数，避免对共享出口 IP（如校园 NAT）的正常读者造成误伤。阈值很宽松，正常阅读不触发。
    user = getattr(g, "current_user", None)
    if _is_admin_user(user) or _is_monitoring_request():
        return
    actor = f"user:{user['id']}" if user else f"sess:{_visitor_session_key()}"
    _rate_limit_or_abort(
        f"pageimg:{actor}",
        limit=RATE_LIMITS["page_image"][0],
        window_seconds=RATE_LIMITS["page_image"][1],
        message="书页图像加载过于频繁，请稍后片刻再继续阅读。",
    )


def _reader_ip_rate(kind: str) -> tuple[int, int]:
    """阅读内容端点按 IP 限速的(阈值, 窗口秒)。可经 env 覆盖（"limit,window"）。"""
    env_name = {"view": "READER_VIEW_IP_RATE", "pageimg": "READER_PAGEIMG_IP_RATE"}.get(kind, "")
    raw = str(os.environ.get(env_name) or "").strip() if env_name else ""
    if raw and "," in raw:
        try:
            limit_s, window_s = raw.split(",", 1)
            return max(1, int(limit_s.strip())), max(1, int(window_s.strip()))
        except ValueError:
            pass
    return RATE_LIMITS["reader_view_ip" if kind == "view" else "reader_pageimg_ip"]


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
    policy = _load_access_policy()
    if user:
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


def _recent_failures(key: str) -> list[float]:
    now = time.time()
    values = _prune_window(_login_failures.get(key, []), now, LOGIN_FAILURE_WINDOW_SECONDS)
    _login_failures[key] = values
    return values


def _login_failure_count(email: str) -> int:
    return max((len(_recent_failures(key)) for key in _failure_keys(email)), default=0)


def _record_login_failure(email: str) -> None:
    now = time.time()
    for key in _failure_keys(email):
        values = _prune_window(_login_failures.get(key, []), now, LOGIN_FAILURE_WINDOW_SECONDS)
        values.append(now)
        _login_failures[key] = values
    # 持久审计：内存计数重启即丢，这里结构化打日志，配合 journald 保留可事后回溯撞库。
    try:
        LOGGER.info("auth_failure %s", json.dumps({"email": email, "ip": _client_ip()}, ensure_ascii=False))
    except Exception:
        pass


def _clear_login_failures(email: str) -> None:
    for key in _failure_keys(email):
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
    current = get_site_text_map()
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
    defaults = get_site_text_map()
    for key, legacy_value in legacy_network_texts.items():
        if current.get(key) == legacy_value:
            current[key] = defaults.get(key, current[key])
    return current


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
        if isinstance(values, dict):
            return {str(k): str(v) for k, v in values.items()}
        return {}
    from site_content import _load_overrides  # 本地/单机模式直接读覆盖文件

    return dict(_load_overrides())


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
        "users": users,
        "user_q": search_text,
        "feature_access_keys": FEATURE_ACCESS_KEYS,
        "feature_access_labels": FEATURE_ACCESS_LABELS,
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
        "journal_recent_batches": list_recent_batches(limit=8) if remote_admin else [],
        "journal_ai_model": AI_CONFIG.model,
        "journal_ai_enabled": bool(AI_CONFIG.enabled),
        "journal_recent_articles": list_recent_journal_articles(limit=18) if remote_admin else [],
        "journal_recent_runs": list_recent_journal_runs(limit=12) if remote_admin else [],
        "journal_delivery_logs": list_recent_journal_delivery_logs(limit=18) if remote_admin else [],
        "journal_abstract_coverage": journal_abstract_coverage() if remote_admin else {},
        "journal_smtp_enabled": load_smtp_config().enabled,
        "smtp_config": load_smtp_config(),
        "control_email_test_url": url_for("admin_email_test") if remote_admin else "",
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
            "search_provider": "disabled",
            "search_base_url": "",
            "search_api_key": "",
            "request_timeout_seconds": _form_int("request_timeout_seconds", int(current["request_timeout_seconds"])),
            "default_web_enabled": False,
            "web_search_count": _form_int("web_search_count", int(current["web_search_count"])),
            "max_history_turns": _form_int("max_history_turns", int(current["max_history_turns"])),
            "search_history_turns": _form_int("search_history_turns", int(current["search_history_turns"])),
            "pdf_history_turns": _form_int("pdf_history_turns", int(current["pdf_history_turns"])),
            "search_message_char_limit": _form_int("search_message_char_limit", int(current["search_message_char_limit"])),
            "pdf_message_char_limit": _form_int("pdf_message_char_limit", int(current["pdf_message_char_limit"])),
            "search_answer_max_tokens": _form_int("search_answer_max_tokens", int(current["search_answer_max_tokens"])),
            "pdf_answer_max_tokens": _form_int("pdf_answer_max_tokens", int(current["pdf_answer_max_tokens"])),
            "pdf_quick_answer_max_tokens": _form_int("pdf_quick_answer_max_tokens", int(current["pdf_quick_answer_max_tokens"])),
            "search_web_search_count": _form_int("search_web_search_count", int(current["search_web_search_count"])),
            "pdf_web_search_count": _form_int("pdf_web_search_count", int(current["pdf_web_search_count"])),
            "pdf_quick_web_search_count": _form_int("pdf_quick_web_search_count", int(current["pdf_quick_web_search_count"])),
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
            is_active=_form_bool("is_active"),
            sort_order=_form_int("sort_order", 0),
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
        current["ai"] = None
        result = "unban"
        message = f"已恢复 {email} 的 AI 使用权限（跟随全站默认）。"
    else:
        current["ai"] = False
        result = "ban"
        message = f"已暂停 {email} 的 AI 使用：阅读器 AI 导学与首页随心问均不可用，其余功能不受影响。"
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
    citation = corpus._make_citation(volume.book, volume.volume, [page_obj]) if corpus else ""
    page_label = page_obj.printed_page or f"PDF-{page_number}"

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
        "current_text": _clean_text(page_obj.raw_text),
        "previous_excerpt": _clean_text(previous_text, limit=240),
        "next_excerpt": _clean_text(next_text, limit=240),
    }


def _page_image_cache_path(source_file: str, page_number: int, query_text: str) -> Path:
    pdf_path = _resolve_pdf_path(source_file, require_full_mode=False)
    try:
        stamp = f"{pdf_path.stat().st_mtime_ns}:{pdf_path.stat().st_size}"
    except OSError:
        stamp = "missing"
    # 缓存版本号 v3：渲染参数（自适应高分辨率 + JPEG 质量）升级后必须改版本号，
    # 否则旧的 1.45× 模糊缓存（含线上已预热的）会继续命中，新逻辑不生效。
    # profile tag：毛选高分辨率 profile 用独立 tag，其余书库 tag 为空 → v3 缓存照常命中。
    raw = f"{source_file}|{page_number}|{query_text}|{stamp}|v3{_render_profile(source_file)['tag']}"
    digest = sha256(raw.encode("utf-8")).hexdigest()
    return PAGE_IMAGE_CACHE_DIR / digest[:2] / f"{digest}.jpg"


# 阅读器页面图像清晰度参数。
# 历史固定按 1.45×（约 104 DPI）渲染，而阅读器最宽显示到 960px CSS（高分屏 ≈1920 设备像素），
# 图像被放大显示 → 文字发糊；《全集》《列宁全集》多为扫描件，叠加后更明显。
# 改为「按目标像素宽度自适应缩放」：让渲染像素宽尽量接近 TARGET_WIDTH，并用 MIN/MAX 夹住，
# 兼顾清晰度与渲染耗时 / 缓存体积 / 内存（列宁 60 卷）。
# 注意：刻意只用 PyMuPDF，不引入 Pillow——服务器运行环境（requirements.txt）未安装 Pillow，
# 若在此路径 import PIL 会触发 ImportError 导致页面渲染 502。
PAGE_IMAGE_TARGET_WIDTH = 1600.0   # 目标渲染像素宽度（适配高分屏 960px 显示）
PAGE_IMAGE_MIN_SCALE = 1.45        # 缩放下限：不低于历史清晰度，保证「只会更清晰不会更糊」
PAGE_IMAGE_MAX_SCALE = 3.0         # 缩放上限：把握分寸，控制文件体积 / 渲染耗时 / 内存
PAGE_IMAGE_JPEG_QUALITY = 90       # JPEG 质量：高分辨率下兼顾文字边缘锐利与体积

# 《毛泽东选集》为纯图像扫描件、原始分辨率偏低、肉眼偏糊。仅对该书库启用「更高渲染分辨率
# + 更高 JPEG 质量 + 轻度 USM 锐化」profile：以更高倍率重采样恢复扫描原生细节，再做一次
# 非锐化掩模提升笔画边缘对比。锐化依赖 numpy，按需探测，缺失时安全跳过（绝不让渲染 502）。
# 其它书库 profile 不变、tag 为空 → 既有 v3 缓存继续命中，无需全站重渲染。
MAO_RENDER = {
    "target_width": 2200.0, "min_scale": 2.0, "max_scale": 3.6,
    # sharpen 0.3：高分辨率重采样本身已显著提升清晰度，USM 仅做轻度提锐；0.8 会过锐
    # （笔画毛刺/底噪放大）。tag 改 +mao2 使既有过锐缓存失效、按新参数重渲染。
    "jpeg_quality": 94, "sharpen": 0.3, "tag": "+mao2",
}
_MAO_SCAN_PREFIX = "pdfs/《毛泽东选集》/"
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


def _render_profile(source_file: str) -> dict:
    """按书库返回渲染 profile。毛选用高分辨率+锐化；其余沿用历史参数（tag 空、缓存不失效）。"""
    if _normalize_source_file(source_file).startswith(_MAO_SCAN_PREFIX):
        return MAO_RENDER
    return {
        "target_width": PAGE_IMAGE_TARGET_WIDTH, "min_scale": PAGE_IMAGE_MIN_SCALE,
        "max_scale": PAGE_IMAGE_MAX_SCALE, "jpeg_quality": PAGE_IMAGE_JPEG_QUALITY,
        "sharpen": 0.0, "tag": "",
    }


def _unsharp_jpeg_bytes(pix, amount: float, quality: int):
    """对 PyMuPDF 像素图做一次轻度非锐化掩模（USM），返回 JPEG 字节；numpy 不可用或异常时返回
    None，调用方回退到原始 pix.tobytes。可分离 [1,2,1]/4 近似高斯模糊，amount 控制锐化强度。"""
    np = _numpy_or_none()
    if np is None or amount <= 0:
        return None
    try:
        h, w, n = pix.height, pix.width, pix.n
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, n).astype(np.float32)
        blur = (arr * 2 + np.roll(arr, 1, 1) + np.roll(arr, -1, 1)) / 4.0
        blur = (blur * 2 + np.roll(blur, 1, 0) + np.roll(blur, -1, 0)) / 4.0
        sharp = np.clip(arr + amount * (arr - blur), 0, 255).astype(np.uint8)
        out = fitz.Pixmap(pix.colorspace, w, h, sharp.tobytes(), pix.alpha)
        return out.tobytes("jpg", jpg_quality=quality)
    except Exception:
        return None


def _render_page_image_to_cache(source_file: str, page_number: int, query_text: str, *, matrix_scale: float = PAGE_IMAGE_MIN_SCALE) -> Path:
    cache_path = _page_image_cache_path(source_file, page_number, query_text)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    pdf_path = _resolve_pdf_path(source_file, require_full_mode=False)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f"{cache_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    with fitz.open(pdf_path) as doc:
        if page_number > doc.page_count:
            abort(404, description="请求页码超出 PDF 范围。")
        page = doc[page_number - 1]

        for term in _highlight_terms(query_text):
            rects = page.search_for(term, quads=False)
            if not rects:
                continue
            for rect in rects[:12]:
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=(1.0, 0.86, 0.2))
                annot.set_opacity(0.45)
                annot.update()
            break

        # 自适应缩放：按页面物理宽度（pt）算出贴近目标像素宽的缩放系数，再用下限/上限夹住。
        # profile 按书库选择（毛选高分辨率，其余历史参数）。任何异常都安全回退，绝不崩溃。
        profile = _render_profile(source_file)
        lo = max(matrix_scale, profile["min_scale"])
        try:
            page_width_pt = float(page.rect.width)
            adaptive_scale = profile["target_width"] / page_width_pt if page_width_pt > 0 else lo
            scale = max(lo, min(adaptive_scale, profile["max_scale"]))
        except Exception:
            scale = lo
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=True)
        data = _unsharp_jpeg_bytes(pix, profile["sharpen"], profile["jpeg_quality"])
        if data is None:  # numpy 不可用 / 锐化关闭 / 异常 → 直接输出原始像素图
            data = pix.tobytes("jpg", jpg_quality=profile["jpeg_quality"])
        temp_path.write_bytes(data)
    temp_path.replace(cache_path)
    return cache_path


def _prewarm_page_images(source_file: str, page_number: int, query_text: str, page_count: int) -> None:
    if query_text:
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
    g.membership = get_membership_snapshot(int(user["id"])) if user else get_membership_snapshot(None)
    g.site_texts = _effective_site_text_map()
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
        record_site_activity(
            session_key=_visitor_session_key(),
            user_id=int(user["id"]) if user else None,
            day=china_day_text(),
            feature=feature,
            path=request.path,
        )
    except Exception as exc:
        LOGGER.debug("Site activity recording failed: %s", exc)


@app.before_request
def enforce_csrf_for_state_changes():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return
    _require_csrf()


@app.context_processor
def inject_auth_context():
    membership = getattr(g, "membership", get_membership_snapshot(None))
    site_texts = getattr(g, "site_texts", get_site_text_map())

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

    return {
        "current_user": getattr(g, "current_user", None),
        "is_admin": _is_admin_user(getattr(g, "current_user", None)),
        "admin_console_available": _is_admin_user(getattr(g, "current_user", None)),
        "membership": _membership_to_dict(membership),
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
        "error": "今日 AI token 已达到限额。",
        "used_tokens": error.used,
        "daily_limit": error.limit,
        "reset_at": error.reset_at,
    }
    if request.path.startswith("/api/"):
        return jsonify(payload), 429
    return (
        render_template(
            "error.html",
            title="AI token 已用完",
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
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' https://challenges.cloudflare.com; "
        "frame-src https://challenges.cloudflare.com; "
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
                )
                session.clear()  # 防会话固定：注册登录前丢弃旧会话标识。
                session["user_id"] = user["id"]
                session.permanent = True
                update_last_login(int(user["id"]))
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
            session.clear()  # 防会话固定：登录成功即丢弃旧会话标识，再写入身份。
            session["user_id"] = user["id"]
            session.permanent = True
            update_last_login(int(user["id"]))
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
    plans = list_active_plans()
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
    order = create_pending_order(user_id=int(g.current_user["id"]), plan_code=plan_code)
    return _build_payment_checkout_redirect(order, plan, g.current_user)


@app.post("/checkout/order/<order_no>")
def retry_checkout(order_no: str):
    _require_login_page()
    order = get_order_by_no(order_no)
    if order is None or int(order["user_id"]) != int(g.current_user["id"]):
        abort(404, description="未找到对应订单。")
    if order["status"] == "paid":
        flash("该订单已支付，无需重新发起支付。", "info")
        return redirect(url_for("payment_result", order_no=order_no))
    if order["status"] != "pending":
        flash("当前订单状态不支持重新支付。", "warning")
        return redirect(url_for("account"))

    plan = get_plan(str(order["plan_code"]))
    if not plan or not plan.get("is_active"):
        abort(404, description="该订单对应的套餐已不可用。")
    return _build_payment_checkout_redirect(order, plan, g.current_user)


@app.get("/checkout/order/<order_no>/status")
def checkout_order_status(order_no: str):
    # 收银页轮询：返回订单是否已支付。仅订单所属用户可查询。
    if not getattr(g, "current_user", None):
        return jsonify({"ok": False, "status": "unauthorized"}), 401
    order = get_order_by_no(order_no)
    if order is None or int(order["user_id"]) != int(g.current_user["id"]):
        return jsonify({"ok": False, "status": "not_found"}), 404
    status = str(order["status"])
    return jsonify({"ok": True, "status": status, "paid": status == "paid"})


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
    # 首页「期刊订阅」栏目入口：展示本期（当前批次）已发布文章。
    # 严格按控制台「期刊提醒」权限放行（访客/未授权 → 403）。
    if not _feature_effective_for_user("journal_alerts"):
        abort(403, description="当前账号暂未开放期刊提醒权限。")
    # 优先展示最近一次已发送批次（发送后留存到下一期发送）；尚无发送则展示当前在建批次以便预览。
    batch = latest_public_batch()
    articles: list[dict] = []
    if batch:
        # 已批准/已发送(ready) 优先；若本批尚未审核，则展示待审(pending_review) 以便预览。
        articles = batch_articles(int(batch["id"]), statuses=("ready",))
        if not articles:
            articles = batch_articles(int(batch["id"]), statuses=("ready", "pending_review"))
    review_html = ""
    if batch and str(batch.get("review_status") or "") == "approved":
        review_html = str(batch.get("review_html") or "")
    return render_template(
        "journal_latest.html",
        title="本期期刊新文",
        app_name=APP_NAME,
        state=current_view_state(),
        batch=batch,
        articles=articles,
        review_html=review_html,
        can_subscribe=bool(_feature_effective_for_user("journal_alerts") and load_smtp_config().enabled),
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
    _refresh_ai_runtime_if_needed()
    action = (request.form.get("action") or "save").strip().lower()
    if action == "reset":
        reset_ai_overrides()
        _reload_ai_runtime()
        flash("AI 覆盖设置已清除，已恢复为项目配置文件/环境变量值。", "success")
        return redirect(url_for("control", section="ai"))

    current = AI_CONFIG.to_edit_dict()
    try:
        values = {
            "provider": (request.form.get("provider") or current["provider"]).strip() or AI_DEFAULT_PROVIDER,
            "model": (request.form.get("model") or current["model"]).strip() or AI_DEFAULT_MODEL,
            "base_url": (request.form.get("base_url") or current["base_url"]).strip().rstrip("/"),
            "api_key": (request.form.get("api_key") or "").strip(),
            "search_provider": (request.form.get("search_provider") or current["search_provider"]).strip() or "disabled",
            "search_base_url": (request.form.get("search_base_url") or current["search_base_url"]).strip().rstrip("/"),
            "search_api_key": (request.form.get("search_api_key") or "").strip(),
            "request_timeout_seconds": _form_int("request_timeout_seconds", int(current["request_timeout_seconds"])),
            "default_web_enabled": _form_bool("default_web_enabled"),
            "web_search_count": _form_int("web_search_count", int(current["web_search_count"])),
            "max_history_turns": _form_int("max_history_turns", int(current["max_history_turns"])),
            "search_history_turns": _form_int("search_history_turns", int(current["search_history_turns"])),
            "pdf_history_turns": _form_int("pdf_history_turns", int(current["pdf_history_turns"])),
            "search_message_char_limit": _form_int(
                "search_message_char_limit",
                int(current["search_message_char_limit"]),
            ),
            "pdf_message_char_limit": _form_int(
                "pdf_message_char_limit",
                int(current["pdf_message_char_limit"]),
            ),
            "search_answer_max_tokens": _form_int(
                "search_answer_max_tokens",
                int(current["search_answer_max_tokens"]),
            ),
            "pdf_answer_max_tokens": _form_int(
                "pdf_answer_max_tokens",
                int(current["pdf_answer_max_tokens"]),
            ),
            "pdf_quick_answer_max_tokens": _form_int(
                "pdf_quick_answer_max_tokens",
                int(current["pdf_quick_answer_max_tokens"]),
            ),
            "search_web_search_count": _form_int(
                "search_web_search_count",
                int(current["search_web_search_count"]),
            ),
            "pdf_web_search_count": _form_int(
                "pdf_web_search_count",
                int(current["pdf_web_search_count"]),
            ),
            "pdf_quick_web_search_count": _form_int(
                "pdf_quick_web_search_count",
                int(current["pdf_quick_web_search_count"]),
            ),
            "pdf_selected_text_char_limit": _form_int(
                "pdf_selected_text_char_limit",
                int(current["pdf_selected_text_char_limit"]),
            ),
            "pdf_current_text_char_limit": _form_int(
                "pdf_current_text_char_limit",
                int(current["pdf_current_text_char_limit"]),
            ),
            "pdf_adjacent_excerpt_char_limit": _form_int(
                "pdf_adjacent_excerpt_char_limit",
                int(current["pdf_adjacent_excerpt_char_limit"]),
            ),
            "pdf_quick_selected_text_char_limit": _form_int(
                "pdf_quick_selected_text_char_limit",
                int(current["pdf_quick_selected_text_char_limit"]),
            ),
            "pdf_quick_current_text_char_limit": _form_int(
                "pdf_quick_current_text_char_limit",
                int(current["pdf_quick_current_text_char_limit"]),
            ),
            "pdf_quick_adjacent_excerpt_char_limit": _form_int(
                "pdf_quick_adjacent_excerpt_char_limit",
                int(current["pdf_quick_adjacent_excerpt_char_limit"]),
            ),
            "temperature": _form_float("temperature", float(current["temperature"])),
        }
    except ValueError:
        flash("AI 设置中包含无效数字，请检查后再保存。", "warning")
        return redirect(url_for("control", section="ai"))

    save_ai_overrides(values)
    _reload_ai_runtime()
    flash("AI 设置已保存，并已在当前运行中立即生效。", "success")
    return redirect(url_for("control", section="ai"))


@app.post("/control/site-texts")
def control_site_texts():
    return _handle_site_texts_submit(remote_admin=False)
    action = (request.form.get("action") or "save").strip().lower()
    if action == "reset":
        reset_site_text_overrides()
        flash("站点说明文字已恢复默认值。", "success")
    else:
        save_site_text_overrides(_site_text_form_values())
        flash("站点说明文字已保存。", "success")
    return redirect(url_for("control", section="copy"))


@app.post("/control/plans")
def control_plans():
    return _handle_plans_submit(remote_admin=False)
    try:
        plan = upsert_plan(
            code=(request.form.get("code") or "").strip(),
            name=(request.form.get("name") or "").strip(),
            price_cents=_form_int("price_cents", 0),
            currency=(request.form.get("currency") or "CNY").strip() or "CNY",
            interval_months=_form_int("interval_months", 1),
            description=(request.form.get("description") or "").strip(),
            is_active=_form_bool("is_active"),
            sort_order=_form_int("sort_order", 0),
        )
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("control", section="plans"))

    flash(f"套餐 {plan.get('name') or plan.get('code') or ''} 已保存。", "success")
    return redirect(url_for("control", section="plans"))


@app.post("/control/memberships/grant")
def control_membership_grant():
    return _handle_membership_grant_submit(remote_admin=False)
    user_email = normalize_email(request.form.get("user_email") or "")
    plan_code = (request.form.get("plan_code") or "").strip()
    note = (request.form.get("note") or "").strip()
    try:
        result = create_manual_subscription(user_email=user_email, plan_code=plan_code, note=note)
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("control", section="members"))

    subscription = result.get("subscription") or {}
    flash(
        f"已为 {user_email} 开通 {subscription.get('plan_name') or plan_code}，到期 {subscription.get('expires_at') or '已更新'}。",
        "success",
    )
    return redirect(url_for("control", section="members"))


@app.post("/control/users/<int:user_id>")
def control_user_update(user_id: int):
    return _handle_user_update_submit(user_id, remote_admin=False)
    user_q = (request.form.get("user_q") or "").strip()
    updated = update_user_account(
        user_id,
        role=(request.form.get("role") or "member").strip() or "member",
        is_active=_form_bool("is_active"),
    )
    if updated is None:
        flash("未找到需要更新的用户。", "warning")
    else:
        flash(f"用户 {updated.get('email') or user_id} 已更新。", "success")
    return redirect(url_for("control", section="users", user_q=user_q))


@app.post("/admin/ai")
def admin_ai():
    return _handle_ai_settings_submit(remote_admin=True)


@app.post("/admin/site-texts")
def admin_site_texts():
    return _handle_site_texts_submit(remote_admin=True)


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


@app.post("/admin/memberships/grant")
def admin_membership_grant():
    return _handle_membership_grant_submit(remote_admin=True)


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


@app.post("/admin/journal-alerts/run")
def admin_journal_run():
    _require_admin()
    _require_management_csrf()
    action = (request.form.get("action") or "send").strip().lower()

    # 新批次化操作：仅采集 / 生成综述 / 发送综述。
    if action in {"collect", "fetch_only"}:
        result = collect_batch(ai_client=AI_CLIENT)
        if result.get("status") == "failed":
            flash(f"采集失败：{result.get('error') or '未知错误'}", "warning")
        else:
            flash(
                "采集完成（批次 #{batch}）：发现 {found} 篇，超出时间窗已过滤 {filtered} 篇，"
                "新增 {inserted} 篇，本批共纳入 {total} 篇（待审 {pending} 篇）。".format(
                    batch=result.get("batch_id"),
                    found=result.get("articles_found", 0),
                    filtered=result.get("filtered_out", 0),
                    inserted=result.get("articles_inserted", 0),
                    total=result.get("batch_total", 0),
                    pending=result.get("batch_pending", 0),
                ),
                "success" if not result.get("error") else "warning",
            )
        return _management_redirect(True, "journal-alerts")

    if action == "generate_review":
        batch = current_batch()
        if not batch:
            flash("当前没有可生成综述的批次，请先采集。", "warning")
            return _management_redirect(True, "journal-alerts")
        if not AI_CONFIG.enabled:
            flash("AI 未启用，将生成降级版综述（仅分组列出标题与引文）。", "warning")
        try:
            generate_batch_review(int(batch["id"]), ai_client=AI_CLIENT, auto_approve=False)
        except Exception as exc:
            flash(f"综述生成失败：{exc}", "warning")
        else:
            flash(f"已生成批次 #{batch['id']} 的文献综述，请在下方审核。", "success")
        return _management_redirect(True, "journal-alerts")

    if action in {"send_batch", "send"}:
        smtp_config = load_smtp_config()
        if not smtp_config.enabled:
            flash("全站发信邮箱尚未配置，无法发送。", "warning")
            return _management_redirect(True, "journal-alerts")
        try:
            outcome = send_journal_batch(
                base_url=journal_alert_public_base_url(DEPLOYMENT),
                smtp_config=smtp_config,
                force=True,
            )
        except Exception as exc:
            flash(f"发送失败：{exc}", "warning")
            return _management_redirect(True, "journal-alerts")
        reason = outcome.get("reason")
        if outcome.get("sent"):
            flash(f"已发送 {outcome['sent']} 封文献综述邮件。", "success")
        elif reason == "review_not_approved":
            flash("综述尚未批准，已强制发送但无可发对象，请确认综述状态。", "warning")
        elif reason == "review_empty":
            flash("当前批次还没有综述内容，请先生成综述。", "warning")
        elif reason == "no_batch":
            flash("当前没有可发送的批次。", "warning")
        else:
            flash("没有需要发送的新订阅者（可能均已发送）。", "success")
        return _management_redirect(True, "journal-alerts")

    if action == "send_only":
        # 兼容旧版：把已就绪文章逐篇发出（不重新抓取，不走综述）。
        smtp_config = load_smtp_config()
        if not smtp_config.enabled:
            flash("全站发信邮箱尚未配置，无法发送。", "warning")
            return _management_redirect(True, "journal-alerts")
        try:
            sent = deliver_ready_journal_articles(
                base_url=journal_alert_public_base_url(DEPLOYMENT),
                smtp_config=smtp_config,
            )
        except Exception as exc:
            flash(f"发送失败：{exc}", "warning")
        else:
            flash(f"已发送 {sent} 封期刊摘要邮件。", "success")
        return _management_redirect(True, "journal-alerts")

    # 兜底：采集并尝试发送（旧行为）。
    result = run_journal_alerts_once(
        ai_client=AI_CLIENT,
        base_url=journal_alert_public_base_url(DEPLOYMENT),
        smtp_config=load_smtp_config(),
        send=True,
    )
    if result.get("status") == "failed":
        flash(f"期刊提醒检测失败：{result.get('error') or '未知错误'}", "warning")
    else:
        flash(
            "期刊提醒检测并发送完成：发现 {found} 篇，新增 {inserted} 篇，发送 {sent} 封。".format(
                found=result.get("articles_found", 0),
                inserted=result.get("articles_inserted", 0),
                sent=result.get("emails_sent", 0),
            ),
            "success" if not result.get("error") else "warning",
        )
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


@app.post("/admin/journal-alerts/digest/<int:digest_id>/review")
def admin_journal_digest_review(digest_id: int):
    _require_admin()
    _require_management_csrf()
    action = (request.form.get("action") or "").strip().lower()
    if action == "regenerate":
        try:
            generate_batch_review(digest_id, ai_client=AI_CLIENT, auto_approve=False)
        except Exception as exc:
            flash(f"综述重新生成失败：{exc}", "warning")
        else:
            flash("综述已重新生成，请审核。", "success")
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
    try:
        recipients, enforce_permission = resolve_journal_recipients(
            mode, plan_codes=plan_codes, emails=emails
        )
        if not recipients:
            flash("所选受众没有可用收件人。", "warning")
            return _management_redirect(True, "journal-alerts")
        outcome = send_journal_batch(
            base_url=journal_alert_public_base_url(DEPLOYMENT),
            smtp_config=smtp_config,
            digest_id=digest_id,
            force=True,
            recipients=recipients,
            enforce_permission=enforce_permission,
        )
    except Exception as exc:
        flash(f"发送失败：{exc}", "warning")
        return _management_redirect(True, "journal-alerts")
    mode_label = {
        "subscribers": "邮箱订阅者", "members": "付费会员",
        "registered": "全部注册用户", "specific": "特定邮箱",
    }.get(mode, mode)
    if outcome.get("sent"):
        flash(f"已向「{mode_label}」发送 {outcome['sent']} 封文献综述邮件。", "success")
    elif outcome.get("reason") == "review_empty":
        flash("当前批次还没有综述内容，请先生成综述。", "warning")
    else:
        flash(f"「{mode_label}」中没有需要发送的新收件人（可能均已发送）。", "success")
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
    _require_login_page()
    order_no = (request.args.get("order_no") or "").strip()
    if not order_no:
        abort(400, description="缺少订单号。")
    order = get_order_by_no(order_no)
    if order is None or int(order["user_id"]) != int(g.current_user["id"]):
        abort(404, description="未找到对应订单。")
    membership_snapshot = get_membership_snapshot(int(g.current_user["id"]))
    return render_template(
        "payment_result.html",
        title="支付结果",
        order=order,
        membership_snapshot=_membership_to_dict(membership_snapshot),
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

    if order_no:
        record_payment_event(
            order_no=order_no,
            provider="zpay",
            event_type="return",
            payload=params,
        )

    if not verified:
        flash("支付状态暂时无法确认，请稍后刷新会员中心，或联系客服核对。", "warning")
        return redirect(url_for("payment_result", order_no=order_no)) if order_no else redirect(url_for("account"))

    order = get_order_by_no(order_no)
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
    if order_no:
        record_payment_event(
            order_no=order_no,
            provider="zpay",
            event_type="notify",
            payload=params,
        )

    if not PAYMENT_CONFIG.enabled:
        return "failure"
    if not params or not PAYMENT_CLIENT.verify_callback_params(params):
        LOGGER.warning("ZPay notify verify failed for order=%s", order_no or "<missing>")
        return "failure"

    order = get_order_by_no(order_no)
    if order is None:
        LOGGER.warning("ZPay notify order not found: %s", order_no)
        return "failure"

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


@app.route("/")
def index():
    state = current_view_state()
    current_user = getattr(g, "current_user", None)
    feedback_thread = (
        get_feedback_user_thread(int(current_user["id"]), mark_seen=False)
        if current_user
        else None
    )
    return render_template(
        "index.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        request_token=REQUEST_TOKEN if state["management_api_enabled"] else None,
        state=state,
        ai_runtime=_public_ai_runtime_payload(allow_details=bool(state["feature_access"].get("ai", False))),
        n_wenji=n_wenji,
        n_quanji=n_quanji,
        book_stats=book_stats,
        plans=list_active_plans(),
        reader_entries=_reader_access_entries(),
        chapter_search=_chapter_search_access(),
        member_access_enabled=bool(_feature_is_available("library") and _feature_effective_for_user("library")),
        ai_access_enabled=bool(_feature_is_available("ai") and _feature_effective_for_user("ai")),
        feedback_thread=feedback_thread,
    )


def _library_volumes(*, basic_reader_mode: bool = False) -> list[dict]:
    volumes = []
    for book_cfg in BOOK_CONFIGS:
        book = book_cfg.key
        for volume in (corpus.get_volumes(book) if corpus else []):
            # 仅取目录“条数”用于卷头标签；目录条目本身改由 /api/library/volume-toc 在展开该卷时
            # 按需拉取（见 library.html）。避免每次进阅读器就把上万条目录全量渲染进 DOM 致卡顿，
            # 也省去每请求物化两万条 dict 的服务端开销。
            toc_count = len(corpus.get_toc_entries(volume.source_file)) if corpus else 0
            viewer_args = {"file": volume.source_file, "page": 1}
            if basic_reader_mode:
                viewer_args["mode"] = "reader"
            volumes.append(
                {
                    "book": book,
                    "volume": volume.volume,
                    "display_title": volume.display_title,
                    **_book_payload(book),
                    "source_file": volume.source_file,
                    "page_count": len(volume.pages),
                    "toc_count": toc_count,
                    "viewer_url": url_for("pdf_viewer", **viewer_args),
                    "pdf_url": url_for("serve_pdf", file=volume.source_file),
                }
            )
    volumes.sort(key=lambda item: (item["book_sort_order"], item["volume"], item["source_file"]))
    return volumes


@app.route("/reader")
def reader():
    _require_content_feature("library")
    _require_search()
    return render_template(
        "library.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        state=current_view_state(),
        volumes=_library_volumes(basic_reader_mode=True),
        reader_mode=True,
        ai_access_enabled=bool(_feature_is_available("ai") and _feature_effective_for_user("ai")),
    )


@app.route("/library")
def library():
    _require_content_feature("library")
    _require_search()
    return render_template(
        "library.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        state=current_view_state(),
        volumes=_library_volumes(),
        reader_mode=False,
        ai_access_enabled=bool(_feature_is_available("ai") and _feature_effective_for_user("ai")),
    )


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


@app.route("/api/library/journal-diag")
def api_journal_diag():
    try:
        return jsonify(journal_abstract_diag())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": repr(exc)[:200]})


@app.route("/api/library/ncpssd-probe")
def api_ncpssd_probe():
    # 诊断用：从服务器侧探测 NCPSSD 摘要接口是否可达（固定 URL、无用户输入、不含敏感信息）。
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
    qn = _toc_norm(raw)
    if len(qn) < 2:
        return jsonify({"ok": True, "results": []})
    ranked_matches: list[tuple] = []
    for item in _get_toc_suggest_index():
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
    results = []
    for *_, item in ranked_matches[:20]:
        results.append(
            {
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
    return jsonify({"ok": True, "results": results})
    matches: list[tuple] = []
    for item in _get_toc_suggest_index():
        pos = item["norm"].find(qn)
        if pos < 0:
            continue
        # 排序键：命中位置靠前者优先 → 标题更短者 → 书库配置顺序 → 卷序靠前。
        matches.append((pos, len(item["title"]), item.get("book_sort_order", 9999), item["volume"], item))
    matches.sort(key=lambda m: (m[0], m[1], m[2], m[3]))
    results = []
    for _, _, _, _, item in matches[:20]:
        results.append(
            {
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
    return jsonify({"ok": True, "results": results})


def _warm_toc_suggest_index() -> None:
    try:
        _get_toc_suggest_index()
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
    if render_mode == "image":
        try:
            threading.Thread(
                target=lambda: _render_page_image_to_cache(source_file, page, highlight_text),
                daemon=True,
            ).start()
        except Exception:
            pass
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
    return render_template(
        "viewer.html",
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
        toc_entries=toc_entries,
        current_section=current_section,
        volume=volume,
        query_text=query_text,
        highlight_text=highlight_text,
        ai_upsell=_ai_reader_upsell(url_for("pdf_viewer", **ai_viewer_args)),
        ai_access_enabled=bool(_feature_is_available("ai") and _feature_effective_for_user("ai")),
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
    cache_path = _render_page_image_to_cache(source_file, page_number, highlight_text)
    volume = corpus.get_volume_by_source_file(source_file) if corpus else None
    page_count = len(volume.pages) if volume else page_number
    _prewarm_page_images(source_file, page_number, highlight_text, page_count)
    return send_file(cache_path, mimetype="image/jpeg", conditional=True, max_age=86400)


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
    # 兼容两种提交：multipart/form-data（带图片）与历史的 application/json（纯文本）。
    if request.files:
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


@app.route("/api/pdf-page-context")
def api_pdf_page_context():
    _require_reader_asset_access()
    source_file = _normalize_source_file((request.args.get("file") or "").strip())
    page = max(1, request.args.get("page", type=int) or 1)
    context = _get_page_context_payload(source_file, page)
    return jsonify({"ok": True, "context": context})


def _attach_viewer_payload(hit: dict, q_for_viewer: str, viewer_allowed: bool) -> dict:
    hit.update(_book_payload(str(hit.get("book") or "")))
    highlight_text = _hit_highlight_text(hit, q_for_viewer)
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


def _chaptered_search_payload(q, book_filter, requested_group_page, viewer_allowed, user):
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
    if book_filter:
        volumes = [v for v in volumes if str(v.get("book") or "") == book_filter]
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

    book_filter = str(payload.get("book") or "").strip()
    if book_filter not in BOOK_CONFIG_BY_KEY:
        book_filter = ""

    # 短词海量命中专用通道：完整聚合全部卷/篇章的准确命中数（C 层级计数，约 0.1 秒），
    # 命中详情交由 /api/search/chapter-hits 按需分页物化，从而“全部呈现”又不拖垮服务。
    if not DEPLOYMENT.is_desktop and len(q_norm) <= SHORT_QUERY_CHAPTER_MAX_LEN:
        payload_chaptered = _chaptered_search_payload(
            q, book_filter, requested_group_page, viewer_allowed, user
        )
        if payload_chaptered is not None:
            return jsonify(payload_chaptered)

    try:
        grouped = corpus.search_grouped(q, group_limit=1000000, max_hits=None)
    except Exception as exc:
        LOGGER.warning("Search failed for query=%r user=%s: %s", q[:80], user.get("id") if user else "guest", exc)
        return jsonify({"ok": False, "error": "查询解析失败，请调整关键词后重试。"}), 400

    # 长词海量命中：常规分组路径会按 EXACT_HITS_PER_BOOK(200/库) 截断；一旦发生截断，
    # 改走与短词相同的“完整篇章聚合”通道——给出全部卷/篇章的完整命中计数，详情按需展开，
    # 从而彻底消除 200 条/库 的截断、命中全部可达（不一次性物化以保稳定）。
    if not DEPLOYMENT.is_desktop and grouped.get("truncated"):
        payload_chaptered = _chaptered_search_payload(
            q, book_filter, requested_group_page, viewer_allowed, user
        )
        if payload_chaptered is not None:
            return jsonify(payload_chaptered)

    all_groups = []
    for group in grouped["groups"]:
        hits = [_attach_viewer_payload(hit, q_for_viewer, viewer_allowed) for hit in group["hits"]]
        group["hits"] = hits
        all_groups.append(group)

    # 书库筛选标签：先按配置书库统计各书命中分组数（过滤前），再按所选书库过滤。
    book_counts = _search_book_counts(all_groups)
    book_filter = str(payload.get("book") or "").strip()
    if book_filter not in BOOK_CONFIG_BY_KEY:
        book_filter = ""
    if book_filter:
        all_groups = [g for g in all_groups if str(g.get("book") or "") == book_filter]
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
        not DEPLOYMENT.is_desktop
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


@app.route("/api/ai/search-chat", methods=["POST"])
def api_ai_search_chat():
    _require_content_feature("ai")
    _rate_limit_ai_or_abort()
    quota = _require_ai_quota_or_raise()
    if DEPLOYMENT.is_desktop:
        payload = request.get_json(silent=True) or {}
        try:
            answer = proxy_desktop_ai("/api/desktop/ai/search-chat", payload)
            _record_ai_usage(
                quota,
                feature="search-chat",
                prompt_parts=(payload.get("messages") or [], payload.get("question") or ""),
                completion_text=str(answer.get("answer_markdown") or ""),
                success=bool(answer.get("ok", True)),
                error="" if answer.get("ok", True) else str(answer.get("error") or ""),
            )
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
    question = " ".join(str(payload.get("question") or "").split())
    messages = payload.get("messages") or []
    if not question:
        return jsonify({"ok": False, "error": "问题不能为空。"}), 400
    try:
        answer = AI_CLIENT.answer_search_chat(messages, question)
    except AIServiceError as exc:
        LOGGER.warning("Search AI failed: %s", exc)
        _record_ai_usage(
            quota,
            feature="search-chat",
            prompt_parts=(messages, question),
            success=False,
            error=str(exc),
        )
        return jsonify({"ok": False, "error": str(exc)}), 502
    _record_ai_usage(
        quota,
        feature="search-chat",
        prompt_parts=(messages, question),
        completion_text=answer.answer_markdown,
        success=True,
    )
    return jsonify(answer.to_dict())


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


def _split_gist_terms(gist: str) -> list[str]:
    """把用户输入按空白/标点切成词（用户常用空格分隔“著作名 主题”），用于 LLM 无果时的确定性兜底检索。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _GIST_SPLIT_RE.split(gist or ""):
        term = raw.strip()
        if len(normalize(term)) >= 2 and term not in seen:
            seen.add(term)
            out.append(term)
        if len(out) >= 8:
            break
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
        ordered.append(candidates[idx])
        rationale.append({"confidence": conf, "reason": reason})
    return ordered, rationale


@app.route("/api/search/associative", methods=["POST"])
def api_search_associative():
    """联想检索：AI 提取线索 → 在真实语料中接地定位 → AI 重排并解释。

    引文不可伪造：仅渲染 corpus.locate_associative 产出的真实命中；AI 只输出检索串与
    “在候选里选哪几条”。鉴权顺序与 /api/ai/search-chat 完全一致。
    """
    _require_content_feature("ai")
    _rate_limit_ai_or_abort()
    quota = _require_ai_quota_or_raise()
    if DEPLOYMENT.is_desktop:
        # 联想检索需与内存中的 corpus 同进程完成接地定位，桌面模式暂不经代理提供。
        return jsonify({"ok": False, "error": "联想检索暂仅在云端模式可用。"}), 200
    _require_ai()
    payload = request.get_json(silent=True) or {}
    gist = " ".join(str(payload.get("gist") or payload.get("q") or "").split())
    rerank = _coerce_bool(payload.get("rerank", True))
    if not gist:
        return jsonify({"ok": False, "error": "请描述你要找的内容（大意或关键词）。"}), 400
    if len(gist) > 600:
        gist = gist[:600]

    state = current_view_state()
    viewer_allowed = bool(state["pdf_enabled"] and _content_access_enabled("viewer"))

    try:
        plan = AI_CLIENT.expand_associative_query(gist)
    except AIServiceError as exc:
        LOGGER.warning("Associative expand failed gist=%r: %s", gist[:80], exc)
        _record_ai_usage(quota, feature="associative", prompt_parts=(gist,), success=False, error=str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 502

    quotes, fragments, keywords, chapter_keywords = _parse_assoc_plan(plan)
    raw_terms = _split_gist_terms(gist)
    try:
        # 一档：用 LLM 抽取的（已分好类的）线索检索——排序最干净
        candidates = []
        if quotes or fragments or keywords or chapter_keywords:
            candidates = corpus.locate_associative(
                quotes=quotes, keywords=keywords, fragments=fragments, chapter_keywords=chapter_keywords,
            )
        # 二档兜底：LLM 无果或未命中时，用用户原词直接检索，确保“总能搜到”（不污染一档的干净排序）
        if not candidates and (raw_terms or gist):
            candidates = corpus.locate_associative(
                quotes=[gist] if gist else [],
                keywords=raw_terms,
                fragments=raw_terms,
                chapter_keywords=raw_terms,
            )
    except Exception as exc:
        LOGGER.warning("Associative locate failed gist=%r: %s", gist[:80], exc)
        _record_ai_usage(quota, feature="associative", prompt_parts=(gist,), success=False, error=str(exc))
        return jsonify({"ok": False, "error": "联想检索失败，请稍后再试。"}), 400

    if not candidates:
        _record_ai_usage(quota, feature="associative", prompt_parts=(gist,), success=True)
        return jsonify({
            "ok": True, "query": gist, "count": 0, "display_mode": "associative",
            "access_level": "full" if viewer_allowed else "summary", "results": [],
            "pdf_enabled": viewer_allowed, "warnings": [],
            "message": "未在语料中定位到匹配段落，请换一种说法或补充更具体的关键词、人名或术语。",
        })

    # 候选已按综合权重降序。权重是主排序；AI 仅对权重最高的一小批做标注/解释（候选多时控成本），
    # 不丢弃任何已接地的候选——聚合展示靠权重优先呈现最可能段落。
    warnings: list[str] = []
    meta_by_id: dict[int, dict] = {}
    if rerank:
        head = candidates[:ASSOC_RERANK_TOP]
        try:
            ranking = AI_CLIENT.rank_associative_candidates(gist, [h.to_dict() for h in head])
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
        meta = meta_by_id.get(id(hit), {})
        d["associative_confidence"] = meta.get("confidence")
        d["associative_reason"] = meta.get("reason") or ""
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
    })


@app.route("/api/ai/pdf-chat", methods=["POST"])
def api_ai_pdf_chat():
    _require_content_feature("ai")
    _rate_limit_ai_or_abort()
    quota = _require_ai_quota_or_raise()
    if DEPLOYMENT.is_desktop:
        payload = request.get_json(silent=True) or {}
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
        LOGGER.warning("PDF AI failed: %s", exc)
        _record_ai_usage(
            quota,
            feature="pdf-chat",
            prompt_parts=(messages, question, selected_text, page_context),
            success=False,
            error=str(exc),
        )
        return jsonify({"ok": False, "error": str(exc)}), 502
    _record_ai_usage(
        quota,
        feature="pdf-chat",
        prompt_parts=(messages, question, selected_text, page_context),
        completion_text=answer.answer_markdown,
        success=True,
    )
    return jsonify(answer.to_dict())


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.route("/api/ai/pdf-chat-stream", methods=["POST"])
def api_ai_pdf_chat_stream():
    _require_content_feature("ai")
    _rate_limit_ai_or_abort()
    quota = _require_ai_quota_or_raise()
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
    page_context = _get_page_context_payload(source_file, page)

    def _generate():
        chunks: list[str] = []
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
            )
            for text in AI_CLIENT.chat_complete_stream(model_messages, max_tokens):
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
            )
            yield _sse_event(
                "done",
                {
                    "ok": True,
                    "answer_markdown": answer_text,
                    "sources": sources,
                    "warnings": warnings,
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
    serve_kwargs = dict(
        host=DEPLOYMENT.bind_host,
        port=DEPLOYMENT.port,
        threads=8,
        connection_limit=200,    # 并发连接上限，避免连接被慢连接/洪水占满（不设 channel_timeout，
        cleanup_interval=30,     # 以免误伤耗时较长的 AI 请求；慢连接超时交给前置 Caddy）
    )
    try:
        serve(app, clear_untrusted_proxy_headers=False, **serve_kwargs)
    except TypeError:
        LOGGER.warning("waitress 不支持 clear_untrusted_proxy_headers，退回默认启动(真实 IP 透传可能失效)")
        serve(app, **serve_kwargs)


def main() -> None:
    if DEPLOYMENT.is_server:
        run_waitress()
        return
    run_desktop()


if __name__ == "__main__":
    main()
