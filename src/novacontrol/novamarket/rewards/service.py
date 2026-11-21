"""PI-14 application service for rewards and policy evaluation."""

from __future__ import annotations

import hashlib
import statistics
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .engine import RewardEngine
from .evaluation import PolicyEvaluationEngine
from .models import (
    DEFAULT_THRESHOLDS,
    MAX_WALK_FORWARD_WINDOWS,
    EvaluationConfig,
    PolicyEvaluationError,
    RewardConfig,
    RewardResult,
    RewardValidationError,
    SplitDefinition,
    canonical_hash,
    default_reward_config,
)
from .policies import policy_catalogue, policy_for
from .store import RewardStore, RewardStoreError

TransitionReader = Callable[[str], Sequence[Mapping[str, Any]]]
MAX_PENDING_CANCELLATIONS = 128
MAX_REQUEST_ID_LENGTH = 128
MAX_CONCURRENT_EVALUATIONS = 2


class RewardService:
    """Coordinates reward calculation and isolated policy evaluation.

    This class is intentionally not given a portfolio, risk, execution or
    autonomy handle. The only data it can reach is the read-only transition
    callback and the historical-bar loader used by the PI-13 environment.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        transition_reader: TransitionReader,
        bar_loader: Callable[[Any], Sequence[Any]],
        feature_loader: Callable[[Any, datetime, Sequence[Any]], Mapping[str, Any] | None]
        | None = None,
        forecast_loader: Callable[[Any, datetime], Mapping[str, Any] | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._engine = RewardEngine(clock=self._clock)
        self._store = RewardStore(path)
        self._transition_reader = transition_reader
        self._evaluation = PolicyEvaluationEngine(
            bar_loader=bar_loader,
            feature_loader=feature_loader,
            forecast_loader=forecast_loader,
            reward_engine=self._engine,
            store=self._store,
            clock=self._clock,
        )
        self._lock = threading.RLock()
        self._last_error = ""
        self._cancel_events: dict[str, threading.Event] = {}
        self._evaluation_slots = threading.BoundedSemaphore(MAX_CONCURRENT_EVALUATIONS)
        self._evaluation_state_lock = threading.Lock()
        self._active_evaluations = 0
        self._ensure_default_config()

    def _acquire_evaluation(self) -> None:
        if not self._evaluation_slots.acquire(blocking=False):
            raise PolicyEvaluationError(
                f"at most {MAX_CONCURRENT_EVALUATIONS} policy evaluations may run at once"
            )
        with self._evaluation_state_lock:
            self._active_evaluations += 1

    def _release_evaluation(self) -> None:
        with self._evaluation_state_lock:
            self._active_evaluations = max(0, self._active_evaluations - 1)
        self._evaluation_slots.release()

    def _compare_guarded(
        self, config: EvaluationConfig, *, cancel: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        self._acquire_evaluation()
        try:
            return self._evaluation.compare(config, cancel=cancel)
        finally:
            self._release_evaluation()

    def _ensure_default_config(self) -> None:
        self._store.save_config(default_reward_config())

    @property
    def store(self) -> RewardStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    # -- status and catalogues ------------------------------------------------
    def status(self) -> dict[str, Any]:
        try:
            storage = self._store.status()
        except RewardStoreError as exc:
            storage = {"error": str(exc)}
        return {
            "phase": "PI-14",
            "implemented": True,
            "initialised": True,
            "engine": self._engine.status(),
            "storage": storage,
            "active_evaluations": self._active_evaluations,
            "max_concurrent_evaluations": MAX_CONCURRENT_EVALUATIONS,
            "baseline_policies": policy_catalogue(),
            "last_error": self._last_error,
            "simulated": True,
            "experimental": True,
            "advisory_only": True,
            "production_authority": False,
            "isolation": {
                "production_portfolio_mutation": False,
                "production_position_mutation": False,
                "production_ledger_write": False,
                "production_paper_fill": False,
                "nm10_order_submission": False,
                "nm08_bypass": False,
                "autonomy_mode_change": False,
                "operator_permission_change": False,
                "automatic_policy_promotion": False,
                "live_trading": False,
            },
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": "PI-14",
            "contract_version": "pi14-reward-v1",
            "reward_schema": "pi14-reward-result-v1",
            "evaluation_schema": "pi14-policy-evaluation-v1",
            "components": self._engine.registry,
            "normalization": {
                "default": "bounded",
                "default_bounds": [-1.0, 1.0],
                "learned_parameters": False,
                "fit_data": "none; experimental fixed scales",
            },
            "configurable": {
                "weights": True,
                "scales": True,
                "bounds": True,
                "thresholds": sorted(DEFAULT_THRESHOLDS),
                "penalties": True,
                "missing_policy": ["renormalize", "zero"],
            },
            "baseline_policies": policy_catalogue(),
            "chronological_splits": True,
            "walk_forward": True,
            "production_authority": False,
            "advisory_only": True,
        }

    def health(self) -> dict[str, Any]:
        try:
            storage = self._store.status()
            db_ok = "error" not in storage
        except Exception as exc:  # noqa: BLE001
            return {
                "state": "error",
                "healthy": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        return {
            "state": "ok" if db_ok else "degraded",
            "healthy": db_ok,
            "storage": storage,
            "engine_calculations": self._engine.status().get("calculations", 0),
        }

    # -- reward configuration -------------------------------------------------
    def create_config(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            config = default_reward_config() if not payload else RewardConfig.from_mapping(payload)
            saved = self._store.save_config(config)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        return {
            "ok": True,
            "configuration": saved,
            "reused": saved.get("config_hash") == config.config_hash,
            "experimental": config.experimental,
            "simulated": True,
        }

    def config(self, config_id: str) -> dict[str, Any]:
        found = self._store.config(config_id)
        if found is None:
            raise RewardValidationError(f"unknown reward configuration {config_id!r}")
        return found

    def configs(self, limit: int = 50) -> dict[str, Any]:
        return self._store.configs(limit)

    def default_config(self) -> RewardConfig:
        return default_reward_config()

    # -- reward calculation ---------------------------------------------------
    def calculate_episode(
        self,
        episode_id: str,
        *,
        config_id: str = "",
        persist: bool = True,
    ) -> dict[str, Any]:
        config_payload = self.config(config_id) if config_id else default_reward_config().to_dict()
        config = RewardConfig.from_mapping(config_payload)
        transitions = list(self._transition_reader(episode_id))
        if not transitions:
            raise RewardValidationError(f"episode {episode_id!r} has no stored transitions")
        results: list[RewardResult] = []
        payloads: list[dict[str, Any]] = []
        for index, transition in enumerate(transitions):
            result = self._engine.calculate(
                transition,
                config=config,
                history=transitions[max(0, index - 50) : index],
            )
            results.append(result)
            payloads.append(result.to_dict())
            if persist:
                self._store.save_result(result)
        totals = [result.total for result in results]
        component_totals: dict[str, float] = {}
        penalties: dict[str, float] = {}
        missing: set[str] = set()
        for result in results:
            for name, component in result.components.items():
                contribution = float(component.get("contribution") or 0.0)
                component_totals[name] = round(component_totals.get(name, 0.0) + contribution, 10)
            for name, value in result.penalties.items():
                penalties[name] = round(penalties.get(name, 0.0) + float(value), 10)
            missing.update(result.missing_components)
        trade_attribution = []
        for result in results:
            action = result.components.get("decision_quality", {}).get("metrics", {}).get("action")
            if action not in {"buy", "sell"}:
                continue
            trade_attribution.append(
                {
                    "transition_id": result.transition_id,
                    "action": action,
                    "total_reward": result.total,
                    "component_contributions": {
                        name: component.get("contribution")
                        for name, component in result.components.items()
                        if component.get("enabled")
                    },
                }
            )
        summary = {
            "episode_id": episode_id,
            "config_id": config.config_id,
            "config_hash": config.config_hash,
            "reward_count": len(results),
            "total_reward": round(sum(totals), 10),
            "average_reward": round(statistics.fmean(totals), 10) if totals else None,
            "reward_stability": round(statistics.pstdev(totals), 10) if len(totals) > 1 else 0.0,
            "component_totals": component_totals,
            "trade_count": len(trade_attribution),
            "trade_attribution": trade_attribution,
            "penalties": penalties,
            "missing_components": sorted(missing),
            "persisted": persist,
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }
        return {"ok": True, "summary": summary, "rewards": payloads}

    def episode_rewards(self, episode_id: str, *, limit: int = 200) -> dict[str, Any]:
        return self._store.results(episode_id, limit=limit)

    def reward(self, reward_id: str) -> dict[str, Any]:
        found = self._store.result(reward_id)
        if found is None:
            raise RewardValidationError(f"unknown reward result {reward_id!r}")
        return found

    # -- policy evaluation ----------------------------------------------------
    def evaluate(
        self,
        payload: Mapping[str, Any],
        *,
        policy_names: Sequence[str] | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        request = str(request_id or payload.get("request_id") or "").strip()
        if len(request) > MAX_REQUEST_ID_LENGTH:
            raise RewardValidationError(
                f"request_id must be at most {MAX_REQUEST_ID_LENGTH} characters"
            )
        # request_id is transport metadata, not part of the immutable evaluation contract.
        config_payload = {key: value for key, value in payload.items() if key != "request_id"}
        config = EvaluationConfig.from_mapping(config_payload)
        reward_config = RewardConfig.from_mapping(config.reward_config)
        self._store.save_config(reward_config)
        names = tuple(policy_names or config.policies)
        try:
            seed = int(config.environment.get("random_seed", 42))
        except (TypeError, ValueError) as exc:
            raise RewardValidationError("environment random_seed must be an integer") from exc
        for name in names:
            policy_for(name, seed=seed)
        self._acquire_evaluation()
        try:
            event: threading.Event | None = None
            if request:
                with self._lock:
                    if request not in self._cancel_events and len(self._cancel_events) >= (
                        MAX_PENDING_CANCELLATIONS
                    ):
                        raise RewardValidationError(
                            "too many pending evaluation cancellation requests"
                        )
                    event = self._cancel_events.setdefault(request, threading.Event())
            try:
                comparison = self._evaluation.compare(
                    config,
                    policy_names=names,
                    cancel=(event.is_set if event is not None else None),
                )
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                if request:
                    with self._lock:
                        self._cancel_events.pop(request, None)
        finally:
            self._release_evaluation()
        return {
            **comparison,
            "ok": True,
            "request_id": request,
            "cancelled": any(item["status"] == "cancelled" for item in comparison["results"]),
            "evaluation_ids": [item["evaluation_id"] for item in comparison["results"]],
        }

    def evaluate_experimental_policy(
        self,
        policy: Any,
        payload: Mapping[str, Any],
        *,
        policy_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate one trained PI-15 policy beside the PI-14 baselines.

        The custom policy is supplied in memory by the isolated training
        service.  It is never written to PI-14's policy catalogue and can never
        become a production policy.  All policies run on one loaded dataset in
        this invocation so their conditions are identical by construction.
        """
        config = EvaluationConfig.from_mapping(payload)
        reward_config = RewardConfig.from_mapping(config.reward_config)
        self._store.save_config(reward_config)
        catalogue = {row["name"]: row for row in policy_catalogue()}
        unknown = [name for name in config.policies if name not in catalogue]
        if unknown:
            raise RewardValidationError(
                "trained-policy evaluation baselines must be PI-14 catalogue policies: "
                + ", ".join(unknown)
            )
        policy_name = str(getattr(policy, "name", "") or "")
        policy_version = str(getattr(policy, "version", "") or "")
        if not policy_name or not policy_version:
            raise RewardValidationError("trained policy name and version are required")
        self._acquire_evaluation()
        try:
            dataset = self._evaluation.dataset(config)
            condition_payload = {
                "dataset_hash": dataset.bars_hash,
                "environment_config_hash": dataset.config.config_hash,
                "initial_capital": format(dataset.config.initial_capital, "f"),
                "execution_config": dict(dataset.config.execution_config),
                "start": dataset.start.isoformat(),
                "end": dataset.end.isoformat(),
                "split": dataset.split,
                "dataset_version": dataset.config.dataset_version,
                "reward_config_hash": reward_config.config_hash,
            }
            metadata = dict(policy_metadata or {})
            evaluation_config = {
                **config.to_dict(),
                "comparison_kind": "pi15_experimental_trained_policy",
                "trained_policy": policy_name,
                "trained_policy_version": policy_version,
                "trained_policy_metadata": metadata,
            }
            results: list[dict[str, Any]] = []
            names = list(config.policies)
            for name in names:
                baseline = policy_for(name, seed=dataset.config.random_seed)
                evaluation_id = (
                    "pe_"
                    + hashlib.sha256(
                        (
                            config.config_hash
                            + "|"
                            + name
                            + "|"
                            + dataset.bars_hash
                            + "|"
                            + policy_version
                        ).encode()
                    ).hexdigest()[:24]
                )
                result = self._evaluation.run_policy(
                    evaluation_id=evaluation_id,
                    dataset=dataset,
                    policy=baseline,
                    reward_config=reward_config,
                    max_transitions=config.max_transitions,
                    evaluation_config=evaluation_config,
                    conditions=condition_payload,
                )
                results.append(
                    {
                        **result.to_dict(),
                        "baseline_type": catalogue[name]["type"],
                        "policy_version": baseline.version,
                        "limitations": catalogue[name]["limitations"],
                        "conditions": condition_payload,
                    }
                )
            trained_evaluation_id = (
                "pe_"
                + hashlib.sha256(
                    (
                        config.config_hash
                        + "|"
                        + policy_name
                        + "|"
                        + dataset.bars_hash
                        + "|"
                        + policy_version
                    ).encode()
                ).hexdigest()[:24]
            )
            trained_result = self._evaluation.run_policy(
                evaluation_id=trained_evaluation_id,
                dataset=dataset,
                policy=policy,
                reward_config=reward_config,
                max_transitions=config.max_transitions,
                evaluation_config=evaluation_config,
                conditions=condition_payload,
            )
            results.append(
                {
                    **trained_result.to_dict(),
                    "baseline_type": "experimental_trained_policy",
                    "policy_version": policy_version,
                    "limitations": (
                        "Experimental PI-15 policy evaluated on a chronological "
                        "holdout; historical simulation is not evidence of future returns."
                    ),
                    "conditions": condition_payload,
                    "policy_metadata": metadata,
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
                        + ",".join([*names, policy_name])
                        + "|"
                        + policy_version
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
                "policy_names": [*names, policy_name],
                "results": results,
                "created_at": self._clock().isoformat(),
                "baseline_catalogue": list(catalogue.values()),
                "trained_policy": policy_name,
                "trained_policy_version": policy_version,
                "simulated": True,
                "experimental": True,
                "advisory_only": True,
                "production_authority": False,
                "promotion_available": False,
                "claim": (
                    "identical historical simulation conditions; not future performance "
                    "and not permission to activate the policy"
                ),
            }
            self._store.save_comparison(comparison)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._release_evaluation()
        return {"ok": True, "comparison": comparison}

    def request_cancel(self, request_id: str) -> dict[str, Any]:
        request = str(request_id or "").strip()
        if not request:
            raise RewardValidationError("request_id is required to cancel an evaluation")
        if len(request) > MAX_REQUEST_ID_LENGTH:
            raise RewardValidationError(
                f"request_id must be at most {MAX_REQUEST_ID_LENGTH} characters"
            )
        with self._lock:
            if request not in self._cancel_events and len(self._cancel_events) >= (
                MAX_PENDING_CANCELLATIONS
            ):
                raise RewardValidationError("too many pending evaluation cancellation requests")
            event = self._cancel_events.setdefault(request, threading.Event())
            event.set()
        return {
            "ok": True,
            "request_id": request,
            "cancel_requested": True,
            "bound": (
                "the evaluation stops at its next simulation step; partial evidence is persisted"
            ),
            "simulated": True,
            "advisory_only": True,
        }

    def evaluations(self, limit: int = 50) -> dict[str, Any]:
        return self._store.evaluations(limit)

    def evaluation(self, evaluation_id: str) -> dict[str, Any]:
        found = self._store.evaluation(evaluation_id)
        if found is None:
            raise RewardValidationError(f"unknown policy evaluation {evaluation_id!r}")
        return found

    def comparisons(self, limit: int = 50) -> dict[str, Any]:
        return self._store.comparisons(limit)

    def baselines(self) -> dict[str, Any]:
        return {
            "policies": policy_catalogue(),
            "identical_conditions_required": True,
            "conditions": [
                "historical dataset and version",
                "historical period and chronological split",
                "initial virtual capital",
                "execution configuration, fees and slippage",
                "instrument universe",
                "risk constraints",
                "reward configuration",
            ],
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }

    def stability(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate every explicitly defined chronological split."""
        config = EvaluationConfig.from_mapping(payload)
        if not config.splits:
            raise PolicyEvaluationError("stability evaluation requires named chronological splits")
        results: list[dict[str, Any]] = []
        for label in config.splits:
            split_config = replace(config, split=label)
            results.append(self._compare_guarded(split_config))
        return {
            "ok": True,
            "name": config.name,
            "config_hash": config.config_hash,
            "splits": list(config.splits),
            "runs": results,
            "stability_claim": "observed across the supplied historical splits only",
            "generalization_claim": False,
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }

    def walk_forward(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate successive out-of-sample windows using chronological order.

        No parameter is fitted on a test window. The train split exists to
        demonstrate the boundary and would be the only place any future fitted
        normalizer or hyperparameter search could read.
        """
        config = EvaluationConfig.from_mapping(payload)
        if not config.walk_forward:
            raise PolicyEvaluationError("walk_forward must be true for this operation")
        base = config.environment
        from novacontrol.novamarket.rl.models import RLEnvironmentConfig

        environment = RLEnvironmentConfig.from_mapping(base)
        # The evaluation loader is allowed a larger read window than an episode's
        # max_steps; the env itself still applies the configured step ceiling.

        loader = getattr(self._evaluation, "_bar_loader", None)
        if loader is None:
            raise PolicyEvaluationError("no historical dataset loader is configured")
        try:
            bars = sorted(
                loader(replace(environment, max_steps=5_000)),
                key=lambda item: item.observed_at,
            )
        except Exception as exc:  # noqa: BLE001
            raise PolicyEvaluationError(f"walk-forward dataset load failed: {exc}") from exc
        bars = [
            item
            for item in bars
            if item.instrument_id == environment.instrument_id
            and environment.start <= item.observed_at <= environment.end
        ]
        if len(bars) < config.walk_forward_train + config.walk_forward_test:
            raise PolicyEvaluationError(
                "the historical window is too short for walk-forward train/test windows"
            )
        window_count = 1 + max(
            0,
            (len(bars) - config.walk_forward_train - config.walk_forward_test)
            // max(1, config.walk_forward_step),
        )
        if window_count > MAX_WALK_FORWARD_WINDOWS:
            raise PolicyEvaluationError(
                "walk-forward request would create "
                f"{window_count} windows; the limit is {MAX_WALK_FORWARD_WINDOWS}"
            )
        runs: list[dict[str, Any]] = []
        train = config.walk_forward_train
        test = config.walk_forward_test
        step = config.walk_forward_step
        start = 0
        while start + train + test <= len(bars):
            train_bars = bars[start : start + train]
            test_bars = bars[start + train : start + train + test]
            splits = {
                "train": SplitDefinition(
                    "train", train_bars[0].observed_at, train_bars[-1].observed_at
                ),
                "test": SplitDefinition(
                    "test", test_bars[0].observed_at, test_bars[-1].observed_at
                ),
            }
            window = replace(config, split="test", splits=splits, walk_forward=False)
            runs.append(
                {
                    "window": len(runs) + 1,
                    "train_start": train_bars[0].observed_at.isoformat(),
                    "train_end": train_bars[-1].observed_at.isoformat(),
                    "test_start": test_bars[0].observed_at.isoformat(),
                    "test_end": test_bars[-1].observed_at.isoformat(),
                    "comparison": self._compare_guarded(window),
                }
            )
            start += max(1, step)
        return {
            "ok": True,
            "name": config.name,
            "windows": len(runs),
            "train_bars": train,
            "test_bars": test,
            "step_bars": step,
            "results": runs,
            "no_test_fitting": True,
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }


__all__ = ["RewardService", "TransitionReader"]
