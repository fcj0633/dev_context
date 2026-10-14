"""Citation binding is a local source check, not factual certification."""
from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class CitationPolicy:
    writer_input_scope: bool = True
    normalize_heading: bool = True


INPUT_CITATIONS = CitationPolicy()


def prepare_section(section, text, available, policy, warnings):
    if policy.normalize_heading:
        expected = f"## {section.title}"
        lines = text.strip().splitlines()
        if lines and lines[0] != expected:
            if re.match(r"^#{1,6}\s", lines[0]):
                lines[0] = expected
            else:
                lines.insert(0, expected)
            if warnings is not None:
                warnings.append({"section": section.id, "warning": "heading normalized"})
        text = "\n".join(lines)
    allowed = set(available) if policy.writer_input_scope else set(section.evidence_labels) & set(available)
    return text, allowed
