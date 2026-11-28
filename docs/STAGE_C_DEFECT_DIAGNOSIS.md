# NovaMarket Stage C defect diagnosis and remediation plan

**Diagnosis date:** 27 November 2026
**Mode:** strict diagnostic. No production source, test, evidence, store or report was modified.
**Scope:** XNSE and XBOM only. XNYS remains deferred and was not opened.
**Verdict:** Stage C did **not** fail because the decision pipeline is broken. It stopped at three
independent boundaries: a real provider data-depth limit on BSE, a missing audit seam on the runtime
control path, and a verification-method limit on derived 20m bars. One additional PI-12 defect was
found that the earlier register did not record.

## Status (unchanged by this diagnosis)

| Criterion | Status |
|---|---|
| C1 — NSE decision path | PARTIAL |
| C2 — BSE decision path | PARTIAL |
| C3 — 20m and configured intervals | PARTIAL |
| C4 — Indian daily bars | PARTIAL |
| C5 — Pause/resume and audit | PARTIAL |
| C6 — Restart and lease recovery | PASS |
| C7 — Natural paper lifecycle | NOT VERIFIED |
| Stage C | OPEN |
| Stage D | NOT STARTED |

---

## 0. Evidence boundary and method

Everything below comes from three read-only sources:

1. The eleven mandatory reports listed in the task.
2. Source inspection of the decision, strategy, market-data, autonomy and audit modules.
3. Read-only SQLite queries (`mode=ro`, `PRAGMA query_only=ON`) over the two isolated acceptance
   stores in `tmp/stage_c_v2_2026_11_27/{xnse,xbom}/data/novamarket.sqlite3`, plus the saved JSON
   evidence files beside them.

No application was started, no provider request was made, no store was written, no migration was run,
and no test was executed. The only filesystem write in this session is this report. The worktree had
176 dirty entries (39 untracked) before the report was written; the report adds one untracked file.

All pre-existing acceptance evidence, including the failed harness attempts, remains as recorded.

---

# Part I — Investigations

## A. NSE and BSE decision paths (C1, C2)

### A.1 What the two stores actually contain

| Observation | XNSE | XBOM |
|---|---|---|
| Decision runs | 1, `completed` | 1, `completed` |
| Evaluations | 1 | 1 |
| Evaluation outcome | `no_action` | `no_action` |
| Triggered conditions | `["trend_positive"]` | `["insufficient_history"]` |
| Evaluation explanation | "The fast average remains above the slow average; no fresh cross." | "8 closes are available; the trend strategy needs 14." |
| Signal direction | `hold` | `no_signal` |
| Signal validation | `no_action` | `no_action` |
| Signal confidence / reference | `0.50` / `1386.1` | `0` / `1386.0` |
| Decision signal quality | `measured`, freshness `fresh` | `measured`, freshness `fresh` |
| Proposals / risk assessments | 0 / 0 | 0 / 0 |
| Orders / fills / intents / positions | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| PI-12 prediction decisions | 1 × `NO_ACTION` | 1 × `NO_ACTION` |
| PI-12 reason | "no stored forecast exists for the configured horizons (5m, 15m, 20m, 1h, 1d, 3d)" | same |
| Strategy | `strat_5325afdf7e1fa071`, `trend_sma`, fast 3 / slow 9 | `strat_6f94ba703c207203`, `trend_sma`, fast 3 / slow 9 |
| Daily closes available to the strategy | 248 | 8 |

### A.2 Where the pipeline stopped

The pathway is
`market data → normalization/quality → monitoring candidate → strategy evaluation → signal →
decision-stage validation → proposal → NM-08 → NM-10`.

- **NSE stopped inside the strategy.** `evaluate_trend` returned `HOLD` because the fast SMA was
  above the slow SMA with no fresh crossover. `HOLD` is a member of `_NO_ACTION`
  (`decision/pipeline.py:1753`), so the decision stage recorded `EvaluationOutcome.NO_ACTION` and
  returned at `decision/pipeline.py:1838` **before** `_ensure_proposal`. Zero proposals is the
  intended behaviour for a hold, not a suppressed candidate.
- **BSE stopped one stage earlier, in data sufficiency.** `evaluate_trend`
  (`strategies/catalog.py:144`) requires `slow_period + 5 = 14` closes. The store had 8, so it
  returned `NO_SIGNAL` with `insufficient_history` and no reference for an entry. The guard failed
  closed exactly as designed.

Neither run reached NM-08. `risk_decisions`, `decision_risk_assessments`, `execution_orders`,
`execution_fills` and `autonomy_execution_intents` are all zero on both exchanges. A zero-approval
count is therefore **not** evidence about NM-08 in either direction.

### A.3 Candidate causes that were checked and excluded

| Candidate cause | Result | Evidence |
|---|---|---|
| Stale or unusable market data rejected the candidate | **Excluded on both** | Candidate quality `good`; signal quality `measured`, freshness `fresh`; `_validate_signal` accepts both. BSE's stale 1m/5m bars did not reach the strategy because the strategy reads `DataInterval.DAY_1` only. |
| Exchange/symbol mapping rejection | **Excluded** | `XNSE:RELIANCE` → `RELIANCE.NS`, `XBOM:RELIANCE` → `RELIANCE.BO`; monitoring candidate MIC matched the instrument prefix; `_candidates` accepted both. |
| Wrong exchange configuration | **Excluded** | Separate stores, one instrument each, separate portfolio and strategy, monitoring exchange `XBOM`/`XNSE`; BSE monitoring detail contained no XNSE candidate. |
| Risk controls rejected a valid candidate | **Excluded (not reached)** | No proposal existed, so `_assess_proposal` was never called. |
| Scheduler or eligibility gate suppressed proposals | **Excluded** | The decision run completed; the monitoring cycle was `completed`, `session_eligible` and `calendar_verified`; the strategy was evaluated and its signal recorded. The stop is above the proposal stage. |
| Harness invoked the wrong decision path | **Excluded** | The persisted records are AT-03 `decision_runs` / `decision_evaluations` / `decision_signals`, produced through the supported route with `trigger=manual`. |
| Forecast-primary policy skipped the strategy | **Excluded** | `_persist_forecast_decision` adds to `skip` only when an identical decision already exists; a fresh `NO_ACTION` does not skip the strategy. Both a `NO_ACTION` prediction decision and a strategy evaluation exist in the same run. |
| The zero-proposal result was expected | **Yes, on both** | NSE: genuine `HOLD`. BSE: genuine `NO_SIGNAL` from 8 < 14 closes. |

### A.4 Findings

**SC-06 — NSE zero proposals.** Classification: **NOT A DEFECT.** The configured strategy is a
3/9 SMA crossover. It enters only on the session where the fast average crosses the slow average.
On 27 November the averages were already separated (`trend_positive`), so `HOLD` was correct. The
decision path executed end to end; only the trade did not exist.

**SC-02 — BSE zero proposals.** Classification: **ENVIRONMENTAL LIMITATION** (provider depth), with
a contributing process factor described in Investigation C. The strategy correctly refused to
evaluate on 8 closes. Nothing in the decision code rejected BSE.

**SC-09 — the PI-12 decision reads the *oldest* 60 bars, across mixed intervals.**
Classification: **CONFIRMED ROOT CAUSE** (new finding).

`AutonomyDecisionPipeline._forecast_bars` calls
`self._market_data.bars(instrument_id, limit=60, ascending=True)`
(`decision/pipeline.py:1230`) — no `interval`, and ascending.

`MarketDataStore.bars` builds `ORDER BY observed_at ASC LIMIT ?` when `ascending=True`
(`marketdata/store.py:990-999`), so the call returns the **oldest** 60 rows in the store, and with no
interval filter those rows can be any interval. The same repository already fixed this exact defect
class twice: `strategies/engine.py:1204-1260` reads `interval=DAY_1, limit=200, ascending=False`
with a comment that an ascending read "would take the OLDEST 200 bars", and
`tests/test_novamarket_pi18.py:772` plus three tests in `tests/test_novamarket_workbench_scan.py`
are named "reads the newest bars not the oldest".

Direct confirmation from the acceptance stores — the published `technical_context.last_close` equals
the last row of the oldest-60 window, not the newest:

| Exchange | `technical_context.last_close` | Oldest-60 window ends at | Newest bar |
|---|---:|---|---|
| XNSE | `1441.300048828125` | 2026-02-18 daily close `1441.30` | 2026-11-27 10:16 IST, `1386.40` (1m) |
| XBOM | `1337.300048828125` | 2026-11-11 20m close `1337.30` | 2026-11-27 10:14 IST, `1386.00` (1m) |

Consequences, all bounded:

- The PI-12 `technical_context` (SMA5/SMA20, ATR%, median volume, `change_5_bars`) is computed on
  February data for NSE and 11 November data for BSE, and on a mixture of 1m/5m/15m/20m rows for BSE.
  It is stored in `prediction_decisions` as if it described the decision moment.
- With the default policy `require_technical_confirmation=false`, this did not change any decision
  type and cannot create or suppress an order. If that flag is ever enabled, the gate would run on
  months-old prices.
- The strategy path is unaffected: `_market()` reads daily bars newest-first correctly.

This is a production defect in the decision-evidence path, independent of C1/C2's zero-proposal
result. It also means C3's "forecast and strategy consumption of the validated 20m groups" cannot be
evidenced from these payloads.

**SC-16 — `signals` counter includes `NO_SIGNAL`.** Classification: **NOT A DEFECT (reporting
semantics).** `_evaluate_instrument` persists a `DecisionSignal` for every returned direction and
increments `counts["signals"]`, including `no_signal`. "One evaluation and one signal" in the
acceptance reports therefore describes a non-actionable record for BSE. It did not affect any gate,
but the wording overstates what occurred and should be read carefully in future evidence.

---

## B. 20-minute aggregation and the missing 5m components (C3)

### B.1 Measurement

| | XNSE | XBOM |
|---|---|---|
| Stored 20m bars | 250 | 250 |
| 20m window | 2026-11-05 10:55 → 2026-11-27 09:55 IST | 2026-11-05 10:55 → 2026-11-27 09:55 IST |
| Stored 5m bars | 249 | 251 |
| 5m window | 2026-11-20 14:00 → 2026-11-27 10:10 IST | 2026-11-20 14:00 → 2026-11-27 10:14 IST |
| Groups with all four persisted 5m rows | 60 | 60 |
| OHLCV mismatches in those groups | 0 | 0 |
| Groups without persisted components | 190 | 190 |
| 20m bars flagged partial | 0 | 0 |

### B.2 Why the coverage is 60 of 250 — confirmed arithmetic

`IngestionSettings.history_limit` defaults to **250** (`marketdata/settings.py:44`) and is applied
**per interval, as a bar count** (`marketdata/engine.py:1177`). The same cap therefore buys a
completely different amount of time at each interval:

- 250 × 5m = 1,250 minutes of session time ≈ 3.3 sessions → 251 stored 5m rows.
- 250 × 20m = 5,000 minutes ≈ 13.3 sessions → 250 stored 20m rows.

The 20m series is four times longer than the 5m series. Only the overlap can be verified:
`1,250 / 20 ≈ 62` possible four-bar groups, of which 60 have all four rows present. That is exactly
the observed 60/190 split. The missing components were **never requested**, not filtered, not
rejected and not lost: the 5m page simply does not reach back to 5 November.

### B.3 Is the aggregation itself correct?

`YahooAdapter._aggregate_20m` (`marketdata/providers/yahoo.py:460-540`) was reviewed line by line:

- groups four consecutive 5m bars on the exchange clock;
- requires offsets exactly `[0, 5, 10, 15]` and drops an incomplete group (no gap filling);
- `open` = first component open, `high` = max, `low` = min, `close` = last close;
- `volume` = sum, and **null** if any component volume is missing — it does not treat missing volume
  as zero;
- rejects a group whose components disagree on currency;
- records `derived_from: "5m"`, `component_count: 4` and the four `component_timestamps`.

The 60/60 exact match on all five values is consistent with this reading. C3 is limited by evidence
coverage, not by arithmetic.

### B.4 Every 20m bar is nevertheless self-verifying

Each stored 20m row keeps its source rows in `raw_json` (`derived_from`, `components` with open/high/
low/close/volume/timestamp). Read-only inspection of the oldest XBOM 20m bar confirms four complete
components. The aggregation arithmetic can therefore be recomputed for **all 250** bars from the
provider values that produced them. What the current check proved — and what the store cannot prove —
is the *independent* 5m persistence for the older 190.

This distinction must be stated precisely in any future C3 claim: 250 bars are arithmetically
reproducible from their recorded components; 60 bars are independently corroborated against a
separate stored series.

### B.5 Findings

**SC-04 / SC-12 — 190 groups lack independent components.** Classification: **CONTRIBUTING FACTOR**
(confirmed mechanism, not a product defect). A bar-count history cap applied identically to intervals
of different duration makes the derived series longer than its source series. No fabrication, no data
loss, no wrong aggregation.

**SC-10 — the 20m bucket anchor is the page minimum, not the session open.**
Classification: **HYPOTHESIS — NEEDS EVIDENCE** (latent).

`_aggregate_20m` computes `anchor = min(hour*60+minute)` over the fetched page
(`marketdata/providers/yahoo.py:476`). The anchor is therefore a property of *which rows the provider
returned*, not of the exchange session. If a page's earliest bar is 09:20 instead of 09:15 — a
plausible provider page edge, a late first print, or a truncated response — every bucket for the
whole page shifts by five minutes, and the resulting 20m bars are silently offset from the session
grid while still passing the four-component check. No observed bar shows this in the current data
(the first bucket opens at 09:15 IST and the last at 14:55 IST), so it is not a confirmed failure.
It is a correctness risk with a cheap test.

**Other configured intervals.** `FEATURE_RESOLUTIONS` lists 1m, 5m, 15m, 20m, 30m, 1h, 1d, 1wk, 1mo
(`config.py:39-49`); the acceptance configured 1m, 5m, 15m, 20m and 1d and the store contains exactly
those. Only 20m is derived. 1m/5m/15m/1d are native provider bars, so "verification" for them means
provenance and freshness, not aggregation. The 15m rows (249/250) were stored but were outside the
declared Stage C check scope; 30m, 1h, 1wk and 1mo were never ingested.

---

## C. Indian daily history (C4)

### C.1 What is stored

**XNSE — 248 daily rows, 2025-11-25 → 2026-11-26.** The latest row is the last completed session
before the run. This part of C4 passes.

**XBOM — 8 daily rows:** 13, 16, 17, 18, 19, 20, 23 and 25 November 2026. All eight carry
`retrieved_at = 2026-11-27T04:59:12.561480Z` — one provider response, during the BSE run.

Two absences must be separated:

- **24 November is correctly absent.** `_NSE_HOLIDAYS_2026` (`marketdata/calendar.py:84`) marks
  24 November 2026 as "Prakash Gurpurb Sri Guru Nanak Dev". Both calendars use the same holiday map
  (`_NSE` and `_BSE`, lines 282-300). Eight sessions from 13 November to 25 November is the complete
  set of trading days in that window.
- **26 November is missing.** That session was completed and tradable and the calendar does not mark
  it a holiday. Its daily bar was not in the provider's response.

### C.2 Why the history is eight rows, and why 26 November is absent

The ingestion path is not the cause. `_collect_bars` requests `limit=history_limit` (250) and appends
**every** returned row; invalid rows are still stored before the quality check, and `_normalize_bar`
only drops rows that are structurally unusable. All eight rows are present with full provenance, so
nothing was discarded locally. The single batch timestamp proves one request produced exactly these
eight sessions.

Three candidate explanations remain, and the evidence distinguishes them:

1. **Provider depth.** Yahoo's `.BO` daily series returned eight sessions for a `range=10y` request,
   while `.NS` returned roughly a year (248 rows). That is a provider-side availability difference
   for the BSE listing, not a symbol, calendar or parser problem. **Most consistent with the data.**
2. **Publication lag.** The completed 26 November daily bar may not have been published for `.BO` at
   10:59 IST on 27 November, even though intraday 1m/5m bars were current to 10:14 IST. This would
   explain the single missing recent session while the older eight are present.
3. **Ingestion short-circuit.** `_needs_bars` skips a daily request when the latest completed session
   is already stored (`marketdata/engine.py:980-1000`). It cannot explain a *missing* session on a
   first fetch, and it did not run a second time here.

Explanations 1 and 2 are both provider behaviour and both are resolved the same way: use BSE's
official end-of-day file for BSE daily bars instead of relying on the `.BO` chart series.

### C.3 Is the fourteen-close requirement configured correctly?

Yes. `evaluate_trend` computes `minimum = slow_period + 5` (`strategies/catalog.py:144-152`); the
strategy config is `slow_period: 9`, so 14 is derived, not hard-coded. The strategy receives up to
200 daily bars (`strategies/engine.py:1230-1240`) and failed closed on 8. The guard is correct; the
input was insufficient.

### C.4 Findings

**SC-02 — BSE daily depth.** Classification: **ENVIRONMENTAL LIMITATION**, with a **CONTRIBUTING
FACTOR**: the acceptance used only the Yahoo chart series for BSE daily bars even though the
repository already contains an official BSE bhavcopy importer
(`marketdata/bse_bhavcopy.py`, tested by `tests/test_bse_bhavcopy.py`) that writes
`provider="bse_bhavcopy"`, `quality=VALID`, `freshness=CLOSED_MARKET` daily bars for registered BSE
instruments. That path was not exercised.

**SC-05 — the 27 November close was not observed.** Classification: **ENVIRONMENTAL LIMITATION.**
Both runs were bounded morning runs, stopped before 11:00 IST. No overnight collector was left
running, by design.

**SC-13 — two providers can store the same session twice.** Classification: **CONFIRMED ROOT CAUSE
(latent, not yet triggered).**

`market_bars` is `UNIQUE(instrument_id, provider, interval, observed_at)`
(`marketdata/store.py` schema) and `MarketDataStore.bars` filters by instrument and interval but does
**not** deduplicate by timestamp or prefer a provider (`marketdata/store.py:962-999`). If BSE
bhavcopy bars are imported for sessions that already have Yahoo `.BO` bars, the store will hold two
`1d` rows for the same session, and `Strategies._market` will feed both closes into the SMA — a
duplicated session, not a duplicated bar id. This must be handled *before* the bhavcopy remediation,
not discovered afterwards.

Note also that the seven older BSE daily bars carry `quality="stale"` and the 25 November bar
`"valid"`. That is an age-based freshness label applied to historical rows, not an invalidity
finding, and `_market()` does not filter on it.

---

## D. Pause, resume and emergency-stop audit (C5)

### D.1 The exact path

`service.autonomy_pause/resume/stop` (`service.py:1340-1355`) call
`AutonomyRuntime.pause/resume/stop` (`autonomy/runtime.py:400-465`), which call `_set_mode`
(`autonomy/runtime.py:198-220`), which calls `AutonomyStore.save_control`
(`autonomy/store.py:544`).

`save_control` performs a single `UPDATE autonomy_runtime SET mode=?, desired_mode=?, runtime_state=?,
last_reason=?, updated_at=? WHERE id=1`. The table is a singleton (`id INTEGER PRIMARY KEY CHECK
(id = 1)`).

### D.2 What that means

| Requirement | Reality |
|---|---|
| Durable state transition | **Yes** — mode and desired mode are persisted and survive restart. |
| Durable control *event* | **No** — each call overwrites the previous one. |
| Actor | **Not recorded** (no actor column or parameter). |
| Timestamp of the operation | Only `updated_at`, overwritten by the next transition. |
| Reason | Only `last_reason`, overwritten by the next transition. |
| Outcome of a refused control | **Nothing written.** `pause()` on a disabled runtime, `resume()` without acknowledgement, and `start()` without acknowledgement raise before any write. |
| Audit journal record | **Never** — `AutonomyRuntime` has no publisher and no audit logger; it imports neither `novacontrol.audit` nor any event bus. `novacontrol/audit/logger.py` is attached to `NovaMarketService` only at NM-14 via `attach_audit_logger` (`service.py:9629`), and no autonomy control path calls it. |

The isolated `audit.jsonl` therefore contained zero "operator pause", zero "operator resume" and zero
"emergency stop" records — not because the operator workflow was wrong, but because no code path can
produce them.

### D.3 Finding

**SC-01 — no control audit seam.** Classification: **CONFIRMED ROOT CAUSE.**

C5's audit requirement cannot be satisfied by re-running the same workflow. This is a product
observability defect: the runtime's control history is destroyed by design (single-row state), and
refusals — arguably the most security-relevant control events — leave no trace at all.

The prior register called this an "evidence/observability gap; root cause not established". It is now
established.

---

## E. Natural paper lifecycle (C7)

### E.1 What C7 requires

Per the canonical preflight (`STAGE_C_PREFLIGHT_REPORT.md`, "C7 rule"): a **naturally eligible**
signal that reaches the complete path
`NM-08 → NM-10 → portfolio/ledger → exit → reconciliation → forecast outcome → advisory RL`, with no
threshold change, synthetic signal or fixture substitution.

### E.2 What is verified today

| Stage | Status | Basis |
|---|---|---|
| Provider observation and provenance | PASS | Real Yahoo `.NS` / `.BO` records |
| Session and monitoring | PASS | Completed, eligible, calendar-verified cycles |
| Strategy evaluation and signal | PASS (as a decision) | Persisted evaluations and signals |
| Proposal creation | **Not exercised** | No actionable signal existed |
| NM-08 approval / rejection | **Not exercised** | Zero risk assessments |
| NM-10 paper order, fill | **Not exercised** | Zero orders/fills/intents |
| Position, ledger, P&L | **Seed state only** | INR 100,000, two balanced setup transactions, zero positions |
| Exit supervision | **Not exercised** | No position |
| Trade reconciliation | **Not exercised** | No paper trade (runtime recovery separately reported zero unresolved records) |
| Forecast outcome | **Not exercised** | `prediction_forecasts = 0` |
| Advisory RL evidence | **Not exercised** | No matured outcome |

The NM-08 and NM-10 *mechanisms* have unit and integration coverage
(`tests/test_novamarket_risk.py`, `tests/test_novamarket_execution.py`,
`tests/test_novamarket_at04.py`, `tests/test_novamarket_pi12.py`). What is missing is not a mechanism
test but a natural-signal run.

### E.3 Why no signal occurred

- NSE: a 3/9 SMA crossover strategy enters only on the crossover session. The averages were already
  separated on 27 November. With ~248 daily bars there are only a handful of crossovers per year;
  the probability that one falls inside a 33-second acceptance window is very small.
- BSE: impossible, because 8 < 14 closes.

### E.4 Can replay be used?

Yes, but only as **supporting** evidence. The repository supports deterministic replay over stored
bars (workbench replay, backtesting, shadow mode), and a replay could demonstrate the NM-08 → NM-10 →
ledger → exit → reconciliation mechanics end to end without touching real money and without inventing
a price. It cannot satisfy C7's "natural real-market" wording, and it must not be labelled as if it
did. The honest options are:

1. Keep C7 **NOT VERIFIED** and wait for a natural crossover inside an authorized session window —
   the only path that satisfies the criterion as written.
2. Obtain operator approval for a documented criterion amendment that separates "lifecycle mechanics
   proven by deterministic replay" from "natural live signal observed", and record both.
3. Do nothing to the strategy. Changing `fast_period`/`slow_period` to force a signal is prohibited.

### E.5 Findings

**SC-11 — C1 and C7 conflate pipeline correctness with the occurrence of a rare event.**
Classification: **CONFIRMED ROOT CAUSE — ACCEPTANCE DESIGN.**

C1's matrix requires "forecast generation", "NM-08 risk assessment" and "NM-10 paper execution" as
sub-rows, but every one of them depends on a strategy crossover that may not occur for weeks. As
written, C1 can only pass on a day the market happens to cross, and C7 duplicates the same dependency
for the full lifecycle. That is why a technically healthy decision path is recorded as PARTIAL. The
diagnosis does not change the status; it identifies that no amount of engineering on the decision
path can close C1 without either a natural event or an explicit criterion clarification.

**SC-14 — no forecast model exists, so PI-12 can never produce BUY/SELL in these stores.**
Classification: **NOT A DEFECT** (correct fail-closed behaviour), **acceptance-preparation gap**.

`prediction_models = 0`, `prediction_forecasts = 0`, while `prediction_requests` = 2 (NSE) / 1 (BSE)
and `prediction_forecast_cycles` = 2 / 1. The PI-09 scheduler created real 20m forecast requests and
checkpoints; no model was trained or registered in either isolated store, so no forecast was
produced. The PI-12 evaluator correctly returned `NO_ACTION` with an explicit reason. For NSE, 248
daily bars exceed the forecasting default `min_history_bars = 80`; for BSE, 8 bars cannot satisfy it
until C4 is fixed. This is a dependency, not a bug.

---

## F. Event-loop lag and performance

### F.1 Confirmed blocking mechanism

`MarketDataStore.append_bar` (`marketdata/store.py:705-760`) runs, per bar:

```python
with self._lock, self._connection:
    cursor = self._connection.execute("INSERT OR IGNORE INTO market_bars ...")
```

`sqlite3` is synchronous, the connection is shared (`check_same_thread=False`), and `with
self._connection` commits at block exit — one WAL commit per bar. The acceptance ingestion stored
roughly 1,250 bars (250 × five intervals) per exchange, so one ingestion burst is on the order of a
thousand individual commits, plus JSON serialization of `provider_metadata` and `raw` on every row.

Nothing in the market-data ingestion path offloads this work: `asyncio.to_thread` appears in
`backtesting`, `learning`, `notifications`, `radar`, `service` and the provider HTTP transport, but
**not** in `marketdata/store.py` or `marketdata/engine.py`. The network call itself is offloaded
(`UrllibTransport.get` → `asyncio.to_thread`, `providers/base.py:123-131`), so provider latency is not
the blocking source; local persistence is.

This is a confirmed blocking path on the event loop. It is a sufficient mechanism for a multi-hundred
millisecond stall.

### F.2 What is not confirmed

- The 485.799 ms peak is **not** correlated with any step. The probe (`stage_c_acceptance_runner.py`
  lines 204-214) records only the delay value, not a timestamp or the step in flight. There is no
  evidence that the peak occurred during ingestion, during the radar request, or during garbage
  collection.
- The probe co-existed in the same event loop as the application, inside the acceptance process. It
  measures loop wake-up delay, which is a valid signal of blocking but not a per-route latency.
- p95 was 13.694 ms over 410 samples and the API samples were 29 ms p95; a single 485 ms outlier in a
  ~2-minute window is a stall, not a sustained regression.
- No concurrency, no soak, and no post-fix comparison exist. The older 270 ms integration figure is
  not comparable (different workload, no correlation).

### F.3 Findings

**SC-07a — per-bar synchronous commit on the event loop.** Classification: **CONFIRMED ROOT CAUSE**
(of a blocking mechanism; not proven to be the cause of the specific peak).

**SC-07b — the 485.799 ms sample.** Classification: **HYPOTHESIS — NEEDS EVIDENCE.**

---

## G. Regression and verification gaps

### G.1 Existing tests that already cover part of this

| Area | Test |
|---|---|
| 20m exact aggregation | `tests/test_novamarket_marketdata.py::test_yahoo_20m_is_an_exact_aggregate_of_four_5m_bars` (line 397), `::test_a_20m_request_ingests_exact_derived_bars` (1015) |
| 20m accepted interval | `tests/test_novamarket_gateway.py::test_20m_is_an_advertised_and_accepted_interval` |
| Pause/resume state | `tests/test_novamarket_autonomy.py` (transitions only — **no audit assertion**) |
| Decision audit immutability | `tests/test_novamarket_pi12.py::test_decision_audit_is_immutable_and_idempotent` |
| Newest-window read precedent | `tests/test_novamarket_pi18.py::test_the_optimization_context_reads_the_newest_bars_not_the_oldest`; three tests in `tests/test_novamarket_workbench_scan.py` |
| BSE bhavcopy parsing/import | `tests/test_bse_bhavcopy.py` |
| NM-10 / AT-04 execution | `tests/test_novamarket_at04.py`, `tests/test_novamarket_execution.py` |
| Restart / lease | `tests/test_novamarket_at06.py`, `test_novamarket_at06_runtime_integration.py` |

### G.2 Tests that do not exist and are required

1. Control audit: pause, resume, emergency stop and a **refused** control each append one immutable
   record with actor, reason, from-mode, to-mode, lease owner and outcome; the sequence survives a
   restart and is not overwritten by the next control.
2. `_forecast_bars`: the newest stored bar is the last element and every element shares one interval.
3. 20m anchor: a page whose earliest bar is 09:20 must still bucket on the session grid (or must
   refuse to aggregate), and an incomplete first group must be dropped rather than shifted.
4. Duplicate-session guard: importing an official bhavcopy bar for a session already covered by
   another provider must not create a second `1d` row, and the strategy's closes must not contain a
   duplicated session.
5. Interval-span alignment: a 20m request must either acquire enough 5m history to cover its window or
   record an explicit, machine-readable coverage fraction.
6. Forecast context freshness: a decision at 27 November must not report a February or 11 November
   `last_close`.
7. Performance: an ingestion burst must not delay loop wake-ups beyond a stated budget, measured with
   timestamps correlated to steps.

### G.3 Commands available (documented, not executed in this session)

From `novamarket/`:

```
python -m pytest tests/ -q
python -m ruff check src tests
python -m mypy src
python -m mypy src --platform win32
python scripts/generate_api_reference.py --check
```

CI runs `pytest tests/ -q`, both mypy platform views, and the API-reference check
(`.github/workflows/ci.yml`). Ruff is configured (`pyproject.toml:116-122`) but not run by CI.

### G.4 C6 preservation

C6's PASS rests on the isolated 60-second lease probe: process A terminated while holding a valid
lease, process B refused to steal it and retried, recovered after natural expiry, and emergency stop
cleared retry so process C stayed disabled. Changes that could invalidate it:

- any edit to `AutonomyStore.acquire_lease/heartbeat/release_lease/recover` or the
  `autonomy_runtime`/`autonomy_cycles` schema;
- any change to `AutonomyRuntime.start/stop/recover` ordering, especially the
  `disable_autostart` path;
- any change to `claim_cycle` or the one-running-cycle unique index;
- any change to `autonomy_execution.recover` or `autonomy_exits.recover`;
- adding a control-audit write **inside** the lease transaction without preserving atomicity.

The audit remediation in R-02 must therefore keep `autonomy_runtime` semantics intact and append the
event in the same transaction as the state write, or after it with a guaranteed reconciliation.

---

# Part II — Deliverables

## A. Executive diagnosis

Stage C did not pass for four separable reasons, only two of which are code defects:

1. **The BSE strategy never had enough history to evaluate.** Yahoo returned eight `.BO` daily
   sessions (13-25 November, with 24 November a genuine exchange holiday) and omitted the completed
   26 November bar. The strategy needs 14. It failed closed with `insufficient_history`, so BSE could
   not produce a proposal, and the same eight bars make BSE forecasting impossible (needs 80). C2 and
   C4 are blocked on data acquisition, not on decision logic.
2. **NSE's strategy legitimately returned HOLD.** The 3/9 SMA pair was already separated on
   27 November, so no proposal was correct. C1 is PARTIAL because the acceptance criterion requires a
   downstream trade that a crossover strategy cannot be expected to produce on any given morning.
3. **The runtime control path has no audit seam at all.** Pause, resume and emergency stop overwrite
   a single row. Actors, timestamps, reasons and refusals are not recorded, and nothing reaches
   `audit.jsonl`. C5 cannot pass by re-running the same workflow; code must change.
4. **The 20m verification method cannot cover the 20m window.** A per-interval cap of 250 bars gives
   the 5m series about 3.3 sessions and the 20m series about 13.3 sessions. Exactly 60 of 250 groups
   overlap. The aggregation is correct — 60/60 exact — but 190 groups have no independently persisted
   source rows. The arithmetic for all 250 is still reproducible from each bar's recorded components.

In addition, this diagnosis found a defect the register missed: the PI-12 decision pipeline reads the
**oldest** 60 bars, mixed across intervals, and publishes them as the decision's technical context.
On NSE that context is February data; on BSE it is 11 November data. It did not change any decision
under the default policy, but the published evidence is wrong and would become a live gate if
`require_technical_confirmation` were enabled.

Nothing observed suggests the decision pipeline, NM-08, NM-10, the ledger, exchange isolation or the
lease mechanism is broken. They were simply not reached.

## B. Root-cause analysis

### B.1 NSE-specific

- No defect blocked the pipeline. `trend_sma` 3/9 on 248 daily closes produced `HOLD` with
  `trend_positive`; `HOLD` is non-actionable by design.
- Secondary defect affecting NSE evidence: **SC-09** (oldest-60 mixed-interval technical context).
- Secondary gap: **SC-14** (no forecast model), which forces PI-12 to `NO_ACTION`.

### B.2 BSE-specific

- **SC-02**: eight daily sessions from Yahoo `.BO`, 26 November absent → `insufficient_history`
  (needs 14) and no possible forecast (needs 80). Environmental provider limit.
- **SC-13** (latent): the official-bhavcopy fix must not create a second daily row for a session.
- **SC-03**: 1m/5m bars were 15 minutes old at retrieval against a 900 s declared delay → "stale"
  freshness. This is the provider's delay, correctly labelled; it did not block the daily strategy.

### B.3 Shared infrastructure

- **SC-01**: no control audit seam. Confirmed product defect.
- **SC-09**: oldest-window read in the PI-12 context. Confirmed product defect.
- **SC-04/SC-12**: count-based per-interval history cap makes derived-series verification
  structurally incomplete. Confirmed mechanism, verification-design limitation.
- **SC-10**: 20m anchor derived from the page instead of the session. Latent correctness risk.
- **SC-07a**: per-bar synchronous SQLite commit on the event loop. Confirmed blocking mechanism.

### B.4 Harness- and acceptance-related

- **SC-08**: the earlier harness defects (invalid trigger, missing `acknowledge_paper_only`, wrong
  FastAPI startup hook) were fixed in the harness and are not product defects. Failed artefacts were
  preserved.
- **SC-11**: C1/C7 depend on a rare natural crossover; the criteria as written cannot be satisfied by
  engineering alone.
- **SC-14**: no model was trained in either isolated store, so the forecast path could not execute.
- **SC-05**: bounded morning runs could not observe the 15:30 IST close.

## C. Dependency map

```
SC-13 (duplicate-session guard)
   └─► SC-02 / C4  official BSE daily history (bhavcopy backfill)
          ├─► BSE ≥ 14 closes ─► BSE strategy can reach a real decision ─► C2
          └─► BSE ≥ 80 closes ─► BSE forecast model ─► PI-12 BUY/SELL ─► proposal

SC-14 (train/register models, NSE first)
   └─► PI-12 actionable decisions ─► proposal ─► NM-08 ─► NM-10 ─► C7

SC-09 (newest-window, single-interval context)
   └─► trustworthy PI-12 evidence ─► C1/C3 consumption claims

SC-01 (control audit seam)
   └─► C5 PASS (independent of market hours)

SC-04 / SC-12 (interval coverage)
   └─► C3 PASS requires either broader 5m acquisition or an explicit
       component-based verification statement

SC-10 (20m anchor hardening)
   └─► C3 correctness under non-session-aligned pages (independent)

SC-11 (criterion clarification, operator decision)
   └─► what C1 is allowed to mean when the strategy returns HOLD

C7 depends on ALL of: C1/C2 reaching an actionable natural signal, SC-14,
SC-09 (for honest decision evidence), and the full NM-08 → NM-10 → exit → outcome path.
```

## D. Ordered remediation plan

Each task is independently verifiable and leaves the system in a valid state. None of them authorizes
a market run.

**R-01 — Fix the PI-12 technical-context window (SC-09).** Smallest safe task.
Change `_forecast_bars` to request a single interval (the decision horizon's base interval; daily for
swing horizons) newest-first, then restore chronological order — mirroring
`strategies/engine.py:1230-1240`. Add one regression test modelled on
`test_novamarket_pi18.py:772` asserting the last element is the newest stored bar and that all
elements share one interval. No order path is touched; `require_technical_confirmation` remains false.

**R-02 — Add the control audit seam (SC-01).**
Add an append-only `autonomy_control_events` table (event id, at, actor, reason, from_mode, to_mode,
desired_mode, lease owner, lease valid, outcome, error) and write one row for every control attempt,
including refusals, in the same transaction as `save_control`. Preserve the singleton
`autonomy_runtime` row and C6's semantics. Optionally mirror the row to the NM-14 audit logger through
`NovaMarketService.attach_audit_logger`, but the durable table is the authority. Tests: pause, resume,
stop, refused resume, refused pause, restart persistence, ordering.

**R-03 — Guard against duplicate daily sessions (SC-13).**
Before appending an official bhavcopy bar, check whether the instrument already has a `1d` bar for
that session from any provider; skip and report it as `duplicate_session` instead of writing a second
row. Add a test that imports a bhavcopy session already covered by a Yahoo bar and asserts the
strategy's closes contains one close for that date.

**R-04 — Acquire official BSE daily history (SC-02, C4).**
Using the existing `import_bse_bhavcopy` (not a new collector), import the missing completed sessions
for `XBOM:RELIANCE` — at minimum 26 November 2026 — into the isolated BSE store, with a checkpoint and
per-session reporting. Verify the completed-session count reaches at least 14 and that 26 November is
present with `provider="bse_bhavcopy"`. This is a data-acquisition action against an isolated store;
it requires network access to BSE and operator authorization.

**R-05 — Make C3 verifiable (SC-04, SC-12).**
Two independent steps. (a) Extend the read-only checker to recompute OHLCV for **all** stored 20m bars
from their own `raw.components`, and report separately how many also have independently persisted 5m
rows. (b) Decide, with the operator, whether the acceptance requires independent 5m persistence for
every group (then acquire a 5m history that spans the 20m window, e.g. a bounded `ingest_history`
call) or accepts component-level reproducibility plus the 60-group independent cross-check. Record the
decision in the report rather than implying coverage.

**R-06 — Harden the 20m anchor (SC-10).**
Derive the bucket anchor from the exchange session open (calendar-driven) rather than the page
minimum, or refuse to aggregate a page whose first bar is not on the expected grid. Apply
prospectively: never rewrite stored bars. Tests: page starting at 09:20; page starting at 09:15;
session with a missing first 5m bar.

**R-07 — Remove the event-loop stall (SC-07).**
Batch the per-ingestion commit (one transaction per interval page instead of one per bar), and/or move
persistence to a dedicated thread with its own connection. Preserve `INSERT OR IGNORE` idempotency and
the conflict-detection result. Measure before/after with step-correlated lag samples; a claimed
improvement requires a measured comparison.

**R-08 — Bootstrap forecast models (SC-14).**
Train and register the supported forecast models in the isolated NSE store (248 daily bars exceed the
80-bar minimum), then confirm a stored forecast reaches PI-12. BSE waits for R-04. This is operational
configuration, not a code change.

**R-09 — Clarify C1/C7 acceptance (SC-11).**
Put the wording question to the operator: does C1 require a downstream trade, or does a complete,
correct decision (including HOLD/NO_SIGNAL with all upstream evidence) satisfy the decision-path
criterion, with the lifecycle reserved for C7? Do not change the status table until the operator
decides.

**R-10 — Re-run acceptance at a later session (SC-05).**
Observe a session close and the final completed daily bars; capture the 20m coverage and freshness
again. Requires explicit authorization and market hours.

## E. Acceptance test plan

| Criterion | Evidence required to move to PASS | Current gap | Type |
|---|---|---|---|
| **C1** | A completed real-market NSE cycle; a decision run whose evaluation records the exact closes available, the configured minimum and the resulting direction; every stage the inputs can reach executed with no skipped gate. If an actionable BUY/SELL occurs, its proposal, NM-08 decision and NM-10 outcome. Plus a correct `technical_context` (post R-01). | HOLD with no proposal; technical context stale (SC-09); no forecast model (SC-14) | Code (R-01), operational (R-08), criterion wording (R-09) |
| **C2** | The same for BSE, with ≥14 completed daily closes at decision time and an honest record of the resolved session set. | 8 closes (SC-02); 26 Nov absent | Data (R-03/R-04), then re-run |
| **C3** | For every stored 20m bar: components present, OHLCV verified, boundaries on the session grid, no partial flags, timestamps/timezone consistent. State explicitly how many groups were independently corroborated and how many only component-reproduced. Plus at least one consumer reading the verified interval. | 60/250 independent (SC-04); anchor not session-derived (SC-10); no consumer evidence (SC-09) | Verification (R-05), code (R-06, R-01) |
| **C4** | NSE: latest completed session present (met). BSE: ≥14 completed sessions including the most recent completed session, from an official or provider source, with no duplicate rows per session. | BSE 8 sessions; 26 Nov missing | Data (R-03/R-04) |
| **C5** | Durable, sequenced records for pause, resume and emergency stop, each with actor, timestamp, reason, from/to mode and outcome, surviving restart; a refused control also recorded. | None exist (SC-01) | Code (R-02) |
| **C6** | Preserve the existing probe evidence; re-run it unchanged after R-02 and any autonomy-schema change to prove no regression. | None (PASS) | Regression only |
| **C7** | A naturally eligible actionable signal that reaches NM-08 → NM-10 → position/ledger → exit → NM-08 exit → NM-10 exit → reconciliation → matured forecast outcome → advisory RL, with no duplicate order or fill. | No actionable signal; no model | Depends on C1/C2 + R-08; may remain open |

No status changes are proposed by this plan. C1 and C7 additionally require R-09's operator decision
before their wording can be applied.

## F. Risk assessment

| Change | Paper-only | NM-08 / NM-10 | Risk controls | Ledger | Exchange isolation | Restart / lease | Existing functionality |
|---|---|---|---|---|---|---|---|
| **R-01** forecast window | None | None — no order path | None | None | None | None | Changes PI-12 decision evidence; if `require_technical_confirmation` were enabled it would change behavior (it is false by default) |
| **R-02** control audit | None | None | None | None | None | **Medium** — schema addition and transactional write must not disturb `save_control`, `recover`, or the C6 probe | Adds a table and a write on every control; risk of a migration on existing isolated stores |
| **R-03** duplicate-session guard | None | None | None | None | None — BSE-only path, NSE bars untouched | None | Changes bhavcopy import outcome reporting; must not reject legitimate new sessions |
| **R-04** BSE bhavcopy import | None — official data, paper store | None | None | None (market data only) | Isolated to XBOM; never used as an NSE price | None | Depends on BSE availability; a missing file must be reported, not substituted |
| **R-05** verification | None | None | None | None | None | None | Read-only checker only; risk of overstating coverage if the two counts are merged |
| **R-06** 20m anchor | None | None | None | None | None | None | Changes future derived timestamps; must never rewrite stored bars or silently shift historical buckets |
| **R-07** persistence batching | None | None | None | None | None | **Medium** — a second thread must not share one SQLite connection without its own lock/connection | Idempotency and conflict detection must be preserved exactly; a batching bug could drop bars |
| **R-08** model bootstrap | None — forecasts are advisory | None — `forecast_authorizes_action=false` | None | None | Per-store; no cross-exchange training | None | Adds model rows; must not enable automatic promotion |
| **R-09** criterion clarification | None | None | None | None | None | None | Documentation only, but it changes what "PASS" means — requires explicit operator approval |

Additional risks:

- **SC-13 is a precondition, not an optional guard.** Importing bhavcopy first and deduplicating later
  would corrupt the SMA input with a duplicated session and could produce a false crossover.
- **Do not "fix" the fourteen-close guard.** It correctly failed closed. Lowering it, or switching the
  strategy, would weaken a safety boundary and violate the acceptance rules.
- **Do not reuse the NSE series for BSE** to reach 14 closes. The stores and instruments are separate
  for a reason.
- **R-02 must not make a control write depend on a network or audit sink.** A local append that fails
  must not block a pause or an emergency stop.

## G. Recommended execution order

1. **R-01** — smallest, read-side, test precedent exists, removes a live defect in decision evidence.
2. **R-02** — closes C5 without market hours; the only criterion that can move to PASS from a code
   change alone.
3. **R-03** — prerequisite for any BSE daily import; tiny and testable.
4. **R-04** — acquire the missing BSE sessions; verify 14+ closes and 26 November.
5. **R-05** — make the C3 statement precise and repeatable before the next acceptance run.
6. **R-06** — harden 20m boundaries; apply prospectively.
7. **R-07** — measure and remove the loop stall; no optimizations without a before/after measurement.
8. **R-08** — bootstrap NSE forecasts; BSE after R-04.
9. **R-09** — operator decision on C1/C7 wording.
10. **R-10** — authorized re-run across a full session, including the close.

The smallest safe next task is **R-01**. The highest-value next task is **R-02**, because it is the
only one that can convert a PARTIAL criterion to PASS without waiting for the market.

---

# Remediation matrix

| ID | Criterion | Confirmed root cause | Evidence | Affected files | Remediation | Tests required | Priority |
|---|---|---|---|---|---|---|---|
| R-01 | C1, C3 | **[CONFIRMED ROOT CAUSE]** `_forecast_bars` reads the oldest 60 bars across mixed intervals and publishes them as `technical_context` | `decision/pipeline.py:1230`; `marketdata/store.py:990-999`; NSE `last_close=1441.300048828125` = 2026-02-18 close; BSE `1337.300048828125` = 2026-11-11 20m close; store contains 1m/5m/15m/20m in the same window | `src/novacontrol/novamarket/decision/pipeline.py` | Read newest-first with one explicit interval, restore chronological order (mirror `strategies/engine.py:1230-1240`) | Newest-bar and single-interval regression test, modelled on `test_novamarket_pi18.py:772` | P0 |
| R-02 | C5 | **[CONFIRMED ROOT CAUSE]** Control operations have no audit or event seam; only a singleton `autonomy_runtime` row exists and refusals write nothing | `autonomy/runtime.py:198-220`, `autonomy/store.py:544`, `service.py:1340-1355`; `audit.jsonl` counts 0/0/0; no publisher or audit import in the runtime | `autonomy/runtime.py`, `autonomy/store.py`, `novamarket/service.py` | Append-only `autonomy_control_events` written with the state change; actor/reason/from/to/outcome; refusals included; optional audit mirror | Pause, resume, stop, refused resume, refused pause, restart persistence, ordering | P0 |
| R-03 | C4, C2 | **[CONFIRMED ROOT CAUSE — latent]** Unique key includes `provider`, and `bars()` does not dedupe, so bhavcopy plus Yahoo can yield two daily rows for one session | `market_bars UNIQUE(instrument_id, provider, interval, observed_at)`; `marketdata/store.py:962-999`; bhavcopy writes `provider="bse_bhavcopy"` | `marketdata/bse_bhavcopy.py`, `marketdata/store.py` (read policy) | Skip a bhavcopy session already covered by any provider; report `duplicate_session` | Import a covered session; assert one close per date and no second row | P0 |
| R-04 | C4, C2 | **[ENVIRONMENTAL LIMITATION]** Yahoo `.BO` returned 8 sessions and omitted completed 26 Nov; 24 Nov is a genuine holiday; need 14 closes (and 80 for forecasts) | `market_bars` 1d dates 13/16/17/18/19/20/23/25 Nov, one `retrieved_at`; `calendar.py:84`; `catalog.py:144` | `marketdata/bse_bhavcopy.py` (use), acceptance data action | Import official BSE bhavcopy for missing completed sessions, isolated store, checkpointed | Session count ≥14; 26 Nov present; provider/quality recorded; no substitution | P0 |
| R-05 | C3 | **[CONTRIBUTING FACTOR]** Per-interval bar-count cap gives 5m ≈3.3 sessions and 20m ≈13.3 sessions; only 60/250 groups overlap | `settings.py:44` (`history_limit=250`); `engine.py:1177`; 5m 2026-11-20→27 (251 rows), 20m 2026-11-05→27 (250 rows); 60/60 exact, 190 missing | `marketdata/engine.py`, `marketdata/settings.py`, read-only checker | (a) recompute all 250 from stored `raw.components`; (b) decide independent-coverage rule; optionally acquire a 5m history spanning the 20m window | Component-recompute test over every 20m row; explicit coverage counts; no gap fill | P1 |
| R-06 | C3 | **[HYPOTHESIS — NEEDS EVIDENCE]** 20m bucket anchor is the page minimum, so a page starting off-grid shifts every bucket | `providers/yahoo.py:476`; current data starts at 09:15 IST and last bucket 14:55 IST, so no observed failure | `marketdata/providers/yahoo.py` | Anchor on the exchange session grid or refuse an off-grid page; apply prospectively | Page starting 09:20; page starting 09:15; missing first 5m bar | P2 |
| R-07 | Performance | **[CONFIRMED ROOT CAUSE — mechanism]** One synchronous SQLite commit per bar on the event loop during ingestion | `marketdata/store.py:705-760` (`with self._lock, self._connection` per bar); no `to_thread` in marketdata; ~1,250 bars per burst; lag max 485.799 ms, p95 13.694 ms | `marketdata/store.py`, `marketdata/engine.py` | Batch per-page transaction or dedicated persistence thread with its own connection | Idempotency/conflict regression; step-correlated lag before/after | P2 |
| R-07b | Performance | **[HYPOTHESIS — NEEDS EVIDENCE]** The 485.799 ms spike is not correlated to any step | Probe records values only (`stage_c_acceptance_runner.py:204-214`); 410 samples; no concurrency or soak | Acceptance harness | Timestamp samples and record the step in flight | Correlated measurement under a justified load | P3 |
| R-08 | C1, C7 | **[NOT A DEFECT]** No model registered, so PI-12 correctly returns NO_ACTION | `prediction_models=0`, `prediction_forecasts=0`; requests 2/1 and cycles 2/1 exist; reason string recorded | Operational (isolated stores) | Train/register supported models; NSE first, BSE after R-04 | Stored forecast reaches PI-12; decision records forecast ids | P1 |
| R-09 | C1, C7 | **[CONFIRMED ROOT CAUSE — acceptance design]** C1/C7 require a downstream trade that depends on a rare 3/9 SMA crossover | NSE `HOLD`/`trend_positive` on 27 Nov; 248 daily closes; BSE 8 < 14; C7 rule in the preflight report | Acceptance documents only | Operator decides whether a complete HOLD/NO_SIGNAL decision satisfies C1, with C7 reserved for the lifecycle | Not a code test; a documented criterion decision | P1 |
| R-10 | C3, C4 | **[ENVIRONMENTAL LIMITATION]** Session close not observed; bounded morning runs | Runs 10:17-10:18 and 10:29-10:31 IST; no collector left running | Acceptance plan | Re-run across a full session | Close-time daily bar and final 20m behaviour captured | P2 |

## Findings that are explicitly not defects

| Finding | Classification | Note |
|---|---|---|
| NSE zero proposals | **NOT A DEFECT** | `HOLD` from a genuine 3/9 SMA state; non-actionable by design |
| BSE `NO_SIGNAL` on 8 closes | **NOT A DEFECT** (guard) | The guard failed closed correctly; the input was insufficient |
| 20m arithmetic | **NOT A DEFECT** | 60/60 exact; correct OHLCV rules; incomplete groups dropped |
| BSE 1m/5m staleness | **ENVIRONMENTAL LIMITATION** | Provider delay 900 s, correctly labelled; the daily strategy is unaffected |
| Harness trigger/ack/startup errors | **NOT A DEFECT** | Fixed in the harness; failed artefacts preserved; no product change |
| `signals` count includes `NO_SIGNAL` | **NOT A DEFECT** | Reporting semantics only; no gate affected |

---

## Status (restated, unchanged)

C1 PARTIAL · C2 PARTIAL · C3 PARTIAL · C4 PARTIAL · C5 PARTIAL · C6 PASS (subject to regression
preservation) · C7 NOT VERIFIED · Stage C OPEN · Stage D NOT STARTED.

## Evidence index

**Reports:** [execution journal](STAGE_C_EXECUTION_JOURNAL.md) · [NSE](STAGE_C_NSE_REAL_MARKET_REPORT.md) ·
[BSE](STAGE_C_BSE_REAL_MARKET_REPORT.md) · [decision path](STAGE_C_DECISION_PATH_ACCEPTANCE.md) ·
[timeframes](STAGE_C_TIMEFRAME_DATA_VALIDATION.md) · [runtime](STAGE_C_RUNTIME_RECOVERY_REPORT.md) ·
[performance](STAGE_C_PERFORMANCE_REPORT.md) · [defect register](STAGE_C_DEFECT_REGISTER.md) ·
[acceptance matrix](STAGE_C_ACCEPTANCE_MATRIX.md) · [closure](STAGE_C_CLOSURE_REPORT.md) ·
[preflight](STAGE_C_PREFLIGHT_REPORT.md) · [scope correction](STAGE_C_SCOPE_CORRECTION_REPORT.md) ·
[STATUS](STATUS.md)

**Machine evidence:** `tmp/stage_c_v2_2026_11_27/{xnse,xbom}/evidence/stage_c_acceptance_*_{xnse,xbom}.json`
and `stage_c_readonly_crosscheck.json`; `tmp/stage_c_v2_2026_11_27/xbom/lease_probe_runs/20261127T045900735591Z/`;
`tmp/stage_c_scope_correction_2026_11_26/evidence/bse_provider_probe.json`.

**Source anchors:** `decision/pipeline.py:1230,1753,1838` · `strategies/catalog.py:144` ·
`strategies/engine.py:1204-1260` · `marketdata/store.py:705-760,962-999` · `marketdata/engine.py:980-1000,1177` ·
`marketdata/settings.py:44` · `marketdata/providers/yahoo.py:476,460-540` · `marketdata/bse_bhavcopy.py` ·
`autonomy/runtime.py:198-220,400-465` · `autonomy/store.py:544` · `service.py:1340-1355,9629` ·
`novacontrol/audit/logger.py`.

**Tests cited:** `test_novamarket_marketdata.py:397,1015` · `test_novamarket_pi18.py:772` ·
`test_novamarket_workbench_scan.py:303,319,365` · `test_novamarket_pi12.py:730` ·
`test_novamarket_autonomy.py` · `test_bse_bhavcopy.py` · `test_novamarket_at04.py`.

---

*Diagnostic only. No fix was implemented. No NSE/BSE execution, proposal, order or migration was run.
No production source, test, evidence file, report or financial record was modified. Stage C remains
OPEN and Stage D has not started.*
