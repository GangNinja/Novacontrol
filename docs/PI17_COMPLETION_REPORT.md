# PI-17 Completion Report — Multi-Agent Trading Intelligence

## Verdict

**PASS** for the implemented PI-17 scope, subject to the limitations below.
PI-17 is a read-only, advisory research layer. NM-08 remains the sole risk
authority, NM-10 remains the sole paper-execution authority and PI-12 remains the
autonomy orchestrator. No order, portfolio mutation, policy activation, broker
connection, permission change or live-trading path was added.

## Scope delivered

PI-17 adds an isolated NovaMarket package at
`src/novacontrol/novamarket/multiagent/` and the React
`MultiAgentIntelligence` workspace. It does not reuse the generic NovaControl
desktop/research agent pipeline for financial evidence; that pipeline executes
sequential tools and is not a financial coordinator.

Implemented:

- Nine explicit roles: market research, technical analysis, fundamental
  analysis, prediction intelligence, market regime, risk analysis, portfolio
  intelligence, RL policy intelligence and an advisory trading-decision agent.
  The decision cycle requires technical, prediction, regime, risk and portfolio;
  research, fundamental and RL policy are optional.
- Versioned role definitions with read-only capabilities, provider/model
  identity, permitted actions and response validation. A specialist cannot
  return an action outside its role contract.
- Validated messages, evidence and decisions. Evidence carries source identity,
  source reference, original/availability/retrieval times, freshness, validation,
  data/feature/model/forecast versions, correlation group and independence.
- Deterministic, point-in-time specialists over existing PI-04 features/regime,
  PI-05/PI-08 forecasts, NM-08 read-only portfolio risk, NM-07 portfolio state,
  PI-15 policies and PI-16 adaptive evidence. Missing or future evidence is
  reported, never guessed.
- Bounded concurrent orchestration with six workers, per-agent and per-cycle
  timeouts, cooperative cancellation, idempotent cycle creation, stale-run
  recovery, history, agent configuration/audit and performance records.
- Conflict handling for directional disagreement, stale/invalid data, high
  forecast uncertainty, NM-08 restrictions, portfolio impediments and
  experimental-RL disagreement. Correlated evidence is discounted; unresolved
  authoritative restrictions prevent BUY/SELL.
- Evidence-weighted aggregation to `buy`, `sell`, `hold`, `wait` or `no_action`.
  Decision confidence is always `None` unless valid calibration exists;
  qualitative evidence strength is reported instead.
- Explicit PI-12 handoff contract with `advisory_only=true`,
  `production_authority=false` and `order_submitted=false`. The documented path
  remains PI-12 → NM-08 → NM-10.
- Isolated `novamarket_multiagent.sqlite3` store using only `ma_*` tables. It
  never opens a production financial table.
- Eighteen paths / nineteen authenticated `/market/multiagent/*` routes, declared
  in `ApiSurface`, `ROUTE_CONSUMERS` and the generated `docs/API.md`.
- React workspace for cycle creation/run/cancel, monitor, agent health and
  configuration, analysis/evidence detail, provenance, conflicts, decision
  aggregation/authority boundary and performance.

## Integration boundaries

- The package has no method that can mutate a portfolio, wallet or ledger, submit
  or cancel an order, grant operator permissions, change autonomy mode, activate
  a policy, connect a broker or enable live trading.
- Risk evidence is a read-only NM-08 report. Multi-agent output is not a risk
  approval and cannot be treated as one.
- Portfolio evidence is a read-only NM-07 view. No allocation, rebalance or
  position write is available.
- RL policy output is experimental and discounted; it cannot activate a policy.
- PI-12 now reads the newest completed handoff for the same
  instrument/exchange/horizon and stores it on its own proposal as
  `advisory_evidence` (see the integration section below). The block is attached
  after the action and notional are already fixed, is not read by the NM-08 gate
  and cannot reach NM-10 on its own.
- The existing kill switch, operator permissions, single-cycle concurrency,
  idempotency, reconciliation and recovery-to-disabled behavior are unchanged.

## PI-17 → PI-12 advisory evidence integration

The handoff is consumed by the AT-03 decision path as evidence only.

- `novacontrol.novamarket.decision.advisory` holds the boundary.
  `MultiAgentAdvisoryEvidence` opens the multi-agent service lazily and asks the
  new read-only `MultiAgentIntelligenceService.latest_handoff(instrument_id,
  exchange, horizon)`, which returns the newest decision on a **completed** cycle
  (plus `decided_at`/`cycle_status`) and refuses an empty or different instrument
  and a mismatched exchange or horizon.
- `advisory_evidence()` copies an allow-listed field set and re-asserts
  `advisory_only=true`, `production_authority=false`, `order_submitted=false`,
  `risk_review_required=true`, `requires_nm08_reassessment=true`,
  `requires_pi12_orchestration=true`, `requires_operator_permission=true`, along
  with `read_only=true` and `affects_action=false`. Unknown keys are dropped, so a
  handoff cannot claim authority or smuggle extra data into a proposal.
- `AutonomyDecisionPipeline` takes an optional `advisory_evidence` reader and
  attaches the sanitised block to `DecisionProposal.advisory_evidence` inside
  `_ensure_proposal`, after the proposal id, action and notional are computed.
  `proposal_id` does not include the evidence, so an idempotent replay attaches
  exactly one block and creates no second proposal.
- The reader is only called once a proposal is actually being created, which is
  after `_assert_runtime_ready()`. A disabled runtime, a paused runtime, a foreign
  lease or the kill switch (`runtime.stop()`) therefore refuse the run before any
  handoff is read. Recovery still reopens disabled: no auto-resume, no lease, no
  evidence consumption.
- A reader failure (for example an unavailable multi-agent store) is recorded in
  `pipeline.status()["advisory_evidence"]["last_error"]` and the decision
  proceeds unchanged. Evidence never blocks a decision, changes an action,
  satisfies NM-08 or authorises NM-10.
- `service.py` wires the adapter behind `_ensure_multiagent`, so a decision run
  that never reaches a proposal never opens the multi-agent service.

Focused tests live in `tests/test_novamarket_pi17_pi12_handoff.py`:

1. the block is attached with the forced read-only flags and a recommendation
   that contradicts the proposal, while the action still follows the PI-12
   signal;
2. a handoff for another instrument is not attached;
3. a raising reader leaves the proposal and its `risk_approved` status intact and
   records the error in status;
4. a replay keeps one proposal, the same proposal id and one evidence block;
5. the kill switch stops the run before the reader is called and records no
   proposal;
6. a recovered runtime is `disabled`, refuses the run and consumes no evidence;
7. the sanitiser cannot be weakened by hostile handoff flags, and identity
   mismatches are rejected;
8. `latest_handoff` returns only a completed, matching decision and `None` for a
   wrong exchange, horizon, instrument or empty instrument.

## Defects found and fixed during verification

1. **Portfolio context was dropped on cycle execution.** `run_cycle` rebuilt
   `CycleRequest` from cycle metadata without `portfolio_id`, so NM-07/NM-08
   context was absent even when creation supplied a portfolio. The field is now
   restored and pinned by a focused test.
2. **Risk-agent error path crashed.** A future-dated/error response called
   `_base(..., error=...)`, which did not accept `error`. The parameter is now
   supported and bounded.
3. **Current NM-08/portfolio reads were misclassified as future data.** Context
   is assembled milliseconds after the cycle cutoff. A five-second tolerance now
   clamps only that transport skew to the cutoff; genuinely future-dated evidence
   is still rejected.
4. **Forecast horizon mapping used unsupported lifecycle labels.** Requests sent
   `next_session`/`few_sessions` to a store that accepts `1d`/`3d`/`5d`, etc.
   Mapping is now explicit (`60m → 1h`; all others use their native labels), and a
   real PI-05 forecast was retrieved by the live cycle.
5. **Prediction evidence lost its real timestamps and direction.** The forecast
   normalizer did not map `predicted_direction`, `forecast_uncertainty`,
   `forecast_validity`, `forecast_generation_timestamp` or
   `forecast_horizon`. The result was an unverified/unknown prediction item. It
   now records a fresh, valid, direction-aware forecast.
6. **Technical evidence hid stale market data.** Derived feature evidence used
   the feature generation `as_of` rather than the source `source_last_at`, making
   stale inputs look fresh. It now uses the source observation time, which
   correctly produced a stale-data conflict and WAIT in the live application.
7. **Route-contract whitelist was incomplete.** The exact no-live-execution route
   set did not include the new `/market/multiagent/*` paths. The test now declares
   all eighteen paths.
8. **Type and lint defects in the new package** (unused imports, unsafe `float`
   narrowing, `Mapping` mutation, ambiguous decision retrieval, list typing and
   overlong lines) were corrected without suppressions or weakened checks.
9. **Optional-only pinning bypassed the required risk gate.** A cycle created
   with only `fin-technical` and `fin-regime` could aggregate a BUY without any
   risk or portfolio assessment. Creation and execution now always union
   `REQUIRED_AGENT_IDS` into the assignment, old stored cycles are normalised at
   run time, and aggregation explicitly reports required roles that are absent.
   Pinned by `test_pinning_cannot_remove_required_agents`.
10. **A non-approved NM-08 outcome could still permit a trade direction.** Risk
    findings with `insufficient_data`, unknown outcomes or breach lists were
    previously treated as advisory only. Any non-`accepted`/`approved` outcome
    and every unresolved breach or restriction now vetoes BUY/SELL and downgrades
    to WAIT. Pinned by `test_nm08_insufficient_data_blocks_an_action`.
11. **FAILED agent responses skipped schema validation.** Validation returned
    early for failed responses, so an injected agent could return an actionable
    payload or prohibited authority key under a FAILED status. Action, payload,
    evidence and prohibited keys are now validated for every response status.
12. **Expired communication records were accepted, and evidence strength
    overstated support.** Expired messages are rejected. Evidence strength now
    counts only independent, verified, valid, non-stale evidence; invalid and
    unverified evidence carries zero directional weight and forces WAIT when it
    is critical. Pinned by
    `test_invalid_evidence_cannot_support_a_directional_action`.
13. **Persistence gaps hid decisions and made recovery time-dependent.**
    `save_decision` did not commit, so a decision could disappear when a
    connection closed; it now commits. Stale-run recovery accepts an injected
    clock and a recovered RUNNING cycle becomes a degraded, decision-less cycle
    instead of remaining stuck. Pinned by the recovery test.
14. **Unbounded request and output scanning edges.** Cycle requests are capped at
    50 KB, prohibited-key scanning covers every item in a validated response
    rather than the first 200, and the status route maps store failures through
    the same HTTP error contract as the other PI-17 routes.
15. **The React workspace understated the required role set.** Optional chips now
    list only research, fundamental and RL policy; the five required roles always
    run. The portfolio field says it is required for risk/portfolio evidence, and
    the historical-cutoff hint no longer promises data that latest-only sources
    cannot supply.

16. **The agent registry did not label required vs optional roles.**
    `/market/multiagent/agents` returns `required` per role and the documented
    required set is five, but the dashboard only listed the role name, so the
    distinction existed only in prose. The Agent dashboard now renders a Scope
    badge; the live page showed the five required specialists plus the
    trading-decision agent as `required`, and market research, fundamental and
    RL policy as `optional`.
17. **Cancel stayed enabled on a finished cycle.** Clicking it on a
    completed/degraded cycle returned 409 “a completed research cycle cannot be
    cancelled” as an error toast. Cancel is now disabled unless the selected
    cycle is `pending` or `running`, matching the coordinator’s own cancel rule;
    the control stays available for a cycle that has not finished.
18. **The handoff document was never rendered.** The workspace displayed the
    authority fields of the decision, but no code requested
    `GET /market/multiagent/cycles/{id}/handoff`, so the contract an evidence
    consumer actually reads could not be inspected. A “Handoff to PI-12” card now
    fetches that endpoint and renders the recommendation, `advisory_only`,
    `production_authority`, `order_submitted`, `eligible_research_signal`, the
    four requirement flags, the PI-12 → NM-08 → NM-10 execution path and the
    authority flags (`agents_can_submit_orders`, `agents_can_override_kill_switch`,
    `agents_can_change_modes`). A cycle without a decision shows “No handoff yet”.
19. **Selecting a cycle without a decision requested the wrong handoff.** The
    fetch was gated on the previously loaded decision, so switching from a
    completed cycle to a cancelled one issued a handoff request for the cancelled
    cycle and logged a browser 404, while the previous document stayed visible
    until refresh. The request is now gated on the loaded cycle id having its own
    decision, and a document is only rendered when its `cycle_id` matches the
    selection.

## Verification evidence

| Check | Result |
| --- | --- |
| `PYTHONPATH=src python -m pytest tests/test_novamarket_pi17.py -q` | **35 passed** |
| `PYTHONPATH=src;tests python -m pytest tests/test_novamarket_pi17_pi12_handoff.py -q` | **10 passed** |
| Integration regression: PI-12, PI-17, handoff, AT-01 autonomy | **94 passed** |
| `python -m mypy src --platform win32` after the integration | clean, **717 source files** |
| Affected regression: features, ensemble, evaluation, PI-13–PI-17 | **265 passed, 3 subtests passed** |
| `PYTHONPATH=src python -m pytest tests/test_novamarket_api.py -q` | **62 passed, 325 subtests passed** |
| `python -m mypy src` | clean, **668 source files** |
| `python -m mypy src --platform win32` | clean, **668 source files** |
| `python -m ruff check` on the multiagent package and focused test | clean |
| `npm run build --prefix web` | passed (TypeScript + Vite) |
| `PYTHONPATH=src python scripts/generate_api_reference.py --check` | in sync, **766 routes** (19 PI-17) |
| `git diff --check` | clean (line-ending warnings only) |
| Staged files | **0** |

The focused suite covers initialization/contracts, deterministic agents and
fixtures, required versus optional failure, conflict and correlated-evidence
rules, multi-horizon selection, WAIT/NO_ACTION, provenance and future-data
rejection, cross-cycle isolation, permissions, idempotency, cancellation,
timeout/recovery, bounded storage, untrusted content, API/UI registration and
static/runtime financial isolation. The HTTP test uses the real
`create_app()` factory and proves a cycle leaves wallet and portfolio state
unchanged.

## Objective verification

| Objective / requirement | Result | Evidence |
| --- | --- | --- |
| Read-only, advisory specialists with no financial authority | **PASS** | Route and source isolation tests; live cycle left wallet/portfolio state unchanged |
| Deterministic agents and reproducible fixtures | **PASS** | Focused agent/fixture tests; identical evidence produces identical responses |
| Required versus optional failure behavior | **PASS** | Required failure blocks; optional failure is visible and degrades without hiding the decision |
| Conflict detection and correlated-evidence discounting | **PASS** | Directional/stale/uncertainty/RL conflicts; duplicated evidence does not outvote independent evidence |
| Multi-horizon isolation | **PASS** | Unsupported/mixed horizons rejected; matching forecast horizon selected; live 1d cycle |
| WAIT/NO_ACTION safety paths | **PASS** | Missing required roles, risk veto, stale/invalid critical evidence and invalid responses |
| Provenance, freshness and future-data rejection | **PASS** | Source identity/time/version tests; future-dated feature, regime, forecast, portfolio and message tests |
| Cross-cycle isolation | **PASS** | Responses/messages are cycle-bound; cross-cycle response rejected |
| Permissions and untrusted content | **PASS** | Read-only capability whitelist; authority/prohibited-key rejection at every status; content treated as data |
| Idempotency, cancellation, timeout and recovery | **PASS** | Focused API/service tests; stale RUNNING recovery now returns degraded with no decision |
| Bounded resources | **PASS with limitation** | Six workers, per-agent/cycle timeout, 50,000 message/run and 2,000 cycle storage caps, 50 KB request and 300 KB response caps; Python cannot forcibly kill a hung thread |
| API surface and authentication | **PASS** | 19 authenticated routes, exact allowlist, 62 API tests |
| React workspace | **PASS (browser)** | TypeScript/Vite build passes; a live cycle was created and run through the controls and the monitor, evidence, conflicts, decision and handoff panels were inspected |
| PI-12 / NM-08 / NM-10 authority handoff | **PASS (evidence-only)** | Handoff is attached to PI-12 proposals as read-only `advisory_evidence`; it cannot select an action, satisfy NM-08, authorise NM-10, override the kill switch or survive recovery as authority |
| Documentation and report | **PASS** | Architecture, API reference in sync at 766 routes, single PI-17 report |

## Real application verification

The application was started against the existing `data/` directory with the real
FastAPI application and Vite frontend.

- A real PI-04 feature set was generated for `XBOM:ADANIENT`:
  `fs_e220cb78daa702cb33b99bea`, source bar timestamp
  `2026-11-11T03:45:00+00:00`, regime `weak_up`.
- A real PI-05 statistical `1d` forecast was generated through
  `POST /market/forecasts`.
- A real portfolio (`pf_31198029410129e5f227`, active short-term acceptance book)
  was supplied as read-only context.
- Cycle `mac_a41e521c5f78414da0055db5ea8e0b88` completed:
  - technical, prediction, regime, risk and portfolio all `completed`;
  - prediction evidence `fresh`/`valid`; technical derived evidence `stale`;
  - one resolved authoritative stale-data conflict;
  - action **WAIT**, evidence strength `moderate`, confidence `null`;
  - handoff `order_submitted=false`, `production_authority=false`,
    `advisory_only=true`;
  - execution path `PI-12 autonomy orchestration → NM-08 authoritative risk
    assessment → NM-10 authoritative paper execution`.
- The workspace loaded the same cycle through the Vite proxy and displayed the
  monitor, agent dashboard, evidence/provenance, conflict, Wait decision,
  authority boundary and performance. After the final API restart, the page made
  all `/market/multiagent/*` requests with 200 responses and no new console
  errors.

The live market history ended several days before the verification date. The
cycle therefore correctly returned WAIT because critical market/portfolio
evidence was stale; this is the intended safety behavior, not a hidden success
path.

A later pass reloaded the Multi-Agent workspace and drove the controls directly:

- **Create cycle** produced pending cycle `mac_ae466c0ee3994f2a985f08ff27b01c76`
  (`XNSE:ALPHA` · `XNSE` · `5d`) with **5 assigned · 5 required** and every
  monitor chip `pending`; the toast reported that the cycle was created and no
  duplicate research was started.
- **Run selected** reached **5 / 5 responses** and status **Degraded**: all five
  required agents returned `Insufficient Evidence` for the requested window, one
  conflict was recorded (resolved, so unresolved conflicts showed 0), and the
  degraded-evidence banner named the missing analysis instead of hiding it.
- **Decision aggregation** rendered **NO_ACTION**, evidence strength
  **Insufficient**, confidence **Not claimed**, “required evidence is missing or
  failed” naming technical, prediction, regime, risk and portfolio, **0**
  independent verified evidence families, and the explicit advisory boundary
  (NM-08 review and PI-12 → NM-10 remain the only execution path, no order
  submitted).
- **Cancel** on that finished cycle was refused by the coordinator
  (`409`, “a completed research cycle cannot be cancelled”). After fix 17 the
  button is disabled there; on a fresh pending cycle
  (`mac_96d4d40eeb7841d5b8fc36b56d36542c`) it returned 200, the monitor showed
  **Cancelled**, and the toast read “Research task cancelled. No financial state
  or order was touched.”
- **Scope labels** rendered from the live registry: six rows `required` (the
  five required specialists plus the trading-decision agent) and three
  `optional`, matching both the API payload and the documented required set.
- A final clean pass on that build (create `mac_55a37e2ab89a43ba9389a6bb75184553`
  then cancel) produced **no console errors** and **200 for every**
  `/market/multiagent/*` request, including the create and cancel mutations.

A later run on the current build created and executed a cycle entirely through the
page controls, then inspected every panel:

- **Create cycle** with `XBOM:ADANIENT` · `XBOM` · `1d` and portfolio
  `pf_31198029410129e5f227` produced pending
  `mac_fa06f1bd4d584295bc9b2c9d7992af77` with **5 assigned · 5 required** and every
  agent chip `pending`.
- **Run selected** reached **5 / 5 responses** and **Degraded**: technical,
  prediction, regime and portfolio `completed`, risk **Invalid** with the
  orchestration note “required agent failure: fin-risk”, **2** conflicts (stale
  data and required failure, both authoritative and resolved), and **5** shared
  evidence rows (NM-07 portfolio, PI-05 forecast, PI-04 regime, NM-02
  observations, NM-08 risk) with real source timestamps.
- **Decision aggregation** rendered **NO_ACTION**, evidence strength
  **Insufficient**, confidence **Not claimed**, **0** unresolved conflicts,
  supporting agent `fin-regime`, and the authority boundary banner.
- **Handoff to PI-12** (added by fix 18) rendered the live document:
  recommendation **No Action**, `advisory_only` **Yes**, `production_authority`
  **No**, `order_submitted` **No**, `eligible_research_signal` **No**, the four
  requirement lines, the PI-12 → NM-08 → NM-10 execution path, and
  `agents_can_submit_orders`/`agents_can_override_kill_switch`/
  `agents_can_change_modes` all **No**.
- Selecting cancelled `mac_55a37e2ab89a43ba9389a6bb75184553` showed “No handoff
  yet” and issued **no** handoff request (fix 19), then reselecting the completed
  cycle restored the document. A reload and the cycle switch produced **no
  console errors**; `npm run build` passes.

## Documentation updated

- `docs/PREDICTION_INTELLIGENCE_ARCHITECTURE.md`: PI-17 status/scope, roadmap,
  module map, evidence/orchestration/aggregation design, authority handoff, API
  surface, verification and limitations.
- `docs/ARCHITECTURE.md`: PI-16/PI-17 boundary summary and unchanged authority
  rule.
- `docs/API.md`: regenerated with 766 routes. The working copy had also lost the
  generated PI-13–PI-17 sections and several route descriptions; regeneration
  restored them, and the `--check` contract now passes.
- This is the single `docs/PI17_COMPLETION_REPORT.md`.

## Remaining limitations and unverified requirements

- Specialists are transparent deterministic rule evaluators over stored
  evidence, not LLM reasoning or a new predictive model. No multi-agent
  probability calibration is claimed.
- The live context loader supplies technical, prediction, regime, risk, portfolio
  and optional RL/adaptive evidence. Fundamental and research agents need
  point-in-time records from their owning systems; without them they abstain.
  Long-horizon confidence can therefore be honestly lower or NO_ACTION.
- PI-12 consumes the handoff as read-only evidence attached to its proposals, not
  as an execution bridge. It cannot gate, trigger, approve or veto a trade;
  re-derivation through PI-12, NM-08 and NM-10 remains mandatory. The integration
  is verified by focused tests, not by a live decision run that happened to have a
  matching completed PI-17 cycle in the live store.
- The real application cycle was created and run through the page controls. The
  live run exercised the degraded/NO_ACTION path; a BUY/SELL cycle was not
  produced by the live data because the stored market history is older than the
  verification date, so the positive-direction rendering of the handoff and
  decision cards remains covered by component/route tests rather than a live run.
- Three earlier degraded cycles remain in the isolated verification store from
  the defects fixed above. They are visible history and did not alter financial
  state.
- No scheduler or automatic cycle loop was added; cycles are explicit operator
  actions.
- The full repository test suite was not run. The focused suite, affected
  regression, API suite, both mypy targets, focused Ruff, frontend build and API
  reference check passed.
- A timed-out Python thread cannot be forcibly killed. The executor abandons the
  future and excludes its result, and the built-in specialists are bounded, but
  cooperative cancellation cannot interrupt an injected agent blocked inside a
  third-party call.
- Historical `as_of` cycles can only use sources that retain point-in-time
  records. Latest-only feature/regime/risk/portfolio snapshots are rejected as
  future-dated; the cycle then degrades or returns WAIT/NO_ACTION rather than
  backfilling an answer.
- With no portfolio id, the required risk and portfolio specialists abstain, so
  a complete BUY/SELL decision is not available. This is now stated in the
  workspace instead of presenting the field as merely optional.
- This work was not staged, committed, pushed, tagged, merged or released.
  PI-18 was not started.
