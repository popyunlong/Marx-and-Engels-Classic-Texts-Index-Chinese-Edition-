"""Task-directed generation. Resource budgets never imply a citation/word quota."""
from __future__ import annotations

import json
import re
import time

from ai import AIServiceError


def choose_model(models, complex_task=False):
    order = [("deepseek-v4-flash", "high"), ("deepseek-v4-flash", "off")]
    if not complex_task:
        order.reverse()
    for model, effort in order:
        if effort in models.get(model, []):
            return {"provider": "deepseek", "model": model, "reasoning_effort": effort}
    raise AIServiceError("当前账户没有可用的综合回答模型。")


def history_context(messages, question, budget=80000):
    """Keep recent full answers/tails plus older user constraints and headings."""
    cleaned = [m for m in (messages or []) if isinstance(m, dict) and m.get("role") in {"user", "assistant"}]
    # Reserve space for user instructions even when the last article fills the context budget.
    user_messages = [str(m.get("content") or "") for m in cleaned if m.get("role") == "user"]
    constraints = "\n".join(([user_messages[0]] + user_messages[-3:]) if user_messages else [])[:min(6000, budget//4)]
    out, remaining = [], max(0, budget-len(constraints)-100)
    for message in reversed(cleaned):
        if remaining < 1000:
            break
        text = str(message.get("content") or "")
        if message.get("role") == "assistant" and message.get("citation_refs"):
            refs, ref_chars = [], 0
            for ref in message["citation_refs"]:
                if not isinstance(ref, dict):
                    continue
                locator = {k: ref[k] for k in ("grounding_index", "review_index", "source_file", "pdf_pages", "document_id") if k in ref}
                encoded = json.dumps(locator, ensure_ascii=False)
                if ref_chars+len(encoded) > min(6000, remaining//4):
                    break
                refs.append(locator); ref_chars += len(encoded)
            text += "\n历史引用定位（仅用于理解追问，须重新检索核验后才能作为本轮证据）：" + json.dumps(refs, ensure_ascii=False)
        if len(text) > remaining:
            headings = "\n".join(re.findall(r"^#{1,6} .+$", text, re.M))
            # The latest answer's end is essential for continuation; preserve the beginning's constraints too.
            text = text[:min(3000, max(0, remaining//4))] + "\n[较早内容已压缩]\n" + headings[:2000] + "\n" + (text[-(remaining//2):] if remaining > 1 else "")
        out.append({"role": message["role"], "content": text})
        remaining -= len(text)
    result = list(reversed(out))
    if constraints:
        result.insert(0, {"role": "user", "content": "此前任务要求（如本轮有修改，以本轮为准）：\n" + constraints})
    return result


def plan_task(question, history, call):
    instruction = (
        "为用户任务制定可执行计划，只输出 JSON，不输出思维过程。字段：complex(boolean)，"
        "needs_corpus(boolean)，needs_web(boolean)，external_facts(boolean)，"
        "web_queries(公开检索关键词字符串数组，不含用户隐私、私有正文或整段对话)，"
        "corpus_queries(站内检索的不同论证侧面字符串数组)，"
        "requirements(用户的篇幅格式等要求字符串数组)，sections(自然的写作部分字符串数组)。"
        "解释、改写、提纲可直接完成，无须检索或引用；原著分析、出处核验及研究论证应检索站内。"
        "写文章通常联网了解背景和框架，原著引文仍来自站内；联网不是每问必搜。"
        "最新研究、事实核验、用户提供的公开网页属于 external_facts。没有最低篇幅和引用数量。"
        "不得从私有文库内容构造网络搜索词。"
    )
    raw = call([{"role": "system", "content": instruction}, *history,
                {"role": "user", "content": question}], 2400)
    try:
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
        if not isinstance(parsed, dict):
            raise ValueError()
        return {"complex": parsed.get("complex") is True,
                "needs_corpus": parsed.get("needs_corpus") is True,
                "needs_web": parsed.get("needs_web") is True,
                "external_facts": parsed.get("external_facts") is True,
                **{key: [str(v)[:1000] for v in (parsed.get(key) if isinstance(parsed.get(key), list) else []) if isinstance(v, str)]
                   for key in ("web_queries", "corpus_queries", "requirements", "sections")}}
    except (ValueError, TypeError):
        raise AIServiceError("任务规划未能完成，请重新提问；尚未执行联网或文库检索。")


def complete_blocks(buffer, final=False):
    """Only release sealed Markdown paragraphs; control markers stay private."""
    from ai_generation_protocol import section_end
    boundary = 0
    fence = None
    quotes = []
    pairs = {'“': '”', '「': '」', '『': '』', '"': '"', '[': ']', '(': ')'}
    offset = 0
    for line in buffer.splitlines(keepends=True):
        stripped = line.strip()
        marker = re.match(r'^(`{3,}|~{3,})', stripped)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
        elif fence is None:
            for part in re.split(r'(`[^`]*`)', line):
                if part.startswith('`'):
                    continue
                for char in part:
                    if quotes and char == quotes[-1]:
                        quotes.pop()
                    elif char in pairs:
                        quotes.append(pairs[char])
            if not stripped and not quotes and section_end(buffer[:offset], '<section_') < 0:
                boundary = offset + len(line)
        offset += len(line)
    partial_marker = any(buffer.rstrip().endswith('<section_complete/>'[:i]) for i in range(1, len('<section_complete/>')))
    if final and fence is None and not quotes and section_end(buffer, '<section_') < 0 and not partial_marker:
        boundary = len(buffer)
    return buffer[:boundary], buffer[boundary:]



from ai_generation_flow import generate  # public API retained


class ScopeEvidenceUnavailable(AIServiceError):
    """An explicit retrieval scope has no evidence for the question."""


def failure_details(exc, phase):
    text = str(getattr(exc, "description", exc))
    if isinstance(exc, ScopeEvidenceUnavailable):
        reason, message, recoverable = "scope_evidence_unavailable", text, False
    elif "本周" in text and "额度" in text:
        reason, message, recoverable = "weekly_quota", "本周 AI 额度不足，请在额度恢复或补充后继续。", False
    elif "额度" in text or "余额" in text:
        reason, message, recoverable = "wallet_quota", "AI 可用额度不足，请补充额度后继续。", False
    elif "时间预算" in text or "任务已停止" in text:
        reason, message, recoverable = "deadline", "本轮任务已达到时间预算，已保存内容可继续完成。", True
    elif isinstance(exc, json.JSONDecodeError) or "JSON" in text or "规划未能完成" in text or "结构输出" in text:
        reason, message, recoverable = "structured_output", "任务规划或完成检查未返回有效结构，已保存内容可继续完成。", True
    elif phase == "verifying":
        reason, message, recoverable = "verification_error", "引文核验未完成，已核验内容已保存，可继续完成。", True
    elif "连接" in text or "断流" in text:
        reason, message, recoverable = "connection_error", "模型连接中断，已保存内容可继续完成。", True
    elif "超时" in text:
        reason, message, recoverable = "upstream_timeout", "模型服务超时，已保存内容可继续完成。", True
    else:
        reason, message, recoverable = "generation_error", "模型生成发生异常，已保存内容可继续完成。", True
    return dict(stop_reason=reason, failure_stage=phase, recoverable=recoverable, message=message)


def model_evidence(passages, budget=100000):
    """Keep evidence IDs and complete quote segments, without duplicating source maps."""
    result = []
    for passage in passages:
        item = {k: passage.get(k) for k in ("index", "citation", "work_title", "work_authors")}
        segments = list(dict.fromkeys(passage.get("quote_segments") or [passage.get("text", "")]))
        item["quote_segments"] = []
        for segment in segments:
            candidate = {**item, "quote_segments": [*item["quote_segments"], segment]}
            if len(json.dumps([*result, candidate], ensure_ascii=False)) <= budget:
                item = candidate
        if item["quote_segments"]:
            result.append(item)
    if passages and not result:
        raise AIServiceError("证据超过本轮上下文预算，请缩小检索范围。")
    return result
