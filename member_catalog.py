"""Public membership contract; historical product codes remain accounting identities."""
PLUS_CODE = "support_plus_20"
RETIRED_PLUS_CODES = frozenset({"support_basic", "support_plus"})
PLUS_CODES = RETIRED_PLUS_CODES | {PLUS_CODE}
LEGACY_CODES = frozenset({"monthly", "quarter", "quarterly", "yearly"})


def tier(code):
    return "max" if code == "support_max" else "pro" if code == "support_pro" else "plus" if code in PLUS_CODES else "legacy" if code in LEGACY_CODES else "free"


def effective_rank(code, rank=0):
    return 120 if code in PLUS_CODES else int(rank or 0)


def priority(code):
    return {"max": 2, "pro": 1}.get(tier(code), 0)


def api_eligible(code):
    return code in LEGACY_CODES or tier(code) in {"pro", "max"}


def public_plan(plan):
    """Current capabilities replace stale marketing copy, never accounting fields."""
    result = dict(plan)
    code = plan["code"]
    if code == PLUS_CODE:
        result.update(name="Plus", description="满足日常文献阅读、检索与 AI 辅助研究需求，享有完整网页会员功能。",
            features="\n".join((
                "文献检索与阅读：全文与篇章检索、原文定位、原典阅读器、马克思主义大辞典、概念地图、联想检索、原文文库与流式阅读。",
                "个人资料与笔记：个人文库上传、阅读与检索；跨书整理笔记，支持 Word、Markdown、JSON 导出及返回原文。",
                "AI 辅助研究：AI 导学，以及 AI 研究对话的快速问答、研究综述、综合模式；支持 DeepSeek Flash 非思考模式与 DeepSeek Pro 高强度思考模式，提供联网搜索、网页阅读和论文插注校注。",
                "研究动态：完整国内外研究周报、双语题录与引文导出，支持自主选择期刊接收邮件。",
                "AI 使用额度：每周最高 100 万 Flash 等值 token，周一重置周限额；不同模型折算消耗，共享套餐 AI 额度，并受每 30 天总额度约束。",
                "不含 API／MCP 数据调用权限。",
            )))
    elif code in {"support_pro", "support_max"}:
        label = "Pro" if code == "support_pro" else "Max"
        is_max = label == "Max"
        result.update(name=label,
            description=("包含 Pro 的全部网页与 API／MCP 功能，提供三档套餐中最高的 AI 用量与排队优先级，适合持续、高强度的研究与写作。"
                         if is_max else "包含 Plus 的全部网页功能，增加 API／MCP 权限、更高 AI 用量及优先排队，适合较高强度的研究与写作。"),
            features="\n".join((
                "API／MCP：套餐内可自助开通，数据调用享有最高排队优先级。" if is_max else "API／MCP：套餐内可自助开通，支持通过外部工具查询公共文献。",
                "AI 模型：支持 DeepSeek Flash 非思考／思考模式，以及 DeepSeek Pro 高强度思考模式。",
                ("每 30 天典型 AI 容量：全部使用 Flash 时约 2,109–4,219 万 token；全部使用 DeepSeek Pro 时约 536–1,071 万 token。"
                 if is_max else "每 30 天典型 AI 容量：全部使用 Flash 时约 1,266–2,531 万 token；全部使用 DeepSeek Pro 时约 321–643 万 token。"),
                "两种模型共享同一份额度，可混合使用；上述容量不能相加。不另设每周 token 上限，仍受套餐总额度约束。",
                "快速问答、研究综述、综合模式均享有" + ("最高优先排队。" if is_max else "优先排队。"),
            )))
    else:
        result["description"] = str(result.get("description") or "").replace("MiMo V2.5 Pro", "DeepSeek Flash").replace("MiMo", "DeepSeek")
        result["features"] = str(result.get("features") or "").replace("MiMo V2.5 Pro", "DeepSeek Flash").replace("MiMo", "DeepSeek")
    return result


# Render these as data so historical auto-text overrides cannot restore obsolete
# numbering or hide the current allowance explanation. Accounting is unchanged.
PRICING_NOTICE_ITEMS = (
    ("购买规则", "已生效的旧套餐与一个新套餐可以并行；Plus / Pro / Max 三档之间互斥；原 15/25 元套餐已停售，原 AI 额度保留。"),
    ("早期会员承诺", "北京时间 2026 年 7 月 4 日零点前购买且仍有效的旧会员，按月／季／年档每周享有 28／49／77 万实际输入与输出 token，周一恢复。每天独立赠送 20 次联网。旧权益支持 DeepSeek Flash 非思考模式，另购套餐的额度单独计算。"),
    ("续费与升降档", "同档续费排在当前周期结束后，升档按剩余秒数补差并立即生效，降档需等下个周期。"),
    ("AI 额度开放", "AI 额度每个 30 天周期分两次开放：周期开始时开放 50%，第 15 天开放余下 50%，周期结束时到期。"),
    ("AI 容量说明", "套餐所列容量按当前计费配置、缓存未命中及 80% 输入／20% 输出估算，范围对应高峰至低谷时段，并非固定对话次数。AI 研究对话与其他消耗套餐额度的 AI 功能共享余额；实际可用量随模型、上下文长度、思考输出、缓存命中及联网工具使用情况变化。"),
    ("排队规则", "排队优先级不打断已经开始的任务。"),
)


BENEFIT_GROUPS = (
    {"title": "阅读与检索", "features": tuple(
        {"key": key, "label": label, "hint": hint} for key, label, hint in (
            ("search", "全文与篇章检索", "精确、模糊、同段共现及篇章范围检索"),
            ("viewer", "原文定位", "查看命中页、印刷页码与引用出处"),
            ("library", "原典阅读器", "逐卷阅读原书"),
            ("dictionary", "马克思主义大辞典", "词条检索与释义"),
            ("concept_map", "概念地图", "概念关系、关联路径及出处"),
            ("associative", "联想检索", "凭大意或残句定位原文"),
            ("static_library", "原文文库", "中外文原著阅读"),
            ("stream_reading", "流式阅读", "适配网页的阅读与翻译"),
            ("personal_library", "个人文库", "自有资料上传、私有阅读与检索"),
            ("notes", "笔记与知识库", "跨书整理、Word/Markdown/JSON 导出与返回原文"),
        ))},
    {"title": "AI 与研究服务", "features": tuple(
        {"key": key, "label": label, "hint": hint} for key, label, hint in (
            ("ai", "AI 导学", "阅读器内的讲解与问答"),
            ("search_chat", "AI 研究对话：快速问答", "结合引文库开展连续问答"),
            ("research", "AI 研究对话：研究综述", "围绕命题检索、核验引文与写作"),
            ("comprehensive", "AI 研究对话：综合模式", "按需规划、检索与分阶段写作"),
            ("deepseek_web", "联网搜索与网页阅读", "按账户日额度与 AI 余额使用"),
            ("citation_assistant", "论文插注校注", "引文核验与文稿导出"),
            ("journal_alerts", "国内外研究动态", "完整周报、双语题录、引文导出与自主选刊邮件"),
            ("agent_api", "API/MCP 数据调用", "Pro/Max 套餐内自助开通；保留合格老会员权益"),
        ))},
)
