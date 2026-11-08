# PR 草案：为会员开放公共文库 REST 与 MCP 查询

新增 `/api/v1` 四类只读查询及 `/mcp` 四工具，让 WorkBuddy、DeepSeek Harness 和 Codex 使用个人 API Key 读取已发布公共文库、原文证据和出处。网页插注校注 Agent 保留，账户中心新增 API 接入页面、Key 管理、三端配置与 OpenAPI。个人、隐藏或无效范围显式拒绝；近似检索要求范围，候选证据保留匹配类型和页码可信度。

默认全站并发 1、账号并发 1、每账号每分钟 30 次；REST/MCP/多个 Key 共享额度，检索继续经过现有容量闸门。共享目录文件锁跨进程生效，计算结束才释放名额。适配器使用独立 Python 3.11+ 与锁定依赖，不加载语料，不调用站方大模型。应用、适配器及 Caddy 路由统一进入现有发布/回滚事务。

已记录的验证包括后端 22 项、主站集成 2 项、HTTP 联调、两项专项授权复测、WorkBuddy/Harness 安装包 SDK、Codex 原生 MCP 调用、旧版适配器退役，以及网页 Key 表单与配置展示。各项只对应测试台账中的版本。已合入最新 main `4fa835ecc658cbfcf2d9dea759d7844fbcdbac8b`；合并版本未复测。

尚未通过：WorkBuddy/Harness 完整桌面导入与模型流程、三端模型自动选工具、Linux 文件锁及真实 systemd/Caddy 发布/回滚、全量发布门禁和生产容量。不能以安装包 SDK 或替身测试代替这些结论。PR 创建前需专项授权执行推送钩子与 CI 中的重复场景，不跳过钩子。证据及具体缺口见 `AGENT_API_TEST_LEDGER_20261107.json` 和 `AGENT_API_RELEASE_HANDOFF_20261108.md`。
