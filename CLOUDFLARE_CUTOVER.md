# 把域名挂到 Cloudflare 做中转 —— 安全切换手册

针对本站（`mazhuzuojiansuo.com`，现有链路：客户端 → Caddy(自动 HTTPS) → waitress:8000 → Flask）。
目标：套上 Cloudflare **橙云代理**（CDN 缓存 + 免费 WAF + L3/4/L7 DDoS 吸收 + 隐藏源站 IP），
**不改域名、不影响现有用户访问与搜索收录**。

> 假设：免费版 Cloudflare、橙云代理（"中转"）、apex 域名 `mazhuzuojiansuo.com`。
> 若只想用 CF 托管 DNS、不代理（灰云），那是本手册的零风险子集——只做第 2~3 步、全程灰云即可。

---

## 一、先回答你的疑问：会不会改域名 / 让用户搜不进来？

**不会。** Cloudflare 做中转的机制是「你把域名的 *Nameserver* 指向 CF，CF 既当你的 DNS 又当反向代理」。

- 域名**一个字都不变**：用户照样访问 `mazhuzuojiansuo.com`，URL、书签、搜索引擎收录全部不变。
- 对搜索引擎是**透明**的（同域名同 URL，SEO 不受影响；CF 还能加速、利于收录）。
- 切换 Nameserver 有个 DNS 传播窗口（通常几分钟~几小时），但**期间网站不断**——新旧 DNS 指向同一个源站 IP，用户无感。
- 任何一步出问题，**把被代理的记录从橙云改回灰云（DNS only）即可秒级回退**到现在的直连状态。

**但**"对用户零影响"有前提：本站有一套**按真实 IP 的反爬/限速/封禁**，以及**支付回调、发信、证书续期**几处，
必须按下面的顺序配好，否则会踩坑。最关键的那个坑（真实 IP 塌缩）**代码侧已经修好**（见第二节）。

---

## 二、唯一的硬前置（代码侧已完成 ✅）

CF 代理后，直连 Caddy 的是 CF 边缘节点，Caddy 会把 *CF 边缘 IP* 写成 `X-Forwarded-For` 最右项。
若应用仍取 XFF 最右，**全体访客会塌缩成少数几个 CF IP**，限速/自动封禁/`_is_public_ip` 全部失效——
这和 2026-06-08 那次"全员 127.0.0.1、暂停一个 IP 等于封掉全站"是同一类事故。

**已修复**：`app.py` 的 `_client_ip()` 现在**优先读 `CF-Connecting-IP`**（CF 写入的真实访客 IP），
取不到才回退原来的 XFF 最右逻辑。因此它对「上 CF 前 / 后 / DNS 切换过渡期」三态都正确，
**可以现在就先行上线，零行为变化**（未上 CF 时该头不存在，等价于旧逻辑）。已加回归测试
`tests/test_security.py::...prefers_cf_connecting_ip...`。

> 这个头的"不可伪造"靠**第 6 步源站防火墙只放行 CF**。在那之前它和旧逻辑一样安全（攻击者本来就够不到源站后面的 waitress）。

**动作**：照常 `deploy/update_cloud.ps1` 把这次代码推上线（含 `app.py`），先稳定运行，再开始下面的切换。

---

## 三、切换顺序（每步都可独立验证、可回退）

### 1. 上线代码
`./deploy/update_cloud.ps1`（带上本次 `app.py`、`tests/`、`deploy/Caddyfile.example` 改动）。
验证 `/admin` 阅读异常里 IP 仍是真实公网 IP（此时还没上 CF，应与现在一致）。

### 2. 在 Cloudflare 添加站点，核对 DNS（**先全程灰云**）
- CF 控制台 Add a site → 输入 `mazhuzuojiansuo.com` → 选 Free。
- CF 自动扫描现有 DNS。**逐条核对扫全了**，尤其：
  - **邮件相关记录（MX、SPF/DKIM/DMARC 的 TXT、发信子域）必须保留且永远灰云（DNS only）。** 本站注册验证码、找回密码、期刊提醒都靠 SMTP 发信，邮件记录被代理或漏导会导致发信/收信异常。
  - A / AAAA（web）此刻**先全部设为灰云**（DNS only，灰色云朵）。这样 CF 只接管 DNS、暂不代理，行为与现在完全一致。

### 3. 改 Nameserver（域名注册商处）
- 把注册商的 Nameserver 改成 CF 给的两个。等 CF 显示 **Active**（几分钟~几小时）。
- **检查点**：此时全灰云 = CF 只是 DNS 托管，源站直连，网站行为和现在**一模一样**。先在这停一下、确认一切正常，再继续。

### 4. 装源站证书 + 设 Full (strict)
> 为什么：CF 终止边缘 TLS 后，Caddy 自动续 Let's Encrypt 会变脆（TLS-ALPN 被 CF 拦、HTTP-01 易被 CF 搅黄）。换 CF 源站证书（15 年），一劳永逸。
- CF 控制台 → SSL/TLS → Origin Server → Create Certificate → 下载 cert/key。
- 放到服务器 `/etc/caddy/cloudflare-origin.pem` / `.key`（`chown caddy`、`chmod 600`）。
- 编辑 `/etc/caddy/Caddyfile`：取消 `tls /etc/caddy/cloudflare-origin.pem ...` 那行注释（参考 `deploy/Caddyfile.example`），`sudo systemctl reload caddy`。
- CF 控制台 → SSL/TLS → Overview → 模式设 **Full (strict)**。（**切勿用 Flexible**：会和站点的 HTTPS 跳转/HSTS/Secure Cookie 打架，造成重定向死循环。）
- 此刻仍灰云，源站证书已就位、只待代理打开。

### 5. 打开橙云（开始真正中转）
- 把 web 的 A（及 www，如有）记录从灰云改 **橙云（Proxied）**，一条一条来。
- 立即按"第四节验证清单"过一遍。出任何问题：**把该记录改回灰云**即回退。

### 6. 配置 CF 规则（本站必做，否则踩坑）
- **SSL/TLS**：Full (strict)（第 4 步已设）；Edge Certificates → Always Use HTTPS：**On**。
- **缓存 Cache Rules**：默认 CF 不缓存 HTML/动态，但保险起见显式**绕过缓存**：路径匹配
  `/api/*`、`/page-image*`、`/reader*`、`/viewer*`、`/admin*`、`/account*`、`/payments*` → Bypass cache。
  `/static/*` 可放心缓存（已带 `?v=` 版本号，升级自动击穿）。
- **⚠️ 支付回调别被挑战拦了**：WAF / Bot Fight Mode 若全局开启，会把 ZPay 的**服务器回调**
  （`/payments/zpay/notify`、`/return`，无浏览器无 JS）当成机器人挑战 → **支付静默失败、会员开不通**。
  必须为 `/payments/*` 加一条 WAF **Skip / Allow** 规则（或该路径 Security Level 设 Essentially Off）。
- **关掉会改写响应的功能**：Speed → Optimization 里 **Rocket Loader 关**、Auto Minify 不动 JS，
  以免干扰 AI 的 **SSE 流式输出**（`text/event-stream`）。（AI 是流式，首字节快，**不受 CF 100 秒超时影响**。）
- **上传体积**：免费版 CF 单请求体上限 **100 MB**。普通用户无影响（全站 `MAX_CONTENT_LENGTH=4MB`）；
  仅**后台「发布文件上传」**（`MAX_RELEASE_UPLOAD_MB`，可达 ~200MB）若走被代理的域名会被 CF 413。
  解决：发布走 `update_cloud.ps1`/`scp`（本就是 SSH，不经 CF），或上传时临时把记录改灰云。语料/PDF 走独立 scp 脚本，不受影响。

### 7. （推荐、放最后、谨慎做）锁源站防火墙到 CF 段——隐藏源站 + 让 `CF-Connecting-IP` 真正不可伪造
> 在此之前 CF 已经给你 CDN/WAF/DDoS；这一步是"藏源站"，把 80/443 只放行 CF，杜绝有人摸到源站 IP 直连绕过 CF（并伪造 `CF-Connecting-IP`）。
- **务必先确认流量已走 CF**（第 5 步生效后观察一会儿），再收紧防火墙；否则 DNS 还缓存着灰云、直连源站的真实用户会被挡。
- `ufw`：放行 CF 官方 IPv4/IPv6 段到 80/443；**22 端口务必保留对你管理 IP 的放行**（别把自己锁外面），并准备好云厂商的 VNC/控制台兜底。
- CF 的 IP 段会变化，建议用脚本定期同步官方列表（`https://www.cloudflare.com/ips/`）。
- 这步不做也能用（CDN/WAF/DDoS 照常），代价仅是源站 IP 若泄露可被绕过——按需取舍。

---

## 四、验证清单（橙云打开后逐条过）

- [ ] 浏览器访问 `https://mazhuzuojiansuo.com` 正常，无证书告警、无重定向死循环。
- [ ] 注册 / 登录 / 会员中心正常；Turnstile 人机验证正常（CSP 早已放行 `challenges.cloudflare.com`）。
- [ ] 首页搜索、`/viewer`、PDF 页图与高亮正常。
- [ ] **`/admin` 阅读异常 / 最近事件里的 IP 是真实公网 IP**（不是 CF 边缘 IP、也不是 127.0.0.1）。← 第二节修复的核心验收点。
- [ ] **支付全链路**：`/pricing` 下单 → 收银台 → 支付 → `/admin#payments` 能看到 `notify` 事件、订单转 `paid`、会员生效。
- [ ] **发信正常**：触发一次注册验证码或后台"全站发信测试邮件"，确认 SMTP 未受影响。
- [ ] AI 随心问 / 联想检索：回答**流式逐字出现**（SSE 未被 CF 缓冲/截断）。
- [ ] 手机 + 桌面浏览器都正常 HTTPS。

---

## 五、回退（任何一步出问题）

- **最快**：CF 把被代理的 web 记录从橙云改回**灰云（DNS only）** → 秒级~随 DNS TTL 生效，流量重新直连源站，回到现在的状态。
- **彻底**：注册商把 Nameserver 改回原来的（生效较慢，几小时）。
- 证书：若回退后想恢复 Caddy 自动 HTTPS，注释掉 Caddyfile 里的 `tls` 行、`reload caddy` 即可。

---

## 六、本站专属"坑"速查表

| 点 | 风险 | 处置 |
|---|---|---|
| 真实 IP | 全员塌缩成 CF 边缘 IP → 反爬全瞎、误封全站 | **已修**：`_client_ip()` 优先 `CF-Connecting-IP`（+ 第 6/7 步防伪造） |
| 证书续期 | Caddy ACME 在 CF 后续期失败 → 几十天后 526 | 用 CF 源站证书 + Full(strict)（第 4 步） |
| 支付回调 | ZPay 服务器回调被 CF 挑战拦 → 会员开不通 | `/payments/*` 加 WAF Skip（第 6 步） |
| 邮件 | MX/SPF/DKIM 被代理或漏导 → 发信异常 | 邮件记录永远灰云、核对齐全（第 2 步） |
| 缓存 | 误缓存鉴权/动态内容 | 动态路径 Bypass、仅缓存 `/static/*`（第 6 步） |
| 上传 100MB | 后台大文件发布走 CF 被 413 | 发布走 SSH 脚本，不经 CF（第 6 步） |
| AI 流式 | Rocket Loader/缓冲干扰 SSE | 关 Rocket Loader；SSE 首字节快不触发 100s 超时（第 6 步） |
| 锁防火墙 | 收紧太早/把 22 关掉 → 自锁 | 确认流量走 CF 后再收紧、留 22、备控制台（第 7 步） |
