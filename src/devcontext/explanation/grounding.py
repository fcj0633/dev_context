from __future__ import annotations

from dataclasses import dataclass

from devcontext.explanation.models import ExplanationSection


# Markers that make a layer's status visible to the reader. Deliberately crude:
# these are cheap deterministic checks that catch a claim written as a flat
# statement when the plan said it was hedged, not a substitute for review.
_HYPOTHESIS_MARKERS = ("假设", "例如", "可以想象", "试想", "如果")
_HEDGE_MARKERS = (
    "无法确认", "未能确认", "不能确认", "无法核实", "未提供", "不在证据",
    "未验证", "尚不能确定", "无从确认",
)


@dataclass(frozen=True, slots=True)
class GroundingIssue:
    section_id: str
    issue_type: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "section_id": self.section_id,
            "issue_type": self.issue_type,
            "description": self.description,
        }


def grounding_issues(
    section: ExplanationSection,
    text: str,
) -> tuple[GroundingIssue, ...]:
    """Check a written section against what its plan said it may claim.

    The four content layers are only useful if the text actually marks which one
    it is speaking in. A section planned as a labelled hypothesis that reads as a
    flat project statement is the failure this catches.
    """
    issues: list[GroundingIssue] = []

    if any(
        claim.claim_type == "PROJECT_FACT" for claim in section.claim_plans
    ) and not section.evidence_labels:
        issues.append(GroundingIssue(
            section.id, "PROJECT_FACT_WITHOUT_EVIDENCE",
            "计划断言了项目事实，但该章节没有绑定任何证据",
        ))

    # An illustrative example is conditional by construction, so keeping it in
    # this list would report one defect twice under two names.
    conditional = [
        item
        for item in section.claim_plans
        if item.conditional and item.claim_type != "ILLUSTRATIVE_EXAMPLE"
    ]
    if conditional and not _contains_any(text, _HYPOTHESIS_MARKERS):
        issues.append(GroundingIssue(
            section.id, "CONDITIONAL_NOT_MARKED",
            "计划含条件推演，但正文没有写成条件句",
        ))

    if any(
        claim.claim_type == "ILLUSTRATIVE_EXAMPLE"
        for claim in section.claim_plans
    ) and not _contains_any(text, _HYPOTHESIS_MARKERS):
        issues.append(GroundingIssue(
            section.id, "EXAMPLE_NOT_LABELLED",
            "计划含假设案例，但正文没有标明这是假设",
        ))

    if section.evidence_state == "UNVERIFIED" and not _contains_any(
        text, _HEDGE_MARKERS
    ):
        issues.append(GroundingIssue(
            section.id, "UNVERIFIED_WRITTEN_AS_FACT",
            "该章节的证据未经验证，但正文没有说明这一点",
        ))

    return tuple(issues)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)
