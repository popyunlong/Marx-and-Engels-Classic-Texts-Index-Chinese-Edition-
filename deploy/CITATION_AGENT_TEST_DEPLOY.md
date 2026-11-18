# 会员插注校注 Agent 运行手册

会员通过 `/citation-agent` 上传与审核任务。服务名和任务库沿用既有标识，`marx-search-citation-agent-test-worker` 负责在站内提取论文、核验书库和导出文件，`marx-citation-agent` 负责模型调用。旧版普通引文任务由 `marx-search-citation-worker` 处理。

## 发布及恢复

由发布协调者使用 `deploy/release.ps1` 发布，使用 `deploy/rollback_release.ps1` 回滚。两条路径均在发布锁内记录服务状态、等待本地 worker 完成当前工作，随后停止模型 worker。升级包下的独立模型目录仅含 `ai_models.py`、`citation_agent_queue.py` 和 `scripts/citation_agent_worker.py`。启用新版时，先启动模型服务并核对协议 2 的进程心跳，再启动本地服务并核对两个心跳。两个服务均随系统启动。

首个 v2 发布记录原有服务是否运行、是否配置开机启动，以及独立 v1 模型目录的实际路径。后续发布继承这份记录。回到旧版时，发布事务按记录恢复两个旧服务。旧版无法安全审核或导出仍在有效期内的 v2 任务。显式降级前检查任务数和未完成的 v2 模型请求，存在不兼容记录时保留现行版本并返回原因。失败发布需要恢复旧站且在切换期间收到 v2 任务时，恢复程序保留数据并暂停不兼容的 worker，协调者可通过发布兼容版本恢复任务。

## 环境和数据

- `/etc/marx-citation-agent-bridge.env` 与 `/etc/marx-citation-agent.env` 沿用生产的 `admin_live` 开关和既有模型配置。仓库中的示例配置供新环境参考。模型凭据只在联网 worker 的环境文件中配置。
- 联网 worker 仅可访问隔离队列和配置的回环 CONNECT 代理。允许的 HTTPS 域名应与 `CITATION_AGENT_API_URL` 及代理过滤器一致。生产当前使用 MiMo，已有配置在发布中保留。
- 任务、论文、导出文件和证据检查点位于应用数据目录。队列默认位于 `/var/lib/marx-citation-agent/queue/queue.sqlite3`。联网请求使用脱敏片段、公开候选短摘录和不透明来源编号，原始文档及个人书库内容保存在本地。
- 服务就绪标识位于 `/run/<服务名>/ready-v2.json`。事务核验协议号、当前主进程 PID 和心跳时间，未通过会进入发布恢复流程。任务详情另行显示最近进展、降级信息及剩余条数。

## 验收和诊断

先查本次测试台账，运行已规划且尚未执行的验证。记录版本、场景、结论和产物位置，重复测试须取得本次复测授权。验证应涵盖会员网页、范围绑定、近似证据、实际导出及 worker 中断恢复。生产诊断优先查看服务状态、心跳、队列计数与错误码，排查报告不包含论文内容或模型凭据。
