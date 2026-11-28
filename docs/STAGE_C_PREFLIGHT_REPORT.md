# NovaMarket — Stage C Fresh Real-Market Acceptance V2 — Preflight Report

**Repository:** NovaControl / NovaMarket  
**Worktree:** `novamarket`  
**Branch:** `feature/novamarket`  
**HEAD:** `9f0aec8a0e5e21e9cb7d5b3472094d5ef313524d`  
**Preflight timestamp:** 26 November 2026, 20:57 IST / 15:27 UTC  
**Status:** **PREFLIGHT ACCEPTED — EXECUTION AUTHORIZED FOR 27 NOV 2026 (30 NOV FALLBACK) — SCOPE CORRECTED TO NSE + BSE — STAGE C NOT CLOSED**  
**Scope:** Preflight audit and isolated preparation, followed by the 26 November 2026 scope correction. **Stage C active scope: NSE + BSE (Indian markets only). NYSE is deferred until Indian-market acceptance succeeds.** No real-provider market-data ingestion, real-market acceptance cycle, decision cycle, paper order, exit or soak has been started.

> **PAPER ONLY.** This preflight found no live broker, no live-money execution path and no enabled automatic policy promotion. The execution engine reports `paper_only=true`, `live_trading=false` and `broker_connected=false`. All future Stage C orders, if any, must continue through NM-08 approval and NM-10 paper execution only.

> **SCOPE CORRECTION — 26 November 2026.** The operator narrowed the first genuine real-market
> acceptance to Indian exchanges. **Active Stage C scope: NSE + BSE (Indian markets only). NYSE is
> deferred until Indian-market acceptance succeeds.** The prepared `XNSE` environment is preserved
> unchanged; the prepared `XNYS` environment is now **DEFERRED — INTERNATIONAL EXPANSION**; a new
> isolated `XBOM` (BSE) environment has been prepared. Full details, generated IDs, provider evidence
> and preservation verification are in
> [`STAGE_C_SCOPE_CORRECTION_REPORT.md`](STAGE_C_SCOPE_CORRECTION_REPORT.md). Sections 2–9 and 11
> below retain the original NSE+NYSE preparation record as history; the acceptance matrix (§10),
> observation plan (§12) and next action (§13) carry the corrected active scope.

---

## 1. Authorization state

The setup and authorization gates are satisfied. Execution is waiting only for an open exchange session:

| Gate | Status |
|---|---|
| Preflight executed | **YES** — this report |
| Real providers configured for acceptance | **YES — isolated only** — the prepared environments have `data_providers=["yahoo"]` and one configured instrument each |
| Isolated acceptance configuration prepared | **YES** — wallet, paper portfolio and active strategy prepared for `XNSE` and `XBOM`; the `XNYS` environment is deferred; zero bars, monitoring cycles and orders |
| Operator authorization to begin Stage C ingestion and paper execution | **GRANTED — 26 November 2026**; 27 November sessions first, 30 November fallback if needed |
| Exchange open for genuine live-session evidence | **NO** — NSE closed for the day; NYSE closed for Thanksgiving |

The only provider request was a single authorized Yahoo liveness probe (`GET /market/data/providers?probe=true`). It fetched the provider's cheap daily chart probe and stored **no** bars or quotes. No ingestion, monitoring cycle, decision, proposal, NM-08 assessment, NM-10 order or exit was performed against real market data. The isolated runtime probe described in §6.2 used no provider and no universe, and submitted no order.

---

## 2. Worktree, baseline and preservation

| Check | Result |
|---|---|
| Branch | `feature/novamarket` |
| HEAD | `9f0aec8a0e5e21e9cb7d5b3472094d5ef313524d` |
| Dirty worktree entries | **162 at preflight start; 164 after adding this report and the STATUS entry** — existing entries preserved, not reset, cleaned, stashed or committed |
| `git diff --check` | exit 0 |
| Source/test files changed after the final 8,154-pass suite | **None** — only `.pyc` cache files were newer |
| User configuration `data/novamarket.json` | SHA-256 `8b4ff83e30807a6430bc9fb989f0839345e9b8a72068e265736475831f8964ec`; unchanged |
| User financial store `data/novamarket.sqlite3` | 66,560,000 bytes, last modified 20:21 IST; read-only inspection only |
| Commit / push / deploy | **None** |

The accepted baseline remains the one recorded in:

- `docs/FULL_SUITE_FAILURE_REMEDIATION_REPORT.md`
- `docs/POST_IMPLEMENTATION_STABILIZATION_AND_REGRESSION_REPORT.md`
- `docs/AUTONOMOUS_DECISION_PATH_REMEDIATION_REPORT.md`
- `docs/OFFLINE_INTEGRATION_VERIFICATION_REPORT.md`

That baseline is **8,154 passed / 0 failed / 3 intentional skips / 6,426 subtests**, 44/44 bounded groups exit 0, 215/215 test files covered exactly once. It is evidence for the current source tree; it is **not** a real-market result.

---

## 3. Provider availability and credentials

The build installs three adapters. “Installed” is not the same as “enabled” or “verified in this pass.”

| Provider | Installed | Declared data | Credential requirement | Current configuration | This preflight |
|---|---|---|---|---|---|
| Yahoo | Yes | Delayed unofficial OHLCV/quote; 1m, 5m, 15m, 20m (derived from 5m), 30m, 1h, 1d, 1wk, 1mo | None | Not enabled | **Not probed** — avoided an unauthorized provider request |
| CoinGecko | Yes | Crypto; declared 30m interval | None | Not enabled | Not probed; not relevant to XNSE/XNYS equities |
| Alpha Vantage | Yes | Daily OHLCV only | Required via `credentials_ref=env:NAME` or `store:NAME` | Not enabled; no credential reference | Not available without an operator key |

Current user configuration has:

- `data_providers = []`
- `provider_credentials = {}`
- `credentials_ref = ""`
- `universe = []`
- `ingestion.enabled = false`
- `ingestion.intervals = ["1d"]`
- `cache.provider_ttl_seconds = 60`
- `freshness.provider_delay_grace_seconds = 60`

The project `.env` contains only local NovaControl administration settings; it contains no provider key. No provider value was read or printed.

### Authorized isolated provider probe

After approval, one cheap liveness probe was run against the isolated XNSE environment. It made no market-history request:

| Check | Result |
|---|---|
| Endpoint | `GET /market/data/providers?probe=true` |
| HTTP status | **200** |
| Probe | `yahoo` **ok** — “chart endpoint returned a result for RELIANCE.NS” |
| Measured latency | **1,019 ms** |
| Other providers | Alpha Vantage and CoinGecko unprobed/unknown; not enabled for this run |
| Market data written | **Zero bars, zero quotes** |

**Verdict:** Yahoo is reachable and keyless. The isolated acceptance environments are configured with `data_providers=["yahoo"]` and one instrument each. No market history has been ingested and no acceptance observation exists yet.

Prior genuine-provider evidence from 23 November 2026 (Yahoo `RELIANCE.NS` and `AAPL`) is recorded in `docs/AUTONOMOUS_REAL_MARKET_ACCEPTANCE_AND_SOAK_REPORT.md`; it is historical evidence, not a fresh Stage C observation.

---

## 4. Instrument catalogue and allow-lists

Read-only inspection of the user’s financial store:

| Exchange | Registered instruments | Relevant observation |
|---|---:|---|
| `XNSE` | **2,409** | Includes real `XNSE:RELIANCE` with `provider_symbols={"yahoo":"RELIANCE.NS"}`; also includes `XNSE:ACCEPT`, which is fixture data and must never be used as market evidence |
| `XNYS` | **0** | No US instrument is registered in the user store |

Current configuration `universe` is empty, so the configured allow-list is empty. The current persisted monitoring exchange is `XNSE`. The user store must not be altered to add Stage C instruments or portfolios.

**Isolated seed written (prepared, not ingested):**

| Exchange | Canonical instrument | Provider mapping | Currency / timezone |
|---|---|---|---|
| XNSE | `XNSE:RELIANCE` | `{"yahoo":"RELIANCE.NS"}` | `INR` / `Asia/Kolkata` |
| XBOM | `XBOM:RELIANCE` | `{"yahoo":"RELIANCE.BO"}` | `INR` / `Asia/Kolkata` |
| XNYS | `XNYS:AAPL` | `{"yahoo":"AAPL"}` | `USD` / `America/New_York` |

The `XBOM` row was added on 26 November 2026 by the scope correction; the provider mapping was verified against Yahoo before registration. The `XNYS` row is preserved history and is now deferred.

The isolated configuration uses each exchange in a separate monitoring cycle and database. The prepared contexts are:

| Case | Portfolio | Strategy | Monitoring exchange | Autonomy |
|---|---|---|---|---|
| `xnse` | `pf_7ab7772f077b63516f76` | `strat_5325afdf7e1fa071` (`trend_sma`, active) | `XNSE` | `disabled`, `auto_start=false` |
| `xbom` | `pf_e0f549adee684a9f8a56` | `strat_6f94ba703c207203` (`trend_sma`, active) | `XBOM` | `disabled`, `auto_start=false` |
| `xnys` | `pf_56a70a4c1c0d8349b8b1` | `strat_d175e7d2f015e1b7` (`trend_sma`, active) | `XNYS` | `disabled`, `auto_start=false` — **deferred** |

All three prepared stores contain one instrument, zero market bars, zero monitoring cycles, zero decision runs, zero proposals and zero orders. The user store was not modified; `XNSE:ACCEPT` is explicitly excluded.

**Verdict:** isolated instrument/allow-list readiness **PASS (prepared)**. No XNYS instrument exists in the user store; the isolated `XNYS:AAPL` registration is the acceptance one.

---

## 5. Exchange calendars and session availability

The verified 2026 calendar was evaluated at the preflight timestamp:

| Exchange | Local time | State | Last completed session | Next open | Next close |
|---|---|---|---|---|---|
| XNSE | 2026-11-26 20:52 IST | `closed` | 2026-11-26 | **2026-11-27 09:15 IST** | 2026-11-27 15:30 IST |
| XNYS | 2026-11-26 10:22 ET | `holiday` — Thanksgiving Day | 2026-11-25 | **2026-11-27 09:30 ET** | **2026-11-27 13:00 ET** (early close) |

Sources in code: `marketdata/calendar.py`, NSE circular NSE/CMTR/71775 and NYSE Group 2026 calendar. The NSE holiday list includes 24 November 2026 but not 26 or 27 November 2026.

Consequences:

- **C1, C2, C4 and C7 cannot be observed now.** The exchanges are genuinely closed. Simulating an open session, backfilling a session, or calling the holiday a trading day is prohibited.
- The earliest available observation window is **Friday 27 November 2026**: NSE 09:15–15:30 IST and NYSE 09:30–13:00 ET (early close).
- The next full US session is **Monday 30 November 2026**. C7 may need more than one session because an eligible signal is not guaranteed and must never be manufactured.
- The current market-data scheduler has a 15-minute refresh floor (`MonitoringConfig.interval_seconds` minimum 900 s). It is not a one-minute execution loop.

---

## 6. Runtime, lease and stored autonomy state

### 6.1 User store (read-only)

| Item | Value |
|---|---|
| Persisted runtime | `mode=disabled`, `runtime_state=stopped`, `desired_mode=disabled` |
| Lease | not held; `lease_expires_at=null`; no active cycle |
| Last reason | `safe restart` |
| Autonomy config | `auto_start=1`, `auto_start_mode=autonomous`, `lease_seconds=60`, `max_cycle_seconds=30`, `paused_exit_policy=protective`, no portfolio ids |
| Monitoring config | enabled, exchange `XNSE`, interval 900 s, batch 20, retry 2, timeout 120 s |
| Recent monitoring cycles | deferred with `OUTSIDE_TRADING_SESSION`; last recorded cycle 26 November 2026 |
| Autonomy execution intents | **0** |
| Autonomy exit decisions / learning evidence | **0 / 0** |
| Historic paper execution | one filled paper order from 20 November 2026; no reconciliation record; not part of this acceptance |
| Current API listener | no backend on 8001/8011; a Vite page is listening on 5173 and was left untouched |

The user store’s `auto_start=1` and `auto_start_mode=autonomous` mean **starting the application against `data/` would auto-start the autonomous shell**. That is not permitted for Stage C. The isolated acceptance environment must use its own data directory and must be verified before the session opens.

### 6.2 Isolated runtime contract probe

A disposable `TestClient` startup was run against `tmp/stage_c_2026_11_27/data` with no provider, no universe and no instruments. It performed GET requests only.

Observed:

| Check | Result |
|---|---|
| `/health` | HTTP 200, `status=ok` |
| Runtime | `mode=autonomous`, `runtime_state=running` |
| Lease | held, valid, no foreign conflict; expires in 60 s |
| `paper_only` | `true` |
| `live_trading` | `false` |
| `live_orders_possible` | `false` |
| `broker_connected` | `false` |
| `automatic_policy_promotion` | `false` |
| Monitoring | `XNSE`, session `closed`, `MARKET_CLOSED`, scheduler active but deferred |
| Instruments / monitoring cycles / orders / fills | **0 / 0 / 0 / 0** |
| Shutdown | runtime `disabled`, `stopped`; lease released |
| NM-08 contract | `executes_orders=false`, `real_money=false` |
| NM-10 status/contract | `paper_only=true`, `live_trading=false`, `broker_connected=false`, `live_broker=false` |

This proves the fresh isolated runtime auto-starts the **paper-only** shell and respects the closed-session gate. It does **not** prove a real-provider cycle, an approved trade or an exit. The acceptance run must start from a fresh directory, not from this probe database.

---

## 7. NM-08, NM-10 and paper-only proof

### 7.1 NM-08 risk engine

- Contract: `executes_orders=false`, `real_money=false`.
- Guarantees include: a failed assessment is never an approval; insufficient data is not approved; a decision is bound to the exact proposal and portfolio state; a rejected decision cannot be applied or overridden; no order or broker exists in the engine.
- Position, exposure, drawdown, volatility, liquidity and data-age policies remain active; no threshold was changed in this pass.

### 7.2 NM-10 paper execution

- Status: `paper_only=true`, `live_trading=false`, `broker_connected=false`, `operational=true`.
- Contract: `paper_only=true`, `live_broker=false`, and explicit “never” rules: place real orders, contact a broker, create a fill without ledger-backed settlement, update a position before a fill.
- Accepts only a proposal with `status=approved`, an approved NM-08 outcome, a non-empty risk decision reference and an unexpired decision.
- Re-reads the NM-08 decision, requires matching proposal, action and portfolio risk-state version, and independently re-runs NM-08 against the actual notional before accepting.
- Duplicate-order protection is present: an existing order for the same proposal is replayed rather than creating a second order.
- Simulated cash/position reservations, fills and ledger settlement remain the only settlement path.

### 7.3 Autonomy and exits

- AT-04 requires `mode=autonomous` and a valid AT-01 lease; it refuses otherwise.
- AT-05 exit supervision is paper-only (`PAPER_ONLY=True`, `LIVE_TRADING=False`, `BROKER_CONNECTED=False`).
- `automatic_policy_promotion` is `False` in the autonomy status, learning/paper-feedback contracts and training contracts.
- `config.RESERVED_MODES["live"]` explicitly refuses live mode; `market.trade.live` is denied by the permission model.
- No live broker class or live order adapter was found in the NovaMarket source scan.

### 7.4 Operator controls and emergency stop

- `/market/autonomy/stop` calls `autonomy_stop`, which disables auto-start, releases the lease and sets the runtime to disabled. It does not bypass or cancel an order through a broker.
- `/market/execution/orders/{order_id}/cancel` exists for paper orders.
- `/market/autonomy/pause` retains the shell and lease; `/market/autonomy/resume` restores the stored `desired_mode` when no explicit mode is supplied.
- Autonomy mutations require the dedicated operator token, except that a local admin credential is accepted only for loopback requests. The project `.env` has no dedicated autonomy token; the acceptance process must set a separate ephemeral operator token and test both the authorized and refused paths. Values are never printed.

**Verdict:** paper-only, NM-08/NM-10 separation, duplicate-order protection and emergency-stop code paths are present and were confirmed by the isolated status probe. Their **live-session operational behavior**, pause/resume, restart and failure handling remain **NOT VERIFIED** until Stage C is authorized and executed.

---

## 8. 20-minute interval readiness

Code-level support is present:

- `DataInterval.MINUTE_20 = "20m"` with 1,200-second bounds.
- Yahoo requests 5-minute source bars and emits a 20m bar only when exactly four consecutive components `[0, 5, 10, 15]` are present.
- Aggregation preserves open, high, low, close, volume (only if all four volumes exist), currency, source timezone, source timestamps, `derived_from=5m`, `component_count=4` and partial state.
- Incomplete or gapped groups are skipped, not padded or fabricated.
- Storage, freshness, gateway schema, scheduler, forecast/feature/statistical paths and UI horizon lists include 20m.

**Verdict:** software support **PASS by code and regression tests**; real-provider 20m evidence is **NOT VERIFIED**. The acceptance run must request real 5m source bars, prove exact four-bar aggregation and prove an incomplete group is excluded.

---

## 9. Isolation and evidence boundary

The preflight designated:

```text
novamarket/tmp/stage_c_2026_11_27/
```

A disposable probe database was created under `tmp/stage_c_2026_11_27/data` and is **not** the acceptance run. The authorized environments are prepared separately:

```text
novamarket/tmp/stage_c_v2_2026_11_27/
  xnse/data/        # isolated config, SQLite stores and evidence (preserved)
  xbom/data/        # isolated config, SQLite stores and evidence (added by the scope correction)
  xnys/data/        # isolated, preserved and DEFERRED — INTERNATIONAL EXPANSION
```

All three environments currently have zero bars, zero monitoring cycles and zero orders. The earlier probe database is not reused. The original two-environment record is retained; the BSE addition and the NYSE deferral are recorded in [`STAGE_C_SCOPE_CORRECTION_REPORT.md`](STAGE_C_SCOPE_CORRECTION_REPORT.md).

Rules for the run:

- `data/` and all user stores are read-only and out of bounds for writes.
- Launch with `NOVACONTROL_SKIP_DOTENV=1` and explicit ephemeral API/autonomy credentials.
- Use `create_app(data_dir=...)` or a loopback server bound to `127.0.0.1` only.
- Keep provider, account and token values out of logs, documentation and evidence.
- Do not reset, clean, stash, commit, push or deploy the worktree.
- Keep the 162 pre-existing dirty entries intact.

**Verdict:** acceptance isolation boundary **PASS (designated and verified path)**. The preflight probe is retained as evidence only.

---

## 10. Preflight acceptance matrix

| # | Preflight requirement | Status | Evidence |
|---:|---|---|---|
| 1 | Inspect branch and working tree | **PASS** | `feature/novamarket`, HEAD `9f0aec8`, 162 dirty entries |
| 2 | Preserve modifications and uncommitted files | **PASS** | No reset/clean/stash/commit; working tree preserved |
| 3 | Read latest stabilization/remediation reports | **PASS** | Four reports listed in §2; Stage A/B evidence reviewed |
| 4 | Read Stage C/original real-market specifications | **PASS** | `AUTONOMOUS_REAL_MARKET_ACCEPTANCE_AND_SOAK_REPORT.md`, `PRE_MARKET_OFFLINE_VALIDATION_REPORT.md`, Stage A/B reports |
| 5 | Verify Stage A/B acceptance evidence | **PASS** | 377 passed / 2 skipped / 9 subtests; Stage B 1 passed; browser checks 39/39 |
| 6 | Verify 8,154 result corresponds to current worktree | **PASS** | No source/test changes after final run; only `.pyc` newer; HEAD unchanged |
| 7 | Verify provider configuration and credentials | **PASS (isolated)** | Yahoo enabled, keyless and probed successfully; no market history written |
| 8 | Verify calendars, instrument metadata and timezones | **PASS** | Verified XNSE/XNYS 2026 calendars; XNSE closed; XNYS holiday; timezone metadata read |
| 9 | Verify NM-08 and NM-10 active/configured | **PASS (code + isolated status)** | Risk/execution contracts and isolated status probe |
| 10 | Verify live broker path disabled | **PASS** | `live_trading=false`, `broker_connected=false`, `live_broker=false`; no broker class found |
| 11 | Verify paper execution is the only order path | **PASS** | NM-10 contract and source; no live adapter; permission model denies live |
| 12 | Verify emergency stop and cancellation | **PASS (code)** / **NOT VERIFIED (runtime)** | Stop disables auto-start/releases lease; cancel route exists; runtime test deferred |
| 13 | Verify configured XNSE/XNYS instruments and allow-lists | **PASS (isolated)** | Separate `XNSE:RELIANCE` and `XNYS:AAPL` stores, one-instrument universes, fixture excluded; user store untouched |
| 14 | Verify isolated test data directory | **PASS** | Dedicated `tmp/stage_c_2026_11_27`; clean run path designated; no `data/` writes |

Rows 8 and 13 record the original preflight check against `XNSE`/`XNYS`. The scope correction additionally prepared `XBOM:RELIANCE`; its identity and provider mapping are recorded in [`STAGE_C_SCOPE_CORRECTION_REPORT.md`](STAGE_C_SCOPE_CORRECTION_REPORT.md).

### Stage C acceptance criteria

All of C1–C7 are **NOT VERIFIED** because the authorized real-market acceptance run has not started. The exchanges were closed at preparation time, and the BSE environment was prepared after the scope correction. **A prepared environment is not acceptance evidence.**

| Criterion | Status | Reason |
|---|---|---|
| C1 — NSE (`XNSE`) real-market decision pipeline | **NOT VERIFIED** | Isolated environment prepared and preserved; no open session observed |
| C2 — BSE (`XBOM`) real-market decision pipeline | **NOT VERIFIED** | Isolated BSE environment prepared; BSE closed; no ingestion or cycle run |
| C3 — genuine 20m aggregation and consumption for the configured Indian instruments | **NOT VERIFIED** | Adapter and four-component aggregation verified from real provider history; no acceptance ingestion run |
| C4 — Indian exchange daily-bar ingestion and completed-session handling | **NOT VERIFIED** | Real daily bars are available from the provider; none ingested into an acceptance environment |
| C5 — pause/resume under runtime | **NOT VERIFIED** | Authorized; prepared runtime stopped; no live test yet |
| C6 — restart inside lease window | **NOT VERIFIED** | Authorized; isolated restart test not yet run |
| C7 — genuine eligible signal through paper execution | **NOT VERIFIED** | No real session or signal observed |
| Radar responsiveness under real ingestion | **NOT VERIFIED** | No real ingestion performed |
| Performance measurements | **NOT VERIFIED** | No real cycle performed |

> **Superseded target:** the original C2 and C4 rows named `XNYS` (NYSE decision pipeline; daily bars
> during an open US session). Those targets are deferred by the scope correction and no XNYS
> acceptance was run. The corrected C1–C7 definitions and the BSE preparation evidence are in
> [`STAGE_C_SCOPE_CORRECTION_REPORT.md`](STAGE_C_SCOPE_CORRECTION_REPORT.md).

---

## 11. Blockers and conditions for authorization

1. **Operator authorization is granted** for the 27 November sessions, with 30 November as the fallback. No further preflight approval is required.
2. **Provider and universe configuration is present in the isolated environments.** The user configuration remains intentionally empty and is not used for Stage C.
3. **Both Indian exchanges are closed now.** The first available session is 27 November 2026; C1–C4 and C7 cannot be observed before then. C7 may remain open beyond one session. (At the original preflight `XNYS` was also closed for Thanksgiving; it is now deferred.)
4. **The user store has autonomous auto-start enabled.** The application must not be launched against `data/` for Stage C; use the isolated data directories only.
5. **No dedicated autonomy operator token is currently present in `.env`.** The isolated preparation used a separate ephemeral API token and operator token; the run must do the same and verify the refusal path without the operator token.
6. **Yahoo is reachable but its session-time behavior is unverified.** The XNSE liveness probe and the read-only XBOM mapping/history probe succeeded on 26 November; if a session request fails, record the provider failure and do not substitute fixtures.
7. **C7 has no guaranteed signal.** A quiet session must end with C7 **NOT VERIFIED**, not with a manufactured candidate or weakened threshold.

---

## 12. Authorized observation plan

### Earliest window — Friday 27 November 2026

| Exchange | Window (exchange local) | Planned checks |
|---|---|---|
| XNSE | 09:15–15:30 IST | C1 monitoring/decision path, C3 20m ingestion, C7 if a natural signal appears |
| XBOM | 09:15–15:30 IST | C2 monitoring/decision path, C4 daily bars, C3 20m, C7 if a natural signal appears |

Both Indian exchanges must be run as separate configured cycles with separate allow-lists. No cross-exchange candidate may enter either cycle; `XNSE:RELIANCE` and `XBOM:RELIANCE` are distinct canonical identities.

**Deferred:** `XNYS` (NYSE) is outside the active scope. Do not run its ingestion, monitoring or acceptance. It stays prepared and stopped until NSE + BSE acceptance succeeds.

### Fallback / extension window — Monday 30 November 2026

Use the next available NSE and BSE sessions to complete C1–C4 and C7 and collect failure/performance evidence if one Indian session is insufficient. C5/C6 can be run against the isolated runtime in either window; C6 must use an isolated lease store and must never manipulate production lease records. `XNYS` is not part of this plan.

### Required evidence for every real observation

Provider, instrument, canonical MIC, symbol, exchange, instrument id, exchange timezone, provider timestamp, received timestamp, interval, OHLCV, currency, source provenance, freshness status and validation result. Cached observations must be labelled cached and must still pass freshness rules. Fixture and synthetic data remain ineligible.

### C7 rule

C7 closes only on a naturally eligible signal that reaches the complete NM-08 → NM-10 → portfolio/ledger → exit → reconciliation → forecast outcome → advisory RL path. If no signal occurs, the report records the observation window and leaves C7 **NOT VERIFIED**. There will be no threshold change, synthetic signal or fixture substitution.

---

## 13. Exact next permissible action

The isolated Indian environments are prepared and stopped with `auto_start=false`. The next permissible action is for the operator to review the scope-correction report and explicitly begin the revised NSE + BSE run. When authorized, at the 27 November 2026 Indian open, start the prepared XNSE environment, explicitly start autonomous paper mode, ingest the authorized real observations and run C1/C3; run the prepared XBOM environment as its own configured cycle for C2/C4/C3. C5/C6 use the same isolated runtime; C7 only if a natural signal occurs. `XNYS` remains deferred and must not be started.

No Stage D, live trading, broker connection, RL promotion, commit, push or deployment is authorized by this report. Stage C cannot close until every applicable C1–C7 requirement has direct genuine-provider or real-application evidence, and no criterion remains PARTIAL, FAIL, BLOCKED or NOT VERIFIED.

---

## 14. Evidence index

| Artifact | Purpose |
|---|---|
| `tmp/stage_c_2026_11_27/preflight_runtime.py` | Disposable GET-only isolated runtime contract probe |
| `tmp/stage_c_2026_11_27/preflight_runtime.json` | Probe output; no provider/universe/order present |
| `tmp/stage_c_2026_11_27/data/` | Disposable preflight probe stores; not the acceptance run |
| `tmp/stage_c_v2_2026_11_27/prepare.py` | Offline preparation of the two isolated paper environments; no ingestion or orders |
| `tmp/stage_c_v2_2026_11_27/prepared_summary.json` | IDs, monitoring settings, safety flags and zero-activity counts |
| `tmp/stage_c_v2_2026_11_27/xnse/evidence/provider_probe.json` | Authorized Yahoo liveness probe; no bars or quotes written |
| `tmp/stage_c_v2_2026_11_27/xnse/`, `.../xbom/`, `.../xnys/` | Separate isolated acceptance environments; zero bars/cycles/orders. `xnys` is **deferred**; `xbom` was added by the scope correction |
| `tmp/stage_c_scope_correction_2026_11_26/` | BSE provider probe, BSE-only preparation and read-only verification evidence |
| `tmp/stage_c_scope_correction_2026_11_26/evidence/bse_provider_probe.json` | Genuine Yahoo BSE identity, quote, 1d/5m/20m bars, freshness and unavailable-symbol error path |
| `tmp/stage_c_scope_correction_2026_11_26/evidence/scope_correction_verification.json` | NSE preservation, BSE readiness, NYSE deferral, safety and user-data checks |
| `docs/STAGE_C_SCOPE_CORRECTION_REPORT.md` | Scope correction report with generated BSE IDs and the corrected C1–C7 scope |
| `data/novamarket.json` | User config, read-only; SHA-256 recorded in §2 |
| `data/novamarket.sqlite3` | User store, read-only; not modified |
| `docs/FULL_SUITE_FAILURE_REMEDIATION_REPORT.md` | Baseline full-suite evidence |
| `docs/POST_IMPLEMENTATION_STABILIZATION_AND_REGRESSION_REPORT.md` | Stabilization evidence |
| `docs/AUTONOMOUS_DECISION_PATH_REMEDIATION_REPORT.md` | D1–D9 remediation and Stage A/B evidence |
| `docs/OFFLINE_INTEGRATION_VERIFICATION_REPORT.md` | Stage A/B browser/fixture evidence |
| `docs/AUTONOMOUS_REAL_MARKET_ACCEPTANCE_AND_SOAK_REPORT.md` | Original real-market acceptance and historical defects |
| `docs/PRE_MARKET_OFFLINE_VALIDATION_REPORT.md` | Offline validation boundaries |
