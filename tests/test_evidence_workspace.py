from __future__ import annotations

import pytest

from devcontext.context import (
    CitationRegistry,
    ContextBuilder,
    EvidenceCatalog,
    EvidenceWorkspace,
)
from devcontext.evidence import EvidenceCandidate
from devcontext.models import SearchResult


def result(
    chunk_id: int,
    *,
    content: str = "body",
    source_type: str = "CODE",
    file_path: str = "Service.java",
) -> SearchResult:
    return SearchResult(
        chunk_id, source_type, "METHOD", file_path, content, 1, 2,
        "Service", "update", None, None, 0.5 * chunk_id,
    )


def candidate(
    chunk_id: int,
    requirement_id: str,
    *,
    content: str = "body",
    source_type: str = "CODE",
) -> EvidenceCandidate:
    return EvidenceCandidate(
        requirement_id, result(chunk_id, content=content, source_type=source_type),
        "IMPLEMENTATION", "CURRENT", 100,
    )


class TestCitationRegistry:
    def test_assigns_labels_in_registration_order(self) -> None:
        registry = CitationRegistry()
        assert registry.register(result(10)) == "E1"
        assert registry.register(result(20)) == "E2"
        assert len(registry) == 2

    def test_same_chunk_keeps_its_label(self) -> None:
        registry = CitationRegistry()
        first = registry.register(result(10))
        registry.register(result(20))
        assert registry.register(result(10)) == first
        assert len(registry) == 2

    def test_freeze_returns_an_immutable_catalog(self) -> None:
        registry = CitationRegistry()
        registry.register(result(10))
        catalog = registry.freeze()

        assert isinstance(catalog, EvidenceCatalog)
        assert catalog.label_for(10) == "E1"
        assert catalog.citation("E1").class_name == "Service"
        assert len(catalog) == 1

    def test_frozen_registry_refuses_new_evidence(self) -> None:
        registry = CitationRegistry()
        registry.register(result(10))
        registry.freeze()

        with pytest.raises(RuntimeError, match="frozen"):
            registry.register(result(20))

    def test_frozen_registry_still_returns_existing_labels(self) -> None:
        registry = CitationRegistry()
        label = registry.register(result(10))
        registry.freeze()
        assert registry.register(result(10)) == label


class TestEvidenceWorkspace:
    def test_keeps_evidence_a_prompt_budget_would_drop(self) -> None:
        """The defect this refactor exists to remove: ContextBuilder stops at the
        first chunk that does not fit and discards everything after it."""
        body = "x" * 3_000
        candidates = tuple(candidate(index, "ER1", content=body) for index in range(1, 21))

        workspace = EvidenceWorkspace("q")
        workspace.ingest(candidates)

        bundle = ContextBuilder(max_chars=28_000).build(
            "q", [item.search_result for item in candidates]
        )

        assert len(workspace) == 20
        assert len(bundle.items) < 20, "precondition: the budget drops the tail"
        assert 20 not in {item.chunk_id for item in bundle.items}
        assert workspace.get(20) is not None
        assert workspace.get(20).content == body

    def test_has_no_char_or_token_ceiling(self) -> None:
        body = "y" * 6_000
        candidates = tuple(candidate(index, "ER1", content=body) for index in range(1, 21))

        workspace = EvidenceWorkspace("q")
        workspace.ingest(candidates)

        stats = workspace.stats()
        assert stats["evidence_count"] == 20
        assert stats["total_content_chars"] == 120_000
        assert len(workspace.all()) == 20

    def test_ids_are_stable_across_the_subsets_asked_for(self) -> None:
        workspace = EvidenceWorkspace("q")
        workspace.ingest([
            candidate(1, "ER1"), candidate(2, "ER2"), candidate(3, "ER1"),
        ])

        by_requirement = {ref.chunk_id: ref.evidence_id for ref in workspace.for_requirement("ER1")}
        by_round = {ref.chunk_id: ref.evidence_id for ref in workspace.for_round(0)}
        by_all = {ref.chunk_id: ref.evidence_id for ref in workspace.all()}

        assert by_requirement == {1: "E1", 3: "E3"}
        assert by_round == by_all == {1: "E1", 2: "E2", 3: "E3"}

    def test_two_rounds_reuse_ids_and_keep_rounds_separate(self) -> None:
        workspace = EvidenceWorkspace("q")
        workspace.ingest([candidate(1, "ER1")], round_index=0)
        workspace.ingest([candidate(2, "ER1"), candidate(1, "ER1")], round_index=1)

        assert [ref.evidence_id for ref in workspace.for_round(0)] == ["E1"]
        # Cumulative: round 1 can see round 0's evidence, plus its own.
        assert [ref.evidence_id for ref in workspace.for_round(1)] == ["E1", "E2"]
        assert workspace.get(1).evidence_id == "E1", "round 2 must not renumber round 1"
        assert workspace.get(1).first_seen_round == 0
        assert len(workspace) == 2

    def test_re_ingest_merges_requirements_instead_of_replacing(self) -> None:
        workspace = EvidenceWorkspace("q")
        workspace.ingest([candidate(1, "ER1")], round_index=0)
        returned = workspace.ingest([candidate(1, "ER2")], round_index=1)

        assert returned == (), "an already-known chunk is not newly added"
        assert workspace.get(1).requirement_ids == ("ER1", "ER2")
        assert [ref.chunk_id for ref in workspace.for_requirement("ER2")] == [1]

    def test_round_snapshot_never_shows_evidence_from_a_later_round(self) -> None:
        workspace = EvidenceWorkspace("q")
        workspace.ingest([candidate(1, "ER1")], round_index=0)
        workspace.ingest([candidate(2, "ER1")], round_index=1)

        assert [ref.chunk_id for ref in workspace.for_round(0)] == [1]
        assert [ref.chunk_id for ref in workspace.for_round(1)] == [1, 2]
        assert workspace.stats()["added_per_round"] == {"0": 1, "1": 1}

    def test_by_citation_resolves_workspace_labels(self) -> None:
        workspace = EvidenceWorkspace("q")
        workspace.ingest([candidate(1, "ER1"), candidate(2, "ER2")])

        assert [ref.chunk_id for ref in workspace.by_citation(["E2", "E9"])] == [2]

    def test_for_requirements_deduplicates_preserving_order(self) -> None:
        workspace = EvidenceWorkspace("q")
        workspace.ingest([candidate(1, "ER1")])
        workspace.ingest([candidate(1, "ER2"), candidate(2, "ER2")], round_index=1)

        assert [ref.chunk_id for ref in workspace.for_requirements(["ER1", "ER2"])] == [1, 2]

    def test_freeze_returns_the_catalog_and_locks_the_workspace(self) -> None:
        workspace = EvidenceWorkspace("q")
        workspace.ingest([candidate(1, "ER1")])
        catalog = workspace.freeze()

        assert catalog.label_for(1) == "E1"
        assert workspace.frozen is True
        with pytest.raises(RuntimeError, match="frozen"):
            workspace.ingest([candidate(2, "ER1")])

    def test_metadata_view_omits_bodies(self) -> None:
        workspace = EvidenceWorkspace("q")
        workspace.ingest([candidate(1, "ER1", content="secret body")])

        entry = workspace.metadata_view()[0]
        assert entry["evidence_id"] == "E1"
        assert entry["first_seen_round"] == 0
        assert "content" not in entry

    def test_empty_workspace_is_usable(self) -> None:
        workspace = EvidenceWorkspace("q")
        assert len(workspace) == 0
        assert workspace.for_requirement("ER1") == ()
        assert workspace.all() == ()
        assert workspace.freeze().label_for(1) is None


class TestEvidencePackageCatalog:
    def test_package_without_a_catalog_still_constructs(self) -> None:
        """Legacy construction sites and fixtures do not pass a catalog."""
        from devcontext.agentic.evidence_models import EvidencePackage
        from devcontext.models import ContextBundle
        from devcontext.planning import EvidencePlan, EvidenceRequirement

        plan = EvidencePlan(
            "q",
            (EvidenceRequirement("ER1", "t", "c", "CORE", "CURRENT", "CODE"),),
        )
        bundle = ContextBundle("q", [], "", 0, 6000, False)
        package = EvidencePackage("q", plan, bundle, (), ("ER1",), "EMPTY", ())

        assert package.evidence_catalog is None
        assert package.to_legacy_sufficiency().enough is False

    def test_catalog_is_not_leaked_into_the_trace_dict(self) -> None:
        """Adding a trace key would break byte-for-byte baseline comparison."""
        from devcontext.agentic.evidence_models import EvidencePackage
        from devcontext.models import ContextBundle
        from devcontext.planning import EvidencePlan, EvidenceRequirement

        plan = EvidencePlan(
            "q",
            (EvidenceRequirement("ER1", "t", "c", "CORE", "CURRENT", "CODE"),),
        )
        bundle = ContextBundle("q", [], "", 0, 6000, False)
        workspace = EvidenceWorkspace("q")
        workspace.ingest([candidate(1, "ER1")])
        package = EvidencePackage(
            "q", plan, bundle, (), ("ER1",), "EMPTY", (), (), workspace.freeze()
        )

        assert package.evidence_catalog is not None
        assert "evidence_catalog" not in package.to_dict()
