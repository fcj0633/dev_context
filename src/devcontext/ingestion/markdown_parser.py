from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from devcontext.models import Chunk


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
EXCLUDED_PARTS = {"bower_components", "node_modules", "target", ".git", ".idea"}


@dataclass(slots=True)
class _Section:
    title: str | None
    heading_path: list[str]
    lines: list[str]
    first_line: int


def _is_excluded(path: Path) -> bool:
    return any(
        part.lower() in EXCLUDED_PARTS or part.lower().startswith("sbadmin2-")
        for part in path.parts
    )


def iter_markdown_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if not _is_excluded(relative):
            yield path


def _paragraph_ranges(lines: list[str], first_line: int) -> list[tuple[list[str], int, int]]:
    ranges: list[tuple[list[str], int, int]] = []
    current: list[str] = []
    start = first_line
    for offset, line in enumerate(lines):
        line_number = first_line + offset
        if not current:
            start = line_number
        current.append(line)
        if not line.strip():
            ranges.append((current, start, line_number))
            current = []
    if current:
        ranges.append((current, start, first_line + len(lines) - 1))
    return ranges


def _split_section(section: _Section, max_chars: int) -> list[tuple[str, int, int]]:
    paragraphs = _paragraph_ranges(section.lines, section.first_line)
    pieces: list[tuple[str, int, int]] = []
    current_lines: list[str] = []
    current_start = section.first_line
    current_end = section.first_line

    def flush() -> None:
        nonlocal current_lines
        text = "\n".join(current_lines).strip()
        if text:
            pieces.append((text, current_start, current_end))
        current_lines = []

    for paragraph_lines, paragraph_start, paragraph_end in paragraphs:
        paragraph_text = "\n".join(paragraph_lines).strip()
        if not paragraph_text:
            continue
        if len(paragraph_text) > max_chars:
            flush()
            line_buffer: list[str] = []
            line_start = paragraph_start
            for offset, line in enumerate(paragraph_lines):
                line_number = paragraph_start + offset
                if line_buffer and len("\n".join(line_buffer + [line])) > max_chars:
                    pieces.append(("\n".join(line_buffer).strip(), line_start, line_number - 1))
                    line_buffer = []
                    line_start = line_number
                while len(line) > max_chars:
                    if line_buffer:
                        pieces.append(("\n".join(line_buffer).strip(), line_start, line_number - 1))
                        line_buffer = []
                    pieces.append((line[:max_chars], line_number, line_number))
                    line = line[max_chars:]
                    line_start = line_number
                line_buffer.append(line)
            if line_buffer:
                pieces.append(("\n".join(line_buffer).strip(), line_start, paragraph_end))
            continue

        candidate = "\n".join(current_lines + paragraph_lines).strip()
        if current_lines and len(candidate) > max_chars:
            flush()
            current_start = paragraph_start
        if not current_lines:
            current_start = paragraph_start
        current_lines.extend(paragraph_lines)
        current_end = paragraph_end
    flush()
    return pieces


def parse_markdown_file(
    root: Path,
    path: Path,
    repository: str = "my12306",
    max_chars: int = 6000,
) -> list[Chunk]:
    raw_lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    sections: list[_Section] = []
    heading_stack: list[str] = []
    current_title: str | None = None
    current_path: list[str] = []
    current_lines: list[str] = []
    current_first_line = 1

    def finish() -> None:
        if current_lines and any(line.strip() for line in current_lines):
            sections.append(
                _Section(current_title, list(current_path), list(current_lines), current_first_line)
            )

    for index, line in enumerate(raw_lines, start=1):
        match = HEADING_RE.match(line)
        if not match:
            current_lines.append(line)
            continue
        finish()
        level = len(match.group(1))
        title = match.group(2).strip()
        heading_stack[level - 1 :] = []
        while len(heading_stack) < level - 1:
            heading_stack.append("")
        heading_stack.append(title)
        current_title = title
        current_path = [item for item in heading_stack if item]
        current_lines = []
        current_first_line = index + 1
    finish()

    relative = path.relative_to(root).as_posix()
    chunks: list[Chunk] = []
    for section in sections:
        for content, start_line, end_line in _split_section(section, max_chars):
            chunks.append(
                Chunk(
                    repository=repository,
                    source_type="DOCUMENT",
                    chunk_type="DOCUMENT_SECTION",
                    file_path=relative,
                    title=section.title or path.stem,
                    heading_path=section.heading_path,
                    content=content,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
    return chunks


def parse_markdown_tree(
    root: Path,
    repository: str = "my12306",
    max_chars: int = 6000,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in iter_markdown_files(root):
        chunks.extend(parse_markdown_file(root, path, repository, max_chars))
    return chunks
