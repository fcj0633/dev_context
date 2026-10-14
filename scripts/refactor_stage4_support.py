"""Frozen Writer evidence restoration. No provider or retrieval calls."""
import json
from pathlib import Path
from types import SimpleNamespace

from devcontext.context.builder import ContextBuilder
from devcontext.context.views import context_item_from_ref
from devcontext.models import ContextBundle
from devcontext.observability.diagnostics import restore_package
from devcontext.synthesis.inputs import AnswerInput

ROOT = Path(__file__).resolve().parents[1]


def restore_writer_snapshot(path):
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    request, package = restore_package(snapshot["retrieval"])
    attempts = [a for a in snapshot["model_attempts"] if a.get("writer")]
    payload = json.loads(attempts[-1]["messages"][-1]["content"])
    text = payload["evidence"]
    found = []
    for ref in package.evidence_workspace.all():
        item = context_item_from_ref(ref)
        header = ContextBuilder._render_header(item)
        position = text.find(header+"\n\n")
        if position >= 0:
            found.append((position, header, item))
    found.sort(key=lambda value: value[0])
    items = []
    for i, (position, header, item) in enumerate(found):
        stop = found[i+1][0]-2 if i+1 < len(found) else len(text)
        body = text[position+len(header)+2:stop]
        item.truncated = body != item.content
        item.content = body
        items.append(item)
    rendered, kept, _ = ContextBuilder(max_chars=len(text)+1).render_items(items)
    if rendered != text:
        raise ValueError(f"{path.name}: exact Writer evidence restoration failed")
    bundle = ContextBundle(payload["question"], kept, rendered, len(rendered), len(rendered)+1,
                           any(i.truncated for i in kept))
    raw_plan = payload["plan"]
    sections = [SimpleNamespace(id=s["id"], title=s["title"], evidence_labels=s["evidence_labels"])
                for s in raw_plan["sections"]]
    plan = SimpleNamespace(sections=sections,
        evidence_labels=tuple(dict.fromkeys(e for s in sections for e in s.evidence_labels)),
        to_dict=lambda: raw_plan)
    selected = {i.chunk_id for i in bundle.items}
    manifest = tuple({"label": r.evidence_id, "chunk_id": r.chunk_id,
        "requirement_ids": list(r.requirement_ids), "included": r.chunk_id in selected,
        "already_truncated": next((i.truncated for i in bundle.items if i.chunk_id==r.chunk_id), False),
        "reason": "frozen_writer_input" if r.chunk_id in selected else "not_in_frozen_writer_input"}
        for r in package.evidence_workspace.all())
    common = AnswerInput(payload["question"], request.policy.reader_assumption, bundle,
        package.retrieval_state, tuple(r.to_dict() for r in package.evidence_plan.requirements),
        tuple(c.to_dict() for c in package.requirement_coverage), tuple(package.unresolved_requirements),
        {}, manifest, {"max_output_tokens": 16000, "evidence_mode": "exact_frozen_writer_input"})
    return request, package, plan, common
