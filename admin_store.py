from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_env import APP_VERSION, APPDATA_DIR, secure_db_file


DB_PATH = APPDATA_DIR / "membership.sqlite3"

# ── 设置项读缓存 ────────────────────────────────────────────────────────────────
# get_setting 原本每次都新开一条 SQLite 连接（实测 ~4.8ms/次）。它在请求热路径上被反复调用
# （如阅读端点每请求要查 reader_bans / blocked_bot_user_agents / monitoring_exemptions 等），
# 累计开销可观。unified_settings 都是「读多写少」的后台配置（管理员偶尔改），故这里加一层很短
# TTL 的进程内缓存：缓存原始 value_json 字符串、每次读仍 json.loads 出**全新对象**（保持调用方可
# 自由改返回值的既有约定）；本进程写入（set/delete_setting）即时失效该键；跨进程（备份/期刊等
# 旁路服务）改动最迟 TTL 秒后生效。TTL 默认 3s，可经 env 调整或设 0 关闭缓存（行为退回直连 DB）。
_SETTINGS_CACHE_TTL = max(0.0, float(os.environ.get("MARX_SETTINGS_CACHE_TTL", "3") or "3"))
_SETTINGS_CACHE: dict[str, tuple[float, str | None]] = {}
_SETTINGS_CACHE_LOCK = threading.Lock()


def _settings_cache_get(key: str) -> tuple[bool, str | None]:
    """返回 (命中?, value_json)。value_json 为 None 表示「该键确认不存在」（也缓存，避免反复查空）。"""
    if _SETTINGS_CACHE_TTL <= 0:
        return False, None
    now = time.monotonic()
    with _SETTINGS_CACHE_LOCK:
        hit = _SETTINGS_CACHE.get(key)
        if hit is not None and now - hit[0] < _SETTINGS_CACHE_TTL:
            return True, hit[1]
    return False, None


def _settings_cache_put(key: str, value_json: str | None) -> None:
    if _SETTINGS_CACHE_TTL <= 0:
        return
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE[key] = (time.monotonic(), value_json)


def _settings_cache_invalidate(key: str) -> None:
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE.pop(key, None)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_WAL_ENABLED = False


def _connect() -> sqlite3.Connection:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    secure_db_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 与 membership._connect 同库（membership.sqlite3），同样开 WAL 提升读写并发。WAL 是持久属性，
    # 任一模块先连上即全局生效；此处再设一次确保 admin_store 独立被导入时也能开启。失败静默回退。
    global _WAL_ENABLED
    if not _WAL_ENABLED:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            _WAL_ENABLED = True
        except sqlite3.Error:
            pass
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return default


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_activation_code() -> str:
    raw = secrets.token_hex(8).upper()
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


_SCHEMA_READY = False


def init_admin_store_db() -> Path:
    # 建表是幂等的（CREATE TABLE IF NOT EXISTS），但每次都新开连接 + executescript + mkdir 并不便宜。
    # load_access_policy 每次都会调它，而权限判定一个请求里要跑几十次 → 旧实现让首页等渲染白跑几十遍
    # 建表脚本（实测占首页渲染约 300ms）。这里加进程级「已就绪」标志：首次真正建表，之后直接返回。
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return DB_PATH
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS unified_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS desktop_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activation_code TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                user_email TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                last_sync_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revoked_at TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_desktop_devices_token_hash ON desktop_devices(token_hash);
            CREATE INDEX IF NOT EXISTS idx_desktop_devices_fingerprint ON desktop_devices(fingerprint);

            CREATE TABLE IF NOT EXISTS desktop_releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL DEFAULT 'stable',
                app_version TEXT NOT NULL,
                data_version TEXT NOT NULL DEFAULT '',
                download_url TEXT NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                force_update INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_desktop_releases_channel_active
                ON desktop_releases(channel, is_active, created_at DESC, id DESC);
            """
        )
        conn.commit()
    _SCHEMA_READY = True
    return DB_PATH


def get_setting(key: str, default: Any = None) -> Any:
    cached, value_json = _settings_cache_get(key)
    if not cached:
        with _connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM unified_settings WHERE key = ?",
                (key,),
            ).fetchone()
        value_json = str(row["value_json"]) if row is not None else None
        _settings_cache_put(key, value_json)
    if value_json is None:
        return default
    # 每次都 json.loads 出全新对象：调用方可放心原地修改返回值，不会污染缓存。
    return _json_loads(value_json, default)


def set_setting(key: str, value: Any, updated_by: str = "") -> None:
    now = utc_now_text()
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO unified_settings(key, value_json, updated_at, updated_by)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at,
                updated_by=excluded.updated_by
            """,
            (key, payload, now, updated_by),
        )
        conn.commit()
    _settings_cache_invalidate(key)


def delete_setting(key: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM unified_settings WHERE key = ?", (key,))
        conn.commit()
    _settings_cache_invalidate(key)


def list_desktop_devices(limit: int = 80) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, activation_code, fingerprint, label, user_email, status, expires_at,
                   notes, last_sync_at, created_at, updated_at, revoked_at,
                   CASE WHEN token_hash = '' THEN 0 ELSE 1 END AS activated
            FROM desktop_devices
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def create_desktop_device(
    *,
    label: str = "",
    user_email: str = "",
    expires_at: str = "",
    notes: str = "",
    activation_code: str = "",
) -> dict:
    now = utc_now_text()
    code = (activation_code or new_activation_code()).strip().upper()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO desktop_devices(
                activation_code, label, user_email, status, expires_at, notes, created_at, updated_at
            )
            VALUES(?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (code, label.strip(), user_email.strip().lower(), expires_at.strip(), notes.strip(), now, now),
        )
        row = conn.execute(
            "SELECT * FROM desktop_devices WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
    return _row_to_dict(row) or {}


def update_desktop_device(
    device_id: int,
    *,
    label: str,
    user_email: str,
    status: str,
    expires_at: str,
    notes: str,
) -> dict | None:
    normalized_status = status if status in {"active", "disabled", "revoked"} else "active"
    revoked_at = utc_now_text() if normalized_status == "revoked" else ""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE desktop_devices
            SET label = ?, user_email = ?, status = ?, expires_at = ?, notes = ?,
                revoked_at = CASE WHEN ? = 'revoked' THEN ? ELSE revoked_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                label.strip(),
                user_email.strip().lower(),
                normalized_status,
                expires_at.strip(),
                notes.strip(),
                normalized_status,
                revoked_at,
                utc_now_text(),
                int(device_id),
            ),
        )
        row = conn.execute("SELECT * FROM desktop_devices WHERE id = ?", (int(device_id),)).fetchone()
        conn.commit()
    return _row_to_dict(row)


def activate_desktop_device(*, activation_code: str, fingerprint: str, label: str = "") -> dict:
    code = activation_code.strip().upper()
    fingerprint = fingerprint.strip()
    if not code or not fingerprint:
        raise ValueError("activation_code and fingerprint are required")
    token = secrets.token_urlsafe(32)
    now = utc_now_text()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM desktop_devices WHERE activation_code = ?",
            (code,),
        ).fetchone()
        if row is None:
            raise ValueError("activation code not found")
        current = _row_to_dict(row) or {}
        if current.get("status") != "active":
            raise ValueError("device authorization is not active")
        saved_fingerprint = str(current.get("fingerprint") or "").strip()
        if saved_fingerprint and saved_fingerprint != fingerprint:
            raise ValueError("activation code is already bound to another device")
        conn.execute(
            """
            UPDATE desktop_devices
            SET token_hash = ?, fingerprint = ?, label = CASE WHEN label = '' THEN ? ELSE label END,
                last_sync_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (token_hash(token), fingerprint, label.strip(), now, now, int(current["id"])),
        )
        updated = conn.execute(
            "SELECT * FROM desktop_devices WHERE id = ?",
            (int(current["id"]),),
        ).fetchone()
        conn.commit()
    return {"token": token, "device": _row_to_dict(updated) or {}}


def get_device_by_token(token: str) -> dict | None:
    hashed = token_hash(token.strip())
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM desktop_devices WHERE token_hash = ?",
            (hashed,),
        ).fetchone()
    return _row_to_dict(row)


def touch_device_sync(device_id: int) -> None:
    now = utc_now_text()
    with _connect() as conn:
        conn.execute(
            "UPDATE desktop_devices SET last_sync_at = ?, updated_at = ? WHERE id = ?",
            (now, now, int(device_id)),
        )
        conn.commit()


def is_device_authorized(device: dict | None) -> bool:
    if not device:
        return False
    if str(device.get("status") or "") != "active":
        return False
    expires_at = str(device.get("expires_at") or "").strip()
    if not expires_at:
        return True
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)


def list_releases(limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, channel, app_version, data_version, download_url, sha256, size_bytes,
                   force_update, notes, is_active, created_at, updated_at
            FROM desktop_releases
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def create_release(
    *,
    channel: str = "stable",
    app_version: str,
    data_version: str = "",
    download_url: str,
    sha256: str = "",
    size_bytes: int = 0,
    force_update: bool = False,
    notes: str = "",
    is_active: bool = True,
) -> dict:
    if not app_version.strip():
        raise ValueError("app_version is required")
    if not download_url.strip():
        raise ValueError("download_url is required")
    now = utc_now_text()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO desktop_releases(
                channel, app_version, data_version, download_url, sha256, size_bytes,
                force_update, notes, is_active, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel.strip() or "stable",
                app_version.strip(),
                data_version.strip(),
                download_url.strip(),
                sha256.strip().lower(),
                max(0, int(size_bytes or 0)),
                1 if force_update else 0,
                notes.strip(),
                1 if is_active else 0,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM desktop_releases WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
    return _row_to_dict(row) or {}


def update_release_status(release_id: int, *, is_active: bool) -> dict | None:
    with _connect() as conn:
        conn.execute(
            "UPDATE desktop_releases SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if is_active else 0, utc_now_text(), int(release_id)),
        )
        row = conn.execute(
            "SELECT * FROM desktop_releases WHERE id = ?",
            (int(release_id),),
        ).fetchone()
        conn.commit()
    return _row_to_dict(row)


def latest_release(channel: str = "stable") -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, channel, app_version, data_version, download_url, sha256, size_bytes,
                   force_update, notes, is_active, created_at, updated_at
            FROM desktop_releases
            WHERE channel = ? AND is_active = 1
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            ((channel or "stable").strip(),),
        ).fetchone()
    release = _row_to_dict(row)
    if release:
        return release
    return {
        "channel": channel or "stable",
        "app_version": APP_VERSION,
        "data_version": "",
        "download_url": "",
        "sha256": "",
        "size_bytes": 0,
        "force_update": 0,
        "notes": "",
    }
