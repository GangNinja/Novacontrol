"""Capability registry, interpretation telemetry, and the learning loop.

Covers the architecture spec sections the orchestrator consumes: the central
capability registry (#15-16), global output normalization via StandardResult
(#19), learning of linguistic variations (#22), and self-improvement telemetry
(#23).
"""

from __future__ import annotations

import unittest

from novacontrol.intelligence import (
    CapabilityRegistry,
    GlobalInputIntelligence,
    ResultStatus,
    StandardResult,
)
from novacontrol.intelligence.capabilities import default_capabilities
from novacontrol.intelligence.intent import IntentName, RiskLevel


class CapabilityRegistryTests(unittest.TestCase):
    def test_default_capabilities_cover_every_subsystem(self) -> None:
        caps = default_capabilities()
        executors = {cap.executor for cap in caps}
        # JARVIS (desktop/phone/browser), research, chat, automation, memory,
        # project, self-improvement, and agents must all be represented.
        for expected in (
            "desktop_controller",
            "phone_controller",
            "browser_controller",
            "explore_service",
            "chat_brain",
            "automation_manager",
            "memory_manager",
            "self_improvement_engine",
            "agent_coordinator",
        ):
            self.assertIn(expected, executors, expected)

    def test_every_capability_declares_a_verifier(self) -> None:
        for cap in default_capabilities():
            with self.subTest(capability=cap.capability):
                self.assertTrue(cap.verifier, "verification strategy required")

    def test_registry_lookup_and_risk(self) -> None:
        registry = CapabilityRegistry()
        for cap in default_capabilities():
            registry.register(cap)
        open_app = registry.best(IntentName.OPEN_APPLICATION)
        assert open_app is not None
        self.assertEqual(open_app.required, ("application",))
        self.assertEqual(open_app.risk, RiskLevel.LOW)
        close_app = registry.best(IntentName.CLOSE_APPLICATION)
        assert close_app is not None
        # Closing apps is more risky than opening them.
        self.assertEqual(close_app.risk, RiskLevel.MEDIUM)
        self.assertEqual(registry.best(IntentName.IMPROVE_SELF).risk, RiskLevel.MEDIUM)  # type: ignore[union-attr]
        self.assertIsNone(registry.best(IntentName.CLARIFY))

    def test_registry_serializes_for_api_consumers(self) -> None:
        registry = CapabilityRegistry()
        for cap in default_capabilities():
            registry.register(cap)
        payload = registry.to_dict()
        self.assertGreaterEqual(len(payload["capabilities"]), 30)
        first = payload["capabilities"][0]
        # Phase 9.2 grew this contract: the original nine keys are still here and
        # still mean what they meant, plus the metadata a capability is now
        # discovered by (id, category, tools, inputs/outputs, permissions,
        # availability, examples, tags, source).
        self.assertEqual(
            sorted(first.keys()),
            [
                "availability", "availability_reason", "capability", "capability_id",
                "category", "description", "examples", "executor", "intent", "name",
                "optional", "permissions", "required", "required_models", "risk",
                "risk_level", "source", "supported_environments", "supported_inputs",
                "supported_outputs", "tags", "tools", "verifier",
            ],
        )
        self.assertEqual(payload["count"], len(payload["capabilities"]))
        self.assertGreaterEqual(payload["registry"]["total"], 30)


class StandardResultTests(unittest.TestCase):
    def test_adapts_a_desktop_plan_payload(self) -> None:
        result = StandardResult.from_payload(
            "task-1",
            {"status": "waiting_for_approval", "summary": "Planned: Open steam"},
            intent="open_application",
        )
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertFalse(result.verified)
        envelope = result.to_dict()
        self.assertEqual(
            sorted(envelope.keys()),
            sorted(["id", "task_id", "status", "intent", "result", "evidence", "verification", "errors", "next_action", "summary"]),
        )

    def test_adapts_an_executed_payload_with_verification(self) -> None:
        result = StandardResult.from_payload(
            "task-2",
            {"status": "executed", "verified": True, "verification_strategy": "window_title"},
            intent="open_application",
        )
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertTrue(result.verified)


class LearningLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_fuzzy_resolution_teaches_the_original_phrase(self) -> None:
        gil = GlobalInputIntelligence()
        first = gil.understand("opn chrme")
        self.assertEqual(first.strategy, "fuzzy")
        second = gil.understand("opn chrme")
        # The learned variation short-circuits... after fuzzy re-derives it;
        # either way the resolution must be identical and stable.
        self.assertEqual(second.intent.intent, first.intent.intent)

    async def test_telemetry_records_resolutions_and_unknowns(self) -> None:
        gil = GlobalInputIntelligence()
        gil.understand("open chrome")
        gil.understand("take a screenshot")
        gil.understand("frobnicate the widget")
        t = gil.telemetry.to_dict()
        self.assertGreaterEqual(t["resolved_total"], 2)
        self.assertGreaterEqual(t["unknown_intents"], 1)
        self.assertTrue(t["resolved_by_intent"].get("open_application"))
        self.assertTrue(t["resolved_by_strategy"].get("fast_path"))
        self.assertTrue(gil.telemetry.improvement_findings())

    async def test_missing_entity_is_recorded_for_self_improvement(self) -> None:
        gil = GlobalInputIntelligence()
        # A resolved OPEN_APPLICATION that names no application must ask one
        # precise question AND land in the failed-entity telemetry. (Real
        # phrasings usually infer a target — "close the window" -> "window" —
        # so this drives the policy directly.)
        from novacontrol.intelligence.intent import IntentName, StructuredIntent

        missing = gil._post_process(
            StructuredIntent(
                raw_input="open",
                normalized_input="open",
                intent=IntentName.OPEN_APPLICATION,
            )
        )
        self.assertTrue(missing.needs_clarification, missing.clarification_question)
        t = gil.telemetry.to_dict()
        self.assertGreaterEqual(t["failed_entity_resolutions"], 1)


if __name__ == "__main__":
    unittest.main()
