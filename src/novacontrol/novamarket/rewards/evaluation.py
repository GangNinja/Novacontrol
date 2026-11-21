"""Chronological policy evaluation over the isolated PI-13 environment."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from novacontrol.novamarket.backtesting.core import HistoricalBar
from novacontrol.novamarket.rl.environment import RLEnvironment
from novacontrol.novamarket.rl.observation import ObservationBuilder
from novacontrol.novamarket.rl.models import (
    RLEnvironmentConfig,
    RLInvalidActionError,
    RLValidationError,
)

from .engine import RewardEngine
from .metrics import policy_metrics
from .models import (
    EvaluationConfig,
    PolicyEvaluationError,
    PolicyEvaluationResult,
    RewardConfig,
    TransitionView,
    canonical_hash,
)
from .policies import Policy, policy_catalogue, policy_for
from .store import RewardStore

BarLoader = Callable[[RLEnvironmentConfig], Sequence[HistoricalBar]]
FeatureLoader = Callable[
    [RLEnvironmentConfig, datetime, Sequence[HistoricalBar]], Mapping[str, Any] | None
]
ForecastLoader = Callable[[RLEnvironmentConfig, datetime], Mapping[str, Any] | None]


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    config: RLEnvironmentConfig
    split: str
    bars: tuple[HistoricalBar, ...]
    features: Mapping[datetime, Mapping[str, Any]]
    forecasts: Mapping[datetime, Mapping[str, Any]]
    bars_hash: str
    start: datetime
    end: datetime
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        # Coverage is what the environment can READ at each bar, not which keys
        # exist: an all-null feature snapshot (or a forecast that is expired at
        # the observation) is evidence of nothing and must never read as 100%.
        builder = ObservationBuilder(self.config)
        feature_counts = _empty_coverage_counts()
        forecast_counts = _empty_coverage_counts()
        for bar in self.bars:
            buckets = builder.coverage(
                at=bar.observed_at,
                feature_snapshot=self.features.get(bar.observed_at),
                forecast_snapshot=self.forecasts.get(bar.observed_at),
            )
            feature_counts[buckets["features"]] += 1
            forecast_counts[buckets["forecasts"]] += 1
        bar_count = len(self.bars)
        feature_bars = feature_counts["available"]
        forecast_bars = forecast_counts["available"]
        # A source the environment config excluded was never requested, so its
        # ratio is not 0% coverage -- that reads as "nothing was available".
        # The ratio is not applicable and the disabled count stays as evidence.
        features_disabled = bar_count > 0 and feature_counts["disabled"] == bar_count
        forecasts_disabled = bar_count > 0 and forecast_counts["disabled"] == bar_count
        return {
            "dataset_id": "ds_"
            + canonical_hash(
                {
                    "bars_hash": self.bars_hash,
                    "split": self.split,
                    "dataset_version": self.config.dataset_version,
                }
            )[:20],
            "dataset_version": self.config.dataset_version,
            "split": self.split,
            "bars": bar_count,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "bars_hash": self.bars_hash,
            "coverage": {
                "bars": bar_count,
                "features": feature_bars,
                "feature_ratio": (
                    None
                    if features_disabled
                    else round(feature_bars / bar_count, 6)
                    if bar_count
                    else 0.0
                ),
                "feature_ratio_status": (
                    "not_applicable" if features_disabled else "measured"
                ),
                "feature_breakdown": feature_counts,
                "feature_basis": (
                    "bars with a point-in-time feature snapshot carrying at least "
                    "one usable value"
                ),
                "forecasts": forecast_bars,
                "forecast_ratio": (
                    None
                    if forecasts_disabled
                    else round(forecast_bars / bar_count, 6)
                    if bar_count
                    else 0.0
                ),
                "forecast_ratio_status": (
                    "not_applicable" if forecasts_disabled else "measured"
                ),
                "forecast_breakdown": forecast_counts,
                "forecast_basis": (
                    "bars with a forecast generated at or before the observation "
                    "that is not stale, expired, invalid or undated"
                ),
            },
            "warnings": list(self.warnings),
        }


class PolicyEvaluationEngine:
    """Runs policies on identical datasets; no production state is reachable."""

    def __init__(
        self,
        *,
        bar_loader: BarLoader | None = None,
        feature_loader: FeatureLoader | None = None,
        forecast_loader: ForecastLoader | None = None,
        reward_engine: RewardEngine | None = None,
        store: RewardStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bar_loader = bar_loader
        self._feature_loader = feature_loader
        self._forecast_loader = forecast_loader
        self._reward_engine = reward_engine or RewardEngine(clock=clock)
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def dataset(self, config: EvaluationConfig) -> EvaluationDataset:
        if self._bar_loader is None:
            raise PolicyEvaluationError("no historical dataset loader is configured")
        base = RLEnvironmentConfig.from_mapping(config.environment)
        split: str
        start = base.start
        end = base.end
        if not config.splits:
            if config.split != "all":
                raise PolicyEvaluationError(
                    "chronological splits are required unless the evaluation uses 'all'"
                )
            split = "all"
        else:
            definition = config.splits.get(config.split)
            if definition is None:
                raise PolicyEvaluationError(f"unknown chronological split {config.split!r}")
            split = definition.label
            start = definition.start
            end = definition.end
        if start < base.start or end > base.end:
            raise PolicyEvaluationError(
                "evaluation split must lie inside the configured environment window"
            )
        # Load a bounded history ending at the split, not only the split's own
        # rows: indicator warm-up must use the past, never the future.  The
        # loader itself must stop at the split end so future rows are never
        # requested or returned, even if its configured window is wider.
        load_config = replace(base, end=end, max_steps=5_000)
        all_bars = sorted(self._bar_loader(load_config), key=lambda item: item.observed_at)
        history_bars = tuple(
            item
            for item in all_bars
            if item.instrument_id == base.instrument_id and item.observed_at <= end
        )
        bars = tuple(item for item in history_bars if start <= item.observed_at <= end)
        if len(bars) < 2:
            raise PolicyEvaluationError(
                "the selected chronological split has fewer than two completed bars"
            )
        if len(bars) > 20_000:
            raise PolicyEvaluationError("evaluation split exceeds the 20,000-bar environment limit")
        effective = replace(base, start=start, end=end)
        features: dict[datetime, Mapping[str, Any]] = {}
        forecasts: dict[datetime, Mapping[str, Any]] = {}
        warnings: list[str] = []
        history: list[HistoricalBar] = []
        for bar in history_bars:
            history.append(bar)
            if bar.observed_at < start:
                continue
            if self._feature_loader is not None and effective.include_features:
                try:
                    snapshot = self._feature_loader(effective, bar.observed_at, tuple(history))
                    if snapshot:
                        features[bar.observed_at] = snapshot
                except Exception as exc:  # noqa: BLE001 - explicit gap
                    if len(warnings) < 20:
                        warnings.append(
                            f"features unavailable at {bar.observed_at.isoformat()}: "
                            f"{type(exc).__name__}"
                        )
            if self._forecast_loader is not None and effective.include_forecasts:
                try:
                    snapshot = self._forecast_loader(effective, bar.observed_at)
                    if snapshot:
                        forecasts[bar.observed_at] = snapshot
                except Exception as exc:  # noqa: BLE001 - explicit gap
                    if len(warnings) < 20:
                        warnings.append(
                            f"forecasts unavailable at {bar.observed_at.isoformat()}: "
                            f"{type(exc).__name__}"
                        )
        bars_hash = _bars_hash(bars)
        return EvaluationDataset(
            config=effective,
            split=split,
            bars=bars,
            features=features,
            forecasts=forecasts,
            bars_hash=bars_hash,
            start=start,
            end=end,
            warnings=tuple(warnings),
        )

    def run_policy(
        self,
        *,
        evaluation_id: str,
        dataset: EvaluationDataset,
        policy: Policy,
        reward_config: RewardConfig,
        max_transitions: int,
        initial_timestamp: datetime | None = None,
        cancel: Callable[[], bool] | None = None,
        evaluation_config: Mapping[str, Any] | None = None,
        conditions: Mapping[str, Any] | None = None,
    ) -> PolicyEvaluationResult:
        env_config = dataset.config
        env = RLEnvironment(
            env_config,
            dataset.bars,
            feature_snapshots=dataset.features,
            forecast_snapshots=dataset.forecasts,
            clock=self._clock,
        )
        episode_id = f"pe_{evaluation_id.removeprefix('pe_')}"
        env.reset(episode_id)
        transitions: list[TransitionView] = []
        transition_payloads: list[dict[str, Any]] = []
        reward_payloads: list[dict[str, Any]] = []
        invalid_actions = 0
        warnings = list(dataset.warnings)
        status = "completed"
        error = ""
        while env.status.value == "running" and len(transitions) < max_transitions:
            if cancel is not None and cancel():
                status = "cancelled"
                warnings.append("evaluation cancelled at the next step boundary")
                break
            observation = env.observation.to_dict()
            portfolio = env.portfolio.to_dict()
            try:
                decision = policy.select(
                    observation=observation,
                    portfolio=portfolio,
                    environment=env_config.to_dict(),
                    history=transition_payloads[-20:],
                )
            except Exception as exc:  # noqa: BLE001 - isolate a policy failure
                raise PolicyEvaluationError(
                    f"policy {policy.name!r} failed at transition "
                    f"{len(transitions) + 1}: {type(exc).__name__}: {exc}"
                ) from exc
            action_payload = dict(decision.action)
            try:
                _observation, _reward, terminated, truncated, transition = env.step(action_payload)
            except RLInvalidActionError as exc:
                invalid_actions += 1
                if len(warnings) < 20:
                    warnings.append(f"policy emitted invalid action: {exc}")
                try:
                    _observation, _reward, terminated, truncated, transition = env.step(
                        {"action_type": "wait"}
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    raise PolicyEvaluationError(
                        "policy emitted an invalid action and the safe fallback failed: "
                        f"{fallback_exc}"
                    ) from fallback_exc
            except RLValidationError as exc:
                invalid_actions += 1
                if len(warnings) < 20:
                    warnings.append(f"policy emitted a malformed action: {exc}")
                try:
                    _observation, _reward, terminated, truncated, transition = env.step(
                        {"action_type": "wait"}
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    raise PolicyEvaluationError(
                        "policy emitted a malformed action and the safe fallback failed: "
                        f"{fallback_exc}"
                    ) from fallback_exc
            transition_payload = transition.to_dict()
            view = TransitionView.from_transition(
                transition_payload, environment=env_config.to_dict()
            )
            transitions.append(view)
            transition_payloads.append(transition_payload)
            reward = self._reward_engine.calculate(
                transition_payload,
                config=reward_config,
                history=transition_payloads[:-1],
                environment=env_config.to_dict(),
            )
            reward_payloads.append(reward.to_dict())
            if self._store is not None:
                self._store.save_result(reward)
            if terminated or truncated:
                break
        if status == "cancelled":
            warnings.append(
                f"cancellation stopped the run after {len(transitions)} transition(s); "
                "the partial result is persisted as experimental evidence"
            )
        elif len(transitions) >= max_transitions and env.status.value == "running":
            status = "truncated"
            warnings.append(f"evaluation stopped at the {max_transitions}-transition bound")
        elif env.status.value == "truncated":
            status = "truncated"
            warnings.append(
                "evaluation reached the PI-13 environment step limit before the history ended"
            )
        elif env.status.value in {"failed", "cancelled"}:
            status = "failed"
            error = env.state().get("error") or env.state().get("termination_reason") or ""
        metrics, _trades = policy_metrics(
            initial_capital=env_config.initial_capital,
            transitions=transitions,
            rewards=reward_payloads,
            invalid_actions=invalid_actions,
        )
        learning = metrics.setdefault("learning", {})
        learning["episode_completion"] = status == "completed"
        learning["episode_truncated"] = status == "truncated"
        learning["episode_cancelled"] = status == "cancelled"
        learning["progression"] = "not applicable: PI-14 does not train a policy"
        created_at = self._clock()
        repro_hash = _repro_hash(
            config_hash=env_config.config_hash,
            bars_hash=dataset.bars_hash,
            policy=policy.name,
            policy_version=policy.version,
            reward_config_hash=reward_config.config_hash,
            transitions=[item.transition_id for item in transitions],
            reward_hashes=[item["result_hash"] for item in reward_payloads],
        )
        result = PolicyEvaluationResult(
            evaluation_id=evaluation_id,
            status=status,
            config_hash=env_config.config_hash,
            split=dataset.split,
            policy_name=policy.name,
            metrics=metrics,
            reward_summary={
                "total_reward": metrics.get("learning", {}).get("total_reward", 0.0),
                "average_reward": metrics.get("learning", {}).get("average_reward"),
                "reward_stability": metrics.get("learning", {}).get("reward_stability"),
                "component_totals": metrics.get("learning", {}).get("component_totals", {}),
                "component_averages": metrics.get("learning", {}).get("component_averages", {}),
                "penalty_totals": metrics.get("learning", {}).get("penalty_totals", {}),
                "reward_config_version": reward_config.version,
                "reward_config_hash": reward_config.config_hash,
            },
            action_distribution=metrics.get("decision", {}).get("action_distribution", {}),
            transitions=len(transitions),
            dataset_version=env_config.dataset_version,
            environment_version=env_config.environment_version,
            repro_hash=repro_hash,
            created_at=created_at,
            warnings=tuple(warnings),
            error=error,
            evaluation_config=dict(evaluation_config or {}),
            dataset=dataset.to_dict(),
            conditions=dict(conditions or {}),
        )
        if self._store is not None:
            self._store.save_evaluation(result)
        return result

    def compare(
        self,
        config: EvaluationConfig,
        *,
        policy_names: Sequence[str] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        dataset = self.dataset(config)
        names = tuple(policy_names or config.policies)
        catalogue = {row["name"]: row for row in policy_catalogue()}
        unknown = [name for name in names if name not in catalogue]
        if unknown:
            raise PolicyEvaluationError("unknown policy(ies): " + ", ".join(unknown))
        reward_config = RewardConfig.from_mapping(config.reward_config)
        results: list[dict[str, Any]] = []
        condition_payload = {
            "dataset_hash": dataset.bars_hash,
            "environment_config_hash": dataset.config.config_hash,
            "initial_capital": format(dataset.config.initial_capital, "f"),
            "execution_config": dict(dataset.config.execution_config),
            "start": dataset.start.isoformat(),
            "end": dataset.end.isoformat(),
            "split": dataset.split,
            "dataset_version": dataset.config.dataset_version,
        }
        for name in names:
            policy = policy_for(name, seed=dataset.config.random_seed)
            evaluation_id = _evaluation_id(
                config.config_hash, name, dataset.bars_hash, self._clock()
            )
            result = self.run_policy(
                evaluation_id=evaluation_id,
                dataset=dataset,
                policy=policy,
                reward_config=reward_config,
                max_transitions=config.max_transitions,
                cancel=cancel,
                evaluation_config=config.to_dict(),
                conditions=condition_payload,
            )
            results.append(
                {
                    **result.to_dict(),
                    "baseline_type": catalogue[name]["type"],
                    "policy_version": policy.version,
                    "limitations": catalogue[name]["limitations"],
                    "conditions": condition_payload,
                }
            )
        comparison_id = (
            "rw_cmp_"
            + hashlib.sha256(
                (
                    config.config_hash
                    + "|"
                    + dataset.bars_hash
                    + "|"
                    + ",".join(names)
                    + "|"
                    + str(len(results))
                ).encode()
            ).hexdigest()[:24]
        )
        comparison = {
            "comparison_id": comparison_id,
            "config": config.to_dict(),
            "config_hash": config.config_hash,
            "dataset": dataset.to_dict(),
            "conditions_identical": True,
            "condition_fingerprint": canonical_hash(condition_payload),
            "policy_names": list(names),
            "results": results,
            "created_at": self._clock().isoformat(),
            "baseline_catalogue": list(catalogue.values()),
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
            "claim": "identical historical simulation conditions; not future performance",
        }
        if self._store is not None:
            self._store.save_comparison(comparison)
        return comparison


def _empty_coverage_counts() -> dict[str, int]:
    return {"available": 0, "stale": 0, "missing": 0, "invalid": 0, "disabled": 0}


def _bars_hash(bars: Sequence[HistoricalBar]) -> str:
    payload = [
        {
            "instrument_id": item.instrument_id,
            "observed_at": item.observed_at.isoformat(),
            "open": str(item.open),
            "high": str(item.high),
            "low": str(item.low),
            "close": str(item.close),
            "volume": str(item.volume),
            "source": item.source,
        }
        for item in bars
    ]
    return canonical_hash({"bars": payload})


def _repro_hash(
    *,
    config_hash: str,
    bars_hash: str,
    policy: str,
    policy_version: str,
    reward_config_hash: str,
    transitions: Sequence[str],
    reward_hashes: Sequence[str],
) -> str:
    return canonical_hash(
        {
            "config_hash": config_hash,
            "bars_hash": bars_hash,
            "policy": policy,
            "policy_version": policy_version,
            "reward_config_hash": reward_config_hash,
            "transitions": list(transitions),
            "reward_hashes": list(reward_hashes),
        }
    )


def _evaluation_id(config_hash: str, policy: str, bars_hash: str, at: datetime) -> str:
    return (
        "pe_"
        + hashlib.sha256(
            (config_hash + "|" + policy + "|" + bars_hash + "|" + at.isoformat()).encode()
        ).hexdigest()[:24]
    )


__all__ = ["EvaluationDataset", "PolicyEvaluationEngine"]
