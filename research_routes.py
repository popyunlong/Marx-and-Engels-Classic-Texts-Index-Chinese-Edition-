"""Flask integration; all publication mutations require the existing admin + CSRF gates."""
from __future__ import annotations

import json
from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for
import research_updates as r


def register(app, host):
    bp = Blueprint("research_updates", __name__)
    r.init_db()

    @bp.before_request
    def reader_limit():
        if request.endpoint in {"research_updates.latest", "research_updates.issue", "research_updates.article", "research_updates.cite"}:
            limiter = host.get("_rate_limit_reader_ip_or_abort")
            if limiter:
                limiter("researchupdates")

    def member():
        return bool(host["_feature_effective_for_user"]("journal_alerts"))

    def checked_snapshot(issue_id):
        issue = r.get_issue(issue_id)
        if not issue or issue["status"] != "published":
            abort(404)
        if not member():
            abort(403, description="正式研究动态周报需开通会员；可返回栏目阅读公开样刊。")
        return json.loads(issue["snapshot"])

    def page(snapshot=None, article=None, preview=False):
        articles = snapshot.get("articles", []) if snapshot else []
        for a in articles:
            a["issue_label"] = r.issue_label(a)
            a["display_pages"] = r.display_pages(a)
        journals = sorted({a["journal"] for a in articles})
        fields = sorted({a.get("discipline", "") for a in articles if a.get("discipline")})
        q = request.args.get("q", "").strip().casefold()
        filtered = [a for a in articles if (not q or q in " ".join([a["title"], a.get("title_zh", ""), " ".join(a.get("authors", [])), " ".join(a.get("authors_zh", []))]).casefold())
                    and (not request.args.get("journal") or a["journal"] == request.args["journal"])
                    and (not request.args.get("type") or a["type"] == request.args["type"])
                    and (not request.args.get("discipline") or a.get("discipline") == request.args["discipline"])]
        return render_template("research_updates.html", title=r.TITLE, snapshot=snapshot, article=article,
                               articles=filtered, all_articles=articles, journals=journals, fields=fields,
                               archives=r.issues(True) if member() else [], citation=r.citation, preview=preview,
                               is_member=member(), type_labels=r.TYPE_LABELS, legacy_url=url_for("journal_alerts_latest"),
                               journal_chinese_name=r.journal_chinese_name, journal_display_name=r.journal_display_name,
                               journal_ids={j: "journal-" + str(n) for n, j in enumerate(journals)},
                               main_journals={a["journal"] for a in articles if a.get("section") not in {"supplement", "correction"}})

    @bp.get("/research-updates")
    def latest():
        public = r.issues(True)
        if public and member():
            return page(json.loads(public[0]["snapshot"]))
        return page()

    @bp.get("/research-updates/<int:issue_id>")
    def issue(issue_id):
        return page(checked_snapshot(issue_id))

    @bp.get("/research-updates/<int:issue_id>/articles/<int:entry_id>")
    def article(issue_id, entry_id):
        snapshot = checked_snapshot(issue_id)
        a = next((a for a in snapshot["articles"] if a["entry_id"] == entry_id), None)
        if not a:
            abort(404)
        return page(snapshot, a)

    @bp.get("/research-updates/<int:issue_id>/articles/<int:entry_id>/citation.<fmt>")
    def cite(issue_id, entry_id, fmt):
        snapshot = checked_snapshot(issue_id)
        a = next((a for a in snapshot["articles"] if a["entry_id"] == entry_id), None)
        if not a or fmt not in {"ris", "bib"}:
            abort(404)
        return Response(r.export_citation(a, fmt), content_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="research-{entry_id}.{fmt}"'})

    @bp.post("/api/research-updates/imports")
    def imports():
        token = request.headers.get("Authorization", "")
        if not token.startswith("Bearer ") or not r.token_valid(token[7:]):
            return jsonify(error="无效或已撤销的导入凭据"), 401
        try:
            data = request.get_json(silent=True)
            return jsonify(r.import_payload(data)), 200
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify(error=str(exc)), 400

    def admin_data(selected=None, issued_token=""):
        issue_list = r.issues()
        selected = selected or (issue_list[0]["id"] if issue_list else None)
        issue = r.get_issue(selected) if selected else None
        with r.connect() as c:
            imports = [dict(x) for x in c.execute("SELECT id,source_id,result,created_at FROM research_imports ORDER BY id DESC LIMIT 50")]
            tokens = [dict(x) for x in c.execute("SELECT id,label,active,created_at FROM research_tokens ORDER BY id DESC")]
            runs = [dict(x) for x in c.execute("SELECT * FROM research_runs WHERE issue_id=? ORDER BY id DESC LIMIT 135", (selected,))]
            deliveries = [dict(x) for x in c.execute("SELECT id,email,status,error,attempts FROM research_deliveries WHERE issue_id=? ORDER BY id", (selected,))]
            jobs = [dict(x) for x in c.execute("SELECT * FROM research_jobs ORDER BY id DESC LIMIT 15")]
        for x in runs:
            x["details"] = json.loads(x["report"])
        return render_template("research_admin.html", issues=issue_list, issue=issue,
                               rows=r.items(selected) if issue else [], imports=imports, tokens=tokens,
                               runs=runs, deliveries=deliveries, jobs=jobs, token=issued_token,
                               csrf_token=host["_ensure_csrf_token"](), preview_hash=r.preview_hash(selected) if issue else "",
                               citation=r.citation)

    @bp.get("/admin/research-updates")
    def admin():
        host["_require_admin"]()
        return admin_data(request.args.get("issue", type=int))

    @bp.get("/admin/research-updates/imports/<int:import_id>")
    def imported_document(import_id):
        host["_require_admin"]()
        with r.connect() as c:
            row = c.execute("SELECT * FROM research_imports WHERE id=?", (import_id,)).fetchone()
        if not row:
            abort(404)
        return render_template("research_import.html", record=dict(row), payload=json.loads(row["payload"]), result=json.loads(row["result"]))

    @bp.get("/admin/research-updates/<int:issue_id>/preview")
    def preview(issue_id):
        host["_require_admin"]()
        issue = r.get_issue(issue_id)
        if not issue:
            abort(404)
        snapshot = {**issue, "articles": [{**x["article"], "entry_id": x["entry_id"], "section": x["section"]}
                                         for x in r.items(issue_id) if x["review"] != "excluded"]}
        if request.args.get("email"):
            _, rich = r.render_email(snapshot, {}, host["journal_alert_public_base_url"](host["DEPLOYMENT"]))
            return Response(rich, content_type="text/html; charset=utf-8")
        return page(snapshot, preview=True)

    @bp.post("/admin/research-updates")
    def action():
        host["_require_admin"]()
        host["_require_management_csrf"]()
        actor = str((g.current_user or {}).get("id", "admin"))
        issue_id = request.form.get("issue_id", type=int)
        action = request.form.get("action")
        try:
            if action == "token":
                return admin_data(issue_id, r.create_token(request.form.get("label", "国内资料同步")))
            if action == "revoke":
                with r.connect(True) as c:
                    c.execute("UPDATE research_tokens SET active=0 WHERE id=?", (request.form.get("token_id", type=int),))
            elif action == "open":
                issue_id = r.issue_for(request.form.get("period_end") or None)["id"]
            elif action in {"collect", "translate"}:
                issue = r.get_issue(issue_id)
                if not issue or issue["status"] != "draft":
                    raise ValueError("请选择草稿")
                r.enqueue(action, {"issue_id": issue_id, "source_id": request.form.get("source_id", ""), "backfill": bool(request.form.get("backfill"))})
                flash("任务已加入后台队列，可查看任务与逐刊进度。", "success")
            elif action == "review":
                edits = None
                if request.form.get("edit"):
                    edits = {k: request.form.get(k, "") for k in ("title", "title_zh", "abstract", "abstract_zh", "authors", "authors_zh", "keywords", "keywords_zh", "volume", "issue", "year", "pages", "page_start", "page_end", "article_number", "doi", "discipline", "type")}
                r.review(issue_id, request.form.getlist("entry_id", type=int), request.form.get("review", "pending"), actor, edits, request.form.get("section") or None, request.form.get("pagination_decision"), request.form.get("content_override_reason", ""))
            elif action == "screen_content":
                result = r.screen_issue(issue_id, actor)
                flash(f"本次排除 {result['excluded']} 条，纠正类型 {result['retyped']} 条，另有 {result['content_review']} 条需核对内容；保留原目录与筛选依据。", "success")
            elif action == "infer_pages":
                count = r.infer_issue_pages(issue_id, actor)
                flash(f"已生成 {count} 条页码推断；请查看相邻目录依据并确认。", "success")
            elif action == "move":
                r.move_items(issue_id, request.form.getlist("entry_id", type=int), request.form.get("target_id", type=int))
            elif action == "merge":
                r.merge_items(issue_id, request.form.get("entry_id", type=int), request.form.get("into_id", type=int), actor)
            elif action == "publish":
                if request.form.get("confirm") != "yes":
                    raise ValueError("请确认发布当前预览并向有效订阅者发送")
                recipients, _ = host["resolve_journal_recipients"]("subscribers")
                snapshot = r.publish(issue_id, request.form.get("preview_hash", ""), actor, recipients)
                issue_id = snapshot["id"]
                flash("网页已发布；邮件已排队。更正版本不会自动重发邮件。", "success")
            elif action == "retry_mail":
                with r.connect(True) as c:
                    c.execute("UPDATE research_deliveries SET status='queued',updated_at=? WHERE id=? AND status='failed'",
                              (r.now_text(), request.form.get("delivery_id", type=int)))
            elif action == "resolve_mail":
                resolution = request.form.get("resolution")
                if resolution not in {"sent", "queued"} or request.form.get("verified") != "yes":
                    raise ValueError("请先人工核查实际收信情况")
                with r.connect(True) as c:
                    c.execute("UPDATE research_deliveries SET status=?,error=?,updated_at=? WHERE id=? AND status='uncertain'",
                              (resolution, "人工核查：" + actor, r.now_text(), request.form.get("delivery_id", type=int)))
            else:
                raise ValueError("未知操作")
        except (ValueError, TypeError, KeyError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("research_updates.admin", issue=issue_id))

    app.register_blueprint(bp)
    # This endpoint accepts ONLY a scoped bearer token, never cookie credentials.
    host["CSRF_EXEMPT_ENDPOINTS"].add("research_updates.imports")
