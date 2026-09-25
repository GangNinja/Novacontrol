"""Phase 5 of the intelligence layers: intelligent tool selection.

The question the selector answers is narrow and load-bearing: *which tool
carries this out?* The tests are grouped by the evidence that decides it —
what the catalog declared, what the runtime registry actually holds, whether
the entities the tool needs are present, and how much caution the capability
asks for — plus the two invariances that make it safe to sit next to the
approval layer: it never lowers a confirmation requirement, and it never runs
anything.
"""

from __future__ import annotations

import unittest
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.brain.models import BrainRequest
from novacontrol.decision import DecisionEngine, DecisionEnvironment, DecisionRoute
from novacontrol.intelligence import GlobalInputIntelligence
from novacontrol.intelligence.intent import (
    Capability,
    CapabilityRegistry,
    IntentName,
    RiskLevel,
    StructuredIntent,
)
from novacontrol.intelligence.registry import IntentCatalog, IntentDefinition
from novacontrol.tools.selection import (
    SelectionReason,
    ToolSelector,
    ToolSource,
)


def _intelligence() -> GlobalInputIntelligence:
    """A deterministic-only NLU (no completion provider, so no model call)."""
    return GlobalInputIntelligence()


def _catalog(*definitions: IntentDefinition) -> IntentCatalog:
    catalog = IntentCatalog()
    for definition in definitions:
        catalog = catalog.register(definition)
    return catalog


def _intent(
    name: IntentName,
    *,
    entities: dict[str, Any] | None = None,
    requires_confirmation: bool = False,
    confidence: float = 0.95,
) -> StructuredIntent:
    return StructuredIntent(
        raw_input=name.value.replace("_", " "),
        normalized_input=name.value.replace("_", " "),
        intent=name,
        action=name.value,
        entities=dict(entities or {}),
        confidence=confidence,
        requires_confirmation=requires_confirmation,
    )


class DeclaredToolTests(unittest.TestCase):
    """The intent catalog is the first authority on which tool runs."""

    def setUp(self) -> None:
        self.nlu = _intelligence()
        self.selector = ToolSelector(catalog=self.nlu.catalog, capabilities=self.nlu.capabilities)

    def test_a_plain_launch_names_the_desktop_tool(self) -> None:
        selection = self.selector.select(self.nlu.understand("Open Chrome.").intent)
        self.assertEqual(selection.tool, "desktop_controller")
        self.assertIs(selection.source, ToolSource.DECLARED)
        self.assertIs(selection.reason, SelectionReason.DECLARED_TOOL)
        self.assertGreater(selection.confidence, 0.9)

    def test_a_status_question_names_the_telemetry_tool(self) -> None:
        selection = self.selector.select(self.nlu.understand("What's my RAM usage?").intent)
        self.assertEqual(selection.tool, "system_monitor")

    def test_the_declared_order_is_the_tiebreak(self) -> None:
        """Two declared tools are not a coin flip: the catalog ranks them."""
        catalog = _catalog(
            IntentDefinition(
                intent=IntentName.OPEN_APPLICATION,
                description="open a program",
                tools=("desktop_controller", "fallback_controller"),
            )
        )
        selector = ToolSelector(catalog=catalog)
        selection = selector.select(
            _intent(IntentName.OPEN_APPLICATION, entities={"application": "chrome"})
        )
        self.assertEqual(selection.tool, "desktop_controller")
        self.assertEqual([item.name for item in selection.candidates], [
            "desktop_controller",
            "fallback_controller",
        ])
        self.assertGreater(
            selection.candidates[0].score,
            selection.candidates[1].score,
        )

    def test_every_declared_tool_is_a_known_answer(self) -> None:
        """Parity guard: whatever the catalog declares, the selector can pick."""
        for definition in self.nlu.catalog.all():
            if not definition.tools:
                continue
            with self.subTest(intent=definition.intent.value):
                selection = self.selector.select(_intent(definition.intent))
                self.assertIn(selection.tool, definition.tools)


class AvailabilityTests(unittest.TestCase):
    """Registered, missing and unknown are three different answers."""

    def setUp(self) -> None:
        self.nlu = _intelligence()

    def _selection(self, registered: Any):
        selector = ToolSelector(catalog=self.nlu.catalog, registered=registered)
        return selector.select(self.nlu.understand("Open Chrome.").intent)

    def test_a_registered_tool_is_confirmed_available(self) -> None:
        selection = self._selection(("desktop_controller",))
        self.assertTrue(selection.candidates[0].registered)
        self.assertEqual(selection.tool, "desktop_controller")

    def test_a_declared_tool_that_is_not_registered_is_still_selected(self) -> None:
        """Declared-but-absent is reported, not silently swapped for a stranger."""
        selection = self._selection(("something_else",))
        self.assertFalse(selection.candidates[0].registered)
        self.assertEqual(selection.tool, "desktop_controller")
        reasons = selection.candidates[0].reasons
        self.assertTrue(any("not registered" in reason for reason in reasons))

    def test_with_no_registry_availability_is_unknown_not_missing(self) -> None:
        selection = self._selection(None)
        self.assertIsNone(selection.candidates[0].registered)
        self.assertEqual(selection.tool, "desktop_controller")
        self.assertGreater(selection.confidence, 0.9)

    def test_a_registry_that_raises_costs_availability_not_the_selection(self) -> None:
        def broken() -> tuple[str, ...]:
            raise RuntimeError("registry unavailable")

        selection = self._selection(broken)
        self.assertIsNone(selection.candidates[0].registered)
        self.assertEqual(selection.tool, "desktop_controller")

    def test_a_live_registry_is_read_per_call(self) -> None:
        """A tool registered after boot must be selectable without a restart."""
        names: list[str] = []

        selection = self._selection(lambda: tuple(names))
        self.assertFalse(selection.candidates[0].registered)
        names.append("desktop_controller")
        again = self._selection(lambda: tuple(names))
        self.assertTrue(again.candidates[0].registered)


class RegisteredToolTests(unittest.TestCase):
    """A runtime tool may serve an intent — but only by saying so."""

    def test_a_tool_registered_for_the_intent_can_win(self) -> None:
        catalog = _catalog(
            IntentDefinition(
                intent=IntentName.OPEN_APPLICATION,
                description="open a program",
                tools=("desktop_controller",),
            )
        )
        selector = ToolSelector(
            catalog=catalog,
            registered=lambda: ("desktop_controller", IntentName.OPEN_APPLICATION.value),
        )
        selection = selector.select(
            _intent(IntentName.OPEN_APPLICATION, entities={"application": "chrome"})
        )
        names = {item.name for item in selection.candidates}
        self.assertIn(IntentName.OPEN_APPLICATION.value, names)
        self.assertIn(selection.tool, {IntentName.OPEN_APPLICATION.value, "desktop_controller"})

    def test_an_unrelated_registered_tool_is_never_guessed_into_service(self) -> None:
        """Selection from a name coincidence is how the wrong thing runs."""
        catalog = _catalog(
            IntentDefinition(
                intent=IntentName.OPEN_APPLICATION,
                description="open a program",
                tools=("desktop_controller",),
            )
        )
        selector = ToolSelector(catalog=catalog, registered=lambda: ("rm_rf_everything",))
        selection = selector.select(
            _intent(IntentName.OPEN_APPLICATION, entities={"application": "chrome"})
        )
        self.assertEqual(selection.tool, "desktop_controller")
        self.assertNotIn("rm_rf_everything", [item.name for item in selection.candidates])


class EntityCompletenessTests(unittest.TestCase):
    """A required entity that is missing makes the step incomplete, and says so."""

    def test_a_missing_required_entity_lowers_confidence_and_is_reported(self) -> None:
        catalog = _catalog(
            IntentDefinition(
                intent=IntentName.FIND_FILE,
                description="find a file",
                tools=("file_manager",),
                required_entities=("file",),
            )
        )
        selector = ToolSelector(catalog=catalog)
        selection = selector.select(_intent(IntentName.FIND_FILE))
        self.assertEqual(selection.missing_entities, ("file",))
        self.assertLess(selection.confidence, 0.7)
        self.assertTrue(
            any("missing required entity" in reason for reason in selection.candidates[0].reasons)
        )

    def test_a_blank_entity_counts_as_missing(self) -> None:
        catalog = _catalog(
            IntentDefinition(
                intent=IntentName.FIND_FILE,
                description="find a file",
                tools=("file_manager",),
                required_entities=("file",),
            )
        )
        selector = ToolSelector(catalog=catalog)
        selection = selector.select(_intent(IntentName.FIND_FILE, entities={"file": "   "}))
        self.assertEqual(selection.missing_entities, ("file",))

    def test_a_present_entity_costs_nothing(self) -> None:
        catalog = _catalog(
            IntentDefinition(
                intent=IntentName.FIND_FILE,
                description="find a file",
                tools=("file_manager",),
                required_entities=("file",),
            )
        )
        selector = ToolSelector(catalog=catalog)
        selection = selector.select(_intent(IntentName.FIND_FILE, entities={"file": "report.pdf"}))
        self.assertEqual(selection.missing_entities, ())
        self.assertGreater(selection.confidence, 0.9)


class CautionTests(unittest.TestCase):
    """Confirmation can only ever be RAISED by a selection."""

    def _selector_for(self, risk: RiskLevel) -> ToolSelector:
        catalog = _catalog(
            IntentDefinition(
                intent=IntentName.DELETE_FILE,
                description="delete a file",
                tools=("file_manager",),
            )
        )
        capabilities = CapabilityRegistry()
        capabilities.register(
            Capability(
                capability="delete_file",
                intent=IntentName.DELETE_FILE,
                description="delete a file",
                risk=risk,
                executor="file_manager",
            )
        )
        return ToolSelector(catalog=catalog, capabilities=capabilities)

    def test_a_medium_risk_capability_asks_for_confirmation(self) -> None:
        selection = self._selector_for(RiskLevel.MEDIUM).select(_intent(IntentName.DELETE_FILE))
        self.assertTrue(selection.requires_confirmation)

    def test_a_low_risk_capability_does_not_add_one(self) -> None:
        selection = self._selector_for(RiskLevel.LOW).select(_intent(IntentName.DELETE_FILE))
        self.assertFalse(selection.requires_confirmation)

    def test_the_intents_own_flag_is_kept(self) -> None:
        selection = self._selector_for(RiskLevel.LOW).select(
            _intent(IntentName.DELETE_FILE, requires_confirmation=True)
        )
        self.assertTrue(selection.requires_confirmation)

    def test_the_capability_executor_is_offered_when_nothing_is_declared(self) -> None:
        selection = self._selector_for(RiskLevel.HIGH).select(_intent(IntentName.DELETE_FILE))
        self.assertEqual(selection.tool, "file_manager")
        self.assertIs(selection.source, ToolSource.DECLARED)


class NoToolTests(unittest.TestCase):
    """Nothing declared and nothing registered is an honest empty answer."""

    def test_an_unknown_capability_reports_no_tool(self) -> None:
        selector = ToolSelector(catalog=IntentCatalog(), capabilities=CapabilityRegistry())
        selection = selector.select(_intent(IntentName.OPEN_APPLICATION))
        self.assertEqual(selection.tool, "")
        self.assertIs(selection.reason, SelectionReason.NO_TOOL)
        self.assertFalse(selection.dispatchable)
        self.assertEqual(selection.confidence, 0.0)

    def test_the_payload_carries_evidence_and_no_reasoning(self) -> None:
        nlu = _intelligence()
        selection = ToolSelector(catalog=nlu.catalog).select(
            nlu.understand("Open Chrome.").intent
        )
        payload = selection.to_dict()
        self.assertEqual(payload["tool"], "desktop_controller")
        self.assertEqual(payload["reason"], "declared_tool")
        self.assertIn("evidence", payload)
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("chain_of_thought", payload)


class DecisionEngineSelectionTests(unittest.TestCase):
    """Phase 3 + 5: the decision names the tool, with the evidence beside it."""

    def setUp(self) -> None:
        self.nlu = _intelligence()
        self.selector = ToolSelector(
            catalog=self.nlu.catalog,
            capabilities=self.nlu.capabilities,
            registered=lambda: ("desktop_controller",),
        )

    def _decide(self, text: str, **engine_kwargs: Any):
        engine = DecisionEngine(
            capabilities=self.nlu.capabilities,
            selector=self.selector,
            **engine_kwargs,
        )
        understood = self.nlu.understand(text)
        return engine.decide(
            understood.intent,
            environment=DecisionEnvironment(local_model="qwen3:8b"),
            strategy=understood.strategy,
        )

    def test_a_direct_tool_decision_names_the_tool_and_its_source(self) -> None:
        decision = self._decide("Open Chrome.")
        self.assertIs(decision.route, DecisionRoute.DIRECT_TOOL)
        self.assertEqual(decision.selected_tool, "desktop_controller")
        self.assertEqual(decision.metadata["tool_source"], "declared")
        self.assertEqual(decision.metadata["tool_reason"], "declared_tool")
        self.assertTrue(decision.metadata["tool_dispatchable"])
        self.assertIn("desktop_controller", decision.metadata["tool_candidates"])

    def test_the_selection_can_raise_the_confirmation_requirement(self) -> None:
        decision = self._decide("Delete the file report.pdf")
        self.assertTrue(decision.requires_confirmation)

    def test_the_selection_cannot_clear_an_intent_that_asked_for_one(self) -> None:
        engine = DecisionEngine(capabilities=self.nlu.capabilities, selector=self.selector)
        intent = _intent(
            IntentName.OPEN_APPLICATION,
            entities={"application": "chrome"},
            requires_confirmation=True,
        )
        decision = engine.decide(intent, environment=DecisionEnvironment())
        self.assertTrue(decision.requires_confirmation)

    def test_a_selector_that_raises_costs_the_name_not_the_routing(self) -> None:
        class Broken:
            def select(self, _intent: StructuredIntent) -> Any:
                raise RuntimeError("catalog exploded")

        engine = DecisionEngine(capabilities=self.nlu.capabilities, selector=Broken())  # type: ignore[arg-type]
        understood = self.nlu.understand("Open Chrome.")
        decision = engine.decide(understood.intent, environment=DecisionEnvironment())
        self.assertIs(decision.route, DecisionRoute.DIRECT_TOOL)
        self.assertEqual(decision.selected_tool, "")

    def test_the_telemetry_record_carries_the_tool_facts(self) -> None:
        decision = self._decide("What's my RAM usage?")
        self.assertEqual(decision.selected_tool, "system_monitor")
        self.assertGreater(decision.metadata["tool_confidence"], 0.9)


class PlanPathSelectionTests(unittest.IsolatedAsyncioTestCase):
    """Phase 4: the planning path carries the decision and the selection."""

    async def asyncSetUp(self) -> None:
        self.app = NovaControlApplication()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    def _request(self, nlu: dict[str, Any], decision: dict[str, Any]) -> BrainRequest:
        # A goal the clarification policy refuses to execute, so the test does
        # not run a workflow: the payload is what is under test, not execution.
        return BrainRequest(
            text="do the thing with something",
            context={"nlu": nlu, "decision": decision},
        )

    async def test_the_plan_payload_carries_the_selection_for_a_known_intent(self) -> None:
        request = self._request(
            {
                "intent": "open_application",
                "normalized_input": "open chrome",
                "entities": {"application": "chrome"},
                "confidence": 0.95,
            },
            {"route": "direct_tool", "decision_type": "deterministic"},
        )
        _route, payload = await self.app._handle_plan(request, request.text)
        self.assertEqual(payload["decision"]["route"], "direct_tool")
        self.assertEqual(payload["selection"]["tool"], "desktop_controller")
        self.assertEqual(payload["selection"]["intent"], "open_application")

    async def test_an_unrecognised_intent_yields_no_selection_rather_than_a_guess(self) -> None:
        request = self._request(
            {"intent": "definitely_not_an_intent", "confidence": 0.9},
            {},
        )
        _route, payload = await self.app._handle_plan(request, request.text)
        self.assertEqual(payload["selection"], {})

    async def test_the_plan_payload_survives_a_missing_nlu_block(self) -> None:
        request = BrainRequest(text="do the thing with something", context={})
        _route, payload = await self.app._handle_plan(request, request.text)
        self.assertIn("plan", payload)


class ApplicationToolSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """The running application shares ONE selector with its decision layer."""

    async def asyncSetUp(self) -> None:
        self.app = NovaControlApplication()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def test_a_launch_reports_the_tool_in_the_readout(self) -> None:
        response = await self.app.handle_request("open chrome")
        decision = response.payload["nlu"]["decision"]
        self.assertEqual(decision["selected_tool"], "desktop_controller")
        self.assertIn(decision["route"], {"direct_tool", "local_capability"})

    async def test_the_decision_block_travels_with_the_response(self) -> None:
        response = await self.app.handle_request("what's my RAM usage?")
        decision = response.payload["nlu"]["decision"]
        self.assertEqual(decision["selected_tool"], "system_monitor")
        self.assertFalse(decision["requires_planning"])
        self.assertFalse(decision["requires_confirmation"])

    async def test_a_request_naming_two_metrics_answers_both(self) -> None:
        """Phase 3's spec example, end to end: one probe, two readings."""
        response = await self.app.handle_request("check my RAM and CPU")
        reading = response.payload["nlu"]
        self.assertEqual(reading["decision"]["route"], "system_tools")
        self.assertEqual(
            set(reading["decision"]["actions"]), {"memory_status", "cpu_status"}
        )
        # Both halves are answered, and neither is a fabricated zero: the CPU
        # sentence is present whether or not this machine reported a figure.
        self.assertIn("RAM", response.summary)
        self.assertIn("CPU", response.summary)

    def test_a_metric_word_in_something_else_does_not_turn_it_into_a_reading(self) -> None:
        """Only a status question is expanded, so "memory" in a launch is just prose."""
        understood = _intelligence().understand("open chrome")
        self.assertEqual(list(understood.intent.actions), ["open_application"])


if __name__ == "__main__":
    unittest.main()
