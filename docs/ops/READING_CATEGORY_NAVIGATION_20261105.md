# 阅读栏目分类导航恢复（2026-11-05）

恢复 2026-10-05 本地预览中的左侧书库分类与右侧著作书架。分类直接来自当前服务端分组；移动端使用下拉框；不保存跨访问筛选；无 JavaScript 时所有书架仍可访问。保留现有分卷、目录与阅读入口。

用户荐书卡片不再重复显示 OCR 质量说明；保留书目与推荐人信息，上传介绍和提交反馈精简为短句。元数据原件不改动。

基线：a39630f17f5e4139e09518d31a4f19ade1e849f2。
本次验证产物：D:/CodexData/outputs/reading-category-navigation-20261105。
- browser-results.json：1440/1024/390 分类、键盘、抽屉与无 JS 降级通过；使用隔离语料，不能代替生产候选真实书目验收。
- fast-tests.log：38 passed。
- integration-tests.log：排除已执行的四个快速测试文件，只覆盖其余测试；结论以最终日志为准。

当前尚未发布；发布必须从干净且与 origin/production 一致的 production 检出经 deploy/release.ps1 执行，锁内候选验收后切换。回退使用 deploy/rollback_release.ps1 并绑定当前及目标发布身份。
