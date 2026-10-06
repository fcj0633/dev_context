"""Normalize certain formatting; reject core errors; disable invalid scenarios."""
from copy import deepcopy
import json
import re

from devcontext.answer_policy import INTENTS
from devcontext.explanation.v3.errors import PlannerFailure
from devcontext.explanation.v3.models import TeachingBlueprint


def issue(code, path, expected, actual, identifier=None):
    return dict(code=code, path=path, id=identifier, expected=expected, actual=actual)


def fail(issues):
    if issues:
        raise PlannerFailure("; ".join(f"{i['path']}: {i['expected']}" for i in issues), issues=issues)


def parse_blueprint(response, *, allowed_labels):
    warnings, errors = [], []
    if isinstance(response, str):
        stripped = response.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", stripped, flags=re.I)
        if fenced:
            stripped = fenced[1]
            warnings.append("JSON_FENCE_REMOVED")
        try:
            raw = json.loads(stripped)
        except ValueError as exc:
            raise PlannerFailure("Full blueprint is not JSON", issues=[issue("INVALID_JSON", "$", "valid JSON object", str(exc))]) from exc
    else:
        raw = deepcopy(response)
    if isinstance(raw, dict) and len(raw) == 1 and next(iter(raw)) in INTENTS:
        wrapper = next(iter(raw))
        raw = raw[wrapper]
        if not isinstance(raw, dict) or raw.get("question_kind") != wrapper:
            raise PlannerFailure("wrapped intent conflicts with blueprint")
        warnings.append("BLUEPRINT_WRAPPER_REMOVED:" + wrapper)
    if not isinstance(raw, dict) or raw.get("question_kind") not in INTENTS:
        raise PlannerFailure("Full requires a supported question_kind")
    kind = raw["question_kind"]
    if raw.get("question_form") != kind:
        warnings.append("QUESTION_FORM_DERIVED")
    raw["question_form"] = kind

    def records(items, prefix, path, target=errors):
        if not isinstance(items, list):
            target.append(issue("INVALID_RECORDS", path, "array of records", items))
            return set()
        ids = set()
        for index, record in enumerate(items):
            identifier = record.get("id") if isinstance(record, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(prefix + r"[1-9]\d*", identifier) or identifier in ids:
                target.append(issue("INVALID_ID", f"{path}[{index}].id", "unique " + prefix + " ID", identifier, identifier))
            else:
                ids.add(identifier)
        return ids

    sections = raw.get("answer_structure")
    claims = raw.get("claims")
    section_ids = records(sections, "S", "answer_structure")
    claim_ids = records(claims, "C", "claims")
    goals = raw.setdefault("goal_capabilities", [])
    goal_ids = records(goals, "G", "goal_capabilities")
    units = raw.setdefault("how_spine" if kind == "HOW" else "explanation_units", []) if kind != "WHY" else None
    spine_ids = records(units, "H" if kind == "HOW" else "U", "how_spine" if kind == "HOW" else "explanation_units") if units is not None else set()
    if kind == "WHY":
        spine = raw.setdefault("why_spine", {})
        if not isinstance(spine, dict):
            errors.append(issue("INVALID_TYPE", "why_spine", "object", spine))
        else:
            spine_ids = set(spine)
    if not sections:
        errors.append(issue("EMPTY_SECTIONS", "answer_structure", "nonempty chapters", sections))
    fail(errors)  # Don't attempt reference checks on malformed records.
    for goal in goals:
        if type(goal.get("applicable")) is not bool:
            errors.append(issue("INVALID_TYPE", "goal_capabilities." + goal["id"] + ".applicable",
                                "boolean", goal.get("applicable"), goal["id"]))

    def refs(values, allowed, path, target=errors):
        if not isinstance(values, list) or any(not isinstance(v, str) or v not in allowed for v in values):
            target.append(issue("INVALID_REFERENCE", path, "array referencing existing IDs", values))
            return []
        return list(dict.fromkeys(values))

    def array(record, field, path, objects=False):
        value = record.setdefault(field, [])
        if not isinstance(value, list) or (objects and any(not isinstance(v, dict) for v in value)):
            errors.append(issue("INVALID_TYPE", path + "." + field, "array" + (" of objects" if objects else ""), value))
            return []
        return value

    def labels(values, path):
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            errors.append(issue("INVALID_LABELS", path, "array of strings", values))
            return []
        removed = [v for v in values if v not in allowed_labels]
        if removed:
            warnings.append("UNAVAILABLE_EVIDENCE:" + path + ":" + ",".join(removed))
        return list(dict.fromkeys(v for v in values if v in allowed_labels))

    for c in claims:
        path = "claims." + c["id"]
        if not isinstance(c.get("statement"), str) or not c["statement"].strip() or (not isinstance(c.get("owner_section"), str) or c["owner_section"] not in section_ids):
            errors.append(issue("INVALID_CLAIM", path, "claim needs statement and valid owner", c, c["id"]))
        c["evidence_labels"] = labels(c.get("evidence_labels", []), path + ".evidence_labels")
        c.setdefault("claim_type", "GENERAL_CONCEPT")
        c.setdefault("status", "INFERENCE")
        c.setdefault("reason", None)
        for field in ("claim_type", "status"):
            if not isinstance(c[field], str):
                errors.append(issue("INVALID_TYPE", path + "." + field, "string", c[field], c["id"]))
        array(c, "preconditions", path)
        if c["claim_type"] == "PROJECT_FACT" and (c["status"] != "CONFIRMED" or not c["evidence_labels"]):
            c.update(claim_type="PROJECT_INFERENCE", status="UNKNOWN" if not c["evidence_labels"] else "INFERENCE")
            warnings.append("CLAIM_STRENGTH_NORMALIZED:" + c["id"])
        if c["status"] == "UNKNOWN" and not c["reason"]:
            c["reason"] = "当前材料未确认该具体结论"
    if kind == "WHY":
        for slot, values in raw["why_spine"].items():
            refs(values, claim_ids, "why_spine." + slot)
    else:
        for u in units:
            path = ("how_spine." if kind == "HOW" else "explanation_units.") + u["id"]
            refs(u.get("guarantee_claim_ids" if kind == "HOW" else "claim_ids", []), claim_ids, path + ".claims")
            for index, link in enumerate(array(u, "links", path, objects=True)):
                refs([link.get("from_step" if kind == "HOW" else "from_unit")], spine_ids, f"{path}.links[{index}]")
    if kind == "HOW":
        tail = raw.setdefault("how_spine_tail", {})
        if not isinstance(tail, dict):
            errors.append(issue("INVALID_TYPE", "how_spine_tail", "object", tail))
        else:
            for field, allowed in (("established_guarantee_claim_ids", claim_ids), ("unknown_boundary_claim_ids", claim_ids), ("failure_step_ids", spine_ids)):
                tail[field] = refs(tail.get(field, []), allowed, "how_spine_tail." + field)
            unknown = {c["id"] for c in claims if c["status"] == "UNKNOWN"}
            moved = [c for c in tail["established_guarantee_claim_ids"] if c in unknown]
            if moved:
                tail["established_guarantee_claim_ids"] = [c for c in tail["established_guarantee_claim_ids"] if c not in unknown]
                tail["unknown_boundary_claim_ids"] = list(dict.fromkeys(tail["unknown_boundary_claim_ids"] + moved))
                warnings.append("UNKNOWN_GUARANTEES_MOVED:" + ",".join(moved))
    for s in sections:
        path = "answer_structure." + s["id"]
        for field in ("title", "section_goal"):
            if not isinstance(s.get(field), str) or not s[field].strip() or (field == "title" and "\n" in s[field]):
                errors.append(issue("INVALID_SECTION", path + "." + field, "section requires title and goal; title must be one line", s.get(field), s["id"]))
        for field, allowed in (("goal_ids", goal_ids), ("spine_refs", spine_ids), ("may_reference", claim_ids)):
            s[field] = refs(s.get(field, []), allowed, path + "." + field)
        s["support_labels"] = labels(s.get("support_labels", []), path + ".support_labels")
        s.setdefault("target_chars", 500)
        if not isinstance(s.get("learning_delta"), dict):
            errors.append(issue("INVALID_DELTA", path + ".learning_delta", "learning delta object", s.get("learning_delta")))
    for field in ("core_mental_model", "critical_distinctions", "comprehension_checks"):
        for index, record in enumerate(array(raw, field, "$", objects=True)):
            refs(record.get("answer_claim_ids" if field == "comprehension_checks" else "claim_ids", []), claim_ids, f"{field}[{index}].claims")
            if field == "comprehension_checks":
                refs(record.get("goal_ids", []), goal_ids, f"{field}[{index}].goal_ids")
    fail(errors)
    raw.pop("answer_depth", None)
    raw.setdefault("reader_assumption", {"basis": "DEFAULT", "profile": "Java 基础学习者", "prerequisites": []})
    raw.setdefault("learning_goal", "能解释本题关键关系")
    from devcontext.explanation.v3.universal import validate_teaching_contract
    for section in sections:
        owned = {c["id"] for c in claims if c["owner_section"] == section["id"]}
        if owned & set(section["may_reference"]):
            warnings.append("OWNED_REFERENCE_DEDUP:" + section["id"])
    validate_teaching_contract(raw)
    scenario_errors = validate_scenario(raw, claim_ids, spine_ids, records, refs)
    if scenario_errors:
        raw["scenario"] = dict(kind="NONE", setup="", actors=[], assumptions=[], checkpoints=[], simplification_reason="invalid scenario disabled")
        for s in sections:
            s["checkpoint_ids"] = []
        warnings.append("SCENARIO_DISABLED:" + json.dumps(scenario_errors, ensure_ascii=False))
    return TeachingBlueprint(raw, tuple(warnings))


def validate_scenario(raw, claim_ids, spine_ids, records, refs):
    errors = []
    scenario = raw.setdefault("scenario", dict(kind="NONE", setup="", actors=[], assumptions=[], checkpoints=[], simplification_reason=None))
    if not isinstance(scenario, dict):
        return [issue("INVALID_SCENARIO", "scenario", "object", scenario)]
    checkpoints = scenario.setdefault("checkpoints", [])
    ids = records(checkpoints, "K", "scenario.checkpoints", errors)
    if errors:
        return errors
    kind = scenario.get("kind")
    allowed = {"NONE": set(), "COUNTERFACTUAL": {"SHARED", "WORLD_A", "WORLD_B"}, "STATE_TRACE": {"NORMAL", "FAILURE"}, "RELATION_EXAMPLE": {"SHARED"}}
    if not isinstance(kind, str) or kind not in allowed:
        errors.append(issue("INVALID_SCENARIO", "scenario.kind", "known scenario kind", kind))
        return errors
    if (kind == "NONE" and checkpoints) or (kind != "NONE" and (not checkpoints or not isinstance(scenario.get("setup"), str) or not scenario["setup"].strip())):
        errors.append(issue("INVALID_SCENARIO", "scenario", "NONE without checkpoints or nonempty scenario with setup", kind))
    by_id = {k["id"]: k for k in checkpoints}
    positions = {k["id"]: i for i, k in enumerate(checkpoints)}
    for k in checkpoints:
        path = "scenario.checkpoints." + k["id"]
        for field, permitted in (("claim_ids", claim_ids), ("spine_refs", spine_ids)):
            values = refs(k.get(field, []), permitted, path + "." + field, errors)
            if not values:
                errors.append(issue("EMPTY_CHECKPOINT_REFS", path + "." + field, "nonempty valid references", k.get(field), k["id"]))
        for field in ("trigger", "state_before", "action", "state_after"):
            if not isinstance(k.get(field), str) or not k[field].strip():
                errors.append(issue("INVALID_CHECKPOINT", path + "." + field, "nonempty text", k.get(field), k["id"]))
        if not isinstance(k.get("branch"), str) or k["branch"] not in allowed[kind]:
            errors.append(issue("SCENARIO_BRANCH", path + ".branch", "scenario branch mismatch", k.get("branch"), k["id"]))
        parent_id = k.get("parent_checkpoint_id")
        parent = by_id.get(parent_id) if isinstance(parent_id, str) else None
        if parent_id is not None and (parent is None or positions[parent_id] >= positions[k["id"]]):
            errors.append(issue("CHECKPOINT_PARENT", path + ".parent_checkpoint_id", "checkpoint parent must precede child", parent_id, k["id"]))
        if k.get("branch") == "FAILURE" and parent is None:
            errors.append(issue("FAILURE_PARENT", path + ".parent_checkpoint_id", "failure checkpoint needs an established parent state", parent_id, k["id"]))
        if parent and isinstance(k.get("branch"), str) and isinstance(parent.get("branch"), str) and ((k.get("branch") == "NORMAL" and parent.get("branch") == "FAILURE") or {k.get("branch"), parent.get("branch")} == {"WORLD_A", "WORLD_B"}):
            errors.append(issue("BRANCH_CONTINUATION", path, "compatible parent branch", parent.get("branch"), k["id"]))
    covered = set()
    for s in raw["answer_structure"]:
        s["checkpoint_ids"] = refs(s.get("checkpoint_ids", []), ids, "answer_structure." + s["id"] + ".checkpoint_ids", errors)
        covered.update(s["checkpoint_ids"])
    if covered != ids:
        errors.append(issue("CHECKPOINT_COVERAGE", "answer_structure", "checkpoint coverage incomplete", sorted(ids - covered)))
    branches = {k.get("branch") for k in checkpoints if isinstance(k.get("branch"), str)}
    if kind == "STATE_TRACE" and "NORMAL" not in branches or kind == "COUNTERFACTUAL" and not {"WORLD_A", "WORLD_B"} <= branches:
        errors.append(issue("SCENARIO_PATHS", "scenario", "required scenario paths", sorted(str(b) for b in branches)))
    return errors
