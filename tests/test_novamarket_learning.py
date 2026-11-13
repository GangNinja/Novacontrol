"""NM-13 controlled adaptive learning tests.

The evidence, monitoring and real evaluation tests use deterministic market
bars and the real NM-11 replay engine.  Governance tests replace only the
evaluation result so approval, promotion and rollback can be exercised without
depending on which candidate happens to win on a synthetic series.
"""

from __future__ import annotations

import asyncio
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from novacontrol.novamarket.learning import (
    CandidateStatus,
    EvidenceStatus,
    ExperimentStatus,
    LearningNotFoundError,
    LearningService,
    LearningStateError,
    LearningValidationError,
    ShadowValidationStatus,
)
from novacontrol.novamarket.service import NovaMarketService

NOW = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)
POLICY = {
    "max_position_weight": "1",
    "max_sector_weight": "1",
    "max_invested_fraction": "1",
    "min_cash_fraction": "0",
    "max_drawdown_pct": "100",
    "max_volatility_pct": "1000",
    "min_liquidity_value": "0",
    "max_turnover": "2",
    "max_data_age_hours": 10000,
    "max_state_age_hours": 10000,
    "min_observations": 2,
    "max_aggregate_sector_weight": "1",
}
BASELINE_CONFIG = {
    "fast_period": 2,
    "slow_period": 5,
    "horizon_days": 5,
    "max_data_age_hours": 720,
}


def _closes(count: int = 180) -> list[float]:
    return [100.0 + 18.0 * math.sin(index / 8.0) + index * 0.05 for index in range(count)]


class _Bar:
    def __init__(self, index: int, close: float, count: int) -> None:
        self.instrument_id = "XNSE:ALPHA"
        self.observed_at = NOW - timedelta(days=count - 1 - index)
        self.available_at = self.observed_at
        self.open = close
        self.high = close + 1
        self.low = close - 1
        self.close = close
        self.volume = 10000
        self.provider = "fixture"
        self.partial = False


class _Page:
    def __init__(self, items: list[_Bar]) -> None:
        self.items = items
        self.total = len(items)


class _Market:
    def __init__(self, count: int = 180) -> None:
        self._rows = [
            _Bar(index, close, count)
            for index, close in enumerate(_closes(count))
        ]

    def bars(
        self,
        instrument_id: str,
        *,
        interval: object = None,
        start: datetime | None = None,
        end: datetime | None = None,
        provider: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        ascending: bool = False,
    ) -> _Page:
        rows = [
            row
            for row in self._rows
            if (start is None or row.observed_at >= start)
            and (end is None or row.observed_at <= end)
        ]
        if not ascending:
            rows = list(reversed(rows))
        page = rows[offset:] if limit is None else rows[offset : offset + limit]
        return _Page(page)


def _strategy(_: str = "strat_1") -> dict[str, object]:
    return {
        "strategy_id": "strat_1",
        "name": "trend",
        "catalog_id": "trend_sma",
        "version": "v1",
        "status": "active",
        "config": dict(BASELINE_CONFIG),
        "horizons": ["short_term"],
    }


def _evidence(
    *,
    kind: str = "backtest",
    source_ref: str = "bt_1",
    window_end: datetime | None = None,
    observations: int = 40,
    metrics: dict[str, object] | None = None,
    in_sample: bool = False,
    out_of_sample: bool = True,
) -> dict[str, object]:
    end = window_end or (NOW - timedelta(days=5))
    return {
        "kind": kind,
        "source_ref": source_ref,
        "strategy_id": "strat_1",
        "strategy_version": "v1",
        "catalog_id": "trend_sma",
        "config": dict(BASELINE_CONFIG),
        "window_start": (end - timedelta(days=30)).isoformat(),
        "window_end": end.isoformat(),
        "observations": observations,
        "metrics": metrics or {"total_return": 0.05, "sharpe": 1.0},
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "instruments": ["XNSE:ALPHA"],
        "regimes": ["bull_low_volatility"],
    }


def _experiment_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "strategy_id": "strat_1",
        "instruments": ["XNSE:ALPHA"],
        "train_start": (NOW - timedelta(days=170)).isoformat(),
        "train_end": (NOW - timedelta(days=60)).isoformat(),
        "validation_start": (NOW - timedelta(days=60)).isoformat(),
        "validation_end": (NOW - timedelta(days=5)).isoformat(),
        "method": "grid",
        "seed": 7,
        "min_trades": 1,
        "min_observations": 10,
        "candidate_configs": [
            {**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8},
            {**BASELINE_CONFIG, "fast_period": 4, "slow_period": 10},
        ],
        "approval_policy": {"mode": "manual"},
    }
    payload.update(overrides)
    return payload


def _eligible_result() -> dict[str, object]:
    return {
        "ok": True,
        "eligible": True,
        "status": "pending_approval",
        "reasons": [],
        "train": {"total_return": 0.04, "observations": 60, "trades": 4},
        "validation": {
            "total_return": 0.03,
            "observations": 40,
            "trades": 3,
            "sharpe": 1.1,
            "max_drawdown": -0.03,
            "total_fees": "10",
            "total_slippage": "2",
        },
        "baseline": {
            "train": {"total_return": 0.02},
            "validation": {"total_return": 0.01, "max_drawdown": -0.05, "sharpe": 0.8},
        },
        "overfitting": {"flagged": False, "gap": 0.01},
        "walk_forward": {"ok": True, "count": 3, "consistency": 0.7},
        "sensitivity": {"available": True, "return_range": 0.05},
        "regime": {"available": True, "regimes": {"bull": {"observations": 20}}},
        "risk": {
            "compliant": True,
            "reasons": [],
            "policy_version": "policy-v1",
        },
        "score": 0.08,
        "out_of_sample": True,
    }


class _FakeShadow:
    def __init__(self, *, decisions: int = 4, candidate_return: str = "2.0", baseline_return: str = "1.0") -> None:
        self.decisions = decisions
        self.candidate_return = candidate_return
        self.baseline_return = baseline_return
        self.sessions: dict[str, dict[str, object]] = {}
        self.counter = 0

    async def create(self, payload: dict[str, object]) -> dict[str, object]:
        self.counter += 1
        session_id = f"shs_{self.counter}"
        self.sessions[session_id] = dict(payload)
        return {"session": {"session_id": session_id, "status": "created"}}

    async def start(self, session_id: str) -> dict[str, object]:
        return {"session": {"session_id": session_id, "status": "running"}}

    async def run_cycle(
        self, session_id: str, *, observations: object = None
    ) -> dict[str, object]:
        return {"counts": {"decisions": self.decisions}}

    def _candidate(self, session_id: str) -> bool:
        payload = self.sessions[session_id]
        strategy = (payload.get("strategies") or [{}])[0]  # type: ignore[index]
        return "candidate" in str(strategy.get("strategy_id") or "")

    def analytics(self, session_id: str) -> dict[str, object]:
        candidate = self._candidate(session_id)
        return {
            "decisions": {"total": self.decisions},
            "outcomes": {
                "mean_return_pct": self.candidate_return if candidate else self.baseline_return,
                "win_rate_pct": "55" if candidate else "50",
                "resolved": 2,
            },
            "risk": {"approved": 4 if candidate else 3, "rejected": 0},
        }

    def history(self, session_id: str) -> dict[str, object]:
        return {
            "timestamps": {
                "started_at": NOW.isoformat(),
                "completed_at": NOW.isoformat(),
            }
        }

    async def complete(self, session_id: str) -> dict[str, object]:
        return {"session": {"session_id": session_id, "status": "completed"}}


class _LearningCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.applied: list[tuple[str, dict[str, object]]] = []

    def service(
        self,
        *,
        policy: dict[str, object] | None = None,
        shadow: object | None = None,
        strategy_loader: object = _strategy,
        paper_loader: object | None = None,
        backtest_loader: object | None = None,
        shadow_loader: object | None = None,
        max_concurrency: int = 2,
        min_instruments: int = 1,
        min_regimes: int = 1,
        max_cpu_seconds: float = 30.0,
        max_memory_mb: float = 512.0,
    ) -> LearningService:
        def apply(strategy_id: str, config: object) -> dict[str, object]:
            self.applied.append((strategy_id, dict(config)))  # type: ignore[arg-type]
            return {"strategy_id": strategy_id, "applied": True}

        service = LearningService(
            Path(self._tmp.name) / "learning.sqlite3",
            market_data=_Market(),
            strategy_loader=strategy_loader,  # type: ignore[arg-type]
            risk_policy_loader=lambda version: policy or POLICY,
            paper_loader=paper_loader,  # type: ignore[arg-type]
            backtest_loader=backtest_loader,  # type: ignore[arg-type]
            shadow_loader=shadow_loader,  # type: ignore[arg-type]
            shadow_service=shadow,
            strategy_applier=apply,
            clock=lambda: NOW,
            min_sample_size=3,
            min_trades=1,
            min_observations=10,
            max_concurrency=max_concurrency,
            min_instruments=min_instruments,
            min_regimes=min_regimes,
            max_cpu_seconds=max_cpu_seconds,
            max_memory_mb=max_memory_mb,
        )
        self.addAsyncCleanup(service.close)
        return service


class EvidenceEligibilityTests(_LearningCase):
    async def test_accepted_evidence_keeps_provenance_and_is_idempotent(self) -> None:
        service = self.service()
        first = service.record_evidence(_evidence())
        self.assertEqual(first["evidence"]["status"], "accepted")
        self.assertEqual(first["evidence"]["kind"], "backtest")
        self.assertIn("historical simulation", first["evidence"]["reliability"])
        self.assertFalse(first["evidence"]["is_live_performance"])

        second = service.record_evidence(_evidence())
        self.assertTrue(second["duplicate"])
        self.assertEqual(service.evidence_rows()["total"], 1)

    async def test_missing_sample_and_future_data_are_rejected(self) -> None:
        service = self.service()
        small = service.record_evidence(_evidence(observations=1, source_ref="bt_small"))
        self.assertEqual(small["evidence"]["status"], "rejected")
        self.assertTrue(any("observation" in reason for reason in small["evidence"]["reasons"]))

        future = service.record_evidence(
            _evidence(
                source_ref="bt_future",
                window_end=NOW + timedelta(days=1),
            )
        )
        self.assertEqual(future["evidence"]["status"], "rejected")
        self.assertTrue(any("future" in reason for reason in future["evidence"]["reasons"]))

    async def test_stale_and_in_sample_evidence_are_graded(self) -> None:
        service = self.service()
        stale = service.record_evidence(
            _evidence(
                source_ref="bt_stale",
                window_end=NOW - timedelta(days=200),
            )
        )
        self.assertEqual(stale["evidence"]["status"], "rejected")
        self.assertTrue(any("older" in reason for reason in stale["evidence"]["reasons"]))

        in_sample = service.record_evidence(
            _evidence(
                source_ref="bt_in_sample",
                in_sample=True,
                out_of_sample=False,
                observations=20,
            )
        )
        self.assertEqual(in_sample["evidence"]["status"], "low_confidence")
        self.assertEqual(in_sample["evidence"]["confidence"], "0.50")

    async def test_invalid_kind_is_refused(self) -> None:
        service = self.service()
        with self.assertRaises(LearningValidationError):
            service.record_evidence({**_evidence(), "kind": "live"})

    async def test_missing_outcomes_and_malformed_observations_are_refused(self) -> None:
        service = self.service()
        missing = service.record_evidence(
            _evidence(metrics={"trades": 4})
        )["evidence"]
        self.assertEqual(missing["status"], "rejected")
        self.assertTrue(
            any("no usable return" in item for item in missing["reasons"])
        )
        with self.assertRaises(LearningValidationError):
            service.record_evidence({**_evidence(), "observations": "many"})

    async def test_paper_import_preserves_realized_pnl_fees_and_slippage(self) -> None:
        def paper_loader(source_ref: str) -> dict[str, object]:
            return {
                "strategy_id": "strat_1",
                "strategy_version": "v1",
                "catalog_id": "trend_sma",
                "config": dict(BASELINE_CONFIG),
                "window_start": (NOW - timedelta(days=20)).isoformat(),
                "window_end": (NOW - timedelta(days=2)).isoformat(),
                "regimes": ["bull_low_volatility"],
                "orders": [{"order_id": "ord_1"}],
                "fills": [
                    {
                        "fill_id": "fill_1",
                        "order_id": "ord_1",
                        "instrument_id": "XNSE:ALPHA",
                        "total_fees": "12.50",
                        "slippage_amount": "3.25",
                        "position_effect": {"realized_delta": "25.00"},
                    },
                    {
                        "fill_id": "fill_2",
                        "order_id": "ord_1",
                        "instrument_id": "XNSE:ALPHA",
                        "total_fees": "7.50",
                        "slippage_amount": "1.75",
                        "position_effect": {"realized_delta": "-5.00"},
                    },
                    {
                        "fill_id": "fill_3",
                        "order_id": "ord_1",
                        "instrument_id": "XNSE:ALPHA",
                        "total_fees": "0.00",
                        "slippage_amount": "0.00",
                        "position_effect": {"realized_delta": "0.00"},
                    },
                ],
            }

        service = self.service(paper_loader=paper_loader)
        evidence = service.record_evidence(
            {"kind": "paper", "source_ref": "paper_1"}
        )["evidence"]
        self.assertEqual(evidence["status"], "low_confidence")
        self.assertTrue(
            any("small sample" in item for item in evidence["reasons"])
        )
        self.assertEqual(evidence["metrics"]["trades"], 3)
        self.assertEqual(evidence["metrics"]["realized_pnl"], 20.0)
        self.assertEqual(evidence["metrics"]["fees"], 20.0)
        self.assertEqual(evidence["metrics"]["slippage"], 5.0)
        self.assertEqual(evidence["metrics"]["return_pct"], 0.02)
        self.assertEqual(evidence["metrics"]["win_rate_pct"], 50.0)
        self.assertEqual(evidence["instrument_coverage"], 1)
        self.assertEqual(evidence["regime_coverage"], 1)
        attribution = evidence["metrics"]["by_instrument"]["XNSE:ALPHA"]
        self.assertEqual(attribution["trades"], 3)
        self.assertEqual(attribution["realized_pnl"], 20.0)
        self.assertEqual(attribution["fees"], 20.0)
        self.assertEqual(attribution["slippage"], 5.0)
        self.assertEqual(attribution["win_rate_pct"], 50.0)
        self.assertTrue(evidence["provenance"]["simulated"])

    async def test_backtest_import_synthesizes_per_instrument_attribution(self) -> None:
        def backtest_loader(source_ref: str) -> dict[str, object]:
            return {
                "config": {
                    "instruments": ["XNSE:ALPHA", "XNSE:BETA"],
                    "strategy_id": "strat_1",
                    "strategy_version": "v1",
                    "strategy_catalog_id": "trend_sma",
                    "strategy_config": dict(BASELINE_CONFIG),
                },
                "metrics": {"observations": 40, "total_return": 0.05, "sharpe": 1.0},
                "trades": [
                    {"instrument_id": "XNSE:ALPHA", "realized_pnl": "25.00", "fees": "2.00", "slippage": "1.00"},
                    {"instrument_id": "XNSE:ALPHA", "realized_pnl": "-5.00", "fees": "1.00", "slippage": "0.50"},
                    {"instrument_id": "XNSE:BETA", "realized_pnl": "10.00", "fees": "0.50", "slippage": "0.25"},
                ],
                "robustness": {"regimes": ["bull_low_volatility"]},
            }

        service = self.service(backtest_loader=backtest_loader)
        evidence = service.record_evidence(
            {"kind": "backtest", "source_ref": "bt_1"}
        )["evidence"]
        self.assertEqual(evidence["status"], "accepted")
        self.assertEqual(evidence["instrument_coverage"], 2)
        attribution = evidence["metrics"]["by_instrument"]
        self.assertEqual(attribution["XNSE:ALPHA"]["realized_pnl"], 20.0)
        self.assertEqual(attribution["XNSE:BETA"]["realized_pnl"], 10.0)
        self.assertEqual(attribution["XNSE:ALPHA"]["fees"], 3.0)
        self.assertEqual(attribution["XNSE:ALPHA"]["wins"], 1)
        self.assertEqual(attribution["XNSE:ALPHA"]["losses"], 1)

    async def test_coverage_and_regime_are_hard_gates(self) -> None:
        service = self.service()
        rejected = service.record_evidence(
            {**_evidence(), "instruments": [], "regimes": []}
        )["evidence"]
        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(
            any("instrument coverage 0" in item for item in rejected["reasons"])
        )
        self.assertTrue(any("regime coverage 0" in item for item in rejected["reasons"]))

        strict = self.service(min_instruments=2, min_regimes=2)
        under = strict.record_evidence(_evidence(source_ref="bt_strict"))["evidence"]
        self.assertEqual(under["status"], "rejected")
        self.assertTrue(any("instrument coverage 1" in item for item in under["reasons"]))
        self.assertTrue(any("regime coverage 1" in item for item in under["reasons"]))

    async def test_resource_ceilings_are_reported_and_enforced(self) -> None:
        service = self.service(max_cpu_seconds=0.000001, max_memory_mb=0.001)
        status = service.status()
        self.assertTrue(status["resources"]["enforced"])
        self.assertEqual(status["coverage_gates"]["min_instruments"], 1)
        with self.assertRaises(LearningStateError):
            service._enforce_resource_limits(0.0, "cand_limit")

    async def test_shadow_import_keeps_instrument_and_horizon_breakdowns(self) -> None:
        def shadow_loader(source_ref: str) -> dict[str, object]:
            return {
                "strategy_id": "strat_1",
                "strategy_version": "v1",
                "catalog_id": "trend_sma",
                "config": dict(BASELINE_CONFIG),
                "started_at": (NOW - timedelta(days=20)).isoformat(),
                "completed_at": (NOW - timedelta(days=2)).isoformat(),
                "regimes": ["bull_low_volatility"],
                "analytics": {
                    "decisions": {"total": 12},
                    "outcomes": {
                        "mean_return_pct": "2.5",
                        "win_rate_pct": "58",
                        "resolved": 8,
                    },
                    "risk": {"approved": 9, "rejected": 3},
                    "by_instrument": {
                        "XNSE:ALPHA": {"resolved": 8, "mean_return_pct": "2.5"}
                    },
                    "by_horizon": {
                        "short_term": {"resolved": 8, "mean_return_pct": "2.5"}
                    },
                },
            }

        service = self.service(shadow_loader=shadow_loader)
        evidence = service.record_evidence(
            {"kind": "shadow", "source_ref": "shadow_1"}
        )["evidence"]
        self.assertEqual(evidence["status"], "accepted")
        self.assertEqual(evidence["metrics"]["by_instrument"]["XNSE:ALPHA"]["resolved"], 8)
        snapshot = service.monitor("strat_1")["snapshots"][2]
        self.assertEqual(snapshot["kind"], "shadow")
        self.assertEqual(snapshot["metrics"]["by_instrument"], {"XNSE:ALPHA": 2.5})
        self.assertEqual(snapshot["metrics"]["by_horizon"], {"short_term": 2.5})

    async def test_invalid_or_reversed_timestamps_are_refused(self) -> None:
        service = self.service()
        with self.assertRaises(LearningValidationError):
            service.record_evidence(
                {**_evidence(), "window_start": "not-a-timestamp"}
            )
        reversed_window = {
            **_evidence(),
            "window_start": (NOW - timedelta(days=1)).isoformat(),
            "window_end": (NOW - timedelta(days=5)).isoformat(),
        }
        rejected = service.record_evidence(reversed_window)["evidence"]
        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(
            any("window_start is after window_end" in item for item in rejected["reasons"])
        )


class MonitoringTests(_LearningCase):
    async def test_deterioration_and_divergence_are_reported_without_averaging(self) -> None:
        service = self.service()
        service.record_evidence(
            _evidence(
                source_ref="bt_previous",
                window_end=NOW - timedelta(days=35),
                metrics={
                    "total_return": 0.08,
                    "sharpe": 1.2,
                    "volatility_pct": 2.0,
                    "signals": 100,
                    "exposure_pct": 40,
                    "max_drawdown_pct": -5,
                    "win_rate_pct": 60,
                },
            )
        )
        service.record_evidence(
            _evidence(
                source_ref="bt_current",
                window_end=NOW - timedelta(days=5),
                metrics={
                    "total_return": -0.03,
                    "sharpe": 0.2,
                    "volatility_pct": 10.0,
                    "signals": 50,
                    "exposure_pct": 60,
                    "max_drawdown_pct": -12,
                    "win_rate_pct": 40,
                },
            )
        )
        service.record_evidence(
            _evidence(
                kind="paper",
                source_ref="paper_current",
                window_end=NOW - timedelta(days=5),
                metrics={"return_pct": -10.0, "win_rate_pct": 40},
            )
        )
        service.record_evidence(
            _evidence(
                kind="shadow",
                source_ref="shadow_current",
                window_end=NOW - timedelta(days=5),
                metrics={"return_pct": 6.0, "win_rate_pct": 55},
            )
        )
        result = service.monitor("strat_1", window_days=30)
        labels = {row["kind"]: row["deterioration"] for row in result["snapshots"]}
        self.assertEqual(labels["backtest"], "degraded")
        self.assertTrue(
            any(row["kind"] == "performance_deterioration" for row in result["alerts"])
        )
        kinds = {row["kind"] for row in result["alerts"]}
        for kind in (
            "volatility_spike",
            "signal_shift",
            "risk_increase",
            "instability",
        ):
            self.assertIn(kind, kinds)
        self.assertIsNotNone(result["divergence"])
        self.assertGreater(result["divergence"]["spread_pct"], 5.0)
        self.assertEqual(
            set(result["divergence"]["returns_pct"]),
            {"backtest", "paper", "shadow"},
        )

    async def test_insufficient_window_withholds_metrics(self) -> None:
        service = self.service()
        service.record_evidence(_evidence(observations=2, source_ref="bt_tiny"))
        result = service.monitor("strat_1", kind="backtest", window_days=30)
        snapshot = result["snapshots"][0]
        self.assertFalse(snapshot["enough_evidence"])
        self.assertEqual(snapshot["deterioration"], "insufficient_evidence")
        self.assertFalse(snapshot["metrics"]["available"])
        self.assertNotIn("win_rate_pct", snapshot["metrics"])


class ExperimentTests(_LearningCase):
    async def test_real_nm11_evaluation_records_training_and_validation(self) -> None:
        service = self.service()
        created = service.create_experiment(_experiment_payload())
        experiment_id = created["experiment"]["experiment_id"]
        self.assertEqual(created["experiment"]["status"], "created")
        self.assertEqual(len(created["candidates"]), 2)
        replay = service.create_experiment(_experiment_payload())
        self.assertTrue(replay["replayed"])

        result = await service.run_experiment(experiment_id)
        self.assertEqual(result["experiment"]["status"], "completed")
        self.assertEqual(result["experiment"]["results"]["evaluated"], 2)
        candidates = service.candidates(experiment_id=experiment_id)["candidates"]
        self.assertEqual(len(candidates), 2)
        for candidate in candidates:
            self.assertIn(candidate["status"], {"pending_approval", "rejected", "evaluation_failed"})
            self.assertIn("train", candidate["evaluation"])
            self.assertIn("validation", candidate["evaluation"])
            self.assertIn("baseline", candidate["evaluation"])
            self.assertIn("risk", candidate["evaluation"])
            self.assertTrue(candidate["evaluation"].get("out_of_sample"))
            if candidate["status"] != "evaluation_failed":
                for key in (
                    "overfitting",
                    "walk_forward",
                    "sensitivity",
                    "regime",
                    "costs_included",
                ):
                    self.assertIn(key, candidate["evaluation"], key)

    async def test_parameter_bounds_and_windows_are_validated(self) -> None:
        service = self.service()
        with self.assertRaises(LearningValidationError):
            service.create_experiment(
                _experiment_payload(
                    validation_end=(NOW + timedelta(days=1)).isoformat()
                )
            )
        with self.assertRaises(LearningValidationError):
            service.create_experiment(
                _experiment_payload(
                    candidate_configs=[{"fast_period": 90, "slow_period": 5}]
                )
            )
        with self.assertRaises(LearningValidationError):
            service.create_experiment(
                _experiment_payload(approval_policy={"mode": "auto"})
            )

    async def test_cancelled_experiment_cannot_run(self) -> None:
        service = self.service()
        experiment_id = service.create_experiment(_experiment_payload())["experiment"]["experiment_id"]
        await service.cancel_experiment(experiment_id, "operator cancelled")
        self.assertEqual(service.experiment(experiment_id)["experiment"]["status"], "cancelled")
        with self.assertRaises(LearningStateError):
            await service.run_experiment(experiment_id)


class GovernanceTests(_LearningCase):
    async def _pending(self, service: LearningService, **overrides: object) -> tuple[str, str]:
        created = service.create_experiment(_experiment_payload(**overrides))
        experiment_id = created["experiment"]["experiment_id"]
        with mock.patch(
            "novacontrol.novamarket.learning.service.experiments.evaluate_candidate",
            return_value=_eligible_result(),
        ):
            await service.run_experiment(experiment_id)
        candidate = service.candidates(experiment_id=experiment_id)["candidates"][0]
        return experiment_id, candidate["candidate_id"]

    async def test_manual_approval_promotion_and_rollback(self) -> None:
        service = self.service()
        _, first_candidate = await self._pending(service)
        approved = service.approve_candidate(first_candidate, {"actor": "alice"})
        self.assertEqual(approved["candidate"]["status"], "approved")
        promoted = service.promote_candidate(first_candidate, {"actor": "alice"})
        self.assertEqual(promoted["candidate"]["status"], "promoted")
        first_version = promoted["version"]["version_id"]
        first_config = dict(promoted["version"]["config"])
        self.assertEqual(service.active_version("strat_1")["active_version"], first_version)
        self.assertTrue(self.applied)

        _, second_candidate = await self._pending(
            service,
            candidate_configs=[{**BASELINE_CONFIG, "fast_period": 6, "slow_period": 14}],
        )
        service.approve_candidate(second_candidate, {"actor": "bob"})
        second = service.promote_candidate(second_candidate, {"actor": "bob"})
        second_version = second["version"]["version_id"]
        self.assertNotEqual(first_version, second_version)

        rollback = service.rollback(
            "strat_1",
            {"to_version": first_version, "reason": "second version underperformed"},
        )
        self.assertEqual(rollback["active_version"], first_version)
        self.assertEqual(service.active_version("strat_1")["active_version"], first_version)
        self.assertEqual(service.rollbacks("strat_1")["total"], 1)
        self.assertEqual(
            service.candidate(second_candidate)["candidate"]["status"], "rolled_back"
        )
        history = service.versions("strat_1")
        self.assertEqual(history["total"], 2)
        preserved = next(
            row for row in history["versions"] if row["version_id"] == first_version
        )
        self.assertEqual(preserved["config"], first_config)
        self.assertTrue(preserved["active"])
        self.assertEqual(preserved["status"], "promoted")

    async def test_invalid_rollback_is_refused(self) -> None:
        service = self.service()
        _, candidate = await self._pending(service)
        service.approve_candidate(candidate)
        promoted = service.promote_candidate(candidate)
        version = promoted["version"]["version_id"]
        with self.assertRaises(LearningNotFoundError):
            service.rollback("strat_1", {"to_version": "lrv_missing", "reason": "bad"})
        with self.assertRaises(LearningStateError):
            service.rollback("strat_1", {"to_version": version, "reason": "same"})
        with self.assertRaises(LearningValidationError):
            service.rollback("strat_1", {"to_version": version})

    async def test_automatic_approval_requires_explicit_policy(self) -> None:
        service = self.service()
        _, candidate = await self._pending(service)
        with self.assertRaises(LearningStateError):
            service.approve_candidate(candidate, {"automatic": True, "actor": "auto"})

    async def test_explicit_auto_policy_approves_without_promoting(self) -> None:
        service = self.service()
        created = service.create_experiment(
            _experiment_payload(
                approval_policy={"mode": "auto", "auto_approve": True},
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8}],
            )
        )
        experiment_id = created["experiment"]["experiment_id"]
        with mock.patch(
            "novacontrol.novamarket.learning.service.experiments.evaluate_candidate",
            return_value=_eligible_result(),
        ):
            await service.run_experiment(experiment_id)
        candidate = service.candidates(experiment_id=experiment_id)["candidates"][0]
        self.assertEqual(candidate["status"], "approved")
        self.assertTrue(candidate["approval"]["automatic"])
        self.assertEqual(service.active_version("strat_1")["active_version"], "")

    async def test_auto_policy_with_shadow_validates_before_approval(self) -> None:
        service = self.service(shadow=_FakeShadow())
        created = service.create_experiment(
            _experiment_payload(
                approval_policy={
                    "mode": "auto",
                    "auto_approve": True,
                    "require_shadow": True,
                },
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8}],
            )
        )
        experiment_id = created["experiment"]["experiment_id"]
        with mock.patch(
            "novacontrol.novamarket.learning.service.experiments.evaluate_candidate",
            return_value=_eligible_result(),
        ):
            await service.run_experiment(experiment_id)
        candidate = service.candidates(experiment_id=experiment_id)["candidates"][0]
        self.assertEqual(candidate["status"], "approved")
        self.assertEqual(candidate["shadow_validation"]["status"], "passed")
        self.assertTrue(candidate["approval"]["automatic"])

    async def test_auto_policy_without_shadow_leaves_candidate_pending(self) -> None:
        service = self.service()
        created = service.create_experiment(
            _experiment_payload(
                approval_policy={
                    "mode": "auto",
                    "auto_approve": True,
                    "require_shadow": True,
                },
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8}],
            )
        )
        experiment_id = created["experiment"]["experiment_id"]
        with mock.patch(
            "novacontrol.novamarket.learning.service.experiments.evaluate_candidate",
            return_value=_eligible_result(),
        ):
            result = await service.run_experiment(experiment_id)
        self.assertEqual(result["experiment"]["status"], "completed")
        self.assertEqual(result["automatic_approval"], "skipped")
        candidate = service.candidates(experiment_id=experiment_id)["candidates"][0]
        self.assertEqual(candidate["status"], "pending_approval")

    async def test_post_promotion_monitor_recommends_rollback(self) -> None:
        service = self.service()
        _, candidate_id = await self._pending(service)
        service.approve_candidate(candidate_id, {"actor": "alice"})
        promoted = service.promote_candidate(candidate_id, {"actor": "alice"})
        version_id = promoted["version"]["version_id"]
        service.record_evidence(
            _evidence(
                source_ref="post_previous",
                window_end=NOW - timedelta(days=35),
                metrics={"total_return": 0.08, "sharpe": 1.2},
            )
        )
        service.record_evidence(
            _evidence(
                source_ref="post_current",
                window_end=NOW - timedelta(days=5),
                metrics={"total_return": -0.03, "sharpe": 0.2},
            )
        )
        monitored = service.monitor("strat_1", window_days=30)
        recommendations = [
            row
            for row in monitored["alerts"]
            if row["kind"] == "rollback_recommended"
        ]
        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0]["metrics"]["active_version"], version_id)
        self.assertIn("automatic rollback is not configured", recommendations[0]["message"])
        self.assertEqual(
            service.active_version("strat_1")["active_version"], version_id
        )

    async def test_risk_policy_violation_rejects_a_candidate(self) -> None:
        service = self.service(policy={**POLICY, "max_position_weight": "0.10"})
        experiment_id = service.create_experiment(
            _experiment_payload(
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8}]
            )
        )["experiment"]["experiment_id"]
        with mock.patch(
            "novacontrol.novamarket.learning.service.experiments.evaluate_candidate",
            return_value={
                **_eligible_result(),
                "eligible": False,
                "status": "rejected",
                "reasons": ["max_position_weight 0.25 exceeds policy 0.10"],
                "risk": {
                    "compliant": False,
                    "reasons": ["max_position_weight 0.25 exceeds policy 0.10"],
                    "policy_version": "policy-v1",
                },
            },
        ):
            await service.run_experiment(experiment_id)
        candidate = service.candidates(experiment_id=experiment_id)["candidates"][0]
        self.assertEqual(candidate["status"], "rejected")
        with self.assertRaises(LearningStateError):
            service.approve_candidate(candidate["candidate_id"])


class ShadowValidationTests(_LearningCase):
    async def _pending(self, service: LearningService) -> str:
        experiment_id = service.create_experiment(
            _experiment_payload(
                approval_policy={"mode": "manual", "require_shadow": True},
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8}],
            )
        )["experiment"]["experiment_id"]
        with mock.patch(
            "novacontrol.novamarket.learning.service.experiments.evaluate_candidate",
            return_value=_eligible_result(),
        ):
            await service.run_experiment(experiment_id)
        return service.candidates(experiment_id=experiment_id)["candidates"][0]["candidate_id"]

    async def test_shadow_validation_pass_then_approval(self) -> None:
        service = self.service(shadow=_FakeShadow())
        candidate_id = await self._pending(service)
        with self.assertRaises(LearningStateError):
            service.approve_candidate(candidate_id)
        passed = await service.shadow_validate_candidate(candidate_id, {"min_decisions": 3})
        self.assertEqual(passed["validation"]["status"], "passed")
        self.assertEqual(passed["candidate"]["status"], "pending_approval")
        history = service.candidate(candidate_id)["shadow_validation_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["candidate_id"], candidate_id)
        self.assertEqual(history[0]["status"], "passed")
        self.assertTrue(history[0]["completed_at"])
        approved = service.approve_candidate(candidate_id)
        self.assertEqual(approved["candidate"]["status"], "approved")

    async def test_insufficient_shadow_evidence_blocks_approval(self) -> None:
        service = self.service(shadow=_FakeShadow(decisions=1))
        candidate_id = await self._pending(service)
        insufficient = await service.shadow_validate_candidate(
            candidate_id, {"min_decisions": 3}
        )
        self.assertEqual(
            insufficient["validation"]["status"], "insufficient_evidence"
        )
        self.assertEqual(
            insufficient["candidate"]["status"], "pending_approval"
        )
        with self.assertRaises(LearningStateError):
            service.approve_candidate(candidate_id)

    async def test_failed_shadow_validation_rejects(self) -> None:
        service = self.service(
            shadow=_FakeShadow(candidate_return="0.1", baseline_return="2.0")
        )
        candidate_id = await self._pending(service)
        failed = await service.shadow_validate_candidate(
            candidate_id, {"min_decisions": 3}
        )
        self.assertEqual(failed["validation"]["status"], "failed")
        self.assertEqual(failed["candidate"]["status"], "rejected")


class ReliabilityTests(_LearningCase):
    async def test_concurrency_limit_serialises_direct_runs(self) -> None:
        service = self.service(max_concurrency=1)
        first = service.create_experiment(
            _experiment_payload(
                seed=1,
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8}],
            )
        )["experiment"]["experiment_id"]
        second = service.create_experiment(
            _experiment_payload(
                seed=2,
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 4, "slow_period": 10}],
            )
        )["experiment"]["experiment_id"]
        active = 0
        peak = 0

        async def wrapped(experiment: object, candidate: object) -> object:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return replace(
                candidate,  # type: ignore[type-var]
                status=CandidateStatus.PENDING_APPROVAL,
                evaluation=_eligible_result(),
                risk={"compliant": True, "reasons": [], "policy_version": "p1"},
            )

        service._evaluate_one = wrapped  # type: ignore[method-assign]
        await asyncio.gather(
            service.run_experiment(first), service.run_experiment(second)
        )
        self.assertEqual(peak, 1)
        self.assertEqual(service.experiment(first)["experiment"]["status"], "completed")
        self.assertEqual(service.experiment(second)["experiment"]["status"], "completed")

    async def test_a_failed_promotion_record_restores_the_previous_config(self) -> None:
        service = self.service()
        experiment_id = service.create_experiment(
            _experiment_payload(
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8}]
            )
        )["experiment"]["experiment_id"]
        with mock.patch(
            "novacontrol.novamarket.learning.service.experiments.evaluate_candidate",
            return_value=_eligible_result(),
        ):
            await service.run_experiment(experiment_id)
        candidate = service.candidates(experiment_id=experiment_id)["candidates"][0]
        service.approve_candidate(candidate["candidate_id"])
        with mock.patch.object(
            service._store, "set_active", side_effect=RuntimeError("disk full")
        ):
            with self.assertRaises(LearningStateError):
                service.promote_candidate(candidate["candidate_id"])
        self.assertEqual(self.applied[-1][1], BASELINE_CONFIG)
        self.assertEqual(service.active_version("strat_1")["active_version"], "")
        self.assertEqual(
            service.candidate(candidate["candidate_id"])["candidate"]["status"],
            "approved",
        )

    async def test_cancellation_during_evaluation_does_not_complete(self) -> None:
        service = self.service()
        experiment_id = service.create_experiment(
            _experiment_payload(
                candidate_configs=[{**BASELINE_CONFIG, "fast_period": 3, "slow_period": 8}]
            )
        )["experiment"]["experiment_id"]
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(experiment: object, candidate: object) -> object:
            started.set()
            await release.wait()
            return replace(
                candidate,  # type: ignore[type-var]
                status=CandidateStatus.PENDING_APPROVAL,
                evaluation=_eligible_result(),
                risk={"compliant": True, "reasons": [], "policy_version": "p1"},
            )

        service._evaluate_one = slow  # type: ignore[method-assign]
        task = asyncio.create_task(service.run_experiment(experiment_id))
        await started.wait()
        await service.cancel_experiment(experiment_id, "operator stopped it")
        release.set()
        result = await task
        self.assertTrue(result["cancelled"])
        self.assertEqual(
            service.experiment(experiment_id)["experiment"]["status"], "cancelled"
        )

    async def test_recovery_fails_interrupted_work(self) -> None:
        service = self.service()
        created = service.create_experiment(_experiment_payload())
        experiment_id = created["experiment"]["experiment_id"]
        candidate_id = created["candidates"][0]["candidate_id"]
        experiment = service._store.experiment(experiment_id)
        candidate = service._store.candidate(candidate_id)
        assert experiment is not None and candidate is not None
        service._store.save_experiment(
            replace(experiment, status=ExperimentStatus.EVALUATING)
        )
        service._store.save_candidate(
            replace(candidate, status=CandidateStatus.EVALUATING)
        )
        recovered = service.recover()
        self.assertEqual(recovered["failed"], 1)
        self.assertEqual(
            service.experiment(experiment_id)["experiment"]["status"], "failed"
        )
        self.assertEqual(
            service.candidate(candidate_id)["candidate"]["status"],
            "evaluation_failed",
        )


class IsolationTests(unittest.TestCase):
    def test_learning_database_is_its_own_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = NovaMarketService(data_dir=tmp)
            learning = Path(service._learning_path())
            self.assertEqual(learning.name, "novamarket_learning.sqlite3")
            self.assertEqual(learning.parent, Path(tmp))
            self.assertNotEqual(learning, Path(service._shadow_path()))
            self.assertNotEqual(learning, Path(service._radar_path()))

    def test_learning_database_is_in_memory_without_a_directory(self) -> None:
        service = NovaMarketService()
        self.assertEqual(service._learning_path(), ":memory:")


if __name__ == "__main__":
    unittest.main()
