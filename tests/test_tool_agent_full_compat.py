from devcontext.explanation.v3.demos import load_demo
from devcontext.explanation.v3.evidence_organizer import resolve_section_dependencies
from devcontext.explanation.v3.universal import parse_answer_blueprint


def test_accepted_optional_distinction_dependencies_are_normalized_for_organizer():
    raw = load_demo("WHAT")["blueprint"]
    raw["critical_distinctions"] = [{"distinction": "Producer versus Consumer",
                                   "clarification": "A distinction without supporting claims"}]
    blueprint = parse_answer_blueprint(raw, allowed_labels={"E1", "E2", "E3"})
    assert blueprint.data["critical_distinctions"][0]["claim_ids"] == []
    # Unsupported distinctions must not become assertions in the writer pack.
    *_, distinctions = resolve_section_dependencies(blueprint.data, blueprint.data["answer_structure"][0], include_map=True)
    assert distinctions == []
