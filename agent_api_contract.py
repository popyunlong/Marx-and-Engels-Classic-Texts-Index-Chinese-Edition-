"""Shared, dependency-free REST/MCP contract. No corpus imports in the adapter."""
ERROR = {"type": "object", "required": ["code", "message", "retryable", "retry_after_seconds"],
         "properties": {"code": {"type": "string"}, "message": {"type": "string"},
                        "retryable": {"type": "boolean"}, "retry_after_seconds": {"type": "integer"}}}
def record(properties):
    return {"type": "object", "required": list(properties), "properties": properties}


def array(items):
    return {"type": "array", "items": items}


TEXT = {"type": "string"}
NULLABLE_TEXT = {"type": ["string", "null"]}
PAGE = {"type": "integer", "minimum": 1}
OFFSET = {"type": "integer", "minimum": 0}
VERSION = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
BIBLIOGRAPHY = record({
    "publication_year": {"type": ["string", "integer"]}, "title": TEXT,
    "authors": array(TEXT), "publisher": TEXT, "edition_note": TEXT,
    "source_edition": TEXT, "isbn": TEXT})
PAGE_REFERENCE = record({
    "pdf_page": PAGE, "printed_page": NULLABLE_TEXT, "display_label": TEXT,
    "numbering_system": TEXT, "segment_id": TEXT, "segment_title": TEXT, "status": TEXT})
CONTEXT_PAGE = record({"pdf_page": PAGE, "printed_page": NULLABLE_TEXT,
                       "start": OFFSET, "end": OFFSET})
CONTEXT_PAGE["description"] = "start/end are Unicode character offsets in text (end exclusive), not UTF-16 offsets."
LAYOUT_MATCH = record({"pdf_page": PAGE, "raw_start": OFFSET, "raw_end": OFFSET, "text": TEXT})
LAYOUT_MATCH["description"] = "Original page fragments retained as layout-match evidence; offsets refer to the full raw page."
EVIDENCE = record({
    "evidence_id": VERSION, "corpus_version": VERSION,
    "source_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    "book": TEXT, "volume": {"type": "integer"}, "bibliography": BIBLIOGRAPHY,
    "work_title": TEXT, "section_title": NULLABLE_TEXT,
    "text": {"type": "string", "maxLength": 6000}, "text_truncated": {"type": "boolean"},
    "context_pages": array(CONTEXT_PAGE), "pdf_pages": array(PAGE),
    "printed_pages": array(NULLABLE_TEXT), "page_refs": array(PAGE_REFERENCE),
    "citations": {"type": "object", "additionalProperties": TEXT},
    "match_type": {"type": "string", "enum": ["exact", "cooccurrence", "near_text"]},
    "score": {"type": "number"}, "fuzzy_errors": {"type": ["integer", "null"], "minimum": 0},
    "exact_basis": {"type": ["string", "null"], "enum": ["canonical", "layout", None]},
    "ignored_layout_types": array(TEXT), "page_matches": array(LAYOUT_MATCH),
    "verification": {"type": "string", "enum": ["normalized_text_match", "candidate_requires_review"]},
    "reader_url": {"type": "string", "format": "uri"}})
SOURCE_VOLUME = record({"volume": {"type": "integer"}, "scope": TEXT,
                        "source_id": EVIDENCE["properties"]["source_id"],
                        **BIBLIOGRAPHY["properties"]})
SOURCE = record({"book": TEXT, "scope": TEXT, "volumes": array(SOURCE_VOLUME)})
RATE = record({"per_minute": {"type": "integer", "minimum": 1},
               "remaining": OFFSET, "reset_after_seconds": OFFSET})
SCHEDULING = record({
    "heavy_concurrency": {"type": "integer", "minimum": 1},
    "light_concurrency": {"type": "integer", "minimum": 1},
    "near_text_concurrency": {"const": 1},
    "user_data_concurrency": {"type": "integer", "minimum": 1},
    "user_control_concurrency": {"const": 1}, "queue": {"const": False}})
ERROR_OUTPUT = record({"ok": {"const": False}, "error": ERROR})
SUCCESS_OUTPUTS = {
    "list_sources": record({"ok": {"const": True}, "corpus_version": VERSION, "sources": array(SOURCE)}),
    "search_corpus": record({"ok": {"const": True}, "corpus_version": VERSION,
        "results": {**array(EVIDENCE), "maxItems": 20}, "truncated": {"type": "boolean"},
        "search_complete": {"type": "boolean"}, "warnings": array(TEXT)}),
    "get_passage": record({"ok": {"const": True}, "result": EVIDENCE}),
    "get_api_limits": record({"ok": {"const": True}, "status": {"const": "available"},
        "global_concurrency": {"type": "integer", "minimum": 1, "description": "Global heavy-request concurrency; light requests have an independent limit in scheduling."},
        "user_concurrency": {"type": "integer", "minimum": 1}, "max_results": {"const": 20},
        "query_budget_seconds": {"type": "integer", "minimum": 1}, "rate": RATE,
        "daily_quota": {"type": "null"}, "consumes_site_ai_tokens": {"const": False}, "scheduling": SCHEDULING}),
}
# MCP requires an object at the root; nested schemas are inline for clients
# that do not resolve document-local OpenAPI references in tools/list.
OUTPUTS = {name: {"type": "object", "oneOf": [schema, ERROR_OUTPUT]}
           for name, schema in SUCCESS_OUTPUTS.items()}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
SEARCH = {"type": "object", "required": ["q"], "additionalProperties": False, "properties": {
    "q": {"type": "string", "minLength": 2, "maxLength": 512},
    "mode": {"type": "string", "enum": ["exact", "cooccurrence", "near_text"], "default": "exact"},
    "scope": {"type": "array", "maxItems": 100, "items": {"type": "string", "pattern": "^(book|vol):"}},
    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}}}
PASSAGE = {"type": "object", "required": ["evidence_id"], "additionalProperties": False,
           "properties": {"evidence_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}}
TOOLS = {
    "list_sources": ("列出已发布公共文库、版本及卷册范围。先选择文库再查原文。", EMPTY, "GET", "/sources"),
    "search_corpus": ("检索原文候选。exact 为规范化精确匹配，cooccurrence 为 2–5 个空格分隔关键词共现；near_text 为有编辑距离校验的近似定位，必须指定 scope。同账号数据请求串行调用；仅 retryable=true 的限流或执行前忙碌可按 retry_after_seconds 退避，最多重试两次。超时或执行状态不明不自动重发。", SEARCH, "POST", "/search"),
    "get_passage": ("读取证据原文及同一作品内最多相邻各一页的上下文。核对版本、印刷页和措辞；近似或共现候选不代表观点已获证实。引用失效时重新检索。", PASSAGE, "GET", "/passages/"),
    "get_api_limits": ("读取当前账号限制与剩余短窗查询额度；多个客户端和 Key 共用额度。", EMPTY, "GET", "/limits"),
}


def openapi():
    paths = {}
    for name, (description, schema, method, path) in TOOLS.items():
        route = path + "{evidence_id}" if name == "get_passage" else path
        operation = {"operationId": name, "description": description, "security": [{"BearerKey": []}],
                     "responses": {str(code): {"description": text, "content": {"application/json": {
                         "schema": SUCCESS_OUTPUTS[name] if code == 200 else ERROR_OUTPUT}},
                         **({"headers": {"Retry-After": {"description": "Seconds before retrying.",
                              "schema": {"type": "integer", "minimum": 1}}}} if code in (429, 503) else {})}
                                   for code, text in ((200, "Success"), (400, "Invalid input"), (401, "Invalid key"),
                                                      (403, "Access denied"), (404, "Evidence unavailable"), (405, "Method not allowed"), (408, "Cancelled"),
                                                      (409, "Corpus changed"), (413, "Request too large"),
                                                      (429, "Shared limit; see Retry-After"), (503, "Unavailable"), (504, "Query budget exhausted"))}}
        if method == "POST":
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        if name == "get_passage":
            operation["parameters"] = [{"name": "evidence_id", "in": "path", "required": True, "schema": schema["properties"]["evidence_id"]}]
        paths[route] = {method.lower(): operation}
    return {"openapi": "3.1.0", "info": {"title": "公共文献查询 API", "version": "1.0.0"},
            "servers": [{"url": "/api/v1"}], "paths": paths,
            "components": {"securitySchemes": {"BearerKey": {"type": "http", "scheme": "bearer"}}}}
