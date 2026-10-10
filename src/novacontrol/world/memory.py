"""Temporal memory: the current state, its bounded history, and the evidence behind it (§11).

Four things are kept, and they are kept SEPARATELY on purpose:

    the current state          one record, replaced each version
    snapshots                  a bounded ring of past versions, for "what did it
                               look like then?"
    transitions                what changed between versions, in order
    observation references     ids, sources and times — never contents

The separation is what makes the honesty rules implementable. A historical query
can only ever be answered from a snapshot that actually covers the timestamp: when
no snapshot does, the answer is "not found", never the current state wearing an old
timestamp (§22D). And the observation references hold ids and times rather than
text, so the memory is a memory OF the world and not a second copy of it (§21).

This module is also where retention lives — one place, applied on write, so a long
session cannot grow without bound. Retention never deletes the current state, and a
record whose timestamp cannot be read is KEPT rather than aged out: deleting by an
unreadable date is how a memory loses the one thing it could not file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from novacontrol.world.models import (
    EvidenceReference,
    Observation,
    TransitionKind,
    WorldState,
    WorldStateTransition,
)
from novacontrol.world.timeutil import parse_timestamp, seconds_between

__all__ = [
    "DEFAULT_RETENTION",
    "MAX_OBSERVATION_REFS",
    "WorldRetentionPolicy",
    "TemporalMemory",
    "WorldMemoryView",
]

#: The default retention posture: a day of history, a few dozen versions, the last
#: few hundred transitions, and the last few dozen observation references.
DEFAULT_RETENTION = "default"
MAX_OBSERVATION_REFS = 128


@dataclass(frozen=True, slots=True)
class WorldRetentionPolicy:
    """How much history is kept, and for how long — one table (§11)."""

    max_snapshots: int = 32
    max_transitions: int = 500
    max_observation_refs: int = MAX_OBSERVATION_REFS
    retention_seconds: int = 86400

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> WorldRetentionPolicy:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults

        def count(key: str, default: int, *, low: int, high: int) -> int:
            try:
                value = int(data.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        return cls(
            max_snapshots=count("max_snapshots", defaults.max_snapshots, low=1, high=1000),
            max_transitions=count("max_transitions", defaults.max_transitions, low=1, high=100000),
            max_observation_refs=count(
                "max_observation_refs", defaults.max_observation_refs, low=1, high=10000
            ),
            retention_seconds=count(
                "retention_seconds", defaults.retention_seconds, low=0, high=31536000
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_snapshots": self.max_snapshots,
            "max_transitions": self.max_transitions,
            "max_observation_refs": self.max_observation_refs,
            "retention_seconds": self.retention_seconds,
        }


@dataclass(frozen=True, slots=True)
class WorldMemoryView:
    """A read-only summary of what the memory holds — counts, not contents (§22D)."""

    world_id: str = "default"
    version: int = 0
    state_id: str = ""
    timestamp: str = ""
    snapshots: int = 0
    transitions: int = 0
    observations: int = 0
    oldest_snapshot: str = ""
    entities: int = 0
    relationships: int = 0
    uncertainty: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "version": self.version,
            "state_id": self.state_id,
            "timestamp": self.timestamp,
            "snapshots": self.snapshots,
            "transitions": self.transitions,
            "observations": self.observations,
            "oldest_snapshot": self.oldest_snapshot,
            "entities": self.entities,
            "relationships": self.relationships,
            "uncertainty": self.uncertainty,
        }


class TemporalMemory:
    """The state store's history, in memory, with the retention applied on write.

    Deliberately synchronous and free of I/O: the engine decides when to save, and
    this class decides what there is to save. A memory that wrote to disk on every
    observation would make ingest cost a disk write — and would make the phase's
    cheap path expensive for no gain.
    """

    def __init__(
        self,
        *,
        world_id: str = "default",
        retention: WorldRetentionPolicy | None = None,
    ) -> None:
        self.world_id = world_id
        self.retention = retention or WorldRetentionPolicy()
        self._snapshots: list[WorldState] = []
        self._transitions: list[WorldStateTransition] = []
        self._observations: list[EvidenceReference] = []
        self.pruned = 0

    # ── write ────────────────────────────────────────────────────────────────
    def set_state(self, state: WorldState, *, observation: Observation | None = None) -> None:
        """Record a new version, the transitions it produced, and its evidence."""
        if self._snapshots and self._snapshots[-1].state_id == state.state_id:
            self._snapshots[-1] = state
        else:
            self._snapshots.append(state)
        if observation is not None:
            self._observations.append(_reference_for(observation))
        self._prune()

    def record_transitions(self, transitions: Sequence[WorldStateTransition]) -> None:
        """Append the transitions of one ingest, then apply retention."""
        self._transitions.extend(transitions)
        self._prune()

    def retain(self, now: str = "") -> int:
        """Apply retention explicitly (a caller that wants the count returned)."""
        before = self._total()
        self._prune(now=now)
        return before - self._total()

    def clear(self) -> None:
        """Forget everything — the authorized state-management operation (§20)."""
        self._snapshots.clear()
        self._transitions.clear()
        self._observations.clear()

    # ── read ─────────────────────────────────────────────────────────────────
    @property
    def state(self) -> WorldState | None:
        """The newest version, or nothing when this world has never been observed."""
        return self._snapshots[-1] if self._snapshots else None

    @property
    def snapshots(self) -> tuple[WorldState, ...]:
        """Every retained version, oldest first."""
        return tuple(self._snapshots)

    @property
    def transitions(self) -> tuple[WorldStateTransition, ...]:
        return tuple(self._transitions)

    @property
    def observations(self) -> tuple[EvidenceReference, ...]:
        return tuple(self._observations)

    def version(self) -> int:
        current = self.state
        return current.version if current is not None else 0

    def state_by_id(self, state_id: str) -> WorldState | None:
        """One retained version by its id, or nothing when it was pruned."""
        wanted = str(state_id)
        for item in reversed(self._snapshots):
            if item.state_id == wanted:
                return item
        return None

    def state_at(self, timestamp: str) -> tuple[WorldState | None, str]:
        """The version covering a moment: the newest snapshot at or before it.

        Returns ``(state, reason)``. ``(None, reason)`` is the honest answer when
        nothing covers the moment — the caller must NOT substitute the current
        state, which is exactly the substitution §22D forbids.
        """
        wanted = parse_timestamp(timestamp)
        if wanted is None:
            return None, f"the requested time {timestamp!r} could not be read as a timestamp"
        for item in reversed(self._snapshots):
            recorded = parse_timestamp(item.timestamp)
            if recorded is None:
                continue
            if recorded <= wanted:
                return item, ""
        return None, (
            "no retained version covers that moment; the memory holds "
            f"{len(self._snapshots)} version(s) and none of them is at or before it"
        )

    def changes_between(self, since: str, until: str = "") -> tuple[WorldStateTransition, ...]:
        """Transitions whose own timestamp falls inside a window, in order."""
        start = parse_timestamp(since)
        end = parse_timestamp(until) if until else None
        rows: list[WorldStateTransition] = []
        for item in self._transitions:
            stamp = parse_timestamp(item.timestamp)
            if stamp is None:
                if not since and not until:
                    rows.append(item)
                continue
            if start is not None and stamp < start:
                continue
            if end is not None and stamp > end:
                continue
            rows.append(item)
        return tuple(rows)

    def transitions_for(self, entity_id: str) -> tuple[WorldStateTransition, ...]:
        """One entity's history, in order."""
        wanted = str(entity_id)
        return tuple(item for item in self._transitions if item.entity_id == wanted)

    def of_kind(self, kind: TransitionKind | str) -> tuple[WorldStateTransition, ...]:
        wanted = kind.value if isinstance(kind, TransitionKind) else str(kind)
        return tuple(item for item in self._transitions if item.kind.value == wanted)

    def stale_entities(self) -> tuple[str, ...]:
        """Entities the current state holds as absent — with the reason it holds them."""
        current = self.state
        if current is None:
            return ()
        return tuple(item.entity_id for item in current.stale_entities())

    def view(self) -> WorldMemoryView:
        """A bounded summary for a status surface (never the rows themselves)."""
        current = self.state
        return WorldMemoryView(
            world_id=self.world_id,
            version=current.version if current is not None else 0,
            state_id=current.state_id if current is not None else "",
            timestamp=current.timestamp if current is not None else "",
            snapshots=len(self._snapshots),
            transitions=len(self._transitions),
            observations=len(self._observations),
            oldest_snapshot=self._snapshots[0].timestamp if self._snapshots else "",
            entities=len(current.entities) if current is not None else 0,
            relationships=len(current.relationships) if current is not None else 0,
            uncertainty=len(current.uncertainty) if current is not None else 0,
        )

    # ── persistence ──────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        """The stored shape: versions, transitions and evidence REFERENCES only."""
        return {
            "world_id": self.world_id,
            "snapshots": [item.to_dict() for item in self._snapshots],
            "transitions": [item.to_dict() for item in self._transitions],
            "observations": [item.to_dict() for item in self._observations],
            "retention": self.retention.to_dict(),
            "pruned": self.pruned,
        }

    def restore(self, payload: Mapping[str, Any]) -> tuple[int, tuple[str, ...]]:
        """Replace the memory from a stored payload; return ``(skipped, reasons)``.

        A record that cannot be read is SKIPPED and counted, never raised: a
        truncated transition must not cost the current state. A payload that is not
        a mapping at all restores nothing and says so.
        """
        skipped = 0
        reasons: list[str] = []
        snapshots: list[WorldState] = []
        for row in _rows(payload.get("snapshots")):
            if not isinstance(row, Mapping):
                skipped += 1
                reasons.append("a snapshot row was not an object")
                continue
            try:
                snapshots.append(WorldState.from_mapping(row))
            except Exception as exc:  # noqa: BLE001 - a bad row is skipped, not fatal
                skipped += 1
                reasons.append(f"a snapshot row could not be read: {type(exc).__name__}")
        transitions: list[WorldStateTransition] = []
        for row in _rows(payload.get("transitions")):
            if not isinstance(row, Mapping):
                skipped += 1
                reasons.append("a transition row was not an object")
                continue
            try:
                transitions.append(WorldStateTransition.from_mapping(row))
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                reasons.append(f"a transition row could not be read: {type(exc).__name__}")
        observations: list[EvidenceReference] = []
        for row in _rows(payload.get("observations")):
            if not isinstance(row, Mapping):
                skipped += 1
                reasons.append("an observation reference was not an object")
                continue
            try:
                observations.append(EvidenceReference.from_mapping(row))
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                reasons.append(
                    f"an observation reference could not be read: {type(exc).__name__}"
                )
        if snapshots:
            # Versions are only useful in order; an out-of-order payload is sorted
            # rather than trusted, because a walk that assumes order must not be
            # fed a file that ignores it.
            snapshots.sort(key=lambda item: (item.version, item.timestamp))
        self._snapshots = snapshots[-self.retention.max_snapshots :]
        self._transitions = transitions[-self.retention.max_transitions :]
        self._observations = observations[-self.retention.max_observation_refs :]
        return skipped, tuple(reasons)

    # ── internals ────────────────────────────────────────────────────────────
    def _total(self) -> int:
        return len(self._snapshots) + len(self._transitions) + len(self._observations)

    def _prune(self, *, now: str = "") -> None:
        """Apply both bounds: the count ceiling and the retention window."""
        policy = self.retention
        if len(self._snapshots) > policy.max_snapshots:
            # The current version is never dropped, even if it is the only one left.
            keep = policy.max_snapshots
            self._drop(snapshots=len(self._snapshots) - keep)
        if len(self._transitions) > policy.max_transitions:
            self._drop(transitions=len(self._transitions) - policy.max_transitions)
        if len(self._observations) > policy.max_observation_refs:
            self._drop(observations=len(self._observations) - policy.max_observation_refs)
        if policy.retention_seconds > 0 and now:
            self._drop_older_than(now, policy.retention_seconds)

    def _drop(
        self, *, snapshots: int = 0, transitions: int = 0, observations: int = 0
    ) -> None:
        if snapshots > 0:
            del self._snapshots[:snapshots]
            self.pruned += snapshots
        if transitions > 0:
            del self._transitions[:transitions]
            self.pruned += transitions
        if observations > 0:
            del self._observations[:observations]
            self.pruned += observations

    def _drop_older_than(self, now: str, seconds: int) -> None:
        """Age out what a readable timestamp says is old; keep what has no timestamp."""
        cutoff_reference = parse_timestamp(now)
        if cutoff_reference is None:
            return
        kept_snapshots = [self._snapshots[-1]] if self._snapshots else []
        for item in self._snapshots[:-1]:
            stamp = parse_timestamp(item.timestamp)
            if stamp is None or (cutoff_reference - stamp).total_seconds() <= seconds:
                kept_snapshots.append(item)
            else:
                self.pruned += 1
        self._snapshots = kept_snapshots
        self._transitions = [
            item
            for item in self._transitions
            if _within(item.timestamp, cutoff_reference, seconds)
        ]
        self._observations = [
            item
            for item in self._observations
            if _within(item.timestamp, cutoff_reference, seconds)
        ]


def _within(timestamp: str, now: datetime, seconds: int) -> bool:
    """Whether a timestamp is inside the retention window (unreadable stays)."""
    stamp = parse_timestamp(timestamp)
    if stamp is None:
        return True
    age = (now - stamp).total_seconds()
    return age <= seconds


def _reference_for(observation: Observation) -> EvidenceReference:
    """The reference stored for an ingest — ids, source and time; never the content."""
    return EvidenceReference(
        evidence_id=observation.observation_id,
        kind="observation",
        source=observation.source_id or observation.source.value,
        detail=f"scope {observation.scope}" if observation.scope else "",
        timestamp=observation.timestamp,
    )


def _rows(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def age_seconds(timestamp: str, now: str) -> float | None:
    """How long ago a timestamp was, or ``None`` when either end is unreadable."""
    seconds = seconds_between(timestamp, now)
    return seconds
