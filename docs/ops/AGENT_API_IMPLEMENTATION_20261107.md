# 外部 Agent 文献查询 API：实现与验收交接

## 基线及交付位置

- 独立分支：`codex/agent-query-api-20261107`；工作树：`D:/CodexData/workspaces/agent-query-api-20261107`。
- 实施起点为最新 main `2250993505b41b37572aa413bbeff4bfd190db40`；收尾再次读取远端，main 未变化。
- production 仍为 `e63b257a7049a7109e0bb5cfd9561b02efc17bff`，实施前线上 runtime 与它一致；未推进或部署 production。
- 本地实现整理为可审阅提交；没有推送、触发 CI 或绕过钩子。提交标识以本分支 Git 记录为准。

## 已实现

提供四个 `/api/v1` REST 接口与相同四工具的 `/mcp` Streamable HTTP，独立查询服务复用现有命中、排版补充、编辑距离、作品边界、去重及引文逻辑。接口只开放当前已发布公共文库，显式非法/个人/隐藏范围拒绝，不回退全库。证据绑定账号与语料版本；返回真实原文、有限上下文、匹配依据、书目、印刷/PDF 页码、可信页码说明和阅读链接。

有效会员及 agent_api/search/viewer 权限每次校验。Key 管理支持创建、命名、撤销及轮换，最多三个有效 Key，明文只在创建响应显示一次。数据库独立、仅增量建表，保存摘要与管理元数据。程序接口只接受 Bearer；网页登录管理仍有 CSRF。网页 Agent 保留，账户中心和 Agent 页面增加接入入口。

默认全站并发 1、每账号 1、每账号每分钟 30 次数据查询；所有 Key、REST/MCP 共享。跨进程名额使用共享目录的操作系统文件锁，并继续进入现有 search_capacity。立即返回结构化 429/503，含等待秒数；不增加网页容量、不扣 AI token、不调用站方模型。计算结束或进程退出才释放实际名额，扫描预算默认 20 秒。

MCP 是独立官方 Python SDK v2 适配进程，不加载语料。锁定 SDK 2.3.0 及独立依赖；主站继续 Python 3.10。生产要求独立 Python 3.11+；本机复用已有 Python 3.14 隔离环境，未安装新的 Linux 运行时。

OpenAPI、逐工具输入/输出 schema、WorkBuddy Connector ZIP、Harness 当前版与 0.2.1 兼容配置、Codex 环境变量配置已交付，详见 `docs/agent-api/README.md` 与 `CLIENT_ACCEPTANCE.md`。

## 验证结果与边界

唯一台账为 `AGENT_API_TEST_LEDGER_20261107.json`；日志和结构化证据保存在 `D:/CodexData/outputs/agent-query-api-20261107`。记录保留开始/结束、源码摘要、成功/失败/中断和产物位置。

| 项目 | 结果 | 覆盖范围 |
|---|---|---|
| 后端首次测试 | 22 passed | 权限、范围、证据、检索模式、排版、跨页/作品边界、版本、额度、Windows 跨进程锁及退出/异常释放 |
| 主站首次集成 | 2 passed | 实际 Flask 挂载、Bearer/CSRF、网页 Agent 边界、配置与 OpenAPI |
| 首次 HTTP 协议联调 | passed | 独立适配器与真实 Flask 测试服务、混合 REST/MCP、共享额度、撤销 |
| 用户明确授权的两项复测 | passed | 契约细化后的 HTTP 联调；仅 OpenAPI/WorkBuddy 配置一致性用例 1 passed |
| WorkBuddy 安装包 SDK 1.29.0 首次兼容验证 | passed | 2025-11-25、发现四工具、来源/检索/正文/额度、结构化输出及繁忙错误 |
| Harness 安装包 SDK 2.2.0 首次兼容验证 | passed | 自动协商 2026-07-28、相同四工具流程与错误解析 |
| Codex 0.167.0-alpha.3.2 原生 app-server 首次验证 | passed | 环境变量 Bearer、四工具发现和直接调用、查询/正文/额度、繁忙错误；不持久化上下文，零模型轮次 |
| 旧版主端 MCP 退役首次集成 | passed | 隔离 ss/systemctl 边界，确认回退至无 MCP 的旧版本时删除路由并停止、禁用遗留主端适配器 |
| 原始差异/脚本语法检查 | passed | 台账 static 对应版本；未单独重跑 |

两项 SDK 验证直接使用本机安装包的官方客户端库，通过既有 Electron Node 运行时调用本机隔离服务；没有重下依赖或借用会员凭证。它们不能替代桌面导入、凭据表单、模型工具选择和完整 Agent 循环验收。

Codex 另完成原生 app-server 调用链验证，使用安装版生成的协议 schema 与官方 app-server 文档。临时覆盖仅在测试进程命令行，Key 经环境变量提供；没有改写 CODEX_HOME、config.toml 或创建持久聊天，没有启动模型轮次。summary 与脱敏 RPC trace 保存在 outputs/codex-native。安装版协议参考快照约 4.6 MB，保留为该次原生验证的版本证据。

发布审阅补齐了一处旧版回退遗漏：恢复不含 MCP 的主端时，除删路由，还要退役失败发布留下的主端 MCP 单元。新隔离场景首次通过；它使用操作系统边界替身，不是 Linux/systemd/Caddy 发布事务实机演练。最新 helper 不沿用旧版独立语法检查结论。

## 桌面接入受阻与清理

WorkBuddy 曾以管理员进程运行，用户完整退出后权限恢复正常；成功进入连接器和自定义 MCP 页面，正常退出重开也成功。随后配置/消息编辑器未触发保存/发送状态，工具还出现请求 WorkBuddy 却返回 Codex 另一聊天画面的情况。用户确认没有其他桌面操作者，不能将该异常归因于用户切窗。验收消息未发送，服务端数据查询计数为 0。

Harness 已正常重开并恢复匹配的截图/控件树；临时条目准备期间没有向测试服务发起查询。其两个配置文件已恢复原文。WorkBuddy 临时 server 条目已移除，测试 Key 已撤销，所有本任务测试服务已退出。原本不存在的 WorkBuddy mcp.json 保留空 mcpServers 结构；界面可能保留本任务未发送草稿和已选的 D 盘隔离工作目录，未删除用户聊天或历史。

早先递归清理任务临时目录被自动审批拒绝，理由仅为 blocked by policy，因此未绕过删除；保留必要台账、日志、合成数据库和配置回退副本。后续测试生成的瞬时 Key 文件已按自身生命周期清理，不新增大下载。

## 尚待完成

- WorkBuddy/Harness 桌面实际操作及三端模型自动选工具的流程；两端 SDK 与 Codex 原生工具调用链已通过，不能扩大成全桌面/Agent 循环通过。
- Connector ZIP 实际导入及凭据表单、真实网页视觉回归。
- Linux/Python 3.11 依赖安装、文件锁平台验证、真实 systemd/Caddy 发布与回滚、生产容量、全量集成/发布门禁。

没有可用本机 WSL 发行版；未安装发行版，未使用或改动 Docker。不得把 Windows/替身结果描述为 Linux 或生产容量结论。

## 发布协调

只由指定协调者通过干净且已推送的 production 分支及 `deploy/release.ps1`，持有 `/run/lock/marx-search-release.lock` 发布，回滚使用既有脚本。应用、MCP 单元和路由纳入同一事务；锁及运行数据库在共享目录，旧版回退不删数据。

本仓库 pre-push 会再次编译 Python、执行部署测试及 smoke；CI 也会重跑部分已执行场景。因此本轮不推送、不绕过钩子。用户“自动决策、继续验收”没有替代 AGENTS.md 要求的专项复测同意。剩余未覆盖场景可首次执行，任何重复场景先查台账并取得具体授权。
