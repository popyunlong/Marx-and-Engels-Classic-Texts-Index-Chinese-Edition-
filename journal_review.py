"""期刊文献综述生成：把一个批次抓取到的文章，用 DeepSeek 按学科领域 + 经典/前沿分类，
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
import logging
import re
from typing import Any

from journal_alerts import gb2015_citation, _markdown_to_html

LOGGER = logging.getLogger(__name__)


# 学科领域（固定顺序，分类与成稿小节的单一事实源）。
# 前 5 项为「马克思主义理论」一级学科下的二级学科；其后为按研究主题增设的专题领域，
# 用于容纳大量国外非主流政治经济学与发展研究类文章——这些硬塞进 5 个二级学科容易判不准、
# 整批落入「暂未归类」。增删领域只改此元组（及下面的释义/别名）即可。
DISCIPLINES = (
    "马克思主义基本原理",
    "马克思主义发展史",
    "马克思主义中国化研究",
    "国外马克思主义研究",
    "思想政治教育",
    "政治经济学与资本主义批判",
    "帝国主义、全球化与发展研究",
    "劳动、阶级与社会再生产",
    "马克思主义思想史与文本研究",
)
# 给模型的一句话释义，降低相近桶之间的误判（尤其「国外马克思主义研究」易吞并各专题桶）。
DISCIPLINE_HINTS = {
    "马克思主义基本原理": "马克思主义哲学、科学社会主义、政治经济学的基本范畴与原理性研究。",
    "马克思主义发展史": "马克思主义自身形成、传播与各阶段演进的历史研究。",
    "马克思主义中国化研究": "马克思主义与中国实际相结合、中国化时代化理论成果的研究。",
    "国外马克思主义研究": "以国外马克思主义流派、思想家或其理论本身为对象的研究（如开放/政治马克思主义）。",
    "思想政治教育": "思想政治教育的理论、方法、实践与立德树人研究。",
    "政治经济学与资本主义批判": "价值、货币、金融化、资产与权力、当代资本主义结构等政治经济学批判。",
    "帝国主义、全球化与发展研究": "帝国主义、全球化、欠发达与转型、全球南方等发展议题。",
    "劳动、阶级与社会再生产": "劳动与剥削、数字/平台劳动、工作世界、阶级与性别及社会再生产。",
    "马克思主义思想史与文本研究": "思想史、方法论、经典文本（如《民族学笔记》）的解读与考辨。",
}
# 模型回传/历史 DB 的学科名常有漂移（缺字、改写、近义）；按关键词兜回规范桶（顺序敏感，先命中先用）。
_DISCIPLINE_ALIASES = (
    ("中国化", "马克思主义中国化研究"),
    ("思想政治教育", "思想政治教育"),
    ("思政", "思想政治教育"),
    ("思想史", "马克思主义思想史与文本研究"),
    ("文本", "马克思主义思想史与文本研究"),
    ("方法论", "马克思主义思想史与文本研究"),
    ("发展史", "马克思主义发展史"),
    ("帝国", "帝国主义、全球化与发展研究"),
    ("欠发达", "帝国主义、全球化与发展研究"),
    ("发展研究", "帝国主义、全球化与发展研究"),
    ("全球化", "帝国主义、全球化与发展研究"),
    ("劳动", "劳动、阶级与社会再生产"),
    ("阶级", "劳动、阶级与社会再生产"),
    ("社会再生产", "劳动、阶级与社会再生产"),
    ("性别", "劳动、阶级与社会再生产"),
    ("工作", "劳动、阶级与社会再生产"),
    ("政治经济学", "政治经济学与资本主义批判"),
    ("资本主义批判", "政治经济学与资本主义批判"),
    ("资本主义", "政治经济学与资本主义批判"),
    ("金融", "政治经济学与资本主义批判"),
    ("货币", "政治经济学与资本主义批判"),
    ("西方马克思主义", "国外马克思主义研究"),
    ("国外", "国外马克思主义研究"),
    ("流派", "国外马克思主义研究"),
    ("基本原理", "马克思主义基本原理"),
    ("哲学", "马克思主义基本原理"),
    ("科学社会主义", "马克思主义基本原理"),
    ("原理", "马克思主义基本原理"),
)
PROBLEM_TYPES = ("经典问题", "前沿问题")
UNCLASSIFIED = "暂未归类"
DISCLAIMER = (
    "本文献综述及其学科归类、经典/前沿研究划分均由人工智能自动生成，仅供学术参考，"
    "可能存在误判或疏漏，请以原文为准。"
)
# 单组篇数偏小 + 充足 token，是避免分类 JSON 被截断、整组落入「暂未归类」的关键。
_CLASSIFY_CHUNK = 10
_CLASSIFY_MIN_SUBCHUNK = 3
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
    cn_index = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四")
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
    """返回 {article_id: {"discipline":..., "problem_type":...}}。

    稳健性要点（此前整批文章掉进「暂未归类」的根因修复）：
    - 小分块 + 充足 max_tokens，避免一组的 JSON 被 token 上限截断；
    - 解析时按对象逐个抢救，截断只丢尾部一两条，而非整组作废（旧逻辑是 except: continue 丢一组）；
    - 整组一条都救不回就拆半重试，并落 WARNING 日志，便于 journalctl 排查；
    - 模型回传的学科名做归一化，兜回规范桶，避免「缺字/改写」一律落入「暂未归类」。
    AI 不可用时沿用 DB 已有分类、其余「暂未归类」。
    """
    result: dict[int, dict] = {
        int(a["id"]): {"discipline": UNCLASSIFIED, "problem_type": PROBLEM_TYPES[0]} for a in articles
    }
    # 复用 DB 已有的分类（重生成场景）：归一化后填入，作为缓存以省去重复调用 AI。
    for a in articles:
        disc = _normalize_discipline(a.get("ai_discipline"))
        if disc != UNCLASSIFIED:
            result[int(a["id"])] = {
                "discipline": disc,
                "problem_type": _normalize_problem_type(a.get("ai_problem_type")),
            }
    if not _client_enabled(client):
        return result

    # 延迟导入写回 DB 的函数。
    try:
        from journal_alerts import set_article_classification
    except Exception:
        set_article_classification = None  # type: ignore[assignment]

    system = _classification_system_prompt()
    # 只对尚未归类的文章调用 AI（DB 已有分类的直接复用，省 token）。
    pending = [a for a in articles if result[int(a["id"])]["discipline"] == UNCLASSIFIED]
    for start in range(0, len(pending), _CLASSIFY_CHUNK):
        chunk = pending[start : start + _CLASSIFY_CHUNK]
        for aid, entry in _classify_chunk(client, system, chunk, depth=0).items():
            result[aid] = entry
            if set_article_classification is not None:
                try:
                    set_article_classification(aid, entry["discipline"], entry["problem_type"])
                except Exception:
                    pass
    return result


def _classification_system_prompt() -> str:
    """分类系统提示：列出全部领域 + 一句话释义，并要求「择优归类、慎用暂未归类」。"""
    lines = ["你是马克思主义理论与政治经济学领域的学术编辑。请把每篇文章归入下列研究领域中**最贴切的一个**："]
    lines += [f"- {d}：{DISCIPLINE_HINTS.get(d, '')}" for d in DISCIPLINES]
    lines.append(
        "归类原则：优先按文章的**研究主题/对象**择优；前 5 项为马克思主义理论二级学科，其后为专题领域，"
        "二者平级择优即可。国外学者的政治经济学、发展研究、劳动研究等应进入对应**专题领域**；"
        "「国外马克思主义研究」仅留给以马克思主义流派、思想家或其理论本身为对象的文章。"
        "只有确实无法判断时才用「" + UNCLASSIFIED + "」，不要轻易使用。"
    )
    lines.append(
        "同时判断每篇属于「经典问题」（对旧问题、旧思想的再分析）还是「前沿问题」（对新问题、新现象的分析）。"
        "只返回 JSON，不要解释。"
    )
    return "\n".join(lines)


def _classify_chunk(client: Any, system: str, chunk: list[dict], depth: int) -> dict[int, dict]:
    """对一组文章分类，返回 {aid: {...}}（仅含成功条目）。

    一条都救不回且组还够大时拆半重试——截断场景下更小的分块必然放得下，
    从而把「整组失败」收敛为「至多个别文章未归类」。
    """
    if not chunk:
        return {}
    briefs = [_article_brief(a) for a in chunk]
    user = (
        "请对下列文章分类，返回形如 "
        "[{\"id\":123,\"discipline\":\"政治经济学与资本主义批判\",\"problem_type\":\"前沿问题\"}] 的 JSON 数组，"
        "discipline 必须取自给定领域，problem_type 取「经典问题」或「前沿问题」。\n\n"
        + json.dumps(briefs, ensure_ascii=False)
    )
    # token 预算随条数线性放宽并留冗余，避免 JSON 收不了尾（旧逻辑固定 1500 是整批失败主因）。
    max_tokens = min(8000, 800 + 280 * len(chunk))
    ids_in_chunk = {int(a["id"]) for a in chunk}
    out: dict[int, dict] = {}
    try:
        entries = _parse_classification_entries(_chat(client, system, user, max_tokens=max_tokens))
    except Exception as exc:
        LOGGER.warning("文献综述分类调用失败 (n=%d, depth=%d): %s", len(chunk), depth, exc)
        entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            aid = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        if aid in ids_in_chunk:
            out[aid] = {
                "discipline": _normalize_discipline(entry.get("discipline")),
                "problem_type": _normalize_problem_type(entry.get("problem_type")),
            }
    if not out and len(chunk) > _CLASSIFY_MIN_SUBCHUNK and depth < 3:
        mid = len(chunk) // 2
        LOGGER.warning("文献综述分类整组失败，拆半重试 (n=%d → %d+%d)", len(chunk), mid, len(chunk) - mid)
        out.update(_classify_chunk(client, system, chunk[:mid], depth + 1))
        out.update(_classify_chunk(client, system, chunk[mid:], depth + 1))
    return out


def _normalize_discipline(raw: Any) -> str:
    """把模型回传/历史 DB 的学科名兜回规范桶；无法识别返回「暂未归类」。"""
    s = re.sub(r"\s+", "", str(raw or "")).strip("「」“”\"'：:·-—()（）")
    if not s:
        return UNCLASSIFIED
    if s in DISCIPLINES:
        return s
    # raw 含某个规范名（如加了括号注脚）→ 该桶。
    for canon in DISCIPLINES:
        if canon in s:
            return canon
    # raw 是某个规范名的轻微缺字（如「国外马克思主义」缺「研究」）→ 该桶。
    for canon in DISCIPLINES:
        if s in canon and len(s) >= len(canon) - 2:
            return canon
    # 关键词兜底。
    for kw, canon in _DISCIPLINE_ALIASES:
        if kw in s:
            return canon
    return UNCLASSIFIED


def _normalize_problem_type(raw: Any) -> str:
    s = str(raw or "").strip()
    if s in PROBLEM_TYPES:
        return s
    return PROBLEM_TYPES[1] if "前沿" in s else PROBLEM_TYPES[0]


def _parse_classification_entries(content: str) -> list:
    """从模型输出解析分类条目；JSON 被截断/夹带解释时，按对象逐个抢救，丢弃尾部不完整的一个。"""
    text = _extract_json(content)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except Exception:
        pass
    decoder = json.JSONDecoder()
    out: list = []
    i, n = 0, len(text)
    while i < n:
        brace = text.find("{", i)
        if brace < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
        except ValueError:
            i = brace + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


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
        "你是马克思主义理论学科的资深综述作者，正在为一篇期刊文献综述撰写其中一个学科小节。\n\n"
        "【综述体例】\n"
        "1. 以研究主题（问题域、研究对象）为线索组织行文，把聚焦同一议题的多篇文献归并在一起对照评述，"
        "切忌一篇一段地逐条罗列或简单复述摘要。\n"
        "2. 每个小节开头先用一两句话概括本领域本期研究的总体关切与切入点，再分主题逐层展开。\n"
        "3. 行文中点明不同文献之间的共识、分歧、递进或补充关系，善用「有学者指出……」「另有研究认为……」"
        "「与之不同的是……」「在此基础上……」等学术过渡语，使各篇自然衔接为整体。\n"
        "4. 保持客观转述，不作主观褒贬、不夸大拔高；可在每小节末尾用一句话点出研究趋势或尚待深化之处。\n"
        "5. 必须覆盖给定的每一篇文献，不得遗漏；也不得引入给定材料之外的任何信息或编造内容。\n\n"
        "【结构与格式】\n"
        "- 用 Markdown 输出，且仅含两个三级标题：「### 经典问题」与「### 前沿问题与前沿研究」，顺序固定。\n"
        "- 某一类没有文章，则在对应标题下只写一句「本领域本周暂无此类研究。」。\n"
        "- 不写一级/二级标题，不写本小节之外的导语或总结。\n\n"
        "【引注规则】\n"
        "- 每转述一篇文献的观点、结论或做法，都在该处句末紧跟方括号引注，如 [3]；序号一律取自该文 JSON 的 ref 字段。\n"
        "- 一处综合多篇写成 [1][4][7]；不得臆造或使用未给定的序号，正文末尾不另附参考文献列表。\n\n"
        "【输出要求】直接输出该小节的 Markdown 正文，第一行即为「### 经典问题」；"
        "不要复述以上要求，不要输出任何思考、计划、分析过程或解释性文字。"
    )
    user = (
        f"学科领域：{discipline}\n\n"
        "下面是该学科本周文章（按经典/前沿分组；每篇的 ref 即其引注序号，正文引用时必须标注 [ref]）：\n"
        + json.dumps(briefs, ensure_ascii=False)
    )
    try:
        text = _trim_to_first_heading(_chat(client, system, user, max_tokens=2200).strip())
        if text:
            return text
    except Exception:
        pass
    return _fallback_discipline_body(classic, frontier, ref_by_id)


def _trim_to_first_heading(text: str) -> str:
    """切掉小节正文前的漏写前置（如模型复述任务要求/思考计划）：
    正文按约定应以「### 」三级标题开头，若标题前混入了其他段落，一并丢弃。"""
    s = str(text or "")
    m = re.search(r"(?m)^\s*###\s", s)
    return s[m.start():].strip() if m else s.strip()


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
