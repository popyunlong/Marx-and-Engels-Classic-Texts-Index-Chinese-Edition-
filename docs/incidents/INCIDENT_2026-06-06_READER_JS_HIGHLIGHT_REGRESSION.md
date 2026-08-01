# 2026-06-06 阅读器前端失效复盘

## 摘要

2026-06-06，网页端上线“搜索结果正文高亮”和“篇章直达排序”修复后，阅读器页面出现严重前端失效：正文书页区域只显示空白框，上下页按钮无反应，目录跳转无反应。后续排查确认，问题由新增的前端常量 `highlightText` 与阅读器既有函数 `highlightText()` 重名引起，浏览器执行脚本时报出重复声明语法错误，导致整段阅读器 JavaScript 没有启动。

已通过将新增常量改名为 `highlightQueryText` 修复，并于同日重新部署上线。线上 `/api/runtime` 与阅读器页面均已验证恢复。

## 影响范围

- 受影响页面：`/viewer` 阅读器页面。
- 受影响功能：
  - 书页图片无法被前端加载到页面中。
  - “上一页 / 下一页”按钮失效。
  - 目录内篇章点击失效。
  - 阅读器内依赖同一脚本的交互功能整体不可用。
- 未受影响：
  - 后端服务未 500。
  - `/page-image` 图片接口可正常返回 JPEG。
  - `/api/runtime` 正常。
  - 语料库与 PDF 文件未被修改。

## 时间线

- 首次修复上线：通过 `deploy/update_cloud.ps1` 上传，备份为 `/opt/marx-search.cloud-backup.20260606-100913`。
- 用户反馈：阅读器打开后只剩空白书页区域，上下页与目录均无反应。
- 排查确认：
  - 线上阅读器 HTML 返回 200。
  - `/page-image` 返回 200，且图片文件本身正常。
  - 抽取阅读器内联脚本做语法检查，发现 `Identifier 'highlightText' has already been declared`。
- 紧急修复：
  - 将新增常量 `highlightText` 改名为 `highlightQueryText`。
  - 更新相关前端引用和测试断言。
- 热修部署：通过 `deploy/update_cloud.ps1` 上传，备份为 `/opt/marx-search.cloud-backup.20260606-102021`。
- 线上验证：
  - `/api/runtime` 返回 200。
  - 线上阅读器脚本中已不存在 `const highlightText`，仅保留 `const highlightQueryText` 与既有 `function highlightText(...)`。

## 根因

根因是前端变量命名冲突：

- 阅读器模板中原本已有函数 `highlightText(text, terms)`，用于 OCR 文本黄色高亮。
- 本次为了让正文页支持独立的高亮参数 `h`，新增了：
  - `const highlightText = {{ highlight_text | ... }}`
- 在同一个脚本作用域内，`const highlightText` 与 `function highlightText()` 同名，触发 JavaScript 语法错误。
- 语法错误发生在脚本解析阶段，因此后续所有事件绑定和图片加载逻辑都没有执行。

这类问题不会被 Python 语法检查发现；仅访问页面 HTML 也会显示 200，必须检查前端脚本或实际浏览器行为才能发现。

## 已完成修复

- 后端：
  - `/viewer` 与 `/page-image` 增加可选参数 `h`，缺省时回退到 `q`。
  - 搜索结果 `viewer_url` 携带 `q` 和 `h`，其中 `h` 来自命中上下文的实际标注文本。
- 前端：
  - 将新增高亮查询常量命名为 `highlightQueryText`。
  - 翻页、目录跳转、书页图片 URL、OCR 文本高亮均继续传递和使用该高亮查询值。
- 篇章直达：
  - 结果按书库配置顺序优先排序：马恩文集、马恩全集、列宁全集。
  - 对完整篇名命中做温和收敛，减少附属项挤占。

## 验证情况

已执行并通过：

- `python -m pytest tests/test_security.py tests/test_deploy_manifests.py`
- `python scripts/deployment_smoke.py --mode server`
- `deploy/update_cloud.ps1 -DryRun`
- `deploy/update_cloud.ps1`
- 热修后定向回归：
  - 阅读器高亮参数测试通过。
  - 阅读器模式基础测试通过。
  - 云端 `/api/runtime` 返回 200。
  - 线上阅读器脚本确认变量名不再冲突。

## 暴露的问题

1. 部署前验证偏后端，缺少前端脚本语法检查。
2. 只验证了页面 HTTP 状态，没有验证阅读器关键交互是否能启动。
3. 新增前端变量未做命名冲突检查，且阅读器内联脚本较长，人工扫描不可靠。
4. 回归测试里检查了 `h` 参数存在，但没有检查渲染后的脚本是否可解析。

## 后续改进

建议补上以下防线：

- 在部署 smoke test 中增加阅读器模板渲染后脚本抽取与 `node --check` 检查。
- 增加至少一个阅读器端到端检查：
  - 打开 `/viewer`。
  - 确认书页图片元素加载了非空 `src`。
  - 确认“下一页”点击后页码变化。
  - 确认目录项点击后页码变化。
- 前端全局变量统一加命名前缀，例如 `readerHighlightQueryText`，减少与函数名冲突。
- 对重大阅读器改动，部署后必须验证：
  - `/api/runtime`
  - `/viewer`
  - `/page-image`
  - 上下页
  - 目录跳转
  - 搜索结果进入正文高亮

## 结论

本次事故的直接原因是前端命名冲突，根本问题是部署前缺少阅读器 JavaScript 可执行性检查。修复本身较小，但影响面大，因为阅读器交互集中依赖同一段内联脚本。后续应把“渲染后的前端脚本语法检查”和“阅读器关键交互检查”纳入部署前固定流程，避免后端健康但前端瘫痪的情况再次出现。
