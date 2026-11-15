# Plus API/MCP 发布记录

用户授权：「推送上线，一次性授权所有测试」。本轮由本会话协调发布。基线 d231ec2879e790c951c24b934e3b1b23ce5570ab。原本地测试及专项复测记录保留在 PLUS_API_TEST_LEDGER_20261115.json。

| 阶段 | 授权执行范围与次数 |
| --- | --- |
| 功能分支推送 | pre-push 各步骤一轮，含 Python 编译、部署清单、版本元数据与启动 smoke |
| PR CI | 精确候选 HEAD 的 CI 一轮，含 fast Python 3.10／3.11 各一、浏览器一、API 后端和 MCP 协议及恢复各一 |
| main CI | 合并修订一轮，含 PR 共用门禁及全量集成、启动 smoke、发布包一致性 |
| production | 从通过的 main 快进。推送 hook 一轮，含全量测试。production CI 一轮，含 main 共用门禁及生产谱系、元数据和发布打包检查 |
| 发布 | 正式脚本演练一轮、事务一轮、候选检查和线上验收各一轮 |

每一阶段保留失败或中断证据。此处的授权不将失败后的重跑计为首次执行。产物目录 D:/CodexData/outputs/plus-api-20261115，持续记录提交、CI、候选和线上版本。
