# 研究型检索每周次数额度功能接手说明

> ✅ 已完成并上线（2026-06-17，DEPLOYED_SHA=bdae9e5-dirty）。
> 收尾改动：补 `/admin/research-quota` 路由；`api_search_associative` 加 mode==research 前置 429 拦截
> 与 auto→research 综述前二次校验；研究综述记账 feature 改为 `research_review`、响应回带 `research_quota`；
> control.html 额度卡 + index.html 徽章/`updateResearchQuota()`；新增 4 条额度测试（全绿，原 45 条亦通过）。
> 部署前已核对：ai.py / site_content.py / index.html 检索标签 UI 系另一并行会话改动，**早已上线**
> （server sha256 与本地一致），整树部署净增仅本功能。下文为原始接手记录，保留备查。

更新时间：2026-06-17

## 用户需求

用户希望在网页后台控制台中可配置“研究型检索”的使用次数，并在前端显示剩余额度。

默认额度要求：

- 登录用户：每周 10 次
- 月度会员：每周 30 次
- 季度会员：每周 45 次
- 年度会员：每周 60 次

前端要求：

- 首页研究型检索区域显示本周剩余额度。
- 用户提交研究型检索后，返回结果里也应带回更新后的剩余额度。
- 达到额度后，服务端应拦截，不应继续消耗 AI。

## 当前工作状态

本轮功能尚未完成，处于“部分代码已写入，未测试，未部署”的状态。

已经动过的文件：

- `membership.py`
- `app.py`

尚未动或尚未完成的文件：

- `templates/control.html`
- `templates/index.html`
- `tests/test_associative_search.py`
- 可能需要新增或扩展测试

## 已完成的代码改动

### 1. `membership.py`

已新增函数：

```python
def count_ai_usage_requests(
    *,
    user_id: int | None,
    session_key: str = "",
    start_day: str,
    end_day: str,
    feature: str = "",
    success_only: bool = True,
) -> int:
```

用途：

- 基于 `ai_usage` 表统计某用户或某 session 在指定日期范围内某个 AI 功能的请求次数。
- 当前计划用于统计 `feature="research_review"` 的本周成功次数。

注意：

- `ai_usage.day` 是 `YYYY-MM-DD` 字符串，可用文本范围比较。
- 默认只统计 `success = 1`。

### 2. `app.py`

已在 membership import 中加入：

```python
count_ai_usage_requests,
```

已新增常量，位于 `RATE_LIMITS` 后：

```python
RESEARCH_WEEKLY_QUOTA_SETTING_KEY = "research_weekly_quota"
RESEARCH_WEEKLY_QUOTA_DEFAULTS = {
    "registered": 10,
    "monthly": 30,
    "quarterly": 45,
    "yearly": 60,
}
RESEARCH_WEEKLY_QUOTA_LABELS = {
    "registered": "登录用户",
    "monthly": "月度会员",
    "quarterly": "季度会员",
    "yearly": "年度会员",
}
RESEARCH_QUOTA_FEATURE = "research_review"
```

已新增服务端额度辅助函数，位于 `_require_local_console()` 后、`current_view_state()` 前：

```python
def _research_weekly_quota_settings() -> dict[str, int]:
    ...

def _research_quota_week_window() -> dict[str, str]:
    ...

def _research_quota_bucket_for_user(user: dict | None) -> str:
    ...

def _research_quota_payload(user: dict | None = None) -> dict:
    ...
```

设计意图：

- 后台配置存入 `admin_store` 的统一设置表，key 为 `research_weekly_quota`。
- 如果后台没有设置，则使用默认值。
- 周期按北京时间周一至周日计算。
- 当前用户根据会员套餐归类为 `registered/monthly/quarterly/yearly`。
- 套餐归类优先看 `membership.plan_code`，其次回查 `plans.interval_months`：
  - `>=12` 视为年度
  - `>=3` 视为季度
  - `>=1` 视为月度
  - 非会员或未识别视为登录用户

已在 `current_view_state()` 返回值中加入：

```python
"research_quota": _research_quota_payload() if has_request_context() else {},
```

已在 `_management_console_context()` 返回值中加入：

```python
"research_quota_settings": _research_weekly_quota_settings(),
"research_quota_labels": RESEARCH_WEEKLY_QUOTA_LABELS,
"control_research_quota_url": url_for("admin_research_quota") if remote_admin else "",
```

已新增 `_handle_research_quota_submit()`，位于 `_handle_plans_submit()` 后：

```python
def _handle_research_quota_submit(*, remote_admin: bool):
    ...
```

用途：

- 后台提交每周额度设置。
- 保存到 `set_setting("research_weekly_quota", values, ...)`。

## 当前未完成且必须补上的内容

### 1. 补后台路由

在 `app.py` 的后台路由区加入：

```python
@app.post("/admin/research-quota")
def admin_research_quota():
    return _handle_research_quota_submit(remote_admin=True)
```

建议放在 `/admin/plans` 附近。

本地 `/control` 不需要开放，因为当前项目策略是运营配置只在网站 `/admin` 改。

### 2. 服务端研究型检索拦截

位置：

`app.py` 中 `api_search_associative()`，大约在：

```python
mode = str(payload.get("mode") or "auto").strip().lower()
...
if not gist:
```

或者在 `_requested_mode` 已判断为 research 后尽早拦截。

建议逻辑：

```python
research_quota = None
if mode == "research":
    research_quota = _research_quota_payload(getattr(g, "current_user", None))
    if not research_quota.get("allowed"):
        return jsonify({
            "ok": False,
            "error": f"本周研究型检索次数已用完（{research_quota['used']}/{research_quota['limit']}），下周一自动恢复。",
            "research_quota": research_quota,
        }), 429
```

注意：

- 不要在 AI expand 之后才拦截，否则超额用户仍会消耗 AI。
- 如果 `limit=0` 的语义按“0 次”处理，目前 `_research_quota_payload` 会 `allowed=False`。这符合“次数控制”的直觉。

### 3. 研究型检索成功后记账 feature 需改为 `research_review`

当前 `api_search_associative()` 的研究型分支在生成综述后记录：

```python
_record_ai_usage(
    quota, feature="associative", prompt_parts=(gist,),
    completion_text=review_md, success=True,
)
```

需要改成：

```python
_record_ai_usage(
    quota, feature=RESEARCH_QUOTA_FEATURE, prompt_parts=(gist,),
    completion_text=review_md, success=True,
)
```

并在返回 JSON 中附带更新后的额度：

```python
"research_quota": _research_quota_payload(getattr(g, "current_user", None)),
```

注意：

- 研究型分支失败 fallback 但仍生成了综述，也应视为一次成功使用，因为用户拿到了综述结果。
- 无候选时目前记录 `feature="associative"` 并返回普通 associative 空结果；如果 intent 是 research 但无候选，是否扣次数需要产品判断。建议“不扣”，因为没有生成综述文章。

### 4. 后台模板 `templates/control.html`

在会员与权限模块中，建议放在“套餐管理”下方或上方，新增一个卡片：

```html
<section class="card" id="research-quota">
  <h2>研究型检索额度</h2>
  <p class="section-note">按北京时间周一至周日统计。达到额度后，本周不能继续生成研究型综述；下周一自动恢复。</p>
  <form class="item" method="post" action="{{ control_research_quota_url }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <div class="form-grid">
      {% for key, label in research_quota_labels.items() %}
      <label class="field">{{ label }}每周次数
        <input type="number" name="research_quota_{{ key }}" value="{{ research_quota_settings[key] }}" min="0">
      </label>
      {% endfor %}
    </div>
    <div class="actions">
      <button class="btn primary" type="submit">保存研究型检索额度</button>
    </div>
  </form>
</section>
```

### 5. 首页模板 `templates/index.html`

现有研究型检索说明位置：

```html
<div class="research-note hidden" id="researchNote">
  <span class="research-note-dot" aria-hidden="true"></span>
  <span><strong>{{ site_text('search.research_tab') }}</strong>：{{ site_text('search.research_note') }}</span>
</div>
```

建议改成：

```html
<div class="research-note hidden" id="researchNote">
  <span class="research-note-dot" aria-hidden="true"></span>
  <span>
    <strong>{{ site_text('search.research_tab') }}</strong>：{{ site_text('search.research_note') }}
    {% if state.research_quota %}
    <span class="research-quota" id="researchQuotaText">{{ state.research_quota.message }}</span>
    {% endif %}
  </span>
</div>
```

JS 中在 `state` 增加：

```js
researchQuota: {{ state.research_quota | tojson }}
```

新增函数：

```js
function updateResearchQuota(quota) {
  if (!quota) return;
  state.researchQuota = quota;
  const el = document.getElementById('researchQuotaText');
  if (el) el.textContent = quota.message || `本周剩余 ${quota.remaining}/${quota.limit} 次`;
}
```

在 `doAssociativeSearch()` 中，收到 data 后：

```js
if (data.research_quota) updateResearchQuota(data.research_quota);
```

如果超额返回 429，当前逻辑会走：

```js
if (!data.ok) {
  renderEmpty(data.error || '未知错误');
  return;
}
```

需要在这之前或里面也更新：

```js
if (data.research_quota) updateResearchQuota(data.research_quota);
```

### 6. 可选：文案后台化

用户没有明确要求额度提示文案也后台可控，但之前多次要求文案纳入内容运营。建议新增 `site_content.py` 文案：

- `search.research_quota_prefix`
- `search.research_quota_exhausted`

也可以先不做，避免扩大改动面。

## 测试建议

至少跑：

```powershell
python -m py_compile app.py membership.py
python scripts\check_inline_js.py
python -m pytest tests\test_auto_site_text.py tests\test_deploy_manifests.py -q
python -m pytest tests\test_associative_search.py -q --tb=short
```

建议新增/扩展测试点：

1. 默认配置返回：
   - 非会员/登录用户 limit=10
   - 月度会员 limit=30
   - 季度会员 limit=45
   - 年度会员 limit=60

2. 超额拦截：
   - 手动插入本周 `ai_usage` 里 10 条 `feature="research_review"` 且 `success=1`
   - 研究型检索接口返回 429
   - 不调用 `AI_CLIENT.expand_associative_query`

3. 成功后扣次数：
   - research 模式成功返回 `research_quota`
   - `remaining` 减少

## 部署方式

此前部署方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\update_cloud.ps1 -DryRun -AllowDirty
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\update_cloud.ps1 -AllowDirty
```

部署后验证：

```powershell
Invoke-WebRequest -UseBasicParsing -Uri 'https://mazhuzuojiansuo.com/api/runtime' -TimeoutSec 30
Invoke-WebRequest -UseBasicParsing -Uri 'https://mazhuzuojiansuo.com/' -TimeoutSec 30
ssh -i "$HOME\.ssh\id_marx_cloud_ed25519" -o BatchMode=yes -o ConnectTimeout=10 root@38.76.174.234 "cat /opt/marx-search/DEPLOYED_SHA && systemctl is-active marx-search"
```

## 当前风险与注意事项

- 当前代码是中途状态，`app.py` 已引用 `count_ai_usage_requests`，但还没有完成后台路由和前台模板，必须继续完成后再测试部署。
- `_research_quota_payload()` 里会调用 `_visitor_session_key()`，该函数定义在后面；Python 运行时只要调用发生在模块加载完成后即可，通常没问题。
- 研究型分支当前仍记录 `feature="associative"`，这会导致新增统计函数暂时统计不到研究型成功次数；必须改成 `feature=RESEARCH_QUOTA_FEATURE`。
- 后台保存函数 `_handle_research_quota_submit()` 已写，但没有路由时无法使用。
- 未运行任何测试，未部署。

