"""DF-6 dispatch-ledger acceptance suite (offline; no desktop input).

The ledger is the point. DF-6-L1 was not a missing check, it was a check that
ran *after* four events had already been injected: ``SendKeys('^v')`` is
Ctrl-down, V-down, V-up, Ctrl-up, and Windows cannot recall an event that has
been dispatched. So no test here trusts the controller's own ``detail`` or
``success``. Instead a :class:`SimulatedDesktop` owns

* the bound identity,
* the current foreground identity,
* every window title,
* scripted hazards ("the target dies after this event", "an unrelated window
  takes the foreground here"), and
* the :class:`DispatchLedger`.

The runner under test is the real :class:`LocalDesktopRunner` — the real
``GatedChord`` loop, the real clipboard lifecycle, the real ``_type_text``
decision logic. Only the *transports* are simulated (foreground probe, key
edge, clipboard, integrity probe). Each event is recorded with the destination
read from the simulator at the instant the transport is called, so the oracle
is independent of the implementation under test.

Twelve scenarios from the DF-6 report §O.6, then the UIA and isolated-session
profiles, which must show zero keyboard events because they generate none.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from novacontrol.agentcore.planner import AgentPlanStep, StepKind
from novacontrol.agentcore.recovery import RecoveryOutcome, RecoveryStrategy
from novacontrol.agentcore.state import ActionRecord, AgentTaskState
from novacontrol.computer.session import (
    IsolatedSessionAdapter,
    SessionIdentity,
    SessionState,
    SessionUnavailable,
)
from novacontrol.desktop.clipboard import ClipboardSnapshot, ClipboardStatus, plan_restore
from novacontrol.desktop.containment import (
    ContainmentProfile,
    Identity,
    IntegrityVerdict,
    compare_integrity,
)
from novacontrol.desktop.controller import LocalDesktopRunner, PasteContainmentError
from novacontrol.desktop.uia import (
    UiaAdapter,
    UiaElement,
    UiaRefusal,
    UiaTarget,
)

BOUND = Identity(7123, 4242)
OTHER = Identity(9921, 8)
BOUND_TITLE = "NovaGroup1MidAction"
TEXT = "hello from the operator loop"


# ---------------------------------------------------------------------------
# The ledger (the oracle)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgeredEvent:
    """One event as the SIMULATOR saw it, not as the controller reported it."""

    event: str
    destination: Identity | None
    kind: str  # "input" | "auxiliary"
    channel: str  # "keyboard" | "uia" | "session"
    gated: bool  # the last identity check before it matched the bound window
    check_index: int


@dataclass(slots=True)
class DispatchLedger:
    """Every dispatched event, in order, with its actual destination."""

    events: list[LedgeredEvent] = field(default_factory=list)

    def record(
        self,
        event: str,
        destination: Identity | None,
        *,
        bound: Identity,
        kind: str = "auxiliary",
        channel: str = "keyboard",
        check_index: int = 0,
    ) -> None:
        self.events.append(
            LedgeredEvent(
                event=event,
                destination=destination,
                kind=kind,
                channel=channel,
                gated=destination == bound,
                check_index=check_index,
            )
        )

    def for_channel(self, channel: str) -> list[LedgeredEvent]:
        return [item for item in self.events if item.channel == channel]

    def input_events(self, channel: str = "keyboard") -> list[LedgeredEvent]:
        return [item for item in self.for_channel(channel) if item.kind == "input"]

    def foreign(
        self, bound: Identity, *, channel: str = "keyboard", kind: str | None = None
    ) -> list[LedgeredEvent]:
        """Events that reached a window other than the bound target.

        ``destination is None`` is "no window at all", which is not a foreign
        application; it is reported separately as an unbound destination.
        """
        found = [
            item
            for item in self.for_channel(channel)
            if item.destination is not None and item.destination != bound
        ]
        if kind is not None:
            found = [item for item in found if item.kind == kind]
        return found

    def unbound(self, *, channel: str = "keyboard") -> list[LedgeredEvent]:
        return [
            item for item in self.for_channel(channel) if item.destination is None
        ]


# ---------------------------------------------------------------------------
# The simulator
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SimulatedDesktop:
    """Foreground/title model plus scripted hazards, independent of the runner."""

    foreground: Identity | None = BOUND
    integrity: IntegrityVerdict = IntegrityVerdict.SAME_OR_LOWER
    titles: dict[Identity, str] = field(
        default_factory=lambda: {BOUND: BOUND_TITLE, OTHER: "Freebuff"}
    )
    ledger: DispatchLedger = field(default_factory=DispatchLedger)
    #: Hazards run AFTER an event has been dispatched.
    after_event: dict[int, Callable[[], None]] = field(default_factory=dict)
    #: Hazard run BETWEEN the identity check and the event dispatch (the race
    #: that pre-send checks cannot close; recorded, never claimed as safe).
    race_on_event: dict[str, Callable[[], None]] = field(default_factory=dict)
    #: Clipboard sequence numbers handed out, in order (after-write, now).
    sequence_reads: list[int] = field(default_factory=lambda: [10, 10])
    clipboard_writes: list[str] = field(default_factory=list)
    restores: list[dict[str, Any]] = field(default_factory=list)
    deny_writes: bool = False
    checks: int = 0
    events: int = 0
    stopping: bool = False
    bound: Identity = BOUND

    # -- hazards ----------------------------------------------------------

    def kill_bound(self) -> None:
        self.titles.pop(BOUND, None)
        if self.foreground == BOUND:
            self.foreground = None

    def steal(self, identity: Identity = OTHER) -> None:
        self.titles.setdefault(identity, "Freebuff")
        self.foreground = identity

    def replace_with_same_title(self) -> None:
        replacement = Identity(BOUND.handle + 2, BOUND.pid + 2)
        self.titles[replacement] = BOUND_TITLE
        self.foreground = replacement

    def bump_clipboard(self) -> None:
        """Another application takes the clipboard: the sequence advances."""
        self.sequence_reads = [10, 11]

    # -- transport --------------------------------------------------------

    def next_sequence(self) -> int:
        if len(self.sequence_reads) > 1:
            return self.sequence_reads.pop(0)
        return self.sequence_reads[0] if self.sequence_reads else 0

    def dispatch(self, event: str) -> None:
        """Record one event's REAL destination, then run its hazard."""
        race = self.race_on_event.get(event)
        if race is not None:
            race()
        self.events += 1
        self.ledger.record(
            event,
            self.foreground,
            bound=self.bound,
            kind="input" if event == "v_down" else "auxiliary",
            check_index=self.checks,
        )
        hazard = self.after_event.get(self.events)
        if hazard is not None:
            hazard()


class LedgerRunner(LocalDesktopRunner):
    """The real runner with only its OS transports simulated."""

    def __init__(self, sim: SimulatedDesktop) -> None:
        super().__init__(windows_type_paste=True)
        self.sim = sim

    def _uses_windows_type_paste(self) -> bool:
        return True

    async def _foreground_identity(self) -> Identity | None:
        self.sim.checks += 1
        return self.sim.foreground

    async def _paste_edge(self, key: str, *, down: bool) -> None:
        self.sim.dispatch(f"{key}_{'down' if down else 'up'}")

    async def _integrity_verdict(self, pid: int) -> IntegrityVerdict:
        return self.sim.integrity

    def _stop_requested_for_input(self) -> bool:
        return self.sim.stopping

    async def _clipboard_capture(self) -> ClipboardSnapshot:
        return ClipboardSnapshot(
            available=True, sequence=9, text="previous clipboard text", has_text=True
        )

    async def _clipboard_write(self, text: str) -> bool:
        if self.sim.deny_writes:
            return False
        self.sim.clipboard_writes.append(text)
        return True

    async def _clipboard_sequence(self) -> int:
        return self.sim.next_sequence()

    async def _clipboard_clear(self) -> bool:
        return not self.sim.deny_writes

    async def _verify_type_windows(self, text: str) -> dict[str, Any] | None:
        foreground = self.sim.foreground
        return {
            "clipboard_match": True,
            "focused_window": foreground is not None,
            "window_title": self.sim.titles.get(foreground, "") if foreground else "",
            "foreground_handle": foreground.handle if foreground else 0,
            "foreground_pid": foreground.pid if foreground else 0,
        }


async def _paste(runner: LedgerRunner) -> dict[str, Any]:
    result = await runner._type_text(
        TEXT,
        expect=BOUND_TITLE,
        expect_handle=BOUND.handle,
        expect_pid=BOUND.pid,
    )
    return dict(result)


def _bound_paste(runner: LedgerRunner) -> dict[str, Any]:
    return {
        "text": TEXT,
        "expect": BOUND_TITLE,
        "expect_handle": BOUND.handle,
        "expect_pid": BOUND.pid,
    }


# ---------------------------------------------------------------------------
# §O.6 scenarios 1-12 against the interactive keyboard profile
# ---------------------------------------------------------------------------


class DispatchLedgerKeyboardTests(unittest.IsolatedAsyncioTestCase):
    """The best_effort profile: bounded exposure, never a containment claim."""

    async def test_01_normal_operation(self) -> None:
        sim = SimulatedDesktop()
        runner = LedgerRunner(sim)
        output = await _paste(runner)

        self.assertTrue(output["verified"])
        self.assertEqual(
            [item.event for item in sim.ledger.events],
            ["ctrl_down", "v_down", "v_up", "ctrl_up"],
        )
        # Zero events to any other window, and every input event gated.
        self.assertEqual(sim.ledger.foreign(BOUND), [])
        self.assertTrue(all(item.gated for item in sim.ledger.events))
        # The profile is declared best_effort and never guaranteed.
        self.assertEqual(output["containment"], ContainmentProfile.BEST_EFFORT.value)
        self.assertFalse(output["guaranteed"])
        # At most one event may follow the last successful identity check.
        self.assertLessEqual(output["input_events_after_last_identity_check"], 1)
        self.assertEqual(output["events_after_last_identity_check"], 1)

    async def test_02_target_killed_before_dispatch(self) -> None:
        sim = SimulatedDesktop()
        sim.kill_bound()
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        self.assertEqual(sim.ledger.events, [])
        record = raised.exception.dispatch_record
        self.assertEqual(record["bound_identity"], BOUND.to_list())
        self.assertIsNone(record["verification"]["observed_identity"])
        # Nothing was sent, so recovery must classify it as not delivered.
        delivery = ActionRecord(
            step_id="s1", action="type", target="Note", success=False,
            detail=str(raised.exception), output=record,
        )
        from novacontrol.agentcore.recovery import input_delivery_state

        self.assertEqual(input_delivery_state(delivery), "not_delivered")

    async def test_03_target_killed_on_event_1(self) -> None:
        sim = SimulatedDesktop()
        sim.after_event[1] = sim.kill_bound
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        self.assertEqual([item.event for item in sim.ledger.events][0], "ctrl_down")
        self.assertEqual(sim.ledger.input_events(), [])
        self.assertEqual(sim.ledger.foreign(BOUND), [])
        self.assertTrue(raised.exception.dispatch_record["aborted"])
        self.assertEqual(
            raised.exception.dispatch_record["abort_reason"],
            "identity_changed_during_input",
        )

    async def test_04_focus_stolen_between_modifier_and_key(self) -> None:
        sim = SimulatedDesktop()
        sim.after_event[1] = sim.steal
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError):
            await _paste(runner)
        ledgered = [item.event for item in sim.ledger.events]
        self.assertIn("ctrl_down", ledgered)
        self.assertNotIn("v_down", ledgered)  # the paste key is never sent
        self.assertEqual(sim.ledger.input_events(), [])
        # The only foreign event is the harmless modifier release, recorded.
        self.assertEqual(
            [item.event for item in sim.ledger.foreign(BOUND, kind="auxiliary")],
            ["ctrl_up"],
        )

    async def test_05_focus_stolen_between_key_down_and_key_up(self) -> None:
        sim = SimulatedDesktop()
        sim.after_event[2] = sim.steal
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        ledgered = [item.event for item in sim.ledger.events]
        self.assertIn("v_down", ledgered)
        self.assertNotIn("v_up", ledgered)  # withheld, never sent to a foreign window
        self.assertNotIn("v_up", [item.event for item in sim.ledger.events])
        # The key-down happened while the target was still bound.
        self.assertEqual(sim.ledger.foreign(BOUND, kind="input"), [])
        # Withholding the key-up is a stuck-key risk, and it is recorded.
        self.assertEqual(raised.exception.dispatch_record["stuck_keys"], ["v"])

    async def test_06_unrelated_foreground_before_send(self) -> None:
        sim = SimulatedDesktop(foreground=OTHER)
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        self.assertEqual(sim.ledger.events, [])
        self.assertEqual(raised.exception.dispatch_record["abort_reason"], "foreground_not_target")

    async def test_07_identity_change_same_title(self) -> None:
        sim = SimulatedDesktop()
        sim.replace_with_same_title()
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        self.assertEqual(sim.ledger.events, [])
        self.assertEqual(
            raised.exception.dispatch_record["verification"]["observed_identity"],
            [BOUND.handle + 2, BOUND.pid + 2],
        )

    async def test_08_replacement_window_with_identical_title(self) -> None:
        """A title is not an identity: the same title, a different process."""
        sim = SimulatedDesktop()
        sim.titles[OTHER] = BOUND_TITLE  # identical title, unrelated pid/handle
        sim.steal(OTHER)
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError):
            await _paste(runner)
        self.assertEqual(sim.ledger.events, [])

    async def test_09_unrelated_destination_is_refused_with_foreground_not_target(self) -> None:
        sim = SimulatedDesktop(foreground=OTHER)
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        record = raised.exception.dispatch_record
        self.assertEqual(record["bound_identity"], BOUND.to_list())
        self.assertEqual(record["verification"]["observed_identity"], OTHER.to_list())
        self.assertEqual(record["events_dispatched"], 0)
        self.assertEqual(sim.ledger.foreign(BOUND), [])

    async def test_10_interrupted_delivery_stop_between_events(self) -> None:
        sim = SimulatedDesktop()

        def stop_after_first() -> None:
            sim.stopping = True

        sim.after_event[1] = stop_after_first
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        self.assertEqual(
            [item.event for item in sim.ledger.events], ["ctrl_down", "ctrl_up"]
        )
        record = raised.exception.dispatch_record
        self.assertTrue(record["aborted"])
        self.assertEqual(record["abort_reason"], "stop_requested")
        self.assertEqual(sim.ledger.input_events(), [])

    async def test_11_recovery_after_uncertain_delivery_never_replays(self) -> None:
        sim = SimulatedDesktop()
        sim.after_event[2] = sim.steal
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        record = raised.exception.dispatch_record
        delivery_record = ActionRecord(
            step_id="s1",
            action="type",
            target="Note",
            success=False,
            detail=str(raised.exception),
            output=record,
        )
        from novacontrol.agentcore.recovery import input_delivery_state

        self.assertEqual(input_delivery_state(delivery_record), "uncertain")

        # The orchestrator's defence in depth: a possibly-delivered input step
        # is re-checked, never retried, whatever the recovery engine proposes.
        from novacontrol.agentcore.orchestrator import AgenticOrchestrator

        class _RetryEngine:
            def diagnose(self, *args: Any, **kwargs: Any) -> RecoveryOutcome:
                return RecoveryOutcome(RecoveryStrategy.RETRY, "transient")

        orchestrator = AgenticOrchestrator(
            perception=object(), actions=object(), recovery=_RetryEngine()  # type: ignore[arg-type]
        )
        rechecked: list[str] = []

        async def _recheck(state: Any, step: Any, record: ActionRecord, after: Any) -> None:
            rechecked.append(record.step_id)

        orchestrator._recheck = _recheck  # type: ignore[method-assign]
        state = AgentTaskState(goal="type hello into Note")
        state.interpretation["target_identity"] = BOUND.to_list()
        step = AgentPlanStep(kind=StepKind.DESKTOP, description="type hello into Note")
        await orchestrator._recover(state, step, delivery_record, None)

        self.assertEqual(state.recoveries[-1]["strategy"], RecoveryStrategy.RECHECK.value)
        self.assertEqual(rechecked, ["s1"])
        # Exactly one delivery attempt: the ledger shows no replay.
        self.assertEqual(len(sim.ledger.input_events()), 1)

    async def test_12_emergency_stop_before_any_event(self) -> None:
        sim = SimulatedDesktop()
        sim.stopping = True
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        self.assertEqual(sim.ledger.events, [])
        self.assertEqual(raised.exception.dispatch_record["abort_reason"], "stop_requested")

    async def test_12b_emergency_stop_mid_run_leaves_no_held_key_unreported(self) -> None:
        sim = SimulatedDesktop()
        sim.after_event[2] = lambda: setattr(sim, "stopping", True)
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        record = raised.exception.dispatch_record
        self.assertEqual(sim.ledger.input_events()[-1].event, "v_down")
        # Every held key is either released or reported as stuck.
        released = [item.event for item in sim.ledger.events if item.event == "v_up"]
        self.assertTrue(record["stuck_keys"] or released)
        self.assertEqual(sim.ledger.foreign(BOUND, kind="input"), [])

    async def test_race_between_check_and_dispatch_is_bounded_and_reported(self) -> None:
        """The documented residual: at most ONE input event, and it is recorded.

        Pre-send checks cannot close the window between the final check and the
        event. The profile's claim is therefore bounded, not absolute — and
        this test is what stops that sentence from being a footnote.
        """
        sim = SimulatedDesktop()
        sim.race_on_event = {"v_down": sim.steal}
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        foreign_input = sim.ledger.foreign(BOUND, kind="input")
        self.assertEqual([item.event for item in foreign_input], ["v_down"])
        self.assertEqual(
            raised.exception.dispatch_record["input_events_after_last_identity_check"], 1
        )

    async def test_an_unknown_profile_label_never_upgrades_the_claim(self) -> None:
        from novacontrol.desktop.controller import _containment_profile

        self.assertIs(
            _containment_profile("guaranteed_by_magic"), ContainmentProfile.BEST_EFFORT
        )
        self.assertIs(_containment_profile(None), ContainmentProfile.BEST_EFFORT)
        self.assertIs(_containment_profile("uia"), ContainmentProfile.UIA)


# ---------------------------------------------------------------------------
# Integrity preflight (UIPI)
# ---------------------------------------------------------------------------


class IntegrityPreflightTests(unittest.IsolatedAsyncioTestCase):
    def test_verdicts(self) -> None:
        self.assertIs(compare_integrity(0x2000, 0x2000), IntegrityVerdict.SAME_OR_LOWER)
        self.assertIs(compare_integrity(0x2000, 0x1000), IntegrityVerdict.SAME_OR_LOWER)
        self.assertIs(compare_integrity(0x2000, 0x3000), IntegrityVerdict.TARGET_HIGHER)
        self.assertIs(compare_integrity(None, 0x2000), IntegrityVerdict.UNKNOWN)
        self.assertIs(compare_integrity(0x2000, None), IntegrityVerdict.UNKNOWN)

    async def test_a_higher_integrity_target_is_refused_before_injection(self) -> None:
        sim = SimulatedDesktop(integrity=IntegrityVerdict.TARGET_HIGHER)
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        self.assertEqual(sim.ledger.events, [])
        self.assertIn("higher integrity level", str(raised.exception))
        self.assertEqual(raised.exception.dispatch_record["integrity"], "target_higher")

    async def test_an_unreadable_integrity_level_is_recorded_as_unverified(self) -> None:
        sim = SimulatedDesktop(integrity=IntegrityVerdict.UNKNOWN)
        runner = LedgerRunner(sim)
        output = await _paste(runner)
        self.assertFalse(output["integrity_verified"])
        self.assertTrue(
            any("integrity" in note for note in output["residual_risk"]),
        )

    async def test_a_guaranteed_profile_refuses_on_unknown_integrity(self) -> None:
        from novacontrol.desktop.containment import GatedChord

        async def _identity() -> Identity | None:
            return BOUND

        async def _send(key: str) -> None:
            raise AssertionError("no event may be dispatched")

        chord = GatedChord(
            identity=_identity,
            press=_send,
            release=_send,
            bound=BOUND,
            profile=ContainmentProfile.UIA,
            integrity=_unknown,
        )
        from novacontrol.desktop.containment import ContainmentError

        with self.assertRaises(ContainmentError):
            await chord.run()


async def _unknown() -> IntegrityVerdict:
    return IntegrityVerdict.UNKNOWN


# ---------------------------------------------------------------------------
# Clipboard lifecycle
# ---------------------------------------------------------------------------


class ClipboardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_plan_matrix(self) -> None:
        full = ClipboardSnapshot(available=True, sequence=9, text="keep me", has_text=True)
        empty = ClipboardSnapshot(available=True, sequence=9, text=None, has_text=False)
        image = ClipboardSnapshot(
            available=True, sequence=9, text=None, has_text=False, has_other=True
        )
        missing = ClipboardSnapshot(available=False, error="denied")

        restored = plan_restore(full, sequence_after_write=10, sequence_now=10, delivered=True)
        self.assertIs(restored.status, ClipboardStatus.RESTORED)
        self.assertEqual(restored.write_text, "keep me")

        cleared = plan_restore(empty, sequence_after_write=10, sequence_now=10, delivered=False)
        self.assertIs(cleared.status, ClipboardStatus.CLEARED)
        self.assertTrue(cleared.clear)

        # A value another application wrote is never overwritten.
        concurrent = plan_restore(full, sequence_after_write=10, sequence_now=11, delivered=True)
        self.assertIs(concurrent.status, ClipboardStatus.SKIPPED_CONCURRENT_CHANGE)
        self.assertFalse(concurrent.modifies_clipboard)

        non_text = plan_restore(image, sequence_after_write=10, sequence_now=10, delivered=True)
        self.assertIs(non_text.status, ClipboardStatus.SKIPPED_NON_TEXT)

        unavailable = plan_restore(
            missing, sequence_after_write=10, sequence_now=10, delivered=True
        )
        self.assertIs(unavailable.status, ClipboardStatus.UNAVAILABLE)

        unreadable = plan_restore(full, sequence_after_write=0, sequence_now=10, delivered=True)
        self.assertIs(unreadable.status, ClipboardStatus.SKIPPED_CONCURRENT_CHANGE)

    async def test_successful_paste_restores_the_previous_clipboard(self) -> None:
        sim = SimulatedDesktop()
        runner = LedgerRunner(sim)
        output = await _paste(runner)
        self.assertEqual(output["clipboard"]["status"], ClipboardStatus.RESTORED.value)
        self.assertEqual(sim.clipboard_writes, [TEXT, "previous clipboard text"])

    async def test_failed_paste_does_not_leave_the_staged_text_behind(self) -> None:
        sim = SimulatedDesktop(foreground=OTHER)
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        clipboard = raised.exception.dispatch_record["clipboard"]
        self.assertEqual(clipboard["status"], ClipboardStatus.RESTORED.value)
        self.assertEqual(sim.clipboard_writes, [TEXT, "previous clipboard text"])

    async def test_interrupted_paste_restores_the_clipboard(self) -> None:
        sim = SimulatedDesktop()
        sim.after_event[2] = lambda: setattr(sim, "stopping", True)
        runner = LedgerRunner(sim)
        with self.assertRaises(PasteContainmentError) as raised:
            await _paste(runner)
        self.assertEqual(
            raised.exception.dispatch_record["clipboard"]["status"],
            ClipboardStatus.RESTORED.value,
        )

    async def test_a_concurrent_clipboard_change_is_not_overwritten(self) -> None:
        sim = SimulatedDesktop()
        sim.bump_clipboard()
        runner = LedgerRunner(sim)
        output = await _paste(runner)
        self.assertEqual(
            output["clipboard"]["status"], ClipboardStatus.SKIPPED_CONCURRENT_CHANGE.value
        )
        self.assertTrue(output["clipboard"]["residue"])
        self.assertEqual(sim.clipboard_writes, [TEXT])  # the other app's value kept

    async def test_a_denied_restore_is_reported_not_claimed(self) -> None:
        sim = SimulatedDesktop()
        runner = LedgerRunner(sim)
        sim.deny_writes = False
        # Stage succeeds, the restore write is the one Windows refuses.
        calls: list[str] = []
        original = runner._clipboard_write

        async def _deny_second(text: str) -> bool:
            calls.append(text)
            if len(calls) > 1:
                return False
            return await original(text)

        runner._clipboard_write = _deny_second  # type: ignore[method-assign]
        output = await _paste(runner)
        self.assertEqual(output["clipboard"]["status"], ClipboardStatus.DENIED.value)
        self.assertFalse(output["clipboard"]["applied"])


# ---------------------------------------------------------------------------
# UIA profile: zero keyboard events, by construction
# ---------------------------------------------------------------------------


def _element(
    *,
    runtime: tuple[int, ...] = (1, 2),
    name: str = "Message",
    patterns: tuple[str, ...] = ("ValuePattern",),
    pid: int = BOUND.pid,
    handle: int = BOUND.handle,
    enabled: bool = True,
    offscreen: bool = False,
) -> UiaElement:
    return UiaElement(
        runtime_id=runtime,
        name=name,
        automation_id="msg",
        control_type="Edit",
        pid=pid,
        window_handle=handle,
        patterns=frozenset(patterns),
        enabled=enabled,
        offscreen=offscreen,
    )


class SimulatedUiaBackend:
    """A UIA transport that records element-scoped calls, not keyboard events."""

    def __init__(
        self,
        *,
        elements: tuple[UiaElement, ...] = (),
        available: bool = True,
        stale_after_first: bool = False,
    ) -> None:
        self.elements = list(elements)
        self.available = available
        self.stale_after_first = stale_after_first
        self.resolves = 0
        self.values: dict[tuple[int, ...], str] = {}
        self.ledger = DispatchLedger()
        self.calls: list[str] = []

    def availability(self) -> tuple[bool, str]:
        return (True, "simulated") if self.available else (False, "no typelib")

    def resolve(self, target: UiaTarget) -> tuple[UiaElement, ...]:
        self.resolves += 1
        self.calls.append("resolve")
        if self.stale_after_first and self.resolves > 1:
            return ()
        return tuple(
            element
            for element in self.elements
            if (not target.name or element.name == target.name)
            and (not target.automation_id or element.automation_id == target.automation_id)
        )

    def read_value(self, element: UiaElement) -> str | None:
        return self.values.get(element.runtime_id, "")

    def _note(self, operation: str, element: UiaElement) -> None:
        self.calls.append(operation)
        self.ledger.record(
            f"uia.{operation}",
            Identity(element.window_handle, element.pid),
            bound=Identity(BOUND.handle, BOUND.pid),
            kind="input",
            channel="uia",
        )

    def set_value(self, element: UiaElement, text: str) -> None:
        self._note("SetValue", element)
        self.values[element.runtime_id] = text

    def invoke(self, element: UiaElement) -> None:
        self._note("Invoke", element)

    def select(self, element: UiaElement) -> None:
        self._note("Select", element)

    def toggle(self, element: UiaElement) -> None:
        self._note("Toggle", element)


class UiaContainmentTests(unittest.IsolatedAsyncioTestCase):
    def _target(self, **kwargs: Any) -> UiaTarget:
        base: dict[str, Any] = {
            "window_handle": BOUND.handle,
            "window_pid": BOUND.pid,
            "name": "Message",
        }
        base.update(kwargs)
        return UiaTarget(**base)

    async def test_set_value_generates_no_keyboard_event(self) -> None:
        backend = SimulatedUiaBackend(elements=(_element(),))
        result = await UiaAdapter(backend).set_text(self._target(), TEXT)
        self.assertTrue(result.ok)
        self.assertEqual(result.keyboard_events, 0)
        self.assertEqual(backend.ledger.foreign(Identity(BOUND.handle, BOUND.pid)), [])
        self.assertEqual([item.event for item in backend.ledger.events], ["uia.SetValue"])

    async def test_a_canvas_without_the_pattern_is_unsupported_not_typed_into(self) -> None:
        backend = SimulatedUiaBackend(elements=(_element(patterns=()),))
        result = await UiaAdapter(backend).set_text(self._target(), TEXT)
        self.assertFalse(result.ok)
        self.assertIs(result.refusal, UiaRefusal.UNSUPPORTED_CAPABILITY)
        self.assertEqual(result.keyboard_events, 0)
        self.assertEqual(backend.ledger.events, [])  # no fallback, no keyboard

    async def test_an_element_owned_by_another_process_is_refused(self) -> None:
        backend = SimulatedUiaBackend(elements=(_element(pid=OTHER.pid, handle=OTHER.handle),))
        result = await UiaAdapter(backend).set_text(self._target(), TEXT)
        self.assertIs(result.refusal, UiaRefusal.OWNERSHIP_MISMATCH)
        self.assertEqual(backend.ledger.events, [])

    async def test_an_ambiguous_match_is_refused(self) -> None:
        backend = SimulatedUiaBackend(
            elements=(_element(runtime=(1, 2)), _element(runtime=(1, 3)))
        )
        result = await UiaAdapter(backend).set_text(self._target(), TEXT)
        self.assertIs(result.refusal, UiaRefusal.AMBIGUOUS_ELEMENT)
        self.assertEqual(backend.ledger.events, [])

    async def test_a_disabled_element_is_not_a_real_interaction(self) -> None:
        backend = SimulatedUiaBackend(elements=(_element(enabled=False),))
        result = await UiaAdapter(backend).set_text(self._target(), TEXT)
        self.assertIs(result.refusal, UiaRefusal.ELEMENT_UNAVAILABLE)

    async def test_a_stale_element_is_refused_before_interaction(self) -> None:
        backend = SimulatedUiaBackend(elements=(_element(),), stale_after_first=True)
        result = await UiaAdapter(backend).set_text(self._target(), TEXT)
        self.assertIs(result.refusal, UiaRefusal.STALE_ELEMENT)
        self.assertEqual(backend.ledger.events, [])

    async def test_a_missing_element_is_stale_not_a_guess(self) -> None:
        backend = SimulatedUiaBackend(elements=())
        result = await UiaAdapter(backend).set_text(self._target(), TEXT)
        self.assertIs(result.refusal, UiaRefusal.STALE_ELEMENT)

    async def test_no_backend_reports_unavailable_without_fallback(self) -> None:
        adapter = UiaAdapter(backend=None)
        self.assertFalse(adapter.availability()["available"])
        result = await adapter.set_text(self._target(), TEXT)
        self.assertIs(result.refusal, UiaRefusal.BACKEND_UNAVAILABLE)
        self.assertEqual(result.keyboard_events, 0)

    async def test_a_window_without_a_control_selector_is_unsupported(self) -> None:
        backend = SimulatedUiaBackend(elements=(_element(),))
        result = await UiaAdapter(backend).set_text(
            self._target(name="", automation_id=""), TEXT
        )
        self.assertIs(result.refusal, UiaRefusal.UNSUPPORTED_CAPABILITY)

    async def test_the_controller_uia_profile_refuses_instead_of_typing(self) -> None:
        backend = SimulatedUiaBackend(elements=(_element(patterns=()),))
        runner = LocalDesktopRunner(windows_type_paste=True, uia_backend=backend)
        with self.assertRaises(PasteContainmentError) as raised:
            await runner._type_text(
                TEXT,
                profile=ContainmentProfile.UIA,
                uia_target=self._target(),
            )
        record = raised.exception.dispatch_record
        self.assertEqual(record["containment"], ContainmentProfile.UIA.value)
        self.assertEqual(backend.ledger.events, [])

    async def test_the_controller_uia_profile_records_the_element_and_operation(self) -> None:
        backend = SimulatedUiaBackend(elements=(_element(),))
        runner = LocalDesktopRunner(windows_type_paste=True, uia_backend=backend)
        output = await runner._type_text(
            TEXT, profile=ContainmentProfile.UIA, uia_target=self._target()
        )
        self.assertEqual(output["containment"], ContainmentProfile.UIA.value)
        self.assertEqual(output["keyboard_events"], 0)
        self.assertEqual(output["uia"]["operation"], "set_value")
        self.assertEqual(output["destination_identity"], [BOUND.handle, BOUND.pid])


# ---------------------------------------------------------------------------
# Isolated-session profile
# ---------------------------------------------------------------------------


class FakeSessionTransport:
    def __init__(self, *, isolated: bool = True) -> None:
        self.isolated = isolated
        self.events: list[str] = []
        self.closed = False
        self.started = False

    async def start(self) -> SessionIdentity:
        self.started = True
        return SessionIdentity(
            session_id=7,
            name="nova-isolated",
            transport="fake-rdp",
            isolated=self.isolated,
            started_at="2026-11-27T00:00:00+00:00",
        )

    async def dispatch(self, event: str) -> None:
        self.events.append(event)

    async def shutdown(self) -> None:
        self.closed = True


class IsolatedSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_without_a_transport_the_blocker_is_precise(self) -> None:
        report = IsolatedSessionAdapter(transport=None, platform_name="Windows").feasibility()
        self.assertFalse(report["supported"])
        codes = [item["code"] for item in report["blockers"]]
        self.assertIn("no_session_transport", codes)
        blocker = next(
            item for item in report["blockers"] if item["code"] == "no_session_transport"
        )
        self.assertIn("interactive session cannot stand in", blocker["detail"])
        self.assertTrue(blocker["dependency"])
        # The report never claims the user's session is an isolated surface.
        self.assertTrue(report["interactive_session_is_not_isolation"])

    async def test_start_without_a_transport_raises_the_blocker(self) -> None:
        adapter = IsolatedSessionAdapter(transport=None, platform_name="Windows")
        with self.assertRaises(SessionUnavailable) as raised:
            await adapter.start()
        self.assertEqual(raised.exception.blocker.code, "no_session_transport")
        self.assertIs(adapter.state, SessionState.BLOCKED)

    async def test_a_transport_that_is_not_isolated_is_refused(self) -> None:
        transport = FakeSessionTransport(isolated=False)
        adapter = IsolatedSessionAdapter(transport=transport, platform_name="Windows")
        with self.assertRaises(SessionUnavailable) as raised:
            await adapter.start()
        self.assertEqual(raised.exception.blocker.code, "transport_not_isolated")
        self.assertTrue(transport.closed)
        self.assertEqual(transport.events, [])

    async def test_events_go_only_to_the_isolated_session(self) -> None:
        transport = FakeSessionTransport()
        adapter = IsolatedSessionAdapter(transport=transport, platform_name="Windows")
        identity = await adapter.start()
        self.assertTrue(identity.isolated)
        for event in ("ctrl_down", "v_down", "v_up", "ctrl_up"):
            sent = await adapter.dispatch(event)
            self.assertTrue(sent["dispatched"])
        self.assertEqual(transport.events, ["ctrl_down", "v_down", "v_up", "ctrl_up"])
        # The interactive session's ledger stays empty: nothing was sent there.
        self.assertEqual(adapter.events_dispatched, 4)

    async def test_emergency_stop_refuses_every_later_event(self) -> None:
        transport = FakeSessionTransport()
        adapter = IsolatedSessionAdapter(transport=transport, platform_name="Windows")
        await adapter.start()
        await adapter.dispatch("v_down")
        stopped = await adapter.emergency_stop()
        self.assertTrue(stopped["stopped"])
        refused = await adapter.dispatch("v_up")
        self.assertFalse(refused["dispatched"])
        self.assertEqual(refused["reason"], "emergency_stop")
        self.assertEqual(transport.events, ["v_down"])

    async def test_teardown_closes_the_session_once(self) -> None:
        transport = FakeSessionTransport()
        adapter = IsolatedSessionAdapter(transport=transport, platform_name="Windows")
        await adapter.start()
        first = await adapter.teardown()
        second = await adapter.teardown()
        self.assertTrue(first["closed"])
        self.assertTrue(second["closed"])
        self.assertTrue(transport.closed)
        self.assertIs(adapter.state, SessionState.CLOSED)
        events = [entry["event"] for entry in adapter.audit]
        self.assertIn("session.ready", events)
        self.assertEqual(events.count("session.closed"), 1)

    def test_the_mechanism_table_rejects_hidden_desktops_and_postmessage(self) -> None:
        report = IsolatedSessionAdapter(transport=None, platform_name="Windows").feasibility()
        unsuitable = {
            item["mechanism"]: item["note"]
            for item in report["mechanisms"]
            if not item["suitable"]
        }
        self.assertTrue(any("CreateDesktop" in name for name in unsuitable))
        self.assertTrue(any("Hidden desktop" in name for name in unsuitable))
        self.assertTrue(any("PostMessage" in name for name in unsuitable))


if __name__ == "__main__":
    unittest.main()
