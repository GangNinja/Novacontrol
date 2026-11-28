# Stage C final acceptance matrix — NSE + BSE

**Acceptance date:** 27 November 2026
**Scope:** NSE (XNSE) and BSE (XBOM); NYSE deferred
**Overall:** OPEN — Stage C is not closed

Statuses use the Stage C specification: PASS, PARTIAL, FAIL, BLOCKED, NOT VERIFIED or NOT APPLICABLE.

## C1 — Genuine NSE decision pipeline

| Requirement | Status | Evidence |
|---|---|---|
| Genuine provider observation and provenance | PASS | Yahoo RELIANCE.NS observation with source and retrieval timestamps. |
| Correct XNSE identity and mapping | PASS | XNSE:RELIANCE mapped to RELIANCE.NS. |
| Exchange/universe isolation | PASS | Monitoring processed the XNSE instrument. |
| Freshness and session validation | PASS | Fresh quote response; verified 27 November NSE session. |
| Monitoring cycle | PASS | One eligible instrument processed; no rejected/deferred instruments. |
| Forecast generation | NOT VERIFIED | No stored horizon forecast resulted in the accepted decision run. |
| Strategy evaluation | PARTIAL | One evaluation and one signal, zero actionable proposal. |
| NM-08 risk assessment | NOT APPLICABLE | No eligible proposal was submitted for assessment. |
| NM-10 paper execution | NOT APPLICABLE | No approved proposal existed; execution was not called. |
| Portfolio, wallet and ledger consistency | PASS | INR 100,000 initial capital, P&L 0, zero positions/orders; two posted setup transactions balanced. |
| Exit supervision for an entry | NOT APPLICABLE | No entry or position. |
| Trade reconciliation | NOT APPLICABLE | No paper trade existed; runtime recovery had zero unresolved records. |
| Forecast outcome and advisory RL evidence | NOT APPLICABLE | No forecast outcome matured. |
| Automatic policy promotion disabled | PASS | Safety contract reported it false. |
| C1 overall | PARTIAL | Downstream forecast/risk/execution/lifecycle path did not naturally occur. |

## C2 — Genuine BSE decision pipeline

| Requirement | Status | Evidence |
|---|---|---|
| Genuine BSE provider observation | PASS | Yahoo observation used RELIANCE.BO with BSE metadata. |
| Correct XBOM identity and mapping | PASS | XBOM:RELIANCE mapped to RELIANCE.BO. |
| Exchange isolation | PASS | XBOM monitoring detail contained one XBOM candidate and no XNSE candidate. |
| Timestamp and freshness | PARTIAL | Quote response said fresh at age 907.765 s; 1m/5m latest bars were stale and 20m delayed. |
| Monitoring | PASS | One XBOM instrument processed; zero deferred/rejected. |
| Forecast and strategy evaluation | PARTIAL | One evaluation/signal, no proposal; response said eight closes were available and fourteen were needed. |
| NM-08 and NM-10 | NOT APPLICABLE | No naturally eligible proposal. |
| Portfolio and wallet consistency | PASS | Portfolio capital INR 100,000, wallet balance INR 100,000 and realized P&L 0. |
| Seed ledger consistency | PASS | Both posted setup transactions balanced against their ledger entries. |
| Exit supervision | NOT APPLICABLE | No entry or position existed. |
| Trade reconciliation | NOT APPLICABLE | No paper order or fill existed to reconcile. |
| Forecast outcome measurement | NOT APPLICABLE | No forecast outcome matured. |
| Advisory RL evidence | NOT APPLICABLE | No eligible matured outcome existed. |
| Automatic policy promotion disabled | PASS | Safety contract reported it false. |
| C2 overall | PARTIAL | Forecast history and the trade-dependent lifecycle remain unverified. |

## C3 — Genuine 20-minute data and other configured intervals

| Requirement | Status | Evidence |
|---|---|---|
| Actual provider-backed 1m, 5m, 20m and 1d records | PASS | Each interval was retrieved and retained with provider provenance on both exchanges. |
| Four 5m components and exact OHLCV | PARTIAL | 60/250 aggregates per exchange were independently comparable and matched; 190 lacked stored 5m components. |
| Interval boundaries, timezone and storage/retrieval | PARTIAL | UTC timestamps and Asia/Kolkata source timezone were recorded; full group proof is limited by missing components. |
| Incomplete groups excluded and no fabricated gap filling | PARTIAL | No partial flag was observed and the verifier did not fill gaps; missing source bars prevent a complete assertion about every stored aggregate. |
| Freshness validation | PARTIAL | BSE 1m/5m were stale; NSE newest intervals were classified delayed. |
| Scheduler and gateway support | PARTIAL | Supported ingestion and bar retrieval routes completed; a scheduled 20m ingestion cadence was not observed. |
| Forecast and strategy consumption | NOT VERIFIED | No stored forecast horizon or eligible decision consumed the validated 20m groups. |
| Session-close behavior | NOT VERIFIED | Market close was not observed. |
| C3 overall | PARTIAL | See timeframe report. |

## C4 — Indian daily bars

| Requirement | Status | Evidence |
|---|---|---|
| NSE completed-session history and identity | PASS | 248 daily rows; latest completed bar was 26 November, XNSE identity, delayed provider provenance. |
| BSE completed-session history and identity | PARTIAL | Eight daily rows; latest was 25 November; 26 November completed session absent. |
| Forecast only with valid required history | PARTIAL | BSE correctly returned insufficient-history detail at 8/14 closes; no valid forecast was produced. |
| C4 overall | PARTIAL | BSE latest completed daily bar and final 27 November close remain unverified. |

## C5 — Pause and resume

| Requirement | Status | Evidence |
|---|---|---|
| Paper-only runtime health and lease | PASS | UI and API showed running mode and held lease during the checkpoint. |
| Pause persists desired mode | PASS | State changed to paused with autonomous intent retained. |
| Resume without explicit mode and acknowledgement | PASS | Resume restored intended mode with acknowledge_paper_only=true. |
| Risk gates and UI status | PASS | UI reflected status and NM-08/NM-10 boundaries. |
| Audit records | PARTIAL | No operator pause/resume/emergency-stop records found in isolated audit JSONL. |
| No duplicate runtime or execution | PASS | Single lease/cycle; zero orders/fills. |
| C5 overall | PARTIAL | Required control-specific audit evidence is missing. |

## C6 — Restart and lease recovery

| Requirement | Status | Evidence |
|---|---|---|
| Valid lease and owner/expiry | PASS | Captured in the isolated 60-second BSE process probe. |
| Restart while lease remains valid | PASS | New process did not steal the lease and used bounded retry. |
| Recovery after natural expiry | PASS | Retry restored autonomous mode after lease expiry. |
| No duplicate execution | PASS | Zero intents, orders or fills in the probe. |
| Emergency stop prevents auto-start and retry | PASS | Stop cleared retry and third process remained disabled. |
| C6 overall | PASS | All recorded lease probe steps passed. |

## C7 — Naturally eligible signal and full paper lifecycle

| Requirement | Status | Evidence |
|---|---|---|
| Natural eligible entry proposal | NOT VERIFIED | Both decision runs produced zero proposals; no signal was forced. |
| NM-08 approval and NM-10 paper entry | NOT VERIFIED | No eligible proposal existed. |
| Portfolio/ledger, exit, NM-08 exit, NM-10 exit and reconciliation | NOT VERIFIED | No entry or position existed. |
| Forecast outcome and advisory RL evidence | NOT VERIFIED | No matured forecast outcome existed. |
| No duplicate/replayed trade | NOT APPLICABLE | No trade was created. |
| Automatic policy promotion disabled | PASS | Safety contract kept promotion false. |
| C7 overall | NOT VERIFIED | The complete natural lifecycle was not observed. |

## Cross-cutting performance

**PARTIAL.** The UI and sampled API remained responsive; BSE event-loop lag p95 was 13.694 ms, maximum 485.799 ms. No multi-second outage was observed, but the sample was short and did not correlate lag to the radar route or measure concurrency/soak.

## Gate

Stage C does not meet its closure rule. C1–C5 remain PARTIAL, C7 remains NOT VERIFIED, and C2–C4 have unresolved provider/history limits. Preserve this state and do not advance to Stage D.

Evidence index: [execution journal](STAGE_C_EXECUTION_JOURNAL.md), [NSE report](STAGE_C_NSE_REAL_MARKET_REPORT.md), [BSE report](STAGE_C_BSE_REAL_MARKET_REPORT.md), [decision path](STAGE_C_DECISION_PATH_ACCEPTANCE.md), [timeframe validation](STAGE_C_TIMEFRAME_DATA_VALIDATION.md), [runtime/recovery](STAGE_C_RUNTIME_RECOVERY_REPORT.md), [performance](STAGE_C_PERFORMANCE_REPORT.md), [defect register](STAGE_C_DEFECT_REGISTER.md).
