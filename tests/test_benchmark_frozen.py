from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from devcontext.evaluation.retrieval_workflow_runner import load_workflow_cases

PROJECT_ROOT = Path(__file__).resolve().parents[1]
L1_BENCHMARK = PROJECT_ROOT / "benchmark" / "cases.jsonl"
REGRESSION = PROJECT_ROOT / "benchmark" / "regression" / "regression-v1.jsonl"

# Frozen with benchmark/baselines/retrieval-v1.json. Changing the L1 ground truth
# invalidates every recorded baseline, so this must only ever change deliberately.
L1_SHA256 = "b4275a82b8e4d4c6f01d32453adcf141e2bd9a53947964b978880b227702a23b"
L1_DISTRIBUTION = {"CODE": 12, "DOC": 12, "MIXED": 12}


def _load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_l1_benchmark_hash_is_frozen() -> None:
    assert sha256(L1_BENCHMARK.read_bytes()).hexdigest() == L1_SHA256


def test_l1_benchmark_distribution_is_frozen() -> None:
    cases = _load_jsonl(L1_BENCHMARK)
    assert len(cases) == 36
    distribution = {
        case_type: sum(case["type"] == case_type for case in cases)
        for case_type in L1_DISTRIBUTION
    }
    assert distribution == L1_DISTRIBUTION


def test_regression_corpus_structure() -> None:
    cases = load_workflow_cases(REGRESSION, regression=True)
    assert len(cases) == 5
    assert [case["id"] for case in cases] == [f"REG-{index:03d}" for index in range(1, 6)]
    for case in cases:
        assert {"id", "question", "answerable_today", "focus"} <= set(case)
        assert isinstance(case["question"], str) and case["question"].strip()
        assert isinstance(case["answerable_today"], bool)
        assert isinstance(case["focus"], str) and case["focus"].strip()


def test_regression_corpus_marks_answerable_cases() -> None:
    cases = _load_jsonl(REGRESSION)
    answerable = [case["id"] for case in cases if case["answerable_today"]]
    assert answerable == ["REG-001", "REG-002", "REG-003"]


def test_l15_corpus_reuses_all_l2_case_ids() -> None:
    l15 = load_workflow_cases(PROJECT_ROOT / "benchmark" / "l1.5-retrieval.jsonl")
    l2 = _load_jsonl(PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl")

    # The two suites stay id-aligned, so adding an L2 case requires an L1.5 twin.
    assert len(l15) == 19
    assert [case["id"] for case in l15] == [case["id"] for case in l2]
