# AT-03 — Decision and Risk Pipeline: Completion Report

**Date:** 2026-11-11  
**Worktree:** `C:/Users/NARASIMHA/OneDrive/Desktop/NovaControl/novamarket`  
**Branch:** `novamarket/nm-01`  
**HEAD:** `afa5ff6` (`afa5ff64c7673553b333fd3cff07cf5dc7c832b7`)  
**Scope:** AT-03 only. AT-04 and later are not implemented.  
**Overall verdict:** **PASS** for the AT-03 non-executable decision/risk scope.

No commit, push, stage or tag was performed.

---

## 1. What AT-03 delivers

AT-03 composes accepted components rather than replacing them:

```
AT-01 runtime (mode + lease + one cycle)
        │
        ▼
AT-02 latest completed, session-eligible monitoring cycle
        │  processed candidates, quality good|partial
        ▼
NM-09 StrategyEngine.evaluate()          (existing strategy evaluation)
        │  validated signal
        ▼
AT-03 DecisionProposal                   (intent record, not an order)
        │
        ▼
NM-08 RiskEngine.assess(proposal_id=…)   (sole risk authority)
        │  approved | rejected | restricted | requires_review | insufficient_data
        ▼
DecisionStore                            (durable runs, evaluations, signals,
                                          proposals, risk assessments, events)
```

There is no order, fill, position, cash or ledger mutation anywhere in this path.
`StrategyEngine.propose()` / `forward()`, `PortfolioEngine.apply()` and NM-10 are
never called. An approved proposal is explicitly non-executable:
`execution_allowed=false`, `orders_possible=false`, `execution_implemented=false`.

### Delivered components

- `src/novacontrol/novamarket/decision/models.py` — contract constants, statuses,
  error codes, dataclasses, legal proposal transitions, expiration timestamps.
- `src/novacontrol/novamarket/decision/store.py` — additive SQLite schema,
  immutable evaluation/signal rows, unique idempotency keys, run recovery,
  events and metrics. It shares the AT-01 SQLite connection and lock.
- `src/novacontrol/novamarket/decision/pipeline.py` — the one orchestrator:
  gates, candidate/portfolio/strategy selection, NM-09 evaluation, validation,
  proposal construction, NM-08 assessment, persistence, idempotence, recovery,
  `maybe_run()` scheduler seam, status and contract surfaces.
- `src/novacontrol/novamarket/service.py` — runtime integration: the decision
  tick runs after the monitoring tick and is closed with the service.
- `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py` —
  eight authenticated AT-03 routes and their API surface/consumer declarations.
- `docs/API.md` — regenerated (608 routes, `--check` clean).
- `web/src/pages/Autonomy.tsx` — AT-03 status, counts, recent proposals,
  health reasons, run/refresh controls, and explicit risk-vs-execution copy.
- `tests/test_novamarket_at03.py` — 21 focused tests, including real NM-09 and
  NM-08 engines and a before/after financial snapshot in every pipeline test.
- `tests/test_novamarket_at02.py` — one existing session-gate test pinned to a
  closed-session verdict so it no longer depends on the wall-clock hour.
- `.runtime/at03_live_server.py`, `.runtime/at03_perf.py` — isolated live and
  performance verification helpers (not application code).

### AT-03 HTTP surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/market/autonomy/decisions/contracts` | Static capabilities, statuses, error codes, non-goals |
| GET | `/market/autonomy/decisions/status` | Health, latest run, counts, recent proposals |
| GET | `/market/autonomy/decisions/runs` | Run history, newest first |
| GET | `/market/autonomy/decisions/runs/{run_id}` | One run with evaluations, signals, proposals, risk and events |
| GET | `/market/autonomy/decisions/signals` | Signal history with validation and provenance |
| GET | `/market/autonomy/decisions/proposals` | Proposal history with NM-08 outcomes |
| GET | `/market/autonomy/decisions/proposals/{proposal_id}` | One proposal with its evaluation, signal, risk decision and events |
| POST | `/market/autonomy/decisions/run` | Run one decision cycle; operator token required |

All reads require the ordinary API bearer token. The POST additionally requires
`X-Autonomy-Operator-Token`. No route in this group submits, fills, executes or
mutates anything financial.

---

## 2. Requirement matrix

| ID | Requirement | Result | Evidence |
|---|---|---|---|
| AT3-01 | Stay inside AT-03; no order, fill, execution, portfolio/ledger mutation, `apply()`, `forward()` or NM-10 call | **PASS** | AST safety test forbids `apply`, `submit`, `forward`, `propose`, `create_order`, `record_fill` in `decision/`; store test confirms no writes to financial tables; live database counts remain zero |
| AT3-02 | Only `monitoring` / `autonomous` may decide; `disabled` / `paused` refuse; valid AT-01 lease required; one cycle at a time | **PASS** | `RuntimeGateTests` (disabled, paused, expired lease, ineligible cycle); live POST returns `409 RUNTIME_DISABLED`; `ALREADY_RUNNING` test |
| AT3-03 | Decide only from the latest completed, session-eligible AT-02 cycle | **PASS** | `_latest_completed_cycle()` requires `completed` + `session_eligible` + verified session; `test_ineligible_monitoring_cycle_refuses` |
| AT3-04 | Use only `processed` AT-02 candidates of quality `good` / `partial`; cap the batch at 20 | **PASS** | `_candidates()` filters disposition, quality, exchange/instrument shape and support; `MAX_INSTRUMENTS_PER_RUN = 20` |
| AT3-05 | Use only active NM-09 strategies applicable to the portfolio horizon; cap at 10 | **PASS** | `_active_strategies()` + horizon filtering; `MAX_STRATEGIES_PER_RUN = 10` |
| AT3-06 | Evaluate through NM-09's existing `evaluate()`; never `propose()` / `forward()` | **PASS** | `_evaluate_strategy()` calls only `strategies.evaluate()`; the real-engine test produces a real signal |
| AT3-07 | Validate signal provenance, version, portfolio, instrument, horizon, quality, reference price, expiry, future timestamp and rationale | **PASS** | `_validate_signal()`; stale-candidate test records `invalid` and creates no proposal |
| AT3-08 | HOLD / NO_SIGNAL never becomes a proposal | **PASS** | `_NO_ACTION` branch records `no_action`; `test_no_action_does_not_become_a_proposal` |
| AT3-09 | Portfolio and instrument validation: active book, usable valuation, cash for entries, holding for reduce/close, horizon `max_positions` guard for new entries | **PASS** | `_eligible_portfolios()`, `_ensure_proposal()`, `_open_positions()`; `test_new_entry_at_the_position_cap_is_skipped_with_reason` asserts the durable `proposal.skipped` reason |
| AT3-10 | Build a durable proposal with action, side, notional, reference price, TTL and no target-weight rebalancing | **PASS** | BUY = 10% of book value, SELL = 50% of holding value, EXIT = full holding value; no target-weight path; `execution_allowed=false` |
| AT3-11 | Delegate all risk to NM-08 `assess()` with the proposal id; map every outcome | **PASS** | `_assess_proposal()` passes `proposal_id`; approved/rejected/restricted/review/insufficient-data mapping in `_RISK_STATUS` |
| AT3-12 | A risk error or timeout must never become approval | **PASS** | `test_risk_engine_failure_is_not_approval`: proposal stays `risk_pending` with `risk_error`; run is `failed` |
| AT3-13 | Persist and enforce an explicit proposal state machine | **PASS** | `PROPOSAL_TRANSITIONS`, `ensure_proposal_transition()` raises on illegal transitions; terminal states are not silently overwritten |
| AT3-14 | Preserve full provenance: evaluation → signal → proposal → NM-08 decision → events | **PASS** | `test_real_strategy_signal_proposal_and_nm08_decision` checks each id chain; proposal detail returns evaluation, signal, risk and events |
| AT3-15 | Additive persistence only; never touch wallets, positions, valuations, execution orders/fills | **PASS** | `decision_*` tables only; immutable triggers on evaluations/signals; `test_store_is_additive_and_does_not_touch_financial_tables` |
| AT3-16 | Persistent idempotency: repeated runs reuse the same signal, proposal and risk record | **PASS** | Unique `evaluation_key`, unique proposal per `signal_id`, risk primary key; `test_repeat_run_reuses_signal_proposal_and_risk` |
| AT3-17 | One active decision run at a time; reject rather than queue | **PASS** | In-process lock, unique partial index on `status='running'`, `test_concurrent_run_is_refused` |
| AT3-18 | Recover after interruption | **PASS** | `recover()` marks an interrupted `running` run `failed` with `CYCLE_CANCELLED`; `test_recovery_marks_an_interrupted_run_failed` |
| AT3-19 | Minimal authenticated API with operator gate, limits, 404 and 409 behavior; no trading route | **PASS** | `DecisionApiTests`; route contract/consumer test; live 401/404/403/409 checks; `docs/API.md` in sync |
| AT3-20 | UI must show decision state and never imply execution | **PASS** | Live Autonomy page shows the AT-03 card, NM-09→NM-08 engine, counts, health reasons and the copy “A `risk_approved` proposal is not an executed trade”; run controls disabled without runtime/lease |
| AT3-21 | `paper_only=true`, `live_trading=false`, no broker, no order capability | **PASS** | Live status and contract return all safety flags false/true as required; case-insensitive live DB probe finds zero order/fill/position/valuation/ledger rows |
| AT3-22 | Targeted tests + regressions + mypy + frontend build | **PASS** | 21 AT-03 tests; 192 passed / 608 subtests in the regression group; mypy clean in 565 files on both platforms; Vite build clean |
| AT3-23 | Measure performance of the real signal→proposal→risk path | **PASS** | 20/20 cycles completed full path; avg 62.94 ms, median 64.85 ms, p95 69.32 ms, peak 184.6 KiB; replay 13.93 ms; zero financial orders |
| AT3-24 | Respect owner decisions and publish the report | **PASS** | NC = ₹1 unchanged; day-trading flattening remains `PENDING_OWNER_DECISION`; no learning promotion or target-weight rebalancing; this report |

**Matrix result: 24 / 24 PASS.** No AT-03 acceptance row is marked FAIL.

---

## 3. Verification evidence

### 3.1 Focused and regression tests

Command:

```bash
cd novamarket
PYTHONPATH=src python -m pytest tests/test_novamarket_at03.py -q
```

Result: **21 passed** (37 warnings) in 15.50 s.

Regression group:

```bash
PYTHONPATH=src python -m pytest \
  tests/test_novamarket_at03.py tests/test_novamarket_at02.py \
  tests/test_novamarket_autonomy*.py tests/test_novamarket_strategies.py \
  tests/test_novamarket_risk.py tests/test_api_reference_sync.py -q
```

Result: **192 passed, 608 subtests passed** in 62.76 s.

The focused suite includes:

- real NM-09 strategy signal → AT-03 proposal → real NM-08 approval;
- the same run with the financial snapshot compared before and after;
- NM-08 rejection recorded with violations and no execution;
- risk-engine failure leaving `risk_pending` with an error, never approval;
- position-cap pre-filter with durable skip reason;
- stale candidate and no-action cases creating no proposal;
- repeat-run idempotency;
- interrupted-run recovery;
- runtime mode/lease/eligible-cycle gates;
- concurrent-run refusal;
- API authentication, operator authorization, limits, unknown ids and route contracts;
- AST safety and additive-store tests.

One pre-existing AT-02 session test was made deterministic: it now pins a verified
exchange-holiday verdict instead of assuming the suite runs while the exchange is
closed. This preserves the intended gate assertion and removes wall-clock flakiness.

### 3.2 Static checks

```bash
python -m mypy src
python -m mypy src --platform win32
PYTHONPATH=src python scripts/generate_api_reference.py --check
cd web && npm run build
```

- mypy: **Success: no issues found in 565 source files** (both platform views).
- API reference: **`docs/API.md: in sync`**.
- Vite: **built in 22.10 s**; 573 KiB main JS chunk; existing >500 KiB warning only.

### 3.3 Live HTTP verification (final code, port 8002)

The isolated server was restarted with both tokens after the final source edit.

| Check | Result |
|---|---|
| `GET /market/autonomy/decisions/contracts` with bearer token | **200** |
| `GET /market/autonomy/decisions/status` without token | **401** |
| `GET /market/autonomy/decisions/proposals/dprop_missing` | **404** |
| `POST /market/autonomy/decisions/run` without operator token | **403** |
| `POST /market/autonomy/decisions/run` with operator token | **409 `RUNTIME_DISABLED`** |
| Status flags | `paper_only=true`, `live_trading=false`, `broker_connected=false`, `orders_possible=false`, `execution_implemented=false` |
| Runtime state | `disabled`, `lease_valid=false`; health degraded for exactly those two reasons |
| Financial tables before and after | `execution_orders=0`, `execution_fills=0`, `portfolio_positions=0`, `portfolio_valuations=0`, `ledger_entries=0`, `wallet_transactions=0`; only the zero-balance issuance wallet exists |
| Decision tables | `decision_runs=0`, `decision_proposals=0` in the untouched live data directory |
| Server log | no errors |

The 409 is the correct safety behavior: a disabled runtime cannot decide, even
with a valid operator token.

### 3.4 UI verification

The Autonomy page was opened at `http://127.0.0.1:5173/#/autonomy` against the
live API. The rendered page showed:

- the **Decision and risk pipeline** card with `NM-09 signal → AT-03 proposal →
  NM-08 risk decision; never an order`;
- the `risk_approved` / not-executed warning;
- all AT-03 counts at zero in the disabled runtime;
- disabled **Run decision pipeline** control with the two explicit health reasons;
- no order, fill, execute or trade control in the AT-03 card.

The page was verified before the final backend-only position-cap edit; the UI
source did not change after the successful build. The full signal→proposal→risk
path was verified by the tests and the performance harness rather than by placing
the live server into a trading mode.

---

## 4. Performance

Harness: `.runtime/at03_perf.py`, using the real NM-09 `StrategyEngine`, real
NM-08 `RiskEngine`, real portfolio/wallet engines, one portfolio, one active
strategy, a deterministic market fixture and a temporary SQLite database. It
measures only `AutonomyDecisionPipeline.run_cycle()`.

Final run (20 cycles, each with a distinct 5-second bar timestamp, so every cycle
contains a full signal → proposal → risk assessment):

| Metric | Value |
|---|---|
| Cycles | 20 |
| Average latency | **62.94 ms** |
| Median | **64.85 ms** |
| Minimum | **41.68 ms** |
| Maximum | **76.10 ms** |
| p95 | **69.32 ms** |
| Current memory | **101.0 KiB** |
| Peak memory | **184.6 KiB** |
| Runs / proposals / risk approvals | **20 / 20 / 20** |
| Idempotent replay | **13.93 ms**, `completed` |
| Tasks after run | 1 |
| Financial orders | **0** |

Earlier, an initial harness reused one bar timestamp for all 20 cycles and
produced 1 proposal with 19 evaluation-only cycles. That was same-bar signal
reuse in NM-09, not a per-cycle proposal limit: advancing the bar per cycle
produced 20/20 proposals. The table above is the representative full-path
measurement; the earlier 48.48 ms average measured mostly evaluation-only cycles
and is not used as the headline number.

---

## 5. Safety argument

AT-03 is deliberately a decision recorder, not an execution layer:

1. The `decision/` package has no import of the execution engine, portfolio
   gateway, wallet engine or ledger.
2. The AST test rejects calls to `apply`, `submit`, `forward`, `propose`,
   `create_order` and `record_fill` anywhere in the package.
3. The store test forbids inserts/updates against wallets, wallet entries,
   positions, valuations, execution orders and fills.
4. Every pipeline test compares the portfolio cash/holdings/valuation and the
   funding wallet before and after a run; they are identical.
5. A live database probe confirms zero order/fill/position/valuation/ledger rows
   before and after the API checks.
6. `risk_approved` is a proposal status, not a trade status. The UI says so
   explicitly and there is no execution endpoint in the AT-03 group.

---

## 6. Deferred items and known limits

These are outside the AT-03 acceptance scope, but must remain visible:

- **AT-04 and later are not implemented.** No paper order, fill, exit
  supervision, target-weight rebalancing or autonomous execution exists.
- **Cross-strategy conflict consolidation is not performed by AT-03.** The
  pipeline calls NM-09 `evaluate()` per active strategy, not `run()` (which
  would also propose) and not its `_resolve_conflicts()` helper. Two strategies
  can therefore record opposing, independently risk-assessed, non-executable
  proposals for the same instrument. This is safe while nothing executes, but a
  conflict pass is an explicit AT-04 entry condition.
- **Proposal TTL is enforced at creation, not swept in the background.** The
  source signal expiry is authoritative and a proposal that is already expired
  is not created. A previously recorded proposal keeps its historical status;
  because it can never execute in AT-03, this does not create a trading path.
- **The live UI was observed with the runtime disabled.** It therefore showed
  zero proposals. Proposal cards are covered by the renderer path and the real
  engine tests, but no browser session was placed into a live trading mode.
- **Frontend bundle warning.** Vite reports a chunk larger than 500 KiB; this is
  a pre-existing bundling warning, not an AT-03 failure.

---

## 7. Reproduce

```bash
cd novamarket

# focused
PYTHONPATH=src python -m pytest tests/test_novamarket_at03.py -q

# static
python -m mypy src
python -m mypy src --platform win32
PYTHONPATH=src python scripts/generate_api_reference.py --check

# frontend
cd web && npm run build && cd ..

# performance
PYTHONPATH=src python .runtime/at03_perf.py

# isolated live server (do not use port 8000; it is the user's run target)
NOVACONTROL_API_TOKEN=api-secret \
NOVACONTROL_AUTONOMY_OPERATOR_TOKEN=operator-secret \
PYTHONPATH=src python .runtime/at03_live_server.py
```

Live UI: `http://127.0.0.1:5173/#/autonomy` (Vite proxy to port 8002).

---

## 8. Final statement

AT-03 is implemented, persisted, documented, exercised through the real NM-09 and
NM-08 engines, and verified against the live API and UI. It records decisions and
risk outcomes with full provenance and idempotence, and it has no path to an
order, fill, position, cash, ledger or execution engine.

**Verdict: PASS for AT-03.** The next milestone must consolidate cross-strategy
conflicts and implement execution under its own gates; neither is claimed here.
