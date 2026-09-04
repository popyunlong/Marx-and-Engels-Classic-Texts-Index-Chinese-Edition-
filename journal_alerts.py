from __future__ import annotations

import hashlib
import html
import json
import logging
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

import membership as membership_store
from admin_store import get_setting as get_legacy_setting, init_admin_store_db
from feature_access import feature_allowed_by_policy, load_access_policy
from membership import init_membership_db, normalize_email
from runtime_env import APPDATA_DIR, DeploymentSettings, secure_db_file
from journal_storage import JOURNAL_DB_PATH, JOURNAL_TMP_DIR, ensure_journal_storage


LOGGER = logging.getLogger("marx_search.journal_alerts")
_DISCOVERY_WARNINGS: dict[str, str] = {}


# Journal records live in their own database on the journal data-disk mount.
# ``DB_PATH`` remains assignable because the zero-network tests use a temporary
# database, but production resolves it from ``MARX_JOURNAL_DATA_ROOT``.
DB_PATH = JOURNAL_DB_PATH
LEGACY_DB_PATH = APPDATA_DIR / "membership.sqlite3"
DEFAULT_LOOKBACK_DAYS = 7
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
JOURNAL_WEEKLY_TITLE = "国外文献精选周刊"
JOURNAL_WEEKLY_TITLE_EN = "Selected International Scholarship Weekly"
_LEGACY_DEFAULT_SUBJECT_PREFIXES = {
    "马克思主义与哲学英文期刊周刊",
    "英文期刊周刊",
}

DEFAULT_ALERT_SETTINGS = {
    "subject_prefix": JOURNAL_WEEKLY_TITLE,
    "intro_text": "您好，以下为本周已经取得公开 PDF 并完成中英双语处理的英文期刊文章。",
    "include_title": True,
    "include_journal": True,
    "include_authors": True,
    "include_published_at": True,
    "include_abstract": True,
    "include_citation": True,
    "include_url": True,
    # 仅指元数据采集；对外发布仍必须经过全文完整性门槛和整期人工批准。
    "auto_publish_all": False,
    # 产品固定每周发送；send_weekday(0=周一..6=周日) 选择发送日。
    "send_frequency": "weekly",
    "send_weekday": 0,
    # 周刊固定覆盖采集日及此前 6 个北京时间自然日。
    "lookback_days": 7,
    # 发送日的发送时间（北京时间 HH:MM）。采集在发送日前一天 19:00（由 systemd timer 控制）。
    "send_time": "08:00",
    # 旧版兼容键：归一化时固定关闭；发布必须经过整期人工批准。
    "auto_approve_articles": False,
    "auto_generate_review": False,
    "auto_send": False,
    # 总闸：开启后采集/综述/发送各阶段一律暂停，便于随时人工干预。
    "automation_paused": False,
    # 归档的旧批次文章是否在下次采集时硬删除（默认仅归档保留）。
    "hard_delete_archived": False,
    # 固定每期最多 45 篇完整英文文章，超额按期刊轮转后顺延。
    "weekly_release_cap": 45,
    # 旧版综述模型键，只为迁移读取；新系统目录不调用综述模型。
    "review_model": "",
    # 定时自动发送的默认受众仅限：subscribers（已订阅且仍为有效会员）/ members（全部有效会员）。
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
    # —— 政治经济学相关刊（2026-06 新增）——
    "当代经济研究": {"issn": "1005-2674"},   # 吉林财大·中国《资本论》研究会会刊
    # 政治经济学评论：不在 NCPSSD 开放库、OpenAlex 也无收录，改抓人大官网（玛格泰克平台当期目录）。
    "政治经济学评论": {
        "issn": "1674-7542",
        "source_type": "web_html",
        "source_url": "http://crpe.ruc.edu.cn/CN/current",
        "config": {"parser": "magtech_journal", "entry_url": "http://crpe.ruc.edu.cn/CN/current"},
    },
    "政治经济学季刊": {"issn": "2097-1516"}, # 清华大学·CSSCI 集刊（已获 CN 刊号）
    "经济纵横": {"issn": "1007-7685"},        # 吉林省社科院
    # 《政治经济学报》（孟捷主编）为 ISBN 集刊，无 ISSN/CN，亦未在 NCPSSD 期刊库；
    # 保持 manual（控制台显示“待补充来源”），管理员可在 /admin 补录来源后启用自动抓取。
    "政治经济学报": {},
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
    # —— 政治经济学相关刊（2026-06 新增，逐刊核对 刊名+ISSN ↔ NCPSSD gch 一致）——
    "当代经济研究": "97946X",
    "经济纵横": "92389X",
    "政治经济学季刊": "73151X",
    # 《政治经济学评论》不在 NCPSSD 开放库，改抓人大官网（见 CN_JOURNAL_SOURCE_DEFAULTS 的 magtech_journal 配置）。
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
    # —— 政治经济学相关期刊（2026-06 新增）——
    _default_source("当代经济研究"),
    _default_source("政治经济学评论"),
    _default_source("政治经济学季刊"),
    _default_source("政治经济学报"),
    _default_source("经济纵横"),
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

# The Chinese rows above are retained as migration/audit knowledge only.  The
# active registry is English-only: the existing 21 titles plus 24 philosophy
# and critical-theory titles.  Source discovery is metadata-only; publication
# still requires a verified public PDF and a complete bilingual document.
LEGACY_JOURNAL_SOURCES = DEFAULT_JOURNAL_SOURCES
_EXISTING_ENGLISH_SOURCES = tuple(
    source for source in LEGACY_JOURNAL_SOURCES
    if str(source.get("language") or "").lower().startswith("en")
)
_PHILOSOPHY_SOURCE_ADDITIONS: tuple[dict[str, Any], ...] = (
    {"name": "Radical Philosophy", "language": "en", "issn": "0300-211X", "source_type": "openalex"},
    {"name": "Philosophy & Social Criticism", "language": "en", "issn": "0191-4537", "source_type": "openalex",
     "config": {"alternate_issn": ["1461-734X"]}},
    {"name": "Constellations", "language": "en", "issn": "1351-0487", "source_type": "openalex",
     "config": {"alternate_issn": ["1467-8675"]}},
    {"name": "Critical Horizons", "language": "en", "issn": "1440-9917", "source_type": "openalex"},
    {"name": "Theory, Culture & Society", "language": "en", "issn": "0263-2764", "source_type": "openalex"},
    {"name": "Thesis Eleven", "language": "en", "issn": "0725-5136", "source_type": "openalex"},
    {"name": "European Journal of Philosophy", "language": "en", "issn": "0966-8373", "source_type": "openalex",
     "config": {"alternate_issn": ["1468-0378"]}},
    {"name": "Hegel Bulletin", "language": "en", "issn": "0263-5232", "source_type": "openalex"},
    {"name": "Continental Philosophy Review", "language": "en", "issn": "1387-2842", "source_type": "openalex",
     "config": {"alternate_issn": ["1573-1103"]}},
    {"name": "Inquiry", "language": "en", "issn": "0020-174X", "source_type": "openalex"},
    {"name": "Mind", "language": "en", "issn": "0026-4423", "source_type": "openalex"},
    {"name": "The Philosophical Review", "language": "en", "issn": "0031-8108", "source_type": "openalex"},
    {"name": "The Journal of Philosophy", "language": "en", "issn": "0022-362X", "source_type": "openalex"},
    {"name": "Noûs", "language": "en", "issn": "0029-4624", "source_type": "openalex"},
    {"name": "Philosophy and Phenomenological Research", "language": "en", "issn": "0031-8205", "source_type": "openalex"},
    {"name": "Ethics", "language": "en", "issn": "0014-1704", "source_type": "openalex"},
    {"name": "Philosophy & Public Affairs", "language": "en", "issn": "0048-3915", "source_type": "openalex"},
    {"name": "Journal of Political Philosophy", "language": "en", "issn": "0963-8016", "source_type": "openalex"},
    {"name": "The Philosophical Quarterly", "language": "en", "issn": "0031-8094", "source_type": "openalex"},
    {"name": "Analysis", "language": "en", "issn": "0003-2638", "source_type": "openalex"},
    {"name": "Australasian Journal of Philosophy", "language": "en", "issn": "0004-8402", "source_type": "openalex"},
    {"name": "Philosophical Studies", "language": "en", "issn": "0031-8116", "source_type": "openalex"},
    {"name": "British Journal for the History of Philosophy", "language": "en", "issn": "0960-8788", "source_type": "openalex"},
    {"name": "Journal of the History of Philosophy", "language": "en", "issn": "0022-5053", "source_type": "openalex",
     "config": {"alternate_issn": ["1538-4586"]}},
)
DEFAULT_JOURNAL_SOURCES = _EXISTING_ENGLISH_SOURCES + _PHILOSOPHY_SOURCE_ADDITIONS

_JOURNAL_CATALOG_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "马克思主义与批判理论",
        "MARXISM & CRITICAL THEORY",
        (
            "Historical Materialism: Research in Critical Marxist Theory", "Rethinking Marxism",
            "Science & Society: A Journal of Marxist Thought and Analysis", "Monthly Review",
            "New Left Review", "Critique: Journal of Socialist Theory", "Socialist Register",
            "International Critical Thought", "Radical Philosophy", "Philosophy & Social Criticism",
            "Constellations", "Critical Horizons", "Theory, Culture & Society", "Thesis Eleven",
        ),
    ),
    (
        "政治经济学与社会研究",
        "POLITICAL ECONOMY & SOCIETY",
        (
            "Capital & Class", "Capitalism Nature Socialism", "Cambridge Journal of Economics",
            "Review of Radical Political Economics", "Review of Political Economy",
            "Journal of Economic Issues", "Structural Change and Economic Dynamics",
            "Economic Geography", "Journal of Institutional Economics",
            "International Journal of Political Economy", "New Political Economy",
            "Review of Development Economics", "Economy and Society",
        ),
    ),
    (
        "哲学核心期刊",
        "CORE PHILOSOPHY",
        tuple(str(source["name"]) for source in _PHILOSOPHY_SOURCE_ADDITIONS[6:]),
    ),
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
    if Path(DB_PATH) == JOURNAL_DB_PATH:
        ensure_journal_storage()
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    secure_db_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _membership_schema(conn: sqlite3.Connection) -> str:
    """Attach the account database for read-only cross-database user joins."""
    membership_store.init_membership_db()
    membership_path = Path(membership_store.DB_PATH).resolve()
    if membership_path == Path(DB_PATH).resolve():
        return "main"
    conn.execute("ATTACH DATABASE ? AS membership_accounts", (str(membership_path),))
    return "membership_accounts"


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
                if aud in {"subscribers", "members"}:
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
            elif key == "weekly_release_cap":
                if key in raw:
                    try:
                        values[key] = max(0, min(1000, int(raw[key])))
                    except (TypeError, ValueError):
                        pass
            elif key in raw:
                values[key] = bool(raw[key])
    # Migrate only historical built-in names; an administrator's custom subject
    # remains untouched.
    if values["subject_prefix"] in _LEGACY_DEFAULT_SUBJECT_PREFIXES:
        values["subject_prefix"] = JOURNAL_WEEKLY_TITLE
    # Product invariants for the bilingual weekly journal.  Legacy settings are
    # read for migration but cannot re-enable daily delivery, AI reviews,
    # automatic publication, or destructive archive deletion.
    values["send_frequency"] = "weekly"
    values["lookback_days"] = 7
    values["weekly_release_cap"] = 45
    values["auto_publish_all"] = False
    values["auto_approve_articles"] = False
    values["auto_generate_review"] = False
    values["auto_send"] = False
    values["hard_delete_archived"] = False
    return values


def load_alert_settings() -> dict:
    """Load journal-owned settings from the data-disk journal database.

    On the first production read only, copy the legacy unified-setting value;
    subsequent reads and all writes are journal-local.
    """
    init_journal_alerts_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM journal_meta WHERE key='alert_settings_v2'"
        ).fetchone()
    if row is not None:
        try:
            return normalize_alert_settings(json.loads(str(row["value"] or "{}")))
        except (TypeError, ValueError):
            return normalize_alert_settings({})
    legacy: dict = {}
    if Path(DB_PATH).resolve() == Path(JOURNAL_DB_PATH).resolve():
        try:
            init_admin_store_db()
            raw = get_legacy_setting("journal_alerts_settings", {})
            legacy = raw if isinstance(raw, dict) else {}
        except Exception:
            legacy = {}
    return save_alert_settings(legacy)


def save_alert_settings(raw: dict | None) -> dict:
    values = normalize_alert_settings(raw)
    init_journal_alerts_db()
    now = utc_now_text()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO journal_meta(key, value, updated_at)
            VALUES('alert_settings_v2', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (_json_dumps(values), now),
        )
        conn.commit()
    return values


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
    """Collect once weekly, on the Beijing-calendar day before the send day.

    The systemd timer checks this predicate daily at 19:00 Beijing time so a
    console change to ``send_weekday`` takes effect without rewriting the timer.
    """
    settings = settings or load_alert_settings()
    if bool(settings.get("automation_paused")):
        return False
    now = now or utc_now()
    send_weekday = max(0, min(6, int(settings.get("send_weekday") or 0)))
    collect_weekday = (send_weekday - 1) % 7
    return now.astimezone(BEIJING_TZ).weekday() == collect_weekday


def weekly_collection_window(now: datetime | None = None, days: int = 7) -> tuple[datetime, datetime]:
    """Return the inclusive Beijing-calendar window represented by one issue."""
    end = (now or utc_now()).astimezone(BEIJING_TZ)
    count = max(1, int(days))
    start = end.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=count - 1)
    return start, end


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
        # Historical Chinese sources remain in the audit database but are never
        # collected or exposed by the English-only product.
        disabled = conn.execute(
            "UPDATE journal_sources SET is_enabled = 0, updated_at = ? "
            "WHERE lower(language) LIKE 'zh%' AND is_enabled != 0",
            (now,),
        )
        changed += int(disabled.rowcount or 0)
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

            # The curated registry is authoritative.  Re-enable titles that an
            # older deployment may have disabled and refresh their identifiers.
            cur = conn.execute(
                """
                UPDATE journal_sources
                SET language = 'en', issn = ?, source_type = ?, source_url = ?,
                    config_json = ?, is_enabled = 1, updated_at = ?
                WHERE name = ? AND (
                    language != 'en' OR issn != ? OR source_type != ? OR
                    source_url != ? OR config_json != ? OR is_enabled != 1
                )
                """,
                (
                    source.get("issn", ""), source.get("source_type", "openalex"),
                    source.get("source_url", ""), _json_dumps(source.get("config", {})), now,
                    source["name"], source.get("issn", ""), source.get("source_type", "openalex"),
                    source.get("source_url", ""), _json_dumps(source.get("config", {})),
                ),
            )
            changed += int(cur.rowcount or 0)

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
    en = [
        source for source in sources
        if int(source.get("is_enabled") or 0)
        and str(source.get("language") or "").lower().startswith("en")
    ]
    by_name = {str(source.get("name") or ""): source for source in en}
    grouped_names: set[str] = set()
    groups: list[dict[str, Any]] = []
    for title, subtitle, names in _JOURNAL_CATALOG_GROUPS:
        items = [by_name[name] for name in names if name in by_name]
        grouped_names.update(str(item.get("name") or "") for item in items)
        if items:
            groups.append({"title": title, "subtitle": subtitle, "sources": items, "count": len(items)})
    other = [source for source in en if str(source.get("name") or "") not in grouped_names]
    if other:
        groups.append({
            "title": "其他英文期刊", "subtitle": "OTHER ENGLISH JOURNALS",
            "sources": other, "count": len(other),
        })
    return {
        "zh": [],
        "en": en,
        "en_groups": groups,
        "total": len(en),
        "auto_count": sum(1 for source in en if source.get("completeness", {}).get("complete")),
        "legacy_hidden": sum(1 for source in sources if source not in en),
    }


_LEGACY_JOURNAL_TABLES = (
    "journal_sources",
    "journal_subscriptions",
    "journal_digests",
    "journal_articles",
    "journal_delivery_logs",
    "journal_runs",
    "journal_digest_deliveries",
)


def _migrate_legacy_journal_tables() -> bool:
    """Copy journal-owned rows out of the legacy membership database once.

    The migration preserves primary keys and copies only columns common to the
    old and new schemas.  It never deletes the legacy tables, so rollback and
    audit remain possible.
    """
    target = Path(DB_PATH).resolve()
    # Test suites and maintenance tools may temporarily point DB_PATH at an
    # isolated database.  Legacy production data must never leak into those
    # databases; the one-time copy is only valid for the configured journal DB.
    if target != Path(JOURNAL_DB_PATH).resolve():
        return False
    legacy = Path(LEGACY_DB_PATH).resolve()
    if target == legacy or not legacy.exists():
        return False
    with _connect() as conn:
        marker = conn.execute(
            "SELECT value FROM journal_meta WHERE key = 'legacy_membership_copy_v1'"
        ).fetchone()
        if marker:
            return False
        conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
        try:
            for table in _LEGACY_JOURNAL_TABLES:
                exists = conn.execute(
                    "SELECT 1 FROM legacy.sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                target_cols = {
                    str(row[1]) for row in conn.execute(f"PRAGMA main.table_info({table})").fetchall()
                }
                legacy_cols = [
                    str(row[1]) for row in conn.execute(f"PRAGMA legacy.table_info({table})").fetchall()
                ]
                columns = [column for column in legacy_cols if column in target_cols]
                if not columns:
                    continue
                quoted = ", ".join(f'"{column}"' for column in columns)
                conn.execute(
                    f"INSERT OR IGNORE INTO main.{table}({quoted}) "
                    f"SELECT {quoted} FROM legacy.{table}"
                )
            conn.execute(
                "INSERT INTO journal_meta(key, value, updated_at) VALUES(?, ?, ?)",
                ("legacy_membership_copy_v1", str(legacy), utc_now_text()),
            )
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE legacy")
    return True


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
                UNIQUE(user_id, email)
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
                pdf_url TEXT NOT NULL DEFAULT '',
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

            CREATE TABLE IF NOT EXISTS journal_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS journal_model_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                feature TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'mimo',
                model TEXT NOT NULL DEFAULT 'mimo-v2.5',
                reasoning_effort TEXT NOT NULL DEFAULT 'off',
                success INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS journal_takedowns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(article_id)
            );

            CREATE TABLE IF NOT EXISTS journal_fulltext (
                article_id INTEGER PRIMARY KEY,
                batch_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                pdf_url TEXT NOT NULL DEFAULT '',
                pdf_host_type TEXT NOT NULL DEFAULT '',
                pdf_bytes INTEGER NOT NULL DEFAULT 0,
                pdf_sha256 TEXT NOT NULL DEFAULT '',
                page_count INTEGER NOT NULL DEFAULT 0,
                para_count INTEGER NOT NULL DEFAULT 0,
                translated INTEGER NOT NULL DEFAULT 0,
                required_translations INTEGER NOT NULL DEFAULT 0,
                src_lang TEXT NOT NULL DEFAULT 'en',
                ocr_pages INTEGER NOT NULL DEFAULT 0,
                provider TEXT NOT NULL DEFAULT 'mimo',
                model TEXT NOT NULL DEFAULT 'mimo-v2.5',
                reasoning_effort TEXT NOT NULL DEFAULT 'off',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_journal_fulltext_status
                ON journal_fulltext(status);
            CREATE INDEX IF NOT EXISTS idx_journal_fulltext_batch
                ON journal_fulltext(batch_id);
            """
        )
        # 幂等补列（线上旧库通过启动迁移补齐，绝不重建/丢数据）。
        _ensure_column(conn, "journal_articles", "batch_id", "INTEGER")
        _ensure_column(conn, "journal_articles", "ai_discipline", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "journal_articles", "ai_problem_type", "TEXT NOT NULL DEFAULT ''")
        # 文献 PDF 下载链接（OpenAlex 开放获取 / NCPSSD 全文，尽力而为；为空则前端不显示下载按钮）。
        _ensure_column(conn, "journal_articles", "pdf_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "journal_articles", "journal_name_zh", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "journal_articles", "authors_zh_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "journal_articles", "citation_gb2015_zh", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "journal_articles", "citation_mks_en", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "journal_articles", "citation_mks_zh", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "journal_articles", "public_pdf_verified", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "journal_digests", "issue_key", "TEXT NOT NULL DEFAULT ''")
        # 2026-08：英文论文已成为统一入口，不再按作者/语种另设“国外马克思主义研究”。
        # 历史数据迁入最接近的主题桶，避免目录里残留已下线分类。
        conn.execute(
            "UPDATE journal_articles SET ai_discipline = '马克思主义思想史与文本研究' "
            "WHERE ai_discipline = '国外马克思主义研究'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_journal_articles_batch ON journal_articles(batch_id, status)"
        )
        # 投递表迁移：支持非订阅收件人（付费会员/注册用户/特定邮箱），去重改为按邮箱。
        _migrate_digest_deliveries(conn)
        conn.commit()
    _migrate_legacy_journal_tables()
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
        user_schema = _membership_schema(conn)
        rows = conn.execute(
            f"""
            SELECT s.*, u.email AS user_account_email, u.display_name, u.role, u.is_active
            FROM journal_subscriptions s
            JOIN {user_schema}.users u ON u.id = s.user_id
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


def set_article_takedown(article_id: int, reason: str = "") -> dict:
    """Immediately hide a public article without deleting its audit artifacts."""
    reason = (reason or "管理员紧急下架").strip()[:1000]
    now = utc_now_text()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM journal_articles WHERE id = ?", (int(article_id),)
        ).fetchone()
        if row is None:
            raise ValueError("文章不存在。")
        conn.execute(
            """
            INSERT INTO journal_takedowns(article_id, reason, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET reason=excluded.reason, created_at=excluded.created_at
            """,
            (int(article_id), reason, now),
        )
        conn.commit()
    return _article_row(row) or {}


def clear_article_takedown(article_id: int) -> dict:
    """Restore visibility; the normal complete/full-text/public-issue gates still apply."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM journal_articles WHERE id = ?", (int(article_id),)
        ).fetchone()
        if row is None:
            raise ValueError("文章不存在。")
        conn.execute("DELETE FROM journal_takedowns WHERE article_id = ?", (int(article_id),))
        conn.commit()
    return _article_row(row) or {}


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
        row = conn.execute(
            "SELECT language FROM journal_sources WHERE id = ?", (int(source_id),)
        ).fetchone()
        if row is None:
            raise ValueError("期刊来源不存在。")
        if str(row["language"] or "").strip().lower() != "en" and is_enabled:
            raise ValueError("中文及其他非英文期刊只能保留为历史审计记录，不能重新启用。")
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
    language: str = "en",
    source_type: str = "manual",
    issn: str = "",
    source_url: str = "",
    config: dict | None = None,
) -> dict:
    """新增一个期刊来源（控制台「期刊来源」区使用）。name 唯一，重复则报错。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("期刊名称不能为空。")
    language = (language or "en").strip().lower()
    if language != "en":
        raise ValueError("期刊订阅仅允许新增英文期刊来源。")
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
    data["authors_zh"] = _json_loads(str(data.get("authors_zh_json") or "[]"), [])
    data["metadata"] = _json_loads(str(data.get("metadata_json") or "{}"), {})
    return data


# ----------------------------------------------------------------------------
# 批次（digest batch）生命周期
# ----------------------------------------------------------------------------

def _digest_row(row: sqlite3.Row | None) -> dict | None:
    return _row_to_dict(row)


def current_batch() -> dict | None:
    """Most recent issue that is still collecting, under review, or awaiting email."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM journal_digests
            WHERE status IN ('collecting', 'reviewing', 'ready_to_send', 'published')
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
    """Latest member-visible issue, including an explicitly curated sample issue."""
    batches = list_public_batches(limit=1)
    return batches[0] if batches else None


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


def list_public_batches(limit: int = 60) -> list[dict]:
    """对会员可翻阅的期数：样刊 + 自动形成的待发送期 + 已发送/归档期。

    collecting/reviewing 尚未产出完整文章，不在此列；ready_to_send 已通过
    公开 PDF、正文完整性与逐段双语门槛，可先在网站供会员阅读，但邮件仍须
    管理员最终确认。sample 是人工明确标记的独立样刊，不会被采集器
    当作当周在建批次，也不会通过邮件批准门槛。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.* FROM journal_digests d
            WHERE d.status IN ('sample', 'ready_to_send', 'published', 'sent', 'archived')
              AND EXISTS (
                  SELECT 1 FROM journal_articles a
                  JOIN journal_fulltext f ON f.article_id = a.id AND f.status = 'ready'
                    AND f.required_translations > 0 AND f.translated = f.required_translations
                  LEFT JOIN journal_takedowns t ON t.article_id = a.id
                  WHERE a.batch_id = d.id AND a.language = 'en' AND t.article_id IS NULL
              )
            ORDER BY d.id DESC LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    candidates = [_digest_row(row) or {} for row in rows]
    return [batch for batch in candidates if public_batch_articles(int(batch["id"]))]


def open_batch(settings: dict | None = None, *, period_days: int | None = None) -> dict:
    """开新批次：归档之前未发送的批次及其文章，再插入一行 collecting 批次。"""
    settings = settings or load_alert_settings()
    days = int(period_days if period_days is not None else (settings.get("lookback_days") or DEFAULT_LOOKBACK_DAYS))
    period_start_dt, period_end_dt = weekly_collection_window(days=days)
    period_start = period_start_dt.isoformat(timespec="seconds")
    period_end = period_end_dt.isoformat(timespec="seconds")
    iso_year, iso_week, _ = period_end_dt.isocalendar()
    issue_key = f"{iso_year}-W{iso_week:02d}"
    hard_delete = bool(settings.get("hard_delete_archived"))
    archive_previous_batches(hard_delete=hard_delete)
    now_text = utc_now_text()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO journal_digests(
                period_start, period_end, issue_key, frequency, status,
                auto_approve_articles, auto_generate_review, auto_send,
                created_at, updated_at
            )
            VALUES(?, ?, ?, ?, 'collecting', ?, ?, ?, ?, ?)
            """,
            (
                period_start,
                period_end,
                issue_key,
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


def public_batch_articles(digest_id: int) -> list[dict]:
    """Only complete English full-text articles belonging to a published issue."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT a.* FROM journal_articles a
            JOIN journal_fulltext f ON f.article_id = a.id AND f.status = 'ready'
              AND f.required_translations > 0 AND f.translated = f.required_translations
            LEFT JOIN journal_takedowns t ON t.article_id = a.id
            WHERE a.batch_id = ? AND a.language = 'en'
              AND a.status IN ('ready', 'archived')
              AND a.public_pdf_verified = 1
              AND t.article_id IS NULL
            ORDER BY a.ai_discipline ASC, a.first_seen_at ASC, a.id ASC
            """,
            (int(digest_id),),
        ).fetchall()
    return [article for row in rows if _public_article_complete(article := _article_row(row))]


def _public_article_complete(article: dict) -> bool:
    required = (
        "title", "title_zh", "journal_name", "journal_name_zh", "abstract", "abstract_zh",
        "citation_gb2015", "citation_mks_en",
    )
    if not all(str(article.get(key) or "").strip() for key in required):
        return False
    if not (article.get("authors") and article.get("authors_zh")):
        return False
    try:
        from journal_fulltext import load_document

        document = load_document(int(article["id"])) or {}
    except Exception:
        return False
    if int(document.get("schema_version") or 0) < 2:
        return False
    paragraphs = document.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        return False
    translatable = [
        block for block in paragraphs
        if str(block.get("kind") or "body") != "reference"
        and len(str(block.get("text") or "").strip()) >= 2
    ]
    return bool(translatable) and all(str(block.get("zh") or "").strip() for block in translatable)


# ----------------------------------------------------------------------------
# 单期发布上限：仅在本期严格 7 天窗口内取舍，绝不跨周顺延
# ----------------------------------------------------------------------------
# Compatibility constant retained for older imports.  The live registry is
# English-only, so release selection no longer reserves a Chinese quota.
_RELEASE_EN_SHARE = 1.0


def count_deferred_articles() -> int:
    """Compatibility metric for English rows still marked deferred in an issue."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM journal_articles "
            "WHERE status = 'deferred' AND language = 'en' AND batch_id IS NOT NULL"
        ).fetchone()
    return int(row["n"]) if row else 0


def _release_language_bucket(article: dict) -> str:
    return "en" if str(article.get("language") or "").lower().startswith("en") else "zh"


def _roundrobin_by_journal(articles: list[dict], quota: int) -> list[dict]:
    """按期刊分组轮转取前 quota 篇：先各家取第 1 篇、再各家取第 2 篇……把来源铺开，
    避免某一家期刊刷屏，并自然铺开学科分布。同期刊内按 first_seen_at/id 升序（最早入库优先，
    保证顺延的旧文最终会被释放）。刊名排序固定，选取结果可复现（便于测试）。"""
    if quota <= 0 or not articles:
        return []
    if quota >= len(articles):
        return list(articles)
    groups: dict[str, list[dict]] = {}
    for a in sorted(articles, key=lambda x: (str(x.get("first_seen_at") or ""), int(x.get("id") or 0))):
        groups.setdefault(str(a.get("journal_name") or ""), []).append(a)
    order = sorted(groups)
    out: list[dict] = []
    col = 0
    while len(out) < quota:
        progressed = False
        for journal in order:
            bucket = groups[journal]
            if col < len(bucket):
                out.append(bucket[col])
                progressed = True
                if len(out) >= quota:
                    break
        if not progressed:
            break
        col += 1
    return out[:quota]


def _select_release_articles(pool: list[dict], cap: int) -> tuple[list[dict], list[dict]]:
    """Select an English-only issue by journal round-robin; return overflow."""
    pool = [a for a in pool if _release_language_bucket(a) == "en"]
    if cap <= 0 or len(pool) <= cap:
        return list(pool), []
    selected = _roundrobin_by_journal(pool, cap)
    selected_ids = {int(a["id"]) for a in selected}
    deferred = [a for a in pool if int(a["id"]) not in selected_ids]
    return selected, deferred


def apply_release_cap(batch_id: int, settings: dict | None = None) -> dict:
    """Apply the issue cap only to English articles in this exact weekly window.

    Overflow remains attached to the issue as ``ignored`` audit data and is
    never recycled into a later issue; that preserves the strict seven-day
    editorial boundary.  The legacy return keys remain for callers/tests.
    """
    settings = settings or load_alert_settings()
    cap = int(settings.get("weekly_release_cap") or 0)
    auto_approve = bool(settings.get("auto_approve_articles") or settings.get("auto_publish_all"))
    now = utc_now_text()
    with _connect() as conn:
        active_rows = conn.execute(
            "SELECT * FROM journal_articles WHERE batch_id = ? "
            "AND status IN ('ready', 'pending_review', 'translation_pending') "
            "AND language = 'en' ORDER BY first_seen_at ASC, id ASC",
            (int(batch_id),),
        ).fetchall()
    pool = [_article_row(r) for r in active_rows]
    selected, deferred = _select_release_articles(pool, cap)
    selected_ids = {int(a["id"]) for a in selected}
    deferred_ids = {int(a["id"]) for a in deferred}
    with _connect() as conn:
        for a in selected:
            cur_status = str(a.get("status") or "")
            # 本期已在办状态保持不变；仅兼容历史未知状态。
            new_status = cur_status if cur_status in ("ready", "pending_review", "translation_pending") else (
                "ready" if auto_approve else "pending_review"
            )
            conn.execute(
                "UPDATE journal_articles SET status = ?, batch_id = ?, updated_at = ? WHERE id = ?",
                (new_status, int(batch_id), now, int(a["id"])),
            )
        for aid in deferred_ids:
            conn.execute(
                "UPDATE journal_articles SET status = 'ignored', batch_id = ?, updated_at = ? WHERE id = ?",
                (int(batch_id), now, int(aid)),
            )
        conn.commit()
    return {
        "selected": len(selected_ids),
        "deferred": len(deferred_ids),
        "backlog_remaining": 0,
        "cap": cap,
    }


# 可对外（含 PDF 下载）开放的文章状态：已发布/审核预览/已归档（旧邮件链接仍可用），
# 排除草稿态（待翻译）与人工忽略，避免越权枚举未发布稿件。
_PUBLIC_ARTICLE_STATUSES = ("ready", "archived")


def get_public_article(article_id: int) -> dict | None:
    """Return a complete English article from a member-visible issue or sample."""
    placeholders = ",".join("?" for _ in _PUBLIC_ARTICLE_STATUSES)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT a.* FROM journal_articles a
            JOIN journal_digests d ON d.id = a.batch_id AND d.status IN ('sample', 'ready_to_send', 'published', 'sent', 'archived')
            JOIN journal_fulltext f ON f.article_id = a.id AND f.status = 'ready'
              AND f.required_translations > 0 AND f.translated = f.required_translations
            LEFT JOIN journal_takedowns t ON t.article_id = a.id
            WHERE a.id = ? AND a.language = 'en'
              AND a.status IN ({placeholders})
              AND a.public_pdf_verified = 1
              AND t.article_id IS NULL
            """,
            (int(article_id), *_PUBLIC_ARTICLE_STATUSES),
        ).fetchone()
    if not row:
        return None
    article = _article_row(row)
    return article if _public_article_complete(article) else None


def get_issue_neighbors(article_id: int, digest_id: int) -> tuple[dict | None, dict | None]:
    articles = public_batch_articles(digest_id)
    ids = [int(item["id"]) for item in articles]
    if int(article_id) not in ids:
        return None, None
    index = ids.index(int(article_id))
    previous = articles[index - 1] if index > 0 else None
    following = articles[index + 1] if index + 1 < len(articles) else None
    return previous, following


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
    auto_send: bool | None = None,
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
    if auto_send is not None:
        sets.append("auto_send = ?"); params.append(1 if auto_send else 0)
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
    return _urlopen_text_final(url, user_agent)[0]


def _urlopen_text_final(url: str, user_agent: str = USER_AGENT) -> tuple[str, str]:
    """返回 (正文, 最终 URL)。最终 URL 用于识别「被 302 重定向到别处」的软失败
    （如 NCPSSD 对境外 IP 把期刊详情页重定向回首页——正文是首页 HTML，解析必然空手）。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="replace"), str(resp.geturl() or url)


# ====== 国内采集中继（journal relay） ======
# 背景：NCPSSD 等国内学术站自 2026-06 起对境外/机房 IP 实施访问拦截（详情页 302 回首页、
# 连接重置），生产服务器（境外）无法直接抓取中文刊。解法＝站长的国内机器定时跑
# scripts/journal_relay_push.py 抓取全部中文网页源，把「fetch_source_articles 同构的文章
# 字典」打包成 JSON 推到服务器本文件路径；服务器采集时对 web_html 源**中继优先**：
# 中继里有该源且足够新鲜 → 直接采用（零外网请求）；否则回退直抓（并对 NCPSSD 重定向
# 显式报错，不再静默空手）。批次/去重/时间窗/翻译/审核等管线完全不变。
RELAY_PATH = JOURNAL_TMP_DIR / "legacy-journal-relay.json"
RELAY_MAX_AGE_DAYS = max(1, int(os.environ.get("MARX_JOURNAL_RELAY_MAX_AGE_DAYS", "10") or "10"))
# 中继超过该天数未更新时，采集轮在 run 错误里附一条提醒（不影响成功状态判定的 warning 级）。
RELAY_STALE_WARN_DAYS = 3

_RELAY_CACHE: dict[str, Any] = {"mtime": None, "payload": None}


def _load_relay_payload() -> dict:
    """读中继 JSON（按 mtime 缓存）。缺文件/坏 JSON 一律返回空 dict——回退直抓路径。"""
    try:
        mtime = RELAY_PATH.stat().st_mtime
    except OSError:
        return {}
    if _RELAY_CACHE["mtime"] == mtime and isinstance(_RELAY_CACHE["payload"], dict):
        return _RELAY_CACHE["payload"]
    try:
        payload = json.loads(RELAY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    _RELAY_CACHE["mtime"] = mtime
    _RELAY_CACHE["payload"] = payload
    return payload


def _relay_entry_age_days(entry: dict) -> float | None:
    fetched = _parse_utc(str(entry.get("fetched_at") or ""))
    if fetched is None:
        return None
    return max(0.0, (utc_now() - fetched).total_seconds() / 86400.0)


def relay_generated_age_days() -> float | None:
    """整包中继的年龄（天）；无中继返回 None。供采集轮附「中继过旧」提醒。"""
    payload = _load_relay_payload()
    generated = _parse_utc(str(payload.get("generated_at") or ""))
    if generated is None:
        return None
    return max(0.0, (utc_now() - generated).total_seconds() / 86400.0)


def relay_status() -> dict:
    """控制台「中文源中继」状态卡数据。

    中英文采集/发送本就在同一批次里统一进行（collect_batch 一次跑全部来源：中文网页源
    经国内中继、英文源直采 OpenAlex/Crossref；发送按批次群发不分语种）。这里只汇报中继
    侧健康度：文件年龄、覆盖多少源多少篇、哪些启用中的中文源缺中继（采集时会回退直抓，
    在境外服务器上必失败）——让管理员在控制台一眼看清，不用登服务器查文件。"""
    payload = _load_relay_payload()
    raw_sources = payload.get("sources")
    sources_map: dict = raw_sources if isinstance(raw_sources, dict) else {}
    age = relay_generated_age_days()
    source_count = 0
    article_count = 0
    for entry in sources_map.values():
        if not isinstance(entry, dict):
            continue
        n = len(entry.get("articles") or [])
        if n:
            source_count += 1
            article_count += n
    # 启用中的中文网页源里，在中继中缺失/为空/过期的（这些源采集时将回退直抓并报错）
    uncovered: list[str] = []
    try:
        for s in list_journal_sources(limit=200):
            if (
                s.get("language") != "zh"
                or str(s.get("source_type") or "") != "web_html"
                or not int(s.get("is_enabled") or 0)
            ):
                continue
            name = str(s.get("name") or "")
            entry = sources_map.get(name)
            entry_age = _relay_entry_age_days(entry) if isinstance(entry, dict) else None
            n = len((entry or {}).get("articles") or []) if isinstance(entry, dict) else 0
            if not n or entry_age is None or entry_age > RELAY_MAX_AGE_DAYS:
                uncovered.append(name)
    except Exception:  # noqa: BLE001 — 状态卡绝不因查询问题弄崩控制台
        pass
    return {
        "present": bool(sources_map),
        "generated_at": str(payload.get("generated_at") or ""),
        "age_days": age,
        "stale": bool(age is not None and age > RELAY_STALE_WARN_DAYS),
        "expired": bool(age is None or age > RELAY_MAX_AGE_DAYS),
        "source_count": source_count,
        "article_count": article_count,
        "uncovered": uncovered,
        "max_age_days": RELAY_MAX_AGE_DAYS,
        "stale_warn_days": RELAY_STALE_WARN_DAYS,
    }


def _fetch_from_relay(source: dict) -> list[dict] | None:
    """中继里有该源的新鲜数据则返回文章列表；否则 None（走直抓）。

    文章字典与 fetch_source_articles 直抓产物同构；journal_name/language/requires_review
    以**服务器侧**来源配置为准重算（管理员在控制台改过可信/自动发送开关时不被本地配置盖掉）。
    """
    payload = _load_relay_payload()
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return None
    entry = sources.get(str(source.get("name") or ""))
    if not isinstance(entry, dict):
        return None
    age = _relay_entry_age_days(entry)
    if age is None or age > RELAY_MAX_AGE_DAYS:
        return None
    raw_articles = entry.get("articles")
    if not isinstance(raw_articles, list):
        return None
    requires_review = (
        str(source.get("source_type") or "").lower() == "web_html"
        and not bool(_source_config(source).get("auto_publish"))
    )
    articles: list[dict] = []
    for item in raw_articles:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        art = dict(item)
        art["journal_name"] = source.get("name") or art.get("journal_name") or ""
        art["language"] = source.get("language") or art.get("language") or "zh"
        art["requires_review"] = requires_review
        meta = art.get("metadata")
        art["metadata"] = dict(meta) if isinstance(meta, dict) else {}
        art["metadata"]["relayed"] = True
        articles.append(art)
    return articles


def fetch_source_articles(source: dict, lookback_days: int | None = None) -> list[dict]:
    days = int(lookback_days) if lookback_days else DEFAULT_LOOKBACK_DAYS
    source_type = str(source.get("source_type") or "manual").strip().lower()
    language = str(source.get("language") or "").strip().lower()
    # The English weekly uses independent indexes in parallel.  OpenAlex remains
    # the primary discovery index, while Crossref catches publisher deposits that
    # OpenAlex has not indexed yet and DOAJ contributes OA-journal records/links.
    # One provider failing must not erase successful results from the others.
    if language.startswith("en") and source_type in {"openalex", "crossref", "doaj"}:
        warning_key = str(source.get("id") or source.get("name") or "")
        _DISCOVERY_WARNINGS.pop(warning_key, None)
        config = _source_config(source)
        configured = config.get("discovery_providers")
        providers = (
            [str(value).strip().lower() for value in configured if str(value).strip()]
            if isinstance(configured, list)
            else ["openalex", "crossref", "doaj"]
        )
        fetchers = {
            "openalex": _fetch_openalex,
            "crossref": _fetch_crossref,
            "doaj": _fetch_doaj,
        }
        batches: list[tuple[str, list[dict]]] = []
        errors: list[str] = []
        for provider in providers:
            fetcher = fetchers.get(provider)
            if fetcher is None:
                errors.append(f"{provider}: unsupported discovery provider")
                continue
            try:
                batches.append((provider, fetcher(source, days)))
            except Exception as exc:
                errors.append(f"{provider}: {type(exc).__name__}: {exc}")
        if batches:
            articles = _merge_discovery_articles(batches)
            if errors:
                _DISCOVERY_WARNINGS[warning_key] = "; ".join(errors)[:1200]
                for article in articles:
                    article.setdefault("metadata", {})["discovery_warnings"] = errors[:6]
                LOGGER.warning("partial discovery failure for %s: %s", source.get("name"), "; ".join(errors))
            return articles
        if errors:
            _DISCOVERY_WARNINGS[warning_key] = "; ".join(errors)[:1200]
            raise RuntimeError("; ".join(errors)[:1200])
        return []
    if source_type == "openalex":
        return _fetch_openalex(source, days)
    if source_type == "crossref":
        return _fetch_crossref(source, days)
    if source_type == "rss":
        return _fetch_rss(source)
    if source_type == "web_html":
        relayed = _fetch_from_relay(source)
        if relayed is not None:
            return relayed
        return _fetch_web_html(source)
    return []


def _normalized_doi(value: Any) -> str:
    doi = str(value or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    return urllib.parse.unquote(doi).strip().lower()


def _discovery_identity(article: dict) -> str:
    doi = _normalized_doi(article.get("doi"))
    if doi:
        return "doi:" + doi
    title = re.sub(r"\W+", " ", _strip_tags(str(article.get("title") or "")).lower()).strip()
    journal = re.sub(r"\W+", " ", str(article.get("journal_name") or "").lower()).strip()
    date = str(article.get("published_at") or "").strip()
    return f"title:{journal}|{title}|{date}"


def _merge_discovery_articles(batches: list[tuple[str, list[dict]]]) -> list[dict]:
    """Merge independent index records without losing alternate OA/PDF locations."""
    merged: dict[str, dict] = {}
    ordered: list[str] = []
    scalar_fields = ("journal_name", "language", "title", "abstract", "doi", "url", "pdf_url",
                     "published_at", "volume", "issue", "pages")
    for provider, articles in batches:
        for raw in articles or []:
            if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
                continue
            article = dict(raw)
            article["doi"] = _normalized_doi(article.get("doi"))
            key = _discovery_identity(article)
            if key not in merged:
                meta = dict(article.get("metadata") or {})
                meta["discovery_sources"] = [provider]
                article["metadata"] = meta
                merged[key] = article
                ordered.append(key)
                continue
            current = merged[key]
            meta = dict(current.get("metadata") or {})
            sources = list(meta.get("discovery_sources") or [])
            if provider not in sources:
                sources.append(provider)
            meta["discovery_sources"] = sources
            incoming_meta = dict(article.get("metadata") or {})
            for name, value in incoming_meta.items():
                if name not in meta or not meta.get(name):
                    meta[name] = value
                elif isinstance(meta.get(name), list) and isinstance(value, list):
                    combined = list(meta[name])
                    for item in value:
                        if item not in combined:
                            combined.append(item)
                    meta[name] = combined
            alternate_urls = list(meta.get("discovery_urls") or [])
            for name in ("pdf_url", "url"):
                value = str(article.get(name) or "").strip()
                if value and value not in alternate_urls:
                    alternate_urls.append(value)
            if alternate_urls:
                meta["discovery_urls"] = alternate_urls
            for field in scalar_fields:
                incoming = article.get(field)
                if not current.get(field) and incoming:
                    current[field] = incoming
            if len(str(article.get("abstract") or "")) > len(str(current.get("abstract") or "")):
                current["abstract"] = article.get("abstract") or ""
            if len(article.get("authors") or []) > len(current.get("authors") or []):
                current["authors"] = article.get("authors") or []
            current["metadata"] = meta
    return [merged[key] for key in ordered]


def _merge_metadata_dicts(existing: dict, incoming: dict) -> dict:
    """Merge new provenance/candidate lists into an existing database record."""
    merged = dict(existing or {})
    for name, value in (incoming or {}).items():
        if name not in merged or not merged.get(name):
            merged[name] = value
        elif isinstance(merged.get(name), list) and isinstance(value, list):
            combined = list(merged[name])
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[name] = combined
        elif isinstance(merged.get(name), dict) and isinstance(value, dict):
            merged[name] = _merge_metadata_dicts(merged[name], value)
    return merged


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
            text, final_url = _urlopen_text_final(url, user_agent=BROWSER_USER_AGENT)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        if parser == "ncpssd_journal" and "journal/details" in url and "journal/details" not in final_url:
            # NCPSSD 对境外/机房 IP 把详情页 302 重定向回首页：正文是首页 HTML，解析必然
            # 0 篇。旧行为静默空手（控制台看着一切正常），现在显式报错让 last_error 说人话。
            errors.append(
                f"{url}: NCPSSD 把详情页重定向到 {final_url}（境外 IP 访问拦截）——"
                "需依赖国内中继采集（journal_relay），请检查站长本机的中继推送任务是否在跑"
            )
            continue
        if parser == "ncpssd_journal":
            articles = _parse_ncpssd_journal_html(text, source, url)
        elif parser == "magtech_journal":
            articles = _parse_magtech_journal_html(text, source, url)
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


def _source_issns(source: dict) -> list[str]:
    values: list[str] = []
    primary = str(source.get("issn") or "").strip()
    if primary:
        values.append(primary)
    alternate = _source_config(source).get("alternate_issn")
    if isinstance(alternate, list):
        for value in alternate:
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
    return values


def _fetch_openalex(source: dict, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[dict]:
    issns = _source_issns(source)
    if not issns:
        return []
    days = max(1, int(lookback_days or DEFAULT_LOOKBACK_DAYS))
    from_date = (utc_now().astimezone(BEIJING_TZ).date() - timedelta(days=days - 1)).isoformat()
    api_key = str(os.environ.get("OPENALEX_API_KEY") or "").strip()
    items: list[dict] = []
    seen_ids: set[str] = set()
    request_errors: list[Exception] = []
    successful_requests = 0
    for issn in issns:
        values = {
            "filter": f"locations.source.issn:{issn},from_publication_date:{from_date}",
            "sort": "publication_date:desc",
            "per-page": "50",
            "mailto": str(os.environ.get("MARX_JOURNAL_CONTACT_EMAIL") or "journal-alerts@makesizhuyi.com"),
        }
        if api_key:
            values["api_key"] = api_key
        try:
            data = _urlopen_json(f"https://api.openalex.org/works?{urllib.parse.urlencode(values)}")
            successful_requests += 1
        except Exception as exc:
            request_errors.append(exc)
            continue
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("id") or item.get("doi") or item.get("title") or "")
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            items.append(item)
    if not successful_requests and request_errors:
        raise request_errors[0]
    articles = []
    crossref_budget = 15  # 仅对缺摘要且有 DOI 的文章用 Crossref 兜底，限量以控制请求数。
    for item in items:
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
        doi = _normalized_doi(item.get("doi"))
        # 开放获取 PDF：best_oa_location 优先，其次 open_access.oa_url / primary_location.pdf_url。
        best_oa = item.get("best_oa_location") or {}
        pdf_url = str(
            best_oa.get("pdf_url")
            or (item.get("open_access") or {}).get("oa_url")
            or location.get("pdf_url")
            or ""
        ).strip()
        abstract = _openalex_abstract(item.get("abstract_inverted_index"))
        if not abstract and doi and crossref_budget > 0:
            crossref_budget -= 1
            abstract = _crossref_abstract(doi)
        locations: list[dict[str, str]] = []
        for oa_location in item.get("locations") or []:
            if not isinstance(oa_location, dict):
                continue
            oa_source = oa_location.get("source") or {}
            compact = {
                "pdf_url": str(oa_location.get("pdf_url") or "").strip(),
                "landing_page_url": str(oa_location.get("landing_page_url") or "").strip(),
                "host_type": str(oa_source.get("type") or "").strip(),
                "license": str(oa_location.get("license") or "").strip(),
                "version": str(oa_location.get("version") or "").strip(),
                "is_oa": bool(oa_location.get("is_oa")),
            }
            if compact["pdf_url"] or compact["landing_page_url"]:
                locations.append(compact)
        articles.append(
            {
                "journal_name": str(source_info.get("display_name") or source.get("name") or "").strip(),
                "language": source.get("language") or "en",
                "title": title,
                "abstract": abstract,
                "authors": [a for a in authors if a],
                "doi": doi,
                "url": str(location.get("landing_page_url") or item.get("id") or "").strip(),
                "pdf_url": pdf_url,
                "published_at": str(item.get("publication_date") or "").strip(),
                "volume": str((item.get("biblio") or {}).get("volume") or "").strip(),
                "issue": str((item.get("biblio") or {}).get("issue") or "").strip(),
                "pages": _openalex_pages(item.get("biblio") or {}),
                "metadata": {
                    "openalex_id": item.get("id"),
                    "work_type": item.get("type"),
                    "is_oa": bool((item.get("open_access") or {}).get("is_oa")),
                    "oa_status": str((item.get("open_access") or {}).get("oa_status") or ""),
                    "oa_license": str(best_oa.get("license") or ""),
                    "oa_version": str(best_oa.get("version") or ""),
                    "oa_landing_page_url": str(best_oa.get("landing_page_url") or ""),
                    "openalex_locations": locations,
                },
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
    issns = _source_issns(source)
    if not issns:
        return []
    days = max(1, int(lookback_days or DEFAULT_LOOKBACK_DAYS))
    from_date = (utc_now().astimezone(BEIJING_TZ).date() - timedelta(days=days - 1)).isoformat()
    items: list[dict] = []
    seen: set[str] = set()
    request_errors: list[Exception] = []
    successful_requests = 0
    for issn in issns:
        params = urllib.parse.urlencode(
            {
                "filter": f"from-pub-date:{from_date}",
                "sort": "published",
                "order": "desc",
                "rows": "50",
                "mailto": str(os.environ.get("MARX_JOURNAL_CONTACT_EMAIL") or "journal-alerts@makesizhuyi.com"),
            }
        )
        try:
            data = _urlopen_json(f"https://api.crossref.org/journals/{urllib.parse.quote(issn)}/works?{params}")
            successful_requests += 1
        except Exception as exc:
            request_errors.append(exc)
            continue
        for item in (data.get("message") or {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            identity = _normalized_doi(item.get("DOI")) or str(item.get("URL") or item.get("title") or "")
            if identity in seen:
                continue
            seen.add(identity)
            items.append(item)
    if not successful_requests and request_errors:
        raise request_errors[0]
    articles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # 只保留正式期刊论文；剔除 book/journal-issue/editorial 等非论文条目。
        if not _is_allowed_work_type(item.get("type"), _CROSSREF_ARTICLE_TYPES):
            continue
        title = _strip_tags(" ".join(item.get("title") or [])).strip()
        if not title or not _looks_like_article_title(title):
            continue
        authors = [_crossref_author_name(author) for author in item.get("author") or [] if isinstance(author, dict)]
        published = _crossref_date(item)
        links = [
            {
                "url": str(link.get("URL") or "").strip(),
                "content_type": str(link.get("content-type") or "").strip(),
                "content_version": str(link.get("content-version") or "").strip(),
                "intended_application": str(link.get("intended-application") or "").strip(),
            }
            for link in item.get("link") or []
            if isinstance(link, dict) and str(link.get("URL") or "").strip()
        ]
        pdf_url = next(
            (
                link["url"] for link in links
                if "pdf" in link["content_type"].lower() or ".pdf" in link["url"].lower()
            ),
            "",
        )
        articles.append(
            {
                "journal_name": " ".join(item.get("container-title") or []).strip() or source.get("name") or "",
                "language": source.get("language") or "en",
                "title": title,
                "abstract": _strip_tags(str(item.get("abstract") or "")),
                "authors": [a for a in authors if a],
                "doi": str(item.get("DOI") or "").strip(),
                "url": str(item.get("URL") or "").strip(),
                "pdf_url": pdf_url,
                "published_at": published,
                "volume": str(item.get("volume") or "").strip(),
                "issue": str(item.get("issue") or "").strip(),
                "pages": str(item.get("page") or "").strip(),
                "metadata": {
                    "crossref_type": item.get("type"),
                    "crossref_links": links,
                    "crossref_resource_url": str(((item.get("resource") or {}).get("primary") or {}).get("URL") or ""),
                },
            }
        )
    return articles


def _fetch_doaj(source: dict, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[dict]:
    """Discover DOAJ-indexed records and retain every advertised full-text link.

    DOAJ article metadata normally has only year/month, so those records enrich
    matching OpenAlex/Crossref DOI rows; a DOAJ-only row enters an issue only if
    the source happens to provide a complete publication date.
    """
    issns = _source_issns(source)
    if not issns:
        return []
    today = utc_now().astimezone(BEIJING_TZ).date()
    first_day = today - timedelta(days=max(1, int(lookback_days or DEFAULT_LOOKBACK_DAYS)) - 1)
    years = list(range(first_day.year, today.year + 1))
    records: list[dict] = []
    seen: set[str] = set()
    request_errors: list[Exception] = []
    successful_requests = 0
    for issn in issns:
        for year_filter in years:
            query = f"index.issn.exact:{issn} AND bibjson.year:{year_filter}"
            url = (
                "https://doaj.org/api/search/articles/"
                + urllib.parse.quote(query, safe="")
                + "?pageSize=50&sort=created_date%3Adesc"
            )
            try:
                data = _urlopen_json(url)
                successful_requests += 1
            except Exception as exc:
                request_errors.append(exc)
                continue
            for result in data.get("results") or []:
                records.append(result)
    if not successful_requests and request_errors:
        raise request_errors[0]
    raw_records = records
    records = []
    for result in raw_records:
        bib = (result or {}).get("bibjson") or {}
        if not isinstance(bib, dict):
            continue
        identifiers = bib.get("identifier") or []
        doi = next(
            (
                _normalized_doi(identifier.get("id"))
                for identifier in identifiers
                if isinstance(identifier, dict) and str(identifier.get("type") or "").lower() == "doi"
            ),
            "",
        )
        title = _strip_tags(str(bib.get("title") or "")).strip()
        identity = doi or title.lower()
        if not title or not identity or identity in seen:
            continue
        seen.add(identity)
        links = [
            {
                "url": str(link.get("url") or "").strip(),
                "content_type": str(link.get("content_type") or "").strip(),
                "type": str(link.get("type") or "").strip(),
            }
            for link in bib.get("link") or []
            if isinstance(link, dict) and str(link.get("url") or "").strip()
        ]
        pdf_url = next(
            (
                link["url"] for link in links
                if "pdf" in link["content_type"].lower() or ".pdf" in link["url"].lower()
            ),
            "",
        )
        landing_url = next((link["url"] for link in links if link["url"] != pdf_url), pdf_url)
        month = str(bib.get("month") or "").strip()
        year = str(bib.get("year") or "").strip()
        published = f"{year}-{int(month):02d}" if year.isdigit() and month.isdigit() else year
        journal = bib.get("journal") or {}
        start_page = str(bib.get("start_page") or "").strip()
        end_page = str(bib.get("end_page") or "").strip()
        records.append(
            {
                "journal_name": str(journal.get("title") or source.get("name") or "").strip(),
                "language": source.get("language") or "en",
                "title": title,
                "abstract": _strip_tags(str(bib.get("abstract") or "")),
                "authors": [
                    str(author.get("name") or "").strip()
                    for author in bib.get("author") or []
                    if isinstance(author, dict) and str(author.get("name") or "").strip()
                ],
                "doi": doi,
                "url": landing_url,
                "pdf_url": pdf_url,
                "published_at": published,
                "volume": str(journal.get("volume") or "").strip(),
                "issue": str(journal.get("number") or "").strip(),
                "pages": f"{start_page}-{end_page}" if start_page and end_page and start_page != end_page else start_page or end_page,
                "metadata": {"doaj_id": str(result.get("id") or ""), "doaj_links": links},
            }
        )
    return records


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


_MAGTECH_LINK_RE = re.compile(r'<a href="(?P<href>[^"]*?/CN/Y\d+/V\d+/I\d+/[^"]+)"[^>]*>(?P<inner>.*?)</a>', re.S)
_MAGTECH_VOLUMN_RE = re.compile(r"(\d{4}),\s*\d+\((\d+)\):\s*([0-9A-Za-z\-]+)")
_MAGTECH_DATE_RE = re.compile(r'name=["\']citation_publication_date["\'] content=["\'](\d{4})[/-](\d{1,2})[/-](\d{1,2})')
_MAGTECH_ABSTRACT_RE = re.compile(r'name=["\']dc\.description["\'] content="([^"]*)"')


def _parse_magtech_journal_html(text: str, source: dict, base_url: str) -> list[dict]:
    """解析玛格泰克(Magtech)期刊平台的当期目录页（如《政治经济学评论》crpe.ruc.edu.cn）。

    当期页内联给出 标题/作者/年卷期页；摘要与精确出版日期在各文章页
    （dc.description / citation_publication_date），按预算逐篇补全（尽力而为，失败不影响列表）。"""
    articles: list[dict] = []
    seen: set[str] = set()
    for match in _MAGTECH_LINK_RE.finditer(text or ""):
        url = urllib.parse.urljoin(base_url, html.unescape(match.group("href"))).replace(".edu.cn//CN", ".edu.cn/CN")
        if url in seen:
            continue
        title = re.sub(r"\s+", " ", _strip_tags(html.unescape(match.group("inner")))).strip()
        if not _looks_like_article_title(title):
            continue
        seen.add(url)
        window = text[match.end(): match.end() + 700]
        author_match = re.search(r"class=['\"]j-author['\"]>([^<]*)<", window)
        authors = _split_cn_authors(_strip_tags(html.unescape(author_match.group(1)))) if author_match else []
        published = issue = pages = ""
        vol_match = re.search(r"class=['\"]j-volumn['\"]>\s*([^<]*?)\s*<", window)
        if vol_match:
            vm = _MAGTECH_VOLUMN_RE.search(_strip_tags(html.unescape(vol_match.group(1))))
            if vm:
                published = f"{vm.group(1)}-01-01"  # 占位年份；下面文章页可补精确日期
                issue, pages = vm.group(2), vm.group(3)
        article = _web_article(source, title, url, published, "", base_url, {"source": "magtech_journal"})
        article["authors"] = authors
        article["issue"] = issue
        article["pages"] = pages
        # 逐篇补全摘要 + 精确出版日期（限量，文章页拉取失败则保留列表字段）。
        if len(articles) < 25:
            try:
                detail_html = _urlopen_text(url, user_agent=BROWSER_USER_AGENT)
            except Exception:
                detail_html = ""
            if detail_html:
                abs_match = _MAGTECH_ABSTRACT_RE.search(detail_html)
                if abs_match:
                    article["abstract"] = re.sub(r"\s+", " ", _strip_tags(html.unescape(abs_match.group(1)))).strip()
                date_match = _MAGTECH_DATE_RE.search(detail_html)
                if date_match:
                    article["published_at"] = f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
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
        "pdf_url": _ncpssd_pdf_url(data),
    }


def _ncpssd_pdf_url(data: dict) -> str:
    """从 NCPSSD 详情数据解析全文 PDF 链接（尽力而为）。

    NCPSSD 多数全文需登录 + 受瑞数 WAF 保护：`pdfurl`/`fileaddress` 仅对开放内容直出，
    此时返回可直接下载的真链；否则若 `pdfsize>0`（确有 PDF），回退到文章页地址，
    由下载路由尝试镜像、失败则跳转源站，至少给用户一条到全文的直达路径。空字符串=无。"""
    direct = str(data.get("pdfurl") or data.get("fileaddress") or "").strip()
    if direct:
        return urllib.parse.urljoin("https://www.ncpssd.cn/", direct) if direct.startswith("/") else direct
    try:
        pdf_size = int(str(data.get("pdfsize") or "0").strip() or "0")
    except (TypeError, ValueError):
        pdf_size = 0
    lngid = str(data.get("lngid") or data.get("id") or "").strip()
    if pdf_size > 0 and lngid:
        return (
            "https://www.ncpssd.cn/Literature/articleinfo?id="
            + urllib.parse.quote(lngid)
            + "&type=journalArticle"
        )
    return ""


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
    "book review", "abstracts in chinese", "abstracts in spanish", ": a tribute", "tribute to ",
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


def _citation_authors(authors: list[str], *, chinese: bool) -> str:
    cleaned = [str(name or "").strip() for name in authors if str(name or "").strip()]
    if not cleaned:
        return "佚名" if chinese else "Anonymous"
    text = ("、" if chinese else ", ").join(cleaned[:3])
    if len(cleaned) > 3:
        text += "，等" if chinese else ", et al."
    return text


def citation_variants(article: dict) -> dict[str, str]:
    """Generate the two original-English citation formats exposed to readers."""
    authors_en = list(article.get("authors") or [])
    title_en = str(article.get("title") or "").strip()
    journal_en = str(article.get("journal_name") or "").strip()
    year = str(article.get("published_at") or "")[:4] or "n.d."
    volume = str(article.get("volume") or "").strip()
    issue = str(article.get("issue") or "").strip()
    pages = str(article.get("pages") or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(article.get("doi") or "").strip(), flags=re.I)

    def gb() -> str:
        names = _citation_authors(authors_en, chinese=False)
        vol_issue = f", {volume}({issue})" if volume and issue else f", {volume}" if volume else f", ({issue})" if issue else ""
        page_text = f": {pages}" if pages else ""
        doi_text = f". DOI: {doi}" if doi else ""
        return f"{names}. {title_en}[J]. {journal_en}, {year}{vol_issue}{page_text}{doi_text}."

    def mks() -> str:
        names = _citation_authors(authors_en, chinese=False)
        details = []
        if volume:
            details.append(f"Vol. {volume}")
        if issue:
            details.append(f"No. {issue}")
        details.append(year)
        tail = ", ".join(details)
        return f'{names}, “{title_en},” {journal_en}, {tail}.'

    return {
        "citation_gb2015": gb(),
        "citation_mks_en": mks(),
    }


def update_article_bilingual_metadata(
    article_id: int,
    *,
    title_zh: str,
    journal_name_zh: str,
    authors_zh: list[str],
    abstract_zh: str,
    discipline: str,
    abstract_en: str | None = None,
    keywords_en: list[str] | None = None,
    keywords_zh: list[str] | None = None,
    abstract_source: str | None = None,
) -> dict | None:
    """Persist translated metadata and deterministic citations after MiMo QA."""
    from journal_taxonomy import is_valid_discipline

    if not is_valid_discipline(discipline):
        raise ValueError(f"invalid journal discipline: {discipline!r}")
    with _connect() as conn:
        row = conn.execute("SELECT * FROM journal_articles WHERE id = ?", (int(article_id),)).fetchone()
        if not row:
            return None
        article = _article_row(row)
        article.update(
            {
                "title_zh": str(title_zh or "").strip(),
                "journal_name_zh": str(journal_name_zh or "").strip(),
                "authors_zh": [str(name).strip() for name in authors_zh if str(name).strip()],
                "abstract_zh": str(abstract_zh or "").strip(),
            }
        )
        if abstract_en is not None:
            article["abstract"] = str(abstract_en or "").strip()
        metadata = dict(article.get("metadata") or {})
        if keywords_en is not None:
            metadata["keywords_en"] = [str(item).strip() for item in keywords_en if str(item).strip()]
        if keywords_zh is not None:
            metadata["keywords_zh"] = [str(item).strip() for item in keywords_zh if str(item).strip()]
        if abstract_source is not None:
            metadata["abstract_source"] = str(abstract_source or "original").strip() or "original"
        citations = citation_variants(article)
        conn.execute(
            """
            UPDATE journal_articles SET
                title_zh = ?, journal_name_zh = ?, authors_zh_json = ?, abstract = ?, abstract_zh = ?,
                ai_discipline = ?, citation_gb2015 = ?, citation_gb2015_zh = '',
                citation_mks_en = ?, citation_mks_zh = '', metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                article["title_zh"], article["journal_name_zh"], _json_dumps(article["authors_zh"]),
                article.get("abstract") or "", article["abstract_zh"], discipline, citations["citation_gb2015"],
                citations["citation_mks_en"], _json_dumps(metadata), utc_now_text(), int(article_id),
            ),
        )
        conn.commit()
        refreshed = conn.execute("SELECT * FROM journal_articles WHERE id = ?", (int(article_id),)).fetchone()
    return _article_row(refreshed) if refreshed else None


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
    config = getattr(ai_client, "config", None)
    if not ai_client or not (
        getattr(config, "mimo_enabled", False) or getattr(config, "enabled", False)
    ):
        return None

    def _translate(article: dict) -> dict:
        prompt = {
            "title": article.get("title") or "",
            "abstract": article.get("abstract") or "",
        }
        from ai import ai_call_context

        with ai_call_context(feature="journal_metadata_translate", charge_user=False):
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
                max_tokens=2400,
                temperature=0.1,
                provider="mimo",
                model="mimo-v2.5",
                disable_thinking=True,
                reasoning_effort="off",
                allow_reasoning_fallback=False,
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
                    authors_json, citation_gb2015, doi, url, pdf_url, published_at, volume, issue, pages,
                    dedupe_key, status, metadata_json, batch_id, first_seen_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    normalized.get("pdf_url") or "",
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
        # Independent indexes often enrich a DOI days after its first sighting.
        # Refresh metadata and alternate full-text candidates even when product
        # status/batch assignment is immutable (ignored/archived/deferred).
        existing_article = _article_row(existing)
        merged_metadata = _merge_metadata_dicts(
            dict(existing_article.get("metadata") or {}),
            dict(normalized.get("metadata") or {}),
        )
        incoming_title = _strip_tags(str(normalized.get("title") or "")).strip()
        stored_title = str(existing_article.get("title") or "").strip()
        refreshed_title = incoming_title if incoming_title and (not stored_title or "<" in stored_title) else stored_title
        stored_abstract = str(existing_article.get("abstract") or "").strip()
        incoming_abstract = str(normalized.get("abstract") or "").strip()
        refreshed_abstract = incoming_abstract if len(incoming_abstract) > len(stored_abstract) else stored_abstract
        stored_authors = list(existing_article.get("authors") or [])
        incoming_authors = list(normalized.get("authors") or [])
        refreshed_authors = incoming_authors if len(incoming_authors) > len(stored_authors) else stored_authors
        citation_input = {
            **normalized,
            "title": refreshed_title,
            "abstract": refreshed_abstract,
            "authors": refreshed_authors,
        }
        conn.execute(
            """
            UPDATE journal_articles
            SET title = ?, abstract = ?, authors_json = ?, citation_gb2015 = ?,
                doi = ?, url = ?, pdf_url = ?, published_at = ?, volume = ?, issue = ?, pages = ?,
                metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                refreshed_title,
                refreshed_abstract,
                _json_dumps(refreshed_authors),
                gb2015_citation(citation_input),
                existing_article.get("doi") or normalized.get("doi") or "",
                existing_article.get("url") or normalized.get("url") or "",
                existing_article.get("pdf_url") or normalized.get("pdf_url") or "",
                existing_article.get("published_at") or normalized.get("published_at") or "",
                existing_article.get("volume") or normalized.get("volume") or "",
                existing_article.get("issue") or normalized.get("issue") or "",
                existing_article.get("pages") or normalized.get("pages") or "",
                _json_dumps(merged_metadata),
                now,
                existing_id,
            ),
        )
        existing = conn.execute(
            "SELECT * FROM journal_articles WHERE id = ?", (existing_id,)
        ).fetchone()
        conn.commit()
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
        # Repeated feeds commonly return the same works for many weeks.  An
        # archived work must not float into a new issue again, and deferred
        # overflow is released only by apply_release_cap's FIFO backlog path.
        if existing_status in {"archived", "deferred", "ignored"}:
            return _article_row(existing), False
        # Rows already attached to the current issue may be refreshed while it
        # is collecting.  This does not cross an issue boundary.
        if batch_id is not None and existing_status != "ignored":
            restored = existing_status
            conn.execute(
                "UPDATE journal_articles SET batch_id = ?, status = ?, updated_at = ? WHERE id = ?",
                (int(batch_id), restored, now, existing_id),
            )
            row = conn.execute("SELECT * FROM journal_articles WHERE id = ?", (existing_id,)).fetchone()
            conn.commit()
            return _article_row(row), False
    return _article_row(existing), False


def _article_seen_before(source: dict, article: dict) -> bool:
    """该文章（按 dedupe_key）是否已在库中。供占位日期文章的「只收新文」窗口分支使用。"""
    normalized = {
        **article,
        "journal_name": article.get("journal_name") or source.get("name") or "",
        "language": article.get("language") or source.get("language") or "zh",
        "authors": article.get("authors") or [],
    }
    key = _dedupe_key(normalized)
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM journal_articles WHERE dedupe_key = ?", (key,)
        ).fetchone() is not None


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


def _publication_calendar_date(value: str):
    """Parse an exact YYYY-MM-DD publication date; partial/unknown dates are ineligible."""
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
    except ValueError:
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
    # 全文 PDF：仅在文章尚无 pdf_url 时回填，避免覆盖已解析到的真链。
    if detail.get("pdf_url") and not str(article.get("pdf_url") or "").strip():
        merged["pdf_url"] = detail["pdf_url"]
    metadata = dict(article.get("metadata") or {})
    if detail.get("keywords"):
        metadata["keywords"] = detail["keywords"]
    citation = gb2015_citation(merged)
    with _connect() as conn:
        conn.execute(
            """
            UPDATE journal_articles
            SET abstract = ?, authors_json = ?, citation_gb2015 = ?, doi = ?, pages = ?,
                issue = ?, published_at = ?, pdf_url = ?, metadata_json = ?, updated_at = ?
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
                merged.get("pdf_url") or "",
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
        user_schema = _membership_schema(conn)
        rows = conn.execute(
            f"""
            SELECT s.*, u.email AS user_account_email, u.display_name, u.role, u.is_active
            FROM journal_subscriptions s
            JOIN {user_schema}.users u ON u.id = s.user_id
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
    subject = f"请确认{JOURNAL_WEEKLY_TITLE}邮件订阅"
    body = (
        "您好：\n\n"
        f"请点击下面的链接确认{JOURNAL_WEEKLY_TITLE}邮件订阅：\n"
        f"{confirm_url}\n\n"
        "如果这不是您本人操作，可以忽略本邮件。"
    )
    send_email(smtp_config, subscription["email"], subject, body, _plain_to_html(body))


def _plain_to_html(text: str) -> str:
    return "<p>" + html.escape(text).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def _markdown_to_html(text: str) -> str:
    """轻量 Markdown→HTML：支持 #-###### 标题、有序/无序列表、> 引用、空行分段、**加粗**、*斜体*、
    `代码`、![图片](https://…)、[链接](https://…)（图片/链接仅接受 http(s) 绝对地址）。

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
        # 图片/链接先于加粗斜体替换，生成的标签属性里不会再被后续正则改写。
        # 仅接受 http(s) 绝对地址（html.escape 已把引号转义，属性注入不可行）。
        escaped = re.sub(
            r"!\[([^\]]*)\]\((https?://[^)\s]+)\)",
            r'<img src="\2" alt="\1" style="max-width:100%;height:auto;display:block;'
            r'margin:10px auto;border:1px solid #e7dccb;border-radius:10px">',
            escaped,
        )
        escaped = re.sub(
            r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
            r'<a href="\2" style="color:#8f1d1d;font-weight:600">\1</a>',
            escaped,
        )
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
                    f"{JOURNAL_WEEKLY_TITLE}仅供有效会员使用；会员已过期或账号已停用。",
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
        html_parts.append(f"<p><a href=\"{html.escape(unsubscribe_url)}\">退订{JOURNAL_WEEKLY_TITLE}邮件</a></p>")
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
    reusable_batch = current_batch() if reuse_open_batch else None
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
    # 时间窗：周刊只收录采集日及此前 6 个北京时间自然日；发布日期不精确则不进入本期。
    window_start, window_end = weekly_collection_window(days=lookback_days)
    window_start_date, window_end_date = window_start.date(), window_end.date()
    if reusable_batch:
        same_window = (
            str(reusable_batch.get("period_start") or "")[:10] == window_start_date.isoformat()
            and str(reusable_batch.get("period_end") or "")[:10] == window_end_date.isoformat()
        )
        if not same_window:
            # Never mix a missed/unsent issue with the next seven-day window.
            # open_batch() will archive the old draft while retaining its public
            # article data and create an independent weekly issue.
            reusable_batch = None
    if reusable_batch and str(reusable_batch.get("status") or "") == "published":
        raise RuntimeError("本期已整期批准并锁定；请先完成邮件发送，再采集下一期。")
    filtered_out = 0
    batch = reusable_batch
    if batch is None:
        batch = open_batch(settings, period_days=lookback_days)
    batch_id = int(batch["id"])
    try:
        sources = [
            source for source in list_journal_sources(limit=240)
            if int(source.get("is_enabled") or 0)
            and str(source.get("language") or "").lower().startswith("en")
        ]
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
                    # 英文周刊按准确自然日严格过滤；仅年份、仅月份、空日期和占位日期
                    # 都无法证明属于本周，因此不进入处理队列。
                    placeholder = _is_placeholder_pub_date(article.get("published_at"), ncpssd_id)
                    publication_date = _publication_calendar_date(article.get("published_at"))
                    if placeholder or publication_date is None or not (
                        window_start_date <= publication_date <= window_end_date
                    ):
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
                warning_key = str(source.get("id") or source.get("name") or "")
                _mark_source_checked(int(source["id"]), _DISCOVERY_WARNINGS.pop(warning_key, ""))
            except Exception as exc:
                sources_checked += 1
                errors.append(f"{source.get('name')}: {exc}")
                _DISCOVERY_WARNINGS.pop(str(source.get("id") or source.get("name") or ""), None)
                _mark_source_checked(int(source["id"]), str(exc))
        # Chinese relay/backfill remains available only for historical audit
        # tooling.  The live collector never calls it in the English-only mode.
        # 单期发布上限：把本批在办文章 + 历史顺延文章按上限挑一批纳入本期，其余顺延后续批次。
        # 放在综述生成之前，确保综述只面对可控体量（避免中继首次全量投递等一次性涌入压垮生成）。
        try:
            cap_result = apply_release_cap(batch_id, settings)
            if cap_result.get("deferred"):
                errors.append(
                    f"release-cap: 本期纳入 {cap_result['selected']} 篇，"
                    f"超出上限的 {cap_result['deferred']} 篇仅保留为本期审计记录，不跨周顺延"
                )
        except Exception as exc:
            errors.append(f"release-cap: {exc}")

        # The former long AI literature review is retired.  A separate worker
        # now resolves public PDFs and prepares complete bilingual issue cards;
        # the issue moves to manual approval only after that gate passes.
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
    # 本期实际进入处理/发布管线的文章数；超额 ignored 行仍挂在本期供审计，
    # 但不计入周刊体量，也不会跨周漂移。
    result["batch_total"] = len(
        batch_articles(
            batch_id,
            statuses=("ready", "pending_review", "translation_pending"),
        )
    )
    result["batch_pending"] = len(
        batch_articles(batch_id, statuses=("pending_review", "translation_pending"))
    )
    result["filtered_out"] = filtered_out
    return result


def generate_batch_review(
    digest_id: int,
    *,
    ai_client: Any = None,
    settings: dict | None = None,
    auto_approve: bool = False,
) -> dict:
    """Build a deterministic admin issue preview (legacy function name)."""
    from journal_taxonomy import DISCIPLINES

    articles = public_batch_articles(digest_id)
    lines = ["# 双语电子期刊整期预览", "", f"共 {len(articles)} 篇完整文章。", ""]
    for discipline in DISCIPLINES:
        items = [item for item in articles if item.get("ai_discipline") == discipline]
        if not items:
            continue
        lines.extend([f"## {discipline}", ""])
        for item in items:
            lines.append(f"- {item.get('title_zh')}  ")
            lines.append(f"  {item.get('title')}")
        lines.append("")
    review_md = "\n".join(lines).strip()
    review_html = _markdown_to_html(review_md)
    current = get_batch(digest_id) or {}
    # A curated sample remains visible while its deterministic TOC is refreshed.
    # It is deliberately not approved, sent, or reused as the next weekly batch.
    current_status = str(current.get("status") or "")
    current_review = str(current.get("review_status") or "")
    if auto_approve:
        next_status = "published"
        next_review = "approved"
    elif current_status == "sample":
        next_status = "sample"
        next_review = "pending"
    elif current_status in {"published", "sent", "archived"}:
        # A harmless preview refresh must never reopen or unapprove a locked issue.
        next_status = current_status
        next_review = current_review or "approved"
    else:
        # Complete articles become member-visible immediately, while the email
        # remains blocked until the administrator performs the final send check.
        next_status = "ready_to_send" if articles else "reviewing"
        next_review = "pending"
    update_batch_review(
        digest_id,
        review_md=review_md,
        review_html=review_html,
        review_model="deterministic-issue-preview-v1",
        review_status=next_review,
        status=next_status,
        mark_generated=True,
        mark_approved=auto_approve,
    )
    return get_batch(digest_id) or {}


RECIPIENT_MODES = ("subscribers", "members")


def resolve_recipients(
    mode: str,
    *,
    plan_codes: list[str] | None = None,
    emails: list[str] | None = None,
) -> tuple[list[dict], bool]:
    """把受众模式解析为收件人列表，并返回是否需要按 journal_alerts 权限过滤。

    返回 (recipients, enforce_permission)。recipients 每项 {email, user_id?, subscription_id?, unsubscribe_token?}。
    - subscribers：邮箱订阅者，并在发送前再次校验有效会员权限；
    - members：全部有效旧版与新版会员，可选套餐范围。
    """
    mode = (mode or "subscribers").strip().lower()
    if mode == "members":
        from membership import list_active_member_emails

        rows = list_active_member_emails(plan_codes)
        return ([{"email": r["email"], "user_id": r.get("user_id")} for r in rows if r.get("email")], False)
    if mode not in RECIPIENT_MODES:
        raise ValueError("国外文献精选周刊只能发送给有效会员或已订阅的有效会员。")
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
    """Send an approved issue's short introduction and article cards, deduplicated by email.

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
    if not approved:
        return {"sent": 0, "batch_id": batch_id, "reason": "issue_not_approved"}
    if not force and not bool(batch.get("auto_send")):
        return {
            "sent": 0,
            "batch_id": batch_id,
            "reason": "scheduled_send_not_approved",
        }
    complete_articles = public_batch_articles(batch_id)
    if not complete_articles:
        return {"sent": 0, "batch_id": batch_id, "reason": "no_complete_articles"}
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
    subject = f"{settings['subject_prefix']}：{batch.get('issue_key') or '本期'}（{len(complete_articles)}篇）"
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
                                       f"{JOURNAL_WEEKLY_TITLE}仅供有效会员使用；会员已过期或账号已停用。",
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
    if sent > 0 or not errors:
        update_batch_review(batch_id, status="sent")
        with _connect() as conn:
            conn.execute(
                "UPDATE journal_digests SET sent_at = ?, emails_sent = emails_sent + ?, updated_at = ? WHERE id = ?",
                (utc_now_text(), sent, utc_now_text(), batch_id),
            )
            conn.commit()
        # 归档更早的已发送批次，使首页只留存最新一期（本期内容留存到下一期发送）。
        archive_sent_batches_before(batch_id)
        try:
            from journal_fulltext import purge_source_pdfs, write_issue_snapshot

            write_issue_snapshot(get_batch(batch_id) or batch, complete_articles)
            purge_source_pdfs(retain_issues=12)
        except Exception as exc:
            errors.append(f"issue-snapshot/retention: {exc}")
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
    """Render the weekly short introduction and metadata cards (no AI review)."""
    from journal_taxonomy import DISCIPLINES

    settings = normalize_alert_settings(settings)
    token = str(subscription.get("unsubscribe_token") or "")
    unsubscribe_url = f"{base_url}/journal-alerts/unsubscribe/{token}" if (base_url and token) else ""
    issue_key = str(digest.get("issue_key") or f"第{digest.get('id')}期")
    articles = public_batch_articles(int(digest["id"]))
    groups: dict[str, list[dict]] = {name: [] for name in DISCIPLINES}
    for article in articles:
        groups.setdefault(str(article.get("ai_discipline") or ""), []).append(article)
    intro = f"{settings['intro_text']} 本期 {issue_key} 共收录 {len(articles)} 篇，分属 {sum(bool(v) for v in groups.values())} 个研究领域。"
    period_start = str(digest.get("period_start") or "")[:10]
    period_end = str(digest.get("period_end") or "")[:10]
    period_text = f"{period_start} — {period_end}" if period_start and period_end else period_start or period_end
    lines = [
        JOURNAL_WEEKLY_TITLE,
        JOURNAL_WEEKLY_TITLE_EN,
        " · ".join(part for part in (issue_key, period_text, f"{len(articles)} 篇完整双语文章") if part),
        "",
        intro,
        "",
        "本期目录 / CONTENTS",
        "=================",
        "",
    ]
    html_parts = [
        '<div style="font-family:\'Microsoft YaHei\',\'Noto Sans CJK SC\',sans-serif;max-width:760px;margin:0 auto;color:#231d17">',
        '<header style="text-align:center;border-top:4px double #231d17;border-bottom:4px double #231d17;padding:28px 18px 24px;background:#fffaf3">',
        '<div style="font-size:12px;font-weight:700;letter-spacing:.18em;color:#8f1d1d">马克思主义 · 哲学 · 批判理论</div>',
        f'<h1 style="font-family:\'Songti SC\',SimSun,serif;font-size:38px;letter-spacing:.08em;margin:8px 0 2px">{JOURNAL_WEEKLY_TITLE}</h1>',
        f'<p style="font-family:Georgia,serif;font-size:16px;font-style:italic;color:#72675d;margin:0 0 16px">{JOURNAL_WEEKLY_TITLE_EN}</p>',
        f'<div style="font-size:13px;color:#4f463f">{html.escape(" · ".join(part for part in (issue_key, period_text, f"{len(articles)} 篇完整双语文章") if part))}</div>',
        '</header>',
        f'<p style="line-height:1.85">{html.escape(intro)}</p>',
        '<section style="border:1px solid #decfbd;background:#fffdf9;padding:22px 24px;margin:20px 0 28px">',
        '<h2 style="font-family:\'Songti SC\',SimSun,serif;text-align:center;font-size:25px;letter-spacing:.14em;margin:0">本期目录</h2>',
        '<p style="font-family:Georgia,serif;text-align:center;font-style:italic;color:#72675d;margin:5px 0 20px">Contents · English original with Chinese translation</p>',
    ]
    for discipline in DISCIPLINES:
        items = groups.get(discipline) or []
        if not items:
            continue
        lines.extend([f"【{discipline}】", ""])
        html_parts.append(
            f'<h3 style="font-size:16px;color:#651313;border-bottom:2px solid #8f1d1d;padding-bottom:6px;margin:18px 0 8px">{html.escape(discipline)} '
            f'<span style="font-size:10px;color:#72675d;font-weight:400">{len(items)} ARTICLES</span></h3><ol style="margin:0;padding-left:24px">'
        )
        for article in items:
            title_zh = str(article.get("title_zh") or "")
            title_en = str(article.get("title") or "")
            read_url = f"{base_url}/journal-alerts/articles/{int(article['id'])}" if base_url else ""
            lines.extend([f"- {title_zh}", f"  {title_en}"])
            title_html = (
                f'<a href="{html.escape(read_url)}" style="color:#231d17;text-decoration:none">{html.escape(title_zh)}</a>'
                if read_url else html.escape(title_zh)
            )
            html_parts.append(
                '<li style="padding:6px 0 8px;line-height:1.55">'
                f'<strong>{title_html}</strong><br>'
                f'<span style="font-family:Georgia,serif;font-style:italic;color:#62574e">{html.escape(title_en)}</span>'
                '</li>'
            )
        lines.append("")
        html_parts.append('</ol>')
    lines.extend(["文章简介 / ARTICLE DIGESTS", "=====================", ""])
    html_parts.extend(
        [
            '</section>',
            '<h2 style="font-family:\'Songti SC\',SimSun,serif;text-align:center;font-size:25px;letter-spacing:.12em;margin:0 0 22px">文章简介</h2>',
        ]
    )
    for discipline in DISCIPLINES:
        items = groups.get(discipline) or []
        if not items:
            continue
        lines.extend([discipline, "=" * len(discipline), ""])
        html_parts.append(
            f'<h2 style="font-size:18px;color:#651313;border-left:4px solid #8f1d1d;padding-left:10px;margin:24px 0 12px">{html.escape(discipline)}</h2>'
        )
        for index, article in enumerate(items, 1):
            authors_en = ", ".join(article.get("authors") or []) or "Unknown"
            authors_zh = "、".join(article.get("authors_zh") or []) or authors_en
            read_url = f"{base_url}/journal-alerts/articles/{int(article['id'])}" if base_url else ""
            source_url = str(article.get("pdf_url") or article.get("url") or "")
            citations = (
                ("GB/T 7714—2015（英文原始）", article.get("citation_gb2015") or ""),
                ("《马克思主义研究》（英文原始）", article.get("citation_mks_en") or ""),
            )
            lines.extend(
                [
                    f"{index}. {article.get('title_zh') or ''}",
                    f"   {article.get('title') or ''}",
                    f"期刊：{article.get('journal_name_zh') or ''} / {article.get('journal_name') or ''}",
                    f"作者：{authors_zh} / {authors_en}",
                    f"摘要（中）：{article.get('abstract_zh') or ''}",
                    f"Abstract: {article.get('abstract') or ''}",
                    *(f"{label}：{value}" for label, value in citations),
                    *( [f"流式阅读：{read_url}"] if read_url else [] ),
                    *( [f"公开PDF来源：{source_url}"] if source_url else [] ),
                    "",
                ]
            )
            html_parts.append(
                '<section style="border:1px solid #decfbd;border-radius:12px;background:#fffdf9;padding:18px;margin:0 0 14px">'
                f'<h3 style="font-size:18px;margin:0 0 5px;color:#231d17">{html.escape(article.get("title_zh") or "")}</h3>'
                f'<p style="font-family:Georgia,serif;margin:0 0 12px;color:#62574e">{html.escape(article.get("title") or "")}</p>'
                f'<p style="line-height:1.7"><strong>期刊：</strong>{html.escape(article.get("journal_name_zh") or "")}<br><span style="color:#72675d">{html.escape(article.get("journal_name") or "")}</span></p>'
                f'<p style="line-height:1.7"><strong>作者：</strong>{html.escape(authors_zh)}<br><span style="color:#72675d">{html.escape(authors_en)}</span></p>'
                f'<p style="line-height:1.8"><strong>摘要：</strong>{html.escape(article.get("abstract_zh") or "")}</p>'
                f'<p style="line-height:1.75;color:#62574e"><strong>Abstract:</strong> {html.escape(article.get("abstract") or "")}</p>'
            )
            for label, value in citations:
                html_parts.append(
                    f'<p style="font-size:13px;line-height:1.65;color:#62574e"><strong>{html.escape(label)}：</strong>{html.escape(value)}</p>'
                )
            links = []
            if read_url:
                links.append(
                    f'<a href="{html.escape(read_url)}" style="display:inline-block;background:#8f1d1d;color:#fff;text-decoration:none;border-radius:999px;padding:9px 16px;margin-right:8px">流式阅读</a>'
                )
            if source_url:
                links.append(
                    f'<a href="{html.escape(source_url)}" style="display:inline-block;color:#8f1d1d;text-decoration:none;border:1px solid #8f1d1d;border-radius:999px;padding:8px 15px">公开 PDF 来源</a>'
                )
            html_parts.append(f'<p>{"".join(links)}</p></section>')

    latest_url = f"{base_url}/journal-alerts/latest" if base_url else ""
    text_body = "\n".join(lines)
    footer_links = []
    if latest_url:
        text_body += f"\n\n查看本期全部文章：{latest_url}"
        footer_links.append(f'<a href="{html.escape(latest_url)}" style="color:#8f1d1d">查看本期全部文章</a>')
    if unsubscribe_url:
        text_body += f"\n\n退订链接：{unsubscribe_url}"
        footer_links.append(f'<a href="{html.escape(unsubscribe_url)}" style="color:#72675d">退订{JOURNAL_WEEKLY_TITLE}邮件</a>')
    if footer_links:
        html_parts.append(
            '<p style="margin-top:16px;padding-top:12px;border-top:1px solid #e7dccb;'
            'font-size:13px;color:#72675d">' + " &nbsp;·&nbsp; ".join(footer_links) + "</p>"
        )
    html_parts.append("</div>")
    return text_body, "\n".join(html_parts)


def run_journal_alerts_once(
    *,
    ai_client: Any = None,
    base_url: str = "",
    smtp_config: SMTPConfig | None = None,
    send: bool = True,
) -> dict:
    """Legacy entry point: collect metadata, then send only an approved complete issue."""
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
