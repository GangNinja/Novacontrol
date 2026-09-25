"""Phase 5: tool metadata, the catalogue, and discovery by relevance.

The specification's example is the first test in the discovery class: *"Which
programs are consuming most of my memory?"* must reach the system monitor and
must not bring the phone, the browser or the code agent along with it. Every
other test here pins the rule that keeps that true — a rare word decides, the
floor refuses an unrelated tool, and a paraphrase still lands.
"""

from __future__ import annotations

import os
import unittest

from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.intelligence.capabilities import default_capabilities
from novacontrol.intelligence.intent import Capability, IntentName
from novacontrol.intelligence.registry import default_catalog
from novacontrol.planning import PlanStatus, WorkflowResult
from novacontrol.tools import (
    DEFAULT_FLOOR,
    FunctionTool,
    ToolCall,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolRegistry,
    ToolRetriever,
    ToolSchema,
    build_tool_catalog,
    default_tool_declarations,
    tool_descriptions,
    validate_tool_call,
)

SPEC_QUERY = "Which programs are consuming most of my memory?"


def shipped_retriever(**kwargs: object) -> ToolRetriever:
    catalog = build_tool_catalog(
        intents=default_catalog(),
        capabilities=default_capabilities(),
    )
    return ToolRetriever(catalog, **kwargs)  # type: ignore[arg-type]


class ToolMetadataTests(unittest.TestCase):
    def test_the_specification_s_metadata_fields_all_exist(self) -> None:
        # The specification lists ten fields; a metadata object missing any of
        # them cannot answer the questions the layer asks of it.
        payload = default_tool_declarations()[0].to_dict()
        for field in (
            "name",
            "description",
            "category",
            "capabilities",
            "input_schema",
            "output_schema",
            "risk",
            "permissions",
            "examples",
            "tags",
        ):
            self.assertIn(field, payload)

    def test_every_tool_declares_an_argument_and_a_result_shape(self) -> None:
        # The specification lists both schemas as metadata. Without them a model
        # is offered a tool with no call shape and no idea what comes back.
        catalog = build_tool_catalog(
            intents=default_catalog(),
            capabilities=default_capabilities(),
        )
        schemas = {
            tool.name: (tool.input_schema, tool.output_schema)
            for tool in catalog.tools()
            if tool.input_schema is None or tool.output_schema is None
        }
        self.assertEqual(schemas, {})
        for tool in catalog.tools():
            self.assertTrue(
                tool.output_schema is not None and tool.output_schema.parameters,
                f"{tool.name} declares no result shape",
            )

    def test_the_argument_shape_is_derived_from_the_intents_it_serves(self) -> None:
        # Not invented: ``open_application`` already requires an application, so
        # the tool that opens it is told to expect one.
        catalog = build_tool_catalog(
            intents=default_catalog(),
            capabilities=default_capabilities(),
        )
        desktop = catalog.get("desktop_controller")
        self.assertIn("application", [p.name for p in desktop.input_schema.parameters])
        monitor = catalog.get("system_monitor")
        self.assertIn("metric", [p.name for p in monitor.input_schema.parameters])

    def test_the_argument_shape_also_comes_from_the_capabilities_it_executes(self) -> None:
        # The second surface that knows an argument. ``find_file`` requires a
        # ``file`` and says ``file_manager`` carries it out, but the hand-written
        # intent entry never names the tool back — so deriving from intents alone
        # left the tool that edits files with NO call shape at all.
        catalog = build_tool_catalog(
            intents=default_catalog(),
            capabilities=default_capabilities(),
        )
        files = catalog.get("file_manager")
        parameters = {p.name: p.required for p in files.input_schema.parameters}
        self.assertTrue(parameters.get("file"), "file_manager must require a file")
        self.assertIn("folder", parameters)  # ``move_file`` allows a destination

    def test_an_executor_that_needs_an_argument_never_offers_none(self) -> None:
        # The invariant behind the derivation: wherever a capability says an
        # argument is required, the tool that executes it must offer that
        # argument. Emptiness is only honest where nothing declares anything.
        catalog = build_tool_catalog(
            intents=default_catalog(),
            capabilities=default_capabilities(),
        )
        needed: dict[str, set[str]] = {}
        for capability in default_capabilities():
            needed.setdefault(capability.executor, set()).update(capability.required or ())
        for tool in catalog.tools():
            offered = {p.name for p in tool.input_schema.parameters}
            self.assertTrue(
                needed.get(tool.name, set()) <= offered,
                f"{tool.name} needs {sorted(needed.get(tool.name, set()))} "
                f"but offers {sorted(offered)}",
            )

    def test_a_capability_name_is_missing_only_where_the_registry_names_none(self) -> None:
        # ``capabilities`` is empty when the tool is nobody's executor (an
        # introspection tool); everywhere else it names what the tool serves.
        catalog = build_tool_catalog(
            intents=default_catalog(),
            capabilities=default_capabilities(),
        )
        executors = {capability.executor for capability in default_capabilities()}
        for tool in catalog.tools():
            if not tool.capabilities:
                self.assertNotIn(
                    tool.name,
                    executors,
                    f"{tool.name} serves capabilities but names none",
                )

    def test_every_shipped_tool_is_described(self) -> None:
        for metadata in default_tool_declarations():
            self.assertTrue(metadata.description, f"{metadata.name} has no description")
            self.assertTrue(metadata.tags, f"{metadata.name} has no tags")
            self.assertTrue(metadata.examples, f"{metadata.name} has no examples")

    def test_approval_is_the_executor_s_own_rule(self) -> None:
        # The same rule ToolExecutor applies: declared permission scopes are what
        # the approval layer gates on, so metadata cannot contradict it.
        gated = ToolMetadata(
            "write_file", "Write a file", permissions=(PermissionScope.FILESYSTEM_WRITE,)
        )
        open_tool = ToolMetadata("read_clock", "Read the clock")
        self.assertTrue(gated.requires_approval())
        self.assertFalse(open_tool.requires_approval())

    def test_searchable_text_carries_the_words_a_person_uses(self) -> None:
        metadata = next(
            item for item in default_tool_declarations() if item.name == "system_monitor"
        )
        text = metadata.searchable_text().lower()
        self.assertIn("memory", text)
        self.assertIn("chewing up my memory", text)

    def test_a_volatile_operation_is_never_cacheable(self) -> None:
        monitor = next(
            item for item in default_tool_declarations() if item.name == "system_monitor"
        )
        # Live load moves between two calls; total disk space does not.
        self.assertFalse(monitor.is_cacheable({"metric": "cpu"}))
        self.assertTrue(monitor.is_cacheable({"metric": "storage"}))
        self.assertIn("volatile", monitor.cache_refusal({"metric": "cpu"}))

    def test_a_write_is_never_cacheable_whatever_its_ttl(self) -> None:
        writer = ToolMetadata("write_file", "Write a file", read_only=False, cache_ttl_s=600)
        self.assertFalse(writer.is_cacheable({}))
        self.assertIn("not read-only", writer.cache_refusal({}))

    def test_merging_takes_the_highest_risk_and_the_strictest_claim(self) -> None:
        declared = ToolMetadata("tool", "d", read_only=True, risk=RiskLevel.LOW)
        contributed = ToolMetadata("tool", "d", read_only=False, risk=RiskLevel.HIGH)
        merged = declared.merged(contributed)
        self.assertEqual(merged.risk, RiskLevel.HIGH)
        self.assertFalse(merged.read_only)


class ToolCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_tool_catalog(
            intents=default_catalog(),
            capabilities=default_capabilities(),
        )

    def test_every_executor_the_capability_registry_names_is_catalogued(self) -> None:
        executors = {capability.executor for capability in default_capabilities()}
        missing = sorted(name for name in executors if name not in self.catalog)
        self.assertEqual(missing, [])

    def test_capability_risk_reaches_the_tool(self) -> None:
        # The registry is the source of truth for risk; a declaration that
        # understates it must not win.
        catalog = build_tool_catalog(
            declared=(ToolMetadata("system_monitor", "Reads readings.", risk=RiskLevel.LOW),),
            capabilities=(
                Capability(
                    capability="run_automation",
                    intent=IntentName.RUN_AUTOMATION,
                    description="Run an automation.",
                    risk=RiskLevel.CRITICAL,
                    executor="system_monitor",
                ),
            ),
        )
        self.assertEqual(catalog.get("system_monitor").risk, RiskLevel.CRITICAL)

    def test_intent_examples_become_searchable_text(self) -> None:
        monitor = self.catalog.get("system_monitor")
        self.assertIn("memory_status", monitor.intents)
        self.assertTrue(monitor.capabilities)

    def test_a_registered_tool_is_reported_as_available_with_its_schema(self) -> None:
        registry = ToolRegistry()
        registry.register(
            FunctionTool(
                "echo",
                ToolSchema(
                    "echo",
                    "Echo a message",
                    parameters=(ToolParameter("message", "string", required=True),),
                ),
                lambda args: {"echo": args["message"]},
                required_permissions=(PermissionScope.FILESYSTEM_READ,),
            )
        )
        catalog = build_tool_catalog(registry=registry)
        echo = catalog.get("echo")
        self.assertTrue(echo.registered)
        self.assertEqual(echo.permissions, (PermissionScope.FILESYSTEM_READ,))
        self.assertIsNotNone(echo.input_schema)
        self.assertEqual(catalog.registered()[0].name, "echo")

    def test_a_registry_contribution_is_neutral_on_read_only(self) -> None:
        # Registering a tool must not turn it into a write. The merge ANDs that
        # claim, so a contribution that asserted "not read-only" as its unknown
        # would make a declared read-only tool uncacheable through registration
        # alone — the fault this neutrality exists to prevent.
        registry = ToolRegistry()
        registry.register(
            FunctionTool(
                "echo",
                ToolSchema("echo", "Echo a message", parameters=()),
                lambda args: {"echo": "hi"},
            )
        )
        catalog = build_tool_catalog(registry=registry)
        self.assertTrue(catalog.get("echo").read_only)

    def test_an_empty_catalogue_still_answers_with_the_shipped_declarations(self) -> None:
        catalog = build_tool_catalog()
        self.assertEqual(len(catalog), len(default_tool_declarations()))
        self.assertEqual(catalog.registered(), ())

    def test_tool_descriptions_are_compact_and_honest_about_the_unknown(self) -> None:
        described = tool_descriptions(self.catalog, ["system_monitor", "no_such_tool"])
        self.assertEqual(described[0]["category"], "system")
        self.assertFalse(described[0]["available"])  # nothing is registered here
        self.assertEqual(
            described[1],
            {"name": "no_such_tool", "description": "", "available": False},
        )

    def test_top_level_dict_is_safe_to_publish(self) -> None:
        payload = self.catalog.to_dict()
        self.assertEqual(payload["count"], len(self.catalog))
        self.assertIn("system", payload["categories"])


class ToolDiscoveryTests(unittest.TestCase):
    """The specification's example, and the rules that make it hold."""

    def setUp(self) -> None:
        self.retriever = shipped_retriever()

    def test_the_specification_s_example_reaches_the_system_monitor(self) -> None:
        matches = self.retriever.search(SPEC_QUERY, limit=3)

        self.assertTrue(matches)
        self.assertEqual(matches[0].name, "system_monitor")
        self.assertGreater(matches[0].score, 0.5)

    def test_unrelated_tools_are_not_offered(self) -> None:
        # The whole point: three relevant tools, not seventeen plausible ones.
        names = {match.name for match in self.retriever.search(SPEC_QUERY, limit=5)}

        self.assertIn("system_monitor", names)
        for unrelated in (
            "phone_controller",
            "browser_controller",
            "code_agent",
            "automation_manager",
        ):
            self.assertNotIn(unrelated, names)

    def test_a_paraphrase_still_lands(self) -> None:
        # "chewing up my ram" shares almost no vocabulary with the declaration.
        matches = self.retriever.search("what is chewing up my ram")
        self.assertEqual(matches[0].name, "system_monitor")

    def test_a_rare_word_decides_over_an_incidental_one(self) -> None:
        match = self.retriever.search("how much disk space is free")[0]
        self.assertEqual(match.name, "system_monitor")
        self.assertIn("disk", match.matched_terms)

    def test_a_request_nothing_can_serve_returns_no_tool(self) -> None:
        # "no tool fits this" is a real answer, and the one that stops a planner
        # reaching for an unrelated tool just to have one.
        self.assertEqual(self.retriever.search("sing me a lullaby about penguins"), ())

    def test_every_match_reports_its_evidence(self) -> None:
        match = self.retriever.search(SPEC_QUERY)[0]
        self.assertGreater(match.lexical, 0.0)
        self.assertTrue(match.matched_terms)
        self.assertIn("system_monitor", match.reason)
        self.assertIn("score", match.to_dict())

    def test_scores_are_deterministic_and_cached(self) -> None:
        first = self.retriever.search(SPEC_QUERY)
        second = self.retriever.search(SPEC_QUERY)

        self.assertEqual(first, second)
        self.assertEqual(self.retriever.hits, 1)
        self.assertEqual(self.retriever.misses, 1)

    def test_semantic_matching_can_be_turned_off(self) -> None:
        lexical_only = shipped_retriever(use_semantic=False)
        match = lexical_only.search(SPEC_QUERY)[0]
        self.assertEqual(match.semantic, 0.0)
        self.assertEqual(match.name, "system_monitor")

    def test_a_supplied_embedder_is_used_instead_of_the_built_in_one(self) -> None:
        class FakeEmbedder:
            name = "fake"

            def embed(self, texts: object) -> list[list[float]]:
                return [[0.5, 0.5] for _ in texts]  # type: ignore[union-attr]

        retriever = shipped_retriever(embedder=FakeEmbedder())
        self.assertEqual(retriever.to_dict()["embedder"], "fake")
        self.assertTrue(retriever.search(SPEC_QUERY))

    def test_filters_narrow_before_ranking(self) -> None:
        system = self.retriever.search(SPEC_QUERY, category=ToolCategory.SYSTEM, limit=3)
        self.assertEqual([match.name for match in system], ["system_monitor"])

        tagged = self.retriever.search("programs eating memory", tags=("memory",))
        self.assertIn("system_monitor", {match.name for match in tagged})

    def test_a_risk_ceiling_excludes_riskier_tools(self) -> None:
        low_risk = self.retriever.search(
            "run the tests and tell me what failed", max_risk=RiskLevel.LOW, limit=5
        )
        self.assertNotIn("code_agent", {match.name for match in low_risk})

    def test_an_unregistered_tool_is_offered_but_marked_unavailable(self) -> None:
        # A capability this build can dispatch but has no callable behind is
        # reported as exactly that, never hidden and never called "ready".
        match = self.retriever.search(SPEC_QUERY)[0]
        self.assertFalse(match.available)
        self.assertFalse(match.to_dict()["available"])
        self.assertEqual(self.retriever.search(SPEC_QUERY, require_registered=True), ())

    def test_a_request_with_no_content_words_still_finds_its_tool(self) -> None:
        # "what can you do?" is all function words, so the lexical and semantic
        # layers both see an empty query and every tool scores zero — the tool
        # whose own example is that phrase must still be found.
        matches = shipped_retriever().search("what can you do?")

        self.assertTrue(matches)
        self.assertEqual(matches[0].name, "capabilities")

    def test_a_phrase_fallback_does_not_invent_a_match(self) -> None:
        retriever = shipped_retriever()
        self.assertEqual(retriever.search("can you help me?"), ())
        # And a query with content words still ranks by them, not by phrasing.
        self.assertEqual(retriever.search(SPEC_QUERY)[0].name, "system_monitor")

    def test_the_floor_is_reported(self) -> None:
        self.assertEqual(self.retriever.to_dict()["floor"], DEFAULT_FLOOR)
        self.assertGreater(DEFAULT_FLOOR, 0.0)

    def test_a_limit_of_one_returns_the_single_best_tool(self) -> None:
        self.assertEqual(self.retriever.prompt_tools(SPEC_QUERY, limit=1), ("system_monitor",))

    def test_an_empty_query_returns_nothing(self) -> None:
        self.assertEqual(self.retriever.search("   "), ())


class ApplicationDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """Discovery as the application wires it, including what the model is shown."""

    async def asyncSetUp(self) -> None:
        os.environ["NOVACONTROL_NLU_ALLOW_LLM"] = "false"
        from novacontrol.application import NovaControlApplication, _escalation_prompt

        self.escalation_prompt = _escalation_prompt
        self.app = NovaControlApplication()
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def test_a_shortlist_carries_a_real_call_shape(self) -> None:
        entry = next(
            item
            for item in self.app.discovered_tools("open chrome", limit=3)
            if item["name"] == "desktop_controller"
        )
        self.assertIn("application", entry["parameters"])

    async def test_the_read_only_tools_are_registered_and_really_run(self) -> None:
        from novacontrol.tools import ToolRequest

        self.assertIn("machine_facts", self.app.tool_catalog.registered_names())
        self.assertIn("capabilities", self.app.tool_catalog.registered_names())
        self.assertIn("installed_applications", self.app.tool_catalog.registered_names())

        info = await self.app.tool_executor.execute(ToolRequest("machine_facts", {"scope": "all"}))
        caps = await self.app.tool_executor.execute(ToolRequest("capabilities", {}))

        self.assertEqual(info.status.value, "completed")
        self.assertTrue(info.output["platform"])
        self.assertGreaterEqual(caps.output["count"], 1)

    async def test_the_installed_application_list_is_real_and_reusable(self) -> None:
        # The specification names installed applications as an operation worth
        # caching, and it is the third surface of the same index the desktop
        # controller opens programs by name from — so it is the machine's real
        # inventory rather than a second opinion about it.
        from novacontrol.tools import ToolRequest

        first = await self.app.tool_executor.execute(ToolRequest("installed_applications", {}))
        self.assertEqual(first.status.value, "completed")
        self.assertIn("applications", first.output)
        self.assertIsInstance(first.output["count"], int)
        # ``available`` says whether the machine could be enumerated at all, so a
        # platform with no Start Menu index reports that rather than "none".
        self.assertIsInstance(first.output["available"], bool)
        self.assertEqual(first.output["available"], first.output["count"] > 0)

        again = await self.app.tool_executor.execute(ToolRequest("installed_applications", {}))
        self.assertTrue(again.cached)

    async def test_the_application_list_narrows_by_name(self) -> None:
        from novacontrol.tools import ToolRequest

        filtered = await self.app.tool_executor.execute(
            ToolRequest("installed_applications", {"filter": "zzzz-not-installed"})
        )
        self.assertEqual(filtered.status.value, "completed")
        self.assertEqual(filtered.output["count"], 0)
        self.assertEqual(filtered.output["applications"], [])

    async def test_a_cached_tool_returns_only_what_it_declared(self) -> None:
        # The caching contract, checked against the payload it caches: a tool
        # that says "reuse this for ten minutes" must not answer with a number
        # that moves in one.
        from novacontrol.tools import ToolRequest

        await self.app.tool_executor.execute(ToolRequest("machine_facts", {"scope": "all"}))
        again = await self.app.tool_executor.execute(ToolRequest("machine_facts", {"scope": "all"}))
        self.assertTrue(again.cached)

        for volatile in ("disk_free_bytes", "uptime_seconds", "used_bytes", "percent"):
            self.assertNotIn(volatile, again.output)

    async def test_the_application_catalogues_every_tool_it_can_dispatch(self) -> None:
        # Every executor the capability registry names must be discoverable: a
        # capability whose tool is missing from the catalogue is work the system
        # can do and cannot find.
        executors = {
            capability.executor for capability in self.app.intelligence.capabilities.all()
        }
        missing = sorted(name for name in executors if name not in self.app.tool_catalog)
        self.assertEqual(missing, [])
        status = self.app.tools_status()
        self.assertIn("discovery", status)
        self.assertIn("cache", status)

    async def test_the_plan_payload_carries_the_discovered_shortlist(self) -> None:
        from novacontrol.brain.models import BrainRequest

        _kind, payload = await self.app._handle_plan(BrainRequest(text=SPEC_QUERY), SPEC_QUERY)

        self.assertEqual(payload["tools"][0]["tool"], "system_monitor")
        self.assertIn("plan", payload)

    async def test_discovery_returns_scores_and_evidence(self) -> None:
        matches = self.app.discover_tools(SPEC_QUERY, limit=2)
        self.assertEqual(matches[0]["tool"], "system_monitor")
        self.assertGreater(matches[0]["score"], 0.5)
        self.assertIn("reason", matches[0])

    async def test_the_model_is_shown_a_shortlist_not_the_whole_toolbox(self) -> None:
        shortlist = self.app.discovered_tools(SPEC_QUERY, limit=3)
        self.assertTrue(shortlist)
        self.assertLess(len(shortlist), len(self.app.tool_catalog))
        names = {entry["name"] for entry in shortlist}
        self.assertIn("system_monitor", names)
        self.assertNotIn("phone_controller", names)

    async def test_the_escalation_prompt_carries_only_the_discovered_tools(self) -> None:
        shortlist = self.app.discovered_tools(SPEC_QUERY, limit=1)
        prompt = self.escalation_prompt("check my memory", self._workflow(), shortlist)

        self.assertIn("Tools that could carry this out", prompt)
        self.assertIn("system_monitor", prompt)
        self.assertNotIn("phone_controller", prompt)

    async def test_an_empty_shortlist_is_stated_rather_than_implied(self) -> None:
        prompt = self.escalation_prompt("sing me a lullaby", self._workflow(), ())
        self.assertIn("No tool in this installation fits this goal", prompt)

    @staticmethod
    def _workflow() -> WorkflowResult:
        return WorkflowResult(
            plan_id="p",
            status=PlanStatus.FAILED,
            step_outputs={},
            errors={"read-memory": "no reading"},
            summary="it failed",
        )


class ToolCallValidationTests(unittest.TestCase):
    """Schema validation is the gate before execution; it lives here so the
    discovery layer's callers can validate without running anything."""

    def setUp(self) -> None:
        self.schema = ToolSchema(
            "run_command",
            "Run a command",
            parameters=(
                ToolParameter("command", "string", required=True),
                ToolParameter("timeout", "integer", required=False),
            ),
        )

    def test_a_valid_call_produces_a_request(self) -> None:
        validation = validate_tool_call(
            self.schema, ToolCall("run_command", {"command": "pytest", "timeout": 30})
        )
        self.assertTrue(validation.valid)
        self.assertEqual(validation.request().arguments["command"], "pytest")

    def test_an_invalid_call_can_never_produce_a_request(self) -> None:
        validation = validate_tool_call(self.schema, ToolCall("run_command", {}))
        self.assertFalse(validation.valid)
        with self.assertRaises(ValueError):
            validation.request()
        self.assertIn("Missing required argument", validation.errors[0])


if __name__ == "__main__":
    unittest.main()
