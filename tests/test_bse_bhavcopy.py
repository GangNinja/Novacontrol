"""Official BSE bhavcopy parsing and resumable import tests (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.novamarket.marketdata.bse_bhavcopy import (
    BseBhavcopyUnavailable,
    bse_bhavcopy_url,
    import_bse_bhavcopy,
    parse_bse_bhavcopy,
)
from novacontrol.novamarket.marketdata.models import Instrument
from novacontrol.novamarket.marketdata.store import MarketDataStore

HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,"
    "LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
    "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
)


def bhavcopy(day: str, *, close: str = "1140.65") -> str:
    return HEADER + (
        f"{day},{day},CM,BSE,STK,500003,INE208C01025,AEGISLOG,A,,,,,"
        f"AEGIS LOGISTICS LTD.,1163.00,1174.65,1127.00,{close},1140.00,"
        "1187.95,,1141.08,,,79921,91363294.00,3428,F1,1,,,,,\n"
        f"{day},{day},CM,BSE,STK,500002,INE117A01022,ABB,A,,,,,"
        "ABB INDIA LIMITED,6510.00,6774.00,6431.00,6774.00,6774.00,"
        "6640.10,,6774.00,,,28168,185307125.00,4355,F1,1,,,,,\n"
        f"{day},{day},CM,BSE,STK,500004,INE000000000,NOTREG,A,,,,,"
        "UNREGISTERED LTD.,10.00,11.00,9.00,10.00,10.00,9.50,,10.00,,,100,1.00,1,F1,1,,,,,\n"
    )


class TestBseParse:
    def test_stock_rows_are_parsed(self) -> None:
        rows = parse_bse_bhavcopy(bhavcopy("2026-11-06"), expected_date=date(2026, 11, 6))
        assert len(rows) == 3
        reliance = next(row for row in rows if row.symbol == "AEGISLOG")
        assert reliance.close == Decimal("1140.65")
        assert reliance.volume == 79921
        assert reliance.observed_at == datetime(2026, 11, 6, 10, 0, tzinfo=UTC)

    def test_url_uses_official_filename(self) -> None:
        assert bse_bhavcopy_url(date(2026, 11, 6)).endswith(
            "BhavCopy_BSE_CM_0_0_0_20261106_F_0000.CSV"
        )


class TestBseImport:
    def setup_method(self) -> None:
        self.temp = TemporaryDirectory()
        self.store = MarketDataStore(Path(self.temp.name) / "market.sqlite3")
        self.store.upsert_instrument(
            Instrument.create(
                symbol="AEGISLOG",
                name="Aegis Logistics",
                exchange="BSE",
                mic="XBOM",
                currency="INR",
                timezone="Asia/Kolkata",
                provider_symbols={"yahoo": "AEGISLOG.BO"},
            )
        )

    def teardown_method(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _fetch(self, day: date) -> str:
        return bhavcopy(day.isoformat())

    def test_imports_only_registered_bse_symbols_and_resumes(self) -> None:
        checkpoint = Path(self.temp.name) / "checkpoint.json"
        first = import_bse_bhavcopy(
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
        assert self.store.bar_inventory(min_bars=1)["items"][0]["bar_count"] == 2
        second = import_bse_bhavcopy(
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
                raise BseBhavcopyUnavailable("holiday")
            return self._fetch(day)

        result = import_bse_bhavcopy(
            self.store,
            start=date(2026, 11, 5),
            end=date(2026, 11, 6),
            fetch=fetch,
        )
        assert not result["ok"]
        assert result["inserted"] == 1
        assert result["errors"][0]["date"] == "2026-11-05"
        assert self.store.bar_inventory(min_bars=1)["items"][0]["bar_count"] == 1
