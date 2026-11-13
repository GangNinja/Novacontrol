"""Alpha Vantage adapter: an authenticated, independent equity fallback.

Alpha Vantage is the keyed provider in this phase. It is genuinely different
from Yahoo (a documented API with a key, a 25-request/day free tier, end-of-day
daily bars) and it gives the fallback/reconciliation path a second independent
equity source when a key is configured.

Two limitations are handled explicitly rather than papered over:

* **Daily data only.** The free tier's ``TIME_SERIES_DAILY`` endpoint supplies
  daily bars; this adapter does not claim intraday intervals it cannot retrieve.
* **Date-only timestamps.** ``GLOBAL_QUOTE`` reports the latest trading DAY, not
  a time. The adapter converts that date to the exchange session's close using
  the verified calendar and records ``timestamp_basis``. If no verified calendar
  exists for the instrument, the adapter REFUSES the quote rather than inventing
  a midnight timestamp.

The API key is resolved through NovaMarket's secret-reference channel at call
time. It is never stored in the adapter's capabilities, an exception message or
a log line.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote as urlquote

from novacontrol.novamarket.contracts import (
    ProviderAuthenticationError,
    ProviderInvalidResponseError,
    ProviderRateLimitedError,
    ProviderSymbolNotFoundError,
    ProviderUnsupportedIntervalError,
)
from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
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
    resolve_credential,
)

_QUERY = "https://www.alphavantage.co/query"
#: The free tier's documented request budget. The adapter counts it locally so
#: it does not discover the limit only by being refused all day.
_FREE_DAILY_LIMIT = 25
_FREE_MIN_INTERVAL_SECONDS = 12.0


class AlphaVantageAdapter(MarketDataAdapter):
    """Provider adapter for Alpha Vantage's free equity endpoints."""

    name = "alpha-vantage"

    def __init__(
        self,
        *,
        transport: HttpTransport,
        credentials_ref: str = "",
        api_key: str | None = None,
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
                asset_classes=(AssetClass.EQUITY, AssetClass.ETF),
                exchanges=("XNAS", "XNYS", "ARCX", "XASE"),
                covers_other_exchanges=False,
                intervals=(DataInterval.DAY_1,),
                supports_quotes=True,
                supports_ohlcv=True,
                supports_lookup=False,
                data_kind="delayed",
                typical_delay_seconds=900,
                requires_credentials=True,
                credential_reference=str(credentials_ref or ""),
                rate_limit_per_minute=5,
                daily_request_limit=_FREE_DAILY_LIMIT,
                historical_days=100,
                limitations=(
                    "Requires an API key supplied through credentials_ref "
                    "(env:NAME or store:NAME); without it the adapter reports an "
                    "authentication failure and no data.",
                    "Free tier: 25 requests/day and 5 requests/minute. The "
                    "adapter enforces both locally, but the counter is "
                    "process-local and resets on restart.",
                    "Daily bars only; the free TIME_SERIES_DAILY endpoint returns "
                    "the latest 100 sessions (compact output).",
                    "GLOBAL_QUOTE gives a trading DATE, not a time. The adapter "
                    "places it at the verified session close and refuses when no "
                    "calendar exists for the instrument.",
                    "Currency is taken from the registered instrument because "
                    "the free endpoints do not return it; the provenance says so.",
                    "US-listed symbols are the verified coverage; other venues "
                    "are not advertised.",
                ),
                documentation="https://www.alphavantage.co/documentation/",
            ),
            limiter=limiter or RateLimiter(_FREE_MIN_INTERVAL_SECONDS),
            retry_policy=retry_policy,
            secret_resolver=secret_resolver,
            daily_quota=daily_quota or DailyQuota(_FREE_DAILY_LIMIT),
        )
        self.credentials_ref = str(credentials_ref or "")
        self._explicit_key = str(api_key or "").strip() or None
        self._clock = clock or (lambda: datetime.now(UTC))

    def _credential(self) -> str:
        key = self._explicit_key
        if key is None:
            key = resolve_credential(self._secrets, self.credentials_ref)
        if not key:
            raise ProviderAuthenticationError(
                "no Alpha Vantage API key is available; configure "
                "credentials_ref=env:NAME or store:NAME",
                provider=self.name,
            )
        return key

    async def _query(self, function: str, params: dict[str, str]) -> dict[str, Any]:
        payload = await self._get_json(
            _QUERY,
            params={"function": function, **params, "apikey": self._credential()},
        )
        if not isinstance(payload, dict):
            raise ProviderInvalidResponseError(
                "Alpha Vantage returned a non-object response", provider=self.name
            )
        self._raise_provider_message(payload)
        return payload

    def _raise_provider_message(self, payload: dict[str, Any]) -> None:
        error = payload.get("Error Message")
        if error:
            # Do not echo the provider body: it is untrusted text and can
            # contain the request URL. The category is enough for the caller.
            raise ProviderSymbolNotFoundError(
                "Alpha Vantage rejected the symbol or function",
                provider=self.name,
            )
        note = str(payload.get("Note") or payload.get("Information") or "")
        lowered = note.lower()
        if note:
            if "rate limit" in lowered or "per day" in lowered or "per minute" in lowered:
                raise ProviderRateLimitedError(
                    "Alpha Vantage's free request limit refused the call",
                    provider=self.name,
                )
            if "apikey" in lowered or "api key" in lowered or "premium" in lowered:
                raise ProviderAuthenticationError(
                    "Alpha Vantage rejected the API key or requires a paid plan",
                    provider=self.name,
                )
            raise ProviderInvalidResponseError(
                "Alpha Vantage returned an informational refusal",
                provider=self.name,
            )

    async def fetch_quote(self, instrument: Any) -> ProviderQuote:
        symbol = instrument.provider_symbol(self.name)
        payload = await self._query("GLOBAL_QUOTE", {"symbol": symbol})
        row = payload.get("Global Quote")
        if not isinstance(row, dict) or not row:
            raise ProviderSymbolNotFoundError(
                f"Alpha Vantage returned no quote for {symbol!r}", provider=self.name
            )
        price = row.get("05. price")
        if price in (None, ""):
            raise ProviderInvalidResponseError(
                "Alpha Vantage returned no price", provider=self.name
            )
        latest_day = _parse_date(row.get("07. latest trading day"))
        observed_at = self._session_close(instrument, latest_day)
        return ProviderQuote(
            price=price,
            currency=instrument.currency,
            observed_at=observed_at,
            source_timezone=instrument.timezone,
            provider_metadata={
                "symbol": symbol,
                "latest_trading_day": latest_day.isoformat(),
                "timestamp_basis": (
                    "session close derived from provider's latest trading day"
                ),
                "currency_source": "registered instrument",
                "open": row.get("02. open"),
                "high": row.get("03. high"),
                "low": row.get("04. low"),
                "volume": row.get("06. volume"),
            },
            raw={
                "symbol": symbol,
                "price": price,
                "latest_trading_day": row.get("07. latest trading day"),
                "open": row.get("02. open"),
                "high": row.get("03. high"),
                "low": row.get("04. low"),
                "volume": row.get("06. volume"),
                "previous_close": row.get("08. previous close"),
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
        if canonical is not DataInterval.DAY_1:
            raise ProviderUnsupportedIntervalError(
                f"Alpha Vantage's free tier supplies daily bars, not "
                f"{canonical.value}",
                provider=self.name,
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        symbol = instrument.provider_symbol(self.name)
        payload = await self._query(
            "TIME_SERIES_DAILY",
            {"symbol": symbol, "outputsize": "compact"},
        )
        series = payload.get("Time Series (Daily)")
        if not isinstance(series, dict) or not series:
            raise ProviderSymbolNotFoundError(
                f"Alpha Vantage returned no daily series for {symbol!r}",
                provider=self.name,
            )
        bars: list[ProviderBar] = []
        for day_text, row in series.items():
            if not isinstance(row, dict):
                raise ProviderInvalidResponseError(
                    "Alpha Vantage returned a malformed daily row",
                    provider=self.name,
                )
            try:
                day = date.fromisoformat(str(day_text))
            except ValueError as exc:
                raise ProviderInvalidResponseError(
                    "Alpha Vantage returned an invalid daily date",
                    provider=self.name,
                ) from exc
            observed_at = self._session_close(instrument, day)
            if start is not None and observed_at < start.astimezone(UTC):
                continue
            if end is not None and observed_at > end.astimezone(UTC):
                continue
            bars.append(
                ProviderBar(
                    interval=canonical,
                    open=_required_decimal(row, "1. open"),
                    high=_required_decimal(row, "2. high"),
                    low=_required_decimal(row, "3. low"),
                    close=_required_decimal(row, "4. close"),
                    volume=_optional_decimal(row, "5. volume"),
                    currency=instrument.currency,
                    observed_at=observed_at,
                    source_timezone=instrument.timezone,
                    provider_metadata={
                        "symbol": symbol,
                        "trading_day": day.isoformat(),
                        "timestamp_basis": "session close derived from daily date",
                        "currency_source": "registered instrument",
                    },
                    raw={
                        "date": day_text,
                        "open": row.get("1. open"),
                        "high": row.get("2. high"),
                        "low": row.get("3. low"),
                        "close": row.get("4. close"),
                        "volume": row.get("5. volume"),
                    },
                )
            )
        if not bars:
            raise ProviderInvalidResponseError(
                "Alpha Vantage returned no daily rows in the requested range",
                provider=self.name,
            )
        bars.sort(key=lambda item: item.observed_at)
        return tuple(bars[-min(limit, 100) :])

    def _session_close(self, instrument: Any, day: date) -> datetime:
        calendar = calendar_by_mic(instrument.mic)
        if calendar is None:
            raise ProviderInvalidResponseError(
                f"Alpha Vantage reports a date but no verified calendar exists "
                f"for {instrument.mic!r}; refusing to invent a timestamp",
                provider=self.name,
            )
        bounds = calendar.session_bounds(day)
        if bounds is None:
            raise ProviderInvalidResponseError(
                f"Alpha Vantage reports {day.isoformat()} as a trading day, but "
                f"the verified {calendar.name} calendar has no session; refusing "
                "to place a price on a date the calendar says is closed",
                provider=self.name,
            )
        return bounds[1]

    async def lookup(self, provider_symbol: str) -> ProviderInstrument:
        raise ProviderInvalidResponseError(
            "Alpha Vantage's free endpoints do not provide instrument metadata",
            provider=self.name,
        )


def _required_decimal(row: dict[str, Any], key: str) -> Decimal:
    value = _optional_decimal(row, key)
    if value is None:
        raise ProviderInvalidResponseError(
            f"Alpha Vantage omitted required field {key!r}", provider="alpha-vantage"
        )
    return value


def _optional_decimal(row: dict[str, Any], key: str) -> Decimal | None:
    raw = row.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderInvalidResponseError(
            f"Alpha Vantage returned a non-numeric {key!r}", provider="alpha-vantage"
        ) from exc
    if not value.is_finite():
        raise ProviderInvalidResponseError(
            f"Alpha Vantage returned a non-finite {key!r}", provider="alpha-vantage"
        )
    return value


def _parse_date(value: object) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ProviderInvalidResponseError(
            "Alpha Vantage returned an invalid latest trading day", provider="alpha-vantage"
        ) from exc


__all__ = ["AlphaVantageAdapter"]
