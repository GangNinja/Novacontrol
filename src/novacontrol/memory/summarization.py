"""Memory summarization strategies."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from novacontrol.memory.models import MemoryRecord


@runtime_checkable
class MemorySummarizer(Protocol):
    async def summarize(self, records: Sequence[MemoryRecord]) -> str:
        """Summarize a sequence of records."""


class ExtractiveMemorySummarizer:
    """Dependency-free summarizer used until an LLM-backed summarizer is configured."""

    def __init__(self, *, max_items: int = 5) -> None:
        self.max_items = max_items

    async def summarize(self, records: Sequence[MemoryRecord]) -> str:
        if not records:
            return ""
        selected = sorted(records, key=lambda record: record.importance, reverse=True)[: self.max_items]
        return "\n".join(_line(record) for record in selected)


def _line(record: MemoryRecord) -> str:
    text = record.text.strip() or str(dict(record.value))
    return f"- [{record.namespace}] {record.key}: {text}"
