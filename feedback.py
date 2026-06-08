from __future__ import annotations

import sqlite3
from pathlib import Path

from membership import utc_now_text
from runtime_env import APPDATA_DIR


DB_PATH = APPDATA_DIR / "feedback.sqlite3"


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


def init_feedback_db() -> Path:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS feedback_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                user_email TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                unread_admin_count INTEGER NOT NULL DEFAULT 0,
                unread_user_count INTEGER NOT NULL DEFAULT 0,
                last_user_message_at TEXT NOT NULL DEFAULT '',
                last_admin_message_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL,
                author_role TEXT NOT NULL,
                author_user_id INTEGER,
                author_email TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                email_status TEXT NOT NULL DEFAULT '',
                email_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES feedback_threads(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS feedback_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                stored_name TEXT NOT NULL,
                mime TEXT NOT NULL DEFAULT '',
                byte_size INTEGER NOT NULL DEFAULT 0,
                original_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (message_id) REFERENCES feedback_messages(id) ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES feedback_threads(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_threads_updated
                ON feedback_threads(updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_feedback_messages_thread
                ON feedback_messages(thread_id, created_at ASC, id ASC);
            CREATE INDEX IF NOT EXISTS idx_feedback_attachments_message
                ON feedback_attachments(message_id, id ASC);
            """
        )
        conn.commit()
    return DB_PATH


def _message_attachments(conn: sqlite3.Connection, message_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, message_id, thread_id, mime, byte_size, original_name, created_at
        FROM feedback_attachments
        WHERE message_id = ?
        ORDER BY id ASC
        """,
        (int(message_id),),
    ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def _thread_from_row(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict | None:
    thread = _row_to_dict(row)
    if thread is None:
        return None
    messages = conn.execute(
        """
        SELECT *
        FROM feedback_messages
        WHERE thread_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (int(thread["id"]),),
    ).fetchall()
    message_dicts = []
    for message in messages:
        message_dict = _row_to_dict(message) or {}
        message_dict["attachments"] = _message_attachments(conn, int(message_dict["id"]))
        message_dicts.append(message_dict)
    thread["messages"] = message_dicts
    return thread


def get_or_create_thread(user: dict) -> dict:
    user_id = int(user["id"])
    email = str(user.get("email") or "")
    display_name = str(user.get("display_name") or "")
    now = utc_now_text()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO feedback_threads(user_id, user_email, display_name, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                user_email = excluded.user_email,
                display_name = excluded.display_name
            """,
            (user_id, email, display_name, now, now),
        )
        row = conn.execute("SELECT * FROM feedback_threads WHERE user_id = ?", (user_id,)).fetchone()
        conn.commit()
        return _thread_from_row(conn, row) or {}


def get_user_thread(user_id: int, *, mark_seen: bool = False) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM feedback_threads WHERE user_id = ?", (int(user_id),)).fetchone()
        thread = _thread_from_row(conn, row)
        if mark_seen and thread is not None and int(thread.get("unread_user_count") or 0) > 0:
            conn.execute(
                "UPDATE feedback_threads SET unread_user_count = 0 WHERE id = ?",
                (int(thread["id"]),),
            )
            conn.commit()
        return thread


def list_feedback_threads(*, limit: int = 40) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM feedback_threads
            ORDER BY unread_admin_count DESC, updated_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [_thread_from_row(conn, row) or {} for row in rows]


def _insert_attachments(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    thread_id: int,
    attachments: list[dict] | None,
    now: str,
) -> None:
    for attachment in attachments or []:
        stored_name = str(attachment.get("stored_name") or "").strip()
        if not stored_name:
            continue
        conn.execute(
            """
            INSERT INTO feedback_attachments(
                message_id, thread_id, stored_name, mime, byte_size, original_name, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(message_id),
                int(thread_id),
                stored_name,
                str(attachment.get("mime") or ""),
                int(attachment.get("byte_size") or 0),
                str(attachment.get("original_name") or "")[:200],
                now,
            ),
        )


def add_user_message(user: dict, body: str, attachments: list[dict] | None = None) -> tuple[dict, dict]:
    thread = get_or_create_thread(user)
    thread_id = int(thread["id"])
    now = utc_now_text()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback_messages(thread_id, author_role, author_user_id, author_email, body, created_at)
            VALUES(?, 'user', ?, ?, ?, ?)
            """,
            (thread_id, int(user["id"]), str(user.get("email") or ""), body, now),
        )
        _insert_attachments(conn, message_id=int(cur.lastrowid), thread_id=thread_id, attachments=attachments, now=now)
        conn.execute(
            """
            UPDATE feedback_threads
            SET status = 'needs_reply',
                unread_admin_count = unread_admin_count + 1,
                last_user_message_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, thread_id),
        )
        message_row = conn.execute("SELECT * FROM feedback_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        thread_row = conn.execute("SELECT * FROM feedback_threads WHERE id = ?", (thread_id,)).fetchone()
        conn.commit()
        message = _row_to_dict(message_row) or {}
        if message:
            message["attachments"] = _message_attachments(conn, int(message["id"]))
        return _thread_from_row(conn, thread_row) or {}, message


def add_admin_reply(thread_id: int, admin: dict, body: str, attachments: list[dict] | None = None) -> tuple[dict, dict]:
    now = utc_now_text()
    with _connect() as conn:
        thread_row = conn.execute("SELECT * FROM feedback_threads WHERE id = ?", (int(thread_id),)).fetchone()
        if thread_row is None:
            raise ValueError("留言会话不存在。")
        cur = conn.execute(
            """
            INSERT INTO feedback_messages(thread_id, author_role, author_user_id, author_email, body, created_at)
            VALUES(?, 'admin', ?, ?, ?, ?)
            """,
            (int(thread_id), int(admin["id"]), str(admin.get("email") or ""), body, now),
        )
        _insert_attachments(conn, message_id=int(cur.lastrowid), thread_id=int(thread_id), attachments=attachments, now=now)
        conn.execute(
            """
            UPDATE feedback_threads
            SET status = 'answered',
                unread_admin_count = 0,
                unread_user_count = unread_user_count + 1,
                last_admin_message_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, int(thread_id)),
        )
        message_row = conn.execute("SELECT * FROM feedback_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        updated = conn.execute("SELECT * FROM feedback_threads WHERE id = ?", (int(thread_id),)).fetchone()
        conn.commit()
        message = _row_to_dict(message_row) or {}
        if message:
            message["attachments"] = _message_attachments(conn, int(message["id"]))
        return _thread_from_row(conn, updated) or {}, message


def update_message_email_status(message_id: int, status: str, error: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE feedback_messages
            SET email_status = ?, email_error = ?
            WHERE id = ?
            """,
            ((status or "").strip(), (error or "")[:1200], int(message_id)),
        )
        conn.commit()


def get_attachment(attachment_id: int) -> dict | None:
    """返回附件元数据及其所属会话 user_id（用于鉴权后送图）。"""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT a.*, t.user_id AS thread_user_id
            FROM feedback_attachments AS a
            JOIN feedback_threads AS t ON t.id = a.thread_id
            WHERE a.id = ?
            """,
            (int(attachment_id),),
        ).fetchone()
        return _row_to_dict(row)
