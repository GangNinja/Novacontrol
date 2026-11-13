"""Resumable ingestion of NSE's official end-of-day bhavcopy.

NSE publishes ``sec_bhavdata_full_<DDMMYYYY>.csv`` as its complete equity
session file.  This module is deliberately a bulk reader, not a second price
model: every row is converted into the canonical :class:`OhlcvBar` and appended
through :class:`MarketDataStore`, so validation, duplicate handling and
conflict retention are the same as every other provider.

The importer is bounded and resumable.  It fetches and parses a whole session
before appending any row, records completed sessions in a checkpoint, and
continues from that checkpoint after a restart.  A missing session (a weekend,
a holiday or an upstream refusal) is reported rather than filled with a
synthetic bar.
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    OhlcvBar,
)
from novacontrol.novamarket.marketdata.store import MarketDataStore

NSE_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/products/content/"
    "sec_bhavdata_full_{day}.csv"
)
IST = ZoneInfo("Asia/Kolkata")
_MAX_BYTES = 8_000_000
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}
_EQUITY_SERIES = frozenset({"EQ", "BE"})


class BhavcopyUnavailable(RuntimeError):
    """The session file could not be obtained; no rows were written."""


@dataclass(frozen=True, slots=True)
class NseBhavcopyBar:
    """One real equity row from an NSE session file."""

    symbol: str
    series: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    previous_close: Decimal | None
    raw: Mapping[str, Any]

    @property
    def observed_at(self) -> datetime:
        # The official close is 15:30 IST.  A date without a time is not a
        # usable observation instant, and UTC is the store's canonical frame.
        return datetime.combine(self.trade_date, dt_time(15, 30), tzinfo=IST).astimezone(
            UTC
        )


def _field(row: Mapping[str, Any], name: str) -> str:
    for key, value in row.items():
        if str(key or "").strip().upper() == name:
            return str(value if value is not None else "").strip()
    return ""


def _decimal(value: str, *, field: str, required: bool = True) -> Decimal | None:
    text = str(value or "").strip()
    if text in ("", "-", "NA", "N/A"):
        if required:
            raise ValueError(f"{field} is missing")
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not a decimal: {value!r}") from exc
    return number


def parse_nse_bhavcopy(
    text: str, *, expected_date: date | None = None
) -> tuple[NseBhavcopyBar, ...]:
    """Parse an official NSE bhavcopy, keeping active equity series only."""
    if not text.strip():
        raise ValueError("the NSE bhavcopy was empty")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("the NSE bhavcopy has no header row")
    required = {"SYMBOL", "SERIES", "DATE1", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE"}
    fields = {str(name or "").strip().upper() for name in reader.fieldnames}
    missing = sorted(required - fields)
    if missing:
        raise ValueError(f"the NSE bhavcopy lacks column(s): {', '.join(missing)}")

    rows: list[NseBhavcopyBar] = []
    seen: set[str] = set()
    for raw in reader:
        symbol = _field(raw, "SYMBOL").upper()
        series = _field(raw, "SERIES").upper()
        if not symbol or series not in _EQUITY_SERIES or symbol in seen:
            continue
        date_text = _field(raw, "DATE1")
        try:
            trade_date = datetime.strptime(date_text, "%d-%b-%Y").date()
        except ValueError as exc:
            raise ValueError(f"{symbol}: invalid DATE1 {date_text!r}") from exc
        if expected_date is not None and trade_date != expected_date:
            raise ValueError(
                f"the NSE bhavcopy contains {trade_date.isoformat()}, "
                f"expected {expected_date.isoformat()}"
            )
        try:
            open_price = _decimal(_field(raw, "OPEN_PRICE"), field="OPEN_PRICE")
            high = _decimal(_field(raw, "HIGH_PRICE"), field="HIGH_PRICE")
            low = _decimal(_field(raw, "LOW_PRICE"), field="LOW_PRICE")
            close = _decimal(_field(raw, "CLOSE_PRICE"), field="CLOSE_PRICE")
            volume = _decimal(_field(raw, "TTL_TRD_QNTY"), field="TTL_TRD_QNTY", required=False)
            previous_close = _decimal(
                _field(raw, "PREV_CLOSE"), field="PREV_CLOSE", required=False
            )
        except ValueError:
            # A row with no usable OHLC is not a bar.  Keep the rest of the
            # session; the rejected symbol is counted by the importer.
            continue
        assert open_price is not None and high is not None and low is not None and close is not None
        if min(open_price, high, low, close) <= 0 or high < low:
            continue
        if not low <= open_price <= high or not low <= close <= high:
            continue
        seen.add(symbol)
        rows.append(
            NseBhavcopyBar(
                symbol=symbol,
                series=series,
                trade_date=trade_date,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                previous_close=previous_close,
                raw={str(key): value for key, value in raw.items()},
            )
        )
    if not rows:
        raise ValueError("the NSE bhavcopy contained no valid equity rows")
    return tuple(rows)


def bhavcopy_url(trade_date: date) -> str:
    return NSE_BHAVCOPY_URL.format(day=trade_date.strftime("%d%m%Y"))


def fetch_nse_bhavcopy(
    trade_date: date, *, timeout: float = 30.0, attempts: int = 3
) -> str:
    """Fetch one session file with bounded retries; a 404 is a missing session."""
    url = bhavcopy_url(trade_date)
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        request = urllib.request.Request(url, headers=_HEADERS, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body: bytes = response.read(_MAX_BYTES + 1)
                if len(body) > _MAX_BYTES:
                    raise BhavcopyUnavailable(
                        f"{trade_date}: NSE bhavcopy exceeded the size ceiling"
                    )
                return body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                raise BhavcopyUnavailable(
                    f"{trade_date}: NSE has no bhavcopy for this session"
                ) from exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise BhavcopyUnavailable(
                    f"{trade_date}: NSE refused the bhavcopy with HTTP {exc.code}"
                ) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(min(2**attempt, 8))
    raise BhavcopyUnavailable(
        f"{trade_date}: NSE bhavcopy unavailable after {attempts} attempts "
        f"({type(last_error).__name__ if last_error else 'unknown error'})"
    )


def trading_days(start: date, end: date) -> tuple[date, ...]:
    """Weekdays in the inclusive range; holidays are discovered by the import."""
    if end < start:
        raise ValueError("end must not precede start")
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return tuple(days)


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"completed_dates": [], "sessions": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"completed_dates": [], "sessions": []}
    return payload if isinstance(payload, dict) else {"completed_dates": [], "sessions": []}


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _registered_nse_instruments(store: MarketDataStore) -> dict[str, str]:
    mapping: dict[str, str] = {}
    offset = 0
    while True:
        page = store.list_instruments(exchange="NSE", limit=1000, offset=offset)
        for instrument in page.items:
            mapping[instrument.symbol.upper()] = instrument.instrument_id
        offset += len(page.items)
        if offset >= page.total or not page.items:
            break
    return mapping


def import_nse_bhavcopy(
    store: MarketDataStore,
    *,
    start: date,
    end: date,
    checkpoint_path: Path | None = None,
    max_sessions: int = 30,
    fetch: Callable[[date], str] = fetch_nse_bhavcopy,
    clock: Callable[[], datetime] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Append real NSE bars for every registered NSE symbol, resumably.

    Only symbols already in the canonical catalogue are written.  A session is
    checkpointed only after all of its rows have been appended, so a retry
    cannot silently skip a half-imported day.  No row is synthesised when NSE
    has no file or a symbol is absent from the file.
    """
    if max_sessions < 1:
        raise ValueError("max_sessions must be positive")
    now = (clock or (lambda: datetime.now(UTC)))()
    instruments = _registered_nse_instruments(store)
    if not instruments:
        raise ValueError("no NSE instruments are registered; sync the universe first")
    checkpoint = Path(checkpoint_path) if checkpoint_path is not None else None
    state = _read_checkpoint(checkpoint) if checkpoint is not None else {
        "completed_dates": [],
        "sessions": [],
    }
    completed = {str(item) for item in state.get("completed_dates") or []}
    sessions: list[dict[str, Any]] = list(state.get("sessions") or [])
    inserted = duplicate = conflict = rejected = 0
    processed = 0
    errors: list[dict[str, str]] = []

    for session in trading_days(start, end):
        if processed >= max_sessions:
            break
        key = session.isoformat()
        if key in completed:
            continue
        try:
            text = fetch(session)
            rows = parse_nse_bhavcopy(text, expected_date=session)
        except Exception as exc:  # noqa: BLE001 - one missing session is reported
            errors.append({"date": key, "error": f"{type(exc).__name__}: {exc}"})
            continue
        counts = {"inserted": 0, "duplicate": 0, "conflict": 0, "rejected": 0}
        for row in rows:
            instrument_id = instruments.get(row.symbol)
            if instrument_id is None:
                continue
            bar = OhlcvBar.create(
                instrument_id=instrument_id,
                provider="nse_bhavcopy",
                interval=DataInterval.DAY_1,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                currency="INR",
                observed_at=row.observed_at,
                retrieved_at=now,
                source_timezone="Asia/Kolkata",
                quality=DataQuality.VALID,
                freshness=Freshness.CLOSED_MARKET,
                partial=False,
                provider_metadata={
                    "source": "NSE sec_bhavdata_full",
                    "series": row.series,
                    "trade_date": key,
                    "previous_close": (
                        None if row.previous_close is None else str(row.previous_close)
                    ),
                },
                raw=row.raw,
            )
            result = store.append_bar(bar)
            if result.inserted:
                counts["inserted"] += 1
            elif result.duplicate:
                counts["duplicate"] += 1
            elif result.conflict:
                counts["conflict"] += 1
            else:
                counts["rejected"] += 1
        inserted += counts["inserted"]
        duplicate += counts["duplicate"]
        conflict += counts["conflict"]
        rejected += counts["rejected"]
        sessions.append(
            {
                "date": key,
                "rows": len(rows),
                "registered": sum(1 for row in rows if row.symbol in instruments),
                **counts,
            }
        )
        completed.add(key)
        processed += 1
        if checkpoint is not None:
            _write_checkpoint(
                checkpoint,
                {
                    "completed_dates": sorted(completed),
                    "sessions": sessions,
                    "inserted": inserted,
                    "duplicate": duplicate,
                    "conflict": conflict,
                    "rejected": rejected,
                    "updated_at": now.isoformat(),
                },
            )
        if progress is not None:
            progress(
                {
                    "date": key,
                    "rows": len(rows),
                    "registered": sum(1 for row in rows if row.symbol in instruments),
                    **counts,
                }
            )

    return {
        "ok": not errors,
        "provider": "nse_bhavcopy",
        "source": "NSE sec_bhavdata_full",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions_completed": len(completed),
        "sessions_processed_this_run": processed,
        "registered_instruments": len(instruments),
        "inserted": inserted,
        "duplicate": duplicate,
        "conflict": conflict,
        "rejected": rejected,
        "errors": errors,
        "sessions": sessions,
        "checkpoint": str(checkpoint) if checkpoint is not None else "",
    }


__all__ = [
    "BhavcopyUnavailable",
    "NseBhavcopyBar",
    "bhavcopy_url",
    "fetch_nse_bhavcopy",
    "import_nse_bhavcopy",
    "parse_nse_bhavcopy",
    "trading_days",
]
