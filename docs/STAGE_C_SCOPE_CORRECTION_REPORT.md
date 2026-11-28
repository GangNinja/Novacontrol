# NovaMarket — Stage C Scope Correction — NSE + BSE (Indian Markets Only)

**Repository:** NovaControl / NovaMarket  
**Worktree:** `novamarket`  
**Branch:** `feature/novamarket`  
**HEAD:** `9f0aec8a0e5e21e9cb7d5b3472094d5ef313524d`  
**Report timestamp:** 26 November 2026, 21:50 IST / 16:20 UTC  
**Status:** **BSE ENVIRONMENT PREPARED AND VERIFIED — NSE PRESERVED — NYSE DEFERRED — STAGE C NOT CLOSED**  
**Active Stage C scope:** **NSE + BSE (Indian markets only). NYSE is deferred until Indian-market acceptance succeeds.**

> **PAPER ONLY.** This correction created only isolated paper environments. No real-market ingestion,
> monitoring cycle, decision, proposal, NM-08 assessment, NM-10 order, exit or reconciliation was run.
> `paper_only=true`, `live_trading=false`, `broker_connected=false` and
> `automatic_policy_promotion=false` remain in force in all three prepared environments.

---

## 1. Why this correction exists

The operator narrowed the first genuine real-market acceptance to Indian exchanges:

1. NSE — `XNSE`
2. BSE — `XBOM`

The previously prepared NYSE (`XNYS`) environment is **deferred until NSE + BSE acceptance succeeds**.
This is a scope correction to an already-prepared environment, not a new implementation phase. No
production source behavior was changed.

| Item | Result |
|---|---|
| Production source files changed | **None** |
| Test files changed | **None** |
| New acceptance environment | `tmp/stage_c_v2_2026_11_27/xbom` |
| NSE environment | Preserved exactly; IDs, capital, runtime and data unchanged |
| NYSE environment | Preserved exactly; marked deferred; no ingestion or acceptance run |
| User configuration / financial store | Unchanged |
| Commit / push / deploy | **None** |
| Stage C closed | **No** |
| Any C1–C7 criterion passed by this report | **No** — preparation only |

### Status vocabulary used here

| Term | Meaning in this report |
|---|---|
| **PREPARED** | The isolated environment exists with the configured instrument, paper portfolio, strategy, monitoring exchange and stopped runtime. It carries no market evidence. |
| **VERIFIED** | A specific check was performed and its evidence is recorded. It does **not** mean a Stage C acceptance criterion passed. |
| **DEFERRED** | Intentionally outside the active scope; preserved unchanged and not executed. |
| **NOT VERIFIED** | No genuine session evidence exists yet. |
| **BLOCKED** | A required capability or safety condition could not be satisfied. |

---

## 2. Preservation of the previously prepared environments

Both pre-existing environments were read for comparison only. They were never opened as an
application data directory during this correction, so no startup, lease, monitoring tick or scheduler
ran against them.

### 2.1 NSE — `XNSE` — PREPARED — PRESERVED

| Item | Recorded value |
|---|---|
| Instrument | `XNSE:RELIANCE` (NSE / `XNSE` / INR / `Asia/Kolkata`) |
| Yahoo mapping | `RELIANCE.NS` |
| Portfolio | `pf_7ab7772f077b63516f76` |
| Portfolio wallet | `pfw_7ab7772f077b63516f76` |
| Funding wallet | `wal_9d2ecabc836b4a1c92f92c792b199aaa` |
| Strategy | `strat_5325afdf7e1fa071` (`trend_sma`, active) |
| Paper capital | `INR 100,000.00`, one `funding` transaction |
| Monitoring exchange | `XNSE`, enabled, 900 s interval |
| Runtime | `mode=disabled`, `runtime_state=stopped`, `desired_mode=disabled` |
| Auto-start | `false` (stored `auto_start=0`), no lease token |
| Market bars / observations | `0 / 0` |
| Monitoring cycles / decision runs | `0 / 0` |
| Prediction decisions / proposals | `0 / 0` |
| Orders / fills / execution intents / exit decisions | `0 / 0 / 0 / 0` |
| Positions | `0` |
| `novamarket.json` SHA-256 | `792a3793d6e8f4af8fbb10bd1ea96159ecdced84e9367ef0d1b80afbe52cccd8` |
| `novamarket.sqlite3` SHA-256 | `904a8d9edcd4dc124c815899b1c3a64893cfb364e0fa87ae45984cf8b55e1b25` |
| SQLite file size | 1,601,536 bytes |

Both hashes were taken before and after the BSE preparation and are identical. Portfolio ID,
strategy ID, capital, monitoring exchange, runtime state and zero-activity counts are unchanged.

### 2.2 NYSE — `XNYS` — DEFERRED — INTERNATIONAL EXPANSION

| Item | Recorded value |
|---|---|
| Instrument | `XNYS:AAPL` (NYSE / `XNYS` / USD / `America/New_York`) |
| Yahoo mapping | `AAPL` |
| Portfolio | `pf_56a70a4c1c0d8349b8b1` |
| Portfolio wallet | `pfw_56a70a4c1c0d8349b8b1` |
| Funding wallet | `wal_3744f1fa62724c7683e8164744092dbe` |
| Strategy | `strat_d175e7d2f015e1b7` (`trend_sma`, active) |
| Paper capital | `INR 100,000.00`, one `funding` transaction |
| Monitoring exchange | `XNYS`, enabled, 900 s interval |
| Runtime | `mode=disabled`, `runtime_state=stopped`, `desired_mode=disabled` |
| Auto-start | `false` (stored `auto_start=0`), no lease token |
| Market bars / observations | `0 / 0` |
| Monitoring cycles / decision runs | `0 / 0` |
| Prediction decisions / proposals | `0 / 0` |
| Orders / fills / execution intents / exit decisions | `0 / 0 / 0 / 0` |
| Positions | `0` |
| `novamarket.json` SHA-256 | `79a6b82a7c095a2f5aff57dd6a9286f06bd157ad7c583c8857a31a910892bd59` |
| `novamarket.sqlite3` SHA-256 | `1086dafeb8c21427aace035fc27a182335b941faed6b083acf3cdf6f0a49034a` |
| SQLite file size | 1,601,536 bytes |

**Deferral reason:** the operator's first real-market acceptance covers Indian exchanges only.
**Resume condition:** resume only after NSE + BSE Stage C acceptance succeeds. Until then, no XNYS
ingestion, monitoring cycle, decision or acceptance run may be started. Its preparation evidence
(`xnys/evidence/prepared.json`) is retained unchanged.

The XNYS portfolio and strategy IDs are **not** reused by BSE; the BSE environment has its own
independently generated wallet, portfolio and strategy.

---

## 3. Newly prepared BSE environment — `XBOM`

Created through the application's own supported provisioning routes (`POST /market/instruments`,
`/market/wallets`, `/market/wallets/{id}/deposit`, `/market/portfolios`, `/market/strategies`,
`/market/strategies/{id}/activate`, `/market/autonomy/monitoring/config`,
`/market/autonomy/stop`) against a fresh data directory. No store was copied from NSE or NYSE.

| Item | Generated value |
|---|---|
| Data directory | `tmp/stage_c_v2_2026_11_27/xbom/data` |
| Evidence directory | `tmp/stage_c_v2_2026_11_27/xbom/evidence` |
| Exchange / MIC | `BSE` / `XBOM` |
| Canonical instrument | **`XBOM:RELIANCE`** |
| Instrument name | `Reliance Industries Ltd` |
| Currency / timezone | `INR` / `Asia/Kolkata` |
| Yahoo mapping | **`RELIANCE.BO`** (verified against the provider, §4) |
| Wallet | `wal_d5ae36b0c8b548d4b453b3bb7960f769` (owner `stage-c-xbom`) |
| Portfolio | **`pf_e0f549adee684a9f8a56`** |
| Portfolio wallet | `pfw_e0f549adee684a9f8a56` |
| Funding transaction | `txn_7cab087f7e92440d85e65897d3846369`, `funding 100000.00` |
| Paper capital | `INR 100,000.00` simulated only |
| Strategy | **`strat_6f94ba703c207203`** (`trend_sma`, `nm09-trend_sma-1.0`, active) |
| Strategy config | fast 3, slow 9, horizon 5 days, max data age 72 h |
| Horizons | `short_term` |
| Monitoring | enabled, exchange `XBOM`, interval 900 s, batch 5, retry 1, timeout 120 s |
| Runtime | `mode=disabled`, `desired_mode=disabled`, `runtime_state=stopped` |
| Auto-start | `false` (stored `auto_start=0`); no lease token; no active cycle |
| Last reason | `Stage C scope correction: leave stopped until authorized Indian-market session` |
| `novamarket.json` SHA-256 | `d001d13863bafb258248b834347a365f87726f1e6306443e5a8d3b1842ed2e4f` |
| `novamarket.sqlite3` SHA-256 | `29824313e6bb62ede0ed75614840c796bb58f672d988974f760fd1408ac61108` |
| SQLite file size | 1,601,536 bytes |

### 3.1 Initial acceptance state

| Table | Count |
|---|---:|
| `market_bars` | 0 |
| `market_observations` | 0 |
| `autonomy_monitoring_cycles` | 0 |
| `decision_runs` | 0 |
| `prediction_decisions` | 0 |
| `decision_proposals` | 0 |
| `execution_orders` | 0 |
| `execution_fills` | 0 |
| `autonomy_execution_intents` | 0 |
| `autonomy_exit_decisions` | 0 |
| `portfolio_positions` | 0 |

The provider probe in §4 ran in a separate process with **no application and no store**, so none of
its observations, bars or quotes entered this environment. The environment has no fabricated
observation and no market evidence of any kind.

### 3.2 Identity separation

`XBOM:RELIANCE` and `XNSE:RELIANCE` are two distinct canonical identities with different MICs and
different provider symbols (`RELIANCE.BO` vs `RELIANCE.NS`). The repository's model explicitly
forbids collapsing them. The three environments use three distinct portfolios and three distinct
strategies:

| Case | Portfolio | Strategy |
|---|---|---|
| `xnse` | `pf_7ab7772f077b63516f76` | `strat_5325afdf7e1fa071` |
| `xbom` | `pf_e0f549adee684a9f8a56` | `strat_6f94ba703c207203` |
| `xnys` (deferred) | `pf_56a70a4c1c0d8349b8b1` | `strat_d175e7d2f015e1b7` |

---

## 4. Provider support and mapping evidence

The configured provider is Yahoo. BSE support is a first-class part of the existing adapter, not a
new code path:

- `marketdata/models.py` maps display name `BSE` → MIC `XBOM`.
- `marketdata/calendar.py` defines a dated `XBOM` calendar (09:15–15:30 `Asia/Kolkata`, the same
  2026 equity holiday list as NSE, coverage 2026).
- `marketdata/providers/yahoo.py` declares `XBOM` in its exchange capabilities and strips the `.BO`
  suffix when deriving the canonical symbol for that MIC.
- The provider symbol is data (`instrument.provider_symbols["yahoo"]`), never a provider-specific
  branch in the engine.

### 4.1 Genuine provider observation

Probe: `tmp/stage_c_scope_correction_2026_11_26/probe_bse_provider.py`  
Evidence: `tmp/stage_c_scope_correction_2026_11_26/evidence/bse_provider_probe.json`

The probe builds no application and opens no store. It made six bounded requests (lookup, quote,
1d, 5m and 20m history, one unavailable symbol) at ~21:45 IST on 26 November 2026.

**Provider lookup of `RELIANCE.BO`:**

| Field | Provider's answer |
|---|---|
| `provider_symbol` | `RELIANCE.BO` |
| Canonical symbol | `RELIANCE` |
| Name | `Reliance Industries Limited` |
| Asset class | `equity` |
| Exchange / MIC | `BSE` / `XBOM` |
| Currency | `INR` |
| Timezone | `Asia/Kolkata` |
| Instrument type | `EQUITY` |
| Yahoo `exchangeName` | `BSE` |

All seven identity checks passed: MIC is `XBOM`, canonical symbol is `RELIANCE`, currency is `INR`,
timezone is `Asia/Kolkata`, the exchange names BSE, the canonical id is `XBOM:RELIANCE`, and the
mapping carries the `.BO` suffix.

**Spot quote:**

| Field | Value |
|---|---|
| Price | `1396.90` |
| Currency | `INR` |
| Provider timestamp (`regularMarketTime`) | `2026-11-26T10:29:43Z` |
| Retrieved | `2026-11-26T16:15:46Z` |
| Previous close | `1370.00` |
| Day high / low | `1401.40` / `1361.55` |
| Day volume | `2,277,177` |
| Change | `+1.77%` |
| Provider `marketState` | empty in the response |

The provider timestamp is recorded exactly as returned. It is **29 m 43 s after the 15:30 IST close**
and is not claimed as a live quote. The repository's freshness classifier therefore reports:

```json
{"freshness": "closed_market", "live": false,
 "reasons": ["closed: this is the last completed session's price, not a live quote"],
 "market_state": "closed", "calendar_coverage": true,
 "expected_delay_seconds": 900, "last_completed_session": "2026-11-26"}
```

**OHLCV history (real provider bars, not stored anywhere):**

| Interval | Bars returned | Range (UTC) | Currency | Last bar OHLCV |
|---|---:|---|---|---|
| `1d` | 9 | 2026-11-13T03:45Z → 2026-11-26T03:45Z | INR | O 1370.00 / H 1401.40 / L 1361.55 / C 1396.90; V 2,277,177 |
| `5m` | 30 | 2026-11-26T07:25Z → 2026-11-26T10:00Z | INR | O/H/L/C 1396.90; V 0 |
| `20m` | 10 | 2026-11-26T06:25Z → 2026-11-26T09:25Z | INR | O 1397.00 / H 1401.40 / L 1393.55 / C 1396.95; V 155,785 |

OHLC values are shown at two decimals for readability; the provider's own float precision is retained
in the recorded evidence. The **20m** series is an exact aggregate of four consecutive 5m bars. The last aggregate records
`derived_from=5m`, `component_count=4` and component timestamps
`09:25Z / 09:30Z / 09:35Z / 09:40Z`. No native 20m response is claimed, and no incomplete group was
padded: the 5m series ends at `10:00Z` (15:30 IST) while the last 20m aggregate starts at `09:25Z`,
because a group is emitted only when all four components are present.

Daily and intraday bars classify as `closed_market` (last completed session `2026-11-26`, coverage
true) rather than stale or live. Their structural quality is `valid` and `usable=true`; `live=false`
is the honest verdict while the exchange is closed.

**Calendar at probe time:**

| Field | Value |
|---|---|
| MIC | `XBOM` (BSE Ltd.) |
| State | `closed` |
| Coverage | `true` (2026) |
| Last completed session | `2026-11-26` |
| Next open | `2026-11-27T09:15:00+05:30` |
| Session close | `15:30:00` |
| Holiday source | Same 2026 equity-market holiday list as NSE |

**Unavailable-data handling:** a request for a non-existent BSE scrip raised
`ProviderSymbolNotFoundError` ("the provider did not find the requested symbol"). No bar, quote or
placeholder observation was produced. The adapter also skips rows with any missing OHLC component
rather than filling them.

### 4.2 Provider limitations that the acceptance run must respect

| Limitation | Consequence for Stage C |
|---|---|
| Unofficial, free, delayed Yahoo chart endpoint (declared typical delay 900 s) | Observations are delayed, not a licensed real-time feed. Freshness must be recorded per observation. |
| Intraday history windows: 1m → 7 days; 5m/15m/20m/30m → 60 days | The 27 November session must be observed while it is genuinely open; history cannot be back-dated into a session. |
| 20m is derived from 5m | C3 must prove the four-component aggregation and the exclusion of incomplete groups. |
| Endpoint can change or restrict without notice | A refused request must be recorded as a provider failure; no fixture may replace it. |
| Provider `marketState` may be empty | Session state must come from the repository calendar, not from the provider field. |

**Verdict:** BSE provider connectivity, instrument identity, exchange mapping, currency, timestamp,
OHLCV, interval availability, provenance, freshness and error handling are **VERIFIED** for this
probe. Genuine **open-session** BSE observations remain **NOT VERIFIED** because BSE is closed.

---

## 5. Corrected Stage C acceptance scope

**Stage C active scope: NSE + BSE (Indian markets only). NYSE is deferred until Indian-market
acceptance succeeds.**

| Acceptance ID | Target |
|---|---|
| **C1** | NSE (`XNSE`) real-market decision pipeline |
| **C2** | BSE (`XBOM`) real-market decision pipeline |
| **C3** | Genuine 20-minute data aggregation and consumption for the configured Indian instruments |
| **C4** | Indian exchange daily-bar ingestion and completed-session handling |
| **C5** | Pause/resume and runtime mode restoration |
| **C6** | Lease ownership, restart and bounded recovery |
| **C7** | Natural eligible signal through the complete paper-execution lifecycle |

C4 no longer contains the NYSE-specific requirement to ingest daily bars during an open US session.
The general daily-history and completed-session validation requirements are unchanged: a daily bar
must be complete for its session, incomplete/partial bars must be excluded, and freshness must be
evaluated against the exchange's own calendar.

### 5.1 Acceptance status

| Criterion | Status | Reason |
|---|---|---|
| C1 — NSE real-market decision pipeline | **NOT VERIFIED** | Isolated XNSE environment prepared and preserved; no open session observed |
| C2 — BSE real-market decision pipeline | **NOT VERIFIED** | Isolated XBOM environment prepared; BSE closed; no ingestion or cycle run |
| C3 — genuine 20m aggregation/consumption (Indian instruments) | **NOT VERIFIED** | Adapter and aggregation verified from real provider history; no acceptance ingestion run |
| C4 — Indian daily bars and completed-session handling | **NOT VERIFIED** | Daily bars exist at the provider; none ingested into an acceptance environment |
| C5 — pause/resume and mode restoration | **NOT VERIFIED** | Runtime prepared but never started |
| C6 — lease ownership, restart, bounded recovery | **NOT VERIFIED** | No runtime started; no lease exercised |
| C7 — natural signal through the paper lifecycle | **NOT VERIFIED** | No session, signal, proposal, order, fill, exit or reconciliation observed |

**A prepared environment is not acceptance evidence.** No criterion may be marked PASS on the basis
of this report.

---

## 6. Safety verification

| Check | XNSE | XBOM | XNYS (deferred) |
|---|---|---|---|
| `paper_only` | `true` | `true` | `true` |
| `live_trading` | `false` | `false` | `false` |
| `live_orders_possible` | `false` | `false` | `false` |
| `broker_connected` | `false` | `false` | `false` |
| `automatic_policy_promotion` | `false` | `false` | `false` |
| NM-10 `paper_only` | `true` | `true` | `true` |
| NM-10 `live_trading` / `broker_connected` | `false` / `false` | `false` / `false` | `false` / `false` |
| NM-08 `executes_orders` / `real_money` | `false` / `false` | `false` / `false` | `false` / `false` |
| Runtime mode | `disabled` | `disabled` | `disabled` |
| Runtime state | `stopped` | `stopped` | `stopped` |
| Lease held | no | no | no |

No risk threshold, permission, approval gate, duplicate-order guard or reconciliation rule was
changed. No live broker class or order adapter exists in the NovaMarket source.

---

## 7. Configuration and financial-store integrity

### 7.1 User data

| Artifact | Preflight record | After correction | Verdict |
|---|---|---|---|
| `data/novamarket.json` | SHA-256 `8b4ff83e30807a6430bc9fb989f0839345e9b8a72068e265736475831f8964ec` | same SHA-256 | **Unchanged** |
| `data/novamarket.sqlite3` | 66,560,000 bytes, mtime 2026-11-26 20:21 IST | 66,560,000 bytes, same mtime, SHA-256 `6977cb79adb7203a1949de31c937bdb2a12b9383dfca1c108ae2d422335c5a68` | **Unchanged** |

No user financial record was created, modified or deleted. All three acceptance environments are
outside `data/`. No configuration change to the user store was necessary: the isolated environments
carry their own configuration, so the user hash did not need an approved scope-only change.

### 7.2 Isolated stores

| Case | `novamarket.json` SHA-256 | `novamarket.sqlite3` SHA-256 | Before vs after |
|---|---|---|---|
| `xnse` | `792a3793…cccd8` | `904a8d9e…5e1b25` | identical |
| `xbom` | `d001d138…ed2e4f` | `29824313…61108` | newly created |
| `xnys` | `79a6b82a…bd59` | `1086dafe…034a` | identical |

The main SQLite database files are hashed; the `-wal` file is 0 bytes and the `-shm` file is shared
memory, not stored data.

---

## 8. Repository preservation

| Check | Result |
|---|---|
| Branch | `feature/novamarket` |
| HEAD | `9f0aec8a0e5e21e9cb7d5b3472094d5ef313524d` |
| Dirty worktree entries at correction start | **164** |
| Dirty worktree entries after preparation | **164** — every pre-existing entry preserved |
| New untracked artifacts | this report (and the ignored `tmp/` preparation evidence) |
| `git diff --check` | exit **0** |
| Reset / clean / stash / discard | **None** |
| Commit / push / deploy | **None** |
| Historical reports deleted | **None** |

The `tmp/` acceptance and evidence directories are local preparation artifacts and are not tracked;
no source, test, configuration or documentation file outside this report and `docs/STATUS.md` was
modified.

---

## 9. Preservation and safety matrix (machine-verified)

Evidence: `tmp/stage_c_scope_correction_2026_11_26/evidence/scope_correction_verification.json`
(generated by `verify_scope_correction.py`, read-only, exit 0).

| Requirement | Result |
|---|---|
| Existing NSE instrument, portfolio, strategy, capital, monitoring and runtime preserved | **PASS** |
| NSE store hash unchanged before/after | **PASS** |
| BSE canonical identity is `XBOM:RELIANCE` | **PASS** |
| BSE provider mapping is `RELIANCE.BO`, verified from a genuine provider response | **PASS** |
| BSE portfolio and strategy are newly generated and distinct from NSE/NYSE | **PASS** |
| BSE paper capital is isolated simulated capital | **PASS** (`INR 100,000.00`) |
| BSE runtime disabled, auto-start false, no lease | **PASS** |
| BSE initial acceptance state empty (all ten activity tables + positions) | **PASS** |
| BSE has no fabricated observation | **PASS** (no store was touched by the probe) |
| NYSE environment preserved, disabled, marked deferred | **PASS** |
| No XNYS ingestion, monitoring or acceptance executed | **PASS** |
| Global paper-only/live-off/broker-off/promotion-off flags | **PASS** |
| User config and financial store unchanged | **PASS** |
| Existing dirty entries preserved; `git diff --check` clean | **PASS** |

---

## 10. Blockers and limitations

1. **Both Indian exchanges are closed at the correction timestamp** (21:50 IST). No open-session BSE
   or NSE observation can exist yet. The next available window is **Friday 27 November 2026,
   09:15–15:30 IST**.
2. **Yahoo is an unofficial delayed feed.** It is acceptable for this paper-only acceptance only
   with recorded provenance and freshness. It is not a licensed real-time feed.
3. **20m is derived, not native.** C3 must prove exact four-bar aggregation and incomplete-group
   exclusion during the acceptance run.
4. **The acceptance run has not been authorized to start by this task.** The operator must review
   this report and explicitly begin the revised NSE + BSE run.
5. **C7 has no guaranteed signal.** A quiet session must end with C7 **NOT VERIFIED**; no threshold
   may be lowered and no signal manufactured.
6. **No separate canonical Stage C specification document exists** beyond
   `docs/STAGE_C_PREFLIGHT_REPORT.md`. That report has been updated with the corrected scope and
   retains its original preparation record.

No condition required marking this correction **BLOCKED**: BSE identity, provider mapping, data
availability, portfolio creation, safety controls and user-data preservation were all satisfied.

---

## 11. Final boundary and next permissible action

This task corrected and prepared the Stage C environment only. It did **not** start NSE or BSE
ingestion, monitoring cycles, decision execution, paper orders, exits, Stage D or international
expansion.

The next permissible action is for the operator to review this report and explicitly authorize the
revised run. When authorized, at the 27 November 2026 Indian open:

1. Start the isolated XNSE environment, explicitly start autonomous paper mode, ingest genuine
   observations, and run C1/C3.
2. Start the isolated XBOM environment as its own configured cycle, explicitly start autonomous
   paper mode, ingest genuine observations, and run C2/C4/C3.
3. Run C5/C6 against the isolated runtime.
4. Leave C7 **NOT VERIFIED** unless a natural eligible signal reaches NM-08 → NM-10 →
   portfolio/ledger → exit → reconciliation → forecast outcome → advisory RL.

XNYS remains deferred and must not be started. Stage C cannot close until every applicable C1–C7
criterion has direct genuine-provider or real-application evidence and none remains PARTIAL, FAIL,
BLOCKED or NOT VERIFIED.

---

## 12. Evidence index

| Artifact | Purpose |
|---|---|
| `tmp/stage_c_scope_correction_2026_11_26/probe_bse_provider.py` | Read-only Yahoo BSE mapping/OHLCV/freshness/error probe; no store |
| `tmp/stage_c_scope_correction_2026_11_26/evidence/bse_provider_probe.json` | Provider identity, quote, 1d/5m/20m bars, freshness, error path |
| `tmp/stage_c_scope_correction_2026_11_26/prepare_bse.py` | BSE-only preparation; reads XNSE/XNYS for comparison, never starts them |
| `tmp/stage_c_scope_correction_2026_11_26/verify_scope_correction.py` | Read-only verification of all three environments, safety and user data |
| `tmp/stage_c_scope_correction_2026_11_26/evidence/scope_correction_verification.json` | Machine-verified preservation/safety matrix |
| `tmp/stage_c_v2_2026_11_27/xbom/data/`, `.../evidence/prepared.json` | New isolated BSE environment and its preparation evidence |
| `tmp/stage_c_v2_2026_11_27/xnse/` | Preserved NSE environment |
| `tmp/stage_c_v2_2026_11_27/xnys/` | Preserved, **deferred** NYSE environment |
| `tmp/stage_c_v2_2026_11_27/prepared_summary_scope_correction.json` | All three cases: IDs, runtime, capital, hashes |
| `docs/STAGE_C_PREFLIGHT_REPORT.md` | Canonical preflight record, updated with the corrected scope |
| `docs/STATUS.md` | Project status entry |
