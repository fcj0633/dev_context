from __future__ import annotations

import json

from devcontext.agentic import CoverageChecker
from devcontext.context import ContextBuilder
from devcontext.evidence import EvidenceAnnotation
from devcontext.models import SearchResult
from devcontext.planning import EvidenceRequirement


def result(identifier: int, source: str = "CODE") -> SearchResult:
    return SearchResult(
        id=identifier,
        source_type=source,
        chunk_type="METHOD" if source == "CODE" else "DOCUMENT_SECTION",
        file_path="Service.java" if source == "CODE" else "design.md",
        content="status is updated only when current status is available",
        start_line=1 if source == "CODE" else None,
        end_line=2 if source == "CODE" else None,
        class_name="Service" if source == "CODE" else None,
        symbol_name="update" if source == "CODE" else None,
        signature=None,
        title=None,
        score=1.0,
    )


def requirement(source: str = "CODE") -> EvidenceRequirement:
    return EvidenceRequirement(
        "ER1", "确认最终状态更新条件", "找到实际更新条件和影响行数检查",
        "CORE", "CURRENT", source,
    )


class _View:
    """Minimal CoverageView: the checker only ever needs items_for()."""

    def __init__(self, items) -> None:
        self._items = list(items)

    def items_for(self, requirement_id: str):
        return [item for item in self._items if requirement_id in item.sub_question_ids]


def bundle(*results: SearchResult) -> _View:
    annotations = {
        item.id: EvidenceAnnotation(
            "IMPLEMENTATION" if item.source_type == "CODE" else "CURRENT_DESIGN",
            "CURRENT", 100, ("ER1",),
        )
        for item in results
    }
    built = ContextBuilder(max_chars=5000).build("问题", list(results), annotations)
    return _View(built.items)


class FakeClient:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls = 0

    def generate(self, messages) -> str:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_coverage_checker_distinguishes_related_from_satisfied() -> None:
    response = json.dumps({
        "statuses": [{
            "requirement_id": "ER1",
            "state": "PARTIAL",
            "evidence_ids": [1],
            "missing_criteria": ["尚缺影响行数检查"],
            "reason": "已有更新条件但缺少结果校验",
        }]
    }, ensure_ascii=False)
    status = CoverageChecker(lambda: FakeClient(response)).check(
        [requirement()], bundle(result(1))
    )[0]

    assert status.state == "PARTIAL"
    assert status.evidence_ids == (1,)
    assert status.missing_criteria == ("尚缺影响行数检查",)


def test_coverage_checker_applies_source_gate_before_llm() -> None:
    client = FakeClient(RuntimeError("must not run"))
    status = CoverageChecker(lambda: client).check(
        [requirement("BOTH")], bundle(result(1, "CODE"))
    )[0]

    assert status.state == "PARTIAL"
    assert "DOCUMENT" in status.missing_criteria[0]
    assert client.calls == 0


def test_coverage_checker_reports_unverified_after_two_failures() -> None:
    clients: list[FakeClient] = []

    def factory() -> FakeClient:
        client = FakeClient(RuntimeError("network secret"))
        clients.append(client)
        return client

    status = CoverageChecker(factory).check(
        [requirement()], bundle(result(1))
    )[0]

    assert status.state == "UNVERIFIED"
    # The diagnostic channel keeps the class and message; the human-facing reason
    # stays generic. Recording only the class name made a transport failure
    # indistinguishable from a genuine parse rejection.
    assert status.check_error.startswith("RuntimeError")
    assert "network secret" in status.check_error
    assert len(clients) == 2
    assert "secret" not in status.reason


def test_coverage_checker_records_a_parse_rejection_distinctly() -> None:
    """A batch the parser rejects must be diagnosable as a parse rejection."""
    checker = CoverageChecker(lambda: FakeClient("not json at all"))

    status = checker.check([requirement()], bundle(result(1)))[0]

    assert status.state == "UNVERIFIED"
    assert status.check_error.startswith("CoverageCheckError")
    assert "valid JSON" in status.check_error


def test_satisfied_coverage_must_reference_direct_evidence() -> None:
    response = json.dumps({
        "statuses": [{
            "requirement_id": "ER1",
            "state": "SATISFIED",
            "evidence_ids": [],
            "missing_criteria": [],
            "reason": "已满足",
        }]
    }, ensure_ascii=False)
    checker = CoverageChecker(lambda: FakeClient(response))

    status = checker.check([requirement()], bundle(result(1)))[0]

    assert status.state == "UNVERIFIED"
    assert status.check_error.startswith("CoverageCheckError")
    assert "evidence" in status.check_error
