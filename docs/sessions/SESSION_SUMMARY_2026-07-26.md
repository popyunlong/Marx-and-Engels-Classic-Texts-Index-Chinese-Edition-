# 会话总结 · 四页面布局上线 + 检索增强 + 首页信息区统一（2026-07-25 → 07-26）

> 面向接手的 AI / 站长的一份完整会话记录。读完即可掌握本会话做了什么、为什么、在哪、怎么验证、
> 已上线到什么程度、以及遗留事项。**代码状态：全部已上线生产（DEPLOYED_SHA `f88c591-dirty`），
> 工作树仍 dirty、未 commit。** 简版自动记忆见 `[[four-pages-layout-preview]]`、`[[retrieval-synonym-prf]]`。

---

## 0. 一句话状态

把「四页面布局重构」从**预览未部署**推到**生产已上线**（`/` cutover 生效＝首页变四页面检索），
其间修了一批预览问题、做了检索层两项召回增强、部署后又统一了首页下方信息区的配色/UI。
全程范围化部署、逐文件核对、零外来 WIP、自带回滚。

---

## 1. 生产环境 & 部署要点（最重要，先记）

| 项 | 值 |
|---|---|
| 服务器 | `38.76.174.234`，root，`/opt/marx-search`，systemd `marx-search`（内网 :8000） |
| SSH | 别名 `marx-cloud`；私钥 `~/.ssh/id_marx_cloud_ed25519` |
| 公网 | https://mazhuzuojiansuo.com （Cloudflare 橙云） |
| 部署脚本 | `deploy/update_cloud.ps1 -AllowDirty`（读 `deploy/cloud_patch_files.txt` 清单，只推清单内文件） |
| DEPLOYED_SHA | `f88c59100d6f792ae7dfc0758d40cad609088a9d-dirty` |
| 回滚点（远端自动备份） | `/opt/marx-search.cloud-backup.20260726-{015929,021353,021949,023043}`（保留最近 5 份） |

**update_cloud 自带安全网**（很关键）：本地冒烟 → **部署前远端全量备份** → scp+解包 → 服务器侧 import 冒烟 →
`systemctl restart marx-search` → **重启后 curl `/`、`/pricing`、`/api/runtime` 校验，失败自动回滚**。
不 `-RebuildCorpus`（语料无改）。

**范围化部署手法**（§5.1）：**临时把 `cloud_patch_files.txt` 换成仅本次差异文件的清单（≥2 行避 splat 坑），
字节备份原清单、部署后还原（勿 git checkout）**。本会话据此分批推了 5 次（首轮 20 文件大改 + 4 次首页 CSS 微调）。

---

## 2. 本会话工作全景（按主题）

### A. 四页面布局预览期修复（上线前）
1. **阅读页外文文库对所有身份可见**：`app.py _foreign_library_books` 加 `require_access` 参数；阅读页传
   `require_access=False`，使外文原著**与中文 PDF 书目一致——对所有身份陈列书名**，真正阅读权仍在
   `wenku_reader` 入口按 `static_library` 门控（游客点击 →302 跳 `/login`，不泄露）。`/reader`、`/library` 原样。
2. **AI 页引文可选引用格式**：`ai-page.js` + `ai-page.css` 引文条头加下拉（国标 GB/T 7714—2015 / 中国社会科学 /
   马克思主义研究），复用**全站共享键** `marx-citation-format-v1`；引文串写进 `data-citations`，切换即就地重写 +
   跨条同步 + 复制当前格式。**研究综述与快速问答接地引文均适用**（后端 `hit.to_dict()` 已带三格式映射）。
3. **流式阅读引文可选格式**：`templates/wenku_reader.html`（内联 JS/CSS，无 `?v=`）「引用所选」浮窗 +
   「引用本页」弹窗加下拉；`stdCiteFormats(c,v,page)` 从卷字段就地拼 3 格式，**与后端
   `search.DEFAULT_CITATION_TEMPLATES` 逐字对齐**（题名领起无著者；`publisher_zh:"北京：人民出版社"` 正则拆
   place/pub）；**仅 `CFG.lang==='zh'`（流式《文集》）启用**，外文书回退单一 zh。
4. **`###` 字面残留修复**：flash 偶发把小标题写成 `**### 标题**`（`###` 又叠整行加粗）→ 标题正则要求行首是
   `#` 故匹配不上 → 掉进「整行加粗提升为 h3」兜底、把字面 `###` 塞进标题。修法＝**判 ATX 标题前先剥整行 `**`
   包裹 + 去尾随 `#`**；兜底里再剥一层 `###` 前缀。**AI 页（ai-page.js）与检索页（index.html 另一份
   renderBasicMarkdown）都修了**。

### B. 排版反复（记录用户意图变化，避免重蹈）
- 研究综述最初做了 essay 层级（`##`→h2 大标题 / `###`→h3）+ 富引文卡（金色 [N]/综述已引用/名目索引徽标/证据页/
  复制引文，对齐检索页 `.rv-cite`）。**坑：`/v2/ai` 在 `body.v2`，`layout-v2.css` 的
  `body.v2 #aip-root .msg-body h3` (1,2,2) 盖过纯 `.aip-essay h3` (1,2,1)→须补 `body.v2` 前缀变体 (1,3,2)。**
- 又加了**原文引语高亮**（essay 模式把 `"…"` 内 ≥6 字成句原文包 `.aip-quote` 暖金底）。
- **然后用户两次改口**：① 快速问答与研究综述应「共用同一套排版逻辑、配色可不同、快速档更对」→
  **统一标题映射为 `hashes<=3?h3:h4`（撤掉研究档 `##`→h2）**，研究档 h3 只覆写颜色（鎏金）其余版式继承快速档；
  ② **把原文高亮去掉** → `_essayMode`/`.aip-quote`/`renderBasicMarkdown` essay 形参全撤，`.aip-essay` 类仅留作
  研究档 h3 鎏金配色。**净结果：两档排版逻辑一致、仅配色红/金之别，无原文高亮。**
- 富引文卡 + 引用格式下拉保留。

### C. 检索层召回增强（`search.py`，`[[retrieval-synonym-prf]]`）
> 本站检索是**纯字面匹配无 embedding**，天花板＝同义/隐含语义召不回。二者**只影响找到哪些真实命中，
> 不碰「引文不可伪造」**（最终引文仍是真实 Hit）。
1. **领域同义/译名词库（概念组共现）**：模块级 `TERM_THESAURUS`（9 组种子：异化/外化、无产阶级/工人阶级、
   私有财产/私有制…）+ 反查表。`keyword_cooccurrence` 加 `expand_synonyms`（**默认 False 逐字兼容**）——
   为真时同义并成「概念组」，**distinct 按概念数计**（不是 surface 词数，否则加同义反抬高门槛，这是关键设计）。
   `locate_associative` 默认 `expand_synonyms=True` 透传。实测：无产阶级+解放 **+26 卷**、私有财产+消灭 **+76 卷**；
   非同义词查询 True==False 逐字一致（148==148）。
2. **伪相关反馈 PRF（两趟检索）**：`locate_associative` 加 `pseudo_feedback`（默认 False，**intent=="research"
   自动开**）。首轮命中后从 top6 命中 `context` 用 `_prf_phrases`（**跨全段半重叠采样，本环境无分词器**）切候选
   短语→回灌 `fragment_search` 第二趟；**去噪靠既有频次闸**（>300 弃用）。实测 locate 加 3 条真实新命中。
3. **调用面**：chat 接地（`_build_chat_grounding`）走默认→同义 ON / PRF OFF（保交互速度）；研究综述
   （`api_search_associative` intent=research）→同义 + PRF 均 ON。`app.py` 无需改。

### D. 首页微调
1. **导航**：`_appnav.html` 会员套餐移到「登录/注册」之后（未登录态）；新增 `.v2btn-soft`（柔和品牌红药丸底），
   三按钮层次＝描边登录→实心注册→柔底会员套餐。
2. **下方信息区改 3+2 栅格**：`index.html` + `layout-v2.css`。第一行「公告 / 注册分布 / **文库(新增)**」三卡等宽等高，
   第二行「社区建设 / 留言反馈」两卡等宽等高（原来社区是通栏 banner）。
3. **「网站支持的文库」卡（新增）**：复用 hero 的 `book_stats` 收录清单（固定版次文案/实时卷数 + 外文原著），
   **竖向无缝循环滚动（同社区建设 marquee 机制：两份内容 translateY -50% + 悬停暂停 + 减少动态回退）**。

### E. 上线后首页信息区微调（纯 CSS，均 `body.v2` 作用域）
1. **注册分布 choropleth 地图隐藏**（窄栏里被 `max-height:165px` 裁不全）→ `body.v2 .rg-mapwrap{display:none}`。
2. **公告自动日期隐藏**（今天日期意义不大）→ `body.v2 .notice-date{display:none}`。
3. **「少…多」渐变条隐藏**（地图没了此条无所指）→ `body.v2 .rg-legend{display:none}`。
4. **五卡配色/UI 统一**：底色全部暖白（原公告米黄/注册分布米色/**社区建设蓝色**/文库·反馈白 → 一致）；
   标题全部品牌红（原公告/注册分布深色）；社区条目由蓝转暖（暖粉底 + 品牌红项目符号，与文库胶囊同调）。

---

## 3. 部署过程（§5.1 严格流程，本会话实战记录）

1. **探明服务器现状**（ssh grep）：四页面路由 `layout_search` 等 = 0、`_appnav.html`/`layout-v2.css` 不存在
   → **四页面布局此前从未部署**，本次是首次上线（含 `/` cutover）。
2. **拉服务器现行版做基线**（tar over ssh），逐文件 `diff` → **20 个文件有差异，全部核对为「四页面 + 本会话」预期，
   零外来 WIP**；config/data 不在差异集（正确排除）。二级页各仅 2 行＝移除全站 AI 抽屉 include；ai.py 14 行＝
   答案分支加 Markdown 排版指令。
3. **补发布清单**：`cloud_patch_files.txt` 原漏了 `ai.html`/`_appnav.html`/`layout-v2.css`/`ai-page.js`/`ai-page.css`
   → 补入；`test_deploy_manifests.py` 6/6 过。
4. **文案对齐**：三处引用格式国标标签统一 `国标 GB/T 7714—2015`；外文命名统一「外文原著」；流式浮窗「格式」→
   「引用格式」。
5. **范围化部署**：临时清单收窄到 20 文件 → `update_cloud -AllowDirty -DryRun`（本地冒烟过、0.41MB）→ 真部署 →
   还原清单。**服务器侧运行校验通过、未触发回滚。**
6. **上线后 4 次 CSS 微调**（E 节）各走一次范围化部署（清单 3 文件：layout-v2.css + index/ai.html 版本号）。

---

## 4. 版本号 / 关键文件

- 静态版本：`ai-page.js?v=19`、`ai-page.css?v=10`、`layout-v2.css?v=24`（index.html / ai.html 已同步）。
  `wenku_reader.html` 内联 JS/CSS 无版本号。
- 本会话改动文件：`app.py`、`ai.py`（四页面既有）、`search.py`、`templates/{index,ai,_appnav,wenku_reader,viewer,
  library,wenku,liushi_home,account,dictionary,dictionary_entry,pricing,journal_alerts,journal_latest}.html`、
  `static/ai-page/{ai-page.js,ai-page.css}`、`static/layout-v2/layout-v2.css`、`deploy/cloud_patch_files.txt`。

---

## 5. 坑 & 经验（务必先读）

- **CSS 特指度**：`/v2/ai` 与首页都在 `body.v2`，`layout-v2.css` 的 `body.v2 …` 规则会盖过纯类选择器；
  想覆写须补 `body.v2` 前缀变体或足够特指度。
- **预览验证**：`run_preview.py` 服务器模式跑 AI（`MARX_SKIP_NODE_CHECK=1 APP_MODE=server PREVIEW_PORT=8011`）；
  **应用内浏览器很脆**——navigate 偶发 300s 卡死、隐藏 pane 截图必超时 → 一律用 `javascript_tool` 读 computed
  style / DOM 断言，用**显式 tabId** 指 preview 源 tab，验 AI 渲染注入会话进 `localStorage(marx-ai-sessions-v2,
  admin uid=-102)` 再 reload。**站长自己前台跑预览；AI 后台起的服务器进程本机会被回收，勿并发起服务器。**
- **`wenku_reader.html` 有 NUL 哨兵**：`grep -a` / `diff -a` 才能当文本比对（否则被判 binary、+0/-0）。
- **部署清单漂移**：新静态/模板须进 `cloud_patch_files.txt`，否则不上线；`test_deploy_manifests.py` 把关。
- **改 `search.py`/`app.py` 须重启**才生效（update_cloud 会重启）；`corpus.sqlite.sha256` 脏不影响网页部署
  （不在清单、只关桌面打包）。
- **retrieval**：`TERM_THESAURUS` 是**种子待站长审校**（私有财产/私有制 这类「紧邻但不全等」为召回计并组，
  嫌宽可拆）；`expand_synonyms` 默认 True 影响所有接地作答，PRF 仅研究综述。

---

## 6. 遗留 / 建议后续

- [ ] **把工作树整理成 git commit**（现以 `-dirty` 上线多轮，纯版本卫生，不影响运行）。
- [ ] 扩充 `TERM_THESAURUS`（按《资本论》等高频术语），并让站长审校现有 9 组。
- [ ] 可选：把 `**###**` 修复之外的检索页研究综述标题层级也拉成 `###→h3`（现为 `###→h5`）——用户未要求。
- [ ] 可选：给检索层建一把「金标准查询集」评测尺，量化同义/PRF 收益后再决定是否上语义向量层。
- [ ] `PLAN_layout_four_pages_2026-07-20.md` 若还有 SPA/套壳旧提法可收口（低优先）。

---

## 7. 交叉引用

- 四页面布局交接（canonical，含完整路由/模板开关/§5.1 部署细则）：`HANDOFF_four_pages_layout_2026-07-25.md`
- 自动记忆：`[[four-pages-layout-preview]]`（四页面 + 首页微调 + 部署要点）、`[[retrieval-synonym-prf]]`（同义词库 + PRF）
