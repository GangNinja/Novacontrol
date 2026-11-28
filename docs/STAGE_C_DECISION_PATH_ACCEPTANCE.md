# Stage C decision-path acceptance — NSE and BSE

**Date:** 27 November 2026
**Overall decision-path status:** PARTIAL
**Execution boundary:** paper-only; no broker connected; no live order path

## Requirement-by-requirement results

| Pipeline requirement | NSE | BSE | Evidence and boundary |
|---|---|---|---|
| Genuine provider observations with provenance | PASS | PASS | Yahoo observation records include provider symbol, observation/retrieval timestamps, exchange and Asia/Kolkata source timezone. |
| Canonical instrument and exchange isolation | PASS | PASS | XNSE:RELIANCE / RELIANCE.NS and XBOM:RELIANCE / RELIANCE.BO stayed separate. BSE monitoring detail contained XBOM only. |
| Verified trading session and data freshness | PASS | PARTIAL | Both session checks were verified for 27 November. BSE quote age was 907.765 s; the provider expected-delay field was 900 s, and 1m/5m bars were stale. |
| Supported data ingestion | PASS | PASS | One instrument succeeded per exchange; no provider failures or rejected records in the accepted runs. |
| Monitoring | PASS | PASS | One eligible instrument processed on each exchange; zero deferred/rejected. |
| Forecast generation and horizon | NOT VERIFIED | BLOCKED | No stored forecast horizon was produced in the accepted decision runs. BSE detail explicitly reported eight closes available against fourteen required. |
| Strategy evaluation | PARTIAL | PARTIAL | Each decision run completed one evaluation and returned one signal, but no proposal or eligible action. |
| NM-08 risk assessment | NOT APPLICABLE | NOT APPLICABLE | No eligible proposal was submitted for risk approval. A zero-approval count is not evidence of an NM-08 approval or rejection decision. |
| NM-10 paper execution | NOT APPLICABLE | NOT APPLICABLE | No risk-approved proposal existed; NM-10 was deliberately not run. No order was fabricated to exercise it. |
| Wallet, portfolio and setup ledger | PASS | PASS | Each isolated portfolio showed INR 100,000 capital and realized P&L 0. There were zero positions, orders, fills and execution intents. Both posted setup transactions had the expected number of ledger entries and zero net signed ledger amount. |
| Exit supervision | NOT APPLICABLE | NOT APPLICABLE | No entry or open position existed. |
| Trade reconciliation | NOT APPLICABLE | NOT APPLICABLE | No paper order or fill existed to reconcile. Runtime recovery separately reported no unresolved records. |
| Forecast outcome measurement | NOT APPLICABLE | NOT APPLICABLE | No forecast outcome had matured. |
| Advisory RL evidence | NOT APPLICABLE | NOT APPLICABLE | No eligible matured outcome existed to create advisory evidence. |
| Automatic policy promotion disabled | PASS | PASS | Safety contracts kept automatic promotion false. |

## Decision response

Each accepted decision run completed with one evaluation and one signal, zero proposals, zero approvals, zero rejections and zero errors. The run reported execution_allowed=false, paper_only=true, live_trading=false and broker_connected=false. The BSE decision detail classified the result as no action because of insufficient history. The UI likewise displayed no actionable proposal.

The response demonstrates the monitor-to-decision boundary against real provider-backed market data. It does not demonstrate the downstream NM-08-approved proposal, NM-10 paper order, portfolio position, exit, ledger trade reconciliation, matured forecast outcome or RL evidence path. Those remain unverified because no natural eligible signal occurred.

## Safety and account checks

The isolated wallets contained the setup deposit and transfer only. Both transactions were posted, each had two ledger entries and each signed ledger sum was zero. The portfolio wallet held INR 100,000; the paper portfolio showed INR 100,000 initial capital and realized P&L 0. No market order, fill, execution intent or position was created.

The normal user store was not opened. All database checks were read-only and limited to the Stage C V2 NSE and BSE directories.

## Evidence

- [NSE accepted summary](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse.json)
- [BSE accepted summary](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom.json)
- [NSE and BSE evidence directory](../tmp/stage_c_v2_2026_11_27/)
- [Decision-path matrix](STAGE_C_ACCEPTANCE_MATRIX.md)

## Conclusion

The upstream real-data, session, monitoring and bounded decision stages were exercised. The downstream paper lifecycle was not eligible and was not forced. C1 and C2 remain PARTIAL; C7 remains NOT VERIFIED. Stage C is not closed.
