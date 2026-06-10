from __future__ import annotations

from admin_store import get_setting, init_admin_store_db
from membership import get_membership_snapshot, normalize_email


FEATURE_ACCESS_KEYS = ("search", "viewer", "library", "ai", "ai_web", "journal_alerts")
FEATURE_ACCESS_LABELS = {
    "search": "检索",
    "viewer": "检索结果正文",
    "library": "单独阅读器",
    "ai": "AI 导学",
    "ai_web": "AI 联网（智谱）",
    "journal_alerts": "期刊提醒",
}
# ai_web（智谱联网通道）默认全站关闭：仅管理员显式勾选（全站/套餐/个人任一层）后才放开。
DEFAULT_FEATURE_ACCESS = {key: key != "ai_web" for key in FEATURE_ACCESS_KEYS}
AUDIENCE_ACCESS_LABELS = {
    "guest": "访客",
    "registered": "注册用户",
}
DEFAULT_AUDIENCE_ACCESS = {
    "guest": {"search": True, "viewer": False, "library": False, "ai": False, "ai_web": False, "journal_alerts": False},
    "registered": {"search": True, "viewer": False, "library": False, "ai": False, "ai_web": False, "journal_alerts": False},
}


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

    user_overrides: dict[str, dict[str, bool | None]] = {}
    raw_users = policy.get("users")
    if isinstance(raw_users, dict):
        for email, values in raw_users.items():
            normalized_email = normalize_email(str(email))
            if not normalized_email or not isinstance(values, dict):
                continue
            user_overrides[normalized_email] = {
                key: (None if values.get(key) is None else bool(values.get(key)))
                for key in FEATURE_ACCESS_KEYS
                if key in values
            }

    plan_rules: dict[str, dict[str, bool]] = {}
    raw_plans = policy.get("plans")
    if isinstance(raw_plans, dict):
        for plan_code, values in raw_plans.items():
            normalized_code = str(plan_code or "").strip()
            if not normalized_code or not isinstance(values, dict):
                continue
            plan_rules[normalized_code] = {
                key: bool(values[key])
                for key in FEATURE_ACCESS_KEYS
                if key in values
            }

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
