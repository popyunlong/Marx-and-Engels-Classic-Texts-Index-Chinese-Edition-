"""Live plan model rights, independent of historical wallet/accounting snapshots."""
from __future__ import annotations

import json
import sqlite3

KEY = "ai_model_policies_v1"
FLASH = "deepseek-v4-flash"
PRO = "deepseek-v4-pro"
PLAN_LABELS = {"support_plus_20": "Plus", "support_basic": "Plus（原15元）",
               "support_plus": "Plus（原25元）", "support_pro": "Pro", "support_max": "Max"}
CHOICES = ((FLASH, "off", "Flash 非思考"), (FLASH, "high", "Flash 思考"),
           (PRO, "high", "DeepSeek Pro 高强度思考"))


def default_policy(code):
    premium = code in {"support_pro", "support_max"}
    return make_policy(premium, premium, "pro" if premium else "flash")


def make_policy(flash_thinking, pro, research):
    selections = {"flash": {"model": FLASH, "reasoning_effort": "off"},
                  "thinking": {"model": FLASH, "reasoning_effort": "high"},
                  "pro": {"model": PRO, "reasoning_effort": "high"}}
    if research not in selections or (research == "thinking" and not flash_thinking) or (research == "pro" and not pro):
        raise ValueError("研究默认模型必须属于该套餐已开放的模型与思考档位。")
    models = {FLASH: ["off", "high"] if flash_thinking else ["off"]}
    if pro:
        models[PRO] = ["high"]
    return {"models": models, "defaults": {"quick": dict(selections["flash"]),
            "reader": dict(selections["flash"]), "research": dict(selections[research])}}


def read(conn=None):
    # Deliberately bypass the process TTL. Admission/reservation must see revocation.
    if conn is None:
        from membership import _connect
        with _connect() as db:
            return read(db)
    try:
        row = conn.execute("SELECT value_json FROM unified_settings WHERE key=?", (KEY,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        row = None
    try:
        data = json.loads(row[0]) if row else {}
        if not isinstance(data, dict) or not isinstance(data.get("plans", {}), dict):
            raise ValueError("invalid policy document")
        data["version"] = max(0, int(data.get("version") or 0))
    except (ValueError, TypeError):
        data = {}
    return {"version": int(data.get("version") or 0), "plans": data.get("plans", {})}


def for_wallet(wallet, settings):
    code = str(wallet["plan_code"])
    if code not in PLAN_LABELS:
        return wallet["model_policy"]
    policy = settings["plans"].get(code)
    if policy is None:
        return default_policy(code)
    # Only the bounded model combinations supported by the website may be used.
    try:
        models = policy["models"]
        selection = policy["defaults"]["research"]
        research = "pro" if selection["model"] == PRO else "thinking" if selection["reasoning_effort"] == "high" else "flash"
        return make_policy("high" in models.get(FLASH, []), "high" in models.get(PRO, []), research)
    except (KeyError, TypeError, ValueError, AttributeError):
        return make_policy(False, False, "flash")


def save(code, policy, *, expected_version, actor):
    from membership import _connect, utc_now_text
    from admin_store import _settings_cache_invalidate
    if code not in PLAN_LABELS:
        raise ValueError("该套餐的独立历史权益不在此编辑。")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        data = read(conn)
        if data["version"] != expected_version:
            raise ValueError("配置已被另一窗口修改，请刷新后再保存。")
        before = for_wallet({"plan_code": code}, data)
        if policy is None:
            data["plans"].pop(code, None)
        else:
            data["plans"][code] = policy
        data["version"] += 1
        conn.execute("INSERT INTO unified_settings(key,value_json,updated_at,updated_by) VALUES(?,?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                     (KEY, json.dumps(data, ensure_ascii=False), utc_now_text(), actor))
        conn.commit()
    _settings_cache_invalidate(KEY)
    return before, for_wallet({"plan_code": code}, data)


def editor():
    data = read()
    rows = []
    for code, label in PLAN_LABELS.items():
        policy = for_wallet({"plan_code": code}, data)
        selection = policy["defaults"]["research"]
        rows.append(dict(code=code, label=label, version=data["version"],
                         flash_thinking="high" in policy["models"][FLASH], pro=PRO in policy["models"],
                         research="pro" if selection["model"] == PRO else "thinking" if selection["reasoning_effort"] == "high" else "flash"))
    return rows
