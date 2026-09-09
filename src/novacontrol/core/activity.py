"""Recent-activity journal: the server-side source of the web timeline.

The Command Center's "Recent Activity" feed used to be client-only (written
straight into ``localStorage`` by whichever tab initiated the action), so it
neither showed actions that came from another client (CLI, GUI, a second tab)
nor survived a browser profile reset. This journal is the single in-process
store every completed action is recorded into, so the timeline can be seeded on
page load and fed live from the shared ``/events/stream`` channel — no polling,
no per-tab localStorage.

Entries are newest-first and bounded. A completion is recorded WHEREVER the
action completes (application.py for command/learn, the explore API handler for
research), and the same code path publishes a ``*.completed`` event on the
application EventBus so every open UI tab appends it over SSE.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any


class RecentActivityLog:
    """Thread-safe, bounded queue of completed user actions."""

    def __init__(self, limit: int = 50) -> None:
        self._limit = max(1, limit)
        self._entries: deque[dict[str, Any]] = deque(maxlen=self._limit)
        self._lock = threading.Lock()

    def record(
        self,
        type_: str,
        title: str,
        detail: str = "",
        *,
        at: datetime | None = None,
    ) -> None:
        """Record one completed action (newest first, bounded)."""
        entry = {
            "type": type_,
            "title": title,
            "detail": detail or "",
            "at": int((at or datetime.now(UTC)).timestamp() * 1000),
        }
        with self._lock:
            self._entries.appendleft(entry)

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Snapshot of recent entries, newest first, capped at ``limit``."""
        with self._lock:
            entries = list(self._entries)
        if limit is None or limit < 0:
            return entries
        return entries[:limit]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
