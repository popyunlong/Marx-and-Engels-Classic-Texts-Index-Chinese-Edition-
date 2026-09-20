from __future__ import annotations

"""Network-enabled worker that sees only redacted queue records.

This module intentionally imports neither the web application nor corpus/document code.
"""

import argparse
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import citation_agent_queue as queue  # noqa: E402


PROMPT_VERSION = "citation-agent-actions-v2-flash-thinking"
DEFAULT_ALLOWED_HOSTS = {"api.deepseek.com"}


class AgentWorkerError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = str(code or "agent_error")[:80]


def _endpoint() -> tuple[str, str, str]:
    raw = str(os.environ.get("CITATION_AGENT_API_URL") or "https://api.deepseek.com").strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AgentWorkerError("unsafe_endpoint", "模型地址必须是无内嵌凭据的 HTTPS 地址。")
    allowed = {
        item.strip().lower() for item in str(
            os.environ.get("CITATION_AGENT_ALLOWED_HOSTS") or ",".join(sorted(DEFAULT_ALLOWED_HOSTS))
        ).split(",") if item.strip()
    }
    if parsed.hostname.lower() not in allowed:
        raise AgentWorkerError("host_not_allowed", "模型域名不在允许名单中。")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions"
    endpoint = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    api_key = str(os.environ.get("CITATION_AGENT_API_KEY") or "").strip()
    if len(api_key) < 12:
        raise AgentWorkerError("missing_api_key", "未配置 Agent 专用模型密钥。")
    model = str(os.environ.get("CITATION_AGENT_MODEL") or "deepseek-v4-flash").strip()[:120]
    return endpoint, api_key, model


def _thinking_mode(payload: dict | None = None, *, repair: bool = False) -> str:
    value = str(os.environ.get("CITATION_AGENT_THINKING") or "enabled").strip().lower()
    enabled = value in {"1", "true", "yes", "on", "enabled"}
    effort = str((payload or {}).get("planning_effort") or "deep")
    return "enabled" if enabled and effort == "deep" and not repair else "disabled"


def _opener() -> urllib.request.OpenerDirector:
    proxy = str(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    require_proxy = str(os.environ.get("CITATION_AGENT_REQUIRE_PROXY") or "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    handlers: list = []
    if proxy:
        proxy_url = urllib.parse.urlparse(proxy)
        if proxy_url.scheme not in {"http", "https"} or not proxy_url.hostname:
            raise AgentWorkerError("unsafe_proxy", "HTTPS 出口代理地址无效。")
        handlers.append(urllib.request.ProxyHandler({"https": proxy}))
    else:
        if require_proxy:
            raise AgentWorkerError("proxy_required", "安全策略要求使用限定域名的 HTTPS 出口代理。")
        handlers.append(urllib.request.ProxyHandler({}))
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None
    handlers.append(_NoRedirect())
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return urllib.request.build_opener(*handlers)


def _messages(payload: dict, *, repair: bool = False) -> list[dict[str, str]]:
    tools = ["exact_fragment", "near_quote"]
    if payload.get("recognition_depth") == "direct_and_paraphrase":
        tools.append("keyword_cooccurrence")
    system = (
        "你是受限的论文引文检索规划器。你看不到语料，也不能给出书名、卷次、页码、出处或置信度。"
        "只能从输入片段中原样挑选适合本地核验的短语，或在允许时挑选片段中逐字出现的关键词。"
        "不得请求联网、扩大书库范围或推测来源。只返回一个 JSON 对象，且唯一顶层键为 actions。"
        "每个动作只能含 action_id、record_id、tool、fragment、keywords；工具仅限："
        + "、".join(tools)
        + "。动作总数不超过 16；没有安全动作时返回 {\"actions\":[]}。"
        "action_id 必须是本回答内唯一的小写编号 a1、a2……；record_id 必须逐字复制输入值。"
        "exact_fragment 与 near_quote 必须给出输入 fragment 中逐字存在的 6—240 字连续片段，"
        "并令 keywords=[]；keyword_cooccurrence 只能用于观点转述模式，fragment 必须为空，"
        "keywords 必须是输入 fragment 中逐字存在的 3—5 个、彼此不同且能共同限定命题的概念词；"
        "不要只选‘社会’‘生产’等宽泛词。不得遗漏五个动作字段。"
        "严格按记录 kind 选工具：kind=paraphrase 时只能用 keyword_cooccurrence，优先选原句中的"
        "学术概念核心词（例如‘劳动过程’‘自然’‘物质变换’），不得用 exact_fragment 或 near_quote；"
        "其他 kind 禁止使用 keyword_cooccurrence。对每条 paraphrase 可给两个不同的关键词动作："
        "第一组使用较短的核心名词，第二组使用能限定命题的特征短语；避免选择‘归根到底’‘发展程度’"
        "‘理论谜团’这类论文转述措辞。"
        "示例：{\"actions\":[{\"action_id\":\"a1\",\"record_id\":\"r1\","
        "\"tool\":\"exact_fragment\",\"fragment\":\"输入中的连续原句\",\"keywords\":[]}]}。"
    )
    if int(payload.get("round") or 1) == 2:
        system += (
            "这是第二轮修正。observations 会列出上一轮的工具、片段、关键词和本地结果。"
            "不要重复 result=none 或 rejected 的相同检索；ambiguous 应改用更长、更有区分度的连续片段；"
            "若 keyword_cooccurrence 为 ambiguous，应改用输入中更具体的 3—5 个概念词缩小范围；"
            "若 keyword_cooccurrence 为 none，应换一组更短、更接近经典术语的核心词，不得原样重复；"
            "unique_exact 无需再次检索；near 可尝试更完整的原句。"
        )
    if repair:
        system += " 上一次回答未通过严格结构校验；这一次不得使用 Markdown、解释或额外字段。"
    user_payload = {
        "protocol_version": payload.get("protocol_version"),
        "round": payload.get("round"),
        "recognition_depth": payload.get("recognition_depth"),
        "allowed_public_books": payload.get("allowed_public_books") or [],
        "personal_source_ids": payload.get("personal_source_ids") or [],
        "records": payload.get("records") or [],
        "observations": payload.get("observations") or [],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def _extract_json(text: str) -> dict:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentWorkerError("invalid_json", "模型没有返回有效 JSON。") from exc
    if not isinstance(value, dict):
        raise AgentWorkerError("invalid_json_shape", "模型 JSON 顶层不是对象。")
    return value


def _call_model(payload: dict, *, repair: bool = False) -> tuple[dict, str]:
    endpoint, api_key, model = _endpoint()
    thinking = _thinking_mode(payload, repair=repair)
    body = json.dumps({
        "model": model,
        "messages": _messages(payload, repair=repair),
        "temperature": 0,
        "max_tokens": 1800 if thinking == "enabled" else 1400,
        "thinking": {"type": thinking},
        "response_format": {"type": "json_object"},
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "marx-citation-agent/1",
        },
    )
    try:
        with _opener().open(request, timeout=40) as response:
            raw = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise AgentWorkerError(f"model_http_{int(exc.code)}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise AgentWorkerError("model_unavailable") from exc
    try:
        envelope = json.loads(raw.decode("utf-8"))
        content = envelope["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentWorkerError("invalid_model_envelope") from exc
    return _extract_json(str(content)), model


def process(task: dict, worker_id: str) -> None:
    task_id = str(task["id"])
    request_payload = queue.validate_request(dict(task.get("request") or {}))
    last_error = "invalid_json"
    for attempt in range(2):
        try:
            response, _model = _call_model(request_payload, repair=bool(attempt))
            clean = queue.validate_response(response, request_payload)
            queue.complete(task_id, worker_id, clean)
            return
        except queue.AgentQueueError:
            last_error = "invalid_action_schema"
        except AgentWorkerError as exc:
            last_error = exc.code
            if exc.code not in {"invalid_json", "invalid_json_shape", "invalid_action_schema"}:
                break
    queue.fail(task_id, worker_id, last_error)


def run(*, once: bool = False, poll_seconds: float = 1.0) -> int:
    worker_id = f"agent:{socket.gethostname()}:{os.getpid()}"
    processed = 0
    # A freshly provisioned or explicitly rebuilt queue may be an empty file.
    # Initialise it before cleanup/claim so the isolated worker can cold-start
    # without relying on the web or deterministic worker to win a race first.
    queue.init_queue(queue.QUEUE_PATH)
    while True:
        mode = str(os.environ.get("CITATION_AGENT_TEST_MODE") or "off").strip().lower()
        if mode != "admin_live":
            if once:
                return processed
            time.sleep(max(0.5, poll_seconds))
            continue
        queue.cleanup()
        task = queue.claim(worker_id, lease_seconds=90)
        if task is None:
            if once:
                return processed
            time.sleep(max(0.5, poll_seconds))
            continue
        try:
            process(task, worker_id)
        except Exception:
            # Never log paper fragments or raw model answers.
            queue.fail(str(task["id"]), worker_id, "worker_internal_error")
        processed += 1
        if once:
            return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="脱敏论文校注 Agent 工作节点")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    raise SystemExit(run(once=bool(args.once), poll_seconds=float(args.poll_seconds)))


if __name__ == "__main__":
    main()
