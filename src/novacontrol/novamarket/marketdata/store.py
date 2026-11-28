"""The durable append-only market-data store.

Why SQLite and not the JSON state store
---------------------------------------
NovaControl's :class:`~novacontrol.persistence.JsonStateStore` is a small
snapshot store: every write replaces a whole JSON file. That is right for
configuration and wrong for a time series. A price history needs indexed range
queries, deterministic duplicate handling, pagination without reading everything
into memory, and a write that cannot rewrite yesterday's observation. Rewriting a
whole snapshot for each quote would be both slow and a corruption risk.

SQLite is already the project's local durable engine (``memory/stores.py`` uses
stdlib ``sqlite3``), so this adds NO dependency and no second database server. The
file is ``data/novamarket.sqlite3`` by default; one file, namespaced tables, WAL
mode, foreign keys on, and a versioned schema via ``PRAGMA user_version``.

History rules, implemented rather than promised
-----------------------------------------------
* There is no UPDATE of a price or an OHLC field. A repeated identity is a
  duplicate (idempotent ingestion); a repeated identity with a DIFFERENT value is
  a conflict, recorded in ``market_corrections`` and the stored record is marked
  SUSPECT. Neither silently overwrites the other.
* Duplicate ingestion is determined by the natural identity
  ``(instrument, provider, observed_at)`` plus a deterministic record id.
* Retrieval and quality logs are capped; observations and bars are not silently
  pruned. A correction keeps both values.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any

from novacontrol.novamarket.contracts import (
    MarketDataStoreError,
    NotOperationalError,
)
from novacontrol.novamarket.marketdata.models import (
    AppendResult,
    AppendStatus,
    DataInterval,
    DataQuality,
    Freshness,
    Instrument,
    OhlcvBar,
    PriceObservation,
    ProviderHealth,
    ProviderStatus,
    TradingStatus,
    worst_quality,
)

#: The durable schema this build writes. A file from a newer build is refused
#: rather than guessed at.
MARKET_DATA_SCHEMA_VERSION = 1

#: How many retrieval/quality rows are retained. Observations are history and
#: are never dropped by this retention; operational logs are observability.
LOG_RETENTION = 5_000


def _json(payload: Mapping[str, Any] | Sequence[Any] | None) -> str:
    return json.dumps(payload if payload is not None else {}, default=str, sort_keys=True)


def _loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _loads_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market-data timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _decimal(value: str | int | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True, slots=True)
class InstrumentPage:
    items: tuple[Instrument, ...]
    total: int
    limit: int
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.offset + len(self.items) < self.total,
        }


@dataclass(frozen=True, slots=True)
class ObservationPage:
    items: tuple[PriceObservation, ...]
    total: int
    limit: int
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.offset + len(self.items) < self.total,
        }


@dataclass(frozen=True, slots=True)
class BarPage:
    items: tuple[OhlcvBar, ...]
    total: int
    limit: int
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.offset + len(self.items) < self.total,
        }


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS market_instruments (
    instrument_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    exchange TEXT NOT NULL,
    mic TEXT NOT NULL,
    currency TEXT NOT NULL,
    timezone TEXT NOT NULL,
    provider_symbols_json TEXT NOT NULL,
    trading_status TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_market_instruments_class
    ON market_instruments(asset_class, exchange);

CREATE TABLE IF NOT EXISTS market_observations (
    observation_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL
        REFERENCES market_instruments(instrument_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    price TEXT NOT NULL,
    currency TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_timezone TEXT NOT NULL,
    quality TEXT NOT NULL,
    freshness TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    provider_metadata_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(instrument_id, provider, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_market_observations_latest
    ON market_observations(instrument_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_market_observations_provider
    ON market_observations(instrument_id, provider, observed_at DESC);

CREATE TABLE IF NOT EXISTS market_bars (
    bar_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL
        REFERENCES market_instruments(instrument_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    interval TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT,
    currency TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_timezone TEXT NOT NULL,
    quality TEXT NOT NULL,
    freshness TEXT NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0,
    reasons_json TEXT NOT NULL,
    provider_metadata_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(instrument_id, provider, interval, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_market_bars_query
    ON market_bars(instrument_id, interval, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_market_bars_provider
    ON market_bars(instrument_id, provider, interval, observed_at DESC);

CREATE TABLE IF NOT EXISTS market_provider_health (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    latency_ms REAL,
    verified_operations_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_retrieval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    instrument_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    outcome TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT,
    records INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_market_retrieval_provider
    ON market_retrieval_log(provider, started_at DESC);

CREATE TABLE IF NOT EXISTS market_quality_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    previous_quality TEXT,
    quality TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_market_quality_record
    ON market_quality_events(record_id, at DESC);

CREATE TABLE IF NOT EXISTS market_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    previous_payload_json TEXT NOT NULL,
    incoming_payload_json TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_market_corrections_record
    ON market_corrections(record_id, detected_at DESC);
"""


class MarketDataStore:
    """A thread-safe, append-only SQLite repository for market data."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Any | None = None,
    ) -> None:
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path = str(path)
        if self.path != ":memory:":
            try:
                Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise MarketDataStoreError(
                    f"market-data directory is not writable: {exc}"
                ) from exc
        try:
            self._connection = sqlite3.connect(
                self.path,
                check_same_thread=False,
                timeout=5.0,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except sqlite3.Error as exc:
            raise MarketDataStoreError(
                f"market-data store could not be opened: {exc}"
            ) from exc

    # -- schema ---------------------------------------------------------------

    def _migrate(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > MARKET_DATA_SCHEMA_VERSION:
            raise MarketDataStoreError(
                f"market-data schema {version} was written by a newer build; "
                f"this build speaks {MARKET_DATA_SCHEMA_VERSION}"
            )
        if version < 1:
            with self._connection:
                self._connection.executescript(_SCHEMA_V1)
                self._connection.execute(
                    f"PRAGMA user_version={MARKET_DATA_SCHEMA_VERSION}"
                )
        self._connection.commit()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- instruments ----------------------------------------------------------

    def upsert_instrument(self, instrument: Instrument) -> Instrument:
        now = _iso(self._clock())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO market_instruments (
                    instrument_id, symbol, name, asset_class, exchange, mic,
                    currency, timezone, provider_symbols_json, trading_status,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id) DO UPDATE SET
                    name=excluded.name,
                    asset_class=excluded.asset_class,
                    exchange=excluded.exchange,
                    mic=excluded.mic,
                    currency=excluded.currency,
                    timezone=excluded.timezone,
                    provider_symbols_json=excluded.provider_symbols_json,
                    trading_status=excluded.trading_status,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    instrument.instrument_id,
                    instrument.symbol,
                    instrument.name,
                    instrument.asset_class.value,
                    instrument.exchange,
                    instrument.mic,
                    instrument.currency,
                    instrument.timezone,
                    _json(instrument.provider_symbols),
                    instrument.trading_status.value,
                    _json(instrument.metadata),
                    now,
                    now,
                ),
            )
        return instrument

    def get_instrument(self, instrument_id: str) -> Instrument | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM market_instruments WHERE instrument_id=?",
                (str(instrument_id),),
            ).fetchone()
        return self._instrument(row) if row is not None else None

    def require_instrument(self, instrument_id: str) -> Instrument:
        instrument = self.get_instrument(instrument_id)
        if instrument is None:
            raise MarketDataStoreError(
                f"instrument {instrument_id!r} is not registered; register it "
                "before storing market data"
            )
        return instrument

    def list_instruments(
        self,
        *,
        asset_class: str | None = None,
        exchange: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> InstrumentPage:
        limit, offset = self._page(limit, offset)
        clauses: list[str] = []
        params: list[Any] = []
        if asset_class:
            clauses.append("asset_class=?")
            params.append(str(asset_class).strip().lower())
        if exchange:
            clauses.append("UPPER(exchange)=?")
            params.append(str(exchange).strip().upper())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM market_instruments{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT * FROM market_instruments{where} "
                "ORDER BY instrument_id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return InstrumentPage(
            tuple(self._instrument(row) for row in rows), total, limit, offset
        )

    def _instrument(self, row: sqlite3.Row) -> Instrument:
        return Instrument(
            instrument_id=row["instrument_id"],
            symbol=row["symbol"],
            name=row["name"],
            asset_class=row["asset_class"],
            exchange=row["exchange"],
            mic=row["mic"],
            currency=row["currency"],
            timezone=row["timezone"],
            provider_symbols=_loads(row["provider_symbols_json"]),
            trading_status=TradingStatus(row["trading_status"]),
            metadata=_loads(row["metadata_json"]),
        )

    # -- observations ---------------------------------------------------------

    def append_observation(self, observation: PriceObservation) -> AppendResult:
        self.require_instrument(observation.instrument_id)
        now = _iso(self._clock())
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO market_observations (
                    observation_id, instrument_id, provider, price, currency,
                    observed_at, retrieved_at, source_timezone, quality,
                    freshness, reasons_json, provider_metadata_json, raw_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.instrument_id,
                    observation.provider,
                    format(observation.price, "f"),
                    observation.currency,
                    _iso(observation.observed_at),
                    _iso(observation.retrieved_at),
                    observation.source_timezone,
                    observation.quality.value,
                    observation.freshness.value,
                    _json(list(observation.reasons)),
                    _json(observation.provider_metadata),
                    _json(observation.raw),
                    now,
                ),
            )
            if cursor.rowcount == 1:
                return AppendResult(
                    AppendStatus.INSERTED, observation.observation_id
                )
            existing = self._connection.execute(
                "SELECT * FROM market_observations WHERE observation_id=?",
                (observation.observation_id,),
            ).fetchone()
        if existing is None:
            return AppendResult(
                AppendStatus.CONFLICT,
                observation.observation_id,
                "the insert was ignored but no existing record was found",
            )
        return self._resolve_conflict(
            kind="observation",
            row=existing,
            incoming_id=observation.observation_id,
            incoming_payload=observation.to_dict(include_raw=True),
            previous_payload={
                "price": existing["price"],
                "currency": existing["currency"],
                "observed_at": existing["observed_at"],
            },
            differs=(
                Decimal(existing["price"]) != observation.price
                or existing["currency"] != observation.currency
            ),
        )

    def _resolve_conflict(
        self,
        *,
        kind: str,
        row: sqlite3.Row,
        incoming_id: str,
        incoming_payload: Mapping[str, Any],
        previous_payload: Mapping[str, Any],
        differs: bool,
    ) -> AppendResult:
        record_id = incoming_id
        if not differs:
            return AppendResult(AppendStatus.DUPLICATE, record_id)
        now = _iso(self._clock())
        previous_quality = DataQuality(row["quality"])
        reasons = _loads_list(row["reasons_json"])
        reason = (
            "a provider returned a different value for the same observation "
            "identity; the original was retained and this conflict was recorded"
        )
        reasons = list(dict.fromkeys([*reasons, reason]))
        quality = worst_quality(previous_quality, DataQuality.SUSPECT)
        table = (
            "market_observations" if kind == "observation" else "market_bars"
        )
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE {table} SET quality=?, reasons_json=? WHERE "
                + ("observation_id=?" if kind == "observation" else "bar_id=?"),
                (quality.value, _json(reasons), record_id),
            )
            self._connection.execute(
                """
                INSERT INTO market_corrections (
                    record_kind, record_id, instrument_id, provider,
                    previous_payload_json, incoming_payload_json, detected_at, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    record_id,
                    row["instrument_id"],
                    row["provider"],
                    _json(previous_payload),
                    _json(incoming_payload),
                    now,
                    reason,
                ),
            )
            if quality is not previous_quality:
                self._connection.execute(
                    """
                    INSERT INTO market_quality_events (
                        record_kind, record_id, instrument_id, provider,
                        previous_quality, quality, reasons_json, at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        record_id,
                        row["instrument_id"],
                        row["provider"],
                        previous_quality.value,
                        quality.value,
                        _json(reasons),
                        now,
                    ),
                )
            self._prune_logs()
        return AppendResult(
            AppendStatus.CONFLICT,
            record_id,
            "a conflicting value was preserved as a correction; the original "
            "observation was not overwritten",
        )

    def get_observation(self, observation_id: str) -> PriceObservation | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM market_observations WHERE observation_id=?",
                (str(observation_id),),
            ).fetchone()
        return self._observation(row) if row is not None else None

    def observation_stored_at(self, observation_id: str) -> datetime | None:
        """The durable write timestamp for one observation, if it exists.

        ``retrieved_at`` is when the provider response was received; this is
        the separate persistence instant, used by the PI-03 latency trace.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT created_at FROM market_observations WHERE observation_id=?",
                (str(observation_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            return _aware(str(row["created_at"]))
        except (TypeError, ValueError):
            return None

    def latest_observations(
        self, instrument_id: str, *, providers: Sequence[str] | None = None
    ) -> tuple[PriceObservation, ...]:
        params: list[Any] = [str(instrument_id)]
        provider_clause = ""
        if providers:
            placeholders = ",".join("?" for _ in providers)
            provider_clause = f" AND provider IN ({placeholders})"
            params.extend(str(item).strip().lower() for item in providers)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY provider ORDER BY observed_at DESC
                    ) AS rank
                    FROM market_observations
                    WHERE instrument_id=?{provider_clause}
                ) WHERE rank=1 ORDER BY observed_at DESC
                """,
                params,
            ).fetchall()
        return tuple(self._observation(row) for row in rows)

    def observations(
        self,
        instrument_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        provider: str | None = None,
        limit: int = 100,
        offset: int = 0,
        ascending: bool = False,
    ) -> ObservationPage:
        limit, offset = self._page(limit, offset)
        clauses = ["instrument_id=?"]
        params: list[Any] = [str(instrument_id)]
        if start is not None:
            clauses.append("observed_at >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("observed_at <= ?")
            params.append(_iso(end))
        if provider:
            clauses.append("provider=?")
            params.append(str(provider).strip().lower())
        where = " WHERE " + " AND ".join(clauses)
        order = "ASC" if ascending else "DESC"
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM market_observations{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT * FROM market_observations{where} "
                f"ORDER BY observed_at {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return ObservationPage(
            tuple(self._observation(row) for row in rows), total, limit, offset
        )

    def _observation(self, row: sqlite3.Row) -> PriceObservation:
        return PriceObservation(
            observation_id=row["observation_id"],
            instrument_id=row["instrument_id"],
            provider=row["provider"],
            price=Decimal(row["price"]),
            currency=row["currency"],
            observed_at=_aware(row["observed_at"]),
            retrieved_at=_aware(row["retrieved_at"]),
            source_timezone=row["source_timezone"],
            quality=DataQuality(row["quality"]),
            freshness=Freshness(row["freshness"]),
            reasons=tuple(_loads_list(row["reasons_json"])),
            provider_metadata=_loads(row["provider_metadata_json"]),
            raw=_loads(row["raw_json"]),
        )

    # -- OHLCV ----------------------------------------------------------------

    def append_bar(self, bar: OhlcvBar) -> AppendResult:
        return self.append_bars((bar,))[0]

    def append_bars(self, bars: Sequence[OhlcvBar]) -> tuple[AppendResult, ...]:
        """Append a page of bars in one transaction.

        Per-row conflict semantics are unchanged: an existing row with the same
        bar id is compared and recorded exactly as before.  Only the transaction
        boundary changed - a provider page commits once instead of once per
        bar - so ingestion no longer blocks the event loop with N synchronous
        SQLite commits.  A failure while inserting rolls the whole page back;
        no bar is lost or duplicated.
        """

        rows = tuple(bars)
        if not rows:
            return ()
        for instrument_id in {bar.instrument_id for bar in rows}:
            self.require_instrument(instrument_id)
        now = _iso(self._clock())
        results: list[AppendResult | None] = [None] * len(rows)
        pending: list[int] = []
        with self._lock, self._connection:
            for index, bar in enumerate(rows):
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO market_bars (
                        bar_id, instrument_id, provider, interval, open, high,
                        low, close, volume, currency, observed_at, retrieved_at,
                        source_timezone, quality, freshness, partial,
                        reasons_json, provider_metadata_json, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bar.bar_id,
                        bar.instrument_id,
                        bar.provider,
                        bar.interval.value,
                        format(bar.open, "f"),
                        format(bar.high, "f"),
                        format(bar.low, "f"),
                        format(bar.close, "f"),
                        None if bar.volume is None else format(bar.volume, "f"),
                        bar.currency,
                        _iso(bar.observed_at),
                        _iso(bar.retrieved_at),
                        bar.source_timezone,
                        bar.quality.value,
                        bar.freshness.value,
                        1 if bar.partial else 0,
                        _json(list(bar.reasons)),
                        _json(bar.provider_metadata),
                        _json(bar.raw),
                        now,
                    ),
                )
                if cursor.rowcount == 1:
                    results[index] = AppendResult(
                        AppendStatus.INSERTED, bar.bar_id
                    )
                else:
                    pending.append(index)
        for index in pending:
            bar = rows[index]
            existing = self._connection.execute(
                "SELECT * FROM market_bars WHERE bar_id=?", (bar.bar_id,)
            ).fetchone()
            if existing is None:
                results[index] = AppendResult(
                    AppendStatus.CONFLICT,
                    bar.bar_id,
                    "the insert was ignored but no existing bar was found",
                )
                continue
            differs = any(
                Decimal(existing[name]) != getattr(bar, name)
                for name in ("open", "high", "low", "close")
            ) or (
                (existing["volume"] is None) != (bar.volume is None)
                or (
                    existing["volume"] is not None
                    and bar.volume is not None
                    and Decimal(existing["volume"]) != bar.volume
                )
            )
            results[index] = self._resolve_conflict(
                kind="bar",
                row=existing,
                incoming_id=bar.bar_id,
                incoming_payload=bar.to_dict(include_raw=True),
                previous_payload={
                    "open": existing["open"],
                    "high": existing["high"],
                    "low": existing["low"],
                    "close": existing["close"],
                    "volume": existing["volume"],
                    "observed_at": existing["observed_at"],
                },
                differs=differs,
            )
        return tuple(result for result in results if result is not None)

    def get_bar(self, bar_id: str) -> OhlcvBar | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM market_bars WHERE bar_id=?", (str(bar_id),)
            ).fetchone()
        return self._bar(row) if row is not None else None

    def get_bars(self, bar_ids: Sequence[str]) -> dict[str, OhlcvBar]:
        """Bulk read for dataset retrieval, chunked below SQLite's parameter cap.

        A dataset version stores exact bar ids.  Reading them one at a time would
        turn a page into hundreds of round trips, so this is the one batched
        door; a missing id is simply absent from the result (the caller reports
        it rather than substituting another bar).
        """
        wanted = [str(value) for value in dict.fromkeys(bar_ids) if str(value)]
        found: dict[str, OhlcvBar] = {}
        for index in range(0, len(wanted), 500):
            chunk = wanted[index : index + 500]
            placeholders = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self._connection.execute(
                    f"SELECT * FROM market_bars WHERE bar_id IN ({placeholders})",
                    chunk,
                ).fetchall()
            for row in rows:
                bar = self._bar(row)
                found[bar.bar_id] = bar
        return found

    def bar_inventory(
        self,
        *,
        interval: DataInterval | str = DataInterval.DAY_1,
        min_bars: int = 1,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Instruments with a stored series, newest series first.

        This is a read-only index over the existing bars table. The desk uses it
        so a board never pretends that a catalogue entry without a bar has a
        price; nothing here fabricates or synthesises a missing series.
        """
        if isinstance(min_bars, bool) or not isinstance(min_bars, int) or min_bars < 1:
            raise ValueError("min_bars must be a positive integer")
        limit, offset = self._page(limit, offset)
        canonical = DataInterval(interval).value
        where = "WHERE interval=?"
        params: list[Any] = [canonical]
        having = "HAVING COUNT(*) >= ?"
        with self._lock:
            total = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT instrument_id FROM market_bars "
                    f"{where} GROUP BY instrument_id {having}"
                    ")",
                    (*params, min_bars),
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                "SELECT instrument_id, COUNT(*) AS bar_count, "
                "MAX(observed_at) AS latest_observed_at "
                "FROM market_bars "
                f"{where} GROUP BY instrument_id {having} "
                "ORDER BY latest_observed_at DESC, instrument_id ASC LIMIT ? OFFSET ?",
                (*params, min_bars, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    "instrument_id": str(row["instrument_id"]),
                    "bar_count": int(row["bar_count"]),
                    "latest_observed_at": row["latest_observed_at"],
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
            "min_bars": min_bars,
            "interval": canonical,
        }

    def board_rows(self, *, exchange: str | None = None) -> list[dict[str, Any]]:
        """One read for the Market Overview screener.

        The latest valid observation and the latest two daily bars are joined
        without N+1 reads. A missing observation/bar stays NULL; the caller
        decides how to label it. No price is inferred from another provider,
        another session or another instrument.
        """
        clauses: list[str] = []
        params: list[Any] = [DataInterval.DAY_1.value, DataInterval.DAY_1.value]
        if exchange and str(exchange).strip().lower() not in ("", "all"):
            clauses.append("UPPER(i.exchange)=?")
            params.append(str(exchange).strip().upper())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            WITH latest_observation AS (
                SELECT * FROM (
                    SELECT o.*, ROW_NUMBER() OVER (
                        PARTITION BY o.instrument_id
                        ORDER BY o.observed_at DESC, o.retrieved_at DESC
                    ) AS rn
                    FROM market_observations o
                    WHERE o.quality != 'invalid'
                ) WHERE rn = 1
            ),
            ranked_bars AS (
                SELECT * FROM (
                    SELECT b.*, ROW_NUMBER() OVER (
                        PARTITION BY b.instrument_id
                        ORDER BY b.observed_at DESC
                    ) AS rn
                    FROM market_bars b
                    WHERE b.interval = ? AND b.quality != 'invalid'
                ) WHERE rn <= 2
            ),
            average_volume AS (
                SELECT instrument_id, AVG(volume) AS average_volume
                FROM (
                    SELECT instrument_id, volume, ROW_NUMBER() OVER (
                        PARTITION BY instrument_id ORDER BY observed_at DESC
                    ) AS rn
                    FROM market_bars
                    WHERE interval = ? AND volume IS NOT NULL AND quality != 'invalid'
                )
                WHERE rn BETWEEN 2 AND 21
                GROUP BY instrument_id
            )
            SELECT
                i.instrument_id, i.symbol, i.name, i.exchange, i.mic,
                i.asset_class, i.currency, i.metadata_json,
                o.provider, o.price, o.observed_at, o.retrieved_at,
                o.quality, o.freshness, o.provider_metadata_json,
                o.raw_json AS observation_raw_json,
                b.open AS bar_open, b.high AS bar_high, b.low AS bar_low,
                b.close AS bar_close, b.volume AS bar_volume,
                b.observed_at AS bar_observed_at, b.partial AS bar_partial,
                b.provider AS bar_provider,
                p.close AS previous_bar_close,
                v.average_volume
            FROM market_instruments i
            LEFT JOIN latest_observation o ON o.instrument_id = i.instrument_id
            LEFT JOIN ranked_bars b ON b.instrument_id = i.instrument_id AND b.rn = 1
            LEFT JOIN ranked_bars p ON p.instrument_id = i.instrument_id AND p.rn = 2
            LEFT JOIN average_volume v ON v.instrument_id = i.instrument_id
            {where}
            ORDER BY i.instrument_id
        """
        if self.path == ":memory:":
            with self._lock:
                rows = self._connection.execute(sql, tuple(params)).fetchall()
        else:
            # This board query scans the full daily-bar history and can take
            # about a second. Running it on the shared connection holds the
            # store lock and makes /market/status wait behind it. WAL permits a
            # concurrent reader, so this query gets its own read-only connection
            # and leaves the status path's connection free.
            reader = sqlite3.connect(
                f"{Path(self.path).resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            reader.row_factory = sqlite3.Row
            try:
                rows = reader.execute(sql, tuple(params)).fetchall()
            finally:
                reader.close()
        return [
            {
                **dict(row),
                "metadata": _loads(row["metadata_json"]),
                "observation_metadata": _loads(row["provider_metadata_json"]),
                "observation_raw": _loads(row["observation_raw_json"]),
            }
            for row in rows
        ]

    def bars(
        self,
        instrument_id: str,
        *,
        interval: DataInterval | str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        provider: str | None = None,
        limit: int = 100,
        offset: int = 0,
        ascending: bool = False,
    ) -> BarPage:
        limit, offset = self._page(limit, offset)
        clauses = ["instrument_id=?"]
        params: list[Any] = [str(instrument_id)]
        if interval is not None:
            clauses.append("interval=?")
            params.append(DataInterval(interval).value)
        if start is not None:
            clauses.append("observed_at >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("observed_at <= ?")
            params.append(_iso(end))
        if provider:
            clauses.append("provider=?")
            params.append(str(provider).strip().lower())
        where = " WHERE " + " AND ".join(clauses)
        order = "ASC" if ascending else "DESC"
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM market_bars{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT * FROM market_bars{where} "
                f"ORDER BY observed_at {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return BarPage(tuple(self._bar(row) for row in rows), total, limit, offset)

    def recent_bars(
        self,
        instrument_id: str,
        *,
        provider: str,
        interval: DataInterval | str,
        before: datetime,
        limit: int = 20,
    ) -> tuple[OhlcvBar, ...]:
        limit, _ = self._page(limit, 0)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM market_bars
                WHERE instrument_id=? AND provider=? AND interval=?
                  AND observed_at < ?
                ORDER BY observed_at DESC LIMIT ?
                """,
                (
                    str(instrument_id),
                    str(provider).strip().lower(),
                    DataInterval(interval).value,
                    _iso(before),
                    limit,
                ),
            ).fetchall()
        return tuple(self._bar(row) for row in reversed(rows))

    def _bar(self, row: sqlite3.Row) -> OhlcvBar:
        return OhlcvBar(
            bar_id=row["bar_id"],
            instrument_id=row["instrument_id"],
            provider=row["provider"],
            interval=DataInterval(row["interval"]),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=_decimal(row["volume"]),
            currency=row["currency"],
            observed_at=_aware(row["observed_at"]),
            retrieved_at=_aware(row["retrieved_at"]),
            source_timezone=row["source_timezone"],
            quality=DataQuality(row["quality"]),
            freshness=Freshness(row["freshness"]),
            partial=bool(row["partial"]),
            reasons=tuple(_loads_list(row["reasons_json"])),
            provider_metadata=_loads(row["provider_metadata_json"]),
            raw=_loads(row["raw_json"]),
        )

    # -- provider health and observability ------------------------------------

    def record_provider_success(
        self,
        provider: str,
        operation: str,
        *,
        latency_ms: float,
        at: datetime | None = None,
    ) -> ProviderHealth:
        moment = _iso(at or self._clock())
        key = str(provider).strip().lower()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM market_provider_health WHERE provider=?", (key,)
            ).fetchone()
            operations = set(_loads_list(row["verified_operations_json"])) if row else set()
            operations.add(str(operation))
            self._connection.execute(
                """
                INSERT INTO market_provider_health (
                    provider, status, consecutive_failures, last_success_at,
                    last_failure_at, last_error, latency_ms,
                    verified_operations_json, updated_at
                ) VALUES (?, ?, 0, ?, NULL, '', ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    status=excluded.status,
                    consecutive_failures=0,
                    last_success_at=excluded.last_success_at,
                    last_error='',
                    latency_ms=excluded.latency_ms,
                    verified_operations_json=excluded.verified_operations_json,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    ProviderStatus.OK.value,
                    moment,
                    float(latency_ms),
                    _json(sorted(operations)),
                    moment,
                ),
            )
        return self.provider_health(key)

    def record_provider_failure(
        self,
        provider: str,
        operation: str,
        *,
        error_type: str,
        error: str,
        latency_ms: float | None = None,
        at: datetime | None = None,
    ) -> ProviderHealth:
        moment = _iso(at or self._clock())
        key = str(provider).strip().lower()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM market_provider_health WHERE provider=?", (key,)
            ).fetchone()
            failures = int(row["consecutive_failures"]) + 1 if row else 1
            operations = _loads_list(row["verified_operations_json"]) if row else []
            status = (
                ProviderStatus.UNAVAILABLE
                if failures >= 3
                else ProviderStatus.DEGRADED
            )
            last_success = row["last_success_at"] if row else None
            self._connection.execute(
                """
                INSERT INTO market_provider_health (
                    provider, status, consecutive_failures, last_success_at,
                    last_failure_at, last_error, latency_ms,
                    verified_operations_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    status=excluded.status,
                    consecutive_failures=excluded.consecutive_failures,
                    last_failure_at=excluded.last_failure_at,
                    last_error=excluded.last_error,
                    latency_ms=excluded.latency_ms,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    status.value,
                    failures,
                    last_success,
                    moment,
                    f"{error_type}: {error}",
                    float(latency_ms) if latency_ms is not None else None,
                    _json(operations),
                    moment,
                ),
            )
        return self.provider_health(key)

    def provider_health(self, provider: str) -> ProviderHealth:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM market_provider_health WHERE provider=?",
                (str(provider).strip().lower(),),
            ).fetchone()
        if row is None:
            return ProviderHealth(provider=str(provider).strip().lower())
        return ProviderHealth(
            provider=row["provider"],
            status=ProviderStatus(row["status"]),
            consecutive_failures=int(row["consecutive_failures"]),
            last_success_at=(
                _aware(row["last_success_at"]) if row["last_success_at"] else None
            ),
            last_failure_at=(
                _aware(row["last_failure_at"]) if row["last_failure_at"] else None
            ),
            last_error=row["last_error"] or "",
            latency_ms=float(row["latency_ms"]) if row["latency_ms"] is not None else None,
            verified_operations=tuple(_loads_list(row["verified_operations_json"])),
            checked_at=_aware(row["updated_at"]),
        )

    def all_provider_health(self) -> tuple[ProviderHealth, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT provider FROM market_provider_health ORDER BY provider"
            ).fetchall()
        return tuple(self.provider_health(row["provider"]) for row in rows)

    def record_retrieval(
        self,
        *,
        provider: str,
        operation: str,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        records: int = 0,
        instrument_id: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        latency = max((finished_at - started_at).total_seconds() * 1000.0, 0.0)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO market_retrieval_log (
                    provider, operation, instrument_id, started_at, finished_at,
                    latency_ms, outcome, error_type, error_message, records
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(provider).strip().lower(),
                    str(operation),
                    instrument_id,
                    _iso(started_at),
                    _iso(finished_at),
                    latency,
                    str(outcome),
                    error_type,
                    (error_message or "")[:2_000],
                    int(records),
                ),
            )
            self._prune_logs()

    def _prune_logs(self) -> None:
        self._connection.execute(
            """
            DELETE FROM market_retrieval_log WHERE id NOT IN (
                SELECT id FROM market_retrieval_log ORDER BY id DESC LIMIT ?
            )
            """,
            (LOG_RETENTION,),
        )
        self._connection.execute(
            """
            DELETE FROM market_quality_events WHERE id NOT IN (
                SELECT id FROM market_quality_events ORDER BY id DESC LIMIT ?
            )
            """,
            (LOG_RETENTION,),
        )

    def quality_events(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        limit, _ = self._page(limit, 0)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM market_quality_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            {
                "record_kind": row["record_kind"],
                "record_id": row["record_id"],
                "instrument_id": row["instrument_id"],
                "provider": row["provider"],
                "previous_quality": row["previous_quality"],
                "quality": row["quality"],
                "reasons": _loads_list(row["reasons_json"]),
                "at": row["at"],
            }
            for row in rows
        )

    def corrections(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        limit, _ = self._page(limit, 0)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM market_corrections ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            {
                "record_kind": row["record_kind"],
                "record_id": row["record_id"],
                "instrument_id": row["instrument_id"],
                "provider": row["provider"],
                "previous": _loads(row["previous_payload_json"]),
                "incoming": _loads(row["incoming_payload_json"]),
                "detected_at": row["detected_at"],
                "detail": row["detail"],
            }
            for row in rows
        )

    def retrieval_log(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        limit, _ = self._page(limit, 0)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM market_retrieval_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    # -- diagnostics ----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            instruments = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM market_instruments"
                ).fetchone()[0]
            )
            observations = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM market_observations"
                ).fetchone()[0]
            )
            bars = int(
                self._connection.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
            )
            providers = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM market_provider_health"
                ).fetchone()[0]
            )
        return {
            "schema_version": self.schema_version,
            "path": self.path,
            "instruments": instruments,
            "observations": observations,
            "bars": bars,
            "providers": providers,
        }

    def check(self) -> dict[str, Any]:
        """A read-only integrity probe; never writes and never mutates state."""
        with self._lock:
            result = self._connection.execute("PRAGMA quick_check").fetchone()[0]
        return {
            "ok": str(result).lower() == "ok",
            "result": str(result),
            "schema_version": self.schema_version,
            "path": self.path,
        }

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError("offset must be an integer")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")
        return limit, offset


__all__ = [
    "BarPage",
    "InstrumentPage",
    "LOG_RETENTION",
    "MARKET_DATA_SCHEMA_VERSION",
    "MarketDataStore",
    "ObservationPage",
]
