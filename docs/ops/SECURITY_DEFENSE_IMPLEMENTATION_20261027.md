# 网站防御实施与发布交接（2026-10-27）

本轮在 `codex/security-defense-20261027` 实施，工作树为 `D:/CodexData/workspaces/security-defense-20261027`。**代码已实现，尚未发布；本轮未修改生产配置、封禁数据库、Cloudflare 规则或运行进程。** 不将新代码计为线上防护能力。

分阶段提交：`3cbfff0` 应用防护及检索可用性；`3e1449f` 发布事务、运维与取证；`ccefc73` 依赖及 OCR 解码收紧。经用户明确授权，固定 pre-push 门禁执行一次并通过，main 与专用分支已推送到 `b106574`。随后 main CI 失败，production 和线上均未推进。后续修正尚未提交或推送。

## 基线与证据

实施起点：远端 main、production 与线上发布均为 `cb945b84cae54b98d3b4de9357f96a5d80e32f14`。线上发布号 `cb945b84cae54b98d3b4de9357f96a5d80e32f14-20261018T165407Z-a988088d`，进程工作目录位于该不可变发布的 app 子目录。原共享主目录落后；实施前已 fetch，在 D 盘创建独立分支和工作树。门禁失败后的只读复核确认 main 为 `b106574`，production/线上仍为原基线，线上 PID 2209394 为 active。

应用、会员、管理存储、检索、运行入口、看门狗、两份 Cloudflare 同步脚本、源站锁定样例、备份脚本共 10 个线上源文件与最新主线 SHA256 一致。完整清单见 [baseline.json](security-defense-20261027/baseline.json)。

实际 Caddy 配置已保存为 [Caddyfile.baseline](security-defense-20261027/Caddyfile.baseline)，SHA256 为 `a4b3b0ce345749c972f52a6fce943d963f25a7df9092e5c73879c0c10cb93ef5`。保留了 8011 快速 AI 侧车、8000 两类 SSE 路由、不压缩 SSE、安全响应头与支付表单域名。线上安装的 CF helper/snippet 已去掉 UA 豁免，仓库旧副本却仍包含豁免；本轮修正仓库漂移，不拿旧样例覆盖线上。

## 已实现的变更

| 问题 | 本轮实现 | 线上状态 |
|---|---|---|
| 新爬虫与封面漏拦 | `meta-webindexer` 名称匹配；统一端点清单；封禁早于读取和渲染 | 当前仍存在，待发布 |
| 版本化文库和期刊插图漏拦 | 补入 `wenku_raw_versioned`、`liushi_raw_versioned`、期刊插图及个人阅读器 | 当前仍存在，待发布 |
| 敏感路径扫描 | 应用及 Caddy 按完整路径分段匹配 `.env`、`.git`、三个服务账号 JSON；不匹配普通文件名或查询参数，不影响 ACME | 应用/Caddy 待发布；Cloudflare 待核验 |
| 无期限自动封禁 | 新极端高频 7 天；新代理池 24 小时；保留人工及历史无 expires_at 记录 | 605 条历史封禁未动 |
| 同 UA 群体误伤 | 保留旧阈值为观察信号；新增池封禁须独立行为证据且进入 enforce；按账号/签名访客会话分别计额 | 新规则未上线 |
| 跨资源批量遍历 | 有界会话/IP 状态；跨端点、卷册数量与轮换 IP 组合识别；限速→验证→短时封禁 | 默认 observe，未上线 |
| 正则卡死 | 单词、多词、模糊上下文与导出共用确定性偏移匹配；多词窗口改为滑动计数 | 当前仍存在，待发布 |
| 检索并发遗漏 | 全局检索预算覆盖主要 Corpus 入口，嵌套复用额度；普通 API 503，已启动 SSE 用 error 事件 | 待发布 |
| 健康探测受发布锁抑制 | 先持续探测；真正重启前取锁、复核 PID/发布路径/健康 | 待发布 |
| CF 同步漂移 | 单一维护源码；兼容 launcher；下载/CIDR 校验、候选配置验证、reload 失败还原 | 已安装旧辅助脚本本身无 UA 豁免；新维护流程待发布 |
| 审计放大数据库压力 | 拒绝流量不写原始阅读审计；后台周期扫描；独立有界安全队列及聚合存储 | 原单写队列已在线；新增部分待发布 |
| 运维告警和存储 | 扫描/代理 5xx/备份失败/看门狗失效聚合告警；访问日志 gzip 轮转；禁止按固定份数删除备份 | 新增部分待发布 |
| 数据库、SSH 基线 | 核验目录 700、数据库 600、环境文件 600；SSH 仅密钥；fail2ban 正常 | 已在线修复/生效 |

### 行为与验证契约

- 账号来自现有认证查询；访客身份来自验签且未过期的会话 ID。Cookie 是否存在、自报 UA 均不构成认证或豁免。
- 同出口不同账号/签名会话分开计额，原有真实 IP 兜底仍保留。代理头只从本机反代链路读取，并校验 IP 格式；CF 头的真实性仍依赖源站锁定配置。
- 初始 observe。管理接口 `/api/admin/reader-security` 复用管理员白名单、二次验证与 CSRF；切到 enforce 必须观察满 24 小时并明确提交 `observation_reviewed: true`。后台面板可切回 observe、撤销全部验证凭证。
- 独立 `READER_TURNSTILE_SITE_KEY` / `READER_TURNSTILE_SECRET_KEY` 未配置时显示“未启用”。服务端 Siteverify 核验 success、hostname、action、会话 nonce 和 5 分钟 token 时间；失败不发凭证。
- 本站验证凭证存放服务端 security.sqlite3，绑定签名访客会话，30 分钟有效；删除记录或更新 epoch 可撤销。不增加会员权限，不绕过封禁或高频限额。
- 图片/JSON/SSE/支付回调不返回交互验证 HTML。异常资源返回 JSON 403 与 `X-Reader-Verification`；前端展示顶层验证链接。仅明确的顶层 document 导航可跳转验证页。限速 429 带 Retry-After；容量忙为 503。

### 取证与容量边界

安全事件包含服务器生成的请求 ID、规则、端点、状态码、发布号和每日 HMAC 去标识 actor。区分 attempt、denied、response_ready/delivery_started、delivery_complete/interrupted。`delivery_complete` 仅表示应用迭代器完成发送，**不证明客户端收到整本文件**；206 只说明该范围响应完成。

队列最多 4096 条，批次最多 2048；按分钟聚合，保留首末请求 ID 样本及计数。每天详细聚合行达到 5000 后合并为低细节计数，并增加 `events_coalesced`。队列丢弃、数据库写入丢弃均单独计数，后台展示并触发聚合告警。此设计不是每个请求完整明细的永久保留承诺。

活动库原始阅读审计保留 30 天，去标识事件 180 天，每日汇总 400 天。Caddy 新日志默认 gzip，25 MiB 轮转，最多 8 个归档、最长 720 小时；上限可能使保留时间短于 30 天。取证只计 access logger，不重复叠加 error logger。事件证据另行保全；已有系统日志与备份本轮不删除。备份仍遵守“两次更新发布健康至少 30 天、可恢复归档后再清理”的项目规则，不能声称活动库清理已经删除备份中的历史数据。

## 新发现的依赖风险

以实际进程 PYTHONPATH 核验到 Flask 3.1.3、Werkzeug 3.1.9、waitress 3.0.2、PyMuPDF 1.27.2.2、Pillow 12.3.0、cryptography 45.0.7、requests 2.33.1、lxml 6.0.2。第一次未带生产 PYTHONPATH 的系统解释器查询不能代表应用依赖，已明确排除。

OSV 查询发现 cryptography、lxml、Pillow 的新公告，原始查询及详情保存在本目录 `security-defense-20261027/dependency-*.json`。这是已列明依赖的公告核查，不等同于完整传递依赖/系统原生库审计。

- requirements 将 cryptography 固定到 50.0.2、lxml 到 6.1.3，并固定实际核验过的 Werkzeug 3.1.9。新增 RSA2 签名/验签/篡改拒绝、XML 外部实体测试通过。
- 当前插注 XML 入口本已使用禁用实体/DTD/网络的 parser；本轮仍升级底层依赖，消除默认 parser 风险。[lxml 官方公告](https://github.com/lxml/lxml/security/advisories/GHSA-vfmq-68hx-4jfw)
- Pillow 修复版 13.0.0 要求 Python >=3.11，而当前生产发布脚本强制 3.10。未擅自扩大为线上解释器迁移。本轮 PDF 内嵌图片改经 PyMuPDF 解码为 RGB 像素再交给 Pillow；OCR 后续仅允许内部 PNG。主应用页图原本就是 Image.frombytes 编码 WebP，不经过 Image.open 自动识别上传文件。**Pillow 依赖风险仍标为待完成，不能计为修复。** [Pillow 自动识别拒绝服务公告](https://github.com/python-pillow/Pillow/security/advisories/GHSA-xgh8-jp6q-p5x8)、[JPEG 缩放解码公告](https://github.com/python-pillow/Pillow/security/advisories/GHSA-8g5g-8vmr-9782)

后续必须准备支持 Python 3.11 的独立发布运行环境及全部依赖，核对 OCR/检索/支付/文档导出兼容性、安排已执行项目的复测授权，再由协调者迁移。不得在当前 3.10 requirements 中直接换成不可安装的 Pillow 13，也不得把未修复项隐藏在“安全升级完成”中。

上传检查保留了体积上限、PDF 魔数、身份/归属校验及审核流程；本轮未改变上传权限、支付签名协议、会话 Secure/HttpOnly/SameSite 配置或管理员权限模型。未发现本次事件导致账号接管、数据库泄露、整本 PDF 外泄的证据。

## 验收记录与限制

唯一台账是 [SECURITY_TEST_LEDGER_20261027.json](SECURITY_TEST_LEDGER_20261027.json)，每次先登记开始状态及源码摘要，再执行，失败/中断不覆盖。日志、JUnit 位于 `D:/CodexData/outputs/security-defense-20261027`。

1. 首次回归：323 passed、2 failed、2 skipped、54 subtests passed。失败为新增测试夹具：未设置回环代理地址、未建立 Flask 请求上下文。原始失败记录保留。用户随后明确批准仅复测这两项；修正夹具后的该次复测为 2 passed，详见台账 `authorized-two-fixtures-20261027`。未重跑其他项目。
2. 依赖与 shell 新场景首次执行：12 passed。使用 D 盘独立依赖目录加载新 cryptography/lxml；未重复上面的测试。
3. 服务器已安装 Caddy 成功 adapt 增量候选。只是解析验证，不 provision、不 reload、不改线上文件；正式发布仍需流程内 caddy validate。
4. 新依赖环境的首次 deployment smoke：模板解析、JS 语法、隔离环境导入与公共导航通过。未装真实语料/API/支付密钥，不能当作全量生产数据、真实 AI/支付调用验收。

smoke 子进程退出码为 0、完成标记及原始日志均已保留；台账包装器在随后回显含中文的日志时发生 Windows GBK 编码错误并退出 1。此错误不是 smoke 失败。已修正日志编码；随后执行上述获批的两项复测时，包装器正常回显并退出 0，smoke 本身未因此重跑。

跳过项目、失败节点及版本以 JUnit 为准。旧 10 月 18 日病理语料重现不重跑；本轮只用新的合成文本验证确定性匹配。未进行线上攻击复现、压力测试、真实邮件发送或真实支付。新的依赖版本不是此前整个 323 项回归的运行环境，不能借旧结果宣称全套在新依赖上通过。

### CI 门禁失败与修正

首次固定 pre-push 门禁通过：391 个 tracked Python 文件编译、26 项发布清单/元数据测试和 deployment smoke。main CI run `40448337299` 的浏览器门禁通过；Python 3.10/3.11 均在 deterministic pytest 步骤失败，后续集成门禁跳过。Node/Ubuntu 升级提示不是已经确认的失败原因。

用户随后明确批准只复测旧节点 `tests/test_candidate_review.py::test_watchdog_does_not_probe_or_record_restart_while_release_owns_lock` 一次。该次复现为 1 failed，具体为第 89 行 `assert 'UNEXPECTED' not in result.stdout`。旧夹具将所有探测标为意外输出，第 91 行还要求“推迟探测”的旧日志；新看门狗按批准计划在占锁时继续观察，只在需要恢复时取锁，因此旧测试契约已经过时。原 CI 完整失败清单尚未取得，不能声称这是唯一失败。

本轮将该节点改名为 `test_watchdog_observes_without_restarting_while_release_owns_lock`，夹具隔离服务 PID、发布路径、启动时间、探测和锁，取消真实等待；断言三次失败探测发生在尝试恢复锁之前，锁忙后不重启、不写入重启时间，健康失败仍有日志。看门狗实现及正式发布的暂停/恢复流程不变。台账保留新旧节点映射，更名不视为未执行的新场景。用户随后明确授权该单项复测，以及通过后 pre-push/main CI 固定门禁各一次；单项复测为 1 passed，见 `authorized-watchdog-postfix-once`，后续门禁失败即停。

另行发现的 `/v2/read` 公共目录页探测兼容问题已修正，两项 curl/urllib 新场景首次执行通过，复用其已有结果，不将其冒充上面的 CI 失败原因。

后端 pytest 步骤改经 `scripts/ci_pytest.py` 单次执行，原有测试范围保留；完整日志、JUnit 和退出码记录存入 runner 临时目录，失败摘要转义后写入 check 注释。各后端 job 使用 [GitHub 官方 artifact action](https://github.com/actions/upload-artifact) 在成功或失败后归档证据 30 天，不重试、不覆盖旧证据。日志工具的新增隔离测试只模拟 pytest 进程，不执行旧业务场景；首次执行为 2 passed，覆盖成功/失败退出码、单次调用、原始日志、JUnit 和注释转义。

## 发布与回滚交接

用户已明确要求复测通过后推送并平滑上线，由本会话继续协调发布。推送前再次核对 main、production、线上发布，均仍为 cb945b8；没有改用旧版本。另发现新增 shell 测试只识别本机 MARX_TEST_BASH 配置，已补 Linux CI 的 bash 自动发现；这些测试的修改将由获批发布门禁验证，未自行重跑。

1. 仅指定协调者可在干净且已推送、HEAD 等于 origin/production 的 production 分支执行 `deploy/release.ps1`；先核对 main/production/live 是否仍为已确认的预期版本。通过所需集成和发布门禁后才能推进 production。若门禁与本轮已执行项目重叠，先取得复测授权，不能直接触发 pre-push/CI 自动重复。
2. 本分支禁止直接部署。生产动作全部在现有发布锁事务内：Caddy 增量处理、服务更新、CF 同步来源切换、安全观察 timer 安装。Caddy 遇到未知站点或日志布局会拒绝修改，交回人工审查。
3. 确定性缺口修复可先发布，行为判断保持 observe 至少 24 小时。复查正常读者/共享出口的风险命中、各端点 403/429、页图成功交付、支付回调、AI SSE、队列丢弃与健康状态后，由管理员决定 enforce。
4. 专用 Turnstile 密钥配置、真实服务端验证验收、Cloudflare 当前 WAF/跳过规则核查仍待完成。不要启用无法按规则排除支付回调的全站 Bot Fight Mode。[Cloudflare 限制](https://developers.cloudflare.com/waf/custom-rules/skip/options/)
5. 确认误伤时通过后台切回 observe，必要时撤销验证 epoch。程序故障仅使用正式 `deploy/rollback_release.ps1`，传入 expected current 和 target；脚本保留记录。回到旧版本时移除本轮标记的 Caddy 防护/日志块并停用无对应源码的安全观察 timer，不覆写其他线上配置。
6. 本轮上线记录为“main CI 失败后停止，尚未执行生产发布”，回滚记录为“未执行”；见 [release-record.json](security-defense-20261027/release-record.json)。线上现有 605 条封禁、原始证据、旧工作树和备份均保留。

本地采用 D 盘小型工作树及复用依赖，没有复制整套语料、PDF、模型或旧工作树，也没有清理 Docker、个人文件和历史聊天。运行环境新增依赖及验证日志需要保留以供发布前复核。
