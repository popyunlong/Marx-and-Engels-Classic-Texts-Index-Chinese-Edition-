from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_env import APP_VERSION, APPDATA_DIR, secure_db_file


DB_PATH = APPDATA_DIR / "membership.sqlite3"


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    secure_db_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def init_admin_store_db() -> Path:
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
    return DB_PATH


def get_setting(key: str, default: Any = None) -> Any:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value_json FROM unified_settings WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return default
    return _json_loads(str(row["value_json"] or ""), default)


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


def delete_setting(key: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM unified_settings WHERE key = ?", (key,))
        conn.commit()


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
