# 篇章范围检索与文献 API 独立入口交接

## 代码与使用

- 基线：`8726f2311ce8dcf9a2a6c3d1ea537152e06d7e18`；实施前与交付前核查远端 main 均为此提交。
- 专用分支：`codex/chapter-scope-api-20261110`。
- 实际工作树：`D:/CodexData/workspaces/chapter-scope-api-20261110`。后续操作须显式使用此路径。桌面工具创建的 C 盘附件路径不是开发目录；移动后工具未能把 D 盘路径注册为 managed worktree。
- 网页检索的“指定著作”面板可展开卷册目录，筛选篇名并多选篇章、子节。父篇章包含子节；整本或整卷覆盖其下篇章；同页不同篇章用独立 document_id 区分。公共篇章与个人文库不混选，个人文库保留原整本检索。
- `/api-access` 为独立“文献 API”栏目，主导航、移动导航、会员功能页和账户中心均有入口。访客可阅读说明，会员按现有权限管理 Key。

## 接口与数据边界

- 新增 `GET /api/search/chapters?book=文集&volume=1`，返回篇章标题、层级、页码、可选状态、原因、`chapter:<document_id>` token 与 `scope_version`。目录按卷懒加载。
- 网页请求仍使用 `scope` 数组。出现 chapter token 时必须提交对应 `scope_version`；版本变化或检索期间文库撤回返回 409，不扩大范围。未知、不可访问或不能定位边界的目录项拒绝使用。
- 边界复用现有规范化文本坐标，额外要求目录标题或足够长的标题前缀能在指定页唯一定位。不能确认起止的项目禁用，本次不修复或重建全库目录。
- 精确／共现计数、惰性分页、Word/HTML 导出共用 `ChapterQuery`；近似与联想沿用现有检索器，传入显式文档区间并裁剪正文。排版投影也按原文区间扫描，并将区间纳入缓存键，避免范围外命中耗尽候选预算。
- 导出沿用现有 `scope_json` 字段，篇章任务保存 `{tokens, version, book_filter?}` 快照；书库分栏的临时收窄也写入快照及复用指纹。导出内容版本提升至 `chapter-boundaries-v4`，旧 worker 不可领取新语义任务。数据库无结构迁移。
- 历史记录、刷新恢复、篇章展开请求都保留范围和版本，包括零结果记录。清除选择时刷新已打开目录，旧目录版本不能自动转成全库查询。
- AI 对话、阅读器问答、网页 Agent、REST/MCP 暂不接收篇章 token。`agent_api` 权限键、Key 数据、REST `/api/v1` 和 MCP `/mcp` 不变；旧 `/account/agent-api` GET 跳转，POST 共用原管理逻辑与 CSRF。

## 验收证据与适用范围

完整记录在 `CHAPTER_SCOPE_API_TEST_LEDGER_20261110.json`，每阶段保存命令、源码哈希、开始结束时间、退出码和日志。

| 阶段 | 结果 | 适用范围 |
| --- | --- | --- |
| backend | 89 passed | 首次功能版本；新增篇章用例与直接相关 API、导出既有回归 |
| browser | 3 passed | 首次页面版本；真实首页请求／分页／刷新、移动选择器、API 页面及导航 |
| edges | 7 passed | 后续排版区间预算、缓存隔离、检索期间版本／公开状态变化，以及此前未运行的 layout-index 用例 |
| directory-edge | 1 passed | 清除旧选择后刷新已打开目录并采用新版本 |
| export-history-edge | Python 1 passed、Node 1 passed | 分栏导出的篇章交集、空结果历史保留范围和版本 |
| static | 见台账 | 单次 diff whitespace、新 JS 语法和 CI YAML 解析 |

产物目录：`D:/CodexData/outputs/chapter-scope-api-20261110`。包括 backend/edges/export-filter JUnit XML、各阶段日志，以及首页桌面、移动篇章面板、API 桌面和移动页面 PNG。截图来自首次浏览器验收；后续仅把 REST 示例中的 HTML 实体占位符换为 `YOUR_API_KEY`。

浏览器首次测试本身通过并已写入台账；包装脚本在最后向 GBK 控制台回显 Unicode 勾号时异常退出。原日志保留，包装器异常单独记录，随后修复输出编码；没有重跑这三个浏览器场景。

严格未重复运行任何已执行场景。初验之后的代码修复由上述新增边界场景覆盖，初验套件未在最终源码上整体重跑，因此不能将初验结果称为最终版本完整回归通过。未执行生产语料验收、性能矩阵、耐久测试、完整 CI、发布演练或生产门禁。模型相关测试使用本地语料及确定性检索，不调用付费模型。

## 发布与回退

- 已将新增用例纳入现有 CI 配置，但本任务不推送或触发 CI。下一次推送前先审查台账；如果推送钩子、CI 或发布门禁会重复本次场景，须取得专项复测授权。
- 本地提交供代码审阅。合入 main、推进 production 和正式发布由指定发布协调者处理；仅可从干净且与 origin/production 完全一致的 production 使用 `deploy/release.ps1`，持有 `/run/lock/marx-search-release.lock`。
- 发布前需完成相关剩余门禁、实际目录可选率与代表性真实篇章抽查；不能用合成语料结果替代。
- 无语料／索引重建、Key 迁移或服务拓扑变更。回退仍用 `deploy/rollback_release.ps1`，保留共享 API 与导出数据库；导出版本门禁负责隔离不兼容任务。

## 本机存储

复用既有 Python、Node 和 Edge，无依赖下载。临时测试目录位于 `D:/CodexData/temp/chapter-scope-api-20261110`，结果在上述 D 盘产物目录；测试自动移除了其隔离运行库，保留 pytest 场景证据与必要产物。

工作树工具最初在 C 盘创建副本，跨盘 Git move 失败后，复制到 D 盘并逐文件 SHA-256 校验，校验字节数 130,700,811（约 125 MiB），随后修复 Git worktree 指向。自动审批两次拒绝删除本任务 C 盘重复副本，理由仅为 `blocked by policy`，因此没有宣称释放 C 盘空间，也没有绕过拦截。原副本保留在 `C:/Users/10108/.codex/worktrees/chapter-scope-api-20261110/【增强】马恩《文集》《全集》检索`，不得作为另一个开发工作树使用。
