from __future__ import annotations

from collections.abc import Sequence
import unittest

from novacontrol.core.events import EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.core.runtime import EventDrivenRuntime


class DemoModule:
    def __init__(self, name: str) -> None:
        self._name = name
        self.started = False
        self.stopped = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> Sequence[Capability]:
        return (Capability(name="demo", description="Demo capability"),)

    async def start(self, event_bus: EventBus) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_starts_and_stops_modules(self) -> None:
        runtime = EventDrivenRuntime()
        module = DemoModule("demo")
        runtime.register_module(module)

        await runtime.start()
        await runtime.stop()

        self.assertTrue(module.started)
        self.assertTrue(module.stopped)
        self.assertFalse(runtime.state.started)

    async def test_duplicate_module_names_are_rejected(self) -> None:
        runtime = EventDrivenRuntime()
        runtime.register_module(DemoModule("demo"))

        with self.assertRaises(ValueError):
            runtime.register_module(DemoModule("demo"))


if __name__ == "__main__":
    unittest.main()
