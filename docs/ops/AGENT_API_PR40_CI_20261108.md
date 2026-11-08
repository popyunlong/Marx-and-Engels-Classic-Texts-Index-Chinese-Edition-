# PR 40：专项授权验证结果

用户明确同意“同意三项各执行一次”。授权标识为 `authorized-three-stage-20261108`，推送钩子、PR CI、WorkBuddy 和 Harness 桌面准备/验收各一次均已消耗，未隐式重跑。

## 已完成

- Feature head `ec26b877a9b80c220bde37c2419a0c015d2e5796` 已推送。
- pre-push：441 个 tracked Python 文件编译、26 项部署清单/元数据测试、server deployment smoke 全部通过，无钩子绕过。
- 草稿 PR：[40](https://github.com/popyunlong/Marx-and-Engels-Classic-Texts-Index-Chinese-Edition-/pull/40)，base main；未合并或发布。
- CI 第一次运行：[42466115525](https://github.com/popyunlong/Marx-and-Engels-Classic-Texts-Index-Chinese-Edition-/actions/runs/42466115525)，attempt 1，测试合并 revision `5fefe59240b354d43fcbfffa41cfb13bb884b2a9`，结果 failure。

## 失败与修复

Fast gate 的 Python 3.10 和 3.11 各为 488 passed、5 failed。失败集中在 `tests/test_catalog_traffic.py` 的 promote 场景：夹具抽取 `switch_caddy` 后未提供新增的 `agent_api_switch_route` 依赖，导致提前退出，尚未进入 Caddy 断言目标。已给纯路由夹具补充默认空钩子，保持隔离服务边界；真实发布脚本仍强制执行 MCP 准备，不增设运行时跳过条件。

另补充此前未覆盖的场景：MCP 路由准备回调失败时，promote/rollback 均须保留原主站路由、不校验/安装/重载 Caddy。首次精确运行这个新场景，2 passed。它不能替代上述 5 个旧场景的复测；夹具修复后的旧场景和整轮 CI 仍标记未验证。

API 与浏览器 job 均在 `actions/setup-python@v5` 下载 Python 3.10.22 时遇到 GitHub HTTP 504，未进入应用测试。没有为网络失败修改依赖或关闭校验，也没有 rerun workflow。Main integration 与 production lineage 在 PR 事件中按配置跳过。

## 桌面状态及清理

WorkBuddy 返回目标控件树，但截图先后显示其他 Chrome/Codex 窗口；Raise 后仍不匹配，因此未继续键盘或坐标输入。Harness 首次画面匹配，随后窗口状态变为最小化；重新绑定后插件点击报 geometry unavailable，再次截图又与控件树不符，未发送 Agent 提示词。

两端均为 blocked_during_preparation，不是通过；各合成服务 `api_calls=0`，有效测试 Key 数为 0，明文 fixture 文件已删除、服务已停止。WorkBuddy mcp.json 已按备份逐字节恢复；Harness 用户配置未修改。没有删除用户聊天或文档。

## 产物及下一步

脱敏日志、状态、两端清理摘要及新场景 XML 位于 `D:/CodexData/outputs/agent-query-api-20261107/authorized-three-stage-20261108`；权威台账为 `AGENT_API_TEST_LEDGER_20261107.json`。

当前修复仅在本地提交。继续推送会再次执行相同 pre-push，更新 PR 会触发第二轮 CI；必须先取得这两项各一次的专项授权。无需再次复测已受阻的桌面场景，直到窗口控制问题得到解决并另获授权。PR 维持草稿，production 未推进或部署。
