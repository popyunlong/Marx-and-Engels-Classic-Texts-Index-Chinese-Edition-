from __future__ import annotations

import os
import json
import sqlite3
from pathlib import Path

from membership import utc_now_text
from runtime_env import APPDATA_DIR, secure_db_file


# 「个人文库」提交与审核记录。落数据盘（同 notes：服务器上 /home/data/... 经 fstab bind 挂到
# APPDATA_DIR/personal_library），代码只认 APPDATA_DIR 路径，数据盘与否属纯运维层。
#
# 注意分工：**本库只存元数据与审核状态**。原始 PDF 与页图存在另一台存储服务器（见
# personal_library_store），每用户的可检索文本索引在 personal_corpus（必须与检索同机，
# 因检索是内存里对 Corpus 做字符串扫描而非 SQL 查询）。
PERSONAL_LIB_DIR = APPDATA_DIR / "personal_library"
DB_PATH = PERSONAL_LIB_DIR / "personal_library.sqlite3"

# 每人可上架册数上限。**后台控制台可调**（设置键 mylib_max_books_per_user）；未设置时回落
# 环境变量、再回落默认值。存储服务器可用空间约 37G、均 20MB/本 → 约 1000 本。
MAX_BOOKS_PER_USER_DEFAULT = int(os.environ.get("MYLIB_MAX_BOOKS_PER_USER", "5"))
SETTING_MAX_BOOKS = "mylib_max_books_per_user"
SETTING_OCR_QUOTAS = "mylib_ocr_quotas"


def max_books_per_user() -> int:
    """每人可上架册数上限（后台可改，立即生效）。读设置失败时回落默认值，不让配额判断挂掉。"""
    try:
        from admin_store import get_setting

        raw = get_setting(SETTING_MAX_BOOKS, None)
        if raw is not None:
            n = int(raw)
            if n > 0:
                return n
    except Exception:  # noqa: BLE001 — 设置库不可用不应挡住上传流程
        pass
    return MAX_BOOKS_PER_USER_DEFAULT


def set_max_books_per_user(value: int) -> int:
    from admin_store import set_setting

    n = max(1, min(int(value), 1000))
    set_setting(SETTING_MAX_BOOKS, n, updated_by="admin")
    return n
# 单本上限 100MB。注意 Flask 全局 MAX_CONTENT_LENGTH 仅 4MB，上传路由须单独放宽。
MAX_PDF_BYTES = int(os.environ.get("MYLIB_MAX_PDF_MB", "100")) * 1024 * 1024

# 「用户荐书」与个人文库共用独立存储节点，但只保存待管理员下载的原始 PDF，绝不进入
# 用户私有索引或公共语料。元数据继续落本机数据盘，便于审计和故障恢复。
BOOK_RECOMMENDATION_MAX_PDF_BYTES = int(
    os.environ.get("BOOK_RECOMMENDATION_MAX_PDF_MB", "100")
) * 1024 * 1024
BOOK_RECOMMENDATION_MAX_ACTIVE_PER_USER = max(
    1, int(os.environ.get("BOOK_RECOMMENDATION_MAX_ACTIVE_PER_USER", "5"))
)

# ---- 扫描件 OCR 成本闸（GLM-4V 按页计费，三道闸缺一不可）----
# 默认值只负责首次启动/设置库不可用时兜底；站长可在网站后台即时调整并持久保存。
# 默认至少容纳一部常见千页学术著作，避免旧版“每人每月 500 页比一本书还小”的断链。
OCR_QUOTA_DEFAULTS = {
    "book": int(os.environ.get("MYLIB_OCR_MAX_PAGES_BOOK", "2000")),
    "user_month": int(os.environ.get("MYLIB_OCR_MAX_PAGES_USER_MONTH", "5000")),
    "site_day": int(os.environ.get("MYLIB_OCR_MAX_PAGES_DAY", "10000")),
}


def ocr_quota_settings() -> dict[str, int]:
    """当前 OCR 三道额度；后台设置优先，缺项回退环境变量/内置默认。"""
    raw: dict = {}
    try:
        from admin_store import get_setting

        value = get_setting(SETTING_OCR_QUOTAS, {})
        if isinstance(value, dict):
            raw = value
    except Exception:  # noqa: BLE001 — 设置库异常不应让解析任务崩溃
        pass
    out: dict[str, int] = {}
    for key, default in OCR_QUOTA_DEFAULTS.items():
        try:
            out[key] = max(0, min(int(raw.get(key, default)), 1_000_000))
        except (TypeError, ValueError):
            out[key] = max(0, int(default))
    return out


def set_ocr_quota_settings(*, book: int, user_month: int, site_day: int) -> dict[str, int]:
    """持久保存 OCR 额度。0 表示暂停相应范围的 OCR，修改后立即生效。"""
    from admin_store import set_setting

    values = {
        "book": max(0, min(int(book), 1_000_000)),
        "user_month": max(0, min(int(user_month), 1_000_000)),
        "site_day": max(0, min(int(site_day), 1_000_000)),
    }
    set_setting(SETTING_OCR_QUOTAS, values, updated_by="admin")
    return values

# 状态机：
#   storing  → 请求已接收，正在后台安全写入存储节点（不可审核）
#   pending  → 已安全入库，待管理员审核
#   rejected → 管理员退回（带原因），用户可改后重传
#   queued   → 已批准，等待解析
#   parsing  → 解析中（progress_done/total 可见）
#   ready    → 已上架，可读；searchable=1 时并可检索
#   failed   → 解析失败（带原因），管理员可重试
#   deleted  → 已删除（软删留审计，用户不可见）
STATUSES = (
    "storing", "pending", "rejected", "queued", "parsing",
    "quality_review", "ready", "failed", "deleted",
)
# 用户「我的文库」可见的状态（deleted 永不露出）
VISIBLE_STATUSES = (
    "storing", "pending", "rejected", "queued", "parsing",
    "quality_review", "ready", "failed",
)

_WAL_ENABLED = False


def _connect() -> sqlite3.Connection:
    PERSONAL_LIB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    secure_db_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def init_personal_library_db() -> Path:
    with _connect() as conn:
        conn.executescript(
            """
            -- 用户上传的书。一切读取/检索/取图都以 user_id 判权（user_id 只来自会话，绝不取自请求体）。
            CREATE TABLE IF NOT EXISTS personal_library_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_email TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                original_filename TEXT NOT NULL DEFAULT '',
                byte_size INTEGER NOT NULL DEFAULT 0,
                page_count INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT '',      -- text_layer / scanned
                searchable INTEGER NOT NULL DEFAULT 0,     -- 1=已并入个人检索索引
                status TEXT NOT NULL DEFAULT 'pending',
                reject_reason TEXT NOT NULL DEFAULT '',
                fail_reason TEXT NOT NULL DEFAULT '',
                license_attested INTEGER NOT NULL DEFAULT 0,
                ocr_consent INTEGER NOT NULL DEFAULT 0,    -- 扫描件是否同意第三方 OCR
                progress_done INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                reviewed_at TEXT NOT NULL DEFAULT '',
                parsed_at TEXT NOT NULL DEFAULT ''
            );
            -- 审核队列：按状态取待审、按时间排序
            CREATE INDEX IF NOT EXISTS idx_plib_status
                ON personal_library_submissions(status, created_at DESC, id DESC);
            -- 「我的文库」列表：按人取书
            CREATE INDEX IF NOT EXISTS idx_plib_user
                ON personal_library_submissions(user_id, status, id DESC);

            -- OCR 用量流水：支撑「每人每月」与「全站每日」两道成本闸，也供控制台用量面板。
            CREATE TABLE IF NOT EXISTS personal_library_ocr_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                pages INTEGER NOT NULL DEFAULT 0,
                day TEXT NOT NULL,              -- YYYY-MM-DD（UTC）
                month TEXT NOT NULL,            -- YYYY-MM（UTC）
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_plib_ocr_day ON personal_library_ocr_usage(day);
            CREATE INDEX IF NOT EXISTS idx_plib_ocr_user_month
                ON personal_library_ocr_usage(user_id, month);

            -- 用户荐书：这里只存书目信息、对象指纹与转存状态；PDF 原件在个人文库存储节点的
            -- recommendations/ 独立命名空间，和个人书 books/ 完全隔离。
            CREATE TABLE IF NOT EXISTS user_book_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_email TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                original_filename TEXT NOT NULL DEFAULT '',
                byte_size INTEGER NOT NULL DEFAULT 0,
                page_count INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'storing',
                fail_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                stored_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_book_recommendations_status
                ON user_book_recommendations(status, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_book_recommendations_user
                ON user_book_recommendations(user_id, status, id DESC);
            """
        )
        # 平滑升级：个人文库已经在生产端产生数据时，CREATE TABLE 不会补新列；逐列迁移可在
        # 不重建表、不锁住旧数据的前提下上线目录/引文派生状态。SQLite 的 ADD COLUMN 对现有行
        # 自动填默认值，旧版本进程在滚动切换窗口里也会忽略这些列。
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(personal_library_submissions)").fetchall()
        }
        migrations = {
            "toc_json": "TEXT NOT NULL DEFAULT '[]'",
            "toc_count": "INTEGER NOT NULL DEFAULT 0",
            "citation_ready": "INTEGER NOT NULL DEFAULT 0",
            "derivative_version": "INTEGER NOT NULL DEFAULT 0",
            "quality_json": "TEXT NOT NULL DEFAULT '{}'",
            "bibliographic_json": "TEXT NOT NULL DEFAULT '{}'",
            "acceptance_status": "TEXT NOT NULL DEFAULT ''",
            "acceptance_json": "TEXT NOT NULL DEFAULT '{}'",
            "acceptance_checked_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in migrations.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE personal_library_submissions ADD COLUMN {name} {ddl}"
                )
        conn.commit()
    return DB_PATH


# ---------- 用户荐书 ----------
def create_book_recommendation(
    *,
    user_id: int,
    user_email: str,
    title: str,
    author: str,
    note: str,
    original_filename: str,
    byte_size: int,
    page_count: int,
    sha256: str,
) -> dict:
    title = str(title or "").strip()[:160]
    author = str(author or "").strip()[:120]
    note = str(note or "").strip()[:500]
    filename = Path(str(original_filename or "")).name[:240]
    digest = str(sha256 or "").strip().lower()[:64]
    if not title:
        raise ValueError("请填写荐书书名。")
    if not filename.lower().endswith(".pdf"):
        raise ValueError("荐书栏目只接收 PDF 文件。")
    if int(byte_size or 0) <= 0 or int(byte_size) > BOOK_RECOMMENDATION_MAX_PDF_BYTES:
        raise ValueError(
            f"PDF 文件须小于 {BOOK_RECOMMENDATION_MAX_PDF_BYTES // 1048576}MB。"
        )
    if not digest:
        raise ValueError("无法计算文件指纹，请重新上传。")

    with _connect() as conn:
        duplicate = conn.execute(
            "SELECT * FROM user_book_recommendations "
            "WHERE user_id = ? AND sha256 = ? AND status IN ('storing','pending') "
            "ORDER BY id DESC LIMIT 1",
            (int(user_id), digest),
        ).fetchone()
        if duplicate:
            return _row_to_dict(duplicate) or {}
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM user_book_recommendations "
            "WHERE user_id = ? AND status IN ('storing','pending')",
            (int(user_id),),
        ).fetchone()
        if active and int(active["n"] or 0) >= BOOK_RECOMMENDATION_MAX_ACTIVE_PER_USER:
            raise ValueError(
                f"你已有 {BOOK_RECOMMENDATION_MAX_ACTIVE_PER_USER} 本荐书等待管理员处理，请稍后再提交。"
            )
        now = utc_now_text()
        cur = conn.execute(
            """
            INSERT INTO user_book_recommendations (
                user_id, user_email, title, author, note, original_filename,
                byte_size, page_count, sha256, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'storing', ?)
            """,
            (
                int(user_id), str(user_email or "").strip()[:240], title, author, note,
                filename, int(byte_size), max(0, int(page_count or 0)), digest, now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM user_book_recommendations WHERE id = ?", (int(cur.lastrowid),)
        ).fetchone()
    return _row_to_dict(row) or {}


def get_book_recommendation(recommendation_id: int, user_id: int | None = None) -> dict | None:
    sql = "SELECT * FROM user_book_recommendations WHERE id = ?"
    params: tuple = (int(recommendation_id),)
    if user_id is not None:
        sql += " AND user_id = ?"
        params += (int(user_id),)
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return _row_to_dict(row)


def find_book_recommendation_by_sha(user_id: int, digest: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_book_recommendations "
            "WHERE user_id = ? AND sha256 = ? AND status IN ('storing','pending') "
            "ORDER BY id DESC LIMIT 1",
            (int(user_id), str(digest or "").strip().lower()),
        ).fetchone()
    return _row_to_dict(row)


def list_book_recommendations(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM user_book_recommendations "
            "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'storing' THEN 1 ELSE 2 END, "
            "created_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def count_pending_book_recommendations() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM user_book_recommendations WHERE status = 'pending'"
        ).fetchone()
    return int(row["n"] or 0) if row else 0


def list_storing_book_recommendations() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM user_book_recommendations WHERE status = 'storing' ORDER BY id"
        ).fetchall()
    return [_row_to_dict(row) or {} for row in rows]


def mark_book_recommendation_stored(recommendation_id: int) -> bool:
    """Only one ingest worker may complete a recommendation and notify its admin."""
    with _connect() as conn:
        result = conn.execute(
            "UPDATE user_book_recommendations SET status = 'pending', fail_reason = '', "
            "stored_at = ? WHERE id = ? AND status = 'storing'",
            (utc_now_text(), int(recommendation_id)),
        )
        conn.commit()
        return result.rowcount == 1


def set_book_recommendation_status(
    recommendation_id: int, status: str, *, fail_reason: str = "",
) -> None:
    normalized = str(status or "").strip().lower()
    if normalized not in {"storing", "pending", "failed", "archived"}:
        raise ValueError("荐书状态无效。")
    stored_at = utc_now_text() if normalized == "pending" else ""
    with _connect() as conn:
        conn.execute(
            "UPDATE user_book_recommendations SET status = ?, fail_reason = ?, "
            "stored_at = CASE WHEN ? <> '' THEN ? ELSE stored_at END WHERE id = ?",
            (
                normalized, str(fail_reason or "").strip()[:500], stored_at, stored_at,
                int(recommendation_id),
            ),
        )
        conn.commit()


def archive_book_recommendations(recommendation_ids: list[int] | tuple[int, ...]) -> None:
    """Atomically mark one fully published recommendation batch as processed."""
    ids = tuple(dict.fromkeys(int(value) for value in recommendation_ids if int(value) > 0))
    if not ids:
        raise ValueError("荐书批次不能为空。")
    placeholders = ",".join("?" for _ in ids)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT id,status FROM user_book_recommendations WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        if len(rows) != len(ids) or any(str(row["status"]) != "pending" for row in rows):
            raise ValueError("荐书记录已变化，不能归档发布批次。")
        conn.execute(
            f"UPDATE user_book_recommendations SET status='archived',fail_reason='' "
            f"WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()


# ---------- OCR 成本闸 ----------
def _now_day_month() -> tuple[str, str]:
    ts = utc_now_text()
    return ts[:10], ts[:7]


def record_ocr_usage(user_id: int, submission_id: int, pages: int) -> None:
    if pages <= 0:
        return
    day, month = _now_day_month()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO personal_library_ocr_usage "
            "(user_id, submission_id, pages, day, month, created_at) VALUES (?,?,?,?,?,?)",
            (int(user_id), int(submission_id), int(pages), day, month, utc_now_text()),
        )
        conn.commit()


def ocr_pages_used(user_id: int | None = None, *, scope: str = "month") -> int:
    """已用页数。scope='month' 且给 user_id → 该用户本月；scope='day' → 全站今日。"""
    day, month = _now_day_month()
    with _connect() as conn:
        if scope == "day":
            row = conn.execute(
                "SELECT COALESCE(SUM(pages),0) AS n FROM personal_library_ocr_usage WHERE day = ?",
                (day,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(pages),0) AS n FROM personal_library_ocr_usage "
                "WHERE user_id = ? AND month = ?",
                (int(user_id or 0), month),
            ).fetchone()
        return int(row["n"]) if row else 0


def ocr_quota_allowance(user_id: int, wanted_pages: int) -> tuple[int, str]:
    """三道闸取最小值 → 本次实际可 OCR 的页数上限。

    返回 (允许页数, 说明)。允许页数为 0 表示本次不能做 OCR（说明里给用户可读的原因）。
    """
    wanted = max(0, int(wanted_pages))
    limits = ocr_quota_settings()
    caps = [
        (limits["book"], f"单本 OCR 上限 {limits['book']} 页"),
        (max(0, limits["user_month"] - ocr_pages_used(user_id, scope="month")),
         f"本月个人 OCR 额度剩余不足（每月 {limits['user_month']} 页）"),
        (max(0, limits["site_day"] - ocr_pages_used(scope="day")),
         f"全站今日 OCR 额度已用尽（每日 {limits['site_day']} 页）"),
    ]
    allowed = wanted
    reason = ""
    for cap, why in caps:
        if cap < allowed:
            allowed = cap
            reason = why
    return max(0, allowed), ("" if allowed >= wanted else reason)


def ocr_usage_overview(limit: int = 10) -> dict:
    """控制台用量面板：今日全站、本月全站、以及本月用量最高的若干用户。"""
    day, month = _now_day_month()
    with _connect() as conn:
        today = conn.execute(
            "SELECT COALESCE(SUM(pages),0) n FROM personal_library_ocr_usage WHERE day = ?",
            (day,)).fetchone()["n"]
        this_month = conn.execute(
            "SELECT COALESCE(SUM(pages),0) n FROM personal_library_ocr_usage WHERE month = ?",
            (month,)).fetchone()["n"]
        rows = conn.execute(
            "SELECT user_id, SUM(pages) AS pages FROM personal_library_ocr_usage "
            "WHERE month = ? GROUP BY user_id ORDER BY pages DESC LIMIT ?",
            (month, int(limit))).fetchall()
    limits = ocr_quota_settings()
    return {
        "day": day, "month": month,
        "today_pages": int(today), "month_pages": int(this_month),
        "day_cap": limits["site_day"],
        "user_month_cap": limits["user_month"],
        "book_cap": limits["book"],
        "top_users": [{"user_id": r["user_id"], "pages": int(r["pages"])} for r in rows],
    }


# ---------- 查询 ----------
def count_user_books(user_id: int, *, statuses: tuple[str, ...] | None = None) -> int:
    """统计某用户占用的册数。默认只算「未删除」的，用于配额判断。"""
    statuses = statuses or VISIBLE_STATUSES
    ph = ",".join("?" * len(statuses))
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM personal_library_submissions "
            f"WHERE user_id = ? AND status IN ({ph})",
            (int(user_id), *statuses),
        ).fetchone()
        return int(row["n"]) if row else 0


def get_submission(submission_id: int, user_id: int | None = None) -> dict | None:
    """取一条提交。传 user_id 则强制归属校验（非本人返回 None）；仅管理员路径可省略。"""
    with _connect() as conn:
        if user_id is None:
            row = conn.execute(
                "SELECT * FROM personal_library_submissions WHERE id = ?",
                (int(submission_id),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM personal_library_submissions WHERE id = ? AND user_id = ?",
                (int(submission_id), int(user_id)),
            ).fetchone()
        return _row_to_dict(row)


def list_user_books(user_id: int) -> list[dict]:
    """「我的文库」列表：该用户全部未删除的书，新的在前。"""
    ph = ",".join("?" * len(VISIBLE_STATUSES))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM personal_library_submissions "
            f"WHERE user_id = ? AND status IN ({ph}) ORDER BY id DESC",
            (int(user_id), *VISIBLE_STATUSES),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_searchable_books(user_id: int) -> list[dict]:
    """该用户可参与检索的书（已上架且有文字层）。供个人 Corpus 与检索范围树使用。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personal_library_submissions "
            "WHERE user_id = ? AND status = 'ready' AND searchable = 1 ORDER BY id",
            (int(user_id),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_pending(limit: int = 80) -> list[dict]:
    """控制台审核队列：待审的排最前，其次是解析中/失败（需要管理员关注）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personal_library_submissions "
            "WHERE status IN ('pending','quality_review','parsing','queued','failed') "
            "ORDER BY CASE status WHEN 'quality_review' THEN 0 WHEN 'pending' THEN 1 "
            "WHEN 'failed' THEN 2 ELSE 3 END, "
            "created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_recent_reviewed(limit: int = 30) -> list[dict]:
    """控制台「已处理」区：最近批准/退回过的。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personal_library_submissions "
            "WHERE status IN ('ready','rejected') ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_acceptance_audit(gate_version: int, limit: int = 5000) -> list[dict]:
    """Return completed books not yet checked by the current acceptance gate version."""
    marker = f'%"gate_version":{max(0, int(gate_version))}%'
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personal_library_submissions "
            "WHERE status IN ('ready','quality_review') "
            "AND (acceptance_status = '' OR acceptance_json NOT LIKE ?) "
            "ORDER BY id LIMIT ?",
            (marker, max(1, int(limit))),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_pending() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM personal_library_submissions "
            "WHERE status IN ('pending','quality_review')"
        ).fetchone()
        return int(row["n"]) if row else 0


def list_resumable() -> list[dict]:
    """启动时扫描：进程重启会让 queued/parsing 的作业悬空，需重新入队续跑。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personal_library_submissions "
            "WHERE status IN ('queued','parsing') ORDER BY id"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_storing() -> list[dict]:
    """启动时恢复已登记但尚未写完远端存储的上传。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personal_library_submissions WHERE status = 'storing' ORDER BY id"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def find_user_submission_by_sha(user_id: int, digest: str) -> dict | None:
    """同一用户重复提交同一文件时复用未删除记录，避免超时重试制造重复书目。"""
    value = str(digest or "").strip().lower()
    if not value:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM personal_library_submissions "
            "WHERE user_id = ? AND sha256 = ? AND status != 'deleted' ORDER BY id DESC LIMIT 1",
            (int(user_id), value),
        ).fetchone()
        return _row_to_dict(row)


def list_derivative_backfill(target_version: int, limit: int = 500) -> list[dict]:
    """找出已解析但仍由旧流水线生成的书，供启动后的低优先级自愈任务回填。

    ready 和 quality_review 都是解析已完成的稳定状态；后者必须纳入
    回填，否则旧管线造成的验收问题会永久留在复核队列。解析中状态仍排除，
    避免与主 worker 争抢同一本索引。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personal_library_submissions "
            "WHERE status IN ('ready','quality_review') "
            "AND derivative_version < ? ORDER BY id LIMIT ?",
            (int(target_version), max(1, int(limit))),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


# ---------- 写入 ----------
def create_submission(
    *,
    user_id: int,
    user_email: str = "",
    title: str,
    author: str = "",
    original_filename: str = "",
    byte_size: int = 0,
    page_count: int = 0,
    sha256: str = "",
    source_kind: str = "",
    license_attested: bool = False,
    ocr_consent: bool = False,
    toc_entries: list[dict] | None = None,
    initial_status: str = "pending",
) -> dict:
    """登记一条待审提交。配额与权利声明在此把关，调用方仍应先做魔数/大小校验。"""
    title = str(title or "").strip()
    if not title:
        raise ValueError("请填写书名。")
    if not license_attested:
        raise ValueError("请先勾选权利声明。")
    if initial_status not in STATUSES or initial_status in {"deleted", "ready"}:
        raise ValueError(f"无效的初始状态：{initial_status}")
    cap = max_books_per_user()
    if count_user_books(int(user_id)) >= cap:
        raise ValueError(f"个人文库册数已达上限（{cap} 本），请先删除不需要的书。")
    now = utc_now_text()
    clean_toc: list[dict] = []
    for item in (toc_entries or [])[:5000]:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("pdf_page") or item.get("page") or 0)
            level = max(1, min(12, int(item.get("level") or 1)))
        except (TypeError, ValueError):
            continue
        title_value = " ".join(str(item.get("title") or "").replace("\x00", "").split())[:500]
        if page < 1 or not title_value:
            continue
        clean_toc.append({"title": title_value, "pdf_page": page, "level": level})
    toc_json = json.dumps(clean_toc, ensure_ascii=False, separators=(",", ":"))
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO personal_library_submissions "
            "(user_id, user_email, title, author, original_filename, byte_size, page_count, "
            " sha256, source_kind, license_attested, ocr_consent, toc_json, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, ?, ?)",
            (int(user_id), str(user_email or ""), title, str(author or "").strip(),
             str(original_filename or "")[:200], int(byte_size), int(page_count),
             str(sha256 or ""), str(source_kind or ""),
             1 if license_attested else 0, 1 if ocr_consent else 0, toc_json,
             initial_status, now),
        )
        conn.commit()
        sid = int(cur.lastrowid)
    return get_submission(sid)


def submission_toc(row: dict | None) -> list[dict]:
    """读取上传时从 PDF 书签提取的目录；坏 JSON 按空目录降级，绝不阻断解析。"""
    if not row:
        return []
    try:
        value = json.loads(str(row.get("toc_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def submission_quality(row: dict | None) -> dict:
    """读取派生质量报告；旧行、坏 JSON 都按空报告降级。"""
    if not row:
        return {}
    try:
        value = json.loads(str(row.get("quality_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def submission_bibliographic(row: dict | None) -> dict:
    """读取版权页识别出的书目数据；旧行或损坏 JSON 安全降级为空。"""
    if not row:
        return {}
    try:
        value = json.loads(str(row.get("bibliographic_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def submission_acceptance(row: dict | None) -> dict:
    """Read the persisted post-parse acceptance report."""
    if not row:
        return {}
    try:
        value = json.loads(str(row.get("acceptance_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def set_bibliographic_metadata(submission_id: int, metadata: dict | None) -> None:
    """持久化版权页/扉页识别结果，供检索引文与阅读器共用。"""
    clean = metadata if isinstance(metadata, dict) else {}
    with _connect() as conn:
        conn.execute(
            "UPDATE personal_library_submissions SET bibliographic_json = ? WHERE id = ?",
            (json.dumps(clean, ensure_ascii=False, separators=(",", ":")), int(submission_id)),
        )
        conn.commit()


def set_catalog_identity(submission_id: int, *, title: str = "", author: str = "") -> None:
    """Apply a high-confidence cover/copyright identity to the user-facing book row."""
    clean_title = str(title or "").strip()[:200]
    clean_author = str(author or "").strip()[:160]
    if not clean_title and not clean_author:
        return
    assignments = []
    values: list[object] = []
    if clean_title:
        assignments.append("title = ?")
        values.append(clean_title)
    if clean_author:
        assignments.append("author = ?")
        values.append(clean_author)
    values.append(int(submission_id))
    with _connect() as conn:
        conn.execute(
            "UPDATE personal_library_submissions SET " + ", ".join(assignments) + " WHERE id = ?",
            values,
        )
        conn.commit()


def set_derivative_state(
    submission_id: int,
    *,
    toc_count: int,
    citation_ready: bool,
    version: int,
    quality: dict | None = None,
) -> None:
    """记录引文/目录派生物已生成。与 ready 状态分列，便于线上审计和版本回填。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE personal_library_submissions "
            "SET toc_count = ?, citation_ready = ?, derivative_version = ?, quality_json = ? "
            "WHERE id = ?",
            (max(0, int(toc_count)), 1 if citation_ready else 0,
             max(0, int(version)),
             json.dumps(quality if isinstance(quality, dict) else {}, ensure_ascii=False,
                        separators=(",", ":")),
             int(submission_id)),
        )
        conn.commit()


def set_derivative_acceptance(
    submission_id: int,
    report: dict | None,
    *,
    status: str = "",
) -> None:
    """Persist the acceptance gate result separately from parser confidence data."""
    clean = report if isinstance(report, dict) else {}
    clean_status = str(status or clean.get("status") or "")[:32]
    with _connect() as conn:
        conn.execute(
            "UPDATE personal_library_submissions "
            "SET acceptance_status = ?, acceptance_json = ?, acceptance_checked_at = ? "
            "WHERE id = ?",
            (
                clean_status,
                json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                utc_now_text(),
                int(submission_id),
            ),
        )
        conn.commit()


def set_status(
    submission_id: int,
    status: str,
    *,
    reject_reason: str | None = None,
    fail_reason: str | None = None,
    searchable: bool | None = None,
    reviewed: bool = False,
    parsed: bool = False,
) -> dict | None:
    if status not in STATUSES:
        raise ValueError(f"未知状态：{status}")
    sets = ["status = ?"]
    args: list[object] = [status]
    if reject_reason is not None:
        sets.append("reject_reason = ?"); args.append(str(reject_reason)[:500])
    if fail_reason is not None:
        sets.append("fail_reason = ?"); args.append(str(fail_reason)[:500])
    if searchable is not None:
        sets.append("searchable = ?"); args.append(1 if searchable else 0)
    if reviewed:
        sets.append("reviewed_at = ?"); args.append(utc_now_text())
    if parsed:
        sets.append("parsed_at = ?"); args.append(utc_now_text())
    args.append(int(submission_id))
    with _connect() as conn:
        conn.execute(
            f"UPDATE personal_library_submissions SET {', '.join(sets)} WHERE id = ?",
            tuple(args),
        )
        conn.commit()
    return get_submission(int(submission_id))


def set_progress(submission_id: int, done: int, total: int) -> None:
    """解析进度，用户与控制台都可见（「解析中 128/312 页」）。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE personal_library_submissions SET progress_done = ?, progress_total = ? WHERE id = ?",
            (int(done), int(total), int(submission_id)),
        )
        conn.commit()


def set_page_count(submission_id: int, page_count: int, source_kind: str = "") -> None:
    with _connect() as conn:
        if source_kind:
            conn.execute(
                "UPDATE personal_library_submissions SET page_count = ?, source_kind = ? WHERE id = ?",
                (int(page_count), str(source_kind), int(submission_id)),
            )
        else:
            conn.execute(
                "UPDATE personal_library_submissions SET page_count = ? WHERE id = ?",
                (int(page_count), int(submission_id)),
            )
        conn.commit()


def mark_deleted(submission_id: int, user_id: int | None = None) -> bool:
    """软删：行留作审计，用户不再可见。真正的对象清理由调用方另行触发。"""
    with _connect() as conn:
        if user_id is None:
            cur = conn.execute(
                "UPDATE personal_library_submissions SET status = 'deleted', searchable = 0 WHERE id = ?",
                (int(submission_id),),
            )
        else:
            cur = conn.execute(
                "UPDATE personal_library_submissions SET status = 'deleted', searchable = 0 "
                "WHERE id = ? AND user_id = ?",
                (int(submission_id), int(user_id)),
            )
        conn.commit()
        return cur.rowcount > 0
