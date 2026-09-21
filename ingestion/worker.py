from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import time
import uuid
from pathlib import Path

from .recognize import child, key, suspect
from .scheduler import Scheduler, YieldRequired
from .store import BudgetError, Store, samples
from . import audit


def price(provider, usage):
    # Rates are deployment configuration, recorded in reports. No unverified quote.
    prefix = "MIMO" if provider in {"mimo", "verify"} else "GLM"
    inp = float(os.environ.get(prefix + "_INPUT_YUAN_PER_MILLION", "1" if prefix == "MIMO" else "0"))
    out = float(os.environ.get(prefix + "_OUTPUT_YUAN_PER_MILLION", "2" if prefix == "MIMO" else "0"))
    prompt = max(0, int(usage.get("prompt_tokens", 0)))
    cached = min(prompt, max(0, int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))))
    cache_price = float(os.environ.get(prefix + "_CACHED_YUAN_PER_MILLION", ".02" if prefix == "MIMO" else "0"))
    return ((prompt - cached) * inp + cached * cache_price + int(usage.get("completion_tokens", 0)) * out) / 1e6


def handle(store, job, owner, result):
    provider = job["stage"]
    history = json.loads(job["result"])
    history.setdefault("runs", []).append({"stage": provider, **result})
    text, label, kind = result["text"], result.get("printed_page", ""), result["kind"]
    reason = suspect(result)
    fields = dict(result=history, text=text, label=label, kind=kind)
    if provider == "glm":
        reasons = audit.mandatory({**job, 'kind':kind, 'result':history})
        layer = result.get("text_layer", "")
        if len(key(layer)) >= 100:
            import difflib
            score = difflib.SequenceMatcher(None, key(layer), key(text), autojunk=False).ratio()
            history["text_layer_agreement"] = score
        audit.mark(history,reasons)
        # Missing folios and disagreement with a text layer first go to free OCR.
        needs_ocr = not label or history.get('text_layer_agreement',1)<.98
        next_stage = "ocr" if reason or result.get("invalid") else ("mimo" if reasons else ('ocr_check' if needs_ocr else "done"))
        store.finish(job, owner, stage=next_stage, **fields)
    elif provider == "ocr":
        # OCR does not invent folios. Retain GLM metadata only pending visual MiMo check.
        fields["label"] = job["label"]
        audit.mark(history,['OCR 兜底页全查'])
        store.finish(job, owner, stage="mimo", **fields)
    elif reason and not (not text.strip() and result.get("ink", 1) < .0005):
        store.finish(job, owner, stage="review", result=history, error="MiMo 无法可靠确认：" + reason)
    elif provider == "mimo":
        agrees = key(text) == key(job["text"])
        if agrees:
            # A blank needs both independent OCR and image evidence, not a model label.
            if not text.strip() and not any(r["stage"] == "ocr" and not r["text"].strip() for r in history["runs"]):
                store.finish(job, owner, stage="ocr", result=history)
            else:
                store.finish(job, owner, stage="done", checked=1, **fields)
        else:
            history["proposed"] = result
            store.finish(job, owner, stage="verify", result=history)
            store.event("disagreement", job["book"], job["page"], requires_second_read=True)
    else:
        proposed = history.get("proposed", {})
        agrees = key(text) == key(proposed.get("text", "")) and label == proposed.get("printed_page", "")
        if agrees and job["repairs"] < 2:
            store.finish(job, owner, stage="done", checked=1, repairs=job["repairs"] + 1, **fields)
            audit.confirmed_error(store,job,history)
            store.event("repair", job["book"], job["page"], before=job["text"], after=text,
                        evidence=result.get("image_sha256"))
        else:
            store.finish(job, owner, stage="review", result=history, error="两次图像复核仍有分歧，需要核对原图")


def advance(store):
    with store.connect() as c:
        books = [dict(r) for r in c.execute("SELECT b.*,a.pilot FROM books b JOIN batches a ON a.id=b.batch WHERE b.status='processing'")]
    for b in books:
        if b["pilot"]:
            continue
        audit.schedule(store,b)
        with store.connect() as c:
            remaining = c.execute("SELECT COUNT(*) FROM pages WHERE book=? AND stage NOT IN ('done','review')", (b["id"],)).fetchone()[0]
            disputed = c.execute("SELECT COUNT(*) FROM pages WHERE book=? AND stage='review'", (b["id"],)).fetchone()[0]
            if remaining == 0:
                c.execute("UPDATE books SET status=? WHERE id=?", ("review" if disputed else "assembling", b["id"]))


def run(store, scheduler, stop=None):
    ctx = mp.get_context("spawn")
    active = {}
    owner = uuid.uuid4().hex
    next_tick = next_advance = 0.0
    assembly = None
    capacity = {"glm": 6, "mimo": 2, "ocr": 1}
    budget_wait = {}
    try:
        while stop is None or not stop.is_set():
            now = time.monotonic()
            if now >= next_tick:
                scheduler.tick()
                next_tick = now + 2
            for pid, unit in list(active.items()):
                process, pipe, job, call_id, start = unit
                outcome = None
                if pipe.poll():
                    try:
                        outcome = pipe.recv()
                    except EOFError:
                        outcome = {"ok": False, "error": "识别子进程退出"}
                elif now - start >= 55 or not process.is_alive():
                    outcome = {"ok": False, "error": "单页超过 55 秒或子进程退出，已让行"}
                if outcome is None:
                    continue
                if process.is_alive():
                    process.join(timeout=.05)
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
                pipe.close()
                active.pop(pid)
                if outcome["ok"]:
                    result = outcome["result"]
                    if call_id:
                        cost = price(job["stage"], result["usage"]) if result.get("usage") else None
                        store.charged(call_id, cost, result.get("usage", {}), result["seconds"])
                    handle(store, job, owner, result)
                else:
                    if call_id:
                        store.charged(call_id, None, {}, now - start, "uncertain")
                    error = outcome["error"]
                    fallback = job["attempts"] >= 2 or "HTTP 400" in error
                    stage = ("ocr" if job["stage"] == "glm" else "review") if fallback else job["stage"]
                    store.finish(job, owner, stage=stage, error=error,
                                 retry_at=0 if fallback else time.time() + min(60, 5 * 2**job["attempts"]))
            for provider in ("verify", "mimo", "ocr", "glm"):
                if budget_wait.get(provider,0)>now:continue
                group = "mimo" if provider == "verify" else provider
                count = sum(("mimo" if u[2]["stage"] == "verify" else u[2]["stage"]) == group for u in active.values())
                if count >= capacity[group]:
                    continue
                try:
                    with scheduler.admit():
                        job = store.claim(provider, owner)
                    if job is None:
                        continue
                    call_id = None
                    if provider != "ocr":
                        # Conservative maximum per image at the configured tariff.
                        reserve = price(provider, {"prompt_tokens": 32000, "completion_tokens": 8192})
                        call_id = store.reserve(job, provider, reserve)
                    recv, send = ctx.Pipe(duplex=False)
                    process = ctx.Process(target=child, args=(send, provider, job["path"], job["page"]), daemon=True)
                    process.start()
                    send.close()
                    active[process.pid] = (process, recv, job, call_id, time.monotonic())
                except YieldRequired:
                    break
                except BudgetError as exc:
                    # Budget waiting is not a failed recognition attempt.
                    with store.connect() as c:
                        c.execute("UPDATE pages SET owner='',lease=0,retry_at=? WHERE book=? AND page=? AND owner=?",
                                  (time.time()+30,job['book'],job['page'],owner))
                    budget_wait[provider]=now+30
                    store.set_state("budget", str(exc))
                    continue  # Free OCR/GLM units can still progress under the same cap.
            if now >= next_advance:
                advance(store)
                next_advance = now + 5
            if assembly:
                process, pipe, book, started = assembly
                if pipe.poll():
                    try:
                        outcome = pipe.recv()
                    except EOFError:
                        outcome = {"ok": False, "error": "候选构建进程退出"}
                    process.join(timeout=.5)
                    if not outcome["ok"]:
                        with store.connect() as c:
                            c.execute("UPDATE books SET status='review',error=? WHERE id=?", (outcome["error"], book))
                    pipe.close()
                    assembly = None
                elif now - started > 55 or not scheduler.heartbeat:
                    process.terminate()
                    process.join(timeout=1)
                    pipe.close()
                    assembly = None
            if assembly is None and not active:
                try:
                    with scheduler.admit():
                        with store.connect() as c:
                            row = c.execute("SELECT id FROM books WHERE status='assembling' AND paused=0 LIMIT 1").fetchone()
                    if row:
                        from .assemble import assemble_child
                        recv, send = ctx.Pipe(duplex=False)
                        process = ctx.Process(target=assemble_child, args=(send, str(store.root), row[0]), daemon=True)
                        process.start()
                        send.close()
                        assembly = (process, recv, row[0], time.monotonic())
                except YieldRequired:
                    pass
            time.sleep(.2)
    finally:
        for process, pipe, job, call_id, start in active.values():
            process.terminate()
            process.join(timeout=1)
            pipe.close()
        if assembly:
            assembly[0].terminate()
            assembly[0].join(timeout=1)
            assembly[1].close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("/home/data/marx-ingestion"))
    p.add_argument("--citation-db", type=Path, default=Path("/var/www/.marx_search_full/citation_assistant.sqlite3"))
    p.add_argument("--health-url", default="http://127.0.0.1:8000/api/runtime")
    args = p.parse_args()
    store = Store(args.root)
    run(store, Scheduler(store, args.citation_db, health_url=args.health_url))


if __name__ == "__main__":
    main()
