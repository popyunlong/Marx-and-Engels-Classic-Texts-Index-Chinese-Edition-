from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from .scheduler import Scheduler, YieldRequired
from .store import Store


def create_app(root: Path, pdf_root: Path, token: str, scheduler=None):
    if len(token) < 32:
        raise ValueError("入库访问令牌至少 32 字符")
    store = Store(root)
    pdf_root = Path(pdf_root).resolve()
    uploads = store.root / "uploads"
    uploads.mkdir(exist_ok=True)
    locks = {}
    lock_guard = threading.Lock()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = (8 << 20) + 4096
    app.config["INGESTION_STORE"] = store

    def lock_for(book):
        with lock_guard:
            return locks.setdefault(book, threading.Lock())

    def admission():
        if scheduler:
            with scheduler.admit():
                pass

    @app.before_request
    def authenticate():
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            return jsonify(error="需要有效的入库访问令牌"), 401

    @app.errorhandler(YieldRequired)
    def yield_error(exc):
        return jsonify(error=str(exc), retry_after=2), 423

    @app.errorhandler(ValueError)
    def value_error(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(KeyError)
    def key_error(exc):
        return jsonify(error=str(exc)), 404

    @app.get("/v1/ingestion")
    def status():
        return jsonify(store.snapshot())

    @app.post('/v1/ingestion/free-ocr/claim')
    def local_ocr_claim():
        from . import ocr_queue
        import contextlib
        owner='local-'+str(request.get_json().get('owner',''))
        # Keep queue inspection and resource claim in the same admission guard.
        with scheduler.admit() if scheduler else contextlib.nullcontext():
            job=ocr_queue.claim(store,owner,local=True)
        return jsonify(job={k:job[k] for k in ('book','page','token','lease')} if job else None)

    @app.post('/v1/ingestion/free-ocr/image')
    def local_ocr_image():
        from . import ocr_queue
        from .free_ocr import render
        from io import BytesIO
        data=request.get_json()
        job=ocr_queue.leased(store,data['book'],int(data['page']),data['token'])
        png=render(job['path'],job['page'])
        ocr_queue.image_issued(store,job,hashlib.sha256(png).hexdigest())
        return send_file(BytesIO(png),mimetype='image/png')

    @app.post('/v1/ingestion/free-ocr/result')
    def local_ocr_result():
        from . import ocr_queue
        data=request.get_json()
        result=ocr_queue.submit(store,data['book'],int(data['page']),data['token'],data['result'])
        ocr_queue.apply_ready(store)
        return jsonify(result)

    @app.post('/v1/ingestion/free-ocr/failure')
    def local_ocr_failure():
        from . import ocr_queue
        data=request.get_json()
        job=ocr_queue.leased(store,data['book'],int(data['page']),data['token'])
        ocr_queue.failed(store,job,str(data.get('error','本机 OCR 中断')))
        return jsonify(ok=True)

    @app.post('/v1/ingestion/free-ocr/release')
    def local_ocr_release():
        from . import ocr_queue
        ocr_queue.release_node(store,'local-'+str(request.get_json().get('owner','')))
        return jsonify(ok=True)

    @app.post("/v1/ingestion/batches")
    def batch():
        data = request.get_json()
        batch_id = data.get("id")
        if batch_id and not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", batch_id):
            raise ValueError("批次标识无效")
        created=store.batch(str(data.get("title", "PDF 扩库"))[:200], batch_id)
        if data.get('free_ocr',False):
            from .ocr_queue import enable
            enable(store,created)
        return jsonify(id=created)

    @app.post('/v1/ingestion/batches/<batch>/free-ocr')
    def enable_free_ocr(batch):
        from .ocr_queue import enable
        enable(store,batch)
        return jsonify(ok=True)

    @app.post('/v1/ingestion/batches/<batch>/review-mode')
    def full_review(batch):
        from .audit import enable_full
        if request.get_json().get('mode')!='full':raise ValueError('核验模式无效')
        return jsonify(enable_full(store,batch))

    @app.get('/v1/ingestion/books/<book>/pages/<int:page>/free-ocr')
    def free_ocr_detail(book,page):
        from .ocr_queue import detail
        return jsonify(detail(store,book,page) or {'status':'not_enabled'})

    @app.post("/v1/ingestion/books")
    def book():
        data = request.get_json()
        return jsonify(store.book(data["batch"], data["name"], data["sha256"], int(data["size"])))

    @app.get("/v1/ingestion/books/<book>")
    def get_book(book):
        return jsonify(store.get_book(book))

    @app.put("/v1/ingestion/books/<book>/chunks/<int:offset>")
    def chunk(book, offset):
        admission()
        data = request.get_data()
        if not data or len(data) > 8 << 20:
            raise ValueError("分块大小必须在 1 字节到 8 MiB 之间")
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Chunk-SHA256", ""), digest):
            raise ValueError("分块校验失败")
        with lock_for(book):
            b = store.get_book(book)
            part = uploads / (b["id"] + ".part")
            if b["status"] != "uploading":
                return jsonify(offset=b["size"], complete=True)
            if offset < b["offset"]:
                with part.open("rb") as f:
                    f.seek(offset)
                    if hashlib.sha256(f.read(len(data))).hexdigest() != digest:
                        raise ValueError("重复分块内容冲突")
                return jsonify(offset=b["offset"])
            if offset != b["offset"] or offset + len(data) > b["size"]:
                return jsonify(error="上传偏移不符", offset=b["offset"]), 409
            with part.open("r+b" if part.exists() else "w+b") as f:
                # A crash after fsync but before the DB commit safely overwrites the tail.
                f.seek(offset)
                f.write(data)
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
            with store.connect() as c:
                c.execute("UPDATE books SET offset=? WHERE id=?", (offset + len(data), book))
        return jsonify(offset=offset + len(data))

    @app.post("/v1/ingestion/books/<book>/complete")
    def complete(book):
        admission()
        with lock_for(book):
            b = store.get_book(book)
            if b["status"] != "uploading":
                return jsonify(b)
            if b["offset"] != b["size"]:
                raise ValueError("上传尚未完成")
            part = uploads / (b["id"] + ".part")
            target = pdf_root / "自动入库" / (b["sha"] + ".pdf")
            source = part if part.exists() else target
            digest = hashlib.sha256()
            started = time.monotonic()
            with source.open("rb") as f:
                for block in iter(lambda: f.read(8 << 20), b""):
                    if time.monotonic() - started > 45:
                        raise YieldRequired("完整文件校验达到单元时限，待空闲重试")
                    admission()
                    digest.update(block)
            if digest.hexdigest() != b["sha"]:
                raise ValueError("完整 PDF SHA-256 不符，未进入识别队列")
            import fitz
            with fitz.open(source) as doc:
                if doc.needs_pass or doc.page_count <= 0:
                    raise ValueError("PDF 已加密或没有页面")
                count = doc.page_count
            selected = (request.get_json(silent=True) or {}).get("pilot_pages")
            if selected is None:
                selected = sorted({min(p, count) for p in [1, 3, 6, max(1, count // 2), max(1, count - 3)]})
            selected = {int(p) for p in selected}
            if len(selected) > 5 or not selected or not all(1 <= p <= count for p in selected):
                raise ValueError("每本试跑最多 5 个有效页面")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source != target:
                if target.exists():
                    from .store import sha256
                    if sha256(target) != b["sha"]:
                        raise ValueError("目标文件内容冲突")
                else:
                    # systemd path isolation creates separate bind mounts, even on
                    # the same physical data disk. Stage beside the destination.
                    incoming = target.with_suffix(".incoming")
                    with source.open("rb") as src, incoming.open("wb") as dst:
                        for block in iter(lambda: src.read(8 << 20), b""):
                            admission()
                            if time.monotonic() - started > 50:
                                raise YieldRequired("文件落盘达到单元时限，待空闲重试")
                            dst.write(block)
                        dst.flush()
                        os.fsync(dst.fileno())
                    os.replace(incoming, target)
                    target.chmod(0o644)
            store.initialize_pages(book, target, count, selected)
            store.event("uploaded", book, sha256=b["sha"], pages=count, pilot_pages=sorted(selected))
            part.unlink(missing_ok=True)
        return jsonify(store.get_book(book))

    @app.post("/v1/ingestion/books/<book>/control")
    def control(book):
        data = request.get_json()
        action = data.get("action")
        store.get_book(book)
        with store.connect() as c:
            if action in {"pause", "resume"}:
                c.execute("UPDATE books SET paused=? WHERE id=?", (int(action == "pause"), book))
            elif action == "retry":
                c.execute("UPDATE pages SET stage='mimo',attempts=0,error='',retry_at=0 WHERE book=? AND stage='review'", (book,))
                c.execute("UPDATE books SET status='processing',error='',paused=0 WHERE id=? AND status='review'", (book,))
            elif action == 'retry_free':
                c.execute("UPDATE free_ocr SET status='pending',attempts=0,retry_at=0,lease=0,owner='',token='',error='' "
                          "WHERE book=? AND status='failed'",(book,))
                c.execute("UPDATE pages SET stage='ocr_check',error='' WHERE book=? AND error LIKE '免费 OCR 重试三次仍失败%'",(book,))
                c.execute("UPDATE books SET status='processing',error='' WHERE id=? AND status='review'",(book,))
            else:
                raise ValueError("操作无效")
        store.event(action, book)
        return jsonify(ok=True)

    @app.post("/v1/ingestion/books/<book>/metadata")
    def metadata(book):
        b = store.get_book(book)
        data = request.get_json()
        if not data.get("reviewed_against_image") or not data.get("reason"):
            raise ValueError("书目修订需要版权页证据")
        evidence_page = int(data.get("evidence_page", 0))
        if not 1 <= evidence_page <= b["pages"]:
            raise ValueError("版权页位置无效")
        if not all(str(data.get(k) or "").strip() for k in ["title", "publisher", "year"]):
            raise ValueError("请填写书名、出版社和出版年份")
        if not re.fullmatch(r"[12][0-9]{3}", str(data["year"])):
            raise ValueError("请填写四位出版年份")
        with store.connect() as c:
            c.execute("UPDATE books SET metadata=?,status=CASE WHEN status='review' THEN 'processing' ELSE status END WHERE id=?",
                      (json.dumps(data, ensure_ascii=False), book))
        store.event("metadata_revision", book, evidence_page, before=json.loads(b["metadata"]), after=data)
        return jsonify(ok=True)

    @app.post("/v1/ingestion/batches/<batch>/budget")
    def budget(batch):
        amount = float(request.get_json()["yuan"])
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("预算必须是正数")
        with store.connect() as c:
            if not c.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
                raise KeyError("批次不存在")
            c.execute("UPDATE batches SET budget=?,pilot=0 WHERE id=?", (amount, batch))
            c.execute("UPDATE pages SET retry_at=0 WHERE book IN (SELECT id FROM books WHERE batch=?)", (batch,))
        store.event("budget_approved", batch=batch, yuan=amount)
        return jsonify(ok=True)

    @app.get("/v1/ingestion/books/<book>/pages")
    def pages(book):
        store.get_book(book)
        with store.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT page,stage,kind,label,checked,repairs,error FROM pages WHERE book=? ORDER BY page", (book,))]
        return jsonify(pages=rows)

    @app.get("/v1/ingestion/books/<book>/pages/<int:page>")
    def page_detail(book, page):
        with store.connect() as c:
            row = c.execute("SELECT * FROM pages WHERE book=? AND page=?", (book, page)).fetchone()
            if row is None:
                raise KeyError("页面不存在")
        from .ocr_queue import detail
        return jsonify({**dict(row),'free_ocr':detail(store,book,page)})

    @app.get("/v1/ingestion/books/<book>/pages/<int:page>/image")
    def page_image(book, page):
        import fitz
        from io import BytesIO
        b = store.get_book(book)
        if not 1 <= page <= b["pages"]:
            raise ValueError("页码越界")
        with fitz.open(b["path"]) as doc:
            p = doc[page - 1]
            pix = p.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            return send_file(BytesIO(pix.tobytes("png")), mimetype="image/png")

    @app.post("/v1/ingestion/books/<book>/pages/<int:page>/revision")
    def revision(book, page):
        data = request.get_json()
        if not data.get("reviewed_against_image") or not data.get("reason"):
            raise ValueError("请对照原图并填写修订依据")
        text = str(data["text"])
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM pages WHERE book=? AND page=?", (book, page)).fetchone()
            if row is None:
                raise KeyError("页面不存在")
            if row["lease"] > time.time() or row["text"] != data.get("original_text"):
                return jsonify(error="页面已变更或正在识别，请刷新后再提交"), 409
            if not text.strip() and not data.get("confirmed_blank"):
                raise ValueError("空白修订须明确确认原图无字")
            c.execute("UPDATE pages SET text=?,label=?,kind=?,checked=1,stage='done',error='' WHERE book=? AND page=?",
                      (text, str(data.get("label", row["label"])), "blank" if not text.strip() else row["kind"], book, page))
            c.execute("UPDATE books SET status='processing' WHERE id=? AND status='review'", (book,))
        store.event("human_revision", book, page, before=row["text"], after=text, reason=data["reason"])
        return jsonify(ok=True)

    @app.get("/v1/ingestion/report")
    def report():
        from .report import report_data
        return jsonify(report_data(store))

    return app


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("/home/data/marx-ingestion"))
    p.add_argument("--pdf-root", type=Path, default=Path("/home/data/pdfs"))
    p.add_argument("--port", type=int, default=8767)
    args = p.parse_args()
    token = (args.root / "access.token").read_text().strip()
    store = Store(args.root)
    scheduler = Scheduler(store, Path("/var/www/.marx_search_full/citation_assistant.sqlite3"),
                          health_url="http://127.0.0.1:8000/api/runtime")
    def watch():
        while True:
            scheduler.tick()
            time.sleep(2)
    threading.Thread(target=watch, daemon=True).start()
    from waitress import serve
    serve(create_app(args.root, args.pdf_root, token, scheduler), host="127.0.0.1", port=args.port,
          threads=3, channel_timeout=60, max_request_body_size=(8 << 20) + 4096)


if __name__ == "__main__":
    main()
