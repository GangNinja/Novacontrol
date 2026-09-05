from __future__ import annotations

import unittest

from novacontrol.core.events import Event, EventBus


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_notifies_exact_and_wildcard_handlers(self) -> None:
        bus = EventBus()
        seen: list[tuple[str, str]] = []

        async def exact(event: Event) -> None:
            seen.append(("exact", event.type))

        async def wildcard(event: Event) -> None:
            seen.append(("wildcard", event.type))

        await bus.subscribe("task.created", exact)
        await bus.subscribe("*", wildcard)

        await bus.publish(Event(type="task.created", payload={"title": "demo"}))

        self.assertEqual(seen, [("exact", "task.created"), ("wildcard", "task.created")])

    def test_event_requires_type(self) -> None:
        with self.assertRaises(ValueError):
            Event(type="")

    def test_event_payload_is_immutable(self) -> None:
        event = Event(type="demo", payload={"value": 1})

        with self.assertRaises(TypeError):
            event.payload["value"] = 2  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
