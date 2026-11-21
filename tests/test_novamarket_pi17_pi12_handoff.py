"""PI-17 advisory handoff consumed by the PI-12 decision path, read-only.

These tests pin the integration boundary: the handoff may be attached to an
AT-03 proposal as evidence, and it may never select the action, satisfy the
NM-08 risk gate, authorise NM-10 execution, bypass the runtime mode/lease or
survive a restart as authority.
"""

from __future__ import annotations

import unittest
from typing import Any

from novacontrol.novamarket.autonomy.runtime import AutonomyRuntime
from novacontrol.novamarket.decision.advisory import (
    MultiAgentAdvisoryEvidence,
    advisory_evidence,
)
from novacontrol.novamarket.decision.models import DecisionErrorCode, DecisionStateError
from novacontrol.novamarket.decision.pipeline import AutonomyDecisionPipeline
from test_novamarket_pi12 import NOW, Harness

CYCLE_ID = "mac_pi17_evidence"
DECISION_ID = "madec_pi17_evidence"


def handoff(
    *,
    instrument_id: str,
    exchange: str = "XNSE",
    horizon: str = "5d",
    recommendation: str = "sell",
    **overrides: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "phase": "PI-17",
        "cycle_id": CYCLE_ID,
        "decision_id": DECISION_ID,
        "instrument_id": instrument_id,
        "exchange": exchange,
        "horizon": horizon,
        "recommendation": recommendation,
        "eligible_research_signal": True,
        "evidence_strength": "moderate",
        "restrictions": ("advisory only",),
        "authority": {
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "agents_can_override_kill_switch": False,
        },
        "decided_at": NOW.isoformat(),
        "cycle_status": "completed",
        "note": "advisory only; the trading decision stays with PI-12",
        "advisory_only": True,
        "production_authority": False,
        "order_submitted": False,
    }
    body.update(overrides)
    return body


class Reader:
    """A counted advisory reader that sanitises a handoff per signal."""

    def __init__(self, build: Any) -> None:
        self.calls: list[str] = []
        self._build = build

    def __call__(self, signal: Any) -> dict[str, Any] | None:
        self.calls.append(signal.signal_id)
        body = self._build(signal)
        if body is None:
            return None
        return advisory_evidence(
            body,
            instrument_id=signal.instrument_id,
            exchange=signal.exchange,
            horizon=signal.horizon,
        )


class Pi17AdvisoryHandoffTests(Harness):
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
            advisory_evidence=reader,
        )

    def proposals(self) -> list[dict[str, Any]]:
        return self.pipeline.proposals()["proposals"]

    def signals(self) -> list[dict[str, Any]]:
        return self.pipeline.signals()["signals"]

    async def test_the_handoff_is_attached_as_read_only_evidence(self) -> None:
        def build(signal: Any) -> dict[str, Any]:
            # Deliberately disagree with the signal: evidence is not a vote.
            recommendation = "sell" if signal.direction.lower() == "buy" else "buy"
            return handoff(
                instrument_id=signal.instrument_id,
                exchange=signal.exchange,
                horizon=signal.horizon,
                recommendation=recommendation,
            )

        reader = Reader(build)
        self.install_reader(reader)
        await self.run_once()
        signal = self.signals()[0]
        proposal = self.proposals()[0]
        evidence = proposal["advisory_evidence"]
        self.assertIsInstance(evidence, dict)
        self.assertIs(evidence["advisory_only"], True)
        self.assertIs(evidence["production_authority"], False)
        self.assertIs(evidence["order_submitted"], False)
        self.assertIs(evidence["read_only"], True)
        self.assertIs(evidence["affects_action"], False)
        self.assertIs(evidence["risk_review_required"], True)
        self.assertIs(evidence["requires_nm08_reassessment"], True)
        self.assertIs(evidence["requires_operator_permission"], True)
        self.assertIs(evidence["authority"]["agents_can_override_kill_switch"], False)
        self.assertEqual(evidence["cycle_id"], CYCLE_ID)
        self.assertEqual(evidence["cycle_status"], "completed")
        expected_action = {
            "buy": "add_position",
            "sell": "reduce_position",
            "exit": "remove_position",
        }[signal["direction"].lower()]
        self.assertEqual(proposal["action"], expected_action)
        opposite = "sell" if signal["direction"].lower() == "buy" else "buy"
        self.assertEqual(evidence["recommendation"], opposite)
        self.assertEqual(reader.calls, [signal["signal_id"]])

    async def test_a_mismatched_handoff_is_not_attached(self) -> None:
        self.install_reader(
            Reader(
                lambda signal: handoff(
                    instrument_id="XNSE:SOMETHING_ELSE",
                    exchange=signal.exchange,
                    horizon=signal.horizon,
                )
            )
        )
        await self.run_once()
        proposal = self.proposals()[0]
        self.assertIsNone(proposal["advisory_evidence"])
        self.assertTrue(proposal["proposal_id"])

    async def test_a_failing_reader_never_blocks_the_decision(self) -> None:
        def explode(signal: Any) -> dict[str, Any]:
            raise RuntimeError("the multi-agent service is unavailable")

        self.install_reader(explode)
        await self.run_once()
        proposal = self.proposals()[0]
        self.assertIsNone(proposal["advisory_evidence"])
        self.assertEqual(proposal["status"], "risk_approved")
        status = self.pipeline.status()
        self.assertIn("RuntimeError", status["advisory_evidence"]["last_error"])
        self.assertIs(status["advisory_evidence"]["read_only"], True)
        self.assertIs(status["advisory_evidence"]["affects_action"], False)

    async def test_replay_keeps_one_proposal_and_one_piece_of_evidence(self) -> None:
        reader = Reader(
            lambda signal: handoff(
                instrument_id=signal.instrument_id,
                exchange=signal.exchange,
                horizon=signal.horizon,
            )
        )
        self.install_reader(reader)
        first = await self.run_once()
        proposals_before = self.proposals()
        evidence_before = proposals_before[0]["advisory_evidence"]
        second = await self.run_once()
        proposals_after = self.proposals()
        self.assertEqual(len(proposals_after), 1)
        self.assertEqual(
            proposals_after[0]["proposal_id"], proposals_before[0]["proposal_id"]
        )
        self.assertEqual(proposals_after[0]["action"], proposals_before[0]["action"])
        self.assertEqual(proposals_after[0]["advisory_evidence"], evidence_before)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(reader.calls, [self.signals()[0]["signal_id"]])

    async def test_the_kill_switch_stops_the_decision_path_before_the_handoff(
        self,
    ) -> None:
        reader = Reader(
            lambda signal: handoff(
                instrument_id=signal.instrument_id,
                exchange=signal.exchange,
                horizon=signal.horizon,
            )
        )
        self.install_reader(reader)
        self.runtime.stop(reason="kill switch")
        with self.assertRaises(DecisionStateError) as raised:
            await self.run_once()
        self.assertEqual(raised.exception.code, DecisionErrorCode.RUNTIME_DISABLED.value)
        self.assertEqual(reader.calls, [])
        self.assertEqual(self.proposals(), [])
        self.assertEqual(self.pipeline.status()["runtime_mode"], "disabled")

    async def test_recovery_returns_to_disabled_without_consuming_evidence(
        self,
    ) -> None:
        reader = Reader(lambda signal: None)
        self.install_reader(reader)
        await self.run_once()
        calls_before = list(reader.calls)
        self.runtime.pause(reason="restart fixture")
        self.pipeline.close()
        self.runtime.close()
        reopened = AutonomyRuntime(self.path, clock=self.clock, owner_id="pi12-restart")
        self.runtime = reopened
        self.pipeline = AutonomyDecisionPipeline(
            reopened,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            forecasts=self.source,
            clock=self.clock,
            open_orders=lambda portfolio_id, instrument_id: [],
            advisory_evidence=reader,
        )
        self.assertEqual(reopened.status()["mode"], "disabled")
        with self.assertRaises(DecisionStateError) as raised:
            await self.run_once()
        self.assertEqual(raised.exception.code, DecisionErrorCode.RUNTIME_DISABLED.value)
        self.assertEqual(reader.calls, calls_before)


class AdvisorySanitizerTests(unittest.TestCase):
    def test_forced_flags_cannot_be_weakened_by_the_handoff(self) -> None:
        body = advisory_evidence(
            handoff(
                instrument_id="XNSE:ALPHA",
                recommendation="buy",
                production_authority=True,
                order_submitted=True,
                advisory_only=False,
                requires_nm08_reassessment=False,
                authority={"agents_can_override_kill_switch": True},
                secret="must not leak",
            ),
            instrument_id="XNSE:ALPHA",
            exchange="XNSE",
            horizon="5d",
        )
        self.assertIsNotNone(body)
        assert body is not None
        self.assertIs(body["production_authority"], False)
        self.assertIs(body["order_submitted"], False)
        self.assertIs(body["advisory_only"], True)
        self.assertIs(body["requires_nm08_reassessment"], True)
        self.assertIs(body["read_only"], True)
        self.assertIs(body["affects_action"], False)
        self.assertNotIn("secret", body)
        self.assertEqual(body["recommendation"], "buy")

    def test_identity_and_usable_content_are_required(self) -> None:
        self.assertIsNone(
            advisory_evidence(None, instrument_id="XNSE:ALPHA", horizon="5d")
        )
        self.assertIsNone(
            advisory_evidence(
                handoff(instrument_id="XNSE:BETA"),
                instrument_id="XNSE:ALPHA",
                horizon="5d",
            )
        )
        self.assertIsNone(
            advisory_evidence(
                handoff(instrument_id="XNSE:ALPHA", horizon="30d"),
                instrument_id="XNSE:ALPHA",
                horizon="5d",
            )
        )
        self.assertIsNone(
            advisory_evidence(
                handoff(instrument_id="XNSE:ALPHA", recommendation=""),
                instrument_id="XNSE:ALPHA",
                horizon="5d",
            )
        )
        self.assertIsNotNone(
            advisory_evidence(
                handoff(instrument_id="XNSE:ALPHA", horizon="5d"),
                instrument_id="XNSE:ALPHA",
                exchange="XNSE",
                horizon="5d",
            )
        )

    def test_the_reader_asks_for_the_signal_identity(self) -> None:
        seen: list[dict[str, str]] = []

        class Service:
            def latest_handoff(
                self, *, instrument_id: str = "", exchange: str = "", horizon: str = ""
            ) -> dict[str, Any]:
                seen.append(
                    {
                        "instrument_id": instrument_id,
                        "exchange": exchange,
                        "horizon": horizon,
                    }
                )
                return handoff(
                    instrument_id=instrument_id, exchange=exchange, horizon=horizon
                )

        reader = MultiAgentAdvisoryEvidence(Service)
        signal = type(
            "Signal",
            (),
            {"instrument_id": "XNSE:ALPHA", "exchange": "XNSE", "horizon": "5d"},
        )()
        body = reader(signal)
        self.assertIsNotNone(body)
        self.assertEqual(
            seen,
            [{"instrument_id": "XNSE:ALPHA", "exchange": "XNSE", "horizon": "5d"}],
        )
        assert body is not None
        self.assertIs(body["advisory_only"], True)
        self.assertIs(body["production_authority"], False)


def test_the_service_returns_only_a_completed_matching_handoff(tmp_path: Any) -> None:
    from pathlib import Path

    from test_novamarket_pi17 import context, create, service

    svc = service(
        Path(tmp_path), context(include_news=True, include_fundamentals=True)
    )
    cycle_id = create(svc)
    svc.run_cycle(cycle_id)
    body = svc.latest_handoff(instrument_id="TEST", exchange="XNSE", horizon="5d")
    assert body is not None
    assert body["cycle_id"] == cycle_id
    assert body["cycle_status"] == "completed"
    assert body["advisory_only"] is True
    assert body["production_authority"] is False
    assert body["order_submitted"] is False
    assert body["decided_at"]
    assert svc.latest_handoff(instrument_id="TEST", horizon="30d") is None
    assert svc.latest_handoff(instrument_id="TEST", exchange="XNAS") is None
    assert svc.latest_handoff(instrument_id="OTHER", horizon="5d") is None
    assert svc.latest_handoff(instrument_id="") is None


__all__ = [
    "AdvisorySanitizerTests",
    "Pi17AdvisoryHandoffTests",
    "test_the_service_returns_only_a_completed_matching_handoff",
]
