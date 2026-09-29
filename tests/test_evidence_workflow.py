from __future__ import annotations

from devcontext.agentic import (
    EvidenceDrivenWorkflow,
    EvidencePackage,
    RequirementCoverage,
    RetrievalOutcome,
)
from devcontext.models import ContextBundle
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import UserRequest


def empty_package() -> EvidencePackage:
    plan = EvidencePlan(
        "当前注册入口在哪里？",
        (EvidenceRequirement(
            "ER1",
            "确认当前注册入口",
            "找到入口类和方法",
            "CORE",
            "CURRENT",
            "CODE",
        ),),
    )
    context = ContextBundle(
        "当前注册入口在哪里？", [], "", 0, 8_000, False
    )
    coverage = (
        RequirementCoverage(
            "ER1",
            "MISSING",
            (),
            ("缺少直接相关的 CODE 证据",),
            "没有证据进入最终 Context",
            "rules",
        ),
    )
    return EvidencePackage(
        plan.original_query,
        plan,
        context,
        coverage,
        ("ER1",),
        "EMPTY",
        (),
    )


class FakeController:
    def retrieve(self, request: UserRequest, top_k: int) -> RetrievalOutcome:
        assert request.original_query == "当前注册入口在哪里？"
        assert top_k == 12
        return RetrievalOutcome(empty_package(), ())


def test_empty_evidence_package_does_not_call_generator_or_expose_internal_ids() -> None:
    def forbidden_generator():
        raise AssertionError("answer generator must not run for EMPTY evidence")

    result = EvidenceDrivenWorkflow(
        FakeController(),  # type: ignore[arg-type]
        forbidden_generator,
        answer_mode="explain",
    ).run("当前注册入口在哪里？", 12)

    assert "ER1" not in result.answer_result.answer
    assert "确认当前注册入口" in result.answer_result.answer
    assert result.trace.evidence_package_state == "EMPTY"
    assert result.trace.evidence_plan is not None
    # Deprecated trace fields are generated only as a serialization alias.
    assert result.trace.plan["intent_summary"] == "当前注册入口在哪里？"
    assert result.trace.sub_question_traces[0].sub_question_id == "ER1"
