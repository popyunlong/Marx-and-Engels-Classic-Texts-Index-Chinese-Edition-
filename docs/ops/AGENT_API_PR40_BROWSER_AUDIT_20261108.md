# PR 40：第二轮 CI 的浏览器假成功审计

本轮明确授权为“同意钩子和 PR CI 各再执行一次”，标识 `authorized-pr40-fix-20261108`。已各执行一次，未重试两端桌面。远端 main/production 仍为 `4fa835e`；feature `657ff219dd9948afaa9cb3927ba515332e562dd7` 已推送，测试合并 revision 为 `605759d69e7fa6f654cad3bea06897372e056d05`。

## 实际结果

[CI 42467129589](https://github.com/popyunlong/Marx-and-Engels-Classic-Texts-Index-Chinese-Edition-/actions/runs/42467129589) attempt 1 的 GitHub 总体状态是 success，但不能据此认定验收通过。原始浏览器日志明确记录 88 项中 69 passed、19 failed；工作流的 `node ... | tee ...` 未启用 pipefail，导致测试退出码被日志程序的成功退出码覆盖。

有效通过证据：本机 pre-push 编译、26 项部署测试、server smoke；Linux Python 3.10/3.11 各 495 deterministic、24 catalogue、35 dictionary，编译和 shell 语法；API 24 项后端/主站集成，以及独立 Linux Python 3.11 MCP 依赖安装和真实 REST/MCP HTTP 联调。它们对应所记版本，不受浏览器假成功影响。全量 integration 和 production lineage 因 PR 事件跳过，未执行。

浏览器 19 项失败分为：17 项启动共享夹具时 `spawn(process.env.PYTHON, ...)` 收到 undefined；2 项断言模式选择器应窄于 230px，且与发送按钮并排，与最新主线 CSS 明确要求全宽模式选择器、检索与发送分组的布局相冲突。未修改产品 HTML/CSS/JS 来适应旧断言。

## 已准备的修复

- 浏览器 step 导出已安装的 Python 路径，启用 pipefail，统一使用 RUNNER_TEMP 日志并始终上传日志。
- 两个固定 Chrome 的夹具在 Linux 使用已安装的 Playwright Chromium，保留 Windows Chrome 默认和显式 BROWSER_CHANNEL。
- 旧布局用例保留原有 v2/legacy、1.75 倍比例、13 个视口、溢出/可点击/互不重叠/停止按钮检查；布局断言跟随当前全宽等宽模式选择器、独立检索和发送分组，移动端允许检索控件换行。
- 新增未覆盖的门禁保护场景：读取实际 workflow shell，只有 Node runner 替身，退出码 0/19 各调用一次，确认 tee 留存日志且真实退出码不会丢失。首次 2 passed，未执行真实浏览器场景。

这些修复的真实浏览器场景与新 CI 整轮尚未验证。用例名称随布局要求更新，但仍视为原来已执行场景，不能以改名获得重跑权限。修复版本不沿用旧浏览器或整轮 CI 的通过结论。

## 后续限制

成果、原始 CI 日志、GitHub 状态快照和有效结论审计均在 `D:/CodexData/outputs/agent-query-api-20261107/authorized-pr40-fix-20261108`。主台账保留 raw success 与 effective failed，旧失败不覆盖。

本轮授权已耗尽；再次推送触发钩子并更新 PR CI，仍需这两项各一次的专项同意。PR 保持草稿，禁止按假成功合并、推进 production 或发布。WorkBuddy/Harness 桌面受阻状态不变，本轮没有重试。
