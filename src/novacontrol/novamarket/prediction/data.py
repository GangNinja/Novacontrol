"""Read-only market-data bridge for Prediction Intelligence.

Prediction Intelligence is a *consumer* of market data, never a second data
acquisition authority.  This gateway calls the existing ``MarketDataEngine``
read methods (``get_instrument``, ``latest``, ``bars``) and turns their results
into a bounded snapshot.  It does not fetch a provider, add a provider, alter a
rate limit, or remove the 15-minute general refresh floor.

Two timestamps are preserved separately: when the provider observed the price
and when NovaMarket received it.  Freshness is assessed against the requested
lifecycle stage, not against the time the snapshot happened to be read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    Instrument,
    OhlcvBar,
    PriceObservation,
    ReconciliationResult,
    ReconciliationStatus,
)
from novacontrol.novamarket.prediction.models import (
    AcquisitionPolicy,
    ConfirmationStage,
    ForecastFamily,
    ForecastHorizon,
    HORIZON_SPECS,
    MarketDataSnapshot,
    PredictionDataError,
    PredictionPolicy,
    PredictionRequest,
    PredictionValidationError,
    RequestPriority,
    horizon_spec,
    prediction_id,
)

_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "429", "too many requests")
_USABLE_QUALITIES = frozenset({DataQuality.VALID, DataQuality.WARNING})


class PredictionDataGateway:
    """Build the exact data snapshot a forecast can cite."""

    def __init__(
        self,
        market_data: Any,
        *,
        policy: PredictionPolicy | None = None,
        acquisition: AcquisitionPolicy | None = None,
        clock: Any | None = None,
    ) -> None:
        if market_data is None:
            raise PredictionValidationError("a MarketDataEngine (or compatible reader) is required")
        self._market_data = market_data
        self.policy = policy or PredictionPolicy()
        self.acquisition = acquisition or AcquisitionPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    def snapshot(
        self,
        request: PredictionRequest,
        *,
        stage: ConfirmationStage = ConfirmationStage.INITIAL,
        now: datetime | None = None,
    ) -> MarketDataSnapshot:
        selected_now = now if now is not None else self._clock()
        if not isinstance(selected_now, datetime):
            raise PredictionValidationError("the market-data clock must return a datetime")
        if selected_now.tzinfo is None or selected_now.utcoffset() is None:
            raise PredictionValidationError("market-data snapshot timestamps must be timezone-aware")
        moment = selected_now.astimezone(UTC)
        spec = horizon_spec(request.horizon)
        reasons: list[str] = []
        try:
            instrument = self._market_data.get_instrument(request.instrument_id)
        except Exception as exc:  # existing engine raises its own domain errors
            raise PredictionDataError(
                f"instrument {request.instrument_id} is not available: {exc}"
            ) from exc
        if instrument.instrument_id != request.instrument_id:
            raise PredictionDataError("the market-data engine returned another instrument")

        bars_page = self._read_bars(instrument.instrument_id, spec.horizon, moment)
        bars = tuple(
            bar
            for bar in getattr(bars_page, "items", ())
            if self._bar_is_eligible(bar, moment)
        )
        quote_result: ReconciliationResult | None = None
        quote_error = ""
        try:
            quote_result = self._market_data.latest(instrument.instrument_id)
        except Exception as exc:
            quote_error = str(exc)
            reasons.append(f"latest quote read failed: {exc}")

        quote_conflict = bool(
            quote_result is not None
            and quote_result.status is ReconciliationStatus.CONFLICT
        )
        selected_quote = (
            quote_result.selected
            if quote_result is not None and not quote_conflict
            else None
        )
        if quote_conflict:
            reasons.append("providers disagree; no quote is selected")
        if quote_result is not None and quote_result.reasons:
            reasons.extend(str(item) for item in quote_result.reasons)

        use_bar = self._prefer_bar(spec.family) and bool(bars)
        reference_bar: OhlcvBar | None = None
        reference_quote: PriceObservation | None = None
        if use_bar:
            reference_bar = max(bars, key=lambda item: item.observed_at)
        elif selected_quote is not None and self._quote_is_eligible(selected_quote):
            reference_quote = selected_quote
        elif bars:
            reference_bar = max(bars, key=lambda item: item.observed_at)
        elif selected_quote is not None:
            reference_quote = selected_quote
            if not self._quote_is_eligible(selected_quote):
                reasons.append(
                    "the selected quote is not usable for a prediction "
                    f"(quality={selected_quote.quality.value}, "
                    f"freshness={selected_quote.freshness.value})"
                )

        provider = ""
        observed_at: datetime | None = None
        received_at: datetime | None = None
        resolution = DataInterval.SPOT
        price = None
        currency = instrument.currency
        quality = DataQuality.UNAVAILABLE
        source_freshness = Freshness.MISSING
        provenance: dict[str, Any] = {
            "instrument_id": instrument.instrument_id,
            "exchange": instrument.exchange,
            "mic": instrument.mic,
            "symbol": instrument.symbol,
            "source_timezone": instrument.timezone,
            "data_path": "intraday_quote" if reference_quote is not None else "historical",
            "general_refresh_floor_seconds": self.acquisition.general_refresh_floor_seconds,
            "discovery_batch_cap": self.acquisition.discovery_batch_cap,
        }
        evidence: list[dict[str, Any]] = []
        if reference_bar is not None:
            provider = reference_bar.provider
            observed_at = reference_bar.observed_at
            received_at = reference_bar.retrieved_at
            resolution = reference_bar.interval
            price = reference_bar.close
            currency = reference_bar.currency
            quality = reference_bar.quality
            source_freshness = reference_bar.freshness
            provenance.update(
                {
                    "bar_id": reference_bar.bar_id,
                    "interval": reference_bar.interval.value,
                    "open": format(reference_bar.open, "f"),
                    "high": format(reference_bar.high, "f"),
                    "low": format(reference_bar.low, "f"),
                    "close": format(reference_bar.close, "f"),
                    "volume": (
                        str(getattr(reference_bar, "volume", None))
                        if getattr(reference_bar, "volume", None) is not None
                        else None
                    ),
                    "partial": getattr(reference_bar, "partial", False),
                    "source_timezone": getattr(
                        reference_bar, "source_timezone", instrument.timezone
                    ),
                    "bar_reasons": list(getattr(reference_bar, "reasons", ())),
                    "provider_metadata": dict(
                        getattr(reference_bar, "provider_metadata", {})
                    ),
                }
            )
            evidence.append(reference_bar.to_dict())
        elif reference_quote is not None:
            provider = reference_quote.provider
            observed_at = reference_quote.observed_at
            received_at = reference_quote.retrieved_at
            resolution = DataInterval.SPOT
            price = reference_quote.price
            currency = reference_quote.currency
            quality = reference_quote.quality
            source_freshness = reference_quote.freshness
            provenance.update(
                {
                    "observation_id": reference_quote.observation_id,
                    "live": reference_quote.live,
                    "source_timezone": reference_quote.source_timezone,
                    "quote_reasons": list(reference_quote.reasons),
                    "provider_metadata": dict(reference_quote.provider_metadata),
                }
            )
            evidence.append(reference_quote.to_dict())

        if reference_quote is not None and reference_bar is None:
            provenance["provider"] = reference_quote.provider
        elif reference_bar is not None:
            provenance["provider"] = reference_bar.provider

        provider_health = ""
        rate_limited = False
        if provider:
            try:
                health = self._market_data.provider_health(provider)
                provider_health = getattr(
                    getattr(health, "status", ""), "value", str(getattr(health, "status", ""))
                )
                last_error = str(getattr(health, "last_error", "") or "").lower()
                rate_limited = any(hint in last_error for hint in _RATE_LIMIT_HINTS)
                provenance["provider_health"] = {
                    "provider": provider,
                    "status": provider_health,
                    "circuit_state": getattr(
                        getattr(health, "circuit_state", ""),
                        "value",
                        str(getattr(health, "circuit_state", "")),
                    ),
                    "latency_ms": getattr(health, "latency_ms", None),
                    "last_error": getattr(health, "last_error", ""),
                }
            except Exception as exc:
                provider_health = "unknown"
                reasons.append(f"provider health read failed: {exc}")

        if quote_error:
            provenance["quote_error"] = quote_error
        if quote_result is not None:
            provenance["reconciliation_status"] = quote_result.status.value
            provenance["reconciliation_reasons"] = list(quote_result.reasons)
        provenance["rate_limited"] = rate_limited
        provenance["source_freshness"] = source_freshness.value
        provenance["stage"] = stage.value
        provenance["max_age_seconds"] = self.policy.max_age_seconds(spec.family, stage)

        if observed_at is None or received_at is None or price is None:
            missing = True
            age_seconds = None
            freshness = Freshness.MISSING
            usable = False
            freshness = Freshness.MISSING
            reasons.append("no usable market observation is available")
        else:
            missing = False
            age = max(moment - observed_at, timedelta(0))
            age_seconds = int(age.total_seconds())
            max_age = self.policy.max_age_seconds(spec.family, stage)
            within_window = age <= timedelta(seconds=max_age)
            if source_freshness is Freshness.CLOSED_MARKET and within_window:
                freshness = Freshness.CLOSED_MARKET
            elif not within_window:
                freshness = Freshness.STALE
                reasons.append(
                    f"data is {age_seconds}s old; stage {stage.value} allows {max_age}s"
                )
            elif source_freshness in (
                Freshness.DELAYED,
                Freshness.STALE,
                Freshness.UNKNOWN,
                Freshness.MISSING,
                Freshness.PROVIDER_UNAVAILABLE,
            ):
                freshness = source_freshness
            else:
                freshness = Freshness.FRESH
            if not spec.accepts_resolution(resolution):
                usable = False
                reasons.append(
                    f"{resolution.value} data cannot support {spec.horizon.value}; "
                    f"maximum interval is {spec.max_input_interval.value}"
                )
            elif quality not in _USABLE_QUALITIES:
                usable = False
                reasons.append(f"data quality {quality.value} is not usable")
            elif freshness in (
                Freshness.STALE,
                Freshness.MISSING,
                Freshness.PROVIDER_UNAVAILABLE,
                Freshness.UNKNOWN,
            ):
                usable = False
                reasons.append(f"data freshness {freshness.value} is not usable")
            elif quote_conflict:
                usable = False
            else:
                usable = True
            if provider == "":
                usable = False
                reasons.append("the observation has no provider identity")

        history_sufficient = len(bars) >= spec.min_history_bars
        if not history_sufficient:
            reasons.append(
                f"history has {len(bars)} bar(s); {spec.horizon.value} recommends "
                f"{spec.min_history_bars}"
            )
        if rate_limited:
            reasons.append("provider reported a rate-limit condition")
        if quote_conflict:
            # A conflict is a data-quality fact even when a bar supplied the
            # reference price; it must be visible to the consumer.
            provenance["provider_conflict"] = True

        snapshot_id = prediction_id(
            "pds_",
            instrument.instrument_id,
            provider,
            resolution.value,
            observed_at.isoformat() if observed_at else "",
            format(price, "f") if price is not None else "",
            stage.value,
        )
        if price is None:
            # A missing snapshot still needs a positive reference price for the
            # contract.  It is explicitly unusable and no forecast may consume
            # it; 1 is a sentinel, never a fabricated market observation.
            price = Decimal("1")
        return MarketDataSnapshot(
            snapshot_id=snapshot_id,
            instrument_id=instrument.instrument_id,
            exchange=instrument.exchange,
            provider=provider,
            source_observed_at=observed_at or moment - timedelta(days=3650),
            received_at=received_at or moment - timedelta(days=3650),
            resolution=resolution,
            reference_price=price,
            currency=currency,
            quality=quality,
            freshness=freshness if observed_at is not None else Freshness.MISSING,
            usable=usable,
            provider_conflict=quote_conflict,
            missing_observation=missing,
            rate_limited=rate_limited,
            provider_health=provider_health or "unknown",
            history_bars=len(bars),
            history_sufficient=history_sufficient,
            reasons=tuple(dict.fromkeys(reasons)),
            provenance=provenance,
            evidence=tuple(evidence),
        )

    def _read_bars(self, instrument_id: str, horizon: ForecastHorizon, moment: datetime) -> Any:
        spec = HORIZON_SPECS[horizon]
        try:
            return self._market_data.bars(
                instrument_id,
                interval=spec.max_input_interval,
                end=moment,
                limit=max(spec.min_history_bars * 2, 100),
                ascending=False,
            )
        except Exception:
            # A missing optional history series must not turn a valid current
            # quote into fabricated historical data; the snapshot records zero
            # bars and the history-insufficiency reason.
            return type("_EmptyBarPage", (), {"items": (), "total": 0})()

    @staticmethod
    def _bar_is_eligible(bar: OhlcvBar, moment: datetime) -> bool:
        return (
            not bar.partial
            and bar.observed_at <= moment + timedelta(seconds=300)
            and bar.quality in _USABLE_QUALITIES
            and bar.close > 0
        )

    @staticmethod
    def _quote_is_eligible(quote: PriceObservation) -> bool:
        return quote.usable and quote.quality in _USABLE_QUALITIES

    @staticmethod
    def _prefer_bar(family: ForecastFamily) -> bool:
        return family in (ForecastFamily.SWING, ForecastFamily.LONG_TERM)


def low_latency_contract(policy: AcquisitionPolicy | None = None) -> dict[str, Any]:
    """The explicit intraday path; it is narrower, not a bypass."""
    policy = policy or AcquisitionPolicy()
    return {
        "general_refresh_floor_seconds": policy.general_refresh_floor_seconds,
        "intraday_min_interval_seconds": policy.intraday_min_interval_seconds,
        "intraday_max_requests_per_minute": policy.intraday_max_requests_per_minute,
        "discovery_batch_cap": policy.discovery_batch_cap,
        "max_concurrent_fetches": policy.max_concurrent_fetches,
        "priorities": [
            RequestPriority.ACTIVE_POSITION.value,
            RequestPriority.FORECAST_CANDIDATE.value,
            RequestPriority.WATCHLIST.value,
            RequestPriority.DISCOVERY.value,
        ],
        "distinct_paths": [
            "general_refresh",
            "intraday_quote",
            "historical",
            "news_and_events",
            "fundamentals",
        ],
        "existing_constraints_preserved": True,
        "fair_rotation_preserved": policy.fair_rotation_preserved,
        "exchange_grade_realtime": False,
        "provider_latency_visible": True,
        "rate_limit_visible": True,
        "unsatisfied_freshness_behavior": (
            "The prediction is marked stale/unconfirmed or the ensemble abstains; "
            "the gateway never upgrades a cached observation to fresh."
        ),
    }
