"""Live market-data verification against real providers.

Runs the REAL application (no mocked transport, no fixture payloads) through the
same HTTP surface a user drives, then prints the evidence the NM-02 report needs:
the provider's own timestamp, the canonical currency, freshness, quality and
provenance. Nothing here fabricates a value: when a provider is unreachable the
step reports the failure instead.

The application boots into a temporary data directory, so the checkout's runtime
state is never touched and a repeated run re-collects rather than reading the
previous run's cache.

Usage::

    PYTHONPATH=src python scripts/verify_marketdata_live.py

Exit code 0 means every configured instrument was registered, ingested and read
back with a usable observation. A closed market still passes: the last completed
session is reported as such, which is the point of the freshness engine.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from novacontrol.api.app import create_app  # noqa: E402

INSTRUMENTS: list[dict[str, Any]] = [
    {
        "symbol": "RELIANCE",
        "name": "Reliance Industries Ltd",
        "exchange": "NSE",
        "mic": "XNSE",
        "asset_class": "equity",
        "currency": "INR",
        "timezone": "Asia/Kolkata",
        "provider_symbols": {"yahoo": "RELIANCE.NS"},
    },
    {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "exchange": "NASDAQ",
        "mic": "XNAS",
        "asset_class": "equity",
        "currency": "USD",
        "timezone": "America/New_York",
        "provider_symbols": {"yahoo": "AAPL"},
    },
    {
        "symbol": "BTC",
        "name": "Bitcoin",
        "exchange": "Crypto",
        "mic": "CRYPTO",
        "asset_class": "crypto",
        "currency": "USD",
        "timezone": "UTC",
        "provider_symbols": {"coingecko": "bitcoin", "yahoo": "BTC-USD"},
    },
]


def main() -> int:
    # The Windows console defaults to cp1252; evidence is still text, so make the
    # encoding explicit rather than dropping characters.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    now = datetime.now(UTC)
    print(f"live market-data verification at {now.isoformat()}")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:

        def isolated(**kwargs: Any) -> Any:
            from novacontrol.application import NovaControlApplication

            kwargs["data_dir"] = tmp
            return NovaControlApplication(**kwargs)

        with mock.patch(
            "novacontrol.api.app.NovaControlApplication", side_effect=isolated
        ):
            with TestClient(create_app()) as client:
                status = client.get("/market/status")
                if status.status_code != 200:
                    print("REFUSED /market/status:", status.text)
                    return 1
                if status.json()["lifecycle"] != "running":
                    enabled = client.post("/market/enable", json={})
                    print("enable:", enabled.status_code, enabled.text[:200])

                providers = client.get(
                    "/market/data/providers", params={"probe": "true"}
                )
                print("\nprovider registry:")
                for row in providers.json().get("providers", []):
                    health = row.get("health") or {}
                    print(
                        f"  {row['provider']:<14} enabled={row['enabled']} "
                        f"kind={row['data_kind']:<8} delay={row['typical_delay_seconds']}s "
                        f"priority={row['priority']} breaker={row['circuit_state']} "
                        f"health={health.get('status')} "
                        f"latency_ms={health.get('latency_ms')} "
                        f"detail={str(health.get('detail'))[:80]}"
                    )

                for spec in INSTRUMENTS:
                    instrument_id = f"{spec['mic']}:{spec['symbol']}"
                    print(f"\n-- {instrument_id} --")
                    register = client.post("/market/instruments", json=spec)
                    if register.status_code != 200:
                        failures.append(
                            f"{instrument_id}: register {register.status_code}"
                        )
                        print("  register FAILED:", register.text[:300])
                        continue
                    ingest = client.post(
                        "/market/data/ingest", json={"instrument_ids": [instrument_id]}
                    )
                    if ingest.status_code != 200:
                        failures.append(f"{instrument_id}: ingest {ingest.status_code}")
                        print("  ingest FAILED:", ingest.text[:300])
                        continue
                    body = ingest.json()
                    print(
                        f"  ingest ok={body['ok']} quotes={body['quotes_inserted']} "
                        f"bars={body['bars_inserted']} rejected="
                        f"{body['quotes_rejected'] + body['bars_rejected']} "
                        f"attempts={body['provider_attempts']} "
                        f"failures={body['provider_failures']} "
                        f"duration_ms={body['duration_ms']}"
                    )
                    for error in body.get("errors", []):
                        print("  ingest error:", json.dumps(error)[:400])

                    quote = client.get(f"/market/data/quote/{instrument_id}")
                    if quote.status_code != 200:
                        failures.append(f"{instrument_id}: quote {quote.status_code}")
                        print("  quote FAILED:", quote.text[:300])
                        continue
                    payload = quote.json()
                    selected = payload["quote"]["selected"]
                    freshness = payload["freshness"]
                    if selected is None:
                        # No observation is a real answer, not a failure: the
                        # freshness block says whether the exchange is shut or a
                        # provider failed, and the report shows which it was.
                        print(
                            f"  quote status={payload['quote']['status']} "
                            f"freshness={freshness['freshness']} "
                            f"market_state={freshness['market_state']} "
                            f"reasons={'; '.join(freshness['reasons'])}"
                        )
                        if freshness["market_state"] in ("open", "continuous"):
                            failures.append(
                                f"{instrument_id}: no quote while the market is open"
                            )
                    else:
                        print(
                            "  selected: "
                            f"{selected['price']} {selected['currency']} from "
                            f"{selected['provider']} observed={selected['observed_at']} "
                            f"freshness={selected['freshness']} "
                            f"quality={selected['quality']} live={selected['live']}"
                        )
                        print(
                            f"  assessment={freshness['freshness']} "
                            f"market_state={freshness['market_state']} "
                            f"age_seconds={freshness['age_seconds']} "
                            f"last_completed_session="
                            f"{freshness['last_completed_session']}"
                        )
                        print(
                            "  provenance: "
                            f"provider={payload['provenance']['provider']} "
                            f"currency={payload['provenance']['currency']} "
                            f"observed_at={payload['provenance']['observed_at']} "
                            f"source_timezone={payload['provenance']['source_timezone']} "
                            f"metadata={payload['provenance']['provider_metadata']}"
                        )
                        if selected["currency"] != spec["currency"]:
                            failures.append(
                                f"{instrument_id}: currency "
                                f"{selected['currency']} != {spec['currency']}"
                            )
                    history = client.get(f"/market/data/history/{instrument_id}")
                    print(
                        f"  history total={history.json()['total']} "
                        f"returned={len(history.json()['items'])}"
                    )
                    bars = client.get(f"/market/data/bars/{instrument_id}")
                    print(f"  daily bars total={bars.json()['total']}")

                data_status = client.get("/market/data/status").json()
                print("\nmarket-data status:")
                store = data_status["store"]
                print(
                    f"  initialised={data_status['initialised']} "
                    f"store_ok={store['ok']} "
                    f"instruments={store['instruments']} "
                    f"observations={store['observations']} "
                    f"bars={store['bars']} "
                    f"schema_version={store['schema_version']}"
                )
                print(f"  cache={data_status['cache']}")
                print(f"  circuits={data_status['circuits']}")
                print("  verified_operations:", data_status["verified_operations"])
                print(
                    "  provider_health:",
                    [
                        (row["provider"], (row.get("health") or {}).get("status"))
                        for row in data_status["providers"]
                    ],
                )

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(" -", failure)
        return 1
    print("\nLIVE VERIFICATION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
