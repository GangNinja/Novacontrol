"""NF-17 — the objective service: one interpretation per validated state.

The service observes the states NF-16 produces and holds the current objective
picture. It is deliberately small, because interpretation is a pure function:
this layer only decides WHICH states are allowed to update the picture, keeps a
bounded history, publishes the change, and persists enough to survive a restart.

The rules:

* **Only newer observations count.** The same observation twice, or a state
  older than the newest one in the same session, is ignored rather than
  re-applied — otherwise one frame could be counted as two progress steps.
* **A session change supersedes, it does not merge.** Objectives are
  session-scoped; the previous session's live objectives are superseded with an
  event, and the new session starts its own list.
* **Interpretation can never fail the reading.** A broken interpreter is a
  recorded error (``last_error``), never an exception into the state service.
* **Nothing here captures or acts.** The input is a state that already exists.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .objectives import (
    SCHEMA_VERSION,
    ObjectiveProfileRegistry,
    ObjectiveState,
    default_objective_registry,
    interpret,
)
from .state import GameState

__all__ = [
    "OBJECTIVE_EVENTS",
    "OBJECTIVE_NAMESPACE",
    "ObjectiveService",
    "ObjectiveStore",
    "Publish",
]

#: The state-store namespace for the persisted objective history.
OBJECTIVE_NAMESPACE = "novaforge_objectives"

#: What this service publishes: one summary per interpretation that changed
#: anything. It never publishes the raw screen text.
OBJECTIVE_EVENTS = ("novaforge.objectives.updated",)

#: Bounded history: a few dozen readings and events is enough to explain a run.
DEFAULT_MAX_HISTORY = 32
DEFAULT_MAX_EVENTS = 64


class Publish(Protocol):
    def __call__(self, event_type: str, payload: dict[str, Any]) -> Awaitable[None]: ...


class ObjectiveStore(Protocol):
    def read(self, namespace: str) -> Mapping[str, Any] | None: ...

    def write(self, namespace: str, value: Mapping[str, Any]) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ObjectiveService:
    """The bounded observer of NF-16 states. All collaborators are injected."""

    registry: ObjectiveProfileRegistry = field(default_factory=default_objective_registry)
    publish: Publish | None = None
    store: Any | None = None
    clock: Callable[[], datetime] = _now
    max_history: int = DEFAULT_MAX_HISTORY
    max_events: int = DEFAULT_MAX_EVENTS
    _state: ObjectiveState | None = field(default=None, init=False, repr=False)
    _history: deque[dict[str, Any]] = field(default_factory=deque, init=False, repr=False)
    _events: deque[dict[str, Any]] = field(default_factory=deque, init=False, repr=False)
    _durations: list[float] = field(default_factory=list, init=False, repr=False)
    _last_key: tuple[str, str, int] | None = field(default=None, init=False, repr=False)
    _last_error: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _received: int = field(default=0, init=False, repr=False)
    _ignored: int = field(default=0, init=False, repr=False)
    _duplicates: int = field(default=0, init=False, repr=False)
    _failed: int = field(default=0, init=False, repr=False)
    _restored: bool = field(default=False, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.max_history = max(1, int(self.max_history))
        self.max_events = max(1, int(self.max_events))
        self._history = deque(maxlen=self.max_history)
        self._events = deque(maxlen=self.max_events)
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self.store is None:
            return
        try:
            payload = self.store.read(OBJECTIVE_NAMESPACE)
        except Exception:  # noqa: BLE001 - an unreadable store is not a crash
            return
        if not isinstance(payload, Mapping):
            return
        events = payload.get("events")
        if isinstance(events, list):
            self._events = deque(
                [dict(row) for row in events if isinstance(row, Mapping)][-self.max_events :],
                maxlen=self.max_events,
            )
        history = payload.get("history")
        if isinstance(history, list):
            self._history = deque(
                [dict(row) for row in history if isinstance(row, Mapping)][-self.max_history :],
                maxlen=self.max_history,
            )
        self._restored = bool(self._events or self._history)
        # The counters are persisted WITH the history, so a panel after a restart
        # reports the same reading counts it reports before one. Loading only the
        # rows made the status claim "0 read" beside a full history.
        counts = payload.get("counts")
        if isinstance(counts, Mapping):
            for name in ("received", "ignored", "duplicates", "failed"):
                figure = counts.get(name)
                if isinstance(figure, int) and not isinstance(figure, bool) and figure >= 0:
                    setattr(self, f"_{name}", figure)

    def _persist(self) -> None:
        if self.store is None:
            return
        try:
            self.store.write(
                OBJECTIVE_NAMESPACE,
                {
                    "schema_version": SCHEMA_VERSION,
                    "saved_at": self.clock().isoformat(),
                    "session_id": self._state.session_id if self._state is not None else "",
                    "history": [dict(row) for row in self._history],
                    "events": [dict(row) for row in self._events],
                    "counts": {
                        "received": self._received,
                        "ignored": self._ignored,
                        "duplicates": self._duplicates,
                        "failed": self._failed,
                    },
                },
            )
        except Exception:  # noqa: BLE001 - persistence must not fail the reading
            return

    # ── the interpretation ─────────────────────────────────────────────────
    async def observe(self, state: GameState) -> ObjectiveState | None:
        """Interpret one state, or explain why it was not interpreted.

        Called by the state service after a successful reading, so it must be
        quick and must never raise: objective understanding is a consumer of the
        perception pipeline, not a second pipeline.
        """
        if self._stopped:
            return None
        try:
            session_id = str(state.observation.session_id or "").strip()
            observation_id = str(state.observation.observation_id or "").strip()
            sequence = int(state.observation.seq)
        except Exception:  # noqa: BLE001 - an object that is not a state is a refusal
            # The docstring's promise is unconditional: a caller handing over the
            # wrong object is a recorded refusal, never an exception into the
            # state service's notification path.
            self._failed += 1
            self._last_error = {
                "reason_code": "invalid_observation",
                "reason": "The object handed to the objective service is not a game state.",
                "at": self.clock().isoformat(),
            }
            self._persist()
            return None
        if not session_id or not observation_id:
            self._failed += 1
            self._last_error = {
                "reason_code": "invalid_observation",
                "reason": "The state has no session or observation identity, so it cannot be "
                "attributed to a run.",
                "at": self.clock().isoformat(),
            }
            self._persist()
            return None
        key = (session_id, observation_id, sequence)
        if self._last_key is not None:
            last_session, last_observation, last_seq = self._last_key
            if (session_id, key[1]) == (last_session, last_observation):
                self._duplicates += 1
                # Persist the count: it is part of the status a restart reads back
                # (the reading itself is unchanged, but the counter moved).
                self._persist()
                return self._state
            if session_id == last_session and key[2] <= last_seq:
                self._ignored += 1
                self._last_error = {
                    "reason_code": "observation_out_of_order",
                    "reason": (
                        f"Observation {key[2]} is not newer than {last_seq} in this session; "
                        "the state was not re-applied."
                    ),
                    "at": self.clock().isoformat(),
                }
                self._persist()
                return self._state

        started = time.monotonic()
        try:
            result = interpret(state, self._state, registry=self.registry, now=self.clock())
        except Exception as exc:  # noqa: BLE001 - a broken interpretation is a result
            self._failed += 1
            self._last_error = {
                "reason_code": "interpretation_failed",
                "reason": f"Objective interpretation failed: {type(exc).__name__}: {exc}",
                "at": self.clock().isoformat(),
            }
            self._persist()
            return None
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)

        self._received += 1
        self._last_key = key
        self._last_error = None
        self._state = result
        self._durations.append(elapsed_ms)
        del self._durations[:-50]
        for event in result.events:
            self._events.append(event.to_dict())
        self._history.append(
            {
                "observation_id": result.observation_id,
                "session_id": result.session_id,
                "at": result.at,
                "sequence": result.sequence,
                "profile_id": result.profile_id,
                "objectives": [
                    {
                        "objective_id": item.objective_id,
                        "description": item.description,
                        "status": item.status.value,
                        "progress": item.progress.value if item.progress is not None else None,
                        "target": item.progress.target if item.progress is not None else None,
                    }
                    for item in result.objectives
                ],
                "events": [event.kind.value for event in result.events],
            }
        )
        self._persist()
        await self._publish(result)
        return result

    async def _publish(self, result: ObjectiveState) -> None:
        """Announce the change once; a lost event cannot fail the interpretation."""
        if self.publish is None or not result.events:
            return
        try:
            await self.publish(
                "novaforge.objectives.updated",
                {
                    "session_id": result.session_id,
                    "observation_id": result.observation_id,
                    "sequence": result.sequence,
                    "profile_id": result.profile_id,
                    "events": [event.kind.value for event in result.events],
                    "counts": result.counts,
                    "objectives": [
                        {
                            "objective_id": item.objective_id,
                            "status": item.status.value,
                            "progress": (
                                item.progress.value if item.progress is not None else None
                            ),
                        }
                        for item in result.objectives
                        if not item.status.value == "superseded"
                    ],
                },
            )
        except Exception:  # noqa: BLE001 - the announcement is not the work
            return

    # ── lifecycle and surfaces ─────────────────────────────────────────────
    async def shutdown(self) -> None:
        """Stop accepting states. Safe to repeat; nothing is left running."""
        self._stopped = True

    def reset(self, reason: str = "requested") -> dict[str, Any]:
        """Forget the current objectives and the ordering baseline, keep history."""
        self._state = None
        self._last_key = None
        self._persist()
        return {
            "reset": True,
            "reason": reason,
            "history_kept": len(self._history),
            "events_kept": len(self._events),
        }

    @property
    def current(self) -> ObjectiveState | None:
        """The newest interpretation, for a consumer that reasons over it (NF-20)."""
        return self._state

    def status(self) -> dict[str, Any]:
        durations = list(self._durations)
        return {
            "schema_version": SCHEMA_VERSION,
            "available": True,
            "tested": self._state is not None,
            "restored": self._restored,
            "stopped": self._stopped,
            "state": self._state.to_dict() if self._state is not None else None,
            "history": [dict(row) for row in self._history],
            "events": [dict(row) for row in self._events],
            "counts": {
                "received": self._received,
                "ignored": self._ignored,
                "duplicates": self._duplicates,
                "failed": self._failed,
                "history": len(self._history),
                "events": len(self._events),
            },
            "measured": {
                "interpretation_ms": {
                    "last": durations[-1] if durations else None,
                    "average": round(sum(durations) / len(durations), 2) if durations else None,
                    "max": max(durations) if durations else None,
                }
            },
            "last_error": dict(self._last_error) if self._last_error is not None else None,
            "profiles": self.registry.rows(),
            "limitations": [
                "Objectives are read from what the screen SHOWS; nothing here reads a "
                "game's quest log, memory or files.",
                "Only explicit completion evidence closes an objective: a declared message "
                "or a declared counter reaching its target. A vanished objective stays "
                "not_verifiable.",
                "One numeric counter per rule is understood; bars, checkpoints and stages "
                "are not measured by this build.",
                "No confidence is reported: no component measures a calibrated probability.",
            ],
        }

    def profiles(self) -> dict[str, Any]:
        return {
            "profiles": self.registry.rows(),
            "defaults": list(self.registry.ids),
            "rejected": self.registry.rejected,
        }
