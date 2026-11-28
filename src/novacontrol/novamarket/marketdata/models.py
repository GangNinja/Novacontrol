"""Canonical market-data models: instrument identity, prices, OHLCV and quality.

The models here are the ONLY shapes downstream NovaMarket phases consume. A
provider response is parsed by its adapter and normalized into these types before
anything is stored or published, so a provider's field names never leak past its
adapter.

The rules that are enforced in construction (not merely documented) are:

* an instrument is identified by ``mic:symbol`` — the same symbol on two
  exchanges is two instruments, because it is two different securities;
* every price carries its own currency, both timestamps (source and retrieval)
  and its provider;
* money is ``Decimal``; a binary float is refused at the door;
* an OHLCV bar cannot have ``high < low``, open/close outside the range, a
  negative volume, or a non-positive price — and a structurally invalid bar is
  marked ``INVALID`` while still being constructible, because rejected data must
  be retained for audit rather than thrown away;
* ``usable`` is the one answer downstream code checks. ``SUSPECT``, ``INVALID``
  and ``UNAVAILABLE`` records are never usable for a financial calculation; a
  stale or partial observation is usable only with its freshness/partial flag
  attached.

A record may be stored with an ``INVALID`` quality. That is deliberate: the
mandate requires rejected observations and their reasons to survive for audit.
What must never happen is an INVALID record being treated as a price.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from novacontrol.novamarket.contracts import MarketDataError

#: Exchange display name -> ISO 10383 market identifier code. A MIC is the
#: identity half of an instrument id; the display name is only a label.
EXCHANGE_MICS: dict[str, str] = {
    "NSE": "XNSE",
    "BSE": "XBOM",
    "NASDAQ": "XNAS",
    "NASDAQGS": "XNAS",
    "NASDAQCM": "XNAS",
    "NYSE": "XNYS",
    "NYSEARCA": "ARCX",
    "AMEX": "XASE",
    "CRYPTO": "CRYPTO",
    "FOREX": "FOREX",
    "FX": "FOREX",
    "LSE": "XLON",
    "XETRA": "XETR",
    "TSE": "XTKS",
    "HKEX": "XHKG",
    "SGX": "XSES",
}

_INSTRUMENT_ID = re.compile(r"^[A-Z0-9]{2,12}:[A-Za-z0-9^&=._/-]{1,64}$")
_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CURRENCY = re.compile(r"^[A-Z]{3,5}$")
_SKEW = timedelta(minutes=5)


class AssetClass(StrEnum):
    """What kind of instrument a record describes."""

    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    CRYPTO = "crypto"
    CURRENCY = "currency"
    MUTUAL_FUND = "mutual_fund"
    COMMODITY = "commodity"
    UNKNOWN = "unknown"


class TradingStatus(StrEnum):
    """The instrument's trading state, when the provider told us."""

    UNKNOWN = "unknown"
    OPEN = "open"
    CLOSED = "closed"
    HALTED = "halted"
    DELISTED = "delisted"


class DataInterval(StrEnum):
    """The intervals this build understands.

    A provider that cannot supply one of these says so; it is never padded with
    an approximation. ``SPOT`` is the pseudo-interval of a latest-price
    observation: it is a point in time, not a bar.
    """

    SPOT = "spot"
    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    MINUTE_20 = "20m"
    MINUTE_30 = "30m"
    HOUR_1 = "1h"
    DAY_1 = "1d"
    WEEK_1 = "1wk"
    MONTH_1 = "1mo"

    @property
    def is_intraday(self) -> bool:
        return self in (
            DataInterval.MINUTE_1,
            DataInterval.MINUTE_5,
            DataInterval.MINUTE_15,
            DataInterval.MINUTE_20,
            DataInterval.MINUTE_30,
            DataInterval.HOUR_1,
        )


class DataQuality(StrEnum):
    """Explicit quality classes; the rules live in ``quality.py``.

    ``WARNING`` is a usable observation with a flag (a large move, an unusual
    volume). ``SUSPECT`` is disagreement or an unresolved correction: retained,
    never used silently. ``STALE`` is old but well-formed; usable only when the
    consumer accepts its freshness. ``INVALID`` failed structural validation.
    """

    VALID = "valid"
    WARNING = "warning"
    STALE = "stale"
    SUSPECT = "suspect"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


#: Ordered by how much they should worry a consumer. A structural failure always
#: wins over a softer classification computed earlier in the pipeline.
_QUALITY_RANK: dict[DataQuality, int] = {
    DataQuality.VALID: 0,
    DataQuality.WARNING: 1,
    DataQuality.STALE: 2,
    DataQuality.SUSPECT: 3,
    DataQuality.INVALID: 4,
    DataQuality.UNAVAILABLE: 5,
}

#: Qualities a financial calculation may consume. STALE is allowed because a
#: previous close is legitimate for valuation; it is not a live quote, and the
#: freshness field says so. SUSPECT/INVALID/UNAVAILABLE never are.
_USABLE_QUALITIES = frozenset(
    {DataQuality.VALID, DataQuality.WARNING, DataQuality.STALE}
)


class Freshness(StrEnum):
    """Why a record is (or is not) current.

    ``CLOSED_MARKET`` is not a failure: yesterday's close is the expected price
    while an exchange is shut, and presenting it as a live quote is the error.
    ``MISSING`` and ``PROVIDER_UNAVAILABLE`` mean no record could be produced.
    """

    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    CLOSED_MARKET = "closed_market"
    MISSING = "missing"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNKNOWN = "unknown"


class ProviderStatus(StrEnum):
    """A provider's operational state, independent of a single request."""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class AppendStatus(StrEnum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class ReconciliationStatus(StrEnum):
    OK = "ok"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


def worst_quality(*values: DataQuality) -> DataQuality:
    """The most severe quality among ``values`` (VALID when none is given)."""
    return max(values, key=lambda item: _QUALITY_RANK[item], default=DataQuality.VALID)


def _coerce_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a decimal value, not bool")
    if isinstance(value, float):
        raise TypeError(
            f"{field_name} must not be a binary float; use Decimal/int/decimal "
            "string so price arithmetic stays deterministic"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field_name} is not a decimal value: {value!r}") from exc
    else:
        raise TypeError(
            f"{field_name} must be Decimal, int or a decimal string, not "
            f"{type(value).__name__}"
        )
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite, got {result}")
    return result


def _aware(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime, not {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be timezone-aware; a naive time cannot be ordered "
            "against market observations"
        )
    return value.astimezone(UTC)


def _currency(value: object) -> str:
    text = str(value or "").strip().upper()
    if not _CURRENCY.match(text):
        raise ValueError(
            f"currency must be a 3-5 letter ISO code such as INR or USD, got "
            f"{value!r}"
        )
    return text


def _timezone(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("a timezone is required (IANA name, e.g. Asia/Kolkata)")
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown IANA timezone {text!r}; use a name such as Asia/Kolkata or UTC"
        ) from exc
    return text


def _clean_reasons(reasons: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for reason in reasons:
        text = str(reason).strip()
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    return tuple(cleaned)


def _safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    return {str(key): item for key, item in value.items()}


def mic_for_exchange(exchange: str) -> str:
    """The MIC for a display exchange name; unknown names become their own MIC.

    An unknown exchange is not silently mapped to a known one: that would merge
    two different markets. The supplied name, upper-cased, is the best identity
    available and is marked as such by ``Instrument.metadata["mic_source"]``.
    """
    text = str(exchange or "").strip().upper().replace(" ", "")
    if not text:
        raise ValueError("an exchange is required")
    return EXCHANGE_MICS.get(text, text[:12])


@dataclass(frozen=True, slots=True)
class Instrument:
    """A financial instrument, identified across exchanges.

    ``instrument_id`` is ``MIC:SYMBOL``. Two exchanges listing ``RELIANCE`` are
    ``XNSE:RELIANCE`` and ``XBOM:RELIANCE``; nothing in the system may collapse
    them. ``provider_symbols`` maps each provider name to that provider's own
    identifier (Yahoo's ``RELIANCE.NS``, CoinGecko's ``bitcoin``), so symbol
    translation stays data rather than a provider-specific branch.
    """

    instrument_id: str
    symbol: str
    name: str
    asset_class: AssetClass
    exchange: str
    mic: str
    currency: str
    timezone: str
    provider_symbols: Mapping[str, str] = field(default_factory=dict)
    trading_status: TradingStatus = TradingStatus.UNKNOWN
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mic = str(self.mic or "").strip().upper()
        symbol = str(self.symbol or "").strip()
        instrument_id = str(self.instrument_id or "").strip()
        if not mic or not symbol:
            raise ValueError("an instrument needs both a MIC and a symbol")
        if not _INSTRUMENT_ID.match(instrument_id):
            raise ValueError(
                f"invalid instrument id {instrument_id!r}; expected MIC:SYMBOL "
                "such as XNSE:RELIANCE or CRYPTO:BTC-USD"
            )
        expected = f"{mic}:{symbol}"
        if instrument_id != expected:
            raise ValueError(
                f"instrument id {instrument_id!r} does not match its identity "
                f"{expected!r}; a symbol alone does not identify an instrument"
            )
        try:
            asset_class = AssetClass(self.asset_class)
        except ValueError as exc:
            raise ValueError(f"unknown asset class: {self.asset_class!r}") from exc
        try:
            trading_status = TradingStatus(self.trading_status)
        except ValueError as exc:
            raise ValueError(
                f"unknown trading status: {self.trading_status!r}"
            ) from exc
        symbols = _safe_mapping(self.provider_symbols)
        if not symbols:
            raise ValueError(
                "an instrument needs at least one provider symbol mapping "
                "(provider name -> that provider's identifier)"
            )
        cleaned_symbols: dict[str, str] = {}
        for provider, provider_symbol in symbols.items():
            key = str(provider).strip().lower()
            value = str(provider_symbol).strip()
            if not _PROVIDER_NAME.match(key):
                raise ValueError(f"invalid provider name in mapping: {provider!r}")
            if not value:
                raise ValueError(f"provider {key!r} has a blank symbol mapping")
            cleaned_symbols[key] = value
        object.__setattr__(self, "mic", mic)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "asset_class", asset_class)
        object.__setattr__(self, "trading_status", trading_status)
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "timezone", _timezone(self.timezone))
        object.__setattr__(self, "name", str(self.name or symbol).strip() or symbol)
        object.__setattr__(self, "exchange", str(self.exchange or mic).strip() or mic)
        object.__setattr__(self, "provider_symbols", cleaned_symbols)
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        name: str = "",
        exchange: str,
        asset_class: AssetClass | str = AssetClass.EQUITY,
        mic: str | None = None,
        currency: str = "INR",
        timezone: str = "UTC",
        provider_symbols: Mapping[str, str] | None = None,
        trading_status: TradingStatus | str = TradingStatus.UNKNOWN,
        metadata: Mapping[str, Any] | None = None,
    ) -> Instrument:
        """Build an instrument, deriving its MIC and id from its identity."""
        upper_symbol = str(symbol or "").strip().upper()
        resolved_mic = str(mic or mic_for_exchange(exchange)).strip().upper()
        return cls(
            instrument_id=f"{resolved_mic}:{upper_symbol}",
            symbol=upper_symbol,
            name=name or upper_symbol,
            asset_class=AssetClass(asset_class),
            exchange=exchange,
            mic=resolved_mic,
            currency=currency,
            timezone=timezone,
            provider_symbols=provider_symbols or {},
            trading_status=TradingStatus(trading_status),
            metadata=metadata or {},
        )

    def provider_symbol(self, provider: str) -> str:
        """This provider's identifier, or a refusal naming the instrument."""
        key = str(provider).strip().lower()
        try:
            return self.provider_symbols[key]
        except KeyError as exc:
            raise MarketDataError(
                f"{self.instrument_id} has no {key!r} symbol mapping; expected "
                f"one of: {', '.join(sorted(self.provider_symbols))}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "name": self.name,
            "asset_class": self.asset_class.value,
            "exchange": self.exchange,
            "mic": self.mic,
            "currency": self.currency,
            "timezone": self.timezone,
            "provider_symbols": dict(self.provider_symbols),
            "trading_status": self.trading_status.value,
            "metadata": dict(self.metadata),
        }


def _identity(*parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """One observed price, with provenance, quality and freshness.

    ``observed_at`` is the provider's source time; ``retrieved_at`` is when this
    process received it. The two are never collapsed. ``raw`` retains the
    provider's own values for audit, and ``to_dict`` withholds them by default so
    an API response is a normalized contract rather than a provider dump.
    """

    observation_id: str
    instrument_id: str
    provider: str
    price: Decimal
    currency: str
    observed_at: datetime
    retrieved_at: datetime
    source_timezone: str
    quality: DataQuality = DataQuality.VALID
    freshness: Freshness = Freshness.UNKNOWN
    reasons: tuple[str, ...] = ()
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        price = _coerce_decimal(self.price, field_name="price")
        reasons = list(self.reasons)
        quality = DataQuality(self.quality)
        if price <= 0:
            reasons.append("price is not positive")
            quality = DataQuality.INVALID
        observed = _aware(self.observed_at, field_name="observed_at")
        retrieved = _aware(self.retrieved_at, field_name="retrieved_at")
        if observed > retrieved + _SKEW:
            reasons.append("source timestamp is in the future beyond clock skew")
            quality = DataQuality.INVALID
        provider = str(self.provider or "").strip().lower()
        if not _PROVIDER_NAME.match(provider):
            raise ValueError(f"invalid provider name: {self.provider!r}")
        instrument_id = str(self.instrument_id or "").strip()
        if not _INSTRUMENT_ID.match(instrument_id):
            raise ValueError(f"invalid instrument id: {instrument_id!r}")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "retrieved_at", retrieved)
        object.__setattr__(self, "source_timezone", _timezone(self.source_timezone))
        object.__setattr__(self, "quality", worst_quality(quality))
        object.__setattr__(self, "reasons", _clean_reasons(reasons))
        object.__setattr__(
            self, "provider_metadata", _safe_mapping(self.provider_metadata)
        )
        object.__setattr__(self, "raw", _safe_mapping(self.raw))

    @classmethod
    def create(
        cls,
        *,
        instrument_id: str,
        provider: str,
        price: object,
        currency: str,
        observed_at: datetime,
        retrieved_at: datetime,
        source_timezone: str,
        quality: DataQuality = DataQuality.VALID,
        freshness: Freshness = Freshness.UNKNOWN,
        reasons: Sequence[str] = (),
        provider_metadata: Mapping[str, Any] | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> PriceObservation:
        moment = _aware(observed_at, field_name="observed_at")
        return cls(
            observation_id="po_"
            + _identity(instrument_id, provider, DataInterval.SPOT.value, moment),
            instrument_id=instrument_id,
            provider=provider,
            price=_coerce_decimal(price, field_name="price"),
            currency=currency,
            observed_at=moment,
            retrieved_at=retrieved_at,
            source_timezone=source_timezone,
            quality=quality,
            freshness=freshness,
            reasons=tuple(reasons),
            provider_metadata=provider_metadata or {},
            raw=raw or {},
        )

    @property
    def usable(self) -> bool:
        """Whether a financial calculation may consume this price."""
        return (
            self.quality in _USABLE_QUALITIES
            and self.freshness
            not in (Freshness.MISSING, Freshness.PROVIDER_UNAVAILABLE)
        )

    @property
    def live(self) -> bool:
        """Whether this is a current live quote, not a close or a delayed print."""
        return self.usable and self.freshness is Freshness.FRESH

    def age(self, *, now: datetime | None = None) -> timedelta:
        moment = _aware(now, field_name="now") if now is not None else datetime.now(UTC)
        return max(moment - self.observed_at, timedelta(0))

    def provenance(self) -> dict[str, Any]:
        """Who supplied this value, from where, and when — the audit trail."""
        return {
            "observation_id": self.observation_id,
            "instrument_id": self.instrument_id,
            "provider": self.provider,
            "observed_at": self.observed_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "source_timezone": self.source_timezone,
            "currency": self.currency,
            "quality": self.quality.value,
            "freshness": self.freshness.value,
            "live": self.live,
            "provider_metadata": dict(self.provider_metadata),
        }

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "observation_id": self.observation_id,
            "instrument_id": self.instrument_id,
            "provider": self.provider,
            "price": format(self.price, "f"),
            "currency": self.currency,
            "observed_at": self.observed_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "source_timezone": self.source_timezone,
            "quality": self.quality.value,
            "freshness": self.freshness.value,
            "usable": self.usable,
            "live": self.live,
            "reasons": list(self.reasons),
            "provider_metadata": dict(self.provider_metadata),
        }
        if include_raw:
            payload["raw"] = dict(self.raw)
        return payload


@dataclass(frozen=True, slots=True)
class OhlcvBar:
    """One OHLCV bar, validated where the provider supplied the fields.

    ``volume`` is ``None`` when the provider does not publish volume (CoinGecko's
    free OHLC response does not); it is never filled with a fabricated zero.
    ``partial`` marks a bar for a session that has not closed. A partial bar is a
    real partial bar, but it is not usable as a completed observation.
    """

    bar_id: str
    instrument_id: str
    provider: str
    interval: DataInterval
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    currency: str
    observed_at: datetime
    retrieved_at: datetime
    source_timezone: str
    volume: Decimal | None = None
    quality: DataQuality = DataQuality.VALID
    freshness: Freshness = Freshness.UNKNOWN
    partial: bool = False
    reasons: tuple[str, ...] = ()
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = {
            name: _coerce_decimal(getattr(self, name), field_name=name)
            for name in ("open", "high", "low", "close")
        }
        try:
            interval = DataInterval(self.interval)
        except ValueError as exc:
            raise ValueError(f"unknown interval: {self.interval!r}") from exc
        if interval is DataInterval.SPOT:
            raise ValueError("an OHLCV bar needs a bar interval, not SPOT")
        volume = (
            None
            if self.volume is None
            else _coerce_decimal(self.volume, field_name="volume")
        )
        reasons = list(self.reasons)
        quality = DataQuality(self.quality)
        if any(value <= 0 for value in values.values()):
            reasons.append("OHLC contains a non-positive price")
            quality = DataQuality.INVALID
        if values["high"] < values["low"]:
            reasons.append("high is below low")
            quality = DataQuality.INVALID
        if not values["low"] <= values["open"] <= values["high"]:
            reasons.append("open is outside the high-low range")
            quality = DataQuality.INVALID
        if not values["low"] <= values["close"] <= values["high"]:
            reasons.append("close is outside the high-low range")
            quality = DataQuality.INVALID
        if volume is not None and volume < 0:
            reasons.append("volume is negative")
            quality = DataQuality.INVALID
        observed = _aware(self.observed_at, field_name="observed_at")
        retrieved = _aware(self.retrieved_at, field_name="retrieved_at")
        if observed > retrieved + _SKEW:
            reasons.append("bar timestamp is in the future beyond clock skew")
            quality = DataQuality.INVALID
        provider = str(self.provider or "").strip().lower()
        if not _PROVIDER_NAME.match(provider):
            raise ValueError(f"invalid provider name: {self.provider!r}")
        instrument_id = str(self.instrument_id or "").strip()
        if not _INSTRUMENT_ID.match(instrument_id):
            raise ValueError(f"invalid instrument id: {instrument_id!r}")
        if self.partial and quality in (DataQuality.VALID, DataQuality.WARNING):
            reasons.append("bar is partial: its session has not closed")
            quality = DataQuality.WARNING
        object.__setattr__(self, "open", values["open"])
        object.__setattr__(self, "high", values["high"])
        object.__setattr__(self, "low", values["low"])
        object.__setattr__(self, "close", values["close"])
        object.__setattr__(self, "volume", volume)
        object.__setattr__(self, "interval", interval)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "retrieved_at", retrieved)
        object.__setattr__(self, "source_timezone", _timezone(self.source_timezone))
        object.__setattr__(self, "quality", worst_quality(quality))
        object.__setattr__(self, "reasons", _clean_reasons(reasons))
        object.__setattr__(
            self, "provider_metadata", _safe_mapping(self.provider_metadata)
        )
        object.__setattr__(self, "raw", _safe_mapping(self.raw))

    @classmethod
    def create(
        cls,
        *,
        instrument_id: str,
        provider: str,
        interval: DataInterval,
        open: object,
        high: object,
        low: object,
        close: object,
        currency: str,
        observed_at: datetime,
        retrieved_at: datetime,
        source_timezone: str,
        volume: object | None = None,
        quality: DataQuality = DataQuality.VALID,
        freshness: Freshness = Freshness.UNKNOWN,
        partial: bool = False,
        reasons: Sequence[str] = (),
        provider_metadata: Mapping[str, Any] | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> OhlcvBar:
        moment = _aware(observed_at, field_name="observed_at")
        canonical_interval = DataInterval(interval)
        return cls(
            bar_id="bar_"
            + _identity(instrument_id, provider, canonical_interval.value, moment),
            instrument_id=instrument_id,
            provider=provider,
            interval=canonical_interval,
            open=_coerce_decimal(open, field_name="open"),
            high=_coerce_decimal(high, field_name="high"),
            low=_coerce_decimal(low, field_name="low"),
            close=_coerce_decimal(close, field_name="close"),
            volume=(
                None
                if volume is None
                else _coerce_decimal(volume, field_name="volume")
            ),
            currency=currency,
            observed_at=moment,
            retrieved_at=retrieved_at,
            source_timezone=source_timezone,
            quality=quality,
            freshness=freshness,
            partial=partial,
            reasons=tuple(reasons),
            provider_metadata=provider_metadata or {},
            raw=raw or {},
        )

    @property
    def usable(self) -> bool:
        return (
            not self.partial
            and self.quality in _USABLE_QUALITIES
            and self.freshness
            not in (Freshness.MISSING, Freshness.PROVIDER_UNAVAILABLE)
        )

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "bar_id": self.bar_id,
            "instrument_id": self.instrument_id,
            "provider": self.provider,
            "interval": self.interval.value,
            "open": format(self.open, "f"),
            "high": format(self.high, "f"),
            "low": format(self.low, "f"),
            "close": format(self.close, "f"),
            "volume": None if self.volume is None else format(self.volume, "f"),
            "currency": self.currency,
            "observed_at": self.observed_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "source_timezone": self.source_timezone,
            "quality": self.quality.value,
            "freshness": self.freshness.value,
            "partial": self.partial,
            "usable": self.usable,
            "reasons": list(self.reasons),
            "provider_metadata": dict(self.provider_metadata),
        }
        if include_raw:
            payload["raw"] = dict(self.raw)
        return payload


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider adapter declares, and which operations were verified.

    Declarations are not advertisements: ``verified_operations`` is filled from
    successful live calls recorded in the store, so ``/market/providers`` can say
    "supports OHLCV, verified" separately from "supports OHLCV, declared but not
    yet exercised".
    """

    provider: str
    asset_classes: tuple[AssetClass, ...]
    exchanges: tuple[str, ...]
    intervals: tuple[DataInterval, ...]
    supports_quotes: bool
    supports_ohlcv: bool
    supports_lookup: bool
    data_kind: str
    typical_delay_seconds: int | None
    requires_credentials: bool
    credential_reference: str = ""
    rate_limit_per_minute: int | None = None
    daily_request_limit: int | None = None
    historical_days: int | None = None
    covers_other_exchanges: bool = False
    #: Where the values originate.  A delayed third-party API is not an
    #: exchange feed, and the gateway must never relabel it as one.
    source_class: str = "third_party_delayed"
    supports_streaming: bool = False
    #: True only when the provider supplies a timestamp distinct from the
    #: observation time (a publication timestamp).
    publication_timestamp_supported: bool = False
    limitations: tuple[str, ...] = ()
    verified_operations: tuple[str, ...] = ()
    documentation: str = ""

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        if not _PROVIDER_NAME.match(provider):
            raise ValueError(f"invalid provider name: {self.provider!r}")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self, "asset_classes", tuple(AssetClass(item) for item in self.asset_classes)
        )
        object.__setattr__(
            self, "intervals", tuple(DataInterval(item) for item in self.intervals)
        )
        if self.data_kind not in ("realtime", "delayed", "end_of_day", "unknown"):
            raise ValueError(
                "data_kind must be realtime, delayed, end_of_day or unknown"
            )
        allowed_source_classes = {
            "exchange",
            "authorized_redistribution",
            "third_party_delayed",
            "delayed",
            "cached",
            "historical",
            "scraped",
            "unknown",
        }
        if self.source_class not in allowed_source_classes:
            raise ValueError(
                "source_class must be exchange, authorized_redistribution, "
                "third_party_delayed, delayed, cached, historical, scraped or unknown"
            )
        if not isinstance(self.supports_streaming, bool):
            raise ValueError("supports_streaming must be a boolean")
        if not isinstance(self.publication_timestamp_supported, bool):
            raise ValueError("publication_timestamp_supported must be a boolean")
        if self.typical_delay_seconds is not None and self.typical_delay_seconds < 0:
            raise ValueError("typical_delay_seconds must not be negative")
        object.__setattr__(
            self, "verified_operations", tuple(sorted(set(self.verified_operations)))
        )

    def supports(self, operation: str) -> bool:
        if operation == "quote":
            return self.supports_quotes
        if operation == "ohlcv":
            return self.supports_ohlcv
        if operation == "lookup":
            return self.supports_lookup
        return False

    def verified(self, operation: str) -> bool:
        return operation in self.verified_operations

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "asset_classes": [item.value for item in self.asset_classes],
            "exchanges": list(self.exchanges),
            "covers_other_exchanges": self.covers_other_exchanges,
            "intervals": [item.value for item in self.intervals],
            "supports": {
                "quote": self.supports_quotes,
                "ohlcv": self.supports_ohlcv,
                "lookup": self.supports_lookup,
            },
            "verified_operations": list(self.verified_operations),
            "data_kind": self.data_kind,
            "source_class": self.source_class,
            "supports_streaming": self.supports_streaming,
            "publication_timestamp_supported": self.publication_timestamp_supported,
            "typical_delay_seconds": self.typical_delay_seconds,
            "requires_credentials": self.requires_credentials,
            "credential_reference": self.credential_reference,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "daily_request_limit": self.daily_request_limit,
            "historical_days": self.historical_days,
            "limitations": list(self.limitations),
            "documentation": self.documentation,
        }


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Persisted health for one provider, including verified operations."""

    provider: str
    status: ProviderStatus = ProviderStatus.UNKNOWN
    circuit_state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str = ""
    latency_ms: float | None = None
    verified_operations: tuple[str, ...] = ()
    checked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must not be negative")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency_ms must not be negative")
        object.__setattr__(
            self,
            "verified_operations",
            tuple(sorted(set(self.verified_operations))),
        )
        for name in ("last_success_at", "last_failure_at", "checked_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, field_name=name))

    @property
    def available(self) -> bool:
        return self.status in (ProviderStatus.OK, ProviderStatus.DEGRADED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "available": self.available,
            "circuit_state": self.circuit_state.value,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_failure_at": (
                self.last_failure_at.isoformat() if self.last_failure_at else None
            ),
            "last_error": self.last_error,
            "latency_ms": self.latency_ms,
            "verified_operations": list(self.verified_operations),
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


@dataclass(frozen=True, slots=True)
class AppendResult:
    """What the append-only store did with one record."""

    status: AppendStatus
    record_id: str
    detail: str = ""

    @property
    def inserted(self) -> bool:
        return self.status is AppendStatus.INSERTED

    @property
    def duplicate(self) -> bool:
        return self.status is AppendStatus.DUPLICATE

    @property
    def conflict(self) -> bool:
        return self.status is AppendStatus.CONFLICT

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "record_id": self.record_id,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """The resolved latest price for an instrument across providers.

    A conflict never becomes an averaged price. When providers disagree beyond
    the configured tolerance the result is ``CONFLICT`` with no selected
    observation, while every provider observation is retained and returned.
    """

    instrument_id: str
    status: ReconciliationStatus
    selected: PriceObservation | None
    observations: tuple[PriceObservation, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return (
            self.status is ReconciliationStatus.OK
            and self.selected is not None
            and self.selected.usable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "status": self.status.value,
            "usable": self.usable,
            "selected": self.selected.to_dict() if self.selected else None,
            "observations": [item.to_dict() for item in self.observations],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class InstrumentIngestResult:
    """One instrument's outcome in an ingestion cycle."""

    instrument_id: str
    status: str
    provider: str = ""
    detail: str = ""
    quote_id: str = ""
    quote_inserted: bool = False
    quote_duplicate: bool = False
    quote_rejected: bool = False
    bars_inserted: int = 0
    bars_duplicate: int = 0
    bars_rejected: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "status": self.status,
            "provider": self.provider,
            "detail": self.detail,
            "quote_id": self.quote_id,
            "quote_inserted": self.quote_inserted,
            "quote_duplicate": self.quote_duplicate,
            "quote_rejected": self.quote_rejected,
            "bars_inserted": self.bars_inserted,
            "bars_duplicate": self.bars_duplicate,
            "bars_rejected": self.bars_rejected,
        }


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """The measured outcome of one ingestion run — never a bare success flag."""

    started_at: datetime
    finished_at: datetime
    trigger: str
    instruments_requested: int = 0
    instruments_succeeded: int = 0
    instruments_unavailable: int = 0
    instruments_skipped: int = 0
    quotes_inserted: int = 0
    quotes_duplicate: int = 0
    quotes_rejected: int = 0
    bars_inserted: int = 0
    bars_duplicate: int = 0
    bars_rejected: int = 0
    conflicts: int = 0
    provider_attempts: int = 0
    provider_failures: int = 0
    errors: tuple[str, ...] = ()
    results: tuple[InstrumentIngestResult, ...] = ()

    @property
    def duration_ms(self) -> float:
        return max(
            (self.finished_at - self.started_at).total_seconds() * 1000.0, 0.0
        )

    @property
    def ok(self) -> bool:
        return not self.errors and self.instruments_unavailable == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": round(self.duration_ms, 3),
            "trigger": self.trigger,
            "ok": self.ok,
            "instruments_requested": self.instruments_requested,
            "instruments_succeeded": self.instruments_succeeded,
            "instruments_unavailable": self.instruments_unavailable,
            "instruments_skipped": self.instruments_skipped,
            "quotes_inserted": self.quotes_inserted,
            "quotes_duplicate": self.quotes_duplicate,
            "quotes_rejected": self.quotes_rejected,
            "bars_inserted": self.bars_inserted,
            "bars_duplicate": self.bars_duplicate,
            "bars_rejected": self.bars_rejected,
            "conflicts": self.conflicts,
            "provider_attempts": self.provider_attempts,
            "provider_failures": self.provider_failures,
            "errors": list(self.errors),
            "results": [item.to_dict() for item in self.results],
        }


@dataclass(frozen=True, slots=True)
class HistoryIngestReport:
    """The measured outcome of one bounded historical OHLCV acquisition.

    PI-02 uses this to acquire a date range through the existing provider,
    normalization, quality and store pipeline.  It is a report, not a promise:
    ``bars_received == 0`` with errors means the provider failed; an incomplete
    range is visible and can be retried incrementally.
    """

    instrument_id: str
    interval: DataInterval
    requested_start: datetime
    requested_end: datetime
    started_at: datetime
    finished_at: datetime
    provider: str = ""
    bars_received: int = 0
    bars_inserted: int = 0
    bars_duplicate: int = 0
    bars_rejected: int = 0
    bars_outside_range: int = 0
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.bars_received < 0 or self.bars_inserted < 0:
            raise ValueError("historical ingest counters must not be negative")
        object.__setattr__(self, "interval", DataInterval(self.interval))
        for name in (
            "requested_start",
            "requested_end",
            "started_at",
            "finished_at",
        ):
            object.__setattr__(self, name, _aware(getattr(self, name), field_name=name))

    @property
    def ok(self) -> bool:
        return bool(self.provider) and self.bars_received > 0

    @property
    def duration_ms(self) -> float:
        return max((self.finished_at - self.started_at).total_seconds() * 1000.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "interval": self.interval.value,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": round(self.duration_ms, 3),
            "provider": self.provider,
            "ok": self.ok,
            "bars_received": self.bars_received,
            "bars_inserted": self.bars_inserted,
            "bars_duplicate": self.bars_duplicate,
            "bars_rejected": self.bars_rejected,
            "bars_outside_range": self.bars_outside_range,
            "errors": list(self.errors),
        }


__all__ = [
    "AppendResult",
    "AppendStatus",
    "AssetClass",
    "CircuitState",
    "DataInterval",
    "DataQuality",
    "EXCHANGE_MICS",
    "Freshness",
    "HistoryIngestReport",
    "IngestionReport",
    "Instrument",
    "InstrumentIngestResult",
    "OhlcvBar",
    "PriceObservation",
    "ProviderCapabilities",
    "ProviderHealth",
    "ProviderStatus",
    "ReconciliationResult",
    "ReconciliationStatus",
    "TradingStatus",
    "mic_for_exchange",
    "worst_quality",
]
