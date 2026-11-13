"""Independent NM-10 reconciliation.

Reconciliation reads orders, fills, reservations, portfolio positions and the
ledger; it never repairs them.  Every discrepancy is retained as evidence in a
persisted reconciliation record and published after that record is committed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from typing import Any, Mapping

from novacontrol.novamarket.execution.config import ExecutionConfig
from novacontrol.novamarket.execution.models import (
    TERMINAL_STATUSES,
    Order,
    OrderSide,
    OrderStatus,
    ReconciliationResult,
    execution_id,
    money,
    quantity,
    text,
)
from novacontrol.novamarket.execution.pricing import fee_breakdown
from novacontrol.novamarket.execution.store import ExecutionStore


class ExecutionReconciler:
    def __init__(
        self,
        store: ExecutionStore,
        *,
        wallets: Any,
        portfolios: Any,
        config: ExecutionConfig,
        clock: Any | None = None,
    ) -> None:
        self._store = store
        self._wallets = wallets
        self._portfolios = portfolios
        self._config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, order_id: str = "") -> ReconciliationResult:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        orders = [self._store.order(order_id)] if order_id else list(self._store.orders(limit=1000))
        orders = [order for order in orders if order is not None]
        issues: list[dict[str, Any]] = []
        checks: dict[str, Any] = {
            "orders_checked": len(orders),
            "fills_checked": 0,
            "ledger_transactions_checked": 0,
            "wallets_checked": 0,
            "positions_checked": 0,
        }
        seen_fill_ids: set[str] = set()
        wallet_reports: dict[str, Any] = {}
        for order in orders:
            fills = list(self._store.fills(order.order_id))
            checks["fills_checked"] += len(fills)
            filled = sum((fill.quantity for fill in fills), Decimal("0"))
            if quantity(filled) != quantity(order.filled_quantity):
                issues.append(
                    self._issue(
                        "filled_quantity_mismatch",
                        order,
                        expected=text(order.filled_quantity),
                        actual=text(filled),
                    )
                )
            if filled > order.requested_quantity:
                issues.append(
                    self._issue(
                        "overfilled_order",
                        order,
                        requested=text(order.requested_quantity),
                        filled=text(filled),
                    )
                )
            remaining = order.requested_quantity - filled
            if quantity(remaining) != quantity(order.remaining_quantity):
                issues.append(
                    self._issue(
                        "remaining_quantity_mismatch",
                        order,
                        expected=text(remaining),
                        actual=text(order.remaining_quantity),
                    )
                )
            if filled > 0 and filled == order.requested_quantity and order.status is not OrderStatus.FILLED:
                issues.append(
                    self._issue(
                        "fully_filled_order_not_marked_filled",
                        order,
                        actual_status=order.status.value,
                    )
                )
            if filled == 0 and order.status is OrderStatus.FILLED:
                issues.append(
                    self._issue("filled_order_has_no_fills", order, actual_status=order.status.value)
                )
            if order.status in TERMINAL_STATUSES:
                active = self._store.reservations(order_id=order.order_id, status="active")
                if active:
                    issues.append(
                        self._issue(
                            "terminal_order_has_active_reservation",
                            order,
                            reservations=[item.reservation_id for item in active],
                        )
                    )
            for fill in fills:
                if fill.fill_id in seen_fill_ids:
                    issues.append(
                        self._issue(
                            "duplicate_fill",
                            order,
                            fill_id=fill.fill_id,
                        )
                    )
                seen_fill_ids.add(fill.fill_id)
                if fill.order_id != order.order_id:
                    issues.append(
                        self._issue(
                            "fill_order_mismatch",
                            order,
                            fill_id=fill.fill_id,
                            actual_order_id=fill.order_id,
                        )
                    )
                expected_gross = money(fill.quantity * fill.price)
                if fill.gross_value != expected_gross:
                    issues.append(
                        self._issue(
                            "gross_value_mismatch",
                            order,
                            fill_id=fill.fill_id,
                            expected=text(expected_gross),
                            actual=text(fill.gross_value),
                        )
                    )
                expected_net = (
                    money(fill.gross_value + fill.total_fees)
                    if order.side is OrderSide.BUY
                    else money(fill.gross_value - fill.total_fees)
                )
                if fill.net_cash != expected_net:
                    issues.append(
                        self._issue(
                            "net_cash_mismatch",
                            order,
                            fill_id=fill.fill_id,
                            expected=text(expected_net),
                            actual=text(fill.net_cash),
                        )
                    )
                components = {
                    key: value for key, value in fill.fee_breakdown.items() if key != "total"
                }
                if sum(components.values(), Decimal("0")) != fill.total_fees:
                    issues.append(
                        self._issue(
                            "fee_breakdown_mismatch",
                            order,
                            fill_id=fill.fill_id,
                            expected=text(sum(components.values(), Decimal("0"))),
                            actual=text(fill.total_fees),
                        )
                    )
                if fill.slippage_model_version == self._config.version:
                    expected_fees = fee_breakdown(fill.gross_value, order.side, self._config)
                    if expected_fees["total"] != fill.total_fees:
                        issues.append(
                            self._issue(
                                "fee_recalculation_mismatch",
                                order,
                                fill_id=fill.fill_id,
                                expected=text(expected_fees["total"]),
                                actual=text(fill.total_fees),
                            )
                        )
                expected_slip = money(abs(fill.price - fill.reference_price) * fill.quantity)
                if expected_slip != fill.slippage_amount:
                    issues.append(
                        self._issue(
                            "slippage_mismatch",
                            order,
                            fill_id=fill.fill_id,
                            expected=text(expected_slip),
                            actual=text(fill.slippage_amount),
                        )
                    )
                if not fill.ledger_transaction_ids:
                    issues.append(
                        self._issue(
                            "missing_ledger_reference",
                            order,
                            fill_id=fill.fill_id,
                        )
                    )
                for transaction_id in fill.ledger_transaction_ids:
                    checks["ledger_transactions_checked"] += 1
                    try:
                        self._wallets.store.transaction(transaction_id)
                    except Exception as exc:  # noqa: BLE001
                        issues.append(
                            self._issue(
                                "missing_ledger_entry",
                                order,
                                fill_id=fill.fill_id,
                                transaction_id=transaction_id,
                                detail=f"{type(exc).__name__}: {exc}",
                            )
                        )
                try:
                    before = Decimal(str(fill.position_effect.get("quantity_before", "0")))
                    after = Decimal(str(fill.position_effect.get("quantity_after", "0")))
                except Exception as exc:  # noqa: BLE001 - corrupt evidence must be reported
                    issues.append(
                        self._issue(
                            "invalid_position_effect",
                            order,
                            fill_id=fill.fill_id,
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                signed = fill.quantity if order.side is OrderSide.BUY else -fill.quantity
                if quantity(before + signed) != quantity(after):
                    issues.append(
                        self._issue(
                            "position_effect_mismatch",
                            order,
                            fill_id=fill.fill_id,
                            expected=text(before + signed),
                            actual=text(after),
                        )
                    )
            try:
                record = self._portfolios.store.portfolio(order.portfolio_id)
                if record is None:
                    issues.append(self._issue("missing_portfolio", order))
                else:
                    if record.wallet_id not in wallet_reports:
                        wallet_reports[record.wallet_id] = await self._wallets.reconcile(
                            record.wallet_id
                        )
                    report = wallet_reports[record.wallet_id]
                    checks["wallets_checked"] = len(wallet_reports)
                    if isinstance(report, Mapping) and report.get("ok") is False:
                        issues.append(
                            self._issue(
                                "wallet_reconciliation_failed",
                                order,
                                wallet_id=record.wallet_id,
                            )
                        )
                    position = self._portfolios.store.position(
                        order.portfolio_id, order.instrument_id
                    )
                    checks["positions_checked"] += 1
                    if position is not None and position.quantity < 0:
                        issues.append(
                            self._issue(
                                "negative_portfolio_position",
                                order,
                                quantity=text(position.quantity),
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                issues.append(
                    self._issue(
                        "portfolio_read_failed",
                        order,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
        result = ReconciliationResult(
            reconciliation_id=execution_id(
                "recon", order_id or "all", now.isoformat(), len(orders), len(issues), uuid4().hex
            ),
            order_id=order_id,
            ok=not issues,
            issues=tuple(issues),
            checks=checks,
            created_at=now,
        )
        return result

    @staticmethod
    def _issue(kind: str, order: Order, **evidence: Any) -> dict[str, Any]:
        return {
            "kind": kind,
            "order_id": order.order_id,
            "proposal_id": order.proposal_id,
            "instrument_id": order.instrument_id,
            **evidence,
        }
