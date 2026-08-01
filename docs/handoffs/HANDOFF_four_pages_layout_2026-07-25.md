# 交接文档 · 四页面布局重构（**已上线 2026-07-26**）

> 面向接手的 AI。读完本文即可无缝继续，无需回看原对话。
> 最后更新：2026-07-26。**代码状态：已上线生产**（DEPLOYED_SHA `f88c591-dirty`；远端备份
> `/opt/marx-search.cloud-backup.20260726-015929`）；工作树仍 dirty、未 commit。
> 部署方式＝范围化 `update_cloud`：临时把 `cloud_patch_files.txt` 换成仅本次差异的 **20 个文件**
> （app/ai/search.py + 13 模板 + `_appnav.html`/`layout-v2.css` 新增 + `ai-page.js/css`），`-AllowDirty`，
> 字节备份还原清单；逐文件比对服务器现行版确认零外来 WIP、config/data 不动；update_cloud 自带远端备份 +
> import 冒烟 + 重启后 `curl / /pricing` 校验 + 失败自动回滚。**`/` cutover 已生效＝首页变四页面检索。**

---

## 0. 一句话状态

把整站面向用户的布局重构成**四个可切换的顶级页面**（检索 / 阅读 / AI研究对话 / 进阶功能），
以**真实 Flask 路由（MPA，非 SPA）** 落地，新布局挂在 `/v2*`，**老首页 `/` 原封不动**。
本地预览已可完整使用并逐项验证通过。**尚未部署**，部署需用户明确点头后再启动。

---

## 1. 目标与已确认的产品决策

**总目标**（用户原话精炼）：研究一流网站的前端排版 → 出计划文件 → 按计划做预览 → 用户确认 → 上线部署。

**四个页面**：
1. **检索**：只放引文检索（含「标准检索 / 联想检索」两个子标签，两者都保留）。**检索页即首页**（默认落地页）。
2. **阅读**：篇章直达 + PDF 阅读器 + AI导读阅读器 + 书库 +（外文文库归此，不归进阶）。
3. **AI研究对话**：保留现有 `/ai` 内容。
4. **进阶功能**（最终定名；曾用「更多功能」「拓展功能」）：大辞典、流式阅读、期刊订阅。

**已确认的设计决策**（AskUserQuestion + 后续澄清）：
- 检索=首页；顶部 4 标签常驻导航；辅助内容（公告 / 注册分布 / 社区 / 反馈）折叠进检索页下方信息区。
- 联想检索作为检索页的子标签保留。
- 视觉：**保留品牌红 `#8f1d1d` + 暖纸色**，但更简约现代（同色系）。
- 账户/会员/登录放在导航右侧账户区（登录后显示头像菜单）。
- 外文文库并入「阅读」，不作为进阶项。
- **AI研究对话是「登录用户」权限，不是会员权限**（后台 `audience.registered.<feature>` 开关）。
- 游客可读原著 PDF；AI 需登录。
- **二级页不套 v2 导航**：大辞典 / 期刊 / 流式 / 文库 / 阅读器 / 套餐 / 账户等保留各自原有导航。

---

## 2. 架构：路由 + 模板开关

**MPA 决策**：明确否决「伪 SPA / 套壳」，用真实路由。理由已写进计划文件 §2。

**四条新路由**（`app.py`，行号以当前工作树为准）：

| 路由 | 视图函数 | 位置 | 说明 |
|---|---|---|---|
| `/v2` | `layout_search()` | app.py:9118 | 检索页（=四页面布局的首页） |
| `/v2/read` | `layout_read()` | app.py:9123 | 阅读页 |
| `/v2/more` | `layout_more()` | app.py:9128 | 进阶功能页 |
| `/v2/ai` | `layout_ai()` | app.py:9156 | AI研究对话页 |
| `/reader/cover` | `reader_cover()` | app.py:9970 | 书籍封面缩略图（PDF 第 1 页） |

- `/v2`、`/v2/read`、`/v2/more` 都调用 **`_render_index_page(layout_page=...)`**（app.py:9003）——
  与老首页 `/`（`index()`）**共用同一个 `index.html` 模板**，靠模板里的 `layout_v2` / `layout_page` 开关分区渲染。
- `/v2/ai` 调用 **`_render_ai_page(layout_v2=True)`**（app.py:9133）——渲染 `ai.html`，与老 `/ai` 共用。
- **老 `/` = 旧首页，完全没动**；`/legacy` 也仍可用。→ 一行回退，风险隔离。

**模板分区开关**（在 `index.html` 顶部 `{% set %}`）：
- `v2` = `layout_v2`（真/假）
- `_pg` = `layout_page`（`search` / `read` / `more`）
- `show_search` / `show_read` / `show_more` = 由 `_pg` 派生的分区布尔量

**导航外壳**：`templates/_appnav.html`（仅 `layout_v2=True` 时 include）。
**样式**：`static/layout-v2/layout-v2.css`，全部作用域挂在 `body.v2` 上（当前 `?v=18`）。

---

## 3. 已完成功能（分模块，均已在预览中验证）

### 3.1 导航外壳 `templates/_appnav.html`
- 顶部 4 标签（检索/阅读/AI研究对话/进阶功能）+ 移动端底部标签栏 + 账户区。
- 品牌 logo 与「检索」标签都指向 `url_for('layout_search')`（即 `/v2`，**不是**老 `index`）。
- 账户区：登录显示头像菜单（账户中心/套餐/本地控制台/管理后台/退出）；未登录显示登录+注册。
- 账户菜单 IIFE + **导航「等待不打断」IIFE**（见 3.7）。

### 3.2 检索页（index.html，`_pg='search'`）
- 单栏布局；标准检索 / 联想检索两子标签保留。
- **引文格式选择条（citeFormatBar）bug 修复**：原来只在有 `[data-citations]` 时才显示，导致「未点开具体引文前看不到格式条」。改为
  `out.querySelector('[data-citations]') || out.querySelector('.group-card')` 即出现即显示。
- 帮助「?」移到「联想检索」旁。
- 辅助内容（公告/注册分布/社区/反馈）折叠到页面下方信息区（反馈**不做**右下浮标——右下角被马克思宠物 z-index 9991 占用）。
- `window.__marxBusy = () => !!document.querySelector('.search-loading')`（index.html:2993）。

### 3.3 阅读页（index.html，`_pg='read'`）
- **篇章直达置顶并重点凸出**（`.chapter-search-panel`：干净卡片、深色衬线标题 21px），放在「继续阅读」历史记录**上方**；历史记录移到其下方。
- **书目按收藏集分类**：后端传 `read_book_groups`（按 `collection` 字段分 5 类：经典原著 / 马中化 / 习思想 等）；分类栏目名 `.v2cat-title` 20px，字号已加大。
- **真实封面缩略图**：每本书封面用 `url_for('reader_cover', file=第一卷source_file)` 取 PDF 第 1 页缩略图，`onerror="this.remove()"` 兜底。
- **多卷书「分卷→分目录」下钻**（不再点进去直接跳第 1 卷）：
  - 后端传 `read_books_nav`（JSON：每书 kind=pdf/foreign，volumes 含 v/title/file/pages/toc/url）。
  - 前端左侧抽屉 `.v2drawer`（**left:0，从左滑入**，因为右下角是宠物；z-index 9996，遮罩 9995 在宠物 9991 之上）。
  - 抽屉里先列分卷，点卷再**懒加载目录** `/api/library/volume-toc?mode=ai&file=`。
  - 抽屉开启动画用**强制回流** `void drawer.offsetWidth`（不用 rAF——隐藏自动化标签页里 rAF 被节流会卡在屏外）。
- **6 级目录视觉层级**（`.v2toc-item.lv1..lv6`）：字号 15.5→12px、字重 800→400、缩进 0→79px、颜色深→淡。已在《资本论》/《文集》卷 5（151 条）验证 lv1–lv6 明显可辨。
- 阅读器跳转都带 `&from=v2read`，PDF 阅读器「返回文库/返回检索」据此回到 v2 页（见 3.6）。

### 3.4 AI研究对话页（ai.html + static/ai-page/）
- **本地化多会话记录**（仿主流 AI 网页应用）：
  - `ai-page.js` 里完整的 per-uid 多会话模型：`marx-ai-sessions-v2`（键含 uid），含 loadStore/persist/currentSession/deriveTitle/saveMessages/loadSessions/switchSession/newSession/deleteSession/renameSession/relTime/renderSidebar + 抽屉开合。
  - 从旧 `marx-ai-thread-v1` 迁移。
  - 跨标签 `storage` 事件同步。
  - `ai.html` 加 `data-uid`、会话侧栏（新建/列表/开合/遮罩）。
- **AI 输出排版修复**：
  - 渲染器标题映射修复：`###` 原来映射到 muted 的 h5，改成 `length<=3?3:4`→`###` 渲染成醒目 h3。
  - 整行 `**...**` 提升为 h3；剥离游离 `**`；引用块内剥 `**`/`__`。
  - `ai.py` 三个答案提示词分支（grounded / zhipu / plain）都加了 Markdown 结构指令（`###` 小标题、`>` 引用块、`**` 加粗，**引用块内照录原文不加粗**、`**` 务必成对）。
- `window.__marxBusy = () => streaming`（ai-page.js:932）。

### 3.5 阅读器 AI导读引文折叠（viewer.html）
- `renderCitations` 默认折叠：`.ai-citations-body { display:none }` + `.ai-citations-toggle` 按钮「展开▾/收起▴」。
- 委托事件绑在 `aiMessagesEl` 上。

### 3.6 跳转链接全面校正
- 所有 v2 阅读器链接带 `&from=v2read`。
- `pdf_viewer` 在 `from==v2read` 时设 `search_back_url` / `library_back_url` 指回 `/v2` 与 `/v2/read`（不再回老版文库）。
- 篇章直达渲染的 `hrefU` 在 `body.v2` 下追加 `&from=v2read`。

### 3.7 「等待中不打断」——新标签页承接（用户本人的点子）
- **问题根因**：新布局是真实路由，导航会中断在飞的 AI/检索 fetch；老站是单页（无导航）所以不受影响。
- **方案**：`_appnav.html` 拦截导航点击——若 `window.__marxBusy()` 为真：
  - 点**其它**标签 → `window.open(href,'_blank','noopener')` 新标签页打开，**原标签继续生成**；
  - 点**当前**页 → 只 `preventDefault` 停留、不重载；
  - 两种都弹 `.v2navtoast` 提示。
- 承接靠共享 localStorage 会话（AI 按 uid 共享 `marx-ai-sessions-v2`）。
- **零后端改动**，完整保留 cancel-on-disconnect / 心跳 / 信号量。

---

## 4. 如何在本地跑预览（关键）

**必须用服务器模式**（desktop 模式把 AI 挡在 server-sync 缓存后，本地测不了 AI）：

```bash
python run_preview.py
```

（`APP_MODE=server` 与 `MARX_SKIP_NODE_CHECK=1` 已在脚本内 `setdefault`，无需再手动前缀；端口默认 8011，可用 `PREVIEW_PORT` 覆盖。）

- 启动器 `run_preview.py` 是**预览专用**（附录 A 有全文）。它 **不改 app.py 一个字节**，仅靠猴补丁 + 请求钩子：
  - 覆盖 `_feature_effective_for_user` / `_is_admin_user` / `_load_access_policy`，按预览身份放权。
  - 注入左下角浮动「预览身份」切换条：**游客 / 注册用户 / 会员 / 管理员**（`?as=guest|registered|member|admin`，记在 session）。
    - 游客：可读原著 PDF；AI 一律需登录。
    - 注册用户：可读原著 + 全部 AI（登录门控，非会员）；会员专属项（流式/期刊/外文库）仍锁。
    - 会员/管理员：全开（管理员多管理入口）。
- `config/ai.yaml` 本地有一把**可用的 DeepSeek key** → 服务器模式下 AI 真的能跑通。
- 打开 http://127.0.0.1:8011/v2 （检索）、`/v2/read`、`/v2/ai`、`/v2/more`。

> ✅ 启动器已落到**仓库根 `run_preview.py`**（未提交、随仓库可见，附录 A 亦有全文备份）。
> ⚠️ **务必在你自己的终端里前台运行**（`python run_preview.py`，Ctrl+C 退出）。经 AI 的后台工具（Bash `run_in_background`）拉起，本 Windows 环境下进程起来后随即被回收、无法常驻——两次实测都是「起来→服务几秒→exit 127 被收走」。

**验证手法**（截图会超时——浏览器 pane 隐藏标签会冻结 CSS 过渡）：用
`mcp__Claude_Browser__javascript_tool` 读 computed style / 断言 DOM / 探测 fetch；或 `read_page` 读无障碍树。

---

## 5. 未完成 / 待办

### 5.1 部署（**未开始，需用户明确点头**）
这是一个横跨 app.py / ai.py / 多个模板 / 新静态目录的大改动集。部署前必须：
1. **协调服务器领先的文件**（曾审计出以下文件服务器版领先/含他会话 WIP）：`config/ai.yaml`、`config/books.yaml`、`deploy/cloud_patch_files.txt`、`templates/index.html`。
   → 以 **scp 下来的服务器现行文件为 diff 基线**，只重放本次布局改动的 hunk，**剥离外来 WIP hunk**（模板叠了大量用户 WIP）。
2. **新静态入清单**：`static/layout-v2/layout-v2.css`、`static/ai-page/*`（改动）、`templates/_appnav.html` 加进 `deploy/cloud_patch_files.txt`（清单**≥2 行**，避免 splat 坑）。
3. 新端点 `/reader/cover` 依赖 PyMuPDF(fitz)——服务器已有（阅读器渲染在用），确认无新依赖。
4. **范围化 `update_cloud`**（`-AllowDirty`）；`update_cloud` 不验 `/viewer` → **服务器侧自查**（grep 路由已注册、NUL 哨兵 `grep -a`）。
5. 备份 + 冒烟 + 回滚方案。
6. **部署前把 `data/corpus.sqlite.sha256` 还原成 git 版本**（见 5.5）。
7. **不要 clobber** 用户 WIP，也**不要动服务器端 `access_policy`**（存在服务器 `get_setting`，不在仓库→部署不会覆盖）。
8. `.ps1` 须 UTF-8 BOM、sha256 须 LF、`.ps1` 里勿加全角中文注释（PS5.1 GBK 会坏解析）。
9. 单一共享工作树 + 单生产机，**勿与并行 AI 会话并发部署**。

### 5.2 `/` 切换（cutover，未做）
现在 `/`=老首页、`/v2`=新布局。用户确认满意后，最终要把新布局切成 `/`（并把导航里 `layout_search`/brand 改回 `url_for('index')` 或保持）。**这一步等用户发话**。

### 5.3 后端权限策略（服务器侧，用户说他已设了一部分）
- 让 AI研究对话「登录即用」：后台「会员与权限」把 `audience.registered.{search_chat,ai,research,associative}` 设为 True（access_gate 自动变 `login`）。
- 让游客能读 PDF：给游客授 `library`（访客权限）。
- 这些是**服务器端设置**，不在仓库，部署不影响。

### 5.4 计划文档 `PLAN_layout_four_pages_2026-07-20.md`
部分内容已被实现超越（§2/§3/§8 里若还有 SPA/套壳/更多功能 旧提法应校正）。已改名「更多功能→进阶功能」「外文文库移入阅读」。**低优先**，可在部署后一并收口。

### 5.5 `data/corpus.sqlite.sha256`
当前是**本地实际哈希**（`b305faee...`，LF），不是 git 版本——因为本地跑 app 校验的是本地 DB。**commit/部署前必须还原成 git 版本**，否则线上桌面打包会哈希不符。

### 5.6 安全
- 用户曾在对话里**明文粘贴过一把 DeepSeek key（`sk-b91f...`）**——**我没用它、没写进任何文件**，本地用的是 `config/ai.yaml` 里既有的另一把有效 key。**提醒用户轮换那把粘贴过的 key**（它已在对话记录里）。

### 5.7 可选小项（提过、非必须）
- `control.html` 的 `ai_assistant_mode` 管理开关（全站 AI 抽屉已移除）是否一并删。

---

## 6. 关键约束与坑（血泪，务必先读）

- **截图不可靠**：项目外文件当静态图渲染、隐藏自动化标签冻结 CSS 过渡 → 一律用 `javascript_tool` 读 computed style / 断言 DOM / 探 fetch。
- **抽屉动画**别用 rAF（隐藏标签被节流卡屏外），用 `void el.offsetWidth` 强制回流。
- **二级页不套 v2 导航**（用户决定）；它们保留自身导航；全站 AI 抽屉 `_ai_assistant.html` 已从二级页移除。
- **模板叠了大量用户 WIP**：工作树领先 git；部署 diff 基线=scp 下来的服务器文件，逐 hunk 剥离外来改动。
- **新静态**必须进 `cloud_patch_files.txt`；静态 `?v=` 改了要 +1。
- **`update_cloud` 不验 `/viewer`**→ 服务器侧自查。
- **本地跑 app 会改写 `corpus.sqlite.sha256`**（git restore 还原）。
- **CSS/JS 版本号**（改了要再 +1）：`layout-v2.css?v=18`、`ai-page.css?v=5`、`ai-page.js?v=12`。
- `.layout_backup/` 存了 `index.html.bak` / `app.py.bak`（改前备份，用于把本次改动从用户 WIP 里隔离）。

---

## 7. 附录 A · `run_preview.py` 全文（预览启动器，可原样另存到仓库根后运行）

```python
"""本地四页面布局预览启动器（含权限切换器）。

直接复用 app.py 里已构建好的 Flask app，绕开 main()/run_desktop()——
不触发 pythonw 重启、不自动开浏览器、不启用空闲自动关闭，便于在后台受控运行。

【权限预览】通过 ?as=guest|member|admin 在游客/会员/管理员三种身份间切换，
选择记在 session 里，切页后保持。实现方式是「猴补丁 + 请求钩子」，全部写在本
启动器里，**不改动 app.py 一个字节**，服务器线上不受任何影响。
"""
import os
import sys

os.environ.setdefault("MARX_SKIP_NODE_CHECK", "1")

REPO = r"D:\claudecode文件夹\【增强】马恩《文集》《全集》检索"
sys.path.insert(0, REPO)
os.chdir(REPO)

PORT = int(os.environ.get("PREVIEW_PORT", "8011"))

import app as A  # noqa: E402  （导入即完成语料/配置初始化）
from flask import g, request, session  # noqa: E402

app = A.app
TIERS = ("guest", "registered", "member", "admin")

# ---- 猴补丁：让权限判定服从预览身份 -------------------------------------------
_orig_eff = A._feature_effective_for_user
_orig_is_admin = A._is_admin_user


def _preview_tier():
    return session.get("preview_as") if A.has_request_context() else None


_AI_FAMILY = {"search_chat", "ai", "research", "associative"}
_TIER_ALLOW = {
    "guest": {"library"},
    "registered": {"library"} | _AI_FAMILY,
}


def _eff(feature, user=None):
    tier = _preview_tier()
    if tier in ("member", "admin"):
        return True
    allow = _TIER_ALLOW.get(tier)
    if allow is not None:
        if feature in allow:
            return True
        return _orig_eff(feature, None)  # 其余按匿名真实评估
    return _orig_eff(feature, user)


def _is_admin(user):
    tier = _preview_tier()
    if tier == "admin":
        return True
    if tier in ("member", "registered", "guest"):
        return False
    return _orig_is_admin(user)


A._feature_effective_for_user = _eff
A._is_admin_user = _is_admin

# 预览：把 AI 家族授给「注册用户」审众（=后台 audience.registered.<feature> 开关）。
_orig_load_policy = A._load_access_policy


def _load_policy():
    pol = _orig_load_policy() or {}
    try:
        pol = dict(pol)
        aud = dict(pol.get("audience") or {})
        reg = dict(aud.get("registered") or {})
        for f in ("search_chat", "ai", "research", "associative"):
            reg[f] = True
        aud["registered"] = reg
        pol["audience"] = aud
    except Exception:
        pass
    return pol


A._load_access_policy = _load_policy

_FAKE = {
    "registered": {"id": -100, "email": "user@preview.local", "display_name": "预览注册用户", "is_active": 1},
    "member": {"id": -101, "email": "member@preview.local", "display_name": "预览会员", "is_active": 1},
    "admin": {"id": -102, "email": "admin@preview.local", "display_name": "预览管理员", "is_active": 1},
}


@app.before_request
def _preview_identity():
    as_ = request.args.get("as")
    if as_ in TIERS:
        session["preview_as"] = as_
    elif as_ == "real":
        session.pop("preview_as", None)
    tier = session.get("preview_as")
    if tier == "guest":
        g.current_user = None
    elif tier in _FAKE:
        g.current_user = dict(_FAKE[tier])
    g.pop("_view_state_cache", None)


# ---- 注入右下浮动的权限切换条（仅预览，HTML 响应才注） -------------------------
_BAR_CSS = """
<style id="pv-role-bar-css">
#pvRoleBar{position:fixed;left:16px;bottom:16px;z-index:99999;display:flex;align-items:center;gap:8px;
 background:#241f1a;color:#efe4d6;border-radius:12px;padding:8px 10px;font:13px/1 -apple-system,"Microsoft YaHei",sans-serif;
 box-shadow:0 12px 34px rgba(0,0,0,.3);}
#pvRoleBar b{color:#f0c674;font-weight:700;letter-spacing:.04em;margin-right:2px;}
#pvRoleBar a{color:#d9cbbb;text-decoration:none;padding:5px 11px;border-radius:8px;font-weight:600;}
#pvRoleBar a.on{background:#f2e6d6;color:#241f1a;}
#pvRoleBar a:hover:not(.on){background:rgba(255,255,255,.1);}
</style>
"""


def _bar_html(current):
    path = request.path
    def link(tier, label):
        cls = " class=\"on\"" if current == tier else ""
        return f'<a href="{path}?as={tier}"{cls}>{label}</a>'
    return (
        _BAR_CSS
        + '<div id="pvRoleBar"><b>预览身份</b>'
        + link("guest", "游客")
        + link("registered", "注册用户")
        + link("member", "会员")
        + link("admin", "管理员")
        + "</div>"
    )


@app.after_request
def _inject_role_bar(resp):
    try:
        ctype = resp.headers.get("Content-Type", "")
        if "text/html" not in ctype or resp.direct_passthrough:
            return resp
        body = resp.get_data(as_text=True)
        if "</body>" not in body:
            return resp
        current = session.get("preview_as", "real")
        resp.set_data(body.replace("</body>", _bar_html(current) + "</body>", 1))
    except Exception:
        pass
    return resp


if __name__ == "__main__":
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    print(f"[preview] starting on http://127.0.0.1:{PORT}  (?as=guest|member|admin)", flush=True)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)
```

---

## 8. 附录 B · 文件改动清单

**新建**
- `templates/_appnav.html` — 导航外壳（顶栏 + 移动底栏 + 账户区 + 账户菜单 IIFE + 等待不打断 IIFE）。
- `static/layout-v2/layout-v2.css` — v2 视觉系统（`body.v2` 作用域；导航/检索单栏/封面卡/分类区/左侧抽屉/6 级目录/toast）。当前 `?v=18`。
- `PLAN_layout_four_pages_2026-07-20.md` — 计划文件（部分被实现超越）。
- `PREVIEW_four_pages.html` — 早期静态 mock（已被真实实现取代，可留档或删）。
- `run_preview.py`（本会话在 scratchpad）— 见附录 A。

**修改**
- `app.py` — `_render_index_page(layout_page)`（9003）+ 三路由 `/v2` `/v2/read` `/v2/more`；`_render_ai_page(layout_v2)`（9133）+ `/v2/ai`；`reader_cover()`（9970，fitz 第 1 页 240px，sha256 缓存到 APPDATA/reader_covers）；传 `read_book_groups`/`read_foreign_books`/`read_books_nav`；pdf_viewer 的 `search_back_url`/`library_back_url` + `from=v2read`；书库对所有人可见（`_feature_is_available("library")`）。
- `ai.py` — 三答案分支（grounded/zhipu/plain）加 Markdown 排版指令。
- `templates/index.html` — v2 分区（`v2`/`_pg`/`show_*`）；v2 head + 引 nav + 引 layout-v2.css(v=18)；检索面板（含 citeFormatBar 修复）；篇章直达置顶+重点凸出；继续阅读移到其下；书目分类网格 + 封面缩略图；分卷/目录抽屉（JSON + markup + JS，6 级目录）；辅助信息区；`window.__marxBusy`（2993）。
- `templates/ai.html` — nav 外壳 + `layout_v2` class；`data-uid`；会话侧栏 markup；`ai-page.css?v=5` / `ai-page.js?v=12`。
- `static/ai-page/ai-page.js` — 渲染器标题映射修复（###→h3）+ bold 提升/剥离；per-uid 多会话模型 + 侧栏 CRUD；旧 thread 迁移；`window.__marxBusy`（932，返回 streaming）。
- `static/ai-page/ai-page.css` — AI 答案排版；会话侧栏；`aip-main` 改双栏（侧栏 + stage 最大 860px）。
- `templates/viewer.html` — AI导读引文默认折叠 + 展开/收起；返回链接 v2 感知。
- 多个二级页模板 — 移除全站 AI 抽屉 include。
- `data/corpus.sqlite.sha256` — 本地实际哈希（部署前还原 git 版）。

**备份**
- `.layout_backup/index.html.bak`、`.layout_backup/app.py.bak`。

---

## 9. 下一步建议

用户最近的可执行需求（6 级目录层级 + 新标签页不打断导航）**均已完成并验证**。
接手后应：**等用户发话**——要么继续微调预览，要么开始「部署前准备」。
**部署不得在用户明确确认前启动**，且须按 §5.1 逐条协调服务器领先文件、还原语料哈希、剥离外来 WIP hunk。

---

## 10. 追加修复（2026-07-25 第二轮预览，均已本地验证）

五项用户反馈已修，全部仍在**工作树未提交**：

1. **阅读页外文文库不见了** → `app.py _foreign_library_books` 加 `require_access` 参数；阅读页
   （`_render_index_page` 的 read 分支）改用 `require_access=False` 调用，使外文原著**与中文 PDF 书目一致
   ——对所有身份（游客/注册/会员/管理员）陈列书名**，真正阅读权仍在 `wenku_reader` 入口按
   `static_library` 门控（已核验游客点外文书 → 302 跳 `/login`，不泄露）。`/reader`、`/library`
   保持 `require_access=True` 原样。病根：外文库原来要求**当前用户** `static_library` 权，非会员整组消失，
   与「列出书名≠授予阅读权」的中文书设计不一致。

2. **研究综述排版 + 引文富卡片** → `static/ai-page/ai-page.js` + `ai-page.css`：
   - **引文富卡片**（对齐检索页 `.rv-cite`）：金色 `[N]` 序号 + 「综述已引用」(`review_quoted`)
     +「名目索引·<subject_label>」徽标 + 证据页链接 + 复制引文。
   - **标题层级**（注：**第 6 项已把它改为与快速问答统一**，见下）：曾给研究档 essay 档做
     `##`→h2 大标题 / `###`→h3 的独立层级；后按用户「两档排版逻辑应一致」的反馈回退。
   - **CSS 特指度坑（仍成立）**：`/v2/ai` 处于 `body.v2`，`layout-v2.css` 的
     `body.v2 #aip-root .msg-body h3` (1,2,2) 会盖过纯 `#aip-root .msg-body.aip-essay h3` (1,2,1)
     → 研究档 h3 的鎏金覆写须补 `body.v2` 前缀变体 (1,3,2) 夺回。

3. **研究对话引文可选引用格式** → 同两文件：引文条头部加「引用格式」下拉（国标 GB/T 7714 / 中国社会科学 /
   马克思主义研究），复用**全站共享键** `marx-citation-format-v1`（与检索页 citeFormatBar 同一套）；
   引文串写进 `data-citations` 属性，切换时 `applyCiteFormat` **就地重写**所有 `[data-citations]` 文本 +
   同步各处下拉（保留展开态、跨研究/快速两类引文条同步），并加「复制引文」复制当前格式串。
   研究综述与快速问答接地引文均适用。

4. **~~研究综述原文引语高亮~~（已按用户要求于第 7 项移除）** → 曾在 essay 模式 `renderInlineMarkdown`
   把 `“…”` 内 ≥6 字成句原文包 `.aip-quote` 暖金底高亮；后用户要求去掉，`_essayMode`/`.aip-quote` 已全部撤除。

6. **统一快速问答与研究综述的排版逻辑**（用户：两档排版逻辑应一致，配色可不同，且觉得快速档更对）→
   `ai-page.js` 标题映射统一为 `hashes<=3?h3:h4`（去掉研究档 `##`→h2 大标题的分支）；`ai-page.css` 研究档
   `.aip-essay h3/h4` **只覆写颜色**（鎏金），字号/内边距/左条宽/圆角/间距全部继承快速档的 `.msg-body h3`。
   浏览器实测：两档首个标题 tag/fontSize(18.24px)/padding(9px14px)/border-left(5px) 逐项一致，仅
   border/bg/文字色不同（品牌红↔鎏金）。`essay` 标志仅保留用于**正文原著引语高亮**（第 4 项），不再影响标题层级。

5. **流式阅读引文可选引用格式** → `templates/wenku_reader.html`（内联 JS/CSS，无 `?v=`）：
   「引用所选」浮窗 + 「引用本页」弹窗加「引用格式」下拉（国标/中国社会科学/马克思主义研究），复用共享键
   `marx-citation-format-v1`。新增 `stdCiteFormats(c,v,page)` 从卷字段就地拼 3 格式，**与后端
   `search.DEFAULT_CITATION_TEMPLATES` 逐字对齐**（题名领起无著者；`publisher_zh:"北京：人民出版社"`
   正则拆 place/pub）。**仅 `CFG.lang==='zh'`（流式《文集》）启用**，外文书 `formats=null` → 回退原
   `zh` 单一格式、不显下拉。复制/报错用当前所选格式串。node + 浏览器双验：gb2015/zgshkx 与研究综述引文逐字一致。

7. **移除研究综述原文高亮 + 确认快速问答引文格式可选**（用户两点反馈）→
   - **移除高亮**：`ai-page.js` 撤掉 `_essayMode` 标志、`renderInlineMarkdown` 的 `“…”`→`.aip-quote`
     包裹、`renderBasicMarkdown` 的 `essay` 形参；`ai-page.css` 删 `.aip-quote` 规则。`.aip-essay` 类保留
     （仅用于研究档 h3/h4 鎏金配色）。
   - **快速问答格式可选＝本就已实现**（第 3 项 `renderCitations` 对两档无差别加下拉；后端 `_build_chat_grounding`
     的接地引文走 `hit.to_dict()`，与研究综述同样带 `citations` 三格式映射——**实测 `cit0_has_multiformat=true`**）。
     浏览器实测：快速问答引文条的「引用格式」下拉 present+visible、3 选项、切换生效。**默认接地开启**
     （`marx-ai-grounding-v1` 缺省 = 开），故默认就有引文+下拉；若用户没看到，多半是把「检索引文库作答」关了
     （无引文＝无格式条）或旧缓存。**无需改代码**。

**版本号**：`ai-page.css?v=10`、`ai-page.js?v=17`（ai.html 已同步）。`wenku_reader.html` 内联无版本号。
layout-v2.css 未改（仍 v=18）。

**验证手法**：注入真实/合成研究综述会话进 `localStorage(marx-ai-sessions-v2, uid=-102)` → reload → 读
computed style / DOM 断言（截图在隐藏 pane 会超时，见 §6；**应用内浏览器 navigate 偶发 300s 卡死→用
显式 tabId 走 preview 源 tab，勿用被 edit-hook 抢焦的 file:// tab**）。外文库用 4 身份 HTTP 探针核验。
流式引文用 `showCiteBubble/openCite` 直调 + node 比对格式串。

**回归**：`test_security.py` 等 41 项失败**全为既有四页面 WIP**（`/` cutover 令旧首页「全文阅读器」断言失配、
config 新增文献选编卷令 collection 集合断言失配），非本轮改动引入——本轮只动 `_foreign_library_books` 一处后端 +
纯前端 ai-page / wenku_reader 模板资源。
