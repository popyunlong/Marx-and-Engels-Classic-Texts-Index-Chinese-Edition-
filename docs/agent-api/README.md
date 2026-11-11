# 公共文献查询 API v1

有效会员在网站独立栏目「API/MCP 数据调用」（`/api-access`）领取个人 Key，账户中心与会员功能页也提供入口。访客可阅读接口说明，管理 Key 需要登录及相应权限。原 `/account/agent-api` 页面跳转至新入口，旧 POST 继续兼容。REST 根路径为 `/api/v1`，MCP 为 `/mcp`。三个客户端使用同一 Key 体系，每账号最多三个有效 Key，全部共享并发和短窗额度。不要把 Key 放在提示词、论文、URL 或公开仓库中。

API/MCP 数据调用 提供原文证据；网页插注校注 Agent 独立提供论文处理流程，两者权限独立。网页检索新增的篇章范围目前不属于 REST/MCP v1 的范围协议；外部调用仍使用 `book:`、`vol:`。

截至 2026-11-11，MCP 已有调用成功记录；最近一次公网 REST 验证返回 Cloudflare 403 / 1010，修复验收尚未完成。本次栏目上线不代表公网 REST 已恢复。

## 使用流程

1. 调用 `list_sources`（`GET /sources`），取得当前公开著作、版本及 `book:` / `vol:` 标识。
2. 调用 `search_corpus`（`POST /search`）：`{"q":"生产关系","mode":"exact","scope":["book:文集"],"limit":10}`。
3. 用返回的 `evidence_id` 调用 `get_passage`（`GET /passages/{evidence_id}`），核对上下文、版本、印刷页码与引文格式。
4. 由外部 Agent 决定是否采用证据，并在用户的文档副本上处理插注或校注。

`exact` 表示规范化文本匹配，可能含繁简、标点等归一化，不能直接称为逐字原文一致。`exact_basis=layout` 表示排版补充命中，必须同时查看 `page_matches` 和 `ignored_layout_types`，不能把被忽略的脚注等内容拼接成新原文。`cooccurrence` 接受 2–5 个以空格分隔的不同关键词；`near_text` 使用现有编辑距离校验，必须指定著作或卷册，并满足现有最短近似查询长度。

共现与近似命中均为待核对候选，不代表论文观点已经得到证实。返回的 `text` 是实际来源文本；`context_pages` 中的起止位置为该字符串内的 Unicode 字符偏移，不是 UTF-16 偏移。缺失的书目信息保持为空，不补造页码或版本。`reader_url` 需要网站正常阅读权限，API Key 不会加入阅读链接。

查询默认只含当前已发布公共文库，永不自动合并个人文库。个人、隐藏、未发布或无效范围一律报错。默认 10 条、最多 20 条；无批量导出和遍历式分页，结果不足时调整范围或查询。`truncated=true` 或 `search_complete=false` 时，不可把结果当作穷尽性结论。

证据引用绑定账号、语料版本，最长保留 24 小时，每账号最近 1000 项、全站最多 50000 项；过期返回 404，版本变化返回 409，重新检索即可。上下文最多相邻各一页、6000 字，并限定于同一作品；无法确认作品边界时只给出命中范围。

## 额度和错误

串行调用；多 Key、多客户端不能增加账号额度。初始全站并发 1、每账号并发 1，每账号每分钟 30 次数据查询；`get_api_limits` 读取额度不占数据查询次数。没有日配额，不调用站方模型，也不消耗站内 AI token。查询扫描预算默认 20 秒，在卷册和候选循环边界检查；原生计算步骤不作强制线程中断。

REST 错误为 `{"ok":false,"error":{"code":"concurrency_limited","message":"…","retryable":true,"retry_after_seconds":2}}`；MCP 在 `structuredContent` 和文字内容中返回同一错误，并设置 `isError`。REST 429/503 同时提供 `Retry-After`。遵守等待时间并添加少量随机延迟，最多重试三次；400/401/403/404/409/504 应先修正输入、凭证或范围，不盲目重试。

客户端断开不会释放仍在运行的服务器查询名额；计算实际结束或进程退出后释放。MCP 与 REST 的数据查询在主站统一扣额，不重复计数。API Key 失效、会员过期和权限撤销在下一次请求生效。Key 管理页面继续要求会话登录和 CSRF。

### REST 调用示例

设置本机环境变量 `MARX_API_KEY` 后，用 Python 发送带有客户端标识的请求。示例保留 HTTP 状态、请求标识和有限错误摘要，便于区分站点鉴权与边缘拒绝。

```python
import json
import os
import urllib.error
import urllib.request

BASE = "https://mazhuzuojiansuo.com/api/v1"
KEY = os.environ["MARX_API_KEY"]

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect())

def call(path, body=None):
    headers = {"Authorization": f"Bearer {KEY}",
               "Accept": "application/json",
               "User-Agent": "MarxCitationClient/1.0"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            details = {"status": error.code}
            for name in ("CF-Ray", "X-Request-ID", "Content-Type", "Retry-After"):
                details[name] = error.headers.get(name, "").replace(KEY, "[REDACTED]")[:200]
            details["summary"] = error.read(4096).decode("utf-8", errors="replace").replace(KEY, "[REDACTED]")[:300]
            raise SystemExit(json.dumps(details, ensure_ascii=False))

sources = call("/sources")
book = next((s for s in sources["sources"] if s["book"] == "文集"), None)
if book is None:
    raise SystemExit("当前公开书目没有文集，请从 /sources 中选择可用范围。")
hits = call("/search", {"q": "生产关系", "mode": "exact",
                        "scope": [book["scope"]], "limit": 10})
print("搜索完整：", hits["search_complete"], "结果截断：", hits["truncated"])
if hits["results"]:
    passage = call("/passages/" + hits["results"][0]["evidence_id"])
    print(json.dumps(passage["result"], ensure_ascii=False, indent=2))
else:
    print("所选范围无结果；请调整查询，不自动扩大范围。")
print(json.dumps(call("/limits"), ensure_ascii=False, indent=2))
```

Cloudflare 返回 `403` 且正文为 `error code: 1010` 时，向站点管理员提供请求时间和 `CF-Ray`，以便核对边缘安全事件。 [Cloudflare 1010 说明](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)

## 客户端

配置示例见本目录的 `codex.toml`、`deepseek-harness.yaml` 和 `workbuddy/`。统一 MCP 地址为 `https://mazhuzuojiansuo.com/mcp`，测试环境应替换为自己的地址。WorkBuddy 的 Key 通过凭证表单注入；Codex 使用环境变量；DeepSeek Harness 使用凭据引用。手动配置或导入不代表已在应用市场发布。

WorkBuddy 可导入包为 `static/agent-api/workbuddy-connector.zip`；三端分别配置和验收的步骤及当前实机状态见 [CLIENT_ACCEPTANCE.md](CLIENT_ACCEPTANCE.md)。

本机安装包的 WorkBuddy SDK 1.29.0（2025-11-25）和 Harness SDK 2.2.0（2026-07-28）已分别首次完成四工具与限流错误兼容验证。桌面导入、模型工具选择和完整 Agent 循环仍待验收。Harness 0.2.1 安装版的环境变量配置另见 `deepseek-harness-0.2.1.yaml`，不以新文档推断旧版本支持全部凭据模板字段。

Codex 0.167.0-alpha.3.2 原生 app-server 的环境变量鉴权、四工具调用及限流解析也已首次通过；使用不持久化的测试上下文，没有启动模型推理或改动用户配置。该证据验证原生调用链，不代表桌面配置和模型选工具均已通过。

- [Codex 官方配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [WorkBuddy 官方连接器说明](https://open.workbuddy.cn/docs/connector)
- [DeepSeek Harness 官方配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.zh.md)
- [官方 MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)

机器可读 REST 文档位于 `/static/agent-api/openapi.json`；工具 schema 由 `agent_api_contract.py` 共用定义并随 MCP `tools/list` 返回。

四个工具分别声明成功输出及结构化错误，定义证据、书目信息、可信页码、原文字符偏移、排版片段和账号额度字段。`publication_year` 沿用现有书目中的字符串或整数；空值和缺失的版本信息不得由客户端自行补造。REST 的 429/503 响应声明 `Retry-After` 秒数。

## 运维交接

主站维持 Python 3.10；MCP 适配进程使用独立 Python 3.11+ 与 `deploy/requirements-agent-api.lock`，不加载语料。发布主机须预先有相应 Python，默认 `/usr/bin/python3.11`，可由协调者设置 `MARX_AGENT_API_PYTHON`。发布前缺失此运行时会直接失败，不自动升级主站运行环境。

正式发布只经 `deploy/release.ps1` 的现有事务与 `/run/lock/marx-search-release.lock`。准备阶段将 MCP 依赖装入候选不可变发布目录，切流前启动指向相同候选主站的 sidecar 并核对 release_id；Caddy 的 `/mcp` 路由与主站在同一配置事务中切换。主站 8000/8001 分别对应 sidecar 18000/18001，均仅监听回环。恢复旧版时删除托管 MCP 路由，保留新运行数据库；退出旧进程前等待连接排空。不得手工覆盖线上源码或单独重载 Caddy。

数据库、锁和证据引用默认位于现有 APPDATA 共享目录，可设置 `MARX_AGENT_API_STATE_DIR` 指定共享位置；所有主站进程及候选必须使用同一目录。该方案针对单主机本地文件系统，不适用于多个独立主机或不可靠的网络文件锁。

配置项：`MARX_AGENT_API_ENABLED`（默认 1）、`MARX_AGENT_API_CONCURRENCY`（1）、`MARX_AGENT_API_USER_CONCURRENCY`（1）、`MARX_AGENT_API_PER_MINUTE`（30）、`MARX_AGENT_API_TIMEOUT_SECONDS`（20）。降低这些限制即可降载；不自动提高 `MARX_SEARCH_CONCURRENCY`。日志只有固定 endpoint、状态、耗时、异常类型，不保存 Key、完整查询或论文正文。

测试台账见 `docs/ops/AGENT_API_TEST_LEDGER_20261107.json`。任何同场景复测（包含后续 CI、构建、发布门禁）必须先查台账并取得专项授权。真实三客户端、Linux 发布演练和生产验收须单独记录，不能用协议测试替代。
