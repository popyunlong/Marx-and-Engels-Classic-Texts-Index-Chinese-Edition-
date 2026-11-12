"""Public-corpus-only evidence API, independent of browser search workflows."""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from urllib.parse import urlencode

from agent_api_store import APIError, setting
from build_index import normalize
from search_capacity import bounded_search


class QueryService:
    def __init__(self, corpus, store, public_books, version, base_url):
        self.corpus, self.store = corpus, store
        self.public_books, self.version, self.base_url = public_books, version, base_url

    def scope(self, tokens):
        public = set(self.public_books())
        if tokens is None or tokens == []:
            return {key: None for key in public}
        if not isinstance(tokens, list) or not 1 <= len(tokens) <= 100:
            raise APIError("invalid_scope", "scope 必须为文库范围标识数组，最多 100 项。")
        result = {}
        for token in tokens:
            if not isinstance(token, str):
                raise APIError("invalid_scope", "范围标识必须为字符串。")
            if token.startswith("book:"):
                book, volume = token[5:], None
            elif token.startswith("vol:"):
                book, _, number = token[4:].rpartition(":")
                if not number.isdigit():
                    raise APIError("invalid_scope", "卷册范围无效。")
                volume = int(number)
            else:
                raise APIError("invalid_scope", "仅接受公共文库的 book: 或 vol: 范围标识。")
            if book not in public or book not in self.corpus.books:
                raise APIError("invalid_scope", "所选文库不可公开查询。")
            if volume is not None and volume not in {v.volume for v in self.corpus.get_volumes(book)}:
                raise APIError("invalid_scope", "所选卷册不可查询。")
            if volume is None:
                result[book] = None
            elif book not in result:
                result[book] = {volume}
            elif result[book] is not None:
                result[book].add(volume)
        return result

    @staticmethod
    def source_id(vol):
        return hashlib.sha256(vol.source_file.encode()).hexdigest()[:32]

    def bibliography(self, book, volume):
        cfg = self.corpus.get_book_config(book).for_volume(volume)
        return {"publication_year": self.corpus._citation_year(book, volume),
                **{key: getattr(cfg, key, "") for key in ("title", "authors", "publisher", "edition_note", "source_edition", "isbn")}}

    def sources(self):
        return {"ok": True, "corpus_version": self.version(), "sources": [
            {"book": key, "scope": "book:" + key,
             "volumes": [{"volume": vol.volume, "scope": f"vol:{key}:{vol.volume}",
                          "source_id": self.source_id(vol), **self.bibliography(key, vol.volume)}
                         for vol in self.corpus.get_volumes(key)]}
            for key in self.corpus.books if key in self.public_books()]}

    def validate_search(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"q", "mode", "scope", "limit"}:
            raise APIError("invalid_request", "请使用 q、mode、scope 和 limit 参数。")
        q, mode, limit = payload.get("q"), payload.get("mode", "exact"), payload.get("limit", 10)
        if not isinstance(q, str) or not 2 <= len(normalize(q.strip())) or len(q) > 512:
            raise APIError("invalid_query", "查询需含至少两个有效字符，最多 512 字。")
        if not isinstance(mode, str) or mode not in {"exact", "cooccurrence", "near_text"}:
            raise APIError("invalid_mode", "不支持的检索模式。")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise APIError("invalid_limit", "limit 必须为 1–20 的整数。")
        scope = self.scope(payload.get("scope"))
        if mode == "near_text":
            if not payload.get("scope"):
                raise APIError("scope_required", "近似定位必须指定著作或卷册。")
            from search import MIN_FUZZY_QUERY_LEN
            if len(normalize(q)) < MIN_FUZZY_QUERY_LEN:
                raise APIError("query_too_short", f"近似定位至少需要 {MIN_FUZZY_QUERY_LEN} 个有效字符。")
        if mode == "cooccurrence" and not 2 <= len(self.corpus._export_cooccurrence_keywords(q.split())) <= 5:
            raise APIError("invalid_keywords", "同段共现需 2–5 个不同关键词，以空格分隔。")
        return q.strip(), mode, limit, scope

    @bounded_search
    def search(self, uid, payload, cancelled=lambda: False, *, validated=None):
        q, mode, limit, scope = validated if validated is not None else self.validate_search(payload)
        version = self.version()
        deadline = time.monotonic() + setting("TIMEOUT_SECONDS", 20)

        def check():
            if cancelled():
                raise APIError("cancelled", "查询已取消。", 408)
            if time.monotonic() >= deadline:
                raise APIError("query_timeout", "查询达到时间预算，请缩小范围。", 504)

        hits, truncated, complete = self.corpus.query_api(q, mode=mode, book_scope=scope, limit=limit, check=check)
        refs = [{"source": hit.source_file, "start": hit.norm_start, "end": hit.norm_end,
                 "mode": mode, "score": hit.score, "fuzzy_errors": hit.fuzzy_errors,
                 "exact_basis": hit.exact_basis, "ignored_layout_types": hit.ignored_layout_types,
                 "layout_hit_ref": hit.layout_hit_ref, "page_matches": hit.page_matches}
                for hit in hits]
        if version != self.version():
            raise APIError("corpus_changed", "语料版本已变化，请重新查询。", 409)
        ids = self.store.put_evidence(uid, version, refs)
        results = [self.result(hit, ref, evidence_id, version, 1500) for hit, ref, evidence_id in zip(hits, refs, ids)]
        check()
        return {"ok": True, "corpus_version": version, "results": results,
                "truncated": truncated, "search_complete": complete and not truncated,
                "warnings": ([] if complete else ["排版补充检索未完成，不能将零结果视为原文不存在。"])}

    def result(self, hit, ref, evidence_id, version, char_limit):
        vol = self.corpus.get_volume_by_source_file(hit.source_file)
        data = hit.to_dict()
        text, pages, clipped = self.raw_window(hit, char_limit)
        return {"evidence_id": evidence_id, "corpus_version": version, "source_id": self.source_id(vol),
                "book": hit.book, "volume": hit.volume, "bibliography": self.bibliography(hit.book, hit.volume),
                "work_title": hit.work_title, "section_title": hit.section_title,
                "text": text, "text_truncated": clipped, "context_pages": pages,
                "pdf_pages": data["pdf_pages"], "printed_pages": data["printed_pages"],
                "page_refs": data["page_refs"], "citations": data["citations"],
                "match_type": ref["mode"], "score": ref["score"], "fuzzy_errors": ref["fuzzy_errors"],
                "exact_basis": ref["exact_basis"] if ref["mode"] == "exact" else None,
                "ignored_layout_types": ref["ignored_layout_types"], "page_matches": ref["page_matches"],
                "verification": "normalized_text_match" if ref["mode"] == "exact" else "candidate_requires_review",
                "reader_url": self.base_url().rstrip("/") + "/viewer?" + urlencode({"file": hit.source_file, "page": hit.pages[0].pdf_page, "lr": ref["layout_hit_ref"]})}

    def raw_window(self, hit, char_limit):
        """Map normalized offsets back to actual text; never synthesize a quote."""
        vol = self.corpus.get_volume_by_source_file(hit.source_file)
        scope = self.corpus.document_scope_for_hit(hit)
        # Without a verified work boundary, expose the matched range only.
        lo = scope.norm_start if scope else hit.norm_start
        hi = scope.norm_end if scope else hit.norm_end
        first = max(0, vol.page_index_at(hit.norm_start) - 1)
        last = min(len(vol.pages) - 1, vol.page_index_at(hit.norm_start) + 1)
        start, end = max(lo, vol.page_offsets[first]), min(hi, vol.page_offsets[last + 1])
        pieces, page_refs, focus, count = [], [], 0, 0
        cache = OrderedDict()
        for index in range(first, last + 1):
            page = vol.pages[index]
            a, b = max(0, start - vol.page_offsets[index]), min(len(page.norm_text), end - vol.page_offsets[index])
            if a >= b:
                continue
            mapping = self.corpus._export_page_raw_map(page, cache)
            if mapping is None:
                raise APIError("text_unavailable", "当前原文映射不可核对，请在阅读器中查看。", 503)
            ra, rb = self.corpus.document_page_raw_bounds(page, mapping, a, b)
            if vol.page_offsets[index] <= hit.norm_start < vol.page_offsets[index + 1]:
                focus_at = max(a, hit.norm_start - vol.page_offsets[index])
                focus_raw, _ = self.corpus.document_page_raw_bounds(page, mapping, focus_at, min(focus_at + 1, b))
                focus = count + max(0, focus_raw - ra)
            piece = page.raw_text[ra:rb]
            pieces.append(piece)
            page_refs.append({"pdf_page": page.pdf_page, "printed_page": page.printed_page,
                              "start": count, "end": count + len(piece)})
            count += len(piece) + 1
        text = "\n".join(pieces)
        offset = max(0, min(focus - char_limit // 3, len(text) - char_limit))
        selected = text[offset:offset + char_limit]
        pages = [{**item, "start": max(0, item["start"] - offset), "end": min(len(selected), item["end"] - offset)}
                 for item in page_refs if item["end"] > offset and item["start"] < offset + len(selected)]
        return selected, pages, len(selected) < len(text)

    def prepare_passage(self, uid, evidence_id):
        version = self.version()
        ref = self.store.evidence(uid, evidence_id, version)
        vol = self.corpus.get_volume_by_source_file(ref["source"])
        if not vol or vol.book not in self.public_books():
            raise APIError("source_unavailable", "来源已不可公开查询。", 404)
        return version, ref, vol

    @bounded_search
    def passage(self, uid, evidence_id, *, prepared=None):
        version, ref, vol = prepared if prepared is not None else self.prepare_passage(uid, evidence_id)
        hit = self.corpus._make_hit(vol, ref["start"], ref["end"], "fuzzy" if ref["mode"] == "near_text" else "exact", ref["score"], "", fuzzy_errors=ref["fuzzy_errors"])
        result = self.result(hit, ref, evidence_id, version, 6000)
        if version != self.version():
            raise APIError("corpus_changed", "语料版本已变化，请重新查询。", 409)
        return {"ok": True, "result": result}
