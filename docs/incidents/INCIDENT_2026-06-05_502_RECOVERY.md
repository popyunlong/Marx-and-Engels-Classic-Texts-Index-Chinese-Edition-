# 2026-06-05 网站 502 事故复盘与防复发措施

## 一、事故结论

2026-06-05 深夜，网站在一次云端更新后出现 502。最终确认：前端代理服务仍在运行，但后端 Python 网站服务 `marx-search` 因启动失败无法监听 `127.0.0.1:8000`，所以公网访问返回 502。

直接错误是：

```text
ModuleNotFoundError: No module named 'book_config'
```

根因是新版本 `app.py` 引用了新文件 `book_config.py`，同时依赖新配置 `config/books.yaml`，但当时的云端增量上传脚本没有把这两个新增文件纳入上传清单。部署后服务重启，后端进程导入失败并反复退出。

## 二、影响范围

- 影响对象：公网网站访问用户。
- 表现：访问网站时出现 502。
- 后端状态：`marx-search.service` 进入反复自动重启状态。
- 数据影响：未发现数据库、PDF、语料索引损坏；属于程序文件缺失导致的启动失败。
- 恢复方式：补传缺失文件、修正权限、重启服务、验证公网 200。

## 三、关键时间线

以下时间以北京时间为主：

- 2026-06-05 23:40 左右：进行云端更新。
- 2026-06-05 23:43 左右：首页模板文件本地有最近改动记录。
- 2026-06-05 23:54 至 23:56：云端服务日志持续出现 `ModuleNotFoundError: No module named 'book_config'`，服务无法启动。
- 2026-06-05 23:56 左右：补传 `book_config.py`，随后发现 `config/books.yaml` 也未在云端。
- 2026-06-05 23:58 左右：补传 `config/books.yaml`，修正权限并重启服务。
- 2026-06-05 23:59 左右：本机后端和公网域名均返回 `200 OK`，网站恢复。

云端日志使用 UTC 时间，日志中的 `15:54-15:59 UTC` 对应北京时间 `23:54-23:59`。

## 四、技术根因

### 1. 增量上传清单漏掉新增运行文件

部署脚本 `deploy/update_cloud.ps1` 使用固定文件清单 `$files` 打包上传。新开发中新增了：

- `book_config.py`
- `config/books.yaml`

但它们没有同步进入上传清单。结果是本地运行正常，云端缺文件。

### 2. 语法编译检查不足以发现导入缺失

部署脚本原有检查包含：

```text
python -m py_compile app.py ...
```

这个检查只能发现 Python 语法错误，不能发现运行导入链上的缺失模块。`app.py` 即使引用了云端不存在的 `book_config.py`，单纯编译仍可能通过。

### 3. 重启前缺少服务器模式导入冒烟测试

这次问题只有在后端服务真正启动、执行 `import app` 时才暴露。部署流程当时没有在重启前执行：

```text
APP_MODE=server python -c 'import app'
```

因此缺失模块直接进入生产重启阶段，造成服务中断。

### 4. 当前部署方式是就地覆盖

脚本会直接覆盖 `/opt/marx-search` 中的程序文件。就地覆盖的优点是简单、快；缺点是如果重启失败，线上服务会依赖自动重启和人工恢复，缺少更优雅的预发布验证与回滚切换。

## 五、已完成的修复措施

### 1. 已恢复生产服务

已在云端补传：

- `/opt/marx-search/book_config.py`
- `/opt/marx-search/config/books.yaml`

并执行：

- 修正文件权限为 `www-data:www-data`
- 重启 `marx-search`
- 验证 `http://127.0.0.1:8000/api/runtime`
- 验证 `https://mazhuzuojiansuo.com/`

最终公网返回 `200 OK`。

### 2. 已修正增量上传清单

`deploy/update_cloud.ps1` 已加入：

- `book_config.py`
- `config/books.yaml`

以后通过该脚本更新云端时，会自动带上这两个文件。

### 3. 已增加重启前导入冒烟测试

`deploy/update_cloud.ps1` 已增加服务器模式导入检查：

```powershell
Write-Host "Running server import smoke test before restart ..."
Invoke-Remote "cd '$RemoteDir' && . .venv/bin/activate && python scripts/deployment_smoke.py --mode server"
```

这个检查放在重启服务之前。如果再出现类似“新增模块未上传、依赖缺失、启动导入失败”的问题，部署脚本会在重启前停止，避免把生产服务重启到不可用状态。

### 4. 已新增自动化冒烟测试脚本

已新增 `scripts/deployment_smoke.py`，自动检查：

- 所有 `templates/*.html` 能被 Jinja 解析。
- `APP_MODE=server` 下可以正常导入 `app`。
- Flask 测试客户端访问 `/`、`/api/runtime`、`/pricing` 均返回预期状态码。

该脚本已经接入 `deploy/update_cloud.ps1`：

- 本地打包前先执行一次，防止把明显不可启动的代码打包上传。
- 云端解压后、重启生产服务前再执行一次，防止云端缺文件或缺依赖。

本次已验证：

```text
python scripts\deployment_smoke.py --mode server
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\update_cloud.ps1 -DryRun
```

两项均已通过。

### 5. 已加入失败自动回滚入口

`deploy/update_cloud.ps1` 现在会捕获刚创建的轻量备份路径。如果重启后的健康检查失败，脚本会自动尝试：

- 从本次部署前的轻量备份恢复程序文件。
- 重启 `marx-search`。
- 再次检查 `/api/runtime`。

这不能替代完整发布系统，但已经能覆盖这次事故类型：程序文件更新后服务无法恢复。

## 六、防止类似事故的长期措施

### 1. 上传清单必须覆盖所有运行时依赖

新增 Python 文件、模板、配置、脚本后，必须同步检查部署脚本的上传清单：

- `deploy/update_cloud.ps1` 的 `$files`
- `deploy/update_cloud.ps1` 的 `$compileFiles`
- 其他全量上传脚本中的文件列表

新增文件如果会被 `app.py` 或服务启动链引用，必须视为生产运行文件，不能只留在本地。

### 2. 部署前必须执行三类检查

每次云端更新前至少通过：

```text
python -m py_compile app.py
```

```text
APP_MODE=server python -c "import app; print('server import ok')"
```

```text
首页、/api/runtime、核心业务页本地或测试环境返回 200
```

语法检查、导入检查、页面检查各管一类问题，不能互相替代。

### 3. 部署后必须做内外两层健康检查

内层检查：

```text
curl -fsS http://127.0.0.1:8000/api/runtime
systemctl is-active marx-search
```

外层检查：

```text
curl -I https://mazhuzuojiansuo.com/
curl -I https://mazhuzuojiansuo.com/library
```

内层 200 只能说明后端可用；外层 200 才说明代理、域名、TLS、后端链路整体可用。

### 4. 建立“新增文件上线”清单

每次上线前人工确认：

- 是否新增 `.py` 文件
- 是否新增 `config/*.yaml`
- 是否新增模板或静态资源
- 是否修改 `requirements.txt`
- 是否修改 systemd、Caddy、定时任务
- 是否修改数据库结构或初始化逻辑

只要答案为“是”，就必须确认部署脚本、权限、重启顺序、回滚方式。

### 5. 保留快速回滚路径

部署前已有轻量备份目录，后续应形成明确回滚命令：

```text
systemctl stop marx-search
从最近的 /opt/marx-search.cloud-backup.* 恢复程序文件
systemctl start marx-search
curl -fsS http://127.0.0.1:8000/api/runtime
```

目标是在 5 分钟内从程序文件级事故中恢复。

### 6. 引入更安全的发布方式

中期建议把就地覆盖改为“候选目录 + 验证 + 切换”：

```text
/opt/marx-search/releases/20260605-xxxx
/opt/marx-search/current -> releases/当前版本
```

流程：

1. 上传到新 release 目录。
2. 在新目录运行编译、导入、页面冒烟测试。
3. 测试通过后切换 `current`。
4. 重启服务。
5. 失败时把 `current` 指回上一版。

这样可以显著降低部署时把线上目录覆盖坏的风险。

### 7. 增加自动告警

建议对以下条件设置告警：

- 公网首页连续 2 次非 200。
- `/api/runtime` 连续 2 次失败。
- `marx-search.service` 进入 failed 或高频重启。
- Caddy 日志中 502 激增。

告警渠道可先用邮件，后续再接入更实时的通知方式。

## 七、以后遇到 502 的应急手册

### 第一步：判断是代理问题还是后端问题

在服务器上检查：

```text
systemctl is-active marx-search
curl -i --max-time 5 http://127.0.0.1:8000/api/runtime
```

如果本机 8000 端口连接失败或非 200，大概率是后端服务问题。

### 第二步：看最近服务日志

```text
journalctl -u marx-search -n 120 --no-pager
```

优先搜索：

- `ModuleNotFoundError`
- `ImportError`
- `SyntaxError`
- `PermissionError`
- `OperationalError`
- `Address already in use`

### 第三步：按错误类型处理

如果是缺模块：

```text
补传缺失 .py 文件
chown www-data:www-data 缺失文件
chmod a+r 缺失文件
```

如果是缺配置：

```text
补传 config 文件
chown www-data:www-data config/对应文件
chmod a+r config/对应文件
```

如果是权限：

```text
chown -R www-data:www-data 必需目录
chmod -R a+rX templates scripts deploy config
```

### 第四步：重启并验证

```text
systemctl restart marx-search
systemctl is-active marx-search
curl -fsS http://127.0.0.1:8000/api/runtime
curl -I https://mazhuzuojiansuo.com/
```

只有公网返回 200，才算恢复完成。

## 八、上线前检查表

每次执行云端更新前，按这个顺序检查：

- 本地 `app.py` 能通过语法检查。
- 本地模板能被 Jinja 解析。
- 本地 `APP_MODE=server` 能导入 `app`。
- 新增文件已加入部署脚本上传清单。
- 新增 Python 文件已加入编译清单。
- 新增配置文件已加入权限处理范围。
- 云端更新后 `/api/runtime` 返回 200。
- 云端更新后公网首页返回 200。
- 如更新会员、支付、AI、阅读器功能，至少抽测一个核心入口。

## 九、此次事故的教训

这次事故不是代码逻辑本身复杂，而是部署链条漏了“新增文件完整性”和“启动导入验证”两个关口。以后不能只确认本地能跑，也不能只依赖语法编译。生产环境真正需要的是：上传完整、权限正确、服务能导入、内外健康检查都通过。

最关键的防线已经补上：部署脚本现在会上传缺失文件，并在重启前实际导入服务器模式下的应用。后续再把发布方式升级为候选目录验证和快速回滚，就能把类似 502 事故的概率和恢复时间都压下来。
