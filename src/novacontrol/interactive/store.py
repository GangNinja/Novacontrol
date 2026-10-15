"""Persistence for interactive learning — the shared store, used the shared way (§13).

The same three-method repository shape the world model uses, over the same
:class:`JsonStateStore`, with the same forgiving behaviour: a missing, empty,
truncated or hand-edited record reads as "nothing was stored yet", and a write that
cannot land reports ``False`` rather than failing the work that produced it.

Two decisions are deliberate.

**One key per objective, plus an index.** Isolation is per objective, so a corrupt
record can only cost the objective it belongs to; the index is what a restart walks.
A bad index is treated as "no objectives", and an objective whose own record is
unreadable is skipped rather than crashing the restore.

**The stored form is reference-only.** Predictions, evidence references and statuses
are stored; observation bodies and reasoning text are not. Nothing here can persist a
chain of thought, because there is no field for one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from novacontrol.persistence.json_store import JsonStateStore

__all__ = [
    "InMemoryLearningRepository",
    "JsonLearningRepository",
    "LearningRepository",
    "sanitize_key",
]

#: Keys an objective id may contain once sanitized. Everything else becomes ``_``.
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")

#: An objective id is a label, not a document.
MAX_KEY_LENGTH = 96

#: The index key — the only fixed key this module uses.
INDEX_KEY = "interactive_learning_index"


def sanitize_key(value: str, *, default: str = "objective") -> str:
    """An objective id that is safe to form a store key from, and stable if it is not."""
    text = str(value or "").strip()
    if not text:
        return default
    cleaned = _SAFE_ID.sub("_", text)[:MAX_KEY_LENGTH]
    return cleaned.strip("._-") or default


class LearningRepository(Protocol):
    """Where a learning objective's stored payload lives. Three methods, no more."""

    def load(self, objective_id: str) -> dict[str, Any]:
        """The stored payload, or an empty mapping when there is nothing to read."""
        ...

    def save(self, objective_id: str, payload: Mapping[str, Any]) -> bool:
        """Persist a payload; ``False`` means the write did not land."""
        ...

    def clear(self, objective_id: str) -> bool:
        """Remove a payload; ``False`` means nothing was there to remove."""
        ...

    def index(self) -> tuple[str, ...]:
        """Every objective id this repository holds."""
        ...

    def remember(self, objective_id: str) -> bool:
        """Add an objective to the index — how a restart finds it again."""
        ...


class JsonLearningRepository:
    """A repository over the application's JSON state store."""

    def __init__(self, store: JsonStateStore, *, prefix: str = "learning") -> None:
        self.store = store
        self.prefix = prefix

    def key(self, objective_id: str) -> str:
        return f"{self.prefix}_{sanitize_key(objective_id)}"

    def load(self, objective_id: str) -> dict[str, Any]:
        try:
            payload = self.store.read(self.key(objective_id))
        except Exception:  # noqa: BLE001 - an unreadable store is an empty one here
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def save(self, objective_id: str, payload: Mapping[str, Any]) -> bool:
        try:
            self.store.write(self.key(objective_id), dict(payload))
        except Exception:  # noqa: BLE001 - persistence must not fail the work
            return False
        return True

    def clear(self, objective_id: str) -> bool:
        try:
            self.store.write(self.key(objective_id), {})
        except Exception:  # noqa: BLE001
            return False
        return True

    def index(self) -> tuple[str, ...]:
        """The stored index of objective ids, filtered to ids that have a record."""
        try:
            payload = self.store.read(INDEX_KEY)
        except Exception:  # noqa: BLE001
            return ()
        raw = payload.get("objectives") if isinstance(payload, Mapping) else None
        if not isinstance(raw, list):
            return ()
        out: list[str] = []
        for item in raw:
            key = str(item or "").strip()
            if key and key not in out:
                out.append(key)
        return tuple(out)

    def remember(self, objective_id: str) -> bool:
        """Add an objective to the index — how a restart finds it again."""
        key = str(objective_id or "").strip()
        if not key:
            return False
        stored = list(self.index())
        if key not in stored:
            stored.append(key)
        try:
            self.store.write(INDEX_KEY, {"objectives": stored[-128:]})
        except Exception:  # noqa: BLE001
            return False
        return True

    def forget(self, objective_id: str) -> bool:
        """Drop an objective from the index (its record is cleared separately)."""
        key = str(objective_id or "").strip()
        if not key:
            return False
        stored = [item for item in self.index() if item != key]
        try:
            self.store.write(INDEX_KEY, {"objectives": stored})
        except Exception:  # noqa: BLE001
            return False
        return True


class InMemoryLearningRepository:
    """A repository that holds objectives in memory — tests, and a no-disk mode."""

    def __init__(self) -> None:
        self._payloads: dict[str, dict[str, Any]] = {}
        self._index: list[str] = []
        self.saves = 0
        self.loads = 0
        self.fail_writes = False

    def load(self, objective_id: str) -> dict[str, Any]:
        self.loads += 1
        stored = self._payloads.get(sanitize_key(objective_id))
        return dict(stored) if stored else {}

    def save(self, objective_id: str, payload: Mapping[str, Any]) -> bool:
        if self.fail_writes:
            return False
        self.saves += 1
        self._payloads[sanitize_key(objective_id)] = dict(payload)
        return True

    def clear(self, objective_id: str) -> bool:
        key = sanitize_key(objective_id)
        self._payloads.pop(key, None)
        self._index = [item for item in self._index if sanitize_key(item) != key]
        return True

    def index(self) -> tuple[str, ...]:
        return tuple(self._index)

    def remember(self, objective_id: str) -> bool:
        key = str(objective_id or "").strip()
        if key and key not in self._index:
            self._index.append(key)
        return bool(key)

    def forget(self, objective_id: str) -> bool:
        key = str(objective_id or "").strip()
        self._index = [item for item in self._index if item != key]
        return bool(key)

    def corrupt(self, objective_id: str, payload: Mapping[str, Any]) -> None:
        """Write a payload that could not have been produced by this module."""
        self._payloads[sanitize_key(objective_id)] = dict(payload)
