# AI 研究对话改进发布准备

用户已指示“推送上线”，后续再次提出“可考虑进行部署”。本会话负责推进本次发布准备；综合模式延续管理员灰度，不扩大普通用户开放范围。

2026-11-07 核对远端 main、production 及公网运行版本一致：

- Git：`62d5980322dc8b5d8861218993f41af08032aec6`
- 线上发布：`62d5980322dc8b5d8861218993f41af08032aec6-20261105T024554Z-400ab3be`
- 功能分支：`codex/ai-dialogue-polish-20261106`
- 工作树：`D:/CodexData/workspaces/ai-dialogue-polish-20261106`

已有场景、失败记录及最小授权复测结果见 `AI_DIALOGUE_POLISH_TEST_LEDGER_20261106.json`。本轮只读准备未重跑任何测试。新增后端和浏览器场景已加入 CI 必需门禁。

## 发布顺序

1. 提交本次源文件、测试、CI 配置和台账，复用已有快速验证记录。
2. 请求专项复测授权，范围仅限推送钩子、PR/main/production CI、制包演练和正式发布事务强制要求的编译、快速测试、浏览器回归、全套集成、部署 smoke、元数据及候选健康/页面检查；每一阶段只触发一次，失败或中断不自动重试。不追加真实付费模型、性能或耐久测试。
3. 获批后正常推送功能分支并创建 PR，通过门禁后合并 main。production 只能从核验后的 main 快进，并不得修改其他会话工作树或强推分支。
4. 使用干净、已推送且 HEAD 精确等于 origin/production 的 production 工作树执行 `deploy/release.ps1 -DryRun` 和正式发布。每次事务前重新读取真实 ExpectedLive，服务器生产操作沿用 `/run/lock/marx-search-release.lock`。
5. 发布在候选阶段完成必需检查与审批后切流；核对线上版本、发布账本与 Git 一致。需要回退时仅执行 `deploy/rollback_release.ps1` 并提供精确当前、目标发布版本。

当前 C 盘低于 60 GiB 阈值；依赖继续复用，临时文件、缓存和发布产物只放 D 盘。尚未推送、创建 PR、触发 CI、推进 production 或修改线上服务。

## 首次发布门禁结果与修正

功能提交 `2f8b165` 已正常推送，pre-push 的 26 项发布测试及 deployment smoke 通过。PR 为 #34，首次 CI 运行 `42333607905`：浏览器通过；Python 3.10、3.11 的快速门禁各 459 通过、2 失败。main/production 尚未推进，线上未切流。

失败项目均在 `tests/test_research_citation_recovery.py`：

- `test_reported_numbered_original_is_recovered_without_changing_argument`
- `test_visually_checked_corpus_page_recovers_reported_original`

原因是近似匹配先把省略源内编号（1）（2）（3）的连续内容归为近似或待核验，跳过了既有逐字分句恢复。修正使仅省略源内列表编号的内容回到既有精确恢复流程；不是把删除编号后的拼接文本当作逐字原文。补丁已准备，未重跑失败测试。

原始日志：`D:/CodexData/outputs/ai-dialogue-polish-20261106/pr-job-128285434005.log`、`pr-job-128285434323.log`。再次推送会触发推送门禁及 PR 全部必需检查，因此须另获明确授权；不自行重触发失败流水线。

用户随后明确同意“上述最小复测及一次推送、PR门禁”。两项编号恢复场景已在本地一次复测通过（2 passed，0.32 秒），记录为 `numbered-recheck.json/log/xml`。仅修正近似匹配优先级，测试断言未放宽；后续推送和 PR 检查按此次新增授权各触发一次。
