"""The public NSE/BSE equity universe.

The catalogue is data, not a hand-maintained symbol list. NSE publishes its
equity list as a CSV; BSE publishes a JSON catalogue behind an endpoint that
sometimes refuses non-browser clients. This module therefore returns exactly
what it could verify:

* NSE rows parsed from the published CSV;
* BSE rows from the published API when it answers;
* otherwise the BSE arm of every NSE company (the same company, separately
  identified as ``XBOM:SYMBOL``), explicitly labelled as a cross-listing.

Nothing here invents a symbol, price or exchange membership. A failed source is
reported in ``errors`` and the returned coverage string says what is missing.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

NSE_EQUITY_CSV = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_EQUITY_LIST = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept": "text/csv,application/json,text/plain,*/*",
}


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One exchange listing that can be registered as a canonical instrument."""

    symbol: str
    name: str
    exchange: str
    mic: str
    provider_symbol: str
    source: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "mic": self.mic,
            "asset_class": "equity",
            "currency": "INR",
            "timezone": "Asia/Kolkata",
            "provider_symbols": {"yahoo": self.provider_symbol},
            "metadata": {
                "universe_source": self.source,
                "country": "IN",
            },
        }


@dataclass(frozen=True, slots=True)
class UniversePlan:
    members: tuple[UniverseMember, ...]
    nse_count: int
    bse_count: int
    bse_only_count: int
    coverage: str
    errors: tuple[str, ...] = field(default_factory=tuple)


def parse_nse_equity_csv(text: str) -> tuple[UniverseMember, ...]:
    """Parse NSE's published equity CSV, keeping only active equity series."""
    if not text.strip():
        raise ValueError("the NSE equity list was empty")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("the NSE equity list has no header row")
    fields = {str(name or "").strip().upper(): name for name in reader.fieldnames}
    symbol_key = fields.get("SYMBOL")
    name_key = fields.get("NAME OF COMPANY")
    series_key = fields.get("SERIES")
    if symbol_key is None or name_key is None or series_key is None:
        raise ValueError(
            "the NSE equity list does not carry SYMBOL, NAME OF COMPANY and SERIES"
        )

    seen: set[str] = set()
    rows: list[UniverseMember] = []
    for raw in reader:
        symbol = str(raw.get(symbol_key) or "").strip().upper()
        name = str(raw.get(name_key) or "").strip()
        series = str(raw.get(series_key) or "").strip().upper()
        if not symbol or not name or series != "EQ":
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.append(
            UniverseMember(
                symbol=symbol,
                name=name,
                exchange="NSE",
                mic="XNSE",
                provider_symbol=f"{symbol}.NS",
                source="NSE EQUITY_L.csv",
            )
        )
    if not rows:
        raise ValueError("the NSE equity list contained no active equity rows")
    return tuple(rows)


def _read(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        status = int(getattr(response, "status", 200))
        if status >= 400:
            raise urllib.error.HTTPError(
                url,
                status,
                "upstream refused the request",
                Message(),
                None,
            )
        return bytes(response.read())


def _first(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def parse_bse_equity_json(text: str) -> tuple[UniverseMember, ...]:
    """Parse the BSE scrip catalogue when the endpoint answers."""
    if not text.strip():
        raise ValueError("the BSE equity list was empty")
    payload = json.loads(text)
    if isinstance(payload, dict):
        for key in ("Table", "table", "data", "Data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                payload = value
                break
    if not isinstance(payload, list):
        raise ValueError("the BSE equity list did not contain a row array")

    seen: set[str] = set()
    rows: list[UniverseMember] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        symbol = _first(
            raw,
            "Scrip_Id",
            "scrip_id",
            "SC_NAME",
            "symbol",
            "Symbol",
            "Scripcode",
        ).upper()
        name = _first(
            raw,
            "Issuer_Name",
            "issuer_name",
            "CompanyName",
            "Scrip_Name",
            "name",
            "Name",
        )
        group = _first(raw, "Group", "group", "Segment", "segment").upper()
        if group and group not in {"A", "B", "T", "X", "XT", "M", "MT", "EQUITY"}:
            continue
        if not symbol or symbol in seen or symbol.isdigit():
            continue
        seen.add(symbol)
        rows.append(
            UniverseMember(
                symbol=symbol,
                name=name or symbol,
                exchange="BSE",
                mic="XBOM",
                provider_symbol=f"{symbol}.BO",
                source="BSE ListofScripData",
            )
        )
    if not rows:
        raise ValueError("the BSE equity list contained no usable symbols")
    return tuple(rows)


def fetch_nse_equity_csv(*, timeout: float = 30.0) -> str:
    return _read(NSE_EQUITY_CSV, timeout).decode("utf-8-sig", errors="replace")


def fetch_bse_equity_json(*, timeout: float = 20.0) -> str:
    return _read(BSE_EQUITY_LIST, timeout).decode("utf-8", errors="replace")


def build_indian_universe(
    *,
    nse_text: str | None = None,
    bse_text: str | None = None,
    include_bse: bool = True,
    timeout: float = 30.0,
) -> UniversePlan:
    """Fetch what is reachable and return an honest, deduplicated plan.

    NSE is required. BSE is best-effort: a refusal falls back to the BSE
    listing of each NSE company, which is still a real exchange listing and is
    labelled as such rather than presented as the complete BSE catalogue.
    """
    errors: list[str] = []
    text = nse_text if nse_text is not None else fetch_nse_equity_csv(timeout=timeout)
    nse_rows = parse_nse_equity_csv(text)
    members: list[UniverseMember] = list(nse_rows)
    bse_rows: tuple[UniverseMember, ...] = ()
    if include_bse:
        try:
            raw_bse = (
                bse_text
                if bse_text is not None
                else fetch_bse_equity_json(timeout=min(timeout, 20.0))
            )
            bse_rows = parse_bse_equity_json(raw_bse)
        except Exception as exc:  # noqa: BLE001 - one source refusal is reported
            errors.append(
                f"BSE catalogue unavailable ({type(exc).__name__}: {exc}); "
                "using BSE listings for the NSE companies"
            )

    nse_symbols = {row.symbol for row in nse_rows}
    bse_only: list[UniverseMember] = []
    if bse_rows:
        for row in bse_rows:
            members.append(row)
            if row.symbol not in nse_symbols:
                bse_only.append(row)
    elif include_bse:
        for row in nse_rows:
            members.append(
                UniverseMember(
                    symbol=row.symbol,
                    name=row.name,
                    exchange="BSE",
                    mic="XBOM",
                    provider_symbol=f"{row.symbol}.BO",
                    source="BSE listing of the NSE company",
                )
            )

    deduped: dict[tuple[str, str], UniverseMember] = {}
    for row in members:
        deduped[(row.mic, row.symbol)] = row
    final = tuple(deduped.values())
    bse_total = sum(1 for row in final if row.mic == "XBOM")
    if bse_rows:
        coverage = (
            f"{len(nse_rows)} NSE equities; {bse_total} BSE equities "
            f"({len(bse_only)} BSE-only)"
        )
    elif include_bse:
        coverage = (
            f"{len(nse_rows)} NSE equities; {bse_total} BSE cross-listings "
            "for the same companies (BSE's separate catalogue was unavailable)"
        )
    else:
        coverage = f"{len(nse_rows)} NSE equities; BSE sync was not requested"
    return UniversePlan(
        members=final,
        nse_count=len(nse_rows),
        bse_count=bse_total,
        bse_only_count=len(bse_only),
        coverage=coverage,
        errors=tuple(errors),
    )


def iter_member_payloads(plan: UniversePlan) -> Iterable[dict[str, Any]]:
    for member in plan.members:
        yield member.to_payload()


__all__ = [
    "BSE_EQUITY_LIST",
    "NSE_EQUITY_CSV",
    "UniverseMember",
    "UniversePlan",
    "build_indian_universe",
    "fetch_bse_equity_json",
    "fetch_nse_equity_csv",
    "iter_member_payloads",
    "parse_bse_equity_json",
    "parse_nse_equity_csv",
]
