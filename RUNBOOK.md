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
