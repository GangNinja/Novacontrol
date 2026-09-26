"""Phase 7: the model + hardware manager, and what it answers.

NovaControl runs on a 16 GB machine where a chat model and a vision model each
want a large slice of RAM. The rules this suite pins down are the ones that keep
that workable:

  * one model resident at a time is the default policy, whatever the runtime
    would otherwise allow;
  * measured memory overrides policy — a model that cannot fit alongside what is
    resident is unloaded first even when exclusivity is switched off;
  * "could not measure" (``None``) is reported as unknown rather than as zero,
    because a confident zero is how a load ends up paging;
  * the same questions are answered through a replaceable backend, so the
    application never reaches into a specific runtime itself;
  * the application surface loads and unloads through the manager, so nobody has
    to remember the memory rule at a call site.
"""

from __future__ import annotations

import unittest
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.intelligence.model_manager import ModelManager
from novacontrol.models import HardwareMonitor
from novacontrol.models import ModelManager as Phase7ModelManager

CHAT = "qwen3:8b"
VISION = "qwen3-vl:4b"
GIB = 1024**3


class StatedMonitor(HardwareMonitor):
    """A monitor whose memory figures are stated rather than measured."""

    def __init__(self, available: int | None = 8 * GIB, total: int | None = 16 * GIB) -> None:
        super().__init__(resident_models=lambda: ())
        self._available = available
        self._total = total

    def available_ram_bytes(self) -> int | None:
        return self._available

    def total_ram_bytes(self) -> int | None:
        return self._total


class FakeBackend:
    """A runtime held in memory, so lifecycle rules can be observed directly.

    Two models loaded at once is exactly the failure this layer exists to
    prevent, so the fake records what is resident after every call instead of
    assuming the rule held.
    """

    name = "fake"

    def __init__(
        self,
        *,
        available_bytes: int | None = 12 * GIB,
        reachable: bool = True,
        sizes: dict[str, int] | None = None,
    ) -> None:
        self.available_bytes = available_bytes
        self.reachable = reachable
        self.sizes = sizes or {CHAT: 5 * GIB, VISION: 3 * GIB}
        self.resident: dict[str, int] = {}
        self.calls: list[str] = []

    # -- the protocol ---------------------------------------------------------

    def list_models(self) -> tuple[str, ...]:
        return tuple(self.sizes)

    def resident_models(self) -> tuple[tuple[str, int], ...] | None:
        if not self.reachable:
            return None
        return tuple(self.resident.items())

    def load(self, model: str) -> bool:
        self.calls.append(f"load:{model}")
        if not self.reachable:
            return False
        self.resident[model] = self.sizes.get(model, GIB)
        return True

    def unload(self, model: str) -> bool:
        self.calls.append(f"unload:{model}")
        if not self.reachable:
            return False
        return self.resident.pop(model, None) is not None

    def available_memory_bytes(self) -> int | None:
        return self.available_bytes

    def model_size_bytes(self, model: str) -> int | None:
        return self.sizes.get(model)


class LifecycleQuestionTests(unittest.TestCase):
    def test_is_loaded_answers_from_the_runtime(self) -> None:
        backend = FakeBackend()
        manager = ModelManager(backend)
        self.assertFalse(manager.is_loaded(CHAT))
        backend.resident[CHAT] = 5 * GIB
        self.assertTrue(manager.is_loaded(CHAT))
        self.assertFalse(manager.is_loaded(VISION))

    def test_active_model_names_what_is_resident(self) -> None:
        backend = FakeBackend()
        manager = ModelManager(backend)
        self.assertEqual(manager.get_active_model(), "")
        backend.resident[CHAT] = 5 * GIB
        self.assertEqual(manager.get_active_model(), CHAT)

    def test_available_memory_is_a_number_or_none(self) -> None:
        manager = ModelManager(FakeBackend())
        self.assertEqual(manager.get_available_memory(), 12 * GIB)
        unmeasurable = ModelManager(FakeBackend(available_bytes=None))
        self.assertIsNone(unmeasurable.get_available_memory())

    def test_an_unreachable_runtime_reports_unknown_not_zero(self) -> None:
        manager = ModelManager(FakeBackend(reachable=False))
        status = manager.get_model_status()
        self.assertFalse(status.reachable)
        self.assertEqual(status.active_model, "")
        self.assertEqual(status.loaded_models, ())
        self.assertFalse(manager.is_loaded(CHAT))

    def test_the_status_snapshot_names_the_backend_and_memory(self) -> None:
        backend = FakeBackend()
        backend.resident[CHAT] = 5 * GIB
        status = ModelManager(backend).get_model_status()
        self.assertEqual(status.backend, "fake")
        self.assertEqual(status.active_model, CHAT)
        self.assertEqual(status.loaded_models, (CHAT,))
        self.assertEqual(status.available_memory_bytes, 12 * GIB)
        self.assertEqual(status.to_dict()["available_memory_bytes"], 12 * GIB)


class ExclusivityTests(unittest.TestCase):
    """One model resident at a time, unless the measurement says otherwise."""

    def test_a_second_model_is_not_kept_alongside_the_first(self) -> None:
        backend = FakeBackend()
        backend.resident[CHAT] = 5 * GIB
        manager = ModelManager(backend)
        result = manager.load_model(VISION)
        self.assertTrue(result.loaded)
        self.assertEqual(result.evicted, (CHAT,))
        self.assertEqual(tuple(backend.resident), (VISION,))

    def test_the_plan_reports_the_measured_numbers(self) -> None:
        backend = FakeBackend()
        backend.resident[CHAT] = 5 * GIB
        plan = ModelManager(backend).plan_for(VISION)
        self.assertEqual(plan.evict, (CHAT,))
        self.assertEqual(plan.available_bytes, 12 * GIB)
        self.assertEqual(plan.incoming_bytes, 3 * GIB)
        self.assertTrue(plan.fits_after_evict)
        self.assertIn(CHAT, plan.describe())

    def test_measured_memory_overrides_the_policy(self) -> None:
        """Even with exclusivity switched off, a model that cannot fit must wait."""
        backend = FakeBackend(available_bytes=1 * GIB)
        backend.resident[CHAT] = 5 * GIB
        manager = ModelManager(backend, exclusive=False)
        plan = manager.plan_for(VISION)
        self.assertEqual(plan.evict, (CHAT,))
        # Evicting the chat model is enough here: 5 GB freed plus 1 GB free
        # covers a 3 GB model and its headroom.
        self.assertTrue(plan.fits_after_evict)

    def test_a_model_that_cannot_fit_even_after_eviction_says_so(self) -> None:
        backend = FakeBackend(
            available_bytes=1 * GIB, sizes={CHAT: 1 * GIB, VISION: 20 * GIB}
        )
        backend.resident[CHAT] = 1 * GIB
        plan = ModelManager(backend, exclusive=False).plan_for(VISION)
        self.assertEqual(plan.evict, (CHAT,))
        self.assertFalse(plan.fits_after_evict)
        self.assertIn("NOT enough room", plan.describe())

    def test_exclusivity_off_keeps_models_that_do_fit(self) -> None:
        backend = FakeBackend(available_bytes=64 * GIB)
        backend.resident[CHAT] = 5 * GIB
        plan = ModelManager(backend, exclusive=False).plan_for(VISION)
        self.assertEqual(plan.evict, ())

    def test_unload_all_releases_everything(self) -> None:
        backend = FakeBackend()
        backend.resident = {CHAT: 5 * GIB, VISION: 3 * GIB}
        freed = ModelManager(backend).unload_all()
        self.assertEqual(sorted(freed), sorted([CHAT, VISION]))
        self.assertEqual(backend.resident, {})

    def test_ensure_exclusive_keeps_only_the_named_model(self) -> None:
        backend = FakeBackend()
        backend.resident = {CHAT: 5 * GIB, VISION: 3 * GIB}
        freed = ModelManager(backend).ensure_exclusive(VISION)
        self.assertEqual(freed, (CHAT,))
        self.assertEqual(tuple(backend.resident), (VISION,))

    def test_unloading_a_model_that_is_not_resident_is_not_a_failure(self) -> None:
        self.assertFalse(ModelManager(FakeBackend()).unload_model(CHAT))

    def test_an_unreachable_runtime_never_raises_from_a_lifecycle_call(self) -> None:
        manager = ModelManager(FakeBackend(reachable=False))
        self.assertFalse(manager.load_model(CHAT).loaded)
        self.assertFalse(manager.unload_model(CHAT))
        self.assertEqual(manager.unload_all(), ())

    def test_loading_the_resident_model_evicts_nothing(self) -> None:
        backend = FakeBackend()
        backend.resident[CHAT] = 5 * GIB
        result = ModelManager(backend).load_model(CHAT)
        self.assertTrue(result.loaded)
        self.assertEqual(result.evicted, ())
        self.assertEqual(tuple(backend.resident), (CHAT,))


class ApplicationModelSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """The application drives the manager, so the rule is not a call-site habit."""

    async def asyncSetUp(self) -> None:
        self.app = NovaControlApplication()
        self.backend = FakeBackend()
        # Both layers get the fake, because both are asked something: Phase 7's
        # manager is the one the application LOADS and UNLOADS through (it is
        # the layer that knows which models a task is using), and the lifecycle
        # manager is the one the model LIST comes from. Pointing only one of
        # them at the fake would leave the other talking to the real runtime.
        self.app.intelligence.model_manager = ModelManager(self.backend)
        # A stated memory measurement too: the eviction rule is what is under
        # test, and a suite whose answer depends on how much RAM the machine
        # running it happens to have free is a suite that fails for the wrong
        # reason.
        # 2 GB free with a 5 GB model resident: the incoming 3 GB model cannot
        # live beside it, so the application is expected to free room FIRST.
        self.app.model_manager = Phase7ModelManager(
            self.backend,
            monitor=StatedMonitor(available=2 * GIB),
            headroom_bytes=GIB // 2,
        )

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def test_the_status_surface_reports_the_policy_and_providers(self) -> None:
        status = await self.app.model_status()
        for key in (
            "backend",
            "active_model",
            "loaded_models",
            "available_memory_bytes",
            "reachable",
            "exclusive",
            "chat_model",
            "vision_model",
            "local_models",
        ):
            with self.subTest(key=key):
                self.assertIn(key, status)
        self.assertTrue(status["exclusive"])
        self.assertEqual(set(status["local_models"]), set(self.backend.sizes))

    async def test_loading_through_the_application_frees_room_first(self) -> None:
        self.backend.resident[CHAT] = 5 * GIB
        result = await self.app.load_model(VISION)
        self.assertTrue(result["loaded"])
        self.assertEqual(result["evicted"], [CHAT])
        self.assertEqual(tuple(self.backend.resident), (VISION,))

    async def test_loading_nothing_is_a_user_fixable_error(self) -> None:
        with self.assertRaises(ValueError):
            await self.app.load_model("   ")

    async def test_unloading_without_a_name_asks_for_all_of_it(self) -> None:
        self.backend.resident = {CHAT: 5 * GIB, VISION: 3 * GIB}
        result = await self.app.unload_model()
        self.assertTrue(result["unloaded"])
        self.assertEqual(sorted(result["models"]), sorted([CHAT, VISION]))
        self.assertEqual(self.backend.resident, {})

    async def test_unloading_one_model_names_what_it_released(self) -> None:
        self.backend.resident[CHAT] = 5 * GIB
        result = await self.app.unload_model(CHAT)
        self.assertTrue(result["unloaded"])
        self.assertEqual(result["models"], [CHAT])
        missing = await self.app.unload_model(VISION)
        self.assertFalse(missing["unloaded"])


class NPUHonestyTests(unittest.TestCase):
    """No hardware acceleration is claimed that has not been measured."""

    def test_the_manager_reports_the_backend_it_actually_drives(self) -> None:
        manager = ModelManager(FakeBackend())
        self.assertEqual(manager.get_model_status().backend, "fake")

    def test_no_acceleration_field_is_invented(self) -> None:
        status: dict[str, Any] = ModelManager(FakeBackend()).get_model_status().to_dict()
        for invented in ("npu", "gpu", "accelerator"):
            self.assertNotIn(invented, status)


if __name__ == "__main__":
    unittest.main()
