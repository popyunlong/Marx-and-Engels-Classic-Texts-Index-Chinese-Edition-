"""Render the real AI templates without importing stores, AI clients, or user data."""
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, render_template, request
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[2]
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
TEXT = {
    "citation.nav_label": "论文插注校注agent",
    "citation.nav_mobile_label": "论文校注",
    "index.hero_title": "马克思主义理论研究辅助程序",
    "ai_page.title": "AI 研究对话",
    "ai_page.subtitle": "可直接引用原著引文的AI研究问答会话。建议优先使用Deepseek模型，写作水平接近期刊要求。灵活切换“快速问答”和“研究级检索”，适时决定是否开启“引文检索”，根据不同任务需求选择不同档位的模型，将更好地辅助您的研究。",
    "ai_page.quick_title": "AI 随心问",
    "ai_page.quick_description": "连续对话 · 可检索引文库佐证",
    "ai_page.research_title": "研究级检索",
    "ai_page.research_description": "深入研究 · 最多 30 条真实引用",
}


@app.get("/ai")
@app.get("/v2/ai")
def ai_page():
    return render_template(
        "ai.html", layout_v2=request.path.startswith("/v2/"), layout_page="ai",
        app_name="AI controls browser fixture", csrf_token="fixture-csrf",
        current_user=None if request.args.get("guest") == "1" else SimpleNamespace(id=90001, email="fixture@example.test", display_name="测试"),
        search_chat_access_enabled=True, research_access_enabled=True,
        citation_style_groups=[], ai_web_access_enabled=False,
        site_text=lambda key: TEXT.get(key, key),
        url_for=lambda endpoint, **kwargs: ("/static/" + kwargs["filename"]) if endpoint == "static" else "/" + endpoint,
    )


@app.get("/refined-fixture")
def refined_fixture():
    return """<!doctype html><html><head><link rel="stylesheet" href="/static/layout-v2/layout-v2.css">
    <link rel="stylesheet" href="/static/refined-ui/refined.css">
    <script defer src="/static/refined-ui/refined.js"></script></head><body class="v2">
    <div class="refined-portrait"><button class="refined-motion">暂停动态</button></div>
    <a class="v2book" href="/reader"><span class="v2book-cover"><img src="/missing-cover.png" alt="测试书封面">
    <span class="v2book-cover-fallback">封面暂不可用</span></span>测试书</a></body></html>"""


@app.get("/navigation-fixture")
def navigation_fixture():
    # Exercise the actual shell with the long, effective production labels and
    # both account variants, including the optional sponsor control.
    nav = render_template(
        "_appnav.html", layout_page="search", citation_assistant_available=True,
        sponsor_enabled=True,
        current_user=None if request.args.get("guest") == "1" else SimpleNamespace(
            display_name="用于检验较长会员昵称的测试账户", email="fixture@example.test"),
        site_text=lambda key: TEXT.get(key, key),
        url_for=lambda endpoint, **kwargs: "/" + endpoint,
    ).replace("会员功能</a>", "其它会员功能</a>")
    return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<link rel="stylesheet" href="/static/layout-v2/layout-v2.css">'
            '<link rel="stylesheet" href="/static/refined-ui/refined.css">'
            '<style>body{margin:0}</style></head><body class="v2">' + nav + '</body></html>')


if __name__ == "__main__":
    server = make_server("127.0.0.1", 0, app, threaded=True)
    print(f"READY=http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
