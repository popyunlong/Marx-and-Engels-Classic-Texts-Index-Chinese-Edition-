"""Bounded, local-only quote variants. Similarity never means verbatim verification."""
from __future__ import annotations

import re
import time
import unicodedata
import contextvars
from functools import wraps
from difflib import SequenceMatcher
from ai_evidence import clean_evidence, exact_quote

KINDS = {"approximate_quote", "elided_quote", "pending_quote"}
ELLIPSIS = re.compile(r"(?:…{1,}|\.{3,}|〔略〕|\[略\])")
CRITICAL = re.compile(r"[不未无非没勿莫否\d一二三四五六七八九十百千万亿]")
_ACTIVE = contextvars.ContextVar("quote_variant_matcher", default=None)


def variant_session(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        passages = kwargs.get("grounding") or (args[2] if len(args) > 2 else [])
        token = _ACTIVE.set(Matcher(passages or []))
        try:
            return fn(*args, **kwargs)
        finally:
            _ACTIVE.reset(token)
    return wrapped


def current_matcher(passages):
    return _ACTIVE.get() or Matcher(passages)


def normalized(text):
    out, offsets = [], []
    for i, char in enumerate(text):
        for c in unicodedata.normalize("NFKC", char):
            if c.isalnum():
                out.append(c); offsets.append(i)
    return "".join(out), offsets


class Matcher:
    """One matcher per answer/checkpoint; no shared private evidence cache."""
    def __init__(self, passages, seconds=0.6):
        self.deadline = time.monotonic() + seconds
        self.cache = {}
        self.sources = []
        for p in passages:
            for segment in p.get("quote_segments") or clean_evidence(p.get("text", ""), p).quote_segments:
                # Never join across evidence-cleaning barriers.
                # Bounded windows overlap enough for the longest accepted quote.
                for start in range(0, len(segment), 1400):
                    window = segment[start:start+2000]
                    self.sources.append((p, window, *normalized(window)))

    def match(self, text, refs=(), explicit=True):
        cache_key = (text, tuple(refs), explicit)
        if cache_key in self.cache:
            return self.cache[cache_key]
        needle, _ = normalized(text)
        if len(needle) < (12 if explicit else 20) or len(needle) > 600:
            return None
        pool = [s for s in self.sources if not refs or s[0]["index"] in refs]
        if any(exact_quote(text, s[1]) for s in pool):
            self.cache[cache_key] = None
            return None
        # Numbered source clauses already have an exact recovery path. Give it
        # priority: omitting source-internal list labels must not turn several
        # independently verbatim clauses into one approximate/pending quote.
        label = r"(?:^|(?<=[，,。！？!?；;：:]))\s*[（(][0-9０-９一二三四五六七八九十]+[）)]\s*"
        if any(exact_quote(text, re.sub(label, "", s[1])) for s in pool):
            self.cache[cache_key] = None
            return None
        candidates = []
        for p, source, norm, positions in pool:
            if time.monotonic() >= self.deadline:
                break
            parts = [normalized(x)[0] for x in ELLIPSIS.split(text)]
            if len(parts) > 1 and all(len(x) >= 4 for x in parts):
                cursor, first = 0, None
                for part in parts:
                    at = norm.find(part, cursor)
                    if at < 0:
                        break
                    if first is None:
                        first = at
                    cursor = at + len(part)
                else:
                    candidates.append(self._result(p, text, source[positions[first]:positions[cursor-1]+1], "elided_quote", 0))
                continue
            # Seed alignment from unchanged runs, then examine only nearby spans.
            allowance = min(6, len(needle)//10)
            for block in SequenceMatcher(None, needle, norm, autojunk=False).get_matching_blocks():
                if block.size < max(6, len(needle)//4):
                    continue
                expected = block.b-block.a
                for left in range(max(0, expected-allowance), min(len(norm), expected+allowance)+1):
                    if time.monotonic() >= self.deadline:
                        break
                    for length in range(len(needle)-allowance, len(needle)+allowance+1):
                        right = left+length
                        if right > len(norm) or right <= left:
                            continue
                        from rapidfuzz.distance import Levenshtein
                        distance = Levenshtein.distance(needle, norm[left:right], score_cutoff=allowance)
                        if distance > allowance:
                            continue
                        literal = source[positions[left]:positions[right-1]+1]
                        result = self._result(p, text, literal, "approximate_quote", distance)
                        candidates.append(result)
        exhausted = time.monotonic() >= self.deadline
        if not candidates:
            result = None
            if exhausted and explicit and refs and pool:
                p, source, *_ = pool[0]
                result = self._result(p, text, source, "pending_quote", 0)
                result["verification_reason"] = "本次匹配时间预算已用尽，尚未确认文字对应关系"
        else:
            candidates.sort(key=lambda c: c["edit_count"])
            best = candidates[0]
            tied = [c for c in candidates if c["edit_count"] == best["edit_count"]]
            identities = {(c["source_identity"], c["source_text"]) for c in tied}
            if len(identities) > 1:
                best = dict(best, kind="pending_quote", verification_reason="来源不唯一")
            if exhausted:
                best = dict(best, kind="pending_quote", verification_reason="匹配预算已用尽，候选尚未完整核对")
            result = best
        self.cache[cache_key] = result
        return result

    @staticmethod
    def _result(p, text, source, kind, distance):
        a, b = normalized(text)[0], normalized(source)[0]
        changes = [{"answer": a[i:j], "source": b[k:l]} for op,i,j,k,l in SequenceMatcher(None,a,b,autojunk=False).get_opcodes() if op != "equal"]
        reason = ""
        if kind == "approximate_quote" and any(CRITICAL.search(d["answer"]+d["source"]) for d in changes):
            kind, reason = "pending_quote", "存在否定或数字等关键差异"
        return {"index": p["index"], "kind": kind, "answer_text": text, "source_text": source,
                "quote": text, "text_verified": False, "differences": changes,
                "edit_count": distance, "verification_reason": reason,
                "source_identity": str(p.get("source_file") or p.get("document_id") or p["index"])}


def variants(answer, passages, matcher=None):
    from ai import _inline_quotations
    from ai_citations import units, reference_ids, _plain
    matcher = matcher or current_matcher(passages)
    records = []
    for start, unit, excluded in units(answer):
        if excluded:
            continue
        refs = reference_ids(unit)
        quotes = list(_inline_quotations(unit))
        candidates = [(m.group("quote"), True) for m in quotes]
        if not quotes and not re.search(r"`|!?\[[^\]]+\]\(", unit):
            candidates = [(_plain(unit).strip('“”「」『』"'), unit.lstrip().startswith(">"))]
        for text, explicit in candidates:
            match = matcher.match(text, refs, explicit)
            if match:
                records.append({**match, "unit": unit.strip(), "answer_start": start,
                                "answer_end": start+len(unit), "quotes": [match["source_text"]]})
    return records


def restore_references(answer, passages):
    records = variants(answer, passages)
    edits = {}
    for r in records:
        if r["kind"] == "pending_quote":
            continue
        from ai_citations import reference_ids
        if r["index"] not in reference_ids(r["unit"]):
            edits.setdefault(r["answer_end"], []).append(r["index"])
    for end, ids in sorted(edits.items(), reverse=True):
        answer = answer[:end]+"".join(f"[{i}]" for i in dict.fromkeys(ids))+answer[end:]
    return answer
