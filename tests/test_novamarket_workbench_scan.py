"""Full-catalogue workbench scan coverage and pagination regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    Instrument,
    OhlcvBar,
)
from novacontrol.novamarket.marketdata.store import MarketDataStore
from novacontrol.novamarket.workbench import MarketWorkbench

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


class _Page:
    def __init__(self, items: list[Any] | tuple[Any, ...], total: int, limit: int, offset: int) -> None:
        self.items = tuple(items)
        self.total = total
        self.limit = limit
        self.offset = offset


class _Instrument:
    def __init__(self, instrument_id: str) -> None:
        self.instrument_id = instrument_id

    def to_dict(self) -> dict[str, Any]:
        symbol = self.instrument_id.split(":", 1)[1]
        return {
            "instrument_id": self.instrument_id,
            "symbol": symbol,
            "name": f"{symbol} Limited",
            "exchange": "NSE",
            "currency": "INR",
            "asset_class": "equity",
            "metadata": {},
        }


def _bar(instrument_id: str, index: int) -> OhlcvBar:
    observed = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    base = Decimal("100") + Decimal(index) / Decimal("10")
    return OhlcvBar.create(
        instrument_id=instrument_id,
        provider="stored",
        interval=DataInterval.DAY_1,
        open=base,
        high=base + Decimal("1"),
        low=base - Decimal("1"),
        close=base + Decimal("0.5"),
        currency="INR",
        observed_at=observed,
        retrieved_at=observed + timedelta(hours=1),
        source_timezone="Asia/Kolkata",
        volume=Decimal("1000"),
        quality=DataQuality.VALID,
        freshness=Freshness.CLOSED_MARKET,
    )


class _FakeEngine:
    """A deterministic paged catalogue; no network or database is involved."""

    def __init__(
        self,
        count: int = 1005,
        *,
        short_ids: set[str] | None = None,
        fail_ids: set[str] | None = None,
        duplicate_page: bool = False,
        inventory_fail_at: int | None = None,
    ) -> None:
        self.ids = [f"XNSE:T{index:04d}" for index in range(count)]
        self.short_ids = set(short_ids or ())
        self.fail_ids = set(fail_ids or ())
        self.duplicate_page = duplicate_page
        self.inventory_fail_at = inventory_fail_at

    def _instrument(self, instrument_id: str) -> _Instrument:
        return _Instrument(instrument_id)

    def bar_inventory(
        self,
        *,
        interval: str,
        min_bars: int,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        if self.inventory_fail_at is not None and offset >= self.inventory_fail_at:
            raise RuntimeError("inventory read failed")
        rows = [
            {
                "instrument_id": instrument_id,
                "bar_count": 35,
                "latest_observed_at": "2026-11-06T10:00:00+00:00",
            }
            for instrument_id in self.ids
        ]
        items = rows[offset : offset + limit]
        if self.duplicate_page and offset == 1000 and items:
            items = [rows[0], *items]
        return {
            "items": items,
            "total": len(rows),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < len(rows),
        }

    def list_instruments(self, *, limit: int = 100, offset: int = 0) -> _Page:
        items = [self._instrument(instrument_id) for instrument_id in self.ids]
        return _Page(items[offset : offset + limit], len(items), limit, offset)

    def get_instrument(self, instrument_id: str) -> _Instrument:
        if instrument_id not in self.ids:
            raise KeyError(instrument_id)
        return self._instrument(instrument_id)

    def bars(
        self,
        instrument_id: str,
        *,
        interval: DataInterval,
        limit: int,
        ascending: bool,
    ) -> _Page:
        if instrument_id in self.fail_ids:
            raise RuntimeError("stored series read failed")
        count = 29 if instrument_id in self.short_ids else 35
        bars = [_bar(instrument_id, index) for index in range(count)]
        return _Page(bars, count, count, 0)


def _workbench(engine: _FakeEngine, **kwargs: Any) -> MarketWorkbench:
    return MarketWorkbench(cast(Any, engine), min_bars=30, now=lambda: NOW, **kwargs)


def test_full_scan_processes_every_page_and_reports_truthful_coverage() -> None:
    result = _workbench(_FakeEngine(1005), batch_size=250).snapshot(limit=10)
    coverage = result["coverage"]

    assert coverage["eligible"] == 1005
    assert coverage["scanned"] == 1005
    assert coverage["analysed"] == 1005
    assert coverage["skipped"] == 0
    assert coverage["failed"] == 0
    assert coverage["truncated"] is False
    assert coverage["complete"] is True
    assert coverage["sample_only"] is False
    assert coverage["scan_cap"] is None
    assert coverage["inventory_pages"] == 2
    assert coverage["batches"] == 5
    assert result["scan"]["strategy"] == "paged_inventory+bounded_batches"

    ids = [row["instrument_id"] for row in result["instruments"]]
    assert len(ids) == len(set(ids)) == 1005


def test_an_explicit_limit_is_disclosed_and_never_called_complete() -> None:
    result = _workbench(_FakeEngine(1005), scan_limit=250).snapshot(limit=10)
    coverage = result["coverage"]

    assert coverage["eligible"] == 1005
    assert coverage["scanned"] == 250
    assert coverage["analysed"] == 250
    assert coverage["scan_cap"] == 250
    assert coverage["scan_limit_applied"] is True
    assert coverage["truncated"] is True
    assert coverage["complete"] is False
    assert coverage["sample_only"] is True


def test_duplicate_inventory_rows_are_not_analysed_twice() -> None:
    result = _workbench(_FakeEngine(1005, duplicate_page=True)).snapshot(limit=10)
    coverage = result["coverage"]

    assert coverage["duplicates"] == 1
    assert coverage["scanned"] == 1005
    assert coverage["analysed"] == 1005
    assert coverage["complete"] is True
    ids = [row["instrument_id"] for row in result["instruments"]]
    assert len(ids) == len(set(ids)) == 1005


def test_one_failed_series_is_counted_and_does_not_abort_the_scan() -> None:
    result = _workbench(_FakeEngine(1005, fail_ids={"XNSE:T0001"})).snapshot(limit=10)
    coverage = result["coverage"]

    assert coverage["scanned"] == 1005
    assert coverage["analysed"] == 1004
    assert coverage["failed"] == 1
    assert coverage["skipped"] == 0
    assert coverage["complete"] is False
    assert result["failed"][0]["instrument_id"] == "XNSE:T0001"
    assert "stored series read failed" in result["failed"][0]["reason"]


def test_short_history_is_skipped_and_reported() -> None:
    result = _workbench(_FakeEngine(1005, short_ids={"XNSE:T0002"})).snapshot(limit=10)
    coverage = result["coverage"]

    assert coverage["scanned"] == 1005
    assert coverage["analysed"] == 1004
    assert coverage["skipped"] == 1
    assert coverage["failed"] == 0
    assert coverage["complete"] is False
    assert "29 closed bars" in result["skipped"][0]["reason"]


def test_inventory_failure_is_visible_and_never_reported_as_complete() -> None:
    result = _workbench(_FakeEngine(1005, inventory_fail_at=1000)).snapshot(limit=10)
    coverage = result["coverage"]

    assert coverage["eligible"] == 1005
    assert coverage["scanned"] == 1000
    assert coverage["inventory_pages"] == 2
    assert coverage["inventory_error"]
    assert coverage["truncated"] is True
    assert coverage["complete"] is False


def test_store_inventory_offset_is_deterministic_and_does_not_repeat() -> None:
    with TemporaryDirectory() as temp:
        store = MarketDataStore(Path(temp) / "market.sqlite3")
        try:
            for symbol in ("A", "B", "C"):
                store.upsert_instrument(
                    Instrument.create(
                        symbol=symbol,
                        name=symbol,
                        exchange="NSE",
                        mic="XNSE",
                        currency="INR",
                        timezone="Asia/Kolkata",
                        provider_symbols={"stored": symbol},
                    )
                )
                store.append_bar(
                    OhlcvBar.create(
                        instrument_id=f"XNSE:{symbol}",
                        provider="stored",
                        interval=DataInterval.DAY_1,
                        open="100",
                        high="101",
                        low="99",
                        close="100.5",
                        currency="INR",
                        observed_at=datetime(2026, 11, 6, 10, tzinfo=UTC),
                        retrieved_at=datetime(2026, 11, 6, 11, tzinfo=UTC),
                        source_timezone="Asia/Kolkata",
                        volume="1000",
                    )
                )
            first = store.bar_inventory(min_bars=1, limit=2, offset=0)
            second = store.bar_inventory(min_bars=1, limit=2, offset=1)
        finally:
            store.close()

    assert first["total"] == 3
    assert [row["instrument_id"] for row in first["items"]] == ["XNSE:A", "XNSE:B"]
    assert first["has_more"] is True
    assert first["next_offset"] == 2
    assert second["offset"] == 1
    assert [row["instrument_id"] for row in second["items"]] == ["XNSE:B", "XNSE:C"]
    assert second["has_more"] is False
    assert second["next_offset"] is None
