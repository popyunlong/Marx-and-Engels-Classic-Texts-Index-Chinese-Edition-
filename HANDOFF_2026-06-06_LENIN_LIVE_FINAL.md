# 2026-06-06 列宁《全集》正式上线 · 最终交接文档

> 本文件接续 `HANDOFF_2026-06-06_LENIN_COLLECTION_SAFE_UPLOAD.md`，记录"安全上传 + 修复检索 bug + 加书库筛选"全部完成后的最终状态。
> 关联事故复盘：`INCIDENT_2026-06-05_502_RECOVERY.md`。

---

## 0. 一句话结论

列宁《全集》**60 卷已全部上线生产环境**，引文检索、篇章检索（首页 + 阅读器）、按卷阅读、PDF 正文渲染、书库筛选标签**全部可用并已线上验证**。全程无 502，仅有重启瞬间的极短切换。

生产服务器：`root@38.76.174.234`，目录 `/opt/marx-search`，公网 `https://mazhuzuojiansuo.com`，systemd 服务名 `marx-search`。

---

## 1. 最重要的安全经验（下一位务必先读）

1. **不要用 PowerShell 跑会发起 SSH 的部署脚本（后台模式）。**
   本轮 `deploy/upload_lenin_pdfs.ps1`、`deploy/update_cloud.ps1` 在"后台/无终端"环境里会**卡死**——`ssh` 没有 `-n`、在脱离控制台时阻塞在 stdin。中文远端路径会加重这个问题。
   - 前台单条 `ssh` 在 PowerShell 工具里是 OK 的（1 秒返回）；但 4GB 上传 / 30 分钟重建不能放前台（会超时）。
   - **本轮改用 bash 全程驱动，稳定可靠**（见第 4 节新增脚本）。下一位继续维护时优先用 bash 脚本。

2. **改 `available` 之类的"露出开关"必须重启服务**才能生效（篇章直达索引是进程内存里构建的，启动时一次性 build）。

3. **重建大语料（corpus.sqlite）仍必须显式进行**，普通代码补丁不重建。Linux 上 `build_index.py` 先 unlink 旧库再建新库，旧进程持有旧 inode 继续服务，**重建期间公网不中断**，只有最后 restart 才切换。

4. **列宁 PDF 只增量传 `pdfs/列宁《全集》`**，绝不重传 `pdfs/文集`、`pdfs/全集`。

5. **新增 `.py` / `config/*.yaml` / 模板后，必须同步进 `deploy/update_cloud.ps1` 的 `$files` / `$compileFiles`**（这是上次 502 的直接原因）。本轮新增文件均已在清单内。

---

## 2. 本轮完成的工作总览

### 2.1 安全上传与上线（阶段 B→E，已全部完成）
- **阶段 B**：上传列宁 60 卷 PDF（4.1 GB）到 `/opt/marx-search/pdfs/列宁《全集》`。本地↔远端总字节数完全一致（4,298,429,936），权限 `www-data:www-data`，未重启、未动数据库。
- **阶段 C**：代码/配置补丁 `-SkipRestart`，远端 `py_compile` + smoke 通过——**证明导致上次 502 的"缺 `book_config` 模块"问题已不存在**。备份在 `/opt/marx-search.cloud-backup.20260606-101614`。
- **阶段 D**：服务器端 `nohup` 后台重建 corpus，counts 与本地一致，SHA256 校验通过，重启健康（带自动回滚兜底，未触发）。
- **阶段 E**：公网验证 `/`、`/library`、`/reader` 均 200，首页显示"列宁《全集》 60 卷"，三套书分组、卷内目录、正文渲染均正常。

### 2.2 修复了两个真实检索 bug（用户反馈后排查）
见第 3 节。

### 2.3 新增"书库筛选标签"功能
见第 3.3 节。

---

## 3. 关键代码改动（本轮新增，务必理解）

### 3.1 `build_index.py` —— 空白页补全索引越界 bug（会让重建崩溃）
- 位置：`fill_missing_printed_pages` 里调用 `is_probably_blank_page(rows_mut[k][4], rows_mut[k][5])`。
- 错误：元组是 `(book, vol, source_file, pdf_page, printed, raw, norm)`，`printed`（索引 4）可能是 `None`，传给 `is_probably_blank_page` 的 `raw_text` 会让 `unicodedata.normalize(None)` 抛 `TypeError`。
- 修复：改成 `rows_mut[k][5], rows_mut[k][6]`（raw, norm）。
- 影响：列宁 PDF 触发了这条空白页路径，第一次重建直接崩。**云端 `-RebuildCorpus` 同样会中招，已一并修好。**

### 3.2 `search.py` —— 引文检索只搜第一套有命中的书（核心 bug）
- 位置：`search_grouped()` 与 `search()` 的精确匹配循环。
- 错误：`for book in self.books: hits = _exact_in_book(...); if hits: return ...` —— **一旦某书命中就 return**，后面的书永远搜不到。任何在《文集》出现的词，《全集》《列宁全集》正文都搜不到（连《全集》也被屏蔽，只是以前没人注意）。
- 实证："帝国主义"在《文集》5 页 /《全集》48 页 /**《列宁全集》3694 页**，原来只返回 3 条《文集》结果。
- 修复：改为**遍历所有书库累计命中**，新增常量 `EXACT_HITS_PER_BOOK = 200`（每库精确命中上限，保证多书都被搜到且限制耗时）。`_group_hits` 本来就按 `book_sort_order` 排好序，无需改。
- 效果："帝国主义"命中组数 **3 → 75**（其中列宁 43 组），所有查询 < 0.1s。

### 3.3 `config/books.yaml` —— 列宁 `available: false → true`
- `available` 字段**只**在 `app.py` 的 `_build_toc_suggest_index()`（第 ~4152 行）使用，用于"篇章直达"是否露出该书库。其余 `_feature_is_available` 等都是无关的会员功能开关。
- 原值 `false` 是"暂未上线"的占位（books.yaml 注释写明"云端验证可用后改 true"）。本轮已验证正文+目录可用，故改 `true`。
- 改后篇章索引含 **11,960 条列宁目录**。**改这个必须重启**。

### 3.4 书库筛选标签（`app.py` + `templates/index.html`）
- 后端 `app.py /api/search`：
  - 新增 `_search_book_counts(groups)` 辅助函数；
  - 读取请求体 `book` 参数（按 `BOOK_CONFIG_BY_KEY` 校验），**过滤前**先统计 `book_counts`（保证标签稳定显示三套书的命中数），再按所选书库过滤 `all_groups`；
  - 用 `effective_total_hits`（过滤后命中数）替换原 `grouped["total_hits"]` 参与展示/分页判断；
  - 响应新增 `book_counts`、`book_filter` 两个字段（summary 与 full 两条返回路径都加了）。
- 前端 `templates/index.html`：
  - HTML：`#results` 上方加 `<div id="searchBookTabs" class="book-tabs" hidden>`；
  - CSS：`.book-tabs / .book-tab / .book-tab.active / .tab-count`；
  - JS：`updateBookTabs(payload)` 渲染标签（仅当命中跨 ≥2 书库才显示）；`doSearch` 增加 `book` 参数并在请求体携带；`#searchBookTabs` 单独绑定点击监听（标签在 `#results` 之外，不能复用 `out` 的监听）；分组翻页 `group-list-page` 透传 `payload.book_filter`；恢复历史检索状态时也调用 `updateBookTabs`。
- 行为：搜索结果上方出现 **全部 / 《文集》 / 《全集》 / 列宁《全集》**（带命中数），点某书库即服务端过滤、分页正确。

---

## 4. 本轮新增的部署脚本（bash，可靠，建议沿用）

> 都在 `deploy/` 下。它们是 PowerShell 脚本的 bash 等价实现，专门规避 PowerShell 后台 ssh 卡死问题。

- `deploy/upload_lenin_pdfs.sh`
  仅上传本地 `pdfs/列宁《全集》`（恰好 60 个 PDF），逐文件按字节大小跳过已存在文件（幂等可续传），改权限、校验远端 60 个，**不重启、不动数据库**。
- `deploy/push_code_patch.sh`
  等价 `update_cloud.ps1 -SkipRestart`：本地 smoke → 打包同一套 `$files` → scp → 远端备份 → 解包 → 改权限 → `py_compile` → 远端 smoke。**不重建、不重启、不动 systemd**。
- `deploy/remote_rebuild.sh`
  在服务器上跑：`build_index.py` → 文集/全集/列宁 toc → `chown` 数据库 → 打印 counts。等价 `-RebuildCorpus` 的重建部分，**不重启**。用法：scp 到 `/tmp/lenin_rebuild.sh`，再 `nohup bash /tmp/lenin_rebuild.sh > /tmp/lenin_rebuild.log 2>&1 &`。
- `deploy/restart_verify.sh`
  在服务器上跑：`systemctl restart marx-search` → 轮询 `/api/runtime` 健康 → 失败则从 `/opt/marx-search.cloud-backup.20260606-101614` 回滚代码再重启。已 scp 到服务器 `/tmp/restart_verify.sh`。

> 单文件快速补丁的可靠姿势（本轮反复用）：
> ```bash
> KEY="$HOME/.ssh/id_marx_cloud_ed25519"; HOST="root@38.76.174.234"
> scp -O -o BatchMode=yes -i "$KEY" 某文件 "$HOST:/opt/marx-search/某文件"
> ssh -n -o BatchMode=yes -i "$KEY" "$HOST" "cd /opt/marx-search && chown www-data:www-data 某文件 && chmod a+r 某文件 && . .venv/bin/activate && python -m py_compile 某文件 && echo ok"
> ssh -n -o BatchMode=yes -i "$KEY" "$HOST" "bash /tmp/restart_verify.sh"
> ```
> 关键：`ssh` 一律带 `-n`，`scp` 用 `-O`。

---

## 5. 生产环境当前真实状态（2026-06-06 验证）

数据库 `data/corpus.sqlite`（本地与云端一致）：
```
pages: 文集=9321   全集=42472  列宁全集=40922   （总 92715 页）
toc:   文集=1302   全集=8554   列宁全集=11968
```
进程启动日志：`Loaded corpus: 文集=10 全集=52 列宁全集=60`。

线上抽测（均通过）：
- 引文检索"帝国主义"：全部 75 组（文集 3 / 全集 29 / 列宁 43）；点"列宁《全集》"标签 → 43 组，首页即列宁。
- 引文检索"农民生活中新的经济变动"："《列宁全集》第1卷，北京：人民出版社，2013年，第1页。"
- 篇章检索"国家与革命" → 4 条列宁目录；"帝国主义" → 含列宁"'帝国主义'笔记"。
- `/`、`/library`、`/reader` 均 200；`/reader`、`/library` 三套书分组、列宁卷名渲染正常。

---

## 6. 仍待办 / 可选项（都不影响功能）

### 6.1 浏览器端人工确认（建议）
筛选标签是登录会员才能触发的前端 JS（访客 `/api/search` 返回 403，是访问策略，非 bug）。已验证：API 数据契约、模板渲染、所有 JS 辅助函数存在。**建议站长登录后实测**：搜"帝国主义"→ 点"列宁《全集》"标签 → 看是否只显示列宁且翻页正常。

### 6.2 品牌/文案默认值（编辑性，非功能）
仍有默认文案写"马恩《文集》《全集》"，**不影响检索**，且可能已被后台 `site_text` 覆盖（线上确有 `site_text.save` 记录），故未擅自改措辞。如需统一，可改：
- `templates/library.html:280` 阅读器页眉 `《马克思恩格斯文集》完整目录`（`library.reader_heading` / `ai_heading`）；
- `site_content.py` 欢迎语、功能栏（`index.hero_intro`、`index.feature_*`、`pricing.feature_viewer` 等）。

### 6.3 引文检索结果排序（产品取舍）
当前按 `book_sort_order`（文集→全集→列宁）排序，常见词列宁在后几页——但**已通过"书库筛选标签"解决可达性**。如还想进一步"列宁置顶 / 三库交错"，可再议（会改变现有马恩检索默认顺序）。

### 6.4 本地清理（可选）
- `data/corpus.sqlite.bak-before-lenin`（224MB，重建前备份，确认无误可删）；
- 日志 `rebuild_lenin.log` / `upload_lenin_bash.log`；
- 服务器临时脚本 `/tmp/lenin_rebuild.sh`、`/tmp/restart_verify.sh`（可保留备用）。

---

## 7. 本任务相关文件清单（本轮改动/新增）

代码/配置：
- `build_index.py`（空白页索引 bug 修复）
- `search.py`（跨书库累计检索 + `EXACT_HITS_PER_BOOK`）
- `app.py`（`book_stats`/`_library_volumes`/`_build_toc_suggest_index` 配置驱动；`_search_book_counts` + `/api/search` 书库筛选）
- `config/books.yaml`（列宁 `available: true`）
- `config/manifest.yaml`、`config/volumes.yaml`（列宁 60 卷 / 出版年）
- `book_config.py`、`runtime_env.py`（动态书库，已含 `available` 字段）
- `templates/index.html`（统计/结果动态化 + 书库筛选标签 UI）
- `templates/library.html`（分组/篇章动态化）
- `scripts/build_toc.py`（通用目录构建，含 `--book 列宁全集`）
- `tests/test_security.py`（toc-suggest 断言改为配置驱动，不再硬编码两套书）

部署脚本：
- 新增 `deploy/upload_lenin_pdfs.sh`、`deploy/push_code_patch.sh`、`deploy/remote_rebuild.sh`、`deploy/restart_verify.sh`
- 既有 `deploy/update_cloud.ps1`（`$files`/`$compileFiles` 已含新文件，`-RebuildCorpus`/`-SkipRestart` 开关）、`deploy/upload_lenin_pdfs.ps1`

> 注意：仓库 `git status` 本就很脏（大量历史未跟踪文件）。**只处理本任务相关文件，不要整理整库，不要 `git reset --hard` / `git checkout -- app.py`。**

---

## 8. 验证与质量

- 每次代码改动后 `python -m pytest` 均 **46 passed**（含修正后的 toc-suggest 测试）。
- 每次云端补丁：本地 smoke → 远端 `py_compile` → 远端 smoke → restart + `/api/runtime` 健康 + 失败自动回滚。
- 全程无生产 502。

---

## 9. 出现 502 时的应急（同事故文档）

```bash
systemctl is-active marx-search
curl -i --max-time 5 http://127.0.0.1:8000/api/runtime
journalctl -u marx-search -n 120 --no-pager   # 重点搜 ModuleNotFoundError/ImportError/SyntaxError/PermissionError/OperationalError
```
缺文件就补传 → `chown www-data:www-data` → `chmod a+r` → `systemctl restart marx-search` → 内外网都验证 200。
代码级回滚点：`/opt/marx-search.cloud-backup.20260606-101614`。
