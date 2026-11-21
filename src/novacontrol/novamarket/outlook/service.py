"""PI-10 horizon orchestration over the existing forecast stack.

This module answers three questions that PI-01–PI-09 deliberately left to a
horizon layer:

* what does "1d/3d/5d/30d/90d" mean on the verified exchange calendar;
* which point-in-time context was available to the forecast;
* how do several forecasts for the same instrument compare without merging
  their accuracy into one misleading number.

It does not fit a model, persist a second forecast, or grant any trading
authority.  ``EnsembleForecastingService`` remains the forecast producer and
``PredictionStore``/PI-09 remain the only forecast and lifecycle stores.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.marketdata.models import DataQuality, Freshness, OhlcvBar
from novacontrol.novamarket.outlook.models import (
    CONTEXT_CONTRACT_VERSION,
    HORIZON_OUTLOOK_PROFILES,
    HORIZON_PROFILE_VERSION,
    OUTLOOK_CONTRACT_VERSION,
    PI10_HORIZONS,
    PI10_PHASE,
    ContextSeries,
    HorizonContext,
    HorizonOutlookProfile,
    OutlookStatus,
    outlook_profile,
    validate_outlook_profiles,
)
from novacontrol.novamarket.prediction.models import (
    ConfirmationStage,
    DecisionForecastEvidence,
    Direction,
    ForecastHorizon,
    ForecastOutput,
    ForecastValidity,
    FundamentalSnapshot,
    NewsEventEvidence,
    PredictionValidationError,
    _aware,
    digest,
    prediction_id,
)
from novacontrol.novamarket.scheduling.schedule import (
    ScheduleValidationError,
    default_target,
)
from novacontrol.novamarket.strategies import indicators

_NEWS_VERIFIED = frozenset(
    {
        "confirmed",
        "officially_confirmed",
        "verified",
        "validated",
        "corroborated",
        "multi_source_corroborated",
    }
)
_NEWS_UNVERIFIED = frozenset(
    {
        "rumour",
        "rumor",
        "unverified",
        "unconfirmed",
        "single_source",
        "disputed",
        "conflicting_evidence",
        "rejected",
        "retracted",
        "expired",
    }
)
_ACCEPTED_BAR_QUALITY = frozenset(
    {DataQuality.VALID, DataQuality.WARNING, DataQuality.STALE}
)
_ACCEPTED_FUNDAMENTAL_QUALITY = frozenset({DataQuality.VALID, DataQuality.WARNING})
_ACTIONABLE = frozenset({Direction.UP.value, Direction.DOWN.value, Direction.NEUTRAL.value})
_CANONICAL_INSTRUMENT = re.compile(r"^[A-Z0-9]{2,12}:[A-Za-z0-9^&=._/-]{1,64}$")


class OutlookValidationError(PredictionValidationError):
    """A PI-10 input cannot be interpreted without inventing evidence."""


class OutlookUnavailableError(PredictionValidationError):
    """The requested horizon has no eligible forecast or context."""


@dataclass(frozen=True, slots=True)
class _AssembledContext:
    context: HorizonContext
    eligible_fundamentals: tuple[FundamentalSnapshot, ...] = ()
    eligible_events: tuple[NewsEventEvidence, ...] = ()
    benchmark_bars: Mapping[str, Sequence[OhlcvBar]] = field(default_factory=dict)
    sector_bars: Mapping[str, Sequence[OhlcvBar]] = field(default_factory=dict)
    context_bars: Mapping[str, Mapping[str, Sequence[OhlcvBar]]] = field(
        default_factory=dict
    )


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise OutlookValidationError(f"{name} must be decimal, not bool")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OutlookValidationError(f"{name} is not decimal") from exc
    if not number.is_finite():
        raise OutlookValidationError(f"{name} must be finite")
    return number


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any, name: str) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return _aware(value, name)
    except PredictionValidationError:
        return None


def _metadata(instrument: Any) -> dict[str, Any]:
    payload = getattr(instrument, "metadata", None)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _canonical_instrument(value: Any) -> str:
    cleaned = str(value or "").strip().upper()
    if not _CANONICAL_INSTRUMENT.match(cleaned):
        raise OutlookValidationError(
            f"instrument id {value!r} is not canonical MIC:SYMBOL"
        )
    return cleaned


def _mapping_instrument(metadata: Mapping[str, Any], *keys: str) -> tuple[str, str]:
    """Read a mapping only when it names a canonical registered instrument.

    A sector label such as ``"IT"`` is not silently converted into an index
    symbol.  The caller records that limitation instead.
    """
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, Mapping):
            value = value.get("instrument_id") or value.get("symbol")
        text = str(value or "").strip()
        if not text:
            continue
        try:
            return _canonical_instrument(text), ""
        except OutlookValidationError:
            return "", f"{key} mapping {text!r} is not a canonical instrument id"
    return "", ""


def _fundamental_metrics(item: Mapping[str, Any]) -> dict[str, str]:
    raw = item.get("metrics")
    metrics: dict[str, str] = {}
    if isinstance(raw, Mapping):
        for name, value in raw.items():
            if str(name).strip() and value is not None:
                metrics[str(name).strip()] = str(value)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("metric") or row.get("name") or "").strip()
            value = row.get("value")
            if name and value is not None:
                metrics[name] = str(value)
    return metrics


def fundamental_from_input(
    item: FundamentalSnapshot | Mapping[str, Any],
) -> FundamentalSnapshot:
    """Validate one supplied fundamental record without inventing a timestamp."""
    if isinstance(item, FundamentalSnapshot):
        return item
    if not isinstance(item, Mapping):
        raise OutlookValidationError("a fundamental record must be a mapping")
    metrics = _fundamental_metrics(item)
    if not metrics:
        raise OutlookValidationError("a fundamental record needs at least one metric")
    observed = _timestamp(
        item.get("source_observed_at")
        or item.get("publication_timestamp")
        or item.get("published_at")
        or item.get("reported_at"),
        "fundamental publication timestamp",
    )
    received = _timestamp(
        item.get("received_at")
        or item.get("acquisition_timestamp")
        or item.get("available_at")
        or item.get("retrieved_at"),
        "fundamental acquisition timestamp",
    )
    if observed is None:
        raise OutlookValidationError(
            "a fundamental record needs a publication/source timestamp"
        )
    if received is None:
        raise OutlookValidationError(
            "a fundamental record needs an acquisition/availability timestamp"
        )
    currency = str(item.get("currency") or "").strip().upper()
    try:
        quality = DataQuality(str(item.get("quality") or DataQuality.VALID.value))
        freshness = Freshness(str(item.get("freshness") or "unknown"))
    except ValueError as exc:
        raise OutlookValidationError(
            f"fundamental quality/freshness is not recognized: {exc}"
        ) from exc
    provenance = dict(item.get("provenance") or {})
    units = item.get("units")
    if units is not None:
        provenance["units"] = units
    if "unit" in item:
        provenance["unit"] = item.get("unit")
    snapshot_id = str(item.get("snapshot_id") or "").strip() or (
        "fnd_" + hashlib.sha256(
            json.dumps(
                {
                    "instrument_id": item.get("instrument_id"),
                    "fiscal_period": item.get("fiscal_period"),
                    "observed": observed.isoformat(),
                    "received": received.isoformat(),
                    "metrics": metrics,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
    )
    return FundamentalSnapshot(
        snapshot_id=snapshot_id,
        instrument_id=str(item.get("instrument_id") or ""),
        fiscal_period=str(item.get("fiscal_period") or item.get("period") or ""),
        source_observed_at=observed,
        received_at=received,
        currency=currency,
        metrics=metrics,
        source=str(item.get("source") or ""),
        quality=quality,
        freshness=freshness,
        source_url=str(item.get("source_url") or item.get("url") or ""),
        reasons=tuple(str(value) for value in item.get("reasons") or ()),
        provenance=provenance,
    )


def news_from_input(item: NewsEventEvidence | Mapping[str, Any]) -> NewsEventEvidence:
    """Validate news/corporate-event evidence, including NM-04 radar rows."""
    if isinstance(item, NewsEventEvidence):
        return item
    if not isinstance(item, Mapping):
        raise OutlookValidationError("a news/event record must be a mapping")
    instruments = (
        item.get("instrument_ids")
        or item.get("instruments")
        or item.get("instrument_id")
        or ()
    )
    if isinstance(instruments, str):
        instruments = (instruments,)
    instruments = tuple(str(value).strip().upper() for value in instruments if str(value).strip())
    if not instruments:
        raise OutlookValidationError("a news/event record must name an instrument")
    published = _timestamp(
        item.get("published_at")
        or item.get("occurred_at")
        or item.get("discovered_at"),
        "news publication timestamp",
    )
    received = _timestamp(
        item.get("received_at")
        or item.get("available_at")
        or item.get("retrieved_at")
        or item.get("discovered_at"),
        "news acquisition timestamp",
    )
    if published is None:
        raise OutlookValidationError("news/event evidence needs a publication timestamp")
    if received is None:
        raise OutlookValidationError("news/event evidence needs an availability timestamp")
    metadata = dict(item.get("metadata") or {})
    for key in (
        "verification",
        "verification_status",
        "verified",
        "status",
        "importance",
        "relevance",
        "sentiment",
        "sentiment_validated",
        "category",
        "subcategory",
        "sector",
        "radar_event_id",
        "confidence",
    ):
        if key in item and key not in metadata:
            metadata[key] = item[key]
    try:
        quality = DataQuality(str(item.get("quality") or DataQuality.VALID.value))
        freshness = Freshness(str(item.get("freshness") or "unknown"))
    except ValueError as exc:
        raise OutlookValidationError(
            f"news/event quality/freshness is not recognized: {exc}"
        ) from exc
    event_type = str(
        item.get("event_type")
        or item.get("category")
        or item.get("subcategory")
        or "event"
    )
    evidence_id = str(
        item.get("evidence_id") or item.get("event_id") or item.get("id") or ""
    ).strip() or prediction_id("nev_", instruments, published.isoformat(), item.get("title"))
    return NewsEventEvidence(
        evidence_id=evidence_id,
        instrument_ids=instruments,
        event_type=event_type,
        headline=str(item.get("headline") or item.get("title") or event_type),
        source=str(
            item.get("source")
            or item.get("publisher")
            or item.get("source_id")
            or "unspecified-source"
        ),
        published_at=published,
        received_at=received,
        quality=quality,
        freshness=freshness,
        source_url=str(item.get("source_url") or item.get("url") or ""),
        summary=str(item.get("summary") or item.get("description") or ""),
        checksum=str(
            item.get("checksum")
            or item.get("fingerprint")
            or item.get("content_hash")
            or item.get("duplicate_key")
            or ""
        ),
        metadata=metadata,
    )


def _news_verified(item: NewsEventEvidence) -> bool:
    metadata = dict(item.metadata)
    if str(item.event_type).strip().lower() in _NEWS_UNVERIFIED:
        return False
    if str(metadata.get("status") or "").strip().lower() in {
        "retracted",
        "expired",
        "rejected",
        "superseded",
    }:
        return False
    if item.quality not in {DataQuality.VALID, DataQuality.WARNING}:
        return False
    verification = str(
        metadata.get("verification_status")
        or metadata.get("verification")
        or ""
    ).strip().lower()
    if verification in _NEWS_UNVERIFIED:
        return False
    if verification in _NEWS_VERIFIED:
        return True
    explicit = metadata.get("verified")
    if isinstance(explicit, bool):
        return explicit
    if str(explicit or "").strip().lower() in {"true", "yes", "confirmed"}:
        return True
    # Data quality says the record was parsed and validated; it does not say the
    # underlying claim was confirmed.  Without an explicit verification claim,
    # the row stays informational context and never reaches a model input.
    return False


def _news_key(item: NewsEventEvidence) -> str:
    if item.checksum:
        return "checksum:" + item.checksum
    return "headline:" + hashlib.sha256(
        "|".join(
            (
                re.sub(r"\s+", " ", item.headline.strip().lower()),
                item.source.strip().lower(),
                item.published_at.date().isoformat(),
            )
        ).encode("utf-8")
    ).hexdigest()[:24]


def _fundamental_payload(item: FundamentalSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": item.snapshot_id,
        "instrument_id": item.instrument_id,
        "fiscal_period": item.fiscal_period,
        "metrics": dict(item.metrics),
        "currency": item.currency,
        "source": item.source,
        "publication_timestamp": item.source_observed_at.isoformat(),
        "source_observed_at": item.source_observed_at.isoformat(),
        "acquisition_timestamp": item.received_at.isoformat(),
        "received_at": item.received_at.isoformat(),
        "quality": item.quality.value,
        "validation_status": item.quality.value,
        "freshness": item.freshness.value,
        "units": dict(item.provenance.get("units") or {}),
        "provenance": dict(item.provenance),
        "point_in_time": True,
    }


def _news_payload(item: NewsEventEvidence, *, verified: bool) -> dict[str, Any]:
    source_verification = (
        item.metadata.get("verification_status")
        or item.metadata.get("verification")
        or item.metadata.get("verified")
        or "unspecified"
    )
    return {
        "evidence_id": item.evidence_id,
        "instrument_ids": list(item.instrument_ids),
        "event_type": item.event_type,
        "headline": item.headline,
        "source": item.source,
        "published_at": item.published_at.isoformat(),
        "received_at": item.received_at.isoformat(),
        "event_date": str(item.metadata.get("event_date") or "") or None,
        "quality": item.quality.value,
        "validation_status": item.quality.value,
        "freshness": item.freshness.value,
        "verification_status": "verified" if verified else "unverified",
        "source_verification": str(source_verification),
        "event_status": str(item.metadata.get("status") or "unspecified"),
        "verified": verified,
        "relevance": item.metadata.get("relevance"),
        "importance": item.metadata.get("importance"),
        "checksum": item.checksum,
        "source_url": item.source_url,
        "point_in_time": True,
        "model_input": verified,
    }


def _outcome_metrics(
    rows: Sequence[Any], *, small_sample_size: int = 30
) -> dict[str, Any]:
    """Per-horizon statistical evidence; never a cross-horizon blend."""
    if not rows:
        return {
            "sample_size": 0,
            "price_mae": None,
            "price_rmse": None,
            "price_bias": None,
            "percentage_price_mae": None,
            "return_mae": None,
            "return_bias": None,
            "direction_accuracy": None,
            "precision_up": None,
            "recall_up": None,
            "f1_up": None,
            "precision_down": None,
            "recall_down": None,
            "f1_down": None,
            "macro_f1": None,
            "neutral_sample_size": 0,
            "neutral_accuracy": None,
            "interval_coverage": None,
            "mean_interval_width": None,
            "reliable": False,
            "direction_counts": {},
        }
    count = len(rows)
    price_errors = [_float(item.price_error) or 0.0 for item in rows]
    abs_price = [_float(item.absolute_price_error) or 0.0 for item in rows]
    abs_return = [_float(item.absolute_return_error) or 0.0 for item in rows]
    return_errors = [_float(item.return_error) or 0.0 for item in rows]
    actionable = [
        item
        for item in rows
        if str(item.predicted_direction) in _ACTIONABLE
        and str(item.actual_direction) in _ACTIONABLE
    ]

    def class_metrics(label: str) -> dict[str, float | None]:
        predicted = [item for item in actionable if str(item.predicted_direction) == label]
        actual = [item for item in actionable if str(item.actual_direction) == label]
        correct = [item for item in predicted if str(item.actual_direction) == label]
        precision = (len(correct) / len(predicted)) if predicted else None
        recall = (len(correct) / len(actual)) if actual else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall > 0
            else None
        )
        return {"precision": precision, "recall": recall, "f1": f1}

    up = class_metrics(Direction.UP.value)
    down = class_metrics(Direction.DOWN.value)
    f1_values = [value for value in (up["f1"], down["f1"]) if value is not None]
    neutral = [item for item in actionable if str(item.predicted_direction) == Direction.NEUTRAL.value]
    neutral_correct = [
        item for item in neutral if str(item.actual_direction) == Direction.NEUTRAL.value
    ]
    return {
        "sample_size": count,
        "price_mae": sum(abs_price) / count,
        "price_rmse": math.sqrt(sum(value * value for value in price_errors) / count),
        "price_bias": sum(price_errors) / count,
        "percentage_price_mae": sum(
            _float(item.percentage_price_error) or 0.0 for item in rows
        )
        / count,
        "return_mae": sum(abs_return) / count,
        "return_bias": sum(return_errors) / count,
        "direction_accuracy": (
            sum(1 for item in rows if item.direction_correct) / count
        ),
        "precision_up": up["precision"],
        "recall_up": up["recall"],
        "f1_up": up["f1"],
        "precision_down": down["precision"],
        "recall_down": down["recall"],
        "f1_down": down["f1"],
        "macro_f1": (sum(f1_values) / len(f1_values)) if f1_values else None,
        "neutral_sample_size": len(neutral),
        "neutral_accuracy": (
            len(neutral_correct) / len(neutral) if neutral else None
        ),
        "interval_coverage": sum(1 for item in rows if item.inside_interval) / count,
        "mean_interval_width": sum(
            _float(item.interval_width) or 0.0 for item in rows
        )
        / count,
        "reliable": count >= max(1, int(small_sample_size)),
        "direction_counts": dict(Counter(str(item.predicted_direction) for item in rows)),
    }


class HorizonForecastService:
    """PI-10 orchestration over the existing forecast and lifecycle services."""

    def __init__(
        self,
        *,
        market_data: Any,
        ensemble: Any,
        prediction: Any,
        clock: Callable[[], datetime],
        news_provider: Callable[[str], Any] | None = None,
        small_sample_size: int = 30,
    ) -> None:
        self._market_data = market_data
        self._ensemble = ensemble
        self._prediction = prediction
        self._clock = clock
        self._news_provider = news_provider
        self._small_sample_size = small_sample_size
        self._generations = 0
        self._failures = 0
        self._last_error = ""

    # -- public capability surfaces -----------------------------------------

    def profiles(self) -> tuple[HorizonOutlookProfile, ...]:
        return tuple(HORIZON_OUTLOOK_PROFILES[item] for item in PI10_HORIZONS)

    def status(self) -> dict[str, Any]:
        return {
            "phase": PI10_PHASE,
            "implemented": True,
            "contract_version": OUTLOOK_CONTRACT_VERSION,
            "context_contract_version": CONTEXT_CONTRACT_VERSION,
            "profile_version": HORIZON_PROFILE_VERSION,
            "profiles": [profile.to_dict() for profile in self.profiles()],
            "profile_problems": list(validate_outlook_profiles()),
            "generations": self._generations,
            "failures": self._failures,
            "last_error": self._last_error,
            "fundamental_ingestion": (
                "reuses supplied PI-01 fundamental evidence and the existing "
                "market-data/NM-04 services; PI-10 adds no ingestion authority"
            ),
            "sector_mapping": (
                "instrument metadata (sector_instrument_id/sector_index); no "
                "sector is inferred from a free-text label"
            ),
            "benchmark_mapping": (
                "instrument metadata (benchmark_instrument_id/benchmark_index)"
            ),
            "cross_horizon": "directional comparison only; divergence is not an error",
            "authority": self._authority(),
        }

    def contract(self) -> dict[str, Any]:
        return {
            **self.status(),
            "context_inputs": {
                "fundamentals": "validated PI-01 FundamentalSnapshot evidence",
                "news_events": "validated PI-01 NewsEventEvidence / NM-04 radar rows",
                "sector": "validated instrument metadata and read-only daily bars",
                "benchmark": "validated instrument metadata and read-only daily bars",
                "peers": "validated instrument metadata and existing market-data bars",
            },
            "evaluation": {
                "interval_coverage": "per horizon",
                "price_metrics": ["MAE", "RMSE", "bias", "percentage MAE"],
                "return_metrics": ["MAE", "bias"],
                "direction_metrics": [
                    "accuracy",
                    "precision",
                    "recall",
                    "F1",
                    "neutral accuracy",
                ],
                "combined_cross_horizon_accuracy": False,
            },
            "known_limitations": [
                "Exchange calendar coverage currently ends at the verified 2026 year.",
                "Fundamentals are only available when supplied from an existing validated source; PI-10 does not fetch or fabricate filings.",
                "Sector and benchmark mappings must already exist in instrument metadata.",
                "No live-market accuracy claim is made from deterministic fixtures.",
            ],
        }

    # -- context assembly ----------------------------------------------------

    def _instrument(self, instrument_id: str) -> Any:
        canonical = _canonical_instrument(instrument_id)
        try:
            return self._market_data.get_instrument(canonical)
        except Exception as exc:  # noqa: BLE001 - a missing instrument is a typed refusal
            raise OutlookUnavailableError(
                f"instrument {canonical!r} is not registered: {type(exc).__name__}: {exc}"
            ) from exc

    def _verified_mapping(
        self, metadata: Mapping[str, Any], *keys: str
    ) -> tuple[str, str]:
        """A metadata mapping is usable only when its target is registered."""
        mapped, problem = _mapping_instrument(metadata, *keys)
        if problem or not mapped:
            return mapped, problem
        try:
            self._instrument(mapped)
        except OutlookUnavailableError:
            return "", f"{keys[0]} mapping {mapped!r} is not a registered instrument"
        return mapped, ""

    def _bars(
        self, instrument_id: str, *, as_of: datetime, limit: int = 500
    ) -> tuple[OhlcvBar, ...]:
        try:
            page = self._market_data.bars(
                instrument_id,
                interval="1d",
                end=as_of,
                limit=max(30, min(int(limit), 1000)),
                ascending=False,
            )
        except Exception as exc:  # noqa: BLE001 - missing context is recorded
            self._last_error = f"context bars {instrument_id}: {type(exc).__name__}: {exc}"
            return ()
        raw = getattr(page, "items", page)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        chosen: dict[str, OhlcvBar] = {}
        for bar in raw:
            if not isinstance(bar, OhlcvBar):
                continue
            if bar.instrument_id != instrument_id:
                continue
            if bar.partial or bar.observed_at > as_of:
                continue
            if bar.quality not in _ACCEPTED_BAR_QUALITY:
                continue
            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                continue
            chosen[bar.observed_at.isoformat()] = bar
        return tuple(chosen[key] for key in sorted(chosen))

    def _series_summary(
        self, role: str, instrument_id: str, bars: Sequence[OhlcvBar], *, reason: str = ""
    ) -> ContextSeries:
        if len(bars) < 2:
            return ContextSeries(
                role=role,
                instrument_id=instrument_id,
                available=False,
                bars=len(bars),
                reasons=(reason or "fewer than two validated observations",),
            )
        closes = [float(item.close) for item in bars]
        daily = indicators.returns(closes)
        return_1 = daily[-1] if daily else None
        return_20 = (closes[-1] / closes[-21] - 1.0) if len(closes) >= 21 else None
        volatility = None
        if len(closes) >= 21:
            value = indicators.historical_volatility(closes, 20)
            volatility = float(value) if value is not None else None
        sma_value = indicators.sma(closes, 20)
        sma = float(sma_value) if sma_value is not None else None
        trend = "unavailable"
        if sma is not None:
            trend = "up" if closes[-1] > sma else "down" if closes[-1] < sma else "flat"
        return ContextSeries(
            role=role,
            instrument_id=instrument_id,
            available=True,
            bars=len(bars),
            first_at=bars[0].observed_at,
            last_at=bars[-1].observed_at,
            return_1=return_1,
            return_20=return_20,
            volatility_20=volatility,
            sma_20=sma,
            trend=trend,
            reasons=(reason,) if reason else (),
        )

    async def _radar_rows(self, instrument_id: str) -> tuple[Any, ...]:
        if self._news_provider is None:
            return ()
        try:
            response = self._news_provider(instrument_id)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:  # noqa: BLE001 - optional context failure is explicit
            self._last_error = f"radar news: {type(exc).__name__}: {exc}"
            return ()
        if isinstance(response, Mapping):
            raw = response.get("items") or response.get("events") or ()
        elif isinstance(response, Sequence) and not isinstance(response, (str, bytes)):
            raw = response
        else:
            raw = ()
        return tuple(item for item in raw if isinstance(item, Mapping))

    async def assemble_context(
        self,
        instrument_id: str,
        *,
        as_of: datetime | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        include_radar: bool = True,
    ) -> _AssembledContext:
        canonical = _canonical_instrument(instrument_id)
        cutoff = _aware(as_of or self._clock(), "as_of")
        instrument = self._instrument(canonical)
        metadata = _metadata(instrument)
        notes: list[str] = []
        counts: dict[str, int] = {}

        fundamental_rows: list[FundamentalSnapshot] = []
        rejected_fundamentals = 0
        future_fundamentals = 0
        for item in fundamentals:
            try:
                snapshot = fundamental_from_input(item)
            except Exception:
                rejected_fundamentals += 1
                continue
            if snapshot.instrument_id != canonical:
                rejected_fundamentals += 1
                continue
            if snapshot.source_observed_at > cutoff or snapshot.received_at > cutoff:
                future_fundamentals += 1
                continue
            fundamental_rows.append(snapshot)
        latest_by_period: dict[str, FundamentalSnapshot] = {}
        for snapshot in sorted(
            fundamental_rows,
            key=lambda item: (item.received_at, item.source_observed_at, item.snapshot_id),
        ):
            latest_by_period[snapshot.fiscal_period] = snapshot
        eligible_fundamentals = tuple(
            item
            for item in latest_by_period.values()
            if item.quality in _ACCEPTED_FUNDAMENTAL_QUALITY
        )
        if future_fundamentals:
            notes.append(
                f"{future_fundamentals} fundamental record(s) were not public at the forecast cutoff"
            )
        if rejected_fundamentals:
            notes.append(f"{rejected_fundamentals} fundamental record(s) were invalid or for another instrument")
        if not fundamentals:
            notes.append("no fundamental evidence was supplied")
        counts["fundamentals_supplied"] = len(fundamentals)
        counts["fundamentals_point_in_time"] = len(fundamental_rows)
        counts["fundamentals_model_eligible"] = len(eligible_fundamentals)
        counts["fundamentals_dropped_future"] = future_fundamentals
        counts["fundamentals_rejected"] = rejected_fundamentals

        raw_news: list[Any] = list(news_events)
        if include_radar:
            raw_news.extend(await self._radar_rows(canonical))
        parsed_news: list[NewsEventEvidence] = []
        future_news = 0
        rejected_news = 0
        for item in raw_news:
            try:
                event = news_from_input(item)
            except Exception:
                rejected_news += 1
                continue
            if canonical not in event.instrument_ids:
                rejected_news += 1
                continue
            if event.published_at > cutoff or event.received_at > cutoff:
                future_news += 1
                continue
            parsed_news.append(event)
        grouped: dict[str, list[NewsEventEvidence]] = {}
        for event in parsed_news:
            grouped.setdefault(_news_key(event), []).append(event)
        eligible_events: list[NewsEventEvidence] = []
        duplicates = 0
        for events in grouped.values():
            duplicates += max(0, len(events) - 1)
            eligible_events.append(
                max(
                    events,
                    key=lambda item: (
                        _news_verified(item),
                        item.quality is DataQuality.VALID,
                        item.received_at,
                        item.published_at,
                        item.evidence_id,
                    ),
                )
            )
        eligible_events.sort(key=lambda item: (item.received_at, item.evidence_id))
        model_events = tuple(item for item in eligible_events if _news_verified(item))
        if future_news:
            notes.append(f"{future_news} news/event record(s) arrived after the cutoff")
        if rejected_news:
            notes.append(f"{rejected_news} news/event record(s) were invalid or unrelated")
        if duplicates:
            notes.append(f"{duplicates} duplicate news/event record(s) were deduplicated")
        if not raw_news:
            notes.append("no validated news or corporate-event evidence was available")
        counts["news_supplied"] = len(raw_news)
        counts["news_point_in_time"] = len(parsed_news)
        counts["news_deduplicated"] = len(eligible_events)
        counts["news_model_eligible"] = len(model_events)
        counts["news_unverified"] = len(eligible_events) - len(model_events)
        counts["news_dropped_future"] = future_news
        counts["news_rejected"] = rejected_news

        own_bars = self._bars(canonical, as_of=cutoff)
        own_summary = self._series_summary("instrument", canonical, own_bars)

        sector_id, sector_problem = self._verified_mapping(
            metadata,
            "sector_instrument_id",
            "sector_index",
            "sector_benchmark",
        )
        if sector_problem:
            notes.append(sector_problem)
        elif not sector_id:
            notes.append("no validated sector instrument mapping exists")
        sector_bars: tuple[OhlcvBar, ...] = ()
        sector_summary: ContextSeries | None = None
        if sector_id:
            sector_bars = self._bars(sector_id, as_of=cutoff)
            sector_summary = self._series_summary(
                "sector",
                sector_id,
                sector_bars,
                reason="" if sector_bars else "sector mapping has no validated history",
            )
            if not sector_bars:
                notes.append(f"sector context {sector_id} has no validated daily bars")

        benchmark_id, benchmark_problem = self._verified_mapping(
            metadata,
            "benchmark_instrument_id",
            "benchmark_index",
            "benchmark",
        )
        if benchmark_problem:
            notes.append(benchmark_problem)
        elif not benchmark_id:
            notes.append("no validated benchmark instrument mapping exists")
        benchmark_bars: tuple[OhlcvBar, ...] = ()
        benchmark_summary: ContextSeries | None = None
        if benchmark_id:
            benchmark_bars = self._bars(benchmark_id, as_of=cutoff)
            benchmark_summary = self._series_summary(
                "benchmark",
                benchmark_id,
                benchmark_bars,
                reason="" if benchmark_bars else "benchmark mapping has no validated history",
            )
            if not benchmark_bars:
                notes.append(f"benchmark context {benchmark_id} has no validated daily bars")

        peers: list[ContextSeries] = []
        context_bars: dict[str, Mapping[str, Sequence[OhlcvBar]]] = {}
        raw_peers = metadata.get("sector_peers") or metadata.get("peers") or ()
        if isinstance(raw_peers, str):
            raw_peers = (raw_peers,)
        if isinstance(raw_peers, Sequence) and not isinstance(raw_peers, (str, bytes)):
            for value in list(raw_peers)[:20]:
                try:
                    peer_id = _canonical_instrument(value)
                except OutlookValidationError:
                    notes.append(f"peer mapping {value!r} is not a canonical instrument id")
                    continue
                if peer_id == canonical:
                    continue
                try:
                    self._instrument(peer_id)
                except OutlookUnavailableError:
                    notes.append(f"peer mapping {value!r} is not a registered instrument")
                    continue
                bars = self._bars(peer_id, as_of=cutoff)
                summary = self._series_summary("peer", peer_id, bars)
                peers.append(summary)
                if bars:
                    context_bars[peer_id] = {"1d": bars}
        counts["peers_requested"] = len(raw_peers) if isinstance(raw_peers, Sequence) else 0
        counts["peers_available"] = sum(1 for item in peers if item.available)

        sector_relative = None
        benchmark_relative = None
        if (
            own_summary.return_20 is not None
            and sector_summary is not None
            and sector_summary.return_20 is not None
        ):
            sector_relative = own_summary.return_20 - sector_summary.return_20
        if (
            own_summary.return_20 is not None
            and benchmark_summary is not None
            and benchmark_summary.return_20 is not None
        ):
            benchmark_relative = own_summary.return_20 - benchmark_summary.return_20

        context = HorizonContext(
            instrument_id=canonical,
            as_of=cutoff,
            instrument=own_summary,
            sector=sector_summary,
            benchmark=benchmark_summary,
            peers=tuple(peers),
            fundamentals=tuple(_fundamental_payload(item) for item in eligible_fundamentals),
            events=tuple(
                _news_payload(item, verified=_news_verified(item))
                for item in eligible_events
            ),
            sector_relative_return_20=sector_relative,
            benchmark_relative_return_20=benchmark_relative,
            notes=tuple(notes),
            counts=counts,
        )
        return _AssembledContext(
            context=context,
            eligible_fundamentals=eligible_fundamentals,
            eligible_events=model_events,
            benchmark_bars={"1d": benchmark_bars} if benchmark_bars else {},
            sector_bars={"1d": sector_bars} if sector_bars else {},
            context_bars=context_bars,
        )

    # -- targets -------------------------------------------------------------

    def _expected_target(
        self, instrument_id: str, horizon: ForecastHorizon, *, as_of: datetime
    ) -> datetime:
        exchange = _canonical_instrument(instrument_id).split(":", 1)[0]
        calendar = calendar_by_mic(exchange)
        if calendar is None:
            raise OutlookUnavailableError(
                f"no verified exchange calendar exists for {exchange!r}"
            )
        try:
            return default_target(horizon=horizon, now=as_of, calendar=calendar)
        except ScheduleValidationError as exc:
            raise OutlookUnavailableError(str(exc)) from exc

    # -- generation ----------------------------------------------------------

    async def generate(
        self,
        instrument_id: str,
        horizons: Sequence[ForecastHorizon | str] | None = None,
        *,
        as_of: datetime | None = None,
        resolution: str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        feature_set_version: str = "",
        feature_set_id: str = "",
        exchange: str = "",
        currency: str = "",
        bars: Sequence[Any] | None = None,
        config: Mapping[str, Any] | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        regime: str = "",
    ) -> dict[str, Any]:
        canonical = _canonical_instrument(instrument_id)
        cutoff = _aware(as_of or self._clock(), "as_of")
        requested = tuple(
            dict.fromkeys(
                outlook_profile(item).horizon
                for item in (horizons or PI10_HORIZONS)
            )
        )
        if not requested:
            raise OutlookValidationError("at least one PI-10 horizon is required")
        if any(item not in PI10_HORIZONS for item in requested):
            raise OutlookValidationError("PI-10 generates only 1d/3d/5d/30d/90d")
        assembled = await self.assemble_context(
            canonical,
            as_of=cutoff,
            fundamentals=fundamentals,
            news_events=news_events,
        )
        context_metadata = {
            "phase": PI10_PHASE,
            "contract_version": OUTLOOK_CONTRACT_VERSION,
            "profile_version": HORIZON_PROFILE_VERSION,
            "context": assembled.context.to_dict(),
            "authorizes_action": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }
        results: list[dict[str, Any]] = []
        for horizon in requested:
            profile = outlook_profile(horizon)
            expected_target = None
            target_error = ""
            try:
                expected_target = self._expected_target(
                    canonical, horizon, as_of=cutoff
                )
            except Exception as exc:  # noqa: BLE001 - target evidence is per horizon
                target_error = str(exc)
            if expected_target is None:
                # Never let a model generate against an unverified target just
                # because its own weekday arithmetic succeeded.  Coverage is a
                # per-horizon requirement, so the other horizons still run.
                self._failures += 1
                results.append(
                    {
                        "horizon": horizon.value,
                        "family": profile.family.value,
                        "profile": profile.to_dict(),
                        "status": OutlookStatus.FAILED.value,
                        "expected_target_at": None,
                        "target_error": target_error,
                        "target_aligned": False,
                        "forecast": None,
                        "reasons": [
                            "the target session cannot be verified: "
                            + (target_error or "calendar coverage is unavailable")
                        ],
                        "context": assembled.context.to_dict(),
                        "authorizes_action": False,
                        "risk_authority": "NM-08",
                        "execution_authority": "NM-10",
                    }
                )
                continue
            try:
                body = await self._generate_one(
                    canonical,
                    profile,
                    as_of=cutoff,
                    resolution=resolution,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    feature_set_version=feature_set_version,
                    feature_set_id=feature_set_id,
                    exchange=exchange,
                    currency=currency,
                    bars=bars,
                    config=config,
                    assembled=assembled,
                    regime=regime,
                    context_metadata=context_metadata,
                )
            except Exception as exc:  # noqa: BLE001 - one horizon cannot disable another
                self._failures += 1
                self._last_error = f"{horizon.value}: {type(exc).__name__}: {exc}"
                results.append(
                    {
                        "horizon": horizon.value,
                        "family": profile.family.value,
                        "profile": profile.to_dict(),
                        "status": OutlookStatus.FAILED.value,
                        "expected_target_at": (
                            expected_target.isoformat() if expected_target else None
                        ),
                        "target_error": target_error,
                        "forecast": None,
                        "reasons": [self._last_error],
                        "context": assembled.context.to_dict(),
                        "authorizes_action": False,
                        "risk_authority": "NM-08",
                        "execution_authority": "NM-10",
                    }
                )
                continue
            status = str(body.get("status") or "")
            forecast = self._forecast_payload(body)
            if forecast is None:
                reason_rows = body.get("reasons") or body.get("failures") or ()
                status = OutlookStatus.ABSTAINED.value
                reasons = [str(item) for item in reason_rows]
            else:
                status = OutlookStatus.GENERATED.value
                reasons = [str(item) for item in body.get("reasons") or ()]
            target_at = forecast.get("forecast_target_timestamp") if forecast else None
            parsed_target = _timestamp(target_at, "forecast target") if target_at else None
            target_aligned = (
                expected_target is not None
                and parsed_target is not None
                and parsed_target == expected_target
            )
            if forecast is not None and not target_aligned:
                # A generated forecast that targets another session cannot be
                # presented as this horizon's forecast.  Keep it for diagnosis
                # but fail this horizon and leave the others untouched.
                self._failures += 1
                status = OutlookStatus.FAILED.value
                reasons = [
                    "the generated target does not match the verified target: "
                    f"{target_at!r} != {expected_target.isoformat()}",
                    *reasons,
                ]
            results.append(
                {
                    "horizon": horizon.value,
                    "family": profile.family.value,
                    "profile": profile.to_dict(),
                    "status": status,
                    "expected_target_at": (
                        expected_target.isoformat() if expected_target else None
                    ),
                    "target_error": target_error,
                    "target_aligned": target_aligned,
                    "forecast": forecast,
                    "reasons": reasons,
                    "suitability": {
                        "eligibility": body.get("eligibility") or [],
                        "weights": body.get("weights") or {},
                        "disagreement": body.get("disagreement"),
                        "calibration": (body.get("ensemble") or {}).get("calibration")
                        if isinstance(body.get("ensemble"), Mapping)
                        else None,
                    },
                    "context": assembled.context.to_dict(),
                    "authorizes_action": False,
                    "risk_authority": "NM-08",
                    "execution_authority": "NM-10",
                }
            )
        self._generations += 1
        generated = sum(
            1 for item in results if item["status"] == OutlookStatus.GENERATED.value
        )
        return {
            "ok": True,
            "phase": PI10_PHASE,
            "contract_version": OUTLOOK_CONTRACT_VERSION,
            "instrument_id": canonical,
            "as_of": cutoff.isoformat(),
            "horizons": [item["horizon"] for item in results],
            "generated": generated,
            "abstained": sum(
                1 for item in results if item["status"] == OutlookStatus.ABSTAINED.value
            ),
            "failed": sum(
                1 for item in results if item["status"] == OutlookStatus.FAILED.value
            ),
            "results": results,
            "context": assembled.context.to_dict(),
            "cross_horizon": self.cross_horizon(results),
            "partial": len(results) > 1 and 0 < generated < len(results),
            "authority": self._authority(),
        }

    async def _generate_one(
        self,
        instrument_id: str,
        profile: HorizonOutlookProfile,
        *,
        as_of: datetime,
        resolution: str | None,
        dataset_id: str,
        dataset_version: str,
        feature_set_version: str,
        feature_set_id: str,
        exchange: str,
        currency: str,
        bars: Sequence[Any] | None,
        config: Mapping[str, Any] | None,
        assembled: _AssembledContext,
        regime: str,
        context_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        metadata = {
            "pi10": {
                **dict(context_metadata),
                "horizon_family": profile.family.value,
                "target_basis": profile.target_basis.value,
                "target_count": profile.target_count,
                "feature_profile": profile.feature_profile.value,
            }
        }
        response = self._ensemble.generate(
            instrument_id,
            profile.horizon.value,
            as_of=as_of,
            resolution=resolution,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            feature_set_version=feature_set_version or "unspecified",
            feature_set_id=feature_set_id,
            exchange=exchange,
            currency=currency,
            bars=bars,
            config=config,
            fundamentals=assembled.eligible_fundamentals,
            news_events=assembled.eligible_events,
            regime=regime,
            benchmark_bars=assembled.benchmark_bars,
            sector_bars=assembled.sector_bars,
            context_bars=assembled.context_bars,
            forecast_metadata=metadata,
        )
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, Mapping):
            raise OutlookValidationError("the forecast service returned a non-mapping response")
        return dict(response)

    @staticmethod
    def _forecast_payload(body: Mapping[str, Any]) -> dict[str, Any] | None:
        nested = body.get("forecast")
        if isinstance(nested, Mapping) and nested.get("predicted_price") is not None:
            return dict(nested)
        if body.get("predicted_price") is not None:
            return dict(body)
        return None

    # -- reads and comparisons ----------------------------------------------

    def _latest_ensemble(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        *,
        as_of: datetime | None = None,
    ) -> ForecastOutput | None:
        try:
            rows = self._prediction.store.list_forecasts(
                instrument_id=instrument_id, horizon=horizon, limit=200
            )
        except Exception as exc:  # noqa: BLE001 - read failure remains visible
            self._last_error = f"forecast read: {type(exc).__name__}: {exc}"
            return None
        for row in rows:
            if as_of is not None and row.generated_at > as_of:
                # A read "as of" a cutoff cannot see a later version.
                continue
            metadata = dict(row.metadata or {})
            if (
                row.model_id == "ensemble"
                or row.forecast_id.startswith(("ens_", "ensfb_"))
                or metadata.get("phase") == "PI-08"
            ):
                return cast(ForecastOutput, row)
        return None

    def latest(
        self, instrument_id: str, *, as_of: datetime | None = None
    ) -> dict[str, Any]:
        canonical = _canonical_instrument(instrument_id)
        cutoff = _aware(as_of or self._clock(), "as_of")
        results: list[dict[str, Any]] = []
        for horizon in PI10_HORIZONS:
            profile = outlook_profile(horizon)
            forecast = self._latest_ensemble(canonical, horizon, as_of=cutoff)
            expected_target = None
            target_error = ""
            try:
                target_reference = (
                    forecast.generated_at if forecast is not None else cutoff
                )
                expected_target = self._expected_target(
                    canonical, horizon, as_of=target_reference
                )
            except Exception as exc:  # noqa: BLE001
                target_error = str(exc)
            if forecast is None:
                results.append(
                    {
                        "horizon": horizon.value,
                        "family": profile.family.value,
                        "profile": profile.to_dict(),
                        "status": "unavailable",
                        "expected_target_at": (
                            expected_target.isoformat() if expected_target else None
                        ),
                        "target_error": target_error,
                        "forecast": None,
                        "reasons": ["no stored ensemble forecast exists for this horizon"],
                        "uncertainty": None,
                        "context": {},
                        "authorizes_action": False,
                        "risk_authority": "NM-08",
                        "execution_authority": "NM-10",
                    }
                )
                continue
            payload = forecast.to_dict()
            pi10 = dict((forecast.metadata or {}).get("pi10") or {}) if isinstance(
                (forecast.metadata or {}).get("pi10"), Mapping
            ) else {}
            results.append(
                {
                    "horizon": horizon.value,
                    "family": profile.family.value,
                    "profile": profile.to_dict(),
                    "status": "stored",
                    "expected_target_at": (
                        expected_target.isoformat() if expected_target else None
                    ),
                    "target_error": target_error,
                    "forecast": payload,
                    "reasons": [],
                    "uncertainty": {
                        "class": payload.get("forecast_uncertainty"),
                        "width": payload.get("uncertainty_width"),
                        "lower_return": payload.get("lower_return"),
                        "upper_return": payload.get("upper_return"),
                        "probability_status": payload.get("probability_status"),
                        "calibration_status": payload.get("calibration_status"),
                    },
                    "context": dict(pi10.get("context") or {}),
                    "model_evidence": {
                        "model_id": payload.get("model_id"),
                        "model_version": payload.get("model_version"),
                        "ensemble_id": payload.get("ensemble_id"),
                        "eligibility": list((forecast.metadata or {}).get("eligibility") or []),
                        "disagreement": (forecast.metadata or {}).get("disagreement"),
                    },
                    "autonomy": self._autonomy_evidence(forecast),
                    "authorizes_action": False,
                    "risk_authority": "NM-08",
                    "execution_authority": "NM-10",
                }
            )
        existing = [item for item in results if item["forecast"] is not None]
        context = next(
            (
                dict(item.get("context") or {})
                for item in existing
                if item.get("context")
            ),
            {},
        )
        return {
            "ok": True,
            "phase": PI10_PHASE,
            "instrument_id": canonical,
            "as_of": cutoff.isoformat(),
            "available_horizons": len(existing),
            "results": results,
            "context": context,
            "cross_horizon": self.cross_horizon(results),
            "authority": self._authority(),
        }

    def context(
        self,
        instrument_id: str,
        *,
        as_of: datetime | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
    ) -> HorizonContext:
        """Synchronous market context for a read route (no radar acquisition)."""
        canonical = _canonical_instrument(instrument_id)
        cutoff = _aware(as_of or self._clock(), "as_of")
        return self._context_sync(
            canonical,
            as_of=cutoff,
            fundamentals=fundamentals,
            news_events=news_events,
        )

    def _context_sync(
        self,
        instrument_id: str,
        *,
        as_of: datetime,
        fundamentals: Sequence[Any],
        news_events: Sequence[Any],
    ) -> HorizonContext:
        # The async assembler is the one implementation.  A synchronous read
        # route supplies no awaitable news provider, so running it is safe and
        # keeps both paths' point-in-time rules identical.
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.assemble_context(
                    instrument_id,
                    as_of=as_of,
                    fundamentals=fundamentals,
                    news_events=news_events,
                    include_radar=False,
                )
            ).context
        # Inside an event loop the async route is required; creating a nested
        # loop would hide the caller's execution context.
        raise OutlookValidationError(
            "use the asynchronous PI-10 generation route for context inside an event loop"
        )

    def cross_horizon(self, results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Compare directions without forcing agreement across horizons."""
        observations: list[dict[str, Any]] = []
        for item in results:
            if str(item.get("status") or "") == OutlookStatus.FAILED.value:
                continue
            forecast = item.get("forecast")
            if not isinstance(forecast, Mapping):
                continue
            horizon = str(item.get("horizon") or forecast.get("forecast_horizon") or "")
            direction = str(
                forecast.get("predicted_direction")
                or forecast.get("direction")
                or "uncertain"
            ).lower()
            return_value = _float(forecast.get("predicted_return"))
            observations.append(
                {
                    "horizon": horizon,
                    "direction": direction,
                    "predicted_return": return_value,
                    "uncertainty": forecast.get("forecast_uncertainty")
                    or forecast.get("uncertainty"),
                    "target_at": forecast.get("forecast_target_timestamp")
                    or forecast.get("target_at"),
                    "validity": forecast.get("forecast_validity")
                    or forecast.get("validity"),
                    "data_freshness": forecast.get("data_freshness_status")
                    or forecast.get("data_freshness"),
                    "model_id": forecast.get("model_id"),
                    "model_version": forecast.get("model_version"),
                    "disagreement": forecast.get("ensemble_disagreement"),
                }
            )
        observations.sort(
            key=lambda row: (
                next(
                    (
                        profile.order
                        for profile in self.profiles()
                        if profile.horizon.value == row["horizon"]
                    ),
                    99,
                )
            )
        )
        actionable = [
            row for row in observations if row["direction"] in _ACTIONABLE
        ]
        directions = [row["direction"] for row in actionable]
        counts = dict(Counter(directions))
        alignment = "insufficient"
        if len(directions) >= 2:
            if len(set(directions)) == 1:
                alignment = "aligned"
            elif counts.get("up") and counts.get("down"):
                alignment = "divergent"
            else:
                alignment = "mixed"
        swing = [
            row
            for row in actionable
            if row["horizon"] in {"1d", "3d", "5d"}
            and row["predicted_return"] is not None
        ]
        long = [
            row
            for row in actionable
            if row["horizon"] in {"30d", "90d"}
            and row["predicted_return"] is not None
        ]
        short_mean = (
            sum(float(row["predicted_return"]) for row in swing) / len(swing)
            if swing
            else None
        )
        long_mean = (
            sum(float(row["predicted_return"]) for row in long) / len(long)
            if long
            else None
        )
        divergence = bool(
            short_mean is not None
            and long_mean is not None
            and ((short_mean > 0 and long_mean < 0) or (short_mean < 0 and long_mean > 0))
        )
        return_std = None
        returns = [
            float(row["predicted_return"])
            for row in observations
            if row["predicted_return"] is not None
        ]
        if len(returns) >= 2:
            mean = sum(returns) / len(returns)
            return_std = math.sqrt(
                sum((value - mean) ** 2 for value in returns) / len(returns)
            )
        return {
            "available_horizons": len(observations),
            "missing_horizons": [
                profile.horizon.value
                for profile in self.profiles()
                if profile.horizon.value not in {row["horizon"] for row in observations}
            ],
            "observations": observations,
            "direction_counts": counts,
            "alignment": alignment,
            "directional_disagreement": len(set(directions)) > 1,
            "short_term_expected_return": short_mean,
            "long_term_expected_return": long_mean,
            "short_long_divergence": divergence,
            "forecast_instability": {
                "predicted_return_std": return_std,
                "model_disagreement": {
                    row["horizon"]: row["disagreement"] for row in observations
                },
            },
            "important": (
                "Short-term and long-term forecasts may legitimately differ; "
                "divergence is reported, not treated as an error."
            ),
        }

    def accuracy(
        self,
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
        family: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Horizon-separated outcome metrics; no all-horizon accuracy number."""
        canonical = _canonical_instrument(instrument_id) if instrument_id else None
        wanted_family = str(family or "").strip().lower()
        if wanted_family not in {"", "swing", "long_term", "long-term"}:
            raise OutlookValidationError("family must be swing or long_term")
        if wanted_family == "long-term":
            wanted_family = "long_term"
        selected = [
            profile
            for profile in self.profiles()
            if (not horizon or profile.horizon.value == horizon)
            and (not wanted_family or profile.family.value == wanted_family)
        ]
        if horizon and not selected:
            raise OutlookValidationError(f"PI-10 does not support accuracy horizon {horizon!r}")
        buckets: list[dict[str, Any]] = []
        for profile in selected:
            try:
                rows = self._prediction.store.forecast_outcomes(
                    instrument_id=canonical,
                    horizon=profile.horizon.value,
                    limit=max(1, min(int(limit), 10_000)),
                )
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"accuracy read: {type(exc).__name__}: {exc}"
                rows = []
            stages = {
                stage: _outcome_metrics(
                    [item for item in rows if item.stage.value == stage],
                    small_sample_size=self._small_sample_size,
                )
                for stage in ("t_minus_20", "t_minus_5", "t_minus_1")
            }
            metrics = _outcome_metrics(
                rows, small_sample_size=self._small_sample_size
            )
            buckets.append(
                {
                    "horizon": profile.horizon.value,
                    "family": profile.family.value,
                    "target_basis": profile.target_basis.value,
                    "metrics": metrics,
                    "by_stage": stages,
                    "reliability_note": (
                        "sample is below the configured small-sample threshold; "
                        "this is not evidence of model accuracy"
                        if not metrics["reliable"]
                        else "historical evaluation evidence only; not a guarantee"
                    ),
                }
            )
        return {
            "ok": True,
            "phase": PI10_PHASE,
            "instrument_id": canonical or "",
            "family": wanted_family,
            "by_horizon": buckets,
            "combined_accuracy": None,
            "combined_accuracy_refused": (
                "unrelated horizons are not merged into one accuracy number"
            ),
            "authority": self._authority(),
        }

    @staticmethod
    def _autonomy_evidence(forecast: ForecastOutput) -> dict[str, Any]:
        """Represent a PI-10 forecast through the existing decision contract.

        PI-10 does not invent a second evidence schema.  The canonical
        ``DecisionForecastEvidence`` remains the only bridge into the existing
        decision pipeline, and its ``authorizes_action`` property is false.
        """
        reasons: list[str] = []
        final_confirmation = forecast.lifecycle_stage is ConfirmationStage.FINAL
        if not final_confirmation:
            reasons.append(
                f"lifecycle stage is {forecast.lifecycle_stage.value}, not final confirmation"
            )
        if forecast.validity not in {ForecastValidity.VALID, ForecastValidity.CONFIRMED}:
            reasons.append(f"forecast validity is {forecast.validity.value}")
        if forecast.direction.value not in _ACTIONABLE:
            reasons.append(f"direction is {forecast.direction.value}")
        if forecast.data_freshness.value in {"missing", "stale", "provider_unavailable"}:
            reasons.append(f"data freshness is {forecast.data_freshness.value}")
        if forecast.p_up is None:
            reasons.append("calibrated directional probability is unavailable")
        evidence = DecisionForecastEvidence(
            evidence_id=prediction_id(
                "pev_",
                forecast.lifecycle_id,
                forecast.forecast_id,
                "",
            ),
            lifecycle_id=forecast.lifecycle_id,
            forecast_id=forecast.forecast_id,
            instrument_id=forecast.instrument_id,
            portfolio_id="",
            direction=forecast.direction,
            expected_return=forecast.predicted_return,
            uncertainty=forecast.uncertainty,
            p_up=forecast.p_up,
            p_down=forecast.p_down,
            p_neutral=forecast.p_neutral,
            reference_price=forecast.reference_price,
            reference_at=forecast.reference_at,
            target_at=forecast.target_at,
            generated_at=forecast.generated_at,
            data_observed_at=forecast.data_observed_at,
            data_freshness=forecast.data_freshness,
            final_confirmation=final_confirmation,
            eligible=not reasons,
            reasons=tuple(reasons),
            metadata={
                "outlook_phase": PI10_PHASE,
                "forecast_horizon": forecast.forecast_horizon.value,
                "source": "PI-10 outlook read surface",
                "authorizes_action": False,
            },
        )
        payload = evidence.to_dict()
        payload.update(
            {
                "eligible_as_advisory_evidence": evidence.eligible,
                "requires_existing_strategy": True,
                "requires_nm08_risk_approval": True,
                "execution_only_through_nm10": True,
            }
        )
        return payload

    @staticmethod
    def _authority() -> dict[str, Any]:
        return {
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "forecast_authorizes_action": False,
            "portfolio_mutations": False,
            "ledger_mutations": False,
            "fills_created": False,
            "orders_created": False,
            "paper_only": True,
            "new_prediction_engine": False,
            "new_forecast_store": False,
        }


__all__ = [
    "HorizonForecastService",
    "OutlookUnavailableError",
    "OutlookValidationError",
    "fundamental_from_input",
    "news_from_input",
]
