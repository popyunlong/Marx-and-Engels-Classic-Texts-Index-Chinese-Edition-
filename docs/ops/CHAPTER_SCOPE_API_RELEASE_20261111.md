# 篇章检索与 API/MCP 栏目发布记录（2026-11-11）

## 候选内容

基线 main/production：8726f2311ce8dcf9a2a6c3d1ea537152e06d7e18。篇章功能提交：0828a689d347d1384d4560c6aa4f4a4a75d0d528。本会话于 2026-11-11 获用户指定为本次唯一发布协调者。

- 网页范围支持公共文库的著作、卷册、篇章和子章节；范围约束贯穿查询、计数、分页、上下文、历史和现有导出，失效或零结果不扩大范围。
- 独立入口 /api-access 在桌面、移动、账户及会员功能页统一为“API/MCP 数据调用”。说明包含 Key 保存、完整 Python REST 示例、Codex 与通用 MCP 配置、四工具工作流程、页码核对、额度及错误处理。
- 保留原有 Key、agent_api 权限、/api/v1、/mcp、旧 Key 管理 POST；旧 GET 重定向。外部接口不接受篇章标识。
- Cloudflare 1010 修复与公网七请求验收不在本次发布范围，页面标明最近验证状态，不宣称已恢复。

## 验证及授权

查阅 CHAPTER_SCOPE_API_TEST_LEDGER_20261110.json 后，用户专项授权最终候选在本地/推送前、PR/main/production CI、发布脚本和上线验收各触发点执行一次必要检查。失败或中断后的重跑须另行授权。

本轮记录见 CHAPTER_SCOPE_API_RELEASE_LEDGER_20261111.json，以及 D:/CodexData/outputs/chapter-api-release-20261111。

- 首次候选后端：97 passed，3 failed。新增示例测试缺少 Flask 文案上下文；运行日志和源文件指纹保留。
- 用户单独授权上述 3 项复测；修正测试上下文后 3 passed，不重跑其余 97 项。功能源码未随该修正变化。
- 浏览器：5 passed，覆盖篇章选择、请求范围、历史、过期目录与桌面/平板/移动导航；静态差异检查通过。
- 浏览器包装器在保存通过记录后输出 Unicode 符号时发生 GBK 编码错误，测试并未失败或重跑。独立保留包装器错误。
- 以上为本地候选结果，不代表 CI 或生产验收；后续阶段记录到外部台账并绑定实际提交、运行 ID 和线上版本。

## 发布与回退

必须经 PR、main 门禁后将 production 快进到已核验提交；生产 CI 通过后从专用干净 production checkout（HEAD 等于 origin/production）调用 deploy/release.ps1。使用 /run/lock/marx-search-release.lock，记录实时 ExpectedLive，保留候选审查与既有切流/观察门禁。

不迁移账号或 Key，不重建语料。构建、缓存和日志存入 D:/CodexData；复用现有环境。发布失败或关键验收失败时停止，通过 deploy/rollback_release.ps1 使用准确当前/目标版本回退。保留既有工作树和回退副本。

本文件随候选提交，不能单独作为上线凭证；实际发布结果以外部 release-status.json、CI 记录及服务器发布账本为准。
