# PI-13 — Reinforcement Learning Environment completion report

**Status:** PASS for the PI-13 scope defined below.

**Phase:** PI-13
**Branch:** `feature/novamarket`
**Contract:** `pi13-rl-v1`
**Environment version:** `pi13-rl-env-1.0`
**Date:** 2026-11-16

PI-13 adds an isolated, reproducible historical trading environment and an
experience/replay surface. It does **not** add an RL algorithm, train a policy,
claim profitability, or connect learning to production execution. Reward
components are exposed as an interface only; the reward total is `0` until
PI-14 owns a reward policy.

---

## 1. Executive summary

PI-13 is implemented as an additive `novacontrol.novamarket.rl` package that
reuses existing NovaMarket data, features, forecasts, realistic pricing and
application architecture. The environment owns a private virtual book, advances
one completed daily bar at a time, fills decisions no earlier than the next
bar's open using NM-10's pricing and fee functions, and records bounded
transitions in a separate `novamarket_rl.sqlite3` database.

The feature is exposed through 14 authenticated `/market/rl/*` routes and a new
React **RL Research** workspace. The runtime has no import or call path to
NM-10 order submission, NM-07 portfolio mutation, NM-08 risk approval,
autonomy-mode changes, operator-authorization changes, or policy promotion.

No production authority, portfolio, ledger, fill, autonomy state or live
trading setting is changed by PI-13.

---

## 2. Deliverables and touched areas

New implementation:

- `src/novacontrol/novamarket/rl/__init__.py`
- `src/novacontrol/novamarket/rl/models.py`
- `src/novacontrol/novamarket/rl/observation.py`
- `src/novacontrol/novamarket/rl/environment.py`
- `src/novacontrol/novamarket/rl/store.py`
- `src/novacontrol/novamarket/rl/service.py`

Application integration:

- `src/novacontrol/novamarket/service.py` — lazy RL service, PI-02 dataset bar
  loader, PI-04 point-in-time feature loader, PI-09 forecast loader, event
  publisher, shutdown/close wiring.
- `src/novacontrol/novamarket/features/service.py` — non-persisting
  `generate_sync` feature generation for historical observations.
- `src/novacontrol/core/events.py` and `src/novacontrol/novamarket/events.py` —
  five typed RL events and payload contracts.
- `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py` — 14
  authenticated routes, API surface declarations and non-render consumers.
- `web/src/pages/RLResearch.tsx`, `web/src/App.tsx`, `web/src/nav.ts` — RL
  Research workspace and navigation entry.
- `tests/test_novamarket_pi13.py` — focused PI-13 tests.
- `tests/test_novamarket.py`, `tests/test_novamarket_api.py` — route/event
  contract expectations updated for PI-13.
- `docs/API.md` — regenerated with **671 routes**.
- `docs/PREDICTION_INTELLIGENCE_ARCHITECTURE.md` — PI-13 scope/contract section.
- `docs/AUTONOMOUS_TRADING_ARCHITECTURE.md` — isolation and hand-off section.

No existing portfolio, execution, risk, backtesting, forecasting, evaluation or
autonomy implementation was duplicated.

---

## 3. Architecture and ownership

```
HTTP /market/rl/*
        |
        v
NovaMarketService (existing application boundary)
        |
        v
RLService (environment manager, episode lifecycle, recovery)
        |------------------------------|
        v                              v
RLEnvironment                  RLExperienceStore
(private simulated book,       (isolated SQLite:
 reset/step-style timeline,     rl_environments, rl_episodes,
 reward components,             rl_experiences)
 provenance, snapshots)
        |
        |-- PI-02 dataset bars (exact dataset + version membership)
        |-- PI-04 FeatureService.generate_sync (point-in-time, no write)
        |-- PI-09 stored forecasts (generated_at <= observation)
        |-- NM-10 pricing functions only:
            execution_price, fee_breakdown
```

`RLEnvironment` is the only owner of the simulated cash, position, average
entry, realized/unrealized P&L, fees, exposure, drawdown and timeline cursor.
It is not a portfolio engine, execution engine or backtester.

---

## 4. Public contracts and versioning

The following schema/version markers are returned by `/market/rl/contracts`:

| Contract | Version |
| --- | --- |
| RL contract | `pi13-rl-v1` |
| Environment | `pi13-rl-env-1.0` |
| Observation | `pi13-observation-v1` |
| Action | `pi13-action-v1` |
| Reward interface | `pi13-reward-interface-v1` |
| Experience | `pi13-experience-v1` |

Every simulated object explicitly reports:

- `simulated: true`
- `advisory_only: true`
- `production_authority: false`
- `production_book: false` for observations/portfolio
- `production_fill: false` for simulated executions

The configuration is immutable per environment and has a canonical
`config_hash`. The store refuses to overwrite an existing environment id with a
different configuration.

---

## 5. Observation construction and point-in-time guarantees

Each observation contains:

- `market`: completed OHLCV plus bounded derived context (returns, gap, range,
  volatility, relative volume, SMA distances, support/resistance, history
  length).
- `features`: PI-04 feature values, statuses, imputed values, version and
  `as_of`.
- `forecasts`: latest eligible PI-09 snapshot, direction, validity and
  uncertainty when available.
- `portfolio`: the environment's private simulated book.
- `provenance`: dataset id/version, bar source and timestamps, feature version
  and id, forecast id and generation time, environment/observation versions.
- `quality`: missing, invalid, imputed, stale and blocked-future-leak fields.

Guarantees enforced by `ObservationBuilder` and `RLEnvironment._prepare_bars`:

1. The active bar must be available at the observation timestamp.
2. History must end at the active bar and must not contain future bars.
3. Duplicate timestamps for the instrument are refused.
4. Bars outside the configured historical window are refused.
5. Closed-session bars are skipped with a warning when the market calendar has
   coverage.
6. Feature/forecast snapshots stamped after the observation timestamp are
   **withheld** and reported as `future_leak_blocked`; they are never silently
   used.
7. Missing, zero, imputed and invalid values remain distinct.
8. A missing or invalid portfolio section cannot pass validation.
9. Observations after the configured end are refused.

`observation_id` is deterministic from environment, episode, timestamp,
instrument, source, close and volume.

---

## 6. Action space and validation

Supported actions:

| Action | Sizing | Effect |
| --- | --- | --- |
| `buy` | one of `quantity`, `notional`, `size_fraction` | long-only buy into the private book |
| `sell` | one of `quantity`, `notional`, `size_fraction` | sell owned quantity only; no shorting |
| `hold` | none | book unchanged |
| `wait` | none | book unchanged |
| `no_action` | none | book unchanged |

Rejected inputs include:

- unknown action types,
- short/negative or zero sizes,
- multiple simultaneous sizing fields,
- size on a non-trade action,
- a missing or out-of-universe instrument,
- sizes that violate the long-only, exposure, position-weight or order-notional
  constraints,
- malformed action payload fields.

Invalid actions increment the episode's invalid-action count and are refused
before a transition is created; the simulated book and timeline remain
unchanged, and the episode remains running for a corrected action.  Rejected
actions are counted on the episode rather than stored as successful experience
rows.

---

## 7. State transitions, execution, costs and accounting

Decision timing:

1. The agent observes a **completed** bar at time `T`.
2. A BUY/SELL is simulated at the **next completed bar's open**.
3. The fill price uses the existing NM-10 `execution_price` function, including
   its configured spread/impact assumptions.
4. Fees use the existing NM-10 `fee_breakdown` function.
5. The fill is capped by a configurable participation rate of next-bar volume,
   producing a partial fill when insufficient liquidity exists.
6. The fill is also capped by `max_order_notional`, `max_position_weight` and
   `max_gross_exposure`.
7. A sell can never exceed the owned quantity; shorting is impossible.
8. If no next bar exists, the action is recorded as `no_next_bar` and the
   episode terminates rather than silently continuing.

The private book tracks cash, quantity, average entry, last price, market value,
realized P&L, unrealized P&L, fees, equity, exposure, peak equity and drawdown.
Drawdown is normalized as a non-positive fraction.

A transition preserves:

- the observation at `T` and next observation after the fill,
- action and execution result,
- portfolio before/after,
- dataset, feature and environment versions,
- config hash and reward components,
- sequence number, timestamps and error/invalid-action state.

---

## 8. Reward interface (not a PI-14 formula)

`RLReward` deliberately returns:

- `total = 0`
- `interface_only = true`
- `authoritative_formula = false`
- policy version `pi13-reward-interface-v1`

Raw components include realized/unrealized P&L deltas, equity return, risk
exposure, drawdown, transaction cost, slippage cost, decision consistency,
forecast direction/validity/uncertainty, unnecessary trading and constraint
violations.

PI-13 does not assign weights, optimize a reward, train a policy, promote a
policy, or claim that the components constitute a trading objective. PI-14 owns
the reward policy.

---

## 9. Episode lifecycle: reset, termination and truncation

Lifecycle states:

`created → running → terminated | truncated | cancelled | failed`

Rules:

- Starting an episode resets the simulated book and timeline.
- An explicit reset clears transitions, realized/unrealized P&L, fees, position,
  cash, drawdown and sequence state for that episode.
- End of the configured historical timeline is **termination** with
  `historical_data_exhausted` or `configured_period_end`.
- Reaching `max_steps` is **truncation** with `step_limit`, distinct from
  termination.
- Cancellation is terminal and does not touch other episodes.
- A failed episode records `environment_failure` and an error message.
- A terminated/truncated/cancelled episode refuses further steps.
- Historical advancement is monotonic; the environment never fills at the
  decision bar's close and never invents an intraday path from daily OHLCV.

---

## 10. Experience collection, replay and provenance

Experiences are stored per episode in `rl_experiences`, keyed by transition id
and unique `(episode_id, sequence)`. Each record includes the full observation,
action, execution, reward, next observation, portfolio snapshots, versions and
error state.

Bounds:

- `MAX_EXPERIENCES_PER_EPISODE = 5_000`
- per-store total cap `50_000` (internally clamped, hard ceiling `250_000`)
- pagination limits are clamped by the service and store
- transitions are written in one SQLite transaction with the episode summary
- duplicate sequence writes are refused
- replay is read-only and preserves provenance; it does not mutate a policy or
  production state

A step writes the episode row and experience atomically so a partial commit
cannot claim a transition that was not stored.

---

## 11. Persistence, recovery and isolation

The RL database defaults to `novamarket_rl.sqlite3` in the NovaMarket data
directory. It contains only:

- `rl_environments`
- `rl_episodes`
- `rl_experiences`
- their indexes

It does not share tables or connections with NM-07 portfolios, NM-10 orders,
fills or ledgers, NM-08 risk records, autonomy state, or the wallet.

Recovery rebuilds the environment from the persisted configuration and snapshot;
the current episode can continue after a service restart. Recovery validates the
config hash and timeline. A corrupted snapshot is refused with `RLDataError`
without altering another episode. Missing historical data is refused explicitly
rather than being replaced by synthetic bars.

---

## 12. Reproducibility and concurrent isolation

- Bars are sorted by observation time and duplicate timestamps are refused.
- Environment configuration, dataset/version, feature profile, start/end,
  initial capital and seed are persisted and hashed.
- The observation id and timeline signature are deterministic.
- Two environments with the same inputs and actions produce the same simulated
  book and transition sequence.
- Each environment owns a distinct virtual book; a step in one environment does
  not change another's cash, position or transition sequence.
- The application caps concurrently active environments at 4.
- The store and service use locks for their in-process shared state.

PI-13 is deterministic by construction; it does not require a random policy.
The `random_seed` is preserved for PI-14 and future policy experiments.

---

## 13. Resource limits and operational health

Configured hard bounds:

- 1 instrument per environment,
- 20,000 historical bars,
- 5,000 steps per episode,
- 5,000 experiences per episode,
- 4 active environments,
- 100 stored environment definitions,
- 5,000 stored episode records,
- 20 retained data warnings (plus one omission marker),
- bounded replay page sizes.

The application loader stops at `max_steps + 2` bars (and the service applies
the same ceiling), so a step-limited episode does not materialize an entire
large dataset. One extra bar is retained so the environment reports truncation
rather than falsely reporting the end of the dataset.

`/market/rl/health` reports:

- active environments and limit,
- average step latency,
- **observation-generation latency**,
- environment initialization latency,
- last step latency,
- experience/environment/episode counts,
- database size,
- last error and storage-health status.

The health panel in the UI renders these values from the API, not from local
financial calculations.

---

## 14. API integration

Fourteen authenticated routes are registered in both the FastAPI app and
`ApiSurface`, with `ROUTE_CONSUMERS` entries:

- `GET /market/rl/status`
- `GET /market/rl/contracts`
- `GET /market/rl/health`
- `POST /market/rl/environments`
- `GET /market/rl/environments`
- `GET /market/rl/environments/{environment_id}`
- `POST /market/rl/environments/{environment_id}/episodes`
- `GET /market/rl/episodes`
- `GET /market/rl/episodes/{episode_id}`
- `POST /market/rl/episodes/{episode_id}/step`
- `POST /market/rl/episodes/{episode_id}/reset`
- `POST /market/rl/episodes/{episode_id}/cancel`
- `GET /market/rl/episodes/{episode_id}/transitions`
- `GET /market/rl/episodes/{episode_id}/experiences`

The API returns structured 4xx errors for invalid configuration, invalid
actions, terminal episodes, missing records and corrupted recovery. It does not
expose an order-submission, portfolio-mutation, autonomy-control or
policy-promotion route.

Five typed events are registered and published:

- `novamarket.rl.episode_started`
- `novamarket.rl.step`
- `novamarket.rl.episode_completed`
- `novamarket.rl.episode_reset`
- `novamarket.rl.episode_cancelled`

The implementation contract event count increased from 187 to 192.

---

## 15. UI integration

The new **RL Research** page (`web/src/pages/RLResearch.tsx`) is registered under
the `rlresearch` navigation item and route. It provides:

- environment configuration form (dataset/version, instrument, window, virtual
  capital, max steps),
- environment and episode lists,
- start/step/reset/cancel controls,
- simulated equity, cash, P&L and drawdown cards,
- transition/experience history,
- observation snapshot with timestamp, feature version, quality and provenance,
- environment health/latency/resource panel,
- version contracts and explicit “policy promotion not available in PI-13”
  messaging.

The page calls only `/market/rl/*` endpoints and renders backend values; it does
not calculate authoritative financial results in the browser.

---

## 16. Safety isolation and authority boundaries

Structural boundaries:

- `novamarket/rl` does not import NM-10's `ExecutionEngine`, order store, fill
  store or ledger.
- It imports only NM-10 pricing functions and `ExecutionConfig` for validation.
- There is no call path to NM-07 portfolio mutation, NM-08 risk approval, NM-09
  lifecycle mutation, NM-10 order submission, autonomy mode/start/stop or
  operator authorization.
- RL actions are plain action contracts without order ids, portfolio ids, wallet
  ids or execution-engine handles.
- The RL database is separate and contains only `rl_*` tables.
- The API surface contains no RL route that changes production state.
- Policy promotion is not implemented; no model is trained or activated.
- No live trading or real-money permission is enabled.

The API/UI isolation is therefore backed by package boundaries and database
separation, not merely by labels.

---

## 17. Configuration reference

Primary configuration fields:

| Field | Default / constraint |
| --- | --- |
| `name` | required |
| `dataset_id`, `dataset_version` | required PI-02 identity |
| `instrument_id` | one canonical `MIC:SYMBOL` |
| `start`, `end` | required, `start < end` |
| `initial_capital` | `100000.00`, positive |
| `resolution` | `1d` only |
| `feature_profile` | `swing` |
| `include_features`, `include_forecasts` | `true` |
| `max_steps` | `252`, range 1–5000 |
| `max_position_weight` | `0.25`, within (0, 1] |
| `max_order_notional` | `25000.00`, positive |
| `max_gross_exposure` | `1.00`, within (0, 1] |
| `execution_config` | NM-10 `ExecutionConfig` mapping; validated |
| `random_seed` | `42` |
| `reward_interface_version` | `pi13-reward-interface-v1` |
| `notes` | optional |

PI-13 intentionally supports one instrument per environment. Multi-instrument
portfolio timing is deferred rather than guessed.

---

## 18. Verification evidence

All commands were run from `novamarket` with `PYTHONPATH=src`.

| Check | Command | Result |
| --- | --- | --- |
| Focused PI-13 suite | `python -m pytest tests/test_novamarket_pi13.py -q` | **40 passed, 3 subtests passed** |
| Event contract | `python -m pytest tests/test_novamarket.py -q` | **87 passed, 53 subtests passed** |
| NovaMarket API | `python -m pytest tests/test_novamarket_api.py -q` | **56 passed, 266 subtests passed** |
| Feature regression | `python -m pytest tests/test_novamarket_features.py -q` | **35 passed** |
| Backtesting regression | `python -m pytest tests/test_novamarket_backtesting.py -q` | **48 passed, 4 subtests passed** |
| Dataset regression | `python -m pytest tests/test_novamarket_datasets.py -q` | **25 passed** |
| PI-12 regression | `python -m pytest tests/test_novamarket_pi12.py -q` | **29 passed** |
| Evaluation regression | `python -m pytest tests/test_novamarket_evaluation.py -q` | **22 passed** |
| Prediction regression | `python -m pytest tests/test_novamarket_prediction.py -q` | **24 passed** |
| Web/API reference contracts | `python -m pytest tests/test_web_platform.py tests/test_api_reference_sync.py -q` | **52 passed, 1385 subtests passed** |
| Application/events/core API | `python -m pytest tests/test_application.py tests/test_events.py tests/test_api.py -q` | **61 passed, 15 subtests passed** |
| Core API | `python -m pytest tests/test_api.py -q` | **3 passed** |
| Type checking | focused `mypy` on the RL package and touched integration files | **Success: no issues found in 12 source files** |
| RL lint | `python -m ruff check src/novacontrol/novamarket/rl` | **All checks passed** |
| Frontend build | `cd web && npm run build` | **success**, 688.80 kB JS / 181.03 kB gzip, 91.25 kB CSS |
| Docs reference | `docs/API.md` regenerated and reference-sync test | **671 routes** |
| Whitespace | `git diff --check` | clean |

Live application integration check:

- Started the real FastAPI app with a temporary data directory and a local test
  bearer token.
- Started the real Vite React app on `http://127.0.0.1:5173`.
- Loaded `#/rlresearch`; the browser requested
  `/market/rl/status`, `/market/rl/contracts`, `/market/rl/health`,
  `/market/rl/environments`, `/market/rl/episodes` and the existing alert/
  overview endpoints.
- All observed RL requests returned **HTTP 200**.
- The page rendered the PI-13 header, version contracts
  (`pi13-observation-v1`, `pi13-action-v1`, `pi13-reward-interface-v1`,
  `pi13-experience-v1`), empty environment/episode states, bounded health panel
  and the “policy promotion not available in PI-13” message.
- No console errors were emitted after the authenticated reload.
- The temporary backend/data directory was removed after verification.
- Not tested live with a seeded PI-02 dataset; creation/start/step execution is
  covered by the API and application tests with the real service wiring and
  mocked loaders, not by a live dataset-backed UI run.

### Post-verification audit fixes

The acceptance audit found and repaired these issues, each with a regression
test or an assertion added to an existing test:

1. Invalid forecasts were classified as `stale`; they are now `invalid` and
   make the observation quality `invalid`.
2. Stale/imputed feature or forecast quality now makes the overall observation
   `partial` instead of being reported as `valid`.
3. Recovery refused an episode whose older experiences had been pruned by the
   bounded store; recovery now accepts a pruned replay history while still
   rejecting a stored experience count greater than the state sequence.
4. Cancelling a persisted running episode after a restart was refused; cancel
   now recovers the snapshot and cancels through the live path.
5. Resetting a non-active episode could create a fifth active episode past the
   four-environment limit; reset and recovery now apply the same limit as start.
6. Two concurrent starts could both cross the active limit; start re-checks
   under the service lock and rolls back its active registration if persistence
   fails.
7. A failed step could leave a failed environment in the active count; failed
   steps now remove the active registration.
8. Recovery's timeline signature covered only open/close/volume; it now covers
   observed/available/retrieved timestamps, OHLC, volume, source, adjustment and
   tradability so any historical mutation is refused.
9. Environment definitions and episode records were unbounded; they now have
   explicit storage caps (100 environments, 5,000 episodes) alongside the
   existing experience bounds.
10. Non-positive prices and negative volume are refused during timeline
   preparation; the shared `HistoricalBar` constructor already blocks the same
   invalid OHLC input.
11. The `unnecessary_trading` reward component was structurally always zero;
    it now reports a trade attempt that produced no fill, while remaining an
    interface-only component.
12. Observations now reject unavailable historical rows in addition to future
    rows, and a refused caller action no longer marks the runtime unhealthy; storage
    errors now make `healthy` false as well.

The focused suite after these fixes is **40 passed, 3 subtests passed**.

---

## 19. Mandatory acceptance matrix

| # | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Environment initialization and configuration | PASS | config validation tests; API create test |
| 2 | Observation schema and required sections | PASS | observation contract test |
| 3 | Timestamps and completed-bar semantics | PASS | timestamp/point-in-time tests |
| 4 | Feature/dataset version provenance | PASS | feature-version and provenance assertions |
| 5 | Future-leak prevention | PASS | future feature/forecast withholding test |
| 6 | BUY action | PASS | next-bar fill/cost test |
| 7 | SELL action | PASS | realized P&L and no-short test |
| 8 | HOLD / WAIT / NO_ACTION | PASS | unchanged-book test |
| 9 | Deterministic transitions | PASS | two-environment reproducibility test |
| 10 | Historical advancement and calendar handling | PASS | advancement/calendar test; closed-session skip |
| 11 | Next-bar execution timing | PASS | fill price = next bar open plus NM-10 pricing |
| 12 | Transaction costs and fees | PASS | NM-10 fee/slippage assertions |
| 13 | Simulated accounting | PASS | cash, position, P&L, equity, drawdown tests |
| 14 | Reset behavior | PASS | reset-clears-state test |
| 15 | Termination | PASS | end-of-history termination test |
| 16 | Truncation distinct from termination | PASS | step-limit truncation test |
| 17 | Experience collection | PASS | grouping/provenance test |
| 18 | Replay | PASS | bounded read-only experience/transition API test |
| 19 | Reproducibility | PASS | deterministic twin-environment test |
| 20 | Concurrent environment isolation | PASS | isolation test; active limit test |
| 21 | Persistence | PASS | restart/recovery test |
| 22 | Recovery and corruption handling | PASS | corrupted snapshot refused, other episode intact |
| 23 | Resource limits and health | PASS | cap tests and health/latency test |
| 24 | API exposure | PASS | 14 authenticated routes; 56 API tests |
| 25 | UI exposure | PASS | RL Research page and frontend integration test |
| 26 | Production-state isolation | PASS | separate DB/tables and no production identifiers |
| 27 | NM-08 risk authority preserved | PASS | no call/import path; API surface has no bypass |
| 28 | NM-10 execution authority preserved | PASS | pricing functions only; no order submission |
| 29 | Actual application integration | PASS | live FastAPI + Vite page, RL requests 200 |
| 30 | Frontend integration | PASS | real route registration and live rendering |
| 31 | PI-01–PI-12 regression | PASS (focused) | API, events, datasets, features, backtesting, prediction, evaluation, PI-12, web/reference suites all pass |

---

## 20. Limitations and hand-off

Deliberate PI-13 limitations:

- No RL algorithm, training loop, policy object, policy registry or automatic
  promotion. The environment is reset/step-style and algorithm-independent.
- Reward total is zero; PI-14 must define and own any reward formula.
- One instrument per environment. Cross-sectional/multi-asset execution is not
  claimed.
- Daily completed OHLCV only; no intraday, order-book depth, queue position or
  latency simulation.
- Execution uses NM-10 pricing/fee functions and a synthetic participation cap;
  it does not submit orders or write production fills.
- Corporate actions, survivorship handling and exchange-specific auction rules
  are not separately modeled in PI-13; dataset bars retain their own
  adjusted/quality metadata.
- Experience replay is a bounded read surface, not a training sampler. PI-14 may
  add sampling while preserving provenance.
- The completion report does not claim profitability, generalization or
  learned-policy quality.

Hand-off to PI-14:

1. Define the reward policy and weights explicitly, versioned separately.
2. Choose discrete/continuous action handling for a training algorithm.
3. Add a replay sampler and training/evaluation loop outside the environment.
4. Keep policy evaluation, promotion and any autonomy proposal human-gated and
   independent of this environment's simulated book.
5. Preserve the PI-13 isolation boundary: training must not gain order,
   portfolio, risk, autonomy or live-trading authority by implication.
