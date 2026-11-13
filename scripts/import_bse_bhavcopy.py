"""Import real BSE end-of-day bars into the canonical market-data store.

The importer writes only symbols already registered in the BSE catalogue and
checkpoints completed sessions.  It is the BSE counterpart of
``import_nse_bhavcopy.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from novacontrol.novamarket.marketdata.bse_bhavcopy import import_bse_bhavcopy
from novacontrol.novamarket.marketdata.nse_bhavcopy import trading_days
from novacontrol.novamarket.marketdata.store import MarketDataStore


def _last_weekday(value: date) -> date:
    cursor = value
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _start_for_sessions(end: date, sessions: int) -> date:
    days = trading_days(end - timedelta(days=max(7, sessions * 2 + 14)), end)
    if len(days) < sessions:
        raise SystemExit("not enough weekdays in the requested window")
    return days[-sessions]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="data/novamarket.sqlite3")
    parser.add_argument("--checkpoint", default="data/bse_bhavcopy_checkpoint.json")
    parser.add_argument("--end", help="last session (YYYY-MM-DD); defaults to the last weekday")
    parser.add_argument("--start", help="first session (YYYY-MM-DD)")
    parser.add_argument("--sessions", type=int, default=30)
    parser.add_argument("--max-sessions", type=int, default=30)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    end = _last_weekday(date.fromisoformat(args.end) if args.end else date.today())
    start = (
        date.fromisoformat(args.start)
        if args.start
        else _start_for_sessions(end, max(1, args.sessions))
    )
    if start > end:
        raise SystemExit("--start must not be after --end")

    store = MarketDataStore(Path(args.database))
    try:
        def progress(row: dict[str, Any]) -> None:
            if not args.quiet:
                print(
                    f"{row['date']}: rows={row['rows']} registered={row['registered']} "
                    f"inserted={row['inserted']} duplicate={row['duplicate']} "
                    f"conflict={row['conflict']}",
                    flush=True,
                )

        report = import_bse_bhavcopy(
            store,
            start=start,
            end=end,
            checkpoint_path=Path(args.checkpoint),
            max_sessions=args.max_sessions,
            progress=progress,
        )
    finally:
        store.close()
    print(json.dumps(report, indent=2, ensure_ascii=True, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
