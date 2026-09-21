from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


class ApiError(RuntimeError):
    def __init__(self, status, payload):
        self.status, self.payload = status, payload
        super().__init__(payload.get("error", f"HTTP {status}"))


class Client:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def request(self, method, path="", data=None, raw=False, headers=None, timeout=60):
        h = {"Authorization": "Bearer " + self.token, **(headers or {})}
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + "/v1/ingestion" + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                value = response.read()
                return value if raw else json.loads(value)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except ValueError:
                payload = {"error": "服务器返回异常"}
            raise ApiError(exc.code, payload) from None

    def upload(self, batch, path, *, pilot_pages=None, progress=None, cancelled=None):
        path = Path(path)
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(8 << 20), b""):
                if cancelled and cancelled():
                    return None
                digest.update(block)
        b = self.request("POST", "/books", dict(batch=batch, name=path.name, size=path.stat().st_size, sha256=digest.hexdigest()))
        if b["status"] != "uploading":
            return b
        url = "/books/" + b["id"]
        offset = b["offset"]
        with path.open("rb") as f:
            while offset < b["size"]:
                if cancelled and cancelled():
                    return None
                f.seek(offset)
                block = f.read(8 << 20)
                started = time.monotonic()
                try:
                    r = self.request("PUT", url + "/chunks/" + str(offset), block,
                                     headers={"X-Chunk-SHA256": hashlib.sha256(block).hexdigest()})
                    offset = r["offset"]
                except ApiError as exc:
                    if exc.status == 423:
                        if progress:
                            progress(exc.payload["error"])
                        time.sleep(2)
                        continue
                    if exc.status != 409:
                        raise
                    offset = self.request("GET", url)["offset"]
                except (OSError, TimeoutError):
                    # Reconcile an uncertain upload, never blindly append it twice.
                    time.sleep(2)
                    offset = self.request("GET", url)["offset"]
                if progress:
                    progress(f"上传 {path.name}：{offset * 100 // b['size']}%")
                time.sleep(max(0, len(block) / (8 << 20) - (time.monotonic() - started)))
        while not cancelled or not cancelled():
            try:
                return self.request("POST", url + "/complete", {"pilot_pages": pilot_pages} if pilot_pages else {})
            except ApiError as exc:
                if exc.status != 423:
                    raise
                if progress:
                    progress(str(exc))
                time.sleep(2)


class Tunnel:
    def __init__(self, host, user, key, port=8767):
        if not host or any(c.isspace() for c in host) or host.startswith("-"):
            raise ValueError("服务器地址无效")
        if not user or not all(c.isalnum() or c in "_-" for c in user):
            raise ValueError("SSH 用户名无效")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        args = ["ssh", "-N", "-T", "-i", str(Path(key).expanduser()), "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
                "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "-L", f"127.0.0.1:{self.port}:127.0.0.1:{port}", f"{user}@{host}"]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = self.process.stderr.read().decode(errors="replace")[:500]
                raise RuntimeError("SSH 连接失败：" + detail)
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.2):
                    return
            except OSError:
                time.sleep(.1)
        self.close()
        raise TimeoutError("SSH 连接超时")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
