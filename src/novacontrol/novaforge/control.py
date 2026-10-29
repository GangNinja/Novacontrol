"""NovaForge game-control interface (NF-19).

NF-07 established the *gated* gameplay action path: a proposal is digest-bound,
approved, and only then injected. This module is the typed CONTROL layer under
that gate — the vocabulary a caller uses to ask for an input, the bounded
operation that sends it, and the record of what actually happened.

One execution path, not two: :class:`GameControlService` composes the session's
existing :class:`~novacontrol.novaforge.actions.ActionRunner`, so every edge
still goes through the ONE :class:`~novacontrol.desktop.input.InputGuard` and
the same DPI-aware injector as the NF-07 path. Nothing here opens a window,
starts a process, or reaches a game's internals.

The distinction this module refuses to blur, in the phase's own words:

* **Requested** — a validated :class:`ControlAction`.
* **Accepted / running / dispatched** — the interface's live state
  (:attr:`ControlStatus.WAITING`, ``RUNNING``, ``DISPATCHED``).
* **Operation completed** — ``COMPLETED``: the input operation finished per the
  input interface's contract (a press went out, a bounded hold released, a wait
  elapsed).
* **Effect observed / verified** — *not this module*. ``ControlResult.effect`` is
  always ``None`` and the payload says so. A key that was sent is not a game
  that reacted; NF-21 owns that question.

Bounds are the interface's own, not a caller's promise: hold durations, wait
durations, deadlines, allowed keys/buttons and the one-action-at-a-time rule are
enforced here. A refused request is a ``REJECTED`` result with a code and a
readable reason, never an exception and never a partially sent action.

Coordinates are ABSOLUTE physical-pixel points inside the target's client area,
the same space NF-07's click and move use — the space the capture worker reports
window geometry in.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from novacontrol.desktop.input import (
    InputAbort,
    MouseButton,
    key_release_command,
    vk_for_key,
)

from .actions import (
    DEFAULT_ALLOWED_KEYS,
    ActionRunner,
    as_int,
    wait_bounded,
)

__all__ = [
    "CONTROL_KINDS",
    "CONTROL_SCHEMA_VERSION",
    "ControlAction",
    "ControlKind",
    "ControlPolicy",
    "ControlResult",
    "ControlStatus",
    "ControlTarget",
    "GameControlService",
]

CONTROL_SCHEMA_VERSION = "nf19.1"

#: Every bound the interface enforces, with one home each.
MAX_KEY_HOLD_MS = 10_000
MAX_MOUSE_HOLD_MS = 10_000
MAX_WAIT_MS = 30_000
MAX_DEADLINE_MS = 30_000
MAX_NOTE = 200


class ControlKind(StrEnum):
    """The input operations this build can perform.

    Deliberately small and edge-explicit: a press and a release are separate
    requests, so a caller can hold a key across several actions and the
    interface always knows exactly what it owns. Chords, text entry, scrolling
    and drags are NOT approximated by sequences — they are refused by name.
    """

    KEY_PRESS = "key_press"
    KEY_RELEASE = "key_release"
    KEY_HOLD = "key_hold"
    MOUSE_MOVE = "mouse_move"
    MOUSE_PRESS = "mouse_press"
    MOUSE_RELEASE = "mouse_release"
    MOUSE_HOLD = "mouse_hold"
    WAIT = "wait"
    #: Raise the session's own target window to the foreground. It exists
    #: because every other kind REQUIRES the target to be in front: without a
    #: way to put it there, a target that lost focus could never be acted on
    #: again, and the interface's safety check became a permanent refusal.
    FOCUS = "focus"


CONTROL_KINDS = frozenset(kind.value for kind in ControlKind)

_KEY_KINDS = frozenset({ControlKind.KEY_PRESS, ControlKind.KEY_RELEASE, ControlKind.KEY_HOLD})
_BUTTON_KINDS = frozenset(
    {ControlKind.MOUSE_PRESS, ControlKind.MOUSE_RELEASE, ControlKind.MOUSE_HOLD}
)
_HOLD_KINDS = frozenset({ControlKind.KEY_HOLD, ControlKind.MOUSE_HOLD})
_MOUSE_KINDS = frozenset(
    {
        ControlKind.MOUSE_MOVE,
        ControlKind.MOUSE_PRESS,
        ControlKind.MOUSE_RELEASE,
        ControlKind.MOUSE_HOLD,
    }
)
_PRESS_KINDS = frozenset({ControlKind.KEY_PRESS, ControlKind.MOUSE_PRESS})
_RELEASE_KINDS = frozenset({ControlKind.KEY_RELEASE, ControlKind.MOUSE_RELEASE})


class ControlStatus(StrEnum):
    """What became of one request — named for what actually happened.

    ``WAITING``/``RUNNING``/``DISPATCHED`` are LIVE states of an in-flight
    action (reported by :meth:`GameControlService.status`); a returned result is
    always terminal. ``COMPLETED`` means the input operation completed, never
    that the game reacted.
    """

    REJECTED = "rejected"
    WAITING = "waiting"
    RUNNING = "running"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    CLEANUP_FAILED = "cleanup_failed"


class Publish(Protocol):
    def __call__(self, event_type: str, payload: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class ControlPolicy:
    """The interface's bounds. One object, validated once, reported everywhere.

    Defaults are conservative on purpose: a 1.5 s key hold, an 800 ms mouse
    hold, a 2 s wait and a 4 s deadline. A caller that needs more changes the
    policy where the session is configured, not per request.
    """

    allowed_keys: frozenset[str] = DEFAULT_ALLOWED_KEYS
    allowed_buttons: frozenset[str] = frozenset({"left"})
    max_key_hold_ms: int = 1500
    max_mouse_hold_ms: int = 800
    max_wait_ms: int = 2000
    #: The whole operation's bound, used when an action names no deadline.
    action_deadline_ms: int = 4000
    #: A keyboard press only reaches the intended app when that app is focused.
    require_foreground: bool = True
    history_limit: int = 50

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not (1 <= self.max_key_hold_ms <= MAX_KEY_HOLD_MS):
            problems.append(f"max_key_hold_ms must be between 1 and {MAX_KEY_HOLD_MS}.")
        if not (1 <= self.max_mouse_hold_ms <= MAX_MOUSE_HOLD_MS):
            problems.append(f"max_mouse_hold_ms must be between 1 and {MAX_MOUSE_HOLD_MS}.")
        if not (1 <= self.max_wait_ms <= MAX_WAIT_MS):
            problems.append(f"max_wait_ms must be between 1 and {MAX_WAIT_MS}.")
        if not (1 <= self.action_deadline_ms <= MAX_DEADLINE_MS):
            problems.append(f"action_deadline_ms must be between 1 and {MAX_DEADLINE_MS}.")
        needed = max(self.max_key_hold_ms, self.max_mouse_hold_ms, self.max_wait_ms)
        if self.action_deadline_ms < needed:
            problems.append(
                "action_deadline_ms must cover the longest permitted duration "
                f"({needed} ms), or a permitted action could never finish inside its deadline."
            )
        if not self.allowed_keys:
            problems.append("At least one key must be allowed.")
        for key in sorted(self.allowed_keys):
            if vk_for_key(key) is None:
                problems.append(f"{key!r} is not a key this build knows.")
        if not self.allowed_buttons:
            problems.append("At least one mouse button must be allowed.")
        for button in sorted(self.allowed_buttons):
            try:
                MouseButton(button)
            except ValueError:
                problems.append(f"{button!r} is not a mouse button this build knows.")
        if not (1 <= self.history_limit <= 500):
            problems.append("history_limit must be between 1 and 500.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_keys": sorted(self.allowed_keys),
            "allowed_buttons": sorted(self.allowed_buttons),
            "max_key_hold_ms": self.max_key_hold_ms,
            "max_mouse_hold_ms": self.max_mouse_hold_ms,
            "max_wait_ms": self.max_wait_ms,
            "action_deadline_ms": self.action_deadline_ms,
            "require_foreground": self.require_foreground,
            "history_limit": self.history_limit,
        }


@dataclass(frozen=True, slots=True)
class ControlTarget:
    """The pinned identity a request is addressed to.

    ``handle`` + ``pid`` are the identity: a retitled window is still the same
    target (NF-07 learned that), and a different window behind the same title is
    not. ``process``/``start_time`` are recorded when the observation layer
    knows them; the observation loop revalidates the start time every tick, and
    a reused pid pauses the session before the control gate can allow anything.
    """

    session_id: str
    title: str = ""
    handle: int = 0
    pid: int = 0
    process: str = ""
    start_time: str = ""
    game: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "handle": self.handle,
            "pid": self.pid,
            "process": self.process,
            "start_time": self.start_time,
            "game": self.game,
        }

    @classmethod
    def from_window(
        cls,
        target: Any,
        session_id: str,
        *,
        game: str = "",
        process: str = "",
        start_time: str = "",
    ) -> "ControlTarget":
        """Read a session's pinned ``TargetWindow`` (duck-typed) into a target."""
        return cls(
            session_id=str(session_id),
            title=str(getattr(target, "title", "") or ""),
            handle=int(getattr(target, "handle", 0) or 0),
            pid=int(getattr(target, "pid", 0) or 0),
            process=str(process or ""),
            start_time=str(start_time or ""),
            game=str(game or ""),
        )

    def mismatches(self, geometry: Any) -> list[str]:
        """Every way the live window is not the target this request names."""
        problems: list[str] = []
        observed_handle = int(getattr(geometry, "handle", 0) or 0)
        observed_pid = int(getattr(geometry, "pid", 0) or 0)
        if self.handle and observed_handle != self.handle:
            problems.append(
                f"The pinned window handle changed (0x{self.handle:x} -> 0x{observed_handle:x})."
            )
        if self.pid and observed_pid and observed_pid != self.pid:
            problems.append(f"The pinned process changed (pid {self.pid} -> {observed_pid}).")
        return problems


@dataclass(frozen=True, slots=True)
class ControlAction:
    """One validated request. Identity and parameters are separate from sending."""

    kind: ControlKind
    session_id: str
    target: ControlTarget
    key: str = ""
    button: str = "left"
    x: int | None = None
    y: int | None = None
    duration_ms: int = 0
    deadline_ms: int = 0
    note: str = ""
    created_at: str = ""
    action_id: str = field(default_factory=lambda: uuid4().hex[:12])

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        session_id: str,
        target: ControlTarget,
        now: Callable[[], datetime] | None = None,
    ) -> "ControlAction":
        """Parse a request, refusing anything unrecognised.

        A missing kind is not "press enter": it is malformed. A ``target`` in
        the payload is ignored on purpose — the session's pinned window is the
        only destination a request may name, so a body cannot redirect input.
        """
        data = dict(payload or {})
        raw_kind = str(data.get("kind", "") or "").strip().lower()
        if raw_kind not in CONTROL_KINDS:
            raise ValueError(
                f"Unknown control kind {raw_kind or '(blank)'!r}; expected one of "
                f"{', '.join(sorted(CONTROL_KINDS))}."
            )
        stamp = now or (lambda: datetime.now(UTC))
        return cls(
            kind=ControlKind(raw_kind),
            session_id=str(session_id),
            target=target,
            key=str(data.get("key", "") or "").strip(),
            button=str(data.get("button", "left") or "left").strip().lower(),
            x=as_int(data.get("x"), "x"),
            y=as_int(data.get("y"), "y"),
            duration_ms=as_int(data.get("duration_ms"), "duration_ms") or 0,
            deadline_ms=as_int(data.get("deadline_ms"), "deadline_ms") or 0,
            note=str(data.get("note", "") or "")[:MAX_NOTE],
            created_at=str(data.get("created_at", "") or "") or stamp().isoformat(),
            action_id=str(data.get("action_id", "") or "") or uuid4().hex[:12],
        )

    def validate(self, policy: ControlPolicy) -> list[str]:
        """Every reason this request must not run. Empty means it may.

        Only parameters are checked here; whether the target is alive, focused
        and still the same window is a fact about the world, checked
        immediately before dispatch.
        """
        problems: list[str] = []
        if not self.session_id.strip():
            problems.append("A control action needs a session id.")
        if self.session_id != self.target.session_id:
            problems.append("The action's session does not match its target's session.")
        if len(self.session_id) > 80 or len(self.key) > 40:
            problems.append("A session id or key name is longer than this interface accepts.")

        if self.kind in _KEY_KINDS:
            if not self.key:
                problems.append(f"A {self.kind.value} action needs a key name.")
            else:
                canonical = key_release_command(self.key)
                if canonical is None:
                    problems.append(
                        f"{self.key!r} is not a key this build knows; refusing to guess a keycode."
                    )
                elif canonical not in policy.allowed_keys:
                    problems.append(
                        f"The {canonical!r} key is not in the control policy's allowed set."
                    )
            if self.x is not None or self.y is not None:
                problems.append("A key action does not take coordinates.")
        if self.kind in _BUTTON_KINDS:
            try:
                button = MouseButton(self.button)
            except ValueError:
                problems.append(
                    f"{self.button!r} is not a mouse button (expected left, right or middle)."
                )
            else:
                if button.value not in policy.allowed_buttons:
                    problems.append(
                        f"The {button.value!r} button is not in the control policy's allowed set."
                    )
            if self.key:
                problems.append("A mouse action does not take a key name.")

        if self.kind is ControlKind.MOUSE_MOVE:
            if self.x is None or self.y is None:
                problems.append("A mouse move needs both x and y coordinates.")
        elif self.kind in _MOUSE_KINDS and (self.x is None) != (self.y is None):
            problems.append("A mouse action takes both coordinates or neither.")
        if self.x is not None and self.x < 0:
            problems.append("Coordinates must be non-negative target-area points.")
        if self.y is not None and self.y < 0:
            problems.append("Coordinates must be non-negative target-area points.")

        if self.kind in _HOLD_KINDS:
            limit = (
                policy.max_key_hold_ms
                if self.kind is ControlKind.KEY_HOLD
                else policy.max_mouse_hold_ms
            )
            if not (1 <= self.duration_ms <= limit):
                problems.append(
                    f"A {self.kind.value} must last between 1 and {limit} ms "
                    f"(asked for {self.duration_ms} ms)."
                )
        elif self.kind is ControlKind.WAIT:
            if not (1 <= self.duration_ms <= policy.max_wait_ms):
                problems.append(
                    f"A wait must last between 1 and {policy.max_wait_ms} ms "
                    f"(asked for {self.duration_ms} ms)."
                )
            if self.key or self.x is not None or self.y is not None:
                problems.append("A wait takes no key and no coordinates.")
        elif self.duration_ms:
            problems.append(f"A {self.kind.value} action does not take a duration.")

        deadline = self.deadline_ms or policy.action_deadline_ms
        if not (1 <= deadline <= MAX_DEADLINE_MS):
            problems.append(f"deadline_ms must be between 1 and {MAX_DEADLINE_MS}.")
        elif self.duration_ms and self.duration_ms >= deadline:
            problems.append(
                f"deadline_ms ({deadline}) must exceed the action's duration "
                f"({self.duration_ms}), or the timeout would preempt it."
            )
        return problems

    def parameters(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "key": key_release_command(self.key) or self.key,
            "button": self.button,
            "x": self.x,
            "y": self.y,
            "duration_ms": self.duration_ms,
            "deadline_ms": self.deadline_ms,
            "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "session_id": self.session_id,
            "target": self.target.to_dict(),
            "created_at": self.created_at,
            **self.parameters(),
        }


@dataclass(slots=True)
class ControlResult:
    """What became of one request: the audit row AND the API answer.

    ``effect`` is always ``None``. Nothing in this phase observes whether the
    game reacted, so there is no field in which a success could be implied.
    """

    action_id: str
    session_id: str
    kind: str
    status: ControlStatus
    ok: bool
    code: str = ""
    reason: str = ""
    error: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    requested_at: str = ""
    started_at: str = ""
    dispatched_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    deadline_ms: int = 0
    cleanup: dict[str, Any] = field(default_factory=dict)
    effect: None = None
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "status": self.status.value,
            "ok": self.ok,
            "code": self.code,
            "reason": self.reason,
            "error": self.error,
            "target": dict(self.target),
            "parameters": dict(self.parameters),
            "validation": dict(self.validation),
            "requested_at": self.requested_at,
            "started_at": self.started_at,
            "dispatched_at": self.dispatched_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 3),
            "deadline_ms": self.deadline_ms,
            "cleanup": dict(self.cleanup),
            # Never a claim: the phase that verifies an effect is NF-21.
            "effect": None,
            "effect_note": (
                "Completion means the input operation finished; it does not mean the "
                "game reacted. No effect was observed or verified."
            ),
            "sequence": self.sequence,
        }


@dataclass(slots=True)
class GameControlService:
    """Bounded, cancellable input execution over the session's ONE runner.

    The service never creates an injector: ``runner`` is the manager's
    ``ActionRunner`` (and therefore the manager's ``InputGuard``), so a press
    made here is tracked, released and reported by the same guard every other
    gameplay input uses. ``runner=None`` is a truthful "no input path is wired"
    and every request is refused ``unsupported``.
    """

    policy: ControlPolicy
    runner: ActionRunner | None
    #: ``(session_id) -> live geometry`` (handle/pid/client/foreground), or None.
    read_target: Callable[[str], Awaitable[Any | None]]
    #: ``(session_id) -> {"allow": bool, "state": str, "reason": str, ...}``.
    #: The manager passes the Guardian's own decision function.
    gate: Callable[[str], Mapping[str, Any]]
    publish: Publish | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    _history: deque[ControlResult] = field(default_factory=deque, init=False, repr=False)
    _active: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _last_result: ControlResult | None = field(default=None, init=False, repr=False)
    _last_cleanup: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)
    _sequence: int = field(default=0, init=False, repr=False)
    #: Strong references to fire-and-forget announcements: a task with no
    #: reference can be collected before it runs, and a lost result event would
    #: be an observability gap the caller never sees.
    _announcing: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _counts: dict[str, int] = field(
        default_factory=lambda: {
            "received": 0,
            "rejected": 0,
            "completed": 0,
            "cancelled": 0,
            "timed_out": 0,
            "failed": 0,
            "cleanup_failed": 0,
            "dispatched": 0,
        },
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        problems = self.policy.errors()
        if problems:
            raise ValueError("Invalid control policy: " + "; ".join(problems))
        self._history = deque(maxlen=self.policy.history_limit)

    # ── read surface ───────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return self.runner is not None

    @property
    def active(self) -> bool:
        """Whether one action is in flight right now."""
        return self._running

    @property
    def active_session(self) -> str:
        """The session the in-flight action belongs to, or ``""`` when idle.

        One service serves every session of the manager, so a cancel or a read
        addressed to session A must not describe (or stop) session B's action.
        """
        if not self._running or self._active is None:
            return ""
        return str(self._active.get("session_id", "") or "")

    async def wait_idle(self, timeout: float = 2.0) -> bool:
        """Wait (bounded) for an in-flight action to unwind. True when idle.

        The session's close path asks an action to cancel and then waits here
        briefly, so the action releases its own input before the session's
        cleanup runs instead of being released underneath.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._running and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return not self._running

    def held(self) -> list[dict[str, str]]:
        """Inputs this interface's guard currently holds, kind and name."""
        if self.runner is None:
            return []
        return [{"kind": kind, "name": name} for kind, name in self.runner.guard.held_items()]

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "available": self.available,
            "stopped": self._stopped,
            "policy": self.policy.to_dict(),
            "active": dict(self._active) if self._active is not None else None,
            "held": self.held(),
            "counts": dict(self._counts),
            "last_result": (
                self._last_result.to_dict() if self._last_result is not None else None
            ),
            "last_cleanup": dict(self._last_cleanup),
            "history_limit": self.policy.history_limit,
            "kinds": sorted(CONTROL_KINDS),
            "limitations": [
                "Completion means the input operation finished, not that the game "
                "reacted; no effect is observed or verified in this phase.",
                "Only inputs this interface pressed are tracked and released; the "
                "platform cannot tell an injected key from a physical one.",
                "A request arriving while another action is in flight is refused "
                "as busy, never queued.",
                "Controller input is not implemented in this build.",
            ],
        }

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = list(self._history)[-max(1, min(int(limit), self.policy.history_limit)):]
        return [row.to_dict() for row in reversed(rows)]

    # ── cancellation / cleanup ─────────────────────────────────────────────
    async def cancel(self, reason: str = "user", *, session_id: str = "") -> dict[str, Any]:
        """Interrupt the in-flight action at its next checkpoint.

        Deliberately not locked: it must not queue behind the running action.
        A stop is one-shot and is re-armed after the action unwinds, so the next
        request in the session can still run.

        ``session_id`` scopes the request: one service serves every session, so
        a cancel addressed to a different (or closed) session is refused rather
        than stopping the live session's action.
        """
        active = self._active
        if self.runner is None or active is None or not self._running:
            return {
                "cancelled": False,
                "action_id": "",
                "reason": "No input action is in flight; nothing was cancelled.",
            }
        if session_id and str(active.get("session_id", "")) != session_id:
            return {
                "cancelled": False,
                "action_id": str(active.get("action_id", "")),
                "reason": (
                    "The in-flight action belongs to a different session; this cancel "
                    "was refused rather than stopping it."
                ),
            }
        self.runner.abort.stop()
        active["cancel_requested"] = True
        active["cancel_reason"] = str(reason or "user")
        return {
            "cancelled": True,
            "action_id": str(active.get("action_id", "")),
            "reason": (
                f"Cancel requested ({reason or 'user'}); the action unwinds at its next "
                "checkpoint and releases what it holds."
            ),
        }

    async def release(self, reason: str = "release") -> dict[str, Any]:
        """Release every input this interface holds; idempotent.

        Refused while an action is in flight: releasing underneath it would make
        its own release fail and misreport what was held. Cancel first.
        """
        if self.runner is None:
            return {
                "ok": True,
                "released": 0,
                "failed": [],
                "held_after": 0,
                "reason": reason,
                "note": "No input path is wired; nothing was held.",
            }
        if self._running:
            return {
                "ok": False,
                "released": 0,
                "failed": [],
                "held_after": None,
                "reason": "busy",
                "note": "An action is in flight; cancel it before releasing.",
            }
        cleanup = await self._release_inputs(reason)
        self._last_cleanup = cleanup
        return cleanup

    def adopt_cleanup(self, cleanup: Mapping[str, Any]) -> None:
        """Record a cleanup the session's own finalize performed.

        The manager releases the shared guard when a session closes; adopting
        that result keeps ``last_cleanup`` describing the same guard rather than
        a stale earlier action.
        """
        self._last_cleanup = dict(cleanup)

    async def shutdown(self) -> dict[str, Any]:
        """Stop accepting requests and release what is held (safe to repeat)."""
        self._stopped = True
        if self.runner is None:
            return {
                "ok": True,
                "released": 0,
                "held_after": 0,
                "failed": [],
                "note": "No input path.",
            }
        if self._running:
            self.runner.abort.stop()
            return {
                "ok": True,
                "released": 0,
                "held_after": None,
                "failed": [],
                "note": "An action is unwinding; it releases its own input on exit.",
            }
        cleanup = await self._release_inputs("shutdown")
        self._last_cleanup = cleanup
        return cleanup

    async def _release_inputs(self, reason: str) -> dict[str, Any]:
        if self.runner is None:
            return {"ok": True, "released": 0, "failed": [], "held_after": 0, "note": ""}
        guard = self.runner.guard
        before = guard.releases
        failures_before = len(guard.release_failures)
        await guard.release_all()
        failed = guard.release_failures[failures_before:]
        held = guard.held_items()
        return {
            "ok": not failed and not held,
            "released": max(0, guard.releases - before),
            "failed": list(failed),
            "held_after": len(held),
            "reason": reason,
            "note": (
                "Every press this interface made has a matching release."
                if not failed and not held
                else (
                    f"{len(held)} input(s) are still held; release failures: "
                    f"{'; '.join(failed) or 'none recorded'}"
                )
            ),
        }

    # ── execution ──────────────────────────────────────────────────────────
    async def execute(self, action: ControlAction) -> ControlResult:
        """Validate, verify the target, run one bounded action, report the truth."""
        self._sequence += 1
        started = time.monotonic()
        if not isinstance(action, ControlAction):
            result = ControlResult(
                action_id="",
                session_id="",
                kind="",
                status=ControlStatus.REJECTED,
                ok=False,
                requested_at=self._stamp(),
                sequence=self._sequence,
            )
            self._counts["received"] += 1
            return self._finish(
                result,
                "invalid_request",
                "The object handed in is not a control action.",
                started,
            )

        result = ControlResult(
            action_id=action.action_id,
            session_id=action.session_id,
            kind=action.kind.value,
            status=ControlStatus.REJECTED,
            ok=False,
            requested_at=self._stamp(),
            sequence=self._sequence,
            deadline_ms=action.deadline_ms or self.policy.action_deadline_ms,
        )
        result.target = action.target.to_dict()
        result.parameters = action.parameters()
        self._counts["received"] += 1
        if self._stopped:
            return self._finish(result, "service_stopped", "The control interface is shut down.", started)
        problems = action.validate(self.policy)
        result.validation = {
            "ok": not problems,
            "problems": problems,
            "policy": self.policy.to_dict(),
        }
        if problems:
            return self._finish(result, "policy", " ".join(problems), started)
        if self.runner is None:
            return self._finish(
                result,
                "unsupported",
                "No gameplay input path is wired to this session, so nothing can be injected.",
                started,
            )
        if self._running:
            return self._finish(
                result,
                "busy",
                "Another control action is in flight; this request was refused rather than queued.",
                started,
            )

        self._running = True
        self._active = {
            "action_id": result.action_id,
            "session_id": result.session_id,
            "kind": result.kind,
            "status": ControlStatus.WAITING.value,
            "requested_at": result.requested_at,
            "deadline_ms": result.deadline_ms,
            "target": result.target,
            "cancel_requested": False,
        }
        guard = self.runner.guard
        failures_before = len(guard.release_failures)
        try:
            decision = dict(self.gate(action.session_id) or {})
            if not decision.get("allow"):
                return self._finish(
                    result,
                    str(decision.get("state") or "denied"),
                    str(decision.get("reason") or "The session gate refused this action."),
                    started,
                )
            geometry = await self.read_target(action.session_id)
            if geometry is None:
                return self._finish(
                    result,
                    "target_lost",
                    "The target window could not be read immediately before dispatch; "
                    "nothing was sent.",
                    started,
                )
            mismatches = action.target.mismatches(geometry)
            if mismatches:
                return self._finish(
                    result,
                    "target_changed",
                    " ".join(mismatches) + " Nothing was sent.",
                    started,
                )
            if action.kind is not ControlKind.FOCUS:
                if bool(getattr(geometry, "minimized", False)):
                    return self._finish(
                        result,
                        "target_minimized",
                        "The target window is minimized; input is refused until it is restored.",
                        started,
                    )
                if not bool(getattr(geometry, "visible", True)):
                    return self._finish(
                        result,
                        "target_hidden",
                        "The target window is not visible; input is refused.",
                        started,
                    )
                if self.policy.require_foreground and not bool(
                    getattr(geometry, "foreground", False)
                ):
                    return self._finish(
                        result,
                        "not_foreground",
                        "The target is not the foreground window; input would land elsewhere.",
                        started,
                    )
            if action.x is not None and action.y is not None and hasattr(geometry, "contains"):
                if not geometry.contains(action.x, action.y, client=True):
                    bounds = (
                        geometry.client_bounds()
                        if callable(getattr(geometry, "client_bounds", None))
                        else ""
                    )
                    return self._finish(
                        result,
                        "outside_target",
                        f"Point ({action.x}, {action.y}) is outside the target's client area "
                        f"{bounds}; nothing was sent.",
                        started,
                    )
            refusal = self._edge_conflict(action)
            if refusal is not None:
                return self._finish(result, refusal[0], refusal[1], started)

            result.started_at = self._stamp()
            self._active["status"] = ControlStatus.RUNNING.value
            self._active["started_at"] = result.started_at
            try:
                async with asyncio.timeout(result.deadline_ms / 1000.0):
                    await self._dispatch(action, result)
            except InputAbort as exc:
                result.status = ControlStatus.CANCELLED
                result.reason = (
                    f"Cancelled: {exc}" if str(exc) else "The action was cancelled."
                )
            except TimeoutError:
                result.status = ControlStatus.TIMED_OUT
                result.reason = (
                    f"The action did not finish inside its {result.deadline_ms} ms deadline; "
                    "it was stopped and its input released."
                )
            except asyncio.CancelledError:
                cleanup = await self._release_inputs("cancelled")
                result.cleanup = cleanup
                result.status = (
                    ControlStatus.CLEANUP_FAILED
                    if cleanup.get("failed") or cleanup.get("held_after")
                    else ControlStatus.CANCELLED
                )
                self._finish(result, "cancelled", "The caller's task was cancelled.", started)
                raise
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                result.status = ControlStatus.FAILED
                result.error = f"{type(exc).__name__}: {exc}"
                result.reason = f"The input operation failed: {result.error}"
            else:
                result.status = ControlStatus.COMPLETED
                result.reason = self._completed_reason(action)

            if result.status in {
                ControlStatus.CANCELLED,
                ControlStatus.TIMED_OUT,
                ControlStatus.FAILED,
            }:
                cleanup = await self._release_inputs(result.status.value)
                result.cleanup = cleanup
                if not cleanup.get("ok"):
                    result.status = ControlStatus.CLEANUP_FAILED
                    result.reason = (
                        result.reason + " " + str(cleanup.get("note", ""))
                    ).strip()
            else:
                result.cleanup = self._cleanup_snapshot(failures_before)
                if not result.cleanup.get("ok"):
                    result.status = ControlStatus.CLEANUP_FAILED
                    result.reason = (
                        result.reason + " " + str(result.cleanup.get("note", ""))
                    ).strip()
            return self._finish(result, "", result.reason, started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a probe/gate bug is an honest failure
            result.status = ControlStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            result.reason = f"The control interface could not complete the action: {result.error}"
            return self._finish(result, "interface_error", result.reason, started)
        finally:
            self._running = False
            self._active = None
            # A cancel is one-shot about the action it interrupted; the next
            # request in the same session must still be able to run.
            if self.runner is not None and self.runner.abort.stopped:
                self.runner.reset_abort()

    async def _dispatch(self, action: ControlAction, result: ControlResult) -> None:
        """Perform the input edges. The caller owns the deadline and cleanup."""
        runner = self.runner
        assert runner is not None
        guard = runner.guard
        kind = action.kind
        if kind is ControlKind.KEY_PRESS:
            await guard.press_key(action.key)
            await self._note_dispatch(action, result)
        elif kind is ControlKind.KEY_RELEASE:
            await guard.release_key(action.key)
        elif kind is ControlKind.KEY_HOLD:
            await guard.press_key(action.key)
            await self._note_dispatch(action, result)
            await wait_bounded(action.duration_ms, runner.abort)
            await guard.release_key(action.key)
        elif kind is ControlKind.MOUSE_MOVE:
            assert action.x is not None and action.y is not None
            await runner.input.move(action.x, action.y)
            await self._note_dispatch(action, result)
        elif kind is ControlKind.MOUSE_PRESS:
            if action.x is not None and action.y is not None:
                await runner.input.move(action.x, action.y)
            await guard.press_button(action.button)
            await self._note_dispatch(action, result)
        elif kind is ControlKind.MOUSE_RELEASE:
            await guard.release_button(action.button)
        elif kind is ControlKind.MOUSE_HOLD:
            if action.x is not None and action.y is not None:
                await runner.input.move(action.x, action.y)
            await guard.press_button(action.button)
            await self._note_dispatch(action, result)
            await wait_bounded(action.duration_ms, runner.abort)
            await guard.release_button(action.button)
        elif kind is ControlKind.WAIT:
            await wait_bounded(action.duration_ms, runner.abort)
        elif kind is ControlKind.FOCUS:
            # The worker raises the window AND verifies GetForegroundWindow, so a
            # completed focus is the precondition of every other kind, measured.
            await runner.input.focus(action.target.handle)
            await self._note_dispatch(action, result)
        else:  # pragma: no cover - ControlKind is closed and validated first
            raise ValueError(f"Unsupported control kind: {kind!r}.")
        # A stop request that arrived DURING the input edge (a slow press, a
        # cursor move) must still end the action as cancelled: the edge itself
        # has no checkpoint, and reporting `completed` would hide that the
        # operator asked to stop. The caller's cleanup then releases whatever
        # this action managed to press.
        runner.abort.check("finishing the input operation")

    async def _note_dispatch(self, action: ControlAction, result: ControlResult) -> None:
        """Record the moment input left the process (once per action)."""
        if result.dispatched_at:
            return
        result.dispatched_at = self._stamp()
        if self._active is not None:
            self._active["status"] = ControlStatus.DISPATCHED.value
            self._active["dispatched_at"] = result.dispatched_at
        await self._publish(
            "novaforge.control.dispatched",
            {
                "action_id": result.action_id,
                "session_id": result.session_id,
                "kind": result.kind,
                "parameters": dict(result.parameters),
                "target": dict(result.target),
                "at": result.dispatched_at,
            },
        )

    def _edge_conflict(self, action: ControlAction) -> tuple[str, str] | None:
        """Refuse an edge that would corrupt the tracked input state."""
        if self.runner is None:
            return None
        guard = self.runner.guard
        held = set(guard.held_items())
        if action.kind is ControlKind.KEY_PRESS:
            canonical = key_release_command(action.key) or action.key
            if ("key", canonical) in held:
                return (
                    "already_held",
                    f"The {canonical!r} key is already held by this interface; "
                    "release it before pressing it again.",
                )
        elif action.kind is ControlKind.KEY_RELEASE:
            canonical = key_release_command(action.key) or action.key
            if ("key", canonical) not in held:
                return (
                    "not_held",
                    f"The {canonical!r} key is not held by this interface; "
                    "there is nothing to release.",
                )
        elif action.kind is ControlKind.MOUSE_PRESS:
            try:
                button = MouseButton(action.button).value
            except ValueError:
                return None
            if ("button", button) in held:
                return (
                    "already_held",
                    f"The {button!r} mouse button is already held by this interface.",
                )
        elif action.kind is ControlKind.MOUSE_RELEASE:
            try:
                button = MouseButton(action.button).value
            except ValueError:
                return None
            if ("button", button) not in held:
                return (
                    "not_held",
                    f"The {button!r} mouse button is not held by this interface; "
                    "there is nothing to release.",
                )
        return None

    def _completed_reason(self, action: ControlAction) -> str:
        if action.kind is ControlKind.WAIT:
            return "The bounded wait elapsed; no input was sent."
        if action.kind is ControlKind.FOCUS:
            return (
                "The target window was raised to the foreground and the worker "
                "verified it is the foreground window; no gameplay input was sent."
            )
        if action.kind in _PRESS_KINDS:
            return (
                "The press was sent and the input is held by this interface until a "
                "release, a cancel or the session's cleanup. The game's reaction was "
                "not observed."
            )
        if action.kind in _RELEASE_KINDS:
            return "The release was sent; this interface no longer holds that input."
        return "The input operation completed. The game's reaction was not observed."

    def _cleanup_snapshot(self, failures_before: int) -> dict[str, Any]:
        """Cleanup as THIS action left it: intentionally held inputs are named."""
        if self.runner is None:
            return {"ok": True, "released": 0, "failed": [], "held_after": 0, "note": ""}
        guard = self.runner.guard
        held = guard.held_items()
        failed = guard.release_failures[failures_before:]
        note = "No input left held by this action."
        if failed:
            note = "A release failed: " + "; ".join(failed[-3:])
        elif held:
            note = (
                "Held on purpose by this interface: "
                + ", ".join(f"{kind}:{name}" for kind, name in held)
                + ". A release action, a cancel or the session's cleanup frees it."
            )
        return {
            "ok": not failed,
            "released": 0,
            "failed": list(failed),
            "held_after": len(held),
            "held_items": [{"kind": kind, "name": name} for kind, name in held],
            "note": note,
        }

    def _finish(
        self,
        result: ControlResult,
        code: str,
        reason: str,
        started: float | None = None,
    ) -> ControlResult:
        """The ONE exit: clock it, count it, keep it (bounded), announce it."""
        if reason and not result.reason:
            result.reason = reason
        if code and not result.code:
            result.code = code
        if not result.finished_at:
            result.finished_at = self._stamp()
        if started is not None:
            result.duration_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        result.ok = result.status is ControlStatus.COMPLETED
        if not result.code:
            result.code = {
                ControlStatus.REJECTED: "rejected",
                ControlStatus.CANCELLED: "cancelled",
                ControlStatus.TIMED_OUT: "timed_out",
                ControlStatus.FAILED: "dispatch_failed",
                ControlStatus.CLEANUP_FAILED: "cleanup_failed",
            }.get(result.status, "")
        if result.dispatched_at:
            self._counts["dispatched"] += 1
        key = {
            ControlStatus.REJECTED: "rejected",
            ControlStatus.COMPLETED: "completed",
            ControlStatus.CANCELLED: "cancelled",
            ControlStatus.TIMED_OUT: "timed_out",
            ControlStatus.FAILED: "failed",
            ControlStatus.CLEANUP_FAILED: "cleanup_failed",
        }.get(result.status)
        if key:
            self._counts[key] += 1
        self._history.append(result)
        self._last_result = result
        self._publish_now(result)
        return result

    def _publish_now(self, result: ControlResult) -> None:
        """Fire-and-forget announcement; a lost event cannot fail the action."""
        if self.publish is None:
            return
        payload = {
            "action_id": result.action_id,
            "session_id": result.session_id,
            "kind": result.kind,
            "status": result.status.value,
            "code": result.code,
            "reason": result.reason,
            "target": dict(result.target),
            "dispatched": bool(result.dispatched_at),
            "cleanup": dict(result.cleanup),
            "elapsed_ms": round(result.duration_ms, 3),
        }
        try:
            outcome = self.publish("novaforge.control.result", payload)
            if asyncio.iscoroutine(outcome):
                task = asyncio.ensure_future(outcome)
                self._announcing.add(task)

                def _consume(done: "asyncio.Future[Any]") -> None:
                    self._announcing.discard(done)
                    with contextlib.suppress(Exception):
                        done.exception()

                task.add_done_callback(_consume)
        except Exception:  # noqa: BLE001 - the announcement is not the work
            return

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            outcome = self.publish(event_type, payload)
            if asyncio.iscoroutine(outcome):
                await outcome
        except Exception:  # noqa: BLE001 - a lost event cannot fail an action
            return

    def _stamp(self) -> str:
        try:
            return self.clock().isoformat()
        except Exception:  # noqa: BLE001 - a failing clock is still a timestamp gap
            return datetime.now(UTC).isoformat()
