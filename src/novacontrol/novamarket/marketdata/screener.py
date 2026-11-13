"""The Market Overview screener.

The screen is deliberately a read over NM-02's existing tables. It does not
create a second quote store or derive a price when none exists. Exchange, index
and market-cap filters use a versioned local copy of NSE's published index
constituent lists; freshness and source are returned with every quote.

Terms used by this module:

* **stored quote** — the latest observation a provider actually returned;
* **last bar** — the latest stored daily OHLCV bar, used only for the 1-day
  range/volume columns;
* **market-cap tier** — index-defined: Nifty 100 = large, Nifty Midcap 100 = mid,
  Nifty Smallcap 100 = small, every other symbol = unclassified.

A missing quote is ``None`` and a missing bar is ``None``. No field is filled
from a neighbouring row, a previous company or an assumed price.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

REFERENCE_PATH = Path(__file__).with_name("india_index_reference.json")
DISCLAIMER = (
    "Market Overview is an observation and screening surface. It does not "
    "execute trades, does not provide investment advice, and does not present "
    "a delayed provider quote as a real-time exchange feed."
)
SORTS = (
    ("default", "Default"),
    ("gainers", "Top gainers (%)"),
    ("losers", "Top losers (%)"),
    ("profit", "Profit (1D %)"),
    ("loss", "Loss (1D %)"),
    ("high", "1D high"),
    ("low", "1D low"),
    ("volume", "High volume"),
    ("price", "Market price"),
)
CAPS = (
    ("all", "All market caps"),
    ("large", "Large cap (Nifty 100)"),
    ("mid", "Mid cap (Nifty Midcap 100)"),
    ("small", "Small cap (Nifty Smallcap 100)"),
    ("unclassified", "Unclassified"),
)
MOVERS = (
    ("all", "All movements"),
    ("gainers", "Gainers only"),
    ("losers", "Losers only"),
    ("unchanged", "Unchanged only"),
)
_CAP_ORDER = {"large": 0, "mid": 1, "small": 2, "unclassified": 3}


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _first_number(*values: object) -> float | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _percent(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous * 100.0


@dataclass(frozen=True, slots=True)
class IndexMembership:
    indices: tuple[str, ...]
    market_cap: str
    industry: str
    name: str


@dataclass(frozen=True, slots=True)
class IndexReference:
    as_of: str
    source: str
    method: str
    index_rows: tuple[dict[str, Any], ...]
    symbols: Mapping[str, IndexMembership]

    def membership(self, symbol: str) -> IndexMembership | None:
        return self.symbols.get(str(symbol or "").strip().upper())


@lru_cache(maxsize=1)
def load_index_reference() -> IndexReference:
    """Load the packaged, dated NSE constituent snapshot.

    The snapshot is data, not a live network call. ``as_of`` is surfaced in
    the API so a consumer can see how current the classification is.
    """

    payload = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), dict):
        raise ValueError("the packaged index reference is malformed")
    symbols: dict[str, IndexMembership] = {}
    for key, value in payload["symbols"].items():
        if not isinstance(value, dict):
            continue
        symbols[str(key).upper()] = IndexMembership(
            indices=tuple(str(item) for item in value.get("indices") or ()),
            market_cap=str(value.get("market_cap") or "unclassified"),
            industry=str(value.get("industry") or ""),
            name=str(value.get("name") or key),
        )
    rows = tuple(
        {
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or ""),
            "count": int(item.get("count") or 0),
        }
        for item in payload.get("indices") or ()
        if isinstance(item, dict) and item.get("id")
    )
    return IndexReference(
        as_of=str(payload.get("as_of") or ""),
        source=str(payload.get("source") or ""),
        method=str(payload.get("method") or ""),
        index_rows=rows,
        symbols=symbols,
    )


def _row_index_map(reference: IndexReference) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in reference.index_rows}


def _representatives(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per canonical symbol for aggregate counts.

    The board itself lists NSE and BSE separately. Aggregates must not count the
    same company twice just because it is cross-listed. The representative
    prefers a row that actually has a quote, then the most recent observation.
    """

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            groups.setdefault(symbol, []).append(row)
    return [
        max(
            group,
            key=lambda item: (
                item.get("price") is not None,
                str(item.get("as_of") or ""),
                item.get("exchange") == "NSE",
            ),
        )
        for group in groups.values()
    ]


class MarketBoard:
    """Build one filterable, source-labelled view over the NM-02 store."""

    def __init__(
        self,
        engine: Any,
        *,
        session: Mapping[str, Any] | None = None,
        reference: IndexReference | None = None,
        now: datetime | None = None,
    ) -> None:
        self._engine = engine
        self._session = dict(session or {})
        self._reference = reference or load_index_reference()
        self._now = now or datetime.now(UTC)

    def snapshot(
        self,
        *,
        exchange: str = "all",
        cap: str = "all",
        index: str = "all",
        sector: str = "all",
        sort: str = "default",
        movers: str = "all",
        query: str = "",
        available_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        exchange_key = str(exchange or "all").strip().upper()
        if exchange_key in ("", "ALL", "BOTH"):
            exchange_key = "all"
        if exchange_key not in ("all", "NSE", "BSE"):
            raise ValueError("exchange must be all, NSE or BSE")

        cap_key = str(cap or "all").strip().lower()
        if cap_key not in {item[0] for item in CAPS}:
            raise ValueError("cap must be all, large, mid, small or unclassified")

        index_key = str(index or "all").strip().lower()
        index_map = _row_index_map(self._reference)
        if index_key not in {"all", *index_map}:
            raise ValueError("unknown index filter")

        sector_key = str(sector or "all").strip()
        sort_key = str(sort or "default").strip().lower()
        sort_aliases = {"profit": "gainers", "loss": "losers"}
        effective_sort = sort_aliases.get(sort_key, sort_key)
        if sort_key not in {item[0] for item in SORTS}:
            raise ValueError("unknown sort option")

        mover_key = str(movers or "all").strip().lower()
        if mover_key not in {item[0] for item in MOVERS}:
            raise ValueError("unknown movement filter")

        limit_value = max(1, min(int(limit), 500))
        offset_value = max(0, int(offset))
        text = str(query or "").strip().lower()

        raw_rows = self._engine.board_rows(
            exchange=None if exchange_key == "all" else exchange_key
        )
        rows: list[dict[str, Any]] = []
        industries: set[str] = set()
        for raw in raw_rows:
            row = self._normalize(raw)
            if not row:
                continue
            membership = self._reference.membership(row["symbol"])
            row["index_membership"] = list(membership.indices) if membership else []
            row["market_cap"] = membership.market_cap if membership else "unclassified"
            row["industry"] = membership.industry if membership else ""
            if row["industry"]:
                industries.add(row["industry"])
            if exchange_key != "all" and row["exchange"].upper() != exchange_key:
                continue
            if index_key != "all" and index_key not in row["index_membership"]:
                continue
            if cap_key != "all" and row["market_cap"] != cap_key:
                continue
            if sector_key not in ("", "all") and row["industry"] != sector_key:
                continue
            if text and text not in " ".join(
                str(row.get(key) or "")
                for key in ("symbol", "name", "instrument_id", "industry")
            ).lower():
                continue
            rows.append(row)

        summary = self._summary(rows)
        scoped = rows
        if available_only:
            rows = [row for row in rows if row["price"] is not None]
        if mover_key == "gainers":
            rows = [row for row in rows if row["change_pct"] is not None and row["change_pct"] > 0]
        elif mover_key == "losers":
            rows = [row for row in rows if row["change_pct"] is not None and row["change_pct"] < 0]
        elif mover_key == "unchanged":
            rows = [row for row in rows if row["change_pct"] is not None and row["change_pct"] == 0]

        ordered = self._sort(rows, effective_sort)
        selected = ordered[offset_value : offset_value + limit_value]
        return {
            "ok": True,
            "generated_at": self._now.isoformat(),
            "data_state": "stored_provider_observations",
            "live": False,
            "price_basis": (
                "Latest stored provider observation. Daily high, low, volume and "
                "previous close come from the last stored 1-day bar when available."
            ),
            "session": self._session or None,
            "filters": {
                "exchange": exchange_key,
                "cap": cap_key,
                "index": index_key,
                "sector": sector_key or "all",
                "sort": sort_key,
                "movers": mover_key,
                "query": text,
                "available_only": available_only,
                "limit": limit_value,
                "offset": offset_value,
            },
            "options": {
                "exchanges": [("all", "NSE + BSE"), ("NSE", "NSE"), ("BSE", "BSE")],
                "caps": CAPS,
                "indices": [("all", "All index universes")] + [
                    (row["id"], row["label"]) for row in self._reference.index_rows
                ],
                "sorts": SORTS,
                "movers": MOVERS,
                "sectors": sorted(industries),
            },
            "reference": {
                "as_of": self._reference.as_of,
                "source": self._reference.source,
                "method": self._reference.method,
                "indices": list(self._reference.index_rows),
            },
            "coverage": {
                "catalogue_total": len(raw_rows),
                "matched": len(scoped),
                "unique_symbols": summary["stocks"],
                "listed_after_availability": len(rows),
                "unique_listed_after_availability": summary["quotes_available"],
                "quotes_available": summary["quotes_available"],
                "quotes_missing": summary["quotes_missing"],
                "quotes_available_listings": summary["quotes_available_listings"],
                "quotes_missing_listings": summary["quotes_missing_listings"],
                "bars_available": summary["bars_available"],
                "returned": len(selected),
                "limit": limit_value,
                "offset": offset_value,
                "total": len(ordered),
                "has_more": offset_value + len(selected) < len(ordered),
                "scanned": len(raw_rows),
                "note": (
                    "Rows without a stored provider quote are shown with unavailable "
                    "prices rather than a placeholder. Use Fetch latest to request a "
                    "bounded live-quote cycle for the visible list."
                ),
            },
            "summary": summary,
            "rows": selected,
            "methodology": {
                "exchange": "Canonical NM-02 exchange field (NSE or BSE cross-listing).",
                "index": "Exact NSE symbol match against the packaged constituent snapshot.",
                "market_cap": (
                    "Index-defined tier, not a full-market-cap calculation: Nifty 100 "
                    "= large, Nifty Midcap 100 = mid, Nifty Smallcap 100 = small."
                ),
                "change": (
                    "Provider observation versus its previous close; when that is "
                    "absent, versus the previous stored daily bar. Otherwise unavailable."
                ),
                "volume": (
                    "The provider quote's own session volume when supplied, otherwise "
                    "the last stored same-session bar; relative volume uses the prior "
                    "20 bars."
                ),
                "counts": (
                    "Aggregate tier, mover and quote counts use distinct canonical "
                    "symbols, so an NSE/BSE cross-listing is one company; the board "
                    "itself still lists each exchange row."
                ),
            },
            "disclaimer": DISCLAIMER,
        }

    def _normalize(self, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        instrument_id = str(raw.get("instrument_id") or "").strip()
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not instrument_id or not symbol:
            return None
        price = _number(raw.get("price"))
        observation_meta = raw.get("observation_metadata")
        observation_raw = raw.get("observation_raw")
        if not isinstance(observation_meta, Mapping):
            observation_meta = {}
        if not isinstance(observation_raw, Mapping):
            observation_raw = {}

        observed_at = raw.get("observed_at")
        observed_dt: datetime | None = None
        age_seconds = None
        if observed_at:
            try:
                observed_dt = datetime.fromisoformat(str(observed_at))
                if observed_dt.tzinfo is None:
                    observed_dt = observed_dt.replace(tzinfo=UTC)
                age_seconds = max(0, int((self._now - observed_dt).total_seconds()))
            except (TypeError, ValueError):
                observed_dt = None
        bar_as_of = raw.get("bar_observed_at")
        bar_dt: datetime | None = None
        if bar_as_of:
            try:
                bar_dt = datetime.fromisoformat(str(bar_as_of))
                if bar_dt.tzinfo is None:
                    bar_dt = bar_dt.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                bar_dt = None
        quote_date = observed_dt.date() if observed_dt else None
        bar_date = bar_dt.date() if bar_dt else None
        same_session = bool(quote_date and bar_date and quote_date == bar_date)

        provider_previous = _number(
            observation_meta.get("previous_close")
            or observation_raw.get("previousClose")
            or observation_raw.get("chartPreviousClose")
        )
        previous_bar_close = _number(raw.get("previous_bar_close"))
        bar_close = _number(raw.get("bar_close"))
        if provider_previous is not None:
            previous_close = provider_previous
            change_basis = "provider previous close"
        elif same_session and previous_bar_close is not None:
            previous_close = previous_bar_close
            change_basis = "previous daily bar"
        elif (
            quote_date
            and bar_date
            and bar_close is not None
            and 0 <= (quote_date - bar_date).days <= 4
        ):
            # A completed bar in the last four calendar days is the latest
            # available close. Older bars are reported as unavailable rather
            # than rebased into a misleading one-day change.
            previous_close = bar_close
            change_basis = "last stored daily close"
        else:
            previous_close = None
            change_basis = None
        change = None if price is None or previous_close is None else price - previous_close
        change_pct = _percent(price, previous_close)

        # The provider quote itself carries the session's own day high/low and
        # volume. Prefer those to a bar, and never relabel a bar from an older
        # session as today's range.
        provider_day_high = _first_number(
            observation_meta.get("day_high"), observation_raw.get("regularMarketDayHigh")
        )
        provider_day_low = _first_number(
            observation_meta.get("day_low"), observation_raw.get("regularMarketDayLow")
        )
        provider_day_volume = _first_number(
            observation_meta.get("day_volume"), observation_raw.get("regularMarketVolume")
        )
        bar_same_high = _number(raw.get("bar_high")) if same_session else None
        bar_same_low = _number(raw.get("bar_low")) if same_session else None
        bar_same_volume = _number(raw.get("bar_volume")) if same_session else None
        if provider_day_high is not None and provider_day_low is not None:
            day_high = provider_day_high
            day_low = provider_day_low
            range_basis = "provider quote day range"
        elif bar_same_high is not None and bar_same_low is not None:
            day_high = bar_same_high
            day_low = bar_same_low
            range_basis = "same-session daily bar"
        else:
            day_high = None
            day_low = None
            range_basis = None
        if provider_day_volume is not None:
            volume = provider_day_volume
            volume_basis = "provider quote day volume"
        elif bar_same_volume is not None:
            volume = bar_same_volume
            volume_basis = "same-session daily bar"
        else:
            volume = None
            volume_basis = None
        average_volume = _number(raw.get("average_volume"))
        relative_volume = (
            volume / average_volume
            if volume is not None and average_volume and average_volume > 0
            else None
        )
        market_state = str(observation_meta.get("market_state") or "").strip()
        if market_state.upper() in ("CLOSED", "POST", "PRE", "PREPRE"):
            market_state = market_state.lower()
        return {
            "instrument_id": instrument_id,
            "symbol": symbol,
            "name": str(raw.get("name") or symbol),
            "exchange": str(raw.get("exchange") or ""),
            "mic": str(raw.get("mic") or ""),
            "asset_class": str(raw.get("asset_class") or ""),
            "currency": str(raw.get("currency") or "INR"),
            "price": _round(price),
            "change": _round(change),
            "change_pct": _round(change_pct, 2),
            "change_basis": change_basis,
            "previous_close": _round(previous_close),
            "as_of": observed_at,
            "age_seconds": age_seconds,
            "provider": raw.get("provider"),
            "quality": raw.get("quality"),
            "freshness": raw.get("freshness"),
            "provider_market_state": market_state or None,
            "day_open": _round(_number(raw.get("bar_open"))) if same_session else None,
            "day_high": _round(day_high),
            "day_low": _round(day_low),
            "day_close": _round(bar_close) if same_session else None,
            "volume": volume,
            "relative_volume": _round(relative_volume, 2),
            "range_basis": range_basis,
            "volume_basis": volume_basis,
            "range_available": day_high is not None and day_low is not None,
            "bar_as_of": bar_as_of,
            "bar_session_matches_quote": same_session,
            "bar_partial": bool(raw.get("bar_partial")) if same_session else False,
            "bar_provider": raw.get("bar_provider"),
            "bar_available": (
                same_session
                and bar_same_high is not None
                and bar_same_low is not None
            ),
            "quote_available": price is not None,
        }

    @staticmethod
    def _summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        listings = list(rows)
        stocks = _representatives(listings)
        priced = [row for row in stocks if row["price"] is not None]
        priced_listings = [row for row in listings if row["price"] is not None]
        changes = [row["change_pct"] for row in priced if row["change_pct"] is not None]
        gainers = len([value for value in changes if value > 0])
        losers = len([value for value in changes if value < 0])
        unchanged = len([value for value in changes if value == 0])
        volumes = [row["volume"] for row in stocks if row["volume"] is not None]
        by_market_cap: dict[str, dict[str, Any]] = {}
        for tier in ("large", "mid", "small", "unclassified"):
            group = [row for row in stocks if row["market_cap"] == tier]
            tier_listings = [row for row in listings if row["market_cap"] == tier]
            tier_changes = [
                row["change_pct"] for row in group if row["change_pct"] is not None
            ]
            by_market_cap[tier] = {
                "count": len(group),
                "listings": len(tier_listings),
                "quotes_available": len(
                    [row for row in group if row["price"] is not None]
                ),
                "quoted_listings": len(
                    [row for row in tier_listings if row["price"] is not None]
                ),
                "gainers": len([value for value in tier_changes if value > 0]),
                "losers": len([value for value in tier_changes if value < 0]),
                "average_change_pct": (
                    round(sum(tier_changes) / len(tier_changes), 2)
                    if tier_changes
                    else None
                ),
            }
        return {
            "listings": len(listings),
            "stocks": len(stocks),
            "quotes_available": len(priced),
            "quotes_missing": len(stocks) - len(priced),
            "quotes_available_listings": len(priced_listings),
            "quotes_missing_listings": len(listings) - len(priced_listings),
            "bars_available": len([row for row in stocks if row["bar_available"]]),
            "gainers": gainers,
            "losers": losers,
            "unchanged": unchanged,
            "advance_decline_ratio": round(gainers / losers, 2) if losers else None,
            "average_change_pct": round(sum(changes) / len(changes), 2) if changes else None,
            "median_change_pct": (
                round(sorted(changes)[len(changes) // 2], 2) if changes else None
            ),
            "total_volume": sum(volumes) if volumes else None,
            "market_cap_counts": {
                tier: by_market_cap[tier]["count"]
                for tier in ("large", "mid", "small", "unclassified")
            },
            "by_market_cap": by_market_cap,
        }

    @staticmethod
    def _sort(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
        def value(row: Mapping[str, Any], key: str) -> float | None:
            raw = row.get(key)
            return _number(raw)

        def available(row: Mapping[str, Any]) -> int:
            return 0 if row.get("price") is not None else 1

        if sort == "gainers":
            return sorted(rows, key=lambda row: (available(row), -(value(row, "change_pct") or -math.inf), row["symbol"]))
        if sort == "losers":
            # A missing quote must remain at the end, not masquerade as the worst loss.
            return sorted(
                rows,
                key=lambda row: (
                    available(row),
                    value(row, "change_pct") if value(row, "change_pct") is not None else math.inf,
                    row["symbol"],
                ),
            )
        if sort == "high":
            return sorted(rows, key=lambda row: (available(row), -(value(row, "day_high") or -math.inf), row["symbol"]))
        if sort == "low":
            return sorted(rows, key=lambda row: (available(row), value(row, "day_low") if value(row, "day_low") is not None else math.inf, row["symbol"]))
        if sort == "volume":
            return sorted(rows, key=lambda row: (available(row), -(value(row, "volume") or -math.inf), row["symbol"]))
        if sort == "price":
            return sorted(rows, key=lambda row: (available(row), -(value(row, "price") or -math.inf), row["symbol"]))
        return sorted(
            rows,
            key=lambda row: (
                _CAP_ORDER.get(str(row.get("market_cap")), 9),
                available(row),
                row["symbol"],
            ),
        )


__all__ = [
    "CAPS",
    "DISCLAIMER",
    "IndexReference",
    "MarketBoard",
    "MOVERS",
    "SORTS",
    "load_index_reference",
]
