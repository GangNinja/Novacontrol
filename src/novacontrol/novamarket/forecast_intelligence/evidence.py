"""Read-only evidence adapters over PI-05 through PI-11 and PI-16.

The coordinator never calls a forecasting model. It reads immutable training
runs, performance snapshots, forecast contracts, calibration records and PI-11
evaluation reports through the callbacks supplied by NovaMarketService.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from novacontrol.novamarket.forecast_intelligence.metrics import (
    merge_metrics,
    normalise_metrics,
)
from novacontrol.novamarket.forecast_intelligence.models import (
    ForecastIntelligenceSources,
    ModelEvidence,
    normalise_family,
)

_MAX_ERRORS = 12
_MAX_WARNINGS = 12


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        candidate = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            candidate = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=UTC)
    return candidate.astimezone(UTC).isoformat()


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _first(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _split_bounds(report: Mapping[str, Any], key: str) -> tuple[str, str]:
    splits = _mapping(report.get("splits"))
    block = _mapping(splits.get(key))
    return _iso(_first(block, "start", "start_at", "from")), _iso(
        _first(block, "end", "end_at", "to")
    )


def _classifier(payload: object) -> dict[str, Any]:
    body = _mapping(payload)
    if not body:
        return {}
    return {
        **body,
        "accuracy": _first(body, "accuracy"),
        "macro_f1": _first(body, "macro_f1", "f1"),
        "balanced_accuracy": _first(body, "balanced_accuracy"),
        "samples": int(_first(body, "samples") or 0),
        "probabilities_calibrated": bool(body.get("probabilities_calibrated")),
    }


def _model_row(
    *,
    model_id: str,
    version: str,
    family: str,
    instrument_id: str,
    horizon: str,
    validation: Mapping[str, Any] | None,
    test: Mapping[str, Any] | None,
    classifier_validation: Mapping[str, Any] | None,
    classifier_test: Mapping[str, Any] | None,
    dataset_version: str,
    feature_set_version: str,
    training_dataset_version: str,
    resolution: str,
    start: str,
    end: str,
    regimes: Mapping[str, Any] | None,
    regime_metrics: Mapping[str, Any] | None,
    artifact_ref: str,
    as_of: str,
    state: str = "eligible",
    calibration_status: str = "uncalibrated",
    calibration: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> ModelEvidence:
    return ModelEvidence(
        model_id=str(model_id or "unknown"),
        version=str(version or "unspecified"),
        family=normalise_family(family or model_id),
        instrument_id=str(instrument_id or ""),
        horizon=str(horizon or ""),
        selection_metrics=normalise_metrics(validation),
        performance_metrics=normalise_metrics(test),
        classifier_validation=_classifier(classifier_validation),
        classifier_test=_classifier(classifier_test),
        dataset_version=str(dataset_version or ""),
        feature_set_version=str(feature_set_version or ""),
        training_dataset_version=str(training_dataset_version or ""),
        resolution=str(resolution or ""),
        evaluation_start=start,
        evaluation_end=end,
        validation_samples=int((validation or {}).get("samples") or 0),
        test_samples=int((test or {}).get("samples") or 0),
        regimes={
            str(key): int(value)
            for key, value in (regimes or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
        regime_metrics={
            str(key): dict(value)
            for key, value in (regime_metrics or {}).items()
            if isinstance(value, Mapping)
        },
        artifact_ref=str(artifact_ref or ""),
        state=str(state or "eligible"),
        calibration_status=str(calibration_status or "uncalibrated"),
        calibration=dict(calibration or {}),
        source="training_run",
        as_of=as_of,
        warnings=tuple(dict.fromkeys(str(item) for item in warnings)),
    )


class ForecastEvidenceReader:
    def __init__(self, sources: ForecastIntelligenceSources, config: Any) -> None:
        self.sources = sources
        self.config = config
        self.errors: list[str] = []

    # -- safe callback access -------------------------------------------------
    def _call(self, name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
        callback = getattr(self.sources, name, None)
        if callback is None:
            return default
        try:
            return callback(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - missing evidence is explicit
            if len(self.errors) < _MAX_ERRORS:
                self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return default

    def _rows(self, name: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        body = self._call(name, *args, default=None, **kwargs)
        if isinstance(body, Mapping):
            for key in (
                "models",
                "performance",
                "forecasts",
                "runs",
                "evaluations",
                "data",
                "rows",
            ):
                if key in body and isinstance(body[key], (list, tuple)):
                    return [dict(item) for item in body[key] if isinstance(item, Mapping)]
            return [dict(body)] if body else []
        if isinstance(body, (list, tuple)):
            return [dict(item) for item in body if isinstance(item, Mapping)]
        return []

    # -- model registry -------------------------------------------------------
    def registry(self, instrument_id: str = "", horizon: str = "") -> list[dict[str, Any]]:
        rows = self._rows("models", instrument_id=instrument_id or None, horizon=horizon or None)
        rows.extend(
            self._rows(
                "ensemble_models",
                instrument_id=instrument_id or None,
                horizon=horizon or None,
            )
        )
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            model_id = str(row.get("model_id") or row.get("family") or "").strip()
            if not model_id:
                continue
            version = str(row.get("version") or row.get("model_version") or "unspecified")
            supported = row.get("supported_horizons") or (horizon,)
            horizons = (
                tuple(str(item) for item in supported)
                if isinstance(supported, (list, tuple))
                else (str(supported),)
            )
            if horizon and horizon not in horizons and row.get("horizon") not in {horizon, ""}:
                continue
            row_instrument = str(row.get("instrument_id") or instrument_id or "")
            unique.setdefault((model_id, version, row_instrument), dict(row))
        return list(unique.values())[: max(1, int(self.config.max_models))]

    # -- training evidence ----------------------------------------------------
    def training_evidence(
        self,
        instrument_id: str,
        horizon: str,
        *,
        as_of: str = "",
    ) -> dict[str, ModelEvidence]:
        runs = self._rows("training_runs", limit=max(int(self.config.max_evaluations) * 4, 20))
        registry = self.registry(instrument_id, horizon)
        states: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for row in registry:
            states[
                (
                    str(row.get("model_id") or ""),
                    str(row.get("version") or ""),
                    str(row.get("instrument_id") or ""),
                )
            ] = row
        evidence: dict[str, ModelEvidence] = {}
        cutoff = _iso(as_of)
        for run in runs:
            run_instrument = str(run.get("instrument_id") or "")
            run_horizon = str(run.get("horizon") or "")
            if instrument_id and run_instrument and run_instrument != instrument_id:
                continue
            if horizon and run_horizon and run_horizon != horizon:
                continue
            finished = _iso(_first(run, "finished_at", "created_at", "at"))
            if cutoff and finished and finished > cutoff:
                continue
            report = _mapping(run.get("report"))
            body = report or run
            kind = str(run.get("kind") or body.get("kind") or "").lower()
            dataset_version = str(
                _first(body, "dataset_version", "training_dataset_version")
                or _first(run, "dataset_version", "training_dataset_version")
                or ""
            )
            feature_set_version = str(_first(body, "feature_set_version") or "unspecified")
            resolution = str(_first(body, "resolution") or "")
            validation_start, validation_end = _split_bounds(body, "validation")
            candidates: list[tuple[str, Mapping[str, Any]]] = []
            models_block = body.get("models")
            families_block = body.get("families")
            if isinstance(models_block, Mapping):
                candidates.extend(
                    (str(key), _mapping(value)) for key, value in models_block.items()
                )
            if isinstance(families_block, Mapping):
                candidates.extend(
                    (str(key), _mapping(value)) for key, value in families_block.items()
                )
            if not candidates:
                candidates.append(
                    (
                        str(
                            run.get("model_id") or "ensemble"
                            if kind == "ensemble"
                            else str(run.get("model_id") or "unknown")
                        ),
                        body,
                    )
                )
            test_used = bool(
                body.get("test_used_for_selection") or run.get("test_used_for_selection") or False
            )
            for key, candidate_row in candidates:
                model_id = str(
                    candidate_row.get("model_id")
                    or candidate_row.get("family")
                    or key
                    or run.get("model_id")
                    or "unknown"
                )
                version = str(
                    candidate_row.get("version")
                    or candidate_row.get("model_version")
                    or "unspecified"
                )
                family = normalise_family(candidate_row.get("family") or key or model_id)
                registry_row = (
                    states.get((model_id, version, run_instrument))
                    or states.get((model_id, version, ""))
                    or states.get((model_id, "", run_instrument))
                    or states.get((model_id, "", ""))
                    or {}
                )
                warnings: list[str] = []
                if test_used:
                    warnings.append(
                        "training report declares test-set selection; "
                        "validation evidence withheld from selection"
                    )
                if not dataset_version:
                    warnings.append("training run does not name a dataset version")
                state = str(registry_row.get("state") or "eligible")
                calibration_status = str(
                    registry_row.get("calibration")
                    or registry_row.get("calibration_status")
                    or "uncalibrated"
                )
                model = _model_row(
                    model_id=model_id,
                    version=version,
                    family=family,
                    instrument_id=run_instrument or instrument_id,
                    horizon=run_horizon or horizon,
                    validation={} if test_used else _mapping(candidate_row.get("validation")),
                    test=_mapping(candidate_row.get("test")),
                    classifier_validation=_mapping(
                        candidate_row.get("classifier_validation")
                        or candidate_row.get("validation_classifier")
                    ),
                    classifier_test=_mapping(candidate_row.get("classifier_test")),
                    dataset_version=dataset_version,
                    feature_set_version=feature_set_version,
                    training_dataset_version=dataset_version,
                    resolution=resolution,
                    start=validation_start,
                    end=validation_end or finished,
                    regimes=_mapping(
                        candidate_row.get("regime_samples") or body.get("regime_samples")
                    ),
                    regime_metrics=_mapping(
                        candidate_row.get("regime_metrics") or body.get("regimes")
                    ),
                    artifact_ref=str(
                        _first(
                            _mapping(candidate_row.get("artifact")),
                            "regressor_path",
                            "path",
                            "location",
                        )
                        or registry_row.get("artifact_location")
                        or ""
                    ),
                    as_of=finished,
                    state=state,
                    calibration_status=calibration_status,
                    calibration=_mapping(registry_row.get("calibration")),
                    warnings=warnings,
                )
                evidence_key = (
                    model_id
                    if instrument_id or horizon
                    else f"{model_id}|{model.instrument_id}|{model.horizon}|{model.version}"
                )
                previous = evidence.get(evidence_key)
                if previous is None or (model.as_of or "") >= (previous.as_of or ""):
                    evidence[evidence_key] = model
        # A registry row without training evidence is still reported, but never
        # becomes selectable. This is how an unavailable model stays visible.
        for (model_id, version, state_instrument), state_row in states.items():
            row_instrument = state_instrument or instrument_id
            row_horizon = str(state_row.get("horizon") or horizon)
            if any(
                item.model_id == model_id
                and item.version == version
                and item.instrument_id == row_instrument
                for item in evidence.values()
            ):
                continue
            evidence_key = (
                model_id
                if instrument_id or horizon
                else f"{model_id}|{row_instrument}|{row_horizon}|{version}"
            )
            if evidence_key in evidence:
                continue
            evidence[evidence_key] = _model_row(
                model_id=model_id,
                version=version,
                family=str(state_row.get("family") or state_row.get("kind") or model_id),
                instrument_id=row_instrument,
                horizon=row_horizon,
                validation={},
                test={},
                classifier_validation={},
                classifier_test={},
                dataset_version=str(state_row.get("dataset_version") or ""),
                feature_set_version=str(state_row.get("feature_set_version") or "unspecified"),
                training_dataset_version=str(state_row.get("dataset_version") or ""),
                resolution="",
                start="",
                end="",
                regimes={},
                regime_metrics={},
                artifact_ref=str(state_row.get("artifact_location") or ""),
                as_of="",
                state=str(state_row.get("state") or "unavailable"),
                calibration_status=str(state_row.get("calibration") or "unavailable"),
                warnings=("no validation training run is available for this registry entry",),
            )
        return evidence

    def performance_evidence(
        self,
        instrument_id: str,
        horizon: str,
        *,
        as_of: str = "",
    ) -> list[dict[str, Any]]:
        rows = self._rows(
            "performance",
            model_id=None,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=max(int(self.config.max_evaluations) * 4, 20),
        )
        cutoff = _iso(as_of)
        result = []
        for row in rows:
            created = _iso(_first(row, "created_at", "at"))
            if cutoff and created and created > cutoff:
                continue
            metrics = _mapping(row.get("metrics"))
            result.append(
                {
                    **row,
                    "metrics": metrics,
                    "normalised": normalise_metrics(metrics),
                    "created_at": created,
                    "model_id": str(row.get("model_id") or metrics.get("model_id") or ""),
                    "version": str(
                        _first(row, "version", "model_version")
                        or _first(metrics, "version", "model_version")
                        or "unspecified"
                    ),
                    "family": normalise_family(
                        _first(row, "family") or _first(metrics, "family") or row.get("model_id")
                    ),
                }
            )
        return result

    def attach_performance(
        self,
        evidence: dict[str, ModelEvidence],
        instrument_id: str,
        horizon: str,
        *,
        as_of: str = "",
    ) -> dict[str, ModelEvidence]:
        rows = self.performance_evidence(instrument_id, horizon, as_of=as_of)
        for row in rows:
            model_id = str(row.get("model_id") or "")
            if not model_id:
                continue
            current = evidence.get(model_id)
            if current is None:
                current = _model_row(
                    model_id=model_id,
                    version=str(row.get("version") or "unspecified"),
                    family=str(row.get("family") or model_id),
                    instrument_id=instrument_id,
                    horizon=horizon,
                    validation={},
                    test=row["metrics"],
                    classifier_validation={},
                    classifier_test={},
                    dataset_version=str(row.get("dataset_version") or ""),
                    feature_set_version=str(row.get("feature_set_version") or "unspecified"),
                    training_dataset_version=str(row.get("dataset_version") or ""),
                    resolution=str(row.get("resolution") or ""),
                    start="",
                    end=str(row.get("created_at") or ""),
                    regimes=_mapping(row.get("regime_samples")),
                    regime_metrics=_mapping(row.get("regime_metrics")),
                    artifact_ref="",
                    as_of=str(row.get("created_at") or ""),
                    warnings=("no validation training evidence; test score is monitoring only",),
                )
                evidence[model_id] = current
                continue
            existing = dict(current.performance_metrics)
            observed = str(row.get("created_at") or "")
            current = replace(
                current,
                performance_metrics=merge_metrics(existing, row["metrics"]),
                test_samples=max(
                    int(current.test_samples),
                    int(row["normalised"].get("samples") or 0),
                ),
                evaluation_end=max(value for value in (current.evaluation_end, observed) if value)
                if current.evaluation_end or observed
                else "",
                source="training_run+performance",
            )
            evidence[model_id] = current
        return evidence

    def attach_calibration(
        self,
        evidence: dict[str, ModelEvidence],
        instrument_id: str,
        horizon: str,
    ) -> dict[str, ModelEvidence]:
        for model_id, current in list(evidence.items()):
            body = self._call(
                "calibration",
                model_id,
                horizon,
                instrument_id=instrument_id,
                default=None,
            )
            if not isinstance(body, Mapping) or not body:
                continue
            payload = _mapping(body.get("calibration")) or dict(body)
            status = str(body.get("status") or payload.get("status") or "unavailable")
            evidence[model_id] = replace(
                current,
                calibration_status=status,
                calibration=payload,
            )
        return evidence

    # -- forecast contracts and evaluation evidence ---------------------------
    def forecasts(
        self,
        instrument_id: str,
        horizon: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in ("forecasts",):
            rows.extend(
                self._rows(
                    name,
                    instrument_id=instrument_id,
                    horizon=horizon,
                    limit=limit,
                )
            )
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            body = _mapping(row.get("forecast")) or dict(row)
            identifier = str(body.get("forecast_id") or "")
            if not identifier:
                identifier = str(
                    body.get("id")
                    or body.get("created_at")
                    or f"{body.get('model_id')}:{body.get('horizon')}:{body.get('target_at')}"
                )
            unique.setdefault(identifier, {**dict(row), **body})
        return list(unique.values())[: max(1, min(int(limit), int(self.config.max_records)))]

    def evaluation_reports(self) -> list[dict[str, Any]]:
        reports = self._rows("evaluations", limit=max(int(self.config.max_evaluations), 1))
        hydrated: list[dict[str, Any]] = []
        for report in reports[: int(self.config.max_evaluations)]:
            evaluation_id = str(report.get("evaluation_id") or "")
            full = self._call("evaluation", evaluation_id, default=None) if evaluation_id else None
            hydrated.append(_mapping(full) or report)
        return hydrated

    def evaluation_records(
        self,
        instrument_id: str = "",
        horizon: str = "",
        model_id: str = "",
        *,
        as_of: str = "",
    ) -> list[dict[str, Any]]:
        cutoff = _iso(as_of)
        records: dict[str, dict[str, Any]] = {}
        for report in self.evaluation_reports():
            samples = _list(report.get("records_sample"))
            if not samples:
                samples = _list(report.get("records"))
            for sample in samples:
                if not isinstance(sample, Mapping):
                    continue
                body = dict(sample)
                if instrument_id and str(body.get("instrument_id") or "") not in {
                    "",
                    instrument_id,
                }:
                    continue
                if horizon and str(body.get("horizon") or "") not in {"", horizon}:
                    continue
                if model_id and str(body.get("model_id") or "") != model_id:
                    continue
                generated = _iso(body.get("generated_at"))
                if cutoff and generated and generated > cutoff:
                    continue
                identifier = str(body.get("forecast_id") or f"{len(records)}")
                body["generated_at"] = generated
                body["dataset_version"] = str(
                    body.get("dataset_version") or report.get("dataset_version") or ""
                )
                records.setdefault(identifier, body)
                if len(records) >= int(self.config.max_records):
                    return list(records.values())
        return list(records.values())

    def regime(self, instrument_id: str) -> dict[str, Any]:
        body = self._call("regime", instrument_id, default=None)
        return _mapping(body)

    def forecast_health(self) -> dict[str, Any]:
        body = self._call("health", default=None)
        return _mapping(body)


__all__ = ["ForecastEvidenceReader"]
