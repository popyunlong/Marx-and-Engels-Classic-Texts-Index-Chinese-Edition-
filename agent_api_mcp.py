"""Official MCP SDK v2 adapter. Run separately; never import the Flask app/corpus."""
from __future__ import annotations

import asyncio
import json
import os
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
BASE = os.environ.get("MARX_AGENT_API_UPSTREAM", "http://127.0.0.1:8000/api/v1").rstrip("/")
if urlsplit(BASE).scheme != "http" or urlsplit(BASE).hostname not in {"127.0.0.1", "::1"}:
    raise RuntimeError("API upstream must be a literal loopback HTTP address")


def failure(code, message, retry=0):
    return {"ok": False, "error": {"code": code, "message": message, "retryable": bool(retry), "retry_after_seconds": retry}}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def forward_sync(method, path, authorization, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = Request(BASE + path, data=data, method=method,
                  headers={"Authorization": authorization, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=25) as response:
            return response.status, json.loads(response.read(2 * 1024 * 1024))
    except HTTPError as exc:
        try:
            body = json.loads(exc.read(65536))
            if not isinstance(body, dict) or "error" not in body:
                raise ValueError()
        except (ValueError, TypeError):
            body = failure("upstream_error", "查询服务暂不可用。", 2)
        return exc.code, body
    except (URLError, TimeoutError, ValueError, OSError):
        return 503, failure("upstream_unavailable", "查询服务暂不可用，请稍后重试。", 2)


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
        if scope["path"] == "/health":
            release_path = os.environ.get("APP_RELEASE_FILE")
            release = json.loads(Path(release_path).read_text()) if release_path else {}
            return await JSONResponse({"ok": True, "service": "marx-agent-api-mcp", "release_id": release.get("release_id", "development")})(scope, receive, send)
        if scope["path"] != "/mcp":
            return await JSONResponse(failure("not_found", "接口不存在。"), status_code=404)(scope, receive, send)
        headers = {key.lower(): value for key, value in scope["headers"]}
        auth = headers.get(b"authorization", b"").decode("latin1")
        if not auth.lower().startswith("bearer marx_") or len(auth) > 150:
            return await JSONResponse(failure("unauthorized", "请提供个人 API Key。"), status_code=401,
                                      headers={"WWW-Authenticate": 'Bearer realm="marx-query-api"'})(scope, receive, send)
        token = AUTH.set(auth)
        try:
            status, body = await forward("GET", "/limits")
            if status != 200:
                return await JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})(scope, receive, send)
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
