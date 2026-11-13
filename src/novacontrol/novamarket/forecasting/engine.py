"""The NM-06 forecasting engine.

It composes NM-02 bars, NM-04 confirmed events and NM-05 candidate context. It
never places an order, sizes a position or moves NovaCoin. Every forecast is
stored with its data cutoff, feature digest, model version and interval; every
outcome is recorded later without changing the forecast itself.
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Any, Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

from novacontrol.novamarket.contracts import (
    ForecastDataError,
    ForecastNotFoundError,
    ForecastStateError,
    ForecastValidationError,
)
from novacontrol.novamarket.discovery.sessions import SESSION_TABLE
from novacontrol.novamarket.forecasting import estimators
from novacontrol.novamarket.forecasting.calibration import (
    CalibrationSample,
    calibrate_probability,
)
from novacontrol.novamarket.forecasting.features import (
    PriceBar,
    classify_regime,
    compute_features,
)
from novacontrol.novamarket.forecasting.metrics import direction_of, predicted_direction
from novacontrol.novamarket.forecasting.models import (
    FEATURE_SET_VERSION,
    HORIZON_SESSIONS,
    METHODOLOGY_VERSION,
    MODEL_CATALOG_VERSION,
    ForecastOutcome,
    ForecastPolicy,
    ForecastRecord,
    ForecastStatus,
    Horizon,
    ModelRecord,
    ModelState,
    UncertaintyLevel,
    digest_payload,
    forecast_id_for,
    utcnow,
)
from novacontrol.novamarket.forecasting.store import ForecastStore
from novacontrol.novamarket.forecasting.validation import (
    best_model,
    evaluate_models,
)

_LOGGER = logging.getLogger("novacontrol.novamarket.forecasting")

Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]

CONFIRMED_EVENT_STATES = frozenset(
    {"officially_confirmed", "multi_source_corroborated"}
)


def _horizon_label(horizon: Horizon | str | None) -> str | None:
    """Validate a horizon once; every read path uses this."""
    if horizon is None:
        return None
    try:
        return Horizon(horizon).value
    except ValueError as exc:
        known = ", ".join(item.value for item in Horizon)
        raise ForecastValidationError(
            f"unknown horizon {horizon!r}; known: {known}"
        ) from exc


def _required_horizon(horizon: Horizon | str | None) -> str:
    label = _horizon_label(horizon)
    if label is None:
        raise ForecastValidationError("a horizon is required")
    return label


class ForecastEngine:
    """Probabilistic forecasts, their evaluation and their models' health."""

    def __init__(
        self,
        store: ForecastStore,
        *,
        market_data: Any | None = None,
        radar: Any | None = None,
        discovery: Any | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
        policy: ForecastPolicy | None = None,
    ) -> None:
        self._store = store
        self._market_data = market_data
        self._radar = radar
        self._discovery = discovery
        self._publisher = publisher
        self._clock = clock or utcnow
        self.policy = policy or ForecastPolicy()
        self._last_error = ""

    # -- wiring ---------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def set_market_data(self, market_data: Any | None) -> None:
        self._market_data = market_data

    def set_radar(self, radar: Any | None) -> None:
        self._radar = radar

    def set_discovery(self, discovery: Any | None) -> None:
        self._discovery = discovery

    def set_publisher(self, publisher: Publisher | None) -> None:
        self._publisher = publisher

    async def _publish(self, name: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(name, payload)
        except Exception as exc:  # noqa: BLE001 - a stored forecast stands
            self._last_error = f"{name}: {type(exc).__name__}: {exc}"
            _LOGGER.warning(
                "novamarket.forecast.event_not_published name=%s error=%s",
                name,
                type(exc).__name__,
            )

    # -- data access ----------------------------------------------------------

    def _instrument(self, instrument_id: str) -> dict[str, Any]:
        if self._market_data is None:
            raise ForecastDataError("market data is not initialised")
        try:
            body: dict[str, Any] = self._market_data.get_instrument(
                instrument_id
            ).to_dict()
            return body
        except Exception as exc:  # noqa: BLE001 - one typed error for the caller
            raise ForecastNotFoundError(
                f"unknown instrument {instrument_id!r}"
            ) from exc

    def _bars(self, instrument_id: str, cutoff: datetime) -> list[PriceBar]:
        if self._market_data is None:
            raise ForecastDataError("market data is not initialised")
        try:
            page = self._market_data.bars(
                instrument_id,
                limit=self.policy.max_history_bars,
                # The store applies LIMIT before ORDER BY, so ascending=True
                # would return the OLDEST N bars and silently exclude the
                # newest data once history exceeds the window.
                ascending=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise ForecastDataError(
                f"historical bars are unavailable: {type(exc).__name__}"
            ) from exc
        bars: list[PriceBar] = []
        for bar in page.items:
            if getattr(bar, "partial", False):
                continue
            quality = str(getattr(bar, "quality", "valid"))
            if quality in {"invalid", "rejected"}:
                continue
            if bar.observed_at > cutoff:
                continue
            bars.append(
                PriceBar(
                    observed_at=bar.observed_at,
                    open=float(bar.open),
                    high=float(bar.high),
                    low=float(bar.low),
                    close=float(bar.close),
                    volume=float(bar.volume) if bar.volume is not None else None,
                )
            )
        bars.sort(key=lambda item: item.observed_at)
        return bars

    def _event_context(
        self, instrument_id: str, cutoff: datetime
    ) -> tuple[tuple[str, ...], bool]:
        """Confirmed events known at the cutoff; high importance widens intervals."""
        if self._radar is None:
            return (), False
        try:
            body = self._radar.instrument_events(instrument_id, limit=20)
        except Exception:  # noqa: BLE001 - events are context, not a blocker
            return (), False
        ids: list[str] = []
        pressure = False
        for event in body.get("events", ()):
            published = event.get("published_at")
            if published:
                try:
                    if datetime.fromisoformat(str(published)) > cutoff:
                        continue
                except ValueError:
                    pass
            if str(event.get("verification") or "") not in CONFIRMED_EVENT_STATES:
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id:
                continue
            ids.append(event_id)
            if str(event.get("importance") or "") in {"high", "critical"}:
                pressure = True
        return tuple(ids[:10]), pressure

    def _candidate_score(self, instrument_id: str) -> float | None:
        """NM-05 context, recorded as provenance — never a return observation."""
        if self._discovery is None:
            return None
        try:
            row = self._discovery.candidate(instrument_id)
        except Exception:  # noqa: BLE001
            return None
        value = row.get("last_score")
        return float(value) if value is not None else None

    def _target_at(
        self, instrument: dict[str, Any], at: datetime, sessions: int
    ) -> datetime:
        exchange = str(instrument.get("exchange") or "").upper()
        zone = (
            ZoneInfo(SESSION_TABLE[exchange][0])
            if exchange in SESSION_TABLE
            else UTC
        )
        cursor = at.astimezone(zone)
        remaining = sessions
        while remaining > 0:
            cursor += timedelta(days=1)
            if cursor.weekday() >= 5:
                continue
            remaining -= 1
        return cursor

    @staticmethod
    def _uncertainty(width: float, policy: ForecastPolicy) -> UncertaintyLevel:
        if width <= policy.uncertainty_low_pct / 100.0:
            return UncertaintyLevel.LOW
        if width >= policy.uncertainty_high_pct / 100.0:
            return UncertaintyLevel.HIGH
        return UncertaintyLevel.MEDIUM

    # -- forecasting ----------------------------------------------------------

    async def generate(
        self,
        instrument_id: str,
        horizon: Horizon | str,
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create, store and publish one probabilistic forecast."""
        try:
            chosen = Horizon(horizon)
        except ValueError as exc:
            known = ", ".join(item.value for item in Horizon)
            raise ForecastValidationError(
                f"unknown horizon {horizon!r}; known: {known}"
            ) from exc
        sessions = HORIZON_SESSIONS[chosen]
        cutoff = at or self._now()
        instrument = self._instrument(instrument_id)
        bars = self._bars(instrument_id, cutoff)
        features = compute_features(
            bars,
            instrument_id=instrument_id,
            cutoff=cutoff,
            policy=self.policy,
        )
        if features is None:
            raise ForecastDataError(
                f"{instrument_id} has fewer than {self.policy.min_history_bars} "
                "usable completed bars; no forecast is produced"
            )
        age_hours = (cutoff - features.last_observed_at).total_seconds() / 3600.0
        if age_hours > self.policy.max_forecast_age_hours:
            raise ForecastDataError(
                f"the newest bar is {age_hours:.1f}h old; the freshness limit is "
                f"{self.policy.max_forecast_age_hours:.0f}h"
            )
        model_id = self._store.selection(instrument_id, chosen.value) or "random_walk"
        closes = [bar.close for bar in bars]
        try:
            output = estimators.forecast(model_id, closes, sessions, self.policy)
        except ValueError as exc:
            raise ForecastDataError(
                f"{model_id} cannot forecast {instrument_id}: {exc}"
            ) from exc
        if output.lower_return is None or output.upper_return is None:
            raise ForecastDataError(f"{model_id} produced no predictive interval")
        event_ids, pressure = self._event_context(instrument_id, cutoff)
        lower = output.lower_return
        upper = output.upper_return
        notes = list(output.notes)
        if pressure:
            factor = self.policy.event_interval_widen
            lower = output.predicted_return - (output.predicted_return - lower) * factor
            upper = output.predicted_return + (upper - output.predicted_return) * factor
            notes.append(
                "interval widened for a confirmed high-importance event "
                "(conservative adjustment, not a validated accuracy claim)"
            )
        width = upper - lower
        target_at = self._target_at(instrument, cutoff, sessions)
        digest = digest_payload(
            {
                "methodology": METHODOLOGY_VERSION,
                "feature_set_version": FEATURE_SET_VERSION,
                "instrument_id": instrument_id,
                "cutoff": cutoff.isoformat(),
                "horizon": chosen.value,
                "model_id": model_id,
                "features": features.digest,
                "predicted_return": output.predicted_return,
                "p_up": output.p_up,
                "p_down": output.p_down,
                "lower": lower,
                "upper": upper,
                "policy": self.policy.to_dict(),
            }
        )
        record = ForecastRecord(
            forecast_id=forecast_id_for(instrument_id, cutoff, chosen, model_id),
            instrument_id=instrument_id,
            created_at=cutoff,
            data_cutoff=cutoff,
            horizon=chosen,
            horizon_sessions=sessions,
            target_at=target_at,
            model_id=model_id,
            model_version=MODEL_CATALOG_VERSION,
            feature_set_version=FEATURE_SET_VERSION,
            predicted_return=output.predicted_return,
            predicted_price=features.last_close * (1.0 + output.predicted_return),
            p_up=float(output.p_up or 0.0),
            p_down=float(output.p_down or 0.0),
            p_sideways=float(output.p_sideways or 0.0),
            lower_return=lower,
            upper_return=upper,
            lower_price=features.last_close * (1.0 + lower),
            upper_price=features.last_close * (1.0 + upper),
            uncertainty=self._uncertainty(width, self.policy),
            uncertainty_width=width,
            data_quality=(
                "partial"
                if any(bar.volume is None for bar in bars)
                else "good"
            ),
            regime=classify_regime(features),
            event_ids=event_ids,
            candidate_score=self._candidate_score(instrument_id),
            methodology_version=METHODOLOGY_VERSION,
            digest=digest,
            notes=tuple(notes),
        )
        self._store.save_forecast(record)
        self._last_error = ""
        await self._publish(
            "novamarket.forecast.created",
            forecast_id=record.forecast_id,
            instrument_id=instrument_id,
            horizon=chosen.value,
            model_id=model_id,
        )
        await self._publish(
            "novamarket.forecast.completed",
            forecast_id=record.forecast_id,
            instrument_id=instrument_id,
            horizon=chosen.value,
            model_id=model_id,
            predicted_return=round(record.predicted_return, 6),
            p_up=round(record.p_up, 6),
        )
        return {
            "ok": True,
            "status": ForecastStatus.PENDING.value,
            **record.to_dict(),
            "features": features.to_dict(),
            "target": "wait for target_at, then POST /market/forecasts/evaluate",
        }

    # -- lifecycle ------------------------------------------------------------

    async def evaluate_due(
        self, *, at: datetime | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Resolve forecasts whose target has data; never rewrite a forecast."""
        now = at or self._now()
        pending = self._store.pending_due(now, limit=limit)
        evaluated = unresolved = waiting = skipped = 0
        results: list[dict[str, Any]] = []
        for row in pending:
            forecast_id = str(row["forecast_id"])
            horizon = str(row["horizon"])
            try:
                bars = self._bars(str(row["instrument_id"]), now)
            except ForecastDataError:
                skipped += 1
                continue
            cutoff = datetime.fromisoformat(str(row["data_cutoff"]))
            target = datetime.fromisoformat(str(row["target_at"]))
            base = next(
                (bar for bar in reversed(bars) if bar.observed_at <= cutoff), None
            )
            actual_bar = next(
                (bar for bar in bars if bar.observed_at >= target), None
            )
            if base is None:
                if (now - target).days > self.policy.unresolved_grace_days:
                    await self._mark_unresolved(
                        row, now, "no bar at or before the data cutoff"
                    )
                    unresolved += 1
                else:
                    waiting += 1
                continue
            if actual_bar is None:
                if (now - target).days > self.policy.unresolved_grace_days:
                    reason = f"no completed bar at or after {target.isoformat()}"
                    await self._mark_unresolved(row, now, reason)
                    unresolved += 1
                else:
                    waiting += 1
                continue
            actual_return = (actual_bar.close / base.close) - 1.0
            threshold = self.policy.directional_threshold_pct / 100.0
            p_up = float(row["p_up"])
            p_down = float(row["p_down"])
            p_sideways = float(row["p_sideways"])
            direction_correct = predicted_direction(
                p_up, p_down, p_sideways
            ) == direction_of(actual_return, threshold)
            actual_direction = direction_of(actual_return, threshold)
            brier = (
                (p_up - (1.0 if actual_direction == "up" else 0.0)) ** 2
                + (p_down - (1.0 if actual_direction == "down" else 0.0)) ** 2
                + (p_sideways - (1.0 if actual_direction == "sideways" else 0.0)) ** 2
            )
            probability = {
                "up": p_up,
                "down": p_down,
                "sideways": p_sideways,
            }[actual_direction]
            outcome = ForecastOutcome(
                forecast_id=forecast_id,
                instrument_id=str(row["instrument_id"]),
                status=ForecastStatus.EVALUATED,
                evaluated_at=now,
                actual_return=actual_return,
                actual_price=actual_bar.close,
                error=float(row["predicted_return"]) - actual_return,
                direction_correct=direction_correct,
                brier=brier,
                log_loss=-math.log(max(probability, 1e-12)),
                inside_interval=(
                    float(row["lower_return"])
                    <= actual_return
                    <= float(row["upper_return"])
                ),
            )
            recorded = self._store.record_outcome(outcome)
            if not recorded:
                skipped += 1
                continue
            evaluated += 1
            await self._publish(
                "novamarket.forecast.evaluated",
                forecast_id=forecast_id,
                instrument_id=str(row["instrument_id"]),
                horizon=horizon,
                model_id=str(row["model_id"]),
                direction_correct=direction_correct,
                brier=round(brier, 6),
            )
            snapshot = self._snapshot(
                str(row["model_id"]),
                str(row["instrument_id"]),
                horizon,
                now,
            )
            if snapshot is not None:
                await self._publish(
                    "novamarket.forecast.performance.updated",
                    model_id=str(row["model_id"]),
                    horizon=horizon,
                    samples=snapshot["samples"],
                )
            results.append(
                {
                    "forecast_id": forecast_id,
                    "status": "evaluated",
                    "actual_return": round(actual_return, 6),
                    "brier": round(brier, 6),
                    "snapshot": snapshot,
                }
            )
        return {
            "ok": True,
            "evaluated": evaluated,
            "unresolved": unresolved,
            "waiting": waiting,
            "skipped": skipped,
            "results": results,
            "at": now.isoformat(),
        }

    async def _mark_unresolved(
        self, row: dict[str, Any], at: datetime, reason: str
    ) -> None:
        outcome = ForecastOutcome(
            forecast_id=str(row["forecast_id"]),
            instrument_id=str(row["instrument_id"]),
            status=ForecastStatus.UNRESOLVED,
            evaluated_at=at,
            actual_return=None,
            actual_price=None,
            error=None,
            direction_correct=None,
            brier=None,
            log_loss=None,
            inside_interval=None,
            reason=reason,
        )
        if not self._store.record_outcome(outcome):
            return
        await self._publish(
            "novamarket.forecast.evaluation.pending",
            forecast_id=outcome.forecast_id,
            instrument_id=outcome.instrument_id,
            reason=reason,
        )

    def _snapshot(
        self,
        model_id: str,
        instrument_id: str,
        horizon: str,
        at: datetime,
    ) -> dict[str, Any] | None:
        outcomes = self._store.outcomes(
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=200,
        )
        if not outcomes:
            return None
        brier_values = [row["brier"] for row in outcomes if row["brier"] is not None]
        errors = [abs(row["error"]) for row in outcomes if row["error"] is not None]
        coverage = [
            1.0 if row["inside_interval"] else 0.0
            for row in outcomes
            if row["inside_interval"] is not None
        ]
        metrics = {
            "samples": len(outcomes),
            "brier": round(fmean(brier_values), 6) if brier_values else None,
            "mae": round(fmean(errors), 6) if errors else None,
            "coverage": round(fmean(coverage), 6) if coverage else None,
        }
        payload = {
            "snapshot_id": "fps_"
            + hashlib.sha256(
                f"{model_id}|{instrument_id}|{horizon}|{at.isoformat()}".encode()
            ).hexdigest()[:16],
            "model_id": model_id,
            "instrument_id": instrument_id,
            "horizon": horizon,
            "samples": len(outcomes),
            "created_at": at.isoformat(),
            "metrics": metrics,
        }
        self._store.save_performance(payload)
        return metrics

    # -- training and model health -------------------------------------------

    async def train(
        self,
        instrument_id: str,
        horizon: Horizon | str,
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Chronological walk-forward comparison, selection and calibration."""
        try:
            chosen = Horizon(horizon)
        except ValueError as exc:
            raise ForecastValidationError(
                f"unknown horizon {horizon!r}"
            ) from exc
        cutoff = at or self._now()
        self._instrument(instrument_id)
        bars = self._bars(instrument_id, cutoff)
        closes = [bar.close for bar in bars]
        if len(closes) < self.policy.min_training_bars:
            raise ForecastDataError(
                f"{instrument_id} has {len(closes)} bars; "
                f"{self.policy.min_training_bars} are required to train"
            )
        sessions = HORIZON_SESSIONS[chosen]
        result = evaluate_models(closes, sessions, self.policy)
        if not result.scores:
            raise ForecastDataError(
                "not enough chronological windows for walk-forward validation"
            )
        baseline = result.scores.get("random_walk")
        best_id, best = best_model(
            result, minimum_samples=self.policy.min_validation_samples
        )
        selected = "random_walk"
        if (
            best is not None
            and best.brier is not None
            and baseline is not None
            and baseline.brier is not None
            and best.brier < baseline.brier
        ):
            selected = best.model_id
        baseline_brier = baseline.brier if baseline is not None else None
        for model_id, score in result.scores.items():
            if model_id == selected:
                state = ModelState.ACTIVE
                reason = (
                    f"selected on walk-forward Brier {score.brier} vs baseline "
                    f"{baseline_brier}"
                )
            elif (
                score.brier is not None
                and baseline_brier is not None
                and score.brier <= baseline_brier * 1.05
            ):
                state = ModelState.VALIDATED
                reason = "within 5% of the baseline on walk-forward Brier"
            else:
                state = ModelState.EXPERIMENTAL
                reason = "did not match the baseline on walk-forward Brier"
            kind, description = estimators.MODEL_METADATA[model_id]
            self._store.upsert_model(
                ModelRecord(
                    model_id=model_id,
                    version=MODEL_CATALOG_VERSION,
                    state=state,
                    kind=kind,
                    description=description,
                    trained_at=cutoff,
                    updated_at=cutoff,
                    instrument_id=instrument_id,
                    horizon=chosen.value,
                    metrics={**score.to_dict(), "baseline_brier": baseline_brier},
                    reason=reason,
                )
            )
            await self._publish(
                "novamarket.forecast.model.trained",
                model_id=model_id,
                instrument_id=instrument_id,
                horizon=chosen.value,
                state=state.value,
            )
        self._store.save_selection(
            instrument_id,
            chosen.value,
            selected,
            result.scores[selected].to_dict() if selected in result.scores else {},
            cutoff,
        )
        calibration_reports: dict[str, Any] = {}
        for model_id in dict.fromkeys((selected, "random_walk")):
            samples = result.calibration.get(model_id)
            if not samples:
                continue
            report = calibrate_probability(
                samples,
                min_samples=self.policy.calibration_min_samples,
                holdout_fraction=self.policy.calibration_holdout_fraction,
            )
            payload = {
                "calibration_id": "fcal_"
                + hashlib.sha256(
                    f"{instrument_id}|{chosen.value}|{model_id}|{cutoff.isoformat()}".encode()
                ).hexdigest()[:16],
                "instrument_id": instrument_id,
                "model_id": model_id,
                "horizon": chosen.value,
                "created_at": cutoff.isoformat(),
                **report.to_dict(),
            }
            self._store.save_calibration(payload)
            calibration_reports[model_id] = payload
            await self._publish(
                "novamarket.forecast.calibration.completed",
                model_id=model_id,
                horizon=chosen.value,
                status=report.status,
                samples=report.samples,
            )
        run = {
            "run_id": "ftr_"
            + hashlib.sha256(
                f"{instrument_id}|{chosen.value}|{cutoff.isoformat()}".encode()
            ).hexdigest()[:16],
            "instrument_id": instrument_id,
            "horizon": chosen.value,
            "model_id": selected,
            "version": MODEL_CATALOG_VERSION,
            "started_at": cutoff.isoformat(),
            "finished_at": self._now().isoformat(),
            "samples": result.origins,
            "metrics": {key: score.to_dict() for key, score in result.scores.items()},
            "calibration": {
                key: report.get("status") for key, report in calibration_reports.items()
            },
            "hyperparameters": self.policy.to_dict(),
            "seed": "deterministic",
            "feature_set_version": FEATURE_SET_VERSION,
        }
        self._store.save_training_run(run)
        await self._publish(
            "novamarket.forecast.model.validated",
            instrument_id=instrument_id,
            horizon=chosen.value,
            selected_model=selected,
            baseline_brier=baseline_brier,
        )
        await self._publish(
            "novamarket.forecast.model.activated",
            model_id=selected,
            instrument_id=instrument_id,
            horizon=chosen.value,
        )
        return {
            "ok": True,
            "instrument_id": instrument_id,
            "horizon": chosen.value,
            "selected_model": selected,
            "baseline_model": "random_walk",
            "scores": {key: score.to_dict() for key, score in result.scores.items()},
            "calibration": calibration_reports,
            "origins": result.origins,
            "run_id": run["run_id"],
        }

    async def promote(
        self, model_id: str, instrument_id: str, horizon: Horizon | str
    ) -> dict[str, Any]:
        """Promote a validated model; never a model without walk-forward evidence."""
        label = _required_horizon(horizon)
        record = self._store.model(model_id, instrument_id, label)
        if record is None:
            raise ForecastNotFoundError(
                f"unknown model {model_id!r} for {instrument_id} {label}"
            )
        if record.state is ModelState.ACTIVE:
            return {"ok": True, **record.to_dict()}
        if record.metrics.get("brier") is None:
            raise ForecastValidationError(
                "a model without a measured Brier score cannot be promoted"
            )
        at = self._now()
        try:
            if record.state is ModelState.EXPERIMENTAL:
                self._store.set_model_state(
                    model_id,
                    instrument_id,
                    label,
                    ModelState.VALIDATED,
                    "promotion: walk-forward metrics recorded",
                    at,
                )
            updated = self._store.set_model_state(
                model_id,
                instrument_id,
                label,
                ModelState.ACTIVE,
                "promoted after validation",
                at,
            )
        except ValueError as exc:
            raise ForecastStateError(str(exc)) from exc
        await self._publish(
            "novamarket.forecast.model.activated",
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=label,
        )
        return {"ok": True, **(updated.to_dict() if updated else record.to_dict())}

    async def check_health(self, *, at: datetime | None = None) -> dict[str, Any]:
        """Degrade or suspend a model whose measured reliability fell."""
        moment = at or self._now()
        changes: list[dict[str, Any]] = []
        for record in self._store.models():
            if record.state not in {ModelState.ACTIVE, ModelState.DEGRADED}:
                continue
            outcomes = self._store.outcomes(
                model_id=record.model_id,
                instrument_id=record.instrument_id,
                horizon=record.horizon,
                limit=100,
            )
            unresolved = self._store.outcomes(
                model_id=record.model_id,
                instrument_id=record.instrument_id,
                horizon=record.horizon,
                status="unresolved",
                limit=200,
            )
            if len(unresolved) >= self.policy.suspend_unresolved:
                updated = self._store.set_model_state(
                    record.model_id,
                    record.instrument_id,
                    record.horizon,
                    ModelState.SUSPENDED,
                    f"{len(unresolved)} unresolved outcomes",
                    moment,
                )
                await self._publish(
                    "novamarket.forecast.model.suspended",
                    model_id=record.model_id,
                    horizon=record.horizon,
                    unresolved=len(unresolved),
                )
                changes.append(updated.to_dict() if updated else {})
                continue
            if len(outcomes) < 10:
                continue
            brier = fmean(row["brier"] for row in outcomes if row["brier"] is not None)
            coverage = fmean(
                1.0 if row["inside_interval"] else 0.0
                for row in outcomes
                if row["inside_interval"] is not None
            )
            baseline_brier = record.metrics.get("baseline_brier")
            missed_probability = (
                baseline_brier is not None
                and brier > float(baseline_brier) + self.policy.degrade_brier_margin
            )
            missed_interval = coverage < self.policy.degrade_coverage_floor
            if (missed_probability or missed_interval) and record.state is ModelState.ACTIVE:
                updated = self._store.set_model_state(
                    record.model_id,
                    record.instrument_id,
                    record.horizon,
                    ModelState.DEGRADED,
                    (
                        f"recent Brier {brier:.4f} vs baseline {baseline_brier}, "
                        f"coverage {coverage:.2f}"
                    ),
                    moment,
                )
                await self._publish(
                    "novamarket.forecast.model.degraded",
                    model_id=record.model_id,
                    horizon=record.horizon,
                    brier=round(brier, 6),
                    coverage=round(coverage, 6),
                )
                changes.append(updated.to_dict() if updated else {})
        return {"ok": True, "checked": len(self._store.models()), "changes": changes}

    # -- reads ----------------------------------------------------------------

    def forecast(self, forecast_id: str) -> dict[str, Any]:
        row = self._store.forecast(forecast_id)
        if row is None:
            raise ForecastNotFoundError(f"unknown forecast {forecast_id!r}")
        return {"ok": True, **row}

    def forecasts(
        self,
        *,
        instrument_id: str | None = None,
        horizon: Horizon | str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        label = _horizon_label(horizon)
        rows = self._store.forecasts(
            instrument_id=instrument_id, horizon=label, status=status, limit=limit
        )
        return {"ok": True, "forecasts": rows, "total": len(rows)}

    def outcome(self, forecast_id: str) -> dict[str, Any]:
        if self._store.forecast(forecast_id) is None:
            raise ForecastNotFoundError(f"unknown forecast {forecast_id!r}")
        body = self._store.outcome(forecast_id)
        if body is None:
            return {
                "ok": True,
                "forecast_id": forecast_id,
                "status": "pending",
                "detail": "the target timestamp has not produced an outcome yet",
            }
        return {"ok": True, **body}

    def performance(
        self,
        *,
        model_id: str | None = None,
        instrument_id: str | None = None,
        horizon: Horizon | str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        label = _horizon_label(horizon)
        snapshots = self._store.performance(
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=label,
            limit=limit,
        )
        outcomes = self._store.outcomes(
            model_id=model_id, instrument_id=instrument_id, horizon=label, limit=500
        )
        brier = [row["brier"] for row in outcomes if row["brier"] is not None]
        errors = [abs(row["error"]) for row in outcomes if row["error"] is not None]
        coverage = [
            1.0 if row["inside_interval"] else 0.0
            for row in outcomes
            if row["inside_interval"] is not None
        ]
        return {
            "ok": True,
            "snapshots": snapshots,
            "live": {
                "samples": len(outcomes),
                "brier": round(fmean(brier), 6) if brier else None,
                "mae": round(fmean(errors), 6) if errors else None,
                "coverage": round(fmean(coverage), 6) if coverage else None,
            },
        }

    def models(
        self, *, instrument_id: str | None = None, horizon: Horizon | str | None = None
    ) -> dict[str, Any]:
        label = _horizon_label(horizon)
        rows = [
            record.to_dict()
            for record in self._store.models(
                instrument_id=instrument_id, horizon=label
            )
        ]
        return {"ok": True, "models": rows, "total": len(rows)}

    def calibration(
        self, model_id: str, horizon: Horizon | str
    ) -> dict[str, Any]:
        cleaned = str(model_id or "").strip()
        if not cleaned:
            raise ForecastValidationError("a model_id is required")
        label = _required_horizon(horizon)
        body = self._store.calibration(cleaned, label)
        if body is None:
            return {
                "ok": True,
                "model_id": cleaned,
                "horizon": label,
                "status": "not_calibrated",
                "detail": "no calibration has been run for this model and horizon",
            }
        return {"ok": True, **body}

    def training_runs(self, *, limit: int = 20) -> dict[str, Any]:
        rows = self._store.training_runs(limit=limit)
        return {"ok": True, "runs": rows, "total": len(rows)}

    def reliability(self, *, model_id: str | None = None) -> dict[str, Any]:
        rows = self._store.reliability(model_id=model_id)
        return {"ok": True, "transitions": rows, "total": len(rows)}

    async def close(self) -> None:
        self._store.close()

    # -- status ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        metrics = self._store.metrics()
        return {
            "initialised": True,
            "phase": "NM-06",
            "schema_version": self._store.schema_version,
            "policy": self.policy.to_dict(),
            "horizons": {
                item.value: HORIZON_SESSIONS[item] for item in Horizon
            },
            "feature_set_version": FEATURE_SET_VERSION,
            "model_catalog_version": MODEL_CATALOG_VERSION,
            "models": metrics["models"],
            "metrics": metrics,
            "last_error": self._last_error,
        }

    def metrics(self) -> dict[str, Any]:
        return self._store.metrics()

    def contract(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "phase": "NM-06",
            "horizons": {item.value: HORIZON_SESSIONS[item] for item in Horizon},
            "probability_definition": {
                "up": f"return > +{self.policy.directional_threshold_pct}%",
                "down": f"return < -{self.policy.directional_threshold_pct}%",
                "sideways": (
                    f"|return| <= {self.policy.directional_threshold_pct}%"
                ),
                "tolerance": 1e-6,
                "note": "the three probabilities sum to 1 within the tolerance",
            },
            "interval": {
                "low_quantile": self.policy.interval_low,
                "high_quantile": self.policy.interval_high,
                "coverage_is_measured": True,
            },
            "models": [
                {"model_id": model_id, "kind": kind, "description": description}
                for model_id, (kind, description) in estimators.MODEL_METADATA.items()
            ],
            "policy": self.policy.to_dict(),
            "data": {
                "market_data": "NM-02 completed daily bars at or before the cutoff",
                "events": "NM-04 confirmed events published at or before the cutoff",
                "candidate": "NM-05 score recorded as provenance, never a return",
            },
            "guarantees": [
                "a forecast is an immutable record; outcomes never rewrite it",
                "no bar after the data cutoff may influence its features",
                "probabilities are not certainties and prices are not promised",
                "a model is ACTIVE only after walk-forward evidence",
                "the three directional probabilities sum to 1",
            ],
            "events": [
                "novamarket.forecast.created",
                "novamarket.forecast.completed",
                "novamarket.forecast.evaluation.pending",
                "novamarket.forecast.evaluated",
                "novamarket.forecast.model.trained",
                "novamarket.forecast.model.validated",
                "novamarket.forecast.model.activated",
                "novamarket.forecast.model.degraded",
                "novamarket.forecast.model.suspended",
                "novamarket.forecast.calibration.completed",
                "novamarket.forecast.performance.updated",
            ],
        }


__all__ = ["CONFIRMED_EVENT_STATES", "ForecastEngine"]
