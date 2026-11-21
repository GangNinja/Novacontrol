"""PI-16 adaptive-intelligence service.

This layer consumes existing evidence and never creates a second forecast,
regime detector, policy evaluator or training system.  Its outputs are research
recommendations: no method here can activate a policy, start training, submit an
order, change autonomy, mutate a portfolio or bypass NM-08/NM-10.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import detectors
from .models import (
    ADAPTIVE_CONTRACT_VERSION,
    ADAPTIVE_PHASE,
    ADAPTIVE_SCHEMA_VERSION,
    DETECTOR_VERSION,
    MAX_RECORDS,
    AdaptiveConfig,
    AdaptiveNotFoundError,
    AdaptiveResourceError,
    AdaptiveStateError,
    AdaptiveStoreError,
    AdaptiveValidationError,
    ChangeStatus,
    DegradationStatus,
    DriftStatus,
    EvidenceStrength,
    ReassessmentStatus,
    RecommendationAction,
    RecommendationStatus,
    RegimeSnapshot,
    SuitabilityStatus,
    canonical_hash,
    evaluation_age_days,
    mapping,
    optional_time,
    parse_time,
    regime_label,
    sequence,
    stable_id,
    text,
    utcnow,
    whole,
)
from .scope import describe_overlaps, reassessment_scope
from .store import AdaptiveStore

_ACTION_TEXT = {
    RecommendationAction.CONTINUE_MONITORING:
        "Continue monitoring; no research change is justified.",
    RecommendationAction.NO_ADAPTATION_REQUIRED:
        "No adaptation is required while evidence stays valid.",
    RecommendationAction.REEVALUATE_POLICY:
        "Re-evaluate the policy on current historical windows.",
    RecommendationAction.RUN_NEW_BACKTEST:
        "Run a new out-of-sample backtest before changing research direction.",
    RecommendationAction.RETRAIN_RECOMMENDED:
        "Prepare an explicitly authorised retraining experiment.",
    RecommendationAction.REASSESS_FEATURES:
        "Review feature distributions and forecast relationships.",
    RecommendationAction.REVIEW_REWARD_CONFIGURATION:
        "Review reward and cost assumptions before retraining.",
    RecommendationAction.POLICY_UNSUITABLE:
        "Do not rely on this policy for the affected regime.",
    RecommendationAction.INSUFFICIENT_EVIDENCE:
        "Collect more valid observations before adapting.",
}
_SEVERE_ACTIONS = {
    RecommendationAction.POLICY_UNSUITABLE.value,
    RecommendationAction.RETRAIN_RECOMMENDED.value,
}


def _strength(count: int, severe: bool = False) -> str:
    if count <= 0:
        return EvidenceStrength.NONE.value
    if severe or count >= 3:
        return EvidenceStrength.HIGH.value
    if count == 2:
        return EvidenceStrength.MEDIUM.value
    return EvidenceStrength.LOW.value


def _number(value: object, default: float | None = None) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return default
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _series(records: Sequence[Mapping[str, Any]], *keys: str) -> list[float]:
    values: list[float] = []
    for record in records:
        for key in keys:
            number = _number(record.get(key))
            if number is not None:
                values.append(number)
                break
    return values


def _features(records: Sequence[Mapping[str, Any]]) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for record in records:
        payload = record.get("features")
        if not isinstance(payload, Mapping):
            payload = {
                key: value
                for key, value in record.items()
                if key not in {"timestamp", "as_of", "available_at", "outcome", "outcome_at"}
            }
        for key, value in payload.items():
            number = _number(value)
            if number is not None:
                result.setdefault(str(key), []).append(number)
    return result


def _metric_number(node: object) -> float | None:
    """Read a PI-14 metric, honoring its declared unit and sufficiency.

    PI-14 wraps most metrics as ``{value, unit, sufficient, observations}``.
    A metric it marks insufficient is missing evidence here too, and a percent
    metric is converted to a fraction so one scaling rule applies to both plain
    fractions and PI-14 percentages.
    """
    if isinstance(node, Mapping):
        if node.get("sufficient") is False:
            return None
        value = _number(node)
        if value is None:
            return None
        if text(node.get("unit")).lower() == "percent":
            return value / 100.0
        return value
    return _number(node)


def _policy_metric(metrics: Mapping[str, Any], path: str) -> float | None:
    node: object = metrics
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return _metric_number(node)


def _is_fraction(value: float) -> bool:
    return abs(value) <= 1.0


def _scope_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    """The per-window scope fields a reassessment record carries."""
    return {
        "out_of_sample": item.get("out_of_sample"),
        "in_sample": bool(item.get("in_sample")),
        "overlaps": list(item.get("overlaps") or ()),
    }


class AdaptiveIntelligenceService:
    """Evidence-based regime suitability, drift and adaptation research."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        feature_service: Any | None = None,
        training_service: Any | None = None,
        reward_service: Any | None = None,
        config: AdaptiveConfig | Mapping[str, Any] | None = None,
        clock: Any | None = None,
    ) -> None:
        explicit = config is not None
        config = (
            config
            if isinstance(config, AdaptiveConfig)
            else AdaptiveConfig.from_mapping(config)
        )
        self._store = AdaptiveStore(path, max_records=config.max_records)
        self._feature_service = feature_service
        self._training_service = training_service
        self._reward_service = reward_service
        self._clock = clock or utcnow
        self._last_error = ""
        self._recovered = 0
        # A restart resumes the configuration the operator last persisted; only
        # an explicit argument (tests, tooling) overrides the stored one.
        restored = None if explicit else self._restore_config()
        self._config = restored or config
        self._store.max_records = max(1, min(self._config.max_records, MAX_RECORDS))
        try:
            self._store.save_config(
                {**self._config.to_dict(), "digest": self._config.digest}
            )
        except AdaptiveStoreError as exc:
            # Startup must not fail on an unpersistable configuration; the
            # in-memory configuration still governs this process.  A restore
            # problem is already recorded and is the more useful message.
            self._last_error = self._last_error or (
                f"active configuration was not persisted: {exc}"
            )
        self._recover_interrupted()

    def _restore_config(self) -> AdaptiveConfig | None:
        """Read the most recently persisted configuration, if one is usable."""
        try:
            records = self._store.configs(limit=1)
        except Exception as exc:  # noqa: BLE001 - startup must not fail here
            self._last_error = f"config restore failed: {type(exc).__name__}: {exc}"
            return None
        if not records:
            return None
        allowed = set(AdaptiveConfig.__dataclass_fields__)
        payload = {str(key): value for key, value in records[0].items() if key in allowed}
        try:
            return AdaptiveConfig.from_mapping(payload)
        except Exception as exc:  # noqa: BLE001 - a bad record falls back to defaults
            self._last_error = (
                f"persisted adaptive config {records[0].get('config_id')!r} could not be "
                f"restored: {type(exc).__name__}: {exc}"
            )
            return None

    # -- introspection -------------------------------------------------------
    @property
    def settings(self) -> AdaptiveConfig:
        return self._config

    @property
    def store(self) -> AdaptiveStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": ADAPTIVE_PHASE,
            "implemented": True,
            "initialised": True,
            "contract_version": ADAPTIVE_CONTRACT_VERSION,
            "schema_version": ADAPTIVE_SCHEMA_VERSION,
            "detector_version": DETECTOR_VERSION,
            "config": self._config.to_dict(),
            "counts": self._store.counts(),
            "sources": {
                "regime": "PI-04 RegimeDetector / RegimeResult",
                "forecasts": "PI-08/PI-10 forecast and uncertainty contracts",
                "evaluation": "PI-11 / PI-14 policy-evaluation evidence",
                "policies": "PI-15 experimental policy artifacts",
            },
            "advisory_only": True,
            "experimental": True,
            "production_authority": False,
            "activation_available": False,
            "automatic_training": False,
            "automatic_promotion": False,
            "last_error": self._last_error,
            "recovered": self._recovered,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": ADAPTIVE_PHASE,
            "contract_version": ADAPTIVE_CONTRACT_VERSION,
            "suitability_statuses": [item.value for item in SuitabilityStatus],
            "change_statuses": [item.value for item in ChangeStatus],
            "drift_statuses": [item.value for item in DriftStatus],
            "degradation_statuses": [item.value for item in DegradationStatus],
            "recommendation_actions": [item.value for item in RecommendationAction],
            "detectors": {
                "distribution": ["psi", "kolmogorov_smirnov", "jensen_shannon"],
                "sequential": ["page_hinkley", "cusum"],
                "relationship": ["fisher_z_correlation_change", "rolling_error_ratio"],
                "action_behavior": ["total_variation_action_shift"],
            },
            "rationale": (
                "The selected detectors cover distribution, sequential-error and "
                "input-outcome relationship changes without a new numerical runtime. "
                "They report sample size and parameters rather than a single opaque score."
            ),
            "thresholds": self._config.to_dict(),
            "response_fields": [
                "recommendation_id",
                "affected_policy",
                "scope",
                "detection_timestamp",
                "supporting_evidence",
                "triggering_metrics",
                "evidence_strength",
                "recommended_next_action",
                "data_versions",
                "model_and_policy_versions",
                "advisory_only",
                "production_authority",
            ],
            "guarantees": [
                "no policy activation or promotion",
                "no automatic retraining",
                "no order, fill, portfolio, ledger or autonomy mutation",
                "NM-08 remains the sole risk authority",
                "NM-10 remains the sole paper-execution authority",
                "future outcomes are rejected before drift calculations",
                (
                    "reassessment windows overlapping the policy's training or "
                    "checkpoint-selection data are refused unless explicitly flagged, "
                    "and flagged in-sample evidence never feeds suitability"
                ),
            ],
            "limitations": [
                "Historical regime slices are observational, not causal.",
                "Small regime samples remain explicitly insufficient.",
                "Thresholds are configurable research defaults, not validated trading rules.",
                "No claim of future profitability is made.",
            ],
        }

    def health(self) -> dict[str, Any]:
        try:
            storage = self._store.storage_info()
            counts = self._store.counts()
        except Exception as exc:  # noqa: BLE001
            return {
                "state": "error",
                "healthy": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        return {
            "state": "ok",
            "healthy": True,
            "storage": storage,
            "counts": counts,
            "sources_available": {
                "regime": self._feature_service is not None,
                "policies": self._training_service is not None,
                "evaluation": self._reward_service is not None,
            },
            "last_error": self._last_error,
        }

    def dashboard(self) -> dict[str, Any]:
        changes = self._store.changes(limit=6)["items"]
        drifts = self._store.drift(limit=6)["items"]
        degradation = self._store.degradation(limit=6)["items"]
        recommendations = self._store.recommendations(limit=6)["items"]
        retraining = self._store.retraining(limit=6)["items"]
        suitability = self._store.suitability(limit=8)["items"]
        latest_regime = None
        try:
            regimes = [
                item
                for item in self._store.annotations(limit=100)["items"]
                if item.get("type") == "regime_snapshot"
            ]
            regimes.sort(
                key=lambda item: text(
                    item.get("source_timestamp") or item.get("available_at")
                ),
                reverse=True,
            )
            latest_regime = regimes[0] if regimes else None
        except Exception:  # noqa: BLE001
            latest_regime = None
        return {
            "ok": True,
            "phase": ADAPTIVE_PHASE,
            "as_of": self._now().isoformat(),
            "latest_regime": latest_regime,
            "suitability": suitability,
            "changes": changes,
            "drift": drifts,
            "degradation": degradation,
            "recommendations": recommendations,
            "retraining": retraining,
            "counts": self._store.counts(),
            "advisory_only": True,
            "production_authority": False,
            "activation_available": False,
        }

    # -- configuration -------------------------------------------------------
    def _now(self) -> datetime:
        value = self._clock()
        return parse_time(value, "clock")

    def _recover_interrupted(self) -> None:
        """A process restart cannot leave a synchronous reassessment running."""
        try:
            rows = self._store.reassessments(
                status=ReassessmentStatus.RUNNING.value, limit=500
            )["items"]
            for row in rows:
                policy_id = text(row.get("policy_id"))
                updated = {
                    **row,
                    "status": ReassessmentStatus.FAILED.value,
                    "finished_at": self._now().isoformat(),
                    "error": "reassessment interrupted by process restart",
                    "evaluation_triggered": False,
                    "recovered_from_interruption": True,
                }
                self._store.save_reassessment(
                    text(row.get("reassessment_id")), policy_id, updated["status"], updated
                )
                self._recovered += 1
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"recovery failed: {type(exc).__name__}: {exc}"

    def create_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        config = AdaptiveConfig.from_mapping(payload)
        created = self._store.config(config.config_id) is None
        stored = self._store.save_config({**config.to_dict(), "digest": config.digest})
        self._config = config
        self._store.max_records = max(1, min(config.max_records, MAX_RECORDS))
        return {
            "ok": True,
            "config": stored,
            "created": created,
            "advisory_only": True,
        }

    def configs(self, limit: int = 50) -> dict[str, Any]:
        return {"ok": True, "configurations": self._store.configs(limit)}

    def config_record(self, config_id: str) -> dict[str, Any]:
        found = self._store.config(config_id)
        if found is None:
            raise AdaptiveNotFoundError(f"unknown adaptive config {config_id!r}")
        return found

    # -- PI-04 regime integration -------------------------------------------
    def regime_analysis(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        instrument_id = text(payload.get("instrument_id")).upper()
        explicit = payload.get("regimes")
        if explicit is not None:
            if not isinstance(explicit, Sequence) or isinstance(explicit, (str, bytes)):
                raise AdaptiveValidationError("regimes must be a list")
            if len(explicit) > 500:
                raise AdaptiveValidationError("at most 500 regime records may be analyzed")
            records = [mapping(item, "regime") for item in explicit]
            current_raw = (
                max(
                    records,
                    key=lambda item: parse_time(item.get("as_of"), "as_of"),
                )
                if records
                else None
            )
            history_raw = records
        else:
            if not instrument_id:
                raise AdaptiveValidationError("instrument_id is required")
            if self._feature_service is None:
                return {
                    "ok": True,
                    "status": "DATA_UNAVAILABLE",
                    "instrument_id": instrument_id,
                    "current": None,
                    "history": [],
                    "reason": "no PI-04 feature service is attached",
                    "advisory_only": True,
                }
            latest = self._feature_service.latest_regime(instrument_id)
            if latest is None:
                return {
                    "ok": True,
                    "status": "DATA_UNAVAILABLE",
                    "instrument_id": instrument_id,
                    "current": None,
                    "history": [],
                    "reason": "no persisted PI-04 regime exists for this instrument",
                    "advisory_only": True,
                }
            current_dict = latest.to_dict() if hasattr(latest, "to_dict") else dict(latest)
            history_payload = self._feature_service.regimes(
                instrument_id=instrument_id, limit=max(2, min(int(payload.get("limit", 20)), 100))
            )
            history_raw = list(history_payload.get("items") or [])
            current_raw = current_dict
        if current_raw is None:
            return {
                "ok": True,
                "status": "DATA_UNAVAILABLE",
                "instrument_id": instrument_id,
                "current": None,
                "history": [],
                "advisory_only": True,
            }
        if not instrument_id:
            instrument_id = text(current_raw.get("instrument_id")).upper()
        current = RegimeSnapshot.from_mapping(current_raw)
        as_of_limit = parse_time(payload.get("as_of") or self._now(), "as_of")
        if current.as_of > as_of_limit:
            raise AdaptiveValidationError(
                "the selected regime observation is timestamped after as_of"
            )
        history = [RegimeSnapshot.from_mapping(item) for item in history_raw if item]
        history = sorted(history, key=lambda item: (item.as_of, item.regime_id))
        history = [item for item in history if item.as_of <= as_of_limit]
        transitions: list[dict[str, Any]] = []
        for previous, current_item in zip(history, history[1:], strict=False):
            if previous.primary != current_item.primary:
                transitions.append(
                    {
                        "at": current_item.as_of.isoformat(),
                        "previous": previous.primary,
                        "current": current_item.primary,
                        "regime_id": current_item.regime_id,
                        "source": current_item.source,
                    }
                )
        analysis = {
            "analysis_id": stable_id("adpr_", instrument_id, current.regime_id),
            "instrument_id": instrument_id,
            "as_of": self._now().isoformat(),
            "source_timestamp": current.as_of.isoformat(),
            "source": current.source,
            "regime_id": current.regime_id,
            "feature_set_version": current.feature_set_version,
            "regime_version": current.method_version,
            "primary_regime": current.primary,
            "secondary_conditions": list(current.secondary),
            "confidence": current.confidence,
            "confidence_calibrated": current.confidence_calibrated,
            "supporting_features": list(current.supporting_features),
            "evidence": dict(current.evidence),
            "quality": current.quality,
            "reasons": list(current.reasons),
            "history": [item.to_dict() for item in history[-50:]],
            "recent_transitions": transitions[-20:],
            "status": "AVAILABLE" if current.primary != "uncertain" else "UNCERTAIN",
            "uncertainty_preserved": current.primary == "uncertain" or current.confidence is None,
            "advisory_only": True,
            "production_authority": False,
        }
        annotation_id = stable_id("adpa_", "snapshot", instrument_id, current.regime_id)
        self._store.save_annotation(
            annotation_id,
            instrument_id,
            "regime_snapshot",
            {
                "annotation_id": annotation_id,
                "type": "regime_snapshot",
                "instrument_id": instrument_id,
                "source_timestamp": current.as_of.isoformat(),
                "available_at": (current.available_at or current.as_of).isoformat(),
                "regime_id": current.regime_id,
                "primary_regime": current.primary,
                "secondary_conditions": list(current.secondary),
                "regime_version": current.method_version,
                "feature_set_version": current.feature_set_version,
                "analysis": analysis,
                "derived_annotation": True,
                "mutates_experience": False,
                "advisory_only": True,
            },
        )
        return analysis

    def latest_regime(self, instrument_id: str) -> dict[str, Any]:
        normalized = text(instrument_id).upper()
        records = self._store.annotations(normalized, limit=500)["items"]
        snapshots = [item for item in records if item.get("type") == "regime_snapshot"]
        snapshots.sort(
            key=lambda item: text(item.get("source_timestamp") or item.get("available_at")),
            reverse=True,
        )
        if not snapshots:
            return {
                "ok": True,
                "instrument_id": normalized,
                "available": False,
                "status": "DATA_UNAVAILABLE",
                "advisory_only": True,
            }
        return {
            "ok": True,
            "instrument_id": normalized,
            "available": True,
            **snapshots[0],
        }

    def annotate_regimes(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Version derived regime labels without editing original transitions."""
        instrument_id = text(payload.get("instrument_id")).upper()
        if not instrument_id:
            raise AdaptiveValidationError("instrument_id is required")
        regime = regime_label(payload.get("regime") or payload.get("primary_regime"))
        as_of = parse_time(payload.get("as_of"), "as_of")
        available_at = optional_time(payload.get("available_at"), "available_at") or as_of
        if available_at > as_of:
            raise AdaptiveValidationError("annotation availability cannot follow as_of")
        source_timestamp = parse_time(
            payload.get("source_timestamp"), "source_timestamp"
        ) if payload.get("source_timestamp") else as_of
        if source_timestamp > as_of:
            raise AdaptiveValidationError("source_timestamp cannot follow as_of")
        source_regime_id = text(payload.get("source_regime_id") or payload.get("regime_id"))
        experiences = sequence(payload.get("experiences"))
        if len(experiences) > 5000:
            raise AdaptiveValidationError("at most 5000 experience ids may be annotated")
        versions = sequence(payload.get("regime_versions"))
        annotation_version = text(payload.get("annotation_version"), "v1")
        annotation_id = stable_id(
            "adpn_", instrument_id, regime, as_of.isoformat(), source_regime_id, annotation_version
        )
        record = {
            "annotation_id": annotation_id,
            "type": "experience_annotation",
            "instrument_id": instrument_id,
            "regime": regime,
            "secondary_conditions": [
                regime_label(item) for item in sequence(payload.get("secondary_conditions"))
            ],
            "as_of": as_of.isoformat(),
            "available_at": available_at.isoformat(),
            "source_regime_id": source_regime_id,
            "source_timestamp": source_timestamp.isoformat(),
            "annotation_version": annotation_version,
            "regime_versions": versions,
            "experience_ids": [
                text(item.get("experience_id") if isinstance(item, Mapping) else item)
                for item in experiences
                if text(item.get("experience_id") if isinstance(item, Mapping) else item)
            ],
            "derived_annotation": True,
            "original_records_mutated": False,
            "no_future_leakage": True,
            "advisory_only": True,
        }
        stored, inserted = self._store.save_annotation(
            annotation_id, instrument_id, "available", record
        )
        return {"ok": True, "annotation": stored, "inserted": inserted, "idempotent": True}

    def annotations(
        self, instrument_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return {
            "ok": True,
            **self._store.annotations(instrument_id.upper(), limit=limit, offset=offset),
        }

    def regime_experiences(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Retrieve only experiences whose regime evidence existed at ``as_of``."""
        as_of = parse_time(payload.get("as_of"), "as_of")
        regime = regime_label(payload.get("regime")) if payload.get("regime") else ""
        instrument_id = text(payload.get("instrument_id")).upper()
        explicit = payload.get("experiences")
        source: list[dict[str, Any]]
        if explicit is not None:
            if not isinstance(explicit, Sequence) or isinstance(explicit, (str, bytes)):
                raise AdaptiveValidationError("experiences must be a list")
            if len(explicit) > 5000:
                raise AdaptiveValidationError("at most 5000 experiences may be queried")
            source = [mapping(item, "experience") for item in explicit]
        else:
            annotations = self._store.annotations(instrument_id, limit=500)["items"]
            source = [
                {
                    "experience_id": item.get("annotation_id"),
                    "regime": item.get("regime") or item.get("primary_regime"),
                    "as_of": item.get("as_of"),
                    "available_at": item.get("available_at"),
                    "annotation": item,
                }
                for item in annotations
                if item.get("type") == "experience_annotation"
            ]
        selected: list[dict[str, Any]] = []
        excluded_future = 0
        for item in source:
            available_raw = item.get("available_at") or item.get("as_of") or item.get("timestamp")
            if not available_raw:
                continue
            available = parse_time(available_raw, "available_at")
            if available > as_of:
                excluded_future += 1
                continue
            item_regime = regime_label(
                item.get("regime") or item.get("primary_regime") or "uncertain"
            )
            if regime and item_regime != regime:
                continue
            if instrument_id and text(item.get("instrument_id")).upper() not in {"", instrument_id}:
                continue
            selected.append({**item, "retrieved_regime": item_regime})
        return {
            "ok": True,
            "as_of": as_of.isoformat(),
            "regime": regime or None,
            "experiences": selected,
            "count": len(selected),
            "excluded_future": excluded_future,
            "leakage_prevented": True,
            "historical_records_mutated": False,
            "advisory_only": True,
        }

    # -- regime-specific policy evidence ------------------------------------
    def _policy(self, policy_id: str) -> dict[str, Any] | None:
        if not policy_id or self._training_service is None:
            return None
        try:
            policy = self._training_service.policy(policy_id)
        except Exception as exc:  # noqa: BLE001
            raise AdaptiveNotFoundError(
                f"unknown or unavailable PI-15 policy {policy_id!r}: {exc}"
            ) from exc
        return dict(policy)

    def _extract_metrics(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw = payload.get("metrics")
        if isinstance(raw, Mapping):
            metrics = dict(raw)
        else:
            metrics = {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "policy_id",
                    "policy_version",
                    "regime",
                    "evaluation_id",
                    "evaluated_at",
                    "dataset_version",
                    "regime_version",
                    "start",
                    "end",
                    "sample_size",
                    "notes",
                }
            }
        financial = dict(metrics.get("financial") or {})
        risk = dict(metrics.get("risk") or {})
        trading = dict(metrics.get("trading") or {})
        decision = dict(metrics.get("decision") or {})
        if not financial and not risk:
            financial = {
                "total_return": _number(metrics.get("total_return")),
                "realized_pnl": _number(metrics.get("realized_pnl")),
                "unrealized_pnl": _number(metrics.get("unrealized_pnl")),
                "observations": _number(metrics.get("observations")),
            }
            risk = {
                "max_drawdown": _number(metrics.get("max_drawdown")),
                "volatility": _number(metrics.get("volatility")),
                "sharpe": _number(metrics.get("sharpe")),
                "sortino": _number(metrics.get("sortino")),
            }
        return {
            "financial": financial,
            "risk": risk,
            "trading": trading,
            "decision": decision,
            "raw": metrics,
        }

    def record_regime_evaluation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        policy_id = text(payload.get("policy_id"))
        if not policy_id:
            raise AdaptiveValidationError("policy_id is required")
        policy = self._policy(policy_id) or {}
        evaluation_id = text(payload.get("evaluation_id"))
        evaluation: dict[str, Any] = {}
        if evaluation_id and self._reward_service is not None:
            try:
                evaluation = dict(self._reward_service.evaluation(evaluation_id))
            except Exception as exc:  # noqa: BLE001
                raise AdaptiveNotFoundError(
                    f"unknown PI-14/PI-15 evaluation {evaluation_id!r}: {exc}"
                ) from exc
        if evaluation and not payload.get("metrics"):
            candidate_results = evaluation.get("results")
            if not isinstance(candidate_results, Sequence):
                candidate_results = [evaluation]
            chosen = None
            for row in candidate_results:
                if not isinstance(row, Mapping):
                    continue
                metadata = row.get("policy_metadata") or {}
                if (
                    text(row.get("policy_name")) == text(policy.get("name"))
                    or text(metadata.get("policy_id") if isinstance(metadata, Mapping) else "")
                    == policy_id
                ):
                    chosen = dict(row)
                    break
            if chosen is None and candidate_results:
                chosen = dict(candidate_results[-1])
            evaluation = chosen or evaluation
            payload = {**evaluation, **dict(payload), "metrics": evaluation.get("metrics") or {}}
        regime = regime_label(payload.get("regime") or payload.get("primary_regime"))
        metrics = self._extract_metrics(payload)
        sample_size = whole(
            payload.get(
                "sample_size",
                _policy_metric(metrics, "financial.observations")
                or _policy_metric(metrics, "trading.total_trades")
                or 0,
            ),
            "sample_size",
            maximum=1_000_000,
        )
        evaluated_at = parse_time(
            payload.get("evaluated_at") or payload.get("created_at") or self._now(),
            "evaluated_at",
        )
        if evaluated_at > self._now():
            raise AdaptiveValidationError(
                "an evaluation cannot be timestamped in the future"
            )
        window_candidate = payload.get("window")
        window_raw = window_candidate if isinstance(window_candidate, Mapping) else {}
        start = payload.get("start") or window_raw.get("start")
        end = payload.get("end") or window_raw.get("end")
        parsed_start = parse_time(start, "start") if start else None
        parsed_end = parse_time(end, "end") if end else None
        if parsed_start is not None and parsed_end is not None and parsed_start >= parsed_end:
            raise AdaptiveValidationError("start must precede end")
        if parsed_end is not None and parsed_end > evaluated_at:
            raise AdaptiveValidationError(
                "an evaluation window cannot end after the evaluation timestamp"
            )
        declared_out_of_sample = payload.get("out_of_sample")
        declared_in_sample = bool(payload.get("in_sample", False))
        if (
            parsed_start is not None
            and parsed_end is not None
            and declared_out_of_sample is None
            and not declared_in_sample
        ):
            # Evidence recorded without a declared scope is classified against
            # the policy's own split windows rather than assumed clean.
            derived = reassessment_scope(
                policy,
                [{"start": parsed_start.isoformat(), "end": parsed_end.isoformat()}],
            )
            derived_window = derived["windows"][0]
            out_of_sample: bool | None = derived_window["out_of_sample"]
            in_sample = bool(derived_window["in_sample"])
            overlaps = list(derived_window["overlaps"])
            scope_verified = derived["verified"]
        else:
            out_of_sample = (
                None if declared_out_of_sample is None else bool(declared_out_of_sample)
            )
            overlaps = [
                dict(item)
                for item in payload.get("overlaps") or ()
                if isinstance(item, Mapping)
            ]
            in_sample = declared_in_sample or out_of_sample is False
            scope_verified = bool(payload.get("scope_verified", False))
        requested_version = text(payload.get("policy_version"))
        current_version = text(policy.get("artifact_version") or policy.get("artifact_hash"))
        if requested_version and current_version and requested_version != current_version:
            raise AdaptiveValidationError(
                "policy_version does not match the current PI-15 artifact"
            )
        record_id = stable_id(
            "ade_",
            policy_id,
            text(policy.get("artifact_hash") or policy.get("artifact_version")),
            regime,
            text(evaluation_id or canonical_hash(metrics)),
            text(start),
            text(end),
            text(payload.get("dataset_version")),
        )
        sufficient = sample_size >= self._config.min_regime_samples
        record = {
            "regime_evaluation_id": record_id,
            "policy_id": policy_id,
            "policy_version": text(
                requested_version or policy.get("artifact_version") or policy.get("artifact_hash")
            ),
            "policy_status": text(policy.get("status")),
            "regime": regime,
            "regime_version": text(
                payload.get("regime_version") or policy.get("environment_version")
            ),
            "evaluation_id": evaluation_id,
            "evaluated_at": evaluated_at.isoformat(),
            "start": parsed_start.isoformat() if parsed_start else None,
            "end": parsed_end.isoformat() if parsed_end else None,
            "dataset_version": text(
                payload.get("dataset_version")
                or (policy.get("dataset_provenance") or {}).get("dataset_version")
            ),
            "feature_set_version": text(
                payload.get("feature_set_version")
                or (policy.get("dataset_provenance") or {}).get("feature_set_version")
            ),
            "config_hash": text(payload.get("config_hash") or policy.get("config_hash")),
            "evaluation_config": dict(payload.get("evaluation_config") or {}),
            "sample_size": sample_size,
            "minimum_sample": self._config.min_regime_samples,
            "sufficient": sufficient,
            "out_of_sample": out_of_sample,
            "in_sample": in_sample,
            "scope_verified": scope_verified,
            "training_overlap": overlaps,
            "status": "evaluated" if sufficient else "insufficient_evidence",
            "metrics": metrics,
            "financial": metrics["financial"],
            "risk": metrics["risk"],
            "trading": metrics["trading"],
            "decision": metrics["decision"],
            "action_distribution": dict(payload.get("action_distribution") or {}),
            "forecast_uncertainty": _number(
                payload.get("forecast_uncertainty") or metrics["raw"].get("forecast_uncertainty")
            ),
            "source": "PI-15 policy + PI-14 evaluation" if evaluation_id else "explicit evidence",
            "simulated": True,
            "experimental": True,
            "advisory_only": True,
            "production_authority": False,
        }
        stored, inserted = self._store.save_regime_evaluation(
            record_id, policy_id, record["status"], record
        )
        return {
            "ok": True,
            "evaluation": stored,
            "inserted": inserted,
            "sample_warning": (
                None
                if sufficient
                else f"{sample_size} observations is below the required "
                f"{self._config.min_regime_samples}"
            ),
            "advisory_only": True,
        }

    def regime_performance(
        self, policy_id: str = "", *, regime: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        rows = self._store.regime_evaluations(policy_id, limit=500)["items"]
        if regime:
            rows = [row for row in rows if row.get("regime") == regime_label(regime)]
        offset = max(0, int(offset))
        page_size = max(1, min(int(limit), 500))
        rows = rows[offset : offset + page_size]
        by_regime: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_regime.setdefault(str(row.get("regime")), []).append(row)
        summary = {
            key: {
                "evaluations": len(items),
                "sufficient": sum(1 for item in items if item.get("sufficient")),
                "total_return_values": [
                    _number((item.get("financial") or {}).get("total_return")) for item in items
                ],
            }
            for key, items in by_regime.items()
        }
        return {
            "ok": True,
            "policy_id": policy_id,
            "regime": regime or None,
            "evaluations": rows,
            "summary": summary,
            "advisory_only": True,
            "production_authority": False,
        }

    # -- market-condition change --------------------------------------------
    def _window(self, payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
        raw = payload.get(key)
        if raw is None:
            return []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise AdaptiveValidationError(f"{key} must be a list of observations")
        observations: list[dict[str, Any]] = []
        limit = self._config.max_observations
        if len(raw) > limit:
            raise AdaptiveValidationError(f"{key} exceeds {limit} observations")
        for index, item in enumerate(raw):
            record = mapping(item, f"{key}[{index}]")
            timestamp = record.get("timestamp") or record.get("as_of")
            if timestamp is not None:
                record["timestamp"] = parse_time(timestamp, f"{key}[{index}].timestamp").isoformat()
            observations.append(record)
        timestamps = [
            parse_time(item["timestamp"]) for item in observations if item.get("timestamp")
        ]
        if len(timestamps) != len(observations):
            raise AdaptiveValidationError(f"{key} observations require timestamps")
        if timestamps != sorted(timestamps):
            raise AdaptiveValidationError(f"{key} must be chronological")
        return observations

    def detect_market_change(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        instrument_id = text(payload.get("instrument_id")).upper()
        if not instrument_id:
            raise AdaptiveValidationError("instrument_id is required")
        reference = self._window(payload, "reference")
        current = self._window(payload, "current")
        if not reference or not current:
            result = self._change_result(
                instrument_id,
                ChangeStatus.DATA_UNAVAILABLE,
                [],
                reason="both reference and current observations are required",
                as_of=payload.get("as_of"),
            )
            return result
        if reference[-1]["timestamp"] > current[0]["timestamp"]:
            raise AdaptiveValidationError(
                "reference observations must precede current observations"
            )
        if payload.get("as_of") is not None:
            as_of = parse_time(payload.get("as_of"), "as_of")
            if as_of > self._now():
                raise AdaptiveValidationError("as_of cannot be in the future")
            if parse_time(current[-1]["timestamp"], "current end") > as_of:
                raise AdaptiveValidationError(
                    "current observations extend beyond the requested as_of"
                )
        ref_returns = _series(reference, "returns", "return")
        cur_returns = _series(current, "returns", "return")
        if (
            len(ref_returns) < self._config.min_observations
            or len(cur_returns) < self._config.min_observations
        ):
            return self._change_result(
                instrument_id,
                ChangeStatus.INSUFFICIENT_EVIDENCE,
                [],
                reason=(
                    f"returns require at least {self._config.min_observations} observations "
                    "in each window"
                ),
                as_of=payload.get("as_of") or current[-1]["timestamp"],
                sample_sizes={"reference": len(ref_returns), "current": len(cur_returns)},
            )
        signals: list[dict[str, Any]] = []
        psi = detectors.population_stability_index(ref_returns, cur_returns)
        if psi.get("score") is not None and psi["score"] >= self._config.psi_threshold:
            signals.append(
                {
                    "name": "return_distribution_psi",
                    "observed": psi["score"],
                    "threshold": self._config.psi_threshold,
                    "ratio": psi["score"] / self._config.psi_threshold,
                    "method": "psi",
                }
            )
        ks = detectors.ks_2samp(ref_returns, cur_returns)
        if ks.get("p_value") is not None and ks["p_value"] <= self._config.ks_alpha:
            signals.append(
                {
                    "name": "return_distribution_ks",
                    "observed": ks["statistic"],
                    "threshold": self._config.ks_alpha,
                    "p_value": ks["p_value"],
                    "ratio": 2.0,
                    "method": "kolmogorov_smirnov",
                }
            )
        js = detectors.jensen_shannon(ref_returns, cur_returns)
        if js.get("score") is not None and js["score"] >= self._config.js_threshold:
            signals.append(
                {
                    "name": "return_distribution_js",
                    "observed": js["score"],
                    "threshold": self._config.js_threshold,
                    "ratio": js["score"] / self._config.js_threshold,
                    "method": "jensen_shannon",
                }
            )
        for label, keys, threshold in (
            (
                "volatility",
                ("volatility", "realized_volatility"),
                self._config.volatility_change_threshold,
            ),
            ("volume", ("volume", "relative_volume"), self._config.volume_change_threshold),
            (
                "forecast_error",
                ("forecast_error", "absolute_error"),
                self._config.forecast_error_change_threshold,
            ),
        ):
            ref = _series(reference, *keys)
            cur = _series(current, *keys)
            ref_mean = detectors.mean(ref)
            cur_mean = detectors.mean(cur)
            if ref_mean is None or cur_mean is None or abs(ref_mean) <= 1e-12:
                continue
            change = (cur_mean - ref_mean) / abs(ref_mean)
            if abs(change) >= threshold:
                signals.append(
                    {
                        "name": f"{label}_change",
                        "observed": change,
                        "baseline": ref_mean,
                        "current": cur_mean,
                        "threshold": threshold,
                        "ratio": abs(change) / threshold,
                        "method": "relative_mean_change",
                    }
                )
        ref_returns_market = _series(reference, "market_returns", "benchmark_returns")
        cur_returns_market = _series(current, "market_returns", "benchmark_returns")
        if len(ref_returns_market) == len(ref_returns) and len(cur_returns_market) == len(
            cur_returns
        ):
            ref_corr = detectors.pearson(ref_returns, ref_returns_market)
            cur_corr = detectors.pearson(cur_returns, cur_returns_market)
            if ref_corr is not None and cur_corr is not None:
                delta = abs(ref_corr - cur_corr)
                if delta >= self._config.correlation_change_threshold:
                    signals.append(
                        {
                            "name": "correlation_change",
                            "observed": delta,
                            "baseline": ref_corr,
                            "current": cur_corr,
                            "threshold": self._config.correlation_change_threshold,
                            "ratio": delta / self._config.correlation_change_threshold,
                            "method": "correlation_delta",
                        }
                    )
        regime_transition = bool(payload.get("regime_transition"))
        if isinstance(payload.get("regime_history"), Sequence):
            history = [
                regime_label(item.get("primary_regime") if isinstance(item, Mapping) else item)
                for item in payload["regime_history"]
            ]
            regime_transition = regime_transition or any(
                left != right for left, right in zip(history, history[1:], strict=False)
            )
        elif payload.get("previous_regime") and payload.get("current_regime"):
            regime_transition = regime_label(payload.get("previous_regime")) != regime_label(
                payload.get("current_regime")
            )
        consecutive = whole(
            payload.get("consecutive_windows", 1), "consecutive_windows", minimum=0, maximum=100
        )
        if not signals:
            status = ChangeStatus.NORMAL_VARIATION
        elif consecutive >= self._config.change_persistence_windows:
            status = ChangeStatus.SUSTAINED_CHANGE
        elif regime_transition or len(signals) >= 2:
            status = ChangeStatus.POTENTIAL_REGIME_TRANSITION
        else:
            status = ChangeStatus.TEMPORARY_ANOMALY
        return self._change_result(
            instrument_id,
            status,
            signals,
            as_of=payload.get("as_of") or current[-1]["timestamp"],
            sample_sizes={"reference": len(ref_returns), "current": len(cur_returns)},
            regime_transition=regime_transition,
            consecutive_windows=consecutive,
            details={
                "psi": psi,
                "ks": ks,
                "js": js,
                "reference_start": reference[0]["timestamp"],
                "reference_end": reference[-1]["timestamp"],
                "current_start": current[0]["timestamp"],
                "current_end": current[-1]["timestamp"],
            },
        )

    def _change_result(
        self,
        instrument_id: str,
        status: ChangeStatus,
        signals: list[dict[str, Any]],
        *,
        reason: str = "",
        as_of: object = None,
        sample_sizes: Mapping[str, Any] | None = None,
        regime_transition: bool = False,
        consecutive_windows: int = 1,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        detected_at = parse_time(as_of, "as_of") if as_of else self._now()
        severe = any(float(item.get("ratio") or 0) >= 2.0 for item in signals)
        fingerprint = sorted(str(item.get("name")) for item in signals)
        event_id = stable_id(
            "adc_", instrument_id, status.value, ",".join(fingerprint), detected_at.isoformat()
        )
        payload = {
            "change_id": event_id,
            "instrument_id": instrument_id,
            "detected_at": detected_at.isoformat(),
            "status": status.value,
            "kind": status.value,
            "signals": signals,
            "signal_count": len(signals),
            "regime_transition": regime_transition,
            "consecutive_windows": consecutive_windows,
            "evidence_strength": _strength(len(signals), severe),
            "reason": reason,
            "sample_sizes": dict(sample_sizes or {}),
            "details": dict(details or {}),
            "thresholds": self._config.to_dict(),
            "method_version": DETECTOR_VERSION,
            "advisory_only": True,
            "production_authority": False,
            "activates_policy": False,
            "changes_autonomy": False,
        }
        stored, inserted = self._store.save_change(event_id, instrument_id, status.value, payload)
        return {"ok": True, "change": stored, "inserted": inserted}

    def changes(
        self, instrument_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return {
            "ok": True,
            **self._store.changes(instrument_id.upper(), limit=limit, offset=offset),
        }

    # -- drift ---------------------------------------------------------------
    def detect_drift(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        as_of = parse_time(payload.get("as_of") or self._now(), "as_of")
        if as_of > self._now():
            raise AdaptiveValidationError("as_of cannot be in the future")
        kind = text(payload.get("kind"), "data").lower()
        if kind not in {"data", "concept", "both"}:
            raise AdaptiveValidationError("kind must be data, concept or both")
        subject = text(payload.get("scope") or payload.get("policy_id") or "market")
        reference = self._window(payload, "reference")
        current = self._window(payload, "current")
        if not reference or not current:
            return self._drift_result(
                subject,
                DriftStatus.DATA_UNAVAILABLE,
                kind,
                as_of=as_of,
                reason="both reference and current windows are required",
            )
        available = []
        for label, items in (("reference", reference), ("current", current)):
            for index, item in enumerate(items):
                observed_at = item.get("timestamp")
                if observed_at is not None:
                    at = parse_time(observed_at, f"{label}[{index}].timestamp")
                    if at > as_of:
                        raise AdaptiveValidationError(
                            f"{label}[{index}] is observed after as_of"
                        )
                available_at = item.get("available_at") or item.get("outcome_at")
                if available_at is not None:
                    at = parse_time(available_at, f"{label}[{index}].available_at")
                    if at > as_of:
                        raise AdaptiveValidationError(
                            f"{label}[{index}] contains information available after as_of"
                        )
                available.append(item)
        feature_results: dict[str, Any] = {}
        affected: list[str] = []
        evaluated_features = 0
        if kind in {"data", "both"}:
            ref_features = _features(reference)
            cur_features = _features(current)
            common = sorted(set(ref_features) & set(cur_features))
            if not common:
                return self._drift_result(
                    subject,
                    DriftStatus.INSUFFICIENT_EVIDENCE,
                    kind,
                    as_of=as_of,
                    reason="no common numeric features in the two windows",
                )
            for name in common:
                ref_values = ref_features[name]
                cur_values = cur_features[name]
                if min(len(ref_values), len(cur_values)) < self._config.min_observations:
                    feature_results[name] = {
                        "status": "insufficient_evidence",
                        "reference_samples": len(ref_values),
                        "current_samples": len(cur_values),
                    }
                    continue
                psi = detectors.population_stability_index(ref_values, cur_values)
                ks = detectors.ks_2samp(ref_values, cur_values)
                js = detectors.jensen_shannon(ref_values, cur_values)
                drifted = bool(
                    (psi.get("score") is not None and psi["score"] >= self._config.psi_threshold)
                    or (ks.get("p_value") is not None and ks["p_value"] <= self._config.ks_alpha)
                    or (js.get("score") is not None and js["score"] >= self._config.js_threshold)
                )
                evaluated_features += 1
                feature_results[name] = {
                    "status": "drifted" if drifted else "stable",
                    "psi": psi,
                    "ks": ks,
                    "js": js,
                    "reference_samples": len(ref_values),
                    "current_samples": len(cur_values),
                }
                if drifted:
                    affected.append(name)
        concept_result: dict[str, Any] = {}
        concept_drifted = False
        if kind in {"concept", "both"}:
            ref_feature = text(payload.get("feature"), "score")
            ref_pairs = self._concept_pairs(reference, as_of, ref_feature)
            cur_pairs = self._concept_pairs(current, as_of, ref_feature)
            if ref_pairs and cur_pairs:
                ref_x, ref_y = ref_pairs
                cur_x, cur_y = cur_pairs
                relationship = detectors.fisher_relationship_change(ref_x, ref_y, cur_x, cur_y)
                relationship["detected"] = bool(
                    (
                        relationship.get("p_value") is not None
                        and relationship["p_value"] <= self._config.relationship_alpha
                    )
                    or (
                        relationship.get("delta") is not None
                        and relationship["delta"] >= self._config.relationship_delta_threshold
                    )
                )
                concept_result["relationship"] = relationship
                concept_drifted = bool(relationship["detected"])
            ref_errors = _series(reference, "forecast_error", "prediction_error", "absolute_error")
            cur_errors = _series(current, "forecast_error", "prediction_error", "absolute_error")
            error_result = detectors.rolling_error_ratio(ref_errors, cur_errors)
            if not error_result.get("insufficient"):
                concept_result["rolling_error"] = error_result
                if (
                    float(error_result.get("ratio") or 0)
                    >= 1.0 + self._config.forecast_error_change_threshold
                ):
                    concept_drifted = True
            ph = detectors.page_hinkley(
                cur_errors,
                delta=self._config.page_hinkley_delta,
                threshold=self._config.page_hinkley_threshold,
            )
            concept_result["page_hinkley"] = ph
            if ph.get("detected"):
                concept_drifted = True
            if ref_errors and cur_errors:
                cusum_result = detectors.cusum(
                    ref_errors, cur_errors, threshold=self._config.cusum_threshold
                )
                concept_result["cusum"] = cusum_result
                if cusum_result.get("detected"):
                    concept_drifted = True
            if not concept_result:
                concept_result["reason"] = (
                    "concept drift requires feature/outcome pairs or forecast-error evidence"
                )
        concept_evidence = any(
            key in concept_result
            for key in ("relationship", "rolling_error", "page_hinkley", "cusum")
        )
        data_drifted = bool(affected)
        if kind == "data":
            if evaluated_features == 0:
                status = DriftStatus.INSUFFICIENT_EVIDENCE
            else:
                status = DriftStatus.DATA_DRIFT if data_drifted else DriftStatus.NO_DRIFT
        elif kind == "concept":
            if concept_drifted:
                status = DriftStatus.CONCEPT_DRIFT
            elif concept_evidence:
                status = DriftStatus.NO_DRIFT
            else:
                status = DriftStatus.INSUFFICIENT_EVIDENCE
        elif data_drifted and concept_drifted:
            status = DriftStatus.BOTH
        elif data_drifted:
            status = DriftStatus.DATA_DRIFT
        elif concept_drifted:
            status = DriftStatus.CONCEPT_DRIFT
        elif evaluated_features == 0 and not concept_evidence:
            status = DriftStatus.INSUFFICIENT_EVIDENCE
        else:
            status = DriftStatus.NO_DRIFT
        return self._drift_result(
            subject,
            status,
            kind,
            as_of=as_of,
            affected_features=affected,
            feature_results=feature_results,
            concept=concept_result,
            data_drifted=data_drifted,
            concept_drifted=concept_drifted,
            sample_sizes={
                "reference": len(reference),
                "current": len(current),
            },
        )

    def _concept_pairs(
        self, records: Sequence[Mapping[str, Any]], as_of: datetime, feature: str
    ) -> tuple[list[float], list[float]] | None:
        xs: list[float] = []
        ys: list[float] = []
        for item in records:
            outcome_at = item.get("outcome_at") or item.get("available_at")
            if outcome_at is None:
                continue
            if parse_time(outcome_at, "outcome_at") > as_of:
                continue
            features = item.get("features")
            if isinstance(features, Mapping):
                x = _number(features.get(feature))
            else:
                x = _number(item.get(feature))
            y = _number(item.get("outcome"))
            if x is not None and y is not None:
                xs.append(x)
                ys.append(y)
        return (xs, ys) if len(xs) >= 4 else None

    def _drift_result(
        self,
        subject: str,
        status: DriftStatus,
        kind: str,
        *,
        as_of: datetime,
        reason: str = "",
        affected_features: Sequence[str] = (),
        feature_results: Mapping[str, Any] | None = None,
        concept: Mapping[str, Any] | None = None,
        data_drifted: bool = False,
        concept_drifted: bool = False,
        sample_sizes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        evidence_count = len(affected_features) + (1 if concept_drifted else 0)
        severity = bool(
            status in {DriftStatus.DATA_DRIFT, DriftStatus.CONCEPT_DRIFT, DriftStatus.BOTH}
        )
        drift_id = stable_id(
            "add_",
            subject,
            kind,
            status.value,
            ",".join(sorted(affected_features)),
            as_of.isoformat(),
        )
        record = {
            "drift_id": drift_id,
            "scope": subject,
            "kind": kind,
            "status": status.value,
            "detected_at": as_of.isoformat(),
            "affected_features": list(affected_features),
            "feature_results": dict(feature_results or {}),
            "concept": dict(concept or {}),
            "data_drifted": data_drifted,
            "concept_drifted": concept_drifted,
            "evidence_strength": _strength(evidence_count, severity),
            "sample_sizes": dict(sample_sizes or {}),
            "reason": reason,
            "parameters": {
                "psi_threshold": self._config.psi_threshold,
                "ks_alpha": self._config.ks_alpha,
                "js_threshold": self._config.js_threshold,
                "relationship_delta_threshold": self._config.relationship_delta_threshold,
                "relationship_alpha": self._config.relationship_alpha,
                "page_hinkley_delta": self._config.page_hinkley_delta,
                "page_hinkley_threshold": self._config.page_hinkley_threshold,
                "cusum_threshold": self._config.cusum_threshold,
            },
            "method_version": DETECTOR_VERSION,
            "future_outcomes_excluded": True,
            "advisory_only": True,
            "production_authority": False,
        }
        stored, inserted = self._store.save_drift(drift_id, subject, status.value, record)
        return {"ok": True, "drift": stored, "inserted": inserted}

    def drift(self, subject: str = "", *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return {"ok": True, **self._store.drift(subject, limit=limit, offset=offset)}

    # -- degradation ---------------------------------------------------------
    def monitor_degradation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        policy_id = text(payload.get("policy_id"))
        if not policy_id:
            raise AdaptiveValidationError("policy_id is required")
        if self._training_service is not None:
            self._policy(policy_id)
        raw = payload.get("observations")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
            return self._degradation_result(
                policy_id,
                DegradationStatus.DATA_UNAVAILABLE,
                [],
                reason="observations are required",
            )
        if len(raw) > self._config.max_observations:
            raise AdaptiveValidationError(f"observations exceeds {self._config.max_observations}")
        observations = []
        for index, item in enumerate(raw):
            record = mapping(item, f"observations[{index}]")
            timestamp = record.get("timestamp") or record.get("as_of")
            if timestamp is None:
                raise AdaptiveValidationError(f"observations[{index}].timestamp is required")
            record["timestamp"] = parse_time(timestamp, "timestamp").isoformat()
            observations.append(record)
        observations.sort(key=lambda item: item["timestamp"])
        as_of = parse_time(payload.get("as_of") or self._now(), "as_of")
        if as_of > self._now():
            raise AdaptiveValidationError("as_of cannot be in the future")
        if parse_time(observations[-1]["timestamp"], "timestamp") > as_of:
            raise AdaptiveValidationError(
                "degradation observations contain information after as_of"
            )
        if len(observations) < self._config.min_policy_observations:
            return self._degradation_result(
                policy_id,
                DegradationStatus.INSUFFICIENT_EVIDENCE,
                [],
                reason=(
                    f"{len(observations)} observations is below the required "
                    f"{self._config.min_policy_observations}"
                ),
                observation_count=len(observations),
            )
        persistence = self._config.degradation_persistence_windows
        if len(observations) < persistence + 2:
            return self._degradation_result(
                policy_id,
                DegradationStatus.INSUFFICIENT_EVIDENCE,
                [],
                reason="not enough history for baseline and persistence windows",
                observation_count=len(observations),
            )
        recent = observations[-persistence:]
        baseline = observations[:-persistence]
        baseline_metrics = self._degradation_metrics(baseline)
        triggers: list[dict[str, Any]] = []
        temporary_signals: list[dict[str, Any]] = []
        candidates = (
            ("return_degradation", "return", "drop", self._config.degradation_return_drop),
            (
                "drawdown_increase",
                "max_drawdown",
                "increase",
                self._config.degradation_drawdown_increase,
            ),
            (
                "volatility_increase",
                "volatility",
                "increase",
                self._config.degradation_volatility_increase,
            ),
            ("sharpe_decline", "sharpe", "drop", self._config.degradation_sharpe_drop),
            ("reward_degradation", "reward", "drop", self._config.degradation_reward_drop),
            (
                "invalid_action_increase",
                "invalid_action_rate",
                "increase",
                self._config.degradation_invalid_action_increase,
            ),
        )
        for name, metric, direction, threshold in candidates:
            base = baseline_metrics.get(metric)
            if base is None:
                continue
            values = [_number(self._degradation_metrics([window]).get(metric)) for window in recent]
            valid = [value for value in values if value is not None]
            if len(valid) < persistence:
                continue
            if direction == "drop":
                breaches = [base - value >= threshold for value in valid]
                observed = min(valid)
                change = base - observed
            else:
                breaches = [value - base >= threshold for value in valid]
                observed = max(valid)
                change = observed - base
            candidate = {
                "name": name,
                "baseline": base,
                "observed": observed,
                "change": change,
                "threshold": threshold,
                "ratio": abs(change) / threshold if threshold else 0.0,
                "persistent_windows": sum(breaches),
                "direction": direction,
                "persistent": sum(breaches) >= persistence,
            }
            if sum(breaches) >= persistence:
                triggers.append(candidate)
            else:
                temporary_signals.append(candidate)
        for name, metric, threshold in (
            (
                "transaction_cost_increase",
                "transaction_costs",
                self._config.degradation_cost_increase,
            ),
            ("turnover_increase", "turnover", self._config.degradation_turnover_increase),
        ):
            base = baseline_metrics.get(metric)
            if base is None or base <= 0:
                continue
            values = [_number(self._degradation_metrics([window]).get(metric)) for window in recent]
            valid = [value for value in values if value is not None]
            if len(valid) < persistence:
                continue
            breaches = [value / base >= threshold for value in valid]
            candidate = {
                "name": name,
                "baseline": base,
                "observed": max(valid),
                "threshold": threshold,
                "ratio": (max(valid) / base) / threshold,
                "persistent_windows": sum(breaches),
                "direction": "ratio",
                "persistent": sum(breaches) >= persistence,
            }
            if sum(breaches) >= persistence:
                triggers.append(candidate)
            else:
                temporary_signals.append(candidate)
        base_actions = baseline_metrics.get("action_distribution") or {}
        action_shift = None
        if isinstance(base_actions, Mapping) and base_actions:
            shifts = []
            for window in recent:
                current_actions = (
                    self._degradation_metrics([window]).get("action_distribution") or {}
                )
                shift = detectors.action_distribution_shift(
                    {str(key): float(value) for key, value in base_actions.items()},
                    {str(key): float(value) for key, value in dict(current_actions).items()},
                )
                if not shift.get("insufficient"):
                    shifts.append(float(shift["shift"]))
            if len(shifts) >= persistence:
                breaches = [value >= self._config.degradation_action_shift for value in shifts]
                action_shift = {
                    "name": "action_distribution_shift",
                    "baseline": 0.0,
                    "observed": max(shifts),
                    "threshold": self._config.degradation_action_shift,
                    "ratio": max(shifts) / self._config.degradation_action_shift,
                    "persistent_windows": sum(breaches),
                    "direction": "increase",
                    "persistent": sum(breaches) >= persistence,
                }
                if sum(breaches) >= persistence:
                    triggers.append(action_shift)
                else:
                    temporary_signals.append(action_shift)
        if not triggers and temporary_signals:
            status = DegradationStatus.TEMPORARY_UNDERPERFORMANCE
            triggers = temporary_signals
        elif not triggers:
            status = DegradationStatus.NONE
        elif len(triggers) >= 2 or any(float(item.get("ratio") or 0) >= 2.0 for item in triggers):
            status = DegradationStatus.PERSISTENT_DEGRADATION
        else:
            status = DegradationStatus.TEMPORARY_UNDERPERFORMANCE
        return self._degradation_result(
            policy_id,
            status,
            triggers,
            observation_count=len(observations),
            baseline_count=len(baseline),
            recent_count=len(recent),
            period_start=observations[0]["timestamp"],
            period_end=observations[-1]["timestamp"],
        )

    def _degradation_metrics(self, observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        grouped: dict[str, list[float]] = {}
        actions: Counter[str] = Counter()
        for item in observations:
            metrics = item.get("metrics")
            source = dict(metrics) if isinstance(metrics, Mapping) else item
            for key in (
                "return",
                "total_return",
                "max_drawdown",
                "drawdown",
                "volatility",
                "sharpe",
                "sortino",
                "reward",
                "transaction_costs",
                "total_costs",
                "turnover",
                "invalid_action_rate",
            ):
                value = _number(source.get(key))
                if value is not None:
                    grouped.setdefault(key, []).append(value)
            distribution = source.get("action_distribution") or source.get("actions")
            if isinstance(distribution, Mapping):
                for action, count in distribution.items():
                    number = _number(count)
                    if number is not None and number >= 0:
                        actions[str(action)] += int(number)
        result: dict[str, Any] = {}
        for key, values in grouped.items():
            result[key] = detectors.mean(values)
        if result.get("return") is None and result.get("total_return") is not None:
            result["return"] = result["total_return"]
        if result.get("max_drawdown") is None and result.get("drawdown") is not None:
            result["max_drawdown"] = result["drawdown"]
        if result.get("transaction_costs") is None and result.get("total_costs") is not None:
            result["transaction_costs"] = result["total_costs"]
        if actions:
            result["action_distribution"] = dict(actions)
        return result

    def _degradation_result(
        self,
        policy_id: str,
        status: DegradationStatus,
        triggers: Sequence[Mapping[str, Any]],
        *,
        reason: str = "",
        observation_count: int = 0,
        baseline_count: int = 0,
        recent_count: int = 0,
        period_start: str = "",
        period_end: str = "",
    ) -> dict[str, Any]:
        detected_at = self._now()
        severe = any(float(item.get("ratio") or 0) >= 2.0 for item in triggers)
        alert_id = stable_id(
            "adg_",
            policy_id,
            status.value,
            ",".join(sorted(str(item.get("name")) for item in triggers)),
            period_end,
        )
        record = {
            "degradation_id": alert_id,
            "policy_id": policy_id,
            "detected_at": detected_at.isoformat(),
            "status": status.value,
            "triggers": [dict(item) for item in triggers],
            "trigger_count": len(triggers),
            "observation_count": observation_count,
            "baseline_count": baseline_count,
            "recent_count": recent_count,
            "period_start": period_start,
            "period_end": period_end,
            "persistent_windows_required": self._config.degradation_persistence_windows,
            "evidence_strength": _strength(len(triggers), severe),
            "reason": reason,
            "minimum_observations": self._config.min_policy_observations,
            "thresholds": self._config.to_dict(),
            "advisory_only": True,
            "production_authority": False,
            "triggers_training": False,
        }
        stored, inserted = self._store.save_degradation(alert_id, policy_id, status.value, record)
        return {"ok": True, "degradation": stored, "inserted": inserted}

    def degradation(
        self, policy_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return {
            "ok": True,
            **self._store.degradation(policy_id, limit=limit, offset=offset),
        }

    # -- suitability ---------------------------------------------------------
    def assess_suitability(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        policy_id = text(payload.get("policy_id"))
        if not policy_id:
            raise AdaptiveValidationError("policy_id is required")
        policy = self._policy(policy_id) or {}
        regime = regime_label(payload.get("regime") or "uncertain")
        metrics_raw = payload.get("metrics")
        evaluated_raw = payload.get("evaluated_at") or payload.get("evaluation_timestamp")
        evaluation_provenance: dict[str, Any] = {}
        in_sample_excluded = 0
        if not isinstance(metrics_raw, Mapping):
            stored_rows = self._store.regime_evaluations(policy_id, limit=500)["items"]
            relevant = [
                row
                for row in stored_rows
                if row.get("regime") == regime and row.get("sufficient")
            ]
            excluded = [row for row in relevant if row.get("in_sample")]
            in_sample_excluded = len(excluded)
            matching = [row for row in relevant if not row.get("in_sample")]
            requested_evaluation = text(payload.get("evaluation_id"))
            if requested_evaluation:
                if any(row.get("evaluation_id") == requested_evaluation for row in excluded):
                    raise AdaptiveValidationError(
                        f"regime evaluation {requested_evaluation!r} is in-sample for this "
                        "policy: it overlaps the policy's training or checkpoint-selection "
                        "data, so it cannot feed suitability"
                    )
                matching = [
                    row
                    for row in matching
                    if row.get("evaluation_id") == requested_evaluation
                ]
                if not matching:
                    raise AdaptiveNotFoundError(
                        f"no sufficient regime evaluation {requested_evaluation!r} exists "
                        "for this policy and regime"
                    )
            matching.sort(
                key=lambda row: text(row.get("evaluated_at") or row.get("created_at")),
                reverse=True,
            )
            if matching:
                latest = matching[0]
                metrics_raw = latest.get("metrics") or {
                    "financial": latest.get("financial") or {},
                    "risk": latest.get("risk") or {},
                    "trading": latest.get("trading") or {},
                    "decision": latest.get("decision") or {},
                }
                evaluated_raw = evaluated_raw or latest.get("evaluated_at")
                if not payload.get("sample_size"):
                    payload = {**payload, "sample_size": latest.get("sample_size")}
                if not payload.get("policy_version"):
                    payload = {**payload, "policy_version": latest.get("policy_version")}
                evaluation_provenance = {
                    "regime_evaluation_id": latest.get("regime_evaluation_id"),
                    "evaluation_id": latest.get("evaluation_id"),
                    "dataset_version": latest.get("dataset_version"),
                    "config_hash": latest.get("config_hash"),
                    "evaluation_config": latest.get("evaluation_config") or {},
                    "out_of_sample": latest.get("out_of_sample"),
                    "scope_verified": bool(latest.get("scope_verified")),
                    "training_overlap": list(latest.get("training_overlap") or ()),
                    "in_sample_excluded": in_sample_excluded,
                }
        if not isinstance(metrics_raw, Mapping) or not evaluated_raw:
            status = SuitabilityStatus.NOT_EVALUATED
            return self._suitability_result(
                policy_id,
                regime,
                status,
                None,
                {},
                sample_size=0,
                evaluated_at=None,
                policy=policy,
                reason=(
                    "only in-sample regime evaluation evidence exists; it overlaps the "
                    "policy's training or checkpoint-selection data and was excluded"
                    if in_sample_excluded
                    else "no usable regime evaluation evidence is available"
                ),
                provenance={"in_sample_excluded": in_sample_excluded},
            )
        metrics = self._extract_metrics({"metrics": metrics_raw})
        evaluated_at = parse_time(evaluated_raw, "evaluated_at")
        now = parse_time(payload.get("as_of") or self._now(), "as_of")
        age_days = evaluation_age_days(evaluated_at, now)
        evaluated_version = text(payload.get("policy_version"))
        current_version = text(policy.get("artifact_version") or policy.get("artifact_hash"))
        evaluated_config_hash = text(evaluation_provenance.get("config_hash"))
        current_config_hash = text(policy.get("config_hash"))
        if (
            evaluated_config_hash
            and current_config_hash
            and evaluated_config_hash != current_config_hash
        ):
            return self._suitability_result(
                policy_id,
                regime,
                SuitabilityStatus.STALE_EVALUATION,
                None,
                {},
                sample_size=whole(payload.get("sample_size", 0), "sample_size"),
                evaluated_at=evaluated_at,
                policy=policy,
                age_days=age_days,
                reason=(
                    "the evaluation configuration hash does not match the current "
                    "policy configuration"
                ),
            )
        if evaluated_version and current_version and evaluated_version != current_version:
            return self._suitability_result(
                policy_id,
                regime,
                SuitabilityStatus.STALE_EVALUATION,
                None,
                {},
                sample_size=whole(payload.get("sample_size", 0), "sample_size"),
                evaluated_at=evaluated_at,
                policy=policy,
                age_days=age_days,
                reason=(
                    f"evaluation policy version {evaluated_version!r} does not match "
                    f"the current artifact {current_version!r}"
                ),
            )
        sample_size = whole(
            payload.get(
                "sample_size",
                _policy_metric(metrics, "financial.observations")
                or _policy_metric(metrics, "trading.total_trades")
                or 0,
            ),
            "sample_size",
            maximum=1_000_000,
        )
        if age_days > self._config.stale_evaluation_days:
            return self._suitability_result(
                policy_id,
                regime,
                SuitabilityStatus.STALE_EVALUATION,
                None,
                {},
                sample_size=sample_size,
                evaluated_at=evaluated_at,
                policy=policy,
                age_days=age_days,
                reason=f"evaluation is {age_days:.1f} days old",
            )
        if sample_size < self._config.min_regime_samples:
            return self._suitability_result(
                policy_id,
                regime,
                SuitabilityStatus.INSUFFICIENT_EVIDENCE,
                None,
                {},
                sample_size=sample_size,
                evaluated_at=evaluated_at,
                policy=policy,
                age_days=age_days,
                reason=(
                    f"{sample_size} observations is below the required "
                    f"{self._config.min_regime_samples}"
                ),
            )
        total_return = _policy_metric(metrics, "financial.total_return")
        drawdown = _policy_metric(metrics, "risk.max_drawdown")
        volatility = _policy_metric(metrics, "risk.volatility")
        if volatility is None:
            # PI-14 publishes the same quantity as a financial metric and as an
            # annualized risk metric; read whichever one it computed.
            volatility = _policy_metric(metrics, "financial.volatility")
        if volatility is None:
            volatility = _policy_metric(metrics, "risk.annualized_volatility")
        sharpe = _policy_metric(metrics, "risk.sharpe")
        sortino = _policy_metric(metrics, "risk.sortino")
        invalid_rate = _number(payload.get("invalid_action_rate"))
        if invalid_rate is None:
            invalid_rate = _policy_metric(metrics, "decision.invalid_action_rate")
        if invalid_rate is None:
            invalid_rate = _policy_metric(metrics, "decision.invalid_action_frequency")
        if invalid_rate is None:
            invalid_rate = _policy_metric(metrics, "trading.invalid_action_rate")
        total_return_pct: float | None = (
            total_return * 100.0
            if total_return is not None and _is_fraction(total_return)
            else total_return
        )
        drawdown_pct: float | None = (
            drawdown * 100.0 if drawdown is not None and _is_fraction(drawdown) else drawdown
        )
        volatility_pct: float | None = (
            volatility * 100.0
            if volatility is not None and _is_fraction(volatility)
            else volatility
        )
        cost_ratio = _number(payload.get("cost_ratio"))
        if cost_ratio is None:
            costs = _number(payload.get("transaction_costs"))
            if costs is None:
                costs = _policy_metric(metrics, "trading.total_costs")
            capital = _policy_metric(metrics, "financial.initial_capital")
            if capital is None:
                capital = _number(payload.get("initial_capital"))
            if costs is not None and capital not in (None, 0):
                assert capital is not None
                cost_ratio = abs(costs) / abs(capital)
        action_consistency = _number(payload.get("action_consistency"))
        if action_consistency is None and invalid_rate is not None:
            action_consistency = max(0.0, 1.0 - invalid_rate)
        forecast_uncertainty = _number(payload.get("forecast_uncertainty"))
        stability = _number(payload.get("policy_stability"))
        if stability is None:
            stability = _number(payload.get("stability_score"))
        if total_return is None or drawdown is None:
            return self._suitability_result(
                policy_id,
                regime,
                SuitabilityStatus.INSUFFICIENT_EVIDENCE,
                None,
                {},
                sample_size=sample_size,
                evaluated_at=evaluated_at,
                policy=policy,
                age_days=age_days,
                reason=(
                    "return and drawdown evidence are required before a suitability "
                    "classification"
                ),
                provenance=evaluation_provenance,
            )
        if sharpe is None and sortino is None:
            # Neither risk-adjusted ratio is usable.  The limitation below tells
            # the reader a neutral component was used, so the score uses 0.5; the
            # ``or 0.0`` fallbacks would have charged the missing evidence as the
            # worst case while still claiming neutrality.
            risk_adjusted = 0.5
        else:
            risk_adjusted = _clamp(
                (sharpe or 0.0) / 2.0 * 0.6 + (sortino or 0.0) / 3.0 * 0.4
            )
        components = {
            "return": _clamp((total_return_pct or 0.0) / 10.0),
            "risk_adjusted": risk_adjusted,
            "drawdown": 1.0 - _clamp((drawdown_pct or 0.0) / 25.0),
            "volatility": 1.0 - _clamp((volatility_pct or 0.0) / 40.0),
            "cost": 1.0 - _clamp((cost_ratio or 0.0) / 2.0),
            "action_consistency": (
                _clamp(action_consistency) if action_consistency is not None else 0.5
            ),
            "uncertainty": (
                1.0 - _clamp(forecast_uncertainty) if forecast_uncertainty is not None else 0.5
            ),
            "stability": _clamp(stability) if stability is not None else 0.5,
            "sample": _clamp(sample_size / max(1, self._config.min_regime_samples * 2)),
            "freshness": _clamp(1.0 - age_days / max(1, self._config.stale_evaluation_days)),
        }
        score = (
            components["return"] * 0.20
            + components["risk_adjusted"] * 0.18
            + components["drawdown"] * 0.14
            + components["volatility"] * 0.08
            + components["cost"] * 0.08
            + components["action_consistency"] * 0.08
            + components["uncertainty"] * 0.06
            + components["stability"] * 0.06
            + components["sample"] * 0.06
            + components["freshness"] * 0.06
        )
        hard_flags = []
        if drawdown_pct is not None and drawdown_pct > self._config.hard_drawdown_limit * 100:
            hard_flags.append("maximum drawdown exceeds the hard research limit")
        if invalid_rate is not None and invalid_rate > self._config.hard_invalid_action_limit:
            hard_flags.append("invalid-action rate exceeds the hard research limit")
        if total_return_pct is not None and total_return_pct < -10.0:
            hard_flags.append("total return is materially negative")
        if sharpe is not None and sharpe < -1.0:
            hard_flags.append("risk-adjusted return is materially negative")
        if not hard_flags and score >= self._config.suitability_suitable_score:
            status = SuitabilityStatus.SUITABLE
        elif not hard_flags and score >= self._config.suitability_conditional_score:
            status = SuitabilityStatus.CONDITIONALLY_SUITABLE
        elif hard_flags:
            status = SuitabilityStatus.UNSUITABLE
        else:
            status = SuitabilityStatus.UNSUITABLE
        material_gaps = []
        if sharpe is None and sortino is None:
            material_gaps.append("risk-adjusted return was not supplied")
        if cost_ratio is None:
            material_gaps.append("transaction-cost ratio was unavailable")
        if action_consistency is None:
            material_gaps.append("action consistency was not supplied")
        if forecast_uncertainty is None:
            material_gaps.append("forecast uncertainty was not supplied")
        if stability is None:
            material_gaps.append("policy stability was not supplied")
        if status == SuitabilityStatus.SUITABLE and material_gaps:
            status = SuitabilityStatus.CONDITIONALLY_SUITABLE
        limitations = [
            f"{gap}; a neutral scoring component was used" for gap in material_gaps
        ]
        if sample_size < self._config.min_regime_samples * 2:
            limitations.append("sample size is adequate but still small for regime conclusions")
        return self._suitability_result(
            policy_id,
            regime,
            status,
            round(score, 6),
            components,
            sample_size=sample_size,
            evaluated_at=evaluated_at,
            policy=policy,
            age_days=age_days,
            hard_flags=hard_flags,
            metrics={
                "total_return_pct": total_return_pct,
                "max_drawdown_pct": drawdown_pct,
                "volatility_pct": volatility_pct,
                "sharpe": sharpe,
                "sortino": sortino,
                "invalid_action_rate": invalid_rate,
                "cost_ratio": cost_ratio,
            },
            limitations=limitations,
            config=self._config.to_dict(),
            provenance=evaluation_provenance,
        )

    def _suitability_result(
        self,
        policy_id: str,
        regime: str,
        status: SuitabilityStatus,
        score: float | None,
        components: Mapping[str, Any],
        *,
        sample_size: int,
        evaluated_at: datetime | None,
        policy: Mapping[str, Any],
        reason: str = "",
        age_days: float | None = None,
        hard_flags: Sequence[str] = (),
        metrics: Mapping[str, Any] | None = None,
        limitations: Sequence[str] = (),
        config: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        assessed_at = self._now()
        assessment_id = stable_id(
            "ads_",
            policy_id,
            regime,
            status.value,
            evaluated_at.isoformat() if evaluated_at else "",
            text(policy.get("artifact_hash") or policy.get("artifact_version")),
            text((metrics or {}).get("total_return_pct")),
            str(score),
        )
        record = {
            "assessment_id": assessment_id,
            "policy_id": policy_id,
            "policy_version": text(policy.get("artifact_version") or policy.get("artifact_hash")),
            "regime": regime,
            "status": status.value,
            "suitability_score": score,
            "components": dict(components),
            "sample_size": sample_size,
            "minimum_sample": self._config.min_regime_samples,
            "evaluated_at": evaluated_at.isoformat() if evaluated_at else None,
            "assessed_at": assessed_at.isoformat(),
            "age_days": age_days,
            "hard_flags": list(hard_flags),
            "metrics": dict(metrics or {}),
            "limitations": list(limitations),
            "evaluation_provenance": dict(provenance or {}),
            "reason": reason,
            "config": dict(config or self._config.to_dict()),
            "advisory_only": True,
            "research_recommendation_only": True,
            "production_authority": False,
            "activation_available": False,
            "automatic_selection": False,
        }
        stored, inserted = self._store.save_suitability(
            assessment_id, policy_id, status.value, record
        )
        return {
            "ok": True,
            "suitability": stored,
            "inserted": inserted,
            "advisory_only": True,
        }

    def suitability(
        self, policy_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return {"ok": True, **self._store.suitability(policy_id, limit=limit, offset=offset)}

    # -- recommendations -----------------------------------------------------
    def recommend(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        policy_id = text(payload.get("policy_id"))
        if not policy_id:
            raise AdaptiveValidationError("policy_id is required")
        scope = text(payload.get("scope") or payload.get("instrument_id") or "market")
        suitability = payload.get("suitability")
        if isinstance(suitability, str):
            suitability = {"status": suitability}
        suitability = dict(suitability) if isinstance(suitability, Mapping) else {}
        degradation = payload.get("degradation")
        if isinstance(degradation, str):
            degradation = {"status": degradation}
        degradation = dict(degradation) if isinstance(degradation, Mapping) else {}
        drift = payload.get("drift")
        drifts = (
            list(drift)
            if isinstance(drift, Sequence) and not isinstance(drift, (str, bytes))
            else [drift]
            if isinstance(drift, Mapping)
            else []
        )
        changes = payload.get("changes")
        change_rows = (
            list(changes)
            if isinstance(changes, Sequence) and not isinstance(changes, (str, bytes))
            else [changes]
            if isinstance(changes, Mapping)
            else []
        )
        suitability_status = text(suitability.get("status"))
        degradation_status = text(degradation.get("status"))
        drift_statuses = {text(item.get("status")) for item in drifts if isinstance(item, Mapping)}
        change_statuses = {
            text(item.get("status")) for item in change_rows if isinstance(item, Mapping)
        }
        concept = bool(drift_statuses & {"concept_drift", "both"})
        data_drift = bool(drift_statuses & {"data_drift", "both"})
        sustained = "sustained_change" in change_statuses
        transition = "potential_regime_transition" in change_statuses
        persistent = degradation_status == "persistent_degradation"
        stale = suitability_status == "stale_evaluation" or bool(payload.get("evaluation_stale"))
        no_evidence = not any(
            [suitability, degradation, drifts, change_rows, payload.get("evaluation_stale")]
        )
        if no_evidence:
            action = RecommendationAction.INSUFFICIENT_EVIDENCE
        elif suitability_status == SuitabilityStatus.UNSUITABLE.value:
            action = RecommendationAction.POLICY_UNSUITABLE
        elif persistent and (concept or sustained):
            action = RecommendationAction.RETRAIN_RECOMMENDED
        elif persistent:
            action = RecommendationAction.REEVALUATE_POLICY
        elif concept or data_drift:
            action = RecommendationAction.REASSESS_FEATURES
        elif sustained or transition:
            action = RecommendationAction.RUN_NEW_BACKTEST
        elif stale:
            action = RecommendationAction.REEVALUATE_POLICY
        elif bool(payload.get("no_action_required")):
            action = RecommendationAction.NO_ADAPTATION_REQUIRED
        else:
            action = RecommendationAction.CONTINUE_MONITORING
        triggers = []
        if suitability:
            triggers.append(
                {
                    "source": "suitability",
                    "status": suitability_status,
                    "score": suitability.get("suitability_score"),
                }
            )
        if degradation:
            triggers.append(
                {
                    "source": "degradation",
                    "status": degradation_status,
                    "triggers": degradation.get("triggers") or [],
                }
            )
        for item in drifts:
            if isinstance(item, Mapping):
                triggers.append(
                    {
                        "source": "drift",
                        "status": item.get("status"),
                        "affected_features": item.get("affected_features") or [],
                    }
                )
        for item in change_rows:
            if isinstance(item, Mapping):
                triggers.append(
                    {
                        "source": "market_change",
                        "status": item.get("status"),
                        "signals": item.get("signals") or [],
                    }
                )
        evidence_sources = len({item["source"] for item in triggers})
        severe = action.value in _SEVERE_ACTIONS
        trigger_fingerprint = sorted(
            f"{item.get('source')}:{item.get('status')}" for item in triggers
        )
        proposed: dict[str, Any] = {
            "recommendation_id": stable_id(
                "adr_",
                policy_id,
                scope,
                action.value,
                evidence_sources,
                ",".join(trigger_fingerprint),
            ),
            "affected_policy": policy_id,
            "scope": scope,
            "action": action.value,
            "recommended_action": action.value,
            "status": RecommendationStatus.OPEN.value,
            "detected_at": self._now().isoformat(),
            "triggering_metrics": triggers,
            "supporting_evidence": {
                "suitability": suitability,
                "degradation": degradation,
                "drift": drifts,
                "changes": change_rows,
            },
            "evidence_strength": _strength(len(triggers), severe),
            "recommended_next_action": _ACTION_TEXT[action],
            "confidence_note": (
                "Evidence strength is a count of independent recorded signals, "
                "not a calibrated probability."
            ),
            "data_versions": dict(payload.get("data_versions") or {}),
            "model_versions": dict(payload.get("model_versions") or {}),
            "policy_version": text(payload.get("policy_version")),
            "requires_explicit_authorization": True,
            "advisory_only": True,
            "production_authority": False,
            "activation_available": False,
            "auto_promote": False,
            "auto_train": False,
            "authorizes_action": False,
        }
        previous_key = f"recommendation:{policy_id}:{scope}"
        previous = self._store.state(previous_key)
        now = self._now()
        if previous and action.value != previous.get("action"):
            previous_at = parse_time(previous.get("at"), "previous recommendation")
            within_cooldown = (now - previous_at).total_seconds() < self._config.cooldown_seconds
            previous_severe = text(previous.get("action")) in _SEVERE_ACTIONS
            previous_count = whole(
                previous.get("trigger_count", 0),
                "previous trigger_count",
                maximum=1_000,
            )
            required_growth = max(
                1,
                math.ceil(previous_count * (1.0 + self._config.hysteresis_margin)),
            )
            evidence_grew = len(triggers) > required_growth
            if within_cooldown and not (severe and not previous_severe) and not evidence_grew:
                existing = self._store.recommendation(text(previous.get("recommendation_id")))
                if existing:
                    updated = {
                        **existing,
                        "last_checked_at": now.isoformat(),
                        "occurrences": int(existing.get("occurrences") or 1) + 1,
                        "hysteresis": {
                            "suppressed": True,
                            "proposed_action": action.value,
                            "reason": (
                                "cooldown and hysteresis prevented a switch without "
                                "materially stronger evidence"
                            ),
                            "cooldown_seconds": self._config.cooldown_seconds,
                        },
                    }
                    self._store.save_recommendation(
                        text(existing["recommendation_id"]),
                        policy_id,
                        text(existing.get("status"), RecommendationStatus.OPEN.value),
                        updated,
                    )
                    return {
                        "ok": True,
                        "recommendation": updated,
                        "inserted": False,
                        "hysteresis_suppressed": True,
                        "advisory_only": True,
                    }
        previous_record = self._store.recommendation(proposed["recommendation_id"])
        if previous_record:
            proposed["occurrences"] = int(previous_record.get("occurrences") or 1) + 1
            proposed["created_at"] = previous_record.get("created_at")
            proposed["status"] = text(
                previous_record.get("status"), RecommendationStatus.OPEN.value
            )
            if previous_record.get("acknowledged_at"):
                proposed["acknowledged_at"] = previous_record.get("acknowledged_at")
                proposed["acknowledgement_note"] = previous_record.get(
                    "acknowledgement_note", ""
                )
        else:
            proposed["occurrences"] = 1
            proposed["created_at"] = now.isoformat()
        proposed["last_checked_at"] = now.isoformat()
        stored = self._store.save_recommendation(
            proposed["recommendation_id"], policy_id, proposed["status"], proposed
        )
        state_at = now
        if previous and text(previous.get("action")) == action.value:
            state_previous_at = optional_time(previous.get("at"))
            if state_previous_at is not None:
                state_at = state_previous_at
        self._store.set_state(
            previous_key,
            {
                "action": action.value,
                "recommendation_id": proposed["recommendation_id"],
                "at": state_at.isoformat(),
                "trigger_count": len(triggers),
            },
        )
        return {
            "ok": True,
            "recommendation": stored,
            "inserted": previous_record is None,
            "hysteresis_suppressed": False,
            "advisory_only": True,
        }

    def recommendations(
        self,
        policy_id: str = "",
        *,
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            **self._store.recommendations(policy_id, status=status, limit=limit, offset=offset),
        }

    def acknowledge_recommendation(
        self, recommendation_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        existing = self._store.recommendation(recommendation_id)
        if existing is None:
            raise AdaptiveNotFoundError(f"unknown recommendation {recommendation_id!r}")
        if existing.get("status") == RecommendationStatus.ACKNOWLEDGED.value:
            return {"ok": True, "recommendation": existing, "already_acknowledged": True}
        note = text((payload or {}).get("note"))
        if len(note) > 2000:
            raise AdaptiveValidationError("acknowledgement note is too long")
        updated = {
            **existing,
            "status": RecommendationStatus.ACKNOWLEDGED.value,
            "acknowledged_at": self._now().isoformat(),
            "acknowledgement_note": note,
            "advisory_only": True,
        }
        self._store.save_recommendation(
            recommendation_id,
            text(existing.get("affected_policy")),
            RecommendationStatus.ACKNOWLEDGED.value,
            updated,
        )
        return {"ok": True, "recommendation": updated, "advisory_only": True}

    # -- retraining ----------------------------------------------------------
    def retraining_recommendation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        policy_id = text(payload.get("policy_id"))
        if not policy_id:
            raise AdaptiveValidationError("policy_id is required")
        policy = self._policy(policy_id) or {}
        recommendation_id = text(
            payload.get("recommendation_id") or payload.get("adaptation_recommendation_id")
        )
        recommendation: dict[str, Any] = {}
        if recommendation_id:
            recommendation = self._store.recommendation(recommendation_id) or {}
            if not recommendation:
                raise AdaptiveNotFoundError(
                    f"unknown adaptation recommendation {recommendation_id!r}"
                )
        reason = text(
            payload.get("reason")
            or payload.get("trigger")
            or recommendation.get("action")
            or "sustained evidence indicates reassessment"
        )
        if len(reason) > 2000:
            raise AdaptiveValidationError("retraining reason is too long")
        start_raw = payload.get("training_start") or payload.get("start")
        end_raw = payload.get("training_end") or payload.get("end")
        if (not start_raw or not end_raw) and recommendation:
            evidence = recommendation.get("supporting_evidence") or {}
            change = (evidence.get("changes") or [{}])[0] if isinstance(evidence, Mapping) else {}
            if isinstance(change, Mapping):
                start_raw = start_raw or change.get("start")
                end_raw = end_raw or change.get("end")
        provenance = dict(policy.get("dataset_provenance") or {})
        start_raw = start_raw or provenance.get("start")
        end_raw = end_raw or provenance.get("end")
        if not start_raw or not end_raw:
            raise AdaptiveValidationError(
                "training_start and training_end are required when no dataset window is recorded"
            )
        start = parse_time(start_raw, "training_start")
        end = parse_time(end_raw, "training_end")
        if start >= end:
            raise AdaptiveValidationError("training_start must precede training_end")
        available_at = parse_time(payload.get("available_at") or self._now(), "available_at")
        if available_at < end:
            raise AdaptiveValidationError(
                "a recommended training window cannot end after the evidence is available"
            )
        regimes = [regime_label(item) for item in sequence(payload.get("required_regimes"))]
        if not regimes:
            regimes = [regime_label(payload.get("regime"))] if payload.get("regime") else []
        recommendation_record: dict[str, Any] = {
            "retraining_id": stable_id(
                "adt_",
                policy_id,
                reason,
                start.isoformat(),
                end.isoformat(),
                ",".join(sorted(regimes)),
            ),
            "policy_id": policy_id,
            "policy_version": text(payload.get("policy_version") or policy.get("artifact_version")),
            "existing_policy_status": text(policy.get("status")),
            "reason": reason,
            "supporting_evidence": dict(
                payload.get("supporting_evidence")
                or recommendation.get("supporting_evidence")
                or {}
            ),
            "triggering_metrics": list(
                payload.get("triggering_metrics") or recommendation.get("triggering_metrics") or []
            ),
            "historical_period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "suggested_training_window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "chronological": True,
            },
            "required_regime_coverage": regimes,
            "reward_configuration": dict(
                payload.get("reward_configuration")
                or {
                    "config_hash": text(
                        payload.get("reward_config_hash") or policy.get("reward_version")
                    )
                }
            ),
            "validation_conditions": dict(
                payload.get("validation_conditions")
                or {
                    "chronological_holdout": True,
                    "untouched_test_required": True,
                    "minimum_regime_samples": self._config.min_regime_samples,
                    "out_of_sample_only": True,
                    "manual_promotion_only": True,
                }
            ),
            "data_versions": dict(payload.get("data_versions") or {}),
            "created_at": self._now().isoformat(),
            "status": "recommended",
            "training_started": False,
            "requires_explicit_authorization": True,
            "automatic_training": False,
            "advisory_only": True,
            "production_authority": False,
            "activation_available": False,
            "promotion_available": False,
        }
        stored, inserted = self._store.save_retraining(
            recommendation_record["retraining_id"], policy_id, "recommended", recommendation_record
        )
        return {
            "ok": True,
            "retraining": stored,
            "inserted": inserted,
            "training_started": False,
            "advisory_only": True,
        }

    def retraining(
        self, policy_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return {"ok": True, **self._store.retraining(policy_id, limit=limit, offset=offset)}

    # -- reassessment --------------------------------------------------------
    def create_reassessment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        policy_id = text(payload.get("policy_id"))
        if not policy_id:
            raise AdaptiveValidationError("policy_id is required")
        regime = regime_label(payload.get("regime") or "uncertain")
        reason = text(payload.get("reason"), "explicit adaptive reassessment")
        windows: list[dict[str, Any]] = []
        if payload.get("windows") is not None:
            raw_windows = sequence(payload.get("windows"))
            if len(raw_windows) < 2 or len(raw_windows) > 20:
                raise AdaptiveValidationError(
                    "walk-forward reassessment requires 2 to 20 explicit windows"
                )
            previous_end: datetime | None = None
            for index, item in enumerate(raw_windows):
                if not isinstance(item, Mapping):
                    raise AdaptiveValidationError(f"windows[{index}] must be an object")
                window_start = parse_time(item.get("start"), f"windows[{index}].start")
                window_end = parse_time(item.get("end"), f"windows[{index}].end")
                if window_start >= window_end:
                    raise AdaptiveValidationError(
                        f"windows[{index}].start must precede its end"
                    )
                if previous_end is not None and window_start < previous_end:
                    raise AdaptiveValidationError(
                        "walk-forward windows must be chronological and non-overlapping"
                    )
                previous_end = window_end
                windows.append(
                    {"start": window_start.isoformat(), "end": window_end.isoformat()}
                )
            start = parse_time(windows[0]["start"], "start")
            end = parse_time(windows[-1]["end"], "end")
            if end > self._now():
                raise AdaptiveValidationError("reassessment windows must be historical")
        else:
            start_raw = payload.get("start")
            end_raw = payload.get("end")
            if not start_raw or not end_raw:
                raise AdaptiveValidationError(
                    "start and end are required for a reassessment"
                )
            start = parse_time(start_raw, "start")
            end = parse_time(end_raw, "end")
            if start >= end:
                raise AdaptiveValidationError("start must precede end")
            if end > self._now():
                raise AdaptiveValidationError("reassessment windows must be historical")
        try:
            policy = self._policy(policy_id) or {}
        except AdaptiveNotFoundError:
            policy = {}
        scope = reassessment_scope(
            policy,
            windows or [{"start": start.isoformat(), "end": end.isoformat()}],
        )
        if windows:
            windows = [
                {**window, **_scope_fields(scope["windows"][index])}
                for index, window in enumerate(windows)
            ]
        if scope["in_sample"] and not bool(payload.get("allow_in_sample", False)):
            raise AdaptiveValidationError(
                "reassessment windows overlap the policy's own data: "
                + describe_overlaps(scope)
                + ". Evaluate a window after the training and validation splits, or "
                "pass allow_in_sample=true to record explicitly flagged in-sample "
                "evidence that suitability never uses."
            )
        config = {
            "regime": regime,
            "scope": scope,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "windows": windows,
            "max_transitions": whole(
                payload.get("max_transitions", 500), "max_transitions", minimum=1, maximum=5000
            ),
            "bounded_seconds": min(
                self._config.max_reassessment_seconds,
                whole(
                    payload.get("bounded_seconds", self._config.max_reassessment_seconds),
                    "bounded_seconds",
                    minimum=1,
                    maximum=self._config.max_reassessment_seconds,
                ),
            ),
            "reuse_valid_results": bool(payload.get("reuse_valid_results", True)),
        }
        assessment_id = stable_id(
            "ada_",
            policy_id,
            regime,
            start.isoformat(),
            end.isoformat(),
            ",".join(f"{item['start']}/{item['end']}" for item in windows),
        )
        existing = self._store.reassessment(assessment_id)
        if existing is not None:
            return {
                "ok": True,
                "reassessment": existing,
                "inserted": False,
                "idempotent": True,
            }
        record: dict[str, Any] = {
            "reassessment_id": assessment_id,
            "policy_id": policy_id,
            "regime": regime,
            "reason": reason,
            "trigger": text(payload.get("trigger")),
            "policy_version": text(payload.get("policy_version")),
            "config": config,
            "in_sample": scope["in_sample"],
            "out_of_sample": scope["out_of_sample"],
            "evidence_scope": scope,
            "status": ReassessmentStatus.PENDING_REASSESSMENT.value,
            "created_at": self._now().isoformat(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "cached": False,
            "advisory_only": True,
            "production_authority": False,
            "trains_policy": False,
        }
        stored = self._store.save_reassessment(assessment_id, policy_id, record["status"], record)
        return {"ok": True, "reassessment": stored, "inserted": True}

    def _cached_reassessment_evaluation(
        self,
        policy_id: str,
        regime: str,
        start: str,
        end: str,
        policy_version: str,
    ) -> dict[str, Any] | None:
        rows = self._store.regime_evaluations(policy_id, limit=500)["items"]
        for item in rows:
            if (
                item.get("regime") == regime
                and item.get("start") == start
                and item.get("end") == end
                and item.get("sufficient")
                and not item.get("in_sample")
                and (
                    not policy_version
                    or text(item.get("policy_version")) in {"", policy_version}
                )
            ):
                return dict(item)
        return None

    def run_reassessment(self, reassessment_id: str) -> dict[str, Any]:
        record = self._store.reassessment(reassessment_id)
        if record is None:
            raise AdaptiveNotFoundError(f"unknown reassessment {reassessment_id!r}")
        config = dict(record.get("config") or {})
        policy_id = text(record.get("policy_id"))
        windows = [dict(item) for item in config.get("windows") or [] if isinstance(item, Mapping)]
        try:
            policy = self._policy(policy_id) or {}
        except AdaptiveNotFoundError:
            policy = {}
        stored_scope = config.get("scope")
        scope_verified = bool(
            stored_scope.get("verified") if isinstance(stored_scope, Mapping) else False
        )
        current_version = text(
            policy.get("artifact_version") or policy.get("artifact_hash")
        )
        recorded_version = text(record.get("policy_version"))
        if record.get("status") == ReassessmentStatus.COMPLETED.value:
            result: Mapping[str, Any] = (
                record["result"] if isinstance(record.get("result"), Mapping) else {}
            )
            result_version = text(result.get("policy_version"))
            observed_versions = [
                value for value in (recorded_version, result_version) if value
            ]
            if current_version and any(
                value != current_version for value in observed_versions
            ):
                stale = {
                    **record,
                    "status": ReassessmentStatus.STALE_EVIDENCE.value,
                    "stale_at": self._now().isoformat(),
                    "error": (
                        "the completed reassessment belongs to a different policy artifact"
                    ),
                }
                self._store.save_reassessment(
                    reassessment_id, policy_id, stale["status"], stale
                )
                return {
                    "ok": False,
                    "reassessment": stale,
                    "reused": False,
                    "reason": stale["error"],
                }
            return {
                "ok": True,
                "reassessment": record,
                "reused": True,
                "reason": "already completed",
            }
        if record.get("status") == ReassessmentStatus.RUNNING.value:
            raise AdaptiveStateError("the reassessment is already running")
        if config.get("reuse_valid_results", True) and not windows:
            existing = self._store.regime_evaluations(policy_id, limit=500)["items"]
            for item in existing:
                if (
                    item.get("regime") == record.get("regime")
                    and item.get("start") == config.get("start")
                    and item.get("end") == config.get("end")
                    and item.get("sufficient")
                    and not item.get("in_sample")
                    and (
                        not current_version
                        or text(item.get("policy_version")) in {"", current_version}
                    )
                ):
                    updated = {
                        **record,
                        "status": ReassessmentStatus.COMPLETED.value,
                        "finished_at": self._now().isoformat(),
                        "result": item,
                        "cached": True,
                        "evaluation_triggered": False,
                        "policy_version": current_version or recorded_version,
                    }
                    self._store.save_reassessment(
                        reassessment_id, policy_id, updated["status"], updated
                    )
                    return {"ok": True, "reassessment": updated, "reused": True}
        training = self._training_service
        if training is None or not hasattr(training, "evaluate_regime_window"):
            updated = {
                **record,
                "status": ReassessmentStatus.FAILED.value,
                "finished_at": self._now().isoformat(),
                "error": "no PI-15 training service with a bounded regime evaluator",
                "evaluation_triggered": False,
            }
            self._store.save_reassessment(reassessment_id, policy_id, updated["status"], updated)
            return {"ok": False, "reassessment": updated, "reason": updated["error"]}
        run_version = current_version or recorded_version
        running = {
            **record,
            "status": ReassessmentStatus.RUNNING.value,
            "started_at": self._now().isoformat(),
            "policy_version": run_version,
        }
        self._store.save_reassessment(reassessment_id, policy_id, running["status"], running)
        started = time.monotonic()
        if windows:
            folds: list[dict[str, Any]] = []
            per_fold_transitions = max(
                1,
                whole(config.get("max_transitions", 500), "max_transitions")
                // len(windows),
            )
            bounded_seconds = float(
                config.get("bounded_seconds", self._config.max_reassessment_seconds)
            )
            try:
                for index, window in enumerate(windows):
                    if time.monotonic() - started > bounded_seconds:
                        raise AdaptiveResourceError(
                            "walk-forward reassessment exceeded its bounded time"
                        )
                    cached_evaluation = None
                    if config.get("reuse_valid_results", True):
                        cached_evaluation = self._cached_reassessment_evaluation(
                            policy_id,
                            text(record.get("regime")),
                            window["start"],
                            window["end"],
                            current_version,
                        )
                    response: dict[str, Any] = {}
                    if cached_evaluation is not None:
                        recorded = {"evaluation": cached_evaluation}
                    else:
                        response = training.evaluate_regime_window(
                            policy_id,
                            {
                                "regime": record.get("regime"),
                                "start": window["start"],
                                "end": window["end"],
                                "max_transitions": per_fold_transitions,
                                "reason": record.get("reason", ""),
                                "walk_forward": True,
                                "fold": index,
                            },
                        )
                        evaluation = dict(response.get("evaluation") or {})
                        recorded = self.record_regime_evaluation(
                            {
                                **evaluation,
                                # PI-14's ``policy_version`` is the adapter version;
                                # adaptive evidence is keyed to the trained artifact.
                                "policy_version": run_version,
                                # PI-14's ``config_hash`` identifies the evaluation
                                # run; the evidence belongs to this policy's config.
                                "config_hash": text(
                                    policy.get("config_hash")
                                    or evaluation.get("config_hash")
                                ),
                                "policy_id": policy_id,
                                "regime": record.get("regime"),
                                "start": window["start"],
                                "end": window["end"],
                                "evaluated_at": self._now().isoformat(),
                                "evaluation_id": evaluation.get("evaluation_id")
                                or f"{reassessment_id}:fold{index}",
                                "metrics": evaluation.get("metrics") or {},
                                "sample_size": evaluation.get("transitions")
                                or evaluation.get("sample_size")
                                or 0,
                                "scope_verified": scope_verified,
                                **_scope_fields(window),
                            }
                        )
                    folds.append(
                        {
                            "index": index,
                            "start": window["start"],
                            "end": window["end"],
                            "evaluation": recorded["evaluation"],
                            "cached": cached_evaluation is not None,
                            "baselines": list(response.get("baselines") or []),
                            "comparison_id": response.get("comparison_id"),
                            **_scope_fields(window),
                        }
                    )
                elapsed = time.monotonic() - started
                if elapsed > bounded_seconds:
                    raise AdaptiveResourceError(
                        "walk-forward reassessment exceeded its bounded time"
                    )
                returns = [
                    _policy_metric(
                        fold["evaluation"].get("metrics") or {},
                        "financial.total_return",
                    )
                    for fold in folds
                ]
                numeric_returns = [value for value in returns if value is not None]
                fold_in_sample = any(fold["in_sample"] for fold in folds)
                all_out_of_sample = bool(folds) and all(
                    fold["out_of_sample"] is True for fold in folds
                )
                aggregate = {
                    "fold_count": len(folds),
                    "mean_total_return": (
                        sum(numeric_returns) / len(numeric_returns)
                        if numeric_returns
                        else None
                    ),
                    "chronological": True,
                    "non_overlapping": True,
                    "per_fold_transition_limit": per_fold_transitions,
                    "in_sample": fold_in_sample,
                    "out_of_sample": (
                        True if all_out_of_sample else False if fold_in_sample else None
                    ),
                    "scope_verified": bool((config.get("scope") or {}).get("verified")),
                }
                all_insufficient = bool(folds) and all(
                    not fold["evaluation"].get("sufficient") for fold in folds
                )
                updated = {
                    **record,
                    "status": (
                        ReassessmentStatus.INSUFFICIENT_EVIDENCE.value
                        if all_insufficient
                        else ReassessmentStatus.COMPLETED.value
                    ),
                    "finished_at": self._now().isoformat(),
                    "elapsed_seconds": round(elapsed, 6),
                    "result": {
                        "walk_forward": True,
                        "folds": folds,
                        "aggregate": aggregate,
                    },
                    "cached": False,
                    "evaluation_triggered": any(not fold["cached"] for fold in folds),
                    "policy_version": run_version,
                    "in_sample": aggregate["in_sample"],
                    "out_of_sample": aggregate["out_of_sample"],
                }
                self._store.save_reassessment(
                    reassessment_id, policy_id, updated["status"], updated
                )
                return {"ok": True, "reassessment": updated, "reused": False}
            except Exception as exc:  # noqa: BLE001
                updated = {
                    **record,
                    "status": ReassessmentStatus.FAILED.value,
                    "finished_at": self._now().isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "evaluation_triggered": False,
                }
                self._store.save_reassessment(
                    reassessment_id, policy_id, updated["status"], updated
                )
                self._last_error = updated["error"]
                return {"ok": False, "reassessment": updated, "reason": updated["error"]}
        single_window_scope = {
            **_scope_fields(((stored_scope or {}).get("windows") or [{}])[0]),
            "scope_verified": scope_verified,
        }
        try:
            response = training.evaluate_regime_window(
                policy_id,
                {
                    "regime": record.get("regime"),
                    "start": config.get("start"),
                    "end": config.get("end"),
                    "max_transitions": config.get("max_transitions", 500),
                    "reason": record.get("reason", ""),
                },
            )
            evaluation = dict(response.get("evaluation") or {})
            recorded = self.record_regime_evaluation(
                {
                    **evaluation,
                    # PI-14's ``policy_version`` is the adapter version; adaptive
                    # evidence is keyed to the trained artifact.
                    "policy_version": run_version,
                    # PI-14's ``config_hash`` identifies the evaluation run; the
                    # evidence belongs to this policy's configuration.
                    "config_hash": text(
                        policy.get("config_hash") or evaluation.get("config_hash")
                    ),
                    "policy_id": policy_id,
                    "regime": record.get("regime"),
                    "start": config.get("start"),
                    "end": config.get("end"),
                    "evaluated_at": self._now().isoformat(),
                    "evaluation_id": evaluation.get("evaluation_id"),
                    "metrics": evaluation.get("metrics") or {},
                    "sample_size": evaluation.get("transitions")
                    or evaluation.get("sample_size")
                    or 0,
                    **single_window_scope,
                }
            )
            elapsed = time.monotonic() - started
            if elapsed > self._config.max_reassessment_seconds:
                raise AdaptiveResourceError("reassessment exceeded its bounded time")
            updated = {
                **record,
                "status": (
                    ReassessmentStatus.INSUFFICIENT_EVIDENCE.value
                    if not recorded["evaluation"].get("sufficient")
                    else ReassessmentStatus.COMPLETED.value
                ),
                "finished_at": self._now().isoformat(),
                "elapsed_seconds": round(elapsed, 6),
                "result": {
                    **recorded["evaluation"],
                    "baselines": list(response.get("baselines") or []),
                    "conditions": dict(response.get("conditions") or {}),
                    "comparison_id": response.get("comparison_id"),
                },
                "cached": False,
                "evaluation_triggered": True,
                "evaluation_id": evaluation.get("evaluation_id"),
                "policy_version": run_version,
                "in_sample": single_window_scope["in_sample"],
                "out_of_sample": single_window_scope["out_of_sample"],
            }
            self._store.save_reassessment(reassessment_id, policy_id, updated["status"], updated)
            return {"ok": True, "reassessment": updated, "reused": False}
        except Exception as exc:  # noqa: BLE001
            updated = {
                **record,
                "status": ReassessmentStatus.FAILED.value,
                "finished_at": self._now().isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
                "evaluation_triggered": False,
            }
            self._store.save_reassessment(reassessment_id, policy_id, updated["status"], updated)
            self._last_error = updated["error"]
            return {"ok": False, "reassessment": updated, "reason": updated["error"]}

    def reassessments(
        self, policy_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return {
            "ok": True,
            **self._store.reassessments(policy_id, limit=limit, offset=offset),
        }

    def reassessment(self, reassessment_id: str) -> dict[str, Any]:
        found = self._store.reassessment(reassessment_id)
        if found is None:
            raise AdaptiveNotFoundError(f"unknown reassessment {reassessment_id!r}")
        return found


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
