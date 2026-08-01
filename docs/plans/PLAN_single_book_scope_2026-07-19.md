# 计划：四类检索增加「针对指定著作（可多选、可细到某一卷）」的检索范围

日期：2026-07-19 ｜ 分支：main ｜ 状态：**✅ 已隔离部署上线生产（2026-07-20）**

## 部署记录（2026-07-20，隔离化 direct-scp）
- 上线文件：`app.py`、`search.py`、`templates/index.html`（**隔离版**）、`templates/viewer.html`、
  `templates/_ai_assistant.html`、`static/book-scope/{book-scope.css,book-scope.js}`。
- **隔离原因**：本地 `index.html` 混着别的会话 WIP（可拉伸 resultsBox / 收录文献折叠 / 首页玻璃卡改版 /
  member-strip 移除 / AI 抽屉移到 /ai）。做法＝**scp 服务器现行 index.html 当基线 + 只重放我的 8 处 book-scope 改动**
  （apply_index_isolation.py，每 anchor 断言恰好 1 次），产出 +30/−4 的隔离版；其余文件经核验为纯本会话改动，直接上传。
- `app.py`/`search.py` 本已含的 `/ai`+research WIP 在本会话期间被别的会话独立部署到了服务器 → 现行 diff 自然干净。
- 安全网：备份→上传→py_compile+import 冒烟（服务仍跑旧码）→通过才 restart→curl /api/runtime+/+/pricing 核验+失败自动回滚。
- 线上核验：homepage 渲染出 bookScopeMount/cooc-scope-row/BOOK_SCOPE_TREE(有数据)，且 resultsBox=0（隔离生效）。
- **遗留**：`templates/wenku_reader.html`（流式/文库阅读器）本会话未加控件（与服务器一致，非回归）——待补。

（下方为原始计划，保留备查。）

## 进度（2026-07-19）
- ✅ **Phase 1（后端·AI 路径）**：`search.py` 逐卷循环加卷级过滤（`_scoped_volumes`/`_volume_in_scope`）；
  `app.py _resolve_search_scope` 认 `book:`/`vol:` token 列表→scope spec（groups 仍返回 set，兼容旧测试）；
  `_scope_label`/`_parse_book_token`/`_scope_id_for_books` 渲染。研究/随心问/联想端点零改动自动支持。
  测试：`ScopeRoutingTests` 扩 8 例，**23 passed**（2 例全库扫描 deselect）。
- ✅ **Phase 2（后端·标准检索）**：`/api/search` 统一走 `_standard_search_scope`→`_scope_allows` 后置过滤
  （多本+卷级；旧 `book` 参数兼容）；`_chaptered_search_payload`/同段多词分支一致；`book_counts` 仍全库（D3）。
  测试：单本回归 + 新多本/卷级，**2 passed**。
- ✅ **Phase 3（前端）**：共享控件 `static/book-scope/book-scope.{js,css}`（`BookScope.mount`，导出 book:/vol: token）+
  后端 `_book_scope_tree()`（按著作群分组+卷号，喂 index/assistant-config/viewer 三处 render）。
  - **index.html**（引文/模糊/研究）：✅ 已本地浏览器实测——控件渲染 30 本/269 卷复选框/9 群；选《文集》整套→
    `scope:["book:文集"]`、结果 64 条全属《文集》、`book_counts` 仍全库 28（D3）；选卷→`{"文集":[5,6]}`→`vol:文集:5/6`；
    点书库分栏清「精选」；无 JS 报错。
  - **AI 随心问抽屉**（`_ai_assistant.html`+`ai-assistant.js`，站内多页）：✅ 已接（`currentScope` 优先精选，`applyConfig`
    挂载，dark 皮肤，`?v=6`）——待运行时复验。
  - **viewer.html**（PDF 阅读器「检索原著原文」=search-chat）：✅ 已接（`currentAiScope` 优先精选，dark，与抽屉共用
    `marx-book-scope-ai-v1` 选择）——待运行时复验。
  - **wenku_reader.html**：❌ **不适用**——其 AI 是 `/api/wenku/ai` 书内文本解读，非跨库检索，无 scope 概念（已核实
    模板无 `ai_scope_options`/`search-chat`）。故四类检索目标由 index+抽屉+viewer 三面已全覆盖。
  - **D-默认＝不跨访问记忆（站长 2026-07-19 定）**：修 `_book_scope_tree` 标签残留书名号（「马恩《选集」类→清全部 《》）；
    `book-scope.js` 默认 `persistOn=false`——每次挂载从空开始（＝全部著作），`sel` 不读/写 localStorage，仅显式
    `persist:true` 才记忆；三处调用均不传 persist，故**默认检索全部著作**。静态 `?v=1→v=2`。
  - **布局（站长 2026-07-19 定）**：首页把「指定著作」控件并入「同段多词检索」同一行、`margin-left:auto` 靠右对齐
    （新 `.cooc-scope-row` flex 容器 + `.book-scope-inline`），不再单占一行。**注意本地起服务须开 Jinja 模板
    自动重载**（`app.jinja_env.auto_reload=True`），否则 `debug=False` 会缓存模板、改动不生效。
  - ✅ 已复验：两静态 JS `node --check` 通过、NUL 哨兵保留（ai-assistant.js 仍 4 个）；viewer/抽屉 test-client 渲染
    含控件容器+树(9群)+接线；assistant-config 回 `book_scope_tree`；scope 测试 **25 passed** 无回归；
    `deployment_smoke --mode server` 通过。
  - 部署清单：`static/book-scope/*` 已加入 `cloud_patch_files.txt`。**生产部署仍待站长发话**——app.py 混有另一会话的
    `/ai` WIP，须以「服务器现行 app.py + 本会话改动」隔离出干净版再上（见上文部署评估）。

目标：让**「在指定的一本或多本著作、乃至指定卷里检索」**成为四类检索统一的一等范围选项——
引文检索、模糊检索、研究级检索、AI 随心问（阅读器 AI 导读同源受益；联想检索同源自动受益）。

---

## 0. 已定决策（站长 2026-07-19 拍板）

- **D1 粒度＝卷级**：能选到「一整套书」（如《文集》），也能进一步细到「某一卷」（如《文集》第 5 卷）。
  更细的「单篇著作」（《资本论》这类藏在整套里的跨卷篇目）**本次不做**。
- **D2 优先级＝指定优先**：同时点了「大类」和「具体的书/卷」时，**以具体选择为准**，忽略大类；
  只有「指定著作」留空（不限定）时，才回落到大类多选 / 自动判断。
- **D3 书库分栏＝照旧全显**：引文/模糊检索指定后，结果上方仍统计**全库**命中分布、可一键切换到别本
  （保留「先看全貌、再随手收窄」的体验）。→ 由此推出：**标准检索走"后置过滤"而非"缩小扫描"**（见 3.2）。
- **D-多选＝支持多本/多卷同时检索**（站长追加）：可勾选多本（如《文集》+《治国理政》）、或跨本挑卷
  （如《全集》第 5、6 卷 + 《文集》全卷）一起检索。**后端天然支持**（范围就是 `{书→卷}` 表，见第 2 节），
  本项主要是前端多选控件。
- **D4 多选控件形态＝方案①「两个控件分工」**：保留现有「大类」快捷 chips，另加一块"精选到书/卷"多选清单；
  精选清单有勾选即优先（D2）。改动集中在新控件，不动已上线的 chips，风险最小。（方案②"层级多选树"未采纳。）

---

## 1. 现状（已核对代码，非记忆）

现有「检索范围」是**著作群**级（8 群：马恩 / 列宁 / 西马 / 毛 / 邓 / 江 / 胡 / 习 / 党和国家文献），
既非单本、更非单卷。数据结构（[search.py](search.py)）：`Corpus.books` 是 `dict[书库键 → list[Volume]]`，
每个 `Volume` 带 `.volume`（卷号）、`.norm_full`（该卷归一化全文）、`.source_file`；**检索内层本就逐卷循环**
（`_exact_in_book`/`_fuzzy_in_book` 里 `for vol in self.books.get(book, [])`，[:1192](search.py:1192)/[:1227](search.py:1227)），
故"限定到某几卷"只是在这层加一个跳过判断。

| 检索类型 | 前端 | 后端 | 现有范围能力 |
|---|---|---|---|
| 引文/模糊（标准检索） | index.html `data-mode="standard"` | `/api/search` → `search_grouped`/`search_cooccurrence_grouped` | **已有单本 `book` 后置过滤**（[app.py:11430](app.py:11430)），无卷级；`search_grouped` 先扫全库再丢弃 |
| 研究级 | index.html `data-mode="research"` | `/api/search/associative`（intent=research，[app.py:12365](app.py:12365)） | 仅**著作群**级（`_resolve_search_scope`，[app.py:11724](app.py:11724)） |
| AI 随心问 | 抽屉 `_ai_assistant.html` + 阅读器 | `/api/ai/search-chat`（[app.py:11859](app.py:11859)）→ `_build_chat_grounding` | 同上，仅群级（`marx-ai-scope-v2`） |
| 联想检索（未点名，同源受益） | index.html `data-mode="associative"` | 同研究端点 | 与研究共用 `_resolve_search_scope` |

可复用设施：`_scoped_book_keys`（[search.py:532](search.py:532)，`None`=全库向后兼容）；联想/研究/接地全链路已吃 `book_scope`
（`locate_quote`/`keyword_cooccurrence`/`fragment_search`/`chapter_focused_search`/`locate_associative`/`locate_subject_index`）；
`_chapter_scope_books()`（[app.py:1892](app.py:1892)，已按著作群分组枚举 ~30 本已开放书库，篇章直达在用）；
`corpus.get_volumes(key)`（列某本的全部卷，供卷下拉）；`_search_book_counts`（[app.py:11280](app.py:11280)）。

---

## 2. 范围表示法（贯穿前后端的"约定"）

把范围统一表示成一个**"书→卷"规格**（下称 scope spec）：`{书库键: 允许卷号集合 或 None}`，
其中某本对应 `None`＝该本全部卷；整个 scope 为 `None`＝全库（向后兼容）。

**这张表天生就是"多本/多卷"的**：`{文集: None, 全集: {5,6}, 治国理政: None}` 就表示"《文集》全卷 + 《全集》第 5、6 卷 +
《治国理政》全卷"三者同时检索——底层逐卷循环照跑，无需为"多选"另加机制（D-多选）。

- 前端→后端的 token（**可传一个列表**）：`book:<键>`（整套）、`vol:<键>:<卷号>`（某一卷）。群仍用原 id（ascii，与中文书库键天然不撞）。
- 合并规则：同一列表里的多个 token 取**并集**；某本若同时出现 `book:<键>`（整套）与 `vol:<键>:<n>`，**整套优先**（该键置 `None`＝全卷）。
- `_scoped_book_keys` 接受"纯书库键集合"时按 `{键: None}` 处理（旧调用零改动）；新增 `_scoped_volumes(book, spec)`
  返回该本在范围内的卷列表，供内层 `for vol in …` 替换为 `for vol in self._scoped_volumes(book, spec)`。

---

## 3. 方案（分阶段）

### Phase 1 — 范围表示 + 研究/随心问/联想 打通到"卷级"（核心）

**3.1 `_resolve_search_scope` 扩词表**（[app.py:11724](app.py:11724)）→ 把 token 列表解析并**合并**成一个 scope spec：
- 现：`all/全部`→不限定；token∈群 id→群书库并集（手动硬限定）；否则 `_detect_scope` 自动。（该函数**已支持列表入参**，多选群本就在用。）
- 增：`book:<键>`（或裸合法书库键）→ 该键并入 `{键: None}`；`vol:<键>:<n>`→ 并入 `{键: {…n}}`；多 token 取并集，
  同键"整套"覆盖"某卷"（置 None）；`manual=True` 硬限定不回填。
- **D2 优先**：只要列表里出现任一 `book:`/`vol:` token，就**只取这些书/卷的并集、丢弃同时传来的群 id**。
- 配套：`_scope_label`（[app.py:11662](app.py:11662)）把 `book:<键>`/`vol:<键>:<n>` 渲染成「《书名》」「《书名》第 N 卷」；
  `_scope_options_payload`（[app.py:11672](app.py:11672)）在群选项后附「单本/单卷」清单（复用 `_chapter_scope_books` + `get_volumes`）。
- **研究/随心问/联想端点零改动**：它们已把 `scope` 透传进 `_resolve_search_scope`（[app.py:12432](app.py:12432)/[:11799](app.py:11799)），
  只要底层接地设施吃"卷级 scope"即生效。

**3.2 底层接地设施吃"卷级"**（[search.py](search.py)，把 `book_scope: Collection[str]` 泛化为可含卷约束的 scope spec）：
- 新增 `_scoped_volumes(book, spec)`；把逐卷循环处（`_exact_in_book`/`_fuzzy_in_book`，及
  `keyword_cooccurrence`/`_fragment_exact_hits`/`chapter_focused_search` 里的卷循环）换成按范围取卷。
- `_exact_in_book`/`_fuzzy_in_book` 增可选卷过滤参数（`locate_quote` 传入）。`locate_subject_index` 的 `scope_set`
  升级为可判卷（名目索引仅《文集》，选到非《文集》卷时自然为空）。
- **向后兼容**：spec 为 `None` 或纯书库键集合时，行为与今日**逐字一致**。

### Phase 2 — 标准检索（引文/模糊）接单本/单卷（走"后置过滤"，遵 D3）

- **不**把范围推进 `search_grouped`（那会丢掉全库分栏计数，违反 D3）；沿用现有"先全库出结果、`book_counts` 全库统计、
  再按选择过滤显示"的路子，**把过滤从"仅按单个 book"扩到"按一组 (book, 卷) 约束"**（即前端传来的 scope spec）：
  - `/api/search`（[app.py:11343](app.py:11343)）：接收 scope（多本多卷）；过滤处（[:11433](app.py:11433)）保留分组当且仅当
    其 book 在所选集合、且（该本无卷约束或其卷在允许集合）内。
  - `_chaptered_search_payload`（短词海量命中通道，[app.py:11302](app.py:11302)）、`search_cooccurrence_grouped`（同段多词）
    的结果过滤同样按此扩展——**三条分支一致**，避免同一查询因规模不同落到不同分支时行为不一。
  - `book_counts` 保持**全库**统计（D3）；点书库分栏 tab＝切换到那本（清掉当前多选/卷限定）。
- 说明：标准检索因此仍"全库扫描后过滤"，与今日一致（引文精确扫描很快；模糊扫描受 `_FUZZY_SCAN_SEMAPHORE` 护栏）。
  若日后要给模糊标准检索也省扫描，再单开"廉价全库计数 + 范围内细扫"的两遍方案，非本次。

### Phase 3 — 前端：统一「指定著作 / 卷」**多选**控件（四个界面）

支持 D-多选：可勾选多本、跨本挑卷。**控件形态＝方案①（D4 已定）**：

- **方案①·两个控件分工（已采纳）**：保留现有「大类」chips 作快捷；**新增一个"精选到书/卷"多选清单**
  （按著作群分组，每本一个复选框；多卷本旁一个可展开的「选卷」子清单，复选框 + 全选/全不选，单卷本不出）。
  按 D2，只要"精选清单"勾了任意书/卷，就以它（的并集）为准、大类 chips 让位。改动集中在新控件，现有 chips 行为不变。
- ~~方案②·层级多选树~~（未采纳：四界面改动大、动到已上线且有测试的 chips 逻辑）。

- **落地**：
  - **index.html**——标准/联想/研究三 tab 面板都露出该控件。标准模式发 `book[]`(+每本 `volume[]`) 或统一发 `scope=[…]` token 列表；
    联想/研究发 `scope=["vol:全集:5","vol:全集:6","book:文集"]` 这样的**列表**。
  - **`_ai_assistant.html` + static/ai-assistant**——随心问抽屉，发 `scope` 列表。
  - **viewer.html / wenku_reader.html**——阅读器 AI「检索原著原文」旁，发 `scope` 列表（同一 search-chat 端点）。
- **记忆**：`marx-search-scope-v2`/`marx-ai-scope-v2` 的 schema 扩为可存多本多卷（`{mode, selected[], books:{键:[卷…]|"all"}}`）。
- **回显**：scope banner 显示「范围：《文集》、《全集》第 5·6 卷 · 手动指定」（多本顿号连接）。静态 `?v=` +1。

---

## 4. 测试

- `ScopeRoutingTests`（tests/test_associative_search.py）：`book:文集`→`{文集:None}`；`vol:文集:5`→`{文集:{5}}`；
  **多选并集** `["vol:全集:5","vol:全集:6","book:文集"]`→`{全集:{5,6}, 文集:None}`；同键"整套"覆盖"某卷"；
  同时给群 id + `book:` → 只剩指定书/卷（D2）；裸键兼容；非法键/卷→回落。
- search.py 卷级：`_scoped_volumes` 只返回范围内卷；`locate_quote(..., 卷限定)` 命中全在该卷。
- `/api/search` 带 `book`+`volume`：结果全在该卷；`book_counts` 仍是全库（D3）；不带则与今日逐字等价（回归护栏）。
- 端点：research / search-chat 传 `scope=vol:治国理政:2` → 命中/接地全在该卷。
- 前端：三 tab + 抽屉 + 两阅读器 render 200 且含著作/卷 select；卷 select 联动逻辑 `node --check` 过。

## 5. 部署（沿用范围化清单，血泪坑复述）

- `cloud_patch_files.txt`/`cloud_compile_files.txt` 临时窄化到本次文件（app.py + search.py + 4 模板 + static/ai-assistant），
  try/finally **字节备份还原**勿 `git checkout`；compile 清单**≥2 行**避 splat 坑。
- 包裹脚本**内联 ASCII + 相对路径**，勿写含中文路径的无 BOM .ps1（PS5.1 GBK 秒崩）。静态 `?v=` +1。
- 服务器侧 `grep` 确认字节落地 + `localhost` curl（`mazhumonitor` UA 豁免反爬）核验端点；`update_cloud` **不验 /viewer**，
  改阅读器须自查（test_client 断言 markers，且**保留**「解释本页内容」按钮不受影响）。

## 6. 风险 / 边界

- 标准检索三条分支（普通分组 / 短词 chaptered / 同段多词）**必须同步**扩到卷级过滤，否则不一致。
- 空范围（选了不在库的本/卷）→ `_scoped_volumes`/`_scoped_book_keys` 保证回落，不会搜出空。
- 研究/随心问硬限定单卷时**不回填**（尊重用户选择），条数可能少于全库自动路由——与既有「手动=不回填」一致。
- 向后兼容：老前端不传 `book`/`volume`/单本 scope → 行为**逐字不变**。

---

## 7. 改动清单速览

| 文件 | Phase | 改动 |
|---|---|---|
| app.py `_resolve_search_scope`/`_scope_label`/`_scope_options_payload` | 1 | 认 `book:<键>`/`vol:<键>:<n>` **token 列表**，并集合成 scope spec；D2 优先 |
| search.py `_scoped_volumes`(新) + `_exact_in_book`/`_fuzzy_in_book`/共现/片段/篇章/名目索引 卷循环 | 1 | 逐卷循环按范围取卷；`book_scope` 泛化为 scope spec，纯键集合向后兼容 |
| app.py `/api/search`(+`_chaptered_search_payload`) 与 `search_cooccurrence_grouped` 结果过滤 | 2 | 接收多本多卷 scope，三分支后置过滤按 (book,卷) 集合；`book_counts` 仍全库（D3） |
| templates/index.html（+CSS/JS） | 3 | 三 tab 加**多选**「著作 + 卷」控件（D4 定形态）；标准发 `book[]`/卷、AI 发 `scope` 列表 |
| templates/_ai_assistant.html + static/ai-assistant/* | 3 | 抽屉加多选著作/卷控件（`scope` 列表） |
| templates/viewer.html / wenku_reader.html | 3 | 阅读器 AI 加多选著作/卷控件（`scope` 列表） |
| tests/test_associative_search.py | 1–2 | 扩 ScopeRoutingTests（含多选并集）+ 卷级过滤/端点测试 |
