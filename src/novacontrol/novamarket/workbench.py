"""Deterministic read-only analysis for the OrderBlock and TradingWatch desks.

This module is deliberately a *consumer* of NM-02. It never fetches a provider,
never writes a bar, never invents a missing value and never places an order. It
reads the stored, validated OHLCV series for instruments that actually have one
and derives structure, order-block, liquidity, momentum and replay views from
them.

The two rules that shape every calculation are:

* **Closed candles only for structure.** A partial bar is shown on a chart but is
  not allowed to confirm a swing, a break of structure or an order block.
* **No future data.** A confirmation is recorded at the bar that closed through
  the structural level. Replay indicators are computed from the prefix that ends
  at the cursor, never from the whole series.

This is a technical model of price structure. It is not a view into
institutional order flow, and the output always says so.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, median
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from novacontrol.novamarket.marketdata.engine import MarketDataEngine
from novacontrol.novamarket.marketdata.derivatives import scan_futures_snapshot
from novacontrol.novamarket.marketdata.models import DataInterval, OhlcvBar
from novacontrol.novamarket.marketdata.screener import load_index_reference
from novacontrol.novamarket.strategies.indicators import (
    atr,
    average_volume,
    rsi,
    sma,
)

METHODOLOGY_VERSION = "nm14-orderblock-1.0"
DERIVATIVES_SNAPSHOT_PATH = Path(__file__).resolve().parents[3] / "data" / "derivatives_snapshot.json"
DEFAULT_SCAN_LIMIT: int | None = None
INVENTORY_PAGE_SIZE = 1000
DEFAULT_BATCH_SIZE = 250
DEFAULT_MIN_BARS = 30
DEFAULT_BOARD_LIMIT = 50
MAX_CHART_BARS = 220

DISCLAIMER = (
    "Order blocks and momentum are technical calculations over stored OHLCV "
    "bars. They are not institutional order data, investment advice or an "
    "execution instruction. No signal from this desk reaches a broker; the "
    "separate risk engine, paper book and explicit approval remain required."
)

_IST = ZoneInfo("Asia/Kolkata")


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return _round(((current / previous) - 1.0) * 100.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class _Bar:
    """A normalized bar with its durable provenance."""

    index: int
    observed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    provider: str
    quality: str
    freshness: str
    partial: bool
    reasons: tuple[str, ...]

    @property
    def time(self) -> str:
        return self.observed_at.isoformat()


@dataclass(frozen=True, slots=True)
class _Pivot:
    kind: str
    index: int
    confirmed_index: int
    price: float


@dataclass(frozen=True, slots=True)
class _CandlePoint:
    """One replay point; every field is calculated from the prefix ending here."""

    bar: _Bar
    sma20: float | None
    rsi14: float | None
    vwap20: float | None
    relative_volume: float | None
    atr14: float | None


class MarketWorkbench:
    """Read-only workbench over the existing NM-02 market-data engine."""

    def __init__(
        self,
        engine: MarketDataEngine,
        *,
        scan_limit: int | None = DEFAULT_SCAN_LIMIT,
        batch_size: int = DEFAULT_BATCH_SIZE,
        min_bars: int = DEFAULT_MIN_BARS,
        now: Callable[[], datetime] | None = None,
        session: dict[str, Any] | None = None,
    ) -> None:
        self._engine = engine
        if scan_limit is None or int(scan_limit) <= 0:
            self._scan_limit = None
        else:
            self._scan_limit = int(scan_limit)
        self._batch_size = max(1, min(int(batch_size), 1000))
        self._min_bars = max(20, min(int(min_bars), 500))
        self._clock = now or (lambda: datetime.now(UTC))
        self._session = dict(session or {})

    # -- public contracts --------------------------------------------------

    def snapshot(self, *, interval: str = "1d", limit: int = DEFAULT_BOARD_LIMIT) -> dict[str, Any]:
        """Return ranks, breadth, sector flow, liquidity and coverage."""
        started = monotonic()
        data_interval = self._interval(interval)
        board_limit = max(1, min(int(limit), 100))
        inventory = self._inventory(data_interval)
        entries = list(inventory.get("items") or ())
        catalogue_total = self._catalogue_total()
        eligible_total = int(inventory.get("total") or 0)
        instruments, instrument_error = self._instrument_index()
        analyses: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        seen: set[str] = set()
        batches = 0

        # Bounded batches keep the working set predictable. The inventory is
        # paged by the store; the analyses are compact board summaries, not
        # full chart payloads, so a full eligible scan does not retain a chart
        # per instrument.
        for batch_start in range(0, len(entries), self._batch_size):
            batches += 1
            for item in entries[batch_start : batch_start + self._batch_size]:
                instrument_id = str(item.get("instrument_id") or "").strip()
                if not instrument_id:
                    continue
                if instrument_id in seen:
                    continue
                seen.add(instrument_id)
                try:
                    detail = self._analyze(
                        instrument_id,
                        interval=data_interval,
                        chart_limit=0,
                        include_chart=False,
                        compact=True,
                        instrument=instruments.get(instrument_id),
                    )
                except Exception as exc:  # noqa: BLE001 - one bad series must not fail the board
                    failed.append(
                        {
                            "instrument_id": instrument_id,
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                if not detail.get("analysis_available"):
                    entry = {
                        "instrument_id": instrument_id,
                        "reason": str(detail.get("reason") or "insufficient closed bars"),
                    }
                    (failed if detail.get("error") else skipped).append(entry)
                    continue
                analyses.append(detail)

        rows = [self._board_row(item) for item in analyses]
        rows.sort(key=lambda row: (-float(row["bullish_score"]), -float(row["momentum_score"])))
        bearish = sorted(
            rows,
            key=lambda row: (-float(row["bearish_score"]), -float(row["momentum_score"])),
        )
        bullish = list(rows)
        momentum = sorted(rows, key=lambda row: (-float(row["momentum_score"]), row["symbol"]))
        xray = self._xray(analyses)
        sectors = self._sector_flow(analyses)
        liquidity = self._liquidity_board(analyses)
        zone_board = self._zone_board(analyses)
        heatmap = self._heatmap(analyses)
        fno = self._fno_contract_status(analyses, list(instruments.values()))
        options = self._options_contract_status()
        futures = self._futures_contract_status()
        flow = self._market_flow(analyses, xray, sectors)
        # The eligible inventory is thresholded at _min_bars, so it is not the
        # same population as "has at least one stored bar".  Count the latter
        # separately; if the read fails, report the thresholded count as a
        # bounded fallback rather than claiming a larger population.
        try:
            with_bars = int(
                self._engine.bar_inventory(
                    interval=data_interval.value,
                    min_bars=1,
                    limit=1,
                    offset=0,
                ).get("total")
                or eligible_total
            )
        except Exception:  # noqa: BLE001 - coverage must not fail the board
            with_bars = eligible_total
        scanned = len(seen)
        excluded = skipped + failed
        truncated = bool(inventory.get("truncated"))
        inventory_error = str(inventory.get("error") or "") or None
        complete = bool(
            not truncated
            and inventory_error is None
            and eligible_total == scanned
            and len(analyses) == eligible_total
            and not failed
        )
        coverage = {
            "catalogue_total": catalogue_total,
            "instruments_with_bars": with_bars,
            "eligible": eligible_total,
            "scanned": scanned,
            "analysed": len(analyses),
            "skipped": len(skipped),
            "failed": len(failed),
            "duplicates": int(inventory.get("duplicates") or 0),
            "excluded": len(excluded),
            "excluded_count": len(excluded),
            "excluded_sample": excluded[:20],
            "excluded_truncated": len(excluded) > 20,
            "minimum_closed_bars": self._min_bars,
            "scan_cap": self._scan_limit,
            "scan_limit_applied": self._scan_limit is not None,
            "truncated": truncated,
            "inventory_pages": int(inventory.get("pages") or 0),
            "inventory_error": inventory_error,
            "instrument_index_error": instrument_error,
            "batch_size": self._batch_size,
            "batches": batches,
            "complete": complete,
            "sample_only": not complete,
            "note": (
                "All eligible instruments with a validated stored OHLCV series "
                "were processed."
                if complete
                else (
                    "Coverage is partial: see eligible, scanned, skipped, failed "
                    "and truncated. No missing value was filled or inferred."
                )
            ),
        }
        duration_ms = round((monotonic() - started) * 1000.0, 2)
        generated = self._clock().isoformat()
        return {
            "ok": True,
            "generated_at": generated,
            "methodology_version": METHODOLOGY_VERSION,
            "interval": data_interval.value,
            "timeframe_label": self._interval_label(data_interval),
            "data_state": "stored_ohlcv",
            "live": False,
            "price_basis": "last closed OHLCV bar",
            "session": self._session or None,
            "coverage": coverage,
            "scan": {
                "strategy": "paged_inventory+bounded_batches",
                "inventory_page_size": int(inventory.get("page_size") or 0),
                "inventory_pages": int(inventory.get("pages") or 0),
                "batch_size": self._batch_size,
                "batches": batches,
                "duration_ms": duration_ms,
                "truncated": truncated,
                "complete": complete,
                "inventory_error": inventory_error,
                "instrument_index_error": instrument_error,
            },
            "sector_reference": self._sector_reference(),
            "instruments": rows,
            "boards": {
                "bullish": bullish[:board_limit],
                "bearish": bearish[:board_limit],
                "note": (
                    "Boards rank directional strength across the analysable sample. "
                    "A score below 55 is not a directional setup."
                ),
            },
            "momentum": momentum[:board_limit],
            "zones": zone_board[: max(board_limit * 2, 20)],
            "heatmap": heatmap,
            "market_flow": flow,
            "market_xray": xray,
            "sector_flow": sectors,
            "liquidity": liquidity[: max(board_limit, 20)],
            "fno": fno,
            "options": options,
            "futures": futures,
            "skipped": skipped[:50],
            "failed": failed[:20],
            "excluded": excluded[:20],
            "methodology": self._methodology(),
            "disclaimer": DISCLAIMER,
        }

    def instrument(self, instrument_id: str, *, interval: str = "1d") -> dict[str, Any]:
        """Return one instrument's full structure, blocks, levels and chart."""
        return self._analyze(
            instrument_id,
            interval=self._interval(interval),
            chart_limit=MAX_CHART_BARS,
        )

    def replay(
        self,
        instrument_id: str,
        *,
        end_date: str | None = None,
        interval: str = "1d",
        limit: int = 180,
    ) -> dict[str, Any]:
        """Return a chronological historical view with a hard cutoff."""
        data_interval = self._interval(interval)
        cutoff = self._cutoff(end_date)
        chart_limit = max(20, min(int(limit), MAX_CHART_BARS))
        instrument = self._instrument(instrument_id)
        try:
            # Stored history is daily. Weekly replay is an explicitly derived
            # aggregation of that same closed series, never a second feed.
            source_interval = (
                DataInterval.DAY_1
                if data_interval is DataInterval.WEEK_1
                else data_interval
            )
            source_limit = (
                min(MAX_CHART_BARS * 5, max(chart_limit * 5, 260))
                if data_interval is DataInterval.WEEK_1
                else chart_limit
            )
            page = self._engine.bars(
                instrument_id,
                interval=source_interval,
                end=cutoff,
                limit=source_limit,
                ascending=True,
            )
            bars = self._normalize(page.items)
        except Exception as exc:  # noqa: BLE001 - the API should explain, not traceback
            return {
                "ok": False,
                "instrument_id": instrument_id,
                "instrument": instrument,
                "reason": f"{type(exc).__name__}: {exc}",
                "bars": [],
                "points": [],
                "data_state": "historical",
                "live": False,
            }
        bars = [bar for bar in bars if bar.observed_at <= cutoff]
        if data_interval is DataInterval.WEEK_1:
            bars = self._weekly_bars(bars)[-chart_limit:]
        if len(bars) < 2:
            return {
                "ok": True,
                "instrument_id": instrument_id,
                "instrument": instrument,
                "interval": data_interval.value,
                "end_date": cutoff.isoformat(),
                "data_state": "historical",
                "live": False,
                "bars": self._chart_bars(bars, MAX_CHART_BARS),
                "points": [],
                "reason": "At least two stored bars are required for a replay.",
                "methodology": self._replay_methodology(),
            }
        points = [
            self._replay_point(bars[index], bars[: index + 1])
            for index in range(len(bars))
        ]
        return {
            "ok": True,
            "instrument_id": instrument_id,
            "instrument": instrument,
            "interval": data_interval.value,
            "timeframe_label": self._interval_label(data_interval),
            "end_date": cutoff.isoformat(),
            "as_of": bars[-1].observed_at.isoformat(),
            "data_state": "historical",
            "live": False,
            "price_basis": "stored OHLCV bar at or before the cutoff",
            "bars": self._chart_bars(bars, MAX_CHART_BARS),
            "points": points,
            "coverage": {
                "bars": len(bars),
                "first": bars[0].observed_at.isoformat(),
                "last": bars[-1].observed_at.isoformat(),
                "cutoff": cutoff.isoformat(),
                "look_ahead_guard": True,
            },
            "methodology": self._replay_methodology(),
            "disclaimer": DISCLAIMER,
        }

    def latest_price(self, instrument_id: str, *, interval: str = "1d") -> dict[str, Any]:
        """Latest stored close, with a reconciled quote only as a fallback."""
        data_interval = self._interval(interval)
        try:
            page = self._engine.bars(
                instrument_id,
                interval=data_interval,
                limit=30,
                ascending=True,
            )
            bars = [bar for bar in self._normalize(page.items) if not bar.partial]
        except Exception:
            bars = []
        if len(bars) >= 2:
            last, prior = bars[-1], bars[-2]
            return {
                "available": True,
                "instrument_id": instrument_id,
                "price": _round(last.close),
                "previous_close": _round(prior.close),
                "change_pct": _pct(last.close, prior.close),
                "as_of": last.observed_at.isoformat(),
                "price_basis": f"last stored {data_interval.value} close",
                "freshness": last.freshness,
                "provider": last.provider,
            }
        try:
            quote = self._engine.latest(instrument_id).to_dict()
            selected = quote.get("selected") or {}
            price = _as_float(selected.get("price"))
            if quote.get("usable") and price is not None:
                return {
                    "available": True,
                    "instrument_id": instrument_id,
                    "price": _round(price),
                    "previous_close": None,
                    "change_pct": None,
                    "as_of": selected.get("observed_at"),
                    "price_basis": "reconciled quote",
                    "freshness": selected.get("freshness"),
                    "provider": selected.get("provider"),
                }
        except Exception:
            pass
        return {
            "available": False,
            "instrument_id": instrument_id,
            "price": None,
            "previous_close": None,
            "change_pct": None,
            "as_of": None,
            "price_basis": None,
            "freshness": "missing",
            "provider": None,
            "reason": "No stored OHLCV close or usable quote is available.",
        }

    # -- data normalization ------------------------------------------------

    @staticmethod
    def _interval(value: str) -> DataInterval:
        try:
            interval = DataInterval(str(value or "1d"))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in DataInterval if item is not DataInterval.SPOT)
            raise ValueError(f"unsupported interval {value!r}; use one of: {allowed}") from exc
        if interval is DataInterval.SPOT:
            raise ValueError("an OHLCV analysis interval cannot be spot")
        return interval

    @staticmethod
    def _interval_label(interval: DataInterval) -> str:
        return {
            DataInterval.MINUTE_1: "1-minute",
            DataInterval.MINUTE_5: "5-minute",
            DataInterval.MINUTE_15: "15-minute",
            DataInterval.MINUTE_30: "30-minute",
            DataInterval.HOUR_1: "1-hour",
            DataInterval.DAY_1: "Daily",
            DataInterval.WEEK_1: "Weekly",
            DataInterval.MONTH_1: "Monthly",
        }.get(interval, interval.value)

    def _inventory(self, interval: DataInterval) -> dict[str, Any]:
        page_size = self._scan_limit or INVENTORY_PAGE_SIZE
        page_size = max(1, min(int(page_size), 1000))
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicates = 0
        offset = 0
        total = 0
        pages = 0
        try:
            while True:
                pages += 1
                page = dict(
                    self._engine.bar_inventory(
                        interval=interval.value,
                        min_bars=self._min_bars,
                        limit=page_size,
                        offset=offset,
                    )
                )
                total = int(page.get("total") or 0)
                batch = list(page.get("items") or ())
                if not batch:
                    break
                previous_offset = offset
                for item in batch:
                    instrument_id = str(item.get("instrument_id") or "").strip()
                    if not instrument_id:
                        continue
                    if instrument_id in seen:
                        duplicates += 1
                        continue
                    seen.add(instrument_id)
                    items.append(item)
                    if self._scan_limit is not None and len(items) >= self._scan_limit:
                        return {
                            "items": items,
                            "total": total,
                            "page_size": page_size,
                            "pages": pages,
                            "limit": self._scan_limit,
                            "duplicates": duplicates,
                            "truncated": total > len(items),
                            "error": None,
                        }
                # Advance by the requested page size, not by the number of
                # rows returned: a duplicate row on a page must not shift the
                # next window and silently skip a real instrument.
                offset += min(len(batch), page_size)
                if offset >= total:
                    break
                if offset <= previous_offset:
                    return {
                        "items": items,
                        "total": total,
                        "page_size": page_size,
                        "pages": pages,
                        "limit": self._scan_limit,
                        "duplicates": duplicates,
                        "truncated": total > len(items),
                        "error": "inventory pagination did not advance",
                    }
                if pages >= 10_000:
                    return {
                        "items": items,
                        "total": total,
                        "page_size": page_size,
                        "pages": pages,
                        "limit": self._scan_limit,
                        "duplicates": duplicates,
                        "truncated": total > len(items),
                        "error": "inventory pagination exceeded 10000 pages",
                    }
            return {
                "items": items,
                "total": total,
                "page_size": page_size,
                "pages": pages,
                "limit": self._scan_limit,
                "duplicates": duplicates,
                "truncated": total > len(items),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - coverage must report the failure
            return {
                "items": items,
                "total": max(total, len(items)),
                "page_size": page_size,
                "pages": pages,
                "limit": self._scan_limit,
                "duplicates": duplicates,
                "truncated": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _catalogue_total(self) -> int:
        try:
            return int(self._engine.list_instruments(limit=1).total)
        except Exception:
            return 0

    def _instrument_index(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Read instrument metadata in pages instead of one query per row."""
        index: dict[str, dict[str, Any]] = {}
        offset = 0
        pages = 0
        try:
            while True:
                page = self._engine.list_instruments(limit=1000, offset=offset)
                items = list(page.items)
                pages += 1
                for instrument in items:
                    index[instrument.instrument_id] = instrument.to_dict()
                offset += len(items)
                if not items or offset >= int(page.total):
                    break
                if pages >= 10_000:
                    return index, "instrument catalogue pagination exceeded 10000 pages"
            return index, None
        except Exception as exc:  # noqa: BLE001 - per-instrument fallback remains available
            return index, f"{type(exc).__name__}: {exc}"

    def _instrument(self, instrument_id: str) -> dict[str, Any]:
        return self._engine.get_instrument(str(instrument_id).strip()).to_dict()

    def _normalize(self, raw_bars: Sequence[OhlcvBar]) -> list[_Bar]:
        """Deduplicate a provider series in a stable, auditable order."""
        priority = {
            str(name).strip().lower(): index
            for index, name in enumerate(getattr(self._engine, "enabled_providers", ()) or ())
        }
        chosen: dict[datetime, tuple[int, float, OhlcvBar]] = {}
        for raw in raw_bars:
            if raw.quality.value == "invalid":
                continue
            observed = raw.observed_at.astimezone(UTC)
            rank = priority.get(raw.provider, len(priority) + 1)
            existing = chosen.get(observed)
            candidate = (rank, -raw.retrieved_at.timestamp(), raw)
            if existing is None or candidate[:2] < existing[:2]:
                chosen[observed] = candidate
        normalized: list[_Bar] = []
        for index, observed in enumerate(sorted(chosen)):
            raw = chosen[observed][2]
            normalized.append(
                _Bar(
                    index=index,
                    observed_at=observed,
                    open=float(raw.open),
                    high=float(raw.high),
                    low=float(raw.low),
                    close=float(raw.close),
                    volume=_as_float(raw.volume),
                    provider=raw.provider,
                    quality=raw.quality.value,
                    freshness=raw.freshness.value,
                    partial=bool(raw.partial),
                    reasons=tuple(raw.reasons),
                )
            )
        return normalized

    def _analyze(
        self,
        instrument_id: str,
        *,
        interval: DataInterval,
        chart_limit: int,
        include_chart: bool = True,
        compact: bool = False,
        instrument: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        instrument = instrument if instrument is not None else self._instrument(instrument_id)
        try:
            page = self._engine.bars(
                instrument_id,
                interval=interval,
                limit=320,
                ascending=True,
            )
            bars = self._normalize(page.items)
        except Exception as exc:  # noqa: BLE001 - a missing series is a stated outcome
            return {
                "ok": True,
                "analysis_available": False,
                "instrument_id": instrument_id,
                "instrument": instrument,
                "interval": interval.value,
                "reason": f"{type(exc).__name__}: {exc}",
                "error": True,
                "failure_kind": "series_read",
                "bars": [],
                "data_state": "stored_ohlcv",
                "live": False,
                "disclaimer": DISCLAIMER,
            }
        closed = [bar for bar in bars if not bar.partial]
        if len(closed) < self._min_bars:
            return {
                "ok": True,
                "analysis_available": False,
                "instrument_id": instrument_id,
                "instrument": instrument,
                "interval": interval.value,
                "reason": (
                    f"{len(closed)} closed bars available; {self._min_bars} are "
                    "required for a structural calculation."
                ),
                "bars": self._chart_bars(bars, chart_limit) if include_chart else [],
                "coverage": {
                    "closed_bars": len(closed),
                    "stored_bars": len(bars),
                    "minimum_closed_bars": self._min_bars,
                },
                "data_state": "stored_ohlcv",
                "live": False,
                "disclaimer": DISCLAIMER,
            }

        closes = [bar.close for bar in closed]
        highs = [bar.high for bar in closed]
        lows = [bar.low for bar in closed]
        volumes = [bar.volume for bar in closed]
        latest = closed[-1]
        previous = closed[-2]
        current_atr = self._atr_at(closed, len(closed) - 1)
        swings = self._pivots(closed)
        blocks = self._detect_blocks(closed, swings, current_atr)
        structure = self._structure(closed, swings, blocks)
        liquidity = self._liquidity_levels(closed, swings)
        support, resistance = self._nearest_levels(liquidity, latest.close)
        metrics = self._metrics(closed)
        signal = self._signal(closed, blocks, support, resistance, current_atr)
        scores = self._directional_scores(metrics, structure, signal)
        weekly = self._timeframe_context(closed, metrics, structure)
        trend = structure.get("trend") or "range"
        vwap_relation = metrics.get("vwap_relation")
        relative_volume = metrics.get("relative_volume")
        sector, sector_source, sector_as_of = self._sector_evidence(instrument)
        evidence = (
            {}
            if compact
            else self._evidence(metrics, structure, signal, blocks, liquidity)
        )
        levels = {
            "support": support,
            "resistance": resistance,
            "invalidation": signal.get("stop"),
            "entry_zone": signal.get("entry_zone"),
            "targets": signal.get("targets") or [],
            "risk_reward": signal.get("risk_reward"),
        }
        return {
            "ok": True,
            "analysis_available": True,
            "instrument_id": instrument_id,
            "instrument": instrument,
            "symbol": instrument.get("symbol") or instrument_id.split(":", 1)[-1],
            "name": instrument.get("name"),
            "sector": sector,
            "sector_source": sector_source,
            "sector_as_of": sector_as_of,
            "exchange": instrument.get("exchange"),
            "currency": instrument.get("currency"),
            "asset_class": instrument.get("asset_class"),
            "interval": interval.value,
            "timeframe_label": self._interval_label(interval),
            "data_state": "stored_ohlcv",
            "live": False,
            "price_basis": f"last closed {interval.value} bar",
            "as_of": latest.observed_at.isoformat(),
            "freshness": latest.freshness,
            "provider": latest.provider,
            "last_price": _round(latest.close),
            "previous_close": _round(previous.close),
            "change_pct": _pct(latest.close, previous.close),
            "metrics": metrics,
            "momentum_score": metrics.get("momentum_score"),
            "relative_volume": relative_volume,
            "vwap_relation": vwap_relation,
            "structure": structure,
            "order_blocks": blocks[:12],
            "liquidity": liquidity[:16],
            "levels": levels,
            "signal": signal,
            "bullish_score": scores["bullish"],
            "bearish_score": scores["bearish"],
            "directional_strength": scores["strength"],
            "directional_bias": scores["bias"],
            "score_factors": scores["factors"],
            "timeframe_context": weekly,
            "coverage": {
                "stored_bars": len(bars),
                "closed_bars": len(closed),
                "partial_bars_excluded": len(bars) - len(closed),
                "minimum_bars": self._min_bars,
                "first_bar": closed[0].observed_at.isoformat(),
                "last_bar": latest.observed_at.isoformat(),
            },
            "trend": trend,
            "vwap_relation": vwap_relation,
            "relative_volume": relative_volume,
            "evidence": evidence,
            "chart": (
                {
                    "bars": self._chart_bars(bars, chart_limit),
                    "zones": self._chart_zones(blocks),
                    "levels": self._chart_levels(support, resistance, signal),
                }
                if include_chart
                else None
            ),
            "bars": self._chart_bars(bars, chart_limit) if include_chart else [],
            "methodology_version": METHODOLOGY_VERSION if not compact else None,
            "methodology": {} if compact else self._methodology(),
            "disclaimer": "" if compact else DISCLAIMER,
        }

    # -- calculations ------------------------------------------------------

    def _atr_at(self, bars: Sequence[_Bar], index: int, period: int = 14) -> float | None:
        if index + 1 < period + 1:
            return None
        window = bars[: index + 1]
        value = atr(
            [bar.high for bar in window],
            [bar.low for bar in window],
            [bar.close for bar in window],
            period,
        )
        return _as_float(value)

    def _pivots(self, bars: Sequence[_Bar], left: int = 2, right: int = 2) -> list[_Pivot]:
        pivots: list[_Pivot] = []
        for index in range(left, len(bars) - right):
            window = bars[index - left : index + right + 1]
            candidate = bars[index]
            if candidate.high >= max(bar.high for bar in window):
                pivots.append(_Pivot("high", index, index + right, candidate.high))
            if candidate.low <= min(bar.low for bar in window):
                pivots.append(_Pivot("low", index, index + right, candidate.low))
        return pivots

    def _detect_blocks(
        self,
        bars: Sequence[_Bar],
        pivots: Sequence[_Pivot],
        current_atr: float | None,
    ) -> list[dict[str, Any]]:
        highs_by_confirm: dict[int, list[_Pivot]] = defaultdict(list)
        lows_by_confirm: dict[int, list[_Pivot]] = defaultdict(list)
        for pivot in pivots:
            (highs_by_confirm if pivot.kind == "high" else lows_by_confirm)[
                pivot.confirmed_index
            ].append(pivot)

        confirmed_highs: list[_Pivot] = []
        confirmed_lows: list[_Pivot] = []
        last_broken_high: int | None = None
        last_broken_low: int | None = None
        used_origins: set[tuple[str, int]] = set()
        blocks: list[dict[str, Any]] = []

        for index, bar in enumerate(bars):
            confirmed_highs.extend(highs_by_confirm.get(index, ()))
            confirmed_lows.extend(lows_by_confirm.get(index, ()))
            if index < 4:
                continue

            swing_high = confirmed_highs[-1] if confirmed_highs else None
            if swing_high is not None and bar.close > swing_high.price:
                if last_broken_high != swing_high.index:
                    last_broken_high = swing_high.index
                    origin = self._last_opposite(
                        bars, index, "bearish", minimum=index - 8
                    )
                    if origin is not None and (("bullish", origin) not in used_origins):
                        block = self._build_block(
                            bars,
                            direction="bullish",
                            origin_index=origin,
                            confirmed_index=index,
                            break_level=swing_high.price,
                            atr_value=current_atr,
                        )
                        if block is not None:
                            used_origins.add(("bullish", origin))
                            blocks.append(block)

            swing_low = confirmed_lows[-1] if confirmed_lows else None
            if swing_low is not None and bar.close < swing_low.price:
                if last_broken_low != swing_low.index:
                    last_broken_low = swing_low.index
                    origin = self._last_opposite(
                        bars, index, "bullish", minimum=index - 8
                    )
                    if origin is not None and (("bearish", origin) not in used_origins):
                        block = self._build_block(
                            bars,
                            direction="bearish",
                            origin_index=origin,
                            confirmed_index=index,
                            break_level=swing_low.price,
                            atr_value=current_atr,
                        )
                        if block is not None:
                            used_origins.add(("bearish", origin))
                            blocks.append(block)

        blocks.extend(self._developing_blocks(bars, pivots, blocks, current_atr))
        blocks.sort(key=lambda item: (item["origin_index"], item["confirmed_index"] or 0))
        for block in blocks:
            self._update_block_status(block, bars)
        return blocks

    def _last_opposite(
        self, bars: Sequence[_Bar], impulse_index: int, direction: str, *, minimum: int
    ) -> int | None:
        floor = max(0, minimum)
        for index in range(impulse_index - 1, floor - 1, -1):
            bar = bars[index]
            if direction == "bearish" and bar.close < bar.open:
                return index
            if direction == "bullish" and bar.close > bar.open:
                return index
        return None

    def _build_block(
        self,
        bars: Sequence[_Bar],
        *,
        direction: str,
        origin_index: int,
        confirmed_index: int,
        break_level: float,
        atr_value: float | None,
    ) -> dict[str, Any] | None:
        origin = bars[origin_index]
        impulse = bars[confirmed_index]
        atr_at = self._atr_at(bars, confirmed_index) or atr_value
        if atr_at is None or atr_at <= 0:
            return None
        displacement = (impulse.high - impulse.low) / atr_at
        if displacement < 0.9:
            return None
        volume_reference = average_volume(
            [bar.volume or 0.0 for bar in bars[max(0, confirmed_index - 20) : confirmed_index]],
            20,
        )
        reference = _as_float(volume_reference)
        volume_ratio = (
            impulse.volume / reference
            if impulse.volume is not None and reference and reference > 0
            else None
        )
        if volume_ratio is not None and volume_ratio < 0.8:
            return None
        zone_low = min(origin.open, origin.close)
        zone_high = max(origin.open, origin.close)
        if direction == "bearish" and not impulse.close < break_level:
            return None
        if direction == "bullish" and not impulse.close > break_level:
            return None
        return {
            "direction": direction,
            "confirmation": "confirmed",
            "status": "confirmed",
            "origin_index": origin_index,
            "confirmed_index": confirmed_index,
            "origin_time": origin.observed_at.isoformat(),
            "confirmed_time": impulse.observed_at.isoformat(),
            "zone_low": _round(zone_low),
            "zone_high": _round(zone_high),
            "break_level": _round(break_level),
            "displacement_atr": _round(displacement, 2),
            "volume_ratio": _round(volume_ratio, 2),
            "volume_confirmation": (
                "confirmed"
                if volume_ratio is not None and volume_ratio >= 0.8
                else "unavailable"
            ),
            "confirmation_basis": "closed candle close through a confirmed swing",
            "repaint_safe": True,
            "rationale": (
                f"Last {'down' if direction == 'bullish' else 'up'}-close candle before "
                f"a {displacement:.2f} ATR displacement that closed "
                f"{'above' if direction == 'bullish' else 'below'} the confirmed "
                f"swing at {break_level:.2f}."
            ),
        }

    def _developing_blocks(
        self,
        bars: Sequence[_Bar],
        pivots: Sequence[_Pivot],
        confirmed: Sequence[dict[str, Any]],
        current_atr: float | None,
    ) -> list[dict[str, Any]]:
        if len(bars) < 4 or current_atr is None or current_atr <= 0:
            return []
        latest_highs = [pivot.price for pivot in pivots if pivot.kind == "high"]
        latest_lows = [pivot.price for pivot in pivots if pivot.kind == "low"]
        swing_high = max(latest_highs) if latest_highs else None
        swing_low = min(latest_lows) if latest_lows else None
        developing: list[dict[str, Any]] = []
        for index in range(len(bars) - 4, len(bars)):
            if index < 1:
                continue
            prior, bar = bars[index - 1], bars[index]
            already = any(item.get("origin_index") == index - 1 for item in confirmed)
            if already:
                continue
            if prior.close < prior.open and bar.close > bar.open:
                displacement = (bar.high - bar.low) / current_atr
                if (
                    displacement >= 1.0
                    and (swing_high is None or bar.close < swing_high)
                ):
                    developing.append(
                        self._developing_block(
                            prior,
                            bar,
                            "bullish",
                            index - 1,
                            index,
                            displacement,
                        )
                    )
            if prior.close > prior.open and bar.close < bar.open:
                displacement = (bar.high - bar.low) / current_atr
                if (
                    displacement >= 1.0
                    and (swing_low is None or bar.close > swing_low)
                ):
                    developing.append(
                        self._developing_block(
                            prior,
                            bar,
                            "bearish",
                            index - 1,
                            index,
                            displacement,
                        )
                    )
        return developing

    def _developing_block(
        self,
        prior: _Bar,
        bar: _Bar,
        direction: str,
        origin_index: int,
        confirmed_index: int,
        displacement: float,
    ) -> dict[str, Any]:
        return {
            "direction": direction,
            "confirmation": "developing",
            "status": "developing",
            "origin_index": origin_index,
            "confirmed_index": confirmed_index,
            "origin_time": prior.observed_at.isoformat(),
            "confirmed_time": bar.observed_at.isoformat(),
            "zone_low": _round(min(prior.open, prior.close)),
            "zone_high": _round(max(prior.open, prior.close)),
            "break_level": None,
            "displacement_atr": _round(displacement, 2),
            "volume_ratio": None,
            "volume_confirmation": "not_applicable",
            "confirmation_basis": "observing displacement before structure break",
            "repaint_safe": True,
            "rationale": (
                "An opposing candle is followed by an expanding candle, but price "
                "has not closed through the last confirmed swing. This is an "
                "unconfirmed setup and cannot be actioned."
            ),
        }

    def _update_block_status(
        self, block: dict[str, Any], bars: Sequence[_Bar]
    ) -> None:
        if block.get("confirmation") == "developing":
            block["invalidation_level"] = (
                _round(
                    (block.get("zone_low") or 0)
                    - (self._atr_at(bars, len(bars) - 1) or 0) * 0.25
                )
                if block["direction"] == "bullish"
                else _round(
                    (block.get("zone_high") or 0)
                    + (self._atr_at(bars, len(bars) - 1) or 0) * 0.25
                )
            )
            return
        confirmed_index = int(block.get("confirmed_index") or 0)
        zone_low = float(block.get("zone_low") or 0)
        zone_high = float(block.get("zone_high") or 0)
        touched = False
        invalidated_at: str | None = None
        for bar in bars[confirmed_index + 1 :]:
            if block["direction"] == "bullish" and bar.close < zone_low:
                invalidated_at = bar.observed_at.isoformat()
                break
            if block["direction"] == "bearish" and bar.close > zone_high:
                invalidated_at = bar.observed_at.isoformat()
                break
            if bar.low <= zone_high and bar.high >= zone_low:
                touched = True
        block["invalidation_level"] = _round(
            zone_low - (self._atr_at(bars, len(bars) - 1) or 0) * 0.25
            if block["direction"] == "bullish"
            else zone_high + (self._atr_at(bars, len(bars) - 1) or 0) * 0.25
        )
        block["invalidated_at"] = invalidated_at
        block["touched"] = touched
        block["status"] = (
            "invalidated" if invalidated_at else "tested" if touched else "confirmed"
        )
        last_price = bars[-1].close
        block["distance_pct"] = (
            _pct(
                last_price,
                zone_high if block["direction"] == "bullish" else zone_low,
            )
            if last_price
            else None
        )

    def _structure(
        self,
        bars: Sequence[_Bar],
        pivots: Sequence[_Pivot],
        blocks: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        closes = [bar.close for bar in bars]
        sma20 = _as_float(sma(closes, 20))
        sma50 = _as_float(sma(closes, 50))
        highs = [pivot for pivot in pivots if pivot.kind == "high"]
        lows = [pivot for pivot in pivots if pivot.kind == "low"]
        higher_highs = len(highs) >= 2 and highs[-1].price > highs[-2].price
        higher_lows = len(lows) >= 2 and lows[-1].price > lows[-2].price
        lower_highs = len(highs) >= 2 and highs[-1].price < highs[-2].price
        lower_lows = len(lows) >= 2 and lows[-1].price < lows[-2].price
        if higher_highs and higher_lows:
            trend = "bullish"
        elif lower_highs and lower_lows:
            trend = "bearish"
        elif sma20 is not None and sma50 is not None:
            trend = "bullish" if sma20 > sma50 else "bearish" if sma20 < sma50 else "range"
        else:
            trend = "range"
        latest_bos = next(
            (
                item
                for item in reversed(blocks)
                if item.get("confirmation") == "confirmed" and item.get("break_level") is not None
            ),
            None,
        )
        return {
            "trend": trend,
            "sma20": _round(sma20),
            "sma50": _round(sma50),
            "last_swing_high": _round(highs[-1].price) if highs else None,
            "last_swing_low": _round(lows[-1].price) if lows else None,
            "higher_highs": higher_highs,
            "higher_lows": higher_lows,
            "lower_highs": lower_highs,
            "lower_lows": lower_lows,
            "break_of_structure": (
                {
                    "direction": latest_bos["direction"],
                    "level": latest_bos["break_level"],
                    "at": latest_bos["confirmed_time"],
                }
                if latest_bos
                else None
            ),
            "confirmation": "closed candles and 2-bar confirmed pivots",
            "note": (
                "A break of structure is a close beyond a confirmed swing, not an "
                "intrabar wick."
            ),
        }

    def _liquidity_levels(
        self, bars: Sequence[_Bar], pivots: Sequence[_Pivot]
    ) -> list[dict[str, Any]]:
        latest_price = bars[-1].close
        levels: list[dict[str, Any]] = []
        seen: list[float] = []
        for kind in ("high", "low"):
            rows = [pivot for pivot in pivots if pivot.kind == kind][-8:]
            for pivot in rows:
                if any(abs(pivot.price - other) / max(pivot.price, 1e-9) < 0.0015 for other in seen):
                    continue
                seen.append(pivot.price)
                peers = [
                    other.price
                    for other in rows
                    if abs(other.price - pivot.price) / max(pivot.price, 1e-9) <= 0.0025
                ]
                label = "Equal highs" if kind == "high" and len(peers) >= 2 else (
                    "Equal lows" if kind == "low" and len(peers) >= 2 else (
                        "Swing high" if kind == "high" else "Swing low"
                    )
                )
                levels.append(
                    {
                        "kind": label,
                        "side": "above" if pivot.price >= latest_price else "below",
                        "price": _round(pivot.price),
                        "distance_pct": _pct(pivot.price, latest_price),
                        "status": "detected",
                        "method": "2-bar confirmed pivot",
                        "confirmed_at": bars[pivot.confirmed_index].observed_at.isoformat(),
                        "manual": False,
                        "estimated": False,
                    }
                )
        if len(bars) >= 2:
            previous = bars[-2]
            for kind, value in (("Prior day high", previous.high), ("Prior day low", previous.low)):
                if any(abs(value - other) / max(value, 1e-9) < 0.0015 for other in seen):
                    continue
                seen.append(value)
                levels.append(
                    {
                        "kind": kind,
                        "side": "above" if value >= latest_price else "below",
                        "price": _round(value),
                        "distance_pct": _pct(value, latest_price),
                        "status": "detected",
                        "method": "previous completed bar",
                        "confirmed_at": previous.observed_at.isoformat(),
                        "manual": False,
                        "estimated": False,
                    }
                )
        levels.sort(key=lambda item: abs(float(item.get("distance_pct") or 0)))
        return levels

    def _nearest_levels(
        self, liquidity: Sequence[dict[str, Any]], price: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        below = [
            item
            for item in liquidity
            if item.get("price") is not None and float(item["price"]) < price
        ]
        above = [
            item
            for item in liquidity
            if item.get("price") is not None and float(item["price"]) > price
        ]
        support = max(below, key=lambda item: float(item["price"]), default=None)
        resistance = min(above, key=lambda item: float(item["price"]), default=None)
        return (
            dict(support, role="support") if support else None,
            dict(resistance, role="resistance") if resistance else None,
        )

    def _metrics(self, bars: Sequence[_Bar]) -> dict[str, Any]:
        closes = [bar.close for bar in bars]
        volumes = [bar.volume for bar in bars]
        latest = bars[-1]
        latest_atr = self._atr_at(bars, len(bars) - 1)
        avg_volume = _as_float(average_volume([value or 0.0 for value in volumes[-20:]], 20))
        prior_volume = _as_float(
            average_volume([value or 0.0 for value in volumes[-21:-1]], 20)
        )
        relative_volume = (
            latest.volume / prior_volume
            if latest.volume is not None and prior_volume and prior_volume > 0
            else None
        )
        vwap = self._vwap_at(bars, len(bars) - 1, 20)
        rsi14 = _as_float(rsi(closes, 14))
        sma20 = _as_float(sma(closes, 20))
        sma50 = _as_float(sma(closes, 50))
        returns_5 = _pct(latest.close, closes[-6]) if len(closes) >= 6 else None
        returns_20 = _pct(latest.close, closes[-21]) if len(closes) >= 21 else None
        change_pct = _pct(latest.close, closes[-2]) if len(closes) >= 2 else None
        momentum_score = self._momentum_score(
            latest.close, change_pct, returns_5, returns_20, rsi14, relative_volume, sma20, sma50, vwap
        )
        return {
            "last_price": _round(latest.close),
            "change_pct": change_pct,
            "return_5d_pct": returns_5,
            "return_20d_pct": returns_20,
            "rsi14": _round(rsi14, 2),
            "atr14": _round(latest_atr),
            "atr_pct": _round((latest_atr / latest.close) * 100.0, 2) if latest_atr else None,
            "average_volume20": _round(avg_volume, 2),
            "relative_volume": _round(relative_volume, 2),
            "vwap20": _round(vwap),
            "vwap_relation": (
                "above" if vwap is not None and latest.close > vwap else "below" if vwap is not None else None
            ),
            "sma20": _round(sma20),
            "sma50": _round(sma50),
            "momentum_score": _round(momentum_score, 2),
            "volume_available": latest.volume is not None,
            "volatility_available": latest_atr is not None,
        }

    def _vwap_at(self, bars: Sequence[_Bar], index: int, period: int = 20) -> float | None:
        start = max(0, index - period + 1)
        window = bars[start : index + 1]
        if not window or any(bar.volume is None or (bar.volume or 0) <= 0 for bar in window):
            return None
        total_volume = sum(bar.volume or 0.0 for bar in window)
        if total_volume <= 0:
            return None
        typical_total = sum(
            ((bar.high + bar.low + bar.close) / 3.0) * (bar.volume or 0.0)
            for bar in window
        )
        return typical_total / total_volume

    def _momentum_score(
        self,
        close: float,
        change_pct: float | None,
        returns_5: float | None,
        returns_20: float | None,
        rsi14: float | None,
        relative_volume: float | None,
        sma20: float | None,
        sma50: float | None,
        vwap: float | None,
    ) -> float:
        score = 50.0
        if sma20 is not None:
            score += 7.0 if close > sma20 else -7.0
        if sma20 is not None and sma50 is not None:
            score += 7.0 if sma20 > sma50 else -7.0
        score += _clamp((returns_5 or 0.0) * 1.4, -14.0, 14.0)
        score += _clamp((returns_20 or 0.0) * 0.55, -12.0, 12.0)
        score += _clamp(((rsi14 or 50.0) - 50.0) * 0.22, -8.0, 8.0)
        if vwap is not None:
            score += 5.0 if close > vwap else -5.0
        if relative_volume is not None and returns_5 is not None:
            direction = 1.0 if returns_5 >= 0 else -1.0
            score += direction * _clamp((relative_volume - 1.0) * 8.0, -7.0, 7.0)
        if change_pct is not None:
            score += _clamp(change_pct * 0.25, -3.0, 3.0)
        return _clamp(score, 0.0, 100.0)

    def _signal(
        self,
        bars: Sequence[_Bar],
        blocks: Sequence[dict[str, Any]],
        support: dict[str, Any] | None,
        resistance: dict[str, Any] | None,
        atr_value: float | None,
    ) -> dict[str, Any]:
        confirmed = [
            block
            for block in blocks
            if block.get("confirmation") == "confirmed"
            and block.get("status") in ("confirmed", "tested")
        ]
        chosen = confirmed[-1] if confirmed else None
        if chosen is None:
            developing = [block for block in blocks if block.get("confirmation") == "developing"]
            if developing:
                block = developing[-1]
                return {
                    "status": "developing",
                    "direction": block["direction"],
                    "confirmation": "developing",
                    "actionable": False,
                    "entry_zone": [block["zone_low"], block["zone_high"]],
                    "stop": block.get("invalidation_level"),
                    "targets": [],
                    "risk_reward": None,
                    "reason": (
                        "A displacement exists, but no close through the last confirmed "
                        "swing has occurred. This setup is developing, not confirmed."
                    ),
                    "execution_allowed": False,
                }
            return {
                "status": "none",
                "direction": None,
                "confirmation": "none",
                "actionable": False,
                "entry_zone": None,
                "stop": None,
                "targets": [],
                "risk_reward": None,
                "reason": "No confirmed order block is active in the stored series.",
                "execution_allowed": False,
            }
        direction = chosen["direction"]
        zone_low = float(chosen["zone_low"])
        zone_high = float(chosen["zone_high"])
        entry = zone_high if direction == "bullish" else zone_low
        stop = (
            zone_low - (atr_value or 0.0) * 0.25
            if direction == "bullish"
            else zone_high + (atr_value or 0.0) * 0.25
        )
        target = (
            float(resistance["price"]) if direction == "bullish" and resistance else None
        )
        if direction == "bearish" and support:
            target = float(support["price"])
        risk = abs(entry - stop)
        reward = abs(target - entry) if target is not None else None
        risk_reward = reward / risk if reward is not None and risk > 0 else None
        valid_target = (
            target is not None
            and ((direction == "bullish" and target > entry) or (direction == "bearish" and target < entry))
        )
        return {
            "status": chosen["status"],
            "direction": direction,
            "confirmation": "confirmed",
            "actionable": False,
            "entry_zone": [_round(zone_low), _round(zone_high)],
            "stop": _round(stop),
            "targets": [_round(target)] if valid_target and target is not None else [],
            "risk_reward": _round(risk_reward, 2) if valid_target else None,
            "reason": (
                f"{direction.title()} order block confirmed at "
                f"{chosen['confirmed_time']}; invalidation is a close beyond the "
                "zone and stop buffer."
            ),
            "execution_allowed": False,
            "risk_engine_required": True,
        }

    def _directional_scores(
        self,
        metrics: dict[str, Any],
        structure: dict[str, Any],
        signal: dict[str, Any],
    ) -> dict[str, Any]:
        factors: list[dict[str, Any]] = []

        def add(name: str, delta: float, reason: str) -> None:
            factors.append({"name": name, "delta": round(delta, 2), "reason": reason})

        trend = str(structure.get("trend") or "range")
        add("structure", 12.0 if trend == "bullish" else -12.0 if trend == "bearish" else 0.0, "confirmed swing sequence")
        sma20 = _as_float(structure.get("sma20"))
        close = _as_float(metrics.get("last_price"))
        if sma20 is not None and close is not None:
            add("sma20", 7.0 if close > sma20 else -7.0, "close versus 20-bar mean")
        sma50 = _as_float(structure.get("sma50"))
        if sma20 is not None and sma50 is not None:
            add("sma50", 7.0 if sma20 > sma50 else -7.0, "20-bar mean versus 50-bar mean")
        r5 = _as_float(metrics.get("return_5d_pct")) or 0.0
        r20 = _as_float(metrics.get("return_20d_pct")) or 0.0
        add("5-bar return", _clamp(r5 * 1.2, -12.0, 12.0), "short momentum")
        add("20-bar return", _clamp(r20 * 0.45, -10.0, 10.0), "medium momentum")
        rsi_value = _as_float(metrics.get("rsi14"))
        if rsi_value is not None:
            add("rsi14", _clamp((rsi_value - 50.0) * 0.25, -8.0, 8.0), "RSI distance from 50")
        vwap_relation = str(metrics.get("vwap_relation") or "")
        if vwap_relation:
            add("vwap20", 6.0 if vwap_relation == "above" else -6.0, "close versus rolling VWAP")
        relative_volume = _as_float(metrics.get("relative_volume"))
        if relative_volume is not None and relative_volume > 1.0:
            direction = 1.0 if r5 >= 0 else -1.0
            add("relative volume", direction * min(7.0, (relative_volume - 1.0) * 8.0), "participation expansion")
        if signal.get("direction") == "bullish" and signal.get("status") in ("confirmed", "tested"):
            add("order block", 10.0, "active bullish block")
        elif signal.get("direction") == "bearish" and signal.get("status") in ("confirmed", "tested"):
            add("order block", -10.0, "active bearish block")
        bullish = _clamp(50.0 + sum(max(float(item["delta"]), 0.0) for item in factors), 0.0, 100.0)
        bearish = _clamp(50.0 + sum(max(-float(item["delta"]), 0.0) for item in factors), 0.0, 100.0)
        bias = "bullish" if bullish > bearish else "bearish" if bearish > bullish else "neutral"
        return {
            "bullish": round(bullish, 2),
            "bearish": round(bearish, 2),
            "strength": round(max(bullish, bearish), 2),
            "bias": bias,
            "factors": factors,
        }

    def _timeframe_context(
        self,
        bars: Sequence[_Bar],
        metrics: dict[str, Any],
        structure: dict[str, Any],
    ) -> dict[str, Any]:
        weekly = self._weekly_bars(bars)
        weekly_trend = "range"
        if len(weekly) >= 3:
            closes = [bar.close for bar in weekly]
            if closes[-1] > closes[-2] > closes[-3]:
                weekly_trend = "bullish"
            elif closes[-1] < closes[-2] < closes[-3]:
                weekly_trend = "bearish"
        daily_trend = str(structure.get("trend") or "range")
        alignment = (
            "aligned"
            if daily_trend == weekly_trend and daily_trend != "range"
            else "conflicting"
            if daily_trend != "range" and weekly_trend != "range"
            else "mixed"
        )
        return {
            "daily": {
                "trend": daily_trend,
                "sma20": structure.get("sma20"),
                "sma50": structure.get("sma50"),
                "last_close": metrics.get("last_price"),
            },
            "weekly": {
                "trend": weekly_trend,
                "bars": len(weekly),
                "last_close": _round(weekly[-1].close) if weekly else None,
                "latest_week_partial": weekly[-1].partial if weekly else None,
                "note": (
                    "Weekly candles are aggregated from the stored daily series; "
                    "the latest week may still be in progress."
                ),
            },
            "alignment": alignment,
            "confirmation_basis": "closed bars only",
        }

    def _weekly_bars(self, bars: Sequence[_Bar]) -> list[_Bar]:
        groups: dict[tuple[int, int], list[_Bar]] = defaultdict(list)
        for bar in bars:
            iso = bar.observed_at.isocalendar()
            groups[(iso.year, iso.week)].append(bar)
        weekly: list[_Bar] = []
        for (_, _), group in sorted(groups.items()):
            first = group[0]
            last = group[-1]
            volume = (
                sum(bar.volume or 0.0 for bar in group)
                if any(bar.volume is not None for bar in group)
                else None
            )
            weekly.append(
                _Bar(
                    index=len(weekly),
                    observed_at=last.observed_at,
                    open=first.open,
                    high=max(bar.high for bar in group),
                    low=min(bar.low for bar in group),
                    close=last.close,
                    volume=volume,
                    provider=last.provider,
                    quality=last.quality,
                    freshness=last.freshness,
                    partial=len(group) < 5,
                    reasons=(),
                )
            )
        return weekly

    def _xray(self, analyses: Sequence[dict[str, Any]]) -> dict[str, Any]:
        changes = [
            item["change_pct"]
            for item in analyses
            if item.get("change_pct") is not None
        ]
        advancing = len([value for value in changes if value > 0])
        declining = len([value for value in changes if value < 0])
        unchanged = len([value for value in changes if value == 0])
        breadth = advancing / (advancing + declining) * 100.0 if advancing + declining else None
        bullish = len([item for item in analyses if item.get("trend") == "bullish"])
        bearish = len([item for item in analyses if item.get("trend") == "bearish"])
        index_items = [
            item for item in analyses if str(item.get("asset_class")) == "index"
        ]
        return {
            "sample_size": len(analyses),
            "advancing": advancing,
            "declining": declining,
            "unchanged": unchanged,
            "breadth_ratio_pct": _round(breadth, 2),
            "advance_decline_ratio": _round(
                advancing / declining if declining else None, 2
            ),
            "average_change_pct": _round(mean(changes) if changes else None, 2),
            "median_change_pct": _round(median(changes) if changes else None, 2),
            "bullish_trend_count": bullish,
            "bearish_trend_count": bearish,
            "bullish_participation_pct": _round(
                bullish / len(analyses) * 100.0 if analyses else None, 2
            ),
            "bearish_participation_pct": _round(
                bearish / len(analyses) * 100.0 if analyses else None, 2
            ),
            "direction": (
                "bullish"
                if breadth is not None and breadth >= 60
                else "bearish"
                if breadth is not None and breadth <= 40
                else "mixed"
            ),
            "index_context": (
                {
                    "available": True,
                    "instruments": [
                        {
                            "instrument_id": item["instrument_id"],
                            "symbol": item["symbol"],
                            "change_pct": item.get("change_pct"),
                            "trend": item.get("trend"),
                            "as_of": item.get("as_of"),
                        }
                        for item in index_items
                    ],
                    "basis": "index instruments with stored bars in this scan",
                }
                if index_items
                else {
                    "available": False,
                    "instruments": [],
                    "reason": "No index instrument with a stored OHLCV series is in the analysable sample.",
                }
            ),
            "coverage_note": (
                "Breadth is measured over the analysed stored-bar universe, "
                "not every listed instrument."
            ),
        }

    def _sector_flow(self, analyses: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in analyses:
            groups[str(item.get("sector") or "Unclassified")].append(item)
        rows: list[dict[str, Any]] = []
        for sector, items in groups.items():
            changes = [item["change_pct"] for item in items if item.get("change_pct") is not None]
            momentum = [item["momentum_score"] for item in items if item.get("momentum_score") is not None]
            volumes = [
                item["metrics"].get("relative_volume")
                for item in items
                if item.get("metrics", {}).get("relative_volume") is not None
            ]
            bullish = len([item for item in items if item.get("trend") == "bullish"])
            bearish = len([item for item in items if item.get("trend") == "bearish"])
            rows.append(
                {
                    "sector": sector,
                    "count": len(items),
                    "advancing": len([value for value in changes if value > 0]),
                    "declining": len([value for value in changes if value < 0]),
                    "average_change_pct": _round(mean(changes) if changes else None, 2),
                    "average_momentum_score": _round(mean(momentum) if momentum else None, 2),
                    "relative_strength_pct": _round(mean(changes) if changes else None, 2),
                    "volume_activity": _round(mean(volumes) if volumes else None, 2),
                    "bullish_participation_pct": _round(
                        bullish / len(items) * 100.0 if items else None, 2
                    ),
                    "bearish_participation_pct": _round(
                        bearish / len(items) * 100.0 if items else None, 2
                    ),
                    "sector_available": sector != "Unclassified",
                    "sector_source": ", ".join(
                        sorted(
                            {
                                str(item.get("sector_source") or "")
                                for item in items
                                if item.get("sector_source")
                            }
                        )
                    ),
                    "sector_as_of": max(
                        (
                            str(item.get("sector_as_of") or "")
                            for item in items
                            if item.get("sector_as_of")
                        ),
                        default="",
                    ),
                }
            )
        rows.sort(key=lambda row: (-(row["average_change_pct"] or 0.0), row["sector"]))
        return rows

    def _heatmap(self, analyses: Sequence[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in analyses:
            groups[str(item.get("sector") or "Unclassified")].append(item)
        sectors: list[dict[str, Any]] = []
        for sector, items in groups.items():
            changes = [item["change_pct"] for item in items if item.get("change_pct") is not None]
            sectors.append(
                {
                    "sector": sector,
                    "average_change_pct": _round(mean(changes) if changes else None, 2),
                    "count": len(items),
                    "sector_source": ", ".join(
                        sorted(
                            {
                                str(item.get("sector_source") or "")
                                for item in items
                                if item.get("sector_source")
                            }
                        )
                    ),
                    "sector_as_of": max(
                        (
                            str(item.get("sector_as_of") or "")
                            for item in items
                            if item.get("sector_as_of")
                        ),
                        default="",
                    ),
                    "instruments": [
                        {
                            "instrument_id": item["instrument_id"],
                            "symbol": item["symbol"],
                            "name": item.get("name"),
                            "change_pct": item.get("change_pct"),
                            "momentum_score": item.get("momentum_score"),
                            "relative_volume": item.get("relative_volume"),
                            "market_cap": None,
                        }
                        for item in sorted(
                            items,
                            key=lambda row: -(row.get("change_pct") or 0.0),
                        )
                    ],
                }
            )
        sectors.sort(key=lambda row: -(row["average_change_pct"] or 0.0))
        return {
            "sectors": sectors,
            "sector_available": any(item["sector"] != "Unclassified" for item in sectors),
            "sector_reference": self._sector_reference(),
            "note": "Each tile is an analysed instrument with a stored bar; missing sector metadata is grouped honestly as Unclassified.",
        }

    def _zone_board(self, analyses: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in analyses:
            for role in ("support", "resistance"):
                level = (item.get("levels") or {}).get(role)
                if not level:
                    continue
                rows.append(
                    {
                        "instrument_id": item["instrument_id"],
                        "symbol": item["symbol"],
                        "name": item.get("name"),
                        "sector": item.get("sector"),
                        "zone_type": role,
                        "price": level.get("price"),
                        "zone_low": None,
                        "zone_high": None,
                        "distance_pct": level.get("distance_pct"),
                        "status": level.get("status"),
                        "method": level.get("method"),
                        "as_of": item.get("as_of"),
                        "freshness": item.get("freshness"),
                    }
                )
            for block in (item.get("order_blocks") or [])[-2:]:
                rows.append(
                    {
                        "instrument_id": item["instrument_id"],
                        "symbol": item["symbol"],
                        "name": item.get("name"),
                        "sector": item.get("sector"),
                        "zone_type": f"{block['direction']}_order_block",
                        "price": _round(
                            ((block.get("zone_low") or 0) + (block.get("zone_high") or 0)) / 2
                        ),
                        "zone_low": block.get("zone_low"),
                        "zone_high": block.get("zone_high"),
                        "distance_pct": block.get("distance_pct"),
                        "status": block.get("status"),
                        "method": block.get("confirmation_basis"),
                        "as_of": item.get("as_of"),
                        "freshness": item.get("freshness"),
                    }
                )
        rows.sort(key=lambda row: abs(float(row.get("distance_pct") or 0.0)))
        return rows

    def _liquidity_board(self, analyses: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in analyses:
            for level in (item.get("liquidity") or [])[:5]:
                rows.append(
                    {
                        "instrument_id": item["instrument_id"],
                        "symbol": item["symbol"],
                        "name": item.get("name"),
                        "kind": level.get("kind"),
                        "price": level.get("price"),
                        "distance_pct": level.get("distance_pct"),
                        "side": level.get("side"),
                        "status": level.get("status"),
                        "method": level.get("method"),
                        "as_of": item.get("as_of"),
                    }
                )
        rows.sort(key=lambda row: abs(float(row.get("distance_pct") or 0.0)))
        return rows

    def _market_flow(
        self,
        analyses: Sequence[dict[str, Any]],
        xray: dict[str, Any],
        sectors: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        volumes = [
            item.get("metrics", {}).get("relative_volume")
            for item in analyses
            if item.get("metrics", {}).get("relative_volume") is not None
        ]
        momentum_rows: list[float] = []
        for item in analyses:
            score = item.get("momentum_score")
            if score is not None:
                momentum_rows.append(float(score))
        return {
            "breadth": {
                "advancing": xray["advancing"],
                "declining": xray["declining"],
                "unchanged": xray["unchanged"],
                "breadth_ratio_pct": xray["breadth_ratio_pct"],
            },
            "volume_context": {
                "average_relative_volume": _round(mean(volumes) if volumes else None, 2),
                "instruments_with_volume": len(volumes),
                "note": "Relative volume is current bar volume divided by the prior 20-bar mean.",
            },
            "momentum_distribution": {
                "bullish": len([value for value in momentum_rows if value >= 55]),
                "neutral": len([value for value in momentum_rows if 45 < value < 55]),
                "bearish": len([value for value in momentum_rows if value <= 45]),
            },
            "sector_participation": list(sectors),
            "index_context": xray["index_context"],
            "coverage_note": xray["coverage_note"],
        }

    @staticmethod
    def _fno_contract_status(
        analyses: Sequence[dict[str, Any]],
        catalogue: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Scan the official futures registry, never relabel an equity setup."""
        if not DERIVATIVES_SNAPSHOT_PATH.exists():
            return {
                "supported": False,
                "status": "BLOCKED",
                "coverage": {
                    "contracts_registered": 0,
                    "contracts_scanned": 0,
                    "equities_scanned": len(analyses),
                    "equity_catalogue_size": len(catalogue or analyses),
                },
                "rows": [],
                "bullish": [],
                "bearish": [],
                "reason": "No official NSE F&O futures snapshot has been imported.",
                "required": [
                    "Run scripts/import_nse_fo_bhavcopy.py to cache the official futures "
                    "contract market",
                    "Equity setups must remain separate from futures contracts",
                ],
            }
        try:
            snapshot = json.loads(DERIVATIVES_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {
                "supported": False,
                "status": "UNAVAILABLE",
                "coverage": {
                    "contracts_registered": 0,
                    "contracts_scanned": 0,
                    "equities_scanned": len(analyses),
                    "equity_catalogue_size": len(catalogue or analyses),
                },
                "rows": [],
                "bullish": [],
                "bearish": [],
                "reason": f"The official futures snapshot could not be read: {exc}",
                "required": ["Re-run scripts/import_nse_fo_bhavcopy.py"],
            }
        catalogue_rows = list(catalogue) if catalogue else list(analyses)
        status = scan_futures_snapshot(snapshot, equities=catalogue_rows)
        coverage = dict(status.get("coverage") or {})
        coverage["equities_scanned"] = len(analyses)
        coverage["equity_catalogue_size"] = len(catalogue_rows)
        result = {**status, "coverage": coverage}
        if not status.get("supported"):
            result["required"] = [
                "An unexpired official NSE futures contract in the cached snapshot",
                "Equity setups must remain separate from futures contracts",
            ]
        return result

    @staticmethod
    def _options_contract_status() -> dict[str, Any]:
        """Read the cached official NSE F&O snapshot, never a synthetic chain."""
        if not DERIVATIVES_SNAPSHOT_PATH.exists():
            return {
                "supported": False,
                "status": "BLOCKED",
                "rows": [],
                "fields_unavailable": [
                    "strikes",
                    "calls",
                    "puts",
                    "open_interest",
                    "change_in_open_interest",
                    "volume",
                    "implied_volatility",
                    "put_call_ratio",
                    "expiry",
                ],
                "reason": (
                    "No official derivative snapshot has been imported. "
                    "Rendering strike rows without that source would fabricate market data."
                ),
                "next_requirement": (
                    "Run scripts/import_nse_fo_bhavcopy.py to cache the official "
                    "NSE F&O bhavcopy, then refresh this desk."
                ),
            }
        try:
            snapshot = json.loads(DERIVATIVES_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {
                "supported": False,
                "status": "UNAVAILABLE",
                "rows": [],
                "reason": f"The official derivative snapshot could not be read: {exc}",
                "next_requirement": "Re-run scripts/import_nse_fo_bhavcopy.py.",
            }
        chain = snapshot.get("default_chain") or {}
        rows = chain.get("rows") or []
        if not rows:
            return {
                "supported": False,
                "status": "EMPTY",
                "rows": [],
                "reason": "The cached official F&O snapshot contains no index-option rows.",
                "next_requirement": "Re-run scripts/import_nse_fo_bhavcopy.py for a completed session.",
            }
        return {
            "supported": True,
            "status": "AVAILABLE",
            "rows": rows,
            "symbol": snapshot.get("default_symbol"),
            "expiry": chain.get("expiry") or snapshot.get("default_expiry"),
            "trade_date": snapshot.get("trade_date"),
            "generated_at": snapshot.get("generated_at"),
            "source": snapshot.get("source"),
            "underlying_price": chain.get("underlying_price"),
            "total_call_oi": chain.get("total_call_oi"),
            "total_put_oi": chain.get("total_put_oi"),
            "pcr": chain.get("pcr"),
            "underlyings": snapshot.get("underlyings") or [],
            "iv_method": snapshot.get("iv_method"),
            "disclaimer": snapshot.get("disclaimer"),
            "fields_available": snapshot.get("fields") or [],
            "reason": (
                "Strike-wise calls, puts, open interest, change in open interest, "
                "volume and model IV from the official NSE F&O bhavcopy."
            ),
        }

    @staticmethod
    def _futures_contract_status() -> dict[str, Any]:
        """Read official index and stock futures from the cached F&O snapshot."""
        if not DERIVATIVES_SNAPSHOT_PATH.exists():
            return {
                "supported": False,
                "status": "BLOCKED",
                "rows": [],
                "reason": "No official derivative snapshot has been imported.",
                "next_requirement": (
                    "Run scripts/import_nse_fo_bhavcopy.py to cache the official "
                    "NSE F&O bhavcopy, then refresh this desk."
                ),
            }
        try:
            snapshot = json.loads(DERIVATIVES_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {
                "supported": False,
                "status": "UNAVAILABLE",
                "rows": [],
                "reason": f"The official derivative snapshot could not be read: {exc}",
                "next_requirement": "Re-run scripts/import_nse_fo_bhavcopy.py.",
            }
        futures = snapshot.get("futures") or {}
        chains = futures.get("chains") or {}
        symbol = str(futures.get("default_symbol") or "")
        if not chains or not symbol:
            return {
                "supported": False,
                "status": "EMPTY",
                "rows": [],
                "reason": "The cached official F&O snapshot contains no futures contracts.",
                "next_requirement": "Re-run scripts/import_nse_fo_bhavcopy.py for a completed session.",
            }
        expiries = sorted(chains.get(symbol, {}))
        rows = [
            {"symbol": symbol, **chains[symbol][expiry]}
            for expiry in expiries
            if chains[symbol].get(expiry)
        ]
        default_expiry = str(futures.get("default_expiry") or (expiries[0] if expiries else ""))
        default_contract = chains.get(symbol, {}).get(default_expiry, {})
        return {
            "supported": True,
            "status": "AVAILABLE",
            "rows": rows,
            "symbol": symbol,
            "expiry": default_expiry,
            "expiries": expiries,
            "contracts": int(futures.get("contracts") or snapshot.get("futures_contracts") or 0),
            "underlyings": futures.get("underlyings") or [],
            "underlying_count": len(futures.get("underlyings") or []),
            "trade_date": snapshot.get("trade_date"),
            "generated_at": snapshot.get("generated_at"),
            "source": snapshot.get("source"),
            "contract": default_contract,
            "fields": futures.get("fields") or [],
            "reason": (
                "Official NSE index and stock futures with expiry, close, open "
                "interest, change in open interest, volume and lot size."
            ),
            "note": futures.get("note"),
        }

    def _board_row(self, detail: dict[str, Any]) -> dict[str, Any]:
        metrics = detail.get("metrics") or {}
        signal = detail.get("signal") or {}
        structure = detail.get("structure") or {}
        levels = detail.get("levels") or {}
        return {
            "instrument_id": detail["instrument_id"],
            "symbol": detail.get("symbol") or detail["instrument_id"].split(":", 1)[-1],
            "name": detail.get("name"),
            "exchange": detail.get("exchange"),
            "sector": detail.get("sector"),
            "asset_class": detail.get("asset_class"),
            "last_price": metrics.get("last_price", detail.get("last_price")),
            "change_pct": metrics.get("change_pct", detail.get("change_pct")),
            "last_price_basis": detail.get("price_basis"),
            "as_of": detail.get("as_of"),
            "freshness": detail.get("freshness"),
            "provider": detail.get("provider"),
            "data_state": detail.get("data_state"),
            "trend": structure.get("trend"),
            "relative_volume": metrics.get("relative_volume"),
            "vwap_relation": metrics.get("vwap_relation"),
            "vwap20": metrics.get("vwap20"),
            "rsi14": metrics.get("rsi14"),
            "momentum_score": detail.get("momentum_score", metrics.get("momentum_score")),
            "bullish_score": detail.get("bullish_score"),
            "bearish_score": detail.get("bearish_score"),
            "directional_bias": detail.get("directional_bias"),
            "directional_strength": detail.get("directional_strength"),
            "order_block_status": signal.get("status"),
            "order_block_direction": signal.get("direction"),
            "order_block_zone": signal.get("entry_zone"),
            "risk_reward": signal.get("risk_reward"),
            "support": (levels.get("support") or {}).get("price"),
            "resistance": (levels.get("resistance") or {}).get("price"),
            "entry_zone": levels.get("entry_zone"),
            "invalidation": levels.get("invalidation"),
            "targets": levels.get("targets"),
            "score_factors": detail.get("score_factors"),
            "timeframe_context": detail.get("timeframe_context"),
            "coverage": detail.get("coverage"),
        }

    def _replay_point(self, bar: _Bar, prefix: Sequence[_Bar]) -> dict[str, Any]:
        closes = [item.close for item in prefix]
        sma20 = _as_float(sma(closes, 20))
        rsi14 = _as_float(rsi(closes, 14))
        vwap20 = self._vwap_at(prefix, len(prefix) - 1, 20)
        prior_volume = _as_float(
            average_volume(
                [item.volume or 0.0 for item in prefix[max(0, len(prefix) - 21) : len(prefix) - 1]],
                20,
            )
        )
        relative_volume = (
            bar.volume / prior_volume
            if bar.volume is not None and prior_volume and prior_volume > 0
            else None
        )
        atr14 = self._atr_at(prefix, len(prefix) - 1)
        return {
            "time": bar.time,
            "cursor": bar.index,
            "sma20": _round(sma20),
            "rsi14": _round(rsi14, 2),
            "vwap20": _round(vwap20),
            "relative_volume": _round(relative_volume, 2),
            "atr14": _round(atr14),
            "inputs": len(prefix),
        }

    def _chart_bars(self, bars: Sequence[_Bar], limit: int) -> list[dict[str, Any]]:
        window = bars[-limit:]
        return [
            {
                "time": bar.time,
                "open": _round(bar.open),
                "high": _round(bar.high),
                "low": _round(bar.low),
                "close": _round(bar.close),
                "volume": _round(bar.volume, 2),
                "vwap20": _round(self._vwap_at(bars, bar.index, 20)),
                "partial": bar.partial,
                "quality": bar.quality,
                "freshness": bar.freshness,
                "provider": bar.provider,
            }
            for bar in window
        ]

    @staticmethod
    def _chart_zones(blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "direction": block["direction"],
                "status": block["status"],
                "zone_low": block["zone_low"],
                "zone_high": block["zone_high"],
                "origin_time": block["origin_time"],
                "confirmed_time": block["confirmed_time"],
                "label": f"{block['direction'].title()} OB",
            }
            for block in blocks
            if block.get("status") != "invalidated"
        ]

    @staticmethod
    def _chart_levels(
        support: dict[str, Any] | None,
        resistance: dict[str, Any] | None,
        signal: dict[str, Any],
    ) -> list[dict[str, Any]]:
        levels: list[dict[str, Any]] = []
        if support and support.get("price") is not None:
            levels.append({"label": "Support", "value": support["price"], "tone": "support"})
        if resistance and resistance.get("price") is not None:
            levels.append({"label": "Resistance", "value": resistance["price"], "tone": "resistance"})
        if signal.get("stop") is not None:
            levels.append({"label": "Invalidation", "value": signal["stop"], "tone": "risk"})
        return levels[:4]

    def _evidence(
        self,
        metrics: dict[str, Any],
        structure: dict[str, Any],
        signal: dict[str, Any],
        blocks: Sequence[dict[str, Any]],
        liquidity: Sequence[dict[str, Any]],
    ) -> list[str]:
        evidence = [
            f"Close {metrics.get('last_price')} against 20-bar mean {structure.get('sma20')} and 50-bar mean {structure.get('sma50')}.",
            f"RSI(14) {metrics.get('rsi14')}; relative volume {metrics.get('relative_volume')}; rolling VWAP {metrics.get('vwap20')}.",
            f"Market structure is {structure.get('trend')} from closed candles with 2-bar confirmed pivots.",
        ]
        if blocks:
            latest = blocks[-1]
            evidence.append(
                f"Latest {latest['direction']} zone {latest['zone_low']}–{latest['zone_high']} "
                f"is {latest['status']} (confirmation: {latest['confirmation_basis']})."
            )
        else:
            evidence.append("No order block met the displacement and structure-break rule.")
        if signal.get("status") == "none":
            evidence.append("No confirmed, non-invalidated block is available for a setup.")
        elif signal.get("status") == "developing":
            evidence.append("The visible setup is developing and is not actionable.")
        if liquidity:
            evidence.append(
                f"Nearest detected liquidity is {liquidity[0]['kind']} at {liquidity[0]['price']}."
            )
        return evidence

    def _sector_evidence(self, instrument: dict[str, Any]) -> tuple[str | None, str, str]:
        """Return the sector and the source/effective date that justify it."""
        metadata = instrument.get("metadata") or {}
        for key in ("sector", "industry", "sector_name"):
            value = metadata.get(key)
            if value:
                return (
                    str(value),
                    str(metadata.get("sector_source") or "instrument metadata"),
                    str(metadata.get("sector_as_of") or ""),
                )
        # The instrument master does not carry a sector for most catalogue
        # rows. Fall back to the same dated NSE constituent snapshot used by
        # Market Overview, keyed by the exact symbol. This is an authoritative
        # exchange-published industry, not a guessed classification.
        symbol = str(instrument.get("symbol") or "").strip().upper()
        if symbol:
            try:
                reference = load_index_reference()
                membership = reference.membership(symbol)
            except Exception:  # noqa: BLE001 - a missing packaged reference stays unclassified
                membership = None
            if membership is not None and membership.industry:
                return membership.industry, reference.source, reference.as_of
        return None, "", ""

    def _sector(self, instrument: dict[str, Any]) -> str | None:
        return self._sector_evidence(instrument)[0]

    @staticmethod
    def _sector_reference() -> dict[str, str]:
        """The packaged classification's source and effective date."""
        try:
            reference = load_index_reference()
        except Exception:  # noqa: BLE001 - an absent snapshot is reported, not invented
            return {}
        return {
            "as_of": reference.as_of,
            "source": reference.source,
            "method": reference.method,
        }

    def _cutoff(self, end_date: str | None) -> datetime:
        now = self._clock()
        if not end_date:
            return now
        text = str(end_date).strip()
        try:
            if len(text) == 10:
                parsed = date.fromisoformat(text)
                local = datetime.combine(parsed, time(23, 59, 59, 999999), tzinfo=_IST)
            else:
                local = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if local.tzinfo is None:
                    local = local.replace(tzinfo=_IST)
        except ValueError as exc:
            raise ValueError("end_date must be an ISO date (YYYY-MM-DD) or datetime") from exc
        cutoff = local.astimezone(UTC)
        return min(cutoff, now) if cutoff > now else cutoff

    @staticmethod
    def _methodology() -> dict[str, Any]:
        return {
            "version": METHODOLOGY_VERSION,
            "order_block": (
                "A bullish block is the most recent down-close candle before a "
                "closed-candle displacement of at least 0.9 ATR that closes above "
                "the last 2-bar confirmed swing high. A bearish block is the mirror. "
                "The zone is the origin candle body. Confirmation is fixed at the "
                "break bar; a developing displacement without a break is labelled "
                "developing and is never actionable."
            ),
            "invalidation": (
                "Bullish: a later close below the zone low. Bearish: a later close "
                "above the zone high. A test that holds is reported as tested, not "
                "as a fresh signal."
            ),
            "structure": (
                "Swing points require two bars on both sides and are confirmed at "
                "the second right-hand bar. Break of structure requires a close, "
                "never an intrabar wick."
            ),
            "vwap": (
                "VWAP is the 20-bar rolling volume-weighted typical price over the "
                "stored interval. It is not an exchange session VWAP when the "
                "interval is daily."
            ),
            "momentum": (
                "Momentum score starts at 50 and combines 5/20-bar returns, RSI(14), "
                "close vs 20/50-bar means, rolling VWAP and relative volume. It is "
                "a ranking score, not a probability."
            ),
            "data": (
                "Only usable stored bars are read. Partial bars are excluded from "
                "structure and confirmation. Missing volume yields an unavailable "
                "relative-volume value, never zero."
            ),
            "order_flow": "Order blocks are technical price zones, not direct access to institutional orders.",
        }

    @staticmethod
    def _replay_methodology() -> dict[str, Any]:
        return {
            "look_ahead": (
                "The series is cut at end_date before calculation. At cursor i, "
                "SMA20, RSI14, rolling VWAP, relative volume and ATR use only "
                "bars[0..i]. There is no future-bar input."
            ),
            "data": "Replay reads stored OHLCV bars and is labelled historical; it is not a live quote.",
            "timeframe": (
                "Daily replay reads stored daily bars. Weekly replay aggregates the same "
                "stored daily bars (open first, high max, low min, close last, volume sum) "
                "and marks the latest partial week."
            ),
            "confirmation": "Indicators require their minimum history and remain null until then.",
        }
