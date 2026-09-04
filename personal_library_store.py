from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from runtime_env import RUNTIME_ROOT

# 个人文库存储/解析/渲染节点（另一台服务器）的客户端。
#
# 为什么不直连 MinIO：节点服务把 S3 完全内部化（MinIO 只监听 127.0.0.1），应用服务器
# 只与节点的 HTTP 接口对话。好处是鉴权、渲染、缓存都在节点侧统一处理，且日后把
# 对象存储换成云 OSS/COS 时应用侧零改动。
#
# 传输安全：节点用自签证书（SAN=IP，10 年），本模块**固定校验该证书**（cafile 指定），
# 而非关闭校验——中间人无法冒充。节点侧另有 nginx IP 白名单 + Bearer token 双保险。
NODE_URL = (os.environ.get("MYLIB_NODE_URL") or "").rstrip("/")
NODE_TOKEN = os.environ.get("MYLIB_NODE_TOKEN") or ""
NODE_CA = os.environ.get("MYLIB_NODE_CA") or str(RUNTIME_ROOT / "config" / "mylib-node.crt")
TIMEOUT_FAST = int(os.environ.get("MYLIB_NODE_TIMEOUT", "30"))
TIMEOUT_SLOW = int(os.environ.get("MYLIB_NODE_TIMEOUT_SLOW", "900"))

_ssl_ctx: ssl.SSLContext | None = None


class StoreError(RuntimeError):
    """节点调用失败。调用方应捕获并落到 failed 状态 + 可读原因，不要让它冒到请求线程。"""


def configured() -> bool:
    """未配置节点时，个人文库功能整体视为不可用（_feature_is_available 会据此下线入口）。"""
    return bool(NODE_URL and NODE_TOKEN)


def _ctx() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        ca = Path(NODE_CA)
        if ca.exists():
            _ssl_ctx = ssl.create_default_context(cafile=str(ca))
            # 自签证书的 CN 不是主机名，靠 SAN=IP 匹配；主机名校验保持开启。
        else:
            # 证书缺失时不静默降级为「不校验」——那等于把中间人风险悄悄留给用户。
            raise StoreError(f"节点证书缺失：{ca}（请从存储服务器复制 tls.crt）")
    return _ssl_ctx


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: bytes | None = None,
    content_type: str = "application/octet-stream",
    timeout: int = TIMEOUT_FAST,
    raw: bool = False,
):
    if not configured():
        raise StoreError("个人文库存储节点未配置（MYLIB_NODE_URL / MYLIB_NODE_TOKEN）。")
    url = NODE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + NODE_TOKEN)
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            payload = resp.read()
            if raw:
                return payload, resp.headers.get("Content-Type", "application/octet-stream")
            return json.loads(payload.decode("utf-8")) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise StoreError(f"节点返回 {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise StoreError(f"无法连接存储节点：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise StoreError(f"存储节点通信失败：{exc}") from exc


# ---------- 接口 ----------
def ping() -> bool:
    """存活探针（匿名端点，不带 token）。"""
    try:
        if not NODE_URL:
            return False
        req = urllib.request.Request(NODE_URL + "/ping")
        with urllib.request.urlopen(req, timeout=8, context=_ctx()) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def health() -> dict:
    return _request("GET", "/health")


def ingest(user_id: int, submission_id: int, pdf_bytes: bytes) -> dict:
    """上传原始 PDF 到存储节点。节点会再做一次魔数校验并回报页数/文字层探测结果。"""
    return _request(
        "POST", "/ingest",
        params={"uid": int(user_id), "sid": int(submission_id)},
        body=pdf_bytes, content_type="application/pdf", timeout=TIMEOUT_SLOW,
    )


def parse(user_id: int, submission_id: int) -> dict:
    """抽取文字层 → 节点侧存 text.jsonl。返回 needs_ocr=True 表示是扫描件。"""
    return _request(
        "POST", "/parse",
        params={"uid": int(user_id), "sid": int(submission_id)}, timeout=TIMEOUT_SLOW,
    )


def fetch_text(user_id: int, submission_id: int) -> list[dict]:
    """取回逐页文本（NDJSON），用于写入该用户的个人检索索引。"""
    payload, _ = _request(
        "GET", "/text",
        params={"uid": int(user_id), "sid": int(submission_id)},
        timeout=TIMEOUT_SLOW, raw=True,
    )
    out: list[dict] = []
    for line in payload.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def render(user_id: int, submission_id: int, page: int, scale: float | None = None) -> tuple[bytes, str]:
    """取某页图像。节点内部三级缓存（本地→S3→现场渲染），故绝大多数请求很快。"""
    return _request(
        "GET", "/render",
        params={"uid": int(user_id), "sid": int(submission_id), "page": int(page), "scale": scale},
        timeout=TIMEOUT_FAST, raw=True,
    )


def prerender(user_id: int, submission_id: int, n: int = 20) -> dict:
    """批准后预渲染前 N 页，保证读者首开即出图（其余按需渲染）。"""
    return _request(
        "POST", "/prerender",
        params={"uid": int(user_id), "sid": int(submission_id), "n": int(n)},
        timeout=TIMEOUT_SLOW,
    )


def ocr_start(user_id: int, submission_id: int, max_pages: int = 0, *, force: bool = False) -> dict:
    """启动忠实 OCR。force 仅供管理员修复既有错误文字层时强制重建。"""
    return _request(
        "POST", "/ocr/start",
        params={
            "uid": int(user_id), "sid": int(submission_id),
            "max_pages": int(max_pages), "force": 1 if force else None,
        },
        timeout=TIMEOUT_FAST,
    )


def ocr_status(user_id: int, submission_id: int) -> dict:
    """OCR 进度：{state: running|done|failed|unknown, done, total, error}。"""
    return _request(
        "GET", "/ocr/status",
        params={"uid": int(user_id), "sid": int(submission_id)}, timeout=TIMEOUT_FAST,
    )


def delete_book(user_id: int, submission_id: int) -> dict:
    """删除该书在存储节点上的全部对象（原件、页图、文本）与节点本地缓存。"""
    return _request(
        "DELETE", "/book",
        params={"uid": int(user_id), "sid": int(submission_id)}, timeout=TIMEOUT_SLOW,
    )


# ---------- 用户荐书（与个人文库对象命名空间隔离） ----------
def ingest_book_recommendation(user_id: int, recommendation_id: int, pdf_bytes: bytes) -> dict:
    """把用户荐书 PDF 暂存到个人文库节点；节点只保存原件，不解析、不建索引。"""
    return _request(
        "POST", "/book-recommendations/source",
        params={"uid": int(user_id), "rid": int(recommendation_id)},
        body=pdf_bytes, content_type="application/pdf", timeout=TIMEOUT_SLOW,
    )


def fetch_book_recommendation(user_id: int, recommendation_id: int) -> tuple[bytes, str]:
    """管理员下载荐书原始 PDF；网站端仍会再次校验管理员身份。"""
    return _request(
        "GET", "/book-recommendations/source",
        params={"uid": int(user_id), "rid": int(recommendation_id)},
        timeout=TIMEOUT_SLOW, raw=True,
    )


# ---------- AI 研究对话云端备份（正文只落个人文库节点） ----------
def _conversation_path(suffix: str = "") -> str:
    suffix = str(suffix or "").strip("/")
    return "/ai-conversations" + (("/" + suffix) if suffix else "")


def _json_body(payload: dict | None) -> bytes:
    return json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def conversation_sync_status(user_id: int) -> dict:
    return _request("GET", _conversation_path("status"), params={"uid": int(user_id)})


def set_conversation_sync_consent(user_id: int, enabled: bool) -> dict:
    return _request(
        "POST", _conversation_path("consent"), params={"uid": int(user_id)},
        body=_json_body({"enabled": bool(enabled)}), content_type="application/json",
    )


def list_conversations(user_id: int) -> dict:
    return _request("GET", _conversation_path(), params={"uid": int(user_id)}, timeout=TIMEOUT_FAST)


def put_conversation(
    user_id: int,
    conversation_id: str,
    conversation: dict,
    *,
    replace_messages: bool = False,
    allow_create: bool = True,
    retention_floor_ms: int = 0,
    retention_cap_ms: int = 0,
) -> dict:
    sid = urllib.parse.quote(str(conversation_id or ""), safe="")
    return _request(
        "PUT", _conversation_path(sid), params={"uid": int(user_id)},
        body=_json_body({
            "conversation": conversation,
            "replace_messages": bool(replace_messages),
            "allow_create": bool(allow_create),
            "retention_floor_ms": max(0, int(retention_floor_ms or 0)),
            "retention_cap_ms": max(0, int(retention_cap_ms or 0)),
        }),
        content_type="application/json", timeout=TIMEOUT_FAST,
    )


def delete_conversation(user_id: int, conversation_id: str) -> dict:
    sid = urllib.parse.quote(str(conversation_id or ""), safe="")
    return _request("DELETE", _conversation_path(sid), params={"uid": int(user_id)})


def cleanup_conversations_before(user_id: int, before_ms: int, *, keep_ids: list[str] | None = None) -> dict:
    return _request(
        "POST", _conversation_path("cleanup-before"), params={"uid": int(user_id)},
        body=_json_body({"before_ms": int(before_ms), "keep_ids": list(keep_ids or [])[:20]}),
        content_type="application/json", timeout=TIMEOUT_FAST,
    )


def extend_conversation(user_id: int, conversation_id: str, *, minimum_expires_at_ms: int = 0) -> dict:
    sid = urllib.parse.quote(str(conversation_id or ""), safe="")
    return _request(
        "POST", _conversation_path(sid + "/extend"), params={"uid": int(user_id)},
        body=_json_body({"minimum_expires_at_ms": max(0, int(minimum_expires_at_ms or 0))}),
        content_type="application/json",
    )


def apply_conversation_retention_policy(
    user_id: int,
    *,
    minimum_expires_at_ms: int = 0,
    fixed_expires_at_ms: int = 0,
) -> dict:
    return _request(
        "POST", _conversation_path("retention-policy"), params={"uid": int(user_id)},
        body=_json_body({
            "minimum_expires_at_ms": max(0, int(minimum_expires_at_ms or 0)),
            "fixed_expires_at_ms": max(0, int(fixed_expires_at_ms or 0)),
        }),
        content_type="application/json",
    )


def list_recoverable_conversations(user_id: int) -> dict:
    return _request("GET", _conversation_path("recoverable"), params={"uid": int(user_id)})


def restore_conversation(user_id: int, conversation_id: str) -> dict:
    sid = urllib.parse.quote(str(conversation_id or ""), safe="")
    return _request("POST", _conversation_path(sid + "/restore"), params={"uid": int(user_id)}, body=b"{}", content_type="application/json")
