"""Run and score the anonymous MiMo/DeepSeek production-sample quality gate.

Input is JSONL. Each row must contain ``id``, ``feature``, ``comparison`` (``base``
or ``pro``), ``messages`` and ``anonymized: true``. ``run`` writes a blind review
file and a separate answer key. Reviewers add scores to the blind rows, then
``summarize`` enforces the 8·15 rollout thresholds.

Secrets are read only from MIMO_API_KEY and DEEPSEEK_API_KEY/ZAI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FEATURES = {"mascot", "research_chat", "ai_reader", "research_review", "structured_search"}
WEIGHTS = {
    "citation_accuracy": 0.35,
    "instruction_following": 0.20,
    "completeness": 0.15,
    "argument_quality": 0.15,
    "persona_tone": 0.05,
}
OBJECTIVE_WEIGHTS = {"latency_score": 0.05, "cost_score": 0.05}
LEAK_RE = re.compile(r"<(?:think|thinking|analysis)>|\b(?:we need|let me reason|chain of thought)\b", re.I)
PII_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?<!\d)\d{18,24}(?!\d)"),
)


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: JSON 无效: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no}: 每行必须是 JSON object")
        rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _validate_sample(row: dict) -> None:
    if not row.get("anonymized"):
        raise ValueError(f"sample {row.get('id')!r} 未显式标记 anonymized=true")
    if str(row.get("feature") or "") not in FEATURES:
        raise ValueError(f"sample {row.get('id')!r} feature 不在允许集")
    if str(row.get("comparison") or "") not in {"base", "pro"}:
        raise ValueError(f"sample {row.get('id')!r} comparison 必须为 base/pro")
    messages = row.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"sample {row.get('id')!r} 缺少 messages")
    raw = json.dumps(messages, ensure_ascii=False)
    if any(pattern.search(raw) for pattern in PII_PATTERNS):
        raise ValueError(f"sample {row.get('id')!r} 疑似包含邮箱/电话/IP/证件或订单号")
    if row.get("contains_personal_upload"):
        raise ValueError(f"sample {row.get('id')!r} 标记为包含个人上传文档")


def _usage(payload: dict) -> dict:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "cached_prompt_tokens": int(prompt_details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
    }


def _answer(payload: dict) -> str:
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return str(content or "")


def _cost_micros(provider: str, model: str, usage: dict, occurred_at: str) -> int:
    """Official per-token cost at call time, in integer microyuan."""
    prices = {
        "mimo-v2.5": (20_000, 1_000_000, 2_000_000),
        "mimo-v2.5-pro": (25_000, 3_000_000, 6_000_000),
    }
    when = datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    if provider == "deepseek" and when < datetime(2026, 8, 16, 16, tzinfo=timezone.utc):
        prices.update({
            "deepseek-v4-flash": (20_000, 1_000_000, 2_000_000),
            "deepseek-v4-pro": (25_000, 3_000_000, 6_000_000),
        })
    elif provider == "deepseek":
        beijing = when.astimezone(timezone(timedelta(hours=8)))
        peak = 9 <= beijing.hour < 12 or 14 <= beijing.hour < 18
        prices.update({
            "deepseek-v4-flash": (100_000, 3_000_000, 9_000_000) if peak else (50_000, 1_500_000, 4_500_000),
            "deepseek-v4-pro": (300_000, 9_000_000, 27_000_000) if peak else (150_000, 4_500_000, 13_500_000),
        })
    cache_price, input_price, output_price = prices[model]
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    cached = min(prompt, max(0, int(usage.get("cached_prompt_tokens") or 0)))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    numerator = cached * cache_price + (prompt - cached) * input_price + completion * output_price
    return math.ceil(numerator / 1_000_000)


def _call(*, provider: str, model: str, messages: list[dict], max_tokens: int, effort: str) -> dict:
    if provider == "mimo":
        key = str(os.environ.get("MIMO_API_KEY") or "").strip()
        base = str(os.environ.get("MIMO_BASE_URL") or "https://api.xiaomimimo.com/v1").rstrip("/")
        headers = {"api-key": key}
    else:
        key = str(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ZAI_API_KEY") or "").strip()
        base = str(os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        headers = {"Authorization": f"Bearer {key}"}
    if not key:
        raise RuntimeError(f"{provider} API key 未通过环境变量配置")
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False, "temperature": 0.2}
    body["max_completion_tokens" if provider == "mimo" else "max_tokens"] = max_tokens
    body["thinking"] = {"type": "disabled" if effort == "off" else "enabled"}
    if provider == "deepseek" and effort in {"low", "high", "max"}:
        body["reasoning_effort"] = effort
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    occurred_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
        error = ""
    except urllib.error.HTTPError as exc:
        payload = {}
        error = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:300]}"
    except Exception as exc:  # noqa: BLE001
        payload = {}
        error = str(exc)[:300]
    usage = _usage(payload)
    return {
        "text": _answer(payload),
        "usage": usage,
        "cost_micros": _cost_micros(provider, model, usage, occurred_at),
        "occurred_at": occurred_at,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "error": error,
    }


def run(args: argparse.Namespace) -> int:
    samples = _read_jsonl(args.samples)
    if len(samples) < args.min_samples:
        raise ValueError(f"样本数 {len(samples)} 少于门禁要求 {args.min_samples}")
    if len({str(row.get("id") or "") for row in samples}) != len(samples):
        raise ValueError("sample id 必须唯一且非空")
    missing_features = FEATURES - {str(row.get("feature") or "") for row in samples}
    if missing_features:
        raise ValueError(f"样本未覆盖全部生产功能: {sorted(missing_features)}")
    if {str(row.get("comparison") or "") for row in samples} != {"base", "pro"}:
        raise ValueError("样本必须同时覆盖 base 与 pro 两组对照")
    rng = random.Random(args.seed)
    blind_rows: list[dict] = []
    key_rows: list[dict] = []
    for row in samples:
        _validate_sample(row)
        comparison = str(row["comparison"])
        max_tokens = max(256, min(128_000, int(row.get("max_completion_tokens") or args.max_tokens)))
        if comparison == "base":
            targets = [("mimo", "mimo-v2.5", "off"), ("deepseek", "deepseek-v4-flash", "off")]
        else:
            targets = [("mimo", "mimo-v2.5-pro", "on"), ("deepseek", "deepseek-v4-pro", str(row.get("deepseek_effort") or "high"))]
        results = [
            {"provider": p, "model": m, "effort": e, **_call(provider=p, model=m, messages=row["messages"], max_tokens=max_tokens, effort=e)}
            for p, m, e in targets
        ]
        rng.shuffle(results)
        labels = ["A", "B"]
        blind = {
            "sample_id": row["id"],
            "feature": row["feature"],
            "comparison": comparison,
            "responses": {
                label: {"text": result["text"]}
                for label, result in zip(labels, results, strict=True)
            },
            "review": {
                label: {**{name: None for name in WEIGHTS}, "quote_correct": None, "quote_total": None, "format_failure": None}
                for label in labels
            },
        }
        key = {
            "sample_id": row["id"],
            "comparison": comparison,
            "answers": {
                label: {k: result[k] for k in (
                    "provider", "model", "effort", "usage", "latency_ms", "cost_micros", "occurred_at", "error"
                )}
                for label, result in zip(labels, results, strict=True)
            },
        }
        blind_rows.append(blind)
        key_rows.append(key)
    _write_jsonl(args.blind_output, blind_rows)
    _write_jsonl(args.key_output, key_rows)
    print(json.dumps({"ok": True, "samples": len(samples), "blind_output": str(args.blind_output), "key_output": str(args.key_output)}, ensure_ascii=False))
    return 0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)])


def summarize(args: argparse.Namespace) -> int:
    reviewed = {str(row["sample_id"]): row for row in _read_jsonl(args.reviewed)}
    keys = {str(row["sample_id"]): row for row in _read_jsonl(args.key)}
    if len(reviewed) < args.min_samples or set(reviewed) != set(keys):
        raise ValueError("评审文件与 answer key 样本不完整或不一致")
    groups: dict[str, dict[str, list[dict]]] = {}
    for sample_id, row in reviewed.items():
        key = keys[sample_id]
        comparison = str(key["comparison"])
        identities = key["answers"]
        positive_latencies = [float(value.get("latency_ms") or 0) for value in identities.values() if float(value.get("latency_ms") or 0) > 0]
        positive_costs = [float(value.get("cost_micros") or 0) for value in identities.values() if float(value.get("cost_micros") or 0) > 0]
        best_latency = min(positive_latencies) if positive_latencies else 0.0
        best_cost = min(positive_costs) if positive_costs else 0.0
        for label, identity in identities.items():
            review = (row.get("review") or {}).get(label) or {}
            if any(review.get(name) is None for name in WEIGHTS):
                raise ValueError(f"sample {sample_id} / {label} 评分未完成")
            latency = float(identity.get("latency_ms") or 0)
            cost = float(identity.get("cost_micros") or 0)
            latency_score = 100.0 if not best_latency else (100.0 * best_latency / latency if latency > 0 else 0.0)
            cost_score = 100.0 if not best_cost else (100.0 * best_cost / cost if cost > 0 else 0.0)
            quality = (
                sum(float(review[name]) * weight for name, weight in WEIGHTS.items())
                + latency_score * OBJECTIVE_WEIGHTS["latency_score"]
                + cost_score * OBJECTIVE_WEIGHTS["cost_score"]
            )
            text = str(((row.get("responses") or {}).get(label) or {}).get("text") or "")
            quote_total = max(0, int(review.get("quote_total") or 0))
            quote_correct = max(0, min(quote_total, int(review.get("quote_correct") or 0)))
            item = {
                "quality": quality,
                "quote_total": quote_total,
                "quote_correct": quote_correct,
                "empty": not bool(text.strip()),
                "leak": bool(LEAK_RE.search(text)),
                "format_failure": bool(review.get("format_failure")),
                "latency_ms": latency,
                "cost_micros": cost,
                "latency_score": latency_score,
                "cost_score": cost_score,
                "error": bool(identity.get("error")),
            }
            groups.setdefault(comparison, {}).setdefault(str(identity["provider"]), []).append(item)
    reports: dict[str, dict] = {}
    all_pass = True
    for comparison, providers in groups.items():
        report: dict[str, Any] = {"providers": {}}
        for provider, items in providers.items():
            count = len(items)
            quotes = sum(x["quote_total"] for x in items)
            report["providers"][provider] = {
                "samples": count,
                "quality_score": round(statistics.mean(x["quality"] for x in items), 3),
                "quote_accuracy": round(sum(x["quote_correct"] for x in items) / quotes, 5) if quotes else 1.0,
                "empty_rate": sum(x["empty"] for x in items) / count,
                "leak_rate": sum(x["leak"] for x in items) / count,
                "format_failure_rate": sum(x["format_failure"] for x in items) / count,
                "error_rate": sum(x["error"] for x in items) / count,
                "p95_latency_ms": round(_p95([x["latency_ms"] for x in items])),
                "total_cost_micros": round(sum(x["cost_micros"] for x in items)),
            }
        mimo = report["providers"].get("mimo", {})
        baseline = report["providers"].get("deepseek", {})
        checks = {
            "quality_at_least_95pct": mimo.get("quality_score", 0) >= baseline.get("quality_score", 0) * 0.95,
            "quote_drop_at_most_2pp": mimo.get("quote_accuracy", 0) >= baseline.get("quote_accuracy", 0) - 0.02,
            "p95_at_most_1_5x": mimo.get("p95_latency_ms", math.inf) <= baseline.get("p95_latency_ms", 0) * 1.5,
        }
        for metric in ("empty_rate", "leak_rate", "format_failure_rate"):
            checks[f"{metric}_within_gate"] = (
                mimo.get(metric, 1) <= 0.01 and mimo.get(metric, 1) <= baseline.get(metric, 0) + 0.005
            )
        report["checks"] = checks
        report["passed"] = all(checks.values())
        all_pass = all_pass and report["passed"]
        reports[comparison] = report
    result = {"passed": all_pass, "sample_count": len(reviewed), "comparisons": reports}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all_pass else 5


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("samples", type=Path)
    run_parser.add_argument("--blind-output", type=Path, required=True)
    run_parser.add_argument("--key-output", type=Path, required=True)
    run_parser.add_argument("--min-samples", type=int, default=60)
    run_parser.add_argument("--max-tokens", type=int, default=6000)
    run_parser.add_argument("--seed", type=int, default=8152026)
    run_parser.set_defaults(handler=run)
    summary_parser = sub.add_parser("summarize")
    summary_parser.add_argument("reviewed", type=Path)
    summary_parser.add_argument("--key", type=Path, required=True)
    summary_parser.add_argument("--output", type=Path, required=True)
    summary_parser.add_argument("--min-samples", type=int, default=60)
    summary_parser.set_defaults(handler=summarize)
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
