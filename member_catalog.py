"""Public membership contract; historical product codes remain accounting identities."""
PLUS_CODE = "support_plus_20"
PLUS_CODES = frozenset({"support_basic", "support_plus", PLUS_CODE})
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
        result.update(name="Plus", description="20 元 / 30 天；全部网页会员权益，不含 API/MCP。",
            features="每周 100 万 Flash 等值额度，周一恢复\n整期 AI 成本额度 18 元，分批开放\nWord/Markdown/JSON 笔记导出与返回原文\n完整研究动态与自主选刊邮件")
    elif code in {"support_pro", "support_max"}:
        label = "Pro" if code == "support_pro" else "Max"
        result.update(name=label, description="30 天会员；全部网页会员权益与 API/MCP 自助开通。",
            features="DeepSeek 按实际模型成本共享 AI 额度\nAI 额度沿用现行规则\n三种 AI 对话模式" + ("最高优先排队\nAPI/MCP 最高优先" if label == "Max" else "优先排队\nAPI/MCP 套餐内开通"))
    else:
        result["description"] = str(result.get("description") or "").replace("MiMo V2.5 Pro", "DeepSeek Flash").replace("MiMo", "DeepSeek")
        result["features"] = str(result.get("features") or "").replace("MiMo V2.5 Pro", "DeepSeek Flash").replace("MiMo", "DeepSeek")
    return result


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
