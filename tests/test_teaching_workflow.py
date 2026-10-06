from __future__ import annotations

import pytest

from devcontext.agentic import (
    EvidencePackage,
    RequirementCoverage,
    RetrievalController,
    RetrievalOutcome,
)
from devcontext.agentic.evidence_workflow import EvidenceDrivenWorkflow
from devcontext.agentic.models import StageUsage
from devcontext.context import ContextBuilder, EvidenceWorkspace
from devcontext.context.views import context_item_from_ref
from devcontext.evidence import EvidenceCandidate
from devcontext.explanation.workflow import (
    TeachingAnswerResult,
    TeachingExplanationWorkflow,
)
from devcontext.models import AnswerResult, ContextBundle, SearchResult
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest


REQUIREMENTS = (
    ("ER1", "确认余票桶的数据结构与字段", "找到桶的键与字段定义", "CORE"),
    ("ER2", "确认取令牌与归还令牌的调用点", "找到两个调用点及其顺序", "CORE"),
)


def make_package(question: str = "详细解释项目的余票桶是如何设计的") -> EvidencePackage:
    plan = EvidencePlan(
        question,
        tuple(
            EvidenceRequirement(item[0], item[1], item[2], item[3], "CURRENT", "CODE")
            for item in REQUIREMENTS
        ),
    )
    workspace = EvidenceWorkspace(question)
    for index, requirement_id in enumerate(("ER1", "ER2"), start=1):
        workspace.ingest([EvidenceCandidate(
            requirement_id,
            SearchResult(
                index, "CODE", "METHOD", "Service.java", f"body {index}",
                1, 2, "Service", f"symbol{index}", None, None, 1.0,
            ),
            "IMPLEMENTATION", "CURRENT", 100,
        )])
    catalog = workspace.freeze()
    coverage = tuple(
        RequirementCoverage(
            requirement.id, "SATISFIED",
            tuple(ref.chunk_id for ref in workspace.for_requirement(requirement.id)),
            (), "ok", "rules",
        )
        for requirement in plan.requirements
    )
    # A realistic non-empty retrieval bundle: the teach path must be decided by
    # the workspace, but the older paths still answer from this.
    items = [context_item_from_ref(ref) for ref in workspace.all()]
    rendered, kept, truncated = ContextBuilder(max_chars=6_000).render_items(items)
    bundle = ContextBundle(
        question, kept, rendered, len(rendered), 6_000, truncated
    )
    return EvidencePackage(
        question, plan, bundle,
        coverage, (), "READY", (), (), catalog, workspace,
    )


class FakeController:
    """Stands in for RetrievalController: the package is what matters here."""

    def __init__(self, package: EvidencePackage) -> None:
        self.package = package

    def retrieve(self, request: UserRequest, top_k: int) -> RetrievalOutcome:
        return RetrievalOutcome(self.package, ())


class FakeTeachingWorkflow:
    def __init__(self, package: EvidencePackage) -> None:
        self.calls: list[str] = []
        self._package = package

    def run(self, request: UserRequest, evidence_package: EvidencePackage):
        self.calls.append(request.original_query)
        workspace = evidence_package.evidence_workspace
        refs = workspace.by_citation(["E1"])
        items = [context_item_from_ref(ref) for ref in refs]
        rendered, kept, truncated = ContextBuilder(max_chars=6_000).render_items(items)
        bound = ContextBundle(
            request.original_query, kept, rendered, len(rendered), 6_000, truncated
        )
        return TeachingAnswerResult(
            answer=AnswerResult("教学答案 [E1]", ["E1"]),
            explanation_plan=None,
            context_bundle=bound,
            draft=None,
            stages=(StageUsage("explanation_planning", 1.0, "fallback"),),
            stats={"bound_evidence_count": len(kept)},
        )


def workflow(mode: str, package: EvidencePackage, teaching=None):
    return EvidenceDrivenWorkflow(
        FakeController(package),  # type: ignore[arg-type]
        lambda: None,  # type: ignore[arg-type]
        answer_mode=mode,
        teaching_workflow=teaching,
    )


def test_teach_dispatch_uses_the_teaching_workflow() -> None:
    package = make_package()
    teaching = FakeTeachingWorkflow(package)
    subject = workflow("teach", package, teaching)

    result = subject.run(package.original_query, 12)

    assert teaching.calls == [package.original_query]
    assert result.answer_result.answer == "教学答案 [E1]"
    assert result.trace.explanation_plan is None or isinstance(
        result.trace.explanation_plan, dict
    )
    assert result.workspace_stats == {"bound_evidence_count": 1}


def test_teach_bundle_is_the_bound_evidence_not_the_retrieval_context() -> None:
    package = make_package()
    subject = workflow("teach", package, FakeTeachingWorkflow(package))

    result = subject.run(package.original_query, 12)

    labels = [item.citation.label for item in result.context_bundle.items]
    assert labels == ["E1"], "only the evidence the plan bound reaches the writer"
    # Every cited label must resolve inside that bundle, or the CLI cannot print
    # the Sources block.
    assert set(result.answer_result.used_citations) <= set(labels)
    assert result.evidence_catalog is not None
    assert result.evidence_catalog.label_for(1) == "E1"


def test_teach_path_reports_retrieval_failure_without_planning() -> None:
    package = make_package()
    failed = EvidencePackage(
        package.original_query, package.evidence_plan, package.context_bundle,
        (), ("ER1", "ER2"), "RETRIEVAL_FAILED", (), (), package.evidence_catalog,
        package.evidence_workspace,
    )
    teaching = FakeTeachingWorkflow(failed)

    result = workflow("teach", failed, teaching).run(failed.original_query, 12)

    assert teaching.calls == []
    assert "检索未能完成" in result.answer_result.answer


def test_unknown_answer_mode_does_not_fall_through_to_explain() -> None:
    package = make_package()
    options = AnswerOptions()
    object.__setattr__(options, "answer_mode", "bogus")
    request = UserRequest(package.original_query, options)

    subject = workflow("explain", package)
    with pytest.raises(ValueError, match="unsupported answer_mode"):
        subject._answer(request, package)


def test_answer_options_only_expose_mode() -> None:
    for mode in ("legacy", "explain", "teach"):
        assert AnswerOptions(answer_mode=mode).to_dict() == {"answer_mode": mode}


def test_teach_mode_is_accepted_and_reaches_the_workflow_constructor() -> None:
    package = make_package()
    teaching = FakeTeachingWorkflow(package)

    subject = workflow("teach", package, teaching)

    assert subject.answer_options.answer_mode == "teach"
    assert subject.teaching_workflow is teaching


def test_teach_without_a_workflow_fails_loudly() -> None:
    package = make_package()
    subject = workflow("teach", package, None)

    with pytest.raises(RuntimeError, match="teaching workflow is unavailable"):
        subject.run(package.original_query, 12)
