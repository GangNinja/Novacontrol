"""AT-06 integration tests added for the continuous-runtime completion pass."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from novacontrol.core.events import EventBus
from novacontrol.novamarket.autonomy import AutonomyRuntime
from novacontrol.novamarket.decision.forecast_metrics import forecast_metrics
from novacontrol.novamarket.learning.paper_feedback import (
    build_paper_experience,
    chronological_splits,
)
from novacontrol.novamarket.service import NovaMarketService


class ForecastMetricsTests(unittest.TestCase):
    def test_metrics_use_only_measured_forecast_error(self) -> None:
        rows = [
            {
                "actual_return": 0.01,
                "forecast": {
                    "expected_return": 0.02,
                    "confidence": 0.8,
                    "correct": True,
                },
            },
            {
                "actual_return": 0.01,
                "forecast": {
                    "expected_return": -0.01,
                    "confidence": 0.7,
                    "correct": False,
                },
            },
            {
                "actual_return": 0.03,
                "forecast": {
                    "expected_return": 0.03,
                    "confidence": 0.9,
                    "correct": True,
                },
            },
            {"actual_return": 0.04, "forecast": {}},
        ]
        result = forecast_metrics(rows)
        self.assertEqual(result["evaluated"], 3)
        self.assertEqual(result["excluded"], 1)
        self.assertAlmostEqual(result["mae"], 0.01, places=8)
        self.assertAlmostEqual(result["rmse"], 0.01290994, places=8)
        self.assertAlmostEqual(result["mean_error"], -0.00333333, places=8)
        self.assertAlmostEqual(result["directional_accuracy"], 0.666667, places=6)
        self.assertAlmostEqual(result["brier_score"], 0.18, places=8)
        self.assertEqual(len(result["calibration"]), 2)
        self.assertFalse(result["automatic_promotion"])

    def test_empty_metrics_do_not_invent_a_score(self) -> None:
        result = forecast_metrics(())
        self.assertEqual(result["evaluated"], 0)
        self.assertIsNone(result["mae"])
        self.assertIsNone(result["directional_accuracy"])
        self.assertEqual(result["calibration"], [])


def _complete_evidence() -> dict:
    return {
        "evidence_id": "xev_test",
        "decision_id": "xdec_test",
        "status": "complete",
        "portfolio_id": "p1",
        "instrument_id": "XNSE:ALPHA",
        "strategy_id": "s1",
        "strategy_version": "1",
        "created_at": "2026-11-11T10:00:00+00:00",
        "entry": {
            "order_id": "ord_entry",
            "opened_at": "2026-11-11T09:15:00+00:00",
            "fills": [
                {
                    "quantity": "100",
                    "price": "100",
                    "total_fees": "10",
                    "slippage_amount": "2",
                }
            ],
        },
        "exit": {
            "order_id": "ord_exit",
            "reconciliation_status": "passed",
            "fills": [
                {
                    "quantity": "100",
                    "price": "105",
                    "total_fees": "10",
                    "slippage_amount": "3",
                }
            ],
        },
        "realized": {"position_realized_pnl": ""},
    }


class PaperFeedbackTests(unittest.TestCase):
    def test_complete_round_trip_becomes_one_costed_experience(self) -> None:
        feedback = build_paper_experience(_complete_evidence())
        self.assertEqual(feedback["status"], "verified")
        self.assertEqual(Decimal(feedback["gross_pnl"]), Decimal("500"))
        self.assertEqual(Decimal(feedback["fees"]), Decimal("20"))
        self.assertEqual(Decimal(feedback["slippage"]), Decimal("5"))
        self.assertEqual(Decimal(feedback["realized_pnl"]), Decimal("480"))
        self.assertEqual(Decimal(feedback["unrealized_pnl"]), Decimal("0"))
        self.assertEqual(feedback["reward_components"]["deposit"], 0.0)
        self.assertGreater(feedback["reward"], 0.0)
        self.assertFalse(feedback["automatic_policy_promotion"])
        self.assertFalse(feedback["training_performed"])
        self.assertFalse(feedback["policy_modified"])

    def test_incomplete_or_unreconciled_trade_is_not_an_experience(self) -> None:
        evidence = _complete_evidence()
        evidence["status"] = "partial"
        self.assertEqual(build_paper_experience(evidence)["status"], "ineligible")
        evidence = _complete_evidence()
        evidence["exit"]["reconciliation_status"] = "failed"
        self.assertEqual(build_paper_experience(evidence)["status"], "ineligible")

    def test_splits_are_chronological_not_random(self) -> None:
        rows = []
        for index in range(10):
            rows.append(
                {
                    "chronological_key": f"2026-11-{index + 1:02d}T10:00:00+00:00",
                    "reward": str(index / 10),
                }
            )
        result = chronological_splits(list(reversed(rows)))
        self.assertEqual((result["train"], result["validation"], result["test"]), (7, 1, 2))
        self.assertEqual([row["split"] for row in result["rows"]].count("train"), 7)
        self.assertTrue(result["chronological"])
        self.assertFalse(result["automatic_policy_promotion"])


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 10, 3, 45, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class AutoStartIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _boot(self, directory: str) -> NovaMarketService:
        service = NovaMarketService(
            data_dir=Path(directory),
            event_bus=EventBus(continue_on_error=True),
        )
        await service.enable(reason="auto-start test")
        service.start_runtime()
        return service

    async def test_unclean_restart_inside_lease_window_retries_after_expiry(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            clock = _Clock()
            path = Path(directory) / "novamarket.sqlite3"
            stale = AutonomyRuntime(
                path, clock=clock, owner_id="dead-owner", lease_seconds=60
            )
            stale.start("monitoring")
            # Simulate an unclean death: the durable lease survives, but the
            # process that owned it is gone and can never renew it.
            stale.store.close()

            service = NovaMarketService(
                data_dir=Path(directory),
                event_bus=EventBus(continue_on_error=True),
                clock=clock,
            )
            with mock.patch.object(
                NovaMarketService,
                "_autonomy_heartbeat_interval_seconds",
                return_value=0.01,
            ), mock.patch.object(service, "_runtime_tick", mock.AsyncMock()):
                await service.enable(reason="restart test")
                service.start_runtime()

                before = service.autonomy_status()
                self.assertEqual(before["mode"], "disabled")
                self.assertEqual(before["runtime_state"], "degraded")
                self.assertTrue(before["lease"]["foreign"])
                self.assertTrue(before["recovery_required"])
                self.assertTrue(before["auto_start_retry"]["active"])
                self.assertIsNotNone(before["auto_start_retry"]["retry_until"])
                self.assertIn("never stolen", before["auto_start_retry"]["reason"])

                # The abandoned lease is not stolen.  Once it has expired the
                # heartbeat retry resumes the persisted application-start mode
                # without any operator action.
                clock.advance(61)
                for _ in range(100):
                    if service.autonomy_status()["mode"] != "disabled":
                        break
                    await asyncio.sleep(0.01)

                after = service.autonomy_status()
                # The stale owner had last enabled monitoring, so that is the
                # persisted application-start mode the retry resumes.
                self.assertEqual(after["mode"], "monitoring")
                self.assertEqual(after["runtime_state"], "running")
                self.assertTrue(after["lease"]["held"])
                self.assertFalse(after["lease"]["foreign"])
                self.assertFalse(after["recovery_required"])
                self.assertFalse(after["auto_start_retry"]["active"])
                self.assertIsNone(after["auto_start_retry"]["retry_until"])

                await service.stop_runtime()
                await service.shutdown(reason="restart test shutdown")

    async def test_runtime_scheduler_ticks_before_its_first_sleep(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            service = NovaMarketService(
                data_dir=Path(directory),
                event_bus=EventBus(continue_on_error=True),
            )
            order: list[str] = []

            async def tick() -> None:
                order.append("tick")

            async def sleep(_seconds: float) -> None:
                order.append("sleep")
                raise asyncio.CancelledError

            with mock.patch.object(
                service, "_autonomy_startup_reconcile", mock.AsyncMock()
            ), mock.patch.object(service, "_runtime_tick", tick), mock.patch(
                "novacontrol.novamarket.service.asyncio.sleep", sleep
            ):
                await service._run_runtime()

            self.assertEqual(order[:2], ["tick", "sleep"])
            await service.shutdown(reason="tick ordering test shutdown")

    async def test_application_start_auto_starts_and_emergency_stop_survives_restart(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            first = await self._boot(directory)
            first_status = first.autonomy_status()
            self.assertEqual(first_status["mode"], "autonomous")
            self.assertTrue(first_status["trading_implemented"])
            self.assertTrue(first_status["orders_possible"])
            self.assertFalse(first_status["live_orders_possible"])
            self.assertEqual(first_status["release_gate"], "open")
            self.assertFalse(first_status["lease"]["foreign"])
            task = first._runtime_task
            first.start_runtime()  # duplicate call must not create a second task
            self.assertIs(first._runtime_task, task)
            await first.stop_runtime()
            await first.shutdown(reason="graceful test shutdown")

            second = await self._boot(directory)
            self.assertEqual(second.autonomy_status()["mode"], "autonomous")
            stopped = second.autonomy_stop(reason="emergency paper halt")
            self.assertEqual(stopped["mode"], "disabled")
            self.assertFalse(stopped["config"]["auto_start"])
            await second.stop_runtime()
            await second.shutdown(reason="halt test shutdown")

            third = await self._boot(directory)
            third_status = third.autonomy_status()
            self.assertEqual(third_status["mode"], "disabled")
            self.assertFalse(third_status["config"]["auto_start"])
            await third.stop_runtime()
            await third.shutdown(reason="final test shutdown")
