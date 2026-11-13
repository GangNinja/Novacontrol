"""CoinGecko adapter: an independent crypto source and fallback.

CoinGecko is the SECOND provider in this phase, not a variation of Yahoo. Its
prices are an aggregate across exchanges (not one venue's print), its free tier
updates every minute or two, and it publishes a 30-minute OHLC series without
volume. All three facts are carried in the adapter's capabilities and in every
stored observation's provenance:

* the quote currency is explicit (USD and INR are both available);
* the observation time is CoinGecko's ``last_updated_at``, not our request time;
* OHLC volume is ``None`` — the free endpoint does not publish it, so it is not
  filled with zero.

The free API has a low rate limit. The adapter waits between requests and keeps a
process-local daily ceiling; if CoinGecko still refuses, the failure is reported
as a rate limit and the circuit breaker prevents a retry storm.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote as urlquote

from novacontrol.novamarket.contracts import (
    ProviderInvalidResponseError,
    ProviderSymbolNotFoundError,
    ProviderUnsupportedIntervalError,
)
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    ProviderCapabilities,
)
from novacontrol.novamarket.marketdata.providers.base import (
    DailyQuota,
    HttpTransport,
    MarketDataAdapter,
    ProviderBar,
    ProviderInstrument,
    ProviderQuote,
    RateLimiter,
    RetryPolicy,
    SecretResolver,
)

_BASE = "https://api.coingecko.com/api/v3"
_SIMPLE_PRICE = _BASE + "/simple/price"
_COIN = _BASE + "/coins/{coin_id}"
_OHLC = _BASE + "/coins/{coin_id}/ohlc"
_PING = _BASE + "/ping"

#: CoinGecko's free OHLC endpoint publishes a 30-minute series for a one-day
#: window. It is the only interval this adapter may claim.
_SUPPORTED_INTERVALS = (DataInterval.MINUTE_30,)


class CoinGeckoAdapter(MarketDataAdapter):
    """Provider adapter for CoinGecko's free crypto endpoints."""

    name = "coingecko"
    supports_probe = True

    def __init__(
        self,
        *,
        transport: HttpTransport,
        retry_policy: RetryPolicy | None = None,
        limiter: RateLimiter | None = None,
        secret_resolver: SecretResolver | None = None,
        daily_quota: DailyQuota | None = None,
        clock: Any | None = None,
    ) -> None:
        super().__init__(
            transport=transport,
            capabilities=ProviderCapabilities(
                provider=self.name,
                asset_classes=(AssetClass.CRYPTO,),
                exchanges=("CRYPTO",),
                intervals=_SUPPORTED_INTERVALS,
                supports_quotes=True,
                supports_ohlcv=True,
                supports_lookup=True,
                data_kind="delayed",
                typical_delay_seconds=120,
                requires_credentials=False,
                rate_limit_per_minute=10,
                daily_request_limit=None,
                historical_days=2,
                limitations=(
                    "Free API; public rate limits are low and can change. The "
                    "adapter waits between calls and reports a rate-limit failure "
                    "rather than retrying indefinitely.",
                    "Free OHLC is 30-minute granularity for a recent one-day "
                    "window and publishes NO volume; volume is stored as absent.",
                    "Prices are an aggregate across exchanges, not a single "
                    "venue's print; provenance records this.",
                    "Commercial use or higher limits require CoinGecko's paid "
                    "plan and are not enabled here.",
                    "Crypto only; no equities, ETFs or indices.",
                ),
                documentation="https://docs.coingecko.com/reference/introduction",
            ),
            limiter=limiter or RateLimiter(6.0),
            retry_policy=retry_policy,
            secret_resolver=secret_resolver,
            daily_quota=daily_quota,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    async def probe(self) -> str:
        payload = await self._get_json(_PING)
        if not isinstance(payload, dict) or "gecko_says" not in payload:
            raise ProviderInvalidResponseError(
                "CoinGecko ping returned an unexpected shape", provider=self.name
            )
        return str(payload.get("gecko_says") or "ping accepted")

    async def fetch_quote(self, instrument: Any) -> ProviderQuote:
        coin_id = instrument.provider_symbol(self.name)
        currency = instrument.currency.lower()
        payload = await self._get_json(
            _SIMPLE_PRICE,
            params={
                "ids": coin_id,
                "vs_currencies": currency,
                "include_last_updated_at": "true",
            },
        )
        if not isinstance(payload, dict):
            raise ProviderInvalidResponseError(
                "CoinGecko returned a non-object quote", provider=self.name
            )
        if "error" in payload:
            raise ProviderInvalidResponseError(
                "CoinGecko rejected the quote request", provider=self.name
            )
        row = payload.get(coin_id)
        if not isinstance(row, dict):
            raise ProviderSymbolNotFoundError(
                f"CoinGecko does not recognise {coin_id!r}", provider=self.name
            )
        price = row.get(currency)
        if price is None:
            raise ProviderInvalidResponseError(
                f"CoinGecko does not quote {coin_id!r} in {currency.upper()}",
                provider=self.name,
            )
        updated = row.get("last_updated_at")
        if updated is None:
            raise ProviderInvalidResponseError(
                "CoinGecko returned no last_updated_at", provider=self.name
            )
        observed_at = _epoch(updated, field_name="last_updated_at")
        return ProviderQuote(
            price=price,
            currency=instrument.currency,
            observed_at=observed_at,
            source_timezone="UTC",
            provider_metadata={
                "coin_id": coin_id,
                "vs_currency": currency,
                "aggregated_across_exchanges": True,
                "timestamp_basis": "provider last_updated_at",
            },
            raw={
                "id": coin_id,
                "currency": currency,
                "price": price,
                "last_updated_at": updated,
            },
        )

    async def fetch_ohlcv(
        self,
        instrument: Any,
        interval: DataInterval | str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 250,
    ) -> tuple[ProviderBar, ...]:
        canonical = DataInterval(interval)
        if canonical not in _SUPPORTED_INTERVALS:
            raise ProviderUnsupportedIntervalError(
                f"CoinGecko's free OHLC endpoint supplies only 30m, not "
                f"{canonical.value}",
                provider=self.name,
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        moment = self._clock()
        if start is not None:
            if start.tzinfo is None:
                raise ValueError("start must be timezone-aware")
            if start < moment - timedelta(days=2):
                raise ProviderUnsupportedIntervalError(
                    "CoinGecko's free OHLC endpoint only supplies a recent "
                    "one-day window",
                    provider=self.name,
                )
        coin_id = instrument.provider_symbol(self.name)
        currency = instrument.currency.lower()
        payload = await self._get_json(
            _OHLC.format(coin_id=urlquote(coin_id, safe="")),
            params={"vs_currency": currency, "days": "1"},
        )
        if not isinstance(payload, list):
            raise ProviderInvalidResponseError(
                "CoinGecko returned a non-list OHLC response", provider=self.name
            )
        bars: list[ProviderBar] = []
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                raise ProviderInvalidResponseError(
                    "CoinGecko returned a malformed OHLC row", provider=self.name
                )
            observed_at = _epoch_ms(row[0])
            if start is not None and observed_at < start.astimezone(UTC):
                continue
            if end is not None and observed_at > end.astimezone(UTC):
                continue
            bars.append(
                ProviderBar(
                    interval=canonical,
                    open=row[1],
                    high=row[2],
                    low=row[3],
                    close=row[4],
                    volume=None,
                    currency=instrument.currency,
                    observed_at=observed_at,
                    source_timezone="UTC",
                    provider_metadata={
                        "coin_id": coin_id,
                        "vs_currency": currency,
                        "aggregated_across_exchanges": True,
                        "volume_available": False,
                        "timestamp_basis": "bar start (30m)",
                    },
                    raw={
                        "timestamp_ms": row[0],
                        "open": row[1],
                        "high": row[2],
                        "low": row[3],
                        "close": row[4],
                    },
                )
            )
        if not bars:
            raise ProviderInvalidResponseError(
                "CoinGecko returned no usable OHLC rows", provider=self.name
            )
        bars.sort(key=lambda item: item.observed_at)
        return tuple(bars[-min(limit, 48) :])

    async def lookup(self, provider_symbol: str) -> ProviderInstrument:
        coin_id = str(provider_symbol or "").strip().lower()
        if not coin_id:
            raise ValueError("provider_symbol must not be blank")
        payload = await self._get_json(_COIN.format(coin_id=urlquote(coin_id, safe="")))
        if not isinstance(payload, dict):
            raise ProviderInvalidResponseError(
                "CoinGecko returned a non-object coin lookup", provider=self.name
            )
        if "error" in payload:
            raise ProviderSymbolNotFoundError(
                f"CoinGecko does not recognise {coin_id!r}", provider=self.name
            )
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise ProviderSymbolNotFoundError(
                f"CoinGecko returned no symbol for {coin_id!r}", provider=self.name
            )
        return ProviderInstrument(
            provider_symbol=coin_id,
            symbol=symbol,
            name=str(payload.get("name") or symbol).strip(),
            asset_class=AssetClass.CRYPTO,
            exchange="CRYPTO",
            mic="CRYPTO",
            currency="USD",
            timezone="UTC",
            metadata={
                "coin_id": coin_id,
                "base_symbol": symbol,
                "provider_symbol": coin_id,
            },
        )


def _numeric(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ProviderInvalidResponseError(
            f"CoinGecko returned an invalid {field_name}", provider="coingecko"
        )
    if isinstance(value, (int, float, Decimal)):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value)
        except ValueError as exc:
            raise ProviderInvalidResponseError(
                f"CoinGecko returned a non-numeric {field_name}",
                provider="coingecko",
            ) from exc
    else:
        raise ProviderInvalidResponseError(
            f"CoinGecko returned an invalid {field_name}", provider="coingecko"
        )
    if number <= 0:
        raise ProviderInvalidResponseError(
            f"CoinGecko returned a non-positive {field_name}", provider="coingecko"
        )
    return number


def _epoch(value: object, *, field_name: str) -> datetime:
    return datetime.fromtimestamp(_numeric(value, field_name=field_name), UTC)


def _epoch_ms(value: object) -> datetime:
    """CoinGecko's OHLC timestamps are milliseconds; convert the scale once.

    Passing milliseconds through ``_epoch`` raised ``OSError: [Errno 22]`` on
    Windows because the value was interpreted as seconds (a year far outside the
    platform's range). Validate the number here, then divide.
    """
    milliseconds = _numeric(value, field_name="OHLC timestamp")
    return datetime.fromtimestamp(milliseconds / 1000.0, UTC)


__all__ = ["CoinGeckoAdapter"]
