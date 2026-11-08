# 综合模式仅保留 Flash（2026-11-08）

## 基线与行为

- 实施前通过线上 `/api/runtime` 核实发布 SHA 为 `e63b257a7049a7109e0bb5cfd9561b02efc17bff`，与远端 production 一致。
- 远端 main 为 `2250993505b41b37572aa413bbeff4bfd190db40`；相对线上只增加独立监控代码，AI 对话代码一致。本次分支从该 main 创建。
- 工作区：`D:/CodexData/workspaces/comprehensive-flash-20261108`；分支：`codex/comprehensive-flash-20261108`。
- 综合模式仅显示已授权的 DeepSeek Flash 档位，默认非思考；只有思考权益时回落到可用 Flash 档位。切换模式、恢复会话均重新过滤选项。
- 移除自动选模型入口。旧客户端的缺省/auto 请求仍按任务复杂度在已授权 Flash 档位中选择，不再升级 Pro。
- 旧客户端显式 Pro 请求转为 Flash，保留请求的思考档位并重新校验 Flash 权限；Pro 权益不能代替 Flash 思考权益。其他提供商的显式请求返回 400，无可用自动 Flash 档位返回 403。
- 规划、正文、完成检查和续写沿用同一 Flash 限制。快速问答、研究综述、全局会员模型权益、计费及原有对话生命周期修复保持原实现。API 请求/响应结构不变。

## 一次性验证

执行前已查阅综合模式与对话生命周期历史台账。本次未重复执行测试。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| `tests/test_comprehensive_mode.py` | 39 passed | `D:/CodexData/outputs/comprehensive-flash-20261108/backend.json`、`.log`、`.xml` |
| `tests/browser/comprehensive.cjs` | 6 passed | `D:/CodexData/outputs/comprehensive-flash-20261108/browser.json`、`.log` |

浏览器覆盖桌面/移动端、Flash 档位、Pro 模式切回、思考权限、请求模型、续写、历史恢复、导出与停止保存。后端覆盖新旧请求、复杂任务、续写、禁止 Pro 回退及 Flash 思考授权。上游调用使用模拟结果，无付费请求。

完整版本哈希与运行记录见 `COMPREHENSIVE_FLASH_TEST_LEDGER_20261108.json`。截图和导出样例保留在上述产物目录。未执行全量回归、发布门禁或生产部署，不能将这些定向结果表述为全量验收通过。

## 交付状态

代码和验证结果保留在独立工作区，未提交、推送或修改线上服务。后续发布仍须经仓库规定门禁与指定发布协调人；与本次已运行项目重叠的复测须先获专项授权。

工作区工具最初在 C 盘建立检出，随后完整移动至上述 D 盘路径并用 `git worktree repair` 修复登记。桌面应用重新附加该 D 盘路径时提示不是 managed worktree；Git 工作区和功能分支正常，后续命令应显式使用 D 盘路径。

## 上线准备

用户已要求统筹部署上线。本次只读复核确认远端 main、production 和线上仍为上述版本，五个代码/测试文件与已通过台账的 SHA256 全部一致，现有结果可复用。准备提交本地功能版本；推送、PR 和线上切流尚未执行。

发布流程会重复已有测试，依据本次用户重新提供的 AGENTS.md，须先取得专项复测授权。最小范围为：功能推送一次；PR、main、production 各一次自动流水线（包括 Python 3.10/3.11 编译、快速测试、浏览器、main/production 全量集成与部署 smoke，以及 production 元数据门禁）；推进主线/生产分支时适用的推送钩子；一次本地制包演练和一次正式发布事务必需的编译、元数据、服务器 smoke、候选页面和健康检查。候选检查按现有发布流程进行；失败或中断保留证据，不自动重试，不追加付费模型调用、性能或耐久测试。

获授权后按正常推送、PR 合并、main 门禁、production 快进及 production 门禁执行。每次事务前重新读取 ExpectedLive；制包和发布均通过 `deploy/release.ps1`，本机临时与产物目录显式指定到 D 盘，生产事务使用既有发布锁。切流后核对 runtime、current/release.json、发布账本和 production 提交一致。不得跳过门禁或使用脏源文件发布。

## 首轮发布与缓存修正

- 用户专项授权后，PR #38、main 和 production 三轮 CI 均通过，已检查的生产提交为 `182356d4589e0e4c66593cb279f4713fde58ea46`；一次制包演练通过。
- 正式发布在上传前健康基线阶段主动中断，未启动服务器发布事务、候选服务或切流。服务器 current 仍为 `e63b257a7049a7109e0bb5cfd9561b02efc17bff-20261107T100856Z-8a9a0853`。中断保留为失败记录，不视为发布成功。
- 上线前检查发现此前遗漏脚本资源版本号更新：公开脚本响应为 `Cache-Control: max-age=14400`，原 URL 仍带 `20261107-lifecycle`，已有浏览器可能继续展示旧 Pro 选项。补丁仅把 AI 脚本 URL 更新为 `20261108-comprehensive-flash`，确保重新加载页面时请求新资源。
- 缓存修正尚未重新测试、推送或发布；原有通过记录仍对应原版本，不能声称已验证新模板。需要用户另行专项授权第二轮推送/PR/main/production 必需门禁、一次修正后制包演练和一次正式发布事务后继续。
- 首轮 PR、main、production CI、制包、发布中断与日志均保留于 `D:/CodexData/outputs/comprehensive-flash-20261108`；不得覆盖首轮记录或自动重试。
