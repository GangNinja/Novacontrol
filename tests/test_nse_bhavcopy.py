"""Official NSE bhavcopy parsing and resumable import tests (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.novamarket.marketdata.models import Instrument
from novacontrol.novamarket.marketdata.nse_bhavcopy import (
    BhavcopyUnavailable,
    bhavcopy_url,
    import_nse_bhavcopy,
    parse_nse_bhavcopy,
)
from novacontrol.novamarket.marketdata.store import MarketDataStore

HEADER = (
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
    "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, "
    "NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
)


def bhavcopy(day: str, *, close: str = "248.20") -> str:
    return HEADER + (
        f"RELIANCE, EQ, {day}, 246.00, 247.00, 249.00, 245.00, 248.50, "
        f"{close}, 248.10, 120000, 100.0, 1000, 5000, 10.0\n"
        f"NOTREG, EQ, {day}, 10.00, 10.00, 11.00, 9.00, 10.50, 10.50, "
        "10.40, 1000, 1.0, 10, 100, 10.0\n"
        f"BOND, GB, {day}, 100.00, 100.00, 101.00, 99.00, 100.50, 100.50, "
        "100.40, 100, 1.0, 1, 10, 10.0\n"
    )


class TestParse:
    def test_equity_rows_are_parsed_with_exchange_close_time(self) -> None:
        rows = parse_nse_bhavcopy(bhavcopy("06-Nov-2026"), expected_date=date(2026, 11, 6))
        assert len(rows) == 2
        reliance = next(row for row in rows if row.symbol == "RELIANCE")
        assert reliance.close == Decimal("248.20")
        assert reliance.volume == 120000
        assert reliance.observed_at == datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
        assert all(row.symbol != "BOND" for row in rows)

    def test_mismatched_file_date_is_refused(self) -> None:
        try:
            parse_nse_bhavcopy(bhavcopy("05-Nov-2026"), expected_date=date(2026, 11, 6))
        except ValueError as exc:
            assert "expected 2026-11-06" in str(exc)
        else:  # pragma: no cover - a mismatch must not pass
            raise AssertionError("a mismatched bhavcopy date was accepted")

    def test_url_uses_the_official_session_name(self) -> None:
        assert bhavcopy_url(date(2026, 11, 6)).endswith("sec_bhavdata_full_06112026.csv")


class TestImport:
    def setup_method(self) -> None:
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "market.sqlite3"
        self.store = MarketDataStore(self.path)
        self.store.upsert_instrument(
            Instrument.create(
                symbol="RELIANCE",
                name="Reliance Industries",
                exchange="NSE",
                mic="XNSE",
                currency="INR",
                timezone="Asia/Kolkata",
                provider_symbols={"yahoo": "RELIANCE.NS"},
            )
        )

    def teardown_method(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _fetch(self, day: date) -> str:
        return bhavcopy(day.strftime("%d-%b-%Y"))

    def test_imports_only_registered_symbols_and_is_idempotent(self) -> None:
        checkpoint = Path(self.temp.name) / "checkpoint.json"
        first = import_nse_bhavcopy(
            self.store,
            start=date(2026, 11, 5),
            end=date(2026, 11, 6),
            checkpoint_path=checkpoint,
            fetch=self._fetch,
            clock=lambda: datetime(2026, 11, 7, 12, tzinfo=UTC),
        )
        assert first["ok"]
        assert first["inserted"] == 2
        assert first["registered_instruments"] == 1
        assert self.store.bar_inventory(min_bars=1)["total"] == 1
        assert self.store.bar_inventory(min_bars=1)["items"][0]["bar_count"] == 2

        second = import_nse_bhavcopy(
            self.store,
            start=date(2026, 11, 5),
            end=date(2026, 11, 6),
            checkpoint_path=checkpoint,
            fetch=self._fetch,
        )
        assert second["sessions_processed_this_run"] == 0
        assert second["inserted"] == 0

    def test_missing_session_is_reported_not_synthesised(self) -> None:
        def fetch(day: date) -> str:
            if day == date(2026, 11, 5):
                raise BhavcopyUnavailable("holiday")
            return self._fetch(day)

        result = import_nse_bhavcopy(
            self.store,
            start=date(2026, 11, 5),
            end=date(2026, 11, 6),
            fetch=fetch,
        )
        assert not result["ok"]
        assert result["inserted"] == 1
        assert result["errors"][0]["date"] == "2026-11-05"
        assert self.store.bar_inventory(min_bars=1)["items"][0]["bar_count"] == 1
