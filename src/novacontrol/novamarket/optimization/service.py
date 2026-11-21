"""PI-18 optimization service: validated inputs, isolated recommendations."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .analytics import (
    align_benchmark,
    aligned_price_matrix,
    correlation_stability,
    diversification_metrics,
    estimate_covariance,
    liquidity_assessment,
    log_returns,
    risk_metrics,
    scenario_analysis,
)
from .inputs import (
    EligibleAsset,
    eligible_universe,
    portfolio_snapshot,
    price_series,
    snapshot_digest,
)
from .models import (
    HORIZONS,
    MAX_CONCURRENT_RUNS,
    OPTIMIZATION_CONTRACT_VERSION,
    OPTIMIZATION_PHASE,
    SOLVER_VERSION,
    TERMINAL_STATUSES,
    AllocationMethod,
    CovarianceMethod,
    OptimizationConfig,
    OptimizationInfeasibleError,
    OptimizationNotFoundError,
    OptimizationObjective,
    OptimizationResourceError,
    OptimizationStateError,
    OptimizationStatus,
    OptimizationValidationError,
    PriceSeries,
    RebalanceAction,
    ScenarioKind,
    digest_payload,
    optional_time,
    stable_id,
    text,
    utcnow,
)
from .solvers import project_feasible, solve_weights
from .store import OptimizationStore

NDArray = np.ndarray[Any, Any]


@dataclass(frozen=True, slots=True)
class InputRequest:
    portfolio_id: str
    horizon: str
    as_of: datetime
    instrument_ids: tuple[str, ...]
    config: OptimizationConfig
    benchmark_instrument_id: str = ""


class _Cancellation:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.deadline = time.monotonic()

    def cancel(self) -> None:
        self.event.set()

    def is_set(self) -> bool:
        return self.event.is_set() or time.monotonic() > self.deadline


class OptimizationService:
    """Read-only portfolio optimization and capital-allocation recommendations."""

    def __init__(
        self,
        store_path: Any,
        *,
        context_loader: Callable[[InputRequest], Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_concurrent: int = MAX_CONCURRENT_RUNS,
        run_timeout_seconds: int = 60,
        store: OptimizationStore | None = None,
    ) -> None:
        self._clock = clock or utcnow
        self._context_loader = context_loader
        self._store = store or OptimizationStore(store_path, clock=self._clock)
        self.max_concurrent = max(1, min(int(max_concurrent), MAX_CONCURRENT_RUNS))
        self.run_timeout_seconds = max(1, min(int(run_timeout_seconds), 120))
        self._lock = threading.RLock()
        self._active: dict[str, _Cancellation] = {}
        self._store.recover_stale_running(older_than_seconds=3_600, now=self._clock())

    @property
    def store(self) -> OptimizationStore:
        return self._store

    def close(self) -> None:
        with self._lock:
            for cancellation in self._active.values():
                cancellation.cancel()
        self._store.close()

    # -- configuration -------------------------------------------------------
    def create_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        config = OptimizationConfig.from_payload(payload, now=self._clock())
        stored = self._store.save_config(config.to_dict())
        return {
            "ok": True,
            "created": stored.get("config_id") == config.config_id,
            "config": stored,
            "financial_mutation": False,
        }

    def configs(
        self, *, portfolio_id: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        rows = self._store.configs(portfolio_id=portfolio_id, limit=limit, offset=offset)
        return {"ok": True, "configs": rows, "total": len(rows)}

    def config(self, config_id: str) -> dict[str, Any]:
        return {"ok": True, "config": self._store.config(config_id)}

    def _config_for_run(self, payload: Mapping[str, Any]) -> OptimizationConfig:
        config_id = text(payload.get("config_id"))
        if config_id:
            stored = self._store.config(config_id)
            config = OptimizationConfig.from_dict(stored)
            if (
                text(payload.get("portfolio_id"))
                and text(payload["portfolio_id"]) != config.portfolio_id
            ):
                raise OptimizationValidationError(
                    "the requested portfolio does not match the stored configuration"
                )
            return config
        config = OptimizationConfig.from_payload(payload, now=self._clock())
        self._store.save_config(config.to_dict())
        return config

    # -- validation / execution ---------------------------------------------
    def validate_inputs(
        self, payload: Mapping[str, Any], *, as_of: datetime | None = None
    ) -> dict[str, Any]:
        config = self._config_for_run(payload)
        effective_at = as_of or optional_time(payload.get("as_of")) or self._clock()
        if effective_at > self._clock() + timedelta(seconds=5):
            raise OptimizationValidationError("as_of cannot be in the future")
        context = self._load_context(config, effective_at)
        snapshot = portfolio_snapshot(
            _mapping(context.get("portfolio")),
            portfolio_id=config.portfolio_id,
            as_of=effective_at,
            pending_orders=_rows(context.get("pending_orders")),
        )
        eligible, excluded, _ = eligible_universe(context, snapshot, config, as_of=effective_at)
        warnings: list[str] = []
        if snapshot.stale:
            warnings.append(
                "one or more portfolio positions have stale or missing prices; "
                "the optimizer will not execute on this snapshot"
            )
        if not eligible:
            return {
                "ok": False,
                "valid": False,
                "status": OptimizationStatus.INSUFFICIENT_DATA.value,
                "snapshot": snapshot.to_dict(),
                "eligible": [],
                "excluded": excluded,
                "warnings": warnings,
                "detail": "no instrument passed eligibility and common-history checks",
                "financial_mutation": False,
            }
        selected, timestamps, prices, alignment_warnings = aligned_price_matrix(
            {asset.instrument_id: asset.series for asset in eligible},
            [asset.instrument_id for asset in eligible],
            minimum_observations=config.min_history_observations,
        )
        warnings.extend(alignment_warnings)
        # Report the configured benchmark's coverage here so a wrong or
        # too-short series is refused before a run rather than surfacing as
        # ``insufficient_data`` after the solver's inputs are assembled.
        benchmark, benchmark_timestamps = _benchmark_report(
            context, config, effective_at, timestamps, prices
        )
        warnings.extend(benchmark.pop("warnings", ()))
        if selected and benchmark["required"] and not benchmark["used"]:
            warnings.append(
                f"market benchmark {benchmark['instrument_id']} was not used: "
                f"{benchmark['reason']}"
            )
            return {
                "ok": False,
                "valid": False,
                "status": OptimizationStatus.INSUFFICIENT_DATA.value,
                "snapshot": snapshot.to_dict(),
                "eligible": [
                    asset.to_dict() for asset in eligible if asset.instrument_id in selected
                ],
                "excluded": excluded,
                "aligned_observations": len(timestamps),
                "benchmark": benchmark,
                "warnings": warnings,
                "detail": (
                    "market_model covariance needs the market benchmark "
                    f"{config.benchmark_instrument_id} but {benchmark['reason']}"
                ),
                "financial_mutation": False,
                "authority": _authority(),
            }
        return {
            "ok": bool(selected),
            "valid": bool(selected) and not snapshot.stale,
            "status": (
                OptimizationStatus.COMPLETED.value
                if selected and not snapshot.stale
                else OptimizationStatus.COMPLETED_WITH_WARNINGS.value
                if selected
                else OptimizationStatus.INSUFFICIENT_DATA.value
            ),
            "snapshot": snapshot.to_dict(),
            "eligible": [asset.to_dict() for asset in eligible if asset.instrument_id in selected],
            "excluded": excluded,
            "aligned_observations": len(benchmark_timestamps),
            "benchmark": benchmark,
            "warnings": warnings,
            "financial_mutation": False,
            "authority": _authority(),
        }

    def run(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        config = self._config_for_run(payload)
        as_of = optional_time(payload.get("as_of")) or self._clock()
        if as_of > self._clock() + timedelta(seconds=5):
            raise OptimizationValidationError("as_of cannot be in the future")
        key = text(idempotency_key or payload.get("idempotency_key"))
        run_id = (
            stable_id(
                "por_",
                key,
                config.config_id,
                config.portfolio_id,
                as_of.isoformat(),
            )
            if key
            else "por_" + uuid.uuid4().hex
        )
        existing = self._store.run(run_id) if self._find_run(run_id) else None
        if existing is not None:
            return {
                "ok": existing.get("status")
                in {
                    OptimizationStatus.COMPLETED.value,
                    OptimizationStatus.COMPLETED_WITH_WARNINGS.value,
                },
                "idempotent_reuse": True,
                "run": existing,
                "financial_mutation": False,
            }
        with self._lock:
            if len(self._active) >= self.max_concurrent:
                raise OptimizationResourceError(
                    f"at most {self.max_concurrent} optimization runs may execute concurrently"
                )
            cancellation = _Cancellation()
            cancellation.deadline = time.monotonic() + self.run_timeout_seconds
            self._active[run_id] = cancellation
        header = self._header(run_id, config, as_of)
        try:
            self._store.save_run(header)
            self._store.event(run_id, "optimization.started", {"portfolio_id": config.portfolio_id})
            result = self._execute(header, config, as_of, cancellation)
            if cancellation.event.is_set() and result.get("status") not in {
                OptimizationStatus.CANCELLED.value,
            }:
                result["status"] = OptimizationStatus.CANCELLED.value
                result["error"] = "optimization was cancelled"
            elif time.monotonic() > cancellation.deadline and result.get("status") not in {
                OptimizationStatus.CANCELLED.value,
                OptimizationStatus.TIMED_OUT.value,
            }:
                result["status"] = OptimizationStatus.TIMED_OUT.value
                result["error"] = "optimization exceeded its run timeout"
            result["finished_at"] = self._clock().isoformat()
            self._store.update_run(run_id, result)
            self._store.event(run_id, "optimization.finished", {"status": result["status"]})
            return {
                "ok": result["status"]
                in {
                    OptimizationStatus.COMPLETED.value,
                    OptimizationStatus.COMPLETED_WITH_WARNINGS.value,
                },
                "run": result,
                "financial_mutation": False,
            }
        except OptimizationStateError as exc:
            status = (
                OptimizationStatus.CANCELLED.value
                if cancellation.event.is_set()
                else OptimizationStatus.TIMED_OUT.value
                if time.monotonic() > cancellation.deadline
                else OptimizationStatus.SOLVER_FAILED.value
            )
            result = {
                **header,
                "status": status,
                "error": str(exc),
                "finished_at": self._clock().isoformat(),
            }
            self._store.update_run(run_id, result)
            return {"ok": False, "run": result, "financial_mutation": False}
        except Exception as exc:  # noqa: BLE001 - failures are structured research evidence
            result = {
                **header,
                "status": _failure_status(exc),
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": self._clock().isoformat(),
            }
            self._store.update_run(run_id, result)
            return {"ok": False, "run": result, "financial_mutation": False}
        finally:
            with self._lock:
                self._active.pop(run_id, None)

    def _find_run(self, run_id: str) -> bool:
        try:
            self._store.run(run_id)
            return True
        except OptimizationNotFoundError:
            return False

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        if run.get("status") in {status.value for status in TERMINAL_STATUSES}:
            return {
                "ok": False,
                "cancelled": False,
                "run": run,
                "detail": "the run is already terminal",
                "financial_mutation": False,
            }
        cancellation = self._active.get(run_id)
        if cancellation is not None:
            cancellation.cancel()
        run = {
            **run,
            "status": OptimizationStatus.CANCELLED.value,
            "error": "cancelled by operator",
        }
        self._store.update_run(run_id, run)
        self._store.event(run_id, "optimization.cancelled", {})
        return {"ok": True, "cancelled": True, "run": run, "financial_mutation": False}

    def _load_context(self, config: OptimizationConfig, as_of: datetime) -> Mapping[str, Any]:
        if self._context_loader is None:
            raise OptimizationValidationError("no read-only portfolio context loader is attached")
        request = InputRequest(
            portfolio_id=config.portfolio_id,
            horizon=config.horizon,
            as_of=as_of,
            instrument_ids=config.instrument_ids,
            config=config,
            benchmark_instrument_id=config.benchmark_instrument_id,
        )
        payload = self._context_loader(request)
        if not isinstance(payload, Mapping):
            raise OptimizationValidationError("portfolio context loader must return an object")
        return payload

    def _header(self, run_id: str, config: OptimizationConfig, as_of: datetime) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "optimization_id": run_id,
            "phase": OPTIMIZATION_PHASE,
            "portfolio_id": config.portfolio_id,
            "horizon": config.horizon,
            "as_of": as_of.isoformat(),
            "created_at": self._clock().isoformat(),
            "status": OptimizationStatus.RUNNING.value,
            "config_id": config.config_id,
            "config_version": config.config_version,
            "objective": config.objective,
            "method": config.method,
            "solver": SOLVER_VERSION,
            "algorithm_version": SOLVER_VERSION,
            "instrument_universe": list(config.instrument_ids),
            "portfolio_snapshot_version": "",
            "market_data_version": "",
            "forecast_version": "",
            "risk_configuration_version": "",
            "optimization_configuration_version": config.config_id,
            "result_status": OptimizationStatus.RUNNING.value,
            "advisory_only": True,
            "production_authority": False,
            "order_submitted": False,
            "financial_mutation": False,
            "currency": "INR",
            "nova_coin_rate": "1 NC = 1 INR",
        }

    def _execute(
        self,
        header: dict[str, Any],
        config: OptimizationConfig,
        as_of: datetime,
        cancellation: _Cancellation,
    ) -> dict[str, Any]:
        context = self._load_context(config, as_of)
        snapshot = portfolio_snapshot(
            _mapping(context.get("portfolio")),
            portfolio_id=config.portfolio_id,
            as_of=as_of,
            pending_orders=_rows(context.get("pending_orders")),
        )
        if snapshot.stale:
            raise OptimizationValidationError(
                "the authoritative portfolio snapshot contains stale or missing position prices"
            )
        eligible, excluded, _ = eligible_universe(context, snapshot, config, as_of=as_of)
        if not eligible:
            return {
                **header,
                "status": OptimizationStatus.INSUFFICIENT_DATA.value,
                "result_status": OptimizationStatus.INSUFFICIENT_DATA.value,
                "portfolio_snapshot": snapshot.to_dict(),
                "portfolio_snapshot_version": snapshot.version,
                "eligible": [],
                "excluded": excluded,
                "error": "no instrument passed eligibility requirements",
                "warnings": ["no usable eligible universe"],
            }
        selected, timestamps, prices, alignment_warnings = aligned_price_matrix(
            {asset.instrument_id: asset.series for asset in eligible},
            [asset.instrument_id for asset in eligible],
            minimum_observations=config.min_history_observations,
        )
        eligible_map = {asset.instrument_id: asset for asset in eligible}
        selected_assets = [eligible_map[item] for item in selected]
        dropped = [item for item in eligible if item.instrument_id not in selected]
        excluded.extend(
            {
                "instrument_id": asset.instrument_id,
                "reasons": ["insufficient aligned common history"],
            }
            for asset in dropped
        )
        if not selected_assets:
            return {
                **header,
                "status": OptimizationStatus.INSUFFICIENT_DATA.value,
                "result_status": OptimizationStatus.INSUFFICIENT_DATA.value,
                "portfolio_snapshot": snapshot.to_dict(),
                "eligible": [],
                "excluded": excluded,
                "error": "no eligible instrument has sufficient common aligned history",
                "warnings": list(alignment_warnings),
            }
        benchmark_series = _benchmark_series(context, config, as_of)
        benchmark_closes = np.empty(0, dtype=float)
        if benchmark_series is not None:
            timestamps, prices, benchmark_closes, benchmark_warnings = align_benchmark(
                timestamps,
                prices,
                observed_at=benchmark_series.observed_at,
                close=benchmark_series.close,
                minimum_observations=config.min_history_observations,
            )
            alignment_warnings = (*alignment_warnings, *benchmark_warnings)
        benchmark_required = config.covariance_method == CovarianceMethod.MARKET_MODEL.value
        if benchmark_required and benchmark_closes.size == 0:
            reason = (
                "its bars are unavailable from PI-02 market data"
                if benchmark_series is None
                else "it does not cover enough observations in the analysis window"
            )
            return {
                **header,
                "status": OptimizationStatus.INSUFFICIENT_DATA.value,
                "result_status": OptimizationStatus.INSUFFICIENT_DATA.value,
                "portfolio_snapshot": snapshot.to_dict(),
                "eligible": [asset.to_dict() for asset in selected_assets],
                "excluded": excluded,
                "stage": "benchmark_alignment",
                "error": (
                    "market_model covariance needs the market benchmark "
                    f"{config.benchmark_instrument_id} but {reason}"
                ),
                "warnings": list(alignment_warnings),
            }
        market_returns = np.diff(np.log(benchmark_closes)) if benchmark_closes.size else None
        benchmark_record: dict[str, Any] = {
            "instrument_id": config.benchmark_instrument_id,
            "used": bool(benchmark_closes.size),
            "source": "PI-02 market-data bars" if benchmark_closes.size else "",
            "interval": _interval(config.horizon, context),
            "observations": int(benchmark_closes.size),
            "read_only": True,
            "used_for_covariance": bool(benchmark_closes.size) and benchmark_required,
            "used_for_backtest": bool(benchmark_closes.size),
        }
        returns, return_warnings = log_returns(prices)
        covariance = estimate_covariance(
            returns,
            selected,
            method=config.covariance_method,
            interval=_interval(config.horizon, context),
            periods_per_year=int(HORIZONS[config.horizon]["periods_per_year"]),
            ewma_halflife=config.ewma_halflife,
            shrinkage=config.shrinkage,
            market_returns=market_returns,
        )
        current = {asset.instrument_id: asset.current_weight for asset in selected_assets}
        investable_override = (
            max(0.0, snapshot.total_value - snapshot.pending_buy_reserve) / snapshot.total_value
            if snapshot.total_value > 0
            else 0.0
        )
        downside = _downside_covariance(returns)
        try:
            solution = solve_weights(
                selected_assets,
                covariance=np.asarray(covariance.matrix, dtype=float),
                config=config,
                current_weights=current,
                downside=downside,
                total_value=snapshot.total_value,
                investable_override=investable_override,
                cancelled=cancellation,
            )
        except OptimizationInfeasibleError as exc:
            # The inputs are valid; the configured caps leave no allocation that
            # honours them.  This is its own status, not invalid input and not a
            # solver failure, so the reason survives in the run record.
            return {
                **header,
                "status": OptimizationStatus.INFEASIBLE_CONSTRAINTS.value,
                "result_status": OptimizationStatus.INFEASIBLE_CONSTRAINTS.value,
                "portfolio_snapshot": snapshot.to_dict(),
                "portfolio_snapshot_version": snapshot.version,
                "eligible": [asset.to_dict() for asset in selected_assets],
                "excluded": excluded,
                "stage": "constrained_allocation",
                "error": str(exc),
                "warnings": [
                    *alignment_warnings,
                    *return_warnings,
                    *covariance.warnings,
                ],
            }
        weights = {instrument: float(weight) for instrument, weight in solution["weights"].items()}
        warnings = [*alignment_warnings, *return_warnings, *covariance.warnings]
        if snapshot.pending_buy_reserve > 0:
            warnings.append(
                f"{snapshot.pending_buy_reserve:.2f} INR of cash is reserved by pending buy orders"
            )
        risk = risk_metrics(
            weights=weights,
            covariance=covariance,
            return_matrix=returns,
            expected_returns={
                asset.instrument_id: (
                    asset.forecast.adjusted_return if asset.forecast is not None else 0.0
                )
                for asset in selected_assets
            },
            horizon_periods=_horizon_periods(config.horizon),
            risk_free_rate=config.risk_free_rate,
            sectors={asset.instrument_id: asset.sector for asset in selected_assets},
        )
        weights, risk, de_risk_warnings = _enforce_risk_caps(
            weights,
            risk,
            covariance=covariance,
            returns=returns,
            config=config,
            selected_assets=selected_assets,
            current=current,
            total_value=snapshot.total_value,
            investable_override=investable_override,
            horizon_periods=_horizon_periods(config.horizon),
        )
        warnings.extend(de_risk_warnings)
        diversification = diversification_metrics(
            weights=weights,
            covariance=covariance,
            sectors={asset.instrument_id: asset.sector for asset in selected_assets},
            max_sector_weight=config.max_sector_weight,
        )
        liquidity = liquidity_assessment(
            weights=weights,
            instruments=[asset.instrument_id for asset in selected_assets],
            series={asset.instrument_id: asset.series for asset in selected_assets},
            total_value=snapshot.total_value,
            max_participation_rate=config.max_participation_rate,
            policy=config.liquidity_policy,
            bars_per_day=_bars_per_day(config.horizon),
        )
        rows = _allocation_rows(
            selected_assets,
            weights,
            current,
            risk,
            liquidity,
            total=snapshot.total_value,
        )
        rebalancing = _rebalancing(
            rows,
            config,
            snapshot,
            expected_returns={
                asset.instrument_id: float(
                    (asset.forecast.adjusted_return if asset.forecast is not None else 0.0) or 0.0
                )
                for asset in selected_assets
            },
            covariance=covariance,
            risk=risk,
            horizon_periods=_horizon_periods(config.horizon),
        )
        scenarios = scenario_analysis(
            weights=weights,
            covariance=covariance,
            return_matrix=returns,
            total_value=snapshot.total_value,
            sectors={asset.instrument_id: asset.sector for asset in selected_assets},
            horizon_periods=_horizon_periods(config.horizon),
            expected_returns={
                asset.instrument_id: float(
                    (asset.forecast.adjusted_return if asset.forecast is not None else 0.0) or 0.0
                )
                for asset in selected_assets
            },
        )
        warnings.extend(diversification.get("warnings", []))
        warnings.extend(solution.get("warnings", ()))
        regimes = _mapping(context.get("regimes"))
        regime_summary: dict[str, Any] = {}
        for asset in selected_assets:
            regime = _mapping(regimes.get(asset.instrument_id))
            if not regime:
                continue
            primary = text(regime.get("primary_regime") or regime.get("primary")).lower()
            regime_summary[asset.instrument_id] = {
                "regime_id": text(regime.get("regime_id")),
                "primary_regime": primary,
                "secondary_conditions": list(regime.get("secondary_conditions") or []),
                "as_of": regime.get("as_of"),
                "method": text(regime.get("method") or regime.get("method_version")),
            }
            if primary in {"high_volatility", "strong_down", "breakdown", "uncertain"}:
                warnings.append(
                    f"{asset.instrument_id}: PI-04 regime {primary} "
                    "increases allocation uncertainty"
                )
        status = (
            OptimizationStatus.COMPLETED_WITH_WARNINGS.value
            if warnings
            else OptimizationStatus.COMPLETED.value
        )
        result = {
            **header,
            "status": status,
            "result_status": status,
            "portfolio_snapshot": snapshot.to_dict(),
            "portfolio_snapshot_version": snapshot.version,
            "snapshot_digest": snapshot_digest(snapshot),
            "market_data_version": _market_version(eligible),
            "forecast_version": _forecast_version(selected_assets),
            "risk_configuration_version": _risk_version(context),
            "eligible": [asset.to_dict() for asset in selected_assets],
            "excluded": excluded,
            "expected_returns": {
                asset.instrument_id: asset.forecast.to_dict()
                for asset in selected_assets
                if asset.forecast is not None
            },
            "covariance": covariance.to_dict(),
            "correlation_stability": correlation_stability(returns),
            "allocation": rows,
            "target_weights": weights,
            "cash_reserve": max(0.0, 1.0 - sum(weights.values())),
            "cash_value": max(0.0, snapshot.total_value * (1.0 - sum(weights.values()))),
            "risk": risk,
            "diversification": diversification,
            "liquidity": liquidity,
            "rebalancing": rebalancing,
            "scenarios": scenarios,
            "solver": {
                "name": str(solution.get("method") or config.method),
                "version": SOLVER_VERSION,
                "iterations": int(solution.get("iterations") or 0),
                "fallback_used": bool(solution.get("fallback_used")),
                "warnings": list(solution.get("warnings", ())),
            },
            "warnings": list(dict.fromkeys(warnings)),
            "input_summary": {
                "eligible_count": len(selected_assets),
                "excluded_count": len(excluded),
                "aligned_observations": covariance.observations,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
                "portfolio_source": "NM-07 read-only valuation",
            },
            "explainability": _explainability(
                config=config,
                rows=rows,
                weights=weights,
                diversification=diversification,
                rebalancing=rebalancing,
                warnings=warnings,
                total=snapshot.total_value,
            ),
            "benchmark": benchmark_record,
            "analysis_history": _history_basis(
                timestamps,
                prices,
                selected,
                benchmark_closes=benchmark_closes,
                benchmark_instrument_id=config.benchmark_instrument_id,
            ),
            "market_regime": regime_summary,
            "authority": _authority(),
            "handoff": {
                "advisory_only": True,
                "production_authority": False,
                "order_submitted": False,
                "requires_nm08_reassessment": True,
                "requires_pi12_orchestration": True,
                "requires_operator_permission": True,
                "execution_path": [
                    "PI-12 autonomy orchestration",
                    "NM-08 authoritative risk assessment",
                    "NM-10 authoritative paper execution",
                ],
            },
            "notes": [
                "target weights are research recommendations, not orders",
                "the optimizer did not mutate the portfolio, cash, ledger or risk state",
                "NM-08 must independently assess any operational change",
                "hypothetical scenarios are not forecasts",
            ],
        }
        if result.get("risk_configuration_version") is None:
            result["risk_configuration_version"] = ""
        return result

    # -- reads ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        active = len(self._active)
        return {
            "phase": OPTIMIZATION_PHASE,
            "implemented": True,
            "initialised": True,
            "advisory_only": True,
            "production_authority": False,
            "order_submission_available": False,
            "active_runs": active,
            "runs": self._store.health()["runs"],
            "completed_runs": self._store.count_runs(OptimizationStatus.COMPLETED.value)
            + self._store.count_runs(OptimizationStatus.COMPLETED_WITH_WARNINGS.value),
            "failed_runs": self._store.count_runs(OptimizationStatus.FAILED.value)
            + self._store.count_runs(OptimizationStatus.SOLVER_FAILED.value)
            + self._store.count_runs(OptimizationStatus.TIMED_OUT.value),
            "infeasible_constraint_runs": self._store.count_runs(
                OptimizationStatus.INFEASIBLE_CONSTRAINTS.value
            ),
            "store": self._store.health(),
            "limits": {
                "max_concurrent_runs": self.max_concurrent,
                "run_timeout_seconds": self.run_timeout_seconds,
                "max_instruments": HORIZONS["5m"].get("max_instruments"),
            },
            "authority_preserved": _authority(),
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": OPTIMIZATION_PHASE,
            "version": OPTIMIZATION_CONTRACT_VERSION,
            "architecture": (
                "read-only snapshot + eligible universe + forecast adapter + covariance "
                "+ constrained solver + analytics + isolated recommendation store"
            ),
            "objectives": [item.value for item in OptimizationObjective],
            "methods": [item.value for item in AllocationMethod],
            "covariance_methods": [item.value for item in CovarianceMethod],
            "statuses": [item.value for item in OptimizationStatus],
            "rebalance_actions": [item.value for item in RebalanceAction],
            "horizons": {
                horizon: {
                    "group": values["group"],
                    "interval": values["interval"],
                    "periods_per_year": values["periods_per_year"],
                }
                for horizon, values in HORIZONS.items()
            },
            "constraints": [
                "long-only; short positions are not produced",
                "total target weights are bounded by the investable fraction",
                "cash is a legitimate allocation and is never forcibly deployed",
                "instrument and sector caps are enforced",
                "liquidity participation caps are enforced when observations exist",
                "turnover is bounded by the configured limit",
                "NM-08 risk approval is never replaced by optimizer metrics",
            ],
            "limits": {
                "max_instruments": 50,
                "max_concurrent_runs": self.max_concurrent,
                "run_timeout_seconds": self.run_timeout_seconds,
                "max_configs": self._store.max_configs,
                "max_runs": self._store.max_runs,
                "max_backtests": self._store.max_backtests,
            },
            "authority": _authority(),
            "currency": {"nova_coin": True, "rate": "1 NC = 1 INR", "display": "INR"},
            "events": [
                "optimization.started",
                "optimization.finished",
                "optimization.cancelled",
                "optimization.recovered",
                "optimization.scenarios.updated",
                "optimization.backtest.completed",
            ],
            "limitations": [
                "research estimates are not guarantees of future returns",
                "historical covariance and scenarios do not include every market failure mode",
                "daily volume is an estimated liquidity proxy, not confirmed execution capacity",
            ],
            "financial_mutation": False,
            "real_money": False,
            "live_trading": False,
        }

    def health(self) -> dict[str, Any]:
        store = self._store.health()
        return {
            "phase": OPTIMIZATION_PHASE,
            "ok": True,
            "health": "healthy",
            "active_runs": len(self._active),
            "store": store,
            "authority_health": {
                "nm08": "untouched external authority",
                "nm10": "untouched external authority",
                "optimizer_can_mutate_portfolio": False,
            },
            "detail": (
                "optimization health is research-store health; it never disables NM-08 or NM-10"
            ),
        }

    def run_record(self, run_id: str) -> dict[str, Any]:
        return {"ok": True, "run": self._store.run(run_id), "events": self._store.events(run_id)}

    def runs(
        self,
        *,
        portfolio_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        rows = self._store.runs(
            portfolio_id=portfolio_id, status=status, limit=limit, offset=offset
        )
        return {"ok": True, "runs": rows, "total": len(rows)}

    def history(
        self, *, portfolio_id: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self.runs(portfolio_id=portfolio_id, limit=limit, offset=offset)

    def allocations(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "status": run.get("status"),
            "allocation": run.get("allocation", []),
            "target_weights": run.get("target_weights", {}),
            "cash_reserve": run.get("cash_reserve"),
            "cash_value": run.get("cash_value"),
            "financial_mutation": False,
        }

    def diversification(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        return {"ok": True, "run_id": run_id, "diversification": run.get("diversification", {})}

    def risk(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "risk": run.get("risk", {}),
            "authority": _authority(),
        }

    def liquidity(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        return {"ok": True, "run_id": run_id, "liquidity": run.get("liquidity", {})}

    def rebalancing(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        return {"ok": True, "run_id": run_id, "rebalancing": run.get("rebalancing", {})}

    def scenarios(self, run_id: str) -> dict[str, Any]:
        self._store.run(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "scenarios": self._store.scenarios(run_id),
            "is_forecast": False,
        }

    def _completed_run(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        if run.get("status") not in {
            OptimizationStatus.COMPLETED.value,
            OptimizationStatus.COMPLETED_WITH_WARNINGS.value,
        }:
            raise OptimizationStateError(
                "only a completed optimization run can be used for this operation"
            )
        return run

    def handoff(self, run_id: str) -> dict[str, Any]:
        run = self._completed_run(run_id)
        return {
            "phase": OPTIMIZATION_PHASE,
            "run_id": run_id,
            "portfolio_id": run.get("portfolio_id"),
            "status": run.get("status"),
            "target_weights": run.get("target_weights", {}),
            "cash_reserve": run.get("cash_reserve"),
            "advisory_only": True,
            "production_authority": False,
            "order_submitted": False,
            "requires_nm08_reassessment": True,
            "requires_pi12_orchestration": True,
            "requires_operator_permission": True,
            "execution_path": [
                "PI-12 autonomy orchestration",
                "NM-08 authoritative risk assessment",
                "NM-10 authoritative paper execution",
            ],
            "authority": _authority(),
            "financial_mutation": False,
        }

    def latest(self, portfolio_id: str, *, horizon: str = "") -> dict[str, Any] | None:
        return self._store.latest_completed(portfolio_id, horizon=horizon)

    # -- scenarios / backtests ----------------------------------------------
    def run_scenarios(self, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        run = self._completed_run(run_id)
        base = list(self._store.scenarios(run_id))
        if not base:
            base = list(run.get("scenarios") or [])
            if base:
                self._store.save_scenarios(run_id, base)
        custom = payload.get("scenarios") or []
        if not isinstance(custom, Sequence) or isinstance(custom, (str, bytes, bytearray)):
            raise OptimizationValidationError("scenarios must be a list")
        generated = list(base)
        for item in custom:
            if not isinstance(item, Mapping):
                raise OptimizationValidationError("each scenario must be an object")
            name = text(item.get("name"), "custom_scenario")
            shocks = item.get("shocks") or {}
            if not isinstance(shocks, Mapping):
                raise OptimizationValidationError("scenario shocks must be an object")
            total = _number(run.get("portfolio_snapshot", {}).get("total_value")) or 0.0
            weights = {
                str(key): float(value)
                for key, value in (run.get("target_weights") or {}).items()
                if _number(value) is not None
            }
            unknown = {str(key) for key in shocks} - set(weights)
            if unknown:
                raise OptimizationValidationError(
                    "scenario shocks reference unknown or unallocated instruments: "
                    + ", ".join(sorted(unknown))
                )
            sectors = {
                str(row.get("instrument_id")): str(row.get("sector") or "unknown")
                for row in _rows(run.get("allocation"))
                if row.get("instrument_id")
            }
            change = 0.0
            contributions: list[dict[str, Any]] = []
            sector_changes: dict[str, float] = {}
            sector_weights: dict[str, float] = {}
            for instrument_id, weight in weights.items():
                shock = _number(shocks.get(instrument_id)) or 0.0
                if abs(shock) > 1.0:
                    raise OptimizationValidationError("scenario shock must be a fraction")
                contribution = total * weight * shock
                change += contribution
                sector = sectors.get(instrument_id, "unknown")
                sector_changes[sector] = sector_changes.get(sector, 0.0) + contribution
                sector_weights[sector] = sector_weights.get(sector, 0.0) + weight
                contributions.append(
                    {
                        "instrument_id": instrument_id,
                        "sector": sector,
                        "weight": weight,
                        "shock": shock,
                        "value_change": contribution,
                    }
                )
            generated.append(
                {
                    "scenario_id": "scn_"
                    + stable_id("", run_id, name, digest_payload(shocks))[:16],
                    "name": name,
                    "kind": ScenarioKind.HYPOTHETICAL.value,
                    "description": text(item.get("description"), "operator-defined shock"),
                    "portfolio_value_change": change,
                    "portfolio_value_change_pct": change / total if total else 0.0,
                    "position_contributions": contributions,
                    "sector_contributions": [
                        {
                            "sector": sector,
                            "weight": sector_weights[sector],
                            "value_change": value,
                        }
                        for sector, value in sorted(sector_changes.items())
                    ],
                    "warnings": ["operator-defined hypothetical; not a forecast"],
                    "assumptions": ["simultaneous shocks with no trading during the scenario"],
                    "is_forecast": False,
                    "is_hypothetical": True,
                }
            )
        if len(generated) > 50:
            raise OptimizationResourceError("at most 50 scenarios may be stored per run")
        self._store.save_scenarios(run_id, generated)
        self._store.update_run(run_id, {**run, "scenarios": generated})
        self._store.event(run_id, "optimization.scenarios.updated", {"count": len(generated)})
        return {"ok": True, "run_id": run_id, "scenarios": generated, "financial_mutation": False}

    def backtest(self, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        from .backtesting import run_strategy_backtest

        run = self._completed_run(run_id)
        basis = run.get("analysis_history") or {}
        if not basis:
            raise OptimizationValidationError("this run has no aligned history for a backtest")
        methods = payload.get("methods") or [
            AllocationMethod.EQUAL_WEIGHT.value,
            AllocationMethod.CURRENT.value,
            AllocationMethod.MIN_VARIANCE.value,
            AllocationMethod.RISK_PARITY.value,
            AllocationMethod.MEAN_VARIANCE.value,
            AllocationMethod.INVERSE_VOLATILITY.value,
        ]
        if not isinstance(methods, Sequence) or isinstance(methods, (str, bytes, bytearray)):
            raise OptimizationValidationError("methods must be a list")
        result = run_strategy_backtest(
            basis=basis,
            current_weights={
                item.get("instrument_id", ""): float(item.get("weight") or 0.0)
                for item in _rows(_mapping(run.get("portfolio_snapshot")).get("positions"))
                if item.get("instrument_id")
            },
            config=OptimizationConfig.from_dict(self._store.config(str(run["config_id"]))),
            initial_capital=float(
                _mapping(run.get("portfolio_snapshot")).get("total_value") or 0.0
            ),
            methods=[str(item) for item in methods],
            rebalance_frequency=int(payload.get("rebalance_frequency") or 5),
        )
        status = str(result.get("status") or "failed")
        backtest_id = "pob_" + stable_id("", run_id, digest_payload(result))[:20]
        result["backtest_id"] = backtest_id
        result["run_id"] = run_id
        result["status"] = status
        result["created_at"] = self._clock().isoformat()
        result["financial_mutation"] = False
        self._store.save_backtest(result)
        self._store.event(
            run_id,
            "optimization.backtest.completed"
            if status == "completed"
            else "optimization.backtest.failed",
            {"backtest_id": backtest_id},
        )
        return {
            "ok": status == "completed",
            "backtest": result,
            "financial_mutation": False,
        }

    def backtest_record(self, backtest_id: str) -> dict[str, Any]:
        return {"ok": True, "backtest": self._store.backtest(backtest_id)}

    def backtests(self, *, run_id: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        rows = self._store.backtests(run_id=run_id, limit=limit, offset=offset)
        return {"ok": True, "backtests": rows, "total": len(rows)}


# -- helpers ------------------------------------------------------------------


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _number(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _interval(horizon: str, context: Mapping[str, Any]) -> str:
    supplied = text(context.get("interval"))
    return supplied or str(HORIZONS[horizon]["interval"])


def _bars_per_day(horizon: str) -> float:
    return float(HORIZONS[horizon].get("bars_per_day", 1.0))


def _horizon_periods(horizon: str) -> int:
    return {
        "5m": 1,
        "15m": 1,
        "20m": 4,
        "60m": 1,
        "1d": 1,
        "3d": 3,
        "5d": 5,
        "30d": 21,
        "90d": 63,
    }.get(horizon, 1)


def _authority() -> dict[str, Any]:
    return {
        "risk": "NM-08",
        "paper_execution": "NM-10",
        "autonomy": "PI-12",
        "portfolio_accounting": "NM-07",
        "optimizer_can_mutate_portfolio": False,
        "optimizer_can_submit_orders": False,
        "optimizer_can_change_autonomy": False,
        "optimizer_can_activate_policies": False,
        "live_trading_available": False,
    }


def _failure_status(exc: Exception) -> str:
    if isinstance(exc, OptimizationResourceError):
        return OptimizationStatus.FAILED.value
    if isinstance(exc, OptimizationStateError):
        return OptimizationStatus.CANCELLED.value
    if isinstance(exc, TimeoutError):
        return OptimizationStatus.TIMED_OUT.value
    if isinstance(exc, OptimizationInfeasibleError):
        return OptimizationStatus.INFEASIBLE_CONSTRAINTS.value
    if isinstance(exc, OptimizationValidationError):
        return OptimizationStatus.INVALID_INPUT.value
    return OptimizationStatus.FAILED.value


def _downside_covariance(returns: NDArray) -> NDArray:
    downside = np.minimum(returns, 0.0)
    covariance = np.cov(downside, rowvar=False, ddof=1)
    covariance = np.atleast_2d(covariance)
    if covariance.shape != (returns.shape[1], returns.shape[1]):
        covariance = np.eye(returns.shape[1])
    return covariance


def _enforce_risk_caps(
    weights: dict[str, float],
    risk: dict[str, Any],
    *,
    covariance: Any,
    returns: NDArray,
    config: OptimizationConfig,
    selected_assets: Sequence[EligibleAsset],
    current: Mapping[str, float],
    total_value: float,
    investable_override: float,
    horizon_periods: int,
) -> tuple[dict[str, float], dict[str, Any], list[str]]:
    warnings: list[str] = []
    volatility = float(risk.get("annualized_volatility") or 0.0)
    drawdown = abs(float(risk.get("max_drawdown") or 0.0))
    scale = 1.0
    if volatility > config.max_volatility > 0:
        scale = min(scale, config.max_volatility / volatility * 0.999)
        warnings.append(
            f"allocation scaled to keep annualized volatility below {config.max_volatility:.0%}"
        )
    if drawdown > config.max_drawdown > 0:
        scale = min(scale, config.max_drawdown / drawdown * 0.999)
        warnings.append(
            f"allocation scaled to keep the historical drawdown under {config.max_drawdown:.0%}"
        )
    if scale >= 1.0:
        return weights, risk, warnings
    scaled = {key: value * scale for key, value in weights.items()}
    instruments = list(covariance.instruments)
    vector = np.array([scaled.get(item, 0.0) for item in instruments])
    upper = np.full(len(instruments), config.max_instrument_weight)
    sector_caps = {
        asset.sector: min(
            config.max_sector_weight,
            float(config.sector_limits.get(asset.sector, config.max_sector_weight)),
        )
        for asset in selected_assets
    }
    vector = project_feasible(
        vector,
        upper=upper,
        total=min(config.investable_fraction, investable_override),
        sectors=[asset.sector for asset in selected_assets],
        sector_caps=sector_caps,
    )
    scaled = {
        asset.instrument_id: float(vector[index])
        for index, asset in enumerate(selected_assets)
        if vector[index] > 1e-10
    }
    result = risk_metrics(
        weights=scaled,
        covariance=covariance,
        return_matrix=returns,
        expected_returns={
            asset.instrument_id: (
                asset.forecast.adjusted_return if asset.forecast is not None else 0.0
            )
            for asset in selected_assets
        },
        horizon_periods=horizon_periods,
        risk_free_rate=config.risk_free_rate,
        sectors={asset.instrument_id: asset.sector for asset in selected_assets},
    )
    remaining_volatility = float(result.get("annualized_volatility") or 0.0)
    remaining_drawdown = abs(float(result.get("max_drawdown") or 0.0))
    if remaining_volatility > config.max_volatility * 1.001:
        warnings.append(
            "projection could not fully satisfy the annualized volatility cap; "
            "the residual risk is reported, not hidden"
        )
    if remaining_drawdown > config.max_drawdown * 1.001:
        warnings.append(
            "projection could not fully satisfy the historical drawdown cap; "
            "the residual risk is reported, not hidden"
        )
    return scaled, result, warnings


def _explainability(
    *,
    config: OptimizationConfig,
    rows: Sequence[Mapping[str, Any]],
    weights: Mapping[str, float],
    diversification: Mapping[str, Any],
    rebalancing: Mapping[str, Any],
    warnings: Sequence[str],
    total: float,
) -> dict[str, Any]:
    """Describe the allocation drivers and binding constraints without inventing attribution."""
    positions: list[dict[str, Any]] = []
    sector_weights = _mapping(diversification.get("sector_weights"))
    binding: list[str] = []
    for row in rows:
        target = float(row.get("target_weight") or 0.0)
        if target <= 0:
            continue
        constraints: list[str] = []
        if target >= config.max_instrument_weight - 1e-8:
            constraints.append(f"instrument cap {config.max_instrument_weight:.2%}")
        sector = text(row.get("sector"), "unknown").lower()
        sector_cap = min(
            config.max_sector_weight,
            float(config.sector_limits.get(sector, config.max_sector_weight)),
        )
        sector_weight = float(sector_weights.get(sector, 0.0) or 0.0)
        if sector_weight >= sector_cap - 1e-8:
            constraints.append(f"sector cap {sector_cap:.2%}")
        cap_value = row.get("liquidity_cap_value")
        if cap_value is not None and total > 0 and target * total >= float(cap_value) - 1e-6:
            constraints.append("liquidity participation cap")
        forecast = row.get("expected_return")
        if forecast is None:
            summary = (
                "Allocated without a horizon-compatible forecast; covariance and the "
                "objective determine the weight."
            )
        else:
            summary = (
                f"Expected return {float(forecast):.2%} with "
                f"{row.get('forecast_quality') or 'unknown'} forecast quality and "
                f"{pct_text(row.get('risk_contribution'))} risk contribution."
            )
        if constraints:
            summary += " Limited by " + ", ".join(constraints) + "."
        positions.append(
            {
                "instrument_id": row.get("instrument_id"),
                "target_weight": target,
                "target_capital": float(row.get("target_capital") or 0.0),
                "expected_return": forecast,
                "forecast_quality": row.get("forecast_quality"),
                "risk_contribution": row.get("risk_contribution"),
                "binding_constraints": constraints,
                "summary": summary,
            }
        )
        binding.extend(item for item in constraints if item not in binding)
    invested = sum(float(value) for value in weights.values())
    if invested >= config.investable_fraction - 1e-8:
        binding.append(
            f"investable ceiling {config.investable_fraction:.2%} "
            f"(minimum cash reserve {config.min_cash_reserve:.2%})"
        )
    if len(positions) >= config.max_positions:
        binding.append(f"maximum positions {config.max_positions}")
    if any("turnover cap" in item for item in warnings):
        binding.append(f"turnover cap {config.max_turnover:.2%}")
    if any("volatility cap" in item for item in warnings):
        binding.append(f"annualized volatility cap {config.max_volatility:.2%}")
    if any("drawdown cap" in item for item in warnings):
        binding.append(f"historical drawdown cap {config.max_drawdown:.2%}")
    return {
        "objective": config.objective,
        "method": config.method,
        "horizon": config.horizon,
        "invested_weight": invested,
        "cash_weight": max(0.0, 1.0 - invested),
        "position_count": len(positions),
        "rebalance_action": rebalancing.get("action"),
        "binding_constraints": binding,
        "positions": positions,
        "decision": (
            "Weights are the constrained solution for the selected objective; cash is "
            "the unallocated residual. Exclusions and binding limits above explain the "
            "difference from an unconstrained allocation."
        ),
        "not_claimed": (
            "no causal claim that a security will outperform; no NM-08 approval; "
            "no order submission"
        ),
    }


def pct_text(value: object) -> str:
    try:
        return f"{float(str(value)):.2%}"
    except (TypeError, ValueError):
        return "unknown"


def _allocation_rows(
    assets: Sequence[EligibleAsset],
    weights: Mapping[str, float],
    current: Mapping[str, float],
    risk: Mapping[str, Any],
    liquidity: Mapping[str, Any],
    *,
    total: float,
) -> list[dict[str, Any]]:
    contributions = _mapping(risk.get("risk_contributions"))
    liquidity_rows = {text(row.get("instrument_id")): row for row in _rows(liquidity.get("rows"))}
    rows: list[dict[str, Any]] = []
    for asset in assets:
        target = float(weights.get(asset.instrument_id, 0.0))
        before = float(current.get(asset.instrument_id, 0.0))
        delta = target - before
        liquidity_row = liquidity_rows.get(asset.instrument_id, {})
        rows.append(
            {
                "instrument_id": asset.instrument_id,
                "exchange": asset.exchange,
                "sector": asset.sector,
                "asset_class": asset.asset_class,
                "price": asset.price,
                "current_weight": before,
                "target_weight": target,
                "weight_difference": delta,
                "target_capital": total * target,
                "current_capital": total * before,
                "capital_change": total * delta,
                "expected_return": (
                    asset.forecast.adjusted_return if asset.forecast is not None else None
                ),
                "expected_return_raw": (
                    asset.forecast.expected_return if asset.forecast is not None else None
                ),
                "forecast_uncertainty": (
                    asset.forecast.uncertainty if asset.forecast is not None else "missing"
                ),
                "forecast_quality": (
                    asset.forecast.quality if asset.forecast is not None else "missing"
                ),
                "risk_contribution": float(contributions.get(asset.instrument_id, 0.0)),
                "liquidity_status": liquidity_row.get("status", asset.liquidity_status),
                "liquidity_cap_value": liquidity_row.get("participation_cap"),
                "action": _rebalance_action(before, target, delta),
                "reasons": list(asset.reasons)
                + [str(item) for item in liquidity_row.get("reasons", [])],
            }
        )
    return rows


def _rebalance_action(before: float, target: float, delta: float) -> str:
    if before <= 0 and target > 0:
        return RebalanceAction.OPEN_POSITION.value
    if before > 0 and target <= 0:
        return RebalanceAction.CLOSE_POSITION.value
    if delta > 0:
        return RebalanceAction.INCREASE_POSITION.value
    if delta < 0:
        return RebalanceAction.REDUCE_POSITION.value
    return RebalanceAction.MAINTAIN_POSITION.value


def _rebalancing(
    rows: Sequence[Mapping[str, Any]],
    config: OptimizationConfig,
    snapshot: Any,
    *,
    expected_returns: Mapping[str, float] | None = None,
    covariance: Any | None = None,
    risk: Mapping[str, Any] | None = None,
    horizon_periods: int = 1,
) -> dict[str, Any]:
    intents: list[dict[str, Any]] = []
    total = float(snapshot.total_value)
    estimated_cost = 0.0
    buys = 0.0
    sells = 0.0
    for row in rows:
        current = float(row.get("current_weight") or 0.0)
        target = float(row.get("target_weight") or 0.0)
        delta = target - current
        action = text(row.get("action"))
        notional = abs(delta) * total
        if (
            (
                action
                not in {
                    RebalanceAction.OPEN_POSITION.value,
                    RebalanceAction.CLOSE_POSITION.value,
                }
                and abs(delta) < config.rebalance_threshold
                and notional < config.min_trade_notional
            )
            or action
            in {
                RebalanceAction.OPEN_POSITION.value,
                RebalanceAction.CLOSE_POSITION.value,
            }
            and notional < config.min_trade_notional
        ):
            action = RebalanceAction.MAINTAIN_POSITION.value
        if action == RebalanceAction.MAINTAIN_POSITION.value:
            continue
        cost = notional * (config.transaction_cost_bps + config.slippage_bps) / 10_000.0
        estimated_cost += cost
        if delta > 0:
            buys += notional
        elif delta < 0:
            sells += notional
        intents.append(
            {
                "instrument_id": row["instrument_id"],
                "side": "buy" if delta > 0 else "sell",
                "action": action,
                "current_weight": current,
                "target_weight": target,
                "weight_change": delta,
                "notional": notional,
                "estimated_transaction_cost": cost,
                "reason": (
                    f"target {target:.2%} vs current {current:.2%}; "
                    f"threshold {config.rebalance_threshold:.2%}"
                ),
            }
        )
    action = RebalanceAction.NO_REBALANCING_REQUIRED.value if not intents else "rebalance"
    cash = max(0.0, 1.0 - sum(float(row.get("target_weight") or 0.0) for row in rows))
    if cash > 0:
        intents.append(
            {
                "instrument_id": "CASH",
                "side": "hold",
                "action": RebalanceAction.HOLD_CASH.value,
                "current_weight": None,
                "target_weight": cash,
                "weight_change": None,
                "notional": cash * total,
                "estimated_transaction_cost": 0.0,
                "reason": "unallocated cash is a legitimate recommendation",
            }
        )
    expected = expected_returns or {}
    target_expected = sum(
        float(row.get("target_weight") or 0.0) * float(expected.get(row["instrument_id"]) or 0.0)
        for row in rows
    )
    current_expected = sum(
        float(row.get("current_weight") or 0.0) * float(expected.get(row["instrument_id"]) or 0.0)
        for row in rows
    )
    target_volatility = float((risk or {}).get("portfolio_volatility_horizon") or 0.0)
    current_volatility: float | None = None
    if covariance is not None:
        instruments = list(covariance.instruments)
        vector = np.array(
            [
                float(
                    next(
                        (
                            r.get("current_weight") or 0.0
                            for r in rows
                            if r["instrument_id"] == item
                        ),
                        0.0,
                    )
                )
                for item in instruments
            ]
        )
        current_variance = float(vector @ np.asarray(covariance.matrix, dtype=float) @ vector)
        current_volatility = float(
            np.sqrt(max(current_variance, 0.0)) * np.sqrt(max(1, horizon_periods))
        )
    cost_ratio = estimated_cost / total if total else 0.0
    if action == RebalanceAction.NO_REBALANCING_REQUIRED.value:
        # Do not advertise an improvement that the configured thresholds
        # declined to act on.
        cost_ratio = 0.0
    funding_required = max(0.0, buys - sells)
    available_cash = max(
        0.0,
        float(getattr(snapshot, "available_cash", snapshot.cash)) - total * config.min_cash_reserve,
    )
    funding_gap = max(0.0, funding_required - available_cash)
    return {
        "action": action,
        "intents": intents,
        "estimated_transaction_costs": estimated_cost,
        "estimated_turnover": 0.5
        * sum(abs(float(item.get("weight_change") or 0.0)) for item in intents),
        "expected_return_change": target_expected - current_expected,
        "expected_improvement": (
            target_expected - current_expected - cost_ratio
            if action != RebalanceAction.NO_REBALANCING_REQUIRED.value
            else 0.0
        ),
        "transaction_cost_drag": cost_ratio,
        "current_expected_return": current_expected,
        "target_expected_return": target_expected,
        "current_portfolio_volatility": current_volatility,
        "target_portfolio_volatility": target_volatility,
        "risk_change": (target_volatility - current_volatility)
        if current_volatility is not None
        else None,
        "funding": {
            "available_cash": available_cash,
            "pending_buy_reserve": float(getattr(snapshot, "pending_buy_reserve", 0.0)),
            "buy_notional": buys,
            "sell_notional": sells,
            "net_cash_required": funding_required,
            "funding_gap": funding_gap,
            "status": "unfunded" if funding_gap > 1e-6 else "funded",
        },
        "thresholds": {
            "rebalance_threshold": config.rebalance_threshold,
            "min_trade_notional": config.min_trade_notional,
            "max_turnover": config.max_turnover,
            "transaction_cost_bps": config.transaction_cost_bps,
            "slippage_bps": config.slippage_bps,
        },
        "note": "recommendations only; PI-12, NM-08 and NM-10 still own the operational path",
        "executes_orders": False,
    }


def _market_version(assets: Sequence[EligibleAsset]) -> str:
    """Content hash of the complete aligned input, not just the last close."""
    return digest_payload(
        {
            asset.instrument_id: {
                "interval": asset.series.interval,
                "currency": asset.series.currency,
                "observed_at": [item.isoformat() for item in asset.series.observed_at],
                "close": list(asset.series.close),
                "volume": list(asset.series.volume),
            }
            for asset in assets
        }
    )


def _forecast_version(assets: Sequence[EligibleAsset]) -> str:
    return digest_payload(
        {
            asset.instrument_id: list(asset.forecast.forecast_ids)
            if asset.forecast is not None
            else []
            for asset in assets
        }
    )


def _risk_version(context: Mapping[str, Any]) -> str:
    risk = _mapping(context.get("risk"))
    return text(
        risk.get("policy", {}).get("version") if isinstance(risk.get("policy"), Mapping) else ""
    )


def _benchmark_series(
    context: Mapping[str, Any], config: OptimizationConfig, as_of: datetime
) -> PriceSeries | None:
    """Read the configured market benchmark's PI-02 bars as a price series."""
    instrument_id = str(config.benchmark_instrument_id or "").strip()
    if not instrument_id or instrument_id in config.instrument_ids:
        return None
    raw = _mapping(context.get("benchmark"))
    rows = _rows(raw.get("bars"))
    if not rows:
        return None
    series = price_series(
        {"bars": {instrument_id: {"bars": rows}}},
        instrument_id,
        interval=_interval(config.horizon, context),
        as_of=as_of,
    )
    return series if len(series.close) >= 2 else None


def _benchmark_report(
    context: Mapping[str, Any],
    config: OptimizationConfig,
    as_of: datetime,
    timestamps: Sequence[object],
    prices: Any,
) -> tuple[dict[str, Any], list[object]]:
    """Describe whether the configured benchmark covers the analysis window.

    Mirrors the run's alignment so validation reports the same observation
    count a run would use, and says why an unusable series is unusable.
    """
    instrument_id = str(config.benchmark_instrument_id or "").strip()
    required = config.covariance_method == CovarianceMethod.MARKET_MODEL.value
    report: dict[str, Any] = {
        "instrument_id": instrument_id,
        "required": required,
        "used": False,
        "status": "not_configured",
        "source": "",
        "read_only": True,
        "observations": 0,
        "aligned_observations": 0,
        "analysis_observations": len(timestamps),
        "reason": "no market benchmark is configured",
        "warnings": [],
    }
    if not instrument_id:
        if required:
            report["reason"] = "market_model covariance needs a benchmark_instrument_id"
        return report, list(timestamps)
    rows = _rows(_mapping(context.get("benchmark")).get("bars"))
    report["observations"] = len(rows)
    series = _benchmark_series(context, config, as_of)
    if series is None:
        report["status"] = "unavailable"
        report["reason"] = (
            "its bars are unavailable from PI-02 market data"
            if not rows
            else "fewer than two usable benchmark closes at or before as_of"
        )
        return report, list(timestamps)
    kept, _, closes, benchmark_warnings = align_benchmark(
        timestamps,
        prices,
        observed_at=series.observed_at,
        close=series.close,
        minimum_observations=config.min_history_observations,
    )
    report["warnings"] = list(benchmark_warnings)
    if closes.size == 0:
        report["status"] = "incomplete"
        report["reason"] = (
            benchmark_warnings[0]
            if benchmark_warnings
            else "the benchmark does not cover enough aligned observations"
        )
        return report, list(timestamps)
    report.update(
        {
            "used": True,
            "status": "used",
            "source": "PI-02 market-data bars",
            "aligned_observations": int(closes.size),
            "reason": (
                f"covers {int(closes.size)} of {len(timestamps)} aligned observations"
            ),
        }
    )
    return report, list(kept)


def _history_basis(
    timestamps: Sequence[object],
    prices: NDArray,
    instruments: Sequence[str],
    *,
    benchmark_closes: Sequence[float] | NDArray = (),
    benchmark_instrument_id: str = "",
) -> dict[str, Any]:
    keep = min(320, len(timestamps))
    if keep <= 0:
        return {}
    start = len(timestamps) - keep
    basis: dict[str, Any] = {
        "timestamps": [str(item) for item in timestamps[start:]],
        "closes": {
            instrument: [float(value) for value in prices[start:, index].tolist()]
            for index, instrument in enumerate(instruments)
        },
        "definition": "aligned closes used for the run; no future observations are included",
    }
    if benchmark_instrument_id and len(benchmark_closes) == len(timestamps):
        basis["benchmark"] = {
            "instrument_id": benchmark_instrument_id,
            "closes": [float(value) for value in benchmark_closes[start:]],
            "source": "PI-02 market-data bars",
            "read_only": True,
        }
    return basis


__all__ = ["InputRequest", "OptimizationService"]
