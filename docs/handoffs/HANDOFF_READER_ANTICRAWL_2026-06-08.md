# 阅读器反爬与异常访客后台接手说明

日期：2026-06-08

## 当前目标
给网站增加阅读器访问审计、异常访客名单、后台一键封禁/解封，以及不明显影响正常阅读体验的温和防御。

## 已完成的改动
- `membership.py`
  - 新增 `reader_access_events` 表，用于保存阅读器访问审计。
  - 新增阅读器访问写入函数 `record_reader_access_event(...)`。
  - 新增异常访客聚合查询 `list_reader_anomaly_visitors(...)`。
  - 新增访客明细查询 `list_reader_access_events(...)`。
  - `get_admin_dashboard_metrics(...)` 现在会带出 `reader_anomaly_visitors` 和 `reader_anomaly_count`。
- `app.py`
  - 接入阅读器访问审计的 `before_request` 钩子。
  - 加了阅读器资源的 `X-Robots-Tag: noindex,nofollow,noarchive`。
  - `robots.txt` 已禁止 `/reader`、`/library`、`/viewer`、`/page-image`、`/pdf`。
  - 新增后台接口：
    - `GET /admin/reader-access`
    - `POST /admin/reader-access-ban`
  - 新增阅读器封禁存储读取/写入辅助函数。
  - 总览上下文里已经把 `control_reader_access_url` 和 `control_reader_access_ban_url` 传给模板。
  - 已在总览里注入 `reader_anomaly_visitors` 的封禁状态标记。
- `templates/control.html`
  - 总览指标卡已经加入“阅读异常”。
  - 已新增“阅读器异常访问”表格区域。
  - 已新增异常访客明细弹层脚本。
- `tests/test_security.py`
  - 已加了三条针对阅读器异常/封禁的回归测试草稿。
  - 其中测试还没完全跑通，需要下一步继续修。
- `site_content.py`
  - 已修一个会阻断 `/admin` 渲染的兼容问题：`auto_literal_definitions` 不存在时走空列表，避免后台崩掉。

## 目前状态
- `python -m py_compile app.py membership.py site_content.py` 通过。
- 直接执行 `deploy/upload_reader_patch.ps1` 时，上传脚本先跑本地 smoke test，然后被本机环境里的 `node --check` 权限问题卡住，导致没有真正上传到服务器。
- 当前本地还留着一些未完成/待验证的测试和模板细节，尤其是阅读器异常表导出的历史列还没补完整。

## 还需要做的事
1. 继续把 `tests/test_security.py` 里的 3 条阅读器测试跑通。
2. 检查 `templates/control.html` 里新加的“阅读器异常访问”区块是否在真实 `/admin` 页面正常显示。
3. 补齐 `app.py` 里历史导出 Excel 的“阅读异常”列，如果需要的话同步更新导出表头和行数据。
4. 解决部署脚本的 smoke test 环境问题，或者临时给 `deploy/update_cloud.ps1` / `scripts/deployment_smoke.py` 加一个跳过本机 `node --check` 的安全开关，再重新上传。

## 重要线索
- 阅读器资源判定入口主要在 `app.py` 的：
  - `/_activity_feature_for_request`
  - `/_rate_limit_page_image_or_abort`
  - `serve_pdf`
  - `page_image`
  - `pdf_viewer`
- 后台总览数据源主要在：
  - `membership.py:get_admin_dashboard_metrics`
  - `app.py:_management_console_context`
  - `templates/control.html` 的总览区块
- 当前阅读器封禁策略是“账号优先，IP 次之”，目标是尽量不影响正常阅读。

## 建议下一个 AI 先做什么
1. 先打开 `tests/test_security.py`，把刚加的 3 条测试跑通。
2. 再检查 `app.py` 的历史导出列是否需要把 `reader_anomaly_count` 加进去。
3. 最后处理上传脚本的 smoke test 阻塞，然后重新跑 `deploy/upload_reader_patch.ps1`。

