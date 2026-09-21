# 生产基线快照（2026-09-21）

本文件记录从当前健康生产站只读固化的源码基线。它用于结束“线上代码无法由单一 Git 提交复现”的状态，不包含密钥、用户数据库、PDF、日志、缓存或上传内容。

## 运行状态

- 采集时线上标记：`0364a30+search-28122d2`
- 服务主进程：`502216`，采集前后未变化
- 有效启动命令：`/home/data/marx-layout-releases/layout-20260913-v1/.venv/bin/python -m ingestion.runtime --with-worker --port 8000`
- Python：`3.10.12`
- 语料版本：`2026.06.09`
- `/api/runtime`：`ok=true`、`db_status=ok`、`layout_exact_ready=true`、嵌入式 ingestion worker 正常

## 完整性

- 生产文件清单共 423 项；采集前后清单 SHA-256 均为 `57AC83CFDD8A171AEF9DEEBB8A84EFBDC7913779D15DCD7F7FCE7243A6DB788D`。
- 导入工作树的每个文件均与生产端逐文件 SHA-256 一致。
- 完整生产哈希清单保存在 `docs/ops/production-baseline-20260921.sha256`。
- 依赖快照保存在 `deploy/locks/requirements-production-20260921.txt`。

## 边界与排除项

- 未采集任何 `/etc/marx-search.env`、真实支付/AI 配置、私钥或用户数据。
- `static/broadcast/` 的历史宣传 GIF、未被页面引用的 `static/hero/founders.jpg`、服务器备份文件、一次性下划线脚本、运行标记和服务器旧测试副本未导入活动源码；它们的哈希仍保留在审计清单或仓库外只读快照中。
- 仓库外恢复快照位于 `C:\Users\10108\.codex\prod-snapshots\marx-20260921-233430`，不得作为日常开发工作区。

## 无扰动说明

快照期间未重启服务、未切换 Caddy、未写入服务器。首次静态资源全量读取期间 `/v2/read` 曾单次达到 12 秒超时，传输随即停止；后续改为哈希对账和仅补取差异文件。最终五个核心入口均返回 200，服务 PID 和线上标记保持不变。
