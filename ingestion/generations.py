"""Keep unfinished citation jobs valid across verified append-only expansions.

No job hashes or user decisions are rewritten. The old generation is an alias
only for unchanged pages and bibliographic configuration, with its original
default book scope retained. Non-append repairs never receive this alias.
"""
from __future__ import annotations

import contextvars
import functools
import json
from pathlib import Path


def install(app_module, manifest_path: Path):
    if not manifest_path.is_file():
        return
    record = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_sha = app_module._citation_corpus_sha256
    current = original_sha()
    if record.get("current") != current or record.get("schema") != 1:
        raise RuntimeError("语料兼容性清单与当前数据库不符")
    ancestors = record.get("ancestors", {})
    context = contextvars.ContextVar("citation_generation", default=None)

    def runtime_sha():
        return context.get() or original_sha()

    app_module._citation_corpus_sha256 = runtime_sha

    def wrap_worker(function, field, get_job):
        @functools.wraps(function)
        def wrapped(job_id, *args, **kwargs):
            job = get_job(job_id)
            generation = job.get(field) if job else None
            token = context.set(generation if generation in ancestors else None)
            try:
                return function(job_id, *args, **kwargs)
            finally:
                context.reset(token)
        return wrapped

    tasks = app_module.citation_tasks
    for name in ["_citation_analysis_worker", "_citation_export_worker"]:
        setattr(app_module, name, wrap_worker(getattr(app_module, name), "corpus_sha256", tasks.get_job))
    original_select = tasks._selected_volumes

    def selected(corpus, scope):
        generation = context.get()
        if generation in ancestors and not [t for t in scope if not str(t).startswith("mylib:")]:
            scope = list(scope) + ["book:" + b for b in ancestors[generation]["books"]]
        return original_select(corpus, scope)

    tasks._selected_volumes = selected

    def compatible_claim(original, field, module, implementation):
        @functools.wraps(original)
        def claim(*args, **kwargs):
            if kwargs.get(field) != current:
                return original(*args, **kwargs)
            return implementation(module, [current, *ancestors], *args, **kwargs)
        return claim

    from .claims import citation, search_export
    tasks.claim_next_job = compatible_claim(tasks.claim_next_job, "corpus_sha256", tasks, citation)
    import search_exports
    app_module._search_export_worker = wrap_worker(app_module._search_export_worker, "corpus_version", search_exports.get_job)
    original_export_context = app_module._search_export_scope_context

    def export_context(job):
        generation = context.get()
        original = original_export_context(job)
        if generation in ancestors and not job.get("scope") and not job.get("book_filter"):
            job = {**job, "scope": ["book:" + b for b in ancestors[generation]["books"]]}
            bounded = original_export_context(job)
            return (bounded[0], original[1], original[2], original[3])
        return original

    app_module._search_export_scope_context = export_context
    search_exports.claim_next_job = compatible_claim(search_exports.claim_next_job, "corpus_version", search_exports, search_export)
