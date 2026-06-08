from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urljoin

from runtime_env import APP_VERSION, APPDATA_DIR, machine_fingerprint, read_data_version


CACHE_PATH = APPDATA_DIR / "desktop_sync.json"
REQUEST_TIMEOUT_SECONDS = 20


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_server_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def load_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_cache(payload: dict[str, Any]) -> None:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def configured_server_url() -> str:
    cache = load_cache()
    return normalize_server_url(
        os.environ.get("DESKTOP_SYNC_SERVER_URL")
        or str(cache.get("server_url") or "")
    )


def _post_json(url: str, payload: dict[str, Any], token: str = "") -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib_request.Request(url, data=body, headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        data = resp.read().decode("utf-8")
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid sync response")
    if not parsed.get("ok", False):
        raise RuntimeError(str(parsed.get("error") or "sync failed"))
    return parsed


def _get_json(url: str, token: str = "") -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib_request.Request(url, headers=headers, method="GET")
    with urllib_request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        data = resp.read().decode("utf-8")
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid sync response")
    if not parsed.get("ok", False):
        raise RuntimeError(str(parsed.get("error") or "request failed"))
    return parsed


def activation_payload(label: str = "") -> dict[str, Any]:
    return {
        "fingerprint": machine_fingerprint(),
        "label": label.strip(),
        "app_version": APP_VERSION,
        "data_version": read_data_version(),
    }


def activate(server_url: str, activation_code: str, label: str = "") -> dict[str, Any]:
    base = normalize_server_url(server_url)
    if not base:
        raise ValueError("server_url is required")
    if not activation_code.strip():
        raise ValueError("activation_code is required")
    response = _post_json(
        urljoin(base + "/", "api/desktop/activate"),
        {**activation_payload(label), "activation_code": activation_code.strip()},
    )
    cache = load_cache()
    cache.update(
        {
            "server_url": base,
            "token": str(response.get("token") or ""),
            "device": response.get("device") or {},
            "license": response.get("license") or {},
            "settings": response.get("settings") or {},
            "release": response.get("release") or {},
            "last_sync_at": utc_now_text(),
            "last_error": "",
        }
    )
    save_cache(cache)
    return cache


def sync() -> dict[str, Any]:
    cache = load_cache()
    base = normalize_server_url(str(cache.get("server_url") or os.environ.get("DESKTOP_SYNC_SERVER_URL") or ""))
    token = str(cache.get("token") or os.environ.get("DESKTOP_SYNC_TOKEN") or "").strip()
    if not base or not token:
        cache["last_error"] = "未配置网站同步地址或本地授权令牌。"
        save_cache(cache)
        return cache
    try:
        response = _post_json(
            urljoin(base + "/", "api/desktop/sync"),
            {
                "fingerprint": machine_fingerprint(),
                "app_version": APP_VERSION,
                "data_version": read_data_version(),
            },
            token=token,
        )
    except Exception as exc:
        cache["last_error"] = str(exc)
        save_cache(cache)
        return cache

    cache.update(
        {
            "server_url": base,
            "token": token,
            "device": response.get("device") or {},
            "license": response.get("license") or {},
            "settings": response.get("settings") or {},
            "release": response.get("release") or {},
            "last_sync_at": utc_now_text(),
            "last_error": "",
        }
    )
    save_cache(cache)
    return cache


def latest_release() -> dict[str, Any]:
    cache = load_cache()
    base = normalize_server_url(str(cache.get("server_url") or ""))
    token = str(cache.get("token") or "").strip()
    if not base:
        return cache.get("release") or {}
    try:
        response = _get_json(
            urljoin(base + "/", f"api/desktop/releases/latest?app_version={APP_VERSION}&data_version={read_data_version()}"),
            token=token,
        )
    except Exception:
        return cache.get("release") or {}
    release = response.get("release") or {}
    cache["release"] = release
    save_cache(cache)
    return release


def proxy_ai(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    cache = load_cache()
    base = normalize_server_url(str(cache.get("server_url") or ""))
    token = str(cache.get("token") or "").strip()
    if not base or not token:
        raise RuntimeError("本地端尚未完成网站授权同步，无法使用服务器 AI。")
    response = _post_json(urljoin(base + "/", path.lstrip("/")), payload, token=token)
    return response


def cached_ai_public_runtime() -> dict[str, Any]:
    cache = load_cache()
    settings = cache.get("settings") or {}
    ai = settings.get("ai") if isinstance(settings, dict) else {}
    if not isinstance(ai, dict):
        ai = {}
    enabled = bool(ai.get("enabled"))
    return {
        "enabled": enabled,
        "provider": str(ai.get("provider") or "server"),
        "model": str(ai.get("model") or ""),
        "base_url": "server-proxy" if enabled else "",
        "search_provider": str(ai.get("search_provider") or ""),
        "search_base_url": "server-proxy" if enabled and ai.get("search_provider") else "",
        "search_enabled": bool(ai.get("search_enabled")),
        "web_search_count": int(ai.get("web_search_count") or 0),
        "request_timeout_seconds": int(ai.get("request_timeout_seconds") or 0),
        "max_history_turns": int(ai.get("max_history_turns") or 0),
        "default_web_enabled": bool(ai.get("default_web_enabled")),
        "search_history_turns": int(ai.get("search_history_turns") or 0),
        "pdf_history_turns": int(ai.get("pdf_history_turns") or 0),
        "search_answer_max_tokens": int(ai.get("search_answer_max_tokens") or 0),
        "pdf_answer_max_tokens": int(ai.get("pdf_answer_max_tokens") or 0),
        "pdf_quick_answer_max_tokens": int(ai.get("pdf_quick_answer_max_tokens") or 0),
        "search_web_search_count": int(ai.get("search_web_search_count") or 0),
        "pdf_web_search_count": int(ai.get("pdf_web_search_count") or 0),
        "pdf_quick_web_search_count": int(ai.get("pdf_quick_web_search_count") or 0),
        "temperature": float(ai.get("temperature") or 0),
        "problems": [] if enabled else ["本地端尚未同步到可用的服务器 AI 配置。"],
    }
