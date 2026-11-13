"""Provider adapters for NM-02 market data.

The public seam is :class:`~novacontrol.novamarket.marketdata.providers.base.MarketDataAdapter`.
Yahoo, CoinGecko and Alpha Vantage are implementations; the registry orders them
by configuration and the engine owns fallback, validation and persistence.
"""

from novacontrol.novamarket.marketdata.providers.alpha_vantage import (
    AlphaVantageAdapter,
)
from novacontrol.novamarket.marketdata.providers.base import (
    CircuitBreaker,
    DailyQuota,
    EnvironmentSecretResolver,
    HttpTransport,
    MarketDataAdapter,
    ProviderBar,
    ProviderInstrument,
    ProviderQuote,
    RateLimiter,
    RetryPolicy,
    SecretResolver,
    UrllibTransport,
    resolve_credential,
    with_retries,
)
from novacontrol.novamarket.marketdata.providers.coingecko import CoinGeckoAdapter
from novacontrol.novamarket.marketdata.providers.registry import (
    ProviderRegistry,
    build_adapters,
)
from novacontrol.novamarket.marketdata.providers.yahoo import YahooAdapter

__all__ = [
    "AlphaVantageAdapter",
    "CircuitBreaker",
    "CoinGeckoAdapter",
    "DailyQuota",
    "EnvironmentSecretResolver",
    "HttpTransport",
    "MarketDataAdapter",
    "ProviderBar",
    "ProviderInstrument",
    "ProviderQuote",
    "ProviderRegistry",
    "RateLimiter",
    "RetryPolicy",
    "SecretResolver",
    "UrllibTransport",
    "YahooAdapter",
    "build_adapters",
    "resolve_credential",
    "with_retries",
]
