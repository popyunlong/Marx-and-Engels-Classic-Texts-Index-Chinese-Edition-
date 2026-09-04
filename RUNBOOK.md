# 运维应急手册（RUNBOOK）

面向站长。命令可直接复制。服务器服务名 `marx-search`，应用目录 `/opt/marx-search`，数据目录 `/var/www/.marx_search_full`，密钥 env `/etc/marx-search.env`。

---

## 一、怎么判断"正在被攻击"

- 后台 `/admin` 总览的「**阅读异常**」卡片数量骤增。
- 收到主题为「**网站反爬自动封禁告警**」的邮件（已配 SMTP 时自动发）。
- 服务变慢/502：先看服务状态与日志（下）。

## 二、看日志（取证）

```bash
# 应用结构化审计：封禁/解封/会员/配置变更/登录失败
journalctl -u marx-search -n 200 | grep -E 'management_action|auth_failure'
# 最近错误
journalctl -u marx-search -n 100 --no-pager
# Caddy 边缘访问日志（配置了独立落盘后，含被应用 403/429 之前的全部流量）
tail -n 200 /var/log/caddy/access.log
```

## 三、手动封禁 / 解封 IP

- **后台操作**：`/admin` →「阅读异常」区 → 对某访客点封禁/恢复（路由 `/admin/reader-access-ban`）。
- **应急总开关**（改 `/etc/marx-search.env` 后 `systemctl restart marx-search`）：
  - `DISABLE_BOT_UA_BLOCK=1` 关闭 UA 黑名单（误伤正常用户时）。
  - `DISABLE_READER_AUTO_BAN=1` 关闭自动封禁。
  - `DISABLE_ADMIN_2FA=1` 关闭管理员二次验证（收不到验证码、被挡在后台外时）。

## 四、区分误报

- **监控/巡检**被当异常：把其账号/UA/IP 加进 env `MONITORING_EMAILS/USER_IDS/USER_AGENTS/IPS` 或后台设置 `monitoring_exemptions`。
- **校园/公司 NAT 共享出口**高频：自动封禁有「双高阈值」（日≥2000 且分钟峰≥90）+ 永不封登录会员，已尽量避免误伤；个别误封在后台解封即可。

## 五、阈值在哪调

- 自动封禁：后台设置 `reader_auto_ban`（`{"enabled":true,"daily_min":2000,"minute_min":90}`）或 env `READER_AUTO_BAN_DAILY_MIN/MINUTE_MIN`。
- 阅读器限速：env `READER_VIEW_IP_RATE` / `READER_PAGEIMG_IP_RATE`（`次数,窗口秒`）。

## 六、本批安全开关一览（env `/etc/marx-search.env`）

| 变量 | 作用 |
|---|---|
| `DISABLE_ADMIN_2FA=1` | 关闭管理员邮箱二次验证（应急） |
| `ADMIN_IP_ALLOWLIST=1.2.3.4,5.6.7.8` | 仅允许这些真实 IP 进后台（**不配则不启用**，配错会把自己挡外，先确认你的固定出口 IP） |
| `SECURITY_ALERT_EMAIL=you@example.com` | 安全告警收件人（不配则发到内置管理员邮箱） |
| `RCLONE_REMOTE=remote:bucket/marx-backups` | 备份异地同步目标（见 `deploy/BACKUP_README.md`） |

---

## 七、一次性应用「部署侧加固」（systemd 沙箱 / 日志保留 / Caddy 日志）

> 这些改动随补丁上传到了 `/opt/marx-search/deploy/`，但**不会**被补丁自动套用到 `/etc`（避免动主服务单元的风险）。确认你能 SSH 进服务器后，按下面**一次性**执行；每步都可回退。

**1) 主服务沙箱**（ProtectSystem=full 等）
```bash
cd /opt/marx-search
cp /etc/systemd/system/marx-search.service /root/marx-search.service.bak     # 先备份
sed 's|/opt/marx-search|/opt/marx-search|g' deploy/marx-search.service > /etc/systemd/system/marx-search.service
systemctl daemon-reload && systemctl restart marx-search
sleep 3 && curl -fsS http://127.0.0.1:8000/api/runtime >/dev/null && echo OK || \
  ( cp /root/marx-search.service.bak /etc/systemd/system/marx-search.service && systemctl daemon-reload && systemctl restart marx-search && echo "已回退" )
```

**2) journald 保留期**
```bash
mkdir -p /etc/systemd/journald.conf.d
cp /opt/marx-search/deploy/journald-marx-search.conf /etc/systemd/journald.conf.d/marx-search.conf
systemctl restart systemd-journald
```

**3) Caddy 独立访问日志**（按需，把 `/etc/caddy/Caddyfile` 的 `log` 改成 `deploy/Caddyfile.example` 里的 `log { output file ... }` 块）
```bash
mkdir -p /var/log/caddy
# 手动编辑 /etc/caddy/Caddyfile 的 log 块后：
caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
```

---

## 八、备份与恢复

见 `deploy/BACKUP_README.md`。要点：每天 04:00 自动备份；**强烈建议配 `RCLONE_REMOTE` 做异地**（否则备份与正本同机）；恢复用 `bash /opt/marx-search/deploy/restore.sh <备份目录>`。

## 九、服务起不来 / 回滚

```bash
systemctl status marx-search --no-pager
journalctl -u marx-search -n 80 --no-pager
# 补丁部署失败会自动从 /opt/marx-search.cloud-backup.<时间戳> 回滚；也可手动：
ls -1dt /opt/marx-search.cloud-backup.* | head
```

## 十、论文引文助手任务不动

任务长时间停在“解析中 / 排队中 / 匹配中 / 导出中”时，先检查独立工作进程：

```bash
systemctl status marx-search-citation-worker --no-pager
journalctl -u marx-search-citation-worker -n 100 --no-pager
systemctl restart marx-search-citation-worker
```

- 工作进程与网站必须读取同一个 `/etc/marx-search.env`、应用数据目录和语料索引。
- 不要把 `CITATION_ASSISTANT_INLINE_WORKER` 在线上改为 `1`；否则耗时任务会回到网页进程。
- “语料或模板版本不一致”是保护性拒绝，不应强行续跑；让用户重新创建任务。
- 删除或过期任务会同时清除原文、候选和导出物。排查日志时不得打印论文正文或完整注释。
- GB/T 7714—2025 未经后台正式标准与黄金样例确认前，会员入口保持关闭属于预期状态。
- 校对 PDF 必须来自“原 DOCX + 对应文字范围的 Word 批注”，再由独立 worker 调用
  `libreoffice --headless` 以页边批注方式转换；不得用逐条重排的列表式 PDF 冒充原文校对稿。
- PDF 导出异常时先运行 `command -v libreoffice`，再检查 worker 日志中的转换超时或批注不可见校验；
  LibreOffice 使用任务目录内的唯一配置目录，网页进程不得直接调用。
- V4 Pro 补漏影子默认关闭（`CITATION_ASSISTANT_AGENT_SHADOW=0`）。启用后也只运行于管理员任务，
  只保存 `citation_assistant_agent_shadow_runs` 中的汇总计数和响应哈希，不保存提示词、论文原文或模型回复，
  且不改变候选和导出结果。日志可用 `journalctl -u marx-search-citation-worker | grep 'citation Agent shadow'`
  观察；任何 Agent 超时或无效 JSON 都应降级为原确定性结果，而不是使任务失败。
- 开启影子会把每项任务最多 8 条未命中摘录发送到所配置的外部 AI 服务；只可用于已获授权的管理员测试文档，
  机密或未获授权的论文不得开启此开关。
- 修改匹配逻辑后先运行 `python scripts/citation_recall_benchmark.py`；黄金集未通过时不得扩大灰度。

## 8·15 MiMo 质量门禁与无停机切换

1. 首次发布始终保持 `MIMO_ADMIN_GRAY_ENABLED=0` 和 `MIMO_MIGRATION_ENABLED=0`。这一步只上线适配器、账本和界面，不改变任何普通用户的模型路由。
2. 轮换曾经暴露的 MiMo 密钥，新密钥只写入 `/etc/marx-search.env` 的 `MIMO_API_KEY`，不得写入仓库或数据库。
3. 从近 30 天生产请求生成至少 60 条已匿名 JSONL；必须排除个人上传文档、邮箱、姓名、IP、订单号等可识别信息。运行：

```bash
. /etc/marx-search.env
. /opt/marx-search/.venv/bin/activate
python /opt/marx-search/scripts/mimo_quality_gate.py run /secure/anonymous-samples.jsonl \
  --blind-output /secure/mimo-blind.jsonl \
  --key-output /secure/mimo-answer-key.jsonl
```

4. 盲评人员只接触 `mimo-blind.jsonl`。完成评分后运行 `summarize`；只有返回码为 0 且报告 `passed=true` 时才能继续。
5. 先设置 `MIMO_ADMIN_GRAY_ENABLED=1`进行管理员灰度。观察错误率、P95 延迟、缓存率、推理 token 占比和引文准确性；不达标就保持正式开关关闭。
6. 门禁和灰度都通过后，再把 `MIMO_MIGRATION_ENABLED=1` 放入候选版本。`deploy/update_cloud.ps1` 会在隔离发布目录和 8001 端口启动候选进程；编译、数据库快照/迁移、冒烟或健康检查任一失败，都不会切换 Caddy 流量。
7. Caddy 只在候选进程已经能对外服务后才平滑重载。切换后会继续监测并保留旧进程排空窗口；候选异常时立即把流量指回旧进程。
8. 紧急回退只需将 `MIMO_MIGRATION_ENABLED=0`，再走同一零停机发布流程；不删除账本、价格版本或历史调用记录。首页马克思形象的 MiMo 异常会回退本地台词，不会自动转用昂贵模型。

## 黑格尔著作集（16 册）建库与发布

扫描输入以 `config/hegel_volumes.yaml` 为唯一清单，共 16 个 PDF、7296 页。MiMo 密钥只通过当前进程的 `MIMO_API_KEY` 注入；不写入仓库、JSONL、日志或服务器环境。

```powershell
$env:MIMO_API_KEY = '<ephemeral-key>'
python scripts/ocr_hegel_mimo.py --all --workers 8
Remove-Item Env:MIMO_API_KEY
python scripts/ocr_hegel_mimo.py --all --audit-only
python scripts/build_scan_volumes.py --only hegel-theology-early hegel-phenomenology-upper hegel-phenomenology-lower hegel-logic-upper hegel-logic-lower hegel-shorter-logic hegel-nature hegel-right hegel-aesthetics-1 hegel-aesthetics-2 hegel-aesthetics-3-upper hegel-aesthetics-3-lower hegel-history-1 hegel-history-2 hegel-history-3 hegel-history-4
python scripts/build_hegel_toc.py
```

扫描脚本是可恢复的：已通过质量门禁的页会跳过，只重试缺页和被拒绝页。发布顺序为 PDF、构建产物、代码、语料库：

```powershell
powershell -File deploy/upload_book_pdfs.ps1 -Folder '黑格尔全集' -ExpectedCount 16
powershell -File deploy/upload_hegel_data.ps1
powershell -File deploy/update_cloud.ps1 -AllowDirty
powershell -File deploy/upload_corpus_db.ps1
```

服务器上 `/opt/marx-search/pdfs/黑格尔全集` 和 `/opt/marx-search/data` 必须位于数据盘；发布后核对 16 个 PDF、16 个 JSONL、目录审计全通过，再验收引文检索、目录跳转、页图阅读器与 AI 导读的页面上下文。
