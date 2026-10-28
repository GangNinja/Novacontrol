"""NovaForge gameplay actions (readiness remediation R1).

The verification audit's first blocker was ``INPUT_PATH_AVAILABLE = False``:
NovaForge could observe a window but never act on it, so every "action budget"
it stored was a number nobody could enforce and every "executed" stage was a
placeholder. This module is the missing half, and it is built from the safety
primitives that already existed rather than a second input stack:

* :class:`~novacontrol.desktop.input.InputGuard` owns the press/release
  invariant. Nothing here sends an edge without going through it, so a stuck
  key cannot outlive an exception, a cancellation, a timeout or a stop.
* :class:`~novacontrol.desktop.input.WindowGeometry` /
  ``validate_input_target`` own the geometry rules: client-area coordinates only,
  and the identity (handle + pid) that was pinned at validate time.
* :class:`ScreenInput` speaks to the SAME DPI-aware capture worker that produces
  the frames, so a coordinate read off a screenshot and a click injected here are
  in one physical-pixel space.

Three distinctions this module refuses to blur:

* **A proposal is not an action.** ``plan`` returns a digest-bound proposal and
  executes nothing. ``execute`` requires that exact digest, re-validates the
  target against the live window, and only then injects.
* **A refusal is not a failure.** A denied action (policy, budget, guardian,
  stale target) records ``denied`` and never counts against the action budget.
* **An execution is not a success.** ``executed`` means the input was sent; the
  objective is decided by :mod:`novacontrol.novaforge.objective`, independently.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from novacontrol.desktop.input import (
    InputAbort,
    InputGuard,
    InputStop,
    MouseButton,
    WindowGeometry,
    key_release_command,
    mouse_flags,
    vk_for_key,
)

__all__ = [
    "ACTION_KINDS",
    "ActionInjector",
    "ActionKind",
    "ActionOutcome",
    "ActionPolicy",
    "ActionProposal",
    "ActionRunner",
    "ActionStatus",
    "DEFAULT_ALLOWED_KEYS",
    "GameplayAction",
    "ScreenInput",
    "action_digest",
    "as_int",
    "wait_bounded",
]


class ActionKind(StrEnum):
    """The four actions this build can send.

    Deliberately small. A game agent needs a key tap, a held key, a click and a
    cursor move; anything else (text entry, scrolling, drags, chords) would need
    a policy decision nobody has made yet, so it is refused by name rather than
    approximated by a sequence of the four.
    """

    KEY = "key"
    HOLD = "hold"
    CLICK = "click"
    MOVE = "move"


ACTION_KINDS = frozenset(kind.value for kind in ActionKind)

#: Keys a gameplay action may name. The guard knows more keys than a game
#: session is allowed to press: system keys (alt+f4, ctrl+esc, the windows key)
#: are exactly how "send a keystroke to the game" becomes "close the game" or
#: "open the Start menu over it". Extending this set is a policy decision, and it
#: is made here, once.
DEFAULT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "up", "down", "left", "right",
        "space", "enter", "return", "esc", "escape", "tab",
        "w", "a", "s", "d", "q", "e", "f", "g", "h", "j", "k", "l",
        "i", "m", "n", "o", "p", "r", "t", "u", "v", "x", "y", "z", "b", "c",
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
        "shift", "ctrl", "control",
    }
)


class ActionStatus(StrEnum):
    """Every ending an action can have, named for what actually happened."""

    PROPOSED = "proposed"
    DENIED = "denied"
    EXECUTED = "executed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """The bounds an action must satisfy before anything is injected.

    Defaults are deliberately tight: a short click, a hold under two seconds, a
    bounded step count and only the left mouse button. A caller that needs more
    changes the policy where the session is configured, so "what may this build
    send" is one readable object rather than a rule spread across call sites.
    """

    allowed_keys: frozenset[str] = DEFAULT_ALLOWED_KEYS
    allowed_buttons: frozenset[str] = frozenset({"left"})
    max_hold_ms: int = 2000
    max_duration_ms: int = 5000
    max_steps: int = 8
    #: A keyboard press only reaches the intended app when that app is focused.
    #: Sessions require it by default; a test may relax it for a harness window.
    require_foreground: bool = True
    #: How long a synthesized tap holds the key down. Long enough for the target
    #: to see it, short enough that a dropped release is not a stuck input.
    tap_ms: int = 60

    MAX_HOLD_MS = 10_000
    MAX_DURATION_MS = 30_000

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not (1 <= self.max_hold_ms <= self.MAX_HOLD_MS):
            problems.append(f"max_hold_ms must be between 1 and {self.MAX_HOLD_MS}.")
        if not (1 <= self.max_duration_ms <= self.MAX_DURATION_MS):
            problems.append(f"max_duration_ms must be between 1 and {self.MAX_DURATION_MS}.")
        if self.max_duration_ms < self.max_hold_ms:
            problems.append("max_duration_ms must be at least max_hold_ms.")
        if not (1 <= self.tap_ms <= self.max_hold_ms):
            problems.append("tap_ms must be between 1 and max_hold_ms.")
        if not self.allowed_buttons:
            problems.append("At least one mouse button must be allowed.")
        if not (1 <= self.max_steps <= 64):
            problems.append("max_steps must be between 1 and 64.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_keys": sorted(self.allowed_keys),
            "allowed_buttons": sorted(self.allowed_buttons),
            "max_hold_ms": self.max_hold_ms,
            "max_duration_ms": self.max_duration_ms,
            "max_steps": self.max_steps,
            "require_foreground": self.require_foreground,
            "tap_ms": self.tap_ms,
        }


def as_int(value: Any, label: str) -> int | None:
    """A strict integer read: ``"12abc"`` is not 12, and ``True`` is not 1.

    Public because the NF-19 control model parses the same request fields and
    must not grow a second, subtly different number parser.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number.")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{label} must be a whole number.")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    raise ValueError(f"{label} must be a whole number.")


@dataclass(frozen=True, slots=True)
class GameplayAction:
    """One typed, bounded thing to send. Validation is separate from sending."""

    kind: ActionKind
    key: str = ""
    button: str = "left"
    x: int | None = None
    y: int | None = None
    duration_ms: int = 60
    note: str = ""
    action_id: str = field(default_factory=lambda: uuid4().hex[:12])

    MAX_NOTE = 200

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "GameplayAction":
        """Read a request into an action, refusing anything unrecognised.

        Raises :class:`ValueError` rather than defaulting: a missing ``kind`` is
        not "press enter", it is a malformed request.
        """
        data = dict(payload or {})
        raw_kind = str(data.get("kind", "") or "").strip().lower()
        if raw_kind not in ACTION_KINDS:
            raise ValueError(
                f"Unknown action kind {raw_kind or '(blank)'!r}; expected one of "
                f"{', '.join(sorted(ACTION_KINDS))}."
            )
        duration = as_int(data.get("duration_ms"), "duration_ms")
        return cls(
            kind=ActionKind(raw_kind),
            key=str(data.get("key", "") or ""),
            button=str(data.get("button", "left") or "left").strip().lower(),
            x=as_int(data.get("x"), "x"),
            y=as_int(data.get("y"), "y"),
            duration_ms=60 if duration is None else duration,
            note=str(data.get("note", "") or "")[: cls.MAX_NOTE],
            action_id=str(data.get("action_id", "") or uuid4().hex[:12]),
        )

    def validate(self, policy: ActionPolicy) -> list[str]:
        """Every reason this action must not run. Empty means it may.

        The checks are ordered so the message names the first real problem: a
        click with no coordinates is reported as such, not as "outside the
        window" (which it also is, but nobody can act on that).
        """
        problems: list[str] = []
        if self.kind in {ActionKind.KEY, ActionKind.HOLD}:
            canonical = key_release_command(self.key)
            if canonical is None:
                problems.append(
                    f"{self.key!r} is not a key this build knows; refusing to guess a keycode."
                )
            elif vk_for_key(self.key) is None:
                problems.append(f"{self.key!r} has no virtual-key code; refusing to send it.")
            elif canonical not in policy.allowed_keys:
                problems.append(
                    f"The {canonical!r} key is not in the gameplay policy's allowed set."
                )
        if self.kind is ActionKind.HOLD:
            if not (1 <= self.duration_ms <= policy.max_hold_ms):
                problems.append(
                    f"A hold must last between 1 and {policy.max_hold_ms} ms "
                    f"(asked for {self.duration_ms} ms)."
                )
        elif self.kind is ActionKind.KEY:
            if not (1 <= self.duration_ms <= policy.max_hold_ms):
                problems.append(
                    f"A key tap must last between 1 and {policy.max_hold_ms} ms "
                    f"(asked for {self.duration_ms} ms)."
                )
        if self.kind in {ActionKind.CLICK, ActionKind.MOVE}:
            if self.x is None or self.y is None:
                problems.append(f"A {self.kind.value} needs both x and y coordinates.")
            elif self.x < 0 or self.y < 0:
                problems.append("Coordinates must be non-negative client-area points.")
        if self.kind is ActionKind.CLICK and self.button not in policy.allowed_buttons:
            problems.append(
                f"The {self.button!r} button is not in the gameplay policy's allowed set."
            )
        if self.duration_ms > policy.max_duration_ms:
            problems.append(
                f"The action duration exceeds the policy's {policy.max_duration_ms} ms bound."
            )
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "key": key_release_command(self.key) or self.key,
            "button": self.button,
            "x": self.x,
            "y": self.y,
            "duration_ms": self.duration_ms,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "GameplayAction | None":
        """Read a persisted action back; None when the record is not readable."""
        try:
            return cls.from_payload(dict(payload or {}))
        except ValueError:
            return None


def action_digest(session_id: str, action: GameplayAction, target: Any) -> str:
    """The digest an approval is bound to.

    Covers the session, the action AND the target identity, so an approval for
    "press W in window 0x1234 pid 99" cannot be replayed against the same title
    in a different process. Canonical JSON, sorted keys, no whitespace.
    """
    body = {
        "session_id": session_id,
        "action": action.to_dict(),
        "target": {
            "handle": getattr(target, "handle", None),
            "pid": getattr(target, "pid", None),
            "title": getattr(target, "title", ""),
        },
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """A planned action plus the digest an execution must echo back."""

    proposal_id: str
    session_id: str
    action: GameplayAction
    digest: str
    created_at: str
    target: dict[str, Any]
    policy: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "session_id": self.session_id,
            "action": self.action.to_dict(),
            "digest": self.digest,
            "created_at": self.created_at,
            "target": dict(self.target),
            "policy": dict(self.policy),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ActionProposal | None":
        data = dict(payload or {})
        action = GameplayAction.from_dict(data.get("action"))
        if action is None:
            return None
        proposal_id = str(data.get("proposal_id", "") or "")
        digest = str(data.get("digest", "") or "")
        if not proposal_id or not digest:
            return None
        return cls(
            proposal_id=proposal_id,
            session_id=str(data.get("session_id", "") or ""),
            action=action,
            digest=digest,
            created_at=str(data.get("created_at", "") or ""),
            target=dict(data.get("target") or {}),
            policy=dict(data.get("policy") or {}),
        )


@dataclass(slots=True)
class ActionOutcome:
    """What became of one proposal: the audit row AND the API answer."""

    status: ActionStatus
    ok: bool
    reason: str = ""
    action: dict[str, Any] = field(default_factory=dict)
    proposal_id: str = ""
    digest: str = ""
    steps: int = 0
    presses: int = 0
    releases: int = 0
    held_after: int = 0
    elapsed_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "ok": self.ok,
            "reason": self.reason,
            "action": dict(self.action),
            "proposal_id": self.proposal_id,
            "digest": self.digest,
            "steps": self.steps,
            "presses": self.presses,
            "releases": self.releases,
            "held_after": self.held_after,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "error": self.error,
        }


@dataclass(slots=True)
class ScreenInput:
    """Injects through the DPI-aware capture worker, one command at a time.

    ``send`` is the worker's ``send_input`` seam (``CaptureResult``-like). A
    failed command raises, because the guard must not record a press that never
    left the process — an unreleased-but-recorded key and an unrecorded-but-held
    key are both lies about the same state.
    """

    send: Callable[[str], Awaitable[Any]]

    async def _command(self, command: str) -> None:
        result = await self.send(command)
        ok = getattr(result, "ok", True)
        if not ok:
            raise RuntimeError(str(getattr(result, "error", "") or f"input command failed: {command}"))

    @staticmethod
    def _vk(name: str) -> int:
        code = vk_for_key(name)
        if code is None:
            raise ValueError(f"No virtual-key code for {name!r}.")
        return code

    async def press(self, kind: str, name: str) -> None:
        if kind == "key":
            await self._command(f"key {self._vk(name)} 1")
        elif kind == "button":
            await self._command(f"button {mouse_flags(MouseButton(name), down=True)}")
        else:
            raise ValueError(f"Unknown input kind {kind!r}.")

    async def release(self, kind: str, name: str) -> None:
        if kind == "key":
            await self._command(f"key {self._vk(name)} 0")
        elif kind == "button":
            await self._command(f"button {mouse_flags(MouseButton(name), down=False)}")
        else:
            raise ValueError(f"Unknown input kind {kind!r}.")

    async def move(self, x: int, y: int) -> None:
        await self._command(f"move {x} {y}")


async def wait_bounded(ms: int, abort: InputStop) -> None:
    """Sleep ``ms`` in small slices so a stop request interrupts a hold.

    One long ``asyncio.sleep`` would keep a held key down for the whole
    duration after the operator asked to stop, which is the exact failure the
    guard exists to prevent. Shared with NF-19's control interface for the same
    reason: there is one interruptible wait in this build.
    """
    remaining = max(0.0, ms / 1000.0)
    while remaining > 0:
        abort.check("waiting out an action duration")
        slice_seconds = min(0.02, remaining)
        await asyncio.sleep(slice_seconds)
        remaining -= slice_seconds


class ActionInjector(Protocol):
    """The three edges a runner needs. No policy, no state: just injection."""

    async def press(self, kind: str, name: str) -> None: ...

    async def release(self, kind: str, name: str) -> None: ...

    async def move(self, x: int, y: int) -> None: ...


@dataclass(slots=True)
class ActionRunner:
    """Sends one validated action, and releases whatever it held — always.

    ONE guard lives for the runner's lifetime, so its press/release counters
    describe the whole session rather than the last action, and a release that
    a crashed action could not complete is still retried by the session's own
    cleanup (``guard.release_all()``) instead of being forgotten with the run.
    """

    input: ActionInjector
    abort: InputStop = field(default_factory=InputStop)
    guard: InputGuard = field(init=False)

    def __post_init__(self) -> None:
        self.guard = InputGuard(press=self.input.press, release=self.input.release, abort=self.abort)

    async def release_all(self) -> tuple[int, int]:
        """Release anything still held, and report the session's totals."""
        await self.guard.release_all()
        return (self.guard.presses, self.guard.releases)

    def begin_session(self) -> None:
        """Arm the runner for a NEW session: clear the stop AND the totals.

        One runner serves every session of the manager, so the press/release
        totals a session reports in its input cleanup must not carry an earlier
        session's presses — a session that tapped once reported "2 / 2" once
        because the numbers were lifetime totals wearing a per-session label.
        The guard's counters are cleared instead of the guard being replaced, so
        the release invariant (and its recorded failures) survive.
        """
        self.reset_abort()
        self.guard.presses = 0
        self.guard.releases = 0
        self.guard.release_failures.clear()

    def reset_abort(self) -> None:
        """Arm the runner for its next action after a stop request.

        A stop is a ONE-SHOT request about an in-flight action, not a permanent
        state of the runner: after the aborted action has unwound and released
        what it held, the next action must be able to run. The guard captured
        the old token at construction, so it has to be re-pointed too —
        replacing only ``self.abort`` would leave the guard aborting forever.
        """
        self.abort = InputStop()
        self.guard.abort = self.abort

    async def run(self, action: GameplayAction, geometry: WindowGeometry, policy: ActionPolicy) -> ActionOutcome:
        started = time.monotonic()
        steps = 0
        guard = self.guard
        error = ""
        aborted = False
        # The guard lives for the whole session, so the counters and failures
        # that describe THIS action are the ones added after it began.
        presses_before, releases_before = guard.presses, guard.releases
        failures_before = len(guard.release_failures)
        try:
            async with guard:
                self.abort.check("starting an action")
                if action.kind in {ActionKind.MOVE, ActionKind.CLICK}:
                    assert action.x is not None and action.y is not None
                    # Geometry is re-checked here, immediately before injection:
                    # the window may have moved between planning and executing.
                    if not geometry.contains(action.x, action.y, client=True):
                        raise ValueError(
                            f"Point ({action.x}, {action.y}) is outside the target's client area "
                            f"{geometry.client_bounds()} — refusing to click into empty space."
                        )
                    await self.input.move(action.x, action.y)
                    steps += 1
                if action.kind is ActionKind.CLICK:
                    await wait_bounded(policy.tap_ms, self.abort)
                    await guard.press_button(action.button)
                    steps += 1
                    await wait_bounded(policy.tap_ms, self.abort)
                    await guard.release_button(action.button)
                    steps += 1
                elif action.kind is ActionKind.KEY:
                    await guard.press_key(action.key)
                    steps += 1
                    await wait_bounded(min(action.duration_ms, policy.max_duration_ms), self.abort)
                    await guard.release_key(action.key)
                    steps += 1
                elif action.kind is ActionKind.HOLD:
                    await guard.press_key(action.key)
                    steps += 1
                    await wait_bounded(min(action.duration_ms, policy.max_hold_ms), self.abort)
                    await guard.release_key(action.key)
                    steps += 1
        except InputAbort as exc:
            aborted = True
            error = str(exc)
        except asyncio.CancelledError:
            # The guard's __aexit__ already released; the cancellation is not an
            # action failure and must not be swallowed.
            raise
        except Exception as exc:  # noqa: BLE001 - reported as the outcome
            error = f"{type(exc).__name__}: {exc}"

        held_after = len(guard.held)
        run_failures = guard.release_failures[failures_before:]
        held_note = ""
        if held_after or run_failures:
            held_note = (
                f"{held_after} input(s) still held; release failures: "
                f"{'; '.join(run_failures) or 'none recorded'}"
            )
        ok = not error and not held_after and not run_failures
        status = (
            ActionStatus.EXECUTED if ok
            else ActionStatus.ABORTED if aborted
            else ActionStatus.FAILED
        )
        if run_failures and not error:
            error = "The action sent, but a release failed: " + "; ".join(run_failures[-3:])
        return ActionOutcome(
            status=status,
            ok=ok,
            reason=error or held_note or "The action was sent and every press has a release.",
            action=action.to_dict(),
            steps=steps,
            presses=guard.presses - presses_before,
            releases=guard.releases - releases_before,
            held_after=held_after,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            error=error or held_note,
        )
