from __future__ import annotations

"""Member-facing web routes for the evidence-bounded citation Agent.

The Agent keeps its database, artifacts and workers separate from the legacy
citation assistant.  Everyone may view the product preview, while every job,
file and mutation remains protected by the site's existing paid-membership
policy and CSRF protection.  Legacy administrator URLs stay as redirects or
API aliases so an open test tab is not broken during the rollout.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import abort, g, jsonify, redirect, render_template, request, send_file, url_for

import citation_agent_test_backend as tasks


_REGISTERED = False


def register_routes(app, web: dict) -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    corpus = web["corpus"]
    mylib_corpus = web["mylib_corpus"]

    def require_admin() -> dict:
        web["_require_admin"]()
        user = getattr(g, "current_user", None)
        if not web["_is_admin_user"](user):
            abort(404)
        return user

    def has_agent_access(user: dict | None) -> bool:
        return bool(web["_citation_assistant_enabled_for_user"](user))

    def require_agent_access() -> dict:
        user = getattr(g, "current_user", None)
        if not user:
            abort(401, description="请先登录会员账号。")
        if not has_agent_access(user):
            abort(403, description="论文插注校注 Agent 正式版仅对有效会员开放。")
        return user

    def access_cta(user: dict | None) -> tuple[str, str]:
        if user:
            return url_for("pricing", next=request.path), "查看会员套餐"
        return url_for("login", next=request.path), "登录并开通会员"

    def validated_scope(user_id: int, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(
            str(value).strip() for value in values if str(value).strip()
        ))
        if not cleaned:
            raise tasks.CitationAssistantError(
                "请先指定至少一部站内著作、卷册或个人文库资料。"
            )
        web["_citation_personal_scope_rows"](user_id, cleaned)
        for token in cleaned:
            if mylib_corpus.submission_id_from_scope_token(token):
                continue
            if token.startswith("book:"):
                key = token[5:].strip()
                if key in corpus.books and corpus.get_book_config(key).available:
                    continue
            elif token.startswith("vol:"):
                key, separator, raw_volume = token[4:].rpartition(":")
                if separator and raw_volume.isdigit() and key in corpus.books:
                    allowed = {int(volume.volume) for volume in corpus.get_volumes(key)}
                    if int(raw_volume) in allowed and corpus.get_book_config(key).available:
                        continue
            raise tasks.CitationAssistantError(
                "所选著作或卷册已不在当前支持文库中，请重新指定。"
            )
        return cleaned[:500]

    def job_payload(job: dict) -> dict:
        public = {
            key: job.get(key) for key in (
                "id", "original_filename", "byte_size", "mode", "note_kind",
                "threshold_mode", "citation_style", "resolved_style",
                "style_confidence", "recognition_depth", "scope", "sections",
                "selected_sections", "flags", "status", "progress_done",
                "progress_total", "candidate_count", "accepted_count",
                "analysis_stage", "agent_status", "agent_verified_count",
                "viewpoint_suggestion_count", "unresolved_count",
                "out_of_scope_count", "skipped_no_evidence_count",
                "direct_agent_status", "paraphrase_agent_status",
                "eligible_record_count", "processed_record_count",
                "deferred_record_count", "completion_reason",
                "auto_insert_eligible_count", "inserted_count",
                "not_inserted_count", "proofread_eligible_count",
                "commented_count", "readonly_count", "word_export_status",
                "pdf_export_status", "pdf_position_failure_count",
                "corpus_sha256", "template_version", "error", "created_at",
                "updated_at", "expires_at",
            )
        }
        public["page_url"] = url_for("citation_agent_job_page", job_id=job["id"])
        public["docx_url"] = (
            url_for("citation_agent_download", job_id=job["id"], artifact="docx")
            if job.get("output_docx_path") else ""
        )
        public["pdf_url"] = (
            url_for("citation_agent_download", job_id=job["id"], artifact="pdf")
            if job.get("output_pdf_path") else ""
        )
        public["pdf_is_annotated"] = "批注版" in Path(
            str(job.get("output_pdf_path") or "")
        ).name
        return public

    @app.after_request
    def citation_agent_test_private_cache_headers(response):
        if (
            request.path.startswith("/citation-agent")
            or request.path.startswith("/api/citation-agent")
            or request.path.startswith("/admin/citation-agent-test")
            or request.path.startswith("/api/admin/citation-agent-test")
        ):
            response.headers["Cache-Control"] = "private, no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response

    @app.get("/citation-agent")
    def citation_agent_page():
        user = getattr(g, "current_user", None)
        if corpus is None:
            abort(503, description="引文语料库尚未就绪。")
        citation_access = has_agent_access(user)
        jobs = []
        if citation_access and user:
            jobs = [
                job_payload(row) for row in tasks.list_jobs(int(user["id"]), limit=20)
            ]
        upgrade_url, access_cta_label = access_cta(user)
        return render_template(
            "citation_agent_test.html", app_name=web["APP_NAME"],
            app_version=web["APP_VERSION"], layout_v2=True,
            layout_page="citation", csrf_token=web["_ensure_csrf_token"](),
            job=None, jobs=jobs, book_scope_tree=web["_book_scope_tree"](),
            max_mb=tasks.MAX_DOCX_BYTES // 1048576,
            gb2025_approved=web["_gb2025_template_approved"](),
            citation_access=citation_access, upgrade_url=upgrade_url,
            access_cta_label=access_cta_label,
        )

    @app.get("/citation-agent/jobs/<job_id>")
    def citation_agent_job_page(job_id: str):
        user = require_agent_access()
        job = tasks.get_job(job_id, int(user["id"]))
        if not job or job.get("status") in {"deleted", "expired"}:
            abort(404, description="任务不存在或已过期。")
        return render_template(
            "citation_agent_test.html", app_name=web["APP_NAME"],
            app_version=web["APP_VERSION"], layout_v2=True,
            layout_page="citation", csrf_token=web["_ensure_csrf_token"](),
            job=job_payload(job), jobs=[], book_scope_tree=web["_book_scope_tree"](),
            max_mb=tasks.MAX_DOCX_BYTES // 1048576,
            gb2025_approved=web["_gb2025_template_approved"](),
            citation_access=True, upgrade_url=url_for("pricing", next=request.path),
            access_cta_label="查看会员套餐",
        )

    @app.get("/admin/citation-agent-test")
    def citation_agent_test_page():
        require_admin()
        return redirect(url_for("citation_agent_page"), code=302)

    @app.get("/admin/citation-agent-test/jobs/<job_id>")
    def citation_agent_test_job_page(job_id: str):
        require_admin()
        return redirect(url_for("citation_agent_job_page", job_id=job_id), code=302)

    @app.post("/api/citation-agent/jobs")
    @app.post("/api/admin/citation-agent-test/jobs")
    def api_citation_agent_create_job():
        user = require_agent_access()
        user_id = int(user["id"])
        if not web["_is_admin_user"](user):
            if tasks.count_active_jobs(user_id) >= tasks.MAX_ACTIVE_PER_USER:
                abort(429, description=f"同时最多可保留 {tasks.MAX_ACTIVE_PER_USER} 个活动任务。")
            local_now = datetime.now(timezone(timedelta(hours=8)))
            local_start = local_now.replace(
                hour=0, minute=0, second=0, microsecond=0,
            ).astimezone(timezone.utc)
            if tasks.count_jobs_since(user_id, local_start.isoformat()) >= tasks.MAX_JOBS_PER_DAY:
                abort(429, description=f"每天最多创建 {tasks.MAX_JOBS_PER_DAY} 个论文任务。")
        upload = (request.files or {}).get("file")
        if upload is None:
            abort(400, description="请选择 .docx 论文文件。")
        style = str(request.form.get("citation_style") or "mkszyj")
        if not web["_gb2025_template_approved"]() and style in {"auto", "gb2025"}:
            abort(400, description="GB/T 7714—2025 尚未核准；请选择其他格式。")
        data = upload.read(tasks.MAX_DOCX_BYTES + 1)
        try:
            scope = json.loads(str(request.form.get("scope") or "[]"))
        except Exception:
            scope = []
        if not isinstance(scope, list):
            scope = []
        try:
            scope = validated_scope(user_id, [str(value) for value in scope[:500]])
            job = tasks.create_job(
                user_id, upload.filename or "论文.docx", data,
                mode=str(request.form.get("mode") or "both"),
                note_kind=str(request.form.get("note_kind") or "footnote"),
                threshold="conservative", citation_style=style,
                recognition_depth=str(request.form.get("recognition_depth") or "direct_only"),
                scope_tokens=scope,
                corpus_sha256=web["_citation_corpus_sha256"](),
                template_version=web["_citation_template_version"](),
            )
        except tasks.CitationAssistantError as exc:
            abort(400, description=str(exc))
        return jsonify({"ok": True, "job": job_payload(job)}), 202

    @app.get("/api/citation-agent/jobs/<job_id>")
    @app.get("/api/admin/citation-agent-test/jobs/<job_id>")
    def api_citation_agent_get_job(job_id: str):
        user = require_agent_access()
        job = tasks.get_job(job_id, int(user["id"]))
        if not job or job.get("status") in {"deleted", "expired"}:
            abort(404, description="任务不存在或已过期。")
        return jsonify({"ok": True, "job": job_payload(job)})

    @app.post("/api/citation-agent/jobs/<job_id>/analyze")
    @app.post("/api/admin/citation-agent-test/jobs/<job_id>/analyze")
    def api_citation_agent_analyze(job_id: str):
        user = require_agent_access()
        user_id = int(user["id"])
        if not tasks.get_job(job_id, user_id):
            abort(404, description="任务不存在。")
        payload = request.get_json(silent=True) or {}
        sections, scope = payload.get("sections") or [], payload.get("scope") or []
        if not isinstance(sections, list) or not isinstance(scope, list):
            abort(400, description="分析范围格式无效。")
        try:
            scope = validated_scope(user_id, [str(value) for value in scope[:500]])
            job = tasks.set_analysis_config(
                job_id, user_id, section_ids=[str(value) for value in sections[:1000]],
                scope_tokens=scope,
            )
        except tasks.CitationAssistantError as exc:
            abort(400, description=str(exc))
        return jsonify({"ok": True, "job": job_payload(job)}), 202

    @app.get("/api/citation-agent/jobs/<job_id>/candidates")
    @app.get("/api/admin/citation-agent-test/jobs/<job_id>/candidates")
    def api_citation_agent_candidates(job_id: str):
        user = require_agent_access()
        if not tasks.get_job(job_id, int(user["id"])):
            abort(404, description="任务不存在。")
        result = tasks.list_candidates(
            job_id, page=request.args.get("page", type=int) or 1,
            page_size=request.args.get("page_size", type=int) or 50,
            kind=str(request.args.get("kind") or ""),
            issue=str(request.args.get("issue") or ""),
            decision=str(request.args.get("decision") or ""),
            section=str(request.args.get("section") or ""),
            bucket=str(request.args.get("bucket") or "actionable"),
        )
        return jsonify({"ok": True, **result})

    @app.patch("/api/citation-agent/jobs/<job_id>/decisions")
    @app.patch("/api/admin/citation-agent-test/jobs/<job_id>/decisions")
    def api_citation_agent_decisions(job_id: str):
        user = require_agent_access()
        decisions = (request.get_json(silent=True) or {}).get("decisions") or []
        if not isinstance(decisions, list):
            abort(400, description="决定列表格式无效。")
        try:
            accepted = tasks.save_decisions(job_id, int(user["id"]), decisions)
        except tasks.CitationAssistantError as exc:
            abort(400, description=str(exc))
        return jsonify({"ok": True, "accepted_count": accepted})

    @app.patch("/api/citation-agent/jobs/<job_id>/decisions/pending")
    @app.patch("/api/admin/citation-agent-test/jobs/<job_id>/decisions/pending")
    def api_citation_agent_pending_decisions(job_id: str):
        user = require_agent_access()
        decision = str((request.get_json(silent=True) or {}).get("decision") or "")
        try:
            result = tasks.bulk_decide_pending(job_id, int(user["id"]), decision)
        except tasks.CitationAssistantError as exc:
            abort(400, description=str(exc))
        return jsonify({"ok": True, **result})

    @app.patch("/api/citation-agent/jobs/<job_id>/citation-style")
    @app.patch("/api/admin/citation-agent-test/jobs/<job_id>/citation-style")
    def api_citation_agent_style(job_id: str):
        user = require_agent_access()
        style = str((request.get_json(silent=True) or {}).get("citation_style") or "")
        if style == "gb2025" and not web["_gb2025_template_approved"]():
            abort(409, description="GB/T 7714—2025 模板尚未核准。")
        try:
            job = tasks.choose_citation_style(job_id, int(user["id"]), style)
        except tasks.CitationAssistantError as exc:
            abort(400, description=str(exc))
        return jsonify({"ok": True, "job": job_payload(job)})

    @app.post("/api/citation-agent/jobs/<job_id>/export")
    @app.post("/api/admin/citation-agent-test/jobs/<job_id>/export")
    def api_citation_agent_export(job_id: str):
        user = require_agent_access()
        job = tasks.get_job(job_id, int(user["id"]))
        if not job:
            abort(404, description="任务不存在。")
        if job.get("citation_style") == "auto" and float(job.get("style_confidence") or 0) < 0.80:
            abort(409, description="请先明确选择引文格式。")
        if job.get("status") != "review_ready":
            abort(409, description="请先完成候选内容审核。")
        tasks.update_job(job_id, status="exporting")
        return jsonify({"ok": True, "status": "exporting"}), 202

    @app.post("/api/citation-agent/jobs/<job_id>/retry-pdf")
    @app.post("/api/admin/citation-agent-test/jobs/<job_id>/retry-pdf")
    def api_citation_agent_retry_pdf(job_id: str):
        user = require_agent_access()
        try:
            job = tasks.queue_pdf_retry(job_id, int(user["id"]))
        except tasks.CitationAssistantError as exc:
            abort(409, description=str(exc))
        return jsonify({"ok": True, "status": "exporting", "job": job_payload(job)}), 202

    @app.get("/api/citation-agent/jobs/<job_id>/download/<artifact>")
    @app.get("/api/admin/citation-agent-test/jobs/<job_id>/download/<artifact>")
    def citation_agent_download(job_id: str, artifact: str):
        user = require_agent_access()
        job = tasks.get_job(job_id, int(user["id"]))
        if not job:
            abort(404, description="任务不存在。")
        if artifact == "docx":
            path = Path(str(job.get("output_docx_path") or ""))
            mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            name = path.name
        elif artifact == "pdf":
            path = Path(str(job.get("output_pdf_path") or ""))
            mimetype, name = "application/pdf", "论文引文校对批注版.pdf"
        else:
            abort(404)
        directory = tasks._job_dir(int(user["id"]), job_id).resolve()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            abort(404, description="导出文件尚未生成或已过期。")
        if directory not in resolved.parents:
            abort(404)
        return send_file(
            resolved, mimetype=mimetype, as_attachment=True,
            download_name=name, conditional=True,
        )

    @app.delete("/api/citation-agent/jobs/<job_id>")
    @app.delete("/api/admin/citation-agent-test/jobs/<job_id>")
    def api_citation_agent_delete_job(job_id: str):
        user = require_agent_access()
        if not tasks.delete_job(job_id, int(user["id"])):
            abort(404, description="任务不存在。")
        return jsonify({"ok": True, "deleted": True})
