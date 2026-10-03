"""Deterministic readability diagnostics. Counts are heuristics, not a judge."""
from __future__ import annotations

import re
from collections.abc import Sequence

from devcontext.explanation.teaching_policy import ABSTRACT_TERMS, TEACHING_GLOSSARY


_CITATIONS = re.compile(r"\[E\d+\]")
_MARKERS = re.compile(r"<<<(?:END_)?SECTION:[^>]+>>>")
_FENCES = re.compile(r"(?ms)^```[^\n]*\n.*?^```[ \t]*$|^~~~[^\n]*\n.*?^~~~[ \t]*$")
_INLINE_CODE = re.compile(r"`+([^`\n]+)`+")
_CODE_WORD = re.compile(r"(?<!\w)@?[A-Za-z_]\w*(?:[.#][A-Za-z_]\w*)*(?:\(\))?(?!\w)")
_BARE_CODE = re.compile(
    r"(?<!\w)(?:@[A-Za-z_]\w*|[A-Za-z_]\w*(?:[.#][A-Za-z_]\w*)+|"
    r"[A-Za-z_]\w*\(\)|[a-z]+(?:[A-Z][A-Za-z0-9]*)+|"
    r"[A-Z][A-Za-z0-9]*(?:Service|Controller|Mapper|DTO|DO|Impl)|"
    r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?!\w)"
)


def _word_pattern(words):
    # Longest match first avoids counting 事务 inside 事务边界 twice.
    return re.compile("|".join(re.escape(word) for word in sorted(words, key=lambda word: (-len(word), word))), re.I)


def _code_spans(text):
    inline = list(_INLINE_CODE.finditer(text))
    spans = []
    for block in inline:
        spans.extend((block.start(1) + match.start(), block.start(1) + match.end())
                     for match in _CODE_WORD.finditer(block.group(1)))
    spans.extend((match.start(), match.end()) for match in _BARE_CODE.finditer(text)
                 if not any(block.start() <= match.start() < block.end() for block in inline))
    return sorted(spans)


def _max_identifier_run(text, spans):
    maximum = run = 0
    previous = None
    for start, end in spans:
        gap = text[previous:start] if previous is not None else ""
        consecutive = previous is not None and "\n\n" not in gap and re.fullmatch(r"[\s,，、;；/|&+`]*", gap)
        run = run + 1 if consecutive else 1
        maximum = max(maximum, run)
        previous = end
    return maximum


def _plain_text(markdown):
    text = _INLINE_CODE.sub(lambda match: match.group(1), markdown)
    text = re.sub(r"!?\[([^\]]+)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+|>\s*)", "", text)
    text = re.sub(r"(\*\*|\*|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", text)
    text = re.sub(r"(?<![A-Za-z0-9_])(__|_)(?=\S)(.+?)(?<=\S)\1(?![A-Za-z0-9_])", r"\2", text)
    return text.replace("|", "")


def _chars(text):
    return len(re.sub(r"\s+", "", text))


class TeachingStyleInspector:
    def __init__(self, vocabulary: Sequence[str] | None = None):
        self.term_pattern = _word_pattern(vocabulary or tuple(TEACHING_GLOSSARY))
        self.abstract_pattern = _word_pattern(ABSTRACT_TERMS)
        self.seen_terms = set()
        self.sections = []

    def checkpoint(self):
        return len(self.sections), self.seen_terms.copy()

    def restore(self, checkpoint):
        length, seen_terms = checkpoint
        del self.sections[length:]
        self.seen_terms = seen_terms

    def inspect_section(self, section_id: str, markdown: str, planned_new_terms=None, target_chars=None):
        text = _MARKERS.sub("", markdown)
        # Remove fences before headings: a Java comment that looks like a heading
        # remains available for the separate code statistics.
        fences = list(_FENCES.finditer(text))
        code_block_chars = sum(_chars(block.group()) for block in fences)
        code_block_identifiers = sum(len(list(_CODE_WORD.finditer(block.group()))) for block in fences)
        body = _FENCES.sub("", text)
        body = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", body)
        citation_count = len(_CITATIONS.findall(body))
        body = _CITATIONS.sub("", body)
        spans = _code_spans(body)
        max_run = _max_identifier_run(body, spans)
        plain = _plain_text(body)
        section_chars = _chars(plain)
        sentences = [_chars(sentence) for sentence in re.split(r"[。！？!?]+|\n\s*\n", plain)]
        sentences = [size for size in sentences if size]
        terms = list(dict.fromkeys(match.group().casefold() for match in self.term_pattern.finditer(plain)))
        new_terms = [term for term in terms if term not in self.seen_terms]
        warnings = []
        for code, value, threshold in (
            ("LONG_SECTION", section_chars, 320),
            ("LONG_SENTENCE", max(sentences, default=0), 80),
            ("NEW_TERM_LOAD", len(new_terms), 2),
            ("IDENTIFIER_RUN", max_run, 4),
        ):
            if value > threshold:
                warnings.append({"section_id": section_id, "code": code, "value": value, "threshold": threshold})
        record = {
            "section_id": section_id, "section_chars": section_chars,
            "sentence_count": len(sentences), "sentence_chars": sentences,
            "avg_sentence_chars": round(sum(sentences) / len(sentences), 2) if sentences else 0,
            "max_sentence_chars": max(sentences, default=0),
            "long_sentence_count": sum(size > 80 for size in sentences),
            "long_sentence_rate": round(sum(size > 80 for size in sentences) / len(sentences), 4) if sentences else 0,
            "target_chars": target_chars,
            "target_chars_delta": None if target_chars is None else section_chars - target_chars,
            "planned_new_terms": None if planned_new_terms is None else list(planned_new_terms),
            "planned_new_term_count": None if planned_new_terms is None else len(planned_new_terms),
            "observed_new_terms": new_terms, "observed_new_term_count": len(new_terms),
            "code_identifier_count": len(spans) + code_block_identifiers,
            "code_block_chars": code_block_chars,
            "max_consecutive_identifiers": max_run,
            "abstract_term_count": len(self.abstract_pattern.findall(plain)),
            "citation_count": citation_count,
            "citation_density": round(100 * citation_count / section_chars, 2) if section_chars else 0,
            "warnings": warnings,
        }
        self.seen_terms.update(terms)
        self.sections.append(record)
        return record

    def summarize(self, completion_status: str):
        count = len(self.sections)
        sentences = [size for section in self.sections for size in section["sentence_chars"]]
        chars = sum(section["section_chars"] for section in self.sections)
        citations = sum(section["citation_count"] for section in self.sections)
        long_count = sum(size > 80 for size in sentences)
        return {
            "metrics_version": "teaching_style_v1", "heuristic": True,
            "completion_status": completion_status, "sections_inspected_count": count,
            "section_chars": {section["section_id"]: section["section_chars"] for section in self.sections},
            "total_body_chars": chars,
            "avg_section_chars": round(chars / count, 2) if count else 0,
            "max_section_chars": max((section["section_chars"] for section in self.sections), default=0),
            "sentence_count": len(sentences),
            "avg_sentence_chars": round(sum(sentences) / len(sentences), 2) if sentences else 0,
            "max_sentence_chars": max(sentences, default=0),
            "long_sentence_count": long_count,
            "long_sentence_rate": round(long_count / len(sentences), 4) if sentences else 0,
            "planned_new_terms_per_section": {s["section_id"]: s["planned_new_term_count"] for s in self.sections},
            "observed_new_terms_per_section": {s["section_id"]: s["observed_new_term_count"] for s in self.sections},
            "code_identifier_count": sum(s["code_identifier_count"] for s in self.sections),
            "abstract_term_count": sum(s["abstract_term_count"] for s in self.sections),
            "citation_count": citations,
            "citation_density": round(100 * citations / chars, 2) if chars else 0,
            "warnings": [warning for s in self.sections for warning in s["warnings"]],
            "sections": list(self.sections),
        }


def inspect_answer(markdown: str, completion_status: str):
    """Offline historical comparison with the same vocabulary and statistics."""
    inspector = TeachingStyleInspector()
    blocks = [block for block in re.split(r"(?m)(?=^## )", markdown) if block.strip()]
    for index, block in enumerate(blocks, 1):
        inspector.inspect_section(f"S{index}", block)
    return inspector.summarize(completion_status)
