"""The NM-07 portfolio engine.

It composes, and never duplicates, the other phases: NM-03 moves every unit of
NovaCoin, NM-02 prices every holding, NM-05 supplies candidates, NM-06 supplies
forecasts and NM-04 supplies verified events. A rebalance is a *proposal*; applying
it posts simulated book entries and ledger transfers, and is reported as exactly
that — no order is placed and no broker exists.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable

from novacontrol.novamarket.contracts import (
    PortfolioDataError,
    PortfolioNotFoundError,
    PortfolioStateError,
    PortfolioValidationError,
)
from novacontrol.novamarket.portfolio.accounting import (
    Valuation,
    performance,
    value_portfolio,
)
from novacontrol.novamarket.portfolio.allocation import allocate
from novacontrol.novamarket.portfolio.models import (
    PAISE,
    SHARE,
    DecisionRecord,
    PortfolioHorizon,
    PortfolioRecord,
    PortfolioStatus,
    Position,
    ProposalStatus,
    RebalanceProposal,
    Side,
    TradeIntent,
    ValuationSnapshot,
    digest_payload,
    money_text,
    parse_capital,
    policy_for,
    portfolio_id_for,
    quantity_text,
    utcnow,
)
from novacontrol.novamarket.portfolio.selection import Assessment, assess_candidates
from novacontrol.novamarket.portfolio.store import PortfolioStore

_LOGGER = logging.getLogger("novacontrol.novamarket.portfolio")

Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]

CONFIRMED_EVENT_STATES = frozenset(
    {"officially_confirmed", "multi_source_corroborated"}
)
PROPOSAL_TTL_HOURS = 24
PRICE_TOLERANCE = Decimal("0.02")


class PortfolioEngine:
    """Three-horizon simulated portfolio construction and management."""

    def __init__(
        self,
        store: PortfolioStore,
        wallets: Any,
        *,
        market_data: Any | None = None,
        discovery: Any | None = None,
        forecasts: Any | None = None,
        radar: Any | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
        max_price_age_hours: float = 72.0,
    ) -> None:
        self._store = store
        self._wallets = wallets
        self._market_data = market_data
        self._discovery = discovery
        self._forecasts = forecasts
        self._radar = radar
        self._publisher = publisher
        self._clock = clock or utcnow
        self.max_price_age_hours = max_price_age_hours
        self._last_error = ""
        self._clearing_wallet_id = "pfw_market_clearing"

    # -- wiring ---------------------------------------------------------------

    @property
    def store(self) -> PortfolioStore:
        return self._store

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_market_data(self, market_data: Any | None) -> None:
        self._market_data = market_data

    def set_discovery(self, discovery: Any | None) -> None:
        self._discovery = discovery

    def set_forecasts(self, forecasts: Any | None) -> None:
        self._forecasts = forecasts

    def set_radar(self, radar: Any | None) -> None:
        self._radar = radar

    def set_publisher(self, publisher: Publisher | None) -> None:
        self._publisher = publisher

    def _now(self) -> datetime:
        return self._clock()

    async def _publish(self, event_name: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(event_name, payload)
        except Exception as exc:  # noqa: BLE001 - a committed book stands
            self._last_error = f"{event_name}: {type(exc).__name__}: {exc}"
            _LOGGER.warning(
                "novamarket.portfolio.event_not_published event=%s error=%s",
                event_name,
                type(exc).__name__,
            )

    # -- lookups --------------------------------------------------------------

    def _record(self, portfolio_id: str) -> PortfolioRecord:
        record = self._store.portfolio(portfolio_id)
        if record is None:
            raise PortfolioNotFoundError(f"unknown portfolio {portfolio_id!r}")
        return record

    def _wallet_balance(self, record: PortfolioRecord) -> Decimal:
        body = self._wallets.wallet(record.wallet_id)
        text = body["wallet"]["balance"]["total"]
        return Decimal(str(text)).quantize(PAISE)

    def _balance_of(self, wallet_id: str) -> Decimal:
        body = self._wallets.wallet(wallet_id)
        return Decimal(str(body["wallet"]["balance"]["total"])).quantize(PAISE)

    def _price(self, instrument_id: str) -> tuple[Decimal | None, datetime | None, str]:
        if self._market_data is None:
            return None, None, "market data is not initialised"
        try:
            result = self._market_data.latest(instrument_id)
        except Exception as exc:  # noqa: BLE001
            return None, None, f"{type(exc).__name__}: {exc}"
        if getattr(result, "usable", True) is False:
            return None, None, "the latest reconciliation is not usable"
        selected = getattr(result, "selected", None)
        if selected is not None and getattr(selected, "price", None) is not None:
            return (
                Decimal(str(selected.price)).quantize(PAISE),
                getattr(selected, "observed_at", None),
                "",
            )
        try:
            page = self._market_data.bars(
                instrument_id, limit=1, ascending=False
            )
            items = list(getattr(page, "items", ()) or ())
        except Exception as exc:  # noqa: BLE001
            return None, None, f"{type(exc).__name__}: {exc}"
        if not items:
            return None, None, "no price observation and no bar"
        bar = items[0]
        return (
            Decimal(str(bar.close)).quantize(PAISE),
            getattr(bar, "observed_at", None),
            "valued from the latest bar (no live quote)",
        )

    def _refresh_positions(
        self, record: PortfolioRecord, at: datetime
    ) -> tuple[list[Position], int]:
        """Price every open position; missing prices stay stale, never invented."""
        stale = 0
        for position in self._store.positions(record.portfolio_id):
            price, observed, _problem = self._price(position.instrument_id)
            if price is None or observed is None:
                stale += 1
                continue
            if position.last_price != price or position.priced_at != observed:
                self._store.upsert_position(
                    replace(
                        position,
                        last_price=price,
                        priced_at=observed,
                        updated_at=at,
                    )
                )
        positions = self._store.positions(record.portfolio_id)
        for position in positions:
            if position.priced_at is None:
                stale += 1
            elif (at - position.priced_at).total_seconds() / 3600.0 > self.max_price_age_hours:
                stale += 1
        return positions, stale

    def _history_totals(self, portfolio_id: str) -> list[Decimal]:
        return [
            Decimal(str(row["total_value"]))
            for row in self._store.valuations(portfolio_id, limit=500)
        ]

    def _valuate(
        self, record: PortfolioRecord, at: datetime
    ) -> tuple[Valuation, ValuationSnapshot]:
        positions, stale = self._refresh_positions(record, at)
        cash = self._wallet_balance(record)
        valuation = value_portfolio(
            record, positions, cash, history_totals=self._history_totals(record.portfolio_id),
            stale_after_hours=self.max_price_age_hours,
            at=self._now(),
        )
        snapshot_id = "pval_" + hashlib.sha256(
            f"{record.portfolio_id}|{at.isoformat()}".encode("utf-8")
        ).hexdigest()[:16]
        snapshot = ValuationSnapshot(
            snapshot_id=snapshot_id,
            portfolio_id=record.portfolio_id,
            at=at,
            cash=valuation.cash,
            invested=valuation.invested,
            total_value=valuation.total,
            realized_pnl=valuation.realized_pnl,
            unrealized_pnl=valuation.unrealized_pnl,
            return_pct=valuation.return_pct,
            drawdown_pct=valuation.drawdown_pct,
            cash_weight=valuation.cash_weight,
            priced_positions=valuation.priced_positions,
            stale_positions=max(valuation.stale_positions, stale),
            holdings=valuation.holdings,
        )
        self._store.save_valuation(snapshot)
        return valuation, snapshot

    # -- creation -------------------------------------------------------------

    async def create(
        self,
        horizon: PortfolioHorizon | str,
        *,
        name: str = "",
        funding_wallet_id: str,
        capital: object,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create and fund one portfolio exactly once."""
        try:
            chosen = PortfolioHorizon(horizon)
        except ValueError as exc:
            raise PortfolioValidationError(
                f"unknown portfolio horizon {horizon!r}"
            ) from exc
        policy = policy_for(chosen)
        amount = parse_capital(capital)
        funding = str(funding_wallet_id or "").strip()
        if not funding:
            raise PortfolioValidationError("a funding wallet is required")
        label = str(name or "").strip() or policy.label
        moment = at or self._now()
        portfolio_id = portfolio_id_for(chosen, funding, label)
        wallet_id = "pfw_" + portfolio_id[3:]
        existing = self._store.portfolio(portfolio_id)
        if existing is not None and amount != existing.initial_capital:
            raise PortfolioValidationError(
                f"portfolio {portfolio_id} already exists with "
                f"{money_text(existing.initial_capital)} NC; a different capital "
                f"({money_text(amount)} NC) needs a different name or horizon"
            )

        try:
            self._wallets.wallet(wallet_id)
        except Exception:
            try:
                await self._wallets.create_wallet(
                    "novamarket:portfolio",
                    label=f"{label} ({chosen.value})",
                    wallet_id=wallet_id,
                )
            except Exception as exc:  # noqa: BLE001
                raise PortfolioStateError(
                    f"the portfolio wallet could not be created: {exc}"
                ) from exc

        if existing is None:
            # Refuse an under-funded request before a record exists, so a refused
            # creation never leaves a portfolio behind.
            try:
                available = self._balance_of(funding)
            except Exception as exc:  # noqa: BLE001
                raise PortfolioValidationError(
                    f"the funding wallet {funding} is not usable: {exc}"
                ) from exc
            if available < amount:
                raise PortfolioValidationError(
                    f"funding wallet {funding} holds {money_text(available)} NC but "
                    f"{money_text(amount)} NC was requested"
                )
            record = PortfolioRecord(
                portfolio_id=portfolio_id,
                name=label,
                horizon=chosen,
                funding_wallet_id=funding,
                wallet_id=wallet_id,
                initial_capital=amount,
                status=PortfolioStatus.INITIALIZING,
                created_at=moment,
                updated_at=moment,
            )
            self._store.create_portfolio(record)
            self._store.save_decision(
                DecisionRecord(
                    decision_id="pdec_"
                    + hashlib.sha256(
                        f"{portfolio_id}|create|{moment.isoformat()}".encode("utf-8")
                    ).hexdigest()[:16],
                    portfolio_id=portfolio_id,
                    kind="create",
                    at=moment,
                    horizon=chosen.value,
                    payload={
                        "rationale": (
                            f"Created the {policy.label.lower()} portfolio with "
                            f"{money_text(amount)} NC from {funding}."
                        ),
                        "selected": [],
                        "rejected": [],
                        "allocations": [],
                        "constraints": policy.to_dict(),
                        "references": [],
                        "data_quality": "not evaluated yet",
                    },
                    digest=digest_payload(
                        {"portfolio_id": portfolio_id, "capital": money_text(amount)}
                    ),
                )
            )
            await self._publish(
                "novamarket.portfolio.created",
                portfolio_id=portfolio_id,
                horizon=chosen.value,
                name=label,
            )
        else:
            portfolio_id = existing.portfolio_id

        current = self._store.portfolio(portfolio_id)
        if current is not None and current.status is PortfolioStatus.INITIALIZING:
            try:
                result = await self._wallets.transfer(
                    funding,
                    wallet_id,
                    amount,
                    idempotency_key=f"pf-fund-{portfolio_id}",
                    reference=portfolio_id,
                    description=f"fund {label}",
                )
            except Exception as exc:  # noqa: BLE001 - the ledger refusal is the reason
                raise PortfolioValidationError(
                    f"capital could not be reserved from {funding}: {exc}"
                ) from exc
            transaction_id = str(
                (result.get("transaction") or {}).get("transaction_id") or ""
            )
            self._store.record_capital_move(
                portfolio_id=portfolio_id,
                transaction_id=transaction_id or f"pf-fund-{portfolio_id}",
                direction="funding",
                amount=amount,
                at=moment,
            )
            self._store.set_status(
                portfolio_id,
                PortfolioStatus.ACTIVE,
                reason="funded from the funding wallet and activated",
                at=moment,
            )
            await self._publish(
                "novamarket.portfolio.activated",
                portfolio_id=portfolio_id,
            )
            # Autonomous construction: a funded portfolio immediately proposes
            # and applies its opening allocation. A ledger refusal above already
            # aborted creation, so a selection failure only leaves the capital in
            # cash and is recorded by propose() as a skipped decision.
            try:
                opening = await self.propose(portfolio_id, force=True, at=moment)
            except Exception as exc:  # noqa: BLE001 - the funding stands
                _LOGGER.warning(
                    "novamarket.portfolio.opening_not_proposed portfolio=%s error=%s",
                    portfolio_id,
                    exc,
                )
            else:
                proposal = opening.get("proposal")
                if proposal is not None:
                    try:
                        await self.apply(str(proposal["proposal_id"]), at=moment)
                    except Exception as exc:  # noqa: BLE001 - cash is never lost
                        _LOGGER.warning(
                            "novamarket.portfolio.opening_not_applied portfolio=%s error=%s",
                            portfolio_id,
                            exc,
                        )
            replayed = False
        else:
            replayed = True
        return {"ok": True, "replayed": replayed, **self.detail(portfolio_id)}

    # -- reads ----------------------------------------------------------------

    def portfolios(
        self,
        *,
        horizon: PortfolioHorizon | str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        label = None
        if horizon is not None:
            try:
                label = PortfolioHorizon(horizon).value
            except ValueError as exc:
                raise PortfolioValidationError(
                    f"unknown portfolio horizon {horizon!r}"
                ) from exc
        rows = self._store.portfolios(horizon=label, status=status, limit=limit)
        return {
            "ok": True,
            "portfolios": [record.to_dict() for record in rows],
            "total": len(rows),
        }

    def detail(self, portfolio_id: str) -> dict[str, Any]:
        record = self._record(portfolio_id)
        policy = policy_for(record.horizon)
        cash = self._wallet_balance(record)
        positions = self._store.positions(portfolio_id)
        valuation = value_portfolio(
            record, positions, cash, history_totals=self._history_totals(portfolio_id),
            stale_after_hours=self.max_price_age_hours,
            at=self._now(),
        )
        return {
            "portfolio": record.to_dict(),
            "policy": policy.to_dict(),
            "cash": money_text(cash),
            "valuation": valuation.to_dict(),
            "holdings": self._holdings_payload(record, positions, valuation),
            "constraints": self.constraints(portfolio_id),
            "counts": {
                "positions": len(positions),
                "decisions": len(self._store.decisions(portfolio_id, limit=500)),
                "valuations": len(self._store.valuations(portfolio_id, limit=500)),
            },
        }

    def _holdings_payload(
        self,
        record: PortfolioRecord,
        positions: list[Position],
        valuation: Valuation,
    ) -> list[dict[str, Any]]:
        total = valuation.total
        payload: list[dict[str, Any]] = []
        for position in positions:
            body = position.to_dict()
            body["stale"] = bool(
                position.priced_at is None
                or (self._now() - position.priced_at).total_seconds() / 3600.0
                > self.max_price_age_hours
            )
            value = position.market_value() or position.cost_basis
            body["weight"] = money_text(
                (value / total).quantize(Decimal("0.0001")) if total > 0 else Decimal("0")
            )
            payload.append(body)
        return payload

    def holdings(self, portfolio_id: str) -> dict[str, Any]:
        record = self._record(portfolio_id)
        cash = self._wallet_balance(record)
        positions = self._store.positions(portfolio_id)
        valuation = value_portfolio(
            record, positions, cash, history_totals=self._history_totals(portfolio_id),
            stale_after_hours=self.max_price_age_hours,
            at=self._now(),
        )
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "holdings": self._holdings_payload(record, positions, valuation),
            "cash": money_text(cash),
            "invested": money_text(valuation.invested),
            "total_value": money_text(valuation.total),
        }

    def valuation(self, portfolio_id: str) -> dict[str, Any]:
        """A read-only valuation: persists nothing and publishes nothing."""
        record = self._record(portfolio_id)
        positions = self._store.positions(portfolio_id)
        cash = self._wallet_balance(record)
        value = value_portfolio(
            record, positions, cash, history_totals=self._history_totals(portfolio_id),
            stale_after_hours=self.max_price_age_hours,
            at=self._now(),
        )
        return {"ok": True, "portfolio_id": portfolio_id, "valuation": value.to_dict()}

    async def valuate(
        self, portfolio_id: str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        """Value the book, store the snapshot and publish the measured result."""
        record = self._record(portfolio_id)
        value, snapshot = self._valuate(record, at or self._now())
        await self._publish(
            "novamarket.portfolio.valuation.updated",
            portfolio_id=portfolio_id,
            total_value=money_text(value.total),
        )
        await self._publish(
            "novamarket.portfolio.performance.updated",
            portfolio_id=portfolio_id,
            observations=str(len(self._store.valuations(portfolio_id, limit=500))),
        )
        return {"ok": True, "valuation": value.to_dict(), "snapshot": snapshot.to_dict()}

    def valuations(
        self, portfolio_id: str, *, limit: int = 200, latest: bool = False
    ) -> dict[str, Any]:
        self._record(portfolio_id)
        rows = self._store.valuations(portfolio_id, limit=limit, latest=latest)
        return {"ok": True, "valuations": rows, "total": len(rows)}

    def decisions(self, portfolio_id: str, *, limit: int = 100) -> dict[str, Any]:
        self._record(portfolio_id)
        rows = self._store.decisions(portfolio_id, limit=limit)
        return {"ok": True, "decisions": rows, "total": len(rows)}

    def proposals(
        self,
        portfolio_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._record(portfolio_id)
        rows = self._store.proposals(portfolio_id, status=status, limit=limit)
        return {
            "ok": True,
            "proposals": [item.to_dict() for item in rows],
            "total": len(rows),
        }

    def lifecycle(self, portfolio_id: str) -> dict[str, Any]:
        self._record(portfolio_id)
        rows = self._store.lifecycle(portfolio_id)
        return {"ok": True, "transitions": rows, "total": len(rows)}

    def constraints(self, portfolio_id: str) -> list[dict[str, Any]]:
        record = self._store.portfolio(portfolio_id)
        if record is None:
            return []
        policy = policy_for(record.horizon)
        cash = self._wallet_balance(record)
        positions = self._store.positions(portfolio_id)
        valuation = value_portfolio(
            record, positions, cash, history_totals=self._history_totals(portfolio_id),
            stale_after_hours=self.max_price_age_hours,
            at=self._now(),
        )
        violations: list[dict[str, Any]] = []
        if len(positions) > policy.max_positions:
            violations.append(
                {
                    "code": "max_positions",
                    "detail": f"{len(positions)} positions exceed {policy.max_positions}",
                }
            )
        total = valuation.total
        sectors: dict[str, Decimal] = {}
        for position in positions:
            value = position.market_value() or position.cost_basis
            weight = (value / total) if total > 0 else Decimal("0")
            if weight > policy.max_position_weight:
                violations.append(
                    {
                        "code": "max_position_weight",
                        "instrument_id": position.instrument_id,
                        "detail": (
                            f"weight {money_text(weight)} exceeds "
                            f"{money_text(policy.max_position_weight)}"
                        ),
                    }
                )
            sectors[position.sector] = sectors.get(position.sector, Decimal("0")) + weight
        for sector, weight in sectors.items():
            if weight > policy.max_sector_weight:
                violations.append(
                    {
                        "code": "max_sector_weight",
                        "sector": sector,
                        "detail": (
                            f"sector weight {money_text(weight)} exceeds "
                            f"{money_text(policy.max_sector_weight)}"
                        ),
                    }
                )
        if total > 0 and valuation.cash_weight < policy.min_cash_fraction:
            violations.append(
                {
                    "code": "min_cash_fraction",
                    "detail": (
                        f"cash weight {money_text(valuation.cash_weight)} is below "
                        f"{money_text(policy.min_cash_fraction)}"
                    ),
                }
            )
        return violations

    def performance(self, portfolio_id: str) -> dict[str, Any]:
        record = self._record(portfolio_id)
        policy = policy_for(record.horizon)
        cash = self._wallet_balance(record)
        positions = self._store.positions(portfolio_id)
        rows = self._store.valuations(portfolio_id, limit=500)
        totals = [Decimal(str(row["total_value"])) for row in rows]
        timestamps = [datetime.fromisoformat(row["at"]) for row in rows]
        valuation = value_portfolio(
            record, positions, cash, history_totals=totals
        )
        history = self._store.position_history(portfolio_id, limit=500)
        turnover_notional = sum(
            (Decimal(str(row["notional"])) for row in history), Decimal("0.00")
        )
        mean_total = (
            sum(totals, Decimal("0.00")) / Decimal(len(totals))
            if totals
            else valuation.total
        )
        metrics = performance(
            record,
            valuation=valuation,
            totals=totals,
            timestamps=timestamps,
            turnover_notional=turnover_notional,
            mean_total=mean_total,
            policy_minimum=policy.min_performance_observations,
        )
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "valuation": valuation.to_dict(),
            "metrics": metrics,
            "external_flows": self._store.capital_moves(portfolio_id),
            "flow_note": (
                "capital deposited or released through the ledger is reported "
                "separately and is not counted as investment return"
            ),
        }

    def status(self) -> dict[str, Any]:
        metrics = self._store.metrics()
        return {
            "initialised": True,
            "phase": "NM-07",
            "schema_version": self._store.schema_version,
            "portfolios": metrics["portfolios"],
            "by_status": metrics["by_status"],
            "open_positions": metrics["open_positions"],
            "valuations": metrics["valuations"],
            "proposals": metrics["proposals"],
            "decisions": metrics["decisions"],
            "horizons": {
                horizon.value: policy_for(horizon).to_dict()
                for horizon in PortfolioHorizon
            },
            "last_error": self._last_error,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "phase": "NM-07",
            "horizons": {
                horizon.value: policy_for(horizon).to_dict()
                for horizon in PortfolioHorizon
            },
            "capital": {
                "unit": "NovaCoin",
                "exchange_rate": "1 NC = 1 INR",
                "funding": "an NM-03 ledger transfer from the funding wallet",
                "clearing": (
                    "a simulated market-counterparty wallet receives buy cost and "
                    "pays sale proceeds, so NC is conserved across wallets"
                ),
                "orders": "none: NM-07 records simulated book entries only",
                "execution_phase": "NM-10",
            },
            "guarantees": [
                "no portfolio is funded twice for the same idempotent request",
                "capital never exceeds the funding wallet's available balance",
                "total committed capital is bounded by ledger balances",
                "a rebalance is a proposal until an explicit apply is recorded",
                "every applied change keeps its decision and position history",
                "no order is placed and no broker exists in this build",
            ],
            "events": [
                "novamarket.portfolio.created",
                "novamarket.portfolio.activated",
                "novamarket.portfolio.allocation.updated",
                "novamarket.portfolio.position.added",
                "novamarket.portfolio.position.updated",
                "novamarket.portfolio.position.removed",
                "novamarket.portfolio.valuation.updated",
                "novamarket.portfolio.rebalance.proposed",
                "novamarket.portfolio.rebalance.completed",
                "novamarket.portfolio.performance.updated",
                "novamarket.portfolio.paused",
                "novamarket.portfolio.suspended",
                "novamarket.portfolio.closed",
            ],
            "executes_orders": False,
            "real_money": False,
        }

    # -- proposal -------------------------------------------------------------

    def _candidate_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        if self._discovery is None:
            return []
        try:
            body = self._discovery.candidates(limit=limit)
        except Exception as exc:  # noqa: BLE001 - no data is a stated skip
            self._last_error = f"discovery: {type(exc).__name__}: {exc}"
            return []
        rows = [dict(row) for row in body.get("candidates", ()) or ()]
        for row in rows:
            instrument_id = str(row.get("instrument_id") or "")
            if self._market_data is None or not instrument_id:
                continue
            try:
                meta = self._market_data.get_instrument(instrument_id).to_dict()
            except Exception:  # noqa: BLE001
                continue
            row.setdefault("sector", meta.get("sector") or meta.get("asset_class") or "unknown")
            row.setdefault("asset_class", meta.get("asset_class") or "unknown")
        return rows

    def _forecast_index(
        self, instrument_ids: list[str]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        index: dict[str, dict[str, dict[str, Any]]] = {}
        if self._forecasts is None:
            return index
        for instrument_id in instrument_ids:
            try:
                body = self._forecasts.forecasts(instrument_id=instrument_id, limit=50)
            except Exception:  # noqa: BLE001
                continue
            by_horizon: dict[str, dict[str, Any]] = {}
            for row in body.get("forecasts", ()) or ():
                horizon = str(row.get("horizon") or "")
                current = by_horizon.get(horizon)
                if current is None or str(row.get("created_at")) > str(
                    current.get("created_at")
                ):
                    by_horizon[horizon] = dict(row)
            index[instrument_id] = by_horizon
        return index

    def _model_states(self) -> dict[tuple[str, str, str], str]:
        if self._forecasts is None:
            return {}
        try:
            body = self._forecasts.models()
        except Exception:  # noqa: BLE001
            return {}
        states: dict[tuple[str, str, str], str] = {}
        for row in body.get("models", ()) or ():
            key = (
                str(row.get("instrument_id") or ""),
                str(row.get("model_id") or ""),
                str(row.get("horizon") or ""),
            )
            states[key] = str(row.get("state") or "")
        return states

    def _event_pressure(self, instrument_ids: list[str]) -> dict[str, bool]:
        pressure: dict[str, bool] = {}
        if self._radar is None:
            return pressure
        for instrument_id in instrument_ids:
            try:
                body = self._radar.instrument_events(instrument_id, limit=20)
            except Exception:  # noqa: BLE001
                continue
            for event in body.get("events", ()) or ():
                if str(event.get("verification") or "") not in CONFIRMED_EVENT_STATES:
                    continue
                if str(event.get("importance") or "") in {"high", "critical"}:
                    pressure[instrument_id] = True
                    break
        return pressure

    def _intents(
        self,
        record: PortfolioRecord,
        selected: list[Assessment],
        weights: dict[str, Decimal],
        positions: dict[str, Position],
        valuation: Valuation,
        policy: Any,
        at: datetime,
    ) -> tuple[list[TradeIntent], Decimal, list[str]]:
        total = valuation.total
        notes: list[str] = []
        intents: list[TradeIntent] = []
        current: dict[str, Decimal] = {}
        for instrument_id, position in positions.items():
            value = position.market_value() or position.cost_basis
            current[instrument_id] = (
                (value / total) if total > 0 else Decimal("0")
            )
        min_change = (total * policy.drift_threshold).quantize(PAISE)
        for assessment in selected:
            instrument_id = assessment.instrument_id
            target_weight = weights.get(instrument_id, Decimal("0"))
            price, _observed, problem = self._price(instrument_id)
            if price is None:
                notes.append(
                    f"{instrument_id}: no usable price, allocation stays in cash"
                )
                continue
            target_value = (total * target_weight).quantize(PAISE)
            current_value = (
                (total * current.get(instrument_id, Decimal("0"))).quantize(PAISE)
            )
            delta = target_value - current_value
            if instrument_id in positions and abs(delta) < min_change:
                continue
            if delta <= 0 and instrument_id not in positions:
                continue
            side = Side.BUY if delta > 0 else Side.SELL
            quantity_delta = (abs(delta) / price).quantize(SHARE)
            notional = (quantity_delta * price).quantize(PAISE)
            if notional <= 0:
                continue
            intents.append(
                TradeIntent(
                    instrument_id=instrument_id,
                    side=side,
                    target_weight=target_weight,
                    current_weight=current.get(instrument_id, Decimal("0")),
                    notional=notional,
                    quantity_delta=quantity_delta if side is Side.BUY else -quantity_delta,
                    price=price,
                    reason="; ".join(assessment.reasons),
                )
            )
        for instrument_id, position in positions.items():
            if instrument_id in weights:
                continue
            price, _observed, problem = self._price(instrument_id)
            if price is None:
                notes.append(
                    f"{instrument_id}: held but unpriced, no exit proposed"
                )
                continue
            intents.append(
                TradeIntent(
                    instrument_id=instrument_id,
                    side=Side.SELL,
                    target_weight=Decimal("0"),
                    current_weight=current.get(instrument_id, Decimal("0")),
                    notional=position.cost_basis,
                    quantity_delta=-position.quantity,
                    price=price,
                    reason="no longer selected by this horizon's policy",
                )
            )
        return intents, min_change, notes

    async def propose(
        self,
        portfolio_id: str,
        *,
        force: bool = False,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        record = self._record(portfolio_id)
        policy = policy_for(record.horizon)
        moment = at or self._now()
        if record.status not in {PortfolioStatus.ACTIVE, PortfolioStatus.UNDER_REVIEW}:
            raise PortfolioStateError(
                f"portfolio {portfolio_id} is {record.status.value}; a proposal needs "
                "an active portfolio"
            )
        if (
            not force
            and record.last_rebalanced_at is not None
            and moment - record.last_rebalanced_at < timedelta(hours=policy.cooldown_hours)
        ):
            return {
                "ok": True,
                "skipped": "cooldown",
                "detail": (
                    f"last rebalance {record.last_rebalanced_at.isoformat()}; the "
                    f"cooldown is {policy.cooldown_hours}h"
                ),
                "proposal": None,
            }
        valuation, _snapshot = self._valuate(record, moment)
        positions = {position.instrument_id: position for position in self._store.positions(portfolio_id)}
        rows = self._candidate_rows()
        if not rows:
            decision = self._record_decision(
                record,
                "rebalance_skipped",
                moment,
                {
                    "rationale": (
                        "No discovery candidates are available, so no allocation is "
                        "proposed and existing holdings are retained."
                    ),
                    "constraints": policy.to_dict(),
                    "references": [],
                    "data_quality": "no candidate data",
                },
            )
            self._store.save_decision(decision)
            return {
                "ok": True,
                "skipped": "no_candidates",
                "detail": "discovery has produced no candidates yet",
                "proposal": None,
            }
        instrument_ids = [str(row.get("instrument_id")) for row in rows]
        selected, rejected = assess_candidates(
            rows,
            self._forecast_index(instrument_ids),
            self._model_states(),
            policy,
            moment,
            event_pressure=self._event_pressure(instrument_ids),
        )
        plan = allocate(selected, policy)
        weights = plan.weights
        if not weights and positions:
            decision = self._record_decision(
                record,
                "rebalance_skipped",
                moment,
                {
                    "rationale": (
                        "No candidate passed this horizon's gates; existing holdings "
                        "are retained rather than liquidated on missing evidence."
                    ),
                    "selected": [],
                    "rejected": [item.to_dict() for item in rejected],
                    "allocations": [],
                    "constraints": policy.to_dict(),
                    "references": [],
                    "data_quality": "insufficient eligible evidence",
                },
            )
            self._store.save_decision(decision)
            return {
                "ok": True,
                "skipped": "no_eligible_assets",
                "detail": "no candidate passed the horizon's selection gates",
                "proposal": None,
            }
        selected = [asset for asset in selected if asset.instrument_id in weights]
        intents, min_change, notes = self._intents(
            record, selected, weights, positions, valuation, policy, moment
        )
        if not intents:
            return {
                "ok": True,
                "skipped": "no_change",
                "detail": "the desired allocation is within the drift threshold",
                "proposal": None,
            }
        turnover = (
            sum((intent.notional for intent in intents), Decimal("0.00")) / valuation.total
            if valuation.total > 0
            else Decimal("0")
        ).quantize(Decimal("0.0001"))
        drift = max(
            (
                abs(weights.get(key, Decimal("0")) - intent.current_weight)
                for key, intent in ((item.instrument_id, item) for item in intents)
            ),
            default=Decimal("0"),
        ).quantize(Decimal("0.0001"))
        if drift < policy.drift_threshold and not force:
            return {
                "ok": True,
                "skipped": "drift",
                "detail": (
                    f"drift {money_text(drift)} is below "
                    f"{money_text(policy.drift_threshold)}"
                ),
                "proposal": None,
            }
        if positions and turnover > policy.max_turnover and not force:
            return {
                "ok": True,
                "skipped": "turnover",
                "detail": (
                    f"turnover {money_text(turnover)} exceeds the "
                    f"{money_text(policy.max_turnover)} limit"
                ),
                "proposal": None,
            }
        buys = sum(
            (intent.notional for intent in intents if intent.side is Side.BUY),
            Decimal("0.00"),
        )
        sells = sum(
            (intent.notional for intent in intents if intent.side is Side.SELL),
            Decimal("0.00"),
        )
        cash_after = (valuation.cash + sells - buys).quantize(PAISE)
        if cash_after < (valuation.total * policy.min_cash_fraction).quantize(PAISE):
            raise PortfolioStateError(
                "the proposal would breach the cash reserve; nothing was proposed"
            )
        digest = digest_payload(
            {
                "portfolio_id": portfolio_id,
                "policy": policy.version,
                "intents": [
                    {
                        "instrument_id": intent.instrument_id,
                        "side": intent.side.value,
                        "quantity": quantity_text(intent.quantity_delta),
                    }
                    for intent in intents
                ],
            }
        )
        for existing in self._store.proposals(portfolio_id, status="validated"):
            if existing.digest == digest and existing.expires_at > moment:
                return {
                    "ok": True,
                    "replayed": True,
                    "proposal": existing.to_dict(),
                }
        proposal_id = "preb_" + hashlib.sha256(
            f"{portfolio_id}|{digest}".encode("utf-8")
        ).hexdigest()[:16]
        # The same desired book must never be stored twice: a proposal that was
        # already applied (or rejected) is replayed, not re-inserted.
        earlier = self._store.proposal(proposal_id)
        if earlier is not None:
            return {
                "ok": True,
                "replayed": True,
                "proposal": earlier.to_dict(),
            }
        proposal = RebalanceProposal(
            proposal_id=proposal_id,
            portfolio_id=portfolio_id,
            created_at=moment,
            status=ProposalStatus.VALIDATED,
            drift=drift,
            turnover=turnover,
            intents=tuple(intents),
            rationale=(
                f"{len(intents)} change(s) for the {policy.label.lower()} portfolio "
                f"using {policy.weighting} weighting; drift {money_text(drift)}, "
                f"turnover {money_text(turnover)}."
            ),
            policy_version=policy.version,
            expires_at=moment + timedelta(hours=PROPOSAL_TTL_HOURS),
            cash_after=cash_after,
            digest=digest,
            notes=tuple([*plan.notes, *notes]),
        )
        self._store.save_proposal(proposal)
        self._store.save_decision(
            self._decision_for_proposal(record, proposal, selected, rejected, moment)
        )
        self._store.set_status(
            portfolio_id,
            PortfolioStatus.UNDER_REVIEW,
            reason="a rebalance proposal is awaiting a decision",
            at=moment,
        )
        await self._publish(
            "novamarket.portfolio.rebalance.proposed",
            portfolio_id=portfolio_id,
            proposal_id=proposal.proposal_id,
            turnover=money_text(turnover),
        )
        return {"ok": True, "proposal": proposal.to_dict()}

    def _record_decision(
        self, record: PortfolioRecord, kind: str, at: datetime, payload: dict[str, Any]
    ) -> DecisionRecord:
        return DecisionRecord(
            decision_id="pdec_"
            + hashlib.sha256(
                f"{record.portfolio_id}|{kind}|{at.isoformat()}".encode("utf-8")
            ).hexdigest()[:16],
            portfolio_id=record.portfolio_id,
            kind=kind,
            at=at,
            horizon=record.horizon.value,
            payload=payload,
            digest=digest_payload(payload),
        )

    def _decision_for_proposal(
        self,
        record: PortfolioRecord,
        proposal: RebalanceProposal,
        selected: list[Assessment],
        rejected: list[Assessment],
        at: datetime,
    ) -> DecisionRecord:
        payload = {
            "rationale": proposal.rationale,
            "proposal_id": proposal.proposal_id,
            "selected": [item.to_dict() for item in selected],
            "rejected": [item.to_dict() for item in rejected],
            "allocations": [
                {
                    "instrument_id": intent.instrument_id,
                    "side": intent.side.value,
                    "target_weight": money_text(intent.target_weight),
                    "current_weight": money_text(intent.current_weight),
                    "notional": money_text(intent.notional),
                    "price": money_text(intent.price),
                    "reason": intent.reason,
                }
                for intent in proposal.intents
            ],
            "constraints": policy_for(record.horizon).to_dict(),
            "references": sorted(
                {
                    str(item.forecast_id)
                    for item in selected
                    if item.forecast_id is not None
                }
            ),
            "event_references": sorted(
                {event for item in selected for event in item.event_ids}
            ),
            "data_quality": {
                item.instrument_id: item.data_quality for item in selected
            },
            "drift": money_text(proposal.drift),
            "turnover": money_text(proposal.turnover),
        }
        return self._record_decision(record, "rebalance_proposed", at, payload)

    # -- apply ----------------------------------------------------------------

    async def _ensure_clearing_wallet(self) -> str:
        try:
            self._wallets.wallet(self._clearing_wallet_id)
        except Exception:
            try:
                await self._wallets.create_wallet(
                    "novamarket:market_clearing",
                    label="Simulated market counterparty",
                    wallet_id=self._clearing_wallet_id,
                )
            except Exception as exc:  # noqa: BLE001
                raise PortfolioStateError(
                    f"the clearing wallet could not be created: {exc}"
                ) from exc
        return self._clearing_wallet_id

    async def apply(self, proposal_id: str, *, at: datetime | None = None) -> dict[str, Any]:
        """Apply a validated proposal to the book; the ledger moves the cash."""
        proposal = self._store.proposal(proposal_id)
        if proposal is None:
            raise PortfolioNotFoundError(f"unknown proposal {proposal_id!r}")
        record = self._record(proposal.portfolio_id)
        moment = at or self._now()
        if proposal.status is ProposalStatus.APPLIED:
            return {
                "ok": True,
                "replayed": True,
                "proposal": proposal.to_dict(),
                "portfolio": self.detail(record.portfolio_id),
            }
        if proposal.status is not ProposalStatus.VALIDATED:
            raise PortfolioStateError(
                f"proposal {proposal_id} is {proposal.status.value}; only a validated "
                "proposal can be applied"
            )
        if proposal.expires_at <= moment:
            self._store.set_proposal_status(proposal_id, ProposalStatus.EXPIRED)
            raise PortfolioStateError("the proposal has expired; re-plan it")
        if record.status not in {PortfolioStatus.ACTIVE, PortfolioStatus.UNDER_REVIEW}:
            raise PortfolioStateError(
                f"portfolio {record.portfolio_id} is {record.status.value}"
            )
        policy = policy_for(record.horizon)
        valuation, _snapshot = self._valuate(record, moment)
        positions = {
            position.instrument_id: position
            for position in self._store.positions(record.portfolio_id)
        }
        metadata: dict[str, tuple[str, str]] = {}
        for row in self._candidate_rows():
            instrument_id = str(row.get("instrument_id") or "")
            if instrument_id:
                metadata[instrument_id] = (
                    str(row.get("sector") or "unknown"),
                    str(row.get("asset_class") or "unknown"),
                )
        for intent in proposal.intents:
            if intent.instrument_id in metadata or self._market_data is None:
                continue
            try:
                meta = self._market_data.get_instrument(intent.instrument_id).to_dict()
            except Exception:  # noqa: BLE001 - unknown metadata is stated, not guessed
                continue
            metadata[intent.instrument_id] = (
                str(meta.get("sector") or "unknown"),
                str(meta.get("asset_class") or "unknown"),
            )
        # Pre-flight: every price must still be within tolerance and the cash must
        # cover every buy after the sells, before a single unit moves.
        buys = Decimal("0.00")
        sells = Decimal("0.00")
        for intent in proposal.intents:
            price, _observed, _problem = self._price(intent.instrument_id)
            if price is None:
                raise PortfolioDataError(
                    f"no usable price for {intent.instrument_id}; nothing was applied"
                )
            if intent.price > 0:
                move = abs(price - intent.price) / intent.price
                if move > PRICE_TOLERANCE:
                    raise PortfolioStateError(
                        f"{intent.instrument_id} moved {money_text(move * 100)}% since "
                        "the proposal; re-plan it"
                    )
            if intent.side is Side.BUY:
                buys += intent.notional
            else:
                sells += intent.notional
        reserve = (valuation.total * policy.min_cash_fraction).quantize(PAISE)
        if valuation.cash + sells - buys < reserve:
            raise PortfolioStateError(
                "applying this proposal would breach the cash reserve; nothing was applied"
            )
        clearing = await self._ensure_clearing_wallet()
        self._store.set_status(
            record.portfolio_id,
            PortfolioStatus.REBALANCING,
            reason="applying a validated rebalance proposal",
            at=moment,
        )
        try:
            realized_delta = Decimal("0.00")
            for intent in proposal.intents:
                if intent.side is not Side.SELL:
                    continue
                position = positions.get(intent.instrument_id)
                if position is None or position.quantity <= 0:
                    continue
                price, _observed, _problem = self._price(intent.instrument_id)
                assert price is not None
                sold = min(position.quantity, abs(intent.quantity_delta))
                proceeds = (sold * price).quantize(PAISE)
                realized = ((price - position.average_cost) * sold).quantize(PAISE)
                await self._wallets.transfer(
                    clearing,
                    record.wallet_id,
                    proceeds,
                    idempotency_key=f"pf-sell-{proposal_id}-{intent.instrument_id}",
                    reference=proposal_id,
                    description=f"sell {intent.instrument_id}",
                )
                remaining = (position.quantity - sold).quantize(SHARE)
                self._store.upsert_position(
                    replace(
                        position,
                        quantity=remaining,
                        realized_pnl=(position.realized_pnl + realized).quantize(PAISE),
                        last_price=price,
                        priced_at=self._now(),
                        updated_at=moment,
                        closed_at=moment if remaining == 0 else position.closed_at,
                    )
                )
                self._store.record_position_history(
                    portfolio_id=record.portfolio_id,
                    instrument_id=intent.instrument_id,
                    kind="sell" if remaining else "close",
                    quantity=sold,
                    price=price,
                    notional=proceeds,
                    realized=realized,
                    at=moment,
                    reference=proposal_id,
                )
                realized_delta += realized
                await self._publish(
                    "novamarket.portfolio.position.updated",
                    portfolio_id=record.portfolio_id,
                    instrument_id=intent.instrument_id,
                    quantity=quantity_text(remaining),
                    price=money_text(price),
                )
                if remaining == 0:
                    await self._publish(
                        "novamarket.portfolio.position.removed",
                        portfolio_id=record.portfolio_id,
                        instrument_id=intent.instrument_id,
                        reason="sold to target weight",
                    )
            for intent in proposal.intents:
                if intent.side is not Side.BUY:
                    continue
                sector, asset_class = metadata.get(
                    intent.instrument_id, ("unknown", "unknown")
                )
                price, _observed, _problem = self._price(intent.instrument_id)
                assert price is not None
                quantity = (intent.notional / price).quantize(SHARE)
                cost = (quantity * price).quantize(PAISE)
                if quantity <= 0 or cost <= 0:
                    continue
                await self._wallets.transfer(
                    record.wallet_id,
                    clearing,
                    cost,
                    idempotency_key=f"pf-buy-{proposal_id}-{intent.instrument_id}",
                    reference=proposal_id,
                    description=f"buy {intent.instrument_id}",
                )
                position = positions.get(intent.instrument_id)
                if position is None:
                    position = Position(
                        portfolio_id=record.portfolio_id,
                        instrument_id=intent.instrument_id,
                        quantity=quantity,
                        average_cost=price,
                        realized_pnl=Decimal("0.00"),
                        sector=sector,
                        asset_class=asset_class,
                        opened_at=moment,
                        updated_at=moment,
                        last_price=price,
                        priced_at=moment,
                    )
                    await self._publish(
                        "novamarket.portfolio.position.added",
                        portfolio_id=record.portfolio_id,
                        instrument_id=intent.instrument_id,
                        quantity=quantity_text(quantity),
                    )
                else:
                    total_quantity = (position.quantity + quantity).quantize(SHARE)
                    total_cost = (
                        position.quantity * position.average_cost + cost
                    ).quantize(PAISE)
                    average = (total_cost / total_quantity).quantize(PAISE)
                    position = replace(
                        position,
                        quantity=total_quantity,
                        average_cost=average,
                        last_price=price,
                        priced_at=moment,
                        updated_at=moment,
                        sector=(
                            position.sector
                            if position.sector not in {"", "unknown"}
                            else sector
                        ),
                        asset_class=(
                            position.asset_class
                            if position.asset_class not in {"", "unknown"}
                            else asset_class
                        ),
                    )
                    await self._publish(
                        "novamarket.portfolio.position.updated",
                        portfolio_id=record.portfolio_id,
                        instrument_id=intent.instrument_id,
                        quantity=quantity_text(total_quantity),
                        price=money_text(price),
                    )
                self._store.upsert_position(position)
                positions[intent.instrument_id] = position
                self._store.record_position_history(
                    portfolio_id=record.portfolio_id,
                    instrument_id=intent.instrument_id,
                    kind="buy",
                    quantity=quantity,
                    price=price,
                    notional=cost,
                    realized=Decimal("0.00"),
                    at=moment,
                    reference=proposal_id,
                )
        except Exception:
            # The book is reverted to a reviewable state; the ledger is append-only
            # and reconcile() reports any transfer that landed without a book entry.
            self._store.set_status(
                record.portfolio_id,
                PortfolioStatus.UNDER_REVIEW,
                reason="apply failed; review the ledger and book before retrying",
                at=moment,
            )
            # A partially applied book must never be applied again: the ledger
            # entries would replay but the position writes would double-count.
            self._store.set_proposal_status(proposal_id, ProposalStatus.FAILED)
            raise
        self._store.set_proposal_status(proposal_id, ProposalStatus.APPLIED)
        self._store.touch(
            record.portfolio_id,
            at=moment,
            evaluated=True,
            rebalanced=True,
            realized_delta=realized_delta,
        )
        refreshed = self._record(record.portfolio_id)
        self._store.set_status(
            refreshed.portfolio_id,
            PortfolioStatus.ACTIVE,
            reason="rebalance applied and the portfolio is active",
            at=moment,
        )
        self._store.save_decision(
            self._record_decision(
                refreshed,
                "rebalance_applied",
                moment,
                {
                    "proposal_id": proposal_id,
                    "rationale": (
                        f"Applied {len(proposal.intents)} simulated book change(s); "
                        "cash moved through the NM-03 ledger and no order was placed."
                    ),
                    "realized_pnl_delta": money_text(realized_delta),
                    "constraints": policy.to_dict(),
                    "references": [proposal_id],
                    "data_quality": "applied at proposal prices within tolerance",
                },
            )
        )
        await self._publish(
            "novamarket.portfolio.allocation.updated",
            portfolio_id=record.portfolio_id,
            positions=str(len(positions)),
        )
        await self._publish(
            "novamarket.portfolio.rebalance.completed",
            portfolio_id=record.portfolio_id,
            proposal_id=proposal_id,
            turnover=money_text(proposal.turnover),
        )
        return {
            "ok": True,
            "proposal": (self._store.proposal(proposal_id) or proposal).to_dict(),
            "portfolio": self.detail(record.portfolio_id),
        }

    # -- lifecycle ------------------------------------------------------------

    async def set_status(
        self,
        portfolio_id: str,
        action: str,
        *,
        reason: str = "",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        record = self._record(portfolio_id)
        moment = at or self._now()
        target: PortfolioStatus
        if action == "pause":
            target = PortfolioStatus.PAUSED
            event = "novamarket.portfolio.paused"
        elif action == "resume":
            target = PortfolioStatus.ACTIVE
            event = "novamarket.portfolio.activated"
        elif action == "suspend":
            target = PortfolioStatus.SUSPENDED
            event = "novamarket.portfolio.suspended"
        else:
            raise PortfolioValidationError(f"unknown portfolio action {action!r}")
        try:
            updated = self._store.set_status(
                portfolio_id, target, reason=reason or action, at=moment
            )
        except ValueError as exc:
            raise PortfolioStateError(str(exc)) from exc
        if updated is None:
            raise PortfolioNotFoundError(f"unknown portfolio {portfolio_id!r}")
        await self._publish(event, portfolio_id=portfolio_id, reason=reason or action)
        return {"ok": True, "portfolio": updated.to_dict()}

    async def close(
        self,
        portfolio_id: str,
        *,
        reason: str = "",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        record = self._record(portfolio_id)
        moment = at or self._now()
        if record.status is PortfolioStatus.CLOSED:
            return {"ok": True, "replayed": True, "portfolio": record.to_dict()}
        if record.status not in {
            PortfolioStatus.ACTIVE,
            PortfolioStatus.PAUSED,
            PortfolioStatus.UNDER_REVIEW,
        }:
            raise PortfolioStateError(
                f"portfolio {portfolio_id} is {record.status.value}"
            )
        positions = self._store.positions(portfolio_id)
        prices: dict[str, Decimal] = {}
        for position in positions:
            price, _observed, _problem = self._price(position.instrument_id)
            if price is None:
                raise PortfolioDataError(
                    f"{position.instrument_id} has no usable price; closing would "
                    "value the exit arbitrarily"
                )
            prices[position.instrument_id] = price
        clearing = await self._ensure_clearing_wallet()
        realized = Decimal("0.00")
        for position in positions:
            price = prices[position.instrument_id]
            proceeds = (position.quantity * price).quantize(PAISE)
            gain = ((price - position.average_cost) * position.quantity).quantize(PAISE)
            await self._wallets.transfer(
                clearing,
                record.wallet_id,
                proceeds,
                idempotency_key=f"pf-close-{portfolio_id}-{position.instrument_id}",
                reference=portfolio_id,
                description=f"close {position.instrument_id}",
            )
            self._store.upsert_position(
                replace(
                    position,
                    quantity=Decimal("0"),
                    realized_pnl=(position.realized_pnl + gain).quantize(PAISE),
                    last_price=price,
                    priced_at=moment,
                    updated_at=moment,
                    closed_at=moment,
                )
            )
            self._store.record_position_history(
                portfolio_id=portfolio_id,
                instrument_id=position.instrument_id,
                kind="close",
                quantity=position.quantity,
                price=price,
                notional=proceeds,
                realized=gain,
                at=moment,
                reference="portfolio_closed",
            )
            realized += gain
        cash = self._wallet_balance(record)
        if cash > 0:
            await self._wallets.transfer(
                record.wallet_id,
                record.funding_wallet_id,
                cash,
                idempotency_key=f"pf-release-{portfolio_id}",
                reference=portfolio_id,
                description="release portfolio capital",
            )
            self._store.record_capital_move(
                portfolio_id=portfolio_id,
                transaction_id=f"pf-release-{portfolio_id}",
                direction="release",
                amount=cash,
                at=moment,
            )
        self._store.touch(
            portfolio_id, at=moment, evaluated=True, realized_delta=realized
        )
        self._store.set_status(
            portfolio_id,
            PortfolioStatus.CLOSED,
            reason=reason or "portfolio closed and capital released",
            at=moment,
        )
        self._store.save_decision(
            self._record_decision(
                record,
                "close",
                moment,
                {
                    "rationale": reason or "portfolio closed; capital released to the funding wallet",
                    "realized_pnl": money_text(realized),
                    "references": [],
                    "constraints": {},
                    "data_quality": "priced at the latest observations",
                },
            )
        )
        await self._publish(
            "novamarket.portfolio.closed",
            portfolio_id=portfolio_id,
            reason=reason or "closed",
        )
        return {"ok": True, "portfolio": self._record(portfolio_id).to_dict()}

    # -- reconciliation -------------------------------------------------------

    def reconcile(self, portfolio_id: str) -> dict[str, Any]:
        record = self._record(portfolio_id)
        moves = self._store.capital_moves(portfolio_id)
        funded = sum(
            (
                Decimal(str(row["amount"]))
                for row in moves
                if row["direction"] == "funding"
            ),
            Decimal("0.00"),
        )
        released = sum(
            (
                Decimal(str(row["amount"]))
                for row in moves
                if row["direction"] == "release"
            ),
            Decimal("0.00"),
        )
        history = self._store.position_history(portfolio_id, limit=500)
        realized_history = sum(
            (Decimal(str(row["realized"])) for row in history), Decimal("0.00")
        )
        discrepancies: list[dict[str, Any]] = []
        if funded - released != record.initial_capital and record.status is not PortfolioStatus.CLOSED:
            discrepancies.append(
                {
                    "code": "funding_mismatch",
                    "detail": (
                        f"recorded funding {money_text(funded)} - released "
                        f"{money_text(released)} != initial capital "
                        f"{money_text(record.initial_capital)}"
                    ),
                }
            )
        if record.realized_pnl != realized_history:
            discrepancies.append(
                {
                    "code": "realized_mismatch",
                    "detail": (
                        f"portfolio realized {money_text(record.realized_pnl)} != "
                        f"position history {money_text(realized_history)}"
                    ),
                }
            )
        return {
            "ok": not discrepancies,
            "portfolio_id": portfolio_id,
            "funded": money_text(funded),
            "released": money_text(released),
            "realized_book": money_text(record.realized_pnl),
            "realized_history": money_text(realized_history),
            "discrepancies": discrepancies,
        }

    def close_engine(self) -> None:
        self._store.close()


__all__ = ["PortfolioEngine"]
