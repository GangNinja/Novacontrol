"""Official NSE F&O contract parsing and option-chain tests (offline)."""

from __future__ import annotations

import json
import io
import math
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from novacontrol.novamarket.marketdata.derivatives import (
    build_futures_chains,
    build_option_chains,
    implied_volatility,
    load_nse_fo_snapshot,
    parse_nse_fo_futures_csv,
    parse_nse_fo_csv,
    scan_futures_snapshot,
)
from novacontrol.novamarket.workbench import DERIVATIVES_SNAPSHOT_PATH

HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,"
    "LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
    "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
)
DAY = "2026-11-06"


def row(strike: str, side: str, oi: str, close: str) -> str:
    return (
        f"{DAY},{DAY},FO,NSE,IDO,1,,NIFTY,,2026-11-10,2026-11-10,{strike},{side},"
        f"NIFTY26NOV{strike}{side},0,0,0,{close},0,{close},25000.00,0,{oi},0,100,1000,1,F1,75,,,,,\n"
    )


def sample() -> str:
    return HEADER + row("25000", "CE", "120000", "130.00") + row(
        "25000", "PE", "180000", "80.00"
    ) + row("25100", "CE", "60000", "90.00") + row("25100", "PE", "90000", "120.00") + future_row(
        "NIFTY", "IDF", "2026-11-24", "25120.50", "900000", "12000", "75"
    ) + future_row("RELIANCE", "STF", "2026-11-24", "1450.00", "250000", "3000", "250")


def future_row(
    symbol: str,
    kind: str,
    expiry: str,
    close: str,
    oi: str,
    volume: str,
    lot: str,
) -> str:
    return (
        f"{DAY},{DAY},FO,NSE,{kind},2,,{symbol},,{expiry},{expiry},0,,"
        f"{symbol}26NOVFUT,100,110,90,{close},0,{close},0,{close},{oi},500,{volume},1000,10,F1,{lot},,,,,,\n"
    )


def scanner_snapshot() -> dict:
    """Two expiries plus one expired row and one unmapped underlying."""
    def contract(
        kind: str,
        name: str,
        expiration: str,
        close: str,
        previous: str,
        oi: int,
        change: int,
        volume: int,
        lot: int,
    ) -> dict:
        return {
            "expiry": expiration,
            "instrument_type": kind,
            "contract_name": name,
            "close": close,
            "settlement": close,
            "previous_close": previous,
            "underlying_price": close,
            "open_interest": oi,
            "change_in_open_interest": change,
            "volume": volume,
            "value": str(volume * 100),
            "trades": 1,
            "lot_size": lot,
        }

    chains = {
        "NIFTY": {
            "2026-11-05": contract("IDF", "NIFTY 05 NOV 26 FUT", "2026-11-05", "24900", "24800", 700000, 5000, 8000, 75),
            "2026-11-10": contract("IDF", "NIFTY 10 NOV 26 FUT", "2026-11-10", "25120.50", "25080", 900000, 12000, 12000, 75),
            "2026-12-29": contract("IDF", "NIFTY 29 DEC 26 FUT", "2026-12-29", "25300", "25120", 800000, -10000, 9000, 75),
        },
        "RELIANCE": {
            "2026-11-10": contract("STF", "RELIANCE 10 NOV 26 FUT", "2026-11-10", "1460", "1450", 260000, 10000, 3000, 250),
        },
        "UNKNOWN": {
            "2026-11-10": contract("STF", "UNKNOWN 10 NOV 26 FUT", "2026-11-10", "100", "101", 10000, -1000, 500, 100),
        },
    }
    return {
        "source": "official fixture",
        "trade_date": DAY,
        "generated_at": "2026-11-06T12:00:00+00:00",
        "futures": {
            "contracts": 5,
            "underlyings": sorted(chains),
            "chains": chains,
            "default_symbol": "NIFTY",
            "default_expiry": "2026-11-10",
        },
    }


class TestDerivatives:
    def test_official_contracts_and_pcr(self) -> None:
        contracts = parse_nse_fo_csv(sample(), expected_date=date(2026, 11, 6))
        assert len(contracts) == 4
        chains = build_option_chains(contracts)
        chain = chains["NIFTY"]["2026-11-10"]
        assert chain["total_call_oi"] == 180000
        assert chain["total_put_oi"] == 270000
        assert chain["pcr"] == 1.5
        assert len(chain["rows"]) == 2
        assert chain["rows"][0]["CE"]["open_interest"] == 120000
        assert chain["rows"][0]["PE"]["open_interest"] == 180000

    def test_model_iv_is_null_when_intrinsic_only(self) -> None:
        assert (
            implied_volatility(
                Decimal("1"),
                Decimal("25000"),
                Decimal("24000"),
                date(2026, 11, 10),
                date(2026, 11, 6),
                "CE",
            )
            is None
        )

    def test_model_iv_solves_for_a_real_premium(self) -> None:
        value = implied_volatility(
            Decimal("130"),
            Decimal("25000"),
            Decimal("25000"),
            date(2026, 11, 10),
            date(2026, 11, 6),
            "CE",
        )
        assert value is not None and 1.0 < value < 500.0

    def test_model_iv_recovers_independent_black_scholes_reference(self) -> None:
        spot = 25_000.0
        strike = 25_000.0
        years = 4.0 / 365.0
        rate = 0.065
        vol = 0.20
        root = vol * math.sqrt(years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / root
        d2 = d1 - root
        cdf = lambda value: 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
        reference = spot * cdf(d1) - strike * math.exp(-rate * years) * cdf(d2)
        value = implied_volatility(
            Decimal(str(round(reference, 6))),
            Decimal("25000"),
            Decimal("25000"),
            date(2026, 11, 10),
            date(2026, 11, 6),
            "CE",
        )
        assert value is not None and abs(value - 20.0) < 0.05

    def test_iv_refuses_missing_or_expired_inputs(self) -> None:
        assert implied_volatility(None, Decimal("25000"), Decimal("25000"), date(2026, 11, 10), date(2026, 11, 6), "CE") is None
        assert implied_volatility(Decimal("130"), Decimal("25000"), Decimal("25000"), date(2026, 11, 6), date(2026, 11, 6), "CE") is None

    def test_futures_parser_keeps_index_and_stock_futures(self) -> None:
        contracts = parse_nse_fo_futures_csv(sample(), expected_date=date(2026, 11, 6))
        assert len(contracts) == 2
        chains = build_futures_chains(contracts)
        nifty = chains["NIFTY"]["2026-11-24"]
        assert nifty["instrument_type"] == "IDF"
        assert nifty["open_interest"] == 900000
        assert nifty["change_in_open_interest"] == 500
        assert nifty["volume"] == 12000
        assert nifty["lot_size"] == 75
        assert nifty["close"] == "25120.50"
        reliance = chains["RELIANCE"]["2026-11-24"]
        assert reliance["instrument_type"] == "STF"
        assert reliance["lot_size"] == 250

    def test_duplicate_futures_are_deduplicated_by_official_identity(self) -> None:
        duplicate = future_row("NIFTY", "IDF", "2026-11-24", "25120.50", "900000", "12000", "75")
        contracts = parse_nse_fo_futures_csv(sample() + duplicate)
        assert len(contracts) == 2

    def test_invalid_future_expiry_is_skipped(self) -> None:
        invalid = future_row("NIFTY", "IDF", "2026-11-24", "25120.50", "900000", "12000", "75")
        contracts = parse_nse_fo_futures_csv(sample() + invalid.replace("2026-11-24", "not-a-date"))
        assert len(contracts) == 2
        assert {item.symbol for item in contracts} == {"NIFTY", "RELIANCE"}

    def test_futures_scanner_filters_expired_and_reports_rollover_and_mapping(self) -> None:
        status = scan_futures_snapshot(
            scanner_snapshot(),
            equities=[{"symbol": "RELIANCE", "instrument_id": "XNSE:RELIANCE", "name": "Reliance"}],
            as_of=date(2026, 11, 10),
        )
        coverage = status["coverage"]
        assert status["status"] == "AVAILABLE"
        assert status["supported"] is True
        assert coverage["contracts_registered"] == 5
        assert coverage["contracts_scanned"] == 4
        assert coverage["expired_excluded"] == 1
        assert coverage["invalid_excluded"] == 0
        assert coverage["index_contracts"] == 2
        assert coverage["stock_contracts"] == 2
        assert coverage["mapped_contracts"] == 1
        assert coverage["unmapped_contracts"] == 1
        assert coverage["front_month_contracts"] == 3
        nifty = next(row for row in status["rows"] if row["symbol"] == "NIFTY" and row["expiry"] == "2026-11-10")
        assert nifty["is_front"] is True
        assert nifty["price_change_pct"] > 0
        assert nifty["oi_change_pct"] > 0
        assert nifty["buildup"] == "long_buildup"
        reliance = next(row for row in status["rows"] if row["symbol"] == "RELIANCE")
        assert reliance["underlying_instrument_id"] == "XNSE:RELIANCE"
        assert reliance["mapping_status"] == "mapped"
        unknown = next(row for row in status["rows"] if row["symbol"] == "UNKNOWN")
        assert unknown["mapping_status"] == "missing_equity_mapping"
        assert next(item for item in status["rollover"] if item["symbol"] == "NIFTY")["next_expiry"] == "2026-12-29"

    def test_futures_scanner_excludes_every_expired_contract(self) -> None:
        status = scan_futures_snapshot(scanner_snapshot(), as_of=date(2027, 1, 1))
        assert status["status"] == "EMPTY"
        assert status["supported"] is False
        assert status["rows"] == []
        assert status["coverage"]["expired_excluded"] == 5

    def test_futures_scanner_marks_a_stale_snapshot(self) -> None:
        status = scan_futures_snapshot(scanner_snapshot(), as_of=date(2026, 11, 20))
        assert status["status"] == "STALE"
        assert status["stale_days"] == 14
        assert status["coverage"]["contracts_scanned"] == 1

    def test_futures_scanner_reports_invalid_chain_rows(self) -> None:
        snapshot = scanner_snapshot()
        snapshot["futures"]["chains"]["NIFTY"]["not-a-date"] = {"close": "1"}
        status = scan_futures_snapshot(snapshot, as_of=date(2026, 11, 10))
        assert status["coverage"]["contracts_registered"] == 6
        assert status["coverage"]["invalid_excluded"] == 1
        assert status["coverage"]["contracts_scanned"] == 4

    def test_workbench_fno_reads_and_maps_the_official_futures_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        from novacontrol.novamarket import workbench

        path = tmp_path / "derivatives_snapshot.json"
        path.write_text(json.dumps(scanner_snapshot()), encoding="utf-8")
        monkeypatch.setattr(workbench, "DERIVATIVES_SNAPSHOT_PATH", path)
        status = workbench.MarketWorkbench._fno_contract_status(
            [{"symbol": "RELIANCE", "instrument_id": "XNSE:RELIANCE", "name": "Reliance"}]
        )
        assert status["supported"] is True
        assert status["status"] == "AVAILABLE"
        assert status["coverage"]["equities_scanned"] == 1
        assert status["coverage"]["mapped_contracts"] == 1
        assert any(row["underlying_instrument_id"] == "XNSE:RELIANCE" for row in status["rows"])

    def test_workbench_fno_missing_snapshot_is_blocked_without_rows(self, tmp_path: Path, monkeypatch) -> None:
        from novacontrol.novamarket import workbench

        monkeypatch.setattr(workbench, "DERIVATIVES_SNAPSHOT_PATH", tmp_path / "absent.json")
        status = workbench.MarketWorkbench._fno_contract_status([])
        assert status["supported"] is False
        assert status["status"] == "BLOCKED"
        assert status["rows"] == []
        assert status["coverage"]["contracts_scanned"] == 0

    def test_snapshot_separates_option_and_futures_counts(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as bundle:
            bundle.writestr("BhavCopy_NSE_FO.csv", sample())
        snapshot = load_nse_fo_snapshot(date(2026, 11, 6), payload=payload.getvalue())
        assert snapshot["contracts"] == 4
        assert snapshot["option_contracts"] == 4
        assert snapshot["futures_contracts"] == 2
        assert snapshot["total_contracts"] == 6
        assert snapshot["futures"]["default_symbol"] == "NIFTY"
        assert snapshot["futures"]["default_expiry"] == "2026-11-24"
        assert snapshot["futures"]["chains"]["RELIANCE"]["2026-11-24"]["instrument_type"] == "STF"

    def test_workbench_reads_cached_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        from novacontrol.novamarket import workbench

        contracts = parse_nse_fo_csv(sample())
        chain = build_option_chains(contracts)["NIFTY"]["2026-11-10"]
        snapshot = {
            "source": "official fixture",
            "trade_date": DAY,
            "generated_at": "2026-11-06T12:00:00+00:00",
            "default_symbol": "NIFTY",
            "default_expiry": "2026-11-10",
            "default_chain": chain,
            "underlyings": ["NIFTY"],
            "fields": ["strike", "open_interest", "implied_volatility_pct"],
            "iv_method": "test",
            "disclaimer": "official fixture",
        }
        path = tmp_path / "derivatives_snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        monkeypatch.setattr(workbench, "DERIVATIVES_SNAPSHOT_PATH", path)
        status = workbench.MarketWorkbench._options_contract_status()
        assert status["supported"] is True
        assert status["status"] == "AVAILABLE"
        assert status["pcr"] == 1.5
        assert len(status["rows"]) == 2

    def test_missing_snapshot_is_blocked_not_synthesised(self, tmp_path: Path, monkeypatch) -> None:
        from novacontrol.novamarket import workbench

        monkeypatch.setattr(workbench, "DERIVATIVES_SNAPSHOT_PATH", tmp_path / "absent.json")
        status = workbench.MarketWorkbench._options_contract_status()
        assert status["supported"] is False
        assert status["status"] == "BLOCKED"
        assert status["rows"] == []

    def test_workbench_reads_futures_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        from novacontrol.novamarket import workbench

        contracts = parse_nse_fo_futures_csv(sample())
        chains = build_futures_chains(contracts)
        snapshot = {
            "source": "official fixture",
            "trade_date": DAY,
            "generated_at": "2026-11-06T12:00:00+00:00",
            "futures_contracts": len(contracts),
            "futures": {
                "contracts": len(contracts),
                "underlyings": sorted(chains),
                "chains": chains,
                "default_symbol": "NIFTY",
                "default_expiry": "2026-11-24",
                "fields": ["expiry", "open_interest", "volume"],
                "note": "official fixture",
            },
        }
        path = tmp_path / "derivatives_snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        monkeypatch.setattr(workbench, "DERIVATIVES_SNAPSHOT_PATH", path)
        status = workbench.MarketWorkbench._futures_contract_status()
        assert status["supported"] is True
        assert status["status"] == "AVAILABLE"
        assert status["contracts"] == 2
        assert status["underlying_count"] == 2
        assert status["rows"][0]["expiry"] == "2026-11-24"
        assert status["rows"][0]["instrument_type"] == "IDF"
