"""AT-05 focused tests: real position -> stop-loss -> NM-08 -> NM-10 paper exit.

The wallet, portfolio, risk, strategy, decision and execution stores are real.
Only the market/forecast providers are deterministic fixtures (the same ones the
accepted AT-04 suite uses), so exit fills, position quantities and ledger
reconciliation are authoritative rather than mocked.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).parent))
from test_novamarket_at04 import NOW, ROOT, ExecutionHarness  # noqa: E402

from novacontrol.api.models import ApiSurface  # noqa: E402
from novacontrol.api.route_consumers import ROUTE_CONSUMERS  # noqa: E402
from novacontrol.novamarket.autonomy_exits import (  # noqa: E402
    AutonomyExitSupervisor,
    ExitStatus,
    InvalidExitConfiguration,
    build_exit_configuration,
)
from novacontrol.novamarket.execution import OrderSide, OrderStatus  # noqa: E402


class ExitHarness(ExecutionHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.supervisor: AutonomyExitSupervisor | None = None
        self.learning_calls: list[dict[str, Any]] = []

    async def asyncTearDown(self) -> None:
        if self.supervisor is not None:
            self.supervisor.close()
        await super().asyncTearDown()

    def portfolio_view(self, portfolio_id: str = "", limit: int = 200) -> Any:
        if portfolio_id:
            return self.portfolios.detail(portfolio_id)
        return self.portfolios.portfolios(limit=limit)

    def learning_record(self, evidence: dict[str, Any]) -> dict[str, Any]:
        self.learning_calls.append(dict(evidence))
        return {"ok": True, "duplicate": False, "evidence": {"status": "rejected"}}

    def make_supervisor(self, **kwargs: Any) -> AutonomyExitSupervisor:
        if self.supervisor is not None:
            self.supervisor.close()
        self.supervisor = AutonomyExitSupervisor(
            runtime=self.runtime,
            portfolios=self.portfolio_view,
            market=self.market,
            risk=self.risk,
            execution=self.execution,
            decisions=self.decisions,
            learning_recorder=self.learning_record,
            clock=self.clock,
            stale_after_seconds=300,
            **kwargs,
        )
        return self.supervisor

    async def enter_position(self) -> dict[str, Any]:
        result = await self.run_execution()
        self.assertEqual(result["status"], "completed", result)
        execution = result["executions"][0]
        self.assertEqual(execution["order"]["status"], "filled", execution)
        detail = self.portfolios.detail(self.portfolio_id)
        self.assertTrue(detail["holdings"])
        return execution

    def holding(self) -> dict[str, Any] | None:
        detail = self.portfolios.detail(self.portfolio_id)
        return next(
            (row for row in detail["holdings"]
             if row["instrument_id"] == "XNSE:ALPHA"),
            None,
        )

    def crash_price(self, factor: str = "0.97") -> Decimal:
        holding = self.holding()
        self.assertIsNotNone(holding)
        price = (Decimal(str(holding["average_cost"])) * Decimal(factor)).quantize(
            Decimal("0.01")
        )
        self.market.closes[-1] = float(price)
        self.market.observed_at = NOW
        return price

    def configure_exit(
        self,
        *,
        target: Decimal | None = None,
        trailing_percent: Decimal | None = None,
    ) -> dict[str, Any]:
        holding = self.holding()
        self.assertIsNotNone(holding)
        body = build_exit_configuration(
            self.portfolio_id,
            "XNSE:ALPHA",
            position_id=str(holding.get("position_id") or holding.get("id") or ""),
            target_price=target,
            trailing_stop_percent=trailing_percent,
            average_cost=holding["average_cost"],
        )
        return self.execution_store.save_exit_configuration(body)

    def set_price(self, factor: str) -> Decimal:
        holding = self.holding()
        self.assertIsNotNone(holding)
        price = (Decimal(str(holding["average_cost"])) * Decimal(factor)).quantize(
            Decimal("0.01")
        )
        self.market.closes[-1] = float(price)
        self.market.observed_at = NOW
        return price

    def sell_orders(self) -> list[Any]:
        return [
            order for order in self.execution_store.orders(limit=100)
            if order.side is OrderSide.SELL
        ]

    def buy_orders(self) -> list[Any]:
        return [
            order for order in self.execution_store.orders(limit=100)
            if order.side is OrderSide.BUY
        ]


class PrerequisiteAndContractTests(unittest.TestCase):
    def test_prior_reports_are_pass(self) -> None:
        for relative in (
            "docs/AT-01_IMPLEMENTATION_REPORT.md",
            "docs/AUTONOMOUS_TRADING_AT02_REPORT.md",
            "AUTONOMOUS_TRADING_AT03_REPORT.md",
            "AUTONOMOUS_TRADING_AT04_REPORT.md",
        ):
            body = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("PASS", body, relative)

    def test_contract_supports_target_and_trailing_and_is_paper_only(self) -> None:
        supervisor = AutonomyExitSupervisor(
            runtime=SimpleNamespace(status=lambda: {"mode": "disabled"}),
            portfolios=lambda **_: {"portfolios": []},
            market=SimpleNamespace(),
            risk=SimpleNamespace(),
            execution=SimpleNamespace(store=SimpleNamespace()),
            decisions=SimpleNamespace(),
        )
        contract = supervisor.contract()
        self.assertTrue(contract["paper_only"])
        self.assertFalse(contract["live_trading"])
        self.assertFalse(contract["broker_connected"])
        self.assertEqual(contract["risk_authority"], "NM-08")
        self.assertEqual(contract["execution_authority"], "NM-10")
        self.assertEqual(
            contract["supported_exit_conditions"],
            ["stop_loss", "target_price", "trailing_stop"],
        )
        self.assertEqual(contract["unsupported_exit_conditions"], [])
        self.assertEqual(contract["supported_directions"], ["long"])
        self.assertFalse(contract["learning"]["automatic_promotion"])

    def test_routes_are_in_api_contract_and_have_consumers(self) -> None:
        routes = {(route.method, route.path) for route in ApiSurface.default().routes}
        expected = (
            ("GET", "/market/autonomy/exits/contracts"),
            ("GET", "/market/autonomy/exits/status"),
            ("GET", "/market/autonomy/exits/positions"),
            ("GET", "/market/autonomy/exits/configurations"),
            ("PUT", "/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}"),
            ("DELETE", "/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}"),
            ("GET", "/market/autonomy/exits/decisions"),
            ("GET", "/market/autonomy/exits/decisions/{decision_id}"),
            ("GET", "/market/autonomy/exits/decisions/{decision_id}/events"),
            ("GET", "/market/autonomy/exits/learning"),
            ("POST", "/market/autonomy/exits/run"),
            ("POST", "/market/autonomy/exits/decisions/{decision_id}/reconcile"),
        )
        for route in expected:
            self.assertIn(route, routes)
            self.assertIn(route, ROUTE_CONSUMERS)
            self.assertEqual(ROUTE_CONSUMERS[route], "no-render")

    def test_no_direct_financial_write_or_broker_path(self) -> None:
        source = (ROOT / "src/novacontrol/novamarket/autonomy_exits/pipeline.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        forbidden = ("broker", "angel", "zerodha", "upstox", "kite", "live_order")
        self.assertFalse(any(part in name.lower() for name in imports for part in forbidden))
        for name in (
            "record_fill",
            "apply_fill",
            "deposit",
            "withdraw",
            "update_position",
            "set_balance",
        ):
            self.assertNotIn(name, source)
        self.assertIn("execution.submit", source)
        self.assertIn("execution.preflight", source)
        self.assertIn("risk.assess", source)


class StopLossTests(ExitHarness):
    async def test_stop_loss_full_exit_reconciles_position_cash_and_ledger(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        expected_stop = (average_cost * Decimal("0.99")).quantize(Decimal("0.01"))
        quantity = Decimal(str(self.holding()["quantity"]))
        trigger = self.crash_price()
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="test")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["submitted"], 1, result)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.FILLED.value, decision)
        self.assertEqual(decision["condition"], "stop_loss")
        self.assertEqual(decision["trigger_value"], str(expected_stop))
        self.assertEqual(Decimal(decision["observed_price"]), trigger)
        self.assertTrue(decision["position_closed"], decision)
        self.assertEqual(decision["reconciliation_status"], "passed", decision)
        orders = self.sell_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].status, OrderStatus.FILLED)
        self.assertLessEqual(orders[0].requested_quantity, quantity)
        fills = self.execution_store.fills(orders[0].order_id)
        self.assertEqual(len(fills), 1)
        self.assertTrue(fills[0].ledger_transaction_ids)
        self.assertIsNone(self.holding())
        # One authoritative buy order and one authoritative sell order only.
        self.assertEqual(len(self.buy_orders()), 1)
        self.assertEqual(len(self.execution_store.orders(limit=100)), 2)

    async def test_learning_evidence_links_entry_to_exit_and_never_promotes(self) -> None:
        entry = await self.enter_position()
        self.crash_price()
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="test")
        evidence = self.execution_store.learning_evidence()
        self.assertEqual(len(evidence), 1)
        row = evidence[0]
        self.assertEqual(row["status"], "complete", row)
        self.assertEqual(row["attribution_status"], "complete")
        self.assertFalse(row["auto_promoted"])
        self.assertFalse(row["strategy_modified"])
        self.assertTrue(row["paper_only"])
        self.assertEqual(row["entry"]["order_id"], entry["order"]["order_id"])
        self.assertTrue(row["exit"]["order_id"].startswith("ord_"))
        self.assertTrue(row["exit"]["fills"])
        self.assertEqual(row["exit"]["condition"], "stop_loss")
        self.assertTrue(row["realized"]["source"])
        self.assertIn("authoritative", row["realized"]["source"])
        self.assertEqual(self.learning_calls[0]["decision_id"], row["decision_id"])
        self.assertIn("learning_service", row)
        self.assertTrue(row["learning_service"]["recorded"])

    async def test_partial_fill_is_reported_and_suppresses_a_duplicate_exit(self) -> None:
        await self.enter_position()
        self.crash_price()
        self.market.volume = 10.0
        supervisor = self.make_supervisor()
        first = await supervisor.run_cycle(trigger="test")
        self.assertEqual(first["submitted"], 1, first)
        self.assertEqual(len(self.sell_orders()), 1)
        order = self.sell_orders()[0]
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.PARTIALLY_FILLED.value)
        self.assertFalse(decision["position_closed"])
        self.assertGreater(Decimal(decision["remaining_quantity"]), 0)
        self.assertGreater(Decimal(decision["filled_quantity"]), 0)
        evidence = self.execution_store.learning_evidence()
        self.assertEqual(evidence[0]["status"], "partial", evidence[0])
        self.assertIn("remaining position quantity", evidence[0]["missing_evidence"])
        second = await supervisor.run_cycle(trigger="duplicate")
        self.assertEqual(second["submitted"], 0, second)
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)

    async def test_restart_reconciles_pending_partial_without_second_order(self) -> None:
        await self.enter_position()
        self.crash_price()
        self.market.volume = 10.0
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="first")
        order_id = self.sell_orders()[0].order_id
        supervisor.close()
        restarted = self.make_supervisor()
        result = await restarted.run_cycle(trigger="restart")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(self.sell_orders()[0].order_id, order_id)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)

    async def test_repeated_trigger_and_concurrent_cycles_share_one_order(self) -> None:
        await self.enter_position()
        self.crash_price()
        supervisor = self.make_supervisor()
        first, second = await asyncio.gather(
            supervisor.run_cycle(trigger="concurrent-a"),
            supervisor.run_cycle(trigger="concurrent-b"),
        )
        statuses = {first.get("status"), second.get("status")}
        self.assertIn("completed", statuses)
        self.assertTrue("skipped" in statuses or second.get("status") == "completed")
        self.assertEqual(len(self.sell_orders()), 1)
        third = await supervisor.run_cycle(trigger="again")
        self.assertIn(third["status"], {"completed", "skipped"})
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)


class ModeTests(ExitHarness):
    async def test_monitoring_records_trigger_without_an_order_then_autonomous_submits(
        self,
    ) -> None:
        await self.enter_position()
        self.runtime.pause(reason="test monitoring")
        self.runtime.resume("monitoring")
        self.crash_price()
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="monitoring")
        self.assertEqual(result["triggered"], 1, result)
        self.assertEqual(result["submitted"], 0, result)
        self.assertEqual(self.sell_orders(), [])
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.MONITORING.value)
        self.runtime.pause(reason="switch to autonomous")
        self.runtime.resume("autonomous", acknowledge_paper_only=True)
        second = await supervisor.run_cycle(trigger="autonomous")
        self.assertEqual(second["submitted"], 1, second)
        self.assertEqual(self.execution_store.exit_decisions()[0]["status"],
                         ExitStatus.FILLED.value)
        self.assertEqual(len(self.sell_orders()), 1)

    async def test_paused_mode_blocks_entries_but_submits_protective_exit(self) -> None:
        await self.enter_position()
        self.runtime.pause(reason="test paused exit")
        self.crash_price()
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="paused")
        self.assertEqual(result["mode"], "paused")
        self.assertEqual(result["submitted"], 1, result)
        self.assertEqual(self.execution_store.exit_decisions()[0]["status"],
                         ExitStatus.FILLED.value)
        # The entry pipeline refuses a paused runtime.
        with self.assertRaises(Exception):
            await self.decisions.run_cycle(
                monitoring_cycle_id=self.cycle_id, trigger="paused", actor="test"
            )

    async def test_disabled_mode_performs_no_supervision_or_order_action(self) -> None:
        await self.enter_position()
        self.runtime.stop(reason="test disabled")
        self.crash_price()
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="disabled")
        self.assertEqual(result["status"], "skipped", result)
        self.assertEqual(result["skipped_reason"], "runtime_disabled")
        self.assertEqual(self.sell_orders(), [])
        self.assertEqual(self.execution_store.exit_decisions(), ())

    async def test_expired_lease_blocks_submission(self) -> None:
        await self.enter_position()
        self.crash_price()
        self.clock.now += __import__("datetime").timedelta(seconds=3700)
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="expired-lease")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skipped_reason"], "lease_not_held_or_expired")
        self.assertEqual(self.sell_orders(), [])


class MarketDataTests(ExitHarness):
    async def test_stale_market_data_never_triggers_an_exit(self) -> None:
        await self.enter_position()
        self.crash_price()
        self.market.observed_at = self.clock.now - __import__("datetime").timedelta(hours=2)
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="stale")
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(self.sell_orders(), [])
        self.assertEqual(self.execution_store.exit_decisions(), ())
        self.assertIn("stale_market_data", result["positions"][0]["skipped"])

    async def test_missing_or_unusable_market_data_fails_closed(self) -> None:
        await self.enter_position()
        self.crash_price()
        supervisor = self.make_supervisor()
        with mock.patch.object(
            self.market,
            "latest",
            return_value=SimpleNamespace(usable=False, selected=None),
        ):
            result = await supervisor.run_cycle(trigger="missing")
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(self.sell_orders(), [])
        self.assertEqual(self.execution_store.exit_decisions(), ())


class FailureTests(ExitHarness):
    async def test_timeout_after_nm10_commit_is_reconciled_without_duplicate(self) -> None:
        await self.enter_position()
        self.crash_price()
        delegate = self.execution

        class CommitThenTimeout:
            store = delegate.store
            config = delegate.config
            set_proposal_loader = delegate.set_proposal_loader

            async def preflight(self, *args: Any, **kwargs: Any) -> Any:
                return await delegate.preflight(*args, **kwargs)

            async def submit(self, *args: Any, **kwargs: Any) -> Any:
                await delegate.submit(*args, **kwargs)
                raise asyncio.TimeoutError("synthetic timeout after commit")

            async def reconcile(self, *args: Any, **kwargs: Any) -> Any:
                return await delegate.reconcile(*args, **kwargs)

        self.supervisor = AutonomyExitSupervisor(
            runtime=self.runtime,
            portfolios=self.portfolio_view,
            market=self.market,
            risk=self.risk,
            execution=CommitThenTimeout(),
            decisions=self.decisions,
            learning_recorder=self.learning_record,
            clock=self.clock,
        )
        result = await self.supervisor.run_cycle(trigger="timeout")
        self.assertEqual(result["submitted"], 1, result)
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(self.execution_store.exit_decisions()[0]["status"],
                         ExitStatus.FILLED.value)

    async def test_nm08_rejection_never_creates_an_nm10_order(self) -> None:
        await self.enter_position()
        self.crash_price()
        await self.risk.update_policy(
            "short_term", {"max_turnover": Decimal("0.0001")}, actor="at05-test"
        )
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="risk-reject")
        self.assertEqual(result["submitted"], 0)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.RISK_REJECTED.value)
        self.assertEqual(self.sell_orders(), [])
        self.assertNotEqual(decision["risk_outcome"], "approved")


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class ExitApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_API_TOKEN": "api-secret",
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": "operator-secret",
            },
            clear=False,
        )
        self._env.start()
        from novacontrol.api.app import create_app

        self.client = TestClient(create_app(data_dir=self._tmp.name))
        self.auth = {"Authorization": "Bearer api-secret"}

    def tearDown(self) -> None:
        self.client.close()
        self._env.stop()
        self._tmp.cleanup()

    def test_exit_reads_require_auth_and_contract_reports_supported_gap(self) -> None:
        for path in (
            "/market/autonomy/exits/contracts",
            "/market/autonomy/exits/status",
            "/market/autonomy/exits/positions",
            "/market/autonomy/exits/configurations",
            "/market/autonomy/exits/decisions",
            "/market/autonomy/exits/learning",
        ):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        response = self.client.get(
            "/market/autonomy/exits/contracts", headers=self.auth
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["paper_only"])
        self.assertFalse(body["live_trading"])
        self.assertEqual(
            body["supported_exit_conditions"],
            ["stop_loss", "target_price", "trailing_stop"],
        )
        self.assertEqual(body["unsupported_exit_conditions"], [])
        self.assertEqual(body["supported_directions"], ["long"])

    def test_exit_run_requires_the_dedicated_operator_token(self) -> None:
        response = self.client.post(
            "/market/autonomy/exits/run", headers=self.auth, json={"trigger": "test"}
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_configuration_write_requires_the_dedicated_operator_token(self) -> None:
        response = self.client.put(
            "/market/autonomy/exits/configurations/pfl_1/XNSE:ALPHA",
            headers=self.auth,
            json={"target_price": "120.00"},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_configuration_write_reaches_the_service_with_operator_auth(self) -> None:
        from novacontrol.novamarket import NovaMarketService

        expected = {
            "ok": True,
            "phase": "AT-05",
            "configuration": {
                "config_id": "xcfg_api",
                "target_price": "120.00",
                "trailing_stop_percent": "",
            },
            "paper_only": True,
            "live_trading": False,
        }
        with mock.patch.object(
            NovaMarketService,
            "set_autonomy_exit_configuration",
            return_value=expected,
        ) as setter:
            response = self.client.put(
                "/market/autonomy/exits/configurations/pfl_1/XNSE:ALPHA",
                headers={
                    **self.auth,
                    "X-Autonomy-Operator-Token": "operator-secret",
                },
                json={"target_price": "120.00"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["configuration"]["config_id"], "xcfg_api")
        setter.assert_called_once_with(
            "pfl_1", "XNSE:ALPHA", {"target_price": "120.00"}
        )



class TargetPriceTests(ExitHarness):
    async def test_target_not_reached_then_reached_creates_one_paper_exit(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        target = (average_cost * Decimal("1.05")).quantize(Decimal("0.01"))
        self.configure_exit(target=target)
        supervisor = self.make_supervisor()

        self.set_price("1.02")
        below = await supervisor.run_cycle(trigger="below-target")
        self.assertEqual(below["submitted"], 0, below)
        self.assertEqual(self.sell_orders(), [])
        self.assertEqual(self.execution_store.exit_decisions(), ())

        observed = self.set_price("1.05")
        reached = await supervisor.run_cycle(trigger="target")
        self.assertEqual(reached["status"], "completed", reached)
        self.assertEqual(reached["submitted"], 1, reached)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["condition"], "target_price", decision)
        self.assertEqual(decision["status"], ExitStatus.FILLED.value, decision)
        self.assertEqual(Decimal(decision["observed_price"]), observed)
        self.assertEqual(Decimal(decision["trigger_value"]), target)
        self.assertEqual(decision["reconciliation_status"], "passed")
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertIsNone(self.holding())

    async def test_target_gap_through_price_triggers_the_same_durable_exit(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        target = (average_cost * Decimal("1.05")).quantize(Decimal("0.01"))
        self.configure_exit(target=target)
        self.set_price("1.10")
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="gap-target")
        self.assertEqual(result["submitted"], 1, result)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["condition"], "target_price")
        self.assertGreater(Decimal(decision["observed_price"]), target)

    async def test_target_restart_does_not_duplicate_or_lose_the_decision(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        self.configure_exit(
            target=(average_cost * Decimal("1.05")).quantize(Decimal("0.01"))
        )
        self.set_price("1.05")
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="before-restart")
        order_id = self.sell_orders()[0].order_id
        supervisor.close()

        restarted = self.make_supervisor()
        result = await restarted.run_cycle(trigger="after-restart")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(self.sell_orders()[0].order_id, order_id)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)
        self.assertEqual(self.execution_store.exit_configurations()[0]["version"], 1)

    async def test_target_partial_fill_is_retained_and_never_oversells(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        self.configure_exit(
            target=(average_cost * Decimal("1.05")).quantize(Decimal("0.01"))
        )
        self.set_price("1.05")
        self.market.volume = 10.0
        supervisor = self.make_supervisor()
        first = await supervisor.run_cycle(trigger="target-partial")
        self.assertEqual(first["submitted"], 1, first)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.PARTIALLY_FILLED.value, decision)
        self.assertGreater(Decimal(decision["filled_quantity"]), 0)
        self.assertGreater(Decimal(decision["remaining_quantity"]), 0)
        second = await supervisor.run_cycle(trigger="target-partial-duplicate")
        self.assertEqual(second["submitted"], 0, second)
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)

    async def test_target_stale_market_data_fails_closed(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        self.configure_exit(
            target=(average_cost * Decimal("1.05")).quantize(Decimal("0.01"))
        )
        self.set_price("1.05")
        self.market.observed_at = self.clock.now - __import__("datetime").timedelta(
            hours=2
        )
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="target-stale")
        self.assertEqual(result["submitted"], 0, result)
        self.assertEqual(self.sell_orders(), [])
        self.assertEqual(self.execution_store.exit_decisions(), ())
        self.assertIn("stale_market_data", result["positions"][0]["skipped"])

    async def test_target_monitoring_records_then_autonomous_executes(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        self.configure_exit(
            target=(average_cost * Decimal("1.05")).quantize(Decimal("0.01"))
        )
        self.set_price("1.05")
        self.runtime.pause(reason="target monitoring")
        self.runtime.resume("monitoring")
        supervisor = self.make_supervisor()
        first = await supervisor.run_cycle(trigger="target-monitoring")
        self.assertEqual(first["submitted"], 0, first)
        self.assertEqual(self.sell_orders(), [])
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.MONITORING.value)
        self.assertEqual(decision["condition"], "target_price")
        self.runtime.pause(reason="target autonomous")
        self.runtime.resume("autonomous", acknowledge_paper_only=True)
        second = await supervisor.run_cycle(trigger="target-autonomous")
        self.assertEqual(second["submitted"], 1, second)
        self.assertEqual(len(self.sell_orders()), 1)

    async def test_target_nm08_rejection_creates_no_order(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        self.configure_exit(
            target=(average_cost * Decimal("1.05")).quantize(Decimal("0.01"))
        )
        self.set_price("1.05")
        await self.risk.update_policy(
            "short_term", {"max_turnover": Decimal("0.0001")}, actor="at05-target-test"
        )
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="target-risk-reject")
        self.assertEqual(result["submitted"], 0, result)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.RISK_REJECTED.value)
        self.assertEqual(self.sell_orders(), [])

    async def test_invalid_target_is_refused_without_a_configuration(self) -> None:
        await self.enter_position()
        average_cost = Decimal(str(self.holding()["average_cost"]))
        with self.assertRaises(InvalidExitConfiguration):
            self.configure_exit(target=average_cost)
        self.assertEqual(self.execution_store.exit_configurations(), ())


class TrailingStopTests(ExitHarness):
    async def test_high_water_only_rises_and_trailing_trigger_is_paper_reconciled(self) -> None:
        await self.enter_position()
        self.configure_exit(trailing_percent=Decimal("5"))
        supervisor = self.make_supervisor()

        self.set_price("1.08")
        first = await supervisor.run_cycle(trigger="trailing-high")
        self.assertEqual(first["submitted"], 0, first)
        config = self.execution_store.exit_configurations()[0]
        self.assertEqual(Decimal(config["high_water_mark"]), self.set_price("1.08"))
        self.assertEqual(
            Decimal(config["current_trailing_stop_price"]),
            (Decimal(config["high_water_mark"]) * Decimal("0.95")).quantize(
                Decimal("0.01")
            ),
        )

        self.set_price("1.07")
        second = await supervisor.run_cycle(trigger="trailing-fall")
        self.assertEqual(second["submitted"], 0, second)
        config = self.execution_store.exit_configurations()[0]
        self.assertEqual(Decimal(config["high_water_mark"]), self.set_price("1.08"))

        expected_high = Decimal(config["high_water_mark"])
        self.set_price("1.02")
        third = await supervisor.run_cycle(trigger="trailing-trigger")
        self.assertEqual(third["submitted"], 1, third)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["condition"], "trailing_stop", decision)
        self.assertEqual(decision["status"], ExitStatus.FILLED.value, decision)
        self.assertEqual(Decimal(decision["trailing_high_water"]), expected_high)
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertIsNone(self.holding())

    async def test_trailing_restart_preserves_high_water_and_suppresses_duplicate(self) -> None:
        await self.enter_position()
        self.configure_exit(trailing_percent=Decimal("5"))
        self.set_price("1.08")
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="high-before-restart")
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
        self.set_price("1.02")
        result = await restarted.run_cycle(trigger="trailing-trigger")
        self.assertEqual(result["submitted"], 1, result)
        self.assertEqual(len(self.sell_orders()), 1)
        self.assertEqual(len(self.execution_store.exit_decisions()), 1)

    async def test_trailing_stale_data_never_triggers(self) -> None:
        await self.enter_position()
        self.configure_exit(trailing_percent=Decimal("5"))
        self.set_price("1.10")
        self.market.observed_at = self.clock.now - __import__("datetime").timedelta(
            hours=2
        )
        supervisor = self.make_supervisor()
        result = await supervisor.run_cycle(trigger="trailing-stale")
        self.assertEqual(result["submitted"], 0, result)
        self.assertEqual(self.sell_orders(), [])
        self.assertEqual(self.execution_store.exit_decisions(), ())

    async def test_trailing_partial_fill_is_retained_and_repeated_cycle_is_suppressed(self) -> None:
        await self.enter_position()
        self.configure_exit(trailing_percent=Decimal("5"))
        self.set_price("1.08")
        supervisor = self.make_supervisor()
        await supervisor.run_cycle(trigger="trailing-high")
        self.set_price("1.02")
        self.market.volume = 10.0
        first = await supervisor.run_cycle(trigger="trailing-partial")
        self.assertEqual(first["submitted"], 1, first)
        decision = self.execution_store.exit_decisions()[0]
        self.assertEqual(decision["status"], ExitStatus.PARTIALLY_FILLED.value)
        self.assertGreater(Decimal(decision["remaining_quantity"]), 0)
        second = await supervisor.run_cycle(trigger="trailing-partial-duplicate")
        self.assertEqual(second["submitted"], 0, second)
        self.assertEqual(len(self.sell_orders()), 1)

    async def test_invalid_trailing_percent_is_refused(self) -> None:
        await self.enter_position()
        with self.assertRaises(InvalidExitConfiguration):
            self.configure_exit(trailing_percent=Decimal("0"))
        with self.assertRaises(InvalidExitConfiguration):
            self.configure_exit(trailing_percent=Decimal("100"))
        self.assertEqual(self.execution_store.exit_configurations(), ())



if __name__ == "__main__":
    unittest.main()
