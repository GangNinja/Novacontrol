"""Bounded training orchestration, monitoring and checkpoint recovery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import statistics
import threading
import time
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from novacontrol.novamarket.rewards.engine import RewardEngine
from novacontrol.novamarket.rewards.models import canonical_hash
from novacontrol.novamarket.rewards.regimes import MIN_REGIME_SAMPLES
from novacontrol.novamarket.rl.environment import RLEnvironment
from novacontrol.novamarket.rl.models import (
    RLInvalidActionError,
    RLValidationError,
)

from .agent import (
    ACTION_NAMES,
    DQNAgent,
    ObservationEncoder,
    TrainedPolicy,
    action_payload,
)
from .datasets import TrainingDataBundle, TrainingDataPreparer
from .models import (
    CHECKPOINT_SCHEMA_VERSION,
    MAX_COMPARED_POLICIES,
    MAX_CONFIGS,
    MAX_OPTIMIZATION_TRIALS,
    MAX_RUNS,
    POLICY_SCHEMA_VERSION,
    TRAINING_CONTRACT_VERSION,
    OptimizationTrial,
    PolicyArtifact,
    TrainingConfig,
    TrainingResourceError,
    TrainingRun,
    TrainingRunStatus,
    TrainingStateError,
    TrainingStoreError,
    TrainingValidationError,
    allowed_policy_transition,
    utcnow,
    validation_payload,
)
from .models import (
    canonical_hash as config_hash,
)
from .stability import (
    DEFAULT_STABILITY_LIMITS,
    FAIL as STABILITY_FAIL,
    stability_record,
)
from .store import TrainingStore
from .walkforward import (
    DEFAULT_WALK_FORWARD_FOLDS,
    MAX_WALK_FORWARD_FOLDS,
    WalkForwardFold,
    build_walk_forward_folds,
    fold_config,
    select_fold,
)

MAX_CONCURRENT_TRAINING = 1
CHECKPOINT_RETENTION = 20
MAX_SEARCH_OBSERVATIONS = 20
TRAINING_ALGORITHM_VERSION = "pi15-dqn-pure-python-1"
TRAINING_FRAMEWORK = "pure-python-stdlib"
SUPPORTED_OPTIMIZATION_FIELDS: dict[str, tuple[Any, ...]] = {
    "learning_rate": (0.0003, 0.001, 0.003),
    "gamma": (0.95, 0.99, 0.995),
    "batch_size": (16, 32, 64),
    "hidden_layers": ((32,), (64, 32), (64, 64)),
    "epsilon_end": (0.02, 0.05, 0.10),
    "target_update_interval": (10, 25, 50),
}


class _Cancelled(RuntimeError):
    pass


class _UnstableTraining(RuntimeError):
    """A stability monitor requires the run to stop instead of continuing."""


class _NumericalInstability(RuntimeError):
    pass


@dataclass
class _Runtime:
    run_id: str
    config: TrainingConfig
    bundle: TrainingDataBundle | None = None
    resume_state: Mapping[str, Any] | None = None
    agent: DQNAgent | None = None
    thread: threading.Thread | None = None
    pause_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    checkpoint_requested: threading.Event = field(default_factory=threading.Event)
    started_monotonic: float = 0.0
    slot_acquired: bool = False
    #: Why the run was cancelled; a slot owner cancelling a parked run records
    #: the stability pause here so the run does not report a generic request.
    cancel_reason: str = ""

    def __post_init__(self) -> None:
        self.pause_event = threading.Event()
        self.cancel_event = threading.Event()
        self.checkpoint_requested = threading.Event()


@dataclass(frozen=True, slots=True)
class _EpisodeStep:
    observation: Mapping[str, Any]
    next_observation: Mapping[str, Any]
    reward: float
    done: bool
    invalid_action: bool
    transition: Mapping[str, Any]


class TrainingManager:
    """Owns isolated training threads and their research artifacts.

    No constructor argument is a portfolio, position store, ledger, NM-08 risk
    engine, NM-10 execution engine or autonomy controller.  The only data path
    is the historical bar/feature/forecast loader and the PI-14 reward service.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        bar_loader: Any,
        feature_loader: Any = None,
        forecast_loader: Any = None,
        reward_service: Any,
        artifact_dir: str | Path | None = None,
        clock: Any = None,
    ) -> None:
        self._path = Path(path)
        self._store = TrainingStore(path)
        self._artifact_dir = (
            Path(artifact_dir) if artifact_dir else self._path.parent / "rl_training"
        )
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_dir = self._artifact_dir / "checkpoints"
        self._policy_dir = self._artifact_dir / "policies"
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._policy_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reward_service = reward_service
        self._preparer = TrainingDataPreparer(
            bar_loader=bar_loader,
            feature_loader=feature_loader,
            forecast_loader=forecast_loader,
            clock=self._clock,
        )
        self._reward_engine = RewardEngine(clock=self._clock)
        self._lock = threading.RLock()
        self._runtimes: dict[str, _Runtime] = {}
        self._runs: dict[str, TrainingRun] = {}
        self._search_states: dict[str, dict[str, Any]] = {}
        self._walk_forward_states: dict[str, dict[str, Any]] = {}
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_TRAINING)
        self._closed = False
        self._last_error = ""
        self._last_checkpoint: dict[str, Any] | None = None
        self.recover_interrupted()

    def recover_interrupted(self) -> dict[str, Any]:
        """Close runs left mid-flight by a previous application process.

        A checkpoint is preserved and can be resumed explicitly; the interrupted
        run itself is never reported as successful. This runs once when the
        manager is constructed, before any new worker is started.
        """
        recovered: list[str] = []
        for raw in self._store.interrupted_runs():
            run = TrainingRun.from_dict(raw)
            with self._lock:
                if run.run_id in self._runtimes:
                    continue
            reason = "application interrupted; checkpoint recovery is available"
            self._save_run(
                run.with_status(
                    TrainingRunStatus.FAILED,
                    error=reason,
                    stop_reason="interrupted",
                    ended_at=self._clock().isoformat(),
                )
            )
            if run.policy_id:
                try:
                    policy = self._get_policy(run.policy_id)
                    if policy.status in {"created", "training", "validation_pending"}:
                        self._save_policy(
                            policy.with_status(
                                "rejected",
                                creation_metadata={
                                    **dict(policy.creation_metadata),
                                    "recovery": reason,
                                },
                            )
                        )
                except Exception:  # noqa: BLE001 - recovery must not abort startup
                    pass
            recovered.append(run.run_id)
        if recovered:
            self._last_error = (
                f"recovered {len(recovered)} interrupted training run(s); "
                "resume from a checkpoint to continue"
            )
        return {
            "ok": True,
            "recovered": recovered,
            "count": len(recovered),
            "note": "interrupted runs are failed, never silently completed",
        }

    @property
    def storage_path(self) -> Path:
        return self._path

    # -- status and contracts -------------------------------------------------
    def status(self) -> dict[str, Any]:
        counts = self._store.counts()
        with self._lock:
            active = sum(1 for item in self._runtimes.values() if item.thread is not None)
            paused = sum(
                1
                for item in self._runtimes.values()
                if item.pause_event.is_set() and item.thread is not None
            )
        return {
            "phase": "PI-15",
            "implemented": True,
            "initialised": True,
            "algorithm": "dqn",
            "algorithm_version": TRAINING_ALGORITHM_VERSION,
            "contract_version": TRAINING_CONTRACT_VERSION,
            "active_runs": active,
            "paused_runs": paused,
            "max_concurrent_runs": MAX_CONCURRENT_TRAINING,
            "counts": counts,
            "storage": self._store.storage_info(),
            "last_checkpoint": self._last_checkpoint,
            "last_error": self._last_error,
            "simulated": True,
            "experimental": True,
            "advisory_only": True,
            "production_authority": False,
            "activation_available": False,
            "isolation": self._isolation(),
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": "PI-15",
            "contract_version": TRAINING_CONTRACT_VERSION,
            "policy_schema": POLICY_SCHEMA_VERSION,
            "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
            "algorithms": [
                {
                    "name": "dqn",
                    "primary": True,
                    "version": TRAINING_ALGORITHM_VERSION,
                    "rationale": (
                        "PI-13 exposes five discrete actions and a bounded numeric observation. "
                        "DQN directly estimates action values and supports experience replay "
                        "and a target network without a second policy runtime."
                    ),
                    "action_space": "discrete: buy, sell, hold, wait, no_action",
                    "observation_space": "fixed numeric PI-13 observation projection",
                    "limitations": (
                        "Off-policy value learning can overestimate rare actions, is sensitive "
                        "to reward scale and does not establish financial profitability."
                    ),
                    "comparison_algorithms": [],
                }
            ],
            "actions": list(ACTION_NAMES),
            "compatible_environments": "PI-13 RLEnvironmentConfig contract",
            "rewards": "PI-14 RewardConfig and PolicyEvaluationEngine",
            "validation_baselines": ["no_action", "rule_based", "pi12_adapter"],
            "chronological_splits": ["train", "validation", "test"],
            "hyperparameter_methods": ["random", "grid"],
            "limits": {
                "max_concurrent_runs": MAX_CONCURRENT_TRAINING,
                "max_optimization_trials": MAX_OPTIMIZATION_TRIALS,
                "max_checkpoints_per_run": CHECKPOINT_RETENTION,
                "max_compared_policies": MAX_COMPARED_POLICIES,
            },
            "stability": {
                "monitors": [
                    "non_finite_or_exploding_loss",
                    "reward_collapse",
                    "action_repetition",
                    "invalid_action_spike",
                ],
                "limits": DEFAULT_STABILITY_LIMITS.to_dict(),
                "response": {
                    "fail": (
                        "stop the run, keep a failure checkpoint and reject the artifact"
                    ),
                    "pause": (
                        "write a checkpoint and park in 'paused' until an operator resumes"
                    ),
                },
                "selection_impact": "a paused or failed run is never validated or promoted",
            },
            "regimes": {
                "slice": "per-market-regime validation and out-of-sample metrics",
                "label_source": (
                    "PI-04 RegimeDetector over each PI-13 observation's own feature "
                    "snapshot; PI-13 observation evidence (return_20, volatility_20) "
                    "only where no features exist"
                ),
                "label_granularity": "one label per observation, no cross-split inference",
                "min_samples": MIN_REGIME_SAMPLES,
                "used_for_training": False,
                "used_for_selection": False,
            },
            "walk_forward": {
                "max_folds": MAX_WALK_FORWARD_FOLDS,
                "window": "rolling chronological train -> validation -> test folds",
                "execution": "bounded and sequential; one fold run at a time",
                "overlap": (
                    "refused before the first fold runs; a fold's test window never "
                    "trains, validates or selects another fold"
                ),
                "selection_basis": "validation",
            },
            "production_authority": False,
        }

    def health(self) -> dict[str, Any]:
        try:
            storage = self._store.storage_info()
        except Exception as exc:  # noqa: BLE001
            return {
                "state": "error",
                "healthy": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        resource = self._resource_snapshot()
        with self._lock:
            active = sum(1 for item in self._runtimes.values() if item.thread is not None)
        return {
            "state": "ok",
            "healthy": True,
            "active_runs": active,
            "max_concurrent_runs": MAX_CONCURRENT_TRAINING,
            "storage": storage,
            "resource": resource,
            "checkpoint_directory": str(self._checkpoint_dir),
            "policy_directory": str(self._policy_dir),
            "last_error": self._last_error,
        }

    @staticmethod
    def _isolation() -> dict[str, bool]:
        return {
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
            "broker_connection": False,
            "activation_control": False,
        }

    # -- configurations -------------------------------------------------------
    def validate_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(payload)
        values.pop("request_id", None)
        if "config" in values and isinstance(values["config"], Mapping):
            values = dict(values["config"])
        result = validation_payload(values)
        if result.get("valid"):
            return self._probe_dataset(result, values)
        return result

    def _probe_dataset(
        self, result: dict[str, Any], values: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Prepare the three chronological windows, so ``valid`` means startable.

        The schema check cannot see PI-02 storage or the bar supply.  Without
        this probe a configuration validates, its run is then refused at
        launch, and the form has already told the user the configuration was
        fine.  Preparing is exactly the step the worker takes first, so an
        unknown dataset or a split with no trades is reported here instead.
        """
        try:
            self._preparer.prepare(TrainingConfig.from_mapping(values))
        except Exception as exc:  # noqa: BLE001 - the reason is the validation error
            errors = [*result.get("errors", ()), f"dataset is not startable: {exc}"]
            return {**result, "valid": False, "errors": errors}
        return result

    def create_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(payload)
        values.pop("request_id", None)
        if "config" in values and isinstance(values["config"], Mapping):
            values = dict(values["config"])
        config = TrainingConfig.from_mapping(values)
        existing = self._store.configs(MAX_CONFIGS)
        if existing["count"] >= MAX_CONFIGS and self._store.config(config.config_id) is None:
            raise TrainingStoreError("training configuration record limit reached")
        saved = self._store.save_config(config)
        return {
            "ok": True,
            "configuration": saved,
            "validation": validation_payload(config.payload()),
            "simulated": True,
            "experimental": True,
        }

    def configs(self, limit: int = 50) -> dict[str, Any]:
        return self._store.configs(limit)

    def config(self, config_id: str) -> dict[str, Any]:
        found = self._store.config(config_id)
        if found is None:
            raise TrainingValidationError(f"unknown training configuration {config_id!r}")
        return found

    # -- launch and lifecycle -------------------------------------------------
    def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or "")
        config = self._config_from_payload(payload)
        return self._launch(config, request_id=request_id)

    def _config_from_payload(self, payload: Mapping[str, Any]) -> TrainingConfig:
        values = dict(payload)
        values.pop("request_id", None)
        config_id = str(values.get("config_id") or "").strip()
        if config_id:
            saved = self._store.config(config_id)
            if saved is None:
                raise TrainingValidationError(f"unknown training configuration {config_id!r}")
            raw = {
                key: value
                for key, value in saved.items()
                if key
                not in {
                    "config_id",
                    "config_hash",
                    "schema_version",
                    "simulated",
                    "experimental",
                    "advisory_only",
                    "production_authority",
                }
            }
            return TrainingConfig.from_mapping(raw)
        if "config" in values and isinstance(values["config"], Mapping):
            return TrainingConfig.from_mapping(values["config"])
        return TrainingConfig.from_mapping(values)

    def _launch(
        self,
        config: TrainingConfig,
        *,
        request_id: str = "",
        resume_state: Mapping[str, Any] | None = None,
        bundle: TrainingDataBundle | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise TrainingStateError("training manager is closed")
        counts = self._store.counts()
        if counts["runs"] >= MAX_RUNS:
            raise TrainingStoreError("training run record limit reached")
        self._store.save_config(config)
        prepared = bundle or self._preparer.prepare(config)
        if not self._slots.acquire(blocking=False):
            raise TrainingResourceError(
                f"at most {MAX_CONCURRENT_TRAINING} training run may execute at once"
            )
        run: TrainingRun | None = None
        try:
            now = utcnow().isoformat()
            signature = config_hash(
                {
                    "config": config.payload(),
                    "data_hash": prepared.data_hash,
                    "seed": config.seed,
                    "algorithm_version": TRAINING_ALGORITHM_VERSION,
                    "contract_version": TRAINING_CONTRACT_VERSION,
                }
            )
            run = TrainingRun(
                config_id=config.config_id,
                config_hash=config.config_hash,
                name=config.name,
                algorithm=config.algorithm,
                status=TrainingRunStatus.CREATED.value,
                total_episodes=config.max_episodes,
                total_steps=config.max_steps,
                seed=config.seed,
                training_signature=signature,
                created_at=now,
                updated_at=now,
            )
            policy = self._new_policy(config, prepared, run_id=run.run_id)
            self._store.save_policy(policy)
            run = replace(run, policy_id=policy.policy_id)
            self._save_run(run)
            runtime = _Runtime(
                run_id=run.run_id,
                config=config,
                bundle=prepared,
                resume_state=resume_state,
                started_monotonic=time.monotonic(),
                slot_acquired=True,
            )
            if resume_state:
                progress = resume_state.get("progress") or {}
                rng_state = resume_state.get("episode_rng_state")
                if isinstance(rng_state, (list, tuple)):
                    with suppress(AttributeError):
                        runtime.__dict__["_episode_rng_state"] = tuple(rng_state)
                run = replace(
                    run,
                    current_step=max(0, int(progress.get("step") or 0)),
                    current_episode=max(0, int(progress.get("episode") or 0)),
                )
                self._save_run(run)
            thread = threading.Thread(
                target=self._worker,
                args=(runtime,),
                name=f"pi15-training-{run.run_id[:10]}",
                daemon=True,
            )
            runtime.thread = thread
            with self._lock:
                self._runtimes[run.run_id] = runtime
            thread.start()
        except Exception as exc:
            if run is not None:
                self._save_safely(run.with_status(TrainingRunStatus.FAILED, error=str(exc)))
            self._slots.release()
            raise
        return {
            "ok": True,
            "accepted": True,
            "request_id": request_id,
            "run": self._run_payload(run),
            "policy_id": policy.policy_id,
            "isolation": self._isolation(),
            "note": "training runs in an isolated daemon worker; advisory artifacts only",
        }

    @staticmethod
    def _new_policy(
        config: TrainingConfig, bundle: TrainingDataBundle, *, run_id: str = ""
    ) -> Any:
        """The artifact born with a run; it must name that run.

        ``run_id`` is not decoration: the artifact detail and the run's policy
        link are read back from it, and a policy without it cannot be traced to
        the run and data that produced it.
        """
        from .models import PolicyArtifact, PolicyStatus

        return PolicyArtifact(
            name=f"{config.name} (experimental)",
            run_id=run_id,
            algorithm=config.algorithm,
            status=PolicyStatus.TRAINING.value,
            config_id=config.config_id,
            config_hash=config.config_hash,
            seed=config.seed,
            dataset_provenance=dict(bundle.provenance),
            environment_version=str(bundle.provenance.get("environment_version") or ""),
            reward_version=str(bundle.provenance.get("reward_version") or ""),
            creation_metadata={
                "algorithm_version": TRAINING_ALGORITHM_VERSION,
                "contract_version": TRAINING_CONTRACT_VERSION,
                "experimental": True,
                "activation_available": False,
            },
        )

    # -- run queries and controls --------------------------------------------
    def _get_run(self, run_id: str) -> TrainingRun:
        with self._lock:
            found = self._runs.get(run_id)
        if found is not None:
            return found
        raw = self._store.run(run_id)
        if raw is None:
            raise TrainingValidationError(f"unknown training run {run_id!r}")
        return TrainingRun.from_dict(raw)

    def _save_run(self, run: TrainingRun) -> TrainingRun:
        with self._lock:
            self._runs[run.run_id] = run
        self._store.save_run(run)
        return run

    def _save_safely(self, run: TrainingRun) -> None:
        with suppress(Exception):
            self._save_run(run)

    def _run_payload(self, run: TrainingRun) -> dict[str, Any]:
        payload = run.to_dict()
        with self._lock:
            runtime = self._runtimes.get(run.run_id)
            if runtime is not None:
                payload["pause_requested"] = runtime.pause_event.is_set()
                payload["cancel_requested"] = runtime.cancel_event.is_set()
                payload["checkpoint_requested"] = runtime.checkpoint_requested.is_set()
                payload["worker_alive"] = bool(runtime.thread and runtime.thread.is_alive())
        return payload

    def runs(self, limit: int = 50, *, status: str = "") -> dict[str, Any]:
        result = self._store.runs(limit, status=status)
        return {
            **result,
            "runs": [self._run_payload(TrainingRun.from_dict(row)) for row in result["runs"]],
        }

    def run(self, run_id: str) -> dict[str, Any]:
        return self._run_payload(self._get_run(run_id))

    def metrics(self, run_id: str) -> dict[str, Any]:
        run = self._get_run(run_id)
        return {
            "run_id": run.run_id,
            "status": run.status,
            "current_episode": run.current_episode,
            "total_episodes": run.total_episodes,
            "current_step": run.current_step,
            "total_steps": run.total_steps,
            "metrics": dict(run.metrics),
            "resource_usage": dict(run.resource_usage),
            "note": "training reward is an optimisation signal, not realised financial performance",
            "simulated": True,
            "experimental": True,
        }

    def pause(self, run_id: str) -> dict[str, Any]:
        runtime = self._active_runtime(run_id)
        runtime.pause_event.set()
        return {
            "ok": True,
            "run_id": run_id,
            "pause_requested": True,
            "note": "the worker writes a paused checkpoint at the next safe boundary",
        }

    def resume(self, run_id: str) -> dict[str, Any]:
        runtime = self._active_runtime(run_id)
        if runtime.cancel_event.is_set():
            raise TrainingStateError("a cancelled run cannot be resumed")
        runtime.pause_event.clear()
        run = self._get_run(run_id)
        self._save_run(run.with_status(TrainingRunStatus.RUNNING, stop_reason=""))
        return {"ok": True, "run_id": run_id, "resumed": True}

    def cancel(
        self, run_id: str, *, reason: str = "cancelled by request"
    ) -> dict[str, Any]:
        runtime = self._active_runtime(run_id)
        runtime.cancel_reason = str(reason or "cancelled by request")
        runtime.cancel_event.set()
        runtime.pause_event.clear()
        return {
            "ok": True,
            "run_id": run_id,
            "cancel_requested": True,
            "reason": runtime.cancel_reason,
            "note": "cancellation is checked at the next simulation-step boundary",
        }

    def retry(self, run_id: str) -> dict[str, Any]:
        run = self._get_run(run_id)
        if not run.terminal:
            raise TrainingStateError("a run must be terminal before it can be retried")
        config = TrainingConfig.from_mapping(self.config(run.config_id))
        return self._launch(config, request_id=f"retry:{run_id}")

    def _active_runtime(self, run_id: str) -> _Runtime:
        with self._lock:
            runtime = self._runtimes.get(run_id)
        if runtime is None or runtime.thread is None or not runtime.thread.is_alive():
            raise TrainingStateError(f"training run {run_id!r} is not active")
        return runtime

    # -- manual checkpoint ----------------------------------------------------
    def request_checkpoint(self, run_id: str) -> dict[str, Any]:
        # The worker is the only writer: a parked run consumes this in _control
        # and a running one at its next step boundary.  Writing from here as
        # well saved the same request twice while the worker was parked.
        runtime = self._active_runtime(run_id)
        runtime.checkpoint_requested.set()
        return {
            "ok": True,
            "requested": True,
            "note": "checkpoint is written at the next simulation-step boundary",
        }

    def checkpoints(self, run_id: str = "", limit: int = 100) -> dict[str, Any]:
        if run_id:
            self._get_run(run_id)
        return self._store.checkpoints(run_id, limit)

    def checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        found = self._store.checkpoint(checkpoint_id)
        if found is None:
            raise TrainingValidationError(f"unknown checkpoint {checkpoint_id!r}")
        return found

    def resume_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        record = self.checkpoint(checkpoint_id)
        state = self._load_checkpoint_file(str(record.get("path") or ""), record)
        saved_config = state.get("config")
        if not isinstance(saved_config, Mapping):
            raise TrainingValidationError("checkpoint does not contain a training configuration")
        config = TrainingConfig.from_mapping(saved_config)
        run_id = str(state.get("run_id") or record.get("run_id") or "")
        previous = self._get_run(run_id) if run_id else None
        if previous is not None and previous.config_hash != config.config_hash:
            raise TrainingValidationError("checkpoint configuration does not match its run")
        return self._launch(config, request_id=f"resume:{checkpoint_id}", resume_state=state)

    # -- checkpoints ----------------------------------------------------------
    def _save_checkpoint(
        self,
        runtime: _Runtime,
        run: TrainingRun,
        agent: DQNAgent,
        kind: str,
    ) -> dict[str, Any]:
        checkpoint_id = "rl_ckpt_" + uuid4().hex
        body: dict[str, Any] = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id,
            "run_id": run.run_id,
            "config_id": runtime.config.config_id,
            "config_hash": runtime.config.config_hash,
            "kind": kind,
            "algorithm": runtime.config.algorithm,
            "algorithm_version": TRAINING_ALGORITHM_VERSION,
            "created_at": self._clock().isoformat(),
            "progress": {
                "step": run.current_step,
                "episode": run.current_episode,
                "metrics": dict(run.metrics),
            },
            "versions": {
                "environment": str(
                    (runtime.bundle.provenance if runtime.bundle else {}).get(
                        "environment_version"
                    )
                    or ""
                ),
                "reward": str(
                    (runtime.bundle.provenance if runtime.bundle else {}).get(
                        "reward_version"
                    )
                    or ""
                ),
                "dataset": str(
                    (runtime.bundle.provenance if runtime.bundle else {}).get(
                        "dataset_version"
                    )
                    or ""
                ),
                "feature_set": str(
                    (runtime.bundle.provenance if runtime.bundle else {}).get(
                        "feature_set_version"
                    )
                    or ""
                ),
                "data_hash": runtime.bundle.data_hash if runtime.bundle else "",
                "framework": TRAINING_FRAMEWORK,
                "framework_version": platform.python_version(),
            },
            "config": runtime.config.payload(),
            "agent": agent.save_state(),
            "episode_rng_state": list(runtime.__dict__.get("_episode_rng_state", ())),
        }
        integrity = canonical_hash(body)
        body["integrity_hash"] = integrity
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        path = self._checkpoint_dir / f"{run.run_id}_{kind}_{checkpoint_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
        from .models import CheckpointRecord

        record = CheckpointRecord(
            checkpoint_id=checkpoint_id,
            run_id=run.run_id,
            path=str(path),
            kind=kind,
            step=run.current_step,
            episode=run.current_episode,
            integrity_hash=integrity,
            size_bytes=len(encoded),
            status="complete",
            created_at=str(body["created_at"]),
        )
        payload = self._store.save_checkpoint(record)
        self._last_checkpoint = payload
        updated = replace(
            run,
            checkpoint_ids=(*run.checkpoint_ids, checkpoint_id),
            updated_at=utcnow().isoformat(),
        )
        self._save_run(updated)
        self._prune_checkpoints(run.run_id)
        return payload

    def _prune_checkpoints(self, run_id: str) -> None:
        rows = self._store.checkpoints(run_id, 200)["checkpoints"]
        if len(rows) <= CHECKPOINT_RETENTION:
            return
        protected: set[str] = set()
        for kind in ("best_validation", "final", "paused", "cancelled", "failed"):
            match = next((row for row in rows if row.get("kind") == kind), None)
            if match:
                protected.add(str(match["checkpoint_id"]))
        if rows:
            protected.add(str(rows[0]["checkpoint_id"]))
        removable = [
            row for row in reversed(rows) if str(row["checkpoint_id"]) not in protected
        ]
        for row in removable[: max(0, len(rows) - CHECKPOINT_RETENTION)]:
            path = Path(str(row.get("path") or ""))
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            self._store.delete_checkpoint(str(row["checkpoint_id"]))

    def _load_checkpoint_file(
        self, path_text: str, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not path_text:
            raise TrainingValidationError("checkpoint has no artifact path")
        path = Path(path_text)
        if not path.is_file():
            raise TrainingValidationError(f"checkpoint artifact is missing: {path.name}")
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TrainingValidationError(f"checkpoint artifact is unreadable: {exc}") from exc
        if not isinstance(decoded, dict):
            raise TrainingValidationError("checkpoint artifact must contain an object")
        integrity = str(decoded.pop("integrity_hash", ""))
        computed = canonical_hash(decoded)
        recorded = str(record.get("integrity_hash") or "")
        if not integrity or integrity != computed or (recorded and recorded != integrity):
            raise TrainingValidationError("checkpoint integrity validation failed")
        if str(decoded.get("schema_version") or "") != CHECKPOINT_SCHEMA_VERSION:
            raise TrainingValidationError("unsupported checkpoint schema version")
        agent = decoded.get("agent")
        if not isinstance(agent, Mapping):
            raise TrainingValidationError("checkpoint does not contain agent weights")
        return decoded

    # -- training worker ------------------------------------------------------
    def _worker(self, runtime: _Runtime) -> None:
        run = self._get_run(runtime.run_id)
        run = self._save_run(
            run.with_status(
                TrainingRunStatus.RUNNING,
                started_at=run.started_at or utcnow().isoformat(),
                stop_reason="",
            )
        )
        agent: DQNAgent | None = None
        try:
            bundle = runtime.bundle
            if bundle is None:
                raise TrainingStateError("training data bundle is missing")
            episode_rng = random.Random(runtime.config.seed + 7919)
            saved_rng = runtime.__dict__.get("_episode_rng_state")
            if isinstance(saved_rng, (list, tuple)) and saved_rng:
                with suppress(TypeError, ValueError):
                    episode_rng.setstate(tuple(saved_rng))
            runtime.__dict__["_episode_rng_state"] = episode_rng.getstate()
            train_dataset = bundle.datasets["train"]
            env, observation = self._new_episode(
                train_dataset, runtime.config, episode_rng, 0, runtime.run_id
            )
            encoder = (
                ObservationEncoder.from_dict(runtime.resume_state["agent"]["encoder"])
                if runtime.resume_state
                and isinstance(runtime.resume_state.get("agent"), Mapping)
                and isinstance(runtime.resume_state["agent"].get("encoder"), Mapping)
                else ObservationEncoder.fit(observation)
            )
            if runtime.resume_state and isinstance(runtime.resume_state.get("agent"), Mapping):
                agent = DQNAgent.from_state(runtime.resume_state["agent"])
                if agent.input_dim != encoder.dimension:
                    raise TrainingValidationError(
                        "checkpoint observation encoder is incompatible with the training data"
                    )
            else:
                agent = DQNAgent(
                    input_dim=encoder.dimension,
                    hidden_layers=runtime.config.hidden_layers,
                    activation=runtime.config.activation,
                    optimizer=runtime.config.optimizer,
                    learning_rate=runtime.config.learning_rate,
                    gamma=runtime.config.gamma,
                    epsilon_start=runtime.config.epsilon_start,
                    epsilon_end=runtime.config.epsilon_end,
                    epsilon_decay=runtime.config.epsilon_decay,
                    target_update_interval=runtime.config.target_update_interval,
                    replay_capacity=runtime.config.replay_capacity,
                    seed=runtime.config.seed,
                    encoder=encoder,
                )
            runtime.agent = agent
            progress = runtime.resume_state.get("progress") if runtime.resume_state else {}
            start_episode = max(0, int((progress or {}).get("episode") or 0))
            total_steps = max(0, int((progress or {}).get("step") or 0))
            prior_metrics = dict((progress or {}).get("metrics") or {})
            episode_rewards = list(prior_metrics.get("reward_history") or [])
            episode_lengths = list(prior_metrics.get("episode_length_history") or [])
            losses = list(prior_metrics.get("loss_history") or [])
            action_counts = Counter(prior_metrics.get("action_distribution") or {})
            invalid_actions = int(prior_metrics.get("invalid_actions") or 0)
            rolling_rewards: deque[float] = deque(episode_rewards[-20:], maxlen=20)
            best_score = float(prior_metrics.get("best_validation_score") or -math.inf)
            best_checkpoint_id = str(prior_metrics.get("best_checkpoint_id") or "")
            stopped_reason = "completed"
            for episode in range(start_episode, runtime.config.max_episodes):
                self._control(runtime, run, agent)
                env, observation = self._new_episode(
                    train_dataset,
                    runtime.config,
                    episode_rng,
                    episode,
                    runtime.run_id,
                )
                runtime.__dict__["_episode_rng_state"] = episode_rng.getstate()
                state = encoder.encode(observation)
                episode_reward = 0.0
                episode_steps = 0
                history: list[Mapping[str, Any]] = []
                while env.status.value == "running" and total_steps < runtime.config.max_steps:
                    self._control(runtime, run, agent)
                    self._check_resources(runtime)
                    action_index = agent.act(state)
                    action_counts[ACTION_NAMES[action_index]] += 1
                    step = self._step(
                        env,
                        observation,
                        action_index,
                        history,
                        bundle,
                    )
                    next_state = encoder.encode(step.next_observation)
                    agent.remember(
                        state,
                        action_index,
                        step.reward,
                        next_state,
                        step.done,
                    )
                    loss = agent.train_step(runtime.config.batch_size)
                    if loss is not None:
                        if not math.isfinite(loss) or loss > 1e12:
                            losses.append(float("inf"))
                            raise _NumericalInstability(
                                "non-finite or exploding training loss detected"
                            )
                        losses.append(loss)
                    history.append(step.transition)
                    history = history[-50:]
                    state = next_state
                    observation = step.next_observation
                    total_steps += 1
                    episode_steps += 1
                    episode_reward += step.reward
                    invalid_actions += int(step.invalid_action)
                    run = replace(
                        run,
                        current_step=total_steps,
                        current_episode=episode + 1,
                        updated_at=utcnow().isoformat(),
                    )
                    if total_steps % runtime.config.checkpoint_interval == 0:
                        run = self._update_training_metrics(
                            run,
                            episode,
                            episode_reward,
                            episode_steps,
                            episode_rewards,
                            episode_lengths,
                            rolling_rewards,
                            losses,
                            action_counts,
                            invalid_actions,
                            agent,
                            best_score,
                            best_checkpoint_id,
                        )
                        self._save_run(run)
                        self._save_checkpoint(runtime, run, agent, "periodic")
                        run = self._monitor_stability(runtime, run, agent)
                    if step.done:
                        break
                episode_rewards.append(episode_reward)
                episode_lengths.append(episode_steps)
                rolling_rewards.append(episode_reward)
                run = self._update_training_metrics(
                    run,
                    episode,
                    episode_reward,
                    episode_steps,
                    episode_rewards,
                    episode_lengths,
                    rolling_rewards,
                    losses,
                    action_counts,
                    invalid_actions,
                    agent,
                    best_score,
                    best_checkpoint_id,
                )
                self._save_run(run)
                run = self._monitor_stability(runtime, run, agent)
                if episode_steps == 0:
                    raise TrainingStateError("an episode produced no transitions")
                validation = None
                if (
                    (episode + 1) % runtime.config.validation_interval == 0
                    or total_steps >= runtime.config.max_steps
                ):
                    validation = self._validate_policy(
                        runtime, run, agent, policy=None, persist=False
                    )
                    score = float(validation.get("score") or 0.0)
                    if score > best_score:
                        best_score = score
                        checkpoint = self._save_checkpoint(
                            runtime, run, agent, "best_validation"
                        )
                        best_checkpoint_id = str(checkpoint["checkpoint_id"])
                    run = self._update_training_metrics(
                        run,
                        episode,
                        episode_reward,
                        episode_steps,
                        episode_rewards,
                        episode_lengths,
                        rolling_rewards,
                        losses,
                        action_counts,
                        invalid_actions,
                        agent,
                        best_score,
                        best_checkpoint_id,
                        validation=validation,
                    )
                    self._save_run(run)
                if total_steps >= runtime.config.max_steps:
                    stopped_reason = "max_steps"
                    break
            if total_steps == 0:
                raise TrainingStateError("training produced no transitions")
            self._save_checkpoint(runtime, run, agent, "final")
            run = self._finish_success(runtime, run, agent, stopped_reason)
            runtime.pause_event.clear()
        except _Cancelled as exc:
            if agent is not None:
                self._save_checkpoint(runtime, self._get_run(runtime.run_id), agent, "cancelled")
            self._mark_terminal(
                runtime,
                TrainingRunStatus.CANCELLED,
                str(exc) or "cancelled by request",
            )
        except _NumericalInstability as exc:
            if agent is not None:
                self._save_checkpoint(runtime, self._get_run(runtime.run_id), agent, "failed")
            self._mark_terminal(runtime, TrainingRunStatus.FAILED, str(exc))
            self._reject_policy(runtime, str(exc))
        except _UnstableTraining as exc:
            if agent is not None:
                self._save_checkpoint(runtime, self._get_run(runtime.run_id), agent, "failed")
            self._mark_terminal(runtime, TrainingRunStatus.FAILED, str(exc))
            self._reject_policy(runtime, str(exc))
        except TrainingResourceError as exc:
            if agent is not None:
                self._save_checkpoint(runtime, self._get_run(runtime.run_id), agent, "failed")
            self._mark_terminal(runtime, TrainingRunStatus.FAILED, str(exc))
            self._reject_policy(runtime, str(exc))
        except Exception as exc:  # noqa: BLE001 - persisted as a failed run
            self._last_error = f"{type(exc).__name__}: {exc}"
            if agent is not None:
                with suppress(Exception):
                    self._save_checkpoint(
                        runtime, self._get_run(runtime.run_id), agent, "failed"
                    )
            self._mark_terminal(
                runtime,
                TrainingRunStatus.FAILED,
                f"{type(exc).__name__}: {exc}",
            )
            self._reject_policy(runtime, f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                runtime.thread = None
            if runtime.slot_acquired:
                runtime.slot_acquired = False
                self._slots.release()

    def _control(self, runtime: _Runtime, run: TrainingRun, agent: DQNAgent) -> None:
        while True:
            if runtime.cancel_event.is_set():
                raise _Cancelled(runtime.cancel_reason or "cancelled by request")
            if runtime.checkpoint_requested.is_set():
                runtime.checkpoint_requested.clear()
                self._save_checkpoint(runtime, self._get_run(run.run_id), agent, "manual")
            if not runtime.pause_event.is_set():
                return
            if self._get_run(run.run_id).status != TrainingRunStatus.PAUSED.value:
                self._save_checkpoint(runtime, self._get_run(run.run_id), agent, "paused")
                paused = self._get_run(run.run_id).with_status(
                    TrainingRunStatus.PAUSED, stop_reason="paused at a safe step boundary"
                )
                self._save_run(paused)
            time.sleep(0.05)

    def _check_resources(self, runtime: _Runtime) -> None:
        elapsed = time.monotonic() - runtime.started_monotonic
        if elapsed > runtime.config.max_wall_seconds:
            raise TrainingResourceError(
                f"training exceeded max_wall_seconds={runtime.config.max_wall_seconds}"
            )
        resource = self._resource_snapshot()
        rss = resource.get("process_rss_mb")
        if isinstance(rss, (int, float)) and rss > runtime.config.max_memory_mb:
            raise TrainingResourceError(
                f"training exceeded max_memory_mb={runtime.config.max_memory_mb} "
                f"(observed {rss} MiB)"
            )

    def _monitor_stability(
        self, runtime: _Runtime, run: TrainingRun, agent: DQNAgent
    ) -> TrainingRun:
        """Stop a run that is no longer learning safely.

        A ``fail`` decision raises ``_UnstableTraining`` so the worker keeps a
        failure checkpoint and rejects the artifact.  A ``pause`` decision
        writes a checkpoint, parks the run in ``paused`` with the monitor's
        reason and blocks here until an operator resumes or cancels it, so no
        validation or artifact work happens on a paused policy.
        """
        record = stability_record(
            run.metrics or {},
            DEFAULT_STABILITY_LIMITS,
            checked_at=self._clock().isoformat(),
        )
        if record["action"] not in {STABILITY_FAIL, "pause"}:
            return run
        metrics = dict(run.metrics)
        metrics["stability"] = record
        run = self._save_run(
            replace(run, metrics=metrics, updated_at=utcnow().isoformat())
        )
        if record["action"] == STABILITY_FAIL:
            raise _UnstableTraining(
                "; ".join(
                    str(signal.get("reason") or "")
                    for signal in record["signals"]
                    if signal.get("severity") == STABILITY_FAIL
                )
            )
        self._save_checkpoint(runtime, run, agent, "paused")
        paused = self._save_run(
            run.with_status(
                TrainingRunStatus.PAUSED,
                stop_reason="stability pause: " + ", ".join(record["codes"]),
            )
        )
        runtime.pause_event.set()
        self._control(runtime, paused, agent)
        return self._get_run(run.run_id)

    def _new_episode(
        self,
        dataset: Any,
        config: TrainingConfig,
        rng: random.Random,
        episode: int,
        run_id: str,
    ) -> tuple[RLEnvironment, Mapping[str, Any]]:
        bars = list(dataset.bars)
        count = min(len(bars), config.episode_length + 1)
        if len(bars) <= count:
            selected = bars
        else:
            start = rng.randrange(0, len(bars) - count + 1)
            selected = bars[start : start + count]
        if len(selected) < 2:
            raise TrainingValidationError(
                "training split cannot form an episode with two completed bars"
            )
        start_at = selected[0].observed_at
        end_at = selected[-1].observed_at
        effective = replace(dataset.config, start=start_at, end=end_at)
        features = {
            key: value
            for key, value in dataset.features.items()
            if start_at <= key <= end_at
        }
        forecasts = {
            key: value
            for key, value in dataset.forecasts.items()
            if start_at <= key <= end_at
        }
        env = RLEnvironment(
            effective,
            tuple(selected),
            feature_snapshots=features,
            forecast_snapshots=forecasts,
            clock=self._clock,
        )
        observation = env.reset(f"train_{run_id}_{episode}")
        return env, observation.to_dict()

    def _step(
        self,
        env: RLEnvironment,
        observation: Mapping[str, Any],
        action_index: int,
        history: Sequence[Mapping[str, Any]],
        bundle: TrainingDataBundle,
    ) -> _EpisodeStep:
        payload = action_payload(action_index, observation)
        invalid = False
        try:
            next_observation, _reward, terminated, truncated, transition = env.step(payload)
        except RLInvalidActionError:
            invalid = True
            next_observation, _reward, terminated, truncated, transition = env.step(
                {"action_type": "wait"}
            )
        except RLValidationError:
            invalid = True
            next_observation, _reward, terminated, truncated, transition = env.step(
                {"action_type": "wait"}
            )
        transition_payload = transition.to_dict()
        result = self._reward_engine.calculate(
            transition_payload,
            config=bundle.reward_config,
            history=list(history),
            environment=env.config.to_dict(),
        )
        return _EpisodeStep(
            observation=observation,
            next_observation=next_observation.to_dict(),
            reward=float(result.total),
            done=bool(terminated or truncated),
            invalid_action=invalid,
            transition=transition_payload,
        )

    def _update_training_metrics(
        self,
        run: TrainingRun,
        episode: int,
        episode_reward: float,
        episode_steps: int,
        episode_rewards: Sequence[float],
        episode_lengths: Sequence[int],
        rolling_rewards: deque[float],
        losses: Sequence[float],
        action_counts: Counter[str],
        invalid_actions: int,
        agent: DQNAgent,
        best_score: float,
        best_checkpoint_id: str,
        *,
        validation: Mapping[str, Any] | None = None,
    ) -> TrainingRun:
        prior_stability = (run.metrics or {}).get("stability")
        actions_total = sum(action_counts.values())
        finite_losses = [value for value in losses[-100:] if math.isfinite(value)]
        mean_loss = statistics.fmean(finite_losses) if finite_losses else None
        elapsed_seconds = runtime_elapsed(run)
        throughput = (
            round(run.current_step / elapsed_seconds, 6)
            if elapsed_seconds > 0 and run.current_step > 0
            else None
        )
        step_latency_ms = (
            round(elapsed_seconds * 1000.0 / run.current_step, 6)
            if elapsed_seconds > 0 and run.current_step > 0
            else None
        )
        remaining_seconds = (
            round((run.total_steps - run.current_step) / throughput, 3)
            if throughput and run.total_steps > run.current_step
            else None
        )
        dominant_action = (
            max(action_counts, key=lambda name: action_counts[name])
            if action_counts
            else ""
        )
        dominant_frequency = (
            round(action_counts[dominant_action] / actions_total, 8)
            if actions_total and dominant_action
            else 0.0
        )
        metrics: dict[str, Any] = {
            "episode": episode + 1,
            "step": run.current_step,
            "episode_reward": round(episode_reward, 10),
            "mean_reward": round(statistics.fmean(episode_rewards), 10)
            if episode_rewards
            else None,
            "reward_variance": round(statistics.pvariance(episode_rewards), 10)
            if len(episode_rewards) > 1
            else 0.0,
            "rolling_reward": round(statistics.fmean(rolling_rewards), 10)
            if rolling_rewards
            else None,
            "episode_length": episode_steps,
            "mean_episode_length": round(statistics.fmean(episode_lengths), 4)
            if episode_lengths
            else None,
            "action_distribution": dict(sorted(action_counts.items())),
            "action_frequencies": {
                name: round(action_counts.get(name, 0) / actions_total, 8)
                for name in ACTION_NAMES
            }
            if actions_total
            else {},
            "invalid_actions": invalid_actions,
            "invalid_action_frequency": round(invalid_actions / actions_total, 8)
            if actions_total
            else None,
            "exploration_epsilon": round(agent.epsilon_for_step(agent.steps), 8),
            "dominant_action": dominant_action,
            "dominant_action_frequency": dominant_frequency,
            "training_elapsed_seconds": round(elapsed_seconds, 3),
            "training_throughput_steps_per_second": throughput,
            "environment_step_latency_ms": step_latency_ms,
            "estimated_remaining_seconds": remaining_seconds,
            "policy_loss": None,
            "value_loss": round(mean_loss, 10) if mean_loss is not None else None,
            "entropy": None,
            "loss_history": [
                round(value, 10) if math.isfinite(value) else None
                for value in losses[-100:]
            ],
            "reward_history": [round(value, 10) for value in episode_rewards[-100:]],
            "episode_length_history": list(episode_lengths[-100:]),
            "best_validation_score": best_score if math.isfinite(best_score) else None,
            "best_checkpoint_id": best_checkpoint_id,
            "reward_note": "optimisation reward only; it is not financial profit",
            "reproducibility_note": (
                "pure-Python CPU computation is deterministic for a fixed seed and "
                "single-threaded run; OS/process timing is not part of the model"
            ),
        }
        # The stability monitor is the only owner of reward collapse, action
        # repetition and invalid-action spikes.  Its record is therefore the
        # single summary of those conditions in the run metrics: it is written on
        # every update instead of the removed per-condition booleans, whose
        # thresholds had drifted from the monitor's published limits.
        record = stability_record(
            metrics,
            DEFAULT_STABILITY_LIMITS,
            checked_at=self._clock().isoformat(),
        )
        prior_record = (
            dict(prior_stability) if isinstance(prior_stability, Mapping) else None
        )
        prior_action = str((prior_record or {}).get("action") or "")
        if (
            prior_record is not None
            and prior_action not in {"", "ok"}
            and record["action"] == "ok"
        ):
            # The last non-ok decision stays visible after a resume; the monitor
            # overwrites it on its next decision.
            metrics["stability"] = prior_record
        else:
            metrics["stability"] = record
        if validation is not None:
            metrics["latest_validation"] = {
                "comparison_id": validation.get("comparison_id"),
                "score": validation.get("score"),
                "policy": validation.get("trained"),
                "baselines": validation.get("baselines"),
                "warnings": validation.get("warnings"),
            }
        return replace(
            run,
            metrics=metrics,
            resource_usage=self._resource_snapshot(elapsed=runtime_elapsed(run)),
            updated_at=utcnow().isoformat(),
        )

    # -- validation, testing and policy artifacts ----------------------------
    def _validate_policy(
        self,
        runtime: _Runtime,
        run: TrainingRun,
        agent: DQNAgent,
        *,
        policy: Any,
        persist: bool,
    ) -> dict[str, Any]:
        if policy is None:
            policy = self._policy_adapter(agent, runtime)
        return self._evaluate_for_split(
            runtime.config,
            policy,
            "validation",
            run_id=run.run_id,
            policy_id=run.policy_id,
            persist=persist,
        )

    def _policy_adapter(self, agent: DQNAgent, runtime: _Runtime) -> TrainedPolicy:
        version = f"pi15-{TRAINING_ALGORITHM_VERSION}-{runtime.run_id[:12]}"
        return TrainedPolicy(
            agent,
            name=f"{runtime.config.name} experimental DQN",
            version=version,
            description="PI-15 experimental DQN policy; advisory only.",
        )

    def _evaluate_for_split(
        self,
        config: TrainingConfig,
        policy: Any,
        split: str,
        *,
        run_id: str,
        policy_id: str,
        persist: bool,
    ) -> dict[str, Any]:
        max_transitions = (
            config.validation_max_transitions
            if split == "validation"
            else config.test_max_transitions
        )
        payload = {
            "name": f"PI-15 {split} evaluation: {config.name}",
            "environment": dict(config.environment),
            "policies": ["no_action", "rule_based", "pi12_adapter"],
            "reward_config": dict(config.reward_config),
            "split": split,
            "splits": {label: dict(row) for label, row in config.splits.items()},
            "max_transitions": max_transitions,
            "description": (
                "Experimental PI-15 trained-policy comparison. The test split is never "
                "used for training or checkpoint selection."
            ),
        }
        response = self._reward_service.evaluate_experimental_policy(
            policy,
            payload,
            policy_metadata={
                "run_id": run_id,
                "policy_id": policy_id,
                "split": split,
                "experimental": True,
                "advisory_only": True,
                "activation_available": False,
            },
        )
        comparison = dict(response["comparison"])
        results = list(comparison.get("results") or [])
        trained = next(
            (row for row in results if row.get("policy_name") == policy.name),
            None,
        )
        if trained is None:
            raise TrainingValidationError(
                f"{split} evaluation did not return the trained policy result"
            )
        baselines = [
            {
                "policy_name": row.get("policy_name"),
                "policy_version": row.get("policy_version"),
                "status": row.get("status"),
                "metrics": _metric_subset(row.get("metrics") or {}),
                "reward_summary": dict(row.get("reward_summary") or {}),
                "action_distribution": dict(row.get("action_distribution") or {}),
                "transitions": row.get("transitions"),
                "warnings": list(row.get("warnings") or []),
            }
            for row in results
            if row.get("policy_name") != policy.name
        ]
        trained_metrics = _metric_subset(trained.get("metrics") or {})
        score = _selection_score(trained_metrics)
        summary = {
            "split": split,
            "comparison_id": comparison.get("comparison_id"),
            "evaluation_id": trained.get("evaluation_id"),
            "conditions_identical": bool(comparison.get("conditions_identical")),
            "dataset": dict(comparison.get("dataset") or {}),
            "policy": {
                "policy_name": trained.get("policy_name"),
                "policy_version": trained.get("policy_version"),
                "status": trained.get("status"),
                "metrics": trained_metrics,
                "reward_summary": dict(trained.get("reward_summary") or {}),
                "action_distribution": dict(trained.get("action_distribution") or {}),
                "transitions": trained.get("transitions"),
                "warnings": list(trained.get("warnings") or []),
            },
            "baselines": baselines,
            "metrics": trained_metrics,
            "financial": trained_metrics.get("financial", {}),
            "risk": trained_metrics.get("risk", {}),
            "trading": trained_metrics.get("trading", {}),
            "decision": trained_metrics.get("decision", {}),
            "learning": trained_metrics.get("learning", {}),
            "score": score,
            "score_basis": (
                "experimental checkpoint-selection score: 0.5*mean reward + "
                "0.5*total return + 0.5*drawdown; it is not a profitability claim"
            ),
            "regimes": dict(trained_metrics.get("regimes") or {}),
            "regime_note": (
                "per-market-regime slices are labelled from this split's own PI-13 "
                "observation evidence and are descriptive; they never train the "
                "policy, tune a hyperparameter or decide checkpoint selection"
            ),
            "warnings": list(trained.get("warnings") or []),
            "test_data_used": split == "test",
        }
        if persist:
            self._store.save_evaluation(
                evaluation_id=str(summary.get("evaluation_id") or comparison.get("comparison_id")),
                run_id=run_id,
                policy_id=policy_id,
                split=split,
                payload=summary,
            )
        return summary

    def _finish_success(
        self,
        runtime: _Runtime,
        run: TrainingRun,
        agent: DQNAgent,
        stopped_reason: str,
    ) -> TrainingRun:
        config = runtime.config
        policy = self._get_policy(run.policy_id)
        training_metrics = dict(run.metrics)
        policy = self._persist_policy_artifact(runtime, run, agent, policy, training_metrics)
        policy = policy.with_status(
            "validation_pending",
            training_metrics=training_metrics,
        )
        policy = self._save_policy(policy)
        run = self._save_run(
            run.with_status(
                TrainingRunStatus.VALIDATING_POLICY,
                policy_id=policy.policy_id,
                stop_reason="final validation running",
            )
        )
        validation: dict[str, Any] | None = None
        try:
            validation = self._evaluate_for_split(
                config,
                self._policy_adapter(agent, runtime),
                "validation",
                run_id=run.run_id,
                policy_id=policy.policy_id,
                persist=True,
            )
            policy = policy.with_status(
                "validated",
                validation_metrics=validation,
            )
            policy = self._save_policy(policy)
        except Exception as exc:  # noqa: BLE001 - validation gap is evidence, not a crash
            validation = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "note": "validation did not complete; the artifact remains experimental",
            }
            policy = self._save_policy(
                policy.with_status(
                    "trained",
                    validation_metrics={"status": "failed", "error": str(exc)},
                )
            )
            self._last_error = f"validation: {type(exc).__name__}: {exc}"
        test: dict[str, Any] | None = None
        if policy.status == "validated":
            try:
                test = self._evaluate_for_split(
                    config,
                    self._policy_adapter(agent, runtime),
                    "test",
                    run_id=run.run_id,
                    policy_id=policy.policy_id,
                    persist=True,
                )
                policy = self._save_policy(
                    policy.with_status("tested", test_metrics=test)
                )
            except Exception as exc:  # noqa: BLE001 - report the out-of-sample gap
                test = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "note": "out-of-sample testing did not complete",
                }
                self._last_error = f"test: {type(exc).__name__}: {exc}"
        validation_failed = isinstance(validation, Mapping) and validation.get(
            "status"
        ) == "failed"
        test_failed = isinstance(test, Mapping) and test.get("status") == "failed"
        final_status = (
            TrainingRunStatus.FAILED
            if validation_failed or test_failed
            else TrainingRunStatus.COMPLETED
        )
        final_error = ""
        final_stop_reason = stopped_reason
        if validation_failed:
            final_error = (
                "validation did not complete: "
                f"{(validation or {}).get('error', '')}"
            )
            final_stop_reason = "validation_failed"
        elif test_failed:
            final_error = (
                "out-of-sample test did not complete: "
                f"{(test or {}).get('error', '')}"
            )
            final_stop_reason = "test_failed"
        metrics = dict(run.metrics)
        metrics.update(
            {
                "validation": validation,
                "test": test,
                "policy_status": policy.status,
                "stop_reason": final_stop_reason,
                "completed_episodes": run.current_episode,
                "completed_steps": run.current_step,
                "financial_performance_note": (
                    "Training success and historical validation are not proof of "
                    "profitability or future performance."
                ),
            }
        )
        run = self._save_run(
            run.with_status(
                final_status,
                metrics=metrics,
                ended_at=utcnow().isoformat(),
                stop_reason=final_stop_reason,
                policy_id=policy.policy_id,
                error=final_error,
            )
        )
        return run

    def _persist_policy_artifact(
        self,
        runtime: _Runtime,
        run: TrainingRun,
        agent: DQNAgent,
        policy: Any,
        training_metrics: Mapping[str, Any],
    ) -> Any:
        artifact = {
            "schema_version": POLICY_SCHEMA_VERSION,
            "policy_id": policy.policy_id,
            "run_id": run.run_id,
            "name": policy.name,
            "algorithm": runtime.config.algorithm,
            "algorithm_version": TRAINING_ALGORITHM_VERSION,
            "created_at": self._clock().isoformat(),
            "config": runtime.config.payload(),
            "dataset_provenance": dict(runtime.bundle.provenance if runtime.bundle else {}),
            "training_metrics": dict(training_metrics),
            "agent": agent.save_state(),
            "experimental": True,
            "advisory_only": True,
            "production_authority": False,
            "activation_available": False,
        }
        encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
        artifact_hash = hashlib.sha256(encoded).hexdigest()
        path = self._policy_dir / f"{policy.policy_id}_{artifact_hash[:12]}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
        return self._save_policy(
            policy.with_status(
                "trained",
                artifact_path=str(path),
                artifact_hash=artifact_hash,
                training_metrics=dict(training_metrics),
                dataset_provenance=dict(runtime.bundle.provenance if runtime.bundle else {}),
                environment_version=str(
                    (runtime.bundle.provenance if runtime.bundle else {}).get(
                        "environment_version", ""
                    )
                ),
                reward_version=str(
                    (runtime.bundle.provenance if runtime.bundle else {}).get(
                        "reward_version", ""
                    )
                ),
                creation_metadata={
                    **dict(policy.creation_metadata),
                    "training_signature": run.training_signature,
                    "completed_steps": run.current_step,
                    "framework": TRAINING_FRAMEWORK,
                    "framework_version": platform.python_version(),
                    "hardware": self._resource_snapshot(),
                },
            )
        )

    def _mark_terminal(
        self,
        runtime: _Runtime,
        status: TrainingRunStatus,
        reason: str,
    ) -> None:
        try:
            run = self._get_run(runtime.run_id)
            run = self._save_run(
                run.with_status(
                    status,
                    error=reason if status == TrainingRunStatus.FAILED else "",
                    stop_reason=reason,
                    ended_at=utcnow().isoformat(),
                )
            )
        except Exception:
            return

    def _reject_policy(self, runtime: _Runtime, reason: str) -> None:
        try:
            run = self._get_run(runtime.run_id)
            if not run.policy_id:
                return
            policy = self._get_policy(run.policy_id)
            if policy.status in {"archived", "rejected"}:
                return
            self._save_policy(
                policy.with_status(
                    "rejected",
                    training_metrics={
                        **dict(policy.training_metrics),
                        "failure_reason": reason,
                    },
                )
            )
        except Exception:
            pass

    # -- run finish controls --------------------------------------------------
    def _save_policy(self, policy: Any) -> Any:
        self._store.save_policy(policy)
        return policy

    def _get_policy(self, policy_id: str) -> PolicyArtifact:
        raw = self._store.policy(policy_id)
        if raw is None:
            raise TrainingValidationError(f"unknown policy artifact {policy_id!r}")
        return PolicyArtifact.from_dict(raw)

    def policies(self, limit: int = 50, *, status: str = "") -> dict[str, Any]:
        result = self._store.policies(limit, status=status)
        return {
            **result,
            "experimental": True,
            "advisory_only": True,
            "activation_available": False,
        }

    def policy(self, policy_id: str) -> dict[str, Any]:
        return self._get_policy(policy_id).to_dict()

    def archive_policy(self, policy_id: str) -> dict[str, Any]:
        policy = self._get_policy(policy_id)
        if policy.status == "archived":
            return {"ok": True, "policy": policy.to_dict(), "already_archived": True}
        if not allowed_policy_transition(policy.status, "archived"):
            raise TrainingStateError(
                f"policy status {policy.status!r} cannot transition to archived"
            )
        updated = self._save_policy(policy.with_status("archived"))
        return {"ok": True, "policy": updated.to_dict()}

    def _load_trained_policy(self, policy_id: str) -> tuple[Any, TrainedPolicy, TrainingConfig]:
        policy = self._get_policy(policy_id)
        if policy.status in {"archived", "rejected"}:
            raise TrainingStateError(f"policy {policy_id!r} is {policy.status}")
        if not policy.artifact_path or not policy.artifact_hash:
            raise TrainingValidationError("policy artifact file is not available")
        path = Path(policy.artifact_path)
        if not path.is_file():
            raise TrainingValidationError("policy artifact file is missing")
        encoded = path.read_bytes()
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != policy.artifact_hash:
            raise TrainingValidationError("policy artifact integrity validation failed")
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TrainingValidationError("policy artifact is unreadable") from exc
        if not isinstance(payload, Mapping):
            raise TrainingValidationError("policy artifact must contain an object")
        if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
            raise TrainingValidationError("unsupported policy artifact schema")
        config_payload = payload.get("config")
        agent_payload = payload.get("agent")
        if not isinstance(config_payload, Mapping) or not isinstance(agent_payload, Mapping):
            raise TrainingValidationError("policy artifact is missing its config or weights")
        config = TrainingConfig.from_mapping(config_payload)
        if config.config_hash != policy.config_hash:
            raise TrainingValidationError("policy artifact configuration hash mismatch")
        agent = DQNAgent.from_state(agent_payload)
        adapter = TrainedPolicy(
            agent,
            name=policy.name,
            version=f"pi15-{policy.artifact_hash[:16]}",
            description="PI-15 experimental DQN policy; advisory only.",
        )
        return policy, adapter, config

    def validate_policy(self, policy_id: str) -> dict[str, Any]:
        policy, adapter, config = self._load_trained_policy(policy_id)
        if policy.status in {"validated", "tested"}:
            return {
                "ok": True,
                "already_validated": True,
                "policy": policy.to_dict(),
                "validation": dict(policy.validation_metrics),
            }
        if policy.status != "trained":
            raise TrainingStateError(
                f"policy status {policy.status!r} must be trained before validation"
            )
        pending = self._save_policy(policy.with_status("validation_pending"))
        try:
            validation = self._evaluate_for_split(
                config,
                adapter,
                "validation",
                run_id=pending.run_id,
                policy_id=pending.policy_id,
                persist=True,
            )
            updated = self._save_policy(
                pending.with_status("validated", validation_metrics=validation)
            )
        except Exception:
            self._save_policy(pending.with_status("trained"))
            raise
        return {"ok": True, "policy": updated.to_dict(), "validation": validation}

    def test_policy(self, policy_id: str) -> dict[str, Any]:
        policy, adapter, config = self._load_trained_policy(policy_id)
        if policy.status == "tested":
            return {
                "ok": True,
                "already_tested": True,
                "policy": policy.to_dict(),
                "test": dict(policy.test_metrics),
            }
        if policy.status != "validated":
            raise TrainingStateError(
                "out-of-sample testing requires a validated policy; training data is never reused"
            )
        test = self._evaluate_for_split(
            config,
            adapter,
            "test",
            run_id=policy.run_id,
            policy_id=policy.policy_id,
            persist=True,
        )
        updated = self._save_policy(policy.with_status("tested", test_metrics=test))
        return {"ok": True, "policy": updated.to_dict(), "test": test}

    def advisory_proposal(
        self, policy_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return one PI-12-shaped advisory action; never an order.

        The proposal is deliberately not persisted as a production proposal and
        has no execution path. NM-08 must assess it before any other authority
        could act, and NM-10 remains the only paper-execution engine.
        """
        policy, adapter, config = self._load_trained_policy(policy_id)
        if policy.status not in {"trained", "validated", "tested"}:
            raise TrainingStateError(
                f"policy status {policy.status!r} cannot issue an advisory proposal"
            )
        observation = payload.get("observation")
        if not isinstance(observation, Mapping):
            raise TrainingValidationError("advisory proposal requires an observation object")
        portfolio = payload.get("portfolio")
        if portfolio is not None and not isinstance(portfolio, Mapping):
            raise TrainingValidationError("portfolio must be an object")
        environment = payload.get("environment")
        if environment is not None and not isinstance(environment, Mapping):
            raise TrainingValidationError("environment must be an object")
        history = payload.get("history") or []
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
            raise TrainingValidationError("history must be a list")
        decision = adapter.select(
            observation=observation,
            portfolio=portfolio or {},
            environment=environment or dict(config.environment),
            history=history,
        )
        action = dict(decision.action)
        return {
            "advisory": True,
            "status": "advisory_pending_nm08",
            "policy_id": policy.policy_id,
            "policy_version": adapter.version,
            "algorithm": policy.algorithm,
            "action": action,
            "rationale": decision.rationale,
            "risk_review_required": True,
            "nm08_authority": True,
            "nm10_authority": True,
            "authorizes_action": False,
            "execution_allowed": False,
            "orders_possible": False,
            "production_authority": False,
            "activation_available": False,
            "warnings": [
                "Advisory research output only; NM-08 must assess any proposal.",
                "This method cannot submit an NM-10 order or activate autonomy.",
            ],
        }

    def evaluate_regime_window(
        self, policy_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Evaluate one trained policy on an explicit historical regime window.

        PI-16 supplies the window after deciding that reassessment is justified.
        This method only runs PI-14's deterministic historical simulation; it
        never trains, promotes or activates the policy and never writes a
        production portfolio or order.
        """
        policy, adapter, config = self._load_trained_policy(policy_id)
        if policy.status not in {"trained", "validated", "tested"}:
            raise TrainingStateError(
                f"policy status {policy.status!r} cannot be reassessed"
            )
        start = payload.get("start")
        end = payload.get("end")
        if not start or not end:
            raise TrainingValidationError("a regime reassessment requires start and end")
        environment = dict(config.environment)
        environment["start"] = start
        environment["end"] = end
        max_transitions = int(payload.get("max_transitions", 500))
        if not 1 <= max_transitions <= 5_000:
            raise TrainingValidationError("max_transitions must be within [1, 5000]")
        response = self._reward_service.evaluate_experimental_policy(
            adapter,
            {
                "name": f"PI-16 regime reassessment: {payload.get('regime', 'uncertain')}",
                "environment": environment,
                "policies": ["no_action", "rule_based", "pi12_adapter"],
                "reward_config": dict(config.reward_config),
                "split": "all",
                "max_transitions": max_transitions,
                "description": "Explicit bounded PI-16 regime reassessment on historical data.",
            },
            policy_metadata={
                "run_id": policy.run_id,
                "policy_id": policy.policy_id,
                "regime": str(payload.get("regime", "")),
                "purpose": "adaptive_regime_reassessment",
                "reason": str(payload.get("reason", "")),
                "experimental": True,
                "advisory_only": True,
                "activation_available": False,
            },
        )
        comparison = dict(response["comparison"])
        rows = list(comparison.get("results") or [])
        trained = next(
            (row for row in rows if row.get("policy_name") == policy.name),
            None,
        )
        if trained is None:
            raise TrainingValidationError("reassessment did not return the trained policy")
        return {
            "ok": True,
            "policy_id": policy.policy_id,
            "policy": policy.to_dict(),
            "evaluation": dict(trained),
            "baselines": [
                dict(row) for row in rows if row.get("policy_name") != policy.name
            ],
            "comparison_id": comparison.get("comparison_id"),
            "conditions": dict(comparison.get("conditions") or {}),
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
            "activation_available": False,
        }

    def evaluations(self, policy_id: str = "", limit: int = 50) -> dict[str, Any]:
        return self._store.evaluations(policy_id, limit)

    def compare_policies(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ids = payload.get("policy_ids")
        if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)):
            raise TrainingValidationError("policy_ids must be a non-empty list")
        policy_ids = [str(item).strip() for item in ids if str(item).strip()]
        if len(policy_ids) < 2:
            raise TrainingValidationError("at least two policies are required for comparison")
        if len(policy_ids) > MAX_COMPARED_POLICIES:
            raise TrainingValidationError(
                f"at most {MAX_COMPARED_POLICIES} policies may be compared"
            )
        rows = [self._get_policy(policy_id) for policy_id in policy_ids]
        provenance = [dict(row.dataset_provenance) for row in rows]
        keys = ("data_hash", "environment_version", "reward_version", "reward_config_hash")
        mismatched = [
            key
            for key in keys
            if len({str(item.get(key) or "") for item in provenance}) > 1
        ]
        if mismatched:
            raise TrainingValidationError(
                "policies are not directly comparable; mismatched conditions: "
                + ", ".join(mismatched)
            )
        results = []
        for row, item in zip(rows, provenance, strict=True):
            results.append(
                {
                    "policy_id": row.policy_id,
                    "name": row.name,
                    "algorithm": row.algorithm,
                    "status": row.status,
                    "artifact_version": row.artifact_version,
                    "seed": row.seed,
                    "training_metrics": {
                        key: (row.training_metrics or {}).get(key)
                        for key in (
                            "mean_reward",
                            "rolling_reward",
                            "reward_variance",
                            "completed_episodes",
                            "completed_steps",
                            "invalid_actions",
                            "invalid_action_frequency",
                            "exploration_epsilon",
                            "policy_status",
                        )
                    },
                    "validation_metrics": _metric_subset(row.validation_metrics),
                    "test_metrics": _metric_subset(row.test_metrics),
                    "validation_score": (row.validation_metrics or {}).get("score"),
                    "test_score": (row.test_metrics or {}).get("score"),
                    "dataset_provenance": {
                        key: item.get(key)
                        for key in (
                            "data_hash",
                            "dataset_version",
                            "feature_set_version",
                            "environment_version",
                            "reward_version",
                        )
                    },
                    "experimental": True,
                    "advisory_only": True,
                }
            )
        conditions = {
            "dataset_hash": provenance[0].get("data_hash"),
            "environment_version": provenance[0].get("environment_version"),
            "reward_version": provenance[0].get("reward_version"),
            "reward_config_hash": provenance[0].get("reward_config_hash"),
            "execution_assumptions": "PI-13/PI-14 historical simulation",
        }
        comparison = {
            "comparison_id": "rl_policy_cmp_" + canonical_hash(
                {"policies": policy_ids, "conditions": conditions}
            )[:24],
            "conditions": conditions,
            "conditions_identical": True,
            "results": results,
            "created_at": self._clock().isoformat(),
            "limitations": (
                "Different hyperparameters or algorithms may be compared only because "
                "both policies used the recorded identical data and execution conditions. "
                "A higher historical score does not establish future profitability."
            ),
            "experimental": True,
            "advisory_only": True,
            "production_authority": False,
            "promotion_available": False,
        }
        return {"ok": True, "comparison": comparison}

    # -- bounded hyperparameter search ----------------------------------------
    def optimize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        config = self._config_from_payload(payload)
        method = str(payload.get("method") or "random").strip().lower()
        if method not in {"random", "grid"}:
            raise TrainingValidationError("method must be 'random' or 'grid'")
        raw_space = payload.get("space")
        if raw_space is None:
            space = {key: list(values) for key, values in SUPPORTED_OPTIMIZATION_FIELDS.items()}
        elif isinstance(raw_space, Mapping):
            space = {}
            for key, values in raw_space.items():
                name = str(key)
                if name not in SUPPORTED_OPTIMIZATION_FIELDS:
                    raise TrainingValidationError(
                        f"unsupported hyperparameter {name!r}; supported: "
                        + ", ".join(sorted(SUPPORTED_OPTIMIZATION_FIELDS))
                    )
                if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                    raise TrainingValidationError(f"space.{name} must be a list")
                if not values:
                    raise TrainingValidationError(f"space.{name} cannot be empty")
                space[name] = list(values)
        else:
            raise TrainingValidationError("space must be an object")
        trials_requested = int(payload.get("trials") or min(6, MAX_OPTIMIZATION_TRIALS))
        if not 1 <= trials_requested <= MAX_OPTIMIZATION_TRIALS:
            raise TrainingValidationError(
                f"trials must be within [1, {MAX_OPTIMIZATION_TRIALS}]"
            )
        candidates = _search_candidates(space, method, trials_requested, config.seed)
        prepared = self._preparer.prepare(config)
        search_id = "rl_search_" + canonical_hash(
            {
                "config": config.payload(),
                "space": {key: list(values) for key, values in space.items()},
                "method": method,
                "trials": trials_requested,
                "data_hash": prepared.data_hash,
            }
        )[:24]
        with self._lock:
            existing = self._search_states.get(search_id)
            if existing is not None:
                return {"ok": True, "search": existing, "reused": True}
            state: dict[str, Any] = {
                "search_id": search_id,
                "status": "running",
                "method": method,
                "trials_requested": trials_requested,
                "completed_trials": 0,
                "failed_trials": 0,
                "best_trial_id": "",
                "best_score": None,
                "trials": [],
                "created_at": self._clock().isoformat(),
                "test_data_used": False,
                "simulated": True,
                "experimental": True,
                "production_authority": False,
            }
            self._search_states[search_id] = state
        for candidate in candidates:
            trial_config = _apply_candidate(config, candidate)
            trial = {
                "candidate": candidate,
                "config_hash": trial_config.config_hash,
                "status": "pending",
                "run_id": "",
                "validation_score": None,
                "error": "",
                "trial_id": "rl_trial_" + uuid4().hex,
                "config": trial_config.to_dict(),
            }
            state["trials"].append(trial)
            self._save_trial_record(search_id, trial)
        thread = threading.Thread(
            target=self._optimization_worker,
            args=(search_id, config, prepared, candidates),
            name=f"pi15-search-{search_id[:10]}",
            daemon=True,
        )
        thread.start()
        return {"ok": True, "accepted": True, "search": state}

    def _optimization_worker(
        self,
        search_id: str,
        base_config: TrainingConfig,
        bundle: TrainingDataBundle,
        candidates: Sequence[Mapping[str, Any]],
    ) -> None:
        state = self._search_states[search_id]
        best_score = -math.inf
        for trial, candidate in zip(state["trials"], candidates, strict=True):
            if self._closed:
                trial["status"] = "cancelled"
                continue
            trial_config = _apply_candidate(base_config, candidate)
            try:
                launched = self._launch(
                    trial_config,
                    request_id=f"search:{search_id}",
                    bundle=bundle,
                )
                run_id = str(launched["run"]["run_id"])
                trial["run_id"] = run_id
                trial["status"] = "running"
                run = self._wait_for_run(run_id, stop_on_paused=True)
                if run.status == TrainingRunStatus.COMPLETED.value:
                    validation = (run.metrics or {}).get("validation") or {}
                    score = validation.get("score")
                    trial["validation_score"] = score
                    if isinstance(score, (int, float)) and score > best_score:
                        best_score = float(score)
                        state["best_trial_id"] = trial["trial_id"]
                        state["best_score"] = score
                    trial["status"] = "completed"
                    state["completed_trials"] += 1
                else:
                    trial["status"] = "failed"
                    trial["error"] = run.error or run.stop_reason or run.status
                    state["failed_trials"] += 1
            except Exception as exc:  # noqa: BLE001 - a trial failure is recorded
                trial["status"] = "failed"
                trial["error"] = f"{type(exc).__name__}: {exc}"
                state["failed_trials"] += 1
            self._save_trial_record(search_id, trial)
        state["status"] = "completed"
        state["finished_at"] = self._clock().isoformat()

    def _save_trial_record(self, search_id: str, trial: Mapping[str, Any]) -> None:
        config_payload = trial.get("config")
        self._store.save_trial(
            OptimizationTrial(
                trial_id=str(trial.get("trial_id") or ""),
                search_id=search_id,
                status=str(trial.get("status") or "pending"),
                config=dict(config_payload) if isinstance(config_payload, Mapping) else {},
                validation_score=(
                    None
                    if trial.get("validation_score") is None
                    else float(trial["validation_score"])
                ),
                run_id=str(trial.get("run_id") or ""),
                error=str(trial.get("error") or ""),
            )
        )

    def _wait_for_run(self, run_id: str, *, stop_on_paused: bool = False) -> TrainingRun:
        """Block until the run is terminal.

        ``stop_on_paused`` is for a caller that OWNS the single training slot (a
        walk-forward fold or an optimisation trial).  A stability pause parks
        that run's worker while it still holds the slot, so waiting forever
        would deadlock the whole sequence: the paused run is cancelled instead,
        which frees the slot and keeps its checkpoints for inspection.
        """
        while True:
            run = self._get_run(run_id)
            if stop_on_paused and run.status == TrainingRunStatus.PAUSED.value:
                # The caller owns the single training slot, so the parked run has
                # to be cancelled for the sequence to continue.  Carry the pause
                # it parked on as the cancellation reason: the fold/trial row then
                # says why it ended instead of a generic "cancelled by request".
                pause_reason = run.stop_reason or "stability pause"
                with suppress(Exception):
                    self.cancel(
                        run_id,
                        reason=(
                            f"{pause_reason}; cancelled automatically to release "
                            "the single training slot"
                        ),
                    )
            if run.terminal:
                return run
            time.sleep(0.05)

    def search(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            found = self._search_states.get(search_id)
        if found is None:
            raise TrainingValidationError(f"unknown hyperparameter search {search_id!r}")
        return {"ok": True, "search": found}

    def trials(self, search_id: str = "", limit: int = 100) -> dict[str, Any]:
        return self._store.trials(search_id, limit)

    # -- bounded sequential walk-forward folds --------------------------------
    def walk_forward(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Plan bounded rolling folds and run them sequentially.

        The plan is built and overlap-checked BEFORE the first run exists, so a
        refused fold schedule launches nothing.  Each fold is then launched as
        an ordinary run through the existing training lifecycle: configuration,
        chronological data preparation, isolated worker, validation, test and
        policy artifact.  No second trainer, artifact or registry is involved.
        """
        config = self._config_from_payload(payload)
        folds = build_walk_forward_folds(config, payload.get("folds", DEFAULT_WALK_FORWARD_FOLDS))
        walk_forward_id = "rl_wf_" + config_hash(
            {
                "config": config.payload(),
                "folds": [fold.to_dict() for fold in folds],
                "algorithm_version": TRAINING_ALGORITHM_VERSION,
                "contract_version": TRAINING_CONTRACT_VERSION,
            }
        )[:24]
        with self._lock:
            existing = self._walk_forward_states.get(walk_forward_id)
            if existing is not None:
                return {"ok": True, "walk_forward": existing, "reused": True}
            state: dict[str, Any] = {
                "walk_forward_id": walk_forward_id,
                "status": "running",
                "folds_requested": len(folds),
                "completed_folds": 0,
                "failed_folds": 0,
                "cancelled_folds": 0,
                "selected_fold": None,
                "selection_basis": "validation",
                "folds": [
                    {
                        "index": fold.index,
                        "status": "pending",
                        "run_id": "",
                        "policy_id": "",
                        "config_hash": "",
                        "validation_score": None,
                        "test": None,
                        "error": "",
                        "window": fold.to_dict(),
                    }
                    for fold in folds
                ],
                "max_folds": MAX_WALK_FORWARD_FOLDS,
                "sequential": True,
                "max_concurrent_folds": 1,
                "fold_overlap_refused": True,
                "test_used_for_training": False,
                "test_used_for_selection": False,
                "created_at": self._clock().isoformat(),
                "finished_at": "",
                "simulated": True,
                "experimental": True,
                "advisory_only": True,
                "activation_available": False,
                "production_authority": False,
            }
            self._walk_forward_states[walk_forward_id] = state
        thread = threading.Thread(
            target=self._walk_forward_worker,
            args=(walk_forward_id, config, folds),
            name=f"pi15-walk-forward-{walk_forward_id[-10:]}",
            daemon=True,
        )
        thread.start()
        return {"ok": True, "accepted": True, "walk_forward": state}

    def _walk_forward_worker(
        self,
        walk_forward_id: str,
        base_config: TrainingConfig,
        folds: Sequence[WalkForwardFold],
    ) -> None:
        """Run the folds one at a time, in chronological order.

        ``_launch`` takes the single training slot and ``_wait_for_run`` blocks
        until that fold's run is terminal, so the next fold is prepared only
        after the previous one finished: the folds never overlap in time either.
        """
        state = self._walk_forward_states[walk_forward_id]
        for row, fold in zip(state["folds"], folds, strict=True):
            if self._closed:
                row["status"] = "cancelled"
                state["cancelled_folds"] += 1
                continue
            try:
                config = fold_config(base_config, fold)
                row["config_hash"] = config.config_hash
                launched = self._launch(
                    config,
                    request_id=f"walk_forward:{walk_forward_id}:{fold.index}",
                )
                row["run_id"] = str(launched["run"]["run_id"])
                row["policy_id"] = str(launched["policy_id"])
                row["status"] = "running"
                run = self._wait_for_run(row["run_id"], stop_on_paused=True)
                if run.status == TrainingRunStatus.COMPLETED.value:
                    metrics = run.metrics or {}
                    validation = metrics.get("validation") or {}
                    test = metrics.get("test") or {}
                    row["validation_score"] = validation.get("score")
                    # Reported beside the fold, never read by select_fold().
                    row["test"] = {"score": test.get("score")}
                    row["status"] = "completed"
                    state["completed_folds"] += 1
                else:
                    row["status"] = "failed"
                    row["error"] = run.error or run.stop_reason or run.status
                    state["failed_folds"] += 1
            except Exception as exc:  # noqa: BLE001 - a fold failure is recorded
                row["status"] = "failed"
                row["error"] = f"{type(exc).__name__}: {exc}"
                state["failed_folds"] += 1
        state["selected_fold"] = select_fold(state["folds"])
        state["status"] = "completed"
        state["finished_at"] = self._clock().isoformat()

    def walk_forward_record(self, walk_forward_id: str) -> dict[str, Any]:
        with self._lock:
            found = self._walk_forward_states.get(walk_forward_id)
        if found is None:
            raise TrainingValidationError(
                f"unknown walk-forward sequence {walk_forward_id!r}"
            )
        return {"ok": True, "walk_forward": found}

    # -- resource snapshot ----------------------------------------------------
    @staticmethod
    def _resource_snapshot(elapsed: float | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cpu_count": os.cpu_count(),
            "gpu_available": False,
            "gpu_note": "no GPU is required or detected by the PI-15 pure-Python path",
            "process_rss_mb": None,
        }
        if elapsed is not None:
            result["training_elapsed_seconds"] = round(elapsed, 3)
        try:
            import psutil

            process = psutil.Process()
            result["process_rss_mb"] = round(process.memory_info().rss / (1024 * 1024), 2)
            result["cpu_percent"] = psutil.cpu_percent(interval=None)
            result["system_memory_percent"] = psutil.virtual_memory().percent
        except Exception:
            result["cpu_percent"] = None
            result["system_memory_percent"] = None
        return result

    # -- close ----------------------------------------------------------------
    def close(self) -> None:
        self._closed = True
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            runtime.cancel_event.set()
            runtime.pause_event.clear()
        for runtime in runtimes:
            if runtime.thread is not None:
                runtime.thread.join(timeout=3.0)
        self._store.close()


def runtime_elapsed(run: TrainingRun) -> float:
    try:
        started = datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
        ended = datetime.now(UTC)
        return max(0.0, (ended - started).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _metric_subset(metrics: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metrics, Mapping):
        return {}
    financial = _mapping_dict(metrics.get("financial"))
    risk = _mapping_dict(metrics.get("risk"))
    trading = _mapping_dict(metrics.get("trading"))
    decision = _mapping_dict(metrics.get("decision"))
    learning = _mapping_dict(metrics.get("learning"))
    return {
        "financial": {
            key: financial.get(key)
            for key in (
                "total_return",
                "annualized_return",
                "sharpe",
                "sortino",
                "calmar",
                "volatility",
                "fees_paid",
            )
        },
        "risk": {
            key: risk.get(key)
            for key in ("max_drawdown", "annualized_volatility", "sharpe", "sortino")
        },
        "trading": {
            key: trading.get(key)
            for key in (
                "total_trades",
                "win_rate",
                "profit_factor",
                "turnover",
                "total_costs",
                "trade_frequency",
            )
        },
        "decision": {
            key: decision.get(key)
            for key in (
                "action_distribution",
                "action_frequencies",
                "invalid_actions",
                "invalid_action_frequency",
                "unnecessary_trading",
                "constraint_violations",
            )
        },
        "learning": {
            key: learning.get(key)
            for key in (
                "total_reward",
                "average_reward",
                "reward_stability",
                "component_totals",
                "penalty_totals",
            )
        },
        "validity": dict(metrics.get("validity") or {}),
        # Per-market-regime slices are reported for this split only; they are
        # never read by the checkpoint-selection score.
        "regimes": dict(metrics.get("regimes") or {}),
        "schema": "pi15-metric-subset-v1",
    }


def _mapping_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _selection_score(metrics: Mapping[str, Any]) -> float:
    financial = _mapping_dict(metrics.get("financial"))
    risk = _mapping_dict(metrics.get("risk"))
    learning = _mapping_dict(metrics.get("learning"))
    average_reward = _finite(learning.get("average_reward"), 0.0)
    total_return = _finite(financial.get("total_return"), 0.0)
    max_drawdown = _find_number(risk, ("max_drawdown", "maximum_drawdown", "drawdown"))
    # The score is an experimental checkpoint selector, deliberately transparent
    # and separate from any profitability claim.
    return round(0.5 * average_reward + 0.5 * total_return + 0.5 * max_drawdown, 10)


def _find_number(payload: Any, keys: Sequence[str]) -> float:
    if isinstance(payload, Mapping):
        for key in keys:
            if key in payload:
                value = payload[key]
                if isinstance(value, Mapping):
                    for nested in ("value", "pct", "decimal"):
                        if nested in value:
                            result = _finite(value[nested], 0.0)
                            if result:
                                return result
                else:
                    result = _finite(value, 0.0)
                    if result:
                        return result
        for value in payload.values():
            result = _find_number(value, keys)
            if result:
                return result
    return 0.0


def _finite(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _search_candidates(
    space: Mapping[str, Sequence[Any]],
    method: str,
    count: int,
    seed: int,
) -> list[dict[str, Any]]:
    if not space:
        raise TrainingValidationError("at least one hyperparameter must be searched")
    keys = sorted(space)
    if method == "grid":
        import itertools

        product = 1
        for key in keys:
            product *= len(space[key])
        if product > MAX_OPTIMIZATION_TRIALS:
            raise TrainingValidationError(
                f"grid has {product} combinations; limit trials or use random search"
            )
        values = [
            dict(zip(keys, combo, strict=True))
            for combo in itertools.product(*(space[key] for key in keys))
        ]
        return values[:count]
    rng = random.Random(seed + 104729)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    attempts = 0
    while len(candidates) < count and attempts < count * 20:
        attempts += 1
        candidate = {key: rng.choice(list(space[key])) for key in keys}
        fingerprint = canonical_hash(candidate)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append(candidate)
    return candidates[:count]


def _apply_candidate(config: TrainingConfig, candidate: Mapping[str, Any]) -> TrainingConfig:
    values = config.payload()
    for key, value in candidate.items():
        if key not in SUPPORTED_OPTIMIZATION_FIELDS:
            raise TrainingValidationError(f"unsupported hyperparameter {key!r}")
        values[key] = tuple(value) if key == "hidden_layers" and isinstance(value, list) else value
    return TrainingConfig.from_mapping(values)


__all__ = ["TrainingManager"]
