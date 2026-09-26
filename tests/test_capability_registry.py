"""Phase 9.2/9.3: the capability registry, and discovery over it.

One idea carries this file: **the registry is the answer to "what can this
installation do?"**, and it must answer from facts rather than from a second
copy of them. So the tests here check three things over and over:

  * a capability says what the specification asks it to say — its id, category,
    tools, models, inputs, outputs, risk, permissions, availability, examples
    and tags — and the fields it does NOT declare are filled from the catalogue
    it is attached to, not invented;
  * availability is EARNED: a capability whose model is not here, or whose tool
    is not registered, reports unavailable with the reason, while a capability
    nobody can check reports unknown rather than available;
  * discovery ranks and explains, and NEVER runs anything — a match is a
    candidate, and finding one is not permission to do it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.decision import DecisionRoute
from novacontrol.decision.engine import DecisionEngine
from novacontrol.intelligence.intent import (
    Capability,
    CapabilityAvailability,
    CapabilityRegistry,
    CapabilitySource,
    IntentName,
    RiskLevel,
    StructuredIntent,
    resolve_intent,
)
from novacontrol.intelligence.capabilities import default_capabilities
from novacontrol.tools import ToolCatalog
from novacontrol.tools.metadata import ToolCategory, ToolMetadata
from novacontrol.tools.selection import ToolSelector

#: The fields the specification names for a capability's metadata. Every one of
#: them must be answerable for EVERY capability, declared or projected.
_REQUIRED_METADATA = (
    "capability_id",
    "name",
    "description",
    "category",
    "tools",
    "required_models",
    "supported_inputs",
    "supported_outputs",
    "risk_level",
    "permissions",
    "availability",
    "examples",
    "tags",
)


def _metadata(name: str, **kwargs: Any) -> ToolMetadata:
    """A tool declaration with the shipped category/risk defaults overridden."""
    return ToolMetadata(
        name=name,
        description=kwargs.pop("description", f"The {name} tool."),
        category=kwargs.pop("category", ToolCategory.SYSTEM),
        **kwargs,
    )


class CapabilityMetadataTests(unittest.TestCase):
    """9.2: what a capability publishes about itself."""

    def test_the_specifications_metadata_is_present_on_every_capability(self) -> None:
        for capability in default_capabilities():
            declared = capability.to_dict()
            for field in _REQUIRED_METADATA:
                self.assertIn(field, declared, f"{capability.id} is missing {field}")

    def test_the_id_is_a_dotted_name_and_the_name_is_readable(self) -> None:
        capability = Capability(
            capability="get_ram",
            executor="system_monitor",
            capability_id="system.get_ram",
            description="Report free memory.",
            required_models=("chat",),
            supported_inputs=("metric",),
            supported_outputs=("bytes",),
            risk=RiskLevel.LOW,
            permissions=("system.read",),
            examples=("How much RAM is free?",),
            tags=("ram", "memory"),
        )

        self.assertEqual(capability.id, "system.get_ram")
        self.assertEqual(capability.display_name, "get ram")
        self.assertEqual(capability.category_name, "system")
        self.assertEqual(capability.risk_level, RiskLevel.LOW)
        self.assertEqual(capability.required_models, ("chat",))
        self.assertEqual(capability.permissions, ("system.read",))
        self.assertEqual(capability.tags, ("ram", "memory"))
        self.assertFalse(capability.requires_confirmation)

    def test_a_capability_with_no_declared_id_gets_one_from_its_executor(self) -> None:
        capability = Capability(capability="read file", executor="file_manager")

        self.assertEqual(capability.id, "filesystem.read_file")
        self.assertEqual(capability.category_name, "filesystem")

    def test_medium_risk_asks_before_it_runs(self) -> None:
        for risk in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
            self.assertTrue(Capability(capability="x", risk=risk).requires_confirmation)
        self.assertFalse(Capability(capability="x", risk=RiskLevel.LOW).requires_confirmation)

    def test_a_capability_needs_a_name(self) -> None:
        with self.assertRaises(ValueError):
            Capability(capability="   ")


class CapabilityRegistrationTests(unittest.TestCase):
    """9.2: registering, and the three sources a registry indexes."""

    def test_registration_is_queryable_by_id(self) -> None:
        registry = CapabilityRegistry()
        registered = registry.register_action(
            "run_tests",
            capability_id="developer.run_tests",
            intent=IntentName.RUN_COMMAND,
            tags=("test", "tests"),
        )

        self.assertEqual(registry.get("developer.run_tests"), registered)
        self.assertEqual(registry.require("developer.run_tests"), registered)
        self.assertEqual(registered.source, CapabilitySource.ACTION)

    def test_only_a_declared_capability_answers_the_routing_question(self) -> None:
        """``for_intent``/``best`` are the ORIGINAL contract and keep meaning it.

        An action this application carries out is machinery, not a user-facing
        verb, so it must not silently become the capability a request routes to.
        """
        registry = CapabilityRegistry()
        registry.register_action(
            "run_tests", capability_id="developer.run_tests", intent=IntentName.RUN_COMMAND
        )
        declared = Capability(
            capability="run_command",
            intent=IntentName.RUN_COMMAND,
            capability_id="legacy.run_command",
        )
        registry.register(declared)

        self.assertEqual(registry.best(IntentName.RUN_COMMAND), declared)
        self.assertNotIn(
            "developer.run_tests", [item.id for item in registry.for_intent(IntentName.RUN_COMMAND)]
        )

    def test_a_bare_name_finds_the_capability_too(self) -> None:
        registry = CapabilityRegistry()
        registry.register_action("locate_project", capability_id="developer.locate_project")

        self.assertIsNotNone(registry.get("developer.locate_project"))
        self.assertEqual(registry.capability_ids(), ("developer.locate_project",))

    def test_two_different_capabilities_may_not_share_an_id(self) -> None:
        registry = CapabilityRegistry()
        registry.register_action("run_tests", capability_id="developer.run_tests")

        with self.assertRaises(ValueError):
            registry.register_action("run_checks", capability_id="developer.run_tests")

    def test_the_same_declaration_twice_is_not_a_conflict(self) -> None:
        registry = CapabilityRegistry()
        registry.register_action("run_tests", capability_id="developer.run_tests", tags=("a",))
        registry.register_action("run_tests", capability_id="developer.run_tests", tags=("a",))

        self.assertEqual(len(registry.everything()), 1)

    def test_an_unknown_id_is_an_error_rather_than_a_none(self) -> None:
        registry = CapabilityRegistry()

        with self.assertRaises(KeyError):
            registry.require("system.does_not_exist")

    def test_the_inventory_follows_the_catalogue_rather_than_a_boot_snapshot(self) -> None:
        """An attached catalogue is read on EVERY query, not copied once."""
        registry = CapabilityRegistry()
        catalog = ToolCatalog([_metadata("machine_facts", description="Stable machine facts.")])
        registry.attach(tools=catalog, tool_names=catalog.names)
        # Declared but with no callable behind it: not something this can DO.
        self.assertIsNone(registry.get("system.machine_facts"))

        catalog.register(_metadata("machine_facts", description="Stable machine facts.", registered=True))

        capability = registry.get("system.machine_facts")
        assert capability is not None
        self.assertEqual(capability.source, CapabilitySource.TOOL)
        self.assertIn("system.machine_facts", registry.capability_ids())
        self.assertEqual(registry.report()["by_source"]["tool"], 1)

    def test_tools_are_projected_from_the_catalogue_that_declares_them(self) -> None:
        registry = CapabilityRegistry()
        catalog = ToolCatalog(
            [
                _metadata(
                    "installed_applications",
                    description="List the applications installed on this machine.",
                    examples=("what apps do I have?",),
                    tags=("apps", "installed"),
                    risk=RiskLevel.LOW,
                )
            ]
        )
        registry.attach(tools=catalog, tool_names=catalog.names)

        self.assertEqual(registry.register_tools(catalog.registered() or catalog.tools()), 1)
        projected = registry.get("system.installed_applications")
        assert projected is not None
        self.assertEqual(projected.source, CapabilitySource.TOOL)
        self.assertEqual(projected.description, "List the applications installed on this machine.")
        self.assertEqual(projected.examples, ("what apps do I have?",))
        self.assertEqual(projected.tags, ("apps", "installed"))
        self.assertEqual(projected.risk, RiskLevel.LOW)


class CapabilityAvailabilityTests(unittest.TestCase):
    """9.2: availability is measured against the machine, never assumed."""

    def test_a_capability_with_no_requirements_is_available(self) -> None:
        registry = CapabilityRegistry()

        registry.register_action("remember", capability_id="memory.remember")

        availability, reason = registry.availability_of("memory.remember")
        self.assertIs(availability, CapabilityAvailability.AVAILABLE)
        self.assertEqual(reason, "")

    def test_a_missing_model_makes_it_unavailable_and_names_the_model(self) -> None:
        registry = CapabilityRegistry(model_probe=lambda role: False)
        registry.register_action(
            "analyze_screen", capability_id="vision.analyze_screen", required_models=("vision",)
        )

        availability, reason = registry.availability_of("vision.analyze_screen")

        self.assertIs(availability, CapabilityAvailability.UNAVAILABLE)
        self.assertIn("vision", reason)

    def test_a_present_model_makes_it_available(self) -> None:
        registry = CapabilityRegistry(model_probe=lambda role: role == "vision")
        registry.register_action(
            "analyze_screen", capability_id="vision.analyze_screen", required_models=("vision",)
        )

        self.assertIs(
            registry.availability_of("vision.analyze_screen")[0],
            CapabilityAvailability.AVAILABLE,
        )

    def test_needing_a_model_nobody_can_probe_is_unknown_not_available(self) -> None:
        registry = CapabilityRegistry()  # no model probe is wired
        registry.register_action("chat", capability_id="chat.chat", required_models=("chat",))

        availability, reason = registry.availability_of("chat.chat")

        self.assertIs(availability, CapabilityAvailability.UNKNOWN)
        self.assertIn("no model probe", reason)

    def test_a_tool_this_installation_does_not_have_is_unavailable(self) -> None:
        registry = CapabilityRegistry(tool_names=lambda: ("file_manager",))
        registry.register_action(
            "run_tests", capability_id="developer.run_tests", tools=("test_runner",)
        )

        availability, reason = registry.availability_of("developer.run_tests")

        self.assertIs(availability, CapabilityAvailability.UNAVAILABLE)
        self.assertIn("test_runner", reason)

    def test_a_capability_declared_unavailable_says_why(self) -> None:
        registry = CapabilityRegistry()
        registry.register_action(
            "reason",
            capability_id="system.reason",
            availability=CapabilityAvailability.UNAVAILABLE,
            availability_reason="no executor carries reasoning locally",
        )

        availability, reason = registry.availability_of("system.reason")

        self.assertIs(availability, CapabilityAvailability.UNAVAILABLE)
        self.assertEqual(reason, "no executor carries reasoning locally")

    def test_the_report_separates_what_can_run_from_what_cannot(self) -> None:
        registry = CapabilityRegistry(model_probe=lambda role: False)
        registry.register_action("remember", capability_id="memory.remember")
        registry.register_action(
            "analyze_screen", capability_id="vision.analyze_screen", required_models=("vision",)
        )

        report = registry.report()

        self.assertEqual(report["total"], 2)
        self.assertEqual(report["available"], 1)
        self.assertEqual(report["unavailable"], 1)
        self.assertEqual(report["unavailable_ids"], ["vision.analyze_screen"])
        self.assertTrue(report["model_probe"])


class CapabilityDiscoveryTests(unittest.TestCase):
    """9.3: "what capabilities are available for this task?" — without running any."""

    def setUp(self) -> None:
        self.registry = CapabilityRegistry()
        for capability in default_capabilities():
            self.registry.register(capability)
        self.registry.register_action(
            "run_tests",
            capability_id="developer.run_tests",
            description="Run a located project's test suite and capture its exit code.",
            tags=("test", "tests", "suite", "pytest", "run", "check", "failing"),
            examples=("Run the test suite.",),
        )
        self.registry.register_action(
            "analyze_result",
            capability_id="developer.analyze_result",
            description="Read captured output and name what failed, deterministically.",
            tags=("analyze", "failure", "failures", "error", "logs", "traceback", "why"),
        )

    def test_the_specifications_example_finds_the_developer_capabilities(self) -> None:
        matches = self.registry.discover("Check why my Python project is failing.")
        found = [match.capability_id for match in matches]

        # The specification's example task. This installation HAS these two of
        # the four it lists: inspecting the project, and running its suite.
        # ``developer.read_logs`` and ``developer.inspect_dependencies`` are not
        # capabilities here — reading a log is ``filesystem.read`` on a log file
        # and checking dependencies is a command — so they are absent rather
        # than invented, which is the honest answer to "what can you do?".
        self.assertIn("developer.inspect_project", found)
        self.assertIn("developer.run_tests", found)
        # The project-inspection capability is the one the words point at, so it
        # is ranked first — discovery is a ranking, not a set.
        self.assertEqual(found[0], "developer.inspect_project")

    def test_the_specifications_own_example_capability_ids_exist(self) -> None:
        for capability_id in (
            "system.get_ram",
            "browser.open_url",
            "browser.search",
            "filesystem.read",
            "filesystem.write",
            "developer.run_tests",
        ):
            self.assertIsNotNone(self.registry.get(capability_id), capability_id)

    def test_a_match_carries_the_evidence_for_choosing_it(self) -> None:
        match = self.registry.discover("Run the test suite.")[0]

        self.assertEqual(match.capability_id, "developer.run_tests")
        self.assertGreater(match.score, 0.0)
        self.assertTrue(match.reasons)
        self.assertEqual(match.category, "developer")
        self.assertIn(match.availability, tuple(CapabilityAvailability))
        self.assertTrue(match.available)
        self.assertEqual(match.requires_confirmation, False)

    def test_an_id_the_task_names_wins(self) -> None:
        matches = self.registry.discover("please run system.get_ram for me")

        self.assertEqual(matches[0].capability_id, "system.get_ram")
        self.assertIn("the task names system.get_ram", matches[0].reasons)

    def test_naming_a_capability_is_evidence_stronger_than_a_shared_word(self) -> None:
        registry = CapabilityRegistry()
        registry.register_action(
            "run_tests",
            capability_id="developer.run_tests",
            tags=("run", "tests", "project"),
        )
        registry.register_action(
            "run_checks",
            capability_id="developer.run_checks",
            tags=("run", "tests", "project"),
        )

        matches = registry.discover("Use developer.run_checks on this project.")

        self.assertEqual(matches[0].capability_id, "developer.run_checks")
        self.assertIn("the task names developer.run_checks", matches[0].reasons)

    def test_discovery_can_be_restricted_to_one_intent(self) -> None:
        self.registry.register(
            Capability(
                capability="run_command",
                intent=IntentName.RUN_COMMAND,
                capability_id="developer.run_command",
                description="Run a shell command and capture its exit code.",
                tags=("run", "command", "shell", "tests"),
            )
        )

        matches = self.registry.discover("run the tests", intent=IntentName.RUN_COMMAND)

        self.assertTrue(matches)
        for match in matches:
            self.assertEqual(match.intent, IntentName.RUN_COMMAND.value)
        # An action this application carries out is not part of the answer a
        # routing query gets: it is machinery, not a capability a request
        # routes to (see ``for_intent``).
        self.assertNotIn(
            "developer.run_tests", [match.capability_id for match in matches]
        )

    def test_an_unavailable_capability_is_left_out_unless_it_is_asked_for(self) -> None:
        registry = CapabilityRegistry(model_probe=lambda role: False)
        registry.register_action(
            "analyze_screen",
            capability_id="vision.analyze_screen",
            description="Look at the screen and answer about it.",
            required_models=("vision",),
            tags=("screenshot", "screen", "look"),
        )

        self.assertEqual(registry.discover("look at my screen"), ())

        asked = registry.discover("look at my screen", include_unavailable=True)
        self.assertEqual(asked[0].capability_id, "vision.analyze_screen")
        self.assertIs(asked[0].availability, CapabilityAvailability.UNAVAILABLE)
        self.assertIn("vision", asked[0].unavailable_reason)

    def test_the_limit_is_honoured(self) -> None:
        self.assertEqual(len(self.registry.discover("run the tests", limit=1)), 1)
        self.assertGreater(len(self.registry.discover("run the tests", limit=0)), 1)

    def test_discovery_offers_and_never_executes(self) -> None:
        """A match is advice: finding a capability must not run its tool."""
        ran: list[str] = []

        class _Tools:
            def names(self) -> tuple[str, ...]:
                ran.append("asked")
                return ("file_manager",)

        registry = CapabilityRegistry(tools=_Tools(), tool_names=lambda: ("file_manager",))
        registry.register_action("run_tests", capability_id="developer.run_tests")

        registry.discover("run the tests")

        # It may ASK the catalogue what exists (that is how it tells available
        # from unavailable) and it may never ask anything to RUN.
        self.assertEqual(ran, ["asked"])

    def test_an_unknown_task_finds_nothing_rather_than_everything(self) -> None:
        self.assertEqual(self.registry.discover("zzzz qqqq"), ())


class DecisionEngineCapabilityTests(unittest.TestCase):
    """9.2: the decision engine asks the registry instead of keeping its own copy."""

    def _engine(self, registry: CapabilityRegistry) -> DecisionEngine:
        return DecisionEngine(capabilities=registry)

    def _request(self, text: str, name: str = "system_info") -> StructuredIntent:
        intent = resolve_intent(name)
        assert intent is not None
        return StructuredIntent(
            raw_input=text,
            normalized_input=text.lower(),
            intent=intent,
            confidence=0.9,
        )

    def test_the_decision_names_the_capability_the_registry_matched(self) -> None:
        registry = CapabilityRegistry(model_probe=lambda role: True)
        for capability in default_capabilities():
            registry.register(capability)

        decision = self._engine(registry).decide(
            self._request("how much ram is free", "memory_status")
        )

        self.assertEqual(decision.metadata["capability_id"], "system.get_ram")
        self.assertEqual(decision.metadata["capability_availability"], "available")
        self.assertEqual(decision.metadata["capability_unavailable_reason"], "")
        self.assertTrue(decision.metadata["capability_registered"])

    def test_an_unavailable_capability_is_reported_with_its_reason(self) -> None:
        registry = CapabilityRegistry(model_probe=lambda role: False)
        for capability in default_capabilities():
            registry.register(capability)

        decision = self._engine(registry).decide(
            self._request("explain this python error", "code_explanation")
        )

        # The route is unchanged — availability is a fact about the MACHINE, and
        # a decision that re-routed with the state of an unrelated install would
        # send the same words two different ways. What changes is what it SAYS.
        self.assertEqual(decision.metadata["capability_id"], "developer.explain_code")
        self.assertEqual(decision.metadata["capability_availability"], "unavailable")
        self.assertIn("chat", decision.metadata["capability_unavailable_reason"])

    def test_with_no_registry_nothing_is_claimed(self) -> None:
        decision = self._engine(CapabilityRegistry()).decide(
            self._request("how much ram is free", "memory_status")
        )

        self.assertEqual(decision.metadata["capability_id"], "")
        self.assertFalse(decision.metadata["capability_registered"])
        self.assertEqual(decision.metadata["capability_availability"], "")

    def test_a_vision_reading_still_routes_to_vision_when_no_model_is_here(self) -> None:
        registry = CapabilityRegistry(model_probe=lambda role: False)
        for capability in default_capabilities():
            registry.register(capability)

        decision = self._engine(registry).decide(
            self._request("look at my screen", "screenshot_analysis")
        )

        self.assertIs(decision.route, DecisionRoute.VISION)


class ToolSelectorCapabilityTests(unittest.TestCase):
    """9.2: the selector reports the registry's verdict beside its choice."""

    def _selector(self, registry: CapabilityRegistry) -> ToolSelector:
        return ToolSelector(
            capabilities=registry,
            registered=lambda: ("file_manager", "terminal"),
        )

    def _declare_run_tests(
        self, registry: CapabilityRegistry, *, model_here: bool
    ) -> None:
        """A DECLARED capability: the selector asks the routing question, so a
        declaration is what it looks up (an action is machinery, not a verb)."""
        registry.attach(model_probe=lambda role: model_here)
        registry.register(
            Capability(
                capability="run_command",
                intent=IntentName.RUN_COMMAND,
                capability_id="developer.run_command",
                required_models=("chat",),
                executor="orchestrator",
                tools=("terminal",),
            )
        )

    def _intent(self, name: str) -> StructuredIntent:
        intent = resolve_intent(name)
        assert intent is not None
        return StructuredIntent(
            raw_input="run the tests", normalized_input="run the tests", intent=intent
        )

    def test_the_choice_carries_availability_and_why(self) -> None:
        registry = CapabilityRegistry()
        self._declare_run_tests(registry, model_here=False)

        selection = self._selector(registry).select(self._intent("run_command"))

        self.assertEqual(selection.capability, "run_command")
        self.assertEqual(selection.availability, "unavailable")
        self.assertIn("chat", selection.availability_reason)
        self.assertTrue(selection.degraded)

    def test_a_healthy_capability_is_not_degraded(self) -> None:
        registry = CapabilityRegistry()
        self._declare_run_tests(registry, model_here=True)

        selection = self._selector(registry).select(self._intent("run_command"))

        self.assertEqual(selection.availability, "available")
        self.assertEqual(selection.availability_reason, "")
        self.assertFalse(selection.degraded)

    def test_the_degradation_survives_into_the_dict_a_caller_reads(self) -> None:
        registry = CapabilityRegistry()
        self._declare_run_tests(registry, model_here=False)

        payload = self._selector(registry).select(self._intent("run_command")).to_dict()

        self.assertEqual(payload["availability"], "unavailable")
        self.assertIn("chat", payload["availability_reason"])
        self.assertTrue(payload["degraded"])
        # Every candidate says the same thing, so a caller reading the rejected
        # options is not told less than the one that won.
        self.assertTrue(all(item["availability"] == "unavailable" for item in payload["candidates"]))

    def test_with_no_registry_availability_is_silent_not_optimistic(self) -> None:
        selection = self._selector(CapabilityRegistry()).select(self._intent("run_command"))

        self.assertEqual(selection.availability, "")
        self.assertFalse(selection.degraded)

    def test_the_observer_is_told_and_never_changes_the_answer(self) -> None:
        registry = CapabilityRegistry(model_probe=lambda role: True)
        self._declare_run_tests(registry, model_here=True)
        seen: list[str] = []
        selector = ToolSelector(
            capabilities=registry,
            registered=lambda: ("terminal",),
            observer=lambda selection: seen.append(selection.tool),
        )

        selection = selector.select(self._intent("run_command"))

        self.assertEqual(seen, [selection.tool])

    def test_an_observer_that_fails_is_reported_and_does_not_take_the_selection(self) -> None:
        """Isolation, without silence: a broken watcher is findable in the log."""
        registry = CapabilityRegistry(model_probe=lambda role: True)
        self._declare_run_tests(registry, model_here=True)

        def broken(_selection: Any) -> None:
            raise RuntimeError("this watcher is broken")

        selector = ToolSelector(
            capabilities=registry, registered=lambda: ("terminal",), observer=broken
        )

        with self.assertLogs("novacontrol.tools.selection", level="WARNING") as logged:
            selection = selector.select(self._intent("run_command"))

        # The answer is unchanged and still returned: the catalog names no tool
        # for the intent, so the capability's own executor is offered.
        self.assertEqual(selection.tool, "orchestrator")
        self.assertIn("this watcher is broken", "\n".join(logged.output))


class ApplicationCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """9.2/9.3: what the running application declares, and what it admits it cannot."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = NovaControlApplication(data_dir=Path(self._tmp.name))
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    async def test_the_application_registers_its_own_actions(self) -> None:
        registry = self.app.capabilities
        action_ids = (
            "system.read_metric",
            "developer.locate_project",
            "developer.run_tests",
            "developer.collect_output",
            "developer.analyze_result",
            "developer.summarize_result",
        )

        for capability_id in action_ids:
            capability = registry.get(capability_id)
            self.assertIsNotNone(capability, capability_id)
            assert capability is not None
            self.assertEqual(capability.source, CapabilitySource.ACTION, capability_id)

    async def test_a_step_that_dispatches_to_a_tool_is_not_declared_twice(self) -> None:
        """The catalogue already says what the tool is; the registry does not restate it."""
        registry = self.app.capabilities
        tool_ids = {
            capability.id
            for capability in registry.everything()
            if capability.source is CapabilitySource.TOOL
        }
        action_ids = {
            capability.id
            for capability in registry.everything()
            if capability.source is CapabilitySource.ACTION
        }

        self.assertEqual(tool_ids & action_ids, set())
        self.assertEqual(tool_ids, {f"system.{name}" for name in self.app.tool_catalog.registered_names()})

    async def test_the_gap_it_cannot_close_is_declared_rather_than_advertised(self) -> None:
        availability, reason = self.app.capabilities.availability_of("system.reason")

        self.assertIs(availability, CapabilityAvailability.UNAVAILABLE)
        self.assertIn("reason", reason)

    async def test_the_running_tools_are_capabilities_too(self) -> None:
        registry = self.app.capabilities

        for metadata in self.app.tool_catalog.registered():
            projected = registry.get(f"system.{metadata.name}")
            self.assertIsNotNone(projected, metadata.name)
            self.assertEqual(projected.source, CapabilitySource.TOOL)

    async def test_the_application_answers_the_specifications_question(self) -> None:
        matches = self.app.capabilities.discover("Check why my Python project is failing.")

        found = [match.capability_id for match in matches]
        self.assertIn("developer.inspect_project", found)
        self.assertIn("developer.run_tests", found)
        self.assertEqual(found[0], "developer.inspect_project")

    async def test_the_vision_capability_follows_the_vision_pipeline(self) -> None:
        """Availability is measured from the provider this app would really use."""
        capability = self.app.capabilities.get("vision.analyze_screen")
        assert capability is not None
        availability, _reason = self.app.capabilities.availability_of(capability)
        expected = bool(self.app.vision_manager.status()["provider"]["available"])

        self.assertEqual(
            availability is CapabilityAvailability.AVAILABLE,
            expected,
            "the registry and the vision pipeline disagree about whether images can be read",
        )


if __name__ == "__main__":
    unittest.main()
