"""Phase 7: selection by CAPABILITY, loading by MEASURED memory, and the numbers.

The specification's claim is narrow enough to test directly: NovaControl must not
assume one model handles everything, and it must not choose a model by its NAME.
So the tests here are arranged around those two refusals and the things that make
them honest:

  * selection matches a capability requirement against declarations — a name
    appears in a test only as DATA, never as the reason for an answer;
  * a capability nobody declared is ABSENT, and a runtime report outranks both a
    declaration and a name guess in the directions each can be trusted;
  * loading checks memory, inspects what is resident, estimates, frees only what
    is safe, loads, and VERIFIES — and refuses rather than paging;
  * a model an active task is using is never evicted, and switching is avoided
    when the resident model already satisfies the requirement;
  * no accelerator is claimed that was not detected, and "could not measure" is
    reported as unknown rather than as zero;
  * the unified telemetry carries durations, RAM and provenance — and no
    chain-of-thought — through the existing surfaces.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any

from novacontrol.intelligence.telemetry import REQUEST_STAGES, InterpretationTelemetry
from novacontrol.models import (
    HardwareMonitor,
    KeepAlivePolicy,
    KeepAliveSettings,
    ModelCapability,
    ModelManager,
    ModelProfile,
    apply_reported,
    infer_profile,
    select_profile,
)
from novacontrol.models.hardware import BYTES_PER_PARAMETER

GIB = 1024 ** 3

CHAT = "qwen3:8b"
VISION = "qwen3-vl:4b"
TINY = "llama3.2:3b"


class FakeProvider:
    """A runtime that can be told exactly what it has, in memory and on disk."""

    name = "fake"

    def __init__(
        self,
        installed: dict[str, int] | None = None,
        *,
        resident: dict[str, int] | None = None,
        available: int | None = 8 * GIB,
        capabilities: dict[str, tuple[str, ...]] | None = None,
        reachable: bool = True,
        load_ok: bool = True,
        verifies: bool = True,
    ) -> None:
        self.installed = dict(installed or {})
        self.resident = dict(resident or {})
        self.available = available
        self.reported = dict(capabilities or {})
        self.reachable = reachable
        self.load_ok = load_ok
        self.verifies = verifies
        self.loads: list[str] = []
        self.unloads: list[str] = []
        self.probed: list[str] = []

    # -- the protocol ---------------------------------------------------------
    def list_models(self) -> tuple[str, ...]:
        return tuple(self.installed) if self.reachable else ()

    def resident_models(self) -> tuple[tuple[str, int], ...] | None:
        if not self.reachable:
            return None
        return tuple((name, size) for name, size in self.resident.items())

    def load(self, model: str) -> bool:
        self.loads.append(model)
        # A runtime nobody can reach cannot load anything — the fake refuses for
        # the same reason a real one does, so "accepted but unconfirmed" stays a
        # case that has to be constructed on purpose.
        if not self.load_ok or not self.reachable:
            return False
        self.resident[model] = self.installed.get(model, GIB)
        if not self.verifies:
            del self.resident[model]
        return True

    def unload(self, model: str) -> bool:
        self.unloads.append(model)
        return self.resident.pop(model, None) is not None

    def available_memory_bytes(self) -> int | None:
        return self.available

    def model_size_bytes(self, model: str) -> int | None:
        return self.resident.get(model) or self.installed.get(model)

    def capabilities(self, model: str) -> tuple[str, ...] | None:
        self.probed.append(model)
        return self.reported.get(model)


class FakeMonitor(HardwareMonitor):
    """A monitor whose measurements are stated rather than taken."""

    def __init__(self, available: int | None = 8 * GIB, total: int | None = 16 * GIB) -> None:
        super().__init__(resident_models=lambda: ())
        self._available = available
        self._total = total

    def available_ram_bytes(self) -> int | None:
        return self._available

    def total_ram_bytes(self) -> int | None:
        return self._total


def _provider(**kwargs: Any) -> FakeProvider:
    """A provider with the two models this machine actually runs."""
    settings = {
        "installed": {CHAT: 6 * GIB, VISION: 3 * GIB, TINY: 2 * GIB},
        "capabilities": {
            CHAT: ("completion", "tools", "thinking"),
            VISION: ("completion", "vision"),
            TINY: ("completion", "tools"),
        },
    }
    settings.update(kwargs)
    return FakeProvider(**settings)


def _manager(provider: FakeProvider | None = None, **kwargs: Any) -> ModelManager:
    manager = ModelManager(
        provider if provider is not None else _provider(),
        monitor=kwargs.pop("monitor", FakeMonitor()),
        **kwargs,
    )
    return manager


# ── the capability registry ──────────────────────────────────────────────────


class CapabilityRegistryTests(unittest.TestCase):
    """What a model can DO is the whole basis of a selection."""

    def test_every_named_capability_is_selectable_as_data(self) -> None:
        """Each capability in the vocabulary has something on this machine."""
        manager = _manager()
        manager.refresh_capabilities()
        for capability in ModelCapability:
            selection = manager.select({capability})
            with self.subTest(capability=capability.value):
                if capability in {ModelCapability.EMBEDDINGS}:
                    # Declared but not installed: nothing local satisfies it, and
                    # the honest answer is "none", not a chat model standing in.
                    self.assertFalse(selection.found)
                    continue
                self.assertTrue(selection.found)

    def test_a_capability_nobody_declared_is_absent_not_guessed(self) -> None:
        """The floor that keeps a text model from being asked to see."""
        text_only = ModelProfile(
            name="some-text-model:7b",
            capabilities=frozenset({ModelCapability.TEXT}),
            provider="ollama",
        )
        match = select_profile({ModelCapability.VISION}, [text_only])
        self.assertFalse(match.found)
        self.assertEqual(
            match.rejected["some-text-model:7b"], "declares no vision"
        )

    def test_selection_is_not_a_model_name_lookup(self) -> None:
        """Renaming the models changes nothing about which one is selected."""
        renamed = [
            ModelProfile(
                name="alpha",
                capabilities=frozenset({ModelCapability.TEXT, ModelCapability.REASONING}),
                provider="ollama",
                parameters_b=8.0,
            ),
            ModelProfile(
                name="beta",
                capabilities=frozenset({ModelCapability.TEXT, ModelCapability.VISION}),
                provider="ollama",
                parameters_b=3.0,
            ),
        ]
        reasoning = select_profile({ModelCapability.REASONING}, renamed)
        vision = select_profile({ModelCapability.VISION}, renamed)
        self.assertEqual(reasoning.profile.name, "alpha")
        self.assertEqual(vision.profile.name, "beta")

    def test_the_smallest_fitting_model_wins_and_quality_flips_it(self) -> None:
        small = ModelProfile(
            name="small", capabilities=frozenset({ModelCapability.TEXT}), parameters_b=3.0
        )
        large = ModelProfile(
            name="large", capabilities=frozenset({ModelCapability.TEXT}), parameters_b=70.0
        )
        cheap = select_profile({ModelCapability.TEXT}, [large, small])
        best = select_profile(
            {ModelCapability.TEXT}, [large, small], latency_preference="quality"
        )
        self.assertEqual(cheap.profile.name, "small")
        self.assertEqual(best.profile.name, "large")

    def test_a_model_that_does_not_fit_is_rejected_with_that_reason(self) -> None:
        huge = ModelProfile(
            name="huge",
            capabilities=frozenset({ModelCapability.TEXT}),
            memory_bytes=20 * GIB,
        )
        match = select_profile(
            {ModelCapability.TEXT}, [huge], available_memory_bytes=8 * GIB
        )
        self.assertFalse(match.found)
        self.assertEqual(match.rejected["huge"], "needs more memory than is free")

    def test_an_unmeasured_model_is_not_rejected_for_being_unmeasured(self) -> None:
        unknown_size = ModelProfile(
            name="unmeasured", capabilities=frozenset({ModelCapability.TEXT})
        )
        match = select_profile(
            {ModelCapability.TEXT}, [unknown_size], available_memory_bytes=1 * GIB
        )
        self.assertTrue(match.found)


class RuntimeReportedCapabilityTests(unittest.TestCase):
    """A measurement outranks a declaration, and a declaration outranks a guess."""

    def test_a_reported_capability_is_added(self) -> None:
        guessed = infer_profile("mystery-model:7b")
        confirmed = apply_reported(guessed, ["completion", "vision"])
        self.assertIn(ModelCapability.VISION, confirmed.capabilities)
        self.assertTrue(confirmed.runtime_confirmed)
        self.assertEqual(confirmed.reported, frozenset({"completion", "vision"}))

    def test_a_reported_ABSENCE_removes_a_capability_the_name_suggested(self) -> None:
        """The case the whole reconciliation exists for: a text-only 'llava'."""
        guessed = infer_profile("llava")
        self.assertIn(ModelCapability.VISION, guessed.capabilities)
        corrected = apply_reported(guessed, ["completion"])
        self.assertNotIn(ModelCapability.VISION, corrected.capabilities)

    def test_a_runtime_cannot_deny_what_its_vocabulary_cannot_express(self) -> None:
        """Silence about 'coding' is not evidence against it."""
        profile = ModelProfile(
            name="coder:8b",
            capabilities=frozenset({ModelCapability.CODING, ModelCapability.TEXT}),
        )
        reported = apply_reported(profile, ["completion"])
        self.assertIn(ModelCapability.CODING, reported.capabilities)

    def test_silence_changes_nothing(self) -> None:
        profile = infer_profile("mystery-model:7b")
        self.assertEqual(apply_reported(profile, []), profile)
        self.assertEqual(apply_reported(profile, [""]), profile)

    def test_an_undeclared_installed_model_becomes_selectable_by_measurement(self) -> None:
        provider = FakeProvider(
            installed={"brand-new-vl:3b": 2 * GIB},
            capabilities={"brand-new-vl:3b": ("completion", "vision")},
        )
        manager = _manager(provider)
        manager.refresh_capabilities()
        selection = manager.select({"vision"})
        self.assertEqual(selection.model, "brand-new-vl:3b")
        profile = manager.registry.get("brand-new-vl:3b")
        self.assertIsNotNone(profile)
        self.assertFalse(profile.declared)  # still marked as discovered
        self.assertTrue(profile.runtime_confirmed)

    def test_the_runtime_is_asked_once_per_model(self) -> None:
        provider = _provider()
        manager = _manager(provider)
        manager.refresh_capabilities()
        first = len(provider.probed)
        manager.refresh_capabilities()
        self.assertEqual(len(provider.probed), first)
        self.assertEqual(sorted(set(provider.probed)), sorted(provider.probed))

    def test_a_runtime_that_cannot_describe_its_models_keeps_the_declarations(self) -> None:
        provider = _provider(capabilities={})
        manager = _manager(provider)
        manager.refresh_capabilities()
        self.assertEqual(manager.select({"reasoning"}).model, CHAT)
        self.assertEqual(manager.registry.get(CHAT).reported, frozenset())


# ── loading: the specification's six steps ───────────────────────────────────


class RamAwareLoadingTests(unittest.TestCase):
    def test_a_load_refuses_when_the_model_cannot_fit(self) -> None:
        provider = _provider(installed={CHAT: 12 * GIB})
        manager = _manager(provider, monitor=FakeMonitor(available=4 * GIB))
        outcome = manager.load(CHAT)
        self.assertTrue(outcome.refused)
        self.assertFalse(outcome.loaded)
        self.assertEqual(provider.loads, [])
        self.assertIn("more memory than is free", outcome.reason)

    def test_the_six_steps_are_all_recorded(self) -> None:
        provider = _provider(resident={CHAT: 6 * GIB})
        # 2 GB free and a 6 GB resident model: the vision model cannot live
        # beside it, so room has to be made and the whole path is exercised.
        manager = _manager(provider, monitor=FakeMonitor(available=2 * GIB))
        outcome = manager.load(VISION)
        self.assertTrue(outcome.loaded)
        steps = [str(step.get("step")) for step in outcome.steps]
        self.assertEqual(
            steps,
            ["check_memory", "check_resident", "estimate", "evict", "load", "verify"],
        )
        self.assertEqual(outcome.evicted, (CHAT,))
        self.assertEqual(provider.unloads, [CHAT])
        self.assertTrue(outcome.verified)
        estimate = next(s for s in outcome.steps if s.get("step") == "estimate")
        self.assertIs(estimate["fits"], False)
        self.assertIs(estimate["fits_after_evict"], True)
        self.assertEqual(estimate["freed_by_eviction_bytes"], 6 * GIB)

    def test_a_model_that_fits_alongside_is_loaded_without_unloading_anything(self) -> None:
        """Switching is a cost, so it is not paid when the measurement says otherwise."""
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = _manager(provider)  # 8 GB free, the vision model needs 3
        outcome = manager.load(VISION)
        self.assertTrue(outcome.loaded)
        self.assertEqual(outcome.evicted, ())
        self.assertEqual(provider.unloads, [])
        self.assertEqual(manager.telemetry.evictions, 0)
        self.assertIn(CHAT, manager.runtime_status().loaded_models)

    def test_a_load_that_only_fits_after_eviction_is_not_refused(self) -> None:
        """The specification's own switch: make room first, then load.

        Refusing on "does it fit right now" would decline to switch on exactly
        the machine this layer exists for, and the reason would read plausibly
        the whole time.
        """
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = _manager(provider, monitor=FakeMonitor(available=2 * GIB))
        outcome = manager.load(VISION)
        self.assertTrue(outcome.loaded)
        self.assertFalse(outcome.refused)
        self.assertEqual(manager.runtime_status().loaded_models, (VISION,))

    def test_a_verified_load_is_not_the_same_as_an_accepted_one(self) -> None:
        """The runtime accepting a load is a claim; listing the model is proof."""
        provider = _provider(verifies=False)
        manager = _manager(provider)
        outcome = manager.load(CHAT)
        self.assertFalse(outcome.loaded)
        self.assertIs(outcome.verified, False)
        self.assertIn("does not list the model", outcome.reason)
        self.assertEqual(manager.telemetry.verified_failures, 1)

    def test_an_already_resident_model_is_not_loaded_again(self) -> None:
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = _manager(provider)
        outcome = manager.load(CHAT)
        self.assertTrue(outcome.loaded)
        self.assertEqual(provider.loads, [])
        self.assertEqual(manager.telemetry.load_skips, 1)
        self.assertEqual(outcome.reason, "the model was already resident")

    def test_a_model_an_active_task_is_using_is_never_evicted(self) -> None:
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = _manager(provider, monitor=FakeMonitor(available=2 * GIB))
        manager.begin_activity(CHAT)
        outcome = manager.load(VISION)
        self.assertTrue(outcome.refused)
        self.assertIn(CHAT, outcome.reason)
        self.assertEqual(provider.unloads, [])
        self.assertIn(CHAT, manager.runtime_status().loaded_models)
        manager.end_activity(CHAT)
        self.assertTrue(manager.load(VISION).loaded)

    def test_an_in_use_model_does_not_block_a_load_that_fits_beside_it(self) -> None:
        """The protection is about eviction, not about exclusivity for its own sake."""
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = _manager(provider)
        manager.begin_activity(CHAT)
        outcome = manager.load(VISION)
        self.assertTrue(outcome.loaded)
        self.assertEqual(outcome.evicted, ())
        self.assertIn(CHAT, manager.runtime_status().loaded_models)

    def test_an_unreachable_runtime_refuses_rather_than_failing(self) -> None:
        provider = _provider(reachable=False, available=None)
        manager = _manager(provider)
        outcome = manager.load(CHAT)
        self.assertFalse(outcome.loaded)
        # Attempted once through the provider (which is the thing that knows the
        # runtime is down) and reported as a failure, not raised.
        self.assertEqual(provider.loads, [CHAT])
        self.assertEqual(outcome.reason, "the runtime did not accept the load")
        self.assertEqual(manager.runtime_status().loaded_models, ())

    def test_a_load_the_runtime_cannot_confirm_is_not_reported_as_verified(self) -> None:
        """Step 6 failing to run is not the same as step 6 succeeding."""
        provider = _provider()

        class Unverifiable(FakeProvider):
            def resident_models(self) -> None:  # type: ignore[override]
                return None

        unverifiable = Unverifiable(
            provider.installed, capabilities=provider.reported
        )
        outcome = _manager(unverifiable).load(CHAT)
        self.assertTrue(outcome.loaded)
        self.assertIsNone(outcome.verified)
        self.assertIn("could not confirm", outcome.reason)

    def test_an_unknown_model_name_is_refused_immediately(self) -> None:
        outcome = _manager().load("   ")
        self.assertTrue(outcome.refused)
        self.assertEqual(outcome.reason, "no model was named")

    def test_an_estimate_is_used_when_nothing_measured_the_model(self) -> None:
        monitor = FakeMonitor(available=16 * GIB)
        needed = monitor.estimate_bytes(parameters_b=8.0)
        self.assertEqual(needed, int(8.0 * BYTES_PER_PARAMETER))
        self.assertIsNone(monitor.estimate_bytes())


# ── acquiring a model for a request ──────────────────────────────────────────


class ModelAcquisitionTests(unittest.TestCase):
    """``acquire`` and ``release_after_use``: the pair a request path uses.

    The API endpoint was the only caller of the six-step load, so an AUTOMATIC
    request reached the runtime with the wrong model resident — the case the
    specification's own switching example describes. These tests pin the pairing
    that fixes it, including what happens when there is no room.
    """

    def test_acquiring_makes_room_for_the_model_that_is_needed(self) -> None:
        """Qwen3 resident, a vision request, room made, VLM verified.

        Two gigabytes free and a three-gigabyte VLM: alongside the resident
        chat model it does not fit, and with that model released it does — the
        exact arithmetic the specification's example describes.
        """
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = _manager(provider, monitor=FakeMonitor(available=2 * GIB))
        outcome = manager.acquire(VISION)
        self.assertTrue(outcome.loaded)
        self.assertEqual(outcome.evicted, (CHAT,))
        self.assertEqual(provider.unloads, [CHAT])
        self.assertEqual(provider.loads, [VISION])
        self.assertTrue(outcome.verified)
        self.assertEqual(manager.active_models(), (VISION,))

    def test_an_acquired_model_is_not_evicted_to_make_room_for_another(self) -> None:
        """Activity taken by ``acquire`` is what protects a model mid-request."""
        provider = _provider(resident={VISION: 3 * GIB})
        manager = _manager(provider, monitor=FakeMonitor(available=4 * GIB))
        manager.acquire(VISION)
        refused = manager.load(CHAT)
        self.assertTrue(refused.refused)
        self.assertIn(VISION, refused.reason)
        self.assertEqual(provider.unloads, [])

    def test_acquiring_a_resident_model_switches_nothing(self) -> None:
        provider = _provider(resident={VISION: 3 * GIB})
        manager = _manager(provider)
        outcome = manager.acquire(VISION)
        self.assertTrue(outcome.loaded)
        self.assertEqual(outcome.reason, "the model was already resident")
        self.assertEqual(provider.loads, [])
        self.assertEqual(manager.telemetry.to_dict()["load_skips"], 1)

    def test_a_warm_model_survives_the_release_and_the_policy_still_applies(self) -> None:
        provider = _provider(resident={VISION: 3 * GIB})
        manager = _manager(
            provider,
            keep_alive=KeepAliveSettings(policy=KeepAlivePolicy.WARM, idle_seconds=60),
        )
        manager.acquire(VISION)
        self.assertFalse(manager.release_after_use(VISION))
        self.assertEqual(provider.unloads, [])
        self.assertEqual(manager.active_models(), ())

    def test_the_idle_window_is_applied_when_a_model_is_acquired(self) -> None:
        """A configured "keep it warm for N minutes" has to actually expire."""
        clock = [0.0]
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = ModelManager(
            provider,
            monitor=FakeMonitor(),
            keep_alive=KeepAliveSettings(policy=KeepAlivePolicy.WARM, idle_seconds=60),
            clock=lambda: clock[0],
        )
        manager.end_activity(CHAT)
        manager.enforce_keep_alive()  # first sighting starts the idle clock
        clock[0] = 120.0
        outcome = manager.acquire(VISION)
        self.assertEqual(provider.unloads, [CHAT])
        self.assertEqual(outcome.evicted, ())  # already gone: nothing left to evict
        self.assertEqual(provider.loads, [VISION])

    def test_acquiring_nothing_is_a_refusal_rather_than_a_load(self) -> None:
        provider = _provider()
        outcome = _manager(provider).acquire("  ")
        self.assertTrue(outcome.refused)
        self.assertEqual(provider.loads, [])


# ── keep-alive ───────────────────────────────────────────────────────────────


class KeepAliveTests(unittest.TestCase):
    def _manager(self, policy: KeepAlivePolicy, **kwargs: Any) -> tuple[ModelManager, FakeProvider]:
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = ModelManager(
            provider,
            monitor=FakeMonitor(),
            keep_alive=KeepAliveSettings(policy=policy, **kwargs),
        )
        return manager, provider

    def test_the_default_is_the_one_that_does_not_invent_a_duration(self) -> None:
        settings = KeepAliveSettings.from_mapping(None)
        self.assertIs(settings.policy, KeepAlivePolicy.WARM)
        self.assertEqual(settings.idle_seconds, 0)

    def test_immediate_releases_as_soon_as_the_operation_returns(self) -> None:
        manager, _provider_ = self._manager(KeepAlivePolicy.IMMEDIATE)
        self.assertTrue(manager.release_after_use(CHAT))
        self.assertEqual(manager.unload(CHAT), ())

    def test_warm_holds_until_the_idle_window_has_passed(self) -> None:
        clock = [0.0]
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = ModelManager(
            provider,
            monitor=FakeMonitor(),
            keep_alive=KeepAliveSettings(policy=KeepAlivePolicy.WARM, idle_seconds=60),
            clock=lambda: clock[0],
        )
        manager.end_activity(CHAT)
        self.assertEqual(manager.enforce_keep_alive(), ())  # first sighting: starts the clock
        clock[0] = 30.0
        self.assertEqual(manager.enforce_keep_alive(), ())
        clock[0] = 90.0
        self.assertEqual(manager.enforce_keep_alive(), (CHAT,))
        self.assertEqual(provider.unloads, [CHAT])

    def test_while_active_releases_once_the_last_task_has_finished(self) -> None:
        """The policy means what its name says: warm WHILE a task is running.

        Held for the rest of the process's life it would be indistinguishable from
        ``never`` — a configured policy that behaves exactly like a different one
        is a policy that does nothing, and nothing on the request path could tell
        the difference. Both directions are asserted here: a second task still
        using the model keeps it, and the last one finishing releases it.
        """
        manager, provider = self._manager(KeepAlivePolicy.WHILE_ACTIVE, active_seconds=1)
        manager.begin_activity(CHAT)
        manager.begin_activity(CHAT)  # two tasks share the model
        self.assertFalse(manager.release_after_use(CHAT))  # one is still running
        self.assertEqual(manager.enforce_keep_alive(), ())
        self.assertEqual(provider.unloads, [])
        self.assertTrue(manager.release_after_use(CHAT))  # the last one finished
        self.assertEqual(provider.unloads, [CHAT])

    def test_never_means_never(self) -> None:
        manager, provider = self._manager(KeepAlivePolicy.NEVER, idle_seconds=1)
        manager.end_activity(CHAT)
        self.assertEqual(manager.enforce_keep_alive(), ())
        self.assertFalse(manager.release_after_use(CHAT))
        self.assertEqual(provider.unloads, [])

    def test_a_policy_typo_is_reported_rather_than_silently_ignored(self) -> None:
        settings = KeepAliveSettings.from_mapping({"policy": "immedate", "idle_seconds": 5})
        self.assertIs(settings.policy, KeepAlivePolicy.WARM)
        self.assertFalse(settings.honoured)
        self.assertEqual(settings.requested, "immedate")
        self.assertTrue(KeepAliveSettings.from_mapping({"policy": "immediate"}).honoured)


# ── hardware ─────────────────────────────────────────────────────────────────


class HardwareHonestyTests(unittest.TestCase):
    def test_no_accelerator_is_claimed_that_was_not_detected(self) -> None:
        monitor = HardwareMonitor(resident_models=lambda: ())
        npu = monitor.npu()
        self.assertFalse(npu["available"])
        self.assertTrue(npu["reason"])
        self.assertFalse(monitor.snapshot().npu_available)

    def test_an_unmeasurable_fit_is_unknown_not_a_yes(self) -> None:
        monitor = FakeMonitor(available=None)
        self.assertIsNone(monitor.available_ram_bytes())
        self.assertIsNone(monitor.fits(1 * GIB))
        self.assertIsNone(monitor.fits(None))
        self.assertIsNone(monitor.headroom_report()["available_ram_bytes"])

    def test_the_snapshot_names_the_source_of_every_figure(self) -> None:
        snapshot = FakeMonitor().snapshot()
        self.assertIn("ram", snapshot.sources)
        self.assertIn("npu", snapshot.sources)
        self.assertFalse(snapshot.gpu_available)

    def test_headroom_is_reported_alongside_the_measurement(self) -> None:
        report = FakeMonitor(available=4 * GIB).headroom_report()
        self.assertEqual(report["available_ram_bytes"], 4 * GIB)
        self.assertGreater(report["headroom_bytes"], 0)
        self.assertIsNotNone(report["total_ram_bytes"])


# ── routing, switching and cloud fallback ────────────────────────────────────


class RoutingTests(unittest.TestCase):
    """The specification's example flows, as capability requirements."""

    def setUp(self) -> None:
        self.provider = _provider()
        self.manager = _manager(self.provider)
        self.manager.refresh_capabilities()

    def test_a_simple_request_prefers_a_model_without_extra_abilities(self) -> None:
        selection = self.manager.select_for_request()
        self.assertEqual(selection.model, TINY)
        self.assertEqual(selection.route, "local")

    def test_plain_reasoning_goes_to_the_reasoning_model(self) -> None:
        self.assertEqual(self.manager.select_for_request(needs_reasoning=True).model, CHAT)

    def test_coding_goes_to_the_coding_model(self) -> None:
        self.assertEqual(self.manager.select_for_request(needs_coding=True).model, CHAT)

    def test_a_vision_request_goes_to_the_model_that_can_see(self) -> None:
        selection = self.manager.select_for_request(requires_vision=True)
        self.assertEqual(selection.model, VISION)
        self.assertIn("vision", selection.required)

    def test_vision_with_tool_calls_is_a_different_requirement(self) -> None:
        self._install(
            "vision-with-tools:7b",
            capabilities=("text", "vision", "tool_calling"),
            parameters_b=7.0,
        )
        selection = self.manager.select_for_request(
            requires_vision=True, needs_tools=True
        )
        self.assertEqual(selection.model, "vision-with-tools:7b")

    def test_the_strongest_model_is_available_when_latency_does_not_matter(self) -> None:
        self._install(
            "big-reasoner:32b",
            capabilities=("text", "reasoning"),
            parameters_b=32.0,
            size=6 * GIB,
        )
        selection = self.manager.select_for_request(
            needs_reasoning=True, latency_preference="quality"
        )
        self.assertEqual(selection.model, "big-reasoner:32b")

    def test_a_declared_model_that_is_not_installed_cannot_be_selected(self) -> None:
        """The pool is what the runtime OFFERS, so a declaration is not enough."""
        self.manager.registry.register(
            ModelProfile(
                name="never-pulled:70b",
                capabilities=frozenset({ModelCapability.TEXT, ModelCapability.REASONING}),
            )
        )
        selection = self.manager.select_for_request(needs_reasoning=True)
        self.assertNotEqual(selection.model, "never-pulled:70b")

    def test_a_capability_may_be_named_as_a_string(self) -> None:
        """The spelling a caller actually writes must not crash the refusal path."""
        selection = self.manager.select({"vision"})
        self.assertEqual(selection.model, VISION)
        self.assertIn("vision", selection.required)

    def test_an_unrecognised_capability_is_an_error_not_an_empty_requirement(self) -> None:
        """A typo must not be answered by a model that cannot do it."""
        with self.assertRaises(ValueError):
            self.manager.select({"visoin"})

    def _install(
        self,
        name: str,
        *,
        capabilities: tuple[str, ...],
        parameters_b: float | None = None,
        size: int = 2 * GIB,
    ) -> None:
        """Declare AND install a model, the way a pulled build appears."""
        self.provider.installed[name] = size
        self.provider.reported[name] = tuple(capabilities)
        self.manager.refresh_capabilities()
        profile = self.manager.registry.get(name)
        self.assertIsNotNone(profile)
        if parameters_b is not None:
            self.manager.registry.register(replace(profile, parameters_b=parameters_b))

    def test_a_requirement_nothing_local_meets_falls_back_to_the_cloud(self) -> None:
        selection = self.manager.select_for_request(
            needs_coding=True,
            requires_vision=True,
            cloud_available=True,
            cloud_model="gpt-5",
        )
        self.assertEqual(selection.route, "cloud")
        self.assertEqual(selection.model, "gpt-5")
        self.assertIn("cloud provider", selection.reason)

    def test_with_no_cloud_the_answer_is_none_and_says_why(self) -> None:
        selection = self.manager.select_for_request(
            needs_coding=True, requires_vision=True
        )
        self.assertEqual(selection.route, "none")
        self.assertFalse(selection.found)
        self.assertTrue(selection.required)
        self.assertTrue(selection.match.rejected)

    def test_the_resident_model_wins_when_it_already_satisfies_the_requirement(self) -> None:
        """The 'avoid unnecessary switching' rule, enforced by selection."""
        self.provider.resident = {TINY: 2 * GIB}
        selection = self.manager.select_for_request()
        self.assertEqual(selection.model, TINY)
        self.assertIn("already resident", selection.reason)

    def test_a_configured_choice_wins_over_the_size_heuristic(self) -> None:
        selection = self.manager.select_for_request(preferred=CHAT)
        self.assertEqual(selection.model, CHAT)
        self.assertIn("configuration names", selection.reason)

    def test_the_cost_of_a_switch_is_measured_not_estimated(self) -> None:
        """Batching can only be judged with the numbers a switch actually costs."""
        provider = _provider(resident={CHAT: 6 * GIB})
        manager = _manager(provider, monitor=FakeMonitor(available=2 * GIB))
        outcome = manager.load(VISION)
        self.assertTrue(outcome.loaded)
        telemetry = manager.telemetry.to_dict()
        self.assertEqual(telemetry["loads"], 1)
        self.assertEqual(telemetry["unloads"], 1)
        self.assertEqual(telemetry["evictions"], 1)
        self.assertEqual(telemetry["load_seconds"]["count"], 1)
        self.assertEqual(telemetry["unload_seconds"]["count"], 1)
        # And a model that is already there costs nothing at all, which is why
        # the selector prefers one that satisfies the requirement.
        again = manager.load(VISION)
        self.assertEqual(again.reason, "the model was already resident")
        self.assertEqual(manager.telemetry.to_dict()["load_skips"], 1)
        self.assertEqual(provider.loads, [VISION])


# ── telemetry ────────────────────────────────────────────────────────────────


class RequestTelemetryTests(unittest.TestCase):
    def test_a_request_row_carries_durations_ram_and_provenance(self) -> None:
        telemetry = InterpretationTelemetry()
        trace = telemetry.begin_request(ram_before=8 * GIB)
        with telemetry.stage("nlu"):
            pass
        with telemetry.stage("planning"):
            pass
        row = telemetry.end_request(
            trace,
            ram_after=7 * GIB,
            model=CHAT,
            provider="ollama",
            fast_path=False,
            success=True,
        )
        self.assertGreaterEqual(row["total_ms"], 0.0)
        self.assertEqual(sorted(row["stages_ms"]), ["nlu", "planning"])
        self.assertEqual(row["ram_delta_bytes"], -(1 * GIB))
        self.assertEqual(row["model"], CHAT)
        self.assertEqual(row["provider"], "ollama")
        self.assertTrue(row["success"])

    def test_the_summary_reports_every_stage_and_an_honest_empty_block(self) -> None:
        telemetry = InterpretationTelemetry()
        telemetry.record_stage("nlu", 0.01)
        summary = telemetry.to_dict()
        self.assertEqual(sorted(summary["stages_ms"]), sorted(REQUEST_STAGES))
        self.assertEqual(summary["stages_ms"]["nlu"]["count"], 1)
        self.assertIsNone(summary["stages_ms"]["vision"]["avg"])
        self.assertIsNone(summary["requests"]["total_ms"]["avg"])

    def test_fast_path_usage_and_escalation_rate_are_separate_numbers(self) -> None:
        telemetry = InterpretationTelemetry()
        for fast in (True, True, False):
            trace = telemetry.begin_request()
            telemetry.end_request(trace, fast_path=fast)
        summary = telemetry.to_dict()
        self.assertEqual(summary["requests"]["count"], 3)
        self.assertEqual(summary["requests"]["fast_paths"], 2)
        self.assertAlmostEqual(summary["requests"]["fast_path_rate"], 0.667, places=3)

    def test_a_stage_can_be_timed_with_no_request_open(self) -> None:
        telemetry = InterpretationTelemetry()
        with telemetry.stage("tool_execution"):
            pass
        self.assertEqual(telemetry.to_dict()["stages_ms"]["tool_execution"]["count"], 1)

    def test_a_model_call_reports_its_timings_without_claiming_an_escalation(self) -> None:
        """First-token latency and generation rate, for the call that produced them.

        These two figures used to be recorded only when the NLU gave up and
        escalated, so they described one path out of several and the model that
        answered most requests reported nothing at all.
        """
        telemetry = InterpretationTelemetry()
        measured = telemetry.record_model_timings(
            reason="chat",
            timings={
                "model_load_ms": 900.0,
                "first_token_ms": 1200.0,
                "tokens_per_second": 18.4,
            },
        )
        self.assertEqual(measured["first_token_ms"], 1200.0)
        summary = telemetry.to_dict()
        self.assertEqual(summary["model_timings"]["tokens_per_second"], 18.4)
        self.assertEqual(summary["model_timing_calls"], {"chat": 1})
        # A chat answer is NOT the understanding layer falling back to a model.
        self.assertEqual(summary["escalations"], {})
        self.assertEqual(summary["escalation_rate"], 0.0)

    def test_a_backend_that_reports_no_timing_records_nothing(self) -> None:
        telemetry = InterpretationTelemetry()
        self.assertEqual(telemetry.record_model_timings(reason="chat", timings={}), {})
        self.assertEqual(telemetry.record_model_timings(reason="chat"), {})
        summary = telemetry.to_dict()
        self.assertEqual(summary["model_timings"], {})
        self.assertEqual(summary["model_timing_calls"], {})

    def test_no_chain_of_thought_is_exposed(self) -> None:
        telemetry = InterpretationTelemetry()
        trace = telemetry.begin_request()
        telemetry.end_request(trace, model=CHAT, provider="ollama", success=True)
        text = repr(telemetry.to_dict()).lower()
        for forbidden in ("prompt", "reasoning_text", "thinking", "chain_of_thought", "answer"):
            with self.subTest(key=forbidden):
                self.assertNotIn(forbidden, text)


class TelemetryStageContractTests(unittest.TestCase):
    def test_every_stage_name_the_application_records_is_declared(self) -> None:
        """A stage that no reader knows about is a number nobody will find.

        Checked across the modules that time a request, not just the application:
        the context layer's cost is measured inside the understanding engine, and
        a stage named there but not in ``REQUEST_STAGES`` would land in a bucket
        no reader lists.
        """
        import re
        from pathlib import Path

        used: set[str] = set()
        for module in ("application.py", "intelligence/engine.py"):
            source = Path(f"src/novacontrol/{module}").read_text(encoding="utf-8")
            used |= set(re.findall(r'telemetry\.stage\("([a-z_]+)"\)', source))
        self.assertTrue(used)
        self.assertLessEqual(used, set(REQUEST_STAGES))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
