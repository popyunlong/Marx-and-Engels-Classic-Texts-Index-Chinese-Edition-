"""Full-quotation evidence for insertion into a paper. Positions are character offsets."""

from __future__ import annotations

from collections import defaultdict, OrderedDict
import re

from rapidfuzz.distance import Levenshtein

from build_index import normalize

POLICY_VERSION = 2
MAX_ERRORS = 2
MAX_OPTIONS = 8
MAX_WINDOWS_PER_QUOTE_VOLUME = 256
# Difference in a numeral, negation or named entity requires individual review.
_PROTECTED = re.compile(
    r"[\d零〇一二三四五六七八九十百千万亿两无不非未莫勿否]|"
    r"马克思|恩格斯|列宁|斯大林|毛泽东|习近平|邓小平|黑格尔|费尔巴哈"
)


def _raw_span(corpus, volume, start, end, cache):
    if not getattr(volume, "pages", None) or end <= start:
        return ""
    pieces = []
    first, last = volume.page_index_at(start), volume.page_index_at(end - 1)
    if any(volume.pages[i].pdf_page != volume.pages[i - 1].pdf_page + 1 for i in range(first + 1, last + 1)):
        return ""
    for i in range(first, last + 1):
        page = volume.pages[i]
        left = max(0, start - volume.page_offsets[i])
        right = min(len(page.norm_text), end - volume.page_offsets[i])
        if right <= left:
            continue
        mapping = corpus._export_page_raw_map(page, cache)
        if mapping is None:
            return ""
        pieces.append(page.raw_text[mapping[0][left]:mapping[1][right - 1]])
    raw = "\n".join(pieces)
    return raw if normalize(raw) == volume.norm_full[start:end] else ""


def _raw_offsets(text):
    normalized, offsets = [], []
    for i, character in enumerate(text):
        part = normalize(character)
        normalized.append(part)
        offsets.extend([i] * len(part))
    return "".join(normalized), offsets


def _bounds(offsets, start, end, size):
    left = offsets[start] if start < len(offsets) else size
    return left, offsets[end - 1] + 1 if end > start else left


def _material_change(text, left, right):
    # An insertion inside a name also changes the name.
    return any(
        (m.start() < right and left < m.end()) or (left == right and m.start() < left < m.end())
        for m in _PROTECTED.finditer(text)
    )


def compare_text(paper, source):
    query, qmap = _raw_offsets(paper)
    target, smap = _raw_offsets(source)
    if not query or not target or query != normalize(paper) or target != normalize(source):
        return None
    differences, anchors, risk = [], 0, False
    edits = Levenshtein.editops(query, target)
    for tag, i, j, k, l in edits.as_opcodes():
        if tag == "equal":
            anchors += (j - i) // 4
            continue
        pstart, pend = _bounds(qmap, i, j, len(paper))
        sstart, send = _bounds(smap, k, l, len(source))
        differences.append(dict(paper=paper[pstart:pend], source=source[sstart:send],
                                paper_start=pstart, paper_end=pend,
                                source_start=sstart, source_end=send))
        risk |= _material_change(query, i, j) or _material_change(target, k, l)
    return dict(policy_version=POLICY_VERSION, paper_text=paper, source_text=source,
                coverage=1.0, errors=len(edits), anchors=anchors, differences=differences,
                similarity=100 * (1 - len(edits) / max(len(query), len(target))),
                material_risk=risk, complete_search=False)


def _best_window(query, source, estimate):
    best = None
    for left in range(max(0, estimate - MAX_ERRORS), min(len(source), estimate + MAX_ERRORS + 1)):
        for length in range(len(query) - MAX_ERRORS, len(query) + MAX_ERRORS + 1):
            right = left + length
            if right > len(source):
                continue
            errors = Levenshtein.distance(query, source[left:right], score_cutoff=MAX_ERRORS)
            if errors <= MAX_ERRORS:
                rank = (errors, abs(length - len(query)), abs(left - estimate), left, right)
                if best is None or rank < best:
                    best = rank
    return best


def verify_many(corpus, volumes, texts, *, check_active=None):
    """Scan all selected volumes in one batch. A <=2-edit match retains one of 3 disjoint seeds."""
    import citation_assistant as core

    grouped = defaultdict(list)
    seeds = defaultdict(list)
    for raw in dict.fromkeys(texts):
        query = normalize(raw)
        if len(query) >= 10 and not re.search(r"…|⋯|\.{3,}", raw):
            grouped[query].append(raw)
    for query in grouped:
        for i in range(3):
            offset = i * len(query) // 3
            seed = query[offset:(i + 1) * len(query) // 3]
            seeds[seed].append((query, offset))
    if not seeds:
        return {}
    matcher = core._build_seed_matcher(seeds)
    hits = defaultdict(list)
    incomplete = set()

    def active():
        if check_active is not None and check_active() is False:
            raise TimeoutError("完整引文核验已中断")

    for volume in volumes:
        active()
        source = volume.norm_full
        checked = defaultdict(set)
        windows = defaultdict(list)
        for n, (pos, seed) in enumerate(core._iter_seed_hits(source, seeds, matcher)):
            if n % 64 == 0:
                active()
            for query, offset in seeds[seed]:
                if len(hits[query]) > MAX_OPTIONS:
                    incomplete.add(query)
                    continue
                estimate = pos - offset
                seen = checked[query]
                if estimate in seen:
                    continue
                if len(seen) >= MAX_WINDOWS_PER_QUOTE_VOLUME:
                    incomplete.add(query)
                    continue
                seen.add(estimate)
                match = _best_window(query, source, estimate)
                if match is not None:
                    windows[query].append(match)
        for query, matches in windows.items():
            kept = []
            for cost, _, _, start, end in sorted(matches):
                # Several anchors identify the same passage. Keep distinct occurrences.
                if any(abs(start - s) <= MAX_ERRORS and abs(end - e) <= MAX_ERRORS for _, s, e in kept):
                    # Competing equal-cost windows slightly offset in repetitive text
                    # cannot certify one exact location.
                    if any((s, e) != (start, end) and near_cost == cost
                           and abs(start - s) <= MAX_ERRORS and abs(end - e) <= MAX_ERRORS
                           for near_cost, s, e in kept):
                        incomplete.add(query)
                    continue
                kept.append((cost, start, end))
                hits[query].append((cost, start, end, volume))
                if len(hits[query]) > MAX_OPTIONS:
                    incomplete.add(query)
                    break

    results = {}
    cache = OrderedDict()
    for query, rows in hits.items():
        sorted_hits = sorted(rows, key=lambda row: (row[0], row[3].source_file, row[1]))
        for raw in grouped[query]:
            options = []
            complete = query not in incomplete
            for errors, start, end, volume in sorted_hits[:MAX_OPTIONS]:
                source_text = _raw_span(corpus, volume, start, end, cache)
                evidence = compare_text(raw, source_text)
                if evidence is None or evidence["errors"] != errors:
                    complete = False
                    continue
                hit = corpus._make_hit(volume, start, end, "fuzzy" if errors else "exact",
                                       round(evidence["similarity"]), raw, fuzzy_errors=errors)
                option = core._option_from_hit(hit)
                if not option.get("citation"):
                    complete = False
                    continue
                evidence.update(norm_start=start, norm_end=end)
                option["quote_evidence"] = evidence
                options.append(option)
            for option in options:
                option["quote_evidence"]["complete_search"] = complete
            results[raw] = options
    return results


def attest_located_options(corpus, volumes_by_file, paper, options):
    """For existing-note audit, validate the actual located span, including the tail."""
    for option in options:
        volume = volumes_by_file.get(option.get("source_file"))
        start, end = option.get("norm_start"), option.get("norm_end")
        if volume is None or not isinstance(start, int) or not isinstance(end, int):
            continue
        source = _raw_span(corpus, volume, start, end, OrderedDict())
        proof = compare_text(paper, source)
        if proof and proof["errors"] <= MAX_ERRORS:
            proof.update(norm_start=start, norm_end=end)
            option["quote_evidence"] = proof
        elif option["match_type"] == "exact" and source and normalize(source) != normalize(paper):
            option["partial_quote"] = True


def verify_options(corpus, volumes, paper_text, style=None, *, check_active=None):
    return verify_many(corpus, volumes, [paper_text], check_active=check_active).get(paper_text, [])


def selected_evidence(item):
    options = item.get("source_options") or []
    i = int(item.get("selected_option") or 0)
    if not 0 <= i < len(options):
        return {}
    return options[i].get("quote_evidence") or {}


def near_permission(item, reliable_page):
    """Return 'auto', 'manual', or '' from persisted, locally generated evidence."""
    opts = item.get("source_options") or []
    i = int(item.get("selected_option") or 0)
    ev = selected_evidence(item)
    if (ev.get("policy_version") != POLICY_VERSION or ev.get("coverage") != 1.0
            or not 1 <= ev.get("errors", 0) <= MAX_ERRORS or not reliable_page(opts[i])
            or normalize(item.get("paper_text") or "") != normalize(ev["paper_text"])
            or ev.get("similarity", 0) < 80 or item.get("verification_scope") != "full_quote"):
        return ""
    if (len(opts) == 1 and ev.get("complete_search") and ev.get("ai_confirmed") is True
            and ev.get("ai_verdict") == "minor_difference" and not ev.get("material_risk")
            and len(normalize(ev["paper_text"])) >= 20 and ev.get("similarity", 0) >= 92
            and ev.get("anchors", 0) >= 2):
        return "auto"
    return "manual"
