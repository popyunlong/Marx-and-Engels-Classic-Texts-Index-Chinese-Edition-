from __future__ import annotations

from admin_store import get_setting, init_admin_store_db
from membership import get_membership_snapshot, normalize_email


FEATURE_ACCESS_KEYS = ("search", "viewer", "library", "dictionary", "static_library", "stream_reading", "journal_alerts", "notes", "ai", "search_chat", "associative", "research", "ai_web")
FEATURE_ACCESS_LABELS = {
    "search": "检索",
    "viewer": "检索结果正文",
    "library": "单独阅读器",
    "dictionary": "马克思主义大辞典",
    "static_library": "原文文库",
    "stream_reading": "流式阅读",
    "journal_alerts": "期刊提醒",
    "notes": "笔记与知识库",
    "ai": "AI 导学（阅读器）",
    "search_chat": "AI 随心问",
    "associative": "联想检索",
    "research": "研究型检索",
    "ai_web": "AI 联网（智谱）",
}
# 一句话说明每项权限（控制台展示，帮助管理员区分四类 AI 能力）。
FEATURE_ACCESS_HINTS = {
    "search": "全文精确/模糊检索",
    "viewer": "查看命中所在正文页",
    "library": "独立阅读器逐卷阅读",
    "dictionary": "马克思主义大辞典查词",
    "static_library": "中外文原著文库",
    "stream_reading": "《马克思恩格斯文集》网页适配阅读",
    "journal_alerts": "期刊订阅与提醒",
    "notes": "阅读器记笔记 + 「我的知识库」跨书聚合",
    "ai": "阅读器内 AI 导学讲解",
    "search_chat": "首页右下「AI 随心问」对话",
    "associative": "凭大意/残句定位特定原文",
    "research": "研究命题铺开多部相关引文",
    "ai_web": "智谱 GLM 联网检索通道",
}
# 控制台权限分组：把同类功能聚到一张子表里，避免十余项平铺难辨。每个键必须且只属于一组。
FEATURE_ACCESS_GROUPS = (
    {"label": "内容与功能", "features": ("search", "viewer", "library", "dictionary", "static_library", "stream_reading", "journal_alerts", "notes")},
    {"label": "AI 功能（由「AI 导学」拆分而来，可分别开放）", "features": ("ai", "search_chat", "associative", "research", "ai_web")},
)
# ai_web（智谱联网通道）默认全站关闭：仅管理员显式勾选（全站/套餐/个人任一层）后才放开。
DEFAULT_FEATURE_ACCESS = {key: key != "ai_web" for key in FEATURE_ACCESS_KEYS}
AUDIENCE_ACCESS_LABELS = {
    "guest": "访客",
    "registered": "注册用户",
}
DEFAULT_AUDIENCE_ACCESS = {
    "guest": {"search": True, "viewer": False, "library": False, "dictionary": False, "ai": False, "search_chat": False, "associative": False, "research": False, "ai_web": False, "journal_alerts": False, "static_library": False, "stream_reading": False, "notes": False},
    "registered": {"search": True, "viewer": False, "library": False, "dictionary": False, "ai": False, "search_chat": False, "associative": False, "research": False, "ai_web": False, "journal_alerts": False, "static_library": False, "stream_reading": False, "notes": False},
}


def _inherit_split_features(values: dict) -> None:
    """迁移兼容：「AI 随心问」「联想检索」「研究型检索」均由「AI 导学」拆分而来。已保存的策略里缺这些
    键时，继承同层既有取值，保证拆分上线瞬间所有人的可用性与拆分前完全一致；管理员保存任一层权限后
    写入显式值，此后各自独立配置。继承链：associative←ai；search_chat←ai；research←associative(其本身可能←ai)。"""
    if "associative" not in values and "ai" in values:
        values["associative"] = values["ai"]
    if "search_chat" not in values and "ai" in values:
        values["search_chat"] = values["ai"]
    if "research" not in values:
        if "associative" in values:
            values["research"] = values["associative"]
        elif "ai" in values:
            values["research"] = values["ai"]


def load_access_policy(*, include_saved: bool = True) -> dict:
    if include_saved:
        init_admin_store_db()
        policy = get_setting("access_policy", {})
    else:
        policy = {}
    if not isinstance(policy, dict):
        policy = {}

    global_defaults = dict(DEFAULT_FEATURE_ACCESS)
    raw_global = policy.get("global")
    if isinstance(raw_global, dict):
        for key in FEATURE_ACCESS_KEYS:
            if key in raw_global:
                global_defaults[key] = bool(raw_global[key])
        # 旧策略缺派生键：继承全站既有取值（见 _inherit_split_features 说明）。
        if raw_global and "associative" not in raw_global:
            global_defaults["associative"] = global_defaults["ai"]
        if raw_global and "search_chat" not in raw_global:
            global_defaults["search_chat"] = global_defaults["ai"]
        if raw_global and "research" not in raw_global:
            global_defaults["research"] = global_defaults["associative"]

    user_overrides: dict[str, dict[str, bool | None]] = {}
    raw_users = policy.get("users")
    if isinstance(raw_users, dict):
        for email, values in raw_users.items():
            normalized_email = normalize_email(str(email))
            if not normalized_email or not isinstance(values, dict):
                continue
            normalized_values: dict[str, bool | None] = {
                key: (None if values.get(key) is None else bool(values.get(key)))
                for key in FEATURE_ACCESS_KEYS
                if key in values
            }
            _inherit_split_features(normalized_values)
            user_overrides[normalized_email] = normalized_values

    plan_rules: dict[str, dict[str, bool]] = {}
    raw_plans = policy.get("plans")
    if isinstance(raw_plans, dict):
        for plan_code, values in raw_plans.items():
            normalized_code = str(plan_code or "").strip()
            if not normalized_code or not isinstance(values, dict):
                continue
            normalized_plan: dict[str, bool] = {
                key: bool(values[key])
                for key in FEATURE_ACCESS_KEYS
                if key in values
            }
            _inherit_split_features(normalized_plan)
            plan_rules[normalized_code] = normalized_plan

    audience_rules = {name: dict(values) for name, values in DEFAULT_AUDIENCE_ACCESS.items()}
    raw_audience = policy.get("audience")
    if isinstance(raw_audience, dict):
        for audience_key in AUDIENCE_ACCESS_LABELS:
            values = raw_audience.get(audience_key)
            if not isinstance(values, dict):
                continue
            for key in FEATURE_ACCESS_KEYS:
                if key in values:
                    audience_rules[audience_key][key] = bool(values[key])
            if "associative" not in values and "ai" in values:
                audience_rules[audience_key]["associative"] = bool(values["ai"])
            if "search_chat" not in values and "ai" in values:
                audience_rules[audience_key]["search_chat"] = bool(values["ai"])
            if "research" not in values:
                if "associative" in values:
                    audience_rules[audience_key]["research"] = bool(values["associative"])
                elif "ai" in values:
                    audience_rules[audience_key]["research"] = bool(values["ai"])

    return {
        "global": global_defaults,
        "audience": audience_rules,
        "plans": plan_rules,
        "users": user_overrides,
    }


def membership_plan_code_for_user(user: dict | None) -> str:
    if not user:
        return ""
    for key in ("membership_plan_code", "plan_code"):
        value = str(user.get(key) or "").strip()
        if value:
            return value
    user_id = user.get("id")
    if not user_id:
        return ""
    try:
        snapshot = get_membership_snapshot(int(user_id))
    except Exception:
        return ""
    return str(snapshot.plan_code or "").strip() if snapshot.is_active_member else ""


def is_admin_user(user: dict | None) -> bool:
    return bool(user and str(user.get("role") or "").strip().lower() == "admin")


def feature_allowed_by_policy(policy: dict, feature: str, user: dict | None = None) -> bool:
    if feature not in FEATURE_ACCESS_KEYS:
        return True
    if not user:
        return bool((policy.get("audience") or {}).get("guest", {}).get(feature, False))

    if not is_admin_user(user):
        plan_code = membership_plan_code_for_user(user)
        if not plan_code:
            allowed = bool((policy.get("audience") or {}).get("registered", {}).get(feature, False))
            email = normalize_email(str(user.get("email") or ""))
            override = (policy.get("users") or {}).get(email, {}).get(feature) if email else None
            return allowed if override is None else bool(override)

    allowed = bool((policy.get("global") or {}).get(feature, True))
    plan_code = membership_plan_code_for_user(user)
    plan_values = (policy.get("plans") or {}).get(plan_code, {}) if plan_code else {}
    if feature in plan_values:
        allowed = bool(plan_values[feature])
    email = normalize_email(str((user or {}).get("email") or ""))
    override = (policy.get("users") or {}).get(email, {}).get(feature) if email else None
    return allowed if override is None else bool(override)


def feature_effective_for_user(feature: str, user: dict | None = None, *, include_saved: bool = True) -> bool:
    if feature not in FEATURE_ACCESS_KEYS:
        return True
    return feature_allowed_by_policy(load_access_policy(include_saved=include_saved), feature, user)


def plan_feature_access_rows(plans: list[dict], policy: dict) -> list[dict]:
    rows = []
    for plan in plans:
        code = str(plan.get("code") or "").strip()
        values = {
            key: bool((policy.get("plans") or {}).get(code, {}).get(key, policy["global"].get(key, True)))
            for key in FEATURE_ACCESS_KEYS
        }
        rows.append({"plan": plan, "values": values})
    return rows


def audience_feature_access_rows(policy: dict) -> list[dict]:
    rows = []
    audience_values = policy.get("audience") or {}
    for key, label in AUDIENCE_ACCESS_LABELS.items():
        values = {
            feature: bool(audience_values.get(key, {}).get(feature, False))
            for feature in FEATURE_ACCESS_KEYS
        }
        rows.append({"key": key, "label": label, "values": values})
    return rows


def feature_access_rows(users: list[dict], policy: dict) -> list[dict]:
    rows = []
    for user in users:
        email = normalize_email(str(user.get("email") or ""))
        overrides = dict((policy.get("users") or {}).get(email, {}))
        effective = {
            key: feature_allowed_by_policy(policy, key, user)
            for key in FEATURE_ACCESS_KEYS
        }
        rows.append({"user_id": user.get("id"), "email": email, "overrides": overrides, "effective": effective})
    return rows
