"""Research bulletin storage: independent metadata, reviewed snapshots and durable mail.

The legacy bilingual journal tables are deliberately never rewritten here.
All writes use short SQLite transactions; network/AI/SMTP calls occur outside them.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from journal_storage import JOURNAL_DB_PATH, ensure_journal_storage
import research_content as content
from research_journals import chinese_name as journal_chinese_name, display_name as journal_display_name

DB_PATH = JOURNAL_DB_PATH
TITLE = "本周国内外研究动态"
TYPE_LABELS = {"article": "研究论文", "journal-article": "研究论文", "review": "书评", "editorial": "编者按／评论", "commentary": "评论／回应", "interview": "访谈", "letter": "学术通信", "erratum": "更正", "peer-review": "评议", "other": "资料与其他内容"}
TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
MISSING = {"原文未提供", "未提供", "暂无", "无", "待补充", "null", "None"}


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


@contextmanager
def connect(write: bool = False):
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    if Path(DB_PATH) == Path(JOURNAL_DB_PATH):
        ensure_journal_storage()
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS research_issues (
          id INTEGER PRIMARY KEY, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft', parent_id INTEGER REFERENCES research_issues(id),
          revision INTEGER NOT NULL DEFAULT 0, snapshot TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, published_at TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '');
        CREATE UNIQUE INDEX IF NOT EXISTS research_week ON research_issues(period_end) WHERE parent_id IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS research_correction ON research_issues(parent_id) WHERE status='draft' AND parent_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS research_entries (
          id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE, data TEXT NOT NULL,
          first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_aliases (identity TEXT PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES research_entries(id));
        CREATE TABLE IF NOT EXISTS research_items (
          issue_id INTEGER REFERENCES research_issues(id), entry_id INTEGER REFERENCES research_entries(id),
          data TEXT NOT NULL, review TEXT NOT NULL DEFAULT 'pending', section TEXT NOT NULL DEFAULT 'new',
          actor TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
          PRIMARY KEY(issue_id,entry_id));
        CREATE TABLE IF NOT EXISTS research_revisions (
          id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL, revision INTEGER NOT NULL,
          snapshot TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(issue_id,revision));
        CREATE TABLE IF NOT EXISTS research_imports (
          id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, content_hash TEXT NOT NULL,
          payload TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(source_id,content_hash));
        CREATE TABLE IF NOT EXISTS research_tokens (
          id INTEGER PRIMARY KEY, label TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_deliveries (
          id INTEGER PRIMARY KEY, issue_id INTEGER REFERENCES research_issues(id), email TEXT NOT NULL,
          recipient TEXT NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
          UNIQUE(issue_id,email));
        CREATE TABLE IF NOT EXISTS research_runs (
          id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL, source_id TEXT NOT NULL,
          status TEXT NOT NULL, report TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_state (key TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_translations (hash TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_jobs (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
        from research_delivery import init as init_mail
        init_mail(c)


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def weekly_window(at: datetime | None = None) -> tuple[str, str]:
    current = (at or datetime.now(TZ)).astimezone(TZ)
    end = current.replace(hour=22, minute=0, second=0, microsecond=0)
    end -= timedelta(days=(current.weekday() - 5) % 7)
    if end > current:
        end -= timedelta(days=7)
    return (end - timedelta(days=7)).isoformat(), end.isoformat()


def issue_for(end: str | None = None) -> dict:
    start, stop = weekly_window()
    if end:
        stop = parse_time(end).isoformat()
        start = (parse_time(stop) - timedelta(days=7)).isoformat()
    with connect(True) as c:
        c.execute("INSERT OR IGNORE INTO research_issues(period_start,period_end,created_at) VALUES(?,?,?)",
                  (start, stop, now_text()))
        return dict(c.execute("SELECT * FROM research_issues WHERE period_end=? AND parent_id IS NULL", (stop,)).fetchone())


def get_issue(issue_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM research_issues WHERE id=?", (issue_id,)).fetchone()
        return dict(r) if r else None


def issues(public: bool = False) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM research_issues " +
                ("WHERE status='published' " if public else "") + "ORDER BY period_end DESC,id DESC LIMIT 200")]


def items(issue_id: int) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "article": json.loads(r["data"])} for r in c.execute(
            "SELECT * FROM research_items WHERE issue_id=? ORDER BY entry_id", (issue_id,))]


def clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in MISSING else text


def safe_url(value: Any) -> str:
    text = clean(value)
    parts = urlsplit(text)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def normalize(raw: dict) -> dict:
    a = dict(raw)
    for k in ("title", "title_zh", "journal", "abstract", "abstract_zh", "year", "volume", "issue",
              "total_issue", "pages", "page_start", "page_end", "article_number", "doi", "issn",
              "published_at", "published_online", "published_print", "source_published_at", "discipline"):
        a[k] = clean(a.get(k))
    a["doi"] = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", unquote(a["doi"]), flags=re.I).lower()
    for k in ("authors", "authors_zh", "keywords", "keywords_zh", "affiliations"):
        value = a.get(k) or []
        if isinstance(value, str):
            value = re.split(r"[、;；]", value)
        a[k] = [clean(x) for x in value if clean(x)]
    a["url"] = safe_url(a.get("url"))
    a["origin"] = a.get("origin") if a.get("origin") in {"domestic", "foreign"} else "foreign"
    a["type"] = clean(a.get("type")) or "article"
    content.apply(a)
    if "ordinal" not in a:
        m = re.match(r"\d+", a["page_start"] or a["pages"])
        a["ordinal"] = int(m[0]) if m else 999999
    if not a["discipline"]:
        from journal_taxonomy import DISCIPLINES
        a["discipline"] = next((d for d in DISCIPLINES if d in str(a.get("section_name", ""))), "")
    if not a["title"] or not a["journal"] or not a["url"]:
        raise ValueError("条目必须有题名、期刊和有效来源链接")
    a.setdefault("warnings", [])
    a.setdefault("provenance", {})
    if a["authors_zh"] and len(a["authors_zh"]) != len(a["authors"]):
        a["authors_zh"] = []
    return a


def identity(a: dict) -> str:
    if a.get("doi"):
        return "doi:" + a["doi"]
    norm = lambda s: re.sub(r"[\W_]+", "", str(s).casefold())
    return "title:" + digest([norm(a["journal"]), norm(a["title"]),
                              [norm(x) for x in a.get("authors", [])], a.get("year", "")])


def infer_catalogue_pages(articles: list[dict]) -> int:
    """Suggest contiguous print pages from consecutive entries in ONE catalogue.

    Never treats a weekly selection as a complete TOC, crosses an unknown entry,
    or replaces source-supplied end pages. Suggestions need explicit review.
    """
    groups = {}
    for a in articles:
        provenance = a.get("provenance", {})
        source = a.get("source_id") or provenance.get("import_source")
        if a.get("origin") != "domestic" or not source or not a.get("year") or not (a.get("issue") or a.get("total_issue")):
            continue
        key = (source, a["journal"], a["year"], a.get("volume", ""), a.get("issue", ""), a.get("total_issue", ""))
        groups.setdefault(key, []).append(a)
    changed = 0
    for group in groups.values():
        group.sort(key=lambda a: a.get("ordinal") if isinstance(a.get("ordinal"), int) else 999999)
        for a, nxt in zip(group, group[1:]):
            start, following = a.get("page_start", ""), nxt.get("page_start", "")
            if not start and re.fullmatch(r"[0-9]+", a.get("pages", "")):
                start = a["pages"]
            if not following and re.fullmatch(r"[0-9]+", nxt.get("pages", "")):
                following = nxt["pages"]
            if (not isinstance(a.get("ordinal"), int) or nxt.get("ordinal") != a["ordinal"] + 1
                    or a.get("article_number") or nxt.get("article_number") or a.get("page_end")
                    or (a.get("pages") and a["pages"] != start)
                    or not re.fullmatch(r"[0-9]+", start) or not re.fullmatch(r"[0-9]+", following)
                    or int(following) <= int(start)):
                continue
            # Ambiguous duplicate starts in this issue cannot establish adjacency.
            if sum((x.get("page_start") or x.get("pages")) == start for x in group) != 1:
                continue
            if sum((x.get("page_start") or x.get("pages")) == following for x in group) != 1:
                continue
            if any(sum(x.get("ordinal") == y["ordinal"] for x in group) != 1 for y in (a, nxt)):
                continue
            proposal = {"method": "next_start_minus_one", "status": "pending", "page_start": start,
                        "page_end": str(int(following) - 1), "pages": f"{start}-{int(following) - 1}",
                        "next_title": nxt["title"], "next_page_start": following, "next_url": nxt["url"],
                        "next_ordinal": nxt["ordinal"], "source_url": a.get("provenance", {}).get("import_source") or a["url"],
                        "evidence_ids": nxt.get("provenance", {}).get("field_evidence", {}).get("page_start", []),
                        "assumption": "同一期原目录相邻条目连续编页，下一篇起始页减一；空白页、插页或漏项须人工核对"}
            previous = a.get("pagination_inference", {})
            if {k: v for k, v in previous.items() if k not in {"status", "actor"}} == {k: v for k, v in proposal.items() if k != "status"}:
                continue
            a["pagination_inference"] = proposal
            changed += 1
    return changed


def infer_issue_pages(issue_id: int, actor: str) -> int:
    with connect(True) as c:
        issue = c.execute("SELECT status FROM research_issues WHERE id=?", (issue_id,)).fetchone()
        if not issue or issue[0] != "draft":
            raise ValueError("只能为草稿推断页码")
        rows = c.execute("SELECT entry_id,data FROM research_items WHERE issue_id=?", (issue_id,)).fetchall()
        articles = [json.loads(x["data"]) for x in rows]
        changed = infer_catalogue_pages(articles)
        for row, a in zip(rows, articles):
            if dumps(a) != row["data"]:
                c.execute("UPDATE research_items SET data=?,review=CASE WHEN review='excluded' THEN 'excluded' ELSE 'pending' END,actor=?,updated_at=? WHERE issue_id=? AND entry_id=?",
                          (dumps(a), actor, now_text(), issue_id, row["entry_id"]))
        return changed


def period_section(a: dict, issue: dict) -> str:
    value = a.get("source_published_at") if a.get("origin") == "domestic" else (a.get("published_online") or a.get("published_at"))
    value = value or a.get("published_at") or ""
    start, end = parse_time(issue["period_start"]), parse_time(issue["period_end"])
    if len(value) < 10:
        # A coarse date wholly outside the week is not ambiguous about this week.
        try:
            if re.fullmatch(r"\d{4}(?:-\d{2})?", value):
                year, month = int(value[:4]), int(value[5:7]) if len(value) == 7 else 1
                lower = datetime(year, month, 1, tzinfo=TZ)
                upper = (datetime(year + 1, 1, 1, tzinfo=TZ) if len(value) == 4 or month == 12
                         else datetime(year, month + 1, 1, tzinfo=TZ))
                if upper <= start:
                    return "supplement"
                if lower >= end:
                    return "future"
        except ValueError:
            pass
        return "date_review"
    try:
        dt = parse_time(value)
    except (ValueError, TypeError):
        return "date_review"
    if len(value) == 10 and dt.date() in {start.date(), end.date()}:
        return "date_review"
    if dt >= end:
        return "future"
    return "new" if dt >= start else "supplement"


def _editable_issue(c, issue_id: int) -> int:
    issue = c.execute("SELECT * FROM research_issues WHERE id=?", (issue_id,)).fetchone()
    if not issue:
        raise ValueError("周报不存在")
    if issue["status"] == "draft":
        return issue_id
    if issue["status"] != "published":
        raise ValueError("该草稿已应用，请回到正式期次")
    existing = c.execute("SELECT id FROM research_issues WHERE parent_id=? AND status='draft'", (issue_id,)).fetchone()
    if existing:
        return existing[0]
    cur = c.execute("INSERT INTO research_issues(period_start,period_end,parent_id,revision,created_at) VALUES(?,?,?,?,?)",
                    (issue["period_start"], issue["period_end"], issue_id, issue["revision"], now_text()))
    new_id = cur.lastrowid
    for a in json.loads(issue["snapshot"]).get("articles", []):
        c.execute("INSERT INTO research_items VALUES(?,?,?,?,?,?,?)", (new_id, a["entry_id"], dumps(a), "approved", a.get("section", "new"), "snapshot", now_text()))
    return new_id


def _upsert(c, issue_id: int, raw: dict) -> dict:
    a = normalize(raw)
    # Import/collector credentials cannot supply an editorial override.
    a.pop("content_override", None)
    key = identity(a)
    old = c.execute("SELECT e.* FROM research_aliases a JOIN research_entries e ON e.id=a.entry_id WHERE a.identity=?", (key,)).fetchone()
    if not old:
        old = c.execute("SELECT * FROM research_entries WHERE identity=?", (key,)).fetchone()
    # Adding a DOI to an earlier DOI-less record must retain its identity.
    if not old and a["doi"]:
        fallback = identity({**a, "doi": ""})
        old = c.execute("SELECT * FROM research_entries WHERE identity=?", (fallback,)).fetchone()
        if old:
            c.execute("UPDATE research_entries SET identity=? WHERE id=?", (key, old["id"]))
    if not old and a["origin"] == "foreign":
        # Publisher URLs and exact title/author matches bridge DOI assignment and
        # online-first year changes. Ambiguous matches remain separate for review.
        candidates = c.execute("SELECT * FROM research_entries WHERE json_extract(data,'$.journal')=?", (a["journal"],)).fetchall()
        matches = []
        for candidate in candidates:
            other = json.loads(candidate["data"])
            if a["doi"] and other.get("doi") and a["doi"] != other["doi"]:
                continue
            same_title = identity({**a, "doi": "", "year": ""}) == identity({**other, "doi": "", "year": ""})
            same_url = a["url"] == other.get("url") and ("/doi/" in a["url"] or "/article" in a["url"] or "/issues/" in a["url"])
            if (same_title and a["authors"]) or same_url:
                matches.append(candidate)
        if len(matches) == 1:
            old = matches[0]
            c.execute("INSERT OR IGNORE INTO research_aliases VALUES(?,?)", (key, old["id"]))
    if old:
        previous = json.loads(old["data"])
        source_rank = {"publisher": 3, "crossref": 2, "openalex": 1}
        previous_sources, incoming_sources = previous.get("field_sources", {}), a.get("field_sources", {})
        for field, evidence in previous_sources.items():
            rank = max((source_rank.get(x.get("provider"), 0) for x in evidence), default=0)
            incoming_rank = max((source_rank.get(x.get("provider"), 0) for x in incoming_sources.get(field, [])), default=0)
            if incoming_rank and rank > incoming_rank and previous.get(field):
                a[field] = previous[field]
                incoming_sources[field] = evidence
        a["field_sources"] = {**previous_sources, **incoming_sources}
        if previous.get("provenance", {}).get("publisher") and not a.get("provenance", {}).get("publisher"):
            a["ordinal"] = previous.get("ordinal", a["ordinal"])
        for k, v in previous.items():
            if not a.get(k):
                a[k] = v
        # Evidence is cumulative; a less complete provider cannot erase known facts.
        a["provenance"] = {**previous.get("provenance", {}), **a.get("provenance", {})}
        for original, translated in (("title", "title_zh"), ("abstract", "abstract_zh"), ("keywords", "keywords_zh")):
            if a.get(original) != previous.get(original):
                a[translated] = [] if translated == "keywords_zh" else ""
        if a["authors"] != previous.get("authors", []):
            a["authors_zh"] = []
        a["collected_at"] = previous.get("collected_at") or old["first_seen_at"]
        eid = old["id"]
        last = c.execute("SELECT i.* FROM research_items m JOIN research_issues i ON i.id=m.issue_id "
                         "WHERE m.entry_id=? AND i.parent_id IS NULL ORDER BY i.period_end LIMIT 1", (eid,)).fetchone()
        if last:
            issue_id = last["id"]
        evidence_only = {"provenance", "field_sources", "collected_at", "index_topics"}
        substantive = lambda value: {k: v for k, v in value.items() if k not in evidence_only}
        content.apply(a)
        if digest(substantive(previous)) == digest(substantive(a)):
            c.execute("UPDATE research_entries SET data=?,updated_at=? WHERE id=?", (dumps(a), now_text(), eid))
            return {"entry_id": eid, "issue_id": issue_id, "duplicate": True}
        c.execute("UPDATE research_entries SET data=?,updated_at=? WHERE id=?", (dumps(a), now_text(), eid))
    else:
        a["collected_at"] = now_text()
        cur = c.execute("INSERT INTO research_entries(identity,data,first_seen_at,updated_at) VALUES(?,?,?,?)",
                        (key, dumps(a), now_text(), now_text()))
        eid = cur.lastrowid
    content.apply(a)
    target = _editable_issue(c, issue_id)
    issue = dict(c.execute("SELECT * FROM research_issues WHERE id=?", (target,)).fetchone())
    section = "correction" if issue["parent_id"] else period_section(a, issue)
    if section == "future":
        section = "date_review"
    screening = "excluded" if a["content_policy"]["decision"] == "exclude" else "pending"
    c.execute("INSERT INTO research_items VALUES(?,?,?,?,?,?,?) ON CONFLICT(issue_id,entry_id) DO UPDATE SET "
              "data=excluded.data,review=excluded.review,section=excluded.section,actor=excluded.actor,updated_at=excluded.updated_at",
              (target, eid, dumps(a), screening, section, "content-rules" if screening == "excluded" else "", now_text()))
    return {"entry_id": eid, "issue_id": target, "duplicate": False}


def upsert(issue_id: int, raw: dict) -> dict:
    with connect(True) as c:
        return _upsert(c, issue_id, raw)


def parse_markdown(markdown: str) -> list[dict]:
    mapping = {"期刊": "journal", "栏目": "section_name", "作者": "authors", "年份": "year", "期号": "issue",
               "卷号": "volume", "总期号": "total_issue", "页码": "pages", "起始页": "page_start", "结束页": "page_end",
               "文章编号": "article_number", "DOI": "doi", "ISSN": "issn", "作者单位": "affiliations", "关键词": "keywords"}
    records = []
    chunks = re.split(r"(?m)^##\s+(?:\d+[.、]\s*)?", markdown)
    for block in chunks[1:]:
        title, _, rest = block.partition("\n")
        a = {"title": title.strip()}
        for line in rest.splitlines():
            m = re.match(r"([^：:]+)[：:]\s*(.*)", line)
            if m and m[1] in mapping:
                a[mapping[m[1]]] = clean(m[2])
        m = re.search(r"(?ms)^###\s+摘要\s*\n(.*?)(?=^引文缺项[：:]|^证据[：:]|\Z)", rest)
        a["abstract"] = clean(m[1]) if m else ""
        records.append(a)
    return records


def parse_import(payload: dict) -> tuple[list[dict], list[str]]:
    if payload.get("version") != SCHEMA_VERSION:
        raise ValueError("不支持的导入版本")
    markdown = payload.get("markdown")
    if not isinstance(markdown, str) or len(markdown.encode()) > 2_000_000:
        raise ValueError("Markdown 为空或超过 2 MB")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata 必须是对象")
    md_records = parse_markdown(markdown)
    records = metadata.get("papers") or md_records
    if not isinstance(records, list) or not records or len(records) > 1000:
        raise ValueError("未解析到条目，或单次超过 1000 条")
    header = markdown.split("\n## ", 1)[0]
    source = metadata.get("source") or {}
    url_match = re.search(r"(?m)^来源[：:]\s*<?(https?://[^\s>]+)", header)
    time_match = re.search(r"(?m)^公众号推送[：:]\s*(.+)", header)
    source_url = source.get("url") or (url_match[1] if url_match else "")
    pub = source.get("published_at") or (time_match[1].strip() if time_match else "")
    warnings = [str(x) for x in metadata.get("warnings", [])]
    if metadata.get("needs_review") or "待复核" in header:
        warnings.append("上游资料待复核；请核对原文与 OCR 疑点")
    out = []
    for i, r in enumerate(records):
        a = normalize({**r, "origin": "domestic", "url": r.get("url") or source_url,
                       "source_published_at": pub, "source_id": payload["source_id"], "ordinal": i,
                       "provenance": {"import_source": source_url, "field_evidence": r.get("field_evidence", {}),
                                      "evidence_ids": r.get("evidence_ids", [])}, "warnings": list(warnings)})
        if metadata:
            md = md_records[i] if i < len(md_records) else {}
            def comparable(key, value):
                if key in {"authors", "keywords"}:
                    parts = re.split(r"[、;；]", value) if isinstance(value, str) else (value or [])
                    return [clean(x) for x in parts if clean(x)]
                return clean(value)
            differences = [k for k in ("title", "journal", "authors", "year", "volume", "issue", "pages", "page_start", "page_end", "article_number", "doi", "abstract", "keywords")
                           if comparable(k, md.get(k)) != comparable(k, a.get(k))]
            if differences:
                a["warnings"].append("Markdown 与结构化资料不一致：" + "、".join(differences))
        out.append(a)
    if len(md_records) != len(records):
        warnings.append("Markdown 与结构化资料条目数不一致")
        for a in out:
            a["warnings"].append(warnings[-1])
    return out, warnings


def import_payload(payload: dict, issue_id: int | None = None) -> dict:
    if not isinstance(payload, dict) or not clean(payload.get("source_id")):
        raise ValueError("缺少 source_id")
    expected = digest({"markdown": payload.get("markdown"), "metadata": payload.get("metadata")})
    if payload.get("content_hash") != expected:
        raise ValueError("文件哈希不匹配")
    articles, warnings = parse_import(payload)
    inferred_pages = infer_catalogue_pages(articles)
    target = get_issue(issue_id) if issue_id else issue_for()
    if not issue_id and articles:
        published = articles[0].get("source_published_at", "")
        if len(published) >= 10 and parse_time(published) >= parse_time(target["period_end"]):
            # During the week imports belong to the upcoming Saturday, not the
            # already closed previous issue. Publication still requires approval.
            stop = parse_time(target["period_end"])
            while stop <= parse_time(published):
                stop += timedelta(days=7)
            target = issue_for(stop.isoformat())
    if not target:
        raise ValueError("目标周报不存在")
    with connect(True) as c:
        prior = c.execute("SELECT id,result FROM research_imports WHERE source_id=? AND content_hash=?",
                           (payload["source_id"], expected)).fetchone()
        if prior:
            return {**json.loads(prior["result"]), "import_id": prior["id"], "replayed": True}
        result = {"parsed": len(articles), "duplicates": 0, "pending": 0, "excluded": 0, "content_review": 0, "conflicts": [], "warnings": warnings, "issues": [], "inferred_pages": inferred_pages}
        for a in articles:
            # OCR corrections without a stable paper identifier require a human merge.
            candidates = c.execute("SELECT id,data FROM research_entries WHERE json_extract(data,'$.source_id')=? AND json_extract(data,'$.ordinal')=?",
                                   (payload["source_id"], a["ordinal"])).fetchall()
            for old in candidates:
                if identity(json.loads(old["data"])) != identity(a):
                    a["warnings"].append(f"可能是条目 {old['id']} 的修订，请合并或排除重复项")
                    result["conflicts"].append(old["id"])
            r = _upsert(c, target["id"], a)
            result["duplicates" if r["duplicate"] else "excluded" if a["content_policy"]["decision"] == "exclude" else "pending"] += 1
            result["content_review"] += int(a["content_policy"]["decision"] == "review")
            if r["issue_id"] not in result["issues"]:
                result["issues"].append(r["issue_id"])
        cur = c.execute("INSERT INTO research_imports(source_id,content_hash,payload,result,created_at) VALUES(?,?,?,?,?)",
                        (payload["source_id"], expected, dumps(payload), dumps(result), now_text()))
        result["import_id"] = cur.lastrowid
        return result


def move_items(issue_id: int, entry_ids: list[int], target_id: int) -> None:
    with connect(True) as c:
        source = c.execute("SELECT status FROM research_issues WHERE id=?", (issue_id,)).fetchone()
        if not source or source[0] != "draft" or issue_id == target_id:
            raise ValueError("只能将草稿条目移入另一期")
        target_id = _editable_issue(c, target_id)
        target = dict(c.execute("SELECT * FROM research_issues WHERE id=?", (target_id,)).fetchone())
        for eid in entry_ids:
            row = c.execute("SELECT * FROM research_items WHERE issue_id=? AND entry_id=?", (issue_id, eid)).fetchone()
            if not row:
                raise ValueError("条目不属于草稿")
            section = "correction" if target["parent_id"] else period_section(json.loads(row["data"]), target)
            section = "date_review" if section == "future" else section
            screening = "excluded" if row["review"] == "excluded" else "pending"
            c.execute("INSERT INTO research_items VALUES(?,?,?,?,?,?,?) ON CONFLICT(issue_id,entry_id) DO UPDATE SET data=excluded.data,review=excluded.review",
                      (target_id, eid, row["data"], screening, section, "", now_text()))
            c.execute("DELETE FROM research_items WHERE issue_id=? AND entry_id=?", (issue_id, eid))


def merge_items(issue_id: int, entry_id: int, into_id: int, actor: str) -> None:
    """Explicit human resolution of OCR identity changes; keep the old entry id."""
    with connect(True) as c:
        issue = c.execute("SELECT status FROM research_issues WHERE id=?", (issue_id,)).fetchone()
        if not issue or issue[0] != "draft" or entry_id == into_id:
            raise ValueError("请选择草稿中两个不同的条目")
        rows = c.execute("SELECT * FROM research_items WHERE issue_id=? AND entry_id IN (?,?)", (issue_id, entry_id, into_id)).fetchall()
        if len(rows) != 2:
            raise ValueError("合并前请将两个条目移入同一期草稿")
        incoming = next(x for x in rows if x["entry_id"] == entry_id)
        entry = c.execute("SELECT identity FROM research_entries WHERE id=?", (entry_id,)).fetchone()
        c.execute("INSERT OR REPLACE INTO research_aliases VALUES(?,?)", (entry[0], into_id))
        c.execute("UPDATE research_items SET data=?,review='pending',actor=?,updated_at=? WHERE issue_id=? AND entry_id=?",
                  (incoming["data"], actor, now_text(), issue_id, into_id))
        c.execute("UPDATE research_entries SET data=?,updated_at=? WHERE id=?", (incoming["data"], now_text(), into_id))
        c.execute("UPDATE research_items SET review='excluded',actor=? WHERE issue_id=? AND entry_id=?", (actor, issue_id, entry_id))


def screen_issue(issue_id: int, actor: str) -> dict:
    """Re-screen drafts without deleting catalogue anchors or existing exclusions."""
    result = {"excluded": 0, "retyped": 0, "content_review": 0, "changed": 0}
    with connect(True) as c:
        issue = c.execute("SELECT status FROM research_issues WHERE id=?", (issue_id,)).fetchone()
        if not issue or issue["status"] != "draft":
            raise ValueError("只能筛选未发布草稿")
        for row in c.execute("SELECT * FROM research_items WHERE issue_id=?", (issue_id,)).fetchall():
            a = json.loads(row["data"])
            previous_type = a.get("type")
            content.apply(a)
            selected = row["review"]
            decision = a["content_policy"]["decision"]
            overridden = content_confirmed(a)
            if decision == "exclude" and not overridden:
                result["excluded"] += int(selected != "excluded")
                selected = "excluded"
            if selected != "excluded":
                result["retyped"] += int(previous_type != a["type"])
                result["content_review"] += int(decision == "review" and not overridden)
                if (previous_type != a["type"] or decision == "review") and not overridden:
                    selected = "pending"
            if dumps(a) != row["data"] or selected != row["review"]:
                result["changed"] += 1
                c.execute("UPDATE research_items SET data=?,review=?,actor=?,updated_at=? WHERE issue_id=? AND entry_id=?",
                          (dumps(a), selected, actor, now_text(), issue_id, row["entry_id"]))
    return result


def review(issue_id: int, entry_ids: list[int], action: str, actor: str, edits: dict | None = None,
           section: str | None = None, pagination_decision: str | None = None,
           content_override_reason: str = "") -> None:
    if action not in {"approved", "pending", "excluded"}:
        raise ValueError("无效审核操作")
    with connect(True) as c:
        issue = c.execute("SELECT * FROM research_issues WHERE id=?", (issue_id,)).fetchone()
        if not issue or issue["status"] != "draft":
            raise ValueError("只能修改草稿")
        for eid in entry_ids:
            row = c.execute("SELECT * FROM research_items WHERE issue_id=? AND entry_id=?", (issue_id, eid)).fetchone()
            if not row:
                raise ValueError("条目不属于该期")
            a = json.loads(row["data"])
            previous_pages = (a.get("pages"), a.get("page_start"), a.get("page_end"))
            if edits:
                allowed = {"title", "title_zh", "authors", "authors_zh", "abstract", "abstract_zh", "keywords", "keywords_zh",
                           "year", "volume", "issue", "pages", "page_start", "page_end", "article_number", "doi", "discipline", "type"}
                old_authors = a.get("authors", [])
                old_author_translations = a.get("authors_zh", [])
                a = normalize({**a, **{k: v for k, v in edits.items() if k in allowed}})
                if a["authors"] != old_authors and ("authors_zh" not in edits or a["authors_zh"] == old_author_translations):
                    a["authors_zh"] = []
            content.apply(a)
            if clean(content_override_reason):
                a["content_override"] = {"reason": clean(content_override_reason), "actor": actor,
                                         "rule": a["content_policy"]["rule"], "at": now_text(),
                                         "fingerprint": digest([a["title"], a.get("section_name"), a["authors"], a["type"]])}
            if a["content_policy"]["decision"] == "exclude" and action != "excluded" and not content_confirmed(a):
                raise ValueError("此条命中非学术内容规则；恢复前请填写人工核对依据")
            inferred = a.get("pagination_inference", {})
            if inferred and previous_pages != (a.get("pages"), a.get("page_start"), a.get("page_end")):
                inferred.update(status="overridden", actor=actor)
            if inferred.get("status") == "pending":
                if pagination_decision == "accept":
                    a.update({k: inferred[k] for k in ("pages", "page_start", "page_end")})
                    inferred.update(status="confirmed", actor=actor)
                    for field in ("pages", "page_end"):
                        a.setdefault("field_sources", {})[field] = [{"provider": "inferred", "url": inferred["source_url"], "verified_by": actor}]
                elif pagination_decision == "reject":
                    inferred.update(status="rejected", actor=actor)
                elif action == "approved":
                    raise ValueError("请先确认或放弃页码推断，再确认条目")
            selected = section or row["section"]
            if selected not in {"new", "supplement", "correction", "date_review"}:
                raise ValueError("无效归属")
            if action == "approved":
                if a["content_policy"]["decision"] == "review" and not content_confirmed(a):
                    raise ValueError("内容类型待核对；请填写人工确认学术内容的依据")
                if selected == "date_review":
                    raise ValueError("请先确认日期，将条目归为本周新文或补录")
                if a["origin"] == "foreign" and (not a["title_zh"] or (a["abstract"] and not a["abstract_zh"])):
                    raise ValueError("外文题名或摘要译文尚未完成")
                if a["origin"] == "foreign" and len(a["keywords_zh"]) != len(a["keywords"]):
                    raise ValueError("原刊关键词译文尚未核对完整")
            a["verified_fields"] = [k for k in ("title", "authors", "journal", "year", "volume", "issue", "pages",
                                                "page_start", "page_end", "article_number", "doi", "url", "issn") if a.get(k)]
            c.execute("UPDATE research_items SET data=?,review=?,section=?,actor=?,updated_at=? WHERE issue_id=? AND entry_id=?",
                      (dumps(a), action, selected, actor, now_text(), issue_id, eid))


def preview_hash(issue_id: int, c=None) -> str:
    if c is None:
        with connect() as db:
            return preview_hash(issue_id, db)
    rows = [dict(r) for r in c.execute("SELECT * FROM research_items WHERE issue_id=? ORDER BY entry_id", (issue_id,))]
    return digest(rows)


def content_confirmed(a: dict) -> bool:
    override = a.get("content_override", {})
    return bool(override.get("reason") and override.get("fingerprint") ==
                digest([a["title"], a.get("section_name"), a.get("authors", []), a.get("type")]))


def publish(issue_id: int, expected_hash: str, actor: str, recipients: list[dict] | None = None,
            scheduled_at: str | None = None) -> dict:
    # recipients is retained for callers upgrading from the draft implementation.
    # Resolve the actual audience only when the confirmed schedule starts.
    import research_delivery as mail
    with connect(True) as c:
        issue = c.execute("SELECT * FROM research_issues WHERE id=?", (issue_id,)).fetchone()
        if not issue or issue["status"] != "draft" or preview_hash(issue_id, c) != expected_hash:
            raise ValueError("预览已变化或该期已经发布，请重新预览")
        rows = c.execute("SELECT * FROM research_items WHERE issue_id=? ORDER BY entry_id", (issue_id,)).fetchall()
        if any(r["review"] == "pending" for r in rows):
            raise ValueError("仍有待复核条目，请确认或排除后发布")
        articles = [{**json.loads(r["data"]), "entry_id": r["entry_id"], "section": r["section"]}
                    for r in rows if r["review"] == "approved"]
        if not articles:
            raise ValueError("没有已确认条目")
        for a in articles:
            if content.assess(a)["decision"] != "keep" and not content_confirmed(a):
                raise ValueError("存在尚未确认的非学术内容筛选结果，请重新核对")
        articles.sort(key=lambda a: (a["origin"], a["journal"], a.get("year", ""), a.get("issue", ""), a.get("ordinal", 999999), a["entry_id"]))
        target = issue["parent_id"] or issue_id
        parent = c.execute("SELECT * FROM research_issues WHERE id=?", (target,)).fetchone()
        if issue["parent_id"] and parent["revision"] != issue["revision"]:
            raise ValueError("正式期次已有更新，请重新合并")
        revision = parent["revision"] + 1
        snapshot = {"id": target, "title": TITLE, "period_start": issue["period_start"], "period_end": issue["period_end"],
                    "articles": articles, "revision": revision, "published_at": now_text()}
        encoded = dumps(snapshot)
        c.execute("UPDATE research_issues SET status='published',snapshot=?,revision=?,published_at=?,actor=? WHERE id=?",
                  (encoded, revision, now_text(), actor, target))
        c.execute("INSERT INTO research_revisions(issue_id,revision,snapshot,actor,created_at) VALUES(?,?,?,?,?)",
                  (target, revision, encoded, actor, now_text()))
        if issue["parent_id"]:
            c.execute("UPDATE research_issues SET status='applied' WHERE id=?", (issue_id,))
        else:
            mail.reserve(c, target, snapshot, scheduled_at or mail.default_time(issue), actor)
        return snapshot


def issue_label(a: dict) -> str:
    return " · ".join(filter(None, ((str(a["year"]) + " 年") if a.get("year") else "", ("第 " + a["volume"] + " 卷") if a.get("volume") else "",
                                    ("第 " + a["issue"] + " 期") if a.get("issue") else ""))) or "在线发表／暂无卷期信息"


def display_pages(a: dict) -> str:
    inference = a.get("pagination_inference", {})
    if inference.get("status") in {"pending", "confirmed"}:
        return inference["pages"]
    return a.get("pages") or (f"{a['page_start']}-{a['page_end']}" if a.get("page_start") and a.get("page_end") else "")


def citation(a: dict, editorial: bool = False) -> str:
    authors = ", ".join(a.get("authors") or [])
    place = str(a.get("year") or "")
    if a.get("volume"):
        place += ", " + a["volume"]
    if a.get("issue"):
        place += "(" + a["issue"] + ")"
    pages = display_pages(a)
    if pages:
        place += ": " + pages
    elif a.get("article_number"):
        place += ": " + a["article_number"]
    result = ". ".join(x for x in (authors, a.get("title", "") + "[J]", a.get("journal", ""), place) if x) + "."
    if a.get("doi"):
        result += " DOI: " + a["doi"] + "."
    inference = a.get("pagination_inference", {})
    if editorial and a.get("page_start") and not a.get("page_end") and inference.get("status") not in {"pending", "confirmed"}:
        result += " [仅起始页已知：" + a["page_start"] + "]"
    if inference.get("status") == "pending" and editorial:
        result += " [推断页码：" + inference["pages"] + "；待核对]"
    elif inference.get("status") == "confirmed" and editorial:
        result += " [页码据相邻目录推断，已确认]"
    return result


def export_citation(a: dict, fmt: str) -> str:
    v = {k: a.get(k) for k in a.get("verified_fields", [])}
    if not v.get("title"):
        raise ValueError("尚无已核对的引文字段")
    line = lambda value: re.sub(r"[\r\n\x00-\x1f]+", " ", str(value or ""))
    if fmt == "ris":
        out = ["TY  - JOUR", "TI  - " + line(v["title"])]
        out += ["AU  - " + line(x) for x in v.get("authors", [])]
        for tag, key in (("JO", "journal"), ("PY", "year"), ("VL", "volume"), ("IS", "issue"), ("SP", "page_start"),
                         ("EP", "page_end"), ("DO", "doi"), ("UR", "url"), ("SN", "issn")):
            if v.get(key):
                out.append(tag + "  - " + line(v[key]))
        if v.get("pages") and not v.get("page_start"):
            parts = re.split(r"[-–]", v["pages"], maxsplit=1)
            out.append("SP  - " + line(parts[0]))
            if len(parts) == 2:
                out.append("EP  - " + line(parts[1]))
        if a.get("pagination_inference", {}).get("status") == "confirmed":
            out.append("N1  - 页码据同一期相邻目录推断，已人工确认")
        return "\r\n".join(out + ["ER  -", ""])
    if fmt != "bib":
        raise ValueError("不支持的引文格式")
    escapes = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "%": r"\%", "&": r"\&", "#": r"\#", "_": r"\_"}
    escape = lambda value: "".join(escapes.get(ch, ch) for ch in line(value))
    fields = {"title": v.get("title"), "author": " and ".join(v.get("authors") or []), "journal": v.get("journal"),
              "year": v.get("year"), "volume": v.get("volume"), "number": v.get("issue"), "pages": v.get("pages"),
              "doi": v.get("doi"), "url": v.get("url")}
    if a.get("pagination_inference", {}).get("status") == "confirmed":
        fields["note"] = "页码据同一期相邻目录推断，已人工确认"
    return "@article{research" + str(a.get("entry_id", "")) + ",\n" + ",\n".join("  " + k + " = {" + escape(x) + "}" for k, x in fields.items() if x) + "\n}\n"


def create_token(label: str) -> str:
    token = secrets.token_urlsafe(40)
    with connect(True) as c:
        c.execute("INSERT INTO research_tokens(label,token_hash,created_at) VALUES(?,?,?)", (label[:100], digest(token), now_text()))
    return token


def token_valid(token: str) -> bool:
    if not token:
        return False
    with connect() as c:
        return c.execute("SELECT 1 FROM research_tokens WHERE active=1 AND token_hash=?", (digest(token),)).fetchone() is not None


def render_email(snapshot: dict, recipient: dict, base_url: str) -> tuple[str, str]:
    esc = html.escape
    url = base_url.rstrip("/") + "/research-updates/" + str(snapshot["id"])
    text = [TITLE, snapshot["period_start"] + " — " + snapshot["period_end"], "完整目录与摘要：" + url, ""]
    body = ['<div style="max-width:740px;margin:auto;background:#fffdf8;color:#211b16;padding:24px;font-family:serif">',
            "<h1>" + TITLE + "</h1><p>" + esc(text[1]) + "</p><p>共 " + str(len(snapshot["articles"])) + ' 篇。<a href="' + esc(url) + '">阅读完整周报</a></p>']
    first_group = True
    for origin, label in (("domestic", "国内研究动态"), ("foreign", "国外研究动态"), ("supplement", "补录与更正")):
        selected = [a for a in snapshot["articles"] if
                    (origin == "supplement" and a.get("section") in {"supplement", "correction"}) or
                    (a["origin"] == origin and a.get("section") not in {"supplement", "correction"})]
        if not selected:
            continue
        body.append('<h2 style="font-size:21px;color:#7c3433;border-bottom:2px solid #7c3433;padding-bottom:8px;margin-top:30px">' + label + '</h2>')
        text.append(label)
        journals = sorted({a["journal"] for a in selected})
        for journal in journals:
            group = [a for a in selected if a["journal"] == journal]
            issue_groups = {}
            for a in group:
                issue_groups.setdefault(issue_label(a), []).append(a)
            for label_issue, entries in sorted(issue_groups.items()):
                heading = journal_display_name(journal) + ' · ' + label_issue + ' · ' + str(len(entries)) + ' 篇'
                link = url + '?' + urlencode({'journal': journal})
                body.append('<h3 style="font-size:16px;line-height:1.8;background:#f4f0e7;padding:10px 12px;margin:18px 0 0">' + esc(heading) + '</h3>')
                text.append(heading)
                ordered = sorted(entries, key=lambda x: x.get("ordinal", 999999))
                if not first_group:
                    titles = [a.get('title_zh') or a['title'] for a in ordered[:3]]
                    preview = '；'.join(t[:90] + ('…' if len(t) > 90 else '') for t in titles)
                    if len(entries) > 3:
                        preview += '；另 ' + str(len(entries) - 3) + ' 篇'
                    text.extend([preview, '查看本刊目录：' + link, ''])
                    body.append('<p style="font-size:13px;line-height:1.85;margin:9px 0">' + esc(preview) + '</p><p style="font:12px/1.8 sans-serif;margin:6px 0 16px"><a style="color:#7c3433" href="' + esc(link) + '">查看本刊目录与摘要 →</a></p>')
                    continue
                first_group = False
                for n, a in enumerate(ordered[:5], 1):
                    title = a.get("title_zh") or a["title"]
                    detail = url + "/articles/" + str(a["entry_id"])
                    summary = a.get("abstract_zh") or a.get("abstract") or ""
                    pages = display_pages(a) or ((a["page_start"] + ("—" + a["page_end"] if a.get("page_end") else " 起")) if a.get("page_start") else "")
                    pages = pages or a.get("article_number") or "来源未提供页码"
                    if pages == a.get("page_start") and not a.get("page_end"):
                        pages += " 起 · 仅列起始页"
                    names = "、".join(a.get("authors", [])) or "作者信息未提供"
                    if a.get("authors_zh"):
                        names = "、".join(a["authors_zh"]) + " / " + names
                    meta = " · ".join((names, TYPE_LABELS.get(a.get("type"), "资料与其他内容"), pages, "含摘要" if summary else "仅题录"))
                    text.extend([str(n) + ". " + title, meta, summary[:180], detail, ""])
                    body.append('<div style="padding:15px 0;border-bottom:1px solid #e6e0d5"><p style="font-size:16px;line-height:1.65;margin:0 0 6px"><span style="color:#a99e90">' + str(n).zfill(2) + '.</span> <a style="color:#292622;text-decoration:none" href="' + esc(detail) + '">' + esc(title) + '</a></p>')
                    if a.get("title_zh"):
                        body.append('<p style="font:13px/1.65 Georgia;color:#7c766e;margin:3px 0">' + esc(a["title"]) + '</p>')
                    body.append('<p style="font:12px/1.8 sans-serif;color:#7c766e;margin:6px 0">' + esc(meta) + '</p>')
                    if summary:
                        body.append('<p style="font-size:13px;line-height:1.85;margin:8px 0">' + esc(summary[:180]) + ('…' if len(summary) > 180 else '') + '</p>')
                    body.append('</div>')
                text.extend(['查看本刊全部 ' + str(len(entries)) + ' 篇：' + link, ''])
                body.append('<p style="font:12px/1.8 sans-serif;margin:10px 0 18px"><a style="color:#7c3433" href="' + esc(link) + '">查看本刊全部 ' + str(len(entries)) + ' 篇 →</a></p>')
    if len("".join(body).encode()) > 80000:
        text = text[:4] + ["本期目录较长，请在网页查看全部条目。"]
        body = body[:2] + ['<p>本期目录较长，请从以下期刊入口查看全部条目。</p>']
        for origin, label in (("domestic", "国内研究动态"), ("foreign", "国外研究动态")):
            journals = sorted({a["journal"] for a in snapshot["articles"] if a["origin"] == origin})
            text.append(label)
            body.append("<h2>" + label + "</h2><ul>")
            for journal in journals:
                link = url + "?" + urlencode({"journal": journal})
                group = [a for a in snapshot["articles"] if a["origin"] == origin and a["journal"] == journal]
                heading = journal_display_name(journal) + ' · ' + ' / '.join(sorted({issue_label(a) for a in group})) + ' · ' + str(len(group)) + ' 篇'
                text.append(heading + "：" + link)
                body.append('<li><a href="' + esc(link) + '">' + esc(heading) + '</a></li>')
            body.append("</ul>")
    unsubscribe = recipient.get("unsubscribe_token")
    if unsubscribe:
        link = base_url.rstrip("/") + "/journal-alerts/unsubscribe/" + unsubscribe
        text.append("退订：" + link)
        body.append('<p><a href="' + esc(link) + '">退订本周研究动态邮件</a></p>')
    body.append("</div>")
    return "\n".join(text), "".join(body)


def deliver(base_url: str, limit: int = 100, sender=None, recipients=None, smtp=None) -> dict:
    from research_delivery import deliver as scheduled_deliver
    return scheduled_deliver(base_url, limit=limit, sender=sender, recipients=recipients, smtp=smtp)


def enqueue(kind: str, args: dict) -> int:
    if kind not in {"collect", "translate"}:
        raise ValueError("未知任务")
    with connect(True) as c:
        pending = c.execute("SELECT id FROM research_jobs WHERE kind=? AND args=? AND status IN ('queued','running')", (kind, dumps(args))).fetchone()
        if pending:
            return pending[0]
        return c.execute("INSERT INTO research_jobs(kind,args,created_at,updated_at) VALUES(?,?,?,?)", (kind, dumps(args), now_text(), now_text())).lastrowid
