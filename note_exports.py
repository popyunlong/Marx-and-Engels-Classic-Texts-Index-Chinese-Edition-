"""Owner-scoped complete note exports and conservative reader URLs."""
import io
import json
import re
from urllib.parse import urlsplit, urlencode, unquote, quote

import notes


def source_url(note):
    link = _reader_url(note)
    anchor = str(note.get("anchor_text") or "").strip()[:200]
    if link and anchor and note.get("reader") in {"wenku", "liushi"} and "#" not in link:
        link += "#:~:text=" + quote(anchor, safe="")
    return link


def _reader_url(note):
    value = str(note.get("source_url") or "").strip()
    decoded = unquote(value)
    parts = urlsplit(value)
    safe = (value.startswith("/") and not decoded.startswith("//") and "\\" not in decoded
            and not parts.netloc and not parts.scheme and not any(ord(c) < 32 for c in decoded)
            and re.match(r"^/(viewer|wenku|liushi|mylib)(/|$)", parts.path)
            and ".." not in unquote(parts.path).split("/"))
    if safe:
        return value
    key, reader, page = note.get("book_key", ""), note.get("reader"), note.get("page")
    if reader == "viewer" and key and page:
        return "/viewer?" + urlencode({"file": key, "page": page})
    if reader == "mylib" and re.fullmatch(r"mylib:\d+", key) and page:
        return "/mylib/" + key.split(":")[1] + "?" + urlencode({"page": page})
    if reader in {"wenku", "liushi"} and key.startswith("wenku:") and "|" in key:
        book, volume = key[6:].rsplit("|", 1)
        if volume.isdigit() and book:
            return "/" + reader + "/" + __import__('urllib.parse', fromlist=['quote']).quote(book, safe='') + "?" + urlencode({"vol": volume, "doc": note.get("doc_path") or "", "pg": page or ""})
    return ""


def collect(uid, payload):
    scope = payload.get("scope", "all")
    if scope not in {"all", "filtered", "single"}:
        raise ValueError("未知导出范围。")
    if scope == "single":
        row = notes.get_note(int(payload.get("note_id") or 0), uid)
        if row is None:
            raise LookupError("笔记不存在。")
        return [row]
    # One read snapshot, no 2000-row UI ceiling or concurrent pagination gaps.
    clauses, params = ["user_id=?"], [uid]
    if scope == "filtered":
        if payload.get("book"):
            clauses.append("book_key=?"); params.append(str(payload["book"]))
        if payload.get("q"):
            clauses.append("(quote LIKE ? OR body LIKE ? OR book_title LIKE ?)")
            params.extend(["%" + str(payload["q"]) + "%"] * 3)
    with notes._connect() as c:
        return [dict(x) for x in c.execute("SELECT * FROM notes WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC,id DESC", params)]


def render(rows, fmt, base_url):
    if fmt not in {"md", "json", "docx"}:
        raise ValueError("请选择 Word、Markdown 或 JSON。")
    public = [{k: v for k, v in row.items() if k != "user_id"} for row in rows]
    for row in public:
        link = source_url(row)
        row["source_url"] = base_url.rstrip("/") + link if link else ""
    if fmt == "json":
        return json.dumps({"exported_at": notes.utc_now_text(), "notes": public}, ensure_ascii=False, indent=2).encode(), "application/json"
    if fmt == "md":
        chunks = ["# 我的研究笔记", ""]
        for row in public:
            chunks.extend(["## " + row["book_title"] + " " + row["volume_label"], row["page_label"] or row["doc_path"] or ("第 " + str(row["page"]) + " 页" if row.get("page") else "位置未记录"),
                "> " + row["quote"].replace("\n", "\n> "), "", row["body"], "", "创建：" + row["created_at"] + "；更新：" + row["updated_at"],
                "[返回原文](" + row["source_url"].replace(')', '%29') + ")" if row["source_url"] else "原文位置暂不可定位", "", "---", ""])
        return "\n".join(chunks).encode(), "text/markdown; charset=utf-8"
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.shared import Pt
    document = Document()
    style = document.styles['Normal']; style.font.name = 'Microsoft YaHei'; style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    document.add_heading("我的研究笔记", 0)
    for row in public:
        document.add_heading(row["book_title"] + " " + row["volume_label"], 1)
        document.add_paragraph(row["page_label"] or row["doc_path"] or ("第 " + str(row["page"]) + " 页" if row.get("page") else "位置未记录"))
        if row["quote"]: document.add_paragraph(row["quote"], style='Quote')
        document.add_paragraph(row["body"])
        document.add_paragraph("创建：" + row["created_at"] + "\n更新：" + row["updated_at"])
        paragraph = document.add_paragraph()
        if row["source_url"]:
            link = OxmlElement('w:hyperlink'); link.set(qn('r:id'), paragraph.part.relate_to(row["source_url"], RT.HYPERLINK, is_external=True))
            run = OxmlElement('w:r'); text = OxmlElement('w:t'); text.text = '返回原文'; run.append(text); link.append(run); paragraph._p.append(link)
        else: paragraph.add_run("原文位置暂不可定位")
    output = io.BytesIO(); document.save(output)
    return output.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
