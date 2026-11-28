"""PI-23 forecast optimization coordinator.

The service reads existing PI-05 through PI-11 evidence, computes an advisory
assessment and persists it as immutable PI-23 records. It has no order, fill,
ledger, broker or risk-policy mutation path. Production model promotion and
ensemble-weight application remain outside this service.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from novacontrol.novamarket.forecast_intelligence.evidence import ForecastEvidenceReader
from novacontrol.novamarket.forecast_intelligence.metrics import (
    actual_direction,
    calibration_analysis,
    classification_metrics,
    conditional_coverage,
    degradation_analysis,
    drift_analysis,
    merge_metrics,
    normalise_metrics,
    predicted_direction,
    probability_metrics,
    regression_metrics,
    reliability_label,
    uncertainty_metrics,
)
from novacontrol.novamarket.forecast_intelligence.models import (
    ABSTAINED_OUTCOME,
    AGREEMENT_CRITICAL,
    AGREEMENT_HIGH,
    AGREEMENT_INSUFFICIENT,
    AGREEMENT_MODERATE,
    AGREEMENT_STRONG,
    ALL_FAMILIES,
    ALTERNATIVE_MODEL,
    AUTOMATIC_PROMOTION,
    BASELINE_FALLBACK,
    BASELINE_FALLBACK_OUTCOME,
    BASELINE_FAMILIES,
    CALIBRATION_VERSION,
    CONDITIONAL_SUITABILITY,
    CONDITIONALLY_SELECTED,
    DECREASE_WEIGHT,
    DEGRADED,
    DRIFT_METHOD_VERSION,
    ENSEMBLE_FALLBACK,
    EXECUTION_AUTHORITY,
    HEALTHY,
    HIGH_RELIABILITY,
    HORIZON_FAMILY,
    HORIZONS,
    INCREASE_WEIGHT,
    INSUFFICIENT_DATA_OUTCOME,
    INSUFFICIENT_EVIDENCE,
    INSUFFICIENT_HEALTH,
    INSUFFICIENT_RELIABILITY,
    INSUFFICIENT_SUITABILITY,
    LOW_RELIABILITY,
    MODERATE_RELIABILITY,
    NO_ELIGIBLE_MODEL,
    PI23_CONTRACT_VERSION,
    PI23_PHASE,
    PI23_SCHEMA_VERSION,
    PI23_VERSION,
    PRIMARY_MODEL,
    PRODUCTION_WEIGHT_MUTATION,
    RELIABILITY_POLICY_VERSION,
    REMOVE_ELIGIBILITY,
    RETAIN_WEIGHT,
    RISK_AUTHORITY,
    SELECTED,
    SELECTION_POLICY_VERSION,
    STATUS_COMPLETED,
    STATUS_DRIFT_UNCONFIRMED,
    STATUS_FAILED,
    STATUS_INSUFFICIENT,
    STATUS_STALE,
    STATUS_WARNING,
    STRONG_SUITABILITY,
    TASKS,
    UNCERTAINTY_HIGH,
    UNCERTAINTY_LOW,
    UNCERTAINTY_MODERATE,
    UNCERTAINTY_UNAVAILABLE,
    UNCERTAINTY_VERY_HIGH,
    UNRELIABLE,
    WEAK_SUITABILITY,
    WEIGHT_INSUFFICIENT,
    ForecastIntelligenceConfig,
    ForecastIntelligenceNotFoundError,
    ForecastIntelligenceSources,
    ForecastIntelligenceValidationError,
    ModelEvidence,
    family_supports_task,
    finite,
    normalise_family,
    valid_horizon,
)
from novacontrol.novamarket.forecast_intelligence.store import ForecastIntelligenceStore

_VIEWS = (
    ("overview", "Model performance", "Performance, coverage, calibration and model health."),
    ("comparison", "Model comparison", "Eligible models for one instrument and horizon."),
    (
        "horizons",
        "Horizon optimization",
        "Suitability across intraday, swing and long-term horizons.",
    ),
    ("reliability", "Model reliability", "Categorical reliability with traceable evidence."),
    (
        "calibration",
        "Forecast calibration",
        "Probability calibration, reliability bins and holdout metrics.",
    ),
    (
        "disagreement",
        "Model disagreement",
        "Compatible forecasts, disagreement and conservative responses.",
    ),
    ("drift", "Drift and degradation", "Distribution, residual and performance monitoring."),
    ("selection", "Selection history", "Validation-only selection decisions and alternatives."),
    ("fallback", "Fallback and abstention", "Why a preferred forecast was or was not used."),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _parse(value: object) -> datetime | None:
    text = _iso(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _number(value: object) -> float | None:
    return finite(value)


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _instrument(value: object) -> str:
    text = str(value or "").strip().upper()
    if ":" not in text:
        raise ForecastIntelligenceValidationError(
            "instrument_id must be MIC:SYMBOL, for example XNSE:INFY"
        )
    exchange, symbol = text.split(":", 1)
    if len(exchange) < 2 or len(exchange) > 12 or not symbol:
        raise ForecastIntelligenceValidationError("instrument_id is not canonical")
    return f"{exchange}:{symbol}"


def _scope(values: Mapping[str, Any]) -> tuple[str, str]:
    if "instrument_id" not in values:
        raise ForecastIntelligenceValidationError("instrument_id is required")
    instrument = _instrument(values.get("instrument_id"))
    horizon = valid_horizon(values.get("horizon"))
    return instrument, horizon


def _horizon_seconds(horizon: str) -> int:
    return int(
        {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "20m": 1200,
            "1h": 3600,
            "1d": 86400,
            "3d": 3 * 86400,
            "5d": 5 * 86400,
            "30d": 30 * 86400,
            "90d": 90 * 86400,
        }[horizon]
    )


class ForecastIntelligenceService:
    def __init__(
        self,
        sources: ForecastIntelligenceSources | None = None,
        *,
        store_path: str = ":memory:",
        config: ForecastIntelligenceConfig | None = None,
        clock: Any = None,
    ) -> None:
        self._clock = clock or _now
        self._config = config or ForecastIntelligenceConfig()
        self._store = ForecastIntelligenceStore(store_path, clock=self._clock)
        stored = self._store.load_config()
        if stored:
            try:
                self._config = ForecastIntelligenceConfig(**stored)
            except (TypeError, ValueError):
                self._config = config or ForecastIntelligenceConfig()
        self.sources = sources or ForecastIntelligenceSources()
        self._reader = ForecastEvidenceReader(self.sources, self._config)

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self._store.close()

    @property
    def store(self) -> ForecastIntelligenceStore:
        return self._store

    @property
    def config(self) -> ForecastIntelligenceConfig:
        return self._config

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PI23_PHASE,
            "implemented": True,
            "initialised": True,
            "version": PI23_VERSION,
            "contract_version": PI23_CONTRACT_VERSION,
            "schema_version": PI23_SCHEMA_VERSION,
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "calibration_version": CALIBRATION_VERSION,
            "drift_method_version": DRIFT_METHOD_VERSION,
            "reliability_policy_version": RELIABILITY_POLICY_VERSION,
            "counts": self._store.counts(),
            "sources": self.sources.to_dict(),
            "evidence_errors": self._reader.errors[:12],
            "authority": self._authority(),
            "latest": {
                "evaluation": self._store.latest("evaluation"),
                "selection": self._store.latest("selection"),
                "calibration": self._store.latest("calibration"),
                "drift": self._store.latest("drift"),
                "reliability": self._store.latest("reliability"),
                "fallback": self._store.latest("fallback"),
            },
            "last_error": self._reader.errors[-1] if self._reader.errors else "",
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PI23_PHASE,
            "version": PI23_VERSION,
            "implemented": True,
            "contract_version": PI23_CONTRACT_VERSION,
            "scope": (
                "Advisory optimization over existing PI-05 through PI-11 forecasts; "
                "no new statistical, tree, LSTM or ensemble implementation."
            ),
            "authority": self._authority(),
            "horizons": {
                key: {
                    "family": HORIZON_FAMILY[key],
                    "duration_seconds": _horizon_seconds(key),
                }
                for key in HORIZONS
            },
            "tasks": list(TASKS),
            "selection": {
                "policy_version": SELECTION_POLICY_VERSION,
                "evidence": "validation and walk-forward training evidence only",
                "test_used_for_selection": False,
                "point_in_time": True,
                "automatic_promotion": False,
                "minimum_samples": self._config.min_selection_samples,
                "outcomes": [
                    SELECTED,
                    CONDITIONALLY_SELECTED,
                    BASELINE_FALLBACK,
                    INSUFFICIENT_EVIDENCE,
                    NO_ELIGIBLE_MODEL,
                ],
            },
            "calibration": {
                "version": CALIBRATION_VERSION,
                "methods": ["platt", "isotonic", "temperature", "auto"],
                "fitted_on": "earliest chronological segment",
                "evaluated_on": "later holdout",
                "minimum_samples": self._config.min_calibration_samples,
                "applied_to_existing_forecasts": False,
            },
            "uncertainty": {
                "labels": [
                    UNCERTAINTY_LOW,
                    UNCERTAINTY_MODERATE,
                    UNCERTAINTY_HIGH,
                    UNCERTAINTY_VERY_HIGH,
                    UNCERTAINTY_UNAVAILABLE,
                ],
                "calibrated_probabilities": False,
                "method": (
                    "interval coverage/width plus separately named model, data, "
                    "market and regime evidence"
                ),
            },
            "drift": {
                "version": DRIFT_METHOD_VERSION,
                "methods": ["psi", "kolmogorov_smirnov", "rolling_residuals"],
                "thresholds": {
                    "psi": self._config.drift_psi_threshold,
                    "ks_p_value": self._config.drift_ks_p_threshold,
                    "minimum_samples": self._config.drift_min_samples,
                    "persistence_windows": self._config.drift_persistence_windows,
                },
            },
            "reliability": {
                "policy_version": RELIABILITY_POLICY_VERSION,
                "classification_only": True,
                "numeric_score": None,
                "labels": [
                    HIGH_RELIABILITY,
                    MODERATE_RELIABILITY,
                    LOW_RELIABILITY,
                    UNRELIABLE,
                    INSUFFICIENT_RELIABILITY,
                ],
            },
            "fallback": {
                "outcomes": [
                    PRIMARY_MODEL,
                    ALTERNATIVE_MODEL,
                    ENSEMBLE_FALLBACK,
                    BASELINE_FALLBACK_OUTCOME,
                    ABSTAINED_OUTCOME,
                    INSUFFICIENT_DATA_OUTCOME,
                ],
                "abstention_is_valid": True,
                "issues_new_forecast": False,
            },
            "lifecycle": {
                "pi09_contract_preserved": True,
                "pi10_contract_preserved": True,
                "immutable_forecast_versioning": True,
                "new_forecast_version_required_for_new_selection": True,
            },
            "leakage_controls": {
                "test_set_selection": False,
                "future_outcomes_for_selection": False,
                "calibration_holdout": True,
                "point_in_time_training_runs": True,
                "duplicate_target_alignment": (
                    "exact horizon and target grouping; incompatible forecasts excluded"
                ),
            },
            "limits": {
                "max_models": self._config.max_models,
                "max_records": self._config.max_records,
                "max_evaluations": self._config.max_evaluations,
                "max_instruments": self._config.max_instruments,
            },
            "weight_changes": "advisory only; no production weight mutation",
        }

    def navigation(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PI23_PHASE,
            "route": "/market/forecast-intelligence",
            "views": [
                {"id": identifier, "label": label, "description": description}
                for identifier, label, description in _VIEWS
            ],
            "authority": self._authority(),
        }

    def config_view(self) -> dict[str, Any]:
        return {"ok": True, "config": self._config.to_dict()}

    def update_config(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ForecastIntelligenceValidationError("config payload must be an object")
        known = set(self._config.to_dict())
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ForecastIntelligenceValidationError(
                f"unknown configuration key(s): {', '.join(unknown)}"
            )
        values = {**self._config.to_dict(), **dict(payload)}
        updated = ForecastIntelligenceConfig(**values)
        self._store.save_config(updated.to_dict(), updated_at=_iso(self._clock()))
        self._config = updated
        self._reader.config = updated
        return {"ok": True, "config": updated.to_dict(), "actor": str(actor or "")}

    # -- shared helpers ------------------------------------------------------
    @staticmethod
    def _authority() -> dict[str, Any]:
        return {
            "risk_authority": RISK_AUTHORITY,
            "execution_authority": EXECUTION_AUTHORITY,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "new_forecasting_model": False,
            "new_ensemble_implementation": False,
            "submits_orders": False,
            "mutates_portfolio": False,
            "mutates_ledger": False,
            "automatic_promotion": AUTOMATIC_PROMOTION,
            "production_weight_mutation": PRODUCTION_WEIGHT_MUTATION,
        }

    def _reader_for_call(self) -> ForecastEvidenceReader:
        reader = ForecastEvidenceReader(self.sources, self._config)
        return reader

    def _freshness(self, evidence: ModelEvidence, horizon: str) -> dict[str, Any]:
        moments = [
            parsed
            for parsed in (
                _parse(evidence.as_of),
                _parse(evidence.evaluation_end),
            )
            if parsed is not None
        ]
        moment = max(moments) if moments else None
        if moment is None:
            return {"status": "unknown", "age_seconds": None, "stale": False}
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        age = max(0.0, (current - moment).total_seconds())
        stale = age > self._config.stale_seconds(horizon)
        return {
            "status": "stale" if stale else "fresh",
            "age_seconds": age,
            "stale": stale,
            "as_of": moment.isoformat(),
        }

    def _model_state(self, evidence: ModelEvidence) -> str:
        state = str(evidence.state or "").lower()
        if state in {"unavailable", "retired", "rejected"}:
            return "unavailable"
        if state in {"suspended", "degraded"}:
            return state
        return "eligible"

    def _current_forecasts(
        self, rows: Sequence[Mapping[str, Any]], *, require_target: bool = True
    ) -> list[dict[str, Any]]:
        """Keep only current forecasts whose target has not already passed."""
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        current: list[dict[str, Any]] = []
        for row in rows:
            body = dict(row)
            validity = str(body.get("validity") or body.get("status") or "").lower()
            if validity in {"stale", "invalid", "expired", "failed", "evaluated"}:
                continue
            target = _parse(body.get("target_at"))
            if target is None:
                if require_target:
                    continue
            elif target < moment:
                continue
            current.append(body)
        return current

    def _suitability(
        self,
        evidence: ModelEvidence,
        *,
        task: str,
        regime: str = "",
        resolution: str = "",
    ) -> dict[str, Any]:
        metrics = merge_metrics(evidence.selection_metrics, evidence.performance_metrics)
        samples = max(
            evidence.validation_samples, evidence.test_samples, int(metrics.get("samples") or 0)
        )
        family = evidence.family
        compatible = family_supports_task(family, task)
        reasons: list[str] = []
        warnings: list[str] = list(evidence.warnings)
        if not compatible:
            reasons.append(f"{family} is not compatible with {task}")
        if samples < self._config.min_evaluation_samples:
            reasons.append(
                f"{samples} observations are below the "
                f"{self._config.min_evaluation_samples} evaluation minimum"
            )
        if resolution and not self._resolution_compatible(evidence.horizon, resolution):
            reasons.append(f"resolution {resolution} is coarser than the horizon contract permits")
        state = self._model_state(evidence)
        if state == "unavailable":
            reasons.append("model is unavailable or retired")
        elif state in {"suspended", "degraded"}:
            warnings.append(f"registry state is {state}")
        freshness = self._freshness(evidence, evidence.horizon)
        if freshness["stale"]:
            warnings.append("latest evidence is stale for this horizon")
        if regime:
            count = int(evidence.regimes.get(regime, 0))
            if count < self._config.min_regime_samples:
                warnings.append(
                    f"regime {regime} has {count} observations; "
                    f"{self._config.min_regime_samples} required"
                )
        accuracy = _number(metrics.get("directional_accuracy"))
        coverage = _number(metrics.get("interval_coverage"))
        ece = _number(metrics.get("calibration_error"))
        if task in {"direction_classification", "probability_estimation"} and accuracy is None:
            reasons.append("no directional validation metric is available")
        if task == "probability_estimation" and ece is None:
            reasons.append("no probability calibration evidence is available")
        if coverage is not None and coverage < 0.45:
            warnings.append("empirical interval coverage is materially below the nominal interval")
        if ece is not None and ece > 0.20:
            warnings.append("calibration error is high")
        if reasons or samples < self._config.min_selection_samples:
            level = (
                INSUFFICIENT_SUITABILITY
                if samples < self._config.min_evaluation_samples
                else WEAK_SUITABILITY
            )
        elif (
            compatible
            and state == "eligible"
            and accuracy is not None
            and accuracy >= 0.53
            and (coverage is None or coverage >= 0.55)
            and (ece is None or ece <= 0.12)
            and not freshness["stale"]
            and not warnings
        ):
            level = STRONG_SUITABILITY
        elif compatible and state != "unavailable":
            level = CONDITIONAL_SUITABILITY
        else:
            level = WEAK_SUITABILITY
        return {
            "model_id": evidence.model_id,
            "version": evidence.version,
            "family": family,
            "instrument_id": evidence.instrument_id,
            "horizon": evidence.horizon,
            "task": task,
            "suitability": level,
            "eligible": level in {STRONG_SUITABILITY, CONDITIONAL_SUITABILITY},
            "compatible": compatible,
            "samples": samples,
            "validation_samples": evidence.validation_samples,
            "test_samples": evidence.test_samples,
            "metrics": metrics,
            "regime": regime or "all",
            "regime_samples": int(evidence.regimes.get(regime, 0))
            if regime
            else sum(evidence.regimes.values()),
            "freshness": freshness,
            "state": state,
            "calibration_status": evidence.calibration_status,
            "reasons": reasons,
            "warnings": warnings,
            "evidence_source": evidence.source,
        }

    @staticmethod
    def _resolution_compatible(horizon: str, resolution: str) -> bool:
        # PI-01 ranks interval coarseness. A daily bar cannot support a 5m target.
        ranks = {
            "spot": 0,
            "1m": 1,
            "minute_1": 1,
            "5m": 2,
            "minute_5": 2,
            "15m": 3,
            "minute_15": 3,
            "30m": 4,
            "minute_30": 4,
            "1h": 5,
            "hour_1": 5,
            "1d": 6,
            "day_1": 6,
            "1w": 7,
            "week_1": 7,
            "1mo": 8,
            "month_1": 8,
        }
        maximum = {
            "1m": 1,
            "5m": 2,
            "15m": 3,
            "20m": 3,
            "1h": 5,
            "1d": 6,
            "3d": 6,
            "5d": 6,
            "30d": 6,
            "90d": 6,
        }
        chosen = ranks.get(str(resolution or "").lower())
        if chosen is None:
            return True
        return chosen <= maximum.get(horizon, 6)

    def _evaluation_records(
        self,
        reader: ForecastEvidenceReader,
        *,
        instrument_id: str,
        horizon: str,
        model_id: str = "",
        as_of: str = "",
        explicit: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if explicit is not None:
            records = [dict(item) for item in explicit if isinstance(item, Mapping)]
        else:
            records = reader.evaluation_records(
                instrument_id, horizon, model_id=model_id, as_of=as_of
            )
        result: list[dict[str, Any]] = []
        for row in records:
            if instrument_id and str(row.get("instrument_id") or "") not in {"", instrument_id}:
                continue
            if horizon and str(row.get("horizon") or "") not in {"", horizon}:
                continue
            if model_id and str(row.get("model_id") or "") != model_id:
                continue
            body = dict(row)
            if (
                body.get("actual_return") is None
                and body.get("actual_price") is not None
                and body.get("predicted_price") is not None
            ):
                try:
                    reference = float(body.get("reference_price") or 0.0)
                    if reference > 0:
                        body["actual_return"] = float(body["actual_price"]) / reference - 1.0
                        body["predicted_return"] = float(body["predicted_price"]) / reference - 1.0
                except (TypeError, ValueError):
                    pass
            if not body.get("actual_direction") and body.get("actual_return") is not None:
                body["actual_direction"] = actual_direction(
                    float(body["actual_return"]), self._config.directional_threshold
                )
            if not body.get("predicted_direction"):
                body["predicted_direction"] = predicted_direction(
                    body, self._config.directional_threshold
                )
            result.append(body)
            if len(result) >= self._config.max_records:
                break
        return result

    def _evidence(
        self,
        instrument_id: str,
        horizon: str,
        *,
        as_of: str = "",
    ) -> tuple[dict[str, ModelEvidence], ForecastEvidenceReader]:
        reader = self._reader_for_call()
        evidence = reader.training_evidence(instrument_id, horizon, as_of=as_of)
        evidence = reader.attach_performance(evidence, instrument_id, horizon, as_of=as_of)
        evidence = reader.attach_calibration(evidence, instrument_id, horizon)
        if not evidence:
            registry = reader.registry(instrument_id, horizon)
            for row in registry[: self._config.max_models]:
                model_id = str(row.get("model_id") or row.get("family") or "unknown")
                evidence[model_id] = ModelEvidence(
                    model_id=model_id,
                    version=str(row.get("version") or row.get("model_version") or "unspecified"),
                    family=normalise_family(row.get("family") or row.get("kind") or model_id),
                    instrument_id=instrument_id,
                    horizon=horizon,
                    dataset_version=str(row.get("dataset_version") or ""),
                    feature_set_version=str(row.get("feature_set_version") or "unspecified"),
                    training_dataset_version=str(row.get("dataset_version") or ""),
                    artifact_ref=str(row.get("artifact_location") or ""),
                    state=str(row.get("state") or "unavailable"),
                    calibration_status=str(row.get("calibration") or "unavailable"),
                    as_of=_iso(row.get("updated_at")),
                    warnings=("no stored validation or performance scorecard",),
                )
        return evidence, reader

    def _model_rows(
        self,
        evidence: Mapping[str, ModelEvidence],
        *,
        task: str,
        regime: str = "",
        resolution: str = "",
    ) -> list[dict[str, Any]]:
        return [
            self._suitability(model, task=task, regime=regime, resolution=resolution)
            for model in sorted(evidence.values(), key=lambda item: item.model_id)
            if model.model_id
        ]

    # -- evaluation ----------------------------------------------------------
    def evaluate(
        self,
        payload: Mapping[str, Any],
        *,
        records: Sequence[Mapping[str, Any]] | None = None,
        persist: bool = True,
        as_of: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        task = str(payload.get("task") or "return_regression").strip().lower()
        if task not in TASKS:
            raise ForecastIntelligenceValidationError(
                f"unknown task {task!r}; known: {', '.join(TASKS)}"
            )
        if as_of and _parse(as_of) is None:
            raise ForecastIntelligenceValidationError("as_of must be an ISO timestamp")
        reader = self._reader_for_call()
        evidence = reader.training_evidence(instrument_id, horizon, as_of=as_of)
        evidence = reader.attach_performance(evidence, instrument_id, horizon, as_of=as_of)
        evidence = reader.attach_calibration(evidence, instrument_id, horizon)
        if not evidence:
            # A registry entry without validation is still honest evidence of
            # availability, but not of model quality.
            for row in reader.registry(instrument_id, horizon)[: self._config.max_models]:
                model_id = str(row.get("model_id") or row.get("family") or "unknown")
                evidence[model_id] = ModelEvidence(
                    model_id=model_id,
                    version=str(row.get("version") or row.get("model_version") or "unspecified"),
                    family=normalise_family(row.get("family") or row.get("kind") or model_id),
                    instrument_id=instrument_id,
                    horizon=horizon,
                    dataset_version=str(row.get("dataset_version") or ""),
                    feature_set_version=str(row.get("feature_set_version") or "unspecified"),
                    artifact_ref=str(row.get("artifact_location") or ""),
                    state=str(row.get("state") or "unavailable"),
                    calibration_status=str(row.get("calibration") or "unavailable"),
                    as_of=_iso(row.get("updated_at")),
                    warnings=("no stored validation or performance scorecard",),
                )
        evaluation_records = self._evaluation_records(
            reader,
            instrument_id=instrument_id,
            horizon=horizon,
            as_of=as_of,
            explicit=records,
        )
        explicit_metrics = normalise_metrics(
            regression_metrics(
                evaluation_records, task=task, threshold=self._config.directional_threshold
            )
        )
        explicit_metrics.update(
            {
                "classification": classification_metrics(
                    evaluation_records, threshold=self._config.directional_threshold
                ),
                "probability": probability_metrics(
                    evaluation_records,
                    threshold=self._config.directional_threshold,
                    bins=self._config.calibration_bins,
                ),
                "uncertainty": uncertainty_metrics(evaluation_records),
            }
        )
        models: list[dict[str, Any]] = []
        for model in sorted(evidence.values(), key=lambda item: item.model_id):
            suitability = self._suitability(
                model,
                task=task,
                regime=str(payload.get("regime") or ""),
                resolution=str(payload.get("resolution") or ""),
            )
            metrics = merge_metrics(model.selection_metrics, model.performance_metrics)
            samples = max(
                model.validation_samples, model.test_samples, int(metrics.get("samples") or 0)
            )
            model_records = self._evaluation_records(
                reader,
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model.model_id,
                as_of=as_of,
                explicit=records,
            )
            model_metrics = dict(metrics)
            if model_records:
                model_metrics.update(
                    {
                        "classification": classification_metrics(model_records),
                        "probability": probability_metrics(
                            model_records, bins=self._config.calibration_bins
                        ),
                        "uncertainty": uncertainty_metrics(model_records),
                    }
                )
            status = STATUS_COMPLETED
            warnings = list(model.warnings)
            if samples < self._config.min_evaluation_samples:
                status = STATUS_INSUFFICIENT
            if self._freshness(model, horizon)["stale"]:
                status = STATUS_STALE if status == STATUS_COMPLETED else status
                warnings.append("latest evaluation evidence is stale for this horizon")
            if not model.artifact_ref and model.family in {"xgboost", "lightgbm", "lstm"}:
                warnings.append("model artifact reference is unavailable")
            model_body = {
                "model_id": model.model_id,
                "version": model.version,
                "family": model.family,
                "instrument_id": instrument_id,
                "exchange": instrument_id.split(":", 1)[0],
                "horizon": horizon,
                "task": task,
                "dataset_version": model.dataset_version,
                "training_dataset_version": model.training_dataset_version,
                "feature_set_version": model.feature_set_version,
                "evaluation_dataset_version": str(payload.get("evaluation_dataset_version") or ""),
                "resolution": model.resolution or str(payload.get("resolution") or ""),
                "evaluation_period": {
                    "start": model.evaluation_start,
                    "end": model.evaluation_end,
                },
                "selection_metrics": dict(model.selection_metrics),
                "performance_metrics": dict(model.performance_metrics),
                "metrics": model_metrics,
                "samples": samples,
                "validation_samples": model.validation_samples,
                "test_samples": model.test_samples,
                "regimes": dict(model.regimes),
                "regime_metrics": {key: dict(value) for key, value in model.regime_metrics.items()},
                "calibration_status": model.calibration_status,
                "calibration": dict(model.calibration),
                "freshness": self._freshness(model, horizon),
                "state": self._model_state(model),
                "suitability": suitability,
                "status": status,
                "warnings": list(dict.fromkeys(warnings)),
                "test_used_for_selection": False,
                "selection_evidence_source": "validation"
                if model.selection_metrics
                else "unavailable",
            }
            models.append(model_body)
        statuses = {str(row.get("status")) for row in models}
        if not models:
            data_status = STATUS_INSUFFICIENT
        elif STATUS_FAILED in statuses:
            data_status = STATUS_FAILED
        elif STATUS_STALE in statuses:
            data_status = STATUS_STALE
        elif STATUS_INSUFFICIENT in statuses:
            data_status = STATUS_WARNING
        else:
            data_status = STATUS_COMPLETED
        created = _iso(self._clock())
        evidence_moments = [
            parsed
            for model in evidence.values()
            for parsed in (_parse(model.as_of), _parse(model.evaluation_end))
            if parsed is not None
        ]
        evidence_as_of = max(evidence_moments) if evidence_moments else None
        as_of_value = _iso(as_of) if as_of else _iso(evidence_as_of) if evidence_as_of else created
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "evaluation_id": "",
            "created_at": created,
            "as_of": as_of_value,
            "instrument_id": instrument_id,
            "exchange": instrument_id.split(":", 1)[0],
            "horizon": horizon,
            "family": HORIZON_FAMILY[horizon],
            "task": task,
            "regime": str(payload.get("regime") or ""),
            "resolution": str(payload.get("resolution") or ""),
            "dataset_versions": sorted(
                {model.dataset_version for model in evidence.values() if model.dataset_version}
            ),
            "feature_set_versions": sorted(
                {
                    model.feature_set_version
                    for model in evidence.values()
                    if model.feature_set_version
                }
            ),
            "models": models,
            "model_count": len(models),
            "records": evaluation_records[: min(len(evaluation_records), self._config.max_records)],
            "record_count": len(evaluation_records),
            "record_metrics": explicit_metrics if evaluation_records else {},
            "data_status": data_status,
            "status": data_status,
            "warnings": [],
            "evidence_errors": reader.errors[:12],
            "selection_evidence": (
                "validation-only; test scorecards are monitoring evidence "
                "and are not used to select"
            ),
            "test_used_for_selection": False,
            "automatic_promotion": False,
            "authority": self._authority(),
            "limitations": [
                "Model training and inference remain owned by PI-05 through PI-08.",
                "Test scorecards are reported for monitoring but never used for selection.",
                "Missing evaluation evidence is insufficent evidence, not zero error.",
            ],
        }
        if not evaluation_records and not any(
            model.selection_metrics for model in evidence.values()
        ):
            body["warnings"].append("no chronological evaluation or validation evidence was found")
        if reader.errors:
            body["warnings"].append("one or more evidence sources were unavailable")
        evidence_token = json.dumps(
            [
                {
                    "model_id": row["model_id"],
                    "version": row["version"],
                    "samples": row["samples"],
                    "selection_metrics": row["selection_metrics"],
                    "performance_metrics": row["performance_metrics"],
                }
                for row in models
            ],
            sort_keys=True,
            default=str,
        )
        record_token = json.dumps(evaluation_records, sort_keys=True, default=str)
        digest = hashlib.sha256(
            "|".join(
                [
                    instrument_id,
                    horizon,
                    task,
                    as_of_value,
                    evidence_token,
                    record_token,
                    json.dumps(reader.errors, sort_keys=True),
                ]
            ).encode("utf-8")
        ).hexdigest()
        body["evaluation_id"] = f"fie_{digest[:24]}"
        if persist:
            body["actor"] = str(actor or "")
            return self._store.save(
                "evaluation",
                body,
                record_id=str(body["evaluation_id"]),
                instrument_id=instrument_id,
                horizon=horizon,
                status=data_status,
                created_at=created,
            )
        return body

    # -- selection -----------------------------------------------------------
    def select(
        self,
        payload: Mapping[str, Any],
        *,
        persist: bool = True,
        actor: str = "",
        evidence: Mapping[str, ModelEvidence] | None = None,
        as_of: str = "",
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        task = str(payload.get("task") or "return_regression").strip().lower()
        if task not in TASKS:
            raise ForecastIntelligenceValidationError(f"unknown task {task!r}")
        regime = str(payload.get("regime") or "").strip()
        resolution = str(payload.get("resolution") or "").strip()
        if evidence is None:
            evidence, _reader = self._evidence(instrument_id, horizon, as_of=as_of)
        candidates = self._model_rows(evidence, task=task, regime=regime, resolution=resolution)
        eligible: list[dict[str, Any]] = []
        for row in candidates:
            model = evidence.get(str(row["model_id"]))
            if model is None:
                continue
            metrics = merge_metrics(model.selection_metrics)
            samples = int(model.validation_samples or metrics.get("samples") or 0)
            if samples < self._config.min_selection_samples:
                continue
            if not row["compatible"]:
                continue
            if row["state"] == "unavailable":
                continue
            if task == "probability_estimation":
                score = _number(metrics.get("brier"))
                higher_is_better = False
            elif task == "direction_classification":
                score = _number(metrics.get("directional_accuracy"))
                higher_is_better = True
            elif task == "interval_estimation":
                score = _number(metrics.get("interval_coverage"))
                higher_is_better = True
            else:
                score = _number(metrics.get("mae"))
                higher_is_better = False
            if score is None:
                continue
            coverage = _number(metrics.get("interval_coverage"))
            accuracy = _number(metrics.get("directional_accuracy"))
            ece = _number(metrics.get("calibration_error"))
            guardrails = (
                (accuracy is None or accuracy >= 0.48)
                and (coverage is None or coverage >= 0.45)
                and (task != "probability_estimation" or (ece is not None and ece <= 0.20))
            )
            eligible.append(
                {
                    **row,
                    "selection_score": score,
                    "score_direction": "higher" if higher_is_better else "lower",
                    "guardrails_passed": guardrails,
                    "selection_metrics": metrics,
                    "selection_samples": samples,
                }
            )
        selected: dict[str, Any] | None = None
        outcome = INSUFFICIENT_EVIDENCE
        rationale: list[str] = []
        if eligible:

            def sort_key(row: Mapping[str, Any]) -> tuple[float, int, float]:
                score = float(row["selection_score"])
                primary = -score if row["score_direction"] == "higher" else score
                calibration = _number(row.get("selection_metrics", {}).get("calibration_error"))
                return (
                    primary,
                    -int(row["selection_samples"]),
                    calibration if calibration is not None else 1.0,
                )

            eligible.sort(key=sort_key)
            selected = dict(eligible[0])
            if not selected["guardrails_passed"]:
                outcome = CONDITIONALLY_SELECTED
                rationale.append(
                    "the highest-ranked validation model missed one or more guardrails"
                )
            elif int(selected["validation_samples"]) < self._config.min_selection_samples:
                outcome = CONDITIONALLY_SELECTED
                rationale.append("validation sample is below the preferred selection minimum")
            else:
                outcome = SELECTED
                rationale.append(
                    "highest validation score among models that passed every guardrail"
                )
            if selected["state"] in {"suspended", "degraded"}:
                outcome = CONDITIONALLY_SELECTED
                rationale.append(f"registry state is {selected['state']}")
            if selected["freshness"]["stale"]:
                outcome = CONDITIONALLY_SELECTED
                rationale.append("evaluation evidence is stale")
        else:
            # The established statistical baseline is a valid fallback, but only
            # when it has chronological validation evidence.
            baselines = [
                row
                for row in candidates
                if row["family"] in BASELINE_FAMILIES
                and int(row["validation_samples"]) >= self._config.min_selection_samples
                and row["compatible"]
            ]
            if baselines:
                baselines.sort(
                    key=lambda row: (
                        _number(row["metrics"].get("mae")) is None,
                        _number(row["metrics"].get("mae")) or float("inf"),
                    )
                )
                selected = dict(baselines[0])
                outcome = BASELINE_FALLBACK
                rationale.append(
                    "no model had eligible validation evidence; an established "
                    "statistical baseline is available"
                )
            elif candidates:
                outcome = NO_ELIGIBLE_MODEL
                rationale.append(
                    "models exist, but none has sufficient validation evidence and compatible scope"
                )
            else:
                outcome = INSUFFICIENT_EVIDENCE
                rationale.append("no model evidence was available for this scope")
        alternatives = []
        for row in eligible:
            if (
                selected
                and row["model_id"] == selected["model_id"]
                and row["version"] == selected["version"]
            ):
                continue
            alternatives.append(
                {
                    "model_id": row["model_id"],
                    "version": row["version"],
                    "family": row["family"],
                    "selection_score": row["selection_score"],
                    "score_direction": row["score_direction"],
                    "samples": row["selection_samples"],
                    "guardrails_passed": row["guardrails_passed"],
                    "suitability": row["suitability"],
                }
            )
        created = _iso(self._clock())
        selected_id = selected.get("model_id") if selected else ""
        selection_seed = f"{instrument_id}|{horizon}|{task}|{regime}|{created}|{selected_id}"
        selection_id = "fis_" + hashlib.sha256(selection_seed.encode("utf-8")).hexdigest()[:24]
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "selection_id": selection_id,
            "created_at": created,
            "as_of": _iso(as_of) if as_of else created,
            "instrument_id": instrument_id,
            "exchange": instrument_id.split(":", 1)[0],
            "horizon": horizon,
            "task": task,
            "regime": regime or "all",
            "resolution": resolution,
            "outcome": outcome,
            "selected_model": selected,
            "selected_model_id": selected["model_id"] if selected else None,
            "selected_version": selected["version"] if selected else None,
            "alternatives": alternatives,
            "rationale": rationale,
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "selection_evidence": "validation-only; test set excluded",
            "test_used_for_selection": False,
            "sample_count": int(selected["validation_samples"]) if selected else 0,
            "calibration_status": selected.get("calibration_status") if selected else "unavailable",
            "uncertainty": self._selection_uncertainty(selected),
            "automatic_promotion": False,
            "authority": self._authority(),
            "limitations": [
                "A selection is advisory and does not replace a production model.",
                "Validation evidence is point-in-time and may not predict future performance.",
                "No test-set score contributes to this decision.",
            ],
            "actor": str(actor or ""),
        }
        if persist:
            return self._store.save(
                "selection",
                body,
                record_id=selection_id,
                instrument_id=instrument_id,
                horizon=horizon,
                regime=regime,
                status=outcome,
                created_at=created,
            )
        return body

    def _selection_uncertainty(self, selected: Mapping[str, Any] | None) -> dict[str, Any]:
        if not selected:
            return {"label": UNCERTAINTY_UNAVAILABLE, "reason": "no selected model"}
        metrics = selected.get("selection_metrics") or {}
        width = _number(metrics.get("interval_width"))
        coverage = _number(metrics.get("interval_coverage"))
        ece = _number(metrics.get("calibration_error"))
        if width is None and coverage is None:
            label = UNCERTAINTY_UNAVAILABLE
        elif (
            width is not None
            and width > self._config.max_interval_width
            or coverage is not None
            and coverage < 0.40
        ):
            label = UNCERTAINTY_VERY_HIGH
        elif (coverage is not None and coverage < 0.55) or (ece is not None and ece > 0.15):
            label = UNCERTAINTY_HIGH
        elif (width is not None and width > 0.05) or (ece is not None and ece > 0.10):
            label = UNCERTAINTY_MODERATE
        else:
            label = UNCERTAINTY_LOW
        return {
            "label": label,
            "coverage": coverage,
            "interval_width": width,
            "calibration_error": ece,
            "calibrated_probability": False,
        }

    # -- calibration ---------------------------------------------------------
    def calibrate(
        self,
        payload: Mapping[str, Any],
        *,
        records: Sequence[Mapping[str, Any]] | None = None,
        persist: bool = True,
        actor: str = "",
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        model_id = str(payload.get("model_id") or "").strip()
        method = str(payload.get("method") or "auto").strip().lower()
        reader = self._reader_for_call()
        evidence_records = self._evaluation_records(
            reader,
            instrument_id=instrument_id,
            horizon=horizon,
            model_id=model_id,
            explicit=records,
        )
        result = calibration_analysis(
            evidence_records,
            method=method,
            config=self._config,
            dataset_version=str(payload.get("dataset_version") or ""),
            model_version=str(payload.get("model_version") or ""),
        )
        created = _iso(self._clock())
        calibration_seed = (
            f"{instrument_id}|{horizon}|{model_id}|{method}|{created}|{len(evidence_records)}"
        )
        calibration_id = "fic_" + hashlib.sha256(calibration_seed.encode("utf-8")).hexdigest()[:24]
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "calibration_id": calibration_id,
            "created_at": created,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "model_id": model_id or "all",
            "method": method,
            "status": result["status"],
            "samples": result["samples"],
            "calibration": result.get("calibration"),
            "before": result.get("before"),
            "after": result.get("after"),
            "ece_improvement": result.get("ece_improvement"),
            "accepted": result.get("accepted", False),
            "dataset_version": str(payload.get("dataset_version") or ""),
            "model_version": str(payload.get("model_version") or ""),
            "fitted_on": result.get("fitted_on"),
            "evaluation_on": result.get("evaluation_on"),
            "detail": result.get("detail"),
            "calibration_version": CALIBRATION_VERSION,
            "applied_to_existing_forecasts": False,
            "records_supplied_by_caller": records is not None,
            "authority": self._authority(),
            "limitations": [
                "The calibrator is not attached to a production model by PI-23.",
                "A calibrated probability is not guaranteed to remain "
                "calibrated after a regime change.",
            ],
            "actor": str(actor or ""),
        }
        if persist:
            return self._store.save(
                "calibration",
                body,
                record_id=calibration_id,
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model_id,
                status=result["status"],
                created_at=created,
            )
        return body

    def calibration(self, instrument_id: str, horizon: str, model_id: str = "") -> dict[str, Any]:
        return {
            "ok": True,
            "calibration": self._store.latest(
                "calibration",
                instrument_id=_instrument(instrument_id),
                horizon=valid_horizon(horizon),
                model_id=model_id,
            ),
        }

    # -- uncertainty ---------------------------------------------------------
    def uncertainty(
        self,
        payload: Mapping[str, Any],
        *,
        records: Sequence[Mapping[str, Any]] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        model_id = str(payload.get("model_id") or "").strip()
        reader = self._reader_for_call()
        selected_records = self._evaluation_records(
            reader,
            instrument_id=instrument_id,
            horizon=horizon,
            model_id=model_id,
            explicit=records,
        )
        evidence, _ = self._evidence(instrument_id, horizon)
        model = evidence.get(model_id) if model_id else None
        metrics = merge_metrics(
            *(item.selection_metrics for item in evidence.values()),
            *(item.performance_metrics for item in evidence.values()),
        )
        if selected_records:
            empirical = uncertainty_metrics(selected_records)
            conditional = conditional_coverage(selected_records)
            calibration_error = probability_metrics(
                selected_records, bins=self._config.calibration_bins
            ).get("calibration_error")
        else:
            empirical = {
                "samples": int(metrics.get("samples") or 0),
                "coverage": metrics.get("interval_coverage"),
                "mean_width": metrics.get("interval_width"),
                "quantile_loss": metrics.get("quantile_loss"),
                "nominal_coverage": None,
                "coverage_gap": None,
                "notes": [
                    "derived from stored scorecards; no per-observation "
                    "interval records were available"
                ],
            }
            conditional = {}
            calibration_error = metrics.get("calibration_error")
        width = _number(empirical.get("mean_width"))
        coverage = _number(empirical.get("coverage"))
        if width is None and coverage is None:
            label = UNCERTAINTY_UNAVAILABLE
        elif (
            width is not None
            and width > self._config.max_interval_width
            or coverage is not None
            and coverage < 0.40
        ):
            label = UNCERTAINTY_VERY_HIGH
        elif (coverage is not None and coverage < 0.55) or (
            calibration_error is not None and calibration_error > 0.15
        ):
            label = UNCERTAINTY_HIGH
        elif (width is not None and width > 0.05) or (
            calibration_error is not None and calibration_error > 0.10
        ):
            label = UNCERTAINTY_MODERATE
        else:
            label = UNCERTAINTY_LOW
        considered = [model] if model is not None else list(evidence.values())
        stale_models = sorted(
            item.model_id for item in considered if self._freshness(item, horizon)["stale"]
        )
        regime_limited = any(
            item.regimes and max(item.regimes.values(), default=0) < self._config.min_regime_samples
            for item in considered
        )
        model_components = {
            "model_uncertainty": (
                "unavailable"
                if width is None
                else ("high" if label in {UNCERTAINTY_HIGH, UNCERTAINTY_VERY_HIGH} else "moderate")
            ),
            "data_uncertainty": (
                "stale" if stale_models else "current" if considered else "unavailable"
            ),
            "market_uncertainty": (
                "unavailable"
                if not selected_records
                else "high"
                if width is not None and width > self._config.max_interval_width * 0.75
                else "normal"
            ),
            "regime_uncertainty": (
                "insufficient_samples"
                if regime_limited
                else "observed"
                if any(item.regimes for item in considered)
                else "unavailable"
            ),
        }
        created = _iso(self._clock())
        assessment_id = (
            "fiu_"
            + hashlib.sha256(
                f"{instrument_id}|{horizon}|{model_id}|{created}|{label}".encode()
            ).hexdigest()[:24]
        )
        body: dict[str, Any] = {
            "ok": True,
            "assessment_id": assessment_id,
            "created_at": created,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "model_id": model_id or "all",
            "label": label,
            "calibrated_probability": False,
            "empirical": empirical,
            "conditional_coverage": conditional,
            "components": model_components,
            "stale_models": stale_models,
            "calibration_error": calibration_error,
            "method": (
                "empirical interval coverage and width; label thresholds are "
                "documented heuristics, not calibrated probabilities"
            ),
            "limitations": [
                "Model, data, market and regime uncertainty are separately "
                "named and are not a variance decomposition.",
                "A narrow interval is not treated as reliable when historical coverage is poor.",
            ],
        }
        if persist:
            return self._store.save(
                "uncertainty",
                body,
                record_id=assessment_id,
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model_id,
                status=label,
                created_at=created,
            )
        return body

    # -- disagreement --------------------------------------------------------
    def disagreement(
        self,
        payload: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        reader = self._reader_for_call()
        forecasts = reader.forecasts(
            instrument_id, horizon, limit=min(self._config.max_records, 200)
        )
        forecasts = self._current_forecasts(forecasts)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for forecast in forecasts:
            target = _iso(forecast.get("target_at"))
            generated = _parse(forecast.get("generated_at") or forecast.get("created_at"))
            validity = str(forecast.get("validity") or forecast.get("status") or "").lower()
            if validity in {"stale", "invalid", "expired", "failed"}:
                continue
            if not target:
                continue
            forecast["_generated"] = generated
            grouped.setdefault(f"{horizon}|{target}", []).append(forecast)
        compatible: list[dict[str, Any]] = []
        for _key, rows in grouped.items():
            parsed = [row for row in rows if row.get("_generated") is not None]
            if not parsed:
                continue
            latest = max(row["_generated"] for row in parsed)
            tolerance = timedelta(seconds=self._config.reference_tolerance_seconds)
            cluster = [row for row in parsed if latest - row["_generated"] <= tolerance]
            if len(cluster) > len(compatible):
                compatible = cluster
        if len(compatible) < 2:
            state = AGREEMENT_INSUFFICIENT
            body: dict[str, Any] = {
                "ok": True,
                "instrument_id": instrument_id,
                "horizon": horizon,
                "target_at": compatible[0].get("target_at") if compatible else "",
                "state": state,
                "participants": [],
                "reason": (
                    "fewer than two compatible current forecasts share the same horizon and target"
                ),
                "alignment": "exact horizon and target, reference generation within tolerance",
                "recommended_response": "WAIT",
                "advisory_only": True,
                "created_at": _iso(self._clock()),
            }
            if persist:
                self._store.save(
                    "disagreement", body, instrument_id=instrument_id, horizon=horizon, status=state
                )
            return body
        rows = []
        for row in compatible:
            p_up = _number(row.get("p_up"))
            p_down = _number(row.get("p_down"))
            p_neutral = _number(row.get("p_neutral", row.get("p_sideways")))
            direction = str(row.get("direction") or "").lower()
            if not direction:
                direction = predicted_direction(row, self._config.directional_threshold)
            rows.append(
                {
                    "model_id": str(row.get("model_id") or row.get("engine") or "unknown"),
                    "version": str(row.get("model_version") or row.get("version") or "unspecified"),
                    "predicted_return": _number(row.get("predicted_return")),
                    "predicted_price": _number(row.get("predicted_price")),
                    "direction": direction,
                    "p_up": p_up,
                    "p_down": p_down,
                    "p_neutral": p_neutral,
                    "interval_lower": _number(row.get("lower_return", row.get("interval_lower"))),
                    "interval_upper": _number(row.get("upper_return", row.get("interval_upper"))),
                    "generated_at": _iso(row.get("generated_at") or row.get("created_at")),
                    "freshness": row.get("data_freshness") or row.get("freshness") or "unknown",
                }
            )
        returns = [row["predicted_return"] for row in rows if row["predicted_return"] is not None]
        directions = [row["direction"] for row in rows if row["direction"]]
        return_range = max(returns) - min(returns) if returns else None
        mean_abs = _mean([abs(value) for value in returns]) if returns else None
        normalised_range = (
            return_range
            / max(
                mean_abs if mean_abs is not None else 0.0,
                self._config.max_disagreement_return / 2.0,
            )
            if return_range is not None
            else None
        )
        direction_counts: dict[str, int] = {}
        for direction in directions:
            direction_counts[direction] = direction_counts.get(direction, 0) + 1
        direction_split = (
            1.0 - max(direction_counts.values()) / len(directions) if directions else None
        )
        p_up_values = [row["p_up"] for row in rows if row["p_up"] is not None]
        probability_spread = max(p_up_values) - min(p_up_values) if len(p_up_values) > 1 else None
        overlap = self._interval_overlap(rows)
        if return_range is None:
            state = AGREEMENT_INSUFFICIENT
        elif (
            return_range > self._config.critical_disagreement_return
            or (direction_split or 0) >= 0.50
        ):
            state = AGREEMENT_CRITICAL
        elif return_range > self._config.max_disagreement_return or (direction_split or 0) >= 0.25:
            state = AGREEMENT_HIGH
        elif (
            return_range > self._config.max_disagreement_return / 2.0
            or (direction_split or 0) > 0.0
        ):
            state = AGREEMENT_MODERATE
        else:
            state = AGREEMENT_STRONG
        response = "WAIT" if state in {AGREEMENT_HIGH, AGREEMENT_CRITICAL} else "MONITOR"
        reasons = []
        if direction_split:
            reasons.append("participating models disagree on direction")
        if return_range is not None and return_range > self._config.max_disagreement_return:
            reasons.append("expected-return range exceeds the configured agreement threshold")
        if probability_spread is not None and probability_spread > 0.25:
            reasons.append("upward probability spread is wide")
        if overlap is not None and overlap < 0.20:
            reasons.append("prediction intervals barely overlap")
        created = _iso(self._clock())
        timing_values: list[datetime] = []
        for row in rows:
            parsed_time = _parse(row["generated_at"])
            if parsed_time is not None:
                timing_values.append(parsed_time)
        payload_body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "disagreement_id": "fid_"
            + hashlib.sha256(f"{instrument_id}|{horizon}|{created}|{state}".encode()).hexdigest()[
                :24
            ],
            "created_at": created,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "target_at": compatible[0].get("target_at"),
            "state": state,
            "participants": rows,
            "participant_count": len(rows),
            "measurements": {
                "return_range": return_range,
                "return_dispersion": statistics.pstdev(returns) if len(returns) > 1 else None,
                "normalised_return_range": normalised_range,
                "direction_split": direction_split,
                "direction_counts": direction_counts,
                "probability_spread": probability_spread,
                "interval_overlap": overlap,
                "timing_spread_seconds": (
                    (max(timing_values) - min(timing_values)).total_seconds()
                    if len(timing_values) > 1
                    else 0
                ),
            },
            "historical_disagreement": self._store.latest(
                "disagreement", instrument_id=instrument_id, horizon=horizon
            ),
            "reasons": reasons,
            "recommended_response": response,
            "advisory_only": True,
            "executes_orders": False,
            "alignment": (
                "exact horizon and target; only forecasts generated within "
                "the reference tolerance are compared"
            ),
            "limitations": [
                "Disagreement is not evidence that any single model is correct.",
                "High disagreement can only inform WAIT/NO_ACTION through "
                "existing decision workflows.",
            ],
        }
        if persist:
            return self._store.save(
                "disagreement",
                payload_body,
                record_id=str(payload_body["disagreement_id"]),
                instrument_id=instrument_id,
                horizon=horizon,
                status=state,
                created_at=created,
            )
        return payload_body

    @staticmethod
    def _interval_overlap(rows: Sequence[Mapping[str, Any]]) -> float | None:
        intervals = [
            (row["interval_lower"], row["interval_upper"])
            for row in rows
            if row.get("interval_lower") is not None and row.get("interval_upper") is not None
        ]
        if len(intervals) < 2:
            return None
        lower = max(item[0] for item in intervals)
        upper = min(item[1] for item in intervals)
        intersection = max(0.0, upper - lower)
        union = max(item[1] for item in intervals) - min(item[0] for item in intervals)
        return intersection / union if union > 0 else None

    # -- multi-horizon consistency ------------------------------------------
    def consistency(self, instrument_id: str, *, persist: bool = True) -> dict[str, Any]:
        instrument = _instrument(instrument_id)
        reader = self._reader_for_call()
        latest: dict[str, dict[str, Any]] = {}
        for horizon in HORIZONS:
            rows = self._current_forecasts(reader.forecasts(instrument, horizon, limit=20))
            if rows:
                rows.sort(
                    key=lambda row: _iso(row.get("generated_at") or row.get("created_at")),
                    reverse=True,
                )
                latest[horizon] = rows[0]
        ordered = sorted(latest, key=lambda item: _horizon_seconds(item))
        entries = []
        for horizon in ordered:
            row = latest[horizon]
            direction = str(row.get("direction") or "").lower() or predicted_direction(
                row, self._config.directional_threshold
            )
            upper_return = _number(row.get("upper_return"))
            lower_return = _number(row.get("lower_return"))
            entries.append(
                {
                    "horizon": horizon,
                    "family": HORIZON_FAMILY[horizon],
                    "direction": direction,
                    "predicted_return": _number(row.get("predicted_return")),
                    "target_at": _iso(row.get("target_at")),
                    "generated_at": _iso(row.get("generated_at") or row.get("created_at")),
                    "uncertainty": row.get("uncertainty") or "unknown",
                    "interval_width": (
                        upper_return - lower_return
                        if upper_return is not None and lower_return is not None
                        else None
                    ),
                }
            )
        pairs = []
        for left, right in zip(entries, entries[1:], strict=False):
            if not left["direction"] or not right["direction"]:
                state = "insufficient"
            elif left["direction"] == right["direction"]:
                state = "aligned"
            else:
                state = "horizon_divergence"
            target_alignment = bool(
                str(left.get("target_at") or "")
                and str(right.get("target_at") or "")
                and str(left.get("target_at")) <= str(right.get("target_at"))
            )
            pairs.append(
                {
                    "short_horizon": left["horizon"],
                    "long_horizon": right["horizon"],
                    "short_direction": left["direction"],
                    "long_direction": right["direction"],
                    "state": state,
                    "target_alignment": target_alignment,
                    "interpretation": (
                        "Different horizons have different objectives; opposite "
                        "directions are not automatically a contradiction."
                        if state == "horizon_divergence"
                        else "The horizons agree on direction."
                        if state == "aligned"
                        else "One or more horizons lack a compatible current forecast."
                    ),
                }
            )
        transitions = [
            pair
            for pair in pairs
            if pair["state"] == "horizon_divergence"
            and pair["short_direction"] in {"up", "down"}
            and pair["long_direction"] in {"up", "down"}
        ]
        status = STATUS_COMPLETED if len(entries) >= 2 else STATUS_INSUFFICIENT
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "consistency_id": "ficn_"
            + hashlib.sha256(
                f"{instrument}|{_iso(self._clock())}|{len(entries)}".encode()
            ).hexdigest()[:24],
            "created_at": _iso(self._clock()),
            "instrument_id": instrument,
            "horizons": entries,
            "pairs": pairs,
            "potential_trend_transitions": transitions,
            "status": status,
            "directional_agreement": (
                sum(1 for pair in pairs if pair["state"] == "aligned") / len(pairs)
                if pairs
                else None
            ),
            "regime": reader.regime(instrument),
            "interpretation": (
                "Cross-horizon directions are evaluated separately; a 5-minute "
                "view and a 90-day view may legitimately disagree."
            ),
            "limitations": [
                "Forecasts are compared only when they share an instrument; "
                "target ordering is preserved.",
                "Different horizons can reflect different objectives and are "
                "not treated as a prediction error.",
            ],
        }
        if persist:
            return self._store.save(
                "consistency",
                body,
                record_id=str(body["consistency_id"]),
                instrument_id=instrument,
                horizon="",
                status=status,
                created_at=body["created_at"],
            )
        return body

    # -- drift and degradation ----------------------------------------------
    def drift(
        self,
        payload: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        model_id = str(payload.get("model_id") or "").strip()
        reference = payload.get("reference_features")
        current = payload.get("current_features")
        if reference is not None and not isinstance(reference, Mapping):
            raise ForecastIntelligenceValidationError("reference_features must be an object")
        if current is not None and not isinstance(current, Mapping):
            raise ForecastIntelligenceValidationError("current_features must be an object")

        def clean_features(value: Mapping[str, Any] | None) -> dict[str, list[float]]:
            result: dict[str, list[float]] = {}
            for name, values in (value or {}).items():
                if not isinstance(values, (list, tuple)):
                    raise ForecastIntelligenceValidationError(f"feature {name} must be an array")
                cleaned = [number for item in values if (number := finite(item)) is not None]
                result[str(name)] = cleaned[:1000]
            return result

        reader = self._reader_for_call()
        records = self._evaluation_records(
            reader, instrument_id=instrument_id, horizon=horizon, model_id=model_id
        )
        result = drift_analysis(
            records,
            reference_features=clean_features(reference),
            current_features=clean_features(current),
            config=self._config,
        )
        status = (
            "detected"
            if result["status"] == "detected"
            else STATUS_DRIFT_UNCONFIRMED
            if result["status"] == "drift_unconfirmed"
            else result["status"]
        )
        created = _iso(self._clock())
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "drift_id": "fidr_"
            + hashlib.sha256(
                f"{instrument_id}|{horizon}|{model_id}|{created}|{status}".encode()
            ).hexdigest()[:24],
            "created_at": created,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "model_id": model_id or "all",
            "category": "input_and_behaviour",
            "status": status,
            "detected": bool(result.get("detected")),
            "window_detected": bool(result.get("window_detected")),
            "detected_windows": int(result.get("detected_windows") or 0),
            "events": result.get("events", []),
            "unavailable": result.get("unavailable", []),
            "evidence": {
                "records": len(records),
                "methods": result.get("method"),
                "minimum_samples": result.get("minimum_samples"),
                "persistence_required": result.get("persistence_required"),
            },
            "severity": (
                "high"
                if result.get("detected")
                and any(event.get("severity") == "high" for event in result.get("events", []))
                else "watch"
                if result.get("detected")
                else "none"
            ),
            "review": (
                "Review feature/data pipeline and model health; do not "
                "deactivate a production model from one drift window."
            ),
            "limitations": result.get("limitations"),
            "automatic_deactivation": False,
        }
        if persist:
            return self._store.save(
                "drift",
                body,
                record_id=str(body["drift_id"]),
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model_id,
                status=status,
                created_at=created,
            )
        return body

    def degradation(
        self,
        payload: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        model_id = str(payload.get("model_id") or "").strip()
        reader = self._reader_for_call()
        records = self._evaluation_records(
            reader, instrument_id=instrument_id, horizon=horizon, model_id=model_id
        )
        regime_changed = False
        current_regime = str((reader.regime(instrument_id) or {}).get("primary_regime") or "")
        if records and current_regime:
            latest_regime = str(records[-1].get("regime") or "")
            regime_changed = bool(latest_regime and latest_regime != current_regime)
        result = degradation_analysis(records, config=self._config, regime_changed=regime_changed)
        created = _iso(self._clock())
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "degradation_id": "fideg_"
            + hashlib.sha256(
                f"{instrument_id}|{horizon}|{model_id}|{created}|{result['health']}".encode()
            ).hexdigest()[:24],
            "created_at": created,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "model_id": model_id or "all",
            "health": result["health"],
            "status": result["status"],
            "samples": result.get("samples", 0),
            "windows": result.get("windows", []),
            "mae_change_pct": result.get("mae_change_pct"),
            "accuracy_drop": result.get("accuracy_drop"),
            "coverage_drop": result.get("coverage_drop"),
            "worsening_windows": result.get("worsening_windows"),
            "persistence_required": result.get("persistence_required"),
            "regime_changed": result.get("regime_changed"),
            "detail": result.get("detail"),
            "limitations": result.get("limitations"),
            "automatic_deactivation": False,
        }
        if persist:
            return self._store.save(
                "degradation",
                body,
                record_id=str(body["degradation_id"]),
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model_id,
                status=str(result["health"]),
                created_at=created,
            )
        return body

    # -- weights -------------------------------------------------------------
    def weights(
        self,
        payload: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        regime = str(payload.get("regime") or "").strip()
        evidence, reader = self._evidence(instrument_id, horizon)
        rows: list[dict[str, Any]] = []
        usable = [
            model
            for model in evidence.values()
            if model.selection_metrics
            and model.validation_samples >= self._config.min_weight_samples
        ]
        errors = {
            model.model_id: _number(model.selection_metrics.get("mae"))
            for model in usable
            if _number(model.selection_metrics.get("mae")) is not None
        }
        ranked = sorted(
            (key for key in errors if errors[key] is not None),
            key=lambda key: errors[key] or 0.0,
        )
        current_default = 1.0 / max(1, len(evidence))
        for model in sorted(evidence.values(), key=lambda item: item.model_id):
            metrics = merge_metrics(model.selection_metrics)
            current = _number(model.calibration.get("weight")) if model.calibration else None
            if current is None:
                current = current_default
            rank = ranked.index(model.model_id) + 1 if model.model_id in ranked else None
            health = str(
                (self._degradation_for_evidence(model) or {}).get("health") or INSUFFICIENT_HEALTH
            )
            if model.validation_samples < self._config.min_weight_samples or not metrics:
                action = WEIGHT_INSUFFICIENT
                proposed = current
                reason = "validation evidence is below the weight minimum"
            elif health in {DEGRADED, "unsuitable"}:
                action = REMOVE_ELIGIBILITY
                proposed = 0.0
                reason = "model health is degraded or unsuitable; eligibility should be reviewed"
            elif rank is not None and rank <= max(1, math.ceil(len(ranked) / 3.0)):
                action = INCREASE_WEIGHT
                proposed = min(
                    self._config.weight_max,
                    current + self._config.weight_step * self._config.weight_smoothing,
                )
                reason = "top validation rank for this scope; bounded increase recommended"
            elif rank is not None and rank > math.floor(len(ranked) * 2.0 / 3.0):
                action = DECREASE_WEIGHT
                proposed = max(
                    self._config.weight_min,
                    current - self._config.weight_step * self._config.weight_smoothing,
                )
                reason = "bottom validation rank for this scope; bounded decrease recommended"
            elif health == HEALTHY:
                action = RETAIN_WEIGHT
                proposed = current
                reason = "validation rank is within the stable middle band"
            else:
                action = WEIGHT_INSUFFICIENT
                proposed = current
                reason = "health or calibration evidence is insufficient for a change"
            coverage = _number(metrics.get("interval_coverage"))
            ece = _number(metrics.get("calibration_error"))
            if action in {INCREASE_WEIGHT, RETAIN_WEIGHT} and (
                (coverage is not None and coverage < 0.50) or (ece is not None and ece > 0.20)
            ):
                action = DECREASE_WEIGHT
                proposed = max(
                    self._config.weight_min,
                    current - self._config.weight_step * self._config.weight_smoothing,
                )
                reason = "coverage or calibration guardrail failed; no increase is justified"
            rows.append(
                {
                    "recommendation_id": "fiw_"
                    + hashlib.sha256(
                        (
                            f"{instrument_id}|{horizon}|{model.model_id}|"
                            f"{model.version}|{_iso(self._clock())}"
                        ).encode()
                    ).hexdigest()[:24],
                    "model_id": model.model_id,
                    "version": model.version,
                    "action": action,
                    "current_weight": current,
                    "proposed_weight": proposed,
                    "validation_rank": rank,
                    "validation_samples": model.validation_samples,
                    "metrics": metrics,
                    "health": health,
                    "reason": reason,
                    "bounded_change": abs(proposed - current) <= self._config.weight_step,
                    "applied": False,
                }
            )
        created = _iso(self._clock())
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "weight_run_id": "fiwr_"
            + hashlib.sha256(f"{instrument_id}|{horizon}|{regime}|{created}".encode()).hexdigest()[
                :24
            ],
            "created_at": created,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "regime": regime or "all",
            "recommendations": rows,
            "applied": False,
            "production_weights_mutated": False,
            "method": (
                "bounded validation-rank adjustment with a stability cap; "
                "no test score and no single-period promotion"
            ),
            "limits": {
                "min_samples": self._config.min_weight_samples,
                "step": self._config.weight_step,
                "min_weight": self._config.weight_min,
                "max_weight": self._config.weight_max,
            },
            "journal": [{"action": row["action"], "model_id": row["model_id"]} for row in rows],
            "authority": self._authority(),
            "limitations": [
                "The recommendation is not applied to PI-08 production weights.",
                "Error correlation and full historical stability are reported "
                "only where the existing evidence exposes them.",
            ],
        }
        if persist:
            return self._store.save(
                "weights",
                body,
                record_id=str(body["weight_run_id"]),
                instrument_id=instrument_id,
                horizon=horizon,
                regime=regime,
                status="advisory",
                created_at=created,
            )
        return body

    def _degradation_for_evidence(self, model: ModelEvidence) -> dict[str, Any] | None:
        latest = self._store.latest(
            "degradation",
            instrument_id=model.instrument_id,
            horizon=model.horizon,
            model_id=model.model_id,
        )
        return latest

    # -- reliability ---------------------------------------------------------
    def reliability(
        self,
        payload: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        model_id = str(payload.get("model_id") or "").strip()
        evidence, reader = self._evidence(instrument_id, horizon)
        models = (
            [evidence[model_id]] if model_id and model_id in evidence else list(evidence.values())
        )
        if model_id and model_id not in evidence:
            raise ForecastIntelligenceNotFoundError(f"unknown model {model_id!r} for this scope")
        assessments = []
        for model in sorted(models, key=lambda item: item.model_id):
            metrics = merge_metrics(model.selection_metrics, model.performance_metrics)
            degradation = self._degradation_for_evidence(model) or {}
            drift = (
                self._store.latest(
                    "drift", instrument_id=instrument_id, horizon=horizon, model_id=model.model_id
                )
                or {}
            )
            accuracy = _number(metrics.get("directional_accuracy"))
            coverage = _number(metrics.get("interval_coverage"))
            ece = _number(metrics.get("calibration_error"))
            freshness = self._freshness(model, horizon)
            regime_supported = (
                not model.regimes
                or max(model.regimes.values(), default=0) >= self._config.min_regime_samples
            )
            label, evidence_rows = reliability_label(
                samples=max(
                    model.validation_samples, model.test_samples, int(metrics.get("samples") or 0)
                ),
                min_evaluation_samples=self._config.min_evaluation_samples,
                direction_accuracy=accuracy,
                interval_coverage=coverage,
                calibration_error=ece,
                health=str(degradation.get("health") or INSUFFICIENT_HEALTH),
                drift=bool(drift.get("detected")),
                data_fresh=not freshness["stale"],
                regime_supported=regime_supported,
            )
            assessments.append(
                {
                    "reliability_id": "fir_"
                    + hashlib.sha256(
                        f"{instrument_id}|{horizon}|{model.model_id}|{_iso(self._clock())}".encode()
                    ).hexdigest()[:24],
                    "model_id": model.model_id,
                    "version": model.version,
                    "family": model.family,
                    "classification": label,
                    "evidence": evidence_rows,
                    "metrics": metrics,
                    "health": degradation.get("health") or INSUFFICIENT_HEALTH,
                    "drift_detected": bool(drift.get("detected")),
                    "data_freshness": freshness,
                    "regime_samples": dict(model.regimes),
                    "calibration_status": model.calibration_status,
                    "sample_count": max(
                        model.validation_samples,
                        model.test_samples,
                        int(metrics.get("samples") or 0),
                    ),
                    "numeric_score": None,
                    "traceable": True,
                }
            )
        created = _iso(self._clock())
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "reliability_assessment_id": "fira_"
            + hashlib.sha256(
                f"{instrument_id}|{horizon}|{model_id}|{created}".encode()
            ).hexdigest()[:24],
            "created_at": created,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "model_id": model_id or "all",
            "assessments": assessments,
            "classification_only": True,
            "numeric_score": None,
            "policy_version": RELIABILITY_POLICY_VERSION,
            "no_probability_claim": True,
            "limitations": [
                "Reliability is a categorical evidence assessment, not a "
                "calibrated probability of correctness.",
                "A high classification is not a guarantee and does not authorize a trade.",
            ],
        }
        if persist:
            return self._store.save(
                "reliability",
                body,
                record_id=str(body["reliability_assessment_id"]),
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model_id,
                status="assessed",
                created_at=created,
            )
        return body

    # -- fallback ------------------------------------------------------------
    def fallback(
        self,
        payload: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        instrument_id, horizon = _scope(payload)
        preferred = str(payload.get("preferred_model_id") or payload.get("model_id") or "").strip()
        task = str(payload.get("task") or "return_regression").strip().lower()
        evidence, reader = self._evidence(instrument_id, horizon)
        forecasts = self._current_forecasts(
            reader.forecasts(instrument_id, horizon, limit=min(self._config.max_records, 200))
        )
        current: dict[str, dict[str, Any]] = {}
        for forecast in forecasts:
            model_id = str(forecast.get("model_id") or forecast.get("engine") or "")
            if not model_id:
                continue
            current.setdefault(model_id, forecast)
        selected: dict[str, Any] | None = None
        outcome = INSUFFICIENT_DATA_OUTCOME
        reasons: list[str] = []
        preferred_evidence = evidence.get(preferred) if preferred else None
        preferred_suitable = (
            self._suitability(preferred_evidence, task=task)
            if preferred_evidence is not None
            else None
        )
        if preferred and preferred not in evidence:
            reasons.append(f"preferred model {preferred} is not known for this scope")
        if preferred_evidence is not None:
            if self._model_state(preferred_evidence) != "eligible":
                reasons.append(f"preferred model state is {self._model_state(preferred_evidence)}")
            if preferred_evidence.family not in ALL_FAMILIES:
                reasons.append("preferred model family is unknown")
            if preferred_suitable and preferred_suitable["suitability"] == INSUFFICIENT_SUITABILITY:
                reasons.append("preferred model lacks sufficient evaluation evidence")
            degradation = self._degradation_for_evidence(preferred_evidence) or {}
            if degradation.get("health") in {DEGRADED, "unsuitable"}:
                reasons.append(f"preferred model health is {degradation.get('health')}")
            if (
                preferred_evidence
                and preferred in current
                and preferred_suitable
                and preferred_suitable["eligible"]
            ):
                outcome = PRIMARY_MODEL
                selected = {
                    "model_id": preferred,
                    "version": preferred_evidence.version,
                    "forecast_id": current[preferred].get("forecast_id"),
                    "reason": "preferred model is available, eligible and has a current forecast",
                }
        for model in sorted(evidence.values(), key=lambda item: item.model_id):
            if selected is not None:
                break
            if model.model_id == preferred:
                continue
            if model.model_id not in current:
                continue
            suitability = self._suitability(model, task=task)
            if suitability["eligible"] and self._model_state(model) == "eligible":
                outcome = ALTERNATIVE_MODEL
                selected = {
                    "model_id": model.model_id,
                    "version": model.version,
                    "forecast_id": current[model.model_id].get("forecast_id"),
                    "reason": (
                        "preferred model was unavailable or unsuitable; an "
                        "eligible alternative has a current forecast"
                    ),
                }
        if selected is None and "ensemble" in current:
            outcome = ENSEMBLE_FALLBACK
            selected = {
                "model_id": "ensemble",
                "version": str(
                    current["ensemble"].get("model_version")
                    or current["ensemble"].get("version")
                    or "PI-08"
                ),
                "forecast_id": current["ensemble"].get("forecast_id"),
                "reason": (
                    "no preferred or alternative model forecast was usable; "
                    "the PI-08 ensemble is available"
                ),
            }
        if selected is None:
            baselines = [
                model
                for model in evidence.values()
                if model.family in BASELINE_FAMILIES and model.model_id in current
            ]
            if baselines:
                baseline = sorted(baselines, key=lambda item: item.model_id)[0]
                outcome = BASELINE_FALLBACK_OUTCOME
                selected = {
                    "model_id": baseline.model_id,
                    "version": baseline.version,
                    "forecast_id": current[baseline.model_id].get("forecast_id"),
                    "reason": "only an established statistical baseline has a current forecast",
                }
        if selected is None:
            if forecasts:
                outcome = ABSTAINED_OUTCOME
                reasons.append(
                    "current forecasts exist, but no model passed suitability "
                    "and availability checks"
                )
            else:
                outcome = INSUFFICIENT_DATA_OUTCOME
                reasons.append("no current forecast is available for this scope")
        created = _iso(self._clock())
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI23_PHASE,
            "fallback_id": "fifb_"
            + hashlib.sha256(
                f"{instrument_id}|{horizon}|{preferred}|{created}|{outcome}".encode()
            ).hexdigest()[:24],
            "created_at": created,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "preferred_model_id": preferred or None,
            "outcome": outcome,
            "selected": selected,
            "reasons": reasons,
            "abstained": outcome == ABSTAINED_OUTCOME,
            "new_forecast_created": False,
            "authority": self._authority(),
            "evidence": {
                "models": len(evidence),
                "current_forecasts": len(current),
                "preferred_suitability": preferred_suitable,
            },
            "limitations": [
                "PI-23 does not manufacture a forecast to avoid abstention.",
                "A fallback is evidence for the existing decision workflow, "
                "not authorization for an order.",
            ],
        }
        if persist:
            return self._store.save(
                "fallback",
                body,
                record_id=str(body["fallback_id"]),
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=preferred,
                status=outcome,
                created_at=created,
            )
        return body

    # -- read views ----------------------------------------------------------
    def performance_overview(
        self, instrument_id: str = "", horizon: str = "", *, limit: int = 100
    ) -> dict[str, Any]:
        if instrument_id:
            instrument = _instrument(instrument_id)
            horizon_value = valid_horizon(horizon) if horizon else ""
        else:
            instrument = ""
            horizon_value = valid_horizon(horizon) if horizon else ""
        if instrument:
            body = self.evaluate(
                {
                    "instrument_id": instrument,
                    "horizon": horizon_value,
                    "task": "return_regression",
                },
                persist=False,
            )
            return {
                "ok": True,
                "evaluation": body,
                "models": body["models"],
                "data_status": body["data_status"],
            }
        history = self._store.list("evaluation", limit=limit)
        rows = []
        for report in history:
            for model in report.get("models", ()):
                rows.append(model)
        return {"ok": True, "models": rows[:limit], "evaluations": history[:limit]}

    def comparison(
        self, instrument_id: str, horizon: str, task: str = "return_regression"
    ) -> dict[str, Any]:
        instrument = _instrument(instrument_id)
        horizon_value = valid_horizon(horizon)
        evaluation = self.evaluate(
            {"instrument_id": instrument, "horizon": horizon_value, "task": task},
            persist=False,
        )
        selection = self.select(
            {"instrument_id": instrument, "horizon": horizon_value, "task": task},
            persist=False,
            evidence=None,
        )
        return {
            "ok": True,
            "instrument_id": instrument,
            "horizon": horizon_value,
            "task": task,
            "evaluation": evaluation,
            "selection": selection,
            "models": evaluation["models"],
        }

    def horizons(self, instrument_id: str, task: str = "return_regression") -> dict[str, Any]:
        instrument = _instrument(instrument_id)
        rows = []
        for horizon in HORIZONS:
            evaluation = self.evaluate(
                {"instrument_id": instrument, "horizon": horizon, "task": task},
                persist=False,
            )
            selection = self.select(
                {"instrument_id": instrument, "horizon": horizon, "task": task},
                persist=False,
            )
            rows.append(
                {
                    "horizon": horizon,
                    "family": HORIZON_FAMILY[horizon],
                    "status": evaluation["data_status"],
                    "supported": any(
                        model["suitability"]["suitability"]
                        in {STRONG_SUITABILITY, CONDITIONAL_SUITABILITY}
                        for model in evaluation["models"]
                    ),
                    "models": evaluation["models"],
                    "selection": selection,
                }
            )
        return {"ok": True, "instrument_id": instrument, "task": task, "horizons": rows}

    def instruments(
        self, instrument_ids: Sequence[str], horizon: str, task: str = "return_regression"
    ) -> dict[str, Any]:
        requested = [str(value) for value in instrument_ids[: self._config.max_instruments]]
        cleaned: list[str] = []
        invalid: list[dict[str, str]] = []
        for value in requested:
            try:
                instrument = _instrument(value)
            except ForecastIntelligenceValidationError as exc:
                invalid.append({"instrument_id": value, "reason": exc.message})
            else:
                if instrument not in cleaned:
                    cleaned.append(instrument)
        horizon_value = valid_horizon(horizon)
        rows = []
        for instrument in cleaned:
            evaluation = self.evaluate(
                {"instrument_id": instrument, "horizon": horizon_value, "task": task},
                persist=False,
            )
            rows.append(
                {
                    "instrument_id": instrument,
                    "status": evaluation["data_status"],
                    "models": evaluation["models"],
                    "selection": self.select(
                        {"instrument_id": instrument, "horizon": horizon_value, "task": task},
                        persist=False,
                    ),
                }
            )
        return {
            "ok": True,
            "horizon": horizon_value,
            "instruments": rows,
            "total": len(rows),
            "requested": len(requested),
            "invalid": invalid,
            "truncated": len(instrument_ids) > self._config.max_instruments,
        }

    def regimes(self, instrument_id: str, horizon: str) -> dict[str, Any]:
        instrument = _instrument(instrument_id)
        horizon_value = valid_horizon(horizon)
        reader = self._reader_for_call()
        records = self._evaluation_records(reader, instrument_id=instrument, horizon=horizon_value)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in records:
            grouped.setdefault(str(row.get("regime") or "uncertain"), []).append(row)
        slices = []
        for regime, values in sorted(grouped.items()):
            slices.append(
                {
                    "regime": regime,
                    "samples": len(values),
                    "sufficient": len(values) >= self._config.min_regime_samples,
                    "metrics": {
                        "regression": regression_metrics(values),
                        "classification": classification_metrics(values),
                        "probability": probability_metrics(
                            values, bins=self._config.calibration_bins
                        ),
                        "uncertainty": uncertainty_metrics(values),
                    },
                }
            )
        return {
            "ok": True,
            "instrument_id": instrument,
            "horizon": horizon_value,
            "regimes": slices,
            "minimum_samples": self._config.min_regime_samples,
            "limitations": [
                "Regime slices below the minimum are not reliable performance claims.",
                "Historical regime performance does not guarantee future performance.",
            ],
        }

    def registry(self, instrument_id: str = "", horizon: str = "") -> dict[str, Any]:
        reader = self._reader_for_call()
        instrument = _instrument(instrument_id) if instrument_id else ""
        horizon_value = valid_horizon(horizon) if horizon else ""
        if instrument or horizon_value:
            evidence = reader.training_evidence(instrument, horizon_value)
        else:
            evidence = reader.training_evidence("", "")
        models = list(evidence.values())
        rows = []
        for model in models:
            degradation = self._degradation_for_evidence(model) or {}
            rows.append(
                {
                    **model.to_dict(),
                    "deployment_status": (
                        "production"
                        if model.state == "production"
                        else "validated"
                        if model.state in {"eligible", "approved"}
                        and model.validation_samples >= self._config.min_selection_samples
                        else "experimental"
                        if model.state == "eligible"
                        else model.state
                    ),
                    "health": degradation.get("health")
                    or ("healthy" if model.selection_metrics else INSUFFICIENT_HEALTH),
                    "read_only": True,
                    "promotion_performed": False,
                    "reproducibility": {
                        "dataset_version": model.dataset_version,
                        "feature_set_version": model.feature_set_version,
                        "artifact_ref": model.artifact_ref,
                        "model_version": model.version,
                    },
                }
            )
        return {
            "ok": True,
            "models": rows,
            "total": len(rows),
            "scope": "scoped" if instrument or horizon_value else "all",
            "automatic_promotion": False,
            "production_mutation": False,
            "limitations": ["PI-23 does not promote, overwrite or roll back model artifacts."],
        }

    def history(
        self,
        *,
        instrument_id: str = "",
        horizon: str = "",
        kind: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._store.history(
            instrument_id=_instrument(instrument_id) if instrument_id else "",
            horizon=valid_horizon(horizon) if horizon else "",
            kind=kind,
            limit=min(max(int(limit), 1), 500),
        )

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PI23_PHASE,
            "store": self._store.integrity(),
            "counts": self._store.counts(),
            "sources": self.sources.to_dict(),
            "evidence_errors": self._reader.errors[:12],
            "authority": self._authority(),
        }

    def analyze(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Run the optimization coordinator once over one instrument/horizon."""
        instrument_id, horizon = _scope(payload)
        evaluation = self.evaluate(payload)
        selection = self.select(payload)
        calibration = self.calibrate(payload)
        uncertainty = self.uncertainty(payload)
        disagreement = self.disagreement(payload)
        consistency = self.consistency(instrument_id)
        drift = self.drift(payload)
        degradation = self.degradation(payload)
        weights = self.weights(payload)
        reliability = self.reliability(payload)
        fallback = self.fallback(payload)
        return {
            "ok": True,
            "phase": PI23_PHASE,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "results": {
                "evaluation": evaluation,
                "selection": selection,
                "calibration": calibration,
                "uncertainty": uncertainty,
                "disagreement": disagreement,
                "consistency": consistency,
                "drift": drift,
                "degradation": degradation,
                "weights": weights,
                "reliability": reliability,
                "fallback": fallback,
            },
            "automatic_promotion": False,
            "authority": self._authority(),
        }


__all__ = ["ForecastIntelligenceService"]
