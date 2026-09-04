#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def prepare(review_db: Path, output_root: Path, live_db: Path) -> dict:
    with connect(review_db) as conn:
        rows = conn.execute(
            "SELECT * FROM repair_batches WHERE state='confirmed' AND promotion_status='requested' "
            "ORDER BY confirmed_at,id LIMIT 2"
        ).fetchall()
        if not rows:
            return {"pending": False}
        if len(rows) != 1:
            raise RuntimeError("more than one promotion request is pending")
        row = rows[0]
        batch_id = str(row["id"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,100}", batch_id):
            raise RuntimeError("invalid batch id")
        candidate = Path(str(row["candidate_database"] or "")).resolve()
        expected_parent = (output_root / "batches" / batch_id).resolve()
        try:
            candidate.relative_to(expected_parent)
        except ValueError as exc:
            raise RuntimeError("candidate path escapes approved batch directory") from exc
        if candidate.name != "candidate-corpus.sqlite" or not candidate.is_file():
            raise RuntimeError("candidate database is missing")
        validation = json.loads(str(row["validation_json"] or "{}"))
        if not validation.get("gates_ok") or int(validation.get("production_write_count", -1)) != 0:
            raise RuntimeError("candidate validation is not approved")
        candidate_hash = digest(candidate)
        if candidate_hash != str(row["candidate_database_sha256"] or ""):
            raise RuntimeError("candidate hash mismatch")
        live_hash = digest(live_db)
        if live_hash != str(row["source_database_sha256"] or ""):
            message = "live database changed after candidate generation"
            conn.execute(
                "UPDATE repair_batches SET state='stale',promotion_status='failed',promotion_error=? WHERE id=?",
                (message, batch_id),
            )
            conn.execute(
                "INSERT INTO repair_events(batch_id,at,level,event,detail) VALUES(?,?,?,?,?)",
                (batch_id, now(), "error", "promotion_stale", message),
            )
            conn.commit()
            raise RuntimeError(message)
        unresolved = int(conn.execute(
            "SELECT COUNT(*) FROM repair_issues WHERE batch_id=? "
            "AND status IN ('detected','ai_reviewed','manual_review')", (batch_id,)
        ).fetchone()[0])
        if unresolved:
            raise RuntimeError("confirmed batch contains unresolved review items")
    return {
        "pending": True,
        "batch_id": batch_id,
        "candidate": str(candidate),
        "candidate_sha256": candidate_hash,
        "source_sha256": live_hash,
        "phase": int(row["phase"]),
    }


def mark(review_db: Path, batch_id: str, status: str, error: str = "") -> dict:
    if status not in {"running", "requested", "failed", "applied", "stale"}:
        raise ValueError("invalid promotion status")
    with connect(review_db) as conn:
        row = conn.execute("SELECT state,promotion_status FROM repair_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise LookupError("batch not found")
        if status == "running" and (str(row["state"]) != "confirmed" or str(row["promotion_status"]) != "requested"):
            raise RuntimeError("batch is not eligible for promotion")
        state = "applied" if status == "applied" else ("stale" if status == "stale" else str(row["state"]))
        stored_status = "failed" if status == "stale" else status
        conn.execute(
            "UPDATE repair_batches SET state=?,promotion_status=?,promotion_error=? WHERE id=?",
            (state, stored_status, str(error or "")[:2000], batch_id),
        )
        if status == "applied":
            conn.execute(
                "UPDATE repair_issues SET status='applied',applied_at=? "
                "WHERE batch_id=? AND status IN ('automatic_candidate','approved')",
                (now(), batch_id),
            )
        conn.execute(
            "INSERT INTO repair_events(batch_id,at,level,event,detail) VALUES(?,?,?,?,?)",
            (batch_id, now(), "error" if status == "failed" else "info", "promotion_" + status,
             str(error or "")[:2000]),
        )
        conn.commit()
    return {"batch_id": batch_id, "promotion_status": stored_status, "state": state}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--review-db", type=Path, required=True)
    prep.add_argument("--output-root", type=Path, required=True)
    prep.add_argument("--live-db", type=Path, required=True)
    update = sub.add_parser("mark")
    update.add_argument("--review-db", type=Path, required=True)
    update.add_argument("--batch-id", required=True)
    update.add_argument("--status", required=True)
    update.add_argument("--error", default="")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        payload = prepare(args.review_db.resolve(), args.output_root.resolve(), args.live_db.resolve())
    else:
        payload = mark(args.review_db.resolve(), args.batch_id, args.status, args.error)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
