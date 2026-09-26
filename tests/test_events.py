from __future__ import annotations

import unittest

from novacontrol.core.errors import EventDeliveryError
from novacontrol.core.events import (
    EVENT_PAYLOAD_FIELDS,
    Event,
    EventBus,
    EventType,
    InvalidEventPayloadError,
    task_event_name,
)


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


class EventVocabularyTests(unittest.TestCase):
    """Phase 9.1: the typed vocabulary this system publishes by name."""

    #: The specification's own list. Every one of them must exist, because a
    #: vocabulary nobody can name is a vocabulary nobody can subscribe to.
    _SPECIFICATION_EVENTS = (
        "intent.detected",
        "context.resolved",
        "decision.created",
        "plan.created",
        "task.started",
        "task.paused",
        "task.resumed",
        "task.cancelled",
        "tool.selected",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "verification.started",
        "verification.completed",
        "recovery.started",
        "recovery.completed",
        "task.completed",
        "task.failed",
        "model.loaded",
        "model.unloaded",
        "vision.started",
        "vision.completed",
    )

    def test_every_specification_event_is_part_of_the_vocabulary(self) -> None:
        declared = {member.value for member in EventType}

        for name in self._SPECIFICATION_EVENTS:
            self.assertIn(name, declared)

    def test_every_event_carries_the_fields_it_promises(self) -> None:
        self.assertEqual(set(EVENT_PAYLOAD_FIELDS), set(EventType))
        for fields in EVENT_PAYLOAD_FIELDS.values():
            self.assertTrue(fields)

    def test_publishing_without_a_required_field_is_an_error_at_the_publisher(self) -> None:
        with self.assertRaises(InvalidEventPayloadError) as caught:
            Event.of(EventType.DECISION_CREATED.value, route="system_tools")

        self.assertEqual(caught.exception.type, "decision.created")
        self.assertEqual(caught.exception.missing, ("decision_type",))

    def test_an_event_with_all_of_its_fields_is_accepted(self) -> None:
        event = Event.of(
            EventType.DECISION_CREATED.value, route="system_tools", decision_type="deterministic"
        )

        self.assertEqual(event.type, "decision.created")

    def test_extra_fields_are_allowed(self) -> None:
        event = Event.of(EventType.TOOL_STARTED.value, tool="machine_facts", attempt=2)

        self.assertEqual(event.payload["attempt"], 2)

    def test_a_custom_event_type_is_left_alone(self) -> None:
        """A module is free to publish its own events without registering here."""
        event = Event.of("command.progress", detail="working")

        self.assertEqual(event.type, "command.progress")

    def test_a_child_event_keeps_the_correlation(self) -> None:
        parent = Event.of(EventType.INTENT_DETECTED.value, correlation_id="run-1", intent="demo")

        child = parent.child(EventType.DECISION_CREATED.value, route="chat", decision_type="chat")

        self.assertEqual(child.correlation_id, "run-1")
        self.assertEqual(child.causation_id, "run-1")

    def test_a_task_state_maps_to_its_lifecycle_event(self) -> None:
        self.assertEqual(task_event_name("running"), EventType.TASK_STARTED)
        self.assertEqual(task_event_name("paused"), EventType.TASK_PAUSED)
        self.assertEqual(task_event_name("completed"), EventType.TASK_COMPLETED)
        # A state with no lifecycle meaning (the middle of a run) publishes
        # nothing rather than a misleading event.
        self.assertIsNone(task_event_name("verifying"))


class EventDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Phase 9.1: delivery, isolation, unsubscription and history."""

    async def test_a_handler_that_fails_does_not_stop_the_others(self) -> None:
        bus = EventBus(continue_on_error=True)
        seen: list[str] = []

        async def broken(_event: Event) -> None:
            raise RuntimeError("no")

        async def after(event: Event) -> None:
            seen.append(event.type)

        await bus.subscribe("tool.started", broken)
        await bus.subscribe("tool.started", after)

        result = await bus.emit(EventType.TOOL_STARTED.value, tool="machine_facts")

        self.assertEqual(seen, ["tool.started"])
        self.assertEqual(result.delivered, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("no", str(result.failures[0].error))

    async def test_the_error_handler_hears_about_the_failure(self) -> None:
        heard: list[str] = []

        async def on_error(failure: object) -> None:
            heard.append(str(getattr(failure, "handler", "")))

        bus = EventBus(continue_on_error=True, error_handler=on_error)

        async def broken(_event: Event) -> None:
            raise RuntimeError("no")

        await bus.subscribe(EventType.TOOL_STARTED.value, broken)
        await bus.emit(EventType.TOOL_STARTED.value, tool="machine_facts")

        self.assertTrue(heard)

    async def test_strict_mode_reports_the_failure_to_the_publisher(self) -> None:
        bus = EventBus()  # continue_on_error defaults to False

        async def broken(_event: Event) -> None:
            raise RuntimeError("no")

        await bus.subscribe(EventType.TOOL_STARTED.value, broken)

        with self.assertRaises(EventDeliveryError):
            await bus.publish(Event.of(EventType.TOOL_STARTED.value, tool="machine_facts"))

    async def test_emit_never_raises_because_a_subscriber_failed(self) -> None:
        bus = EventBus()

        async def broken(_event: Event) -> None:
            raise RuntimeError("no")

        await bus.subscribe(EventType.TOOL_STARTED.value, broken)

        result = await bus.emit(EventType.TOOL_STARTED.value, tool="machine_facts")

        self.assertFalse(result.ok)
        self.assertEqual(result.delivered, 0)

    async def test_a_synchronous_handler_is_supported(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        def plain(event: Event) -> None:
            seen.append(event.type)

        await bus.subscribe("*", plain)
        await bus.emit(EventType.TASK_STARTED.value, task_id="t-1")

        self.assertEqual(seen, ["task.started"])

    async def test_unsubscribing_stops_delivery(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def handler(event: Event) -> None:
            seen.append(event.type)

        await bus.subscribe(EventType.TASK_STARTED.value, handler)
        await bus.emit(EventType.TASK_STARTED.value, task_id="t-1")
        await bus.unsubscribe(EventType.TASK_STARTED.value, handler)
        await bus.emit(EventType.TASK_STARTED.value, task_id="t-2")

        self.assertEqual(seen, ["task.started"])
        self.assertEqual(bus.subscriber_count("task.started"), 0)

    async def test_unsubscribing_a_wildcard_stops_delivery_too(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def handler(event: Event) -> None:
            seen.append(event.type)

        await bus.subscribe("*", handler)
        await bus.unsubscribe("*", handler)
        await bus.emit(EventType.TASK_STARTED.value, task_id="t-1")

        self.assertEqual(seen, [])

    async def test_unsubscribing_something_never_subscribed_is_a_no_op(self) -> None:
        bus = EventBus()

        async def handler(_event: Event) -> None:
            return None

        await bus.unsubscribe("task.started", handler)

        self.assertEqual(bus.subscriber_count("task.started"), 0)

    async def test_a_blank_event_type_cannot_be_subscribed_to(self) -> None:
        bus = EventBus()

        async def handler(_event: Event) -> None:
            return None

        with self.assertRaises(ValueError):
            await bus.subscribe("  ", handler)

    async def test_the_subscriber_list_is_readable(self) -> None:
        bus = EventBus()

        async def handler(_event: Event) -> None:
            return None

        await bus.subscribe("task.started", handler)
        await bus.subscribe("tool.started", handler)
        await bus.subscribe("*", handler)

        self.assertEqual(bus.subscribers(), ("task.started", "tool.started"))
        self.assertEqual(bus.subscriber_count(), 3)

    async def test_history_is_bounded_and_filterable(self) -> None:
        bus = EventBus(history=3)

        for index in range(5):
            await bus.emit(EventType.TASK_STARTED.value, task_id=f"t-{index}")

        recent = bus.recent()
        self.assertEqual(len(recent), 3)
        self.assertEqual(
            [dict(event.payload)["task_id"] for event in recent],
            ["t-2", "t-3", "t-4"],
        )
        self.assertEqual(len(bus.recent(type_="task.started")), 3)
        self.assertEqual(bus.recent(type_="task.completed"), ())

    async def test_history_can_be_turned_off(self) -> None:
        bus = EventBus()

        await bus.emit(EventType.TASK_STARTED.value, task_id="t-1")

        self.assertEqual(bus.recent(), ())

    async def test_the_journal_still_receives_everything(self) -> None:
        class _Journal:
            def __init__(self) -> None:
                self.events: list[Event] = []

            async def append(self, event: Event) -> None:
                self.events.append(event)

        journal = _Journal()
        bus = EventBus(journal=journal, history=1)

        for index in range(3):
            await bus.emit(EventType.TASK_STARTED.value, task_id=f"t-{index}")

        self.assertEqual(len(journal.events), 3)
        self.assertEqual(len(bus.recent()), 1)


if __name__ == "__main__":
    unittest.main()
