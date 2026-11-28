# Stage C BSE real-market evidence — XBOM:RELIANCE

**Date:** 27 November 2026
**Status:** PARTIAL
**Environment:** tmp/stage_c_v2_2026_11_27/xbom

## Session and provenance

The separately provisioned BSE store began disabled with auto-start disabled. At 10:29 IST, its session response returned eligible=true, calendar_verified=true, trading date 2026-11-27 and exchange XBOM.

The canonical instrument remained XBOM:RELIANCE and Yahoo mapped it to RELIANCE.BO. The provider observation timestamp was 04:44:12 UTC (10:14:12 IST), retrieved at 04:59:12 UTC (10:29:12 IST), with source timezone Asia/Kolkata and BSE exchange metadata. The freshness endpoint classified it as fresh at age 907.765 seconds with expected_delay_seconds=900. The quote was close to and slightly beyond that stated delay; this is recorded as returned by the application, not treated as real-time data. Yahoo is unofficial and does not provide licensed exchange real-time evidence.

## BSE decision path

| Check | Result | Evidence |
|---|---|---|
| Canonical identity and provider mapping | PASS | XBOM:RELIANCE, RELIANCE.BO, provider Yahoo. |
| Session and exchange isolation | PASS | Verified XBOM session. Monitoring discovered, qualified and processed one XBOM instrument only; no XNSE candidate appeared. |
| Quote and ingestion | PASS | Quote refresh completed in 7.206 s. Supported ingestion completed in 10.547 s, with one instrument succeeded and zero provider failures or rejected bars. |
| Monitoring | PASS | Completed in 25.25 ms; one eligible instrument processed, zero deferred and zero rejected. |
| Strategy and forecast | PARTIAL | One evaluation and one signal, but no proposal. The decision detail reported no action and insufficient history: eight daily closes were available while the strategy required fourteen. No stored forecast horizon resulted. |
| NM-08 and NM-10 | NOT APPLICABLE | No eligible proposal existed for risk approval or paper execution. NM-10 was not run. |
| Portfolio, wallet and setup ledger | PASS | INR 100,000 portfolio capital, realized P&L 0, zero positions, orders, fills and intents. Both posted setup transactions balanced. |
| Exit and forecast outcome | NOT APPLICABLE | No position or matured forecast outcome existed. |
| Automatic policy promotion | PASS | Disabled by the safety contract. |

## Market-data limits

The isolated store contained 250 1m bars, 251 5m bars, 250 20m bars and eight 1d bars. Latest 1m and 5m source timestamps were 10:14:17 IST and were marked stale at retrieval. The latest 20m bar was 09:55 IST and was valid but delayed. The latest completed daily bar was dated 25 November; the completed 26 November session was missing. These data limitations prevent full BSE daily-history and forecasting acceptance.

The read-only 20m check found 250 stored bars. It verified all five OHLCV values against four referenced 5m components for 60 groups with zero mismatches. The 190 remaining groups lacked component rows in the isolated 5m store, so their four-bar calculations were not independently verified. No stored 20m bar carried a partial flag.

## Runtime, UI and recovery

The actual autonomy page displayed XBOM, the verified open session and the completed BSE monitoring and decision results. Chromium reported no page errors. The operator paused and resumed through the UI, then emergency-stopped. Final state was disabled, auto_start=false and no lease. A clean application restart recovered to disabled without auto-resume or unresolved records.

A separate process-level probe used a fresh runtime-only store and a 60-second lease. After process termination, the next process did not steal the still-valid lease and entered bounded retry. Once the lease expired naturally, auto-start recovered the runtime. Emergency stop then cleared retry and prevented a later restart from auto-starting. No duplicate order or fill was created.

The audit JSONL did not contain operator pause, operator resume or emergency-stop records. Runtime state was observable, but the specific C5 audit requirement remains PARTIAL.

## Evidence

- [Accepted JSONL journal](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom.jsonl)
- [Accepted run summary](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom.json)
- [Autonomy page screenshot](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom_autonomy.png)
- [Read-only store cross-check](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_readonly_crosscheck.json)
- [Process-level lease probe evidence](../tmp/stage_c_v2_2026_11_27/xbom/lease_probe_runs/20261127T045900735591Z/)
- [Earlier harness boot failure, preserved](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045817483724Z_xbom.json)

## Open requirements

The morning run did not verify BSE close, the 26 November completed daily bar, a valid forecast, a naturally eligible NM-08 approval, NM-10 execution, a paper exit or its reconciliation. C2 and C4 remain PARTIAL; C3 remains PARTIAL; C7 remains NOT VERIFIED.
