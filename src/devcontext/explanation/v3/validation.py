from __future__ import annotations

import json
from copy import deepcopy

from devcontext.explanation.models import CLAIM_TYPES
from devcontext.explanation.v3.errors import PlannerFailure, UnsupportedQuestionKind
from devcontext.explanation.v3.models import TeachingBlueprint

COMMON = set("question_form question_kind kind_rationale answer_depth reader_assumption learning_goal goal_capabilities comprehension_checks claims core_mental_model critical_distinctions scenario answer_structure".split())
WHY_SLOTS = set("constraint alternative alternative_limit chosen_design changed_condition causal_explanation tradeoff_boundary".split())
CLAIM_FIELDS = set("id statement claim_type status evidence_labels preconditions reason owner_section".split())
SECTION_FIELDS = set("id title section_goal goal_ids spine_refs learning_delta may_reference checkpoint_ids support_labels target_chars".split())
STEP_FIELDS = set("id path trigger state_before problem action state_after guarantee_claim_ids preconditions remaining_boundary links".split())
CHECKPOINT_FIELDS = set("id spine_refs branch parent_checkpoint_id trigger state_before action state_after claim_ids".split())


def require(condition, message):
    if not condition:
        raise PlannerFailure(message)


def obj(value, fields, name):
    require(isinstance(value, dict) and set(value) == set(fields), f"{name}: invalid fields")
    return value


def text(value, name, *, nullable=False):
    if nullable and value is None:
        return
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 1200, f"{name}: nonempty text required")


def array(value, name):
    require(isinstance(value, list), f"{name}: list required")
    return value


def refs(value, allowed, name, *, nonempty=False):
    array(value, name)
    require(all(isinstance(x, str) for x in value), f"{name}: string references required")
    require(len(value) == len(set(value)) and set(value) <= set(allowed), f"{name}: unknown or duplicate reference")
    require(not nonempty or bool(value), f"{name}: empty references")


def records(value, prefix, fields, name, *, nonempty=True):
    array(value, name)
    require(not nonempty or bool(value), f"{name}: empty records")
    result = {}
    for index, item in enumerate(value, 1):
        obj(item, fields, name)
        identifier = item["id"]
        require(identifier == f"{prefix}{index}", f"{name}: IDs must be consecutive {prefix}1..n")
        result[identifier] = item
    return result


def parse_teaching_plan(response, *, allowed_labels, expected_depth=None):
    try:
        raw = json.loads(response) if isinstance(response, str) else deepcopy(response)
    except (ValueError, TypeError) as exc:
        raise PlannerFailure("Planner output is not JSON") from exc
    require(isinstance(raw, dict), "plan must be an object")
    warnings = []
    if len(raw) == 1 and next(iter(raw)) in {"WHY", "HOW"}:
        wrapper = next(iter(raw))
        require(isinstance(raw[wrapper], dict) and raw[wrapper].get("question_kind") == wrapper, "invalid blueprint wrapper")
        raw = raw[wrapper]
        warnings.append("BLUEPRINT_WRAPPER_REMOVED:" + wrapper)
    kind = raw.get("question_kind")
    require(kind in {"WHY", "HOW", "UNSUPPORTED"}, "invalid question_kind")
    if kind == "UNSUPPORTED":
        obj(raw, {"question_form", "question_kind", "kind_rationale"}, "unsupported")
        require(raw["question_form"] in {"WHAT", "COMPARE", "DEBUG", "LOCATE", "OTHER"}, "invalid unsupported form")
        text(raw["kind_rationale"], "rationale")
        raise UnsupportedQuestionKind(raw["question_form"], raw["kind_rationale"])
    obj(raw, COMMON | ({"why_spine"} if kind == "WHY" else {"how_spine", "how_spine_tail"}), "plan")
    require(raw["question_form"] in {"WHY", "HOW", "WHAT", "COMPARE", "DEBUG", "LOCATE", "OTHER"}, "invalid question_form")
    # Supported form duplicates the selected main task. Derive it, rather than
    # wasting another model call on metadata that cannot change the contract.
    if raw["question_form"] != kind:
        warnings.append(f"QUESTION_FORM_DERIVED:{raw['question_form']}->{kind}")
        raw["question_form"] = kind
    text(raw["kind_rationale"], "rationale")
    text(raw["learning_goal"], "learning_goal")
    require(raw["answer_depth"] in {"detailed", "deep"}, "V3 depth must be detailed/deep")
    require(expected_depth is None or raw["answer_depth"] == expected_depth, "explicit depth mismatch")
    reader = obj(raw["reader_assumption"], {"basis", "profile", "prerequisites"}, "reader")
    require(reader["basis"] in {"DEFAULT", "USER_EXPLICIT"}, "invalid reader basis")
    text(reader["profile"], "reader profile")
    for value in array(reader["prerequisites"], "prerequisites"):
        text(value, "prerequisite")
    goals = records(raw["goal_capabilities"], "G", {"id", "applicable", "capability", "reason"}, "goals")
    require(len(goals) == 5, "five goal dimensions required")
    for goal in goals.values():
        require(type(goal["applicable"]) is bool, "applicable must be boolean")
        text(goal["capability"] if goal["applicable"] else goal["reason"], "goal capability/reason")
        require((goal["reason"] if goal["applicable"] else goal["capability"]) is None, "goal nullable field mismatch")
    claims = records(raw["claims"], "C", CLAIM_FIELDS, "claims")
    for claim in claims.values():
        text(claim["statement"], "claim statement")
        require(claim["claim_type"] in CLAIM_TYPES, "invalid claim type")
        require(claim["status"] in {"CONFIRMED", "INFERENCE", "UNKNOWN"}, "invalid status")
        if claim["claim_type"] == "PROJECT_FACT":
            require(claim["status"] == "CONFIRMED", "project fact must be confirmed")
        if claim["claim_type"] == "PROJECT_INFERENCE":
            require(claim["status"] in {"INFERENCE", "UNKNOWN"}, "inference must be inferred or unknown")
        if claim["status"] == "UNKNOWN":
            text(claim["reason"], "unknown reason")
        else:
            require(claim["reason"] is None, "known claim reason must be null")
        refs(claim["evidence_labels"], allowed_labels, "claim evidence", nonempty=claim["claim_type"] in {"PROJECT_FACT", "PROJECT_INFERENCE"} and claim["status"] != "UNKNOWN")
        for precondition in array(claim["preconditions"], "preconditions"):
            text(precondition, "precondition")
    if kind == "WHY":
        obj(raw["why_spine"], WHY_SLOTS, "why spine")
        for slot, values in raw["why_spine"].items():
            refs(values, claims, slot, nonempty=slot not in {"alternative", "alternative_limit"})
        if not raw["why_spine"]["alternative"]:
            require(not goals["G2"]["applicable"], "missing alternative requires inapplicable G2")
        spine = raw["why_spine"]
    else:
        spine = records(raw["how_spine"], "H", STEP_FIELDS, "how spine")
        require(any(h["path"] == "NORMAL" for h in spine.values()), "normal path required")
        for h in spine.values():
            require(h["path"] in {"NORMAL", "FAILURE"}, "invalid path")
            for field in ("state_before", "problem", "action", "state_after"):
                text(h[field], field)
            text(h["trigger"], "trigger", nullable=h["path"] == "NORMAL")
            require(isinstance(h["remaining_boundary"], str), "boundary must be text")
            refs(h["guarantee_claim_ids"], claims, "step claims", nonempty=True)
            for pre in array(h["preconditions"], "step preconditions"):
                text(pre, "step precondition")
            for link in array(h["links"], "links"):
                obj(link, {"from_step", "relation", "explanation"}, "link")
                require(link["from_step"] in spine and link["from_step"] != h["id"], "invalid link source")
                require(link["relation"] in {"PRECEDES", "DEPENDS_ON", "JOINT_CONSTRAINT", "BOUNDARY_EXTENSION", "BRANCH_FROM"}, "invalid relation")
                text(link["explanation"], "link explanation")
            if h["path"] == "FAILURE":
                require(any(l["relation"] == "BRANCH_FROM" for l in h["links"]), "failure needs branch origin")
        tail = obj(raw["how_spine_tail"], {"established_guarantee_claim_ids", "unknown_boundary_claim_ids", "failure_step_ids"}, "tail")
        refs(tail["established_guarantee_claim_ids"], claims, "final guarantees", nonempty=True)
        require(all(claims[c]["status"] != "UNKNOWN" for c in tail["established_guarantee_claim_ids"]), "unknown cannot be guarantee")
        refs(tail["unknown_boundary_claim_ids"], claims, "unknown boundaries")
        require(all(claims[c]["status"] == "UNKNOWN" for c in tail["unknown_boundary_claim_ids"]), "boundary must be unknown")
        refs(tail["failure_step_ids"], spine, "failure steps")
        require(set(tail["failure_step_ids"]) == {h["id"] for h in spine.values() if h["path"] == "FAILURE"}, "tail failure mismatch")
    models = records(raw["core_mental_model"], "M", {"id", "statement", "relation", "roles", "claim_ids"}, "mental model")
    for model in models.values():
        text(model["statement"], "model statement")
        require(model["relation"] in {"FUNCTION", "DEPENDENCY", "BOUNDARY"}, "invalid model relation")
        refs(model["roles"], {"correctness", "performance", "protection", "boundary"}, "model roles", nonempty=True)
        refs(model["claim_ids"], claims, "model claims", nonempty=True)
    for distinction in array(raw["critical_distinctions"], "distinctions"):
        obj(distinction, {"left", "right", "why_confusable", "claim_ids"}, "distinction")
        for field in ("left", "right", "why_confusable"):
            text(distinction[field], field)
        require(distinction["left"] != distinction["right"], "identical distinction")
        refs(distinction["claim_ids"], claims, "distinction claims", nonempty=True)
    scenario = obj(raw["scenario"], {"kind", "setup", "actors", "assumptions", "checkpoints", "simplification_reason"}, "scenario")
    require(scenario["kind"] in ({"COUNTERFACTUAL", "RELATION_EXAMPLE"} if kind == "WHY" else {"STATE_TRACE"}), "scenario kind mismatch")
    text(scenario["setup"], "setup")
    require(scenario["setup"].startswith("假设"), "scenario setup must be hypothetical")
    for field in ("actors", "assumptions"):
        for value in array(scenario[field], field):
            text(value, field)
    text(scenario["simplification_reason"], "simplification reason", nullable=True)
    if scenario["kind"] == "RELATION_EXAMPLE":
        text(scenario["simplification_reason"], "relation example reason")
    checkpoints = records(scenario["checkpoints"], "K", CHECKPOINT_FIELDS, "checkpoints")
    branches = {"SHARED", "WORLD_A", "WORLD_B"} if scenario["kind"] == "COUNTERFACTUAL" else ({"SHARED"} if kind == "WHY" else {"NORMAL", "FAILURE"})
    seen = set()
    for k in checkpoints.values():
        require(k["branch"] in branches, "invalid checkpoint branch")
        require(k["parent_checkpoint_id"] is None or k["parent_checkpoint_id"] in seen, "checkpoint parent must precede child")
        if k["parent_checkpoint_id"] is not None:
            parent = checkpoints[k["parent_checkpoint_id"]]
            require(not (k["branch"] == "NORMAL" and parent["branch"] == "FAILURE"), "normal path cannot continue failure state")
            require(not ({k["branch"], parent["branch"]} == {"WORLD_A", "WORLD_B"}), "counterfactual worlds cannot continue each other")
        refs(k["spine_refs"], spine, "checkpoint spine", nonempty=True)
        refs(k["claim_ids"], claims, "checkpoint claims", nonempty=True)
        for field in ("trigger", "state_before", "action", "state_after"):
            text(k[field], field)
        seen.add(k["id"])
    if scenario["kind"] == "COUNTERFACTUAL":
        require({"WORLD_A", "WORLD_B"} <= {k["branch"] for k in checkpoints.values()}, "both counterfactual worlds required")
    sections = records(raw["answer_structure"], "S", SECTION_FIELDS, "sections")
    owned = {s: set() for s in sections}
    for c in claims.values():
        require(c["owner_section"] in sections, "invalid claim owner")
        owned[c["owner_section"]].add(c["id"])
    for s in sections.values():
        text(s["title"], "title")
        require("\n" not in s["title"], "title must be one line")
        text(s["section_goal"], "section goal")
        refs(s["goal_ids"], goals, "section goals", nonempty=True)
        refs(s["spine_refs"], spine, "section spine")
        refs(s["may_reference"], claims, "may reference")
        overlap = owned[s["id"]] & set(s["may_reference"])
        if overlap:
            warnings.append(f"OWNED_REFERENCE_DEDUP:{s['id']}:{','.join(sorted(overlap))}")
            s["may_reference"] = [c for c in s["may_reference"] if c not in overlap]
        refs(s["checkpoint_ids"], checkpoints, "section checkpoints")
        refs(s["support_labels"], allowed_labels, "support evidence")
        delta = obj(s["learning_delta"], {"before_kind", "before", "after"}, "delta")
        require(delta["before_kind"] in {"GAP", "POSSIBLE_MISCONCEPTION", "PRIOR_SECTION_LIMIT"}, "invalid before kind")
        text(delta["before"], "before")
        text(delta["after"], "after")
        require(delta["before"] != delta["after"], "delta must change")
        require(type(s["target_chars"]) is int and s["target_chars"] > 0, "positive target_chars required")
    require({g for s in sections.values() for g in s["goal_ids"]} >= {g["id"] for g in goals.values() if g["applicable"]}, "goal coverage incomplete")
    require({r for s in sections.values() for r in s["spine_refs"]} >= {r for r in spine if kind != "WHY" or spine[r]}, "spine coverage incomplete")
    require({k for s in sections.values() for k in s["checkpoint_ids"]} == set(checkpoints), "checkpoint coverage incomplete")
    checks = records(raw["comprehension_checks"], "Q", {"id", "question", "goal_ids", "answer_claim_ids"}, "checks")
    require(len(checks) == 2, "two comprehension checks required")
    for check in checks.values():
        text(check["question"], "check question")
        refs(check["goal_ids"], goals, "check goals", nonempty=True)
        refs(check["answer_claim_ids"], claims, "check claims", nonempty=True)
    if not 3 <= len(models) <= 5:
        warnings.append("MENTAL_MODEL_SIZE")
    if len(claims) > 12 or len(spine) > 8:
        warnings.append("BLUEPRINT_SIZE")
    return TeachingBlueprint(raw, tuple(warnings))
