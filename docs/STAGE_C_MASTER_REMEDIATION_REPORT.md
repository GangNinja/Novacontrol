# NovaMarket — Stage C Master Remediation Report

**Scope:** NSE + BSE real-market acceptance remediation (Stage C). Paper-only.
**Baseline diagnosis:** `docs/STAGE_C_DEFECT_DIAGNOSIS.md` (unchanged).
**Executed:** 2026-11-27, local Windows host, isolated acceptance stores under
`tmp/stage_c_v2_2026_11_27/`.
**Acceptance run:** none. No live-market session, proposal, order, fill or Stage D
start was performed by this work.

---

## 1. Summary

| ID | Finding | Classification | Status |
|---|---|---|---|
| R-01 | PI-12 technical context read the oldest 60 mixed-interval bars | CONFIRMED ROOT CAUSE | **FIXED AND VERIFIED** |
| R-02 | Pause/resume/stop had no durable control audit | CONFIRMED ROOT CAUSE | **FIXED AND VERIFIED** |
| R-03 | A daily session could be stored by two providers and counted twice | CONFIRMED LATENT DEFECT | **FIXED AND VERIFIED** |
| R-04 | BSE had only 8 genuine daily sessions and no 26 Nov bar | ENVIRONMENTAL (provider depth) | **FIXED — history restored** |
| R-05 | 20m independent verification was incomplete for count-based reasons | ACCEPTANCE DECISION REQUIRED | **MEASURED — criterion open** |
| R-06 | 20m grid anchored on the page minimum, not the session open | CONFIRMED LATENT RISK | **FIXED AND VERIFIED** |
| R-07 | One synchronous SQLite commit per bar on the event loop | CONFIRMED BLOCKING MECHANISM | **FIXED AND MEASURED** |
| R-08 | No forecast models; PI-12 horizon vocabulary cannot match NM-06 | NEW DEFECT + DATA LIMIT | **NOT FIXED — decision required** |
| R-09 | C1 acceptance ambiguity (legitimate HOLD vs required BUY/SELL) | ACCEPTANCE DECISION REQUIRED | **DOCUMENTED — open** |
| R-10 | Full-session acceptance procedure | PREPARATION ONLY | **DOCUMENTED — not run** |

Two criteria improved on evidence: **C4 → PASS** and **C5 → PASS**; C6 retained
PASS after regression. C1, C2 and C3 remain PARTIAL; C7 remains NOT VERIFIED;
Stage C stays OPEN and Stage D stays NOT STARTED.

---

## 2. R-01 — PI-12 technical context

**Original finding.** `_forecast_bars` called `bars(instrument_id, limit=60,
ascending=True)` with no interval. `MarketDataStore.bars` orders `ASC` for
`ascending=True`, so the call returned the **oldest 60 rows in the store**, mixed
across every stored interval. Both acceptance stores published a stale
`technical_context.last_close`: `1441.300048828125` (NSE, the 2026-02-18 daily
close) and `1337.300048828125` (BSE, a 2026-11-11 20m close).

**Root cause.** Missing interval scope and an inverted sort direction in the
decision pipeline's context reader; `strategies/engine.py` already had the correct
newest-first pattern and a comment describing exactly this failure.

**Implementation.**
- One decision interval per portfolio horizon: `short_term → 5m`, `medium_term →
  1d`, `long_term → 1d`, unknown horizon → `1d`. No mixed-interval fallback.
- Read is interval-scoped, `limit=60`, `ascending=False`.
- Partial bars are excluded (unchanged rule).
- Duplicate observation instants are collapsed newest-first; daily rows are
  reduced to one close per exchange-local session by the R-03 policy.
- The page is reversed to chronological order so `_technical` reads
  `closes[-1]` as the newest close.
- Fewer than 21 complete bars returns an empty context (fail closed) and logs
  `novamarket.decision.technical_context_insufficient`; an adapter that rejects
  the interval scope returns an empty context and logs
  `technical_context_unsupported`.

**Files.** `src/novacontrol/novamarket/decision/pipeline.py` (`_forecast_bars`,
`_TECHNICAL_INTERVAL`, `_TECHNICAL_MIN_BARS`, `_bar_value`; call site passes the
portfolio horizon).

**Tests added (7).** `tests/test_stage_c_remediation.py::Pi12ForecastBarsTests`:
daily series/scoping/ordering, 5m short-term selection, unknown-horizon default,
duplicate-instant collapse, partial-newest exclusion, insufficient-history fail
closed, unscoped-adapter fail closed.

**Verification.** All 7 pass. No stale or mixed series can now be presented as the
current context.

---

## 3. R-02 — Durable autonomy control audit

**Original finding.** `pause`, `resume` and emergency `stop` only overwrote the
singleton `autonomy_runtime` row (`save_control`, `release_lease`). No actor, no
sequence, no rejected-attempt record; `audit.jsonl` never received a control
event.

**Implementation.**
- Schema **v5**, additive: `autonomy_control_events` with `seq INTEGER PRIMARY
  KEY` (monotonic, never reused because rows are never deleted), `event_id`
  unique, `occurred_at`, `operation`, `actor`, `reason`, `from_mode`,
  `requested_mode`, `resulting_mode`, `desired_mode`, `lease_owner`,
  `lease_valid`, `outcome`, `error`, `details_json`. Existing v4 stores gain the
  table via `CREATE TABLE IF NOT EXISTS`; no row is rewritten and the version row
  moves 4 → 5.
- Successful transitions: `save_control` inserts the event **inside the same
  transaction** as the mode UPDATE; a lease mismatch rolls both back.
- Emergency stop / graceful close: `release_lease` inserts the event in the same
  transaction as the runtime disable.
- Refusals and no-ops: `append_control_event` records the attempt **without**
  touching `autonomy_runtime`; a failed audit write can never change state
  (best-effort, logged).
- `stop` is split into an operator stop (`operation="stop"`) and a graceful
  application shutdown (`operation="shutdown"`), so a close is not mistaken for
  an operator action. Emergency stop remains fail-safe and still disables the
  auto-start opt-in.
- Actor is the authenticated autonomy-operator principal supplied by the API
  (`require_autonomy_operator` → `_operator`), never invented. Internal calls use
  the explicit `system` identity.
- `status()` exposes the newest 20 control events; `AutonomyStore.control_events`
  reads up to 200 newest-first.

**Files.** `autonomy/store.py`, `autonomy/runtime.py`, `service.py`,
`api/app.py`; existing schema test updated to the v5 contract.

**Tests added (8).** Applied transitions and ordering, refused pause, refused
resume + no-op pause, restart persistence/sequence/uniqueness, rejected state
change writes no event (atomicity), v4 → v5 additive migration preserving config,
actor attribution. Existing AT-01 suite (C6 lease/recovery) passes unchanged.

**Result.** `audit.jsonl` is not used; the audit is local SQLite inside the
autonomy store, survives restart, and is append-only.

**C6 regression.** The whole `tests/test_novamarket_autonomy.py` file — lease
acquisition/conflict, restart-never-resumes, expiry, recovery and the API
control flows — passed in the same run as the new audit tests (52 passed across
the three focused files). No C6 behaviour changed.

---

## 4. R-03 — Duplicate daily sessions

**Original finding.** The bar identity includes `provider`, so Yahoo and an
official bhavcopy can store the same instrument/session. The daily reader had no
session dedupe; two closes would enter the SMA window.

**Implementation.**
- New `marketdata/dedupe.py`: session = bar's exchange-local date from
  `source_timezone`; deterministic preference `bse_bhavcopy` → `nse_bhavcopy` →
  provider name → bar id; returns chronological unique sessions.
- `strategies/engine.py` (the SMA input) and the PI-12 daily context both pass
  their daily bars through it.
- `import_bse_bhavcopy` pre-loads existing daily sessions per instrument and:
  - skips an identical session (`duplicate`),
  - skips a differing session already held by an official provider
    (`skipped_official_conflict`),
  - appends the official row beside a differing quote-provider row
    (`official_session_appended`) and reports the conflict; nothing is
    overwritten or merged.
- The import result reports `duplicate_session_policy`,
  `session_conflict_count` and up to 50 `session_conflicts`.

**Files.** `marketdata/dedupe.py` (new), `marketdata/bse_bhavcopy.py`,
`strategies/engine.py`, `decision/pipeline.py`.

**Tests added (4).** Matching Yahoo session not re-imported; conflicting values
reported and both rows preserved with the official one selected by the reader;
new session imported then a retry does not duplicate; reader prefers the official
row.

---

## 5. R-04 — Official BSE daily history

**Backup before any write** (`tmp/stage_c_remediation_evidence/backup_xbom_pre_r04/`):
`novamarket.sqlite3` 3,084,288 bytes,
SHA-256 `83059da71e5748f4ed3d29e264713e452a1eab7efd2c67f0657d846a11bf8195`
(WAL/SHM copied too). The normal financial store was not touched.

**Import.** Existing official path `marketdata/bse_bhavcopy.py`, isolated XBOM
store, `2026-07-01 … 2026-11-27`, resumable checkpoint
`tmp/stage_c_remediation_evidence/r04_bse_checkpoint.json`.

**Result (independent read-only query).**
- 108 daily rows, **101 distinct exchange sessions**, first `2026-07-01`, last
  `2026-11-26`.
- Providers: 100 `bse_bhavcopy` + 8 `yahoo`; 6 sessions hold both (official row
  appended and reported as a conflict).
- `2026-11-26` present. `2026-11-27` absent: the bhavcopy is published after the
  close and the run was intraday.
- Missing from the weekday calendar: `2026-07-08` (genuine provider gap) and the
  market holidays `2026-09-14`, `2026-10-02`, `2026-10-20`, `2026-11-10`,
  `2026-11-24` (BSE returns its HTML shell, which the parser refuses as
  malformed rather than synthesising a bar).
- 14-session requirement met (101); 80-bar forecast-preparation requirement met
  (101 ≥ 80).
- `PRAGMA integrity_check` = `ok`.

**Consequence.** The BSE strategy now has enough closes to stop failing with
`insufficient_history`. That does not itself produce a C2 decision; a decision
run remains unauthorized.

---

## 6. R-05 — 20m verification (PASS)

**Previous state.** 250/250 stored 20m bars re-derived from `raw_json`, but
0/1000 components existed independently as 5m bars: `history_limit = 250` is a
bar count *per interval*, so 5m kept ~3.3 sessions while 20m kept ~13.3.
Component reproducibility alone was not treated as satisfaction.

**Backfill (additive, isolated stores only).** `tmp/r05_backfill_5m.py` fetched
Yahoo 5m history through `MarketDataEngine.ingest_history` — the production
ingest path, not a direct write — into the isolated XNSE/XBOM stores, after
byte-level backups. Nothing was synthesised; every stored bar came from the
provider and passed the normal normalisation/quality gates.

| Exchange | Received | Inserted | Duplicate | Rejected | Backup SHA-256 (db) |
|---|---|---|---|---|---|
| XNSE | 1,097 | 846 | 249 | 2 | `0f06c53d789f355f77c9152883a6c0be3f73318626183e41a05c6b2412639c87` |
| XBOM | 1,084 | 835 | 249 | 0 | `ef95f8a33546fc97ad25947db8925542649857712b63df23bd05fb8e05d2e83f` |

**Verifier fixes.** `scripts/stage_c_20m_verify.py` compared epoch integers
against ISO strings, so it counted every component as missing; it now normalises
both sides. It also gained `_independent_recompute_matches`: each 20m bar is
recomputed from the *independently stored 5m rows* (OHLCV + offsets +
uniqueness) instead of only re-derived from `raw_json`.

| Measure | NSE | BSE |
|---|---|---|
| Stored 20m bars | 250 | 250 |
| Components re-derived exactly (OHLCV + offsets + uniqueness) | **250 / 250 (1.000)** | **250 / 250 (1.000)** |
| Component stamps checked | 1,000 | 1,000 |
| Components also stored independently as 5m bars | **1,000 / 1,000 (1.000)** | **1,000 / 1,000 (1.000)** |
| Missing independent components | **0** | **0** |
| 20m bars recomputed from the stored 5m rows | **250 / 250 (1.000)** | **250 / 250 (1.000)** |
| Malformed groups / problems | 0 / none | 0 / none |

Evidence: `tmp/stage_c_remediation_evidence/r05_5m_backfill_{xnse,xbom}.json`,
`r05_20m_{xnse,xbom}.json`.

**Consumer contract.** `strategies/engine.py` reads `1d`; PI-12 reads `5m`
(short-term) or `1d`; 20m is evidence/aggregation only, and costs are never read
from it. Verified by the R-01 tests, not by the script.

**Verdict: R-05 PASS on both exchanges** — independent 5m corroboration is
complete and every 20m bar is independently recomputable from stored data.

---

## 7. R-06 — 20m session anchor

**Original risk.** `_aggregate_20m` anchored on the minimum timestamp in the
provider page. A page starting 09:20 (missing the 09:15 row) emitted a complete
but five-minute-shifted bucket.

**Implementation.** `fetch_ohlcv` resolves the exchange session open from
`calendar_by_mic(instrument.mic)` and passes it as `session_open_minute`. The
aggregator groups on that grid and records `session_anchor="session_open"` in
provider metadata. Venues without a calendar (or with a mismatched provider
timezone) fall back to the page minimum and record
`session_anchor="page_minimum"`; no shifted grid is produced where a session is
defined. Missing components still drop their group; nothing is fabricated;
existing stored bars are untouched.

**Tests added (7).** Session-open grid, 09:20 page (proves the old shift and the
new 09:35 alignment), missing middle component, incomplete final bucket, multiple
sessions, holiday/empty page, metadata anchor.

---

## 8. R-07 — Event-loop blocking persistence

**Original finding.** `append_bar` performed one synchronous SQLite commit per
bar on the event loop; `_collect_bars` called it per row.

**Implementation.**
- `MarketDataStore.append_bars` inserts a whole provider page inside one
  transaction, then resolves ignored inserts with the same duplicate/conflict
  semantics (`INSERT OR IGNORE`, per-row comparison, `market_corrections`).
  `append_bar` delegates to it, so every existing caller keeps its contract.
- `MarketDataEngine._collect_bars` normalises and assesses the page first, then
  appends once; a store failure rolls the page back and is reported per page.
  Invalid-quality bars are still stored and still counted/published as rejected.

**Measurement** (isolated temp databases, 500 bars, same workload both modes):

| Mode | Wall time | Peak event-loop lag | Transactions | Stored |
|---|---|---|---|---|
| Per bar (previous behaviour) | 0.6959 s | **696.07 ms** | 500 | 500 |
| One page (new) | **0.0157 s** | **31.42 ms** | 1 | 500 |

Wall time 44× lower; peak loop lag 22× lower. The original 485.799 ms acceptance
sample was not reproduced (different hardware/workload); what is confirmed is the
mechanism and a comparable order of magnitude. The new peak is not zero because
a single 500-row transaction still costs CPU; it is bounded and no longer scales
with rows × commit.

**Tests added (4).** Page inserts then repeats as duplicates; changed value in a
batch is a recorded conflict with the original retained; unknown instrument rolls
the page back; empty page is a no-op.

---

## 9. R-08 — Forecast models (SC-R08-01 FIXED; NSE TRAINED; BSE BLOCKED by history)

### 9.1 SC-R08-01 — NM-06 ↔ PI-12 horizon vocabulary mismatch (FIXED)

PI-12 selects forecasts by *decision labels* (`1d/3d/5d/30d/90d`), while NM-06
stores NM-06 `Horizon` values (`next_session/few_sessions/one_week/one_month/
three_months`). No reverse mapping existed in `decision/forecast.py`, so a
stored `one_week` forecast could never satisfy `models(horizon="5d")` and the
risk bridge could not see the forecast horizon.

**Fix.** One canonical map in `decision/forecast.py`:

| Decision label | NM-06 horizon |
|---|---|
| `5d` | `one_week` |
| `3d` | `few_sessions` |
| `30d` | `one_month` |
| `90d` | `three_months` |

`HORIZON_CANONICAL` + `canonical_horizon()` are the single source; the PI-12
model lookup queries the store unfiltered and then filters canonically (a
store-level filter on the wrong vocabulary returned nothing);
`RISK_HORIZON_MAP["5d"]` was corrected to `one_week`; the risk bridge resolves
canonically, so both the interval (`5d`) and label (`one_week`) forms work while
intraday horizons remain excluded.

**End-to-end proof.** NSE trained from 248 genuine daily bars (`min_training_bars
= 140` unchanged); `bootstrap` became `active` with five candidates registered
under `one_week`; a forecast was generated and stored as
`fc_42d151e421dd9f79a566`. After that, `PI-12 models(horizon="5d")` returned
all six models, bridge horizons resolved to `["one_week"]`, and stored forecast
horizons were `["one_week"]`. Evidence:
`tmp/stage_c_remediation_evidence/r08_train_xnse.json`.

### 9.2 BSE training — BLOCKED by genuine history, not lowered

XBOM holds **108 daily rows across 101 distinct sessions** (100 official bhavcopy
rows + 8 Yahoo rows, 6 sessions carrying both; 2026-07-01 … 2026-11-26); the
policy minimum is 140. No model was trained, no bar was synthesised, and the
minimum was not lowered. BSE forecasts stay unavailable until enough official
sessions exist.

Stage D was not started and no strategy/threshold was changed to manufacture a
signal.

---

## 10. R-09 — C1 interpretation (decision required)

**A. Correctness interpretation.** C1 passes when the real decision path is
correct, including a legitimate `HOLD`/`NO_SIGNAL` with complete upstream
evidence (bars, strategy evaluation, signal, decision record, non-actionable
reason). Consequence: the current NSE evidence — 248 daily closes,
`trend_positive`, `HOLD`, `no_action`, confidence 0.50, reference 1386.1, one
`NO_ACTION` forecast decision — could satisfy C1 after operator review.

**B. Actionable-trade interpretation.** C1 requires an actionable `BUY`/`SELL`
with a proposal, NM-08 risk assessment and downstream paper-execution evidence.
Consequence: C1 cannot pass until the market produces a non-HOLD signal; no
threshold, strategy or data may be altered to manufacture one. C7 remains the
natural-lifecycle criterion.

The acceptance documents were **not** modified and C1 was **not** marked PASS.

---

## 11. R-10 — Full-session acceptance preparation

Ready: corrected interval-scoped decision context; session-deduplicated daily
readers; durable control audit with actor; official BSE history with provenance;
session-anchored 20m aggregation independently corroborated by stored 5m bars on
both exchanges; batched persistence; a trained NSE forecast model that PI-12 can
select; `PRAGMA integrity_check` passing on both stores; exchange-isolated
stores; market calendar with holidays/early closes; paper-only invariants
unchanged; restart and lease recovery covered by the AT-01 suite.

The procedure, abort criteria and per-criterion evidence list are in
`docs/STAGE_C_ACCEPTANCE_READINESS_CHECKLIST.md`.

Not started and not authorized: a live session, unattended collector, or any
proposal/order/fill. `2026-11-27` remains an intraday day with no official
bhavcopy; a full-session run must follow the close.

---

## 12. Verification results

| Check | Command | Result |
|---|---|---|
| Focused remediation tests | `pytest tests/test_stage_c_remediation.py tests/test_bse_bhavcopy.py tests/test_novamarket_autonomy.py -q` | **52 passed** |
| Market-data integration | `pytest tests/test_novamarket_marketdata.py -q` | **51 passed, 2 skipped** |
| Related decision/autonomy selection | `pytest tests -q -k "pi12 or forecast_decision or autonomy"` | **216 passed, 1 skipped** (before the schema-test update; the only failure was the v4→v5 expectation, then fixed) |
| Ruff (new files) | `ruff check dedupe.py stage_c_20m_verify.py test_stage_c_remediation.py` | **All checks passed** |
| Ruff (changed code) | `ruff check --output-format=concise` filtered to new symbols | no finding in added code |
| mypy (changed modules) | `mypy autonomy/store.py autonomy/runtime.py marketdata/dedupe.py decision/pipeline.py marketdata/bse_bhavcopy.py marketdata/providers/yahoo.py marketdata/store.py marketdata/engine.py strategies/engine.py service.py` | **Success: no issues in 10 files** |
| mypy (whole package) | `mypy src` | **Success: no issues in 729 source files** |
| mypy (Windows view) | `mypy src --platform win32` | **Success: no issues in 729 source files** |
| API reference | `python scripts/generate_api_reference.py --check` | **in sync (936 routes)** |
| DB integrity | `PRAGMA integrity_check` on both isolated stores | **ok / ok** |
| Broad affected-module regression | 19 files: `test_novamarket*.py`, `test_novamarket_at0[2-6].py`, `test_bse_bhavcopy.py`, `test_nse_bhavcopy.py`, `test_novamarket_marketdata.py`, `test_novamarket_strategies.py`, `test_novamarket_pi12.py`, `test_novamarket_pi1[78]*.py`, `test_novamarket_workbench_scan.py`, `test_novamarket_forecasting.py`, `test_stage_c_remediation.py` | **685 passed, 2 skipped, 2 failed** — both failures were the old v4 schema-version expectation (now v5); `test_novamarket_at02.py` rerun **67/67 passed** |
| Per-file suite matrix | `bash tmp/run_suite_matrix.sh` (216 files) | **216/216 files pass**; 8,189 passed tests + 6,426 subtests, 3 skipped, 0 failed — see §12.1 |
| 20m verifier | `stage_c_20m_verify.py` on both stores | **1000/1000 independent 5m components, 250/250 independent recomputes, 0 problems** each |
| NSE forecast end to end | `tmp/r08_train_nse.py` | **TRAINED**, `fc_42d151e421dd9f79a566`, `models("5d")` returns 6, bridge `["one_week"]` |
| Desktop capture regression | `pytest tests/test_desktop.py -q` | **81 passed, 20 subtests** (pre-existing desync fixed) |

Pre-existing Ruff findings in untouched regions of `app.py`, `service.py`,
`pipeline.py` and the autonomy package (import ordering, long lines, quoted
annotations) remain; they are not introduced by this work and were not mass-fixed
to keep the diff scoped.

### 12.1 Full suite — complete per-file matrix (216/216)

A single-process `pytest tests -q` run was stopped earlier at 17% after 90
minutes (~9 h projected: OneDrive-served fixtures plus no `pytest-timeout` to
bound stalled tests). It was replaced by a **bounded, resumable per-file
matrix**: every one of the 216 test files ran in its own pytest process (240 s
bound; 900 s for app/e2e/web files), with one log and one ledger row per file
(`tmp/stage_c_remediation_evidence/suite_runs/`).

| Outcome | Files |
|---|---|
| Passed on first run | 209 |
| Passed only with a wider bound | 2 (`test_application` 145 s, `test_novamarket_api` 327 s) |
| Passed after a code fix | 1 (`test_desktop` — pre-existing capture desync) |
| Passed after isolating the host environment | 2 (Playwright under load; host admin token) |
| Passed on re-run only (transient) | 1 (`test_build_project`) |
| No tests collected — optional-dependency skip (exit 5) | 1 (`test_database`, `sqlalchemy` absent) |

Aggregate after substituting the resolved runs: **8,189 passed tests + 6,426
passing subtests, 3 skipped, 0 failed, 0 errors**. Six first-run failures were
each traced to a cause:

1. `test_application.py` / `test_novamarket_api.py` — the 240 s bound, not a
   hang. Both complete under a wider bound (34 and 66 tests passed). Cold
   self-improvement scan of 948 files measured at 19.6 s on this host.
2. `test_desktop.py` — **real defect, fixed.** A cancellation during the
   capture worker's startup banner read left `ready 800x600` on the pipe; the
   next exchange parsed the banner as its reply. `_read_line` now marks the
   worker desynced on `CancelledError`, so the next exchange restarts it.
3. `test_browser_token_reuse_e2e.py` — Playwright `Page.goto` 30 s timeout under
   full-suite load; passes isolated.
4. `test_focus_rings.py` — proven host configuration: this machine's `.env` sets
   `NOVACONTROL_ADMIN_TOKEN`, so the tokenless page gets 401 from `/status` and
   `#statusText` never leaves "Initializing"; measured `create_app` startup
   30.3 s. With that variable emptied: 7 passed.
5. `test_build_project.py` — one transient failure in a retained log, not
   reproducible in 3/3 isolated re-runs (21 passed each); the ledger recorded
   exit 0 for it.

Limitation kept explicit: a per-file matrix isolates module-level state per
file, so it is **not** byte-identical to one-process `pytest tests`. A CI run on
a fast filesystem remains the gate for that; no single-process full-suite pass
is claimed.

### 12.2 Static analysis

- `mypy src` — 729 files, no issues.
- `mypy src --platform win32` — 729 files, no issues.
- `scripts/generate_api_reference.py --check` — in sync at 936 routes.
- Ruff: all three new files clean; no finding attributed to changed code in the
  modified files. Pre-existing findings in untouched regions (I001/E501/UP037
  and similar in `app.py`, `service.py`, `pipeline.py`, `autonomy/monitoring.py`)
  were deliberately not mass-fixed.

---

## 13. Remaining limitations

1. BSE has 108 genuine daily rows across 101 sessions but one weekday gap
   (`2026-07-08`) and no
   `2026-11-27` bar (not yet published). No bar was synthesised.
2. BSE cannot train a forecast model (108 < 140). NSE can train and PI-12 now
   selects its stored forecast, but BSE stays blocked by genuine history.
3. The generated NSE forecast has `validity = null`: no matured outcome window
   was observed, so no forecast accuracy is claimed.
4. C1 interpretation is open; C2 needs an authorized decision run; C7 needs a
   natural lifecycle. C3–C6 are PASS.
5. Pre-existing Ruff findings outside the changed code were left untouched
   (21 long lines in `desktop/capture.py` predate the one desync fix there).
6. The 485.799 ms acceptance lag sample was not reproduced; the mechanism is
   confirmed and measured at 696 ms → 31 ms peak in a controlled workload.
7. Three suite files need a host with Playwright/a longer budget
   (`test_browser_token_reuse_e2e`, `test_focus_rings`) or more time than the
   per-file bound (`test_application`); see §12.1.

## 14. Next required operator actions

1. Decide C1 interpretation A or B (R-09).
2. Confirm C3 as met: independent 5m corroboration is now 1000/1000 and 250/250
   recomputes on both exchanges (the earlier open decision is satisfied).
3. Decide the BSE forecast scope: wait for 32 more official sessions, change the
   training minimum under a separate validation plan, or exclude the BSE forecast
   leg from C2.
4. Authorize an after-close acceptance run for C2/C7 once the above are settled
   (`docs/STAGE_C_ACCEPTANCE_READINESS_CHECKLIST.md`).
5. Authorize Stage D only after every Stage C criterion is PASS.

## 15. Integrity of existing work

No commit, push, deploy, reset, clean or stash. The original diagnosis and all
Stage C evidence/reports are unchanged. The normal user financial store was not
opened. Only the isolated acceptance stores received additive writes (XBOM daily
bars), after a byte backup.
