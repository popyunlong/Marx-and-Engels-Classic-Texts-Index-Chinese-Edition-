"""Offline member workflow with the production Flask routes, queue schema, matcher and writer."""
from hashlib import sha256
from io import BytesIO
import json
import sqlite3
from zipfile import ZipFile

from docx import Document
from flask import Flask, g

from build_index import normalize
import citation_agent_bridge as bridge
import citation_agent_queue as queue
import citation_agent_test_backend as tasks
import citation_agent_test_web as web
import citation_job_execution as execution
import personal_corpus
from search import Corpus


def test_member_upload_to_verified_near_footnote(tmp_path, monkeypatch):
    from scripts import citation_agent_test_worker as worker

    source = "这批调查材料具体记录了纺织工人修理木质纱锭的过程以及使用的工具。"
    manuscript = source.replace("纱锭", "纱淀")
    db = tmp_path / "library.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE pages (id INTEGER PRIMARY KEY, book TEXT, volume INTEGER, source_file TEXT,
                pdf_page INTEGER, printed_page TEXT, raw_text TEXT, normalized_text TEXT);
            CREATE TABLE toc_entries (source_file TEXT, title TEXT, pdf_page INTEGER, printed_page TEXT,
                level INTEGER, kind TEXT, sort_order INTEGER);
        """)
        conn.execute("INSERT INTO pages VALUES(1, '文集', 1, 's1.pdf', 7, '42', ?, ?)", (source, normalize(source)))
        conn.execute("INSERT INTO toc_entries VALUES('s1.pdf','调查笔记',7,'42',1,'body',1)")
    corpus = Corpus(db_path=db)
    generation = sha256(db.read_bytes()).hexdigest()
    template = worker.runtime.template_version()
    monkeypatch.setattr(worker, "corpus", corpus)
    monkeypatch.setattr(tasks.core, "DB_PATH", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(tasks.core, "ARTIFACT_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(tasks, "DB_PATH", tasks.core.DB_PATH)
    monkeypatch.setattr(tasks, "ARTIFACT_ROOT", tasks.core.ARTIFACT_ROOT)
    monkeypatch.setattr(tasks, "_READY_DB_PATH", "")
    monkeypatch.setattr(web, "_REGISTERED", False)
    monkeypatch.setenv("CITATION_AGENT_TEST_MODE", "live")

    app = Flask(__name__)

    @app.before_request
    def logged_in_member():
        g.current_user = {"id": 83, "member": True}

    web.register_routes(app, {
        "corpus": corpus, "mylib_corpus": personal_corpus,
        "_is_admin_user": lambda user: False,
        "_citation_assistant_enabled_for_user": lambda user: user["member"],
        "_citation_personal_scope_rows": lambda user, tokens: [],
        "_citation_corpus_sha256": lambda: generation, "_citation_template_version": lambda: template,
        "_gb2025_template_approved": lambda: False,
    })

    exchanges = []

    def in_process_model_queue(payload, remaining, check_active):
        check_active()
        path = tmp_path / "redacted.sqlite"
        token = queue.enqueue(payload, ttl_seconds=int(remaining), path=path)
        task = queue.claim("local-model", path=path)
        assert task["id"] == token
        request = task["request"]
        assert request["protocol_version"] == 2
        judgments = [
            {"record_id": record["record_id"], "candidate_id": pair["candidate_id"], "judgment": "minor_difference"}
            for record in request["records"] for pair in record["evidence"]
            if "纱淀" in pair["quotation"] and "纱锭" in pair["excerpt"]
        ]
        exchanges.extend(judgments)
        queue.complete(token, "local-model", {"actions": [], "verdicts": judgments}, path=path)
        return queue.result(token, path=path)

    monkeypatch.setattr(bridge, "_queue_exchange", in_process_model_queue)
    client = app.test_client()
    original = Document()
    original.add_paragraph("文中举例写道：“" + manuscript + "”")
    upload = BytesIO()
    original.save(upload)
    upload.seek(0)
    response = client.post("/api/citation-agent/jobs", data={
        "file": (upload, "辨字.docx"), "scope": json.dumps(["vol:文集:1"]),
        "mode": "generate", "note_kind": "footnote", "citation_style": "mkszyj",
    })
    assert response.status_code == 202, response.data
    job_id = response.json["job"]["id"]
    url = "/api/citation-agent/jobs/" + job_id

    def handle(stage):
        job = tasks.claim_next_job("offline-worker", corpus_sha256=generation, template_version=template)
        assert job and job["id"] == job_id and job["claimed_stage"] == stage
        # Run the stage inside its real database lease. The subprocess watchdog is
        # independently exercised by the worker lifecycle cases.
        with execution.worker_claim(job):
            if stage == "extracting":
                tasks.run_extraction(job_id)
            elif stage == "queued":
                worker._analysis(job_id, generation, template)
            else:
                worker._export(job_id, generation, template)

    handle("extracting")
    extracted = client.get(url).json["job"]
    assert extracted["status"] == "awaiting_sections", extracted
    sections = [section["id"] for section in extracted["sections"]]
    response = client.post(url + "/analyze", json={"scope": ["vol:文集:1"], "sections": sections})
    assert response.status_code == 202, response.data
    handle("queued")
    reviewed = client.get(url).json["job"]
    assert reviewed["status"] == "review_ready" and reviewed["agent_status"] == "complete", reviewed
    items = client.get(url + "/candidates").json["items"]
    assert len(exchanges) == 1 and len(items) == 1, items
    candidate = items[0]
    assert candidate["decision"] == "accepted" and candidate["auto_selected"]
    assert candidate["writeback_mode"] == "footnote"
    option = candidate["source_options"][candidate["selected_option"]]
    assert option["printed_pages"] == ["42"] and option["quote_evidence"]["ai_confirmed"]
    response = client.post(url + "/export")
    assert response.status_code == 202, response.data
    handle("exporting")
    final = client.get(url).json["job"]
    assert final["status"] == "complete" and final["inserted_count"] == 1, final
    download = client.get(final["docx_url"])
    assert download.status_code == 200
    with ZipFile(BytesIO(download.data)) as result:
        parsed = lambda name: tasks.core._parse_xml(result.read(name))
        body = parsed("word/document.xml")
        assert "纱淀" in "".join(body.xpath("//w:t/text()", namespaces=tasks.core.NS))
        assert len(body.xpath("//w:footnoteReference", namespaces=tasks.core.NS)) == 1
        notes = parsed("word/footnotes.xml").xpath("//w:t/text()", namespaces=tasks.core.NS)
        assert candidate["proposed_citation"] in "".join(notes) and "42" in "".join(notes)
        comments = parsed("word/comments.xml").xpath("//w:t/text()", namespaces=tasks.core.NS)
        assert "淀 → 原书 锭" in "".join(comments)
