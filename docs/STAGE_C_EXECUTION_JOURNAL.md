# NovaMarket Stage C execution journal — NSE + BSE

**Execution date:** 27 November 2026
**Scope:** XNSE and XBOM only; XNYS remains deferred
**Verdict:** Stage C remains open. No Stage D work was started.

## Checkpoint record

All times are India Standard Time (UTC+05:30). Acceptance used separate isolated stores:

- NSE: **tmp/stage_c_v2_2026_11_27/xnse**
- BSE: **tmp/stage_c_v2_2026_11_27/xbom**

The normal NovaMarket store and user financial records were not opened. Both stores were checked through the application's supported routes and later queried with SQLite read-only mode. Safety state throughout was paper-only, live trading false, broker disconnected, and automatic policy promotion false.

| Exchange | Instrument / provider symbol | Accepted session | Bounded run duration | Main result |
|---|---|---|---:|---|
| NSE / XNSE | XNSE:RELIANCE / RELIANCE.NS | 10:17:32–10:18:05 IST | 32.86 s | Provider, session, monitoring and decision paths ran. One evaluation and signal; zero proposals and orders. |
| BSE / XBOM | XBOM:RELIANCE / RELIANCE.BO | 10:29:04–10:31:12 IST | 127.48 s | Provider, session, monitoring and decision paths ran. One evaluation and signal; zero proposals and orders. Process-level lease recovery also passed. |

The session endpoint returned eligible=true and calendar_verified=true for 27 November 2026 on both exchanges. Market close at 15:30 IST was not observed; the runs were stopped in the morning and no collector was left running.

## Commands and operations

From the NovaMarket repository root, the bounded acceptance commands were:

    & 'C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl\.venv\Scripts\python.exe' .planning\2026-11-27-stage-c-acceptance\stage_c_acceptance_runner.py xnse
    & 'C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl\.venv\Scripts\python.exe' .planning\2026-11-27-stage-c-acceptance\stage_c_acceptance_runner.py xbom

The runner exercised health and autonomy contracts; exchange session and catalogue; quote refresh and supported ingestion; 1m, 5m, 20m and 1d bar retrieval; daily history; forecast-horizon lookup; TradingWatch radar; runtime start; monitoring cycle and detail; decision run and detail; proposal list; execution, exit, learning and reliability status; pause, resume, stop and recovery.

The user-level UI workflow was exercised in Chromium at the Autonomy page: pause, inspect persisted desired mode, resume without an explicit mode, pause again, resume from the UI, then confirm emergency stop. The runner saved screenshots and checked for page errors.

The read-only data cross-check command, run from the NovaControl root, was:

    & '.\.venv\Scripts\python.exe' 'novamarket/.planning/2026-11-27-stage-c-acceptance/stage_c_readonly_crosscheck.py'

It opened only the two isolated SQLite files with mode=ro and PRAGMA query_only=ON. It compared stored 20m bars against any available four 5m components, checked seed ledger balancing, counted lifecycle records, and wrote a JSON summary beside each exchange's evidence.

After shutdown, Get-NetTCPConnection returned no listeners on ports 8001, 5174 or 8002. The final autonomy states were disabled, auto_start=false, and lease absent. A controlled application restart and recovery returned to disabled without automatic resume or unresolved records.

## Pipeline results

| Requirement | NSE | BSE |
|---|---|---|
| Genuine provider mapping and exchange identity | PASS — Yahoo provenance named RELIANCE.NS and XNSE:RELIANCE. | PASS — Yahoo provenance named RELIANCE.BO and XBOM:RELIANCE. |
| Verified market session and monitoring | PASS — one discovered, eligible and processed instrument; zero rejected or deferred. | PASS — one XBOM candidate only; no XNSE candidate appeared; zero rejected or deferred. |
| Forecast and strategy outcome | PARTIAL — decision run completed, but no stored forecast horizon or eligible proposal resulted. | PARTIAL — decision returned no action; eight daily closes were available while the strategy reported that it needed fourteen. |
| NM-08 and NM-10 | NOT APPLICABLE to these runs — no eligible proposal existed; NM-10 was deliberately not run. | NOT APPLICABLE to these runs — no eligible proposal existed; NM-10 was deliberately not run. |
| Wallet, portfolio and seed ledger | PASS — INR 100,000 portfolio capital, zero realized P&L, zero positions and orders; both posted setup transactions balanced. | PASS — same isolated paper-only financial state. |
| Exit lifecycle and forecast outcome measurement | NOT APPLICABLE — no position or matured forecast outcome existed. | NOT APPLICABLE — no position or matured forecast outcome existed. |
| Automatic policy promotion | PASS — disabled in the safety contract. | PASS — disabled in the safety contract. |

The decision response on each exchange recorded one evaluation, one signal, zero proposals, zero approvals, zero rejections and zero errors. The run remained paper-only, execution_allowed=false, live_trading=false and broker_connected=false.

## Harness corrections and preserved failed attempts

No NovaMarket production source was changed. The first NSE attempt is preserved in the evidence directory and failed at the acceptance harness boundary: it sent a decision trigger that the API rejects (only manual or scheduled are accepted) and resumed autonomous mode without acknowledge_paper_only=true. Those steps returned HTTP 422; the acceptance was stopped and the isolated runtime was subsequently stopped through the supported API before the successful rerun.

The harness was corrected to use trigger=manual and to include acknowledge_paper_only=true while still omitting the resume mode. An early BSE harness boot also used a FastAPI lifecycle method unavailable on the installed FastAPI version; it was changed to the app router's startup hooks before the accepted BSE run. These were harness corrections, not application fixes. Failed evidence was retained.

## Evidence index

- NSE accepted journal: [JSONL](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse.jsonl), [summary](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse.json), [autonomy UI screenshot](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse_autonomy.png).
- BSE accepted journal: [JSONL](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom.jsonl), [summary](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom.json), [autonomy UI screenshot](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom_autonomy.png).
- Read-only reconciliation: [NSE](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_readonly_crosscheck.json), [BSE](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_readonly_crosscheck.json).
- Preserved failed NSE attempt: [JSONL](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044052250394Z_xnse.jsonl), [summary](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044052250394Z_xnse.json).
- Preserved early BSE harness boot failure: [summary](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045817483724Z_xbom.json).

No full unit suite, static-analysis suite, type-check suite or CI suite was run in this live acceptance session. No production code changed. No commit, push, deploy, reset, clean or stash was performed.

## Remaining checkpoint

Stage C is open pending the gaps in the acceptance matrix and closure report. The fallback checkpoint is 30 November 2026. Continue with Indian exchanges only, preserve all evidence, and do not start Stage D until every applicable criterion is PASS.
