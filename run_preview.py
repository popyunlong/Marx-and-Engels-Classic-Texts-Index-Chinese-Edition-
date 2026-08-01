"""本地四页面布局预览启动器（含权限切换器）。

直接复用 app.py 里已构建好的 Flask app，绕开 main()/run_desktop()——
不触发 pythonw 重启、不自动开浏览器、不启用空闲自动关闭，便于在后台受控运行。

【权限预览】通过 ?as=guest|registered|member|admin 在四种身份间切换，
选择记在 session 里，切页后保持。实现方式是「猴补丁 + 请求钩子」，全部写在本
启动器里，**不改动 app.py 一个字节**，服务器线上不受任何影响。

用法（在项目根目录）：
    python run_preview.py
默认已设 APP_MODE=server（本地才能测 AI）与 MARX_SKIP_NODE_CHECK=1；
端口默认 8011，可用环境变量 PREVIEW_PORT 覆盖。

注意：请在你自己的终端里跑（前台运行、Ctrl+C 退出）。经 AI 的后台工具拉起在本
Windows 环境会被回收（进程起来后随即被收走），无法常驻。
"""
import os
import sys

os.environ.setdefault("MARX_SKIP_NODE_CHECK", "1")
os.environ.setdefault("APP_MODE", "server")  # 预览就是要本地测 AI；缺省即服务器模式

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


# ---- 注入左下浮动的权限切换条（仅预览，HTML 响应才注） -------------------------
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
    print(f"[preview] starting on http://127.0.0.1:{PORT}  (?as=guest|registered|member|admin)", flush=True)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)
