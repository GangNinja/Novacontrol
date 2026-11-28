# Stage C closure report — NSE + BSE

**Decision date:** 27 November 2026
**Closure decision:** NOT CLOSED
**Next permitted checkpoint:** 30 November 2026
**Stage D:** Not started

## Decision

Stage C's gate requires every applicable criterion to be PASS. This run does not satisfy that gate. C1–C5 are PARTIAL, C6 is PASS, and C7 is NOT VERIFIED. The full natural paper lifecycle did not occur, the required control audit records were not found, BSE daily history is incomplete, and the market close was not observed.

This is not a claim that the system failed to operate. The real provider, exchange session, monitoring and bounded decision routes ran for both Indian exchanges. The result is open because the evidence stops before several required downstream and close-of-session requirements.

## Final status

| Criterion | Status | Primary reason |
|---|---|---|
| C1 — NSE decision path | PARTIAL | No stored forecast horizon, eligible proposal or downstream paper lifecycle. |
| C2 — BSE decision path | PARTIAL | Only eight of fourteen required closes; no proposal or paper lifecycle. |
| C3 — 20m and configured intervals | PARTIAL | 60/250 20m bars per exchange matched against complete 5m groups; 190 per exchange lacked persisted components; BSE intraday data was stale. |
| C4 — Indian daily history | PARTIAL | NSE through 26 Nov; BSE only through 25 Nov. |
| C5 — pause/resume | PARTIAL | UI and persisted mode checks passed, but control-specific audit records were absent. |
| C6 — lease/restart recovery | PASS | Process termination, no lease stealing, bounded retry, natural expiry recovery and emergency-stop prevention passed. |
| C7 — natural end-to-end paper lifecycle | NOT VERIFIED | No naturally eligible proposal occurred; no entry or exit was created. |

The detailed sub-requirements and direct artifact paths are in the [final acceptance matrix](STAGE_C_ACCEPTANCE_MATRIX.md).

## Safety and preservation

- NSE and BSE ran in separate isolated paper stores.
- XNYS remained deferred and untouched by this acceptance.
- No real-money order, broker connection, fabricated provider data or forced signal was used.
- No strategy threshold was changed.
- The normal user financial store and user configuration were not opened or modified.
- Runtime was stopped; both final states were disabled with auto_start=false and no lease.
- Evidence from successful and failed harness attempts was preserved.
- No production source changed. No commit, push, deployment, reset, clean or stash occurred.
- No full unit, static-analysis, type-check or CI suite was run in this live acceptance session.

## Fixes made

Only acceptance harness corrections were needed: use the API's accepted manual decision trigger; include explicit paper-only acknowledgement when resuming autonomous mode without a mode; and register startup/shutdown using the installed FastAPI app router. The failed attempts remain linked from the [execution journal](STAGE_C_EXECUTION_JOURNAL.md). No application defect was silently relabeled as fixed.

## Remaining work before closure

1. Recheck BSE completed daily history and sufficient forecast history from the BSE provider mapping.
2. Obtain control-specific audit evidence for pause, resume and emergency stop, then rerun C5.
3. Repeat the 20m validation with enough persisted 5m components to verify all requested groups; capture freshness and completed daily bars.
4. Observe a later authorized session's close and final daily-bar behavior.
5. Continue observing only natural signals. If one occurs, verify NM-08, NM-10, portfolio/ledger reconciliation, exit, forecast outcome and advisory evidence end to end.
6. Repeat performance measurement with radar-correlated event-loop timing; do not treat the current bounded sample as a soak or concurrency pass.

The fallback date is 30 November 2026. If any item still cannot be verified then, retain its open status. Do not start Stage D until all applicable rows are PASS.

## Reports

- [Execution journal](STAGE_C_EXECUTION_JOURNAL.md)
- [NSE real-market report](STAGE_C_NSE_REAL_MARKET_REPORT.md)
- [BSE real-market report](STAGE_C_BSE_REAL_MARKET_REPORT.md)
- [Decision-path acceptance](STAGE_C_DECISION_PATH_ACCEPTANCE.md)
- [Timeframe and daily data](STAGE_C_TIMEFRAME_DATA_VALIDATION.md)
- [Runtime and recovery](STAGE_C_RUNTIME_RECOVERY_REPORT.md)
- [Performance](STAGE_C_PERFORMANCE_REPORT.md)
- [Defect register](STAGE_C_DEFECT_REGISTER.md)
- [Acceptance matrix](STAGE_C_ACCEPTANCE_MATRIX.md)
