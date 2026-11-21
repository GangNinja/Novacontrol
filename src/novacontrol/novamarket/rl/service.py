"""Application-facing RL research capability for PI-13.

The service is the only door through which the HTTP layer talks to the
environment.  It owns environment definitions, live episodes, recovery and the
isolated experience store.  It has no reference to NM-07, NM-08, NM-10 or the
autonomy runtime, which is what makes the absence of a production mutation path
structural rather than a UI convention.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.novamarket.backtesting.core import HistoricalBar

from .environment import RLEnvironment
from .models import (
    MAX_ACTIVE_ENVIRONMENTS,
    MAX_BARS,
    MAX_EXPERIENCES_PER_EPISODE,
    RL_ACTION_SCHEMA_VERSION,
    RL_CONTRACT_VERSION,
    RL_ENVIRONMENT_VERSION,
    RL_EXPERIENCE_SCHEMA_VERSION,
    RL_OBSERVATION_SCHEMA_VERSION,
    RL_PHASE,
    RL_REWARD_INTERFACE_VERSION,
    EpisodeStatus,
    RLAction,
    RLDataError,
    RLEnvironmentConfig,
    RLEpisode,
    RLEpisodeError,
    RLInvalidActionError,
    RLStoreError,
    RLValidationError,
    parse_time,
)
from .store import RLExperienceStore, make_episode

BarLoader = Callable[[RLEnvironmentConfig], Sequence[HistoricalBar]]
FeatureLoader = Callable[
    [RLEnvironmentConfig, datetime, Sequence[HistoricalBar]],
    Mapping[str, Any] | None,
]
ForecastLoader = Callable[[RLEnvironmentConfig, datetime], Mapping[str, Any] | None]
Publisher = Callable[[str, Mapping[str, Any]], Awaitable[None]]


class RLService:
    """Bounded environment manager, episode lifecycle and experience replay."""

    def __init__(
        self,
        path: str | Path,
        *,
        bar_loader: BarLoader | None = None,
        feature_loader: FeatureLoader | None = None,
        forecast_loader: ForecastLoader | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
        max_active_environments: int = MAX_ACTIVE_ENVIRONMENTS,
        max_experiences_per_episode: int = MAX_EXPERIENCES_PER_EPISODE,
    ) -> None:
        self._max_experiences = max(1, min(int(max_experiences_per_episode), 50_000))
        self._store = RLExperienceStore(path, max_experiences_per_episode=self._max_experiences)
        self._bar_loader = bar_loader
        self._feature_loader = feature_loader
        self._forecast_loader = forecast_loader
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_active = max(1, min(int(max_active_environments), MAX_ACTIVE_ENVIRONMENTS))
        self._active: dict[str, RLEnvironment] = {}
        self._created: dict[str, datetime] = {}
        self._lock = threading.RLock()
        self._step_latencies: deque[float] = deque(maxlen=100)
        self._reset_latencies: deque[float] = deque(maxlen=100)
        self._observation_latencies: deque[float] = deque(maxlen=100)
        self._last_error = ""
        self._cancelled: set[str] = set()

    @property
    def store(self) -> RLExperienceStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    # -- status and contract --------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            active = len(self._active)
        try:
            storage = self._store.status()
        except RLStoreError as exc:
            storage = {"error": str(exc)}
        return {
            "phase": RL_PHASE,
            "implemented": True,
            "initialised": True,
            "contract_version": RL_CONTRACT_VERSION,
            "environment_version": RL_ENVIRONMENT_VERSION,
            "environment_count": storage.get("environments", 0),
            "episode_count": storage.get("episodes", 0),
            "experience_count": storage.get("experiences", 0),
            "active_environments": active,
            "max_active_environments": self._max_active,
            "max_experiences_per_episode": self._max_experiences,
            "max_environments": storage.get("max_environments", 0),
            "max_stored_episodes": storage.get("max_stored_episodes", 0),
            "storage": storage,
            "health": self.health(),
            "last_error": self._last_error,
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
            "isolation": {
                "separate_simulated_portfolio": True,
                "separate_simulated_cash": True,
                "separate_simulated_positions": True,
                "separate_simulated_execution_records": True,
                "separate_experience_database": True,
                "nm10_order_submission": False,
                "nm08_bypass": False,
                "production_portfolio_mutation": False,
                "production_ledger_write": False,
                "production_fill_creation": False,
                "autonomy_mode_change": False,
                "operator_authorization_change": False,
                "automatic_policy_promotion": False,
                "live_trading": False,
            },
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": RL_PHASE,
            "contract_version": RL_CONTRACT_VERSION,
            "environment_version": RL_ENVIRONMENT_VERSION,
            "observation_schema_version": RL_OBSERVATION_SCHEMA_VERSION,
            "action_schema_version": RL_ACTION_SCHEMA_VERSION,
            "reward_interface_version": RL_REWARD_INTERFACE_VERSION,
            "experience_schema_version": RL_EXPERIENCE_SCHEMA_VERSION,
            "action_space": [
                {
                    "action": "buy",
                    "parameters": ["quantity|notional|size_fraction"],
                    "long_only": True,
                },
                {
                    "action": "sell",
                    "parameters": ["quantity|notional|size_fraction"],
                    "long_only": True,
                    "no_shorting": True,
                },
                {"action": "hold", "parameters": [], "effect": "simulated book unchanged"},
                {"action": "wait", "parameters": [], "effect": "simulated book unchanged"},
                {"action": "no_action", "parameters": [], "effect": "simulated book unchanged"},
            ],
            "observation_sections": [
                "market",
                "features",
                "forecasts",
                "portfolio",
                "provenance",
                "quality",
            ],
            "observation_guarantees": {
                "completed_daily_bars_only": True,
                "point_in_time": True,
                "feature_versioned": True,
                "future_feature_rows_withheld": True,
                "missing_zero_imputed_invalid_distinct": True,
                "portfolio_section_is_isolated_simulated_book": True,
            },
            "transition_rules": {
                "decision": "after a completed bar at T",
                "fill": "next completed bar open plus the NM-10 slippage and fee functions",
                "partial_fills": "bounded by configured participation of next-bar volume",
                "intraday_claim": False,
                "end_of_history": "terminated, never silently continued",
                "step_limit": "truncated, distinct from termination",
            },
            "reward": {
                "interface_only": True,
                "total": "0 by design until PI-14 supplies a reward policy",
                "components": [
                    "realized_pnl_delta",
                    "unrealized_pnl_delta",
                    "equity_return",
                    "risk_exposure",
                    "drawdown",
                    "transaction_cost",
                    "slippage_cost",
                    "decision_consistency",
                    "forecast_direction",
                    "forecast_validity",
                    "forecast_uncertainty",
                    "unnecessary_trading",
                    "constraint_violations",
                ],
            },
            "algorithm_compatibility": {
                "environment_interface": "reset/step-style",
                "gymnasium_dependency_installed": False,
                "algorithms_implemented": [],
                "future_candidates": [
                    "PPO",
                    "DQN (discrete actions)",
                    "A2C",
                    "SAC (after a justified continuous action design)",
                ],
                "reward_policy_deferred_to": "PI-14",
            },
            "safety": {
                "simulated_only": True,
                "no_nm10_order_submission": True,
                "no_production_portfolio_or_ledger_write": True,
                "no_autonomy_or_operator_change": True,
                "no_automatic_policy_promotion": True,
                "no_live_trading": True,
            },
            "limitations": [
                "one instrument per environment in PI-13",
                "daily completed OHLCV only; no intraday execution claim",
                "no order-book depth; synthetic spread and square-root impact are approximations",
                "reward total is intentionally zero until PI-14",
                "no trained policy or profitability claim in PI-13",
            ],
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            active = len(self._active)
        step_avg = (
            sum(self._step_latencies) / len(self._step_latencies) if self._step_latencies else 0.0
        )
        obs_avg = (
            sum(self._observation_latencies) / len(self._observation_latencies)
            if self._observation_latencies
            else 0.0
        )
        reset_avg = (
            sum(self._reset_latencies) / len(self._reset_latencies)
            if self._reset_latencies
            else 0.0
        )
        try:
            storage = self._store.status()
        except RLStoreError as exc:
            storage = {"error": str(exc)}
        storage_healthy = "error" not in storage
        return {
            "healthy": not bool(self._last_error) and storage_healthy,
            "active_environments": active,
            "max_active_environments": self._max_active,
            "step_latency_ms": round(step_avg * 1000, 3),
            "observation_latency_ms": round(obs_avg * 1000, 3),
            "environment_initialization_ms": round(reset_avg * 1000, 3),
            "last_step_latency_ms": round(self._step_latencies[-1] * 1000, 3)
            if self._step_latencies
            else 0.0,
            "last_error": self._last_error,
            "storage": storage,
            "bounded_resources": True,
            "cancellation_supported": True,
        }

    # -- environment catalogue ------------------------------------------------
    def create_environment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise RLValidationError("environment configuration must be an object")
        config = RLEnvironmentConfig.from_mapping(payload)
        environment_id = str(payload.get("environment_id") or "").strip()
        if not environment_id:
            environment_id = (
                "rl_env_"
                + hashlib.sha256(
                    (config.name + "|" + config.config_hash).encode("utf-8")
                ).hexdigest()[:16]
            )
        config = config.with_environment_id(environment_id)
        existing = self._store.environment(environment_id)
        if existing:
            if str(existing.get("config_hash")) != config.config_hash:
                raise RLValidationError(
                    "environment_id already exists with a different configuration"
                )
            return existing
        saved = self._store.save_environment(config.to_dict())
        return saved

    def environments(self, limit: int = 50) -> dict[str, Any]:
        return self._store.environments(limit)

    def environment(self, environment_id: str) -> dict[str, Any]:
        found = self._store.environment(environment_id)
        if found is None:
            raise RLValidationError(f"unknown RL environment {environment_id!r}")
        return found

    # -- episode lifecycle ----------------------------------------------------
    def start_episode(
        self, environment_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        if payload.get("max_steps") is not None:
            raise RLValidationError("max_steps is fixed by the immutable environment configuration")
        config = self._config_from_store(environment_id)
        with self._lock:
            if len(self._active) >= self._max_active:
                raise RLEpisodeError(
                    f"active episode limit reached ({self._max_active}); cancel an episode first"
                )
        started = time.perf_counter()
        env, warnings = self._build_environment(config)
        episode_id = "rl_ep_" + uuid.uuid4().hex
        env.reset(episode_id)
        self._observation_latencies.append(env.last_observation_seconds)
        created = self._clock()
        state = env.state()
        if warnings:
            state["data_warnings"] = warnings
        episode = make_episode(
            episode_id=episode_id,
            environment_id=environment_id,
            status=env.status.value,
            config=config.to_dict(),
            state=state,
            summary=env.summary(),
            transition_count=env.sequence,
            invalid_actions=env.invalid_actions,
            created_at=created,
        )
        with self._lock:
            # Re-check after the (possibly expensive) environment build. Two
            # concurrent starts must not both cross the active-episode limit.
            if len(self._active) >= self._max_active:
                raise RLEpisodeError(
                    f"active episode limit reached ({self._max_active}); cancel an episode first"
                )
            self._store.save_episode(episode)
            self._active[episode_id] = env
            self._created[episode_id] = created
        self._reset_latencies.append(time.perf_counter() - started)
        self._publish(
            "novamarket.rl.episode_started",
            {
                "environment_id": environment_id,
                "episode_id": episode_id,
                "steps": 0,
                "simulated": True,
            },
        )
        return {
            "ok": True,
            "episode": episode.to_dict(),
            "observation": state.get("observation"),
            "portfolio": state.get("portfolio"),
            "summary": env.summary(),
            "warnings": warnings,
            "simulated": True,
            "advisory_only": True,
        }

    def episodes(
        self, *, environment_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self._store.episodes(environment_id=environment_id, limit=limit, offset=offset)

    def episode(self, episode_id: str) -> dict[str, Any]:
        stored = self._store.episode(episode_id)
        if stored is None:
            raise RLValidationError(f"unknown RL episode {episode_id!r}")
        with self._lock:
            active = self._active.get(episode_id)
        if active is not None:
            result = active.state()
            result["config"] = active.config.to_dict()
            result["transition_count"] = active.sequence
            return result
        result = dict(stored)
        result["recovery_available"] = stored.get("status") == EpisodeStatus.RUNNING.value
        return result

    def step(self, episode_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = RLAction.from_mapping(payload)
        env = self._active_environment(episode_id)
        before_step = env.snapshot()
        started = time.perf_counter()
        try:
            observation, reward, terminated, truncated, transition = env.step(action)
        except RLInvalidActionError:
            self._persist_current(env)
            raise
        except Exception as exc:
            # A failure can happen after the environment advanced its cursor or
            # portfolio (for example while building the next observation).
            # Restore the last durable step before marking the episode failed,
            # so no half-advanced state can be persisted without an experience.
            with contextlib.suppress(Exception):
                env.restore(before_step)
            env.fail(f"{type(exc).__name__}: {exc}")
            try:
                self._persist_current(env)
            finally:
                self._drop_active(episode_id)
            self._last_error = str(exc)
            raise
        self._observation_latencies.append(env.last_observation_seconds)
        episode = self._episode_payload(env)
        transition_count = env.sequence
        try:
            # The experience and the episode summary are one transaction.  A
            # successful step is never persisted without its experience.
            self._store.record_step(episode, transition)
        except RLStoreError as exc:
            # Restore the last durable step before marking the episode failed,
            # so the database cannot advertise a transition that was rolled back.
            with contextlib.suppress(Exception):
                env.restore(before_step)
            env.fail(f"experience persistence failed: {exc}")
            try:
                self._persist_current(env)
            finally:
                self._drop_active(episode_id)
            self._last_error = str(exc)
            raise
        self._step_latencies.append(time.perf_counter() - started)
        self._publish(
            "novamarket.rl.step",
            {
                "environment_id": env.config.environment_id,
                "episode_id": episode_id,
                "sequence": transition.sequence,
                "action": transition.action.get("action_type"),
                "terminated": terminated,
                "truncated": truncated,
                "simulated": True,
            },
        )
        if terminated or truncated:
            self._publish(
                "novamarket.rl.episode_completed",
                {
                    "environment_id": env.config.environment_id,
                    "episode_id": episode_id,
                    "status": env.status.value,
                    "steps": env.steps,
                    "simulated": True,
                },
            )
            with self._lock:
                self._active.pop(episode_id, None)
                self._created.pop(episode_id, None)
        return {
            "ok": True,
            "episode_id": episode_id,
            "environment_id": env.config.environment_id,
            "status": env.status.value,
            "step_count": env.steps,
            "transition_count": transition_count,
            "observation": observation.to_dict(),
            "reward": reward.to_dict(),
            "terminated": terminated,
            "truncated": truncated,
            "portfolio": env.portfolio.to_dict(),
            "transition": transition.to_dict(),
            "summary": env.summary(),
            "simulated": True,
            "advisory_only": True,
        }

    def reset_episode(self, episode_id: str) -> dict[str, Any]:
        stored = self._store.episode(episode_id)
        if stored is None:
            raise RLValidationError(f"unknown RL episode {episode_id!r}")
        environment_id = str(stored.get("environment_id") or "")
        config = self._config_from_store(environment_id)
        env, warnings = self._build_environment(config)
        env.reset(episode_id)
        self._observation_latencies.append(env.last_observation_seconds)
        created_raw = stored.get("created_at")
        created = self._created.get(episode_id) or (
            parse_time(created_raw) if created_raw else self._clock()
        )
        state = env.state()
        if warnings:
            state["data_warnings"] = warnings
        episode = make_episode(
            episode_id=episode_id,
            environment_id=environment_id,
            status=env.status.value,
            config=config.to_dict(),
            state=state,
            summary=env.summary(),
            transition_count=0,
            invalid_actions=0,
            created_at=created,
        )
        with self._lock:
            was_active = episode_id in self._active
            if not was_active and len(self._active) >= self._max_active:
                raise RLEpisodeError(
                    f"active episode limit reached ({self._max_active}); cancel an episode first"
                )
            self._store.reset_episode(episode)
            self._active[episode_id] = env
            self._created[episode_id] = created
        self._publish(
            "novamarket.rl.episode_reset",
            {"environment_id": environment_id, "episode_id": episode_id, "simulated": True},
        )
        return {
            "ok": True,
            "episode": episode.to_dict(),
            "observation": state.get("observation"),
            "portfolio": state.get("portfolio"),
            "summary": env.summary(),
            "warnings": warnings,
            "simulated": True,
        }

    def cancel(self, episode_id: str) -> dict[str, Any]:
        stored = self._store.episode(episode_id)
        if stored is None:
            raise RLValidationError(f"unknown RL episode {episode_id!r}")
        with self._lock:
            active = self._active.get(episode_id)
        if active is None and str(stored.get("status")) == EpisodeStatus.RUNNING.value:
            # A restart leaves a running episode persisted but not live.  It is
            # still cancellable; recover its snapshot first, then cancel through
            # the same live path (never mutate the persisted row directly).
            self._active_environment(episode_id)
            return self.cancel(episode_id)
        if active is not None:
            state = active.cancel()
            episode = self._persist_current(active)
            with self._lock:
                self._active.pop(episode_id, None)
                self._cancelled.add(episode_id)
            self._publish(
                "novamarket.rl.episode_cancelled",
                {
                    "environment_id": active.config.environment_id,
                    "episode_id": episode_id,
                    "simulated": True,
                },
            )
            return {"ok": True, "episode": episode.to_dict(), "state": state, "simulated": True}
        if str(stored.get("status")) in (
            EpisodeStatus.CANCELLED.value,
            EpisodeStatus.TERMINATED.value,
            EpisodeStatus.TRUNCATED.value,
        ):
            return {"ok": True, "episode": stored, "replayed": True, "simulated": True}
        raise RLEpisodeError("only a live or running episode can be cancelled")

    # -- experience replay ----------------------------------------------------
    def experiences(self, episode_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        if self._store.episode(episode_id) is None:
            raise RLValidationError(f"unknown RL episode {episode_id!r}")
        result = self._store.experiences(episode_id, limit=limit, offset=offset)
        result["dataset_version"] = self._episode_dataset_version(episode_id)
        result["simulated"] = True
        return result

    def transitions(self, episode_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self.experiences(episode_id, limit=limit, offset=offset)

    # -- internals ------------------------------------------------------------
    def _config_from_store(self, environment_id: str) -> RLEnvironmentConfig:
        payload = self.environment(environment_id)
        return RLEnvironmentConfig.from_mapping(payload)

    def _build_environment(self, config: RLEnvironmentConfig) -> tuple[RLEnvironment, list[str]]:
        if self._bar_loader is None:
            raise RLDataError("no historical dataset loader is configured")
        bars = sorted(self._bar_loader(config), key=lambda item: item.observed_at)
        if len(bars) > MAX_BARS:
            raise RLDataError(f"historical dataset exceeds the {MAX_BARS}-bar environment limit")
        warnings: list[str] = []

        def add_warning(message: str) -> None:
            if len(warnings) < 20:
                warnings.append(message)
            elif len(warnings) == 20:
                warnings.append("additional data warnings omitted")
            # Keep the warning list bounded; one repeated loader failure must not
            # turn a 5,000-bar request into a 5,000-message response.

        # A step-limited episode never needs to materialize the whole history.
        # Keep one extra bar so the environment can report TRUNCATED rather than
        # pretending the dataset ended at the configured step boundary.
        bar_ceiling = config.max_steps + 2
        if len(bars) > bar_ceiling:
            add_warning(f"loaded the first {bar_ceiling} bars; max_steps bounds this episode")
            bars = bars[:bar_ceiling]
        features: dict[datetime, Mapping[str, Any]] = {}
        forecasts: dict[datetime, Mapping[str, Any]] = {}
        for index, bar in enumerate(bars):
            history = bars[: index + 1]
            if self._feature_loader is not None and config.include_features:
                try:
                    snapshot = self._feature_loader(config, bar.observed_at, history)
                    if snapshot:
                        features[bar.observed_at] = snapshot
                except Exception as exc:  # noqa: BLE001 - feature gap is explicit
                    add_warning(
                        f"features unavailable at {bar.observed_at.isoformat()}: "
                        f"{type(exc).__name__}: {exc}"
                    )
            if self._forecast_loader is not None and config.include_forecasts:
                try:
                    snapshot = self._forecast_loader(config, bar.observed_at)
                    if snapshot:
                        forecasts[bar.observed_at] = snapshot
                except Exception as exc:  # noqa: BLE001 - forecast gap is explicit
                    add_warning(
                        f"forecasts unavailable at {bar.observed_at.isoformat()}: "
                        f"{type(exc).__name__}: {exc}"
                    )
        env = RLEnvironment(
            config,
            bars,
            feature_snapshots=features,
            forecast_snapshots=forecasts,
            clock=self._clock,
        )
        return env, warnings

    def _active_environment(self, episode_id: str) -> RLEnvironment:
        with self._lock:
            env = self._active.get(episode_id)
            if env is not None:
                return env
        stored = self._store.episode(episode_id)
        if stored is None:
            raise RLValidationError(f"unknown RL episode {episode_id!r}")
        if str(stored.get("status")) != EpisodeStatus.RUNNING.value:
            raise RLEpisodeError(f"episode is {stored.get('status')}, not running")
        config = self._config_from_store(str(stored.get("environment_id") or ""))
        env, warnings = self._build_environment(config)
        state = dict(stored.get("state") or {})
        snapshot = dict(state.get("snapshot") or {})
        if not snapshot:
            raise RLDataError("stored running episode has no recoverable state")
        env.restore(snapshot)
        if warnings:
            env._warnings.extend(warnings)  # noqa: SLF001 - recovery provenance
        persisted_count = int(stored.get("transition_count") or 0)
        if persisted_count != env.sequence:
            raise RLDataError("stored episode transition count does not match its recovered state")
        persisted_experiences = self._store.experience_count(episode_id)
        if persisted_experiences > env.sequence:
            raise RLDataError("stored experience count exceeds the episode state")
        with self._lock:
            if len(self._active) >= self._max_active:
                raise RLEpisodeError(
                    f"active episode limit reached ({self._max_active}); cancel an episode first"
                )
            self._active[episode_id] = env
            created_raw = stored.get("created_at")
            self._created[episode_id] = parse_time(created_raw) if created_raw else self._clock()
        return env

    def _episode_payload(self, env: RLEnvironment) -> RLEpisode:
        created = self._created.get(env.episode_id) or self._clock()
        state = env.state()
        return make_episode(
            episode_id=env.episode_id,
            environment_id=env.config.environment_id,
            status=env.status.value,
            config=env.config.to_dict(),
            state=state,
            summary=env.summary(),
            transition_count=env.sequence,
            invalid_actions=env.invalid_actions,
            error=state.get("error") or "",
            created_at=created,
        )

    def _persist_current(self, env: RLEnvironment) -> RLEpisode:
        episode = self._episode_payload(env)
        self._store.save_episode(episode)
        return episode

    def _drop_active(self, episode_id: str) -> None:
        with self._lock:
            self._active.pop(episode_id, None)
            self._created.pop(episode_id, None)

    def _episode_dataset_version(self, episode_id: str) -> str:
        stored = self._store.episode(episode_id) or {}
        config = dict(stored.get("config") or {})
        return str(config.get("dataset_version") or "")

    def _publish(self, name: str, payload: Mapping[str, Any]) -> None:
        if self._publisher is None:
            return

        async def _deliver() -> None:
            assert self._publisher is not None
            await self._publisher(name, dict(payload))

        try:
            asyncio.get_running_loop().create_task(_deliver())
        except RuntimeError:
            try:
                asyncio.run(_deliver())
            except Exception as exc:  # noqa: BLE001 - committed state stands
                self._last_error = f"{name}: {type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - committed state stands
            self._last_error = f"{name}: {type(exc).__name__}: {exc}"
