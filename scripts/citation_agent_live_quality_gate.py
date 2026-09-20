from __future__ import annotations

"""Run a privacy-safe live API/corpus quality gate for the admin citation Agent.

The cases are checked-in synthetic sentences and canonical public-domain quotations.
No uploaded paper, user identity, task database row, or raw model answer is read or
stored.  The script uses the same redacted queue and local evidence executor as the
administrator test lane.
"""

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import citation_agent_bridge as bridge
import citation_agent_test_runtime as runtime
from build_index import normalize


DIRECT_CASES = [
    "社会生活在本质上是实践的",
    "哲学家们只是用不同的方式解释世界而问题在于改变世界",
    "资产阶级的灭亡和无产阶级的胜利是同样不可避免的",
    "劳动首先是人和自然之间的过程",
    "一步实际运动比一打纲领更重要",
    "全世界无产者联合起来",
    "自由人的联合体",
]
DIRECT_NEGATIVE = "量子芯片通过低温封装提高短视频推荐系统的点击率"

PARAPHRASE_CASES = [
    {
        "id": "practice",
        "paper": "人的社会生活归根到底具有实践性质，理论谜团也需要在人类实践中解决。",
        "required": ["社会生活在本质上是实践的"],
    },
    {
        "id": "change_world",
        "paper": "哲学的任务不能停留在解释世界，而应当着力改变世界。",
        "required": ["解释世界", "改变世界"],
    },
    {
        "id": "movement_forms",
        "paper": "不同国家的工人运动发展程度并不一致，实际运动会呈现不同的理论形式。",
        "required": ["实际运动", "理论形式"],
    },
    {
        "id": "labour_nature",
        "paper": "劳动过程体现人通过自身活动调节并控制同自然之间物质变换的关系。",
        "required": ["劳动过程", "自然", "物质变换"],
    },
]
PARAPHRASE_NEGATIVE = "数字平台依靠用户画像优化短视频推荐并提高广告点击率。"


def _records(texts: list[str], *, quoted: bool) -> list[dict]:
    return [
        {
            "paragraph_index": index,
            "section_id": "body",
            "raw_start": 0,
            "raw_end": len(text),
            "citation_start": 0,
            "citation_end": len(text),
            "raw_text": text,
            "quoted": quoted,
        }
        for index, text in enumerate(texts)
    ]


def _candidate_by_text(candidates: list[dict]) -> dict[str, dict]:
    return {
        normalize(str(candidate.get("paper_text") or "")): candidate
        for candidate in candidates
        if candidate.get("review_bucket") == "actionable"
    }


def _source_signature(option: dict) -> tuple[str, tuple[str, ...]]:
    return (
        str(option.get("source_file") or ""),
        tuple(str(value) for value in option.get("printed_pages") or []),
    )


def _direct_gate(corpus) -> dict:
    expected: dict[str, set[tuple[str, tuple[str, ...]]]] = {}
    eligible: list[str] = []
    for text in DIRECT_CASES:
        hits = corpus.locate_quote(
            text, per_book_exact=8, allow_fuzzy=False, book_scope={"文集": None},
        )
        signatures = {
            _source_signature(hit.to_dict())
            for hit in hits if str(hit.match_type) == "exact"
        }
        if signatures:
            eligible.append(text)
            expected[normalize(text)] = signatures
    if len(eligible) < 5:
        raise RuntimeError(f"direct gate has only {len(eligible)} corpus-backed positive cases")
    texts = [*eligible, DIRECT_NEGATIVE]
    candidates, report = bridge.run_agent(
        job={"id": "live-direct-gate", "recognition_depth": "direct_only", "scope": ["book:文集"]},
        extracted={}, records=_records(texts, quoted=True), deterministic_candidates=[],
        resolved_style="mkszyj", corpus=corpus,
    )
    by_text = _candidate_by_text(candidates)
    rows = []
    correct = 0
    false_positive = 0
    auto_wrong = 0
    for text in eligible:
        candidate = by_text.get(normalize(text))
        actual = {
            _source_signature(option) for option in (candidate or {}).get("source_options") or []
        }
        source_correct = bool(actual & expected[normalize(text)])
        correct += int(source_correct)
        auto_wrong += int(bool((candidate or {}).get("auto_selected")) and not source_correct)
        rows.append({
            "text": text, "found": bool(candidate), "source_correct": source_correct,
            "evidence_level": str((candidate or {}).get("evidence_level") or ""),
            "auto_selected": bool((candidate or {}).get("auto_selected")),
        })
    negative_candidate = by_text.get(normalize(DIRECT_NEGATIVE))
    false_positive += int(bool(negative_candidate))
    predicted = sum(bool(row["found"]) for row in rows) + false_positive
    precision = correct / predicted if predicted else 1.0
    recall = correct / len(eligible)
    return {
        "positive_cases": len(eligible), "negative_cases": 1,
        "correct": correct, "false_positives": false_positive,
        "precision": round(precision, 4), "recall": round(recall, 4),
        "auto_wrong": auto_wrong, "report": report, "cases": rows,
    }


def _paraphrase_gate(corpus) -> dict:
    texts = [*[str(case["paper"]) for case in PARAPHRASE_CASES], PARAPHRASE_NEGATIVE]
    action_trace: list[dict] = []
    original_execute = bridge._execute_action

    def traced_execute(action, record, *args, **kwargs):
        observation, candidate = original_execute(action, record, *args, **kwargs)
        action_trace.append({
            "paper": str(record.get("_raw") or record.get("raw_text") or ""),
            "tool": str(action.get("tool") or ""),
            "fragment": str(action.get("fragment") or ""),
            "keywords": list(action.get("keywords") or []),
            "result": str(observation.get("result") or ""),
            "hit_count": int(observation.get("hit_count") or 0),
        })
        return observation, candidate

    bridge._execute_action = traced_execute
    try:
        candidates, report = bridge.run_agent(
            job={
                "id": "live-paraphrase-gate", "recognition_depth": "direct_and_paraphrase",
                "scope": ["book:文集"],
            },
            extracted={}, records=_records(texts, quoted=False), deterministic_candidates=[],
            resolved_style="mkszyj", corpus=corpus,
        )
    finally:
        bridge._execute_action = original_execute
    by_text = _candidate_by_text(candidates)
    rows = []
    relevant = 0
    suggestions = 0
    for case in PARAPHRASE_CASES:
        candidate = by_text.get(normalize(str(case["paper"])))
        option = ((candidate or {}).get("source_options") or [{}])[0]
        context = normalize(str(option.get("context") or ""))
        source_relevant = bool(candidate) and all(
            normalize(str(term)) in context for term in case["required"]
        )
        suggestions += int(bool(candidate))
        relevant += int(source_relevant)
        rows.append({
            "id": case["id"], "suggested": bool(candidate), "source_relevant": source_relevant,
            "evidence_level": str((candidate or {}).get("evidence_level") or ""),
            "auto_selected": bool((candidate or {}).get("auto_selected")),
            "source_file": str(option.get("source_file") or ""),
            "printed_pages": list(option.get("printed_pages") or []),
            "section_title": str(option.get("section_title") or ""),
        })
    negative_candidate = by_text.get(normalize(PARAPHRASE_NEGATIVE))
    negative_false_positive = int(bool(negative_candidate))
    suggestions += negative_false_positive
    precision_proxy = relevant / suggestions if suggestions else None
    recall_proxy = relevant / len(PARAPHRASE_CASES)
    return {
        "positive_cases": len(PARAPHRASE_CASES), "negative_cases": 1,
        "relevant_suggestions": relevant, "suggestions": suggestions,
        "negative_false_positives": negative_false_positive,
        "precision_proxy": round(precision_proxy, 4) if precision_proxy is not None else None,
        "recall_proxy": round(recall_proxy, 4),
        "any_auto_selected": any(bool(row["auto_selected"]) for row in rows),
        "report": report, "cases": rows, "action_trace": action_trace,
    }


def run(mode: str = "all") -> dict:
    corpus = runtime.load_corpus()
    # The live gate must never mutate a website task record.  Only the redacted
    # disposable Agent queue is exercised; run_agent deletes each queue item.
    bridge.tasks.update_job = lambda *_args, **_kwargs: None
    result: dict = {"pass": {}}
    if mode in {"all", "direct"}:
        direct = _direct_gate(corpus)
        result["direct"] = direct
        result["pass"].update({
            "direct_precision_95": direct["precision"] >= 0.95,
            "direct_recall_increment": direct["recall"] > 0,
            "auto_adoption_100": direct["auto_wrong"] == 0,
            "direct_agent_complete": direct["report"].get("status") == "complete",
        })
    if mode in {"all", "paraphrase"}:
        paraphrase = _paraphrase_gate(corpus)
        result["paraphrase"] = paraphrase
        result["pass"].update({
            "paraphrase_precision_proxy_80": (
                paraphrase["suggestions"] > 0
                and float(paraphrase["precision_proxy"] or 0) >= 0.80
            ),
            "paraphrase_recall_increment": paraphrase["recall_proxy"] > 0,
            "paraphrase_never_auto": not paraphrase["any_auto_selected"],
            "paraphrase_agent_complete": paraphrase["report"].get("status") == "complete",
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="管理员论文插校 Agent 真实 API 质量门")
    parser.add_argument("--mode", choices=("all", "direct", "paraphrase"), default="all")
    args = parser.parse_args()
    result = run(str(args.mode))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(result["pass"].values()) else 1)


if __name__ == "__main__":
    main()
