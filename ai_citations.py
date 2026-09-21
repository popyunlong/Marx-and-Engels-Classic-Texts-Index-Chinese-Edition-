"""AI-only evidence admission, citation ledger and bounded additive revision.

No database writes, global search changes or default answer-generation calls.
Quotation checks deliberately retain punctuation; fuzzy similarity is only used
to avoid presenting duplicate candidates, never to establish a quotation.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from difflib import SequenceMatcher

from ai_evidence import clean_evidence, exact_quote, _quote_map

REF = re.compile(r"\[(\d+(?:\s*[,，、]\s*\d+)*)\]")
AUX = re.compile(r"^(?:名目索引|人名索引|主题索引|注释(?:与|及)?索引|索引|目录|注释|编者注|编者说明|出版说明)(?:$|[（(：:\s])")
EXCLUDE = re.compile(r"(?:不能|不应|不宜|不再|不予|不作为|不算|不计|排除|剔除|未采用|未使用).{0,22}(?:证据|来源|引文|引用|计入|独立|采用)|(?:索引|重复材料).{0,16}(?:不能|不算|不计|排除)")
AUDIT_HEADING = re.compile(r"(?:关于)?(?:证据|引文|引用|来源)(?:数量|不足|局限|排除|清单)|参考文献|来源列表")
MORE = re.compile(r"(?:增加|增补|补充|补足|补齐|再找|多找|多给|扩充|更多|丰富|充实).{0,16}(?:引文|引用|原文|证据|出处|来源)|(?:引文|引用|原文|证据|出处|来源).{0,16}(?:增加|补充|补足|补齐|更多|上限|满额)")
NUMBER = re.compile(r"(?:提供|整理|列出|给出|需要|至少|达到|增加至|补足到|补齐到)?\s*(\d{1,3}|十二|三十)\s*条.{0,18}(?:引文|引用|原文|证据|来源)|(?:引文|引用|证据|来源).{0,12}?(\d{1,3}|十二|三十)\s*条")


def enabled() -> bool:
    return os.environ.get("AI_CITATION_LEDGER_ENABLED", "1").lower() not in {"0", "false", "off"}


def key(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(text)) if c.isalnum())


def reference_ids(text: str) -> list[int]:
    return list(dict.fromkeys(int(n) for m in REF.finditer(text) for n in re.findall(r"\d+", m.group(1))))


def number_final_citations(answer: str, cards: list[dict]) -> tuple[str, list[dict], dict[int, int]]:
    """Number the completed answer by first use, without changing its evidence.

    Candidate IDs remain untouched throughout retrieval and augmentation. Only
    the completed response is renumbered; code and Markdown links are literals.
    """
    by_id = {int(c.get("grounding_index") or c.get("review_index") or i + 1): c for i, c in enumerate(cards)}
    tokens = re.compile(r"(?P<literal>^```[^\n]*\n[\s\S]*?^```[^\n]*$|^~~~[^\n]*\n[\s\S]*?^~~~[^\n]*$|`[^`\n]*`|!?\[[^\]\n]*\]\([^\n)]*\)|^\[\d+\]:[^\n]*)|(?P<ref>\[\d+(?:\s*[,，、]\s*\d+)*\])", re.M)
    order = []
    for match in tokens.finditer(answer):
        if match.group("ref"):
            for index in reference_ids(match.group()):
                if index in by_id and index not in order:
                    order.append(index)
    order.extend(index for index in by_id if index not in order)
    mapping = {index: i + 1 for i, index in enumerate(order)}
    def replace(match):
        if not match.group("ref"):
            return match.group()
        return re.sub(r"\d+", lambda n: str(mapping.get(int(n.group()), int(n.group()))), match.group())
    numbered = []
    for index in order:
        card = dict(by_id[index])
        fields = [field for field in ("grounding_index", "review_index") if field in card] or ["grounding_index"]
        for field in fields:
            card[field] = mapping[index]
        numbered.append(card)
    return tokens.sub(replace, answer), numbered, mapping


def auxiliary_requested(question: str) -> bool:
    return bool(re.search(r"(?:研究|分析|比较|核查|讨论|引用).{0,12}(?:索引|编者|注释)|(?:索引|编者说明|编者注).{0,12}(?:研究|分析|比较|内容)", question)) and not bool(re.search(r"(?:不要|排除|不引用).{0,8}(?:索引|注释|编者)", question))


def admissible(metadata: dict, text: str, question: str) -> bool:
    """Do not confuse an index *pointer to body text* with an index page."""
    if auxiliary_requested(question):
        return True
    labels = [re.sub(r"\s+", "", str(metadata.get(k) or "")) for k in ("section_title", "work_title", "title")]
    if any(AUX.search(label) for label in labels):
        return False
    if (any(label in {"前言", "出版前言", "编者前言"} for label in labels)
            and not metadata.get("work_authors")
            and re.search(r"本卷|本集|编译局|编者|编写组", text)
            and re.search(r"(?:马克思|恩格斯).{0,25}(?:写道|指出|认为|研究|著作|发现|阐明|说明|著述)", text)):
        return False
    # Only inspect page-heading lines, not mentions of notes inside an argument.
    heading_lines = text.splitlines()[:3]
    if not any(labels):
        for segment in metadata.get("_ai_quote_segments", []):
            heading_lines.extend(str(segment).splitlines()[:3])
    if any(AUX.fullmatch(re.sub(r"\s+", "", line)) for line in heading_lines):
        return False
    return True


class EvidencePool:
    def __init__(self, question: str = ""):
        self.compare_versions = bool(re.search(r"(?:版本|译本|译文|不同版).{0,15}(?:比较|对照|差异)|(?:比较|对照).{0,15}(?:版本|译本|译文|不同版)", question))
        self.items: list[tuple[str, str]] = []

    def add(self, text: str, metadata: dict | None = None) -> bool:
        value = key(text)
        edition = str((metadata or {}).get("source_file") or (metadata or {}).get("citation") or "")
        if not value:
            return False
        for previous, previous_edition in self.items:
            if self.compare_versions and edition != previous_edition:
                continue
            if value == previous or value in previous:
                return False
            if len(value) >= 60 and len(previous) >= 60:
                matcher = SequenceMatcher(None, previous, value, autojunk=False)
                overlap = sum(m.size for m in matcher.get_matching_blocks())
                # A substantially new sentence keeps a neighbouring passage useful.
                if overlap / len(value) >= .88 and len(value) - overlap < 45:
                    return False
        self.items.append((value, edition))
        return True


def _plain(text: str) -> str:
    return re.sub(r"[*_`]|^[ \t]*>[ \t]*", "", REF.sub("", text), flags=re.M).strip()


def _sentence_parts(line: str):
    """Split prose only outside sealed quotations, code and Markdown links."""
    from ai import _inline_quotations
    protected = [(m.start(), m.close + 1) for m in _inline_quotations(line)]
    protected += [(m.start(), m.end()) for m in re.finditer(r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)", line)]
    start = 0
    for ender in re.finditer(r"[。！？!?]", line):
        if ender.start() < start or any(a <= ender.start() < b for a, b in protected):
            continue
        end = ender.end()
        suffix = re.match(r'[”’」』）)\"*_]*(?:[ \t]*\[\d+(?:\s*[,，、]\s*\d+)*\])*', line[end:])
        end += len(suffix.group())
        yield start, line[start:end]
        start = end
    if line[start:].strip():
        yield start, line[start:]


def units(text: str):
    """Sentence-local citation units; quote blocks stay sealed, code is ignored."""
    offset = 0
    fenced = False
    audit = False
    lines = text.splitlines(keepends=True)
    grouped = []
    for line in lines:
        if line.lstrip().startswith(">") and grouped and grouped[-1].lstrip().startswith(">"):
            grouped[-1] += line
        else:
            grouped.append(line)
    for line in grouped:
        stripped = line.strip()
        if re.match(r"^(?:```|~~~)", stripped):
            fenced = not fenced
        if fenced or re.match(r"^(?:```|~~~)", stripped):
            offset += len(line)
            continue
        if re.match(r"^#{1,6}\s", stripped):
            audit = bool(AUDIT_HEADING.search(stripped))
            offset += len(line)
            continue
        if re.fullmatch(r"(?:参考文献|来源列表|来源清单|引文清单)[：:]?", stripped.strip("*")):
            audit = True
            offset += len(line)
            continue
        # A following marker belongs to the preceding sentence, including spaces.
        parts = [(0, line)] if stripped.startswith(">") else _sentence_parts(line.rstrip("\n"))
        for start, part in parts:
            if EXCLUDE.search(_plain(part)) and not re.search('[“”「」『』"]', part):
                for clause in re.finditer(r"[^；;]+[；;]?", part):
                    yield offset + start + clause.start(), clause.group(), audit or bool(EXCLUDE.search(_plain(clause.group())))
            else:
                yield offset + start, part, audit or bool(EXCLUDE.search(_plain(part)))
        offset += len(line)


def _matched_source(quote: str, passage: dict) -> str:
    segments = passage.get("quote_segments") or clean_evidence(passage.get("text", ""), passage).quote_segments
    span = next((span for segment in segments if (span := exact_quote(quote, segment))), "")
    return span if span and exact_quote(span, passage.get("text", "")) else ""


def _repair_nested_source_spans(answer: str, passages: list[dict]) -> tuple[str, list[str]]:
    """Recover source-internal quotes across a misplaced outer quote boundary.

    Only an exact continuous source span can cross existing quote marks. A
    variant removes one existing outer pair, never original words/punctuation;
    the complete replacement is rechecked against the safe source segment.
    """
    from ai import _inline_quotations
    evidence = {int(p["index"]): p for p in passages}
    edits = []
    for start, unit, excluded in units(answer):
        if excluded or unit.lstrip().startswith(">"):
            continue
        quoted = list(_inline_quotations(unit))
        if not quoted:
            continue
        refs = reference_ids(unit)
        pool = [evidence[i] for i in refs if i in evidence] if refs else passages
        protected = [(m.start(), m.end()) for m in re.finditer(r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)", unit)]
        variants = [(unit, list(range(len(unit))), None)]
        for m in quoted:
            if len(key(m.group("quote"))) >= 8:
                positions = [i for i in range(len(unit)) if i not in {m.start(), m.close}]
                variants.append((''.join(unit[i] for i in positions), positions, m))
        candidates = []
        for text, positions, removed in variants:
            norm, offsets = _quote_map(text)
            for p in pool:
                for segment in p.get("quote_segments") or clean_evidence(p.get("text", ""), p).quote_segments:
                    source_norm, _ = _quote_map(segment)
                    for block in SequenceMatcher(None, norm, source_norm, autojunk=False).get_matching_blocks():
                        if block.size < 24:
                            continue
                        left = offsets[block.a]; right = offsets[block.a + block.size - 1] + 1
                        while left < right and text[left] in ' ，,。！？!?；;：:':
                            left += 1
                        value = text[left:right]
                        if len(key(value)) < 24:
                            continue
                        a, b = positions[left], positions[right-1] + 1
                        if removed:
                            if not (a <= removed.start() and b >= removed.close):
                                continue
                            if a == removed.start()+1:
                                a = removed.start()
                            if b == removed.close:
                                b += 1
                        # Require at least one complete quote pair belonging to
                        # the original, surrounded by original narrative text.
                        inner = list(_inline_quotations(value))
                        if not any(m.start() > 0 and m.close < len(value)-1 for m in inner):
                            continue
                        if any(a < y and b > x for x,y in protected):
                            continue
                        if not (a == 0 or unit[a-1] in ' ，,。！？!?；;：:'):
                            continue
                        if not (b == len(unit) or unit[b] in ' ，,。！？!?；;：:['):
                            continue
                        if not _matched_source(value, p):
                            continue
                        matches = [q for q in pool if _matched_source(value, q)]
                        if len(matches) != 1:
                            continue
                        if any(m.start() <= a and b <= m.close+1 for m in quoted):
                            continue
                        index = int(p['index'])
                        marker = '' if index in refs else f'[{index}]'
                        candidates.append((a,b,f'“{value}”{marker}'))
        selected = []
        for a,b,replacement in sorted(set(candidates), key=lambda x: (-(x[1]-x[0]), x[0])):
            if any(a < y and b > x for x,y in selected):
                continue
            selected.append((a,b)); edits.append((start+a,start+b,replacement))
    for a,b,replacement in sorted(edits,reverse=True):
        answer = answer[:a]+replacement+answer[b:]
    return answer, ['nested_verbatim_boundary_restored'] if edits else []


def remove_redundant_quote_intros(answer: str, passages: list[dict]) -> tuple[str, list[str]]:
    """Keep the complete adjacent block when its lead only repeats its prefix.

    Same-source exact checks are mandatory. Intervening analysis, different
    references, and quotations reused elsewhere are untouched.
    """
    from ai import _inline_quotations
    evidence = {int(p['index']): p for p in passages}
    lines = answer.splitlines(keepends=True)
    fenced = False
    changed = False
    for i, line in enumerate(lines):
        if re.match(r'^\s*(?:```|~~~)', line):
            fenced = not fenced
        if fenced or line.lstrip().startswith(('>', '#', '```', '~~~')):
            continue
        quotes = list(_inline_quotations(line))
        if not quotes:
            continue
        quote = quotes[-1]
        if not re.fullmatch(r'\s*[:：]?\s*', line[quote.end():]):
            continue
        prefix = line[:quote.start()].rstrip()
        if prefix and (not prefix.endswith(('，', ',', '：', ':')) or ':' not in line[quote.end():].replace('：', ':')):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        block = []
        while j < len(lines) and lines[j].lstrip().startswith('>'):
            block.append(lines[j].lstrip()[1:].strip())
            j += 1
        if not block:
            continue
        body = '\n'.join(block)
        refs = reference_ids(quote.group('refs') or '')
        if len(refs) != 1 or refs != reference_ids(body) or refs[0] not in evidence:
            continue
        short = quote.group('quote')
        full = REF.sub('', body).strip()
        short_norm, _ = _quote_map(short)
        full_norm, _ = _quote_map(full)
        if len(key(short)) < 12 or not full_norm.startswith(short_norm):
            continue
        if not _matched_source(short, evidence[refs[0]]) or not _matched_source(full, evidence[refs[0]]):
            continue
        lines[i] = prefix.rstrip('，,：:') + '：\n' if prefix else ''
        changed = True
    return ''.join(lines), ['adjacent_quote_intro_compacted'] if changed else []


def repair_missing_references(answer: str, passages: list[dict]) -> tuple[str, list[str]]:
    """Add quotation boundaries and references without changing any prose.

    Exact matching blocks are candidate discovery only. Every accepted span is
    checked again against one safe, continuous source segment. No similarity
    score establishes evidence; common short terms are not discovered this way.
    """
    from ai import _inline_quotations, _citation_unit_refs
    answer, nested_issues = _repair_nested_source_spans(answer, passages)
    answer, duplicate_issues = remove_redundant_quote_intros(answer, passages)
    evidence = {int(p["index"]): p for p in passages}
    edits, issues = [], list(nested_issues + duplicate_issues)
    for start, unit, excluded in units(answer):
        if excluded:
            continue
        refs = [i for i in reference_ids(unit) if i in evidence]
        quoted = list(_inline_quotations(unit))
        protected = [(m.start(), m.end()) for m in quoted]
        protected += [(m.start(), m.end()) for m in re.finditer(r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)|\[\d+(?:\s*[,，、]\s*\d+)*\]", unit)]

        def source_for(value, preferred):
            matches = [i for i in preferred if i in evidence and _matched_source(value, evidence[i])]
            if len(matches) == 1:
                return matches[0]
            if matches:
                return None
            matches = [i for i, p in evidence.items() if _matched_source(value, p)]
            return matches[0] if len(matches) == 1 else None

        if unit.lstrip().startswith(">"):
            value = _plain(unit).strip('“”「」『』"')
            index = source_for(value, refs)
            if index and index not in refs:
                end = start + len(unit.rstrip())
                edits.append((end, end, f"[{index}]"))
                issues.append("unmarked_verbatim_reference_restored")
            continue
        for m in quoted:
            if any(a <= start + m.start() < b for a, b, _ in edits):
                continue
            local_refs = _citation_unit_refs(unit, m.close + 1)
            # An explicit short term may be an analytical label. Do not add a
            # new source for it unless the writer already supplied a reference.
            if len(key(m.group("quote"))) < (4 if local_refs else 8):
                continue
            index = source_for(m.group("quote"), local_refs)
            # The writer may close a quote halfway through a verbatim sentence.
            # Extend only across text already present in the answer, anchored to
            # this exact source. Never complete a quote by adding missing words.
            immediate = reference_ids(m.group("refs") or "")
            if index and (not immediate or immediate == [index]):
                tail_start = m.end()
                # A following quoted term may be an inner quotation in the
                # same original sentence. Exact source matching decides whether
                # it belongs; code, links and another reference still stop us.
                stops = [x.start() for x in re.finditer(r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)|\[\d+(?:\s*[,，、]\s*\d+)*\]", unit)
                         if x.start() >= tail_start]
                stop = min(stops, default=len(unit))
                tail = unit[tail_start:stop]
                tail_norm, tail_positions = _quote_map(tail)
                needle, _ = _quote_map(m.group("quote"))
                extension_end = 0
                p = evidence[index]
                for segment in p.get("quote_segments") or clean_evidence(p.get("text", ""), p).quote_segments:
                    source_norm, _ = _quote_map(segment)
                    at = source_norm.find(needle)
                    while at >= 0:
                        suffix = source_norm[at+len(needle):]
                        n = 0
                        while n < min(len(suffix), len(tail_norm)) and suffix[n] == tail_norm[n]:
                            n += 1
                        if n:
                            end = tail_positions[n-1] + 1
                            value = tail[:end]
                            boundary = end == len(tail.rstrip()) or tail[end:end+1] in "，,。！？!?；;：: \t"
                            source_boundary = n == len(suffix) or suffix[n:n+1] in "，,。！？!?；;：:"
                            if len(key(value)) >= 2 and boundary and source_boundary and _matched_source(m.group("quote") + value, p):
                                extension_end = max(extension_end, tail_start + end)
                        at = source_norm.find(needle, at+1)
                if extension_end:
                    marker = f"[{index}]" if immediate or index not in local_refs else ""
                    literal = m.group("quote") + unit[tail_start:extension_end]
                    edits.append((start+m.start(), start+extension_end, f"“{literal}”{marker}"))
                    protected.append((m.start(), extension_end))
                    issues.append("verbatim_quote_boundary_extended")
                    continue
            if index and index not in local_refs:
                pos = start + m.close + 1
                edits.append((pos, pos, f"[{index}]"))
                issues.append("unmarked_verbatim_reference_restored")

        # Work on each unquoted run separately; never jump across a reference,
        # emphasis delimiter or quote and turn the intervening analysis into a quote.
        protected += [(m.start(), m.end()) for m in re.finditer(r"\*+|_+|^\s*(?:[-+]|\d+[.)、])\s+", unit)]
        cuts = sorted({0, len(unit), *(p for pair in protected for p in pair)})
        candidates = []
        for left, right in zip(cuts, cuts[1:]):
            if any(a <= left < b for a, b in protected):
                continue
            run = unit[left:right]
            norm, offsets = _quote_map(run)
            if len(key(run)) < 12:
                continue
            for p in passages:
                if exact_quote(run.strip(), p.get("text", "")) and not _matched_source(run.strip(), p):
                    continue
                for segment in p.get("quote_segments") or clean_evidence(p.get("text", ""), p).quote_segments:
                    source_norm, _ = _quote_map(segment)
                    for block in SequenceMatcher(None, norm, source_norm, autojunk=False).get_matching_blocks():
                        if block.size < 12:
                            continue
                        a, b = offsets[block.a], offsets[block.a + block.size - 1] + 1
                        while a < b and run[a] in ' \t，,。！？!?；;：:“”「」『』"':
                            a += 1
                        value = run[a:b]
                        boundary = not a or run[a-1] in "，,。！？!?；;：: \t"
                        end_boundary = b == len(run) or run[b] in "，,。！？!?；;：: \t"
                        source_start = block.b + len(_quote_map(run[offsets[block.a]:a])[0])
                        source_end = block.b + block.size
                        source_quoted = source_start > 0 and source_end < len(source_norm) and source_norm[source_start-1] == source_norm[source_end] == '"'
                        if len(key(value)) < 12 or not (source_quoted or (boundary and end_boundary)):
                            continue
                        if not (source_start == 0 or source_norm[source_start-1] in '，,。！？!?；;：:"'):
                            continue
                        if not (source_end == len(source_norm) or source_norm[source_end-1] in '，,。！？!?；;：:"' or source_norm[source_end] in '，,。！？!?；;：:"'):
                            continue
                        local_refs = _citation_unit_refs(unit, left + b)
                        index = source_for(value, local_refs)
                        if index:
                            candidates.append((left + a, left + b, index, local_refs))
        selected = []
        for a, b, index, local_refs in sorted(candidates, key=lambda r: (-(r[1]-r[0]), r[0])):
            if any(a < y and b > x for x, y in selected):
                continue
            selected.append((a, b))
            value = unit[a:b]
            marker = "" if index in local_refs else f"[{index}]"
            edits.append((start+a, start+b, f"“{value}”{marker}"))
            issues.append("verbatim_quote_delimited")
            if marker:
                issues.append("unmarked_verbatim_reference_restored")
    for a, b, replacement in sorted(set(edits), reverse=True):
        answer = answer[:a] + replacement + answer[b:]
    return answer, list(dict.fromkeys(issues))


def ledger(answer: str, passages: list[dict]) -> dict:
    # Delayed import avoids a cycle when ai.py calls this module after generation.
    from ai import _inline_quotations
    evidence = {int(p["index"]): p for p in passages}
    records = []
    edits = []
    for start, unit, excluded in units(answer):
        refs = [i for i in reference_ids(unit) if i in evidence]
        if not refs:
            continue
        if excluded:
            # Retain the explanation, but make these ordinary candidate names so
            # exports cannot accidentally turn them back into scholarly footnotes.
            edits.append((start, start + len(unit), REF.sub(lambda m: "（候选" + m.group(1) + "）", unit)))
            continue
        plain = _plain(unit)
        quotes = [m.group("quote") for m in _inline_quotations(unit)]
        if unit.lstrip().startswith(">"):
            quotes = [plain.strip('“”「」『』"')]
        for idx in refs:
            p = evidence[idx]
            segments = p.get("quote_segments") or clean_evidence(p.get("text", ""), p).quote_segments
            matched = []
            for quote in quotes + [plain.strip('“”「」『』"')]:
                if len(key(quote)) < 4:
                    continue
                span = next((s for segment in segments if (s := exact_quote(quote, segment))), "")
                if span and exact_quote(span, p.get("text", "")) and span not in matched:
                    matched.append(span)
            source_ranges = []
            normalized, positions = _quote_map(p.get("text", ""))
            for quote in matched:
                needle, _ = _quote_map(quote)
                at = normalized.find(needle)
                if needle and at >= 0:
                    left, right = positions[at], positions[at + len(needle) - 1] + 1
                    source_ranges.append({"start": left, "end": right,
                        "pdf_pages": list(dict.fromkeys(s["pdf_page"] for s in p.get("source_segments", [])
                            if s.get("pdf_page") and s["start"] < right and s["end"] > left))})
            records.append({"index": idx, "unit": unit.strip(), "answer_start": start,
                            "answer_end": start + len(unit), "source_ranges": source_ranges,
                            "kind": "quote" if matched else "paraphrase", "quotes": matched})
    for record in records:
        shift = sum(len(replacement) - (b - a) for a, b, replacement in edits if b <= record["answer_start"])
        record["answer_start"] += shift
        record["answer_end"] += shift
    for a, b, replacement in reversed(edits):
        answer = answer[:a] + replacement + answer[b:]
    used = sorted({r["index"] for r in records})
    quotes = {key(q) for r in records for q in r["quotes"]}
    return {"answer_markdown": answer, "used_indices": used, "citation_records": records,
            "citation_stats": {"eligible_candidates": len(passages), "effective_sources": len(used), "direct_quotes": len(quotes)},
            "issues": ["excluded_reference_not_counted"] if edits else []}


def numbered_verification(verification: dict, answer: str, passages: list[dict], mapping: dict[int, int]) -> dict:
    """Refresh positions after final numbering; preserve existing audit status."""
    details = ledger(answer, [dict(p, index=mapping[p["index"]]) for p in passages if p["index"] in mapping])
    return {**verification, **{k: details[k] for k in ("answer_markdown", "used_indices", "citation_records")}}


def augmentation_target(question: str, cap: int, existing: int | None = None) -> int | None:
    if os.environ.get("AI_CITATION_AUGMENT_ENABLED", "1").lower() in {"0", "false", "off"}:
        return None
    if re.search(r"(?:不要|无需|不必|不用).{0,8}(?:增加|增补|补充|补足|凑满)", question):
        return None
    numbers = [m.group(1) or m.group(2) for m in NUMBER.finditer(question)]
    if numbers:
        n = numbers[-1]
        count = {"十二": 12, "三十": 30}.get(n, int(n) if n.isdigit() else cap)
        incremental = existing is not None and re.search(r"(?:增加|补充|增补|再找|多给|多找)\s*\d+\s*条", question) and not re.search(r"(?:至|达到|总共|共计|补足|补齐)", question)
        return min(cap, count + (existing if incremental else 0))
    return cap if MORE.search(question) else None


def augmentation_deadline(started: float) -> float:
    try:
        seconds = max(30, int(os.environ.get("AI_CITATION_AUGMENT_BUDGET_SECONDS", "900")))
    except ValueError:
        seconds = 900
    return started + seconds


def augment(answer: str, passages: list[dict], *, question: str, target: int,
            deadline: float, retrieve, write, verify, cancelled=lambda: False) -> dict:
    """Only insert validated additions. Existing paragraphs and numbering survive.

    Callbacks keep credentials, scope enforcement and billing in the application.
    Every retrieval round changes the direction. No fixed success-round limit.
    """
    current = ledger(answer, passages)
    answer = current["answer_markdown"]
    initial = len(current["used_indices"])
    initial_indices = list(current["used_indices"])
    tried: set[tuple[int, str]] = set()
    trace = []
    no_new = 0
    round_no = 0
    reason = "target_reached"
    while len(current["used_indices"]) < target:
        if cancelled() or time.monotonic() >= deadline - 8:
            reason = "cancelled" if cancelled() else "time_budget"
            break
        unused = [p for p in passages if p["index"] not in current["used_indices"] and (p["index"], key(p["text"])) not in tried]
        if unused:
            # A batch can cover several gaps, without overloading the revision.
            batch = unused[:min(8, target - len(current["used_indices"]))]
            tried.update((p["index"], key(p["text"])) for p in batch)
            try:
                additions = write(answer, batch, deadline)
                accepted = 0
                for addition in additions:
                    text = str(addition.get("text") or "").strip()
                    anchor = str(addition.get("after") or "").strip()
                    if not text or len(key(text)) < 45 or re.match(r"^#{1,6}\s", text):
                        continue
                    if not anchor or answer.count(anchor) != 1:
                        continue
                    # Insertions may end a paragraph, never splice inside a word.
                    pos = answer.index(anchor) + len(anchor)
                    if answer[pos:pos + 1] not in {"", "\n"}:
                        continue
                    repaired = verify(text, passages)
                    checked = ledger(repaired.get("answer_markdown", ""), passages)
                    fresh = set(checked["used_indices"]) - set(current["used_indices"])
                    if not fresh or not fresh <= {p["index"] for p in batch}:
                        continue
                    if len(set(current["used_indices"]) | fresh) > target:
                        continue
                    old_quotes = [key(q) for r in current["citation_records"] for q in r["quotes"]]
                    if not any(r["index"] in fresh and any(not any(key(q) in old for old in old_quotes) for q in r["quotes"]) for r in checked["citation_records"]):
                        continue
                    if any("unverified" in i or "dequoted" in i for i in repaired.get("issues", [])):
                        continue
                    if key(checked["answer_markdown"]) in key(answer):
                        continue
                    answer = answer[:pos] + "\n\n" + checked["answer_markdown"] + answer[pos:]
                    current = ledger(answer, passages)
                    accepted += len(fresh)
                trace.append({"stage": "write", "attempted": [p["index"] for p in batch], "added": accepted})
            except Exception as exc:
                trace.append({"stage": "write", "error": type(exc).__name__})
                reason = "generation_or_quota_limit"
                break
            continue
        if no_new >= 2:
            reason = "no_new_evidence"
            break
        try:
            round_no += 1
            count = retrieve(round_no, answer, passages, deadline)
            no_new = no_new + 1 if not count else 0
            trace.append({"stage": "retrieve", "round": round_no, "added": count})
        except Exception as exc:
            trace.append({"stage": "retrieve", "error": type(exc).__name__})
            reason = "retrieval_or_quota_limit"
            break
    current["answer_markdown"] = answer
    current["augmentation"] = {"requested": True, "target": target, "initial": initial, "initial_indices": initial_indices,
                               "added": len(current["used_indices"]) - initial,
                               "effective": len(current["used_indices"]), "stop_reason": reason, "trace": trace}
    return current


def decode_additions(response: str) -> list[dict]:
    from ai import _extract_json_object
    value = _extract_json_object(response)
    if not isinstance(value, dict):
        raise ValueError("No structured additions")
    return [item for item in value.get("additions", []) if isinstance(item, dict)]
