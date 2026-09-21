"""Atomic cross-generation claims with the established queue order and limits."""
from __future__ import annotations
import re
from datetime import timedelta


def citation(tasks, versions, worker_id, lease_seconds=600, *, corpus_sha256=None, template_version=None):
    owner = re.sub(r'[^A-Za-z0-9_.:-]', '', str(worker_id or ''))[:100]
    if not owner:
        raise tasks.CitationAssistantError('工作节点标识无效。')
    now = tasks._utcnow()
    stamp = tasks._iso(now)
    until = tasks._iso(now + timedelta(seconds=max(60, int(lease_seconds))))
    clause = ' AND corpus_sha256 IN (' + ','.join('?' for _ in versions) + ')'
    values = list(versions)
    if template_version is not None:
        clause += ' AND template_version=?'
        values.append(str(template_version))
    with tasks._connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT * FROM citation_assistant_jobs WHERE "
                        "status IN ('extracting','queued','matching','exporting') AND expires_at>? "
                        "AND (lease_owner='' OR lease_expires_at='' OR lease_expires_at<=?) " + clause +
                        " AND (status NOT IN ('queued','matching') OR "
                        "(SELECT COUNT(*) FROM citation_assistant_jobs active WHERE active.status='matching' "
                        "AND active.lease_owner!='' AND active.lease_expires_at>?) < ?) "
                        "ORDER BY CASE status WHEN 'exporting' THEN 0 WHEN 'extracting' THEN 1 ELSE 2 END,created_at LIMIT 1",
                        (stamp, stamp, *values, stamp, tasks.MATCH_CONCURRENCY)).fetchone()
        if row is None:
            c.commit()
            return None
        stage = row['status']
        c.execute("UPDATE citation_assistant_jobs SET status=?,lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?",
                  ('matching' if stage in {'queued','matching'} else stage, owner, until, stamp, row['id']))
        c.commit()
    job = tasks.get_job(row['id'])
    if job is not None:
        job['claimed_stage'] = stage
    return job


def search_export(tasks, versions, worker_id, lease_seconds=None, *, corpus_version=None, template_version=None):
    tasks.init_db()
    now = tasks.utc_now()
    stamp = tasks.utc_text(now)
    until = tasks.utc_text(now + timedelta(seconds=max(60, int(lease_seconds or tasks.MAX_RUNTIME_SECONDS + 60))))
    clause = ' AND corpus_version IN (' + ','.join('?' for _ in versions) + ')'
    values = list(versions)
    if template_version is not None:
        clause += ' AND template_version=?'
        values.append(str(template_version))
    with tasks._connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for condition, status, error in [('<','queued','工作节点中断，任务已自动重试。'),
                                         ('>=','failed','工作节点连续中断，任务已停止。')]:
            c.execute("UPDATE search_export_jobs SET status=?,lease_owner='',lease_expires_at=NULL,error=?,updated_at=? "
                      "WHERE status IN ('counting','collecting','rendering','packaging') "
                      "AND lease_expires_at IS NOT NULL AND lease_expires_at<? AND attempts" + condition + '?',
                      (status, error, stamp, stamp, tasks.MAX_ATTEMPTS))
        row = c.execute("SELECT id FROM search_export_jobs WHERE status='queued' AND expires_at>? AND attempts<? "
                        + clause + ' ORDER BY created_at LIMIT 1', (stamp,tasks.MAX_ATTEMPTS,*values)).fetchone()
        if row is None:
            c.commit()
            return None
        c.execute("UPDATE search_export_jobs SET status='counting',attempts=attempts+1,lease_owner=?,lease_expires_at=?,"
                  "error='',updated_at=? WHERE id=?", (str(worker_id)[:200],until,stamp,row['id']))
        claimed = c.execute('SELECT * FROM search_export_jobs WHERE id=?',(row['id'],)).fetchone()
        c.commit()
    return tasks._decode_job(claimed)
