from __future__ import annotations

import argparse
import json
import os
import threading
import time
import html
import re
from pathlib import Path


def prepare_embedded_worker(worker_module):
    # Only the publisher may retire a serving HTTP generation after draining its
    # connections. The standalone worker's os.exec reload would also replace
    # the entire web process when run inside a thread during the next release.
    worker_module._reload_if_corpus_changed = lambda loaded_fingerprint: None


def run_activated_worker(run, activation_file=None, *, wait=time.sleep):
    # run() recovers jobs and performs maintenance before its first claim.
    # A warm, unactivated executor must not reach any of those writes.
    while activation_file is not None and not activation_file.exists():
        wait(.2)
    return run()


def expose_worker_health(app_module, worker_thread, activation_file=None):
    original=app_module.app.view_functions['api_runtime']
    def runtime_health():
        response=app_module.app.make_response(original())
        payload=response.get_json()
        payload['ingestion_worker']={'alive':worker_thread.is_alive(),
            'enabled':activation_file is None or activation_file.exists()}
        response.set_data(json.dumps(payload,ensure_ascii=False))
        return response
    app_module.app.view_functions['api_runtime']=runtime_health


def verify(app_module, record):
    """Pre-listen gate uses the loaded corpus; no second copy or public bypass."""
    corpus = app_module.corpus
    if corpus is None:
        raise RuntimeError("候选语料未加载")
    from build_index import normalize
    guards = {name: getattr(app_module, name) for name in [
        "_require_reader_asset_access", "_admin_content_access_enabled", "_require_search",
        "_require_content_feature", "_require_feature", "_rate_limit_or_abort", "_record_community_trend"]}
    try:
        app_module._require_reader_asset_access = lambda: None
        app_module._admin_content_access_enabled = lambda: True
        app_module._require_search = lambda: None
        app_module._require_content_feature = lambda _: None
        app_module._require_feature = lambda _: None
        # Local pre-listen tests must not consume visitor quotas or write
        # synthetic queries into production community trend data.
        app_module._rate_limit_or_abort = lambda *a, **k: None
        app_module._record_community_trend = lambda *a, **k: None
        with app_module.app.test_client() as client:
            with client.session_transaction() as session:
                session["_csrf_token"] = "ingestion-local-acceptance"
            for item in record["books"]:
                volume = corpus.get_volume_by_source_file(item["source_file"])
                if volume is None or len(volume.pages) != item["pages"]:
                    raise RuntimeError("阅读器书目或页数不符")
                if not corpus.get_toc_entries(item["source_file"]):
                    raise RuntimeError("篇章目录缺失")
                body = [p for p in volume.pages if len(normalize(p.raw_text)) >= 80]
                if not body:
                    raise RuntimeError("正文抽样页缺失")
                for page in [body[0], body[len(body) // 2], body[-1]]:
                    probe = normalize(page.raw_text)[20:44]
                    hits, _ = corpus._exact_in_book(item["key"], probe, probe, limit=100)
                    if not any(h.source_file == item["source_file"] and any(p.pdf_page == page.pdf_page for p in h.pages) for h in hits):
                        raise RuntimeError("首中尾引文回跳验收失败")
                    context = client.get("/api/pdf-page-context", query_string={"file": item["source_file"], "page": page.pdf_page})
                    if context.status_code != 200 or not (context.get_json() or {}).get("ok"):
                        raise RuntimeError("AI 原文上下文验收失败")
                pdf = client.get("/pdf", query_string={"file": item["source_file"]}, headers={"Range": "bytes=0-1023"})
                if pdf.status_code != 206 or len(pdf.data) != 1024:
                    raise RuntimeError("PDF 阅读分段加载失败")
                toc = client.get("/api/library/volume-toc", query_string={"file": item["source_file"]})
                if toc.status_code != 200 or not (toc.get_json() or {}).get("results"):
                    raise RuntimeError("篇章直达接口失败")
                search = client.post("/api/search", json={"q": probe, "book": item["key"]},
                                     headers={"X-CSRF-Token": "ingestion-local-acceptance"})
                if search.status_code != 200 or not (search.get_json() or {}).get("ok"):
                    payload = search.get_json(silent=True) or {}
                    raise RuntimeError(f"引文检索接口失败: {item['key']} status={search.status_code} error={payload.get('error','')}")
                print(json.dumps({'preflight_book':item['key'],'routes_ok':True,'at':time.time()},ensure_ascii=False),flush=True)
            for route in ["/v2/read", "/api/ai/assistant-config"]:
                response = client.get(route)
                if response.status_code != 200:
                    raise RuntimeError("阅读或 AI 范围接口失败")
                text=html.unescape(response.get_data(as_text=True))
                if route=='/v2/read':
                    match=re.search(r'<script id="v2ReadNav" type="application/json">(.*?)</script>',text,re.S)
                    if not match:
                        raise RuntimeError('阅读导航数据缺失')
                    text=json.dumps(json.loads(match[1]),ensure_ascii=False)
                for item in record["books"]:
                    if item["key"] not in text and json.dumps(item["key"], ensure_ascii=True)[1:-1] not in text:
                        raise RuntimeError(f"书目范围缺少新书: route={route} book={item['key']}")
    finally:
        for name, value in guards.items():
            setattr(app_module, name, value)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--worker", action="store_true")
    p.add_argument("--with-worker", action="store_true")
    p.add_argument("--preflight", action="store_true")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--activation-file", type=Path)
    p.add_argument("--pinned-template", type=Path)
    args = p.parse_args()
    if args.pinned_template and (not args.worker or args.with_worker):
        p.error('--pinned-template is only for a retained standalone executor')
    os.environ["CITATION_ASSISTANT_INLINE_WORKER"] = "0"
    os.environ["PORT"] = str(args.port)
    os.environ["BIND_HOST"] = "127.0.0.1"
    import app
    from .scopes import install as install_scopes
    install_scopes(app)
    from .paddle_runtime import install as install_paddle
    install_paddle(app)
    from .generations import install
    install(app, Path.cwd() / "data/ingestion-generations.json")
    if args.pinned_template:
        from .pinned_template import install as install_pinned_template
        install_pinned_template(app, args.pinned_template)
    if args.activation_file:
        from .activation import install as install_gate
        import search_exports
        install_gate([app.citation_tasks, search_exports], args.activation_file)
    if args.preflight:
        verify(app, json.loads((Path.cwd() / "candidate.json").read_text(encoding="utf-8")))
        (Path.cwd() / "runtime-verified.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    from scripts import citation_assistant_worker as worker_module
    run = worker_module.run
    if args.worker:
        run_activated_worker(run, args.activation_file)
        return
    if args.with_worker:
        prepare_embedded_worker(worker_module)
        def consume():
            run_activated_worker(run, args.activation_file)
        worker_thread=threading.Thread(target=consume, name="ingestion-citation-worker", daemon=True)
        worker_thread.start()
        expose_worker_health(app,worker_thread,args.activation_file)
    app.run_waitress()


if __name__ == "__main__":
    main()
