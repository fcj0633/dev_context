from __future__ import annotations

import pytest

from devcontext.context import (
    FALLBACK_CAPABILITIES,
    EvidenceWorkspace,
    HeuristicTokenEstimator,
    ModelCapabilities,
    TokenBudgetPolicy,
    build_context_view,
    capabilities_for,
    compare_estimate,
)
from devcontext.evidence import EvidenceCandidate
from devcontext.models import SearchResult


def workspace_with(*entries: tuple[str, str, str]) -> EvidenceWorkspace:
    """entries are (requirement_id, content, symbol)."""
    workspace = EvidenceWorkspace("q")
    for index, (requirement_id, content, symbol) in enumerate(entries, start=1):
        workspace.ingest([
            EvidenceCandidate(
                requirement_id,
                SearchResult(
                    index, "CODE", "METHOD", "Service.java", content,
                    1, 2, "Service", symbol, None, None, 1.0,
                ),
                "IMPLEMENTATION", "CURRENT", 100,
            )
        ])
    return workspace


class TestWorkspaceCoverageView:
    def test_shows_only_the_asked_for_requirement(self) -> None:
        from devcontext.context import WorkspaceCoverageView

        workspace = workspace_with(("ER1", "a", "one"), ("ER2", "b", "two"))

        view = WorkspaceCoverageView(workspace)

        assert [item.chunk_id for item in view.items_for("ER1")] == [1]
        assert [item.chunk_id for item in view.items_for("ER2")] == [2]

    def test_never_shows_evidence_a_later_round_has_not_found(self) -> None:
        from devcontext.context import WorkspaceCoverageView

        workspace = EvidenceWorkspace("q")
        workspace.ingest(
            [EvidenceCandidate(
                "ER1",
                SearchResult(1, "CODE", "METHOD", "S.java", "a", 1, 2, "S", "one", None, None, 1.0),
                "IMPLEMENTATION", "CURRENT", 100,
            )],
            round_index=0,
        )
        workspace.ingest(
            [EvidenceCandidate(
                "ER1",
                SearchResult(2, "CODE", "METHOD", "S.java", "b", 1, 2, "S", "two", None, None, 1.0),
                "IMPLEMENTATION", "CURRENT", 100,
            )],
            round_index=1,
        )

        assert [item.chunk_id for item in WorkspaceCoverageView(workspace, round_index=0).items_for("ER1")] == [1]
        assert [item.chunk_id for item in WorkspaceCoverageView(workspace, round_index=1).items_for("ER1")] == [1, 2]

    def test_items_are_never_marked_truncated(self) -> None:
        from devcontext.context import WorkspaceCoverageView

        workspace = workspace_with(("ER1", "x" * 50_000, "huge"))

        items = WorkspaceCoverageView(workspace).items_for("ER1")

        assert len(items) == 1
        assert items[0].truncated is False
        assert len(items[0].content) == 50_000


class TestBuildContextView:
    def test_section_view_materializes_only_its_own_evidence(self) -> None:
        workspace = workspace_with(
            ("ER1", "alpha", "a"), ("ER2", "beta", "b"), ("ER1", "gamma", "c"),
        )

        view = build_context_view(workspace, ["ER1"])

        assert view.evidence_ids == ("E1", "E3")
        assert "alpha" in view.rendered_text
        assert "beta" not in view.rendered_text

    def test_large_evidence_survives_planning_when_the_section_asks_for_it(self) -> None:
        """The tail is reachable by asking for it, which the old single bundle
        could never offer because the budget had already discarded it."""
        workspace = workspace_with(
            ("ER1", "x" * 20_000, "first"),
            ("ER2", "y" * 20_000, "tail"),
        )

        everything = build_context_view(workspace, ["ER1", "ER2"], max_chars=200_000)
        tail_only = build_context_view(workspace, ["ER2"])

        assert len(everything.items) == 2
        assert "tail" in tail_only.rendered_text
        assert len(tail_only.items) == 1

    def test_reports_what_it_truncated_rather_than_losing_it_silently(self) -> None:
        workspace = workspace_with(
            ("ER1", "x" * 8_000, "first"), ("ER1", "y" * 8_000, "second"),
        )

        view = build_context_view(workspace, ["ER1"], max_chars=9_000)

        assert len(view.items) == 2
        assert view.truncated_evidence_ids == ("E2",)
        assert view.omitted_evidence_ids == ()

    def test_reports_what_it_had_to_drop_entirely(self) -> None:
        workspace = workspace_with(
            ("ER1", "x" * 40, "first"), ("ER1", "y" * 40, "second"),
        )

        # Too small even for one whole item, so the renderer drops the rest.
        view = build_context_view(workspace, ["ER1"], max_chars=30)

        assert view.omitted_evidence_ids == ("E1", "E2")
        assert view.truncated_evidence_ids == ()


class TestTokenBudget:
    def test_budget_comes_from_the_window_not_a_character_ceiling(self) -> None:
        policy = TokenBudgetPolicy(ModelCapabilities(32_768, 8_192))
        estimator = HeuristicTokenEstimator()

        view = build_context_view(
            workspace_with(("ER1", "余" * 500, "a")),
            ["ER1"],
            policy=policy,
            estimator=estimator,
            fixed_tokens=1_000,
            requested_output_tokens=2_000,
        )

        assert view.budget_tokens == 32_768 - 1_000 - 2_000 - 1_024
        assert view.max_chars == view.budget_tokens

    def test_equal_character_counts_are_not_equal_token_counts(self) -> None:
        policy = TokenBudgetPolicy(ModelCapabilities(32_768, 8_192))
        estimator = HeuristicTokenEstimator()

        def tokens_for(content: str) -> int:
            return build_context_view(
                workspace_with(("ER1", content, "a")),
                ["ER1"],
                policy=policy,
                estimator=estimator,
                fixed_tokens=0,
                requested_output_tokens=0,
            ).estimated_tokens

        cjk = tokens_for("余" * 600)
        latin = tokens_for("a" * 600)

        assert cjk > latin * 2, "the same char count must not imply the same cost"

    def test_refuses_when_the_fixed_prompt_alone_overflows(self) -> None:
        policy = TokenBudgetPolicy(ModelCapabilities(8_192, 2_048))

        decision = policy.decide(fixed_tokens=9_000, requested_output_tokens=1_000)

        assert decision.allowed is False
        assert decision.overflow_tokens > 0
        assert decision.context_budget_tokens == 0

    def test_an_impossible_budget_omits_everything_rather_than_overrunning(self) -> None:
        workspace = workspace_with(("ER1", "x" * 100, "a"))

        view = build_context_view(
            workspace,
            ["ER1"],
            policy=TokenBudgetPolicy(ModelCapabilities(2_048, 512)),
            estimator=HeuristicTokenEstimator(),
            fixed_tokens=5_000,
            requested_output_tokens=500,
        )

        assert view.items == ()
        assert view.omitted_evidence_ids == ("E1",)

    def test_output_reserve_is_capped_by_the_model(self) -> None:
        policy = TokenBudgetPolicy(ModelCapabilities(32_768, 4_096))

        decision = policy.decide(fixed_tokens=100, requested_output_tokens=100_000)

        assert decision.output_reserve_tokens == 4_096

    def test_capabilities_precedence(self) -> None:
        reported = ModelCapabilities(1_000_000, 384_000)
        configured = ModelCapabilities(64_000, 8_000)

        assert capabilities_for(reported=reported, configured=configured) is reported
        assert capabilities_for(configured=configured) is configured
        assert capabilities_for() is FALLBACK_CAPABILITIES

    def test_capabilities_reject_an_impossible_window(self) -> None:
        with pytest.raises(ValueError, match="context_window"):
            ModelCapabilities(0, 1)
        with pytest.raises(ValueError, match="max_output_tokens"):
            ModelCapabilities(1_000, 2_000)


class TestTokenEstimator:
    def test_cjk_is_charged_one_token_per_character(self) -> None:
        assert HeuristicTokenEstimator().estimate("余票桶令牌桶") == 6

    def test_latin_is_charged_roughly_four_characters_per_token(self) -> None:
        assert HeuristicTokenEstimator().estimate("abcdefgh") == 2

    def test_empty_text_costs_nothing(self) -> None:
        assert HeuristicTokenEstimator().estimate("") == 0

    def test_tokens_round_trip_into_a_conservative_character_budget(self) -> None:
        estimator = HeuristicTokenEstimator()

        assert estimator.chars_for_tokens(1_000) == 1_000
        with pytest.raises(ValueError, match="positive"):
            estimator.chars_for_tokens(0)


class TestEstimateComparison:
    def test_records_the_drift_between_estimate_and_reported_usage(self) -> None:
        comparison = compare_estimate(estimated=18_231, actual=17_542)

        assert comparison.error_ratio == pytest.approx(0.0393, abs=1e-4)
        assert comparison.to_dict()["estimated_input_tokens"] == 18_231

    def test_over_estimating_shows_up_as_a_positive_ratio(self) -> None:
        assert compare_estimate(120, 100).error_ratio == 0.2
        assert compare_estimate(80, 100).error_ratio == -0.2

    def test_rejects_a_zero_actual_count(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            compare_estimate(10, 0)
