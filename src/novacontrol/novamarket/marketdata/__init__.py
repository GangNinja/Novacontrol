"""NovaMarket NM-02 market-data infrastructure.

The package layers are intentional:

``models``
    canonical instruments, observations, OHLCV, quality/freshness enums.
``calendar`` / ``freshness``
    exchange sessions and asset/interval-aware freshness.
``quality``
    structural/cross-record validation and provider reconciliation.
``store``
    the durable append-only SQLite history.
``providers``
    the provider seam (Yahoo, CoinGecko, Alpha Vantage) with transport, retry,
    rate limit, quota, circuit breaker and secret resolution.
``engine``
    the one pipeline: fetch → normalize → assess → persist → reconcile →
    report → schedule.

Provider APIs are not exposed directly to any downstream phase; they consume
the canonical models and the engine's validated reads.
"""

from novacontrol.novamarket.marketdata.calendar import (
    ExchangeCalendar,
    SessionState,
    TradingSession,
    calendar_by_mic,
    calendar_contract,
    calendar_for,
    calendars,
)
from novacontrol.novamarket.marketdata.engine import (
    EventSink,
    MarketDataEngine,
    ProviderProbe,
)
from novacontrol.novamarket.marketdata.freshness import (
    FreshnessAssessment,
    FreshnessSettings,
    assess_bar,
    assess_quote,
    missing_assessment,
)
from novacontrol.novamarket.marketdata.models import (
    AppendResult,
    AppendStatus,
    AssetClass,
    CircuitState,
    DataInterval,
    DataQuality,
    Freshness,
    IngestionReport,
    Instrument,
    InstrumentIngestResult,
    OhlcvBar,
    PriceObservation,
    ProviderCapabilities,
    ProviderHealth,
    ProviderStatus,
    ReconciliationResult,
    ReconciliationStatus,
    TradingStatus,
    mic_for_exchange,
    worst_quality,
)
from novacontrol.novamarket.marketdata.quality import (
    QualityAssessment,
    QualitySettings,
    assess_bar as assess_bar_quality,
    assess_quote as assess_quote_quality,
    detect_daily_gaps,
    reconcile,
)
from novacontrol.novamarket.marketdata.settings import CacheSettings, IngestionSettings
from novacontrol.novamarket.marketdata.store import (
    BarPage,
    InstrumentPage,
    MARKET_DATA_SCHEMA_VERSION,
    MarketDataStore,
    ObservationPage,
)

__all__ = [
    "AppendResult",
    "AppendStatus",
    "AssetClass",
    "BarPage",
    "CacheSettings",
    "CircuitState",
    "DataInterval",
    "DataQuality",
    "EventSink",
    "ExchangeCalendar",
    "Freshness",
    "FreshnessAssessment",
    "FreshnessSettings",
    "IngestionReport",
    "IngestionSettings",
    "Instrument",
    "InstrumentIngestResult",
    "InstrumentPage",
    "MARKET_DATA_SCHEMA_VERSION",
    "MarketDataEngine",
    "MarketDataStore",
    "ObservationPage",
    "OhlcvBar",
    "PriceObservation",
    "ProviderCapabilities",
    "ProviderHealth",
    "ProviderProbe",
    "ProviderStatus",
    "QualityAssessment",
    "QualitySettings",
    "ReconciliationResult",
    "ReconciliationStatus",
    "SessionState",
    "TradingSession",
    "TradingStatus",
    "assess_bar",
    "assess_bar_quality",
    "assess_quote",
    "assess_quote_quality",
    "calendar_by_mic",
    "calendar_contract",
    "calendar_for",
    "calendars",
    "detect_daily_gaps",
    "mic_for_exchange",
    "missing_assessment",
    "reconcile",
    "worst_quality",
]
