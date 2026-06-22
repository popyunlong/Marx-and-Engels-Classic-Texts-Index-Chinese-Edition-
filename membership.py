from __future__ import annotations

import json
import hashlib
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime_env import APPDATA_DIR, secure_db_file


DB_PATH = APPDATA_DIR / "membership.sqlite3"
SECRET_KEY_PATH = APPDATA_DIR / "session_secret.txt"
# 灾备导出账本（append-only）：每笔会员开通/续费即时追加一行 JSON（含完整账号信息），供本地每分钟
# 增量拉取到站长本机；站点若被封可凭此即时迁移会员。拉取/落地脚本见 deploy/sync_members_local.ps1。
MEMBER_EXPORT_DIR = APPDATA_DIR / "member_exports"
MEMBER_EXPORT_FILE = MEMBER_EXPORT_DIR / "members.ndjson"
_UNSET = object()

# 请求级缓存（挂在 flask.g）：会员快照在同一请求内不会变化，却被鉴权/视图状态/各功能权限判定反复
# 查库（current_view_state 一次就要为每个功能键各取一次快照，约 25 次）。这里按 user_id 在请求内 memo，
# 把每请求的会员查库塌缩成一次。跨请求始终重查、零陈旧；写入订阅（mark_order_paid/create_manual_
# subscription）时主动清空本请求缓存，杜绝「同请求内先写后读拿到旧值」。无请求上下文（脚本/后台任务）
# 时退回直连库。纯数据层对 flask 的依赖以 try 包裹，flask 不可用也不影响导入。
try:  # pragma: no cover - flask 必然可用，仅作纯数据层防御
    from flask import g as _flask_g, has_request_context as _has_request_context
except Exception:  # noqa: BLE001
    _flask_g = None

    def _has_request_context() -> bool:  # type: ignore[misc]
        return False

_MEMBERSHIP_REQ_CACHE_ATTR = "_membership_snapshot_req_cache"


def _request_membership_cache() -> dict | None:
    if _flask_g is None or not _has_request_context():
        return None
    cache = getattr(_flask_g, _MEMBERSHIP_REQ_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(_flask_g, _MEMBERSHIP_REQ_CACHE_ATTR, cache)
        except Exception:  # noqa: BLE001
            return None
    return cache


def _invalidate_request_membership_cache() -> None:
    if _flask_g is None or not _has_request_context():
        return
    try:
        setattr(_flask_g, _MEMBERSHIP_REQ_CACHE_ATTR, {})
    except Exception:  # noqa: BLE001
        pass


DEFAULT_PLANS = (
    {
        "code": "monthly",
        "name": "月度会员",
        "price_cents": 2900,
        "currency": "CNY",
        "interval_months": 1,
        "description": "适合按月使用，解锁全文 PDF、页内图像与 AI 讲解。",
        "sort_order": 10,
    },
    {
        "code": "yearly",
        "name": "年度会员",
        "price_cents": 29900,
        "currency": "CNY",
        "interval_months": 12,
        "description": "适合长期使用，全年访问会员功能。",
        "sort_order": 20,
    },
)


@dataclass(frozen=True)
class MembershipSnapshot:
    is_logged_in: bool
    is_active_member: bool
    status: str
    plan_code: str
    plan_name: str
    expires_at: str
    days_remaining: int | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_text() -> str:
    return utc_now().isoformat(timespec="seconds")


def china_day_text(value: datetime | None = None) -> str:
    base = value or utc_now()
    return base.astimezone(timezone(timedelta(hours=8))).date().isoformat()


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _months_delta(months: int) -> timedelta:
    return timedelta(days=max(1, months) * 30)


_WAL_ENABLED = False


def _connect() -> sqlite3.Connection:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)  # Python 默认 busy timeout=5s，锁等待而非立即报错
    secure_db_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL：8 线程 waitress 下，阅读热路径每请求都写审计/活动/在线记录；回滚日志模式下写会独占库锁、
    # 阻塞其它线程的读（查用户/会员/设置）。WAL 允许「1 写 + N 读」并发，消除这种排队。WAL 是持久库
    # 属性，进程内设一次即可（之后所有连接、含旁路服务自动继承）；备份走 sqlite3 .backup（WAL 感知、
    # 一致快照）不受影响。设置失败（只读盘/极旧 SQLite）静默回退到原模式，绝不阻断连接。
    global _WAL_ENABLED
    if not _WAL_ENABLED:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            _WAL_ENABLED = True
        except sqlite3.Error:
            pass
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _hash_token(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def load_session_secret() -> str:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_PATH.exists():
        value = SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(48)
    SECRET_KEY_PATH.write_text(value, encoding="utf-8")
    return value


def init_membership_db() -> Path:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                is_active INTEGER NOT NULL DEFAULT 1,
                daily_ai_token_limit_override INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS plans (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'CNY',
                interval_months INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                daily_ai_token_limit INTEGER,
                features TEXT NOT NULL DEFAULT '',
                badge TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                plan_code TEXT NOT NULL,
                status TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payment_provider TEXT NOT NULL DEFAULT 'pending',
                payment_reference TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                paid_at TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (plan_code) REFERENCES plans(code)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_code TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                starts_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (plan_code) REFERENCES plans(code)
            );

            CREATE TABLE IF NOT EXISTS payment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL,
                provider TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_email_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                user_id INTEGER,
                purpose TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                code_hash TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                expires_at TEXT NOT NULL,
                used_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS site_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                user_id INTEGER,
                day TEXT NOT NULL,
                feature TEXT NOT NULL DEFAULT 'site',
                path TEXT NOT NULL DEFAULT '',
                request_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(session_key, day, feature)
            );

            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                session_key TEXT NOT NULL DEFAULT '',
                day TEXT NOT NULL,
                feature TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated INTEGER NOT NULL DEFAULT 1,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reader_access_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                actor_type TEXT NOT NULL DEFAULT '',
                session_key TEXT NOT NULL DEFAULT '',
                user_id INTEGER,
                email TEXT NOT NULL DEFAULT '',
                client_ip TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                endpoint TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                reader_mode TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                page INTEGER NOT NULL DEFAULT 0,
                is_rate_limited INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS online_presence (
                bucket_start TEXT NOT NULL,
                session_key TEXT NOT NULL,
                user_id INTEGER,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (bucket_start, session_key)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_payment_events_order_no ON payment_events(order_no, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_account_email_tokens_email
                ON account_email_tokens(email, purpose, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_site_activity_day_feature
                ON site_activity(day, feature, last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_site_activity_last_seen
                ON site_activity(last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_usage_day_user
                ON ai_usage(day, user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reader_access_day_actor
                ON reader_access_events(day, actor_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reader_access_created
                ON reader_access_events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_online_presence_bucket
                ON online_presence(bucket_start);
            """
        )
        user_columns = _table_columns(conn, "users")
        if "email_verified_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT NOT NULL DEFAULT ''")
        if "deactivated_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN deactivated_at TEXT NOT NULL DEFAULT ''")
        if "daily_ai_token_limit_override" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN daily_ai_token_limit_override INTEGER")
        plan_columns = _table_columns(conn, "plans")
        if "daily_ai_token_limit" not in plan_columns:
            conn.execute("ALTER TABLE plans ADD COLUMN daily_ai_token_limit INTEGER")
        if "daily_zhipu_token_limit" not in plan_columns:
            # 智谱（GLM 联网通道）每日 token 子配额按套餐分级。建列时按套餐时长一次性
            # 初始化为与套餐营销文案一致的默认值（月 3 万/季 6 万/年 10 万），后台可改；
            # NULL＝跟随智能服务里的全局默认，0＝该套餐不限。
            conn.execute("ALTER TABLE plans ADD COLUMN daily_zhipu_token_limit INTEGER")
            conn.execute(
                """
                UPDATE plans SET daily_zhipu_token_limit = CASE
                    WHEN interval_months >= 12 THEN 100000
                    WHEN interval_months >= 3 THEN 60000
                    ELSE 30000
                END
                """
            )
        # 每套餐独立营销文案：features=卖点清单（每行一条），badge=角标/促销标签。
        # 旧库通过 ALTER 补列，默认空＝定价页回退到全站统一的默认卖点，向后兼容。
        if "features" not in plan_columns:
            conn.execute("ALTER TABLE plans ADD COLUMN features TEXT NOT NULL DEFAULT ''")
        if "badge" not in plan_columns:
            conn.execute("ALTER TABLE plans ADD COLUMN badge TEXT NOT NULL DEFAULT ''")
        # 资源包（消耗型次数包）：kind='membership' 仍是按月会员；kind='credit_pack' 是一次性
        # 购买的「研究级检索次数 + AI随心问次数」，支付成功记入 ai_credit_ledger 台账而非开会员。
        if "kind" not in plan_columns:
            conn.execute("ALTER TABLE plans ADD COLUMN kind TEXT NOT NULL DEFAULT 'membership'")
        if "research_credits" not in plan_columns:
            conn.execute("ALTER TABLE plans ADD COLUMN research_credits INTEGER NOT NULL DEFAULT 0")
        if "chat_credits" not in plan_columns:
            conn.execute("ALTER TABLE plans ADD COLUMN chat_credits INTEGER NOT NULL DEFAULT 0")
        # AI 导学问答（阅读器逐页讲解）次数：仅限 DeepSeek-V4-Pro 主通道消耗（智谱联网通道不抵扣）。
        if "reader_credits" not in plan_columns:
            conn.execute("ALTER TABLE plans ADD COLUMN reader_credits INTEGER NOT NULL DEFAULT 0")
            # 一次性回填：给已存在的 ¥3 资源包补 10 次 AI 导学问答；ALTER 仅首启执行一次，幂等安全。
            conn.execute(
                "UPDATE plans SET reader_credits = 10 WHERE code = 'pack_basic' AND reader_credits = 0"
            )
        # AI 次数台账：研究级检索/随心问的「资源包」消耗型次数，余额=按 (user,kind) 求和。
        # delta>0 为发放（购买/管理员），delta<0 为消耗（免费额度用完后每次扣 1）。永久有效、可叠加。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_credit_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                order_no TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_credit_ledger_user_kind ON ai_credit_ledger(user_id, kind)"
        )
        # AI 用量审计字段：记录用户真实输入摘要、来源页与客户端 IP，便于后台排查异常用量。
        ai_usage_columns = _table_columns(conn, "ai_usage")
        if "prompt_excerpt" not in ai_usage_columns:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN prompt_excerpt TEXT NOT NULL DEFAULT ''")
        if "client_ip" not in ai_usage_columns:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN client_ip TEXT NOT NULL DEFAULT ''")
        if "source_ref" not in ai_usage_columns:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            UPDATE users
            SET email_verified_at = CASE
                    WHEN last_login_at != '' THEN last_login_at
                    ELSE created_at
                END,
                updated_at = CASE WHEN updated_at = '' THEN created_at ELSE updated_at END
            WHERE email_verified_at = ''
              AND (
                last_login_at != ''
                OR EXISTS (
                    SELECT 1 FROM subscriptions s
                    WHERE s.user_id = users.id
                      AND s.status = 'active'
                      AND s.expires_at > ?
                )
              )
            """,
            (utc_now_text(),),
        )
        for plan in DEFAULT_PLANS:
            conn.execute(
                """
                INSERT INTO plans(code, name, price_cents, currency, interval_months, description, is_active, sort_order)
                VALUES(:code, :name, :price_cents, :currency, :interval_months, :description, 1, :sort_order)
                ON CONFLICT(code) DO NOTHING
                """,
                plan,
            )
        # 种子：¥3 资源包（10 次研究级检索 + 20 次 AI随心问 + 10 次 AI 导学问答），所有登录用户可购、
        # 永久有效可叠加。后台「套餐管理」可改价/次数/上下架；ON CONFLICT DO NOTHING 保证不覆盖管理员后续修改。
        conn.execute(
            """
            INSERT INTO plans(
                code, name, price_cents, currency, interval_months, description,
                kind, research_credits, chat_credits, reader_credits, is_active, sort_order
            )
            VALUES('pack_basic', '研究资源包', 300, 'CNY', 1,
                   '一次性购买：10 次研究级检索 + 20 次 AI随心问 + 10 次 AI 导学问答（DeepSeek-V4-Pro），永久有效、可叠加。',
                   'credit_pack', 10, 20, 10, 1, 100)
            ON CONFLICT(code) DO NOTHING
            """
        )
        conn.commit()
    return DB_PATH


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def list_active_plans() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT code, name, price_cents, currency, interval_months, description,
                   daily_ai_token_limit, daily_zhipu_token_limit, features, badge,
                   kind, research_credits, chat_credits, reader_credits
            FROM plans
            WHERE is_active = 1
            ORDER BY sort_order ASC, code ASC
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_plans(include_inactive: bool = False) -> list[dict]:
    where = "" if include_inactive else "WHERE is_active = 1"
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT code, name, price_cents, currency, interval_months, description,
                   daily_ai_token_limit, daily_zhipu_token_limit, features, badge, is_active, sort_order,
                   kind, research_credits, chat_credits, reader_credits
            FROM plans
            {where}
            ORDER BY sort_order ASC, code ASC
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_plan(plan_code: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT code, name, price_cents, currency, interval_months, description,
                   daily_ai_token_limit, daily_zhipu_token_limit, features, badge, is_active,
                   kind, research_credits, chat_credits, reader_credits
            FROM plans
            WHERE code = ?
            """,
            (plan_code,),
        ).fetchone()
    return row_to_dict(row)


def upsert_plan(
    *,
    code: str,
    name: str,
    price_cents: int,
    currency: str = "CNY",
    interval_months: int,
    description: str = "",
    daily_ai_token_limit: int | None = None,
    daily_zhipu_token_limit: int | None = None,
    features: str = "",
    badge: str = "",
    is_active: bool = True,
    sort_order: int = 0,
    kind: str = "membership",
    research_credits: int = 0,
    chat_credits: int = 0,
    reader_credits: int = 0,
) -> dict:
    normalized_code = (code or "").strip()
    if not normalized_code:
        raise ValueError("套餐代码不能为空。")
    if interval_months < 1:
        raise ValueError("套餐周期必须大于等于 1。")
    if price_cents < 0:
        raise ValueError("套餐价格不能小于 0。")
    normalized_kind = "credit_pack" if str(kind or "").strip().lower() == "credit_pack" else "membership"
    research_credits = max(0, int(research_credits or 0))
    chat_credits = max(0, int(chat_credits or 0))
    reader_credits = max(0, int(reader_credits or 0))
    if daily_ai_token_limit is not None and int(daily_ai_token_limit) < 0:
        raise ValueError("AI token 限额不能小于 0。")
    if daily_zhipu_token_limit is not None and int(daily_zhipu_token_limit) < 0:
        raise ValueError("智谱每日限额不能小于 0。")
    # 卖点清单逐行规整：去掉空行与首尾空白，统一以 \n 存储，便于定价页逐行渲染。
    normalized_features = "\n".join(
        line.strip() for line in (features or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO plans(
                code, name, price_cents, currency, interval_months, description,
                daily_ai_token_limit, daily_zhipu_token_limit, features, badge, is_active, sort_order,
                kind, research_credits, chat_credits, reader_credits
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                price_cents=excluded.price_cents,
                currency=excluded.currency,
                interval_months=excluded.interval_months,
                description=excluded.description,
                daily_ai_token_limit=excluded.daily_ai_token_limit,
                daily_zhipu_token_limit=excluded.daily_zhipu_token_limit,
                features=excluded.features,
                badge=excluded.badge,
                is_active=excluded.is_active,
                sort_order=excluded.sort_order,
                kind=excluded.kind,
                research_credits=excluded.research_credits,
                chat_credits=excluded.chat_credits,
                reader_credits=excluded.reader_credits
            """,
            (
                normalized_code,
                (name or "").strip() or normalized_code,
                int(price_cents),
                (currency or "CNY").strip().upper() or "CNY",
                int(interval_months),
                (description or "").strip(),
                None if daily_ai_token_limit is None else int(daily_ai_token_limit),
                None if daily_zhipu_token_limit is None else int(daily_zhipu_token_limit),
                normalized_features,
                (badge or "").strip(),
                1 if is_active else 0,
                int(sort_order),
                normalized_kind,
                research_credits,
                chat_credits,
                reader_credits,
            ),
        )
        row = conn.execute(
            """
            SELECT code, name, price_cents, currency, interval_months, description,
                   daily_ai_token_limit, daily_zhipu_token_limit, features, badge, is_active, sort_order,
                   kind, research_credits, chat_credits, reader_credits
            FROM plans
            WHERE code = ?
            """,
            (normalized_code,),
        ).fetchone()
        conn.commit()
    return row_to_dict(row) or {}


def create_user(*, email: str, display_name: str, password_hash: str, email_verified_at: str = "") -> dict:
    now = utc_now_text()
    normalized_email = normalize_email(email)
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO users(email, display_name, password_hash, email_verified_at, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (normalized_email, display_name.strip(), password_hash, email_verified_at or "", now, now),
        )
        user_id = cur.lastrowid
        row = conn.execute(
            """
            SELECT id, email, display_name, role, is_active, email_verified_at, deactivated_at,
                   daily_ai_token_limit_override, created_at, updated_at, last_login_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        conn.commit()
    return row_to_dict(row) or {}


def get_user_by_email(email: str) -> dict | None:
    normalized_email = normalize_email(email)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, email, display_name, password_hash, role, is_active, email_verified_at, deactivated_at,
                   daily_ai_token_limit_override, created_at, updated_at, last_login_at
            FROM users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
    return row_to_dict(row)


def get_user_by_id(user_id: int | None) -> dict | None:
    if not user_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, email, display_name, password_hash, role, is_active, email_verified_at, deactivated_at,
                   daily_ai_token_limit_override, created_at, updated_at, last_login_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    return row_to_dict(row)


def update_last_login(user_id: int) -> None:
    now = utc_now_text()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET last_login_at = ?,
                email_verified_at = CASE WHEN email_verified_at = '' THEN ? ELSE email_verified_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, now, user_id),
        )
        conn.commit()


def list_users(search_text: str = "", limit: int = 50) -> list[dict]:
    needle = f"%{(search_text or '').strip().lower()}%"
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                u.id,
                u.email,
                u.display_name,
                u.role,
                u.is_active,
                u.email_verified_at,
                u.deactivated_at,
                u.daily_ai_token_limit_override,
                u.created_at,
                u.updated_at,
                u.last_login_at,
                s.status AS membership_status,
                s.expires_at AS membership_expires_at,
                s.plan_code AS membership_plan_code,
                p.daily_ai_token_limit AS membership_plan_daily_ai_token_limit,
                p.name AS membership_plan_name
            FROM users u
            LEFT JOIN subscriptions s
                ON s.id = (
                    SELECT s2.id
                    FROM subscriptions s2
                    WHERE s2.user_id = u.id
                    ORDER BY s2.created_at DESC, s2.id DESC
                    LIMIT 1
                )
            LEFT JOIN plans p ON p.code = s.plan_code
            WHERE
                ? = '%%'
                OR lower(u.email) LIKE ?
                OR lower(u.display_name) LIKE ?
            ORDER BY u.created_at DESC, u.id DESC
            LIMIT ?
            """,
            (needle, needle, needle, max(1, int(limit))),
        ).fetchall()
    users = [row_to_dict(row) for row in rows]
    # 后台展示与运行期口径对齐：上面的 SQL 取「最近创建的一条订阅」，但会员身份/到期实际按
    # 「当前有效订阅中最高档 + 最远到期日」判定（见 _compute_membership_snapshot）。若不对齐，
    # 高档会员叠买低档后台会误显示为低档（且 membership_plan_code 还会喂给 /admin 的逐用户
    # 权限预览，导致预览也按低档算）。这里对「当前有效会员」用快照覆盖这几列；非会员保持原值
    # （展示已到期/未开通）。token 限额按套餐表一次性建映射，避免逐用户重复查库。
    plan_token_by_code = {
        str(p.get("code")): p.get("daily_ai_token_limit")
        for p in list_plans(include_inactive=True)
    }
    for user in users:
        snapshot = get_membership_snapshot(int(user["id"]))
        if snapshot.is_active_member:
            user["membership_status"] = "active"
            user["membership_plan_code"] = snapshot.plan_code
            user["membership_plan_name"] = snapshot.plan_name
            user["membership_expires_at"] = snapshot.expires_at
            user["membership_plan_daily_ai_token_limit"] = plan_token_by_code.get(snapshot.plan_code)
    return users


def list_active_user_emails() -> list[dict]:
    """全部启用中的注册用户（用于期刊综述群发）。返回 [{user_id, email, display_name}]。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id AS user_id, email, display_name
            FROM users
            WHERE is_active = 1 AND TRIM(email) != ''
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_active_member_emails(plan_codes: list[str] | None = None) -> list[dict]:
    """有有效付费会员资格的用户（subscriptions.status='active' 且未过期）。

    plan_codes 非空时仅返回这些套餐的会员；为空/None 返回全部有效会员。
    返回 [{user_id, email, display_name, plan_code}]，按用户去重（同人多订阅取其一）。
    """
    now = utc_now_text()
    params = [now]
    plan_filter = ""
    codes = [str(c).strip() for c in (plan_codes or []) if str(c).strip()]
    if codes:
        placeholders = ",".join("?" for _ in codes)
        plan_filter = f" AND s.plan_code IN ({placeholders})"
        params.extend(codes)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT u.id AS user_id, u.email, u.display_name, s.plan_code
            FROM users u
            JOIN subscriptions s ON s.user_id = u.id
            WHERE u.is_active = 1 AND TRIM(u.email) != ''
              AND s.status = 'active' AND s.expires_at > ?{plan_filter}
            GROUP BY u.id
            ORDER BY u.created_at ASC, u.id ASC
            """,
            params,
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def update_user_account(
    user_id: int,
    *,
    role: str | None = None,
    is_active: bool | None = None,
    daily_ai_token_limit_override: int | None | object = _UNSET,
) -> dict | None:
    updates: list[str] = []
    values: list[object] = []
    if role is not None:
        updates.append("role = ?")
        values.append((role or "").strip() or "member")
    if is_active is not None:
        updates.append("is_active = ?")
        values.append(1 if is_active else 0)
    if daily_ai_token_limit_override is not _UNSET:
        if daily_ai_token_limit_override is not None and int(daily_ai_token_limit_override) < 0:
            raise ValueError("AI token limit cannot be negative.")
        updates.append("daily_ai_token_limit_override = ?")
        values.append(None if daily_ai_token_limit_override is None else int(daily_ai_token_limit_override))
    updates.append("updated_at = ?")
    values.append(utc_now_text())
    values.append(user_id)
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE users
            SET {", ".join(updates)}
            WHERE id = ?
            """,
            values,
        )
        row = conn.execute(
            """
            SELECT id, email, display_name, role, is_active, email_verified_at, deactivated_at,
                   daily_ai_token_limit_override, created_at, updated_at, last_login_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        conn.commit()
    return row_to_dict(row)


def create_account_email_token(
    *,
    email: str,
    purpose: str,
    user_id: int | None = None,
    code: str = "",
    token: str = "",
    ttl_minutes: int = 15,
    metadata: dict | None = None,
) -> dict:
    normalized_email = normalize_email(email)
    if not token:
        token = secrets.token_urlsafe(32)
    now = utc_now()
    created_at = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(minutes=max(1, int(ttl_minutes)))).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO account_email_tokens(
                email, user_id, purpose, token_hash, code_hash, metadata_json, expires_at, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_email,
                user_id,
                (purpose or "").strip(),
                _hash_token(token),
                _hash_token(code) if code else "",
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                expires_at,
                created_at,
            ),
        )
        conn.commit()
    return {
        "email": normalized_email,
        "user_id": user_id,
        "purpose": purpose,
        "token": token,
        "code": code,
        "expires_at": expires_at,
    }


def verify_account_email_code(*, email: str, purpose: str, code: str, max_attempts: int = 5) -> dict | None:
    normalized_email = normalize_email(email)
    now_text = utc_now_text()
    code_hash = _hash_token((code or "").strip())
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM account_email_tokens
            WHERE email = ?
              AND purpose = ?
              AND used_at = ''
              AND code_hash <> ''
              AND expires_at > ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_email, (purpose or "").strip(), now_text),
        ).fetchone()
        if row is None:
            return None
        attempts = int(row["attempts"] or 0)
        if attempts >= max_attempts:
            return None
        if str(row["code_hash"]) != code_hash:
            conn.execute("UPDATE account_email_tokens SET attempts = attempts + 1 WHERE id = ?", (int(row["id"]),))
            conn.commit()
            return None
        conn.execute("UPDATE account_email_tokens SET used_at = ? WHERE id = ?", (now_text, int(row["id"])))
        conn.commit()
    return row_to_dict(row)


def consume_account_email_token(*, token: str, purpose: str) -> dict | None:
    token_hash = _hash_token((token or "").strip())
    now_text = utc_now_text()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM account_email_tokens
            WHERE token_hash = ?
              AND purpose = ?
              AND used_at = ''
              AND expires_at > ?
            """,
            (token_hash, (purpose or "").strip(), now_text),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE account_email_tokens SET used_at = ? WHERE id = ?", (now_text, int(row["id"])))
        conn.commit()
    return row_to_dict(row)


def get_account_email_token(*, token: str, purpose: str) -> dict | None:
    token_hash = _hash_token((token or "").strip())
    now_text = utc_now_text()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM account_email_tokens
            WHERE token_hash = ?
              AND purpose = ?
              AND used_at = ''
              AND expires_at > ?
            """,
            (token_hash, (purpose or "").strip(), now_text),
        ).fetchone()
    return row_to_dict(row)


def update_user_password(user_id: int, password_hash: str) -> None:
    now = utc_now_text()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET password_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (password_hash, now, user_id),
        )
        conn.commit()


def deactivate_user_account(user_id: int) -> None:
    now = utc_now_text()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET is_active = 0,
                deactivated_at = CASE WHEN deactivated_at = '' THEN ? ELSE deactivated_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, user_id),
        )
        conn.commit()


def prune_duplicate_pending_orders_for_user(user_id: int | None = None) -> int:
    now = utc_now_text()
    user_filter = ""
    params: list[object] = []
    if user_id is not None:
        user_filter = "AND user_id = ?"
        params.append(int(user_id))
    params.append(now)
    with _connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE orders
            SET status = 'expired',
                notes = CASE
                    WHEN notes = '' THEN 'auto-expired duplicate pending order'
                    ELSE notes || '; auto-expired duplicate pending order'
                END
            WHERE status = 'pending'
              {user_filter}
              AND id NOT IN (
                SELECT MAX(id)
                FROM orders
                WHERE status = 'pending'
                  AND (expires_at = '' OR expires_at > ?)
                GROUP BY user_id, plan_code
              )
            """,
            tuple(params),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def create_pending_order(*, user_id: int, plan_code: str) -> dict:
    plan = get_plan(plan_code)
    if not plan or not plan.get("is_active"):
        raise ValueError("套餐不存在或未启用")
    expire_pending_orders()
    created_at = utc_now_text()
    with _connect() as conn:
        existing = conn.execute(
            """
            SELECT o.*, p.name AS plan_name, p.interval_months
            FROM orders o
            JOIN plans p ON p.code = o.plan_code
            WHERE o.user_id = ?
              AND o.plan_code = ?
              AND o.status = 'pending'
              AND (o.expires_at = '' OR o.expires_at > ?)
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT 1
            """,
            (int(user_id), plan_code, created_at),
        ).fetchone()
        if existing is not None:
            # 仅当金额/币种与当前套餐价一致时才复用旧的待支付订单；
            # 否则说明套餐价已调整，旧订单金额已过时——作废后按新价重建，避免支付页显示旧金额。
            if (
                int(existing["amount_cents"]) == int(plan["price_cents"])
                and str(existing["currency"] or "").upper() == str(plan["currency"] or "CNY").upper()
            ):
                return row_to_dict(existing) or {}
            conn.execute(
                """
                UPDATE orders
                SET status = 'expired',
                    notes = CASE WHEN notes = '' THEN 'price-changed' ELSE notes || '; price-changed' END
                WHERE id = ?
                """,
                (int(existing["id"]),),
            )

        order_no = f"{utc_now().strftime('%Y%m%d%H%M%S')}{secrets.randbelow(100000):05d}"
        expires_at = (utc_now() + timedelta(hours=24)).isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO orders(
                order_no, user_id, plan_code, status, amount_cents, currency,
                payment_provider, notes, created_at, expires_at
            )
            VALUES(?, ?, ?, 'pending', ?, ?, 'pending', '', ?, ?)
            """,
            (
                order_no,
                user_id,
                plan_code,
                plan["price_cents"],
                plan["currency"],
                created_at,
                expires_at,
            ),
        )
        order_id = cur.lastrowid
        row = conn.execute(
            """
            SELECT o.*, p.name AS plan_name, p.interval_months
            FROM orders o
            JOIN plans p ON p.code = o.plan_code
            WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()
        conn.commit()
    return row_to_dict(row) or {}


def expire_pending_orders(*, older_than_hours: int = 24) -> int:
    cutoff = (utc_now() - timedelta(hours=max(1, int(older_than_hours)))).isoformat(timespec="seconds")
    now = utc_now_text()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE orders
            SET status = 'expired',
                notes = CASE
                    WHEN notes = '' THEN 'auto-expired'
                    ELSE notes || '; auto-expired'
                END
            WHERE status = 'pending'
              AND (
                (expires_at != '' AND expires_at <= ?)
                OR (expires_at = '' AND created_at <= ?)
              )
            """,
            (now, cutoff),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def clear_pending_orders(*, user_id: int | None = None) -> int:
    """管理员一键清理：把待支付订单置为 expired（不删除，保留审计）。

    user_id 为 None 时清理全部待支付订单；否则只清理该用户的。已 expired 的旧单
    不会被 create_pending_order 复用，相当于把卡住的账号重置，下次下单会按当前价新建。
    """
    with _connect() as conn:
        if user_id is None:
            cur = conn.execute(
                """
                UPDATE orders
                SET status = 'expired',
                    notes = CASE WHEN notes = '' THEN 'admin-cleared' ELSE notes || '; admin-cleared' END
                WHERE status = 'pending'
                """
            )
        else:
            cur = conn.execute(
                """
                UPDATE orders
                SET status = 'expired',
                    notes = CASE WHEN notes = '' THEN 'admin-cleared' ELSE notes || '; admin-cleared' END
                WHERE status = 'pending' AND user_id = ?
                """,
                (int(user_id),),
            )
        conn.commit()
        return int(cur.rowcount or 0)


def list_orders_for_user(user_id: int) -> list[dict]:
    expire_pending_orders()
    prune_duplicate_pending_orders_for_user(int(user_id))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT o.*, p.name AS plan_name, p.interval_months
            FROM orders o
            JOIN plans p ON p.code = o.plan_code
            WHERE o.user_id = ?
            ORDER BY o.created_at DESC, o.id DESC
            """,
            (user_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_recent_orders(limit: int = 50) -> list[dict]:
    expire_pending_orders()
    prune_duplicate_pending_orders_for_user()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT o.*, p.name AS plan_name, u.email AS user_email, u.display_name AS user_display_name
            FROM orders o
            JOIN plans p ON p.code = o.plan_code
            JOIN users u ON u.id = o.user_id
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_order_by_no(order_no: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT o.*, p.name AS plan_name, p.interval_months
            FROM orders o
            JOIN plans p ON p.code = o.plan_code
            WHERE o.order_no = ?
            """,
            (order_no,),
        ).fetchone()
    return row_to_dict(row)


def list_subscriptions_for_user(user_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, p.name AS plan_name, p.interval_months
            FROM subscriptions s
            JOIN plans p ON p.code = s.plan_code
            WHERE s.user_id = ?
            ORDER BY s.created_at DESC, s.id DESC
            """,
            (user_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_recent_subscriptions(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, p.name AS plan_name, u.email AS user_email, u.display_name AS user_display_name
            FROM subscriptions s
            JOIN plans p ON p.code = s.plan_code
            JOIN users u ON u.id = s.user_id
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_membership_snapshot(user_id: int | None) -> MembershipSnapshot:
    if not user_id:
        return MembershipSnapshot(
            is_logged_in=False,
            is_active_member=False,
            status="anonymous",
            plan_code="",
            plan_name="",
            expires_at="",
            days_remaining=None,
        )
    uid = int(user_id)
    cache = _request_membership_cache()
    if cache is not None:
        hit = cache.get(uid)
        if hit is not None:
            return hit
    snapshot = _compute_membership_snapshot(uid)
    if cache is not None:
        cache[uid] = snapshot
    return snapshot


def _compute_membership_snapshot(user_id: int) -> MembershipSnapshot:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.status, s.plan_code, s.expires_at, s.created_at,
                   p.name AS plan_name, p.interval_months
            FROM subscriptions s
            JOIN plans p ON p.code = s.plan_code
            WHERE s.user_id = ?
            ORDER BY s.created_at DESC, s.id DESC
            """,
            (user_id,),
        ).fetchall()
    if not rows:
        return MembershipSnapshot(
            is_logged_in=True,
            is_active_member=False,
            status="free",
            plan_code="",
            plan_name="",
            expires_at="",
            days_remaining=None,
        )
    now = utc_now()

    # 会员身份取「当前有效订阅中档次最高者」，有效期取「最远到期日」——二者可能来自不同订阅。
    # 因为续费/升级会把新购时长叠加在到期日之后（见 mark_order_paid），同一用户常同时持有多张有效订阅。
    # 若按「最近一次开通」判定身份，季度会员再叠买一张月度就会被降级到月度档（token/研究配额/功能权限齐跌），
    # 用户为高档付了费反被降级。改为按最高档判定：升级即时生效、永不降级；档次以 interval_months 为代理
    # （月 1 < 季 3 < 年 12）。代价是低档叠加出的尾段也按高档计权益，属可接受的偏宽松。
    active: list[tuple] = []
    for r in rows:
        parsed = _parse_utc(r["expires_at"] or "")
        if r["status"] == "active" and parsed is not None and parsed > now:
            active.append((r, parsed))
    if active:
        # 身份：interval_months 最大；同档取到期最远、再取 id 最大（最近创建）。
        tier_row = max(
            active,
            key=lambda rp: (int(rp[0]["interval_months"] or 0), rp[1], int(rp[0]["id"])),
        )[0]
        # 有效期：所有有效订阅中的最远到期日（可能来自比 tier_row 更晚叠加的低档订阅）。
        expiry_row, expiry_dt = max(active, key=lambda rp: rp[1])
        return MembershipSnapshot(
            is_logged_in=True,
            is_active_member=True,
            status="active",
            plan_code=tier_row["plan_code"] or "",
            plan_name=tier_row["plan_name"] or "",
            expires_at=expiry_row["expires_at"] or "",
            days_remaining=max(0, (expiry_dt - now).days),
        )

    # 无任何有效订阅：沿用「最近创建」的那条用于展示已到期/已取消等状态。
    row = rows[0]
    expires_at = row["expires_at"] or ""
    parsed = _parse_utc(expires_at)
    days_remaining = max(0, (parsed - now).days) if parsed is not None else None
    return MembershipSnapshot(
        is_logged_in=True,
        is_active_member=False,
        status=row["status"],
        plan_code=row["plan_code"] or "",
        plan_name=row["plan_name"] or "",
        expires_at=expires_at,
        days_remaining=days_remaining,
    )


def _append_member_export(record: dict) -> None:
    """把一笔会员开通/续费即时写入 append-only 导出账本(NDJSON)，供本地灾备每分钟增量拉取。

    纯尽力而为、自吞异常：DR 导出绝不能影响支付主流程。每行一条 JSON，含完整账号信息（用户、订阅、
    订单），便于站点被封时凭此在异地即时恢复会员。写入后 fsync 落盘，缩小「已收款但未落盘」窗口。
    """
    try:
        MEMBER_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with open(MEMBER_EXPORT_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except Exception:
        # 导出失败不影响支付；当日的「每日全量同步」会兜底捕获该会员。
        pass


def mark_order_paid(
    *,
    order_no: str,
    provider: str,
    payment_reference: str = "",
    notes: str = "",
    source: str = "manual",
) -> dict:
    paid_at = utc_now_text()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            """
            SELECT o.*, p.interval_months, p.name AS plan_name,
                   p.kind AS plan_kind, p.research_credits, p.chat_credits, p.reader_credits
            FROM orders o
            JOIN plans p ON p.code = o.plan_code
            WHERE o.order_no = ?
            """,
            (order_no,),
        ).fetchone()
        if order is None:
            raise ValueError("订单不存在")
        if order["status"] not in {"pending", "paid"}:
            raise ValueError("订单状态不允许开通会员")
        is_credit_pack = str(order["plan_kind"] or "membership") == "credit_pack"
        if order["status"] == "paid":
            # 已支付：幂等返回。资源包不开会员，只回当前次数余额。
            if is_credit_pack:
                conn.commit()
                return {
                    "order": row_to_dict(order),
                    "subscription": None,
                    "credits": get_ai_credit_balances(int(order["user_id"])),
                }
            subscription = conn.execute(
                """
                SELECT s.*, p.name AS plan_name
                FROM subscriptions s
                JOIN plans p ON p.code = s.plan_code
                WHERE s.user_id = ? AND s.plan_code = ?
                ORDER BY s.created_at DESC, s.id DESC
                LIMIT 1
                """,
                (order["user_id"], order["plan_code"]),
            ).fetchone()
            return {
                "order": row_to_dict(order),
                "subscription": row_to_dict(subscription),
            }

        # 资源包：标记订单已支付 + 记入次数台账，不创建会员订阅。
        if is_credit_pack:
            conn.execute(
                """
                UPDATE orders
                SET status = 'paid', payment_provider = ?, payment_reference = ?, notes = ?, paid_at = ?
                WHERE order_no = ? AND status = 'pending'
                """,
                (provider, payment_reference, notes, paid_at, order_no),
            )
            research_n = max(0, int(order["research_credits"] or 0))
            chat_n = max(0, int(order["chat_credits"] or 0))
            reader_n = max(0, int(order["reader_credits"] or 0))
            ledger_rows = []
            for ck, n in (("research", research_n), ("chat", chat_n), ("reader", reader_n)):
                if n:
                    ledger_rows.append(
                        (int(order["user_id"]), ck, n, f"purchase:{order['plan_code']}", order_no, paid_at)
                    )
            if ledger_rows:
                conn.executemany(
                    "INSERT INTO ai_credit_ledger(user_id, kind, delta, reason, order_no, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    ledger_rows,
                )
            balances = conn.execute(
                "SELECT kind, COALESCE(SUM(delta), 0) AS bal FROM ai_credit_ledger WHERE user_id = ? GROUP BY kind",
                (int(order["user_id"]),),
            ).fetchall()
            updated_order = conn.execute(
                "SELECT o.*, p.name AS plan_name, p.interval_months FROM orders o "
                "JOIN plans p ON p.code = o.plan_code WHERE o.order_no = ?",
                (order_no,),
            ).fetchone()
            conn.commit()
            credit_balances = {k: 0 for k in AI_CREDIT_KINDS}
            for row in balances:
                kind = _normalize_credit_kind(row["kind"])
                if kind:
                    credit_balances[kind] = max(0, int(row["bal"] or 0))
            return {
                "order": row_to_dict(updated_order),
                "subscription": None,
                "credits": credit_balances,
            }

        starts_at = utc_now()
        current_membership = conn.execute(
            """
            SELECT expires_at
            FROM subscriptions
            WHERE user_id = ? AND status = 'active'
            ORDER BY expires_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (order["user_id"],),
        ).fetchone()
        if current_membership is not None:
            current_expires = _parse_utc(current_membership["expires_at"] or "")
            if current_expires is not None and current_expires > starts_at:
                starts_at = current_expires
        expires_at = starts_at + _months_delta(int(order["interval_months"] or 1))

        conn.execute(
            """
            UPDATE orders
            SET status = 'paid',
                payment_provider = ?,
                payment_reference = ?,
                notes = ?,
                paid_at = ?
            WHERE order_no = ? AND status = 'pending'
            """,
            (provider, payment_reference, notes, paid_at, order_no),
        )
        conn.execute(
            """
            INSERT INTO subscriptions(
                user_id, plan_code, status, source, starts_at, expires_at, notes, created_at, updated_at
            )
            VALUES(?, ?, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                order["user_id"],
                order["plan_code"],
                source,
                starts_at.isoformat(timespec="seconds"),
                expires_at.isoformat(timespec="seconds"),
                notes,
                paid_at,
                paid_at,
            ),
        )
        updated_order = conn.execute(
            """
            SELECT o.*, p.name AS plan_name, p.interval_months
            FROM orders o
            JOIN plans p ON p.code = o.plan_code
            WHERE o.order_no = ?
            """,
            (order_no,),
        ).fetchone()
        subscription = conn.execute(
            """
            SELECT s.*, p.name AS plan_name, p.interval_months
            FROM subscriptions s
            JOIN plans p ON p.code = s.plan_code
            WHERE s.user_id = ? AND s.plan_code = ?
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT 1
            """,
            (order["user_id"], order["plan_code"]),
        ).fetchone()
        conn.commit()
    # 本请求刚为该用户写入了新订阅：清掉请求级会员快照缓存，避免同请求后续读到旧的「非会员」状态。
    _invalidate_request_membership_cache()
    # 灾备：把这笔会员开通/续费即时追加到导出账本，供本地每分钟增量拉取（站点被封时可凭此即时迁移）。
    _append_member_export(
        {
            "ts": paid_at,
            "event": "membership_paid",
            "order_no": order_no,
            "source": source,
            "user": get_user_by_id(int(order["user_id"])) or {},
            "subscription": row_to_dict(subscription) or {},
            "order": row_to_dict(updated_order) or {},
        }
    )
    return {
        "order": row_to_dict(updated_order),
        "subscription": row_to_dict(subscription),
    }


def create_manual_subscription(*, user_email: str, plan_code: str, note: str = "") -> dict:
    user = get_user_by_email(user_email)
    if user is None:
        raise ValueError("用户不存在")
    order = create_pending_order(user_id=int(user["id"]), plan_code=plan_code)
    result = mark_order_paid(
        order_no=order["order_no"],
        provider="manual",
        payment_reference="manual-grant",
        notes=note,
        source="manual",
    )
    return {
        "user": user,
        "order": result["order"],
        "subscription": result["subscription"],
    }


def list_payment_events(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT pe.*, o.user_id, o.plan_code, u.email AS user_email
            FROM payment_events pe
            LEFT JOIN orders o ON o.order_no = pe.order_no
            LEFT JOIN users u ON u.id = o.user_id
            ORDER BY pe.created_at DESC, pe.id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def record_payment_event(*, order_no: str, provider: str, event_type: str, payload: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO payment_events(order_no, provider, event_type, payload_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (order_no, provider, event_type, json.dumps(payload, ensure_ascii=False), utc_now_text()),
        )
        conn.commit()


def record_site_activity(
    *,
    session_key: str,
    user_id: int | None,
    day: str,
    feature: str,
    path: str = "",
) -> None:
    now = utc_now_text()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO site_activity(
                session_key, user_id, day, feature, path, request_count, created_at, last_seen_at
            )
            VALUES(?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(session_key, day, feature) DO UPDATE SET
                user_id=COALESCE(excluded.user_id, site_activity.user_id),
                path=excluded.path,
                request_count=site_activity.request_count + 1,
                last_seen_at=excluded.last_seen_at
            """,
            (
                (session_key or "").strip() or "anonymous",
                user_id,
                (day or china_day_text()).strip(),
                (feature or "site").strip()[:40],
                (path or "").strip()[:240],
                now,
                now,
            ),
        )
        conn.commit()


def _online_bucket_start(at: datetime | None = None) -> str:
    """把时间点向下取整到 15 分钟时槽起点（UTC ISO），作为在线变化图的横轴刻度。"""
    now = at or utc_now()
    floored = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return floored.isoformat(timespec="seconds")


def record_online_presence(
    *,
    session_key: str,
    user_id: int | None,
    at: datetime | None = None,
) -> None:
    """按 15 分钟时槽记录在线访问者，用于 24 小时在线变化图。

    同一访客在同一时槽内只占一行（按 session_key 去重）；登录后 user_id 经
    COALESCE 落到该行，使该时槽内由访客变为注册/会员身份，与总览去重口径一致。
    """
    bucket = _online_bucket_start(at)
    seen = (at or utc_now()).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO online_presence(bucket_start, session_key, user_id, last_seen_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(bucket_start, session_key) DO UPDATE SET
                user_id=COALESCE(excluded.user_id, online_presence.user_id),
                last_seen_at=excluded.last_seen_at
            """,
            (bucket, (session_key or "").strip() or "anonymous", user_id, seen),
        )
        conn.commit()


def prune_online_presence(*, keep_hours: int = 48) -> None:
    cutoff = utc_now() - timedelta(hours=max(1, int(keep_hours)))
    with _connect() as conn:
        conn.execute(
            "DELETE FROM online_presence WHERE bucket_start < ?",
            (cutoff.isoformat(timespec="seconds"),),
        )
        conn.commit()


def get_online_presence_series(
    *,
    since_text: str,
    until_text: str,
    member_as_of: str,
) -> dict[str, dict]:
    """返回 [since, until] 区间内每个 15 分钟时槽的在线人数，按时槽起点(UTC ISO)索引。

    - total：所有访问者（含访客），登录用户按账号去重、访客按会话去重；
    - registered：去重后的注册用户数（user_id 非空）；
    - members：去重后、在 member_as_of 时点仍为有效会员的用户数。
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                bucket_start,
                COUNT(DISTINCT CASE
                    WHEN user_id IS NOT NULL THEN 'u:' || user_id
                    ELSE 's:' || session_key
                END) AS total,
                COUNT(DISTINCT user_id) AS registered,
                COUNT(DISTINCT CASE WHEN user_id IN (
                    SELECT user_id FROM subscriptions
                    WHERE status = 'active' AND starts_at < ? AND expires_at > ?
                ) THEN user_id END) AS members
            FROM online_presence
            WHERE bucket_start >= ? AND bucket_start <= ?
            GROUP BY bucket_start
            """,
            (member_as_of, member_as_of, since_text, until_text),
        ).fetchall()
    return {str(row["bucket_start"]): (row_to_dict(row) or {}) for row in rows}


def _reader_actor_key(*, user_id: int | None, client_ip: str, session_key: str) -> tuple[str, str]:
    if user_id:
        return f"user:{int(user_id)}", "user"
    ip = (client_ip or "").strip()
    if ip:
        return f"ip:{ip}", "ip"
    return f"session:{(session_key or '').strip() or 'anonymous'}", "session"


def record_reader_access_event(
    *,
    session_key: str,
    user_id: int | None,
    email: str = "",
    client_ip: str = "",
    user_agent: str = "",
    endpoint: str = "",
    method: str = "",
    path: str = "",
    reader_mode: str = "",
    source_file: str = "",
    page: int = 0,
    is_rate_limited: bool = False,
    day: str | None = None,
) -> None:
    actor_key, actor_type = _reader_actor_key(user_id=user_id, client_ip=client_ip, session_key=session_key)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO reader_access_events(
                day, actor_key, actor_type, session_key, user_id, email, client_ip,
                user_agent, endpoint, method, path, reader_mode, source_file, page,
                is_rate_limited, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (day or china_day_text()).strip(),
                actor_key,
                actor_type,
                (session_key or "").strip()[:120],
                user_id,
                normalize_email(email),
                (client_ip or "").strip()[:80],
                (user_agent or "").strip()[:500],
                (endpoint or "").strip()[:80],
                (method or "").strip()[:12],
                (path or "").strip()[:500],
                (reader_mode or "").strip()[:40],
                (source_file or "").strip()[:240],
                max(0, int(page or 0)),
                1 if is_rate_limited else 0,
                utc_now_text(),
            ),
        )
        conn.commit()


def prune_reader_access_events(*, keep_days: int = 30) -> None:
    cutoff = utc_now() - timedelta(days=max(1, int(keep_days)))
    with _connect() as conn:
        conn.execute("DELETE FROM reader_access_events WHERE created_at < ?", (cutoff.isoformat(timespec="seconds"),))
        conn.commit()


def _max_consecutive_page_run(pages: list[tuple[str, int]]) -> int:
    best = 0
    current_source = ""
    previous_page = -100
    current = 0
    for source_file, page in pages:
        if not source_file or page <= 0:
            continue
        if source_file == current_source and page == previous_page + 1:
            current += 1
        else:
            current_source = source_file
            current = 1
        previous_page = page
        best = max(best, current)
    return best


def _reader_anomaly_reasons(item: dict) -> list[str]:
    reasons: list[str] = []
    request_count = int(item.get("request_count") or 0)
    page_image_count = int(item.get("page_image_count") or 0)
    page_count = int(item.get("page_count") or 0)
    volume_count = int(item.get("volume_count") or 0)
    max_minute_requests = int(item.get("max_minute_requests") or 0)
    sequential_pages = int(item.get("max_consecutive_pages") or 0)
    limited_count = int(item.get("limited_count") or 0)
    ua = str(item.get("user_agent") or "").lower()
    if request_count >= 300:
        reasons.append(f"当日阅读请求 {request_count} 次")
    if page_image_count >= 200:
        reasons.append(f"书页图像 {page_image_count} 次")
    if max_minute_requests >= 90:
        reasons.append(f"单分钟最高 {max_minute_requests} 次")
    if page_count >= 120:
        reasons.append(f"访问 {page_count} 个页面")
    if volume_count >= 8:
        reasons.append(f"跨 {volume_count} 个卷册")
    if sequential_pages >= 60:
        reasons.append(f"连续翻页 {sequential_pages} 页")
    if limited_count:
        reasons.append(f"触发限速 {limited_count} 次")
    if any(marker in ua for marker in ("bot", "spider", "crawler", "scrapy", "python-requests", "curl", "wget")):
        reasons.append("疑似自动化 User-Agent")
    return reasons


def list_reader_anomaly_visitors(*, day: str, limit: int = 30) -> list[dict]:
    day_value = (day or china_day_text()).strip()
    with _connect() as conn:
        rows = conn.execute(
            """
            WITH minute_counts AS (
                SELECT actor_key, COUNT(*) AS minute_count
                FROM reader_access_events
                WHERE day = ?
                GROUP BY actor_key, substr(created_at, 1, 16)
            ),
            minute_max AS (
                SELECT actor_key, MAX(minute_count) AS max_minute_requests
                FROM minute_counts
                GROUP BY actor_key
            )
            SELECT
                e.actor_key,
                MAX(e.actor_type) AS actor_type,
                MAX(e.user_id) AS user_id,
                MAX(e.email) AS email,
                MAX(e.client_ip) AS client_ip,
                MAX(e.user_agent) AS user_agent,
                COUNT(*) AS request_count,
                SUM(CASE WHEN e.endpoint = 'page_image' THEN 1 ELSE 0 END) AS page_image_count,
                COUNT(DISTINCT CASE WHEN e.source_file != '' THEN e.source_file END) AS volume_count,
                COUNT(DISTINCT CASE WHEN e.source_file != '' AND e.page > 0 THEN e.source_file || ':' || e.page END) AS page_count,
                SUM(CASE WHEN e.is_rate_limited = 1 THEN 1 ELSE 0 END) AS limited_count,
                MIN(e.created_at) AS first_seen_at,
                MAX(e.created_at) AS last_seen_at,
                COALESCE(MAX(m.max_minute_requests), 0) AS max_minute_requests
            FROM reader_access_events e
            LEFT JOIN minute_max m ON m.actor_key = e.actor_key
            WHERE e.day = ?
            GROUP BY e.actor_key
            ORDER BY request_count DESC, page_image_count DESC
            LIMIT ?
            """,
            (day_value, day_value, max(1, int(limit) * 4)),
        ).fetchall()
        items = [row_to_dict(row) for row in rows]
        for item in items:
            page_rows = conn.execute(
                """
                SELECT DISTINCT source_file, page
                FROM reader_access_events
                WHERE day = ? AND actor_key = ? AND source_file != '' AND page > 0
                ORDER BY source_file ASC, page ASC
                """,
                (day_value, item.get("actor_key") or ""),
            ).fetchall()
            item["max_consecutive_pages"] = _max_consecutive_page_run(
                [(str(row["source_file"] or ""), int(row["page"] or 0)) for row in page_rows]
            )
            reasons = _reader_anomaly_reasons(item)
            item["alert_reason"] = "；".join(reasons)
            item["is_anomaly"] = bool(reasons)
    anomalies = [item for item in items if item.get("is_anomaly")]
    return anomalies[: max(1, int(limit))]


def list_reader_access_events(*, actor_key: str, day: str | None = None, limit: int = 120) -> list[dict]:
    params: list[object] = [(actor_key or "").strip()]
    where = "WHERE actor_key = ?"
    if day:
        where += " AND day = ?"
        params.append(day.strip())
    params.append(max(1, int(limit)))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, day, actor_key, actor_type, session_key, user_id, email, client_ip,
                   user_agent, endpoint, method, path, reader_mode, source_file, page,
                   is_rate_limited, created_at
            FROM reader_access_events
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def record_ai_usage(
    *,
    user_id: int | None,
    session_key: str = "",
    day: str | None = None,
    feature: str = "",
    provider: str = "",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    estimated: bool = True,
    success: bool = True,
    error: str = "",
    prompt_excerpt: str = "",
    client_ip: str = "",
    source_ref: str = "",
) -> None:
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    total = prompt + completion if total_tokens is None else max(0, int(total_tokens or 0))
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO ai_usage(
                user_id, session_key, day, feature, provider, model, prompt_tokens,
                completion_tokens, total_tokens, estimated, success, error, created_at,
                prompt_excerpt, client_ip, source_ref
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                (session_key or "").strip(),
                (day or china_day_text()).strip(),
                (feature or "").strip()[:40],
                (provider or "").strip()[:80],
                (model or "").strip()[:160],
                prompt,
                completion,
                total,
                1 if estimated else 0,
                1 if success else 0,
                (error or "").strip()[:500],
                utc_now_text(),
                (prompt_excerpt or "").strip()[:4000],
                (client_ip or "").strip()[:80],
                (source_ref or "").strip()[:300],
            ),
        )
        conn.commit()


def get_ai_token_usage(
    *,
    day: str | None = None,
    user_id: int | None = None,
    session_key: str = "",
    provider: str = "",
) -> int:
    """当日估算 token 用量合计。provider 非空时仅统计该通道（如 \"zhipu\"），用于通道级子配额。"""
    day_value = (day or china_day_text()).strip()
    provider_value = (provider or "").strip()
    where = "WHERE day = ?"
    params: list[object] = [day_value]
    if user_id:
        where += " AND user_id = ?"
        params.append(int(user_id))
    else:
        where += " AND session_key = ?"
        params.append((session_key or "").strip())
    if provider_value:
        where += " AND provider = ?"
        params.append(provider_value)
    with _connect() as conn:
        value = conn.execute(
            f"SELECT COALESCE(SUM(total_tokens), 0) FROM ai_usage {where}",
            tuple(params),
        ).fetchone()[0]
    return int(value or 0)


def get_ai_token_usage_range(
    *,
    start_day: str,
    end_day: str,
    user_id: int | None = None,
    session_key: str = "",
    provider: str = "",
) -> int:
    """日期区间内估算 token 用量合计（day 为 YYYY-MM-DD 文本，按字典序闭区间）。

    用于「每周」额度统计：每日额度是软上限，本周累计封顶才是硬上限（弹性借用）。
    """
    start_value = (start_day or "").strip()
    end_value = (end_day or "").strip()
    if not start_value or not end_value:
        return 0
    where = "WHERE day >= ? AND day <= ?"
    params: list[object] = [start_value, end_value]
    if user_id:
        where += " AND user_id = ?"
        params.append(int(user_id))
    else:
        where += " AND session_key = ?"
        params.append((session_key or "").strip())
    if (provider or "").strip():
        where += " AND provider = ?"
        params.append((provider or "").strip())
    with _connect() as conn:
        value = conn.execute(
            f"SELECT COALESCE(SUM(total_tokens), 0) FROM ai_usage {where}",
            tuple(params),
        ).fetchone()[0]
    return int(value or 0)


def list_ai_usage_for_user(user_id: int | None, *, day: str | None = None, limit: int = 80) -> list[dict]:
    """返回某注册用户最近的 AI 请求明细，用于后台核查异常用量时了解具体输入与来源。

    day 为空时跨日期返回最近若干条；给定日期时仅返回当日记录。包含真实输入摘要、
    实际命中的 provider/model（可据此判断是否被转接到其它接口）、token、成败与客户端 IP。
    """
    if not user_id:
        return []
    params: list[object] = [int(user_id)]
    where = "WHERE user_id = ?"
    if day:
        where += " AND day = ?"
        params.append(day.strip())
    params.append(max(1, int(limit)))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, day, feature, provider, model, prompt_tokens, completion_tokens,
                   total_tokens, estimated, success, error, created_at,
                   prompt_excerpt, client_ip, source_ref, session_key
            FROM ai_usage
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def count_ai_usage_requests(
    *,
    user_id: int | None,
    session_key: str = "",
    start_day: str,
    end_day: str,
    feature: str = "",
    success_only: bool = True,
) -> int:
    """Count AI request rows in a China-date day range.

    Day values are stored as YYYY-MM-DD text, so lexical range checks are stable.
    """
    start_value = (start_day or "").strip()
    end_value = (end_day or "").strip()
    if not start_value or not end_value:
        return 0
    params: list[object] = [start_value, end_value]
    where = "WHERE day >= ? AND day <= ?"
    if user_id:
        where += " AND user_id = ?"
        params.append(int(user_id))
    else:
        where += " AND session_key = ?"
        params.append((session_key or "").strip())
    if feature:
        where += " AND feature = ?"
        params.append((feature or "").strip()[:40])
    if success_only:
        where += " AND success = 1"
    with _connect() as conn:
        value = conn.execute(
            f"SELECT COUNT(*) FROM ai_usage {where}",
            tuple(params),
        ).fetchone()[0]
    return int(value or 0)


# --- 资源包：AI 次数台账（研究级检索 / 随心问 / AI 导学问答）---------------------
# reader = 阅读器「AI 导学问答」次数，仅 DeepSeek-V4-Pro 主通道消耗（智谱联网通道不抵扣）。
AI_CREDIT_KINDS = ("research", "chat", "reader")


def _normalize_credit_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    return value if value in AI_CREDIT_KINDS else ""


def get_ai_credit_balances(user_id: int | None) -> dict[str, int]:
    """返回 {'research': n, 'chat': m}，按 (user, kind) 对台账 delta 求和。"""
    balances = {k: 0 for k in AI_CREDIT_KINDS}
    if not user_id:
        return balances
    with _connect() as conn:
        rows = conn.execute(
            "SELECT kind, COALESCE(SUM(delta), 0) AS bal FROM ai_credit_ledger WHERE user_id = ? GROUP BY kind",
            (int(user_id),),
        ).fetchall()
    for row in rows:
        kind = _normalize_credit_kind(row["kind"])
        if kind:
            balances[kind] = max(0, int(row["bal"] or 0))
    return balances


def get_ai_credit_balance(user_id: int | None, kind: str) -> int:
    kind = _normalize_credit_kind(kind)
    if not user_id or not kind:
        return 0
    return int(get_ai_credit_balances(user_id).get(kind, 0))


def grant_ai_credits(
    user_id: int,
    *,
    research: int = 0,
    chat: int = 0,
    reader: int = 0,
    reason: str = "",
    order_no: str = "",
) -> dict[str, int]:
    """发放次数（购买/管理员补偿）。research/chat/reader 为正整数增量；返回发放后的余额。"""
    research = max(0, int(research or 0))
    chat = max(0, int(chat or 0))
    reader = max(0, int(reader or 0))
    now = utc_now_text()
    rows = []
    if research:
        rows.append((int(user_id), "research", research, (reason or "grant")[:80], (order_no or "")[:64], now))
    if chat:
        rows.append((int(user_id), "chat", chat, (reason or "grant")[:80], (order_no or "")[:64], now))
    if reader:
        rows.append((int(user_id), "reader", reader, (reason or "grant")[:80], (order_no or "")[:64], now))
    if rows:
        with _connect() as conn:
            conn.executemany(
                "INSERT INTO ai_credit_ledger(user_id, kind, delta, reason, order_no, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
    return get_ai_credit_balances(user_id)


def consume_ai_credit(user_id: int | None, kind: str, *, reason: str = "", order_no: str = "") -> bool:
    """原子扣 1 次：余额>0 才扣，返回是否扣成功。并发安全（BEGIN IMMEDIATE + 复核）。"""
    kind = _normalize_credit_kind(kind)
    if not user_id or not kind:
        return False
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        bal = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM ai_credit_ledger WHERE user_id = ? AND kind = ?",
            (int(user_id), kind),
        ).fetchone()[0]
        if int(bal or 0) <= 0:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO ai_credit_ledger(user_id, kind, delta, reason, order_no, created_at) "
            "VALUES(?, ?, -1, ?, ?, ?)",
            (int(user_id), kind, (reason or "consume")[:80], (order_no or "")[:64], utc_now_text()),
        )
        conn.commit()
    return True


def get_user_ai_limit(user_id: int | None) -> dict:
    if not user_id:
        return {
            "limit": None,
            "source": "default",
            "plan_code": "",
            "plan_name": "",
            "user_override": None,
            "plan_limit": None,
        }
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                u.daily_ai_token_limit_override AS user_override,
                s.plan_code,
                p.name AS plan_name,
                p.daily_ai_token_limit AS plan_limit
            FROM users u
            LEFT JOIN subscriptions s
                ON s.id = (
                    SELECT s2.id
                    FROM subscriptions s2
                    WHERE s2.user_id = u.id
                      AND s2.status = 'active'
                      AND s2.expires_at > ?
                    ORDER BY s2.expires_at DESC, s2.created_at DESC, s2.id DESC
                    LIMIT 1
                )
            LEFT JOIN plans p ON p.code = s.plan_code
            WHERE u.id = ?
            """,
            (utc_now_text(), int(user_id)),
        ).fetchone()
    data = row_to_dict(row) or {}
    user_override = data.get("user_override")
    plan_limit = data.get("plan_limit")
    if user_override is not None:
        limit = int(user_override)
        source = "user"
    elif plan_limit is not None:
        limit = int(plan_limit)
        source = "plan"
    else:
        limit = None
        source = "default"
    return {
        "limit": limit,
        "source": source,
        "plan_code": data.get("plan_code") or "",
        "plan_name": data.get("plan_name") or "",
        "user_override": None if user_override is None else int(user_override),
        "plan_limit": None if plan_limit is None else int(plan_limit),
    }


def get_user_zhipu_limit(user_id: int | None) -> int | None:
    """当前用户的智谱每日 token 子配额（套餐级）。

    返回套餐的 daily_zhipu_token_limit：None＝该套餐未单设（或无套餐/未登录），
    调用方回退到智能服务里的全局默认；0＝该套餐不限。
    """
    if not user_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT p.daily_zhipu_token_limit AS plan_limit
            FROM subscriptions s
            JOIN plans p ON p.code = s.plan_code
            WHERE s.user_id = ?
              AND s.status = 'active'
              AND s.expires_at > ?
            ORDER BY s.expires_at DESC, s.created_at DESC, s.id DESC
            LIMIT 1
            """,
            (int(user_id), utc_now_text()),
        ).fetchone()
    if row is None or row["plan_limit"] is None:
        return None
    return int(row["plan_limit"])


def get_admin_dashboard_first_day() -> str:
    candidates: list[str] = []
    with _connect() as conn:
        for table_name in ("site_activity", "ai_usage"):
            try:
                value = conn.execute(f"SELECT MIN(day) FROM {table_name}").fetchone()[0]
            except sqlite3.OperationalError:
                value = None
            if value:
                candidates.append(str(value))
        timestamp_sources = (
            ("users", "created_at"),
            ("orders", "created_at"),
            ("orders", "paid_at"),
            ("subscriptions", "created_at"),
            ("payment_events", "created_at"),
            ("journal_subscriptions", "created_at"),
            ("journal_articles", "first_seen_at"),
            ("journal_delivery_logs", "created_at"),
        )
        for table_name, column_name in timestamp_sources:
            try:
                value = conn.execute(
                    f"SELECT MIN({column_name}) FROM {table_name} WHERE {column_name} != ''"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                value = None
            parsed = _parse_utc(str(value or ""))
            if parsed:
                candidates.append(china_day_text(parsed))
    valid = [day for day in candidates if len(day) == 10 and day[4] == "-" and day[7] == "-"]
    return min(valid) if valid else china_day_text()


def get_admin_dashboard_metrics(
    *,
    day: str,
    start_at: str,
    end_at: str,
    online_since: str,
    now_text: str,
    high_token_threshold: int = 50000,
    token_limit_ratio: float = 0.8,
) -> dict:
    with _connect() as conn:
        def scalar(sql: str, params: tuple = ()) -> int:
            return int(conn.execute(sql, params).fetchone()[0] or 0)

        member_as_of = now_text if start_at <= now_text < end_at else end_at
        metrics = {
            "total_users": scalar("SELECT COUNT(*) FROM users"),
            "active_accounts": scalar("SELECT COUNT(*) FROM users WHERE is_active = 1"),
            "verified_users": scalar("SELECT COUNT(*) FROM users WHERE email_verified_at != ''"),
            "disabled_users": scalar(
                "SELECT COUNT(*) FROM users WHERE is_active = 0 OR deactivated_at != ''"
            ),
            # 在线去重：先把“同一会话曾登录过的访客行”归并到其账号，避免登录前后被
            # 算成两个访问者（访客一次 + 注册一次）。再按账号/会话去重计数。
            "current_online": scalar(
                """
                SELECT COUNT(DISTINCT CASE
                    WHEN COALESCE(sa.user_id, m.user_id) IS NOT NULL
                        THEN 'u:' || COALESCE(sa.user_id, m.user_id)
                    ELSE 's:' || sa.session_key
                END)
                FROM site_activity sa
                LEFT JOIN (
                    SELECT session_key, MAX(user_id) AS user_id
                    FROM site_activity
                    WHERE user_id IS NOT NULL
                    GROUP BY session_key
                ) m ON m.session_key = sa.session_key
                WHERE sa.last_seen_at >= ?
                """,
                (online_since,),
            ),
            "today_online": scalar(
                """
                SELECT COUNT(DISTINCT CASE
                    WHEN COALESCE(sa.user_id, m.user_id) IS NOT NULL
                        THEN 'u:' || COALESCE(sa.user_id, m.user_id)
                    ELSE 's:' || sa.session_key
                END)
                FROM site_activity sa
                LEFT JOIN (
                    SELECT session_key, MAX(user_id) AS user_id
                    FROM site_activity
                    WHERE user_id IS NOT NULL AND day = ?
                    GROUP BY session_key
                ) m ON m.session_key = sa.session_key
                WHERE sa.day = ?
                """,
                (day, day),
            ),
            "registered_online_today": scalar(
                "SELECT COUNT(DISTINCT user_id) FROM site_activity WHERE day = ? AND user_id IS NOT NULL",
                (day,),
            ),
            "new_users_today": scalar(
                "SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?",
                (start_at, end_at),
            ),
            "active_members": scalar(
                """
                SELECT COUNT(DISTINCT user_id)
                FROM subscriptions
                WHERE status = 'active'
                  AND starts_at < ?
                  AND expires_at > ?
                """,
                (member_as_of, member_as_of),
            ),
            # 今日上线的会员：当日在 site_activity 有记录、且在 member_as_of 时点仍为有效会员的注册用户。
            "member_online_today": scalar(
                """
                SELECT COUNT(DISTINCT sa.user_id)
                FROM site_activity sa
                WHERE sa.day = ?
                  AND sa.user_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM subscriptions s
                      WHERE s.user_id = sa.user_id
                        AND s.status = 'active'
                        AND s.starts_at < ?
                        AND s.expires_at > ?
                  )
                """,
                (day, member_as_of, member_as_of),
            ),
            "paid_today_cents": scalar(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM orders WHERE status = 'paid' AND paid_at >= ? AND paid_at < ?",
                (start_at, end_at),
            ),
            "pending_orders": scalar("SELECT COUNT(*) FROM orders WHERE status = 'pending'"),
            "payment_errors_today": scalar(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE status IN ('failed', 'cancelled', 'expired')
                  AND created_at >= ?
                  AND created_at < ?
                """,
                (start_at, end_at),
            ),
            "searches_today": scalar(
                "SELECT COALESCE(SUM(request_count), 0) FROM site_activity WHERE day = ? AND feature = 'search'",
                (day,),
            ),
            "reader_views_today": scalar(
                "SELECT COALESCE(SUM(request_count), 0) FROM site_activity WHERE day = ? AND feature = 'reader'",
                (day,),
            ),
            "ai_requests_today": scalar("SELECT COUNT(*) FROM ai_usage WHERE day = ?", (day,)),
            "ai_tokens_today": scalar(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM ai_usage WHERE day = ?",
                (day,),
            ),
            "ai_errors_today": scalar(
                "SELECT COUNT(*) FROM ai_usage WHERE day = ? AND success = 0",
                (day,),
            ),
            # 改动观察指标：定价页访问（含期刊卡来源）与智谱通道用量，便于按天对比转化路径。
            "pricing_views_today": scalar(
                "SELECT COALESCE(SUM(request_count), 0) FROM site_activity WHERE day = ? AND feature IN ('pricing', 'pricing_journal')",
                (day,),
            ),
            "pricing_from_journal_today": scalar(
                "SELECT COALESCE(SUM(request_count), 0) FROM site_activity WHERE day = ? AND feature = 'pricing_journal'",
                (day,),
            ),
            "zhipu_requests_today": scalar(
                "SELECT COUNT(*) FROM ai_usage WHERE day = ? AND provider = 'zhipu'",
                (day,),
            ),
            "zhipu_tokens_today": scalar(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM ai_usage WHERE day = ? AND provider = 'zhipu'",
                (day,),
            ),
        }
        token_rows = conn.execute(
            """
            SELECT
                u.id AS user_id,
                u.email,
                u.display_name,
                u.daily_ai_token_limit_override AS user_limit,
                p.daily_ai_token_limit AS plan_limit,
                p.name AS plan_name,
                COUNT(a.id) AS request_count,
                COALESCE(SUM(a.total_tokens), 0) AS total_tokens,
                COALESCE(MAX(a.total_tokens), 0) AS max_request_tokens,
                SUM(CASE WHEN a.success = 0 THEN 1 ELSE 0 END) AS error_count,
                MIN(a.created_at) AS first_used_at,
                MAX(a.created_at) AS last_used_at
            FROM ai_usage a
            JOIN users u ON u.id = a.user_id
            LEFT JOIN subscriptions s
                ON s.id = (
                    SELECT s2.id
                    FROM subscriptions s2
                    WHERE s2.user_id = u.id
                      AND s2.status = 'active'
                      AND s2.starts_at < ?
                      AND s2.expires_at > ?
                    ORDER BY s2.expires_at DESC, s2.created_at DESC, s2.id DESC
                    LIMIT 1
                )
            LEFT JOIN plans p ON p.code = s.plan_code
            WHERE a.day = ?
            GROUP BY u.id, u.email, u.display_name, u.daily_ai_token_limit_override, p.daily_ai_token_limit, p.name
            ORDER BY total_tokens DESC, request_count DESC, u.id ASC
            LIMIT 20
            """,
            (member_as_of, member_as_of, day),
        ).fetchall()
        plan_rows = conn.execute(
            """
            SELECT p.code, p.name, COUNT(DISTINCT s.user_id) AS active_count
            FROM plans p
            LEFT JOIN subscriptions s
                ON s.plan_code = p.code
               AND s.status = 'active'
               AND s.starts_at < ?
               AND s.expires_at > ?
            GROUP BY p.code, p.name
            ORDER BY p.sort_order ASC, p.code ASC
            """,
            (member_as_of, member_as_of),
        ).fetchall()
        try:
            metrics.update(
                {
                    # 已确认订阅写入的状态是 'active'（见 journal_alerts.confirm 流程）。
                    "journal_subscriptions": scalar(
                        "SELECT COUNT(*) FROM journal_subscriptions WHERE status = 'active'"
                    ),
                    "journal_pending_articles": scalar(
                        "SELECT COUNT(*) FROM journal_articles WHERE status = 'pending_review'"
                    ),
                    "journal_ready_articles": scalar(
                        "SELECT COUNT(*) FROM journal_articles WHERE status = 'ready'"
                    ),
                    # 近期发送：综述群发记录（新）+ 旧版逐文章投递记录（兼容历史数据）。
                    "journal_recent_sends": scalar(
                        "SELECT (SELECT COUNT(*) FROM journal_digest_deliveries "
                        "        WHERE status = 'sent' AND created_at >= ? AND created_at < ?) "
                        "     + (SELECT COUNT(*) FROM journal_delivery_logs "
                        "        WHERE created_at >= ? AND created_at < ?)",
                        (start_at, end_at, start_at, end_at),
                    ),
                }
            )
        except sqlite3.OperationalError:
            metrics.update(
                {
                    "journal_subscriptions": 0,
                    "journal_pending_articles": 0,
                    "journal_ready_articles": 0,
                    "journal_recent_sends": 0,
                }
            )
    metrics["active_members_by_plan"] = [row_to_dict(row) for row in plan_rows]
    top_token_users: list[dict] = []
    high_token_users: list[dict] = []
    for row in token_rows:
        item = row_to_dict(row) or {}
        user_limit = item.get("user_limit")
        plan_limit = item.get("plan_limit")
        effective_limit = user_limit if user_limit is not None else plan_limit
        total_tokens = int(item.get("total_tokens") or 0)
        item["effective_limit"] = None if effective_limit is None else int(effective_limit)
        item["limit_ratio"] = (
            None
            if effective_limit is None
            else round(total_tokens / max(1, int(effective_limit)), 3)
        )
        reasons: list[str] = []
        if total_tokens >= int(high_token_threshold):
            reasons.append(f"超过 {int(high_token_threshold)} token")
        if effective_limit is not None and total_tokens >= int(effective_limit):
            reasons.append("超过每日限额")
        elif effective_limit is not None and total_tokens >= int(int(effective_limit) * float(token_limit_ratio)):
            reasons.append(f"达到限额 {int(float(token_limit_ratio) * 100)}%")
        item["alert_reason"] = "；".join(reasons)
        top_token_users.append(item)
        if reasons:
            high_token_users.append(item)
    metrics["top_token_users"] = top_token_users
    metrics["high_token_users"] = high_token_users
    metrics["high_token_user_count"] = len(high_token_users)
    metrics["high_token_threshold"] = int(high_token_threshold)
    metrics["token_limit_ratio"] = float(token_limit_ratio)
    metrics["member_as_of"] = member_as_of
    reader_anomalies = list_reader_anomaly_visitors(day=day, limit=20)
    metrics["reader_anomaly_visitors"] = reader_anomalies
    metrics["reader_anomaly_count"] = len(reader_anomalies)
    return metrics
