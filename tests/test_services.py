from __future__ import annotations

import unittest

from novacontrol.core.services import ServiceContainer


class ServiceContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_and_resolve(self) -> None:
        services = ServiceContainer()
        services.register("answer", 42)

        self.assertEqual(services.resolve("answer", int), 42)

    async def test_dispose_calls_close(self) -> None:
        class Closable:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        service = Closable()
        services = ServiceContainer()
        services.register("closable", service)

        await services.dispose()

        self.assertTrue(service.closed)


if __name__ == "__main__":
    unittest.main()
