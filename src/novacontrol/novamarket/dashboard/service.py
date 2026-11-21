"""PI-20 read-only Prediction Intelligence Dashboard composition service.

Every number surfaced here comes from an existing PI-04..PI-19 record or is a
clearly labelled derived presentation value (direction from two recorded prices,
relative duration between two recorded timestamps).  This service never opens a
broker, submits an order, writes a ledger row, changes a position, activates a
policy or edits an autonomy setting.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from .models import (
    DASHBOARD_CONTRACT_VERSION,
    DASHBOARD_PHASE,
    DECISION_ACTIONS,
    DIRECTIONS,
    EXECUTION_AUTHORITY,
    FORECAST_ENGINES,
    HORIZON_ALIASES,
    HORIZON_FAMILIES,
    HORIZONS,
    RISK_AUTHORITY,
    DashboardFilters,
    DashboardNotFoundError,
    DashboardStoreError,
    DashboardValidationError,
    DataStatus,
    authority_contract,
)
from .store import DashboardStore

_ACTIVE_VALIDITY = {"valid", "unconfirmed", "pending", "available"}
_BAD_VALIDITY = {"expired", "invalid", "stale", "unavailable", "failed"}


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _values(value: object) -> list[Any]:
    """Preserve recorded strings and mapping records in one ordered list."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            result.append(dict(item))
        elif isinstance(item, (str, int, float, bool)):
            text = _text(item, 1200)
            if text:
                result.append(text)
    return result


def _names(value: object) -> list[str]:
    """Preserve an agent/name list whether the source stores strings or dicts."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    names: list[str] = []
    for item in value:
        name = (
            _text(
                _first(
                    item.get("agent_id"),
                    item.get("agent"),
                    item.get("id"),
                    item.get("name"),
                ),
                120,
            )
            if isinstance(item, Mapping)
            else _text(item, 120)
        )
        if name and name not in names:
            names.append(name)
    return names


def _text(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _iso(value: object) -> str:
    """One canonical UTC timestamp form for every comparison and display.

    Source records can carry different UTC offsets.  Comparing their raw ISO
    strings is not point-in-time correct, so timestamps are normalized before
    any replay, late-checkpoint or ordering decision.
    """
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    text = _text(value, 80)
    if not text or len(text) == 10:
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _direction_disagreement(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Directions differ across distinct models at a compatible target.

    Multiple versions of one model are not counted as independent model
    disagreement; the first usable direction for each model is compared.
    """
    by_model: dict[str, str] = {}
    for row in rows:
        direction = _text(row.get("direction"), 20)
        if direction in {"", "uncertain", "invalid"}:
            continue
        model = _text(
            _first(
                row.get("model_id"),
                row.get("engine"),
                row.get("forecast_id"),
            ),
            120,
        ).lower()
        if model and model not in by_model:
            by_model[model] = direction
    return len(set(by_model.values())) > 1


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(value: object, fallback: int = 0) -> int:
    parsed = _number(value)
    return int(parsed) if parsed is not None else fallback


def _bool(value: object) -> bool:
    return value is True


def _first(*values: object) -> object:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _direction(row: Mapping[str, Any]) -> tuple[str, str]:
    explicit = _text(_first(row.get("direction"), row.get("predicted_direction")), 20).lower()
    aliases = {"bullish": "up", "bearish": "down", "flat": "neutral", "unknown": "uncertain"}
    explicit = aliases.get(explicit, explicit)
    if explicit in DIRECTIONS:
        return explicit, "recorded"
    expected = _number(_first(row.get("expected_return"), row.get("predicted_return")))
    reference = _number(_first(row.get("reference_price"), row.get("price_at_forecast")))
    predicted = _number(_first(row.get("predicted_price"), row.get("target_price")))
    if expected is not None:
        if expected > 0:
            return "up", "derived_from_recorded_expected_return"
        if expected < 0:
            return "down", "derived_from_recorded_expected_return"
        return "neutral", "derived_from_recorded_expected_return"
    if reference is not None and predicted is not None:
        if predicted > reference:
            return "up", "derived_from_recorded_prices"
        if predicted < reference:
            return "down", "derived_from_recorded_prices"
        return "neutral", "derived_from_recorded_prices"
    return "uncertain", "unavailable"


def _freshness(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            "state": _text(_first(value.get("state"), value.get("status")), 40).lower(),
            "as_of": _iso(_first(value.get("as_of"), value.get("observed_at"))),
            "age_seconds": _number(value.get("age_seconds")),
            "note": _text(value.get("note"), 300),
        }
    text = _text(value, 40).lower()
    return {"state": text, "as_of": "", "age_seconds": None, "note": ""}


class PredictionDashboardService:
    """One dashboard facade over the existing prediction intelligence services."""

    def __init__(
        self,
        source: Any,
        *,
        store_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._store_path = store_path
        self._store: DashboardStore | None = None
        self._store_error = ""
        if store_path is not None:
            try:
                self._store = DashboardStore(store_path)
            except DashboardStoreError as exc:
                self._store_error = str(exc)

    # -- lifecycle and contracts -------------------------------------------

    def close(self) -> None:
        store = self._store
        self._store = None
        if store is not None:
            store.close()

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "implemented": True,
            "initialised": self._store is not None,
            "contract_version": DASHBOARD_CONTRACT_VERSION,
            "read_only": True,
            "store": (
                self._store.health()
                if self._store is not None
                else {
                    "ok": False,
                    "reason": self._store_error or "dashboard preferences are not configured",
                    "financial_tables": [],
                }
            ),
            "authorities": authority_contract(),
            "surfaces": [
                "status",
                "contract",
                "navigation",
                "overview",
                "instrument",
                "forecasts",
                "compare",
                "horizons",
                "lifecycle",
                "performance",
                "regimes",
                "decisions",
                "explain",
                "attribution",
                "evidence",
                "research",
                "portfolio",
                "policies",
                "replay",
                "filters",
            ],
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "name": "Prediction Intelligence Dashboard and Decision Explainability",
            "contract_version": DASHBOARD_CONTRACT_VERSION,
            "read_only": True,
            "statuses": [item.value for item in DataStatus],
            "horizons": list(HORIZONS),
            "horizon_families": dict(HORIZON_FAMILIES),
            "forecast_engines": list(FORECAST_ENGINES),
            "decision_actions": list(DECISION_ACTIONS),
            "authorities": authority_contract(),
            "separation": {
                "forecast_accuracy": "statistical evaluation only",
                "trading_profitability": "not inferred from forecast accuracy",
                "execution_quality": EXECUTION_AUTHORITY,
                "risk": RISK_AUTHORITY,
                "confidence": "calibrated probability only when the model reports calibration",
            },
        }

    # -- shared source adapters --------------------------------------------

    def _safe(
        self,
        label: str,
        factory: Callable[[], Any],
        default: Any,
        sources: dict[str, str],
        errors: dict[str, str],
    ) -> Any:
        try:
            value = factory()
        except Exception as exc:  # noqa: BLE001 - dashboard degrades, never crashes
            sources[label] = DataStatus.UNAVAILABLE.value
            errors[label] = f"{type(exc).__name__}: {_text(exc, 300)}"
            return default
        sources[label] = DataStatus.AVAILABLE.value
        return value

    def _forecast_rows(
        self,
        sources: dict[str, str],
        errors: dict[str, str],
        *,
        limit: int = 200,
        instrument_id: str | None = None,
        horizon: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for engine in (*FORECAST_ENGINES, ""):
            label = f"forecast_list:{engine or 'default'}"
            body = self._safe(
                label,
                partial(
                    self._source.forecast_list,
                    instrument_id=instrument_id or None,
                    horizon=horizon or None,
                    limit=limit,
                    engine=engine,
                ),
                {},
                sources,
                errors,
            )
            body = _dict(body)
            engine_rows = _items(_first(body.get("forecasts"), body.get("items"), body))
            for row in engine_rows:
                forecast = _dict(row.get("forecast"))
                candidate = {**row, **forecast} if forecast else row
                identity = _text(
                    _first(
                        candidate.get("forecast_id"),
                        candidate.get("prediction_id"),
                        candidate.get("id"),
                    ),
                    120,
                )
                key = identity or hashlib.sha1(
                    json.dumps(candidate, sort_keys=True, default=str).encode()
                ).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                candidate.setdefault("engine", engine or "default")
                rows.append(candidate)
        return rows

    def _model_rows(
        self,
        sources: dict[str, str],
        errors: dict[str, str],
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for engine in FORECAST_ENGINES:
            label = f"forecast_models:{engine}"
            body = self._safe(
                label,
                partial(
                    self._source.forecast_models,
                    instrument_id=instrument_id or None,
                    horizon=horizon or None,
                    engine=engine,
                ),
                {},
                sources,
                errors,
            )
            for row in _items(_first(_dict(body).get("models"), _dict(body).get("items"))):
                model_id = _text(_first(row.get("model_id"), row.get("name")), 120)
                version = _text(
                    _first(row.get("model_version"), row.get("version")), 80
                )
                key = f"{engine}|{model_id}|{version}"
                if key in seen:
                    continue
                seen.add(key)
                rows.append({**row, "engine": _first(row.get("engine"), engine)})
        return rows

    def _forecast_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        direction, direction_source = _direction(row)
        raw_horizon = _text(
            _first(row.get("horizon"), row.get("forecast_horizon")), 20
        )
        horizon = HORIZON_ALIASES.get(raw_horizon, raw_horizon)
        reference = _number(
            _first(
                row.get("reference_price"),
                row.get("price_at_forecast"),
                row.get("last_price"),
            )
        )
        predicted = _number(
            _first(row.get("predicted_price"), row.get("target_price"), row.get("price"))
        )
        expected = _number(_first(row.get("expected_return"), row.get("predicted_return")))
        if expected is None and reference not in (None, 0) and predicted is not None:
            expected = (predicted - reference) / reference
        low = _number(
            _first(
                row.get("interval_low"),
                row.get("lower_price"),
                row.get("prediction_interval_low"),
            )
        )
        high = _number(
            _first(
                row.get("interval_high"),
                row.get("upper_price"),
                row.get("prediction_interval_high"),
            )
        )
        probability_status = _text(
            _first(
                row.get("probability_status"),
                row.get("calibration_status"),
                row.get("confidence_kind"),
            ),
            40,
        ).lower()
        probabilities = {
            "up": _number(row.get("p_up")),
            "down": _number(row.get("p_down")),
            "neutral": _number(row.get("p_neutral")),
        }
        probability = _number(
            _first(
                row.get("directional_probability"),
                row.get("calibrated_probability"),
                row.get("probability"),
            )
        )
        if probability is None and probability_status in {
            "calibrated",
            "calibrated_probability",
        } and direction in probabilities:
            probability = probabilities[direction]
        if probability is not None and probability_status not in {
            "calibrated",
            "calibrated_probability",
        }:
            probability = None
            probability_status = probability_status or "uncalibrated_not_displayed"
        if probability is None and probability_status != "calibrated":
            probability_status = "unavailable"
        calibration_status = _text(row.get("calibration_status"), 40).lower()
        calibrated = probability_status in {"calibrated", "calibrated_probability"}
        if calibration_status == "uncalibrated":
            calibrated = False
        if not calibrated:
            probabilities = {"up": None, "down": None, "neutral": None}
            if probability_status == "calibrated" and calibration_status == "uncalibrated":
                probability_status = "uncalibrated_not_displayed"
                probability = None
        uncertainty = _dict(row.get("uncertainty"))
        if not uncertainty:
            uncertainty = {
                "class": _first(
                    row.get("forecast_uncertainty"),
                    row.get("uncertainty_class"),
                ),
                "width": _number(_first(row.get("uncertainty_width"), row.get("width"))),
                "method": _first(row.get("uncertainty_method"), row.get("method")),
            }
        validity = _text(
            _first(row.get("validity"), row.get("forecast_validity"), row.get("status")),
            40,
        ).lower()
        freshness = _freshness(
            _first(
                row.get("freshness"),
                row.get("data_freshness"),
                row.get("data_freshness_status"),
            )
        )
        if not freshness["as_of"]:
            freshness["as_of"] = _iso(
                _first(row.get("underlying_data_timestamp"), row.get("reference_at"))
            )
        if not validity:
            validity = (
                "stale"
                if freshness["state"] in {"stale", "delayed"}
                else "unconfirmed"
            )
        return {
            "forecast_id": _text(_first(row.get("forecast_id"), row.get("id")), 120),
            "instrument_id": _text(row.get("instrument_id"), 80).upper(),
            "exchange": _text(row.get("exchange"), 16).upper(),
            "model_id": _text(_first(row.get("model_id"), row.get("model")), 120),
            "model_version": _text(
                _first(row.get("model_version"), row.get("version")), 80
            ),
            "engine": _text(row.get("engine"), 40),
            "sector": _text(_first(row.get("sector"), row.get("industry")), 80),
            "regime": _text(_first(row.get("regime"), row.get("market_regime")), 60),
            "horizon": horizon,
            "horizon_family": HORIZON_FAMILIES.get(horizon, "other"),
            "reference_price": reference,
            "predicted_price": predicted,
            "expected_return": expected,
            "direction": direction,
            "direction_source": direction_source,
            "interval_low": low,
            "interval_high": high,
            "probability": probability,
            "probability_status": probability_status or "unavailable",
            "probabilities": probabilities,
            "uncertainty": uncertainty,
            "data_quality": _text(row.get("data_quality"), 40),
            "ensemble_disagreement": _number(row.get("ensemble_disagreement")),
            "data_freshness": freshness,
            "generated_at": _iso(
                _first(
                    row.get("generated_at"),
                    row.get("forecast_generation_timestamp"),
                    row.get("created_at"),
                    row.get("as_of"),
                )
            ),
            "target_at": _iso(
                _first(
                    row.get("target_at"),
                    row.get("forecast_target_timestamp"),
                    row.get("target_timestamp"),
                    row.get("valid_until"),
                )
            ),
            "reference_at": _iso(
                _first(
                    row.get("reference_at"),
                    row.get("reference_price_timestamp"),
                    row.get("data_as_of"),
                    row.get("underlying_data_timestamp"),
                    row.get("observed_at"),
                )
            ),
            "validity": validity,
            "validity_source": (
                "recorded"
                if _first(row.get("validity"), row.get("forecast_validity")) is not None
                else "derived_status"
            ),
            "confirmation_stage": _text(
                _first(row.get("confirmation_stage"), row.get("lifecycle_stage")), 40
            ),
            "dataset_version": _text(row.get("dataset_version"), 80),
            "feature_set_version": _text(row.get("feature_set_version"), 80),
            "authorizes_action": False,
            "risk_authority": RISK_AUTHORITY,
            "execution_authority": EXECUTION_AUTHORITY,
        }

    @staticmethod
    def _matches(value: object, wanted: str) -> bool:
        return not wanted or _text(value, 80).lower() == wanted.lower()

    @staticmethod
    def _decision_history_rows(body: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Flatten PI-17 history items whose decision lives under ``decision``."""
        rows: list[dict[str, Any]] = []
        for item in _items(_first(body.get("items"), body.get("decisions"))):
            nested = _dict(item.get("decision"))
            row = {**item, **nested} if nested else dict(item)
            row.setdefault("cycle_status", item.get("cycle_status"))
            row["status"] = _text(
                _first(item.get("cycle_status"), row.get("status")), 40
            ).lower()
            row.setdefault("assigned_agents", item.get("agents_assigned"))
            rows.append(row)
        return rows

    def _filter_forecasts(
        self,
        rows: Sequence[Mapping[str, Any]],
        filters: DashboardFilters,
        *,
        paginate: bool = True,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            if filters.instrument_id and not self._matches(
                row.get("instrument_id"), filters.instrument_id
            ):
                continue
            if filters.exchange and not self._matches(row.get("exchange"), filters.exchange):
                continue
            if filters.sector and not self._matches(row.get("sector"), filters.sector):
                continue
            if filters.regime and not self._matches(row.get("regime"), filters.regime):
                continue
            if filters.horizon and not self._matches(row.get("horizon"), filters.horizon):
                continue
            if filters.model_id and filters.model_id.lower() not in _text(
                _first(row.get("model_id"), row.get("engine")), 160
            ).lower():
                continue
            if filters.direction and not self._matches(row.get("direction"), filters.direction):
                continue
            if filters.validity and not self._matches(row.get("validity"), filters.validity):
                continue
            result.append(dict(row))
        if not paginate:
            return result
        start, end = filters.offset, filters.offset + filters.limit
        return result[start:end]

    # -- overview ----------------------------------------------------------

    def overview(
        self, filters: DashboardFilters | Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        selected = (
            filters if isinstance(filters, DashboardFilters)
            else DashboardFilters.from_mapping(filters)
        )
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        market = _dict(
            self._safe(
                "market_screener",
                lambda: self._source.market_screener(limit=25, available_only=False),
                {},
                sources,
                errors,
            )
        )
        raw_forecasts = self._forecast_rows(
            sources,
            errors,
            limit=200,
            instrument_id=selected.instrument_id or None,
            horizon=selected.horizon or None,
        )
        forecasts = self._filter_forecasts(
            [self._forecast_view(row) for row in raw_forecasts],
            selected,
            paginate=False,
        )
        models = self._model_rows(
            sources,
            errors,
            instrument_id=selected.instrument_id or None,
            horizon=selected.horizon or None,
        )
        cycles = _items(
            self._safe(
                "forecast_cycles",
                lambda: self._source.forecast_cycles(limit=80),
                [],
                sources,
                errors,
            )
        )
        decision_history = _dict(
            self._safe(
                "multiagent_history",
                lambda: self._source.multiagent_history(
                    instrument_id=selected.instrument_id or "", limit=60
                ),
                {},
                sources,
                errors,
            )
        )
        decisions = self._decision_history_rows(decision_history)
        decision_total = _int(
            _first(decision_history.get("total"), len(decisions)), len(decisions)
        )
        evaluation_history = _dict(
            self._safe(
                "evaluations",
                lambda: self._source.evaluation_list(limit=20),
                {},
                sources,
                errors,
            )
        )
        evaluations = _items(
            _first(evaluation_history.get("items"), evaluation_history.get("evaluations"))
        )
        adaptive = _dict(
            self._safe(
                "adaptive", lambda: self._source.adaptive_dashboard(), {}, sources, errors
            )
        )
        research = _dict(
            self._safe(
                "research",
                lambda: self._source.research_history(limit=20),
                {},
                sources,
                errors,
            )
        )
        research_runs = _items(research.get("runs"))
        if selected.research_status:
            research_runs = [
                row
                for row in research_runs
                if _text(row.get("status"), 40).lower()
                == selected.research_status.lower()
            ]
        portfolios = _dict(
            self._safe(
                "portfolios",
                lambda: self._source.portfolios_list(limit=20),
                {},
                sources,
                errors,
            )
        )
        portfolio_rows = _items(_first(portfolios.get("portfolios"), portfolios.get("items")))
        selected_portfolio = _text(
            _first(
                portfolio_rows[0].get("portfolio_id") if portfolio_rows else "",
                portfolio_rows[0].get("id") if portfolio_rows else "",
            ),
            80,
        )
        optimization = (
            _dict(
                self._safe(
                    "optimization",
                    lambda: self._source.optimization_latest(selected_portfolio),
                    {},
                    sources,
                    errors,
                )
            )
            if selected_portfolio
            else {}
        )
        policies = _dict(
            self._safe(
                "training_policies",
                lambda: self._source.training_policies(limit=20),
                {},
                sources,
                errors,
            )
        )

        active = [
            row
            for row in forecasts
            if row["validity"] in _ACTIVE_VALIDITY and row["validity"] != "expired"
        ]
        disagreement_groups: dict[str, list[dict[str, Any]]] = {}
        for row in forecasts:
            if row["reference_at"] and row["target_at"]:
                key = "|".join(
                    [row["instrument_id"], row["horizon"], row["reference_at"], row["target_at"]]
                )
                disagreement_groups.setdefault(key, []).append(row)
        disagreement_count = sum(
            _direction_disagreement(group) for group in disagreement_groups.values()
        )
        counts = {
            "active_forecasts": len(active),
            "eligible_instruments": len(
                {row["instrument_id"] for row in forecasts if row["instrument_id"]}
            ),
            "upward": sum(row["direction"] == "up" for row in forecasts),
            "downward": sum(row["direction"] == "down" for row in forecasts),
            "neutral": sum(row["direction"] == "neutral" for row in forecasts),
            "uncertain": sum(row["direction"] == "uncertain" for row in forecasts),
            "awaiting_confirmation": sum(
                row["confirmation_stage"] in {"", "initial", "t-20"}
                for row in active
            ),
            "expired": sum(row["validity"] == "expired" for row in forecasts),
            "invalid_or_stale": sum(
                row["validity"] in {"invalid", "stale", "unavailable", "failed"}
                for row in forecasts
            ),
            "model_disagreement": disagreement_count,
            "model_disagreement_rule": (
                "compatible forecasts disagree when their recorded directions differ; "
                "uncertain and invalid directions do not create a disagreement"
            ),
        }
        action_counts = dict.fromkeys(DECISION_ACTIONS, 0)
        evidence_needed = 0
        for decision in decisions:
            action = _text(decision.get("action"), 20).lower()
            if action in action_counts:
                action_counts[action] += 1
            if decision.get("degraded") or _text(decision.get("evidence_strength"), 40).lower() in {
                "insufficient",
                "weak",
                "missing",
            }:
                evidence_needed += 1
        model_view = [
            {
                "model_id": _text(_first(row.get("model_id"), row.get("name")), 120),
                "model_version": _text(
                    _first(row.get("model_version"), row.get("version")), 80
                ),
                "engine": _text(row.get("engine"), 40),
                "state": _text(
                    _first(row.get("state"), row.get("status"), row.get("health")), 40
                ).lower(),
                "trained_at": _iso(_first(row.get("trained_at"), row.get("created_at"))),
                "metrics": _dict(row.get("metrics")),
            }
            for row in models
        ]
        latest_regime = _dict(adaptive.get("latest_regime"))
        regime = {
            "primary": _text(
                _first(
                    latest_regime.get("primary_regime"),
                    latest_regime.get("primary"),
                    latest_regime.get("regime"),
                ),
                60,
            ),
            "secondary": _text(latest_regime.get("secondary_regime"), 60),
            "status": _text(latest_regime.get("status"), 40).lower() or "unavailable",
            "confidence": latest_regime.get("confidence"),
            "confidence_kind": _text(latest_regime.get("confidence_kind"), 40),
            "detected_at": _iso(
                _first(latest_regime.get("detected_at"), latest_regime.get("as_of"))
            ),
            "uncertain": _bool(latest_regime.get("uncertain")),
        }
        if not regime["primary"]:
            regime["primary"] = "uncertain"
            regime["status"] = "insufficient_evidence"
        market_view = {
            "summary": _dict(market.get("summary")),
            "session": _dict(market.get("session")),
            "data_state": _text(market.get("data_state"), 80),
            "live": market.get("live") is True,
            "price_basis": _text(market.get("price_basis"), 400),
            "benchmarks": _items(market.get("benchmarks")),
            "top_rows": _items(market.get("rows"))[:12],
            "sector_performance": _items(
                _first(market.get("sectors"), market.get("sector_performance"))
            ),
            "events": _items(market.get("events")),
        }
        has_data = bool(
            forecasts
            or decisions
            or models
            or evaluations
            or research_runs
            or cycles
        )
        if errors and has_data:
            data_status = DataStatus.PARTIAL_DATA
        elif errors and not has_data:
            data_status = DataStatus.FAILED
        elif not has_data:
            data_status = DataStatus.INSUFFICIENT_EVIDENCE
        elif any(row["validity"] == "stale" for row in forecasts):
            data_status = DataStatus.STALE
        else:
            data_status = DataStatus.AVAILABLE
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": data_status.value,
            "filters": selected.to_dict(),
            "market": market_view,
            "regime": regime,
            "predictions": counts,
            "models": {
                "available": model_view,
                "available_count": len(model_view),
                "active_versions": sum(
                    row["state"] in {"active", "ready", "eligible", "available"}
                    for row in model_view
                ),
                "ensemble": _dict(adaptive.get("ensemble")),
                "health": _dict(adaptive.get("model_health")),
                "recent_performance": evaluations,
                "disagreement": counts["model_disagreement"],
            },
            "decisions": {
                **action_counts,
                "requires_more_evidence": evidence_needed,
                "cycles": decision_total,
                "scanned": len(decisions),
                "action_counts_scope": "recent scanned decisions",
                "recorded_cycles": len(cycles),
                "distinction": (
                    "forecasts are predictions; agent decisions are recommendations; "
                    "paper orders are execution records owned by NM-10"
                ),
                "latest": [
                    {
                        "cycle_id": _text(
                            _first(row.get("cycle_id"), row.get("id")), 120
                        ),
                        "instrument_id": _text(row.get("instrument_id"), 80),
                        "action": _text(row.get("action"), 20).lower(),
                        "confidence": row.get("confidence"),
                        "confidence_kind": _text(row.get("confidence_kind"), 40),
                        "decided_at": _iso(
                            _first(row.get("decided_at"), row.get("created_at"))
                        ),
                        "degraded": _bool(row.get("degraded")),
                    }
                    for row in decisions[:10]
                ],
            },
            "research": {
                "runs": len(research_runs),
                "latest": research_runs[:5],
                "status": _text(research.get("status"), 40) or "available",
                "filter_status": selected.research_status,
            },
            "portfolio": {
                "portfolios": portfolio_rows,
                "latest_optimization": _text(
                    _first(optimization.get("run_id"), optimization.get("id")), 120
                ),
                "optimization_experimental": True,
            },
            "policies": {
                "count": len(_items(_first(policies.get("policies"), policies.get("items")))),
                "experimental": True,
                "activation_available": False,
            },
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
            "limitations": [
                "Forecast coverage reflects stored model outputs; "
                "an absent engine is not a prediction.",
                "Uncalibrated confidence is not shown as probability.",
                "Forecast accuracy is not trading profitability.",
                "Free provider data may be delayed; latency is shown with the source record.",
            ],
        }

    # -- instrument analysis ------------------------------------------------

    def instrument(self, instrument_id: str) -> dict[str, Any]:
        canonical = DashboardFilters(instrument_id=instrument_id).instrument_id
        if not canonical:
            raise DashboardValidationError("instrument_id is required")
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        analysis = _dict(
            self._safe(
                "market_instrument",
                lambda: self._source.market_orderblock_instrument(canonical),
                {},
                sources,
                errors,
            )
        )
        instrument = _dict(analysis.get("instrument"))
        bars = _items(_dict(analysis.get("chart")).get("bars"))
        indicators = _dict(analysis.get("indicators"))
        levels = _items(analysis.get("levels"))
        regime = _dict(
            self._safe(
                "instrument_regime",
                lambda: self._source.latest_regime(instrument_id=canonical),
                {},
                sources,
                errors,
            )
        )
        research = _dict(
            self._safe(
                "research_latest",
                lambda: self._source.research_latest(canonical),
                {},
                sources,
                errors,
            )
        )
        events = _items(
            _dict(
                self._safe(
                    "radar_events",
                    lambda: self._source.radar_instrument_events(canonical, limit=50),
                    {},
                    sources,
                    errors,
                )
            ).get("events")
        )
        raw_forecasts = self._forecast_rows(
            sources, errors, limit=120, instrument_id=canonical
        )
        forecasts = sorted(
            (self._forecast_view(row) for row in raw_forecasts),
            key=lambda row: row["generated_at"],
            reverse=True,
        )
        price = _number(
            _first(
                analysis.get("last_price"),
                analysis.get("price"),
                instrument.get("last_price"),
            )
        )
        price_at = _iso(
            _first(analysis.get("as_of"), analysis.get("price_at"), instrument.get("as_of"))
        )
        freshness = _freshness(
            _first(analysis.get("freshness"), analysis.get("data_freshness"))
        )
        chart_rows = []
        for bar in bars[-240:]:
            chart_rows.append(
                {
                    "time": _iso(_first(bar.get("time"), bar.get("timestamp"), bar.get("date"))),
                    "open": _number(bar.get("open")),
                    "high": _number(bar.get("high")),
                    "low": _number(bar.get("low")),
                    "close": _number(bar.get("close")),
                    "volume": _number(bar.get("volume")),
                    "vwap20": _number(bar.get("vwap20")),
                    "partial": _bool(bar.get("partial")),
                }
            )
        level_rows = [
            {
                "label": _text(
                    _first(level.get("label"), level.get("kind"), "level"), 40
                ),
                "value": _number(_first(level.get("value"), level.get("price"))),
                "tone": _text(
                    _first(level.get("tone"), level.get("kind"), "resistance"), 20
                ).lower(),
            }
            for level in levels
        ]
        level_rows = [level for level in level_rows if level["value"] is not None]
        available = bool(analysis or forecasts)
        if errors and available:
            data_status = DataStatus.PARTIAL_DATA
        elif errors and not available:
            data_status = DataStatus.UNAVAILABLE
        elif not available:
            data_status = DataStatus.INSUFFICIENT_EVIDENCE
        elif freshness["state"] in {"stale", "delayed"}:
            data_status = DataStatus.STALE
        else:
            data_status = DataStatus.AVAILABLE
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": data_status.value,
            "instrument": {
                "instrument_id": canonical,
                "symbol": _text(
                    _first(instrument.get("symbol"), canonical.split(":")[-1]),
                    40,
                ),
                "name": _text(
                    _first(instrument.get("name"), instrument.get("company_name")), 160
                ),
                "exchange": _text(
                    _first(instrument.get("exchange"), canonical.split(":")[0]), 16
                ).upper(),
                "sector": _text(
                    _first(instrument.get("sector"), instrument.get("industry")), 80
                ),
                "market_status": _text(
                    _first(
                        instrument.get("market_status"),
                        _dict(analysis.get("session")).get("state"),
                    ),
                    40,
                ),
                "price": price,
                "price_at": price_at,
                "currency": _text(
                    _first(instrument.get("currency"), analysis.get("currency")), 8
                )
                or "INR",
                "data_freshness": freshness,
                "delayed_or_historical": analysis.get("live") is not True,
                "price_basis": _text(
                    _first(analysis.get("price_basis"), analysis.get("data_state")), 300
                ),
            },
            "chart": {
                "rows": chart_rows,
                "interval": _text(_dict(analysis.get("chart")).get("interval"), 20) or "1d",
                "supports_ohlc": bool(chart_rows)
                and all(row["open"] is not None and row["close"] is not None for row in chart_rows),
                "indicators": indicators,
                "levels": level_rows,
                "regime_annotations": _items(
                    _first(analysis.get("regime_annotations"), analysis.get("regimes"))
                ),
                "corporate_events": _items(analysis.get("corporate_events")),
                "news_events": events,
            },
            "regime": {
                "primary": _text(
                    _first(
                        regime.get("primary_regime"),
                        regime.get("primary"),
                        analysis.get("regime"),
                    ),
                    60,
                )
                or "uncertain",
                "secondary": _text(regime.get("secondary_regime"), 60),
                "confidence": regime.get("confidence"),
                "confidence_kind": _text(regime.get("confidence_kind"), 40),
                "detected_at": _iso(
                    _first(regime.get("as_of"), regime.get("detected_at"))
                ),
                "status": _text(regime.get("status"), 40) or "available",
            },
            "research": {
                "run_id": _text(research.get("run_id"), 120),
                "status": _text(research.get("status"), 40) or "unavailable",
                "as_of": _iso(research.get("as_of")),
                "thesis": _dict(research.get("thesis")),
            },
            "forecasts": forecasts[:30],
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    # -- forecast views ----------------------------------------------------

    def forecasts(
        self, filters: DashboardFilters | Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        selected = (
            filters if isinstance(filters, DashboardFilters)
            else DashboardFilters.from_mapping(filters)
        )
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        rows = self._forecast_rows(
            sources,
            errors,
            limit=200,
            instrument_id=selected.instrument_id or None,
            horizon=selected.horizon or None,
        )
        views = [self._forecast_view(row) for row in rows]
        matched = self._filter_forecasts(views, selected, paginate=False)
        filtered = self._filter_forecasts(views, selected)
        total = len(matched)
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.PARTIAL_DATA.value
                if errors and views
                else DataStatus.AVAILABLE.value
                if views
                else DataStatus.INSUFFICIENT_EVIDENCE.value
                if not errors
                else DataStatus.FAILED.value
            ),
            "filters": selected.to_dict(),
            "forecasts": filtered,
            "total": total,
            "has_more": selected.offset + len(filtered) < total,
            "sources": sources,
            "source_errors": errors,
            "probability_rule": (
                "Only calibrated directional probabilities are populated; "
                "uncalibrated confidence is retained as confidence metadata."
            ),
            "authorities": authority_contract(),
        }

    def compare(
        self, filters: DashboardFilters | Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        selected = (
            filters if isinstance(filters, DashboardFilters)
            else DashboardFilters.from_mapping(filters)
        )
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        rows = [
            self._forecast_view(row)
            for row in self._forecast_rows(
                sources,
                errors,
                limit=200,
                instrument_id=selected.instrument_id or None,
                horizon=selected.horizon or None,
            )
        ]
        scoped = self._filter_forecasts(rows, selected, paginate=False)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in scoped:
            if row["reference_at"] and row["target_at"]:
                key = "|".join(
                    [
                        row["instrument_id"],
                        row["horizon"],
                        row["reference_at"],
                        row["target_at"],
                    ]
                )
            else:
                key = "unverifiable|" + row["forecast_id"]
            groups.setdefault(key, []).append(row)
        comparisons = []
        for key, group in groups.items():
            model_keys = {
                item["model_id"] or item["engine"] or item["forecast_id"]
                for item in group
            }
            dataset_versions = {
                item["dataset_version"] for item in group if item["dataset_version"]
            }
            has_scope = not key.startswith("unverifiable|")
            comparable = bool(
                has_scope and len(model_keys) >= 2 and len(dataset_versions) <= 1
            )
            reason = ""
            if not has_scope:
                reason = "requires the same recorded reference timestamp and target"
            elif len(model_keys) < 2:
                reason = "requires at least two distinct models"
            elif len(dataset_versions) > 1:
                reason = "requires one compatible dataset version"
            comparisons.append(
                {
                    "key": key,
                    "instrument_id": group[0]["instrument_id"],
                    "horizon": group[0]["horizon"],
                    "reference_at": group[0]["reference_at"],
                    "target_at": group[0]["target_at"],
                    "comparable": comparable,
                    "reason": reason,
                    "models": group,
                    "model_count": len(model_keys),
                    "directions": sorted({item["direction"] for item in group}),
                    "disagreement": _direction_disagreement(group),
                    "best_model": None,
                    "best_model_scope": "not ranked without a scoped evaluation report",
                }
            )
        comparisons.sort(key=lambda item: (item["instrument_id"], item["horizon"]))
        total = len(comparisons)
        comparisons = comparisons[selected.offset : selected.offset + selected.limit]
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.PARTIAL_DATA.value
                if comparisons and errors
                else DataStatus.AVAILABLE.value
                if comparisons
                else DataStatus.FAILED.value
                if errors
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "filters": selected.to_dict(),
            "comparisons": comparisons,
            "total": total,
            "limit": selected.limit,
            "offset": selected.offset,
            "has_more": selected.offset + len(comparisons) < total,
            "comparison_rule": (
                "Only forecasts from at least two distinct models with the same "
                "instrument, horizon, recorded reference timestamp, target and "
                "compatible dataset are comparable. No universal best model is claimed."
            ),
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    def horizons(self, instrument_id: str) -> dict[str, Any]:
        canonical = DashboardFilters(instrument_id=instrument_id).instrument_id
        if not canonical:
            raise DashboardValidationError("instrument_id is required")
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        outlook = _dict(
            self._safe(
                "outlook_latest",
                lambda: self._source.outlook_latest(canonical),
                {},
                sources,
                errors,
            )
        )
        stored = []
        for result in _items(outlook.get("results")):
            forecast = _dict(result.get("forecast"))
            if not forecast:
                continue
            merged = {**forecast, "engine": "ensemble", **result}
            merged.pop("forecast", None)
            stored.append(self._forecast_view(merged))
        streamed = [
            self._forecast_view(row)
            for row in self._forecast_rows(
                sources, errors, limit=200, instrument_id=canonical
            )
            if HORIZON_ALIASES.get(
                _text(_first(row.get("horizon"), row.get("forecast_horizon")), 20),
                _text(_first(row.get("horizon"), row.get("forecast_horizon")), 20),
            )
            in HORIZONS
        ]
        rows = stored + streamed
        seen: set[str] = set()
        deduped = []
        for row in rows:
            key = row["forecast_id"] or f"{row['horizon']}|{row['model_id']}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        families: dict[str, list[dict[str, Any]]] = {}
        for row in deduped:
            families.setdefault(row["horizon_family"], []).append(row)
        cross = _dict(outlook.get("cross_horizon"))
        family_directions = {
            name: sorted(
                {
                    row["direction"]
                    for row in group
                    if row["direction"] not in {"", "uncertain", "invalid"}
                }
            )
            for name, group in families.items()
        }
        disagreement = _direction_disagreement(deduped)
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "instrument_id": canonical,
            "data_status": (
                DataStatus.PARTIAL_DATA.value
                if errors and deduped
                else DataStatus.AVAILABLE.value
                if deduped
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "families": {
                name: {
                    "horizons": list(
                        dict.fromkeys(row["horizon"] for row in group)
                    ),
                    "forecasts": group,
                    "directions": sorted({row["direction"] for row in group}),
                }
                for name, group in families.items()
            },
            "cross_horizon": {
                **cross,
                "disagreement": bool(cross.get("disagreement")) or disagreement,
                "family_directions": family_directions,
                "note": (
                    "Horizons are shown separately; their prices are not combined "
                    "into one target."
                ),
            },
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    # -- lifecycle ---------------------------------------------------------

    def lifecycle(
        self,
        *,
        cycle_id: str = "",
        instrument_id: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        if cycle_id:
            cycle_ids = [cycle_id]
        else:
            body = self._safe(
                "forecast_cycles",
                lambda: self._source.forecast_cycles(
                    instrument_id=instrument_id or None, limit=max(1, min(limit, 50))
                ),
                [],
                sources,
                errors,
            )
            rows = (
                _items(body)
                if isinstance(body, Sequence) and not isinstance(body, (str, bytes))
                else _items(_first(_dict(body).get("cycles"), _dict(body).get("items")))
            )
            cycle_ids = [
                _text(_first(row.get("cycle_id"), row.get("id")), 120)
                for row in rows
            ][: max(1, min(limit, 30))]
        records = []
        for current in [item for item in cycle_ids if item]:
            detail = _dict(
                self._safe(
                    f"forecast_cycle:{current}",
                    partial(self._source.forecast_cycle, current),
                    {},
                    sources,
                    errors,
                )
            )
            cycle = _dict(_first(detail.get("cycle"), detail))
            checkpoints = _items(
                _first(detail.get("checkpoints"), cycle.get("checkpoints"))
            )
            normalized = []
            for checkpoint in checkpoints:
                status = _text(
                    _first(checkpoint.get("status"), checkpoint.get("state")), 40
                ).lower()
                generated = _iso(
                    _first(
                        checkpoint.get("generated_at"),
                        checkpoint.get("confirmed_at"),
                        checkpoint.get("created_at"),
                    )
                )
                planned = _iso(
                    _first(
                        checkpoint.get("planned_at"),
                        checkpoint.get("scheduled_at"),
                        checkpoint.get("target_at"),
                    )
                )
                normalized.append(
                    {
                        "checkpoint": _text(
                            _first(
                                checkpoint.get("checkpoint"),
                                checkpoint.get("stage"),
                                checkpoint.get("name"),
                            ),
                            40,
                        ),
                        "status": status or "unavailable",
                        "planned_at": planned,
                        "generated_at": generated,
                        "late": _bool(checkpoint.get("late"))
                        or bool(
                            planned
                            and generated
                            and generated > planned
                        ),
                        "reference_price": _number(checkpoint.get("reference_price")),
                        "predicted_price": _number(checkpoint.get("predicted_price")),
                        "predicted_direction": _text(
                            checkpoint.get("predicted_direction"), 30
                        ),
                        "confirmation_result": _text(
                            _first(
                                checkpoint.get("confirmation_result"),
                                checkpoint.get("result"),
                            ),
                            120,
                        ),
                        "data_freshness": _freshness(checkpoint.get("data_freshness")),
                        "forecast_version": _text(
                            _first(
                                checkpoint.get("forecast_version"),
                                checkpoint.get("version"),
                            ),
                            80,
                        ),
                        "forecast_id": _text(checkpoint.get("forecast_id"), 120),
                    }
                )
            cycle_horizon = _text(cycle.get("horizon"), 20)
            cycle_horizon = HORIZON_ALIASES.get(cycle_horizon, cycle_horizon)
            records.append(
                {
                    "cycle_id": _text(
                        _first(cycle.get("cycle_id"), cycle.get("id"), current), 120
                    ),
                    "instrument_id": _text(
                        _first(cycle.get("instrument_id"), instrument_id), 80
                    ),
                    "forecast_id": _text(cycle.get("forecast_id"), 120),
                    "horizon": cycle_horizon,
                    "horizon_family": HORIZON_FAMILIES.get(cycle_horizon, "other"),
                    "target_at": _iso(_first(cycle.get("target_at"), cycle.get("target"))),
                    "created_at": _iso(_first(cycle.get("created_at"), cycle.get("as_of"))),
                    "checkpoints": normalized,
                    "missing_checkpoints": _items(
                        _first(
                            cycle.get("missing_checkpoints"),
                            detail.get("missing_checkpoints"),
                        )
                    ),
                    "checkpoint_schedule": (
                        "intraday T-20/T-5/T-1/T"
                        if cycle_horizon in {"5m", "15m", "20m", "1h"}
                        else "as recorded for this swing/long-term lifecycle"
                    ),
                    "evaluation_schedule": _text(
                        _first(
                            cycle.get("evaluation_schedule"),
                            _dict(cycle.get("profile")).get("evaluation_schedule"),
                        ),
                        200,
                    ),
                    "immutable_version": _bool(cycle.get("immutable_version"))
                    or bool(cycle.get("forecast_version")),
                }
            )
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.AVAILABLE.value
                if records
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "cycles": records,
            "total": len(records),
            "sources": sources,
            "source_errors": errors,
            "note": (
                "Checkpoint labels are the recorded lifecycle stages. A late "
                "confirmation is never relabelled as an earlier checkpoint."
            ),
            "authorities": authority_contract(),
        }

    # -- performance -------------------------------------------------------

    def performance(
        self,
        *,
        evaluation_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        evaluation_start: str = "",
        evaluation_end: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        listing = _dict(
            self._safe(
                "evaluations",
                lambda: self._source.evaluation_list(limit=max(1, min(limit, 50))),
                {},
                sources,
                errors,
            )
        )
        evaluations = _items(_first(listing.get("items"), listing.get("evaluations")))

        def evaluation_instrument(row: Mapping[str, Any]) -> str:
            config = _dict(row.get("config"))
            candidates = _first(
                config.get("instruments"),
                config.get("instrument_ids"),
                config.get("instrument_id"),
                config.get("instrument"),
            )
            nested = (
                _first(*candidates)
                if isinstance(candidates, Sequence)
                and not isinstance(candidates, (str, bytes))
                else candidates
            )
            return _text(
                _first(
                    row.get("instrument_id"),
                    row.get("instrument"),
                    nested,
                ),
                80,
            ).upper()

        def evaluation_horizon(row: Mapping[str, Any]) -> str:
            config = _dict(row.get("config"))
            metrics = _dict(row.get("metrics"))
            horizons = _first(
                metrics.get("horizons"), config.get("horizons"), config.get("horizon")
            )
            nested = (
                _first(*horizons)
                if isinstance(horizons, Sequence)
                and not isinstance(horizons, (str, bytes))
                else horizons
            )
            if isinstance(nested, Mapping):
                nested = _first(nested.get("horizon"), nested.get("name"))
            return _text(
                _first(
                    row.get("horizon"),
                    row.get("forecast_horizon"),
                    nested,
                ),
                20,
            )

        def evaluation_period(row: Mapping[str, Any]) -> tuple[str, str]:
            config = _dict(row.get("config"))
            start = _iso(
                _first(
                    row.get("period_start"),
                    row.get("start"),
                    config.get("start"),
                    _dict(row.get("period")).get("start"),
                    row.get("evaluated_at"),
                    row.get("created_at"),
                )
            )[:10]
            end = _iso(
                _first(
                    row.get("period_end"),
                    row.get("end"),
                    config.get("end"),
                    _dict(row.get("period")).get("end"),
                    row.get("evaluated_at"),
                    row.get("created_at"),
                )
            )[:10]
            return start, end

        def in_period(row: Mapping[str, Any]) -> bool:
            if not (evaluation_start or evaluation_end):
                return True
            start, end = evaluation_period(row)
            if evaluation_start and end and end < evaluation_start:
                return False
            if evaluation_end and start and start > evaluation_end:
                return False
            return bool(start or end)

        if instrument_id:
            evaluations = [
                row
                for row in evaluations
                if evaluation_instrument(row) == instrument_id.upper()
            ]
        if horizon:
            evaluations = [
                row for row in evaluations if evaluation_horizon(row) == horizon
            ]
        evaluations = [row for row in evaluations if in_period(row)]
        selected_id = evaluation_id or _text(
            _first(
                _dict(evaluations[0]).get("evaluation_id") if evaluations else "",
                _dict(evaluations[0]).get("id") if evaluations else "",
            ),
            120,
        )
        detail = (
            _dict(
                self._safe(
                    "evaluation_detail",
                    lambda: self._source.evaluation(selected_id),
                    {},
                    sources,
                    errors,
                )
            )
            if selected_id
            else {}
        )
        period_scope_ok = (
            in_period(detail)
            if detail
            else not (evaluation_start or evaluation_end)
        )
        if detail and instrument_id and evaluation_instrument(detail) != instrument_id.upper():
            detail = {}
        if detail and horizon and evaluation_horizon(detail) != horizon:
            detail = {}
        if detail and not period_scope_ok:
            detail = {}
        accuracy = _dict(
            self._safe(
                "forecast_accuracy",
                lambda: self._source.forecast_accuracy(
                    instrument_id=instrument_id or None, horizon=horizon or None
                ),
                {},
                sources,
                errors,
            )
        )
        metrics = _dict(_first(detail.get("metrics"), accuracy.get("metrics")))
        metric_price = _dict(metrics.get("price"))
        metric_direction = _dict(metrics.get("direction"))
        metric_probabilities = _dict(metrics.get("probabilities"))
        metric_sample = _dict(metrics.get("sample"))
        metric_config = _dict(detail.get("config"))
        sample_size = _first(
            detail.get("sample_size"),
            detail.get("samples"),
            detail.get("record_count"),
            _dict(detail.get("metrics")).get("sample_size"),
            metric_sample.get("samples"),
            accuracy.get("sample_size"),
        )
        sample_value = _int(sample_size) if sample_size is not None else None
        sufficient = metric_sample.get("sufficient")
        valid = (
            period_scope_ok
            and bool(metrics)
            and (sample_value is None or sample_value > 0)
            and sufficient is not False
        )
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.AVAILABLE.value
                if valid
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "evaluations": evaluations,
            "selected_evaluation_id": selected_id,
            "evaluation": {
                "evaluation_id": selected_id,
                "instrument_id": _text(
                    _first(
                        detail.get("instrument_id"),
                        evaluation_instrument(detail),
                        instrument_id,
                    ),
                    80,
                ),
                "horizon": _text(
                    _first(
                        detail.get("horizon"),
                        evaluation_horizon(detail),
                        horizon,
                    ),
                    20,
                ),
                "model_id": _text(detail.get("model_id"), 120),
                "models": _items(_first(detail.get("models"), metric_config.get("models"))),
                "model_version": _text(detail.get("model_version"), 80),
                "dataset_version": _text(
                    _first(
                        detail.get("dataset_version"),
                        _dict(metric_config.get("metadata")).get("dataset_version"),
                    ),
                    80,
                ),
                "evaluated_at": _iso(
                    _first(detail.get("evaluated_at"), detail.get("created_at"))
                ),
                "period_start": _iso(
                    _first(
                        detail.get("period_start"),
                        detail.get("start"),
                        metric_config.get("start"),
                        _dict(detail.get("period")).get("start"),
                    )
                ),
                "period_end": _iso(
                    _first(
                        detail.get("period_end"),
                        detail.get("end"),
                        metric_config.get("end"),
                        _dict(detail.get("period")).get("end"),
                    )
                ),
                "sample_size": sample_value,
                "metrics": metrics,
                "regression": _dict(_first(detail.get("regression"), metric_price)),
                "directional": _dict(
                    _first(detail.get("directional"), metric_direction)
                ),
                "probabilistic": _dict(
                    _first(detail.get("probabilistic"), metric_probabilities)
                ),
                "confusion_matrix": _first(
                    detail.get("confusion_matrix"),
                    _dict(detail.get("directional")).get("confusion_matrix"),
                    metric_direction.get("confusion_matrix"),
                ),
                "by_horizon": _items(
                    _first(detail.get("by_horizon"), metrics.get("horizons"))
                ),
                "by_instrument": _items(detail.get("by_instrument")),
                "by_regime": _items(
                    _first(
                        detail.get("by_regime"),
                        metrics.get("regimes"),
                        metrics.get("by_regime"),
                    )
                ),
                "over_time": _items(
                    _first(detail.get("over_time"), metrics.get("stability"))
                ),
                "ensemble_vs_individual": _dict(
                    _first(
                        detail.get("ensemble_vs_individual"), metrics.get("models")
                    )
                ),
                "leakage_audit": _dict(detail.get("leakage_audit")),
                "sample": metric_sample,
                "profitability": _dict(detail.get("profitability")) or None,
                "profitability_claimed": bool(detail.get("profitability")),
                "statistically_valid": valid,
                "invalid_reason": (
                    ""
                    if valid
                    else "the selected evaluation is outside the requested evaluation "
                    "period, has no non-zero sample or is below the report's minimum "
                    "sample threshold"
                ),
            },
            "forecast_accuracy": accuracy,
            "separation": {
                "forecast_accuracy": "prediction error and direction metrics",
                "trading_profitability": (
                    "only shown if an evaluation report explicitly contains a "
                    "profitability section; never inferred from accuracy"
                ),
            },
            "evaluation_period_filter": {
                "start": evaluation_start,
                "end": evaluation_end,
            },
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    # -- regimes -----------------------------------------------------------

    def regimes(self, *, instrument_id: str = "", limit: int = 100) -> dict[str, Any]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        history = _dict(
            self._safe(
                "feature_regimes",
                lambda: self._source.feature_regimes(
                    instrument_id=instrument_id or None,
                    limit=max(1, min(limit, 500)),
                ),
                {},
                sources,
                errors,
            )
        )
        rows = _items(_first(history.get("items"), history.get("regimes")))
        current = _dict(
            self._safe(
                "latest_regime",
                lambda: self._source.latest_regime(instrument_id=instrument_id or None),
                {},
                sources,
                errors,
            )
        )
        adaptive = _dict(
            self._safe(
                "adaptive", lambda: self._source.adaptive_dashboard(), {}, sources, errors
            )
        )
        ordered = sorted(
            rows,
            key=lambda row: _iso(_first(row.get("as_of"), row.get("created_at"))),
            reverse=True,
        )
        transitions = []
        for index, row in enumerate(ordered[:-1]):
            previous = ordered[index + 1]
            if _text(row.get("primary_regime"), 60) != _text(
                previous.get("primary_regime"), 60
            ):
                current_at = _iso(_first(row.get("as_of"), row.get("created_at")))
                previous_at = _iso(
                    _first(previous.get("as_of"), previous.get("created_at"))
                )
                duration_hours = None
                try:
                    duration_hours = round(
                        (
                            datetime.fromisoformat(current_at)
                            - datetime.fromisoformat(previous_at)
                        ).total_seconds()
                        / 3600.0,
                        4,
                    )
                except (TypeError, ValueError):
                    duration_hours = None
                transitions.append(
                    {
                        "from": _text(previous.get("primary_regime"), 60),
                        "to": _text(row.get("primary_regime"), 60),
                        "at": current_at,
                        "duration_hours": duration_hours,
                        "duration_source": (
                            "derived from consecutive recorded observations"
                            if duration_hours is not None
                            else "not provided by detector"
                        ),
                    }
                )
        if not transitions and instrument_id:
            transitions = _items(adaptive.get("changes"))
        latest_adaptive = _dict(adaptive.get("latest_regime"))
        primary = _text(
            _first(
                current.get("primary_regime"),
                current.get("primary"),
                latest_adaptive.get("primary_regime"),
            ),
            60,
        )
        status = _text(
            _first(current.get("status"), latest_adaptive.get("status")), 40
        ).lower()
        if not primary:
            primary = "uncertain"
            status = "insufficient_evidence"
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.PARTIAL_DATA.value
                if primary != "uncertain" and errors
                else DataStatus.AVAILABLE.value
                if primary != "uncertain"
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "instrument_id": _text(instrument_id, 80).upper(),
            "current": {
                "primary": primary,
                "secondary": _text(
                    _first(
                        current.get("secondary_regime"),
                        latest_adaptive.get("secondary_regime"),
                    ),
                    60,
                ),
                "confidence": _first(
                    current.get("confidence"), latest_adaptive.get("confidence")
                ),
                "confidence_kind": _text(
                    _first(
                        current.get("confidence_kind"),
                        latest_adaptive.get("confidence_kind"),
                    ),
                    40,
                ),
                "detected_at": _iso(
                    _first(current.get("as_of"), latest_adaptive.get("as_of"))
                ),
                "status": status or "available",
                "uncertain": _bool(current.get("uncertain"))
                or primary == "uncertain",
            },
            "transitions": transitions[:50],
            "history": ordered,
            "volatility": _dict(
                _first(current.get("volatility"), latest_adaptive.get("volatility"))
            ),
            "trend_strength": _first(
                current.get("trend_strength"), latest_adaptive.get("trend_strength")
            ),
            "drift": _dict(adaptive.get("drift")),
            "degradation": _dict(adaptive.get("degradation")),
            "suitability": _items(
                _first(adaptive.get("suitability"), adaptive.get("policy_suitability"))
            ),
            "recommendations": _items(adaptive.get("recommendations")),
            "retraining": _dict(adaptive.get("retraining")),
            "insufficient_evidence": primary == "uncertain",
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    # -- multi-agent decisions --------------------------------------------

    def _decision_detail(self, cycle_id: str) -> tuple[dict[str, Any], dict[str, str]]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        detail = _dict(
            self._safe(
                "multiagent_cycle",
                lambda: self._source.multiagent_cycle(cycle_id),
                {},
                sources,
                errors,
            )
        )
        if not detail:
            if errors:
                raise DashboardNotFoundError(
                    f"decision cycle {cycle_id!r} is unavailable: "
                    + "; ".join(errors.values())
                )
            raise DashboardNotFoundError(f"decision cycle {cycle_id!r} does not exist")
        evidence = _dict(
            self._safe(
                "multiagent_evidence",
                lambda: self._source.multiagent_evidence(cycle_id),
                {},
                sources,
                errors,
            )
        )
        runs = _dict(
            self._safe(
                "multiagent_runs",
                lambda: self._source.multiagent_runs(cycle_id),
                {},
                sources,
                errors,
            )
        )
        conflicts = _dict(
            self._safe(
                "multiagent_conflicts",
                lambda: self._source.multiagent_conflicts(cycle_id),
                {},
                sources,
                errors,
            )
        )
        detail["_evidence_body"] = evidence
        detail["_runs_body"] = runs
        detail["_conflicts_body"] = conflicts
        detail["_sources"] = sources
        detail["_errors"] = errors
        return detail, sources

    def decisions(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        agent_id: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        local_filter = bool(status or agent_id)
        rows: list[dict[str, Any]] = []
        listing: dict[str, Any] = {}
        if local_filter:
            # Status and agent filters are presentation filters.  Read the
            # available history pages so a filtered total is not a page count.
            seen_cycles: set[str] = set()
            for page_index in range(20):
                page_offset = page_index * 100
                history_page = _dict(
                    self._safe(
                        "multiagent_history",
                        partial(
                            self._source.multiagent_history,
                            instrument_id=instrument_id or "",
                            limit=100,
                            offset=page_offset,
                        ),
                        {},
                        sources,
                        errors,
                    )
                )
                page_rows = self._decision_history_rows(history_page)
                for row in page_rows:
                    cycle_key = _text(
                        _first(row.get("cycle_id"), row.get("id")), 120
                    )
                    if cycle_key and cycle_key in seen_cycles:
                        continue
                    if cycle_key:
                        seen_cycles.add(cycle_key)
                    rows.append(row)
                if not history_page.get("has_more") or not page_rows:
                    break
        else:
            listing = _dict(
                self._safe(
                    "multiagent_history",
                    lambda: self._source.multiagent_history(
                        instrument_id=instrument_id or "",
                        limit=max(1, min(limit, 100)),
                        offset=max(0, offset),
                    ),
                    {},
                    sources,
                    errors,
                )
            )
            rows = self._decision_history_rows(listing)
        filtered = []
        for row in rows:
            if status and _text(row.get("status"), 40).lower() != status.lower():
                continue
            assigned = _names(
                _first(row.get("agents_assigned"), row.get("assigned_agents"))
            )
            agent_ids = {
                name.lower()
                for name in (
                    assigned
                    + _names(row.get("supporting_agents"))
                    + _names(row.get("contradicting_agents"))
                )
            }
            if agent_id and agent_id.lower() not in agent_ids:
                continue
            filtered.append(
                {
                    "cycle_id": _text(_first(row.get("cycle_id"), row.get("id")), 120),
                    "instrument_id": _text(row.get("instrument_id"), 80),
                    "exchange": _text(row.get("exchange"), 16),
                    "horizon": _text(row.get("horizon"), 20),
                    "action": _text(row.get("action"), 20).lower(),
                    "status": _text(row.get("status"), 40).lower(),
                    "confidence": row.get("confidence"),
                    "confidence_kind": _text(row.get("confidence_kind"), 40),
                    "evidence_strength": _text(row.get("evidence_strength"), 40),
                    "degraded": _bool(row.get("degraded")),
                    "agents_assigned": assigned,
                    "decided_at": _iso(
                        _first(row.get("decided_at"), row.get("created_at"), row.get("as_of"))
                    ),
                    "order_submitted": False,
                    "execution_authority": EXECUTION_AUTHORITY,
                }
            )
        total = len(filtered) if local_filter else _int(
            _first(listing.get("total"), len(filtered)), len(filtered)
        )
        visible_decisions = (
            filtered[max(0, offset) : max(0, offset) + max(1, min(limit, 100))]
            if local_filter
            else filtered
        )
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.PARTIAL_DATA.value
                if visible_decisions and errors
                else DataStatus.AVAILABLE.value
                if visible_decisions
                else DataStatus.FAILED.value
                if errors
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "decisions": visible_decisions,
            "total": total,
            "limit": max(1, min(limit, 100)),
            "offset": max(0, offset),
            "has_more": (
                max(0, offset) + len(visible_decisions) < total
                if local_filter
                else bool(listing.get("has_more"))
            ),
            "agent_filter": agent_id,
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    def decision(self, cycle_id: str) -> dict[str, Any]:
        detail, sources = self._decision_detail(cycle_id)
        cycle = _dict(_first(detail.get("cycle"), detail))
        decision = _dict(_first(detail.get("decision"), cycle.get("decision")))
        responses = _items(_first(detail.get("responses"), cycle.get("responses")))
        evidence_body = _dict(detail.get("_evidence_body"))
        conflicts_body = _dict(detail.get("_conflicts_body"))
        agents = []
        for response in responses:
            agents.append(
                {
                    "agent_id": _text(response.get("agent_id"), 80),
                    "role": _text(response.get("role"), 80),
                    "status": _text(response.get("status"), 40).lower(),
                    "action": _text(response.get("action"), 30).lower(),
                    "findings": _items(response.get("findings")),
                    "supporting": _items(response.get("supporting")),
                    "contradicting": _items(response.get("contradicting")),
                    "uncertainties": _items(response.get("uncertainties")),
                    "missing": _items(response.get("missing")),
                    "confidence": response.get("confidence"),
                    "confidence_kind": _text(response.get("confidence_kind"), 40),
                    "evidence": _items(response.get("evidence")),
                    "source_references": _items(response.get("source_references")),
                    "model_version": _text(response.get("model_version"), 80),
                    "config_version": _text(response.get("config_version"), 80),
                }
            )
        all_evidence = _items(
            _first(
                evidence_body.get("evidence"),
                evidence_body.get("items"),
                decision.get("evidence"),
            )
        )
        interpreted = {"derived", "interpretation", "hypothesis", "unverified", "ai"}
        source_counts: dict[str, int] = {}
        for item in all_evidence:
            source_key = _text(
                _first(item.get("source_reference"), item.get("source"), item.get("evidence_id")),
                300,
            ).lower()
            if source_key:
                source_counts[source_key] = source_counts.get(source_key, 0) + 1
        for item in all_evidence:
            evidence_class = _text(item.get("kind"), 40).lower() or "recorded"
            derived = _bool(item.get("derived")) or evidence_class in interpreted
            source_key = _text(
                _first(item.get("source_reference"), item.get("source"), item.get("evidence_id")),
                300,
            ).lower()
            item.setdefault("derived", derived)
            item.setdefault(
                "independent",
                bool(source_key) and not derived and source_counts.get(source_key, 0) <= 1,
            )
            item.setdefault("evidence_class", evidence_class)
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": DataStatus.AVAILABLE.value,
            "cycle": {
                "cycle_id": _text(_first(cycle.get("cycle_id"), cycle.get("id"), cycle_id), 120),
                "instrument_id": _text(cycle.get("instrument_id"), 80),
                "exchange": _text(cycle.get("exchange"), 16),
                "horizon": _text(cycle.get("horizon"), 20),
                "decided_at": _iso(
                    _first(cycle.get("decided_at"), cycle.get("created_at"), cycle.get("as_of"))
                ),
                "agents_assigned": _names(
                    _first(cycle.get("agents_assigned"), cycle.get("assigned_agents"))
                ),
                "execution_status": _text(cycle.get("execution_status"), 40),
                "execution_outcome": _dict(cycle.get("execution_outcome")),
                "degraded": _bool(cycle.get("degraded")),
            },
            "agents": agents,
            "decision": {
                "action": _text(decision.get("action"), 20).lower(),
                "supporting_agents": _names(decision.get("supporting_agents")),
                "contradicting_agents": _names(decision.get("contradicting_agents")),
                "supporting_evidence": _items(decision.get("supporting_evidence")),
                "contradictory_evidence": _items(decision.get("contradictory_evidence")),
                "evidence_strength": _text(decision.get("evidence_strength"), 40),
                "confidence": decision.get("confidence"),
                "confidence_kind": _text(decision.get("confidence_kind"), 40),
                "confidence_provenance": _text(
                    decision.get("confidence_provenance"), 120
                ),
                "uncertainty": _dict(decision.get("uncertainty")),
                "portfolio_context": _dict(decision.get("portfolio_context")),
                "risk_restrictions": _items(decision.get("risk_restrictions")),
                "unresolved_conflicts": _items(decision.get("unresolved_conflicts")),
                "explanation": _text(decision.get("explanation"), 1200),
                "decided_at": _iso(decision.get("decided_at")),
                "degraded": _bool(decision.get("degraded")),
                "order_submitted": False,
                "risk_authority": RISK_AUTHORITY,
                "execution_authority": EXECUTION_AUTHORITY,
            },
            "evidence": all_evidence,
            "conflicts": _items(
                _first(
                    detail.get("conflicts"),
                    conflicts_body.get("conflicts"),
                    conflicts_body.get("items"),
                )
            ),
            "runs": _items(_first(detail.get("runs"), detail.get("_runs_body"))),
            "sources": sources,
            "source_errors": _dict(detail.get("_errors")),
            "evidence_note": (
                "Repeated agent interpretations of one source are not independent "
                "evidence; the source reference and evidence class are preserved."
            ),
            "authorities": authority_contract(),
        }

    # -- explainability ----------------------------------------------------

    @staticmethod
    def _attribution(explain: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Read the shapes the real PI-06/PI-07/PI-08 explainers return.

        Gradient models return ``local_contributions`` (tree SHAP-style), LSTM
        returns global validation occlusion under ``feature_importance``, and
        neither is fabricated into a local causal attribution.  A mapping-style
        SHAP payload is accepted too.
        """
        raw = next(
            (
                value
                for value in (
                    explain.get("local_contributions"),
                    explain.get("contributions"),
                    explain.get("feature_contributions"),
                    explain.get("features"),
                    explain.get("shap_values"),
                )
                if value not in (None, "", [], {})
            ),
            None,
        )
        rows = _items(raw)
        attribution_kind = "local_contribution"
        if not rows and isinstance(raw, Mapping):
            rows = [
                {"feature": key, "contribution": value}
                for key, value in raw.items()
            ]
        if not rows:
            importance = next(
                (
                    value
                    for value in (
                        explain.get("feature_importance"),
                        explain.get("global_importance"),
                    )
                    if value not in (None, "", [], {})
                ),
                None,
            )
            rows = _items(importance)
            attribution_kind = "global_importance"
        result = []
        for row in rows:
            feature = _text(
                _first(
                    row.get("feature"),
                    row.get("name"),
                    row.get("feature_name"),
                ),
                120,
            )
            contribution = _number(
                _first(
                    row.get("contribution"),
                    row.get("shap_value"),
                    row.get("importance"),
                    row.get("mae_increase"),
                    row.get("value"),
                )
            )
            if feature and contribution is not None:
                result.append(
                    {
                        "feature": feature,
                        "contribution": contribution,
                        "method": _text(
                            _first(
                                row.get("method"),
                                explain.get("method"),
                                explain.get("explanation_method"),
                            ),
                            120,
                        ),
                        "direction": _text(row.get("direction"), 30),
                        "kind": attribution_kind,
                    }
                )
        return result

    def _forecast_explanation(self, forecast_id: str) -> dict[str, Any]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        forecast = _dict(
            self._safe(
                "forecast_get",
                lambda: self._source.forecast_get(forecast_id),
                {},
                sources,
                errors,
            )
        )
        if not forecast and errors:
            raise DashboardNotFoundError(
                f"forecast {forecast_id!r} is unavailable: " + "; ".join(errors.values())
            )
        if not forecast:
            raise DashboardNotFoundError(f"forecast {forecast_id!r} does not exist")
        raw_forecast = _dict(_first(forecast.get("forecast"), forecast))
        view = self._forecast_view(raw_forecast)
        engine = _text(
            _first(raw_forecast.get("engine"), view.get("engine")), 40
        ).lower()
        model_id = _text(view.get("model_id"), 80).lower()
        method = ""
        if (
            engine == "ensemble"
            or forecast_id.startswith(("ens_", "pens_"))
            or "ensemble" in model_id
        ):
            method = "ensemble_explain"
        elif (
            engine == "lstm"
            or forecast_id.startswith("lstm_")
            or "lstm" in model_id
        ):
            method = "lstm_explain"
        elif (
            forecast_id.startswith("gbf_")
            or "xgboost" in model_id
            or "lightgbm" in model_id
        ):
            method = "gradient_explain"
        explained: dict[str, Any] = {}
        if method:
            explained = _dict(
                self._safe(
                    method,
                    lambda: getattr(self._source, method)(forecast_id),
                    {},
                    sources,
                    errors,
                )
            )
        outcome = _dict(
            self._safe(
                "forecast_outcome",
                lambda: self._source.forecast_outcome(forecast_id),
                {},
                sources,
                errors,
            )
        )
        contributions = self._attribution(explained)
        explanation_text = _text(
            _first(explained.get("summary"), explained.get("explanation")), 1200
        )
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.AVAILABLE.value
                if view
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "kind": "forecast",
            "subject_id": forecast_id,
            "what": {
                "instrument_id": view["instrument_id"],
                "model_id": view["model_id"],
                "model_version": view["model_version"],
                "action": view["direction"],
                "horizon": view["horizon"],
                "recommendation_timestamp": view["generated_at"],
                "forecast_only": True,
                "order_submitted": False,
            },
            "why": {
                "prediction": view,
                "explanation": explanation_text,
                "explanation_method": _text(
                    _first(
                        explained.get("method"),
                        explained.get("explanation_method"),
                        method,
                    ),
                    120,
                ),
                "explanation_available": bool(explained),
                "contributions": contributions,
                "attribution_kind": (
                    contributions[0].get("kind", "local_contribution")
                    if contributions
                    else "unavailable"
                ),
                "model_contributions": [
                    {
                        **item,
                        "expected_return": _number(
                            _first(
                                item.get("expected_return"),
                                item.get("predicted_return"),
                            )
                        ),
                    }
                    for item in _items(explained.get("constituents"))
                ],
                "constituent_weights": _dict(explained.get("weights")),
                "model_disagreement": _dict(explained.get("disagreement")),
                "feature_set_version": _text(
                    _first(
                        explained.get("feature_set_version"),
                        view["feature_set_version"],
                    ),
                    80,
                ),
                "model_version": _text(
                    _first(explained.get("model_version"), view["model_version"]), 80
                ),
                "explanation_timestamp": _iso(
                    _first(
                        explained.get("as_of"),
                        explained.get("generated_at"),
                        explained.get("created_at"),
                    )
                ),
            },
            "supporting": _items(
                _first(
                    explained.get("supporting"),
                    _dict(forecast.get("metadata")).get("supporting"),
                )
            ),
            "contradicted_by": {
                "model_disagreement": _dict(view["uncertainty"]).get("model_disagreement"),
                "uncertainty": view["uncertainty"],
                "interval": [view["interval_low"], view["interval_high"]],
                "conflicting_forecasts": _items(explained.get("contradicting")),
            },
            "limitations": [
                item
                for item in [
                    (
                        "The model does not report a calibrated directional probability."
                        if view["probability"] is None
                        else ""
                    ),
                    (
                        "No model explanation is available for this forecast."
                        if not explained
                        else ""
                    ),
                    (
                        "Feature attribution is model-specific evidence, not causal proof."
                        if contributions
                        else ""
                    ),
                    (
                        "The displayed attribution is global feature importance from "
                        "validation, not a per-forecast local contribution."
                        if contributions
                        and contributions[0].get("kind") == "global_importance"
                        else ""
                    ),
                ]
                if item
            ],
            "aftermath": {
                "available": bool(outcome),
                "forecast_outcome": outcome,
                "actual_direction": _text(outcome.get("actual_direction"), 30),
                "prediction_correct": outcome.get("direction_correct"),
                "note": "Outcome evidence is separate from decision and execution quality.",
            },
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    def explain(self, subject_id: str, *, kind: str = "") -> dict[str, Any]:
        subject = _text(subject_id, 160)
        if not subject:
            raise DashboardValidationError("subject_id is required")
        resolved = kind.lower()
        if not resolved:
            if subject.startswith(("pfc_", "gbf_", "lstm_", "ens_", "fc_")):
                resolved = "forecast"
            elif subject.startswith(("prr_", "run_")):
                resolved = "research"
            else:
                resolved = "decision"
        if resolved not in {"forecast", "research", "decision"}:
            raise DashboardValidationError(
                "kind must be forecast, research or decision"
            )
        if resolved == "forecast":
            return self._forecast_explanation(subject)
        if resolved == "research":
            research = self.research(run_id=subject)
            detail = _dict(research.get("detail"))
            thesis = _dict(detail.get("thesis"))
            return {
                "ok": True,
                "phase": DASHBOARD_PHASE,
                "as_of": self._clock().isoformat(),
                "data_status": research.get("data_status"),
                "kind": "research",
                "subject_id": subject,
                "what": {
                    "instrument_id": _text(detail.get("instrument_id"), 80),
                    "action": "research recommendation",
                    "run_id": subject,
                    "as_of": _iso(detail.get("as_of")),
                    "not_a_trade": True,
                },
                "why": thesis,
                "supporting": _items(thesis.get("bullish")),
                "contradicted_by": {
                    "bearish_factors": _items(thesis.get("bearish")),
                    "uncertainty": _dict(research.get("uncertainty")),
                },
                "limitations": _names(research.get("limitations")),
                "aftermath": {},
                "sources": research.get("sources", {}),
                "authorities": authority_contract(),
            }
        decision = self.decision(subject)
        record = decision["decision"]
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": decision["data_status"],
            "kind": "decision",
            "subject_id": subject,
            "what": {
                "instrument_id": decision["cycle"]["instrument_id"],
                "exchange": decision["cycle"]["exchange"],
                "action": record["action"],
                "horizon": decision["cycle"]["horizon"],
                "recommendation_timestamp": decision["cycle"]["decided_at"],
                "paper_order_submitted": False,
            },
            "why": {
                "recorded_explanation": record["explanation"],
                "supporting_agents": record["supporting_agents"],
                "contradicting_agents": record["contradicting_agents"],
                "evidence_strength": record["evidence_strength"],
                "confidence": record["confidence"],
                "confidence_kind": record["confidence_kind"],
                "confidence_provenance": record["confidence_provenance"],
                "agent_findings": decision["agents"],
                "research": _dict(record["portfolio_context"]).get("research"),
            },
            "supporting": record["supporting_evidence"],
            "contradicted_by": {
                "evidence": record["contradictory_evidence"],
                "conflicts": decision["conflicts"],
                "uncertainty": record["uncertainty"],
                "risk_restrictions": record["risk_restrictions"],
                "market_regime_disagreement": record["uncertainty"].get(
                    "regime_disagreement"
                ),
                "missing_data": record["uncertainty"].get("missing"),
            },
            "limitations": record["uncertainty"].get("limitations") or [],
            "aftermath": {
                "available": bool(decision["cycle"].get("execution_outcome")),
                "execution_status": decision["cycle"].get("execution_status"),
                "execution_outcome": decision["cycle"].get("execution_outcome"),
                "reason": (
                    "No execution outcome was recorded; NM-10 remains the execution authority."
                    if not decision["cycle"].get("execution_outcome")
                    else (
                        "Recorded execution outcome; prediction accuracy and decision "
                        "quality remain separate."
                    )
                ),
            },
            "evidence": decision["evidence"],
            "sources": decision["sources"],
            "authorities": authority_contract(),
        }

    def attribution(self, forecast_id: str) -> dict[str, Any]:
        explanation = self._forecast_explanation(forecast_id)
        why = _dict(explanation.get("why"))
        contributions = _items(why.get("contributions"))
        model_contributions = _items(why.get("model_contributions"))
        attribution_kind = why.get("attribution_kind", "unavailable")
        if not contributions and model_contributions:
            attribution_kind = "ensemble_model_contributions"
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.AVAILABLE.value
                if contributions or model_contributions
                else DataStatus.UNAVAILABLE.value
            ),
            "forecast_id": forecast_id,
            "method": why.get("explanation_method", ""),
            "attribution_kind": attribution_kind,
            "model_version": why.get("model_version", ""),
            "feature_set_version": why.get("feature_set_version", ""),
            "explanation_timestamp": why.get("explanation_timestamp", ""),
            "contributions": contributions,
            "model_contributions": model_contributions,
            "constituent_weights": why.get("constituent_weights", {}),
            "model_disagreement": why.get("model_disagreement", {}),
            "causal_proof": False,
            "unavailable_reason": (
                ""
                if contributions or model_contributions
                else "This forecast/model did not return computed feature contributions."
            ),
            "authorities": authority_contract(),
        }

    # -- evidence and research --------------------------------------------

    def evidence(self, subject_id: str, *, kind: str = "") -> dict[str, Any]:
        subject = _text(subject_id, 160)
        if not subject:
            raise DashboardValidationError("subject_id is required")
        resolved = kind.lower() or (
            "research" if subject.startswith(("prr_", "run_")) else "decision"
        )
        if resolved not in {"decision", "research"}:
            raise DashboardValidationError("kind must be decision or research")
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        if resolved == "research":
            body = _dict(
                self._safe(
                    "research_evidence",
                    lambda: self._source.research_evidence(subject),
                    {},
                    sources,
                    errors,
                )
            )
            rows = _items(_first(body.get("evidence"), body.get("items")))
        else:
            body = _dict(
                self._safe(
                    "multiagent_evidence",
                    lambda: self._source.multiagent_evidence(subject),
                    {},
                    sources,
                    errors,
                )
            )
            rows = _items(_first(body.get("evidence"), body.get("items")))
        timeline: list[dict[str, Any]] = []
        interpreted = {"derived", "interpretation", "hypothesis", "unverified", "ai"}
        source_counts: dict[str, int] = {}
        for row in rows:
            key = _text(
                _first(row.get("source_reference"), row.get("source"), row.get("evidence_id")),
                300,
            ).lower()
            if key:
                source_counts[key] = source_counts.get(key, 0) + 1
        for row in rows:
            when = _iso(
                _first(
                    row.get("published_at"),
                    row.get("available_at"),
                    row.get("observed_at"),
                    row.get("created_at"),
                    row.get("as_of"),
                )
            )
            evidence_class = _text(
                _first(row.get("evidence_class"), row.get("verification"), row.get("kind")),
                60,
            )
            source_key = _text(
                _first(row.get("source_reference"), row.get("source"), row.get("evidence_id")),
                300,
            ).lower()
            derived = _bool(row.get("derived")) or evidence_class.lower() in interpreted
            timeline.append(
                {
                    "evidence_id": _text(
                        _first(row.get("evidence_id"), row.get("id")), 120
                    ),
                    "claim": _text(
                        _first(row.get("claim"), row.get("statement"), row.get("summary")),
                        800,
                    ),
                    "source": _text(
                        _first(row.get("source"), row.get("source_id")), 160
                    ),
                    "source_reference": _text(row.get("source_reference"), 300),
                    "kind": _text(row.get("kind"), 40),
                    "class": evidence_class,
                    "derived": derived,
                    "independent": (
                        row.get("independent")
                        if row.get("independent") is not None
                        else bool(source_key)
                        and not derived
                        and source_counts.get(source_key, 0) <= 1
                    ),
                    "timestamp": when,
                    "model_version": _text(row.get("model_version"), 80),
                    "raw": row,
                }
            )
        timeline.sort(key=lambda item: str(item["timestamp"]))
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.AVAILABLE.value
                if timeline
                else DataStatus.FAILED.value
                if errors
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "subject_id": subject,
            "kind": resolved,
            "timeline": timeline,
            "total": len(timeline),
            "note": "Evidence is returned as recorded; provenance is never synthesized.",
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    def research(
        self,
        *,
        instrument_id: str = "",
        run_id: str = "",
        research_status: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        history = _dict(
            self._safe(
                "research_history",
                lambda: self._source.research_history(
                    instrument_id=instrument_id or "",
                    limit=max(1, min(limit, 50)),
                ),
                {},
                sources,
                errors,
            )
        )
        runs = _items(_first(history.get("runs"), history.get("items")))
        if research_status:
            runs = [
                row
                for row in runs
                if _text(row.get("status"), 40).lower() == research_status.lower()
            ]
        selected_id = run_id
        if not selected_id and instrument_id:
            latest = _dict(
                self._safe(
                    "research_latest",
                    lambda: self._source.research_latest(instrument_id),
                    {},
                    sources,
                    errors,
                )
            )
            selected_id = _text(latest.get("run_id"), 120)
            if (
                selected_id
                and research_status
                and _text(latest.get("status"), 40).lower()
                != research_status.lower()
            ):
                selected_id = ""
        if not selected_id and runs:
            selected_id = _text(
                _first(runs[0].get("run_id"), runs[0].get("id")), 120
            )
        detail = (
            _dict(
                self._safe(
                    "research_detail",
                    lambda: self._source.research_detail(selected_id),
                    {},
                    sources,
                    errors,
                )
            )
            if selected_id
            else {}
        )
        if run_id and not detail:
            if errors:
                raise DashboardNotFoundError(
                    f"research run {run_id!r} is unavailable: " + "; ".join(errors.values())
                )
            raise DashboardNotFoundError(f"research run {run_id!r} does not exist")
        if (
            detail
            and research_status
            and _text(detail.get("status"), 40).lower() != research_status.lower()
        ):
            detail = {}
            selected_id = ""
        evidence_body = (
            _dict(
                self._safe(
                    "research_evidence",
                    lambda: self._source.research_evidence(selected_id),
                    {},
                    sources,
                    errors,
                )
            )
            if selected_id
            else {}
        )
        evidence = _items(
            _first(evidence_body.get("evidence"), evidence_body.get("items"))
        )
        thesis = _dict(detail.get("thesis"))
        events_section = detail.get("events")
        news_section = detail.get("news")
        events = _items(events_section) or _items(
            _dict(events_section).get("events")
            if isinstance(events_section, Mapping)
            else None
        ) or _items(
            _dict(events_section).get("items")
            if isinstance(events_section, Mapping)
            else None
        )
        news = _items(news_section) or _items(
            _dict(news_section).get("articles")
            if isinstance(news_section, Mapping)
            else None
        ) or _items(
            _dict(news_section).get("items")
            if isinstance(news_section, Mapping)
            else None
        )
        profile = _dict(_first(detail.get("profile"), detail.get("company")))
        positive_factors = _values(
            _first(
                thesis.get("bullish"),
                thesis.get("positive_factors"),
                detail.get("bullish"),
                detail.get("positive_factors"),
            )
        )
        negative_factors = _values(
            _first(
                thesis.get("bearish"),
                thesis.get("negative_factors"),
                detail.get("bearish"),
                detail.get("negative_factors"),
            )
        )
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.AVAILABLE.value
                if detail or runs
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "instrument_id": _text(instrument_id, 80).upper(),
            "runs": runs,
            "selected_run_id": selected_id,
            "status_filter": research_status,
            "detail": detail,
            "profile": profile,
            "company": _dict(_first(detail.get("company"), profile)),
            "financials": _dict(detail.get("financials")),
            "valuation": _dict(detail.get("valuation")),
            "sector": _dict(detail.get("sector")),
            "peers": _values(detail.get("peers")),
            "macro": _values(detail.get("macro")),
            "events": events,
            "news": news,
            "thesis": {
                **thesis,
                "bullish": positive_factors,
                "bearish": negative_factors,
                "positive_factors": positive_factors,
                "negative_factors": negative_factors,
            },
            "evidence": evidence,
            "uncertainty": _dict(
                _first(
                    detail.get("uncertainty"),
                    detail.get("research_uncertainty"),
                    thesis.get("confidence"),
                )
            ),
            "limitations": _values(
                _first(detail.get("limitations"), detail.get("warnings"))
            ),
            "separation": {
                "reported_financial_facts": "as recorded with reporting period and source",
                "ai_interpretation": "thesis and qualitative assessment",
                "estimated_valuation": "only when the research record labels it estimated",
            },
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    # -- portfolio, policies and replay -----------------------------------

    def portfolio(
        self, *, portfolio_id: str = "", limit: int = 20
    ) -> dict[str, Any]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        listing = _dict(
            self._safe(
                "portfolios",
                lambda: self._source.portfolios_list(limit=max(1, min(limit, 50))),
                {},
                sources,
                errors,
            )
        )
        portfolios = _items(_first(listing.get("portfolios"), listing.get("items")))
        selected = portfolio_id or _text(
            _first(
                portfolios[0].get("portfolio_id") if portfolios else "",
                portfolios[0].get("id") if portfolios else "",
            ),
            80,
        )
        detail = (
            _dict(
                self._safe(
                    "portfolio_detail",
                    lambda: self._source.portfolio_detail(selected),
                    {},
                    sources,
                    errors,
                )
            )
            if selected
            else {}
        )
        optimization = (
            _dict(
                self._safe(
                    "optimization_latest",
                    lambda: self._source.optimization_latest(selected),
                    {},
                    sources,
                    errors,
                )
            )
            if selected
            else {}
        )
        record = _dict(detail.get("portfolio"))
        valuation = _dict(detail.get("valuation"))
        cash = _number(detail.get("cash"))
        total = _number(_first(valuation.get("total"), valuation.get("total_value")))
        holdings = _items(detail.get("holdings"))
        recorded_weights = [
            {
                "instrument_id": _text(row.get("instrument_id"), 80),
                "weight": _number(row.get("weight")),
            }
            for row in holdings
        ]
        current_allocation = [row for row in recorded_weights if row["weight"] is not None]
        rebalancing_payload = _first(
            optimization.get("rebalancing"),
            optimization.get("proposals"),
        )
        rebalancing = (
            _dict(rebalancing_payload)
            if isinstance(rebalancing_payload, Mapping)
            else _items(rebalancing_payload)
        )
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.PARTIAL_DATA.value
                if portfolios and errors
                else DataStatus.AVAILABLE.value
                if portfolios
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "portfolios": portfolios,
            "selected_portfolio_id": selected,
            "current_authoritative": {
                "portfolio": record,
                "cash_nc": cash,
                "valuation": valuation,
                "total_value_nc": total,
                "currency": _text(_first(record.get("currency"), valuation.get("currency")), 8)
                or "NC",
                "inr_equivalent": _number(
                    _first(valuation.get("inr_equivalent"), total)
                ),
                "inr_conversion": {
                    "rate": 1.0 if total is not None else None,
                    "derived": total is not None,
                    "note": "NovaMarket accounting convention: 1 NC = 1 INR.",
                },
                "holdings": holdings,
                "current_allocation": current_allocation,
                "allocation_source": (
                    "recorded position weights"
                    if current_allocation
                    else "position weights unavailable"
                ),
                "constraints": _items(detail.get("constraints")),
                "stale_prices": sum(
                    bool(row.get("stale")) for row in holdings
                ),
                "authoritative": True,
            },
            "simulated": None,
            "recommendation": {
                "run_id": _text(_first(optimization.get("run_id"), optimization.get("id")), 120),
                "status": _text(optimization.get("status"), 40),
                "experimental": True,
                "advisory_only": True,
                "allocation": _items(
                    _first(
                        optimization.get("allocation"),
                        optimization.get("recommended_allocation"),
                    )
                ),
                "sector_exposure": _items(
                    _first(
                        optimization.get("sector_exposure"),
                        optimization.get("sectors"),
                    )
                ),
                "diversification": _dict(
                    _first(
                        optimization.get("diversification"),
                        optimization.get("metrics"),
                    )
                ),
                "risk": _dict(optimization.get("risk")),
                "rebalancing": rebalancing,
                "changes_portfolio": False,
            },
            "isolation": {
                "visualization_only": True,
                "can_modify_positions": False,
                "can_write_ledger": False,
                "can_submit_orders": False,
            },
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    def policies(self, *, policy_id: str = "", limit: int = 30) -> dict[str, Any]:
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        policies = _dict(
            self._safe(
                "training_policies",
                lambda: self._source.training_policies(limit=max(1, min(limit, 50))),
                {},
                sources,
                errors,
            )
        )
        runs = _dict(
            self._safe(
                "training_runs",
                lambda: self._source.training_runs(limit=max(1, min(limit, 50))),
                {},
                sources,
                errors,
            )
        )
        rewards = _dict(
            self._safe(
                "reward_evaluations",
                lambda: self._source.reward_evaluations(limit=max(1, min(limit, 50))),
                {},
                sources,
                errors,
            )
        )
        adaptive = _dict(
            self._safe(
                "adaptive", lambda: self._source.adaptive_dashboard(), {}, sources, errors
            )
        )
        degradation = _dict(
            self._safe(
                "adaptive_degradation",
                lambda: self._source.adaptive_degradation(policy_id, limit=limit),
                {},
                sources,
                errors,
            )
        )
        retraining = _dict(
            self._safe(
                "adaptive_retraining",
                lambda: self._source.adaptive_retraining(policy_id, limit=limit),
                {},
                sources,
                errors,
            )
        )
        policy_rows = _items(
            _first(policies.get("policies"), policies.get("items"))
        )
        policy_views = []
        for row in policy_rows:
            training_metrics = _dict(row.get("training_metrics"))
            validation_metrics = _dict(row.get("validation_metrics"))
            test_metrics = _dict(row.get("test_metrics"))
            creation_metadata = _dict(row.get("creation_metadata"))
            policy_views.append(
                {
                    **row,
                    "policy_version": _text(
                        _first(row.get("policy_version"), row.get("artifact_version")), 80
                    ),
                    "training_reward": _first(
                        row.get("training_reward"),
                        training_metrics.get("reward"),
                        training_metrics.get("total_reward"),
                        training_metrics.get("mean_reward"),
                        training_metrics.get("episode_reward"),
                    ),
                    "validation": validation_metrics or row.get("validation"),
                    "validation_metrics": validation_metrics,
                    "out_of_sample_metrics": test_metrics or validation_metrics,
                    "out_of_sample": _first(
                        row.get("out_of_sample"),
                        test_metrics.get("out_of_sample"),
                        bool(test_metrics),
                    ),
                    "regime_suitability": _first(
                        row.get("regime_suitability"),
                        test_metrics.get("regime_suitability"),
                        validation_metrics.get("regime_suitability"),
                        creation_metadata.get("regime_suitability"),
                    ),
                    "drift": _first(
                        row.get("drift"),
                        test_metrics.get("drift"),
                        creation_metadata.get("drift"),
                    ),
                    "experimental": True,
                    "advisory_only": True,
                    "activation_available": False,
                    "training_reward_is_not_profit": True,
                }
            )
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": (
                DataStatus.PARTIAL_DATA.value
                if errors
                and (
                    policy_views
                    or _items(_first(runs.get("runs"), runs.get("items")))
                    or _items(_first(rewards.get("evaluations"), rewards.get("items")))
                )
                else DataStatus.AVAILABLE.value
                if policy_views
                or _items(_first(runs.get("runs"), runs.get("items")))
                or _items(_first(rewards.get("evaluations"), rewards.get("items")))
                else DataStatus.INSUFFICIENT_EVIDENCE.value
            ),
            "policies": policy_views,
            "runs": _items(_first(runs.get("runs"), runs.get("items"))),
            "reward_evaluations": _items(
                _first(rewards.get("evaluations"), rewards.get("items"))
            ),
            "degradation": _items(
                _first(degradation.get("items"), degradation.get("degradation"))
            ),
            "retraining": _items(
                _first(retraining.get("items"), retraining.get("recommendations"))
            ),
            "adaptive": {
                "drift": _dict(adaptive.get("drift")),
                "degradation": _dict(adaptive.get("degradation")),
                "recommendations": _items(adaptive.get("recommendations")),
            },
            "labels": {
                "experimental_rl": True,
                "training_reward": "optimization signal, not portfolio profit",
                "activation_available": False,
            },
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    def replay(self, cycle_id: str) -> dict[str, Any]:
        detail, sources = self._decision_detail(cycle_id)
        cycle = _dict(_first(detail.get("cycle"), detail))
        decision = _dict(_first(detail.get("decision"), cycle.get("decision")))
        as_of = _iso(
            _first(
                decision.get("decided_at"),
                cycle.get("decided_at"),
                cycle.get("as_of"),
                cycle.get("created_at"),
            )
        )
        if not as_of:
            raise DashboardValidationError("the selected cycle has no decision timestamp")
        instrument_id = _text(
            _first(cycle.get("instrument_id"), decision.get("instrument_id")), 80
        )
        errors = _dict(detail.get("_errors"))
        forecasts = []
        future_forecasts = 0
        for row in self._forecast_rows(
            sources, errors, limit=200, instrument_id=instrument_id or None
        ):
            view = self._forecast_view(row)
            if view["generated_at"] and view["generated_at"] <= as_of:
                forecasts.append(view)
            elif view["generated_at"]:
                future_forecasts += 1
        evidence_body = _dict(detail.get("_evidence_body"))
        evidence = []
        future_evidence = 0
        for row in _items(
            _first(evidence_body.get("evidence"), evidence_body.get("items"))
        ):
            when = _iso(
                _first(
                    row.get("published_at"),
                    row.get("available_at"),
                    row.get("observed_at"),
                    row.get("created_at"),
                    row.get("as_of"),
                )
            )
            if when and when > as_of:
                future_evidence += 1
                continue
            evidence.append(row)
        research_history = _dict(
            self._safe(
                "research_history",
                lambda: self._source.research_history(
                    instrument_id=instrument_id, limit=50
                ),
                {},
                sources,
                errors,
            )
        )
        research_runs = _items(
            _first(research_history.get("runs"), research_history.get("items"))
        )

        def research_time(row: Mapping[str, Any]) -> str:
            return _iso(
                _first(
                    row.get("as_of"),
                    row.get("created_at"),
                    row.get("generated_at"),
                    row.get("completed_at"),
                    row.get("updated_at"),
                )
            )

        visible_runs = [
            run
            for run in research_runs
            if not research_time(run) or research_time(run) <= as_of
        ]
        future_research = len(research_runs) - len(visible_runs)
        selected_run = max(
            visible_runs,
            key=lambda run: research_time(run),
            default={},
        )
        research: dict[str, Any] = {}
        if selected_run:
            run_id = _text(selected_run.get("run_id"), 120)
            research = (
                _dict(
                    self._safe(
                        "research_detail_at_cutoff",
                        lambda: self._source.research_detail(run_id),
                        {},
                        sources,
                        errors,
                    )
                )
                if run_id
                else {}
            ) or dict(selected_run)
        if not research:
            latest = _dict(
                self._safe(
                    "research_latest",
                    lambda: self._source.research_latest(instrument_id),
                    {},
                    sources,
                    errors,
                )
            )
            latest_at = research_time(latest)
            if latest and (not latest_at or latest_at <= as_of):
                research = latest
            elif latest and not research_runs:
                future_research += 1
        research_at = research_time(research)
        if research and research_at and research_at > as_of:
            future_research += 1
            research = dict(selected_run) if selected_run else {}
            research_at = research_time(research)
        research_visible = bool(research) and (not research_at or research_at <= as_of)
        regime_history = _dict(
            self._safe(
                "regime_history_at_cutoff",
                lambda: self._source.feature_regimes(
                    instrument_id=instrument_id or None, limit=500
                ),
                {},
                sources,
                errors,
            )
        )
        regime_rows = _items(
            _first(regime_history.get("items"), regime_history.get("regimes"))
        )
        visible_regimes = [
            row
            for row in regime_rows
            if not _iso(_first(row.get("as_of"), row.get("detected_at")))
            or _iso(_first(row.get("as_of"), row.get("detected_at"))) <= as_of
        ]
        future_regimes = len(regime_rows) - len(visible_regimes)
        regime = max(
            visible_regimes,
            key=lambda row: _iso(_first(row.get("as_of"), row.get("detected_at"))),
            default={},
        )
        if not regime:
            latest_regime = _dict(
                self._safe(
                    "latest_regime",
                    lambda: self._source.latest_regime(instrument_id=instrument_id),
                    {},
                    sources,
                    errors,
                )
            )
            regime_at = _iso(
                _first(latest_regime.get("as_of"), latest_regime.get("detected_at"))
            )
            if latest_regime and (not regime_at or regime_at <= as_of):
                regime = latest_regime
            elif latest_regime and not regime_rows:
                future_regimes += 1
        regime_at = _iso(_first(regime.get("as_of"), regime.get("detected_at")))
        market = _dict(
            self._safe(
                "market_replay",
                lambda: self._source.market_tradingwatch_replay(
                    instrument_id, end_date=as_of[:10], interval="1d", limit=120
                ),
                {},
                sources,
                errors,
            )
        )
        bars = _items(_first(_dict(market.get("chart")).get("bars"), market.get("bars")))
        visible_bars = [
            bar
            for bar in bars
            if not _iso(_first(bar.get("time"), bar.get("timestamp"), bar.get("date")))
            or _iso(_first(bar.get("time"), bar.get("timestamp"), bar.get("date"))) <= as_of
        ]
        events = _items(
            _dict(
                self._safe(
                    "radar_events",
                    lambda: self._source.radar_instrument_events(instrument_id, limit=50),
                    {},
                    sources,
                    errors,
                )
            ).get("events")
        )
        visible_events = []
        excluded_events = 0
        for event in events:
            when = _iso(
                _first(
                    event.get("published_at"),
                    event.get("observed_at"),
                    event.get("created_at"),
                )
            )
            if when and when > as_of:
                excluded_events += 1
                continue
            visible_events.append(event)
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "as_of": self._clock().isoformat(),
            "data_status": DataStatus.AVAILABLE.value,
            "cycle_id": _text(_first(cycle.get("cycle_id"), cycle.get("id"), cycle_id), 120),
            "known_at": as_of,
            "instrument_id": instrument_id,
            "recorded_history": {
                "decision": decision,
                "agent_responses": _items(
                    _first(cycle.get("responses"), detail.get("responses"))
                ),
                "evidence": evidence,
                "portfolio_context": _dict(decision.get("portfolio_context")),
                "risk_restrictions": _items(decision.get("risk_restrictions")),
                "execution_status": _text(cycle.get("execution_status"), 40),
                "execution_outcome": _dict(cycle.get("execution_outcome")),
            },
            "reconstructed_at_cutoff": {
                "market_observations": visible_bars,
                "forecasts_available": forecasts,
                "research": research if research_visible else None,
                "research_excluded": future_research > 0,
                "regime": regime if not regime_at or regime_at <= as_of else None,
                "regime_excluded": future_regimes > 0,
                "news_events": visible_events,
                "mode": "reconstructed_read_only",
            },
            "point_in_time": {
                "cutoff": as_of,
                "future_forecasts_excluded": future_forecasts,
                "future_evidence_excluded": future_evidence,
                "future_news_excluded": excluded_events,
                "future_research_excluded": future_research,
                "future_regime_excluded": future_regimes,
                "look_ahead_enforced": True,
            },
            "separation": (
                "recorded operational history is preserved separately from the "
                "reconstructed research view"
            ),
            "sources": sources,
            "source_errors": errors,
            "authorities": authority_contract(),
        }

    # -- filters -----------------------------------------------------------

    def filters(self, *, name: str = "default") -> dict[str, Any]:
        if self._store is None:
            return {
                "ok": True,
                "phase": DASHBOARD_PHASE,
                "data_status": DataStatus.UNAVAILABLE.value,
                "name": name,
                "filters": DashboardFilters().to_dict(),
                "saved": False,
                "reason": self._store_error or "dashboard preferences are not configured",
            }
        try:
            saved = self._store.get_config(name)
            presets = self._store.list_configs()
        except DashboardStoreError as exc:
            return {
                "ok": True,
                "phase": DASHBOARD_PHASE,
                "data_status": DataStatus.FAILED.value,
                "name": name,
                "filters": DashboardFilters().to_dict(),
                "saved": False,
                "reason": str(exc),
            }
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "data_status": DataStatus.AVAILABLE.value,
            "name": name,
            "filters": _dict(saved).get("filters") or DashboardFilters().to_dict(),
            "saved": bool(saved),
            "presets": presets,
            "updated_at": _iso(_dict(saved).get("updated_at")),
            "presentation_only": True,
        }

    def save_filters(
        self, payload: Mapping[str, Any], *, name: str = "default"
    ) -> dict[str, Any]:
        if self._store is None:
            raise DashboardValidationError(
                self._store_error or "dashboard preferences are not configured"
            )
        body = _dict(payload)
        filters = DashboardFilters.from_mapping(_dict(body.get("filters")) or body)
        requested_name = _text(body.get("name"), 80)
        preset_name = name if name != "default" else (requested_name or name)
        saved = self._store.save_config(
            preset_name, filters, updated_at=self._clock()
        )
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "data_status": DataStatus.AVAILABLE.value,
            "preset": saved,
            "presentation_only": True,
            "financial_mutation": False,
        }

    # -- navigation metadata ----------------------------------------------

    def navigation(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": DASHBOARD_PHASE,
            "views": [
                {"id": "overview", "label": "Overview"},
                {"id": "instrument", "label": "Instrument analysis"},
                {"id": "forecasts", "label": "Forecasts"},
                {"id": "compare", "label": "Model comparison"},
                {"id": "horizons", "label": "Multi-horizon"},
                {"id": "lifecycle", "label": "Lifecycle"},
                {"id": "performance", "label": "Performance"},
                {"id": "regimes", "label": "Market regimes"},
                {"id": "decisions", "label": "Agent decisions"},
                {"id": "research", "label": "Financial research"},
                {"id": "portfolio", "label": "Portfolio intelligence"},
                {"id": "policies", "label": "RL & adaptive"},
                {"id": "replay", "label": "Historical replay"},
            ],
            "filters": [
                "instrument_id",
                "exchange",
                "sector",
                "regime",
                "horizon",
                "model_id",
                "direction",
                "validity",
                "evaluation_start",
                "evaluation_end",
                "decision_status",
                "agent_id",
                "portfolio_id",
                "research_status",
            ],
            "authorities": authority_contract(),
        }
