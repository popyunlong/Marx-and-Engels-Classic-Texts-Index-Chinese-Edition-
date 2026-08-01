# 2026-06-10 阅读器 PDF 渲染清晰度增强 · 交接文档

> 任务：两个阅读器（`/viewer`、`/reader`）里部分 PDF（尤其《马恩全集》《列宁全集》扫描件）显示发糊，做"增强渲染"让字更清晰，但**把握分寸、不增强过头**；同时**保障安全、不损坏既有功能、不让网站崩溃**。
> 上一份交接：`HANDOFF_2026-06-06_LENIN_LIVE_FINAL.md`（列宁 60 卷上线）。

---

## 0. 一句话结论

模糊根因是**服务端把 PDF 页固定按 1.45×（约 104 DPI）渲染成 JPEG，却在最宽 960px 的阅读器里放大显示** → 上采样发糊，扫描件叠加更明显。改为**按目标像素宽度自适应缩放（夹在 1.45×～3.0× 之间）+ JPEG 质量 90 + 缓存版本号升级**，只动 `app.py` 一个文件，已安全部署到生产并线上验证（`/` `/library` `/reader` 均 200，服务 `HEALTH_OK`）。

生产环境：`root@38.76.174.234`，目录 `/opt/marx-search`，公网 `https://mazhuzuojiansuo.com`，systemd 服务 `marx-search`。

---

## 1. 根因分析

- 两个阅读器（`/viewer`→`viewer.html`，`/reader`→`library.html`）最终都走**同一条服务端渲染路径**：`/page-image` → `_render_page_image_to_cache()`（`app.py`）。改这一处即覆盖两个阅读器。
- 页面用 `<img class="page-image">` 显示，CSS 宽 `min(100%, 960px)`（高分屏 ≈1920 设备像素）。
- 原渲染：`page.get_pixmap(matrix=fitz.Matrix(1.45, 1.45))` + `pix.tobytes("jpg")`。
  - 实测一张《全集》页（宽 369pt）只渲染出 **536×856px**，被放大到 960px 显示 → 明显糊。
- 不是 JPEG 质量问题（PyMuPDF 默认已 95），**核心是渲染分辨率太低**。

---

## 2. 改动内容（只动 `app.py`，向后兼容、失败安全）

### 2.1 自适应高分辨率渲染 —— `_render_page_image_to_cache()`
新增模块级常量（带详细注释）：
```python
PAGE_IMAGE_TARGET_WIDTH = 1600.0   # 目标渲染像素宽度（适配高分屏 960px 显示）
PAGE_IMAGE_MIN_SCALE = 1.45        # 缩放下限：不低于历史清晰度，「只会更清晰不会更糊」
PAGE_IMAGE_MAX_SCALE = 3.0         # 缩放上限：把握分寸，控制文件体积/渲染耗时/内存
PAGE_IMAGE_JPEG_QUALITY = 90       # JPEG 质量：高分辨率下兼顾文字边缘锐利与体积
```
渲染逻辑：按页面物理宽度算缩放系数，逼近目标像素宽，再用下限/上限夹住，**任何异常都回退到 `matrix_scale`（默认 1.45），绝不让渲染崩溃**：
```python
try:
    page_width_pt = float(page.rect.width)
    adaptive_scale = PAGE_IMAGE_TARGET_WIDTH / page_width_pt if page_width_pt > 0 else matrix_scale
    scale = max(matrix_scale, min(adaptive_scale, PAGE_IMAGE_MAX_SCALE))
except Exception:
    scale = matrix_scale
pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=True)
temp_path.write_bytes(pix.tobytes("jpg", jpg_quality=PAGE_IMAGE_JPEG_QUALITY))
```
函数签名默认值从 `1.45` 改为 `PAGE_IMAGE_MIN_SCALE`（等值），所有调用点（prewarm、viewer 预热线程、`/page-image` 路由）零改动。

### 2.2 缓存版本号 `v2 → v3` —— `_page_image_cache_path()`
```python
raw = f"{source_file}|{page_number}|{query_text}|{stamp}|v3"
```
**关键**：渲染参数变了必须改版本号，否则线上**已预热的旧 1.45× 模糊缓存**会继续命中、新逻辑不生效。

### 2.3 效果实测（真实《全集》PDF，宽 369pt）
| | 像素 | 文件 |
|---|---|---|
| 旧 1.45× | 536×856 | 117 KB |
| 新（3.0× 上限拦住，原本想要 4.33×） | 1109×1771 | 266 KB |

体积约 2× ，没过头；上限把超小页的过度放大拦住了——"把握分寸"的保护点生效。

---

## 3. 安全策略（针对"上次网站崩溃"的教训）

1. **刻意不引入 Pillow。** 服务器运行环境 `requirements.txt` **没装 Pillow**（只在 `requirements-build.txt` 桌面构建里有）。若在渲染路径 `import PIL` 会触发 `ImportError` → 页面 502。方案纯用 PyMuPDF（已装 1.27.2.2），零新依赖。
2. **失败安全**：缩放计算包 try/except，异常回退 1.45×；下限保证"只会更清晰不会更糊"。
3. **向后兼容**：函数签名/所有调用点不变。
4. **验证门禁**：`py_compile` + 本地 `pytest` **46 passed**（含 `tests/test_security.py` 全套安全测试）+ 本地/远端 `deployment_smoke.py`。

---

## 4. 部署方式与重要教训（务必沿用）

> 用户最初要求用 `update_cloud.ps1`，但上一份手册强调 **PowerShell 驱动的 SSH 部署脚本在"无终端"环境会卡死**。本轮经确认后**改用 Bash 工具做单文件快速补丁**，全程 `ssh -n` / `scp -O`，稳定无卡。

实际执行步骤（单文件补丁，最干净）：
```bash
KEY="$HOME/.ssh/id_marx_cloud_ed25519"; HOST="root@38.76.174.234"
# 1) 备份线上 app.py（回滚点 = 当前生产版，不是旧全量备份）
ssh -n -o BatchMode=yes -i "$KEY" "$HOST" "cp -a /opt/marx-search/app.py /opt/marx-search/app.py.bak-clarity"
# 2) 上传 + 远端 py_compile + smoke（不重启，失败则恢复备份）
scp -O -o BatchMode=yes -i "$KEY" app.py "$HOST:/opt/marx-search/app.py"
ssh -n -o BatchMode=yes -i "$KEY" "$HOST" "cd /opt/marx-search && chown www-data:www-data app.py && chmod a+r app.py && . .venv/bin/activate && python -m py_compile app.py && python scripts/deployment_smoke.py --mode server 2>&1"
# 3) 重启 + 健康轮询 + 失败回滚（脚本作为单引号内联参数，不要用 heredoc）
ssh -n -o BatchMode=yes -i "$KEY" "$HOST" 'RT=http://127.0.0.1:8000/api/runtime; systemctl restart marx-search; ok=0; for i in $(seq 1 20); do curl -fsS --max-time 5 "$RT" >/dev/null 2>&1 && { ok=1; break; }; sleep 3; done; if [ $ok -eq 1 ] && systemctl is-active --quiet marx-search; then echo HEALTH_OK; else cp -a /opt/marx-search/app.py.bak-clarity /opt/marx-search/app.py; systemctl restart marx-search; echo ROLLED_BACK; fi'
```

### ⚠️ 本轮新踩的坑（重要）
**`ssh -n` 与 heredoc 冲突。** `ssh -n` 把 stdin 重定向到 `/dev/null`，会**吞掉** `ssh ... 'bash -s' <<'EOF'` 的 heredoc，导致远端 `bash -s` 收到空脚本、"看似成功实则没执行"（第一次重启就是空跑、`exit 0`）。
- **正解**：把远端脚本**作为单引号内联参数**传给 `ssh -n`（局部变量用单引号包住，避免本地 bash 提前展开 `$i`/`$ok`/`$(seq)`）。
- 这也解释了为何 `restart_verify.sh`（内部 `bash -s` 读 heredoc）不能配 `ssh -n` 直接用。

---

## 5. 线上验证结果（已通过）

- `systemctl restart marx-search` → `HEALTH_OK`，`/api/runtime` = `{"ok":true,"db_status":"ok","search_enabled":true,...}`。
- 远端 `app.py` 已含 `MAX_SCALE=3.0`、缓存 `v3`、`jpg_quality`、自适应缩放代码。
- 公网 `/`、`/library`、`/reader` 均 **200**。

---

## 6. 仍需知道 / 可选项

1. **缓存按页懒生成**：每页首次打开时按新逻辑重渲染（首次稍慢），之后命中缓存。
2. **旧 v2 缓存成孤儿**：留在服务器 `page_images` 缓存目录，确认效果后可清理回收磁盘（可选，不影响功能）。缓存目录见 `app.py` 的 `PAGE_IMAGE_CACHE_DIR`（线上 `/var/www/.marx_search_full/page_images`）。
3. **浏览器端 24h 缓存**：`/page-image` 带 `max_age=86400` 且 URL 不变，**之前看过的页**在浏览器里可能仍显示旧糊图（最多 24h 或 Ctrl+F5 强刷）；**没看过的页**立即清晰。
   - 若要所有人**立刻**看到清晰版：可在 `viewer.html` / `library.html` 的图片 URL 上加版本参数（如 `&rev=3`）做缓存击穿——小模板改动，再走一次同样的单文件补丁即可。**本轮未做**（用户只要求渲染增强）。
4. **进一步增强（如仍嫌不够清晰）**：把 `PAGE_IMAGE_MAX_SCALE` 调到 3.0 以上即可，代价是文件更大/渲染更慢。这是唯一旋钮，改完同样只需重传 `app.py` + 重启。注意上调后**建议同时把缓存号 `v3 → v4`**，否则刚生成的 v3 缓存不会刷新。

---

## 7. 本轮改动文件清单

- `app.py`：
  - `_page_image_cache_path()` 缓存号 `v2 → v3`；
  - 新增 `PAGE_IMAGE_TARGET_WIDTH/MIN_SCALE/MAX_SCALE/JPEG_QUALITY` 常量；
  - `_render_page_image_to_cache()` 自适应缩放 + `jpg_quality`。
- 生产服务器：新增回滚点 `/opt/marx-search/app.py.bak-clarity`（可保留备用）。
- **未改**：模板、配置、数据库、其它 `.py`；未重建语料；未动 PDF。

---

## 8. 出现 502 时的应急（同既有事故文档）

```bash
systemctl is-active marx-search
curl -i --max-time 5 http://127.0.0.1:8000/api/runtime
journalctl -u marx-search -n 120 --no-pager
# 本轮快速回滚（仅 app.py）：
ssh -n -i ~/.ssh/id_marx_cloud_ed25519 root@38.76.174.234 \
  "cp -a /opt/marx-search/app.py.bak-clarity /opt/marx-search/app.py && systemctl restart marx-search"
```
更早的全量代码回滚点仍是 `/opt/marx-search.cloud-backup.20260606-101614`。
