"""PI-11 orchestration over existing forecast stores and evidence.

No model is retrained, no forecast is generated and no order is submitted here.
The service reads PI-09 outcomes and NM-06 evaluated forecasts, scores them with
the deterministic metrics module, and persists an immutable report.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from novacontrol.novamarket.evaluation.metrics import evaluate_metrics, leakage_audit
from novacontrol.novamarket.evaluation.models import (
    EvaluationConfig,
    EvaluationError,
    EvaluationNotFoundError,
    EvaluationRecord,
    PI11_PHASE,
    REPORT_CONTRACT_VERSION,
    canonical_payload,
    record_from_mapping,
    record_sample,
    report_hash,
)
from novacontrol.novamarket.evaluation.store import EvaluationStore

_STAGE_ALIASES = {
    "initial": "t_minus_20",
    "t-20": "t_minus_20",
    "t_minus_20": "t_minus_20",
    "first_confirmation": "t_minus_5",
    "first-confirmation": "t_minus_5",
    "t-5": "t_minus_5",
    "t_minus_5": "t_minus_5",
    "final": "t_minus_1",
    "final_confirmation": "t_minus_1",
    "t-1": "t_minus_1",
    "t_minus_1": "t_minus_1",
}


def _stage(value: object) -> str:
    return _STAGE_ALIASES.get(str(value or "").strip().lower(), str(value or "").strip().lower())


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _ids(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _created_at_or_before(value: object, cutoff: datetime | None) -> bool:
    """Only claim scorecard evidence that already existed at the cutoff."""

    if cutoff is None:
        return True
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return False
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) <= cutoff


class PredictionEvaluationService:
    """Forecast quality evaluation with no financial or execution authority."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        prediction_store: Any | None = None,
        forecast_store: Any | None = None,
        market_data: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._store = EvaluationStore(path)
        self._prediction_store = prediction_store
        self._forecast_store = forecast_store
        self._market_data = market_data
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._last_error = ""

    @property
    def store(self) -> EvaluationStore:
        return self._store

    def attach_stores(
        self,
        *,
        prediction_store: Any | None = None,
        forecast_store: Any | None = None,
        market_data: Any | None = None,
    ) -> None:
        """Attach read-only stores when the application opens them lazily."""
        if prediction_store is not None:
            self._prediction_store = prediction_store
        if forecast_store is not None:
            self._forecast_store = forecast_store
        if market_data is not None:
            self._market_data = market_data

    def close(self) -> None:
        self._store.close()

    def status(self) -> dict[str, Any]:
        counts = self._store.counts()
        return {
            "ok": True,
            "phase": PI11_PHASE,
            "contract_version": REPORT_CONTRACT_VERSION,
            "initialised": True,
            "forecast_stores": {
                "lifecycle": self._prediction_store is not None,
                "nm06": self._forecast_store is not None,
                "target_bar_resolution": self._market_data is not None,
            },
            "evaluations": counts,
            "last_error": self._last_error,
            "authority": self._authority(),
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PI11_PHASE,
            "contract_version": REPORT_CONTRACT_VERSION,
            "sources": {
                "lifecycle": "immutable PI-09 forecast outcomes at T-20/T-5/T-1",
                "nm06": (
                    "stored NM-06 forecast contracts with evaluated outcomes; an "
                    "unresolved original forecast may use the first observed bar at "
                    "or after its target, with the observation timestamp retained"
                ),
                "records": (
                    "explicit caller-supplied records, allowed only with a declared "
                    "operational, reconstructed or synthetic origin"
                ),
            },
            "metrics": [
                "price MAE, RMSE, MAPE, sMAPE, bias and error distribution",
                "return error, bias, over/under-estimation and downside/upside error",
                "direction accuracy, precision, recall, F1 and confusion matrix",
                "calibrated multi-class Brier, log loss, calibration bins and ECE",
                "prediction-interval coverage and width against nominal coverage",
                "stored uncertainty labels and ensemble disagreement, kept separate",
                "chronological stability windows and degradation",
                "paired forecast-lifecycle checkpoint comparisons",
                "model comparison within identical horizons",
                "market-regime and horizon slices with sample-size caveats",
                "fixed-configuration walk-forward evaluation windows",
            ],
            "leakage_controls": [
                "target must follow generation",
                "actual observation may not precede the target",
                "information and model-training cutoffs may not follow generation",
                "as_of excludes records not yet generated",
                "equal-horizon comparisons only",
                "no test-set tuning and no automatic model promotion",
            ],
            "limitations": self._limitations(),
            "authority": self._authority(),
        }

    def evaluate(
        self,
        payload: Mapping[str, Any],
        *,
        records: Sequence[Mapping[str, Any]] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        config = EvaluationConfig.from_mapping(payload)
        explicit = list(records or payload.get("records") or ())
        explicit_inputs = [dict(item) for item in explicit]
        records_list, rejected, source_counts = self._collect(config, explicit)
        metrics = evaluate_metrics(records_list, config)
        model_evidence = self._model_evidence(config)
        audit = leakage_audit(records_list, rejected)
        data_digest = hashlib.sha256(
            canonical_payload([item.to_dict() for item in records_list]).encode("utf-8")
        ).hexdigest()
        created = self._clock()
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        created = created.astimezone(UTC)
        status = "completed" if records_list else "insufficient_data"
        if records_list and (
            audit["rejected_records"]
            or audit["delayed_observation_count"]
            or audit["missing_version_count"]
        ):
            status = "degraded"
        evaluation_id = "pev_" + hashlib.sha256(
            f"{config.hash}|{data_digest}|{created.isoformat()}".encode("utf-8")
        ).hexdigest()[:24]
        origins = {
            origin: sum(1 for item in records_list if item.origin == origin)
            for origin in sorted({item.origin for item in records_list})
        }
        auto_resolved_targets = sum(
            1
            for item in records_list
            if item.metadata.get("target_resolution")
            == "first_observed_bar_at_or_after_target"
        )
        body: dict[str, Any] = {
            "ok": True,
            "phase": PI11_PHASE,
            "evaluation_id": evaluation_id,
            "name": config.name,
            "created_at": created.isoformat(),
            "status": status,
            "samples": len(records_list),
            "config": config.to_dict(),
            "config_hash": config.hash,
            "data_digest": data_digest,
            "source_summary": {
                "counts": source_counts,
                "origins": origins,
                "records_loaded": sum(source_counts.values()),
                "records_used": len(records_list),
                "records_rejected": len(rejected),
            },
            "provenance": {
                "operational_forecasts": origins.get("historical_operational", 0),
                "research_reconstructions": origins.get("research_reconstruction", 0),
                "synthetic_fixtures": origins.get("synthetic_fixture", 0),
                "auto_resolved_targets": auto_resolved_targets,
                "no_regeneration": True,
                "statement": (
                    "Stored forecast contracts are read verbatim and no forecast is "
                    "regenerated at a historical timestamp. Stored target-time "
                    "outcomes are read verbatim; when an NM-06 target outcome was "
                    "never stored, the first observed bar at or after the target is "
                    "used and that reconstruction is counted here."
                ),
            },
            "metrics": metrics,
            "model_evidence": model_evidence,
            "leakage_audit": audit,
            "limitations": self._limitations(records_list),
            "records_sample": record_sample(records_list, 25),
            "record_count": len(records_list),
            # Persist the caller's raw explicit evidence, including rows that
            # were filtered or rejected, so reproduction can replay the same
            # audit instead of silently evaluating only the survivors.
            "input_records": (
                explicit_inputs
                if explicit or "records" in config.sources
                else []
            ),
            "report_hash": "",
        }
        body["report_hash"] = report_hash(body)
        stored = self._store.save(body) if persist else body
        if status == "insufficient_data":
            self._last_error = "no evaluated forecast records matched the configuration"
        return stored

    # -- record sources -----------------------------------------------------
    def _collect(
        self,
        config: EvaluationConfig,
        explicit: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[list[EvaluationRecord], list[dict[str, Any]], dict[str, int]]:
        """Collect, filter and de-duplicate the records a config selects.

        Shared by ``evaluate`` and ``collect_records`` so a downstream reader
        (PI-24's event-aware comparison) sees exactly the evidence the report
        would, including the same lifecycle-over-NM-06 de-duplication.
        """

        all_records: list[EvaluationRecord] = []
        rejected: list[dict[str, Any]] = []
        source_counts = {"lifecycle": 0, "nm06": 0, "records": 0}
        if "lifecycle" in config.sources:
            loaded, refused = self._lifecycle_records(config)
            all_records.extend(loaded)
            rejected.extend(refused)
            source_counts["lifecycle"] = len(loaded)
        if "nm06" in config.sources:
            loaded, refused = self._nm06_records(config)
            all_records.extend(loaded)
            rejected.extend(refused)
            source_counts["nm06"] = len(loaded)
        if "records" in config.sources or explicit:
            loaded, refused = self._explicit_records(config, list(explicit))
            all_records.extend(loaded)
            rejected.extend(refused)
            source_counts["records"] = len(loaded)
        filtered, refused = self._filter(all_records, config)
        rejected.extend(refused)
        # A lifecycle record is the canonical evidence when the same forecast
        # appears in more than one source. This de-duplicates by immutable id.
        unique: dict[str, EvaluationRecord] = {}
        for item in filtered:
            current = unique.get(item.forecast_id)
            if current is None or (current.source != "lifecycle" and item.source == "lifecycle"):
                unique[item.forecast_id] = item
        records_list = sorted(
            unique.values(),
            key=lambda item: (item.generated_at, item.forecast_id),
        )
        return records_list, rejected, source_counts

    def collect_records(
        self, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return the typed records a configuration selects, read-only.

        No report is written: callers that need a segmentation (event-labelled
        versus not) can score the same evidence with the same metric functions
        without inventing a second record pipeline.
        """

        config = EvaluationConfig.from_mapping(payload or {})
        records, rejected, source_counts = self._collect(config)
        return {
            "ok": True,
            "phase": PI11_PHASE,
            "config": config.to_dict(),
            "records": records,
            "record_count": len(records),
            "rejected": rejected[:50],
            "rejected_count": len(rejected),
            "source_counts": source_counts,
        }

    def _lifecycle_records(
        self, config: EvaluationConfig
    ) -> tuple[list[EvaluationRecord], list[dict[str, Any]]]:
        if self._prediction_store is None:
            return [], [{"reason": "lifecycle store is unavailable"}]
        outcomes: list[Any] = []
        try:
            # Load the bounded outcome set once, then filter on the immutable
            # forecast contract. The store's optional instrument query depends
            # on a cycle row; evaluation must not hide an outcome merely because
            # its cycle index is unavailable.
            outcomes = list(
                self._prediction_store.forecast_outcomes(
                    limit=min(config.max_records, 10_000)
                )
            )
        except Exception as exc:  # noqa: BLE001 - report source failure
            self._last_error = f"lifecycle outcomes: {type(exc).__name__}: {exc}"
            return [], [{"reason": self._last_error}]
        records: list[EvaluationRecord] = []
        rejected: list[dict[str, Any]] = []
        for outcome in outcomes:
            try:
                forecast = self._prediction_store.get_forecast(outcome.forecast_id)
                if forecast is None:
                    rejected.append(
                        {"forecast_id": outcome.forecast_id, "reason": "forecast contract missing"}
                    )
                    continue
                if forecast.generated_at > config.as_of:
                    rejected.append(
                        {"forecast_id": outcome.forecast_id, "reason": "generated after as_of"}
                    )
                    continue
                cycle = None
                try:
                    cycles = self._prediction_store.forecast_cycles(
                        cycle_id=outcome.cycle_id, limit=1
                    )
                    cycle = cycles[0] if cycles else None
                except Exception:  # noqa: BLE001 - regime metadata is optional
                    cycle = None
                metadata = dict(outcome.metadata or {})
                forecast_metadata = dict(forecast.metadata or {})
                regime = (
                    metadata.get("regime")
                    or forecast_metadata.get("regime")
                    or (cycle.metadata.get("regime") if cycle else None)
                    or "unknown"
                )
                record = EvaluationRecord(
                    forecast_id=forecast.forecast_id,
                    source="lifecycle",
                    instrument_id=forecast.instrument_id,
                    horizon=forecast.forecast_horizon.value,
                    generated_at=forecast.generated_at,
                    target_at=outcome.target_at,
                    reference_at=forecast.reference_at,
                    actual_observed_at=outcome.actual_observed_at,
                    reference_price=float(forecast.reference_price),
                    predicted_price=float(outcome.predicted_price),
                    actual_price=float(outcome.actual_price),
                    predicted_return=float(outcome.predicted_return),
                    actual_return=float(outcome.actual_return),
                    predicted_direction=outcome.predicted_direction,
                    model_id=forecast.model_id,
                    model_version=forecast.model_version,
                    ensemble_id=forecast.ensemble_id,
                    dataset_version=forecast.dataset_version,
                    feature_set_version=forecast.feature_set_version,
                    cycle_id=outcome.cycle_id,
                    lifecycle_id=forecast.lifecycle_id,
                    stage=_stage(outcome.stage.value),
                    p_up=forecast.p_up,
                    p_down=forecast.p_down,
                    p_neutral=forecast.p_neutral,
                    probability_status=forecast.probability_status.value,
                    interval_lower=float(outcome.interval_lower),
                    interval_upper=float(outcome.interval_upper),
                    interval_width=float(outcome.interval_width),
                    inside_interval=bool(outcome.inside_interval),
                    uncertainty=forecast.uncertainty.value,
                    disagreement=float(getattr(forecast, "disagreement", 0.0) or 0.0),
                    regime=str(regime),
                    data_quality=forecast.data_quality.value,
                    data_freshness=forecast.data_freshness.value,
                    validity=forecast.validity.value,
                    origin="historical_operational",
                    metadata={
                        "observation_delay_seconds": outcome.observation_delay_seconds,
                        "cycle_policy_version": cycle.policy_version if cycle else "",
                    },
                )
                records.append(record)
            except Exception as exc:  # noqa: BLE001 - one bad record never hides others
                rejected.append(
                    {
                        "forecast_id": getattr(outcome, "forecast_id", ""),
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
        return records, rejected

    def _nm06_records(
        self, config: EvaluationConfig
    ) -> tuple[list[EvaluationRecord], list[dict[str, Any]]]:
        if self._forecast_store is None:
            return [], [{"reason": "NM-06 forecast store is unavailable"}]
        rows: list[dict[str, Any]] = []
        try:
            if config.instruments:
                for instrument in config.instruments:
                    rows.extend(
                        self._forecast_store.forecasts(
                            instrument_id=instrument,
                            limit=min(config.max_records, 500),
                        )
                    )
            else:
                rows.extend(
                    self._forecast_store.forecasts(limit=min(config.max_records, 500))
                )
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"NM-06 forecasts: {type(exc).__name__}: {exc}"
            return [], [{"reason": self._last_error}]
        records: list[EvaluationRecord] = []
        rejected: list[dict[str, Any]] = []
        for row in rows:
            try:
                outcome = dict(row.get("outcome") or {})
                record_status = str(row.get("status") or "").lower()
                if record_status in {"failed", "suspended", "invalid"}:
                    continue
                generated = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                if config.as_of is not None and generated > config.as_of:
                    rejected.append(
                        {"forecast_id": row.get("forecast_id"), "reason": "generated after as_of"}
                    )
                    continue
                resolved_target = False
                if outcome.get("actual_price") is None or outcome.get("actual_return") is None:
                    resolved = self._resolve_nm06_target(row, config)
                    if resolved is None:
                        continue
                    if "_missing" in resolved:
                        rejected.append(
                            {
                                "forecast_id": row.get("forecast_id"),
                                "reason": str(resolved["_missing"]),
                            }
                        )
                        continue
                    outcome = resolved
                    resolved_target = True
                p_up = row.get("p_up")
                p_down = row.get("p_down")
                p_neutral = row.get("p_sideways")
                supplied = p_up is not None and p_down is not None and p_neutral is not None
                predicted = str(row.get("predicted_direction") or "")
                if not predicted:
                    if supplied:
                        predicted = max(
                            (("up", _float(p_up)), ("down", _float(p_down)), ("neutral", _float(p_neutral))),
                            key=lambda item: item[1],
                        )[0]
                    predicted = predicted or "uncertain"
                interval_lower = row.get("lower_return")
                interval_upper = row.get("upper_return")
                reference_price = row.get("reference_price")
                if reference_price is None and row.get("predicted_price") is not None:
                    denominator = 1.0 + float(row.get("predicted_return", 0.0))
                    reference_price = (
                        float(row["predicted_price"]) / denominator
                        if denominator != 0.0
                        else float(row["predicted_price"])
                    )
                observed_at = (
                    outcome.get("observed_at")
                    or outcome.get("actual_observed_at")
                    or outcome.get("evaluated_at")
                    or row["target_at"]
                )
                record = EvaluationRecord(
                    forecast_id=str(row["forecast_id"]),
                    source="nm06",
                    instrument_id=str(row["instrument_id"]),
                    horizon=str(row.get("horizon") or ""),
                    generated_at=generated,
                    target_at=datetime.fromisoformat(str(row["target_at"]).replace("Z", "+00:00")),
                    reference_at=datetime.fromisoformat(str(row.get("data_cutoff") or row["created_at"]).replace("Z", "+00:00")),
                    actual_observed_at=datetime.fromisoformat(
                        str(observed_at).replace("Z", "+00:00")
                    ),
                    reference_price=float(reference_price or row.get("predicted_price") or 1),
                    predicted_price=float(row.get("predicted_price") or row.get("actual_price") or 1),
                    actual_price=float(outcome["actual_price"]),
                    predicted_return=float(row.get("predicted_return") or 0.0),
                    actual_return=float(outcome["actual_return"]),
                    predicted_direction=predicted,
                    model_id=str(row.get("model_id") or ""),
                    model_version=str(row.get("model_version") or ""),
                    dataset_version=str(row.get("dataset_version") or ""),
                    feature_set_version=str(row.get("feature_set_version") or ""),
                    p_up=float(p_up) if p_up is not None else None,
                    p_down=float(p_down) if p_down is not None else None,
                    p_neutral=float(p_neutral) if p_neutral is not None else None,
                    probability_status=str(row.get("probability_status") or ""),
                    interval_lower=float(interval_lower) if interval_lower is not None else None,
                    interval_upper=float(interval_upper) if interval_upper is not None else None,
                    inside_interval=outcome.get("inside_interval"),
                    uncertainty=str(row.get("uncertainty") or ""),
                    regime=str(row.get("regime") or "unknown"),
                    data_quality=str(row.get("data_quality") or ""),
                    data_freshness=str(row.get("data_freshness") or ""),
                    validity=str(row.get("validity") or ""),
                    origin="historical_operational",
                    metadata={
                        "methodology_version": row.get("methodology_version", ""),
                        "target_resolution": (
                            "stored_outcome"
                            if not resolved_target
                            else "first_observed_bar_at_or_after_target"
                        ),
                    },
                )
                records.append(record)
            except Exception as exc:  # noqa: BLE001
                rejected.append(
                    {
                        "forecast_id": row.get("forecast_id"),
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
        return records, rejected

    def _resolve_nm06_target(
        self, row: Mapping[str, Any], config: EvaluationConfig
    ) -> dict[str, Any] | None:
        """Resolve an original stored forecast against a later observed bar.

        Only the target observation is added. The forecast, its model version
        and its generated timestamp are read verbatim. A bar later than the
        evaluation cutoff is never used, and a missing target remains missing.
        """
        if self._market_data is None:
            return None
        target = datetime.fromisoformat(str(row["target_at"]).replace("Z", "+00:00"))
        cutoff = config.as_of
        if cutoff is None or target > cutoff:
            return None
        instrument = str(row["instrument_id"])
        try:
            page = self._market_data.bars(instrument, limit=1000, ascending=False)
        except Exception as exc:  # noqa: BLE001 - report the missing observation
            return {"_missing": f"target bar unavailable: {type(exc).__name__}: {exc}"}
        items = getattr(page, "items", page)
        candidates: list[tuple[datetime, Any]] = []
        for bar in items or ():
            observed = getattr(bar, "observed_at", None)
            if observed is None and isinstance(bar, Mapping):
                observed = bar.get("observed_at")
            if observed is None:
                continue
            if isinstance(observed, str):
                observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if observed >= target and observed <= cutoff:
                candidates.append((observed, bar))
        if not candidates:
            return {
                "_missing": (
                    f"no observed target bar at or after {target.isoformat()} "
                    f"within the evaluation cutoff"
                )
            }
        observed_at, bar = min(candidates, key=lambda item: item[0])
        close = (
            getattr(bar, "close", None)
            if not isinstance(bar, Mapping)
            else bar.get("close")
        )
        if close is None:
            return {"_missing": "target bar has no close price"}
        actual_price = float(close)
        reference = row.get("reference_price")
        if reference is None:
            denominator = 1.0 + float(row.get("predicted_return") or 0.0)
            predicted_price = float(row.get("predicted_price") or 0.0)
            reference = predicted_price / denominator if denominator else predicted_price
        reference = float(reference or 0.0)
        if reference <= 0:
            return {"_missing": "the forecast has no usable reference price"}
        actual_return = (actual_price / reference) - 1.0
        lower = row.get("lower_return")
        upper = row.get("upper_return")
        inside = (
            float(lower) <= actual_return <= float(upper)
            if lower is not None and upper is not None
            else None
        )
        return {
            "actual_price": actual_price,
            "actual_return": actual_return,
            "observed_at": observed_at.isoformat(),
            "inside_interval": inside,
        }

    def _explicit_records(
        self,
        config: EvaluationConfig,
        payload: Sequence[Mapping[str, Any]],
    ) -> tuple[list[EvaluationRecord], list[dict[str, Any]]]:
        records: list[EvaluationRecord] = []
        rejected: list[dict[str, Any]] = []
        if len(payload) > config.max_records:
            return [], [
                {
                    "reason": (
                        f"{len(payload)} explicit records exceed max_records="
                        f"{config.max_records}; the request was not partially evaluated"
                    )
                }
            ]
        for row in payload:
            try:
                values = dict(row)
                if not values.get("origin"):
                    raise EvaluationError(
                        "explicit records require an origin: historical_operational, "
                        "research_reconstruction or synthetic_fixture"
                    )
                values.setdefault("source", "records")
                records.append(record_from_mapping(values))
            except Exception as exc:  # noqa: BLE001
                rejected.append(
                    {
                        "forecast_id": row.get("forecast_id", ""),
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
        return records, rejected

    def _filter(
        self,
        records: Sequence[EvaluationRecord],
        config: EvaluationConfig,
    ) -> tuple[list[EvaluationRecord], list[dict[str, Any]]]:
        selected: list[EvaluationRecord] = []
        rejected: list[dict[str, Any]] = []
        instruments = set(config.instruments)
        horizons = set(config.horizons)
        models = set(config.models)
        stages = {_stage(item) for item in config.stages}
        for item in records:
            if instruments and item.instrument_id not in instruments:
                continue
            if horizons and item.horizon not in horizons:
                continue
            if models and not (
                item.model_id in models
                or item.model_version in models
                or item.ensemble_id in models
            ):
                continue
            if stages and _stage(item.stage) not in stages:
                continue
            if config.start is not None and item.target_at < config.start:
                continue
            if config.end is not None and item.target_at > config.end:
                continue
            if config.as_of is not None and item.generated_at > config.as_of:
                rejected.append(
                    {"forecast_id": item.forecast_id, "reason": "generated after as_of"}
                )
                continue
            if config.as_of is not None and item.actual_observed_at > config.as_of:
                rejected.append(
                    {
                        "forecast_id": item.forecast_id,
                        "reason": "target observed after as_of",
                    }
                )
                continue
            selected.append(item)
        return selected, rejected

    def _model_evidence(self, config: EvaluationConfig) -> dict[str, Any]:
        """Read the existing PI-05/06/07/08 out-of-sample scorecards.

        These scorecards are aggregate training/evaluation evidence, not
        per-target forecast records. They are kept in a separate section and
        are never mixed into the per-record price metrics above.
        """
        rows: list[dict[str, Any]] = []
        instruments = set(config.instruments)
        horizons = set(config.horizons)
        models = set(config.models)
        if self._forecast_store is not None:
            try:
                performance_rows = self._forecast_store.performance(
                    limit=min(200, config.max_records)
                )
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"model scorecards: {type(exc).__name__}: {exc}"
                performance_rows = []
            for row in performance_rows:
                if not _created_at_or_before(row.get("created_at"), config.as_of):
                    continue
                instrument_id = str(row.get("instrument_id") or "")
                horizon = str(row.get("horizon") or "")
                model_id = str(row.get("model_id") or "")
                if instruments and instrument_id not in instruments:
                    continue
                if horizons and horizon not in horizons:
                    continue
                if models and model_id not in models:
                    continue
                metrics = dict(row.get("metrics") or {})
                rows.append(
                    {
                        "source": "stored_evaluation",
                        "model_id": model_id,
                        "version": str(metrics.get("version") or ""),
                        "instrument_id": instrument_id,
                        "horizon": horizon,
                        "created_at": str(row.get("created_at") or ""),
                        "samples": int(row.get("samples") or 0),
                        "price_mae": metrics.get("price_mae", metrics.get("mae")),
                        "price_rmse": metrics.get("price_rmse", metrics.get("rmse")),
                        "direction_accuracy": metrics.get(
                            "directional_accuracy", metrics.get("direction_accuracy")
                        ),
                        "interval_coverage": metrics.get(
                            "interval_coverage", metrics.get("actual_coverage")
                        ),
                        "forecast_bias": metrics.get(
                            "forecast_bias", metrics.get("bias")
                        ),
                        "family": metrics.get("family") or model_id,
                    }
                )
        if self._prediction_store is not None:
            try:
                ensemble_rows = self._prediction_store.ensemble_evaluations(
                    limit=min(100, config.max_records)
                )
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"ensemble scorecards: {type(exc).__name__}: {exc}"
                ensemble_rows = []
            for row in ensemble_rows:
                if not _created_at_or_before(row.get("created_at"), config.as_of):
                    continue
                instrument_id = str(row.get("instrument_id") or "")
                horizon = str(row.get("horizon") or "")
                if instruments and instrument_id not in instruments:
                    continue
                if horizons and horizon not in horizons:
                    continue
                if models and "ensemble" not in models:
                    continue
                test = dict(row.get("test") or {})
                if not test:
                    continue
                rows.append(
                    {
                        "source": "stored_evaluation",
                        "model_id": "ensemble",
                        "version": str(row.get("config_id") or row.get("ensemble_id") or ""),
                        "instrument_id": instrument_id,
                        "horizon": horizon,
                        "created_at": str(row.get("created_at") or ""),
                        "samples": int(test.get("samples") or 0),
                        "price_mae": test.get("price_mae", test.get("mae")),
                        "price_rmse": test.get("price_rmse", test.get("rmse")),
                        "direction_accuracy": test.get(
                            "directional_accuracy", test.get("direction_accuracy")
                        ),
                        "interval_coverage": test.get(
                            "interval_coverage", test.get("actual_coverage")
                        ),
                        "forecast_bias": test.get("forecast_bias", test.get("bias")),
                        "family": "ensemble",
                    }
                )
        ranking: list[dict[str, Any]] = []
        for horizon in sorted({str(row["horizon"]) for row in rows}):
            candidates = [
                row
                for row in rows
                if row["horizon"] == horizon and row.get("price_mae") is not None
            ]
            candidates.sort(
                key=lambda row: (
                    float(row["price_mae"]),
                    float(row["price_rmse"])
                    if row.get("price_rmse") is not None
                    else float("inf"),
                )
            )
            for index, row in enumerate(candidates, start=1):
                ranking.append(
                    {
                        "horizon": horizon,
                        "rank": index,
                        "model_id": row["model_id"],
                        "version": row["version"],
                        "price_mae": row["price_mae"],
                    }
                )
        return {
            "scorecards": rows,
            "ranking": ranking,
            "ranking_rule": (
                "stored out-of-sample scorecards are ranked only within a matching "
                "horizon and are kept separate from per-target forecast metrics"
            ),
            "automatic_promotion": False,
            "note": (
                "PI-05/06/07/08 scorecards are aggregate evaluation evidence, not "
                "the per-target records used for price, direction or interval metrics."
            ),
        }

    # -- reproducibility -----------------------------------------------------
    def list(self, limit: int = 50) -> dict[str, Any]:
        rows = self._store.list(limit)
        return {"ok": True, "phase": PI11_PHASE, "evaluations": rows, "total": len(rows)}

    def get(self, evaluation_id: str) -> dict[str, Any]:
        body = self._store.get(evaluation_id)
        if body is None:
            raise EvaluationNotFoundError(f"unknown evaluation {evaluation_id!r}")
        return body

    def verify(self, evaluation_id: str) -> dict[str, Any]:
        return self._store.verify(evaluation_id)

    def reproduce(self, evaluation_id: str) -> dict[str, Any]:
        original = self.get(evaluation_id)
        config = EvaluationConfig.from_mapping(original["config"])
        regenerated = self.evaluate(
            config.to_dict(),
            records=original.get("input_records") or None,
            persist=False,
        )
        same_data = regenerated.get("data_digest") == original.get("data_digest")
        same_report = regenerated.get("report_hash") == original.get("report_hash")
        return {
            "ok": same_data and same_report,
            "evaluation_id": evaluation_id,
            "original_report_hash": original.get("report_hash"),
            "recomputed_report_hash": regenerated.get("report_hash"),
            "same_data_digest": same_data,
            "same_report": same_report,
            "original_samples": original.get("samples"),
            "recomputed_samples": regenerated.get("samples"),
            "detail": (
                "a mismatch means the underlying evaluated evidence changed; the "
                "original report was not overwritten"
            ),
        }

    # -- fixed statements ----------------------------------------------------
    @staticmethod
    def _authority() -> dict[str, Any]:
        return {
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "new_risk_engine": False,
            "new_paper_execution_engine": False,
            "broker_connected": False,
            "live_trading": False,
            "portfolio_mutations": False,
            "ledger_mutations": False,
            "orders_created": False,
            "fills_created": False,
            "forecast_authorizes_action": False,
            "paper_only": True,
        }

    @staticmethod
    def _limitations(records: Sequence[EvaluationRecord] = ()) -> Sequence[str]:
        limitations = [
            "Forecast accuracy is not trading profitability; the two are reported separately.",
            "A prediction interval's nominal confidence is evaluated, never assumed.",
            "Daily-bar replay cannot establish intraday fills, spreads or market impact.",
            "No historical index membership or delisting reconstruction is claimed.",
            "No model is retrained, promoted or reweighted by this evaluation.",
            "Past forecast errors do not guarantee future accuracy.",
        ]
        if not records:
            limitations.append(
                "No evaluated records matched; this is not evidence of zero error."
            )
        if any(not item.dataset_version and not item.feature_set_version for item in records):
            limitations.append(
                "Some stored forecasts did not carry a dataset or feature-set version."
            )
        if any(item.actual_observed_at > item.target_at for item in records):
            limitations.append(
                "Some target observations were delayed; the evaluation delay is recorded."
            )
        return limitations


__all__ = ["PredictionEvaluationService"]
