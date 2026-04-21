"""
Flask 本地 Web 界面。

启动：
    python app.py
然后浏览器访问 http://127.0.0.1:5000/
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from build_index import DB_PATH
from search import Corpus


# Windows 本地脚本运行时，自动切换到 pythonw 后台运行，避免弹出黑框。
# 如需保留控制台调试，可先设置环境变量 APP_NO_PYTHONW=1。
def _maybe_relaunch_with_pythonw() -> None:
    if os.name != "nt" or getattr(sys, "frozen", False):
        return
    if os.environ.get("APP_NO_PYTHONW") == "1":
        return
    if os.environ.get("APP_PYTHONW_LAUNCHED") == "1":
        return

    exe_name = Path(sys.executable).name.lower()
    if exe_name != "python.exe":
        return

    pythonw = str(Path(sys.executable).with_name("pythonw.exe"))
    if not Path(pythonw).exists():
        return

    env = os.environ.copy()
    env["APP_PYTHONW_LAUNCHED"] = "1"

    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    subprocess.Popen(
        [pythonw, str(Path(__file__).resolve())],
        cwd=str(Path(__file__).resolve().parent),
        env=env,
        close_fds=True,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    raise SystemExit


_maybe_relaunch_with_pythonw()


# 路径处理：打包成 exe 时，templates/static 在 _MEIPASS 临时解压目录
if getattr(sys, "frozen", False):
    ROOT = Path(sys._MEIPASS)
else:
    ROOT = Path(__file__).resolve().parent

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)


# 启动时一次性加载索引到内存
if not DB_PATH.exists():
    print(
        f"未找到索引 {DB_PATH}。请先运行 `python build_index.py --scan` 然后 `python build_index.py` 构建索引。",
        file=sys.stderr,
    )
    sys.exit(1)

print("正在加载索引到内存……")
corpus = Corpus.load_default()
n_wenji = len(corpus.books.get("文集", []))
n_quanji = len(corpus.books.get("全集", []))
print(f"已加载：文集 {n_wenji} 卷，全集 {n_quanji} 卷。")


# 心跳监控：
# 1) 页面加载后立即开始 ping；
# 2) 启动后给较宽松的宽限期；
# 3) 若长时间收不到心跳，再自动退出。
_last_ping: list[float] = [time.time()]
_GRACE = 45          # 启动宽限期（秒）
_TIMEOUT = 60        # 心跳超时（秒）
_CHECK_INTERVAL = 5  # 轮询检查间隔（秒）


def _shutdown_app() -> None:
    try:
        if os.name == "nt":
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            os.kill(os.getpid(), signal.SIGINT)
    except Exception:
        os._exit(0)


def _watchdog() -> None:
    time.sleep(_GRACE)
    while True:
        time.sleep(_CHECK_INTERVAL)
        if time.time() - _last_ping[0] > _TIMEOUT:
            print("页面已关闭或长时间无心跳，服务自动退出。")
            _shutdown_app()
            break


@app.route("/")
def index():
    return render_template("index.html", n_wenji=n_wenji, n_quanji=n_quanji)


@app.route("/api/ping", methods=["POST"])
def api_ping():
    _last_ping[0] = time.time()
    return "", 204


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "查询为空"}), 400

    hits = corpus.search(q, max_results=5)
    return jsonify(
        {
            "ok": True,
            "query": q,
            "count": len(hits),
            "results": [h.to_dict() for h in hits],
        }
    )


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    def _stop():
        time.sleep(0.2)
        _shutdown_app()

    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    def _wait_and_open_browser() -> None:
        url = "http://127.0.0.1:5000/"
        deadline = time.time() + 30

        # 等 Flask 真正监听成功后再打开浏览器，避免 onefile/exe 场景打开过早。
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", 5000), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)

        try:
            if os.name == "nt":
                os.startfile(url)
            else:
                import webbrowser

                webbrowser.open(url)
        except Exception as e:
            print(f"自动打开浏览器失败，请手动访问 {url}。错误：{e}")

    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_wait_and_open_browser, daemon=True).start()

    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
