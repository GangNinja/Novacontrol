"""The Market Overview screener must filter honestly and never rebase a price."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from novacontrol.novamarket.marketdata.screener import MarketBoard, load_index_reference
from novacontrol.novamarket.workbench import MarketWorkbench


class FakeEngine:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str | None] = []

    def board_rows(self, *, exchange: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(exchange)
        if exchange is None:
            return list(self.rows)
        return [row for row in self.rows if row["exchange"] == exchange]


def row(
    symbol: str,
    *,
    exchange: str = "NSE",
    price: str | None = "110",
    observed_at: str = "2026-11-05T04:15:00+00:00",
    bar_observed_at: str | None = "2026-11-05T03:45:00+00:00",
    bar_high: str | None = "112",
    bar_low: str | None = "105",
    bar_close: str | None = "109",
    previous_bar_close: str | None = "100",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "instrument_id": f"X{'NSE' if exchange == 'NSE' else 'BOM'}:{symbol}",
        "symbol": symbol,
        "name": f"{symbol} Limited",
        "exchange": exchange,
        "mic": "XNSE" if exchange == "NSE" else "XBOM",
        "asset_class": "equity",
        "currency": "INR",
        "price": price,
        "observed_at": observed_at,
        "provider": "yahoo" if price else None,
        "quality": "valid" if price else None,
        "freshness": "fresh" if price else None,
        "provider_metadata_json": "{}",
        "observation_metadata": metadata or {},
        "observation_raw": {},
        "bar_open": "106",
        "bar_high": bar_high,
        "bar_low": bar_low,
        "bar_close": bar_close,
        "bar_volume": "1000",
        "bar_observed_at": bar_observed_at,
        "bar_partial": 0,
        "bar_provider": "yahoo" if bar_observed_at else None,
        "previous_bar_close": previous_bar_close,
        "average_volume": 900.0,
    }


def board(rows: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return MarketBoard(
        FakeEngine(rows),
        now=datetime(2026, 11, 5, 5, tzinfo=UTC),
    ).snapshot(**kwargs)


class TestIndexReference:
    def test_required_index_universes_are_packaged(self) -> None:
        reference = load_index_reference()
        ids = {row["id"] for row in reference.index_rows}
        assert {
            "nifty50",
            "nifty100",
            "niftytotalmarket",
            "nifty500",
            "niftymidcap100",
            "niftysmallcap100",
        } <= ids
        assert reference.as_of
        assert reference.source

    def test_market_cap_tier_is_index_defined(self) -> None:
        reference = load_index_reference()
        reliance = reference.membership("RELIANCE")
        assert reliance is not None
        assert reliance.market_cap == "large"
        assert "nifty50" in reliance.indices
        assert "nifty500" in reliance.indices


class TestWorkbenchSectorFallback:
    def test_packaged_reference_fills_a_missing_instrument_sector(self) -> None:
        workbench = MarketWorkbench.__new__(MarketWorkbench)
        assert workbench._sector({"symbol": "RELIANCE", "metadata": {}}) == (
            "Oil Gas & Consumable Fuels"
        )

    def test_explicit_instrument_metadata_keeps_precedence(self) -> None:
        workbench = MarketWorkbench.__new__(MarketWorkbench)
        assert workbench._sector(
            {"symbol": "RELIANCE", "metadata": {"sector": "Desk override"}}
        ) == "Desk override"

    def test_sector_evidence_includes_source_and_effective_date(self) -> None:
        workbench = MarketWorkbench.__new__(MarketWorkbench)
        reference = load_index_reference()
        sector, source, as_of = workbench._sector_evidence(
            {"symbol": "RELIANCE", "metadata": {}}
        )
        assert sector == "Oil Gas & Consumable Fuels"
        assert source == reference.source
        assert as_of == reference.as_of

    def test_metadata_provenance_is_kept_verbatim(self) -> None:
        workbench = MarketWorkbench.__new__(MarketWorkbench)
        sector, source, as_of = workbench._sector_evidence(
            {
                "symbol": "RELIANCE",
                "metadata": {
                    "sector": "Desk override",
                    "sector_source": "Desk master v3",
                    "sector_as_of": "2026-10-01",
                },
            }
        )
        assert sector == "Desk override"
        assert source == "Desk master v3"
        assert as_of == "2026-10-01"

    def test_flow_and_heatmap_surface_the_effective_date(self) -> None:
        workbench = MarketWorkbench.__new__(MarketWorkbench)
        analyses = [
            {
                "instrument_id": "XNSE:RELIANCE",
                "symbol": "RELIANCE",
                "name": "Reliance Industries",
                "sector": "Oil Gas & Consumable Fuels",
                "sector_source": "NSE snapshot",
                "sector_as_of": "2026-11-05",
                "change_pct": 1.2,
                "momentum_score": 61.0,
                "relative_volume": 1.4,
                "metrics": {"relative_volume": 1.4},
                "trend": "bullish",
            }
        ]
        flow = workbench._sector_flow(analyses)
        assert flow[0]["sector_source"] == "NSE snapshot"
        assert flow[0]["sector_as_of"] == "2026-11-05"
        heatmap = workbench._heatmap(analyses)
        assert heatmap["sector_reference"]["as_of"] == load_index_reference().as_of
        assert heatmap["sectors"][0]["sector_as_of"] == "2026-11-05"


class TestScreenerFilters:
    def test_exchange_index_cap_and_mover_filters_are_independent(self) -> None:
        rows = [
            row("RELIANCE"),
            row("TCS"),
            row("ABC", price="90", observed_at="2026-11-05T04:15:00+00:00"),
            row("XYZ", price=None),
            row("RELIANCE", exchange="BSE"),
        ]
        # ABC and XYZ are not in the packaged large-cap universe, but one has a
        # real quote. This proves the cap filter does not hide an available row.
        result = board(rows, exchange="NSE", cap="all", available_only=False)
        assert {item["symbol"] for item in result["rows"]} == {
            "RELIANCE",
            "TCS",
            "ABC",
            "XYZ",
        }
        assert result["coverage"]["catalogue_total"] == 4
        assert result["summary"]["quotes_available"] == 3
        assert result["summary"]["quotes_missing"] == 1

        large = board(rows, cap="large")
        assert {item["symbol"] for item in large["rows"]} == {"RELIANCE", "TCS"}

        nifty = board(rows, index="nifty50")
        assert {item["symbol"] for item in nifty["rows"]} == {"RELIANCE", "TCS"}

        bse = board(rows, exchange="BSE")
        assert {item["exchange"] for item in bse["rows"]} == {"BSE"}

        gainers = board(rows, movers="gainers")
        assert {item["symbol"] for item in gainers["rows"]} == {"RELIANCE", "TCS"}
        losers = board(rows, movers="losers")
        assert {item["symbol"] for item in losers["rows"]} == {"ABC"}

    def test_available_only_keeps_scope_counts(self) -> None:
        result = board([row("RELIANCE"), row("MISSING", price=None)], available_only=True)
        assert [item["symbol"] for item in result["rows"]] == ["RELIANCE"]
        assert result["coverage"]["matched"] == 2
        assert result["coverage"]["listed_after_availability"] == 1
        assert result["summary"]["quotes_missing"] == 1

    def test_cross_listings_are_one_stock_in_aggregate_counts(self) -> None:
        result = board(
            [row("RELIANCE"), row("RELIANCE", exchange="BSE")],
            available_only=False,
        )
        # The board shows both exchange listings, but the summary is about the
        # company, not about how many venues list it.
        assert result["coverage"]["matched"] == 2
        assert result["coverage"]["unique_symbols"] == 1
        assert result["summary"]["stocks"] == 1
        assert result["summary"]["quotes_available"] == 1
        assert result["summary"]["quotes_available_listings"] == 2
        large = result["summary"]["by_market_cap"]["large"]
        assert large["count"] == 1
        assert large["listings"] == 2
        assert large["quotes_available"] == 1
        assert large["gainers"] == 1

    def test_unknown_filter_is_rejected(self) -> None:
        rows = [row("RELIANCE")]
        for kwargs in (
            {"exchange": "NASDAQ"},
            {"cap": "mega"},
            {"index": "nifty999"},
            {"sort": "sideways"},
            {"movers": "maybe"},
        ):
            try:
                board(rows, **kwargs)
            except ValueError:
                pass
            else:  # pragma: no cover - failure is the assertion
                raise AssertionError(f"{kwargs} should have been rejected")

    def test_arrangement_puts_missing_values_last(self) -> None:
        result = board(
            [row("MISSING", price=None), row("LOSS", price="90"), row("GAIN", price="110")],
            sort="losers",
            available_only=False,
        )
        assert [item["symbol"] for item in result["rows"]] == ["LOSS", "GAIN", "MISSING"]


class TestPriceIntegrity:
    def test_provider_previous_close_is_used_for_change(self) -> None:
        result = board(
            [row("RELIANCE", price="110", metadata={"previous_close": 100})],
            available_only=True,
        )
        item = result["rows"][0]
        assert item["previous_close"] == 100.0
        assert item["change_pct"] == 10.0
        assert item["change_basis"] == "provider previous close"
        assert item["day_high"] == 112.0
        assert item["bar_available"] is True

    def test_provider_day_range_and_volume_need_no_same_session_bar(self) -> None:
        result = board(
            [
                row(
                    "RELIANCE",
                    price="110",
                    bar_observed_at="2026-10-30T03:45:00+00:00",
                    metadata={
                        "previous_close": 100,
                        "day_high": 115,
                        "day_low": 108,
                        "day_volume": 250000,
                    },
                )
            ],
            available_only=True,
        )
        item = result["rows"][0]
        assert item["day_high"] == 115.0
        assert item["day_low"] == 108.0
        assert item["volume"] == 250000.0
        assert item["range_basis"] == "provider quote day range"
        assert item["volume_basis"] == "provider quote day volume"
        assert item["bar_available"] is False

    def test_stale_bar_never_becomes_a_one_day_move_or_range(self) -> None:
        result = board(
            [
                row(
                    "OLD",
                    price="110",
                    observed_at="2026-11-05T04:15:00+00:00",
                    bar_observed_at="2026-10-30T03:45:00+00:00",
                    bar_high="112",
                    bar_low="105",
                    bar_close="109",
                    previous_bar_close="100",
                )
            ],
            available_only=True,
        )
        item = result["rows"][0]
        assert item["change_pct"] is None
        assert item["previous_close"] is None
        assert item["day_high"] is None
        assert item["day_low"] is None
        assert item["volume"] is None
        assert item["bar_available"] is False
        assert item["bar_session_matches_quote"] is False
        assert result["summary"]["bars_available"] == 0

    def test_last_four_day_close_is_used_as_a_labelled_fallback(self) -> None:
        result = board(
            [
                row(
                    "RECENT",
                    price="110",
                    observed_at="2026-11-05T04:15:00+00:00",
                    bar_observed_at="2026-11-03T03:45:00+00:00",
                    bar_close="100",
                )
            ],
            available_only=True,
        )
        item = result["rows"][0]
        assert item["previous_close"] == 100.0
        assert item["change_pct"] == 10.0
        assert item["change_basis"] == "last stored daily close"
        assert item["day_high"] is None
        assert item["bar_available"] is False
