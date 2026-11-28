# Market Session 2026-11-23 — Supervised Paper Validation Report

**PAPER ONLY — NO LIVE TRADING — NO BROKER — NO AUTONOMOUS PROMOTION**

| Field | Value |
| --- | --- |
| Session date | Monday 2026-11-23 (verified 2026 calendar; next holiday 2026-11-24) |
| Exchange session | NSE/BSE 09:15–15:30 Asia/Kolkata |
| Capture window actually observed | **11:04:31 – 12:17:35 IST** |
| Observed duration | **73 min 4 s** (4 384.5 s), 50 cycles |
| Harness | `tmp/continuous_paper_validation/run_market_open.py` |
| Mode | `supervised_capture_only` — `--execute-paper` **not** used |
| Portfolio | `pf_41277535de675e34701d` (`short_term`) |
| Instruments | `XBOM:ADANIENT`, `XBOM:RELIANCE` |
| Provider | `yahoo` (unofficial delayed feed) |
| Result | **Completed, no trade, all reconciliations ok** |

Everything below is classified as **ACTUAL_MARKET_DATA** unless a section explicitly says otherwise. Labels are never mixed within a result.

---

## 0. Verdict

| Area | Result | Class |
| --- | --- | --- |
| Session eligibility & safety gate | Passed (`safe=true`, paper-only, no live routes) | ACTUAL_MARKET_DATA |
| Quotation continuity (50/50 cycles per instrument) | Passed, 0 missing quote cycles | ACTUAL_MARKET_DATA |
| Freshness honesty | Passed — labelled `fresh`/`delayed`, never `live` when stale | ACTUAL_MARKET_DATA |
| Intraday candle completeness | **Partial** — tail incomplete by ~15 min; 6 missing minutes on ADANIENT | ACTUAL_MARKET_DATA |
| Multi-timeframe intelligence | **Passed (abstention)** — 100/100 decisions `wait_for_confirmation` | ACTUAL_MARKET_DATA |
| Strategy signals | 50 BUY signals on ADANIENT; none on RELIANCE (insufficient daily history) | ACTUAL_MARKET_DATA |
| Proposal creation | 0 — every signal correctly blocked by MTF | ACTUAL_MARKET_DATA |
| NM-08 risk validation | **Not exercised today** (no proposal reached it) | — |
| NM-10 paper execution | **Not exercised today** (0 forwards) | — |
| Forecasting | **Not exercised today** — 0 forecasts created by this session | — |
| Portfolio / wallet / ledger | Balanced, reconciled, `difference = 0.00` | ACTUAL_MARKET_DATA |
| Database integrity | `all_ok = true` across 15 SQLite stores | ACTUAL_MARKET_DATA |
| Orders created | **0** (session portfolio) | ACTUAL_MARKET_DATA |
| Supervised execution recommended? | **No** — conditions not met (§10) | — |

**The single most important behavioural finding:** the system did not trade, and it did not pretend to. A BUY signal fired on every one of 50 cycles, and on every cycle the multi-timeframe layer refused to confirm it because the intraday tail was incomplete. That abstention is the correct behaviour and it propagated cleanly through to zero proposals.

---

## 1. Actual session start and end

| Event | Time (IST) | Time (UTC) | Evidence |
| --- | --- | --- | --- |
| Pre-flight isolation check | 10:57 | 05:27 | `preflight_check.py` output |
| `auto_start` seeded off in session DB | 11:04:29 | 05:34:29 | `SEEDED auto_start=False monitoring` |
| Capture started | **11:04:31** | 05:34:31 | `capture.started_at` |
| Last cycle written | 12:16:11 | 06:46:11 | cycle 50 `at` |
| Final reconciliation snapshot | 12:17:25 | 06:47:25 | `final.valuation.snapshot.at` |
| Process exit / summary written | 12:17:35 | 06:47:35 | `session_resources.json` |

**Coverage:** 73 minutes of a 375-minute session = **19.5 % of the trading day**. The capture therefore describes the **11:04–12:17 IST** slice only. It is a partial-day run and is not evidence of continuous operation or all-day behaviour.

The session began ~1 h 49 min after the 09:15 open because the pre-flight safety review found a harness/build incompatibility that had to be resolved first (§9).

---

## 2. Provider and data provenance

| Property | Observed |
| --- | --- |
| Provider | `yahoo` only (no fallback provider fired) |
| Instrument symbols | `ADANIENT.BO`, `RELIANCE.BO` (BSE) |
| Declared data kind | delayed, unofficial, ~900 s |
| **Measured quote delay (median)** | **932.5 s** (ADANIENT), **904.0 s** (RELIANCE) |
| Provider HTTP latency (3 samples each, direct) | RELIANCE 453 / 532 / 397 ms (median 453.4); ADANIENT 347 / 295 / 288 ms (median 294.8) |
| Provider name stored per quote/bar | `yahoo` |
| Provenance stored per observation | `observed_at`, `retrieved_at`, `source_timezone`, `quality`, `freshness`, `partial` |

The measured delay matches the feed's documented behaviour (~15 min) and is well inside the configured `spot_delayed_seconds = 900` grace. No observation was relabelled as real-time.

---

## 3. Instruments monitored

| Instrument | Exchange | Daily bars in store | 1m bars (today) | 5m bars (today) | Quote cycles |
| --- | --- | --- | --- | --- | --- |
| `XBOM:ADANIENT` | BSE | 258 (to 2026-11-19) | 148 rows / 124 minutes | 26 | 50/50 |
| `XBOM:RELIANCE` | BSE | **2** (2026-11-05, 11-06) | 155 rows / 130 minutes | 26 | 50/50 |

**Coverage defect:** `XBOM:RELIANCE` has only **two** daily bars. Its daily indicators (SMA 3/9) cannot be computed, which is why it produced no signal all session. `XNSE:RELIANCE` in the same store has 256 daily bars — so this is a **BSE-symbol backfill gap**, not a missing instrument. ADANIENT's daily series also stops at 2026-11-19 (two sessions behind).

---

## 4. Data freshness and completeness

**Quotes:** 50 observations per instrument, none missing.

| Instrument | Freshness labels | First price | Last price | Range |
| --- | --- | --- | --- | --- |
| ADANIENT | 48 `fresh`, 2 `delayed` | 2712.00 | 2705.60 | 2705.60 – 2720.95 |
| RELIANCE | 50 `fresh` | 1359.60 | 1356.35 | 1356.20 – 1362.35 |

**Intraday bars (today, 1-minute, vs. 09:15 open):**

| Instrument | Bars | Duplicate timestamps | Minutes expected / present | Missing minutes | Unaligned (non-`:00` seconds) | Zero-volume bars |
| --- | --- | --- | --- | --- | --- | --- |
| ADANIENT | 148 | **0** | 130 / 124 | **6** | 25 | 25 |
| RELIANCE | 155 | **0** | 130 / 130 | 0 | 26 | 26 |

- **No duplicate observations** (distinct timestamps = row count, both instruments).
- **No out-of-order storage** — reads are ordered by `observed_at`; the store is keyed on `(instrument, interval, observed_at)`.
- **Unaligned bars:** Yahoo injects snapshot bars at arbitrary seconds (e.g. `05:20:11Z`, `05:23:39Z`) with `open = high = low = close` and `volume = 0`. They are correctly flagged `partial`/invalid and excluded downstream. They are the direct cause of the "partial/invalid bar(s) excluded" reason in every MTF decision.
- **Tail incompleteness:** at the moment of each decision the newest 1m bar was ~15 minutes old (provider delay), so the current-session window was always short by ~15 minutes. This is the dominant cause of "missing 1m/5m candles inside the current session".
- **Re-ingest load:** each cycle inserted 6 bars and re-sent ~994 duplicate bars (475 per instrument across 1m/5m history). Harmless for correctness (deduplicated) but wasteful of provider calls.

---

## 5. Multi-timeframe analysis

100 decisions (50 per instrument, one per cycle), all **`wait_for_confirmation`**, direction `neutral`, confidence `0`.

| Instrument | `wait_for_confirmation` |
| --- | --- |
| XBOM:ADANIENT | 50 / 50 |
| XBOM:RELIANCE | 50 / 50 |

Missing-layer counts across decisions: `5m` 100, `1m` 90, `20m` 2.

Representative reasons (all cycles, abridged):

```
1–2 partial/invalid bar(s) excluded
7–9 incomplete 20m bucket(s) excluded
1–2 partial/invalid 5m bar(s) excluded
missing five-minute candle(s) inside the current session
missing one-minute candle(s) inside the current session
missing or unusable timeframe(s): 5m, 1m
```

**Assessment:** the layer behaved exactly as designed.
- It built 20m context from 5m bars and **rejected incomplete buckets** rather than filling them.
- It refused to treat a delayed or gap-tailed 1m/5m series as confirmation.
- It returned a neutral, zero-confidence decision — not a guess — and every downstream proposal was suppressed.
- No conflicting-timeframe signal was ever manufactured; where a layer was unusable it was declared missing, not assumed.

**Defect found (harness, not engine):** the harness's standalone diagnostics call sent `instrument_ids` as a repeated query parameter while the endpoint accepts one comma-separated string. FastAPI kept only the last value, so `/market/multi-timeframe/analysis` analysed **only RELIANCE** in every cycle, silently. The engine's own integration inside `/market/strategies/run` used the correct list and **did** evaluate both instruments. Fixed in the harness (§9).

---

## 6. Forecasting

**Today's session produced no forecasts.** The harness has no forecast stage; it calls ingest → quotes → MTF → strategy → (optional) forward. No forecast request was made, so no prediction was recorded, evaluated, or scored for 2026-11-23.

- Forecasts created with `created_at` on 2026-11-23: **0**
- Forecast outcomes stored: **0**
- Metrics (directional accuracy, MAE, RMSE, Brier) for today: **not computable, not reported, not estimated**
- Forecast abstention today: **total** (the stage was never invoked)

Records that *do* exist in the isolated store pre-date the session and are **HISTORICAL_REPLAY** relative to this report:

| forecast_id | Instrument | Created | Target | Horizon | Model |
| --- | --- | --- | --- | --- | --- |
| `fc_ca63b2e6…` | XNSE:ACCEPT | 2026-11-04 | 2026-11-09 | few_sessions | random_walk |
| `fc_19965a02…` | XNSE:ACCEPT | 2026-11-04 | 2026-11-09 | few_sessions | random_walk |
| `fc_25758aed…` | XNSE:ACCEPT | 2026-11-04 | 2026-11-09 | few_sessions | random_walk |
| `fc_bca1855e…` | XNSE:ACCEPT | 2026-11-04 | 2026-11-09 | few_sessions | random_walk |
| `fc_a58ae65b…` | XNSE:ACCEPT | 2026-11-04 | 2026-11-09 | few_sessions | random_walk |
| `fc_849ec265…` | XNSE:ACCEPT | 2026-11-04 | 2026-11-09 | few_sessions | random_walk |
| `fc_7886d9ef…` | XNSE:ACCEPT | 2026-11-21 | 2026-11-25 | few_sessions | random_walk |
| `fc_1908163f…` | XNSE:ACCEPT | 2026-11-21 | 2026-11-25 | few_sessions | random_walk |

Six of these have elapsed targets (2026-11-09) yet **zero stored outcomes** — evaluation has never run against them in this store. They were not scored by this validation either: that would require choosing an evaluation method the system itself did not use, and this report does not substitute its own methodology for the product's. Full export: `tmp/market_open_paper/forecast_evidence.json` (24 `prediction_forecasts` included, all historical).

---

## 7. Strategy decisions

| Decision | Count | Detail |
| --- | --- | --- |
| BUY signals | 50 | `XBOM:ADANIENT`, confidence **0.56**, `data_quality: measured` |
| SELL signals | 0 | — |
| HOLD / WAIT | 50 | Both instruments, MTF `wait_for_confirmation` |
| Proposals created | **0** | Every signal rejected at the timeframe gate |
| Forwarded to NM-10 | **0** | — |

`/market/strategies/run` returned HTTP 200 on all 50 cycles; `errors` was empty on all 50. Each response carried an explicit `timeframe.rejected` entry naming the signal and the exact MTF reason, e.g.:

```
signal sig_7639d2f3… (ADANIENT buy) → wait_for_confirmation:
  1 partial/invalid bar(s) excluded; 7 incomplete 20m bucket(s) excluded; …;
  missing or unusable timeframe(s): 5m, 1m
```

RELIANCE produced no signal because its daily history is 2 bars (§3). That is an honest no-signal outcome, not a suppressed one.

**Reason for zero proposals:** the strategy gate converts a signal into a proposal only when the timeframe check confirms it. With both instruments unconfirmed on every cycle, zero proposals is the correct result, and it is the reason NM-08/NM-10 were never reached.

---

## 8. Risk decisions

- **NM-08 was not exercised today** — 0 proposals reached it. No approval, rejection, sizing, liquidity or exposure decision occurred.
- `risk_policy_limits` *were* read at session start and stored in the capture (the harness deliberately captures the policy limits alongside proposals so a risk decision can be audited). Recorded limits are in `capture.risk_policy_limits`.
- The risk engine was never bypassed, disabled, or fed a hand-written proposal. No limit was altered.
- `risk_evidence` on every cycle: `proposal_count 0, captured_count 0, approved_count 0, non_approved_count 0, all_proposals_have_risk_decision true`.

For completeness, the store contains **one pre-session paper round trip** (2026-11-20, `ord_46536bb0…`, `XNSE:ACCEPT`, portfolio `pf_31198029410129e5f227`) with a full NM-08 decision id (`rdec_2cd985f3…`), NM-10 fill, slippage (0.0449 %), fees ₹0.78 and two ledger transaction ids. That is **HISTORICAL** evidence that the chain works; it is **not** today's session and is not counted as such.

---

## 9. Portfolio and ledger verification

Final state of the session portfolio (`pf_41277535de675e34701d`):

| Item | Value |
| --- | --- |
| Wallet | `pfw_41277535de675e34701d`, INR, active |
| NovaCoin balance | **100 000.00** |
| Available / reserved / total | 100 000.00 / 0.00 / 100 000.00 |
| Cash / invested / total value | 100 000.00 / 0.00 / 100 000.00 |
| Realized / unrealized / total P&L | 0.00 / 0.00 / 0.00 |
| Return / drawdown | 0.00 % / 0.00 % |
| Holdings / priced positions | 0 / 0 |
| Ledger entries / transactions | 1 / 1 (the ₹1,00,000 funding entry) |
| Orders / fills (this portfolio) | **0 / 0** |
| Portfolio reconcile | `ok: true`, funded 100 000.00, released 0.00, realized 0.00, **discrepancies: []** |
| Wallet reconcile | `ok: true`, stored 100 000.00 = ledger 100 000.00, **difference 0.00**, integrity `ok`, issues `[]` |
| NovaCoin reconcile | ledger balance 100 000.00, difference 0.00 |
| SQLite integrity | `all_ok: true` — 15 stores, 0 foreign-key violations |

**External deposits:** exactly one, the prepared ₹1,00,000 paper funding on 2026-11-21. No deposit or withdrawal occurred during the session.

**Global store check:** `execution_orders` still contains exactly **one** row, created 2026-11-20 for a *different* portfolio; `execution_fills` still contains exactly one. Nothing was created by today's run.

---

## 10. Paper-execution status and the supervised-execution decision

- Mode: **capture-only**. `--execute-paper` was never passed.
- Forwards: 0. Blocked forwards: 0. Fills: 0. Orders: 0. Warnings: [].
- No paper execution test was performed, so no NM-10 behaviour is claimed for today.

**Recommendation: remain capture-only. Do not run a supervised execution test on the next session either, until the following are fixed.** The gate conditions were not met:

| Condition for a supervised execution test | Status |
| --- | --- |
| Market data sufficiently fresh | **Partial** — quotes fresh, but intraday tail 15 min behind |
| Multi-timeframe data valid | **No** — 100/100 decisions unconfirmed |
| Portfolio state reconciled | **Yes** |
| Risk controls active | **Untested today** (no proposal reached NM-08) |
| Operator explicitly authorises | Not requested — conditions failed first |

There was also nothing to execute: with 0 proposals, an execution run would have produced the same empty result. An `--execute-paper` run today would have added risk without adding evidence.

---

## 11. Performance and resource measurements

Measured in-process by `tmp/continuous_paper_validation/run_supervised.py` (sampler) plus one OS spot sample.

| Metric | Value |
| --- | --- |
| Wall time | 4 384.5 s (73 min 4 s) |
| CPU time consumed | 1 162.77 s |
| **CPU utilisation** | **26.5 % of one core** |
| Cycle cadence (median / mean) | 73.2 s / 87.0 s |
| Worst cycle gap | 638.5 s (cycles 44→45) |
| Memory (mid-session OS spot sample) | **≈441 MB** working set (`tasklist`, PID 12416) |
| Provider latency | 295–453 ms median per chart request |
| DB errors | 0 |
| Application errors (5xx) | 0 |
| Ingest HTTP 400s | **5 of 50** — "an ingestion cycle is already running" |
| Provider attempt failures | 3 of 300 attempts (1 failed attempt each in cycles 24, 34, 45) |

**Memory note (honest limitation):** the in-process sampler's `GetProcessMemoryInfo` call returned 0 in its worker thread, so `session_resources.json` reports `peak_rss_mb: 0.0`. That value is invalid and is **not** used. The 441 MB figure is a single OS sample taken during the run; no peak or growth curve is claimed.

**Throughput interpretation:** 26.5 % of one core for a 60 s polling loop is dominated by the blocking ingest call, not by analysis. The five 400 collisions show the ingest sometimes exceeds the 60 s poll interval — the harness does not wait for or retry it.

---

## 12. Errors and recovery

| # | Event | Count | Handling | Assessment |
| --- | --- | --- | --- | --- |
| 1 | `POST /market/data/ingest` → **400 "an ingestion cycle is already running"** | 5 (cycles 14, 18, 36, 46, 48) | Cycle continued on already-stored data; no retry, no warning raised | **Defect** — the round silently uses older data; the capture does not flag it in `warnings` |
| 2 | Provider attempt failure (1 of 6 per affected cycle) | 3 (cycles 24, 34, 45) | Ingest still returned 200 with the other attempts; session continued | Correct degradation |
| 3 | 638 s stall between cycles 44 and 45 | 1 | Recovered on its own; next cycles normal | Provider/SQLite stall, unreported in the capture |
| 4 | ADANIENT quote frozen at 06:13:29Z late in the run | 2 cycles | Labelled `delayed` instead of `fresh` | Correct honesty |
| 5 | Harness MTF `instrument_ids` sent as a list | all 50 cycles | Only last instrument analysed in the diagnostics call | **Fixed** in harness |
| 6 | Harness safety gate required `release_gate == "closed"` | pre-flight | Aborted before any collection | **Fixed** — see below |
| 7 | Permanent runtime would auto-start in the session DB | pre-flight | Would have started `autonomous` and could have ordered | **Prevented** — `auto_start=false` seeded |

No 5xx, no crash, no reconciliation break, no integrity failure, no lost cycle. Nothing required a restart.

### Changes made (all outside `src/`, none to the autonomous runtime)

1. **`tmp/continuous_paper_validation/run_market_open.py` — safety gate.** Replaced the stale `autonomy_release_gate == "closed"` proxy with the direct paper-only invariants and added an explicit `live_orders_possible is False` check (stricter than before for live risk). `paper_only`, `live_trading`, `broker_connected`, `real_money_trading`, `live_execution_routes`, `market_trade_live_denied`, `execution_paper_only` are all still enforced.
2. **Same file — MTF call.** `instrument_ids` now sent comma-joined, so both instruments are analysed.
3. **`tmp/market_open_paper/preflight_check.py`** (new) — copies the prepared session, seeds `auto_start=false`, boots the app and prints the safety flags without ingesting or trading.
4. **`tmp/continuous_paper_validation/run_supervised.py`** (new) — capture-only supervisor that samples CPU/memory around the unmodified `run_session`.
5. **`tmp/market_open_paper/analyze_session.py`** (new) — read-only capture digest.

**Isolation preserved:** the session ran on a *copy* at `tmp/market_open_paper/open_session_2026-11-23/` (`data_dir`), never on `data/`. Every capture, event, resource and snapshot path is under `tmp/`, and the session's integrity report covered the copy (15 stores, `all_ok`).

**Production `data/` — precise statement.** The files were **not** written by this session: the production store contains **no** `XBOM:ADANIENT`/`XBOM:RELIANCE` bars dated 2026-11-23 and **no** order dated 2026-11-23. Its mtime did advance to 13:28 IST (07:58 UTC), i.e. **after** the capture ended at 12:17 IST, with `autonomy_runtime.last_reason = "service runtime stopped"` — an application start/stop outside this validation, not part of it. This report therefore does **not** claim production was untouched; it claims (and verified) that this capture did not write to it.

No source file under `src/` was modified by this task; no Git commit or push; no competing scheduler; the permanent runtime's autonomy row stayed `mode=disabled`, `runtime_state=stopped`, `auto_start=0/monitoring` for the whole session.

---

## 13. Evidence file locations

| Path | Contents |
| --- | --- |
| `tmp/market_open_paper/market_open_capture.json` | Final capture (50 cycles, final block) |
| `tmp/market_open_paper/market_open_capture_window1.json` | Immutable copy of the completed window |
| `tmp/market_open_paper/market_open_capture_window1_digest.json` | Computed digest (cadence, quotes, MTF, counts) |
| `tmp/market_open_paper/market_open_events.jsonl` (+ `_window1`) | Append-only per-cycle event stream |
| `tmp/market_open_paper/market_open_session.json` | Run summary + full SQLite integrity report |
| `tmp/market_open_paper/session_resources.json` / `.jsonl` | 379 resource samples + wall/CPU summary |
| `tmp/market_open_paper/forecast_evidence.json` | Historical forecast records and counts |
| `tmp/market_open_paper/snapshot_mid_session.sqlite3` | Consistent mid-session DB snapshot |
| `tmp/market_open_paper/open_session_2026-11-23/` | Isolated session data dir (all stores) |
| `tmp/continuous_paper_validation/market_open_session_2026-11-23.log` | Terminal log of the capture |
| `tmp/market_open_paper/market_open_readiness.json` | Pre-session readiness (2026-11-21) |

---

## 14. Remaining limitations

1. **Partial day only** — 19.5 % of the session, no coverage of the open, the close, or the afternoon.
2. **No NM-08/NM-10 evidence today** — zero proposals, so risk approval/rejection, sizing, liquidity and exposure limits are **untested** in this session.
3. **No forecasting evidence today** — the harness never invokes the forecast stage; the elapsed historical forecasts remain unevaluated.
4. **No supervised execution test** — deliberately not run.
5. **Intraday data quality is provider-limited** — ~15 min delay, injected zero-volume snapshot bars, and 6 missing ADANIENT minutes. MTF cannot confirm under these conditions, so the strategy path could not reach a proposal at all.
6. **BSE daily backfill gaps** — RELIANCE 2 daily bars, ADANIENT ending 2026-11-19.
7. **Peak memory unknown** — the sampler failed; only one 441 MB spot sample exists.
8. **Ingest collisions unhandled** — 5 cycles used pre-existing data without flagging it.
9. **Single session, single provider, two instruments** — no statistical claim about skill or profitability is possible or made.

---

## 15. Recommendation for the next market session

1. **Fix the data before the strategy.** Backfill `XBOM:RELIANCE` daily history to ≥ 30 sessions and refresh ADANIENT to the latest close. RELIANCE cannot produce a signal at all until this is done.
2. **Make the ingest loop self-aware.** Set the poll to exceed the ingest deadline (or have the harness wait/retry on the 400 "already running" response) and record a warning when a cycle reuses stale data. Today 5 cycles did so silently.
3. **Address the MTF tail.** Require either a completed-bar cut-off aligned to the provider delay, or a decision rule that evaluates the last *complete* minute instead of the wall-clock minute. As it stands, a 15-minute-delayed feed can never confirm a 1m layer, so no proposal can ever be produced with Yahoo alone. This is the single biggest blocker to exercising NM-08/NM-10 at all.
4. **Separate the diagnostics call from the decision call** — already fixed here; keep the comma-joined contract pinned by a test.
5. **Re-run capture-only first**, and only when a session produces at least one MTF-confirmed proposal, consider a supervised `--execute-paper` run with the operator present.
6. **Add a forecast stage to the harness** (or explicitly document that forecasting is out of its scope) so §4 of this validation can be answered with real, evaluated forecasts rather than an absence.
7. **Keep this harness capture-only until (1)–(3) are closed.** A supervised execution run today would have produced no orders and no additional evidence.

---

**Classification summary for the whole document**

- **ACTUAL_MARKET_DATA:** §1–§5, §7–§12 — quotations, bars, MTF decisions, signals, portfolio, ledger, resources, errors, all produced from live Yahoo observations on 2026-11-23.
- **HISTORICAL_REPLAY:** §6 forecast records (Nov 4 / Nov 21) and the §8 pre-session NM-10 round trip (Nov 20). Clearly separated and never counted as today's results.
- **SIMULATION:** none used.
- **FIXTURE:** none used.

**PAPER ONLY. No live order, broker connection, real-money execution, RL promotion, autonomous start, commit or push occurred.**
