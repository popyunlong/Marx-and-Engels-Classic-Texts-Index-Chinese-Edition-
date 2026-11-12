# REST 恢复执行记录

## 当前结论

2026-11-12 20:34:59 至 20:35:17（北京时间），启用 Cloudflare 配置规则 `c3a683c86c6042e9a868b416e784c1ee` 后，单次公网复测 7 项全部通过。默认 Python 客户端的两种代理模式目录查询、搜索、正文和额度查询均返回 JSON 200，缺失或无效 Key 均返回 JSON 401。复测使用与故障基线相同的线上版本，报告为 `D:/CodexData/outputs/rest-api-recovery-20261112/after-bic-rule-once/public-rest.json`。

## 故障基线

2026-11-12 20:05（北京时间）的公网验收确认 403/1010 仍存在，线上版本为 `419a7e74a14991c2d4c1f3974dec03255e717c60-20261112T052851Z-dd8a6e34`。两种代理模式的目录、搜索、额度、缺失 Key 和无效 Key 共六个请求均收到 Cloudflare 403/1010。搜索未产生证据 ID，正文请求跳过。验收记录为 `D:/CodexData/outputs/rest-api-recovery-20261112/public-and-protocol-once/public-rest.json`。

目录请求的 CF-Ray 为 `a595687cea4174fd-KIX`（系统代理）和 `a5956880dd0e1bd0-SIN`（禁用代理）。在覆盖验收时段的 Caddy 访问日志中，六个 Ray 均未出现，同期也没有目录、搜索或额度的路径记录。用户导出的 Cloudflare 安全事件确认，第一条目录请求命中了 Browser Integrity Check。记录包含 `source=bic`、`action=block`、`ruleId=bic`。同一 Ray 的另一个事件是 `link_maze_injected`。导出仅包含这一条 Ray，其他请求未取得逐条安全事件证据。

用户选择继续使用电脑、先做后台工作。因此已在本专用工作树准备第二、三阶段的本地补丁，不代表生产启用：重请求 2、轻请求 2、near_text 1，主检索与扫描各 3；API 先验证和获取全部所需名额再计次，独立额度查询不计次；统一应用错误与追踪信息；MCP 超时不建议自动重试并补齐入口粗限流。公网恢复仍是发布前置条件。

获授权的本地批次已各执行一次。六个 pytest 模块共 36 项通过，MCP 故障脚本 3 项通过。REST/MCP 协议脚本在并发检查时因共享存储引入 Werkzeug 而失败，检索、正文及此前的额度读写检查已走通。日志和 JUnit 报告保存在 `D:/CodexData/outputs/rest-api-recovery-20261112/local-validation-once`。

容量校验已移到创建检索池的 Web 端，共享存储保持标准库依赖。获授权的本地协议复测通过，覆盖共享配额、并发拒绝和 Key 撤销。报告为 `D:/CodexData/outputs/rest-api-recovery-20261112/public-and-protocol-once/protocol/protocol-summary.json`。首轮 36 项 pytest 与 3 项 MCP 故障检查对应修正前的版本。分支尚未提交。

源站 Caddy 将 `/mcp` 交给 `127.0.0.1:18000`，REST 进入主应用 `127.0.0.1:8000`，两个服务均为 active。公开 DNS 返回 Cloudflare 地址，不能据此排除 Worker 或 Origin Rules。现有源站日志中只有一个与本次故障无关的 `/api/v1/.env` 扫描，不能把它算作合法 REST 调用失败，也不能用缺少历史请求证明具体拦截原因。

站点安全事件已记录本次目录请求的 BIC 拦截。若路径规则上线后仍有 1010，再按实际链路检查有效配置、规则优先级及 Workers/Pages 托管域名层。

## 已启用的 Cloudflare 规则

用户在 Cloudflare 启用了唯一一条 Configuration Rule。规则匹配主域 `mazhuzuojiansuo.com` 下以 `/api/v1/` 开头的路径，设置 `bic=false`。`deploy/cloudflare/rest_api_bic_rule.candidate.json` 保留该规则的请求体。

应用前读取 BIC、现有 Configuration Rules 与顺序、Worker 路由、重定向/Origin Rules、Cache Rules；保存原配置。确认本站 BIC 是适用拦截层之后，再添加或更新这条配置规则，并确认之后没有匹配规则将 BIC 重新打开。不要删除五条现有自定义 WAF 规则，不要扩大成 `/api/*`，也不要把 User-Agent 当成鉴权凭证。


生产变更由指定发布协调者持有 `/run/lock/marx-search-release.lock` 操作。应用源代码及 Caddy 上游变更遵循 `deploy/release.ps1`；应用回滚使用 `deploy/rollback_release.ps1`。2026-11-12 用户已指定本会话为本次唯一发布协调者。

## 本次应用发布范围与授权

以最新 main/production `419a7e74` 为基线发布本地 REST/MCP 错误处理及调度补丁，并精简 API 页：保留功能、Key 管理、REST/MCP 示例、账号额度和常见问题；删除过期故障公告、重复告诫与内部实施说明。有效的 Cloudflare REST 规则保持不变。

用户专项授权最终候选的本地 `test_agent_api_main_integration`、`test_agent_api_recovery`、`test_api_access` 各一次，以及功能分支/production 推送钩子、PR/main/production CI、发布演练、正式发布内置门禁各一轮。另授权桌面/移动页面检查、上线后原七请求 REST 验收及一次 MCP 四工具调用。失败或中断保留证据，不自动重跑；其余本地旧结果按源码适用范围复用。

本轮追加证据保存至 `D:/CodexData/outputs/rest-api-recovery-20261112/application-release`，逐项绑定候选源码指纹、提交、CI 运行与线上版本。此文件随候选提交，实际是否已上线以外部执行台账及服务器发布账本为准。

## 公网验收记录与复验条件

复用 `D:/CodexData/outputs/rest-403-research-20261111/rest-acceptance-once.py`，不复制改名规避复测限制。其场景包括两种代理模式的目录请求、搜索、证据正文、额度、缺失 Key 和无效 Key，共最多七请求，每个一次、无自动重试。依赖搜索证据的正文请求在搜索失败时记录未执行。前五个场景应为 200 JSON，后两个应为 401 JSON。

2026-11-12 已保留变更前失败报告和变更后通过报告。此后的复测仍按仓库规则取得专项授权。报告记录脱敏诊断头、错误与版本，Key 由环境变量提供。

公网 REST 恢复前置条件已满足。第二、三阶段应用补丁和并发配置仍处于本地工作树，另行走应用发布流程。

## 桌面与存储

当前没有 Cloudflare 管理连接，现有两个 Edge 窗口均已最小化。未恢复窗口或发送键鼠输入，已询问用户可用的控制台操作时段。

用户已答复：仍在使用电脑，先做后台工作。控制台读取继续推迟，不自行恢复窗口。

工作树工具先创建了 C 盘副本，随后逐文件 SHA-256 校验复制到 D 盘并修复 Git 指向。自动审批拒绝包含删除源副本的命令，理由为 `blocked by policy`，没有绕过执行。C 盘旧副本保留，后续操作必须显式使用 `D:/CodexData/workspaces/rest-api-recovery-20261112`，不能同时使用两份副本开发。

参考：

- [Cloudflare 1010 与托管域名限制](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)
- [按路径配置 Browser Integrity Check](https://developers.cloudflare.com/rules/configuration-rules/settings/#browser-integrity-check)
