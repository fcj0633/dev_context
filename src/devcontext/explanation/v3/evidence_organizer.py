from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from types import SimpleNamespace

from devcontext.context.builder import ContextBuilder
from devcontext.context.views import context_item_from_ref
from devcontext.models import Citation, ContextBundle, ContextItem
from devcontext.explanation.v3.errors import EvidencePackUnavailable
from devcontext.explanation.v3.models import WriterEvidencePack, WriterSection


def build_writer_evidence_pack(blueprint, package, *, max_chars=28000, required_token_budget=None, estimator=None):
    data = blueprint.data
    claims = {c["id"]: c for c in data["claims"]}
    checkpoints = {k["id"]: k for k in data["scenario"]["checkpoints"]}
    if package.evidence_workspace is not None:
        items = {r.evidence_id: context_item_from_ref(r) for r in package.evidence_workspace.all()}
    else:
        items = {i.citation.label: i for i in package.context_bundle.items}
    sections = []
    required_order = []
    optional_order = []
    established_claims = set()
    for index, section in enumerate(data["answer_structure"]):
        own = [c["id"] for c in claims.values() if c["owner_section"] == section["id"]]
        referenced = set(own) | set(section["may_reference"])
        # Natural transitions may cite conclusions already explained. Keep the
        # evidence for this shared knowledge available without changing owners.
        prior_context = [c for c in claims if c in established_claims]
        referenced.update(prior_context)
        if blueprint.question_kind == "WHY":
            steps = {slot: data["why_spine"][slot] for slot in section["spine_refs"]}
            referenced.update(c for values in steps.values() for c in values)
        else:
            steps = [deepcopy(h) for h in data["how_spine"] if h["id"] in section["spine_refs"]]
            referenced.update(c for h in steps for c in h["guarantee_claim_ids"])
        local_checkpoints = [deepcopy(checkpoints[k]) for k in section["checkpoint_ids"]]
        referenced.update(c for k in local_checkpoints for c in k["claim_ids"])
        # Opening map and closing compression both refer to the global map.
        # Bind its claims explicitly instead of making Writer borrow labels.
        if index in {0, len(data["answer_structure"]) - 1}:
            referenced.update(c for m in data["core_mental_model"] for c in m["claim_ids"])
        local_distinctions = [deepcopy(d) for d in data["critical_distinctions"] if referenced & set(d["claim_ids"])]
        referenced.update(c for d in local_distinctions for c in d["claim_ids"])
        ordered_claims = [c for c in claims if c in referenced]
        required = list(dict.fromkeys(e for c in ordered_claims for e in claims[c]["evidence_labels"]))
        if not required:
            raise EvidencePackUnavailable(f"{section['id']} 没有项目证据，请将纯概念节合并到相关机制节")
        for label in required:
            if label not in items or not items[label].content or not items[label].content.strip() or items[label].truncated:
                raise EvidencePackUnavailable(f"必要证据 {label} 缺失、为空或已截断")
            if label not in required_order:
                required_order.append(label)
        optional_order.extend(e for e in section["support_labels"] if e not in optional_order)
        contract = deepcopy(section)
        contract.update({
            "owned_claims": [deepcopy(claims[c]) for c in own],
            "may_reference": [deepcopy(claims[c]) for c in section["may_reference"]],
            "dependency_claims": [deepcopy(claims[c]) for c in ordered_claims if c not in own and c not in section["may_reference"]],
            "spine": steps, "checkpoints": local_checkpoints, "critical_distinctions": local_distinctions,
            "required_labels": required,
            "prior_context_claim_ids": prior_context,
        })
        sections.append(WriterSection(section["id"], section["title"], tuple(required), contract))
        established_claims.update(own)

    def render(labels):
        selected = [deepcopy(items[e]) for e in labels]
        # Rendering only; selection/budget already decided here. No partial block.
        total = sum(len(i.content) for i in selected) + 2000 * len(selected) + 1
        rendered, kept, truncated = ContextBuilder(max_chars=total).render_items(selected)
        if truncated or len(kept) != len(selected):
            raise EvidencePackUnavailable("Evidence rendering unexpectedly truncated")
        return rendered, kept

    selected = list(required_order)
    rendered, kept = render(selected)
    if required_token_budget is None:
        if len(rendered) > max_chars:
            raise EvidencePackUnavailable(f"必要证据包 {len(rendered)} 字符超过预算 {max_chars}，未裁剪证明链")
    elif estimator.estimate(rendered) > required_token_budget:
        raise EvidencePackUnavailable("必要证据超过实际模型 token 预算，未裁剪证明链")
    for label in optional_order:
        if label in selected or label not in items or not items[label].content or items[label].truncated:
            continue
        candidate, candidate_items = render([*selected, label])
        if len(candidate) <= max_chars:
            selected.append(label)
            rendered, kept = candidate, candidate_items
    catalog = tuple({"label": i.citation.label, "citation": i.citation.to_dict(), "source_role": i.source_role,
                     "temporal_status": i.temporal_status, "content": i.content} for i in kept)
    updated = []
    for section in sections:
        allowed = list(dict.fromkeys([*section.evidence_labels, *(e for e in section.contract["support_labels"] if e in selected)]))
        section.contract["allowed_labels"] = allowed
        section.contract["support_labels"] = [e for e in section.contract["support_labels"] if e in selected]
        updated.append(replace(section, evidence_labels=tuple(allowed)))
    bundle = ContextBundle(package.original_query, kept, rendered, len(rendered), max(max_chars, len(rendered)), False)
    return WriterEvidencePack(tuple(updated), catalog, bundle)


@lru_cache(maxsize=2)
def demo_pack(kind):
    from devcontext.explanation.v3.demos import load_demo
    from devcontext.explanation.v3.validation import parse_teaching_plan
    demo = load_demo(kind)
    evidence = demo["input"]["evidence"]
    blueprint = parse_teaching_plan(demo["blueprint"], allowed_labels={e["label"] for e in evidence}, expected_depth="detailed")
    items = [ContextItem(Citation(e["label"], "DOCUMENT", "fictional-demo.md"), e["content"], i, "DOCUMENT", 1.0, i,
                         source_role="DESIGN", temporal_status="CURRENT") for i, e in enumerate(evidence, 1)]
    package = SimpleNamespace(original_query=demo["input"]["question"], evidence_workspace=None,
                              context_bundle=ContextBundle(demo["input"]["question"], items, "", 0, 28000, False))
    return blueprint, build_writer_evidence_pack(blueprint, package)
