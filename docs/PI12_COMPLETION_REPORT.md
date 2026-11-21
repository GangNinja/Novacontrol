# PI-12 — Autonomous Trading Integration Completion Report

**Date:** 16 November 2026
**Branch:** `feature/novamarket`
**Verdict:** **PASS (implementation scope)** — with the explicit local-evidence
limitation recorded in section 16.

NovaMarket remains paper-only. No broker, live-order, real-money, short-selling
or automatic model/policy-promotion path was introduced.

---

## 1. Initial repository assessment

The work was performed on the existing `feature/novamarket` working tree. The
tree already contained the uncommitted PI-01 through PI-11 implementation and
their completion reports. No file was staged, committed, tagged, pushed, merged
or published.

The mandatory dependency gate was satisfied by the existing phase reports:

- PI-01 through PI-11 were previously recorded as complete and their reports are
  present under `docs/PI01_COMPLETION_REPORT.md` … `docs/PI11_COMPLETION_REPORT.md`.
- AT-01 through AT-06 are present and accepted in
  `docs/AT-01_IMPLEMENTATION_REPORT.md`,
  `docs/AUTONOMOUS_TRADING_AT02_REPORT.md`,
  `docs/AUTONOMOUS_TRADING_AT03_REPORT.md`,
  `docs/AUTONOMOUS_TRADING_AT04_REPORT.md`,
  `docs/AUTONOMOUS_TRADING_AT05_REPORT.md`,
  `docs/AUTONOMOUS_TRADING_AT06_REPORT.md` and
  `docs/AUTONOMOUS_TRADING_FINAL_ACCEPTANCE_REPORT.md`.
- The accepted authorities were located before editing: NM-08 risk
  (`risk/engine.py`), NM-10 paper execution (`execution/`), portfolio/valuation
  (`portfolio/`), wallet/ledger (`wallet/`), AT-03 decisions (`decision/`),
  AT-04 paper forwarding (`autonomy_execution/`), PI-08 ensemble
  (`ensemble/`), PI-10 outlook (`outlook/`) and PI-11 evaluation
  (`evaluation/`).

No prerequisite dependency failure was found.

## 2. Existing functionality reused

PI-12 deliberately adds no duplicate runtime, risk, execution, portfolio,
ledger, discovery, forecast-store or scheduling authority. It reuses:

- AT-01 runtime modes, server-side operator authorization, persistent lease,
  kill switch and recovery-to-disabled;
- AT-02 session checks and the completed monitoring-cycle candidate list
  (maximum 20 instruments, fair rotation);
- AT-03 deterministic decision runs, signals, proposals, NM-08 assessment,
  append-only decision audit and proposal idempotency;
- NM-08 risk policy/snapshot/assessment and its existing forecast-reliability
  check;
- AT-04/NM-10 paper order submission, order lifecycle, fills, partial fills,
  reconciliation and duplicate-order prevention;
- NM-03/NM-07 wallet, portfolio, position, valuation and ledger services;
- PI-08 ensemble forecasts, PI-09 lifecycle records, PI-10 swing/long-term
  outlook and PI-11 evaluation reports, all read-only;
- the existing authenticated API, `ApiSurface`, `ROUTE_CONSUMERS`, generated
  API reference and React Autonomy workspace.

The new code is concentrated in
`src/novacontrol/novamarket/decision/forecast.py`,
`decision/outcomes.py`, additive fields/schema/reads in the AT-03 decision
store and pipeline, service composition, API routes and the existing Autonomy
page.

## 3. Prediction integration

`ForecastDecisionSource` reads stored forecasts from the PI-08 ensemble service
and PI-10 outlook service by instrument. It merges records by `forecast_id` and
preserves the forecast contract, but never regenerates a prediction. PI-11
reports are attached by reference as historical evidence only. The source uses
a 30-second read cache; it is not a forecast store.

The normalizer consumes the actual `ForecastOutput.to_dict()` field names:
`forecast_generation_timestamp`, `forecast_target_timestamp`,
`underlying_data_timestamp`, `forecast_validity`, `data_freshness_status` and
`ensemble_disagreement`, plus the documented short names. It preserves
forecast/lifecycle identity, direction, expected return, interval, calibrated
probabilities, uncertainty, model disagreement, model/ensemble versions,
feature-set version, target/reference timestamps, data snapshot and freshness.

By default the evaluator requires the PI-09 final (`t_minus_1`) stage and a
`valid` or `confirmed` validity. Stale, invalid, uncalibrated or uncertain
evidence cannot become BUY/SELL automatically.

A dedicated regression constructs a real `ForecastOutput`, serializes it with
`to_dict()`, and proves that the serialized contract yields a BUY decision. This
closed a real defect found during verification: an earlier normalizer read only
short internal names and would have treated an operational PI-08 forecast as
missing evidence.

## 4. Opportunity discovery

PI-12 does not create a discovery engine. It consumes only candidates already
discovered and recorded as `processed` by the AT-02 monitoring cycle. The
existing 20-instrument cap, exchange validation, duplicate suppression,
instrument-support check and fair rotation remain authoritative.

Candidate quality is carried into the decision. Bad or unavailable quality
fails closed to WAIT rather than creating a trade. `WAIT` explicitly records
the quality failure.

## 5. Decision engine implementation

`ForecastDecisionEvaluator` is pure/deterministic with an injected clock and
produces exactly one of BUY, SELL, HOLD, WAIT or NO_ACTION.

- BUY requires an eligible upward forecast, calibrated probability above the
  policy floor, expected return above the floor, acceptable uncertainty,
  disagreement, freshness, data quality, technical/event checks and portfolio
  cash/exposure rules.
- SELL requires an existing long holding and an eligible downward forecast (or
  an approved exit condition). No holding produces NO_ACTION; no short proposal
  is possible.
- HOLD retains a position when the evidence does not justify a change; add-on
  exposure is disabled by default.
- WAIT records uncertainty, excessive disagreement, absent evidence, an
  unreconciled existing order, a horizon conflict, poor candidate quality or a
  failed source.
- NO_ACTION records no eligible action. If selected forecast evidence exists,
  the candidate is owned by PI-12 and an independent NM-09 strategy is skipped
  for that run; if no selected forecast exists, the pre-existing strategy path
  remains available.

BUY and SELL create ordinary AT-03 `DecisionSignal`/`DecisionProposal` records.
HOLD, WAIT and NO_ACTION skip proposal creation. Every category stores explicit
supporting reasons, contradictory evidence, observations and model-derived
conclusions rather than a generic explanation.

## 6. Multi-horizon decision behavior

The versioned policy selects horizons per portfolio horizon:

| Portfolio horizon | Selected horizons | Default priority |
|---|---|---|
| short_term | 5m, 15m, 20m, 1h, 1d, 3d | 1h, 5m, 15m, 20m, 1d, 3d |
| medium_term | 1d, 3d, 5d, 30d | 5d, 3d, 1d, 30d |
| long_term | 30d, 90d | 90d, 30d |

Only a horizon in the selected set can be primary. Eligible upward and downward
horizons conflict rather than being silently averaged; the default conflict
policy is WAIT. `prefer_primary` and `no_action` are explicit alternatives.
Each forecast retains its own target timestamp and the decision stores
`horizon_evidence` for every selected horizon, including eligibility, failures,
uncertainty and disagreement.

The API validates policy fields, unknown keys, non-overlapping
priority/horizon sets and bounds. It cannot promote a model.

## 7. Portfolio-awareness evidence

Before evaluation, the pipeline reads the authoritative portfolio detail:
holdings, quantity, average cost, market value, unrealized P&L, cash, valuation,
sector/concentration context and position age where available. It also reads
existing NM-10 open orders through a callback over the execution store.

Tests prove:

- a BUY is a real risk-approved proposal and leaves the portfolio unchanged;
- a SELL uses an existing long position and creates a reduce-position
  proposal;
- a downward forecast without a holding produces NO_ACTION and no short;
- a HOLD for an existing position creates no proposal;
- an unreconciled open order produces WAIT;
- cash/exposure policy can block a new entry without direct portfolio mutation.

No portfolio, position, cash, valuation or ledger value is written by PI-12.

## 8. NM-08 integration evidence

NM-08 remains the sole risk authority. Every BUY/SELL proposal is assessed
through the existing `assess` interface and existing policy store. PI-12 does
not compute a limit, override an outcome or retry through another route.

`ForecastRiskAdapter` is a read-only NM-06-shaped view over the stored PI
forecasts. Its mapping is:

- 3d and 5d → `few_sessions`
- 30d → `one_month`
- 90d → `three_months`

It maps only stored evidence; NM-08 still calculates the reliability check and
all risk limits. Missing/invalid PI evidence fails the existing forecast check
closed. A risk rejection test records `risk_rejected`, preserves the violations
and risk decision, and proves the proposal is not executable. The actual service
integration test confirms the risk engine carries a `ForecastRiskAdapter` and
the decision contract reports `risk_authority="NM-08"`.

## 9. NM-10 integration evidence

NM-10 remains the sole paper-execution authority. PI-12 never calls
`submit`, `apply`, `record_fill`, a wallet mutation or a ledger write. An
approved proposal is forwarded only by the existing AT-04
`AutonomyPaperExecution` path, with its preflight, order validation, risk
revalidation, order lifecycle and reconciliation.

Tests use the real execution store/engine with deterministic market data:

- a filled BUY order is reconciled, the PI-12 decision status becomes `filled`,
  and the separate outcome records the fill count and paper P&L;
- a partial fill is reconciled, the decision becomes `partially_filled`, the
  remaining quantity is preserved and a repeated explicit cycle does not create
  a second order;
- an unfilled target outcome remains pending; a repeated tracker pass creates no
  duplicate outcome;
- a rejected/blocked proposal has no order and no execution path.

## 10. Autonomy-mode and authorization evidence

No mode semantics were changed:

- **disabled** — no autonomous scan, decision or order;
- **monitoring** — monitoring/forecast/decision simulation only, no submission;
- **autonomous** — research/decisions through NM-08 and NM-10 with the existing
  server-side operator authorization;
- **paused** — no new entries; existing protective exits remain governed by
  AT-05/NM-10;
- **kill switch** — blocks new autonomous actions and does not claim existing
  submitted orders were cancelled;
- **recovery** — returns to disabled; never auto-resumes.

The new write routes (`PUT forecast-policy`, `POST outcomes/run`) require the
existing autonomy operator token in addition to the bearer token. Tests prove
401 without authentication and 403 without operator permission. The frontend
control is not an authorization authority.

## 11. Decision-audit evidence

`DecisionStore` schema version 2 adds PI-12 tables without altering financial
tables:

- `prediction_decisions` — one immutable decision payload per deterministic
  decision key;
- `prediction_decision_events` — append-only generation/proposal/risk/execution/
  outcome events;
- `prediction_decision_outcomes` — immutable measured outcomes.

Database triggers reject updates/deletes for decisions, events and outcomes.
A decision key includes monitoring cycle, portfolio, instrument, selected
horizon, primary forecast, policy digest and decision type; repeated processing
returns the existing row rather than appending a duplicate.

Each record preserves decision ID, run/cycle ID, instrument/exchange, timestamp,
decision type, forecast/ensemble/model versions, feature/regime/event/evaluation
context, portfolio context, expected return, uncertainty, probabilities,
horizon evidence, rationale and contradictions, NM-08 result, proposal/order
references and status. The full rationale can be reconstructed from the
immutable forecast snapshot and recorded evidence.

## 12. Outcome-tracking evidence

`ForecastOutcomeTracker` measures only due decisions with a target time and a
stored market bar at or after that target. It reads the existing market-data
store and NM-10 records and writes only PI-12 outcome rows/events.

Outcome sections are deliberately separate:

1. forecast accuracy (direction/return versus the actual target-period move);
2. decision correctness (whether the chosen action was appropriate under the
   recorded evidence);
3. execution state (submitted/accepted/partial/filled/rejected/cancelled and
   reconciliation);
4. portfolio paper P&L attributable to the filled paper order.

A correct forecast does not make an execution successful, and a profitable
paper trade does not prove forecast accuracy. No model weight, risk policy or
strategy is promoted; `automatic_promotion=false` and `learning_applied=false`
are recorded.

## 13. API and frontend integration

Six additive routes were added to the existing authenticated decision surface:

- `GET /market/autonomy/decisions/predictions`
- `GET /market/autonomy/decisions/predictions/{decision_id}`
- `GET /market/autonomy/decisions/outcomes`
- `POST /market/autonomy/decisions/outcomes/run`
- `GET /market/autonomy/decisions/forecast-policy`
- `PUT /market/autonomy/decisions/forecast-policy`

They use the existing `ApiSurface`, request/response models,
`ROUTE_CONSUMERS`, authorization dependencies, error mapping and generated API
reference. `docs/API.md` was regenerated from the shared registries (657
routes). The React `Autonomy` workspace shows PI-12 decision counts, the
horizon/reliability policy, decision evidence, outcomes, refresh and
measure-due-outcomes actions. The page reads authoritative endpoints; it does
not calculate portfolio, risk or order state locally.

The frontend production build (`npm run build`) succeeds: TypeScript project
build and Vite bundle complete. The generated bundle is 672.03 kB (177.43 kB
gzip), with the existing dynamic-import warning.

## 14. Tests and actual results

Commands were run from `novamarket` with `PYTHONPATH=src`.

- PI-12 focused suite:
  `python -m pytest tests/test_novamarket_pi12.py -q` — **29 passed**.
- Focused broad regression:
  `test_novamarket_prediction.py`, `ensemble`, `scheduler`, `outlook`,
  `evaluation`, `pi12`, `at03`, `at04`, `at05`, `at06`, `execution`, `api` and
  `test_novamarket.py` — **417 passed, 305 subtests passed**. The execution
  suite includes the existing NM-10 rejected-order and partial-fill checks;
  PI-12 adds its own preflight-rejection, partial-fill, duplicate-order and
  pending-order-audit checks. The final validation pass re-ran the focused
  phase suite first, then this dependency/integration group; no full-suite run
  is claimed.
- `mypy` on the changed decision/store/service/API modules — **Success: no
  issues found in 7 source files**.
- `cd web && npm run build` — **success**.
- Actual `NovaMarketService` composition smoke/integration test:
  enable reaches `running` with an event bus, `autonomy_decision_status()`
  initialises the AT-03 pipeline, `prediction_integration.configured` is true,
  the source is a `ForecastDecisionSource`, the risk engine holds a
  `ForecastRiskAdapter`, forecast policy read/update works, decision/outcome
  reads return honest zero totals, and `live_trading`/`orders_possible` remain
  false.
- API route/contract tests prove all six PI-12 paths are registered, have
  consumers, return 401 unauthenticated, validate limits/decision types, and
  require the operator token for writes. The API reference/manifest checks also
  pass (`test_api_reference_sync.py` and `test_api.py`: **9 passed, 657
  subtests passed**), confirming `docs/API.md` remains synchronised.

The tests use deterministic market and forecast fixtures and isolated paper
stores. No broker order or real-money transaction was submitted. The frontend
build was re-run during this validation pass; no web source changed, and it
still completes successfully with the same bundle size and dynamic-import
warning.

## 15. Defects fixed

1. **Operational forecast contract mismatch.** The first normalizer read only
   `generated_at`, `target_at`, `validity`, `data_freshness` and `disagreement`.
   Actual `ForecastOutput.to_dict()` uses the timestamp/status names listed in
   section 3, so real stored forecasts would have failed closed as WAIT. Fallback
   keys were added and a real-contract serialization test now guards the path.
2. **Route inventory regression.** The AT-03 API test and the subsystem route
   namespace test counted the old surface. The PI-12 route set and the new
   `/market` route count were updated; API and route tests pass.
3. **Type-check defects.** Missing `Mapping` import, a redundant cast, an
   untyped clock return, a nullable timestamp formatting path and a policy body
   type were corrected; `mypy` is clean on the changed modules.
4. **NO_ACTION strategy ownership.** A stored downward forecast without a long
   holding now skips the independent strategy evaluation for that candidate,
   preventing a legacy strategy from silently taking the opposite trade. With no
   selected forecast evidence, the prior strategy path remains available.
5. **Outcome-queue starvation.** Decisions with no selected forecast have no
   measurable target; the pending-outcome query now excludes them instead of
   allowing them to occupy the bounded queue forever. A regression asserts the
   queue stays empty for a no-forecast NO_ACTION.
6. **Test-quality defects.** A meaningless `if False` assertion in the NM-08
   rejection test was replaced with concrete checks that the proposal is not
   executable and has no order.
7. **Inconsistent forecast direction and return.** An upward forecast with a
   non-positive expected return (or a downward forecast with a non-negative
   return) is now rejected as WAIT instead of entering a BUY/SELL. Probability
   values outside `[0, 1]`, or an up/down probability sum above one, are also
   rejected. These are forecast-contract checks; NM-08 remains the risk
   authority.
8. **Partial bars used as confirmation or outcomes.** Partial session bars are
   excluded from the technical confirmation set, and a partial bar at or after
   the forecast target can no longer become a measured outcome. Outcome
   measurement waits for a completed bar and records the actual observation
   time.
9. **Pending order states were under-reported.** NM-10 `created`,
   `pending_validation` and `cancel_pending` statuses now become the explicit
   `order_submitted` decision state with their own immutable event, rather than
   leaving the decision at `risk_approved` after an order reference exists.
10. **Invalid outcome limit raised an unhandled error.** A non-integer
    `limit` on `POST /market/autonomy/decisions/outcomes/run` now returns a
    structured 422 validation response instead of escaping as a server error.
11. **Schema metadata could stay stale.** Re-opening a decision store now
    upserts the current schema version instead of leaving an earlier metadata
    row in place.
12. **Persisted forecast policy silently reverted after restart.** The stored
    policy includes descriptive metadata (`updated_at`, phase/authority fields)
    that the parser treated as unknown, so a fresh pipeline fell back to
    defaults. The parser now accepts and strips its own descriptive metadata;
    a reopen test proves the configured thresholds and horizons survive.
13. **Outcome measurement could miss durable orders.** The service outcome
    tracker now opens the existing NM-10 engine lazily when no in-process engine
    is attached, so an order submitted before a restart remains visible. A
    failure is recorded as an execution-evidence error, never as a fill.
14. **Valid JSON bar timestamps became pending outcomes.** A target bar read as
    a mapping with an ISO-string `observed_at` parsed for selection but was then
    handed to measurement as a string, where `.isoformat()` failed. The selected
    observation is now normalised to an aware datetime; a regression proves a
    string-timestamp bar is measured once.
15. **Unknown nested horizon keys were silently accepted.** A policy could add
    an unknown key inside `horizons` or `priority` and have it ignored. Both maps
    now reject keys outside `short_term`, `medium_term` and `long_term`; the
    existing unknown-top-level-field validation is unchanged.
16. **Unknown open-order state could look like no open order.** If the NM-10
    execution object had no order store, service order awareness returned an
    empty list. It now raises so AT-03 records WAIT instead of treating an
    unknown execution state as permission to enter. The service integration
    test covers the fail-closed branch.
17. **Portfolio context omitted sector and authoritative constraints.** The
    evaluator read the portfolio detail but dropped its holding sector and the
    portfolio engine's concentration/sector constraint evidence. Both are now
    preserved read-only on the decision; NM-08 still decides the constraint.

## 16. Remaining limitations

- **No operational forecast evidence was available locally.** The local
  database does not establish a history of live PI-08/PI-10 forecasts or PI-11
  reports. Deterministic fixtures prove the code path; they are not claimed as
  operational accuracy evidence. A local empty database produces honest
  NO_ACTION/WAIT decisions.
- The NSE/BSE calendar is verified for 2026 only. Outside verified coverage,
  AT-02 autonomy fails closed; this is inherited, not changed by PI-12.
- PI-12 reads forecasts; it does not run PI-09 scheduling or train/refit models
  during autonomy. Those remain separate services/phases.
- Fundamental and news intelligence is consumed by the PI-08/PI-10 models and
  preserved in the selected forecast's point-in-time context/provenance. PI-12
  does not independently score fundamentals or invent a second directional
  fundamental signal; doing so would duplicate prediction intelligence and risk
  an unevidenced rule. If the owner requires a separate fundamental trading
  rule, that is a new, currently unimplemented requirement.
- When no eligible stored forecast exists, the prior strategy-only decision path
  remains available by design. This preserves existing AT-03 behaviour, but a
  deployment that requires every autonomous trade to have PI-12 forecast
  evidence would need an additional forecast-only gate. Forecast-backed
  BUY/SELL decisions themselves have no strategy fallback.
- The deterministic service-composition/fixture path exercises the integration,
  but no live local decision was driven from a pre-existing operational
  PI-08/PI-10 forecast history. That remains unverified rather than claimed.
- Outcome measurement needs the first stored bar at or after the target.
  Provider outages leave outcomes pending rather than fabricating a price.
- The UI was compiled and its build verified, but it was not visually inspected
  in a browser during this phase. No browser click-through is claimed.
- The final validation used deterministic fixtures and an isolated paper store;
  actual operational forecast accuracy, NSE/BSE paper fills during a live
  session, and behaviour during a real provider outage were not demonstrated.
- No commit, stage, push, tag or merge was performed, per the phase restriction.

## 17. Final acceptance verdict

**PASS for the implemented PI-12 application scope**, subject to the
local-evidence limitation above:

- PI-01 through PI-11 prerequisites are present and reported complete;
- existing architecture and authorities were inspected and reused;
- stored PI-08/PI-10 forecasts and PI-11 evidence feed an explicit decision
  layer;
- BUY, SELL, HOLD, WAIT and NO_ACTION paths are implemented and tested;
- multi-horizon selection, uncertainty, disagreement and conflict handling are
  enforced;
- portfolio/open-order awareness is read-only and tested;
- NM-08 remains the sole risk authority and NM-10 the sole paper-execution
  authority;
- approved paper orders use the existing forwarding path and reconcile filled
  and partial states without duplicate orders;
- autonomy modes, server-side operator authorization, kill switch, recovery and
  one-cycle-at-a-time behavior are preserved;
- decisions are immutable, explainable and auditable, with separate outcome
  evidence;
- API and frontend integration compile and are covered by route/contract tests;
- no broker, live-trading, real-money or short-selling functionality exists.

No known unresolved PI-12 blocking defect remains. PI-13 may begin only after
the owner accepts the limitation that operational forecast accuracy was not
demonstrated with local live evidence.
