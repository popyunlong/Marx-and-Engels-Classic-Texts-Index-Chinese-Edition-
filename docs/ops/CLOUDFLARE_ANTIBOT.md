# Cloudflare 反爬配置（针对代理池扫库攻击）

> 触发事件：2026-06-22，约 440 个代理 IP 共用单一伪造 UA 扫 `/viewer`，把 8 线程源站打到队列 187、站点不可用。
> 按 IP 封无效（分布式 + 轮换）且历史上误伤过真实会员/站长 VPN——正解是 Cloudflare 边缘拦截。

## 攻击指纹（本次）
- **UA**：`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0`（699 次 / 438 IP）
- **路径**：`/viewer?file=...&page=...&q=...`（最贵，带搜索高亮渲染）、`/page-image?file=...&page=...`
- **特征**：匿名、**不带 cookie**（每个 IP 只打 7–10 次压在单 IP 限速线下；真实读者一定是登录态、带 `session` cookie）

## 在 Cloudflare 后台操作（域名 mazhuzuojiansuo.com）

### 1) ⚠️ 不要开 Bot Fight Mode（Free 版）
~~Security → Bots → Bot Fight Mode 打开~~ ——**撤回此建议**：Free 版 BFM **不受 WAF Custom Rules 的 Skip/Allow 豁免**，
会把 ZPay/支付宝的**服务器回调**（`/payments/*/notify`，无浏览器无 JS）当机器人挑战 → 支付静默失败、会员开不通
（与 CLOUDFLARE_CUTOVER.md 第 6 步的支付回调警告冲突）。
可以安全开启的替代项：Security → Bots → **Block AI bots / AI Scrapers and Crawlers**（只拦自报身份的 AI 采集爬虫
GPTBot 等，正是源站 UA 黑名单在硬扛的主力，不影响支付回调与普通访客）。
另：2026-07-05 起源站已做 Caddy 层锁定（见 CLOUDFLARE_CUTOVER.md 第 7 步），直连源站 IP 绕过 CF 的路已封死，
CF 边缘规则从此没有旁路。

### 2) WAF 自定义规则 A —— 直接拦掉本波 UA（精准、对登录用户零误伤）
Security → WAF → Custom rules → Create rule
- Name: `block-reader-bot-ua`
- Expression（点 "Edit expression" 粘贴）：
  ```
  (starts_with(http.request.uri.path, "/viewer") or starts_with(http.request.uri.path, "/page-image"))
  and (http.user_agent contains "Edg/149.0.0.0")
  ```
- Action: **Block**
- 说明：精准打"这个假 UA + 读者路径"。**切勿加 `not (http.cookie contains "session=")` 这类条件**——Flask 给匿名访客也发 `session` cookie，爬虫同样带着（已实测命中 100% 带 cookie），cookie 区分不了人机，加了规则永不命中、形同虚设。

### 3) WAF 自定义规则 B —— 防 UA 轮换：只挑战「匿名」读者流量（稳健主力）
- Name: `challenge-anon-reader`
- Expression：
  ```
  starts_with(http.request.uri.path, "/viewer")
  and not (http.cookie contains "mz_auth=")
  ```
- Action: **Managed Challenge**
- 说明：对**匿名**阅读器导航做 JS 质询。已登录会员的浏览器带着源站下发的 `mz_auth=1` 标记 cookie（见 app.py `_sync_cf_auth_marker_cookie`，仅登录态才有、不含鉴权能力），整条规则不命中 → **点检索结果进正文零质询、无等待**；匿名爬虫池没有该 cookie、又跑不了 JS → 被挡。规则顺序把 A 放在 B 之前。
- **为什么用 `mz_auth=` 而不是 `session=`**：Flask 给匿名访客也发 `session` cookie、爬虫同样带着（已实测 100% 带），`session=` 区分不了人机；`mz_auth=` 是源站**仅对登录用户**下发的独立标记，才能精准放行真人、只拦匿名。
- **绝不含 `/page-image`**：页面图像是 `<img>` 子请求，无法显示质询交互页；一旦质询，微信内置浏览器/部分国内网络/`cf_clearance` 过期/深链场景下书页图会整片裂开，表现为「多设备进不去 / 卡在验证」。挑战只放在顶层导航 `/viewer`。
- **不含 `/pdf`**：公网服务器上 `/pdf` 对匿名与普通登录用户已直接返回 404（整本 PDF 下载早已关闭，仅管理员/桌面可取），既不是攻击目标、也无需质询；且 `starts_with("/pdf")` 会顺带前缀匹配到 `/pdfs…`，故去掉。

### 4) 可选兜底：按 IP 限速（Rate Limiting Rules）
- 匹配：`starts_with(http.request.uri.path, "/page-image") or starts_with(http.request.uri.path, "/viewer")`
- 阈值：单 IP 每 1 分钟 > 30 次 → Managed Challenge / Block。
- 分布式攻击单 IP 量低、限速作用有限，仅作"某 IP 突然放量"的保险。

## 不要误伤（重要）
- 规则只限定 `/viewer`、`/page-image` 这两条真正被攻击的读者路径；**不要**碰 `/payments/*`（支付回调）、`/api/ai/*`（AI 是 SSE 长连接）、邮件/管理路径；`/pdf` 也不用管（服务器上已对普通用户 404）。
- 别用 "Block by Country/ASN" 一刀切，也别在源站手动 iptables 封 IP（历史教训：误伤真实会员和站长自己的 VPN）。

## 验证是否生效
- CF：Security → Events，应看到大量针对 `/viewer` 的 Block/Managed Challenge（来自那批代理 IP）。
- 源站：首页响应时间回到 ~0.05s、`journalctl -u marx-search` 不再刷 "queue depth"。
- 监控：`reader_access_events` 里匿名 IP 命中应骤降。

## 若 CF 边缘还压不住（攻击者携带 cookie 适配）
则上"源站登录闸"（代码改动，需另行部署）：让 `/page-image`、`/viewer` 对**未登录**直接返回轻量 302/403、不做昂贵渲染——这样即使绕过 CF，源站也不再被拖垮。属彻底的 belt-and-suspenders。
