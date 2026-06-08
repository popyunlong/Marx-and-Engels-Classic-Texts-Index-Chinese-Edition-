# 2026-06-06 列宁《全集》接入与安全上传交接文档

## 0. 最重要的安全背景

本轮工作发生在一次生产 502 事故之后。事故复盘文件是：

`D:/claudecode文件夹/【增强】马恩《文集》《全集》检索/INCIDENT_2026-06-05_502_RECOVERY.md`

事故直接原因：

- 云端部署后 `app.py` 引用了新增文件 `book_config.py` 和新增配置 `config/books.yaml`。
- 当时 `deploy/update_cloud.ps1` 的固定上传清单漏掉这两个文件。
- 服务重启后 `ModuleNotFoundError: No module named 'book_config'`，后端无法启动，公网返回 502。

因此，下一位 AI 继续时必须把以下规则作为硬约束：

- 不要直接“上传并重启”。
- 新增 `.py`、`config/*.yaml`、脚本、模板后，必须同步检查 `deploy/update_cloud.ps1` 的 `$files` 和 `$compileFiles`。
- 每次云端代码补丁必须先本地 `-DryRun`，并依赖 `scripts/deployment_smoke.py`。
- 重建大语料库必须显式进行，不要让普通代码补丁默认重建。
- 列宁 PDF 上传必须只上传 `pdfs/列宁《全集》`，绝对不要重新上传 `pdfs/文集` 和 `pdfs/全集`。

## 1. 用户最终目标

用户在 `pdfs/列宁《全集》` 中新增了 60 卷可检索 PDF，希望：

- 列宁《全集》进入网站端引文搜索引擎。
- 引文搜索命中后能定位并打开正文。
- 首页和阅读器中的“篇章直达”能搜索列宁篇目。
- `/reader` 和 `/library` 两个阅读器都能看到列宁《全集》，按卷分组，卷内展示目录，点击目录可看 PDF 渲染正文。
- 后续还可能继续新增更多资料，因此要留出配置驱动接口，不能只硬编码列宁。
- 云端大资料上传只增量上传列宁 PDF，不重新上传原来的马恩资料。

## 2. 当前已完成的工作

### 2.1 新增书库配置

已新增：

- `book_config.py`
- `config/books.yaml`

`config/books.yaml` 当前包含三个书库：

- `文集`
- `全集`
- `列宁全集`

每个书库包含：

- `key`
- `title`
- `short_title`
- `citation_title`
- `folder`
- `sort_order`
- `tag_class`

注意：

- 这些文件已经加入 `deploy/update_cloud.ps1` 上传清单。
- `book_config.py` 已加入 `deploy/update_cloud.ps1` 编译清单。

### 2.2 manifest 已加入列宁 60 卷

已运行：

```powershell
$env:PYTHONIOENCODING='utf-8'; python build_index.py --scan
```

结果：

```text
已写入 config/manifest.yaml，共扫描到 122 个 PDF。
```

已验证 `config/manifest.yaml` 数量：

```text
文集: 10
全集: 52
列宁全集: 60
```

这正好对应：

- 马恩文集 10 卷
- 马恩全集 52 个 PDF 文件
- 列宁全集 60 卷

### 2.3 volumes 已加入列宁出版年份

已修改：

- `config/volumes.yaml`

新增：

- `列宁全集: 1-60`

当前填写规则：

- 1-28 卷：`2013`
- 29-60 卷：`2017`

这是根据抽查 PDF 版权页得出的默认值。后续若需更精确，可逐卷核对版权页。

### 2.4 后端已初步配置驱动化

已修改：

- `runtime_env.py`
- `build_index.py`
- `search.py`
- `app.py`

已实现的方向：

- `runtime_env.load_allowed_source_files()` 不再只认 `文集/全集`，而是读取 manifest 所有书库。
- `build_index.py --scan` 使用 `book_config.load_book_configs()` 扫描所有配置书库。
- `build_index.py` 构建时遍历所有配置书库。
- `search.py` 使用 `load_book_configs()` 初始化动态书库。
- `search.py` 的 hit/group 返回增加：
  - `book_title`
  - `book_short_title`
  - `citation_title`
  - `book_sort_order`
- `search.py` 引文生成从 `book_config` 读取书名，列宁应生成类似：
  - `《列宁全集》第31卷，北京：人民出版社，2017年，第...页。`
- `app.py` 已新增动态书库辅助：
  - `BOOK_CONFIGS`
  - `BOOK_CONFIG_BY_KEY`
  - `book_stats`
  - `_book_config()`
  - `_book_sort_order()`
  - `_book_payload()`
- `/api/search` 的排序和结果字段已初步改为动态书库。
- `/reader`、`/library` 的 `_library_volumes()` 已按配置书库遍历。
- `/api/library/toc-suggest` 已按配置书库遍历，并返回动态书名/tag 字段。

### 2.5 前端已部分动态化

已修改：

- `templates/index.html`
- `templates/library.html`

已完成：

- 首页统计从固定 `文集/全集` 改为遍历 `book_stats`。
- 首页搜索结果显示改为优先使用后端返回的 `book_title` / `book_short_title`。
- 首页篇章直达标签改为使用 `tag_class` 和动态书名。
- 阅读页 `library.html` 标题、篇章直达提示、书库分组标题初步改为动态。
- `library.html` 下拉篇章搜索 JS 已改为使用后端 `book_short_title/book_title/tag_class`。

### 2.6 新增通用目录构建脚本

已新增：

- `scripts/build_toc.py`

作用：

- 通用写入 `toc_entries`。
- 默认构建 `列宁全集`。
- 可用：

```powershell
python scripts/build_toc.py --book 列宁全集
```

或：

```powershell
python scripts/build_toc.py --all
```

设计选择：

- 列宁 PDF 书签质量较好，通用脚本优先复用 `search.Corpus.get_toc_entries()`，主要吃 PDF 书签。
- 马恩文集和马恩全集现有精修脚本仍保留：
  - `scripts/build_wenji_toc.py`
  - `scripts/build_quanji_toc.py`

### 2.7 部署脚本安全改造

已修改：

- `deploy/update_cloud.ps1`
- `deploy/upload_reader_patch.ps1`

已新增：

- `deploy/upload_lenin_pdfs.ps1`

`deploy/update_cloud.ps1` 当前状态：

- `$files` 已包含：
  - `book_config.py`
  - `build_index.py`
  - `deploy/upload_lenin_pdfs.ps1`
  - `scripts/build_toc.py`
  - `config/books.yaml`
  - `config/manifest.yaml`
  - `config/volumes.yaml`
- `$compileFiles` 已包含：
  - `book_config.py`
  - `build_index.py`
  - `scripts/build_toc.py`
- 权限修复已包含：
  - `config/books.yaml`
  - `config/manifest.yaml`
  - `config/volumes.yaml`
- 新增 `-RebuildCorpus` 开关：
  - 不带 `-RebuildCorpus` 时，不重建 `corpus.sqlite`。
  - 带 `-RebuildCorpus` 时才执行：

```bash
python build_index.py
python scripts/build_wenji_toc.py
python scripts/build_quanji_toc.py
python scripts/build_toc.py --book '列宁全集'
```

这点很重要：普通代码补丁不再默默重建大库。

`deploy/upload_lenin_pdfs.ps1` 当前状态：

- 只上传本地 `pdfs` 下“恰好有 60 个 PDF 的目录”，当前识别为 `pdfs/列宁《全集》`。
- 不硬编码中文路径字面量，避免 PowerShell 编码把中文路径读坏。
- 上传前要求本地恰好找到一个 60-PDF 目录，否则拒绝执行。
- 逐文件检查远端同名同大小文件，存在则跳过。
- 只上传到：

```text
/opt/marx-search/pdfs/列宁《全集》
```

- 上传后修正权限。
- 上传后远端验证 PDF 数量为 60。
- 不重启服务。
- 不改数据库。
- 支持 `-DryRun`。

## 3. 已完成的本地验证

### 3.1 Python 编译通过

已运行：

```powershell
python -m py_compile book_config.py build_index.py search.py app.py scripts\build_toc.py scripts\deployment_smoke.py
```

结果：通过。

### 3.2 代码补丁 dry-run 通过

已运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\update_cloud.ps1 -DryRun
```

结果：

```text
Running local deployment smoke test ...
deployment smoke ok: mode=server
Preparing cloud patch package ...
Patch package ready: 0.35 MB
Dry run complete. No cloud connection was made.
```

注意：dry-run 日志里仍显示：

```text
Loaded corpus: 文集=10 全集=50 列宁全集=0
```

这说明当前 `data/corpus.sqlite` 尚未重建，数据库里还没有列宁页数据；`全集=50` 是当前运行库的状态，manifest 已有 52 个马恩全集 PDF，但数据库仍是旧状态。

### 3.3 列宁 PDF 上传脚本 dry-run 通过

已运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\upload_lenin_pdfs.ps1 -DryRun
```

结果：

```text
Lenin PDF upload plan: 60 files, 4 GB
Remote target: root@38.76.174.234:/opt/marx-search/pdfs/列宁《全集》
Dry run: no cloud connection or upload will be made.
```

这说明脚本本地识别列宁 60 卷正常。

## 4. 当前尚未完成的关键事项

### 4.1 尚未真正上传列宁 PDF

还没有执行非 dry-run 的：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\upload_lenin_pdfs.ps1
```

用户说“可以接续进行资料上传”，但又要求本轮停止并交接。所以当前没有连接云端上传 PDF。

### 4.2 尚未重建本地 `data/corpus.sqlite`

当前数据库验证：

```text
pages:
全集 42472
文集 9321

toc_entries:
全集 8346
文集 1302
```

当前数据库还没有：

- `pages.book = '列宁全集'`
- `toc_entries.book = '列宁全集'`

下一步必须重建数据库后才能真正搜索列宁正文。

### 4.3 尚未验证 `scripts/build_toc.py --book 列宁全集`

脚本已新增、语法通过，但还没有在重建后的数据库上实际跑列宁目录写入。

### 4.4 尚未完成所有前端/文案扫尾

已改 `index.html` 和 `library.html` 的主要动态展示，但还需要下一位继续检查：

- `templates/viewer.html` 是否还有硬编码文案。
- `site_content.py` 里大量默认文案仍写“马恩《文集》《全集》”。
- `templates/library.html` 是否还有遗漏的 “文集阅读” 类文案。
- 首页功能栏文案可能仍偏向《马克思恩格斯文集》。

这些不是立即导致服务崩溃的问题，但影响最终体验。

### 4.5 尚未运行完整 pytest

目前只跑了 py_compile 和 deployment smoke dry-run。下一位应在完成收尾后跑：

```powershell
python -m pytest
```

若测试依赖生产数据或耗时过长，要记录具体失败原因。

## 5. 下一位 AI 推荐的安全推进顺序

### 阶段 A：不要碰云端，先本地收口

1. 检查当前代码是否仍有硬编码书库判断：

```powershell
Select-String -Path app.py,search.py,build_index.py,templates\*.html,site_content.py -Pattern "文集\",\"全集|文集', '全集|book.*!=.*文集|== '文集'|马克思恩格斯{{ volume.book }}|搜索《文集》《全集》|马恩《文集》《全集》"
```

2. 修正必要的前端/文案遗留。

3. 重建本地索引。注意这可能耗时较长，因为列宁 60 卷约 4GB：

```powershell
$env:PYTHONIOENCODING='utf-8'
python build_index.py
python scripts/build_wenji_toc.py
python scripts/build_quanji_toc.py
python scripts/build_toc.py --book 列宁全集
```

4. 验证数据库：

```powershell
$env:PYTHONIOENCODING='utf-8'
@'
import sqlite3
conn=sqlite3.connect("data/corpus.sqlite")
print(conn.execute("select book,count(*) from pages group by book order by book").fetchall())
print(conn.execute("select book,count(*) from toc_entries group by book order by book").fetchall())
conn.close()
'@ | python -
```

期望至少看到：

```text
列宁全集 pages > 0
列宁全集 toc_entries > 0
```

5. 测试搜索列宁文本。可以从抽查中使用：

- `国家与革命`
- `农民生活中新的经济变动`
- `帝国主义`

示例：

```powershell
$env:PYTHONIOENCODING='utf-8'
python search.py "国家与革命"
```

6. 跑 smoke：

```powershell
python scripts\deployment_smoke.py --mode server
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\update_cloud.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\upload_lenin_pdfs.ps1 -DryRun
```

7. 视情况跑：

```powershell
python -m pytest
```

### 阶段 B：先上传列宁 PDF，不重启服务

这一步只上传资料，不改代码，不重启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\upload_lenin_pdfs.ps1
```

脚本会：

- 检查本地列宁 PDF 数量为 60。
- 创建远端 `/opt/marx-search/pdfs/列宁《全集》`。
- 逐文件跳过远端同名同大小文件。
- 上传缺失/大小不同文件。
- 修正权限。
- 验证远端 PDF 数量为 60。

如果这一步中断，可以再次运行同一命令；同名同大小文件会跳过。

严禁用 `deploy/upload_to_server.ps1` 做这一步，因为它会上传整个 `pdfs`，包括老的马恩资料。

### 阶段 C：上传代码补丁，但先不重建、不重启

先做：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\update_cloud.ps1 -SkipRestart
```

这会：

- 本地 smoke。
- 上传代码/模板/配置脚本。
- 远端备份。
- 远端 py_compile。
- 远端 `scripts/deployment_smoke.py --mode server`。
- 不重建 corpus。
- 不重启服务。

注意：`-SkipRestart` 后线上服务仍在旧进程上跑。文件已就地覆盖，但服务未重启。这个模式适合先验证云端文件完整性。

### 阶段 D：显式重建云端 corpus 并重启

确认阶段 B/C 都正常后，再执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\update_cloud.ps1 -RebuildCorpus
```

这一步会：

- 再次本地 smoke。
- 上传补丁。
- 远端 smoke。
- 重建完整 `corpus.sqlite`。
- 写入文集目录、马恩全集目录、列宁目录。
- 重启服务。
- 检查 `/api/runtime`。
- 如重启后健康检查失败，脚本会尝试从轻量备份回滚程序文件。

这一步风险最高。执行前务必确认：

- 列宁 PDF 已上传且远端 60 个。
- 本地 `update_cloud.ps1 -DryRun` 通过。
- 本地 `deployment_smoke.py --mode server` 通过。
- 本地重建过索引，列宁搜索有效。

### 阶段 E：公网验证

重启后，必须验证：

```text
https://mazhuzuojiansuo.com/
https://mazhuzuojiansuo.com/library
https://mazhuzuojiansuo.com/reader
```

并至少抽测：

- 首页搜索 `国家与革命`。
- 搜索结果里出现 `《列宁全集》`。
- “打开正文”能打开 `/viewer?...`。
- `/library` 能看到列宁分组。
- 列宁某卷目录可展开。
- 点击列宁目录项能显示 PDF 页图。

## 6. 当前需要特别注意的代码问题

### 6.1 `app.py` 改动很大

`git diff --stat` 显示 `app.py` 和 `templates/index.html` 改动行数很大。这部分可能因为原文件本来是未跟踪/换行差异导致统计膨胀。下一位应谨慎检查实际 diff，不要盲目回退。

严禁：

```powershell
git reset --hard
git checkout -- app.py
```

因为工作区有大量用户/既有改动。

### 6.2 当前 git 状态很脏

`git status --short` 显示大量文件是 modified 或 untracked。这不是本轮全部造成的，很多文件之前就未跟踪。

下一位只应处理本任务相关文件，不要整理整个仓库。

本任务相关文件主要是：

- `book_config.py`
- `config/books.yaml`
- `config/manifest.yaml`
- `config/volumes.yaml`
- `runtime_env.py`
- `build_index.py`
- `search.py`
- `app.py`
- `templates/index.html`
- `templates/library.html`
- `scripts/build_toc.py`
- `deploy/update_cloud.ps1`
- `deploy/upload_lenin_pdfs.ps1`
- `deploy/upload_reader_patch.ps1`
- 本交接文件

### 6.3 当前数据库还不含列宁

再次强调：`manifest.yaml` 有列宁，不代表 `data/corpus.sqlite` 已有列宁。

当前验证结果是：

```text
db_pages [('全集', 42472), ('文集', 9321)]
db_toc [('全集', 8346), ('文集', 1302)]
```

如果下一位直接上传代码并重启，但不重建 corpus，网站可能能启动，但列宁搜索不会有正文命中。

### 6.4 PowerShell 中文路径问题已规避

最初 `deploy/upload_lenin_pdfs.ps1` 硬写中文路径时，dry-run 失败，因为当前环境把中文字符串按旧编码读坏。

已改成：

- 从本地 `pdfs` 下寻找“恰好 60 个 PDF 的目录”。
- 使用该目录真实名称作为远端目录名。

下一位不要把脚本改回硬编码中文路径。

## 7. 建议补充的测试

可以在 `tests/test_security.py` 或新增测试中加入轻量测试：

- `book_config.load_book_configs()` 返回三个书库。
- `runtime_env.load_allowed_source_files()` 包含列宁 PDF。
- `Corpus` 在重建后的库里能加载 `列宁全集`。
- `/api/library/toc-suggest?q=国家` 能返回 `book_title = 《列宁全集》`。
- 搜索接口结果中有 `book_title/book_short_title/book_sort_order`。

注意：测试不要依赖真实 4GB PDF 全量重建；可以用现有数据库或 mock/临时 manifest 做轻量验证。

## 8. 上线命令速查

只做本地 dry-run：

```powershell
python scripts\deployment_smoke.py --mode server
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\update_cloud.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\upload_lenin_pdfs.ps1 -DryRun
```

只上传列宁 PDF，不重启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\upload_lenin_pdfs.ps1
```

上传代码但不重启、不重建：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\update_cloud.ps1 -SkipRestart
```

确认 PDF 已在云端后，上传代码、重建 corpus、重启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\update_cloud.ps1 -RebuildCorpus
```

## 9. 出现 502 时的应急判断

如果部署后公网 502，立即按照事故文档处理。

服务器上先看：

```bash
systemctl is-active marx-search
curl -i --max-time 5 http://127.0.0.1:8000/api/runtime
journalctl -u marx-search -n 120 --no-pager
```

优先搜索：

- `ModuleNotFoundError`
- `ImportError`
- `SyntaxError`
- `PermissionError`
- `OperationalError`

如果是新增文件缺失：

- 补传缺失文件。
- `chown www-data:www-data`。
- `chmod a+r`。
- 重启服务。
- 内外网都验证 200。

## 10. 本轮停止点

本轮已经停止在安全边界内：

- 没有执行真实云端上传。
- 没有重启生产服务。
- 没有重建生产数据库。
- 已完成本地 py_compile。
- 已完成 `update_cloud.ps1 -DryRun`。
- 已完成 `upload_lenin_pdfs.ps1 -DryRun`。

下一位 AI 可以从“阶段 A：本地收口与重建索引”继续，或者在用户确认后按“阶段 B：只上传列宁 PDF”继续。
