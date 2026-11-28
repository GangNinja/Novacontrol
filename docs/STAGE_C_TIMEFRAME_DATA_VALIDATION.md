# Stage C timeframe and daily-data validation

**Date:** 27 November 2026
**Exchanges:** XNSE and XBOM
**Status:** C3 PARTIAL; C4 PARTIAL

## Source and timestamp handling

All observed records came from Yahoo provider calls through NovaMarket's supported ingestion path. Canonical NSE and BSE instruments remained separate, with provider symbols RELIANCE.NS and RELIANCE.BO. The source timezone was Asia/Kolkata; persisted timestamps were UTC. Session eligibility was checked through the exchange calendar before monitoring.

Yahoo is unofficial and delayed. These observations do not establish licensed exchange real-time data.

## Intervals observed

| Interval | XNSE stored bars | XNSE latest source time / quality | XBOM stored bars | XBOM latest source time / quality |
|---|---:|---|---:|---|
| 1m | 255 | 27 Nov 10:16 IST, valid / delayed | 250 | 27 Nov 10:14:17 IST, stale |
| 5m | 249 | 27 Nov 10:10 IST, valid / delayed | 251 | 27 Nov 10:14:17 IST, stale |
| 20m | 250 | 27 Nov 09:55 IST, valid / delayed | 250 | 27 Nov 09:55 IST, valid / delayed |
| 1d | 248 | 26 Nov completed session, valid / delayed | 8 | 25 Nov completed session, valid / delayed |

The store also contained 15m rows (249 XNSE and 250 XBOM). The Stage C acceptance scope explicitly checked 1m, 5m, 20m and 1d; the additional 15m rows were not evaluated here.

## 20-minute aggregation verification

The read-only cross-check examined every stored 20m bar and its four recorded 5m component timestamps. It used exact decimal comparison for open, high, low, close and volume:

| Exchange | Stored 20m bars | Four source bars available | Exact OHLCV mismatches | Missing source components | Partial flags |
|---|---:|---:|---:|---:|---:|
| XNSE | 250 | 60 | 0 | 190 | 0 |
| XBOM | 250 | 60 | 0 | 190 | 0 |

The 60 complete groups per exchange matched on all five values. The 190 remaining bars per exchange could not be independently verified against four 5m records in the persisted dataset. No gap filling was performed by this check. A zero partial flag does not supply the missing 5m evidence, so it is not treated as proof of those 190 aggregates.

The API's bounded 200-row views independently reported 48 checked groups and 152 missing component groups within the returned window. The full-store cross-check above additionally examined the 50 older 20m rows. This accounts for the 60/190 totals.

## Daily data

NSE had 248 stored daily bars, with the latest dated 26 November, the last completed session before this morning's run. BSE had eight stored daily bars, latest 25 November; the completed 26 November daily session was missing. The BSE history did not satisfy the configured strategy's stated fourteen-close requirement: only eight were available. No daily bar for the still-open 27 November session was treated as complete.

## Acceptance

- C3: PARTIAL. All four requested intervals were retrieved, provenance and timezone were recorded, and 60 four-bar aggregations per exchange matched exactly. Stale BSE intraday records, 190 unavailable 5m component groups per exchange, and unobserved session close remain.
- C4 NSE: PASS for the observed completed-session daily history through 26 November; the final 27 November close was not observed.
- C4 BSE: PARTIAL because the latest completed-session bar was 25 November and 26 November was absent.
- C4 overall: PARTIAL.

## Evidence

- [NSE accepted run](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse.json)
- [BSE accepted run](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom.json)
- [NSE read-only cross-check](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_readonly_crosscheck.json)
- [BSE read-only cross-check](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_readonly_crosscheck.json)
- [Reproducible read-only checker](../.planning/2026-11-27-stage-c-acceptance/stage_c_readonly_crosscheck.py)
