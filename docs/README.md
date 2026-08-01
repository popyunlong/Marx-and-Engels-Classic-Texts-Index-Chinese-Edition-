# docs/ — 项目文档归档

2026-08-02 整理：原先散落在仓库根目录的历史文档统一归入本目录。
**文件名全部保持原样**（代码注释、交接文档、记忆索引均按文件名引用，改名会断引用）。

## 目录约定

| 子目录 | 内容 | 说明 |
| --- | --- | --- |
| `handoffs/` | `HANDOFF_*.md` | 功能上线后的交接/复盘文档，含部署步骤与坑 |
| `plans/` | `PLAN_*.md` | 功能动工前的方案文档（多数已实施，见对应 HANDOFF） |
| `incidents/` | `INCIDENT_*.md` | 线上事故复盘（502 恢复、阅读器 JS 回归等） |
| `ops/` | 安全加固计划、Cloudflare 切换/防爬 | **仍在生效的运维参考**，含回滚步骤 |
| `previews/` | `PREVIEW_*.html` | 改版前的本地静态预览稿（历史存档） |
| `sessions/` | `SESSION_SUMMARY_*.md` | 工作会话总结 |

## 仍留在根目录的文档（勿移动）

- `DEPLOY_SERVER.md`、`RUNBOOK.md` — 在 `deploy/cloud_patch_files.txt` 部署清单内，会同步到服务器，路径不能变。
- `README.md`、`CHANGELOG.md`、`LICENSE`、`PACKAGING.md` — 仓库根目录惯例位置。

## 新增文档的归档规则

新的 HANDOFF/PLAN/INCIDENT/总结文档直接写入对应子目录，不要再放仓库根目录。
文档间互相引用只写文件名（不带路径），便于日后再挪动。
