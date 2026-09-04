# 工作区与生产环境审计（2026-09-04）

## 审计基线

- Git 基线：`b03f0a84ba1123b90c7388691c5eee39bb3a49d4`
- 生产标记：`b03f0a84ba1123b90c7388691c5eee39bb3a49d4-dirty`
- 清理前保护分支：`backup/pre-cleanup-20260904`
- 生产同步分支：`codex/prod-sync-20260904`
- 生产语料 SHA-256：`e8571319f4cad403608a6f22e2f806617f59e8b4a491fcfb9b401861eeb77509`

清理前工作区有 75 个已跟踪修改和 483 个未跟踪文件。生产环境由带未提交修改的工作区部署，因而不能仅由原 Git 提交复现。本次同步只读取生产服务器，不上传代码、不重启服务、不修改数据库或环境变量。

## 功能状态

| 功能 | 生产状态 | 后续归属 |
| --- | --- | --- |
| 正式论文插注校注 | 已上线，Citation Worker 运行正常 | 生产基线 |
| Citation Agent 管理员测试版 | 未部署，公开验证门禁未通过 | `codex/citation-agent-admin-preview` |
| 搜索结果导出 | 已上线并启用 | 生产基线 |
| 全站引文文字修复 | 程序和 unit 部分存在，服务及定时器关闭 | `codex/corpus-repair-rollout` |
| 个人书库、期刊、MEGA²、哲学类增量 | 已上线 | 生产基线；构建工具另行归档 |
| 搜索限流、范围隔离和重试界面 | 本地版本领先生产 | `codex/search-scope-rate-limit` |
| 周恩来书目及《文集》两处文字校正 | 仅本地 | `codex/biblio-corrections-20260904` |
| Windows Word 转 PDF 回退 | 仅本地 | `codex/citation-windows-pdf` |
| MiMo | 生产实际启用，正式质量门禁记录不完整 | 不改变生产开关；另行补做质量门禁 |

## 生产运行状态

- `marx-search.service`：active/running，清理前 PID 757，重启次数 0。
- `marx-search-citation-worker.service`：active/running，清理前 PID 774，重启次数 0。
- `marx-search-ai-sync.service`：active/running；其服务器专用桥接脚本已补入生产基线。
- Corpus Repair 相关 timer 均为 disabled；本次清理不得启用。
- `/` 剩余约 21GB；语料数据盘剩余约 26GB，无紧急删档需要。
- `/opt/marx-search/data` 与 `/var/www/.marx_search_full/journal` 均位于独立数据盘 `/dev/vdb1`。
- 清理前 `/`、`/api/runtime`、`/citation-assistant` 均返回 HTTP 200。

## 同步和保护边界

- 生产候选中实际存在的 134 个运行文件已只读取回；复制后原始字节哈希全部一致。
- systemd unit、服务器辅助脚本和正在运行的 AI 同步桥接脚本以快照形式纳入 Git，未对服务器配置执行 `daemon-reload` 或重启。
- 生产数据库、真实环境文件、用户文件、私钥及 API 密钥不进入 Git。
- `config/mylib-node.crt` 是公开 CA 证书，可以跟踪；对应私钥不得进入仓库。
- 本地数据库仅作为外部制品管理，受控文件只记录生产语料哈希。

## 清理停止条件

服务器清理若出现健康接口异常、新 5xx、服务 PID 变化、Worker 心跳中断、权限错误或明显 I/O 影响，立即停止并恢复上一批操作。任何需要服务重启、版本切换或数据库写入的事项不属于本次在线清理。
