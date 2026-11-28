"""AT-06 acceptance scenarios over the real paper-trading engines.

The wallet, portfolio, risk, strategy, decision, execution, ledger and exit
stores are the real implementations.  Only the market/forecast providers are
deterministic fixtures, exactly as in the accepted AT-04/AT-05 suites.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import tracemalloc
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from test_novamarket_at04 import Clock, TimeoutExecution  # noqa: E402
from test_novamarket_at05 import ExitHarness  # noqa: E402

from novacontrol.novamarket.autonomy import AutonomyRuntime  # noqa: E402
from novacontrol.novamarket.autonomy_execution import AutonomyPaperExecution  # noqa: E402
from novacontrol.novamarket.autonomy_exits import ExitStatus  # noqa: E402
from novacontrol.novamarket.autonomy_observability import (  # noqa: E402
    build_reliability_report,
    failure_domain,
)
from novacontrol.novamarket.execution import OrderStatus  # noqa: E402


class ReliabilityReportTests(unittest.TestCase):
    def test_failure_domains_are_stable_and_unknown_is_not_guessed(self) -> None:
        self.assertEqual(failure_domain("LEASE_INVALID"), "lease")
        self.assertEqual(failure_domain("EXECUTION_SUBMISSION_UNKNOWN"), "execution")
        self.assertEqual(failure_domain("NOT_A_REAL_CODE"), "unknown")
        self.assertEqual(failure_domain(""), "unknown")

    def test_report_composes_statuses_without_inventing_health(self) -> None:
        report = build_reliability_report(
            runtime={
                "mode": "autonomous",
                "runtime_state": "running",
                "paper_only": True,
                "live_trading": False,
                "lease": {"valid": True, "expires_at": "2026-11-11T05:00:00+00:00"},
                "health": {"state": "ok", "reasons": []},
            },
            monitoring={"health": {"state": "ok", "reasons": []},
                        "scheduler": {"running": True}, "instruments_discovered": 7},
            decisions={"counts": {"evaluations": 4, "signals": 3, "proposals": 2,
                                  "risk_assessments": 2},
                       "health": {"state": "ok", "reasons": []}},
            execution={"counts": {"intents": 2, "orders": 1, "fills": 1,
                                  "forwarding": 1, "unknown": 0},
                       "health": {"state": "degraded",
                                  "reasons": ["one or more executions require reconciliation"]}},
            exits={"counts": {"decisions": 0}, "open_decisions": 0},
            learning={"evidence": [], "total": 0},
            recovery={"status": "completed", "unresolved": ["aex_1"],
                      "unresolved_count": 1},
        )
        self.assertEqual(report["milestone"], "AT-06")
        self.assertTrue(report["paper_only"])
        self.assertFalse(report["live_trading"])
        self.assertEqual(report["health"]["state"], "degraded")
        self.assertIn("execution", report["health"]["reasons"][0])
        self.assertEqual(report["financial"]["pending_orders"], 1)
        self.assertEqual(report["recovery"]["unresolved_count"], 1)
        self.assertFalse(report["recovery"]["auto_resumed"])
        self.assertEqual(report["safety"]["risk_authority"], "NM-08")
        self.assertEqual(report["safety"]["execution_authority"], "NM-10")


class LifecycleAcceptanceTests(ExitHarness):
    # Scenario 1
    async def test_scenario_01_clean_startup_is_safe_default(self) -> None:
        path = Path(self._tmp.name) / "clean-start.sqlite3"
        runtime = AutonomyRuntime(path, clock=self.clock, owner_id="at06-clean")
        try:
            status = runtime.status()
            self.assertEqual(status["mode"], "disabled")
            self.assertEqual(status["runtime_state"], "stopped")
            self.assertFalse(status["lease"]["valid"])
        finally:
            runtime.close()

    # Scenario 2
    async def test_scenario_02_restart_does_not_auto_resume(self) -> None:
        path = Path(self._tmp.name) / "restart.sqlite3"
        first = AutonomyRuntime(path, clock=self.clock, owner_id="at06-first")
        try:
            first.start("monitoring", reason="acceptance")
        finally:
            first.close()
        second = AutonomyRuntime(path, clock=self.clock, owner_id="at06-second")
        try:
            status = second.status()
            self.assertEqual(status["mode"], "disabled")
            self.assertEqual(status["runtime_state"], "stopped")
            self.assertEqual(status["desired_mode"], "disabled")
        finally:
            second.close()

    # Scenario 3
    async def test_scenario_03_monitoring_creates_no_order(self) -> None:
        self.assertEqual(self.proposal["status"], "risk_approved")
        self.assertEqual(self.buy_orders(), [])
        self.assertEqual(self.execution_store.orders(), ())

    # Scenario 4
    async def test_scenario_04_autonomous_proposal_has_nm08_provenance(self) -> None:
        self.assertEqual(self.runtime.status()["mode"], "autonomous")
        self.assertEqual(self.proposal["risk_outcome"], "approved")
        self.assertTrue(self.proposal["risk_decision_id"].startswith("rdec_"))
        assessment = self.decisions._store.risk_assessment(self.proposal["proposal_id"])
        self.assertEqual(assessment["outcome"], "approved")

    # Scenario 5
    async def test_scenario_05_approved_entry_creates_one_nm10_order(self) -> None:
        result = await self.enter_position()
        self.assertEqual(len(self.buy_orders()), 1)
        self.assertEqual(result["execution"]["order_id"], result["order"]["order_id"])
        self.assertEqual(len(result["fills"]), 1)
        self.assertTrue(result["order"]["market_reference"]["currency"] == "INR")

    # Scenario 6
    async def test_scenario_06_rejected_entry_never_reaches_nm10(self) -> None:
        rejected = dict(self.proposal)
        rejected["status"] = "risk_rejected"
        inspected, code, _ = await self.executions._preflight(rejected)
        self.assertIsNone(inspected)
        self.assertEqual(code, "RISK_APPROVAL_INVALID")
        self.assertEqual(self.execution_store.orders(), ())

    # Scenario 7
    async def test_scenario_07_duplicate_proposal_does_not_duplicate_order(self) -> None:
        first = await self.enter_position()
        second = await self.run_execution()
        self.assertEqual(
            first["order"]["order_id"], second["executions"][0]["order"]["order_id"]
        )
        self.assertEqual(len(self.execution_store.orders()), 1)
        self.assertEqual(len(self.execution_store.fills()), 1)

    # Scenario 8
    async def test_scenario_08_submission_timeout_reconciles_before_retry(self) -> None:
        timeout = TimeoutExecution(self.execution)
        service = AutonomyPaperExecution(
            self.runtime, self.decisions, timeout, self.portfolios,
            market_data=self.market, clock=self.clock,
        )
        try:
            first = await service.run_cycle(
                trigger="timeout", actor="at06",
                proposal_id=self.proposal["proposal_id"],
            )
            self.assertEqual(first["executions"][0]["execution"]["status"], "unknown")
            self.assertEqual(self.execution_store.orders(), ())
            timeout.fail = False
            second = await service.run_cycle(
                trigger="recovery", actor="at06",
                proposal_id=self.proposal["proposal_id"],
            )
            self.assertEqual(second["executions"][0]["execution"]["status"], "filled")
            self.assertEqual(len(self.execution_store.orders()), 1)
        finally:
            service.close()

    # Scenario 9
    async def test_scenario_09_partial_entry_fill_has_correct_remainder(self) -> None:
        self.market.volume = 20.0
        result = await self.run_execution()
        execution = result["executions"][0]
        self.assertEqual(execution["order"]["status"], "partially_filled")
        self.assertGreater(Decimal(execution["execution"]["remaining_quantity"]), 0)
        self.assertTrue(execution["fills"][0]["ledger_transaction_ids"])
        self.assertEqual(len(self.execution_store.fills()), 1)

    # Scenario 10
    async def test_scenario_10_full_entry_reconciles_position_and_ledger(self) -> None:
        result = await self.enter_position()
        order = result["order"]
        detail = self.portfolios.detail(self.portfolio_id)
        holding = next(row for row in detail["holdings"]
                       if row["instrument_id"] == "XNSE:ALPHA")
        self.assertEqual(Decimal(str(holding["quantity"])),
                         Decimal(order["filled_quantity"]))
        self.assertTrue(result["fills"][0]["ledger_transaction_ids"])
        reconciled = await self.execution.reconcile(order["order_id"])
        self.assertTrue(reconciled["ok"], reconciled)

    # Scenario 11
    async def test_scenario_11_stop_loss_generates_and_executes_exit(self) -> None:
        await self.enter_position()
        self.crash_price()
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="at06-stop")
        self.assertEqual(result["submitted"], 1, result)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["condition"], "stop_loss")
        self.assertEqual(decision["status"], ExitStatus.FILLED.value)
        self.assertEqual(decision["reconciliation_status"], "passed")

    # Scenario 12
    async def test_scenario_12_target_generates_one_paper_exit(self) -> None:
        await self.enter_position()
        average = Decimal(str(self.holding()["average_cost"]))
        target = (average * Decimal("1.05")).quantize(Decimal("0.01"))
        self.configure_exit(target=target)
        self.set_price("1.05")
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="at06-target")
        self.assertEqual(result["submitted"], 1, result)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["condition"], "target_price")
        self.assertEqual(decision["status"], ExitStatus.FILLED.value)

    # Scenario 13
    async def test_scenario_13_paused_mode_blocks_new_entries(self) -> None:
        self.runtime.pause(reason="at06 acceptance")
        with self.assertRaises(Exception):
            await self.decisions.run_cycle(
                monitoring_cycle_id=self.cycle_id, trigger="paused", actor="at06"
            )
        self.assertEqual(self.buy_orders(), [])

    # Scenario 14
    async def test_scenario_14_paused_mode_allows_protective_exit(self) -> None:
        await self.enter_position()
        self.runtime.pause(reason="at06 paused protective exit")
        self.crash_price()
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="paused")
        self.assertEqual(result["mode"], "paused")
        self.assertEqual(result["submitted"], 1, result)
        self.assertEqual(self.execution_store.exit_decisions()[0]["status"],
                         ExitStatus.FILLED.value)

    # Scenario 15
    async def test_scenario_15_partial_exit_keeps_correct_remaining_position(self) -> None:
        await self.enter_position()
        self.crash_price()
        self.market.volume = 10.0
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="partial-exit")
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.PARTIALLY_FILLED.value)
        self.assertGreater(Decimal(decision["remaining_quantity"]), 0)
        self.assertFalse(decision["position_closed"])
        second = await supervisor.run_cycle(trigger="duplicate")
        self.assertEqual(second["submitted"], 0)
        self.assertEqual(len(self.sell_orders()), 1)

    # Scenario 16
    async def test_scenario_16_full_exit_closes_position_and_reconciles(self) -> None:
        await self.enter_position()
        self.crash_price()
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="full-exit")
        decision = self.execution_store.exit_decisions()[0]
        self.assertTrue(decision["position_closed"])
        self.assertEqual(decision["reconciliation_status"], "passed")
        self.assertIsNone(self.holding())

    # Scenario 17
    async def test_scenario_17_duplicate_exit_trigger_is_suppressed(self) -> None:
        await self.enter_position()
        self.crash_price()
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="first")
        second = await supervisor.run_cycle(trigger="duplicate")
        self.assertEqual(second["submitted"], 0, second)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)
        self.assertEqual(len(self.sell_orders()), 1)

    # Scenario 18
    async def test_scenario_18_crash_recovery_does_not_duplicate_exit(self) -> None:
        await self.enter_position()
        self.crash_price()
        self.market.volume = 10.0
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="before-crash")
        order_id = self.sell_orders()[0].order_id
        supervisor.close()
        restarted = self.make_supervisor()
        recovery_started = time.perf_counter()
        recovered = await restarted.recover()
        recovery_elapsed = time.perf_counter() - recovery_started
        print("AT06_RECOVERY_MS=" + json.dumps({
            "milliseconds": round(recovery_elapsed * 1000, 2),
            "recovered": recovered.get("recovered"),
            "reconciled": recovered.get("reconciled"),
        }, sort_keys=True))
        self.assertEqual(recovered["status"], "completed", recovered)
        again = await restarted.run_cycle(trigger="after-restart")
        self.assertEqual(again["status"], "completed", again)
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(self.sell_orders()[0].order_id, order_id)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)

    # Scenario 19
    async def test_scenario_19_learning_links_entry_and_exit(self) -> None:
        entry = await self.enter_position()
        self.crash_price()
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="learning")
        evidence = self.execution_store.learning_evidence()
        self.assertEqual(len(evidence), 1)
        row = evidence[0]
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["entry"]["order_id"], entry["order"]["order_id"])
        self.assertTrue(row["exit"]["order_id"].startswith("ord_"))
        self.assertFalse(row["auto_promoted"])
        self.assertFalse(row["strategy_modified"])
        feedback = row["rl_feedback"]
        self.assertEqual(feedback["status"], "verified")
        self.assertIsNotNone(feedback["reward"])
        self.assertEqual(feedback["unrealized_pnl"], "0")
        self.assertFalse(feedback["automatic_policy_promotion"])
        self.assertFalse(feedback["training_performed"])
        self.assertFalse(feedback["policy_modified"])
        self.assertGreater(len(feedback["entry_order_id"]), 0)
        self.assertEqual(feedback["source_evidence_id"], row["evidence_id"])

    # Scenario 20
    async def test_scenario_20_stop_is_the_kill_switch(self) -> None:
        await self.enter_position()
        self.runtime.stop(reason="at06 kill switch")
        self.crash_price()
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="kill-switch")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skipped_reason"], "runtime_disabled")
        self.assertEqual(self.sell_orders(), [])
        with self.assertRaises(Exception):
            await self.run_execution()
        self.assertEqual(len(self.buy_orders()), 1)

    # AT-05 addendum: trailing-stop acceptance and restart persistence.
    async def test_scenario_21_trailing_stop_generates_one_exit(self) -> None:
        await self.enter_position()
        self.configure_exit(trailing_percent=Decimal("5"))
        supervisor = self.make_supervisor()
        self.set_price("1.08")
        high = await supervisor.run_cycle(trigger="high")
        self.assertEqual(high["submitted"], 0, high)
        self.set_price("1.02")
        triggered = await supervisor.run_cycle(trigger="trailing")
        self.assertEqual(triggered["submitted"], 1, triggered)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["condition"], "trailing_stop")
        self.assertEqual(len(self.sell_orders()), 1)

    async def test_scenario_22_trailing_high_water_survives_restart(self) -> None:
        await self.enter_position()
        self.configure_exit(trailing_percent=Decimal("5"))
        self.set_price("1.08")
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="before-restart")
        persisted = Decimal(
            str(self.execution_store.exit_configurations()[0]["high_water_mark"])
        )
        supervisor.close()
        restarted = self.make_supervisor()
        self.set_price("1.07")
        await restarted.run_cycle(trigger="after-restart")
        self.assertEqual(
            Decimal(self.execution_store.exit_configurations()[0]["high_water_mark"]),
            persisted,
        )


class PerformanceAcceptanceTests(ExitHarness):
    async def test_sustained_duplicate_cycles_do_not_leak_tasks_or_orders(self) -> None:
        await self.enter_position()
        self.crash_price()
        self.market.volume = 10.0
        supervisor = self.make_supervisor()
        first = await supervisor.run_cycle(trigger="sustained-first")
        self.assertEqual(first["submitted"], 1, first)
        for _ in range(10):
            await supervisor.run_cycle(trigger="warmup")
        tasks_before = len(asyncio.all_tasks())
        tracemalloc.start()
        started = time.perf_counter()
        for _ in range(60):
            result = await supervisor.run_cycle(trigger="sustained")
            self.assertEqual(result["submitted"], 0, result)
        elapsed = time.perf_counter() - started
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        tasks_after = len(asyncio.all_tasks())
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)
        self.assertLessEqual(tasks_after, tasks_before + 1)
        runtime_path = Path(self._tmp.name) / "perf-runtime.sqlite3"
        runtime = AutonomyRuntime(runtime_path, clock=Clock(), owner_id="at06-perf")
        startup_started = time.perf_counter()
        runtime.start("monitoring", reason="at06 performance")
        startup_elapsed = time.perf_counter() - startup_started
        shutdown_started = time.perf_counter()
        runtime.stop(reason="at06 performance")
        shutdown_elapsed = time.perf_counter() - shutdown_started
        runtime.close()
        print("AT06_PERF=" + json.dumps({
            "cycles": 60,
            "elapsed_seconds": round(elapsed, 4),
            "mean_cycle_ms": round((elapsed / 60) * 1000, 2),
            "tasks_before": tasks_before,
            "tasks_after": tasks_after,
            "traced_current_bytes": current,
            "traced_peak_bytes": peak,
            "sell_orders": len(self.sell_orders()),
            "exit_decisions": len(self.execution_store.exit_decisions()),
            "runtime_startup_ms": round(startup_elapsed * 1000, 2),
            "runtime_shutdown_ms": round(shutdown_elapsed * 1000, 2),
        }, sort_keys=True))
