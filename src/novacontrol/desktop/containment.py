"""Containment profiles and the gated dispatch primitive behind DF-6.

DF-6-L1 was a paste tail that reached an unrelated application: ``SendKeys('^v')``
is four input events (Ctrl-down, V-down, V-up, Ctrl-up) and the identity check
happened only *after* all four had been injected. Nothing on Windows can recall
an event that has already been dispatched, so containment has to be structural:

* the **UIA** and **isolated-session** profiles never inject a keyboard event
  into the interactive desktop at all, which is what lets them claim a
  guarantee;
* the **interactive keyboard** profile can only narrow the exposure to at most
  one input event per identity check, so it is labelled ``best_effort`` and may
  never carry DF-6's PASS.

:class:`GatedChord` is that narrowing, and it is also the object the dispatch
ledger drives: every event is preceded by its own identity check, the remaining
events are withheld the moment a check fails, and the record states exactly how
many events went out after the last successful check.

Two kinds of edge are distinguished, because they carry different risk:

``input``
    An event that can move or modify the destination (a character key-down, a
    click). These are the events that must never reach a foreign window.
``auxiliary``
    A release of a modifier this process is holding (``ctrl_up``). It cannot
    produce text or activate anything, and withholding it would leave Ctrl held
    down for the *user's* next keystrokes — a worse outcome than the harmless
    release. It is still recorded, and when the destination is no longer bound
    it is recorded as an ``unbound_release`` rather than pretending it was
    gated.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "ContainmentError",
    "ContainmentProfile",
    "GatedChord",
    "Identity",
    "IntegrityVerdict",
    "PASTE_CHORD",
    "compare_integrity",
    "edge_key",
    "edge_kind",
]


class ContainmentProfile(StrEnum):
    """How input reaches the target application, and what that entitles DF-6 to.

    ``guaranteed`` is a claim about construction, not about a runtime check: a
    profile is guaranteed only when no event can be injected into another
    application's window in the first place.
    """

    UIA = "uia"
    ISOLATED_SESSION = "isolated_session"
    BEST_EFFORT = "best_effort"

    @property
    def guaranteed(self) -> bool:
        """Whether this profile may carry a DF-6 containment PASS."""
        return self is not ContainmentProfile.BEST_EFFORT


class IntegrityVerdict(StrEnum):
    """Whether this process may drive the target at all (UIPI)."""

    SAME_OR_LOWER = "same_or_lower"
    TARGET_HIGHER = "target_higher"
    UNKNOWN = "unknown"


def compare_integrity(ours: int | None, target: int | None) -> IntegrityVerdict:
    """The UIPI verdict for one target, from two integrity RIDs.

    Windows drops ``SendInput`` silently when the target runs at a higher
    integrity level, so the only safe response is to refuse before injecting.
    A missing reading is ``UNKNOWN``: it is not evidence of compatibility.
    """
    if ours is None or target is None:
        return IntegrityVerdict.UNKNOWN
    if target > ours:
        return IntegrityVerdict.TARGET_HIGHER
    return IntegrityVerdict.SAME_OR_LOWER


def _as_int(value: object) -> int:
    """An int from a probe value, or 0 when it is not one (never a guess)."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


@dataclass(frozen=True, slots=True, order=True)
class Identity:
    """A window's stable identity: the pair the OS actually keys on."""

    handle: int
    pid: int

    @classmethod
    def of(cls, handle: object, pid: object) -> Identity | None:
        """Build an identity, or None when either half is missing.

        A title is not an identity (D12): a same-titled window owned by another
        process is indistinguishable by title and must not be accepted.
        """
        resolved_handle = _as_int(handle)
        resolved_pid = _as_int(pid)
        if resolved_handle <= 0 or resolved_pid <= 0:
            return None
        return cls(handle=resolved_handle, pid=resolved_pid)

    def to_list(self) -> list[int]:
        return [self.handle, self.pid]

    def __str__(self) -> str:
        return f"{self.handle}/{self.pid}"


class ContainmentError(RuntimeError):
    """An input attempt that was refused or aborted, with its dispatch record.

    The record travels with the exception so the failed attempt still reports
    where its events were addressed and how many went out after the last
    identity check. A failure that carries no destination is exactly the
    evidence gap M2.8b exists to catch.
    """

    def __init__(self, message: str, record: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.dispatch_record: dict[str, Any] = dict(record or {})


#: The paste chord, one event at a time. Splitting it is the point: as a single
#: ``SendKeys`` call Windows injected all four events regardless of what the
#: foreground had become.
PASTE_CHORD: tuple[str, ...] = ("ctrl_down", "v_down", "v_up", "ctrl_up")

#: Edges that can produce text or activate a control. Everything else is a
#: modifier release (see the module docstring).
_INPUT_EDGES: frozenset[str] = frozenset({"v_down"})

_MODIFIERS: frozenset[str] = frozenset({"ctrl", "shift", "alt"})


def edge_key(event: str) -> str:
    """The key an edge belongs to (``ctrl_down`` -> ``ctrl``)."""
    for suffix in ("_down", "_up"):
        if event.endswith(suffix):
            return event[: -len(suffix)]
    return event


def edge_kind(event: str) -> str:
    """``input`` for an event that can deliver, ``auxiliary`` for a release."""
    return "input" if event in _INPUT_EDGES else "auxiliary"


def _blank_record(
    profile: ContainmentProfile, bound: Identity | None
) -> dict[str, Any]:
    return {
        "containment": profile.value,
        "guaranteed": profile.guaranteed,
        "bound_identity": bound.to_list() if bound is not None else None,
        "identity_source": "objective" if bound is not None else "unbound",
        "identity_checks": 0,
        "destinations": [],
        "events_dispatched": 0,
        "input_events": 0,
        "auxiliary_events": 0,
        "events_after_last_identity_check": 0,
        "input_events_after_last_identity_check": 0,
        "aborted": False,
        "abort_reason": "",
        "stuck_keys": [],
        "unbound_releases": [],
        "integrity": IntegrityVerdict.UNKNOWN.value,
        "integrity_verified": False,
        "residual_risk": [],
    }


@dataclass(slots=True)
class GatedChord:
    """Dispatch a fixed sequence of key edges, each gated on the live identity.

    The callables are injected so the primitive is pure: ``identity`` reads the
    CURRENT foreground identity, ``press``/``release`` send one edge, and
    ``stop_requested`` is checked between edges. Nothing here talks to Windows,
    which is what lets the dispatch ledger drive it without sending input.

    ``bound`` is the objective-bound identity. When it is None the first
    observation becomes the baseline (``identity_source`` records that, and the
    run carries an ``unbound_objective`` residual risk) — a caller with no
    bound identity cannot be given a stronger claim than self-consistency.
    """

    identity: Callable[[], Awaitable[Identity | None]]
    press: Callable[[str], Awaitable[None]]
    release: Callable[[str], Awaitable[None]]
    bound: Identity | None = None
    profile: ContainmentProfile = ContainmentProfile.BEST_EFFORT
    integrity: Callable[[], Awaitable[IntegrityVerdict]] | None = None
    stop_requested: Callable[[], bool] = lambda: False
    clock: Callable[[], float] = time.monotonic
    record: dict[str, Any] = field(init=False, repr=False)
    #: Keys this process is holding down, so it never forfeits a release.
    _held: list[str] = field(init=False, repr=False, default_factory=list)
    #: Events dispatched since the last SUCCESSFUL identity check.
    _since_check: int = field(init=False, repr=False, default=0)
    _input_since_check: int = field(init=False, repr=False, default=0)

    def __post_init__(self) -> None:
        self.record = _blank_record(self.profile, self.bound)

    # -- the gate ---------------------------------------------------------

    async def _check(self) -> Identity | None:
        """One identity check, recorded before the event it guards."""
        observed = await self.identity()
        self.record["identity_checks"] = int(self.record["identity_checks"]) + 1
        if self.bound is None:
            # No objective-bound identity: the first observation is the most
            # this profile can promise, and the record says so.
            self.bound = observed
            self.record["bound_identity"] = (
                observed.to_list() if observed is not None else None
            )
            self.record["identity_source"] = (
                "first_observation" if observed is not None else "unbound"
            )
            if observed is not None:
                risks = self.record["residual_risk"]
                if isinstance(risks, list) and "unbound_objective" not in risks:
                    risks.append("unbound_objective")
        if observed is not None:
            destinations = self.record["destinations"]
            if isinstance(destinations, list):
                destinations.append(observed.to_list())
        self.record["last_identity"] = (
            observed.to_list() if observed is not None else None
        )
        if observed == self.bound and observed is not None:
            self._since_check = 0
            self._input_since_check = 0
        return observed

    async def _send(self, event: str) -> bool:
        """Dispatch one edge after its own identity check.

        Returns False when the check refused it, in which case nothing was sent
        and the caller must withhold the rest of the sequence.
        """
        observed = await self._check()
        if observed is None:
            return False
        if observed != self.bound:
            return False
        if event.endswith("_down"):
            await self.press(edge_key(event))
            self._held.append(edge_key(event))
        else:
            await self.release(edge_key(event))
            key = edge_key(event)
            if key in self._held:
                self._held.remove(key)
        self._since_check += 1
        kind = edge_kind(event)
        if kind == "input":
            self._input_since_check += 1
            self.record["input_events"] = int(self.record["input_events"]) + 1
        else:
            self.record["auxiliary_events"] = int(self.record["auxiliary_events"]) + 1
        self.record["events_dispatched"] = int(self.record["events_dispatched"]) + 1
        self.record["events_after_last_identity_check"] = self._since_check
        self.record["input_events_after_last_identity_check"] = self._input_since_check
        return True

    async def _withhold(self, reason: str) -> None:
        """Free what this process holds, without claiming containment.

        A held modifier is released even when the destination is no longer the
        bound window: the release delivers nothing, while leaving Ctrl down
        would modify the *user's* next keystrokes. Any other held key is
        withheld and recorded as a stuck-key risk, because releasing it would
        be an event aimed at a window this run may not address.
        """
        self.record["aborted"] = True
        self.record["abort_reason"] = reason
        remaining = list(self._held)
        self._held.clear()
        stuck = self.record["stuck_keys"]
        unbound = self.record["unbound_releases"]
        for key in reversed(remaining):
            if key in _MODIFIERS:
                await self.release(key)
                if isinstance(unbound, list):
                    unbound.append(key)
                continue
            if isinstance(stuck, list):
                stuck.append(key)

    # -- the run ----------------------------------------------------------

    async def run(self, events: Sequence[str] = PASTE_CHORD) -> dict[str, Any]:
        """Dispatch ``events``, gated, and return the dispatch record.

        Raises :class:`ContainmentError` when nothing was dispatched *and* the
        attempt is a refusal (no foreground window, higher-integrity target, or
        a foreground that is not the bound identity before the first event).
        An abort after the first event returns the record with ``aborted`` set
        so the caller can report a partial, bounded exposure.
        """
        verdict = (
            await self.integrity()
            if self.integrity is not None
            else IntegrityVerdict.UNKNOWN
        )
        self.record["integrity"] = verdict.value
        self.record["integrity_verified"] = verdict is IntegrityVerdict.SAME_OR_LOWER
        if verdict is IntegrityVerdict.TARGET_HIGHER:
            risks = self.record["residual_risk"]
            if isinstance(risks, list):
                risks.append("uipi_target_denied")
            raise ContainmentError(
                "Refusing to inject input: the target window belongs to a "
                "process at a higher integrity level, and Windows drops such "
                "input silently rather than reporting it.",
                self.record,
            )
        if verdict is IntegrityVerdict.UNKNOWN and self.profile.guaranteed:
            risks = self.record["residual_risk"]
            if isinstance(risks, list):
                risks.append("integrity_unverified")
            raise ContainmentError(
                "Refusing to inject input in a guaranteed-containment profile: "
                "the integrity level of the target could not be established, "
                "so the profile's guarantee cannot be demonstrated.",
                self.record,
            )
        if verdict is IntegrityVerdict.UNKNOWN:
            risks = self.record["residual_risk"]
            if isinstance(risks, list):
                risks.append("integrity_unverified")

        for index, event in enumerate(events):
            if self.stop_requested():
                await self._withhold("stop_requested")
                return self.record
            if not await self._send(event):
                if index == 0:
                    self.record["aborted"] = True
                    self.record["abort_reason"] = (
                        "no_foreground_window"
                        if self.record.get("last_identity") is None
                        else "foreground_not_target"
                    )
                    raise ContainmentError(
                        "Refusing to paste: the foreground window is not the "
                        "objective-bound target "
                        f"(expected {self.bound}, observed "
                        f"{self.record.get('last_identity')}), so no input was sent.",
                        self.record,
                    )
                await self._withhold("identity_changed_during_input")
                return self.record
        return self.record


def residual_risk_notes(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Human-readable residual risks carried by a dispatch record."""
    notes: list[str] = []
    if record.get("identity_source") == "first_observation":
        notes.append("The objective bound no identity; the first foreground window was adopted.")
    risks = record.get("residual_risk") or []
    if "integrity_unverified" in risks:
        notes.append("The target's integrity level could not be read (UIPI drop is silent).")
    if "uipi_target_denied" in risks:
        notes.append("The target runs at a higher integrity level than this process.")
    if record.get("unbound_releases"):
        notes.append("A held modifier was released while the bound window was gone.")
    if record.get("stuck_keys"):
        notes.append("A key was withheld while held; it may remain down in the session.")
    notes.append(
        "A focus change between the final identity check and its event dispatch "
        "cannot be closed by pre-send checks alone."
    )
    return tuple(notes)
