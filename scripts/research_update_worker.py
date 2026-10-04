"""Server collector and durable job/mail worker; no automatic publication."""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_updates as r
import research_collect as collect


def work_once():
    # A process crash makes old jobs retryable. Collection/import are idempotent;
    # SMTP has a separate uncertain state and is never blindly replayed.
    with r.connect(True) as c:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        c.execute("UPDATE research_jobs SET status='queued' WHERE status='running' AND updated_at<?", (cutoff,))
        job = c.execute("SELECT * FROM research_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if job:
            c.execute("UPDATE research_jobs SET status='running',updated_at=? WHERE id=?", (r.now_text(), job["id"]))
    if not job:
        return
    status, error = "done", ""
    try:
        args = json.loads(job["args"])
        if job["kind"] == "collect":
            reports = collect.collect(args["issue_id"], args.get("source_id", ""), backfill=args.get("backfill", False),
                                      progress=lambda report: print(r.dumps(report), flush=True))
            if any(x["status"] in {"failed", "partial"} for x in reports):
                status, error = "partial", "部分来源未完成，详见逐刊记录，可单刊重试"
            r.enqueue("translate", {"issue_id": args["issue_id"]})
        else:
            from ai import ZAIClient, load_ai_config
            result = collect.translate(args["issue_id"], ZAIClient(load_ai_config()))
            print(r.dumps(result), flush=True)
            if result["failed"]:
                status, error = "partial", "部分译文失败，原文仍保留，请检查翻译通道后重试"
    except Exception as exc:
        status, error = "failed", type(exc).__name__ + ": " + str(exc)[:300]
    with r.connect(True) as c:
        c.execute("UPDATE research_jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, r.now_text(), job["id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["schedule", "work", "send", "collect", "translate"], required=True)
    parser.add_argument("--end", help="Explicit Beijing week cutoff, e.g. 2026-10-03T22:00:00+08:00")
    parser.add_argument("--source", default="")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--probe-only", action="store_true", help="Probe all publisher entrypoints without collecting articles")
    args = parser.parse_args()
    r.init_db()
    if args.probe_only:
        http = collect.HTTP(retries=0)
        for source in collect.sources():
            try:
                body = http.get(source["toc_url"])
                status = "accessible" if len(body) > 500 else "empty"
                report = {"source": source["name"], "issn": source["id"], "url": source["toc_url"], "status": status, "bytes": len(body.encode())}
            except Exception as exc:
                report = {"source": source["name"], "issn": source["id"], "url": source["toc_url"], "status": "failed", "error": str(exc)[:200]}
            print(r.dumps(report), flush=True)
        return
    if args.stage == "send":
        from journal_alerts import public_base_url
        from runtime_env import load_deployment_settings
        print(r.dumps(r.deliver(public_base_url(load_deployment_settings()))))
    elif args.stage == "work":
        work_once()
    else:
        issue = r.issue_for(args.end)
        if issue["status"] != "draft":
            print("Issue already published; scheduled collection skipped")
            return
        kind = "translate" if args.stage == "translate" else "collect"
        r.enqueue(kind, {"issue_id": issue["id"], "source_id": args.source, "backfill": args.backfill})
        if args.stage != "schedule":
            work_once()


if __name__ == "__main__":
    main()
