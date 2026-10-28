"""NF-08: observation, the decision policy and the bounded closed loop.

The loop is tested against stubs for the session and the observation builder so
each STOP has exactly one cause; the observation and manager layers are tested
against the REAL classes with fake desktop seams, because the honesty rules
(status, freshness, OCR confidence) live in that code, not in a stub.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from PIL import Image

from novacontrol.desktop.input import WindowGeometry
from novacontrol.novaforge import (
    ActionPolicy,
    ClosedLoopRunner,
    GameObservation,
    HarnessReadyPolicy,
    LoopLimits,
    MissionValidationError,
    NovaForgeManager,
    ObservationBuilder,
    ObservationHistory,
    OcrReading,
    OcrUnavailable,
    PolicyUnavailable,
    ScreenTextReader,
    UnavailablePolicy,
    summarise,
)

WINDOW_TITLE = "NC FORGE HARNESS - idle"
HANDLE = 0x1234
PID = 4242


# ── fixtures ──────────────────────────────────────────────────────────────


def geometry(*, title: str = WINDOW_TITLE) -> WindowGeometry:
    return WindowGeometry(
        handle=HANDLE,
        title=title,
        bounds=(100, 50, 1100, 650),
        client=(0, 0, 1000, 600),
        pid=PID,
        scale=1.0,
        foreground=True,
    )


def observation(
    *,
    status: str = "observed",
    title: str = WINDOW_TITLE,
    text: str = "WAITING FOR INPUT",
    reason: str = "",
) -> GameObservation:
    return GameObservation(
        observed_at="2026-10-21T10:00:00+00:00",
        status=status,
        target={"handle": HANDLE, "pid": PID, "title": title},
        ocr=OcrReading(text=text, words=({"text": text, "line": 0},) if text else ()),
        reason=reason or f"status {status}",
    )


class FakeReader:
    """The OCR engine, as the observation builder sees it."""

    def __init__(self, words: Any = None, *, available: bool = True, raises: Exception | None = None):
        self.words = words if words is not None else [
            {"text": "WAITING", "line": 0, "x": 1, "y": 2, "width": 3, "height": 4},
            {"text": "INPUT", "line": 0, "x": 6, "y": 2, "width": 3, "height": 4},
        ]
        self._available = available
        self.raises = raises
        self.calls: list[str] = []
        self.name = "fake.ocr"
        self.confidence = None

    @property
    def available(self) -> bool:
        return self._available

    async def read(self, source: str) -> Any:
        self.calls.append(source)
        if self.raises is not None:
            raise self.raises
        return self.words


def observer(
    *,
    captured: dict[str, Any] | None = None,
    reader: Any = "default",
    probe: Any = "default",
) -> ObservationBuilder:
    async def window(_handle: int) -> Any:
        return geometry() if probe == "default" else probe

    async def capture(_title: str) -> dict[str, Any]:
        if captured is not None:
            return captured
        return {
            "path": "C:/frames/000001.png",
            "fresh": True,
            "captured_at": "2026-10-21T10:00:00+00:00",
            "age_seconds": 0.2,
            "width": 1000,
            "height": 600,
            "source": "window_crop",
        }

    ocr = FakeReader() if reader == "default" else reader
    return ObservationBuilder(
        probe=window,
        capture=capture,
        ocr=ocr,
        max_frame_age=2.0,
        probe_timeout=5.0,
        ocr_timeout=5.0,
    )


# ── observation ───────────────────────────────────────────────────────────


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_fresh_frame_with_readable_text_is_actionable(self) -> None:
        built = await observer().observe(HANDLE, pid=PID)
        self.assertEqual(built.status, "observed")
        self.assertTrue(built.actionable)
        self.assertEqual(built.title, WINDOW_TITLE)
        self.assertEqual(built.ocr.text if built.ocr else "", "WAITING INPUT")
        self.assertIsNone(built.confidence, "the fake engine reports no confidence")
        methods = [item["method"] for item in built.evidence]
        self.assertEqual(methods, ["window_probe", "frame", "ocr"])
        self.assertIn("No vision model is wired", " ".join(built.uncertainty))

    async def test_a_stale_frame_is_never_actionable(self) -> None:
        builder = observer(
            captured={
                "path": "C:/frames/000001.png",
                "fresh": True,
                "captured_at": "2026-10-21T09:00:00+00:00",
                "age_seconds": 30.0,
            }
        )
        built = await builder.observe(HANDLE)
        self.assertEqual(built.status, "stale")
        self.assertFalse(built.actionable)
        self.assertIn("30.0 s old", built.uncertainty[0])

    async def test_a_capture_without_an_age_is_not_fresh(self) -> None:
        builder = observer(captured={"path": "C:/frames/000001.png"})
        built = await builder.observe(HANDLE)
        self.assertEqual(built.status, "stale")
        self.assertFalse(built.actionable)

    async def test_no_readable_text_and_no_title_is_degraded_with_the_reason(self) -> None:
        built = await observer(
            reader=FakeReader(words=[]), probe=geometry(title="")
        ).observe(HANDLE)
        self.assertEqual(built.status, "degraded")
        self.assertFalse(built.actionable)
        self.assertTrue(any("recognised no text" in item for item in built.uncertainty))

    async def test_a_title_alone_is_actionable_when_no_text_was_recognised(self) -> None:
        built = await observer(reader=FakeReader(words=[])).observe(HANDLE)
        self.assertEqual(built.status, "observed")
        self.assertIn("rests on the title alone", built.reason)
        self.assertEqual(built.ocr.text if built.ocr else "", "")

    async def test_an_unavailable_engine_is_reported_rather_than_blank(self) -> None:
        reader = FakeReader(available=False)
        built = await observer(reader=reader).observe(HANDLE)
        self.assertEqual(reader.calls, [], "an unavailable engine was still asked to read")
        self.assertTrue(any("reports itself unavailable" in item for item in built.uncertainty))

    async def test_a_reader_that_cannot_read_is_reported(self) -> None:
        built = await observer(reader=FakeReader(raises=OcrUnavailable("no pack"))).observe(HANDLE)
        self.assertEqual(built.status, "observed", "the readable title is still evidence")
        self.assertTrue(any("no pack" in item for item in built.uncertainty))

    async def test_a_missing_window_is_unavailable(self) -> None:
        built = await observer(probe=None).observe(HANDLE)
        self.assertEqual(built.status, "unavailable")
        self.assertFalse(built.actionable)
        self.assertEqual(built.evidence, ())

    async def test_without_a_capture_seam_the_observation_says_so(self) -> None:
        builder = ObservationBuilder(probe=lambda handle: _geometry(handle), capture=None)
        built = await builder.observe(HANDLE)
        self.assertEqual(built.status, "unavailable")
        self.assertIn("cannot capture", built.reason)

    async def test_changes_compare_against_the_previous_observation(self) -> None:
        builder = observer()
        first = await builder.observe(HANDLE)
        async def moved(_title: str) -> dict[str, Any]:
            return {
                "path": "C:/frames/000002.png",
                "fresh": True,
                "captured_at": "2026-10-21T10:00:02+00:00",
                "age_seconds": 0.1,
            }
        builder.capture = moved
        builder.ocr = FakeReader(words=[{"text": "RECEIVED 1 KEY", "line": 0}])
        async def retitled(_handle: int) -> Any:
            return geometry(title="NC FORGE HARNESS - READY")
        builder.probe = retitled
        second = await builder.observe(HANDLE, previous=first)
        self.assertTrue(any(item.startswith("title changed") for item in second.changes))
        self.assertIn("recognised text changed", second.changes)

    async def test_capability_names_what_is_missing(self) -> None:
        builder = ObservationBuilder(probe=lambda handle: _geometry(handle), capture=None)
        capability = builder.capability()
        self.assertFalse(capability["observation"]["available"])
        self.assertFalse(capability["interpretation"]["ocr"])
        self.assertIn("No OCR engine", capability["interpretation"]["reason"])

    async def test_summary_is_a_bounded_view(self) -> None:
        built = await observer().observe(HANDLE)
        summary = summarise(built, limit=4)
        self.assertEqual(summary["title"], WINDOW_TITLE[:4])
        self.assertEqual(summary["status"], "observed")
        self.assertTrue(summary["frame_age_seconds"] is not None)

    async def test_history_is_bounded_newest_last(self) -> None:
        history = ObservationHistory(limit=2)
        for index in range(3):
            history.add(observation(title=f"w{index}"))
        self.assertEqual([item.title for item in history.items], ["w1", "w2"])
        self.assertEqual(history.latest.title, "w2")


async def _geometry(_handle: int) -> Any:
    return geometry()


# ── policy ────────────────────────────────────────────────────────────────


class HarnessPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_proposes_one_tap_while_the_marker_is_absent(self) -> None:
        policy = HarnessReadyPolicy()
        proposal = await policy.propose(observation())
        assert proposal is not None
        self.assertEqual(proposal.action, {"kind": "key", "key": "space", "duration_ms": 60})
        self.assertIn("does not contain", proposal.reason)
        self.assertEqual(proposal.expects, {"title_contains": "READY"})
        self.assertEqual(proposal.policy, "HarnessReadyPolicy")
        self.assertTrue(any(ref.startswith("observation:") for ref in proposal.evidence))

    async def test_it_stops_once_the_marker_is_present(self) -> None:
        policy = HarnessReadyPolicy()
        self.assertIsNone(await policy.propose(observation(title="NC FORGE HARNESS - READY")))
        self.assertIn("verifier", policy.last_reason)

    async def test_it_never_acts_on_an_untrusted_observation(self) -> None:
        policy = HarnessReadyPolicy()
        self.assertIsNone(await policy.propose(observation(status="stale")))
        self.assertIn("stale", policy.last_reason)

    async def test_it_stops_when_repeating_would_be_a_loop(self) -> None:
        policy = HarnessReadyPolicy(stall_limit=2)
        history = [observation(), observation()]
        self.assertIsNone(await policy.propose(observation(), history=history))
        self.assertIn("no change", policy.last_reason)

    async def test_a_changed_history_is_not_a_stall(self) -> None:
        policy = HarnessReadyPolicy(stall_limit=2)
        history = [observation(text="A"), observation(text="B")]
        self.assertIsNotNone(await policy.propose(observation(text="C"), history=history))

    async def test_its_capability_does_not_claim_intelligence(self) -> None:
        capability = HarnessReadyPolicy().capability()
        self.assertFalse(capability["model_backed"])
        self.assertIn("not evidence of gameplay intelligence", capability["reason"])

    async def test_the_unavailable_policy_refuses_explicitly(self) -> None:
        with self.assertRaises(PolicyUnavailable):
            await UnavailablePolicy().propose(observation())
        self.assertFalse(UnavailablePolicy().available)


# ── closed loop ───────────────────────────────────────────────────────────


class StubObservations:
    def __init__(self, *items: GameObservation) -> None:
        self.queue = list(items)
        self.calls: list[dict[str, Any]] = []

    def capability(self) -> dict[str, Any]:
        return {
            "observation": {"available": True, "capture": True, "ocr": True},
            "interpretation": {"available": True, "ocr": True, "vision_model": False},
        }

    async def observe(self, handle: int, **kwargs: Any) -> GameObservation:
        self.calls.append({"handle": handle, **kwargs})
        if not self.queue:
            return observation(status="unavailable", reason="no more observations")
        return self.queue.pop(0)


class FakeGateway:
    def __init__(
        self,
        *,
        record: Any = None,
        plan: dict[str, Any] | None = None,
        execute: dict[str, Any] | None = None,
        verdict: dict[str, Any] | None = None,
    ) -> None:
        self.record = record
        self.plan = plan or {
            "status": "proposed",
            "proposal": {"proposal_id": "p1", "digest": "d1"},
            "remaining_actions": 7,
        }
        self.execute = execute or {"status": "executed"}
        self.verdict = verdict or {"outcome": "unknown", "reason": "no verdict yet"}
        self.plan_calls: list[dict[str, Any]] = []
        self.execute_calls: list[dict[str, Any]] = []
        self.verify_calls: list[str] = []

    def get(self, session_id: str) -> Any:
        return self.record if session_id == "s1" else None

    def plan_action(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.plan_calls.append(payload)
        return self.plan

    async def execute_action(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.execute_calls.append(payload)
        return self.execute

    async def verify_objective(self, session_id: str, *, reason: str = "manual", action_id: str = "") -> dict[str, Any]:
        self.verify_calls.append(reason)
        return self.verdict


def session(*, pinned: bool = True) -> SimpleNamespace:
    target = SimpleNamespace(handle=HANDLE, pid=PID, title=WINDOW_TITLE) if pinned else None
    return SimpleNamespace(target=target, objective=None)


def runner(
    gateway: FakeGateway,
    observations: StubObservations,
    *,
    policy: Any = None,
    limits: LoopLimits | None = None,
    cancel: asyncio.Event | None = None,
) -> ClosedLoopRunner:
    return ClosedLoopRunner(
        gateway=gateway,
        observations=observations,
        policy=policy or HarnessReadyPolicy(),
        limits=limits,
        cancel=cancel,
    )


class ClosedLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_verified_objective_completes_the_run(self) -> None:
        gateway = FakeGateway(
            record=session(),
            verdict={"outcome": "verified_success", "reason": "the screen showed READY"},
        )
        result = await runner(gateway, StubObservations()).run("s1")
        self.assertTrue(result.completed)
        self.assertEqual(result.reason, "the screen showed READY")
        self.assertEqual(result.actions_executed, 0)
        self.assertEqual(gateway.plan_calls, [])
        self.assertEqual(gateway.verify_calls, ["loop_precheck"])

    async def test_a_precheck_failure_is_the_starting_state_not_a_verdict(self) -> None:
        class StartingStateGateway(FakeGateway):
            def __init__(self) -> None:
                super().__init__(record=session())
                self.verdicts = [
                    {"outcome": "verified_failure", "reason": "READY is not on screen yet"},
                    {"outcome": "unknown", "reason": "not yet"},
                    {"outcome": "verified_success", "reason": "READY"},
                ]

            async def verify_objective(self, session_id: str, *, reason: str = "manual", action_id: str = "") -> dict[str, Any]:
                self.verify_calls.append(reason)
                return self.verdicts.pop(0)

        gateway = StartingStateGateway()
        result = await runner(
            gateway,
            StubObservations(
                observation(),
                observation(title="NC FORGE HARNESS - READY"),
                observation(title="NC FORGE HARNESS - READY"),
            ),
        ).run("s1")
        self.assertTrue(result.completed, result.reason)
        self.assertEqual(result.actions_executed, 1)
        self.assertEqual(result.verification["outcome"], "verified_success")

    async def test_a_failure_after_acting_is_final_and_not_retried(self) -> None:
        class FailingGateway(FakeGateway):
            def __init__(self) -> None:
                super().__init__(record=session())
                self.verdicts = [
                    {"outcome": "verified_failure", "reason": "not yet"},
                    {"outcome": "unknown", "reason": "not yet"},
                    {"outcome": "verified_failure", "reason": "the screen still showed GAME OVER"},
                ]

            async def verify_objective(self, session_id: str, *, reason: str = "manual", action_id: str = "") -> dict[str, Any]:
                self.verify_calls.append(reason)
                return self.verdicts.pop(0)

        gateway = FailingGateway()
        observations = StubObservations(
            observation(),
            observation(text="GAME OVER"),
            observation(text="GAME OVER"),
        )
        result = await runner(gateway, observations).run("s1")
        self.assertEqual(result.status, "failed")
        self.assertIn("GAME OVER", result.reason)
        self.assertEqual(result.actions_executed, 1)
        self.assertEqual(len(gateway.plan_calls), 1, "a decided failure was retried")

    async def test_one_observed_step_acts_through_the_gateway_then_verifies(self) -> None:
        class SequencedGateway(FakeGateway):
            def __init__(self) -> None:
                super().__init__(record=session())
                self.verdicts = [
                    {"outcome": "unknown", "reason": "not yet"},
                    {"outcome": "unknown", "reason": "still not yet"},
                    {"outcome": "verified_success", "reason": "READY"},
                ]

            async def verify_objective(self, session_id: str, *, reason: str = "manual", action_id: str = "") -> dict[str, Any]:
                self.verify_calls.append(reason)
                return self.verdicts.pop(0)

        gateway = SequencedGateway()
        observations = StubObservations(
            observation(),
            observation(title="NC FORGE HARNESS - READY"),
            observation(title="NC FORGE HARNESS - READY"),
        )
        result = await runner(gateway, observations).run("s1")
        self.assertTrue(result.completed)
        self.assertEqual(result.actions_executed, 1)
        self.assertEqual(gateway.plan_calls[0]["kind"], "key")
        self.assertEqual(gateway.execute_calls[0], {"proposal_id": "p1", "digest": "d1"})
        # Two decision rounds were recorded: the acting one and the observation
        # that was verified before any further proposal.
        self.assertEqual(len(result.steps), 2)
        self.assertIsNone(result.steps[1].proposal)
        step = result.steps[0]
        self.assertTrue(step.executed)
        self.assertEqual(step.plan_status, "proposed")
        self.assertIs(step.outcome_met, True)
        self.assertEqual(step.remaining_actions, 7)
        self.assertEqual(result.verification["outcome"], "verified_success")

    async def test_a_refused_plan_stops_the_run_without_executing(self) -> None:
        gateway = FakeGateway(
            record=session(),
            verdict={"outcome": "unknown", "reason": "not yet"},
            plan={"status": "denied", "outcome": {"reason": "the target is not in the foreground"}},
        )
        result = await runner(gateway, StubObservations(observation())).run(
            "s1", verify_before_first_action=False
        )
        self.assertEqual(result.status, "refused")
        self.assertIn("foreground", result.reason)
        self.assertEqual(result.actions_refused, 1)
        self.assertEqual(gateway.execute_calls, [])

    async def test_an_action_that_did_not_execute_is_a_refusal(self) -> None:
        gateway = FakeGateway(
            record=session(),
            verdict={"outcome": "unknown", "reason": "not yet"},
            execute={"status": "failed", "outcome": {"reason": "the input guard refused"}},
        )
        result = await runner(gateway, StubObservations(observation())).run(
            "s1", verify_before_first_action=False
        )
        self.assertEqual(result.status, "refused")
        self.assertIn("input guard", result.reason)
        self.assertEqual(result.actions_executed, 0)

    async def test_a_stale_observation_stops_before_any_proposal(self) -> None:
        gateway = FakeGateway(record=session(), verdict={"outcome": "unknown", "reason": "?"})
        result = await runner(gateway, StubObservations(observation(status="stale"))).run(
            "s1", verify_before_first_action=False
        )
        self.assertEqual(result.status, "stopped")
        self.assertIn("stale", result.reason)
        self.assertEqual(result.decisions, 0)
        self.assertEqual(gateway.plan_calls, [])

    async def test_an_unavailable_first_observation_is_unavailable(self) -> None:
        gateway = FakeGateway(record=session(), verdict={"outcome": "unknown", "reason": "?"})
        result = await runner(gateway, StubObservations(observation(status="unavailable"))).run(
            "s1", verify_before_first_action=False
        )
        self.assertEqual(result.status, "unavailable")

    async def test_a_missing_session_or_unpinned_target_does_nothing(self) -> None:
        missing = await runner(FakeGateway(), StubObservations()).run("s1")
        self.assertEqual(missing.status, "unavailable")
        unpinned = await runner(
            FakeGateway(record=session(pinned=False)), StubObservations()
        ).run("s1")
        self.assertEqual(unpinned.status, "unavailable")
        self.assertIn("pinned", unpinned.reason)

    async def test_the_action_budget_bounds_the_loop(self) -> None:
        gateway = FakeGateway(record=session(), verdict={"outcome": "unknown", "reason": "?"})
        # Each observation must CHANGE, or the policy's own stall guard stops the
        # loop first and the budget is never reached; that guard is tested above.
        observations = StubObservations(
            *[observation(text=f"frame {index}") for index in range(6)]
        )
        result = await runner(
            gateway, observations, limits=LoopLimits(max_actions=2, deadline_seconds=30)
        ).run("s1", verify_before_first_action=False)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.actions_executed, 2)
        self.assertIn("budget", result.reason)

    async def test_a_cancelled_loop_stops_before_the_next_action(self) -> None:
        cancel = asyncio.Event()
        cancel.set()
        gateway = FakeGateway(record=session(), verdict={"outcome": "unknown", "reason": "?"})
        result = await runner(gateway, StubObservations(), cancel=cancel).run(
            "s1", verify_before_first_action=False
        )
        self.assertEqual(result.status, "canceled")
        self.assertEqual(gateway.execute_calls, [])

    async def test_a_policy_with_nothing_to_say_stops_with_its_reason(self) -> None:
        class SilentPolicy:
            name = "SilentPolicy"
            available = True
            last_reason = "nothing honest is left"

            async def propose(self, observation: GameObservation, *, history: Any = ()) -> Any:
                return None

        gateway = FakeGateway(record=session(), verdict={"outcome": "unknown", "reason": "?"})
        result = await runner(gateway, StubObservations(observation()), policy=SilentPolicy()).run(
            "s1", verify_before_first_action=False
        )
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.reason, "nothing honest is left")

    async def test_a_verifier_timeout_is_reported_and_never_decides(self) -> None:
        class SlowGateway(FakeGateway):
            async def verify_objective(self, session_id: str, *, reason: str = "manual", action_id: str = "") -> dict[str, Any]:
                await asyncio.sleep(0.5)
                return {"outcome": "verified_success"}

        gateway = SlowGateway(record=session())
        limits = LoopLimits(max_actions=1, deadline_seconds=5, verify_timeout_seconds=0.01)
        result = await runner(gateway, StubObservations(observation()), limits=limits).run(
            "s1", verify_before_first_action=False
        )
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.verification["outcome"], "verification_error")
        self.assertIn("did not answer", result.verification["reason"])

    async def test_an_unavailable_policy_is_named_not_simulated(self) -> None:
        gateway = FakeGateway(record=session())
        result = await runner(gateway, StubObservations(), policy=UnavailablePolicy()).run("s1")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("No decision policy", result.reason)
        self.assertEqual(gateway.verify_calls, [])

    async def test_the_result_payload_separates_completion_from_incident(self) -> None:
        gateway = FakeGateway(
            record=session(),
            verdict={"outcome": "verified_success", "reason": "READY"},
        )
        result = await runner(gateway, StubObservations()).run("s1")
        payload = result.to_dict()
        self.assertEqual(payload["status"], "completed")
        self.assertIn("does not mean the plan ran without incident", payload["note"])
        self.assertEqual(payload["limits"]["max_actions"], 8)

    async def test_invalid_limits_are_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            runner(FakeGateway(), StubObservations(), limits=LoopLimits(max_actions=0))

    async def test_its_capability_names_the_safety_path_and_the_stops(self) -> None:
        capability = runner(FakeGateway(), StubObservations()).capability()["closed_loop"]
        self.assertTrue(capability["available"])
        self.assertFalse(capability["policy"]["model_backed"])
        self.assertTrue(capability["safety"]["guarded_actions"])
        self.assertIn("the observation is stale, degraded or unavailable", capability["stopping"])


# ── the manager's OCR seam, against the real capture/crop path ────────────


class FakeDesktop:
    def __init__(self) -> None:
        self.window: WindowGeometry | None = geometry()
        self.captures = 0

    async def capture(self, path: str) -> dict[str, Any]:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 800), (12, 18, 30)).save(path, format="PNG")
        self.captures += 1
        return {
            "ok": True,
            "path": path,
            "fresh": True,
            "width": 1000,
            "height": 600,
            "elapsed_ms": 1.0,
            "size_bytes": 72,
            "source": "window_crop",
            "mode": "window",
        }

    async def window_lines(self) -> list[str] | None:
        return [f"python.exe|{WINDOW_TITLE}"]

    async def geometry_for(self, _title: str) -> WindowGeometry | None:
        return self.window


class ManagerOcrTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.desktop = FakeDesktop()
        self.words = [
            {"text": "RECEIVED", "line": 0, "x": 10, "y": 10, "w": 80, "h": 20},
            {"text": "1", "line": 0, "x": 95, "y": 10, "w": 10, "h": 20},
            {"text": "KEY(S)", "line": 0, "x": 110, "y": 10, "w": 55, "h": 20},
        ]

        async def probe(_handle: int) -> Any:
            return self.desktop.window

        async def reader(_path: str) -> Any:
            return self.words

        self.manager = NovaForgeManager(
            data_dir=Path(self.temp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry_for,
            window_lines=self.desktop.window_lines,
            release_inputs=self._release,
            objective_probe=probe,
            ocr_reader=reader,
            action_policy=ActionPolicy(),
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=1.0,
            stop_timeout=2.0,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temp.cleanup()

    async def _release(self) -> tuple[int, int]:
        return (0, 0)

    async def start(self) -> Any:
        described = await self.manager.start(
            {
                "name": "OCR probe",
                "objective": "Read the harness",
                "target_title": WINDOW_TITLE,
                "frame_interval_seconds": 0.05,
            }
        )
        record = self.manager.get(described["session_id"])
        for _ in range(40):
            if record is not None and record.target_validated:
                break
            await asyncio.sleep(0.02)
        assert record is not None and record.target_validated
        return record

    async def test_a_fresh_frame_is_read_into_words_without_a_confidence(self) -> None:
        reading = await self.manager.read_screen_text(HANDLE)
        assert reading is not None
        self.assertTrue(reading["available"])
        self.assertTrue(reading["fresh"])
        self.assertEqual(reading["text"], "RECEIVED 1 KEY(S)")
        self.assertEqual(reading["words"][0]["width"], 80, "w/h were not normalised")
        self.assertIsNone(reading["confidence"])
        self.assertIs(reading["confidence_reported"], False)

    async def test_an_absent_engine_reports_unavailable(self) -> None:
        async def reader(_path: str) -> Any:
            return None

        self.manager.ocr_reader = reader
        reading = await self.manager.read_screen_text(HANDLE)
        assert reading is not None
        self.assertFalse(reading["available"])
        self.assertIn("unavailable", reading["reason"])

    async def test_a_vanished_window_yields_no_reading(self) -> None:
        self.desktop.window = None
        self.assertIsNone(await self.manager.read_screen_text(HANDLE))

    async def test_capture_frame_refuses_a_window_it_cannot_find(self) -> None:
        self.desktop.window = None
        frame = await self.manager.capture_frame("nothing")
        self.assertFalse(frame["fresh"])
        self.assertIn("No open window", frame["reason"])

    async def test_capture_frame_writes_a_bounded_number_of_observation_frames(self) -> None:
        self.manager.max_observation_frames = 2
        for _ in range(4):
            frame = await self.manager.capture_frame(WINDOW_TITLE)
            self.assertTrue(frame["fresh"])
            await asyncio.sleep(0.01)
        kept = list((Path(self.temp.name) / "novaforge" / "frames" / "observations").glob("*.png"))
        self.assertLessEqual(len(kept), 2)

    async def test_an_ocr_objective_is_verified_through_the_manager(self) -> None:
        record = await self.start()
        configured = self.manager.set_objective(
            record.session_id, {"kind": "ocr_text", "parameter": "received 1 key"}
        )
        self.assertTrue(configured["supported"])
        verdict = await self.manager.verify_objective(record.session_id, reason="ocr")
        self.assertEqual(verdict["outcome"], "verified_success")
        self.assertTrue(verdict["achieved"])
        self.assertEqual(verdict["evidence"][0]["method"], "ocr_text")
        self.assertIn("RECEIVED 1 KEY(S)", verdict["evidence"][0]["observed"])
        kinds = {row["kind"]: row["available"] for row in self.manager.objectives.availability()}
        self.assertTrue(kinds["ocr_text"])
        self.assertTrue(kinds["window_title"])

    async def test_an_ocr_objective_fails_only_on_a_decided_read(self) -> None:
        record = await self.start()
        self.manager.set_objective(record.session_id, {"kind": "ocr_text", "parameter": "GAME OVER"})
        verdict = await self.manager.verify_objective(record.session_id, reason="ocr")
        self.assertEqual(verdict["outcome"], "verified_failure")
        self.assertIs(verdict["achieved"], False)

    async def test_the_decision_capability_is_absent_until_one_is_attached(self) -> None:
        pipeline = self.manager.capabilities()["decision_pipeline"]
        self.assertFalse(pipeline["available"])
        self.assertIn("No planner", pipeline["reason"])


class ManagerLoopWiringTests(unittest.IsolatedAsyncioTestCase):
    """The application-side composition, without an application or a desktop."""

    async def test_the_builder_reads_the_managers_own_seams(self) -> None:
        with TemporaryDirectory() as temp:
            desktop = FakeDesktop()

            async def probe(_handle: int) -> Any:
                return desktop.window

            async def reader(_path: str) -> Any:
                return [{"text": "WAITING", "line": 0, "x": 1, "y": 1, "w": 2, "h": 2}]

            manager = NovaForgeManager(
                data_dir=Path(temp),
                capture=desktop.capture,
                geometry=desktop.geometry_for,
                window_lines=desktop.window_lines,
                objective_probe=probe,
                ocr_reader=reader,
                capture_supported=True,
                frame_interval=0.05,
            )
            try:
                builder = ObservationBuilder(
                    probe=manager.window_probe,
                    capture=manager.capture_frame,
                    ocr=ScreenTextReader(read_text=reader),
                )
                built = await builder.observe(HANDLE, pid=PID)
                self.assertEqual(built.status, "observed")
                self.assertEqual(built.ocr.text if built.ocr else "", "WAITING")
            finally:
                await manager.shutdown()

    async def test_the_application_composes_an_honest_capability(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as temp:
            application = NovaControlApplication(data_dir=Path(temp))
            try:
                capability = application._novaforge_loop_capability()
                self.assertIn("policy", capability)
                self.assertFalse(capability["policy"]["model_backed"])
                # Whether it is available depends on this host's desktop seams;
                # when it is not, the reason must name the missing path.
                if not capability["available"]:
                    self.assertTrue(capability["reason"])
            finally:
                await application.stop()
