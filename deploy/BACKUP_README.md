# 数据备份与恢复说明

面向站长。目标：勒索/误删/磁盘故障/VPS 失联时，能快速恢复会员、订单、审计、反馈与密钥。

## 一、备份什么、怎么备

由 `deploy/backup.sh` 完成，`marx-search-backup.timer` **每天 04:00（北京时间）**自动跑一次：

| 数据 | 方式 |
|---|---|
| 会员库 `membership.sqlite3`（成员/套餐/订单/会话/ai_usage/reader_access_events） | `sqlite3 .backup` 在线一致快照 |
| 反馈库 `feedback.sqlite3` | `sqlite3 .backup` |
| `/etc/marx-search.env`（支付/邮件/AI 密钥）、`session_secret.txt`、`ai.override.yaml`、反馈图片 `feedback_images/` | tar 打包 `files.tar.gz` |

备份落在 `/var/backups/marx-search/<时间戳>/`，本机保留最近 **14** 份（可用 env `BACKUP_KEEP` 调整）。

> 语料库 `corpus.sqlite`（384MB）**不**进每日备份：它可由 PDF + `build_index.py` 重建（见下「PDF 母本冷备」）。

## 二、异地备份（强烈建议，防一锅端）

本机备份与生产同盘，**勒索/删库/磁盘坏了会和正本一起没**。配置异地对象存储即可闭环：

1. 在服务器装 rclone：`curl https://rclone.org/install.sh | sudo bash`
2. `rclone config` 配一个对象存储 remote（阿里云 OSS / 腾讯云 COS / Backblaze B2 等），建议**开版本控制 + 30 天生命周期**防勒索。
3. 在 `/etc/marx-search.env` 加一行：`RCLONE_REMOTE=你的remote名:bucket/marx-backups`
4. 下次备份就会自动把当天目录同步上去（`backup.sh` 第 4 步）。

> 密钥体积极小，务必纳入异地包——`session_secret.txt` 丢了所有人被强制登出，`/etc/marx-search.env` 丢了支付/邮件/AI 全断。

## 三、手动备份 / 验证

```bash
sudo systemctl start marx-search-backup.service     # 立即跑一次
journalctl -u marx-search-backup.service -n 30       # 看日志
ls -1dt /var/backups/marx-search/*/ | head           # 看备份列表
systemctl list-timers marx-search-backup.timer       # 确认定时器在跑
```

## 四、恢复（出事时）

```bash
sudo bash /opt/marx-search/deploy/restore.sh /var/backups/marx-search/<时间戳>
```

脚本会：停服 → 还原两个库（chown www-data + chmod 600）→ 解包密钥/配置/图片 → 启服 → 自检 `/api/runtime`。完成后**手动验证 `/admin` 登录与阅读器**。

异地恢复：先 `rclone copy 你的remote:bucket/marx-backups/<时间戳> /var/backups/marx-search/<时间戳>`，再跑上面的 `restore.sh`。

## 五、PDF 母本冷备（不可再生，单独做一次）

真正不可重建的是**原始扫描件 `pdfs/`** 与 `config/` 下的 toc/manifest。`corpus.sqlite` 可由它们重建（链路：`build_index.py` → `scripts/build_wenji_toc.py` → `build_quanji_toc.py` → `build_toc.py --book '列宁全集'`）。

建议：PDF 母本在**站长本地电脑 + 一处云盘**各留一份一次性冷备（体积大、几乎不变，不必每日）。列宁卷等增量若 PDF 丢失则无法重建。

## 六、上线后做一次恢复演练

在测试目录还原一份昨日备份并启动验证，确认流程真的能跑通，别等出事才发现备份不可用。
