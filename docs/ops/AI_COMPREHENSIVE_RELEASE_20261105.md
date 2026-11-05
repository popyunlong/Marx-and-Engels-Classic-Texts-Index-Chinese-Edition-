# 综合模式管理员灰度发布准备

用户要求先上线后实测。本次发布目标为管理员灰度，普通用户功能开关继续关闭。此文件为发布准备记录，不是已上线凭据。

## 核对结果

- 功能工作树：D:/CodexData/workspaces/ai-comprehensive-20261105
- 功能分支：codex/ai-comprehensive-20261105
- 远端 main / production：4ee1b5d4d315b0740050002728c2559381b3e028
- 公网当前版本：4ee1b5d4d315b0740050002728c2559381b3e028-20261105T001408Z-b51a8f92
- 本机已安装 Python 3.10，发布构建无需下载新解释器。
- 既有测试及最小授权复测结果见 AI_COMPREHENSIVE_TEST_LEDGER_20261105.json。
- CI 配置已纳入综合模式后端和浏览器用例；尚未执行此配置。

## 灰度配置与实测入口

AI_COMPREHENSIVE_ENABLED=0、DEEPSEEK_WEB_ENABLED=0；两个 ADMIN_PREVIEW 默认开启。由真实管理员账号登录 https://mazhuzuojiansuo.com/v2/ai 后使用综合模式与独立联网控件。发布前核对有效运行配置，不能只根据源码默认值声称灰度已生效。

## 发布顺序与授权边界

1. 提交功能分支，复用已保存的本地验证证据。未创建会自动运行重复测试的 PR。
2. 请求明确授权：本会话作为本次唯一发布协调者；仅允许发布流程强制要求的重复编译、快速测试、浏览器回归、集成测试、元数据检查、构建演练和候选健康门禁。范围限 pre-push、PR、main、production 及正式发布事务，不重复额外性能或耐久测试。失败或中断不自行重新触发流水线。
3. 授权后，通过 PR/main 门禁，将 production 快进到同一已核验 main，使用干净且已推送、与 origin/production 一致的 production 构建。
4. 先 deploy/release.ps1 -DryRun，再在全局锁 /run/lock/marx-search-release.lock 下执行正式事务。重新读取真实 ExpectedLive，发现变化立即停止旧预期事务。
5. 候选审批保留必需的元数据、数据绑定与健康检查。管理员实际 AI 内容验收安排在灰度上线后；不伪造任何预上线内容实测证据。
6. 完成后核对公网、current/release.json、发布账本及 Git 版本一致，再给出实测链接；回退仅使用 deploy/rollback_release.ps1。

目前未创建 PR、未触发 CI、未修改生产服务或生产配置。