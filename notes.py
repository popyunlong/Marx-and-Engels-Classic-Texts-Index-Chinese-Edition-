from __future__ import annotations

import sqlite3
from pathlib import Path

from membership import utc_now_text
from runtime_env import APPDATA_DIR, secure_db_file


# 笔记 / 知识库数据库。落「数据盘」：服务器上 /home/data/notes 经 fstab bind 挂到
# APPDATA_DIR/notes（与 page_images 同一手法），本地开发则直接落 APPDATA_DIR/notes。
# 代码只认 APPDATA_DIR/notes 这个路径，数据盘与否属纯运维层（见 [[server-storage-layout]]）。
NOTES_DIR = APPDATA_DIR / "notes"
DB_PATH = NOTES_DIR / "notes.sqlite3"

# 每用户笔记条数上限：知识库定位为「个人研究积累」，给一个宽松但防失控的封顶。
MAX_NOTES_PER_USER = 5000

_WAL_ENABLED = False


def _connect() -> sqlite3.Connection:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    secure_db_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发记笔记须等锁而非立即 500：busy_timeout 等锁，WAL 提升读写并发（持久属性，设一次、失败静默回退）。
    conn.execute("PRAGMA busy_timeout = 5000")
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


def init_notes_db() -> Path:
    with _connect() as conn:
        conn.executescript(
            """
            -- 阅读器笔记 + 「我的知识库」聚合。每条笔记归属单一用户（user_id），按阅读位置锚定。
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                reader TEXT NOT NULL DEFAULT '',        -- viewer(扫描) / liushi(流式) / wenku(文库)
                book_key TEXT NOT NULL DEFAULT '',       -- viewer: source_file；wenku/liushi: book_key|卷号
                book_title TEXT NOT NULL DEFAULT '',
                volume_label TEXT NOT NULL DEFAULT '',
                page INTEGER,                            -- viewer: PDF 页码；wenku/liushi: 印刷页（可空）
                page_label TEXT NOT NULL DEFAULT '',      -- 「第 X 页」等人类可读页码
                doc_path TEXT NOT NULL DEFAULT '',        -- wenku/liushi: 章节相对路径
                anchor_text TEXT NOT NULL DEFAULT '',     -- wenku/liushi: 供重定位的原文片段
                quote TEXT NOT NULL DEFAULT '',           -- 选中的摘录（可空 → 整页/整章便签）
                body TEXT NOT NULL DEFAULT '',            -- 用户笔记正文
                color TEXT NOT NULL DEFAULT '',           -- 预留（多色高亮）
                source_url TEXT NOT NULL DEFAULT '',      -- 跳回原文的根相对 URL
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notes_user
                ON notes(user_id, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_notes_user_book
                ON notes(user_id, book_key, page, id);
            """
        )
        conn.commit()
    return DB_PATH


def count_notes(user_id: int, *, book_key: str | None = None) -> int:
    with _connect() as conn:
        if book_key:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM notes WHERE user_id = ? AND book_key = ?",
                (int(user_id), str(book_key)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM notes WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
        return int(row["n"]) if row else 0


def create_note(
    *,
    user_id: int,
    reader: str,
    book_key: str,
    book_title: str = "",
    volume_label: str = "",
    page: int | None = None,
    page_label: str = "",
    doc_path: str = "",
    anchor_text: str = "",
    quote: str = "",
    body: str = "",
    color: str = "",
    source_url: str = "",
) -> dict:
    """新建一条笔记（已按 user_id 归属）。摘录与正文不可同时为空；超过每用户上限则抛 ValueError。"""
    body = str(body or "").strip()
    quote = str(quote or "").strip()
    if not body and not quote:
        raise ValueError("笔记内容为空。")
    if count_notes(int(user_id)) >= MAX_NOTES_PER_USER:
        raise ValueError(f"笔记数量已达上限（{MAX_NOTES_PER_USER} 条），请先清理或导出。")
    now = utc_now_text()
    page_val: int | None = None
    try:
        if page is not None and str(page).strip() != "":
            page_val = int(page)
    except (TypeError, ValueError):
        page_val = None
    # 仅接受根相对 URL，防注入外链（与 reading-history 前端同策略）。
    src = str(source_url or "").strip()
    if src and not src.startswith("/"):
        src = ""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO notes(
                user_id, reader, book_key, book_title, volume_label, page, page_label,
                doc_path, anchor_text, quote, body, color, source_url, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                str(reader or "")[:24],
                str(book_key or "")[:200],
                str(book_title or "")[:200],
                str(volume_label or "")[:200],
                page_val,
                str(page_label or "")[:64],
                str(doc_path or "")[:500],
                str(anchor_text or "")[:600],
                quote[:4000],
                body[:8000],
                str(color or "")[:24],
                src[:600],
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (int(cur.lastrowid),)).fetchone()
        return _row_to_dict(row) or {}


def list_notes(
    user_id: int,
    *,
    book_key: str | None = None,
    query: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    """列出某用户的笔记，可按书（book_key）过滤、按关键词（摘录/正文/书名）搜索。"""
    clauses = ["user_id = ?"]
    params: list = [int(user_id)]
    if book_key:
        clauses.append("book_key = ?")
        params.append(str(book_key))
    q = str(query or "").strip()
    if q:
        clauses.append("(quote LIKE ? OR body LIKE ? OR book_title LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    where = " AND ".join(clauses)
    params.append(max(1, min(2000, int(limit))))
    params.append(max(0, int(offset)))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM notes WHERE {where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]


def get_note(note_id: int, user_id: int) -> dict | None:
    """取单条笔记——强制归属校验，非本人返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM notes WHERE id = ? AND user_id = ?",
            (int(note_id), int(user_id)),
        ).fetchone()
        return _row_to_dict(row)


def update_note(
    note_id: int,
    user_id: int,
    *,
    body: str | None = None,
    color: str | None = None,
) -> dict | None:
    """更新自己的一条笔记正文/颜色。仅归属者可改；无匹配返回 None。"""
    sets = []
    params: list = []
    if body is not None:
        b = str(body).strip()
        if not b:
            raise ValueError("笔记内容为空。")
        sets.append("body = ?")
        params.append(b[:8000])
    if color is not None:
        sets.append("color = ?")
        params.append(str(color)[:24])
    if not sets:
        return get_note(int(note_id), int(user_id))
    now = utc_now_text()
    sets.append("updated_at = ?")
    params.append(now)
    params.append(int(note_id))
    params.append(int(user_id))
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE notes SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            params,
        )
        conn.commit()
        if cur.rowcount <= 0:
            return None
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (int(note_id),)).fetchone()
        return _row_to_dict(row)


def delete_note(note_id: int, user_id: int) -> bool:
    """删除自己的一条笔记。仅归属者可删。"""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM notes WHERE id = ? AND user_id = ?",
            (int(note_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def list_note_books(user_id: int) -> list[dict]:
    """「我的知识库」聚合视图用：按书/卷分组统计本用户的笔记（书名、卷、条数、最近更新）。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT book_key,
                   MAX(book_title)   AS book_title,
                   MAX(volume_label) AS volume_label,
                   MAX(reader)       AS reader,
                   COUNT(*)          AS note_count,
                   MAX(updated_at)   AS last_updated
            FROM notes
            WHERE user_id = ?
            GROUP BY book_key
            ORDER BY last_updated DESC
            """,
            (int(user_id),),
        ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]
