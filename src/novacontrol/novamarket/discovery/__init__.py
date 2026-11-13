"""NM-05 — autonomous asset discovery, evaluation and watchlists.

The package composes NM-02's canonical instruments and market data with NM-04's
evidenced events. It owns the discovery decision only: a candidate score, an
explainable factor breakdown, and persistent watchlist membership with a
history. Nothing here can place an order or allocate capital.

Reachable through ``NovaMarketService`` (``/market/discovery*`` and
``/market/watchlists*``) and the ``novamarket.asset.*`` / ``novamarket.watchlist.*``
events.
"""

from novacontrol.novamarket.discovery.engine import (
    CONFIRMED_STATES,
    DEFAULT_WATCHLISTS,
    AssetDiscoveryEngine,
)
from novacontrol.novamarket.discovery.models import (
    ENTRY_TRANSITIONS,
    METHODOLOGY_VERSION,
    CandidateEvaluation,
    CycleReport,
    DataQuality,
    DiscoveryPolicy,
    DiscoveryReason,
    EntryStatus,
    ScoreFactor,
    TriggerKind,
    UniverseFilter,
    WatchlistEntry,
    WatchlistKind,
    WatchlistRecord,
    ensure_entry_transition,
    entry_id_for,
    watchlist_id_for,
)
from novacontrol.novamarket.discovery.sessions import (
    SESSION_TABLE,
    SessionState,
    session_state,
    sessions_for,
)
from novacontrol.novamarket.discovery.scoring import (
    QUALITY_CAPS,
    WEIGHTS,
    BarPoint,
    EventSignal,
    describe_model,
    score_candidate,
)
from novacontrol.novamarket.discovery.store import SCHEMA_VERSION, DiscoveryStore

__all__ = [
    "CONFIRMED_STATES",
    "DEFAULT_WATCHLISTS",
    "ENTRY_TRANSITIONS",
    "METHODOLOGY_VERSION",
    "QUALITY_CAPS",
    "SCHEMA_VERSION",
    "SESSION_TABLE",
    "SessionState",
    "WEIGHTS",
    "session_state",
    "sessions_for",
    "AssetDiscoveryEngine",
    "BarPoint",
    "CandidateEvaluation",
    "CycleReport",
    "DataQuality",
    "DiscoveryPolicy",
    "DiscoveryReason",
    "DiscoveryStore",
    "EntryStatus",
    "EventSignal",
    "ScoreFactor",
    "TriggerKind",
    "UniverseFilter",
    "WatchlistEntry",
    "WatchlistKind",
    "WatchlistRecord",
    "describe_model",
    "ensure_entry_transition",
    "entry_id_for",
    "score_candidate",
    "watchlist_id_for",
]
