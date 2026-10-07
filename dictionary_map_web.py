"""Dictionary graph routes, sharing the application's existing access check."""
import sqlite3
from flask import Blueprint, abort, jsonify, render_template, request, url_for
from werkzeug.exceptions import HTTPException
from dictionary_graph import KINDS, current_graph


def create_blueprint(require_access, page_context):
    bp = Blueprint("dictionary_map", __name__)

    @bp.errorhandler(sqlite3.Error)
    def storage_unavailable(error):
        return jsonify(ok=False,error="概念地图暂不可用，词条目录和正文仍可正常阅读。"),503

    @bp.errorhandler(HTTPException)
    def api_error(error):
        if request.path.startswith("/api/dictionary/"):
            return jsonify(ok=False, error=error.description), error.code
        return error

    @bp.before_request
    def permission():
        require_access("dictionary")

    def graph():
        value = current_graph()
        if value is None:
            abort(503, description="概念地图暂不可用，词条目录和正文仍可正常阅读。")
        version = request.args.get("version")
        if version and version != value.meta["id"]:
            abort(409, description="地图已更新，请刷新后继续。")
        return value

    def options():
        kind = request.args.get("kind", "")
        if kind and kind not in KINDS:
            abort(400, description="未知的关系类型。")
        return dict(inference=request.args.get("inference") == "1", kind=kind)

    def pagination(default, maximum=60):
        try:
            return dict(limit=max(1, min(maximum, int(request.args.get("limit", default)))),
                        offset=max(0, min(10000, int(request.args.get("offset", "0")))))
        except ValueError:
            abort(400, description="数量参数无效。")

    def decorate(payload, value):
        focus = payload.get("focus", {})
        for node in payload.get("nodes", []) + focus.get("nodes", []):
            node["url"] = url_for("dictionary_entry_page", slug=node["slug"])
        for edge in payload.get("edges", []) + focus.get("edges", []):
            for evidence in edge["evidence"]:
                evidence["url"] = url_for("dictionary_entry_page", slug=evidence["slug"], _anchor="paragraph-" + str(evidence["paragraph"]))
        payload.update(ok=True, version=value.meta["id"], coverage=value.meta["coverage"], themes=value.overview())
        response = jsonify(payload)
        # Permissions are checked before data is served; never share graph responses across users.
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @bp.get("/dictionary/map")
    @bp.get("/concept-map")
    def page():
        value = current_graph()
        context = page_context()
        context["layout_page"] = "concept-map"
        return render_template("dictionary_map.html", **context,
                               graph_ready=value is not None, kinds=KINDS)

    @bp.get("/api/dictionary/graph")
    def api_graph():
        value = graph()
        opts = options()
        center = request.args.get("center", "")
        if center:
            try:
                payload = value.neighborhood(center, limit=pagination(12, 59)["limit"], **opts)
            except KeyError:
                abort(404, description="未找到该词条。")
        else:
            payload = value.browse_page(request.args.get("q", ""), request.args.get("theme", ""), **pagination(60))
        return decorate(payload, value)

    @bp.get("/api/dictionary/relations")
    def api_relations():
        value = graph()
        try:
            payload = value.relations(request.args.get("center", ""), group=request.args.get("group", ""),
                                      query=request.args.get("q", ""), picks=request.args.getlist("pick")[:20], **pagination(20), **options())
        except KeyError:
            abort(404, description="未找到该词条。")
        except ValueError:
            abort(400, description="未知的关系分组。")
        return decorate(payload, value)

    @bp.get("/api/dictionary/path")
    def api_path():
        value = graph()
        try:
            payload = value.path_between(request.args.get("from", ""), request.args.get("to", ""), **options())
        except KeyError:
            abort(404, description="请选择有效的起点和终点词条。")
        return decorate(payload, value)

    return bp
