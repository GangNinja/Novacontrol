"""Chunking: turning a document into retrievable pieces.

A chunk is the unit retrieval returns, so how the text is cut decides what the
answer can be about. Three rules shape this module:

* **Respect the structure that exists.** Markdown is cut on headings and prose
  on paragraphs, so a chunk is a thought rather than 200 tokens of the middle of
  one. A code file is cut at top-level definitions, so a hit is a function.
* **Carry the context a chunk needs to be understood.** A body paragraph under
  "## Configuration" is nearly useless without that heading, so the heading
  travels INSIDE the chunk's text and in its ``heading`` field.
* **Overlap only what a cut could split.** Two chunks of one paragraph share a
  few tokens so a sentence spanning the boundary is in both; independent
  sections are never duplicated.

Line ranges are tracked because a citation that cannot be looked up is a
footnote, not evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from novacontrol.knowledge.models import SourceType, estimate_tokens

#: Target size of a chunk. Small enough that four of them fit a modest budget,
#: large enough that a paragraph and its surrounding sentences stay together.
DEFAULT_CHUNK_TOKENS = 220
#: How much of the previous chunk is repeated at the start of the next one.
DEFAULT_OVERLAP_TOKENS = 40
#: Below this, a block is merged with its neighbour instead of becoming a chunk.
MIN_CHUNK_TOKENS = 40

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE_START_RE = re.compile(
    r"^(?:async\s+def|def|class|function|export|public|private|protected|impl|"
    r"struct|enum|interface|type|fn|func|sub|template|@\w+)\b"
)
_BLANK_LINE_RE = re.compile(r"^\s*$")


@dataclass(frozen=True, slots=True)
class TextChunk:
    """One chunk before it is indexed: text, where it came from, what it is under."""

    text: str
    ordinal: int
    start_line: int
    end_line: int
    heading: str = ""

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(slots=True)
class _Block:
    text: str
    start_line: int
    end_line: int
    heading: str = ""
    tokens: int = field(default=0)

    def __post_init__(self) -> None:
        self.tokens = estimate_tokens(self.text)


def chunk_document(
    text: str,
    *,
    source_type: SourceType = SourceType.TEXT,
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> tuple[TextChunk, ...]:
    """Cut one document into chunks, according to what kind of document it is.

    An empty or whitespace-only document has no chunks: an empty chunk would
    match nothing and still occupy a slot in every budget.
    """
    if not text.strip():
        return ()
    if source_type in (SourceType.MARKDOWN, SourceType.DOCUMENTATION):
        blocks = _markdown_blocks(text)
    elif source_type is SourceType.CODE:
        blocks = _code_blocks(text)
    else:
        blocks = _text_blocks(text)
    return _pack(blocks, max_tokens=max_tokens, overlap_tokens=overlap_tokens)


def _lines_with_numbers(text: str) -> list[tuple[int, str]]:
    return list(enumerate(text.splitlines(), start=1))


def _text_blocks(text: str) -> list[_Block]:
    """Paragraph blocks: blank lines separate, nothing else does."""
    blocks: list[_Block] = []
    current: list[str] = []
    start = 1
    for number, line in _lines_with_numbers(text):
        if _BLANK_LINE_RE.match(line):
            if current:
                blocks.append(_Block("\n".join(current).strip(), start, number - 1))
                current = []
            start = number + 1
            continue
        if not current:
            start = number
        current.append(line)
    if current:
        blocks.append(_Block("\n".join(current).strip(), start, len(text.splitlines())))
    return [block for block in blocks if block.text]


def _markdown_blocks(text: str) -> list[_Block]:
    """Heading-aware blocks: each block knows the heading path above it."""
    blocks: list[_Block] = []
    path: list[str] = []
    current: list[str] = []
    start = 1
    heading = ""
    for number, line in _lines_with_numbers(text):
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            if current:
                blocks.append(_Block("\n".join(current).strip(), start, number - 1, heading))
                current = []
            depth = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            path = [*path[: depth - 1], title]
            heading = " > ".join(path)
            start = number
            current = [line]
            continue
        if not current:
            start = number
        current.append(line)
    if current:
        blocks.append(_Block("\n".join(current).strip(), start, len(text.splitlines()), heading))
    return [block for block in blocks if block.text.strip()]


def _code_blocks(text: str) -> list[_Block]:
    """Definition-aware blocks: a top-level definition starts a new chunk."""
    blocks: list[_Block] = []
    current: list[str] = []
    start = 1
    lines = _lines_with_numbers(text)
    for number, line in lines:
        starts_definition = bool(_CODE_START_RE.match(line)) and not line.startswith((" ", "\t"))
        if starts_definition and current and estimate_tokens("\n".join(current)) >= MIN_CHUNK_TOKENS:
            blocks.append(_Block("\n".join(current).rstrip(), start, number - 1))
            current = []
        if not current:
            start = number
        current.append(line)
    if current:
        blocks.append(_Block("\n".join(current).rstrip(), start, lines[-1][0] if lines else 1))
    return [block for block in blocks if block.text.strip()]


def _render(heading: str, body: str) -> str:
    """A chunk's text, with the heading path it sits under in front of it.

    A body paragraph under "Configuration > Endpoints" is nearly useless on its
    own, so the heading travels with the text (and in the chunk's ``heading``
    field for a citation).

    The heading LINE is removed from the body first when it is the same
    heading: a Markdown block already begins with "## Configuration", and
    prefixing it would both double the text (and its token cost) and repeat the
    line in every excerpt a person reads. Removing it keeps the hierarchy — the
    prefix names the path, which the line alone never did.
    """
    body = body.strip()
    if not heading:
        return body
    lines = body.splitlines()
    if lines and _is_heading_line(lines[0], heading):
        body = "\n".join(lines[1:]).strip()
    return f"# {heading}\n{body}" if body else f"# {heading}"


def _is_heading_line(line: str, heading: str) -> bool:
    """Whether this line IS the heading the block is filed under."""
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return False
    text = stripped.lstrip("#").strip()
    if not text:
        return False
    return text == heading.rsplit(" > ", 1)[-1]


def _pack(
    blocks: list[_Block],
    *,
    max_tokens: int,
    overlap_tokens: int,
) -> tuple[TextChunk, ...]:
    """Merge blocks into chunks of about ``max_tokens``, with a bounded overlap.

    Blocks are merged while they fit; a single block larger than the target is
    cut on line boundaries rather than split mid-word, and the overlap between
    consecutive chunks of the SAME block is what keeps a sentence that spans the
    cut retrievable from both sides.
    """
    target = max(MIN_CHUNK_TOKENS, int(max_tokens))
    overlap = max(0, min(int(overlap_tokens), target // 2))
    chunks: list[TextChunk] = []
    pending: list[_Block] = []
    pending_tokens = 0

    def flush(extra: _Block | None = None) -> None:
        nonlocal pending, pending_tokens
        blocks_to_write = [*pending, *([extra] if extra is not None else [])]
        if not blocks_to_write:
            return
        text = "\n\n".join(
            _render(block.heading, block.text) for block in blocks_to_write
        ).strip()
        chunks.append(
            TextChunk(
                text=text,
                ordinal=len(chunks),
                start_line=blocks_to_write[0].start_line,
                end_line=blocks_to_write[-1].end_line,
                heading=blocks_to_write[0].heading,
            )
        )
        pending = []
        pending_tokens = 0

    for block in blocks:
        if block.tokens > target:
            flush()
            for piece in _split_oversized(block, target=target, overlap=overlap, ordinal_offset=len(chunks)):
                chunks.append(piece)
            continue
        if pending and pending_tokens + block.tokens > target:
            previous = pending
            flush()
            if overlap and previous:
                tail = _tail_text(
                    "\n\n".join(_render(item.heading, item.text) for item in previous),
                    overlap,
                )
                if tail:
                    carry = _Block(
                        text=tail,
                        start_line=previous[-1].end_line,
                        end_line=previous[-1].end_line,
                        heading=previous[-1].heading,
                    )
                    pending = [carry]
                    pending_tokens = carry.tokens
        pending.append(block)
        pending_tokens += block.tokens
        if pending_tokens >= target:
            flush()
    flush()
    return tuple(
        TextChunk(
            text=chunk.text,
            ordinal=index,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            heading=chunk.heading,
        )
        for index, chunk in enumerate(chunks)
    )


def _split_oversized(block: _Block, *, target: int, overlap: int, ordinal_offset: int) -> list[TextChunk]:
    """Cut one oversized block on its own lines, keeping the overlap."""
    lines = block.text.splitlines()
    pieces: list[TextChunk] = []
    current: list[str] = []
    current_start = block.start_line
    for offset, line in enumerate(lines):
        number = block.start_line + offset
        if current and estimate_tokens("\n".join([*current, line])) > target:
            body = "\n".join(current)
            pieces.append(
                TextChunk(
                    text=_render(block.heading, body),
                    ordinal=ordinal_offset + len(pieces),
                    start_line=current_start,
                    end_line=number - 1,
                    heading=block.heading,
                )
            )
            carried = _tail_text(body, overlap).splitlines() if overlap else []
            carry = [item for item in carried if item.strip()]
            current = [*carry, line]
            current_start = max(block.start_line, number - len(carry))
            continue
        current.append(line)
    if current:
        pieces.append(
            TextChunk(
                text=_render(block.heading, "\n".join(current)),
                ordinal=ordinal_offset + len(pieces),
                start_line=current_start,
                end_line=block.end_line,
                heading=block.heading,
            )
        )
    return pieces


def _tail_text(text: str, tokens: int) -> str:
    """The last ``tokens``-worth of text, cut at a line boundary when possible."""
    if tokens <= 0:
        return ""
    characters = tokens * 4
    if len(text) <= characters:
        return text
    tail = text[-characters:]
    newline = tail.find("\n")
    return tail[newline + 1 :] if 0 <= newline < len(tail) - 1 else tail


__all__ = [
    "DEFAULT_CHUNK_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "MIN_CHUNK_TOKENS",
    "TextChunk",
    "chunk_document",
]
