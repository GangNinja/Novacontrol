"""NM-04: evidence-backed market event radar.

Every test here drives the real pipeline (parse -> extract -> verify ->
classify -> dedupe -> map -> store -> publish). Only the outbound HTTP seam is
faked, because a test must never depend on a live feed being reachable; nothing
in these tests replaces extraction, verification, classification or storage
logic with a stub.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.core.events import EVENT_PAYLOAD_FIELDS, EventType
from novacontrol.novamarket.contracts import (
    EventNotFoundError,
    EventSourceError,
    SourceMalformedError,
)
from novacontrol.novamarket.marketdata.providers.base import HttpResponse, RetryPolicy
from novacontrol.novamarket.radar import (
    DEFAULT_SOURCES,
    EventCategory,
    EventFeedClient,
    EventQuery,
    EventStore,
    EventStatus,
    EventMapping,
    ExtractionResult,
    FeedFormat,
    Importance,
    InstrumentView,
    MarketEventRadar,
    Relationship,
    SourceCatalog,
    SourceEntry,
    SourceReliability,
    SourceType,
    VerificationState,
    parse_feed,
)
from novacontrol.novamarket.radar import classification, dedupe, extraction, mapping, sources, verification
from novacontrol.novamarket.radar.models import (
    STATUS_TRANSITIONS,
    VERIFICATION_TRANSITIONS,
    EventSourceDefinition,
    EvidenceRecord,
    EvidenceStatus,
    ExtractedEvent,
    VerificationDecision,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

RATE_TITLE = "RBI holds the policy repo rate at 6.50 per cent"
RATE_SUMMARY = (
    "The Monetary Policy Committee decided to keep the policy repo rate "
    "unchanged at 6.50 per cent."
)
EARNINGS_TITLE = "Reliance Industries reports quarterly earnings"
EARNINGS_SUMMARY = (
    "Reliance Industries reported a rise in quarterly profit and revenue for "
    "the quarter ended September."
)
SECTOR_TITLE = "Steel industry faces a supply chain disruption"
SECTOR_SUMMARY = "Steel mills reported a supply chain disruption that cut output."
HALT_TITLE = "NSE announces a trading halt in the derivatives segment"
HALT_SUMMARY = "The exchange announced a trading halt after a systems incident."
WEATHER_TITLE = "Weather update for the eastern coast"
WEATHER_SUMMARY = "Light rain is expected along the eastern coast this week."


def _rss(*items: tuple[str, str, str, str, str]) -> str:
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>']
    body.append("<title>Fixture feed</title>")
    for title, summary, link, guid, published in items:
        body.append("<item>")
        body.append(f"<title>{title}</title>")
        body.append(f"<description>{summary}</description>")
        body.append(f"<link>{link}</link>")
        body.append(f"<guid>{guid}</guid>")
        body.append(f"<pubDate>{published}</pubDate>")
        body.append("</item>")
    body.append("</channel></rss>")
    return "".join(body)


def _item(
    title: str,
    summary: str,
    slug: str,
    published: str = "Mon, 02 Nov 2026 05:30:00 GMT",
    guid: str = "",
    url: str = "",
) -> tuple[str, str, str, str, str]:
    return (title, summary, url or f"https://example.test/{slug}", guid or slug, published)


def _definition(
    source_id: str = "probe_feed",
    *,
    reliability: SourceReliability = SourceReliability.OFFICIAL,
    source_type: SourceType = SourceType.GOVERNMENT,
    publisher: str = "Probe Publisher",
    market: str = "IN",
    url: str = "https://example.test/feed",
    enabled_default: bool = True,
    feed_format: FeedFormat = FeedFormat.RSS,
) -> EventSourceDefinition:
    return EventSourceDefinition(
        source_id=source_id,
        name=f"{source_id} feed",
        source_type=source_type,
        reliability=reliability,
        url=url,
        feed_format=feed_format,
        publisher=publisher,
        market=market,
        description="fixture source",
        enabled_default=enabled_default,
        verified_live=False,
    )


def _entries(body: str, definition: EventSourceDefinition) -> tuple[SourceEntry, ...]:
    return parse_feed(body, definition)


def _radar(
    definitions: tuple[EventSourceDefinition, ...] | None = None,
    *,
    instruments: tuple[InstrumentView, ...] = (),
    store: EventStore | None = None,
    client: EventFeedClient | None = None,
    clock=lambda: NOW,
) -> MarketEventRadar:
    definitions = definitions or (_definition(),)
    return MarketEventRadar(
        store or EventStore(clock=clock),
        catalog=SourceCatalog(definitions),
        client=client,
        instruments=lambda: instruments,
        clock=clock,
    )


def _ingest(
    radar: MarketEventRadar,
    body: str,
    definition: EventSourceDefinition,
) -> dict[str, int]:
    return asyncio.run(radar.ingest(definition, _entries(body, definition)))


class _Transport:
    """A transport double: only the network is faked, nothing else."""

    def __init__(self, responses: dict[str, object] | object) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, *, params=None, headers=None) -> HttpResponse:
        self.calls.append((url, dict(headers or {})))
        value = self.responses[url] if isinstance(self.responses, dict) else self.responses
        if isinstance(value, Exception):
            raise value
        status, body, extra = value  # type: ignore[misc]
        return HttpResponse(status=status, body=body, headers=extra, url=url)


class _Publisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    async def publish(self, event_type, **payload) -> None:
        self.events.append((str(event_type), payload))


def _instrument(
    instrument_id: str = "XNSE:RELIANCE",
    *,
    name: str = "Reliance Industries Limited",
    aliases: tuple[str, ...] = ("Reliance Industries", "RIL"),
    sectors: tuple[str, ...] = ("Energy", "Retail"),
    market: str = "IN",
) -> InstrumentView:
    return InstrumentView(
        instrument_id=instrument_id,
        symbol=instrument_id.split(":")[1],
        name=name,
        asset_class="equity",
        exchange=instrument_id.split(":")[0],
        market=market,
        sectors=sectors,
        aliases=aliases,
    )


# --------------------------------------------------------------------------
# Source retrieval and parsing
# --------------------------------------------------------------------------


class FeedParsingTests(unittest.TestCase):
    def test_rss_items_keep_their_own_fields_and_timestamps(self) -> None:
        body = _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/mpc-1"))
        entries = _entries(body, _definition())
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.title, RATE_TITLE)
        self.assertEqual(entry.url, "https://example.test/rbi/mpc-1")
        self.assertEqual(entry.external_id, "rbi/mpc-1")
        self.assertEqual(entry.published_at, datetime(2026, 11, 2, 5, 30, tzinfo=UTC))

    def test_atom_entries_convert_their_offset_to_utc(self) -> None:
        body = """<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>Atom fixture</title>
          <entry>
            <title>RBI holds the policy repo rate at 6.50 per cent</title>
            <summary>The committee kept the repo rate unchanged.</summary>
            <link href="https://example.test/atom/1"/>
            <id>atom-1</id>
            <published>2026-11-02T11:00:00+05:30</published>
          </entry>
        </feed>"""
        entries = _entries(body, _definition(feed_format=FeedFormat.ATOM))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].published_at, datetime(2026, 11, 2, 5, 30, tzinfo=UTC))
        self.assertEqual(entries[0].url, "https://example.test/atom/1")

    def test_a_doctype_is_refused_rather_than_expanded(self) -> None:
        body = (
            '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "boom">]>'
            "<rss><channel><item><title>&x;</title></item></channel></rss>"
        )
        with self.assertRaises(SourceMalformedError) as caught:
            _entries(body, _definition())
        self.assertTrue(caught.exception.code)

    def test_malformed_xml_is_a_typed_failure(self) -> None:
        with self.assertRaises(SourceMalformedError):
            _entries("<rss><channel><item>", _definition())

    def test_an_unparseable_date_stays_unknown(self) -> None:
        body = _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1", published="sometime"))
        entry = _entries(body, _definition())[0]
        self.assertIsNone(entry.published_at)

    def test_date_forms_are_parsed_without_guessing_a_zone(self) -> None:
        self.assertEqual(
            sources.parse_feed_datetime("Mon, 02 Nov 2026 05:30:00 +0530"),
            datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(
            sources.parse_feed_datetime("2026-11-02T05:30:00Z"),
            datetime(2026, 11, 2, 5, 30, tzinfo=UTC),
        )
        self.assertEqual(
            sources.parse_feed_datetime("2026-11-02 05:30:00"),
            datetime(2026, 11, 2, 5, 30, tzinfo=UTC),
        )
        self.assertIsNone(sources.parse_feed_datetime(""))

    def test_provenance_travels_with_the_entry_into_evidence(self) -> None:
        radar = _radar()
        definition = _definition(publisher="Reserve Bank of India")
        _ingest(
            radar,
            _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/mpc-1")),
            definition,
        )
        event_id = radar.search(EventQuery())[ "events"][0]["event_id"]
        evidence = radar.evidence(event_id)["evidence"][0]
        self.assertEqual(evidence["source_id"], definition.source_id)
        self.assertEqual(evidence["publisher"], "Reserve Bank of India")
        self.assertEqual(evidence["url"], "https://example.test/rbi/mpc-1")
        self.assertEqual(evidence["reliability"], "official")
        self.assertTrue(evidence["content_hash"])
        self.assertTrue(evidence["retrieved_at"])

    def test_the_real_sources_are_declared_with_india_news_enabled(self) -> None:
        ids = {spec.source_id for spec in DEFAULT_SOURCES}
        self.assertEqual(
            ids,
            {
                "rbi_press",
                "fed_press",
                "bls_latest",
                "sec_edgar_8k",
                "et_markets",
                "bs_markets",
            },
        )
        india_news = {"et_markets", "bs_markets"}
        for spec in DEFAULT_SOURCES:
            with self.subTest(source=spec.source_id):
                self.assertEqual(spec.enabled_default, spec.source_id in india_news)
                self.assertFalse(spec.verified_live)
                self.assertTrue(spec.url.startswith("https://"))


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_successful_fetch_returns_entries_and_a_cache_token(self) -> None:
        body = _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1"))
        transport = _Transport({"https://example.test/feed": (200, body, {"ETag": '"v1"'})})
        client = EventFeedClient(transport=transport)
        result = await client.fetch(_definition())
        self.assertEqual(result.status, 200)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.etag, '"v1"')
        self.assertFalse(result.unchanged)

    async def test_a_not_modified_answer_is_unchanged_not_empty(self) -> None:
        transport = _Transport({"https://example.test/feed": (304, "", {})})
        client = EventFeedClient(transport=transport)
        result = await client.fetch(_definition(), etag='"v1"')
        self.assertTrue(result.unchanged)
        self.assertEqual(result.entries, ())
        self.assertEqual(transport.calls[0][1]["If-None-Match"], '"v1"')

    async def test_a_rate_limited_source_raises_a_typed_error(self) -> None:
        transport = _Transport({"https://example.test/feed": (429, "", {})})
        client = EventFeedClient(transport=transport, retry=_no_retry())
        with self.assertRaises(EventSourceError) as caught:
            await client.fetch(_definition())
        self.assertTrue(caught.exception.code)
        self.assertFalse(caught.exception.retryable)

    async def test_an_unavailable_source_raises_a_retryable_error(self) -> None:
        transport = _Transport({"https://example.test/feed": (503, "", {})})
        client = EventFeedClient(transport=transport, retry=_no_retry())
        with self.assertRaises(EventSourceError) as caught:
            await client.fetch(_definition())
        self.assertTrue(caught.exception.retryable)

    async def test_a_transport_failure_never_looks_like_an_empty_feed(self) -> None:
        transport = _Transport({"https://example.test/feed": TimeoutError("timed out")})
        client = EventFeedClient(transport=transport, retry=_no_retry())
        with self.assertRaises(EventSourceError):
            await client.fetch(_definition())

    async def test_a_transient_failure_is_retried_within_the_budget(self) -> None:
        body = _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1"))
        transport = _SequenceTransport(
            [
                TimeoutError("first"),
                (503, "", {}),
                (200, body, {}),
            ]
        )
        delays: list[float] = []
        client = EventFeedClient(
            transport=transport,
            retry=RetryPolicy(max_attempts=3),
            sleeper=lambda seconds: delays.append(seconds),
        )
        result = await client.fetch(_definition())
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(delays), 2)

    async def test_the_retry_budget_is_bounded(self) -> None:
        transport = _SequenceTransport([TimeoutError("always")] * 5)
        client = EventFeedClient(
            transport=transport,
            retry=_retry_policy(max_attempts=2),
            sleeper=lambda seconds: None,
        )
        with self.assertRaises(EventSourceError):
            await client.fetch(_definition())
        self.assertEqual(len(transport.calls), 2)


def _retry_policy(*, max_attempts: int):
    from novacontrol.novamarket.marketdata.providers.base import RetryPolicy

    return RetryPolicy(
        max_attempts=max_attempts, base_delay_seconds=0.0, max_delay_seconds=0.0
    )


def _no_retry():
    return _retry_policy(max_attempts=1)


class _SequenceTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def get(self, url: str, *, params=None, headers=None) -> HttpResponse:
        self.calls.append(url)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, body, extra = value  # type: ignore[misc]
        return HttpResponse(status=status, body=body, headers=extra, url=url)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


class ExtractionTests(unittest.TestCase):
    def _extract(self, title: str, summary: str) -> ExtractionResult:
        return extraction.extract(
            SourceEntry(title=title, summary=summary, url="", published_at=None, external_id="x"),
            publisher="Probe Publisher",
        )

    def test_a_policy_rate_item_is_macroeconomic(self) -> None:
        result = self._extract(RATE_TITLE, RATE_SUMMARY)
        self.assertFalse(result.unclassified)
        self.assertEqual(result.event.category, EventCategory.MACROECONOMIC)
        self.assertEqual(result.event.subcategory, "interest_rate")
        self.assertTrue(result.event.market_wide)

    def test_an_earnings_item_is_company_specific(self) -> None:
        result = self._extract(EARNINGS_TITLE, EARNINGS_SUMMARY)
        self.assertEqual(result.event.category, EventCategory.COMPANY)
        self.assertIn("earnings", result.event.subcategory)

    def test_a_supply_disruption_is_sector_wide(self) -> None:
        result = self._extract(SECTOR_TITLE, SECTOR_SUMMARY)
        self.assertEqual(result.event.category, EventCategory.SECTOR)

    def test_a_trading_halt_is_market_structure(self) -> None:
        result = self._extract(HALT_TITLE, HALT_SUMMARY)
        self.assertEqual(result.event.category, EventCategory.MARKET_STRUCTURE)

    def test_unrelated_text_is_unclassified_and_never_stored(self) -> None:
        self.assertTrue(self._extract(WEATHER_TITLE, WEATHER_SUMMARY).unclassified)
        radar = _radar()
        counts = _ingest(
            radar, _rss(_item(WEATHER_TITLE, WEATHER_SUMMARY, "weather/1")), _definition()
        )
        self.assertEqual(counts["unclassified"], 1)
        self.assertEqual(counts["created"], 0)
        self.assertEqual(radar.search(EventQuery())["total"], 0)

    def test_a_missing_summary_falls_back_to_the_title(self) -> None:
        result = self._extract(RATE_TITLE, "")
        self.assertFalse(result.unclassified)
        self.assertEqual(result.event.description, RATE_TITLE)

    def test_occurrence_time_is_unknown_when_the_item_states_none(self) -> None:
        result = self._extract(RATE_TITLE, RATE_SUMMARY)
        self.assertIsNone(result.event.occurred_at)

    def test_an_explicit_occurrence_time_is_read_from_the_text(self) -> None:
        entry = SourceEntry(
            title="RBI holds the policy repo rate at 6.50 per cent",
            summary="The decision was announced on 2026-11-02.",
            url="",
            published_at=datetime(2026, 11, 2, 5, 30, tzinfo=UTC),
            external_id="x",
        )
        result = extraction.extract(entry, publisher="Probe Publisher")
        self.assertEqual(result.event.occurred_at, datetime(2026, 11, 2, tzinfo=UTC))


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


class VerificationTests(unittest.TestCase):
    def _single(self, reliability: SourceReliability) -> dict:
        radar = _radar()
        definition = _definition(source_id=f"src_{reliability.value}", reliability=reliability)
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), definition)
        return radar.search(EventQuery())["events"][0]

    def test_one_official_source_confirms_what_it_says(self) -> None:
        row = self._single(SourceReliability.OFFICIAL)
        self.assertEqual(row["verification"], "officially_confirmed")
        self.assertEqual(row["evidence_count"], 1)
        self.assertEqual(row["independent_sources"], 1)
        self.assertTrue(row["verification_detail"]["reasons"])

    def test_one_secondary_source_is_not_official_confirmation(self) -> None:
        row = self._single(SourceReliability.REPUTABLE)
        self.assertEqual(row["verification"], "single_source")

    def test_a_rumour_is_never_presented_as_confirmed(self) -> None:
        row = self._single(SourceReliability.SECONDARY)
        self.assertNotIn(
            row["verification"], {"officially_confirmed", "multi_source_corroborated"}
        )

    def test_independent_sources_corroborate_each_other(self) -> None:
        first = _definition(source_id="wire_a", publisher="Wire A", reliability=SourceReliability.REPUTABLE, url="https://a.example.test/feed")
        second = _definition(source_id="wire_b", publisher="Wire B", reliability=SourceReliability.REPUTABLE, url="https://b.example.test/feed")
        radar = _radar((first, second))
        _ingest(
            radar,
            _rss(_item(RATE_TITLE, RATE_SUMMARY, "a/1", url="https://a.example.test/a/1")),
            first,
        )
        counts = _ingest(
            radar,
            _rss(
                _item(
                    "RBI holds the policy repo rate at 6.50 per cent, officials say",
                    "Borrowing costs stay put: the committee left the benchmark rate at 6.50 per cent after its two-day review in Mumbai.",
                    "b/1",
                    url="https://b.example.test/b/1",
                )
            ),
            second,
        )
        self.assertEqual(counts["updated"], 1)
        row = radar.search(EventQuery(latest_only=True))["events"][0]
        self.assertEqual(row["verification"], "multi_source_corroborated")
        self.assertEqual(row["independent_sources"], 2)
        self.assertEqual(row["evidence_count"], 2)

    def test_syndicated_copies_are_not_independent_corroboration(self) -> None:
        first = _definition(source_id="syn_a", publisher="Syndicated Wire", url="https://wire.example.test/a")
        second = _definition(source_id="syn_b", publisher="Syndicated Wire", url="https://wire.example.test/b")
        radar = _radar((first, second))
        _ingest(
            radar,
            _rss(_item(RATE_TITLE, RATE_SUMMARY, "a/1", url="https://a.example.test/a/1")),
            first,
        )
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "b/1")), second)
        rows = radar.search(EventQuery(latest_only=True))["events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["independent_sources"], 1)
        self.assertNotEqual(rows[0]["verification"], "multi_source_corroborated")

    def test_conflicting_figures_are_recorded_and_kept(self) -> None:
        first = _definition(source_id="wire_a", publisher="Wire A", reliability=SourceReliability.REPUTABLE, url="https://a.example.test/feed")
        second = _definition(source_id="wire_b", publisher="Wire B", reliability=SourceReliability.REPUTABLE, url="https://b.example.test/feed")
        radar = _radar((first, second))
        _ingest(
            radar,
            _rss(_item(RATE_TITLE, RATE_SUMMARY, "a/1", url="https://a.example.test/a/1")),
            first,
        )
        _ingest(
            radar,
            _rss(
                _item(
                    "RBI holds the policy repo rate at 6.75 per cent",
                    "The Monetary Policy Committee kept the policy repo rate unchanged at 6.75 per cent.",
                    "b/1",
                    url="https://b.example.test/b/1",
                )
            ),
            second,
        )
        rows = radar.search(EventQuery(latest_only=True))["events"]
        self.assertEqual(len(rows), 1, "a conflicting report is not a second event")
        self.assertEqual(rows[0]["verification"], "conflicting_evidence")
        self.assertTrue(rows[0]["verification_detail"]["conflicts"])
        self.assertEqual(radar.evidence(rows[0]["event_id"])["total"], 2)

    def test_a_retraction_moves_the_event_to_retracted(self) -> None:
        radar = _radar()
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        _ingest(
            radar,
            _rss(
                _item(
                    "RBI withdraws the statement on the policy repo rate",
                    "The Reserve Bank of India withdrew its earlier statement about the policy repo rate.",
                    "rbi/retract",
                )
            ),
            _definition(),
        )
        rows = radar.search(EventQuery(latest_only=True))["events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verification"], "retracted")
        self.assertEqual(rows[0]["status"], "retracted")

    def test_invalid_verification_transitions_are_refused(self) -> None:
        for current, target in (
            ("retracted", "single_source"),
            ("expired", "retracted"),
            ("officially_confirmed", "unverified"),
        ):
            with self.subTest(current=current, target=target):
                with self.assertRaises(ValueError):
                    verification.ensure_verification_transition(current, target)

    def test_a_valid_verification_transition_is_returned_as_enums(self) -> None:
        current, target = verification.ensure_verification_transition(
            "single_source", "officially_confirmed"
        )
        self.assertEqual(current, VerificationState.SINGLE_SOURCE)
        self.assertEqual(target, VerificationState.OFFICIALLY_CONFIRMED)
        for state in VerificationState:
            self.assertIn(state, VERIFICATION_TRANSITIONS)

    def test_retraction_and_denial_wording_is_detected(self) -> None:
        self.assertTrue(verification.retraction_frame("RBI withdraws its earlier statement"))
        self.assertFalse(verification.retraction_frame("RBI holds the rate"))
        self.assertTrue(verification.denial_frame("The company denies the report"))
        self.assertFalse(verification.denial_frame("The company reports earnings"))


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


class ClassificationTests(unittest.TestCase):
    def test_importance_bands_ascend(self) -> None:
        self.assertEqual(classification.band(0.0), Importance.LOW)
        self.assertEqual(classification.band(100.0), Importance.CRITICAL)
        scores = (10.0, 40.0, 60.0, 80.0)
        bands = [classification.band(score) for score in scores]
        self.assertEqual(
            bands,
            [Importance.LOW, Importance.MODERATE, Importance.HIGH, Importance.CRITICAL],
        )

    def test_recency_decays_with_age(self) -> None:
        fresh = classification.recency_value(NOW, NOW)
        old = classification.recency_value(NOW - timedelta(days=30), NOW)
        unknown = classification.recency_value(None, NOW)
        self.assertGreater(fresh, old)
        self.assertGreaterEqual(old, 0.0)
        self.assertEqual(unknown, 0.35)

    def test_a_central_bank_rate_decision_ranks_high(self) -> None:
        radar = _radar()
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        row = radar.search(EventQuery())["events"][0]
        self.assertIn(row["importance"], {"high", "critical"})
        detail = row["classification"]
        self.assertTrue(detail["explanation"])
        self.assertTrue(detail["factors"])
        self.assertAlmostEqual(
            sum(factor["contribution"] for factor in detail["factors"]),
            detail["score"],
            places=2,
        )

    def test_confidence_is_reported_separately_from_importance(self) -> None:
        radar = _radar()
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        row = radar.search(EventQuery())["events"][0]
        self.assertGreaterEqual(row["confidence"], 0.0)
        self.assertLessEqual(row["confidence"], 1.0)
        self.assertNotEqual(row["confidence"], row["importance"])

    def test_no_event_is_a_price_prediction_or_a_recommendation(self) -> None:
        radar = _radar()
        _ingest(radar, _rss(_item(EARNINGS_TITLE, EARNINGS_SUMMARY, "ril/1")), _definition())
        row = radar.search(EventQuery())["events"][0]
        self.assertFalse(row["is_price_prediction"])
        self.assertFalse(row["is_trading_recommendation"])

    def test_more_affected_instruments_raise_the_score(self) -> None:
        event = ExtractedEvent(
            title=RATE_TITLE,
            description=RATE_SUMMARY,
            category=EventCategory.MACROECONOMIC,
            subcategory="interest_rate",
            matched_terms=("repo rate",),
            entity_names=(),
            occurred_at=None,
            market_wide=True,
        )
        decision = VerificationDecision(
            state=VerificationState.OFFICIALLY_CONFIRMED,
            reasons=("official source",),
            evidence_ids=("evd_1",),
            independent_sources=1,
            conflicts=(),
        )
        evidence = (
            EvidenceRecord(
                evidence_id="evd_1",
                event_id="evt_1",
                source_id="probe_feed",
                url="https://example.test/1",
                publisher="Probe Publisher",
                reliability=SourceReliability.OFFICIAL,
                title=RATE_TITLE,
                excerpt=RATE_SUMMARY,
                content_hash="abc",
                independence_key="probe",
                retrieved_at=NOW,
                published_at=NOW,
                status=EvidenceStatus.RETRIEVED,
                reasons=(),
            ),
        )
        bare = classification.classify(
            event=event, verification=decision, evidence=evidence, mappings=(), published_at=NOW, now=NOW
        )
        mapped = classification.classify(
            event=event,
            verification=decision,
            evidence=evidence,
            mappings=(
                EventMapping("XNSE:HDFCBANK", Relationship.DIRECT, "named in the statement", ""),
                EventMapping("XNSE:ICICIBANK", Relationship.DIRECT, "named in the statement", ""),
            ),
            published_at=NOW,
            now=NOW,
        )
        self.assertGreater(mapped.score, bare.score)
        names = {factor.name for factor in bare.factors}
        self.assertIn("verification", names)
        self.assertIn("recency", names)


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------


class DedupeTests(unittest.TestCase):
    def _candidate(self, **overrides):
        values = dict(
            subject="rbi",
            subcategory="interest_rate",
            title=RATE_TITLE,
            digest="digest-a",
            instrument_ids=(),
            published_at=NOW,
        )
        values.update(overrides)
        return dedupe.Candidate(**values)

    def _existing(self, **overrides):
        values = dict(
            event_id="evt_1",
            event_key="key-1",
            subject="rbi",
            subcategory="interest_rate",
            title=RATE_TITLE,
            digest="digest-a",
            instrument_ids=(),
            published_at=NOW,
            version=1,
        )
        values.update(overrides)
        return dedupe.Existing(**values)

    def test_an_identical_report_is_a_duplicate(self) -> None:
        decision = dedupe.decide(self._candidate(), (self._existing(),))
        self.assertEqual(decision.kind, "duplicate")
        self.assertEqual(decision.event_id, "evt_1")
        self.assertTrue(decision.reasons)

    def test_a_different_event_with_a_similar_title_stays_new(self) -> None:
        decision = dedupe.decide(
            self._candidate(subcategory="earnings", subject="reliance", digest="digest-b"),
            (self._existing(),),
        )
        self.assertEqual(decision.kind, "new")

    def test_a_report_far_outside_the_window_is_a_new_event(self) -> None:
        decision = dedupe.decide(
            self._candidate(published_at=NOW + timedelta(days=30), digest="digest-c"),
            (self._existing(),),
        )
        self.assertEqual(decision.kind, "new")

    def test_a_confirming_report_updates_the_same_event(self) -> None:
        decision = dedupe.decide(
            self._candidate(
                title=RATE_TITLE + " confirmed",
                digest="digest-d",
                published_at=NOW + timedelta(hours=3),
            ),
            (self._existing(),),
        )
        self.assertEqual(decision.kind, "update")
        self.assertGreater(decision.similarity, 0.0)

    def test_an_update_appends_a_version_and_keeps_the_old_evidence(self) -> None:
        first = _definition(source_id="wire_a", publisher="Wire A", url="https://a.example.test/feed")
        second = _definition(source_id="wire_b", publisher="Wire B", reliability=SourceReliability.REPUTABLE, url="https://b.example.test/feed")
        radar = _radar((first, second))
        _ingest(
            radar,
            _rss(_item(RATE_TITLE, RATE_SUMMARY, "a/1", url="https://a.example.test/a/1")),
            first,
        )
        _ingest(
            radar,
            _rss(_item(RATE_TITLE + " confirmed", RATE_SUMMARY + " Officials confirmed the decision.", "b/1")),
            second,
        )
        history = radar.history(radar.search(EventQuery())["events"][0]["event_id"])
        self.assertEqual(len(history["versions"]), 2)
        self.assertEqual(history["versions"][-1]["version"], 2)
        self.assertEqual(history["versions"][-1]["supersedes"], 1)


# --------------------------------------------------------------------------
# Instrument and sector mapping
# --------------------------------------------------------------------------


class MappingTests(unittest.TestCase):
    def test_a_named_company_maps_to_its_canonical_instrument(self) -> None:
        radar = _radar(instruments=(_instrument(),))
        counts = _ingest(
            radar,
            _rss(_item(EARNINGS_TITLE, EARNINGS_SUMMARY, "ril/1")),
            _definition(source_type=SourceType.COMPANY_IR, reliability=SourceReliability.PRIMARY),
        )
        self.assertEqual(counts["created"], 1)
        row = radar.search(EventQuery())["events"][0]
        self.assertIn("XNSE:RELIANCE", row["instruments"])
        mapping_row = row["mappings"][0]
        self.assertEqual(mapping_row["relationship"], "direct")
        self.assertTrue(mapping_row["reason"])
        self.assertIn("Energy", row["sectors"])

    def test_an_unknown_company_gets_no_invented_instrument(self) -> None:
        radar = _radar(instruments=(_instrument(),))
        _ingest(
            radar,
            _rss(_item("Acme Widgets reports quarterly earnings", "Acme Widgets reported quarterly profit.", "acme/1")),
            _definition(),
        )
        row = radar.search(EventQuery())["events"][0]
        self.assertEqual(row["instruments"], [])
        self.assertEqual(row["mappings"], [])

    def test_a_ticker_inside_a_word_is_not_an_instrument_link(self) -> None:
        instrument = _instrument(
            instrument_id="XBOM:BIL",
            name="Bharat Forge Limited",
            aliases=(),
            sectors=(),
        )
        decisions = mapping.map_event(
            title="Musk stocks lose $120 billion as Democrats' Texas win rattles investors",
            summary="Tesla and SpaceX lost more than $120 billion in combined market value.",
            entity_names=(),
            instruments=(instrument,),
            sector_relevance=False,
        )
        self.assertEqual(decisions, ())

    def test_a_macro_event_maps_through_sector_context_not_similarity(self) -> None:
        radar = _radar(instruments=(_instrument(),))
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        row = radar.search(EventQuery())["events"][0]
        for mapping_row in row["mappings"]:
            self.assertIn(mapping_row["relationship"], {"direct", "indirect", "uncertain"})
        self.assertEqual(row["category"], "macroeconomic")

    def test_a_sector_only_connection_is_never_reported_as_direct(self) -> None:
        decisions = mapping.map_event(
            title=SECTOR_TITLE,
            summary=SECTOR_SUMMARY,
            entity_names=(),
            instruments=(_instrument(sectors=("Steel",)),),
            sector_relevance=True,
        )
        for decision in decisions:
            self.assertNotEqual(decision.relationship, Relationship.DIRECT)
            self.assertTrue(decision.reason)

    def test_an_event_can_be_found_by_instrument(self) -> None:
        radar = _radar(instruments=(_instrument(),))
        _ingest(
            radar,
            _rss(_item(EARNINGS_TITLE, EARNINGS_SUMMARY, "ril/1")),
            _definition(source_type=SourceType.COMPANY_IR, reliability=SourceReliability.PRIMARY),
        )
        body = radar.instrument_events("XNSE:RELIANCE")
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["events"][0]["instruments"], ["XNSE:RELIANCE"])
        self.assertEqual(radar.instrument_events("XNSE:UNKNOWN")["total"], 0)


# --------------------------------------------------------------------------
# Persistence, history and retrieval
# --------------------------------------------------------------------------


class PersistenceTests(unittest.TestCase):
    def test_events_and_evidence_survive_a_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "radar.sqlite3"
            store = EventStore(path, clock=lambda: NOW)
            radar = _radar(store=store)
            _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
            event_id = radar.search(EventQuery())["events"][0]["event_id"]
            radar.close()
            store.close()

            reopened = EventStore(path, clock=lambda: NOW)
            radar = _radar(store=reopened)
            body = radar.event(event_id)
            self.assertEqual(body["title"], RATE_TITLE)
            self.assertEqual(radar.evidence(event_id)["total"], 1)
            self.assertEqual(reopened.schema_version, 1)
            radar.close()
            reopened.close()

    def test_the_lifecycle_path_is_recorded_once_per_stage(self) -> None:
        radar = _radar()
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        event_id = radar.search(EventQuery())["events"][0]["event_id"]
        transitions = radar.history(event_id)["transitions"]
        statuses = [row["to_status"] for row in transitions if row.get("to_status")]
        for expected in ("extracted", "normalized", "verification", "classified", "active"):
            self.assertIn(expected, statuses)

    def test_filters_narrow_by_category_verification_and_time(self) -> None:
        radar = _radar()
        _ingest(
            radar,
            _rss(
                _item(RATE_TITLE, RATE_SUMMARY, "rbi/1"),
                _item(EARNINGS_TITLE, EARNINGS_SUMMARY, "ril/1"),
            ),
            _definition(),
        )
        self.assertEqual(radar.search(EventQuery())["total"], 2)
        self.assertEqual(
            radar.search(EventQuery(category=EventCategory.MACROECONOMIC))["total"], 1
        )
        self.assertEqual(
            radar.search(EventQuery(category=EventCategory.COMPANY))["total"], 1
        )
        self.assertEqual(
            radar.search(EventQuery(verification=VerificationState.SINGLE_SOURCE))["total"], 0
        )
        self.assertEqual(
            radar.search(EventQuery(importance=Importance.LOW))["total"]
            + radar.search(EventQuery(importance=Importance.MODERATE))["total"]
            + radar.search(EventQuery(importance=Importance.HIGH))["total"]
            + radar.search(EventQuery(importance=Importance.CRITICAL))["total"],
            2,
        )
        self.assertEqual(
            radar.search(
                EventQuery(since=datetime(2026, 11, 2, tzinfo=UTC), until=datetime(2026, 11, 3, tzinfo=UTC))
            )["total"],
            2,
        )
        self.assertEqual(
            radar.search(EventQuery(since=datetime(2026, 11, 5, tzinfo=UTC)))["total"], 0
        )
        page = radar.search(EventQuery(limit=1, offset=1))
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["total"], 2)

    def test_an_unknown_event_is_a_typed_not_found(self) -> None:
        radar = _radar()
        with self.assertRaises(EventNotFoundError):
            radar.event("evt_missing")

    def test_expiry_does_not_delete_history(self) -> None:
        store = EventStore(clock=lambda: NOW)
        radar = _radar(store=store)
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        event_id = radar.search(EventQuery())["events"][0]["event_id"]
        later = NOW + timedelta(days=400)
        report = asyncio.run(radar.expire_due(now=later))
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["expired"][0]["event_id"], event_id)
        self.assertEqual(radar.event(event_id)["status"], "expired")
        self.assertTrue(radar.history(event_id)["versions"])

    def test_an_event_inside_its_window_is_not_expired(self) -> None:
        radar = _radar()
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        self.assertEqual(
            asyncio.run(radar.expire_due(now=NOW + timedelta(days=1)))["count"], 0
        )


# --------------------------------------------------------------------------
# Engine, publication and observability
# --------------------------------------------------------------------------


class EngineIntegrationTests(unittest.TestCase):
    def test_refresh_ingests_a_feed_and_reports_health(self) -> None:
        body = _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1"))
        transport = _Transport({"https://example.test/feed": (200, body, {"ETag": '"v1"'})})
        radar = _radar(client=EventFeedClient(transport=transport))
        report = asyncio.run(radar.refresh("probe_feed"))
        self.assertEqual(report["events_created"], 1)
        health = radar.sources()[0]
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["entries_seen"], 1)
        self.assertEqual(health["entries_new"], 1)
        self.assertTrue(health["last_success"])

    def test_a_second_identical_refresh_is_a_duplicate(self) -> None:
        body = _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1"))
        transport = _Transport({"https://example.test/feed": (200, body, {})})
        radar = _radar(client=EventFeedClient(transport=transport))
        asyncio.run(radar.refresh("probe_feed"))
        second = asyncio.run(radar.refresh("probe_feed"))
        self.assertEqual(second["events_created"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(radar.search(EventQuery())["total"], 1)

    def test_one_failing_source_does_not_stop_the_others(self) -> None:
        good = _definition(source_id="good_feed", url="https://good.example.test/feed")
        bad = _definition(source_id="bad_feed", url="https://bad.example.test/feed")
        transport = _Transport(
            {
                "https://good.example.test/feed": (200, _rss(_item(RATE_TITLE, RATE_SUMMARY, "good/1")), {}),
                "https://bad.example.test/feed": (503, "", {}),
            }
        )
        radar = _radar((good, bad), client=EventFeedClient(transport=transport, retry=_no_retry()))
        report = asyncio.run(radar.refresh(None))
        self.assertEqual(report["events_created"], 1)
        health = {row["source_id"]: row for row in radar.sources()}
        self.assertEqual(health["good_feed"]["status"], "ok")
        self.assertEqual(health["bad_feed"]["status"], "error")
        self.assertTrue(health["bad_feed"]["last_error_code"])
        self.assertEqual(radar.metrics()["sources_failing"], 1)

    def test_a_source_failure_is_never_reported_as_a_successful_ingest(self) -> None:
        transport = _Transport({"https://example.test/feed": (503, "", {})})
        radar = _radar(client=EventFeedClient(transport=transport, retry=_no_retry()))
        report = asyncio.run(radar.refresh("probe_feed"))
        self.assertEqual(report["events_created"], 0)
        self.assertTrue(report["sources"][0]["error_code"])
        self.assertEqual(radar.metrics()["events"], 0)

    def test_events_are_published_only_after_the_row_is_stored(self) -> None:
        publisher = _Publisher()
        radar = _radar()
        radar.set_publisher(publisher)
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        self.assertTrue(publisher.events)
        stored_id = radar.search(EventQuery())["events"][0]["event_id"]
        types = {event_type for event_type, _ in publisher.events}
        self.assertIn("novamarket.market_event.discovered", types)
        self.assertIn("novamarket.market_event.verified", types)
        self.assertIn("novamarket.market_event.classified", types)
        for event_type, payload in publisher.events:
            self.assertTrue(event_type.startswith("novamarket.market_event."))
            self.assertEqual(payload.get("event_id"), stored_id)

    def test_expiry_publishes_the_expired_event(self) -> None:
        publisher = _Publisher()
        radar = _radar()
        radar.set_publisher(publisher)
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        asyncio.run(radar.expire_due(now=NOW + timedelta(days=400)))
        types = [event_type for event_type, _ in publisher.events]
        self.assertIn("novamarket.market_event.expired", types)

    def test_an_update_does_not_republish_discovery(self) -> None:
        first = _definition(source_id="wire_a", publisher="Wire A", url="https://a.example.test/feed")
        second = _definition(source_id="wire_b", publisher="Wire B", url="https://b.example.test/feed")
        publisher = _Publisher()
        radar = _radar((first, second))
        radar.set_publisher(publisher)
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "a/1")), first)
        _ingest(
            radar,
            _rss(_item(RATE_TITLE + " today", RATE_SUMMARY, "b/1", url="https://b.example.test/b/1")),
            second,
        )
        discovered = [
            payload
            for name, payload in publisher.events
            if name == "novamarket.market_event.discovered"
        ]
        self.assertEqual(len(discovered), 1, "an update is not a new discovery")
        self.assertEqual(
            discovered[0]["event_id"],
            radar.search(EventQuery())["events"][0]["event_id"],
        )

    def test_metrics_describe_what_is_actually_stored(self) -> None:
        radar = _radar()
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        metrics = radar.metrics()
        self.assertEqual(metrics["events"], 1)
        self.assertEqual(metrics["evidence_records"], 1)
        self.assertGreaterEqual(metrics["transitions"], 6)
        self.assertIn("officially_confirmed", metrics["by_verification"])

    def test_the_contract_exposes_stable_consumer_surfaces(self) -> None:
        radar = _radar()
        contract = radar.contract()
        for key in ("categories", "importance", "verification_states", "dedupe", "timestamps", "limits"):
            self.assertIn(key, contract)
        self.assertEqual(
            set(contract["verification_states"]),
            {state.value for state in VerificationState},
        )
        self.assertEqual(
            set(contract["importance"]["weights"]),
            set(classification.FACTOR_WEIGHTS),
        )
        categories = {row["category"] for row in contract["categories"]}
        self.assertTrue({"company", "macroeconomic", "sector", "market_structure"} <= categories)
        self.assertFalse(contract["importance"]["is_price_prediction"])

    def test_status_reports_the_store_and_the_running_state(self) -> None:
        radar = _radar()
        status = radar.status()
        self.assertTrue(status["initialised"])
        self.assertEqual(status["schema_version"], 1)
        self.assertIn("metrics", status)

    def test_source_enablement_is_persisted_not_just_remembered(self) -> None:
        radar = _radar()
        body = radar.set_source_enabled("probe_feed", False)
        self.assertFalse(body["enabled"])
        self.assertFalse(radar.sources()[0]["enabled"])
        self.assertEqual(asyncio.run(radar.refresh(None))["events_created"], 0)


# --------------------------------------------------------------------------
# Contracts with the rest of NovaMarket
# --------------------------------------------------------------------------


class VocabularyAndApiTests(unittest.TestCase):
    def test_every_radar_event_has_an_envelope_contract(self) -> None:
        names = {
            "novamarket.market_event.discovered",
            "novamarket.market_event.extracted",
            "novamarket.market_event.normalized",
            "novamarket.market_event.verified",
            "novamarket.market_event.classified",
            "novamarket.market_event.updated",
            "novamarket.market_event.retracted",
            "novamarket.market_event.expired",
            "novamarket.market_event.source_unavailable",
            "novamarket.market_event.evidence.conflict_detected",
        }
        event_types = {event.value for event in EventType}
        self.assertTrue(names <= event_types)
        for name in names:
            self.assertIn(EventType(name), EVENT_PAYLOAD_FIELDS)

    def test_the_lifecycle_state_machine_refuses_unknown_statuses(self) -> None:
        self.assertEqual(EventStatus.ACTIVE.value, "active")
        for status in EventStatus:
            self.assertIn(status, STATUS_TRANSITIONS)
        self.assertIn(EventStatus.ACTIVE, STATUS_TRANSITIONS[EventStatus.CLASSIFIED])

    def test_the_nine_event_routes_are_declared_and_consumed(self) -> None:
        paths = {route.path for route in ApiSurface.default().routes}
        expected = {
            "/market/events",
            "/market/events/sources",
            "/market/events/sources/{source_id}/enabled",
            "/market/events/refresh",
            "/market/events/expire",
            "/market/events/instrument/{instrument_id}",
            "/market/events/{event_id}",
            "/market/events/{event_id}/evidence",
            "/market/events/{event_id}/history",
        }
        self.assertTrue(expected <= paths)
        for path in expected:
            self.assertIn(path, {route.path for route in ApiSurface.default().routes})
        consumers = {path for _, path in ROUTE_CONSUMERS if path.startswith("/market/events")}
        self.assertEqual(consumers, expected)

    def test_an_event_from_the_radar_is_storable_as_a_future_consumer_read(self) -> None:
        radar = _radar()
        _ingest(radar, _rss(_item(RATE_TITLE, RATE_SUMMARY, "rbi/1")), _definition())
        row = radar.search(EventQuery())["events"][0]
        for key in (
            "event_id",
            "category",
            "subcategory",
            "verification",
            "importance",
            "confidence",
            "instruments",
            "sectors",
            "occurred_at",
            "published_at",
            "discovered_at",
            "retrieved_at",
        ):
            self.assertIn(key, row)
        json.dumps(row)


if __name__ == "__main__":
    unittest.main()
