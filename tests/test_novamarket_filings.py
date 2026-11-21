"""Free filing adapter coverage: mapping, timestamps, limits, honesty.

Every test injects a fake fetcher; nothing here reaches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from novacontrol.novamarket.filings import (
    FILING_SOURCE_ID,
    FILING_SOURCE_NAME,
    HttpReply,
    YahooFundamentalsAdapter,
)
from novacontrol.novamarket.research.evidence import build_company_profile
from novacontrol.novamarket.research.financials import (
    analyze_financials,
    select_point_in_time,
)
from novacontrol.novamarket.research.models import EvidenceRecord, FinancialStatement

INSTRUMENT = {
    "instrument_id": "XBOM:ADANIENT",
    "symbol": "ADANIENT",
    "exchange": "BSE",
    "provider_symbols": {"yahoo": "ADANIENT.BO"},
}


def _row(series_type: str, periods: dict[str, float], currency: str = "INR") -> dict[str, Any]:
    entries = [
        {
            "asOfDate": period,
            "periodType": "12M",
            "currencyCode": currency,
            "reportedValue": {"raw": value},
        }
        for period, value in sorted(periods.items())
    ]
    return {"meta": {"symbol": ["ADANIENT.BO"], "type": [series_type]}, series_type: entries}


def _timeseries(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if rows is None:
        rows = [
            _row("annualTotalRevenue", {"2025-03-31": 9.7e11, "2026-03-31": 1.0e12}),
            _row("annualGrossProfit", {"2025-03-31": 2.6e11, "2026-03-31": 2.6e11}),
            _row("annualNetIncome", {"2025-03-31": 7.0e10, "2026-03-31": 9.3e10}),
            _row("annualTotalAssets", {"2025-03-31": 1.9e12, "2026-03-31": 2.6e12}),
            _row("annualStockholdersEquity", {"2025-03-31": 4.0e11, "2026-03-31": 4.5e11}),
            _row("quarterlyTotalRevenue", {"2026-06-30": 3.29e11}),
            _row("quarterlyNetIncome", {"2026-06-30": 1.5e10}),
        ]
    return {"timeseries": {"result": rows, "error": None}}


def _profile(summary: str = "Adani Enterprises operates ports, airports and mining.") -> dict[str, Any]:
    return {
        "quoteSummary": {
            "result": [
                {
                    "assetProfile": {
                        "sector": "Energy",
                        "industry": "Thermal Coal",
                        "longBusinessSummary": summary,
                    }
                }
            ],
            "error": None,
        }
    }


class FakeYahoo:
    """A deterministic stand-in for Yahoo's three endpoints."""

    def __init__(
        self,
        *,
        timeseries: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
        crumb: str = "abc.123",
        timeseries_status: int = 200,
        profile_status: int = 200,
    ) -> None:
        self.timeseries = timeseries if timeseries is not None else _timeseries()
        self.profile = profile if profile is not None else _profile()
        self.crumb = crumb
        self.timeseries_status = timeseries_status
        self.profile_status = profile_status
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Any) -> HttpReply:
        self.calls.append(url)
        if "fundamentals-timeseries" in url:
            return HttpReply(self.timeseries_status, json.dumps(self.timeseries).encode())
        if url.endswith("getcrumb"):
            return HttpReply(200, self.crumb.encode())
        if "quoteSummary" in url:
            return HttpReply(self.profile_status, json.dumps(self.profile).encode())
        return HttpReply(404, b"")

    @property
    def timeseries_calls(self) -> list[str]:
        return [url for url in self.calls if "fundamentals-timeseries" in url]


def _adapter(
    fake: FakeYahoo,
    *,
    cache_dir: Path | None = None,
    clock: Any = None,
    **kwargs: Any,
) -> YahooFundamentalsAdapter:
    return YahooFundamentalsAdapter(
        fetcher=fake,
        cache_dir=cache_dir,
        # The real first observation is the clock, so every test dates its
        # statements deterministically instead of inheriting wall time.
        clock=clock or _at(AS_OF),
        min_interval_seconds=0.0,
        sleeper=lambda seconds: None,
        **kwargs,
    )


def _at(moment: datetime) -> Any:
    return lambda: moment


AS_OF = datetime(2026, 11, 19, 12, tzinfo=UTC)


def test_statements_are_mapped_from_the_public_series() -> None:
    fake = FakeYahoo()
    bundle = _adapter(fake).load(INSTRUMENT, as_of=AS_OF)

    assert bundle.symbol == "ADANIENT.BO"
    assert bundle.requests >= 2
    by_key = {(row["statement_type"], row["period_type"], row["period_end"]) for row in bundle.statements}
    assert ("income", "annual", "2026-03-31T00:00:00+00:00") in by_key
    assert ("balance_sheet", "annual", "2026-03-31T00:00:00+00:00") in by_key
    assert ("income", "quarterly", "2026-06-30T00:00:00+00:00") in by_key
    income = next(
        row
        for row in bundle.statements
        if row["statement_type"] == "income"
        and row["period_type"] == "annual"
        and row["period_end"].startswith("2026")
    )
    assert income["currency"] == "INR"
    assert income["values"]["revenue"] == 1.0e12
    assert income["values"]["net_income"] == 9.3e10
    assert income["basis"] == "consolidated"
    assert income["verification"] == "unverified"
    assert income["fiscal_year"] == 2026


def test_publication_time_is_the_first_observation_and_is_never_backdated() -> None:
    """A filing is dated by its first observation, and the note says so.

    The stamp is the observation, never the reporting period and never an
    earlier research cutoff; the existing point-in-time gate then excludes a
    historical run's statements and keeps a live run's.
    """
    fake = FakeYahoo()
    first = _adapter(fake, clock=_at(AS_OF)).load(INSTRUMENT)
    annual = next(row for row in first.statements if row["period_end"].startswith("2026-03-31"))
    assert annual["published_at"] == AS_OF.isoformat()
    # Never the reporting period: that would claim a filing time the source
    # does not publish.
    assert annual["published_at"] != annual["period_end"]
    # The run states the basis it used instead of leaving it implicit.
    assert any(
        "each publication time is its first observation" in note for note in first.notes
    ), first.notes
    assert any("publishes no filing timestamp" in note for note in first.notes), first.notes

    # A run whose cutoff is on an earlier day cannot claim the data as its own:
    # the stamp stays the real observation, so the gate excludes every statement.
    cutoff = AS_OF - timedelta(days=45)
    historical = _adapter(fake, clock=_at(AS_OF)).load(INSTRUMENT, as_of=cutoff)
    backdated = next(
        row for row in historical.statements if row["period_end"].startswith("2026-03-31")
    )
    assert backdated["published_at"] == AS_OF.isoformat()
    assert backdated["retrieved_at"] == AS_OF.isoformat()
    assert backdated["published_at"] != cutoff.isoformat()
    assert historical.fetched_at == AS_OF
    # The historical run states the same basis in its own notes, and every
    # statement it returned carries the observation stamp, not the cutoff.
    assert any(
        "each publication time is its first observation" in note
        for note in historical.notes
    ), historical.notes
    assert historical.statements
    assert all(
        row["published_at"] == AS_OF.isoformat() for row in historical.statements
    ), [row["published_at"] for row in historical.statements]

    statements = [
        FinancialStatement.from_payload(row, instrument_id="XBOM:ADANIENT", default_time=cutoff)
        for row in historical.statements
    ]
    selected, skipped, _restatements, _warnings = select_point_in_time(
        statements, as_of=cutoff, cutoff=cutoff, max_age_hours=720.0
    )
    assert selected == []
    assert skipped
    assert all(row["reason"] == "publication is after the research cutoff" for row in skipped)

    live, live_skipped, _live_restatements, _live_warnings = select_point_in_time(
        statements, as_of=AS_OF, cutoff=AS_OF, max_age_hours=720.0
    )
    assert live
    assert live_skipped == []

    # A live run filed earlier the same day is the run doing the observing, so
    # it keeps the evidence it just fetched instead of losing it to the gate.
    same_day_as_of = AS_OF - timedelta(hours=2)
    same_day_bundle = _adapter(fake, clock=_at(AS_OF)).load(INSTRUMENT, as_of=same_day_as_of)
    same_day_statements = [
        FinancialStatement.from_payload(
            row, instrument_id="XBOM:ADANIENT", default_time=same_day_as_of
        )
        for row in same_day_bundle.statements
    ]
    kept, kept_skipped, _kept_restatements, _kept_warnings = select_point_in_time(
        same_day_statements, as_of=same_day_as_of, cutoff=same_day_as_of, max_age_hours=720.0
    )
    assert kept
    assert kept_skipped == []


def test_cache_keeps_the_first_observation_across_a_refetch(tmp_path: Path) -> None:
    fake = FakeYahoo()
    first = _adapter(fake, cache_dir=tmp_path, clock=_at(AS_OF)).load(INSTRUMENT)
    later = AS_OF + timedelta(days=2)
    changed = _timeseries(
        [
            _row("annualTotalRevenue", {"2025-03-31": 9.7e11, "2026-03-31": 1.05e12}),
            _row("annualGrossProfit", {"2025-03-31": 2.6e11, "2026-03-31": 2.7e11}),
            _row("annualNetIncome", {"2025-03-31": 7.0e10, "2026-03-31": 9.9e10}),
            _row("annualTotalAssets", {"2025-03-31": 1.9e12, "2026-03-31": 2.7e12}),
            _row("annualStockholdersEquity", {"2025-03-31": 4.0e11, "2026-03-31": 4.6e11}),
        ]
    )
    refetch = _adapter(fake, cache_dir=tmp_path, clock=_at(later))
    fake.timeseries = changed
    second = refetch.load(INSTRUMENT)

    assert second.from_cache is False
    before = {(row["statement_type"], row["period_end"]): row for row in first.statements}
    for row in second.statements:
        key = (row["statement_type"], row["period_end"])
        if key in before:
            # The proxy time never moves forward on a later fetch.
            assert row["published_at"] == before[key]["published_at"]


def test_cache_answers_inside_the_ttl_without_another_request(tmp_path: Path) -> None:
    fake = FakeYahoo()
    _adapter(fake, cache_dir=tmp_path, clock=_at(AS_OF)).load(INSTRUMENT)
    calls = len(fake.calls)
    bundle = _adapter(fake, cache_dir=tmp_path, clock=_at(AS_OF + timedelta(hours=1))).load(
        INSTRUMENT
    )

    assert bundle.from_cache is True
    assert bundle.statements
    assert len(fake.calls) == calls


def test_incomplete_period_is_reported_not_emitted() -> None:
    fake = FakeYahoo(timeseries=_timeseries([_row("annualTotalRevenue", {"2026-03-31": 1.0e12})]))
    bundle = _adapter(fake).load(INSTRUMENT, as_of=AS_OF)

    assert bundle.statements == ()
    assert any("no statement was assembled" in note for note in bundle.notes)


def test_empty_series_is_stated_not_invented() -> None:
    fake = FakeYahoo(timeseries={"timeseries": {"result": [], "error": None}})
    bundle = _adapter(fake).load(INSTRUMENT, as_of=AS_OF)

    assert bundle.statements == ()
    assert any("no fundamentals series" in note for note in bundle.notes)


def test_profile_facts_become_company_profile_evidence() -> None:
    fake = FakeYahoo()
    bundle = _adapter(fake).load(INSTRUMENT, as_of=AS_OF)

    fields = {row["metadata"]["field"]: row["metadata"]["value"] for row in bundle.evidence}
    assert fields["sector"] == "Energy"
    assert fields["industry"] == "Thermal Coal"
    assert fields["description"].startswith("Adani Enterprises operates")
    for row in bundle.evidence:
        assert row["kind"] == "company_profile"
        assert row["source_id"] == FILING_SOURCE_ID
        assert row["source_name"] == FILING_SOURCE_NAME
        assert row["published_at"] == AS_OF.isoformat()

    records = [
        EvidenceRecord.from_payload(row, instrument_id="XBOM:ADANIENT", default_time=AS_OF)
        for row in bundle.evidence
    ]
    profile = build_company_profile(
        instrument_id="XBOM:ADANIENT",
        identity={"symbol": "ADANIENT", "exchange": "BSE", "name": "Adani Enterprises"},
        evidence=records,
        as_of=AS_OF,
        cutoff=AS_OF,
    )
    assert profile.sector == "energy"
    assert profile.industry == "thermal coal"
    assert profile.description.startswith("Adani Enterprises operates")


def test_refused_profile_session_still_returns_statements() -> None:
    fake = FakeYahoo(crumb='{"finance":{"result":null,"error":{"code":"Unauthorized"}}}')
    bundle = _adapter(fake).load(INSTRUMENT, as_of=AS_OF)

    assert bundle.statements
    assert bundle.evidence == ()
    assert any("company profile unavailable" in note for note in bundle.notes)


def test_daily_budget_refuses_further_requests() -> None:
    fake = FakeYahoo()
    adapter = _adapter(fake, daily_request_limit=1)
    first = adapter.load(INSTRUMENT, as_of=AS_OF)
    assert first.statements  # the one permitted request was spent on the series

    second = adapter.load(INSTRUMENT, as_of=AS_OF)
    assert second.statements == ()
    assert any("ceiling" in note for note in second.notes)
    assert len(fake.calls) == 1


def test_minimum_interval_is_respected() -> None:
    fake = FakeYahoo()
    slept: list[float] = []
    adapter = YahooFundamentalsAdapter(
        fetcher=fake,
        min_interval_seconds=3.0,
        sleeper=slept.append,
    )
    adapter.load(INSTRUMENT, as_of=AS_OF)

    assert slept and max(slept) <= 3.0


def test_instrument_without_a_symbol_reports_the_gap() -> None:
    bundle = _adapter(FakeYahoo()).load({}, instrument_id="XBOM:UNKNOWN", as_of=AS_OF)

    assert bundle.statements == ()
    assert any("no Yahoo symbol mapping" in note for note in bundle.notes)


def test_symbol_resolution_prefers_the_registry_mapping() -> None:
    adapter = _adapter(FakeYahoo())
    assert adapter._symbol(INSTRUMENT) == "ADANIENT.BO"
    assert adapter._symbol({"symbol": "RELIANCE", "exchange": "NSE"}) == "RELIANCE.NS"
    assert adapter._symbol({"symbol": "TCS", "exchange": "BSE"}) == "TCS.BO"


def test_alternate_listing_is_read_when_the_mapped_one_is_empty() -> None:
    class EmptyThenFull(FakeYahoo):
        def __call__(self, url: str, headers: Any) -> HttpReply:
            if "fundamentals-timeseries" in url and ".BO?" in url:
                self.calls.append(url)
                return HttpReply(200, json.dumps({"timeseries": {"result": [], "error": None}}).encode())
            return super().__call__(url, headers)

    fake = EmptyThenFull()
    bundle = _adapter(fake).load(INSTRUMENT, as_of=AS_OF)

    assert bundle.symbol == "ADANIENT.NS"
    assert bundle.statements
    assert any("read the ADANIENT.NS listing instead" in note for note in bundle.notes)


def test_payloads_are_accepted_by_the_research_analysis_layer() -> None:
    bundle = _adapter(FakeYahoo()).load(INSTRUMENT, as_of=AS_OF)
    statements = [
        FinancialStatement.from_payload(row, instrument_id="XBOM:ADANIENT", default_time=AS_OF)
        for row in bundle.statements
    ]
    analysis = analyze_financials(
        statements,
        as_of=AS_OF,
        cutoff=AS_OF,
        max_age_hours=720.0,
        price=2_686.7,
        price_currency="INR",
        shares_outstanding=None,
    )

    assert analysis.statements
    assert any(row["period_end"].startswith("2026-03-31") for row in analysis.statements)
    assert not any("missing publication timestamp" in warning for warning in analysis.warnings)


def test_failed_http_read_is_a_note_and_never_a_statement() -> None:
    fake = FakeYahoo(timeseries_status=429)
    bundle = _adapter(fake).load(INSTRUMENT, as_of=AS_OF)

    assert bundle.statements == ()
    assert any("throttled" in note for note in bundle.notes)
