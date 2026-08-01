# 网站安全加固计划

日期：2026-06-08
背景：近期遭爬虫/抓取入侵（复盘见 `HANDOFF_READER_ANTICRAWL_2026-06-08.md`）。本计划按用户提出的 5 个方向，结合对真实代码的逐维度审计 + 对抗式复核，区分「有则改之（已有但需改进）」与「无则加勉（完全缺失需新增）」，按优先级排序。

> 审计方式：6 个维度各一名审计员读真实代码取证，再各一名对抗式复核员核实「是不是真缺、建议在本架构能不能落地」。下文严重度均为复核后的最终判定。

---

## 一句话总评

**基础面相当扎实**：密码用 scrypt 强哈希、CSRF 全局强制、登录失败锁定 + Turnstile、Caddy 自动 HTTPS + 安全头六件套、爬虫事件后的应用层反爬（真实 IP 透传、双维限速、保守自动封禁、UA 黑名单）都已到位，**SQL 注入面在代码层基本闭合**（全库参数化，检索路径根本不进 SQL）。

**真正的缺口集中在 3 处**：
1. **数据备份几乎为零**（最高风险）——会员/订单/审计/反馈/密钥无任何定时备份、无异地备份。一旦勒索/误删/磁盘故障，用户数据不可恢复。
2. **管理员后台无二次因子**——`/admin` 仅凭单一会话口令即可全权操控，密码或 cookie 泄露即接管全站。
3. **无主动告警**——被攻击时只能登录后台才发现，没有任何邮件通知。

---

## 优先级总览（跨主题）

### P0 — 立即做（高风险 / 低成本，建议本周）
| # | 项 | 主题 | 类型 | 工作量 | 挂点 |
|---|---|---|---|---|---|
| P0-1 | 用户数据每日定时备份（sqlite `.backup` API） | 备份 | 无则加勉(critical) | 中 | 新增 `deploy/backup.sh` + systemd timer |
| P0-2 | 备份异地化（rclone → 加密对象存储） | 备份 | 无则加勉(critical) | 中 | `backup.sh` 末尾 + `BACKUP_README.md` |
| P0-3 | 管理员后台邮箱 OTP 二次验证 | 2FA | 无则加勉(critical) | 中 | `_require_admin()` app.py:1226 |
| P0-4 | 主动告警邮件（异常/自动封禁触发即通知） | 监控 | 无则加勉(high) | 中 | before_request 钩子 app.py:3816 附近 |
| P0-5 | 敏感数据库文件权限收紧至 600 | SQL/DB | 有则改之(high) | 小 | `bootstrap_ubuntu.sh` + `_connect()` os.chmod 兜底 |
| P0-6 | 全局请求体上限 `MAX_CONTENT_LENGTH` | WAF | 无则加勉(high) | 小 | app.py:304-314（release 路由需豁免） |
| P0-7 | `app.log` 日志轮转（一行改动） | 监控 | 有则改之(medium) | 小 | `runtime_env.py:180` |

### P1 — 近期做（中等风险 / 中等成本）
| # | 项 | 主题 | 类型 | 工作量 |
|---|---|---|---|---|
| P1-1 | Cloudflare 免费版前置（CDN+WAF+L7-DDoS+隐藏源站）⚠️须成对改 `_client_ip` | WAF | 无则加勉(high) | 中 |
| P1-2 | 恢复步骤文档 + 一次恢复演练 | 备份 | 无则加勉(high) | 中 |
| P1-3 | 管理员后台 IP 白名单（Caddy 层或 app 层） | 2FA | 无则加勉(high) | 小 |
| P1-4 | systemd 文件系统沙箱（ProtectSystem=strict 等） | SQL/DB | 有则改之(medium) | 小 |
| P1-5 | 会话绝对/滑动过期 `PERMANENT_SESSION_LIFETIME` | 2FA | 有则改之(medium) | 小 |
| P1-6 | 外部 uptime 监控（UptimeRobot 或本机 timer） | 监控 | 无则加勉(medium) | 小 |
| P1-7 | 全局每 IP 兜底限速（非阅读器端点） | WAF | 无则加勉(medium) | 小 |
| P1-8 | 面向站长的事件响应手册 `RUNBOOK.md` | 监控 | 无则加勉(medium) | 中 |
| P1-9 | SQL 注入回归测试 | SQL/DB | 无则加勉(medium) | 中 |
| P1-10 | 反馈图片 + PDF 母本纳入备份/冷备文档 | 备份 | 无则加勉(medium) | 小 |
| P1-11 | journald/Caddy 日志保留 + 独立取证落盘 | 监控 | 有则改之(medium) | 中 |
| P1-12 | `SESSION_COOKIE_SECURE` 误配的部署期冒烟断言 | HTTPS | 无则加勉(medium) | 小 |
| P1-13 | CSP `script-src` 去 `unsafe-inline`（改 per-request nonce） | HTTPS | 有则改之(medium) | 中 |

### P2 — 纵深防御 / 防退化（低风险 / 有空再做）
| # | 项 | 主题 | 类型 | 工作量 |
|---|---|---|---|---|
| P2-1 | 语料库只读连接 `mode=ro&immutable=1` | SQL/DB | 有则改之(low) | 小 |
| P2-2 | 禁止 SQL 字符串拼接的静态检查门禁 | SQL/DB | 无则加勉(low) | 中 |
| P2-3 | 登录会话固定防护（写 session 前 `session.clear()`） | 2FA | 有则改之(low) | 小 |
| P2-4 | 登录失败持久审计（结构化日志，事后取证） | 2FA/监控 | 有则改之(low) | 小 |
| P2-5 | 阅读器异常的人机验证兜底（温和挑战而非硬封） | WAF | 有则改之(low) | 中 |
| P2-6 | waitress slowloris 防护参数（channel_timeout 等） | WAF | 有则改之(low) | 小 |
| P2-7 | CSP/Permissions-Policy 去重（删 Caddy 那份，应用层为权威源） | HTTPS | 有则改之(low) | 小 |
| P2-8 | HSTS preload 首次上线渐进策略（文档提示） | HTTPS | 有则改之(low) | 小 |
| P2-9 | Turnstile 未配置静默放行 → 启动告警 + 确认线上已配 | 2FA | 有则改之(low) | 小 |
| P2-10 | 登录失败计数持久化（重启不清零） | 2FA | 有则改之(low) | 中 |

---

## 按用户 5 个主题分述

### 主题 1｜两步验证（2FA）与账户安全

**已做到位（无需改）**
- 密码 scrypt 强哈希（werkzeug `generate_password_hash`，每用户独立盐、常量时间比对）。app.py:44, 4111
- 登录失败按 IP+邮箱双维计数，≥5 触发 Turnstile，≥10 锁定 15 分钟；另有独立限速。app.py:2362-2400, 419-420
  - （复核更正：失败计数是单进程 8 线程**共享**字典，运行期生效；审计初判「多 worker 不共享」不成立。真实弱点只剩重启清零/未来多进程。）
- CSRF 全局强制所有状态变更 POST。app.py:3841-3847
- 注册邮箱验证码（码哈希入库、5 次上限、15 分钟过期）。app.py:4032-4128
- 密码重置 token 256 位随机、哈希入库、15 分钟过期、一次性消费。membership.py:647
- `/control` 仅桌面端本机可访问（服务器模式直接 403），与 `/admin` 是两条独立链路。app.py:572-581

**待改进缺口**
- **【critical｜无则加勉】管理员后台缺二次因子**：`/admin` 与所有 `/admin/*` 仅靠 `role=='admin'` 的单一会话口令鉴权，与普通会员共用同一登录、同一 cookie，无独立口令/无 IP 白名单/无重认证。密码泄露或 cookie 被窃即可全权操控全站。
  - **建议（方案 A，零新增依赖）**：复用现成的 `create_account_email_token` + `verify_account_email_code`（已支持 purpose、5 次上限、15 分钟过期、code_hash 入库），在 `_require_admin()`（app.py:1226）内追加：本会话未标记 `admin_2fa_verified_at` 或已超 8~12h，则发 6 位邮箱码并跳转一个输入小页，验证通过写入 session。`_require_admin()` 是 `/admin`(4533)、`/admin/*`、`admin_dashboard_export`(4542) 等的统一入口，挂点准确。
  - 方案 B（TOTP/pyotp，users 加 totp_secret 列）成本更高、对非专业站长不友好，不优先。
- **【high｜无则加勉】管理员后台无 IP 白名单**：建议优先反代层（零应用改动）——`deploy/Caddyfile.example` 对 `/admin*` 加 `@adminip remote_ip <家庭/办公出口IP>` 块、未匹配 `respond 404`（已有 `respond /control* 404` 同款模式照抄）。或应用层在 `_require_admin()` 读 env `ADMIN_IP_ALLOWLIST` 用 `_client_ip()` 比对（未配置则不启用以免自锁）。
- **【medium｜有则改之】会话永不过期**：app.py:304-314 未设 `PERMANENT_SESSION_LIFETIME`。建议加（如 14 天）+ `SESSION_REFRESH_EACH_REQUEST=True`，登录/注册成功处置 `session.permanent=True`，使被盗 cookie 不无限期有效。
- **【low｜有则改之】会话固定未防护**：登录/注册写 `session['user_id']` 前未轮换。Flask 签名 cookie 架构下实际可利用性低，写前 `session.clear()` 即可，列 low。
- **【low｜有则改之】登录失败计数内存态**：重启清零、未来多进程会绕过；可落 SQLite 或收紧 `login_email` 阈值兜底。
- **【low｜有则改之】Turnstile 未配置时静默放行**：app.py:2415-2417 未配置即 `return True`。建议服务器模式启动自检对「生产但 Turnstile 未配」记显著告警，并核对线上确已配 `TURNSTILE_*`。

### 主题 2a｜SSL/HTTPS、安全响应头与 Cookie

**已做到位**
- TLS 证书签发 + 续期全托管 Caddy，零手工。`deploy/Caddyfile.example`
- 安全头双层下发（Caddy header 块 + 应用层 after_request `setdefault` 兜底），即使直连 8000 绕过 Caddy 仍有头。app.py:3953-3981
  - （复核语义：正常路径 Caddy 是强制覆盖、应用层 setdefault 只在缺省时补，故线上实际生效的是 Caddy 那份。）
- HSTS（仅 https 部署下发）、X-Frame-Options、nosniff、Referrer-Policy、Permissions-Policy、CSP 结构合理（object-src 'none'、base-uri/form-action 收敛）。
- 会话 Cookie `HttpOnly` + `SameSite=Lax` + `Secure`（随 https 自动开）。全站只用默认 session cookie，三属性即覆盖所有敏感 cookie。app.py:311-313

**待改进缺口**
- **【medium｜无则加勉】`SESSION_COOKIE_SECURE` 误配会静默以非 Secure 下发**：app.py:313 完全依赖 `PUBLIC_BASE_URL` 以 `https://` 开头；若 .env 误填 `http://` 或漏配（`public_scheme` 回退 https 但 base_url 空 → Secure=False），制造「对外是 https 但 cookie 非 Secure」的隐蔽不一致，会话凭据可被中间人降级嗅探。建议在 `scripts/deployment_smoke.py` 加断言：server 模式下 `SESSION_COOKIE_SECURE is True` 且 `PUBLIC_BASE_URL` 以 https 开头。
- **【medium｜有则改之】CSP `script-src 'unsafe-inline'`**：模板有大量内联 `<script>`（如 index.html 用 `|tojson` 注入 csrf_token/运行期 state），直接去掉会破页。建议 per-request nonce（after_request 单点生成 `secrets.token_urlsafe(16)`，CSP 用 `'nonce-xxx'` 替 `'unsafe-inline'`，模板内联脚本统一加 `nonce` 属性）——比抽离所有内联 JS 省很多工。style-src 风险低可暂留。
- **【low｜有则改之】CSP/Permissions-Policy 两处重复易漂移**：app.py 与 Caddyfile 逐字各一份。建议删 Caddy 那份、应用层为唯一权威源（也是实现 per-request nonce 的前提）。
- **【low｜有则改之】HSTS preload + 两年 max-age 缺渐进上线提示**：preload + includeSubDomains 一旦被浏览器收录极难撤销。建议文档提示：首次上线先去 preload、用短 max-age 观察 1~2 周再升级。

### 主题 2b｜SQL 注入与数据库访问控制

**已做到位（好消息：注入面代码层基本闭合）**
- **全库无一处把用户输入用 f-string/`.format()`/`%` 拼进 SQL**，检索词/邮箱/留言正文/ID/LIMIT 一律走 `?` 占位符绑定。267 处 execute 逐个核过。feedback.py:204, membership.py:541
- **最高风险的检索路径根本不进 SQL**：启动期一次性把整张 pages 表（静态 SELECT，无用户输入）载入内存，用户检索词此后只在纯 Python（rapidfuzz）处理。search.py:293-305
- app.py 7164 行无任何直接 `execute`，DB 访问全下沉到模块封装函数，便于集中审计。
- 动态 IN 列表用 `','.join('?' ...)` 安全惯用法；ORDER BY 全硬编码、LIMIT 走 `?`+`int()`；f-string SQL 里的表名/列名全是硬编码字面量。
- 报错不泄露 SQL/堆栈（500 处理器返回中文通用提示、debug=False）。

**待改进缺口（重在最小权限，不在注入本身）**
- **【high｜有则改之】敏感库文件大概率世界可读**：`update_cloud.ps1` 用 `install -d -m 0755` 建 `APPDATA_DIR`，DB 文件随 www-data 默认 umask 落地为 644，全仓 `*.py` 无 `os.chmod/umask` 兜底。同机另一进程/账号被攻陷即可直读会员/订单/会话/审计库。
  - 建议两处兜底：(1) `bootstrap_ubuntu.sh` 建库后 `chmod 600`、目录 0700；(2) `_connect()` mkdir 后 `os.chmod(DB_PATH, 0o600)` 幂等兜底。注意 page_images 缓存目录需保留可写。
  - （复核更正：会员库世界可读的真实成因是目录 0755 + 无 chmod 兜底，**不是**审计初判的 `chmod a+r`——那只作用于公开 PDF/静态资源。）
- **【medium｜有则改之】systemd 缺文件系统沙箱**：`deploy/marx-search.service` 仅 `PrivateTmp`+`NoNewPrivileges`。建议三个单元都加 `ProtectSystem=strict`、`ProtectHome=true`、`ReadWritePaths=/var/www/.marx_search_full /opt/marx-search/logs`、`PrivateDevices=true`、`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`，把 RCE 后的横向破坏面收敛。
- **【medium｜无则加勉】缺 SQL 注入回归测试**：当前全靠开发者纪律。建议 `tests/test_security.py` 加 `TestSqlInjection`：对用户搜索（`' OR 1=1--`、`x'; DROP TABLE users;--`）、留言提交、退订 token 走真实接口断言不报错/不越权/表仍在。
- **【low｜有则改之】语料库未只读连接**：search.py:293 用读写句柄。改 `file:...?mode=ro&immutable=1`（语料静态，还省 -wal 开销）。因检索不进 SQL、无可利用写注入面，列 low、纯纵深。
- **【low｜无则加勉】无 SQL 拼接静态门禁**：在 `.githooks/pre-push` 或新增 `scripts/check_sql_concat.py`（仿 `check_inline_js.py`）禁止 execute 紧跟 SQL f-string/format/%，已知硬编码标识符行加 `# nosql-allow` 白名单。

### 主题 3｜数据备份与灾难恢复（最高风险）

**现状：几乎裸奔**——现有「备份」只是部署时对**代码/模板/配置**打的本地快照（明确排除 corpus 与用户数据），且与生产同机同盘。

**待改进缺口**
- **【critical｜无则加勉】无任何用户数据定时备份**：会员/套餐/订单/会话/审计（ai_usage、reader_access_events）/反馈全部写入 `membership.sqlite3` 与 `feedback.sqlite3`，无定时导出。
  - （复核更正：运行期用户库是**两个**，不是三个——`admin_store.py`/`journal_alerts.py` 都指向同一个 `membership.sqlite3`。）
  - 建议：每日 systemd timer（复用 journal timer 模板）跑 `deploy/backup.sh`，用 `sqlite3 src ".backup 'dst'"` 在线安全 API 导出两个库 + tar 打包 `session_secret.txt`、`ai.override.yaml`、`/etc/marx-search.env`、`feedback_images/`、站点文案覆盖文件；本地留 7~14 份。
- **【critical｜无则加勉】备份与生产同机同盘**：勒索/删库/磁盘故障/VPS 封号一锅端，且密钥不在 git 与代码快照范围。
  - 建议：`backup.sh` 末尾用 rclone（单二进制、配置一次）同步到加密对象存储 bucket（OSS/COS/B2），开版本控制+30 天生命周期防勒索。退路：scp/rsync 到另一台机或本地。密钥体积极小，务必纳入异地包。
- **【high｜有则改之】备份用 `cp -a` 而非在线 `.backup`**：并发写时可能拷到不一致快照。新备份脚本一律用 `.backup`/`VACUUM INTO`。（复核：当前**未开 WAL**，故「漏拷 -wal/-shm」暂不适用，但 `.backup` 仍是正解。）
- **【high｜无则加勉】无恢复文档与演练**：写面向站长的 `BACKUP_README.md`/`DEPLOY_SERVER.md` 新章节：两个库、密钥、env、反馈图片各自落点 + 停服→还原→`chown www-data`→重启→验证 `/admin` 登录的固定步骤 + `deploy/restore.sh`，上线后真做一次演练。
- **【medium｜无则加勉】反馈图片未纳入备份**：`feedback_images/` 存盘不入库，丢了无法重建。纳入每日 tar（可 rsync 增量异地）。page_images 是可重建缓存可不备。
- **【medium｜无则加勉】PDF 母本冷备未文档化**：真正不可再生母本是 `pdfs/` 扫描件 + config 下 toc/manifest（corpus.sqlite 可由 `build_index.py` 重建但耗时长，列宁卷增量若 PDF 丢失不可重建）。建议 PDF 母本本地+云盘各留一份一次性冷备并记录重建命令链。

### 主题 4｜WAF / 限速 / 反爬 / CDN / DDoS

**已做到位（爬虫事件后的应用层反爬很扎实）**
- 真实客户端 IP 透传防 XFF 伪造（ProxyFix x_for=1 + 取 XFF 最右项 + waitress 关闭清头）。app.py:296-303, 2235-2246
- 阅读器按真实 IP 滑动窗口限速（/viewer 200/60s、/page-image 1500/60s，env 可热调）。app.py:2328-2340
- 书页图像按会员/会话限速（NAT 友好，管理员/监控豁免）。
- 保守自动封禁：双高阈值（日≥2000 且分钟峰≥90）才封、仅封公网 IP、永不封会员/内网/监控、120s 节流、写 reader_bans+审计。app.py:1888-1964
- AI 爬虫 + 脚本客户端 UA 黑名单（GPTBot/ClaudeBot/curl/python-requests/空 UA），热可编辑 + 紧急开关。app.py:2052-2104
- robots.txt + X-Robots-Tag noindex；AI/认证类端点均有限速。

**待改进缺口**
- **【high｜无则加勉】源站前无 WAF/CDN/L7-DDoS 边缘层**：Caddy 本体不含 WAF、源站 IP 直接暴露。建议套 Cloudflare 免费版（橙云代理 → CDN 缓存 + 免费托管 WAF + L3/4+基础 L7 DDoS 吸收 + 隐藏源站 + Managed Challenge 顺带补「阅读器无 Turnstile 兜底」）。
  - ⚠️ **强耦合硬前置（必须成对做）**：CF 会把直连的 CF 边缘 IP 追加为 XFF 最右项；若不同步把 `_client_ip` 改为优先读 `CF-Connecting-IP` 并在 Caddy 配 `trusted_proxies`，现有「取 XFF 最右」会把**全部访客算成 CF 边缘 IP**，一次性废掉整套已做到位的 IP 维度反爬（限速/自动封禁/`_is_public_ip`）。这不是「开个橙云就好」。
- **【high｜无则加勉】无全局 `MAX_CONTENT_LENGTH`**：任意 POST 大请求体在 per-field 校验前被读入内存（内存放大面）。建议 `config.update` 加（普通请求 2~4MB）。
  - ⚠️ **落地硬约束**：一旦设全局上限，release 上传（200MB）会在 view 执行前被 Flask 直接 413，app.py:3596 的自查来不及跑；必须对该路由用 `request.max_content_length` 按请求覆盖（Flask 2.3+）或走独立端点豁免。
- **【medium｜无则加勉】无全局每 IP 兜底限速**：普通页面/搜索 GET 无统一限速。在 app.py:3804 reader 守卫之后加一个 before_request 全局每 IP 低频兜底（如 300/60s，复用 `_client_ip`+`_rate_limit_or_abort`，沿用现有豁免，static/page-image 跳过）。优先级低于 CF。
- **【low｜有则改之】限速计数内存态重启清零**：单进程 8 线程加锁正确、够用，攻击者无法触发服务端重启。建议文档化此局限即可（自动封禁名单已 set_setting 持久化），抗冲击交给 CF 边缘。
- **【low｜有则改之】阅读器异常无人机验证兜底**：Turnstile 仅注册/登录。可在 IP 接近阈值时对下一次 /viewer 返回轻量挑战页（比硬封更不误伤真人）；上 CF 后用 Managed Challenge 免改码。
- **【low｜有则改之】waitress 无 slowloris 防护**：serve 仅 `threads=8`，加 `channel_timeout`/`connection_limit`/`cleanup_interval`。
- **【low｜有则改之】Caddy 日志为裸 `log`**：改 JSON + 滚动落独立文件，记录被应用层 403/429 之前的全部边缘流量供取证。

### 主题 5｜持续监控、日志与事件响应

**已做到位**
- `reader_access_events` 取证字段齐全（真实 IP/UA/路径/账号/时间/翻页，长度截断防膨胀，30 天保留 + 每小时 prune）。membership.py:225-243
- 异常访客聚合（actor 优先级 user>ip>session，多维阈值），后台「阅读异常」卡片。membership.py:1359-1446
- ai_usage 审计（prompt 摘要/IP/source_ref + 后台明细弹窗）、management_action 结构化日志（封禁/会员/配置变更）。
- 健康检查端点 `/api/runtime`、部署期自检 + 失败自动回滚。
- 监控豁免 `_is_monitoring_request`（邮箱/user_id 不可伪造 + UA 当密钥 + IP）。
- 日志不泄露密钥/token（已核）。

**待改进缺口**
- **【high｜无则加勉】无主动告警**：异常/自动封禁/健康异常都只写日志或后台卡片，无任何邮件通知，管理员必须登录后台才发现攻击。SMTP 设施已存在（`send_email` app.py:2488，`_send_feedback_admin_notice` 是同模式先例）但未用于安全告警。
  - 建议：before_request 钩子（app.py:3816 已挂自动封禁）加同款节流的 `_alert_anomalies_if_due`，约每 10 分钟扫 `list_reader_anomaly_visitors`，新高危 actor 或自动封禁触发时发简报邮件，用 set_setting 记「已告警 actor_key+day」去重；最直接是在自动封禁落库处（app.py:1959）顺手发一封。SMTP 未配则静默跳过。
  - （复核降级 critical→high：极端扒站已有在线自动封禁处置，告警是「第一时间知情」而非「第一时间拦截」。）
- **【medium｜有则改之】`app.log` 无轮转**：`runtime_env.py:180` 裸 FileHandler 无限增长。换 `RotatingFileHandler(maxBytes=10MB, backupCount=5)` 一行搞定。（复核：审计同时有 StreamHandler→journald 双份，撑大不丢审计，故 medium 非 high。）
- **【medium｜无则加勉】无外部 uptime 监控**：宕机无人知。UptimeRobot 打 `/api/runtime`（监控 UA 加进 `MONITORING_USER_AGENTS` 当密钥豁免），或本机 systemd-timer 仿 journal-alerts.timer。
- **【medium｜有则改之】journald/Caddy 日志无显式保留与独立取证**：bootstrap 写 `journald.conf.d`（`SystemMaxUse=500M`、`MaxRetentionSec=30day`）+ Caddy log 落独立文件加 roll。（复核：journald 默认自封顶不会撑满磁盘，真实问题是保留期不确定 + 取证不便。）
- **【medium｜无则加勉】无站长事件响应手册**：写一页中文 `RUNBOOK.md`：怎么判断正在被攻击、怎么看日志（`journalctl -u marx-search | grep management_action`）、怎么手动封解 IP（`/admin/reader-access-ban` app.py:4788 + 应急 `DISABLE_BOT_UA_BLOCK`）、怎么区分误报、阈值在哪调（`reader_auto_ban`/`READER_AUTO_BAN_*`）。
- **【low｜有则改之】登录失败无持久审计**：`_record_login_failure`（app.py:2381）只写内存，重启即丢，无法回溯撞库。加一行结构化 `LOGGER.info('auth_failure', ...)` 配合 journald 30 天保留即可。

---

## 建议执行顺序（分批，便于单生产机安全部署）

> 注意运维约束：**单一共享工作树 + 单生产机与另一并行 AI 共用，勿并发部署**；`update_cloud.ps1` 从实时树按清单打包；`.ps1` 勿加全角中文注释（PS5.1 GBK 误读会破坏解析）。

- **第 1 批（纯代码、低风险、可走现有部署）**：P0-3 管理员 OTP、P0-6 MAX_CONTENT_LENGTH、P0-7 app.log 轮转、P1-5 会话过期、P0-5 的 `os.chmod` 代码兜底部分。配套加测试。
- **第 2 批（运维/部署侧）**：P0-1/P0-2 备份脚本+timer+rclone、P1-2 恢复文档+演练、P0-5 的 bootstrap chmod、P1-4 systemd 沙箱、P1-11 日志保留。需在生产机上 `daemon-reload`+验证。
- **第 3 批（边缘/外部）**：P1-1 Cloudflare（含 `_client_ip` 改造，需仔细回归 IP 反爬）、P1-6 uptime 监控、P1-3 管理员 IP 白名单。
- **第 4 批（告警与文档）**：P0-4 主动告警邮件、P1-8 RUNBOOK.md。
- **第 5 批（纵深/防退化）**：P2 全部。

每批部署后用 `/verify` 或冒烟脚本确认 `/admin` 登录、阅读器、支付链路正常。
