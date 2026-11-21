"""PI-23 forecast-optimization evidence consumed by the PI-12 decision path.

These tests pin the integration boundary: the latest selection, reliability and
fallback records may be attached to an AT-03 proposal as read-only evidence, and
they may never select the action, change the notional, satisfy the NM-08 risk
gate, authorise NM-10 execution, bypass the runtime mode/lease or blend a
foreign scope's evidence into a proposal.
"""

from __future__ import annotations

import unittest
from typing import Any

from novacontrol.novamarket.decision.forecast_optimization import (
    ForecastOptimizationEvidence,
    forecast_optimization_evidence,
)
from novacontrol.novamarket.decision.models import DecisionErrorCode, DecisionStateError
from novacontrol.novamarket.decision.pipeline import AutonomyDecisionPipeline
from test_novamarket_pi12 import NOW, Harness

SELECTION_ID = "fisel_pi23_evidence"
RELIABILITY_ID = "fira_pi23_evidence"
FALLBACK_ID = "fifb_pi23_evidence"


def selection(
    *, instrument_id: str, horizon: str = "5d", outcome: str = "selected", **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "selection_id": SELECTION_ID,
        "created_at": NOW.isoformat(),
        "instrument_id": instrument_id,
        "horizon": horizon,
        "outcome": outcome,
        "selected_model_id": "arima" if outcome == "selected" else "",
        "selected_version": "pi05-stat-1.0",
        "sample_count": 120,
        "selection_policy_version": "pi23.selection-evidence.1.0",
        "test_used_for_selection": False,
        "calibration_status": "calibrated",
        "rationale": ["best validation MAE among eligible models"],
    }
    body.update(overrides)
    return body


def reliability(
    *, instrument_id: str, horizon: str = "5d", model_id: str = "arima", **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reliability_assessment_id": RELIABILITY_ID,
        "instrument_id": instrument_id,
        "horizon": horizon,
        "policy_version": "pi23.reliability.1.0",
        "assessments": [
            {
                "reliability_id": "fir_arima",
                "model_id": "arima",
                "classification": "high",
                "health": "healthy",
                "drift_detected": False,
                "calibration_status": "calibrated",
                "sample_count": 120,
            },
            {
                "reliability_id": "fir_ema",
                "model_id": "ema",
                "classification": "low",
                "health": "degraded",
                "drift_detected": True,
                "calibration_status": "uncalibrated",
                "sample_count": 40,
            },
        ],
    }
    if model_id:
        body["assessments"] = [
            item for item in body["assessments"] if item["model_id"] == model_id
        ]
    body.update(overrides)
    return body


def fallback(
    *, instrument_id: str, horizon: str = "5d", outcome: str = "selected", **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "fallback_id": FALLBACK_ID,
        "created_at": NOW.isoformat(),
        "instrument_id": instrument_id,
        "horizon": horizon,
        "outcome": outcome,
        "abstained": outcome == "abstained",
        "preferred_model_id": "arima",
        "selected": {
            "model_id": "arima",
            "forecast_id": "fc_pi23_evidence",
            "reason": "the preferred model has a current forecast",
        },
        "reasons": ["the preferred model passed suitability and availability checks"],
    }
    body.update(overrides)
    return body


class Reader:
    """A counted reader that sanitises PI-23 records per signal."""

    def __init__(self, build: Any) -> None:
        self.calls: list[str] = []
        self._build = build

    def __call__(self, signal: Any) -> dict[str, Any] | None:
        self.calls.append(signal.signal_id)
        body = self._build(signal)
        if body is None:
            return None
        return forecast_optimization_evidence(
            body[0],
            body[1],
            body[2],
            instrument_id=signal.instrument_id,
            horizon=signal.horizon,
        )


def records_for(signal: Any, **overrides: Any) -> tuple[Any, Any, Any]:
    return (
        selection(
            instrument_id=signal.instrument_id,
            horizon=signal.horizon,
            **overrides,
        ),
        reliability(instrument_id=signal.instrument_id, horizon=signal.horizon),
        fallback(instrument_id=signal.instrument_id, horizon=signal.horizon),
    )


class Pi23DecisionEvidenceTests(Harness):
    def install_reader(self, reader: Any) -> None:
        self.pipeline.close()
        self.pipeline = AutonomyDecisionPipeline(
            self.runtime,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            forecasts=self.source,
            clock=self.clock,
            open_orders=lambda portfolio_id, instrument_id: [],
            forecast_optimization_evidence=reader,
        )

    def proposals(self) -> list[dict[str, Any]]:
        return self.pipeline.proposals()["proposals"]

    def signals(self) -> list[dict[str, Any]]:
        return self.pipeline.signals()["signals"]

    async def test_selection_reliability_and_fallback_reach_the_proposal(self) -> None:
        reader = Reader(records_for)
        self.install_reader(reader)
        await self.run_once()
        signal = self.signals()[0]
        proposal = self.proposals()[0]
        evidence = proposal["forecast_optimization_evidence"]
        self.assertIsInstance(evidence, dict)
        self.assertEqual(evidence["phase"], "PI-23")
        self.assertEqual(evidence["source"], "PI-23 forecast optimization evidence (read-only)")
        self.assertEqual(evidence["available"], ["selection", "reliability", "fallback"])
        self.assertEqual(evidence["selection"]["outcome"], "selected")
        self.assertEqual(evidence["selection"]["selected_model_id"], "arima")
        self.assertEqual(evidence["reliability"]["classification"], "high")
        self.assertEqual(evidence["fallback"]["selected_forecast_id"], "fc_pi23_evidence")
        # Authority stays with the existing layers, re-asserted on the evidence.
        self.assertIs(evidence["read_only"], True)
        self.assertIs(evidence["advisory_only"], True)
        self.assertIs(evidence["affects_action"], False)
        self.assertIs(evidence["order_submitted"], False)
        self.assertEqual(evidence["risk_authority"], "NM-08")
        self.assertEqual(evidence["execution_authority"], "NM-10")
        self.assertIs(evidence["requires_nm08_reassessment"], True)
        self.assertIs(evidence["requires_operator_permission"], True)
        expected_action = {
            "buy": "add_position",
            "sell": "reduce_position",
            "exit": "remove_position",
        }[signal["direction"].lower()]
        self.assertEqual(proposal["action"], expected_action)
        self.assertFalse(proposal["execution_allowed"])
        self.assertFalse(proposal["orders_possible"])
        self.assertEqual(reader.calls, [signal["signal_id"]])

    async def test_a_contradictory_selection_does_not_change_the_action(self) -> None:
        # The selection says "no eligible model" while the signal is actionable.
        # Evidence is not a vote: the action, notional and reason still come from
        # the signal, and the risk outcome still comes from NM-08.
        reader = Reader(
            lambda signal: records_for(
                signal, outcome="insufficient_evidence", selected_model_id=""
            )
        )
        self.install_reader(reader)
        await self.run_once()
        signal = self.signals()[0]
        proposal = self.proposals()[0]
        expected_action = {
            "buy": "add_position",
            "sell": "reduce_position",
            "exit": "remove_position",
        }[signal["direction"].lower()]
        self.assertEqual(proposal["action"], expected_action)
        self.assertEqual(proposal["reason"], signal["rationale"])
        self.assertNotEqual(proposal["status"], "created")
        self.assertEqual(
            proposal["forecast_optimization_evidence"]["selection"]["outcome"],
            "insufficient_evidence",
        )

    async def test_a_foreign_scope_record_is_not_attached(self) -> None:
        reader = Reader(
            lambda signal: (
                selection(instrument_id="XNSE:OTHER"),
                None,
                None,
            )
        )
        self.install_reader(reader)
        await self.run_once()
        self.assertIsNone(self.proposals()[0]["forecast_optimization_evidence"])

    async def test_reader_failure_is_recorded_and_never_blocks_the_decision(
        self,
    ) -> None:
        def boom(signal: Any) -> dict[str, Any] | None:
            raise RuntimeError("PI-23 store unavailable")

        self.install_reader(boom)
        await self.run_once()
        proposal = self.proposals()[0]
        self.assertIsNone(proposal["forecast_optimization_evidence"])
        self.assertIsNotNone(proposal["action"])
        status = self.pipeline.status()
        self.assertIn("RuntimeError", status["forecast_optimization_evidence"]["last_error"])
        self.assertIs(status["forecast_optimization_evidence"]["read_only"], True)
        self.assertIs(status["forecast_optimization_evidence"]["affects_action"], False)

    async def test_a_disabled_runtime_never_consumes_evidence(self) -> None:
        reader = Reader(records_for)
        self.install_reader(reader)
        self.runtime.stop(reason="kill switch")
        with self.assertRaises(DecisionStateError) as raised:
            await self.run_once()
        self.assertEqual(raised.exception.code, DecisionErrorCode.RUNTIME_DISABLED.value)
        self.assertEqual(reader.calls, [])
        self.assertEqual(self.proposals(), [])

    async def test_evidence_is_metadata_only_and_reads_back_from_the_store(self) -> None:
        reader = Reader(records_for)
        self.install_reader(reader)
        await self.run_once()
        proposal_id = self.proposals()[0]["proposal_id"]
        stored = self.pipeline.store.proposal(proposal_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(
            stored["forecast_optimization_evidence"]["selection"]["selection_id"],
            SELECTION_ID,
        )


class SanitizerTests(unittest.TestCase):
    def test_forced_flags_cannot_be_weakened_by_a_pi23_record(self) -> None:
        body = forecast_optimization_evidence(
            selection(
                instrument_id="XNSE:ALPHA",
                read_only=False,
                advisory_only=False,
                affects_action=True,
                order_submitted=True,
                risk_authority="PI-23",
                execution_authority="PI-23",
                requires_nm08_reassessment=False,
                secret="must not leak",
            ),
            None,
            None,
            instrument_id="XNSE:ALPHA",
            horizon="5d",
        )
        self.assertIsNotNone(body)
        assert body is not None
        self.assertIs(body["read_only"], True)
        self.assertIs(body["advisory_only"], True)
        self.assertIs(body["affects_action"], False)
        self.assertIs(body["order_submitted"], False)
        self.assertEqual(body["risk_authority"], "NM-08")
        self.assertEqual(body["execution_authority"], "NM-10")
        self.assertIs(body["requires_nm08_reassessment"], True)
        self.assertNotIn("secret", body)

    def test_a_different_horizon_is_not_evidence_for_this_signal(self) -> None:
        self.assertIsNone(
            forecast_optimization_evidence(
                selection(instrument_id="XNSE:ALPHA", horizon="5d"),
                None,
                None,
                instrument_id="XNSE:ALPHA",
                horizon="1h",
            )
        )

    def test_no_usable_record_returns_none(self) -> None:
        self.assertIsNone(
            forecast_optimization_evidence(None, None, None, instrument_id="XNSE:ALPHA")
        )
        self.assertIsNone(
            forecast_optimization_evidence(
                None, None, None, instrument_id=""
            )
        )

    def test_reliability_is_the_selected_models_assessment_only(self) -> None:
        body = forecast_optimization_evidence(
            selection(instrument_id="XNSE:ALPHA", horizon="5d"),
            reliability(instrument_id="XNSE:ALPHA", horizon="5d"),
            None,
            instrument_id="XNSE:ALPHA",
            horizon="5d",
        )
        assert body is not None
        self.assertEqual(body["reliability"]["model_id"], "arima")
        self.assertEqual(body["reliability"]["classification"], "high")
        self.assertIsNone(body["reliability"]["numeric_score"])

    def test_reliability_for_another_model_is_not_attached(self) -> None:
        body = forecast_optimization_evidence(
            selection(instrument_id="XNSE:ALPHA", horizon="5d", selected_model_id="holt"),
            reliability(instrument_id="XNSE:ALPHA", horizon="5d", model_id="arima"),
            None,
            instrument_id="XNSE:ALPHA",
            horizon="5d",
        )
        assert body is not None
        self.assertNotIn("reliability", body)
        self.assertEqual(body["available"], ["selection"])

    def test_long_reason_lists_are_bounded(self) -> None:
        body = forecast_optimization_evidence(
            None,
            None,
            fallback(
                instrument_id="XNSE:ALPHA",
                horizon="5d",
                reasons=[f"reason {index}" for index in range(40)],
            ),
            instrument_id="XNSE:ALPHA",
            horizon="5d",
        )
        assert body is not None
        self.assertEqual(len(body["fallback"]["reasons"]), 6)


class StubService:
    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self.records = records
        self.calls: list[tuple[str, str, str]] = []

    def history(
        self, *, kind: str, instrument_id: str, horizon: str, limit: int
    ) -> dict[str, Any]:
        self.calls.append((kind, instrument_id, horizon))
        record = self.records.get(kind)
        return {"ok": True, "records": [record] if record else [], "total": 1 if record else 0}


class ReaderTests(unittest.TestCase):
    def test_reader_reads_the_latest_record_of_each_kind_for_the_scope(self) -> None:
        service = StubService(
            {
                "selection": selection(instrument_id="XNSE:ALPHA", horizon="5d"),
                "reliability": reliability(instrument_id="XNSE:ALPHA", horizon="5d"),
                "fallback": fallback(instrument_id="XNSE:ALPHA", horizon="5d"),
            }
        )
        reader = ForecastOptimizationEvidence(lambda: service)
        body = reader.latest(instrument_id="XNSE:ALPHA", horizon="5d")
        assert body is not None
        self.assertEqual(body["available"], ["selection", "reliability", "fallback"])
        self.assertEqual(
            service.calls,
            [
                ("selection", "XNSE:ALPHA", "5d"),
                ("reliability", "XNSE:ALPHA", "5d"),
                ("fallback", "XNSE:ALPHA", "5d"),
            ],
        )

    def test_reader_is_lazy_and_tolerates_missing_kinds(self) -> None:
        opened: list[str] = []
        service = StubService(
            {"selection": selection(instrument_id="XNSE:ALPHA", horizon="5d")}
        )

        def provider() -> Any:
            opened.append("opened")
            return service

        reader = ForecastOptimizationEvidence(provider)
        self.assertEqual(opened, [])
        body = reader.latest(instrument_id="XNSE:ALPHA", horizon="5d")
        assert body is not None
        self.assertEqual(opened, ["opened"])
        self.assertEqual(body["available"], ["selection"])

    def test_an_empty_store_yields_no_evidence(self) -> None:
        reader = ForecastOptimizationEvidence(lambda: StubService({}))
        self.assertIsNone(reader.latest(instrument_id="XNSE:ALPHA", horizon="5d"))


if __name__ == "__main__":
    unittest.main()


class ApplicationWiringTests(unittest.IsolatedAsyncioTestCase):
    """The real application must hand PI-23 records to the decision pipeline."""

    async def asyncSetUp(self) -> None:
        from tempfile import TemporaryDirectory

        from novacontrol.novamarket.service import NovaMarketService

        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.service = NovaMarketService(data_dir=self._tmp.name)
        await self.service.enable()

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()
        self._tmp.cleanup()

    async def test_the_pipeline_status_advertises_the_reader(self) -> None:
        # ``enable`` can land in DEGRADED on a host without optional engines, and
        # a degraded NovaMarket deliberately serves no decision contract, so the
        # pipeline is opened through the same factory the contract uses.
        contract = self.service._ensure_decision().status()
        evidence = contract["forecast_optimization_evidence"]
        self.assertIs(evidence["reader"], True)
        self.assertIs(evidence["read_only"], True)
        self.assertIs(evidence["affects_action"], False)
        self.assertEqual(
            evidence["source"], "PI-23 forecast optimization evidence (read-only)"
        )
        self.assertEqual(contract["risk_authority"], "NM-08")
        self.assertEqual(contract["execution_authority"], "NM-10")
        self.assertIs(contract["orders_possible"], False)

    async def test_the_wired_reader_returns_real_store_evidence(self) -> None:
        from novacontrol.novamarket.decision.forecast_optimization import (
            ForecastOptimizationEvidence,
        )

        service = self.service.forecast_intelligence_service()
        service.store.save(
            "selection",
            selection(instrument_id="XNSE:ALPHA", horizon="5d"),
            record_id=SELECTION_ID,
            instrument_id="XNSE:ALPHA",
            horizon="5d",
            status="selected",
            created_at=NOW.isoformat(),
        )
        reader = ForecastOptimizationEvidence(self.service.forecast_intelligence_service)
        body = reader.latest(instrument_id="XNSE:ALPHA", horizon="5d")
        assert body is not None
        self.assertEqual(body["available"], ["selection"])
        self.assertEqual(body["selection"]["selection_id"], SELECTION_ID)
        self.assertEqual(body["risk_authority"], "NM-08")
        self.assertEqual(body["execution_authority"], "NM-10")
