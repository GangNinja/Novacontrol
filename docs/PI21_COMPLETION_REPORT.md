# PI-21 — Advanced Autonomous Portfolio Management: Completion Report

Date: 2026-11-18
Branch: `feature/novamarket`
Status: **PASS** (focused and affected-area verification after a corrective audit; limitations listed below)

## 1. Initial repository assessment

- Confirmed the work was on `feature/novamarket` with no staged files before implementation.
- Reviewed the consolidated PI-01 through PI-20 reports and treated their scoped PASS verdicts as the prerequisite baseline. No prerequisite regression was found by the affected-area tests run for this phase.
- Inspected the authoritative NM-07 portfolio/valuation/ledger path, NM-08 risk contracts, NM-10 paper execution and reconciliation, AT-01 to AT-06 autonomy and exit supervision, PI-12 orchestration, PI-17 agents, PI-18 optimisation, PI-19 research and PI-20 dashboard contracts.
- Existing position monitoring, pending-order/reservation queries, valuation history, forecast stores, research evidence and PI-18 targets were reused. No second portfolio ledger, valuation service, risk engine, execution engine or agent orchestrator was created.
- No PI-22 work was started.

## 2. Existing components reused

| Capability | Reused authority/contract |
|---|---|
| Portfolio, positions, valuation | Existing NM-07 portfolio service and authoritative snapshots |
| Cash and reservations | Existing wallet/portfolio cash plus NM-10 active reservations |
| Risk | NM-08 portfolio risk assessment and approved AT-03 proposals |
| Paper execution | NM-10 through the existing PI-12 `autonomy_execution_run` workflow |
| Autonomy modes, lease, kill switch, recovery | Existing AT-01/PI-12 runtime and recovery path |
| Forecasts and lifecycle | PI-05..PI-11 stored forecast/evidence views |
| Feature/regime intelligence | PI-04/PI-16 regime and instrument evidence |
| Multi-agent findings | PI-17 stored decision/evidence records |
| Allocation and rebalancing | PI-18 optimisation targets and metrics |
| Financial research | PI-19 stored research outputs |
| Presentation | PI-20/UI components, `useApi`, hash navigation, toast system |

## 3. Portfolio-management architecture

The implementation lives in `src/novacontrol/novamarket/portfolio_management/`:

- `models.py`: management vocabulary, cycle/handoff statuses, horizon policy, validated `ManagementConfig`, stable IDs, authority contract and typed errors.
- `store.py`: isolated `novamarket_portfolio_management.sqlite3` with only `pm_*` tables. Atomic cycle claim, one active production cycle, idempotency keys, bounded queries, stale-cycle interruption and recovery.
- `evidence.py`: `ManagementSources` callback contract and a per-cycle cached `EvidenceBook`.
- `service.py`: lifecycle, status/contract/navigation, configuration versions, monitoring snapshots, anomaly detection, cycle orchestration, recovery and shutdown.
- `analysis.py`: position assessment, opportunity discovery, exposure, capital/cash plans, rebalancing, decisions, proposal matching, analytics and evidence views.
- `actions.py`: cycle queries, explicit PI-12 handoff, NM-10 reconciliation, recovery and events.
- `__init__.py`: public API.

`PortfolioManagementService` is composed from `PortfolioManagementAnalysisMixin` and `PortfolioManagementActionsMixin`; the backend package passes `ruff`, `ruff format` and `mypy`.

Each cycle records: cycle ID, portfolio ID, snapshot version, start/completion timestamps, market-price and forecast versions, research/optimisation/risk references, decision-configuration version, horizon, trigger, status and warnings. Decision records are append-only management research; submitted/executed/reconciled state is stored separately.

## 4. Portfolio monitoring evidence

The authoritative snapshot preserves NC values, INR equivalents, valuation timestamps, source prices and snapshot versions. Monitoring detects missing prices, stale valuations, unexpected quantity changes without a matching fill, expired pending orders, reservation/order mismatches and concentration changes. Missing evidence remains `null`; it is never converted to zero. A missing price makes the cycle `waiting_for_evidence`, and a stale snapshot is `completed_with_warnings` or `waiting_for_evidence` as appropriate. An authoritative valuation without a total is marked `valuation_available: false` / `missing_valuation: true`; the cycle is `waiting_for_evidence` and cannot hand off. The numeric planning total is not presented as an available valuation in that case.

## 5. Position-intelligence implementation

Every holding is assessed from entry/current price, quantity, invested capital, value, unrealised P&L, return, holding duration, weight, sector, liquidity, valid forecast direction/uncertainty/agreement, market regime, research, corporate/news evidence, agent findings, portfolio contribution and existing AT-05 protective configuration. Outputs are `strong_position`, `hold_position`, `review_position`, `reduce_position`, `exit_candidate` and `insufficient_evidence`.

P&L alone does not decide the assessment: a negative position with improving forward evidence can stay a hold, and a positive position with deteriorating evidence can be reduced. Assessment scores are labelled `evidence_score_not_probability`. Existing AT-05 stop-loss, trailing-stop and target-price configuration downgrades PI-21 exits to wait/review; the established `stop_loss → trailing_stop → target_price` precedence remains solely in AT-05.

## 6. Opportunity assessment

Discovery consumes only stored, valid, horizon-compatible, confirmed forecasts. Forecasts marked `unconfirmed`, `pending`, `awaiting_confirmation` or `initial` are excluded from actionable evidence and cannot create an entry. It evaluates expected return, uncertainty, model disagreement, regime, technical/liquidity evidence, research, corporate events, sector context, portfolio compatibility and duplication. Missing forecasts produce no opportunities. New positions are bounded by the remaining `max_positions` capacity; existing holdings are still reviewed in full. Sector and position limits can also mark an opportunity ineligible. Each opportunity carries its forecast versions, evidence score (not a probability), proposed allocation, compatibility and reason.

## 7. Capital deployment

Capital planning reconciles available NC, reserved capital, pending buy commitments, target allocations, PI-18 output, expected return, uncertainty, costs and NM-08 restrictions. It supports `deploy_capital`, `partial_deployment`, `retain_cash`, `wait_for_confirmation` and `no_deployment`. Available capital is reduced by the larger of NM-10 cash reservations and active pending buy commitments, including when the pending-notional field is zero but reservations exist. A single cycle-level deployment budget is shared by `add` and `open` decisions: their combined notional cannot exceed deployable capital, and an eligible opportunity that does not receive funding is reported as `wait`. Unavailable cash is `null`, not zero. Reserve targets are configurable. Unused cash is explicitly not a failed optimisation, and no cash balance is modified.

## 8. Entry and exit recommendations

Entries are portfolio-aware and support `new_position`, `add_to_existing_position`, `wait` and `no_entry`. They require confirmed, actionable evidence, acceptable uncertainty/agreement, favourable regime/research, sector and correlation compatibility, liquidity, cost and available capital. The recommendation explains both supporting and conflicting evidence.

Position management supports `maintain`, `reduce`, `exit`, `wait` and `review_required`. Deteriorating forecasts, rising uncertainty, regime changes, negative events, concentration, opportunity cost and protective restrictions are considered. PI-21 never exits on price decline alone and does not bypass AT-05 or NM-08. It never submits an order; handoff continues only an already persisted, NM-08-approved AT-03 proposal through PI-12.

## 9. Exposure and cash management

Exposure reports instrument, sector, industry, correlation-proxy, cash, concentration (HHI/top-three), diversification and risk contribution using PI-18 metrics. Configurable research thresholds generate warnings for concentration, sector imbalance, correlated holdings, theme concentration, insufficient diversification and liquidity. These thresholds are explicitly research-only and do not override NM-08.

Cash management monitors available cash, reservations, pending commitments, cash ratio, unallocated capital and expected requirements. It recommends retention, deployment, reserve preservation, reduced deployment or waiting. It supports reserve targets without forcing turnover and never creates money or changes an authoritative balance.

## 10. Rebalancing coordination

Rebalancing compares current and PI-18 target weights and supports `open_position`, `increase_position`, `reduce_position`, `close_position`, `maintain_position`, `hold_cash` and `no_rebalancing_required`. It applies configurable weight thresholds, minimum trade notional, expected improvement, maximum turnover, cooldown hours and maximum changes per cycle. Protective reduce/exit actions may bypass a non-urgent cooldown; the proposal never executes. Every change records its notional, cost, rationale and PI-12/NM-08 requirements.

## 11. Multi-agent and RL integration

PI-17 findings are attached as supporting or contradictory evidence and unresolved conflicts are preserved; no majority vote can override NM-08. PI-15/PI-16 policies are exposed as advisory-only suitability evidence with policy/regime/history context. `activation_available` and `production_authority` are false; PI-21 has no promotion, mode-changing or policy-execution path.

## 12. PI-12 integration evidence

The management cycle and execution are deliberately separate. `handoff_cycle` requires autonomous mode, a valid lease, no kill switch, a review created in autonomous mode with a completed or completed-with-warnings status, and a matching persisted AT-03 proposal. Monitoring-only, waiting-for-evidence, cancelled and interrupted reviews are refused. At handoff it re-reads the persisted AT-03 proposals instead of trusting the cycle's stored eligibility, and it rechecks `risk_approved`, an approved risk outcome and expiry. If the proposal state cannot be read, the handoff is refused without submitting anything. It calls the existing `autonomy_execution_run(proposal_id=...)` callback and never constructs an order. `order_submission` is true only when an execution/order acknowledgement or a submitted/filled execution status is actually returned. The focused tests verify successful handoff, no matching proposal, risk rejection, proposal-state revalidation, duplicate handoff replay, monitoring-only refusal, mode refusal, kill-switch refusal, execution exception, truthful no-submission reporting and reconciliation.

## 13. NM-08 risk-authority evidence

NM-08 remains the sole risk authority. PI-21 forwards `risk_engine_reference`, consumes `risk_outcome`/restrictions and only matches proposals whose persisted status/risk outcome is approved. Handoff re-reads that state immediately before calling PI-12; a stored but subsequently rejected, expired or missing proposal is not submitted. It does not create risk policy, override a rejection, or turn a research exposure warning into a risk decision. The authority contract exposes `risk_override: false`.

## 14. NM-10 execution and reconciliation evidence

NM-10 remains the sole paper-execution and reconciliation authority. Submitted, pending, partially filled, filled, failed and unknown outcomes are distinct. A partial fill or a mix of filled and failed outcomes is reported as `partially_executed`; an unknown acknowledgement requires reconciliation; a lost acknowledgement is never retried. Reconciliation uses the existing NM-10 execution record and records filled/remaining quantities. Idempotent handoff prevents a duplicate order after replay/restart. The handoff payload reports `order_submission: false` when PI-12 produced only an error, rather than claiming an order was sent.

## 15. API and frontend integration

The backend exposes 21 authenticated routes under `/market/portfolio-management/*`: status, contracts, navigation, config read/update, review, cycles list/detail/events, handoff, reconcile, recover, snapshot, positions, opportunities, decisions, capital, cash, exposure, rebalancing and analytics. Reads require API authentication; every mutation additionally requires the dedicated autonomy-operator token, checked server-side. `docs/API.md` is regenerated and in sync at **857 routes**.

The React workspace is `web/src/pages/PortfolioManagement.tsx`, mounted through `App.tsx` at `#/portfoliomanagement` and `nav.ts`. It has seven views: Command overview, Position intelligence, Opportunities, Capital & cash, Rebalancing, Decision history and Autonomous status. It uses existing UI components and only calls review/handoff/reconcile/recover; there are no direct financial mutation controls. `npm run build` (`tsc -b && vite build`) passes.

## 16. Financial consistency and isolation evidence

- The management database contains only `pm_*` tables plus SQLite's internal `sqlite_sequence`; a test asserts no financial tables and no order/position/ledger tables.
- PI-21 has no imports or calls that mutate portfolios, positions, cash, ledger or fills, and no direct NM-10 order endpoint. Handoff is the only execution path and requires an existing approved proposal.
- The authority contract reports `read_only_financial: true`, `portfolio_mutation: false`, `ledger_write: false`, `order_submission: false`, `risk_override: false`, `policy_activation: false`, `broker_connected: false`, `live_trading: false`, and 1 NC = INR 1.
- In the real FastAPI application, an integration test created and funded a portfolio through the existing authoritative wallet/portfolio APIs, ran a PI-21 review and read back the persisted cycle. The snapshot total matched the authoritative virtual capital and no financial mutation was reported.
- Missing data, stale data and unavailable backups are reported honestly rather than zero-filled or fabricated.

## 17. Actual test counts and results

| Check | Result |
|---|---|
| Focused PI-21 suite `tests/test_novamarket_pi21.py` | **78 passed** after corrective audit |
| Affected integration batch: PI-21 + NovaMarket API + Web API + Web platform | **261 passed, 1,323 subtests** |
| PI-12, PI-17, PI-18, PI-19, PI-20 (run earlier; corrective audit changed no package they depend on) | **201 passed** |
| Backend package `ruff check` / `ruff format --check` | passed (current audit) |
| Targeted backend `mypy --platform win32` | passed (current audit) |
| API reference generation `--check` | in sync, 857 routes (no route changes during the corrective audit) |
| Web production build `npm run build` | passed (frontend unchanged during the backend-only corrective audit) |

The focused suite covers coordinator setup, authoritative snapshots, missing valuation, position/opportunity/capital/entry/exit/exposure/cash/rebalance behavior, pending-commitment cash availability, shared buy budgets, unconfirmed forecasts, max-position capacity, horizons, decision explainability, agents/RL advisory boundaries, analytics, persistence, idempotency, one-cycle concurrency, modes, operator authorization, kill switch, handoff, live proposal revalidation, risk rejection, partial fills, reconciliation, truthful submission reporting, recovery-to-disabled and recovery failure reporting, isolation, API surface/authentication, real application integration and frontend wiring.

## 18. Defects fixed

The corrective audit found and fixed the following implementation defects:

- **Pending commitments were ignored when reservations were zero.** Cash availability read
  `pending_buy_notional` first even when it was zero, so active reservations were not
  subtracted. The snapshot now subtracts the larger of reservations and active pending buy
  commitments, and reports both components.
- **Multiple buy decisions could spend the same cash.** Position additions and opportunity
  entries each used the full deployable amount independently. A single per-cycle deployment
  budget is now shared across `add` and `open` decisions; proposed buy notional is bounded by
  deployable capital, and unfunded opportunities become `wait`.
- **Unconfirmed forecasts could drive entries.** Forecasts marked
  unconfirmed/pending/awaiting-confirmation/initial were treated as actionable. They are now
  excluded from consensus and from `add`/`open` eligibility.
- **Missing authoritative total valuation became zero.** A missing total is now explicit
  (`missing_valuation: true`, `valuation_available: false`), makes data status `unavailable`,
  makes the cycle `waiting_for_evidence`, adds a warning and blocks handoff.
- **Handoff trusted stale cycle eligibility and accepted non-actionable reviews.** Handoff now
  requires a review created in autonomous mode with completed/completed-with-warnings status;
  monitoring-only, waiting-for-evidence, cancelled and interrupted reviews are refused.
- **Handoff did not re-read risk state.** It now re-reads persisted AT-03 proposals and requires
  the exact proposal to remain `risk_approved`, approved and unexpired. A risk-store read
  failure refuses the handoff without submitting anything.
- **`order_submission` could be true when no order existed.** It is now derived from
  execution/order evidence or an actual submitted/filled status, and is false for an execution
  that failed before submission. Mixed filled/failed outcomes report `partially_executed`.
- **Recovery could fail after local recovery succeeded.** An autonomy callback exception no
  longer loses the interrupted-cycle result; the response returns local recovery with
  `ok: false`, `recovery_complete: false`, `mode_verified: false` and the error. A successful
  callback still returns to disabled.
- **`max_positions` was unused.** New entries are now bounded by remaining position capacity;
  existing holdings are still reviewed in full.
- Frontend corrections from earlier in the phase remain: toast tone/API, `Column.label`,
  `PageHeader.description`, single-row `KeyValue`, existing tab/grid classes, and cycle-history
  parsing from `items`.

No direct portfolio, position, cash, ledger, fill or order mutation was introduced by these
fixes.

## 19. Remaining limitations

- Scheduled reviews are invoked by the existing single NovaMarket autonomy runtime tick (`_runtime_tick` → `PortfolioManagementService.maybe_run`); PI-21 adds no second scheduler, thread or timer. The coordinator owns only its cadence (default hourly, persisted attempt time) and re-checks mode, lease and kill switch before running one due `trigger: scheduled` cycle for the first authoritative portfolio. Ticks that are not due report `not_due` instead of starting work.
- Forecast, research, liquidity and agent evidence quality depends on the existing upstream services. PI-21 does not fabricate evidence to fill gaps and may therefore return `wait`, `review` or `waiting_for_evidence`.
- UI verification was limited to a successful TypeScript/Vite production build and source/route contract checks. The page was not interactively clicked through in a browser during this phase.
- Real-money trading, broker connectivity and short selling remain intentionally unsupported. 1 NC = INR 1 is a presentation/accounting convention, not a real currency.
- Historical performance analytics depend on the available authoritative valuation/fill history; missing history is reported as unavailable rather than reconstructed.
- Handoff depends on a live read of the existing AT-03 proposal store. If that store is unavailable, the handoff is deliberately refused rather than relying on the cycle's earlier snapshot.
- Recovery can guarantee `mode: disabled` only when the existing autonomy recovery callback succeeds. If the callback fails, the local cycles are still interrupted and the response reports incomplete/unverified recovery instead of claiming the runtime is disabled.

## 20. Final acceptance verdict

**PASS** for PI-21 within the recorded evidence. Portfolio monitoring, position intelligence, opportunity discovery, capital/cash planning, entry/exit recommendations, exposure management, rebalancing, multi-horizon decisions, explainability, multi-agent/RL advisory integration, API and workspace are implemented. NM-08 remains the sole risk authority, NM-10 the sole paper-execution/reconciliation authority, and PI-12 remains the only execution workflow. Recovery returns to disabled through the existing autonomy callback when that callback succeeds, and reports incomplete/unverified recovery when it does not. Idempotency and single-cycle concurrency are enforced; no direct financial mutation was introduced. PI-22 was not started.
