# Stage C defect and open-gap register

**Review date:** 27 November 2026
**Scope:** Fresh NSE + BSE real-market acceptance
**Production source fixes in this pass:** None

This register distinguishes confirmed acceptance gaps from confirmed product defects. No market data was fabricated and no signal or trade was forced.

| ID | Finding | Classification | Status | Evidence / next action |
|---|---|---|---|---|
| SC-01 | No pause, resume or emergency-stop control event was found in either isolated audit JSONL, although persisted runtime state and UI transitions were verified. | C5 auditability gap; root cause not established | OPEN — C5 PARTIAL | Cross-check reports zero control phrase records in both isolated audit files. Determine the supported audit source or add auditable control events, then rerun C5. |
| SC-02 | XBOM daily history ends on 25 November; the completed 26 November session is absent. Eight closes are available while the BSE strategy response says fourteen are needed. | Provider/data completeness limitation | OPEN — C2/C4 PARTIAL | Recheck a fresh supported XBOM history request on the fallback date. Do not substitute XNSE data. |
| SC-03 | BSE 1m and 5m latest records were classified stale at 10:29 IST; 20m was valid but delayed. | Provider freshness limitation | OPEN — C2/C3 PARTIAL | Record source age and freshness on the next Indian-market checkpoint. |
| SC-04 | Only 60 of 250 stored 20m bars per exchange had all four referenced 5m components available for exact comparison. Those 60 matched all OHLCV fields; 190 per exchange lacked source components in the stored dataset. | Data validation coverage gap | OPEN — C3 PARTIAL | Preserve the current evidence; verify more complete provider component history or retain source pages that cover each aggregate. |
| SC-05 | The 27 November market close and final completed daily bars were not observed. | Unverified session-close requirement | OPEN — C1/C3/C4 PARTIAL | Capture final observations and completed-session handling at a later authorized checkpoint; no overnight collector was left running. |
| SC-06 | No naturally eligible proposal occurred, so NM-08 approval, NM-10 order, position, exit, trade reconciliation, matured forecast outcome and advisory learning were not exercised. | Natural-signal acceptance gap | OPEN — C1/C2 PARTIAL; C7 NOT VERIFIED | Observe only; do not change strategy thresholds or manufacture eligibility. |
| SC-07 | Event-loop maximum lag reached 485.799 ms in one bounded BSE sample. The measurement did not correlate lag with radar and is not comparable to the older 270 ms integration run. | Performance follow-up; regression not established | OPEN — performance PARTIAL | Repeat under request-correlated radar/ingestion observation and a justified bounded load profile. |
| SC-08 | The first NSE harness run used an invalid decision trigger and omitted explicit paper-only acknowledgement on autonomous resume. An early BSE harness boot called an unavailable FastAPI lifecycle method. | Acceptance harness defects | FIXED IN HARNESS | Updated the trigger to manual, supplied acknowledge_paper_only=true while omitting mode, and used app-router startup hooks. Failed artifacts were preserved; no product source was changed. |

No confirmed NovaMarket production code defect was fixed during this acceptance. The open findings are reported without promoting incomplete evidence to PASS. The user database, normal application store and financial records were not touched.

## Related evidence

- [Execution journal](STAGE_C_EXECUTION_JOURNAL.md)
- [Acceptance matrix](STAGE_C_ACCEPTANCE_MATRIX.md)
- [NSE evidence](STAGE_C_NSE_REAL_MARKET_REPORT.md)
- [BSE evidence](STAGE_C_BSE_REAL_MARKET_REPORT.md)
- [Performance measurements](STAGE_C_PERFORMANCE_REPORT.md)
