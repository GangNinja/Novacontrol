"""Focused PI-20 prediction-intelligence dashboard tests.

The fixtures are deterministic records that look like the existing PI-04..PI-19
contracts.  They are not presented as real market data, forecasts or outcomes.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unittest
from typing import Any
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.novamarket.events import IMPLEMENTED_EVENT_NAMES
from novacontrol.novamarket.dashboard import (
    DashboardFilters,
    DashboardNotFoundError,
    DashboardValidationError,
    DataStatus,
    PredictionDashboardService,
    authority_contract,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
INSTRUMENT = "XNSE:ALPHA"
OTHER = "XNSE:BETA"


def _forecast(
    forecast_id: str,
    *,
    model_id: str,
    horizon: str,
    reference: float,
    predicted: float,
    generated: str = "2026-06-01T09:00:00+00:00",
    target: str = "2026-06-02T09:00:00+00:00",
    instrument: str = INSTRUMENT,
    validity: str = "valid",
    probability: float | None = None,
    probability_status: str = "",
) -> dict[str, object]:
    return {
        "forecast_id": forecast_id,
        "instrument_id": instrument,
        "exchange": "XNSE",
        "model_id": model_id,
        "model_version": "v1",
        "horizon": horizon,
        "reference_price": reference,
        "predicted_price": predicted,
        "expected_return": (predicted - reference) / reference,
        "direction": "up" if predicted > reference else "down",
        "interval_low": min(reference, predicted) - 1,
        "interval_high": max(reference, predicted) + 1,
        "directional_probability": probability,
        "probability_status": probability_status,
        "forecast_uncertainty": "medium",
        "uncertainty_width": 2,
        "generated_at": generated,
        "target_at": target,
        "reference_at": generated,
        "validity": validity,
        "data_freshness": {"state": "fresh", "as_of": generated},
        "confirmation_stage": "initial",
        "dataset_version": "ds-1",
        "feature_set_version": "fs-1",
    }


class FakeDashboardSource:
    """Deterministic stand-in for the read-only NovaMarket composition root."""

    def __init__(self) -> None:
        self.forecasts = {
            "statistical": [
                _forecast(
                    "pfc_alpha_1d",
                    model_id="naive",
                    horizon="1d",
                    reference=100,
                    predicted=105,
                ),
                _forecast(
                    "pfc_alpha_future",
                    model_id="naive",
                    horizon="1d",
                    reference=100,
                    predicted=110,
                    generated="2026-06-03T09:00:00+00:00",
                    target="2026-06-04T09:00:00+00:00",
                ),
            ],
            "gradient": [
                _forecast(
                    "gbf_alpha_1d",
                    model_id="xgboost",
                    horizon="1d",
                    reference=100,
                    predicted=99,
                    validity="unconfirmed",
                    probability=0.71,
                    probability_status="uncalibrated",
                )
            ],
            "lstm": [],
            "ensemble": [
                _forecast(
                    "ens_alpha_5m",
                    model_id="ensemble",
                    horizon="5m",
                    reference=101,
                    predicted=102,
                    generated="2026-06-01T09:35:00+00:00",
                    target="2026-06-01T09:40:00+00:00",
                )
            ],
            "": [],
        }
        self.models = {
            "statistical": [
                {
                    "model_id": "naive",
                    "model_version": "v1",
                    "status": "active",
                    "trained_at": "2026-05-30T09:00:00+00:00",
                }
            ],
            "gradient": [
                {
                    "model_id": "xgboost",
                    "model_version": "v1",
                    "status": "active",
                    "trained_at": "2026-05-29T09:00:00+00:00",
                }
            ],
            "lstm": [],
            "ensemble": [
                {"model_id": "ensemble", "model_version": "v1", "status": "active"}
            ],
        }
        self.decision = {
            "cycle_id": "mac_1",
            "decision_id": "mdc_1",
            "instrument_id": INSTRUMENT,
            "exchange": "XNSE",
            "horizon": "1d",
            "action": "buy",
            "supporting_agents": ["fin-technical", "fin-regime"],
            "contradicting_agents": ["fin-risk"],
            "supporting_evidence": [{"claim": "momentum positive", "source": "bars"}],
            "contradictory_evidence": [{"claim": "volatility elevated", "source": "risk"}],
            "evidence_strength": "moderate",
            "confidence": 0.7,
            "confidence_kind": "agent_consensus",
            "confidence_provenance": "recorded consensus, not calibrated probability",
            "uncertainty": {
                "missing": ["fundamental filing"],
                "limitations": ["one source was stale"],
                "regime_disagreement": False,
            },
            "portfolio_context": {"portfolio_id": "pf_1", "research": "prr_1"},
            "risk_restrictions": [{"rule": "max position", "status": "allowed"}],
            "unresolved_conflicts": [],
            "explanation": "Technical and regime agents supported a buy; risk flagged volatility.",
            "decided_at": "2026-06-01T12:00:00+00:00",
            "degraded": False,
            "order_submitted": False,
        }
        self.cycle = {
            "cycle_id": "mac_1",
            "instrument_id": INSTRUMENT,
            "exchange": "XNSE",
            "horizon": "1d",
            "status": "completed",
            "as_of": "2026-06-01T12:00:00+00:00",
            "created_at": "2026-06-01T11:50:00+00:00",
            "finished_at": "2026-06-01T12:00:00+00:00",
            "assigned_agents": ["fin-technical", "fin-regime", "fin-risk"],
            "degraded": False,
            "decision": self.decision,
            "responses": [
                {
                    "agent_id": "fin-technical",
                    "role": "technical",
                    "status": "completed",
                    "action": "buy",
                    "findings": [{"summary": "breakout"}],
                    "supporting": [{"claim": "above 20d MA"}],
                    "contradicting": [],
                    "uncertainties": [],
                    "missing": [],
                    "evidence": [{"claim": "above 20d MA"}],
                    "source_references": ["bars"],
                    "confidence": 0.7,
                    "confidence_kind": "agent",
                    "model_version": "v1",
                },
                {
                    "agent_id": "fin-risk",
                    "role": "risk",
                    "status": "completed",
                    "action": "hold",
                    "findings": [{"summary": "volatility elevated"}],
                    "supporting": [],
                    "contradicting": [{"claim": "volatility"}],
                    "uncertainties": ["volatility"],
                    "missing": [],
                    "evidence": [],
                    "source_references": [],
                },
            ],
            "conflicts": [],
        }
        self.evidence = [
            {
                "evidence_id": "ev_now",
                "claim": "Price closed above resistance",
                "summary": "Price closed above resistance",
                "source": "bars",
                "kind": "technical",
                "published_at": "2026-06-01T08:00:00+00:00",
                "independent": True,
            },
            {
                "evidence_id": "ev_future",
                "claim": "Later filing",
                "summary": "Later filing",
                "source": "filing",
                "kind": "fundamental",
                "published_at": "2026-06-04T08:00:00+00:00",
                "independent": True,
            },
        ]
        self.research = {
            "run_id": "prr_1",
            "instrument_id": INSTRUMENT,
            "status": "completed",
            "as_of": "2026-05-30T10:00:00+00:00",
            "profile": {"name": "Alpha Ltd", "sector": "Industrials"},
            "thesis": {
                "bullish": [{"claim": "order book grew", "evidence_id": "rev_1"}],
                "bearish": [{"claim": "leverage elevated", "evidence_id": "deb_1"}],
            },
            "uncertainty": {"missing": ["auditor note"]},
            "limitations": ["third-party filings only"],
        }

    def forecast_list(self, **kwargs: object) -> dict[str, object]:
        engine = str(kwargs.get("engine") or "")
        return {"forecasts": self.forecasts.get(engine, [])}

    def forecast_models(self, **kwargs: object) -> dict[str, object]:
        engine = str(kwargs.get("engine") or "")
        return {"models": self.models.get(engine, [])}

    def forecast_get(self, forecast_id: str) -> dict[str, object]:
        for rows in self.forecasts.values():
            for row in rows:
                if row["forecast_id"] == forecast_id:
                    return dict(row)
        raise KeyError(forecast_id)

    def gradient_explain(self, forecast_id: str) -> dict[str, object]:
        if forecast_id != "gbf_alpha_1d":
            raise KeyError(forecast_id)
        return {
            "summary": "Momentum and volume pushed the forecast lower.",
            "method": "shap",
            "feature_set_version": "fs-1",
            "as_of": "2026-06-01T09:01:00+00:00",
            "contributions": [
                {"feature": "momentum_5d", "contribution": -0.42, "direction": "down"},
                {"feature": "volume_z", "contribution": 0.11, "direction": "up"},
            ],
        }

    def forecast_outcome(self, forecast_id: str) -> dict[str, object]:
        return {
            "forecast_id": forecast_id,
            "actual_direction": "down",
            "direction_correct": True,
        }

    def multiagent_cycle(self, cycle_id: str) -> dict[str, object]:
        if cycle_id != "mac_1":
            raise KeyError(cycle_id)
        return dict(self.cycle)

    def multiagent_history(self, **kwargs: object) -> dict[str, object]:
        return {
            "items": [
                {
                    "decision": dict(self.decision),
                    "cycle_status": "completed",
                    "degraded": False,
                    "conflicts": 0,
                    "advisory_only": True,
                }
            ],
            "total": 1,
            "has_more": False,
        }

    def multiagent_evidence(self, cycle_id: str, **kwargs: object) -> dict[str, object]:
        return {"cycle_id": cycle_id, "items": list(self.evidence), "count": 2}

    def multiagent_runs(self, cycle_id: str) -> dict[str, object]:
        return {"cycle_id": cycle_id, "items": []}

    def multiagent_conflicts(self, cycle_id: str) -> dict[str, object]:
        return {"cycle_id": cycle_id, "items": []}

    def forecast_cycles(self, **kwargs: object) -> list[dict[str, object]]:
        return [{"cycle_id": "fcc_1", "instrument_id": INSTRUMENT, "horizon": "1d"}]

    def forecast_cycle(self, cycle_id: str) -> dict[str, object]:
        return {
            "cycle": {
                "cycle_id": cycle_id,
                "instrument_id": INSTRUMENT,
                "horizon": "1d",
                "target_at": "2026-06-02T09:00:00+00:00",
                "created_at": "2026-06-01T09:00:00+00:00",
                "forecast_version": "fv-1",
            },
            "checkpoints": [
                {
                    "checkpoint": "T-20",
                    "status": "completed",
                    "planned_at": "2026-06-01T09:00:00+00:00",
                    "generated_at": "2026-06-01T09:02:00+00:00",
                    "reference_price": 100,
                    "predicted_direction": "up",
                    "confirmation_result": "confirmed",
                },
                {
                    "checkpoint": "T-5",
                    "status": "stale",
                    "planned_at": "2026-06-01T11:00:00+00:00",
                    "generated_at": "2026-06-01T11:30:00+00:00",
                    "prediction_direction": "up",
                },
            ],
            "missing_checkpoints": [{"checkpoint": "T-1", "status": "missing"}],
        }

    def market_screener(self, **kwargs: object) -> dict[str, object]:
        return {
            "summary": {"gainers": 3, "losers": 2, "advance_decline_ratio": 1.5},
            "session": {"state": "closed"},
            "data_state": "stored_provider_observations",
            "live": False,
            "price_basis": "Last stored provider observation.",
            "rows": [{"symbol": "ALPHA", "price": 100, "change_pct": 1.2}],
            "sectors": [{"sector": "Industrials", "change_pct": 0.8}],
        }

    def market_orderblock_instrument(self, instrument_id: str) -> dict[str, object]:
        if instrument_id != INSTRUMENT:
            return {}
        return {
            "instrument": {
                "instrument_id": instrument_id,
                "name": "Alpha Ltd",
                "symbol": "ALPHA",
                "exchange": "XNSE",
                "sector": "Industrials",
                "currency": "INR",
            },
            "last_price": 100,
            "as_of": "2026-06-01T08:00:00+00:00",
            "freshness": {"state": "stale", "as_of": "2026-06-01T08:00:00+00:00"},
            "data_state": "stored_provider_observations",
            "live": False,
            "chart": {
                "interval": "1d",
                "bars": [
                    {
                        "time": "2026-05-31T00:00:00+00:00",
                        "open": 98,
                        "high": 101,
                        "low": 97,
                        "close": 100,
                        "volume": 1000,
                    },
                    {
                        "time": "2026-06-05T00:00:00+00:00",
                        "open": 100,
                        "high": 103,
                        "low": 99,
                        "close": 102,
                        "volume": 900,
                    },
                ],
            },
            "levels": [{"value": 101, "kind": "resistance"}],
        }

    def latest_regime(self, instrument_id: str = "", **kwargs: object) -> dict[str, object]:
        return {
            "primary_regime": "trending_up",
            "secondary_regime": "low_volatility",
            "status": "available",
            "as_of": "2026-05-31T00:00:00+00:00",
            "confidence_kind": "rule_score",
            "confidence": 0.6,
        }

    def feature_regimes(self, **kwargs: object) -> dict[str, object]:
        return {
            "items": [
                {
                    "regime_id": "rg_2",
                    "instrument_id": INSTRUMENT,
                    "primary_regime": "trending_up",
                    "as_of": "2026-05-31T00:00:00+00:00",
                    "resolution": "1d",
                    "method_version": "v1",
                },
                {
                    "regime_id": "rg_1",
                    "instrument_id": INSTRUMENT,
                    "primary_regime": "sideways",
                    "as_of": "2026-05-20T00:00:00+00:00",
                    "resolution": "1d",
                    "method_version": "v1",
                },
            ]
        }

    def adaptive_dashboard(self) -> dict[str, object]:
        return {
            "latest_regime": {
                "primary_regime": "trending_up",
                "status": "available",
                "as_of": "2026-05-31T00:00:00+00:00",
            },
            "drift": {"state": "none"},
            "degradation": {"state": "stable"},
            "recommendations": [{"kind": "monitor"}],
            "retraining": {"recommended": False},
        }

    def research_history(self, **kwargs: object) -> dict[str, object]:
        return {"runs": [dict(self.research)]}

    def research_latest(self, instrument_id: str, **kwargs: object) -> dict[str, object] | None:
        return dict(self.research)

    def research_detail(self, run_id: str) -> dict[str, object]:
        if run_id != "prr_1":
            raise KeyError(run_id)
        return dict(self.research)

    def research_evidence(self, run_id: str) -> dict[str, object]:
        return {
            "run_id": run_id,
            "evidence": [
                {
                    "evidence_id": "rev_1",
                    "claim": "Revenue grew",
                    "source": "filing",
                    "published_at": "2026-05-20T00:00:00+00:00",
                    "kind": "reported_fact",
                }
            ],
        }

    def evaluation_list(self, limit: int = 50) -> dict[str, object]:
        return {
            "evaluations": [
                {"evaluation_id": "ev_1", "instrument_id": INSTRUMENT, "horizon": "1d"}
            ],
            "total": 1,
        }

    def evaluation(self, evaluation_id: str) -> dict[str, object]:
        return {
            "evaluation_id": evaluation_id,
            "instrument_id": INSTRUMENT,
            "horizon": "1d",
            "model_id": "naive",
            "model_version": "v1",
            "dataset_version": "ds-1",
            "sample_size": 40,
            "evaluated_at": "2026-06-02T10:00:00+00:00",
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "metrics": {
                "mae": 1.2,
                "rmse": 1.8,
                "directional": {"accuracy": 0.6, "f1": 0.58},
                "probabilistic": {"brier": 0.22, "coverage": 0.91},
            },
            "by_regime": [{"regime": "trending_up", "mae": 1.0, "samples": 20}],
            "profitability": None,
        }

    def forecast_accuracy(self, **kwargs: object) -> dict[str, object]:
        return {"metrics": {"mae": 1.2}, "sample_size": 40}

    def outlook_latest(self, instrument_id: str) -> dict[str, object]:
        return {
            "instrument_id": instrument_id,
            "as_of": "2026-06-01T12:00:00+00:00",
            "results": [
                {
                    "horizon": "1d",
                    "family": "swing",
                    "status": "stored",
                    "forecast": _forecast(
                        "ens_alpha_1d",
                        model_id="ensemble",
                        horizon="1d",
                        reference=100,
                        predicted=104,
                    ),
                    "uncertainty": {"class": "medium"},
                },
                {
                    "horizon": "5m",
                    "family": "intraday",
                    "status": "stored",
                    "forecast": _forecast(
                        "ens_alpha_5m",
                        model_id="ensemble",
                        horizon="5m",
                        reference=101,
                        predicted=102,
                        target="2026-06-01T09:40:00+00:00",
                    ),
                    "uncertainty": {"class": "high"},
                },
            ],
            "cross_horizon": {"disagreement": True},
        }

    def portfolios_list(self, **kwargs: object) -> dict[str, object]:
        return {"portfolios": [{"portfolio_id": "pf_1", "name": "Swing", "horizon": "medium"}]}

    def portfolio_detail(self, portfolio_id: str) -> dict[str, object]:
        return {
            "portfolio": {"portfolio_id": portfolio_id, "name": "Swing", "horizon": "medium"},
            "cash": "1000",
            "valuation": {"total": "25000", "invested": "24000", "currency": "NC"},
            "holdings": [
                {
                    "position_id": "pos_1",
                    "instrument_id": INSTRUMENT,
                    "quantity": "10",
                    "average_price": "90",
                    "market_price": "100",
                    "weight": "0.04",
                    "stale": False,
                }
            ],
            "constraints": [],
        }

    def optimization_latest(self, portfolio_id: str) -> dict[str, object] | None:
        if portfolio_id != "pf_1":
            return None
        return {
            "run_id": "por_1",
            "status": "completed",
            "allocation": [{"instrument_id": INSTRUMENT, "weight": 0.2}],
            "risk": {"volatility": 0.18},
            "rebalancing": [{"instrument_id": INSTRUMENT, "delta": 0.02}],
        }

    def training_policies(self, **kwargs: object) -> dict[str, object]:
        return {
            "policies": [
                {
                    "policy_id": "pol_1",
                    "algorithm": "dqn",
                    "policy_version": "v1",
                    "status": "validated",
                    "training_reward": 12.4,
                }
            ]
        }

    def training_runs(self, **kwargs: object) -> dict[str, object]:
        return {"runs": [{"run_id": "trn_1", "status": "completed"}]}

    def reward_evaluations(self, **kwargs: object) -> dict[str, object]:
        return {"evaluations": [{"evaluation_id": "rwe_1", "total_reward": 3.2}]}

    def adaptive_degradation(self, policy_id: str = "", **kwargs: object) -> dict[str, object]:
        return {"items": [{"policy_id": policy_id or "pol_1", "state": "stable"}]}

    def adaptive_retraining(self, policy_id: str = "", **kwargs: object) -> dict[str, object]:
        return {"items": [{"policy_id": policy_id or "pol_1", "recommended": False}]}

    def market_tradingwatch_replay(self, instrument_id: str, **kwargs: object) -> dict[str, object]:
        return {
            "chart": {
                "bars": [
                    {"time": "2026-05-31T00:00:00+00:00", "close": 100},
                    {"time": "2026-06-05T00:00:00+00:00", "close": 102},
                ]
            }
        }

    def radar_instrument_events(self, instrument_id: str, **kwargs: object) -> dict[str, object]:
        return {
            "events": [
                {
                    "event_id": "evt_known",
                    "title": "Known event",
                    "published_at": "2026-06-01T07:00:00+00:00",
                },
                {
                    "event_id": "evt_future",
                    "title": "Later event",
                    "published_at": "2026-06-04T07:00:00+00:00",
                },
            ]
        }


class EmptySource:
    pass


class FailingSource:
    def __getattr__(self, name: str) -> object:
        def fail(*args: object, **kwargs: object) -> object:
            raise RuntimeError(f"{name} unavailable")

        return fail


@pytest.fixture()
def source() -> FakeDashboardSource:
    return FakeDashboardSource()


@pytest.fixture()
def dashboard(source: FakeDashboardSource, tmp_path: Path) -> PredictionDashboardService:
    service = PredictionDashboardService(
        source, store_path=tmp_path / "novamarket_dashboard.sqlite3"
    )
    yield service
    service.close()


def _filters(**kwargs: object) -> DashboardFilters:
    return DashboardFilters(**kwargs)


class DashboardInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.service = PredictionDashboardService(
            FakeDashboardSource(),
            store_path=Path(self._tmp.name) / "dashboard.sqlite3",
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def test_status_and_contract_are_read_only(self) -> None:
        status = self.service.status()
        contract = self.service.contract()
        self.assertTrue(status["ok"])
        self.assertTrue(status["read_only"])
        self.assertEqual(status["store"]["financial_tables"], [])
        self.assertFalse(contract["authorities"]["can_submit_orders"])
        self.assertFalse(contract["authorities"]["can_mutate_portfolio"])
        self.assertEqual(contract["authorities"]["risk_authority"], "NM-08")
        self.assertEqual(contract["authorities"]["execution_authority"], "NM-10")

    def test_preferences_persist_in_an_isolated_table(self) -> None:
        saved = self.service.save_filters(
            {"filters": {"instrument_id": INSTRUMENT, "horizon": "1d"}}
        )
        self.assertTrue(saved["presentation_only"])
        self.assertFalse(saved["financial_mutation"])
        result = self.service.filters()
        self.assertEqual(result["filters"]["instrument_id"], INSTRUMENT)
        connection = sqlite3.connect(Path(self._tmp.name) / "dashboard.sqlite3")
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual(tables, {"pd_dashboard_meta", "pd_dashboard_configs"})

    def test_store_failure_is_reported_not_raised(self) -> None:
        service = PredictionDashboardService(FakeDashboardSource(), store_path=None)
        self.assertFalse(service.status()["initialised"])
        self.assertEqual(
            service.filters()["data_status"], DataStatus.UNAVAILABLE.value
        )
        service.close()


class DashboardOverviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_overview_counts_recorded_forecasts_and_decisions(self) -> None:
        body = self.service.overview()
        self.assertEqual(body["predictions"]["upward"], 3)
        self.assertEqual(body["predictions"]["downward"], 1)
        self.assertEqual(body["decisions"]["buy"], 1)
        self.assertEqual(body["models"]["available_count"], 3)
        self.assertEqual(body["regime"]["primary"], "trending_up")
        self.assertFalse(body["market"]["live"])
        self.assertTrue(body["authorities"]["read_only"])

    def test_missing_source_is_failed_not_zeroed(self) -> None:
        service = PredictionDashboardService(FailingSource(), store_path=":memory:")
        body = service.overview()
        self.assertEqual(body["data_status"], DataStatus.FAILED.value)
        self.assertTrue(body["source_errors"])
        self.assertEqual(body["market"]["live"], False)
        service.close()


class DashboardInstrumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_instrument_keeps_price_timestamp_freshness_and_chart(self) -> None:
        body = self.service.instrument(INSTRUMENT)
        self.assertEqual(body["instrument"]["price"], 100)
        self.assertEqual(body["instrument"]["price_at"], "2026-06-01T08:00:00+00:00")
        self.assertTrue(body["instrument"]["delayed_or_historical"])
        self.assertEqual(body["data_status"], DataStatus.STALE.value)
        self.assertEqual(len(body["chart"]["rows"]), 2)
        self.assertTrue(body["chart"]["supports_ohlc"])
        self.assertTrue(body["forecasts"])

    def test_instrument_without_a_quote_does_not_invent_one(self) -> None:
        body = self.service.instrument(OTHER)
        self.assertIsNone(body["instrument"]["price"])
        self.assertEqual(body["instrument"]["price_at"], "")

    def test_invalid_instrument_is_rejected(self) -> None:
        with self.assertRaises(DashboardValidationError):
            self.service.instrument("ALPHA")


class DashboardForecastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_uncalibrated_confidence_is_not_a_probability(self) -> None:
        body = self.service.forecasts(_filters(instrument_id=INSTRUMENT))
        rows = {row["model_id"]: row for row in body["forecasts"]}
        self.assertIsNone(rows["xgboost"]["probability"])
        self.assertEqual(rows["xgboost"]["probability_status"], "unavailable")
        self.assertEqual(rows["naive"]["probability_status"], "unavailable")
        self.assertIn("calibrated", body["probability_rule"])

    def test_forecast_keeps_interval_uncertainty_and_versions(self) -> None:
        body = self.service.forecasts(_filters(instrument_id=INSTRUMENT))
        row = next(item for item in body["forecasts"] if item["forecast_id"] == "pfc_alpha_1d")
        self.assertEqual(row["interval_low"], 99)
        self.assertEqual(row["interval_high"], 106)
        self.assertEqual(row["uncertainty"]["class"], "medium")
        self.assertEqual(row["feature_set_version"], "fs-1")
        self.assertEqual(row["generated_at"], "2026-06-01T09:00:00+00:00")

    def test_compare_only_joins_matching_reference_targets(self) -> None:
        body = self.service.compare(_filters(instrument_id=INSTRUMENT, horizon="1d"))
        swing = next(
            group
            for group in body["comparisons"]
            if group["horizon"] == "1d" and group["comparable"]
        )
        self.assertGreaterEqual(len(swing["models"]), 2)
        self.assertTrue(swing["comparable"])
        self.assertTrue(swing["disagreement"])
        self.assertIsNone(swing["best_model"])
        self.assertIn("recorded reference", body["comparison_rule"])

    def test_real_forecast_contract_fields_are_mapped(self) -> None:
        class ContractSource(FakeDashboardSource):
            def forecast_list(self, **kwargs: object) -> dict[str, object]:
                engine = str(kwargs.get("engine") or "")
                if engine != "gradient":
                    return {"forecasts": []}
                return {
                    "forecasts": [
                        {
                            "forecast_id": "gbf_contract_15m",
                            "instrument_id": INSTRUMENT,
                            "exchange": "XNSE",
                            "model_id": "xgboost",
                            "model_version": "v2",
                            "forecast_horizon": "15m",
                            "reference_price": 100,
                            "predicted_price": 101,
                            "p_up": 0.55,
                            "p_down": 0.3,
                            "p_neutral": 0.15,
                            "probability_status": "calibrated",
                            "forecast_generation_timestamp": "2026-06-01T09:00:00+00:00",
                            "forecast_target_timestamp": "2026-06-01T09:15:00+00:00",
                            "reference_price_timestamp": "2026-06-01T09:00:00+00:00",
                            "underlying_data_timestamp": "2026-06-01T08:59:00+00:00",
                            "data_freshness_status": "fresh",
                            "forecast_validity": "valid",
                            "lifecycle_stage": "initial",
                        }
                    ]
                }

        service = PredictionDashboardService(ContractSource(), store_path=":memory:")
        body = service.forecasts(_filters(instrument_id=INSTRUMENT, horizon="15m"))
        row = body["forecasts"][0]
        self.assertEqual(row["horizon"], "15m")
        self.assertEqual(row["generated_at"], "2026-06-01T09:00:00+00:00")
        self.assertEqual(row["target_at"], "2026-06-01T09:15:00+00:00")
        self.assertEqual(row["reference_at"], "2026-06-01T09:00:00+00:00")
        self.assertEqual(row["data_freshness"]["as_of"], "2026-06-01T08:59:00+00:00")
        self.assertEqual(row["probability"], 0.55)
        self.assertEqual(row["probabilities"], {"up": 0.55, "down": 0.3, "neutral": 0.15})
        self.assertEqual(row["confirmation_stage"], "initial")
        self.assertEqual(row["validity"], "valid")
        service.close()

    def test_compare_does_not_treat_a_single_model_as_comparable(self) -> None:
        class OneModelSource(FakeDashboardSource):
            def forecast_list(self, **kwargs: object) -> dict[str, object]:
                return {
                    "forecasts": [
                        _forecast(
                            "pfc_a",
                            model_id="naive",
                            horizon="1d",
                            reference=100,
                            predicted=105,
                        ),
                        _forecast(
                            "pfc_b",
                            model_id="naive",
                            horizon="1d",
                            reference=100,
                            predicted=104,
                        ),
                    ]
                }

        service = PredictionDashboardService(OneModelSource(), store_path=":memory:")
        body = service.compare(_filters(instrument_id=INSTRUMENT, horizon="1d"))
        self.assertFalse(body["comparisons"][0]["comparable"])
        self.assertIn("two distinct models", body["comparisons"][0]["reason"])
        service.close()

    def test_horizons_are_separate_and_never_merged(self) -> None:
        body = self.service.horizons(INSTRUMENT)
        self.assertIn("intraday", body["families"])
        self.assertIn("swing", body["families"])
        for group in body["families"].values():
            for forecast in group["forecasts"]:
                self.assertIn(forecast["horizon"], group["horizons"])
        self.assertIn("not combined", body["cross_horizon"]["note"])

    def test_one_hour_horizon_matches_the_prediction_contract(self) -> None:
        self.assertEqual(DashboardFilters(horizon="1h").horizon, "1h")
        self.assertEqual(DashboardFilters(horizon="60m").horizon, "1h")

    def test_filters_are_validated_and_paginated(self) -> None:
        with self.assertRaises(DashboardValidationError):
            DashboardFilters(direction="sideways")
        with self.assertRaises(DashboardValidationError):
            DashboardFilters(limit=1000)
        body = self.service.forecasts(_filters(instrument_id=INSTRUMENT, limit=1))
        self.assertEqual(len(body["forecasts"]), 1)
        self.assertTrue(body["has_more"])


class DashboardLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_late_checkpoints_are_explicit_and_not_relabelled(self) -> None:
        body = self.service.lifecycle()
        cycle = body["cycles"][0]
        t5 = next(item for item in cycle["checkpoints"] if item["checkpoint"] == "T-5")
        self.assertTrue(t5["late"])
        self.assertEqual(t5["status"], "stale")
        self.assertEqual(cycle["missing_checkpoints"][0]["checkpoint"], "T-1")
        self.assertEqual(
            body["note"].split(".")[0],
            "Checkpoint labels are the recorded lifecycle stages",
        )


class DashboardPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_metrics_keep_scope_and_do_not_claim_profitability(self) -> None:
        body = self.service.performance()
        evaluation = body["evaluation"]
        self.assertEqual(evaluation["sample_size"], 40)
        self.assertEqual(evaluation["metrics"]["mae"], 1.2)
        self.assertTrue(evaluation["statistically_valid"])
        self.assertFalse(evaluation["profitability_claimed"])
        self.assertIn("never inferred", body["separation"]["trading_profitability"])

    def test_pi11_nested_metrics_and_evaluation_period_filter(self) -> None:
        class Pi11Source(FakeDashboardSource):
            def evaluation_list(self, limit: int = 50) -> dict[str, object]:
                return {
                    "evaluations": [
                        {
                            "evaluation_id": "ev_old",
                            "instrument_id": INSTRUMENT,
                            "horizon": "1d",
                            "period_start": "2026-01-01",
                            "period_end": "2026-01-31",
                        },
                        {
                            "evaluation_id": "ev_new",
                            "instrument_id": INSTRUMENT,
                            "horizon": "1d",
                            "period_start": "2026-06-01",
                            "period_end": "2026-06-30",
                        },
                    ]
                }

            def evaluation(self, evaluation_id: str) -> dict[str, object]:
                self.asserted = evaluation_id
                return {
                    "evaluation_id": evaluation_id,
                    "instrument_id": INSTRUMENT,
                    "horizon": "1d",
                    "created_at": "2026-07-01T10:00:00+00:00",
                    "samples": 55,
                    "config": {
                        "start": "2026-06-01",
                        "end": "2026-06-30",
                        "models": ["naive", "xgboost"],
                    },
                    "metrics": {
                        "price": {"mae": 1.1},
                        "direction": {"accuracy": 0.61, "confusion_matrix": {"up": {"up": 12}}},
                        "probabilities": {"brier": 0.2},
                        "sample": {"samples": 55, "sufficient": True},
                        "horizons": [{"horizon": "1d", "mae": 1.1}],
                        "regimes": [{"regime": "trending_up", "mae": 1.0}],
                        "stability": [{"period": "2026-06", "mae": 1.1}],
                        "models": {"ensemble": 0.02},
                    },
                    "leakage_audit": {"passed": True},
                }

        service = PredictionDashboardService(Pi11Source(), store_path=":memory:")
        body = service.performance(
            instrument_id=INSTRUMENT,
            horizon="1d",
            evaluation_start="2026-06-01",
            evaluation_end="2026-06-30",
        )
        self.assertEqual(body["selected_evaluation_id"], "ev_new")
        self.assertEqual(body["evaluation"]["sample_size"], 55)
        self.assertEqual(body["evaluation"]["regression"]["mae"], 1.1)
        self.assertEqual(body["evaluation"]["confusion_matrix"]["up"]["up"], 12)
        self.assertEqual(body["evaluation"]["by_horizon"][0]["horizon"], "1d")
        self.assertEqual(body["evaluation"]["by_regime"][0]["regime"], "trending_up")
        self.assertTrue(body["evaluation"]["leakage_audit"]["passed"])
        self.assertTrue(body["evaluation"]["statistically_valid"])
        service.close()

    def test_zero_sample_is_insufficient_evidence(self) -> None:
        class ZeroSource(FakeDashboardSource):
            def evaluation(self, evaluation_id: str) -> dict[str, object]:
                return {**super().evaluation(evaluation_id), "sample_size": 0}

        service = PredictionDashboardService(ZeroSource(), store_path=":memory:")
        body = service.performance()
        self.assertEqual(body["data_status"], DataStatus.INSUFFICIENT_EVIDENCE.value)
        self.assertFalse(body["evaluation"]["statistically_valid"])
        service.close()


class DashboardRegimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_regimes_keep_detection_time_and_transitions(self) -> None:
        body = self.service.regimes(instrument_id=INSTRUMENT)
        self.assertEqual(body["current"]["primary"], "trending_up")
        self.assertEqual(body["current"]["detected_at"], "2026-05-31T00:00:00+00:00")
        self.assertEqual(body["transitions"][0]["from"], "sideways")
        self.assertEqual(body["transitions"][0]["to"], "trending_up")

    def test_unknown_regime_stays_uncertain(self) -> None:
        service = PredictionDashboardService(EmptySource(), store_path=":memory:")
        body = service.regimes()
        self.assertEqual(body["current"]["primary"], "uncertain")
        self.assertEqual(body["data_status"], DataStatus.INSUFFICIENT_EVIDENCE.value)
        service.close()


class DashboardDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_decision_history_is_paginated_and_flattened(self) -> None:
        body = self.service.decisions(instrument_id=INSTRUMENT)
        self.assertEqual(body["decisions"][0]["action"], "buy")
        self.assertEqual(body["decisions"][0]["status"], "completed")
        self.assertFalse(body["decisions"][0]["order_submitted"])

    def test_decision_preserves_agents_evidence_and_conflicts(self) -> None:
        body = self.service.decision("mac_1")
        self.assertEqual(body["decision"]["action"], "buy")
        self.assertEqual(len(body["agents"]), 2)
        self.assertTrue(body["decision"]["supporting_evidence"])
        self.assertTrue(body["decision"]["contradictory_evidence"])
        self.assertFalse(body["decision"]["order_submitted"])
        self.assertIn("not independent", body["evidence_note"])

    def test_decision_agent_filter_scans_paged_history(self) -> None:
        class PagedSource(FakeDashboardSource):
            rows = [
                {
                    "cycle_id": "mac_a",
                    "instrument_id": INSTRUMENT,
                    "cycle_status": "completed",
                    "assigned_agents": ["fin-technical"],
                    "decision": {
                        "action": "buy",
                        "instrument_id": INSTRUMENT,
                        "decided_at": "2026-06-01T12:00:00+00:00",
                    },
                },
                {
                    "cycle_id": "mac_b",
                    "instrument_id": INSTRUMENT,
                    "cycle_status": "rejected",
                    "assigned_agents": ["fin-risk"],
                    "decision": {
                        "action": "hold",
                        "instrument_id": INSTRUMENT,
                        "decided_at": "2026-06-01T13:00:00+00:00",
                    },
                },
            ]

            def multiagent_history(self, **kwargs: object) -> dict[str, object]:
                offset = int(kwargs.get("offset") or 0)
                limit = int(kwargs.get("limit") or 1)
                page = self.rows[offset : offset + limit]
                return {
                    "items": page,
                    "total": len(self.rows),
                    "has_more": offset + len(page) < len(self.rows),
                }

        service = PredictionDashboardService(PagedSource(), store_path=":memory:")
        body = service.decisions(instrument_id=INSTRUMENT, agent_id="fin-risk")
        self.assertEqual(body["total"], 1)
        self.assertEqual([row["cycle_id"] for row in body["decisions"]], ["mac_b"])
        self.assertFalse(body["has_more"])
        service.close()

    def test_decision_explanation_has_all_six_sections(self) -> None:
        body = self.service.explain("mac_1")
        for key in ("what", "why", "supporting", "contradicted_by", "limitations", "aftermath"):
            self.assertIn(key, body)
        self.assertFalse(body["what"]["paper_order_submitted"])
        self.assertEqual(body["why"]["confidence_kind"], "agent_consensus")

    def test_attribution_only_uses_computed_contributions(self) -> None:
        body = self.service.attribution("gbf_alpha_1d")
        self.assertEqual(body["data_status"], DataStatus.AVAILABLE.value)
        self.assertEqual(body["method"], "shap")
        self.assertFalse(body["causal_proof"])
        self.assertEqual(body["contributions"][0]["feature"], "momentum_5d")

    def test_ensemble_constituents_and_lstm_global_importance_are_not_mixed(self) -> None:
        class ExplainShapeSource(FakeDashboardSource):
            def forecast_get(self, forecast_id: str) -> dict[str, object]:
                if forecast_id == "ens_x":
                    return _forecast(
                        "ens_x",
                        model_id="ensemble",
                        horizon="1d",
                        reference=100,
                        predicted=101,
                    ) | {"engine": "ensemble"}
                return _forecast(
                    "lstm_x",
                    model_id="lstm",
                    horizon="1d",
                    reference=100,
                    predicted=101,
                ) | {"engine": "lstm"}

            def ensemble_explain(self, forecast_id: str) -> dict[str, object]:
                return {
                    "method": "constituent_weighting",
                    "constituents": [
                        {"model_id": "naive", "weight": 0.6, "expected_return": 0.01}
                    ],
                    "weights": {"naive": 0.6},
                    "disagreement": {"models": 2},
                    "as_of": "2026-06-01T09:01:00+00:00",
                }

            def lstm_explain(self, forecast_id: str) -> dict[str, object]:
                return {
                    "method": "permutation_importance",
                    "feature_importance": [
                        {"name": "momentum_5d", "mae_increase": 0.04}
                    ],
                    "as_of": "2026-06-01T09:01:00+00:00",
                }

        service = PredictionDashboardService(ExplainShapeSource(), store_path=":memory:")
        ensemble = service.attribution("ens_x")
        self.assertEqual(ensemble["data_status"], DataStatus.AVAILABLE.value)
        self.assertEqual(ensemble["attribution_kind"], "ensemble_model_contributions")
        self.assertEqual(ensemble["model_contributions"][0]["model_id"], "naive")
        self.assertEqual(ensemble["contributions"], [])
        lstm = service.attribution("lstm_x")
        self.assertEqual(lstm["attribution_kind"], "global_importance")
        self.assertEqual(lstm["contributions"][0]["feature"], "momentum_5d")
        self.assertEqual(lstm["contributions"][0]["contribution"], 0.04)
        service.close()

    def test_attribution_is_explicitly_unavailable_for_naive(self) -> None:
        body = self.service.attribution("pfc_alpha_1d")
        self.assertEqual(body["data_status"], DataStatus.UNAVAILABLE.value)
        self.assertTrue(body["unavailable_reason"])

    def test_missing_decision_is_a_not_found(self) -> None:
        with self.assertRaises(DashboardNotFoundError):
            self.service.decision("mac_missing")


class DashboardResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_research_preserves_thesis_evidence_and_uncertainty(self) -> None:
        body = self.service.research(instrument_id=INSTRUMENT)
        self.assertEqual(body["selected_run_id"], "prr_1")
        self.assertTrue(body["thesis"]["bullish"])
        self.assertTrue(body["thesis"]["bearish"])
        self.assertEqual(len(body["evidence"]), 1)
        self.assertIn("reported_financial_facts", body["separation"])

    def test_research_evidence_timeline_keeps_provenance(self) -> None:
        body = self.service.evidence("prr_1", kind="research")
        self.assertEqual(body["timeline"][0]["source"], "filing")
        self.assertEqual(body["timeline"][0]["kind"], "reported_fact")


class DashboardPortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_authoritative_and_experimental_states_are_separate(self) -> None:
        body = self.service.portfolio()
        self.assertTrue(body["current_authoritative"]["authoritative"])
        self.assertIsNone(body["simulated"])
        self.assertTrue(body["recommendation"]["experimental"])
        self.assertTrue(body["recommendation"]["advisory_only"])
        self.assertFalse(body["recommendation"]["changes_portfolio"])
        self.assertFalse(body["isolation"]["can_modify_positions"])
        self.assertEqual(body["current_authoritative"]["total_value_nc"], 25000.0)

    def test_policies_are_experimental_and_not_profit(self) -> None:
        body = self.service.policies()
        self.assertTrue(body["policies"][0]["experimental"])
        self.assertFalse(body["policies"][0]["activation_available"])
        self.assertTrue(body["policies"][0]["training_reward_is_not_profit"])
        self.assertFalse(body["labels"]["activation_available"])


class DashboardReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PredictionDashboardService(FakeDashboardSource(), store_path=":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_replay_excludes_future_forecasts_evidence_news_and_bars(self) -> None:
        body = self.service.replay("mac_1")
        self.assertEqual(body["point_in_time"]["cutoff"], "2026-06-01T12:00:00+00:00")
        ids = {row["forecast_id"] for row in body["reconstructed_at_cutoff"]["forecasts_available"]}
        self.assertIn("pfc_alpha_1d", ids)
        self.assertNotIn("pfc_alpha_future", ids)
        self.assertEqual(body["point_in_time"]["future_forecasts_excluded"], 1)
        self.assertEqual(body["point_in_time"]["future_evidence_excluded"], 1)
        self.assertEqual(body["point_in_time"]["future_news_excluded"], 1)
        self.assertEqual(
            [bar["time"] for bar in body["reconstructed_at_cutoff"]["market_observations"]],
            ["2026-05-31T00:00:00+00:00"],
        )
        self.assertEqual(body["reconstructed_at_cutoff"]["research"]["run_id"], "prr_1")
        self.assertEqual(
            body["reconstructed_at_cutoff"]["regime"]["primary_regime"],
            "trending_up",
        )
        self.assertTrue(body["point_in_time"]["look_ahead_enforced"])

    def test_replay_keeps_recorded_history_separate(self) -> None:
        body = self.service.replay("mac_1")
        self.assertIn("decision", body["recorded_history"])
        self.assertEqual(body["reconstructed_at_cutoff"]["mode"], "reconstructed_read_only")
        self.assertTrue(body["separation"].startswith("recorded operational history"))


class DashboardFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.service = PredictionDashboardService(
            FakeDashboardSource(), store_path=Path(self._tmp.name) / "dashboard.sqlite3"
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def test_filters_are_presentation_only_and_persist(self) -> None:
        saved = self.service.save_filters(
            {"name": "desk", "filters": {"instrument_id": INSTRUMENT, "direction": "up"}}
        )
        self.assertTrue(saved["presentation_only"])
        self.assertFalse(saved["financial_mutation"])
        loaded = self.service.filters(name="desk")
        self.assertEqual(loaded["filters"]["direction"], "up")
        self.assertTrue(loaded["saved"])

    def test_filter_dates_are_ordered(self) -> None:
        with self.assertRaises(DashboardValidationError):
            DashboardFilters(
                evaluation_start="2026-06-02", evaluation_end="2026-06-01"
            )


class DashboardContractTests(unittest.TestCase):
    def test_every_dashboard_route_is_declared_and_non_render(self) -> None:
        routes = {
            (route.method, route.path)
            for route in ApiSurface.default().routes
            if route.path.startswith("/market/intelligence/")
        }
        self.assertEqual(len(routes), 22)
        for route in routes:
            self.assertEqual(ROUTE_CONSUMERS[route], "no-render")

    def test_operation_events_are_declared_on_both_sides(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "web/src/pages/PredictionIntelligence.tsx").read_text(
            encoding="utf-8"
        )
        block = page.split("const DASHBOARD_EVENT_TYPES = [", 1)[1].split(
            "] as const", 1
        )[0]
        for name in (
            "novamarket.agent.cycle.completed",
            "novamarket.research.run.completed",
            "novamarket.optimization.recommendation.created",
            "novamarket.policy.degraded",
        ):
            self.assertIn(f'"{name}"', block)
            self.assertIn(name, IMPLEMENTED_EVENT_NAMES)

    def test_frontend_uses_the_shared_event_stream_without_polling(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "web/src/pages/PredictionIntelligence.tsx").read_text(
            encoding="utf-8"
        )
        vite = (root / "web/vite.config.ts").read_text(encoding="utf-8")
        self.assertIn("new EventSource", page)
        self.assertIn("/events/stream", page)
        self.assertIn("refreshKey: revision", page)
        self.assertNotIn("refreshMs", page)
        self.assertIn('\"/events\"', vite)

    def test_frontend_declares_thirteen_accessible_views(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "web/src/pages/PredictionIntelligence.tsx").read_text(
            encoding="utf-8"
        )
        views_block = page.split("const VIEWS = [", 1)[1].split("] as const", 1)[0]
        self.assertEqual(
            re.findall(r'id: "([a-z_]+)"', views_block),
            [
                "overview",
                "instrument",
                "forecasts",
                "compare",
                "horizons",
                "lifecycle",
                "performance",
                "regimes",
                "decisions",
                "research",
                "portfolio",
                "policies",
                "replay",
            ],
        )
        for attribute in (
            'role="tablist"',
            'role="tab"',
            "aria-selected={view === item.id}",
            'aria-controls="prediction-intel-panel"',
            "tabIndex={view === item.id ? 0 : -1}",
            'role="tabpanel"',
            "aria-labelledby={`prediction-intel-tab-${activeView.id}`}",
            "event.key === \"ArrowRight\"",
            "event.key === \"Home\"",
            "event.key === \"End\"",
        ):
            self.assertIn(attribute, page)

    def test_structured_policy_payloads_are_rendered_as_text(self) -> None:
        # Regression: a PI-14 validation object rendered as a React child
        # blanked the whole workspace when the RL & adaptive view opened.
        root = Path(__file__).resolve().parents[1]
        page = (root / "web/src/pages/PredictionIntelligence.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("function cell(", page)
        self.assertIn("typeof value === \"object\") return JSON.stringify(value)", page)
        for expression in (
            "cell(row.training_reward)",
            "cell(row.validation ?? row.validation_status)",
            "cell(row.regime_suitability)",
            "cell(row.drift ?? row.drift_status)",
        ):
            self.assertIn(expression, page)

    def test_top_level_empty_states_use_a_section_heading(self) -> None:
        # An h1 -> h3 jump is a heading-order defect; EmptyState can now render
        # a section heading for views whose first content is an empty state.
        root = Path(__file__).resolve().parents[1]
        page = (root / "web/src/pages/PredictionIntelligence.tsx").read_text(
            encoding="utf-8"
        )
        ui = (root / "web/src/components/ui.tsx").read_text(encoding="utf-8")
        self.assertIn("level?: 2 | 3;", ui)
        self.assertIn('const Heading = level === 2 ? "h2" : "h3";', ui)
        self.assertGreaterEqual(page.count("level={2}"), 4)

    def test_malformed_instrument_filter_never_reaches_the_api(self) -> None:
        # A typo in the shared filter used to turn every one of the 13 views
        # into a 422 error block; the page now validates before it commits.
        root = Path(__file__).resolve().parents[1]
        page = (root / "web/src/pages/PredictionIntelligence.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("const MIC_SYMBOL = /^[A-Z0-9]{2,8}:[A-Z0-9][A-Z0-9._-]*$/;", page)
        self.assertIn("const updateInstrument = (value: string) => {", page)
        self.assertIn("if (!value || MIC_SYMBOL.test(value)) update(\"instrument_id\", value);", page)
        self.assertIn("MIC-qualified symbol required, e.g. XNSE:ALPHA", page)
        self.assertNotIn(
            'onChange={(event) => update("instrument_id", event.target.value.toUpperCase())}',
            page,
        )

    def test_thesis_factors_render_readable_text_not_raw_json(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "web/src/pages/PredictionIntelligence.tsx").read_text(
            encoding="utf-8"
        )
        styles = (root / "web/src/styles.css").read_text(encoding="utf-8")
        self.assertIn("function FactorList(", page)
        self.assertIn("item?.rationale || item?.claim || item?.summary || item?.factor", page)
        self.assertIn("<FactorList rows={arrayFrom(data.thesis?.bullish)} />", page)
        self.assertIn(".plain-list li { overflow-wrap: anywhere; }", styles)

    def test_authority_contract_blocks_every_mutation(self) -> None:
        contract = authority_contract()
        for key in (
            "can_submit_orders",
            "can_mutate_portfolio",
            "can_write_ledger",
            "can_change_autonomy",
            "can_activate_policies",
            "broker_connected",
            "live_trading",
        ):
            self.assertFalse(contract[key], key)
        self.assertFalse(contract["authorizes_action"])


class DashboardApiIntegrationTests(unittest.TestCase):
    """Drive the real FastAPI app over HTTP with the real NovaMarket subsystem."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:  # pragma: no cover - optional dev dependency
            raise unittest.SkipTest(
                f"fastapi.testclient requires httpx: {exc}"
            ) from exc
        cls._tmp = TemporaryDirectory()
        cls._env = mock.patch.dict(
            os.environ,
            {"NOVACONTROL_API_TOKEN": "pi20-test-token"},
            clear=False,
        )
        cls._env.start()
        from novacontrol.api.app import create_app

        cls._client = TestClient(create_app(data_dir=cls._tmp.name))
        cls._client.__enter__()
        cls._headers = {"Authorization": "Bearer pi20-test-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client.__exit__(None, None, None)
        cls._env.stop()
        cls._tmp.cleanup()

    def test_status_and_overview_are_available(self) -> None:
        status = self._client.get(
            "/market/intelligence/status", headers=self._headers
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["phase"], "PI-20")
        overview = self._client.get(
            "/market/intelligence/overview", headers=self._headers
        )
        self.assertEqual(overview.status_code, 200, overview.text)
        body = overview.json()
        self.assertIn("data_status", body)
        self.assertFalse(body["authorities"]["can_submit_orders"])
        self.assertFalse(body["authorities"]["can_mutate_portfolio"])

    def test_dashboard_routes_require_auth(self) -> None:
        response = self._client.get("/market/intelligence/status")
        self.assertEqual(response.status_code, 401)

    def test_filter_preset_round_trip_through_http(self) -> None:
        response = self._client.put(
            "/market/intelligence/filters",
            headers=self._headers,
            json={"name": "http", "filters": {"instrument_id": INSTRUMENT}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        loaded = self._client.get(
            "/market/intelligence/filters?name=http", headers=self._headers
        )
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["filters"]["instrument_id"], INSTRUMENT)
        self.assertFalse(loaded.json()["presentation_only"] is False)


class OperationAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    """Committed PI-17/18/19 operations and policy health reach the one bus."""

    async def asyncSetUp(self) -> None:
        from novacontrol.core.events import EventBus
        from novacontrol.novamarket.service import NovaMarketService

        self.bus = EventBus()
        self.events: list[Any] = []

        async def capture(event: Any) -> None:
            self.events.append(event)

        await self.bus.subscribe("*", capture)
        self.service = NovaMarketService()
        self.service.attach_event_bus(self.bus)
        self.events.clear()

    def _names(self) -> list[str]:
        return [str(event.type) for event in self.events]

    async def test_completed_cycle_is_announced_once(self) -> None:
        await self.service.announce_agent_cycle(
            {
                "cycle": {
                    "cycle_id": "mac_1",
                    "instrument_id": INSTRUMENT,
                    "horizon": "1d",
                    "status": "completed",
                },
                "decision": {"action": "buy"},
            }
        )
        self.assertEqual(self._names(), ["novamarket.agent.cycle.completed"])
        self.assertEqual(self.events[0].payload["action"], "buy")
        self.assertEqual(self.events[0].payload["cycle_id"], "mac_1")

    async def test_idempotent_cycle_reuse_is_not_reannounced(self) -> None:
        await self.service.announce_agent_cycle(
            {
                "idempotent_reuse": True,
                "cycle": {"cycle_id": "mac_1", "status": "completed"},
                "decision": {"action": "buy"},
            }
        )
        self.assertEqual(self.events, [])

    async def test_terminal_research_run_is_announced(self) -> None:
        await self.service.announce_research_run(
            {"run_id": "prr_1", "instrument_id": INSTRUMENT, "status": "running"}
        )
        self.assertEqual(self.events, [])
        await self.service.announce_research_run(
            {
                "run_id": "prr_1",
                "instrument_id": INSTRUMENT,
                "status": "completed",
                "thesis": {"stance": "neutral"},
            }
        )
        self.assertEqual(self._names(), ["novamarket.research.run.completed"])
        self.assertEqual(self.events[0].payload["stance"], "neutral")

    async def test_optimization_recommendation_counts_its_intents(self) -> None:
        await self.service.announce_optimization_run(
            {"run": {"run_id": "opt_1", "status": "insufficient_data"}}
        )
        self.assertEqual(self.events, [])
        await self.service.announce_optimization_run(
            {
                "run": {
                    "run_id": "opt_1",
                    "portfolio_id": "pf_1",
                    "status": "completed",
                    "rebalancing": {"action": "rebalance", "intents": [{}, {}, {}]},
                }
            }
        )
        self.assertEqual(
            self._names(), ["novamarket.optimization.recommendation.created"]
        )
        self.assertEqual(self.events[0].payload["recommendation_count"], 3)

    async def test_only_new_degradation_alerts_are_announced(self) -> None:
        alert = {
            "policy_id": "pol_1",
            "status": "persistent_degradation",
            "trigger_count": 2,
        }
        await self.service.announce_policy_degradation(
            {"degradation": alert, "inserted": False}
        )
        self.assertEqual(self.events, [])
        await self.service.announce_policy_degradation(
            {"degradation": alert, "inserted": True}
        )
        self.assertEqual(self._names(), ["novamarket.policy.degraded"])
        self.assertTrue(self.events[0].payload["retraining_recommended"])
