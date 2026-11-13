"""Evidence-based instrument and sector mapping.

An event is linked to an instrument only when the event's own text names that
instrument — by symbol, by company name, or by one of the aliases NM-02's
instrument carries. Nothing here guesses from a category, and a company event
that names nobody stays unmapped.

Relationships are stated, not implied:

* ``direct`` — the instrument is named in the title or the summary;
* ``indirect`` — only the sector is named, so the instrument is exposed through
  its sector rather than by name (sector and macroeconomic events only).

A ticker that appears only inside another word is not an instrument link. That
rule matters for short BSE symbols: ``BIL`` inside "billion" is not a company
mention, and publishing it as one made an unrelated headline look stock-linked.
Indirect links are capped and ordered deterministically, so one macro headline
cannot explode into an unbounded mapping list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.novamarket.radar.models import (
    EventMapping,
    Relationship,
    normalize_text,
    similarity,
)

#: A single event may touch at most this many sector-linked instruments.
MAX_INDIRECT = 25


@dataclass(frozen=True, slots=True)
class InstrumentView:
    """The slice of an NM-02 instrument the radar needs, and nothing more."""

    instrument_id: str
    symbol: str
    name: str
    asset_class: str = "equity"
    exchange: str = ""
    market: str = ""
    sectors: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.instrument_id or ":" not in self.instrument_id:
            raise ValueError(
                f"instrument_id must be MIC:SYMBOL, got {self.instrument_id!r}"
            )
        object.__setattr__(self, "sectors", tuple(str(item).strip() for item in self.sectors if str(item).strip()))
        object.__setattr__(self, "aliases", tuple(str(item).strip() for item in self.aliases if str(item).strip()))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InstrumentView":
        """Build from an NM-02 ``Instrument.to_dict()`` without importing it."""
        metadata = payload.get("metadata") or {}
        sectors: list[str] = []
        raw_sector = metadata.get("sector")
        if isinstance(raw_sector, str) and raw_sector.strip():
            sectors.append(raw_sector)
        raw_sectors = metadata.get("sectors")
        if isinstance(raw_sectors, (list, tuple)):
            sectors.extend(str(item) for item in raw_sectors)
        aliases = metadata.get("aliases") or ()
        if isinstance(aliases, str):
            aliases = (aliases,)
        return cls(
            instrument_id=str(payload.get("instrument_id") or ""),
            symbol=str(payload.get("symbol") or ""),
            name=str(payload.get("name") or ""),
            asset_class=str(payload.get("asset_class") or "equity"),
            exchange=str(payload.get("exchange") or ""),
            market=str(metadata.get("market") or ""),
            sectors=tuple(sectors),
            aliases=tuple(str(item) for item in aliases),
        )

    def terms(self) -> tuple[str, ...]:
        values = {self.symbol, self.name, *self.aliases}
        return tuple(sorted((value.strip() for value in values if value.strip()), key=len, reverse=True))


def _word_match(term: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(normalize_text(term))}\b", text) is not None


def map_event(
    *,
    title: str,
    summary: str,
    entity_names: Sequence[str],
    instruments: Iterable[InstrumentView],
    sector_relevance: bool,
) -> tuple[EventMapping, ...]:
    """Map one event to instruments by name, with the match that justified it."""
    text = normalize_text(f"{title}. {summary}")
    named_entities = {normalize_text(name) for name in entity_names}
    direct: list[EventMapping] = []

    for instrument in instruments:
        matched_term = ""
        for term in instrument.terms():
            if _word_match(term, text):
                matched_term = term
                break
        if matched_term:
            direct.append(
                EventMapping(
                    instrument_id=instrument.instrument_id,
                    relationship=Relationship.DIRECT,
                    reason=f"the event text names {matched_term!r}",
                    sector=instrument.sectors[0] if instrument.sectors else "",
                )
            )
            continue
        if normalize_text(instrument.name) in named_entities:
            direct.append(
                EventMapping(
                    instrument_id=instrument.instrument_id,
                    relationship=Relationship.DIRECT,
                    reason=f"the extractor matched the entity {instrument.name!r}",
                    sector=instrument.sectors[0] if instrument.sectors else "",
                )
            )
            continue

    if not sector_relevance or direct:
        return tuple(direct)

    sector_hits: list[EventMapping] = []
    for instrument in instruments:
        for sector in instrument.sectors:
            if sector and similarity(sector, text) >= 0.34:
                sector_hits.append(
                    EventMapping(
                        instrument_id=instrument.instrument_id,
                        relationship=Relationship.INDIRECT,
                        reason=f"the event names the {sector!r} sector",
                        sector=sector,
                    )
                )
                break
        if len(sector_hits) >= MAX_INDIRECT:
            break
    return tuple(sector_hits)


def sectors_from(mappings: Sequence[EventMapping]) -> tuple[str, ...]:
    return tuple(sorted({mapping.sector for mapping in mappings if mapping.sector}))


def markets_from(
    mappings: Sequence[EventMapping], instruments: Iterable[InstrumentView]
) -> tuple[str, ...]:
    index = {instrument.instrument_id: instrument for instrument in instruments}
    markets = set()
    for mapping in mappings:
        instrument = index.get(mapping.instrument_id)
        if instrument is None:
            continue
        if instrument.market:
            markets.add(instrument.market.upper())
        elif instrument.instrument_id.startswith(("XNSE:", "XBOM:")):
            markets.add("IN")
        elif instrument.instrument_id.startswith(("XNAS:", "XNYS:", "ARCX:", "XASE:")):
            markets.add("US")
    return tuple(sorted(markets))


__all__ = [
    "InstrumentView",
    "MAX_INDIRECT",
    "map_event",
    "markets_from",
    "sectors_from",
]
