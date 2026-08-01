"""站内群发邮件（后台·内容运营）。

给「全部注册用户 / 全部有效会员 / 指定等级会员 / 指定邮箱」群发一封可 Markdown 排版的
公告邮件。复用 journal_alerts 的发信（send_email）、排版（_markdown_to_html + _email_styled_html，
与期刊综述/阅读器讲解同一套内联样式，邮件客户端可正确渲染标题/加粗/列表/引用）、以及 SQLite
连接（membership.sqlite3，与 users/subscriptions 同库，可写 user_id 外键式记账）。

发送本身在后台线程里逐收件人跑（见 app.py 的单飞调度），每封落 broadcast_deliveries 投递记录，
汇总计数写回 broadcast_campaigns，管理员刷新后台即可查看结果。
"""

from __future__ import annotations

import html
import re

from journal_alerts import (
    SMTPConfig,
    _connect,
    _email_styled_html,
    _markdown_to_html,
    load_smtp_config,
    send_email,
)
from membership import (
    list_active_member_emails,
    list_active_user_emails,
    list_dormant_noip_user_emails,
    normalize_email,
    utc_now_text,
)

# 受众范围：全部注册用户 / 全部有效会员 / 指定等级(套餐)会员 / 指定邮箱 / 久未回访用户。
# dormant_noip＝注册于记 IP 功能上线(2026-06-24)前、至今无登录态回访的老用户（召回邮件），
# 判据与名单见 membership.list_dormant_noip_user_emails——回访即自动离开该集合。
BROADCAST_SCOPES = ("registered", "members", "plans", "specific", "dormant_noip")

# 正文/主题里的个性化占位符。渲染时按收件人替换（未知则用邮箱 @ 前缀兜底）。
_PLACEHOLDER_RE = re.compile(r"\{(name|email)\}")


def init_broadcast_db() -> None:
    """建群发邮件所需的两张表（幂等）。表落在 membership.sqlite3，与 users 同库。"""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS broadcast_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                body_md TEXT NOT NULL,
                scope TEXT NOT NULL,
                plan_codes TEXT NOT NULL DEFAULT '',
                total_recipients INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'sending',
                error TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_created
                ON broadcast_campaigns(created_at DESC);

            CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                user_id INTEGER,
                email TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (campaign_id) REFERENCES broadcast_campaigns(id)
            );

            CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_campaign
                ON broadcast_deliveries(campaign_id, status);
            """
        )
        conn.commit()


def _greeting_name(recipient: dict) -> str:
    name = str(recipient.get("display_name") or "").strip()
    if name:
        return name
    email = str(recipient.get("email") or "").strip()
    return email.split("@", 1)[0] if "@" in email else email


def personalize(text: str, recipient: dict) -> str:
    """把 {name} / {email} 替换成收件人信息。用于纯文本主题；正文替换在 Markdown 渲染前做。"""
    name = _greeting_name(recipient)
    email = str(recipient.get("email") or "")

    def _sub(match: re.Match) -> str:
        return name if match.group(1) == "name" else email

    return _PLACEHOLDER_RE.sub(_sub, text or "")


def has_placeholders(text: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(text or ""))


def render_broadcast_html(body_md: str, recipient: dict | None = None) -> str:
    """把 Markdown 正文渲染成带内联样式的邮件 HTML（与期刊综述同一套排版）。"""
    text = personalize(body_md, recipient) if recipient else str(body_md or "")
    return _email_styled_html(_markdown_to_html(text))


def render_broadcast_email(
    subject: str, body_md: str, recipient: dict
) -> tuple[str, str, str]:
    """返回 (主题, 纯文本正文, HTML 正文)，主题与正文都已按收件人替换占位符。"""
    subject_out = personalize(subject, recipient).strip()
    text_body = personalize(body_md, recipient)
    html_body = render_broadcast_html(body_md, recipient)
    return subject_out, text_body, html_body


def _dedupe_recipients(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        email = normalize_email(str(row.get("email") or ""))
        if not email or email in seen:
            continue
        seen.add(email)
        out.append(
            {
                "email": email,
                "user_id": row.get("user_id"),
                "display_name": row.get("display_name") or "",
            }
        )
    return out


def resolve_recipients(
    scope: str,
    *,
    plan_codes: list[str] | None = None,
    emails: list[str] | None = None,
) -> list[dict]:
    """把受众范围解析成去重后的收件人列表 [{email, user_id, display_name}]。

    管理员群发，一律忽略 journal_alerts 订阅权限（这是站务公告，不是期刊订阅）。
    """
    scope = (scope or "").strip().lower()
    if scope == "registered":
        return _dedupe_recipients(list_active_user_emails())
    if scope == "members":
        return _dedupe_recipients(list_active_member_emails(None))
    if scope == "plans":
        codes = [str(c).strip() for c in (plan_codes or []) if str(c).strip()]
        if not codes:
            return []
        return _dedupe_recipients(list_active_member_emails(codes))
    if scope == "specific":
        rows: list[dict] = []
        for raw in emails or []:
            addr = normalize_email(str(raw))
            if addr:
                rows.append({"email": addr})
        return _dedupe_recipients(rows)
    if scope == "dormant_noip":
        return _dedupe_recipients(list_dormant_noip_user_emails())
    return []


def count_recipients(
    scope: str,
    *,
    plan_codes: list[str] | None = None,
    emails: list[str] | None = None,
) -> int:
    return len(resolve_recipients(scope, plan_codes=plan_codes, emails=emails))


def create_campaign(
    *,
    subject: str,
    body_md: str,
    scope: str,
    plan_codes: list[str] | None,
    total_recipients: int,
    created_by: str = "",
) -> int:
    now = utc_now_text()
    codes = ",".join(str(c).strip() for c in (plan_codes or []) if str(c).strip())
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO broadcast_campaigns(
                subject, body_md, scope, plan_codes, total_recipients,
                status, created_by, created_at
            ) VALUES(?, ?, ?, ?, ?, 'sending', ?, ?)
            """,
            (subject, body_md, scope, codes, int(total_recipients), created_by, now),
        )
        conn.commit()
        return int(cur.lastrowid)


def record_delivery(
    campaign_id: int, email: str, status: str, error: str = "", *, user_id: int | None = None
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO broadcast_deliveries(campaign_id, user_id, email, status, error, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (int(campaign_id), user_id, email, status, str(error or "")[:1000], utc_now_text()),
        )
        conn.commit()


def finish_campaign(campaign_id: int, *, sent: int, failed: int, error: str = "") -> None:
    status = "done" if failed == 0 else ("failed" if sent == 0 else "partial")
    with _connect() as conn:
        conn.execute(
            """
            UPDATE broadcast_campaigns
            SET sent = ?, failed = ?, status = ?, error = ?, finished_at = ?
            WHERE id = ?
            """,
            (int(sent), int(failed), status, str(error or "")[:4000], utc_now_text(), int(campaign_id)),
        )
        conn.commit()


def send_campaign(
    campaign_id: int,
    recipients: list[dict],
    *,
    subject: str,
    body_md: str,
    smtp_config: SMTPConfig | None = None,
) -> dict:
    """逐收件人发送并落投递记录。设计为在后台线程调用（入参显式、不依赖请求上下文）。"""
    smtp_config = smtp_config or load_smtp_config()
    sent = 0
    failed = 0
    errors: list[str] = []
    # 无占位符时正文/主题对所有人相同，渲染一次复用，省去逐封重复的 Markdown 解析。
    static_render = not (has_placeholders(body_md) or has_placeholders(subject))
    shared_subject = subject
    shared_text = body_md
    shared_html = render_broadcast_html(body_md) if static_render else ""
    for recipient in recipients:
        email = normalize_email(str(recipient.get("email") or ""))
        if not email:
            continue
        user_id = recipient.get("user_id")
        if static_render:
            subj, text_body, html_body = shared_subject, shared_text, shared_html
        else:
            subj, text_body, html_body = render_broadcast_email(subject, body_md, recipient)
        try:
            send_email(smtp_config, email, subj, text_body, html_body)
        except Exception as exc:  # noqa: BLE001 — 单封失败不阻断整批，落记录后继续。
            failed += 1
            errors.append(f"{email}: {exc}")
            record_delivery(campaign_id, email, "failed", str(exc), user_id=user_id)
            continue
        sent += 1
        record_delivery(campaign_id, email, "sent", "", user_id=user_id)
    finish_campaign(campaign_id, sent=sent, failed=failed, error="\n".join(errors[:50]))
    return {"sent": sent, "failed": failed}


def list_recent_campaigns(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, subject, scope, plan_codes, total_recipients, sent, failed,
                   status, error, created_by, created_at, finished_at
            FROM broadcast_campaigns
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def list_campaign_failures(campaign_id: int, limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT email, error, created_at
            FROM broadcast_deliveries
            WHERE campaign_id = ? AND status = 'failed'
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(campaign_id), int(limit)),
        ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]
