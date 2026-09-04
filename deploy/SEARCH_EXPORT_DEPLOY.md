# 首页引文汇编：隔离发布步骤

本功能默认关闭。不得从当前混合工作区直接运行宽泛的 `update_cloud.ps1`；发布包必须从实时生产目录的只读副本开始，只合并 `search_export_release_manifest.txt` 中列出的功能改动。

1. 在生产机用 `capture_search_export_preconditions.sh` 记录实时文件 SHA-256；同时复制同一时点的生产代码作为候选基线。
2. 在候选基线上合并并复核本功能，使用 `scripts/build_search_export_patch.py` 生成归档；该工具会拒绝绝对路径、`..`、重复项、符号链接和白名单外文件，并复读归档确认它与 `search_export_release_manifest.txt` 逐项一致。
3. 上传前再次运行 `verify_search_export_preconditions.sh`。任一文件哈希变化都必须中止并重新取基线，禁止强行覆盖。
4. 使用现有 `stage_release.sh` 创建独立候选目录；把 `deploy/search-export-requirements.txt` 安装到独立的 `/opt/marx-search-export-deps`，不得改动主站 `.venv`。以该目录加入 `PYTHONPATH` 后编译并运行三个导出测试文件。启动候选前先以网站服务账号执行 `sudo -u www-data -H ... scripts/search_export_control.py disable`，不能只依赖默认值；并对本发布涉及的 shell 脚本执行 `bash -n`。
5. 在 8001 启动候选站点，完成首页、检索、阅读器、会员、支付、AI 和后台健康检查。随后在独立工作节点创建真实小任务，并用 LibreOffice 渲染生成的 DOCX。
6. 先运行 10,000 条候选负载门禁；若触发资源暂停或网页 p95 回退超过 10%，不得开放 10,000 条档位。本次门禁已按此规则把会员上限收紧为 5,000 条，并把 1,001 条以上任务的分册大小收紧为 500 条；1,000 条以内仍为单文件。必须重新通过 5,000 条门禁：零 5xx/超时、网页 p95 回退不超过 10%、主站 RSS 增量不超过 100MB、工作节点峰值低于 1.8GB 且不超过一核，才允许继续。
7. 仅在以上门禁全部通过后，以 `MARX_EXTRA_PYTHONPATH=/opt/marx-search-export-deps` 和 `MARX_SKIP_DEP_INSTALL=1` 调用候选中的 `zero_downtime_restart.sh` 蓝绿切流，确保脚本不会安装或改写主站 `.venv`。安装更新后的工作节点 unit 后执行 `systemctl daemon-reload` 并仅重启该工作节点。
8. 最后以网站服务账号执行 `sudo -u www-data -H ... scripts/search_export_control.py enable` 动态开启。异常时同样以该账号执行 `disable`，再回退代码；关闭开关不需要重启主站。切勿用 root 直接运行控制脚本，否则会写入 root 自己的应用数据目录。

导出数据库与成品目录位于应用数据目录下的 `search_exports.sqlite3` 和 `search_exports/`，不参与会员库、语料库或检索索引迁移。
