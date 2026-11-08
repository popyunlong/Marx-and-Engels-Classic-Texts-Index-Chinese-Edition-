# Agent API 发布准备与剩余门禁

当前阻断：第二轮 CI 的浏览器任务存在 19 项失败，但 tee 掩盖退出码导致 GitHub 显示绿色。不得据此合并或发布；后端/MCP 的有效通过和本地修复见 `AGENT_API_PR40_BROWSER_AUDIT_20261108.md`。再次推送/CI 需另获专项授权。

最新进展：已专项授权执行一次推送门禁并通过，feature `ec26b87` 已推送，草稿 PR #40 已创建；该轮 CI 未通过，本地已修复隔离夹具并首次通过新增失败边界场景。详见 `AGENT_API_PR40_CI_20261108.md`。下文保留原始阶段清单；它不是执行或通过记录。本任务没有推进 main/production 或发布。

## 可审阅版本与基线

- 工作树：`D:/CodexData/workspaces/agent-query-api-20261107`。
- 分支：`codex/agent-query-api-20261107`。
- 实现提交：`88b15c3`；Codex 原生验收：`ae88b23`；网页验收记录：`1c6a0b6`。
- 合入最新主线的提交：`72b150f138af6390d0a7ca9fc884896a260b8fed`。最新文档提交见 Git HEAD。
- 最新 main、production、线上 runtime 的 git_sha 均为 `4fa835ecc658cbfcf2d9dea759d7844fbcdbac8b`。
- 此次观察的线上 release_id：`4fa835ecc658cbfcf2d9dea759d7844fbcdbac8b-20261108T030331Z-9988d447`。发布前必须重新读取，不能将本次观察当作永久预期值。
- 合入主线只有综合对话 Flash 限制、前端缓存标识及相应测试/台账，无 API 文件重叠；未据此推断合并版本测试通过。

## 下一阶段的具体复测范围

AGENTS.md 禁止同次工作自行重复测试，换平台或版本不构成授权。一般性的继续工作不能解除该要求。以下内容在取得针对性的同意后才执行，每项仅一次；失败、中断及进一步改动均保留证据，不自动重试。

1. 推送此 feature 分支时运行 `.githooks/pre-push`：全部已跟踪 Python 文件编译、`test_deploy_manifests.py`、`test_release_metadata.py`、`deployment_smoke.py --mode server`。feature 分支不在本机执行全套 integration。禁止 SKIP_PREPUSH。
2. 创建 PR 后执行该 PR 的一轮 CI：fast-gate（Python 3.10/3.11 编译、deterministic/catalogue/dictionary、shell syntax）、browser-gate，以及 agent-api-gate（后端/主站集成与 HTTP MCP/REST）。其中有已执行场景；Linux/新版本不构成例外。PR 事件不触发 integration-gate。
3. WorkBuddy 与 Harness 各重新准备一次隔离桌面服务/配置，接续原先受阻场景，分别完成连接、查询、读取原文、出处与繁忙提示；WorkBuddy 另核对 Connector ZIP/凭据表单。不得用 SDK 调用替代桌面结果。临时配置先备份，测试 Key 在结束时撤销。

推送和 PR 的具体目标为仓库 `popyunlong/Marx-and-Engels-Classic-Texts-Index-Chinese-Edition-`，head `codex/agent-query-api-20261107`，base `main`；正文使用相邻的 PR_DRAFT 文件。CI 后续 rerun、main/production 推进引发的门禁需要另按台账确认授权。

## 合并与生产发布前置条件

由指定发布协调者处理 production。先完成 PR 审阅、所需客户端验收、Linux 独立 Python 3.11+ 依赖及文件锁验证、真实 sidecar/Caddy 发布和回滚演练、全量 integration 与发布门禁。未获得环境或未通过时记录为未覆盖/未通过，不能扩大 Windows 或替身结果。

生产必须从已检查的 main 快进 production，且发布 checkout 干净、已推送、HEAD 等于 origin/production。所有生产变更经 `deploy/release.ps1`，持有 `/run/lock/marx-search-release.lock`，不得从本 feature 分支发布。协调者读取实时 runtime 后以实际值调用：

```powershell
& .\deploy\release.ps1 -ExpectedLive '<刚读取的 app_release.id>' -ArtifactDirectory 'D:\CodexData\outputs\agent-query-api-20261107\release'
```

发布前确认主机已有独立 Python 3.11+（默认 `/usr/bin/python3.11`），候选准备使用锁定依赖；不升级主站 Python 3.10。主站所有 worker、候选和主端共享同一个 `MARX_AGENT_API_STATE_DIR`。初值保持全站 1、账号 1、每分钟 30，不提高网页 `MARX_SEARCH_CONCURRENCY`。runtime 数据库仅增量建表，不迁移原始 PDF/索引，不输出 Key 或查询正文。

事务须核对候选主站与 MCP release_id，统一切换主站和 `/mcp`，排空后退役旧进程。生产验收包括混合 REST/MCP 额度、会员/撤销即时生效、网页及原 Agent 可用；容量结果单独记录。回滚只使用：

```powershell
& .\deploy\rollback_release.ps1 -ExpectedCurrent '<当前 app_release.id>' -TargetRelease '<已保留的目标 release_id>'
```

旧版不含 MCP 时应撤销托管路由、停用遗留适配器，保留共享运行数据库。保留旧 worktree/备份，满足两次新发布健康 30 天之前不清理。

## 产物与清理状态

权威测试台账：`AGENT_API_TEST_LEDGER_20261107.json`。完整脱敏证据：`D:/CodexData/outputs/agent-query-api-20261107`；网页画面：`browser-center/center-after-revoke.png`。首次全页截图接口失败，改为视口截图留证，不重复表单测试。浏览器临时页已关闭、全部合成 Key 已撤销、已核实身份的测试服务已停止，明文 fixture 文件已移除。

本机 C 盘仍低于 60 GiB，未新增大下载/环境或构建包；未使用 Docker。早先被审批拒绝的递归清理没有绕过执行，必要回退资料继续保留。
