"""Official MCP SDK v2 adapter. Run separately; never import the Flask app/corpus."""
from __future__ import annotations

import asyncio
import json
import os
import ipaddress
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextvars import ContextVar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from jsonschema import Draft202012Validator
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from starlette.responses import JSONResponse

from agent_api_contract import OUTPUTS, TOOLS

AUTH = ContextVar("agent_api_authorization", default="")
TRACE = ContextVar("agent_api_mcp_request_id", default="")
RAY = ContextVar("agent_api_mcp_cf_ray", default="")
LOG = logging.getLogger(__name__)
BASE = os.environ.get("MARX_AGENT_API_UPSTREAM", "http://127.0.0.1:8000/api/v1").rstrip("/")
if urlsplit(BASE).scheme != "http" or urlsplit(BASE).hostname not in {"127.0.0.1", "::1"}:
    raise RuntimeError("API upstream must be a literal loopback HTTP address")


def failure(code, message, retry=0):
    return {"ok": False, "error": {"code": code, "message": message, "retryable": bool(retry), "retry_after_seconds": retry}}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class IngressLimit:
    """Coarse, bounded per-IP protection at the loopback-only MCP ingress."""
    def __init__(self):
        self._clients = OrderedDict()
        self._mutex = threading.Lock()

    def admit(self, address):
        try:
            if not ipaddress.ip_address(address).is_global:
                return 0
        except ValueError:
            return 0
        now = time.monotonic()
        with self._mutex:
            while self._clients and next(iter(self._clients.values()))[-1] <= now - 60:
                self._clients.popitem(last=False)
            calls = self._clients.setdefault(address, deque())
            while calls and calls[0] <= now - 60:
                calls.popleft()
            self._clients.move_to_end(address)
            if len(calls) >= 600:
                return max(1, int(calls[0] + 61 - now))
            calls.append(now)
            while len(self._clients) > 4096:
                self._clients.popitem(last=False)
        return 0


INGRESS = IngressLimit()


def upstream_trace(response):
    request_id = (response.headers or {}).get("X-Request-ID", "")
    if re.fullmatch(r"[0-9a-f]{32}", request_id):
        LOG.info("agent_api_mcp request_id=%s upstream_request_id=%s", TRACE.get(), request_id)


def forward_sync(method, path, authorization, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = Request(BASE + path, data=data, method=method,
                  headers={"Authorization": authorization, "Content-Type": "application/json", "Accept": "application/json",
                           "User-Agent": "MarxCorpusMCP/1.0", "X-MCP-Request-ID": TRACE.get(), "CF-Ray": RAY.get()})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=25) as response:
            upstream_trace(response)
            body = json.loads(response.read(2 * 1024 * 1024))
            if not isinstance(body, dict) or "ok" not in body:
                return 503, failure("upstream_error", "查询服务返回了无法识别的响应。")
            return response.status, body
    except HTTPError as exc:
        upstream_trace(exc)
        try:
            body = json.loads(exc.read(65536))
            if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
                raise ValueError()
        except (ValueError, TypeError):
            body = failure("upstream_error", "查询服务返回了无法识别的响应，请提供请求标识联系管理员。")
        return exc.code, body
    except TimeoutError:
        return 504, failure("upstream_timeout", "查询服务响应超时，执行状态未确认，请勿自动重发。")
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return 504, failure("upstream_timeout", "查询服务响应超时，执行状态未确认，请勿自动重发。")
        return 503, failure("upstream_unavailable", "查询服务响应未确认，请勿自动重发。")
    except (ValueError, OSError):
        return 503, failure("upstream_unavailable", "查询服务响应未确认，请勿自动重发。")


async def forward(method, path, payload=None, authorization=None):
    # Closing an MCP connection never releases the REST server's actual work slot.
    return await asyncio.to_thread(forward_sync, method, path, authorization or AUTH.get(), payload)


async def list_tools(ctx, params):
    return ListToolsResult(tools=[Tool(name=name, description=description, input_schema=schema,
        output_schema=OUTPUTS[name], annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                                         idempotent_hint=True, open_world_hint=False))
        for name, (description, schema, _, _) in TOOLS.items()])


async def call_tool(ctx, params):
    args = params.arguments or {}
    if params.name not in TOOLS:
        body = failure("unknown_tool", "未知工具。")
    else:
        _, schema, method, path = TOOLS[params.name]
        if not Draft202012Validator(schema).is_valid(args):
            body = failure("invalid_arguments", "工具参数不符合 schema。")
        else:
            if params.name == "get_passage":
                path += args["evidence_id"]
            authorization = ctx.request.headers.get("Authorization", "") if ctx.request else ""
            _, body = await forward(method, path, args if method == "POST" else None, authorization)
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
                          structured_content=body, is_error=not body.get("ok", False))


server = Server("marx_search", on_list_tools=list_tools, on_call_tool=call_tool)
public = os.environ.get("PUBLIC_BASE_URL", "https://mazhuzuojiansuo.com").rstrip("/")
host = urlsplit(public).netloc
transport = server.streamable_http_app(stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[public]))


class AuthenticatedApp:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await transport(scope, receive, send)
        trace = TRACE.set(secrets.token_hex(16))
        headers = {key.lower(): value for key, value in scope["headers"]}
        ray = headers.get(b"cf-ray", b"").decode("latin1")
        ray_token = RAY.set(ray if re.fullmatch(r"[0-9a-fA-F]{16,32}-[A-Z]{3}", ray) else "")

        async def respond(event):
            if event["type"] == "http.response.start":
                event = dict(event)
                event["headers"] = [(key, value) for key, value in event.get("headers", [])
                                    if key.lower() not in {b"cache-control", b"x-request-id"}]
                event["headers"].extend([(b"cache-control", b"private, no-store"), (b"x-request-id", TRACE.get().encode())])
                LOG.info("agent_api_mcp request_id=%s cf_ray=%s status=%s", TRACE.get(), RAY.get(), event["status"])
            await send(event)

        try:
            return await self.handle(scope, receive, respond, headers)
        finally:
            TRACE.reset(trace)
            RAY.reset(ray_token)

    async def handle(self, scope, receive, send, headers):
        if scope["path"] == "/health":
            release_path = os.environ.get("APP_RELEASE_FILE")
            release = json.loads(Path(release_path).read_text()) if release_path else {}
            return await JSONResponse({"ok": True, "service": "marx-agent-api-mcp", "release_id": release.get("release_id", "development")})(scope, receive, send)
        if scope["path"] != "/mcp":
            return await JSONResponse(failure("not_found", "接口不存在。"), status_code=404)(scope, receive, send)
        # Caddy's origin lock strips spoofed CF headers from non-Cloudflare traffic.
        # This service must remain bound to loopback, with Caddy as its only public ingress.
        address = headers.get(b"cf-connecting-ip", b"").decode("latin1")
        if not address:
            address = (scope.get("client") or ("", 0))[0]
        retry = INGRESS.admit(address)
        if retry:
            return await JSONResponse(failure("rate_limited", "访问过于频繁，请稍后再试。", retry), status_code=429,
                                      headers={"Retry-After": str(retry)})(scope, receive, send)
        auth = headers.get(b"authorization", b"").decode("latin1")
        if not auth.lower().startswith("bearer marx_") or len(auth) > 150:
            return await JSONResponse(failure("unauthorized", "请提供个人 API Key。"), status_code=401,
                                      headers={"WWW-Authenticate": 'Bearer realm="marx-query-api"'})(scope, receive, send)
        token = AUTH.set(auth)
        try:
            status, body = await forward("GET", "/limits")
            if status != 200:
                response_headers = {}
                if status == 401:
                    response_headers["WWW-Authenticate"] = 'Bearer realm="marx-query-api"'
                error = body.get("error", {})
                if error.get("retryable") and error.get("retry_after_seconds", 0) > 0:
                    response_headers["Retry-After"] = str(error["retry_after_seconds"])
                return await JSONResponse(body, status_code=status, headers=response_headers)(scope, receive, send)
            # Limit protocol envelopes before handing them to the SDK.
            chunks, size = [], 0
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                size += len(event.get("body", b""))
                if size > 16384:
                    return await JSONResponse(failure("request_too_large", "请求超过 16 KiB。"), status_code=413)(scope, receive, send)
                chunks.append(event)
                if not event.get("more_body"):
                    break

            async def replay():
                return chunks.pop(0) if chunks else await receive()

            await transport(scope, replay, send)
        finally:
            AUTH.reset(token)


app = AuthenticatedApp()
