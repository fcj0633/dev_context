from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json

from devcontext.context.builder import ContextBuilder
from devcontext.context.budget import TokenBudgetPolicy
from devcontext.context.views import context_item_from_ref
from devcontext.models import Citation, ContextBundle, ContextItem


@dataclass(frozen=True, slots=True)
class AnswerInput:
    question: str
    reader: str
    bundle: ContextBundle
    retrieval_state: str
    requirements: tuple[dict, ...]
    coverage: tuple[dict, ...]
    unresolved: tuple[str, ...]
    structure: dict
    selection: tuple[dict, ...]
    budget: dict
    schema_version: int = 1

    @property
    def available_labels(self):
        return frozenset(i.citation.label for i in self.bundle.items if i.content.strip())

    def to_dict(self):
        return json.loads(json.dumps({"schema_version": self.schema_version, "question": self.question,
            "reader": self.reader, "bundle": self.bundle.to_dict(),
            "retrieval_state": self.retrieval_state, "requirements": list(self.requirements),
            "coverage": list(self.coverage), "unresolved": list(self.unresolved),
            "structure": self.structure, "selection": list(self.selection), "budget": self.budget}, ensure_ascii=False))

    @classmethod
    def from_dict(cls, data):
        data = deepcopy(data)
        if data.get("schema_version") != 1:
            raise ValueError("unsupported AnswerInput version")
        raw = data["bundle"]
        items = [ContextItem(**{**i, "citation": Citation(**i["citation"])}) for i in raw["items"]]
        labels = [i.citation.label for i in items]
        ids = [i.chunk_id for i in items]
        if len(set(labels)) != len(labels) or len(set(ids)) != len(ids):
            raise ValueError("duplicate source identity")
        expected = "\n\n".join(ContextBuilder._render_block(ContextBuilder._render_header(i), i.content) for i in items)
        if raw["rendered_text"] != expected:
            raise ValueError("rendered evidence differs from source bodies")
        raw["items"] = items
        return cls(data["question"], data["reader"], ContextBundle(**raw), data["retrieval_state"],
            tuple(data["requirements"]), tuple(data["coverage"]), tuple(data["unresolved"]),
            data["structure"], tuple(data["selection"]), data["budget"])


class AnswerInputBuilder:
    """Select whole evidence blocks; never use a prose outline to select sources."""
    def __init__(self, capabilities, estimator):
        self.capabilities, self.estimator = capabilities, estimator

    def build(self, request, package, budget):
        workspace = package.evidence_workspace
        candidates = ([context_item_from_ref(r) for r in workspace.all()] if workspace is not None
                      else deepcopy(package.context_bundle.items))
        by_id, by_label = {}, {}
        for item in candidates:
            if item.chunk_id in by_id:
                if item.citation != by_id[item.chunk_id].citation or item.content != by_id[item.chunk_id].content:
                    raise ValueError("conflicting evidence identity")
                prior = by_id[item.chunk_id]
                prior.sub_question_ids = sorted(set(prior.sub_question_ids) | set(item.sub_question_ids))
                prior.truncated |= item.truncated
                continue
            if item.citation.label in by_label:
                raise ValueError("conflicting stable label")
            by_id[item.chunk_id], by_label[item.citation.label] = item, item
        requirements = tuple(r.to_dict() for r in package.evidence_plan.requirements)
        structure = workspace.structural_metadata() if workspace is not None else {}
        endpoints = {s["chunk_id"] for s in structure.get("symbols", [])}

        def rank(item):
            name = item.citation.symbol_name or item.citation.class_name or ""
            return (item.chunk_id not in endpoints, not (name and name in request.original_query),
                not (item.citation.source_type == "CODE" and item.temporal_status != "HISTORICAL"),
                -item.authority_priority, item.retrieval_rank, item.chunk_id, item.citation.label)

        queues = []
        for requirement in sorted(requirements, key=lambda r: r["priority"] != "CORE"):
            queues.append(sorted([i for i in by_id.values() if requirement["id"] in i.sub_question_ids], key=rank))
        queues.append(sorted(by_id.values(), key=rank))
        ordered, seen = [], set()
        # CORE queues are exhausted before supporting queues, but each CORE gets
        # a chance in each pass. The final orphan queue only supplies leftovers.
        core_count = sum(r["priority"] == "CORE" for r in requirements)
        groups = (queues[:core_count], queues[core_count:-1], queues[-1:])
        for group in groups:
            while any(group):
                for values in group:
                    if not values:
                        continue
                    item = values.pop(0)
                    if item.chunk_id not in seen:
                        seen.add(item.chunk_id)
                        ordered.append(item)

        output = min(budget.max_output_tokens, self.capabilities.max_output_tokens)
        policy = TokenBudgetPolicy(self.capabilities)
        chosen, decisions = [], []

        def make(items, selections):
            total = sum(len(i.content) + 2000 for i in items) + 1
            rendered, kept, _ = ContextBuilder(max_chars=total).render_items(deepcopy(items))
            included = {i.chunk_id for i in kept}
            complete = {i.chunk_id for i in kept if not i.truncated}
            symbols = [deepcopy(s) for s in structure.get("symbols", [])]
            keys = {s["symbol_key"]: s["chunk_id"] for s in symbols}
            relations = [{**deepcopy(r), "source_evidence_in_input": keys.get(r["source"]) in included,
                "target_evidence_in_input": keys.get(r["target"]) in included,
                "source_evidence_untruncated": keys.get(r["source"]) in complete,
                "target_evidence_untruncated": keys.get(r["target"]) in complete}
                for r in structure.get("relations", [])
                if keys.get(r["source"]) in included or keys.get(r["target"]) in included]
            public_structure = {"symbols": [{**s, "evidence_in_input": s["chunk_id"] in included,
                "evidence_untruncated": s["chunk_id"] in complete}
                for s in symbols if s["chunk_id"] in included or any(s["symbol_key"] in {r["source"],r["target"]} for r in relations)],
                "relations": relations}
            return AnswerInput(request.original_query, request.policy.reader_assumption if request.policy else "基础 Java 读者",
                ContextBundle(request.original_query, kept, rendered, len(rendered), total,
                    any(i.truncated for i in kept) or any(not s["included"] for s in selections)),
                package.retrieval_state, requirements, tuple(c.to_dict() for c in package.requirement_coverage),
                tuple(package.unresolved_requirements), public_structure, tuple(selections),
                {"max_output_tokens": output, "context_window": self.capabilities.context_window,
                 "safety_margin_tokens": policy.safety_margin_tokens,
                 "max_chars_override": request.context_budget_override})

        for item in ordered:
            entry = {"label": item.citation.label, "chunk_id": item.chunk_id,
                "requirement_ids": list(item.sub_question_ids), "included": False,
                "already_truncated": item.truncated, "reason": "empty"}
            if item.content.strip():
                probe = make([*chosen, item], [])
                tokens = self._tokens(probe)
                fits = policy.decide(fixed_tokens=tokens, requested_output_tokens=output).allowed
                char_fits = request.context_budget_override is None or len(probe.bundle.rendered_text) <= request.context_budget_override
                entry.update(included=fits and char_fits,
                    reason="included" if fits and char_fits else "token_budget" if not fits else "max_chars")
                if entry["included"]:
                    chosen.append(item)
            decisions.append(entry)

        result = make(chosen, decisions)
        # Selection/coverage metadata is real prompt input too. Evict the last
        # selected block if that overhead requires space, never truncate bodies.
        while not policy.decide(fixed_tokens=self._tokens(result), requested_output_tokens=output).allowed:
            if not chosen:
                raise ValueError("question and evidence metadata exceed model window")
            removed = chosen.pop()
            next(e for e in decisions if e["chunk_id"] == removed.chunk_id).update(included=False, reason="metadata_budget")
            result = make(chosen, decisions)
        data = result.to_dict()
        data["budget"]["prompt_estimated_tokens"] = self._tokens(result)
        data["budget"]["omitted_core_requirements"] = [r["id"] for r in requirements if r["priority"] == "CORE"
            and any(not e["included"] and r["id"] in e["requirement_ids"] for e in decisions)]
        # Budget diagnostics are not sent back to the model; avoid recursive
        # accounting where estimates alter the prompt being estimated.
        return AnswerInput.from_dict(data)

    def _tokens(self, value):
        from .prompts import writer_messages
        return sum(self.estimator.estimate(m.content) for m in writer_messages(value))
