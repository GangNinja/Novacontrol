"""Persistence for the world model — the shared store, used the shared way (§12).

NovaControl already has one local persistence pattern: :class:`JsonStateStore`,
which stages a payload in a unique temporary file and moves it into place, reads
forgivingly (a missing, empty, truncated or hand-edited file means "nothing was
stored yet") and never raises into the work that produced the snapshot. The world
model uses exactly that, through a three-method repository so tests and previews
can hold a world in memory without touching a disk.

Two properties matter more than the mechanism:

**A corrupt record cannot corrupt the current state.** The stored payload is
read row by row by :meth:`TemporalMemory.restore`, which skips what it cannot read
and counts it; the file itself degrading to "nothing" is the same answer as a
first run.

**Isolation is by world id.** Each world persists under its own key, so a fact
from one environment cannot appear in another: two worlds sharing a file would be
one world with extra steps (§11).

``world_id`` is sanitized before it becomes part of a filename. It arrives from
configuration and from API bodies, and a path-forming id is the one piece of this
module that could escape its directory.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from novacontrol.persistence.json_store import JsonStateStore

__all__ = [
    "InMemoryWorldRepository",
    "JsonWorldRepository",
    "WorldRepository",
    "sanitize_world_id",
]

#: Keys a world id may contain once sanitized. Everything else becomes ``_``.
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")

#: A world id is a label, not a document: bounded so a caller cannot make one that
#: exceeds a filesystem's name limit.
MAX_WORLD_ID = 64


def sanitize_world_id(world_id: str, *, default: str = "default") -> str:
    """A world id that is safe to form a filename from, and stable if it is not.

    A blank id becomes ``default``; anything path-ish is flattened. The result is
    deterministic, so the same caller always reopens the same world.
    """
    text = str(world_id or "").strip()
    if not text:
        return default
    cleaned = _SAFE_ID.sub("_", text)[:MAX_WORLD_ID]
    cleaned = cleaned.strip("._-") or default
    return cleaned


class WorldRepository(Protocol):
    """Where a world's stored payload lives. Three methods, no more."""

    def load(self, world_id: str) -> dict[str, Any]:
        """The stored payload, or an empty mapping when there is nothing to read."""
        ...

    def save(self, world_id: str, payload: Mapping[str, Any]) -> bool:
        """Persist a payload; ``False`` means the write did not land."""
        ...

    def clear(self, world_id: str) -> bool:
        """Remove a world's payload; ``False`` means nothing was there to remove."""
        ...


class JsonWorldRepository:
    """A repository over the application's JSON state store."""

    def __init__(self, store: JsonStateStore, *, prefix: str = "world") -> None:
        self.store = store
        self.prefix = prefix

    def key(self, world_id: str) -> str:
        """The store key for one world — the isolation boundary, in one place."""
        return f"{self.prefix}_{sanitize_world_id(world_id)}"

    def load(self, world_id: str) -> dict[str, Any]:
        try:
            payload = self.store.read(self.key(world_id))
        except Exception:  # noqa: BLE001 - an unreadable store is an empty one here
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def save(self, world_id: str, payload: Mapping[str, Any]) -> bool:
        try:
            self.store.write(self.key(world_id), dict(payload))
        except Exception:  # noqa: BLE001 - persistence must not fail the work
            return False
        return True

    def clear(self, world_id: str) -> bool:
        try:
            self.store.write(self.key(world_id), {})
        except Exception:  # noqa: BLE001
            return False
        return True


class InMemoryWorldRepository:
    """A repository that holds worlds in memory — for tests and for a no-disk mode."""

    def __init__(self) -> None:
        self._worlds: dict[str, dict[str, Any]] = {}
        self.saves = 0
        self.loads = 0

    def load(self, world_id: str) -> dict[str, Any]:
        self.loads += 1
        stored = self._worlds.get(sanitize_world_id(world_id))
        return dict(stored) if stored else {}

    def save(self, world_id: str, payload: Mapping[str, Any]) -> bool:
        self.saves += 1
        self._worlds[sanitize_world_id(world_id)] = dict(payload)
        return True

    def clear(self, world_id: str) -> bool:
        return self._worlds.pop(sanitize_world_id(world_id), None) is not None

    def worlds(self) -> tuple[str, ...]:
        """Every world this repository holds — used by tests to prove isolation."""
        return tuple(sorted(self._worlds))
