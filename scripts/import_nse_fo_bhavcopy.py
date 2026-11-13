"""Cache the official NSE F&O bhavcopy as a contract/option-chain snapshot.

The snapshot contains only provider fields plus a clearly labelled model IV.
No chain is generated when the official file is unavailable.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from novacontrol.novamarket.marketdata.derivatives import (
    DerivativesUnavailable,
    load_nse_fo_snapshot,
    save_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="trade date (YYYY-MM-DD); defaults to the last weekday")
    parser.add_argument("--output", default="data/derivatives_snapshot.json")
    args = parser.parse_args(argv)
    target = date.fromisoformat(args.date) if args.date else None
    try:
        snapshot = load_nse_fo_snapshot(target)
    except DerivativesUnavailable as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    path = save_snapshot(snapshot, Path(args.output))
    print(
        json.dumps(
            {
                "ok": snapshot["ok"],
                "output": str(path),
                "trade_date": snapshot["trade_date"],
                "contracts": snapshot["contracts"],
                "underlyings": len(snapshot["underlyings"]),
                "default_symbol": snapshot["default_symbol"],
                "default_expiry": snapshot["default_expiry"],
                "rows": len(snapshot["default_chain"].get("rows", [])),
                "pcr": snapshot["default_chain"].get("pcr"),
                "iv_method": snapshot["iv_method"],
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
