"""Incremental Markdown boundaries, independent of model-authored protocols."""
import re
import time

from devcontext.explanation.stream_writer import ValidatedSection


def clean_citations(text, available):
    """Only plain Markdown text is a citation surface; code is preserved."""
    citations, warnings = [], []
    def plain(value):
        def replace(match):
            label = match.group(1)
            if re.fullmatch(r"E[1-9]\d*", label) and label in available:
                if label not in citations:
                    citations.append(label)
                return match.group(0)
            warnings.append({"reference": label, "warning": "invalid citation removed"})
            return ""
        return re.sub(r"\[([EC]\d[^\]\n]*)\]", replace, value)
    def inline(value):
        # Exact delimiter runs, including multi-line inline code. A single
        # backtick inside a double-backtick span is not a closing delimiter.
        pieces, cursor = [], 0
        while cursor < len(value):
            opening = re.search(r"`+", value[cursor:])
            if opening is None:
                pieces.append(plain(value[cursor:])); break
            start = cursor + opening.start()
            end = cursor + opening.end()
            delimiter = value[start:end]
            closing = re.search(r"(?<!`)" + re.escape(delimiter) + r"(?!`)", value[end:])
            if closing is None:
                pieces.append(plain(value[cursor:])); break
            stop = end + closing.end()
            pieces.extend((plain(value[cursor:start]), value[start:stop]))
            cursor = stop
        return "".join(pieces)
    output, prose, fence = [], [], None
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None:
            output.append(line)
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
        elif match:
            output.extend((inline("".join(prose)), line)); prose.clear()
            fence = (match[1][0], len(match[1]))
        else:
            prose.append(line)
    output.append(inline("".join(prose)))
    return "".join(output), tuple(citations), warnings


class MarkdownParts:
    """Release ordinary text immediately, commit at H2 outside fenced code."""
    def __init__(self, generation, available, on_section=None, *, limit=200_000):
        self.generation, self.available, self.on_section = generation, available, on_section
        self.limit = limit
        self.sections, self.warnings = [], []
        self.body, self.prefix, self.line, self.delta = "", "", "", []
        self.decided = False
        self.fence = None
        self.opening = False
        self.active = None
        self.visible = False
        self.first_ready_at = None

    def _release(self, text):
        if not text:
            return
        if self.active is None:
            self.active = f"S{len(self.sections)+1}"
            self.generation.parser_update("start", self.active)
        if len(self.body) + len(text) > self.limit:
            raise ValueError("Markdown part exceeds safety limit")
        self.body += text
        self.delta.append(text)
        self.visible |= bool(text.strip())

    def _flush(self):
        if self.delta:
            self.generation.parser_update("delta", self.active, "".join(self.delta))
            self.delta.clear()

    def _commit(self):
        self._flush()
        if not self.body.strip():
            return
        text, citations, warnings = clean_citations(self.body.strip(), self.available)
        self.warnings.extend(warnings)
        if not text.strip():
            raise ValueError("empty answer part after citation cleanup")
        section = ValidatedSection(self.active, text, citations)
        from devcontext.observability.diagnostics import mark
        mark("first_section_ready", {"section_id": self.active})
        self.generation.commit(section)
        if self.on_section is not None:
            from devcontext.delivery import DeliveryError
            try:
                self.on_section(section)
            except Exception as exc:
                raise DeliveryError("answer section delivery failed") from exc
            mark("first_published", {"section_id": self.active})
        self.sections.append(section)
        if self.first_ready_at is None:
            self.first_ready_at = time.perf_counter()
        self.body, self.active = "", None

    def feed(self, text):
        for char in text:
            self.line += char
            if self.decided or self.fence is not None:
                self._release(char)
            else:
                self.prefix += char
                stripped = self.prefix.lstrip(" ")
                indent = len(self.prefix) - len(stripped)
                possible_heading = "## ".startswith(stripped) or "##\t".startswith(stripped)
                possible_fence = "```".startswith(stripped) or "~~~".startswith(stripped)
                if indent <= 3 and stripped in {"## ", "##\t"}:
                    self._commit()
                    self.decided = True
                    self._release(self.prefix)
                    self.prefix = ""
                elif indent <= 3 and stripped in {"```", "~~~"}:
                    self.opening = True
                    self.decided = True
                    self._release(self.prefix)
                    self.prefix = ""
                elif indent > 3 or not (possible_heading or possible_fence or not stripped):
                    self.decided = True
                    self._release(self.prefix)
                    self.prefix = ""
            if char == "\n":
                if self.prefix:
                    self._release(self.prefix)
                    self.prefix = ""
                match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", self.line.rstrip("\r\n"))
                if self.opening and match:
                    self.fence = (match[1][0], len(match[1]))
                elif self.fence and match and match[1][0] == self.fence[0] and len(match[1]) >= self.fence[1] and not match[2].strip():
                    self.fence = None
                self.line, self.decided, self.opening = "", False, False
        self._flush()

    def finish(self):
        if self.prefix:
            self._release(self.prefix)
            self.prefix = ""
        self._commit()
