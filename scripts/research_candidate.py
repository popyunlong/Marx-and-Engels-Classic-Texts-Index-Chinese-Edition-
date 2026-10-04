"""Portable draft handoff. Never replace the shared database or import approvals/mail."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_updates as r


def export_draft(issue_id: int) -> dict:
    issue = r.get_issue(issue_id)
    if not issue or issue["status"] != "draft" or issue["parent_id"]:
        raise ValueError("只导出普通未发布草稿")
    rows = [x for x in r.items(issue_id) if x["review"] != "excluded"]
    sources = {x["article"].get("source_id") for x in rows if x["article"]["origin"] == "domestic"}
    with r.connect() as c:
        imports = [json.loads(x["payload"]) for x in c.execute("SELECT * FROM research_imports ORDER BY id") if x["source_id"] in sources]
        runs = [dict(x) for x in c.execute("SELECT source_id,status,report,created_at FROM research_runs WHERE id IN (SELECT max(id) FROM research_runs WHERE issue_id=? GROUP BY source_id)", (issue_id,))]
    payload = {"version": 1, "period_end": issue["period_end"], "articles": [x["article"] for x in rows if x["article"]["origin"] == "foreign"], "imports": imports, "runs": runs}
    return {"sha256": r.digest(payload), "payload": payload}


def import_draft(bundle: dict) -> dict:
    payload = bundle["payload"]
    if payload.get("version") != 1 or r.digest(payload) != bundle.get("sha256"):
        raise ValueError("候选资料版本或校验值不匹配")
    key = "bundle:" + bundle["sha256"]
    with r.connect() as c:
        previous = c.execute("SELECT data FROM research_state WHERE key=?", (key,)).fetchone()
    if previous:
        return {**json.loads(previous[0]), "replayed": True}
    issue = r.issue_for(payload["period_end"])
    if issue["status"] != "draft":
        raise ValueError("目标期次已发布；请通过后台更正流程处理")
    # Replays preserve domestic document provenance and version links.
    domestic = [r.import_payload(p, issue["id"]) for p in payload["imports"]]
    for a in payload["articles"]:
        if a.get("origin") != "foreign":
            raise ValueError("外刊候选条目的来源类型不正确")
        r.upsert(issue["id"], {**a, "verified_fields": []})
    result = {"issue_id": issue["id"], "domestic_imports": len(domestic), "foreign": len(payload["articles"]), "status": "draft"}
    with r.connect(True) as c:
        for run in payload["runs"]:
            c.execute("INSERT INTO research_runs(issue_id,source_id,status,report,created_at) VALUES(?,?,?,?,?)", (issue["id"], run["source_id"], run["status"], run["report"], run["created_at"]))
        c.execute("INSERT OR REPLACE INTO research_state VALUES(?,?)", (key, r.dumps(result)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "import"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--issue", type=int)
    args = parser.parse_args()
    r.init_db()
    if args.action == "export":
        args.path.parent.mkdir(parents=True, exist_ok=True)
        args.path.write_text(r.dumps(export_draft(args.issue)), encoding="utf-8")
        print("Draft exported; no credentials, approval states or deliveries included.")
    else:
        if args.path.stat().st_size > 20_000_000:
            raise ValueError("候选资料超过 20 MB")
        print(r.dumps(import_draft(json.loads(args.path.read_text(encoding="utf-8")))))


if __name__ == "__main__":
    main()
