"""NM-10 paper-execution tests.

The market-data and strategy/risk seams are deterministic fakes; the wallet,
ledger, portfolio, execution and reconciliation stores are the real ones.  That
keeps the accounting assertions meaningful while avoiding network access.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from novacontrol.novamarket.execution import (
    ExecutionConfig,
    ExecutionEngine,
    ExecutionStore,
    ExecutionStoreError,
    Fill,
    OrderStatus,
    OrderType,
)
from novacontrol.novamarket.portfolio.models import (
    PortfolioHorizon,
    PortfolioRecord,
    PortfolioStatus,
    Position,
)
from novacontrol.novamarket.portfolio.store import PortfolioStore
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class _Market:
    def __init__(self, *, price: str = "100", volume: str = "100000", stale: bool = False) -> None:
        self.price = Decimal(price)
        self.volume = Decimal(volume)
        self.stale = stale
        self.quality = "valid"
        self.usable = True
        self.instrument = SimpleNamespace(
            instrument_id="XNSE:TEST",
            currency="INR",
            asset_class=SimpleNamespace(value="equity"),
            trading_status=SimpleNamespace(value="open"),
            metadata={"sector": "Technology"},
        )

    def get_instrument(self, instrument_id: str) -> Any:
        return self.instrument

    def latest(self, instrument_id: str) -> Any:
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            usable=self.usable,
            selected=SimpleNamespace(
                price=self.price,
                observed_at=now - timedelta(seconds=10),
                freshness=SimpleNamespace(value="stale" if self.stale else "fresh"),
                quality=SimpleNamespace(value=self.quality),
            ),
        )

    def bars(self, instrument_id: str, limit: int = 30) -> Any:
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    close=self.price,
                    volume=self.volume,
                    partial=False,
                    observed_at=datetime.now(timezone.utc) - timedelta(seconds=20),
                )
            ]
        )


class _Strategy:
    def __init__(self, proposal: dict[str, Any]) -> None:
        self._proposal = proposal

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        if proposal_id != self._proposal["proposal_id"]:
            raise KeyError(proposal_id)
        return dict(self._proposal)


class _RiskStore:
    def __init__(self, decision: dict[str, Any]) -> None:
        self._decision = decision

    def decision(self, decision_id: str) -> dict[str, Any] | None:
        return dict(self._decision) if decision_id == self._decision["decision_id"] else None


class _Risk:
    def __init__(self, decision: dict[str, Any]) -> None:
        self.store = _RiskStore(decision)
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def assess(self, action: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"action": action, **kwargs})
        result = dict(self.decision)
        result["action"] = action
        return result


class _Portfolios:
    def __init__(self, store: PortfolioStore, record: PortfolioRecord) -> None:
        self.store = store
        self.record = record

    def detail(self, portfolio_id: str) -> dict[str, Any]:
        return {"portfolio": self.record.to_dict()}


class ExecutionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "execution.sqlite3"
        self.clock = _Clock()
        self.wallet_store = WalletStore(self.path, clock=self.clock)
        self.wallets = WalletEngine(self.wallet_store, clock=self.clock)
        self.wallet_store.create_wallet(
            "novamarket", wallet_id="pfw_test", label="test portfolio"
        )
        await self.wallets.deposit(
            "pfw_test", "1000000", idempotency_key="fund-test", reference="test"
        )
        self.portfolio_store = PortfolioStore(self.path, clock=self.clock)
        now = self.clock.now
        self.record = PortfolioRecord(
            portfolio_id="pf_test",
            name="Test",
            horizon=PortfolioHorizon.SHORT,
            funding_wallet_id="pfw_test",
            wallet_id="pfw_test",
            initial_capital=Decimal("1000000"),
            status=PortfolioStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self.portfolio_store.create_portfolio(self.record)
        self.portfolios = _Portfolios(self.portfolio_store, self.record)
        self.decision = {
            "decision_id": "rdec_test",
            "outcome": "approved",
            "action": "add_position",
            "portfolio_id": "pf_test",
            "proposal_id": "prop_test",
            "policy_version": "1.0.0",
            "state_version": "sv1",
            "expires_at": (now + timedelta(hours=2)).isoformat(),
            "restrictions": [],
        }
        self.proposal = {
            "proposal_id": "prop_test",
            "strategy_id": "strat_test",
            "portfolio_id": "pf_test",
            "instrument_id": "XNSE:TEST",
            "action": "buy",
            "side": "buy",
            "notional": "10000",
            "reference_price": "100",
            "status": "approved",
            "risk_outcome": "approved",
            "risk_decision_id": "rdec_test",
            "risk_state_version": "sv1",
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "reason": "test",
        }
        self.market = _Market()
        self.strategies = _Strategy(self.proposal)
        self.risk = _Risk(self.decision)
        self.store = ExecutionStore(self.path, clock=self.clock)
        self.engine = ExecutionEngine(
            self.store,
            wallets=self.wallets,
            portfolios=self.portfolios,
            risk=self.risk,
            strategies=self.strategies,
            market_data=self.market,
            clock=self.clock,
            config=ExecutionConfig(
                participation_rate=Decimal("1"),
                spread_bps=Decimal("0"),
                impact_coefficient_bps=Decimal("0"),
                maximum_slippage_bps=Decimal("150"),
            ),
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.wallet_store.close()
        self.portfolio_store.close()
        self.tmp.cleanup()

    async def test_market_buy_creates_order_fill_and_ledger_backed_position(self) -> None:
        result = await self.engine.submit("prop_test")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["order"]["status"], "filled")
        fill = result["fills"][0]
        self.assertEqual(fill["quantity"], "100.0000")
        self.assertGreater(float(fill["total_fees"]), 0)
        self.assertTrue(fill["ledger_transaction_ids"])
        position = self.portfolio_store.position("pf_test", "XNSE:TEST")
        self.assertIsNotNone(position)
        self.assertEqual(position.quantity, Decimal("100.0000"))
        self.assertEqual(len(self.store.fills()), 1)

    async def test_duplicate_submission_replays_and_never_creates_a_second_order(self) -> None:
        first = await self.engine.submit("prop_test")
        second = await self.engine.submit("prop_test")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["order"]["order_id"], second["order"]["order_id"])
        self.assertEqual(len(self.store.orders()), 1)
        self.assertEqual(len(self.store.fills()), 1)

    async def test_rejected_proposal_never_reaches_an_order(self) -> None:
        self.proposal["status"] = "pending_risk"
        result = await self.engine.submit("prop_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["order"]["status"], "rejected")
        self.assertEqual(self.portfolio_store.position("pf_test", "XNSE:TEST"), None)
        self.assertEqual(len(self.store.fills()), 0)

    async def test_stale_data_refuses_execution(self) -> None:
        self.market.stale = True
        result = await self.engine.submit("prop_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["order"]["status"], "rejected")
        self.assertIn("fresh", str(result["error"]).lower())

    async def test_limit_order_does_not_fill_above_limit(self) -> None:
        self.market.price = Decimal("110")
        result = await self.engine.submit(
            "prop_test", order_type=OrderType.LIMIT, limit_price="100"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["order"]["status"], "open")
        self.assertEqual(result["fills"], [])

    async def test_stop_order_waits_for_trigger(self) -> None:
        self.market.price = Decimal("90")
        result = await self.engine.submit(
            "prop_test", order_type=OrderType.STOP, trigger_price="90.5", quantity="10"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["order"]["status"], "open")
        self.market.price = Decimal("90.6")
        processed = await self.engine.process(result["order"]["order_id"])
        self.assertEqual(processed["order"]["status"], "filled")
        self.assertEqual(len(processed["fills"]), 1)

    async def test_partial_fill_uses_available_volume_and_preserves_rest(self) -> None:
        self.market.volume = Decimal("20")
        result = await self.engine.submit("prop_test")
        self.assertEqual(result["order"]["status"], "partially_filled")
        self.assertEqual(result["order"]["filled_quantity"], "20.0000")
        self.assertEqual(result["order"]["remaining_quantity"], "80.0000")
        cancelled = await self.engine.cancel(
            result["order"]["order_id"], reason="test cancel"
        )
        self.assertEqual(cancelled["order"]["status"], "cancelled")
        self.assertEqual(len(cancelled["fills"]), 1)
        self.assertEqual(self.portfolio_store.position("pf_test", "XNSE:TEST").quantity, Decimal("20.0000"))

    async def test_sell_preserves_remaining_position_and_ledger(self) -> None:
        bought = await self.engine.submit("prop_test")
        self.assertEqual(bought["order"]["status"], "filled")
        self.proposal["proposal_id"] = "prop_sell"
        self.proposal["action"] = "sell"
        self.proposal["side"] = "sell"
        self.proposal["notional"] = "5000"
        self.decision["proposal_id"] = "prop_sell"
        self.decision["action"] = "reduce_position"
        self.strategies._proposal = dict(self.proposal)
        sold = await self.engine.submit("prop_sell")
        self.assertTrue(sold["ok"], sold)
        self.assertEqual(sold["order"]["status"], "filled")
        position = self.portfolio_store.position("pf_test", "XNSE:TEST")
        self.assertEqual(position.quantity, Decimal("50.0000"))
        # Selling at the same reference price is still a loss after simulated fees.
        self.assertLess(position.realized_pnl, Decimal("0"))

    async def test_reconciliation_passes_for_a_clean_fill(self) -> None:
        result = await self.engine.submit("prop_test")
        report = await self.engine.reconcile(result["order"]["order_id"])
        self.assertTrue(report["ok"], report)

    async def test_reconciliation_survives_a_store_round_trip(self) -> None:
        result = await self.engine.submit("prop_test")
        report = await self.engine.reconcile(result["order"]["order_id"])
        stored = self.engine.reconciliations(limit=5)["reconciliations"]
        self.assertTrue(stored)
        self.assertEqual(stored[0]["reconciliation_id"], report["reconciliation_id"])
        self.assertRegex(stored[0]["created_at"], r"\+00:00$")

    async def test_reconciliation_detects_an_overfilled_order(self) -> None:
        result = await self.engine.submit("prop_test")
        order = self.store.order(result["order"]["order_id"])
        assert order is not None
        # Corrupt the read model only; the immutable fill remains the evidence.
        broken = type(order)(
            **{
                **{name: getattr(order, name) for name in order.__dataclass_fields__},
                "filled_quantity": Decimal("101"),
                "remaining_quantity": Decimal("0"),
            }
        )
        self.store.save_order(broken)
        report = await self.engine.reconcile(order.order_id)
        self.assertFalse(report["ok"])
        self.assertTrue(any(item["kind"] in {"filled_quantity_mismatch", "overfilled_order"} for item in report["issues"]))

    async def test_cancel_releases_reservation_and_never_erases_fills(self) -> None:
        await self.engine.submit("prop_test")
        self.proposal["proposal_id"] = "prop_limit"
        self.proposal["action"] = "buy"
        self.proposal["side"] = "buy"
        self.proposal["notional"] = "1000"
        self.decision["proposal_id"] = "prop_limit"
        self.decision["action"] = "add_position"
        self.strategies._proposal = dict(self.proposal)
        self.market.price = Decimal("110")
        result = await self.engine.submit(
            "prop_limit", order_type=OrderType.LIMIT, limit_price="100"
        )
        self.assertEqual(result["order"]["status"], "open")
        cancelled = await self.engine.cancel(result["order"]["order_id"])
        self.assertEqual(cancelled["order"]["status"], "cancelled")
        self.assertEqual(self.store.reserved_total("pf_test", "cash"), Decimal("0.00"))

    async def test_unsupported_order_type_is_explicit(self) -> None:
        with self.assertRaises(Exception) as caught:
            await self.engine.submit("prop_test", order_type="trailing_stop")
        self.assertIn("unsupported", str(caught.exception).lower())

    async def test_stop_limit_requires_trigger_and_limit(self) -> None:
        self.market.price = Decimal("90")
        result = await self.engine.submit(
            "prop_test",
            order_type=OrderType.STOP_LIMIT,
            trigger_price="95",
            limit_price="100",
            quantity="10",
        )
        self.assertEqual(result["order"]["status"], "open")
        self.market.price = Decimal("99")
        processed = await self.engine.process(result["order"]["order_id"])
        self.assertEqual(processed["order"]["status"], "filled")
        self.assertLessEqual(Decimal(processed["fills"][0]["price"]), Decimal("100"))

    async def test_expired_order_releases_reservation_and_keeps_no_fill(self) -> None:
        expiry = self.clock.now + timedelta(minutes=1)
        result = await self.engine.submit("prop_test", execute=False, expires_at=expiry)
        self.assertEqual(result["order"]["status"], "open")
        self.clock.now += timedelta(minutes=2)
        expired = await self.engine.process(result["order"]["order_id"])
        self.assertEqual(expired["order"]["status"], "expired")
        self.assertEqual(expired["fills"], [])
        self.assertEqual(self.store.reserved_total("pf_test", "cash"), Decimal("0.00"))

    async def test_accepted_order_can_expire_before_it_opens(self) -> None:
        result = await self.engine.submit("prop_test", execute=False)
        order = self.store.order(result["order"]["order_id"])
        assert order is not None
        accepted = replace(
            order,
            status=OrderStatus.ACCEPTED,
            expires_at=self.clock.now - timedelta(seconds=1),
            updated_at=self.clock.now,
        )
        self.store.save_order(accepted)
        expired = await self.engine.process(accepted.order_id)
        self.assertEqual(expired["order"]["status"], "expired")
        self.assertEqual(expired["fills"], [])

    async def test_risk_restrictions_fail_closed(self) -> None:
        self.decision["restrictions"] = ["reduce the requested notional"]
        result = await self.engine.submit("prop_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["order"]["status"], "rejected")
        self.assertIn("restriction", str(result["error"]).lower())

    async def test_multiple_partial_fills_sum_to_the_requested_quantity(self) -> None:
        self.market.volume = Decimal("10")
        first = await self.engine.submit("prop_test")
        self.assertEqual(first["order"]["status"], "partially_filled")
        self.assertEqual(first["order"]["filled_quantity"], "10.0000")
        self.market.volume = Decimal("1000")
        second = await self.engine.process(first["order"]["order_id"])
        self.assertEqual(second["order"]["status"], "filled")
        self.assertEqual(len(second["fills"]), 1)
        all_fills = self.store.fills(first["order"]["order_id"])
        self.assertEqual(
            sum((fill.quantity for fill in all_fills), Decimal("0")),
            Decimal(first["order"]["quantity"]),
        )

    async def test_fee_and_slippage_records_are_auditable(self) -> None:
        result = await self.engine.submit("prop_test")
        fill = result["fills"][0]
        self.assertEqual(fill["slippage_model_version"], "nm10-exec-v1")
        self.assertIn("brokerage", fill["fee_breakdown"])
        self.assertEqual(
            sum(Decimal(value) for key, value in fill["fee_breakdown"].items() if key != "total"),
            Decimal(fill["total_fees"]),
        )

    async def test_wallet_commit_before_position_failure_is_compensated(self) -> None:
        original = Decimal(
            self.wallets.wallet("pfw_test")["wallet"]["balance"]["total"]
        )
        with mock.patch.object(
            self.engine,
            "_apply_position",
            side_effect=RuntimeError("position store unavailable"),
        ):
            result = await self.engine.submit("prop_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["order"]["status"], "failed")
        self.assertEqual(self.store.fills(), ())
        self.assertIsNone(self.portfolio_store.position("pf_test", "XNSE:TEST"))
        self.assertEqual(
            Decimal(self.wallets.wallet("pfw_test")["wallet"]["balance"]["total"]),
            original,
        )
        self.assertEqual(self.store.pending_inflight(), ())
        self.assertEqual(self.store.reserved_total("pf_test", "cash"), Decimal("0.00"))

    async def test_position_commit_before_execution_write_failure_is_compensated(self) -> None:
        original = Decimal(
            self.wallets.wallet("pfw_test")["wallet"]["balance"]["total"]
        )
        with mock.patch.object(
            self.store,
            "record_fill",
            side_effect=ExecutionStoreError("execution fill write failed"),
        ):
            result = await self.engine.submit("prop_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["order"]["status"], "failed")
        position = self.portfolio_store.position("pf_test", "XNSE:TEST")
        self.assertTrue(position is None or position.quantity == 0)
        self.assertEqual(
            Decimal(self.wallets.wallet("pfw_test")["wallet"]["balance"]["total"]),
            original,
        )
        kinds = {row["kind"] for row in self.portfolio_store.position_history("pf_test")}
        self.assertIn("paper_buy_reversal", kinds)
        self.assertEqual(self.store.pending_inflight(), ())

    async def test_pending_fill_is_reversed_after_restart_recovery(self) -> None:
        original = Decimal(
            self.wallets.wallet("pfw_test")["wallet"]["balance"]["total"]
        )
        self.market.price = Decimal("110")
        opened = await self.engine.submit(
            "prop_test", order_type=OrderType.LIMIT, limit_price="100", execute=False
        )
        order = opened["order"]
        fill_id = "fill_recovery_test"
        fill = Fill(
            fill_id=fill_id,
            order_id=order["order_id"],
            instrument_id="XNSE:TEST",
            sequence=1,
            quantity="10",
            price="100",
            reference_price="100",
            slippage_amount="0",
            slippage_pct="0",
            slippage_model_version=self.engine.config.version,
            fee_breakdown={"total": "0.00"},
            total_fees="0",
            gross_value="1000",
            net_cash="1000",
            status="recorded",
            market_reference={},
            executed_at=self.clock.now,
            ledger_transaction_ids=(),
        )
        self.store.save_inflight(fill)
        await self.engine._ensure_clearing_wallet()
        await self.wallets.transfer(
            "pfw_test",
            "pfw_market_clearing",
            "1000",
            idempotency_key=f"{fill_id}:gross",
            reference=fill_id,
        )
        restarted = ExecutionEngine(
            self.store,
            wallets=self.wallets,
            portfolios=self.portfolios,
            risk=self.risk,
            strategies=self.strategies,
            market_data=self.market,
            clock=self.clock,
            config=self.engine.config,
        )
        report = await restarted.recover_pending()
        self.assertTrue(report["ok"], report)
        recovered = self.store.order(order["order_id"])
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.status, OrderStatus.FAILED)
        self.assertEqual(self.store.fills(), ())
        self.assertEqual(self.store.pending_inflight(), ())
        self.assertEqual(
            Decimal(self.wallets.wallet("pfw_test")["wallet"]["balance"]["total"]),
            original,
        )

    async def test_risk_revalidation_uses_the_actual_order_notional(self) -> None:
        result = await self.engine.submit("prop_test", quantity="120")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.risk.calls[0]["notional"], Decimal("12000.00"))

    async def test_unusable_market_quality_is_refused(self) -> None:
        self.market.quality = "suspect"
        result = await self.engine.submit("prop_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["order"]["status"], "rejected")
        self.assertIn("quality", str(result["error"]).lower())

    async def test_unusable_latest_reconciliation_falls_back_only_when_configured(self) -> None:
        self.market.usable = False
        result = await self.engine.submit("prop_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["order"]["status"], "rejected")
        self.assertIn("latest", str(result["error"]).lower())


if __name__ == "__main__":
    unittest.main()
