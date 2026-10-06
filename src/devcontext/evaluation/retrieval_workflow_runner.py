from __future__ import annotations

import copy
import json
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from statistics import mean
from typing import Any

from devcontext.agentic import (
    RequirementCoverage,
    RetrievalController,
    RetrievalObserver,
    SearchAction,
)
from devcontext.context.views import CoverageView
from devcontext.models import ContextBundle, ContextItem, SearchExecution, SearchResult
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest
from devcontext.evaluation.runner import _matches_group


WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_STATES = {"READY", "PARTIAL", "EMPTY"}
PRIORITIES = {"CORE", "SUPPORTING"}
TEMPORAL_SCOPES = {"CURRENT", "HISTORY", "FUTURE", "ANY"}
SOURCE_REQUIREMENTS = {"CODE", "DOCUMENT", "BOTH", "ANY"}
TARGET_FIELDS = {
    "source_type", "path_contains", "class_name", "symbol", "signature",
    "start_line", "annotation_contains", "title_contains", "heading_path",
}
CASE_FIELDS = {"id", "question", "tags", "expected_retrieval_state", "requirements"}
REGRESSION_FIELDS = CASE_FIELDS | {"answerable_today", "focus"}
REQUIREMENT_FIELDS = {
    "id", "target", "success_criteria", "priority", "temporal_scope",
    "source_requirement", "expected_satisfied", "relevant", "queries",
}


@dataclass(slots=True)
class RetrievalTraceRecorder(RetrievalObserver):
    action_candidates: list[dict[str, Any]]
    round_contexts: dict[int, ContextBundle]

    def __init__(self) -> None:
        self.action_candidates = []
        self.round_contexts = {}

    def on_action_completed(
        self, action: SearchAction, execution: SearchExecution
    ) -> None:
        self.action_candidates.append(
            {
                "action": action.to_dict(),
                "results": copy.deepcopy(execution.results),
                "timings": execution.timings.to_dict(),
                # Strategy trace: which retrieval actually ran for this action and
                # which real code symbols (if any) selected it. Without these the
                # report can only see that a round scored worse, not why.
                "strategy": execution.strategy,
                "symbols": list(execution.symbols),
            }
        )

    def on_context_built(self, round_index: int, context: ContextBundle) -> None:
        self.round_contexts[round_index] = copy.deepcopy(context)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_candidates": [
                {
                    **{key: value for key, value in item.items() if key != "results"},
                    "results": [result.to_dict() for result in item["results"]],
                }
                for item in self.action_candidates
            ],
            "round_contexts": {
                str(index): _context_digest(context)
                for index, context in sorted(self.round_contexts.items())
            },
        }


class FrozenEvidencePlanner:
    last_client = None

    def __init__(self, case: Mapping[str, Any]) -> None:
        self.case = case

    def plan(self, query: str) -> EvidencePlan:
        return EvidencePlan(
            query,
            tuple(_requirement_model(item) for item in self.case["requirements"]),
            "fallback",
        )


class FrozenSearchActionPlanner:
    last_client = None

    def __init__(self, case: Mapping[str, Any]) -> None:
        self.case = case
        self._by_id = {item["id"]: item for item in case["requirements"]}

    def plan_actions(
        self,
        original_query: str,
        requirements: Sequence[EvidenceRequirement],
        *,
        round_index: int,
        **_: Any,
    ) -> tuple[SearchAction, ...]:
        key = f"round_{round_index}"
        actions = []
        for offset, requirement in enumerate(requirements, start=1):
            query = self._by_id[requirement.id]["queries"].get(key)
            if not query:
                continue
            actions.append(
                SearchAction(
                    f"SA{round_index + 1}-{offset}", requirement.id,
                    round_index, query, requirement.source_requirement,
                    "frozen benchmark query", "fallback",
                )
            )
        return tuple(actions)


class OracleCoverageChecker:
    last_client = None

    def __init__(self, case: Mapping[str, Any]) -> None:
        self._specs = {item["id"]: item for item in case["requirements"]}

    def check(
        self,
        requirements: Sequence[EvidenceRequirement],
        view: CoverageView,
    ) -> tuple[RequirementCoverage, ...]:
        statuses = []
        for requirement in requirements:
            spec = self._specs[requirement.id]
            owned = [
                item for item in view.items_for(requirement.id)
                if _temporal_eligible(item, spec["temporal_scope"])
            ]
            matched = _matched_group_indexes(owned, spec["relevant"])
            if spec["expected_satisfied"] and len(matched) == len(spec["relevant"]):
                state = "SATISFIED"
                missing: tuple[str, ...] = ()
                reason = "冻结 Ground Truth 的全部证据组已进入最终 Context"
            elif matched or owned:
                state = "PARTIAL"
                missing = (spec["success_criteria"],)
                reason = "只找到冻结 Ground Truth 的部分证据"
            else:
                state = "MISSING"
                missing = (spec["success_criteria"],)
                reason = "没有找到冻结 Ground Truth 的直接证据"
            statuses.append(
                RequirementCoverage(
                    requirement.id, state,
                    tuple(item.chunk_id for item in owned), missing, reason, "rules",
                )
            )
        return tuple(statuses)


def load_workflow_cases(path: Path, *, regression: bool = False) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exception:
                raise ValueError(
                    f"Invalid retrieval workflow JSON at line {line_number}"
                ) from exception
    return validate_workflow_cases(cases, regression=regression)


def validate_workflow_cases(
    cases: Any, *, regression: bool = False
) -> list[dict[str, Any]]:
    if not isinstance(cases, list) or not cases:
        raise ValueError("Retrieval workflow benchmark must contain cases")
    expected_fields = REGRESSION_FIELDS if regression else CASE_FIELDS
    seen: set[str] = set()
    for case_index, case in enumerate(cases):
        context = f"case[{case_index}]"
        if not isinstance(case, dict) or set(case) != expected_fields:
            raise ValueError(f"{context} has invalid fields")
        case_id = _text(case.get("id"), f"{context}.id")
        if case_id in seen:
            raise ValueError(f"Duplicate retrieval workflow id: {case_id}")
        seen.add(case_id)
        _text(case.get("question"), f"{case_id}.question")
        tags = case.get("tags")
        if not isinstance(tags, list) or not tags or not all(
            isinstance(item, str) and item.strip() for item in tags
        ):
            raise ValueError(f"{case_id}.tags must be a non-empty string list")
        state = case.get("expected_retrieval_state")
        if state not in WORKFLOW_STATES:
            raise ValueError(f"{case_id}.expected_retrieval_state is invalid")
        if regression:
            if not isinstance(case.get("answerable_today"), bool):
                raise ValueError(f"{case_id}.answerable_today must be boolean")
            _text(case.get("focus"), f"{case_id}.focus")
        requirements = case.get("requirements")
        if not isinstance(requirements, list) or not requirements:
            raise ValueError(f"{case_id}.requirements must be non-empty")
        requirement_ids: set[str] = set()
        for requirement_index, requirement in enumerate(requirements):
            _validate_requirement(
                requirement,
                f"{case_id}.requirements[{requirement_index}]",
                requirement_ids,
            )
        if not any(item["priority"] == "CORE" for item in requirements):
            raise ValueError(f"{case_id} requires at least one CORE requirement")
        derived = _derived_expected_state(requirements)
        if state != derived:
            raise ValueError(
                f"{case_id}.expected_retrieval_state must be {derived}, got {state}"
            )
    return cases


def _validate_requirement(
    requirement: Any, context: str, seen: set[str]
) -> None:
    if not isinstance(requirement, dict) or set(requirement) != REQUIREMENT_FIELDS:
        raise ValueError(f"{context} has invalid fields")
    requirement_id = _text(requirement.get("id"), f"{context}.id")
    if requirement_id in seen:
        raise ValueError(f"Duplicate requirement id: {requirement_id}")
    seen.add(requirement_id)
    _text(requirement.get("target"), f"{context}.target")
    _text(requirement.get("success_criteria"), f"{context}.success_criteria")
    if requirement.get("priority") not in PRIORITIES:
        raise ValueError(f"{context}.priority is invalid")
    if requirement.get("temporal_scope") not in TEMPORAL_SCOPES:
        raise ValueError(f"{context}.temporal_scope is invalid")
    if requirement.get("source_requirement") not in SOURCE_REQUIREMENTS:
        raise ValueError(f"{context}.source_requirement is invalid")
    if not isinstance(requirement.get("expected_satisfied"), bool):
        raise ValueError(f"{context}.expected_satisfied must be boolean")
    relevant = requirement.get("relevant")
    if not isinstance(relevant, list):
        raise ValueError(f"{context}.relevant must be a list")
    if requirement["expected_satisfied"] and not relevant:
        raise ValueError(f"{context}.relevant cannot be empty when satisfiable")
    for index, group in enumerate(relevant):
        _validate_workflow_group(group, f"{context}.relevant[{index}]")
    sources = {_workflow_group_source(group) for group in relevant}
    required_source = requirement["source_requirement"]
    if relevant and (
        (required_source == "CODE" and sources != {"CODE"})
        or (required_source == "DOCUMENT" and sources != {"DOCUMENT"})
        or (required_source == "BOTH" and sources != {"CODE", "DOCUMENT"})
    ):
        raise ValueError(f"{context}.relevant does not match source_requirement")
    queries = requirement.get("queries")
    expected_query_fields = (
        {"round_0", "round_1"}
        if requirement["priority"] == "CORE" else {"round_0"}
    )
    if not isinstance(queries, dict) or set(queries) != expected_query_fields:
        raise ValueError(f"{context}.queries has invalid fields")
    for key, value in queries.items():
        _text(value, f"{context}.queries.{key}")


def _validate_workflow_group(group: Any, context: str) -> None:
    if not isinstance(group, dict) or not group:
        raise ValueError(f"{context} must be a non-empty object")
    targets = group.get("any_of") if "any_of" in group else [group]
    if "any_of" in group and (
        set(group) != {"any_of"} or not isinstance(targets, list) or not targets
    ):
        raise ValueError(f"{context}.any_of is invalid")
    sources: set[str] = set()
    for index, target in enumerate(targets):
        target_context = f"{context}.any_of[{index}]"
        if not isinstance(target, dict) or not target:
            raise ValueError(f"{target_context} must be an object")
        if set(target) - TARGET_FIELDS:
            raise ValueError(f"{target_context} has unsupported fields")
        source = target.get("source_type")
        if source not in {"CODE", "DOCUMENT"}:
            raise ValueError(f"{target_context}.source_type is invalid")
        if not isinstance(target.get("path_contains"), str) or not target["path_contains"].strip():
            raise ValueError(f"{target_context}.path_contains is required")
        if source == "CODE" and not any(
            target.get(field) not in (None, "")
            for field in ("class_name", "symbol", "signature", "start_line")
        ):
            raise ValueError(f"{target_context} requires a code identity")
        if source == "DOCUMENT" and not target.get("heading_path"):
            raise ValueError(f"{target_context}.heading_path is required")
        if "heading_path" in target and (
            not isinstance(target["heading_path"], list)
            or not target["heading_path"]
            or not all(
                isinstance(item, str) and item.strip()
                for item in target["heading_path"]
            )
        ):
            raise ValueError(f"{target_context}.heading_path is invalid")
        if "start_line" in target and (
            not isinstance(target["start_line"], int) or target["start_line"] < 1
        ):
            raise ValueError(f"{target_context}.start_line is invalid")
        sources.add(source)
    if len(sources) != 1:
        raise ValueError(f"{context}.any_of alternatives must share source_type")


def _workflow_group_source(group: Mapping[str, Any]) -> str:
    targets = group["any_of"] if "any_of" in group else [group]
    return targets[0]["source_type"]


def run_retrieval_workflow_evaluation(
    *,
    case_sets: Mapping[str, tuple[Path, bool]],
    modes: Sequence[str],
    runs: int,
    controller_factory: Callable[
        [dict[str, Any], str, RetrievalTraceRecorder], RetrievalController
    ],
    top_k: int = 12,
    only: Sequence[str] | None = None,
    limit: int | None = None,
    source_policy_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not modes or any(mode not in {"frozen", "live"} for mode in modes):
        raise ValueError("modes must contain frozen and/or live")
    if not 1 <= runs <= 5:
        raise ValueError("runs must be between 1 and 5")
    if tuple(modes) == ("frozen",) and runs != 1:
        raise ValueError("frozen mode only supports runs=1")
    selected_ids = set(only or ())
    loaded: list[tuple[str, dict[str, Any]]] = []
    dataset_bytes = bytearray()
    dataset_paths: dict[str, str] = {}
    for suite, (path, regression) in case_sets.items():
        dataset_paths[suite] = str(path)
        raw = path.read_bytes()
        dataset_bytes.extend(suite.encode("utf-8") + b"\0" + raw)
        cases = load_workflow_cases(path, regression=regression)
        loaded.extend((suite, case) for case in cases)
    dataset_case_count = len(loaded)
    if selected_ids:
        loaded = [item for item in loaded if item[1]["id"] in selected_ids]
        missing = selected_ids - {item[1]["id"] for item in loaded}
        if missing:
            raise ValueError(f"Unknown case ids: {sorted(missing)}")
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        loaded = loaded[:limit]
    if not loaded:
        raise ValueError("No retrieval workflow cases selected")

    records: list[dict[str, Any]] = []
    for mode in modes:
        mode_runs = runs if mode == "live" else 1
        for run_index in range(mode_runs):
            for suite, case in loaded:
                if progress:
                    progress(
                        f"[{mode} {run_index + 1}/{mode_runs}] "
                        f"{suite}/{case['id']}"
                    )
                observer = RetrievalTraceRecorder()
                try:
                    controller = controller_factory(case, mode, observer)
                    # The mode is stated rather than defaulted: it decides
                    # AnswerOptions.evidence_source, which decides whether an
                    # empty retrieval bundle counts as "nothing found". Leaving
                    # it implicit would let a future default change silently
                    # rewrite what this suite measures.
                    outcome = controller.retrieve(
                        UserRequest(case["question"], AnswerOptions(None, "legacy")),
                        top_k,
                    )
                    records.append(
                        score_workflow_case(
                            case, suite, mode, run_index + 1,
                            outcome.package, observer,
                            [item.to_dict() for item in outcome.stage_usage],
                        )
                    )
                except Exception as exception:
                    records.append(
                        _error_record(
                            case, suite, mode, run_index + 1,
                            type(exception).__name__, observer,
                        )
                    )

    summaries = {
        mode: _summarize([item for item in records if item["mode"] == mode])
        for mode in modes
    }
    execution_errors = sum(item["error"] is not None for item in records)
    false_ready_count = sum(bool(item.get("false_ready")) for item in records)
    acceptance = [
        {
            "name": "execution_error_count",
            "passed": execution_errors == 0,
            "current": execution_errors,
            "target": 0,
        },
        {
            "name": "false_ready_count",
            "passed": false_ready_count == 0,
            "current": false_ready_count,
            "target": 0,
        },
    ]
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "benchmark_level": "L1.5",
        "suite": "+".join(case_sets),
        "mode": "+".join(modes),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "dataset_sha256": sha256(dataset_bytes).hexdigest(),
        "datasets": dataset_paths,
        "source_policy_sha256": (
            sha256(source_policy_path.read_bytes()).hexdigest()
            if source_policy_path and source_policy_path.is_file() else None
        ),
        "run_config": {
            "top_k": top_k, "runs": runs, "modes": list(modes),
            "only": list(only or ()), "limit": limit,
            "dataset_case_count": dataset_case_count,
            "selected_case_count": len(loaded),
        },
        "summary": summaries,
        "acceptance": acceptance,
        "quality_passed": all(item["passed"] for item in acceptance),
        "records": records,
    }


def score_workflow_case(
    case: Mapping[str, Any],
    suite: str,
    mode: str,
    run_index: int,
    package: Any,
    observer: RetrievalTraceRecorder,
    stage_usage: list[dict[str, Any]],
) -> dict[str, Any]:
    final_context = package.context_bundle
    oracle = _oracle_coverage(case, final_context)
    satisfiable = [item for item in oracle if item["expected_satisfied"]]
    full_case = all(item["satisfied"] for item in satisfiable)
    core = [
        item for item in oracle
        if item["priority"] == "CORE" and item["expected_satisfied"]
    ]
    core_satisfied = sum(item["satisfied"] for item in core)
    all_gold_core = all(
        item["satisfied"] for item in oracle if item["priority"] == "CORE"
    )
    # A READY claim is a claim about what retrieval found, so it is checked
    # against what retrieval found. Whether the answer's context went on to
    # present that evidence is a different question and is reported separately -
    # conflating the two made a correct READY look like an overclaim.
    core_ids = {
        item.id
        for item in package.evidence_plan.requirements
        if item.priority == "CORE"
    }
    all_retrieved_core = all(
        item.satisfied
        for item in package.requirement_coverage
        if item.requirement_id in core_ids
    )
    false_ready = package.retrieval_state == "READY" and (
        not all_retrieved_core or case["expected_retrieval_state"] != "READY"
    )
    ready_with_dropped_evidence = (
        package.retrieval_state == "READY" and not all_gold_core
    )
    survival = _context_survival(case, observer, final_context)
    rescue = _second_round_rescue(case, package, observer, final_context)
    _annotate_action_gold(observer.action_candidates, case)
    trace = observer.to_dict()
    return {
        "case_id": case["id"], "suite": suite, "mode": mode,
        "run_index": run_index, "question": case["question"],
        "tags": list(case["tags"]),
        "expected_retrieval_state": case["expected_retrieval_state"],
        "actual_retrieval_state": package.retrieval_state,
        "state_matches_expected": (
            package.retrieval_state == case["expected_retrieval_state"]
        ),
        "evidence_plan": package.evidence_plan.to_dict(),
        "search_actions": [item.to_dict() for item in package.search_history],
        "action_candidates": trace["action_candidates"],
        "round_contexts": trace["round_contexts"],
        "final_context": _context_digest(final_context),
        "actual_requirement_coverage": [
            item.to_dict() for item in package.requirement_coverage
        ],
        "oracle_requirement_coverage": oracle,
        "full_case_success": full_case,
        "core_requirement_total": len(core),
        "core_requirement_satisfied": core_satisfied,
        "core_requirement_coverage": (
            core_satisfied / len(core) if core else None
        ),
        "all_core_satisfied": (
            all(item["satisfied"] for item in core) if core else None
        ),
        "false_ready": false_ready,
        "ready_with_dropped_evidence": ready_with_dropped_evidence,
        "context_survival": survival,
        "second_round_rescue": rescue,
        "round_zero": _round_zero_metrics(case, observer),
        "round_one_new_gold_group_count": sum(
            len(item["round_1_new_groups"]) for item in rescue["details"]
        ),
        "stage_usage": stage_usage,
        "error": None,
    }


def _oracle_coverage(
    case: Mapping[str, Any], context: ContextBundle
) -> list[dict[str, Any]]:
    values = []
    for spec in case["requirements"]:
        eligible = [
            item for item in context.items
            if not item.truncated and _temporal_eligible(item, spec["temporal_scope"])
        ]
        matched = _matched_group_indexes(eligible, spec["relevant"])
        satisfied = bool(spec["expected_satisfied"]) and (
            len(matched) == len(spec["relevant"])
        )
        values.append(
            {
                "requirement_id": spec["id"],
                "priority": spec["priority"],
                "expected_satisfied": spec["expected_satisfied"],
                "satisfied": satisfied,
                "matched_group_indexes": matched,
                "required_group_count": len(spec["relevant"]),
            }
        )
    return values


def _context_survival(
    case: Mapping[str, Any],
    observer: RetrievalTraceRecorder,
    final_context: ContextBundle,
) -> dict[str, Any]:
    candidates = [
        result
        for action in observer.action_candidates
        for result in action["results"]
    ]
    candidate_groups: set[tuple[str, int]] = set()
    survived_groups: set[tuple[str, int]] = set()
    for spec in case["requirements"]:
        for index, group in enumerate(spec["relevant"]):
            key = (spec["id"], index)
            if any(_matches_group(result, group) for result in candidates):
                candidate_groups.add(key)
            if any(
                not item.truncated
                and _temporal_eligible(item, spec["temporal_scope"])
                and _matches_group(_context_result(item), group)
                for item in final_context.items
            ):
                survived_groups.add(key)
    survived = candidate_groups & survived_groups
    dropped = sorted(candidate_groups - survived_groups)
    return {
        "candidate_found_group_count": len(candidate_groups),
        "survived_group_count": len(survived),
        "candidate_found_but_dropped_count": len(dropped),
        "context_survival_rate": (
            len(survived) / len(candidate_groups) if candidate_groups else None
        ),
        "dropped_group_details": [
            {"requirement_id": requirement_id, "group_index": index}
            for requirement_id, index in dropped
        ],
    }


def _second_round_rescue(
    case: Mapping[str, Any],
    package: Any,
    observer: RetrievalTraceRecorder,
    final_context: ContextBundle,
) -> dict[str, Any]:
    round_zero = observer.round_contexts.get(0, ContextBundle("", [], "", 0, 1, False))
    second_actions = [item for item in package.search_history if item.round_index == 1]
    details = []
    unanswerable_attempt_count = 0
    for spec in case["requirements"]:
        if spec["priority"] != "CORE":
            continue
        source = spec["source_requirement"]
        compatible = [
            item for item in second_actions
            if item.requirement_id == spec["id"]
            and _scope_compatible(item.source_scope, source)
        ]
        if not compatible:
            continue
        if not spec["expected_satisfied"]:
            unanswerable_attempt_count += 1
            continue
        before = _gold_requirement_satisfied(spec, round_zero)
        if before:
            continue
        after = _gold_requirement_satisfied(spec, final_context)
        before_groups = _matched_group_indexes(round_zero.items, spec["relevant"])
        after_groups = _matched_group_indexes(final_context.items, spec["relevant"])
        details.append(
            {
                "requirement_id": spec["id"],
                "round_0_queries": [
                    item.query for item in package.search_history
                    if item.round_index == 0
                    and item.requirement_id == spec["id"]
                ],
                "round_1_queries": [item.query for item in compatible],
                "round_0_matched_groups": before_groups,
                "round_1_new_groups": sorted(set(after_groups) - set(before_groups)),
                "rescued": after,
            }
        )
    eligible = len(details)
    rescued = sum(item["rescued"] for item in details)
    no_gain = sum(
        not item["rescued"] and not item["round_1_new_groups"]
        for item in details
    )
    return {
        "eligible_count": eligible,
        "rescued_count": rescued,
        "rescue_rate": rescued / eligible if eligible else None,
        "no_gain_count": no_gain,
        "partial_gain_count": eligible - rescued - no_gain,
        "unanswerable_second_round_attempt_count": unanswerable_attempt_count,
        "details": details,
    }


def _annotate_action_gold(
    entries: Sequence[dict[str, Any]], case: Mapping[str, Any]
) -> None:
    """Attach per-action gold matching, in place, to the recorded candidates.

    ``gold_rank`` is the 1-based position of the first retrieved result matching
    any gold group of that requirement, or None when none matched. Temporal
    scope is deliberately NOT applied: the recorded candidates are raw
    ``SearchResult`` rows, which carry no temporal status, so filtering them
    would silently match nothing. The oracle's coverage numbers are the
    temporal-aware ones; this rank is a ranking-quality signal only.
    """
    specs = {spec["id"]: spec for spec in case["requirements"]}
    for entry in entries:
        spec = specs.get(entry["action"]["requirement_id"])
        if spec is None or not spec["relevant"]:
            entry["gold_hit"] = False
            entry["gold_rank"] = None
            continue
        rank = None
        for index, result in enumerate(entry["results"], start=1):
            if any(_matches_group(result, group) for group in spec["relevant"]):
                rank = index
                break
        entry["gold_hit"] = rank is not None
        entry["gold_rank"] = rank


def _strategy_counts(entries: Sequence[dict[str, Any]]) -> dict[str, int]:
    """How many actions ran under each source scope / strategy pair.

    The pair matters rather than the strategy alone: ``CODE:vector`` versus
    ``CODE:keyword`` is the comparison this experiment exists to make, and a
    bare strategy tally would let a DOCUMENT action hide a CODE one.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        scope = entry["action"].get("source_scope") or "?"
        strategy = entry.get("strategy") or "unknown"
        key = f"{scope}:{strategy}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _merge_counts(values: Iterable[Mapping[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for value in values:
        for key, count in value.items():
            merged[key] = merged.get(key, 0) + count
    return merged


def _round_zero_metrics(
    case: Mapping[str, Any], observer: RetrievalTraceRecorder
) -> dict[str, Any] | None:
    """What round 0 alone achieved, scored on round 0's own context.

    The final numbers cannot say whether a case was answered by the first
    semantic sweep or rescued by the second exact one - and that distinction is
    the whole question this experiment asks - so round 0 is scored separately.
    """
    round_zero = observer.round_contexts.get(0)
    if round_zero is None:
        return None
    core = [spec for spec in case["requirements"] if spec["priority"] == "CORE"]
    satisfiable = [spec for spec in case["requirements"] if spec["expected_satisfied"]]
    core_satisfied = sum(
        _gold_requirement_satisfied(spec, round_zero) for spec in core
    )
    group_total = sum(len(spec["relevant"]) for spec in case["requirements"])
    group_hit = sum(
        len(_matched_group_indexes(round_zero.items, spec["relevant"]))
        for spec in case["requirements"]
    )
    round_zero_actions = [
        entry
        for entry in observer.action_candidates
        if entry["action"]["round_index"] == 0
    ]
    return {
        "core_requirement_total": len(core),
        "core_requirement_satisfied": core_satisfied,
        "core_satisfaction_rate": _rate(core_satisfied, len(core)),
        "gold_group_total": group_total,
        "gold_group_hit": group_hit,
        "gold_group_recall": _rate(group_hit, group_total),
        "full_case_success": all(
            _gold_requirement_satisfied(spec, round_zero) for spec in satisfiable
        ),
        "action_count": len(round_zero_actions),
        "strategy_counts": _strategy_counts(round_zero_actions),
    }


def _summarize(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    valid = [item for item in records if item["error"] is None]
    predicted_ready = [item for item in valid if item["actual_retrieval_state"] == "READY"]
    false_ready = [item for item in valid if item["false_ready"]]
    eligible = sum(item["second_round_rescue"]["eligible_count"] for item in valid)
    rescued = sum(item["second_round_rescue"]["rescued_count"] for item in valid)
    candidate_groups = sum(
        item["context_survival"]["candidate_found_group_count"] for item in valid
    )
    survived_groups = sum(
        item["context_survival"]["survived_group_count"] for item in valid
    )
    core_total = sum(item["core_requirement_total"] for item in valid)
    core_satisfied = sum(item["core_requirement_satisfied"] for item in valid)
    round_zero = [
        item["round_zero"] for item in valid if item.get("round_zero") is not None
    ]
    r0_core_total = sum(item["core_requirement_total"] for item in round_zero)
    r0_core_satisfied = sum(
        item["core_requirement_satisfied"] for item in round_zero
    )
    r0_group_total = sum(item["gold_group_total"] for item in round_zero)
    r0_group_hit = sum(item["gold_group_hit"] for item in round_zero)
    r0_full_success = sum(item["full_case_success"] for item in round_zero)
    core_cases = [item for item in valid if item["core_requirement_total"] > 0]
    ready_gold = [
        item for item in valid if item["expected_retrieval_state"] == "READY"
    ]
    summary = {
        "case_run_count": len(records),
        "successful_run_count": len(valid),
        "execution_error_count": len(records) - len(valid),
        "full_case_success_count": sum(item["full_case_success"] for item in valid),
        "full_case_success_rate": _rate(
            sum(item["full_case_success"] for item in valid), len(valid)
        ),
        "core_requirement_total": core_total,
        "core_requirement_satisfied": core_satisfied,
        "core_requirement_coverage": _rate(core_satisfied, core_total),
        "all_core_satisfied_case_count": sum(
            bool(item["all_core_satisfied"]) for item in core_cases
        ),
        "all_core_satisfied_case_rate": _rate(
            sum(bool(item["all_core_satisfied"]) for item in core_cases),
            len(core_cases),
        ),
        "predicted_ready_count": len(predicted_ready),
        "false_ready_count": len(false_ready),
        "false_ready_rate": _rate(len(false_ready), len(predicted_ready)),
        "false_ready_case_ids": [item["case_id"] for item in false_ready],
        # Reported, not gated in this phase: retrieval found the evidence but the
        # answer's single context bundle did not carry it. Closing this gap is
        # what the section-scoped views exist for.
        "ready_with_dropped_evidence_count": sum(
            bool(item["ready_with_dropped_evidence"]) for item in valid
        ),
        "ready_with_dropped_evidence_case_ids": [
            item["case_id"] for item in valid if item["ready_with_dropped_evidence"]
        ],
        "ready_recall": _rate(
            sum(item["actual_retrieval_state"] == "READY" for item in ready_gold),
            len(ready_gold),
        ),
        "expected_state_accuracy": _rate(
            sum(item["state_matches_expected"] for item in valid), len(valid)
        ),
        "candidate_found_group_count": candidate_groups,
        "survived_group_count": survived_groups,
        "context_survival_rate": _rate(survived_groups, candidate_groups),
        "second_round_eligible_count": eligible,
        "second_round_rescued_count": rescued,
        "second_round_rescue_rate": _rate(rescued, eligible),
        "second_round_no_gain_count": sum(
            item["second_round_rescue"]["no_gain_count"] for item in valid
        ),
        "second_round_partial_gain_count": sum(
            item["second_round_rescue"]["partial_gain_count"] for item in valid
        ),
        "unanswerable_second_round_attempt_count": sum(
            item["second_round_rescue"]["unanswerable_second_round_attempt_count"]
            for item in valid
        ),
        "average_search_action_count": (
            mean(len(item["search_actions"]) for item in valid) if valid else None
        ),
        # Round-level reporting. The final numbers average the two rounds
        # together and so cannot answer this experiment's question - whether the
        # first semantic sweep was enough, or the second exact one did the work.
        "round0_core_requirement_total": r0_core_total,
        "round0_core_requirement_satisfied": r0_core_satisfied,
        "round0_core_satisfaction_rate": _rate(r0_core_satisfied, r0_core_total),
        "round0_gold_group_total": r0_group_total,
        "round0_gold_group_hit": r0_group_hit,
        "round0_gold_group_recall": _rate(r0_group_hit, r0_group_total),
        "round0_full_case_success_count": r0_full_success,
        "round0_full_case_success_rate": _rate(r0_full_success, len(round_zero)),
        "round0_strategy_counts": _merge_counts(
            item["strategy_counts"] for item in round_zero
        ),
        "round1_new_gold_group_count": sum(
            item["round_one_new_gold_group_count"] for item in valid
        ),
    }
    summary["by_tag"] = {
        tag: _tag_summary(valid, tag)
        for tag in ("flow", "why", "edge", "negative", "locate")
        if any(tag in item["tags"] for item in valid)
    }
    summary["by_expected_state"] = {
        state: _state_summary(valid, state)
        for state in ("READY", "PARTIAL", "EMPTY")
        if any(item["expected_retrieval_state"] == state for item in valid)
    }
    return summary


def compare_workflow_baseline(
    report: Mapping[str, Any], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    if baseline.get("dataset_sha256") != report.get("dataset_sha256"):
        return {
            "status": "incompatible",
            "reason": "dataset_sha256 differs",
            "baseline_sha256": baseline.get("dataset_sha256"),
            "current_sha256": report.get("dataset_sha256"),
        }
    if baseline.get("source_policy_sha256") != report.get("source_policy_sha256"):
        return {
            "status": "incompatible",
            "reason": "source_policy_sha256 differs",
            "baseline_sha256": baseline.get("source_policy_sha256"),
            "current_sha256": report.get("source_policy_sha256"),
        }
    baseline_numbers = _flatten_numbers(baseline.get("summary", {}))
    current_numbers = _flatten_numbers(report.get("summary", {}))
    return {
        "status": "comparable",
        "deltas": {
            key: {
                "baseline": baseline_numbers[key],
                "current": current_numbers[key],
                "delta": current_numbers[key] - baseline_numbers[key],
            }
            for key in sorted(baseline_numbers.keys() & current_numbers.keys())
        },
    }


def build_workflow_baseline(report: Mapping[str, Any]) -> dict[str, Any]:
    if report.get("mode") != "frozen" or report.get("suite") != "l1.5+regression":
        raise ValueError("workflow baseline requires a full frozen all-suite run")
    run_config = report.get("run_config", {})
    if (
        run_config.get("only")
        or run_config.get("limit") is not None
        or run_config.get("selected_case_count") != run_config.get("dataset_case_count")
    ):
        raise ValueError("workflow baseline requires every dataset case")
    if any(not item["passed"] for item in report["acceptance"]):
        raise ValueError("workflow baseline requires a passing run")
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "name": "retrieval-workflow-v1",
        "generated_at": report["generated_at"],
        "git_commit": report["git_commit"],
        "dataset_sha256": report["dataset_sha256"],
        "source_policy_sha256": report["source_policy_sha256"],
        "summary": report["summary"],
    }


def _requirement_model(spec: Mapping[str, Any]) -> EvidenceRequirement:
    return EvidenceRequirement(
        spec["id"], spec["target"], spec["success_criteria"],
        spec["priority"], spec["temporal_scope"], spec["source_requirement"],
    )


def _derived_expected_state(requirements: Sequence[Mapping[str, Any]]) -> str:
    core = [item for item in requirements if item["priority"] == "CORE"]
    if all(item["expected_satisfied"] for item in core):
        return "READY"
    if any(item["expected_satisfied"] for item in requirements):
        return "PARTIAL"
    return "EMPTY"


def _matched_group_indexes(
    items: Sequence[ContextItem], relevant: Sequence[dict[str, Any]]
) -> list[int]:
    return [
        index for index, group in enumerate(relevant)
        if any(
            not item.truncated and _matches_group(_context_result(item), group)
            for item in items
        )
    ]


def _gold_requirement_satisfied(
    spec: Mapping[str, Any], context: ContextBundle
) -> bool:
    if not spec["expected_satisfied"]:
        return False
    eligible = [
        item for item in context.items
        if not item.truncated and _temporal_eligible(item, spec["temporal_scope"])
    ]
    return len(_matched_group_indexes(eligible, spec["relevant"])) == len(spec["relevant"])


def _temporal_eligible(item: ContextItem, temporal_scope: str) -> bool:
    if temporal_scope == "ANY":
        return True
    if temporal_scope == "CURRENT":
        return item.temporal_status not in {"HISTORICAL", "FUTURE"}
    expected = {"HISTORY": "HISTORICAL", "FUTURE": "FUTURE"}[temporal_scope]
    return item.temporal_status == expected


def _context_result(item: ContextItem) -> SearchResult:
    citation = item.citation
    return SearchResult(
        item.chunk_id, citation.source_type, item.chunk_type, citation.file_path,
        item.content, citation.start_line, citation.end_line, citation.class_name,
        citation.symbol_name, citation.signature,
        citation.heading_path[-1] if citation.heading_path else None, item.score,
        [], list(citation.heading_path),
    )


def _context_digest(context: ContextBundle) -> dict[str, Any]:
    return {
        "total_chars": context.total_chars,
        "max_chars": context.max_chars,
        "truncated": context.truncated,
        "items": [item.to_dict() for item in context.items],
    }


def _scope_compatible(actual: str, required: str) -> bool:
    if actual in {"ANY", "BOTH"} or required in {"ANY", "BOTH"}:
        return True
    return actual == required


def _tag_summary(records: Sequence[dict[str, Any]], tag: str) -> dict[str, Any]:
    selected = [item for item in records if tag in item["tags"]]
    return {
        "case_run_count": len(selected),
        "full_case_success_rate": _rate(
            sum(item["full_case_success"] for item in selected), len(selected)
        ),
    }


def _state_summary(records: Sequence[dict[str, Any]], state: str) -> dict[str, Any]:
    selected = [
        item for item in records if item["expected_retrieval_state"] == state
    ]
    return {
        "case_run_count": len(selected),
        "state_accuracy": _rate(
            sum(item["state_matches_expected"] for item in selected), len(selected)
        ),
        "full_case_success_rate": _rate(
            sum(item["full_case_success"] for item in selected), len(selected)
        ),
    }


def _error_record(
    case: Mapping[str, Any], suite: str, mode: str, run_index: int,
    error: str, observer: RetrievalTraceRecorder,
) -> dict[str, Any]:
    trace = observer.to_dict()
    return {
        "case_id": case["id"], "suite": suite, "mode": mode,
        "run_index": run_index, "question": case["question"],
        "tags": list(case["tags"]),
        "expected_retrieval_state": case["expected_retrieval_state"],
        "actual_retrieval_state": None, "state_matches_expected": False,
        "evidence_plan": None, "search_actions": [],
        "action_candidates": trace["action_candidates"],
        "round_contexts": trace["round_contexts"], "final_context": None,
        "actual_requirement_coverage": [], "oracle_requirement_coverage": [],
        "full_case_success": False, "core_requirement_total": 0,
        "core_requirement_satisfied": 0, "core_requirement_coverage": None,
        "all_core_satisfied": False, "false_ready": False,
        "ready_with_dropped_evidence": False,
        "context_survival": None, "second_round_rescue": None,
        "stage_usage": [], "error": error,
    }


def _rate(numerator: int | float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _flatten_numbers(value: Any, prefix: str = "") -> dict[str, float]:
    values: dict[str, float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            values.update(_flatten_numbers(child, child_prefix))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        values[prefix] = float(value)
    return values


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be non-empty text")
    return value.strip()


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
