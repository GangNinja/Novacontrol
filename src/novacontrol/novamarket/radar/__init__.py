"""NM-04 — the evidence-backed market event radar.

The package is a pipeline, and each stage has exactly one owner:

``sources``        declared feeds, bounded retrieval, RSS/Atom/JSON parsing
``extraction``     deterministic rules -> a structured event candidate
``dedupe``         new / update / duplicate, with the reasons
``verification``   what the evidence supports (and the conflict/retraction rules)
``classification`` explainable importance, and a separate confidence
``mapping``        evidence-based instrument and sector links
``store``          append-only event versions, evidence, mappings, transitions
``engine``         the sequencing, failure isolation and publish-after-commit

Reachable through ``NovaMarketService`` (``/market/events*``) and the
``novamarket.market_event.*`` events; nothing here imports a later phase.
"""

from novacontrol.novamarket.radar.classification import (
    FACTOR_WEIGHTS,
    VERIFICATION_WEIGHT,
    band,
    classify,
)
from novacontrol.novamarket.radar.dedupe import (
    Candidate,
    Decision,
    Existing,
    decide,
)
from novacontrol.novamarket.radar.engine import (
    EventPublisher,
    IngestReport,
    MarketEventRadar,
    SourceRefresh,
    entity_hints,
    independence_key_for,
    subject_of,
)
from novacontrol.novamarket.radar.extraction import (
    EntityHint,
    ExtractionResult,
    Match,
    extract,
    find_matches,
    resolve_occurred_at,
)
from novacontrol.novamarket.radar.mapping import (
    InstrumentView,
    map_event,
    markets_from,
    sectors_from,
)
from novacontrol.novamarket.radar.models import (
    SUBCATEGORIES,
    SUBCATEGORY_INDEX,
    STATUS_TRANSITIONS,
    UNCLASSIFIED,
    VERIFICATION_TRANSITIONS,
    Classification,
    EventCategory,
    EventMapping,
    EventQuery,
    EventStatus,
    EvidenceRecord,
    EvidenceStatus,
    ExtractedEvent,
    Factor,
    FeedFormat,
    Importance,
    MarketEvent,
    Relationship,
    SourceEntry,
    SourceHealth,
    SourceReliability,
    SourceType,
    SubcategoryDefinition,
    VerificationDecision,
    VerificationState,
    ensure_transition,
    ensure_verification_transition,
    event_key,
    expiry_deadline,
    expiry_for,
)
from novacontrol.novamarket.radar.sources import (
    DEFAULT_SOURCES,
    SOURCE_INDEX,
    EventFeedClient,
    FeedResult,
    SourceCatalog,
    parse_feed,
    parse_feed_datetime,
)
from novacontrol.novamarket.radar.store import RADAR_SCHEMA_VERSION, EventStore
from novacontrol.novamarket.radar.verification import (
    OFFICIAL_TYPES,
    VerificationEngine,
    VerificationInput,
    describe_state,
    independence_groups,
    is_expired,
    numeric_claims,
    retraction_frame,
)

__all__ = [
    "Candidate",
    "Classification",
    "DEFAULT_SOURCES",
    "Decision",
    "EntityHint",
    "EventCategory",
    "EventFeedClient",
    "EventMapping",
    "EventPublisher",
    "EventQuery",
    "EventStatus",
    "EventStore",
    "EvidenceRecord",
    "EvidenceStatus",
    "Existing",
    "ExtractedEvent",
    "ExtractionResult",
    "FACTOR_WEIGHTS",
    "FeedFormat",
    "FeedResult",
    "Importance",
    "IngestReport",
    "InstrumentView",
    "MarketEvent",
    "MarketEventRadar",
    "Match",
    "OFFICIAL_TYPES",
    "RADAR_SCHEMA_VERSION",
    "Relationship",
    "SOURCE_INDEX",
    "STATUS_TRANSITIONS",
    "SUBCATEGORIES",
    "SUBCATEGORY_INDEX",
    "SourceCatalog",
    "SourceEntry",
    "SourceHealth",
    "SourceRefresh",
    "SourceReliability",
    "SourceType",
    "SubcategoryDefinition",
    "UNCLASSIFIED",
    "VERIFICATION_TRANSITIONS",
    "VERIFICATION_WEIGHT",
    "VerificationDecision",
    "VerificationEngine",
    "VerificationInput",
    "VerificationState",
    "band",
    "classify",
    "decide",
    "describe_state",
    "ensure_transition",
    "ensure_verification_transition",
    "entity_hints",
    "event_key",
    "expiry_deadline",
    "expiry_for",
    "extract",
    "find_matches",
    "independence_groups",
    "independence_key_for",
    "is_expired",
    "map_event",
    "markets_from",
    "numeric_claims",
    "parse_feed",
    "parse_feed_datetime",
    "resolve_occurred_at",
    "retraction_frame",
    "sectors_from",
    "subject_of",
]
