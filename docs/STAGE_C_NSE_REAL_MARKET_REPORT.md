# Stage C NSE real-market evidence — XNSE:RELIANCE

**Date:** 27 November 2026
**Status:** PARTIAL
**Environment:** tmp/stage_c_v2_2026_11_27/xnse

## Session and provenance

The isolated NSE runtime was initially disabled with auto-start disabled. At 10:17 IST, the verified session endpoint returned eligible=true, calendar_verified=true, trading date 2026-11-27 and exchange XNSE.

The instrument remained XNSE:RELIANCE and Yahoo mapped it to RELIANCE.NS. The provider observation was timestamped 04:47:39 UTC (10:17:39 IST), retrieved at 04:47:40 UTC, identified source timezone Asia/Kolkata and carried Yahoo provider metadata. The application classified the quote as fresh, with age 9.407 seconds at retrieval. Yahoo is an unofficial provider and this is not evidence of licensed exchange real-time delivery.

## What the run established

| Check | Result | Evidence |
|---|---|---|
| Exchange identity and provider mapping | PASS | XNSE:RELIANCE, RELIANCE.NS, provider Yahoo, NSE exchange metadata. |
| Verified session | PASS | Session response eligible and calendar verified for 2026-11-27. |
| Quote and supported ingestion | PASS | Quote refresh completed in 8.220 s. Supported ingestion completed in 5.449 s; one requested instrument succeeded, with zero provider failures and zero rejected bars. |
| Retrieved configured intervals | PARTIAL | Stored 1m/5m/20m/1d totals were 255/249/250/248. Latest source bars were at 10:16, 10:10, 09:55 and 26 November respectively. The newest bars were valid but classified delayed. |
| Monitoring | PASS | One instrument discovered, eligible and processed; zero deferred and zero rejected. |
| Decision path | PARTIAL | One evaluation and one signal; zero proposals, approvals, rejections or errors. No eligible forecast-driven action resulted. |
| Risk approval and paper order | NOT APPLICABLE | There was no eligible proposal to submit to NM-08; NM-10 was not called. |
| Account state | PASS | INR 100,000 portfolio capital, realized P&L 0, zero positions, orders, fills and execution intents. Two posted setup transactions each balanced against two ledger entries. |
| Exit and matured outcome | NOT APPLICABLE | No entry, open position or matured forecast outcome existed. |
| Policy promotion | PASS | Automatic policy promotion was false. |

The decision run completed in 40.07 ms. Forecast-horizon lookup completed in 21.34 ms, and the TradingWatch request completed in 103.92 ms. These timings establish route responsiveness for this bounded run; they do not establish a real forecast, an order lifecycle or a market-day soak.

## Timeframe cross-check

A read-only comparison found 250 stored 20m bars. For 60 bars, all four referenced 5m component bars were present; open, high, low, close and volume matched exactly for all 60. There were zero OHLCV mismatches. The other 190 groups could not be independently compared because their referenced 5m records were not present in the isolated store. No stored 20m bar carried a partial flag. This makes the 20m aggregation evidence partial, not a complete proof of every stored bar.

The latest 1d bar was for the completed 26 November session. No 27 November closing bar was available or expected during the morning run.

## Operator workflow and shutdown

The actual autonomy page loaded in Chromium. Pause persisted the intended autonomous mode; resume without a mode restored it when paper-only acknowledgement was supplied. The page was paused again, resumed through the UI, and emergency-stopped. The accepted run's screenshot shows the active checkpoint before final stop. The final API state was disabled, auto_start=false, and no lease. Recovery completed without auto-resuming and with no unresolved records.

The isolated audit JSONL had zero occurrences of the recorded operator-pause, operator-resume and emergency-stop control phrases. Runtime state transitions were observable, but the C5 audit-record requirement is therefore still open.

## Evidence

- [Accepted JSONL journal](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse.jsonl)
- [Accepted run summary](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse.json)
- [Autonomy page screenshot](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse_autonomy.png)
- [Read-only store cross-check](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_readonly_crosscheck.json)
- [Earlier failed attempt, preserved](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044052250394Z_xnse.jsonl)

## Open requirements

The morning run did not observe NSE session close, a final completed daily bar, a naturally eligible NM-08 proposal, an NM-10 order, an exit, reconciliation of a trade, forecast outcome measurement or advisory learning evidence. C1 and C3 remain PARTIAL; C7 remains NOT VERIFIED.
