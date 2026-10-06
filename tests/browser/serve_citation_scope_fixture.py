"""Render real Agent pages without loading application stores or workers."""
from flask import render_template, request
from werkzeug.serving import make_server
from serve_ai_fixture import app

TREE = [
    {"id": "marx_engels", "label": "马克思 · 恩格斯", "books": [
        {"key": "文集", "label": "马克思恩格斯文集", "volumes": [1, 2, 3]},
        {"key": "全集", "label": "马克思恩格斯全集", "volumes": [1, 2]},
    ]},
    {"id": "lenin", "label": "列宁", "books": [{"key": "列宁全集", "label": "列宁全集", "volumes": [1, 2]}]},
    {"id": "mylib", "label": "我的文库（私有）", "books": [
        {"key": "mylib:fixture", "label": "个人资料", "volumes": [], "personal": True},
    ]},
]


@app.get("/citation-agent")
@app.get("/citation-agent/jobs/fixture")
def citation_page():
    tree = TREE
    if request.args.get("large"):
        tree = [{"id": "large", "label": "测试类别", "books": [
            {"key": f"b{n}", "label": f"测试书 {n}", "volumes": []} for n in range(501)
        ]}]
    job = None
    if "/jobs/" in request.path:
        job = {"id": "fixture", "status": request.args.get("status", "awaiting_sections"),
               "scope": ["vol:文集:2", "book:列宁全集", "book:mylib:fixture"],
               "sections": [{"id": "body", "title": "正文", "start": 0, "end": 2}],
               "selected_sections": ["body"], "mode": "both"}
    return render_template("citation_agent_test.html", app_name="论文 Agent 测试",
        csrf_token="fixture", job=job, jobs=[], book_scope_tree=tree,
        citation_access=True, max_mb=30, gb2025_approved=False,
        citation_style_options=[], citation_style_groups=[],
        upgrade_url="/pricing", access_cta_label="会员",
        site_text=lambda key: key, url_for=lambda endpoint, **kwargs: "/" + endpoint)


if __name__ == "__main__":
    server = make_server("127.0.0.1", 0, app, threaded=True)
    print(f"READY=http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
