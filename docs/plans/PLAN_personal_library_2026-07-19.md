# 个人文库模块 · 建设方案

日期：2026-07-19 ｜ 状态：**待站长确认后进入实现**
本文覆盖：新阅读器 + 用户自助上传 + 管理员审核 + 独立存储服务器 + 检索/AI 接入 + 用户增删。

---

## 0. 已锁定的四项决策

| 维度 | 选定 | 含义 |
|---|---|---|
| 存储位置 | **新建独立存储服务器** | 审核通过的原始 PDF 存到一台与主站语料彻底分家的机器/对象存储 |
| 阅读器形态 | **页面图像·还原原书** | 复用成熟的 `/viewer` fitz 渲染，任何 PDF（含扫描件）都能忠实阅读 |
| 检索覆盖 | **先做文字层 PDF** | 有文字层的书审核后即可检索/AI 问答；扫描件可读但标注「暂不支持检索」（OCR 留作 Phase 2）|
| 私密性 | **纯私有** | 每个用户只看得到/检索得到自己上传的书；审核只做合规与滥用把关 |

---

## 1. 总体架构与拓扑

**两台机器分工（核心拆分原则：大不可变 blob → 存储服务器；小可检索文本 + 元数据 → 应用服务器）**

```
                 ┌─────────────────────────── 应用服务器（现有 /opt/marx-search）─────────────────────────┐
   用户浏览器 ───►│  ① 上传接收（仿留言附件）   ② 审核队列（控制台面板）                                   │
                 │  ③ 文字层解析 fitz.get_text ④ 每用户检索索引 personal_index/<uid>.sqlite（在检索路径内）│
                 │  ⑤ 页面图像阅读器（按 owner 鉴权的取图接口 + 回源渲染缓存）                              │
                 │  ⑥ AI 问答挂接（每用户 Corpus 实例并入 grounding）                                       │
                 └────────────────┬──────────────────────────────────────────────────────────────────────┘
                                  │  原始 PDF 上行（审核通过后）/ 阅读时按需回源
                                  ▼
                 ┌─────────── 存储服务器（新建，可插拔后端）───────────┐
                 │  只存审核通过的原始 PDF（大文件、版权敏感）           │
                 │  后端三选一：LOCAL(开发) / S3 兼容对象存储(推荐) / 独立 VPS over SSH │
                 └──────────────────────────────────────────────────────┘
```

**为什么这样分：**
- 检索是**内存里对全局 `Corpus` 做字符串扫描**（`app.py:415`，无 SQL、无按用户隔离）。个人书**绝不能**塞进共享 `corpus.books`（会串给别的用户），所以每用户单独建一份小索引，且这份小文本索引必须留在应用服务器（检索在这里跑）。
- 原始 PDF 是大文件、版权敏感面，天然适合放到独立存储服务器，与主站精修语料/6.7GB 官方 PDF 分家，单独增长、单独备份、单独承责。
- 页面图像阅读器需要 PDF 字节做渲染 → 应用服务器**回源拉取 + 本地渲染缓存**（复用现有 page-image LRU 缓存机制），存储服务器只当权威冷存。

**存储后端抽象 `PersonalLibraryStore`（可插拔）：**
- `LOCAL`：文件落应用服务器数据盘隔离目录 —— 用于本地开发与预览，**功能可先端到端跑通**，无需等新服务器。
- `OBJECT`（推荐生产）：S3 兼容对象存储（阿里 OSS / 腾讯 COS / Backblaze B2），按量付费、免运维、签名 URL 访问。
- `SSH`：一台独立 VPS，克隆 `deploy/upload_book_pdfs.ps1`（改 `$ServerHost`/`$RemoteDir`）即为传输层。
- 三者同一接口 `put(sid, pdf_bytes)` / `get_bytes(sid)` / `delete(sid)`；**部署时用环境变量选定，代码无需改动**。

---

## 2. 数据模型

**新表 `personal_library_submissions`**（新模块 `personal_library.py`，DB 落 `APPDATA_DIR/personal_library.sqlite3`，规格仿 `feedback.py`）：

```
id INTEGER PRIMARY KEY
user_id INTEGER            -- 拥有者（所有读/检索/取图鉴权都对它）
user_email TEXT
title TEXT                 -- 用户填写书名（检索/引用显示用）
author TEXT                -- 用户填写作者（可空）
original_filename TEXT
stored_name TEXT           -- 随机名 token_hex(16).pdf
byte_size INTEGER
page_count INTEGER
sha256 TEXT                -- 存证 + 去重
has_text_layer INTEGER     -- 1=可检索 0=仅可读（扫描件）
status TEXT DEFAULT 'pending'  -- pending / approved / rejected / failed / deleted
reject_reason TEXT
license_attested INTEGER   -- 用户勾选「声明拥有使用权」
storage_backend TEXT       -- local/object/ssh，记录实际落点
created_at, reviewed_at, parsed_at TEXT
```
索引 `(status, created_at DESC)`（审核队列排序，仿 `page_error_reports`）。

**每用户检索索引**：`APPDATA_DIR/personal_index/<user_id>.sqlite`，**复用 `build_index.py:init_db` 的 `pages`+`toc_entries` schema**，`book` 列用 `mylib:<sid>` 命名。

**权限位** `personal_library`（`feature_access.py:7` 加一项）——控制「谁能用个人文库」，默认建议**会员**（同 `static_library`），管理员可在权限面板调整。

---

## 3. 六条流程（每条都对齐现有可复用模板）

### 流程一 · 上传（模板：留言图片附件 `_save_feedback_uploads` `app.py:4009`）
- 新页 `/mylib/upload`：书名 + 作者 + 选文件 + **「我声明拥有该文件的使用权」必勾**（沿用「西马文库」权利声明措辞）。
- 校验：魔数 `%PDF-` 嗅探（不信扩展名）；单本大小上限（建议 **100MB**，需为该路由提高 `MAX_CONTENT_LENGTH`，现全局仅 4MB `app.py:387`）；fitz 打开读页数 + 探测文字层。
- 落随机名临时 PDF + 插 `personal_library_submissions` 行 `status='pending'` + 后台线程邮件通知管理员（仿 `_send_page_error_admin_notice`）。

### 流程二 · 管理员审核（模板：期刊文章审核 `admin_journal_article_review` `app.py:8402`）
- 控制台**新面板 `#library-uploads`**（挂「内容运营」模块，标题带「（N 条待审核）」徽章）。
- 每条显示：书名/作者/上传者/页数/大小/是否有文字层/权利声明，可**预览首页缩略图**。
- 双路由 `POST /admin|/control/library/submissions/<sid>/review`，`action→status`：`approve→approved`、`reject→rejected`（附理由）、`reopen→pending`。走 `_require_admin()`+`_require_management_csrf()`+`_log_management_action()`+`_management_redirect()`。
- **审核就是你说的「控制台的审核控制」落点。**

### 流程三 · 解析 + 入库 + 推送存储（批准动作触发）
- 批准后：`fitz.get_text` 抽每页文字 → 写该用户 `personal_index/<uid>.sqlite` 的 `pages`（快、可在生产机跑）；顺带建极简 `toc_entries`。
- 扫描件（无文字层）：跳过入索引，`has_text_layer=0`，仅可阅读。
- 原始 PDF 经 `PersonalLibraryStore.put()` 推到存储服务器，删应用服务器临时副本（仅留渲染缓存）。
- 落 `status='approved'`（解析异常则 `failed`）+ `parsed_at` + 邮件通知上传者（仿 `_send_feedback_user_reply`，记录发信状态）。

### 流程四 · 阅读（新页面图像阅读器）
- 新阅读器 `/mylib/<sid>`（模板仿 `viewer.html` 骨架，去掉全局白名单相关件）。
- **新取图接口** `GET /api/mylib/page-image?sid=&page=N`：要求登录 + `submission.user_id==当前用户` + `status=='approved'`，然后 `PersonalLibraryStore.get_bytes()` 回源（本地 LRU 缓存 PDF）→ fitz 渲染 → 复用现有 page-image 缓存与 WebP/JPEG 协商。**绝不走全局 `ALLOWED_SOURCE_FILES`**，隔离靠 owner 判断而非共享白名单。
- 复用「继续阅读」历史（`reading-history.js`，`kind='mylib'`）。

### 流程五 · 检索 + AI 问答（核心工程，模板：既有 scope 机制）
- scope 词表加令牌 `mine`（或 `book:mylib:<sid>`）：`_parse_book_token`/`_resolve_search_scope`（`app.py:11733/11850`）学它，`_scope_label`/`_scope_options_payload` 渲染成 chip「我的文库」。
- **每用户 Corpus 实例**：按需从 `personal_index/<uid>.sqlite` 惰性加载一个 `Corpus`（`search.py` 同类），LRU 缓存最近 N 个用户的实例；上传批准/删除时失效该用户缓存。
- **标准检索**（`/api/search`）：现在是全局扫描后 post-filter，个人书扫不到 → 增加「个人 Corpus 扫描分支 + 合并分组/计数」。
- **AI 问答 / 联想 / 研究**：`_build_chat_grounding._locate`（`app.py:11960`）与 `/api/search/associative`（`app.py:12596`）在选中「我的文库」时，追加 `personal_corpus.locate_associative(...)` 并入 passages/citations。引文照旧不可伪造、真实接地。
- localStorage 沿用 `marx-search-scope-v2` / `marx-ai-scope-v2`。

### 流程六 · 用户自助增删（你要的「增添、删减」）
- 新页 `/mylib`（我的个人文库管理页）：列出该用户全部提交（含 pending/approved/rejected 状态），提供**阅读**、**删除**、**再上传**。
- 删除：清 `personal_index` 该书行 + `PersonalLibraryStore.delete()` 存储 blob + 表行标 `deleted`（软删留审计）。失效该用户 Corpus 缓存。
- 「增添」= 再走流程一（每次新书都要过审核）。

---

## 4. 安全与版权

- **纯私有 + owner 鉴权贯穿读/检索/取图三处**；个人书永不进公共语料、永不进全局白名单。
- **上传即声明使用权**（`license_attested` 必勾 + `sha256` 存证），措辞沿用「西马文库」既有实践；纯私有把版权暴露压到最低。
- 审核做合规/滥用把关，管理员可拒可删；拒绝/删除都留理由与审计（`_log_management_action`）。
- 大小/页数上限 + 魔数校验 + 随机存名 + 路径穿越防护（复用 `send_file` + `relative_to` 守卫）。

---

## 5. 分期与工作量

| 阶段 | 内容 | 是否本次上线 |
|---|---|---|
| **Phase 1（MVP）** | 上传 + 审核 + 文字层解析 + 页面图像阅读 + 每用户检索 + AI grounding + 自助增删 + 权限位 | ✅ 本次目标 |
| Phase 2 | 扫描件 OCR 入检索（离线 worker：rapidocr / GLM-4V，须在生产机之外跑）| 后续 |
| Phase 3（可选）| 每人容量/本数配额与计费、批量上传、EPUB 支持、存储服务器兼当渲染节点分流带宽 | 视需要 |

> 存储服务器 provisioning 是 Phase 1 前置，但**可先用 `LOCAL` 后端把整条链路跑通并上线预览**，等你把独立存储机/对象存储开好，改一个环境变量切到远端即可。

---

## 6. 部署要点（沿用你的既有范式）

- 新静态资源加进 `deploy/cloud_patch_files.txt`；新 `.ps1` 存 **UTF-8 BOM**；`.sha256` 用 **LF**；范化部署清单 **≥2 行**（避 splat 坑）。
- 单一共享工作树 / 单生产机 —— **勿与并行 AI 会话并发部署**。
- 存储传输：对象存储走 SDK；独立 VPS 克隆 `deploy/upload_book_pdfs.ps1`。
- `update_cloud.ps1` 不验新阅读器路由，须 SSH 侧 `grep` 自查已注册（同你既往教训）。

---

## 7. 待你拍板的三个小项（不阻塞出预览）

1. **存储后端具体形态**：S3 兼容对象存储（推荐，免运维）还是独立 VPS？代码可插拔，部署时定。
2. **上传权限默认档**：建议**会员**可用（同原文文库）。
3. **每人容量/单本上限**：建议单本 ≤100MB、每人 ≤N 本（N 待定，如 20 本）。

---

## 8. 涉及的主要文件（实现时）

- 新增：`personal_library.py`（表 + 存取，仿 `feedback.py`）、`personal_library_store.py`（存储抽象）、`templates/mylib_upload.html`、`templates/mylib_home.html`（我的文库）、`templates/mylib_reader.html`（新阅读器）、控制台新面板片段。
- 改动：`app.py`（上传/审核/取图/检索合并/AI grounding/路由与控制台上下文）、`feature_access.py`（权限位）、`search.py`（每用户 Corpus 加载 + scope 合并）、`control.html`（审核面板）、`index.html`+`_ai_assistant.html`（scope chip「我的文库」）、`deploy/cloud_patch_files.txt`。
```
