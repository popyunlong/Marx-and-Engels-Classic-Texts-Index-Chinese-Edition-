"""Flask integration for comprehensive conversations and shared web capabilities."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from flask import abort, copy_current_request_context, g, jsonify, request, Response, stream_with_context
import ai
import ai_comprehensive as tasks
import ai_web
import ai_citations
import ai_citation_runtime
import membership

_jobs = OrderedDict()
_lock = threading.Lock()


def enabled(A):
    user = getattr(g, "current_user", None)
    if not user:
        return False
    # The chat feature permission remains authoritative. This new rollout flag
    # supersedes the old admin-only preview without modifying live config.
    return A._env_flag("AI_COMPREHENSIVE_REGISTERED_ENABLED", True) or (
        A._is_admin_user(user) and A._env_flag("AI_COMPREHENSIVE_ADMIN_PREVIEW", True))


def web_allowed(A):
    user = getattr(g, "current_user", None)
    if not user:
        return False
    if A._is_admin_user(user):
        return A._env_flag("DEEPSEEK_WEB_ADMIN_PREVIEW", True)
    rights = membership.get_ai_entitlements(int(user["id"]))
    return bool(A._env_flag("DEEPSEEK_WEB_ENABLED", False) and rights.get("models") and rights.get("wallets"))


def capabilities(A):
    return {"comprehensive_access": bool(enabled(A) and A._feature_effective_for_user("search_chat")),
            "deepseek_web_access": web_allowed(A),
            "web_defaults": {"comprehensive": "auto", "quick": "off", "research": "off"}}


def prepare_web(A, payload, selection, *, plan=None, deadline=None, cancelled=lambda: False, previous_state=None):
    try:
        mode = ai_web.normalize_mode(payload.get("web_mode"), "off")
    except ValueError as exc:
        raise ai.AIServiceError(str(exc)) from exc
    if mode == "off":
        return None
    if not web_allowed(A):
        raise ai.AIServiceError("当前账户尚未开放 DeepSeek 联网。")
    user = getattr(g, "current_user", None)
    web = ai_web.WebEvidence(A.AI_CLIENT, user_id=int(user["id"]), selection=selection,
        charge_user=not A._is_admin_user(user), deadline=deadline or time.monotonic()+90, cancelled=cancelled)
    if previous_state:
        web.sources = copy.deepcopy(previous_state.get("web_sources") or [])
        web.cache = dict(previous_state.get("web_cache") or {})
    question = str(payload.get("question") or payload.get("gist") or payload.get("q") or "")
    # Do not transmit personal-library text, even if it occurred in an earlier turn.
    private_scope = "mylib:" in json.dumps(payload.get("scope"), ensure_ascii=False)
    if private_scope:
        web.warnings.append("个人文库任务未向网络搜索发送书籍正文；本次使用站内材料。")
        return web
    if plan is None:
        # Existing modes retain their generation behavior. Search is a separate, compact preparation step.
        should_search = mode == "always" or bool(re.search(r"最新|近年|当前|现状|研究进展|写.*文章|论文|https?://", question))
        queries = [" ".join(A._split_gist_terms(question))[:128]] if should_search else []
    else:
        should_search = mode == "always" or plan.get("needs_web")
        queries = plan.get("web_queries") or []
    if should_search:
        # Raw question/history is never passed via ori_question.
        if not queries:
            web.warnings.append("未生成可公开的搜索关键词，本次没有执行联网。")
        for query in queries:
            web.search(query)
            if cancelled() or time.monotonic() >= web.deadline:
                break
        urls = re.findall(r"https?://[^\s<>\"）]+", question)
        for url in urls:
            url = ai_web.safe_url(url)
            if url and not any(s["url"] == url for s in web.sources):
                web.sources.append({"id": "W"+str(len(web.sources)+1), "kind": "web", "url": url, "link": url,
                    "title": url, "site": "用户指定网页", "source_id": hashlib.sha256(url.encode()).hexdigest()[:20],
                    "summary": "", "content": "", "verification": "unread", "purpose": "background",
                    "retrieved_at": membership.utc_now_text()})
        # Read promising results within a content budget, not all returned search results.
        chars = 0
        for source in web.sources:
            if chars >= 48000 or web.counts["fetch"] >= web.limits["fetch"] or cancelled():
                break
            web.fetch(source)
            chars += len(source.get("content", ""))
    return web


def finish_web(result, web, field="answer_markdown"):
    if web is None:
        result.setdefault("web_status", "off")
        return result
    text, sources, warnings, status = web.finish(result.get(field) or "")
    result[field] = text
    result["sources"] = sources
    result["warnings"] = list(dict.fromkeys([*result.get("warnings", []), *warnings]))
    result["web_status"] = status
    result["used_web"] = bool(web.sources)
    return result


def register(A):
    @A.app.post("/api/ai/comprehensive-chat")
    def api_comprehensive_chat():
        A._require_content_feature("search_chat")
        if not enabled(A) or A.DEPLOYMENT.is_desktop:
            abort(403, description="综合模式尚未开放。")
        A._rate_limit_ai_or_abort()
        A._require_ai()
        quota = A._require_ai_quota_or_raise(credit_kind="chat")
        payload = request.get_json(silent=True) or {}
        question = str(payload.get("question") or "").strip()
        if not question or len(question) > 40000:
            abort(400, description="请输入有效问题（最多 40000 字符）。")
        user = getattr(g, "current_user", None)
        if not user:
            abort(401, description="请先登录。")
        uid = int(user["id"])
        request_id = str(payload.get("request_id") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", request_id):
            abort(400, description="缺少有效请求标识。")
        try:
            mode = ai_web.normalize_mode(payload.get("web_mode"), "auto")
        except ValueError as exc:
            abort(400, description=str(exc))
        if not web_allowed(A):
            if mode == "always":
                abort(403, description="当前账户尚未开放 DeepSeek 联网。")
            mode = "off"
        grounding = payload.get("grounding_mode", "auto")
        if grounding not in {"auto", "off", "always"}:
            abort(400, description="引文库设置无效。")
        request_scope = payload.get("scope", "auto")
        rights = A._ai_entitlements_payload(user).get("models", {})
        automatic = payload.get("model") in {None, "", "auto"}
        selection = tasks.choose_model(rights) if automatic else {
            "provider": payload.get("provider", "deepseek"), "model": payload.get("model"),
            "reasoning_effort": payload.get("reasoning_effort", "off")}
        selection = A._resolve_ai_selection_or_abort(selection, feature="comprehensive-chat")
        charge = bool(selection.get("charge_user_wallet", True) and not A._is_admin_user(user))
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = (uid, request_id)
        with _lock:
            now = time.monotonic()
            for old_key, old in list(_jobs.items()):
                if old.get("finished") and now-old["created"] > 900:
                    del _jobs[old_key]
            if key in _jobs:
                job = _jobs[key]
                if job["digest"] != digest:
                    abort(409, description="请求标识已用于其他内容。")
                if job.get("result"):
                    return jsonify(job["result"])
                abort(409, description="此请求已在处理中，请勿重复发送。")
            if len(_jobs) >= 32:
                abort(429, description="综合模式当前任务较多，请稍后再试。")
            previous = _jobs.get((uid, str(payload.get("continue_request_id") or "")))
            if payload.get("continue_request_id") and (not previous or not previous.get("finished")):
                abort(409, description="续接任务已过期或尚未结束，请基于已保存正文重新提问。")
            if previous and previous.get("scope") != request_scope:
                abort(400, description="继续完成时请保持原检索范围。")
            if not membership.claim_ai_conversation_request(uid, request_id, digest):
                abort(409, description="此请求已受理过，当前实例没有其结果；请查看已保存会话，不会重复执行扣费。")
            job = {"created": now, "digest": digest, "scope": request_scope, "finished": False,
                   "checkpoint": {}, "state": {}, "result": None}
            _jobs[key] = job
        progress = {"message": "正在排队", "phase": "queued"}
        timings = {}
        phase_started = time.monotonic()
        def stage(phase, message):
            nonlocal phase_started
            now = time.monotonic()
            old = progress.get("phase", "queued")
            timings[old] = timings.get(old, 0) + now-phase_started
            phase_started = now
            progress.update(phase=phase, message=message)
        request_g = dict(vars(g._get_current_object()))
        calls, warnings = [], []
        public_books = A._public_book_keys()

        @copy_current_request_context
        def slow(cancel_event):
            for name, value in request_g.items():
                setattr(g, name, value)
            nonlocal selection
            deadline = time.monotonic() + max(20, int(os.getenv("AI_COMPREHENSIVE_BUDGET_SECONDS", "600")))
            web, answer, done, status = None, "", 0, "partial"
            acquired = False
            result = None
            try:
                if not A._RESEARCH_QUEUE_SEMAPHORE.acquire(blocking=False):
                    raise ai.AIServiceError("综合模式当前任务较多，请稍后再试。")
                acquired = True
                slot = A._acquire_research_pipeline_slot(cancel_event)
                if not slot:
                    raise ai.AIServiceError("任务已停止或排队超时。")
                try:
                    def call(messages, max_tokens, streaming=False):
                        if cancel_event.is_set() or time.monotonic() >= deadline:
                            raise ai.AIServiceError("本次任务已达到时间预算，可继续完成。")
                        # Recheck authoritative rights and current balance before *every* hidden/visible call.
                        checked = A._resolve_ai_selection_or_abort(selection, feature="comprehensive-chat")
                        budget = max_tokens
                        if charge:
                            budget = membership.affordable_completion_tokens(
                                user_id=uid, selection=checked, messages=ai_web.augment_messages(messages), requested=max_tokens)
                        elif not A._is_admin_user(user):
                            limit = quota.get("weekly_limit")
                            credit_authorized = quota.get("over_free_limit") and int(quota.get("credit_balance") or 0) > 0
                            if limit is not None and not credit_authorized:
                                remaining = max(0, int(limit)-int(quota.get("weekly_used") or 0))
                                budget = min(budget, remaining)
                                if budget < 256:
                                    raise ai.AIServiceError("本周 AI 额度不足，可补充额度后继续。")
                        if streaming:
                            def tokens():
                                collected = []
                                try:
                                    with ai.research_ai_http_context(), ai.ai_call_context(
                                        user_id=uid, feature="comprehensive-chat", charge_user=charge, provider_call_ids=calls):
                                        upstream = A.AI_CLIENT.chat_complete_stream(messages, max_tokens=budget,
                                            provider=checked["provider"], model=checked["model"], reasoning_effort=checked["reasoning_effort"],
                                            http_timeout=max(1, min(480, deadline-time.monotonic())),
                                            should_cancel=cancel_event.is_set, deadline=deadline)
                                        try:
                                            for token in upstream:
                                                collected.append(token)
                                                yield token
                                        finally:
                                            if hasattr(upstream, "close"):
                                                upstream.close()
                                finally:
                                    if not charge and not A._is_admin_user(user):
                                        quota["weekly_used"] = int(quota.get("weekly_used") or 0) + A._estimate_tokens_from_text(messages, "".join(collected))
                            return tokens()
                        with ai.research_ai_http_context(), ai.ai_call_context(
                            user_id=uid, feature="comprehensive-chat", charge_user=charge, provider_call_ids=calls):
                            result = A.AI_CLIENT.chat_complete(messages, max_tokens=budget,
                                provider=checked["provider"], model=checked["model"],
                                reasoning_effort=checked["reasoning_effort"],
                                http_timeout=max(1, min(480, deadline-time.monotonic())))
                        if not charge and not A._is_admin_user(user):
                            quota["weekly_used"] = int(quota.get("weekly_used") or 0) + A._estimate_tokens_from_text(messages, result)
                        return result

                    effective_question = previous["state"].get("question", question) if previous else question
                    history = tasks.history_context(payload.get("messages"), effective_question)
                    stage("planning", "正在理解任务")
                    plan = tasks.plan_task(question, history, call) if not previous else copy.deepcopy(previous["state"]["plan"])
                    if automatic:
                        selection = A._resolve_ai_selection_or_abort(tasks.choose_model(rights, plan["complex"]), feature="comprehensive-chat")
                    passages, citations, scope_meta = [], [], {}
                    if previous:
                        state = previous["state"]
                        if state.get("public_books") != public_books:
                            raise ai.AIServiceError("文库权限或公开状态已变化，请重新检索。")
                        # Personal source access is revalidated, not trusted from browser history.
                        for citation in state.get("citations", []):
                            if citation.get("personal"):
                                sid = A.mylib_corpus.submission_id_from_key(str(citation.get("book") or ""))
                                A._get_mylib_ai_page_context_payload(sid, (citation.get("pdf_pages") or [1])[0])
                        passages, citations, scope_meta = copy.deepcopy(state["passages"]), copy.deepcopy(state["citations"]), state["scope_meta"]
                        answer, done = state.get("answer", ""), state.get("done", 0)
                        plan = copy.deepcopy(state["plan"])
                    web_payload = {**payload, "web_mode": mode}
                    needs_web = mode != "off" and (mode == "always" or plan.get("needs_web"))
                    needs_corpus = not previous and grounding != "off" and (grounding == "always" or plan["needs_corpus"] or request_scope not in ("auto", "all", None))
                    stage("retrieving" if needs_corpus else "web" if needs_web else "writing",
                          "\u6b63\u5728\u68c0\u7d22\u539f\u6587\u4e0e\u67e5\u9605\u80cc\u666f" if needs_corpus and needs_web else "\u6b63\u5728\u68c0\u7d22\u539f\u6587" if needs_corpus else "\u6b63\u5728\u67e5\u9605\u80cc\u666f" if needs_web else "\u6b63\u5728\u7ec4\u7ec7\u6b63\u6587")
                    @copy_current_request_context
                    def fetch_web():
                        for name, value in request_g.items():
                            setattr(g, name, value)
                        return prepare_web(A, web_payload, selection, plan=plan, deadline=deadline,
                            cancelled=cancel_event.is_set, previous_state=previous["state"] if previous else None)
                    preparation = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-web")
                    web_future = preparation.submit(fetch_web)
                    try:
                        if not previous and grounding != "off" and (grounding == "always" or plan["needs_corpus"] or request_scope not in ("auto", "all", None)):
                            queries = plan.get("corpus_queries") or [question]
                            for query in queries:
                                remaining = 100000-sum(len(p["text"]) for p in passages)
                                if remaining <= 0 or cancel_event.is_set() or time.monotonic() >= deadline:
                                    break
                                terms = A._split_gist_terms(query)
                                p, c, w, sm = A._build_chat_grounding(question, raw_scope=request_scope, user_id=uid,
                                    supplied_plan={"keywords": terms, "fragments": terms, "quotes": [], "chapter_keywords": []},
                                    source_limit=None, context_char_budget=remaining)
                                scope_meta = sm
                                warnings.extend(w)
                                for passage, citation in zip(p, c):
                                    if any(old["text"] == passage["text"] for old in passages):
                                        continue
                                    index = len(passages)+1
                                    passages.append({**passage, "index": index})
                                    citations.append({**citation, "grounding_index": index, "source_kind": "corpus"})
                            if not passages and (scope_meta.get("manual") or scope_meta.get("personal_only")):
                                raise ai.AIServiceError(warnings[0] if warnings else "指定范围内没有找到可核验原文，请调整问题或范围。")
                        web = web_future.result()
                    except BaseException:
                        cancel_event.set()
                        raise
                    finally:
                        preparation.shutdown(wait=True, cancel_futures=True)
                    if previous and previous["state"].get("web_sources"):
                        # Keep stable W numbers across continuation; avoid replacing earlier citations.
                        if web is None:
                            web = ai_web.WebEvidence(A.AI_CLIENT, user_id=uid, selection=selection,
                                charge_user=charge, deadline=deadline, cancelled=cancel_event.is_set)
                        fresh = web.sources
                        web.sources = copy.deepcopy(previous["state"]["web_sources"])
                        for source in fresh:
                            if not any(old["url"] == source["url"] for old in web.sources):
                                source["id"] = "W"+str(len(web.sources)+1)
                                web.sources.append(source)
                    job["state"] = {"plan": plan, "question": effective_question, "passages": passages, "citations": citations,
                        "web_sources": copy.deepcopy(web.sources) if web else [],
                        "web_cache": dict(web.cache) if web else {},
                        "scope_meta": scope_meta, "public_books": public_books, "answer": answer, "done": done}
                    verified_parts = {}
                    located_cards = {}
                    def verify(text):
                        if A._public_book_keys() != public_books:
                            raise ai.AIServiceError("文库公开状态已变化，请重新检索。")
                        if not passages:
                            return re.sub(r"(?<!\[)\[\d+\](?!\()", "（出处未核验）", text)
                        repaired = A.AI_CLIENT.repair_grounded_answer(text, passages)
                        if repaired.get("issues"):
                            warnings.append("部分引文已作核验修正，请结合出处查看原文。")
                        text = repaired["answer_markdown"]
                        verified_parts[text] = ai_citations.ledger(text, passages)
                        return text
                    def checkpoint(text, section, state):
                        job["state"].update(answer=text, done=section)
                        cards = [c for c in citations if c["grounding_index"] in set(A._review_ref_indices(text))]
                        if passages and ai_citations.enabled():
                            records = []
                            for part in text.split("\n\n"):
                                if part not in verified_parts:
                                    verified_parts[part] = ai_citations.ledger(part, passages)
                                records.extend(verified_parts[part]["citation_records"])
                            details = {"answer_markdown": text, "citation_records": records,
                                       "used_indices": sorted({r["index"] for r in records})}
                            text = details["answer_markdown"]
                            view = A.current_view_state()
                            can_view = bool(view["pdf_enabled"] and A._content_access_enabled("viewer"))
                            cards = []
                            for p in passages:
                                if p["index"] not in details["used_indices"]:
                                    continue
                                records = [r for r in details["citation_records"] if r["index"] == p["index"]]
                                key = (p["index"], can_view, json.dumps(records, ensure_ascii=False, sort_keys=True))
                                if key not in located_cards:
                                    located_cards[key] = ai_citation_runtime.card(A, citations[p["index"]-1], p, records, can_view, effective_question)
                                cards.append(located_cards[key])
                        result = {"ok": True, "answer_markdown": text, "citations": [c for c in citations
                            if c["grounding_index"] in set(A._review_ref_indices(text))],
                            "warnings": list(dict.fromkeys(warnings)), "sources": [], "grounding_scope": scope_meta,
                            "actual_model": {k: selection[k] for k in ("provider", "model", "reasoning_effort")}, "completion_status": state, "request_id": request_id,
                            "continue_request_id": request_id if state != "complete" else None}
                        result["citations"] = cards
                        finish_web(result, web)
                        job["checkpoint"] = result
                        if text and "first_visible_seconds" not in timings:
                            timings["first_visible_seconds"] = time.monotonic()-job["created"]
                        result["timings"] = dict(timings, total_seconds=time.monotonic()-job["created"])
                        progress.update(checkpoint=result, checkpoint_version=progress.get("checkpoint_version", 0)+1)
                    with ai_web.answer_context(web):
                        answer, done, status, warning = tasks.generate(effective_question, history, plan, passages, call,
                            deadline=deadline, cancelled=cancel_event.is_set, checkpoint=checkpoint, verify=verify,
                            previous=answer, completed_sections=done,
                            stream=lambda messages, budget: call(messages, budget, streaming=True), stage=stage)
                    if warning:
                        warnings.append(warning)
                    if status != "complete":
                        warnings.append("本次保留已完成内容，可选择“继续完成”。")
                    stage("complete" if status == "complete" else "partial", "回答已完成" if status == "complete" else "已保存完成内容")
                    checkpoint(answer, done, status)
                    result = dict(job["checkpoint"])
                    if answer:
                        A._consume_credit_if_paid(quota, "chat")
                    return result
                finally:
                    A._RESEARCH_PIPELINE_SEMAPHORE.release()
            except Exception as exc:
                result = dict(job["checkpoint"]) if job["checkpoint"] else {"ok": False, "error": str(getattr(exc, "description", exc))}
                if result.get("ok"):
                    result.update(completion_status="partial", continue_request_id=request_id)
                    result.setdefault("warnings", []).append("任务中断，已保留完成内容。")
                return result
            finally:
                if acquired:
                    A._RESEARCH_QUEUE_SEMAPHORE.release()
                A._record_ai_usage(quota, feature="comprehensive-chat", prompt_parts=(question,),
                    completion_text=job.get("state", {}).get("answer", ""), provider="deepseek", model=selection["model"],
                    success=bool(job["checkpoint"]), provider_call_ids=calls)
                job["result"] = result
                job["finished"] = True

        def finalize(result, error):
            result = result or {"ok": False, "error": "综合回答未能完成。"}
            result["ai_token_quota"] = A._ai_token_quota_payload(user)
            result["ai_entitlements"] = A._ai_entitlements_payload(user)
            job["result"] = result
            return result
        return Response(stream_with_context(A._sse_run_with_heartbeat(slow, finalize,
            progress_message="正在排队", heartbeat_interval=1.0, progress_state=progress)),
            mimetype="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
