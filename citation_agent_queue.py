from __future__ import annotations

"""Small privacy-filtered SQLite/WAL queue shared by the two isolated workers."""

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOCAL_QUEUE_ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.cwd()) / "marx-citation-agent"
QUEUE_PATH = Path(os.environ.get("CITATION_AGENT_QUEUE_PATH") or (_LOCAL_QUEUE_ROOT / "queue.sqlite3"))
MAX_FRAGMENT_CHARS = 320
MAX_BATCH_RECORDS = 8
MAX_ACTIONS_PER_RESPONSE = 16
_OPAQUE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_BOOK_KEY_RE = re.compile(r"^[\w-]{1,64}$", re.UNICODE)
_FORBIDDEN_KEYS = {
    "filename", "file_name", "source_file", "path", "user", "user_id", "account",
    "email", "author", "paper", "document", "docx", "pdf", "prompt", "raw_response",
    "context", "corpus_text", "note_text", "existing_note_text",
}


class AgentQueueError(ValueError):
    pass


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()


def _lease_owner(worker_id: str) -> str:
    """Return the exact owner token persisted and compared for queue leases."""
    owner = re.sub(r"[^A-Za-z0-9_.:-]", "", str(worker_id or ""))[:100]
    if not owner:
        raise AgentQueueError("Agent worker 标识无效。")
    return owner


@contextmanager
def _connect(path: Path = QUEUE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_queue(path: Path = QUEUE_PATH) -> Path:
    with _connect(path) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS citation_agent_queue (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT NOT NULL DEFAULT '',
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_caq_status_created
              ON citation_agent_queue(status,created_at);
            """
        )
    try:
        # A dedicated setgid group is the only shared trust boundary between the
        # two system users.  Raw-paper directories remain 0700 elsewhere.
        os.chmod(path.parent, 0o2770)
        os.chmod(path, 0o660)
        for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
            if sidecar.exists():
                os.chmod(sidecar, 0o660)
    except OSError:
        pass
    return path


def _reject_forbidden_keys(value, *, prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            clean = str(key).strip().lower()
            if clean in _FORBIDDEN_KEYS:
                raise AgentQueueError(f"脱敏请求包含禁止字段：{prefix}{clean}")
            _reject_forbidden_keys(child, prefix=f"{prefix}{clean}.")
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_keys(child, prefix=prefix)


def validate_request(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise AgentQueueError("Agent 请求必须是对象。")
    _reject_forbidden_keys(payload)
    allowed_top = {
        "protocol_version", "prompt_version", "round", "recognition_depth",
        "planning_effort", "allowed_public_books", "personal_source_ids", "records", "observations",
    }
    if set(payload) - allowed_top:
        raise AgentQueueError("Agent 请求包含未授权字段。")
    depth = str(payload.get("recognition_depth") or "direct_only")
    if depth not in {"direct_only", "direct_and_paraphrase"}:
        raise AgentQueueError("识别深度无效。")
    round_no = int(payload.get("round") or 1)
    if round_no not in {1, 2}:
        raise AgentQueueError("Agent 轮次无效。")
    planning_effort = str(payload.get("planning_effort") or "deep")
    if planning_effort not in {"deep", "fast"}:
        raise AgentQueueError("Agent 规划强度无效。")
    books = payload.get("allowed_public_books") or []
    personal = payload.get("personal_source_ids") or []
    if not isinstance(books, list) or len(books) > 100 or not all(_BOOK_KEY_RE.fullmatch(str(x)) for x in books):
        raise AgentQueueError("公共书库键无效。")
    if not isinstance(personal, list) or len(personal) > 100 or not all(_OPAQUE_ID_RE.fullmatch(str(x)) for x in personal):
        raise AgentQueueError("个人书编号无效。")
    records = payload.get("records") or []
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH_RECORDS:
        raise AgentQueueError("每批记录必须为 1—8 条。")
    clean_records: list[dict] = []
    for item in records:
        if not isinstance(item, dict) or set(item) - {"record_id", "kind", "fragment"}:
            raise AgentQueueError("脱敏记录结构无效。")
        record_id = str(item.get("record_id") or "")
        kind = str(item.get("kind") or "direct")
        fragment = str(item.get("fragment") or "").strip()
        if not _OPAQUE_ID_RE.fullmatch(record_id) or kind not in {"note_anchor", "quote", "direct", "paraphrase"}:
            raise AgentQueueError("脱敏记录标识无效。")
        if not fragment or len(fragment) > MAX_FRAGMENT_CHARS:
            raise AgentQueueError("脱敏片段为空或超出 320 字。")
        clean_records.append({"record_id": record_id, "kind": kind, "fragment": fragment})
    observations = payload.get("observations") or []
    if not isinstance(observations, list) or len(observations) > MAX_ACTIONS_PER_RESPONSE:
        raise AgentQueueError("观察结果数量无效。")
    clean_observations: list[dict] = []
    for item in observations:
        if not isinstance(item, dict) or set(item) - {
            "record_id", "action_id", "tool", "fragment", "keywords",
            "hit_count", "exact_count", "unique_source_count", "result",
        }:
            raise AgentQueueError("观察结果结构无效。")
        result = str(item.get("result") or "none")
        if result not in {"none", "ambiguous", "near", "unique_exact", "rejected"}:
            raise AgentQueueError("观察结果状态无效。")
        tool = str(item.get("tool") or "")
        if tool not in {"exact_fragment", "near_quote", "keyword_cooccurrence"}:
            raise AgentQueueError("观察结果工具无效。")
        fragment = str(item.get("fragment") or "").strip()[:240]
        keywords = item.get("keywords") or []
        if not isinstance(keywords, list):
            raise AgentQueueError("观察结果关键词无效。")
        clean_observations.append({
            "record_id": str(item.get("record_id") or "")[:32],
            "action_id": str(item.get("action_id") or "")[:32],
            "tool": tool, "fragment": fragment,
            "keywords": [str(value).strip()[:24] for value in keywords[:5]],
            "hit_count": max(0, min(int(item.get("hit_count") or 0), 100)),
            "exact_count": max(0, min(int(item.get("exact_count") or 0), 100)),
            "unique_source_count": max(0, min(int(item.get("unique_source_count") or 0), 100)),
            "result": result,
        })
    return {
        "protocol_version": 1,
        "prompt_version": str(payload.get("prompt_version") or "citation-agent-v1")[:80],
        "round": round_no,
        "planning_effort": planning_effort,
        "recognition_depth": depth,
        "allowed_public_books": list(dict.fromkeys(str(x) for x in books)),
        "personal_source_ids": list(dict.fromkeys(str(x) for x in personal)),
        "records": clean_records,
        "observations": clean_observations,
    }


def validate_response(payload: dict, request_payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) - {"actions"}:
        raise AgentQueueError("Agent 回答必须只包含 actions。")
    actions = payload.get("actions") or []
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS_PER_RESPONSE:
        raise AgentQueueError("Agent 动作数量超出上限。")
    record_map = {str(item["record_id"]): item for item in request_payload.get("records") or []}
    allow_paraphrase = request_payload.get("recognition_depth") == "direct_and_paraphrase"
    clean: list[dict] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(actions):
        if not isinstance(item, dict) or set(item) - {"action_id", "record_id", "tool", "fragment", "keywords"}:
            raise AgentQueueError("Agent 动作结构无效。")
        action_id = str(item.get("action_id") or f"a{index + 1}")
        record_id = str(item.get("record_id") or "")
        tool = str(item.get("tool") or "")
        fragment = str(item.get("fragment") or "").strip()
        keywords = item.get("keywords") or []
        if not _OPAQUE_ID_RE.fullmatch(action_id) or action_id in seen_ids:
            raise AgentQueueError("Agent 动作编号无效或重复。")
        if record_id not in record_map:
            raise AgentQueueError("Agent 动作引用了批次外记录。")
        if tool not in {"exact_fragment", "near_quote", "keyword_cooccurrence"}:
            raise AgentQueueError("Agent 请求了未授权工具。")
        if tool == "keyword_cooccurrence" and not allow_paraphrase:
            raise AgentQueueError("直接引文模式禁止观点转述工具。")
        record_kind = str(record_map[record_id].get("kind") or "direct")
        if record_kind == "paraphrase" and tool != "keyword_cooccurrence":
            raise AgentQueueError("观点转述记录只能使用关键词共现工具。")
        if record_kind != "paraphrase" and tool == "keyword_cooccurrence":
            raise AgentQueueError("非转述记录禁止使用关键词共现工具。")
        if tool in {"exact_fragment", "near_quote"}:
            if not 6 <= len(fragment) <= 240:
                raise AgentQueueError("检索片段长度无效。")
            keywords = []
        else:
            if not isinstance(keywords, list) or not 3 <= len(keywords) <= 5:
                raise AgentQueueError("关键词数量无效。")
            keywords = [str(value).strip()[:24] for value in keywords]
            if any(len(value) < 2 for value in keywords):
                raise AgentQueueError("关键词过短。")
            fragment = ""
        seen_ids.add(action_id)
        clean.append({
            "action_id": action_id, "record_id": record_id, "tool": tool,
            "fragment": fragment, "keywords": keywords,
        })
    return {"actions": clean}


def enqueue(payload: dict, *, ttl_seconds: int = 180, path: Path = QUEUE_PATH) -> str:
    clean = validate_request(payload)
    task_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    init_queue(path)
    with _connect(path) as conn:
        conn.execute(
            """INSERT INTO citation_agent_queue
               (id,status,request_json,response_json,error_code,lease_owner,lease_expires_at,
                created_at,updated_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, "queued", json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                "{}", "", "", "", _iso(now), _iso(now),
                _iso(now + timedelta(seconds=max(30, int(ttl_seconds)))),
            ),
        )
    return task_id


def claim(worker_id: str, *, lease_seconds: int = 120, path: Path = QUEUE_PATH) -> dict | None:
    owner = _lease_owner(worker_id)
    now = datetime.now(timezone.utc)
    now_text = _iso(now)
    lease_until = _iso(now + timedelta(seconds=max(30, int(lease_seconds))))
    init_queue(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT * FROM citation_agent_queue
               WHERE status IN ('queued','running') AND expires_at>?
                 AND (status='queued' OR lease_expires_at='' OR lease_expires_at<=?)
               ORDER BY created_at LIMIT 1""",
            (now_text, now_text),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        changed = conn.execute(
            """UPDATE citation_agent_queue SET status='running',lease_owner=?,lease_expires_at=?,updated_at=?
               WHERE id=? AND (status='queued' OR lease_expires_at='' OR lease_expires_at<=?)""",
            (owner, lease_until, now_text, str(row["id"]), now_text),
        ).rowcount
        conn.commit()
    if not changed:
        return None
    return {"id": str(row["id"]), "request": json.loads(str(row["request_json"] or "{}"))}


def complete(task_id: str, worker_id: str, response: dict, *, path: Path = QUEUE_PATH) -> None:
    owner = _lease_owner(worker_id)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT request_json FROM citation_agent_queue WHERE id=? AND status='running' AND lease_owner=?",
            (str(task_id), owner),
        ).fetchone()
        if row is None:
            raise AgentQueueError("Agent 队列租约已失效。")
        request_payload = json.loads(str(row[0] or "{}"))
        clean = validate_response(response, request_payload)
        conn.execute(
            """UPDATE citation_agent_queue SET status='complete',response_json=?,error_code='',
               lease_owner='',lease_expires_at='',updated_at=? WHERE id=? AND lease_owner=?""",
            (json.dumps(clean, ensure_ascii=False, separators=(",", ":")), _iso(), str(task_id), owner),
        )


def fail(task_id: str, worker_id: str, error_code: str, *, path: Path = QUEUE_PATH) -> None:
    owner = _lease_owner(worker_id)
    with _connect(path) as conn:
        conn.execute(
            """UPDATE citation_agent_queue SET status='failed',response_json='{}',error_code=?,
               lease_owner='',lease_expires_at='',updated_at=? WHERE id=? AND lease_owner=?""",
            (str(error_code or "agent_error")[:80], _iso(), str(task_id), owner),
        )


def result(task_id: str, *, path: Path = QUEUE_PATH) -> dict | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT status,response_json,error_code,expires_at FROM citation_agent_queue WHERE id=?",
            (str(task_id),),
        ).fetchone()
    if row is None:
        return {"status": "missing", "response": {}, "error_code": "missing"}
    status = str(row["status"] or "")
    if str(row["expires_at"] or "") <= _iso() and status not in {"complete", "failed"}:
        status = "expired"
    try:
        response = json.loads(str(row["response_json"] or "{}"))
    except json.JSONDecodeError:
        response = {}
    return {"status": status, "response": response, "error_code": str(row["error_code"] or "")}


def wait_result(task_id: str, *, timeout_seconds: float, path: Path = QUEUE_PATH) -> dict:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while time.monotonic() < deadline:
        current = result(task_id, path=path) or {}
        if current.get("status") in {"complete", "failed", "expired", "missing"}:
            return current
        time.sleep(0.2)
    return {"status": "timeout", "response": {}, "error_code": "timeout"}


def delete(task_id: str, *, path: Path = QUEUE_PATH) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM citation_agent_queue WHERE id=?", (str(task_id),))


def cleanup(*, path: Path = QUEUE_PATH) -> int:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM citation_agent_queue WHERE expires_at<=? OR "
            "(status IN ('complete','failed') AND updated_at<=?)",
            (_iso(), _iso(datetime.now(timezone.utc) - timedelta(minutes=10))),
        )
    return max(0, int(cursor.rowcount or 0))
