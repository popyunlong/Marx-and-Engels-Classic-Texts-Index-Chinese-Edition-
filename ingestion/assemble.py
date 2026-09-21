from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .recognize import key


class ReviewRequired(ValueError):
    pass


def _title_key(value):
    return re.sub(r"[\W_]", "", str(value))


def assemble(store, book_id):
    b = store.get_book(book_id)
    with store.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM pages WHERE book=? ORDER BY page", (book_id,))]
    if len(rows) != b["pages"] or any(r["stage"] != "done" for r in rows):
        raise ReviewRequired("仍有未完成或有争议的页面")
    evidence, printed_toc = [], []
    for row in rows:
        history = json.loads(row["result"])
        matching = [r for r in history.get("runs", []) if r.get("stage") in {"mimo", "verify"}
                    and r.get("finish") == "stop" and key(r.get("text")) == key(row["text"])]
        if matching:
            last = matching[-1]
            if last.get("metadata"):
                evidence.append((row, last["metadata"]))
            if row["kind"] == "toc":
                printed_toc.extend(dict(item, evidence_page=row["page"]) for item in last.get("toc", []))
    manual = json.loads(b["metadata"])
    metadata = {}
    for field in ["title", "publisher", "year", "editor", "edition"]:
        values = {}
        for row, item in evidence:
            value = str(item.get(field) or "").strip().strip("《》")
            if value and _title_key(value) in _title_key(row["text"]):
                values[value] = row["page"]
        if manual.get("reviewed_against_image"):
            metadata[field] = str(manual.get(field) or "")
        elif len(values) == 1:
            metadata[field] = next(iter(values))
        elif len(values) > 1:
            raise ReviewRequired(f"书目{field}存在冲突，需要对照版权页确认")
        else:
            metadata[field] = ""
    if not all(metadata.get(k) for k in ["title", "publisher", "year"]):
        raise ReviewRequired("版权页书名、出版社或出版年份证据不足")
    if not re.fullmatch(r"[12][0-9]{3}", metadata["year"]):
        raise ReviewRequired("出版年份格式待核对")
    labels = {}
    for row in rows:
        if row["label"]:
            labels.setdefault(row["label"], []).append(row)
        if row["kind"] == "body" and not row["label"]:
            raise ReviewRequired(f"PDF 第 {row['page']} 页的印刷页码尚未确定")
    toc, seen = [], set()
    for item in printed_toc:
        title = str(item.get("title") or "").strip()
        label = str(item.get("printed_page") or "").strip()
        if not title or not label:
            raise ReviewRequired("目录条目缺标题或印刷页码")
        candidates = labels.get(label, [])
        found = [r for r in candidates if _title_key(title) in _title_key(r["text"][:1600])]
        if len(found) != 1:
            raise ReviewRequired(f"目录“{title}”未能唯一对应正文页")
        identity = (title, found[0]["page"])
        if identity in seen:
            continue
        seen.add(identity)
        toc.append(dict(title=title, pdf_page=found[0]["page"], printed_page=label,
                        level=max(1, min(6, int(item.get("level", 1)))), kind="chapter",
                        sort_order=len(toc), evidence_page=item["evidence_page"]))
    if not toc:
        # Bookmarks can seed discovery but never bypass image verification.
        raise ReviewRequired("没有经过原图与正文双重验证的目录")
    if any(toc[i]["pdf_page"] > toc[i + 1]["pdf_page"] for i in range(len(toc) - 1)):
        raise ReviewRequired("目录跳转顺序异常")
    package = {"schema": 1, "book_id": book_id, "source_sha256": b["sha"],
               "source_file": "pdfs/自动入库/" + b["sha"] + ".pdf", "page_count": b["pages"],
               "metadata": metadata, "toc": toc,
               "pages": [{k: r[k] for k in ("page", "text", "label", "kind", "checked", "repairs")} for r in rows]}
    directory = store.root / "packages" / book_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "book.json"
    incoming = target.with_suffix(".incoming")
    incoming.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    incoming.replace(target)
    from .store import sha256
    digest = sha256(target)
    with store.connect() as c:
        c.execute("UPDATE books SET metadata=?,toc=?,status='ready',error='' WHERE id=?",
                  (json.dumps(metadata, ensure_ascii=False), json.dumps(toc, ensure_ascii=False), book_id))
    store.event("accepted", book_id, package_sha256=digest, pages=len(rows), toc_entries=len(toc))
    return package


def assemble_child(pipe, root, book):
    try:
        from .store import Store
        package = assemble(Store(Path(root)), book)
        pipe.send({"ok": True, "pages": package["page_count"]})
    except Exception as exc:
        pipe.send({"ok": False, "error": str(exc)})
    finally:
        pipe.close()
