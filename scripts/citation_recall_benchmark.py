from __future__ import annotations

"""Run the privacy-safe citation-recall golden set.

The checked-in cases are synthetic and contain no uploaded paper text.  This
benchmark exercises the same deterministic matcher used by the worker and
reports quality/performance metrics separately so a recall gain cannot hide a
precision or auto-adoption regression.
"""

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import citation_assistant as tasks

DEFAULT_GOLD = PROJECT_ROOT / "tests" / "data" / "citation_recall_gold.json"


class GoldenCorpus:
    def __init__(self, sources: list[dict]):
        self.books: dict[str, list[SimpleNamespace]] = {"gold": []}
        self._source_by_file: dict[str, dict] = {}
        for index, source in enumerate(sources, start=1):
            source_id = str(source["id"])
            source_file = f"gold/{source_id}.pdf"
            text = str(source["text"])
            volume = SimpleNamespace(
                book="gold",
                volume=index,
                source_file=source_file,
                display_title=f"黄金集来源 {source_id}",
                norm_full=tasks.normalize(text),
            )
            self.books["gold"].append(volume)
            self._source_by_file[source_file] = source

    def _make_hit(
        self,
        volume,
        start: int,
        end: int,
        match_type: str,
        score: int,
        paper_text: str,
        fuzzy_errors: int | None = None,
    ) -> dict:
        source = self._source_by_file[str(volume.source_file)]
        printed = str(source.get("printed_page") or "")
        source_id = str(source["id"])
        return {
            "book": "gold",
            "volume": int(volume.volume),
            "source_file": str(volume.source_file),
            "display_title": str(volume.display_title),
            "citation_title": str(volume.display_title),
            "printed_pages": [printed] if printed else [],
            "pdf_pages": [int(volume.volume)],
            "match_type": match_type,
            "score": int(score),
            "fuzzy_errors": fuzzy_errors,
            "citation": f"黄金集来源 {source_id}",
            "citations": {"mkszyj": f"黄金集来源 {source_id}"},
            "context": str(source["text"]),
        }

    @staticmethod
    def locate_quote(*args, **kwargs) -> list:
        # The benchmark targets citation_assistant's batched matcher.  Its
        # explicit-quote fallback is deliberately disabled so it cannot mask a
        # failed seed/review path by running a separate full-corpus fuzzy scan.
        return []


def _source_id(option: dict) -> str:
    return Path(str(option.get("source_file") or "")).stem


def run(gold_path: Path = DEFAULT_GOLD) -> dict:
    cases = json.loads(gold_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    tracemalloc.start()
    suite_started = time.perf_counter()
    for case in cases:
        paper_chars = list(str(case["paper_text"]))
        for index in case.get("mutation_indices") or []:
            paper_chars[int(index)] = "错"
        paper = "".join(paper_chars)
        paragraph = f"前文。\u201c{paper}。\u201d后文。" if case.get("quoted") else f"前文。{paper}。后文。"
        extracted = {"paragraphs": [{
            "index": 0,
            "text": paragraph,
            "section_id": "body",
            "note_refs": [],
            "unsupported": False,
        }]}
        started = time.perf_counter()
        candidates, _, _ = tasks.analyze_extraction(
            extracted,
            GoldenCorpus(list(case.get("sources") or [])),
            selected_sections=["body"],
            scope_tokens=["book:gold"],
            mode="generate",
            threshold="conservative",
            citation_style="mkszyj",
        )
        elapsed = time.perf_counter() - started
        generated = [item for item in candidates if item.get("kind") == "generate"]
        candidate = generated[0] if generated else None
        actual_sources = {
            _source_id(option)
            for option in (candidate or {}).get("source_options") or []
        }
        expected_sources = {str(item) for item in case.get("expected_source_ids") or []}
        expected_positive = bool(expected_sources)
        source_correct = bool(actual_sources & expected_sources) if expected_positive else not actual_sources
        type_correct = str((candidate or {}).get("match_type") or "none") == str(case["expected_match_type"])
        auto_correct = bool((candidate or {}).get("auto_selected")) == bool(case["expected_auto_selected"])
        passed = source_correct and type_correct and auto_correct
        rows.append({
            "id": str(case["id"]),
            "category": str(case["category"]),
            "expected_positive": expected_positive,
            "candidate_found": bool(candidate),
            "source_correct": source_correct,
            "match_type": str((candidate or {}).get("match_type") or "none"),
            "auto_selected": bool((candidate or {}).get("auto_selected")),
            "option_count": len(actual_sources),
            "ambiguous": len(actual_sources) > 1,
            "elapsed_ms": round(elapsed * 1000, 3),
            "passed": passed,
        })
    suite_seconds = time.perf_counter() - suite_started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    positive = [row for row in rows if row["expected_positive"]]
    predicted = [row for row in rows if row["candidate_found"]]
    auto = [row for row in rows if row["auto_selected"]]
    review = [row for row in predicted if not row["auto_selected"]]
    correct_positive = [row for row in positive if row["candidate_found"] and row["source_correct"]]
    correct_predicted = [row for row in predicted if row["expected_positive"] and row["source_correct"]]
    correct_auto = [row for row in auto if row["expected_positive"] and row["source_correct"]]
    accepted_review = [row for row in review if row["expected_positive"] and row["source_correct"]]

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 1.0

    metrics = {
        "recall": ratio(len(correct_positive), len(positive)),
        "precision": ratio(len(correct_predicted), len(predicted)),
        "auto_adoption_accuracy": ratio(len(correct_auto), len(auto)),
        "review_acceptance_rate": ratio(len(accepted_review), len(review)),
        "ambiguity_rate": ratio(sum(bool(row["ambiguous"]) for row in predicted), len(predicted)),
        "no_result_rate": ratio(sum(not row["candidate_found"] for row in rows), len(rows)),
        "average_case_ms": round(sum(float(row["elapsed_ms"]) for row in rows) / max(len(rows), 1), 3),
        "suite_seconds": round(suite_seconds, 3),
        "peak_memory_mb": round(peak_bytes / (1024 * 1024), 3),
    }
    return {
        "ok": all(bool(row["passed"]) for row in rows),
        "gold_path": str(gold_path),
        "case_count": len(rows),
        "metrics": metrics,
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="论文引文助手召回黄金集基准")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    args = parser.parse_args()
    result = run(args.gold)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
