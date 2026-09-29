from __future__ import annotations

import json

from devcontext.context import ContextBuilder
from devcontext.evidence import EvidenceCandidate, EvidencePool, SourcePolicy
from devcontext.models import SearchResult
from devcontext.planning import SubQuestion


def result(identifier: int, source_type: str, path: str, score: float = 1.0) -> SearchResult:
    return SearchResult(
        id=identifier,
        source_type=source_type,
        chunk_type="METHOD" if source_type == "CODE" else "DOCUMENT_SECTION",
        file_path=path,
        content=f"evidence {identifier}",
        start_line=1 if source_type == "CODE" else None,
        end_line=2 if source_type == "CODE" else None,
        class_name="Service" if source_type == "CODE" else None,
        symbol_name="run" if source_type == "CODE" else None,
        signature=None,
        title="说明" if source_type == "DOCUMENT" else None,
        score=score,
    )


def question(
    identifier: str, importance: str, sources: tuple[str, ...]
) -> SubQuestion:
    return SubQuestion(
        identifier,
        f"问题 {identifier}",
        "作用",
        "所需证据",
        sources,
        f"检索 {identifier}",
        importance,
        "CURRENT",
    )


def test_source_policy_marks_code_as_current_implementation() -> None:
    candidate = SourcePolicy().classify(
        result(1, "CODE", "src/Service.java"), "SQ1"
    )

    assert candidate.source_role == "IMPLEMENTATION"
    assert candidate.temporal_status == "CURRENT"
    assert candidate.authority_priority == 100


def test_source_policy_uses_first_matching_document_rule(tmp_path) -> None:
    policy_path = tmp_path / "source-policy.json"
    policy_path.write_text(json.dumps({"rules": [
        {"pattern": "**/*验证报告*.md", "source_role": "VERIFICATION", "temporal_status": "CURRENT", "priority": 95},
        {"pattern": "**/*.md", "source_role": "GENERAL_DOCUMENT", "temporal_status": "UNKNOWN", "priority": 55},
    ]}, ensure_ascii=False), encoding="utf-8")

    candidate = SourcePolicy.from_file(policy_path).classify(
        result(2, "DOCUMENT", "验证报告.md"), "SQ1"
    )

    assert candidate.source_role == "VERIFICATION"
    assert candidate.authority_priority == 95


def test_evidence_pool_reserves_core_mixed_and_supporting_slots() -> None:
    policy = SourcePolicy()
    core = question("SQ1", "CORE", ("CODE", "DOCUMENT"))
    supporting = question("SQ2", "SUPPORTING", ("DOCUMENT",))
    pool = EvidencePool()
    pool.add(policy.classify(result(1, "CODE", "Service.java"), "SQ1"))
    pool.add(policy.classify(result(2, "DOCUMENT", "design.md"), "SQ1"))
    pool.add(policy.classify(result(3, "DOCUMENT", "guide.md"), "SQ2"))

    selected, annotations = pool.select((core, supporting), 3)

    assert {item.id for item in selected} == {1, 2, 3}
    assert annotations[1].sub_question_ids == ("SQ1",)
    assert annotations[3].sub_question_ids == ("SQ2",)


def test_context_header_exposes_authority_only_to_internal_context() -> None:
    policy = SourcePolicy()
    core = question("SQ1", "CORE", ("CODE",))
    pool = EvidencePool()
    pool.add(policy.classify(result(1, "CODE", "Service.java"), "SQ1"))
    selected, annotations = pool.select((core,), 1)

    bundle = ContextBuilder(max_chars=2000).build("问题", selected, annotations)

    assert "role=IMPLEMENTATION" in bundle.rendered_text
    assert bundle.items[0].source_role == "IMPLEMENTATION"
    assert bundle.items[0].sub_question_ids == ["SQ1"]


def test_current_question_prefers_current_evidence_over_historical_plan() -> None:
    current = question("SQ1", "CORE", ("DOCUMENT",))
    pool = EvidencePool()
    pool.add(EvidenceCandidate(
        "SQ1", result(1, "DOCUMENT", "current.md", 0.4),
        "CURRENT_DESIGN", "CURRENT", 70,
    ))
    pool.add(EvidenceCandidate(
        "SQ1", result(2, "DOCUMENT", "history.md", 1.0),
        "HISTORICAL_PLAN", "HISTORICAL", 70,
    ))

    selected, _ = pool.select((current,), 1)

    assert [item.id for item in selected] == [1]
