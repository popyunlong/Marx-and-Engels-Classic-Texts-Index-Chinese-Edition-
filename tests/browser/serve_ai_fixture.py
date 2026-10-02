"""Render the real AI templates without importing stores, AI clients, or user data."""
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, render_template, request
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[2]
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
TEXT = {
    "index.hero_title": "马克思主义理论研究辅助平台",
    "ai_page.title": "AI 研究对话",
    "ai_page.subtitle": "连续对话，检索原著，辅助研究。",
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
        current_user=SimpleNamespace(id=90001, email="fixture@example.test", display_name="测试"),
        search_chat_access_enabled=True, research_access_enabled=True,
        citation_style_groups=[], ai_web_access_enabled=False,
        site_text=lambda key: TEXT.get(key, key),
        url_for=lambda endpoint, **kwargs: "/" + endpoint,
    )


if __name__ == "__main__":
    server = make_server("127.0.0.1", 0, app, threaded=True)
    print(f"READY=http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
