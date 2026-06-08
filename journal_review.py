"""期刊文献综述生成：把一个批次抓取到的文章，用 DeepSeek 按五大学科 + 经典/前沿分类，
生成中文文献综述，并在文末确定性地附上 GB/T 7714-2015 引文（全覆盖本批文章）。

设计要点：
- 两段式：先分类（每篇 → 学科 + 经典/前沿），再按学科生成综述散文；大批量分块调用避免超 token。
- 引文不交给 AI 编造，而是用 journal_alerts.gb2015_citation 确定性生成，保证准确且 100% 覆盖。
- 某学科无文章时显式提示，并全程保留「AI 自动判断，可能误判」的免责声明。
- AI 不可用/失败时降级为「仅引文 + 提示」，绝不抛出异常打断采集/发送流程。
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

from journal_alerts import gb2015_citation, _markdown_to_html


# 五大学科领域（固定顺序）。
DISCIPLINES = (
    "马克思主义基本原理",
    "马克思主义发展史",
    "马克思主义中国化研究",
    "国外马克思主义研究",
    "思想政治教育",
)
PROBLEM_TYPES = ("经典问题", "前沿问题")
UNCLASSIFIED = "暂未归类"
DISCLAIMER = (
    "本文献综述及其学科归类、经典/前沿研究划分均由人工智能自动生成，仅供学术参考，"
    "可能存在误判或疏漏，请以原文为准。"
)
_CLASSIFY_CHUNK = 25
_ABSTRACT_CLIP = 320


def build_literature_review(
    articles: list[dict],
    *,
    ai_client: Any = None,
    settings: dict | None = None,
) -> tuple[str, str, str]:
    """生成文献综述。返回 (review_md, review_html, model_used)。"""
    settings = settings or {}
    client, model_used = _resolve_client(ai_client, settings)
    articles = [a for a in (articles or []) if str(a.get("title") or "").strip()]

    period = _period_label(settings)
    if not articles:
        md = (
            f"# 本周马克思主义理论学科文献综述{period}\n\n"
            f"> {DISCLAIMER}\n\n"
            "本周抓取窗口内暂无新公开发表的相关期刊文章。"
        )
        return md, _markdown_to_html(md), model_used

    # 全局引注序号：文末引文按 articles 顺序编号，正文引注 [n] 必须与之严格一致。
    ref_by_id = {int(a["id"]): idx for idx, a in enumerate(articles, 1)}

    # 1) 分类（失败时全部归入「暂未归类」，不打断流程）。
    classified = _classify_articles(articles, client)

    # 2) 按学科生成综述正文——只输出「本期确有文章」的学科，灵活省略空领域。
    cn_index = ("一", "二", "三", "四", "五", "六", "七", "八")
    present: list[tuple[str, list[dict]]] = []
    for discipline in DISCIPLINES:
        items = [a for a in articles if classified.get(int(a["id"]), {}).get("discipline") == discipline]
        if items:
            present.append((discipline, items))
    leftover = [a for a in articles if classified.get(int(a["id"]), {}).get("discipline") not in DISCIPLINES]
    if leftover:
        present.append((UNCLASSIFIED, leftover))

    sections: list[str] = []
    for i, (discipline, items) in enumerate(present):
        label = "其他 / " + UNCLASSIFIED if discipline == UNCLASSIFIED else discipline
        heading = f"## {cn_index[i] if i < len(cn_index) else i + 1}、{label}"
        body = _review_one_discipline(discipline, items, classified, client, ref_by_id)
        sections.append(f"{heading}\n\n{body}")

    covered = [d for d, _ in present if d != UNCLASSIFIED]
    coverage_note = (
        f"本期共收录 {len(articles)} 篇文章，涉及 " + "、".join(covered) + " 等领域。"
        if covered
        else f"本期共收录 {len(articles)} 篇文章。"
    )

    # 3) 文末 GB2015 引文（确定性，全覆盖）。
    citations = _citation_block(articles)

    md = (
        f"# 本周马克思主义理论学科文献综述{period}\n\n"
        f"> {DISCLAIMER}\n\n"
        f"{coverage_note}正文中每处观点后的方括号序号（如 [3]）对应文末「引文」中的同号文献。\n\n"
        + "\n\n".join(sections)
        + "\n\n"
        + citations
    )
    return md, _markdown_to_html(md), model_used


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _resolve_client(ai_client: Any, settings: dict) -> tuple[Any, str]:
    """返回 (可用的 client 或 None, 实际使用的模型名)。

    review_model 留空时沿用运行时生效模型；非空时用 dataclasses.replace 覆盖模型（不改全站配置）。
    """
    if not _client_enabled(ai_client):
        return None, str(settings.get("review_model") or "（AI 未启用）")
    override = str(settings.get("review_model") or "").strip()
    effective = str(getattr(ai_client.config, "model", "") or "")
    if override and override != effective:
        try:
            from ai import ZAIClient  # 延迟导入，避免无谓依赖。

            new_config = dataclasses.replace(ai_client.config, model=override)
            return ZAIClient(new_config), override
        except Exception:
            return ai_client, effective
    return ai_client, effective


def _client_enabled(ai_client: Any) -> bool:
    return bool(ai_client and getattr(getattr(ai_client, "config", None), "enabled", False))


def _chat(client: Any, system: str, user: str, max_tokens: int) -> str:
    raw = client.chat_complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
    )
    return _strip_thinking(raw)


# 推理模型可能把思考过程混在正文里，这里统一剥离，确保综述只剩正文。
_THINK_TAG_RE = re.compile(r"<\s*think\s*>.*?<\s*/\s*think\s*>", re.S | re.I)
_THINK_UNICODE_RE = re.compile(r"◁\s*think\s*▷.*?◁\s*/\s*think\s*▷", re.S | re.I)
_THINK_OPEN_RE = re.compile(r"<\s*think\s*>", re.I)


def _strip_thinking(text: str) -> str:
    """去除模型思考内容：<think>…</think>、◁think▷…◁/think▷、未闭合 <think> 之后到正文的前导，
    以及「（思考/分析/推理）：…」式开头段落。失败时尽量保留正文。"""
    s = str(text or "")
    s = _THINK_TAG_RE.sub("", s)
    s = _THINK_UNICODE_RE.sub("", s)
    # 未闭合 <think>：丢弃其后所有内容前的思考块——取最后一个 </think>（已被上面清掉）或开标签后内容。
    open_match = _THINK_OPEN_RE.search(s)
    if open_match:
        # 只剩一个未闭合开标签，说明思考未结束，正文可能在其后；保守删到下一个空行/标题。
        after = s[open_match.end():]
        m = re.search(r"\n\s*\n|^#|\n#", after)
        s = after[m.start():] if m else after
    s = s.strip()
    # 去掉「思考：」「分析过程：」等前导整段（直到首个空行或 Markdown 标题）。
    lead = re.match(r"^\s*(?:思考|分析过程|推理过程|我的思考|让我).{0,40}?[:：].*?(?:\n\s*\n|(?=\n#))", s, re.S)
    if lead and "#" not in s[: lead.end()]:
        s = s[lead.end():].strip()
    return s


def _article_brief(article: dict, ref: int | None = None) -> dict:
    authors = article.get("authors") or []
    abstract = str(article.get("abstract") or article.get("abstract_zh") or "").strip()
    brief = {
        "id": int(article["id"]),
        "title": str(article.get("title_zh") or article.get("title") or "").strip(),
        "journal": str(article.get("journal_name") or "").strip(),
        "authors": "、".join(authors[:5]) if authors else "",
        "abstract": abstract[:_ABSTRACT_CLIP],
    }
    if ref is not None:
        # ref 即文末引文序号，正文引用该文献观点时必须标注 [ref]。
        brief["ref"] = ref
    return brief


def _classify_articles(articles: list[dict], client: Any) -> dict[int, dict]:
    """返回 {article_id: {"discipline":..., "problem_type":...}}。AI 不可用时全部「暂未归类」。"""
    result: dict[int, dict] = {
        int(a["id"]): {"discipline": UNCLASSIFIED, "problem_type": PROBLEM_TYPES[0]} for a in articles
    }
    # 复用 DB 已有的分类（重生成场景），先填入。
    for a in articles:
        disc = str(a.get("ai_discipline") or "").strip()
        ptype = str(a.get("ai_problem_type") or "").strip()
        if disc:
            result[int(a["id"])] = {"discipline": disc, "problem_type": ptype or PROBLEM_TYPES[0]}
    if not _client_enabled(client):
        return result

    # 延迟导入写回 DB 的函数。
    try:
        from journal_alerts import set_article_classification
    except Exception:
        set_article_classification = None  # type: ignore[assignment]

    system = (
        "你是马克思主义理论学科的学术编辑。请把每篇文章归入下列五个二级学科之一："
        + "、".join(DISCIPLINES)
        + "；若确实无法判断，用「" + UNCLASSIFIED + "」。"
        "同时判断它属于「经典问题」（对旧问题、旧思想的再分析）还是「前沿问题」（对新问题、新现象的分析）。"
        "只返回 JSON，不要解释。"
    )
    for start in range(0, len(articles), _CLASSIFY_CHUNK):
        chunk = articles[start : start + _CLASSIFY_CHUNK]
        briefs = [_article_brief(a) for a in chunk]
        user = (
            "请对下列文章分类，返回形如 "
            "[{\"id\":123,\"discipline\":\"马克思主义基本原理\",\"problem_type\":\"前沿问题\"}] 的 JSON 数组，"
            "discipline 必须取自给定学科或「" + UNCLASSIFIED + "」，problem_type 取「经典问题」或「前沿问题」。\n\n"
            + json.dumps(briefs, ensure_ascii=False)
        )
        try:
            content = _chat(client, system, user, max_tokens=1500)
            parsed = json.loads(_extract_json(content))
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            try:
                aid = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            if aid not in result:
                continue
            discipline = str(entry.get("discipline") or "").strip()
            if discipline not in DISCIPLINES:
                discipline = UNCLASSIFIED
            problem_type = str(entry.get("problem_type") or "").strip()
            if problem_type not in PROBLEM_TYPES:
                problem_type = PROBLEM_TYPES[1] if "前沿" in problem_type else PROBLEM_TYPES[0]
            result[aid] = {"discipline": discipline, "problem_type": problem_type}
            if set_article_classification is not None:
                try:
                    set_article_classification(aid, discipline, problem_type)
                except Exception:
                    pass
    return result


def _review_one_discipline(
    discipline: str,
    items: list[dict],
    classified: dict[int, dict],
    client: Any,
    ref_by_id: dict[int, int],
) -> str:
    """为单一学科生成综述正文（含「经典问题」「前沿问题与前沿研究」两小节）。"""
    classic = [a for a in items if classified.get(int(a["id"]), {}).get("problem_type") == "经典问题"]
    frontier = [a for a in items if classified.get(int(a["id"]), {}).get("problem_type") != "经典问题"]
    if not _client_enabled(client):
        return _fallback_discipline_body(classic, frontier, ref_by_id)

    briefs = {
        "经典问题": [_article_brief(a, ref_by_id.get(int(a["id"]))) for a in classic],
        "前沿问题": [_article_brief(a, ref_by_id.get(int(a["id"]))) for a in frontier],
    }
    system = (
        "你是马克思主义理论学科的资深综述作者。请基于给定文章撰写一节中文文献综述，"
        "语言学术、客观、连贯，避免逐条罗列。必须覆盖给定的每一篇文章（可按主题归并叙述）。"
        "用 Markdown 输出，包含两个三级标题：「### 经典问题」与「### 前沿问题与前沿研究」；"
        "某一类没有文章则在该小节注明「本领域本周暂无此类研究」。"
        "【引注要求·务必遵守】每提到一篇文献的观点、结论或做法，都必须在该处句末紧跟其引注序号，"
        "格式为方括号加数字，如 [3]；序号必须使用每篇文章 JSON 中给定的 ref 值，"
        "一处综合多篇时写成 [1][4][7]。不得臆造序号，不得使用未给定的序号，不要在正文末尾另附引用列表。"
        "不要编造文章中没有的信息。"
    )
    user = (
        f"学科领域：{discipline}\n\n"
        "下面是该学科本周文章（按经典/前沿分组；每篇的 ref 即其引注序号，正文引用时必须标注 [ref]）：\n"
        + json.dumps(briefs, ensure_ascii=False)
    )
    try:
        text = _chat(client, system, user, max_tokens=2200).strip()
        if text:
            return text
    except Exception:
        pass
    return _fallback_discipline_body(classic, frontier, ref_by_id)


def _fallback_discipline_body(classic: list[dict], frontier: list[dict], ref_by_id: dict[int, int]) -> str:
    """AI 不可用时的降级正文：分组列出标题并附引注序号，保证信息与引文一一对应。"""
    def block(label: str, items: list[dict]) -> str:
        if not items:
            return f"### {label}\n\n本领域本周暂无此类研究。"
        bullets = "\n".join(
            f"- {str(a.get('title_zh') or a.get('title') or '').strip()}"
            f"（{str(a.get('journal_name') or '').strip()}）[{ref_by_id.get(int(a['id']), '?')}]"
            for a in items
        )
        return f"### {label}\n\n{bullets}"

    return block("经典问题", classic) + "\n\n" + block("前沿问题与前沿研究", frontier)


def _citation_block(articles: list[dict]) -> str:
    lines = ["## 引文（GB/T 7714-2015）", ""]
    for idx, article in enumerate(articles, 1):
        lines.append(f"[{idx}] {gb2015_citation(article)}")
    return "\n".join(lines)


def _period_label(settings: dict) -> str:
    days = settings.get("lookback_days")
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 0
    return f"（近 {days} 天）" if days else ""


def _extract_json(value: str) -> str:
    """从模型输出里截取第一个 JSON 数组/对象（容忍 ```json 围栏与前后解释）。"""
    text = str(value or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    start = min([p for p in (text.find("["), text.find("{")) if p >= 0], default=-1)
    if start < 0:
        return text
    end = max(text.rfind("]"), text.rfind("}"))
    if end > start:
        return text[start : end + 1]
    return text[start:]
