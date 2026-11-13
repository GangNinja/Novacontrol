"""NM-13 controlled adaptive learning service.

The service is deliberately conservative:

* evidence is filtered before it can influence anything, and its origin is kept;
* experiments are bounded, seeded and reproducible, and evaluate on a separate
  validation window with NM-11's real replay engine;
* a candidate never becomes active because a score is high — it needs an
  approval (manual by default), a risk check and, when configured, NM-12 shadow
  validation;
* promotion and rollback are versioned records applied through one injected
  strategy seam; learning owns no execution, wallet, ledger or risk engine.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import tracemalloc
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from novacontrol.novamarket.backtesting import (
    BacktestEngine,
    BacktestError,
    BacktestStore,
    HistoricalBar,
)
from novacontrol.novamarket.learning import experiments, monitor
from novacontrol.novamarket.learning.models import (
    AlertKind,
    AlertSeverity,
    CandidateStatus,
    EvidenceKind,
    EvidenceRecord,
    EvidenceStatus,
    ExperimentStatus,
    LearningAlert,
    LearningCandidate,
    LearningExperiment,
    PerformanceSnapshot,
    RollbackRecord,
    ShadowValidationStatus,
    StrategyVersionRecord,
    VersionStatus,
    decimal,
    digest,
    ensure_candidate_transition,
    ensure_experiment_transition,
    learning_id,
    optional_time,
    parse_time,
    text,
    utcnow,
)
from novacontrol.novamarket.learning.store import LearningStore
from novacontrol.novamarket.risk.models import POLICY_DEFAULTS, RiskPolicy
from novacontrol.novamarket.strategies.catalog import validate_config

logger = logging.getLogger("novacontrol.novamarket.learning")


class LearningError(RuntimeError):
    """Base class for a refused NM-13 operation."""


class LearningValidationError(LearningError, ValueError):
    """The request is malformed, unsafe or outside declared bounds."""


class LearningNotFoundError(LearningError, LookupError):
    """No experiment, candidate, version or alert has that identity."""


class LearningStateError(LearningError):
    """The record exists but its state does not permit the requested move."""


class LearningIsolationError(LearningError):
    """An operation asked learning to execute or mutate trading state."""


_DECIMAL_FIELDS = (
    "max_position_weight",
    "max_sector_weight",
    "max_invested_fraction",
    "min_cash_fraction",
    "max_drawdown_pct",
    "max_volatility_pct",
    "min_liquidity_value",
    "max_turnover",
    "max_aggregate_sector_weight",
)


class LearningService:
    """Evidence, experiments, governance, versioning and rollback."""

    def __init__(
        self,
        path: str | Path,
        *,
        market_data: Any | None = None,
        strategy_loader: Callable[[str], Mapping[str, Any]] | None = None,
        risk_policy_loader: Callable[[str], Mapping[str, Any]] | None = None,
        backtest_loader: Callable[[str], Mapping[str, Any]] | None = None,
        paper_loader: Callable[[str], Mapping[str, Any]] | None = None,
        shadow_loader: Callable[[str], Mapping[str, Any]] | None = None,
        shadow_service: Any | None = None,
        strategy_applier: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        publisher: Callable[[str, dict[str, Any]], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        min_sample_size: int = 3,
        min_trades: int = 2,
        min_observations: int = 10,
        min_instruments: int = 1,
        min_regimes: int = 1,
        max_concurrency: int = 2,
        max_cpu_seconds: float = 30.0,
        max_memory_mb: float = 512.0,
        evidence_max_age_days: int = 90,
        divergence_threshold_pct: float = 5.0,
        initial_capital: Decimal = Decimal("100000.00"),
        recover: bool = True,
    ) -> None:
        self._store = LearningStore(path, clock=clock)
        self._market_data = market_data
        self._strategy_loader = strategy_loader
        self._risk_policy_loader = risk_policy_loader
        self._backtest_loader = backtest_loader
        self._paper_loader = paper_loader
        self._shadow_loader = shadow_loader
        self._shadow_service = shadow_service
        self._strategy_applier = strategy_applier
        self._publisher = publisher
        self._clock = clock or utcnow
        self._min_sample_size = max(1, int(min_sample_size))
        self._min_trades = max(1, int(min_trades))
        self._min_observations = max(2, int(min_observations))
        self._min_instruments = max(0, int(min_instruments))
        self._min_regimes = max(0, int(min_regimes))
        self._max_cpu_seconds = max(0.0, float(max_cpu_seconds))
        self._max_memory_mb = max(0.0, float(max_memory_mb))
        self._evidence_max_age_days = max(1, int(evidence_max_age_days))
        self._divergence_threshold_pct = float(divergence_threshold_pct)
        self._initial_capital = decimal(initial_capital)
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._jobs: dict[str, asyncio.Task[Any]] = {}
        self._cancel_requested: set[str] = set()
        self._last_error = ""
        if recover:
            self.recover()

    # -- introspection --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        counts = self._store.counts()
        experiments_status: Counter[str] = Counter()
        for experiment_row in self._store.experiments(limit=500):
            experiments_status[experiment_row.status.value] += 1
        candidates_status: Counter[str] = Counter()
        for candidate_row in self._store.candidates(limit=1000):
            candidates_status[candidate_row.status.value] += 1
        evidence_kind: Counter[str] = Counter()
        evidence_status: Counter[str] = Counter()
        for evidence_row in self._store.evidence_rows(limit=1000):
            evidence_kind[evidence_row.kind.value] += 1
            evidence_status[evidence_row.status.value] += 1
        open_alerts = self._store.alerts(status="open", limit=500)
        return {
            "ok": True,
            "phase": "NM-13",
            "initialised": True,
            "isolated": True,
            "real_money": False,
            "execution_connected": False,
            "executes_trades": False,
            "counts": counts,
            "experiments": dict(experiments_status),
            "candidates": dict(candidates_status),
            "evidence": {
                "by_kind": dict(evidence_kind),
                "by_status": dict(evidence_status),
            },
            "alerts": {
                "open": len(open_alerts),
                "critical": sum(
                    1 for row in open_alerts if row.severity is AlertSeverity.CRITICAL
                ),
            },
            "jobs": {
                "running": sum(1 for task in self._jobs.values() if not task.done()),
            },
            "coverage_gates": {
                "min_instruments": self._min_instruments,
                "min_regimes": self._min_regimes,
                "hard": True,
            },
            "resources": {
                "max_cpu_seconds": self._max_cpu_seconds,
                "max_memory_mb": self._max_memory_mb,
                "enforced": True,
            },
            "active_versions": self._active_versions(),
            "last_error": self._last_error,
            "guarantees": [
                "no broker, exchange or execution call",
                "no NM-10 order is created by learning",
                "no wallet, ledger, portfolio or risk limit is mutated",
                "promotion requires an approval and an explicit applier",
                "every version, approval and rollback is persisted",
            ],
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": "NM-13",
            "implemented": True,
            "real_money": False,
            "execution_connected": False,
            "executes_trades": False,
            "evidence_kinds": [item.value for item in EvidenceKind],
            "evidence_statuses": [item.value for item in EvidenceStatus],
            "experiment_states": [item.value for item in ExperimentStatus],
            "candidate_states": [item.value for item in CandidateStatus],
            "alert_kinds": [item.value for item in AlertKind],
            "shadow_validation_states": [item.value for item in ShadowValidationStatus],
            "methods": list(experiments.METHODS),
            "pipeline": [
                "evidence capture with provenance",
                "eligibility, freshness and sample gate",
                "instrument and regime coverage gate",
                "performance monitoring by origin",
                "bounded candidate generation",
                "NM-11 training and validation replay",
                "walk-forward, sensitivity and regime checks",
                "risk-policy compatibility gate",
                "manual or explicitly configured automatic approval",
                "optional NM-12 shadow validation",
                "versioned promotion and rollback",
            ],
            "guarantees": [
                "backtest, paper and shadow evidence are never averaged as if interchangeable",
                "a candidate cannot be promoted without approval and risk compliance",
                "automatic approval is refused unless explicitly enabled",
                "a promoted version is immutable and the active pointer is explicit",
                "rollback targets an approved version and preserves history",
                "learning cannot execute a trade or change a risk limit",
            ],
            "limitations": [
                "optimisation is bounded search, not unrestricted model training",
                "paper evidence is simulated execution, never live performance",
                "shadow validation depends on the observations supplied to NM-12",
                "regime labels are descriptive, not predictive",
            ],
            "coverage_gates": {
                "min_instruments": self._min_instruments,
                "min_regimes": self._min_regimes,
                "hard": True,
            },
            "resource_ceilings": {
                "max_cpu_seconds": self._max_cpu_seconds,
                "max_memory_mb": self._max_memory_mb,
                "enforced": True,
            },
        }

    def health(self) -> dict[str, Any]:
        counts = self._store.counts()
        open_alerts = self._store.alerts(status="open", limit=500)
        failed = [
            row for row in self._store.experiments(limit=100) if row.status is ExperimentStatus.FAILED
        ]
        healthy = not open_alerts and not failed
        return {
            "ok": True,
            "phase": "NM-13",
            "healthy": healthy,
            "reason": (
                "no open alerts or failed experiments"
                if healthy
                else f"{len(open_alerts)} open alert(s), {len(failed)} failed experiment(s)"
            ),
            "counts": counts,
            "open_alerts": len(open_alerts),
            "last_error": self._last_error,
            "execution_connected": False,
        }

    def dashboard(self) -> dict[str, Any]:
        experiments_rows = self._store.experiments(limit=50)
        candidates_rows = self._store.candidates(limit=100)
        alerts_rows = self._store.alerts(limit=50)
        return {
            "ok": True,
            "phase": "NM-13",
            "active_versions": self._active_versions(),
            "experiments": {
                "total": len(experiments_rows),
                "recent": [row.to_dict() for row in experiments_rows[:10]],
            },
            "candidates": {
                "total": len(candidates_rows),
                "pending_approval": [
                    row.to_dict()
                    for row in candidates_rows
                    if row.status is CandidateStatus.PENDING_APPROVAL
                ],
                "approved": [
                    row.to_dict()
                    for row in candidates_rows
                    if row.status is CandidateStatus.APPROVED
                ],
            },
            "shadow_validations": self._store.shadow_validations(limit=20),
            "approval_queue": [
                row.to_dict()
                for row in candidates_rows
                if row.status is CandidateStatus.PENDING_APPROVAL
            ],
            "alerts": [row.to_dict() for row in alerts_rows if row.status == "open"],
            "evidence": self._evidence_summary(),
            "promotions": self._promotions(),
            "rollbacks": [
                row.to_dict()
                for row in self._store.rollbacks(limit=20)
            ],
            "health": self.health(),
        }

    # -- evidence -------------------------------------------------------------
    def record_evidence(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        try:
            kind = EvidenceKind(str(body.get("kind") or ""))
        except ValueError as exc:
            raise LearningValidationError(
                "evidence kind must be one of: "
                + ", ".join(item.value for item in EvidenceKind)
            ) from exc
        if not str(body.get("source_ref") or "").strip() and not body.get("metrics"):
            raise LearningValidationError("evidence needs a source_ref or explicit metrics")
        if not body.get("metrics"):
            imported = self._import_source(kind, body)
            if imported is not None:
                body = imported
        record = self._eligibility(kind, body)
        if record.duplicate:
            existing = self._store.evidence_by_digest(record.evidence_digest)
            if existing is not None:
                return {
                    "ok": True,
                    "evidence": existing.to_dict(),
                    "duplicate": True,
                    "reason": "the same evidence was already recorded",
                }
        self._store.save_evidence(record)
        logger.info(
            "novamarket.learning.evidence",
            extra={
                "event": "evidence.recorded",
                "evidence_id": record.evidence_id,
                "kind": record.kind.value,
                "strategy_id": record.strategy_id,
                "status": record.status.value,
                "instrument_coverage": len(record.instruments),
                "regime_coverage": len(record.regimes),
            },
        )
        if record.status is EvidenceStatus.REJECTED:
            self._publish(
                "novamarket.learning.evidence.rejected",
                {
                    "evidence_id": record.evidence_id,
                    "kind": record.kind.value,
                    "strategy_id": record.strategy_id,
                    "reasons": list(record.reasons),
                },
            )
        else:
            self._publish(
                "novamarket.learning.evidence.recorded",
                {
                    "evidence_id": record.evidence_id,
                    "kind": record.kind.value,
                    "strategy_id": record.strategy_id,
                    "confidence": record.confidence,
                },
            )
        return {
            "ok": True,
            "evidence": record.to_dict(),
            "duplicate": False,
        }

    def evidence(self, evidence_id: str) -> dict[str, Any]:
        row = self._store.evidence(evidence_id)
        if row is None:
            raise LearningNotFoundError(f"unknown learning evidence {evidence_id!r}")
        return {"ok": True, "evidence": row.to_dict()}

    def evidence_rows(
        self,
        *,
        strategy_id: str = "",
        kind: str = "",
        status: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        rows = self._store.evidence_rows(
            strategy_id=strategy_id, kind=kind, status=status, limit=limit
        )
        return {"ok": True, "evidence": [row.to_dict() for row in rows], "total": len(rows)}

    # -- monitoring -----------------------------------------------------------
    def monitor(self, strategy_id: str, *, kind: str = "", window_days: int = 30) -> dict[str, Any]:
        if not strategy_id.strip():
            raise LearningValidationError("strategy_id is required")
        window = max(1, min(int(window_days), 365))
        rows = self._store.evidence_rows(strategy_id=strategy_id, limit=1000)
        target_kinds = (
            [EvidenceKind(kind)]
            if kind
            else [EvidenceKind.BACKTEST, EvidenceKind.PAPER, EvidenceKind.SHADOW]
        )
        now = self._clock()
        snapshots: list[PerformanceSnapshot] = []
        alerts: list[LearningAlert] = []
        open_kinds = {
            row.kind
            for row in self._store.alerts(
                strategy_id=strategy_id, status="open", limit=100
            )
        }
        for origin in target_kinds:
            current = monitor.build_snapshot(
                strategy_id,
                origin,
                rows,
                now=now,
                min_observations=self._min_sample_size,
                window_days=window,
            )
            previous_rows = [
                row
                for row in rows
                if row.window_end is not None
                and row.window_end <= now - timedelta(days=window)
                and row.window_end >= now - timedelta(days=window * 2)
            ]
            previous = monitor.build_snapshot(
                strategy_id,
                origin,
                previous_rows,
                now=now - timedelta(days=window),
                min_observations=self._min_sample_size,
                window_days=window,
            )
            label, reason = monitor.detect_deterioration(current, previous)
            current = replace(current, deterioration=label, reason=reason)
            snapshots.append(current)
            if label == "degraded":
                alerts.append(
                    self._raise_alert(
                        strategy_id=strategy_id,
                        kind=AlertKind.PERFORMANCE_DETERIORATION,
                        severity=AlertSeverity.WARNING,
                        message=f"{origin.value} performance deteriorated: {reason}",
                        metrics=dict(current.metrics),
                        evidence_refs=current.evidence_refs,
                    )
                )
            for anomaly in monitor.detect_anomalies(current, previous):
                anomaly_kind = AlertKind(str(anomaly["kind"]))
                if anomaly_kind in open_kinds:
                    continue
                open_kinds.add(anomaly_kind)
                alerts.append(
                    self._raise_alert(
                        strategy_id=strategy_id,
                        kind=anomaly_kind,
                        severity=AlertSeverity(str(anomaly["severity"])),
                        message=f"{origin.value}: {anomaly['message']}",
                        metrics=dict(anomaly.get("metrics") or {}),
                        evidence_refs=current.evidence_refs,
                    )
                )
            regime_reason = monitor.regime_change(current, previous)
            if regime_reason:
                alerts.append(
                    self._raise_alert(
                        strategy_id=strategy_id,
                        kind=AlertKind.REGIME_CHANGE,
                        severity=AlertSeverity.INFO,
                        message=f"{origin.value} regime coverage changed: {regime_reason}",
                        metrics={"regimes": list(current.regimes)},
                        evidence_refs=current.evidence_refs,
                    )
                )
        spread = monitor.source_divergence(
            snapshots, threshold_pct=self._divergence_threshold_pct
        )
        if spread is not None:
            alerts.append(
                self._raise_alert(
                    strategy_id=strategy_id,
                    kind=AlertKind.SOURCE_DIVERGENCE,
                    severity=AlertSeverity.WARNING,
                    message=(
                        "backtest, paper and shadow returns diverge by "
                        f"{spread['spread_pct']:.2f}pp; they were not averaged"
                    ),
                    metrics=spread,
                    evidence_refs=tuple(
                        ref for snapshot in snapshots for ref in snapshot.evidence_refs
                    ),
                )
            )
        active_version_id = self._store.active_version(strategy_id)
        degraded = [
            snapshot for snapshot in snapshots if snapshot.deterioration == "degraded"
        ]
        if active_version_id and degraded:
            active_version = self._store.version(active_version_id)
            evaluation = (
                {} if active_version is None else dict(active_version.evaluation)
            )
            baseline = evaluation.get("baseline") or {}
            validation = evaluation.get("validation") or {}
            baseline_validation = baseline.get("validation") or {}
            existing = self._store.alerts(
                strategy_id=strategy_id, status="open", limit=100
            )
            already_recommended = any(
                row.kind is AlertKind.ROLLBACK_RECOMMENDED for row in existing
            )
            if not already_recommended:
                sources = ", ".join(
                    snapshot.kind.value for snapshot in degraded
                )
                alerts.append(
                    self._raise_alert(
                        strategy_id=strategy_id,
                        kind=AlertKind.ROLLBACK_RECOMMENDED,
                        severity=AlertSeverity.WARNING,
                        message=(
                            f"active version {active_version_id} shows degradation "
                            f"in {sources}; rollback is recommended for approval; "
                            "automatic rollback is not configured"
                        ),
                        metrics={
                            "active_version": active_version_id,
                            "version_present": active_version is not None,
                            "candidate_validation_return": validation.get(
                                "total_return"
                            ),
                            "baseline_validation_return": baseline_validation.get(
                                "total_return"
                            ),
                            "sources": {
                                snapshot.kind.value: snapshot.metrics.get("return_pct")
                                for snapshot in snapshots
                            },
                        },
                        evidence_refs=tuple(
                            ref for snapshot in snapshots for ref in snapshot.evidence_refs
                        ),
                    )
                )
        self._publish(
            "novamarket.learning.monitor.completed",
            {
                "strategy_id": strategy_id,
                "snapshots": len(snapshots),
                "alerts": len(alerts),
            },
        )
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "window_days": window,
            "snapshots": [row.to_dict() for row in snapshots],
            "alerts": [row.to_dict() for row in alerts],
            "divergence": spread,
        }

    def alerts(
        self, *, strategy_id: str = "", status: str = "", limit: int = 100
    ) -> dict[str, Any]:
        rows = self._store.alerts(strategy_id=strategy_id, status=status, limit=limit)
        return {"ok": True, "alerts": [row.to_dict() for row in rows], "total": len(rows)}

    def acknowledge_alert(self, alert_id: str, actor: str = "") -> dict[str, Any]:
        row = self._store.acknowledge_alert(alert_id, actor or "operator")
        if row is None:
            raise LearningNotFoundError(f"unknown learning alert {alert_id!r}")
        self._publish(
            "novamarket.learning.alert.acknowledged",
            {"alert_id": row.alert_id, "strategy_id": row.strategy_id},
        )
        return {"ok": True, "alert": row.to_dict()}

    # -- experiments ----------------------------------------------------------
    def create_experiment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        strategy_id = str(body.get("strategy_id") or "").strip()
        if not strategy_id:
            raise LearningValidationError("strategy_id is required")
        strategy = self._strategy_record(strategy_id)
        catalog_id = str(body.get("catalog_id") or strategy.get("catalog_id") or "")
        baseline_config = dict(body.get("baseline_config") or strategy.get("config") or {})
        baseline_version = str(
            body.get("baseline_version") or strategy.get("version") or ""
        )
        if not catalog_id or not baseline_version:
            raise LearningValidationError(
                "a baseline strategy needs a catalog_id and version"
            )
        try:
            validate_config(catalog_id, baseline_config)
        except Exception as exc:  # noqa: BLE001
            raise LearningValidationError(f"baseline configuration is invalid: {exc}") from exc
        try:
            train_start = parse_time(body.get("train_start"))
            train_end = parse_time(body.get("train_end"))
            validation_start = parse_time(body.get("validation_start"))
            validation_end = parse_time(body.get("validation_end"))
        except ValueError as exc:
            raise LearningValidationError(
                f"experiment windows must be valid timezone-aware ISO values: {exc}"
            ) from exc
        now = self._clock()
        if not (train_start < train_end <= validation_start < validation_end):
            raise LearningValidationError(
                "windows must satisfy train_start < train_end <= validation_start < validation_end"
            )
        if validation_end > now:
            raise LearningValidationError(
                "validation_end must not be in the future; future information would leak"
            )
        instruments = tuple(
            str(item) for item in body.get("instruments") or () if str(item).strip()
        )
        if not instruments:
            raise LearningValidationError("at least one instrument is required")
        method = str(body.get("method") or "grid").lower()
        if method not in experiments.METHODS:
            raise LearningValidationError(
                "method must be one of " + ", ".join(experiments.METHODS)
            )
        seed = int(body.get("seed") or 42)
        count = int(body.get("candidate_count") or 3)
        objective = str(body.get("objective") or "risk_adjusted_return")
        approval_policy = self._approval_policy(body.get("approval_policy"))
        explicit = body.get("candidate_configs")
        if explicit:
            candidate_configs = [dict(row) for row in explicit if isinstance(row, Mapping)]
            bounds = experiments.bounds_for(catalog_id, baseline_config)
            for config in candidate_configs:
                try:
                    validate_config(catalog_id, config)
                except Exception as exc:  # noqa: BLE001
                    raise LearningValidationError(
                        f"candidate configuration is invalid: {exc}"
                    ) from exc
                for key, value in config.items():
                    if key not in bounds or isinstance(value, bool):
                        continue
                    if not isinstance(value, (int, float)):
                        continue
                    low, high = bounds[key]
                    if not low <= float(value) <= high:
                        raise LearningValidationError(
                            f"{key}={value} is outside the declared bounds "
                            f"[{low:g}, {high:g}]"
                        )
                if catalog_id == "trend_sma":
                    fast = config.get("fast_period")
                    slow = config.get("slow_period")
                    if (
                        isinstance(fast, int)
                        and isinstance(slow, int)
                        and fast >= slow
                    ):
                        raise LearningValidationError(
                            "trend_sma requires fast_period < slow_period"
                        )
        else:
            try:
                candidate_configs = experiments.generate_candidates(
                    catalog_id,
                    baseline_config,
                    count=count,
                    method=method,
                    seed=seed,
                )
            except ValueError as exc:
                raise LearningValidationError(str(exc)) from exc
        if not candidate_configs:
            raise LearningValidationError(
                "the declared bounds produced no valid candidate; supply candidate_configs"
            )
        baseline_snapshot = dict(
            body.get("baseline_snapshot")
            or {
                "positions": [],
                "cash": "100000",
                "total_value": "100000",
                "currency": "INR",
            }
        )
        key = digest(
            {
                "strategy_id": strategy_id,
                "baseline_version": baseline_version,
                "baseline_config": baseline_config,
                "candidate_configs": candidate_configs,
                "train_start": train_start.isoformat(),
                "train_end": train_end.isoformat(),
                "validation_start": validation_start.isoformat(),
                "validation_end": validation_end.isoformat(),
                "instruments": list(instruments),
                "objective": objective,
                "method": method,
                "seed": seed,
                "approval_policy": approval_policy,
            }
        )
        existing = self._store.experiment_by_key(key)
        if existing is not None:
            return {
                "ok": True,
                "experiment": existing.to_dict(),
                "replayed": True,
                "candidates": [
                    row.to_dict()
                    for row in self._store.candidates(
                        experiment_id=existing.experiment_id, limit=50
                    )
                ],
            }
        experiment_id = learning_id("lex_", key, time.time_ns())
        moment = self._clock()
        experiment = LearningExperiment(
            experiment_id=experiment_id,
            strategy_id=strategy_id,
            catalog_id=catalog_id,
            baseline_version=baseline_version,
            baseline_config=baseline_config,
            objective=objective,
            method=method,
            seed=seed,
            requested_candidates=len(candidate_configs),
            candidate_configs=tuple(candidate_configs),
            evidence_refs=tuple(
                str(item) for item in body.get("evidence_refs") or ()
            ),
            train_start=train_start,
            train_end=train_end,
            validation_start=validation_start,
            validation_end=validation_end,
            instruments=instruments,
            baseline_snapshot=baseline_snapshot,
            approval_policy=approval_policy,
            min_trades=max(1, int(body.get("min_trades") or self._min_trades)),
            min_observations=max(
                2, int(body.get("min_observations") or self._min_observations)
            ),
            status=ExperimentStatus.CREATED,
            progress=0,
            results={},
            candidate_ids=(),
            error="",
            created_at=moment,
            started_at=None,
            completed_at=None,
            cancelled_at=None,
            idempotency_key=key,
        )
        candidates: list[LearningCandidate] = []
        for config in candidate_configs:
            candidate_id = learning_id("lcn_", experiment_id, digest(config))
            candidates.append(
                LearningCandidate(
                    candidate_id=candidate_id,
                    experiment_id=experiment_id,
                    strategy_id=strategy_id,
                    catalog_id=catalog_id,
                    baseline_version=baseline_version,
                    baseline_config=baseline_config,
                    config=config,
                    config_digest=digest(config),
                    objective=objective,
                    status=CandidateStatus.GENERATED,
                    evaluation={},
                    risk={},
                    policy={},
                    shadow_validation={
                        "status": ShadowValidationStatus.PENDING.value
                        if approval_policy.get("require_shadow")
                        else ShadowValidationStatus.NOT_REQUIRED.value
                    },
                    approval={},
                    promoted_version="",
                    reasons=(),
                    created_at=moment,
                    updated_at=moment,
                )
            )
        experiment = replace(
            experiment, candidate_ids=tuple(row.candidate_id for row in candidates)
        )
        with self._store.transaction():
            self._store.save_experiment(experiment)
            for row in candidates:
                self._store.save_candidate(row)
        self._publish(
            "novamarket.learning.experiment.created",
            {
                "experiment_id": experiment_id,
                "strategy_id": strategy_id,
                "candidates": len(candidates),
                "method": method,
            },
        )
        return {
            "ok": True,
            "experiment": experiment.to_dict(),
            "candidates": [row.to_dict() for row in candidates],
            "replayed": False,
        }

    async def start_experiment(self, experiment_id: str) -> dict[str, Any]:
        experiment = self._load_experiment(experiment_id)
        if experiment.status is ExperimentStatus.QUEUED:
            return {"ok": True, "experiment": experiment.to_dict(), "started": False}
        if experiment.status is not ExperimentStatus.CREATED:
            raise LearningStateError(
                f"experiment {experiment_id} is {experiment.status.value}; "
                "only a created experiment can start"
            )
        queued = replace(experiment, status=ExperimentStatus.QUEUED)
        self._store.save_experiment(queued)
        self._publish(
            "novamarket.learning.experiment.started",
            {"experiment_id": experiment_id, "strategy_id": experiment.strategy_id},
        )
        task = asyncio.create_task(self._run_job(experiment_id))
        self._jobs[experiment_id] = task
        return {"ok": True, "experiment": queued.to_dict(), "started": True}

    async def run_experiment(self, experiment_id: str) -> dict[str, Any]:
        experiment = self._load_experiment(experiment_id)
        if experiment.status is not ExperimentStatus.CREATED:
            if experiment.status is ExperimentStatus.COMPLETED:
                return {"ok": True, "experiment": experiment.to_dict(), "completed": True}
            raise LearningStateError(
                f"experiment {experiment_id} is {experiment.status.value}; "
                "only a created experiment can run"
            )
        async with self._semaphore:
            return await self._evaluate_all(experiment_id)

    async def cancel_experiment(self, experiment_id: str, reason: str = "") -> dict[str, Any]:
        experiment = self._load_experiment(experiment_id)
        if experiment.status in (
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
        ):
            raise LearningStateError(
                f"experiment {experiment_id} is {experiment.status.value}; it cannot be cancelled"
            )
        self._cancel_requested.add(experiment_id)
        task = self._jobs.pop(experiment_id, None)
        if task is not None and not task.done():
            task.cancel()
        cancelled = replace(
            experiment,
            status=ExperimentStatus.CANCELLED,
            cancelled_at=self._clock(),
            error=reason or "cancelled by operator",
        )
        self._store.save_experiment(cancelled)
        self._publish(
            "novamarket.learning.experiment.cancelled",
            {"experiment_id": experiment_id, "strategy_id": experiment.strategy_id},
        )
        return {"ok": True, "experiment": cancelled.to_dict()}

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        return {"ok": True, "experiment": self._load_experiment(experiment_id).to_dict()}

    def experiments(self, limit: int = 50) -> dict[str, Any]:
        rows = self._store.experiments(limit=limit)
        return {"ok": True, "experiments": [row.to_dict() for row in rows], "total": len(rows)}

    def candidates(
        self, *, experiment_id: str = "", strategy_id: str = "", limit: int = 200
    ) -> dict[str, Any]:
        rows = self._store.candidates(
            experiment_id=experiment_id, strategy_id=strategy_id, limit=limit
        )
        return {"ok": True, "candidates": [row.to_dict() for row in rows], "total": len(rows)}

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "candidate": self._load_candidate(candidate_id).to_dict(),
            "shadow_validation_history": self._store.shadow_validations(
                candidate_id=candidate_id, limit=50
            ),
        }

    async def evaluate_candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate = self._load_candidate(candidate_id)
        if candidate.status not in (
            CandidateStatus.GENERATED,
            CandidateStatus.EVALUATION_FAILED,
        ):
            if candidate.status in (
                CandidateStatus.PENDING_APPROVAL,
                CandidateStatus.APPROVED,
                CandidateStatus.REJECTED,
            ):
                return {"ok": True, "candidate": candidate.to_dict(), "evaluated": False}
            raise LearningStateError(
                f"candidate {candidate_id} is {candidate.status.value}; it cannot be evaluated"
            )
        experiment = self._load_experiment(candidate.experiment_id)
        evaluated = await self._evaluate_one(experiment, candidate)
        return {"ok": True, "candidate": evaluated.to_dict(), "evaluated": True}

    # -- governance -----------------------------------------------------------
    def approve_candidate(self, candidate_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        candidate = self._load_candidate(candidate_id)
        if candidate.status is CandidateStatus.APPROVED:
            return {"ok": True, "candidate": candidate.to_dict(), "unchanged": True}
        if candidate.status is not CandidateStatus.PENDING_APPROVAL:
            raise LearningStateError(
                f"candidate {candidate_id} is {candidate.status.value}; "
                "only a pending candidate can be approved"
            )
        if not candidate.risk.get("compliant", False):
            raise LearningStateError(
                "candidate is not risk-policy compliant: "
                + "; ".join(candidate.risk.get("reasons") or ["risk check unavailable"])
            )
        experiment = self._load_experiment(candidate.experiment_id)
        if experiment.approval_policy.get("require_shadow"):
            status = str(candidate.shadow_validation.get("status") or "")
            if status != ShadowValidationStatus.PASSED.value:
                raise LearningStateError(
                    "shadow validation must pass before approval"
                )
        actor = str(body.get("actor") or "operator")
        automatic = bool(body.get("automatic"))
        if automatic and not self._auto_approval_allowed(experiment):
            raise LearningStateError(
                "automatic approval was not explicitly enabled for this experiment"
            )
        moment = self._clock()
        approval = {
            "decision": "approved",
            "actor": actor,
            "at": moment.isoformat(),
            "automatic": automatic,
            "note": str(body.get("note") or ""),
        }
        updated = replace(
            candidate,
            status=CandidateStatus.APPROVED,
            approval=approval,
            updated_at=moment,
        )
        with self._store.transaction():
            self._store.save_candidate(updated)
            self._store.save_approval(
                learning_id("lap_", candidate_id, "approved", moment.isoformat()),
                candidate_id,
                "approved",
                approval,
            )
        self._publish(
            "novamarket.learning.candidate.approved",
            {
                "candidate_id": candidate_id,
                "experiment_id": updated.experiment_id,
                "automatic": automatic,
            },
        )
        return {"ok": True, "candidate": updated.to_dict()}

    def reject_candidate(
        self, candidate_id: str, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        if not reason.strip():
            raise LearningValidationError("a rejection needs a reason")
        candidate = self._load_candidate(candidate_id)
        if candidate.status is CandidateStatus.REJECTED:
            return {"ok": True, "candidate": candidate.to_dict(), "unchanged": True}
        if candidate.status not in (
            CandidateStatus.PENDING_APPROVAL,
            CandidateStatus.APPROVED,
            CandidateStatus.EVALUATION_FAILED,
        ):
            raise LearningStateError(
                f"candidate {candidate_id} is {candidate.status.value}; it cannot be rejected"
            )
        moment = self._clock()
        approval = {
            "decision": "rejected",
            "actor": actor or "operator",
            "at": moment.isoformat(),
            "reason": reason,
        }
        updated = replace(
            candidate,
            status=CandidateStatus.REJECTED,
            approval=approval,
            reasons=(*candidate.reasons, reason),
            updated_at=moment,
        )
        self._store.save_candidate(updated)
        self._store.save_approval(
            learning_id("lap_", candidate_id, "rejected", moment.isoformat()),
            candidate_id,
            "rejected",
            approval,
        )
        self._publish(
            "novamarket.learning.candidate.rejected",
            {
                "candidate_id": candidate_id,
                "experiment_id": updated.experiment_id,
                "reason": reason,
            },
        )
        return {"ok": True, "candidate": updated.to_dict()}

    async def shadow_validate_candidate(
        self, candidate_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        body = dict(payload or {})
        candidate = self._load_candidate(candidate_id)
        if candidate.status not in (
            CandidateStatus.PENDING_APPROVAL,
            CandidateStatus.APPROVED,
        ):
            raise LearningStateError(
                f"candidate {candidate_id} is {candidate.status.value}; "
                "shadow validation runs on a pending or approved candidate"
            )
        if self._shadow_service is None:
            raise LearningStateError("NM-12 shadow validation is unavailable")
        experiment = self._load_experiment(candidate.experiment_id)
        started = self._clock()
        validation_id = learning_id(
            "lsv_", candidate_id, started.isoformat()
        )
        validation_row: dict[str, Any] = {
            "validation_id": validation_id,
            "candidate_id": candidate_id,
            "experiment_id": candidate.experiment_id,
            "status": "running",
            "started_at": started.isoformat(),
            "completed_at": None,
            "previous_status": candidate.status.value,
        }
        self._store.save_shadow_validation(validation_row)
        candidate = replace(
            candidate,
            status=CandidateStatus.SHADOW_VALIDATION,
            shadow_validation={
                **dict(candidate.shadow_validation),
                **validation_row,
            },
            updated_at=started,
        )
        self._store.save_candidate(candidate)
        logger.info(
            "novamarket.learning.shadow_validation",
            extra={
                "event": "shadow_validation.started",
                "candidate_id": candidate_id,
                "validation_id": validation_id,
            },
        )
        try:
            result = await self._shadow_validate(experiment, candidate, body)
        except Exception as exc:
            failed_at = self._clock()
            failed_row = {
                **validation_row,
                "status": ShadowValidationStatus.FAILED.value,
                "completed_at": failed_at.isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            self._store.save_shadow_validation(failed_row)
            self._store.save_candidate(
                replace(
                    candidate,
                    status=CandidateStatus(candidate.shadow_validation["previous_status"]),
                    shadow_validation=failed_row,
                    updated_at=failed_at,
                )
            )
            raise
        moment = self._clock()
        if result["status"] == ShadowValidationStatus.FAILED.value:
            status = CandidateStatus.REJECTED
        elif validation_row["previous_status"] == CandidateStatus.APPROVED.value:
            status = CandidateStatus.APPROVED
        else:
            status = CandidateStatus.PENDING_APPROVAL
        updated = replace(
            candidate,
            shadow_validation={
                **result,
                "validation_id": validation_id,
                "started_at": validation_row["started_at"],
                "completed_at": moment.isoformat(),
            },
            status=status,
            updated_at=moment,
        )
        self._store.save_shadow_validation(
            {
                **validation_row,
                "status": result["status"],
                "completed_at": moment.isoformat(),
                "result": result,
            }
        )
        self._store.save_candidate(updated)
        logger.info(
            "novamarket.learning.shadow_validation",
            extra={
                "event": "shadow_validation.completed",
                "candidate_id": candidate_id,
                "validation_id": validation_id,
                "status": result["status"],
            },
        )
        self._publish(
            "novamarket.learning.candidate.shadow.validated",
            {
                "candidate_id": candidate_id,
                "status": result["status"],
                "decisions": result.get("candidate_decisions", 0),
            },
        )
        return {"ok": True, "candidate": updated.to_dict(), "validation": result}

    def promote_candidate(
        self, candidate_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        body = dict(payload or {})
        candidate = self._load_candidate(candidate_id)
        if candidate.status is CandidateStatus.PROMOTED and candidate.promoted_version:
            return {
                "ok": True,
                "candidate": candidate.to_dict(),
                "version": self._load_version(candidate.promoted_version).to_dict(),
                "unchanged": True,
            }
        if candidate.status is not CandidateStatus.APPROVED:
            raise LearningStateError(
                f"candidate {candidate_id} is {candidate.status.value}; "
                "only an approved candidate can be promoted"
            )
        experiment = self._load_experiment(candidate.experiment_id)
        if experiment.approval_policy.get("require_shadow"):
            if candidate.shadow_validation.get("status") != ShadowValidationStatus.PASSED.value:
                raise LearningStateError(
                    "promotion requires a passed shadow validation"
                )
        if not candidate.risk.get("compliant", False):
            raise LearningStateError("candidate is not risk-policy compliant")
        if self._strategy_applier is None:
            raise LearningStateError(
                "no strategy applier is configured; promotion would be a record "
                "without an effect"
            )
        version_id = learning_id(
            "lrv_", candidate.strategy_id, candidate_id, candidate.config_digest
        )
        existing = self._store.version(version_id)
        if existing is not None:
            return {
                "ok": True,
                "candidate": candidate.to_dict(),
                "version": existing.to_dict(),
                "unchanged": True,
            }
        previous = self._store.active_version(candidate.strategy_id)
        moment = self._clock()
        try:
            before_config = dict(
                self._strategy_record(candidate.strategy_id).get("config") or {}
            )
        except Exception as exc:  # noqa: BLE001 - without a restore point, do not apply
            raise LearningStateError(
                "promotion needs the current strategy configuration as a restore "
                f"point: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            applied = dict(
                self._strategy_applier(candidate.strategy_id, dict(candidate.config))
            )
        except Exception as exc:  # noqa: BLE001 - a failed apply changes nothing
            raise LearningStateError(
                f"promotion was not applied: {type(exc).__name__}: {exc}"
            ) from exc
        version = StrategyVersionRecord(
            version_id=version_id,
            strategy_id=candidate.strategy_id,
            catalog_id=candidate.catalog_id,
            parent_version=previous or candidate.baseline_version,
            config=dict(candidate.config),
            created_at=moment,
            description=str(
                body.get("description")
                or f"promoted candidate {candidate_id} from experiment {candidate.experiment_id}"
            ),
            experiment_id=candidate.experiment_id,
            candidate_id=candidate_id,
            evidence_refs=(
                *tuple(str(item) for item in body.get("evidence_refs") or ()),
                *tuple(candidate.evaluation.get("evidence_refs") or ()),
            ),
            evaluation=dict(candidate.evaluation),
            approval=dict(candidate.approval),
            promotion={
                "applied_by": str(body.get("actor") or "operator"),
                "at": moment.isoformat(),
                "previous_version": previous,
                "applier": applied,
            },
            status=VersionStatus.PROMOTED,
        )
        updated = replace(
            candidate,
            status=CandidateStatus.PROMOTED,
            promoted_version=version_id,
            updated_at=moment,
        )
        try:
            with self._store.transaction():
                self._store.save_version(version)
                self._store.set_active(candidate.strategy_id, version_id)
                self._store.save_candidate(updated)
        except Exception as exc:  # noqa: BLE001 - never leave an unrecorded promotion
            try:
                self._strategy_applier(candidate.strategy_id, before_config)
            except Exception as restore_exc:  # noqa: BLE001 - reported, not hidden
                self._last_error = (
                    "promotion record failed and the previous strategy could not "
                    f"be restored: {type(restore_exc).__name__}: {restore_exc}"
                )
            raise LearningStateError(
                "promotion could not be recorded; the previous configuration was "
                "restored"
            ) from exc
        self._publish(
            "novamarket.learning.candidate.promoted",
            {
                "candidate_id": candidate_id,
                "strategy_id": candidate.strategy_id,
                "version_id": version_id,
                "previous_version": previous,
            },
        )
        return {"ok": True, "candidate": updated.to_dict(), "version": version.to_dict()}

    def rollback(self, strategy_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        target_version_id = str(body.get("to_version") or body.get("version_id") or "")
        if not target_version_id:
            raise LearningValidationError("rollback needs to_version")
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise LearningValidationError("rollback needs a reason")
        target = self._store.version(target_version_id)
        if target is None:
            raise LearningNotFoundError(f"unknown learning version {target_version_id!r}")
        if target.strategy_id != strategy_id:
            raise LearningValidationError(
                "the target version belongs to another strategy"
            )
        if target.status is not VersionStatus.PROMOTED:
            raise LearningStateError(
                f"version {target_version_id} is {target.status.value}; "
                "only a promoted version can be a rollback target"
            )
        current = self._store.active_version(strategy_id)
        if current == target_version_id:
            raise LearningStateError("the requested version is already active")
        try:
            validate_config(target.catalog_id, target.config)
        except Exception as exc:  # noqa: BLE001
            raise LearningStateError(
                f"the target version is not a valid configuration: {exc}"
            ) from exc
        if self._strategy_applier is None:
            raise LearningStateError("no strategy applier is configured")
        try:
            before_config = dict(self._strategy_record(strategy_id).get("config") or {})
        except Exception as exc:  # noqa: BLE001 - without a restore point, do not apply
            raise LearningStateError(
                "rollback needs the current strategy configuration as a restore "
                f"point: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            applied = dict(self._strategy_applier(strategy_id, dict(target.config)))
        except Exception as exc:  # noqa: BLE001 - an incomplete rollback is refused
            raise LearningStateError(
                f"rollback was not applied: {type(exc).__name__}: {exc}"
            ) from exc
        moment = self._clock()
        rollback = RollbackRecord(
            rollback_id=learning_id(
                "lrb_", strategy_id, current, target_version_id, moment.isoformat()
            ),
            strategy_id=strategy_id,
            from_version=current,
            to_version=target_version_id,
            reason=reason,
            actor=str(body.get("actor") or "operator"),
            at=moment,
            applied=True,
        )
        promoted = next(
            (
                row
                for row in self._store.candidates(strategy_id=strategy_id, limit=500)
                if row.promoted_version == current
            ),
            None,
        )
        try:
            with self._store.transaction():
                self._store.save_rollback(rollback)
                self._store.set_active(strategy_id, target_version_id)
                if promoted is not None and promoted.status is CandidateStatus.PROMOTED:
                    self._store.save_candidate(
                        replace(
                            promoted,
                            status=CandidateStatus.ROLLED_BACK,
                            updated_at=moment,
                        )
                    )
        except Exception as exc:  # noqa: BLE001 - never leave an unrecorded rollback
            try:
                self._strategy_applier(strategy_id, before_config)
            except Exception as restore_exc:  # noqa: BLE001 - reported, not hidden
                self._last_error = (
                    "rollback record failed and the previous strategy could not "
                    f"be restored: {type(restore_exc).__name__}: {restore_exc}"
                )
            raise LearningStateError(
                "rollback could not be recorded; the previous configuration was "
                "restored"
            ) from exc
        self._publish(
            "novamarket.learning.strategy.rolled_back",
            {
                "strategy_id": strategy_id,
                "from_version": current,
                "to_version": target_version_id,
                "reason": reason,
            },
        )
        return {
            "ok": True,
            "rollback": rollback.to_dict(),
            "active_version": target_version_id,
            "applier": applied,
        }

    def versions(self, strategy_id: str) -> dict[str, Any]:
        rows = self._store.versions(strategy_id)
        active = self._store.active_version(strategy_id)
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "active_version": active,
            "versions": [
                {**row.to_dict(), "active": row.version_id == active} for row in rows
            ],
            "total": len(rows),
        }

    def active_version(self, strategy_id: str) -> dict[str, Any]:
        version_id = self._store.active_version(strategy_id)
        if not version_id:
            return {
                "ok": True,
                "strategy_id": strategy_id,
                "active_version": "",
                "version": None,
            }
        row = self._store.version(version_id)
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "active_version": version_id,
            "version": None if row is None else row.to_dict(),
        }

    def rollbacks(self, strategy_id: str = "", limit: int = 50) -> dict[str, Any]:
        rows = self._store.rollbacks(strategy_id=strategy_id, limit=limit)
        return {"ok": True, "rollbacks": [row.to_dict() for row in rows], "total": len(rows)}

    # -- recovery and shutdown ------------------------------------------------
    def recover(self) -> dict[str, Any]:
        recovered = 0
        failed = 0
        for experiment in self._store.experiments(limit=500):
            if experiment.status in (
                ExperimentStatus.EVALUATING,
                ExperimentStatus.QUEUED,
            ):
                updated = replace(
                    experiment,
                    status=ExperimentStatus.FAILED,
                    error="application restarted during evaluation; start a new run",
                    completed_at=self._clock(),
                )
                self._store.save_experiment(updated)
                failed += 1
            recovered += 1
        for candidate in self._store.candidates(limit=2000):
            if candidate.status is CandidateStatus.EVALUATING:
                self._store.save_candidate(
                    replace(
                        candidate,
                        status=CandidateStatus.EVALUATION_FAILED,
                        reasons=(*candidate.reasons, "application restarted during evaluation"),
                        updated_at=self._clock(),
                    )
                )
        return {"ok": True, "experiments": recovered, "failed": failed}

    async def close(self) -> None:
        for task in list(self._jobs.values()):
            if not task.done():
                task.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        self._jobs.clear()
        self._store.close()

    # -- internals ------------------------------------------------------------
    def _publish(self, name: str, payload: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        try:
            result = self._publisher(name, payload)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    result.close()
                else:
                    loop.create_task(result)
        except Exception as exc:  # noqa: BLE001 - committed state stands
            self._last_error = f"{name}: {type(exc).__name__}: {exc}"

    def _load_experiment(self, experiment_id: str) -> LearningExperiment:
        row = self._store.experiment(experiment_id)
        if row is None:
            raise LearningNotFoundError(f"unknown learning experiment {experiment_id!r}")
        return row

    def _load_candidate(self, candidate_id: str) -> LearningCandidate:
        row = self._store.candidate(candidate_id)
        if row is None:
            raise LearningNotFoundError(f"unknown learning candidate {candidate_id!r}")
        return row

    def _load_version(self, version_id: str) -> StrategyVersionRecord:
        row = self._store.version(version_id)
        if row is None:
            raise LearningNotFoundError(f"unknown learning version {version_id!r}")
        return row

    def _strategy_record(self, strategy_id: str) -> Mapping[str, Any]:
        if self._strategy_loader is None:
            raise LearningStateError("strategy resolution is unavailable")
        record = self._strategy_loader(strategy_id)
        if not isinstance(record, Mapping):
            raise LearningValidationError(f"strategy {strategy_id!r} is not an object")
        body = record.get("strategy")
        return body if isinstance(body, Mapping) else record

    def _approval_policy(self, value: object) -> dict[str, Any]:
        body = dict(value) if isinstance(value, Mapping) else {}
        mode = str(body.get("mode") or "manual").lower()
        if mode not in ("manual", "auto"):
            raise LearningValidationError("approval_policy.mode must be manual or auto")
        auto_approve = bool(body.get("auto_approve", False))
        if mode == "auto" and not bool(body.get("auto_approve", False)):
            raise LearningValidationError(
                "automatic approval must be explicitly enabled with auto_approve=true"
            )
        return {
            "mode": mode,
            "auto_approve": auto_approve,
            "auto_promote": bool(body.get("auto_promote", False)) and mode == "auto",
            "require_shadow": bool(body.get("require_shadow", False)),
            "min_validation_return": float(body.get("min_validation_return", 0.002)),
            "actor": str(body.get("actor") or "policy:auto"),
        }

    def _auto_approval_allowed(self, experiment: LearningExperiment) -> bool:
        return (
            experiment.approval_policy.get("mode") == "auto"
            and bool(experiment.approval_policy.get("auto_approve"))
        )

    def _active_versions(self) -> dict[str, str]:
        return self._store.active_rows()

    def _evidence_summary(self) -> dict[str, Any]:
        rows = self._store.evidence_rows(limit=500)
        by_kind: Counter[str] = Counter()
        by_status: Counter[str] = Counter()
        for row in rows:
            by_kind[row.kind.value] += 1
            by_status[row.status.value] += 1
        return {"by_kind": dict(by_kind), "by_status": dict(by_status), "total": len(rows)}

    def _promotions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for strategy_id in self._store.strategy_ids():
            for version in self._store.versions(strategy_id):
                if version.status is VersionStatus.PROMOTED:
                    rows.append(version.to_dict())
        return rows[:50]

    def _import_source(self, kind: EvidenceKind, body: dict[str, Any]) -> dict[str, Any] | None:
        source_ref = str(body.get("source_ref") or "")
        if not source_ref:
            return None
        loader = {
            EvidenceKind.BACKTEST: self._backtest_loader,
            EvidenceKind.PAPER: self._paper_loader,
            EvidenceKind.SHADOW: self._shadow_loader,
        }[kind]
        if loader is None:
            raise LearningStateError(
                f"no {kind.value} evidence loader is configured"
            )
        try:
            source = dict(loader(source_ref))
        except Exception as exc:  # noqa: BLE001
            raise LearningStateError(
                f"could not load {kind.value} evidence {source_ref!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if kind is EvidenceKind.BACKTEST:
            config = dict(source.get("config") or {})
            metrics = dict(source.get("metrics") or {})
            observations = int(metrics.get("observations") or 0)
            trades = [
                row for row in source.get("trades") or () if isinstance(row, Mapping)
            ]
            by_instrument = self._instrument_rollup(
                trades,
                pnl_key="realized_pnl",
                fees_key="fees",
                slippage_key="slippage",
            )
            if by_instrument:
                metrics["by_instrument"] = by_instrument
            instruments = [
                str(item)
                for item in (
                    body.get("instruments")
                    or config.get("instruments")
                    or list(by_instrument)
                )
            ]
            regimes = [
                str(item)
                for item in (
                    body.get("regimes")
                    or source.get("regimes")
                    or self._market_regimes(
                        instruments,
                        optional_time(config.get("start")),
                        optional_time(config.get("end")),
                    )
                )
            ]
            return {
                **body,
                "instruments": instruments,
                "strategy_id": body.get("strategy_id") or config.get("strategy_id"),
                "strategy_version": body.get("strategy_version")
                or config.get("strategy_version"),
                "catalog_id": body.get("catalog_id")
                or config.get("strategy_catalog_id"),
                "config": body.get("config") or config.get("strategy_config") or {},
                "window_start": config.get("start"),
                "window_end": config.get("end"),
                "observations": observations,
                "metrics": metrics,
                "out_of_sample": bool(body.get("out_of_sample")),
                "regimes": regimes,
                "provenance": {
                    "source": "NM-11",
                    "source_ref": source_ref,
                    "repro_hash": source.get("repro_hash"),
                },
            }
        if kind is EvidenceKind.PAPER:
            fills = list(source.get("fills") or ())
            orders = list(source.get("orders") or ())
            pnl = 0.0
            fees = 0.0
            slippage = 0.0
            wins = 0
            losses = 0
            realized_seen = False
            for row in fills:
                effect = row.get("position_effect") or {}
                value = self._float(
                    effect.get("realized_delta")
                    if isinstance(effect, Mapping)
                    else None
                )
                if value is None:
                    value = self._float(row.get("realized_pnl"))
                if value is not None:
                    realized_seen = True
                    pnl += value
                    wins += 1 if value > 0 else 0
                    losses += 1 if value < 0 else 0
                fees += self._float(row.get("total_fees")) or 0.0
                slippage += self._float(row.get("slippage_amount")) or 0.0
            initial = float(self._initial_capital)
            by_instrument = self._instrument_rollup(
                fills,
                pnl_key="realized_pnl",
                fees_key="total_fees",
                slippage_key="slippage_amount",
            )
            instruments = [
                str(item)
                for item in (
                    body.get("instruments")
                    or source.get("instruments")
                    or list(by_instrument)
                )
            ]
            window_start = optional_time(body.get("window_start") or source.get("window_start"))
            window_end = optional_time(body.get("window_end") or source.get("window_end"))
            regimes = [
                str(item)
                for item in (
                    body.get("regimes")
                    or source.get("regimes")
                    or self._market_regimes(instruments, window_start, window_end)
                )
            ]
            metrics = {
                "return_pct": (
                    (pnl / initial) * 100.0 if initial and realized_seen else None
                ),
                "win_rate_pct": (
                    (wins / (wins + losses) * 100.0)
                    if realized_seen and wins + losses
                    else None
                ),
                "trades": len(fills),
                "fees": fees,
                "slippage": slippage,
                "realized_pnl": pnl if realized_seen else None,
                "by_instrument": by_instrument,
            }
            return {
                **body,
                "instruments": instruments,
                "strategy_id": body.get("strategy_id")
                or source.get("strategy_id"),
                "strategy_version": body.get("strategy_version")
                or source.get("strategy_version"),
                "catalog_id": body.get("catalog_id") or source.get("catalog_id"),
                "config": body.get("config") or source.get("config") or {},
                "window_start": body.get("window_start")
                or source.get("window_start"),
                "window_end": body.get("window_end") or source.get("window_end"),
                "observations": len(fills) or len(orders),
                "regimes": regimes,
                "metrics": metrics,
                "out_of_sample": True,
                "provenance": {
                    "source": "NM-10",
                    "source_ref": source_ref,
                    "simulated": True,
                },
            }
        analytics = dict(source.get("analytics") or source)
        outcomes = dict(analytics.get("outcomes") or {})
        decisions = dict(analytics.get("decisions") or {})
        by_instrument = analytics.get("by_instrument") or {}
        instruments = [
            str(item)
            for item in (
                body.get("instruments")
                or source.get("instruments")
                or list(by_instrument)
            )
        ]
        regimes = [
            str(item)
            for item in (
                body.get("regimes")
                or source.get("regimes")
                or self._market_regimes(
                    instruments,
                    optional_time(body.get("window_start") or source.get("started_at")),
                    optional_time(body.get("window_end") or source.get("completed_at")),
                )
            )
        ]
        return {
            **body,
            "instruments": instruments,
            "strategy_id": body.get("strategy_id") or source.get("strategy_id"),
            "strategy_version": body.get("strategy_version")
            or source.get("strategy_version"),
            "catalog_id": body.get("catalog_id") or source.get("catalog_id"),
            "config": body.get("config") or source.get("config") or {},
            "window_start": body.get("window_start") or source.get("started_at"),
            "window_end": body.get("window_end") or source.get("completed_at"),
            "observations": int(decisions.get("total") or 0),
            "regimes": regimes,
            "metrics": {
                "return_pct": outcomes.get("mean_return_pct"),
                "win_rate_pct": outcomes.get("win_rate_pct"),
                "trades": outcomes.get("resolved", 0),
                "decisions": decisions.get("total", 0),
                "risk_approved": (analytics.get("risk") or {}).get("approved", 0),
                "risk_rejected": (analytics.get("risk") or {}).get("rejected", 0),
                "by_instrument": by_instrument,
                "by_horizon": analytics.get("by_horizon") or {},
            },
            "out_of_sample": True,
            "provenance": {
                "source": "NM-12",
                "source_ref": source_ref,
                "hypothetical": True,
            },
        }

    def _eligibility(self, kind: EvidenceKind, body: Mapping[str, Any]) -> EvidenceRecord:
        now = self._clock()
        strategy_id = str(body.get("strategy_id") or "").strip()
        version = str(body.get("strategy_version") or "").strip()
        catalog_id = str(body.get("catalog_id") or "").strip()
        config = dict(body.get("config") or {})
        try:
            observations = int(body.get("observations") or 0)
        except (TypeError, ValueError) as exc:
            raise LearningValidationError(
                "evidence observations must be an integer"
            ) from exc
        metrics = dict(body.get("metrics") or {})
        reasons: list[str] = []
        status = EvidenceStatus.ACCEPTED
        confidence = "0.90"
        try:
            window_start = optional_time(body.get("window_start"))
            window_end = optional_time(body.get("window_end"))
        except ValueError as exc:
            raise LearningValidationError(
                f"evidence timestamps must be valid timezone-aware ISO values: {exc}"
            ) from exc
        if not strategy_id:
            reasons.append("strategy_id is missing")
        if not version:
            reasons.append("strategy_version is missing")
        if window_start is not None and window_end is not None and window_start > window_end:
            reasons.append("window_start is after window_end")
        if observations < self._min_sample_size:
            reasons.append(
                f"{observations} observation(s); at least {self._min_sample_size} are required"
            )
        instruments = {
            str(item).strip()
            for item in body.get("instruments") or ()
            if str(item).strip()
        }
        by_instrument = metrics.get("by_instrument")
        if isinstance(by_instrument, Mapping):
            instruments.update(
                str(item).strip() for item in by_instrument if str(item).strip()
            )
        regimes = {
            str(item).strip()
            for item in body.get("regimes") or ()
            if str(item).strip()
        }
        if len(instruments) < self._min_instruments:
            reasons.append(
                f"instrument coverage {len(instruments)}; at least "
                f"{self._min_instruments} required"
            )
        if len(regimes) < self._min_regimes:
            reasons.append(
                f"regime coverage {len(regimes)}; at least "
                f"{self._min_regimes} required"
            )
        has_outcome = False
        for key in ("return_pct", "total_return", "mean_return_pct", "sharpe"):
            value = metrics.get(key)
            if value in (None, ""):
                continue
            try:
                float(value)
            except (TypeError, ValueError):
                continue
            has_outcome = True
            break
        if not has_outcome:
            reasons.append("no usable return or risk-adjusted outcome is present")
        if window_end is not None and window_end > now:
            reasons.append("window_end is in the future; future information would leak")
        if (
            window_end is not None
            and (now - window_end).days > self._evidence_max_age_days
        ):
            reasons.append(
                f"evidence is older than {self._evidence_max_age_days} days"
            )
        if reasons:
            status = EvidenceStatus.REJECTED
            confidence = "0"
        elif bool(body.get("in_sample")) and not bool(body.get("out_of_sample")):
            status = EvidenceStatus.LOW_CONFIDENCE
            confidence = "0.50"
            reasons.append("in-sample only; it cannot be the sole promotion evidence")
        elif observations < self._min_sample_size * 2:
            status = EvidenceStatus.LOW_CONFIDENCE
            confidence = "0.65"
            reasons.append("small sample; confidence is reduced")
        provenance = {
            "source": kind.value,
            "reliability": (
                "historical simulation"
                if kind is EvidenceKind.BACKTEST
                else "simulated paper execution"
                if kind is EvidenceKind.PAPER
                else "hypothetical shadow observation"
            ),
            "in_sample": bool(body.get("in_sample")),
            "out_of_sample": bool(body.get("out_of_sample")),
            "strategy_version": version,
            "config_digest": digest(config),
            "received_at": now.isoformat(),
            **dict(body.get("provenance") or {}),
        }
        evidence_digest = digest(
            {
                "kind": kind.value,
                "source_ref": str(body.get("source_ref") or ""),
                "strategy_id": strategy_id,
                "strategy_version": version,
                "config": config,
                "window_start": window_start.isoformat() if window_start else None,
                "window_end": window_end.isoformat() if window_end else None,
                "metrics": metrics,
            }
        )
        existing = self._store.evidence_by_digest(evidence_digest)
        evidence_id = (
            existing.evidence_id
            if existing is not None
            else learning_id(
                "lev_", kind.value, strategy_id, version, evidence_digest
            )
        )
        return EvidenceRecord(
            evidence_id=evidence_id,
            kind=kind,
            source_ref=str(body.get("source_ref") or ""),
            strategy_id=strategy_id,
            strategy_version=version,
            catalog_id=catalog_id,
            config=config,
            window_start=window_start,
            window_end=window_end,
            observations=observations,
            instruments=tuple(sorted(instruments)),
            metrics=metrics,
            in_sample=bool(body.get("in_sample")),
            out_of_sample=bool(body.get("out_of_sample")),
            regimes=tuple(sorted(regimes)),
            received_at=now,
            status=status,
            confidence=confidence,
            reasons=tuple(reasons),
            provenance=provenance,
            evidence_digest=evidence_digest,
            duplicate=existing is not None,
        )

    async def _run_job(self, experiment_id: str) -> None:
        async with self._semaphore:
            try:
                await self._evaluate_all(experiment_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the experiment records its failure
                self._last_error = f"{type(exc).__name__}: {exc}"
                experiment = self._store.experiment(experiment_id)
                if experiment is not None:
                    self._store.save_experiment(
                        replace(
                            experiment,
                            status=ExperimentStatus.FAILED,
                            error=self._last_error,
                            completed_at=self._clock(),
                        )
                    )
                self._publish(
                    "novamarket.learning.experiment.failed",
                    {"experiment_id": experiment_id, "error": self._last_error},
                )
            finally:
                self._jobs.pop(experiment_id, None)

    async def _evaluate_all(self, experiment_id: str) -> dict[str, Any]:
        experiment = self._load_experiment(experiment_id)
        ensure_experiment_transition(experiment.status, ExperimentStatus.EVALUATING)
        experiment = replace(
            experiment,
            status=ExperimentStatus.EVALUATING,
            started_at=self._clock(),
            progress=0,
        )
        self._store.save_experiment(experiment)
        self._publish(
            "novamarket.learning.experiment.started",
            {"experiment_id": experiment_id, "strategy_id": experiment.strategy_id},
        )
        total = len(experiment.candidate_ids)
        evaluated = 0
        eligible = 0
        rejected = 0
        failed = 0
        best: LearningCandidate | None = None
        for index, candidate_id in enumerate(experiment.candidate_ids, start=1):
            if experiment_id in self._cancel_requested:
                cancelled = replace(
                    self._load_experiment(experiment_id),
                    status=ExperimentStatus.CANCELLED,
                    cancelled_at=self._clock(),
                )
                self._store.save_experiment(cancelled)
                self._cancel_requested.discard(experiment_id)
                self._publish(
                    "novamarket.learning.experiment.cancelled",
                    {"experiment_id": experiment_id},
                )
                return {"ok": True, "experiment": cancelled.to_dict(), "cancelled": True}
            candidate = self._load_candidate(candidate_id)
            if candidate.status is not CandidateStatus.GENERATED:
                continue
            candidate = replace(
                candidate,
                status=CandidateStatus.EVALUATING,
                updated_at=self._clock(),
            )
            self._store.save_candidate(candidate)
            evaluated_row = await self._evaluate_one(experiment, candidate)
            evaluated += 1
            if evaluated_row.status is CandidateStatus.PENDING_APPROVAL:
                eligible += 1
                if best is None or self._score(evaluated_row) > self._score(best):
                    best = evaluated_row
            elif evaluated_row.status is CandidateStatus.REJECTED:
                rejected += 1
            else:
                failed += 1
            progress = int(index / max(1, total) * 100)
            current = self._load_experiment(experiment_id)
            self._store.save_experiment(replace(current, progress=progress))
        current = self._load_experiment(experiment_id)
        if experiment_id in self._cancel_requested or current.status is ExperimentStatus.CANCELLED:
            self._cancel_requested.discard(experiment_id)
            if current.status is not ExperimentStatus.CANCELLED:
                current = replace(
                    current,
                    status=ExperimentStatus.CANCELLED,
                    cancelled_at=self._clock(),
                    error="cancelled during evaluation",
                )
                self._store.save_experiment(current)
                self._publish(
                    "novamarket.learning.experiment.cancelled",
                    {"experiment_id": experiment_id},
                )
            return {"ok": True, "experiment": current.to_dict(), "cancelled": True}
        completed = replace(
            current,
            status=ExperimentStatus.COMPLETED,
            progress=100,
            completed_at=self._clock(),
            results={
                "evaluated": evaluated,
                "eligible": eligible,
                "rejected": rejected,
                "failed": failed,
                "best_candidate": "" if best is None else best.candidate_id,
                "best_score": None if best is None else self._score(best),
                "out_of_sample": True,
            },
        )
        self._store.save_experiment(completed)
        self._publish(
            "novamarket.learning.experiment.completed",
            {
                "experiment_id": experiment_id,
                "evaluated": evaluated,
                "eligible": eligible,
                "rejected": rejected,
            },
        )
        if best is not None and self._auto_approval_allowed(completed):
            # Shadow validation must happen before approval: approve_candidate
            # refuses a policy that requires a passing validation.  A missing
            # NM-12 service leaves the candidate pending for a human instead of
            # failing an already-completed experiment.
            if completed.approval_policy.get("require_shadow"):
                try:
                    validation = await self.shadow_validate_candidate(
                        best.candidate_id, {"observations": {}, "min_decisions": 1}
                    )
                except LearningError as exc:
                    self._last_error = (
                        "automatic shadow validation was unavailable: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return {
                        "ok": True,
                        "experiment": completed.to_dict(),
                        "automatic_approval": "skipped",
                        "reason": self._last_error,
                    }
                best = self._load_candidate(best.candidate_id)
                if (
                    validation["validation"]["status"]
                    != ShadowValidationStatus.PASSED.value
                ):
                    return {
                        "ok": True,
                        "experiment": completed.to_dict(),
                        "automatic_approval": "skipped",
                        "reason": "shadow validation did not pass",
                    }
            try:
                approved = self.approve_candidate(
                    best.candidate_id,
                    {
                        "actor": "policy:auto",
                        "automatic": True,
                        "note": "policy-approved",
                    },
                )
            except LearningError as exc:
                self._last_error = (
                    "automatic approval was refused: "
                    f"{type(exc).__name__}: {exc}"
                )
                return {
                    "ok": True,
                    "experiment": completed.to_dict(),
                    "automatic_approval": "skipped",
                    "reason": self._last_error,
                }
            approved_candidate = self._load_candidate(
                approved["candidate"]["candidate_id"]
            )
            if completed.approval_policy.get("auto_promote"):
                try:
                    self.promote_candidate(
                        approved_candidate.candidate_id, {"actor": "policy:auto"}
                    )
                except LearningError as exc:
                    self._last_error = (
                        "automatic promotion was refused: "
                        f"{type(exc).__name__}: {exc}"
                    )
        return {"ok": True, "experiment": completed.to_dict()}

    async def _evaluate_one(
        self,
        experiment: LearningExperiment,
        candidate: LearningCandidate,
    ) -> LearningCandidate:
        result = await asyncio.to_thread(self._evaluate_sync, experiment, candidate)
        if not result.get("ok"):
            status = CandidateStatus.EVALUATION_FAILED
        elif result.get("eligible"):
            status = CandidateStatus.PENDING_APPROVAL
        else:
            status = CandidateStatus.REJECTED
        updated = replace(
            candidate,
            status=status,
            evaluation=result,
            risk=dict(result.get("risk") or {}),
            policy={
                "approval": dict(experiment.approval_policy),
                "risk_policy_version": (result.get("risk") or {}).get("policy_version", ""),
            },
            reasons=tuple(str(item) for item in result.get("reasons") or ()),
            updated_at=self._clock(),
        )
        self._store.save_candidate(updated)
        if status is CandidateStatus.REJECTED:
            self._publish(
                "novamarket.learning.candidate.rejected",
                {
                    "candidate_id": updated.candidate_id,
                    "experiment_id": updated.experiment_id,
                    "reasons": list(updated.reasons),
                },
            )
        else:
            self._publish(
                "novamarket.learning.candidate.evaluated",
                {
                    "candidate_id": updated.candidate_id,
                    "experiment_id": updated.experiment_id,
                    "status": status.value,
                    "eligible": status is CandidateStatus.PENDING_APPROVAL,
                },
            )
        return updated

    def _evaluate_sync(
        self, experiment: LearningExperiment, candidate: LearningCandidate
    ) -> dict[str, Any]:
        """Run one evaluation on a thread-private NM-11 engine.

        SQLite connections are thread-bound; a shared engine would either fail
        from the worker thread or serialise every experiment behind one lock.
        """
        store = BacktestStore(":memory:")
        engine = BacktestEngine(store, clock=self._clock, max_concurrency=1)
        started_cpu = time.process_time()
        tracing = self._max_memory_mb > 0
        already_tracing = tracemalloc.is_tracing()
        if tracing and not already_tracing:
            tracemalloc.start()
            tracemalloc.reset_peak()
        try:
            bars = self._load_bars(experiment)
            policy = self._policy(
                str(experiment.approval_policy.get("risk_policy_version") or "")
            )
            result = experiments.evaluate_candidate(
                candidate_config=candidate.config,
                baseline_config=experiment.baseline_config,
                catalog_id=experiment.catalog_id,
                strategy_id=experiment.strategy_id,
                baseline_version=experiment.baseline_version,
                instruments=experiment.instruments,
                train_start=experiment.train_start,
                train_end=experiment.train_end,
                validation_start=experiment.validation_start,
                validation_end=experiment.validation_end,
                bars=bars,
                engine=engine,
                policy=policy,
                risk_policy_snapshot=self._risk_snapshot(policy),
                min_trades=experiment.min_trades,
                min_observations=experiment.min_observations,
                initial_capital=self._initial_capital,
            )
            self._enforce_resource_limits(started_cpu, candidate.candidate_id)
            return result
        finally:
            if tracing and not already_tracing:
                tracemalloc.stop()
            store.close()

    def _enforce_resource_limits(self, started_cpu: float, candidate_id: str) -> None:
        """Fail a candidate that exceeds the declared CPU or memory ceiling.

        CPU is the worker's process time for this evaluation. Memory is the
        Python allocation peak observed by ``tracemalloc`` while the replay runs.
        Both are explicit ceilings reported by ``status()``; a run that crosses
        one is failed and never silently accepted.
        """
        if self._max_cpu_seconds > 0:
            elapsed = time.process_time() - started_cpu
            if elapsed > self._max_cpu_seconds:
                raise LearningStateError(
                    f"candidate {candidate_id} exceeded the CPU ceiling "
                    f"({elapsed:.3f}s > {self._max_cpu_seconds:.3f}s)"
                )
        if self._max_memory_mb > 0:
            _current, peak = tracemalloc.get_traced_memory()
            ceiling = int(self._max_memory_mb * 1024 * 1024)
            if peak > ceiling:
                raise LearningStateError(
                    f"candidate {candidate_id} exceeded the memory ceiling "
                    f"({peak / (1024 * 1024):.1f} MiB > {self._max_memory_mb:.1f} MiB)"
                )

    def _score(self, candidate: LearningCandidate) -> float:
        value = candidate.evaluation.get("score")
        return float(value) if isinstance(value, (int, float)) else float("-inf")

    def _load_bars(self, experiment: LearningExperiment) -> list[HistoricalBar]:
        if self._market_data is None:
            raise LearningStateError("NM-02 market data is unavailable")
        rows: list[HistoricalBar] = []
        for instrument in experiment.instruments:
            offset = 0
            total = 1
            while offset < total:
                page = self._market_data.bars(
                    instrument,
                    interval="1d",
                    start=experiment.train_start,
                    end=experiment.validation_end,
                    limit=1000,
                    offset=offset,
                    ascending=True,
                )
                items = list(getattr(page, "items", page) or ())
                total = int(getattr(page, "total", len(items)) or len(items))
                for item in items:
                    rows.append(self._historical_bar(item, instrument))
                if not items:
                    break
                offset += len(items)
        return rows

    @staticmethod
    def _historical_bar(item: Any, instrument: str) -> HistoricalBar:
        close = getattr(item, "close", None)
        observed = getattr(item, "observed_at", None)
        available = getattr(item, "available_at", None)
        return HistoricalBar.from_mapping(
            {
                "instrument_id": str(getattr(item, "instrument_id", instrument)),
                "observed_at": observed.isoformat()
                if isinstance(observed, datetime)
                else observed,
                "available_at": available.isoformat()
                if isinstance(available, datetime)
                else available,
                "open": getattr(item, "open", close),
                "high": getattr(item, "high", close),
                "low": getattr(item, "low", close),
                "close": close,
                "volume": getattr(item, "volume", 0),
                "source": getattr(item, "provider", "nm02"),
                "partial": bool(getattr(item, "partial", False)),
            }
        )

    def _policy(self, version: str) -> RiskPolicy:
        payload: Any = None
        if self._risk_policy_loader is not None:
            payload = self._risk_policy_loader(version)
        if isinstance(payload, RiskPolicy):
            return payload
        if payload is None:
            return POLICY_DEFAULTS["medium_term"]
        if not isinstance(payload, Mapping):
            raise LearningValidationError("a risk policy must be an object")
        candidate = payload.get("policy")
        body: Mapping[str, Any] = (
            candidate if isinstance(candidate, Mapping) else payload
        )
        base = POLICY_DEFAULTS["medium_term"]
        updates: dict[str, Any] = {}
        for name in base.__dataclass_fields__:
            if name in body:
                updates[name] = body[name]
        for name in _DECIMAL_FIELDS:
            if name in updates:
                updates[name] = decimal(updates[name], name=name)
        try:
            return replace(base, **updates)
        except (TypeError, ValueError) as exc:
            raise LearningValidationError(f"invalid risk policy: {exc}") from exc

    def _risk_snapshot(self, policy: RiskPolicy) -> dict[str, Any]:
        return {
            "max_position_weight": str(policy.max_position_weight),
            "max_invested_fraction": str(policy.max_invested_fraction),
            "max_gross_exposure": str(policy.max_invested_fraction),
            "min_cash_fraction": str(policy.min_cash_fraction),
            "max_drawdown_pct": str(policy.max_drawdown_pct),
            "restricted_instruments": list(policy.restricted_instruments),
        }

    async def _shadow_validate(
        self,
        experiment: LearningExperiment,
        candidate: LearningCandidate,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._shadow_service is None:
            raise LearningStateError("NM-12 shadow validation is unavailable")
        observations = dict(body.get("observations") or {})
        cycles = int(body.get("cycles") or 1)
        minimum = max(1, int(body.get("min_decisions") or self._min_sample_size))
        candidate_session = await self._shadow_session(
            experiment,
            candidate,
            candidate.config,
            f"candidate {candidate.candidate_id}",
            observations,
            cycles,
        )
        baseline_session = await self._shadow_session(
            experiment,
            candidate,
            experiment.baseline_config,
            f"baseline {candidate.candidate_id}",
            observations,
            cycles,
        )
        candidate_analytics = candidate_session["analytics"]
        baseline_analytics = baseline_session["analytics"]
        candidate_decisions = int(
            (candidate_analytics.get("decisions") or {}).get("total") or 0
        )
        baseline_decisions = int(
            (baseline_analytics.get("decisions") or {}).get("total") or 0
        )
        candidate_outcomes = dict(candidate_analytics.get("outcomes") or {})
        baseline_outcomes = dict(baseline_analytics.get("outcomes") or {})
        reasons: list[str] = []
        status = ShadowValidationStatus.PASSED
        if candidate_decisions < minimum or baseline_decisions < minimum:
            status = ShadowValidationStatus.INSUFFICIENT_EVIDENCE
            reasons.append(
                f"candidate {candidate_decisions} / baseline {baseline_decisions} "
                f"decision(s); {minimum} are required"
            )
        if candidate_decisions and baseline_decisions and status is ShadowValidationStatus.PASSED:
            candidate_return = self._float(candidate_outcomes.get("mean_return_pct"))
            baseline_return = self._float(baseline_outcomes.get("mean_return_pct"))
            tolerance = float(body.get("tolerance_pct") or 1.0)
            if (
                candidate_return is not None
                and baseline_return is not None
                and candidate_return < baseline_return - tolerance
            ):
                status = ShadowValidationStatus.FAILED
                reasons.append(
                    f"candidate shadow return {candidate_return:.2f}% trails "
                    f"baseline {baseline_return:.2f}% by more than {tolerance:.2f}pp"
                )
            candidate_risk = candidate_analytics.get("risk") or {}
            baseline_risk = baseline_analytics.get("risk") or {}
            candidate_total = int(candidate_risk.get("approved", 0)) + int(
                candidate_risk.get("rejected", 0)
            )
            baseline_total = int(baseline_risk.get("approved", 0)) + int(
                baseline_risk.get("rejected", 0)
            )
            if candidate_total and baseline_total:
                candidate_ratio = int(candidate_risk.get("approved", 0)) / candidate_total
                baseline_ratio = int(baseline_risk.get("approved", 0)) / baseline_total
                if candidate_ratio < baseline_ratio - 0.15:
                    status = ShadowValidationStatus.FAILED
                    reasons.append(
                        f"candidate risk approval {candidate_ratio:.2f} trails "
                        f"baseline {baseline_ratio:.2f}"
                    )
        result = {
            "status": status.value,
            "candidate_session_id": candidate_session["session_id"],
            "baseline_session_id": baseline_session["session_id"],
            "candidate_decisions": candidate_decisions,
            "baseline_decisions": baseline_decisions,
            "candidate_return_pct": candidate_outcomes.get("mean_return_pct"),
            "baseline_return_pct": baseline_outcomes.get("mean_return_pct"),
            "candidate_risk_approved": (candidate_analytics.get("risk") or {}).get("approved", 0),
            "baseline_risk_approved": (baseline_analytics.get("risk") or {}).get("approved", 0),
            "minimum_decisions": minimum,
            "reasons": reasons,
            "out_of_sample": True,
            "hypothetical": True,
            "executes_trades": False,
        }
        if status is ShadowValidationStatus.PASSED:
            try:
                self.record_evidence(
                    {
                        "kind": "shadow",
                        "source_ref": candidate_session["session_id"],
                        "strategy_id": candidate.strategy_id,
                        "strategy_version": f"shadow-{candidate.config_digest[:8]}",
                        "catalog_id": candidate.catalog_id,
                        "config": candidate.config,
                        "window_start": candidate_session["started_at"],
                        "window_end": candidate_session["completed_at"],
                        "observations": candidate_decisions,
                        "metrics": {
                            "return_pct": candidate_outcomes.get("mean_return_pct"),
                            "win_rate_pct": candidate_outcomes.get("win_rate_pct"),
                            "trades": candidate_outcomes.get("resolved", 0),
                            "by_instrument": candidate_analytics.get("by_instrument") or {},
                        },
                        "instruments": list(experiment.instruments),
                        "regimes": self._market_regimes(
                            list(experiment.instruments),
                            optional_time(candidate_session.get("started_at")),
                            optional_time(candidate_session.get("completed_at")),
                        ),
                        "out_of_sample": True,
                        "provenance": {"validation": "NM-12", "candidate": candidate.candidate_id},
                    }
                )
            except LearningError:
                pass
        return result

    async def _shadow_session(
        self,
        experiment: LearningExperiment,
        candidate: LearningCandidate,
        config: Mapping[str, Any],
        label: str,
        observations: Mapping[str, Any],
        cycles: int,
    ) -> dict[str, Any]:
        shadow = self._shadow_service
        if shadow is None:
            raise LearningStateError("NM-12 shadow validation is unavailable")
        created = await shadow.create(
            {
                "name": f"NM-13 {label}",
                "instruments": list(experiment.instruments),
                "strategies": [
                    {
                        "strategy_id": f"learning_{candidate.candidate_id}_{label}",
                        "catalog_id": experiment.catalog_id,
                        "version": f"lrn-{candidate.config_digest[:8]}",
                        "config": dict(config),
                        "horizons": ["short_term"],
                    }
                ],
                "baseline_snapshot": dict(experiment.baseline_snapshot),
                "auto_monitor": False,
                "observation_intervals": [1],
                "metadata": {"learning_candidate": candidate.candidate_id},
            }
        )
        session = created["session"]
        session_id = session["session_id"]
        await shadow.start(session_id)
        for _ in range(max(1, cycles)):
            await shadow.run_cycle(session_id, observations=observations)
        analytics = shadow.analytics(session_id)
        history = shadow.history(session_id) if hasattr(shadow, "history") else {}
        completed = await shadow.complete(session_id)
        return {
            "session_id": session_id,
            "analytics": analytics,
            "started_at": (history.get("timestamps") or {}).get("started_at")
            or self._clock().isoformat(),
            "completed_at": (history.get("timestamps") or {}).get("completed_at")
            or self._clock().isoformat(),
            "status": completed.get("session", {}).get("status", "completed"),
        }

    def _raise_alert(
        self,
        *,
        strategy_id: str,
        kind: AlertKind,
        severity: AlertSeverity,
        message: str,
        metrics: dict[str, Any],
        evidence_refs: Sequence[str],
    ) -> LearningAlert:
        moment = self._clock()
        alert_id = learning_id(
            "lea_", strategy_id, kind.value, moment.date().isoformat(), message
        )
        existing = self._store.alert(alert_id)
        if existing is not None:
            return existing
        alert = LearningAlert(
            alert_id=alert_id,
            strategy_id=strategy_id,
            kind=kind,
            severity=severity,
            message=message,
            metrics=dict(metrics),
            evidence_refs=tuple(evidence_refs),
            status="open",
            created_at=moment,
        )
        self._store.save_alert(alert)
        self._publish(
            "novamarket.learning.alert.raised",
            {
                "alert_id": alert_id,
                "strategy_id": strategy_id,
                "kind": kind.value,
                "severity": severity.value,
            },
        )
        return alert

    @staticmethod
    def _float(value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    def _instrument_rollup(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        pnl_key: str,
        fees_key: str,
        slippage_key: str,
    ) -> dict[str, dict[str, Any]]:
        """Aggregate fills or trades into a per-instrument attribution.

        The importers previously kept only an aggregate realized P&L, which hid
        which instrument produced it.  This rollup reads the same records and
        exposes the P&L, costs and win/loss count per instrument.  It does not
        invent a price or a return for an instrument without a realized fill.
        """
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            instrument = str(row.get("instrument_id") or "").strip()
            if not instrument:
                continue
            bucket = buckets.setdefault(
                instrument,
                {
                    "trades": 0,
                    "realized_pnl": 0.0,
                    "fees": 0.0,
                    "slippage": 0.0,
                    "wins": 0,
                    "losses": 0,
                },
            )
            bucket["trades"] += 1
            pnl = None
            effect = row.get("position_effect")
            if isinstance(effect, Mapping):
                pnl = self._float(effect.get("realized_delta"))
            if pnl is None:
                pnl = self._float(row.get(pnl_key))
            if pnl is not None:
                bucket["realized_pnl"] += pnl
                bucket["wins"] += 1 if pnl > 0 else 0
                bucket["losses"] += 1 if pnl < 0 else 0
            bucket["fees"] += self._float(row.get(fees_key)) or 0.0
            bucket["slippage"] += self._float(row.get(slippage_key)) or 0.0
        for bucket in buckets.values():
            decided = bucket["wins"] + bucket["losses"]
            bucket["win_rate_pct"] = (
                bucket["wins"] / decided * 100.0 if decided else None
            )
            for key in ("realized_pnl", "fees", "slippage"):
                bucket[key] = round(bucket[key], 8)
        return buckets

    def _market_regimes(
        self,
        instruments: Sequence[str],
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> list[str]:
        """Describe the observed market regime for the evidence window.

        A regime is derived from the same daily closes the strategy could have
        seen: trend from the window return and volatility from daily returns.
        A bar set too short to classify yields no label, which is preferable to
        inventing a regime that would satisfy a coverage gate.
        """
        market = self._market_data
        if market is None or not instruments:
            return []
        labels: set[str] = set()
        for instrument in instruments:
            if not instrument:
                continue
            try:
                page = market.bars(
                    instrument,
                    interval="1d",
                    start=window_start,
                    end=window_end,
                    limit=250,
                    ascending=True,
                )
            except Exception:  # noqa: BLE001 - absence of bars is not a new regime
                continue
            items = list(getattr(page, "items", page) or ())
            closes = [self._float(getattr(item, "close", None)) for item in items]
            values = [value for value in closes if value is not None and value > 0]
            if len(values) < 3:
                continue
            window_return = (values[-1] - values[0]) / values[0]
            daily = [
                (values[index] - values[index - 1]) / values[index - 1]
                for index in range(1, len(values))
            ]
            mean = sum(daily) / len(daily)
            volatility = math.sqrt(
                sum((value - mean) ** 2 for value in daily) / len(daily)
            )
            trend = (
                "bull"
                if window_return > 0.02
                else "bear"
                if window_return < -0.02
                else "sideways"
            )
            band = "high_volatility" if volatility > 0.02 else "low_volatility"
            labels.add(f"{trend}_{band}")
        return sorted(labels)


__all__ = [
    "LearningError",
    "LearningIsolationError",
    "LearningNotFoundError",
    "LearningService",
    "LearningStateError",
    "LearningValidationError",
]
