from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path


def settings_path():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "MarxIngestionDesktop" / "settings.json"


def protect(value, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("持久保存令牌需要 Windows 用户加密存储")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    source = base64.b64decode(value) if decrypt else value.encode()
    buffer = ctypes.create_string_buffer(source)
    src = Blob(len(source), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    dst = Blob()
    method = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not method(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ctypes.WinError()
    try:
        data = ctypes.string_at(dst.data, dst.size)
        return data.decode() if decrypt else base64.b64encode(data).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(dst.data)


def load():
    path = settings_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save(value):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
