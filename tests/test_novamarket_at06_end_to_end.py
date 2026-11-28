"""One continuous acceptance workflow over the real AT-03/04/05 engines.

The market, discovery and forecast objects are deterministic fixtures, as in the
accepted AT-04/AT-05 suites.  Risk, strategy, decision, portfolio, wallet,
execution, exit and learning persistence are the production implementations.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_novamarket_at05 import ExitHarness  # noqa: E402

from novacontrol.novamarket.decision.forecast_metrics import forecast_metrics  # noqa: E402
from novacontrol.novamarket.decision.outcomes import ForecastOutcomeTracker  # noqa: E402


class CompleteWorkflowAcceptanceTests(ExitHarness):
    def _seed_invalid_candidate_cycle(self) -> str:
        cycle_id = "mon_at06_invalid_candidate"
        if self.runtime.store.monitoring_cycle(cycle_id) is None:
            self.runtime.store.create_monitoring_cycle(
                {
                    "cycle_id": cycle_id,
                    "cycle_key": "monitoring:XNSE:2026-11-11:at06-invalid",
                    "owner_id": "at06-test",
                    "mode": "autonomous",
                    "exchange": "XNSE",
                    "trading_date": "2026-11-11",
                    "calendar_verified": True,
                    "calendar_source": "marketdata/calendar.py",
                    "session_state": "trading_session",
                    "session_eligible": True,
                    "phase": "monitoring",
                    "status": "running",
                    "trigger": "seed",
                    "started_at": self.clock.now.isoformat(),
                    "rotation_start": 0,
                },
                now=self.clock.now,
            )
            self.runtime.store.update_monitoring_cycle(
                cycle_id,
                status="completed",
                phase="completed",
                finished=True,
                processed_count=1,
                now=self.clock.now,
            )
            self.runtime.store.record_monitoring_candidates(
                cycle_id,
                [
                    {
                        "instrument_id": "XNSE:ALPHA",
                        "symbol": "ALPHA",
                        "exchange": "XNSE",
                        "disposition": "processed",
                        "reason": "at06 invalid fixture",
                        "rank": 1,
                        "data_quality": "stale",
                    }
                ],
            )
        return cycle_id

    async def test_20_step_isolated_paper_workflow(self) -> None:
        # 1-4: the harness has already started the runtime, restored the real
        # wallet/portfolio book and prepared the deterministic market fixture.
        self.assertEqual(self.runtime.status()["mode"], "autonomous")
        detail_before = self.portfolios.detail(self.portfolio_id)
        self.assertIn("valuation", detail_before)
        self.assertTrue(detail_before["valuation"]["cash"] is not None)
        wallet_id = str(detail_before["portfolio"]["wallet_id"])
        wallet_before = Decimal(
            str(self.wallets.wallet(wallet_id)["wallet"]["balance"]["total"])
        )
        self.assertEqual(Decimal(str(detail_before["cash"])), wallet_before)

        # 5-7: a forecast decision with a target that has already elapsed.
        target = self.clock.now - timedelta(days=2)
        prediction = {
            "decision_id": "pdec_at06_accept",
            "decision_key": "at06:acceptance:prediction",
            "run_id": "run_at06_accept",
            "monitoring_cycle_id": self.cycle_id,
            "portfolio_id": self.portfolio_id,
            "instrument_id": "XNSE:ALPHA",
            "decision_type": "BUY",
            "horizon": "next_session",
            "primary_forecast_id": "fc_ALPHA_next_session",
            "proposal_id": "",
            "created_at": (target - timedelta(days=1)).isoformat(),
            "target_at": target.isoformat(),
            "reference_price": "100",
            "expected_return": 0.02,
            "probability_up": 0.70,
            "status": "risk_approved",
            "primary_forecast": {
                "forecast_id": "fc_ALPHA_next_session",
                "direction": "up",
                "predicted_return": "0.02",
                "model_version": "m1",
                "ensemble_id": "ens_at06",
                "data_observed_at": (target - timedelta(days=1)).isoformat(),
            },
        }
        saved_prediction = self.decisions.store.save_prediction_decision(prediction)
        self.assertEqual(saved_prediction["decision_id"], prediction["decision_id"])

        # 8-11: AT-03 proposal -> NM-08 approval -> NM-10 fill -> portfolio.
        proposal_detail = self.decisions.proposal_detail(
            self.proposal["proposal_id"]
        )
        risk = proposal_detail["risk"]
        self.assertEqual(proposal_detail["proposal"]["status"], "risk_approved")
        self.assertEqual(risk["outcome"], "approved")
        self.assertEqual(risk["proposal_id"], self.proposal["proposal_id"])
        self.assertEqual(
            risk["risk_decision_id"], self.proposal["risk_decision_id"]
        )
        entry = await self.enter_position()
        self.assertTrue(entry["order"]["order_id"].startswith("ord_"))
        self.assertTrue(entry["fills"][0]["ledger_transaction_ids"])
        self.assertEqual(
            entry["order"]["risk_decision_id"], risk["risk_decision_id"]
        )
        self.assertEqual(
            entry["order"]["proposal_id"], self.proposal["proposal_id"]
        )
        self.assertTrue(self.holding())
        wallet_after_entry = Decimal(
            str(self.wallets.wallet(wallet_id)["wallet"]["balance"]["total"])
        )
        detail_after_entry = self.portfolios.detail(self.portfolio_id)
        self.assertEqual(
            Decimal(str(detail_after_entry["cash"])), wallet_after_entry
        )
        self.assertLess(wallet_after_entry, wallet_before)

        # 12-17: position supervision -> exit proposal -> NM-08 -> NM-10 ->
        # ledger reconciliation -> realized P&L.
        self.crash_price()
        supervisor = self.make_supervisor()
        exit_cycle = await supervisor.run_cycle(trigger="at06-e2e-exit")
        self.assertEqual(exit_cycle["submitted"], 1, exit_cycle)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], "filled")
        self.assertEqual(decision["reconciliation_status"], "passed")
        self.assertTrue(decision["position_closed"])
        self.assertEqual(decision["risk_outcome"], "approved")
        self.assertNotEqual(
            decision["risk_assessment_id"], self.proposal["risk_decision_id"]
        )
        exit_risk = self.risk.store.decision(decision["risk_assessment_id"])
        self.assertIsNotNone(exit_risk)
        self.assertEqual(exit_risk["outcome"], "approved")
        self.assertEqual(exit_risk["proposal_id"], decision["proposal_id"])
        self.assertTrue(decision["order_id"])
        self.assertEqual(self.sell_orders()[0].proposal_id, decision["proposal_id"])
        self.assertIsNone(self.holding())
        detail_after_exit = self.portfolios.detail(self.portfolio_id)
        wallet_after_exit = Decimal(
            str(self.wallets.wallet(wallet_id)["wallet"]["balance"]["total"])
        )
        self.assertEqual(
            Decimal(str(detail_after_exit["cash"])), wallet_after_exit
        )
        self.assertGreater(wallet_after_exit, wallet_after_entry)
        self.assertLess(wallet_after_exit, wallet_before)
        evidence = self.execution_store.learning_evidence()[0]
        self.assertEqual(evidence["status"], "complete")
        self.assertTrue(evidence["realized"]["portfolio_realized_pnl"])
        self.assertLess(
            Decimal(str(evidence["realized"]["portfolio_realized_pnl"])), 0
        )

        # 18: evaluate the prediction only now that its target has elapsed.
        tracker = ForecastOutcomeTracker(
            self.decisions.store,
            self.market,
            execution_provider=lambda: self.execution,
            clock=self.clock,
        )
        tracked = tracker.track(limit=10)
        self.assertEqual(tracked["measured_count"], 1, tracked)
        outcome = tracked["measured"][0]
        self.assertEqual(outcome["decision_id"], prediction["decision_id"])
        self.assertEqual(outcome["forecast"]["model_id"], "m1")
        self.assertEqual(outcome["forecast"]["direction"], "up")
        self.assertIsNotNone(outcome["forecast"]["error"])
        self.assertIsNotNone(outcome["forecast"]["absolute_error"])
        self.assertIsNotNone(outcome["forecast"]["brier_score"])
        self.assertIsNotNone(outcome["forecast"]["provenance"]["actual_observation_at"])
        metrics = forecast_metrics([outcome])
        self.assertEqual(metrics["evaluated"], 1)
        self.assertIsNotNone(metrics["mae"])
        self.assertIsNotNone(metrics["directional_accuracy"])
        self.assertFalse(metrics["automatic_promotion"])

        # 19: the verified round trip produced a costed RL experience/reward.
        feedback = evidence["rl_feedback"]
        self.assertEqual(feedback["status"], "verified")
        self.assertEqual(feedback["source_evidence_id"], evidence["evidence_id"])
        self.assertGreater(Decimal(feedback["fees"]), 0)
        self.assertEqual(feedback["unrealized_pnl"], "0")
        self.assertFalse(feedback["automatic_policy_promotion"])
        self.assertFalse(feedback["training_performed"])

        # 19b: a stale candidate is rejected before any proposal or order.
        order_ids = [order.order_id for order in self.execution_store.orders(limit=100)]
        wallet_before_invalid = Decimal(
            str(self.wallets.wallet(wallet_id)["wallet"]["balance"]["total"])
        )
        invalid_run = await self.decisions.run_cycle(
            monitoring_cycle_id=self._seed_invalid_candidate_cycle(),
            trigger="manual",
            actor="at06-test",
        )
        self.assertGreaterEqual(invalid_run["invalid"], 1, invalid_run)
        self.assertEqual(
            self.decisions.store.proposals(run_id=invalid_run["run_id"]), []
        )
        self.assertEqual(
            [order.order_id for order in self.execution_store.orders(limit=100)],
            order_ids,
        )
        self.assertEqual(
            Decimal(str(self.wallets.wallet(wallet_id)["wallet"]["balance"]["total"])),
            wallet_before_invalid,
        )

        # 20: a restart reconciles without replaying the order or the exit.
        supervisor.close()
        restarted = self.make_supervisor()
        recovered = await restarted.recover()
        self.assertEqual(recovered["status"], "completed", recovered)
        self.assertEqual(
            [order.order_id for order in self.execution_store.orders(limit=100)],
            order_ids,
        )
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)
        self.assertEqual(
            self.execution_store.learning_evidence()[0]["evidence_id"],
            evidence["evidence_id"],
        )
