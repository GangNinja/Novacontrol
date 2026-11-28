# Stage C Remediation Matrix

Companion to `docs/STAGE_C_MASTER_REMEDIATION_REPORT.md`. The original diagnosis
(`docs/STAGE_C_DEFECT_DIAGNOSIS.md`) and the acceptance documents are unchanged.

| ID | Criterion | Confirmed root cause | Evidence | Affected files | Remediation | Tests required | Priority | Status |
|---|---|---|---|---|---|---|---|---|
| R-01 | C1, C2 (PI-12 context) | `_forecast_bars` read `limit=60, ascending=True`, no interval → oldest 60 rows across all intervals | NSE `last_close 1441.300048828125` = 2026-02-18 close; BSE `1337.300048828125` = 2026-11-11 20m close | `decision/pipeline.py` | Interval per horizon (5m short / 1d otherwise), newest-first, chronological, session/timestamp dedupe, ≥21 bars or fail closed | Scoping, ordering, newest close, duplicates, partial exclusion, insufficient history, unscoped adapter | P0 | FIXED AND VERIFIED |
| R-02 | C5 | Control calls only UPDATE the singleton runtime row; no actor, sequence or refused-attempt record | `autonomy/store.py:544` `save_control`; `audit.jsonl` control counts 0/0/0 | `autonomy/store.py`, `autonomy/runtime.py`, `service.py`, `api/app.py` | Schema v5 append-only `autonomy_control_events`; event in the same transaction as the transition; refusals/no-ops recorded without state change; operator principal as actor; shutdown split from stop | Applied chain, refusals, no-op, restart persistence, ordering, atomic rollback, v4→v5 migration, actor | P0 | FIXED AND VERIFIED |
| R-03 | C4, C3 | Unique key includes provider; daily readers did not dedupe by session | Both providers can hold one session; timestamps differ (03:45Z vs 10:00Z) | `marketdata/dedupe.py` (new), `bse_bhavcopy.py`, `strategies/engine.py`, `decision/pipeline.py` | Exchange-local session policy; official bhavcopy preferred; importer skips/append-with-conflict, never overwrites; readers dedupe | Matching session, conflicting values, new session, retry, official preference | P0 | FIXED AND VERIFIED |
| R-04 | C4 | Yahoo `.BO` supplied 8 sessions and omitted 26 Nov | 8 rows, one provider response, 24 Nov holiday | `marketdata/bse_bhavcopy.py` (existing path) | Official BSE import 2026-07-01…11-27 into isolated XBOM; 101 distinct sessions, 26 Nov present, backup taken | Independent read-only count, provenance, 26 Nov presence, integrity | P0 | FIXED (data) — C4 PASS |
| R-05 | C3 | 250 is a bar count per interval, so 5m covered ~3.3 sessions while 20m covers ~13.3 | Backfill XNSE 846 inserted / XBOM 835 inserted via `ingest_history`; **1000/1000 independent 5m components and 250/250 independent recomputes on both exchanges** | `scripts/stage_c_20m_verify.py`, `tmp/r05_backfill_5m.py` | Epoch→ISO normalisation, independent 5m persistence, independent recompute path, consumer contract | Verifier run on both stores; independent-recompute tests; R-01 interval tests | P1 | **PASS** |
| R-06 | C3 | 20m anchor = page minimum, so a 09:20 page shifts the grid | Page 09:20 → shifted 09:20 bucket; with session open → correct 09:35 bucket | `marketdata/providers/yahoo.py` | Anchor on `calendar_by_mic(...).sessions[0].open`; disclose `session_anchor`; page fallback only without a calendar | 09:15, 09:20, missing middle, incomplete tail, multi-session, holiday, metadata | P2 | FIXED AND VERIFIED |
| R-07 | C1, C2 (latency) | One synchronous SQLite commit per bar on the event loop | 500 bars: 696.07 ms peak lag / 500 commits vs 31.42 ms / 1 commit | `marketdata/store.py`, `marketdata/engine.py` | `append_bars` one transaction per provider page; `append_bar` delegates; page-level rollback | Insert/idempotency, conflict, rollback, empty page, benchmark | P2 | FIXED AND MEASURED |
| R-08 | C1, C2 (forecasts) | No registered models; NM-06 horizons (`one_week`) never match decision labels (`5d`) | NSE trained from 248 bars: `bootstrap` active + 5 candidates, `fc_42d151e421dd9f79a566` stored, `models("5d")` returns all 6, bridge `["one_week"]`; BSE 108 < 140 | `decision/forecast.py` | Single canonical map (`HORIZON_CANONICAL`/`canonical_horizon`), unfiltered-then-canonical model lookup, risk bridge canonical both ways, intraday still excluded | Horizon-compatibility, label-form bridge, NSE train/select/generate, BSE block check | P1 | **SC-R08-01 FIXED**; NSE TRAINED; BSE BLOCKED (108 < 140) |
| R-09 | C1 | Acceptance ambiguity: legitimate HOLD vs required actionable trade | NSE HOLD is non-actionable by design (`pipeline.py:1753`, `:1838`) | none | Two interpretations documented; no acceptance document edited | Operator decision | P1 | ACCEPTANCE DECISION REQUIRED |
| R-10 | C2, C7 | Full-session procedure needs corrected paths and evidence | This report §11 | none | Checklist prepared; no session started | Authorized after-close run | P1 | PREPARED — NOT RUN |

## Criterion status after remediation

| Criterion | Before | After | Reason |
|---|---|---|---|
| C1 — NSE decision path | PARTIAL | **PARTIAL** | Context defect fixed, forecast model trained and consumed; interpretation A/B still open |
| C2 — BSE decision path | PARTIAL | **PARTIAL** | History sufficient for decisions (108 rows / 101 sessions); BSE forecast model blocked at 108 < 140 |
| C3 — Timeframes/intervals | PARTIAL | **PASS** | 20m independent 5m coverage 1000/1000 and 250/250 independent recomputes on both exchanges; anchor, ordering and interval scoping fixed |
| C4 — Indian daily bars | PARTIAL | **PASS** | 101 genuine BSE sessions incl. 26 Nov, official provenance, integrity ok |
| C5 — Pause/resume and audit | PARTIAL | **PASS** | Durable append-only control audit, actor, sequencing, restart persistence, atomic transitions |
| C6 — Restart and lease recovery | PASS | **PASS** | AT-01 suite re-run green |
| C7 — Natural lifecycle | NOT VERIFIED | **NOT VERIFIED** | No authorized full-session run |
| Stage C | OPEN | **OPEN** | C1, C2, C7 unresolved; C3–C6 PASS |
| Stage D | NOT STARTED | **NOT STARTED** | Gate not met |
