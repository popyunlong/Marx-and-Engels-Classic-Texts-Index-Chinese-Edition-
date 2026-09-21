#!/usr/bin/env python3
"""Convert the quick-chat SSE response to heartbeat-padded JSON."""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPSTREAM = os.environ.get(
    "MARX_AI_SYNC_UPSTREAM",
    "http://127.0.0.1:8000/api/ai/search-chat",
)
LISTEN = ("127.0.0.1", int(os.environ.get("MARX_AI_SYNC_PORT", "8010")))
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = ""
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _json_error(self, status: int, message: str) -> None:
        body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/ai/search-chat":
            self._json_error(404, "not found")
            return
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 2_000_000))
            body = self.rfile.read(length)
            headers = {
                key: value for key, value in self.headers.items()
                if key.lower() not in HOP_HEADERS
            }
            req = urllib.request.Request(UPSTREAM, data=body, headers=headers, method="POST")
            try:
                upstream = urllib.request.urlopen(req, timeout=180)
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            content_type = upstream.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type.lower():
                payload = upstream.read()
                self.send_response(upstream.status)
                self.send_header("Content-Type", content_type or "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                for cookie in upstream.headers.get_all("Set-Cookie", []):
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(payload)
                upstream.close()
                return

            result_queue: queue.Queue[dict] = queue.Queue(maxsize=1)

            def consume() -> None:
                event_name = ""
                data_lines: list[str] = []
                result: dict | None = None
                try:
                    for raw in upstream:
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line:
                            if data_lines:
                                try:
                                    data = json.loads("".join(data_lines))
                                    if event_name in {"done", "error"}:
                                        result = data
                                except json.JSONDecodeError:
                                    pass
                            event_name = ""
                            data_lines = []
                            if result is not None:
                                break
                            continue
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                    if result is None:
                        result = {"ok": False, "error": "AI 回答连接提前结束，请稍后重试。"}
                except Exception as exc:  # keep the browser response valid JSON
                    result = {"ok": False, "error": f"AI 服务连接失败：{exc}"}
                finally:
                    upstream.close()
                    result_queue.put(result)

            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            for cookie in upstream.headers.get_all("Set-Cookie", []):
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.close_connection = True

            while worker.is_alive():
                try:
                    self.wfile.write(b" \n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    upstream.close()
                    return
                worker.join(timeout=2.0)
            payload = json.dumps(result_queue.get(), ensure_ascii=False).encode("utf-8")
            self.wfile.write(payload)
            self.wfile.flush()
        except Exception as exc:
            try:
                self._json_error(502, f"AI 快速问答桥接失败：{exc}")
            except (BrokenPipeError, ConnectionResetError):
                pass


if __name__ == "__main__":
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
