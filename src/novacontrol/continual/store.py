"""Persistence for Phase 27's learning evidence.

The store is intentionally boring: the SAME ``JsonStateStore`` the rest of the
application uses, one namespace, atomic writes, and a forgiving read. What lives
above it is the part that matters — ``LearningState`` parses every row through
its model's ``from_dict`` and COUNTS what it skipped instead of guessing at a
corrupt record. A corrupt row costs exactly itself.

Retention is bounded by the engine, not by this module: the store's job is to
round-trip a payload, and it does so even when the data directory is unusable
(the underlying store already degrades instead of raising).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from novacontrol.continual.models import (
    SCHEMA_VERSION,
    ExecutionEvidence,
    LearningProposal,
    LearningSignal,
    StrategyRecord,
)
from novacontrol.persistence.json_store import JsonStateStore

__all__ = [
    "NAMESPACE",
    "ContinualLearningStore",
    "InMemoryContinualLearningStore",
    "JsonContinualLearningStore",
    "LearningState",
]

#: One key in the application's state store. The name continues the phase
#: vocabulary (``interactive`` state uses per-object keys; this subsystem keeps
#: its bounded corpus together so retention can prune it as one decision).
NAMESPACE = "continual_learning"


class ContinualLearningStore(Protocol):
    """The seam the engine persists through — injectable for tests."""

    def load(self) -> dict[str, Any]:  # pragma: no cover - protocol
        ...

    def save(self, payload: Mapping[str, Any]) -> None:  # pragma: no cover - protocol
        ...


class InMemoryContinualLearningStore:
    """A store that survives nothing — for tests and for a disabled build."""

    def __init__(self, payload: Mapping[str, Any] | None = None) -> None:
        self._payload: dict[str, Any] = dict(payload or {})

    def load(self) -> dict[str, Any]:
        return dict(self._payload)

    def save(self, payload: Mapping[str, Any]) -> None:
        self._payload = dict(payload)


class JsonContinualLearningStore:
    """The application's persisted learning corpus, via ``JsonStateStore``."""

    def __init__(self, store: JsonStateStore, namespace: str = NAMESPACE) -> None:
        self.store = store
        self.namespace = namespace

    def load(self) -> dict[str, Any]:
        return dict(self.store.read(self.namespace))

    def save(self, payload: Mapping[str, Any]) -> None:
        self.store.write(self.namespace, dict(payload))


@dataclass(slots=True)
class LearningState:
    """The parsed corpus, with an honest report of what could not be read."""

    experiences: dict[str, ExecutionEvidence] = field(default_factory=dict)
    signals: dict[str, LearningSignal] = field(default_factory=dict)
    strategies: dict[str, StrategyRecord] = field(default_factory=dict)
    proposals: dict[str, LearningProposal] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LearningState":
        """Parse a stored payload. Never raises; skipped rows are counted."""
        state = cls()
        if not isinstance(payload, Mapping):
            return state
        stored_schema = str(payload.get("schema_version", "") or "")
        if stored_schema and stored_schema != SCHEMA_VERSION:
            # A corpus from another schema is not reinterpreted. Every row would
            # have to be guessed at, and guessed evidence is worse than none.
            state.skipped["schema"] = 1
            return state
        state.schema_version = stored_schema or SCHEMA_VERSION
        state._read_rows(payload.get("experiences"), ExecutionEvidence.from_dict, state.experiences, "experiences")
        state._read_rows(payload.get("signals"), LearningSignal.from_dict, state.signals, "signals")
        state._read_rows(payload.get("strategies"), StrategyRecord.from_dict, state.strategies, "strategies")
        state._read_rows(payload.get("proposals"), LearningProposal.from_dict, state.proposals, "proposals")
        return state

    def _read_rows(
        self,
        rows: Any,
        parser: Any,
        target: dict[str, Any],
        label: str,
    ) -> None:
        if rows is None:
            return
        if not isinstance(rows, list):
            self.skipped[label] = self.skipped.get(label, 0) + 1
            return
        for raw in rows:
            parsed = parser(raw) if isinstance(raw, Mapping) else None
            if parsed is None:
                self.skipped[label] = self.skipped.get(label, 0) + 1
                continue
            key = _row_id(parsed)
            if not key:
                self.skipped[label] = self.skipped.get(label, 0) + 1
                continue
            target[key] = parsed

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "experiences": [record.to_dict() for record in self.experiences.values()],
            "signals": [signal.to_dict() for signal in self.signals.values()],
            "strategies": [strategy.to_dict() for strategy in self.strategies.values()],
            "proposals": [proposal.to_dict() for proposal in self.proposals.values()],
        }

    def report(self) -> dict[str, Any]:
        return {
            "experiences": len(self.experiences),
            "signals": len(self.signals),
            "strategies": len(self.strategies),
            "proposals": len(self.proposals),
            "skipped": dict(self.skipped),
            "schema_version": self.schema_version,
        }


def _row_id(record: Any) -> str:
    for name in ("experience_id", "signal_id", "strategy_id", "proposal_id"):
        value = getattr(record, name, "")
        if value:
            return str(value)
    return ""
