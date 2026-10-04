"""Confirmed snapshots, explicit reservations and restart-safe SMTP delivery."""
from __future__ import annotations

import json
import secrets
import smtplib
from datetime import timedelta, timezone

import research_updates as r

LABELS = {"scheduled": "已预约", "sending": "发送中", "paused": "已暂停", "missed": "已错过时间，需另约",
          "cancelled": "已取消", "complete": "本轮投递结束"}


def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS research_mail_schedules (
      issue_id INTEGER PRIMARY KEY REFERENCES research_issues(id),
      snapshot TEXT NOT NULL, scheduled_at TEXT NOT NULL, status TEXT NOT NULL,
      actor TEXT NOT NULL, audience_initialized INTEGER NOT NULL DEFAULT 0,
      worker TEXT NOT NULL DEFAULT '', heartbeat TEXT NOT NULL DEFAULT '',
      started_at TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS research_mail_events (
      id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL, action TEXT NOT NULL,
      actor TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
    """)


def utc(value):
    return r.parse_time(value).astimezone(timezone.utc).isoformat()


def default_time(issue):
    end = r.parse_time(issue["period_end"])
    days = (0 - end.weekday()) % 7 or 7
    return (end + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()


def local_time(value):
    return r.parse_time(value).strftime("%Y-%m-%dT%H:%M") if value else ""


def future(value):
    result = utc(value)
    if r.parse_time(result) <= r.parse_time(r.now_text()):
        raise ValueError("预约时间已经过去，请明确选择新的未来时间；不会自动补发")
    return result


def event(c, issue_id, action, actor, detail=""):
    c.execute("INSERT INTO research_mail_events(issue_id,action,actor,detail,created_at) VALUES(?,?,?,?,?)",
              (issue_id, action, actor, detail, r.now_text()))


def reserve(c, issue_id, snapshot, when, actor):
    when = future(when)
    c.execute("INSERT INTO research_mail_schedules(issue_id,snapshot,scheduled_at,status,actor,updated_at) VALUES(?,?,?,'scheduled',?,?)",
              (issue_id, r.dumps(snapshot), when, actor, r.now_text()))
    event(c, issue_id, "schedule", actor, when)


def get(issue_id):
    with r.connect() as c:
        row = c.execute("SELECT * FROM research_mail_schedules WHERE issue_id=?", (issue_id,)).fetchone()
    return dict(row) if row else None


def change(issue_id, action, actor, when="", expected_hash="", replace_snapshot=False):
    with r.connect(True) as c:
        row = c.execute("SELECT * FROM research_mail_schedules WHERE issue_id=?", (issue_id,)).fetchone()
        if not row:
            raise ValueError("该期没有邮件预约")
        if action in {"pause", "cancel"}:
            status = "paused" if action == "pause" else "cancelled"
            c.execute("UPDATE research_mail_schedules SET status=?,reason=?,updated_at=? WHERE issue_id=?",
                      (status, "管理员" + LABELS[status], r.now_text(), issue_id))
        elif action == "reschedule":
            if row["worker"]:
                raise ValueError("上一轮投递仍在退出，请稍后改期")
            when = future(when)
            snapshot = row["snapshot"]
            if replace_snapshot:
                if row["audience_initialized"]:
                    raise ValueError("已开始投递，不能替换邮件快照")
                issue = c.execute("SELECT * FROM research_issues WHERE id=?", (issue_id,)).fetchone()
                if expected_hash != r.digest(json.loads(issue["snapshot"])):
                    raise ValueError("网页预览已变化，请重新核对")
                snapshot = issue["snapshot"]
            if row["audience_initialized"] and not c.execute(
                    "SELECT 1 FROM research_deliveries WHERE issue_id=? AND status='queued'", (issue_id,)).fetchone():
                raise ValueError("没有待发送邮件；失败或结果不明请先逐封处理")
            c.execute("UPDATE research_mail_schedules SET status='scheduled',scheduled_at=?,snapshot=?,actor=?,reason='',updated_at=? WHERE issue_id=?",
                      (when, snapshot, actor, r.now_text(), issue_id))
        else:
            raise ValueError("未知预约操作")
        event(c, issue_id, action, actor, when + ("; replace snapshot" if replace_snapshot else ""))


def resolve(issue_id, delivery_id, actor, resolution):
    with r.connect(True) as c:
        row = c.execute("SELECT * FROM research_deliveries WHERE id=? AND issue_id=?", (delivery_id, issue_id)).fetchone()
        if not row or row["status"] not in {"failed", "uncertain"} or resolution not in {"sent", "queued"}:
            raise ValueError("只能逐封处理明确失败或已人工核查的邮件")
        schedule = c.execute("SELECT * FROM research_mail_schedules WHERE issue_id=?", (issue_id,)).fetchone()
        if schedule["worker"]:
            raise ValueError("请等待本轮投递结束再处理")
        c.execute("UPDATE research_deliveries SET status=?,error=?,updated_at=? WHERE id=?",
                  (resolution, "人工核查：" + actor, r.now_text(), delivery_id))
        if resolution == "queued":
            c.execute("UPDATE research_mail_schedules SET status='paused',reason='单封重试已准备，请另约时间' WHERE issue_id=?", (issue_id,))
        event(c, issue_id, "resolve", actor, f"{delivery_id}: {resolution}")


def _eligible(recipients):
    import journal_alerts as ja
    current = recipients if recipients is not None else ja.resolve_recipients("subscribers")[0]
    return {r.clean(x["email"]).lower(): x for x in current
            if recipients is not None or ja.subscription_is_deliverable(x.get("_subscription") or {})}


def deliver(base_url, limit=100, sender=None, recipients=None, smtp=None):
    import journal_alerts as ja
    sender = sender or ja.send_email
    smtp = smtp or ja.load_smtp_config()
    result = dict(sent=0, failed=0, uncertain=0, skipped=0)
    now = utc(r.now_text())
    worker = secrets.token_hex(16)
    with r.connect(True) as c:
        # An interrupted owner is never replaced by a second automatic sender.
        cutoff = (r.parse_time(now) - timedelta(minutes=15)).astimezone(timezone.utc).isoformat()
        abandoned = c.execute("SELECT issue_id FROM research_mail_schedules WHERE worker!='' AND heartbeat<?", (cutoff,)).fetchall()
        for stale in abandoned:
            iid = stale["issue_id"]
            c.execute("UPDATE research_deliveries SET status='uncertain',error='发送进程中断，需核查实际收信' WHERE issue_id=? AND status='sending'", (iid,))
            c.execute("UPDATE research_mail_schedules SET status='paused',worker='',reason='发送进程中断，需人工核查并另约',updated_at=? WHERE issue_id=?", (now, iid))
            event(c, iid, "interrupted", "worker")
        rows = c.execute("SELECT * FROM research_mail_schedules WHERE worker='' AND status IN ('scheduled','sending') ORDER BY scheduled_at").fetchall()
        selected = None
        for row in rows:
            if row["status"] == "scheduled":
                delay = (r.parse_time(now) - r.parse_time(row["scheduled_at"])).total_seconds()
                if delay < 0:
                    continue
                if delay >= 60:
                    c.execute("UPDATE research_mail_schedules SET status='missed',reason='错过预约启动窗口，请另约时间',updated_at=? WHERE issue_id=?", (now, row["issue_id"]))
                    event(c, row["issue_id"], "missed", "worker")
                    continue
            selected = dict(row)
            c.execute("UPDATE research_mail_schedules SET status='sending',worker=?,heartbeat=?,updated_at=? WHERE issue_id=?",
                      (worker, now, now, row["issue_id"]))
            break
    if selected is None:
        return result
    iid = selected["issue_id"]
    try:
        if hasattr(smtp, "enabled") and not smtp.enabled:
            raise ValueError("SMTP尚未配置，需修复后另约")
        audience = _eligible(recipients) if not selected["audience_initialized"] else None
        with r.connect(True) as c:
            live = c.execute("SELECT * FROM research_mail_schedules WHERE issue_id=?", (iid,)).fetchone()
            if live["status"] != "sending" or live["worker"] != worker:
                return result
            if audience is not None:
                for email, recipient in audience.items():
                    c.execute("INSERT OR IGNORE INTO research_deliveries(issue_id,email,recipient,snapshot,updated_at) VALUES(?,?,?,?,?)",
                              (iid, email, r.dumps(recipient), selected["snapshot"], now))
                c.execute("UPDATE research_mail_schedules SET audience_initialized=1,started_at=? WHERE issue_id=?", (now, iid))
                event(c, iid, "start", "worker", f"{len(audience)} recipients")
        for _ in range(limit):
            with r.connect(True) as c:
                live = c.execute("SELECT * FROM research_mail_schedules WHERE issue_id=?", (iid,)).fetchone()
                if live["status"] != "sending" or live["worker"] != worker:
                    break
                row = c.execute("SELECT * FROM research_deliveries WHERE issue_id=? AND status='queued' ORDER BY id LIMIT 1", (iid,)).fetchone()
                if not row:
                    break
                c.execute("UPDATE research_deliveries SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?", (r.now_text(), row["id"]))
                c.execute("UPDATE research_mail_schedules SET heartbeat=? WHERE issue_id=?", (utc(r.now_text()), iid))
            status, error = "sent", ""
            try:
                eligible = _eligible(recipients)
                if row["email"] not in eligible:
                    status, error = "skipped", "已退订或会员资格失效"
                else:
                    snapshot = json.loads(row["snapshot"])
                    plain, rich = r.render_email(snapshot, eligible[row["email"]], base_url)
                    sender(smtp, row["email"], r.TITLE + " · " + snapshot["period_end"][:10], plain, rich)
            except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused) as exc:
                status, error = "failed", type(exc).__name__ + ": " + str(exc)[:200]
            except (ConnectionError, TimeoutError, OSError) as exc:
                status, error = "uncertain", type(exc).__name__ + ": " + str(exc)[:200]
            except Exception as exc:
                status, error = "failed", type(exc).__name__ + ": " + str(exc)[:200]
            with r.connect(True) as c:
                c.execute("UPDATE research_deliveries SET status=?,error=?,updated_at=? WHERE id=?", (status, error, r.now_text(), row["id"]))
            result[status] += 1
    except BaseException:
        with r.connect(True) as c:
            c.execute("UPDATE research_deliveries SET status='uncertain',error='投递中断，需核查' WHERE issue_id=? AND status='sending'", (iid,))
            c.execute("UPDATE research_mail_schedules SET status='paused',reason='运行中断或配置不可用，请检查后另约' WHERE issue_id=? AND worker=?", (iid, worker))
            event(c, iid, "paused", "worker", "运行异常")
        raise
    finally:
        with r.connect(True) as c:
            pending = c.execute("SELECT 1 FROM research_deliveries WHERE issue_id=? AND status IN ('queued','sending')", (iid,)).fetchone()
            if not pending:
                c.execute("UPDATE research_mail_schedules SET status='complete' WHERE issue_id=? AND status='sending' AND worker=?", (iid, worker))
            c.execute("UPDATE research_mail_schedules SET worker='',heartbeat='',updated_at=? WHERE issue_id=? AND worker=?", (r.now_text(), iid, worker))
    return result
