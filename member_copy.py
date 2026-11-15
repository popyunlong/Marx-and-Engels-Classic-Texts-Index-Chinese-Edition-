"""Stable, editable membership presentation keys; no accounting writes."""
from member_catalog import PRICING_NOTICE_ITEMS, _default_public_plan

PLAN_CODES = ("support_plus_20", "support_pro", "support_max")
NOTICE_IDS = ("purchase", "legacy", "renewal", "release", "capacity", "queue")
FIELDS = {"name": "名称", "description": "说明", "features": "卖点（每行一条）"}


def definitions():
    from site_content import SiteTextDefinition
    result = []
    for code in PLAN_CODES:
        plan = _default_public_plan({"code": code})
        for field, label in FIELDS.items():
            result.append(SiteTextDefinition(f"pricing.plan.{code}.{field}", "会员文案 · " + plan["name"],
                                            label, plan[field], multiline=field != "name"))
    for name, (title, body) in zip(NOTICE_IDS, PRICING_NOTICE_ITEMS):
        for field, value in (("title", title), ("body", body)):
            result.append(SiteTextDefinition(f"pricing.notice.{name}.{field}", "会员黄框 · " + title,
                                            "标题" if field == "title" else "正文", value, multiline=field == "body"))
    return result


def keys():
    return {item.key for item in definitions()} | {"pricing.payment_enabled", "pricing.payment_disabled"}


def plan_view(plan, texts):
    result = dict(plan)
    if plan["code"] in PLAN_CODES:
        for field in FIELDS:
            key = f"pricing.plan.{plan['code']}.{field}"
            if key in texts:
                result[field] = texts[key]
    return result


def notices(texts):
    return [(texts.get(f"pricing.notice.{name}.title", title), texts.get(f"pricing.notice.{name}.body", body))
            for name, (title, body) in zip(NOTICE_IDS, PRICING_NOTICE_ITEMS)]


def editor(texts):
    from site_content import get_site_text_map
    defaults = get_site_text_map()
    groups = []
    for code in PLAN_CODES:
        fields = [f"pricing.plan.{code}.{field}" for field in FIELDS]
        groups.append({"id": code, "title": _default_public_plan({"code": code})["name"], "keys": fields})
    groups.append({"id": "intro", "title": "黄框引导语", "keys": ["pricing.payment_enabled", "pricing.payment_disabled"]})
    for name, (title, _) in zip(NOTICE_IDS, PRICING_NOTICE_ITEMS):
        groups.append({"id": name, "title": "黄框 · " + title,
                       "keys": [f"pricing.notice.{name}.title", f"pricing.notice.{name}.body"]})
    labels = {d.key: d.label for d in definitions()}
    labels.update({"pricing.payment_enabled": "支付已启用时", "pricing.payment_disabled": "支付未启用时"})
    for group in groups:
        group["entries"] = [{"key": key, "label": labels[key], "value": texts.get(key, defaults[key])}
                            for key in group["keys"]]
    return groups
