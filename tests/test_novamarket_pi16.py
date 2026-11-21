"""Focused PI-16 adaptive-intelligence tests."""

from __future__ import annotations

import ast
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from novacontrol.novamarket.adaptive import (
    AdaptiveConfig,
    AdaptiveIntelligenceService,
    AdaptiveNotFoundError,
    AdaptiveStoreError,
    AdaptiveValidationError,
    detectors,
)
from novacontrol.novamarket.adaptive.store import AdaptiveStore

NOW = datetime(2025, 6, 30, tzinfo=UTC)


def at(day: int) -> str:
    return (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=day)).isoformat()


def clock() -> datetime:
    return NOW


def observations(
    count: int,
    *,
    start: int,
    returns: float,
    volatility: float = 0.01,
    volume: float = 100.0,
    features: dict[str, float] | None = None,
) -> list[dict[str, object]]:
    return [
        {
            "timestamp": at(start + index),
            "returns": returns,
            "volatility": volatility,
            "volume": volume,
            "features": dict(features or {"rsi_14": 50.0, "return_20": 0.002}),
        }
        for index in range(count)
    ]


def pair_observations(*, start: int, slope: float) -> list[dict[str, object]]:
    rows = []
    for index in range(20):
        score = (index - 10) / 10.0
        rows.append(
            {
                "timestamp": at(start + index),
                "features": {"score": score},
                "outcome": score * slope,
                "outcome_at": at(start + index + 1),
            }
        )
    return rows


def regime_payload(primary: str = "strong_up") -> dict[str, object]:
    return {
        "regime_id": "rg_test_1",
        "instrument_id": "XNSE:ALPHA",
        "as_of": at(20),
        "available_at": at(20),
        "primary_regime": primary,
        "secondary_conditions": ["high_volatility"],
        "confidence": None,
        "supporting_features": ["sma_20", "rsi_14"],
        "evidence": {"rsi_14": 63.0, "adx_14": 28.0},
        "feature_set_version": "pi04-feature-set-1.0",
        "method_version": "pi04-regime-1.0",
        "feature_quality": "valid",
    }


class FakeTraining:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.artifact_version = "7"

    def policy(self, policy_id: str) -> dict[str, object]:
        if policy_id != "policy_1":
            raise KeyError(policy_id)
        return {
            "policy_id": "policy_1",
            "name": "adaptive experiment",
            "status": "validated",
            "artifact_version": self.artifact_version,
            "artifact_hash": "abc123",
            "config_hash": "cfg123",
            "environment_version": "pi13-v1",
            "reward_version": "pi14-v1",
            "dataset_provenance": {
                "dataset_version": "ds@1",
                "start": at(0),
                "end": at(30),
            },
        }

    def evaluate_regime_window(
        self, policy_id: str, payload: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append((policy_id, payload))
        return {
            "ok": True,
            "evaluation": {
                "evaluation_id": "pe_regime_1",
                "policy_name": "adaptive experiment",
                "transitions": 40,
                "metrics": {
                    "financial": {
                        "observations": 40,
                        "total_return": 0.04,
                        "realized_pnl": 4000.0,
                    },
                    "risk": {
                        "max_drawdown": 0.04,
                        "volatility": 0.12,
                        "sharpe": 1.1,
                        "sortino": 1.4,
                    },
                    "trading": {"total_costs": "500", "turnover": 1.2},
                    "decision": {"invalid_action_count": 0},
                },
                "action_distribution": {"buy": 10, "hold": 30},
                "dataset_version": "ds@1",
                "created_at": at(29),
            },
            "baselines": [
                {"policy_name": "no_action", "metrics": {"financial": {"total_return": 0.0}}},
                {"policy_name": "rule_based", "metrics": {"financial": {"total_return": 0.01}}},
            ],
            "comparison_id": "cmp_regime_1",
            "conditions": {"split": "all", "simulated": True},
        }


def build_service(*, path: str = ":memory:", config: AdaptiveConfig | None = None) -> tuple[
    AdaptiveIntelligenceService, FakeTraining
]:
    training = FakeTraining()
    service = AdaptiveIntelligenceService(
        path,
        training_service=training,
        config=config or AdaptiveConfig(cooldown_seconds=0),
        clock=clock,
    )
    return service, training


def test_initialization_and_contract_are_advisory() -> None:
    service, _ = build_service()
    status = service.status()
    contract = service.contract()
    assert status["phase"] == "PI-16"
    assert status["production_authority"] is False
    assert status["activation_available"] is False
    assert status["automatic_training"] is False
    assert status["automatic_promotion"] is False
    assert contract["detectors"]["distribution"] == [
        "psi",
        "kolmogorov_smirnov",
        "jensen_shannon",
    ]
    assert contract["guarantees"]
    assert service.health()["healthy"] is True


def test_regime_analysis_preserves_source_and_uncertainty() -> None:
    service, _ = build_service()
    result = service.regime_analysis({"regimes": [regime_payload("uncertain")]})
    assert result["primary_regime"] == "uncertain"
    assert result["source"] == "pi04"
    assert result["source_timestamp"] == at(20)
    assert result["confidence"] is None
    assert result["uncertainty_preserved"] is True
    assert result["secondary_conditions"] == ["high_volatility"]
    assert result["advisory_only"] is True


def test_regime_aliases_do_not_invent_a_label() -> None:
    service, _ = build_service()
    result = service.regime_analysis(
        {"regimes": [{**regime_payload(), "primary_regime": "super_bullish"}]}
    )
    assert result["primary_regime"] == "uncertain"


def test_regime_specific_evaluation_and_sample_limit() -> None:
    service, _ = build_service()
    good = service.record_regime_evaluation(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": at(21),
            "sample_size": 40,
            "start": at(0),
            "end": at(20),
            "metrics": {
                "financial": {"total_return": 0.04, "observations": 40},
                "risk": {"max_drawdown": 0.04, "sharpe": 1.1},
                "trading": {"total_trades": 10},
                "decision": {},
            },
        }
    )
    small = service.record_regime_evaluation(
        {
            "policy_id": "policy_1",
            "regime": "high_volatility",
            "evaluated_at": at(21),
            "sample_size": 2,
            "metrics": {"financial": {"total_return": 0.5}, "risk": {}},
        }
    )
    assert good["evaluation"]["sufficient"] is True
    assert good["evaluation"]["policy_version"] == "7"
    assert small["evaluation"]["sufficient"] is False
    assert small["evaluation"]["status"] == "insufficient_evidence"
    performance = service.regime_performance("policy_1")
    assert performance["summary"]["strong_up"]["evaluations"] == 1


def test_suitability_statuses_are_evidence_based() -> None:
    service, _ = build_service()
    common = {
        "policy_id": "policy_1",
        "regime": "strong_up",
        "evaluated_at": (NOW - timedelta(days=2)).isoformat(),
        "as_of": NOW.isoformat(),
        "sample_size": 120,
        "forecast_uncertainty": 0.2,
        "policy_stability": 0.8,
    }
    suitable = service.assess_suitability(
        {
            **common,
            "metrics": {
                "financial": {
                    "total_return": 0.08,
                    "observations": 120,
                    "initial_capital": 100000,
                },
                "risk": {
                    "max_drawdown": 0.05,
                    "volatility": 0.15,
                    "sharpe": 1.2,
                    "sortino": 1.5,
                },
                "trading": {"total_costs": "500", "turnover": 1.0},
                "decision": {"invalid_action_rate": 0.0},
            },
        }
    )
    unsuitable = service.assess_suitability(
        {
            **common,
            "metrics": {
                "financial": {"total_return": -0.20, "observations": 120},
                "risk": {
                    "max_drawdown": 0.40,
                    "volatility": 0.50,
                    "sharpe": -1.5,
                    "sortino": -1.2,
                },
                "trading": {"total_costs": "5000"},
                "decision": {},
            },
        }
    )
    stale = service.assess_suitability(
        {
            **common,
            "evaluated_at": at(0),
            "metrics": {"financial": {"total_return": 0.08}, "risk": {}},
        }
    )
    not_evaluated = service.assess_suitability({"policy_id": "policy_1", "regime": "strong_up"})
    assert suitable["suitability"]["status"] == "suitable"
    assert suitable["suitability"]["production_authority"] is False
    assert unsuitable["suitability"]["status"] == "unsuitable"
    assert stale["suitability"]["status"] == "stale_evaluation"
    assert not_evaluated["suitability"]["status"] == "not_evaluated"


def test_insufficient_sample_never_claims_suitability() -> None:
    service, _ = build_service()
    result = service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": at(20),
            "as_of": at(21),
            "sample_size": 2,
            "metrics": {"financial": {"total_return": 1.0}, "risk": {"sharpe": 5.0}},
        }
    )
    assert result["suitability"]["status"] == "insufficient_evidence"
    assert result["suitability"]["suitability_score"] is None


def test_suitability_reuses_stored_regime_evidence_and_rejects_wrong_version() -> None:
    service, _ = build_service()
    service.record_regime_evaluation(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": (NOW - timedelta(days=2)).isoformat(),
            "sample_size": 120,
            "metrics": {
                "financial": {
                    "total_return": 0.08,
                    "observations": 120,
                    "initial_capital": 100000,
                },
                "risk": {
                    "max_drawdown": 0.05,
                    "volatility": 0.15,
                    "sharpe": 1.2,
                    "sortino": 1.5,
                },
                "trading": {"total_costs": "500"},
                "decision": {"invalid_action_rate": 0.0},
            },
            "policy_version": "7",
            "dataset_version": "ds@1",
        }
    )
    reused = service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "as_of": NOW.isoformat(),
            "forecast_uncertainty": 0.2,
            "policy_stability": 0.8,
        }
    )
    assert reused["suitability"]["status"] == "suitable"
    assert reused["suitability"]["evaluation_provenance"]["dataset_version"] == "ds@1"

    wrong_version = service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": (NOW - timedelta(days=2)).isoformat(),
            "as_of": NOW.isoformat(),
            "sample_size": 120,
            "policy_version": "6",
            "metrics": {"financial": {"total_return": 0.08}, "risk": {"sharpe": 1.2}},
        }
    )
    assert wrong_version["suitability"]["status"] == "stale_evaluation"
    assert "does not match" in wrong_version["suitability"]["reason"]


def test_unknown_suitability_evaluation_and_future_evaluation_are_rejected() -> None:
    service, _ = build_service()
    with pytest.raises(AdaptiveNotFoundError, match="no sufficient regime evaluation"):
        service.assess_suitability(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "evaluation_id": "pe_missing",
            }
        )
    with pytest.raises(AdaptiveValidationError, match="future"):
        service.record_regime_evaluation(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "evaluated_at": (NOW + timedelta(days=1)).isoformat(),
                "sample_size": 20,
                "metrics": {
                    "financial": {"total_return": 0.04},
                    "risk": {"max_drawdown": 0.02},
                },
            }
        )


def test_future_observation_timestamp_is_rejected() -> None:
    service, _ = build_service()
    reference = observations(20, start=0, returns=0.001)
    current = observations(20, start=30, returns=0.03)
    with pytest.raises(AdaptiveValidationError, match="observed after as_of"):
        service.detect_drift(
            {
                "kind": "data",
                "as_of": at(35),
                "reference": reference,
                "current": current,
            }
        )


def test_market_change_distinguishes_normal_and_sustained() -> None:
    service, _ = build_service()
    normal = service.detect_market_change(
        {
            "instrument_id": "XNSE:ALPHA",
            "as_of": at(65),
            "reference": observations(20, start=0, returns=0.001),
            "current": observations(20, start=40, returns=0.001),
        }
    )
    sustained = service.detect_market_change(
        {
            "instrument_id": "XNSE:ALPHA",
            "as_of": at(65),
            "consecutive_windows": 2,
            "reference": observations(20, start=0, returns=0.001),
            "current": observations(
                20, start=40, returns=0.03, volatility=0.05, volume=250.0
            ),
        }
    )
    assert normal["change"]["status"] == "normal_variation"
    assert normal["change"]["signal_count"] == 0
    assert sustained["change"]["status"] == "sustained_change"
    assert sustained["change"]["signal_count"] >= 2
    assert sustained["change"]["evidence_strength"] in {"medium", "high"}


def test_market_change_insufficient_window_is_explicit() -> None:
    service, _ = build_service()
    result = service.detect_market_change(
        {
            "instrument_id": "XNSE:ALPHA",
            "reference": observations(5, start=0, returns=0.001),
            "current": observations(5, start=10, returns=0.001),
        }
    )
    assert result["change"]["status"] == "insufficient_evidence"
    assert result["change"]["signal_count"] == 0


def test_drift_with_only_undersized_features_is_insufficient() -> None:
    service, _ = build_service()
    reference = observations(5, start=0, returns=0.001)
    current = observations(5, start=20, returns=0.03)
    result = service.detect_drift(
        {
            "kind": "data",
            "as_of": at(40),
            "reference": reference,
            "current": current,
        }
    )
    assert result["drift"]["status"] == "insufficient_evidence"
    assert result["drift"]["data_drifted"] is False


def test_data_drift_detects_genuine_shift_and_not_normal_variation() -> None:
    service, _ = build_service()
    reference = observations(
        20, start=0, returns=0.001, features={"rsi_14": 50.0, "return_20": 0.002}
    )
    same = observations(
        20, start=40, returns=0.001, features={"rsi_14": 50.0, "return_20": 0.002}
    )
    shifted = observations(
        20, start=40, returns=0.03, features={"rsi_14": 82.0, "return_20": 0.08}
    )
    normal = service.detect_drift(
        {"scope": "XNSE:ALPHA", "as_of": at(65), "reference": reference, "current": same}
    )
    genuine = service.detect_drift(
        {"scope": "XNSE:ALPHA", "as_of": at(65), "reference": reference, "current": shifted}
    )
    assert normal["drift"]["status"] == "no_drift"
    assert genuine["drift"]["status"] == "data_drift"
    assert "rsi_14" in genuine["drift"]["affected_features"]


def test_concept_drift_detects_changed_input_outcome_relationship() -> None:
    service, _ = build_service()
    result = service.detect_drift(
        {
            "kind": "concept",
            "scope": "policy_1",
            "as_of": at(80),
            "reference": pair_observations(start=0, slope=0.1),
            "current": pair_observations(start=40, slope=-0.1),
        }
    )
    assert result["drift"]["status"] == "concept_drift"
    relationship = result["drift"]["concept"]["relationship"]
    assert relationship["delta"] >= 0.3
    assert relationship["reference_correlation"] > 0
    assert relationship["current_correlation"] < 0


def test_future_outcomes_are_rejected_before_drift_calculation() -> None:
    service, _ = build_service()
    with pytest.raises(AdaptiveValidationError, match="available after as_of"):
        service.detect_drift(
            {
                "kind": "concept",
                "as_of": at(45),
                "reference": pair_observations(start=0, slope=0.1),
                "current": pair_observations(start=40, slope=-0.1),
            }
        )


def test_degradation_needs_persistence_and_does_not_trigger_on_one_window() -> None:
    service, _ = build_service()
    temporary = []
    persistent = []
    for index in range(8):
        temporary.append(
            {"timestamp": at(index), "return": 0.01 if index < 7 else -0.01}
        )
        persistent.append(
            {
                "timestamp": at(index),
                "return": 0.01 if index < 5 else -0.02,
                "max_drawdown": 0.03 if index < 5 else 0.12,
                "volatility": 0.01 if index < 5 else 0.08,
                "sharpe": 1.0 if index < 5 else 0.1,
                "reward": 1.0 if index < 5 else 0.5,
                "transaction_costs": 100.0,
                "turnover": 1.0,
                "action_distribution": {"buy": 5, "hold": 5}
                if index < 5
                else {"buy": 1, "hold": 9},
            }
        )
    one = service.monitor_degradation({"policy_id": "policy_1", "observations": temporary})
    many = service.monitor_degradation({"policy_id": "policy_1", "observations": persistent})
    assert one["degradation"]["status"] == "temporary_underperformance"
    assert one["degradation"]["trigger_count"] == 1
    assert many["degradation"]["status"] == "persistent_degradation"
    assert many["degradation"]["trigger_count"] >= 2
    assert many["degradation"]["triggers_training"] is False


def test_degradation_insufficient_data_is_not_a_degradation_claim() -> None:
    service, _ = build_service()
    result = service.monitor_degradation(
        {
            "policy_id": "policy_1",
            "observations": [{"timestamp": at(0), "return": -0.1}],
        }
    )
    assert result["degradation"]["status"] == "insufficient_evidence"
    assert result["degradation"]["trigger_count"] == 0


def test_recommendation_hysteresis_suppresses_low_evidence_switch() -> None:
    service, _ = build_service(config=AdaptiveConfig(cooldown_seconds=86_400))
    first = service.recommend(
        {
            "policy_id": "policy_1",
            "scope": "XNSE:ALPHA",
            "suitability": {"status": "suitable", "suitability_score": 0.8},
        }
    )
    second = service.recommend(
        {
            "policy_id": "policy_1",
            "scope": "XNSE:ALPHA",
            "changes": [
                {
                    "status": "sustained_change",
                    "signals": [{"name": "return_distribution_psi"}],
                }
            ],
        }
    )
    assert first["recommendation"]["action"] == "continue_monitoring"
    assert second["hysteresis_suppressed"] is True
    assert second["recommendation"]["action"] == "continue_monitoring"
    assert second["recommendation"]["hysteresis"]["proposed_action"] == "run_new_backtest"


def test_recommendation_escalates_to_retraining_with_concept_drift_and_degradation() -> None:
    service, _ = build_service()
    result = service.recommend(
        {
            "policy_id": "policy_1",
            "scope": "XNSE:ALPHA",
            "suitability": {"status": "conditionally_suitable", "suitability_score": 0.5},
            "degradation": {
                "status": "persistent_degradation",
                "triggers": [{"name": "sharpe_decline"}],
            },
            "drift": {"status": "concept_drift", "affected_features": ["score"]},
        }
    )
    recommendation = result["recommendation"]
    assert recommendation["action"] == "retrain_recommended"
    assert recommendation["requires_explicit_authorization"] is True
    assert recommendation["auto_train"] is False
    assert recommendation["activation_available"] is False
    acknowledged = service.acknowledge_recommendation(
        recommendation["recommendation_id"], {"note": "reviewed"}
    )
    assert acknowledged["recommendation"]["status"] == "acknowledged"
    assert acknowledged["recommendation"]["activation_available"] is False


def test_retraining_recommendation_records_window_without_starting_training() -> None:
    service, training = build_service()
    result = service.retraining_recommendation(
        {
            "policy_id": "policy_1",
            "reason": "sustained concept drift and degradation",
            "training_start": at(0),
            "training_end": at(30),
            "required_regimes": ["strong_up", "high_volatility"],
            "reward_config_hash": "reward-1",
            "validation_conditions": {"chronological_holdout": True},
        }
    )
    record = result["retraining"]
    assert record["training_started"] is False
    assert record["automatic_training"] is False
    assert record["required_regime_coverage"] == ["strong_up", "high_volatility"]
    assert record["suggested_training_window"]["chronological"] is True
    assert training.calls == []
    again = service.retraining_recommendation(
        {
            "policy_id": "policy_1",
            "reason": "sustained concept drift and degradation",
            "training_start": at(0),
            "training_end": at(30),
            "required_regimes": ["strong_up", "high_volatility"],
            "reward_config_hash": "reward-1",
        }
    )
    assert again["inserted"] is False
    assert again["retraining"]["retraining_id"] == record["retraining_id"]


def test_regime_annotations_do_not_mutate_experiences_and_future_is_excluded() -> None:
    service, _ = build_service()
    experiences = [
        {"experience_id": "exp_1", "as_of": at(5), "available_at": at(5), "regime": "strong_up"},
        {"experience_id": "exp_2", "as_of": at(15), "available_at": at(15), "regime": "strong_up"},
    ]
    annotation = service.annotate_regimes(
        {
            "instrument_id": "XNSE:ALPHA",
            "regime": "strong_up",
            "as_of": at(10),
            "available_at": at(6),
            "source_regime_id": "rg_1",
            "experiences": experiences,
            "annotation_version": "v1",
        }
    )
    assert experiences[1]["available_at"] == at(15)
    assert annotation["annotation"]["original_records_mutated"] is False
    selected = service.regime_experiences(
        {
            "instrument_id": "XNSE:ALPHA",
            "as_of": at(10),
            "regime": "strong_up",
            "experiences": experiences,
        }
    )
    assert [row["experience_id"] for row in selected["experiences"]] == ["exp_1"]
    assert selected["excluded_future"] == 1
    assert selected["leakage_prevented"] is True


def test_reassessment_is_bounded_cached_and_does_not_train() -> None:
    service, training = build_service()
    created = service.create_reassessment(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "reason": "regime transition evidence",
            "start": at(0),
            "end": at(30),
            "max_transitions": 100,
        }
    )
    assert created["reassessment"]["status"] == "pending_reassessment"
    completed = service.run_reassessment(created["reassessment"]["reassessment_id"])
    assert completed["reassessment"]["status"] == "completed"
    assert completed["reassessment"]["evaluation_triggered"] is True
    assert len(completed["reassessment"]["result"]["baselines"]) == 2
    assert completed["reassessment"]["result"]["comparison_id"] == "cmp_regime_1"
    assert training.calls[0][1]["max_transitions"] == 100
    cached = service.run_reassessment(created["reassessment"]["reassessment_id"])
    assert cached["reused"] is True
    assert training.calls and training.calls[0][0] == "policy_1"


def test_reassessment_supports_explicit_walk_forward_windows() -> None:
    service, training = build_service()
    created = service.create_reassessment(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "windows": [
                {"start": at(0), "end": at(10)},
                {"start": at(11), "end": at(20)},
            ],
            "max_transitions": 100,
        }
    )
    assert created["reassessment"]["config"]["windows"]
    completed = service.run_reassessment(created["reassessment"]["reassessment_id"])
    assert completed["reassessment"]["status"] == "completed"
    result = completed["reassessment"]["result"]
    assert result["walk_forward"] is True
    assert len(result["folds"]) == 2
    assert result["aggregate"]["chronological"] is True
    assert all(len(fold["baselines"]) == 2 for fold in result["folds"])
    assert len(training.calls) == 2
    assert all(call[1]["max_transitions"] == 50 for call in training.calls)

    with pytest.raises(AdaptiveValidationError, match="non-overlapping"):
        service.create_reassessment(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "windows": [
                    {"start": at(0), "end": at(15)},
                    {"start": at(10), "end": at(20)},
                ],
            }
        )


def test_reassessment_with_real_pi15_artifact_uses_artifact_version(tmp_path: Path) -> None:
    from novacontrol.novamarket.agent_training import TrainingManager
    from novacontrol.novamarket.rewards import RewardService
    from test_novamarket_pi15 import config_payload, forecast, price_bars

    bars = price_bars()
    forecasts = {bar.observed_at: forecast(bar.observed_at) for bar in bars}
    database = tmp_path / "real_policy.sqlite3"
    rewards = RewardService(
        database,
        transition_reader=lambda episode_id: [],
        bar_loader=lambda config: bars,
        forecast_loader=lambda config, at: forecasts.get(at),
    )
    manager = TrainingManager(
        database,
        bar_loader=lambda config: bars,
        forecast_loader=lambda config, at: forecasts.get(at),
        reward_service=rewards,
    )
    adaptive = AdaptiveIntelligenceService(
        tmp_path / "adaptive.sqlite3",
        training_service=manager,
        clock=lambda: datetime(2026, 6, 1, tzinfo=UTC),
    )
    try:
        started = manager.start({"config": config_payload(bars)})
        run_id = started["run"]["run_id"]
        policy_id = started["policy_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            run = manager.run(run_id)
            if run["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert run["status"] == "completed", run.get("error")
        policy = manager.policy(policy_id)
        assert policy["status"] == "tested"

        # The window must lie in the untouched test split: the training split
        # (bars 0-49) and the checkpoint-selection split (bars 50-69) are
        # in-sample for this artifact and are refused.
        created = adaptive.create_reassessment(
            {
                "policy_id": policy_id,
                "regime": "strong_up",
                "start": bars[70].observed_at.isoformat(),
                "end": bars[89].observed_at.isoformat(),
                "max_transitions": 30,
            }
        )
        assert created["reassessment"]["out_of_sample"] is True
        assert created["reassessment"]["in_sample"] is False
        assert created["reassessment"]["evidence_scope"]["verified"] is True
        completed = adaptive.run_reassessment(
            created["reassessment"]["reassessment_id"]
        )
        assert completed["ok"] is True, completed["reassessment"].get("error")
        assert completed["reassessment"]["status"] == "completed"
        result = completed["reassessment"]["result"]
        # PI-14 labels the adapter version ``policy_version``; adaptive evidence
        # must instead reference the immutable trained artifact.
        assert result["policy_version"] == policy["artifact_version"]
        assert result["sample_size"] >= AdaptiveConfig().min_regime_samples
        assert result["sufficient"] is True
        assert {row["policy_name"] for row in result["baselines"]} == {
            "no_action",
            "rule_based",
            "pi12_adapter",
        }
        assert completed["reassessment"]["out_of_sample"] is True
        assert completed["reassessment"]["trains_policy"] is False
        assert completed["reassessment"]["production_authority"] is False
    finally:
        adaptive.close()
        manager.close()
        rewards.close()


def test_reassessment_without_evaluator_fails_honestly() -> None:
    service = AdaptiveIntelligenceService(":memory:", config=AdaptiveConfig(), clock=clock)
    created = service.create_reassessment(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "start": at(0),
            "end": at(30),
        }
    )
    result = service.run_reassessment(created["reassessment"]["reassessment_id"])
    assert result["ok"] is False
    assert result["reassessment"]["status"] == "failed"
    assert "no PI-15" in result["reassessment"]["error"]


def test_event_ids_are_idempotent_for_identical_evidence() -> None:
    service, _ = build_service()
    payload = {
        "instrument_id": "XNSE:ALPHA",
        "as_of": at(65),
        "consecutive_windows": 2,
        "reference": observations(20, start=0, returns=0.001),
        "current": observations(20, start=40, returns=0.03, volatility=0.05),
    }
    first = service.detect_market_change(payload)
    second = service.detect_market_change(payload)
    assert first["change"]["change_id"] == second["change"]["change_id"]
    assert second["inserted"] is False
    assert len(service.changes()["items"]) == 1


def test_persistence_and_recovery(tmp_path: Path) -> None:
    path = tmp_path / "adaptive.sqlite3"
    service, _ = build_service(path=str(path))
    service.detect_market_change(
        {
            "instrument_id": "XNSE:ALPHA",
            "as_of": at(65),
            "reference": observations(20, start=0, returns=0.001),
            "current": observations(20, start=40, returns=0.03),
        }
    )
    service.close()
    reopened, _ = build_service(path=str(path))
    assert reopened.status()["counts"]["changes"] == 1
    assert reopened.health()["storage"]["schema_version"] == 1
    reopened.close()


def test_missing_or_unknown_records_are_explicit() -> None:
    service, _ = build_service()
    with pytest.raises(AdaptiveNotFoundError):
        service.config_record("missing")
    with pytest.raises(AdaptiveNotFoundError):
        service.acknowledge_recommendation("missing")
    unavailable = service.detect_drift(
        {
            "kind": "data",
            "reference": observations(20, start=0, returns=0.001),
            "current": [],
        }
    )
    assert unavailable["drift"]["status"] == "data_unavailable"


def test_detectors_have_expected_behavior() -> None:
    reference = [0.01] * 30
    assert detectors.population_stability_index(reference, reference)["score"] == 0.0
    shifted = [0.05] * 30
    assert detectors.population_stability_index(reference, shifted)["score"] > 0.2
    ks = detectors.ks_2samp([0.0, 0.1, 0.2, 0.3], [0.8, 0.9, 1.0, 1.1])
    assert ks["p_value"] < 0.05
    js = detectors.jensen_shannon([0.0, 0.0, 1.0], [1.0, 1.0, 1.0])
    assert js["score"] is not None and js["score"] > 0
    assert detectors.page_hinkley([0.1] * 20)["detected"] is False
    assert detectors.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)


def test_exported_service_is_json_serializable() -> None:
    service, _ = build_service()
    service.regime_analysis({"regimes": [regime_payload()]})
    service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": at(20),
            "as_of": at(21),
            "sample_size": 100,
            "metrics": {"financial": {"total_return": 0.04}, "risk": {"sharpe": 1.0}},
        }
    )
    payload = service.dashboard()
    encoded = json.dumps(payload, sort_keys=True)
    assert "activation_available" in encoded
    assert payload["advisory_only"] is True


def test_package_and_storage_are_financially_isolated(tmp_path: Path) -> None:
    package = Path(__file__).parents[1] / "src/novacontrol/novamarket/adaptive"
    forbidden = ("portfolio", "ledger", "execution", "broker", "order_gateway")
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        for module in modules:
            for token in forbidden:
                assert token not in module, (source.name, module)

    path = tmp_path / "adaptive.sqlite3"
    service, _ = build_service(path=str(path))
    service.status()
    service.close()
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables
    assert all(name.startswith("adp_") for name in tables)


def test_store_limit_still_allows_idempotent_replays() -> None:
    service, _ = build_service(config=AdaptiveConfig(max_records=1))
    payload = {
        "instrument_id": "XNSE:ALPHA",
        "as_of": at(65),
        "reference": observations(20, start=0, returns=0.001),
        "current": observations(20, start=40, returns=0.03),
    }
    first = service.detect_market_change(payload)
    replay = service.detect_market_change(payload)
    assert first["inserted"] is True
    assert replay["inserted"] is False
    with pytest.raises(AdaptiveStoreError, match="storage limit"):
        service.detect_market_change({**payload, "as_of": at(66)})


def test_suitability_requires_risk_evidence_and_does_not_claim_suitable_without_costs() -> None:
    service, _ = build_service()
    missing_risk = service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": (NOW - timedelta(days=2)).isoformat(),
            "as_of": NOW.isoformat(),
            "sample_size": 120,
            "metrics": {"financial": {"total_return": 0.08}}, 
        }
    )
    assert missing_risk["suitability"]["status"] == "insufficient_evidence"
    missing_cost = service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": (NOW - timedelta(days=2)).isoformat(),
            "as_of": NOW.isoformat(),
            "sample_size": 120,
            "forecast_uncertainty": 0.2,
            "policy_stability": 0.8,
            "metrics": {
                "financial": {"total_return": 0.08, "observations": 120},
                "risk": {"max_drawdown": 0.05, "sharpe": 1.2, "sortino": 1.5},
                "decision": {"invalid_action_rate": 0.0},
            },
        }
    )
    assert missing_cost["suitability"]["status"] == "conditionally_suitable"


def test_acknowledged_recommendation_is_not_reopened_and_cooldown_expires() -> None:
    now = [NOW]
    service = AdaptiveIntelligenceService(
        ":memory:",
        training_service=FakeTraining(),
        config=AdaptiveConfig(cooldown_seconds=60),
        clock=lambda: now[0],
    )
    first = service.recommend({"policy_id": "policy_1", "scope": "XNSE:ALPHA"})
    recommendation_id = first["recommendation"]["recommendation_id"]
    service.acknowledge_recommendation(recommendation_id, {"note": "reviewed"})
    repeated = service.recommend({"policy_id": "policy_1", "scope": "XNSE:ALPHA"})
    assert repeated["recommendation"]["status"] == "acknowledged"

    switched = service.recommend(
        {
            "policy_id": "policy_1",
            "scope": "XNSE:ALPHA",
            "changes": [{"status": "sustained_change"}],
        }
    )
    assert switched["hysteresis_suppressed"] is True

    now[0] = NOW + timedelta(seconds=61)
    allowed = service.recommend(
        {
            "policy_id": "policy_1",
            "scope": "XNSE:ALPHA",
            "changes": [{"status": "sustained_change"}],
        }
    )
    assert allowed["hysteresis_suppressed"] is False
    assert allowed["recommendation"]["action"] == "run_new_backtest"


def test_reassessment_is_stale_for_a_different_policy_artifact() -> None:
    service, training = build_service()
    created = service.create_reassessment(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "start": at(0),
            "end": at(30),
        }
    )
    reassessment_id = created["reassessment"]["reassessment_id"]
    completed = service.run_reassessment(reassessment_id)
    assert completed["reassessment"]["status"] == "completed"
    training.artifact_version = "8"
    stale = service.run_reassessment(reassessment_id)
    assert stale["ok"] is False
    assert stale["reassessment"]["status"] == "stale_evidence"
    assert "different policy artifact" in stale["reason"]


def test_reassessment_recovers_a_running_job(tmp_path: Path) -> None:
    path = tmp_path / "adaptive.sqlite3"
    service, _ = build_service(path=str(path))
    created = service.create_reassessment(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "start": at(0),
            "end": at(30),
        }
    )
    row = created["reassessment"]
    running = {**row, "status": "running"}
    service.store.save_reassessment(
        row["reassessment_id"], "policy_1", "running", running
    )
    service.close()

    reopened, _ = build_service(path=str(path))
    recovered = reopened.reassessment(row["reassessment_id"])
    assert recovered["status"] == "failed"
    assert recovered["recovered_from_interruption"] is True
    assert "interrupted" in recovered["error"]
    assert reopened.status()["recovered"] == 1


def test_concept_drift_accepts_cusum_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacontrol.novamarket.adaptive import service as adaptive_service

    service, _ = build_service()
    monkeypatch.setattr(
        adaptive_service.detectors,
        "cusum",
        lambda *args, **kwargs: {
            "detected": True,
            "score": 9.0,
            "index": 2,
            "insufficient": False,
        },
    )
    reference = [
        {"timestamp": at(index), "forecast_error": 0.1} for index in range(20)
    ]
    current = [
        {"timestamp": at(30 + index), "forecast_error": 0.1} for index in range(20)
    ]
    result = service.detect_drift(
        {
            "kind": "concept",
            "as_of": at(60),
            "reference": reference,
            "current": current,
        }
    )
    assert result["drift"]["status"] == "concept_drift"
    assert result["drift"]["concept"]["cusum"]["detected"] is True


def test_configuration_rejects_zero_thresholds_and_unknown_fields() -> None:
    with pytest.raises(AdaptiveValidationError, match="greater than zero"):
        AdaptiveConfig(psi_threshold=0)
    with pytest.raises(AdaptiveValidationError, match="unknown adaptive configuration"):
        AdaptiveConfig.from_mapping({"psi_threshhold": 0.2})
    with pytest.raises(AdaptiveValidationError, match="within"):
        AdaptiveConfig(degradation_persistence_windows=0)


def test_future_degradation_observation_is_rejected() -> None:
    service, _ = build_service()
    rows = [
        {
            "timestamp": at(index),
            "return": 0.01,
            "max_drawdown": 0.02,
        }
        for index in range(8)
    ]
    with pytest.raises(AdaptiveValidationError, match="after as_of"):
        service.monitor_degradation(
            {"policy_id": "policy_1", "as_of": at(3), "observations": rows}
        )


def test_latest_regime_uses_source_time_not_insertion_order() -> None:
    service, _ = build_service()
    old = {
        **regime_payload("strong_up"),
        "regime_id": "rg_old",
        "as_of": at(5),
        "available_at": at(5),
    }
    new = {
        **regime_payload("weak_down"),
        "regime_id": "rg_new",
        "as_of": at(6),
        "available_at": at(6),
    }
    service.regime_analysis({"instrument_id": "XNSE:ALPHA", "regimes": [new]})
    service.regime_analysis({"instrument_id": "XNSE:ALPHA", "regimes": [old]})
    latest = service.latest_regime("XNSE:ALPHA")
    assert latest["regime_id"] == "rg_new"
    assert latest["primary_regime"] == "weak_down"
    assert service.dashboard()["latest_regime"]["regime_id"] == "rg_new"


def test_unknown_retraining_recommendation_is_explicit() -> None:
    service, _ = build_service()
    with pytest.raises(AdaptiveNotFoundError, match="unknown adaptation recommendation"):
        service.retraining_recommendation(
            {
                "policy_id": "policy_1",
                "recommendation_id": "adr_missing",
                "training_start": at(0),
                "training_end": at(30),
            }
        )


def test_a_persisted_configuration_survives_a_service_restart(tmp_path: Path) -> None:
    path = tmp_path / "adaptive.sqlite3"
    service, _ = build_service(path=str(path))
    service.create_config(
        {"config_id": "adp_custom", "cooldown_seconds": 1_234, "max_records": 7}
    )
    assert service.settings.cooldown_seconds == 1_234
    service.close()

    restarted = AdaptiveIntelligenceService(str(path), clock=clock)
    try:
        assert restarted.settings.config_id == "adp_custom"
        assert restarted.settings.cooldown_seconds == 1_234
        assert restarted.settings.max_records == 7
        assert restarted.store.max_records == 7
        status = restarted.status()["config"]
        assert status["cooldown_seconds"] == 1_234
        assert status["max_records"] == 7
    finally:
        restarted.close()


def test_the_most_recently_persisted_configuration_wins_on_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adaptive.sqlite3"
    service, _ = build_service(path=str(path))
    service.create_config({"config_id": "adp_first", "cooldown_seconds": 100})
    service.create_config(
        {"config_id": "adp_second", "cooldown_seconds": 200, "max_records": 9}
    )
    service.close()

    restarted = AdaptiveIntelligenceService(str(path), clock=clock)
    try:
        assert restarted.settings.config_id == "adp_second"
        assert restarted.settings.cooldown_seconds == 200
        assert restarted.settings.max_records == 9
    finally:
        restarted.close()


def test_an_explicit_configuration_still_overrides_the_persisted_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adaptive.sqlite3"
    service, _ = build_service(path=str(path))
    service.create_config({"config_id": "adp_stored", "cooldown_seconds": 999})
    service.close()

    explicit, _ = build_service(
        path=str(path),
        config=AdaptiveConfig(config_id="adp_explicit", cooldown_seconds=15),
    )
    try:
        assert explicit.settings.cooldown_seconds == 15
        assert explicit.settings.config_id == "adp_explicit"
    finally:
        explicit.close()


def test_an_unusable_persisted_configuration_falls_back_to_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adaptive.sqlite3"
    store = AdaptiveStore(str(path))
    broken = {
        **AdaptiveConfig().to_dict(),
        "automatic_promotion": True,
        "digest": "digest-of-an-invalid-config",
    }
    store.save_config(broken)
    store.close()

    restarted = AdaptiveIntelligenceService(str(path), clock=clock)
    try:
        assert restarted.settings.cooldown_seconds == AdaptiveConfig().cooldown_seconds
        assert restarted.settings.max_records == AdaptiveConfig().max_records
        assert "could not be restored" in restarted.status()["last_error"]
    finally:
        restarted.close()


def test_pi14_structured_metrics_are_read_with_units_and_sufficiency() -> None:
    service, _ = build_service()
    result = service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": (NOW - timedelta(days=2)).isoformat(),
            "as_of": NOW.isoformat(),
            "sample_size": 40,
            "metrics": {
                "financial": {
                    "total_return": 0.04,
                    "observations": 40,
                    "volatility": 0.12,
                    "initial_capital": 100000,
                },
                "risk": {
                    "max_drawdown": {
                        "name": "max_drawdown_pct",
                        "value": "8.0",
                        "unit": "percent",
                        "sufficient": True,
                        "observations": 40,
                    },
                    "sharpe": {
                        "name": "sharpe_ratio",
                        "value": None,
                        "unit": "ratio",
                        "sufficient": False,
                        "observations": 12,
                    },
                    "sortino": {"value": "1.4", "unit": "ratio", "sufficient": True},
                },
                "trading": {
                    "total_costs": {"value": "500", "unit": "currency", "sufficient": True}
                },
                "decision": {"invalid_action_frequency": 0.0},
            },
        }
    )["suitability"]
    components = result["components"]
    assert components["drawdown"] == pytest.approx(1.0 - 8.0 / 25.0)
    assert components["volatility"] == pytest.approx(1.0 - 12.0 / 40.0)
    assert components["action_consistency"] == pytest.approx(1.0)
    assert result["hard_flags"] == []
    # The insufficient sharpe is dropped, the sufficient sortino still counts.
    assert components["risk_adjusted"] == pytest.approx((1.4 / 3.0) * 0.4)
    assert "risk-adjusted return was not supplied" not in " ".join(result["limitations"])

    without_sortino = service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": (NOW - timedelta(days=2)).isoformat(),
            "as_of": NOW.isoformat(),
            "sample_size": 40,
            "metrics": {
                "financial": {"total_return": 0.04, "observations": 40},
                "risk": {
                    "max_drawdown": 0.04,
                    "sharpe": {"value": None, "unit": "ratio", "sufficient": False},
                },
                "trading": {"total_costs": "500"},
                "decision": {},
            },
        }
    )["suitability"]
    assert "risk-adjusted return was not supplied" in " ".join(
        without_sortino["limitations"]
    )


def test_pi16_suitability_uses_a_neutral_component_when_both_ratios_are_missing() -> None:
    """Neither Sharpe nor Sortino: the limitation and the score must agree."""

    service, _ = build_service()
    result = service.assess_suitability(
        {
            "policy_id": "policy_1",
            "regime": "strong_up",
            "evaluated_at": (NOW - timedelta(days=2)).isoformat(),
            "as_of": NOW.isoformat(),
            "sample_size": 40,
            "metrics": {
                "financial": {"total_return": 0.04, "observations": 40, "volatility": 0.12},
                "risk": {
                    "max_drawdown": 0.08,
                    "sharpe": {"value": None, "unit": "ratio", "sufficient": False},
                    "sortino": {"value": None, "unit": "ratio", "sufficient": False},
                },
                "trading": {"total_costs": "500"},
                "decision": {"invalid_action_frequency": 0.0},
            },
        }
    )["suitability"]
    components = result["components"]
    # Neutral, not worst-case: the limitation promises a neutral component.
    assert components["risk_adjusted"] == pytest.approx(0.5)
    limitations = " ".join(result["limitations"])
    assert (
        "risk-adjusted return was not supplied; a neutral scoring component was used"
        in limitations
    )
    # The score must actually carry that neutral component at its stated weight.
    weights = {
        "return": 0.20,
        "risk_adjusted": 0.18,
        "drawdown": 0.14,
        "volatility": 0.08,
        "cost": 0.08,
        "action_consistency": 0.08,
        "uncertainty": 0.06,
        "stability": 0.06,
        "sample": 0.06,
        "freshness": 0.06,
    }
    expected = sum(components[name] * weight for name, weight in weights.items())
    assert result["suitability_score"] == pytest.approx(expected, abs=1e-6)
