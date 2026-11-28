"""Stage C R-05: independent verification of stored 20m bars (read-only).

Three separate measurements, never conflated:

A. Component reproducibility - every stored 20m bar keeps its four 5m
   components in ``raw_json``; recompute OHLCV and check the component stamps.
B. Independent persistence and recomputation - how many of those components
   also exist as separately stored 5m bars, and how many 20m bars reproduce
   exactly when recomputed from those independent rows.  A component that
   exists only inside ``raw_json`` is NOT independent corroboration.  Component
   stamps are epoch seconds; the store keeps ISO-8601, so they are normalised
   before lookup.
C. Consumer contract - which interval each consumer is required to read.  This
   is verified by tests, not by this script; the report states the contract.

Usage:
    python scripts/stage_c_20m_verify.py --store <path> --exchange XNSE \
        --out <report.json>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

COMPONENT_INTERVALS = (0, 5, 10, 15)


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _stamp(value: object) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _verify_group(row: sqlite3.Row) -> dict[str, Any]:
    problems: list[str] = []
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except (TypeError, ValueError):
        raw = {}
    components = raw.get("components") if isinstance(raw, dict) else None
    if not isinstance(components, list):
        components = []
    if len(components) != 4:
        problems.append(f"component_count={len(components)}")
    stamps = sorted(
        stamp
        for item in components
        if isinstance(item, dict) and (stamp := _stamp(item.get("timestamp"))) is not None
    )
    if len(stamps) != len(components):
        problems.append("malformed_component_timestamp")
    elif len(stamps) == 4:
        if len(set(stamps)) != 4:
            problems.append("duplicated_component_timestamp")
        offsets = [
            int((moment - stamps[0]).total_seconds() // 60) for moment in stamps
        ]
        if offsets != list(COMPONENT_INTERVALS):
            problems.append(f"non_consecutive_offsets={offsets}")

    if len(components) == 4 and all(isinstance(item, dict) for item in components):
        ordered = sorted(
            components, key=lambda item: str(item.get("timestamp") or "")
        )
        closes = [_decimal(item.get("close")) for item in ordered]
        opens = [_decimal(item.get("open")) for item in ordered]
        highs = [_decimal(item.get("high")) for item in ordered]
        lows = [_decimal(item.get("low")) for item in ordered]
        volumes = [_decimal(item.get("volume")) for item in ordered]
        if None in opens or None in highs or None in lows or None in closes:
            problems.append("component_ohlc_missing")
        else:
            if Decimal(str(row["open"])) != opens[0]:
                problems.append("open_mismatch")
            if Decimal(str(row["high"])) != max(highs):  # type: ignore[type-var]
                problems.append("high_mismatch")
            if Decimal(str(row["low"])) != min(lows):  # type: ignore[type-var]
                problems.append("low_mismatch")
            if Decimal(str(row["close"])) != closes[-1]:
                problems.append("close_mismatch")
            expected_volume = (
                sum(volumes, Decimal(0))
                if all(item is not None for item in volumes)
                else None
            )
            stored_volume = (
                None
                if row["volume"] is None
                else Decimal(str(row["volume"]))
            )
            if expected_volume is not None and stored_volume != expected_volume:
                problems.append("volume_mismatch")
    return {
        "bar_id": row["bar_id"],
        "instrument_id": row["instrument_id"],
        "observed_at": row["observed_at"],
        "component_timestamps": [
            item.get("timestamp") for item in components if isinstance(item, dict)
        ],
        "problems": problems,
    }


def _independent_recompute_matches(
    row: sqlite3.Row, bars: list[sqlite3.Row]
) -> bool:
    """True when the stored 20m OHLCV equals its four independent 5m rows."""

    if len(bars) != 4:
        return False
    ordered = sorted(bars, key=lambda item: str(item["observed_at"]))
    try:
        opens = [Decimal(str(item["open"])) for item in ordered]
        highs = [Decimal(str(item["high"])) for item in ordered]
        lows = [Decimal(str(item["low"])) for item in ordered]
        closes = [Decimal(str(item["close"])) for item in ordered]
        volumes = [Decimal(str(item["volume"])) for item in ordered if item["volume"] is not None]
    except (InvalidOperation, TypeError, ValueError):
        return False
    if len(volumes) != 4:
        return False
    return (
        Decimal(str(row["open"])) == opens[0]
        and Decimal(str(row["high"])) == max(highs)
        and Decimal(str(row["low"])) == min(lows)
        and Decimal(str(row["close"])) == closes[-1]
        and Decimal(str(row["volume"])) == sum(volumes, Decimal(0))
    )


def verify(db_path: Path, exchange: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        rows = connection.execute(
            "SELECT * FROM market_bars WHERE interval='20m' "
            "ORDER BY instrument_id, observed_at"
        ).fetchall()
        repeat = 0
        covered = 0
        independently_recomputed = 0
        components_total = 0
        problems: list[dict[str, Any]] = []
        for row in rows:
            group = _verify_group(row)
            if group["problems"]:
                problems.append(group)
            else:
                repeat += 1
            for stamp in group["component_timestamps"]:
                components_total += 1
                moment = _stamp(stamp)
                iso = moment.astimezone(UTC).isoformat() if moment else str(stamp)
                exists = connection.execute(
                    "SELECT 1 FROM market_bars WHERE instrument_id=? "
                    "AND interval='5m' AND observed_at=? LIMIT 1",
                    (row["instrument_id"], iso),
                ).fetchone()
                if exists is not None:
                    covered += 1
            independent_rows = [
                found
                for stamp in group["component_timestamps"]
                if (moment := _stamp(stamp)) is not None
                and (
                    found := connection.execute(
                        "SELECT * FROM market_bars WHERE instrument_id=? "
                        "AND interval='5m' AND observed_at=? LIMIT 1",
                        (row["instrument_id"], moment.astimezone(UTC).isoformat()),
                    ).fetchone()
                )
                is not None
            ]
            if _independent_recompute_matches(row, independent_rows):
                independently_recomputed += 1
        return {
            "exchange": exchange,
            "store": str(db_path),
            "total_20m_bars": len(rows),
            "component_reproducible": repeat,
            "component_reproducibility_rate": (
                round(repeat / len(rows), 6) if rows else None
            ),
            "malformed_groups": len(problems),
            "problems": problems[:25],
            "components_total": components_total,
            "components_with_independent_5m_bar": covered,
            "independent_5m_coverage_rate": (
                round(covered / components_total, 6) if components_total else None
            ),
            "missing_independent_5m_components": components_total - covered,
            "independent_recomputed_20m_bars": independently_recomputed,
            "independent_recompute_rate": (
                round(independently_recomputed / len(rows), 6) if rows else None
            ),
            "consumer_contract": {
                "strategy_engine": "1d",
                "forecast_technical_context": "5m for short_term, 1d otherwise",
                "20m": "evidence/aggregation only; costs are never read from 20m",
                "verified_by": "tests/test_stage_c_remediation.py",
            },
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = verify(args.store, args.exchange)
    text = json.dumps(report, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
