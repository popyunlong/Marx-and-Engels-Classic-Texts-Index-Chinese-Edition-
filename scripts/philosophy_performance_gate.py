#!/usr/bin/env python3
"""Compare representative existing-corpus search latency and load RSS.

The parent process launches one clean interpreter per database so peak memory from
the baseline cannot contaminate the candidate measurement.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_QUERIES = (
    "全世界无产者，联合起来",
    "哲学家们只是用不同的方式解释世界",
    "每个人的自由发展是一切人的自由发展的条件",
    "生产关系的总和构成社会的经济结构",
)


def _rss_bytes() -> int:
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        # Linux ru_maxrss is KiB; this worker is deployed on Linux.
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.999999) - 1))]


def _worker(db: Path, repeats: int, warmups: int, queries: tuple[str, ...]) -> dict:
    from search import Corpus

    load_started = time.perf_counter()
    corpus = Corpus(db_path=db)
    load_seconds = time.perf_counter() - load_started
    rss = _rss_bytes()

    for _ in range(warmups):
        for query in queries:
            corpus.search_grouped(query, group_limit=10)

    samples_ms: list[float] = []
    by_query: dict[str, list[float]] = {query: [] for query in queries}
    for _ in range(repeats):
        for query in queries:
            started = time.perf_counter()
            corpus.search_grouped(query, group_limit=10)
            elapsed = (time.perf_counter() - started) * 1000
            samples_ms.append(elapsed)
            by_query[query].append(elapsed)

    return {
        "database": str(db.resolve()),
        "load_seconds": round(load_seconds, 6),
        "rss_bytes": rss,
        "sample_count": len(samples_ms),
        "p50_ms": round(statistics.median(samples_ms), 6),
        "p95_ms": round(_percentile95(samples_ms), 6),
        "by_query_p95_ms": {
            query: round(_percentile95(values), 6) for query, values in by_query.items()
        },
    }


def _run_clean(db: Path, repeats: int, warmups: int, queries: tuple[str, ...]) -> dict:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--database",
        str(db),
        "--repeats",
        str(repeats),
        "--warmups",
        str(warmups),
    ]
    for query in queries:
        command.extend(("--query", query))
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(
            f"clean performance worker failed for {db}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--query", action="append")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--latency-limit", type=float, default=0.20)
    parser.add_argument("--memory-limit", type=float, default=0.15)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    queries = tuple(args.query or DEFAULT_QUERIES)

    if args.repeats < 1 or args.warmups < 0:
        parser.error("repeats must be positive and warmups non-negative")
    if args.worker:
        if not args.database:
            parser.error("--worker requires --database")
        print(json.dumps(_worker(args.database, args.repeats, args.warmups, queries), ensure_ascii=False))
        return 0
    if not args.baseline or not args.candidate:
        parser.error("--baseline and --candidate are required")

    baseline = _run_clean(args.baseline, args.repeats, args.warmups, queries)
    candidate = _run_clean(args.candidate, args.repeats, args.warmups, queries)
    latency_change = candidate["p95_ms"] / max(baseline["p95_ms"], 0.000001) - 1
    memory_change = candidate["rss_bytes"] / max(baseline["rss_bytes"], 1) - 1
    passed = latency_change <= args.latency_limit and memory_change <= args.memory_limit
    report = {
        "passed": passed,
        "limits": {"latency": args.latency_limit, "memory": args.memory_limit},
        "changes": {"latency": latency_change, "memory": memory_change},
        "baseline": baseline,
        "candidate": candidate,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
