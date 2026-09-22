# 云端部署说明

> **现网更新只允许使用不可变发布事务。** 本文的“首次安装”章节仅适用于全新空主机，禁止拿来覆盖当前生产站。日常发布与回滚以 [`docs/ops/PRODUCTION_RELEASE_RUNBOOK.md`](docs/ops/PRODUCTION_RELEASE_RUNBOOK.md) 为准。

云端网页版部署到香港或海外 Ubuntu 云主机，站点使用 HTTPS，由站内注册登录、管理员角色和会员权限控制访问。Caddy 只负责反向代理和 HTTPS，不再使用整站 Basic Auth，避免支付回调被拦截。

## 推荐规格

- 系统：`Ubuntu 24.04 LTS`
- 规格：`2 vCPU / 4 GB RAM / 100 GB SSD` 起步
- 对外端口：`22`、`80`、`443`
- 站点目录：`/opt/marx-search`

当前仓库的完整资料大致体量：

- `data/` 约 222 MB
- `pdfs/` 约 6.7 GB

因此首版优先选择普通云主机，不建议先上 serverless。

## 一次性准备

1. 购买云主机并拿到公网 IP。
2. 购买域名，并把子域名解析到该 IP，例如 `search.example.com`。
3. 准备第四方支付会员中心里的商户 `pid` 和商户密钥 `key`。
4. 确认域名已经可以通过 HTTPS 访问，后续 `PUBLIC_BASE_URL` 会用于生成支付回调地址。

## 方案 A：用仓库脚本快速上线

### 1. 在全新服务器检出受版本控制的源码

`upload_to_server.ps1` 已停用，避免把本地脏工作区直接覆盖到服务器。全新空主机应检出已审核提交，并单独恢复数据与 PDF：

```bash
sudo git clone --branch production --single-branch \
  git@github.com:popyunlong/Marx-and-Engels-Classic-Texts-Index-Chinese-Edition-.git \
  /opt/marx-search
```

首次初始化完成后，所有更新都必须改用不可变发布事务，不能再次覆盖 `/opt/marx-search`。

### 2. 在服务器上执行初始化

```bash
cd /opt/marx-search
sudo bash deploy/bootstrap_ubuntu.sh \
  --domain search.example.com \
  --zpay-pid 'your-pid' \
  --zpay-key 'your-key'
```

支付密钥不要提交到仓库。脚本会把 `ZPAY_PID`、`ZPAY_KEY` 和默认网关 `https://zpayz.cn/submit.php` 写入 `/etc/marx-search.env`。

如果希望脚本顺手配置 `ufw` 放行 `22/80/443`，额外加上：

```bash
--configure-ufw
```

如果以后要启用 AI，再补上：

```bash
--zai-api-key 'your-key'
```

这个脚本会完成：

- 安装 `python3`、`python3-venv`、`python3-pip`、`caddy`
- 创建并安装 `/opt/marx-search/.venv`
- 写入 `/etc/marx-search.env`
- 安装 systemd 服务
- 安装 `/etc/caddy/Caddyfile`
- 启动并设置 `marx-search` 与 `caddy` 开机自启

## 方案 B：手动部署

如果你们想完全手动走一遍，也可以按下面执行。

### 1. 安装依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip caddy
```

### 2. 上传目录

确保服务器 `/opt/marx-search` 下至少有：

```text
/opt/marx-search/
  app.py
  serve.py
  runtime_env.py
  alipay.py
  zpay.py
  membership.py
  site_content.py
  ai.py
  search.py
  build_index.py
  requirements.txt
  deploy/
  config/
  data/
  pdfs/
  static/
  templates/
```

### 3. 创建虚拟环境并安装依赖

```bash
sudo mkdir -p /opt/marx-search
sudo chown -R $USER:$USER /opt/marx-search
cd /opt/marx-search
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
sudo mkdir -p /opt/marx-search/logs
sudo chown -R www-data:www-data /opt/marx-search
```

### 4. 写环境变量

```bash
sudo cp deploy/marx-search.env.example /etc/marx-search.env
sudo nano /etc/marx-search.env
```

至少修改：

- `PUBLIC_BASE_URL`
- `ZPAY_PID`
- `ZPAY_KEY`
- `ZPAY_TYPE`，`alipay` 或 `wxpay`
- `ZAI_API_KEY`，如果要启用 AI
- `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY`，用于注册与高风险登录的人机验证；未配置时仍会启用频率限制
- `SECURITY_CONTACT`，用于 `/.well-known/security.txt`
- `MAX_RELEASE_UPLOAD_MB`，限制后台发布文件上传大小
- 论文引文助手默认使用独立单并发工作进程；保持 `CITATION_ASSISTANT_INLINE_WORKER=0`。如工作节点不与应用共享数据目录，另生成至少 32 位随机 `CITATION_ASSISTANT_WORKER_TOKEN`，并在两端使用同一值。

### 5. 安装 systemd 服务

```bash
sudo cp deploy/marx-search.service /etc/systemd/system/marx-search.service
sudo cp deploy/marx-search-citation-worker.service /etc/systemd/system/marx-search-citation-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now marx-search.service marx-search-citation-worker.service
```

### 6. 安装 Caddy

```bash
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

## 上线后验证

先在服务器本机验证：

```bash
curl http://127.0.0.1:8000/api/runtime
systemctl status marx-search.service --no-pager
systemctl status marx-search-citation-worker.service --no-pager
systemctl status caddy --no-pager
journalctl -u marx-search.service -n 50 --no-pager
journalctl -u marx-search-citation-worker.service -n 50 --no-pager
```

再从浏览器验证：

- 访问域名可进入首页，不应出现整站 Basic Auth 弹窗
- 注册、登录、会员中心正常
- 首页搜索正常
- `/viewer` 正常打开
- PDF 页图和高亮正常
- 手机和桌面浏览器都能正常访问 HTTPS
- `/pricing` 显示在线支付已启用；未配置密钥时会显示未启用原因
- `/admin#payments` 能看到最近订单、支付事件和订阅记录

## 第四方支付联调

第四方支付会员中心里配置：

- 异步通知：`https://你的域名/payments/zpay/notify`
- 同步返回：`https://你的域名/payments/zpay/return`
- 支付方式：`alipay` 或 `wxpay`

联调步骤：

1. 前台注册测试账号并登录。
2. 在 `/pricing` 购买月度会员，确认跳转到第四方支付收银台。
3. 支付成功后回到 `/payments/result`。
4. 在 `/admin#payments` 检查 `create_page_pay`、`return`、`notify` 事件。
5. 确认订单变为 `paid`，订阅记录新增，会员中心显示会员有效。

### 收银台二维码不显示 / 报错 ACQ.APPLY_PC_MERCHANT_CODE_ERROR

收银台显示类似 `{"code":"msg","msg":"错误信息：ACQ.APPLY_PC_MERCHANT_CODE_ERROR，渠道ID：xxxxx"}`，
说明这是**支付宝**返回的错误（经第四方渠道透传），并非本站渲染问题：第四方为该订单路由到的支付宝渠道
**未签约「当面付 / PC 扫码下单(precreate)」产品**，支付宝拒绝生成 PC 二维码。桌面浏览器默认走 PC 当面付，
因此一旦该产品未签约就会失败。

按下列任一方式处理（前两种无需联系第四方）：

1. **改用 wap 产品（推荐，最快）**：把 `config/zpay.yaml` 的 `device` 改为 `mobile`（或设环境变量 `ZPAY_DEVICE=mobile`），
   重启 `marx-search`。本站会在下单参数中下发 `device=mobile`，支付宝改用「手机网站支付」，在 PC 上同样渲染可扫二维码，且不依赖当面付签约。
2. **更换支付宝渠道**：在第四方支付后台把支付宝通道切到支持当面付/扫码的渠道，或清空/调整 `channel_id`。
3. **签约当面付**：在第四方/支付宝商户后台为该渠道签约「当面付」产品。

注意：`device` 留空或填 `pc` 时本站**不下发** device，由网关按浏览器 UA 自动判定（与历史行为一致，不影响手机端）。
只有显式填 `mobile`/`jump` 等非 `pc` 值时才会下发，从而切换支付产品。

## 论文引文助手上线顺序

1. 先保持全站功能开关 `citation_assistant` 关闭，确认 `marx-search-citation-worker` 正常运行。
2. 管理员使用 GB/T 7714—2015 或期刊格式完成隔离内测；确认校对 PDF 保留原 DOCX 版式、
   在对应文字范围显示页边批注，并检查任务目录可在七天后自动清除。生产节点需安装
   `libreoffice-core libreoffice-writer`，只允许独立单并发 worker 调用无界面转换。
   切换服务前必须以 `www-data` 执行
   `python scripts/citation_agent_preflight.py --require-pdf`，使用匿名最小 DOCX 真实校验转换、中文文字提取和临时目录清理。
3. 可在管理员内测阶段将 `CITATION_ASSISTANT_AGENT_SHADOW=1` 开启 V4 Pro 补漏影子。该模式固定使用
   `deepseek-v4-pro`，模型只提议短片段，本地语料必须再通过全文相似度和页码校验；影子数据不进入候选，
   只记录汇总指标。先比较增量召回与歧义率，确认稳定后再讨论是否进入人工复核结果。
4. 在后台“引用格式”中依据 GB/T 7714—2025 正式标准文本录入模板，用黄金样例核对后勾选确认。未确认前，会员入口和前台 2025 选项会保持关闭。
5. 用 Word 与 WPS 人工打开导出的 DOCX，确认脚注、尾注、编号和原有样式；再开放 DOCX 导出和小范围会员灰度。
6. 观察工作进程日志以及失败率、歧义率、平均耗时、内存峰值和导出结构校验失败数。

页面向游客和普通登录用户展示，但上传、分析、审核、导出与下载 API 必须在服务端强制校验有效会员。
PDF 转换失败或定位失败时保留 Word；“单独重试 PDF”只能读取已生成的最终 Word，不得重新分析或重复插注。

应用进程与工作进程必须挂载同一应用数据目录和同一只读语料索引；语料哈希或模板版本发生变化时，已有任务会拒绝继续处理，用户需要重新上传。

## 更新部署

现网禁止重新运行上传/初始化脚本覆盖应用目录。代码更新须先通过 `main` 门禁，再由唯一发布协调者把 `production` 快进到同一提交并推送。

先读取当前 `/api/runtime` 的 `app_release.id`，然后在 `production` 的干净独立工作树做不联网演练：

```powershell
pwsh -File deploy/release.ps1 -ExpectedLive '<exact-live-id>' -DryRun -KeepArtifact
```

批准后去掉 `-DryRun`。脚本会在连接服务器前拒绝脏工作树、非 `production` 分支、未推送提交和非 Python 3.10 构建环境；服务器会用全局锁与 `parent_release_id` 比较拒绝过期会话。

回滚必须显式给出当前版和目标版：

```powershell
pwsh -File deploy/rollback_release.ps1 -ExpectedCurrent '<current-id>' -TargetRelease '<target-id>'
```

## 安全建议

- `8000` 端口只绑定 `127.0.0.1`，不要放进安全组或防火墙放行列表。
- 站点依赖站内账号和管理员角色控制后台，首个管理员请上线后立即开通。
- 第四方支付商户密钥只放在服务器 `/etc/marx-search.env` 或服务器 `config/zpay.yaml` 中，不要提交到 git。
- 会员开通只以 `/payments/zpay/notify` 的异步通知为准；`/payments/zpay/return` 只做验签记录与页面跳转，不应在这里写订单状态。
- AI 默认建议先关闭，等主站稳定后再启用。

## 远程管理后台（/admin）

现在程序已经支持远程管理后台：

- 本地 `http://127.0.0.1/.../control` 仍然是“本地控制台”，只允许服务器本机或桌面端本机访问。
- 线上 `https://你的域名/admin` 是“远程管理后台”，必须先登录站内账号，并且该账号的 `role=admin`。
- 远程后台修改的是当前服务器实例上的运行时覆盖文件和会员数据库，不会回写你本地电脑里的配置文件。

### 首个管理员开通

1. 先在网站前台注册一个普通账号。
2. 登录服务器，在项目目录执行：

```bash
cd /opt/marx-search
. .venv/bin/activate
python scripts/grant_admin.py --email you@example.com
```

3. 成功后，用这个账号登录网站，访问 `/admin`。

如果脚本提示“未找到用户”，说明这个邮箱还没有完成站内注册。

### 远程后台能改什么

- AI 设置
- 站点文案
- 套餐管理
- 会员开通
- 用户角色与启用状态

这些改动都只作用于当前服务器：

- AI 设置写入服务器运行账号自己的运行时覆盖文件
- 站点文案写入服务器运行时文案覆盖文件
- 套餐、用户、会员、订单数据仍写入服务器上的 `membership.sqlite3`

### 部署后验证

建议上线后补做这一轮检查：

```bash
cd /opt/marx-search
. .venv/bin/activate
python scripts/grant_admin.py --email you@example.com
```

然后在浏览器里验证：

- 未登录访问 `/admin` 会跳到登录页
- 普通用户访问 `/admin` 返回 403
- 管理员登录后可以进入 `/admin`
- 在 `/admin` 保存 AI Key、套餐或文案后，刷新页面仍能看到刚才的值

## 期刊新文提醒

期刊提醒由两个独立 systemd timer 触发，不占用网站请求线程，分「采集」与「发送」两个阶段：

- **采集**：`marx-search-journal-alerts.timer` 每天 11:00 UTC（北京时间 19:00）运行 `worker --stage=collect`。仅在「明天是发送日」时开新批次、抓取「抓取时间范围」内各来源全部新文，并归档上一批未处理文章（避免堆积）；如开启自动化档位，会顺带自动批准/生成综述。
- **发送**：`marx-search-journal-send.timer` 每小时触发 `worker --stage=send`，仅在发送日且到达控制台配置的「发送时间」（北京时间）时，把已批准批次的文献综述群发给已确认订阅者；批次发出后即离开未发送集合，不会重复发送。

```bash
sudo systemctl status marx-search-journal-alerts.timer marx-search-journal-send.timer --no-pager
sudo systemctl start marx-search-journal-alerts.service   # 手动采集一次
sudo systemctl start marx-search-journal-send.service     # 手动发送一次（受发送日/发送时间限制）
sudo journalctl -u marx-search-journal-alerts.service -u marx-search-journal-send.service -n 80 --no-pager
```

发送频率、发送时间、抓取时间范围、五学科综述的自动化档位（自动批准文章 / 自动生成综述 / 自动发送 / 暂停自动化总闸）以及综述专用模型，均在后台 `/admin` 的「期刊订阅」区配置。综述由站点 DeepSeek 模型生成，按马克思主义基本原理、马克思主义发展史、马克思主义中国化研究、国外马克思主义研究、思想政治教育五个学科分类，区分经典/前沿问题，文末附 GB/T 7714-2015 引文，全覆盖本批文章。

上线后只需要在 `/etc/marx-search.env` 填写一套 SMTP 配置并重启服务；注册邮箱验证码、找回密码、期刊新文提醒会共用这一个发信邮箱：

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=notice@example.com
SMTP_PASSWORD=your-password
SMTP_FROM_EMAIL=notice@example.com
SMTP_FROM_NAME=马著作检索
JOURNAL_ALERT_BASE_URL=https://你的域名
```

获得“期刊提醒”权限的用户可在“会员中心 -> 期刊新文提醒”订阅邮箱；后台 `/admin` 的“期刊提醒”区块可以查看期刊源、订阅概览、检测记录、文章记录和邮件发送日志，也可以发送一封全站发信测试邮件，确认验证码、找回密码、期刊提醒三条链路共用的 SMTP 已可用。

## 发布演练与崩溃防护

代码更新通过 `deploy/release.ps1` 从 Git 提交生成完整不可变版本。`deploy/update_cloud.ps1` 仅为兼容入口，最终调用同一事务；旧增量覆盖、跳过重启和脏目录开关均已移除。

1. 本机验证分支、洁净度、远端同步状态、Python 3.10、源码编译和完整树哈希；失败时尚未连接服务器。
2. 每个压缩包、远端上传和候选目录均使用完整提交号、UTC 时间与随机标识，不共享临时文件名。
3. 服务器在 `/run/lock/marx-search-release.lock` 内比较当前版本与包内父版本；不一致即拒绝。
4. 候选在 8001 端口通过 `/api/runtime`、首页、定价、AI 与阅读页验证后，Caddy 才平滑切流。
5. 每次切流后先等待旧端口的已建立连接排空（默认最长 720 秒，可用 `MARX_DEPLOY_DRAIN_TIMEOUT_SECONDS` 覆盖），再重启或停止该实例；旧主站超时未排空时撤销本次发布，不中断正在进行的 AI 回答。
6. `current`/`previous` 原子切换；主进程验证失败时自动恢复直接前任。
7. `/api/runtime`、`current/release.json`、`DEPLOYED_SHA` 和发布账本共同记录同一代码版本。`data_version` 独立表示语料版本。

PDF、索引和运行数据库是显式共享数据，不进入源码版本目录。直接语料上传/交换脚本已冻结；语料只能走受验证的候选流程，并与代码发布共用同一把锁。

### 推荐的发布动作
- 先运行 `release.ps1 -ExpectedLive '<id>' -DryRun -KeepArtifact`：只构建和验证，不连服务器。
- 每个会话使用独立工作树与功能分支；任何会话都不得直接在共享 `main` 上开发。
- 推送会触发本地门禁，CI 再分别运行 PR 快速门禁、`main` 集成门禁和 `production` 版本谱系门禁。
- `git push` 只更新远端仓库，不会改动线上；实际发布仍需本机人工批准。

## 8·15 发布特别约束

- 先在本机执行 `deploy/release.ps1 -ExpectedLive '<id>' -DryRun -KeepArtifact`；脏目录没有任何绕过方式。
- MiMo 正式迁移开关默认为 0。未完成 60 条匿名生产样本盲评和管理员灰度前，不得将 `MIMO_MIGRATION_ENABLED` 改为 1。
- 实际发布先在隔离目录与 8001 端口验证候选版，再平滑切换 Caddy；迁移或健康检查失败时原 8000 进程继续服务。
- 密钥只由 `/etc/marx-search.env` 注入。聊天、工单或日志中暴露过的密钥必须先轮换。
