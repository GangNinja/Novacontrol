"""Phase 35 — business opportunity and validation engine tests.

The suite deliberately separates the happy path from the adversarial one. A
high score, an operator's optimistic assumption, a model's suggestion or a stale
source must never be enough to produce ``VALIDATED``.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from novacontrol.agents.models import AgentTask, AgentTaskStatus
from novacontrol.business import (
    BusinessConflict,
    BusinessError,
    BusinessOpportunityService,
    DuplicateOpportunity,
    OpportunityNotFound,
    OpportunityStage,
    ResearchUnavailable,
    SourceRef,
)
from novacontrol.business.agent import build_business_agent
from novacontrol.business.financial import analyse_financials
from novacontrol.business.models import Opportunity, transition
from novacontrol.persistence import JsonStateStore

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]


FINANCIALS: dict[str, Any] = {
    "price": 50.0,
    "units_per_month": 100.0,
    "variable_cost_per_unit": 5.0,
    "fixed_monthly_costs": 500.0,
    "infrastructure_monthly": 100.0,
    "marketing_monthly": 200.0,
    "customer_acquisition_cost": 100.0,
    "customer_lifetime_months": 12.0,
    "initial_investment": 1000.0,
    "development_cost": 2000.0,
}
FEASIBILITY: dict[str, Any] = {
    "operational_score": 0.8,
    "legal_score": 0.9,
    "technical_complexity": 0.3,
    "execution_complexity": 0.4,
    "dependency_risks": [],
}
ASSESSMENT: dict[str, Any] = {"differentiation": 0.7}


def discover(service: BusinessOpportunityService, *, title: str | None = None) -> str:
    result = service.discover(
        title=title or f"Invoice follow-up {uuid4().hex[:8]}",
        customer_problem="Small agencies chase late invoices manually",
        proposed_solution="Automated polite invoice reminders",
        target_audience="Small agencies",
        industry="SaaS",
        category="Finance",
        source_reference="operator brief / interview notes",
    )
    return result["id"]


def add_evidence(
    service: BusinessOpportunityService,
    opportunity_id: str,
    *,
    kind: str,
    quality: str,
    claim: str,
    url: str = "",
    reference: str = "",
    supplied_by: str = "analyst",
    value: dict[str, Any] | None = None,
    retrieved_at: str = "",
) -> None:
    service.add_evidence(
        opportunity_id,
        kind=kind,
        quality=quality,
        claim=claim,
        source_title=claim[:60],
        source_url=url,
        source_reference=reference or ("" if url else "record-1"),
        supplied_by=supplied_by,
        value=value or {},
        retrieved_at=retrieved_at,
    )


def add_full_evidence(
    service: BusinessOpportunityService,
    opportunity_id: str,
    *,
    primary: bool = True,
    stale_primary: bool = False,
) -> None:
    add_evidence(
        service,
        opportunity_id,
        kind="market_size",
        quality="secondary",
        claim="Sourced market-size record",
        url="https://example.com/market",
        value={"value": 1_000_000, "year": 2026, "currency": "USD"},
    )
    add_evidence(
        service,
        opportunity_id,
        kind="demand_signal",
        quality="secondary",
        claim="Agencies report late payments weekly",
        url="https://example.com/demand",
        value={"segment": "Small agencies", "pain_point": "late payments"},
    )
    if primary:
        add_evidence(
            service,
            opportunity_id,
            kind="customer_interview",
            quality="primary",
            claim="Owner spends five hours a week chasing invoices",
            reference="interview-2026-11-01",
            supplied_by="Agency owner",
            value={
                "severity": 5,
                "frequency": "weekly",
                "willingness_to_pay": "USD 50/month",
                "segment": "Small agencies",
            },
            retrieved_at="2020-01-01T00:00:00+00:00" if stale_primary else "",
        )
    add_evidence(
        service,
        opportunity_id,
        kind="competitor",
        quality="secondary",
        claim="Acme offers invoice reminders",
        url="https://example.com/acme",
        value={"name": "Acme", "pricing": "USD 49/month", "weaknesses": ["no agency workflow"]},
    )
    add_evidence(
        service,
        opportunity_id,
        kind="technical",
        quality="secondary",
        claim="Reminder workflow is technically simple",
        url="https://example.com/technical",
    )


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = JsonStateStore(Path(self._directory.name))
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.audits: list[dict[str, Any]] = []
        self.knowledge: list[tuple[str, str]] = []

        async def knowledge(text: str, title: str) -> None:
            self.knowledge.append((text, title))

        self.service = BusinessOpportunityService(
            store=self.store,
            resource_snapshot=lambda: {
                "available_ram_gb": 16.0,
                "cpu_percent": 10.0,
                "gpu_available": False,
                "source": "test measurement",
            },
            publish=lambda type_, payload: self.events.append((type_, dict(payload))),
            audit=lambda **fields: self.audits.append(dict(fields)),
            knowledge=knowledge,
        )

    async def assess_full(
        self,
        opportunity_id: str,
        *,
        financials: dict[str, Any] | None = None,
        feasibility: dict[str, Any] | None = None,
        assessment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.service.assess(
            opportunity_id,
            financial_inputs=financials or dict(FINANCIALS),
            feasibility_inputs=feasibility or dict(FEASIBILITY),
            assessment_inputs=assessment or dict(ASSESSMENT),
        )


class FinancialTests(unittest.TestCase):
    def test_transparent_scenarios_and_break_even(self) -> None:
        analysis = analyse_financials(
            {
                "currency": "usd",
                **FINANCIALS,
            }
        )
        self.assertEqual(analysis.currency, "USD")
        scenarios = {row.name: row for row in analysis.scenarios}
        self.assertEqual(set(scenarios), {"conservative", "base", "optimistic"})
        base = scenarios["base"]
        self.assertTrue(base.calculated)
        self.assertEqual(base.monthly_revenue, 5000.0)
        self.assertEqual(base.monthly_gross_profit, 4500.0)
        self.assertEqual(base.monthly_operating_expenses, 800.0)
        self.assertEqual(base.monthly_net_profit, 3700.0)
        self.assertEqual(base.break_even_units, 11.11)
        self.assertEqual(base.cash_required, 3800.0)
        self.assertIn("monthly revenue", base.methodology[0])

    def test_missing_base_input_is_not_invented(self) -> None:
        analysis = analyse_financials({"price": 50, "units_per_month": 10})
        for scenario in analysis.scenarios:
            self.assertFalse(scenario.calculated)
            self.assertIn("variable_cost_per_unit", scenario.missing_inputs)
            self.assertIsNone(scenario.monthly_revenue)
            self.assertIsNone(scenario.monthly_net_profit)

    def test_partial_cash_requirement_stays_unavailable(self) -> None:
        analysis = analyse_financials(
            {
                **FINANCIALS,
                "initial_investment": None,
            }
        )
        base = next(row for row in analysis.scenarios if row.name == "base")
        self.assertTrue(base.calculated)
        self.assertIsNone(base.cash_required)
        self.assertIn("initial_investment", base.missing_inputs)

    def test_negative_contribution_is_visible(self) -> None:
        analysis = analyse_financials(
            {
                **FINANCIALS,
                "variable_cost_per_unit": 60,
            }
        )
        base = next(row for row in analysis.scenarios if row.name == "base")
        self.assertLess(base.contribution_margin or 0, 0)
        self.assertIsNone(base.break_even_units)


class DiscoveryAndEvidenceTests(ServiceCase):
    def test_discovery_requires_a_source_reference(self) -> None:
        with self.assertRaises(BusinessError):
            self.service.discover(
                title="Unsourced idea",
                customer_problem="Somebody might want it",
                proposed_solution="Build it",
            )

    def test_duplicate_discovery_is_refused(self) -> None:
        first = self.service.discover(
            title="Agency invoice chase",
            customer_problem="Late invoice follow-up is manual",
            proposed_solution="Automated reminders",
            industry="SaaS",
            source_reference="brief",
        )
        with self.assertRaises(DuplicateOpportunity) as raised:
            self.service.discover(
                title="Agency invoice chase",
                customer_problem="Late invoice follow-up is manual",
                proposed_solution="Automated reminders",
                industry="SaaS",
                source_reference="brief",
            )
        self.assertEqual(raised.exception.existing_id, first["id"])

    def test_measured_evidence_needs_a_source(self) -> None:
        opportunity_id = discover(self.service)
        with self.assertRaises(BusinessError):
            self.service.add_evidence(
                opportunity_id,
                kind="market_size",
                quality="secondary",
                claim="The market is huge",
            )

    def test_non_finite_confidence_is_refused(self) -> None:
        opportunity_id = discover(self.service)
        with self.assertRaises(BusinessError):
            self.service.add_evidence(
                opportunity_id,
                kind="technical",
                quality="assumption",
                claim="technical note",
                confidence=float("nan"),
            )

    def test_primary_evidence_needs_a_named_supplier(self) -> None:
        opportunity_id = discover(self.service)
        with self.assertRaises(BusinessError):
            self.service.add_evidence(
                opportunity_id,
                kind="customer_interview",
                quality="primary",
                claim="A customer said this hurts",
                source_reference="interview-1",
            )

    def test_evidence_is_persisted_with_provenance_and_source_url(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        reopened = BusinessOpportunityService(
            store=JsonStateStore(Path(self._directory.name)),
            resource_snapshot=lambda: {"available_ram_gb": 16.0, "cpu_percent": 10.0},
        )
        detail = reopened.detail(opportunity_id)
        source_urls = {
            row["source"]["url"]
            for row in detail["evidence"]
            if row.get("source") and row["source"].get("url")
        }
        self.assertIn("https://example.com/acme", source_urls)
        primary = next(row for row in detail["evidence"] if row["quality"] == "primary")
        self.assertEqual(primary["supplied_by"], "Agency owner")
        self.assertEqual(primary["source"]["source_type"], "operator_reference")
        self.assertEqual(len(detail["source_refs"]), len(source_urls) + 2)

    def test_get_unknown_opportunity_is_a_real_not_found(self) -> None:
        with self.assertRaises(OpportunityNotFound):
            self.service.get_opportunity("opp-missing")


class ValidationTests(ServiceCase):
    async def test_no_evidence_cannot_validate(self) -> None:
        opportunity_id = discover(self.service)
        result = await self.service.evaluate(opportunity_id)
        self.assertEqual(result["verdict"], "INSUFFICIENT_EVIDENCE")
        self.assertNotEqual(result["stage"], "validated")
        self.assertTrue(result["report"]["evidence_gaps"])

    async def test_assumptions_and_a_high_score_cannot_validate(self) -> None:
        opportunity_id = discover(self.service)
        for kind, claim in (
            ("market_size", "The market is definitely a billion dollars"),
            ("customer_interview", "Customers will definitely pay"),
            ("competitor", "There are probably no competitors"),
        ):
            add_evidence(
                self.service,
                opportunity_id,
                kind=kind,
                quality="assumption",
                claim=claim,
                supplied_by="operator",
            )
        result = await self.service.evaluate(opportunity_id)
        self.assertEqual(result["verdict"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["customer"]["primary_evidence_ids"], [])
        self.assertFalse(result["score"]["eligible"])

    async def test_missing_primary_customer_evidence_blocks_validation(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id, primary=False)
        await self.assess_full(opportunity_id)
        result = await self.service.evaluate(opportunity_id)
        self.assertNotEqual(result["verdict"], "VALIDATED")
        self.assertEqual(result["verdict"], "REQUIRES_FURTHER_RESEARCH")
        self.assertIn("primary customer", " ".join(result["report"]["evidence_gaps"]).lower())

    async def test_stale_primary_evidence_cannot_validate(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id, stale_primary=True)
        await self.assess_full(opportunity_id)
        result = await self.service.evaluate(opportunity_id)
        self.assertNotEqual(result["verdict"], "VALIDATED")
        self.assertEqual(result["report"]["stale_count"], 1)
        self.assertTrue(result["customer"]["has_primary"])
        self.assertFalse(result["customer"]["has_current_primary"])
        self.assertTrue(any("stale" in gap.lower() for gap in result["report"]["evidence_gaps"]))

    async def test_estimates_alone_cannot_satisfy_market_or_competitor_gates(self) -> None:
        opportunity_id = discover(self.service)
        add_evidence(
            self.service,
            opportunity_id,
            kind="market_size",
            quality="estimate",
            claim="Estimated market size",
            url="https://example.com/estimated-market",
            value={"value": 1_000_000, "year": 2026},
        )
        add_evidence(
            self.service,
            opportunity_id,
            kind="demand_signal",
            quality="estimate",
            claim="Estimated demand",
            url="https://example.com/estimated-demand",
            value={"segment": "Small agencies"},
        )
        add_evidence(
            self.service,
            opportunity_id,
            kind="customer_interview",
            quality="primary",
            claim="Owner reports the problem",
            reference="interview-estimate-gate",
            supplied_by="Agency owner",
            value={"severity": 5},
        )
        add_evidence(
            self.service,
            opportunity_id,
            kind="competitor",
            quality="estimate",
            claim="Estimated competitor",
            url="https://example.com/estimated-competitor",
            value={"name": "Estimated Acme"},
        )
        await self.assess_full(opportunity_id)
        detail = self.service.detail(opportunity_id)
        self.assertFalse(detail["competition"]["search_complete"])
        result = await self.service.evaluate(opportunity_id)
        self.assertNotEqual(result["verdict"], "VALIDATED")
        self.assertFalse(result["score"]["eligible"])

    async def test_assumption_demand_is_not_current_market_research(self) -> None:
        opportunity_id = discover(self.service)
        add_evidence(
            self.service,
            opportunity_id,
            kind="demand_signal",
            quality="assumption",
            claim="I assume agencies will pay",
            value={"segment": "Small agencies"},
        )
        await self.assess_full(opportunity_id)
        detail = self.service.detail(opportunity_id)
        self.assertEqual(detail["market"]["demand_indicators"], [])
        self.assertFalse(detail["score"]["eligible"])

    async def test_malformed_source_values_do_not_crash_assessment(self) -> None:
        opportunity_id = discover(self.service)
        add_evidence(
            self.service,
            opportunity_id,
            kind="market_size",
            quality="secondary",
            claim="Sourced market size with a malformed year",
            url="https://example.com/malformed-year",
            value={"value": 1000, "year": "not-a-year"},
        )
        result = await self.service.assess(opportunity_id)
        self.assertEqual(result["market"]["market_size"]["value"], 1000)
        self.assertIsNone(result["market"]["market_size"]["year"])

    async def test_all_missing_feasibility_does_not_divide_by_zero(self) -> None:
        service = BusinessOpportunityService(
            store=JsonStateStore(Path(self._directory.name) / "empty-signals"),
            resource_snapshot=lambda: {},
        )
        opportunity_id = discover(service)
        result = await service.assess(opportunity_id)
        self.assertFalse(result["feasibility"]["complete"])
        self.assertIsNone(result["feasibility"]["overall_score"])
        # With every feasibility dimension missing, risk must stay missing rather
        # than a neutral score, and a target audience alone must not become a
        # researched-segment score.
        self.assertIn("risk_exposure", result["score"]["missing"])
        self.assertEqual(result["score"]["weighted_score"], 0.0)
        self.assertFalse(result["score"]["eligible"])

    async def test_malformed_ram_requirement_is_reported_not_crashed(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        result = await self.assess_full(
            opportunity_id,
            feasibility={**FEASIBILITY, "technical_required_ram_gb": "lots"},
        )
        technical = next(
            row for row in result["feasibility"]["dimensions"] if row["name"] == "technical"
        )
        self.assertIsNone(technical["score"])
        self.assertIn("not a number", technical["missing_reason"])

    async def test_unreadable_live_memory_is_missing_not_a_false_blocker(self) -> None:
        service = BusinessOpportunityService(
            store=JsonStateStore(Path(self._directory.name) / "unreadable-memory"),
            resource_snapshot=lambda: {"available_ram_gb": "unknown"},
        )
        opportunity_id = discover(service)
        add_full_evidence(service, opportunity_id)
        result = await service.assess(
            opportunity_id,
            financial_inputs=dict(FINANCIALS),
            feasibility_inputs={**FEASIBILITY, "technical_required_ram_gb": 8},
            assessment_inputs=dict(ASSESSMENT),
        )
        technical = next(
            row for row in result["feasibility"]["dimensions"] if row["name"] == "technical"
        )
        self.assertIsNone(technical["score"])
        self.assertIn("unreadable memory measurement", " ".join(result["feasibility"]["gaps"]))
        self.assertFalse(
            any("exceeds available" in blocker for blocker in result["feasibility"]["blockers"])
        )

    async def test_gpu_only_requirement_is_not_treated_as_no_requirement(self) -> None:
        opportunity_id = discover(self.service)
        result = await self.service.assess(
            opportunity_id,
            financial_inputs=dict(FINANCIALS),
            feasibility_inputs={"technical_requires_gpu": True},
        )
        technical = next(
            row for row in result["feasibility"]["dimensions"] if row["name"] == "technical"
        )
        self.assertIsNotNone(technical["score"])
        self.assertTrue(
            any("GPU" in blocker for blocker in result["feasibility"]["blockers"])
        )

    async def test_unavailable_market_data_is_reported_not_invented(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        await self.assess_full(opportunity_id)
        detail = self.service.detail(opportunity_id)
        self.assertEqual(detail["market"]["market_size"]["status"], "measured")
        self.assertEqual(detail["market"]["market_size"]["value"], 1_000_000)

        no_market = discover(self.service)
        add_evidence(
            self.service,
            no_market,
            kind="technical",
            quality="secondary",
            claim="Technically simple",
            url="https://example.com/technical-2",
        )
        await self.assess_full(no_market)
        detail = self.service.detail(no_market)
        self.assertEqual(detail["market"]["market_size"]["status"], "unavailable")
        self.assertIsNone(detail["market"]["market_size"]["value"])

    async def test_complete_evidence_can_validate(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        await self.assess_full(opportunity_id)
        result = await self.service.evaluate(opportunity_id)
        self.assertEqual(result["verdict"], "VALIDATED")
        self.assertEqual(result["stage"], "validated")
        self.assertEqual(len(result["report"]["sections"]), 15)
        self.assertEqual(result["score"]["evidence_coverage"], 1.0)
        self.assertTrue(self.knowledge and "Business validation report" in self.knowledge[0][0])

    async def test_non_finite_financial_input_is_refused(self) -> None:
        opportunity_id = discover(self.service)
        with self.assertRaises(ValueError):
            await self.service.assess(
                opportunity_id,
                financial_inputs={**FINANCIALS, "price": float("nan")},
            )

    async def test_blank_reassessment_reuses_stored_assumptions(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        first = await self.assess_full(opportunity_id)
        self.assertTrue(first["financials"]["scenarios"][1]["calculated"])
        second = await self.service.assess(opportunity_id)
        self.assertTrue(second["financials"]["scenarios"][1]["calculated"])
        self.assertTrue(second["feasibility"]["complete"])
        self.assertIsNotNone(second["score"])

    async def test_partial_financial_update_keeps_other_stored_assumptions(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        await self.assess_full(opportunity_id)
        updated = await self.service.assess(
            opportunity_id, financial_inputs={"price": 60.0}
        )
        base = next(
            row for row in updated["financials"]["scenarios"] if row["name"] == "base"
        )
        self.assertTrue(base["calculated"])
        self.assertEqual(base["assumptions"]["price"], 60.0)
        self.assertEqual(base["assumptions"]["units_per_month"], FINANCIALS["units_per_month"])
        self.assertEqual(updated["financials"]["scenarios"][1]["monthly_revenue"], 6000.0)

    async def test_negative_unit_economics_are_not_viable(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        result = await self.assess_full(
            opportunity_id,
            financials={**FINANCIALS, "variable_cost_per_unit": 70},
        )
        self.assertLess(result["financials"]["scenarios"][1]["contribution_margin"], 0)
        final = await self.service.evaluate(opportunity_id)
        self.assertEqual(final["verdict"], "NOT_VIABLE")

    async def test_legal_blocker_is_not_viable(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        await self.assess_full(
            opportunity_id,
            feasibility={**FEASIBILITY, "legal_blockers": ["regulated advice licence required"]},
        )
        final = await self.service.evaluate(opportunity_id)
        self.assertEqual(final["verdict"], "NOT_VIABLE")
        self.assertTrue(any("legal" in blocker.lower() for blocker in final["feasibility"]["blockers"]))

    async def test_verdict_cannot_be_assigned_directly(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        current = self.service.get_opportunity(opportunity_id)
        with self.assertRaises(ValueError):
            transition(current, OpportunityStage.VALIDATED, reason="agent says so", actor="agent")


class LifecycleTests(ServiceCase):
    async def test_pause_resume_and_history_are_recorded(self) -> None:
        opportunity_id = discover(self.service)
        paused = self.service.pause(opportunity_id, reason="waiting for interviews")
        self.assertEqual(paused["stage"], "paused")
        self.assertEqual(paused["metadata"]["paused_from"], "discovered")
        resumed = self.service.resume(opportunity_id, reason="interviews booked")
        self.assertEqual(resumed["stage"], "discovered")
        self.assertEqual(resumed["history"][-1]["to_stage"], "discovered")

    async def test_reject_requires_a_reason_and_archive_is_allowed(self) -> None:
        opportunity_id = discover(self.service)
        with self.assertRaises(BusinessError):
            self.service.reject(opportunity_id, reason="")
        rejected = self.service.reject(opportunity_id, reason="problem is too small")
        self.assertEqual(rejected["stage"], "rejected")
        archived = self.service.archive(opportunity_id, reason="superseded")
        self.assertEqual(archived["stage"], "archived")

    async def test_terminal_validated_record_is_immutable(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        await self.assess_full(opportunity_id)
        await self.service.evaluate(opportunity_id)
        with self.assertRaises(BusinessConflict):
            await self.service.evaluate(opportunity_id)
        with self.assertRaises(BusinessConflict):
            self.service.pause(opportunity_id, reason="should be refused")
        with self.assertRaises(BusinessConflict):
            self.service.add_evidence(
                opportunity_id,
                kind="other",
                quality="assumption",
                claim="post-hoc",
            )

    async def test_resume_requires_an_actually_paused_record(self) -> None:
        opportunity_id = discover(self.service)
        current = self.service.get_opportunity(opportunity_id)
        self.service._opportunities[opportunity_id] = replace(
            current, metadata={**dict(current.metadata), "paused_from": "discovered"}
        )
        with self.assertRaises(BusinessConflict):
            self.service.resume(opportunity_id)

    async def test_queued_assessment_cannot_mutate_a_terminal_record(self) -> None:
        opportunity_id = discover(self.service)
        lock = self.service._locks.setdefault(opportunity_id, asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(self.service.assess(opportunity_id))
        # Let the task pass its pre-lock check and block on the opportunity lock.
        await asyncio.sleep(0.01)
        current = self.service.get_opportunity(opportunity_id)
        self.service._opportunities[opportunity_id] = replace(
            current, stage=OpportunityStage.VALIDATED
        )
        lock.release()
        with self.assertRaises(BusinessConflict):
            await task

    async def test_interrupted_validation_stage_can_finish(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        await self.assess_full(opportunity_id)
        current = self.service.get_opportunity(opportunity_id)
        self.service._opportunities[opportunity_id] = replace(
            current, stage=OpportunityStage.VALIDATION
        )
        result = await self.service.evaluate(opportunity_id)
        self.assertEqual(result["stage"], "validated")
        self.assertEqual(result["verdict"], "VALIDATED")


class ResearchTests(ServiceCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.calls: list[tuple[str, int]] = []

        async def fake_research(query: str, limit: int) -> list[dict[str, Any]]:
            self.calls.append((query, limit))
            return [
                {
                    "title": "Sourced market article",
                    "url": "https://example.com/research-article",
                    "snippet": "A real search result about invoice reminders.",
                    "retrieved_at": datetime.now(UTC).isoformat(),
                }
            ]

        self.service._research_callable = fake_research

    async def test_discovery_scan_returns_sourced_candidates_without_inventing_briefs(self) -> None:
        before = self.service.status()["opportunities"]
        result = await self.service.scan_signals(
            query="agencies complaining about invoice follow-up",
            signal_type="customer_complaint",
            limit=6,
        )
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["customer_problem"], "")
        self.assertEqual(candidate["proposed_solution"], "")
        self.assertTrue(candidate["source_url"].startswith("https://"))
        self.assertEqual(candidate["signal_type"], "customer_complaint")
        self.assertEqual(self.service.status()["opportunities"], before)
        self.assertIn("business.discovery.scan.completed", {row[0] for row in self.events})

    async def test_discovery_scan_budget_and_provider_failure_are_reported(self) -> None:
        self.service.max_scan_runs = 1
        await self.service.scan_signals(query="first scan")
        with self.assertRaises(BusinessConflict):
            await self.service.scan_signals(query="second scan")
        self.service._research_callable = None
        with self.assertRaises(ResearchUnavailable):
            await self.service.scan_signals(query="no provider")

    async def test_research_records_unclassified_sourced_reference(self) -> None:
        opportunity_id = discover(self.service)
        result = await self.service.research(opportunity_id)
        self.assertEqual(result["sources_added"], 1)
        self.assertEqual(result["opportunity"]["stage"], "evidence_collection")
        self.assertEqual(result["opportunity"]["evidence"][-1]["kind"], "other")
        self.assertEqual(result["opportunity"]["evidence"][-1]["quality"], "secondary")
        self.assertTrue(result["opportunity"]["evidence"][-1]["source"]["url"].startswith("https://"))

    async def test_research_provider_failure_does_not_create_evidence(self) -> None:
        opportunity_id = discover(self.service)

        async def failing(query: str, limit: int) -> list[dict[str, Any]]:
            raise RuntimeError("search offline")

        self.service._research_callable = failing
        result = await self.service.research(opportunity_id)
        self.assertEqual(result["sources_added"], 0)
        self.assertIn("unavailable", result["warnings"][0].lower())
        self.assertEqual(self.service.detail(opportunity_id)["stage"], "researching")

    async def test_research_budget_is_enforced(self) -> None:
        opportunity_id = discover(self.service)
        self.service.max_research_runs = 1
        await self.service.research(opportunity_id)
        with self.assertRaises(BusinessConflict):
            await self.service.research(opportunity_id)

    async def test_cancelled_research_releases_the_single_run_guard(self) -> None:
        opportunity_id = discover(self.service)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(query: str, limit: int) -> list[dict[str, Any]]:
            started.set()
            await release.wait()
            return []

        self.service._research_callable = slow
        task = asyncio.create_task(self.service.research(opportunity_id))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        result = await self.service.research(opportunity_id)
        self.assertEqual(result["sources_added"], 0)
        self.assertNotIn(opportunity_id, self.service._active)

    async def test_duplicate_concurrent_research_is_refused(self) -> None:
        opportunity_id = discover(self.service)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(query: str, limit: int) -> list[dict[str, Any]]:
            started.set()
            await release.wait()
            return []

        self.service._research_callable = slow
        first = asyncio.create_task(self.service.research(opportunity_id))
        await asyncio.wait_for(started.wait(), timeout=2)
        second = await asyncio.gather(
            self.service.research(opportunity_id), return_exceptions=True
        )
        self.assertIsInstance(second[0], BusinessConflict)
        release.set()
        await first
        self.assertNotIn(opportunity_id, self.service._active)


class ExperimentTests(ServiceCase):
    async def test_experiments_require_approval_before_outcome(self) -> None:
        opportunity_id = discover(self.service)
        planned = await self.service.plan_experiments(opportunity_id)
        self.assertEqual(planned["stage"], "experiment_planning")
        self.assertTrue(planned["experiments"])
        external = next(
            row for row in planned["experiments"] if row["requires_approval"] and row["status"] == "proposed"
        )
        with self.assertRaises(BusinessConflict):
            self.service.record_experiment_outcome(
                opportunity_id, external["id"], outcome="success"
            )
        approved = self.service.approve_experiment(
            opportunity_id, external["id"], approved=True, note="operator approved the test"
        )
        approved_row = next(row for row in approved["experiments"] if row["id"] == external["id"])
        self.assertEqual(approved_row["status"], "approved")
        self.assertFalse(approved_row["external_action_executed"])
        completed = self.service.record_experiment_outcome(
            opportunity_id,
            external["id"],
            outcome="success",
            result={"measured": "5% conversion"},
        )
        completed_row = next(row for row in completed["experiments"] if row["id"] == external["id"])
        self.assertEqual(completed_row["status"], "completed")
        self.assertIn("business.experiment.approval.granted", {row[0] for row in self.events})
        self.assertIn("business.experiment.outcome.recorded", {row[0] for row in self.events})

    async def test_estimated_demand_still_requires_a_demand_experiment(self) -> None:
        opportunity_id = discover(self.service)
        add_evidence(
            self.service,
            opportunity_id,
            kind="demand_signal",
            quality="estimate",
            claim="Estimated demand for invoice reminders",
            url="https://example.com/estimated-demand-plan",
            value={"segment": "Small agencies"},
        )
        planned = await self.service.plan_experiments(opportunity_id)
        titles = {row["title"] for row in planned["experiments"]}
        self.assertIn("Landing-page demand test", titles)

    async def test_denied_experiment_cannot_record_outcome(self) -> None:
        opportunity_id = discover(self.service)
        planned = await self.service.plan_experiments(opportunity_id)
        external = next(row for row in planned["experiments"] if row["requires_approval"])
        denied = self.service.approve_experiment(
            opportunity_id, external["id"], approved=False, note="not yet"
        )
        row = next(item for item in denied["experiments"] if item["id"] == external["id"])
        self.assertEqual(row["status"], "rejected")
        with self.assertRaises(BusinessConflict):
            self.service.record_experiment_outcome(
                opportunity_id, external["id"], outcome="success"
            )


class AgentIntegrationTests(ServiceCase):
    async def test_registered_business_agent_is_advisory_only(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        await self.assess_full(opportunity_id)
        await self.service.evaluate(opportunity_id)
        before = self.service.detail(opportunity_id)
        agent = build_business_agent(self.service)
        response = await agent.handle_task(
            AgentTask("Summarise the business evidence", metadata={"opportunity_id": opportunity_id})
        )
        self.assertEqual(response.status, AgentTaskStatus.COMPLETED)
        self.assertTrue(response.metadata["advisory_only"])
        self.assertFalse(response.metadata["external_action_executed"])
        self.assertIn("VALIDATED", response.content)
        after = self.service.detail(opportunity_id)
        self.assertEqual(before["verdict"], after["verdict"])
        self.assertEqual(before["stage"], after["stage"])

    async def test_agent_missing_opportunity_fails_without_inventing_one(self) -> None:
        agent = build_business_agent(self.service)
        response = await agent.handle_task(
            AgentTask("Inspect missing", metadata={"opportunity_id": "opp-missing"})
        )
        self.assertEqual(response.status, AgentTaskStatus.FAILED)
        self.assertIn("not found", response.content)


class PersistenceAndAuditTests(ServiceCase):
    async def test_events_audit_and_knowledge_ingestion_are_recorded(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        await self.assess_full(opportunity_id)
        await self.service.evaluate(opportunity_id)
        event_types = {row[0] for row in self.events}
        self.assertTrue(
            {
                "business.opportunity.discovered",
                "business.evidence.added",
                "business.assessment.completed",
                "business.score.calculated",
                "business.report.generated",
                "business.validation.completed",
            }
            <= event_types
        )
        self.assertTrue(self.audits)
        self.assertTrue(any(row.get("intent") == "business.validation.completed" for row in self.audits))
        self.assertEqual(len(self.knowledge), 1)
        self.assertIn("Business validation report", self.knowledge[0][0])
        self.assertIn("VALIDATED", self.knowledge[0][0])

    async def test_list_filters_and_compare(self) -> None:
        first = discover(self.service, title="Invoice follow-up Alpha")
        second = discover(self.service, title="Invoice follow-up Beta")
        listing = self.service.list_opportunities(query="follow-up", industry="SaaS")
        self.assertEqual(listing["total"], 2)
        self.assertEqual(self.service.list_opportunities(stage="paused")["total"], 0)
        compared = self.service.compare((first, second))
        self.assertEqual(len(compared["opportunities"]), 2)
        with self.assertRaises(BusinessError):
            self.service.compare((first,))

    async def test_new_evidence_invalidates_old_analysis_and_keeps_report_version(self) -> None:
        opportunity_id = discover(self.service)
        first = await self.service.evaluate(opportunity_id)
        self.assertEqual(first["verdict"], "INSUFFICIENT_EVIDENCE")
        first_version = first["report"]["version"]
        add_evidence(
            self.service,
            opportunity_id,
            kind="technical",
            quality="secondary",
            claim="A sourced technical note",
            url="https://example.com/versioned-technical",
        )
        changed = self.service.detail(opportunity_id)
        self.assertIsNone(changed["score"])
        self.assertIsNone(changed["market"])
        self.assertIsNone(changed["report"])
        second = await self.service.evaluate(opportunity_id)
        self.assertEqual(second["report"]["version"], first_version + 1)

    def test_corrupt_rows_are_skipped_without_hiding_valid_rows(self) -> None:
        good = discover(self.service)
        payload = self.store.read("business_opportunities")
        payload["opportunities"].append({"id": "broken"})
        self.store.write("business_opportunities", payload)
        reopened = BusinessOpportunityService(store=self.store)
        self.assertEqual(reopened.detail(good)["id"], good)
        self.assertTrue(reopened.status()["load_errors"])

    def test_missing_source_timestamp_is_a_load_error_not_current_provenance(self) -> None:
        opportunity_id = discover(self.service)
        add_full_evidence(self.service, opportunity_id)
        payload = self.store.read("business_opportunities")
        row = payload["opportunities"][0]
        row["source_refs"][0].pop("retrieved_at", None)
        self.store.write("business_opportunities", payload)
        reopened = BusinessOpportunityService(store=self.store)
        self.assertTrue(reopened.status()["load_errors"])
        with self.assertRaises(OpportunityNotFound):
            reopened.get_opportunity(opportunity_id)


class FrontendContractTests(unittest.TestCase):
    def test_business_workspace_and_renderer_are_wired(self) -> None:
        root = Path("src/novacontrol/web/static")
        html = (root / "index.html").read_text(encoding="utf-8")
        renderer = (root / "js" / "render-business.js").read_text(encoding="utf-8")
        dispatch = (root / "js" / "render-utils.js").read_text(encoding="utf-8")
        app = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="businessPanel"', html)
        self.assertIn('data-panel="businessPanel"', html)
        self.assertIn("render-business.js", html)
        self.assertIn('type === "business"', dispatch)
        self.assertIn("function renderBusinessOpportunities(", renderer)
        self.assertIn("function setupBusinessOpportunities(", app)
        self.assertIn("/business/opportunities", app)
        self.assertIn("businessScanButton", html)
        self.assertIn("/business/discovery/scan", app)
        self.assertIn("renderBusinessScan(", renderer)


@unittest.skipIf(TestClient is None, "FastAPI TestClient is unavailable")
class ApiTests(unittest.TestCase):
    def test_business_routes_refuse_missing_evidence_and_return_real_state(self) -> None:
        from novacontrol.api.app import create_app

        app = create_app()
        unique = uuid4().hex[:10]

        def cleanup_records() -> None:
            # create_app() owns the project's runtime data directory. Remove
            # only this test's row, so the developer's real opportunities and
            # every other test's rows stay untouched.
            store = JsonStateStore(Path("data"))
            payload = store.read("business_opportunities")
            rows = payload.get("opportunities")
            if not isinstance(rows, list):
                return
            kept = [
                row
                for row in rows
                if not str(row.get("title", "")).startswith("Phase 35 API opportunity")
            ]
            if len(kept) != len(rows):
                store.write("business_opportunities", {**payload, "opportunities": kept})

        self.addCleanup(cleanup_records)
        with TestClient(app) as client:
            status = client.get("/business/status")
            self.assertEqual(status.status_code, 200)
            self.assertIn("boundaries", status.json())
            self.assertFalse(status.json()["boundaries"]["external_actions_executed"])
            application_status = client.get("/status")
            self.assertEqual(application_status.status_code, 200)
            self.assertIn("business_opportunities", application_status.json()["app"])

            created = client.post(
                "/business/opportunities",
                json={
                    "title": f"Phase 35 API opportunity {unique}",
                    "customer_problem": "Late invoice follow-up is manual",
                    "proposed_solution": "Automated reminders",
                    "target_audience": "Small agencies",
                    "industry": "SaaS",
                    "source_reference": "operator brief for API test",
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            opportunity_id = created.json()["id"]
            again = client.post(
                "/business/opportunities",
                json={
                    "title": f"Phase 35 API opportunity {unique}",
                    "customer_problem": "Late invoice follow-up is manual",
                    "proposed_solution": "Automated reminders",
                    "industry": "SaaS",
                    "source_reference": "duplicate brief",
                },
            )
            self.assertEqual(again.status_code, 409)
            self.assertEqual(again.json()["detail"]["existing_id"], opportunity_id)

            missing_source = client.post(
                f"/business/opportunities/{opportunity_id}/evidence",
                json={"kind": "market_size", "quality": "secondary", "claim": "Huge market"},
            )
            self.assertEqual(missing_source.status_code, 422)
            self.assertEqual(client.get("/business/opportunities/opp-missing").status_code, 404)
            # Research is exercised against the injected provider in the service
            # tests; this API test deliberately makes no outbound request.
            evaluated = client.post(f"/business/opportunities/{opportunity_id}/evaluate")
            self.assertEqual(evaluated.status_code, 200, evaluated.text)
            self.assertEqual(evaluated.json()["verdict"], "INSUFFICIENT_EVIDENCE")
            invalid_approval = client.post(
                f"/business/opportunities/{opportunity_id}/experiments/exp-missing/approval",
                json={"approved": "yes"},
            )
            self.assertEqual(invalid_approval.status_code, 422)
            detail = client.get(f"/business/opportunities/{opportunity_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("history", detail.json())

            rejected = client.post(
                f"/business/opportunities/{opportunity_id}/reject",
                json={"reason": "test close-out"},
            )
            self.assertEqual(rejected.status_code, 200)
            self.assertEqual(rejected.json()["stage"], "rejected")


__all__ = [
    "AgentIntegrationTests",
    "ApiTests",
    "DiscoveryAndEvidenceTests",
    "ExperimentTests",
    "FinancialTests",
    "FrontendContractTests",
    "LifecycleTests",
    "PersistenceAndAuditTests",
    "ResearchTests",
    "ValidationTests",
]
