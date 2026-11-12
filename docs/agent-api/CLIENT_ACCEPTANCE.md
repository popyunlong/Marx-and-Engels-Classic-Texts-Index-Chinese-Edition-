# 三客户端接入与验收状态

三个客户端共用 `/mcp`、相同四个只读工具、个人 Key、公共文库范围和账号额度。配置交付、安装包 SDK 兼容性、桌面与 Agent 实际流程分别记录。

## 当前证据

2026-11-08 用户另授权两端桌面各重试一次；仍在准备阶段遇到目标窗口/画面不匹配，Harness 另报点击 geometry unavailable。未发送模型消息，两端 api_calls 均为 0。所有测试 Key 已撤销、服务已停止，WorkBuddy 配置按备份恢复，Harness 配置未改。该次受阻记录保留在台账及 `docs/ops/AGENT_API_PR40_CI_20261108.md`，不能将其计为桌面通过。

| 客户端 | 已交付 | 安装包 SDK 兼容性 | 桌面/Agent 完整流程 |
|---|---|---|---|
| WorkBuddy 5.9.3 | Connector ZIP、Token 表单、streamableHttp 配置 | SDK 1.29.0 首次通过，协议 2025-11-25 | 未完成；导航已恢复，编辑/发送及画面捕获受阻 |
| DeepSeek Harness 0.2.1-rc.2 | 当前版凭据引用 YAML、旧版环境变量兼容 YAML | SDK 2.2.0 首次通过，自动协商 2026-07-28 | 未完成；临时配置已回退 |
| Codex 0.167.0-alpha.3.2 | 远程 MCP TOML、bearer_token_env_var | 原生 app-server 工具调用链首次通过 | 原生连接/查询/正文/限流已验证；桌面与模型工具选择未覆盖 |

两项 SDK 场景通过本机安装包的官方客户端库分别完成：发现四工具 → list_sources → search_corpus → get_passage → get_api_limits → 名额忙时解析 isError、concurrency_limited 和等待秒数。证据在 outputs 下 bundled-sdk；不得把它们称为桌面导入或模型工具调用已通过。

## WorkBuddy

可导入包为 `static/agent-api/workbuddy-connector.zip`，根目录含 `connector-meta.json`、`mcp.json`、`token-schema.json`、`icon.svg`，包内无用户凭证。类型 MCP，传输 streamableHttp，认证 token，最低版本 4.24.0。通过客户端支持的本地 Connector 导入入口导入并填写个人 Key；实际 ZIP 导入与表单注入仍需验证。

本机 5.9.3 的实际手动入口为左侧「专家·技能·连接器」→ 页面顶端「连接器」→「自定义连接器」→「添加 MCP」。配置路径显示为 `~/.workbuddy/mcp.json`。可在此配置标准 MCP；此操作与 Connector 包导入不是同一场景。

本轮只创建临时本机配置，未连接线上会员数据。成功导航和正常退出重开后，编辑器未进入可保存/发送状态，捕获画面又与目标窗口不符，未发送模型请求。已删除临时 server 条目并撤销 Key，数据查询次数为 0。目录选择为 D 盘隔离工作空间；未触碰用户文档。

包由 `scripts/package_agent_connector.py` 生成，更新源文件时同步生成，不混用过期 ZIP。依据：[官方 Connector 文档](https://open.workbuddy.cn/docs/connector)。

## DeepSeek Harness

`deepseek-harness.yaml` 面向当前官方说明：在目标 Agent 的插件作用域添加 `@deepseek-ai/dsh-mcp-client` 条目，使用 streamable-http、expandTemplates 与 `${credential:MARX_API_KEY}`，凭据提供方须已加载。

本机安装版 0.2.1-rc.2 的配套说明提供 `!!js` 从启动环境读取 Key 的方式，见 `deepseek-harness-0.2.1.yaml`。它不把 Key 明文写入配置；启动进程须继承 MARX_API_KEY。该兼容 YAML 的完整配置/Agent 流程尚未验收，不能只凭 SDK 联调宣布通过。

「账号菜单 → 设置 → 打开配置文件」定位到 desktop profile 的 cordis.patch.yml；它用于覆盖已有条目。新增插件放入相同 profile 的自定义配置，并保持目标 Agent 作用域，不能仅在没有工具服务的全局作用域追加后就认定已启用。临时配置均先备份并已恢复。

成功后应出现 `mcp__marx_search__list_sources` 等工具；连接失败、等待凭据与未加载工具要明确区分。依据：[官方 MCP 客户端说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.zh.md)。

## Codex

合并 `codex.toml` 中的 MCP 条目，通过 bearer_token_env_var 读取个人 Key。当前聊天运行在 Codex 中不代表已连接新 MCP。

已使用实际安装的 Codex 0.167.0-alpha.3.2 app-server，通过环境变量注入测试 Key、命令行临时覆盖服务配置，创建不持久化的测试上下文，直接执行原生 `mcpServer/tool/call`。Bearer 认证、四工具发现、来源/检索/正文/额度及繁忙错误均首次通过。没有启动模型轮次、创建用户聊天或修改 config.toml，测试进程和 Key 已收回。证据在 outputs 下 codex-native 的 summary.json 与 RPC trace。

此场景验证原生 MCP 调用链；没有覆盖桌面配置导入或模型自动选工具。协议参照安装版生成的 schema 和[官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)，不依赖 Codex UI 自动化。

## 后续真实客户端场景

每端分别完成一次连接/四工具发现、公共范围查询、证据正文读取及出处核对、忙时限流说明。同账号数据请求串行调用；仅明确 retryable=true 的限流或执行前忙碌可按返回间隔退避，最多重试两次；超时或执行状态不明不自动重发。测试脚本仍不自动重试，按台账逐次取得授权。用合成语料、隔离账号和可撤销测试 Key，不在提示词中放凭证。

台账 `docs/ops/AGENT_API_TEST_LEDGER_20261107.json` 保存全部结果和受阻记录。任何失败、中断或已通过场景的重试都需专项同意；不以改脚本、改平台或改名规避。桌面控制恢复前不反复尝试已受阻的输入，也不将协议/SDK 测试换称完整客户端验收。
