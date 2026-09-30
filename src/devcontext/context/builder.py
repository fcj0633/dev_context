from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from devcontext.evidence import EvidenceAnnotation
from devcontext.models import Citation, ContextBundle, ContextItem, SearchResult


DEFAULT_MAX_CHARS = 6000
TRUNCATION_MARKER = "… [truncated]"
ITEM_SEPARATOR = "\n\n"
CONTENT_SEPARATOR = "\n\n"


@dataclass(slots=True, frozen=True)
class _RankedResult:
    retrieval_rank: int
    result: SearchResult


class ContextBuilder:
    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be greater than 0")
        self.max_chars = max_chars

    def build(
        self,
        query: str,
        results: Sequence[SearchResult],
        annotations: Mapping[int, EvidenceAnnotation] | None = None,
    ) -> ContextBundle:
        if not query.strip():
            raise ValueError("query must not be empty")

        ranked_results = self._deduplicate(results)
        if not ranked_results:
            return ContextBundle(
                query=query,
                items=[],
                rendered_text="",
                total_chars=0,
                max_chars=self.max_chars,
                truncated=False,
            )

        prioritized = self._prioritize_sources(ranked_results)
        items = [
            self._item_from_result(
                ranked, f"C{index}", (annotations or {}).get(ranked.result.id)
            )
            for index, ranked in enumerate(prioritized, start=1)
        ]
        rendered_text, kept, budget_truncated = self.render_items(items)
        return ContextBundle(
            query=query,
            items=kept,
            rendered_text=rendered_text,
            total_chars=len(rendered_text),
            max_chars=self.max_chars,
            truncated=budget_truncated,
        )

    def render_items(
        self, items: Sequence[ContextItem]
    ) -> tuple[str, list[ContextItem], bool]:
        """Apply the character budget to already-built items, keeping their labels.

        Shared with the workspace views so a view renders through the same header
        format as this builder. Note the budget is a hard stop: the first item
        that does not fit ends the render and everything after it is dropped.
        Views over the workspace exist precisely because that is not an
        acceptable way to choose what a coverage check gets to see.
        """
        kept: list[ContextItem] = []
        blocks: list[str] = []
        budget_truncated = False

        for item in items:
            header = self._render_header(item)
            full_block = self._render_block(header, item.content)
            separator_length = len(ITEM_SEPARATOR) if blocks else 0
            current_length = len(ITEM_SEPARATOR.join(blocks))
            available = self.max_chars - current_length - separator_length

            if len(full_block) <= available:
                kept.append(item)
                blocks.append(full_block)
                continue

            budget_truncated = True
            content_budget = (
                available
                - len(header)
                - len(CONTENT_SEPARATOR)
                - len(TRUNCATION_MARKER)
            )
            if item.content and content_budget >= 1:
                item.content = item.content[:content_budget] + TRUNCATION_MARKER
                item.truncated = True
                kept.append(item)
                blocks.append(self._render_block(header, item.content))
            break

        return ITEM_SEPARATOR.join(blocks), kept, budget_truncated

    @staticmethod
    def _deduplicate(results: Sequence[SearchResult]) -> list[_RankedResult]:
        unique: list[_RankedResult] = []
        seen: set[int] = set()
        for rank, result in enumerate(results, start=1):
            if result.id in seen:
                continue
            seen.add(result.id)
            unique.append(_RankedResult(retrieval_rank=rank, result=result))
        return unique

    def _prioritize_sources(
        self, ranked_results: list[_RankedResult]
    ) -> list[_RankedResult]:
        first_code = next(
            (item for item in ranked_results if item.result.source_type == "CODE"), None
        )
        first_document = next(
            (item for item in ranked_results if item.result.source_type == "DOCUMENT"), None
        )
        if first_code is None or first_document is None:
            return ranked_results

        anchors = sorted(
            (first_code, first_document), key=lambda item: item.retrieval_rank
        )
        anchor_blocks = [
            self._render_full_ranked(item, f"C{index}")
            for index, item in enumerate(anchors, start=1)
        ]
        if len(ITEM_SEPARATOR.join(anchor_blocks)) > self.max_chars:
            return ranked_results

        anchor_ids = {item.result.id for item in anchors}
        remaining = [item for item in ranked_results if item.result.id not in anchor_ids]
        return anchors + remaining

    def _render_full_ranked(self, ranked: _RankedResult, label: str) -> str:
        item = self._item_from_result(ranked, label)
        return self._render_block(self._render_header(item), item.content)

    @staticmethod
    def _item_from_result(
        ranked: _RankedResult,
        label: str,
        annotation: EvidenceAnnotation | None = None,
    ) -> ContextItem:
        result = ranked.result
        citation = Citation(
            label=label,
            source_type=result.source_type,
            file_path=result.file_path,
            class_name=result.class_name,
            symbol_name=result.symbol_name,
            signature=result.signature,
            start_line=result.start_line,
            end_line=result.end_line,
            heading_path=list(result.heading_path),
        )
        return ContextItem(
            citation=citation,
            content=result.content.strip(),
            chunk_id=result.id,
            chunk_type=result.chunk_type,
            score=result.score,
            retrieval_rank=ranked.retrieval_rank,
            source_role=annotation.source_role if annotation else "UNKNOWN",
            temporal_status=annotation.temporal_status if annotation else "UNKNOWN",
            authority_priority=annotation.authority_priority if annotation else 50,
            sub_question_ids=list(annotation.sub_question_ids) if annotation else [],
        )

    @staticmethod
    def _render_header(item: ContextItem) -> str:
        citation = item.citation
        lines = [
            f"[{citation.label}] {citation.source_type}",
            f"File: {citation.file_path}",
        ]
        if citation.source_type == "CODE":
            symbol = ContextBuilder._code_symbol(citation)
            if symbol:
                lines.append(f"Symbol: {symbol}")
            if citation.signature:
                lines.append(f"Signature: {citation.signature}")
            lines.append(
                f"Lines: {ContextBuilder._line_range(citation.start_line, citation.end_line)}"
            )
        else:
            heading = " > ".join(citation.heading_path) or "(root)"
            lines.append(f"Heading: {heading}")
        lines.append(f"Score: {item.score:.6f}")
        if item.source_role != "UNKNOWN" or item.temporal_status != "UNKNOWN":
            lines.append(
                f"Evidence: role={item.source_role}; time={item.temporal_status}; "
                f"priority={item.authority_priority}"
            )
        if item.sub_question_ids:
            lines.append(f"Supports: {', '.join(item.sub_question_ids)}")
        return "\n".join(lines)

    @staticmethod
    def _code_symbol(citation: Citation) -> str | None:
        if citation.class_name and citation.symbol_name:
            return f"{citation.class_name}#{citation.symbol_name}"
        return citation.class_name or citation.symbol_name

    @staticmethod
    def _line_range(start_line: int | None, end_line: int | None) -> str:
        if start_line is not None and end_line is not None:
            return f"{start_line}-{end_line}"
        if start_line is not None:
            return str(start_line)
        if end_line is not None:
            return f"?-{end_line}"
        return "unknown"

    @staticmethod
    def _render_block(header: str, content: str) -> str:
        return f"{header}{CONTENT_SEPARATOR}{content}" if content else header
