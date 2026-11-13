"""Official NSE F&O bhavcopy: contract master and index-option chain.

NSE publishes the complete F&O contract file as a zipped CSV.  It carries the
real strike, expiry, option type, underlying price, close/settlement, open
interest, change in open interest, volume and lot size.  It does not carry an
implied-volatility column.  The IV in this module is therefore a model output
computed by bisection from the official premium and is labelled as such; OI,
volume and prices are the provider's values.

Nothing here invents a strike, an expiry or an OI value.  A row with no usable
premium is retained without an IV rather than given a fabricated one.
"""

from __future__ import annotations

import csv
import io
import json
import math
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

NSE_FO_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{day}_F_0000.csv.zip"
)
IST = ZoneInfo("Asia/Kolkata")
_MAX_BYTES = 25_000_000
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept": "application/zip,application/octet-stream,*/*",
}
IV_RATE = 0.065
IV_METHOD = (
    "Black-Scholes bisection on the official close/settlement premium; "
    "6.5% annual risk-free rate; expiry at 15:30 IST. Model output, not a "
    "provider-reported IV."
)


class DerivativesUnavailable(RuntimeError):
    """The official F&O file could not be obtained or parsed."""


@dataclass(frozen=True, slots=True)
class OptionContract:
    trade_date: date
    symbol: str
    instrument_type: str
    expiry: date
    strike: Decimal
    option_type: str
    close: Decimal | None
    settlement: Decimal | None
    previous_close: Decimal | None
    underlying_price: Decimal | None
    open_interest: int
    change_in_oi: int
    volume: int
    value: Decimal | None
    trades: int
    lot_size: int
    contract_name: str

    @property
    def premium(self) -> Decimal | None:
        for value in (self.close, self.settlement):
            if value is not None and value > 0:
                return value
        return None


@dataclass(frozen=True, slots=True)
class FuturesContract:
    """One official NSE index or stock futures contract."""

    trade_date: date
    symbol: str
    instrument_type: str
    expiry: date
    close: Decimal | None
    settlement: Decimal | None
    previous_close: Decimal | None
    underlying_price: Decimal | None
    open_interest: int
    change_in_oi: int
    volume: int
    value: Decimal | None
    trades: int
    lot_size: int
    contract_name: str


def _field(row: dict[str, Any], name: str) -> str:
    wanted = name.upper()
    for key, value in row.items():
        if str(key or "").strip().upper() == wanted:
            return str(value if value is not None else "").strip()
    return ""


def _decimal(value: str, *, required: bool = False) -> Decimal | None:
    text = str(value or "").strip()
    if text in ("", "-", "NA", "N/A"):
        if required:
            raise ValueError("a required decimal is missing")
        return None
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a decimal: {value!r}") from exc


def _int(value: str) -> int:
    try:
        return int(Decimal(str(value or "0").replace(",", "")))
    except (InvalidOperation, ValueError):
        return 0


def _expiry(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid expiry {value!r}") from exc


def parse_nse_fo_csv(
    text: str, *, expected_date: date | None = None
) -> tuple[OptionContract, ...]:
    """Parse the official F&O CSV into index-option contracts."""
    if not text.strip():
        raise ValueError("the NSE F&O bhavcopy was empty")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("the NSE F&O bhavcopy has no header row")
    fields = {str(name or "").strip().upper() for name in reader.fieldnames}
    required = {
        "TRADDT",
        "FININSTRMTP",
        "TCKRSYMB",
        "XPRYDT",
        "STRKPRIC",
        "OPTNTP",
        "CLSPRIC",
        "OPNINTRST",
    }
    missing = sorted(required - fields)
    if missing:
        raise ValueError(f"the F&O bhavcopy lacks column(s): {', '.join(missing)}")

    rows: list[OptionContract] = []
    seen: set[str] = set()
    for raw in reader:
        if _field(raw, "FinInstrmTp") != "IDO":
            continue
        symbol = _field(raw, "TckrSymb").upper()
        option_type = _field(raw, "OptnTp").upper()
        contract_name = _field(raw, "FinInstrmNm")
        if not symbol or option_type not in ("CE", "PE"):
            continue
        trade_text = _field(raw, "TradDt")
        try:
            trade_date = datetime.strptime(trade_text, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"invalid TradDt {trade_text!r}") from exc
        if expected_date is not None and trade_date != expected_date:
            raise ValueError(
                f"the F&O bhavcopy contains {trade_date.isoformat()}, "
                f"expected {expected_date.isoformat()}"
            )
        identity = contract_name or "|".join(
            (symbol, _field(raw, "XpryDt"), _field(raw, "StrkPric"), option_type)
        )
        if identity in seen:
            continue
        try:
            strike = _decimal(_field(raw, "StrkPric"), required=True)
            expiry = _expiry(_field(raw, "XpryDt"))
        except ValueError:
            continue
        assert strike is not None
        if strike <= 0:
            continue
        seen.add(identity)
        rows.append(
            OptionContract(
                trade_date=trade_date,
                symbol=symbol,
                instrument_type="IDO",
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                close=_decimal(_field(raw, "ClsPric")),
                settlement=_decimal(_field(raw, "SttlmPric")),
                previous_close=_decimal(_field(raw, "PrvsClsgPric")),
                underlying_price=_decimal(_field(raw, "UndrlygPric")),
                open_interest=_int(_field(raw, "OpnIntrst")),
                change_in_oi=_int(_field(raw, "ChngInOpnIntrst")),
                volume=_int(_field(raw, "TtlTradgVol")),
                value=_decimal(_field(raw, "TtlTrfVal")),
                trades=_int(_field(raw, "TtlNbOfTxsExctd")),
                lot_size=_int(_field(raw, "NewBrdLotQty")),
                contract_name=contract_name,
            )
        )
    if not rows:
        raise ValueError("the NSE F&O bhavcopy contained no index-option contracts")
    return tuple(rows)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _black_scholes(
    spot: float, strike: float, years: float, rate: float, vol: float, kind: str
) -> float:
    if years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    root = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / root
    d2 = d1 - root
    if kind == "CE":
        return spot * _normal_cdf(d1) - strike * math.exp(-rate * years) * _normal_cdf(d2)
    return strike * math.exp(-rate * years) * _normal_cdf(-d2) - spot * _normal_cdf(-d1)


def implied_volatility(
    premium: Decimal | None,
    spot: Decimal | None,
    strike: Decimal,
    expiry: date,
    trade_date: date,
    kind: str,
) -> float | None:
    """Solve IV from the official premium; return None when it is not solvable."""
    if premium is None or spot is None or premium <= 0 or spot <= 0:
        return None
    years = (
        datetime.combine(expiry, dt_time(15, 30), tzinfo=IST)
        - datetime.combine(trade_date, dt_time(15, 30), tzinfo=IST)
    ).total_seconds() / (365.0 * 24.0 * 3600.0)
    if years <= 0:
        return None
    spot_f, strike_f, premium_f = float(spot), float(strike), float(premium)
    intrinsic = max(0.0, spot_f - strike_f) if kind == "CE" else max(0.0, strike_f - spot_f)
    if premium_f <= intrinsic + 1e-9:
        return None
    low, high = 1e-4, 5.0
    if _black_scholes(spot_f, strike_f, years, IV_RATE, high, kind) < premium_f:
        return None
    for _ in range(60):
        middle = (low + high) / 2.0
        if _black_scholes(spot_f, strike_f, years, IV_RATE, middle, kind) > premium_f:
            high = middle
        else:
            low = middle
    return round((low + high) / 2.0 * 100.0, 2)


def build_option_chains(contracts: Iterable[OptionContract]) -> dict[str, Any]:
    """Group official contracts by underlying and expiry, with PCR and IV."""
    grouped: dict[str, dict[str, list[OptionContract]]] = defaultdict(lambda: defaultdict(list))
    for contract in contracts:
        grouped[contract.symbol][contract.expiry.isoformat()].append(contract)

    chains: dict[str, Any] = {}
    for symbol, expiries in grouped.items():
        chains[symbol] = {}
        for expiry, rows in expiries.items():
            by_strike: dict[Decimal, dict[str, Any]] = {}
            for contract in rows:
                entry = by_strike.setdefault(
                    contract.strike,
                    {
                        "strike": str(contract.strike),
                        "CE": None,
                        "PE": None,
                    },
                )
                side = {
                    "price": str(contract.premium) if contract.premium is not None else None,
                    "settlement": str(contract.settlement) if contract.settlement is not None else None,
                    "open_interest": contract.open_interest,
                    "change_in_open_interest": contract.change_in_oi,
                    "volume": contract.volume,
                    "implied_volatility_pct": implied_volatility(
                        contract.premium,
                        contract.underlying_price,
                        contract.strike,
                        contract.expiry,
                        contract.trade_date,
                        contract.option_type,
                    ),
                    "lot_size": contract.lot_size,
                    "contract_name": contract.contract_name,
                }
                entry[contract.option_type] = side
            call_oi = sum(
                int(item["CE"]["open_interest"])
                for item in by_strike.values()
                if item.get("CE")
            )
            put_oi = sum(
                int(item["PE"]["open_interest"])
                for item in by_strike.values()
                if item.get("PE")
            )
            underlying = next(
                (contract.underlying_price for contract in rows if contract.underlying_price),
                None,
            )
            chains[symbol][expiry] = {
                "expiry": expiry,
                "underlying_price": str(underlying) if underlying is not None else None,
                "total_call_oi": call_oi,
                "total_put_oi": put_oi,
                "pcr": round(put_oi / call_oi, 4) if call_oi else None,
                "rows": [by_strike[strike] for strike in sorted(by_strike)],
            }
    return chains


def parse_nse_fo_futures_csv(
    text: str, *, expected_date: date | None = None
) -> tuple[FuturesContract, ...]:
    """Parse the official NSE F&O CSV into index and stock futures."""
    if not text.strip():
        raise ValueError("the NSE F&O bhavcopy was empty")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("the NSE F&O bhavcopy has no header row")
    fields = {str(name or "").strip().upper() for name in reader.fieldnames}
    required = {
        "TRADDT",
        "FININSTRMTP",
        "TCKRSYMB",
        "XPRYDT",
        "CLSPRIC",
        "OPNINTRST",
    }
    missing = sorted(required - fields)
    if missing:
        raise ValueError(f"the F&O bhavcopy lacks column(s): {', '.join(missing)}")

    rows: list[FuturesContract] = []
    seen: set[str] = set()
    for raw in reader:
        if _field(raw, "FinInstrmTp") not in ("IDF", "STF"):
            continue
        symbol = _field(raw, "TckrSymb").upper()
        contract_name = _field(raw, "FinInstrmNm")
        if not symbol:
            continue
        trade_text = _field(raw, "TradDt")
        try:
            trade_date = datetime.strptime(trade_text, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"invalid TradDt {trade_text!r}") from exc
        if expected_date is not None and trade_date != expected_date:
            raise ValueError(
                f"the F&O bhavcopy contains {trade_date.isoformat()}, "
                f"expected {expected_date.isoformat()}"
            )
        identity = contract_name or "|".join(
            (symbol, _field(raw, "XpryDt"), _field(raw, "FinInstrmTp"))
        )
        if identity in seen:
            continue
        try:
            expiry = _expiry(_field(raw, "XpryDt"))
        except ValueError:
            continue
        seen.add(identity)
        rows.append(
            FuturesContract(
                trade_date=trade_date,
                symbol=symbol,
                instrument_type=_field(raw, "FinInstrmTp"),
                expiry=expiry,
                close=_decimal(_field(raw, "ClsPric")),
                settlement=_decimal(_field(raw, "SttlmPric")),
                previous_close=_decimal(_field(raw, "PrvsClsgPric")),
                underlying_price=_decimal(_field(raw, "UndrlygPric")),
                open_interest=_int(_field(raw, "OpnIntrst")),
                change_in_oi=_int(_field(raw, "ChngInOpnIntrst")),
                volume=_int(_field(raw, "TtlTradgVol")),
                value=_decimal(_field(raw, "TtlTrfVal")),
                trades=_int(_field(raw, "TtlNbOfTxsExctd")),
                lot_size=_int(_field(raw, "NewBrdLotQty")),
                contract_name=contract_name,
            )
        )
    return tuple(rows)


def build_futures_chains(
    contracts: Iterable[FuturesContract],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Group official futures by underlying and expiry without synthesising rows."""
    grouped: dict[str, dict[str, list[FuturesContract]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for contract in contracts:
        grouped[contract.symbol][contract.expiry.isoformat()].append(contract)

    chains: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol, expiries in grouped.items():
        chains[symbol] = {}
        for expiry, rows in expiries.items():
            contract = rows[0]
            chains[symbol][expiry] = {
                "expiry": expiry,
                "instrument_type": contract.instrument_type,
                "contract_name": contract.contract_name,
                "close": str(contract.close) if contract.close is not None else None,
                "settlement": (
                    str(contract.settlement) if contract.settlement is not None else None
                ),
                "previous_close": (
                    str(contract.previous_close)
                    if contract.previous_close is not None
                    else None
                ),
                "underlying_price": (
                    str(contract.underlying_price)
                    if contract.underlying_price is not None
                    else None
                ),
                "open_interest": contract.open_interest,
                "change_in_open_interest": contract.change_in_oi,
                "volume": contract.volume,
                "value": str(contract.value) if contract.value is not None else None,
                "trades": contract.trades,
                "lot_size": contract.lot_size,
            }
    return chains


def scan_futures_snapshot(
    snapshot: Mapping[str, Any],
    *,
    equities: Iterable[Mapping[str, Any]] = (),
    as_of: date | None = None,
) -> dict[str, Any]:
    """Scan official futures contracts without inventing a contract or value.

    The snapshot is the output of :func:`load_nse_fo_snapshot`.  Expired
    expiries are excluded against the snapshot trade date (or the supplied
    ``as_of`` date), the nearest remaining expiry is marked as the front
    contract, and an equity entry is attached only when the underlying symbol
    exists in the supplied catalogue rows.  Price change, OI change and the
    four buildup labels are calculations over provider values; they are
    reported separately from the provider fields.
    """
    raw_futures = snapshot.get("futures")
    raw_chains = raw_futures.get("chains") if isinstance(raw_futures, Mapping) else None
    trade_text = str(snapshot.get("trade_date") or "").strip()
    try:
        trade_date = date.fromisoformat(trade_text) if trade_text else None
    except ValueError:
        trade_date = None
    if trade_date is None:
        return {
            "supported": False,
            "status": "UNAVAILABLE",
            "rows": [],
            "coverage": {"contracts_registered": 0, "contracts_scanned": 0},
            "reason": "The official futures snapshot has no valid trade date.",
            "next_requirement": "Re-run scripts/import_nse_fo_bhavcopy.py.",
        }
    if not isinstance(raw_chains, Mapping) or not raw_chains:
        return {
            "supported": False,
            "status": "EMPTY",
            "rows": [],
            "coverage": {"contracts_registered": 0, "contracts_scanned": 0},
            "trade_date": trade_date.isoformat(),
            "reason": "The official futures snapshot contains no futures chains.",
            "next_requirement": "Re-run scripts/import_nse_fo_bhavcopy.py for a completed session.",
        }

    reference_date = as_of or trade_date
    equity_by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in equities:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        instrument_id = str(row.get("instrument_id") or "").strip()
        if symbol and instrument_id and symbol not in equity_by_symbol:
            equity_by_symbol[symbol] = row

    valid_by_symbol: dict[str, list[tuple[date, Mapping[str, Any]]]] = defaultdict(list)
    contracts_registered = 0
    expired_excluded = 0
    invalid_excluded = 0
    for raw_symbol, expiry_rows in raw_chains.items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or not isinstance(expiry_rows, Mapping):
            invalid_excluded += 1
            continue
        contracts_registered += len(expiry_rows)
        for raw_expiry, contract in expiry_rows.items():
            try:
                expiry = date.fromisoformat(str(raw_expiry))
            except ValueError:
                invalid_excluded += 1
                continue
            if not isinstance(contract, Mapping):
                invalid_excluded += 1
                continue
            if expiry < reference_date:
                expired_excluded += 1
                continue
            valid_by_symbol[symbol].append((expiry, contract))

    def number(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    def pct(current: float | None, previous: float | None) -> float | None:
        if current is None or previous is None or previous == 0:
            return None
        return round((current / previous - 1.0) * 100.0, 4)

    rows: list[dict[str, Any]] = []
    rollover: list[dict[str, Any]] = []
    for symbol in sorted(valid_by_symbol):
        expiries = sorted(expiry for expiry, _ in valid_by_symbol[symbol])
        front_expiry = expiries[0]
        next_expiry = expiries[1] if len(expiries) > 1 else None
        rollover.append(
            {
                "symbol": symbol,
                "front_expiry": front_expiry.isoformat(),
                "next_expiry": next_expiry.isoformat() if next_expiry else None,
                "expiry_count": len(expiries),
                "days_to_front": (front_expiry - reference_date).days,
            }
        )
        for expiry, contract in sorted(valid_by_symbol[symbol], key=lambda item: item[0]):
            instrument_type = str(contract.get("instrument_type") or "").upper()
            close = number(contract.get("close"))
            previous_close = number(contract.get("previous_close"))
            open_interest = int(number(contract.get("open_interest")) or 0)
            change_in_oi = int(number(contract.get("change_in_open_interest")) or 0)
            volume = int(number(contract.get("volume")) or 0)
            lot_size = int(number(contract.get("lot_size")) or 0)
            price_change_pct = pct(close, previous_close)
            prior_oi = open_interest - change_in_oi
            oi_change_pct = pct(float(open_interest), float(prior_oi)) if prior_oi > 0 else None
            direction = (
                "bullish"
                if price_change_pct is not None and price_change_pct > 0
                else "bearish"
                if price_change_pct is not None and price_change_pct < 0
                else "neutral"
                if price_change_pct == 0
                else "unknown"
            )
            if price_change_pct is None or oi_change_pct is None:
                buildup = "unclassified"
            elif price_change_pct > 0 and oi_change_pct > 0:
                buildup = "long_buildup"
            elif price_change_pct < 0 and oi_change_pct > 0:
                buildup = "short_buildup"
            elif price_change_pct > 0 and oi_change_pct < 0:
                buildup = "short_covering"
            elif price_change_pct < 0 and oi_change_pct < 0:
                buildup = "long_unwinding"
            else:
                buildup = "flat"
            mapped = equity_by_symbol.get(symbol)
            is_index = instrument_type == "IDF"
            mapping_status = (
                "index_underlying"
                if is_index
                else "mapped"
                if mapped is not None
                else "missing_equity_mapping"
            )
            contract_name = str(contract.get("contract_name") or "").strip()
            contract_id = contract_name or f"{symbol}:{expiry.isoformat()}:{instrument_type or 'FUT'}"
            rows.append(
                {
                    "contract_id": contract_id,
                    "contract_name": contract_name or contract_id,
                    "symbol": symbol,
                    "instrument_type": instrument_type,
                    "expiry": expiry.isoformat(),
                    "is_front": expiry == front_expiry,
                    "expiry_count": len(expiries),
                    "underlying_instrument_id": (
                        str(mapped.get("instrument_id")) if mapped is not None else None
                    ),
                    "underlying_name": (
                        str(mapped.get("name")) if mapped is not None and mapped.get("name") else None
                    ),
                    "mapping_status": mapping_status,
                    "mapped": mapped is not None,
                    "close": contract.get("close"),
                    "settlement": contract.get("settlement"),
                    "previous_close": contract.get("previous_close"),
                    "underlying_price": contract.get("underlying_price"),
                    "price_change_pct": price_change_pct,
                    "direction": direction,
                    "open_interest": open_interest,
                    "change_in_open_interest": change_in_oi,
                    "prior_open_interest": prior_oi,
                    "oi_change_pct": oi_change_pct,
                    "buildup": buildup,
                    "strength": abs(price_change_pct) if price_change_pct is not None else None,
                    "volume": volume,
                    "value": contract.get("value"),
                    "trades": int(number(contract.get("trades")) or 0),
                    "lot_size": lot_size,
                    "days_to_expiry": (expiry - reference_date).days,
                    "as_of": trade_date.isoformat(),
                    "data_state": "official_eod",
                    "source": snapshot.get("source"),
                    "oi_source": "provider",
                    "calculated_fields": [
                        "price_change_pct",
                        "oi_change_pct",
                        "direction",
                        "buildup",
                        "strength",
                    ],
                }
            )

    rows.sort(key=lambda row: (str(row["symbol"]), str(row["expiry"])))
    stale_days = max(0, (reference_date - trade_date).days)
    status = "EMPTY" if not rows else "STALE" if stale_days > 7 else "AVAILABLE"
    underlyings = sorted({str(row["symbol"]) for row in rows})
    coverage = {
        "contracts_registered": contracts_registered,
        "contracts_scanned": len(rows),
        "expired_excluded": expired_excluded,
        "invalid_excluded": invalid_excluded,
        "underlyings": len(underlyings),
        "index_contracts": sum(1 for row in rows if row["instrument_type"] == "IDF"),
        "stock_contracts": sum(1 for row in rows if row["instrument_type"] == "STF"),
        "mapped_contracts": sum(1 for row in rows if row["mapped"]),
        "unmapped_contracts": sum(1 for row in rows if row["mapping_status"] == "missing_equity_mapping"),
        "front_month_contracts": sum(1 for row in rows if row["is_front"]),
    }
    return {
        "supported": bool(rows),
        "status": status,
        "rows": rows,
        "coverage": coverage,
        "bullish": [row["contract_id"] for row in rows if row["direction"] == "bullish"],
        "bearish": [row["contract_id"] for row in rows if row["direction"] == "bearish"],
        "underlyings": underlyings,
        "rollover": rollover,
        "trade_date": trade_date.isoformat(),
        "as_of": trade_date.isoformat(),
        "generated_at": snapshot.get("generated_at"),
        "source": snapshot.get("source"),
        "stale_days": stale_days,
        "freshness": "STALE" if status == "STALE" else "EOD",
        "fields": [
            "expiry",
            "close",
            "previous_close",
            "open_interest",
            "change_in_open_interest",
            "volume",
            "lot_size",
        ],
        "reason": (
            "Official NSE index and stock futures with provider expiry, price, "
            "open interest, change in open interest, volume and lot size."
        ),
        "next_requirement": (
            "No unexpired official futures contract is available in the cached "
            "snapshot; re-import the current NSE F&O bhavcopy."
            if not rows
            else None
        ),
        "note": (
            "Price change, OI change, direction and buildup are calculated from "
            "provider close/previous close/OI/ΔOI. Missing inputs remain null. "
            "Equity mapping is by exact underlying symbol, never by name."
        ),
    }


def fetch_nse_fo_bhavcopy(trade_date: date, *, timeout: float = 45.0) -> bytes:
    url = NSE_FO_URL.format(day=trade_date.strftime("%Y%m%d"))
    request = urllib.request.Request(url, headers=_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body: bytes = response.read(_MAX_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DerivativesUnavailable(f"{trade_date}: NSE F&O bhavcopy unavailable: {exc}") from exc
    if len(body) > _MAX_BYTES:
        raise DerivativesUnavailable(f"{trade_date}: NSE F&O bhavcopy exceeded the size ceiling")
    return body


def load_nse_fo_snapshot(
    trade_date: date | None = None, *, payload: bytes | None = None
) -> dict[str, Any]:
    """Fetch/parse the official file and return the JSON snapshot document."""
    target = trade_date or _last_weekday(date.today())
    archive = payload if payload is not None else fetch_nse_fo_bhavcopy(target)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            name = next((item for item in bundle.namelist() if item.lower().endswith(".csv")), "")
            if not name:
                raise DerivativesUnavailable("the NSE F&O archive contains no CSV")
            text = bundle.read(name).decode("utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DerivativesUnavailable(f"the NSE F&O archive is not a valid zip: {exc}") from exc
    contracts = parse_nse_fo_csv(text, expected_date=target)
    futures = parse_nse_fo_futures_csv(text, expected_date=target)
    chains = build_option_chains(contracts)
    futures_chains = build_futures_chains(futures)
    symbols = sorted(chains)
    default_symbol = "NIFTY" if "NIFTY" in chains else (symbols[0] if symbols else "")
    expiries = sorted(chains.get(default_symbol, {}))
    default_expiry = expiries[0] if expiries else ""
    chosen = chains.get(default_symbol, {}).get(default_expiry, {})
    futures_symbols = sorted(futures_chains)
    default_futures_symbol = (
        "NIFTY" if "NIFTY" in futures_chains else (futures_symbols[0] if futures_symbols else "")
    )
    futures_expiries = sorted(futures_chains.get(default_futures_symbol, {}))
    default_futures_expiry = futures_expiries[0] if futures_expiries else ""
    return {
        "ok": bool(contracts),
        "source": "NSE BhavCopy_NSE_FO (official end-of-day F&O file)",
        "trade_date": target.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "contracts": len(contracts),
        "option_contracts": len(contracts),
        "futures_contracts": len(futures),
        "total_contracts": len(contracts) + len(futures),
        "underlyings": symbols,
        "futures_underlyings": futures_symbols,
        "chains": chains,
        "futures": {
            "contracts": len(futures),
            "underlyings": futures_symbols,
            "chains": futures_chains,
            "default_symbol": default_futures_symbol,
            "default_expiry": default_futures_expiry,
            "fields": [
                "expiry",
                "close",
                "settlement",
                "open_interest",
                "change_in_open_interest",
                "volume",
                "lot_size",
            ],
            "note": (
                "Official NSE index and stock futures. Open interest and "
                "volume are provider values; no implied volatility is "
                "reported for futures."
            ),
        },
        "default_symbol": default_symbol,
        "default_expiry": default_expiry,
        "default_chain": chosen,
        "iv_method": IV_METHOD,
        "fields": [
            "strike",
            "CE",
            "PE",
            "open_interest",
            "change_in_open_interest",
            "volume",
            "implied_volatility_pct",
            "pcr",
        ],
        "disclaimer": (
            "Official end-of-day NSE contracts. Implied volatility is computed "
            "from the official premium by Black-Scholes, not reported by NSE."
        ),
    }


def save_snapshot(snapshot: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _last_weekday(value: date) -> date:
    cursor = value
    while cursor.weekday() >= 5:
        cursor -= date.resolution
    return cursor


__all__ = [
    "DerivativesUnavailable",
    "FuturesContract",
    "IV_METHOD",
    "OptionContract",
    "build_futures_chains",
    "build_option_chains",
    "fetch_nse_fo_bhavcopy",
    "implied_volatility",
    "load_nse_fo_snapshot",
    "parse_nse_fo_futures_csv",
    "parse_nse_fo_csv",
    "save_snapshot",
    "scan_futures_snapshot",
]
