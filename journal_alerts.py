from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from admin_store import get_setting, init_admin_store_db
from feature_access import feature_allowed_by_policy, load_access_policy
from membership import init_membership_db, normalize_email
from runtime_env import APPDATA_DIR, DeploymentSettings


DB_PATH = APPDATA_DIR / "membership.sqlite3"
DEFAULT_LOOKBACK_DAYS = 45
HTTP_TIMEOUT_SECONDS = 25
# 每轮最多调用多少次 NCPSSD 详情接口补全摘要。需足够大，使“新增文章内联补全”与
# “历史缺摘要回填”都能在一轮内完成，避免大批量新增时把回填预算挤占干净造成长期缺摘要。
NCPSSD_ENRICH_PER_RUN = 400
USER_AGENT = "marx-search-journal-alerts/1.0 (+https://example.com)"
# 抓取中文期刊网站（NCPSSD 等）时使用更接近浏览器的 UA，避免被 bot UA 拒绝。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_ALERT_SETTINGS = {
    "subject_prefix": "期刊新文每日摘要",
    "intro_text": "您好，以下是今日汇总的新公开发表相关期刊文章：",
    "include_title": True,
    "include_journal": True,
    "include_authors": True,
    "include_published_at": True,
    "include_abstract": True,
    "include_citation": True,
    "include_url": True,
    # 全局自动发送：开启后每日抓取的新文章无需人工审核，直接进入发送队列。
    "auto_publish_all": False,
    # 发送频率：daily/weekly/biweekly/monthly；weekly/biweekly 在 send_weekday(0=周一..6=周日) 当天发送。
    "send_frequency": "weekly",
    "send_weekday": 0,
    # 抓取时间范围（天）：只收录最近 N 天内发表的文章，保证时效性（对 OpenAlex/Crossref 生效）。
    "lookback_days": 30,
    # 发送日的发送时间（北京时间 HH:MM）。采集在发送日前一天 19:00（由 systemd timer 控制）。
    "send_time": "08:00",
    # 综述生成的自动化分级开关（按发送批次快照）：
    "auto_approve_articles": False,  # 抓到的文章自动批准（跳过人工审核）
    "auto_generate_review": False,   # 采集后自动调用 AI 生成文献综述
    "auto_send": False,              # 综述自动批准并在发送日自动群发（全流程自动化）
    # 总闸：开启后采集/综述/发送各阶段一律暂停，便于随时人工干预。
    "automation_paused": False,
    # 归档的旧批次文章是否在下次采集时硬删除（默认仅归档保留）。
    "hard_delete_archived": False,
    # 综述专用模型（留空=沿用 ai.override.yaml 的运行时生效模型，自动适配 flash/pro）。
    "review_model": "",
    # 定时自动发送的默认受众：subscribers（邮箱订阅者，按权限）/ members（付费会员）/ registered（全部注册用户）。
    "send_audience": "subscribers",
    # 当 send_audience=members 时，限定的套餐 code 列表；为空=全部有效付费会员。
    "send_audience_plans": [],
}

SEND_FREQUENCIES = ("daily", "weekly", "biweekly", "monthly")
_SEND_INTERVAL_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 28}
BEIJING_TZ = timezone(timedelta(hours=8))


CN_JOURNAL_SOURCE_DEFAULTS: dict[str, dict[str, Any]] = {
    "马克思主义研究": {"issn": "1006-5199"},
    "马克思主义与现实": {"issn": "1004-5961"},
    "求是": {"issn": "1002-4980"},
    "教学与研究": {"issn": "0257-2826"},
    "社会主义研究": {"issn": "1001-4527"},
    "毛泽东邓小平理论研究": {"issn": "1005-8273"},
    "中国特色社会主义研究": {"issn": "1006-6470"},
    "科学社会主义": {"issn": "1002-1493"},
    "中共党史研究": {"issn": "1003-3815"},
    "党的文献": {"issn": "1005-1597"},
    "思想理论教育导刊": {"issn": "1009-2528"},
    "思想教育研究": {"issn": "1002-5707"},
    "思想理论教育": {"issn": "1007-192X"},
    "高校马克思主义理论研究": {"issn": "2096-1170"},
    "当代世界社会主义问题": {"issn": "1001-5574"},
    "国外理论动态": {"issn": "1674-1277"},
    "红旗文稿": {"issn": "2095-1817"},
    "理论视野": {"issn": "1008-1747"},
}


# 已核对（刊名+ISSN 与 ncpssd.cn 期刊页一致）的 NCPSSD 期刊号(gch)。
# 这些期刊的当期文章列表由 https://www.ncpssd.cn/journal/details?gch=<gch> 服务端直出，可解析。
# 未列出的中文期刊保持 manual，管理员可在控制台填入 gch 后启用自动抓取。
CN_JOURNAL_NCPSSD_GCH: dict[str, str] = {
    "马克思主义研究": "80453X",
    "马克思主义与现实": "80390X",
    "社会主义研究": "82324X",
    "中国特色社会主义研究": "81828X",
    "科学社会主义": "83729X",
    "中共党史研究": "81413X",
    "党的文献": "81382X",
    "思想理论教育导刊": "82718X",
    "思想理论教育": "82576B",
    "高校马克思主义理论研究": "72234X",
    "当代世界社会主义问题": "83093X",
    "理论视野": "81578X",
    "求是": "91584X",
    "红旗文稿": "81256A",
    "教学与研究": "96928X",
}

# 这些为权威/官方刊物，默认标记为可信来源（抓到即自动发送，无需人工审核）。
CN_JOURNAL_TRUSTED: set[str] = {"求是", "红旗文稿", "教学与研究"}

NCPSSD_JOURNAL_BASE = "https://www.ncpssd.cn/journal/details"


def _ncpssd_journal_config(gch: str, auto_publish: bool = False) -> dict[str, Any]:
    details = f"{NCPSSD_JOURNAL_BASE}?gch={gch}"
    return {
        "source_type": "web_html",
        "source_url": details,
        "config": {
            "parser": "ncpssd_journal",
            "gch": gch,
            "entry_url": details,
            # 默认先进人工审核；可信刊物或管理员核对后可置为自动发送。
            "auto_publish": auto_publish,
        },
    }


def _default_source(name: str, language: str = "zh") -> dict[str, Any]:
    values = dict(CN_JOURNAL_SOURCE_DEFAULTS.get(name, {}))
    issn = str(values.get("issn") or "").strip()
    if language == "zh" and "source_type" not in values:
        gch = CN_JOURNAL_NCPSSD_GCH.get(name, "")
        if gch:
            values.update(_ncpssd_journal_config(gch, auto_publish=name in CN_JOURNAL_TRUSTED))
        elif issn:
            # 暂无 NCPSSD 刊号的中文刊，回退到 OpenAlex（按 ISSN），保证来源“已配置”而非待补充。
            values["source_type"] = "openalex"
    return {
        "name": name,
        "language": language,
        "issn": issn,
        "source_type": values.get("source_type", "manual"),
        "source_url": values.get("source_url", ""),
        "config": values.get("config", {}),
    }


DEFAULT_JOURNAL_SOURCES: tuple[dict[str, Any], ...] = (
    _default_source("马克思主义研究"),
    _default_source("马克思主义与现实"),
    _default_source("求是"),
    _default_source("教学与研究"),
    _default_source("社会主义研究"),
    _default_source("毛泽东邓小平理论研究"),
    _default_source("中国特色社会主义研究"),
    _default_source("科学社会主义"),
    _default_source("中共党史研究"),
    _default_source("党的文献"),
    _default_source("思想理论教育导刊"),
    _default_source("思想教育研究"),
    _default_source("思想理论教育"),
    _default_source("高校马克思主义理论研究"),
    _default_source("当代世界社会主义问题"),
    _default_source("国外理论动态"),
    _default_source("红旗文稿"),
    _default_source("理论视野"),
    {
        "name": "Historical Materialism: Research in Critical Marxist Theory",
        "language": "en",
        "issn": "1465-4466",
        "source_type": "openalex",
    },
    {"name": "Rethinking Marxism", "language": "en", "issn": "0893-5696", "source_type": "openalex"},
    {
        "name": "Science & Society: A Journal of Marxist Thought and Analysis",
        "language": "en",
        "issn": "0036-8237",
        "source_type": "openalex",
    },
    {"name": "Capital & Class", "language": "en", "issn": "0309-8168", "source_type": "openalex"},
    {"name": "Monthly Review", "language": "en", "issn": "0027-0520", "source_type": "openalex"},
    {"name": "New Left Review", "language": "en", "issn": "0028-6060", "source_type": "openalex"},
    {"name": "Critique: Journal of Socialist Theory", "language": "en", "issn": "0301-7605", "source_type": "openalex"},
    {"name": "Socialist Register", "language": "en", "issn": "0081-0606", "source_type": "openalex"},
    {"name": "International Critical Thought", "language": "en", "issn": "2159-8282", "source_type": "openalex"},
    {"name": "Capitalism Nature Socialism", "language": "en", "issn": "1045-5752", "source_type": "openalex"},
    {"name": "Cambridge Journal of Economics", "language": "en", "issn": "0309-166X", "source_type": "openalex"},
    {"name": "Review of Radical Political Economics", "language": "en", "issn": "0486-6134", "source_type": "openalex"},
    {"name": "Review of Political Economy", "language": "en", "issn": "0953-8259", "source_type": "openalex"},
    {"name": "Journal of Economic Issues", "language": "en", "issn": "0021-3624", "source_type": "openalex"},
    {"name": "Structural Change and Economic Dynamics", "language": "en", "issn": "0954-349X", "source_type": "openalex"},
    {"name": "Economic Geography", "language": "en", "issn": "0013-0095", "source_type": "openalex"},
    {"name": "Journal of Institutional Economics", "language": "en", "issn": "1744-1374", "source_type": "openalex"},
    {"name": "International Journal of Political Economy", "language": "en", "issn": "0891-1916", "source_type": "openalex"},
    {"name": "New Political Economy", "language": "en", "issn": "1356-3467", "source_type": "openalex"},
    {"name": "Review of Development Economics", "language": "en", "issn": "1363-6669", "source_type": "openalex"},
    {"name": "Economy and Society", "language": "en", "issn": "0308-5147", "source_type": "openalex"},
)


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    username: str
    password: str
    from_email: str
    from_name: str
    use_tls: bool

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.port and self.from_email)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_text() -> str:
    return utc_now().isoformat(timespec="seconds")


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _connect() -> sqlite3.Connection:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """幂等地为已存在的表补充列：仅在列缺失时 ALTER TABLE ADD COLUMN，不重建表、不丢数据。"""
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_digest_deliveries(conn: sqlite3.Connection) -> None:
    """把 journal_digest_deliveries 迁到新结构（subscription_id 可空、加 user_id、去重按 email）。

    旧结构 `subscription_id NOT NULL + FK + UNIQUE(digest_id,subscription_id)` 无法记录非订阅收件人。
    幂等：仅当缺少 `user_id` 列（=旧结构）时重建，**保留旧行**。线上低风险（该表仅本功能的发送日志）。
    """
    cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(journal_digest_deliveries)").fetchall()}
    if not cols or "user_id" in cols:
        return  # 表不存在（上面的 CREATE 已建新表）或已是新结构。
    conn.executescript(
        """
        CREATE TABLE journal_digest_deliveries_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_id INTEGER NOT NULL,
            subscription_id INTEGER,
            user_id INTEGER,
            email TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(digest_id, email),
            FOREIGN KEY (digest_id) REFERENCES journal_digests(id)
        );
        INSERT OR IGNORE INTO journal_digest_deliveries_new(
            id, digest_id, subscription_id, user_id, email, status, error, created_at
        )
        SELECT id, digest_id, subscription_id, NULL, email, status, error, created_at
        FROM journal_digest_deliveries;
        DROP TABLE journal_digest_deliveries;
        ALTER TABLE journal_digest_deliveries_new RENAME TO journal_digest_deliveries;
        CREATE INDEX IF NOT EXISTS idx_journal_digest_deliveries_status
            ON journal_digest_deliveries(digest_id, status);
        """
    )


def _json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalize_alert_settings(raw: dict | None = None) -> dict:
    values = dict(DEFAULT_ALERT_SETTINGS)
    if isinstance(raw, dict):
        for key in DEFAULT_ALERT_SETTINGS:
            if key in {"subject_prefix", "intro_text"}:
                text = str(raw.get(key) or "").strip()
                if text:
                    values[key] = text
            elif key == "review_model":
                # 模型 id 允许为空（=沿用生效模型）；非空时去除首尾空白后采用。
                if key in raw:
                    values[key] = str(raw.get(key) or "").strip()
            elif key == "send_audience":
                aud = str(raw.get(key) or "").strip().lower()
                if aud in {"subscribers", "members", "registered"}:
                    values[key] = aud
            elif key == "send_audience_plans":
                if key in raw:
                    raw_plans = raw.get(key)
                    if isinstance(raw_plans, (list, tuple)):
                        values[key] = [str(c).strip() for c in raw_plans if str(c).strip()]
                    elif isinstance(raw_plans, str):
                        values[key] = [c.strip() for c in raw_plans.split(",") if c.strip()]
            elif key == "send_time":
                if key in raw:
                    values[key] = _normalize_hhmm(raw.get(key), values[key])
            elif key == "send_frequency":
                freq = str(raw.get(key) or "").strip().lower()
                if freq in SEND_FREQUENCIES:
                    values[key] = freq
            elif key == "send_weekday":
                if key in raw:
                    try:
                        values[key] = max(0, min(6, int(raw[key])))
                    except (TypeError, ValueError):
                        pass
            elif key == "lookback_days":
                if key in raw:
                    try:
                        values[key] = max(1, min(365, int(raw[key])))
                    except (TypeError, ValueError):
                        pass
            elif key in raw:
                values[key] = bool(raw[key])
    return values


def load_alert_settings() -> dict:
    init_admin_store_db()
    return normalize_alert_settings(get_setting("journal_alerts_settings", {}))


def last_email_run_at() -> datetime | None:
    """最近一次实际发出邮件的运行完成时间（保留兼容，不再用于发送防重）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT finished_at FROM journal_runs WHERE emails_sent > 0 AND finished_at != '' "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    return _parse_utc(str(row["finished_at"])) if row else None


def last_batch_created_at() -> datetime | None:
    """最近一个批次的创建时间（用于 biweekly/monthly 控制「多久开一个新批次」）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM journal_digests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _parse_utc(str(row["created_at"])) if row and row["created_at"] else None


def is_send_due(settings: dict | None = None, now: datetime | None = None) -> bool:
    """是否到发送日。发送是否真正发出由「批次是否已发送」防重（current_batch 排除 sent），
    因此这里不再用「距上次发送 N 天」的时间防重——那会被人工测试发送污染、阻塞正常调度。"""
    settings = settings or load_alert_settings()
    freq = str(settings.get("send_frequency") or "weekly").lower()
    if freq == "daily":
        return True
    now = now or utc_now()
    # weekly/biweekly 仅在设定的星期几（北京时间）发送；monthly 任何日子均可（由采集节流控制节奏）。
    if freq in {"weekly", "biweekly"}:
        return now.astimezone(BEIJING_TZ).weekday() == int(settings.get("send_weekday") or 0)
    return True


def _normalize_hhmm(value: Any, fallback: str = "08:00") -> str:
    """把任意输入规整为 HH:MM（24 小时制）；非法时返回 fallback。"""
    text = str(value or "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        return fallback
    hour = max(0, min(23, int(match.group(1))))
    minute = max(0, min(59, int(match.group(2))))
    return f"{hour:02d}:{minute:02d}"


def is_collect_due(settings: dict | None = None, now: datetime | None = None) -> bool:
    """采集阶段在「发送日的前一天」执行。daily 每天采集；biweekly/monthly 还需距上次开批足够久，
    保证 14/28 天的真实节奏（用「上次开批时间」节流，不受人工发送影响）。"""
    settings = settings or load_alert_settings()
    if bool(settings.get("automation_paused")):
        return False
    freq = str(settings.get("send_frequency") or "weekly").lower()
    if freq == "daily":
        return True
    now_utc = now or utc_now()
    now_bj = now_utc.astimezone(BEIJING_TZ)
    tomorrow = now_bj + timedelta(days=1)
    if freq in {"weekly", "biweekly"} and tomorrow.weekday() != int(settings.get("send_weekday") or 0):
        return False
    # biweekly/monthly 节流：距上次开批不足 (interval-2) 天则跳过本次，避免每周/每天都开新批。
    if freq in {"biweekly", "monthly"}:
        interval_days = _SEND_INTERVAL_DAYS.get(freq, 14)
        last = last_batch_created_at()
        if last is not None and (now_utc - last).total_seconds() < (interval_days - 2) * 86400:
            return False
    return True


def _default_source_by_name() -> dict[str, dict[str, Any]]:
    return {str(source["name"]): dict(source) for source in DEFAULT_JOURNAL_SOURCES}


def _source_config(source: dict) -> dict[str, Any]:
    config = source.get("config")
    if isinstance(config, dict):
        return config
    return _json_loads(str(source.get("config_json") or "{}"), {})


def _source_completeness(source: dict) -> dict[str, Any]:
    source_type = str(source.get("source_type") or "manual").strip().lower()
    issn = str(source.get("issn") or "").strip()
    source_url = str(source.get("source_url") or "").strip()
    config = _source_config(source)
    needs_issn = source_type in {"openalex", "crossref"}
    needs_url = source_type in {"rss", "web_html"}
    complete = source_type != "manual"
    if needs_issn and not issn:
        complete = False
    if needs_url and not (source_url or config.get("entry_url")):
        complete = False
    if source_type == "web_html" and not config.get("parser"):
        complete = False
    auto_publish = bool(config.get("auto_publish"))
    # 非网页抓取（openalex/crossref/rss）抓到即 ready 自动发；网页抓取需 auto_publish 才自动发，否则进人工审核。
    auto_send = complete and (source_type != "web_html" or auto_publish)
    if not int(source.get("is_enabled", 1) or 0):
        label = "已停用"
    elif complete:
        label = "可自动抓取·自动发送" if auto_send else "可自动抓取·人工审核"
    else:
        label = "待补充来源"
    return {
        "complete": complete,
        "label": label,
        "needs_issn": needs_issn,
        "needs_url": needs_url,
        "parser": str(config.get("parser") or ""),
        "auto_publish": auto_publish,
        "auto_send": auto_send,
        "gch": str(config.get("gch") or ""),
    }


def _source_row(row: sqlite3.Row | None) -> dict:
    data = _row_to_dict(row) or {}
    data["config"] = _source_config(data)
    data["completeness"] = _source_completeness(data)
    return data


def backfill_default_journal_sources() -> int:
    defaults = _default_source_by_name()
    changed = 0
    now = utc_now_text()
    with _connect() as conn:
        for source in DEFAULT_JOURNAL_SOURCES:
            cur = conn.execute(
                """
                INSERT INTO journal_sources(
                    name, language, issn, source_type, source_url, config_json,
                    is_enabled, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(name) DO NOTHING
                """,
                (
                    source["name"],
                    source.get("language", "zh"),
                    source.get("issn", ""),
                    source.get("source_type", "manual"),
                    source.get("source_url", ""),
                    _json_dumps(source.get("config", {})),
                    now,
                    now,
                ),
            )
            changed += int(cur.rowcount > 0)

        rows = conn.execute("SELECT * FROM journal_sources").fetchall()
        for row in rows:
            name = str(row["name"] or "")
            default = defaults.get(name)
            if not default:
                continue
            config_json = str(row["config_json"] or "").strip()
            safe_to_backfill = (
                str(row["source_type"] or "").strip().lower() == "manual"
                and not str(row["issn"] or "").strip()
                and not str(row["source_url"] or "").strip()
                and config_json in {"", "{}"}
            )
            # 迁移历史遗留配置：旧 NCPSSD 域名 / result.aspx 搜索抓取 / CNKI 搜索页均已失效，
            # 一律刷新为新的默认来源（ncpssd_journal 或 manual）。
            legacy_blob = f"{row['source_url'] or ''} {config_json}"
            stale_legacy = any(
                marker in legacy_blob
                for marker in (
                    "ncpssd.org",
                    "result.aspx",
                    "kns.cnki.net",
                    "ncssd_cnki_list",
                    # 求是网栏目地址已 404、人大 RSS 已 403，统一迁移到 NCPSSD。
                    "qstheory",
                    "qstheory_list",
                    "jxyyj.ruc.edu.cn",
                )
            )
            # 把新的默认来源（已知 NCPSSD 刊号 / OpenAlex 回退）套到仍为 manual 或缺少应有 gch 的历史行，
            # 修复“已配 gch 却仍显示待补充来源”的问题；管理员已自定义为非 manual 的行不动。
            default_type = str(default.get("source_type") or "manual").strip().lower()
            default_gch = str((default.get("config") or {}).get("gch") or "")
            row_type = str(row["source_type"] or "manual").strip().lower()
            needs_upgrade = default_type != "manual" and (
                row_type == "manual" or (bool(default_gch) and default_gch not in config_json)
            )
            if not (safe_to_backfill or stale_legacy or needs_upgrade):
                continue
            cur = conn.execute(
                """
                UPDATE journal_sources
                SET issn = ?, source_type = ?, source_url = ?, config_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    default.get("issn", ""),
                    default.get("source_type", "manual"),
                    default.get("source_url", ""),
                    _json_dumps(default.get("config", {})),
                    now,
                    int(row["id"]),
                ),
            )
            changed += int(cur.rowcount > 0)
        conn.commit()
    return changed


def journal_source_catalog() -> dict[str, Any]:
    sources = list_journal_sources(limit=240)
    zh = [source for source in sources if str(source.get("language") or "").lower().startswith("zh")]
    en = [source for source in sources if not str(source.get("language") or "").lower().startswith("zh")]
    return {
        "zh": zh,
        "en": en,
        "total": len(sources),
        "auto_count": sum(1 for source in sources if source.get("completeness", {}).get("complete")),
    }


def init_journal_alerts_db() -> Path:
    init_membership_db()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS journal_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                confirm_token TEXT NOT NULL UNIQUE,
                unsubscribe_token TEXT NOT NULL UNIQUE,
                confirmed_at TEXT NOT NULL DEFAULT '',
                unsubscribed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_sent_at TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                UNIQUE(user_id, email),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_journal_subscriptions_email
                ON journal_subscriptions(email);
            CREATE INDEX IF NOT EXISTS idx_journal_subscriptions_status
                ON journal_subscriptions(status);

            CREATE TABLE IF NOT EXISTS journal_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                language TEXT NOT NULL DEFAULT 'zh',
                issn TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'manual',
                source_url TEXT NOT NULL DEFAULT '',
                config_json TEXT NOT NULL DEFAULT '{}',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS journal_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                journal_name TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'zh',
                title TEXT NOT NULL,
                title_zh TEXT NOT NULL DEFAULT '',
                abstract TEXT NOT NULL DEFAULT '',
                abstract_zh TEXT NOT NULL DEFAULT '',
                authors_json TEXT NOT NULL DEFAULT '[]',
                citation_gb2015 TEXT NOT NULL DEFAULT '',
                doi TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                volume TEXT NOT NULL DEFAULT '',
                issue TEXT NOT NULL DEFAULT '',
                pages TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'ready',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                notified_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (source_id) REFERENCES journal_sources(id)
            );

            CREATE INDEX IF NOT EXISTS idx_journal_articles_status_seen
                ON journal_articles(status, first_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_journal_articles_source
                ON journal_articles(source_id, published_at DESC);

            CREATE TABLE IF NOT EXISTS journal_delivery_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                subscription_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(article_id, subscription_id),
                FOREIGN KEY (article_id) REFERENCES journal_articles(id),
                FOREIGN KEY (subscription_id) REFERENCES journal_subscriptions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_journal_delivery_logs_status
                ON journal_delivery_logs(status, created_at DESC);

            CREATE TABLE IF NOT EXISTS journal_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                sources_checked INTEGER NOT NULL DEFAULT 0,
                articles_found INTEGER NOT NULL DEFAULT 0,
                articles_inserted INTEGER NOT NULL DEFAULT 0,
                emails_sent INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );

            -- 批次（一个发送周期一行）：采集→生成综述→审核→发送的承载单元。
            CREATE TABLE IF NOT EXISTS journal_digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_start TEXT NOT NULL DEFAULT '',
                period_end TEXT NOT NULL DEFAULT '',
                frequency TEXT NOT NULL DEFAULT 'weekly',
                status TEXT NOT NULL DEFAULT 'collecting',
                review_md TEXT NOT NULL DEFAULT '',
                review_html TEXT NOT NULL DEFAULT '',
                review_status TEXT NOT NULL DEFAULT 'none',
                review_model TEXT NOT NULL DEFAULT '',
                review_generated_at TEXT NOT NULL DEFAULT '',
                review_approved_at TEXT NOT NULL DEFAULT '',
                auto_approve_articles INTEGER NOT NULL DEFAULT 0,
                auto_generate_review INTEGER NOT NULL DEFAULT 0,
                auto_send INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT NOT NULL DEFAULT '',
                emails_sent INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_journal_digests_status
                ON journal_digests(status, created_at DESC);

            -- 按批次记录每个订阅者的综述邮件投递（群发去重）。
            CREATE TABLE IF NOT EXISTS journal_digest_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_id INTEGER NOT NULL,
                subscription_id INTEGER,
                user_id INTEGER,
                email TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(digest_id, email),
                FOREIGN KEY (digest_id) REFERENCES journal_digests(id)
            );

            CREATE INDEX IF NOT EXISTS idx_journal_digest_deliveries_status
                ON journal_digest_deliveries(digest_id, status);
            """
        )
        # 幂等补列（线上旧库通过启动迁移补齐，绝不重建/丢数据）。
        _ensure_column(conn, "journal_articles", "batch_id", "INTEGER")
        _ensure_column(conn, "journal_articles", "ai_discipline", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "journal_articles", "ai_problem_type", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_journal_articles_batch ON journal_articles(batch_id, status)"
        )
        # 投递表迁移：支持非订阅收件人（付费会员/注册用户/特定邮箱），去重改为按邮箱。
        _migrate_digest_deliveries(conn)
        now = utc_now_text()
        for source in DEFAULT_JOURNAL_SOURCES:
            conn.execute(
                """
                INSERT INTO journal_sources(
                    name, language, issn, source_type, source_url, config_json,
                    is_enabled, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(name) DO NOTHING
                """,
                (
                    source["name"],
                    source.get("language", "zh"),
                    source.get("issn", ""),
                    source.get("source_type", "manual"),
                    source.get("source_url", ""),
                    _json_dumps(source.get("config", {})),
                    now,
                    now,
                ),
            )
        conn.commit()
    backfill_default_journal_sources()
    return DB_PATH


def load_smtp_config() -> SMTPConfig:
    try:
        port = int(os.environ.get("SMTP_PORT") or "587")
    except ValueError:
        port = 587
    use_tls = str(os.environ.get("SMTP_USE_TLS") or "1").strip().lower() not in {"0", "false", "no", "off"}
    return SMTPConfig(
        host=str(os.environ.get("SMTP_HOST") or "").strip(),
        port=port,
        username=str(os.environ.get("SMTP_USERNAME") or "").strip(),
        password=str(os.environ.get("SMTP_PASSWORD") or "").strip(),
        from_email=str(os.environ.get("SMTP_FROM_EMAIL") or "").strip(),
        from_name=str(os.environ.get("SMTP_FROM_NAME") or "马著作检索").strip(),
        use_tls=use_tls,
    )


def public_base_url(deployment: DeploymentSettings | None = None) -> str:
    value = str(os.environ.get("JOURNAL_ALERT_BASE_URL") or "").strip().rstrip("/")
    if value:
        return value
    if deployment and deployment.public_base_url:
        return deployment.public_base_url.rstrip("/")
    return ""


def _make_token() -> str:
    return secrets.token_urlsafe(32)


def create_or_update_subscription(user_id: int, email: str) -> dict:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        raise ValueError("请输入有效邮箱。")
    now = utc_now_text()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM journal_subscriptions
            WHERE user_id = ? AND email = ?
            """,
            (user_id, normalized),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO journal_subscriptions(
                    user_id, email, status, confirm_token, unsubscribe_token,
                    created_at, updated_at
                )
                VALUES(?, ?, 'pending', ?, ?, ?, ?)
                """,
                (user_id, normalized, _make_token(), _make_token(), now, now),
            )
        else:
            conn.execute(
                """
                UPDATE journal_subscriptions
                SET status = 'pending',
                    confirm_token = ?,
                    confirmed_at = '',
                    unsubscribed_at = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (_make_token(), now, int(row["id"])),
            )
        updated = conn.execute(
            """
            SELECT *
            FROM journal_subscriptions
            WHERE user_id = ? AND email = ?
            """,
            (user_id, normalized),
        ).fetchone()
        conn.commit()
    return _row_to_dict(updated) or {}


def list_subscriptions_for_user(user_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM journal_subscriptions
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def list_recent_subscriptions(limit: int = 80) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, u.email AS user_account_email, u.display_name, u.role, u.is_active
            FROM journal_subscriptions s
            JOIN users u ON u.id = s.user_id
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def confirm_subscription(token: str) -> dict | None:
    now = utc_now_text()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM journal_subscriptions WHERE confirm_token = ?",
            ((token or "").strip(),),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE journal_subscriptions
            SET status = 'active', confirmed_at = CASE WHEN confirmed_at = '' THEN ? ELSE confirmed_at END,
                unsubscribed_at = '', updated_at = ?
            WHERE id = ?
            """,
            (now, now, int(row["id"])),
        )
        updated = conn.execute("SELECT * FROM journal_subscriptions WHERE id = ?", (int(row["id"]),)).fetchone()
        conn.commit()
    return _row_to_dict(updated)


def unsubscribe_by_token(token: str) -> dict | None:
    return _unsubscribe("unsubscribe_token", (token or "").strip())


def unsubscribe_by_id(user_id: int, subscription_id: int) -> dict | None:
    now = utc_now_text()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM journal_subscriptions
            WHERE id = ? AND user_id = ?
            """,
            (subscription_id, user_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE journal_subscriptions
            SET status = 'unsubscribed',
                unsubscribed_at = CASE WHEN unsubscribed_at = '' THEN ? ELSE unsubscribed_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, int(row["id"])),
        )
        updated = conn.execute("SELECT * FROM journal_subscriptions WHERE id = ?", (int(row["id"]),)).fetchone()
        conn.commit()
    return _row_to_dict(updated)


def _unsubscribe(token_field: str, token: str) -> dict | None:
    now = utc_now_text()
    with _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM journal_subscriptions WHERE {token_field} = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE journal_subscriptions
            SET status = 'unsubscribed',
                unsubscribed_at = CASE WHEN unsubscribed_at = '' THEN ? ELSE unsubscribed_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, int(row["id"])),
        )
        updated = conn.execute("SELECT * FROM journal_subscriptions WHERE id = ?", (int(row["id"]),)).fetchone()
        conn.commit()
    return _row_to_dict(updated)


def list_journal_sources(limit: int = 80) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM journal_sources
            ORDER BY language DESC, name ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [_source_row(row) for row in rows]


def list_recent_articles(limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM journal_articles
            ORDER BY first_seen_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [_article_row(row) for row in rows]


def list_articles_by_status(status: str, limit: int = 30) -> list[dict]:
    status = (status or "pending_review").strip()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM journal_articles
            WHERE status = ?
            ORDER BY first_seen_at DESC, id DESC
            LIMIT ?
            """,
            (status, max(1, int(limit))),
        ).fetchall()
    return [_article_row(row) for row in rows]


def update_article_review_status(article_id: int, status: str) -> dict | None:
    status = (status or "").strip()
    if status not in {"ready", "ignored", "pending_review"}:
        raise ValueError("文章状态只支持 ready、ignored、pending_review。")
    with _connect() as conn:
        conn.execute(
            "UPDATE journal_articles SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now_text(), int(article_id)),
        )
        row = conn.execute("SELECT * FROM journal_articles WHERE id = ?", (int(article_id),)).fetchone()
        conn.commit()
    return _article_row(row) if row is not None else None


def approve_all_pending_articles() -> int:
    """一键批准：把所有待审核文章置为 ready（进入发送队列）。返回批准的数量。"""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE journal_articles SET status = 'ready', updated_at = ? WHERE status = 'pending_review'",
            (utc_now_text(),),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def list_recent_delivery_logs(limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT l.*, a.title, a.journal_name
            FROM journal_delivery_logs l
            JOIN journal_articles a ON a.id = l.article_id
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def list_recent_runs(limit: int = 12) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM journal_runs
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def update_journal_source(
    source_id: int,
    *,
    source_type: str,
    source_url: str,
    issn: str,
    is_enabled: bool,
    config: dict | None = None,
) -> None:
    source_type = (source_type or "manual").strip().lower()
    if source_type not in {"manual", "openalex", "crossref", "rss", "web_html"}:
        raise ValueError("来源类型只支持 manual、openalex、crossref、rss、web_html。")
    config_json = _json_dumps(config or {})
    with _connect() as conn:
        conn.execute(
            """
            UPDATE journal_sources
            SET source_type = ?, source_url = ?, issn = ?, config_json = ?, is_enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                source_type,
                (source_url or "").strip(),
                (issn or "").strip(),
                config_json,
                1 if is_enabled else 0,
                utc_now_text(),
                source_id,
            ),
        )
        conn.commit()


def add_journal_source(
    *,
    name: str,
    language: str = "zh",
    source_type: str = "manual",
    issn: str = "",
    source_url: str = "",
    config: dict | None = None,
) -> dict:
    """新增一个期刊来源（控制台「期刊来源」区使用）。name 唯一，重复则报错。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("期刊名称不能为空。")
    language = (language or "zh").strip().lower()
    if language not in {"zh", "en"}:
        language = "zh"
    source_type = (source_type or "manual").strip().lower()
    if source_type not in {"manual", "openalex", "crossref", "rss", "web_html"}:
        raise ValueError("来源类型只支持 manual、openalex、crossref、rss、web_html。")
    now = utc_now_text()
    init_journal_alerts_db()
    with _connect() as conn:
        exists = conn.execute("SELECT id FROM journal_sources WHERE name = ?", (name,)).fetchone()
        if exists is not None:
            raise ValueError(f"期刊来源“{name}”已存在。")
        cur = conn.execute(
            """
            INSERT INTO journal_sources(
                name, language, issn, source_type, source_url, config_json,
                is_enabled, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                name,
                language,
                (issn or "").strip(),
                source_type,
                (source_url or "").strip(),
                _json_dumps(config or {}),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM journal_sources WHERE id = ?", (int(cur.lastrowid),)).fetchone()
        conn.commit()
    return _source_row(row)


def _article_row(row: sqlite3.Row) -> dict:
    data = _row_to_dict(row) or {}
    data["authors"] = _json_loads(str(data.get("authors_json") or "[]"), [])
    data["metadata"] = _json_loads(str(data.get("metadata_json") or "{}"), {})
    return data


# ----------------------------------------------------------------------------
# 批次（digest batch）生命周期
# ----------------------------------------------------------------------------

def _digest_row(row: sqlite3.Row | None) -> dict | None:
    return _row_to_dict(row)


def current_batch() -> dict | None:
    """最近一个尚未发送/归档的批次（collecting/reviewing/ready_to_send）。"""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM journal_digests
            WHERE status IN ('collecting', 'reviewing', 'ready_to_send')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    return _digest_row(row)


def last_sent_batch() -> dict | None:
    """最近一个已发送批次（用于首页留存展示，直到下一批发送）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM journal_digests WHERE status = 'sent' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _digest_row(row)


def latest_public_batch() -> dict | None:
    """首页「查看本周新文」展示的批次：优先最近一次已发送批次（发送后留存到下次发送），
    尚无发送时退回当前在建批次（便于审核期预览）。"""
    return last_sent_batch() or current_batch()


def archive_sent_batches_before(keep_digest_id: int) -> None:
    """归档比 keep_digest_id 更早的已发送批次及其文章，使首页只留存最新一期已发送内容。"""
    now = utc_now_text()
    with _connect() as conn:
        old = conn.execute(
            "SELECT id FROM journal_digests WHERE status = 'sent' AND id < ?",
            (int(keep_digest_id),),
        ).fetchall()
        ids = [int(r["id"]) for r in old]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE journal_articles SET status = 'archived', updated_at = ? "
                f"WHERE batch_id IN ({placeholders}) AND status IN ('ready', 'pending_review', 'translation_pending')",
                (now, *ids),
            )
            conn.execute(
                f"UPDATE journal_digests SET status = 'archived', updated_at = ? WHERE id IN ({placeholders})",
                (now, *ids),
            )
            conn.commit()


def get_batch(digest_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM journal_digests WHERE id = ?", (int(digest_id),)).fetchone()
    return _digest_row(row)


def list_recent_batches(limit: int = 12) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM journal_digests ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [_digest_row(row) or {} for row in rows]


def open_batch(settings: dict | None = None, *, period_days: int | None = None) -> dict:
    """开新批次：归档之前未发送的批次及其文章，再插入一行 collecting 批次。"""
    settings = settings or load_alert_settings()
    now = utc_now()
    days = int(period_days if period_days is not None else (settings.get("lookback_days") or DEFAULT_LOOKBACK_DAYS))
    period_start = (now - timedelta(days=days)).isoformat(timespec="seconds")
    period_end = now.isoformat(timespec="seconds")
    hard_delete = bool(settings.get("hard_delete_archived"))
    archive_previous_batches(hard_delete=hard_delete)
    now_text = utc_now_text()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO journal_digests(
                period_start, period_end, frequency, status,
                auto_approve_articles, auto_generate_review, auto_send,
                created_at, updated_at
            )
            VALUES(?, ?, ?, 'collecting', ?, ?, ?, ?, ?)
            """,
            (
                period_start,
                period_end,
                str(settings.get("send_frequency") or "weekly"),
                1 if settings.get("auto_approve_articles") else 0,
                1 if settings.get("auto_generate_review") else 0,
                1 if settings.get("auto_send") else 0,
                now_text,
                now_text,
            ),
        )
        row = conn.execute("SELECT * FROM journal_digests WHERE id = ?", (int(cur.lastrowid),)).fetchone()
        conn.commit()
    return _digest_row(row) or {}


def archive_previous_batches(*, hard_delete: bool = False) -> int:
    """把所有未发送的旧批次标记为 archived；其下仍待处理的文章一并归档或硬删除。"""
    now = utc_now_text()
    with _connect() as conn:
        old = conn.execute(
            "SELECT id FROM journal_digests WHERE status IN ('collecting', 'reviewing', 'ready_to_send')"
        ).fetchall()
        batch_ids = [int(r["id"]) for r in old]
        if hard_delete and batch_ids:
            placeholders = ",".join("?" for _ in batch_ids)
            conn.execute(
                f"DELETE FROM journal_articles WHERE batch_id IN ({placeholders}) "
                "AND status IN ('pending_review', 'ready', 'translation_pending')",
                tuple(batch_ids),
            )
        elif batch_ids:
            placeholders = ",".join("?" for _ in batch_ids)
            conn.execute(
                f"UPDATE journal_articles SET status = 'archived', updated_at = ? "
                f"WHERE batch_id IN ({placeholders}) "
                "AND status IN ('pending_review', 'ready', 'translation_pending')",
                (now, *batch_ids),
            )
        # 兜底：没有 batch_id 的历史遗留待处理文章（旧版本写入）也一并归档/删除。
        if hard_delete:
            conn.execute(
                "DELETE FROM journal_articles WHERE batch_id IS NULL "
                "AND status IN ('pending_review', 'ready', 'translation_pending')"
            )
        else:
            conn.execute(
                "UPDATE journal_articles SET status = 'archived', updated_at = ? "
                "WHERE batch_id IS NULL AND status IN ('pending_review', 'ready', 'translation_pending')",
                (now,),
            )
        if batch_ids:
            conn.execute(
                f"UPDATE journal_digests SET status = 'archived', updated_at = ? "
                f"WHERE id IN ({','.join('?' for _ in batch_ids)})",
                (now, *batch_ids),
            )
        conn.commit()
    return len(batch_ids)


def purge_archived_articles() -> int:
    """硬删除所有已归档文章（控制台「硬删除已归档」按钮）。"""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM journal_articles WHERE status = 'archived'")
        conn.commit()
        return int(cur.rowcount or 0)


def batch_articles(digest_id: int, statuses: tuple[str, ...] | None = None) -> list[dict]:
    """取某批次的文章，可按状态过滤。"""
    with _connect() as conn:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT * FROM journal_articles WHERE batch_id = ? AND status IN ({placeholders}) "
                "ORDER BY ai_discipline ASC, first_seen_at ASC, id ASC",
                (int(digest_id), *statuses),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM journal_articles WHERE batch_id = ? "
                "ORDER BY ai_discipline ASC, first_seen_at ASC, id ASC",
                (int(digest_id),),
            ).fetchall()
    return [_article_row(row) for row in rows]


def set_article_classification(article_id: int, discipline: str, problem_type: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE journal_articles SET ai_discipline = ?, ai_problem_type = ?, updated_at = ? WHERE id = ?",
            ((discipline or "").strip(), (problem_type or "").strip(), utc_now_text(), int(article_id)),
        )
        conn.commit()


def update_batch_review(
    digest_id: int,
    *,
    review_md: str | None = None,
    review_html: str | None = None,
    review_status: str | None = None,
    review_model: str | None = None,
    status: str | None = None,
    mark_generated: bool = False,
    mark_approved: bool = False,
) -> dict | None:
    now = utc_now_text()
    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [now]
    if review_md is not None:
        sets.append("review_md = ?"); params.append(review_md)
    if review_html is not None:
        sets.append("review_html = ?"); params.append(review_html)
    if review_status is not None:
        sets.append("review_status = ?"); params.append(review_status)
    if review_model is not None:
        sets.append("review_model = ?"); params.append(review_model)
    if status is not None:
        sets.append("status = ?"); params.append(status)
    if mark_generated:
        sets.append("review_generated_at = ?"); params.append(now)
    if mark_approved:
        sets.append("review_approved_at = ?"); params.append(now)
    params.append(int(digest_id))
    with _connect() as conn:
        conn.execute(f"UPDATE journal_digests SET {', '.join(sets)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM journal_digests WHERE id = ?", (int(digest_id),)).fetchone()
        conn.commit()
    return _digest_row(row)


def _urlopen_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _urlopen_text(url: str, user_agent: str = USER_AGENT) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_source_articles(source: dict, lookback_days: int | None = None) -> list[dict]:
    days = int(lookback_days) if lookback_days else DEFAULT_LOOKBACK_DAYS
    source_type = str(source.get("source_type") or "manual").strip().lower()
    if source_type == "openalex":
        return _fetch_openalex(source, days)
    if source_type == "crossref":
        return _fetch_crossref(source, days)
    if source_type == "rss":
        return _fetch_rss(source)
    if source_type == "web_html":
        return _fetch_web_html(source)
    return []


def _candidate_source_urls(source: dict) -> list[str]:
    config = _source_config(source)
    urls: list[str] = []
    for value in (config.get("entry_url"), source.get("source_url")):
        text = str(value or "").strip()
        if text and text not in urls:
            urls.append(text)
    fallback_urls = config.get("fallback_urls")
    if isinstance(fallback_urls, list):
        for value in fallback_urls:
            text = str(value or "").strip()
            if text and text not in urls:
                urls.append(text)
    return urls


def _fetch_web_html(source: dict) -> list[dict]:
    config = _source_config(source)
    parser = str(config.get("parser") or "generic").strip().lower()
    urls = _candidate_source_urls(source)
    # 仅填了 gch 的来源（管理员在控制台补录）自动拼出 NCPSSD 期刊页地址。
    if parser == "ncpssd_journal":
        gch = str(config.get("gch") or "").strip()
        if gch:
            details = f"{NCPSSD_JOURNAL_BASE}?gch={gch}"
            if details not in urls:
                urls.insert(0, details)
    errors: list[str] = []
    for url in urls:
        try:
            text = _urlopen_text(url, user_agent=BROWSER_USER_AGENT)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        if parser == "ncpssd_journal":
            articles = _parse_ncpssd_journal_html(text, source, url)
        elif parser == "qstheory_list":
            articles = _parse_qstheory_html(text, source, url)
        elif parser == "ncssd_cnki_list":
            articles = _parse_ncssd_cnki_html(text, source, url)
        else:
            articles = _parse_generic_article_list(text, source, url)
        if articles:
            return articles
    if errors:
        raise RuntimeError("; ".join(errors)[:1200])
    return []


def _fetch_openalex(source: dict, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[dict]:
    issn = str(source.get("issn") or "").strip()
    if not issn:
        return []
    from_date = (utc_now() - timedelta(days=int(lookback_days or DEFAULT_LOOKBACK_DAYS))).date().isoformat()
    params = urllib.parse.urlencode(
        {
            "filter": f"locations.source.issn:{issn},from_publication_date:{from_date}",
            "sort": "publication_date:desc",
            "per-page": "25",
        }
    )
    data = _urlopen_json(f"https://api.openalex.org/works?{params}")
    articles = []
    crossref_budget = 15  # 仅对缺摘要且有 DOI 的文章用 Crossref 兜底，限量以控制请求数。
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        # 只保留正式期刊论文；剔除 book-chapter/dataset/editorial/erratum 等。
        if not _is_allowed_work_type(item.get("type"), _OPENALEX_ARTICLE_TYPES):
            continue
        title = str(item.get("title") or "").strip()
        if not title or not _looks_like_article_title(title):
            continue
        authors = [
            str((auth.get("author") or {}).get("display_name") or "").strip()
            for auth in item.get("authorships") or []
            if isinstance(auth, dict)
        ]
        location = item.get("primary_location") or {}
        source_info = location.get("source") or {}
        doi = str(item.get("doi") or "").strip()
        if doi.lower().startswith("https://doi.org/"):
            doi = doi[16:]
        abstract = _openalex_abstract(item.get("abstract_inverted_index"))
        if not abstract and doi and crossref_budget > 0:
            crossref_budget -= 1
            abstract = _crossref_abstract(doi)
        articles.append(
            {
                "journal_name": str(source_info.get("display_name") or source.get("name") or "").strip(),
                "language": source.get("language") or "en",
                "title": title,
                "abstract": abstract,
                "authors": [a for a in authors if a],
                "doi": doi,
                "url": str(location.get("landing_page_url") or item.get("id") or "").strip(),
                "published_at": str(item.get("publication_date") or "").strip(),
                "volume": str((item.get("biblio") or {}).get("volume") or "").strip(),
                "issue": str((item.get("biblio") or {}).get("issue") or "").strip(),
                "pages": _openalex_pages(item.get("biblio") or {}),
                "metadata": {"openalex_id": item.get("id"), "work_type": item.get("type")},
            }
        )
    return articles


def _openalex_abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            try:
                words.append((int(pos), str(word)))
            except (TypeError, ValueError):
                continue
    return " ".join(word for _, word in sorted(words))


def _openalex_pages(biblio: dict) -> str:
    first = str(biblio.get("first_page") or "").strip()
    last = str(biblio.get("last_page") or "").strip()
    if first and last and first != last:
        return f"{first}-{last}"
    return first or last


def _fetch_crossref(source: dict, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[dict]:
    issn = str(source.get("issn") or "").strip()
    if not issn:
        return []
    from_date = (utc_now() - timedelta(days=int(lookback_days or DEFAULT_LOOKBACK_DAYS))).date().isoformat()
    params = urllib.parse.urlencode({"filter": f"from-pub-date:{from_date}", "sort": "published", "order": "desc", "rows": "25"})
    data = _urlopen_json(f"https://api.crossref.org/journals/{urllib.parse.quote(issn)}/works?{params}")
    items = ((data.get("message") or {}).get("items") or [])
    articles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # 只保留正式期刊论文；剔除 book/journal-issue/editorial 等非论文条目。
        if not _is_allowed_work_type(item.get("type"), _CROSSREF_ARTICLE_TYPES):
            continue
        title = " ".join(item.get("title") or []).strip()
        if not title or not _looks_like_article_title(title):
            continue
        authors = [_crossref_author_name(author) for author in item.get("author") or [] if isinstance(author, dict)]
        published = _crossref_date(item)
        articles.append(
            {
                "journal_name": " ".join(item.get("container-title") or []).strip() or source.get("name") or "",
                "language": source.get("language") or "en",
                "title": title,
                "abstract": _strip_tags(str(item.get("abstract") or "")),
                "authors": [a for a in authors if a],
                "doi": str(item.get("DOI") or "").strip(),
                "url": str(item.get("URL") or "").strip(),
                "published_at": published,
                "volume": str(item.get("volume") or "").strip(),
                "issue": str(item.get("issue") or "").strip(),
                "pages": str(item.get("page") or "").strip(),
                "metadata": {"crossref_type": item.get("type")},
            }
        )
    return articles


def _crossref_abstract(doi: str) -> str:
    """按 DOI 取 Crossref 摘要（JATS），用于 OpenAlex 缺摘要时兜底。失败返回空串。"""
    doi = str(doi or "").strip()
    if not doi:
        return ""
    try:
        data = _urlopen_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")
    except Exception:
        return ""
    abstract = ((data.get("message") or {}) if isinstance(data, dict) else {}).get("abstract") or ""
    return _strip_tags(str(abstract)).strip()


def _crossref_author_name(author: dict) -> str:
    family = str(author.get("family") or "").strip()
    given = str(author.get("given") or "").strip()
    return " ".join(part for part in (family, given) if part)


def _crossref_date(item: dict) -> str:
    for key in ("published-print", "published-online", "published", "created"):
        parts = (((item.get(key) or {}).get("date-parts") or [[]])[0] or [])
        if parts:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def _fetch_rss(source: dict) -> list[dict]:
    errors: list[str] = []
    for url in _candidate_source_urls(source):
        try:
            return _parse_rss_text(_urlopen_text(url), source, url)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors)[:1200])
    return []


def _parse_rss_text(text: str, source: dict, base_url: str = "") -> list[dict]:
    root = ET.fromstring(text)
    items = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
    articles = []
    for item in items[:25]:
        title = _find_text(item, ("title", "{http://www.w3.org/2005/Atom}title"))
        link = _find_text(item, ("link", "{http://www.w3.org/2005/Atom}link"))
        if not link:
            atom_link = item.find("{http://www.w3.org/2005/Atom}link")
            link = atom_link.get("href", "") if atom_link is not None else ""
        abstract = _find_text(item, ("description", "summary", "{http://www.w3.org/2005/Atom}summary", "content"))
        published = _find_text(item, ("pubDate", "published", "{http://www.w3.org/2005/Atom}published", "updated", "{http://www.w3.org/2005/Atom}updated"))
        if title:
            articles.append(_web_article(source, title, link, published, abstract, base_url, {"source": "rss"}))
    return articles


# NCPSSD 期刊页文章条目：<a onclick="openDetail('/Literature/articleinfo?id=<ID>...')" ... title='<标题>'>
_NCPSSD_ARTICLE_RE = re.compile(
    r"openDetail\('(?P<href>/Literature/articleinfo\?id=(?P<id>[A-Za-z0-9]+)[^']*)'\)"
    r".*?title='(?P<title>[^']*)'",
    re.S,
)
# 文章 id 形如 <刊号>YYYYNNNSSS（年份 4 位 + 期号 3 位 + 序号 3 位）。
_NCPSSD_YEAR_ISSUE_RE = re.compile(r"(?P<year>(?:19|20)\d{2})(?P<issue>\d{3})\d{3}$")


def _split_cn_authors(text: str) -> list[str]:
    # 去掉作者后的机构标注，如 [1]、[1,2]、[1，2]。
    text = re.sub(r"\[[^\]]*\]", "", text or "")
    parts = re.split(r"[;,，、\s]+", text)
    return [part.strip() for part in parts if part.strip()]


def _parse_ncpssd_journal_html(text: str, source: dict, base_url: str) -> list[dict]:
    articles: list[dict] = []
    seen: set[str] = set()
    for match in _NCPSSD_ARTICLE_RE.finditer(text or ""):
        title = html.unescape(_strip_tags(match.group("title") or "")).strip()
        if not _looks_like_article_title(title):
            continue
        article_id = match.group("id")
        if article_id in seen:
            continue
        seen.add(article_id)
        url = urllib.parse.urljoin(base_url, html.unescape(match.group("href")))
        published = ""
        issue = ""
        year_issue = _NCPSSD_YEAR_ISSUE_RE.search(article_id)
        if year_issue:
            published = f"{year_issue.group('year')}-01-01"
            issue = str(int(year_issue.group("issue")))
        window = text[match.end() : match.end() + 600]
        writer_match = re.search(r"class=['\"]writer['\"][^>]*>(?P<authors>[^<]*)<", window)
        authors = _split_cn_authors(_strip_tags(html.unescape(writer_match.group("authors")))) if writer_match else []
        pages_match = re.search(r"class=['\"]pages['\"][^>]*>\(?(?P<pages>[^<)]*)", window)
        pages = pages_match.group("pages").strip() if pages_match else ""
        article = _web_article(
            source, title, url, published, "", base_url, {"source": "ncpssd_journal", "ncpssd_id": article_id}
        )
        article["authors"] = authors
        article["issue"] = issue
        article["pages"] = pages
        articles.append(article)
        if len(articles) >= 25:
            break
    return articles


NCPSSD_DETAIL_API = "https://www.ncpssd.cn/articleinfoHandler/getjournalarticletable"
NCPSSD_HOME = "https://www.ncpssd.cn/"

_NCPSSD_OPENER: Any = None
_NCPSSD_WARMED = False


def _ncpssd_opener():
    """带 Cookie 的共享 opener；首次使用时先访问首页获取可能的 WAF Cookie。"""
    global _NCPSSD_OPENER, _NCPSSD_WARMED
    if _NCPSSD_OPENER is None:
        import http.cookiejar

        jar = http.cookiejar.CookieJar()
        _NCPSSD_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    if not _NCPSSD_WARMED:
        try:
            warm = urllib.request.Request(NCPSSD_HOME, headers={"User-Agent": BROWSER_USER_AGENT})
            with _NCPSSD_OPENER.open(warm, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                resp.read(2048)
        except Exception:
            pass
        _NCPSSD_WARMED = True
    return _NCPSSD_OPENER


def _ncpssd_detail_request(lngid: str):
    body = json.dumps({"lngid": lngid, "type": "journalArticle", "pageType": ""}).encode("utf-8")
    return urllib.request.Request(
        NCPSSD_DETAIL_API,
        data=body,
        method="POST",
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://www.ncpssd.cn",
            "Referer": "https://www.ncpssd.cn/",
            "X-Requested-With": "XMLHttpRequest",
            "wzws-api-verify": str(int(utc_now().timestamp() * 1000)),
        },
    )


def _ncpssd_detail_raw(lngid: str) -> str:
    opener = _ncpssd_opener()
    with opener.open(_ncpssd_detail_request(lngid), timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_ncpssd_detail(lngid: str) -> dict:
    """调用 NCPSSD 文章详情接口补全摘要等字段。

    该接口受瑞数 WAF 保护：带 wzws-api-verify=当前毫秒时间戳的请求头 + Cookie 通常可通过；
    负载为 {lngid, type:"journalArticle", pageType:""}。失败/被拦时返回空字典（容错）。
    """
    lngid = str(lngid or "").strip()
    if not lngid:
        return {}
    raw = _ncpssd_detail_raw(lngid)
    payload = json.loads(raw)  # WAF 拦截时返回 HTML，会在此抛出 JSONDecodeError（由调用方记录）
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data:
        return {}
    abstract = _strip_tags(html.unescape(str(data.get("remarkc") or ""))).strip()
    authors = _split_cn_authors(_strip_tags(html.unescape(str(data.get("showwriter") or ""))))
    begin = str(data.get("beginpage") or "").strip()
    end = str(data.get("endpage") or "").strip()
    pages = f"{begin}-{end}" if begin and end and begin != end else (begin or end)
    years = str(data.get("years") or "").strip()
    published = f"{years}-01-01" if re.fullmatch(r"(?:19|20)\d{2}", years) else ""
    return {
        "abstract": abstract,
        "authors": authors,
        "pages": pages,
        "doi": str(data.get("doi") or "").strip(),
        "issue": str(data.get("num") or "").strip(),
        "published_at": published,
        "keywords": _strip_tags(html.unescape(str(data.get("keywordc") or ""))).strip(),
    }


def journal_abstract_coverage() -> dict:
    """轻量统计：文章总数 / 含摘要数 / 中文(NCPSSD)总数与含摘要数（供控制台展示）。"""
    with _connect() as conn:
        def one(q: str) -> int:
            return int(conn.execute(q).fetchone()[0])

        return {
            "total": one("SELECT COUNT(*) FROM journal_articles"),
            "with_abstract": one("SELECT COUNT(*) FROM journal_articles WHERE TRIM(abstract) != ''"),
            "cn_total": one("SELECT COUNT(*) FROM journal_articles WHERE metadata_json LIKE '%ncpssd_id%'"),
            "cn_with_abstract": one(
                "SELECT COUNT(*) FROM journal_articles WHERE metadata_json LIKE '%ncpssd_id%' AND TRIM(abstract) != ''"
            ),
        }


def journal_abstract_diag() -> dict:
    """诊断：期刊文章的摘要覆盖情况与最近运行概况（仅计数，无隐私数据）。"""
    with _connect() as conn:
        def one(q: str) -> int:
            return int(conn.execute(q).fetchone()[0])

        total = one("SELECT COUNT(*) FROM journal_articles")
        with_abs = one("SELECT COUNT(*) FROM journal_articles WHERE TRIM(abstract) != ''")
        ncp = one("SELECT COUNT(*) FROM journal_articles WHERE metadata_json LIKE '%ncpssd_id%'")
        ncp_abs = one(
            "SELECT COUNT(*) FROM journal_articles WHERE metadata_json LIKE '%ncpssd_id%' AND TRIM(abstract) != ''"
        )
        by_status = {str(k): int(v) for k, v in conn.execute(
            "SELECT status, COUNT(*) FROM journal_articles GROUP BY status"
        ).fetchall()}
        missing = conn.execute(
            "SELECT journal_name, title, metadata_json FROM journal_articles "
            "WHERE metadata_json LIKE '%ncpssd_id%' AND TRIM(abstract) = '' "
            "ORDER BY first_seen_at DESC LIMIT 6"
        ).fetchall()
    # 对缺摘要的中文文章做实时探测：判断是“NCPSSD 本就无摘要”还是“可补全但漏掉了”。
    samples = []
    for row in missing:
        meta = _json_loads(str(row["metadata_json"] or "{}"), {})
        lngid = str((meta or {}).get("ncpssd_id") or "")
        live = 0
        try:
            live = len((fetch_ncpssd_detail(lngid) or {}).get("abstract") or "")
        except Exception:
            live = -1
        samples.append({"journal": row["journal_name"], "title": str(row["title"])[:40], "lngid": lngid, "live_abstract_len": live})
    runs = list_recent_runs(3)
    return {
        "articles_total": total,
        "with_abstract": with_abs,
        "ncpssd_total": ncp,
        "ncpssd_with_abstract": ncp_abs,
        "by_status": by_status,
        "missing_samples": samples,
        "recent_runs": [
            {k: r.get(k) for k in ("status", "sources_checked", "articles_found", "articles_inserted", "emails_sent")}
            for r in runs
        ],
    }


def ncpssd_detail_probe(lngid: str = "MKSZYYJ2026004003") -> dict:
    """诊断：从当前主机调用 NCPSSD 详情接口，报告是否被 WAF 拦截（不抛异常）。"""
    import urllib.error

    out: dict[str, Any] = {"lngid": lngid}
    try:
        raw = _ncpssd_detail_raw(lngid)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return {**out, "ok": False, "stage": "http_error", "http_status": exc.code, "body_head": body}
    except Exception as exc:
        return {**out, "ok": False, "stage": "network_error", "error": repr(exc)[:200]}
    try:
        payload = json.loads(raw)
    except Exception:
        return {**out, "ok": False, "stage": "waf_or_html", "body_head": raw[:200]}
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    abstract = str((data or {}).get("remarkc") or "")
    return {
        "lngid": lngid,
        "ok": bool(abstract),
        "stage": "json_ok",
        "code": payload.get("code") if isinstance(payload, dict) else None,
        "abstract_len": len(abstract),
        "abstract_head": abstract[:60],
    }


def _parse_qstheory_html(text: str, source: dict, base_url: str) -> list[dict]:
    return _parse_generic_article_list(text, source, base_url, parser="qstheory_list")


def _parse_ncssd_cnki_html(text: str, source: dict, base_url: str) -> list[dict]:
    return _parse_generic_article_list(text, source, base_url, parser="ncssd_cnki_list")


def _parse_generic_article_list(text: str, source: dict, base_url: str, parser: str = "generic") -> list[dict]:
    config = _source_config(source)
    # 可选：仅保留 href 含该子串的链接（管理员可在控制台填写，用于排除导航/广告链接）。
    href_filter = str(config.get("link_selector") or "").strip()
    anchors = re.finditer(r"<a\b(?P<attrs>[^>]*)>(?P<title>.*?)</a>", text or "", flags=re.I | re.S)
    articles: list[dict] = []
    seen: set[str] = set()
    for match in anchors:
        attrs = match.group("attrs") or ""
        href_match = re.search(r"""href\s*=\s*["'](?P<href>[^"']+)["']""", attrs, flags=re.I)
        if not href_match:
            continue
        title = html.unescape(_strip_tags(match.group("title") or "")).strip()
        if not _looks_like_article_title(title):
            continue
        href = html.unescape(href_match.group("href")).strip()
        if href_filter and href_filter not in href:
            continue
        url = urllib.parse.urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        window = text[max(0, match.start() - 240) : min(len(text), match.end() + 360)]
        published = _extract_date_text(window)
        abstract = _extract_meta_description(text) if parser == "qstheory_list" and len(articles) == 0 else ""
        articles.append(_web_article(source, title, url, published, abstract, base_url, {"source": parser}))
        if len(articles) >= 25:
            break
    return articles


def _web_article(
    source: dict,
    title: str,
    url: str,
    published: str,
    abstract: str,
    base_url: str,
    metadata: dict | None = None,
) -> dict:
    return {
        "journal_name": source.get("name") or "",
        "language": source.get("language") or "zh",
        "title": html.unescape(str(title or "").strip()),
        "abstract": _strip_tags(html.unescape(str(abstract or "").strip())),
        "authors": [],
        "doi": "",
        "url": urllib.parse.urljoin(base_url, str(url or "").strip()),
        "published_at": str(published or "").strip(),
        "volume": "",
        "issue": "",
        "pages": "",
        # 网页抓取默认需人工审核；来源标记为可信(auto_publish)时直接进入发送队列。
        "requires_review": (
            str(source.get("source_type") or "").lower() == "web_html"
            and not bool(_source_config(source).get("auto_publish"))
        ),
        "metadata": metadata or {},
    }


def _looks_like_article_title(title: str) -> bool:
    if not title or len(title) < 4 or len(title) > 160:
        return False
    blocked = {
        "首页",
        "上一页",
        "下一页",
        "更多",
        "投稿",
        "登录",
        "注册",
        "目录",
        "期刊简介",
        "联系我们",
        "版权声明",
    }
    text = title.strip()
    if text in blocked or text in _NON_ARTICLE_EXACT:
        return False
    lower = text.lower()
    for kw in _NON_ARTICLE_SUBSTRINGS_EN:
        if kw in lower:
            return False
    # \u4e2d\u6587\u975e\u8bba\u6587\u5b50\u4e32\uff1a\u77ed\u6807\u9898\uff08\u226416 \u5b57\uff09\u6574\u4f53\u5373\u680f\u76ee\u540d\uff0c\u547d\u4e2d\u5373\u5254\u9664\uff1b\u957f\u6807\u9898\u4ec5\u5f53\u4ee5\u8fd9\u4e9b\u8bcd\u5f00\u5934/\u7ed3\u5c3e\u65f6\u5254\u9664\uff0c\u907f\u514d\u8bef\u6740\u6b63\u6587\u542b\u8be5\u8bcd\u7684\u8bba\u6587\u3002
    for kw in _NON_ARTICLE_SUBSTRINGS_ZH:
        if kw in text and (len(text) <= 16 or text.startswith(kw) or text.endswith(kw)):
            return False
    # \u7eaf\u671f\u53f7/\u9875\u7801/\u5e74\u4efd\u7b49\u65e0\u8bed\u4e49\u4e32\u5254\u9664\u3002
    if re.fullmatch(r"[\u7b2c\d\s\u5e74\u5377\u671f\u9875\-\u2014~\u3001.,()\uff08\uff09]+", text):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", title))


# \u6574\u4f53\u5373\u4e3a\u975e\u8bba\u6587\u7684\u6807\u9898\uff08\u7cbe\u786e\u5339\u914d\uff09\u3002
_NON_ARTICLE_EXACT = {
    "\u603b\u76ee\u5f55", "\u672c\u671f\u76ee\u5f55", "\u5c01\u4e8c", "\u5c01\u4e09", "\u5c01\u5e95", "\u63d2\u9875", "\u5f69\u9875", "\u66f4\u6b63", "\u52d8\u8bef",
}
# \u6807\u9898\u5305\u542b\u8fd9\u4e9b\u5b50\u4e32\u5219\u503e\u5411\u5224\u4e3a\u975e\u8bba\u6587\uff08\u76ee\u5f55/\u901a\u77e5/\u65b0\u95fb/\u5377\u9996\u8bed/\u5b66\u9662\u52a8\u6001\u7b49\uff09\u3002
_NON_ARTICLE_SUBSTRINGS_ZH = (
    "\u76ee\u5f55", "\u603b\u76ee\u6b21", "\u76ee\u6b21", "\u8981\u76ee", "\u5377\u9996\u8bed", "\u7f16\u8005\u6309", "\u7f16\u540e", "\u7f16\u8f91\u90e8", "\u7a3f\u7ea6",
    "\u5f81\u7a3f", "\u5f81\u8ba2", "\u542f\u4e8b", "\u58f0\u660e", "\u901a\u77e5", "\u516c\u544a", "\u8d3a\u4fe1", "\u8d3a\u8bcd", "\u795d\u8bcd",
    "\u8981\u95fb", "\u7b80\u8baf", "\u5feb\u8baf", "\u5b66\u754c\u52a8\u6001", "\u5e7f\u544a", "\u62db\u8058", "\u81f4\u8c22", "\u9e23\u8c22",
    "\u64a4\u7a3f", "\u6295\u7a3f\u987b\u77e5", "\u7ea6\u7a3f", "\u4e2d\u5fc3\u7b80\u4ecb", "\u673a\u6784\u7b80\u4ecb",
    "\u6211\u6821", "\u6211\u9662", "\u672c\u6821", "\u672c\u9662", "\u53ec\u5f00", "\u9686\u91cd\u4e3e\u884c",
    "\u5c01\u9762", "\u5c01\u4e8c", "\u5c01\u4e09", "\u5c01\u5e95", "\u5f69\u9875", "\u63d2\u9875", "\u56fe\u7247\u62a5\u9053", "\u56fe\u7247\u65b0\u95fb",
)
_NON_ARTICLE_SUBSTRINGS_EN = (
    "table of contents", "editorial board", "editorial note",
    "issue information", "front matter", "back matter", "masthead",
    "call for papers", "announcement", "in memoriam", "erratum", "corrigendum",
    "correction to", "list of contributors", "acknowledgment",
    "advertisement", "cover image", "frontispiece", "index to volume",
)


# OpenAlex/Crossref \u4ec5\u4fdd\u7559\u6b63\u5f0f\u671f\u520a\u8bba\u6587\u7684 type\uff1btype \u7f3a\u5931\u65f6\u4fdd\u5b88\u4fdd\u7559\uff08\u4ea4\u6807\u9898\u8fc7\u6ee4\u515c\u5e95\uff09\u3002
_OPENALEX_ARTICLE_TYPES = {"article", "journal-article", "review"}
_CROSSREF_ARTICLE_TYPES = {"journal-article"}


def _is_allowed_work_type(value: str, allowed: set[str]) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return True
    return text in allowed


def _extract_date_text(text: str) -> str:
    match = re.search(r"(20\d{2}|19\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text or "")
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"(20\d{2}|19\d{2})[-/.年](\d{1,2})", text or "")
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-01"
    match = re.search(r"(20\d{2}|19\d{2})", text or "")
    return f"{int(match.group(1)):04d}-01-01" if match else ""


def _extract_meta_description(text: str) -> str:
    match = re.search(
        r"""<meta\b[^>]*(?:name|property)\s*=\s*["'](?:description|og:description)["'][^>]*content\s*=\s*["'](?P<value>[^"']+)["']""",
        text or "",
        flags=re.I | re.S,
    )
    return html.unescape(match.group("value")).strip() if match else ""


def _find_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text
    return ""


def _strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def _dedupe_key(article: dict) -> str:
    doi = str(article.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    url = str(article.get("url") or "").strip().lower()
    if url:
        return "url:" + url
    raw = "|".join(
        [
            str(article.get("journal_name") or "").strip().lower(),
            str(article.get("title") or "").strip().lower(),
            ",".join(article.get("authors") or []),
            str(article.get("published_at") or "").strip(),
        ]
    )
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def gb2015_citation(article: dict) -> str:
    authors = article.get("authors") or []
    author_text = ", ".join(authors[:3])
    if len(authors) > 3:
        author_text += ", 等" if str(article.get("language") or "").lower().startswith("zh") else ", et al"
    if not author_text:
        author_text = "佚名"
    title = str(article.get("title") or "").strip()
    journal = str(article.get("journal_name") or "").strip()
    year = (str(article.get("published_at") or "").strip()[:4] or "出版年不详")
    volume = str(article.get("volume") or "").strip()
    issue = str(article.get("issue") or "").strip()
    pages = str(article.get("pages") or "").strip()
    vol_issue = ""
    if volume and issue:
        vol_issue = f", {volume}({issue})"
    elif volume:
        vol_issue = f", {volume}"
    elif issue:
        vol_issue = f", ({issue})"
    page_text = f": {pages}" if pages else ""
    doi = str(article.get("doi") or "").strip()
    doi_text = f". DOI: {doi}" if doi else ""
    return f"{author_text}. {title}[J]. {journal}, {year}{vol_issue}{page_text}{doi_text}."


def _needs_translation(article: dict) -> bool:
    return str(article.get("language") or "").lower().startswith("en")


def _translate_article(article: dict, translate: Callable[[dict], dict] | None) -> tuple[str, str, str]:
    if not _needs_translation(article):
        return "", "", "ready"
    if translate is None:
        return "", "", "translation_pending"
    translated = translate(article)
    title_zh = str(translated.get("title_zh") or "").strip()
    abstract_zh = str(translated.get("abstract_zh") or "").strip()
    return title_zh, abstract_zh, "ready" if title_zh and (abstract_zh or not article.get("abstract")) else "translation_pending"


def make_ai_translator(ai_client: Any) -> Callable[[dict], dict] | None:
    if not ai_client or not getattr(getattr(ai_client, "config", None), "enabled", False):
        return None

    def _translate(article: dict) -> dict:
        prompt = {
            "title": article.get("title") or "",
            "abstract": article.get("abstract") or "",
        }
        content = ai_client.chat_complete(
            [
                {
                    "role": "system",
                    "content": "你是严谨的学术翻译助手。请把英文论文题名和摘要译为中文，只返回 JSON。",
                },
                {
                    "role": "user",
                    "content": (
                        "请返回形如 {\"title_zh\":\"...\",\"abstract_zh\":\"...\"} 的 JSON，"
                        "不要添加解释。\n\n"
                        + json.dumps(prompt, ensure_ascii=False)
                    ),
                },
            ],
            max_tokens=1200,
        )
        try:
            parsed = json.loads(_extract_json_object(content))
        except json.JSONDecodeError:
            return {"title_zh": "", "abstract_zh": ""}
        return {
            "title_zh": str(parsed.get("title_zh") or "").strip(),
            "abstract_zh": str(parsed.get("abstract_zh") or "").strip(),
        }

    return _translate


def _extract_json_object(value: str) -> str:
    text = (value or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def upsert_article(
    source: dict,
    article: dict,
    translate: Callable[[dict], dict] | None = None,
    force_publish: bool = False,
    batch_id: int | None = None,
) -> tuple[dict, bool]:
    normalized = {
        **article,
        "journal_name": article.get("journal_name") or source.get("name") or "",
        "language": article.get("language") or source.get("language") or "zh",
        "authors": article.get("authors") or [],
    }
    normalized["citation_gb2015"] = gb2015_citation(normalized)
    normalized["dedupe_key"] = _dedupe_key(normalized)
    title_zh, abstract_zh, translated_status = _translate_article(normalized, translate)
    requested_status = str(article.get("status") or "").strip()
    if requested_status:
        status = requested_status
    elif article.get("requires_review") and not force_publish:
        status = "pending_review"
    else:
        status = translated_status
    now = utc_now_text()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM journal_articles WHERE dedupe_key = ?",
            (normalized["dedupe_key"],),
        ).fetchone()
        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO journal_articles(
                    source_id, journal_name, language, title, title_zh, abstract, abstract_zh,
                    authors_json, citation_gb2015, doi, url, published_at, volume, issue, pages,
                    dedupe_key, status, metadata_json, batch_id, first_seen_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(source["id"]),
                    normalized["journal_name"],
                    normalized["language"],
                    normalized.get("title") or "",
                    title_zh,
                    normalized.get("abstract") or "",
                    abstract_zh,
                    _json_dumps(normalized["authors"]),
                    normalized["citation_gb2015"],
                    normalized.get("doi") or "",
                    normalized.get("url") or "",
                    normalized.get("published_at") or "",
                    normalized.get("volume") or "",
                    normalized.get("issue") or "",
                    normalized.get("pages") or "",
                    normalized["dedupe_key"],
                    status,
                    _json_dumps(normalized.get("metadata") or {}),
                    int(batch_id) if batch_id is not None else None,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM journal_articles WHERE id = ?", (cur.lastrowid,)).fetchone()
            conn.commit()
            return _article_row(row), True
        existing_id = int(existing["id"])
        existing_status = str(existing["status"] or "")
        if existing_status == "translation_pending" and translate is not None:
            title_zh, abstract_zh, status = _translate_article(normalized, translate)
            conn.execute(
                """
                UPDATE journal_articles
                SET title_zh = ?, abstract_zh = ?, status = ?, updated_at = ?,
                    batch_id = COALESCE(?, batch_id)
                WHERE id = ?
                """,
                (title_zh, abstract_zh, status, now, batch_id, existing_id),
            )
            row = conn.execute("SELECT * FROM journal_articles WHERE id = ?", (existing_id,)).fetchone()
            conn.commit()
            return _article_row(row), False
        # 采集场景：把本窗口内已存在的文章并入当前批次；之前被归档的文章恢复到审核/发送队列。
        # 已被人工「忽略」的文章保持忽略，不再重新浮现，尊重人工决定。
        if batch_id is not None and existing_status != "ignored":
            if existing_status == "archived":
                if requested_status:
                    restored = requested_status
                elif article.get("requires_review") and not force_publish:
                    restored = "pending_review"
                else:
                    restored = translated_status
            else:
                restored = existing_status
            conn.execute(
                "UPDATE journal_articles SET batch_id = ?, status = ?, updated_at = ? WHERE id = ?",
                (int(batch_id), restored, now, existing_id),
            )
            row = conn.execute("SELECT * FROM journal_articles WHERE id = ?", (existing_id,)).fetchone()
            conn.commit()
            return _article_row(row), False
    return _article_row(existing), False


def _is_placeholder_pub_date(value: str, ncpssd_id: Any = None) -> bool:
    """判断 published_at 是否为「占位/不可靠」日期：空、仅年份、或 NCPSSD 列表写入的 YYYY-01-01。"""
    text = str(value or "").strip()
    if not text:
        return True
    if re.match(r"^\d{4}$", text):
        return True
    # NCPSSD 列表解析统一写成 1 月 1 日占位（真实日期需详情接口），视为占位。
    if ncpssd_id and text.endswith("-01-01"):
        return True
    return False


def _pub_date_bound(value: str) -> datetime | None:
    """把 published_at 解析为「在该粒度下最晚可能的时刻」，用于时间窗判断；无法解析返回 None。

    YYYY-MM-DD → 当日；YYYY-MM → 当月末附近（宽松，含整月）；仅年份/空 → None（视为未知）。
    """
    text = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{1,2})$", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), 28, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _apply_article_detail(article: dict, detail: dict) -> dict | None:
    """用详情接口返回的字段补全文章（摘要/作者/页码/DOI 等）并重算引文。返回更新后的行。"""
    if not detail:
        return None
    merged = dict(article)
    if detail.get("abstract") and not str(article.get("abstract") or "").strip():
        merged["abstract"] = detail["abstract"]
    if detail.get("authors") and not (article.get("authors") or []):
        merged["authors"] = detail["authors"]
    # published_at 特殊处理：详情给出真实日期时，覆盖列表写入的占位日期（YYYY-01-01）。
    ncpssd_id = (article.get("metadata") or {}).get("ncpssd_id")
    if detail.get("published_at") and _is_placeholder_pub_date(article.get("published_at"), ncpssd_id):
        merged["published_at"] = detail["published_at"]
    for key in ("pages", "doi", "issue"):
        if detail.get(key) and not str(article.get(key) or "").strip():
            merged[key] = detail[key]
    metadata = dict(article.get("metadata") or {})
    if detail.get("keywords"):
        metadata["keywords"] = detail["keywords"]
    citation = gb2015_citation(merged)
    with _connect() as conn:
        conn.execute(
            """
            UPDATE journal_articles
            SET abstract = ?, authors_json = ?, citation_gb2015 = ?, doi = ?, pages = ?,
                issue = ?, published_at = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                merged.get("abstract") or "",
                _json_dumps(merged.get("authors") or []),
                citation,
                merged.get("doi") or "",
                merged.get("pages") or "",
                merged.get("issue") or "",
                merged.get("published_at") or "",
                _json_dumps(metadata),
                utc_now_text(),
                int(article["id"]),
            ),
        )
        row = conn.execute("SELECT * FROM journal_articles WHERE id = ?", (int(article["id"]),)).fetchone()
        conn.commit()
    return _article_row(row) if row is not None else None


def backfill_ncpssd_abstracts(limit: int) -> int:
    """为已入库但仍缺摘要的 NCPSSD 文章补全摘要（一次最多 limit 篇）。返回补全成功数。"""
    if limit <= 0:
        return 0
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM journal_articles
            WHERE TRIM(abstract) = '' AND metadata_json LIKE '%ncpssd_id%'
              AND status IN ('ready', 'pending_review', 'translation_pending')
            ORDER BY first_seen_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    enriched = 0
    for row in rows:
        article = _article_row(row)
        lngid = str((article.get("metadata") or {}).get("ncpssd_id") or "")
        if not lngid:
            continue
        try:
            detail = fetch_ncpssd_detail(lngid)
        except Exception:
            continue
        if detail and detail.get("abstract") and _apply_article_detail(article, detail):
            enriched += 1
    return enriched


def active_subscriptions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, u.email AS user_account_email, u.display_name, u.role, u.is_active
            FROM journal_subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE s.status = 'active' AND u.is_active = 1
            ORDER BY s.created_at ASC, s.id ASC
            """
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def subscription_is_deliverable(subscription: dict, policy: dict | None = None) -> bool:
    if not int(subscription.get("is_active") or 0):
        return False
    policy = policy or load_access_policy()
    user = {
        "id": subscription.get("user_id"),
        "email": subscription.get("user_account_email") or subscription.get("email") or "",
        "role": subscription.get("role") or "member",
    }
    return feature_allowed_by_policy(policy, "journal_alerts", user)


def pending_ready_articles() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM journal_articles
            WHERE status = 'ready'
            ORDER BY first_seen_at ASC, id ASC
            """
        ).fetchall()
    return [_article_row(row) for row in rows]


def send_email(config: SMTPConfig, to_email: str, subject: str, text_body: str, html_body: str = "") -> None:
    if not config.enabled:
        raise RuntimeError("SMTP 未配置。")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{config.from_name} <{config.from_email}>" if config.from_name else config.from_email
    msg["To"] = to_email
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    if int(config.port) == 465:
        with smtplib.SMTP_SSL(config.host, config.port, timeout=30, context=ssl.create_default_context()) as smtp:
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(config.host, config.port, timeout=30) as smtp:
        if config.use_tls:
            smtp.starttls(context=ssl.create_default_context())
        if config.username:
            smtp.login(config.username, config.password)
        smtp.send_message(msg)


def send_confirmation_email(subscription: dict, base_url: str, smtp_config: SMTPConfig | None = None) -> None:
    smtp_config = smtp_config or load_smtp_config()
    if not base_url:
        raise RuntimeError("JOURNAL_ALERT_BASE_URL 或 PUBLIC_BASE_URL 未配置。")
    confirm_url = f"{base_url}/journal-alerts/confirm/{subscription['confirm_token']}" if base_url else ""
    subject = "请确认期刊新文提醒订阅"
    body = (
        "您好：\n\n"
        "请点击下面的链接确认期刊新文提醒订阅：\n"
        f"{confirm_url}\n\n"
        "如果这不是您本人操作，可以忽略本邮件。"
    )
    send_email(smtp_config, subscription["email"], subject, body, _plain_to_html(body))


def _plain_to_html(text: str) -> str:
    return "<p>" + html.escape(text).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def _markdown_to_html(text: str) -> str:
    """轻量 Markdown→HTML：支持 #-###### 标题、有序/无序列表、> 引用、空行分段、**加粗**、*斜体*、`代码`。

    输出干净语义标签，配合模板中的 .msg-body 样式（与阅读器「AI 讲解」一致）美化呈现。无第三方依赖。
    """
    lines = str(text or "").splitlines()
    out: list[str] = []
    list_kind: str | None = None  # 'ul' | 'ol' | None
    in_quote = False

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    def close_quote() -> None:
        nonlocal in_quote
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    def inline(s: str) -> str:
        escaped = html.escape(s)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])", r"<em>\1</em>", escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        return escaped

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_list()
            close_quote()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            close_list()
            close_quote()
            level = min(6, len(heading.group(1)))
            out.append(f"<h{level}>{inline(heading.group(2).strip())}</h{level}>")
            continue
        quote = re.match(r"^>\s?(.*)$", line)
        if quote:
            close_list()
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append(f"<p>{inline(quote.group(1).strip())}</p>")
            continue
        close_quote()
        ordered = re.match(r"^\d+[.)]\s+(.*)$", line)
        if ordered:
            if list_kind != "ol":
                close_list()
                out.append("<ol>")
                list_kind = "ol"
            out.append(f"<li>{inline(ordered.group(1).strip())}</li>")
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", line)
        if bullet:
            if list_kind != "ul":
                close_list()
                out.append("<ul>")
                list_kind = "ul"
            out.append(f"<li>{inline(bullet.group(1).strip())}</li>")
            continue
        close_list()
        out.append(f"<p>{inline(line.strip())}</p>")
    close_list()
    close_quote()
    return "\n".join(out)


def deliver_ready_articles(base_url: str = "", smtp_config: SMTPConfig | None = None) -> int:
    smtp_config = smtp_config or load_smtp_config()
    articles = pending_ready_articles()
    if not articles:
        return 0
    subscriptions = active_subscriptions()
    policy = load_access_policy()
    alert_settings = load_alert_settings()
    sent = 0
    for subscription in subscriptions:
        remaining = _new_articles_for_subscription(subscription, articles)
        if not remaining:
            continue
        if not subscription_is_deliverable(subscription, policy):
            for article in remaining:
                record_delivery(
                    int(article["id"]),
                    int(subscription["id"]),
                    str(subscription["email"]),
                    "skipped",
                    "期刊提醒权限未开放、会员已过期或账号已停用。",
                )
            continue
        subject = f"{alert_settings['subject_prefix']}：{len(remaining)} 篇新文章"
        text_body, html_body = render_articles_email(remaining, subscription, base_url, alert_settings)
        try:
            send_email(smtp_config, str(subscription["email"]), subject, text_body, html_body)
        except Exception as exc:
            for article in remaining:
                record_delivery(int(article["id"]), int(subscription["id"]), str(subscription["email"]), "failed", str(exc))
            continue
        now = utc_now_text()
        for article in remaining:
            record_delivery(int(article["id"]), int(subscription["id"]), str(subscription["email"]), "sent", "")
            mark_article_notified(int(article["id"]), now)
        mark_subscription_sent(int(subscription["id"]), now)
        sent += 1
    return sent


def _new_articles_for_subscription(subscription: dict, articles: list[dict]) -> list[dict]:
    cutoff = _parse_utc(str(subscription.get("confirmed_at") or subscription.get("created_at") or ""))
    if cutoff is None:
        candidates = articles
    else:
        candidates = [
            article
            for article in articles
            if (_parse_utc(str(article.get("first_seen_at") or "")) or utc_now()) >= cutoff
        ]
    return _undelivered_articles_for_subscription(int(subscription["id"]), candidates)


def _undelivered_articles_for_subscription(subscription_id: int, articles: list[dict]) -> list[dict]:
    ids = [int(article["id"]) for article in articles]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT article_id
            FROM journal_delivery_logs
            WHERE subscription_id = ? AND article_id IN ({placeholders}) AND status IN ('sent', 'skipped')
            """,
            (subscription_id, *ids),
        ).fetchall()
    delivered = {int(row["article_id"]) for row in rows}
    return [article for article in articles if int(article["id"]) not in delivered]


def render_articles_email(
    articles: list[dict],
    subscription: dict,
    base_url: str,
    settings: dict | None = None,
) -> tuple[str, str]:
    settings = normalize_alert_settings(settings)
    unsubscribe_url = (
        f"{base_url}/journal-alerts/unsubscribe/{subscription['unsubscribe_token']}"
        if base_url
        else ""
    )
    intro_text = str(settings["intro_text"])
    lines = [intro_text, ""]
    html_parts = [f"<p>{html.escape(intro_text)}</p>"]
    for idx, article in enumerate(articles, 1):
        authors = ", ".join(article.get("authors") or []) or "作者信息暂缺"
        title_line = article["title"]
        if article.get("title_zh"):
            title_line += f"\n中文题名：{article['title_zh']}"
        abstract = article.get("abstract") or "摘要暂缺"
        if article.get("abstract_zh"):
            abstract += f"\n中文摘要：{article['abstract_zh']}"
        article_lines = [f"{idx}. {title_line}" if settings["include_title"] else f"{idx}. 新文章"]
        if settings["include_journal"]:
            article_lines.append(f"期刊：{article.get('journal_name') or ''}")
        if settings["include_authors"]:
            article_lines.append(f"作者：{authors}")
        if settings["include_published_at"]:
            article_lines.append(f"发表日期：{article.get('published_at') or '暂缺'}")
        if settings["include_abstract"]:
            article_lines.append(f"摘要：{abstract}")
        if settings["include_citation"]:
            article_lines.append(f"引文：{article.get('citation_gb2015') or ''}")
        if settings["include_url"]:
            article_lines.append(f"链接：{article.get('url') or '暂缺'}")
        lines.extend([*article_lines, ""])

        article_html = "<section>"
        article_html += (
            f"<h3>{idx}. {html.escape(article['title'])}</h3>"
            if settings["include_title"]
            else f"<h3>{idx}. 新文章</h3>"
        )
        if settings["include_title"] and article.get("title_zh"):
            article_html += f"<p><strong>中文题名：</strong>{html.escape(article['title_zh'])}</p>"
        if settings["include_journal"]:
            article_html += f"<p><strong>期刊：</strong>{html.escape(article.get('journal_name') or '')}</p>"
        if settings["include_authors"]:
            article_html += f"<p><strong>作者：</strong>{html.escape(authors)}</p>"
        if settings["include_published_at"]:
            article_html += f"<p><strong>发表日期：</strong>{html.escape(article.get('published_at') or '暂缺')}</p>"
        if settings["include_abstract"]:
            article_html += f"<p><strong>摘要：</strong>{html.escape(article.get('abstract') or '摘要暂缺')}</p>"
            if article.get("abstract_zh"):
                article_html += f"<p><strong>中文摘要：</strong>{html.escape(article['abstract_zh'])}</p>"
        if settings["include_citation"]:
            article_html += f"<p><strong>引文：</strong>{html.escape(article.get('citation_gb2015') or '')}</p>"
        if settings["include_url"] and article.get("url"):
            article_html += f"<p><a href=\"{html.escape(article.get('url') or '')}\">查看来源</a></p>"
        article_html += "</section>"
        html_parts.append(article_html)
    if unsubscribe_url:
        lines.extend(["退订链接：", unsubscribe_url])
        html_parts.append(f"<p><a href=\"{html.escape(unsubscribe_url)}\">退订期刊新文提醒</a></p>")
    return "\n".join(lines), "\n".join(html_parts)


def record_delivery(article_id: int, subscription_id: int, email: str, status: str, error: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO journal_delivery_logs(article_id, subscription_id, email, status, error, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id, subscription_id) DO UPDATE SET
                status=excluded.status,
                error=excluded.error,
                created_at=excluded.created_at
            """,
            (article_id, subscription_id, email, status, error[:1200], utc_now_text()),
        )
        conn.commit()


def mark_article_notified(article_id: int, now: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE journal_articles
            SET notified_at = CASE WHEN notified_at = '' THEN ? ELSE notified_at END
            WHERE id = ?
            """,
            (now or utc_now_text(), article_id),
        )
        conn.commit()


def mark_subscription_sent(subscription_id: int, now: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE journal_subscriptions SET last_sent_at = ?, updated_at = ? WHERE id = ?",
            (now or utc_now_text(), now or utc_now_text(), subscription_id),
        )
        conn.commit()


def collect_batch(
    *,
    ai_client: Any = None,
    settings: dict | None = None,
    reuse_open_batch: bool = False,
) -> dict:
    """采集阶段：开新批次 → 抓取窗口内全部来源 → 补全摘要 →（可选）自动批准/生成综述。

    返回 journal_runs 行 + batch_id/batch_status。reuse_open_batch=True 时复用当前未发送批次
    （用于「仅生成综述/重抓」而不想重置批次的场景）。
    """
    init_journal_alerts_db()
    settings = settings or load_alert_settings()
    started = utc_now_text()
    with _connect() as conn:
        cur = conn.execute("INSERT INTO journal_runs(started_at) VALUES(?)", (started,))
        run_id = cur.lastrowid
        conn.commit()
    sources_checked = 0
    found = 0
    inserted = 0
    errors: list[str] = []
    translate = make_ai_translator(ai_client)
    # 自动批准：本批抓取的新文章直接进入发送队列（跳过人工审核）。
    # 兼容旧设置键 auto_publish_all；新键 auto_approve_articles 优先。
    force_publish = bool(settings.get("auto_approve_articles") or settings.get("auto_publish_all"))
    lookback_days = int(settings.get("lookback_days") or DEFAULT_LOOKBACK_DAYS)
    enrich_budget = NCPSSD_ENRICH_PER_RUN
    # 时间窗：只收录发表日期在最近 lookback_days 天内的文章（中英文期刊统一生效）。
    cutoff_dt = utc_now() - timedelta(days=lookback_days)
    filtered_out = 0
    batch = current_batch() if reuse_open_batch else None
    if batch is None:
        batch = open_batch(settings, period_days=lookback_days)
    batch_id = int(batch["id"])
    try:
        sources = [source for source in list_journal_sources(limit=200) if int(source.get("is_enabled") or 0)]
        for source in sources:
            try:
                articles = fetch_source_articles(source, lookback_days=lookback_days)
                sources_checked += 1
                found += len(articles)
                for article in articles:
                    meta = article.get("metadata") or {}
                    ncpssd_id = meta.get("ncpssd_id")
                    detail_cache = None
                    # NCPSSD 列表只给 YYYY-01-01 占位日期，需详情接口取真实日期后才能按窗口过滤（受预算限制）。
                    if ncpssd_id and enrich_budget > 0 and _is_placeholder_pub_date(article.get("published_at"), ncpssd_id):
                        try:
                            detail_cache = fetch_ncpssd_detail(ncpssd_id)
                        except Exception:
                            detail_cache = None
                        enrich_budget -= 1
                        if detail_cache and detail_cache.get("published_at"):
                            article["published_at"] = detail_cache["published_at"]
                    # 窗口判断：能解析到具体日期则严格比较；只到年份/未知则仅保留不早于窗口起始年的。
                    bound = _pub_date_bound(article.get("published_at"))
                    if bound is not None:
                        if bound < cutoff_dt:
                            filtered_out += 1
                            continue
                    else:
                        year_match = re.match(r"^(\d{4})", str(article.get("published_at") or ""))
                        if year_match and int(year_match.group(1)) < cutoff_dt.year:
                            filtered_out += 1
                            continue
                    row, created = upsert_article(
                        source, article, translate, force_publish=force_publish, batch_id=batch_id
                    )
                    if created:
                        inserted += 1
                    # 用详情补全摘要并把真实日期持久化（覆盖占位日期）；复用上面已取的详情避免二次请求。
                    needs_detail = ncpssd_id and (
                        not str(row.get("abstract") or "").strip()
                        or _is_placeholder_pub_date(row.get("published_at"), ncpssd_id)
                    )
                    if needs_detail:
                        if detail_cache is None and enrich_budget > 0:
                            try:
                                detail_cache = fetch_ncpssd_detail(ncpssd_id)
                            except Exception:
                                detail_cache = None
                            enrich_budget -= 1
                        if detail_cache:
                            try:
                                _apply_article_detail(row, detail_cache)
                            except Exception:
                                pass
                _mark_source_checked(int(source["id"]), "")
            except Exception as exc:
                sources_checked += 1
                errors.append(f"{source.get('name')}: {exc}")
                _mark_source_checked(int(source["id"]), str(exc))
        # 回填历史遗留、仍缺摘要的中文文章（独立预算）。
        try:
            backfill_ncpssd_abstracts(NCPSSD_ENRICH_PER_RUN)
        except Exception as exc:
            errors.append(f"abstract-backfill: {exc}")
        # 自动生成综述：auto_generate_review 或 auto_send 任一开启即生成（auto_send 隐含需要综述）。
        # auto_send 时连带自动批准综述，从而发送日定时器可直接群发，实现全流程自动化。
        want_review = settings.get("auto_generate_review") or settings.get("auto_send")
        if want_review and not settings.get("automation_paused"):
            try:
                generate_batch_review(
                    batch_id, ai_client=ai_client, settings=settings,
                    auto_approve=bool(settings.get("auto_send")),
                )
            except Exception as exc:
                errors.append(f"review: {exc}")
        status = "warning" if errors else "success"
    except Exception as exc:
        errors.append(str(exc))
        status = "failed"
    finished = utc_now_text()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE journal_runs
            SET finished_at = ?, status = ?, sources_checked = ?, articles_found = ?,
                articles_inserted = ?, emails_sent = 0, error = ?
            WHERE id = ?
            """,
            (finished, status, sources_checked, found, inserted, "\n".join(errors)[:4000], int(run_id)),
        )
        row = conn.execute("SELECT * FROM journal_runs WHERE id = ?", (int(run_id),)).fetchone()
        conn.commit()
    result = _row_to_dict(row) or {}
    result["batch_id"] = batch_id
    refreshed = get_batch(batch_id) or {}
    result["batch_status"] = refreshed.get("status")
    result["review_status"] = refreshed.get("review_status")
    # 本批次实际纳入的文章数（含本窗口内复用的已有文章；新增数仅统计首次入库）。
    result["batch_total"] = len(batch_articles(batch_id))
    result["batch_pending"] = len(batch_articles(batch_id, statuses=("pending_review",)))
    result["filtered_out"] = filtered_out
    return result


def generate_batch_review(
    digest_id: int,
    *,
    ai_client: Any = None,
    settings: dict | None = None,
    auto_approve: bool = False,
) -> dict:
    """调用 AI 为某批次生成文献综述并写回批次。auto_approve=True 时直接批准（全自动）。"""
    # 延迟导入，避免与 journal_review 形成模块级循环依赖。
    from journal_review import build_literature_review

    settings = settings or load_alert_settings()
    articles = batch_articles(digest_id, statuses=("ready", "pending_review"))
    review_md, review_html, model_used = build_literature_review(
        articles, ai_client=ai_client, settings=settings
    )
    update_batch_review(
        digest_id,
        review_md=review_md,
        review_html=review_html,
        review_model=model_used,
        review_status="approved" if auto_approve else "pending",
        status="ready_to_send" if auto_approve else "reviewing",
        mark_generated=True,
        mark_approved=auto_approve,
    )
    return get_batch(digest_id) or {}


RECIPIENT_MODES = ("subscribers", "members", "registered", "specific")


def resolve_recipients(
    mode: str,
    *,
    plan_codes: list[str] | None = None,
    emails: list[str] | None = None,
) -> tuple[list[dict], bool]:
    """把受众模式解析为收件人列表，并返回是否需要按 journal_alerts 权限过滤。

    返回 (recipients, enforce_permission)。recipients 每项 {email, user_id?, subscription_id?, unsubscribe_token?}。
    - subscribers：邮箱订阅者，**需权限校验**；
    - members/registered/specific：管理员强制群发，**忽略权限**。
    """
    mode = (mode or "subscribers").strip().lower()
    if mode == "members":
        from membership import list_active_member_emails

        rows = list_active_member_emails(plan_codes)
        return ([{"email": r["email"], "user_id": r.get("user_id")} for r in rows if r.get("email")], False)
    if mode == "registered":
        from membership import list_active_user_emails

        rows = list_active_user_emails()
        return ([{"email": r["email"], "user_id": r.get("user_id")} for r in rows if r.get("email")], False)
    if mode == "specific":
        seen: set[str] = set()
        out: list[dict] = []
        for raw in emails or []:
            addr = normalize_email(str(raw))
            if addr and addr not in seen:
                seen.add(addr)
                out.append({"email": addr})
        return (out, False)
    # 默认：邮箱订阅者，保留权限校验。
    out = [
        {
            "email": s.get("email"),
            "user_id": s.get("user_id"),
            "subscription_id": s.get("id"),
            "unsubscribe_token": s.get("unsubscribe_token"),
            "_subscription": s,
        }
        for s in active_subscriptions()
        if s.get("email")
    ]
    return (out, True)


def send_batch(
    *,
    base_url: str = "",
    smtp_config: SMTPConfig | None = None,
    digest_id: int | None = None,
    force: bool = False,
    recipients: list[dict] | None = None,
    enforce_permission: bool = True,
) -> dict:
    """发送阶段：把已批准批次的文献综述发给给定收件人（默认=按设置受众解析），按邮箱去重。

    recipients=None 时按 settings.send_audience（自动发送）解析；显式传入则用之（控制台手选受众）。
    """
    init_journal_alerts_db()
    smtp_config = smtp_config or load_smtp_config()
    settings = load_alert_settings()
    batch = get_batch(digest_id) if digest_id is not None else current_batch()
    if not batch:
        return {"sent": 0, "reason": "no_batch"}
    batch_id = int(batch["id"])
    approved = str(batch.get("review_status") or "") == "approved"
    if not approved and not force:
        return {"sent": 0, "batch_id": batch_id, "reason": "review_not_approved"}
    review_html = str(batch.get("review_html") or "").strip()
    review_md = str(batch.get("review_md") or "").strip()
    if not review_html and not review_md:
        return {"sent": 0, "batch_id": batch_id, "reason": "review_empty"}
    if recipients is None:
        recipients, enforce_permission = resolve_recipients(
            str(settings.get("send_audience") or "subscribers"),
            plan_codes=settings.get("send_audience_plans") or [],
        )
    policy = load_access_policy() if enforce_permission else {}
    already = _digest_delivered_emails(batch_id)
    started = utc_now_text()
    with _connect() as conn:
        cur = conn.execute("INSERT INTO journal_runs(started_at) VALUES(?)", (started,))
        run_id = cur.lastrowid
        conn.commit()
    sent = 0
    errors: list[str] = []
    subject = f"{settings['subject_prefix']}：本期文献综述"
    for recipient in recipients:
        email = normalize_email(str(recipient.get("email") or ""))
        if not email or email in already:
            continue
        already.add(email)
        sub_id = recipient.get("subscription_id")
        user_id = recipient.get("user_id")
        if enforce_permission:
            sub = recipient.get("_subscription") or {}
            if not subscription_is_deliverable(sub, policy):
                record_digest_delivery(batch_id, email, "skipped",
                                       "期刊提醒权限未开放、会员已过期或账号已停用。",
                                       subscription_id=sub_id, user_id=user_id)
                continue
        text_body, html_body = render_review_email(batch, recipient, base_url, settings)
        try:
            send_email(smtp_config, email, subject, text_body, html_body)
        except Exception as exc:
            errors.append(f"{email}: {exc}")
            record_digest_delivery(batch_id, email, "failed", str(exc), subscription_id=sub_id, user_id=user_id)
            continue
        record_digest_delivery(batch_id, email, "sent", "", subscription_id=sub_id, user_id=user_id)
        if sub_id:
            mark_subscription_sent(int(sub_id), utc_now_text())
        sent += 1
    if sent > 0 or approved:
        update_batch_review(batch_id, status="sent")
        with _connect() as conn:
            conn.execute(
                "UPDATE journal_digests SET sent_at = ?, emails_sent = emails_sent + ?, updated_at = ? WHERE id = ?",
                (utc_now_text(), sent, utc_now_text(), batch_id),
            )
            conn.commit()
        # 归档更早的已发送批次，使首页只留存最新一期（本期内容留存到下一期发送）。
        archive_sent_batches_before(batch_id)
    finished = utc_now_text()
    with _connect() as conn:
        conn.execute(
            "UPDATE journal_runs SET finished_at = ?, status = ?, emails_sent = ?, error = ? WHERE id = ?",
            (finished, "warning" if errors else "success", sent, "\n".join(errors)[:4000], int(run_id)),
        )
        conn.commit()
    return {"sent": sent, "batch_id": batch_id, "errors": errors}


def _digest_delivered_emails(digest_id: int) -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT email FROM journal_digest_deliveries "
            "WHERE digest_id = ? AND status IN ('sent', 'skipped')",
            (int(digest_id),),
        ).fetchall()
    return {normalize_email(str(row["email"])) for row in rows}


def record_digest_delivery(
    digest_id: int,
    email: str,
    status: str,
    error: str = "",
    *,
    subscription_id: int | None = None,
    user_id: int | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO journal_digest_deliveries(digest_id, subscription_id, user_id, email, status, error, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(digest_id, email) DO UPDATE SET
                status=excluded.status, error=excluded.error, created_at=excluded.created_at,
                subscription_id=COALESCE(excluded.subscription_id, journal_digest_deliveries.subscription_id),
                user_id=COALESCE(excluded.user_id, journal_digest_deliveries.user_id)
            """,
            (
                int(digest_id),
                int(subscription_id) if subscription_id else None,
                int(user_id) if user_id else None,
                email,
                status,
                error[:1200],
                utc_now_text(),
            ),
        )
        conn.commit()


_EMAIL_INLINE_STYLES = {
    "<h1>": '<h1 style="font-size:20px;color:#5f1717;border-bottom:2px solid #f5e2dc;padding-bottom:6px;margin:18px 0 10px">',
    "<h2>": '<h2 style="font-size:17px;color:#5f1717;border-left:4px solid #8f1d1d;background:#f7ede6;padding:7px 11px;border-radius:8px;margin:18px 0 8px">',
    "<h3>": '<h3 style="font-size:15px;color:#7a2222;margin:14px 0 6px">',
    "<h4>": '<h4 style="font-size:14px;color:#7a2222;margin:12px 0 6px">',
    "<blockquote>": '<blockquote style="border-left:4px solid rgba(143,29,29,.5);background:#fffdf8;margin:10px 0;padding:8px 12px;color:#4a3b30;border-radius:8px">',
    "<p>": '<p style="margin:0 0 10px;line-height:1.85;color:#231d17">',
    "<ul>": '<ul style="margin:0 0 10px;padding-left:22px;line-height:1.85;color:#231d17">',
    "<ol>": '<ol style="margin:0 0 10px;padding-left:22px;line-height:1.85;color:#231d17">',
    "<li>": '<li style="margin:0 0 4px">',
    "<strong>": '<strong style="color:#5f1717">',
    "<em>": '<em style="color:#7a2222;font-style:normal;font-weight:700">',
    "<code>": '<code style="background:rgba(143,29,29,.08);color:#5f1717;padding:1px 6px;border-radius:6px;font-family:Consolas,monospace">',
}


def _email_styled_html(inner_html: str) -> str:
    """把综述 HTML 套上内联样式（邮件客户端不读外链 CSS），与首页/阅读器排版一致。"""
    styled = inner_html
    for tag, replacement in _EMAIL_INLINE_STYLES.items():
        styled = styled.replace(tag, replacement)
    return (
        '<div style="font-family:\'Microsoft YaHei\',\'Noto Sans CJK SC\',sans-serif;'
        'max-width:760px;margin:0 auto;padding:8px 4px;color:#231d17">' + styled + "</div>"
    )


def render_review_email(
    digest: dict,
    subscription: dict,
    base_url: str,
    settings: dict | None = None,
) -> tuple[str, str]:
    settings = normalize_alert_settings(settings)
    token = str(subscription.get("unsubscribe_token") or "")
    unsubscribe_url = f"{base_url}/journal-alerts/unsubscribe/{token}" if (base_url and token) else ""
    latest_url = f"{base_url}/journal-alerts/latest" if base_url else ""
    review_md = str(digest.get("review_md") or "")
    review_html = str(digest.get("review_html") or "").strip() or _markdown_to_html(review_md)
    intro = str(settings["intro_text"])
    text_body = f"{intro}\n\n{review_md}"
    html_body = (
        f'<p style="line-height:1.85;color:#231d17">{html.escape(intro)}</p>\n'
        + _email_styled_html(review_html)
    )
    footer_links = []
    if latest_url:
        text_body += f"\n\n查看本期全部文章：{latest_url}"
        footer_links.append(f'<a href="{html.escape(latest_url)}" style="color:#8f1d1d">查看本期全部文章</a>')
    if unsubscribe_url:
        text_body += f"\n\n退订链接：{unsubscribe_url}"
        footer_links.append(f'<a href="{html.escape(unsubscribe_url)}" style="color:#72675d">退订期刊新文提醒</a>')
    if footer_links:
        html_body += (
            '<p style="margin-top:16px;padding-top:12px;border-top:1px solid #e7dccb;'
            'font-size:13px;color:#72675d">' + " &nbsp;·&nbsp; ".join(footer_links) + "</p>"
        )
    return text_body, html_body


def run_journal_alerts_once(
    *,
    ai_client: Any = None,
    base_url: str = "",
    smtp_config: SMTPConfig | None = None,
    send: bool = True,
) -> dict:
    """兼容入口：采集一个批次；send=True 时尝试发送当前批次（仅在综述已批准时实际发出）。"""
    result = collect_batch(ai_client=ai_client)
    if send:
        try:
            outcome = send_batch(base_url=base_url, smtp_config=smtp_config)
            result["emails_sent"] = int(outcome.get("sent") or 0)
        except Exception as exc:
            result.setdefault("error", "")
            result["error"] = (str(result.get("error") or "") + f"\nsend: {exc}").strip()
    return result


def _mark_source_checked(source_id: int, error: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE journal_sources
            SET last_checked_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (utc_now_text(), error[:1200], utc_now_text(), source_id),
        )
        conn.commit()
