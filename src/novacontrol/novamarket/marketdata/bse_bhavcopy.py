"""Resumable ingestion of BSE's official end-of-day equity bhavcopy.

BSE publishes one official ``BhavCopy_BSE_CM`` file for the whole cash market.
Rows are written as canonical daily bars for BSE instruments that are already
in the catalogue.  The file is not reused as an NSE price: a BSE listing is a
different instrument, even when it carries the same ticker.

The importer keeps the same guarantees as the NSE importer: a full session is
parsed before any write, a checkpoint is written only after the whole session
has been appended, and a missing or server-substituted session is reported.
"""

from __future__ import annotations

import csv
import io
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from novacontrol.novamarket.marketdata.dedupe import (
    OFFICIAL_DAILY_PROVIDERS,
    bar_session_date,
)
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    OhlcvBar,
)
from novacontrol.novamarket.marketdata.nse_bhavcopy import (
    NseBhavcopyBar,
    _read_checkpoint,
    _write_checkpoint,
    trading_days,
)
from novacontrol.novamarket.marketdata.store import MarketDataStore

BSE_BHAVCOPY_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/"
    "BhavCopy_BSE_CM_0_0_0_{day}_F_0000.CSV"
)
_MAX_BYTES = 12_000_000
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}


class BseBhavcopyUnavailable(RuntimeError):
    """The session file could not be obtained; no rows were written."""


def _field(row: Mapping[str, Any], name: str) -> str:
    wanted = str(name or "").strip().upper()
    for key, value in row.items():
        if str(key or "").strip().upper() == wanted:
            return str(value if value is not None else "").strip()
    return ""


def _decimal(value: str, *, field: str, required: bool = True) -> Decimal | None:
    text = str(value or "").strip()
    if text in ("", "-", "NA", "N/A"):
        if required:
            raise ValueError(f"{field} is missing")
        return None
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not a decimal: {value!r}") from exc


def parse_bse_bhavcopy(
    text: str, *, expected_date: date | None = None
) -> tuple[NseBhavcopyBar, ...]:
    """Parse BSE's official cash-market file, keeping STK rows only."""
    if not text.strip():
        raise ValueError("the BSE bhavcopy was empty")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("the BSE bhavcopy has no header row")
    required = {
        "TRADDT",
        "FININSTRMTP",
        "TCKRSYMB",
        "SCTYSRS",
        "OPNPRIC",
        "HGHPRIC",
        "LWPRIC",
        "CLSPRIC",
    }
    fields = {str(name or "").strip().upper() for name in reader.fieldnames}
    missing = sorted(required - fields)
    if missing:
        raise ValueError(f"the BSE bhavcopy lacks column(s): {', '.join(missing)}")

    rows: list[NseBhavcopyBar] = []
    seen: set[str] = set()
    for raw in reader:
        if _field(raw, "FinInstrmTp").upper() != "STK":
            continue
        symbol = _field(raw, "TckrSymb").upper()
        if not symbol or symbol in seen:
            continue
        date_text = _field(raw, "TradDt")
        try:
            trade_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"{symbol}: invalid TradDt {date_text!r}") from exc
        if expected_date is not None and trade_date != expected_date:
            raise ValueError(
                f"the BSE bhavcopy contains {trade_date.isoformat()}, "
                f"expected {expected_date.isoformat()}"
            )
        try:
            open_price = _decimal(_field(raw, "OpnPric"), field="OpnPric")
            high = _decimal(_field(raw, "HghPric"), field="HghPric")
            low = _decimal(_field(raw, "LwPric"), field="LwPric")
            close = _decimal(_field(raw, "ClsPric"), field="ClsPric")
            volume = _decimal(_field(raw, "TtlTradgVol"), field="TtlTradgVol", required=False)
            previous_close = _decimal(
                _field(raw, "PrvsClsgPric"), field="PrvsClsgPric", required=False
            )
        except ValueError:
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
                series=_field(raw, "SctySrs").upper(),
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
        raise ValueError("the BSE bhavcopy contained no valid STK rows")
    return tuple(rows)


def bse_bhavcopy_url(trade_date: date) -> str:
    return BSE_BHAVCOPY_URL.format(day=trade_date.strftime("%Y%m%d"))


def fetch_bse_bhavcopy(
    trade_date: date, *, timeout: float = 30.0, attempts: int = 3
) -> str:
    url = bse_bhavcopy_url(trade_date)
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        request = urllib.request.Request(url, headers=_HEADERS, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body: bytes = response.read(_MAX_BYTES + 1)
                if len(body) > _MAX_BYTES:
                    raise BseBhavcopyUnavailable(
                        f"{trade_date}: BSE bhavcopy exceeded the size ceiling"
                    )
                return body.decode("utf-8-sig", errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                raise BseBhavcopyUnavailable(
                    f"{trade_date}: BSE has no bhavcopy for this session"
                ) from exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise BseBhavcopyUnavailable(
                    f"{trade_date}: BSE refused the bhavcopy with HTTP {exc.code}"
                ) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(min(2**attempt, 8))
    raise BseBhavcopyUnavailable(
        f"{trade_date}: BSE bhavcopy unavailable after {attempts} attempts "
        f"({type(last_error).__name__ if last_error else 'unknown error'})"
    )


def _registered_bse_instruments(store: MarketDataStore) -> dict[str, str]:
    mapping: dict[str, str] = {}
    offset = 0
    while True:
        page = store.list_instruments(exchange="BSE", limit=1000, offset=offset)
        for instrument in page.items:
            mapping[instrument.symbol.upper()] = instrument.instrument_id
        offset += len(page.items)
        if offset >= page.total or not page.items:
            break
    return mapping


def _same_session_bar(previous: Any, incoming: Any) -> bool:
    """Equal OHLCV means the same session, whichever provider stored it."""

    for name in ("open", "high", "low", "close"):
        if getattr(previous, name, None) != getattr(incoming, name, None):
            return False
    return bool(getattr(previous, "volume", None) == incoming.volume)


def import_bse_bhavcopy(
    store: MarketDataStore,
    *,
    start: date,
    end: date,
    checkpoint_path: Path | None = None,
    max_sessions: int = 30,
    fetch: Callable[[date], str] = fetch_bse_bhavcopy,
    clock: Callable[[], datetime] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Append official BSE bars for registered BSE symbols, resumably."""
    if max_sessions < 1:
        raise ValueError("max_sessions must be positive")
    now = (clock or (lambda: datetime.now(UTC)))()
    instruments = _registered_bse_instruments(store)
    if not instruments:
        raise ValueError("no BSE instruments are registered; sync the universe first")
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
    session_conflicts: list[dict[str, str]] = []

    # An official import must not add a second close for a session that
    # another provider already stored.  The store's unique key includes
    # the provider, so this check is by exchange-local session date.
    existing_sessions: dict[str, dict[date, Any]] = {}
    for registered_id in set(instruments.values()):
        page = store.bars(
            registered_id,
            interval=DataInterval.DAY_1,
            limit=1000,
            ascending=False,
        )
        existing_sessions[registered_id] = {
            day: bar
            for bar in page.items
            if (day := bar_session_date(bar)) is not None
        }

    for session in trading_days(start, end):
        if processed >= max_sessions:
            break
        key = session.isoformat()
        if key in completed:
            continue
        try:
            text = fetch(session)
            rows = parse_bse_bhavcopy(text, expected_date=session)
        except Exception as exc:  # noqa: BLE001 - one missing session is reported
            errors.append({"date": key, "error": f"{type(exc).__name__}: {exc}"})
            continue
        counts = {"inserted": 0, "duplicate": 0, "conflict": 0, "rejected": 0}
        registered = 0
        for row in rows:
            instrument_id = instruments.get(row.symbol)
            if instrument_id is None:
                continue
            registered += 1
            bar = OhlcvBar.create(
                instrument_id=instrument_id,
                provider="bse_bhavcopy",
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
                    "source": "BSE BhavCopy_BSE_CM",
                    "series": row.series,
                    "trade_date": key,
                    "previous_close": (
                        None if row.previous_close is None else str(row.previous_close)
                    ),
                },
                raw=row.raw,
            )
            prior = existing_sessions[instrument_id].get(row.trade_date)
            if prior is not None:
                prior_provider = str(getattr(prior, "provider", ""))
                if _same_session_bar(prior, bar):
                    counts["duplicate"] += 1
                    continue
                counts["conflict"] += 1
                official_prior = prior_provider in OFFICIAL_DAILY_PROVIDERS
                session_conflicts.append(
                    {
                        "symbol": row.symbol,
                        "date": key,
                        "existing_provider": prior_provider,
                        "existing_close": str(getattr(prior, "close", "")),
                        "incoming_provider": "bse_bhavcopy",
                        "incoming_close": str(row.close),
                        "action": (
                            "skipped_official_conflict"
                            if official_prior
                            else "official_session_appended"
                        ),
                    }
                )
                if official_prior:
                    continue
                # The official row is appended beside the quote-provider row:
                # nothing is overwritten, and the reader's deterministic policy
                # prefers the official close for that session.

            result = store.append_bar(bar)
            if result.inserted:
                counts["inserted"] += 1
                existing_sessions[instrument_id][row.trade_date] = bar
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
        sessions.append({"date": key, "rows": len(rows), "registered": registered, **counts})
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
            progress({"date": key, "rows": len(rows), "registered": registered, **counts})

    return {
        "ok": not errors,
        "provider": "bse_bhavcopy",
        "source": "BSE BhavCopy_BSE_CM",
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
        "duplicate_session_policy": (
            "one effective daily close per instrument and exchange session; "
            "an existing session is never rewritten, and a provider conflict "
            "is reported rather than merged"
        ),
        "session_conflict_count": len(session_conflicts),
        "session_conflicts": session_conflicts[:50],
        "sessions": sessions,
        "checkpoint": str(checkpoint) if checkpoint is not None else "",
    }


__all__ = [
    "BseBhavcopyUnavailable",
    "bse_bhavcopy_url",
    "fetch_bse_bhavcopy",
    "import_bse_bhavcopy",
    "parse_bse_bhavcopy",
]
