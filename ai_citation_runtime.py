"""Application adapter for AI citations; public search/reader code is untouched."""
from __future__ import annotations

import re
import time
import hashlib
from collections import OrderedDict

import ai_citations as C
from ai_evidence import clean_evidence, exact_quote, _quote_map

# Visually checked against PDF physical page 946 / printed page 928 on 2026-09-14.
# Same-length substitutions preserve the raw-coordinate map. An OCR revision
# invalidates this rule until the changed page has been reviewed again.
_REVIEWED = ({"source_file": "pdfs/文集/马克思恩格斯文集[第7卷]马克思《资本论》第三卷.pdf",
              "pdf_page": 946, "sha256": "ecd098bdd237f95b0f728a160ab2a76ee1be5ffb975409b78371fc9bede53211",
              "old": "始$因而", "new": "始；因而"},
             {"source_file": "pdfs/文集/马克思恩格斯文集[第1卷]马克思恩格斯1843-1848年著作.pdf",
              "pdf_page": 626, "sha256": "72627d6125bb41175d12881e6ea6b9ca4acd70b457736c24759255f70583f0c7",
              # Visually reviewed on 2026-09-16: superscript annotation 208,
              # not author text. Equal-length spaces retain source coordinates.
              "old": '"不死的死"208', "new": '"不死的死"   ',
              "headers": ["第二章", "政治经济学的形而上学"]},)


def reviewed_page_text(page, source_file):
    raw = str(page.raw_text or "")
    for rule in _REVIEWED:
        if source_file == rule["source_file"] and int(page.pdf_page) == rule["pdf_page"] and hashlib.sha256(raw.encode()).hexdigest() == rule["sha256"]:
            raw = raw.replace(rule["old"], rule["new"])
    return raw


def correct_source_window(A, text, payload):
    # Repeated chapter headings are page furniture, not quotation text. Obtain
    # them from the first physical lines of the actual evidence pages only.
    if "_ai_page_headers" not in payload:
        payload["_ai_page_headers"] = []
        volume = A._payload_volume(payload)
        if volume:
            center = A._payload_first_page_index(payload, volume)
            for page in volume.pages[max(0, center - 1):center + 2]:
                for line in str(page.raw_text or "").splitlines()[:3]:
                    line = line.strip()
                    if re.fullmatch(r"第[一二三四五六七八九十百0-9]+[篇章节][^。！？；，,:：]{1,35}", line):
                        payload["_ai_page_headers"].append(line)
    sf = payload.get("source_file")
    if not any(sf == rule["source_file"] and rule["old"] in text for rule in _REVIEWED):
        return text
    volume = A._payload_volume(payload)
    if volume:
        for rule in _REVIEWED:
            if sf != rule["source_file"]:
                continue
            page = next((p for p in volume.pages if p.pdf_page == rule["pdf_page"]), None)
            if page and reviewed_page_text(page, sf) != page.raw_text:
                text = text.replace(rule["old"], rule["new"])
                payload.setdefault('_ai_page_headers', []).extend(rule.get('headers', []))
    return text


def source_segments(A, hit, payload: dict, text: str) -> list[dict]:
    """Carry actual page identities with the text given to the model."""
    volume = A._hit_volume(hit)
    if volume is None:
        return []
    document = A.corpus.document_scope_for_hit(hit) if A.corpus else None
    hit_index = A._hit_first_page_index(hit, volume)
    pieces, ranges = [], []
    cursor = 0
    for pi in range(max(0, hit_index - 1), min(len(volume.pages), hit_index + 2)):
        page = volume.pages[pi]
        raw = reviewed_page_text(page, volume.source_file)
        raw_offset = 0
        if document:
            left = max(0, document.norm_start - volume.page_offsets[pi])
            right = min(len(page.norm_text), document.norm_end - volume.page_offsets[pi])
            if right <= left:
                continue
            mapping = A.corpus._export_page_raw_map(page, OrderedDict())
            if not mapping:
                return []
            raw_offset, raw_end = A.corpus.document_page_raw_bounds(page, mapping, left, right)
            raw = raw[raw_offset:raw_end]
        if pieces:
            cursor += 1
        ranges.append((cursor, cursor + len(raw), pi, raw_offset))
        cursor += len(raw)
        pieces.append(raw)
    cleaned = clean_evidence("\n".join(pieces), payload)
    needle, text_positions = _quote_map(text)
    haystack, clean_positions = _quote_map(cleaned.text)
    found = haystack.find(needle)
    if not needle or found < 0:
        return []
    groups = []
    for j, text_pos in enumerate(text_positions):
        clean_pos = clean_positions[found + j]
        raw_pos = cleaned.source_positions[clean_pos]
        row = next((r for r in ranges if r[0] <= raw_pos < r[1]), None)
        if row is None:
            continue
        a, b, pi, offset = row
        if not groups or groups[-1]["page_index"] != pi:
            groups.append({"start": text_pos, "end": text_pos + 1, "raw_start": raw_pos - a + offset,
                           "raw_end": raw_pos - a + offset + 1, "page_index": pi,
                           "pdf_page": int(volume.pages[pi].pdf_page), "source_file": volume.source_file})
        else:
            groups[-1].update(end=text_pos + 1, raw_end=raw_pos - a + offset + 1)
    return groups


def passage(A, hit, payload: dict, text: str, index: int) -> dict:
    out = {"index": index, "citation": payload.get("citation") or "", "text": text,
           "quote_segments": payload.get("_ai_quote_segments", [text])}
    for k in ("document_id", "work_title", "work_authors", "provenance_verified", "source_file", "book", "section_title"):
        out[k] = payload.get(k)
    try:
        out["source_segments"] = source_segments(A, hit, payload, text)
    except Exception:
        out["source_segments"] = []
    return out


def compact_card_evidence(evidence: list[dict]) -> list[dict]:
    """Hide contained quote fragments only within the same located source page."""
    normalized = [_quote_map(ev.get('quote') or '')[0] for ev in evidence]
    kept = []
    for i, ev in enumerate(evidence):
        if ev.get('kind') != 'quote' or not normalized[i]:
            kept.append(ev)
            continue
        identity = (ev.get('source_file'), ev.get('pdf_page'), ev.get('location_status'))
        covered = any(
            j != i and other.get('kind') == 'quote'
            and (other.get('source_file'), other.get('pdf_page'), other.get('location_status')) == identity
            and normalized[i] in normalized[j]
            and (len(normalized[j]) > len(normalized[i]) or j < i)
            for j, other in enumerate(evidence)
        )
        if not covered:
            kept.append(ev)
    return kept


def card(A, base: dict, passage: dict, records: list[dict], viewer_allowed: bool, topic: str) -> dict:
    """One source number, every cited span, and every actual physical page."""
    out = dict(base)
    volume = A._payload_volume(base)
    source = passage.get("text", "")
    segments = passage.get("source_segments") or []
    evidence = []
    seen = set()
    for record in records:
        spans = record.get("quotes") or []
        if not spans:
            sentence = A._best_review_cited_sentence_in_passage(source, [record["unit"]])
            # A paraphrase may use the whole passage rather than one matching
            # sentence. Show its mapped source window, without labelling it a
            # direct quotation or guessing a smaller textual support span.
            spans = [sentence or source] if source else []
        for span in spans:
            needle, _ = _quote_map(span)
            norm, positions = _quote_map(source)
            at = norm.find(needle)
            if not needle or at < 0:
                continue
            left, right = positions[at], positions[at + len(needle) - 1] + 1
            matched = []
            for segment in segments:
                a, b = max(left, segment["start"]), min(right, segment["end"])
                if a >= b or not volume:
                    continue
                literal = source[a:b]
                # A closing bracket left at the end of a previous page is not
                # textual evidence for the following sentence.
                if not C.key(literal):
                    continue
                item = A._volume_page_evidence(volume, segment["page_index"], literal, literal,
                                              kind=record["kind"], quote=span if record["kind"] == "quote" else "")
                if item:
                    matched.append(item)
            # Legacy records have no mapped text. Use the existing verifier only
            # if its result really contains the complete span on that page.
            if not matched and volume and not segments:
                item = A._quote_match_in_citation_payload(base, span)
                if item:
                    page = next((p for p in volume.pages if int(p.pdf_page) == item["pdf_page"]), None)
                    if page and exact_quote(span, clean_evidence(page.raw_text, base).text):
                        item["kind"] = record["kind"]
                        matched = [item]
            if not matched:
                identity = (None, C.key(span), record["kind"])
                if identity not in seen:
                    evidence.append({"kind": record["kind"], "context": span,
                                     "text_verified": record["kind"] == "quote",
                                     "quote": span if record["kind"] == "quote" else "", "location_status": "unresolved",
                                     "pdf_page": None, "printed_page": "", "viewer_url": "",
                                     "citation": "原文片段已匹配，具体页码待核验", "citations": {}})
                    seen.add(identity)
                continue
            for item in matched:
                identity = (item["pdf_page"], C.key(item["span"]), record["kind"])
                if identity in seen:
                    continue
                seen.add(identity)
                ev = {k: v for k, v in item.items() if k not in {"source_text", "span"}}
                ev.update(text_verified=record["kind"] == "quote",
                          context=A._review_evidence_context(item["span"], [item["span"]], metadata=base),
                          quote=span if record["kind"] == "quote" else "", location_status="verified", viewer_url="")
                if viewer_allowed:
                    ev["viewer_url"] = A.url_for("pdf_viewer", file=item["source_file"], page=item["pdf_page"],
                                                 q=topic, h=item["span"], section=base.get("section_title") or "",
                                                 printed=item.get("printed_page") or "")
                evidence.append(ev)
    evidence = compact_card_evidence(evidence)
    out.update(evidence=evidence, context=evidence[0]["context"] if evidence else clean_evidence(base.get("context", ""), base).text,
               candidate_pdf_pages=list(base.get("pdf_pages") or []))
    resolved = [ev for ev in evidence if ev.get("location_status") == "verified"]
    # A private-reader citation keeps its own owner-scoped navigation adapter.
    if not volume and base.get("personal"):
        out["location_status"] = "unresolved"
        return out
    out.update(pdf_pages=list(dict.fromkeys(ev["pdf_page"] for ev in resolved)),
               printed_pages=list(dict.fromkeys(ev.get("printed_page", "") for ev in resolved)),
               page_refs=[ref for ev in resolved for ref in ev.get("page_refs", [])],
               quote_page_verified=bool(resolved) and len(resolved) == len(evidence),
               location_status="verified" if resolved and len(resolved) == len(evidence) else "partial" if resolved else "unresolved",
               viewer_url=resolved[0]["viewer_url"] if resolved else "", review_quoted=bool(resolved))
    # Rendering each page's existing formatter output also handles discontinuous
    # printed sequences and production's page_labels without inventing a range.
    out["citation"] = "；".join(dict.fromkeys(ev.get("citation", "") for ev in resolved if ev.get("citation"))) or "具体页码待核验"
    formats = {fmt for ev in resolved for fmt in ev.get("citations", {})}
    out["citations"] = {fmt: "；".join(dict.fromkeys(ev.get("citations", {}).get(fmt, ev.get("citation", "")) for ev in resolved)) for fmt in formats}
    out["page_location"] = "、".join(dict.fromkeys(ev.get("page_location") or ev.get("printed_page") or f"PDF {ev['pdf_page']}" for ev in resolved))
    return out


def final_stats(details, cards, progress=None):
    stats = dict(details["citation_stats"])
    verified = sum(c.get("location_status") == "verified" for c in cards)
    pending = len(cards) - verified
    stats.update(body_sources=stats["effective_sources"], effective_sources=verified,
                 page_verified_sources=verified, page_pending_sources=pending)
    if progress and pending:
        initial_ids = set(progress.get("initial_indices", []))
        initial = sum(c.get("location_status") == "verified" and (c.get("grounding_index") or c.get("review_index")) in initial_ids for c in cards) if initial_ids else max(0, progress["initial"] - pending)
        progress.update(effective=verified, initial=initial, added=max(0, verified - initial))
        if verified < progress["target"] and progress["stop_reason"] == "target_reached":
            progress["stop_reason"] = "page_verification_incomplete"
    return stats


def run_augmentation(A, answer, passages, bases, *, question, target, deadline,
                     allowed_books, document_scopes, provider, model, reasoning, cancelled):
    """Runs inside the existing ai_call_context, preserving billing/provider."""
    pool = C.EvidencePool(question)
    for p in passages:
        pool.add(p["text"], p)

    def retrieve(round_no, current, items, end):
        if time.monotonic() >= end - 8 or cancelled():
            return 0
        headings = re.findall(r"(?m)^#{1,6}\s+(.+)", current)
        focus = headings[(round_no - 1) % len(headings)] if headings else "相关原著的不同论点"
        cues = ["概念界定与形成条件", "历史过程与社会关系", "矛盾和限制条件", "实践路径及不同著作的侧重"]
        direction = cues[((round_no - 1) // max(1, len(headings))) % len(cues)]
        query = f"{question}\n围绕{focus}，寻找{direction}的新原文。"
        # A short planning call shares the remaining deadline and the selected
        # model, with no new provider or account policy.
        raw = A.AI_CLIENT._chat_research_review([
            {"role": "system", "content": "仅返回JSON：{\"keywords\":[词],\"fragments\":[可能出现在原著的短语]}。提出与研究缺口直接相关的不同检索线索，不编造出处。"},
            {"role": "user", "content": query}], max_tokens=2048, deadline=end,
            provider=provider, model=model, reasoning_effort=reasoning)
        from ai import _extract_json_object
        plan = _extract_json_object(raw)
        if not isinstance(plan, dict) or not any(isinstance(plan.get(k), list) and plan[k] for k in ("keywords", "fragments")):
            # A malformed planning response still gets a deterministic scoped
            # search, rather than terminating an otherwise useful augmentation.
            plan = {"keywords": A._split_gist_terms(focus + " " + direction), "fragments": []}
        keywords = [str(x)[:30] for x in plan.get("keywords", [])][:12] if isinstance(plan.get("keywords"), list) else []
        fragments = [str(x)[:60] for x in plan.get("fragments", [])][:12] if isinstance(plan.get("fragments"), list) else []
        if document_scopes:
            hits = A.corpus.locate_associative_in_documents(document_scopes, quotes=[], keywords=keywords, fragments=fragments)
        elif allowed_books:
            hits = A.corpus.locate_associative(quotes=[], keywords=keywords, fragments=fragments, book_scope=allowed_books)
            hits += A.corpus.locate_subject_index(keywords, cap=target * 3, book_scope=allowed_books)
        else:
            return 0
        added = 0
        for hit in A._select_research_review_hits(hits, target * 3):
            if time.monotonic() >= end - 8 or cancelled():
                break
            hit = A.corpus.enrich_hit_document(hit)
            base = hit.to_dict()
            text = A._research_review_passage_text(hit, base, query)
            if not text or not C.admissible(base, text, question) or not pool.add(text, base):
                continue
            item = passage(A, hit, base, text, 0)
            if not item.get("source_segments"):
                continue
            # Reclaim unused, already-attempted slots only after all candidates
            # were offered to the writer. Used numbering is never reassigned.
            used = set(C.ledger(current, items)["used_indices"])
            index = next((i for i in range(1, target + 1) if i not in {p["index"] for p in items}), None)
            if index is None:
                old = next((p for p in items if p["index"] not in used), None)
                if old is None:
                    break
                index = old["index"]
                items.remove(old)
            item["index"] = index
            items.append(item)
            bases[index] = base
            added += 1
            if added >= min(8, target - len(used)):
                break
        return added

    def write(current, batch, end):
        from ai import _ACADEMIC_WRITING_RULES
        paragraphs = current.split("\n\n")
        numbered = "\n\n".join(f"【段落{i}】\n{p}" for i, p in enumerate(paragraphs, 1))
        prompt = ("根据新证据定向增补原回答，保留全部已有内容。仅返回JSON："
                  '{"additions":[{"after_paragraph":段落编号整数,"text":"新增论述"}]}。'
                  "每段新增论述必须有至少一处来自新证据的逐字引文及[N]，解释其含义并联系所在分论点；"
                  "不能只有引文清单，不能输出标题、整篇重写或参考文献。新增论证须直接相关，"
                  "不扩大引文含义、不补充没有依据的历史事实或作者意图；不适用的证据可以不使用。"
                  "after_paragraph选择所属小节的正文段落，不选标题或结论。"
                  "可以补充多个段落，不受快速回答通常篇幅限制。\n"
                  f"{_ACADEMIC_WRITING_RULES}\n"
                  f"研究要求：{question}\n原回答（段落编号仅供选择插入位置）：\n{numbered}\n新证据：\n" + A.AI_CLIENT._format_grounding_block(batch))
        raw = A.AI_CLIENT._chat_research_review([
            {"role": "system", "content": "你是严谨的中文研究助手，仅定向增补有可核验依据的论证。"},
            {"role": "user", "content": prompt}], max_tokens=min(32768, max(8000, len(batch) * 2500)),
            deadline=end, provider=provider, model=model, reasoning_effort=reasoning)
        additions = C.decode_additions(raw)
        for addition in additions:
            try:
                index = int(addition.get("after_paragraph")) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(paragraphs) and not re.match(r"^\s*#", paragraphs[index]):
                addition["after"] = paragraphs[index]
        return additions

    result = C.augment(answer, passages, question=question, target=target, deadline=deadline,
                       retrieve=retrieve, write=write, verify=A.AI_CLIENT.repair_grounded_answer, cancelled=cancelled)
    A.LOGGER.info("Citation augmentation effective=%s target=%s added=%s stop=%s",
                  result["augmentation"]["effective"], target, result["augmentation"]["added"], result["augmentation"]["stop_reason"])
    return result


def recover_empty_candidates(A, question, cap, allowed_books, document_scopes):
    """An explicit request with no initial evidence still gets changed clues.

    The caller owns the existing internal-retrieval billing context. Once body
    evidence is found, normal generation and the gap-driven loop take over.
    Two consecutive empty directions exhaust this otherwise empty starting pool.
    """
    for direction in ("概念的界定与现实条件", "历史过程、矛盾与限制"):
        try:
            plan = A.AI_CLIENT.expand_associative_query(question + "；重点寻找：" + direction)
            quotes, fragments, keywords, chapter_keywords = A._parse_assoc_plan(plan)
            if document_scopes:
                hits = A.corpus.locate_associative_in_documents(document_scopes, quotes=quotes, keywords=keywords, fragments=fragments)
            elif allowed_books:
                hits = A.corpus.locate_associative(quotes=quotes, keywords=keywords, fragments=fragments,
                    chapter_keywords=chapter_keywords, book_scope=allowed_books)
                hits += A.corpus.locate_subject_index(keywords, cap=cap * 3, book_scope=allowed_books)
            else:
                return []
            eligible = []
            pool = C.EvidencePool(question)
            for hit in A._select_research_review_hits(hits, cap * 3):
                hit = A.corpus.enrich_hit_document(hit)
                base = hit.to_dict()
                text = A._research_review_passage_text(hit, base, question)
                if text and C.admissible(base, text, question) and pool.add(text, base):
                    eligible.append(hit)
            A.LOGGER.info("Empty citation pool changed direction; qualified candidates=%s", len(eligible))
            if eligible:
                return eligible
        except Exception:
            A.LOGGER.exception("Empty citation pool recovery unavailable")
            return []
    return []


def restore_history(A, messages, question, cap, allowed_books, document_scopes):
    """Re-read historical evidence from the permitted corpus, never trust client text."""
    if not C.MORE.search(question) or not isinstance(messages, list):
        return None
    previous = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "assistant"), None)
    if not previous:
        return None
    answer = str(previous.get("content") or "")
    if len(answer) > 80000 or "（此处略）" in answer:
        return None
    refs = previous.get("citation_refs") or []
    used = set(C.reference_ids(answer))
    passages, bases = [], {}
    for ref in refs[:cap]:
        try:
            index = int(ref.get("grounding_index") or ref.get("review_index") or 0)
        except (ValueError, TypeError):
            continue
        if index not in used or not 1 <= index <= cap or index in bases:
            continue
        volume = A.corpus.get_volume_by_source_file(str(ref.get("source_file") or ""))
        if not volume or not A._scope_allows(volume.book, volume.volume, allowed_books):
            continue
        requested_pages = set(ref.get("pdf_pages") or ref.get("candidate_pdf_pages") or [ref.get("pdf_page")])
        parts, maps, quote_segments = [], [], []
        cursor = 0
        safe_pages = []
        previous_pi = None
        previous_joinable = False
        for pi, page in enumerate(volume.pages):
            if page.pdf_page not in requested_pages:
                continue
            start, end = volume.page_offsets[pi], volume.page_offsets[pi + 1]
            raw = reviewed_page_text(page, volume.source_file)
            if document_scopes:
                overlaps = [(max(start, s.norm_start), min(end, s.norm_end)) for s in document_scopes
                            if s.source_file == volume.source_file and s.norm_start < end and s.norm_end > start]
                if len(overlaps) != 1:
                    return None
                left, right = overlaps[0]
                mapping = A.corpus._export_page_raw_map(page, OrderedDict())
                if not mapping or right <= left:
                    return None
                raw = raw[mapping[0][left - start]:mapping[1][right - start - 1]]
            page_meta = {"book": volume.book, "source_file": volume.source_file, "pdf_pages": [page.pdf_page]}
            raw = correct_source_window(A, raw, page_meta)
            clean = clean_evidence(raw, page_meta)
            if parts:
                cursor += 1
            maps.append({"start": cursor, "end": cursor + len(clean.text), "page_index": pi,
                         "pdf_page": page.pdf_page, "source_file": volume.source_file})
            parts.append(clean.text)
            safe = list(clean.quote_segments)
            # Adjacent page boundaries are continuous original text; omitted
            # footnotes and non-contiguous pages never become fabricated joins.
            if (safe and quote_segments and previous_pi == pi - 1 and previous_joinable
                    and clean.text.startswith(safe[0])):
                quote_segments[-1] += "\n" + safe.pop(0)
            quote_segments.extend(safe)
            previous_pi = pi
            previous_joinable = bool(clean.quote_segments and clean.text.endswith(clean.quote_segments[-1]))
            cursor += len(clean.text)
            safe_pages.append(page)
        if not parts:
            continue
        chapter = A.corpus.get_chapter_for_page(volume.source_file, safe_pages[0].pdf_page)
        base = {"book": volume.book, "volume": volume.volume, "source_file": volume.source_file,
                "pdf_pages": [p.pdf_page for p in safe_pages], "printed_pages": [p.printed_page for p in safe_pages],
                "section_title": chapter.title if chapter else "", "context": " ".join(parts),
                "citation": A.corpus._make_citation(volume.book, volume.volume, safe_pages, source_file=volume.source_file)}
        first_pi = maps[0]["page_index"]
        hit = A.corpus._make_hit(volume, volume.page_offsets[first_pi], volume.page_offsets[first_pi] + 1, "exact", 100, "")
        hit = A.corpus.enrich_hit_document(hit)
        metadata = hit.to_dict()
        for field in ("document_id", "work_title", "work_authors", "provenance_verified"):
            base[field] = metadata.get(field)
        if not C.admissible(base, "\n".join(parts), question):
            continue
        p = {**base, "index": index, "text": "\n".join(parts), "quote_segments": quote_segments,
             "source_segments": maps}
        # Full-page history is only accepted if the cited text still validates.
        passages.append(p)
        bases[index] = base
    if not used or not used <= set(bases):
        return None
    history_issues = A.AI_CLIENT.validate_grounded_answer(answer, passages)
    # The legacy validator associates every short quoted expression in a
    # sentence with its final reference, including analytical comparisons.
    # Keep these pre-existing issues visible without replacing the old essay.
    # Long quotations and quote blocks still require exact source verification.
    from ai import _inline_quotations, _citation_unit_refs, _DIRECT_QUOTE_ENDERS
    short_expression_issues, blocking_quotes = set(), set()
    by_index = {p["index"]: p for p in passages}
    for line in answer.splitlines():
        if line.lstrip().startswith(">"):
            plain = C._plain(line).strip('“”「」『』"')
            for index in C.reference_ids(line):
                if index in by_index and not A.AI_CLIENT._grounded_quote_excerpt(plain, by_index[index]):
                    blocking_quotes.add(f"quote_mismatch:{index}")
            continue
        for match in _inline_quotations(line):
            quote = match.group("quote")
            for index in _citation_unit_refs(line, match.close + 1):
                if index in by_index and not A.AI_CLIENT._grounded_quote_excerpt(quote, by_index[index]):
                    issue = f"quote_mismatch:{index}"
                    if len(C.key(quote)) < 20 and quote[-1:] not in _DIRECT_QUOTE_ENDERS:
                        short_expression_issues.add(issue)
                    else:
                        blocking_quotes.add(issue)
    # An unassigned quoted expression in existing analysis does not invalidate
    # the separately re-read, referenced evidence. Preserve that analysis and
    # surface its existing issue; never regenerate the whole answer because of it.
    permitted = {"uncited_direct_quote", "uncited_named_attribution", "generic_source_phrase"} | (short_expression_issues - blocking_quotes)
    if any(issue not in permitted for issue in history_issues):
        return None
    for p in passages:
        p["_history_verification_issues"] = history_issues
    return answer, passages, bases
