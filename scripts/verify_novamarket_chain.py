"""Run one NovaMarket acceptance chain through the real HTTP application.

The market-data repository is seeded with deterministic, non-pretend bars so
the chain does not depend on an exchange being open or a provider being
available.  Every other step uses the real NM-02..NM-13 services, their real
SQLite stores and the real FastAPI routes.  A phase that does not transition
raises immediately; the script never reports a nominal success for a skipped
step.

Run with::

    PYTHONPATH=src ../.venv/Scripts/python.exe scripts/verify_novamarket_chain.py
"""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception as exc:  # pragma: no cover - dependency error is reported
    raise SystemExit(f"fastapi.testclient is unavailable: {exc}") from exc

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    Freshness,
    OhlcvBar,
    PriceObservation,
)

INSTRUMENT = "XNSE:ACCEPT"
SYMBOL = "ACCEPT"
# The acceptance fixture is deterministic: freeze its clock to a weekday
# market-open moment so an after-hours run does not turn the real freshness
# guard into an accidental failure. Production uses the live clock.
# Bars use the real clock so the forecast's 72-hour freshness window is
# exercised normally. The quote/execution clock is frozen to the same weekday's
# market-open moment, so an after-hours acceptance run still exercises the real
# freshness guard against a genuinely fresh observation.
BASE_TIME = datetime.now(UTC)
ACCEPTANCE_NOW = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
while ACCEPTANCE_NOW.weekday() >= 5:
    ACCEPTANCE_NOW -= timedelta(days=1)
HISTORY_SESSIONS = 300
TRAIN_END_SESSION = 150
VALIDATION_END_SESSION = 295


def _session_dates(now: datetime, count: int = HISTORY_SESSIONS) -> list[datetime]:
    """The last ``count`` weekday sessions, oldest first.

    Bars are stamped at the exchange's 15:30 IST close (10:00 UTC).  Stamping
    them at the wall-clock instant would turn every Friday session into a
    Saturday in IST, and the exchange calendar would drop it.
    """
    dates: list[datetime] = []
    cursor = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if cursor > now:
        cursor -= timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(dates))


def _check(response: Any, label: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise RuntimeError(f"{label} failed: HTTP {response.status_code}: {response.text}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"{label} returned a non-object body: {body!r}")
    return body


def _seed_market(
    nova: NovaControlApplication, *, final_volume: int | None = None
) -> None:
    """Seed closed daily bars and one current observed quote for the chain."""
    service = nova.novamarket
    store = service._market_store
    if store is None:
        raise RuntimeError("market-data store was not initialised")
    # The instrument is registered through HTTP below, before this function.
    sessions = _session_dates(BASE_TIME)
    for index in range(HISTORY_SESSIONS):
        # A rising market with repeated tradable cycles.  The drift keeps the
        # strategy's out-of-sample edge real; the cycle supplies genuine
        # crossovers in every walk-forward fold.  The last four bars (outside
        # validation) form a small pullback and recovery so the live signal is
        # a fresh crossover on the newest closed bar.
        if index == 296:
            close = Decimal("246.00")
        elif index == 297:
            close = Decimal("246.50")
        elif index == 298:
            close = Decimal("246.00")
        elif index == 299:
            close = Decimal("247.00")
        else:
            close = (
                Decimal("100.00")
                + Decimal("0.50") * index
                + Decimal("2.00")
                * Decimal(
                    str(
                        math.sin(
                            2.0 * math.pi * index / 10.0 + math.radians(70.0)
                        )
                    )
                )
            ).quantize(Decimal("0.01"))
        open_price = close - Decimal("0.35")
        high = max(open_price, close) + Decimal("0.80")
        low = min(open_price, close) - Decimal("0.80")
        observed_at = sessions[index]
        bar = OhlcvBar.create(
            instrument_id=INSTRUMENT,
            provider="fixture",
            interval=DataInterval.DAY_1,
            open=open_price,
            high=high,
            low=low,
            close=close,
            # ``final_volume`` is a fixture-only liquidity override used by the
            # paper-trading walkthrough.  Apply it to every bar: the pricing
            # service derives available quantity from the average of the last
            # 30 bars, so overriding only the final bar would not narrow it.
            volume=(
                final_volume if final_volume is not None else 100_000 + index * 100
            ),
            currency="INR",
            observed_at=observed_at,
            retrieved_at=BASE_TIME,
            source_timezone="UTC",
        )
        store.append_bar(bar)
    last_close = close
    latest = ACCEPTANCE_NOW
    store.append_observation(
        PriceObservation.create(
            instrument_id=INSTRUMENT,
            provider="fixture",
            price=last_close,
            currency="INR",
            observed_at=latest,
            retrieved_at=latest,
            source_timezone="UTC",
            freshness=Freshness.FRESH,
        )
    )


def _experiment_payload(
    strategy: dict[str, Any], now: datetime
) -> dict[str, Any]:
    sessions = _session_dates(now)
    return {
        "strategy_id": strategy["strategy_id"],
        "instruments": [INSTRUMENT],
        "train_start": sessions[0].isoformat(),
        "train_end": sessions[TRAIN_END_SESSION].isoformat(),
        "validation_start": sessions[TRAIN_END_SESSION + 1].isoformat(),
        "validation_end": sessions[VALIDATION_END_SESSION].isoformat(),
        "baseline_version": strategy["version"],
        "baseline_config": dict(strategy["config"]),
        "candidate_configs": [
            {**strategy["config"], "fast_period": 4, "slow_period": 10},
        ],
        "method": "grid",
        "objective": "risk_adjusted_return",
        "min_trades": 1,
        "min_observations": 10,
        "approval_policy": {"mode": "manual", "require_shadow": False},
        "source_evidence_ids": [],
    }


def run_chain() -> dict[str, Any]:
    state: dict[str, Any] = {}

    def build(**kwargs: Any) -> NovaControlApplication:
        # Every run needs its own stores. Reusing the project's data directory
        # made a second run read the first run's bars and signals, which is the
        # opposite of an acceptance test.
        kwargs.setdefault("data_dir", tmp)
        application = NovaControlApplication(**kwargs)
        state["app"] = application
        return application

    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch(
            "novacontrol.api.app.NovaControlApplication", side_effect=build
        ), mock.patch(
            "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
        ), mock.patch(
            "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
        ):
            with TestClient(create_app(data_dir=tmp)) as client:
                nova = state.get("app")
                if nova is None:
                    raise RuntimeError("the application was not constructed")
                regression = _check(client.post("/market/enable", json={"reason": "acceptance chain"}), "enable")
                # Freeze only the acceptance data engine's clock. The freshness
                # policy, calendar and execution guard remain the production
                # implementations; the fixture quote is assessed at a real
                # market-open moment rather than after the close.
                market_data = getattr(nova.novamarket, "market_data", None)
                if market_data is None:
                    raise RuntimeError("market-data engine was not initialised")
                market_data._clock = lambda: ACCEPTANCE_NOW
                # Pricing asks the execution engine for its own clock. Keep
                # that clock on the same market-open instant; the store and
                # production callers retain their real-time clock.
                execution_engine = nova.novamarket._ensure_execution()
                execution_engine._clock = lambda: ACCEPTANCE_NOW
                registered = _check(
                    client.post(
                        "/market/instruments",
                        json={
                            "symbol": SYMBOL,
                            "name": "Acceptance Chain",
                            "exchange": "NSE",
                            "mic": "XNSE",
                            "asset_class": "equity",
                            "currency": "INR",
                            "timezone": "Asia/Kolkata",
                            "trading_status": "open",
                            "provider_symbols": {"fixture": SYMBOL},
                        },
                    ),
                    "instrument registration",
                )
                _seed_market(nova)
                if nova.novamarket.market_data is not None:
                    state["latest_after_seed"] = nova.novamarket.market_data.latest(
                        INSTRUMENT
                    ).to_dict()
                bars = _check(
                    client.get(
                        f"/market/data/bars/{INSTRUMENT}?limit=5&ascending=true"
                    ),
                    "seeded bars",
                )
                if len(bars.get("items") or ()) < 5:
                    raise RuntimeError("seeded bars were not visible through the API")

                training = _check(
                    client.post(
                        "/market/forecasts/train",
                        json={
                            "instrument_id": INSTRUMENT,
                            "horizon": "few_sessions",
                        },
                    ),
                    "forecast training",
                )
                if not training.get("selected_model"):
                    raise RuntimeError(f"forecast training selected no model: {training}")
                forecast = _check(
                    client.post(
                        "/market/forecasts",
                        json={"instrument_id": INSTRUMENT, "horizon": "few_sessions"},
                    ),
                    "forecast",
                )

                wallet = _check(
                    client.post("/market/wallets", json={"owner": "acceptance"}),
                    "wallet creation",
                )["wallet"]
                wallet_id = wallet["wallet_id"]
                _check(
                    client.post(
                        f"/market/wallets/{wallet_id}/deposit",
                        json={"amount": "50000.00"},
                    ),
                    "wallet deposit",
                )
                portfolio = _check(
                    client.post(
                        "/market/portfolios",
                        json={
                            "horizon": "short_term",
                            "name": "Acceptance book",
                            "funding_wallet_id": wallet_id,
                            "capital": "20000.00",
                        },
                    ),
                    "portfolio creation",
                )["portfolio"]
                portfolio_id = portfolio["portfolio_id"]

                strategy = _check(
                    client.post(
                        "/market/strategies",
                        json={
                            "catalog_id": "trend_sma",
                            "name": "Acceptance trend",
                            "config": {
                                "fast_period": 3,
                                "slow_period": 9,
                                "horizon_days": 5,
                                "max_data_age_hours": 72,
                            },
                        },
                    ),
                    "strategy creation",
                )["strategy"]
                strategy_id = strategy["strategy_id"]
                _check(
                    client.post(
                        f"/market/strategies/{strategy_id}/activate",
                        json={"reason": "acceptance chain"},
                    ),
                    "strategy activation",
                )

                evaluation = _check(
                    client.post(
                        f"/market/strategies/{strategy_id}/evaluate",
                        json={
                            "portfolio_id": portfolio_id,
                            "instrument_ids": [INSTRUMENT],
                        },
                    ),
                    "strategy evaluation",
                )
                signals = list(evaluation.get("signals") or ())
                if not signals:
                    raise RuntimeError("strategy evaluation returned no signal")
                signal = signals[0]
                if str(signal.get("signal_type")).lower() not in {
                    "buy",
                    "sell",
                    "exit",
                }:
                    raise RuntimeError(
                        "strategy returned a non-actionable signal: "
                        + json.dumps(signal, default=str)
                        + "; latest="
                        + json.dumps(state.get("latest_after_seed"), default=str)
                    )
                proposed = asyncio.run(
                    nova.novamarket.propose_strategy_signal(
                        str(signal["signal_id"]),
                        notional="500.00",
                        actor="acceptance-chain",
                    )
                )
                if not proposed.get("ok"):
                    raise RuntimeError(f"proposal failed: {proposed}")
                proposal = proposed["proposal"]
                if proposal.get("status") != "approved":
                    decision = None
                    if proposal.get("risk_decision_id"):
                        decision = client.get(
                            f"/market/risk/decisions/{proposal['risk_decision_id']}"
                        ).json()
                    raise RuntimeError(
                        f"proposal was not approved: {proposal.get('status')}: "
                        f"{proposal.get('risk_reasons') or proposal.get('risk_outcome')}; "
                        f"decision={json.dumps(decision, default=str)}"
                    )

                order = _check(
                    client.post(
                        "/market/execution/orders",
                        json={"proposal_id": proposal["proposal_id"]},
                    ),
                    "paper order",
                )
                order_row = order["order"]
                if order_row.get("status") not in {"filled", "partially_filled", "open"}:
                    raise RuntimeError(f"paper order was not accepted: {order_row}")

                backtest = _check(
                    client.post(
                        "/market/backtests",
                        json={
                            "name": "Acceptance replay",
                            "strategy_catalog_id": "trend_sma",
                            "strategy_version": "nm09-trend_sma-1.0",
                            "strategy_config": strategy["config"],
                            "instruments": [INSTRUMENT],
                            "start": (BASE_TIME - timedelta(days=115)).isoformat(),
                            "end": (BASE_TIME - timedelta(days=2)).isoformat(),
                            "initial_capital": "100000.00",
                            "execution_config": {
                                "participation_rate": "1",
                                "spread_bps": "0",
                                "impact_coefficient_bps": "0",
                                "volatility_weight": "0",
                                "brokerage_bps": "0",
                                "exchange_bps": "0",
                                "sebi_bps": "0",
                                "stamp_duty_bps": "0",
                                "gst_percent": "0",
                                "minimum_total_fee": "0",
                                "require_fresh_data": False,
                            },
                        },
                    ),
                    "backtest creation",
                )
                backtest_id = backtest["backtest_id"]
                started_backtest = _check(
                    client.post(f"/market/backtests/{backtest_id}/start"),
                    "backtest start",
                )
                if started_backtest.get("status") != "completed":
                    raise RuntimeError(f"backtest did not complete: {started_backtest}")

                shadow = _check(
                    client.post(
                        "/market/shadow/sessions",
                        json={
                            "name": "Acceptance shadow",
                            "portfolio_id": portfolio_id,
                            "instruments": [INSTRUMENT],
                            "strategies": [strategy_id],
                        },
                    ),
                    "shadow creation",
                )
                shadow_id = shadow["session"]["session_id"]
                _check(client.post(f"/market/shadow/sessions/{shadow_id}/start"), "shadow start")
                _check(
                    client.post(
                        f"/market/shadow/sessions/{shadow_id}/cycle", json={}
                    ),
                    "shadow cycle",
                )
                _check(client.post(f"/market/shadow/sessions/{shadow_id}/complete"), "shadow complete")

                evidence = _check(
                    client.post(
                        "/market/learning/evidence",
                        json={
                            "kind": "backtest",
                            "source_ref": backtest_id,
                            "strategy_id": strategy_id,
                            "strategy_version": strategy["version"],
                            "catalog_id": "trend_sma",
                            "config": strategy["config"],
                            "instruments": [INSTRUMENT],
                            "out_of_sample": True,
                        },
                    ),
                    "learning evidence",
                )["evidence"]
                experiment = _check(
                    client.post(
                        "/market/learning/experiments",
                        json=_experiment_payload(strategy, BASE_TIME),
                    ),
                    "learning experiment",
                )["experiment"]
                experiment_id = experiment["experiment_id"]
                evaluation_run = _check(
                    client.post(
                        f"/market/learning/experiments/{experiment_id}/run"
                    ),
                    "learning run",
                )
                if evaluation_run.get("experiment", {}).get("status") != "completed":
                    raise RuntimeError(f"learning experiment did not complete: {evaluation_run}")
                candidates = _check(
                    client.get(
                        f"/market/learning/experiments/{experiment_id}/candidates"
                    ),
                    "learning candidates",
                )["candidates"]
                if not candidates:
                    raise RuntimeError("learning experiment produced no candidates")
                eligible_candidates = [
                    row
                    for row in candidates
                    if row.get("status") in {"pending_approval", "approved"}
                ]
                if not eligible_candidates:
                    raise RuntimeError(
                        "learning experiment produced no eligible candidate: "
                        + json.dumps(candidates, default=str)
                    )
                candidate = eligible_candidates[0]
                if candidate["status"] == "pending_approval":
                    _check(
                        client.post(
                            f"/market/learning/candidates/{candidate['candidate_id']}/approve",
                            json={"actor": "acceptance-chain"},
                        ),
                        "learning approval",
                    )
                promoted = _check(
                    client.post(
                        f"/market/learning/candidates/{candidate['candidate_id']}/promote",
                        json={"actor": "acceptance-chain"},
                    ),
                    "learning promotion",
                )
                return {
                    "ok": True,
                    "lifecycle": regression.get("lifecycle"),
                    "instrument": registered["instrument"]["instrument_id"],
                    "bars": int(bars.get("total") or len(bars.get("items") or ())),
                    "forecast": {
                        "forecast_id": forecast.get("forecast_id"),
                        "horizon": forecast.get("horizon"),
                    },
                    "wallet_id": wallet_id,
                    "portfolio_id": portfolio_id,
                    "strategy_id": strategy_id,
                    "signal_type": signal.get("signal_type"),
                    "proposal_status": proposal.get("status"),
                    "order_status": order_row.get("status"),
                    "backtest_status": started_backtest.get("status"),
                    "shadow_status": "completed",
                    "evidence_status": evidence.get("status"),
                    "experiment_status": evaluation_run.get("experiment", {}).get("status"),
                    "candidate_status": promoted.get("candidate", {}).get("status"),
                }


def main() -> int:
    try:
        result = run_chain()
    except Exception as exc:  # noqa: BLE001 - the CLI reports the first failure
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())