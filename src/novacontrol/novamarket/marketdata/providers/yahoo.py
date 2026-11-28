"""Yahoo Finance chart adapter (delayed, unofficial, broad coverage).

Yahoo's chart endpoint is the broadest no-key source verified for this phase:
NSE and BSE equities (``.NS`` / ``.BO``), US and international equities, ETFs,
indices and crypto pairs. It is also an UNOFFICIAL endpoint: it is not a licensed
real-time feed, it can change without notice, and its terms do not permit
redistribution. The adapter therefore labels its data ``delayed`` and the
documentation says exactly that; nothing here presents a Yahoo quote as a live
exchange price.

The adapter keeps Yahoo's field names inside this file. It returns
``ProviderQuote`` / ``ProviderBar`` values with Decimal prices and timezone-aware
timestamps; the engine adds instrument identity, quality and freshness.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote as urlquote
from zoneinfo import ZoneInfo

from novacontrol.novamarket.contracts import (
    ProviderInvalidResponseError,
    ProviderSymbolNotFoundError,
    ProviderUnsupportedIntervalError,
)
from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    ProviderCapabilities,
    mic_for_exchange,
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

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_INTERVALS: dict[DataInterval, str] = {
    DataInterval.MINUTE_1: "1m",
    DataInterval.MINUTE_5: "5m",
    DataInterval.MINUTE_15: "15m",
    # Yahoo has no native 20m interval.  The request uses 5m and the adapter
    # combines four consecutive bars exactly; it never relabels 15m/30m data.
    DataInterval.MINUTE_20: "5m",
    DataInterval.MINUTE_30: "30m",
    DataInterval.HOUR_1: "60m",
    DataInterval.DAY_1: "1d",
    DataInterval.WEEK_1: "1wk",
    DataInterval.MONTH_1: "1mo",
}
_RANGE_FOR: dict[DataInterval, str] = {
    DataInterval.MINUTE_1: "7d",
    DataInterval.MINUTE_5: "60d",
    DataInterval.MINUTE_15: "60d",
    DataInterval.MINUTE_20: "60d",
    DataInterval.MINUTE_30: "60d",
    DataInterval.HOUR_1: "730d",
    DataInterval.DAY_1: "10y",
    DataInterval.WEEK_1: "10y",
    DataInterval.MONTH_1: "10y",
}


class YahooAdapter(MarketDataAdapter):
    """Provider adapter for the Yahoo Finance chart endpoint."""

    name = "yahoo"
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
                asset_classes=(
                    AssetClass.EQUITY,
                    AssetClass.ETF,
                    AssetClass.INDEX,
                    AssetClass.CRYPTO,
                    AssetClass.CURRENCY,
                    AssetClass.MUTUAL_FUND,
                ),
                exchanges=(
                    "XNSE",
                    "XBOM",
                    "XNAS",
                    "XNYS",
                    "ARCX",
                    "XASE",
                    "CRYPTO",
                ),
                covers_other_exchanges=True,
                intervals=tuple(_INTERVALS),
                supports_quotes=True,
                supports_ohlcv=True,
                supports_lookup=True,
                data_kind="delayed",
                typical_delay_seconds=900,
                requires_credentials=False,
                rate_limit_per_minute=60,
                daily_request_limit=None,
                historical_days=3_650,
                limitations=(
                    "Unofficial Yahoo chart endpoint, not a licensed real-time "
                    "feed; free/delayed data only. Commercial use or redistribution "
                    "requires a licensed provider.",
                    "Intraday history is limited: 1m to 7 days, 5m/15m/20m/30m to "
                    "60 days, 1h to 730 days.",
                    "20m bars are exact aggregates of four consecutive 5m Yahoo "
                    "bars; no native 20m response is claimed.",
                    "Yahoo can change or restrict the endpoint without notice.",
                    "Some indices and FX pairs have no volume; it is stored as "
                    "absent, never as zero.",
                    "NSE/BSE symbols use Yahoo's .NS/.BO suffixes through the "
                    "instrument's provider_symbols mapping.",
                ),
                documentation="https://query1.finance.yahoo.com/v8/finance/chart/",
            ),
            limiter=limiter or RateLimiter(1.0),
            retry_policy=retry_policy,
            secret_resolver=secret_resolver,
            daily_quota=daily_quota,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    async def probe(self) -> str:
        """One cheap chart read proves the endpoint and response shape work."""
        payload = await self._get_json(
            _CHART_URL.format(symbol=urlquote("RELIANCE.NS", safe="^=")),
            params={"interval": "1d", "range": "5d", "includePrePost": "false"},
        )
        self._chart_result(payload, symbol="RELIANCE.NS")
        return "chart endpoint returned a result for RELIANCE.NS"

    async def fetch_quote(self, instrument: Any) -> ProviderQuote:
        symbol = instrument.provider_symbol(self.name)
        payload = await self._get_json(
            _CHART_URL.format(symbol=urlquote(symbol, safe="^=")),
            params={
                "interval": "1d",
                "range": "5d",
                "includePrePost": "false",
                "events": "div,splits",
            },
        )
        result = self._chart_result(payload, symbol=symbol)
        meta = result.get("meta")
        if not isinstance(meta, dict):
            raise ProviderInvalidResponseError(
                "Yahoo returned no chart metadata", provider=self.name
            )
        price = meta.get("regularMarketPrice")
        observed_epoch = meta.get("regularMarketTime")
        quote_block = self._quote_block(result)
        if price is None:
            # Fall back to the last non-null close in the returned series, which
            # is a real provider value, not a synthesized one.
            closes = quote_block.get("close") or []
            timestamps = result.get("timestamp") or []
            for index in range(len(closes) - 1, -1, -1):
                if index < len(closes) and closes[index] is not None:
                    price = closes[index]
                    if index < len(timestamps):
                        observed_epoch = timestamps[index]
                    break
        if price is None or observed_epoch is None:
            raise ProviderInvalidResponseError(
                "Yahoo returned no current price for the symbol",
                provider=self.name,
            )
        currency = str(meta.get("currency") or "").strip().upper()
        if not currency:
            raise ProviderInvalidResponseError(
                "Yahoo returned no currency", provider=self.name
            )
        timezone = (
            str(meta.get("exchangeTimezoneName") or "").strip()
            or instrument.timezone
        )
        observed_at = _epoch(observed_epoch, field_name="regularMarketTime")
        previous_close = meta.get("previousClose")
        if previous_close is None:
            previous_close = meta.get("chartPreviousClose")
        return ProviderQuote(
            price=price,
            currency=currency,
            observed_at=observed_at,
            source_timezone=timezone,
            provider_metadata={
                "symbol": str(meta.get("symbol") or symbol),
                "exchange": str(meta.get("exchangeName") or ""),
                "full_exchange": str(meta.get("fullExchangeName") or ""),
                "instrument_type": str(meta.get("instrumentType") or ""),
                "market_state": str(meta.get("marketState") or ""),
                "exchange_timezone": timezone,
                "gmtoffset": meta.get("gmtoffset"),
                "previous_close": previous_close,
                "day_high": meta.get("regularMarketDayHigh"),
                "day_low": meta.get("regularMarketDayLow"),
                "day_volume": meta.get("regularMarketVolume"),
                "change_percent": meta.get("regularMarketChangePercent"),
                "timestamp_basis": "provider regularMarketTime",
            },
            raw={
                "regularMarketPrice": price,
                "regularMarketTime": observed_epoch,
                "currency": currency,
                "exchangeName": meta.get("exchangeName"),
                "instrumentType": meta.get("instrumentType"),
                "marketState": meta.get("marketState"),
                "previousClose": previous_close,
                "chartPreviousClose": meta.get("chartPreviousClose"),
                "regularMarketDayHigh": meta.get("regularMarketDayHigh"),
                "regularMarketDayLow": meta.get("regularMarketDayLow"),
                "regularMarketVolume": meta.get("regularMarketVolume"),
                "regularMarketChangePercent": meta.get("regularMarketChangePercent"),
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
        if canonical not in _INTERVALS:
            raise ProviderUnsupportedIntervalError(
                f"Yahoo does not supply {canonical.value}",
                provider=self.name,
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 5_000)
        derived_20m = canonical is DataInterval.MINUTE_20
        request_interval = _INTERVALS[canonical]
        source_interval = DataInterval.MINUTE_5 if derived_20m else canonical
        symbol = instrument.provider_symbol(self.name)
        params: dict[str, str] = {
            "interval": request_interval,
            "includePrePost": "false",
            "events": "div,splits",
        }
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValueError("start and end must be supplied together")
            if end < start:
                raise ValueError("end must not precede start")
            params["period1"] = str(int(start.timestamp()))
            params["period2"] = str(int(end.timestamp()) + 86_400)
        else:
            params["range"] = _RANGE_FOR[canonical]
        payload = await self._get_json(
            _CHART_URL.format(symbol=urlquote(symbol, safe="^=")), params=params
        )
        result = self._chart_result(payload, symbol=symbol)
        meta = result.get("meta")
        if not isinstance(meta, dict):
            raise ProviderInvalidResponseError(
                "Yahoo returned no chart metadata", provider=self.name
            )
        currency = str(meta.get("currency") or "").strip().upper()
        if not currency:
            raise ProviderInvalidResponseError(
                "Yahoo returned no currency for the series", provider=self.name
            )
        timezone = (
            str(meta.get("exchangeTimezoneName") or "").strip()
            or instrument.timezone
        )
        timestamps = result.get("timestamp")
        quote_block = self._quote_block(result)
        if not isinstance(timestamps, list) or not timestamps:
            raise ProviderInvalidResponseError(
                "Yahoo returned no OHLCV timestamps", provider=self.name
            )
        opens = quote_block.get("open") or []
        highs = quote_block.get("high") or []
        lows = quote_block.get("low") or []
        closes = quote_block.get("close") or []
        volumes = quote_block.get("volume") or []
        count = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
        bars: list[ProviderBar] = []
        for index in range(count):
            if any(
                values[index] is None
                for values in (opens, highs, lows, closes)
            ):
                # A partial/missing row is skipped, not filled with a neighbour.
                continue
            volume = volumes[index] if index < len(volumes) else None
            bars.append(
                ProviderBar(
                    interval=source_interval,
                    open=opens[index],
                    high=highs[index],
                    low=lows[index],
                    close=closes[index],
                    volume=volume,
                    currency=currency,
                    observed_at=_epoch(timestamps[index], field_name="timestamp"),
                    source_timezone=timezone,
                    provider_metadata={
                        "symbol": str(meta.get("symbol") or symbol),
                        "exchange": str(meta.get("exchangeName") or ""),
                        "interval": canonical.value if derived_20m else request_interval,
                        "yahoo_interval": request_interval,
                        "derived_from": "5m" if derived_20m else None,
                        "timestamp_basis": "bar start",
                    },
                    raw={
                        "timestamp": timestamps[index],
                        "open": opens[index],
                        "high": highs[index],
                        "low": lows[index],
                        "close": closes[index],
                        "volume": volume,
                    },
                )
            )
        if not bars:
            raise ProviderInvalidResponseError(
                "Yahoo returned no complete OHLCV rows", provider=self.name
            )
        bars.sort(key=lambda item: item.observed_at)
        if derived_20m:
            bars = _aggregate_20m(
                bars,
                timezone,
                session_open_minute=_session_open_minute(instrument, timezone),
            )
            if not bars:
                raise ProviderInvalidResponseError(
                    "Yahoo returned no complete 20m aggregates from 5m bars",
                    provider=self.name,
                )
        return tuple(bars[-limit:])

    async def lookup(self, provider_symbol: str) -> ProviderInstrument:
        symbol = str(provider_symbol or "").strip()
        if not symbol:
            raise ValueError("provider_symbol must not be blank")
        payload = await self._get_json(
            _CHART_URL.format(symbol=urlquote(symbol, safe="^=")),
            params={"interval": "1d", "range": "5d", "includePrePost": "false"},
        )
        result = self._chart_result(payload, symbol=symbol)
        meta = result.get("meta")
        if not isinstance(meta, dict):
            raise ProviderInvalidResponseError(
                "Yahoo returned no chart metadata", provider=self.name
            )
        instrument_type = str(meta.get("instrumentType") or "").upper()
        asset_class = {
            "EQUITY": AssetClass.EQUITY,
            "ETF": AssetClass.ETF,
            "INDEX": AssetClass.INDEX,
            "CRYPTOCURRENCY": AssetClass.CRYPTO,
            "CURRENCY": AssetClass.CURRENCY,
            "MUTUALFUND": AssetClass.MUTUAL_FUND,
        }.get(instrument_type, AssetClass.UNKNOWN)
        exchange = str(
            meta.get("fullExchangeName") or meta.get("exchangeName") or ""
        ).strip()
        if asset_class is AssetClass.CRYPTO:
            exchange = "CRYPTO"
        mic = mic_for_exchange(exchange or "UNKNOWN")
        currency = str(meta.get("currency") or "").strip().upper()
        if not currency:
            raise ProviderInvalidResponseError(
                "Yahoo returned no currency for the lookup", provider=self.name
            )
        timezone = str(meta.get("exchangeTimezoneName") or "UTC").strip()
        name = str(
            meta.get("longName") or meta.get("shortName") or symbol
        ).strip()
        canonical_symbol = _canonical_symbol(symbol, mic)
        return ProviderInstrument(
            provider_symbol=symbol,
            symbol=canonical_symbol,
            name=name,
            asset_class=asset_class,
            exchange=exchange or mic,
            mic=mic,
            currency=currency,
            timezone=timezone,
            metadata={
                "instrument_type": instrument_type,
                "provider_symbol": symbol,
                "exchange_name": str(meta.get("exchangeName") or ""),
            },
        )

    # -- parsing helpers ------------------------------------------------------

    def _chart_result(self, payload: Any, *, symbol: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProviderInvalidResponseError(
                "Yahoo returned a non-object response", provider=self.name
            )
        chart = payload.get("chart")
        if not isinstance(chart, dict):
            raise ProviderInvalidResponseError(
                "Yahoo returned no chart object", provider=self.name
            )
        error = chart.get("error")
        if error:
            code = str(error.get("code") or "").strip()
            description = str(error.get("description") or "").strip()
            if "not found" in code.lower() or "not found" in description.lower():
                raise ProviderSymbolNotFoundError(
                    f"Yahoo does not recognise {symbol!r}",
                    provider=self.name,
                )
            if "unauthor" in code.lower() or "unauthor" in description.lower():
                raise ProviderInvalidResponseError(
                    "Yahoo refused the chart request as unauthorised",
                    provider=self.name,
                )
            raise ProviderInvalidResponseError(
                f"Yahoo chart error {code or 'unknown'}: {description or 'no detail'}",
                provider=self.name,
            )
        result = chart.get("result")
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            raise ProviderSymbolNotFoundError(
                f"Yahoo returned no result for {symbol!r}", provider=self.name
            )
        return result[0]

    @staticmethod
    def _quote_block(result: dict[str, Any]) -> dict[str, Any]:
        indicators = result.get("indicators")
        if not isinstance(indicators, dict):
            return {}
        quotes = indicators.get("quote")
        if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
            return {}
        return quotes[0]


def _session_open_minute(instrument: Any, timezone: str) -> int | None:
    """The exchange session open on the provider's clock, when it is known."""

    mic = str(getattr(instrument, "mic", "") or "")
    calendar = calendar_by_mic(mic) if mic else None
    if calendar is None or not calendar.sessions:
        return None
    if calendar.timezone != timezone:
        # A different zone cannot be re-anchored by clock time; the caller
        # falls back to the disclosed page anchor instead.
        return None
    session = calendar.sessions[0]
    return session.open_time.hour * 60 + session.open_time.minute


def _aggregate_20m(
    bars: list[ProviderBar],
    timezone: str,
    *,
    session_open_minute: int | None = None,
) -> list[ProviderBar]:
    """Combine four consecutive 5m bars into one exact 20m bar.

    Yahoo does not publish 20m bars.  The request therefore reads 5m bars and
    groups them on the exchange's session clock.  A group is emitted only when
    all four 5m components are present, so a missing row is never filled and the
    result is not a resampled approximation.  The source timestamps are recorded
    in each bar's provenance.
    """
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        zone = ZoneInfo("UTC")
    local = [(bar.observed_at.astimezone(zone), bar) for bar in bars]
    if not local:
        return []
    # The grid belongs to the exchange session, not to whichever bar the
    # provider page happens to start with.  A page that begins at 09:20
    # must not shift every 20m bucket five minutes late.
    anchor = (
        session_open_minute
        if session_open_minute is not None
        else min(moment.hour * 60 + moment.minute for moment, _ in local)
    )
    groups: dict[tuple[date, int], list[tuple[int, ProviderBar]]] = {}
    for moment, bar in local:
        minute = moment.hour * 60 + moment.minute
        delta = minute - anchor
        if delta < 0 or delta % 5:
            continue
        bucket = delta // 20
        groups.setdefault((moment.date(), bucket), []).append(
            (delta - bucket * 20, bar)
        )
    aggregated: list[ProviderBar] = []
    for components in groups.values():
        components.sort(key=lambda item: item[0])
        if [offset for offset, _ in components] != [0, 5, 10, 15]:
            continue
        rows = [bar for _, bar in components]
        if len({bar.currency for bar in rows}) != 1:
            continue
        volume = (
            sum(
                (bar.volume for bar in rows if bar.volume is not None),
                Decimal(0),
            )
            if all(bar.volume is not None for bar in rows)
            else None
        )
        first = rows[0]
        aggregated.append(
            ProviderBar(
                interval=DataInterval.MINUTE_20,
                open=first.open,
                high=max(bar.high for bar in rows),
                low=min(bar.low for bar in rows),
                close=rows[-1].close,
                volume=volume,
                currency=first.currency,
                observed_at=first.observed_at,
                source_timezone=first.source_timezone,
                partial=any(bar.partial for bar in rows),
                provider_metadata={
                    **dict(first.provider_metadata),
                    "interval": DataInterval.MINUTE_20.value,
                    "yahoo_interval": "5m",
                    "derived_from": "5m",
                    "aggregation": "exact aggregate of four consecutive 5m bars",
                    "session_anchor": (
                        "session_open"
                        if session_open_minute is not None
                        else "page_minimum"
                    ),
                    "component_count": 4,
                    "component_timestamps": [
                        bar.observed_at.isoformat() for bar in rows
                    ],
                },
                raw={
                    "derived_from": "5m",
                    "components": [dict(bar.raw) for bar in rows],
                },
            )
        )
    aggregated.sort(key=lambda item: item.observed_at)
    return aggregated


def _epoch(value: object, *, field_name: str) -> datetime:
    if isinstance(value, bool) or value is None:
        raise ProviderInvalidResponseError(
            f"Yahoo returned an invalid {field_name}", provider="yahoo"
        )
    if isinstance(value, (int, float, Decimal)):
        seconds = int(value)
    elif isinstance(value, str):
        try:
            seconds = int(value)
        except ValueError as exc:
            raise ProviderInvalidResponseError(
                f"Yahoo returned a non-numeric {field_name}", provider="yahoo"
            ) from exc
    else:
        raise ProviderInvalidResponseError(
            f"Yahoo returned an invalid {field_name}", provider="yahoo"
        )
    if seconds <= 0:
        raise ProviderInvalidResponseError(
            f"Yahoo returned a non-positive {field_name}", provider="yahoo"
        )
    return datetime.fromtimestamp(seconds, UTC)


def _canonical_symbol(provider_symbol: str, mic: str) -> str:
    text = provider_symbol.strip()
    upper = text.upper()
    if mic == "XNSE" and upper.endswith(".NS"):
        return upper[: -len(".NS")]
    if mic == "XBOM" and upper.endswith(".BO"):
        return upper[: -len(".BO")]
    return upper


__all__ = ["YahooAdapter"]
