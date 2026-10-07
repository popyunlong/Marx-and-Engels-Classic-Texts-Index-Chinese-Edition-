"""Read-only, release-bound dictionary maps. No model or application imports."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections import deque
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KINDS = {
    "mention": "正文提及", "reference": "参见", "synonym": "别称",
    "broader": "上位概念", "part": "组成关系", "opposes": "对立或区别",
    "related": "概念联系", "background": "背景联系", "work": "著作联系",
}
THEMES = ("哲学与方法论", "政治经济学", "科学社会主义", "人物与著作", "历史事件与组织", "中国马克思主义", "其他词条")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def binding(root=ROOT):
    path = Path(root) / "config/dictionary_graph_release.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text("utf-8"))
    if set(value) != {"id", "sha256", "source_sha256"}:
        raise ValueError("invalid dictionary graph binding")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", value["id"]):
        raise ValueError("unsafe dictionary graph id")
    if any(not re.fullmatch(r"[0-9a-f]{64}", value[k]) for k in ("sha256", "source_sha256")):
        raise ValueError("invalid dictionary graph fingerprint")
    return value


class GraphUnavailable(ValueError):
    pass


@lru_cache(maxsize=8)
def _verified(path, signature, expected_hash, source, source_signature, expected_source):
    if file_hash(Path(path)) != expected_hash or file_hash(Path(source)) != expected_source:
        raise GraphUnavailable("地图与辞典版本不匹配")
    with readonly(path) as c:
        if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise GraphUnavailable("地图数据校验失败")
        meta = json.loads(c.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
        if meta["schema_version"] != 1 or meta["source_sha256"] != expected_source:
            raise GraphUnavailable("地图格式或来源不匹配")
    return meta


def signature(path):
    s = Path(path).stat()
    return (s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class Graph:
    def __init__(self, path, selected, source):
        self.path = Path(path)
        self.meta = _verified(str(self.path), signature(self.path), selected["sha256"],
                              str(source), signature(source), selected["source_sha256"])
        if self.meta["id"] != selected["id"]:
            raise GraphUnavailable("地图版本不匹配")

    def node(self, slug):
        with readonly(self.path) as c:
            row = c.execute("SELECT * FROM nodes WHERE slug=?", (slug,)).fetchone()
            return dict(row) if row else None

    def overview(self):
        with readonly(self.path) as c:
            return [dict(r) for r in c.execute("SELECT theme, count(*) AS count FROM nodes GROUP BY theme ORDER BY theme")]

    def browse(self, query="", theme="", limit=60, offset=0):
        with readonly(self.path) as c:
            rows = c.execute("SELECT * FROM nodes WHERE instr(title,?)>0 AND (?='' OR theme=?) ORDER BY title,slug LIMIT ? OFFSET ?",
                             (query[:100], theme, theme, min(60, max(1, limit)), max(0, offset)))
            return [dict(r) for r in rows]

    @staticmethod
    def edge(row):
        edge = dict(row)
        edge["evidence"] = json.loads(edge.pop("evidence_json"))
        edge["label"] = KINDS[edge["kind"]]
        return edge

    def neighborhood(self, slug, *, limit=12, inference=False, kind=""):
        node = self.node(slug)
        if not node:
            raise KeyError(slug)
        limit = max(1, min(59, limit))
        with readonly(self.path) as c:
            rows = c.execute("""SELECT * FROM edges WHERE (source=? OR target=?)
                AND (? OR layer='evidence') AND (?='' OR kind=?)
                ORDER BY CASE kind WHEN 'reference' THEN 0 WHEN 'synonym' THEN 1 WHEN 'mention' THEN 3 ELSE 2 END,
                CASE layer WHEN 'evidence' THEN 0 ELSE 1 END,
                CASE WHEN source=? THEN 0 ELSE 1 END, id""", (slug, slug, inference, kind, kind, slug)).fetchall()
            nodes = {slug: node}
            edges = []
            for row in rows:
                other = row["target"] if row["source"] == slug else row["source"]
                if other not in nodes:
                    if len(nodes) > limit:
                        continue
                    nodes[other] = dict(c.execute("SELECT * FROM nodes WHERE slug=?", (other,)).fetchone())
                if len(edges) < 120:
                    edges.append(self.edge(row))
            return {"nodes": list(nodes.values()), "edges": edges,
                    "truncated": len(edges) < len(rows), "total_relations": len(rows)}

    def path_between(self, start, end, *, inference=False, kind=""):
        if not self.node(start) or not self.node(end):
            raise KeyError("unknown dictionary term")
        with readonly(self.path) as c:
            # Relation paths are undirected walks; each returned edge retains its original direction.
            adjacency = {}
            for r in c.execute("SELECT source,target,id FROM edges WHERE (? OR layer='evidence') AND (?='' OR kind=?) ORDER BY id", (inference, kind, kind)):
                adjacency.setdefault(r["source"], []).append((r["target"], r["id"]))
                adjacency.setdefault(r["target"], []).append((r["source"], r["id"]))
            queue, seen = deque([(start, [])]), {start}
            while queue:
                here, steps = queue.popleft()
                if here == end:
                    edges = [self.edge(c.execute("SELECT * FROM edges WHERE id=?", (eid,)).fetchone()) for eid in steps]
                    ids = {start, end} | {e[k] for e in edges for k in ("source", "target")}
                    return {"nodes": [self.node(s) for s in sorted(ids)], "edges": edges, "found": True}
                if len(steps) == 4:
                    continue
                for other, eid in adjacency.get(here, []):
                    if other not in seen:
                        seen.add(other)
                        queue.append((other, steps + [eid]))
            return {"nodes": [self.node(start), self.node(end)], "edges": [], "found": False}


def current_graph():
    """Missing/broken graph never prevents dictionary text or application startup."""
    try:
        selected = binding()
        if not selected:
            return None
        data = Path(os.environ.get("MARX_RUNTIME_DATA_DIR") or ROOT / "data")
        control = data / "dictionary-graph-control.json"
        if control.exists():
            control_value = json.loads(control.read_text("utf-8"))
            if control_value.get("disabled", True):
                return None
        # Local preview supplies an explicit artifact root; production defaults to shared data.
        graph_root = Path(os.environ.get("MARX_DICTIONARY_GRAPH_DIR") or data / "dictionary-graphs")
        return Graph(graph_root / selected["id"] / "graph.sqlite", selected, ROOT / "data/dictionary.sqlite")
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None
